"use strict";
/* The sidebar and notification scripts are classic page scripts, mostly
   DOM wiring that only a real browser exercises (the Chrome-driven tests do
   that). Their pure helpers -- how a run's age reads, which state a run
   shows, what the tab icon looks like -- are loaded here into a sandbox
   scope, unedited, and asserted directly. */

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const { pathToFileURL } = require("node:url");
const { loadClassic, inScope, loadPage, PAGE_SCRIPTS, STATIC } = require("./fixtures/classic_script.js");

const inert = { document: { addEventListener() {} }, setInterval() {} };

/* ---------- runs.js ---------- */

function runsScript(nowSeconds = 1_000_000) {
  return loadClassic("runs.js", { ...inert, Date: { now: () => nowSeconds * 1000 } });
}

test("a run's task is shown as its first words, trimmed and cut with an ellipsis", () => {
  const { firstWords } = runsScript();
  assert.strictEqual(firstWords("  fix   the\n flaky   test please now  "), "fix the flaky test please now");
  assert.strictEqual(firstWords("one two three four five six seven"), "one two three four five six…");
  // Known bad: exactly the limit is not cut, and no ellipsis is added.
  assert.strictEqual(firstWords("one two three four five six"), "one two three four five six");
  assert.strictEqual(firstWords("a b c d", 2), "a b…");
  for (const nothing of [undefined, null, "", "   "]) assert.strictEqual(firstWords(nothing), "");
});

test("an age reads in seconds, minutes, then hours and minutes", () => {
  const { shortAge } = runsScript();
  assert.strictEqual(shortAge(0), "0s");
  assert.strictEqual(shortAge(59.9), "59s");
  assert.strictEqual(shortAge(60), "1m");
  assert.strictEqual(shortAge(3599), "59m");
  assert.strictEqual(shortAge(3600), "1h 0m");
  assert.strictEqual(shortAge(3700), "1h 1m");
  // A clock skew that puts the start in the future is shown as zero, not negative.
  assert.strictEqual(shortAge(-5), "0s");
});

test("a run no live server holds, still marked going, is shown as interrupted", () => {
  const { shownState } = runsScript();
  assert.strictEqual(shownState({ live: false, state: "running" }), "interrupted");
  assert.strictEqual(shownState({ live: false, state: "needs_you" }), "interrupted");
  // Known good: a live run, and a run that ended, show their own state.
  assert.strictEqual(shownState({ live: true, state: "running" }), "running");
  for (const ended of ["finished", "stopped", "unchanged", "asked", "failed"]) {
    assert.strictEqual(shownState({ live: false, state: ended }), ended);
  }
});

test("a run's time is its wait, its duration, or its age so far", () => {
  const now = 1_000_000;
  const { runMeta } = runsScript(now);
  // Waiting on the person: how long since it began waiting.
  assert.strictEqual(runMeta({ live: true, state: "needs_you", state_since: now - 125 }), "waiting 2m");
  assert.strictEqual(runMeta({ live: true, state: "needs_you" }), "waiting 0s");
  // Ended: start to end, not start to now.
  assert.strictEqual(runMeta({ live: false, state: "finished", started: now - 5000, ended: now - 4000 }), "16m");
  assert.strictEqual(runMeta({ live: false, state: "stopped", started: now - 100, state_since: now - 40 }), "1m");
  // Cut off: the same, ended-style.
  assert.strictEqual(runMeta({ live: false, state: "running", started: now - 100, ended: now - 70 }), "30s");
  // Still running: start to now.
  assert.strictEqual(runMeta({ live: true, state: "running", started: now - 3700 }), "1h 1m");
  // No timestamps at all: zero, never NaN.
  assert.strictEqual(runMeta({ live: true, state: "running" }), "0s");
  // The server's clock, not the page's: a skew moves every age.
  const skewed = runsScript(now);
  inScope(skewed, "runsView.skew = 60");
  assert.strictEqual(skewed.runMeta({ live: true, state: "running", started: now }), "1m");
});

/* ---------- notify.js ---------- */

const notifyScript = () => loadClassic("notify.js", inert);

test("each run state has a look, and an unknown or missing one has none", () => {
  const { lookFor } = notifyScript();
  assert.strictEqual(lookFor("needs_you").word, "needs you");
  assert.strictEqual(lookFor("finished").glyph, "✓");
  assert.strictEqual(lookFor("running").color, "#7fb6e6");
  assert.strictEqual(lookFor("no-such-state"), undefined);
  for (const none of [null, undefined, ""]) assert.strictEqual(lookFor(none), undefined);
});

