"""`saddle yaml-check`: parse, duplicate-key and unsafe-tag checks for YAML files.

Contract, in three sentences:

1. `check_text` names a problem for every way a file fails, at the 1-based line
   and column where that problem starts in the whole file, and names none for a
   file PyYAML's own safe loader builds. `check_file` reads the file first, and a
   file that is not there, is not text, or breaks the check in some other way is a
   problem too, so no file ends `saddle yaml-check` in a traceback or in a pass.
2. One mapping may not write the same key twice. Two keys are the same key when
   they load to the same value, so `"name"` and `name` are one key and `1` and
   `"1"` are two; the second occurrence is what gets named. A key a merge brings
   in (`<<: *defaults`) is not written in the mapping, so overriding it is not a
   duplicate.
3. A tag the safe loader has no constructor for is refused by name at the node it
   tags, in key, value or document position; a tag it has a constructor for but
   cannot build from the text written (`!!int abc`, `!!bool yesno`) is refused the
   same way.

Known-good: a workflow that parses, a multi-document file, anchors and aliases, a
merge that overrides an anchored key, a `=` key, a value that points into itself,
`1` and `"1"` in one mapping, quoted and plain spellings of one key name, and an
empty file. Known-bad: text that does not parse, a key written twice (also nested
under a sequence, in a mapping that also merges, and inside a multi document
file), `!!python/object`, a custom tag such as `!Ref`, a tag from a `%TAG` prefix,
`!!int abc`, a `=` used as a value, a `<<` merge of a scalar, a collection used as
a key, an undefined alias, and a file with a key written twice inside a mapping
its tag already refuses. A file that is not there to read, and one that is not
text, are problems too, which no file text can hold and a test names directly.

Contract mutants, each one killed by a test in tests/test_yaml_check.py as
listed in that module's docstring.
"""

from __future__ import annotations

from collections.abc import Hashable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Final

from yaml.constructor import SafeConstructor  # type: ignore[import-untyped]
from yaml.error import YAMLError  # type: ignore[import-untyped]
from yaml.loader import SafeLoader  # type: ignore[import-untyped]
from yaml.nodes import MappingNode, ScalarNode, SequenceNode  # type: ignore[import-untyped]

#: The tags PyYAML's safe loader has a constructor for, read from the loader so
#: this check refuses what `yaml.safe_load` refuses and not a list typed out by
#: hand. `None` is the safe loader's own "this tag has no constructor", so it is
#: not a tag a file may use.
SAFE_TAGS: Final = frozenset(tag for tag in SafeConstructor.yaml_constructors if tag is not None)

#: The tag the resolver gives the key `<<`. It has no constructor: the safe
#: loader's `flatten_mapping` consumes it before the mapping is built, so it is
#: legal in that one place and nowhere else.
MERGE_TAG: Final = "tag:yaml.org,2002:merge"

#: The tag the resolver gives the plain scalar `=`. `flatten_mapping` reads it as
#: the string `=` in key position; the safe loader has no constructor for it
#: anywhere else.
VALUE_TAG: Final = "tag:yaml.org,2002:value"

#: What `flatten_mapping` retags `=` to, so that `=` and `"="` are one key.
STR_TAG: Final = "tag:yaml.org,2002:str"

#: The prefix of a tag in the YAML namespace, which is how such a tag reads when
#: it is written out: `tag:yaml.org,2002:map` is `!!map`.
YAML_NAMESPACE: Final = "tag:yaml.org,2002:"

#: `construct_mapping`'s own rule, which a key that composes to a collection
#: breaks: a dict key has to be hashable.
UNHASHABLE: Final = "the safe loader cannot use a {kind} as a key: it is not hashable"

#: What `_scalar` hands back for a scalar the safe loader cannot build, so that
#: it can never read as a key that loaded to something.
_NO_VALUE: Final = object()


@dataclass(frozen=True)
class YamlProblem:
    """One way one file fails: where in the file it fails, and why.

    A problem with no line and column is a whole-file failure, such as a file
    that is not there or is not text, so it is named by its path alone.
    """

    path: str
    message: str
    line: int | None = None
    column: int | None = None

    def __str__(self) -> str:
        if self.line is None or self.column is None:
            return f"{self.path}: {self.message}"
        return f"{self.path}:{self.line}:{self.column}: {self.message}"

    def order(self) -> tuple[str, int, int, str]:
        """Sort a file's problems by where they are in it, then by what they say."""
        return (self.path, self.line or 0, self.column or 0, self.message)


