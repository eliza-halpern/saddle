"""Rule D in the run path (P2-3): known-good and known-bad pairs.

Every store here is a real S1-c store written with `write_reference_set` and
loaded through its sealed id; every tree is real code run in a subprocess.
"""

from __future__ import annotations

import argparse
import io
import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from saddle import cli, rule_d, rule_d_run
from saddle import slice as slice_module
from saddle.answer_book import Book
from saddle.dag import Dag
from saddle.evidence import TEST_MEMORY_LIMIT_BYTES, CapturedRun, run_argv
from saddle.journal import attempt_sidecar_path, read_records, read_spans
from saddle.refstore import AnswerTable, Reference, Stamp, write_reference_set
from saddle.rule_d_run import (
    CensusClass,
    RuleDConfig,
    RuleDDecision,
    RuleDError,
    census,
    load,
    render_census,
    tree_answers,
    verdict,
)
from saddle.slice import NodeQuestionError, run_slice
from saddle.vllm import DiffProposal

IDS = tuple(f"r{i}" for i in range(1, 9))
FILLER = tuple(f"n{i}" for i in range(30))
SPLIT = "a@@b"
FN = "n.f"
T0 = "2026-09-25T00:00:00Z"
STAMP = Stamp(model="m", sampling={}, server_profile=None, drawn="test")
GOOD = 'return "@" in s and "@@" not in s'
WRONG = 'return "@" in s'
BAND = 'return s == "n0" or ("@" in s and "@@" not in s)'


def _rows(*, panel: bool = False, extra: dict[str, object] | None = None) -> bytes:
    table: list[tuple[str, Sequence[object]]] = [
        ("a@b.c", [True] * 8),
        ("nope", [False] * 8),
        *[(x, [False] * 8) for x in FILLER],
        (SPLIT, [True] * 4 + [False] * 4),
    ]
    out = []
    for x, vs in table:
        row: dict[str, object] = {"input": x, "refs": dict(zip(IDS, vs, strict=True))}
        if panel:
            row["panel_vote"] = "T,T" if vs[0] else "F,F"
        if extra and x in extra:
            row["refs"] = {**row["refs"], "r1": extra[x]}  # type: ignore[dict-item]
        out.append(json.dumps(row) + "\n")
    return "".join(out).encode()


def _store(
    root: Path, *, valid: bool = True, panel: bool = False, extra: dict[str, object] | None = None
) -> tuple[Path, str]:
    store = root / "store"
    refs = [Reference(i, "def f(s):\n    return True\n", valid or i != "r8") for i in IDS]
    set_id = write_reference_set(
        store,
        task_id="tX",
        path="n.py",
        function="f",
        stamp=STAMP,
        references=refs,
        answers=[AnswerTable("t", "unit", "0" * 64, _rows(panel=panel, extra=extra))],
    )
    return store, set_id


def _book(path: Path, *, answer: str | None = "reject", panel_sha: str | None = None) -> Path:
    b = Book()
    b.append(
        {
            "kind": "registration",
            "callable": FN,
            "by": "eliza",
            "at": T0,
            "key_fn": "k3@1",
            "either_by": [],
            "either_at": ["plan", "verdict"],
            "panel_id": "p" if panel_sha else None,
            "panel_sha": panel_sha,
        }
    )
    if answer is not None:
        b.append(
            {
                "kind": "exact",
                "callable": FN,
                "input": SPLIT,
                "answer": answer,
                "source": "user",
                "by": "eliza",
                "at": T0,
                "question_hash": "0" * 64,
                "tree_hash": "",
                "ask_point": "plan",
                "run_id": "run-1",
            }
        )
    b.dump(path)
    return path


def _config(tmp_path: Path, *, book: bool = True, **kw: Any) -> RuleDConfig:
    store, set_id = _store(tmp_path)
    return RuleDConfig(
        store, set_id, table="t", book=_book(tmp_path / "book.jsonl") if book else None, **kw
    )


