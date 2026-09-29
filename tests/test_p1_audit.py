"""P1 in the tiered audit: `saddle audit --tiered --task-requirements FILE`.

The fixture is a sorted container whose in-place repeat skips re-sorting,
with the agent's own test pinning the broken value (the shape of a
sorted-list task's `*=`): every existing gate passes it. The requirements
file is sealed by hand with three agreeing predictions, agreeing executed
references and three accepting known-correct probes (step 2 builds those),
so the only open switch is `P1_REFUSAL_LICENSED`.
"""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from saddle import gates
from saddle.auditor import TASK_REQUIREMENTS, Auditor, AuditorConfig
from saddle.cli import main
from saddle.packet import compile_packet
from saddle.task_examples import Outcome
from saddle.task_requirements import seal

TASK = """# Box

`Box(items=None)` holds items in ascending order.

- `b *= n` repeats the items n times in place.
- Stdlib only.
"""

BASE = """class Box:
    def __init__(self, items=None):
        self.items = sorted(items or [])

    def __iter__(self):
        return iter(self.items)
"""

IMUL_BAD = """
    def __imul__(self, n):
        self.items = self.items * n
        return self
"""
IMUL_GOOD = """
    def __imul__(self, n):
        self.items = sorted(self.items * n)
        return self
"""
TEST_BASE = "from box import Box\n\n\ndef test_init():\n    assert list(Box([2, 1])) == [1, 2]\n"
TEST_IMUL = "\n\ndef test_imul():\n    b = Box([2, 1])\n    b *= 2\n    assert list(b) == {want}\n"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def make_tree(root: Path, imul: str, want: str) -> Path:
    (root / "tests").mkdir(parents=True)
    (root / "box.py").write_text(BASE)
    (root / "tests" / "test_box.py").write_text(TEST_BASE)
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    (root / "box.py").write_text(BASE + imul)
    (root / "tests" / "test_box.py").write_text(TEST_BASE + TEST_IMUL.format(want=want))
    return root


@pytest.fixture
def bad(tmp_path: Path) -> Path:
    """The known-bad tree: its own test pins [1, 2, 1, 2]."""
    return make_tree(tmp_path / "bad", IMUL_BAD, "[1, 2, 1, 2]")


@pytest.fixture
def good(tmp_path: Path) -> Path:
    """The known-good twin."""
    return make_tree(tmp_path / "good", IMUL_GOOD, "[1, 1, 2, 2]")


def requirements(tmp_path: Path) -> Path:
    o = Outcome.of("value", "[1, 1, 2, 2]").to_dict()
    record = {
        "task_text": TASK,
        "examples": [
            {
                "id": "E-001",
                "units": ["S-002", "S-001"],
                "setup": ["from box import Box", "b = Box([2, 1])", "b *= 2"],
                "call": "list(b)",
                "args": ["[2, 1]", "2"],
                "predictions": [
                    {"outcome": o, "decides": "repeats the items n times", "raw_sha256": f"r{i}"}
                    for i in range(3)
                ],
                "references": [{"status": "ran", "outcome": o}] * 3,
                "probes": [
                    {"sha256": f"{i}" * 64, "status": "ran", "outcome": o} for i in range(3)
                ],
            }
        ],
        "not_executable": [{"unit": "S-003", "reason": "environment"}],
        "probes": [{"sha256": f"{i}" * 64, "source": "oracle-pass"} for i in range(3)],
    }
    path = tmp_path / "task-requirements.json"
    path.write_text(json.dumps(seal(record)))
    return path


