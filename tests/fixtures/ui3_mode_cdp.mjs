// Drives the chat page in headless Chrome over the DevTools Protocol, with
// real key events, and prints one JSON line of what it saw. Used by
// tests/test_ui3_mode.py; no dependencies beyond node >= 22 and Chrome.
//
// usage: node ui3_mode_cdp.mjs <base-url> <session-id> <task|chat|nofolder> [shot-dir]
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const [base, sid, step, shots] = process.argv.slice(2);
const port = 9300 + Math.floor(Math.random() * 600);
const prof = mkdtempSync(join(tmpdir(), "cdp-ui3-"));
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
  const send = (/** @type {string} */ method, params = {}) =>
    new Promise((resolve) => {
      const n = ++id;
      pending.set(n, resolve);
      ws.send(JSON.stringify({ id: n, method, params }));
    });
  const js = async (/** @type {string} */ expression) => {
    const r = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
    if (r.result?.exceptionDetails) throw new Error(JSON.stringify(r.result.exceptionDetails));
    return r.result?.result?.value;
  };
  const key = async (/** @type {string} */ k, modifiers = 0) => {
    const code = /** @type {Record<string, number>} */ ({ Enter: 13, Escape: 27 })[k];
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
    writeFileSync(join(shots, name), Buffer.from(s.result.data, "base64"));
  };
  const look = () =>
    js(`(() => ({
    stripVisible: !document.querySelector("#task-confirm").hidden,
    focus: document.activeElement && document.activeElement.id,
    desc: document.querySelector("#mode-desc").textContent.trim(),
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
        return real(url, opts);
      };
    })()`);
  };

  const [w, h, mobile] = step === "phone" ? [390, 844, true] : [1200, 800, false];
  await send("Emulation.setDeviceMetricsOverride", { width: w, height: h, deviceScaleFactor: 2, mobile });
  await send("Page.enable");
  await send("Runtime.enable");
  await load();
  /** @type {Record<string, any>} */
  const out = {};
  if (step === "task" || step === "phone") {
    out.before = await look();
    await type("make add add");
    await shot(step === "phone" ? "4-phone-task-mode.png" : "2-task-mode.png");
    await key("Enter");
    out.afterFirstEnter = await look();
    await shot(step === "phone" ? "5-phone-strip.png" : "3-strip-opened-by-enter.png");
    await key("Escape");
    out.afterEscape = await look();
    await key("Enter");
    await key("Enter");
    out.afterSecondEnter = await look();
  } else if (step === "chat") {
    out.before = await look();
    await type("hello there");
    await shot("1-chat-mode.png");
    await key("Enter");
    out.afterEnter = await look();
    await js(
      `document.querySelector("#lane-chip").click(); document.querySelector('#lane-menu li[data-lane="task"]').click()`,
    );
    await sleep(500);
    out.afterSwitch = await look();
    await load();
    out.afterReload = await look();
  } else if (step === "nofolder") {
    await js(`state.folder = null`);
    await type("make add add");
    await key("Enter");
    Object.assign(out, await look());
  }
  console.log(JSON.stringify(out));
  await finish(0);
} catch (error) {
  console.error(String(/** @type {any} */ (error)?.stack || error));
  await finish(1);
}
