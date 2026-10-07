// Drives the chat page in headless Chrome over CDP for the Runs group, the
// delete undo toast and the running card's phase line. Used by
// tests/test_runs.py; node >= 22 and Chrome only.
//
// usage: node runs_cdp.mjs <base> <sid> <runs|phase|undo|expire> <other-sid> [shot-dir]
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { coverage } from "./cdp_coverage.mjs";
import { activePort, chromeEnv } from "./cdp_port.mjs";

const [base, sid, step, other, shots] = process.argv.slice(2);
const prof = mkdtempSync(join(tmpdir(), "cdp-runs-"));
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
  { stdio: "ignore", env: chromeEnv() },
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
  const shot = async (/** @type {string} */ name) => {
    if (!shots) return;
    const s = await send("Page.captureScreenshot", { format: "png" });
    writeFileSync(join(shots, name), Buffer.from(s.result.data, "base64"));
  };
  const load = async () => {
    await send("Page.navigate", { url: `${base}/` });
    for (let i = 0; i < 50; i++) {
      await sleep(200);
      const ready = await js(
        `typeof state !== "undefined" && state.sessionId === ${JSON.stringify(sid)} && !!document.querySelector("#mode-chip") && document.querySelector("#mode-chip").textContent !== ""`,
      ).catch(() => false);
      if (ready) break;
    }
    await sleep(300);
    // Count what the page asks the server to do; the server counts too.
    await js(`(() => {
      const real = window.fetch;
      window.fetch = (url, opts) => {
        const u = String(url);
        if (u.endsWith("/task") && opts && opts.method === "POST") window.__taskPosts = (window.__taskPosts || 0) + 1;
        if (u.endsWith("/message") && opts && opts.method === "POST") window.__chatPosts = (window.__chatPosts || 0) + 1;
        return real(url, opts);
      };
    })()`);
  };

  const until = async (/** @type {string} */ cond, ms = 8000) => {
    for (let t = 0; t < ms; t += 100) {
      if (await js(cond)) return true;
      await sleep(100);
    }
    return false;
  };
  const start = (/** @type {string} */ s, /** @type {string} */ text) =>
    js(
      `fetch("/api/sessions/${s}/task", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({text: ${JSON.stringify(text)}})}).then((r) => r.json()).then((j) => j.run_id)`,
    );
  const listed = () => js(`[...document.querySelectorAll("#session-list .session")].map((r) => r.dataset.sid)`);

  await send("Page.addScriptToEvaluateOnNewDocument", {
    source: `
    try { localStorage.setItem("saddle.session", ${JSON.stringify(sid)}); } catch {}
  `,
  });
  await send("Emulation.setDeviceMetricsOverride", { width: 1200, height: 800, deviceScaleFactor: 2, mobile: false });
  await send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: "dark" }] });
  await send("Page.enable");
  await send("Runtime.enable");
  await load();
  /** @type {Record<string, any>} */
  const out = {};
  if (step === "runs") {
    await start(other, "ask which schema the loader should use for v2 files");
    await sleep(300);
    await start(sid, "fix the parser");
    await until(
      `document.querySelectorAll("#run-list .run-row").length === 2 && !!document.querySelector('#run-list .run-row[data-state="needs_you"]')`,
    );
    await sleep(1200);
    out.active = await js(`state.sessionId`);
    out.rows = await js(`[...document.querySelectorAll("#run-list .run-row")].map((r) => ({
      run: r.dataset.run, sid: r.dataset.sid, state: r.dataset.state,
      task: r.querySelector(".run-task").textContent, time: r.querySelector(".run-time").textContent,
      lane: r.querySelector(".run-lane").textContent }))`);
    await shot("sidebar-runs-dark.png");
    await js(`document.querySelector('#run-list .run-row[data-sid="${other}"]').click()`);
    await until(`state.sessionId === ${JSON.stringify(other)} && !!document.querySelector(".task-card.flash")`);
    out.jumped = await js(
      `({ active: state.sessionId, card: !!document.querySelector('.task-card[data-state="needs_you"]') })`,
    );
    await shot("runs-jump-dark.png");
  } else if (step === "phase") {
    await start(sid, "fix the parser");
    await until(`(document.querySelector(".task-card .task-now") || {}).textContent?.startsWith("editing")`);
    // Take the polls out and count every request from here on.
    await js(`pollSessions = () => {}; loadRuns = () => Promise.resolve(); window.__fetches = 0;
      const real = window.fetch; window.fetch = (...a) => { window.__fetches += 1; return real(...a); };`);
    out.first = await js(`document.querySelector(".task-card .task-now").textContent`);
    await sleep(2600);
    out.later = await js(`document.querySelector(".task-card .task-now").textContent`);
    out.fetches = await js(`window.__fetches`);
    await shot("running-card-phase-dark.png");
  } else if (step === "undo" || step === "expire") {
    if (step === "expire") await js(`runsView.undoMs = 800`);
    await js(`document.querySelector('.session[data-sid="${other}"] .kill').click()`);
    await until(`!document.querySelector("#toast").hidden`);
    out.toast = await js(`({ shown: !document.querySelector("#toast").hidden, focus: document.activeElement.textContent,
      text: document.querySelector("#toast").textContent })`);
    out.toast.listed = await listed();
    await shot("delete-undo-toast-dark.png");
    if (step === "undo") {
      await js(`document.activeElement.click()`);
      await until(`!!document.querySelector('.session[data-sid="${other}"]')`);
      out.afterUndo = { listed: await listed() };
    } else {
      await sleep(1500);
      out.toastGone = await js(`document.querySelector("#toast").hidden`);
    }
  }
  console.log(JSON.stringify(out));
  await cov.save();
  await finish(0);
} catch (error) {
  console.error(String(/** @type {any} */ (error)?.stack || error));
  await finish(1);
}