def _tree(root: Path, body: str) -> Path:
    tree = root / "tree"
    tree.mkdir(exist_ok=True)
    (tree / "n.py").write_text(f"def f(s):\n    {body}\n")
    return tree


# ------------------------------------------------------------------ verdict: known-good


def test_rule_d_run_a_correct_tree_with_the_book_is_accepted_and_seals_the_user_cite(
    tmp_path: Path,
) -> None:
    loaded = load(_config(tmp_path))
    d = verdict(loaded, _tree(tmp_path, GOOD))
    assert (d.verdict, d.halt, d.refuse) == ("accept", False, False)
    entry = loaded.book.records[-1]["record_hash"]
    assert d.evidence["cites"] == [f"user:eliza:{entry}"]
    assert d.evidence["provenance"] == {"references": 32, "user": 1}
    assert d.evidence["disagreements"] == []
    assert d.evidence["reference_set_id"] == loaded.refset.set_id
    assert d.evidence["book_head"] == entry
    assert d.evidence["panel_sha"] == loaded.ctx.panel_sha


def test_rule_d_run_a_correct_tree_without_the_book_is_routed_on_the_split_class(
    tmp_path: Path,
) -> None:
    d = verdict(load(_config(tmp_path, book=False)), _tree(tmp_path, GOOD))
    assert (d.verdict, d.halt, d.refuse) == ("route", False, False)
    assert d.evidence["provenance"] == {"references": 32, "split": 1}
    assert len(d.evidence["asks"]) == 1


# ------------------------------------------------------------------ verdict: known-bad


def test_rule_d_run_a_wrong_tree_is_refused_with_the_miss_and_its_cite(tmp_path: Path) -> None:
    loaded = load(_config(tmp_path))
    d = verdict(loaded, _tree(tmp_path, WRONG))
    entry = loaded.book.records[-1]["record_hash"]
    assert (d.verdict, d.halt, d.refuse) == ("refuse", False, True)
    assert d.evidence["disagreements"] == [
        {
            "input": SPLIT,
            "got": "true",
            "expected": "false",
            "source": "user",
            "cite": f"user:eliza:{entry}",
        }
    ]
    assert f"'{SPLIT}': tree true, expected false [user: user:eliza:{entry}]" in d.detail


def test_rule_d_run_a_thin_band_miss_is_a_question_that_halts(tmp_path: Path) -> None:
    d = verdict(load(_config(tmp_path)), _tree(tmp_path, BAND))
    assert (d.verdict, d.halt, d.refuse) == ("question", True, False)
    assert "rule D question: 1 ask(s) (budget 5)" in d.detail
    assert "'n0': tree true, references false" in d.detail
    assert d.evidence["question"] == d.detail
    assert d.evidence["over_budget"] is False


def test_rule_d_run_asks_over_the_verdict_budget_halt_a_routed_tree(tmp_path: Path) -> None:
    d = verdict(load(_config(tmp_path, book=False, verdict_budget=0)), _tree(tmp_path, GOOD))
    assert (d.verdict, d.halt, d.refuse) == ("route", True, False)
    assert "over budget, stopping (no ask dropped)" in d.detail
    assert f"'{SPLIT}': tree false" in d.detail
    assert d.evidence["over_budget"] is True


def test_rule_d_run_an_unusable_tree_is_refused_and_says_why(tmp_path: Path) -> None:
    d = verdict(load(_config(tmp_path)), _tree(tmp_path, "raise ImportError"))
    assert d.verdict == "refuse"
    tree = tmp_path / "tree"
    (tree / "n.py").write_text("import nosuchmodule_xyz\n")
    d = verdict(load(_config(tmp_path / "b")), tree)
    assert d.refuse
    assert d.detail.startswith("rule D refused: tree unusable (import-error:")


# ------------------------------------------------------------------ tree answers


