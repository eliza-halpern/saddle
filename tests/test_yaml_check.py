"""Tests for `saddle yaml-check`.

The contract, in the words src/saddle/yamlcheck.py uses for itself: `check_file`
names every way a file fails at the 1-based line and column where it fails, and
names none for a file PyYAML's own safe loader builds; a key written twice in one
mapping is refused at the second writing, two keys being the same key when they
load to the same value, and a key a merge brings in not being written at all; and
a tag the safe loader has no constructor for is refused by name at the node it
tags, as is a value it has a constructor for but cannot build from its text.
`saddle yaml-check PATH...` prints `PATH:LINE:COL: <problem>` for every problem
and exits 1 when there is one and 0 when there is not.

Fixtures live in tests/yaml_check_fixtures.py: GOOD is every file that must be
taken (each is a file `yaml.safe_load_all` itself builds), BAD every file that
must be refused, with the line, the column and a needle the report must contain.

Contract mutants, and the test that kills each:

1. Count a mapping key by the text that wrote it (`key_node.value`) instead of by
   what it loads to: killed by
   `test_a_file_that_fails_is_refused_at_the_line_and_column_it_fails[duplicate-written-quoted-and-plain]`,
   which reports nothing when `"cluster":` and `cluster:` are read as two names.
2. Take a key's tag as the file wrote it, instead of the tag it is built under:
   killed by `...[duplicate-quoted-equals-key]`, where `=` and `'='` are one key.
3. Name a duplicate at the first writing instead of the second: killed by
   `test_the_second_writing_of_a_key_is_the_one_refused`, which expects 2 and 3.
4. Count a `<<` merge as a written key: killed by
   `test_a_key_brought_in_by_a_merge_is_an_override_not_a_duplicate` and by
   `test_a_quoted_double_angle_is_a_key_and_not_a_merge`.
5. Walk a mapping again for every alias that names it: killed by
   `test_one_mapping_named_by_two_aliases_is_refused_once`.
6. Ignore an unsafe tag, or name it but keep walking under it: killed by
   `test_a_file_that_fails_is_refused_at_the_line_and_column_it_fails[unsafe-python-tag]`
   and `...[unsafe-custom-tag]`, and by
   `test_a_node_refused_by_its_tag_names_that_node_and_not_its_contents`.
7. Name a tag by something other than what the file wrote: killed by
   `test_a_tag_is_refused_by_the_name_the_file_wrote`.
8. Keep a scalar the safe loader cannot build instead of naming it, or catch only
   `yaml.YAMLError` while building it: killed by
   `test_a_file_that_fails_is_refused_at_the_line_and_column_it_fails[value-the-safe-loader-cannot-build]`
   and `...[bool-the-safe-loader-cannot-build]`, whose loader raises a `KeyError`
   and whose test asks for a named problem, not a crash.
9. Read a position as the loader counted it (0-based) instead of as a person
   counts it: killed by every BAD fixture, whose line and column are counted from
   1.
10. Let a file that is not there, or not text, be a pass: killed by
    `test_a_file_that_is_not_there_is_not_a_pass` and
    `test_a_file_that_is_not_text_is_refused_without_a_traceback`.
11. Read what a `<<` names as an ordinary value, so any merge shape is legal and
    nothing is ever merged in: killed by
    `test_a_merge_of_a_scalar_is_refused_at_the_merge` and by
    `test_a_file_that_fails_is_refused_at_the_line_and_column_it_fails[merge-of-a-list-containing-a-scalar]`,
    which names the scalar inside the list the merge asks to merge.
12. Leave `yaml-check` out of the commands `main` handles, or run it without
    PyYAML: killed by `test_the_yaml_check_command_refuses_a_bad_file` and by
    `test_the_yaml_check_command_names_pyyaml_when_it_is_missing`.
13. Report the path the check resolved rather than the path the command was
    given: killed by `test_the_path_is_printed_as_it_was_given_on_the_command_line`
    and by the relative path `test_the_yaml_check_command_refuses_a_bad_file`
    hands to `main`, both of which read the report line back as the shell named it.
"""

from __future__ import annotations

import re
import sys
from io import StringIO
from pathlib import Path
from typing import Final

import pytest
import yaml  # type: ignore[import-untyped]
from yaml_check_fixtures import BAD, GOOD, NOT_UTF8

