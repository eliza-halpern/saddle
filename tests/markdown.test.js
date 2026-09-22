"use strict";
/* The transcript renderer runs in a browser, but its contract is pure: given
   markdown text it produces a tree. So it is tested here against a DOM small
   enough to read, with no browser and no network.
 *
 * The property that matters is equivalence: text streamed in token by token
 * must render to exactly the tree that the same text renders to in one shot.
 * A streaming renderer that is merely *fast* is worthless if it disagrees
 * with the finished article -- and the first version of splitStable did,
 * cutting a code block in half once its closing fence arrived. */

const test = require("node:test");
const assert = require("node:assert");
const path = require("node:path");

/* ---------- a DOM, but only the parts the renderer touches ---------- */

class TextNode {
  constructor(text) { this.data = String(text); this.parent = null; }
  get textContent() { return this.data; }
  // Escaped, like a real serializer: the renderer's safety property is that
  // model output lands in text nodes, and a shim that does not escape would
  // report that as safe no matter what the renderer did.
  get html() {
    return this.data.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
}

class Element {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.childNodes = [];
    this.dataset = {};
    this.parent = null;
    this._cls = "";
  }
  get children() { return this.childNodes.filter((n) => n instanceof Element); }
  get firstChild() { return this.childNodes[0] || null; }
  get firstElementChild() { return this.children[0] || null; }
  get lastElementChild() { const c = this.children; return c[c.length - 1] || null; }
  set className(v) { this._cls = v || ""; }
  get className() { return this._cls; }
  get classList() {
    const self = this;
    return {
      contains: (c) => self._cls.split(/\s+/).includes(c),
      add: (c) => { if (!self.classList.contains(c)) self._cls = (self._cls + " " + c).trim(); },
      remove: (c) => { self._cls = self._cls.split(/\s+/).filter((x) => x !== c).join(" "); },
    };
  }
  appendChild(node) {
    // Real appendChild *moves* a node. The incremental painter relies on it:
    // `while (chunk.firstChild) stable.appendChild(chunk.firstChild)` never
    // terminates against a shim that copies.
    if (node.parent) {
      const kids = node.parent.childNodes;
      kids.splice(kids.indexOf(node), 1);
    }
    node.parent = this;
    this.childNodes.push(node);
    return node;
  }
  append(...nodes) { for (const n of nodes) this.appendChild(n); }
  get textContent() { return this.childNodes.map((n) => n.textContent).join(""); }
  set textContent(v) {
    for (const kid of this.childNodes) kid.parent = null;
    this.childNodes = [];
    if (v !== "" && v !== undefined && v !== null) this.childNodes.push(new TextNode(v));
  }
  get html() {
    const cls = this._cls ? ` class="${this._cls}"` : "";
    const tag = this.tagName.toLowerCase();
    return `<${tag}${cls}>` + this.childNodes.map((n) => n.html).join("") + `</${tag}>`;
  }
}

global.document = {
  createElement: (tag) => new Element(tag),
  createTextNode: (text) => new TextNode(text),
};

const md = require(path.join(__dirname, "..", "src", "saddle", "web", "static", "markdown.js"));
const { el, renderMarkdown, splitStable, paintStream } = md;

/* ---------- helpers ---------- */

const oneShot = (raw) => { const n = el("div"); renderMarkdown(n, raw); return n.html; };

function streamed(raw, chunk = 7) {
  const node = el("div");
  node.dataset.raw = "";
  for (const bit of raw.match(new RegExp(`.{1,${chunk}}`, "gs")) || []) {
    node.dataset.raw += bit;
    paintStream(node);
  }
  // paintStream keeps a settled half and a live half; flatten to compare.
  const flat = el("div");
  for (const half of [...node.children]) while (half.firstChild) flat.appendChild(half.firstChild);
  return flat.html;
}