test("the tab icon is a coloured dot for a state and a hollow ring when idle", () => {
  const { faviconFor } = notifyScript();
  const decode = (/** @type {string} */ uri) => decodeURIComponent(uri.slice("data:image/svg+xml,".length));
  const finished = decode(faviconFor("finished"));
  assert.ok(finished.startsWith("<svg"), finished);
  assert.ok(finished.includes("#5fcf86"), "finished is green");
  assert.ok(!finished.includes("fill='none'"), "a state is a filled dot");
  for (const idle of [null, "no-such-state"]) {
    const ring = decode(faviconFor(idle));
    assert.ok(ring.includes("fill='none'"), "idle is a hollow ring");
    assert.ok(!ring.includes("#5fcf86"));
  }
  assert.notStrictEqual(faviconFor("finished"), faviconFor("failed"));
});

test("a session is named by its title, or by the product when it has none", () => {
  const page = notifyScript();
  inScope(page, 'runWatch.names.set("s1", "Copy button")');
  assert.strictEqual(page.sessionName("s1"), "Copy button");
  assert.strictEqual(page.sessionName("unknown"), "saddle");
  assert.strictEqual(page.sessionName(null), "saddle");
});

/* ---------- app.js, through loadPage ---------- */

test("loadPage loads the page's scripts in index.html's order", () => {
  const html = fs.readFileSync(path.join(STATIC, "index.html"), "utf8");
  const listed = [...html.matchAll(/<script src="\/static\/([^"]+)"/g)].map((m) => m[1]);
  assert.deepStrictEqual(PAGE_SCRIPTS, listed);
  assert.ok(listed.includes("app.js"));
});

test("app.js loads beside its siblings, and a painter's output reads back from the document", () => {
  const page = loadPage();
  page.setStatus("working");
  const status = page.document.querySelector("#status");
  assert.strictEqual(status.className, "status working");
  assert.strictEqual(status.textContent, "working");
  assert.strictEqual(page.document.querySelector("#send").title, "Stop");
  page.setStatus("idle", "ready");
  assert.strictEqual(status.className, "status idle");
  assert.strictEqual(status.textContent, "ready");
  assert.strictEqual(page.document.querySelector("#send").title, "Send");
});

/* ---------- accessible names: the status pill and the cite chips (#86) ---------- */

test("the status pill's name is the plain state, whatever the pill shows", () => {
  const page = loadPage();
  const status = page.document.querySelector("#status");
  const seen = [];
  for (const [kind, detail] of [
    ["idle", undefined],
    ["working", undefined],
    ["needs", undefined],
    ["error", undefined],
    ["working", "stopping…"], // the detail covers the word, so the name carries the state alone
    ["idle", "ready"],
  ]) {
    page.setStatus(kind, detail);
    seen.push([status.getAttribute("aria-label"), status.textContent, status.className]);
  }
  assert.deepStrictEqual(seen, [
    ["idle", "idle", "status idle"],
    ["working", "working", "status working"],
    ["needs", "needs", "status needs"],
    ["error", "error", "status error"],
    ["working", "stopping…", "status working"],
    ["idle", "ready", "status idle"],
  ]);
});

test("a cite chip names the record it opens, and keeps the hash it shows inside that name", () => {
  const page = loadPage();
  const host = page.document.createElement("div");
  const rows = [
    ["packet row", page.citeButton("1a2b3c4d5e6f7a8b9c0d", { hash: "1a2b3c4d5e6f7a8b9c0d" }, host)],
    ["session line", page.sessionCite({ lines: host }, "ff00ff00ff00ff00ff00")],
  ];
  const read = rows.map(([kind, chip]) => [kind, chip.getAttribute("aria-label"), chip.textContent]);
  assert.deepStrictEqual(read, [
    ["packet row", "Open the sealed ledger record 1a2b3c4d", "1a2b3c4d"],
    ["session line", "Open the sealed ledger record ff00ff00", "ff00ff00"],
  ]);
  // Known bad: a name that dropped the eight digits the chip shows would leave
  // the name and the label saying different things (WCAG 2.5.3, Label in Name).
  for (const [kind, label, shown] of read) {
    assert.ok(label.includes(shown), `${kind}: the name ${label} drops the hash ${shown} the chip shows`);
  }
});

test("each page script runs under its file URL, which mutation testing needs to count the test", () => {
  const page = loadPage();
  // Known bad: a document with no #status makes app.js's own code throw, and
  // the frame that throws names the file the script was loaded under.
  page.document = { querySelector: () => null };
  assert.throws(
    () => page.setStatus("working"),
    (/** @type {Error} */ error) => String(error.stack).includes(`${pathToFileURL(path.join(STATIC, "app.js")).href}:`),
  );
});

/* ---------- the conditions strip: what the session runs with ---------- */