from saddle import yamlcheck
from saddle.cli import main
from saddle.yamlcheck import YamlProblem, check_file, check_paths, check_text

#: The shape of one report line: the path as given, the line, the column, the problem.
REPORT_LINE: Final = r"^{}:\d+:\d+: .+$"


def _text(*lines: str) -> str:
    """A file's text from the lines it is written in."""
    return "".join(f"{line}\n" for line in lines)


def _file(tmp_path: Path, name: str, text: str) -> str:
    """One fixture as a file on disk, and the path a person would type for it."""
    path = tmp_path / f"{name}.yml"
    path.write_text(text, encoding="utf-8")
    return str(path)


def _report(paths: list[str]) -> tuple[int, list[str], str]:
    """What `saddle yaml-check PATH...` does: its exit code, its lines, its raw text."""
    out = StringIO()
    code = check_paths(paths, stdout=out)
    text = out.getvalue()
    return code, text.splitlines(), text


def _where(lines: list[str], path: str) -> list[tuple[int, int]]:
    """The line and column of each report line, as a person counts them."""
    where = [
        (int(match.group(1)), int(match.group(2)))
        for line in lines
        if (match := re.match(rf"^{re.escape(path)}:(\d+):(\d+): ", line)) is not None
    ]
    assert len(where) == len(lines), lines
    return where


def _safe_loader_builds(text: str) -> bool:
    """Whether PyYAML's own safe loader builds every document of this text."""
    try:
        list(yaml.safe_load_all(text))
    except Exception:  # any failure of the loader itself is one here
        return False
    return True


@pytest.mark.parametrize("name", sorted(GOOD))
def test_a_file_the_safe_loader_builds_is_taken(name: str, tmp_path: Path) -> None:
    """Known-good: the workflow, the multi-document file, anchors and aliases, the
    merges, `1` beside `"1"`, the `=` key, a value that points into itself, the
    empty file, and the comment-only file."""
    text = GOOD[name]
    path = _file(tmp_path, name, text)
    assert _safe_loader_builds(text), f"{name}: a known-good fixture the safe loader refuses"
    assert check_file(path) == []
    code, lines, raw = _report([path])
    assert code == 0, raw
    assert lines == []


@pytest.mark.parametrize("name", sorted(BAD))
def test_a_file_that_fails_is_refused_at_the_line_and_column_it_fails(
    name: str, tmp_path: Path
) -> None:
    """Known-bad: each file is refused, at the line and column its problem starts
    at, with the problem named, and with nothing else named in the file."""
    text, refusals = BAD[name]
    path = _file(tmp_path, name, text)
    problems = check_file(path)
    named = [(problem.line, problem.column, problem.message) for problem in problems]
    for refusal in refusals:
        matches = [
            row
            for row in named
            if (row[0], row[1]) == (refusal.line, refusal.column) and refusal.says in row[2]
        ]
        assert matches, (
            f"{name}: expected {refusal.says!r} at {refusal.line}:{refusal.column}, got {named}"
        )
    assert len(named) == len(refusals), f"{name}: expected {len(refusals)} problems, got {named}"

    code, lines, raw = _report([path])
    assert code == 1, raw
    assert len(lines) == len(refusals), raw
    for line in lines:
        assert re.fullmatch(REPORT_LINE.format(re.escape(path)), line), line
    for refusal in refusals:
        assert refusal.says in raw, raw


@pytest.mark.parametrize("name", sorted(BAD))
def test_a_file_the_safe_loader_refuses_is_refused_here_too(name: str) -> None:
    """A file the safe loader itself will not build is never taken by the check:
    the two agree on every file that carries a problem outside the duplicate rule."""
    text, _ = BAD[name]
    problems = check_text("f.yml", text)
    if _safe_loader_builds(text):
        # The only thing this check adds over the loader is the duplicate rule, so
        # a file the loader builds can only be refused here for a duplicate.
        assert all(" is written twice in this mapping" in p.message for p in problems), problems
        return
    assert problems, f"{name}: the safe loader refuses this file and the check took it"


def test_the_problem_line_names_the_file_the_line_and_the_column(tmp_path: Path) -> None:
    path = _file(tmp_path, "pipeline", BAD["duplicate-under-a-sequence"][0])
    code, lines, raw = _report([path])
    assert code == 1, raw
    assert len(lines) == 1, raw
    assert lines[0].startswith(f"{path}:4:5: ")
    assert "duplicate key 'name'" in lines[0]


