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

/* The fake DOM, the clipboard recorder and the navigator switch live in the
   shared shim; requiring it installs the global `document` and `navigator`. */
const {
  clipboard, setNavigator, resetClipboard,
} = require("./fixtures/dom_shim.js");

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


/* ---------- transcript ordering ---------- */

test("a turn's blocks are released when a tool starts", () => {
  /* The ordering bug, as a unit.
   *
   * A turn is rounds of "think, say something, call a tool". The assistant
   * and reasoning blocks are cached so consecutive deltas land in one node,
   * but the cache has to be dropped when a round ends -- otherwise round
   * two's text is appended into round one's block, which sits *above* the
   * tool rows, and the answer appears before the tools that produced it.
   *
   * It looked cosmetic because a reload rebuilt it correctly: history is
   * stored in order, so only the live stream was ever wrong.
   */
  const transcript = el("div");
  const state = { turnNode: transcript, assistantNode: null, reasoningNode: null };

  const say = (text) => {
    if (!state.assistantNode) {
      state.assistantNode = el("div", "assistant");
      transcript.appendChild(state.assistantNode);
    }
    state.assistantNode.appendChild(document.createTextNode(text));
  };
  const callTool = (name) => {
    state.assistantNode = null;          // the fix
    state.reasoningNode = null;
    transcript.appendChild(el("div", "tool", name));
  };

  say("first, I will look at the file");
  callTool("Read a.py");
  say("now I will change it");
  callTool("Edited a.py");
  say("done");

  assert.deepStrictEqual(
    transcript.children.map((c) => [c.className, c.textContent]),
    [
      ["assistant", "first, I will look at the file"],
      ["tool", "Read a.py"],
      ["assistant", "now I will change it"],
      ["tool", "Edited a.py"],
      ["assistant", "done"],
    ]);
});

test("without releasing the cache the text jumps above the tools", () => {
  // The same script with the fix removed, to show the test can see it.
  const transcript = el("div");
  const state = { assistantNode: null };
  const say = (text) => {
    if (!state.assistantNode) {
      state.assistantNode = el("div", "assistant");
      transcript.appendChild(state.assistantNode);
    }
    state.assistantNode.appendChild(document.createTextNode(text));
  };
  const callTool = (name) => transcript.appendChild(el("div", "tool", name));

  say("first");
  callTool("Read a.py");
  say(" and second");

  // Both sentences are in one block, and that block is before the tool.
  assert.deepStrictEqual(
    transcript.children.map((c) => c.className), ["assistant", "tool"]);
  assert.strictEqual(transcript.children[0].textContent, "first and second");
});


/* ---------- a newly written file ---------- */

const { languageForPath, CREATED } = md;

test("a created file is shown as the file, highlighted", () => {
  // "created 'x.py' (42 bytes)" says nothing about what was written, and a
  // new file is the one case with no diff to read instead.
  const row = el("div"), detail = el("div");
  fillToolDetail(row, detail,
    "created 'greet.py' (46 bytes)\ndef greet(name):\n    return \"hi\"");

  assert.ok(row.classList.contains("has-diff"));
  const head = detail.children[0];
  assert.strictEqual(head.className, "c-head");
  assert.strictEqual(head.textContent, "created greet.py (46 bytes)");

  const body = detail.children[1];
  assert.strictEqual(body.tagName, "CODE");
  const parts = body.children.map((c) => [c.className, c.textContent]);
  assert.deepStrictEqual(parts, [
    ["c-keyword", "def"], ["c-keyword", "return"], ["c-string", '"hi"'],
  ]);
  assert.strictEqual(body.textContent, 'def greet(name):\n    return "hi"');
});

