"use strict";
/* The sidebar and notification scripts are classic page scripts, mostly
   DOM wiring that only a real browser exercises (the Chrome-driven tests do
   that). Their pure helpers -- how a run's age reads, which state a run
   shows, what the tab icon looks like -- are loaded here into a sandbox
   scope, unedited, and asserted directly. */

const test = require("node:test");
const assert = require("node:assert");
const { loadClassic, inScope } = require("./fixtures/classic_script.js");

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
  const decode = (uri) => decodeURIComponent(uri.slice("data:image/svg+xml,".length));
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
