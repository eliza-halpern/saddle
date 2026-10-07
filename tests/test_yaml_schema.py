"""Tests for `saddle yaml-check --schema`: the shape every document must have.

The contract, in the words src/saddle/yamlcheck.py uses for itself: with
`--schema`, every document of every named file is held against the JSON Schema
that file names, compiled the way `jsonschema` compiles a schema and run on the
document the safe loader builds (merge keys applied); each violation is a problem
with the file, at the 1-based line and column, in the whole file, where the node
its own `absolute_path` lands on starts, and its message names that document path
-- keys joined by dots, sequence items by index, `resources.jobs.nightly.tasks[0]`
-- with no path in front of a problem about the document itself; every violation
of every document of a multi-document file is printed, in the order the file
writes them; a key the schema does not allow is named by that key; a schema file
that cannot be read, or is not a schema, is named instead of standing as a rule
nothing can fail; without `--schema` the command only parses, as #156a left it;
and with it every problem the parse, key and tag rules refuse is still refused.

Mutations each test refuses, one per item of the contract.

- The shape is not checked, or is checked against the wrong thing.
  `validator.iter_errors(validator)` -- the schema validated against itself, so no
  document ever is -- and `validator.iter_errors(document.node)`, the node tree
  rather than the value the loader built, which drops every violation a merge
  brings in and takes every job whose keys come from a preset:
  `test_a_document_that_does_not_match_the_schema_is_refused_at_the_node_and_path_it_is_about`,
  `test_every_bundle_the_tool_writes_is_taken_by_its_shape`, and
  `test_the_documents_a_shape_is_run_against_are_the_documents_the_loader_builds`
  for each side. `if validator is not None:` dropped from `check_text`, which
  would hold every document to a shape even with no `--schema`, is caught by
  `test_without_a_schema_the_command_only_parses_as_before`, and `validator_for`
  replaced with one draft class by
  `test_load_schema_compiles_the_draft_a_schema_file_names`.
- A violation is not traced to its node, or not named by its document path.
  `parts[:1]` for `parts` in `_locate`, which locates every violation at the top
  key of the path, and `where` left empty in `_schema_problems`, which prints the
  schema's sentence with no path to say who it is about:
  `test_the_lines_and_columns_a_violation_is_reported_at_are_those_of_the_node_the_path_lands_on`,
  which holds both halves of each violation's path in front of the report and
  refuses the shallow half, and the document-path lines of
  `test_a_missing_required_key_in_a_sequence_item_is_named_at_the_item_that_lacks_it`,
  `test_a_violation_brought_in_by_a_merge_is_at_where_the_merged_value_is_written`
  and `test_a_document_that_is_not_an_object_is_refused_at_the_whole_document`.
- Not every violation, and not in the order the file writes them. A `break` after
  the first problem of a document, a loop that stops at the first document, and
  `sorted(` read as `sorted(reverse` in `check_text`:
  `test_a_multi_document_file_holds_a_violation_in_each_document_in_order` (six
  violations across four documents),
  `test_a_document_with_two_violations_of_one_shape_is_refused_twice` (two at the
  same node), and
  `test_the_violations_of_one_document_are_reported_in_the_order_the_file_writes_them`,
  whose fixture is written so the key rule reaches its problem at line 8 and the
  shape rule reaches its own at line 1, so a report in the order the checks found
  them in is the wrong order while a count alone cannot see it.
- A shape problem displaces a parse problem, or the option changes what is
  refused. `problems.extend(_Document(...))` dropped, `_Document` skipped when a
  `validator` is in hand, and `load_schema`'s failure answered as an empty
  validator: `test_every_problem_the_parse_key_and_tag_rules_refuse_is_refused_with_a_schema_too`,
  `test_the_shape_rule_and_the_key_rule_refuse_one_document_for_their_own_reasons`
  (which counts the problems in the report rather than searching for what it
  expects, so a problem dropped on either side counts differently) and
  `test_a_schema_file_that_is_not_usable_is_named_before_any_yaml_file_is_checked`.
- The validator reaches the documents by the wrong wiring: one validator built per
  file or per document instead of per run, or a document checked with a validator
  that came from elsewhere.
  `test_the_shape_check_is_run_once_for_every_document_of_every_file_it_was_given`,
  which counts calls and compares the object every call was handed against the one
  the option named, and catches a validator threaded into the call -- the wiring
  the suite passed with until this test existed, and which `--schema` needed to be
  real at all.

Fixtures live in tests/yaml_schema_fixtures.py. `BUNDLE_SCHEMA` is the shape
exported once from a deployment tool's published schema and committed at
tests/fixtures/deployment-bundle.schema.json; `SCHEMAS` (`any`, `api-version-only`,
`api-version-and-nothing-else`) and `BAD_SCHEMAS` (`schema-that-is-not-json`,
`json-that-is-not-a-schema`) are the stand-in schema files a test hands to
`--schema`. `SCHEMA_GOOD` is four bundles the shape takes --
`bundle-as-the-tool-writes-it`, `a-job-that-merges-a-preset`,
`a-job-that-writes-again-a-key-its-preset-brings` and
`optional-keys-at-their-bounds` -- and `SCHEMA_BAD` is fifteen it refuses, each
with one `Refusal` per violation: `misspelt-key`, `a-key-a-job-does-not-allow`,
`a-key-a-volume-does-not-allow`, `a-key-a-mapping-one-level-down-does-not-allow`,
`a-key-the-document-itself-does-not-allow`,
`missing-required-key-in-a-sequence-item`, `a-job-with-an-empty-task-list`,
`a-bundle-that-declares-no-jobs`, `violation-deep-in-a-document`,
`every-bound-on-a-task-crossed-at-once`, `violation-in-each-document-of-a-file`,
`violation-carried-in-by-a-merge`, `a-document-that-is-a-list`,
`missing-a-key-the-document-itself-needs` and
`the-key-rule-and-the-shape-rule-on-one-document`. No run here opens a socket:
every YAML file, and every schema, is in these fixtures.
"""