/** A view as the server's `check_conditions` sends it. */
function conditionsView() {
  const capabilities = { images: "on", browser: "off", ocr: "off", mcp: "off" };
  return {
    start: { reasoning: "on", effort: "medium", keep_reasoning: "on", capabilities },
    current: {
      reasoning: "on",
      effort: "xhigh",
      keep_reasoning: "on",
      capabilities: { ...capabilities, ocr: "unavailable" },
      rows: [
        { name: "images", state: "on", reason: "", available: null },
        { name: "browser", state: "off", reason: "", available: true },
        { name: "ocr", state: "unavailable", reason: "not installed: tesseract", available: null },
        { name: "mcp", state: "off", reason: "no server", available: false },
      ],
    },
    changes: [{ at: 2, time: 0, changes: [{ item: "effort", before: "medium", after: "xhigh" }] }],
    error: "",
  };
}

/** @param {unknown} value */
const plain = (value) => JSON.parse(JSON.stringify(value));

test("each condition is a chip: off-but-available warns, on-but-unavailable errs, a change shows before → after", () => {
  const page = loadPage();
  const chips = plain(page.conditionChips(conditionsView())).map((/** @type {any} */ c) => [
    c.text,
    c.kind,
    c.changed,
    c.label,
  ]);
  assert.deepStrictEqual(chips, [
    ["reasoning on", "", false, "reasoning: on"],
    ["effort medium → xhigh", "", true, "effort: xhigh, changed from medium"],
    ["keep reasoning on", "", false, "keep reasoning: on"],
    ["images on", "", false, "images: on"],
    // Known bad, each a different row: an off capability that would work is not a plain "off" ...
    ["⚠ browser off (available)", "warn", false, "browser: off, but available"],
    // ... one switched on that cannot work is an error, and says so in words ...
    [
      "✕ ocr off → unavailable",
      "error",
      true,
      "ocr: switched on but unavailable: not installed: tesseract, changed from off",
    ],
    // ... and an off capability that would not work stays quiet.
    ["mcp off", "", false, "mcp: off"],
  ]);
  const titles = plain(page.conditionChips(conditionsView())).map((/** @type {any} */ c) => c.title);
  assert.strictEqual(titles[4], "off, but it would work: turn it on with saddle capabilities enable browser");
  assert.strictEqual(titles[6], "off; it would not work now: no server");
});

test("reasoning off and keep-reasoning off warn; a view with no start compares with itself", () => {
  const page = loadPage();
  const view = conditionsView();
  const current = { ...view.current, reasoning: "off", effort: "none", keep_reasoning: "off", rows: [] };
  const chips = plain(page.conditionChips({ current, start: null, changes: [], error: "" }));
  assert.deepStrictEqual(
    chips.map((/** @type {any} */ c) => [c.text, c.kind, c.changed]),
    [
      ["⚠ reasoning off", "warn", false],
      ["effort none", "", false],
      ["⚠ keep reasoning off", "warn", false],
    ],
  );
});

test("switches that cannot be read are one error chip naming why", () => {
  const page = loadPage();
  const chips = plain(page.conditionChips({ current: null, start: null, changes: [], error: "bad file" }));
  assert.deepStrictEqual(chips, [
    {
      text: "✕ capability switches unreadable: bad file",
      kind: "error",
      changed: false,
      label: "error: capability switches unreadable: bad file",
      title: "bad file",
    },
  ]);
});

test("the strip paints one named list item per chip, and a change adds a line to the transcript", () => {
  const page = loadPage();
  page.paintConditions(conditionsView());
  const strip = page.document.querySelector("#conditions");
  const items = strip.children.map((/** @type {any} */ li) => [
    li.tagName,
    li.className,
    li.getAttribute("aria-label"),
  ]);
  assert.strictEqual(items.length, 7);
  assert.deepStrictEqual(items[1], ["LI", "cond cond-changed", "effort: xhigh, changed from medium"]);
  assert.deepStrictEqual(items[4], ["LI", "cond cond-warn", "browser: off, but available"]);
  assert.deepStrictEqual(items[5][1], "cond cond-error cond-changed");
  assert.deepStrictEqual(items[0][1], "cond");

  const row = { at: 3, time: 1_700_000_000, changes: [{ item: "keep_reasoning", before: "on", after: "off" }] };
  const line = page.conditionsLine(row);
  assert.strictEqual(line.className, "conditions-change");
  assert.strictEqual(line.dataset.at, "3");
  assert.strictEqual(line.getAttribute("role"), "note");
  assert.match(line.textContent, /^⚑ .+ conditions changed: keep reasoning on → off$/);
  assert.strictEqual(
    page.changeText([
      { item: "effort", before: "low", after: "none" },
      { item: "images", before: "off", after: "on" },
    ]),
    "effort low → none; images off → on",
  );

  // The live event: the strip is repainted from its view and the line lands in the transcript.
  const transcript = page.document.querySelector("#transcript");
  const before = transcript.children.length;
  page.handle({ kind: "conditions.changed", ...row, view: { ...conditionsView(), start: null } });
  assert.strictEqual(transcript.children.length, before + 1);
  assert.match(transcript.children.at(-1).textContent, /keep reasoning on → off$/);
  assert.strictEqual(strip.children.at(-1).className, "cond");
});

