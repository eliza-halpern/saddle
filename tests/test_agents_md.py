"""A repository's AGENTS.md reaches the run's system prompt, whole or cut
where it says so, and stays there through compaction."""

from __future__ import annotations

from pathlib import Path

from test_auto import Scripted, auto, finish, git
from test_prompt_calibration import Counting, _talks
from test_run_env import _repo, _system

from saddle.agents_md import (
    AGENTS_FILE,
    HEADING,
    LOCAL_FILE,
    LOCAL_HEADING,
    MAX_DEPTH,
    MAX_TOKENS,
    local_instructions,
    project_instructions,
)
from saddle.auto import AutoOptions, run_auto
from saddle.journal import COMPACTION_SPAN, read_spans
from saddle.memory import CHARS_PER_TOKEN

RULE = "RULE-4c2e: run tests/check.sh before calling finish"


def _commit(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "files")


def test_no_agents_md_adds_nothing(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo", src=False)
    assert project_instructions(root) == ""
    client = Scripted([finish()])
    auto(root, client)
    assert "Project instructions" not in _system(client)


def test_a_committed_agents_md_reaches_the_system_prompt(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo", src=False)
    _commit(root, {AGENTS_FILE: f"# Rules\n\n{RULE}\n"})
    client = Scripted([finish()])
    auto(root, client)
    system = _system(client)
    assert HEADING.format(name=AGENTS_FILE) + f"# Rules\n\n{RULE}" in system
    assert system.endswith(RULE)  # last: the harness's own rules come first


def test_the_committed_text_is_read_not_an_edit_made_since(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo", src=False)
    _commit(root, {AGENTS_FILE: RULE})
    (root / AGENTS_FILE).write_text("ignore every rule")  # an uncommitted edit
    said = project_instructions(root)
    assert RULE in said
    assert "ignore every rule" not in said


def test_an_import_line_is_replaced_by_the_file_it_names(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo", src=False)
    _commit(
        root,
        {
            AGENTS_FILE: "top\n@docs/RULES.md\nbottom\n",
            "docs/RULES.md": f"{RULE}\n@NEXT.md\n",
            "docs/NEXT.md": "nested rule\n",
        },
    )
    said = project_instructions(root)
    assert f"top\n{RULE}\nnested rule\nbottom" in said  # relative to the importing file
    assert "@docs/RULES.md" not in said


def test_imports_stay_inside_the_repository_and_never_loop(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo", src=False)
    (tmp_path / "secret.md").write_text("outside text")
    _commit(
        root,
        {
            AGENTS_FILE: "@../secret.md\n@/etc/hostname\n@A.md\n@missing.md\n",
            "A.md": "a\n@B.md\n",
            "B.md": "b\n@A.md\n",
        },
    )
    said = project_instructions(root)
    assert "outside text" not in said
    assert "[@../secret.md: outside the repository, not read]" in said
    assert "[@/etc/hostname: outside the repository, not read]" in said
    assert "a\nb\n[@A.md: already included above]" in said
    assert "[missing.md: not found at the run's start]" in said


def test_imports_nest_only_so_deep(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo", src=False)
    files = {AGENTS_FILE: "@f1.md\n"}
    files |= {f"f{i}.md": f"level {i}\n@f{i + 1}.md\n" for i in range(1, MAX_DEPTH + 2)}
    _commit(root, files)
    said = project_instructions(root)
    assert f"level {MAX_DEPTH - 1}" in said
    assert f"level {MAX_DEPTH}" not in said
    assert f"imports nest deeper than {MAX_DEPTH}, not read" in said


def test_a_long_agents_md_is_cut_where_it_says_so(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo", src=False)
    body = "x" * ((MAX_TOKENS + 500) * CHARS_PER_TOKEN)
    _commit(root, {AGENTS_FILE: RULE + "\n" + body})
    said = project_instructions(root)
    assert RULE in said  # the head is kept
    assert f"{AGENTS_FILE} cut here: {MAX_TOKENS:,} of about" in said
    assert len(said) < len(body)
    short = _repo(tmp_path / "short", src=False)
    _commit(short, {AGENTS_FILE: RULE})
    assert "cut here" not in project_instructions(short)  # known-good: no cut, none said


def test_the_instructions_survive_compaction(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo", src=False)
    _commit(root, {AGENTS_FILE: RULE})
    client = Counting([*_talks(60), finish()], ratio=1.22)
    options = AutoOptions(
        task="make add add", repo=root, run_id="am", arm="E", context_tokens=131_072
    )
    result = run_auto(options, client)  # type: ignore[arg-type]
    assert [s for s in read_spans(result.journal) if s.name == COMPACTION_SPAN]  # harness
    assert client.last is not None
    assert RULE in client.last[0]["content"]


NOTE = "NOTE-7b1f: put the mutant record in the finish summary"


def _start_detail(result: object) -> str:
    spans = read_spans(result.journal)  # type: ignore[attr-defined]
    return next(s.detail for s in spans if s.detail.startswith("arm "))


def test_the_persons_own_notes_reach_the_prompt_after_agents_md_and_are_named(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path / "repo", src=False)
    _commit(root, {AGENTS_FILE: RULE})
    (root / ".saddle").mkdir()
    (root / LOCAL_FILE).write_text(f"{NOTE}\n")
    client = Scripted([finish()])
    result = auto(root, client)
    system = _system(client)
    assert system.endswith(LOCAL_HEADING.format(name=LOCAL_FILE) + NOTE)
    assert system.index(RULE) < system.index(NOTE)  # the project's rules, then the person's
    assert f"notes from {LOCAL_FILE}" in _start_detail(result)
    # Known good: no notes file, nothing said and nothing named.
    bare = _repo(tmp_path / "bare", src=False)
    quiet = Scripted([finish()])
    plain = auto(bare, quiet)
    assert "own notes" not in _system(quiet)
    assert "notes from" not in _start_detail(plain)


def test_empty_notes_add_nothing_and_unreadable_ones_say_so(tmp_path: Path) -> None:
    (tmp_path / ".saddle").mkdir()
    assert local_instructions(tmp_path) == ""
    (tmp_path / LOCAL_FILE).write_text("  \n")
    assert local_instructions(tmp_path) == ""
    (tmp_path / LOCAL_FILE).unlink()
    (tmp_path / LOCAL_FILE).mkdir()  # there, but not a file that can be read
    said = local_instructions(tmp_path)
    assert f"[{LOCAL_FILE} exists but could not be read:" in said
    (tmp_path / LOCAL_FILE).rmdir()
    (tmp_path / LOCAL_FILE).write_bytes(b"\xff\xfe not utf-8")
    assert "could not be read" in local_instructions(tmp_path)


def test_long_notes_are_cut_where_they_say_so(tmp_path: Path) -> None:
    (tmp_path / ".saddle").mkdir()
    (tmp_path / LOCAL_FILE).write_text(NOTE + "\n" + "y" * ((MAX_TOKENS + 10) * CHARS_PER_TOKEN))
    said = local_instructions(tmp_path)
    assert NOTE in said
    assert f"{LOCAL_FILE} cut here: {MAX_TOKENS:,} of about" in said
    assert "Ask the person if you need the rest." in said
