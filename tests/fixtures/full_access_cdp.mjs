// Drives the chat page's full-access control (#124) in headless Chrome over
// the DevTools Protocol, with real key events and clicks, and prints one JSON
// line of what it saw. Used by tests/test_full_access_ui.py; no dependencies
// beyond node >= 22 and Chrome.
//
// usage: node full_access_cdp.mjs <base-url> <session-id> [shot-dir]
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const [base, sid, shots] = process.argv.slice(2);
const port = 9300 + Math.floor(Math.random() * 600);
const prof = mkdtempSync(join(tmpdir(), "cdp-fa-"));
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
  const key = async (/** @type {string} */ k) => {
    const code = /** @type {Record<string, number>} */ ({ Enter: 13, Escape: 27 })[k];
    const text = k === "Enter" ? "\r" : undefined;
    await send("Input.dispatchKeyEvent", {
      type: "keyDown",
      key: k,
      code: k,
      windowsVirtualKeyCode: code,
      nativeVirtualKeyCode: code,
      text,
    });
    await send("Input.dispatchKeyEvent", {
      type: "keyUp",
      key: k,
      code: k,
      windowsVirtualKeyCode: code,
      nativeVirtualKeyCode: code,
    });
    await sleep(400);
  };
  // A real mouse click at the element's centre, as a person would make it.
  const click = async (/** @type {string} */ selector) => {
    const box =
      await js(`(() => { const r = document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect();
      return { x: r.left + r.width / 2, y: r.top + r.height / 2 }; })()`);
    for (const type of ["mousePressed", "mouseReleased"]) {
      await send("Input.dispatchMouseEvent", { type, x: box.x, y: box.y, button: "left", clickCount: 1 });
    }
    await sleep(500);
  };
  const shot = async (/** @type {string} */ name) => {
    if (!shots) return;
    const s = await send("Page.captureScreenshot", { format: "png" });
    writeFileSync(join(shots, name), Buffer.from(s.result.data, "base64"));
  };
  const look = () =>
    js(`(() => ({
    dialogOpen: document.querySelector("#full-access-dialog").open,
    focus: document.activeElement && document.activeElement.id,
    banner: !document.querySelector("#full-access-banner").hidden,
    openButton: !document.querySelector("#full-access-open").hidden,
    badges: [...document.querySelectorAll("details.tool")].map((d) => !!d.querySelector(".unsandboxed-badge")),
    overflow: document.documentElement.scrollWidth > window.innerWidth,
    posts: window.__fullAccessPosts || 0,
  }))()`);
  const load = async () => {
    await send("Page.navigate", { url: `${base}/` });
    for (let i = 0; i < 50; i++) {
      await sleep(200);
      const ready = await js(
        `typeof state !== "undefined" && state.sessionId === ${JSON.stringify(sid)} && document.querySelector("#mode-chip").textContent !== ""`,
      ).catch(() => false);
      if (ready) break;
    }
    await sleep(400);
    // Count what the page asks the server to change; the server is checked too.
    await js(`(() => {
      const real = window.fetch;
      window.fetch = (url, opts) => {
        if (String(url).endsWith("/full-access") && opts && opts.method === "POST") {
          window.__fullAccessPosts = (window.__fullAccessPosts || 0) + 1;
        }
        return real(url, opts);
      };
    })()`);
  };

  await send("Emulation.setDeviceMetricsOverride", { width: 1200, height: 800, deviceScaleFactor: 2, mobile: false });
  await send("Page.enable");
  await send("Runtime.enable");
  await load();
  /** @type {Record<string, any>} */
  const out = { before: await look() };
  await click("#full-access-open");
  out.opened = await look();
  await shot("1-dialog.png");
  await key("Enter"); // the default answer
  out.afterEnter = await look();
  await click("#full-access-open");
  await key("Escape"); // closing it is No too
  out.afterEscape = await look();
  await click("#full-access-open");
  await click("#fa-grant"); // the only yes
  out.afterGrant = await look();
  await load();
  out.afterReload = await look();
  await shot("2-on.png");
  await click("#full-access-off");
  out.afterOff = await look();
  await click("#full-access-open");
  await click("#fa-grant");
  out.afterRegrant = await look();
  const lane = async (/** @type {string} */ name) => {
    // the lane menu, clicked as a person would
    await click("#lane-chip");
    await click(`#lane-menu li[data-lane="${name}"]`);
  };
  await lane("ask"); // leaving Edit ends it
  out.afterAsk = await look();
  await shot("3-left-edit.png");
  await lane("edit"); // and coming back does not restore it
  out.afterBack = await look();
  console.log(JSON.stringify(out));
  await finish(0);
} catch (error) {
  console.error(String(/** @type {any} */ (error)?.stack || error));
  await finish(1);
}
