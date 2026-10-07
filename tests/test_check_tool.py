"""The `check` tool (`saddle auto --check-tool`): the model's pull on the auditor.

The contract, each half both ways:

- `check` runs tiers 0 and 1 of the same auditor on the current tree and
  returns its findings rendered as a finish refusal renders them; tier 1's
  finding records equal the finish audit's on the same tree;
- tier 2 never runs in a check; it still runs at finish;
- a check never counts as a finish refusal;
- a check on a tree unchanged since the last check is refused and runs
  nothing; a changed tree always allows one;
- every check that runs is sealed (an `audit:check` span whose sidecar is
  its findings, listed in the outcome's audit list) and `verify` holds it;
- without the flag the tool is not offered, the prompt and sealed records
  are unchanged, and a `check` call reaches no auditor.
"""

from __future__ import annotations

import io
import json
import subprocess
import time
from pathlib import Path
from typing import Any, cast

import pytest

from saddle import cli
from saddle.auditor import Finding, Findings
from saddle.auto import CHECK_PROMPT, AutoError, AutoOptions, AutoResult, run_auto
from saddle.engine import AUDIT_UNRESOLVED, FINISH_REFUSED
from saddle.feed import CHECK_SPAN, CHECK_UNCHANGED
from saddle.journal import attempt_sidecar_path, read_spans, verify_journal
from saddle.packet import compile_packet
from saddle.tools import CHECK_TOOL
from saddle.transcript import session_line
from saddle.vllm import ToolCall, VllmClient

BUGGY = "def add(a, b):\n    return a - b\n"
TEST = "from calc import add\n\n\ndef test_add():\n    assert add(2, 2) == 4\n"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def make_repo(root: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path / "repo", {"calc.py": BUGGY, "tests/test_calc.py": TEST})


def call(name: str, call_id: str, **arguments: Any) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=json.dumps(arguments))


def edit(call_id: str, old: str, new: str, path: str = "calc.py") -> ToolCall:
    return call("edit_file", call_id, path=path, old=old, new=new)


CHECK = call(CHECK_TOOL, "k")
FINISH = call("finish", "f", summary="done")


class Scripted:
    """Replays rounds of tool calls, then calls finish forever; records what it was sent."""

    def __init__(self, rounds: list[list[ToolCall]]) -> None:
        self.rounds = list(rounds)
        self.tools: list[list[str]] = []
        self.system = ""

    def stream_chat(self, messages: Any, **kwargs: Any) -> Any:
        self.tools.append([t["function"]["name"] for t in kwargs["tools"]])
        self.system = str(messages[0]["content"])
        return iter(self.rounds.pop(0) if self.rounds else [FINISH])


