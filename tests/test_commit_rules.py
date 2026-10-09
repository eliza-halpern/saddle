"""`[tool.saddle] commit-rules`: a run's commit follows the project's own commit rules.

CONTRIBUTING.md asks every commit for the change's contract as one sentence, the
direction of any contract change, and the mutants with their verdicts. `saddle auto` left
a message of the shape `saddle auto <id>: <outcome> (<reason>)` plus the model's
narrative, so a run's commit on saddle's own tree had to be rewritten by hand before it
could merge.

The contract, each half both ways: a project states its rules with `commit-rules = true`
in the `[tool.saddle]` table of the `pyproject.toml` committed at the commit the run
starts from, read like the table's other keys. With it, the run's commit carries
`auto.commit_block`: the contract and the direction the model states at `finish`, written
as it states them, and one `- ` line per mutant of the run's mutation record, in that
record's order, with the status the record gives -- never with a mutant the model claims
and no audit scored. A field the run cannot determine is `undetermined`, never a guess.
Without the key the message is exactly what it has always been.

Known-good: a tree with `commit-rules = true` commits the contract, the direction and the
recorded mutants and their statuses. Known-bad: a mutant the model names that the record
does not hold is not in the Mutants block; a direction outside the project's three is
`undetermined`; a value that is not `true` or `false` is refused by name; a key the tree
holds only in its working tree, or a typo of it, does not state rules for the run.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, cast

import pytest
from test_auto import Scripted, call, git

from saddle.anchor import COAUTHOR_TRAILER, LEDGER_TRAILER, OUTCOME_TRAILER
from saddle.auditor import Finding, Findings
from saddle.auto import RULES_UNREAD, AutoOptions, AutoResult, commit_block, run_auto
from saddle.evidence import SuiteLimitError, commit_rules
from saddle.feed import Arm, AuditFeed, AuditResult
from saddle.journal import attempt_sidecar_path, read_spans
from saddle.vllm import VllmClient

CONTRACT_SENTENCE = "A gate that cannot vary its count proves nothing, so the count must vary."

# ---------------------------------------------------------------------------
# `evidence.commit_rules`: the key, read where that table's other keys are read.
# ---------------------------------------------------------------------------


def _commit(root: Path, files: dict[str, str]) -> str:
    """A git tree holding `files`, committed; the commit's sha."""
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return git(root, "rev-parse", "HEAD").strip()


def _saddle_table(root: Path, written: str) -> str:
    return _commit(root, {"pyproject.toml": f'[project]\nname = "p"\n\n[tool.saddle]\n{written}\n'})


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        ({"pyproject.toml": '[project]\nname = "p"\n\n[tool.saddle]\ncommit-rules = true\n'}, True),
        (
            {"pyproject.toml": '[project]\nname = "p"\n\n[tool.saddle]\ncommit-rules = false\n'},
            False,
        ),
        ({"pyproject.toml": '[project]\nname = "p"\n\n[tool.saddle]\ntest-timeout = 60\n'}, False),
        ({"pyproject.toml": '[project]\nname = "p"\n'}, False),
        ({"other.txt": "no config here\n"}, False),
    ],
    ids=["true", "false", "table-without-the-key", "no-table", "no-file"],
)
def test_the_key_is_read_as_the_project_states_it(
    tmp_path: Path, files: dict[str, str], expected: bool
) -> None:
    _commit(tmp_path, files)
    assert commit_rules(tmp_path, "HEAD") is expected


def test_a_key_only_the_working_tree_has_is_not_the_project_s(tmp_path: Path) -> None:
    """Known-bad for "read like the table's other keys": a tree that gives itself rules
    after the commit does not have them for a run or audit that starts from that commit."""
    _commit(tmp_path, {"pyproject.toml": '[project]\nname = "p"\n'})
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "p"\n\n[tool.saddle]\ncommit-rules = true\n', encoding="utf-8"
    )
    assert commit_rules(tmp_path, "HEAD") is False