from __future__ import annotations

import io
import json
import re
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]
from jsonschema.validators import validator_for  # type: ignore[import-untyped]
from yaml_check_fixtures import BAD
from yaml_schema_fixtures import BAD_SCHEMAS, BUNDLE_SCHEMA, SCHEMA_BAD, SCHEMA_GOOD, SCHEMAS

from saddle import yamlcheck
from saddle.cli import main
from saddle.yamlcheck import YamlProblem, check_file, check_paths, load_schema

_SCHEMA_PATH = str(BUNDLE_SCHEMA)
_KEY_RULE_FIXTURE = "the-key-rule-and-the-shape-rule-on-one-document"
_ONE_SHAPE = re.compile(r"^(?P<file>.+):(?P<line>\d+):(?P<column>\d+): (?P<saying>.*)$")


def _file(tmp_path: Path, name: str, text: str) -> str:
    """One YAML file, named the way the report has to print it."""
    path = tmp_path / f"{name}.yml"
    path.write_text(text, encoding="utf-8")
    return str(path)


def _schema_file(tmp_path: Path, name: str) -> str:
    """One schema file, holding the JSON `SCHEMAS` says it holds. It is written
    under a name no YAML file could be given, because a file that is named by
    `--schema` is a file a check was asked to use."""
    path = tmp_path / f"{name}.json"
    path.write_text(SCHEMAS[name], encoding="utf-8")
    return str(path)


def _run(paths: list[str], schema: str | None = None) -> tuple[int, list[str], str]:
    """What the command does with those files and that schema: its exit code, the
    problem lines it printed, and everything it printed, to search."""
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = check_paths(paths, stdout=buffer, schema=schema)
    raw = buffer.getvalue()
    return code, [line for line in raw.splitlines() if line], raw


def _bundle_validator() -> Any:
    """The validator for the committed shape, which is what the fixtures here are
    written against."""
    validator, problem = load_schema(_SCHEMA_PATH)
    assert problem is None, problem
    assert validator is not None
    return validator


def _report(path: str, schema: str | None = _SCHEMA_PATH) -> list[tuple[int, int, str]]:
    """Every problem with one file, as `(line, column, message)` in the order the
    command prints them -- which is the order the report has to print them in."""
    return [
        (_at(one.line), _at(one.column), one.message)
        for one in check_file(path, _validator_of(schema))
    ]


def _validator_of(schema: str | None) -> Any:
    """The validator `--schema` would hand the check for that schema file, which is
    what every problem list here is built with."""
    if schema is None:
        return None
    validator, problem = load_schema(schema)
    assert problem is None, problem
    assert validator is not None
    return validator


def _lines(path: str, schema: str | None = _SCHEMA_PATH) -> list[str]:
    """The lines the command prints for one file, as a person reads them."""
    return _run([path], schema)[1]


def _documents(text: str) -> list[Any]:
    """The document nodes of one file, composed as this check composes them."""
    loader = yaml.SafeLoader(text)
    documents: list[Any] = []
    while loader.check_node():
        documents.append(loader.get_node())
    return documents


def _at(position: int | None) -> int:
    """A position the check reports, as the number a test compares it to: `None` is a
    problem with no place in the file, which no position assertion here is about."""
    return 0 if position is None else int(position)


def _starts(text: str) -> list[int]:
    """The 1-based line each document of one file starts on, which is what a position
    "in the whole file" is counted from and what "in every document" is grouped by."""
    return [int(node.start_mark.line) + 1 for node in _documents(text)]


def _loaded(text: str) -> list[Any]:
    """What PyYAML's own safe loader builds of one file: the values a bundle schema
    is written for, merge keys applied."""
    return list(yaml.safe_load_all(text))


def _safe_loader_refuses(text: str) -> bool:
    """Whether the safe loader itself refuses to build this file."""
    try:
        list(yaml.safe_load_all(text))
    except (yaml.YAMLError, ValueError, KeyError, TypeError, ArithmeticError):
        return True
    return False


def _place(line: str) -> tuple[int, int]:
    """The `(line, column)` a report line names, which is what the order of a
    report and the position of a violation are both about."""
    match = _ONE_SHAPE.match(line)
    assert match is not None, line
    return int(match.group("line")), int(match.group("column"))


def _front(line: str) -> str:
    """A report line up to the colon its problem starts after: the file, the line
    and the column, which is the half a position test is about."""
    match = _ONE_SHAPE.match(line)
    assert match is not None, line
    return f"{match.group('file')}:{match.group('line')}:{match.group('column')}"


def _node_at(document: Any, parts: list[Any]) -> tuple[int, int]:
    """Where a document path lands in the nodes of one document, read by this test
    alone: a key into a mapping, an index into a sequence, and the line and column
    the node that lands on starts at. Independent of the module's own locating, so
    that a report and this describe the same node without one reading the other."""
    node: Any = document
    for part in parts:
        if isinstance(part, int):
            assert isinstance(node, yaml.SequenceNode), parts
            node = node.value[part]
        else:
            assert isinstance(node, yaml.MappingNode), parts
            holder: Any = None
            for key_node, value_node in node.value:
                if isinstance(key_node, yaml.ScalarNode) and key_node.value == str(part):
                    holder = value_node
                    break
            assert holder is not None, parts
            node = holder
    return int(node.start_mark.line) + 1, int(node.start_mark.column) + 1


def _keys_written(document: Any, parts: list[Any]) -> list[str]:
    """The keys one mapping of one document writes, before any merge is applied: what
    a run that held the node tree rather than the loaded document to the shape would
    find there."""
    node: Any = document
    for part in parts:
        if isinstance(part, int):
            assert isinstance(node, yaml.SequenceNode), parts
            node = node.value[part]
            continue
        assert isinstance(node, yaml.MappingNode), parts
        holder: Any = None
        for key_node, value_node in node.value:
            assert isinstance(key_node, yaml.ScalarNode), parts
            if key_node.value == str(part):
                holder = value_node
                break
        assert holder is not None, parts
        node = holder
    assert isinstance(node, yaml.MappingNode), parts
    return [str(key_node.value) for key_node, _ in node.value]


