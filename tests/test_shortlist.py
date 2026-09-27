"""SHORTLIST: the tier-2 mutation verdict as a per-line survivor shortlist.

Contract (scope narrowed from an 85% score): the audit's mutation finding
passes iff no surviving -- or untested -- mutant on a changed line lacks an
accepted reason. The score is still computed and recorded in `basis`
(sealed in the finding's cites) but decides nothing. The detail names up to
N survivors, one per line first, each with file:line, the changed line's
text and the mutation.

Loosening proof (CONTRIBUTING.md): M3F (internal run report, not public)
measured 6 of 6 oracle-PASS T5 trees scoring 63-76% against the 85% bar.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from saddle import cli
from saddle.auditor import Auditor, AuditorConfig, Findings, Tier2Mode, behaviour_at
from saddle.auto import AutoOptions, run_auto
from saddle.evidence import (
    MutationOutcome,
    SurvivorDetail,
    message_only_mutant,
    mutation_sample,
    mutation_text,
    run_argv,
)
from saddle.gates import (
    check_mutation,
    check_mutation_shortlist,
    set_aside_kind,
    shortlist_order,
)
from saddle.journal import attempt_sidecar_path, read_spans
from saddle.vllm import ToolCall


def _outcome(killed: int, details: list[SurvivorDetail]) -> MutationOutcome:
    return MutationOutcome(
        killed=killed,
        total=killed + len(details),
        generated=killed + len(details),
        survivors=tuple(d[0] for d in details),
        survivor_details=tuple(details),
        untested=sum(1 for d in details if d[1] == "no tests"),
    )


def _d(
    name: str, line: int, status: str = "survived", path: str = "m.py", message: bool = False
) -> SurvivorDetail:
    return (name, status, path, line, f"-    return {line}\n+    return {line + 1}", message)


SOURCE = "def f():\n" + "".join(f"    x{i} = {i}\n" for i in range(2, 12))


# -- the verdict ---------------------------------------------------------------


def test_a_survivor_fails_even_when_the_score_clears_the_bar() -> None:
    """Mutant 'the score still decides' dies here: 19 of 20 is 95% >= 85%."""
    outcome = _outcome(19, [_d("m1", 3)])
    assert check_mutation(outcome, 85.0).passed  # the old gate passed it
    check = check_mutation_shortlist(outcome, 85.0)
    assert not check.passed
    assert "m.py:3" in check.detail


def test_every_survivor_accepted_passes_below_the_bar() -> None:
    """Known-good in the M3F shape: a low score whose survivors all carry a reason."""
    outcome = _outcome(1, [_d("m1", 3), _d("m2", 4)])
    assert not check_mutation(outcome, 85.0).passed
    check = check_mutation_shortlist(outcome, 85.0, accepted={("m.py", 3), ("m.py", 4)})
    assert check.passed
    assert "2 survivor(s) on lines with an accepted reason" in check.detail


def test_an_accepted_line_does_not_excuse_another_line() -> None:
    outcome = _outcome(5, [_d("m1", 3), _d("m2", 4)])
    check = check_mutation_shortlist(outcome, 85.0, accepted={("m.py", 3)})
    assert not check.passed
    assert "m.py:4" in check.detail
    assert "m.py:3" not in check.detail


def test_no_survivor_passes_and_the_score_is_recorded_not_decisive() -> None:
    outcome = _outcome(7, [])
    check = check_mutation_shortlist(outcome, 85.0)
    assert check.passed
    assert check.basis == "sampled n=7; score 100.0% vs 85.0% (recorded, not decisive)"
    low = check_mutation_shortlist(_outcome(3, [_d("m1", 2)]), 85.0)
    assert low.basis is not None
    assert "score 75.0%" in low.basis


def test_an_empty_population_keeps_the_old_no_evidence_verdict() -> None:
    for outcome in (
        MutationOutcome(killed=0, total=0, generated=0, survivors=()),
        MutationOutcome(killed=0, total=0, generated=0, survivors=("mutmut run exited 1: x",)),
    ):
        check = check_mutation_shortlist(outcome, 85.0)
        assert check == check_mutation(outcome, 85.0)
        assert not check.passed


# -- the shortlist ---------------------------------------------------------------


def test_every_survivor_is_named_when_n_allows_it() -> None:
    """Mutant 'a survivor omitted from the list when N allows it' dies here."""
    outcome = _outcome(10, [_d("m1", 3), _d("m2", 5), _d("m3", 7)])
    check = check_mutation_shortlist(outcome, 85.0, sources={"m.py": SOURCE})
    rows = [r for r in check.detail.splitlines() if r.startswith("- ")]
    assert [r.split()[1] for r in rows] == ["m.py:3", "m.py:5", "m.py:7"]
    assert "more)" not in check.detail
    assert "3 surviving mutant(s)" in check.detail


def test_untested_mutants_are_listed_and_counted() -> None:
    """Mutant 'untested mutants not listed' dies here."""
    outcome = _outcome(4, [_d("m1", 3, "no tests"), _d("m2", 4)])
    check = check_mutation_shortlist(outcome, 85.0)
    assert "- m.py:3" in check.detail
    assert "(no tests)" in check.detail
    assert "1 untested" in check.detail
    assert not check_mutation_shortlist(_outcome(4, [_d("m1", 3, "no tests")]), 85.0).passed


def test_n_caps_the_list_and_counts_the_rest() -> None:
    outcome = _outcome(0, [_d(f"m{i}", i) for i in range(2, 9)])
    check = check_mutation_shortlist(outcome, 85.0, shortlist=5)
    rows = [r for r in check.detail.splitlines() if r.startswith("- ")]
    assert len(rows) == 5
    assert check.detail.endswith("(and 2 more)")
    two = check_mutation_shortlist(outcome, 85.0, shortlist=2)
    assert len([r for r in two.detail.splitlines() if r.startswith("- ")]) == 2


def test_a_row_carries_the_line_text_and_the_mutation() -> None:
    outcome = _outcome(0, [_d("m1", 3)])
    check = check_mutation_shortlist(outcome, 85.0, sources={"m.py": SOURCE})
    assert "- m.py:3 `x3 = 3`: mutant m1 (survived): -    return 3 -> +    return 4" in check.detail
    bare = check_mutation_shortlist(outcome, 85.0)
    assert "- m.py:3: mutant m1" in bare.detail


def test_one_mutant_per_line_first_then_the_rest() -> None:
    details = [_d("a", 3), _d("b", 3), _d("c", 4), _d("d", 2, path="a.py")]
    assert [d[0] for d in shortlist_order(details)] == ["d", "a", "c", "b"]


# -- the evidence: every survivor carries its line and its mutation -----------


def _stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, results: str, show: str) -> None:
    stub = tmp_path / "stub"
    stub.mkdir()
    script = stub / "mutmut"
    script.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  run) exit 0;;\n"
        f"  results) printf '%s\\n' '{results}';;\n"
        f"  show) printf '%s' '{show}';;\n"
        "esac\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{stub}{os.pathsep}{os.environ['PATH']}")


SHOW = "--- n.py\n+++ n.py\n@@ -2 +2 @@\n-    return 2\n+    return 3\n"


def test_mutation_sample_records_each_survivor_with_its_line_and_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub(tmp_path, monkeypatch, "  m1: survived\n  m2: killed\n  m3: no tests", SHOW)
    work = tmp_path / "w"
    work.mkdir()
    (work / "n.py").write_text("def f():\n    return 2\n")
    outcome = mutation_sample(work, {(str(work / "n.py"), 2)}, 10, test_files=())
    assert outcome.survivor_details == (
        ("m1", "survived", str(work / "n.py"), 2, "-    return 2\n+    return 3", False),
        ("m3", "no tests", str(work / "n.py"), 2, "-    return 2\n+    return 3", False),
    )


def test_mutation_text_keeps_only_hunk_lines() -> None:
    assert mutation_text(SHOW) == "-    return 2\n+    return 3"
    assert mutation_text("# m1: survived\n") == ""


# -- the auditor: tier 2 decides on the shortlist ------------------------------


def _git(root: Path, *argv: str) -> None:
    assert run_argv(["git", *argv], root) == 0


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "tree"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "n.py").write_text("def f():\n    return 1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "base")
    (root / "n.py").write_text('def f():\n    """Return the answer."""\n    return 2\n')
    (root / "test_n.py").write_text("from n import f\n\n\ndef test_f():\n    assert f() == 2\n")
    return root


SHORTLIST = AuditorConfig(tier2="shortlist")
SHOW3 = "--- n.py\n+++ n.py\n@@ -3 +3 @@\n-    return 2\n+    return 3\n"


def test_tier2_surfaces_one_survivor_at_a_95_percent_score_as_not_proven(
    tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    results = "\n".join([*(f"  k{i}: killed" for i in range(19)), "  s1: survived"])
    _stub(tmp_path, monkeypatch, results, SHOW3)
    found = Auditor(tree, config=SHORTLIST).tier2()
    mutation = next(f for f in found.findings if f.gate == "mutation")
    # flip (SHORTLIST-5): was "fail"; out/SHORTRESEARCH/report.md section 3.
    assert mutation.verdict == "not-proven", mutation.detail
    assert "- n.py:3 `return 2`: mutant s1 (survived)" in mutation.detail
    assert mutation.cites == (
        "saddle.gates.check_mutation_shortlist",
        "sampled n=20; score 95.0% vs 85.0% (recorded, not decisive)",
    )
    assert [(s.path, s.line, s.name, s.source) for s in found.survivors] == [
        ("n.py", 3, "s1", "return 2")
    ]
    assert found.survivors[0].behaviour == "`f`: Return the answer."
    assert found.survivors[0].mutation == "-    return 2\n+    return 3"
    assert Findings.from_dict(found.to_dict()).survivors == found.survivors
    assert found.passed  # flip (SHORTLIST-5): was `not found.passed`


def test_tier2_passes_with_every_mutant_killed(tree: Path) -> None:
    found = Auditor(tree, config=SHORTLIST).tier2()
    mutation = next(f for f in found.findings if f.gate == "mutation")
    assert mutation.verdict == "pass", mutation.detail
    assert found.survivors == ()
    assert "survivors" not in found.to_dict()


def test_score_mode_is_the_default_and_keeps_the_old_verdict(
    tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Flag off: one survivor at 95% passes on the bar, cited as before."""
    results = "\n".join([*(f"  k{i}: killed" for i in range(19)), "  s1: survived"])
    _stub(tmp_path, monkeypatch, results, SHOW3)
    assert AuditorConfig().tier2 == "score"
    found = Auditor(tree).tier2()
    mutation = next(f for f in found.findings if f.gate == "mutation")
    assert mutation.verdict == "pass"
    assert mutation.cites == ("saddle.gates.check_mutation", "sampled n=20")
    assert mutation.detail == "killed 19 of 20 changed-line mutants (95.0% >= 85.0%)"
    assert found.survivors == ()
    assert "survivors" not in found.to_dict()
    assert Auditor(tree)._key(2, "t") != Auditor(tree, config=SHORTLIST)._key(2, "t")


