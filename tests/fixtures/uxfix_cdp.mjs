// Fixes from the second UX review, driven in headless Chrome over CDP; prints one JSON line.
// Used by tests/test_uxfix.py; node >= 22 and Chrome only.
//
// usage: node uxfix_cdp.mjs <base-url> <step> <session-id> [other-session-id] [shot-dir]
//   switch: start a task in <session-id> (tokens 1k) and wait for its question,
//           then select <other-session-id>: report the header pill, the send
//           button and what submitting there does; then select back.
//   phone:  as switch, at 400 px: report the header pill on A (display, width)
//           and the ☰ button on B (class, ::after content) -- what says
//           "needs you" outside a closed drawer on a phone.
//   jump:   scroll <session-id>'s transcript to the top at 400 px and 1280 px
//           and report where the "↓ newest" pill sits against the composer.
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const [base, step, sid, other, shots] = process.argv.slice(2);
const port = 9300 + Math.floor(Math.random() * 600);
const prof = mkdtempSync(join(tmpdir(), "cdp-uxfix-"));
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
  const shot = async (/** @type {string} */ name) => {
    if (!shots) return;
    const s = await send("Page.captureScreenshot", { format: "png" });
    writeFileSync(join(shots, name), Buffer.from(s.result.data, "base64"));
  };
  const until = async (/** @type {string} */ expression, /** @type {string} */ what, tries = 300) => {
    for (let i = 0; i < tries; i++) {
      if (await js(expression).catch(() => false)) return;
      await sleep(200);
    }
    throw new Error(`timed out waiting for ${what}`);
  };
  const width = async (/** @type {number} */ w) => {
    await send("Emulation.setDeviceMetricsOverride", { width: w, height: 860, deviceScaleFactor: 1, mobile: w < 700 });
    await sleep(500);
  };
  const header = () =>
    js(`({ status: document.querySelector("#status").textContent,
    send: document.querySelector("#send").textContent, busy: state.busy, sid: state.sessionId })`);
  await width(1280);
  await send("Page.enable");
  await send("Runtime.enable");
  await send("Page.navigate", { url: `${base}/` });
  await until(`typeof state !== "undefined" && !!state.sessionId`, "the page");
  /** @type {Record<string, any>} */
  const out = {};
  if (step === "switch") {
    await js(`select(${JSON.stringify(sid)})`);
    await until(
      `state.sessionId === ${JSON.stringify(sid)} && document.querySelector("#mode-chip").textContent === "task"`,
      "session A",
    );
    // Budgets left the UI; start with an explicit token budget through the
    // page's own launcher so the 80%-budget question still fires as the vehicle
    // this test needs (a run with a pending question).
    await js(`launchTask("f(None) should be 0", 0, 1000, false)`);
    await until(`!!document.querySelector(".task-ask:not([hidden]) .ask-text")`, "the question");
    out.onA = await header();
    await js(`select(${JSON.stringify(other)})`);
    await until(`state.historyFor === ${JSON.stringify(other)} && !document.querySelector(".task-card")`, "session B");
    await sleep(1500);
    out.onB = await header();
    await shot("uxfix-switch-B.png");
    // Submitting in B must not reach A's run.
    await js(`document.querySelector("#composer").requestSubmit()`);
    await sleep(1500);
    out.afterSubmitOnB = await header();
    await js(`select(${JSON.stringify(sid)})`);
    // Soft: when the defect is present, submitting in B stopped A's run, so
    // there is no question to come back to; report what is there instead.
    await until(`!!document.querySelector(".task-ask:not([hidden]) .ask-text")`, "A's question again", 50).catch(
      () => {},
    );
    await sleep(500);
    out.backOnA = await header();
  } else if (step === "phone") {
    await width(400);
    await js(`select(${JSON.stringify(sid)})`);
    await until(
      `state.sessionId === ${JSON.stringify(sid)} && document.querySelector("#mode-chip").textContent === "task"`,
      "session A",
    );
    // Budgets left the UI; start with an explicit token budget through the
    // page's own launcher so the 80%-budget question still fires as the vehicle
    // this test needs (a run with a pending question).
    await js(`launchTask("f(None) should be 0", 0, 1000, false)`);
    await until(`!!document.querySelector(".task-ask:not([hidden]) .ask-text")`, "the question");
    const pill = () =>
      js(`(() => { const n = document.querySelector("#status"); const r = n.getBoundingClientRect();
      return { text: n.textContent, display: getComputedStyle(n).display, width: r.width,
               onScreen: r.right <= innerWidth && r.width > 0 }; })()`);
    const menu = () =>
      js(`(() => { const n = document.querySelector("#menu"); const a = getComputedStyle(n, "::after");
      return { needs: n.classList.contains("needs"), label: n.getAttribute("aria-label"),
               after: a.content, afterWidth: parseFloat(a.width) || 0, color: a.backgroundColor,
               drawerOpen: document.querySelector("#sidebar").classList.contains("open") }; })()`);
    out.onA = { pill: await pill(), menu: await menu() };
    await shot("phone-A-needs-you-400.png");
    await js(`select(${JSON.stringify(other)})`);
    await until(`state.historyFor === ${JSON.stringify(other)} && !document.querySelector(".task-card")`, "session B");
    await sleep(1500);
    out.onB = { pill: await pill(), menu: await menu() };
    await shot("phone-B-other-needs-you-400.png");
  } else if (step === "jump") {
    await js(`select(${JSON.stringify(sid)})`);
    await until(
      `state.sessionId === ${JSON.stringify(sid)} && document.querySelectorAll("#transcript .turn").length > 5`,
      "the transcript",
    );
    for (const w of [400, 1280]) {
      await width(w);
      await js(`followBottom()`);
      await sleep(300);
      await js(
        `(() => { const t = document.querySelector("#transcript"); t.scrollTop = 0; t.dispatchEvent(new Event("scroll")); })()`,
      );
      await sleep(400);
      out[w] = await js(`(() => { const j = document.querySelector("#jump").getBoundingClientRect();
        const c = document.querySelector("#composer").getBoundingClientRect();
        return { hidden: document.querySelector("#jump").hidden, jumpTop: j.top, jumpBottom: j.bottom,
                 jumpRight: j.right, composerTop: c.top, viewport: innerWidth }; })()`);
      await shot(`uxfix-jump-${w}.png`);
    }
  }
  console.log(JSON.stringify(out));
  await finish(0);
} catch (error) {
  console.error(String(/** @type {any} */ (error)?.stack || error));
  await finish(1);
}
