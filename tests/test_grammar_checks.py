"""The grammar checkers in `tools/`: their corpus builders and the runner.

The grammars themselves are enforced by xgrammar, which lives in the serving
container, so nothing here can say what a grammar admits. What these tests
hold is the harness around it: the encoders that turn real file content into
admit cases, the corpus builder that reads git, and the accounting in `run`
that turns "admitted / refused / may stop" answers into a verdict.

`run` is exercised against a fake `xgrammar` whose "grammar" is an explicit
finite language, so each branch of the accounting can be driven by a known
answer. The fake says nothing about the real grammars; `test_vllm.py` and
`test_edits.py` pin the corpus, and the container run checks the grammar.
"""

from __future__ import annotations

import json
import runpy
import subprocess
import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from saddle.edits import EDIT_GRAMMAR, Edit, parse_edits
from saddle.slice import whole_file_reconstruction
from saddle.vllm import DIFF_GRAMMAR
from tools import diff_grammar_check, edit_grammar_check

SHARED_CASES_PATH = diff_grammar_check.CASES_PATH

# Content that stresses an encoder: quotes, backslashes, a regex, a blank
# line in the middle, a leading `-` and `+`, non-ASCII text.
AWKWARD = 'say("hi")\n\nre = r"\\d+"\n-x\n+y\ncafé\n'