const SAMPLES = {
  "plain paragraphs": "One paragraph.\n\nTwo paragraph.\n\nThree.",
  "fence with a blank line inside": "Intro text.\n\n```python\ndef f():\n\n    return 1\n```\n\nAfter.",
  "bare fence with a blank line": "Pre.\n\n```\nline\n\nline2\n```\n\nPost.",
  "two fences": "A.\n\n```js\nlet x;\n\nlet y;\n```\n\nB.\n\n```py\nz = 1\n\nw = 2\n```\n\nC.",
  "bullet list": "Lead in:\n\n- alpha\n- beta\n\nThen more.",
  "numbered list": "Steps:\n\n1. one\n2. two\n\nDone.",
  "heading and inline marks": "### Title\n\nBody **bold**, *italic* and `code`.\n\n> quote\n\nEnd.",
  "no blank lines at all": "A single line with no breaks at all and some `inline`.",
  "unterminated fence": "Text.\n\n```js\nconst x = 1;\n",
  "link": "See [docs](https://example.test/y) please.\n\nNext.",
  "trailing blank lines": "Body.\n\n\n\n",
  "fence immediately after prose": "Prose.\n```\ncode\n```\nTail.",
};

for (const [name, raw] of Object.entries(SAMPLES)) {
  test(`streamed render matches one-shot: ${name}`, () => {
    assert.strictEqual(streamed(raw), oneShot(raw));
  });
  test(`chunk size does not change the result: ${name}`, () => {
    for (const size of [1, 3, 13, 400]) assert.strictEqual(streamed(raw, size), oneShot(raw));
  });
}

test("no text is lost, whatever the split", () => {
  for (const raw of Object.values(SAMPLES)) {
    const node = el("div");
    node.dataset.raw = raw;
    paintStream(node);
    const [head, tail] = splitStable(raw);
    assert.strictEqual(head + tail, raw, "splitStable must partition, not drop");
  }
});

test("a settled paragraph is never rebuilt", () => {
  // The point of the split: once a paragraph closes, its nodes are the same
  // objects forever, so a reader's selection inside it survives the stream.
  const raw = "First para.\n\nSecond para.\n\nThird para.\n\nFourth.";
  const node = el("div");
  node.dataset.raw = "";
  let firstNode = null;
  for (const bit of raw.match(/.{1,5}/gs)) {
    node.dataset.raw += bit;
    paintStream(node);
    const settled = node.firstElementChild.firstElementChild;
    if (settled && !firstNode) firstNode = settled;
    if (firstNode && settled) assert.strictEqual(settled, firstNode, "settled node was replaced");
  }
  assert.ok(firstNode, "nothing ever settled");
});

test("an open fence settles the prose above it, not the code inside it", () => {
  const raw = "Prose before.\n\n```python\nline one\n\nline two\n";
  const [head, tail] = splitStable(raw);
  assert.strictEqual(head, "Prose before.\n");
  assert.ok(tail.startsWith("\n```python"), tail);
});

test("markup in model output stays text", () => {
  const html = oneShot("Try <img src=x onerror=alert(1)> now.");
  assert.ok(!html.includes("<img"), html);
  assert.ok(html.includes("&lt;img"), html);
});

/* Only an absolute http(s) URL becomes a link. Everything else -- a file
 * path, a bare fragment, a javascript: URL -- is shown as text.
 *
 * The old code gave those href="#" with target="_blank", so clicking one
 * opened a second tab of the chat itself: the reader clicked a link and got
 * their own session back. A path the model mentions is information, not a
 * destination. */

test("an http link is a link", () => {
  const node = el("div");
  renderMarkdown(node, "see [docs](https://example.test/a) now");
  const link = node.children[0].children[0];
  assert.strictEqual(link.tagName, "A");
  assert.strictEqual(link.href, "https://example.test/a");
  assert.strictEqual(link.target, "_blank");
  assert.strictEqual(link.rel, "noopener noreferrer");
  assert.strictEqual(link.textContent, "docs");
});

