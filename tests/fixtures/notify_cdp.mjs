// Drives the chat page in headless Chrome over CDP to check that a run finds
// you: tab title, favicon, live region, Notification and sidebar dots. The
// page's visibilityState and window.Notification are stubbed before load.
// Used by tests/test_notify.py; node >= 22 and Chrome only.
//
// usage: node notify_cdp.mjs <base> <sid> <hidden|visible|other|control> <other-sid> <granted|default|denied> [shot-dir]
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const [base, sid, step, other, perm, shots] = process.argv.slice(2);
const port = 9300 + Math.floor(Math.random() * 600);
const prof = mkdtempSync(join(tmpdir(), "cdp-notify-"));
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
  const key = async (k, modifiers = 0) => {
    const code = { Enter: 13, Escape: 27 }[k];
    const text = k === "Enter" ? "\r" : undefined;
    await send("Input.dispatchKeyEvent", { type: "keyDown", key: k, code: k,
      windowsVirtualKeyCode: code, nativeVirtualKeyCode: code, modifiers, text });
    await send("Input.dispatchKeyEvent", { type: "keyUp", key: k, code: k,
      windowsVirtualKeyCode: code, nativeVirtualKeyCode: code, modifiers });
    await sleep(400);
  };
  const type = async (text) => {
    await js(`document.querySelector("#input").focus()`);
    await send("Input.insertText", { text });
    await sleep(100);
  };
  const shot = async (name) => {
    if (!shots) return;
    const s = await send("Page.captureScreenshot", { format: "png" });
    writeFileSync(join(shots, name), Buffer.from(s.result.data, "base64"));
  };
  const load = async () => {
    await send("Page.navigate", { url: `${base}/` });
    for (let i = 0; i < 50; i++) {
      await sleep(200);
      const ready = await js(`typeof state !== "undefined" && state.sessionId === ${JSON.stringify(sid)} && !!document.querySelector("#mode-chip") && document.querySelector("#mode-chip").textContent !== ""`).catch(() => false);
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


  const look = () => js(`(() => ({
    title: document.title,
    icon: (document.querySelector("link[rel='icon']") || {}).href || "",
    live: document.querySelector("#run-live").textContent,
    liveAttr: document.querySelector("#run-live").getAttribute("aria-live"),
    notes: window.__notes.slice(),
    requests: window.__requests,
    dots: Object.fromEntries([...document.querySelectorAll(".session")].map((r) => [r.dataset.sid, (r.querySelector(".run-dot") || {dataset: {}}).dataset.state || null])),
    control: document.querySelector("#notify-toggle").textContent,
  }))()`);
  const until = async (cond, ms = 8000) => {
    for (let t = 0; t < ms; t += 100) { if (await js(cond)) return true; await sleep(100); }
    return false;
  };
  const setVis = async (v) => { await js(`window.__vis = ${JSON.stringify(v)}; document.dispatchEvent(new Event("visibilitychange"))`); await sleep(150); };
  const answer = (s) => js(`fetch("/api/tasks/" + window.__lastRun["${s}"] + "/answer", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({text: "yes"})}).then((r) => r.status)`);
  const start = (s, text) => js(`fetch("/api/sessions/${s}/task", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({text: ${JSON.stringify(text)}})}).then((r) => r.json()).then((j) => { (window.__lastRun = window.__lastRun || {})["${s}"] = j.run_id; return j.run_id; })`);

  // Stubs installed before any page script: a controllable visibilityState
  // and a Notification that records instead of showing.
  await send("Page.addScriptToEvaluateOnNewDocument", { source: `
    window.__vis = "visible"; window.__notes = []; window.__requests = 0;
    // "control" starts undecided and the prompt answers with <perm>.
    window.__perm = ${JSON.stringify(step === "control" ? "default" : perm)};
    Object.defineProperty(Document.prototype, "visibilityState", { get() { return window.__vis; }, configurable: true });
    window.Notification = class { constructor(t, o) { window.__notes.push({ title: t, body: (o || {}).body || "" }); }
      close() {} static get permission() { return window.__perm; }
      static requestPermission() { window.__requests += 1; window.__perm = ${JSON.stringify(perm)}; return Promise.resolve(window.__perm); } };
    try { localStorage.setItem("saddle.session", ${JSON.stringify(sid)}); } catch {}
    try { localStorage.setItem("saddle.notify", ${JSON.stringify(step === "control" ? "off" : "on")}); } catch {}
  ` });
  await send("Emulation.setDeviceMetricsOverride", { width: 1200, height: 800, deviceScaleFactor: 2, mobile: false });
  await send("Page.enable");
  await send("Runtime.enable");
  await load();
  const out = {};
  out.idle = await look();
  if (step === "hidden" || step === "visible") {
    // The active session's tab must follow its own events, not the poll.
    await js(`pollSessions = () => {}`);
    if (step === "hidden") await setVis("hidden");
    await start(sid, "fix the parser");
    await until(`document.title.startsWith("?")`);
    out.needs = await look();
    await answer(sid);
    await until(`document.title.startsWith("✓")`);
    out.finished = await look();
    await setVis("visible");
    out.back = await look();
  } else if (step === "other") {
    await setVis("hidden");
    await start(other, "ask about the schema");
    await until(`(document.querySelector('.session[data-sid="${other}"] .run-dot') || {dataset: {}}).dataset.state === "needs_you"`);
    out.otherNeeds = await look();
    await setVis("visible");
    await start(sid, "tidy the imports");
    await until(`document.title.startsWith("?")`);
    await answer(sid);
    await until(`document.title.startsWith("✓")`);
    out.mixed = await look();
    for (const scheme of ["dark", "light"]) {
      await send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: scheme }] });
      await sleep(200);
      await shot(`sidebar-two-states-${scheme}.png`);
    }
  } else if (step === "focus") {
    // Keyboard focus on each control the review found with outline: none.
    out.rings = {};
    await send("Emulation.setFocusEmulationEnabled", { enabled: true });
    for (const sel of ["#title", "#input", "#temp", "#notify-toggle"]) {
      out.rings[sel] = await js(`(() => { const n = document.querySelector(${JSON.stringify(sel)}); if (!n) return null;
        n.focus({ focusVisible: true }); const c = getComputedStyle(n);
        return { style: c.outlineStyle, width: parseFloat(c.outlineWidth) }; })()`);
    }
  } else if (step === "control") {
    await js(`document.querySelector("#notify-toggle").click()`);
    await sleep(200);
    out.afterClick = await look();
    out.pref = await js(`localStorage.getItem("saddle.notify")`);
    await js(`document.querySelector("#notify-toggle").click()`);
    await sleep(200);
    out.afterSecond = await look();
  }
  console.log(JSON.stringify(out));
  await finish(0);
} catch (error) {
  console.error(String(error && error.stack || error));
  await finish(1);
}