@pytest.mark.parametrize("written", ['"true"', "'false'", "1", "1.0", "[true]"])
def test_a_value_that_is_not_a_boolean_is_refused_by_name(tmp_path: Path, written: str) -> None:
    """TOML's `true` or `false` is the only way to state the rules. A quoted value, a
    number and a list are refused, naming the commit and the value, rather than read as
    "no rules"; a bare word is refused with the file, pinned in test_suite_limit.py."""
    sha = _saddle_table(tmp_path, f"commit-rules = {written}")
    with pytest.raises(SuiteLimitError) as caught:
        commit_rules(tmp_path, "HEAD")
    message = str(caught.value)
    assert message.startswith("cannot read the commit rules: ")
    assert f"pyproject.toml at {sha[:12]}: commit-rules = " in message
    assert "is not `true` or `false`" in message


@pytest.mark.parametrize("key", ["commit_rules", "commit-rules-on", "commitrules", "commit"])
def test_a_typo_of_the_key_is_refused_and_never_reads_as_unset(tmp_path: Path, key: str) -> None:
    _saddle_table(tmp_path, f"{key} = true")
    with pytest.raises(SuiteLimitError) as caught:
        commit_rules(tmp_path, "HEAD")
    message = str(caught.value)
    assert f"[tool.saddle] holds {key};" in message
    assert "the keys saddle reads there are " in message


@pytest.mark.parametrize(
    "files",
    [
        {"pyproject.toml": "[tool]\nsaddle = 5\n"},
        {"pyproject.toml": "[tool.saddle\ncommit-rules = true\n"},
        {"pyproject.toml/inner.txt": "x\n"},
    ],
    ids=["not-a-table", "not-toml", "a-directory"],
)
def test_a_table_that_cannot_be_read_is_refused(tmp_path: Path, files: dict[str, str]) -> None:
    _commit(tmp_path, files)
    with pytest.raises(SuiteLimitError) as caught:
        commit_rules(tmp_path, "HEAD")
    assert str(caught.value).startswith("cannot read the commit rules: ")


# ---------------------------------------------------------------------------
# `auto.commit_block`: the three fields, from the record and from the model.
# ---------------------------------------------------------------------------

RECORDED: tuple[tuple[str, str], ...] = (
    ("calc.py:2:sum_returns_difference", "survived"),
    ("calc.py:2:sum_returns_zero", "killed"),
    ("calc.py:5:guard_inverted", "timed out"),
)

CONTRACT_LABEL = "Contract (as the model states it): "
DIRECTION_LABEL = "Direction (as the model states it): "
MUTANTS_LABEL = "Mutants (from the run's mutation record):"


def test_the_block_names_the_contract_the_direction_and_the_recorded_mutants() -> None:
    """Known-good, the whole block: the two fields the model states, then the mutants in
    the record's own order with the record's own statuses."""
    assert commit_block(CONTRACT_SENTENCE, "tightened", RECORDED).splitlines() == [
        f"{CONTRACT_LABEL}{CONTRACT_SENTENCE}",
        f"{DIRECTION_LABEL}tightened",
        MUTANTS_LABEL,
        "- calc.py:2:sum_returns_difference: survived",
        "- calc.py:2:sum_returns_zero: killed",
        "- calc.py:5:guard_inverted: timed out",
    ]


@pytest.mark.parametrize("direction", ["tightened", "loosened", "scope narrowed"])
def test_each_of_the_project_s_three_directions_is_written_as_the_model_states_it(
    direction: str,
) -> None:
    assert commit_block(CONTRACT_SENTENCE, direction, RECORDED).splitlines()[1] == (
        f"{DIRECTION_LABEL}{direction}"
    )


@pytest.mark.parametrize(
    "direction", ["", "   ", "tighten", "Tightened", "tightened the gate", "widened", "none"]
)
def test_a_direction_that_is_not_one_of_the_three_is_undetermined(direction: str) -> None:
    """Known-bad: a label the project does not use is not a direction. Guessing which of
    its three the model meant would put a claim about the contract in the commit the
    model never made."""
    assert commit_block(CONTRACT_SENTENCE, direction, RECORDED).splitlines()[1] == (
        f"{DIRECTION_LABEL}undetermined"
    )


