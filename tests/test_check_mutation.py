"""`check` with `mutation: true` runs tier 2 as finish runs it (#180).

A watched dogfood run wrote its own mutation loops, over and over, to learn
before finish which mutants would survive: `check` ran tiers 0 and 1 only, and
finish was the first place tier 2 answered. With `mutation` set, check runs the
same auditor's tier 2 on the tree as it is now, so its survivors are the ones
finish names on that tree.

Known-bad (before): no check ran tier 2, whatever it asked for. Known-good: a
plain check still never runs tier 2; a real mutation check names the same
mutants, with the same verdicts, as the finish audit of a separate run on the
same tree; a `mutation` that is not a boolean runs nothing; a repeat of a
mutation check on an unchanged tree is refused, and a plain check of that tree
before it does not refuse it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
from test_check_tool import (
    CHECK,
    FINISH,
    REAL_TEST,
    Scripted,
    WholeSuiteAuditor,
    call,
    check_spans,
    edit,
    make_repo,
    outcome,
    repo,
    results,
    run,
)
from test_evidence import _without_stubbed_mutmut

from saddle.auto import AutoOptions, AutoResult, run_auto
from saddle.engine import CHECK_MUTATION_NOT_BOOL
from saddle.feed import CHECK_MUTATION, CHECK_UNCHANGED
from saddle.journal import attempt_sidecar_path
from saddle.packet import compile_packet
from saddle.tools import CHECK_TOOL
from saddle.vllm import VllmClient

__all__ = ["repo"]  # the fixture, shared with test_check_tool

MUTATION = call(CHECK_TOOL, "m", mutation=True)


def test_a_mutation_check_runs_tier_two_and_a_plain_check_never_does(repo: Path) -> None:
    client = Scripted([[edit("e", "a - b", "a + b")], [CHECK], [MUTATION], [MUTATION]])
    result, fake = run(repo, client)
    plain, mutated, repeat = results(result, CHECK_TOOL)
    # plain check, mutation check, (the repeat runs nothing), the finish audit
    assert fake.tiers() == [0, 1, 0, 1, 2, 0, 1, 2]
    assert "PASS]" in plain
    assert mutated.startswith("[audit check 2 ")
    assert repeat.startswith(CHECK_UNCHANGED + "2;")
    assert "; that mutation check saw these same files" in repeat
    first, second = check_spans(result)
    assert first.argv[-1] != CHECK_MUTATION
    assert second.argv[-1] == CHECK_MUTATION
    record = json.loads(attempt_sidecar_path(result.journal, second.span_id).read_text())
    assert [f["tier"] for f in record["findings"]] == [0, 1, 2]
    row = next(r for r in compile_packet(result.journal).rows if r.key == "check")
    assert row.text.startswith("The model checked 2 times (1 with mutation); last check:")
    assert "call check with mutation set to true" in client.system


def test_a_whole_suite_mutation_check_runs_both(repo: Path) -> None:
    fake = WholeSuiteAuditor()
    both = call(CHECK_TOOL, "wm", whole_suite=True, mutation=True)
    result, _ = run(repo, Scripted([[edit("e", "a - b", "a + b")], [both]]), auditor=fake)
    assert len(fake.whole) == 1
    assert fake.tiers()[:3] == [0, 1, 2]
    (span,) = check_spans(result)
    assert span.argv[-2:] == ["whole-suite", CHECK_MUTATION]


@pytest.mark.parametrize("value", ["yes", 1, None])
def test_a_mutation_that_is_not_a_boolean_runs_nothing(repo: Path, value: object) -> None:
    client = Scripted([[edit("e", "a - b", "a + b")], [call(CHECK_TOOL, "b", mutation=value)]])
    result, fake = run(repo, client)
    assert results(result, CHECK_TOOL) == [f"{CHECK_MUTATION_NOT_BOOL}, not {json.dumps(value)}"]
    assert fake.tiers() == [0, 1, 2]  # the finish audit's alone
    assert check_spans(result) == []


@pytest.mark.parametrize(("value", "tier2"), [("True", True), ("false", False)])
def test_mutation_written_as_a_string_is_the_boolean_it_names(
    repo: Path, value: str, tier2: bool
) -> None:
    client = Scripted([[edit("e", "a - b", "a + b")], [call(CHECK_TOOL, "m", mutation=value)]])
    result, fake = run(repo, client)
    (span,) = check_spans(result)
    assert (span.argv[-1] == CHECK_MUTATION) is tier2
    # the check's tiers come first, then the finish audit's 0, 1, 2
    assert fake.tiers()[:3] == ([0, 1, 2] if tier2 else [0, 1, 0])


# -- the real auditor: a mutation check and the finish audit name the same mutants --

REAL_BASE = "def f():\n    return 1\n"
LABELLED = 'def f():\n    label = "two"\n    return 2 if label else 3'
"""A change whose string and `else` arm no test can tell apart: mutants survive."""


class Recording(Scripted):
    """A Scripted client that keeps what it was last sent: the tool results the
    model read, whole (the ledger seals each one capped)."""

    sent: list[dict[str, Any]]

    def stream_chat(self, messages: Any, **kwargs: Any) -> Any:
        self.sent = [dict(m) for m in messages]
        return super().stream_chat(messages, **kwargs)

    def read(self, call_id: str) -> str:
        """The first result the model read for the call with `call_id`."""
        return next(str(m["content"]) for m in self.sent if m.get("tool_call_id") == call_id)


def _real(root: Path, rounds: list[list[Any]], run_id: str) -> tuple[AutoResult, Recording]:
    make_repo(root, {"n.py": REAL_BASE, "test_n.py": REAL_TEST})
    client = Recording([[edit("e", "def f():\n    return 1", LABELLED, path="n.py")], *rounds])
    # Cap 2: the model reads the first refusal before the repeat stops the run.
    options = AutoOptions(
        task="make f return 2", repo=root, run_id=run_id, check_tool=True, finish_refusal_cap=2
    )
    return run_auto(options, cast(VllmClient, client)), client


def _mutation_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.startswith("- mutation (tier 2)")]


def _tier2(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [f for f in findings if f["tier"] == 2]


def test_a_mutation_check_names_the_mutants_a_separate_finish_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_stubbed_mutmut(monkeypatch)  # the real engine: the stub decides no mutant here
    checked, seen = _real(tmp_path / "checked", [[MUTATION], [FINISH]], "checked")
    alone, unseen = _real(tmp_path / "alone", [[FINISH]], "alone")
    (span,) = check_spans(checked)
    check = json.loads(attempt_sidecar_path(checked.journal, span.span_id).read_text())
    finish = outcome(alone)["audit"]
    assert check["tree"] == finish["tree"]
    assert any(m["status"] == "survived" for m in check["mutant_detail"]), check
    assert check["mutant_detail"] == finish["mutant_detail"]
    assert _tier2(check["findings"]) == _tier2(finish["findings"])
    assert not check["passed"]
    # The model reads the survivors as the other run's refused finish names them.
    said = seen.read(MUTATION.id)
    assert said.startswith("[audit check 1 on tree ")
    assert _mutation_lines(said) == _mutation_lines(unseen.read(FINISH.id))
    assert all(m["name"] in said for m in check["mutant_detail"] if m["status"] == "survived")