def display_tag(tag: str) -> str:
    """A tag as the file wrote it: `tag:yaml.org,2002:map` is `!!map`."""
    if tag.startswith(YAML_NAMESPACE):
        return f"!!{tag[len(YAML_NAMESPACE) :]}"
    return tag


def tag_is_safe(tag: str) -> bool:
    """Whether PyYAML's safe loader has a constructor for this tag."""
    return tag in SAFE_TAGS


def key_tag(tag: str) -> str:
    """The tag a mapping key is built under: `flatten_mapping`'s `=` becomes `!!str`."""
    return STR_TAG if tag == VALUE_TAG else tag


class _Document:
    """The checks one document's node tree goes through."""

    def __init__(self, loader: Any, path: str) -> None:
        self.loader = loader
        self.path = path
        self.problems: set[YamlProblem] = set()
        # Nodes whose walk is still open: an alias back to one is a value that
        # points into itself, which the safe loader builds as it is, so the walk
        # stops there rather than following it for ever.
        self.open: set[Any] = set()
        # Nodes already walked. An alias names one node, so the same mapping
        # named twice is checked once, however many times it is named.
        self.walked: set[Any] = set()

    def problems_of(self, node: Any) -> list[YamlProblem]:
        self._node(node)
        return sorted(self.problems, key=YamlProblem.order)

    def _fail(self, mark: Any, message: str) -> None:
        self.problems.add(YamlProblem(self.path, message, mark.line + 1, mark.column + 1))

    def _node(self, node: Any) -> None:
        """One node: its tag first, then everything under it."""
        if node in self.walked:
            return
        if node in self.open:
            # An alias back to a node this walk is inside is a value that points
            # into itself, which is a structure the safe loader builds as it is.
            # The walk stops here instead of following it for ever.
            return
        self.open.add(node)
        try:
            if not tag_is_safe(node.tag):
                self._fail(node.start_mark, f"unsafe tag {display_tag(node.tag)!r}")
            elif isinstance(node, MappingNode):
                self._mapping(node)
            elif isinstance(node, SequenceNode):
                for item in node.value:
                    self._node(item)
            else:
                self._scalar(node)
        finally:
            self.open.discard(node)
            self.walked.add(node)

    def _mapping(self, node: Any) -> None:
        """The keys as they are written, then everything under each value."""
        written: dict[tuple[str, Any], int] = {}
        for key_node, value_node in node.value:
            if key_node.tag == MERGE_TAG:
                # A merge writes no key of its own, so a key it brings in that
                # this mapping also writes is an override, not a duplicate.
                self._merge(value_node)
                continue
            key = self._key(key_node)
            if key is not None:
                first = written.get(key)
                if first is None:
                    written[key] = key_node.start_mark.line + 1
                else:
                    self._fail(
                        key_node.start_mark,
                        f"duplicate key {key[1]!r}: it is written twice in this mapping, "
                        f"the first at line {first}",
                    )
            self._node(value_node)

    def _merge(self, value_node: Any) -> None:
        """What a `<<` may name: a mapping, or a list of mappings to merge in.

        What it names is checked, because a merge can name a mapping written
        here and nowhere else (`<<: {a: 1, a: 2}`); a mapping named by an alias
        was checked where it was written, so naming it again adds nothing, and a
        merge that names the mapping it sits in is dissolved the same way
        `flatten_mapping` dissolves it rather than read as a cycle: a merge is
        not a value that contains itself.
        """
        if isinstance(value_node, MappingNode):
            self._merged(value_node)
            return
        if isinstance(value_node, SequenceNode):
            for item in value_node.value:
                if isinstance(item, MappingNode):
                    self._merged(item)
                else:
                    self._fail(
                        item.start_mark, f"a merge needs mappings to merge in, found a {item.id}"
                    )
            return
        self._fail(
            value_node.start_mark,
            f"a merge needs a mapping, or a list of mappings, found a {value_node.id}",
        )

    def _merged(self, node: Any) -> None:
        if node in self.open:
            return
        self._node(node)

    def _key(self, key_node: Any) -> tuple[str, Any] | None:
        """What this key is built from, shaped so two equal keys come out equal.

        `name` and `"name"` are built the same way and so name one key, while
        `1` and `"1"` are built differently and name two. None comes back when
        the key is itself a failure, so a bad key is named once and not twice.
        """
        tag = key_tag(key_node.tag)
        if not tag_is_safe(tag):
            self._fail(
                key_node.start_mark, f"unsafe tag {display_tag(key_node.tag)!r} in key position"
            )
            return None
        if not isinstance(key_node, ScalarNode):
            self._fail(key_node.start_mark, UNHASHABLE.format(kind=key_node.id))
            return None
        value = self._scalar(node_as(key_node, tag))
        if value is _NO_VALUE:
            return None  # `_scalar` already named what it could not build
        if not isinstance(value, Hashable):
            self._fail(key_node.start_mark, UNHASHABLE.format(kind=display_tag(tag)))
            return None
        return (type(value).__name__, value)

    def _scalar(self, node: Any) -> Any:
        """The value PyYAML's own safe constructor makes from this scalar.

        Anything the constructor raises, whatever it raises, is a value the safe
        loader cannot build, so it is a finding here and never a crash: `!!int
        abc` and `!!bool yesno` fail the same way a tag it has no constructor
        for does.
        """
        try:
            return self.loader.construct_object(node)
        except Exception as exc:
            reason = getattr(exc, "problem", None) or str(exc)
            self._fail(
                node.start_mark,
                f"the safe loader cannot build {display_tag(node.tag)!r} "
                f"from {node.value!r}: {reason}",
            )
            return _NO_VALUE