def _errors(document: Any, validator: Any) -> list[Any]:
    """The violations of one loaded document, with the paths the schema itself
    names them by."""
    return list(validator.iter_errors(document))


def _path_of(parts: Any) -> str:
    """A schema's own path parts, as the document path the report prints."""
    where: str = ""
    for part in parts:
        where = f"{where}[{part}]" if isinstance(part, int) else f"{where}.{part}"
    return where.lstrip(".")


# The committed shape, and the files that match it.


def test_the_committed_bundle_schema_is_a_shape_a_validator_compiles_from() -> None:
    """The shape these fixtures are written against is a file the project commits,
    and it is read as `jsonschema` reads a schema: read as text, compiled to a
    validator, and checked as a schema first. Checked before any file is, because a
    schema file that is not a schema makes every refusal in this module a rule that
    never applied."""
    validator, problem = load_schema(_SCHEMA_PATH)
    assert problem is None, problem
    assert validator is not None
    committed = json.loads(Path(_SCHEMA_PATH).read_text(encoding="utf-8"))
    assert validator.__class__ is validator_for(committed)
    assert validator.is_valid(
        {
            "apiVersion": "bundle/v1",
            "resources": {"jobs": {"nightly": {"tasks": [{"name": "a", "run": "b"}], "when": []}}},
        }
    )


@pytest.mark.parametrize("name", list(SCHEMA_GOOD))
def test_every_bundle_the_tool_writes_is_taken_by_its_shape(name: str, tmp_path: Path) -> None:
    """The known-good half: a bundle written the way the tool writes one -- an
    anchor and a merge, a block scalar, a task that adds the optional keys it may
    add -- is named by nothing. A validator that reads the shape as its own value,
    or that holds the node tree instead of the loaded document to it, finds
    violations here."""
    text = SCHEMA_GOOD[name]
    path = _file(tmp_path, name, text)
    assert _report(path) == []
    assert _run([path]) == (0, [], "")
    assert _run([path], schema=_SCHEMA_PATH) == (0, [], "")


def test_a_job_can_take_a_key_it_needs_from_a_merge(tmp_path: Path) -> None:
    """`as the safe loader loads it (merge keys applied)`, from the side where that
    matters: this job writes no `tasks` of its own, takes it from its preset, and is
    a job the shape takes. Hold the nodes rather than the loaded document to the
    shape and this file is refused; check the document the loader built and it is
    not. That difference is the whole difference between the two, and it is shown
    here on one file."""
    name = "a-job-that-merges-a-preset"
    text = SCHEMA_GOOD[name]
    path = _file(tmp_path, name, text)
    validator = _bundle_validator()
    assert _report(path) == []
    written = _keys_written(_documents(text)[0], ["resources", "jobs", "nightly"])
    assert written == ["<<"], written
    other = SCHEMA_GOOD["a-job-that-writes-again-a-key-its-preset-brings"]
    assert _report(_file(tmp_path, "override", other)) == []
    assert _keys_written(_documents(other)[0], ["resources", "jobs", "nightly"]) == [
        "<<",
        "when",
        "tasks",
    ]
    job = _loaded(other)[0]["resources"]["jobs"]["nightly"]
    assert job["when"] == ["on-push"], job
    assert job["tasks"] == [
        {"name": "fetch", "run": "curl -sf https://example.invalid/nightly", "timeout": 5}
    ]
    job = _loaded(text)[0]["resources"]["jobs"]["nightly"]
    assert job["tasks"] == [{"name": "build", "run": "npm run build"}]
    # the same job without what the merge brings, which is what its own keys are, is
    # the job the shape refuses: the known-bad fixture is refused for the `run` a task
    # does not write, and this one would be refused for the `tasks` it does not write.
    job_without_the_preset = _loaded(text)[0]
    job_without_the_preset["resources"]["jobs"]["nightly"] = {"when": ["nightly"]}
    lacking = _errors(job_without_the_preset, validator)
    assert [list(error.path) for error in lacking] == [["resources", "jobs", "nightly"]], lacking
    assert [error.message for error in lacking] == ["'tasks' is a required property"], lacking


# Every known-bad file: every violation, where it is, and what it is called.


@pytest.mark.parametrize("name", list(SCHEMA_BAD))
def test_a_document_that_does_not_match_the_schema_is_refused_at_the_node_and_path_it_is_about(
    name: str, tmp_path: Path
) -> None:
    """Every violation of every known-bad bundle, in the report exactly once, at the
    line and column in the whole file where its own node starts, with its document
    path in front of the schema's sentence about it, and with the run exiting 1. A
    report built from the schema's keywords instead of the node the violation is
    about, or from a validator that validates the wrong thing, is caught here in the
    half that says where, and in the half that says what."""
    text = SCHEMA_BAD[name][0]
    expects = SCHEMA_BAD[name][1]
    path = _file(tmp_path, name, text)
    code, problems, raw = _run([path], schema=_SCHEMA_PATH)
    assert code == 1, raw
    assert len(problems) == len(expects), raw
    for expect in expects:
        front = f"{path}:{expect.line}:{expect.column}: "
        hits = [line for line in problems if line.startswith(front) and expect.says in line]
        assert len(hits) == 1, (expect, raw)


def test_a_violation_is_read_from_the_document_down_through_every_key_and_index(
    tmp_path: Path,
) -> None:
    """`resources.jobs.nightly.tasks[1].timeout`: every mapping key of the path in
    order, every sequence item of it as an index, joined by dots, and the position
    of the node the whole path lands on. A path taken from `parts[0]` alone, or one
    that names a key but not the index it sits at, prints something else here."""
    name = "violation-deep-in-a-document"
    text = SCHEMA_BAD[name][0]
    path = _file(tmp_path, name, text)
    code, problems, raw = _run([path], schema=_SCHEMA_PATH)
    assert code == 1, raw
    assert problems == [
        f"{path}:9:20: resources.jobs.nightly.tasks[1].timeout: -1 is less than the minimum of 0",
        f"{path}:10:15: resources.jobs.nightly.tasks[1].run: None is not of type 'string'",
    ], raw