@pytest.mark.parametrize(
    ("source", "want"),
    [
        (None, ("missing",)),
        ("import nosuchmodule_xyz\n", ("import-error",)),
        ("f = 3\n", ("missing",)),
        ("def f():\n    return 1\n", ("arity",)),
        ("f = print\n", "print"),
        ("def f(s):\n    raise KeyError(s)\n", {"a": ("raise", "KeyError")}),
        ("def f(s):\n    return object()\n", {"a": ("opaque", "<class 'object'>")}),
        ("def f(s):\n    return [s]\n", {"a": ("value", '["a"]')}),
    ],
)
def test_rule_d_run_tree_answers_name_each_way_a_tree_can_answer(
    tmp_path: Path, source: str | None, want: object
) -> None:
    if source is not None:
        (tmp_path / "n.py").write_text(source)
    got = tree_answers(tmp_path, "n.py", "f", ["a"])
    if want == "print":
        assert got == {"a": rule_d.Answer("value", "null")}
    elif isinstance(want, tuple):
        assert isinstance(got, rule_d.Unusable)
        assert got.reason == want[0]
    else:
        assert isinstance(want, dict)
        assert got == {k: rule_d.Answer(*v) for k, v in want.items()}


def test_rule_d_run_tree_answers_timeout_and_crash_are_unusable(tmp_path: Path) -> None:
    def timed_out(argv: list[str], cwd: Path, **_kw: Any) -> CapturedRun:
        return CapturedRun(tuple(argv), 124, "", "", timed_out=True)

    def crashed(argv: list[str], cwd: Path, **_kw: Any) -> CapturedRun:
        return CapturedRun(tuple(argv), 137, "", "Killed")

    got = tree_answers(tmp_path, "n.py", "f", ["a"], runner=timed_out)
    assert got == rule_d.Unusable("timeout", "no answers within 120.0 s")
    got = tree_answers(tmp_path, "n.py", "f", ["a"], runner=crashed)
    assert got == rule_d.Unusable("crashed", "exit 137: Killed")


