// The task card's meters across a reload, in headless Chrome over CDP;
// prints one JSON line. Used by tests/test_card_ui.py.
//
// usage: node card_cdp.mjs <base-url> <session-id> <width> <scheme> [shot-dir] [prefix]
//   Starts a task in <session-id> from the page, waits until the model is
//   a few seconds into its first reply, then reports: the time meter just before
//   and just after a reload; the card after Stop; and the recap card after
//   a second reload.
//   <scheme> is "dark" or "light" (prefers-color-scheme).
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const [base, sid, widthArg, scheme, shots, prefix = ""] = process.argv.slice(2);
const W = Number(widthArg) || 1280;
const port = 9300 + Math.floor(Math.random() * 600);
const prof = mkdtempSync(join(tmpdir(), "cdp-card-"));
const chrome = spawn("google-chrome", [
  "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
  `--remote-debugging-port=${port}`, `--user-data-dir=${prof}`, "about:blank",
], { stdio: "ignore" });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const finish = async (code) => {
  chrome.kill();
  await sleep(300);
  rmSync(prof, { recursive: true, force: true });
  process.exit(code);
};

async function target() {
  for (let i = 0; i < 75; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
      const page = list.find((t) => t.type === "page");
      if (page) return page.webSocketDebuggerUrl;
    } catch {}
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
    if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); }
  });
  const send = (method, params = {}) => new Promise((resolve) => {
    const n = ++id; pending.set(n, resolve); ws.send(JSON.stringify({ id: n, method, params }));
  });
  const js = async (expression) => {
    const r = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
    if (r.result?.exceptionDetails) throw new Error(JSON.stringify(r.result.exceptionDetails));
    return r.result?.result?.value;
  };
  const until = async (expression, what, tries = 300) => {
    for (let i = 0; i < tries; i++) {
      if (await js(expression).catch(() => false)) return;
      await sleep(200);
    }
    throw new Error(`timed out waiting for ${what}`);
  };
  const shot = async (name) => {
    if (!shots) return;
    await js(`(() => { const c = document.querySelector(".task-card"); if (c) c.scrollIntoView({ block: "start" }); })()`);
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
  const card = () => js(`(() => {
    const n = document.querySelector(".task-card");
    if (!n) return null;
    const c = tasks.get(n.dataset.run);
    const meters = n.querySelector(".task-meters");
    return {
      state: n.dataset.state,
      time: n.querySelectorAll(".tmeter-value")[0].textContent,
      tokens: n.querySelectorAll(".tmeter-value")[1].textContent,
      metersShown: !!meters && getComputedStyle(meters).display !== "none",
      shown: c.elapsed + (c.state === "running" ? (Date.now() - c.elapsedAt) / 1000 : 0),
      lines: n.querySelectorAll(".task-lines li").length,
      packet: !n.querySelector(".packet").hidden && !!n.querySelector(".packet .verdict"),
    };
  })()`);

  await send("Emulation.setDeviceMetricsOverride", { width: W, height: W < 700 ? 812 : 900, deviceScaleFactor: 1, mobile: W < 700 });
  await send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: scheme || "dark" }] });
  await send("Page.enable");
  await send("Runtime.enable");
  // The session the page opens on is remembered per browser.
  await send("Page.navigate", { url: `${base}/` });
  await until(`typeof state !== "undefined" && !!state.sessionId`, "the page");
  await js(`localStorage.setItem("saddle.session", ${JSON.stringify(sid)})`);
  await load();
  const out = {};
  await js(`launchTask("make add add", 1800, 100000, true)`);
  await until(`!!document.querySelector(".task-card")`, "the card");
  await sleep(3000);
  out.streaming = [await card()];
  await shot("running.png");

  // Reload mid-reply: no progress event comes until the reply ends.
  await sleep(1500);
  out.beforeReload = await card();
  await load();
  await until(`!!document.querySelector(".task-card[data-state=running]")`, "the card after a reload");
  await sleep(400);
  out.afterReload = await card();
  await shot("reloaded.png");

  await js(`stopTask(document.querySelector(".task-card").dataset.run)`);
  await until(`!!document.querySelector(".task-card .packet .verdict")`, "the stopped card's packet");
  await sleep(600);
  out.stopped = await card();
  await shot("stopped.png");
  await load();
  await until(`!!document.querySelector(".task-card .packet .verdict") && document.querySelector(".task-card").dataset.state !== "loading"`, "the recap card");
  await sleep(600);
  out.recap = await card();
  out.cost = await js(`[...document.querySelectorAll(".task-card .prow.k-cost .prow-text")].map((p) => p.textContent)[0] || ""`);
  await shot("stopped-reloaded.png");
  console.log(JSON.stringify(out));
  await finish(0);
} catch (error) {
  console.error(String(error && error.stack || error));
  await finish(1);
}