def audit(repo: Path, *extra: str) -> tuple[int, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(["audit", "--repo", str(repo), "--no-cache", *extra], stdout=out, stderr=err)
    return code, out.getvalue() + err.getvalue()


# -- R1 / R11: the refusal reaches tier 1 through the CLI --------------------------


def test_r1_r11_the_cli_refuses_the_pinned_misreading_in_tier_one(
    bad: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Red on main: the same tree, audited with `--tiered` alone, is accepted
    by every gate (the next test); with the requirements file it is refused
    by the task-text check, naming the unit, route, example and the changed
    line the example ran."""
    monkeypatch.setattr(gates, "P1_REFUSAL_LICENSED", True)
    code, text = audit(bad, "--tiered", "--task-requirements", str(requirements(tmp_path)))
    assert code == 1
    line = next(x for x in text.splitlines() if x.startswith("fail") and TASK_REQUIREMENTS in x)
    assert "[code-wrong] S-002" in line
    assert "via executed-reference (3/3), probe (3/3)" in line
    assert "b *= 2; list(b) expected [1, 1, 2, 2], got [1, 2, 1, 2]" in line
    assert "ran changed lines box.py:9 (Box.__imul__)" in line
    assert text.rstrip().endswith("verdict: refuse")


def test_r1_without_the_file_every_gate_accepts_the_same_tree(bad: Path) -> None:
    """The red half: nothing else in the tiered battery sees the misreading."""
    auditor = Auditor(bad, "HEAD", AuditorConfig())
    first = auditor.tier1()
    assert first.passed
    assert TASK_REQUIREMENTS not in [f.gate for f in first.findings]


def test_r14_at_question_strength_the_same_tree_is_a_question(bad: Path, tmp_path: Path) -> None:
    auditor = Auditor(bad, "HEAD", AuditorConfig(task_requirements=requirements(tmp_path)))
    first = auditor.tier1()
    p1 = {f.gate: f for f in first.findings}[TASK_REQUIREMENTS]
    assert (p1.verdict, first.passed, first.needs_you) == ("question", True, True)
    assert p1.detail.endswith("[would refuse at full strength]")
    assert p1.cites[1].startswith("question strength: dev-probe floor unmet")


def test_r1_g_the_known_good_twin_passes(good: Path, tmp_path: Path) -> None:
    auditor = Auditor(good, "HEAD", AuditorConfig(task_requirements=requirements(tmp_path)))
    first = auditor.tier1()
    p1 = {f.gate: f for f in first.findings}[TASK_REQUIREMENTS]
    assert (p1.verdict, p1.detail) == ("pass", "1 example(s) matched the task text")
    assert first.passed
    assert not first.needs_you


def test_the_requirements_file_is_part_of_the_cache_key(good: Path, tmp_path: Path) -> None:
    path = requirements(tmp_path)
    plain = Auditor(good, "HEAD", AuditorConfig())._key(1, "t", "b")
    with_p1 = Auditor(good, "HEAD", AuditorConfig(task_requirements=path))._key(1, "t", "b")
    assert plain != with_p1
    path.write_text(path.read_text() + " ")
    assert Auditor(good, "HEAD", AuditorConfig(task_requirements=path))._key(1, "t", "b") != with_p1
    missing = AuditorConfig(task_requirements=tmp_path / "nope.json")
    assert Auditor(good, "HEAD", missing)._key(1, "t", "b") not in (plain, with_p1)


# -- R9 through the auditor, R10 in the packet ----------------------------------------


def test_r9_a_tampered_file_asks_and_never_passes_silently(good: Path, tmp_path: Path) -> None:
    path = requirements(tmp_path)
    data: dict[str, Any] = json.loads(path.read_text())
    data["examples"][0]["call"] = "list(b) + [0]"
    path.write_text(json.dumps(data))
    first = Auditor(good, "HEAD", AuditorConfig(task_requirements=path)).tier1()
    p1 = {f.gate: f for f in first.findings}[TASK_REQUIREMENTS]
    assert p1.verdict == "question"
    assert p1.detail.startswith("P1 could not run: requirements file does not match the task")
    assert first.needs_you  # never a silent pass
    assert first.passed  # and never a refusal


def test_r10_a_not_executable_unit_is_named_in_the_basis_and_the_packet(
    good: Path, tmp_path: Path
) -> None:
    journal = tmp_path / "proofs.jsonl"
    config = AuditorConfig(task_requirements=requirements(tmp_path), journal=journal)
    first = Auditor(good, "HEAD", config).tier1()
    p1 = {f.gate: f for f in first.findings}[TASK_REQUIREMENTS]
    assert "not executable S-003: environment" in p1.cites[1]
    gaps = {r.key: r for r in compile_packet(journal).rows}["not-proven"].items
    assert "Not judged by the task-text check: not executable S-003: environment." in gaps


def test_the_cli_says_question_with_exit_four(
    bad: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 2 is not blocked by a question; its outcome is stubbed here to keep
    the suite fast (the real tier 2 is covered by test_auditor)."""
    from saddle.auditor import Findings

    def quiet_tier2(self: Auditor, tree: Path | None = None) -> Findings:
        return Findings(tier=2, key="k2", findings=())

    monkeypatch.setattr(Auditor, "tier2", quiet_tier2)
    code, text = audit(bad, "--task-requirements", str(requirements(tmp_path)))
    assert code == 4
    assert text.rstrip().endswith("verdict: question")
    assert "question       task-requirements" in text


# -- `saddle requirements` ---------------------------------------------------------


def test_requirements_census_and_verify(tmp_path: Path) -> None:
    task = tmp_path / "task.md"
    task.write_text(TASK)
    out = io.StringIO()
    assert main(["requirements", "census", str(task)], stdout=out) == 0
    assert out.getvalue().startswith("3 unit(s): 3 binding, 0 delegating")
    path = requirements(tmp_path)
    out = io.StringIO()
    assert main(["requirements", "verify", str(path), str(task)], stdout=out) == 0
    assert "checks out: 1 example(s), 1 not executable, 0 unanswered, 0 cut" in out.getvalue()
    task.write_text(TASK + "\n- One more rule.\n")
    out = io.StringIO()
    assert main(["requirements", "verify", str(path), str(task)], stdout=out) == 1
    assert out.getvalue().startswith(
        "question: P1 could not run: requirements file does not match the task"
    )


def test_requirements_extract_seals_a_file_through_the_client(
    good: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_task_passes import Scripted

    from saddle import cli

    client = Scripted(
        {
            "inputs": [
                {
                    "units": ["S-002"],
                    "setup": ["from box import Box", "b = Box([2, 1])", "b *= 2"],
                    "call": "list(b)",
                    "args": ["[2, 1]", "2"],
                }
            ],
            "not_executable": [{"unit": "S-003", "reason": "environment"}],
        }
    )

    class Client:
        def __init__(self, **kw: object) -> None:
            self.kw = kw

        def __enter__(self) -> Scripted:
            return client

        def __exit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(cli, "_api_key", lambda: "test-key")
    monkeypatch.setattr(cli, "VllmClient", Client)
    task = tmp_path / "task.md"
    task.write_text(TASK)
    target = tmp_path / "sealed.json"
    out = io.StringIO()
    argv = ["requirements", "extract", str(task), "--out", str(target), "--repo", str(good)]
    assert main([*argv, "--model", "m"], stdout=out) == 0
    assert out.getvalue().startswith(f"sealed {target} (")
    assert "1 example(s) over 3 unit(s); 1 not executable, 1 unanswered, 0 cut" in out.getvalue()
    record = json.loads(target.read_text())
    assert record["model"] == "m"
    # the passes saw the baseline's `Box` signature
    assert any("class Box" in prompt for _, prompt, _, _ in client.sent)


def test_requirements_extract_runs_and_seals_the_probes_it_is_given(
    good: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--probe-tree` seals an oracle-PASS tree and `--reference` a user one,
    each by hash, with every example's outcome on it; a bad probe stops the
    extraction before the model is called."""
    from test_task_passes import Scripted

    from saddle import cli
    from saddle.task_requirements import tree_sha256

    client = Scripted(
        {
            "inputs": [
                {
                    "units": ["S-002"],
                    "setup": ["from box import Box", "b = Box([2, 1])", "b *= 2"],
                    "call": "list(b)",
                    "args": ["[2, 1]", "2"],
                }
            ]
        }
    )

    class Client:
        def __init__(self, **kw: object) -> None:
            pass

        def __enter__(self) -> Scripted:
            return client

        def __exit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(cli, "_api_key", lambda: "test-key")
    monkeypatch.setattr(cli, "VllmClient", Client)
    task = tmp_path / "task.md"
    task.write_text(TASK)
    target = tmp_path / "sealed.json"
    reference = tmp_path / "reference"
    reference.mkdir()
    (reference / "box.py").write_text(BASE + IMUL_GOOD)
    argv = ["requirements", "extract", str(task), "--out", str(target), "--repo", str(good)]
    out = io.StringIO()
    code = main([*argv, "--probe-tree", str(good), "--reference", str(reference)], stdout=out)
    assert code == 0
    listed = [(tree_sha256(good), "oracle-pass"), (tree_sha256(reference), "user")]
    assert (
        out.getvalue()
        .rstrip()
        .endswith(
            "0 cut, 2 known-correct probe(s): "
            + ", ".join(f"{src} {sha[:12]}" for sha, src in listed)
        )
    )
    record = json.loads(target.read_text())
    assert [(p["sha256"], p["source"]) for p in record["probes"]] == listed
    assert [p["outcome"]["text"] for p in record["examples"][0]["probes"]] == ["[1, 1, 2, 2]"] * 2
    out = io.StringIO()
    assert main(["requirements", "verify", str(target), str(task)], stdout=out) == 0
    assert (
        out.getvalue()
        .rstrip()
        .endswith(
            "0 cut, 2 known-correct probe(s): "
            + ", ".join(f"{src} {sha[:12]}" for sha, src in listed)
        )
    )
    sent = len(client.sent)
    out = io.StringIO()
    assert main([*argv, "--reference", str(tmp_path / "absent")], stdout=out) == 2
    assert out.getvalue().startswith("error: probe tree ")
    assert len(client.sent) == sent  # no model call
    out = io.StringIO()
    assert main([*argv], stdout=out) == 0
    assert (
        out.getvalue().rstrip().endswith("0 cut, no known-correct probe (route (b) can only ask)")
    )