def test_a_missing_required_key_in_a_sequence_item_is_named_at_the_item_that_lacks_it(
    tmp_path: Path,
) -> None:
    """A task with no `name`, as the second item of a sequence, in the document that
    starts on the third line of the file: the item is what the rule is about, so the
    path ends at the index of that item and the position is the `-` it is written
    after, counted from the start of the file. A location read from the document's
    own marks rather than the file's puts it at line 8 here."""
    name = "missing-required-key-in-a-sequence-item"
    text = SCHEMA_BAD[name][0]
    path = _file(tmp_path, name, text)
    assert _lines(path) == [
        f"{path}:10:11: resources.jobs.nightly.tasks[1]: 'name' is a required property"
    ]


def test_a_key_the_schema_does_not_allow_is_named_in_the_problem_about_the_mapping_that_writes_it(
    tmp_path: Path,
) -> None:
    """The defect this option exists to catch, spelled as a person spells it: the
    task writes `nmae`, so the problem names `nmae` as the key that was unexpected,
    and the same node is named again for the `name` the file never wrote. Both
    violations are at the item, and neither is the key's own line and column."""
    name = "misspelt-key"
    text = SCHEMA_BAD[name][0]
    path = _file(tmp_path, name, text)
    code, problems, raw = _run([path], schema=_SCHEMA_PATH)
    assert code == 1, raw
    assert problems == [
        f"{path}:6:11: resources.jobs.nightly.tasks[0]: 'name' is a required property",
        f"{path}:6:11: resources.jobs.nightly.tasks[0]: "
        "Additional properties are not allowed ('nmae' was unexpected)",
    ], raw


def test_a_document_with_two_violations_of_one_shape_is_refused_twice(tmp_path: Path) -> None:
    """Two violations of one mapping, both about the same node, both printed: a
    required key the node does not write and a key it should not write. A check that
    stops after the first violation per node, or per document, prints one line
    here."""
    name = "misspelt-key"
    text = SCHEMA_BAD[name][0]
    path = _file(tmp_path, name, text)
    problems = _report(path)
    assert len(problems) == 2, problems
    assert {(one[0], one[1]) for one in problems} == {(6, 11)}, problems
    assert [one[2] for one in problems] == [
        "resources.jobs.nightly.tasks[0]: 'name' is a required property",
        "resources.jobs.nightly.tasks[0]: Additional properties are not allowed "
        "('nmae' was unexpected)",
    ], problems


def test_a_violation_brought_in_by_a_merge_is_at_where_the_merged_value_is_written(
    tmp_path: Path,
) -> None:
    """A task that never writes the `timeout` that fails the minimum: its preset
    writes it, and the task merges it in. So the violation carries the path of the
    task that ends up holding it -- `resources.jobs.nightly.tasks[0].timeout` -- and
    is at the line the merged value is written on, the preset's line, and not at the
    `<<`. A shape run on the node trees sees a task with no `timeout` at all and
    refuses nothing."""
    name = "violation-carried-in-by-a-merge"
    text = SCHEMA_BAD[name][0]
    path = _file(tmp_path, name, text)
    assert _lines(path) == [
        f"{path}:4:14: resources.jobs.nightly.tasks[0].timeout: -5 is less than the minimum of 0"
    ]
    assert _run([path], schema=_SCHEMA_PATH)[0] == 1


def test_a_violation_on_a_key_a_task_writes_again_over_its_merge_is_at_its_own_value(
    tmp_path: Path,
) -> None:
    """The other half of a merge: the task merges a preset's `timeout: 30`, then
    writes `timeout: -5` itself. The loader builds -5, the value the task wrote, so
    the violation is at line 12, where -5 is written, and never at the preset's
    line 4, which holds a valid 30. Known-bad (a review mutant that survived the
    suite): `_locate` keeping a key's first entry, the merged one, put it at 4:14.
    """
    text = (
        "apiVersion: bundle/v1\n"
        "presets:\n"
        "  task-defaults: &task-defaults\n"
        "    timeout: 30\n"
        "resources:\n"
        "  jobs:\n"
        "    nightly:\n"
        "      tasks:\n"
        "        - name: fetch\n"
        "          run: curl -sf https://example.invalid/nightly\n"
        "          <<: *task-defaults\n"
        "          timeout: -5\n"
    )
    path = _file(tmp_path, "a-task-that-writes-again-a-key-its-preset-brings", text)
    assert _lines(path) == [
        f"{path}:12:20: resources.jobs.nightly.tasks[0].timeout: -5 is less than the minimum of 0"
    ]


def test_a_violation_at_the_document_itself_is_named_at_the_document_that_lacks_the_key(
    tmp_path: Path,
) -> None:
    """`resources` is required and this file never writes it, so the violation's own
    path is empty and the line names no document path in front of the problem, at the
    line the document starts on. An empty path rendered as a lonely `:` or `.:` makes
    a line a reader cannot follow, and a path built from the schema's own keyword
    names a key the file does not have."""
    name = "missing-a-key-the-document-itself-needs"
    text = SCHEMA_BAD[name][0]
    path = _file(tmp_path, name, text)
    assert _lines(path) == [f"{path}:1:1: 'resources' is a required property"]


def test_a_document_that_is_not_an_object_is_refused_at_the_whole_document(tmp_path: Path) -> None:
    """A document that is a sequence fails the rule the shape states for the
    document itself, at the line and column its node starts on, in the second
    document of the file. Its path is the document's own, which is no path at all,
    so the problem is the schema's sentence with nothing in front of it."""
    name = "a-document-that-is-a-list"
    text = SCHEMA_BAD[name][0]
    path = _file(tmp_path, name, text)
    assert _lines(path) == [f"{path}:9:1: {SCHEMA_BAD[name][1][0].says}"]