for (const [name, href] of [
  ["a javascript: URL", "javascript:alert(1)"],
  ["a relative file path", "./out/chart.png"],
  ["an absolute file path", "/etc/passwd"],
  ["a bare fragment", "#section"],
  ["a root-relative app path", "/api/sessions"],
  ["a data: URL", "data:text/html,<script>x</script>"],
]) {
  test(`${name} is shown, not linked`, () => {
    const node = el("div");
    renderMarkdown(node, `try [here](${href}) please`);
    const para = node.children[0];
    assert.strictEqual(para.children.filter((c) => c.tagName === "A").length, 0,
                       `${href} became a link`);
    // ...and it is still legible: the label and the target both survive.
    assert.ok(para.textContent.includes("here"), para.textContent);
    assert.ok(para.textContent.includes(href), para.textContent);
  });
}


/* ---------- tool output ---------- */

const { isDiff, renderDiff, fillToolDetail } = md;

test("a unified diff is recognised, other output is not", () => {
  assert.ok(isDiff("--- a/frog.svg\n+++ b/frog.svg\n@@ -1 +1 @@\n-a\n+b"));
  assert.ok(!isDiff("created 'frog.svg' (1692 bytes)"));
  assert.ok(!isDiff("frog.svg unchanged"));
  assert.ok(!isDiff(""));
  assert.ok(!isDiff(undefined));
  assert.ok(!isDiff("error: cannot read 'x'"));
});

test("added lines are marked added and removed lines removed", () => {
  const node = el("div");
  renderDiff(node,
    "--- a/frog.svg\n+++ b/frog.svg\n@@ -39,5 +39,5 @@\n" +
    " <!-- Blushing cheeks -->\n" +
    '-  <ellipse rx="10" fill="#F48FB1"/>\n' +
    '+  <ellipse rx="13" fill="#F06292"/>');
  const classes = node.children.map((c) => c.className);
  assert.deepStrictEqual(classes,
    ["d-file", "d-file", "d-hunk", "d-ctx", "d-del", "d-add"]);
  // The +++ header must not be mistaken for an added line, nor --- for a
  // removed one: they are three characters of the same prefix.
  assert.strictEqual(node.children[1].textContent, "+++ b/frog.svg");
});

test("a blank context line keeps its row", () => {
  // Collapsing it would silently shift every line after it.
  const node = el("div");
  renderDiff(node, "--- a/x\n+++ b/x\n@@ -1,2 +1,2 @@\n\n+added");
  assert.strictEqual(node.children.length, 5);
  assert.strictEqual(node.children[3].className, "d-ctx");
});

test("the live row and a reloaded row fill identically", () => {
  // They had separate code and disagreed: the live one coloured a diff and
  // the stored one printed it flat, so a diff lost its colours on reload.
  const diff = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new";
  const liveRow = el("div"), liveDetail = el("div");
  const pastRow = el("div"), pastDetail = el("div");
  fillToolDetail(liveRow, liveDetail, diff);
  fillToolDetail(pastRow, pastDetail, diff);

  assert.strictEqual(pastDetail.html, liveDetail.html);
  assert.ok(pastRow.classList.contains("has-diff"));
  assert.strictEqual(pastDetail.children.filter((c) => c.className === "d-add").length, 1);
  assert.strictEqual(pastDetail.children.filter((c) => c.className === "d-del").length, 1);
});

test("output that is not a diff is left alone", () => {
  const row = el("div"), detail = el("div");
  fillToolDetail(row, detail, "created 'frog.svg' (1692 bytes)");
  assert.strictEqual(detail.textContent, "created 'frog.svg' (1692 bytes)");
  assert.ok(!row.classList.contains("has-diff"));
});

test("an empty result says so rather than showing nothing", () => {
  const row = el("div"), detail = el("div");
  fillToolDetail(row, detail, "");
  assert.strictEqual(detail.textContent, "(no output)");
});


/* ---------- syntax highlighting ---------- */

const { highlight, grammarFor } = md;

function tokens(code, language) {
  const node = el("code");
  highlight(node, code, language);
  return node.children.map((c) => [c.className, c.textContent]);
}

test("python gets its comments, strings, keywords and numbers", () => {
  const got = tokens('def f(x):\n    # note\n    return "hi" + 42', "python");
  assert.deepStrictEqual(got, [
    ["c-keyword", "def"],
    ["c-comment", "# note"],
    ["c-keyword", "return"],
    ["c-string", '"hi"'],
    ["c-number", "42"],
  ]);
});