def test_a_missing_contract_is_undetermined_and_a_stated_one_is_kept_as_said() -> None:
    assert commit_block("", "loosened", RECORDED).splitlines()[0] == f"{CONTRACT_LABEL}undetermined"
    assert commit_block("   ", "loosened", RECORDED).splitlines()[0] == (
        f"{CONTRACT_LABEL}undetermined"
    )
    said = "The block  is built from the record, not from the run's account."
    assert (
        commit_block(f"  {said}  ", "loosened", RECORDED).splitlines()[0]
        == f"{CONTRACT_LABEL}{said}"
    )


def test_a_run_with_no_mutation_record_says_none_recorded() -> None:
    """Arm E has no auditor, so no mutation record exists: the block is there -- the
    project stated its rules -- and it says the record holds nothing."""
    assert commit_block(CONTRACT_SENTENCE, "tightened", ()).splitlines() == [
        f"{CONTRACT_LABEL}{CONTRACT_SENTENCE}",
        f"{DIRECTION_LABEL}tightened",
        MUTANTS_LABEL,
        "- none recorded",
    ]


def test_the_block_is_the_record_and_not_the_model_s_account_of_it() -> None:
    """The model's contract sentence claims two mutants the record does not hold; the
    Mutants block is the record's three, with the record's statuses. The claim stays in
    the Contract line, as the model's statement of what it did."""
    claim = "I killed every mutant, calc.py:9:sum_hides_the_bug and gates.py:1:bar included."
    block = commit_block(claim, "tightened", RECORDED)
    listed = [line for line in block.splitlines() if line.startswith("- ")]
    assert listed == [f"- {name}: {status}" for name, status in RECORDED]
    assert claim in block
    mutants = "\n".join(listed)
    assert "calc.py:9:sum_hides_the_bug" not in mutants
    assert "gates.py:1:bar" not in mutants


# ---------------------------------------------------------------------------
# `feed.AuditFeed.committed_mutants`: the record an audit made.
# ---------------------------------------------------------------------------


def _scored(point: str, mutants: tuple[tuple[str, str, str, tuple[str, ...]], ...]) -> AuditResult:
    found = Finding("mutation", 2, "pass", "code-wrong", f"{len(mutants)} scored", ())
    return AuditResult(point=point, tree="t", findings=(found,), mutant_detail=mutants)


def _feed(tmp_path: Path) -> AuditFeed:
    return AuditFeed(
        worktree=tmp_path,
        baseline="b",
        journal=tmp_path / "j.jsonl",
        run_span="s",
        factory=lambda *args: RecordsTwoMutants(),
    )


def test_the_mutants_come_from_the_last_audit_that_scored_any(tmp_path: Path) -> None:
    """The record is the last audit's; a later audit that scored nothing -- one refused at
    tier 0, a tree with no mutants, no audit at all -- cannot erase or invent one."""
    checkpoint = _scored(
        "checkpoint 1", (("old.py:1:x", "killed", "show", ("tests/test_old.py::test_old",)),)
    )
    later = _scored(
        "checkpoint 2",
        (
            ("n.py:1:one_off", "killed", "show", ("tests/test_n.py::test_n",)),
            ("n.py:2:gate", "survived", "show", ()),
        ),
    )
    scored = (("n.py:1:one_off", "killed"), ("n.py:2:gate", "survived"))
    for results, expected in (
        ([later], scored),
        ([checkpoint, later], scored),
        ([later, _scored("finish", ())], scored),
        ([later, AuditResult(point="finish", tree="", findings=())], scored),
        ([_scored("finish", ())], ()),
        ([AuditResult(point="finish", tree="", findings=())], ()),
        ([], ()),
    ):
        feed = _feed(tmp_path)
        feed.results = list(results)
        assert feed.committed_mutants() == expected


def test_an_audit_that_ran_no_mutation_carries_no_record_of_its_own(tmp_path: Path) -> None:
    """A blocked finish audit, or a tier-0 refusal that never reached tier 2, brings no
    record with it: it can neither invent mutants nor erase one an audit did score -- an
    audit that scored none is not evidence that the run has no mutants."""
    blocked = AuditResult(point="finish", tree="", findings=())
    refused = AuditResult(point="finish", tree="", findings=(), note="tiers 1 and 2 were not run")
    checkpoint = _scored("checkpoint 1", (("n.py:1:x", "survived", "show", ()),))
    for last in (blocked, refused, _scored("finish", ())):
        feed = _feed(tmp_path)
        feed.results = [checkpoint, last]
        assert feed.committed_mutants() == (("n.py:1:x", "survived"),)
    for only in (blocked, refused):
        feed = _feed(tmp_path)
        feed.results = [only]
        assert feed.committed_mutants() == ()