class FakeAuditor:
    """Fails `tests` (tier 1) and `mutation` (tier 2) while calc.py subtracts."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []

    def _findings(self, tier: int, text: str) -> Findings:
        self.calls.append((tier, text))
        bad = "a - b" in text
        gate = {0: "ruff", 1: "tests", 2: "mutation"}[tier]
        found = Finding(
            gate,
            tier,
            "fail" if bad else "pass",
            "code-wrong",
            "calc.py:2: a - b" if bad else "ok",
            ("fake",),
        )
        return Findings(tier=tier, key=f"k{tier}", findings=(found,))

    def tier0(self, path: str, new_text: str) -> Findings:
        return self._findings(0, new_text)

    def tier1(self, tree: Path | None = None) -> Findings:
        assert tree is not None
        return self._findings(1, (tree / "calc.py").read_text())

    def tier2(self, tree: Path | None = None) -> Findings:
        assert tree is not None
        return self._findings(2, (tree / "calc.py").read_text())

    def tiers(self) -> list[int]:
        return [tier for tier, _ in self.calls]


def run(
    repo: Path, client: Scripted, auditor: Any = None, **kwargs: Any
) -> tuple[AutoResult, FakeAuditor]:
    fake = auditor if auditor is not None else FakeAuditor()
    kwargs.setdefault("check_tool", True)
    options = AutoOptions(
        task="make add add",
        repo=repo,
        run_id=kwargs.pop("run_id", "r1"),
        auditor_factory=lambda *a: fake,
        **kwargs,
    )
    return run_auto(options, cast(VllmClient, client)), fake


def outcome(result: AutoResult) -> dict[str, Any]:
    span = [s for s in read_spans(result.journal) if s.name.startswith("auto:")][-1]
    path = attempt_sidecar_path(result.journal, span.span_id)
    return cast(dict[str, Any], json.loads(path.read_text()))


def results(result: AutoResult, name: str) -> list[str]:
    """The tool results the ledger sealed for calls to `name`, in order."""
    return [
        s.detail
        for s in read_spans(result.journal)
        if s.kind == "tool" and s.argv and s.argv[0] == name and not s.name.startswith("audit")
    ]


def check_spans(result: AutoResult) -> list[Any]:
    return [s for s in read_spans(result.journal) if s.name == CHECK_SPAN]


# -- the flag: absent means absent -------------------------------------------------


def test_without_the_flag_the_tool_is_not_offered_and_a_check_call_audits_nothing(
    repo: Path,
) -> None:
    client = Scripted([[edit("e", "a - b", "a + b")], [CHECK]])
    result, fake = run(repo, client, check_tool=False)
    assert all(CHECK_TOOL not in names for names in client.tools)
    assert CHECK_PROMPT not in client.system
    assert results(result, CHECK_TOOL) == ["error: unknown tool 'check'"]
    # nothing audited before the finish audit (tiers 0, 1, 2, once each)
    assert fake.tiers() == [0, 1, 2]
    assert check_spans(result) == []
    record = outcome(result)
    assert "check_tool" not in record
    assert "checks" not in record
    start = next(s for s in read_spans(result.journal) if s.name == "auto:start")
    assert "check tool" not in start.detail


def test_with_the_flag_the_tool_and_its_caveat_are_offered(repo: Path) -> None:
    client = Scripted([[edit("e", "a - b", "a + b")]])
    result, _ = run(repo, client)
    assert all(names[-1] == CHECK_TOOL for names in client.tools)
    assert client.system.endswith(CHECK_PROMPT)
    assert "does not guarantee finish passes" in CHECK_PROMPT
    record = outcome(result)
    assert record["check_tool"] is True
    assert record["checks"] == 0


@pytest.mark.parametrize("arm", ["E", "E+A"])
def test_the_flag_needs_the_feedback_arm(repo: Path, arm: str) -> None:
    with pytest.raises(AutoError, match="--check-tool needs arm E\\+A\\+F"):
        run(repo, Scripted([]), arm=arm)


def test_the_cli_flag_is_unset_by_default_and_passes_through(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unset (None) lets the arm decide; `--no-check-tool` is an explicit off."""
    seen: list[AutoOptions] = []

    def fake_run(options: AutoOptions, client: Any, **kwargs: Any) -> AutoResult:
        seen.append(options)
        raise AutoError(options.task)

    monkeypatch.setattr(cli, "run_auto", fake_run)
    parser = cli.build_parser()
    for argv in (
        ["auto", "t", "--repo", str(repo)],
        ["auto", "t", "--check-tool"],
        ["auto", "t", "--no-check-tool"],
    ):
        out = io.StringIO()
        assert (
            cli.run_auto_command(parser.parse_args(argv), cast(VllmClient, None), stdout=out) == 1
        )
    assert [o.check_tool for o in seen] == [None, True, False]


def test_by_default_the_feedback_arm_offers_check_and_says_to_use_it(repo: Path) -> None:
    """Red before: the tool was off unless asked for, so a watched dogfood run
    could only learn whether its change passed by running the whole suite and
    every Chrome module itself, again and again."""
    client = Scripted([[edit("e", "a - b", "a + b")]])
    result, _ = run(repo, client, check_tool=None)
    assert all(names[-1] == CHECK_TOOL for names in client.tools)
    assert client.system.endswith(CHECK_PROMPT)
    assert "instead of running the whole test suite" in CHECK_PROMPT
    assert outcome(result)["check_tool"] is True


@pytest.mark.parametrize("arm", ["E", "E+A"])
def test_by_default_the_arms_without_feedback_neither_offer_check_nor_refuse(
    repo: Path, arm: str
) -> None:
    client = Scripted([[edit("e", "a - b", "a + b")]])
    result, _ = run(repo, client, check_tool=None, arm=arm)
    assert all(CHECK_TOOL not in names for names in client.tools)
    assert CHECK_PROMPT not in client.system
    assert "check_tool" not in outcome(result)