/* ---------- which stored question each question bubble holds (#84) ---------- */

const NOTE_TEXT =
  "[Earlier conversation compacted: 4 earlier message(s) dropped to fit the window.]\n" +
  "Run state, rebuilt from the run's records (not model-written):\n- task: fix the reporter";

/** A row as `/api/sessions/{sid}/messages` answers it: the stored message, the
    index it sits at now, and `note` saying the system wrote it. */
function storedRow(
  /** @type {number} */ index,
  /** @type {string} */ role,
  /** @type {any} */ content,
  /** @type {boolean} */ note = false,
) {
  return { index, role, content, note };
}

function askedBubble(/** @type {string} */ said, /** @type {boolean} */ task = false) {
  return { said, task };
}

/** A turn as the pairing sees it: only what `numberTurn` and `bubbleQuestion`
    touch. `inserted` is the tools bar a numbering adds; `toolsRemoved` says a
    numbering took the bar away because this question is no longer in the store. */
function fakeAskTurn(
  /** @type {string} */ said,
  /** @type {{task?: boolean, tools?: boolean}} */ { task = false, tools = false } = {},
) {
  /** @type {Record<string, any>} */
  const found = { ".user": { textContent: said } };
  const turn = {
    dataset: /** @type {Record<string, string>} */ ({}),
    firstChild: null,
    inserted: /** @type {any} */ (null),
    toolsRemoved: false,
    querySelector: (/** @type {string} */ selector) => found[selector] ?? null,
    insertBefore: (/** @type {any} */ node) => {
      turn.inserted = node;
      found[".turn-tools"] = node;
      return node;
    },
  };
  if (task) found[".task-ask-bubble"] = { className: "user task-ask-bubble" };
  if (tools) {
    const bar = { className: "turn-tools", remove: () => (turn.toolsRemoved = true) };
    found[".turn-tools"] = bar;
  }
  return turn;
}

/** A page whose transcript holds exactly these turns, and whose stored-message
    request answers with `stored`. */
function turnPage(/** @type {any[]} */ turns, /** @type {any[]} */ stored) {
  const page = loadPage({
    // Only the stored-messages request is answered; whatever else the page
    // asks for on load is left outstanding, as the inert page leaves it.
    fetch: (/** @type {string} */ url) =>
      url.includes("/messages") ? Promise.resolve({ ok: true, json: async () => stored }) : new Promise(() => {}),
  });
  // `$("#transcript")` is looked up when the page numbers the turns, not when
  // it loads, so the transcript the fixture hands out can hold exactly these.
  /** @type {any} */
  const transcript = page.document.querySelector("#transcript");
  transcript.querySelectorAll = (/** @type {string} */ selector) => (selector === ".turn" ? turns : []);
  inScope(page, "state.sessionId = 'abc'");
  return page;
}

test("a stored question reads as the words the person used, whatever shape the row is in", () => {
  const page = loadPage();
  assert.strictEqual(
    page.askWords(
      storedRow(2, "user", [{ type: "text", text: "look at " }, { type: "image_url" }, { type: "text", text: "this" }]),
    ),
    "look at this",
  );
  for (const nothing of [null, undefined, "", []]) {
    assert.strictEqual(page.askWords(storedRow(2, "user", nothing)), "");
  }
  // Known bad for the pairing: a row whose text is only one part of a longer
  // question is not that question, and joining the parts is what says so.
  assert.strictEqual(page.askWords(storedRow(2, "user", [{ type: "text", text: "fix " }])), "fix ");
});

test("a bubble answers to the row that is that question, not to one that only contains it", () => {
  const page = loadPage();
  const said = page.asksTheSame;
  assert.strictEqual(said(askedBubble("fix the tests"), storedRow(2, "user", "fix the tests")), true);
  // A stored question names the files that came with it, on lines of its own.
  assert.strictEqual(
    said(askedBubble("fix the tests"), storedRow(2, "user", "fix the tests\n\n[also attached in uploads: a.py]")),
    true,
  );
  // Known bad: "fix the tests later" contains the bubble's words and is not it.
  assert.strictEqual(said(askedBubble("fix the tests"), storedRow(2, "user", "fix the tests later")), false);
  assert.strictEqual(said(askedBubble("fix the tests"), storedRow(2, "user", "please fix the tests")), false);
  // The bubble's words are what the page read off the screen, edges trimmed.
  assert.strictEqual(said(askedBubble("\n  fix the tests \n"), storedRow(2, "user", "fix the tests")), true);
  // A Task's bubble stands for a run, so it answers to that run's recap row.
  const task = askedBubble("make the suite green", true);
  assert.strictEqual(said(task, storedRow(9, "user", "[saddle task ab12cd] make the suite green")), true);
  assert.strictEqual(said(task, storedRow(9, "user", "an ordinary question")), false);
  // The system's own line is not anybody's question.
  assert.strictEqual(said(askedBubble("first question"), storedRow(1, "user", NOTE_TEXT, true)), false);
});