def test_the_violations_of_one_document_are_reported_in_the_order_the_file_writes_them(
    tmp_path: Path,
) -> None:
    """The order of the report is the order of the file, not the order the checks
    reached the problems in. This document's key problem is at line 8 and its shape
    problem at line 1, and the key rule is the one that runs first, so the two
    readings of the same file differ: a `sorted` dropped from `check_text`, or read
    backwards, puts them the other way round while every count still matches."""
    name = "the-key-rule-and-the-shape-rule-on-one-document"
    text = SCHEMA_BAD[name][0]
    path = _file(tmp_path, name, text)
    problems = _lines(path)
    assert problems == [
        f"{path}:1:13: apiVersion: 'bundle/v2' is not one of ['bundle/v1']",
        f"{path}:8:5: duplicate key 'nightly': it is written twice in this mapping, "
        "the first at line 4",
    ]
    assert [_place(one) for one in problems] == sorted(_place(one) for one in problems)
    deep = _file(tmp_path, "deep", SCHEMA_BAD["violation-deep-in-a-document"][0])
    places = [_place(one) for one in _lines(deep)]
    assert places == sorted(places), places
    assert places[0] < places[1], places


def test_a_multi_document_file_holds_a_violation_in_each_document_in_order(
    tmp_path: Path,
) -> None:
    """Every violation, in every document: this file's four documents each hold two
    violations of their shape, and every one of them is printed, in the order the
    file writes them, across the document boundaries. A document loop that returns
    at the first problem it finds, or that checks only the first document of a
    stream, prints a part of this and still exits 1, so the count and the positions
    are both checked here."""
    name = "violation-in-each-document-of-a-file"
    text = SCHEMA_BAD[name][0]
    path = _file(tmp_path, name, text)
    code, problems, raw = _run([path], schema=_SCHEMA_PATH)
    assert code == 1, raw
    assert len(problems) == 6, raw
    assert problems == [
        f"{front}: {saying}"
        for front, saying in zip(
            [
                f"{path}:2:12",
                f"{path}:4:13",
                f"{path}:9:11",
                f"{path}:12:12",
                f"{path}:14:13",
                f"{path}:19:11",
            ],
            [
                "resources: 'jobs' is a required property",
                "apiVersion: 'bundle/v2' is not one of ['bundle/v1']",
                "resources.jobs.nightly.tasks[0]: 'run' is a required property",
                "resources: 'jobs' is a required property",
                "apiVersion: 'bundle/v2' is not one of ['bundle/v1']",
                "resources.jobs.nightly.tasks[0]: 'run' is a required property",
            ],
            strict=True,
        )
    ], raw
    assert _starts(text) == [1, 4, 11, 14], raw


def test_a_document_with_more_than_one_way_to_fail_the_shape_is_refused_for_each(
    tmp_path: Path,
) -> None:
    """Two violations in one document at different depths, and a third document of
    the same file with its own: every one of them is named, at its own node. A loop
    that takes the first violation of a document, or the first document of a file,
    is named by the count here as much as by the positions."""
    name = "violation-in-each-document-of-a-file"
    text = SCHEMA_BAD[name][0]
    path = _file(tmp_path, name, text)
    problems = _report(path)
    assert len(problems) == 6, problems
    assert len({(one[0], one[1]) for one in problems}) == 6, problems
    validator = _bundle_validator()
    names = [_path_of(error.absolute_path) for error in _errors(_loaded(text)[1], validator)]
    assert names == ["apiVersion", "resources.jobs.nightly.tasks[0]"], names
    assert all(any(name in one[2] for one in problems) for name in names), problems


def test_the_lines_and_columns_a_violation_is_reported_at_are_those_of_the_node_the_path_lands_on(
    tmp_path: Path,
) -> None:
    """Both halves of every violation's path, in front of the report: where the path
    stops if the walk takes only its first part, where it lands when it takes all of
    it, and which half the report answers with. It answers with the deep one. Only
    the deep halves differ from one violation to the next, which is what a locator
    reading `parts[0]` as the key -- or a document mark read for the node -- turns
    into several violations reported at one place. Positions are counted from the
    whole file: this file's second document has its own shape problems, at the lines
    a person counts from line 1."""
    name = "violation-deep-in-a-document"
    text = SCHEMA_BAD[name][0]
    path = _file(tmp_path, name, text)
    validator = _bundle_validator()
    document = _documents(text)[0]
    errors = _errors(_loaded(text)[0], validator)
    assert len(errors) == 2, errors
    shallow = [_node_at(document, list(error.absolute_path)[:1]) for error in errors]
    deep = [_node_at(document, list(error.absolute_path)) for error in errors]
    assert len(set(shallow)) == 1, shallow
    assert len(set(deep)) == 2, deep
    assert set(shallow) != set(deep), (shallow, deep)
    problems = _lines(path)
    assert [_place(one) for one in problems] == sorted(deep), (problems, shallow, deep)


def test_the_shape_rule_and_the_key_rule_refuse_one_document_for_their_own_reasons(
    tmp_path: Path,
) -> None:
    """Two checks over one document, one report. This document's `nightly` is written
    twice, which the key rule refuses, and its `apiVersion` is a value the shape
    refuses; the run names both, counts them, and refuses neither rule's problem
    because the other spoke. The same file without a shape is refused for the key
    rule alone, with the count differing the way the rules differ."""
    name = "the-key-rule-and-the-shape-rule-on-one-document"
    text = SCHEMA_BAD[name][0]
    path = _file(tmp_path, name, text)
    without = _report(path, schema=None)
    with_shape = _report(path)
    assert without == [(8, 5, SCHEMA_BAD[name][1][1].says)], without
    assert len(with_shape) == 2, with_shape
    assert any("apiVersion: 'bundle/v2' is not one of" in one[2] for one in with_shape), with_shape
    assert any("duplicate key 'nightly'" in one[2] for one in with_shape), with_shape
    assert [_place(one) for one in _lines(path)] == [(1, 13), (8, 5)], with_shape