# ---------------------------------------------------------------------------
# The run's commit message, end to end.
# ---------------------------------------------------------------------------

BUGGY = "def add(a, b):\n    return a - b\n"
TEST = "from calc import add\n\n\ndef test_add():\n    assert add(2, 2) == 4\n"
EDIT_CALC = call("edit_file", "e1", path="calc.py", old="a - b", new="a + b")
AUDITED_MUTANTS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("calc.py:2:sum_returns_difference", "killed", "- return a + b\n+ return a - b", ()),
    ("calc.py:2:sum_returns_zero", "survived", "- return a + b\n+ return 0", ()),
)


class RecordsTwoMutants:
    """An auditor whose every tier passes and whose tier 2 scored these two mutants."""

    def tier0(self, path: str, new_text: str) -> Findings:
        return Findings(
            tier=0, key="k0", findings=(Finding("ruff", 0, "pass", "code-wrong", "clean", ()),)
        )

    def tier1(self, tree: Path | None = None) -> Findings:
        return Findings(
            tier=1, key="k1", findings=(Finding("tests", 1, "pass", "code-wrong", "1 passed", ()),)
        )

    def tier2(self, tree: Path | None = None) -> Findings:
        return Findings(
            tier=2,
            key="k2",
            findings=(Finding("mutation", 2, "pass", "code-wrong", "2 scored", ()),),
            mutant_detail=AUDITED_MUTANTS,
        )


@pytest.fixture
def calc_repo(tmp_path: Path) -> Path:
    """A one-file project whose `add` subtracts, with the test that says so."""
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "calc.py").write_text(BUGGY, encoding="utf-8")
    (root / "tests" / "test_calc.py").write_text(TEST, encoding="utf-8")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


def _state_rules(root: Path, stated: str) -> None:
    """Commit what the project states in its `[tool.saddle]`: `stated` is the line the
    table holds -- `commit-rules = true`, a typo of it -- or "none" for no table at all."""
    body = '[project]\nname = "p"\n'
    if stated != "none":
        body += f"\n[tool.saddle]\n{stated}\n"
    (root / "pyproject.toml").write_text(body, encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "the project states its commit rules")


def _finish_with(arguments: dict[str, Any]) -> Any:
    return call("finish", "f1", **arguments)


def _run(root: Path, arm: Arm, arguments: dict[str, Any], run_id: str = "r1") -> AutoResult:
    client = Scripted([[EDIT_CALC], [_finish_with(arguments)]])
    options = AutoOptions(
        task="make add add", repo=root, run_id=run_id, arm=arm, auditor_factory=_fake_auditor
    )
    return run_auto(options, cast(VllmClient, client))


def _fake_auditor(*args: object) -> RecordsTwoMutants:
    return RecordsTwoMutants()


def _message(result: AutoResult) -> str:
    return git(result.worktree, "log", "-1", "--format=%B", result.branch).strip()


def _said(message: str, label: str) -> str:
    """What the run's commit says under `label`: the label appears exactly once, and what
    follows it on that line is what this test reads back."""
    lines = [line for line in message.splitlines() if line.startswith(label)]
    assert len(lines) == 1, (
        f"{label!r} headlines {len(lines)} lines of the commit message:\n{message}"
    )
    return lines[0][len(label) :]


def _trailers(message: str | list[str]) -> list[str]:
    """The trailer lines the run's commit ends with: `anchor.anchor_trailers` writes them
    and this change adds nothing beside them."""
    lines = message.splitlines() if isinstance(message, str) else message
    return [line for line in lines if line.startswith(("Saddle-", "Co-Authored-By"))]