test("the language comes from the file's own extension", () => {
  for (const [name, language] of [
    ["a.py", "python"], ["a.svg", "xml"], ["index.html", "xml"],
    ["app.tsx", "javascript"], ["Cargo.toml", "toml"], ["q.sql", "sql"],
    ["run.sh", "bash"], ["a.rs", "rust"], ["main.go", "go"],
    ["styles.scss", "css"], ["deep/nested/file.json", "json"],
  ]) {
    assert.strictEqual(languageForPath(name), language, name);
  }
  // No extension, an unknown one, or nothing at all: plain text, not a guess.
  for (const name of ["Makefile", "notes.txt", "a.xyz", "", null]) {
    assert.strictEqual(languageForPath(name), null, String(name));
  }
});

test("a created file with no recognised extension is still readable", () => {
  const row = el("div"), detail = el("div");
  fillToolDetail(row, detail, "created 'notes.txt' (5 bytes)\nhello");
  assert.strictEqual(detail.children[1].textContent, "hello");
  assert.strictEqual(detail.children[1].children.length, 0);   // uncoloured
});

test("an empty new file says so without pretending to show content", () => {
  const row = el("div"), detail = el("div");
  fillToolDetail(row, detail, "created 'empty.py' (0 bytes)\n");
  assert.strictEqual(detail.children[0].textContent, "created empty.py (0 bytes)");
  assert.strictEqual(detail.children[1].textContent, "");
});

test("only the exact created shape is treated as a file body", () => {
  // Anything else -- a command's output, an error, a message that merely
  // mentions the word -- must not be parsed as a file.
  for (const text of [
    "created 'x.py' (42 bytes)",                 // header with no body
    "I created 'x.py' (42 bytes)\nstuff",        // not at the start
    "created x.py (42 bytes)\nstuff",            // unquoted
    "error: cannot write 'x.py'",
    "x.py unchanged",
  ]) {
    assert.strictEqual(CREATED.test(text), false, text);
  }
});

test("a created file and a reloaded one render identically", () => {
  // The same divergence the diff renderer had: the fix is that both sides
  // read the same stored text.
  const text = "created 'a.py' (8 bytes)\nx = 1\n";
  const live = { row: el("div"), detail: el("div") };
  const past = { row: el("div"), detail: el("div") };
  fillToolDetail(live.row, live.detail, text);
  fillToolDetail(past.row, past.detail, text);
  assert.strictEqual(past.detail.html, live.detail.html);
});

/* ---------- the code block's copy button ---------- */

const { copyButton } = md;

const findPre = (root) => {
  for (const child of root.children) {
    if (child.tagName === "PRE") return child;
    const deep = findPre(child);
    if (deep) return deep;
  }
  return null;
};

test("a fenced code block carries a corner button that names its language", () => {
  const node = el("div");
  renderMarkdown(node, "Here you go.\n\n```python\ndef f():\n    return 1\n```\n\nDone.");
  const pre = node.children.find((c) => c.tagName === "PRE");
  assert.ok(pre, "no code block");
  const button = pre.children.find((c) => c.className === "code-copy");
  assert.ok(button, "the block has no copy button");
  assert.strictEqual(button.textContent, "Copy");
  assert.strictEqual(button.title, "Copy code (python)");
});

test("clicking the button copies the code, not the fences or the language", async () => {
  resetClipboard();
  const node = el("div");
  renderMarkdown(node, "```\nline one\nline two\n```");
  const button = node.children[0].children.find((c) => c.className === "code-copy");
  assert.ok(button);
  const ok = await button.onclick();
  assert.strictEqual(ok, true);
  assert.deepStrictEqual(clipboard.writes, ["line one\nline two"]);
  assert.strictEqual(button.textContent, "Copied");
});

test("a diff row's button copies the diff verbatim, blank line intact", async () => {
  // The renderer pads a blank line to a space for equal row heights; the
  // copy must carry the tool's text, not the renderer's padding.
  resetClipboard();
  const row = el("div"), detail = el("div");
  const diff = "--- a/x\n+++ b/x\n@@ -1,2 +1,2 @@\n\n+added";
  fillToolDetail(row, detail, diff);
  const button = row.children[row.children.length - 1];
  assert.strictEqual(button.className, "code-copy");
  const ok = await button.onclick();
  assert.strictEqual(ok, true);
  assert.strictEqual(clipboard.writes[0], diff);
});