test("a keyword inside a string stays a string", () => {
  // Order inside a grammar is the whole trick: strings and comments are
  // matched before keywords, or every quoted word gets recoloured.
  assert.deepStrictEqual(tokens('x = "return if for"', "python"),
                         [["c-string", '"return if for"']]);
  assert.deepStrictEqual(tokens('# return if for', "python"),
                         [["c-comment", "# return if for"]]);
});

test("a triple-quoted docstring is one string, not three", () => {
  const got = tokens('"""\nreturn\n"""', "python");
  assert.strictEqual(got.length, 1);
  assert.strictEqual(got[0][0], "c-string");
});

test("svg and html are highlighted as markup", () => {
  assert.deepStrictEqual(tokens('<circle cx="120"/>', "svg"), [
    ["c-tag", "<circle"],
    ["c-attr", "cx"],
    ["c-string", '"120"'],
    ["c-tag", "/>"],
  ]);
  assert.ok(grammarFor("html") === grammarFor("svg"));
});

test("json tells a key from a string value", () => {
  assert.deepStrictEqual(tokens('{"name": "ada", "age": 36}', "json"), [
    ["c-property", '"name"'],
    ["c-string", '"ada"'],
    ["c-property", '"age"'],
    ["c-number", "36"],
  ]);
});

test("the aliases people actually type all resolve", () => {
  for (const [alias, real] of [
    ["py", "python"], ["js", "javascript"], ["ts", "javascript"],
    ["tsx", "javascript"], ["sh", "bash"], ["zsh", "bash"],
    ["svg", "xml"], ["html", "xml"], ["yml", "yaml"],
    ["cpp", "c"], ["rs", "rust"], ["golang", "go"], ["patch", "diff"],
    ["scss", "css"], ["psql", "sql"],
  ]) {
    assert.ok(grammarFor(alias), `${alias} has no grammar`);
    assert.strictEqual(grammarFor(alias), grammarFor(real), alias);
  }
});

test("the language name is matched case-insensitively", () => {
  assert.strictEqual(grammarFor("Python"), grammarFor("python"));
  assert.strictEqual(grammarFor("SQL"), grammarFor("sql"));
});

test("an unknown or absent language is plain text, not mangled", () => {
  // Uncoloured code is readable; code a half-matching grammar chewed up is
  // not. So no grammar means no spans at all.
  for (const language of ["brainfuck", "", undefined, null, "lang-python"]) {
    const node = el("code");
    highlight(node, 'def f(): return "x"', language);
    assert.strictEqual(node.children.length, 0, String(language));
    assert.strictEqual(node.textContent, 'def f(): return "x"');
  }
});

test("highlighting never loses a character", () => {
  // Tokens are spans and the gaps between them are text nodes; if the two
  // do not tile the input, code silently goes missing.
  const samples = [
    ['def f(x):\n    return x  # done', "python"],
    ['<svg><rect fill="#fff"/></svg>', "svg"],
    ['{"a": [1, 2, null], "b": true}', "json"],
    ['for f in *.py; do echo "$f"; done', "bash"],
    ['const x = `a ${b} c`; // note', "js"],
    ['SELECT * FROM t WHERE a = 1', "sql"],
    ['fn main() { println!("hi"); }', "rust"],
    ['body { color: #fff; margin: 0 }', "css"],
    ['key: value  # comment', "yaml"],
  ];
  for (const [code, language] of samples) {
    const node = el("code");
    highlight(node, code, language);
    assert.strictEqual(node.textContent, code, language);
  }
});

test("highlighted code is still built from text nodes, never markup", () => {
  // The renderer's safety property has to survive into the highlighter:
  // model output cannot inject elements.
  const node = el("code");
  highlight(node, 'x = "<img src=x onerror=alert(1)>"', "python");
  assert.ok(!node.html.includes("<img"));
  assert.ok(node.html.includes("&lt;img"));
  assert.strictEqual(node.textContent, 'x = "<img src=x onerror=alert(1)>"');
});
