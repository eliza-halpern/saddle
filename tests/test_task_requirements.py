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
    RequirementsError,
    check_tree,
    load,
    run_examples,
    seal,
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
    record = {"task_text": TASK, "examples": list(examples), **extra}
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
