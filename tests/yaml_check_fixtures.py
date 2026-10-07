"""The YAML files `saddle yaml-check` is judged on, as the text of each file.

GOOD is the set of files the check must take: each is a file PyYAML's own safe
loader builds, so a fixture `safe_load` itself refuses would pin a refusal no
file deserves. BAD is the set it must refuse, with one `Refusal` per problem the
report has to name: the 1-based line and column in the whole file, and a needle
the message must contain. A check that refuses the right thing at the wrong
place, or names two problems where one is written, fails here.

`_file` writes a file line by line, so a line number in a `Refusal` is a line a
person counts in the file they opened. The one fixture that is not `\n`-separated,
the CRLF file, says so in its name.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


def _file(*lines: str) -> str:
    """One text file, from the lines it holds."""
    return "".join(f"{line}\n" for line in lines)


@dataclass(frozen=True)
class Refusal:
    """What the report must say about one file: where the problem is, and a needle it names."""

    line: int
    column: int
    says: str


GOOD: Final[dict[str, str]] = {
    "workflow": _file(
        "name: CI",
        "on:",
        "  push:",
        "    branches: [main]",
        "  schedule:",
        "    - cron: '0 6 * * *'",
        "jobs:",
        "  build:",
        '    name: "Build and test"',
        "    runs-on: ubuntu-latest",
        "    steps:",
        "      - uses: actions/checkout@v4",
        "      - run: |",
        "          ./check.sh",
        "          echo done",
    ),
    "multi-document": _file(
        "cluster: north",
        "replicas: 3",
        "---",
        "defaults: &defaults",
        "  region: eu",
        "  tier: standard",
        "---",
        "- one",
        "- two",
    ),
    "anchors-and-aliases": _file(
        "defaults: &defaults",
        "  region: eu",
        "  tier: standard",
        "extra: &extra",
        "  tier: premium",
        "  count: 2",
        "job:",
        "  <<: *defaults",
        "  <<: *extra",
        "  region: us",
        "copy:",
        "  <<: *defaults",
        "  steps: [one, two]",
    ),
    "merging-the-mapping-itself": _file(
        # A `<<` that names the mapping it sits in is what `flatten_mapping`
        # dissolves, not a value that contains itself: `safe_load` builds this.
        "job: &job",
        "  <<: *job",
        "  name: build",
    ),
    "merging-the-same-anchor-twice": _file(
        "base: &base",
        "  tier: standard",
        "job:",
        "  <<: *base",
        "  <<: *base",
    ),
    "list-of-merges": _file(
        "base: &base",
        "  tier: standard",
        "more: &more",
        "  count: 2",
        "job:",
        "  <<: [*base, *more]",
    ),
    "empty-file": _file(),
    "only-a-comment": _file("# a pipeline that has no settings yet"),
    "quoted-and-plain-key-names": _file("alpha: 1", "'beta': 2", '"gamma": 3'),
    "equal-in-writing-unequal-in-loading": _file(
        "1: a",
        "'1': b",
        "true: c",
        "'true': d",
        "null: e",
        "'null': f",
        "1.5: g",
        "'1.5': h",
    ),
    "equals-sign-key": _file("=: 1", "name: build"),
    "flow-style": _file("{name: build, steps: [one, two], when: {branch: main}}"),
    "crlf-line-endings": "name: CI\r\njobs:\r\n  - name: build\r\n",
    "keys-in-another-language": _file("名称: CI", "ünicode: 1", "'ünicode key': 2"),
    "value-that-points-into-itself": _file(
        # An alias back to a node above it: `safe_load` builds that structure as
        # it is, so no problem is due and the walk does not follow it for ever.
        "job: &job",
        "  name: build",
        "  parent: *job",
    ),
}

BAD: Final[dict[str, tuple[str, tuple[Refusal, ...]]]] = {
    "duplicate-at-the-top-level": (
        _file("name: CI", "on: push", "name: Nightly"),
        (Refusal(3, 1, "duplicate key 'name'"),),
    ),
    "duplicate-written-quoted-and-plain": (
        _file("cluster: north", '"cluster": south'),
        (Refusal(2, 1, "duplicate key 'cluster'"),),
    ),
    "duplicate-three-ways-in-one-mapping": (
        _file("a: 1", "'a': 2", '"a": 3'),
        (Refusal(2, 1, "duplicate key 'a'"), Refusal(3, 1, "duplicate key 'a'")),
    ),
    "duplicate-under-a-sequence": (
        _file(
            "jobs:",
            "  - name: build",
            "    runs: 1",
            "    name: test",
            "  - name: package",
        ),
        (Refusal(4, 5, "duplicate key 'name'"),),
    ),
    "duplicate-in-a-multi-document-file": (
        _file("a: 1", "---", "b: 1", "b: 2", "---", "c: [1, 2]", "c: [3]"),
        (Refusal(4, 1, "duplicate key 'b'"), Refusal(7, 1, "duplicate key 'c'")),
    ),
    "duplicate-in-a-mapping-that-also-merges": (
        # The merge writes no key of its own, so it is the two written `tier`
        # keys that are one key written twice.
        _file(
            "defaults: &defaults",
            "  tier: standard",
            "job:",
            "  <<: *defaults",
            "  tier: premium",
            "  tier: gold",
        ),
        (Refusal(6, 3, "duplicate key 'tier'"),),
    ),
    "duplicate-key-with-a-type-tag": (
        _file("5: a", "!!int 5: b"),
        (Refusal(2, 1, "duplicate key 5"),),
    ),
    "duplicate-quoted-equals-key": (
        _file("=: 1", "'=': 2"),
        (Refusal(2, 1, "duplicate key '='"),),
    ),
    "duplicate-inside-a-mapping-a-merge-names-inline": (
        # The merge writes no key of its own, but the mapping it names is written
        # here, and it writes `a` twice. PyYAML's `flatten_mapping` never checks a
        # mapping it merges in, so `safe_load` builds this file: the refusal here
        # is the duplicate rule and nothing else.
        _file("job:", "  <<: {a: 1, a: 2}"),
        (Refusal(2, 14, "duplicate key 'a'"),),
    ),
    "duplicate-inside-a-mapping-in-a-merge-list": (
        # The same mapping written inside the list a merge merges in.
        _file("job:", "  <<: [{a: 1, a: 2}]"),
        (Refusal(2, 15, "duplicate key 'a'"),),
    ),
    "duplicate-in-a-mapping-named-by-two-aliases": (
        # `*base` names one mapping, written once, so its duplicate key is one
        # problem and not one per alias that names it.
        _file("base: &base", "  tier: standard", "  tier: premium", "a: *base", "b: *base"),
        (Refusal(3, 3, "duplicate key 'tier'"),),
    ),
    "a-command-the-unsafe-tag-would-run": (
        # Constructing this value would run something. The check names the tag and
        # never builds the value, so the test that reads this file prints nothing.
        _file("hook: !!python/object/apply:builtins.print", "  - this-ran"),
        (Refusal(1, 7, "unsafe tag '!!python/object/apply:builtins.print'"),),
    ),
    "unsafe-python-tag": (
        _file("hook: !!python/object/apply:os.system", "  - ls"),
        (Refusal(1, 7, "unsafe tag '!!python/object/apply:os.system'"),),
    ),
    "unsafe-custom-tag": (
        _file("Resources:", "  bucket: !Ref Bucket"),
        (Refusal(2, 11, "unsafe tag '!Ref'"),),
    ),
    "unsafe-tag-in-key-position": (
        _file("!Ref bucket: name"),
        (Refusal(1, 1, "unsafe tag '!Ref'"),),
    ),
    "unsafe-tag-from-a-tag-prefix": (
        _file("%TAG !e! tag:example.com,2000:app/", "---", "job: !e!ref bucket"),
        (Refusal(3, 6, "unsafe tag 'tag:example.com,2000:app/ref'"),),
    ),
    "unsafe-tag-on-the-document": (
        _file("--- !!mapx", "a: 1"),
        (Refusal(1, 5, "unsafe tag '!!mapx'"),),
    ),
    "a-duplicate-inside-a-node-the-tag-refuses": (
        # The document is refused at its tag, so nothing under it is built and
        # nothing under it is a second problem: the file is refused either way.
        _file("--- !!mapx", "tier: standard", "tier: premium"),
        (Refusal(1, 5, "unsafe tag '!!mapx'"),),
    ),
    "value-the-safe-loader-cannot-build": (
        _file("retries: 3", "count: !!int abc"),
        (Refusal(2, 8, "the safe loader cannot build '!!int' from 'abc'"),),
    ),
    "a-key-the-safe-loader-cannot-build": (
        # A key is built too: a safe tag whose text no constructor can build is a
        # failure once, in key position, and not a duplicate.
        _file("!!int abc: retries"),
        (Refusal(1, 1, "the safe loader cannot build '!!int' from 'abc'"),),
    ),
    "value-the-safe-loader-cannot-build-in-a-sequence": (
        _file("counts:", "  - 1", "  - !!float 1.2.3"),
        (Refusal(3, 5, "the safe loader cannot build '!!float' from '1.2.3'"),),
    ),
    "bool-the-safe-loader-cannot-build": (
        _file("ok: !!bool yesno"),
        (Refusal(1, 5, "the safe loader cannot build '!!bool' from 'yesno'"),),
    ),
    "equals-sign-value": (
        _file("name: ="),
        (Refusal(1, 7, "unsafe tag '!!value'"),),
    ),
    "merge-of-a-scalar": (
        _file("base: &base plain", "job:", "  <<: *base"),
        (Refusal(1, 7, "a merge needs a mapping, or a list of mappings, found a scalar"),),
    ),
    "merge-of-a-list-containing-a-scalar": (
        _file(
            "base: &base",
            "  tier: standard",
            "other: &other plain",
            "job:",
            "  <<: [*base, *other]",
        ),
        (Refusal(3, 8, "a merge needs mappings to merge in, found a scalar"),),
    ),
    "collection-as-a-key": (
        _file("? [north, south]", ": replicas"),
        (Refusal(1, 3, "it is not hashable"),),
    ),
    "set-tag-as-a-key": (
        _file("? !!set north", ": replicas"),
        (Refusal(1, 3, "it is not hashable"),),
    ),
    "alias-that-was-never-anchored": (
        _file("job: *missing"),
        (Refusal(1, 6, "found undefined alias 'missing'"),),
    ),
    "anchor-written-twice": (
        _file("a: &where north", "b: &where south"),
        (Refusal(2, 4, "found duplicate anchor 'where'"),),
    ),
    "file-that-does-not-parse": (
        _file("jobs:", "  name: build", "    runs: 1"),
        (Refusal(3, 9, "mapping values are not allowed here"),),
    ),
    "flow-sequence-never-closed": (
        # The file ends while the `[` is still open, so the problem is at the end
        # of the file and the message says what was still open there.
        _file("steps: [one, two"),
        (Refusal(2, 1, "expected ',' or ']'"),),
    ),
    "tab-where-an-indent-is": (
        _file("jobs:", "\tname: build"),
        (Refusal(2, 1, "found character"),),
    ),
    "character-a-yaml-file-may-not-hold": (
        _file("name: CI", "job: \x00"),
        (Refusal(2, 6, "special characters are not allowed"),),
    ),
    "duplicate-after-a-bad-merge-shape": (
        # One file can hold more than one kind of problem, and the check names
        # each at its own place.
        _file("base: &base plain", "job:", "  <<: *base", "  name: build", "  name: test"),
        (
            Refusal(1, 7, "a merge needs a mapping, or a list of mappings, found a scalar"),
            Refusal(5, 3, "duplicate key 'name'"),
        ),
    ),
    "problems-on-different-lines-in-line-then-column-order": (
        # Every other multi-problem file here has its problems all at column 1,
        # so it cannot tell a line-first order from a column-first one: this one's
        # second problem sits on a later line but in an earlier column (3:4 before
        # 2:12 would be the column-first reading).
        _file("x: 1", "yy: {b: 1, b: 2}", "z: !Ref q"),
        (
            Refusal(2, 12, "duplicate key 'b'"),
            Refusal(3, 4, "unsafe tag '!Ref'"),
        ),
    ),
    "problems-on-one-line-in-column-order": (
        # Two problems on one line: the column decides the order, and here the
        # column order is the reverse of what the two messages would sort to.
        _file("{a: !Ref q, c: {b: 1, b: 2}}"),
        (
            Refusal(1, 5, "unsafe tag '!Ref'"),
            Refusal(1, 23, "duplicate key 'b'"),
        ),
    ),
}

#: Bytes that no text decoder accepts: a file the check cannot read is refused,
#: and it is refused without a traceback.
NOT_UTF8: Final = b"name: \xff\xfe\n"