test("a created file's button copies the file, not its header", async () => {
  resetClipboard();
  const row = el("div"), detail = el("div");
  fillToolDetail(row, detail, "created 'a.py' (12 bytes)\nx = 1\ny = 2");
  const button = row.children[row.children.length - 1];
  assert.strictEqual(button.className, "code-copy");
  const ok = await button.onclick();
  assert.strictEqual(ok, true);
  assert.strictEqual(clipboard.writes[0], "x = 1\ny = 2");
});

test("a tool row keeps exactly one button across re-fills, copying the latest", async () => {
  // A re-run of the same tool, or a reload overlapping a live fill, must
  // not leave a second button behind, and the one left copies the latest
  // fill; a re-fill with no content has nothing to copy, so it drops it.
  resetClipboard();
  const row = el("div"), detail = el("div");
  fillToolDetail(row, detail, "first");
  fillToolDetail(row, detail, "second");
  const buttons = row.children.filter((c) => c.className === "code-copy");
  assert.strictEqual(buttons.length, 1);
  assert.strictEqual(await buttons[0].onclick(), true);
  assert.strictEqual(clipboard.writes[0], "second");
  fillToolDetail(row, detail, "");
  assert.strictEqual(row.children.filter((c) => c.className === "code-copy").length, 0);
});

test("the button goes in a row's summary and leaves the detail as output only", () => {
  const row = el("details"), summary = el("summary"), detail = el("div");
  row.appendChild(summary);
  row.appendChild(detail);
  fillToolDetail(row, detail, "some output");
  assert.strictEqual(detail.textContent, "some output");
  assert.strictEqual(summary.children.filter((c) => c.className === "code-copy").length, 1);
});

test("a second click restarts the feedback window", async () => {
  resetClipboard();
  const realSetTimeout = globalThis.setTimeout, realClearTimeout = globalThis.clearTimeout;
  const live = new Set();
  let next = 1;
  globalThis.setTimeout = () => { const id = next++; live.add(id); return id; };
  globalThis.clearTimeout = (id) => { live.delete(id); };
  try {
    const button = copyButton("twice", "code");
    await button.onclick();
    await button.onclick();
    assert.strictEqual(live.size, 1);
  } finally {
    globalThis.setTimeout = realSetTimeout;
    globalThis.clearTimeout = realClearTimeout;
  }
});

test("a click inside a summary does not toggle the row", async () => {
  resetClipboard();
  let prevented = 0, stopped = 0;
  const button = copyButton("x", "code");
  await button.onclick({ preventDefault: () => prevented++, stopPropagation: () => stopped++ });
  assert.strictEqual(prevented, 1);
  assert.strictEqual(stopped, 1);
});

test("the fallback copy cleans up its field even when selecting throws", async () => {
  resetClipboard();
  setNavigator({});
  const kids = document.body.childNodes.length;
  const realCreate = document.createElement;
  document.createElement = (tag) => {
    const node = realCreate(tag);
    if (tag === "textarea") node.select = () => { throw new Error("no selection"); };
    return node;
  };
  try {
    const ok = await copyButton("x", "code").onclick();
    assert.strictEqual(ok, false);
    assert.strictEqual(document.body.childNodes.length, kids);
  } finally {
    document.createElement = realCreate;
  }
});

test("the async clipboard is used when it is there", async () => {
  resetClipboard();
  const button = copyButton("some code", "code");
  await button.onclick();
  assert.strictEqual(clipboard.writes.length, 1);
  assert.strictEqual(clipboard.viaExecCommand, 0);
  assert.strictEqual(button.textContent, "Copied");
});