def test_without_a_schema_the_command_only_parses_as_before(tmp_path: Path) -> None:
    """Both halves of the option's own line. Every bundle this module refuses for its
    shape is taken when no shape is given, because nothing about its shape is looked
    at, and every file #156a refuses is still refused with `--schema` in the run;
    the known-good bundles pass either way. A `validator` consulted even when the
    option was not given refuses bundles a person is not being asked about."""
    for name in SCHEMA_BAD:
        path = _file(tmp_path, name, SCHEMA_BAD[name][0])
        code, problems, raw = _run([path])
        if name == _KEY_RULE_FIXTURE:
            assert code == 1, (name, raw)
            assert problems == [f"{path}:8:5: {SCHEMA_BAD[name][1][1].says}"], (name, raw)
            continue
        assert code == 0, (name, raw)
        assert problems == [], (name, raw)
    for name in SCHEMA_GOOD:
        path = _file(tmp_path, name, SCHEMA_GOOD[name])
        assert _run([path]) == (0, [], ""), name
    path = _file(
        tmp_path, "key-rule", SCHEMA_BAD["the-key-rule-and-the-shape-rule-on-one-document"][0]
    )
    assert _run([path])[0] == 1
    assert _run([path], schema=_SCHEMA_PATH)[0] == 1


def test_every_problem_the_parse_key_and_tag_rules_refuse_is_refused_with_a_schema_too(
    tmp_path: Path,
) -> None:
    """A shape takes nothing away. Every known-bad file of #156a, every one of its
    refusals, with the bundle shape in front of the run: each is named, at the line
    and column it was named at before, and the report holds exactly as many problems
    as the same file raises through the check with the same schema -- not more, and
    not the shape's problems in place of the file's own."""
    validator = _bundle_validator()
    for name, (text, refusals) in BAD.items():
        path = _file(tmp_path, name, text)
        code, problems, raw = _run([path], schema=_SCHEMA_PATH)
        assert code == 1, (name, raw)
        for one in refusals:
            front = f"{path}:{one.line}:{one.column}: "
            assert any(line.startswith(front) and one.says in line for line in problems), (
                name,
                one,
                raw,
            )
        assert len(problems) == len(check_file(path, validator)), (name, raw, problems)


def test_a_document_the_safe_loader_cannot_build_is_never_held_against_the_shape(
    tmp_path: Path,
) -> None:
    """The shape is run on the documents the loader builds, and these documents are
    never built -- an unsafe tag, a merge of a scalar, a collection as a key, a
    duplicate key no mapping can hold twice. Each keeps exactly the problems it has
    without a shape, in the same order, and no problem about a shape is added for a
    document that does not exist. `construct_document` answering with a broken value
    instead of leaving the document out is what this refuses."""
    checked = 0
    for name, (text, _) in BAD.items():
        if not _safe_loader_refuses(text):
            continue
        checked += 1
        path = _file(tmp_path, name, text)
        validator = _bundle_validator()
        assert _report(path) == _report(path, schema=None), (name, text)
        assert len(check_file(path, validator)) == len(check_file(path, None)), name
        assert _run([path], schema=_SCHEMA_PATH)[1] == _run([path])[1], name
    assert checked > 1, "no fixture of #156a has a document the safe loader refuses"


def test_the_documents_a_shape_is_run_against_are_the_documents_the_loader_builds(
    tmp_path: Path,
) -> None:
    """What a run holds to the shape, read back rather than assumed: every document of
    the file, in the order the file writes them, each one the value the safe loader
    builds for it. The violations this file reports are exactly the violations those
    loaded documents have, counted per document and in document order -- including the
    value a document ends up holding only because a merge put it there, which the node
    tree does not hold at all. `yamlcheck._documents` composed with the loader that
    does not flatten merges reports a task with no `run` here instead of the violation
    the preset brings in, and a run that hands the shape one document for a whole file
    cannot match these counts."""
    name = "violation-in-each-document-of-a-file"
    text = SCHEMA_BAD[name][0]
    path = _file(tmp_path, name, text)
    validator = _bundle_validator()
    per_document = [len(_errors(document, validator)) for document in _loaded(text)]
    assert per_document == [1, 2, 1, 2], per_document
    assert sum(per_document) == len(_report(path)) == 6, per_document
    assert _starts(text) == [1, 4, 11, 14], text
    merge = "violation-carried-in-by-a-merge"
    merge_text = SCHEMA_BAD[merge][0]
    merge_path = _file(tmp_path, "merge", merge_text)
    assert _report(merge_path) == [
        (4, 14, "resources.jobs.nightly.tasks[0].timeout: -5 is less than the minimum of 0")
    ]
    assert len(_errors(_loaded(merge_text)[0], validator)) == 1
    written = _keys_written(_documents(merge_text)[0], ["resources", "jobs", "nightly", "tasks", 0])
    assert "timeout" not in written, written
    assert _loaded(merge_text)[0]["resources"]["jobs"]["nightly"]["tasks"][0]["timeout"] == -5


def test_a_tag_the_schema_is_never_asked_about_keeps_its_own_problem(tmp_path: Path) -> None:
    """A file whose first document the safe loader will not build, followed by a bundle
    the shape refuses: the tag keeps the problem the tag rules give it, the bundle keeps
    the problem the shape gives it, and the shape is never run on a document that does
    not exist. The positions are read from the document boundaries, which is the same
    file read a second way."""
    text = SCHEMA_BAD["violation-carried-in-by-a-merge"][0]
    for tag in ("!!python", "!!python/name:yaml.SafeLoader", "!!name"):
        named = f"{tag} nothing\n---\n{text}"
        path = _file(tmp_path, f"tagged-{tag[-12:]}", named)
        problems = _report(path)
        assert any("unsafe tag" in one[2] for one in problems if one[0] == 1), (tag, problems)
        assert (6, 14) in {(one[0], one[1]) for one in problems}, (tag, problems)
        assert _starts(named) == [1, 3], named
        with_shape = _run([path], schema=_SCHEMA_PATH)[1]
        without = _run([path])[1]
        assert len(with_shape) == len(without) + 1, (tag, with_shape, without)
        assert without == [
            f"{path}:{one[0]}:{one[1]}: {one[2]}" for one in _report(path, schema=None)
        ], (tag, without)