def node_as(node: Any, tag: str) -> Any:
    """A copy of one scalar node that is to be built under another tag.

    Only for a key the resolver tagged `!!value`: the safe loader retags that
    node in place, and a node named by an alias can be a key here and a value
    there, so this check copies instead.
    """
    if tag == node.tag:
        return node
    return ScalarNode(tag, node.value, node.start_mark, node.end_mark, node.style)


def check_text(path: str, text: str) -> list[YamlProblem]:
    """Every problem with one file's text, as it was read from `path`.

    A file is one YAML stream, so every document in it is checked. A stream that
    does not scan or parse stops at that problem: the loader cannot reach what
    follows the point it could not read.
    """
    try:
        loader: Any = SafeLoader(text)
        documents = list(_documents(loader))
    except YAMLError as exc:
        return [problem_of(path, exc, text)]
    problems: list[YamlProblem] = []
    for document in documents:
        problems.extend(_Document(loader, path).problems_of(document))
    return sorted(set(problems), key=YamlProblem.order)


def check_file(path: str) -> list[YamlProblem]:
    """Every problem with one file: read it, then check the text that is there.

    A file that is not there, is not text, or breaks the check in a way no YAML
    error covers is named as a problem, so a file never ends the command in a
    traceback and never ends it in a pass either.
    """
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except OSError as exc:
        return [YamlProblem(path, f"cannot be read: {exc.strerror or exc}")]
    except UnicodeDecodeError as exc:
        return [YamlProblem(path, f"is not UTF-8 text: {exc.reason}")]
    try:
        return check_text(path, text)
    except Exception as exc:
        return [YamlProblem(path, f"cannot be checked: {type(exc).__name__}: {exc}")]


def _documents(loader: Any) -> Iterator[Any]:
    """Every document of one stream, composed and not yet built."""
    while loader.check_node():
        yield loader.get_node()


def problem_of(path: str, exc: BaseException, text: str) -> YamlProblem:
    """The problem a loader raised, at the line and column it pointed at.

    A `ReaderError` names no mark, only where in the text the character that
    broke it is, so its character index becomes a line and a column here.
    """
    mark = getattr(exc, "problem_mark", None) or getattr(exc, "context_mark", None)
    if mark is not None:
        line, column = mark.line + 1, mark.column + 1
    else:
        index: int = getattr(exc, "position", None) or 0
        line = text.count("\n", 0, index) + 1
        column = index - text.rfind("\n", 0, index)
    context, problem = getattr(exc, "context", None), getattr(exc, "problem", None)
    message = (
        "; ".join(part.strip() for part in (context, problem) if part) or str(exc).splitlines()[0]
    )
    return YamlProblem(path, message, line, column)


def check_paths(paths: Sequence[str], *, stdout: IO[str]) -> int:
    """`saddle yaml-check PATH...`: print every problem with every file, and 1 if any.

    The path in each line is the path as it was given on the command line, so the
    line a person can paste into their shell is the line the problem is in.
    """
    failed = False
    for path in paths:
        problems = check_file(path)
        if problems:
            failed = True
            for problem in problems:
                print(problem, file=stdout)
    return 1 if failed else 0