def _mutant_lines(message: str) -> list[str]:
    lines = message.splitlines()
    start = lines.index(MUTANTS_LABEL)
    out: list[str] = []
    for line in lines[start + 1 :]:
        if not line.startswith("- "):
            break
        out.append(line)
    return out


def test_a_run_on_a_tree_that_states_its_rules_commits_the_block(calc_repo: Path) -> None:
    """Done-when 1: the contract, the direction, and the recorded mutants with the
    statuses their record gave, in the record's order -- beside the subject line, the
    narrative and the anchor trailers, which stay as they are."""
    _state_rules(calc_repo, "commit-rules = true")
    result = _run(
        calc_repo,
        "E+A",
        {"summary": "fixed add", "contract": CONTRACT_SENTENCE, "direction": "tightened"},
    )
    assert (result.outcome, result.reason) == ("finished", "finish called")
    message = _message(result)
    assert message.splitlines()[0] == "saddle auto r1: finished (finish called)"
    assert _said(message, CONTRACT_LABEL) == CONTRACT_SENTENCE
    assert _said(message, DIRECTION_LABEL) == "tightened"
    assert _mutant_lines(message) == [
        "- calc.py:2:sum_returns_difference: killed",
        "- calc.py:2:sum_returns_zero: survived",
    ]
    assert message.index(CONTRACT_LABEL) < message.index("Narrative (model-written")
    assert "Narrative (model-written, not evidence):\nfixed add" in message
    assert _trailers(message) == [
        line for line in message.splitlines() if line.startswith(("Saddle-", "Co-Authored-By"))
    ]
    assert len(_trailers(message)) == 3
    assert re.fullmatch(OUTCOME_TRAILER + r": [0-9a-f]{64}", _trailers(message)[0])
    assert _trailers(message)[1] == f"{LEDGER_TRAILER}: .saddle/runs/r1/proofs.jsonl"
    assert _trailers(message)[2] == COAUTHOR_TRAILER


def test_a_model_claimed_mutant_that_no_audit_scored_is_not_in_the_block(calc_repo: Path) -> None:
    """Done-when 2: the model claims a mutant in its summary and in its contract. Both say
    so as the model's account; the Mutants block holds only the two the record scored."""
    _state_rules(calc_repo, "commit-rules = true")
    claimed = "calc.py:77:sum_hides_the_bug"
    summary = f"fixed add, which killed {claimed}"
    contract = f"add adds now; {claimed} and gates.py:1:bar were the only mutants."
    result = _run(
        calc_repo,
        "E+A",
        {"summary": summary, "contract": contract, "direction": "loosened"},
    )
    message = _message(result)
    assert _said(message, CONTRACT_LABEL) == contract
    assert _said(message, DIRECTION_LABEL) == "loosened"
    assert _mutant_lines(message) == [
        "- calc.py:2:sum_returns_difference: killed",
        "- calc.py:2:sum_returns_zero: survived",
    ]
    assert claimed in message  # as the model's statement, where it belongs ...
    assert claimed not in "\n".join(_mutant_lines(message))  # ... never as a recorded mutant


@pytest.mark.parametrize(
    ("arguments", "contract", "direction"),
    [
        ({"summary": "fixed add"}, "undetermined", "undetermined"),
        ({"summary": "fixed add", "contract": CONTRACT_SENTENCE}, "stated", "undetermined"),
        ({"summary": "fixed add", "direction": "widened"}, "undetermined", "undetermined"),
        ({"summary": "fixed add", "direction": "tightened "}, "undetermined", "tightened"),
        ({"summary": "fixed add", "direction": True}, "undetermined", "undetermined"),
        (
            {"summary": "fixed add", "contract": 7, "direction": "scope narrowed"},
            "undetermined",
            "scope narrowed",
        ),
    ],
    ids=[
        "neither",
        "contract-only",
        "unknown-direction",
        "direction-with-space",
        "direction-not-a-string",
        "contract-not-a-string",
    ],
)
def test_a_field_the_run_cannot_determine_says_undetermined(
    calc_repo: Path, arguments: dict[str, Any], contract: str, direction: str
) -> None:
    """Nothing here is guessed: a missing field, a value that is not a string, and a
    direction outside the project's three all read as undetermined. Arm E has no auditor,
    so its mutation record is nothing, and the block says so."""
    _state_rules(calc_repo, "commit-rules = true")
    result = _run(calc_repo, "E", arguments)
    assert (result.outcome, result.reason) == ("finished", "finish called")
    message = _message(result)
    assert _said(message, CONTRACT_LABEL) == (
        CONTRACT_SENTENCE if contract == "stated" else "undetermined"
    )
    assert _said(message, DIRECTION_LABEL) == direction
    assert _mutant_lines(message) == ["- none recorded"]