test("without the async clipboard, the select-and-execute path copies", async () => {
  // A page the browser will not call local has no clipboard API: the
  // legacy path is the copy, and the click is the authorisation it needs.
  resetClipboard();
  setNavigator({});
  const button = copyButton("legacy copy", "code");
  const ok = await button.onclick();
  assert.strictEqual(ok, true);
  assert.strictEqual(clipboard.viaExecCommand, 1);
  assert.deepStrictEqual(clipboard.writes, ["legacy copy"]);
  assert.strictEqual(button.textContent, "Copied");
});

test("a refused async clipboard falls back to the select-and-execute path", async () => {
  resetClipboard();
  setNavigator({ clipboard: { writeText: () => Promise.reject(new Error("denied")) } });
  const button = copyButton("after the refusal", "code");
  const ok = await button.onclick();
  assert.strictEqual(ok, true);
  assert.strictEqual(clipboard.viaExecCommand, 1);
  assert.strictEqual(clipboard.writes[0], "after the refusal");
});

test("when the system clipboard takes nothing, the button says Failed", async () => {
  resetClipboard();
  setNavigator({ clipboard: { writeText: () => Promise.reject(new Error("denied")) } });
  const realExec = document.execCommand;
  document.execCommand = () => false;
  try {
    const button = copyButton("nowhere to go", "code");
    const ok = await button.onclick();
    assert.strictEqual(ok, false);
    assert.strictEqual(button.textContent, "Failed");
  } finally {
    document.execCommand = realExec;
  }
});

test("the button settles back to Copy after the feedback window", async () => {
  resetClipboard();
  const realSetTimeout = globalThis.setTimeout;
  const timers = [];
  globalThis.setTimeout = (callback, delay) => { timers.push({ callback, delay }); return 0; };
  try {
    const button = copyButton("transient", "code");
    await button.onclick();
    assert.strictEqual(button.textContent, "Copied");
    assert.strictEqual(timers.length, 1);
    assert.ok(timers[0].delay > 0);
    timers[0].callback();
    assert.strictEqual(button.textContent, "Copy");
  } finally {
    globalThis.setTimeout = realSetTimeout;
  }
});

test("a streamed fence settles with its button intact", () => {
  // The button rides inside the pre, so the settled-node rule applies to
  // it: once the fence is settled the block -- button and all -- is the
  // same object forever, or the reader's hover target would move under
  // the cursor.
  const raw = "Prose.\n\n```js\nlet a = 1;\nlet b = 2;\n```\n\nTail.";
  const node = el("div");
  node.dataset.raw = "";
  let settledPre = null;
  for (const bit of raw.match(/.{1,5}/gs)) {
    node.dataset.raw += bit;
    paintStream(node);
    const pre = node.firstElementChild ? findPre(node.firstElementChild) : null;
    if (pre && !settledPre) settledPre = pre;
    if (pre && settledPre) assert.strictEqual(pre, settledPre, "the settled code block was rebuilt");
  }
  assert.ok(settledPre, "the fence never settled");
  assert.strictEqual(settledPre.children[0].tagName, "CODE");
  const button = settledPre.children.find((c) => c.className === "code-copy");
  assert.ok(button, "the settled block has no button");
});

test("a click on a block that is still streaming copies what has arrived", async () => {
  resetClipboard();
  const raw = "```python\ndef f():\n    return 4";
  const node = el("div");
  node.dataset.raw = "";
  for (const bit of raw.match(/.{1,5}/gs)) {
    node.dataset.raw += bit;
    paintStream(node);
  }
  const pre = findPre(node.lastElementChild);
  assert.ok(pre);
  const button = pre.children.find((c) => c.className === "code-copy");
  assert.ok(button);
  const ok = await button.onclick();
  assert.strictEqual(ok, true);
  assert.strictEqual(clipboard.writes[0], "def f():\n    return 4");
});