def test_the_path_is_printed_as_it_was_given_on_the_command_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = BAD["unsafe-custom-tag"][0]
    _file(tmp_path, "pipeline", text)
    monkeypatch.chdir(tmp_path)
    _, _, given_relative = _report(["pipeline.yml"])
    assert given_relative.startswith("pipeline.yml:2:11: "), given_relative
    _, _, given_absolute = _report([str(tmp_path / "pipeline.yml")])
    assert given_absolute.startswith(f"{tmp_path / 'pipeline.yml'}:2:11: "), given_absolute


def test_every_file_named_is_checked_and_each_problem_is_reported_under_its_own_path(
    tmp_path: Path,
) -> None:
    bad_one = _file(tmp_path, "one", BAD["duplicate-at-the-top-level"][0])
    bad_two = _file(tmp_path, "two", BAD["unsafe-python-tag"][0])
    good = _file(tmp_path, "good", GOOD["workflow"])
    code, lines, raw = _report([good, bad_one, bad_two])
    assert code == 1, raw
    assert len(lines) == 2, raw
    assert not any(line.startswith(f"{good}:") for line in lines), raw
    assert sum(1 for line in lines if line.startswith(f"{bad_one}:")) == 1, raw
    assert sum(1 for line in lines if line.startswith(f"{bad_two}:")) == 1, raw


def test_one_file_can_carry_more_than_one_problem(tmp_path: Path) -> None:
    path = _file(tmp_path, "job", BAD["duplicate-three-ways-in-one-mapping"][0])
    code, lines, raw = _report([path])
    assert code == 1, raw
    assert len(lines) == 2, raw
    assert _where(lines, path) == [(2, 1), (3, 1)], raw


def test_a_file_that_is_not_there_is_not_a_pass(tmp_path: Path) -> None:
    missing = tmp_path / "gone.yml"
    code, lines, raw = _report([str(missing)])
    assert code == 1, raw
    assert lines == [f"{missing}: cannot be read: No such file or directory"], raw
    problems = check_file(str(missing))
    assert [str(problem) for problem in problems] == lines, problems
    assert (problems[0].line, problems[0].column) == (None, None), problems


def test_a_file_that_is_not_text_is_refused_without_a_traceback(tmp_path: Path) -> None:
    path = tmp_path / "binary.yml"
    path.write_bytes(NOT_UTF8)
    code, lines, raw = _report([str(path)])
    assert code == 1, raw
    assert len(lines) == 1, raw
    assert lines[0].startswith(f"{path}: is not UTF-8 text"), raw