@pytest.mark.parametrize("written", ["none", "false"], ids=["no-key", "explicit-false"])
def test_a_tree_without_the_key_commits_the_message_it_always_committed(
    calc_repo: Path, written: str
) -> None:
    """Done-when 3: with the key absent, or stated false, a model that supplies a contract
    and a direction still leaves a message of the shape it has always had."""
    _state_rules(calc_repo, written)
    result = _run(
        calc_repo,
        "E",
        {"summary": "fixed add", "contract": CONTRACT_SENTENCE, "direction": "tightened"},
    )
    message = _message(result)
    lines = message.splitlines()
    assert lines[:4] == [
        "saddle auto r1: finished (finish called)",
        "",
        "Narrative (model-written, not evidence):",
        "fixed add",
    ]
    assert CONTRACT_LABEL not in message
    assert DIRECTION_LABEL not in message
    assert MUTANTS_LABEL not in message
    assert len(_trailers(lines)) == 3
    assert re.fullmatch(OUTCOME_TRAILER + r": [0-9a-f]{64}", _trailers(lines)[0])
    assert _trailers(lines)[1] == f"{LEDGER_TRAILER}: .saddle/runs/r1/proofs.jsonl"
    assert _trailers(lines)[2] == COAUTHOR_TRAILER


def test_a_key_the_run_wrote_into_its_own_worktree_states_nothing_for_it(calc_repo: Path) -> None:
    """Known-bad for "read from the commit the run starts from": a model that writes
    `commit-rules = true` into its worktree mid-run gets the rules of its starting commit,
    which are none, and commits today's message."""
    _state_rules(calc_repo, "none")
    wrote_key = call(
        "edit_file",
        "w1",
        path="pyproject.toml",
        old='name = "p"',
        new='name = "p"\n\n[tool.saddle]\ncommit-rules = true',
    )
    client = Scripted(
        [
            [wrote_key],
            [
                _finish_with(
                    {
                        "summary": "fixed add",
                        "contract": CONTRACT_SENTENCE,
                        "direction": "tightened",
                    }
                )
            ],
        ]
    )
    result = run_auto(
        AutoOptions(task="make add add", repo=calc_repo, run_id="r1", arm="E"),
        cast(VllmClient, client),
    )
    assert (result.outcome, result.reason) == ("finished", "finish called")
    message = _message(result)
    assert "commit-rules = true" in (result.worktree / "pyproject.toml").read_text(encoding="utf-8")
    assert CONTRACT_LABEL not in message
    assert MUTANTS_LABEL not in message


def test_a_project_whose_starting_commit_states_its_rules_unreadably_commits_todays_message(
    calc_repo: Path,
) -> None:
    """Known-bad, and the direction of the reading: a `[tool.saddle]` the run cannot read
    -- here a typo of the key, which `SuiteLimitError` refuses -- adds no block and stops
    no run. The block only ever says what the project asked for; the unreadable file is
    the audit's finding to report, pinned in test_suite_limit.py."""
    _state_rules(calc_repo, "commit_rules = true")
    result = _run(
        calc_repo,
        "E+A",
        {"summary": "fixed add", "contract": CONTRACT_SENTENCE, "direction": "tightened"},
    )
    assert (result.outcome, result.reason) == ("finished", "finish called")
    message = _message(result)
    assert CONTRACT_LABEL not in message
    assert DIRECTION_LABEL not in message
    assert MUTANTS_LABEL not in message
    assert message.splitlines()[2:4] == ["Narrative (model-written, not evidence):", "fixed add"]