# -- what a check runs, and what it never runs ------------------------------------


def test_a_check_runs_tiers_zero_and_one_and_never_tier_two(repo: Path) -> None:
    client = Scripted([[edit("e", "a - b", "a + b")], [CHECK]])
    result, fake = run(repo, client)
    finish_at = len(fake.calls) - 3  # the finish audit: tiers 0, 1, 2
    assert fake.tiers()[:finish_at] == [0, 1]
    assert fake.tiers()[finish_at:] == [0, 1, 2]  # tier 2 still runs at finish
    assert result.outcome == "finished"


def test_a_check_returns_the_findings_as_a_finish_refusal_renders_them(repo: Path) -> None:
    client = Scripted([[edit("e", "def add", "# adds\ndef add")], [CHECK], [FINISH]])
    result, _ = run(repo, client, finish_refusal_cap=1)
    (checked,) = results(result, CHECK_TOOL)
    refused = results(result, "finish")[0]
    assert refused.startswith(FINISH_REFUSED)
    line = "- tests (tier 1): fail, code-wrong: calc.py:2: a - b"
    ruff = "- ruff (tier 0): fail, code-wrong: calc.py:2: a - b"
    assert line in checked.splitlines()
    assert ruff in checked.splitlines()
    # The finish refuses on the failing edit check before the suite runs (the
    # check, which runs no mutation, still shows the tests): same line format.
    assert ruff in refused.splitlines()
    assert "tiers 1 and 2 were not run" in refused
    assert checked.startswith("[audit check 1 on tree ")
    assert not checked.startswith("error: ")


def test_an_unchanged_tree_is_refused_and_a_changed_one_is_checked(repo: Path) -> None:
    client = Scripted(
        [
            [edit("e", "def add", "# adds\ndef add")],
            [CHECK],
            [CHECK],
            [edit("x", "a - b", "a + b")],
            [CHECK],
        ]
    )
    result, fake = run(repo, client)
    first, second, third = results(result, CHECK_TOOL)
    assert "FAIL]" in first
    assert second.startswith(CHECK_UNCHANGED + "1;")
    assert "PASS]" in third
    assert third.startswith("[audit check 2 ")
    assert fake.tiers()[:4] == [0, 1, 0, 1]  # the refused check ran nothing
    assert len(check_spans(result)) == 2
    assert outcome(result)["refusals"] == 0  # not a tier-0 guard refusal either


def test_a_check_never_counts_as_a_finish_refusal(repo: Path) -> None:
    client = Scripted(
        [
            [edit("e", "def add", "# one\ndef add")],
            [CHECK],
            [edit("e2", "def add", "# two\ndef add")],
            [CHECK],
            [FINISH],
            [edit("x", "a - b", "a + b")],
            [FINISH],
        ]
    )
    result, _ = run(repo, client, finish_refusal_cap=2)
    record = outcome(result)
    assert result.outcome == "finished", record["reason"]
    assert record["finish_refusals"] == 1
    assert record["checks"] == 2


def test_failing_checks_alone_never_reach_the_cap(repo: Path) -> None:
    rounds: list[list[ToolCall]] = []
    for n in range(3):
        rounds += [[edit(f"e{n}", "def add", f"# {n}\ndef add")], [CHECK]]
    rounds += [[FINISH], [FINISH]]
    result, _ = run(repo, Scripted(rounds), finish_refusal_cap=2)
    record = outcome(result)
    assert result.reason == AUDIT_UNRESOLVED
    assert record["finish_refusals"] == 2
    assert len(results(result, "finish")) == 2


# -- sealed ----------------------------------------------------------------------


def test_each_check_is_sealed_under_the_run_and_verify_holds_it(repo: Path) -> None:
    client = Scripted([[edit("e", "def add", "# adds\ndef add")], [CHECK]])
    result, _ = run(repo, client, finish_refusal_cap=1)
    (span,) = check_spans(result)
    start = next(s for s in read_spans(result.journal) if s.name == "auto:start")
    assert span.parent_id == start.span_id
    assert span.exit_code == 1
    record = json.loads(attempt_sidecar_path(result.journal, span.span_id).read_text())
    assert record["point"] == "check 1"
    assert [f["gate"] for f in record["findings"]] == ["ruff", "tests"]
    assert span.record_hash in outcome(result)["audit_span_hashes"]
    assert verify_journal(result.journal) == []
    lines = result.journal.read_text().splitlines(keepends=True)
    result.journal.write_text("".join(ln for ln in lines if CHECK_SPAN not in ln))
    assert "audit-span-missing" in {i.code for i in verify_journal(result.journal)}