def _git(repo: Path, *argv: str) -> str:
    done = subprocess.run(["git", *argv], cwd=repo, capture_output=True, text=True, check=True)
    return done.stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """A repository of three commits; returns it and each commit's sha by label.

    - `one` writes `a.py`, `blank.py` (first line blank), `empty.py` (no bytes)
    - `two` writes `c.py` with exactly `a.py`'s content (the same blob) and a
      large `big.py`
    - `three` deletes `blank.py`, so `git show --name-only` names a path that
      has no blob at that commit
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    _git(repo, "config", "commit.gpgsign", "false")
    shas: dict[str, str] = {}

    def commit(label: str, files: dict[str, str], delete: tuple[str, ...] = ()) -> None:
        for name, text in files.items():
            (repo / name).write_text(text)
        for name in delete:
            (repo / name).unlink()
        _git(repo, "add", "--all")
        _git(repo, "commit", "--no-verify", "-m", label)
        shas[label] = _git(repo, "rev-parse", "HEAD")

    commit("one", {"a.py": "x = 1\n", "blank.py": "\nx = 2\n", "empty.py": ""})
    commit("two", {"c.py": "x = 1\n", "big.py": "y = 2\n" * 40})
    commit("three", {}, delete=("blank.py",))
    return repo, shas


# ---------------------------------------------------------------- encoders


@pytest.mark.parametrize(
    ("content", "decoded"),
    [
        ("x = 1\n", "x = 1\n"),
        ("x = 1\ny = 2\n", "x = 1\ny = 2\n"),
        (AWKWARD, AWKWARD),
        ("a\n\n\nb\n", "a\n\n\nb\n"),
        # The decoder gives every line its newline and ignores the
        # `\ No newline` marker, so a file without a final newline comes back
        # with one: the inverse holds up to that byte.
        ("no newline at end", "no newline at end\n"),
    ],
)
def test_a_write_section_decodes_back_to_the_content_it_was_built_from(
    content: str, decoded: str
) -> None:
    """`write_section` is the inverse of `slice.whole_file_reconstruction`.

    The corpus is only worth admitting if the encoder spells a file the way
    the run's own decoder reads it back; a drifting encoder would measure
    the grammar against a shape no worker emits.
    """
    section = diff_grammar_check.write_section("pkg/n.py", content)

    assert whole_file_reconstruction(section) == {"pkg/n.py": decoded}


def test_a_write_section_marks_a_file_that_does_not_end_in_a_newline() -> None:
    """A final line without its newline carries the marker; a complete one does not.

    The pair is the instance of the rule: dropping the marker would make the
    two files indistinguishable, which is the information a diff exists to hold.
    """
    whole = diff_grammar_check.write_section("n.py", "x = 1\n")
    cut = diff_grammar_check.write_section("n.py", "x = 1")

    assert whole == "diff --git a/n.py b/n.py\n--- /dev/null\n+++ b/n.py\n@@ -0,0 +1,1 @@\n+x = 1\n"
    assert cut == whole + "\\ No newline at end of file\n"


@pytest.mark.parametrize(
    "content",
    ["x = 1\n", "a = 1\nb = 2\n", AWKWARD.strip("\n") + "\n"],
)
def test_an_edit_section_replaces_the_whole_file_by_itself(content: str) -> None:
    """`edit_section` names the whole file as the search and gives it back as the replacement.

    Read back through `parse_edits`, the applier's own parser, so the corpus
    is the shape the applier acts on.
    """
    section = edit_grammar_check.edit_section("pkg/n.py", content)

    assert parse_edits(section) == [Edit("edit", "pkg/n.py", content, content)]


def test_an_edit_section_names_a_last_line_without_its_newline_as_a_whole_line() -> None:
    """The block format has no marker for a missing final newline: both spellings agree."""
    assert edit_grammar_check.edit_section("n.py", "a = 1\nb = 2") == (
        edit_grammar_check.edit_section("n.py", "a = 1\nb = 2\n")
    )


@pytest.mark.parametrize("content", ["\nx = 1\n", "x = 1\n\n", "", "\n", "  \nx\n"])
def test_an_edit_section_refuses_a_file_it_cannot_name(content: str) -> None:
    """A blank first or last line is unrepresentable, so the section is empty.

    The grammar refuses a block that starts or ends on a blank, so emitting
    one as an admit case would report the grammar broken when the corpus is.
    """
    assert edit_grammar_check.edit_section("n.py", content) == ""


# ---------------------------------------------------------------- the corpus


def test_the_diff_corpus_holds_each_distinct_blob_the_history_wrote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, shas = _repo(tmp_path)
    monkeypatch.setattr(diff_grammar_check, "REPO", repo)

    cases = diff_grammar_check.build_cases(commits=10, max_bytes=100)

    added = {k: v for k, v in cases["admit"].items() if k not in diff_grammar_check.SYNTHETIC}
    # `c.py` is the newer writer of the blob `a.py` also holds, so only it
    # is kept; `empty.py` has no content, `blank.py` has no blob at the
    # newest commit that names it, and `big.py` does not fit the budget.
    assert added == {
        f"{shas['two'][:8]}:c.py": diff_grammar_check.write_section("c.py", "x = 1\n"),
        f"{shas['one'][:8]}:blank.py": diff_grammar_check.write_section("blank.py", "\nx = 2\n"),
    }
    assert set(diff_grammar_check.SYNTHETIC) <= set(cases["admit"])
    assert cases["grammar"] == DIFF_GRAMMAR
    assert cases["reject"] == diff_grammar_check.MUST_REJECT
    assert cases["reject_at"] == diff_grammar_check.REJECT_AT
    assert cases["not_stop"] == diff_grammar_check.MUST_NOT_STOP
    assert cases["stop"] == diff_grammar_check.MUST_STOP


def test_the_diff_corpus_reads_only_as_many_commits_as_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`emit(commits)` reaches `build_cases`: one commit means only the newest one's files."""
    repo, shas = _repo(tmp_path)
    monkeypatch.setattr(diff_grammar_check, "REPO", repo)

    diff_grammar_check.emit(commits=1)

    written = json.loads(capsys.readouterr().out)
    # The newest commit only deleted a file, which has no blob to admit.
    assert set(written["admit"]) == set(diff_grammar_check.SYNTHETIC)
    diff_grammar_check.emit(commits=2)
    wider = json.loads(capsys.readouterr().out)
    assert f"{shas['two'][:8]}:c.py" in wider["admit"]