def test_the_shortlist_size_is_configurable_and_keys_the_cache(
    tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    results = "\n".join(f"  s{i}: survived" for i in range(3))
    _stub(tmp_path, monkeypatch, results, SHOW3)
    one = Auditor(tree, config=AuditorConfig(tier2="shortlist", mutant_shortlist=1))
    mutation = next(f for f in one.tier2().findings if f.gate == "mutation")
    assert len([r for r in mutation.detail.splitlines() if r.startswith("- ")]) == 1
    assert "(and 2 more)" in mutation.detail
    assert len(one.tier2().survivors) == 3
    assert one._key(2, "t") != Auditor(tree, config=SHORTLIST)._key(2, "t")


def test_behaviour_at_names_the_innermost_def() -> None:
    src = (
        'def outer():\n    """Outer doc.\n\n    More."""\n'
        "    def inner():\n        return 1\n    return inner\nX = 1\n"
    )
    assert behaviour_at(src, 6) == "`inner`"
    assert behaviour_at(src, 7) == "`outer`: Outer doc."
    assert behaviour_at(src, 8) == "module top level"
    assert behaviour_at("def (:\n", 1) == "module top level"


# -- plumbing: --mutant-shortlist reaches the auditor and is sealed -----------


def test_the_cli_flags_reach_the_auditor_config_and_the_seal(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    got: list[tuple[str, int]] = []

    def spy(options: AutoOptions, client: object, **kw: object) -> None:
        got.append((options.tier2, options.mutant_shortlist))

    monkeypatch.setattr(cli, "run_auto", spy)
    parser = cli.build_parser()
    for argv in (
        ["auto", "t", "--tier2", "shortlist", "--mutant-shortlist", "2"],
        ["auto", "t"],
    ):
        with pytest.raises(AttributeError):
            cli.run_auto_command(parser.parse_args(argv), None, stdout=None)  # type: ignore[arg-type]
    assert got == [("shortlist", 2), ("score", 5)]

    configs: list[AuditorConfig] = []

    class Client:
        def stream_chat(self, messages: object, **kwargs: object) -> object:
            return iter([ToolCall(id="f", name="finish", arguments='{"summary": "x"}')])

    def factory(repo: Path, base: str, config: AuditorConfig) -> Auditor:
        configs.append(config)
        return Auditor(repo, base, config)

    _git(tree, "add", "-A")
    _git(tree, "commit", "-m", "work")
    cases: tuple[tuple[Tier2Mode, int, dict[str, object]], ...] = (
        ("shortlist", 2, {"tier2": "shortlist", "mutant_shortlist": 2}),
        ("score", 2, {}),
    )
    for mode, n, sealed in cases:
        options = AutoOptions(
            task="t",
            repo=tree,
            run_id=f"s{mode}",
            tier2=mode,
            mutant_shortlist=n,
            auditor_factory=factory,
        )
        result = run_auto(options, Client())  # type: ignore[arg-type]
        assert (configs[-1].tier2, configs[-1].mutant_shortlist) == (mode, n)
        span = [s for s in read_spans(result.journal) if s.name.startswith("auto:")][-1]
        record = json.loads(attempt_sidecar_path(result.journal, span.span_id).read_text())
        keys = ("tier2", "mutant_shortlist")
        assert {k: v for k, v in record.items() if k in keys} == sealed


def test_saddle_audit_tier2_shortlist_implies_tiered(
    tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    results = "\n".join([*(f"  k{i}: killed" for i in range(19)), "  s1: survived"])
    _stub(tmp_path, monkeypatch, results, SHOW3)
    out, err = io.StringIO(), io.StringIO()
    argv = ["audit", "--repo", str(tree), "--no-cache", "--json"]
    code = cli.main(
        [*argv, "--tier2", "shortlist", "--mutant-shortlist", "1"], stdout=out, stderr=err
    )
    payload = json.loads(out.getvalue())
    assert code == 0, err.getvalue()  # flip (SHORTLIST-5): was 1; the survivor is surfaced
    mutation = next(f for t in payload["tiers"] for f in t["findings"] if f["gate"] == "mutation")
    assert "- n.py:3 `return 2`: mutant s1 (survived)" in mutation["detail"]
    out = io.StringIO()
    assert cli.main([*argv, "--tiered"], stdout=out, stderr=err) == 0


# -- message-only survivors are excluded by AST (CALIB, E-t8 s5) --------------

FIXTURES = Path(__file__).parent / "fixtures" / "shortlist"
"""E-t8 s5's sealed tree (`stockbook.py` at 2e849d6) and its 7 survivors as
`mutmut show` printed them (out/CALIB/results/E-t8-s5.tiered.json)."""


def test_e_t8_s5_message_mutants_are_excluded_and_the_rest_stay() -> None:
    """Known-good: CALIB classified 5 of the 7 as message swaps (4 raised
    `ValueError(f"...")` -> `ValueError(None)`, 1 `KeyError(sku)` ->
    `KeyError(None)`); the field-name swap in `_check_int(percent, ...)` and
    the equivalent `Decimal(1)` -> `Decimal(2)` stay."""
    source = (FIXTURES / "stockbook_e_t8_s5.py.txt").read_text()
    survivors = json.loads((FIXTURES / "e_t8_s5_survivors.json").read_text())
    verdicts = {
        s["name"].rsplit("ǁ", 1)[1]: message_only_mutant(s["show"], source, s["name"])
        for s in survivors
    }
    assert verdicts == {
        "adjust__mutmut_12": True,
        "find_by_tag__mutmut_8": True,
        "pop__mutmut_3": True,
        "scale_prices__mutmut_9": True,
        "set_price__mutmut_10": True,
        "scale_prices__mutmut_2": False,
        "scale_prices__mutmut_26": False,
    }


def _show(old: str, new: str) -> str:
    return f"--- m.py\n+++ m.py\n@@ -1 +1 @@\n-{old}\n+{new}\n"


GUARD = (
    "def g(x):\n    if x < 0:\n"
    '        raise ValueError(f"bad {x}")\n    log.info("x=%s", x)\n    return x\n'
)


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ('        raise ValueError(f"bad {x}")', "        raise ValueError(None)", True),
        ('    log.info("x=%s", x)', '    log.info("XXx=%sXX", x)', True),
        ('    log.info("x=%s", x)', "    log.info(None, x)", True),
        # known-bad: the comparison that decides whether to raise
        ("    if x < 0:", "    if x <= 0:", False),
        # known-bad: the call target, not its argument
        ('    log.info("x=%s", x)', '    log.debug("x=%s", x)', False),
        ("    return x", "    return None", False),
        # cannot be placed in the source: never message-only
        ("    return y", "    return None", False),
    ],
)
def test_message_only_is_decided_on_the_ast(old: str, new: str, expected: bool) -> None:
    assert message_only_mutant(_show(old, new), GUARD, "m1") is expected


def test_a_string_outside_a_message_call_is_not_message_only() -> None:
    def verdict(line: str, new: str) -> bool:
        return message_only_mutant(
            _show(f"    {line}", f"    {new}"), f"def g():\n    {line}\n", "m1"
        )

    assert not verdict('return "abc"', 'return "XXabcXX"')
    assert verdict('return MyError("abc")', "return MyError(None)")
    assert verdict('raise make("abc")', "raise make(None)")
    assert not verdict('return fmt.info("abc")', "return fmt.info(None)")
    assert not verdict('return get()("abc")', "return get()(None)")
    assert verdict('log.info("abc")', 'log.info("abc", x)')
    assert not verdict("global a", "global b")
    assert not verdict('log.info("abc")', "")


def test_an_unparseable_or_empty_hunk_is_not_message_only() -> None:
    assert not message_only_mutant("--- m.py\n+++ m.py\n", GUARD, "m1")
    assert not message_only_mutant(_show("    return x", "    return x +"), GUARD, "m1")
    assert not message_only_mutant(_show("    return x", "    return x"), GUARD, "m1")
    assert not message_only_mutant(_show("x", "y"), "def (:\n", "m.x_g__mutmut_1")
    assert not message_only_mutant(_show("x", "y"), GUARD, "m.x_nope__mutmut_1")


def test_message_only_survivors_leave_the_shortlist_counted() -> None:
    outcome = _outcome(9, [_d("m1", 3, message=True), _d("m2", 4)])
    check = check_mutation_shortlist(outcome, 85.0)
    assert not check.passed
    assert "1 message-only survivor(s) excluded" in check.detail
    assert "m.py:3" not in check.detail
    only = check_mutation_shortlist(_outcome(9, [_d("m1", 3, message=True)]), 85.0)
    assert only.passed


# -- mutant_detail: recorded for every scored mutant (MUTSUMMARY's input) -----


def test_mutant_detail_records_killed_mutants_and_omits_unscored_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    results = "  m1: survived\n  m2: killed\n  m3: no tests\n  m4: not checked"
    _stub(tmp_path, monkeypatch, results, SHOW)
    work = tmp_path / "w"
    work.mkdir()
    (work / "n.py").write_text("def f():\n    return 2\n")
    outcome = mutation_sample(work, {(str(work / "n.py"), 2)}, 10, test_files=())
    assert [(n, s) for n, s, _ in outcome.mutant_detail] == [
        ("m1", "survived"),
        ("m2", "killed"),
        ("m3", "no tests"),
    ]
    assert {n: t for n, _, t in outcome.mutant_detail}["m2"] == SHOW


def test_tier2_findings_carry_mutant_detail_in_score_mode_and_round_trip(
    tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub(tmp_path, monkeypatch, "  k1: killed\n  s1: survived", SHOW3)
    auditor = Auditor(tree)
    found = auditor.tier2()
    assert [(n, s) for n, s, _ in found.mutant_detail] == [("k1", "killed"), ("s1", "survived")]
    sealed = found.to_dict()["mutant_detail"]
    assert isinstance(sealed, list)
    assert sealed[0] == {"name": "k1", "status": "killed", "show": SHOW3}
    assert Findings.from_dict(found.to_dict()).mutant_detail == found.mutant_detail
    assert auditor.tier1().mutant_detail == ()
    assert "mutant_detail" not in auditor.tier1().to_dict()


def test_tiered_json_leaves_mutant_detail_out(
    tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub(tmp_path, monkeypatch, "  k1: killed\n  s1: survived", SHOW3)
    out, err = io.StringIO(), io.StringIO()
    cli.main(
        ["audit", "--tiered", "--no-cache", "--json", "--repo", str(tree)], stdout=out, stderr=err
    )
    tiers = json.loads(out.getvalue())["tiers"]
    assert tiers[-1]["tier"] == 2
    assert "mutant_detail" not in out.getvalue()


# -- SHORTLIST-3: statically equivalent / text survivors are set aside ---------


def _fixture_details() -> list[SurvivorDetail]:
    source = (FIXTURES / "stockbook_e_t8_s5.py.txt").read_text()
    survivors = json.loads((FIXTURES / "e_t8_s5_survivors.json").read_text())
    return [
        (
            s["name"],
            s["status"],
            "stockbook.py",
            i + 1,
            mutation_text(s["show"]),
            message_only_mutant(s["show"], source, s["name"]),
        )
        for i, s in enumerate(survivors)
    ]


def test_e_t8_s5_is_admitted_once_equivalent_and_text_survivors_are_set_aside() -> None:
    """Known-good: a correct CALIB T8 tree whose only non-message survivors are
    the `_check_int` field-name swap (text) and the quantize `Decimal(1)` ->
    `Decimal(2)` (equivalent) passes, and the finding names both with the rule."""
    details = _fixture_details()
    kinds = {d[0].rsplit("ǁ", 1)[1]: set_aside_kind(d) for d in details if not d[5]}
    assert kinds == {"scale_prices__mutmut_2": "text", "scale_prices__mutmut_26": "equivalent"}
    got = check_mutation_shortlist(_outcome(101, details), 85.0)
    assert got.passed, got.detail
    assert "; 2 set aside by a static rule" in got.detail
    assert (
        "mutant stockbook.xǁInventoryǁscale_prices__mutmut_26 (equivalent: no behaviour can change"
        in got.detail
    )
    assert "(text: only message or argument text changes)" in got.detail


def test_a_behaviour_survivor_beside_set_aside_ones_stays_open() -> None:
    """Known-bad: a comparison-operator survivor is behaviour, never set aside."""
    boundary = ("m.x_f__mutmut_1", "survived", "m.py", 3, "-    if p < 0:\n+    if p <= 0:", False)
    untested = (
        "m.x_f__mutmut_2",
        "no tests",
        "m.py",
        4,
        "-    x = Decimal(1)\n+    x = Decimal(2)",
        False,
    )
    assert set_aside_kind(boundary) is None
    assert set_aside_kind(untested) is None
    got = check_mutation_shortlist(_outcome(10, [*_fixture_details(), boundary, untested]), 85.0)
    assert not got.passed
    assert got.detail.startswith("2 surviving mutant(s)")
    assert "mutant m.x_f__mutmut_1 (survived)" in got.detail


# -- SHORTLIST-4: coverage is a locator (not-proven) under --tier2 shortlist ---


@pytest.fixture
def uncovered(tree: Path) -> Path:
    """`tree` plus an untested changed module: coverage fails in score mode."""
    (tree / "u.py").write_text("def g():\n    return 7\n")
    return tree


def _gate(found: Findings, gate: str) -> str:
    return next(f.verdict for f in found.findings if f.gate == gate)


def test_score_mode_still_fails_coverage_and_blocks_tier2(uncovered: Path) -> None:
    auditor = Auditor(uncovered)
    assert _gate(auditor.tier1(), "coverage") == "fail"
    assert _gate(auditor.tier2(), "mutation") == "blocked"


def test_shortlist_coverage_is_not_proven_and_tier2_admits_when_nothing_survives(
    uncovered: Path,
) -> None:
    """Known-good: coverage fails, every mutant is killed; the audit passes."""
    auditor = Auditor(uncovered, config=SHORTLIST)
    first = auditor.tier1()
    coverage = next(f for f in first.findings if f.gate == "coverage")
    assert coverage.verdict == "not-proven"
    assert "u.py:2" in coverage.detail
    assert first.passed
    second = auditor.tier2()
    assert _gate(second, "mutation") == "pass"
    assert second.passed


def test_shortlist_with_uncovered_lines_still_names_a_behaviour_survivor(
    uncovered: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Known-bad: the survivor is still named (surfaced, SHORTLIST-5)."""
    _stub(tmp_path, monkeypatch, "  k1: killed\n  s1: survived", SHOW3)
    found = Auditor(uncovered, config=SHORTLIST).tier2()
    mutation = next(f for f in found.findings if f.gate == "mutation")
    assert mutation.verdict == "not-proven"  # flip (SHORTLIST-5): was "fail"
    assert "mutant s1 (survived)" in mutation.detail


def test_not_proven_renders_as_not_proven_and_does_not_refuse() -> None:
    from saddle.auditor import Finding
    from saddle.feed import AuditResult, failing, render

    f = Finding("coverage", 1, "not-proven", "evidence-thin", "no test runs u.py:2", ("c",))
    result = AuditResult("finish", "t" * 12, (f,))
    assert not failing(f)
    assert result.passed
    text = render(result)
    assert "(not proven, does not refuse) coverage (tier 1): no test runs u.py:2" in text
    assert "passed or not applicable" not in text


# -- SHORTLIST-5: an open survivor is surfaced (not-proven), never a refusal ----


def test_open_survivors_are_surfaced_with_every_row_and_the_finish_is_accepted(
    tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Known-good: a tree with open survivors passes; known-bad: every
    survivor still reaches the finding, the AuditResult and its render."""
    from saddle.feed import AuditResult, failing, render

    results = "\n".join(f"  s{i}: survived" for i in range(3))
    _stub(tmp_path, monkeypatch, results, SHOW3)
    found = Auditor(tree, config=AuditorConfig(tier2="shortlist", mutant_shortlist=1)).tier2()
    mutation = next(f for f in found.findings if f.gate == "mutation")
    assert mutation.verdict == "not-proven"
    assert not failing(mutation)
    assert found.passed
    assert "mutant s0 (survived)" in mutation.detail
    assert "(and 2 more)" in mutation.detail
    assert [s.name for s in found.survivors] == ["s0", "s1", "s2"]
    assert [n for n, _, _ in found.mutant_detail] == ["s0", "s1", "s2"]
    result = AuditResult("finish", "t" * 12, found.findings, mutant_detail=found.mutant_detail)
    assert result.passed
    assert "(not proven, does not refuse) mutation (tier 2)" in render(result)
    assert "mutant s0 (survived)" in render(result)
    sealed = result.to_dict()["mutant_detail"]
    assert isinstance(sealed, list)
    assert [d["name"] for d in sealed] == ["s0", "s1", "s2"]


def test_score_mode_still_fails_on_the_same_survivors(
    tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub(tmp_path, monkeypatch, "\n".join(f"  s{i}: survived" for i in range(3)), SHOW3)
    found = Auditor(tree).tier2()
    assert next(f.verdict for f in found.findings if f.gate == "mutation") == "fail"
    assert not found.passed


# -- FEEDFIX (9): the packet reads a not-proven finding as not proven ------------


def _mutation_row(tree: Path, tmp_path: Path) -> tuple[str, str]:
    from saddle.packet import compile_packet

    journal = tmp_path / "proofs.jsonl"
    Auditor(tree, config=AuditorConfig(tier2="shortlist", journal=journal)).tier2()
    rows = {r.key: r for r in compile_packet(journal).rows}
    return rows["mutation"].status, rows["audit"].status


def test_a_shortlist_ledger_with_an_open_survivor_packets_as_not_proven(
    tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Known-bad: an open survivor journals exit 0 (`_JOURNAL_EXIT`), and
    the Mutation row must still not say `proven`."""
    _stub(tmp_path, monkeypatch, "  k1: killed\n  s1: survived", SHOW3)
    assert _mutation_row(tree, tmp_path)[0] == "not-proven"


def test_a_shortlist_ledger_with_every_mutant_killed_packets_as_proven(
    tree: Path, tmp_path: Path
) -> None:
    """Known-good: the same ledger shape with no survivor renders proven."""
    assert _mutation_row(tree, tmp_path) == ("proven", "proven")


def test_a_not_proven_coverage_finding_makes_the_audit_row_not_proven(
    uncovered: Path, tmp_path: Path
) -> None:
    from saddle.packet import compile_packet, render_packet_text

    journal = tmp_path / "proofs.jsonl"
    Auditor(uncovered, config=AuditorConfig(tier2="shortlist", journal=journal)).tier1()
    packet = compile_packet(journal)
    audit = next(r for r in packet.rows if r.key == "audit")
    assert audit.status == "not-proven"
    assert audit.text.endswith(", 1 not proven.")
    assert any(item.startswith("? coverage: tier 1, not-proven:") for item in audit.items)
    assert "Audit [not-proven]" in render_packet_text(packet)


def test_a_finished_packet_with_a_not_proven_finding_does_not_say_every_finding_passed(
    uncovered: Path, tmp_path: Path
) -> None:
    from saddle.journal import append_span, build_span
    from saddle.packet import compile_packet

    journal = tmp_path / "proofs.jsonl"
    Auditor(uncovered, config=AuditorConfig(tier2="shortlist", journal=journal)).tier1()
    append_span(
        journal,
        build_span(node_id="chat#1", argv=["auto:finished"], duration_ms=0, exit_code=0,
                   detail="finished: finish called; arm E+A+F", kind="agent"),
    )  # fmt: skip
    packet = compile_packet(journal)
    assert packet.verdict == "finished"
    assert packet.verdict_text == (
        "The executor called finish. No audit finding failed; 1 finding could not be "
        "proven (they do not refuse finish)."
    )