test("a turn that compacted the context still names the question that just finished", () => {
  const page = loadPage();
  const three = [askedBubble("first question"), askedBubble("second question"), askedBubble("third question")];
  const before = [
    storedRow(0, "system", "instructions"),
    storedRow(1, "user", "first question"),
    storedRow(2, "assistant", "an answer"),
    storedRow(3, "user", "second question"),
    storedRow(4, "assistant", "another answer"),
    storedRow(5, "user", "third question"),
  ];
  // Nothing dropped: every bubble names its own row, with the answers between
  // them. Pairing by counting instead named [0, 1, 2], so the buttons on the
  // second bubble rewound to the first bubble's answer.
  assert.deepStrictEqual(page.askedIndices(three, before), [1, 3, 5]);

  // After a compaction the questions it dropped are gone from the store and
  // the system's note sits where they were. The questions the store kept keep
  // their own indexes, and the bubble whose question is gone is named by nothing.
  const after = [
    storedRow(0, "system", "instructions"),
    storedRow(1, "user", NOTE_TEXT, true),
    storedRow(2, "assistant", "another answer"),
    storedRow(3, "user", "second question"),
    storedRow(4, "assistant", "another answer"),
    storedRow(5, "user", "third question"),
  ];
  assert.deepStrictEqual(page.askedIndices(three, after), [-1, 3, 5]);
  // The note counts as a question only to a pairing that cannot tell it apart:
  // counted as one, this same call matches nothing and names no button at all.
  assert.deepStrictEqual(
    page.askedIndices([askedBubble("second question"), askedBubble("third question")], after),
    [3, 5],
  );
  assert.deepStrictEqual(
    page.askedIndices([askedBubble("first question")], [storedRow(1, "user", NOTE_TEXT, true)]),
    [-1],
  );

  // Lists that do not line up name nothing: mid-turn the answer is not stored
  // yet, and naming turns from a list that disagrees puts the buttons on a
  // question the person never asked.
  assert.strictEqual(page.askedIndices([askedBubble("second question"), askedBubble("not stored yet")], after), null);
  assert.deepStrictEqual(page.askedIndices([], before), []);
  assert.deepStrictEqual(
    page.askedIndices([askedBubble("anything at all")], [storedRow(0, "system", "instructions")]),
    [-1],
  );
});

test("a compaction note is drawn as the system's line, with no question and no buttons", () => {
  const page = loadPage();
  const line = page.compactionNote(NOTE_TEXT);
  assert.strictEqual(line.tagName, "DIV");
  assert.strictEqual(line.className, "compaction");
  assert.strictEqual(line.getAttribute("role"), "note");
  assert.deepStrictEqual(
    line.children.map((/** @type {any} */ node) => [node.tagName, node.className, node.textContent]),
    [
      ["B", "", "Context compacted"],
      ["SPAN", "compaction-words", NOTE_TEXT],
    ],
  );
  // A line nothing rewinds to: no buttons on it, and no question inside it.
  assert.strictEqual(line.children.length, 2);
  assert.ok(!line.children.some((/** @type {any} */ node) => node.tagName === "BUTTON"));
  assert.strictEqual(page.compactionNote(null).children[1].textContent, "");
});

test("a turn is named with the index it belongs to, and a Task's bubble is not given buttons", () => {
  const page = loadPage();
  const turn = fakeAskTurn("fix the tests");
  page.numberTurn(turn, 7);
  assert.strictEqual(turn.dataset.index, "7");
  assert.strictEqual(turn.inserted.className, "turn-tools");
  assert.deepStrictEqual(
    turn.inserted.children.map((/** @type {any} */ button) => button.title),
    ["Run this again", "Edit and run again"],
  );

  // A run's recap is not a question to answer again in chat.
  const card = fakeAskTurn("make the suite green", { task: true });
  page.numberTurn(card, 9);
  assert.strictEqual(card.dataset.index, "9");
  assert.strictEqual(card.inserted, null);

  // Named once already, a turn keeps the buttons it has.
  const again = fakeAskTurn("fix the tests", { tools: true });
  page.numberTurn(again, 4);
  assert.strictEqual(again.dataset.index, "4");
  assert.strictEqual(again.inserted, null);
});

