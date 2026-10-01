"""Every link and anchor in the tracked Markdown files resolves, offline.

Contract: `check_links` reports each relative link whose file is not tracked,
each `#anchor` that matches no heading of its target under GitHub's slug
rule, each reference link without a definition and each malformed external
URL, and it reads nothing inside code. The checker is exercised on temp trees
with a known-good and a known-bad instance of each case, then run on the real
repository, where it must also have found links to check.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from doc_links import (
    Link,
    anchors_of,
    check_links,
    collect_links,
    github_slug,
)
from doc_tree import ROOT, tracked

# README.md opens with its logo and names three docs: a checker that found no
# link there would pass any tree.
MIN_README_LINKS = 1


def _tree(root: Path, files: dict[str, str]) -> tuple[list[str], list[str]]:
    for rel, body in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(body, encoding="utf-8")
    everything = sorted(
        os.path.relpath(os.path.join(base, name), root)
        for base, _, names in os.walk(root)
        for name in names
    )
    return [f for f in everything if f.endswith(".md")], everything


def _problems(tmp_path: Path, files: dict[str, str]) -> list[str]:
    markdown, everything = _tree(tmp_path, files)
    return [str(p) for p in check_links(tmp_path, markdown, everything)]


def test_the_tracked_docs_have_no_broken_link_or_anchor() -> None:
    markdown = tracked("*.md")
    problems = check_links(ROOT, markdown, tracked())
    assert [str(p) for p in problems] == []


def test_the_checker_finds_links_in_the_real_tree() -> None:
    markdown = tracked("*.md")
    assert {"README.md", "CONTRIBUTING.md", "CLAUDE.md"} <= set(markdown)
    readme, _ = collect_links((ROOT / "README.md").read_text(encoding="utf-8"))
    assert len(readme) >= MIN_README_LINKS
    # The anchor path ran on real headings too, not only on the temp trees.
    fragments = [
        link
        for rel in markdown
        for link in collect_links((ROOT / rel).read_text(encoding="utf-8"))[0]
        if "#" in link.dest and not link.dest.startswith("http")
    ]
    assert fragments


def test_good_links_resolve(tmp_path: Path) -> None:
    files = {
        "a.md": (
            "# Alpha\n\n## Two words\n\n"
            "[sibling](b.md) [anchored](b.md#second-heading) [same](#two-words)\n"
            "[rooted](/docs/c.md) [encoded](docs/with%20space.md) [dir](docs/)\n"
            '[titled](b.md "A title") ![img](docs/pic.png)\n'
            '<img src="docs/pic.png" alt="x"> <https://example.com/a?b#c>\n'
            "[ext](https://example.com/x#y) [mail](mailto:a@example.com)\n"
            "[line](docs/code.py#L10)\n"
            "[ref][r] [r][] [r]\n\n[r]: b.md#second-heading\n"
        ),
        "b.md": "# Beta\n\n## Second heading\n",
        "docs/c.md": "# C\n",
        "docs/with space.md": "# S\n",
        "docs/pic.png": "not read",
        "docs/code.py": "x = 1\n",
    }
    assert _problems(tmp_path, files) == []


@pytest.mark.parametrize(
    ("body", "target", "reason"),
    [
        ("[x](missing.md)\n", "missing.md", "no tracked file"),
        ("[x](b.md#nope)\n", "b.md#nope", "no heading or anchor #nope in b.md"),
        ("[x](#nope)\n", "#nope", "no heading or anchor #nope in a.md"),
        ("![x](docs/missing.png)\n", "docs/missing.png", "no tracked file"),
        ('<img src="gone.svg">\n', "gone.svg", "no tracked file"),
        ("[x](../outside.md)\n", "../outside.md", "leaves the repository"),
        ("[x][undefined]\n", "[undefined]", "no definition"),
        # The line reported is the definition's, not the use's.
        ("[x][r]\n\n[r]: gone.md\n", "gone.md", "no tracked file"),
        ("[x]()\n", "", "empty link destination"),
        ("[x](https://)\n", "https://", "no host"),
        ("[x](http://[::1)\n", "http://[::1", "malformed URL"),
        ("[x](http://host:99999/)\n", "http://host:99999/", "malformed URL"),
        ("[x](ftp://host/f)\n", "ftp://host/f", "unsupported scheme"),
        ("[x](javascript:alert(1))\n", "javascript:alert(1)", "unsupported scheme"),
        ("[x](mailto:nobody)\n", "mailto:nobody", "without an address"),
        ("[x](//cdn.example/x.js)\n", "//cdn.example/x.js", "protocol-relative"),
    ],
)
def test_bad_links_are_reported_with_file_target_and_reason(
    tmp_path: Path, body: str, target: str, reason: str
) -> None:
    found = _problems(tmp_path, {"a.md": "# A\n\n" + body, "b.md": "# B\n"})
    assert len(found) == 1, found
    line = 5 if "[r]: " in body else 3
    assert found[0].startswith(f"a.md:{line}: ")
    assert f": {target}: " in found[0]
    assert reason in found[0]


def test_nothing_inside_code_is_checked_and_code_ends_where_it_ends(tmp_path: Path) -> None:
    body = (
        "# A\n\n"
        "```md\n[fenced](gone.md)\n```\n"
        "~~~\n[tilde](gone.md)\n~~~\n"
        "````\n```\n[nested](gone.md)\n```\n````\n"
        "inline `[span](gone.md)` and ``[double ` tick](gone.md)`` here\n\n"
        "[after](after-the-fences.md)\n"
    )
    found = _problems(tmp_path, {"a.md": body})
    assert len(found) == 1, found
    assert "after-the-fences.md" in found[0]
    assert found[0].startswith("a.md:16: ")


def test_a_closing_fence_must_be_as_long_as_the_opening_one(tmp_path: Path) -> None:
    body = "# A\n\n````\n```\n[still code](gone.md)\n````\n[real](gone.md)\n"
    found = _problems(tmp_path, {"a.md": body})
    assert len(found) == 1, found
    assert found[0].startswith("a.md:7: ")


@pytest.mark.parametrize(
    ("heading", "slug"),
    [
        ("Start here", "start-here"),
        ("Server URL and model", "server-url-and-model"),
        ("The test time limit", "the-test-time-limit"),
        # An em dash is dropped but the spaces around it each become a hyphen.
        ("P0 — Mechanical. Hours of work.", "p0--mechanical-hours-of-work"),
        ("`check_node_scope` and `_private`", "check_node_scope-and-_private"),
        ("Calling `<name>` labels", "calling-name-labels"),
        ("A [link text](https://example.com/x) heading", "a-link-text-heading"),
        ("A [ref text][label] heading", "a-ref-text-heading"),
        ("Bold **word** and _emphasis_ here", "bold-word-and-emphasis-here"),
        ("snake_case stays", "snake_case-stays"),
        ("Closing hashes ##", "closing-hashes"),
        ("Café → naïve 3.5", "café--naïve-35"),
        ("Tier 1 / Tier 2", "tier-1--tier-2"),
        ("Check-in and re-run", "check-in-and-re-run"),
    ],
)
def test_the_slug_rule(heading: str, slug: str) -> None:
    assert github_slug(heading) == slug


def test_duplicate_headings_get_numbered_suffixes() -> None:
    text = "# Same\n\n## Same\n\n### Same\n\n## Other\n\n## Same\n"
    assert anchors_of(text) == {"same", "same-1", "same-2", "same-3", "other"}


def test_headings_in_code_do_not_make_anchors_and_html_ids_do() -> None:
    text = '# Real\n\n```md\n# Fake heading\n```\n\n<a id="hand-made"></a>\n> ## Quoted\n'
    assert anchors_of(text) == {"real", "hand-made", "quoted"}


def test_a_link_to_a_duplicate_heading_needs_its_suffix(tmp_path: Path) -> None:
    files = {
        "a.md": "# A\n\n[one](b.md#same) [two](b.md#same-1) [three](b.md#same-2)\n",
        "b.md": "# B\n\n## Same\n\n## Same\n",
    }
    found = _problems(tmp_path, files)
    assert len(found) == 1, found
    assert "b.md#same-2" in found[0]


def test_collect_links_reports_lines_and_destinations() -> None:
    links, definitions = collect_links("one\n[a](x.md)\n\ntext ![i](y.png) [r][k]\n\n[K]: z.md\n")
    assert links == [Link(2, "x.md"), Link(4, "y.png"), Link(6, "z.md")]
    assert definitions == {"k": "z.md"}
