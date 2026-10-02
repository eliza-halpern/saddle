// Drives the chat page in headless Chrome over the DevTools Protocol, with
// real key events, and prints one JSON line of what it saw. Used by
// tests/test_lanechip.py; no dependencies beyond node >= 22 and Chrome.
//
// usage: node lanechip_cdp.mjs <base-url> <session-id> <step> [shot-dir] [light|dark] [prefix]
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { coverage } from "./cdp_coverage.mjs";

const [base, sid, step, shots, scheme, prefix = ""] = process.argv.slice(2);
const port = 9300 + Math.floor(Math.random() * 600);
const prof = mkdtempSync(join(tmpdir(), "cdp-lane-"));
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
  const key = async (/** @type {string} */ k, modifiers = 0) => {
    const code = /** @type {Record<string, number>} */ ({ Enter: 13, Escape: 27, Tab: 9, ArrowDown: 40 })[k];
    const text = k === "Enter" ? "\r" : undefined;
    await send("Input.dispatchKeyEvent", {
      type: "keyDown",
      key: k,
      code: k,
      windowsVirtualKeyCode: code,
      nativeVirtualKeyCode: code,
      modifiers,
      text,
    });
    await send("Input.dispatchKeyEvent", {
      type: "keyUp",
      key: k,
      code: k,
      windowsVirtualKeyCode: code,
      nativeVirtualKeyCode: code,
      modifiers,
    });
    await sleep(400);
  };
  const type = async (/** @type {string} */ text) => {
    await js(`document.querySelector("#input").focus()`);
    await send("Input.insertText", { text });
    await sleep(100);
  };
  const shot = async (/** @type {string} */ name) => {
    if (!shots) return;
    const s = await send("Page.captureScreenshot", { format: "png" });
    writeFileSync(join(shots, prefix + name), Buffer.from(s.result.data, "base64"));
  };
  const look = () =>
    js(`(() => ({
    stripVisible: !document.querySelector("#task-confirm").hidden,
    focus: document.activeElement && document.activeElement.id,
    desc: document.querySelector("#mode-desc").textContent.trim(),
    lane: state.mode,
    chipText: document.querySelector("#lane-name").textContent.trim(),
    placeholder: document.querySelector("#input").placeholder,
    suggest: document.querySelector("#lane-suggest").hidden ? "" : document.querySelector("#lane-suggest").textContent,
    menuOpen: !document.querySelector("#lane-menu").hidden,
    where: document.querySelector("#where").textContent.trim(),
    patches: window.__patches || [],
    chip: document.querySelector("#mode-chip").textContent.trim(),
    note: document.querySelector("#mode-note").hidden ? "" : document.querySelector("#mode-note").textContent,
    taskPosts: window.__taskPosts || 0,
    chatPosts: window.__chatPosts || 0,
    overflow: document.documentElement.scrollWidth > window.innerWidth,
  }))()`);
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
        if (opts && opts.method === "PATCH" && opts.body && JSON.parse(opts.body).mode) (window.__patches = window.__patches || []).push(JSON.parse(opts.body).mode);
        return real(url, opts);
      };
    })()`);
  };

  const [w, h, mobile] = step === "phone" ? [390, 844, true] : [1200, 800, false];
  await send("Emulation.setDeviceMetricsOverride", { width: w, height: h, deviceScaleFactor: 2, mobile });
  if (scheme) await send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: scheme }] });
  await send("Page.enable");
  await send("Runtime.enable");
  await load();
  /** @type {Record<string, any>} */
  const out = {};
  const options = () =>
    js(`[...document.querySelectorAll("#lane-menu li")].map((li) => ({
    lane: li.dataset.lane, label: li.querySelector("b").textContent,
    desc: li.querySelector("span").textContent, disabled: li.getAttribute("aria-disabled") === "true" }))`);
  if (step === "chip") {
    out.before = await look();
    await shot("chip-ask.png");
    await js(`document.querySelector("#lane-chip").click()`);
    await sleep(200);
    out.open = await look();
    out.options = await options();
    await shot("chip-menu-open.png");
    // A greyed lane is not chosen by a click on it.
    for (const lane of ["feature", "breadth", "long"]) {
      await js(`document.querySelector('#lane-menu li[data-lane="${lane}"]').click()`);
      await sleep(150);
    }
    out.afterDisabledClicks = await look();
    await key("Escape");
    // Shift+Tab from the input walks the enabled lanes and wraps.
    await js(`document.querySelector("#input").focus()`);
    out.cycle = [];
    for (let i = 0; i < 4; i++) {
      await key("Tab", 8);
      const seen = await look();
      out.cycle.push(seen.lane);
      if (i === 0) await shot("chip-edit.png");
      if (i === 1) await shot("chip-task.png");
    }
    await sleep(300);
    out.after = await look();
  } else if (step === "menu-pick") {
    await js(`document.querySelector("#lane-chip").click()`);
    await sleep(150);
    await key("ArrowDown");
    await key("Enter");
    out.afterEnter = await look();
    await load();
    out.afterReload = await look();
  } else if (step === "heuristic") {
    out.before = await look();
    await type("fix the rounding in calc.py");
    await sleep(600);
    out.taskish = await look();
    await shot("suggest-task.png");
    await js(`document.querySelector("#input").value = ""`);
    await type("what does add return?");
    await sleep(600);
    out.question = await look();
    await js(`document.querySelector("#input").value = ""`);
    await type("fix the rounding in calc.py");
    await key("Tab");
    out.afterTab = await look();
  } else if (step === "ask-edit") {
    out.before = await look();
    await type("why does add subtract?");
    await key("Enter");
    for (let i = 0; i < 40; i++) {
      await sleep(250);
      if (await js(`!state.busy && document.querySelectorAll("details.tool:not(.running)").length > 0`)) break;
    }
    await sleep(400);
    await js(
      `document.querySelectorAll(".tool, .tool summary, details").forEach((d) => { if (d.tagName === "DETAILS") d.open = true; })`,
    );
    await sleep(200);
    await shot("ask-refused-edit.png");
    out.after = await look();
    out.tool = await js(`(() => { const t = document.querySelector("details.tool");
      return t ? { failed: t.classList.contains("failed"), open: t.open, text: t.textContent } : null; })()`);
  } else if (step === "task-strip") {
    await type("make add add in calc.py");
    await key("Enter");
    out.strip = await look();
    await shot("task-strip.png");
    await key("Escape");
    out.afterEscape = await look();
  }
  console.log(JSON.stringify(out));
  await cov.save();
  await finish(0);
} catch (error) {
  console.error(String(/** @type {any} */ (error)?.stack || error));
  await finish(1);
}