def test_the_packet_counts_the_checks_and_reports_the_last(repo: Path) -> None:
    client = Scripted(
        [[edit("e", "def add", "# adds\ndef add")], [CHECK], [edit("x", "a - b", "a + b")], [CHECK]]
    )
    result, _ = run(repo, client)
    packet = compile_packet(result.journal)
    row = next(r for r in packet.rows if r.key == "check")
    assert row.text.startswith("The model checked 2 times; last check: 0 failing findings.")
    assert row.cites == tuple(s.record_hash for s in check_spans(result))
    lines = [ln.text for s in read_spans(result.journal) if (ln := session_line(s)) is not None]
    assert any("audit check 2 passed, returned by check" in t for t in lines)


def test_a_packet_with_no_check_has_no_check_row(repo: Path) -> None:
    result, _ = run(repo, Scripted([[edit("e", "a - b", "a + b")]]), check_tool=False)
    assert all(r.key != "check" for r in compile_packet(result.journal).rows)


def test_a_tampered_check_record_leaves_the_packet_unrecorded(repo: Path) -> None:
    client = Scripted([[edit("e", "def add", "# adds\ndef add")], [CHECK]])
    result, _ = run(repo, client, finish_refusal_cap=1)
    (span,) = check_spans(result)
    sidecar = attempt_sidecar_path(result.journal, span.span_id)
    sidecar.write_text(sidecar.read_text().replace('"fail"', '"pass"'))
    packet = compile_packet(result.journal)
    assert packet.verdict == "unrecorded"
    assert all(r.key != "check" for r in packet.rows)


# -- the real auditor: a check and the finish audit agree on the same tree ---------

REAL_BASE = "def f():\n    return 1\n"
REAL_TEST = "from n import f\n\n\ndef test_f():\n    assert f() == 2\n"


@pytest.fixture
def real_repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path / "real", {"n.py": REAL_BASE, "test_n.py": REAL_TEST})


def _real(repo: Path, new: str) -> AutoResult:
    client = Scripted([[edit("e", "return 1", new, path="n.py")], [CHECK], [FINISH]])
    options = AutoOptions(
        task="make f return 2", repo=repo, run_id="real", check_tool=True, finish_refusal_cap=1
    )
    return run_auto(options, cast(VllmClient, client))