def test_a_failure_inside_the_check_is_a_problem_and_not_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guarantee is that no file ends `saddle yaml-check` in a traceback, so a
    failure of the check itself is a problem line: simulated here, because no YAML
    text is known to break the walk this way."""

    failure = "the walk broke"

    def broken(path: str, text: str) -> list[YamlProblem]:
        del path, text
        raise ValueError(failure)

    monkeypatch.setattr(yamlcheck, "check_text", broken)
    path = _file(tmp_path, "job", GOOD["workflow"])
    code, lines, raw = _report([path])
    assert code == 1, raw
    assert lines == [f"{path}: cannot be checked: ValueError: the walk broke"], raw


def test_a_file_nested_past_the_walk_is_named_and_not_a_crash(tmp_path: Path) -> None:
    """The check recurses once per level of nesting, as the loader it mirrors does.
    A file past what it can hold is refused with a problem line, not a traceback."""
    deep = "".join(f"{'  ' * level}k{level}:\n" for level in range(600))
    path = _file(tmp_path, "deep", deep)
    code, lines, raw = _report([path])
    assert code == 1, raw
    assert lines, raw


def test_the_second_writing_of_a_key_is_the_one_refused(tmp_path: Path) -> None:
    path = _file(tmp_path, "job", _text("a: 1", "'a': 2", '"a": 3'))
    problems = check_file(path)
    assert [(p.line, p.column) for p in problems] == [(2, 1), (3, 1)], problems
    assert all("the first at line 1" in problem.message for problem in problems), problems


def test_quoted_and_plain_names_are_one_key_written_twice(tmp_path: Path) -> None:
    assert check_file(_file(tmp_path, "good", GOOD["quoted-and-plain-key-names"])) == []
    problems = check_file(_file(tmp_path, "bad", BAD["duplicate-written-quoted-and-plain"][0]))
    assert [(p.line, p.column, p.message) for p in problems] == [
        (2, 1, "duplicate key 'cluster': it is written twice in this mapping, the first at line 1")
    ], problems


def test_numbers_and_words_that_look_alike_are_two_keys(tmp_path: Path) -> None:
    assert check_file(_file(tmp_path, "keys", GOOD["equal-in-writing-unequal-in-loading"])) == []
    problems = check_file(_file(tmp_path, "typed", BAD["duplicate-key-with-a-type-tag"][0]))
    assert [p.message for p in problems] == [
        "duplicate key 5: it is written twice in this mapping, the first at line 1"
    ], problems


def test_a_key_brought_in_by_a_merge_is_an_override_not_a_duplicate(tmp_path: Path) -> None:
    assert check_file(_file(tmp_path, "good", GOOD["anchors-and-aliases"])) == []
    assert check_file(_file(tmp_path, "same", GOOD["merging-the-same-anchor-twice"])) == []
    assert check_file(_file(tmp_path, "itself", GOOD["merging-the-mapping-itself"])) == []
    assert check_file(_file(tmp_path, "list", GOOD["list-of-merges"])) == []
    problems = check_file(_file(tmp_path, "bad", BAD["duplicate-in-a-mapping-that-also-merges"][0]))
    assert [(p.line, p.column) for p in problems] == [(6, 3)], problems


def test_a_quoted_double_angle_is_a_key_and_not_a_merge(tmp_path: Path) -> None:
    assert check_file(_file(tmp_path, "quoted", _text('"<<": a shell step'))) == []


def test_one_mapping_named_by_two_aliases_is_refused_once(tmp_path: Path) -> None:
    problems = check_file(
        _file(tmp_path, "aliases", BAD["duplicate-in-a-mapping-named-by-two-aliases"][0])
    )
    assert [(p.line, p.column, p.message) for p in problems] == [
        (3, 3, "duplicate key 'tier': it is written twice in this mapping, the first at line 2")
    ], problems


def test_a_node_refused_by_its_tag_names_that_node_and_not_its_contents(tmp_path: Path) -> None:
    """`--- !!mapx` refuses the document at the tag: what is under it is never
    built, so it is not a second problem, and the file is refused either way."""
    path = _file(tmp_path, "tagged", BAD["a-duplicate-inside-a-node-the-tag-refuses"][0])
    problems = check_file(path)
    assert [(p.line, p.column, p.message) for p in problems] == [(1, 5, "unsafe tag '!!mapx'")], (
        problems
    )


def test_a_tag_is_refused_by_the_name_the_file_wrote(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("unsafe-python-tag", "unsafe-custom-tag", "unsafe-tag-from-a-tag-prefix"):
        text, refusals = BAD[name]
        path = _file(tmp_path, name, text)
        _, lines, raw = _report([path])
        assert len(lines) == 1, raw
        assert refusals[0].says in lines[0], raw
    problems = check_text("f.yml", GOOD["workflow"])
    assert problems == []
    assert capsys.readouterr().out == ""


def test_an_unsafe_tag_is_never_constructed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Constructing this value would run a command. Naming the tag is the whole
    check, so the sentinel it would print never appears."""
    path = _file(tmp_path, "hook", BAD["a-command-the-unsafe-tag-would-run"][0])
    code, lines, raw = _report([path])
    assert code == 1, raw
    assert len(lines) == 1, raw
    assert "this-ran" not in capsys.readouterr().out, capsys.readouterr().out


def test_an_unsafe_tag_in_key_position_is_refused_by_name(tmp_path: Path) -> None:
    problems = check_file(_file(tmp_path, "key", BAD["unsafe-tag-in-key-position"][0]))
    assert [(p.line, p.column, p.message) for p in problems] == [
        (1, 1, "unsafe tag '!Ref' in key position")
    ], problems


def test_a_value_the_safe_loader_cannot_build_is_a_failure_at_its_node(tmp_path: Path) -> None:
    problems = check_file(_file(tmp_path, "count", BAD["value-the-safe-loader-cannot-build"][0]))
    assert [(p.line, p.column) for p in problems] == [(2, 8)], problems
    assert "the safe loader cannot build '!!int' from 'abc'" in problems[0].message, problems
    keyerr = check_file(_file(tmp_path, "flag", BAD["bool-the-safe-loader-cannot-build"][0]))
    assert [p.message for p in keyerr] == [
        "the safe loader cannot build '!!bool' from 'yesno': 'yesno'"
    ], keyerr
    key = check_file(_file(tmp_path, "keytag", BAD["a-key-the-safe-loader-cannot-build"][0]))
    assert [(p.line, p.column) for p in key] == [(1, 1)], key
    assert key[0].message.startswith("the safe loader cannot build '!!int' from 'abc'"), key


