// Line coverage of the browser scripts, collected by the Chrome-driven tests.
//
// Every *_cdp.mjs driver wraps its CDP `send` in `coverage(send).send`. When
// SADDLE_JS_COVERAGE_DIR names a directory, the wrapper turns on V8's precise
// coverage (call counts, per block) with the page, drains it before each
// navigation (a navigation drops the old page's scripts) and once more when the
// driver calls `save`, and writes one V8-format file per drain into that
// directory. The scripts the page loaded from /static/ are renamed to the
// file:// URL of the file in this checkout, which is what c8 reads back and
// merges with the other drivers' files (`c8 report --temp-directory <dir>`).
// With the variable unset, the wrapper passes every call through untouched.
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const STATIC = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "src", "saddle", "web", "static");

/**
 * The checked-out file a page URL under /static/ was served from, as a file:// URL; "" for any other script.
 * @param {string} pageUrl
 */
export function sourceUrl(pageUrl) {
  let path;
  try {
    path = new URL(pageUrl).pathname;
  } catch {
    return "";
  }
  if (!path.startsWith("/static/") || !path.endsWith(".js")) return "";
  const name = path.slice("/static/".length);
  if (name.includes("/") || name.includes("..")) return "";
  return pathToFileURL(join(STATIC, name)).href;
}

/**
 * V8 coverage entries for the served scripts only, each renamed to its source file.
 * @param {Array<{ url: string }>} result
 */
export function sourceEntries(result) {
  const out = [];
  for (const entry of result) {
    const url = sourceUrl(entry.url);
    if (url) out.push({ ...entry, url });
  }
  return out;
}

/** @typedef {(method: string, params?: object) => Promise<any>} Send */

/**
 * @param {Send} rawSend
 * @param {string | undefined} dir
 * @returns {{ send: Send, save: () => Promise<void> }}
 */
export function coverage(rawSend, dir = process.env.SADDLE_JS_COVERAGE_DIR) {
  let started = false;
  let drains = 0;
  const drain = async () => {
    if (!started) return;
    const taken = await rawSend("Profiler.takePreciseCoverage");
    const result = sourceEntries(taken.result?.result ?? []);
    if (!result.length) return;
    mkdirSync(dir, { recursive: true });
    writeFileSync(join(dir, `coverage-${process.pid}-${++drains}.json`), JSON.stringify({ result }));
  };
  return {
    send: !dir
      ? rawSend
      : async (/** @type {string} */ method, params = {}) => {
          if (method === "Page.navigate") await drain();
          const reply = await rawSend(method, params);
          if (method === "Page.enable" && !started) {
            await rawSend("Profiler.enable");
            await rawSend("Profiler.startPreciseCoverage", { callCount: true, detailed: true });
            started = true;
          }
          return reply;
        },
    save: drain,
  };
}
