"""P1 at audit time: the sealed requirements file and the example driver.

Every hash the file carries is checked before an example runs, and any
mismatch makes the gate a question, never a pass or a refusal (R9). The
driver runs real tree code in the tests gate's sandbox: values, raises,
interface failures (R4), hangs (R5), and a driver that cannot run (R9-c).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from saddle import gates, task_requirements
from saddle.evidence import CapturedRun
from saddle.task_examples import Example, Outcome, Prediction, Probe, Reference, TreeOutcome
from saddle.task_requirements import (
    MISMATCH,
    ProbeTree,
    RequirementsError,
    check_tree,
    load,
    run_examples,
    seal,
    tree_sha256,
)

TASK = """# Tiny

- `twice(xs)` returns the list `xs` repeated twice, sorted.
- `wrap(x)` wraps a non-list as `[x]`.
"""

MODULE_BASE = "def twice(xs):\n    return sorted(xs * 2)\n\n\ndef wrap(x):\n    return [x]\n"
MODULE_BAD = "def twice(xs):\n    return xs * 2\n\n\ndef wrap(x):\n    return [x]\n"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A repo whose baseline commit is correct and whose working tree breaks `twice`."""
    root = tmp_path / "tree"
    root.mkdir()
    (root / "m.py").write_text(MODULE_BASE)
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    (root / "m.py").write_text(MODULE_BAD)
    return root


def example(
    eid: str,
    call: str,
    text: str,
    *,
    setup: tuple[str, ...] = ("from m import twice",),
    units: tuple[str, ...] = ("S-001",),
) -> dict[str, Any]:
    o = Outcome.of("value", text)
    return {
        "id": eid,
        "units": list(units),
        "setup": list(setup),
        "call": call,
        "predictions": [
            {"outcome": o.to_dict(), "decides": "", "raw_sha256": f"r{i}"} for i in range(3)
        ],
        "references": [{"status": "ran", "outcome": o.to_dict()} for _ in range(3)],
        "probes": [
            {"sha256": f"{i}" * 64, "status": "ran", "outcome": o.to_dict()} for i in range(3)
        ],
    }


def sealed(tmp_path: Path, *examples: dict[str, Any], **extra: Any) -> Path:
    path = tmp_path / "task-requirements.json"
    shas = dict.fromkeys(p["sha256"] for e in examples for p in e.get("probes", ()))
    probes = [{"sha256": sha, "source": "oracle-pass"} for sha in shas]
    record = {"task_text": TASK, "examples": list(examples), "probes": probes, **extra}
    path.write_text(json.dumps(seal(record)))
    return path


# -- R9: the file and its hashes ------------------------------------------------


def test_a_sealed_file_loads_with_its_units_and_examples(tmp_path: Path) -> None:
    path = sealed(
        tmp_path,
        example("E-001", "twice([2, 1])", "[1, 1, 2, 2]"),
        not_executable=[{"unit": "S-002", "reason": "prose-only"}],
        cut=["S-002"],
    )
    req = load(path, TASK)
    assert [u.id for u in req.units.units] == ["S-001", "S-002"]
    assert req.examples[0].call == "twice([2, 1])"
    assert (req.not_executable, req.cut) == ((("S-002", "prose-only"),), ("S-002",))
    assert req.sha256 == json.loads(path.read_text())["file_sha256"]


def _edit(path: Path, change: Any) -> None:
    data = json.loads(path.read_text())
    change(data)
    path.write_text(json.dumps(data))


def _rehash(data: dict[str, Any]) -> None:
    body = {k: v for k, v in data.items() if k != "file_sha256"}
    data["file_sha256"] = task_requirements.sha256_text(task_requirements.canonical(body))


def _set(key: str, value: object) -> Any:
    def change(d: dict[str, Any]) -> None:
        d[key] = value
        _rehash(d)

    return change


def _unit_text(d: dict[str, Any]) -> None:
    d["units"][0]["text"] = "edited"
    _rehash(d)


