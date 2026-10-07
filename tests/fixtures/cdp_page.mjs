// One headless Chrome page for a test to drive over the DevTools Protocol.
//
// withPage(base, body, options) starts Chrome on a DevTools port of its own
// (cdp_port.mjs), records page-script coverage when SADDLE_JS_COVERAGE_DIR is
// set (cdp_coverage.mjs), runs body(page), and always closes Chrome. The page:
//   page.goto(path)            open base + path and wait until it has loaded
//   page.chat(sid)             open the chat page and wait until it has rendered session sid
//   page.js(fn, ...values)     run fn(...values) in the page; its awaited result, as JSON
//   page.until(fn, ...values)  run it until it is truthy; after page.timeout ms, throw naming it
//   page.click(selector)       find the element and click it in one step, retried until it exists
//   page.type(selector, text)  focus the element and type text into it
//   page.key(name)             press Enter, Escape, Tab, ArrowUp or ArrowDown
//   page.width(px)             resize the viewport (900 px high; a phone below 600 px)
//   page.shot(name)            a PNG in options.shots, when it names a directory
//   page.send(method, params)  any other DevTools Protocol command
// fn may also be a string of page JavaScript. Values go into the page as JSON.
/* global document, location -- read by the functions page.js and page.until send into the page */
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { coverage } from "./cdp_coverage.mjs";
import { activePort, chromeEnv } from "./cdp_port.mjs";

/** @typedef {((...values: any[]) => unknown) | string} PageCode */
/**
 * @typedef {object} Page
 * @property {number} timeout
 * @property {(path: string) => Promise<void>} goto
 * @property {(sid: string) => Promise<void>} chat
 * @property {(fn: PageCode, ...values: unknown[]) => Promise<any>} js
 * @property {(fn: PageCode, ...values: unknown[]) => Promise<void>} until
 * @property {(selector: string) => Promise<void>} click
 * @property {(selector: string, text: string) => Promise<void>} type
 * @property {(name: string) => Promise<void>} key
 * @property {(px: number) => Promise<void>} width
 * @property {(name: string) => Promise<void>} shot
 * @property {(method: string, params?: object) => Promise<any>} send
 */
/** @typedef {{ width?: number, scheme?: "light" | "dark", shots?: string }} PageOptions */

const REPLY_MS = 30_000;
const KEYS = /** @type {Record<string, number>} */ ({ Enter: 13, Escape: 27, Tab: 9, ArrowUp: 38, ArrowDown: 40 });
const sleep = (/** @type {number} */ ms) => new Promise((r) => setTimeout(r, ms));

/**
 * The page JavaScript that runs `fn` on `values`.
 * @param {PageCode} fn
 * @param {unknown[]} values
 */
export function expression(fn, values) {
  if (typeof fn !== "string") return `(${fn})(...${JSON.stringify(values)})`;
  if (values.length) throw new Error("values go to a function, not to a string of page JavaScript");
  return fn;
}

/**
 * The WebSocket URL of the page in the Chrome whose profile is `prof`.
 * @param {string} prof
 */
async function pageSocket(prof) {
  for (let i = 0; i < 150; i++) {
    const port = activePort(prof);
    if (port) {
      try {
        const list = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
        const page = list.find((/** @type {{ type: string }} */ t) => t.type === "page");
        if (page) return /** @type {string} */ (page.webSocketDebuggerUrl);
      } catch {
        // DevTools is not answering yet; poll again.
      }
    }
    await sleep(100);
  }
  throw new Error("Chrome did not start: no DevTools page after 15 s");
}

/**
 * Run `body` on a fresh page of a headless Chrome, and return what it returns.
 * @template T
 * @param {string} base
 * @param {(page: Page) => Promise<T>} body
 * @param {PageOptions} [options]
 * @returns {Promise<T>}
 */