def test_a_key_that_composes_to_a_collection_is_refused_as_unhashable(tmp_path: Path) -> None:
    for name in ("collection-as-a-key", "set-tag-as-a-key"):
        problems = check_file(_file(tmp_path, name, BAD[name][0]))
        assert len(problems) == 1, problems
        assert (problems[0].line, problems[0].column) == (1, 3), problems
        assert "it is not hashable" in problems[0].message, problems


def test_a_merge_of_a_scalar_is_refused_at_the_merge(tmp_path: Path) -> None:
    problems = check_file(_file(tmp_path, "merge", BAD["merge-of-a-scalar"][0]))
    assert [(p.line, p.column, p.message) for p in problems] == [
        (1, 7, "a merge needs a mapping, or a list of mappings, found a scalar")
    ], problems
    in_list = check_file(_file(tmp_path, "list", BAD["merge-of-a-list-containing-a-scalar"][0]))
    assert [(p.line, p.column, p.message) for p in in_list] == [
        (3, 8, "a merge needs mappings to merge in, found a scalar")
    ], in_list


def test_a_document_that_builds_a_value_pointing_into_itself_is_taken(tmp_path: Path) -> None:
    assert check_file(_file(tmp_path, "loop", GOOD["value-that-points-into-itself"])) == []


def test_the_yaml_check_command_takes_a_good_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _file(tmp_path, "pipeline", GOOD["workflow"])
    assert main(["yaml-check", path]) == 0
    assert capsys.readouterr().out == ""


def test_the_yaml_check_command_refuses_a_bad_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    text = BAD["duplicate-at-the-top-level"][0]
    path = _file(tmp_path, "pipeline", text)
    assert main(["yaml-check", path]) == 1
    out = capsys.readouterr().out
    expected = (
        f"{path}:3:1: duplicate key 'name': "
        "it is written twice in this mapping, the first at line 1\n"
    )
    assert out == expected, out
    # What the shell named is what the report names, not what the check resolved
    # it to while reading the file.
    monkeypatch.chdir(tmp_path)
    assert main(["yaml-check", "pipeline.yml"]) == 1
    out = capsys.readouterr().out
    assert out == (
        "pipeline.yml:3:1: duplicate key 'name': "
        "it is written twice in this mapping, the first at line 1\n"
    ), out


def test_the_yaml_check_command_needs_a_path_to_check(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["yaml-check"])
    assert raised.value.code == 2
    assert "the following arguments are required" in capsys.readouterr().err


def test_yaml_check_needs_no_model_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("SADDLE_API_KEY", "SADDLE_GATEWAY_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    path = _file(tmp_path, "pipeline", GOOD["workflow"])
    assert main(["yaml-check", path]) == 0


def test_the_yaml_check_command_names_pyyaml_when_it_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """PyYAML comes from the optional `yaml` extra. Without it the command says
    what is missing and how to install it, rather than failing as an import error."""
    path = _file(tmp_path, "pipeline", GOOD["workflow"])
    cached = [name for name in sys.modules if name == "yaml" or name.startswith("yaml.")]
    for name in cached:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setitem(sys.modules, "yaml", None)
    monkeypatch.delitem(sys.modules, "saddle.yamlcheck", raising=False)
    assert main(["yaml-check", path]) == 2
    err = capsys.readouterr().err
    assert "saddle yaml-check needs PyYAML" in err, err
    assert "saddle[yaml]" in err, err


def test_a_check_of_nothing_to_check_is_not_a_refusal() -> None:
    assert _report([])[0] == 0


def test_problems_are_printed_in_the_order_the_file_writes_them(tmp_path: Path) -> None:
    text = _text("a: 1", "'a': 2", "b: 1", '"b": 2', "a: 3")
    path = _file(tmp_path, "order", text)
    _, lines, raw = _report([path])
    assert _where(lines, path) == [(2, 1), (4, 1), (5, 1)], raw