@pytest.mark.parametrize(
    ("change", "why"),
    [
        (lambda d: d["examples"][0].update(call="twice([9])"), "its own hash does not match"),
        (_set("task_text", TASK + "x"), "the task text hash differs"),
        (_set("parser_sha256", "0" * 64), "sealed by another P1 build (parser differs)"),
        (_set("whitelist_sha256", "0"), "(whitelist differs)"),
        (_set("templates_sha256", "0"), "(templates differs)"),
        (_unit_text, "the units differ"),
    ],
)
def test_r9_a_file_changed_after_sealing_does_not_load(
    tmp_path: Path, change: Any, why: str
) -> None:
    path = sealed(tmp_path, example("E-001", "twice([2, 1])", "[1, 1, 2, 2]"))
    _edit(path, change)
    with pytest.raises(RequirementsError, match=MISMATCH) as info:
        load(path)
    assert why in str(info.value)


@pytest.mark.parametrize(
    ("content", "why"),
    [
        ("{not json", "cannot be read"),
        ('{"version": 2}', "not a version-1"),
        ("[]", "not a version-1"),
    ],
)
def test_an_unreadable_file_does_not_load(tmp_path: Path, content: str, why: str) -> None:
    path = tmp_path / "f.json"
    path.write_text(content)
    with pytest.raises(RequirementsError, match=why):
        load(path)
    with pytest.raises(RequirementsError, match="cannot be read"):
        load(tmp_path / "missing.json")


def test_a_file_for_another_task_or_a_malformed_one_does_not_load(tmp_path: Path) -> None:
    path = sealed(tmp_path, example("E-001", "twice([2, 1])", "[1, 1, 2, 2]"))
    with pytest.raises(RequirementsError, match="sealed for another task text"):
        load(path, TASK + "\nmore")
    bad = sealed(tmp_path, {"id": "E-1"})
    with pytest.raises(RequirementsError, match="malformed: KeyError"):
        load(bad)
    reason = sealed(tmp_path, not_executable=[{"unit": "S-1", "reason": "too hard"}])
    with pytest.raises(RequirementsError, match="reason 'too hard' is not allowed"):
        load(reason)


def test_r9_the_gate_on_a_tampered_file_is_a_question_naming_it(tree: Path, tmp_path: Path) -> None:
    path = sealed(tmp_path, example("E-001", "twice([2, 1])", "[1, 1, 2, 2]"))
    _edit(path, lambda d: d["examples"][0].update(call="twice([9])"))
    check = check_tree(tree, "HEAD", path)
    assert (check.verdict, check.passed) == ("question", True)
    assert check.detail.startswith(
        "P1 could not run: requirements file does not match the task: its own hash"
    )


# -- the driver on real tree code ---------------------------------------------


def run_one(
    tree: Path, call: str, setup: tuple[str, ...] = ("from m import twice",)
) -> TreeOutcome:
    e = Example("E-001", ("S-001",), setup, call, (Prediction(None, "", "r"),))
    got = run_examples(tree, [e])
    assert isinstance(got, dict)
    return got["E-001"]


def test_the_driver_returns_values_raises_and_the_lines_it_ran(tree: Path) -> None:
    got = run_one(tree, "twice([2, 1])")
    assert (got.kind, got.show()) == ("value", "[2, 1, 2, 1]")
    assert got.ran == ("m.py:2 (twice)",)
    raised = run_one(tree, "twice(None)")
    assert raised.kind == "raises"
    assert raised.raises[:2] == ("TypeError", "Exception")  # raised inside the tree: an outcome
    assert run_one(tree, "iter([1])", ()).kind == "opaque"


@pytest.mark.parametrize(
    ("setup", "call", "said"),
    [
        (("from m import twise",), "twise([1])", "setup: ImportError"),
        (("from m import twice",), "twice.nope([1])", "call: AttributeError"),
        (("from m import twice",), "twice(1, 2, 3)", "call: TypeError"),
        (("from m import twice",), "undefined_name", "call: NameError"),
        (("from m import twice", "x = twice(None)"), "x", "setup: TypeError"),
    ],
)
def test_r4_an_interface_failure_could_not_call(
    tree: Path, setup: tuple[str, ...], call: str, said: str
) -> None:
    got = run_one(tree, call, setup)
    assert got.kind == "could-not-call"
    assert got.detail.startswith(said)