def test_rule_d_run_tree_answers_run_under_the_test_memory_ceiling(tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    def spy(argv: list[str], cwd: Path, **kw: Any) -> CapturedRun:
        seen.update(kw, cwd=cwd)
        return CapturedRun(tuple(argv), 1, "", "")

    tree_answers(tmp_path, "n.py", "f", ["a"], runner=spy)
    assert seen["memory_limit"] == TEST_MEMORY_LIMIT_BYTES
    assert seen["timeout"] == rule_d_run.TREE_TIMEOUT_S
    assert seen["cwd"] == tmp_path


# ------------------------------------------------------------------ load


def test_rule_d_run_load_reads_the_sealed_store_and_refuses_what_it_cannot_use(
    tmp_path: Path,
) -> None:
    loaded = load(_config(tmp_path))
    assert loaded.callable_ == FN
    assert loaded.inputs[0] == "a@b.c"
    assert len(loaded.inputs) == 33
    assert loaded.ctx.refs[SPLIT] == "TTTTFFFF"
    assert loaded.ctx.panel is None
    assert loaded.key("a@b.c") != loaded.key("nope")
    assert loaded.key("nope") == loaded.key("n0")
    assert load(_config(tmp_path / "e", book=False)).key("x") == '["exact","x"]'
    with pytest.raises(RuleDError, match="no answers table 'nope'"):
        load(RuleDConfig(loaded.config.store, loaded.config.set_id, table="nope"))
    store, set_id = _store(tmp_path / "x", extra={"nope": "maybe"})
    with pytest.raises(RuleDError, match="non-boolean reference answer"):
        load(RuleDConfig(store, set_id, table="t"))


def test_rule_d_run_an_invalid_reference_is_unusable_not_an_answer(tmp_path: Path) -> None:
    store, set_id = _store(tmp_path, valid=False)
    loaded = load(RuleDConfig(store, set_id, table="t"))
    assert loaded.references[-1] == ("r8", rule_d.Unusable("import-error", "invalid"))
    assert loaded.ctx.refs[SPLIT] == "TTTTFFF"
    d = verdict(loaded, _tree(tmp_path, GOOD))
    assert d.evidence["references"] == 7
    assert d.verdict == "no-reference"


def test_rule_d_run_the_panel_comes_from_the_table_and_must_match_the_book(
    tmp_path: Path,
) -> None:
    store, set_id = _store(tmp_path, panel=True)
    loaded = load(RuleDConfig(store, set_id, table="t"))
    assert loaded.ctx.panel_label("a@b.c") == "T,T"
    ok = _book(tmp_path / "ok.jsonl", answer=None, panel_sha=loaded.ctx.panel_sha)
    assert load(RuleDConfig(store, set_id, table="t", book=ok)).ctx.panel_sha
    bad = _book(tmp_path / "bad.jsonl", answer=None, panel_sha="f" * 64)
    with pytest.raises(RuleDError, match="registers panel"):
        load(RuleDConfig(store, set_id, table="t", book=bad))
    (tmp_path / "t2").mkdir()
    store2, set_id2 = _store(tmp_path / "t2")
    with pytest.raises(RuleDError, match=r"carries panel \(none\)"):
        load(RuleDConfig(store2, set_id2, table="t", book=bad))


# ------------------------------------------------------------------ census


def test_rule_d_run_census_lists_the_unanswered_split_class_and_the_book_removes_it(
    tmp_path: Path,
) -> None:
    classes = census(load(_config(tmp_path / "a", book=False)))
    assert [c.members for c in classes] == [(SPLIT,)]
    assert classes[0].signatures == ("TTTTFFFF",)
    assert census(load(_config(tmp_path / "b"))) == []


def test_rule_d_run_render_census_shows_every_question_whatever_the_budget() -> None:
    classes = [CensusClass(f"k{i}", (f"x{i}", "y", "z", "w"), ("TF",)) for i in range(4)]
    text = render_census(classes, 1)
    assert text.startswith("rule D census: 4 split class(es) to answer (budget 1)\n")
    assert [line.split()[0] for line in text.splitlines()[1:]] == ["Q1", "Q2", "Q3", "Q4"]
    assert "Q1 k0: 4 input(s), e.g. 'x0', 'y', 'z'; references TF" in text


def test_rule_d_run_over_budget_census_stops_before_any_model_call(tmp_path: Path) -> None:
    config = _config(tmp_path, book=False, census_budget=0)
    calls: list[object] = []

    class NoClient:
        def __getattr__(self, name: str) -> object:
            calls.append(name)
            raise AssertionError(name)

    out = io.StringIO()
    options = cli.RunOptions(task="t", repo=tmp_path, journal=tmp_path / "j", rule_d=config)
    code = cli.run_task(options, NoClient(), stdin=io.StringIO(), stdout=out)  # type: ignore[arg-type]
    assert code == 1
    assert calls == []
    assert "Q1 " in out.getvalue()
    assert "1 census question(s) exceed the budget of 0; stopping" in out.getvalue()


def test_rule_d_run_plan_reports_an_unloadable_store(tmp_path: Path) -> None:
    out = io.StringIO()
    config = RuleDConfig(tmp_path / "none", "0" * 64)
    assert cli.rule_d_plan(config, stdout=out) is None
    assert out.getvalue().startswith("error: rule D: missing ")


# ------------------------------------------------------------------ cli flags


def _args(**kw: Any) -> argparse.Namespace:
    return cli.build_parser().parse_args(["run", *kw.pop("argv", []), "task"])


def test_rule_d_run_flag_off_builds_no_config_and_on_needs_the_store() -> None:
    assert cli.rule_d_config(_args()) is None
    with pytest.raises(cli.RunError, match="--rule-d needs --rule-d-store"):
        cli.rule_d_config(_args(argv=["--rule-d"]))
    config = cli.rule_d_config(
        _args(
            argv=[
                "--rule-d",
                "--rule-d-store",
                "s",
                "--rule-d-set-id",
                "a" * 64,
                "--rule-d-book",
                "b.jsonl",
                "--rule-d-census-budget",
                "7",
                "--rule-d-verdict-budget",
                "2",
            ]
        )
    )
    assert config == RuleDConfig(Path("s"), "a" * 64, "fix8", Path("b.jsonl"), 7, 2)
    assert cli.rule_d_config(
        _args(argv=["--rule-d", "--rule-d-store", "s", "--rule-d-set-id", "a" * 64])
    ) == RuleDConfig(Path("s"), "a" * 64)


def test_rule_d_run_main_reports_a_half_configured_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SADDLE_VLLM_API_KEY", "k1")
    err = io.StringIO()
    assert cli.main(["run", "--repo", str(tmp_path), "--rule-d", "t"], stderr=err) == 1
    assert "error: --rule-d needs --rule-d-store and --rule-d-set-id" in err.getvalue()


# ------------------------------------------------------------------ the run path


def _whole_file(path: str, *lines: str) -> str:
    body = "".join(f"+{line}\n" for line in lines)
    return (
        f"diff --git a/{path} b/{path}\n--- /dev/null\n+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n{body}"
    )


def _repo(root: Path, body: str) -> None:
    """A repo whose test pins what `body` answers on four inputs, so the gates pass."""
    for argv in (
        ["git", "init"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "test"],
    ):
        assert run_argv(argv, root) == 0
    (root / "n.py").write_text("def f(s):\n    return False\n")
    scope: dict[str, Any] = {}
    exec(f"def f(s):\n    {body}\n", scope)
    pins = "".join(
        f"    assert f({x!r}) is {scope['f'](x)}\n" for x in ("a@b.c", "nope", SPLIT, "n0")
    )
    (root / "test_n.py").write_text(f"from n import f\n\n\ndef test_f():  # REQ-001\n{pins}")
    assert run_argv(["git", "add", "n.py", "test_n.py"], root) == 0
    assert run_argv(["git", "commit", "-m", "baseline"], root) == 0


def _dag() -> Dag:
    return Dag.model_validate(
        {
            "nodes": [
                {
                    "id": "n1",
                    "kind": "impl",
                    "dependencies": [],
                    "task_prompt": "Do n1.",
                    "requirements": [
                        {
                            "id": "REQ-001",
                            "statement": "REQ-001 holds.",
                            "accepts": ["2"],
                            "rejects": ["3"],
                        }
                    ],
                    "execution_constraints": {
                        "reasoning_budget": "low",
                        "allowed_tools": ["read_file", "write_file", "run_tests", "lint"],
                        "max_context_tokens": 8000,
                    },
                    "deterministic_gate": {
                        "test_command": "pytest test_n.py",
                        "changed_line_coverage_min": 100.0,
                        "red_phase_required": True,
                        "mutation_sample": {
                            "scope": "changed-lines",
                            "max_mutants": 100,
                            "kill_threshold": 85.0,
                        },
                    },
                }
            ]
        }
    )


def _mutmut(root: Path, body: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """conftest's PATH stub, re-pointed at this tree's changed line (n.py:2)."""
    stub = root / "mutmut-stub"
    stub.mkdir()
    (stub / "show.txt").write_text(f"--- n.py\n+++ n.py\n@@ -2 +2 @@\n-    {body}\n+    pass\n")
    results = "\n".join(f"  m{index}: killed" for index in range(1, 6))
    script = stub / "mutmut"
    script.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  run) exit 0;;\n"
        f"  results) printf '%s\\n' '{results}';;\n"
        f"  show) cat '{stub / 'show.txt'}';;\n"
        "esac\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{stub}:{os.environ['PATH']}")


