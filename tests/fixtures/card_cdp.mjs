// The task card's meters across a reload, and its model-activity strip, in
// headless Chrome over CDP; prints one JSON line. Used by tests/test_card_ui.py.
//
// usage: node card_cdp.mjs <base-url> <session-id> <width> <scheme> [shot-dir] [prefix]
//   Starts a task in <session-id> from the page and, during the model's
//   first reply, reports the strip while it streams (folded, then open) and
//   the time meter just before and just after a reload; then, once the first
//   tool call has run, the strip again; the card after Stop; and the recap
//   card after a second reload.
//   <scheme> is "dark" or "light" (prefers-color-scheme).
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { coverage } from "./cdp_coverage.mjs";

const [base, sid, widthArg, scheme, shots, prefix = ""] = process.argv.slice(2);
const W = Number(widthArg) || 1280;
const port = 9300 + Math.floor(Math.random() * 600);
const prof = mkdtempSync(join(tmpdir(), "cdp-card-"));
const chrome = spawn(
  "google-chrome",
  [
    "--headless=new",
    "--no-sandbox",
    "--disable-gpu",
    "--hide-scrollbars",
    `--remote-debugging-port=${port}`,
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
  const until = async (/** @type {string} */ expression, /** @type {string} */ what, tries = 300) => {
    for (let i = 0; i < tries; i++) {
      if (await js(expression).catch(() => false)) return;
      await sleep(200);
    }
    throw new Error(`timed out waiting for ${what}`);
  };
  const shot = async (/** @type {string} */ name) => {
    if (!shots) return;
    await js(
      `(() => { const c = document.querySelector(".task-card"); if (c) c.scrollIntoView({ block: "start" }); })()`,
    );
    await sleep(250);
    const s = await send("Page.captureScreenshot", { format: "png" });
    writeFileSync(join(shots, `${prefix}${name}`), Buffer.from(s.result.data, "base64"));
  };
  const load = async () => {
    await send("Page.navigate", { url: `${base}/` });
    await until(`typeof state !== "undefined" && state.sessionId === ${JSON.stringify(sid)}`, "the page");
  };
  // What the card shows, read from the DOM, plus the elapsed seconds its
  // meter is drawing (the card's own numbers, not the server's).
  const card = () =>
    js(`(() => {
    const n = document.querySelector(".task-card");
    if (!n) return null;
    const c = tasks.get(n.dataset.run);
    const meters = n.querySelector(".task-meters");
    const a = n.querySelector(".task-activity");
    return {
      state: n.dataset.state,
      time: n.querySelectorAll(".tmeter-value")[0].textContent,
      tokens: n.querySelectorAll(".tmeter-value")[1].textContent,
      metersShown: !!meters && getComputedStyle(meters).display !== "none",
      shown: c.elapsed + (c.state === "running" ? (Date.now() - c.elapsedAt) / 1000 : 0),
      lines: n.querySelectorAll(".task-lines li").length,
      spent: c.spent,
      strip: a ? {
        shown: getComputedStyle(a).display !== "none",
        open: a.open,
        head: a.querySelector("summary").textContent,
        mode: a.dataset.mode,
        chars: c.activity.chars,
        tailLabel: a.querySelector(".act-tail-label").textContent,
        tail: a.querySelector(".act-tail").textContent,
        streamed: a.querySelectorAll(".act-facts dd")[1].textContent,
        tool: a.querySelectorAll(".act-facts dd")[2].textContent,
        inLedger: [...n.querySelectorAll(".task-lines li")].some((li) =>
          c.activity.text && li.textContent.includes(c.activity.text.trim().slice(-20))),
      } : null,
      packet: !n.querySelector(".packet").hidden && !!n.querySelector(".packet .verdict"),
    };
  })()`);

  await send("Emulation.setDeviceMetricsOverride", {
    width: W,
    height: W < 700 ? 812 : 900,
    deviceScaleFactor: 1,
    mobile: W < 700,
  });
  await send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: scheme || "dark" }] });
  await send("Page.enable");
  await send("Runtime.enable");
  // The session the page opens on is remembered per browser.
  await send("Page.navigate", { url: `${base}/` });
  await until(`typeof state !== "undefined" && !!state.sessionId`, "the page");
  await js(`localStorage.setItem("saddle.session", ${JSON.stringify(sid)})`);
  await load();
  /** @type {Record<string, any>} */
  const out = {};
  // Count every request the page makes from here on: the strip makes none.
  await js(`(() => { window.__posts = 0; const f = window.fetch; window.fetch = (u, o) => {
    if (o && o.method && o.method !== "GET") window.__posts += 1; return f(u, o); }; })()`);
  await js(`launchTask("make add add", 1800, 100000, true)`);
  const posts = await (async () => {
    await until(`!!document.querySelector(".task-card")`, "the card");
    return js(`window.__posts`);
  })();
  await until(
    `(() => { const n = document.querySelector(".task-card"); return n && tasks.get(n.dataset.run).activity.chars > 200; })()`,
    "a streaming reply",
  );
  out.streaming = [await card()];
  await sleep(1500);
  out.streaming.push(await card());
  await shot("running-collapsed.png");
  await js(`document.querySelector(".task-activity > summary").click()`);
  await sleep(700);
  out.streaming.push(await card());
  out.stripPosts = (await js(`window.__posts`)) - posts;
  await shot("running-open.png");

  // Reload mid-reply: the engine reports the streaming reply's share as
  // partial progress about once a second; the snapshot must not undercut it.
  await sleep(1500);
  out.beforeReload = await card();
  await load();
  // Read as soon as the card is built from the snapshot, before the next
  // partial progress event can repaint it.
  await until(`!!document.querySelector(".task-card[data-state=running]")`, "the card after a reload", 1500);
  out.firstAfterReload = await card();
  await sleep(400);
  out.afterReload = await card();
  await shot("reloaded.png");
  // The first tool call ends the reply the reloaded page joined part-way.
  await until(
    `/Read calc.py/.test(document.querySelectorAll(".task-card .act-facts dd")[2].textContent)`,
    "the first tool call",
    150,
  );
  await until(`tasks.get(document.querySelector(".task-card").dataset.run).activity.chars > 0`, "the next reply");
  await sleep(400);
  out.afterTool = await card();

  await js(`stopTask(document.querySelector(".task-card").dataset.run)`);
  await until(`!!document.querySelector(".task-card .packet .verdict")`, "the stopped card's packet");
  await sleep(600);
  out.stopped = await card();
  await shot("stopped.png");
  await load();
  await until(
    `!!document.querySelector(".task-card .packet .verdict") && document.querySelector(".task-card").dataset.state !== "loading"`,
    "the recap card",
  );
  await sleep(600);
  out.recap = await card();
  out.cost = await js(
    `[...document.querySelectorAll(".task-card .prow.k-cost .prow-text")].map((p) => p.textContent)[0] || ""`,
  );
  await shot("stopped-reloaded.png");
  console.log(JSON.stringify(out));
  await cov.save();
  await finish(0);
} catch (error) {
  console.error(String(/** @type {any} */ (error)?.stack || error));
  await finish(1);
}
