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

test("a javascript: link is defused", () => {
  const node = el("div");
  renderMarkdown(node, "[click](javascript:alert(1))");
  const link = node.children[0].children[0];
  assert.strictEqual(link.tagName, "A");
  assert.strictEqual(link.href, "#");
});