def test_the_ledger_carries_the_block_s_values(calc_repo: Path) -> None:
    """The sealed outcome holds the contract, the direction and the recorded mutants the
    commit block is built from, so the person can compare the message with what the run
    stated and what its audit scored."""
    _state_rules(calc_repo, "commit-rules = true")
    result = _run(
        calc_repo,
        "E+A",
        {"summary": "fixed add", "contract": CONTRACT_SENTENCE, "direction": "scope narrowed"},
    )
    span = [s for s in read_spans(result.journal) if s.name.startswith("auto:")][-1]
    evidence = json.loads(attempt_sidecar_path(result.journal, span.span_id).read_text())
    assert evidence["commit_contract"] == CONTRACT_SENTENCE
    assert evidence["commit_direction"] == "scope narrowed"
    recorded = [(name, status) for name, status, _, _ in AUDITED_MUTANTS]
    assert evidence["commit_mutants"] == [
        {"name": name, "status": status} for name, status in recorded
    ]
    assert _mutant_lines(_message(result)) == [f"- {name}: {status}" for name, status in recorded]
    arm_e = _run(calc_repo, "E", {"summary": "fixed add"}, run_id="r2")
    span = [s for s in read_spans(arm_e.journal) if s.name.startswith("auto:")][-1]
    plain = json.loads(attempt_sidecar_path(arm_e.journal, span.span_id).read_text())
    assert plain["commit_contract"] == ""
    assert plain["commit_direction"] == ""
    assert plain["commit_mutants"] is None


# ---------------------------------------------------------------------------
# Review of the run (#163): the record is the committed tree's, and an unreadable
# rule is named.
# ---------------------------------------------------------------------------


def test_a_record_scored_on_an_earlier_tree_is_not_the_commits(tmp_path: Path) -> None:
    """Known-bad: the final audit (tree B) decided no mutant, as a mutation run that
    times out does, and the only record was scored on tree A. Committing A's verdicts
    for B would state a result nobody measured; the block says none recorded."""
    on_a = AuditResult(
        point="check",
        tree="tree-a",
        findings=(),
        mutant_detail=(("n.py:1:x", "killed", "show", ("tests/test_n.py::t",)),),
    )
    feed = _feed(tmp_path)
    feed.results = [on_a, AuditResult(point="finish", tree="tree-b", findings=())]
    assert feed.committed_mutants() == ()
    # Known-good: the newest record of the committed tree is kept, an older tree's
    # record after it notwithstanding, and a blocked audit (no tree) decides nothing.
    on_b = AuditResult(
        point="finish",
        tree="tree-b",
        findings=(),
        mutant_detail=(("n.py:2:y", "survived", "show", ()),),
    )
    for results in (
        [on_b, on_a, AuditResult(point="finish", tree="tree-b", findings=())],
        [on_b, AuditResult(point="finish", tree="", findings=())],
    ):
        feed.results = results
        assert feed.committed_mutants() == (("n.py:2:y", "survived"),)


@pytest.mark.parametrize("written", ['commit-rules = "true"', "commit-rules = 1"])
def test_a_value_that_is_not_a_boolean_is_named_in_the_commit_and_adds_no_block(
    calc_repo: Path, written: str
) -> None:
    """Known-bad: a value typo is read by nothing but the commit rules, so a run that
    dropped it silently would commit as though the project stated no rules. The block
    stays out, and the commit says why."""
    _state_rules(calc_repo, written)
    result = _run(
        calc_repo,
        "E+A",
        {"summary": "fixed add", "contract": CONTRACT_SENTENCE, "direction": "tightened"},
    )
    message = _message(result)
    assert MUTANTS_LABEL not in message
    assert message.splitlines()[2:4] == ["Narrative (model-written, not evidence):", "fixed add"]
    line = next(ln for ln in message.splitlines() if ln.startswith(RULES_UNREAD))
    assert "commit-rules = " in line
    assert "is not `true` or `false`" in line


def test_a_tree_without_the_key_names_no_unread_rules(calc_repo: Path) -> None:
    """Known-good: no key is no rules, said by nothing."""
    result = _run(calc_repo, "E+A", {"summary": "fixed add"})
    assert RULES_UNREAD not in _message(result)
