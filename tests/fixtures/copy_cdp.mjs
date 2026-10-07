// Drives the chat page's code-block copy button in headless Chrome over CDP,
// with real mouse clicks, and prints one JSON line of what it saw. Used by
// tests/test_copy_button_ui.py; node >= 22 and Chrome only. No model: the
// session is seeded by the test.
//
// Chrome is told to resolve saddle-ui.test to 127.0.0.1, so the same server
// can be opened as a secure context (http://127.0.0.1:<port>, where the async
// clipboard exists) or as a plain-http non-loopback origin
// (http://saddle-ui.test:<port>, where it does not and the page must fall
// back to selecting a field and calling execCommand).
//
// usage: node copy_cdp.mjs <base-url> <session-id>
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { coverage } from "./cdp_coverage.mjs";
import { activePort, chromeEnv } from "./cdp_port.mjs";

const [base, sid] = process.argv.slice(2);
const prof = mkdtempSync(join(tmpdir(), "cdp-copy-"));
const chrome = spawn(
  "google-chrome",
  [
    "--headless=new",
    "--no-sandbox",
    "--disable-gpu",
    "--hide-scrollbars",
    "--host-resolver-rules=MAP saddle-ui.test 127.0.0.1",
    "--remote-debugging-port=0",
    `--user-data-dir=${prof}`,
    "about:blank",
  ],
  { stdio: "ignore", env: chromeEnv() },
);
/** @type {string[]} */
const pageErrors = [];
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
      /* chrome is still starting */
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
    if (msg.method === "Runtime.exceptionThrown") {
      const thrown = msg.params.exceptionDetails;
      pageErrors.push(thrown.exception?.description || thrown.text);
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
  const until = async (/** @type {string} */ cond, ms = 8000) => {
    for (let t = 0; t < ms; t += 100) {
      if (await js(cond).catch(() => false)) return true;
      await sleep(100);
    }
    throw new Error(`timed out waiting for ${cond}`);
  };
  // A real click: scroll the node to the middle, then press and release the
  // mouse over its centre, so the page sees a user gesture.
  const click = async (/** @type {string} */ sel) => {
    const at = await js(`(() => {
      const n = document.querySelector(${JSON.stringify(sel)});
      n.scrollIntoView({ block: "center" });
      const r = n.getBoundingClientRect();
      return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
    })()`);
    for (const type of ["mouseMoved", "mousePressed", "mouseReleased"]) {
      await send("Input.dispatchMouseEvent", { type, x: at.x, y: at.y, button: "left", clickCount: 1 });
    }
  };

  // The page must count as focused for the clipboard to be read back.
  await send("Emulation.setFocusEmulationEnabled", { enabled: true });
  const origin = new URL(base).origin;
  const granted = await send("Browser.grantPermissions", {
    origin,
    permissions: ["clipboardReadWrite", "clipboardSanitizedWrite"],
  });
  await send("Page.addScriptToEvaluateOnNewDocument", {
    source: `
    try { localStorage.setItem("saddle.session", ${JSON.stringify(sid)}); } catch { /* none */ }
  `,
  });
  await send("Emulation.setDeviceMetricsOverride", { width: 1200, height: 900, deviceScaleFactor: 1, mobile: false });
  await send("Page.enable");
  await send("Runtime.enable");
  await send("Page.navigate", { url: `${base}/` });
  await until(`!!document.querySelector(".assistant pre .code-copy") && !!document.querySelector("details.tool")`);

  const env = await js(`({
    origin: location.origin,
    secure: window.isSecureContext,
    hasClipboard: typeof navigator.clipboard !== "undefined",
  })`);

  // What the page asks the browser to copy, when it has to go through
  // execCommand: the focused field and its selection at the moment of the call.
  await js(`(() => {
    window.copies = [];
    window.copyEvents = 0;
    const real = document.execCommand.bind(document);
    document.execCommand = (name, ...rest) => {
      const a = document.activeElement;
      const field = a && a.tagName === "TEXTAREA" ? a : null;
      const ok = real(name, ...rest);
      window.copies.push({
        name, ok,
        field: field ? "textarea" : (a ? a.tagName.toLowerCase() : null),
        selected: field ? field.value.slice(field.selectionStart, field.selectionEnd) : null,
      });
      return ok;
    };
    document.addEventListener("copy", () => { window.copyEvents += 1; }, true);
    return true;
  })()`);

  const label = (/** @type {string} */ sel) => js(`document.querySelector(${JSON.stringify(sel)}).textContent`);
  const exercise = async (/** @type {string} */ sel) => {
    const before = await label(sel);
    const copiesBefore = await js("window.copies.length");
    const fieldsBefore = await js(`document.querySelectorAll("textarea").length`);
    await click(sel);
    await until(`document.querySelector(${JSON.stringify(sel)}).textContent !== ${JSON.stringify(before)}`);
    const shown = await label(sel);
    const clipboard = env.hasClipboard
      ? await js("navigator.clipboard.readText()").catch((e) => `error: ${e.message}`)
      : null;
    const state = await js(`(() => {
      const button = document.querySelector(${JSON.stringify(sel)});
      return {
        newFields: document.querySelectorAll("textarea").length - ${fieldsBefore},
        focusOnButton: document.activeElement === button,
        copies: window.copies.slice(${copiesBefore}),
      };
    })()`);
    await until(`document.querySelector(${JSON.stringify(sel)}).textContent === ${JSON.stringify(before)}`, 4000);
    return { before, shown, after: await label(sel), clipboard, ...state };
  };

  /** @type {Record<string, any>} */

  const out = { env, granted: !granted.error };
  out.block = await exercise(".assistant pre .code-copy");
  out.blockTitle = await js(`document.querySelector(".assistant pre .code-copy").title`);
  out.rowOpenBefore = await js(`document.querySelector("details.tool").open`);
  out.row = await exercise("details.tool > summary > .code-copy");
  out.rowOpenAfter = await js(`document.querySelector("details.tool").open`);
  out.rowShape = await js(`(() => {
    const row = document.querySelector("details.tool");
    return {
      inSummary: row.querySelectorAll("summary > .code-copy").length,
      inDetail: row.querySelectorAll(".tool-detail .code-copy").length,
      detailText: row.querySelector(".tool-detail").textContent,
    };
  })()`);
  out.copyEvents = await js("window.copyEvents");
  console.log(JSON.stringify(out));
  await cov.save();
  await finish(0);
} catch (error) {
  console.error(String(/** @type {any} */ (error)?.stack || error));
  if (pageErrors.length) console.error(`page errors:\n${pageErrors.join("\n")}`);
  await finish(1);
}