test("a question bubble is what the pairing needs: its words, and whose run it stands for", () => {
  const page = loadPage();
  const asked = page.bubbleQuestion(fakeAskTurn("\n  fix the tests  \n"));
  assert.strictEqual(asked.said, "fix the tests");
  assert.strictEqual(asked.task, false);
  const recap = page.bubbleQuestion(fakeAskTurn("make it green", { task: true }));
  assert.strictEqual(recap.said, "make it green");
  assert.strictEqual(recap.task, true);
  assert.strictEqual(page.isTaskAsk(fakeAskTurn("say anything", { task: true })), true);
  assert.strictEqual(page.isTaskAsk(fakeAskTurn("say anything")), false);
});

test("after a turn the buttons name the question the store holds, and none it dropped", async () => {
  const stored = [
    storedRow(0, "system", "instructions"),
    storedRow(1, "user", NOTE_TEXT, true),
    storedRow(2, "assistant", "an answer"),
    storedRow(3, "user", "second question"),
    storedRow(4, "assistant", "another answer"),
    storedRow(5, "user", "third question"),
  ];
  const dropped = fakeAskTurn("first question", { tools: true });
  dropped.dataset.index = "1"; // the index a compaction has just taken away
  const turns = [dropped, fakeAskTurn("second question"), fakeAskTurn("third question")];
  await turnPage(turns, stored).restampTurns();
  // The question the store no longer holds keeps neither the old index nor the
  // buttons that named it: rewinding there would cut the chat at the system's
  // note, or at whatever moved into its place.
  assert.strictEqual(dropped.dataset.index, undefined);
  assert.strictEqual(dropped.toolsRemoved, true);
  // The questions the store does hold are named by their own rows.
  assert.strictEqual(turns[1].dataset.index, "3");
  assert.strictEqual(turns[2].dataset.index, "5");
  assert.strictEqual(turns[1].inserted.className, "turn-tools");
  assert.strictEqual(turns[2].inserted.className, "turn-tools");
});

test("a store that does not match the screen leaves every turn as it is", async () => {
  const turns = [fakeAskTurn("second question", { tools: true }), fakeAskTurn("a question still being answered")];
  const stored = [storedRow(3, "user", "second question"), storedRow(4, "user", "an older question")];
  turns[1].dataset.index = "9";
  await turnPage(turns, stored).restampTurns();
  assert.strictEqual(turns[1].dataset.index, "9");
  assert.strictEqual(turns[1].toolsRemoved, false);
});

test("a session that compacted opens with the system's line where the questions were dropped", () => {
  const page = loadPage();
  page.renderHistory({
    kind: "chat",
    session_id: "abc",
    title: "compacted chat",
    workdir: "/work/here",
    branch: "main",
    persona: "default",
    mode: "default",
    full_access: false,
    messages: [
      { index: 1, role: "user", content: "first question" },
      { index: 2, role: "assistant", content: "an answer" },
      { index: 3, role: "user", content: NOTE_TEXT, note: true },
      { index: 4, role: "user", content: "fix it and show the test" },
    ],
  });
  const drawing = page.document.querySelector("#transcript");
  // One line per stored message, in the order the store holds them: the note is
  // not dropped, and the question after it does not slide up a place.
  assert.deepStrictEqual(
    [...drawing.children].map((/** @type {any} */ node) => [node.tagName, node.className]),
    [
      ["DIV", "turn"],
      ["DIV", "turn"],
      ["DIV", "compaction"],
      ["DIV", "turn"],
    ],
  );
  const note = /** @type {any} */ (drawing.children[2]);
  assert.strictEqual(note.getAttribute("role"), "note");
  assert.strictEqual(note.children[0].textContent, "Context compacted");
  assert.strictEqual(note.children[1].textContent, NOTE_TEXT);
  // Known-bad, which is the bug: the note drawn as a third question, with a
  // turn of its own and an index a rewind could aim at.
  assert.strictEqual(note.children.length, 2);
  const asked = [...drawing.children].filter(
    (/** @type {any} */ node) => node.className === "turn" && node.dataset.index !== undefined,
  );
  assert.strictEqual(asked.length, 2);
  assert.deepStrictEqual(
    asked.map((/** @type {any} */ turn) => turn.dataset.index),
    ["1", "4"],
  );
  // Each question keeps the tools a rewind needs, and the note keeps none.
  assert.deepStrictEqual(
    asked
      .map((/** @type {any} */ turn) => turn.children)
      .flat()
      .filter((/** @type {any} */ child) => child.className === "turn-tools").length,
    2,
  );
});

/* ---------- the topbar's status pill in a task session (#85 item 3) ---------- */

