"""P1's extraction: the code-blind passes against a scripted client, and the
mini-references run for real in the sandbox.

No test calls a model: `Scripted` answers each pass from a script and
records every prompt, seed and temperature it was sent.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Final

import pytest

from saddle import task_passes
from saddle.evidence import CapturedRun
from saddle.task_examples import Outcome, classify
from saddle.task_passes import (
    EMPTY_BASELINE,
    HIDDEN,
    PREDICT_SEEDS,
    PROPOSE_SEED,
    RETRY_SEED_OFFSET,
    baseline_listing,
    baseline_sources,
    extract,
    perturbed,
    proposed,
    reply_json,
    run_references,
)
from saddle.task_requirements import load
from saddle.task_units import task_units

TASK = """# Box

`Box(items=None)` holds items in ascending order.

- `b *= n` repeats the items n times in place, keeping ascending order.
- `b.first()` raises `IndexError` when it is empty.
- Write your own tests as you go.
"""

IMUL = {
    "units": ["S-002"],
    "setup": ["from containers import Box", "b = Box([2, 1])", "b *= 2"],
    "call": "list(b)",
    "args": ["[2, 1]", "2"],
}
FIRST = {
    "units": ["S-003"],
    "setup": ["from containers import Box"],
    "call": "Box().first()",
    "args": [],
}
DECIDES = "repeats the items n times in place, keeping ascending order"
REF = "def ref(xs, n):\n    return sorted(xs * n)\n"


def predict_reply(seed: int, *, ref: str = REF, same: bool = False) -> str:
    return json.dumps(
        {
            "predictions": [
                {
                    "input": "E-001",
                    "outcome": {"kind": "value", "text": "[1, 1, 2, 2]"},
                    "derivation": "sorted" if same else f"sorted, draw {seed}",
                    "decides": DECIDES,
                },
                {
                    "input": "E-002",
                    "outcome": {"kind": "raises", "text": "IndexError"},
                    "args": ["[]"],
                    "decides": "raises `IndexError` when it is empty",
                },
            ],
            "references": [
                {"unit": "S-002", "source": ref},
                {"unit": "S-003", "source": "def ref(xs):\n    return xs[0]\n"},
            ],
        }
    )


class Scripted:
    """Answers P-a, P-b and P-c from a script; records what each call was sent."""

    def __init__(
        self,
        propose: dict[str, Any] | str,
        predict: Any = predict_reply,
        alternatives: dict[str, Any] | None = None,
    ) -> None:
        self.propose = propose if isinstance(propose, str) else json.dumps(propose)
        self.predict = predict
        self.alternatives = json.dumps(alternatives or {"alternatives": []})
        self.sent: list[tuple[str, str, int | None, float]] = []
        self.lock = threading.Lock()

    def complete(
        self,
        prompt: str,
        *,
        max_tokens: int = 0,
        temperature: float = 0.0,
        reasoning_effort: str = "",
        seed: int | None = None,
    ) -> str:
        name = (
            "P-a"
            if prompt.startswith("You write inputs")
            else "P-b"
            if prompt.startswith("You predict")
            else "P-c"
        )
        with self.lock:
            self.sent.append((name, prompt, seed, temperature))
        if name == "P-a":
            return self.propose
        if name == "P-b":
            got = self.predict(seed)
            if isinstance(got, Exception):
                raise got
            return str(got)
        return self.alternatives


PROPOSAL = {
    "inputs": [IMUL, FIRST, {"units": ["S-999"], "call": "x"}, "junk", {"units": ["S-002"]}],
    "not_executable": [
        {"unit": "S-004", "reason": "prose-only"},
        {"unit": "S-001", "reason": "too hard"},
    ],
}


def sealed(tmp_path: Path, client: Scripted, **kw: Any) -> Path:
    path = tmp_path / "task-requirements.json"
    path.write_text(json.dumps(extract(TASK, client, **kw)))
    return path


# -- the passes -------------------------------------------------------------------


def test_extraction_seals_every_pass_and_a_file_that_loads(tmp_path: Path) -> None:
    client = Scripted(
        PROPOSAL,
        alternatives={
            "alternatives": [
                {
                    "input": "E-001",
                    "outcome": {"kind": "value", "text": "[1, 2, 1, 2]"},
                    "words": "repeats the items n times",
                },
                {"input": "E-001", "outcome": {"kind": "value", "text": "[9]"}, "words": "nowhere"},
                {
                    "input": "E-001",
                    "outcome": {"kind": "value", "text": "[1, 1, 2, 2]"},
                    "words": "x",
                },
                {"input": "E-404", "outcome": {"kind": "value", "text": "1"}, "words": "x"},
                "junk",
            ]
        },
    )
    path = sealed(tmp_path, client, model="the-model")
    record = json.loads(path.read_text())
    req = load(path, TASK)
    assert [e.id for e in req.examples] == ["E-001", "E-002"]
    assert req.not_executable == (("S-004", "prose-only"),)
    assert req.unanswered == ("S-001",)  # an invalid reason is not a reason
    assert record["model"] == "the-model"
    assert record["probes"] == []
    assert [c["pass"] for c in record["calls"]] == ["P-a", "P-b", "P-b", "P-b", "P-c"]
    # the vacuity rule: P-b above temperature 0, with distinct seeds
    sent = [s for s in client.sent if s[0] == "P-b"]
    assert sorted(s[2] or 0 for s in sent) == sorted(PREDICT_SEEDS)
    assert all(s[3] > 0 for s in sent)
    imul = req.examples[0]
    assert [r.status for r in imul.references] == ["ran", "ran", "ran"]
    assert [a.outcome.text for a in imul.alternatives] == ["[1, 2, 1, 2]"]
    klass = classify(imul, req.units)
    assert (klass.route, klass.effective_k, klass.eligible) == ("executed-reference", 3, False)
    assert klass.note == "no known-correct probe"  # step 1 seals no probes
    first = req.examples[1]
    assert [r.outcome for r in first.references] == [Outcome("raises", "IndexError")] * 3
    # E-002 proposed no args: each reference ran on the args its predictor named,
    # sealed with it; E-001's own args left nothing to name
    sealed_refs = record["examples"]
    assert [r.get("args") for r in sealed_refs[1]["references"]] == [["[]"]] * 3
    assert [r.get("args") for r in sealed_refs[0]["references"]] == [None] * 3


def test_an_unparseable_prediction_reply_is_asked_again_on_a_fresh_seed(tmp_path: Path) -> None:
    def garbled(seed: int) -> str:
        return "the answer is [1, 1, 2, 2]" if seed == PREDICT_SEEDS[0] else predict_reply(seed)

    client = Scripted({"inputs": [IMUL]}, predict=garbled)
    path = sealed(tmp_path, client)
    record = json.loads(path.read_text())
    assert [c["pass"] for c in record["calls"]] == ["P-a", "P-b", "P-b", "P-b", "P-b", "P-c"]
    assert record["calls"][1]["raw"] == "the answer is [1, 1, 2, 2]"  # sealed, not dropped
    seeds = sorted(s[2] or 0 for s in client.sent if s[0] == "P-b")
    assert seeds == sorted([*PREDICT_SEEDS, PREDICT_SEEDS[0] + RETRY_SEED_OFFSET])
    req = load(path, TASK)
    assert all(p.outcome is not None for p in req.examples[0].predictions)


STATEFUL: Final = {
    "id": "E-001",
    "units": ["S-002"],
    "setup": ["g = Gauge(3)"],
    "call": "g.move(-4)",
    "args": [],
}
LEVEL: Final = (
    "def ref(level, step):\n    if level + step < 0:\n        raise ValueError(step)\n"
    "    return level + step\n"
)


def test_an_input_with_no_args_runs_on_the_args_its_predictor_names() -> None:
    def run(chosen: dict[str, list[str]]) -> dict[str, Any]:
        return run_references({"S-002": LEVEL}, [STATEFUL], {"E-001": None}, chosen=chosen)["E-001"]

    assert run({"E-001": ["3", "-4"]}) == {
        "status": "ran",
        "outcome": {"kind": "raises", "text": "ValueError"},
        "args": ["3", "-4"],
    }
    # args holding a value the input does not, such as an outcome, never reach it
    assert run({"E-001": ["3", "-1"]}) == {
        "status": "refused: its args: -1 holds -1, which the input does not"
    }
    assert run({})["status"].startswith("could not call: ")  # none named: as before
    # the proposal's args, when it gave some, are the ones used
    got = run_references({"S-002": REF}, [EXAMPLE], {"E-001": None}, chosen={"E-001": ["9"]})
    assert got["E-001"] == {"status": "ran", "outcome": {"kind": "value", "text": "[1, 1, 2, 2]"}}


def test_r8_identical_draws_are_one_sample(tmp_path: Path) -> None:
    client = Scripted({"inputs": [IMUL]}, predict=lambda seed: predict_reply(seed, same=True))
    req = load(sealed(tmp_path, client))
    klass = classify(req.examples[0], req.units)
    assert klass.effective_k == 1
    assert klass.route == "decided-unverified"


def test_a_failed_pass_decides_nothing_and_is_sealed(tmp_path: Path) -> None:
    def flaky(seed: int) -> Any:
        return RuntimeError("server down") if seed == PREDICT_SEEDS[1] else predict_reply(seed)

    client = Scripted({"inputs": [IMUL]}, predict=flaky)
    path = sealed(tmp_path, client)
    record = json.loads(path.read_text())
    assert record["calls"][2]["error"] == "RuntimeError: server down"
    req = load(path)
    assert classify(req.examples[0], req.units).route == "split"
    assert [c["pass"] for c in record["calls"]] == ["P-a", "P-b", "P-b", "P-b"]  # no P-c
    empty = Scripted("not json at all")
    nothing = load(sealed(tmp_path, empty))
    assert nothing.examples == ()
    # A whole reply with no JSON is asked once more, on a fresh seed.
    assert [s[0] for s in empty.sent] == ["P-a", "P-a"]
    assert [s[2] for s in empty.sent] == [PROPOSE_SEED, PROPOSE_SEED + RETRY_SEED_OFFSET]


def test_the_caps_cut_and_name_what_they_cut() -> None:
    units = task_units(TASK)
    many = {"inputs": [dict(FIRST) for _ in range(5)]}
    plan = proposed(many, units)
    assert len(plan.inputs) == 3
    assert plan.cut == ["S-003"]
    assert "S-003" not in plan.unanswered
    assert proposed(None, units).unanswered == ["S-001", "S-002", "S-003", "S-004"]


def test_the_total_example_cap_cuts_too(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(task_passes, "MAX_EXAMPLES", 1)
    plan = proposed({"inputs": [IMUL, FIRST]}, task_units(TASK))
    assert ([i["id"] for i in plan.inputs], plan.cut) == (["E-001"], ["S-003"])


def test_non_literal_args_are_dropped() -> None:
    plan = proposed({"inputs": [{**IMUL, "args": ["[2, 1]", "f(x)"]}]}, task_units(TASK))
    assert plan.inputs[0]["args"] == ["[2, 1]"]


# -- R15: D-2, the baseline the passes see ------------------------------------------

BASELINE = {
    "containers.py": (
        'class Box:\n    """Box keeps insertion order."""\n\n'
        '    def first(self):\n        """Oldest item."""\n\n'
        "    def _private(self):\n        pass\n\n"
        '    def __init__(self, items=None):\n        """Make one."""\n\n'
        'def helper(x):\n    """Helper doc line."""\n\n'
        "def plain():\n    pass\n\n"
        "def _hidden():\n    pass\n\nX = 1\n"
    ),
    "tests/test_containers.py": 'def test_x():\n    """Never shown."""\n',
    "broken.py": "def (:\n",
    "constants.py": "X = 1\n",
    "notes.txt": "x",
}


def test_r15_a_mentioned_name_shows_its_signature_and_the_hidden_line_only(
    tmp_path: Path,
) -> None:
    client = Scripted({"inputs": [IMUL]})
    record = json.loads(sealed(tmp_path, client, sources=BASELINE).read_text())
    for name, prompt, _, _ in client.sent:
        if name == "P-c":
            continue
        assert "class Box" in prompt
        assert HIDDEN in prompt
        assert "insertion order" not in prompt
        assert "Oldest item" not in prompt  # `first` is mentioned too
        assert "Helper doc line." in prompt  # not mentioned: kept
        assert "Never shown" not in prompt  # tests are never shown
    assert record["hidden_docstrings"] == ["containers.py:Box", "containers.py:Box.first"]


def test_r15_naming_the_module_hides_all_its_docstrings() -> None:
    listing, hidden = baseline_listing(BASELINE, "Edit `containers.py`.")
    assert "Helper doc line." not in listing
    assert "containers.py:helper" in hidden
    assert "    def __init__(self, items=None)" in listing
    assert baseline_listing({}, "x") == (EMPTY_BASELINE, [])


def test_baseline_sources_reads_the_tracked_python_files(tmp_path: Path) -> None:
    import subprocess

    repo = tmp_path / "r"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "a.py").write_text("A = 1\n")
    (repo / "b.txt").write_text("x")
    run = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*run[:3], "init", "-q"], check=True)
    subprocess.run([*run, "add", "-A"], check=True)
    subprocess.run([*run, "commit", "-q", "-m", "x"], check=True)
    assert baseline_sources(repo, "HEAD") == {"pkg/a.py": "A = 1\n"}
    assert baseline_sources(tmp_path / "nowhere", "HEAD") == {}


# -- R6-ref, R12-t, R12-p: the mini-references run for real -------------------------

EXAMPLE = {"id": "E-001", "units": ["S-002"], "args": ["[2, 1]", "2"]}


def refs(source: str | None, predicted: Outcome | None = None) -> dict[str, Any]:
    sources = {"S-002": source} if source is not None else {}
    return run_references(sources, [EXAMPLE], {"E-001": predicted})["E-001"]


def test_r6_ref_a_good_reference_runs_and_a_bad_one_never_does() -> None:
    assert refs(REF) == {"status": "ran", "outcome": {"kind": "value", "text": "[1, 1, 2, 2]"}}
    assert refs("import os\n\ndef ref(xs, n):\n    return 1\n") == {
        "status": "refused: import of os is not allowed"
    }
    assert refs(None) == {"status": "missing"}


@pytest.mark.parametrize(
    ("source", "sealed_as"),
    [
        ("def ref(xs, n):\n    raise KeyError(n)\n", {"kind": "raises", "text": "KeyError"}),
        ("import itertools\n\ndef ref(xs, n):\n    return itertools.count(n)\n", "not-canonical"),
        ("def ref(xs, n):\n    return 1\n\nref = ref(1)\n", "must be imports"),
        ("def ref(xs, n, extra=[][0]):\n    return n + 1\n", "raised IndexError while defining it"),
        (
            "def ref(xs, n):\n    return n * float('nan')\n",
            {"kind": "value", "text": 'float("nan")'},
        ),
    ],
)
def test_references_that_raise_or_return_oddities_are_sealed_as_what_they_did(
    source: str, sealed_as: Any
) -> None:
    got = refs(source)
    if isinstance(sealed_as, dict):
        assert got == {"status": "ran", "outcome": sealed_as}
    else:
        assert sealed_as in got["status"]


@pytest.mark.parametrize(
    ("source", "sealed_as"),
    [
        # The input gives two args. A signature that cannot take them never ran:
        # the shape a stateful example produced, `args: []` against `ref(qty, delta)`.
        ("def ref(qty, delta, extra):\n    return qty + delta + extra\n", "could not call"),
        ("def ref(xs, *, n):\n    return len(xs) + n\n", "could not call"),
        # A TypeError the body raises on args it did take is behaviour, as before.
        ("def ref(xs, n):\n    return len(n)\n", {"kind": "raises", "text": "TypeError"}),
        ("def ref(xs, n):\n    raise TypeError(n)\n", {"kind": "raises", "text": "TypeError"}),
        ("def ref(*args):\n    return len(args)\n", {"kind": "value", "text": "2"}),
    ],
)
def test_a_reference_that_cannot_take_the_args_is_no_outcome(source: str, sealed_as: Any) -> None:
    got = refs(source)
    if isinstance(sealed_as, dict):
        assert got == {"status": "ran", "outcome": sealed_as}
    else:
        assert got["status"].startswith(f"{sealed_as}: ")
        assert "outcome" not in got


def test_r12_t_a_reference_that_hangs_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(task_passes, "REFERENCE_CALL_TIMEOUT_S", 0.3)
    assert refs("def ref(xs, n):\n    while xs:\n        pass\n")["status"] == "timeout"
    at_definition = "def ref(xs, n, _=max(x for x in iter(int, 1))):\n    return n + 1\n"
    assert refs(at_definition)["status"] == "timeout"
    ok = "def ok(inp, out):\n    while True:\n        pass\n"
    assert refs(ok, Outcome.of("value", "[1]"))["status"] == "timeout"


def test_r12_p_an_ok_predicate_must_discriminate() -> None:
    predicted = Outcome.of("value", "[1, 1, 2, 2]")
    always = "def ok(inp, out):\n    return True\n"
    sharp = "def ok(inp, out):\n    return out == sorted(inp[0] * inp[1])\n"
    raising = "def ok(inp, out):\n    return out[99]\n"
    assert refs(always, predicted) == {"status": "ran", "form": "ok", "accepts": False}
    assert refs(sharp, predicted) == {"status": "ran", "form": "ok", "accepts": True}
    assert refs(raising, predicted) == {"status": "raised IndexError", "form": "ok"}
    assert refs(always, Outcome.of("raises", "KeyError")) == {
        "status": "not-discriminating",
        "form": "ok",
    }
    assert refs(always, None)["status"] == "not-discriminating"


def test_a_reference_driver_that_dies_is_sealed_as_such() -> None:
    def dead(argv: list[str], cwd: Path, **kw: Any) -> CapturedRun:
        return CapturedRun(tuple(argv), 1, "", "boom", timed_out=False)

    def slow(argv: list[str], cwd: Path, **kw: Any) -> CapturedRun:
        return CapturedRun(tuple(argv), 124, "", "", timed_out=True)

    assert run_references({"S-002": REF}, [EXAMPLE], {}, runner=dead)["E-001"] == {
        "status": "crashed (exit 1)"
    }
    assert run_references({"S-002": REF}, [EXAMPLE], {}, runner=slow)["E-001"] == {
        "status": "timeout"
    }


@pytest.mark.parametrize(
    ("text", "other"),
    [
        ("True", "False"),
        ("2", "3"),
        ("1.5", "2.5"),
        ("[1, 2]", "[1]"),
        ("[]", "[0]"),
        ("'a'", "'ax'"),
    ],
)
def test_perturbed_changes_the_value_mechanically(text: str, other: str) -> None:
    assert perturbed(Outcome.of("value", text)) == other


def test_perturbed_has_nothing_for_other_shapes() -> None:
    assert perturbed(Outcome.of("value", "{1: 2}")) is None
    assert perturbed(Outcome.of("raises", "KeyError")) is None


def test_reply_json_finds_the_object_or_nothing() -> None:
    assert reply_json('noise {"a": 1} noise') == {"a": 1}
    assert reply_json("no braces") is None
    assert reply_json("{broken") is None
    assert reply_json("{broken}") is None
    assert reply_json("} {") is None


def test_a_driver_result_that_is_not_a_record_is_sealed_as_such() -> None:
    def writes(payload: dict[str, Any]) -> Any:
        def runner(argv: list[str], cwd: Path, **kw: Any) -> CapturedRun:
            Path(argv[-1]).write_text(json.dumps(payload))
            return CapturedRun(tuple(argv), 0, "", "")

        return runner

    garbage = {"E-001": {"status": "ran", "value": {"t": "blob"}}}
    assert run_references({"S-002": REF}, [EXAMPLE], {}, runner=writes(garbage))["E-001"] == {
        "status": "not-canonical"
    }
    assert run_references({"S-002": REF}, [EXAMPLE], {}, runner=writes({"E-001": 5}))["E-001"] == {
        "status": "no result",
        "form": "ref",
    }


def test_an_unparseable_prediction_decides_nothing(tmp_path: Path) -> None:
    def bad(seed: int) -> str:
        return json.dumps(
            {"predictions": [{"input": "E-001", "outcome": {"kind": "value", "text": "f("}}]}
        )

    req = load(sealed(tmp_path, Scripted({"inputs": [IMUL]}, predict=bad)))
    assert [p.outcome for p in req.examples[0].predictions] == [None, None, None]
    assert [r.status for r in req.examples[0].references] == ["missing"] * 3