export async function withPage(base, body, { width = 1280, scheme = "light", shots = "" } = {}) {
  const prof = mkdtempSync(join(tmpdir(), "cdp-page-"));
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
  /** @type {WebSocket | undefined} */
  let ws;
  try {
    const socket = new WebSocket(await pageSocket(prof));
    ws = socket;
    await new Promise((resolve, reject) => {
      socket.addEventListener("open", resolve);
      socket.addEventListener("error", () => reject(new Error("could not connect to Chrome's page")));
    });
    let id = 0;
    /** @type {Map<number, { resolve: (msg: any) => void, reject: (error: Error) => void }>} */
    const waiting = new Map();
    socket.addEventListener("message", (m) => {
      const msg = JSON.parse(String(m.data));
      const call = msg.id && waiting.get(msg.id);
      if (call) {
        waiting.delete(msg.id);
        call.resolve(msg);
      }
    });
    socket.addEventListener("close", () => {
      for (const call of waiting.values()) call.reject(new Error("Chrome closed the page connection"));
      waiting.clear();
    });
    /** @type {(method: string, params?: object) => Promise<any>} */
    const rawSend = (method, params = {}) =>
      new Promise((resolve, reject) => {
        const n = ++id;
        const timer = setTimeout(() => {
          waiting.delete(n);
          reject(new Error(`Chrome sent no reply to ${method} in ${REPLY_MS / 1000} s`));
        }, REPLY_MS);
        waiting.set(n, {
          resolve: (msg) => {
            clearTimeout(timer);
            resolve(msg);
          },
          reject: (error) => {
            clearTimeout(timer);
            reject(error);
          },
        });
        socket.send(JSON.stringify({ id: n, method, params }));
      });
    const cov = coverage(rawSend);
    /** @type {Page} */
    const page = {
      timeout: 10_000,
      send: cov.send,
      async js(fn, ...values) {
        const r = await cov.send("Runtime.evaluate", {
          expression: expression(fn, values),
          awaitPromise: true,
          returnByValue: true,
        });
        const thrown = r.result?.exceptionDetails;
        if (thrown) throw new Error(`the page threw: ${thrown.exception?.description || thrown.text}`);
        if (r.error) throw new Error(`Runtime.evaluate failed: ${r.error.message}`);
        return r.result?.result?.value;
      },
      async until(fn, ...values) {
        const code = expression(fn, values);
        const deadline = Date.now() + page.timeout;
        /** @type {unknown} */
        let last;
        for (;;) {
          try {
            if (await page.js(code)) return;
            last = undefined;
          } catch (error) {
            last = error;
          }
          if (Date.now() > deadline) {
            const why = last instanceof Error ? ` (last error: ${last.message})` : "";
            throw new Error(`timed out after ${page.timeout} ms waiting for: ${String(fn)}${why}`);
          }
          await sleep(100);
        }
      },
      async goto(path) {
        const url = new URL(path, base).href;
        await cov.send("Page.navigate", { url });
        await page.until((/** @type {string} */ u) => location.href === u && document.readyState === "complete", url);
      },
      async chat(sid) {
        await page.goto("/");
        // `state` is a top-level const of the page's scripts, not a property of window.
        // The session is selected before its session.info arrives, and the first one
        // rebuilds the transcript (renderHistory): a test that starts sooner can see
        // its own turns wiped. renderHistory marks that rebuild in state.historyFor.
        const session = JSON.stringify(sid);
        await page.until(
          `typeof state !== "undefined" && state.sessionId === ${session} && state.historyFor === ${session}`,
        );
      },
      async click(selector) {
        await page.until((/** @type {string} */ s) => {
          const element = /** @type {HTMLElement | null} */ (document.querySelector(s));
          element?.click();
          return !!element;
        }, selector);
      },
      async type(selector, text) {
        await page.until((/** @type {string} */ s) => {
          const element = /** @type {HTMLElement | null} */ (document.querySelector(s));
          element?.focus();
          return !!element && document.activeElement === element;
        }, selector);
        await cov.send("Input.insertText", { text });
      },
      async key(name) {
        const code = KEYS[name];
        if (!code) throw new Error(`no key named ${name}; known: ${Object.keys(KEYS).join(", ")}`);
        const stroke = { key: name, code: name, windowsVirtualKeyCode: code, nativeVirtualKeyCode: code };
        await cov.send("Input.dispatchKeyEvent", {
          type: "keyDown",
          ...stroke,
          text: name === "Enter" ? "\r" : undefined,
        });
        await cov.send("Input.dispatchKeyEvent", { type: "keyUp", ...stroke });
      },
      async width(px) {
        await cov.send("Emulation.setDeviceMetricsOverride", {
          width: px,
          height: 900,
          deviceScaleFactor: 1,
          mobile: px < 600,
        });
      },
      async shot(name) {
        if (!shots) return;
        const s = await cov.send("Page.captureScreenshot", { format: "png" });
        writeFileSync(join(shots, name), Buffer.from(s.result.data, "base64"));
      },
    };
    await cov.send("Page.enable");
    await cov.send("Runtime.enable");
    await page.width(width);
    await cov.send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: scheme }] });
    const result = await body(page);
    await cov.save();
    return result;
  } finally {
    ws?.close();
    chrome.kill();
    await sleep(300); // Chrome lets go of its profile before it is removed
    rmSync(prof, { recursive: true, force: true });
  }
}