def test_the_shape_a_file_is_held_to_is_the_one_the_option_names(
    tmp_path: Path,
) -> None:
    """Three schema files, three shapes, one YAML file each time: what a run refuses
    comes from the file `--schema` was given, and nothing else is consulted. A shape
    that names no constraint takes every bundle here; a shape that allows only
    `apiVersion` takes a bundle that has more than that; a shape that allows nothing
    else refuses a bundle the committed shape takes. A validator left over from the
    committed shape, or one compiled at import, makes one line of this fail."""
    deep = _file(tmp_path, "deep", SCHEMA_BAD["violation-deep-in-a-document"][0])
    good = _file(tmp_path, "good", SCHEMA_GOOD["bundle-as-the-tool-writes-it"])
    listy = _file(tmp_path, "listy", SCHEMA_BAD["a-document-that-is-a-list"][0])
    for name in SCHEMAS:
        schema = _schema_file(tmp_path, name)
        if name == "any":
            assert _run([deep], schema=schema) == (0, [], ""), name
            assert _run([good], schema=schema) == (0, [], ""), name
        elif name == "api-version-only":
            assert _run([deep], schema=schema) == (0, [], ""), name
            assert _run([good], schema=schema) == (0, [], ""), name
            assert _run([listy], schema=schema)[0] == 1, name
        else:
            assert _run([deep], schema=schema)[0] == 1, name
            assert _run([good], schema=schema)[0] == 1, name
            assert _lines(good, schema=schema) == [
                f"{good}:1:1: Additional properties are not allowed "
                "('presets', 'resources' were unexpected)"
            ], name
    assert _run([good], schema=_SCHEMA_PATH) == (0, [], "")


def test_the_shape_of_a_file_is_the_shape_of_the_run_not_of_the_first_file(
    tmp_path: Path,
) -> None:
    """Several files, one shape: every file is held to the schema the run was given,
    in the order the files were given, each problem printed with the file it was
    found in as that file was named, and the run's exit code refuses the whole set.
    A validator built for one file and carried into the next, or a loop that stops at
    the first file the shape refuses, changes the count or the names here."""
    bad = _file(tmp_path, "bad", SCHEMA_BAD["misspelt-key"][0])
    worse = _file(tmp_path, "worse", SCHEMA_BAD["missing-required-key-in-a-sequence-item"][0])
    good = _file(tmp_path, "good", SCHEMA_GOOD["bundle-as-the-tool-writes-it"])
    merge = _file(tmp_path, "merge", SCHEMA_BAD["violation-carried-in-by-a-merge"][0])
    code, problems, raw = _run([good, bad, worse, merge], schema=_SCHEMA_PATH)
    assert code == 1, raw
    assert len(problems) == 2 + 1 + 1, raw
    assert [_front(one) for one in problems[:2]] == [f"{bad}:6:11", f"{bad}:6:11"], raw
    assert _front(problems[2]) == f"{worse}:10:11", raw
    assert _front(problems[3]) == f"{merge}:4:14", raw
    assert good not in raw, raw


def test_a_schema_file_that_is_not_usable_is_named_before_any_yaml_file_is_checked(
    tmp_path: Path,
) -> None:
    """Every way a `--schema` file can fail to be a schema -- it is not there, it is
    not text, it is not JSON, it is JSON that is not a schema -- is named as the
    problem of the file `--schema` named, on one line, with nothing printed about the
    YAML files, and the run refused. A validator that answers `None` for one takes
    every document, which is the run that reports a shape check it never did."""
    targets = [_file(tmp_path, name, SCHEMA_GOOD[name]) for name in SCHEMA_GOOD]
    missing = tmp_path / "no-such-schema.json"
    binary = tmp_path / "binary.json"
    binary.write_bytes(b"\xff\xfe\x00not JSON\x00\n")
    cases = [
        (str(missing), "is not a JSON Schema I can read"),
        (str(binary), "is not a JSON Schema I can read"),
    ]
    for name, body in BAD_SCHEMAS.items():
        written = tmp_path / f"{name}.json"
        written.write_text(body, encoding="utf-8")
        cases.append((str(written), "is not a JSON Schema"))
    cases.append((_schema_file(tmp_path, "api-version-only"), ""))
    for schema, expectation in cases:
        code, problems, raw = _run(targets, schema=schema)
        if expectation == "":
            assert code == 0, (schema, raw)
            assert problems == [], (schema, raw)
            continue
        assert code == 1, (schema, raw)
        assert len(problems) == 1, (schema, raw)
        assert problems[0].startswith(f"{schema}: {expectation}: "), (schema, raw)
        assert problems[0].splitlines() == [problems[0]], (schema, raw)
    _ = targets
    assert _run(targets)[0] == 0