def test_the_edit_corpus_skips_what_it_cannot_name_and_says_how_many(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, shas = _repo(tmp_path)
    monkeypatch.setattr(edit_grammar_check, "REPO", repo)

    cases = edit_grammar_check.build_cases(commits=10, max_bytes=100)

    added = {k: v for k, v in cases["admit"].items() if k not in edit_grammar_check.SYNTHETIC}
    # `blank.py` begins on a blank line: unrepresentable, counted, not admitted.
    assert added == {
        f"{shas['two'][:8]}:c.py": edit_grammar_check.edit_section("c.py", "x = 1\n"),
    }
    assert capsys.readouterr().err.strip() == (
        f"{len(cases['admit'])} admit cases, 1 files skipped (blank first/last line)"
    )
    assert cases["grammar"] == EDIT_GRAMMAR
    assert cases["reject"] == edit_grammar_check.MUST_REJECT
    assert cases["reject_at"] == edit_grammar_check.REJECT_AT
    assert cases["not_stop"] == edit_grammar_check.MUST_NOT_STOP
    assert cases["stop"] == edit_grammar_check.MUST_STOP


def test_the_edit_corpus_emits_the_same_json_it_builds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, shas = _repo(tmp_path)
    monkeypatch.setattr(edit_grammar_check, "REPO", repo)

    edit_grammar_check.emit(commits=2)

    written = json.loads(capsys.readouterr().out)
    assert f"{shas['two'][:8]}:c.py" in written["admit"]
    assert "grammar" in written


# ---------------------------------------------------------------- corpus accounting


Module = types.ModuleType
BOTH = pytest.mark.parametrize(
    "tool", [diff_grammar_check, edit_grammar_check], ids=["diff", "edit"]
)


def _commit_files(tmp_path: Path, files: dict[str, str]) -> tuple[Path, str]:
    """A repository of one commit holding `files`; returns it and the commit's sha."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    for name, text in files.items():
        (repo / name).write_text(text)
    _git(repo, "add", "--all")
    _git(repo, "commit", "--no-verify", "-m", "one")
    return repo, _git(repo, "rev-parse", "HEAD")


def _admitted(tool: Module, cases: dict[str, Any]) -> list[str]:
    """The corpus entries built from git history, by `sha:name`, in order."""
    return [key for key in cases["admit"] if key not in tool.SYNTHETIC]


@BOTH
def test_a_file_that_overflows_the_budget_does_not_stop_the_smaller_ones_after_it(
    tool: Module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`a_big.py` does not fit, `b_small.py` does: the walk skips one file, not the rest."""
    repo, sha = _commit_files(tmp_path, {"a_big.py": "x" * 60 + "\n", "b_small.py": "y = 1\n"})
    monkeypatch.setattr(tool, "REPO", repo)

    cases = tool.build_cases(commits=5, max_bytes=50)

    assert _admitted(tool, cases) == [f"{sha[:8]}:b_small.py"]


@BOTH
def test_the_budget_counts_what_is_admitted_and_admits_up_to_it_inclusive(
    tool: Module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three 10-byte files against a 20-byte budget: two fit exactly, the third does not.

    A running total that resets or goes down would let the third in; a
    budget that excludes its own limit would keep only one.
    """
    names = ("a.py", "b.py", "c.py")
    repo, sha = _commit_files(tmp_path, {n: f"{n[0] * 9}\n" for n in names})
    monkeypatch.setattr(tool, "REPO", repo)

    cases = tool.build_cases(commits=5, max_bytes=20)

    assert _admitted(tool, cases) == [f"{sha[:8]}:a.py", f"{sha[:8]}:b.py"]


@BOTH
def test_a_path_with_a_space_in_it_is_one_path(
    tool: Module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, sha = _commit_files(tmp_path, {"two words.py": "x = 1\n"})
    monkeypatch.setattr(tool, "REPO", repo)

    cases = tool.build_cases(commits=5)

    assert _admitted(tool, cases) == [f"{sha[:8]}:two words.py"]


def test_every_file_the_edit_corpus_cannot_name_is_counted_and_the_rest_still_admitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    files = {"a.py": "\nx\n", "b.py": "\ny\n", "c.py": "z = 1\n"}
    repo, sha = _commit_files(tmp_path, files)
    monkeypatch.setattr(edit_grammar_check, "REPO", repo)

    cases = edit_grammar_check.build_cases(commits=5)

    assert _admitted(edit_grammar_check, cases) == [f"{sha[:8]}:c.py"]
    assert "2 files skipped" in capsys.readouterr().err


@BOTH
def test_the_default_corpus_reads_the_last_forty_commits(
    tool: Module,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`build_cases()` and `emit()` both default to 40 commits, not 41 and not all."""
    repo, _ = _commit_files(tmp_path, {"f0.py": "v = 0\n"})
    for number in range(1, 41):
        (repo / f"f{number}.py").write_text(f"v = {number}\n")
        _git(repo, "add", "--all")
        _git(repo, "commit", "--no-verify", "-m", f"c{number}")
    monkeypatch.setattr(tool, "REPO", repo)

    assert len(_admitted(tool, tool.build_cases())) == 40

    tool.emit()
    written = json.loads(capsys.readouterr().out)
    assert len([k for k in written["admit"] if k not in tool.SYNTHETIC]) == 40
    assert not any(key.endswith(":f0.py") for key in written["admit"])


@BOTH
def test_the_default_budget_refuses_a_file_one_byte_over_a_million_and_a_half(
    tool: Module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _ = _commit_files(tmp_path, {"huge.py": "x" * 1_500_000 + "\n"})
    monkeypatch.setattr(tool, "REPO", repo)

    assert _admitted(tool, tool.build_cases(commits=1)) == []
    assert len(_admitted(tool, tool.build_cases(commits=1, max_bytes=1_500_001))) == 1


@BOTH
def test_the_grammar_comes_from_the_checkout_being_emitted_not_from_whatever_is_installed(
    tool: Module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--emit` runs in the checkout; its `src/` goes first on the import path.

    A stand-in `saddle` in a scratch checkout holds a different grammar than
    the installed one; the corpus must carry the scratch one. The path is
    reduced to the installed `src/` so that only the position matters.
    """
    checkout = tmp_path / "checkout"
    (checkout / "src" / "saddle").mkdir(parents=True)
    (checkout / "src" / "saddle" / "__init__.py").write_text("")
    (checkout / "src" / "saddle" / "vllm.py").write_text('DIFF_GRAMMAR = "scratch-diff"\n')
    (checkout / "src" / "saddle" / "edits.py").write_text('EDIT_GRAMMAR = "scratch-edit"\n')
    installed_src = str(Path(__file__).resolve().parents[1] / "src")
    before = dict(sys.modules)
    monkeypatch.setattr(tool, "REPO", checkout)
    monkeypatch.setattr(sys, "path", [installed_src])
    for name in [m for m in sys.modules if m == "saddle" or m.startswith("saddle.")]:
        del sys.modules[name]
    try:
        grammar = tool._repo_grammar()
    finally:
        for name in set(sys.modules) - set(before):
            del sys.modules[name]
        sys.modules.update(before)

    assert (
        grammar
        == {"diff_grammar_check": "scratch-diff", "edit_grammar_check": "scratch-edit"}[
            tool.__name__.rsplit(".", 1)[-1]
        ]
    )


# ---------------------------------------------------------------- run()


class _Language:
    """A finite language standing in for a compiled grammar.

    `legal` holds the strings whose prefixes the matcher accepts; `ends`
    holds those after which the stop token is accepted.
    """

    def __init__(self, legal: list[bytes], ends: list[bytes], stop_id: int) -> None:
        self.legal = legal
        self.ends = set(ends)
        self.stop_id = stop_id


class _Matcher:
    def __init__(self, language: _Language) -> None:
        self.language = language
        self.seen = b""

    def accept_string(self, data: bytes) -> bool:
        candidate = self.seen + data
        if any(string.startswith(candidate) for string in self.language.legal):
            self.seen = candidate
            return True
        return False

    def accept_token(self, token_id: int) -> bool:
        return token_id == self.language.stop_id and self.seen in self.language.ends


def _fake_xgrammar(record: dict[str, Any]) -> types.ModuleType:
    module = types.ModuleType("xgrammar")

    class VocabType:
        RAW = "raw"

    class TokenizerInfo:
        def __init__(self, vocab: list[str], vocab_type: str, stop_token_ids: list[int]) -> None:
            record.update(vocab=vocab, vocab_type=vocab_type, stop_token_ids=stop_token_ids)
            self.stop_id = stop_token_ids[0]

    class Grammar:
        def __init__(self, text: str) -> None:
            self.text = text

        @staticmethod
        def from_ebnf(text: str) -> Grammar:
            return Grammar(text)

    class GrammarCompiler:
        def __init__(self, info: TokenizerInfo) -> None:
            self.info = info

        def compile_grammar(self, grammar: Grammar) -> _Language:
            spec = json.loads(grammar.text)
            return _Language(
                [text.encode() for text in spec["legal"]],
                [text.encode() for text in spec["ends"]],
                self.info.stop_id,
            )

    module.VocabType = VocabType  # type: ignore[attr-defined]
    module.TokenizerInfo = TokenizerInfo  # type: ignore[attr-defined]
    module.Grammar = Grammar  # type: ignore[attr-defined]
    module.GrammarCompiler = GrammarCompiler  # type: ignore[attr-defined]
    module.GrammarMatcher = _Matcher  # type: ignore[attr-defined]
    return module


def _corpus() -> dict[str, Any]:
    """The diff tool's own corpus, minus the git history, with a faithful language.

    Faithful means: every admit, stop and not-stop string is legal; a reject
    string is legal exactly up to its `reject_at` byte; only the stop
    strings may end. Built from the corpus itself, so each case has the
    answer the corpus says it should.
    """
    tool = diff_grammar_check
    admit = dict(tool.SYNTHETIC)
    legal = [*admit.values(), *tool.MUST_STOP.values(), *tool.MUST_NOT_STOP.values()]
    for name, text in tool.MUST_REJECT.items():
        legal.append(text[: tool.REJECT_AT.get(name, 0)])
    return {
        "admit": admit,
        "reject": dict(tool.MUST_REJECT),
        "reject_at": dict(tool.REJECT_AT),
        "not_stop": dict(tool.MUST_NOT_STOP),
        "stop": dict(tool.MUST_STOP),
        "legal": legal,
        "ends": list(tool.MUST_STOP.values()),
    }


@pytest.fixture
def cases_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[dict[str, Any]], Path]:
    """Install a fake xgrammar and a writer that puts a cases file where `run` reads it.

    `run` reads the tools' fixed `/tmp/cases.json`; both tools' reads of
    that name are redirected, so no test touches the shared path.
    """
    record: dict[str, Any] = {}
    monkeypatch.setitem(sys.modules, "xgrammar", _fake_xgrammar(record))
    target = tmp_path / "cases.json"
    real_read, real_exists = Path.read_text, Path.exists

    def read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        return real_read(target if str(self) == SHARED_CASES_PATH else self, *args, **kwargs)

    def exists(self: Path, *args: Any, **kwargs: Any) -> bool:
        return real_exists(target if str(self) == SHARED_CASES_PATH else self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(Path, "exists", exists)

    def write(corpus: dict[str, Any]) -> Path:
        language = {"legal": corpus.pop("legal"), "ends": corpus.pop("ends")}
        target.write_text(json.dumps({**corpus, "grammar": json.dumps(language)}))
        return target

    write.record = record  # type: ignore[attr-defined]
    return write


def _verdict(capsys: pytest.CaptureFixture[str]) -> list[str]:
    return capsys.readouterr().out.splitlines()


def test_run_passes_a_corpus_the_grammar_answers_correctly(
    cases_file: Callable[[dict[str, Any]], Path], capsys: pytest.CaptureFixture[str]
) -> None:
    corpus = _corpus()
    total = sum(len(corpus[k]) for k in ("admit", "reject", "not_stop", "stop"))
    cases_file(corpus)

    assert diff_grammar_check.run() == 0

    assert _verdict(capsys) == [f"{total}/{total} cases correct"]


def test_run_asks_the_engine_for_a_byte_vocabulary_with_a_stop_token(
    cases_file: Callable[[dict[str, Any]], Path],
) -> None:
    """The may-stop check needs `<eos>` as a real token, so it is part of the call."""
    cases_file(_corpus())

    diff_grammar_check.run()

    record: dict[str, Any] = cases_file.record  # type: ignore[attr-defined]
    assert record["vocab"][:3] == ["\x00", "\x01", "\x02"]
    assert record["vocab"][255] == "\xff"
    assert record["vocab"][256:] == ["<eos>"]
    assert record["stop_token_ids"] == [256]
    assert record["vocab_type"] == "raw"


def test_run_refuses_a_cases_file_that_carries_no_grammar(
    tmp_path: Path, cases_file: Callable[[dict[str, Any]], Path], capsys: pytest.CaptureFixture[str]
) -> None:
    path = cases_file(_corpus())
    path.write_text(json.dumps({"admit": {}, "reject": {}, "not_stop": {}, "stop": {}}))

    assert diff_grammar_check.run() == 2

    assert capsys.readouterr().out == (
        "cases file carries no grammar: re-run --emit from the repo checkout\n"
    )


def _without(items: list[str], gone: str) -> list[str]:
    assert gone in items, "the corpus no longer carries the case this test breaks"
    return [item for item in items if item != gone]


def _tiny(**halves: Any) -> dict[str, Any]:
    """A corpus of a few short strings, for cases the real corpus' shared prefixes would blur."""
    empty: dict[str, Any] = {"admit": {}, "reject": {}, "not_stop": {}, "stop": {}}
    return {**empty, "reject_at": {}, **halves}


def test_run_reports_an_admit_case_the_grammar_refuses(
    cases_file: Callable[[dict[str, Any]], Path], capsys: pytest.CaptureFixture[str]
) -> None:
    cases_file(_tiny(admit={"wanted": "abcd"}, legal=["abx"], ends=[]))

    assert diff_grammar_check.run() == 1

    assert _verdict(capsys) == ["0/1 cases correct", "  MUST ADMIT but rejected at byte 2: wanted"]


def test_run_reports_a_reject_case_the_grammar_admits(
    cases_file: Callable[[dict[str, Any]], Path], capsys: pytest.CaptureFixture[str]
) -> None:
    corpus = _corpus()
    corpus["legal"].append(corpus["reject"]["prose"])
    cases_file(corpus)

    assert diff_grammar_check.run() == 1

    assert "  MUST REJECT but admitted: prose" in _verdict(capsys)


def test_run_reports_a_reject_case_that_dies_at_the_wrong_byte(
    cases_file: Callable[[dict[str, Any]], Path], capsys: pytest.CaptureFixture[str]
) -> None:
    corpus = _corpus()
    text = corpus["reject"]["anchor past line 1"]
    at = corpus["reject_at"]["anchor past line 1"]
    corpus["legal"].append(text[: at + 1])
    cases_file(corpus)

    assert diff_grammar_check.run() == 1

    assert (f"  MUST REJECT at byte {at} but rejected at {at + 1}: anchor past line 1") in _verdict(
        capsys
    )


def test_run_accepts_a_cases_file_with_no_offsets_at_all(
    cases_file: Callable[[dict[str, Any]], Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """`reject_at` is optional in the file: each reject case then only has to be refused."""
    corpus = _corpus()
    del corpus["reject_at"]
    cases_file(corpus)

    assert diff_grammar_check.run() == 0
    assert "cases correct" in _verdict(capsys)[0]


def test_run_does_not_hold_a_reject_case_without_an_offset_to_one(
    cases_file: Callable[[dict[str, Any]], Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """A reject case with no `reject_at` entry only has to be refused somewhere."""
    corpus = _corpus()
    corpus["legal"].append(corpus["reject"]["prose"][:4])
    cases_file(corpus)

    assert diff_grammar_check.run() == 0
    assert "cases correct" in _verdict(capsys)[0]


def test_run_reports_a_prefix_the_grammar_lets_the_model_end_on(
    cases_file: Callable[[dict[str, Any]], Path], capsys: pytest.CaptureFixture[str]
) -> None:
    corpus = _corpus()
    corpus["ends"].append(corpus["not_stop"]["header only"])
    cases_file(corpus)

    assert diff_grammar_check.run() == 1

    assert "  MUST NOT STOP but grammar allows ending: header only" in _verdict(capsys)


def test_run_does_not_credit_a_not_stop_case_the_grammar_refuses_outright(
    cases_file: Callable[[dict[str, Any]], Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """Refused outright is not "cannot stop": that case says nothing about stopping.

    `may_stop` is False for a refused string whatever the grammar does at
    the end, so crediting it would count a case that measured nothing.
    """
    cases_file(_tiny(not_stop={"unreachable": "abcd"}, legal=["abx"], ends=[]))

    assert diff_grammar_check.run() == 1

    assert _verdict(capsys) == [
        "0/1 cases correct",
        "  MUST NOT STOP is not even a legal prefix, rejected at byte 2: unreachable",
    ]


def test_run_reports_a_finished_diff_the_grammar_will_not_let_end(
    cases_file: Callable[[dict[str, Any]], Path], capsys: pytest.CaptureFixture[str]
) -> None:
    corpus = _corpus()
    corpus["ends"] = _without(corpus["ends"], corpus["stop"]["complete delete"])
    cases_file(corpus)

    assert diff_grammar_check.run() == 1

    assert "  MUST STOP but grammar forbids ending: complete delete" in _verdict(capsys)


def test_run_reports_a_finished_diff_the_grammar_refuses_outright(
    cases_file: Callable[[dict[str, Any]], Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """A stop case the grammar cannot even read is a failure to end, not a pass."""
    cases_file(_tiny(stop={"unreadable": "abcd"}, legal=["abx"], ends=[]))

    assert diff_grammar_check.run() == 1

    assert _verdict(capsys) == [
        "0/1 cases correct",
        "  MUST STOP but grammar forbids ending: unreadable",
    ]


def test_run_counts_every_failure_against_the_total(
    cases_file: Callable[[dict[str, Any]], Path], capsys: pytest.CaptureFixture[str]
) -> None:
    corpus = _corpus()
    total = sum(len(corpus[k]) for k in ("admit", "reject", "not_stop", "stop"))
    corpus["legal"].append(corpus["reject"]["prose"])
    corpus["ends"].append(corpus["not_stop"]["header only"])
    cases_file(corpus)

    assert diff_grammar_check.run() == 1

    assert _verdict(capsys)[0] == f"{total - 2}/{total} cases correct"


# ---------------------------------------------------------------- as scripts


def _script(path: str, *args: str, monkeypatch: pytest.MonkeyPatch) -> int | str | None:
    """Run a tool the way `python tools/x.py ...` does; its exit code."""
    monkeypatch.setattr(sys, "argv", [path, *args])
    monkeypatch.setattr(sys, "path", list(sys.path))
    with pytest.raises(SystemExit) as exc_info:
        runpy.run_path(path, run_name="__main__")
    code: int | str | None = exc_info.value.code
    return code


def _no_git_history(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every git call the tools make answer with an empty history."""

    def run(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)


@pytest.mark.parametrize("module", [diff_grammar_check, edit_grammar_check])
def test_emit_as_a_script_writes_the_corpus_and_exits_clean(
    module: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _no_git_history(monkeypatch)

    assert _script(str(module.__file__), "--emit", monkeypatch=monkeypatch) == 0

    written = json.loads(capsys.readouterr().out)
    assert set(written["admit"]) == set(module.SYNTHETIC)
    assert written["reject"] == module.MUST_REJECT


def test_the_diff_tool_run_as_a_script_exits_with_the_runs_verdict(
    cases_file: Callable[[dict[str, Any]], Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    corpus = _corpus()
    cases_file(corpus)
    assert _script(str(diff_grammar_check.__file__), monkeypatch=monkeypatch) == 0

    broken = _corpus()
    broken["legal"].append(broken["reject"]["prose"])
    cases_file(broken)
    assert _script(str(diff_grammar_check.__file__), monkeypatch=monkeypatch) == 1
    assert "MUST REJECT but admitted: prose" in capsys.readouterr().out


def test_the_edit_tool_run_as_a_script_hands_the_cases_to_the_shared_runner(
    cases_file: Callable[[dict[str, Any]], Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Without `--emit` the edit tool is the diff tool's `run` on its own cases file."""
    cases_file(_corpus())

    assert _script(str(edit_grammar_check.__file__), monkeypatch=monkeypatch) == 0
    assert "cases correct" in capsys.readouterr().out


def test_the_edit_tool_run_as_a_script_without_a_cases_file_says_to_emit_first(
    cases_file: Callable[[dict[str, Any]], Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # `cases_file` redirects the shared name; nothing has been written to it.
    assert _script(str(edit_grammar_check.__file__), monkeypatch=monkeypatch) == 2
    assert "run --emit from the repo checkout first" in capsys.readouterr().out