def test_r5_an_example_that_loops_forever_is_a_hang(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(task_requirements, "EXAMPLE_TIMEOUT_S", 0.5)
    (tree / "loop.py").write_text("def spin():\n    while True:\n        pass\n")
    got = run_one(tree, "spin()", ("from loop import spin",))
    assert got.kind == "hang"


def test_a_refused_snippet_is_never_sent(tree: Path) -> None:
    e = Example("E-001", ("S-001",), ("import os",), "os.getcwd()", ())

    def never(*a: object, **k: object) -> CapturedRun:
        msg = "nothing should run"
        raise AssertionError(msg)

    got = run_examples(tree, [e], runner=never)
    assert got == {
        "E-001": TreeOutcome("could-not-call", detail="snippet refused: Import is not allowed")
    }


def _stub(result: CapturedRun, write: str | None = None) -> Any:
    def runner(argv: list[str], cwd: Path, **kw: Any) -> CapturedRun:
        if write is not None:
            Path(argv[-1]).write_text(write)
        return result

    return runner


@pytest.mark.parametrize(
    ("result", "write", "why"),
    [
        (CapturedRun((), 124, "", "", timed_out=True), None, "exceeded its 120 s wall"),
        (
            CapturedRun((), 1, "", "Segmentation fault"),
            None,
            "crashed (exit 1): Segmentation fault",
        ),
        (CapturedRun((), 0, "", ""), '{"E-001": {"kind": "weird"}}', "crashed (exit 0)"),
    ],
)
def test_r9_c_a_driver_that_cannot_run_is_a_reason_and_the_gate_asks(
    tree: Path, tmp_path: Path, result: CapturedRun, write: str | None, why: str
) -> None:
    e = Example("E-001", ("S-001",), (), "1", ())
    got = run_examples(tree, [e], runner=_stub(result, write))
    assert isinstance(got, str)
    assert why in got
    path = sealed(tmp_path, example("E-001", "twice([2, 1])", "[1, 1, 2, 2]"))
    check = check_tree(tree, "HEAD", path, runner=_stub(result, write))
    assert (check.verdict, check.passed) == ("question", True)
    assert check.detail.startswith("P1 could not run: the example driver")


# -- the gate on a tree ---------------------------------------------------------


def test_the_gate_names_the_changed_line_the_failing_example_ran(
    tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = sealed(
        tmp_path,
        example("E-001", "twice([2, 1])", "[1, 1, 2, 2]"),
        example("E-002", "wrap(3)", "[3]", setup=("from m import wrap",), units=("S-002",)),
    )
    monkeypatch.setattr(gates, "P1_REFUSAL_LICENSED", True)
    check = check_tree(tree, "HEAD", path)
    assert check.verdict == "fail"
    assert check.detail.endswith(
        "twice([2, 1]) expected [1, 1, 2, 2], got [2, 1, 2, 1]; ran changed lines m.py:2 (twice)"
    )
    assert [r.status for r in check.rows] == ["code-wrong", "pass"]
    monkeypatch.setattr(gates, "P1_REFUSAL_LICENSED", False)
    assert check_tree(tree, "HEAD", path).verdict == "question"
    (tree / "m.py").write_text(MODULE_BASE + "\n\nEXTRA = 1\n")
    assert check_tree(tree, "HEAD", path).verdict == "pass"


def test_unused_record_fields_are_kept_by_seal(tmp_path: Path) -> None:
    probes = [Probe("a", "ran", Outcome.of("value", "1"))]
    assert probes[0].outcome is not None
    assert Reference("ran").agrees(Outcome.of("value", "1")) is False
    record = seal({"task_text": TASK, "examples": [], "model": "m", "file_sha256": "stale"})
    assert record["model"] == "m"
    assert record["file_sha256"] != "stale"


# -- D-9: the known-correct probes, run at extraction (spec §4.5, R17) -------------
#
# The contract: a route (b) example is refusal-eligible only if the sealed
# file holds `PROBES_NEEDED` known-correct probes of one source and every one
# of them returned the example's expected outcome when it ran at extraction;
# a probe that returns another outcome makes the example `probe-rejected`
# (never judged), and a missing or unrunnable probe leaves it a question.

BAG = """# Bag

`Bag(items=None)` holds items in ascending order.

- `b.doubled()` returns the items repeated twice, in ascending order.
"""

BAG_BASE = (
    "class Bag:\n    def __init__(self, items=None):\n        self.items = sorted(items or [])\n"
)
DOUBLED = "\n    def doubled(self):\n        {body}\n"
SORTED = "return sorted(self.items * 2)"
UNSORTED = "return self.items * 2"
PAIRS = "return [x for x in self.items for _ in (0, 1)]"
PLUS = "return sorted(self.items + self.items)"
BAG_INPUT = {
    "units": ["S-002"],
    "setup": ["from bag import Bag", "b = Bag([2, 1])"],
    "call": "b.doubled()",
    "args": ["[2, 1]"],
}


def bag_predictions(seed: int) -> str:
    return json.dumps(
        {
            "predictions": [
                {
                    "input": "E-001",
                    "outcome": {"kind": "value", "text": "[1, 1, 2, 2]"},
                    "derivation": f"doubled then sorted, draw {seed}",
                    "decides": "returns the items repeated twice, in ascending order",
                }
            ],
            "references": [
                {"unit": "S-002", "source": "def ref(xs):\n    return sorted(xs * 2)\n"}
            ],
        }
    )


def bag_tree(root: Path, body: str | None, *, method: str = "doubled") -> Path:
    """A plain directory holding `bag.py`, with `doubled` (or `method`) returning `body`."""
    root.mkdir(parents=True)
    extra = DOUBLED.format(body=body).replace("doubled", method) if body else ""
    (root / "bag.py").write_text(BAG_BASE + extra)
    return root


@pytest.fixture
def bag_bad(tmp_path: Path) -> Path:
    return make_bag_bad(tmp_path)


def make_bag_bad(tmp_path: Path) -> Path:
    """The tree under audit: a git repo whose `doubled` skips re-sorting."""
    root = bag_tree(tmp_path / "audited", None)
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    (root / "bag.py").write_text(BAG_BASE + DOUBLED.format(body=UNSORTED))
    return root


def extracted(tmp_path: Path, probes: list[ProbeTree]) -> Path:
    from test_task_passes import Scripted

    from saddle.task_passes import extract

    client = Scripted({"inputs": [BAG_INPUT]}, predict=bag_predictions)
    path = tmp_path / "bag-requirements.json"
    path.write_text(json.dumps(extract(BAG, client, probes=probes)))
    return path


def good_probes(tmp_path: Path) -> list[ProbeTree]:
    return [
        ProbeTree(bag_tree(tmp_path / f"probe-{i}", body), "oracle-pass")
        for i, body in enumerate((SORTED, PAIRS, PLUS))
    ]


def test_r17_known_good_half_three_agreeing_probes_let_route_b_refuse(
    bag_bad: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = extracted(tmp_path, good_probes(tmp_path))
    record = json.loads(path.read_text())
    assert [p["source"] for p in record["probes"]] == ["oracle-pass"] * 3
    assert [p["status"] for p in record["examples"][0]["probes"]] == ["ran"] * 3
    monkeypatch.setattr(gates, "P1_REFUSAL_LICENSED", True)
    check = check_tree(bag_bad, "HEAD", path)
    assert check.verdict == "fail"
    assert "via executed-reference (3/3), probe (3/3)" in check.detail
    assert "expected [1, 1, 2, 2], got [1, 2, 1, 2]" in check.detail


def test_r17_i_with_no_probe_route_b_only_asks(
    bag_bad: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Also the product without a reference: `auto` extracts with no probe."""
    path = extracted(tmp_path, [])
    assert json.loads(path.read_text())["probes"] == []
    monkeypatch.setattr(gates, "P1_REFUSAL_LICENSED", True)
    check = check_tree(bag_bad, "HEAD", path)
    assert (check.verdict, check.passed) == ("question", True)
    assert check.detail.endswith("[no known-correct probe]")


def test_r17_ii_a_probe_that_returns_the_trees_value_rejects_the_example(
    bag_bad: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probes = good_probes(tmp_path)
    # the tree's reading, written differently (the same bytes would be the tree itself)
    wrong_body = "return list(self.items) * 2"
    probes[1] = ProbeTree(bag_tree(tmp_path / "wrong", wrong_body), "oracle-pass")
    path = extracted(tmp_path, probes)
    wrong = json.loads(path.read_text())["probes"][1]["sha256"]
    monkeypatch.setattr(gates, "P1_REFUSAL_LICENSED", True)
    check = check_tree(bag_bad, "HEAD", path)
    assert check.verdict == "question"  # nothing left to judge: never a refusal
    assert (
        f"not-judged E-001 (S-002): probe-rejected: known-correct probe {wrong[:12]} "
        "gives [1, 2, 1, 2]"
    ) in (check.basis or "")


def test_r17_iii_a_probe_that_raises_attribute_error_on_the_example_leaves_a_question(
    bag_bad: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probes = good_probes(tmp_path)
    probes[2] = ProbeTree(bag_tree(tmp_path / "renamed", SORTED, method="twice"), "oracle-pass")
    path = extracted(tmp_path, probes)
    status = json.loads(path.read_text())["examples"][0]["probes"][2]["status"]
    assert status.startswith("could not call: call: AttributeError")
    monkeypatch.setattr(gates, "P1_REFUSAL_LICENSED", True)
    check = check_tree(bag_bad, "HEAD", path)
    assert check.verdict == "question"
    assert check.detail.endswith("[no known-correct probe (a probe could not run on it)]")


def test_r17_fewer_bench_probes_than_needed_only_ask_but_one_user_reference_suffices(
    bag_bad: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gates, "P1_REFUSAL_LICENSED", True)
    two = extracted(tmp_path, good_probes(tmp_path)[:2])
    check = check_tree(bag_bad, "HEAD", two)
    assert check.verdict == "question"
    assert check.detail.endswith("[too few known-correct probes (2 of 3 oracle-pass, 0 of 1 user)]")
    user = [ProbeTree(bag_tree(tmp_path / "reference", SORTED), "user")]
    one = tmp_path / "user"
    one.mkdir()
    check = check_tree(bag_bad, "HEAD", extracted(one, user))
    assert check.verdict == "fail"
    assert "probe (1/1)" in check.detail


def test_the_tree_under_audit_is_never_its_own_probe(
    bag_bad: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probes = good_probes(tmp_path)
    probes[0] = ProbeTree(bag_bad, "oracle-pass")  # a probe that is the audited tree
    path = extracted(tmp_path, probes)
    monkeypatch.setattr(gates, "P1_REFUSAL_LICENSED", True)
    check = check_tree(bag_bad, "HEAD", path)
    assert (check.verdict, check.passed) == ("question", True)
    assert check.detail == f"P1 could not run: {task_requirements.PROBE_IS_TREE}"
    # the same file on a different tree is judged as usual
    (bag_bad / "bag.py").write_text(BAG_BASE + DOUBLED.format(body=SORTED))
    assert check_tree(bag_bad, "HEAD", path).verdict == "question"  # probe 0 now disagrees


def test_tree_sha256_is_the_source_not_the_leftovers(tmp_path: Path) -> None:
    root = bag_tree(tmp_path / "t", SORTED)
    first = tree_sha256(root)
    for leftover in (".git/HEAD", "__pycache__/bag.cpython.pyc", "notes.md", ".venv/x.py"):
        (root / leftover).parent.mkdir(parents=True, exist_ok=True)
        (root / leftover).write_text("x")
    assert tree_sha256(root) == first
    (root / "pkg").mkdir()
    (root / "pkg" / "setup.cfg").write_text("[x]\n")
    assert tree_sha256(root) != first
    moved = tree_sha256(root)
    (root / "pkg" / "setup.cfg").rename(root / "setup.cfg")
    assert tree_sha256(root) != moved  # the path counts, not only the content


@pytest.mark.parametrize(
    ("make", "why"),
    [
        (lambda t: [ProbeTree(t / "absent", "user")], "is not a directory"),
        (lambda t: [ProbeTree(t, "model")], "probe source 'model' is not one of"),
        (lambda t: [ProbeTree(t, "user"), ProbeTree(t, "user")], "the same tree as an earlier"),
    ],
)
def test_a_probe_that_cannot_be_one_stops_the_extraction_before_any_call(
    tmp_path: Path, make: Any, why: str
) -> None:
    from test_task_passes import Scripted

    from saddle.task_passes import extract

    client = Scripted({"inputs": [BAG_INPUT]}, predict=bag_predictions)
    with pytest.raises(RequirementsError, match=why):
        extract(BAG, client, probes=make(bag_tree(tmp_path / "t", SORTED)))
    assert client.sent == []


def _bag_file(tmp_path: Path) -> Path:
    return extracted(tmp_path, good_probes(tmp_path))


@pytest.mark.parametrize(
    ("change", "why"),
    [
        (lambda d: d["probes"].append(dict(d["probes"][0])), "sealed twice"),
        (lambda d: d["probes"][0].update(source="model"), "probe source 'model'"),
        (lambda d: d["examples"][0]["probes"].pop(), "one outcome per sealed probe"),
        (lambda d: d["probes"].pop(), "one outcome per sealed probe"),
    ],
)
def test_a_file_whose_probes_do_not_add_up_does_not_load(
    tmp_path: Path, change: Any, why: str
) -> None:
    path = _bag_file(tmp_path)

    def resealed(d: dict[str, Any]) -> None:
        change(d)
        _rehash(d)

    _edit(path, resealed)
    with pytest.raises(RequirementsError, match=why):
        load(path)


def test_probe_outcomes_are_sealed_as_what_they_did(tmp_path: Path) -> None:
    probe = ProbeTree(bag_tree(tmp_path / "p", SORTED), "oracle-pass")
    listed = task_requirements.probe_listing([probe])
    sha = listed[0]["sha256"]
    examples = [
        Example("E-1", ("S-002",), ("from bag import Bag",), "Bag(None).items[5]", ()),
        Example("E-2", ("S-002",), ("from bag import Bag",), "iter([])", ()),
        Example("E-3", ("S-002",), ("import os",), "1", ()),
    ]
    got = task_requirements.run_probes([probe], listed, examples)
    assert got["E-1"] == [
        {
            "sha256": sha,
            "status": "ran",
            "outcome": {"kind": "raises", "text": "IndexError"},
            "raises": ["IndexError", "LookupError", "Exception", "BaseException", "object"],
        }
    ]
    assert got["E-2"][0]["status"].startswith("opaque: ")
    assert got["E-3"][0]["status"] == "could not call: snippet refused: Import is not allowed"
    crashed = task_requirements.run_probes(
        [probe], listed, examples[:1], runner=_stub(CapturedRun((), 1, "", "boom"))
    )
    assert crashed["E-1"][0]["status"].startswith("crashed: the example driver crashed (exit 1)")
    assert task_requirements.run_probes([probe], listed, []) == {}


@pytest.mark.parametrize(
    ("outcome", "status"),
    [
        (None, "no result"),
        (TreeOutcome("hang", detail="no outcome within 5 s"), "HANG: no outcome within 5 s"),
        (TreeOutcome("raises", raises=()), "raises"),
        (TreeOutcome("value", value={"t": "weird"}), "not-canonical"),
    ],
)
def test_a_probe_outcome_that_is_no_outcome_is_sealed_as_why(
    outcome: TreeOutcome | None, status: str
) -> None:
    assert task_requirements._probe_record(outcome) == {"status": status}


def test_a_probe_raising_a_subclass_of_the_expected_raise_agrees() -> None:
    expected = Outcome.of("raises", "LookupError")
    sub = Probe(
        "a", "ran", Outcome.of("raises", "IndexError"), raises=("IndexError", "LookupError")
    )
    assert sub.agrees(expected)
    assert not Probe("a", "ran", Outcome.of("raises", "KeyError"), raises=("KeyError",)).agrees(
        expected
    )
    assert not Probe("a", "could not call").agrees(expected)
    assert Probe("a", "ran", Outcome.of("value", "[1]")).agrees(Outcome.of("value", "[1]"))