def _slice(
    tmp_path: Path,
    body: str,
    check: slice_module.RuleDCheck | None,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    _mutmut(tmp_path, body, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    _repo(repo, body)
    journal = repo / "proofs.jsonl"
    diff = _whole_file("n.py", "def f(s):", f"    {body}")
    result = run_slice(
        "Fix f.",
        _dag(),
        workdir=repo,
        journal_path=journal,
        propose=lambda node, failure, seed: DiffProposal(diff, ""),
        merge_command=None,
        rule_d=check,
    )
    sidecars = [
        json.loads(attempt_sidecar_path(journal, s.span_id).read_text())
        for s in read_spans(journal)
        if s.name == "worker:n1"
    ]
    return result, sidecars, repo


def test_rule_d_run_the_run_path_seals_an_accept_into_the_proof_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded = load(_config(tmp_path))
    result, sidecars, _repo_dir = _slice(tmp_path, GOOD, rule_d_run.hook(loaded), monkeypatch)
    assert result.passed is True
    assert sidecars[-1]["exit_code"] == 0
    assert sidecars[-1]["rule_d"]["verdict"] == "accept"
    assert sidecars[-1]["rule_d"]["cites"] == [f"user:eliza:{loaded.book.head()}"]


def test_rule_d_run_the_run_path_fails_a_refused_tree_with_the_miss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, sidecars, _repo_dir = _slice(
        tmp_path, WRONG, rule_d_run.hook(load(_config(tmp_path))), monkeypatch
    )
    assert result.passed is False
    assert "- Gate rule-d: FAIL (rule D refused" in result.transcript
    first = sidecars[0]
    assert first["exit_code"] == 1
    assert "rule-d" in first["detail"]
    assert first["rule_d"]["disagreements"][0]["input"] == SPLIT
    assert first["gates"][-1]["name"] == "rule-d"


@pytest.mark.parametrize("retry", [False, True])
def test_rule_d_run_a_refusal_fails_the_node_once_unless_retry_is_asked_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retry: bool
) -> None:
    """Default: one verdict per tree, no repair. `--rule-d-retry`: a repair carrying the misses."""
    _mutmut(tmp_path, WRONG, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    _repo(repo, WRONG)
    journal = repo / "proofs.jsonl"
    diffs = [
        _whole_file("n.py", "def f(s):", f"    {WRONG}"),
        _whole_file("n.py", "def f(s):", "    return '@' in s"),
    ]
    failures: list[str | None] = []

    def propose(node: object, failure: str | None, seed: int) -> DiffProposal:
        failures.append(failure)
        return DiffProposal(diffs[0] if failure is None else diffs[1], "")

    config = _config(tmp_path, retry=retry)
    result = run_slice(
        "Fix f.",
        _dag(),
        workdir=repo,
        journal_path=journal,
        propose=propose,
        merge_command=None,
        rule_d=rule_d_run.hook(load(config)),
    )
    attempts = [s for s in read_spans(journal) if s.name == "worker:n1"]
    sealed = json.loads(attempt_sidecar_path(journal, attempts[0].span_id).read_text())
    assert result.passed is False
    assert sealed["rule_d"]["retry"] is retry
    assert sealed["rule_d"]["panel_sha_definition"] == rule_d_run.PANEL_SHA_DEFINITION
    repairs = [f for f in failures if f is not None]
    if retry:
        assert len(attempts) >= 2
        assert repairs
        assert f"'{SPLIT}': tree true, expected false" in repairs[0]
    else:
        assert len(attempts) == 1
        assert repairs == []


def test_rule_d_run_the_run_path_halts_a_question_without_failing_the_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, sidecars, repo = _slice(
        tmp_path, BAND, rule_d_run.hook(load(_config(tmp_path))), monkeypatch
    )
    assert result.passed is False
    assert len(sidecars) == 1
    assert sidecars[0]["exit_code"] == 2
    assert sidecars[0]["detail"] == "attempt 1/3: halted on a rule D question"
    assert "'n0': tree true, references false" in sidecars[0]["rule_d"]["question"]
    assert "- Gate rule-d: FAIL (rule D question: 1 ask(s)" in result.transcript
    assert read_records(repo / "proofs.jsonl") == []
    assert (repo / "n.py").read_text() == "def f(s):\n    return False\n"


def test_rule_d_run_the_hook_skips_test_nodes_and_trees_without_the_module(tmp_path: Path) -> None:
    check = rule_d_run.hook(load(_config(tmp_path)))
    tree = _tree(tmp_path, GOOD)
    assert check("test", tree) is None
    assert check("impl", tmp_path) is None
    decision = check("impl", tree)
    assert decision is not None
    assert decision.verdict == "accept"


def test_rule_d_run_flag_off_never_calls_rule_d(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Known-bad for the opt-in: with no hook nothing reaches rule D (spied at the source)."""
    calls: list[str] = []

    def spy(name: str) -> Any:
        def record(*_a: object, **_k: object) -> None:
            calls.append(name)
            raise AssertionError(name)

        return record

    monkeypatch.setattr(rule_d, "consensus_verdict", spy("consensus_verdict"))
    monkeypatch.setattr(rule_d_run, "tree_answers", spy("tree_answers"))
    monkeypatch.setattr(cli, "rule_d_plan", spy("rule_d_plan"))
    result, sidecars, _repo_dir = _slice(tmp_path, GOOD, None, monkeypatch)
    assert result.passed is True
    assert "rule_d" not in sidecars[-1]
    assert calls == []


def test_rule_d_run_node_question_error_carries_the_question() -> None:
    exc = NodeQuestionError("n1", "Q?", 2)
    assert (exc.node_id, exc.question, exc.attempts) == ("n1", "Q?", 2)
    assert str(exc) == "node 'n1' halted on a rule D question:\nQ?"
    assert RuleDDecision("accept", False, False, "x").evidence == {}


def test_rule_d_run_saddle_run_hands_no_hook_when_the_flag_is_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Known-bad for the opt-in at the `saddle run` seam: off means no load, no census, no hook."""
    _repo(tmp_path, GOOD)
    handed: list[object] = []
    loads: list[object] = []

    def fake_slice(*_a: object, **kw: Any) -> slice_module.SliceResult:
        handed.append(kw["rule_d"])
        return slice_module.SliceResult(passed=True, transcript="", proofs={})

    monkeypatch.setattr(cli, "_emit_valid_dag", lambda *_a, **_k: _dag())
    monkeypatch.setattr(cli, "run_slice", fake_slice)

    def counted_load(config: RuleDConfig) -> rule_d_run.Loaded:
        loads.append(config)
        return load(config)

    monkeypatch.setattr(cli, "rule_d_load", counted_load)
    off = cli.RunOptions(task="t", repo=tmp_path, journal=tmp_path / "j", yes=True)
    assert cli.run_task(off, None, stdin=io.StringIO(), stdout=io.StringIO()) == 0  # type: ignore[arg-type]
    assert handed == [None]
    assert loads == []
    on = cli.RunOptions(
        task="t", repo=tmp_path, journal=tmp_path / "j", yes=True, rule_d=_config(tmp_path)
    )
    assert cli.run_task(on, None, stdin=io.StringIO(), stdout=io.StringIO()) == 0  # type: ignore[arg-type]
    assert callable(handed[-1])
    assert len(loads) == 1


# ---- run-level QUESTION verdict and exit code (HALT) ----


def _run_span(journal: Path) -> Any:
    return [s for s in read_spans(journal) if s.name == "run"][-1]


def test_rule_d_run_a_question_halt_seals_the_run_as_question_with_exit_4(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Known-good: a run whose only failure is a rule D halt is QUESTION, not FAIL."""
    result, _sidecars, repo = _slice(
        tmp_path, BAND, rule_d_run.hook(load(_config(tmp_path))), monkeypatch
    )
    assert (result.passed, result.question) == (False, True)
    assert "- Verdict: QUESTION\n" in result.transcript
    run = _run_span(repo / "proofs.jsonl")
    assert run.exit_code == slice_module.QUESTION_EXIT == 4
    assert run.detail == "0 proven, 0 failed, 1 halted on a question, 0 undispatched"


@pytest.mark.parametrize(
    ("body", "passed", "verdict", "code"), [(WRONG, False, "FAIL", 1), (GOOD, True, "PASS", 0)]
)
def test_rule_d_run_a_refusal_stays_fail_1_and_an_accept_stays_pass_0(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    body: str,
    passed: bool,
    verdict: str,
    code: int,
) -> None:
    """Known-bad: a refused tree (a gate failure) is not a question; a pass is unchanged."""
    result, _sidecars, repo = _slice(
        tmp_path, body, rule_d_run.hook(load(_config(tmp_path))), monkeypatch
    )
    assert (result.passed, result.question) == (passed, False)
    assert f"- Verdict: {verdict}\n" in result.transcript
    assert _run_span(repo / "proofs.jsonl").exit_code == code


def _two_nodes() -> Dag:
    node = _dag().nodes[0]
    return Dag(nodes=[node, node.model_copy(update={"id": "n2"})])


@pytest.mark.parametrize(
    ("failures", "merge_exit", "deadline", "verdict", "code"),
    [
        ({"n1": "question"}, 0, False, "QUESTION", 4),
        ({"n1": "question", "n2": "question"}, 0, False, "QUESTION", 4),
        ({"n1": "question", "n2": "gate"}, 0, False, "FAIL", 1),
        ({"n1": "question"}, 1, False, "FAIL", 1),
        ({"n1": "question"}, 0, True, "FAIL", 3),
        ({"n1": "gate"}, 0, False, "FAIL", 1),
    ],
)
def test_rule_d_run_question_is_the_verdict_only_when_every_failure_is_a_question(
    tmp_path: Path,
    failures: dict[str, str],
    merge_exit: int,
    deadline: bool,
    verdict: str,
    code: int,
) -> None:
    """A gate failure beside the question, a red merge suite, or a deadline keeps its own code."""
    journal = tmp_path / "proofs.jsonl"
    ever_failed: dict[str, BaseException] = {
        node: NodeQuestionError(node, "Q?")
        if kind == "question"
        else slice_module.NodeUnappliableError(node, "no diff")
        for node, kind in failures.items()
    }
    result = slice_module._seal_run(
        _two_nodes(),
        journal_path=journal,
        proofs={},
        ever_failed=ever_failed,
        replanned_from=set(),
        run_span_id="r" * 16,
        run_start=0.0,
        merge_exit=merge_exit,
        merge_ran=merge_exit != 0,
        task="t",
        started="s",
        now=lambda: "f",
        deadline_hit=deadline,
    )
    assert result.question is (verdict == "QUESTION")
    assert result.passed is False
    assert f"- Verdict: {verdict}\n" in result.transcript
    assert _run_span(journal).exit_code == code


@pytest.mark.parametrize(
    ("result", "code"),
    [
        (slice_module.SliceResult(passed=False, transcript="", proofs={}, question=True), 4),
        (slice_module.SliceResult(passed=False, transcript="", proofs={}), 1),
        (slice_module.SliceResult(passed=True, transcript="", proofs={}), 0),
    ],
)
def test_rule_d_run_saddle_run_exits_4_on_a_question_and_keeps_1_and_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, result: Any, code: int
) -> None:
    """The process exit code of `saddle run` follows the sealed verdict."""
    _repo(tmp_path, GOOD)
    monkeypatch.setattr(cli, "_emit_valid_dag", lambda *_a, **_k: _dag())
    monkeypatch.setattr(cli, "run_slice", lambda *_a, **_k: result)
    options = cli.RunOptions(task="t", repo=tmp_path, journal=tmp_path / "j", yes=True)
    assert cli.run_task(options, None, stdin=io.StringIO(), stdout=io.StringIO()) == code  # type: ignore[arg-type]
