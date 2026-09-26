// Starts a task from the page (Task mode, tests read-only, a token budget in
// k), waits for the card's question, picks <option> on the card and presses
// "Answer and continue", then waits for the packet and prints one JSON line
// of what it saw. Used by tests/test_ask_web.py; node >= 22 and Chrome only.
//
// usage: node ask_cdp.mjs <base-url> <session-id> <option> <tokens-k> [shot-dir] [shot-prefix]
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const [base, sid, option, tokensK, shots, prefix = "ask"] = process.argv.slice(2);
const port = 9300 + Math.floor(Math.random() * 600);
const prof = mkdtempSync(join(tmpdir(), "cdp-ask-"));
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
  const until = async (expression, what, tries = 300) => {
    for (let i = 0; i < tries; i++) {
      if (await js(expression).catch(() => false)) return;
      await sleep(200);
    }
    throw new Error(`timed out waiting for ${what}`);
  };
  await send("Emulation.setDeviceMetricsOverride", { width: 1200, height: 900, deviceScaleFactor: 2, mobile: false });
  await send("Page.enable");
  await send("Runtime.enable");
  await send("Page.navigate", { url: `${base}/` });
  await until(`typeof state !== "undefined" && state.sessionId === ${JSON.stringify(sid)} && document.querySelector("#mode-chip").textContent === "task"`, "the page");
  await js(`document.querySelector("#input").focus()`);
  await send("Input.insertText", { text: "f(None) should be 0" });
  await key("Enter");
  await js(`(() => { const box = document.querySelector("#tc-test-edits"); if (box.checked) box.click();
    document.querySelector("#tc-tokens").value = ${JSON.stringify(tokensK)}; })()`);
  await key("Enter");
  await until(`!!document.querySelector(".task-ask:not([hidden]) .ask-text")`, "the question");
  const out = {};
  out.question = await js(`document.querySelector(".task-ask .ask-text").textContent`);
  out.options = await js(`[...document.querySelectorAll(".task-ask .ask-option")].map((b) => b.textContent)`);
  out.pill = await js(`document.querySelector(".task-pill").textContent`);
  await js(`document.querySelector(".task-ask").scrollIntoView({ block: "center" })`);
  await sleep(300);
  await shot(`${prefix}-1-question.png`);
  const picked = await js(`(() => { const b = [...document.querySelectorAll(".task-ask .ask-option")].find((b) => b.textContent === ${JSON.stringify(option)}); if (!b) return false; b.click(); return true; })()`);
  if (!picked) throw new Error(`no option ${option}`);
  out.typed = await js(`document.querySelector(".task-ask .ask-input").value`);
  await js(`document.querySelector(".task-ask .ask-send").click()`);
  await until(`!!document.querySelector(".packet .verdict")`, "the packet", 600);
  await sleep(500);
  out.verdict = await js(`[...document.querySelector(".packet .verdict").classList].find((c) => c.startsWith("v-"))`);
  out.contract = await js(`(() => { const row = document.querySelector(".prow.k-contract");
    return row ? { status: [...row.classList].find((c) => c.startsWith("s-")),
      items: [...row.querySelectorAll(".prow-items li")].map((li) => li.textContent),
      cites: row.querySelectorAll(".cites .cite").length } : null; })()`);
  out.meter = await js(`document.querySelectorAll(".task-card .tmeter-value")[1].textContent`);
  out.askHidden = await js(`document.querySelector(".task-ask").hidden`);
  await js(`document.querySelector(".prow.k-contract").scrollIntoView({ block: "center" })`);
  await sleep(300);
  await shot(`${prefix}-2-answered-packet.png`);
  console.log(JSON.stringify(out));
  await finish(0);
} catch (error) {
  console.error(String(error && error.stack || error));
  await finish(1);
}
