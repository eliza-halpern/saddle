"""The fake DOM the node suite runs against must be as strict as a real page.

A renderer once called `row.children.find(...)`. A real `children` is an
HTMLCollection, which has no `find`, so every tool row threw in a browser;
the shim's `children` returned an Array, so all 75 node tests passed. The
shim (`tests/fixtures/dom_shim.js`) now hands out collections that offer only
what the platform offers. These tests pin that by instance, through node:
a known-good use of a collection works, the known-bad Array method is absent,
and the real renderer with the old bug put back fails the real suite.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
TESTS = Path(__file__).parent
STATIC = Path("src") / "saddle" / "web" / "static"

needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


def run_node(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    assert NODE is not None
    return subprocess.run(
        [NODE, *args], capture_output=True, text=True, timeout=120, check=False, cwd=cwd
    )


# Prints one JSON line per probe; the shim is required from the tests folder.
PROBE = r"""
const { TextNode, Element, HTMLCollection, NodeList } = require("./fixtures/dom_shim.js");
const parent = new Element("div");
const a = new Element("p"); a.setAttribute("name", "first");
const b = new Element("span");
parent.append(a, new TextNode("loose text"), b);
const out = {
  childrenIsHtmlCollection: parent.children instanceof HTMLCollection,
  childNodesIsNodeList: parent.childNodes instanceof NodeList,
  childrenLength: parent.children.length,
  childNodesLength: parent.childNodes.length,
  indexed: parent.children[1] === b,
  pastTheEnd: parent.children[2] === undefined,
  item: parent.children.item(0) === a && parent.children.item(5) === null,
  namedItem: parent.children.namedItem("first") === a && parent.children.namedItem("x") === null,
  iterated: Array.from(parent.children).map((c) => c.tagName).join(","),
  spread: [...parent.childNodes].length,
  forOf: (() => {
    const t = [];
    for (const c of parent.children) t.push(c.tagName);
    return t.join(",");
  })(),
  nodeListForEach: (() => {
    const t = [];
    parent.childNodes.forEach((n) => t.push(n.textContent));
    return t;
  })(),
  live: (() => {
    const kids = parent.children;
    const before = kids.length;
    parent.appendChild(new Element("i"));
    return [before, kids.length];
  })(),
  isArray: Array.isArray(parent.children) || Array.isArray(parent.childNodes),
  classListLength: typeof parent.classList.length,
};
for (const owner of ["children", "childNodes"]) {
  for (const method of ["find", "filter", "map", "some", "every", "reduce", "slice", "push"]) {
    out[`${owner}.${method}`] = typeof parent[owner][method];
  }
}
out["childNodes.forEach"] = typeof parent.childNodes.forEach;
out["children.forEach"] = typeof parent.children.forEach;
for (const method of ["find", "filter", "map"]) {
  out[`classList.${method}`] = typeof parent.classList[method];
}
console.log(JSON.stringify(out));
"""


@needs_node
def test_shim_collections_offer_what_the_platform_offers() -> None:
    result = run_node("-e", PROBE, cwd=TESTS)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    # Known good: the HTMLCollection and NodeList surface works.
    assert out["childrenIsHtmlCollection"] is True
    assert out["childNodesIsNodeList"] is True
    assert (out["childrenLength"], out["childNodesLength"]) == (2, 3)
    assert out["indexed"] is True
    assert out["pastTheEnd"] is True
    assert out["item"] is True
    assert out["namedItem"] is True
    assert out["iterated"] == "P,SPAN"
    assert out["forOf"] == "P,SPAN"
    assert out["spread"] == 3
    assert out["nodeListForEach"] == ["", "loose text", ""]
    assert out["live"] == [2, 3]
    assert out["isArray"] is False
    assert out["childNodes.forEach"] == "function"
    # Known bad: no Array method the real collections lack.
    for owner in ("children", "childNodes"):
        for method in ("find", "filter", "map", "some", "every", "reduce", "slice", "push"):
            assert out[f"{owner}.{method}"] == "undefined", f"{owner}.{method} exists"
    assert out["children.forEach"] == "undefined"
    for method in ("find", "filter", "map"):
        assert out[f"classList.{method}"] == "undefined"


@needs_node
def test_the_old_children_find_bug_fails_the_node_suite(tmp_path: Path) -> None:
    """The real renderer with `row.children.find` put back fails; the real one passes."""
    root = TESTS.parent
    work = tmp_path / "tree"
    (work / "tests" / "fixtures").mkdir(parents=True)
    (work / STATIC).mkdir(parents=True)
    shutil.copy(TESTS / "markdown.test.js", work / "tests")
    shutil.copy(TESTS / "fixtures" / "dom_shim.js", work / "tests" / "fixtures")
    source = (root / STATIC / "markdown.js").read_text()
    fixed = "Array.from(row.children).find("
    assert fixed in source, "markdown.js no longer has the call this test mutates"
    (work / STATIC / "markdown.js").write_text(source)
    good = run_node("--test", str(work / "tests" / "markdown.test.js"))
    assert good.returncode == 0, good.stdout + good.stderr
    (work / STATIC / "markdown.js").write_text(source.replace(fixed, "row.children.find("))
    bad = run_node("--test", str(work / "tests" / "markdown.test.js"))
    assert bad.returncode != 0, "the suite accepted a renderer that calls children.find"
    assert "children.find is not a function" in bad.stdout + bad.stderr


@needs_node
@pytest.mark.parametrize("suite", ["markdown.test.js", "classic_scripts.test.js"])
def test_each_node_suite_passes(suite: str) -> None:
    """Each node test file runs with the pytest suite, so the audit sees it pass or fail."""
    result = run_node("--test", str(TESTS / suite))
    assert result.returncode == 0, result.stdout + result.stderr