/* In a task run's session the card carries the state -- "● running",
   "? needs you", "■ stopped" -- and the topbar's pill read that state beside
   it, twice on one screen. The rule now: the pill holds only what the card
   leaves unsaid once it scrolls off a phone screen -- "needs you", "stopped",
   "no outcome", and a state no card names at all -- and stays out of the way
   where it would read the card's own words a second time. A chat session's pill
   is as it was.

   `pageWithStubs` is the page as index.html loads it (`loadPage`), with inert
   wiring for what these tests are not about: the packet request an ended run's
   card makes, the toast, and notify.js's tab title, sidebar dot and live
   region. Each state reaches the page the way its stream delivers it, through
   `handleTask`; the reading is the element the topbar paints, the same one
   `document.querySelector("#status")` reaches in the page. */

function pageWithStubs() {
  const page = loadPage();
  const doc = page.document;
  doc.getElementById = (/** @type {string} */ id) => doc.querySelector(`#${id}`); // some boxes the page asks for by id
  page.api = async () => ({}); // the packet an ended run's card would fetch
  page.notice = () => {}; // the toast's text
  page.runState = () => {}; // notify.js: the tab, the sidebar dot, the live region
  return page;
}

/** One run's state, as the session's event stream delivers it. */
function reportRun(/** @type {any} */ page, /** @type {string} */ state) {
  page.handleTask({
    kind: "task.state",
    run_id: "pill-test-run",
    task: "close the duplicate status pill",
    state,
    question: state === "needs_you" ? { text: "Submit the work or stop?", options: ["Submit", "Stop"] } : undefined,
  });
}

/** The Send button, which the page turns into the Stop a going run or a turn
   that waits for you needs: the pill's state is what decides its face. */
function topbarSend(/** @type {any} */ page) {
  const button = page.document.querySelector("#send");
  return {
    glyph: button.textContent,
    title: button.title,
    stopping: button.classList.contains("stopping"),
  };
}

/** What the topbar's status pill is: whether the page shows it, its face, the
   word it reads, and the name a screen reader is given. */
function topbarPill(/** @type {any} */ page) {
  const node = page.document.querySelector("#status");
  return {
    shown: !node.hidden,
    face: node.className,
    words: node.textContent,
    name: node.getAttribute("aria-label"),
  };
}

/** The pill this session's topbar wears when a run that was going reaches
   `state`, in the lane named: `showMode("task")` is what puts the page in the
   task lane, the same `body` hook the context meter follows. */
function pillAfterRun(/** @type {string} */ state, /** @type {string} */ lane) {
  const page = pageWithStubs();
  page.showMode(lane);
  reportRun(page, "running");
  reportRun(page, state);
  return topbarPill(page);
}

test("a task session hides the pill for every state the run's own card already reads", () => {
  // Known-bad, the defect itself: the topbar read "task running" beside the
  // card's "● running", and read "finished" and "unchanged" the same way.
  /** @type {Record<string, any>} */
  const seen = {};
  for (const state of ["running", "finished", "unchanged", "loading"]) {
    seen[state] = pillAfterRun(state, "task").shown;
  }
  assert.deepStrictEqual(seen, { running: false, finished: false, unchanged: false, loading: false });
});

test("a task session keeps the pill for needs you, stopped, no outcome and asked, each in its own face", () => {
  // Kept on purpose, in words a phone and a screen reader both reach: with the
  // card scrolled off, the topbar is where these states are read.
  /** @type {Record<string, any>} */
  const seen = {};
  for (const state of ["needs_you", "stopped", "failed", "asked"]) {
    seen[state] = pillAfterRun(state, "task");
  }
  assert.deepStrictEqual(seen, {
    needs_you: { shown: true, face: "status needs", words: "needs you", name: "needs" },
    stopped: { shown: true, face: "status stopped", words: "stopped", name: "stopped" },
    failed: { shown: true, face: "status failed", words: "no outcome", name: "failed" },
    asked: { shown: true, face: "status needs", words: "needs you", name: "needs" },
  });
});

test("the Stop this page pressed reads stopping in the pill until the run's own state arrives", () => {
  const page = pageWithStubs();
  page.showMode("task");
  reportRun(page, "running");
  assert.strictEqual(topbarPill(page).shown, false); // the card's "● running" is enough
  page.stopTask("pill-test-run");
  assert.deepStrictEqual(topbarPill(page), {
    shown: true,
    face: "status working",
    words: "stopping…",
    name: "working",
  });
  reportRun(page, "stopped");
  assert.deepStrictEqual(topbarPill(page), {
    shown: true,
    face: "status stopped",
    words: "stopped",
    name: "stopped",
  });
});

