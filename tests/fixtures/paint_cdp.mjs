// Streams a reply into the chat page in headless Chrome, in both orders a frame
// and the turn's end can arrive, and prints one JSON line of what was painted.
// Used by tests/test_paint_order.py; node >= 22 and Chrome only.
//
// A streamed delta is painted on the next animation frame (schedulePaint); the
// turn's end paints at once and cancels a frame still pending (flushPaint).
// Which comes first was left to timing, so whether each path ran differed from
// run to run. Here each round calls the page's own event handler in one order:
//   frame-first: a delta, two frames, then turn.end;
//   end-first:   a delta and turn.end in one task, then two frames.
// The turn's end must clear the frame it cancels: if the id is left behind, the
// next delta's schedulePaint returns early and that reply is never painted.
//
// usage: node paint_cdp.mjs <base-url> <session-id>
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { coverage } from "./cdp_coverage.mjs";
import { activePort } from "./cdp_port.mjs";

const [base, sid] = process.argv.slice(2);
const prof = mkdtempSync(join(tmpdir(), "cdp-paint-"));
const chrome = spawn(
  "google-chrome",
  [
    "--headless=new",
    "--no-sandbox",
    "--disable-gpu",
    "--hide-scrollbars",
    "--remote-debugging-port=0",
    `--user-data-dir=${prof}`,
    "about:blank",
  ],
  { stdio: "ignore" },
);
const sleep = (/** @type {number} */ ms) => new Promise((r) => setTimeout(r, ms));
const finish = async (/** @type {number} */ code) => {
  chrome.kill();
  await sleep(300);
  rmSync(prof, { recursive: true, force: true });
  process.exit(code);
};

async function target() {
  for (let i = 0; i < 75; i++) {
    try {
      const port = activePort(prof);
      if (!port) throw new Error("Chrome has not written its DevTools port yet");
      const list = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
      const page = list.find((/** @type {{ type: string, webSocketDebuggerUrl: string }} */ t) => t.type === "page");
      if (page) return page.webSocketDebuggerUrl;
    } catch {
      // Chrome is not listening yet; poll again.
    }
    await sleep(200);
  }
  throw new Error("chrome did not start");
}

// One round, run in the page. `order` is "frame-first" or "end-first"; `text` is
// the delta, unique per round so its paints can be counted.
const ROUND = `async (order, text) => {
  const count = () => document.querySelector("#transcript").textContent.split(text).length - 1;
  const frames = () => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
  handle({ kind: "turn.start" });
  handle({ kind: "content.delta", text });
  const pending = paintFrame !== 0;
  const beforePaint = count();
  if (order === "frame-first") {
    await frames();
    const painted = count();
    const frameLeft = paintFrame;
    handle({ kind: "turn.end" });
    return { order, pending, beforePaint, painted, frameLeft, atEnd: count() };
  }
  handle({ kind: "turn.end" });
  const painted = count();
  const frameLeft = paintFrame;
  await frames();
  return { order, pending, beforePaint, painted, frameLeft, afterFrames: count() };
}`;

try {
  const ws = new WebSocket(await target());
  await new Promise((r) => ws.addEventListener("open", r));
  let id = 0;
  const pending = new Map();
  ws.addEventListener("message", (m) => {
    const msg = JSON.parse(m.data);
    if (msg.id && pending.has(msg.id)) {
      pending.get(msg.id)(msg);
      pending.delete(msg.id);
    }
  });
  const rawSend = (/** @type {string} */ method, params = {}) =>
    new Promise((resolve) => {
      const n = ++id;
      pending.set(n, resolve);
      ws.send(JSON.stringify({ id: n, method, params }));
    });
  const cov = coverage(rawSend);
  const send = cov.send;
  const js = async (/** @type {string} */ expression) => {
    const r = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
    if (r.result?.exceptionDetails) throw new Error(JSON.stringify(r.result.exceptionDetails));
    return r.result?.result?.value;
  };
  await send("Page.enable");
  await send("Runtime.enable");
  await send("Page.navigate", { url: `${base}/` });
  let ready = false;
  for (let i = 0; i < 75 && !ready; i++) {
    await sleep(200);
    ready = await js(
      `typeof state !== "undefined" && state.sessionId === ${JSON.stringify(sid)} && !!document.querySelector("#transcript")`,
    ).catch(() => false);
  }
  if (!ready) throw new Error("the page did not open the session");
  const rounds = [];
  for (const [order, text] of [
    ["frame-first", "first streamed reply"],
    ["frame-first", "second streamed reply"],
    ["end-first", "third streamed reply"],
    ["frame-first", "fourth streamed reply"],
  ]) {
    rounds.push(await js(`(${ROUND})(${JSON.stringify(order)}, ${JSON.stringify(text)})`));
  }
  console.log(JSON.stringify({ rounds }));
  await cov.save();
  await finish(0);
} catch (error) {
  console.error(String(/** @type {any} */ (error)?.stack || error));
  await finish(1);
}