def test_load_schema_compiles_the_draft_a_schema_file_names(tmp_path: Path) -> None:
    """The validator is the one for the draft the schema file names in `$schema`, and
    the latest draft when it names none: named draft-07 is read by draft-07's
    validator, and a file that names none by the one `validator_for` picks, which is
    what makes `$defs` and `prefixItems` mean what their draft says they mean.
    `validator_for(schema)` replaced by one draft class, or `check_schema` skipped so
    that a non-schema stands as a rule, is caught here and by
    `test_a_schema_file_that_is_not_usable_is_named_before_any_yaml_file_is_checked`."""
    named = tmp_path / "draft-07.json"
    body = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "$defs": {"name": {"type": "string", "minLength": 1}},
        "type": "object",
        "additionalProperties": False,
        "properties": {"apiVersion": {"$ref": "#/$defs/name"}},
    }
    named.write_text(json.dumps(body), encoding="utf-8")
    draft7, problem = load_schema(str(named))
    assert problem is None, problem
    assert draft7 is not None
    assert draft7.__class__ is validator_for(body)
    assert draft7.is_valid({"apiVersion": "bundle/v1"})
    assert not draft7.is_valid({"apiVersion": ""})
    assert not draft7.is_valid({"apiVersion": "bundle/v1", "resources": {}})

    latest = tmp_path / "no-draft.json"
    latest_body = {
        "type": "object",
        "required": ["apiVersion"],
        "properties": {"jobs": {"type": "array", "prefixItems": [{"type": "string"}]}},
    }
    latest.write_text(json.dumps(latest_body), encoding="utf-8")
    second, problem = load_schema(str(latest))
    assert problem is None, problem
    assert second is not None
    assert second.__class__ is validator_for(latest_body)
    assert second.__class__ is not draft7.__class__
    assert second.is_valid({"apiVersion": "bundle/v1", "jobs": ["one", 2]})
    assert not second.is_valid({"apiVersion": "bundle/v1", "jobs": [2, "one"]})

    for name, body_text in BAD_SCHEMAS.items():
        broken = tmp_path / f"{name}-draft.json"
        broken.write_text(body_text, encoding="utf-8")
        validator, problem = load_schema(str(broken))
        assert validator is None, problem
        assert problem is not None, body_text
        assert str(problem).startswith(f"{broken}: is not a JSON Schema: "), problem


def test_the_shape_check_is_run_once_for_every_document_of_every_file_it_was_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wiring the outcome tests cannot see: one check per document, for every
    document of every file the run names, each with the file's own path, that
    file's own document, and the one validator the option named. These replace the
    arguments with what they have to be -- the documents of this file in this file's
    order, and the validator read back out of the stand-in -- so a validator built
    per file or per document, a document from another file, or a check that runs
    once for a stream and never reaches the rest of it is named here, in arguments,
    not as an outcome. A whole suite of outcome tests passed while `--schema` had a
    validator built once per file and never handed to the documents, which is what
    this test exists to refuse."""
    text = SCHEMA_BAD["violation-in-each-document-of-a-file"][0]
    real = yamlcheck._schema_problems
    seen: list[tuple[str, int, Any]] = []

    def counting(loader: Any, path: str, document: Any, validator: Any) -> list[YamlProblem]:
        seen.append((path, int(document.start_mark.line), validator))
        return real(loader, path, document, validator)

    monkeypatch.setattr(yamlcheck, "_schema_problems", counting)
    here = _file(tmp_path, "here", text)
    there = _file(tmp_path, "there", SCHEMA_GOOD["bundle-as-the-tool-writes-it"])
    code, problems, raw = _run([here, there], schema=_SCHEMA_PATH)
    assert code == 1, raw
    assert len(problems) == 6, raw
    starts = [int(node.start_mark.line) for node in _documents(text)]
    assert starts[0] == 0, starts
    assert len(set(starts)) == len(starts) == 4, starts
    assert [(name, line) for name, line, _ in seen] == [
        *[(here, line) for line in starts],
        (there, 0),
    ], seen
    assert all(used is not None for _, _, used in seen), seen
    assert len({id(used) for _, _, used in seen}) == 1, seen
    assert len(seen) == 5, seen
    assert [name for name, _, _ in seen].count(there) == 1, seen


def test_a_shape_check_that_breaks_is_a_problem_of_its_own_for_the_file_it_was_asked_about(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The walk is defensive because a person wrote the text; the shape check is a
    library call over a document that already built, and a failure inside it is
    neither a clean file nor a broken file to be mistaken. So the file is refused
    with the failure named on one line, and the files after it are still checked.
    The only test that reaches the last line of `check_file`, because every other
    one hands it text that never gets that far."""
    failure = "the shape walk gave up"

    def broken(loader: Any, path: str, document: Any, validator: Any) -> list[YamlProblem]:
        del loader, document, validator
        message = f"{path}: {failure}"
        raise ValueError(message)

    monkeypatch.setattr(yamlcheck, "_schema_problems", broken)
    here = _file(tmp_path, "here", SCHEMA_GOOD["bundle-as-the-tool-writes-it"])
    there = _file(tmp_path, "there", SCHEMA_BAD["misspelt-key"][0])
    validator = _validator_of(_SCHEMA_PATH)
    problems = check_file(here, validator)
    assert len(problems) == 1, problems
    assert "cannot be checked" in problems[0].message, problems
    assert failure in problems[0].message, problems
    assert problems[0].line is None, problems
    assert problems[0].column is None, problems
    assert str(problems[0]).splitlines() == [str(problems[0])], problems
    code, reported, raw = _run([here, there], schema=_SCHEMA_PATH)
    assert code == 1, raw
    assert len(reported) == 2, raw
    assert here in raw, raw
    assert there in raw, raw


def test_the_command_takes_the_shape_from_the_option_and_refuses_the_files_it_checks(
    tmp_path: Path,
) -> None:
    """`--schema SCHEMA.json` on the command line, as an option with a value, reaching
    the check as a validator and refusing with exit 1. The help names both the option
    and what it is for, because `--schema` is a promise about which checks run. A
    `main` that builds the parser and never passes the value, or passes a path where a
    validator belongs, fails here at the run rather than inside the module."""
    out = io.StringIO()
    with redirect_stdout(out), pytest.raises(SystemExit) as raised:
        main(["yaml-check", "--help"], stdout=out)
    assert raised.value.code == 0, raised.value.code
    help_text = out.getvalue()
    assert "--schema" in help_text, help_text
    assert "JSON Schema" in help_text, help_text

    bad = _file(tmp_path, "bad", SCHEMA_BAD["misspelt-key"][0])
    good = _file(tmp_path, "good", SCHEMA_GOOD["bundle-as-the-tool-writes-it"])
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        assert main(["yaml-check", bad, good]) == 0
        assert main(["yaml-check", "--schema", _SCHEMA_PATH, bad, good]) == 1
    printed = [line for line in buffer.getvalue().splitlines() if line]
    assert len(printed) == 2, printed
    assert all(line.startswith(f"{bad}:6:11: ") for line in printed), printed