test("a state no run card names still reaches the pill, where no card would say it", () => {
  const page = pageWithStubs();
  page.showMode("task");
  reportRun(page, "running");
  page.setStatus("error"); // EventSource's onerror: no card says anything about a broken stream
  assert.deepStrictEqual(topbarPill(page), { shown: true, face: "status error", words: "error", name: "error" });
});

test("a chat session shows the pill for every state, as it did before the task lane had a card", () => {
  /** @type {Record<string, any>} */
  const seen = {};
  for (const kind of ["idle", "working", "needs", "error"]) {
    const page = pageWithStubs(); // the page's own lane is chat: no task chrome
    page.setStatus(kind, kind === "working" ? "task running" : undefined);
    seen[kind] = topbarPill(page);
  }
  assert.deepStrictEqual(seen, {
    idle: { shown: true, face: "status idle", words: "idle", name: "idle" },
    working: { shown: true, face: "status working", words: "task running", name: "working" },
    needs: { shown: true, face: "status needs", words: "needs", name: "needs" },
    error: { shown: true, face: "status error", words: "error", name: "error" },
  });
});

test("a run reported to a session in the Ask lane reads its own state there, and the Send button follows", () => {
  // Beyond the task lane the topbar is the only place a run's state is spoken,
  // so this is the page's old behaviour, kept as it was: the task lane's rule
  // says nothing about a chat session's pill.
  /** @type {Record<string, any>} */
  const seen = {};
  const page = pageWithStubs();
  page.showMode("ask");
  for (const state of ["running", "needs_you", "stopped"]) {
    reportRun(page, state);
    seen[state] = { pill: topbarPill(page), send: topbarSend(page) };
  }
  assert.deepStrictEqual(seen, {
    running: {
      pill: { shown: true, face: "status working", words: "task running", name: "working" },
      send: { glyph: "■", title: "Stop", stopping: true },
    },
    needs_you: {
      pill: { shown: true, face: "status needs", words: "needs you", name: "needs" },
      send: { glyph: "■", title: "Stop", stopping: true },
    },
    // An ended state is the card's to say, so here the topbar keeps the state it
    // last spoke rather than inventing a fourth one.
    stopped: {
      pill: { shown: true, face: "status needs", words: "needs you", name: "needs" },
      send: { glyph: "■", title: "Stop", stopping: true },
    },
  });
});

test("a task session reaches for the Stop its run needs, and holds the state it last spoke", () => {
  // The pill's face is not the only thing a run's state decides: while a run is
  // going, or waits for an answer, the Send button is the Stop. What the card
  // ends leaves it standing, exactly as it did before this item.
  const page = pageWithStubs();
  page.showMode("task");
  reportRun(page, "running");
  assert.deepStrictEqual(topbarSend(page), { glyph: "■", title: "Stop", stopping: true });
  // Hidden, but still the state the page last spoke -- which is also what a
  // reader is given if the lane changes before the next event arrives.
  assert.deepStrictEqual(topbarPill(page), {
    shown: false,
    face: "status working",
    words: "task running",
    name: "working",
  });
  reportRun(page, "needs_you");
  assert.deepStrictEqual(topbarSend(page), { glyph: "■", title: "Stop", stopping: true });
  assert.deepStrictEqual(topbarPill(page), {
    shown: true,
    face: "status needs",
    words: "needs you",
    name: "needs",
  });
  reportRun(page, "unchanged");
  assert.deepStrictEqual(topbarPill(page), {
    shown: false,
    face: "status needs",
    words: "needs you",
    name: "needs",
  });
});

test("a session that has run nothing wears its lane's pill: idle in the chat lane, hidden in the task lane", () => {
  const ask = pageWithStubs();
  ask.showMode("ask");
  const task = pageWithStubs();
  task.showMode("task");
  assert.deepStrictEqual(topbarPill(ask), {
    shown: true,
    face: "status idle",
    words: "idle",
    name: "idle",
  });
  assert.deepStrictEqual(topbarPill(task), {
    shown: false,
    face: "status idle",
    words: "idle",
    name: "idle",
  });
});

test("switching out of the task lane takes the card's rule with it, both ways", () => {
  const page = pageWithStubs();
  page.setStatus("working", "task running");
  const ask = topbarPill(page); // a chat session, before any Task run: shown, as ever
  page.showMode("task");
  const task = topbarPill(page); // the task lane, with no run of its own yet
  page.showMode("ask");
  const back = topbarPill(page); // switched out: chat chrome, the pill with it
  assert.deepStrictEqual(
    [ask, task, back],
    [
      { shown: true, face: "status working", words: "task running", name: "working" },
      { shown: false, face: "status working", words: "task running", name: "working" },
      { shown: true, face: "status working", words: "task running", name: "working" },
    ],
  );
});