def _tier1(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [f for f in findings if f["tier"] == 1]


@pytest.mark.parametrize(("new", "passed"), [("return 2", True), ("return 3", False)])
def test_check_and_finish_seal_identical_tier_one_findings(
    real_repo: Path, new: str, passed: bool
) -> None:
    result = _real(real_repo, new)
    (span,) = check_spans(result)
    check = json.loads(attempt_sidecar_path(result.journal, span.span_id).read_text())
    finish = outcome(result)["audit"]
    assert check["tree"] == finish["tree"]
    assert _tier1(check["findings"]) == _tier1(finish["findings"])
    assert [f["gate"] for f in check["findings"] if f["tier"] == 0] == ["syntax", "ruff", "imports"]
    assert all(f["tier"] in (0, 1) for f in check["findings"])
    assert check["passed"] is passed
    assert result.outcome == ("finished" if passed else "stopped")
    if not passed:
        (checked,) = results(result, CHECK_TOOL)
        refused = results(result, "finish")[0]
        failing = [ln for ln in checked.splitlines() if ln.startswith("- ")]
        assert failing
        assert all(ln in refused.splitlines() for ln in failing)


def test_a_real_check_names_an_unused_import_and_clears_when_it_is_removed(
    real_repo: Path,
) -> None:
    client = Scripted(
        [
            [
                edit(
                    "e",
                    "def f():\n    return 1",
                    "import os\n\n\ndef f():\n    return 2",
                    path="n.py",
                )
            ],
            [CHECK],
            [edit("x", "import os\n\n\n", "", path="n.py")],
            [CHECK],
            [FINISH],
        ]
    )
    options = AutoOptions(task="make f return 2", repo=real_repo, run_id="lint", check_tool=True)
    result = run_auto(options, cast(VllmClient, client))
    first, second = results(result, CHECK_TOOL)
    assert "- ruff (tier 0): fail" in first
    assert "F401" in first
    assert "PASS]" in second
    assert result.outcome == "finished"


# -- #171: no hand-run whole suite; check runs it instead ----------------------------


def test_with_check_offered_the_prompt_never_coaches_a_hand_run_whole_suite(
    repo: Path,
) -> None:
    """Red before (#171): with check offered, the prompt said both "use it
    instead of running the whole test suite" and "when you run the whole
    suite, pass `-n 8` too"; a dogfood run then ran the whole suite by hand
    three times. Without check the worker count is still said."""
    from test_project_env import make_venv

    (repo / "pyproject.toml").write_text("[tool.saddle]\ntest-workers = 8\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "workers")
    make_venv(repo / ".venv")
    (next((repo / ".venv").glob("lib/python*/site-packages")) / "xdist").mkdir()
    said = {}
    for offered in (True, False):
        client = Scripted([])
        run(repo, client, check_tool=offered, run_id=f"r{offered}")
        said[offered] = "pass `-n 8` too" in client.system
    assert said == {True: False, False: True}
    assert "whole_suite set to true" in CHECK_PROMPT
    assert "starts the whole suite or the gate is refused" in CHECK_PROMPT


class WholeSuiteAuditor(FakeAuditor):
    """A FakeAuditor that also runs tier 1 over the whole suite, recorded apart."""

    def __init__(self) -> None:
        super().__init__()
        self.whole: list[str] = []

    def tier1_whole_suite(self, tree: Path | None = None) -> Findings:
        assert tree is not None
        self.whole.append((tree / "calc.py").read_text())
        return self._findings(1, (tree / "calc.py").read_text())


WHOLE = call(CHECK_TOOL, "w", whole_suite=True)


def test_a_whole_suite_check_runs_the_whole_suite_and_a_repeat_of_its_mode_is_refused(
    repo: Path,
) -> None:
    """#171: check with whole_suite runs tier 1 over every test file; the
    unchanged-tree refusal is per mode, so a narrowed check of the same tree
    still runs, and a second whole-suite check of an unchanged tree does not."""
    fake = WholeSuiteAuditor()
    client = Scripted([[edit("e", "a - b", "a + b")], [WHOLE], [WHOLE], [CHECK]])
    result, _ = run(repo, client, auditor=fake)
    first, second, third = results(result, CHECK_TOOL)
    assert len(fake.whole) == 1
    assert "PASS]" in first
    assert second.startswith(CHECK_UNCHANGED)
    assert "PASS]" in third
    # tier 1: the whole-suite check, the narrowed check, the finish audit
    assert len([t for t, _ in fake.calls if t == 1]) == 3
    argvs = [s.argv for s in check_spans(result)]
    assert argvs[0][-1] == "whole-suite"
    assert argvs[1][-1] != "whole-suite"


def test_a_whole_suite_check_on_an_auditor_without_it_is_blocked_never_narrowed(
    repo: Path,
) -> None:
    fake = FakeAuditor()
    client = Scripted([[edit("e", "a - b", "a + b")], [WHOLE]])
    result, _ = run(repo, client, auditor=fake)
    (said,) = results(result, CHECK_TOOL)
    assert "cannot run the whole suite in a check" in said
    assert [t for t, _ in fake.calls if t == 1] == [1]  # only the finish audit's


@pytest.mark.parametrize("value", ["true", 1, None])
def test_a_whole_suite_that_is_not_a_boolean_runs_nothing(repo: Path, value: object) -> None:
    from saddle.engine import CHECK_WHOLE_SUITE_NOT_BOOL

    fake = WholeSuiteAuditor()
    client = Scripted([[edit("e", "a - b", "a + b")], [call(CHECK_TOOL, "b", whole_suite=value)]])
    result, _ = run(repo, client, auditor=fake)
    assert results(result, CHECK_TOOL) == [CHECK_WHOLE_SUITE_NOT_BOOL]
    assert fake.whole == []
    assert check_spans(result) == []


@pytest.mark.parametrize("arguments", ["{not json", "[]", '"whole_suite"'])
def test_check_arguments_that_are_not_an_object_run_nothing(repo: Path, arguments: str) -> None:
    """Unreadable arguments may have asked for the whole suite; read as none,
    a narrowed check would answer for it. No arguments at all is the default."""
    from saddle.engine import CHECK_ARGUMENTS_NOT_OBJECT

    fake = WholeSuiteAuditor()
    broken = ToolCall(id="b", name=CHECK_TOOL, arguments=arguments)
    bare = ToolCall(id="n", name=CHECK_TOOL, arguments="")
    client = Scripted([[edit("e", "a - b", "a + b")], [broken], [bare]])
    result, _ = run(repo, client, auditor=fake)
    refused, ran = results(result, CHECK_TOOL)
    assert refused == CHECK_ARGUMENTS_NOT_OBJECT
    assert "PASS]" in ran
    assert fake.whole == []
    assert len(check_spans(result)) == 1


# -- #178: with check offered, a hand-run whole suite is refused --------------------

SUITE_BY_HAND = "python -m pytest -q -n 8 > /tmp/suite.log 2>&1"
ONE_FILE = "python -m pytest tests/test_calc.py -q --no-cov"


@pytest.mark.parametrize("offered", [True, False])
def test_a_hand_run_whole_suite_is_refused_only_while_check_is_offered(
    repo: Path, offered: bool
) -> None:
    """Refused with check offered, pointing at check whole_suite; without check
    there is no other way to run the suite, so it runs."""
    from saddle.engine import WHOLE_SUITE_BY_CHECK

    command = call("run_command", "s", command=SUITE_BY_HAND)
    client = Scripted([[edit("e", "a - b", "a + b")], [command]])
    result, _ = run(repo, client, check_tool=offered, run_id=f"s{offered}")
    (said,) = results(result, "run_command")
    assert (said == WHOLE_SUITE_BY_CHECK) is offered
    assert "whole_suite set to true" in WHOLE_SUITE_BY_CHECK


def test_with_check_offered_one_test_file_still_runs(repo: Path) -> None:
    from saddle.engine import WHOLE_SUITE_BY_CHECK

    client = Scripted([[edit("e", "a - b", "a + b")], [call("run_command", "o", command=ONE_FILE)]])
    result, _ = run(repo, client)
    (said,) = results(result, "run_command")
    assert said != WHOLE_SUITE_BY_CHECK
    assert "passed" in said


@pytest.mark.parametrize("arguments", ["{not json", "[]", '{"command": 5}'])
def test_a_run_command_the_guard_cannot_read_is_left_to_the_tool(
    repo: Path, arguments: str
) -> None:
    from saddle.engine import WHOLE_SUITE_BY_CHECK

    broken = ToolCall(id="b", name="run_command", arguments=arguments)
    client = Scripted([[edit("e", "a - b", "a + b")], [broken]])
    result, _ = run(repo, client)
    (said,) = results(result, "run_command")
    assert said != WHOLE_SUITE_BY_CHECK
    assert said.startswith("error: ")


# -- #175: check and audit spans carry the audit's measured time --------------------

SLOW_S = 1.0


class SlowAuditor(FakeAuditor):
    """A FakeAuditor whose suite tiers each take SLOW_S, or none with slow=False."""

    def __init__(self, *, slow: bool) -> None:
        super().__init__()
        self.slow = slow

    def tier1(self, tree: Path | None = None) -> Findings:
        if self.slow:
            time.sleep(SLOW_S)
        return super().tier1(tree)

    def tier2(self, tree: Path | None = None) -> Findings:
        if self.slow:
            time.sleep(SLOW_S)
        return super().tier2(tree)


@pytest.mark.parametrize("slow", [True, False])
def test_check_and_finish_spans_record_how_long_their_audit_took(repo: Path, slow: bool) -> None:
    client = Scripted([[edit("e", "a - b", "a + b")], [CHECK]])
    result, _ = run(repo, client, auditor=SlowAuditor(slow=slow), run_id=f"d{slow}")
    (checked,) = check_spans(result)
    (finished,) = [
        s
        for s in read_spans(result.journal)
        if s.name.startswith("audit:") and s.argv[:2] == ["audit", "finish"]
    ]
    floor = int(SLOW_S * 1000)
    if slow:
        assert floor <= checked.duration_ms < 30_000  # tier 1
        assert 2 * floor <= finished.duration_ms < 30_000  # tiers 1 and 2
    else:
        assert checked.duration_ms < floor
        assert finished.duration_ms < floor
