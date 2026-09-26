// Drives the chat page in headless Chrome over CDP to read a finished run's
// packet card: the summary band, the folds, and the action row (View diff,
// Merge, Discard, Ask about this run). Used by tests/test_packet_ui.py; node
// >= 22 and Chrome only. No model: the run is seeded by the test.
//
// usage: node packet_cdp.mjs <base> <sid> <read|diff|merge|merge-cancel|discard|chat> [shot-dir] [shot-prefix] [width]
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const [base, sid, step, shots, prefix = "", width = "1200"] = process.argv.slice(2);
const port = 9300 + Math.floor(Math.random() * 600);
const prof = mkdtempSync(join(tmpdir(), "cdp-packet-"));
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
  const until = async (cond, ms = 8000) => {
    for (let t = 0; t < ms; t += 100) { if (await js(cond).catch(() => false)) return true; await sleep(100); }
    throw new Error(`timed out waiting for ${cond}`);
  };
  const shot = async (name) => {
    if (!shots) return;
    const s = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
    writeFileSync(join(shots, prefix + name), Buffer.from(s.result.data, "base64"));
  };
  const click = async (sel) => { await js(`document.querySelector(${JSON.stringify(sel)}).click()`); await sleep(300); };
  // Scroll the packet's top to the top of the transcript, as a reader lands.
  const toPacket = (sel = ".packet") => js(`(() => { const n = document.querySelector(${JSON.stringify(sel)}); n.scrollIntoView({block: "start"}); return true; })()`);

  await send("Page.addScriptToEvaluateOnNewDocument", { source: `
    try { localStorage.setItem("saddle.session", ${JSON.stringify(sid)}); } catch {}
  ` });
  const w = Number(width);
  await send("Emulation.setDeviceMetricsOverride", { width: w, height: w < 700 ? 820 : 900, deviceScaleFactor: 2, mobile: w < 700 });
  await send("Page.enable");
  await send("Runtime.enable");
  await send("Page.navigate", { url: `${base}/` });
  await until(`!!document.querySelector(".packet .band") && !!document.querySelector(".act-why") && (document.querySelector(".act-merge").textContent !== "Merge" || document.querySelector(".act-why").textContent !== "")`);
  await sleep(200);

  const read = () => js(`(() => {
    const p = document.querySelector(".packet");
    const band = p.querySelector(".band");
    const details = p.querySelector(".packet-details");
    const fold = p.querySelector(".audit-fold");
    const btn = (s) => { const b = p.querySelector(s); return { text: b.textContent, disabled: b.disabled }; };
    return {
      order: [...p.children].map((n) => n.className.split(" ")[0]),
      verdict: p.querySelector(".verdict-word").textContent,
      band: band.dataset.tone,
      lines: [...band.querySelectorAll(".band-line")].map((l) => ({
        key: l.dataset.key, tone: l.dataset.tone, glyph: l.querySelector(".band-glyph").textContent,
        text: l.querySelector(".band-text").textContent,
        items: [...l.querySelectorAll(".band-items li")].map((li) => li.textContent) })),
      firstRows: [...p.querySelectorAll(".rows > .prow, .rows > .audit-fold")].map((n) => n.className),
      audit: fold ? { summary: fold.querySelector(".audit-summary").textContent, open: fold.open } : null,
      details: details ? { summary: details.querySelector("summary").textContent, open: details.open,
        keys: [...details.querySelectorAll(":scope > .prow")].map((n) => [...n.classList].find((c) => c.startsWith("k-")).slice(2)) } : null,
      allRows: [...p.querySelectorAll(".prow")].length,
      bandRows: [...band.querySelectorAll(":scope > .band-line > .prow")].map((n) => [...n.classList].find((c) => c.startsWith("k-")).slice(2)),
      view: btn(".act-diff"), merge: btn(".act-merge"), discard: btn(".act-discard"), chat: btn(".act-chat"), download: btn(".act-download"),
      why: p.querySelector(".act-why").textContent,
      mergeClass: p.querySelector(".act-merge").classList.contains("unproven") ? "unproven" : "",
      panel: p.querySelector(".act-panel").textContent,
    };
  })()`);

  const out = { read: await read() };
  if (step === "read") {
    await toPacket();
    await shot("packet.png");
  } else if (step === "mutation") {
    // Open the band's Mutation line: the English summary sits inside that fold.
    await js(`document.querySelector(".band-line.k-mutation").open = true`);
    await sleep(300);
    out.mutation = await js(`(() => {
      const line = document.querySelector(".band-line.k-mutation");
      const pre = line.querySelector(".prow-summary");
      return { open: line.open, text: line.querySelector(".prow-text").textContent,
        summary: pre ? pre.textContent : null,
        summaryInFold: !!pre && line.contains(pre),
        others: [...document.querySelectorAll(".prow-summary")].length };
    })()`);
    await toPacket(".band");
    await shot("mutation-open.png");
  } else if (step === "diff") {
    await click(".act-diff");
    await until(`!!document.querySelector(".diff-file")`);
    out.diff = await js(`[...document.querySelectorAll(".diff-file")].map((f) => ({ path: f.dataset.path,
      add: [...f.querySelectorAll(".d-add")].map((d) => d.textContent), del: [...f.querySelectorAll(".d-del")].map((d) => d.textContent) }))`);
    await toPacket(".actions");
    await shot("diff-open.png");
    await click(".act-diff");
    out.closed = await js(`document.querySelector(".act-panel").textContent`);
  } else if (step === "merge" || step === "merge-cancel") {
    await click(".act-merge");
    out.confirm = await js(`(() => { const c = document.querySelector(".act-confirm"); return c ? { text: c.querySelector(".act-confirm-text").textContent, yes: c.querySelector(".act-yes").textContent, focus: document.activeElement.textContent } : null; })()`);
    await toPacket(".actions");
    await shot("merge-confirm.png");
    if (step === "merge-cancel") {
      await click(".act-no");
      out.after = await js(`document.querySelector(".act-panel").textContent`);
    } else {
      await click(".act-yes");
      await until(`!!document.querySelector(".act-result")`);
      out.result = await js(`(() => { const r = document.querySelector(".act-result"); return { ok: r.classList.contains("ok"), output: r.querySelector(".act-output").textContent, note: (r.querySelector(".act-note") || {}).textContent || "" }; })()`);
      await toPacket(".actions");
      await shot("merge-result.png");
    }
  } else if (step === "discard") {
    await click(".act-discard");
    out.confirm = await js(`document.querySelector(".act-confirm-text").textContent`);
    await click(".act-yes");
    await until(`!!document.querySelector(".act-result")`);
    out.result = await js(`document.querySelector(".act-output").textContent`);
    out.after = await read();
  } else if (step === "chat" || step === "chat-send") {
    await click(".act-chat");
    await sleep(300);
    out.input = await js(`document.querySelector("#input").value`);
    out.mode = await js(`state.mode`);
    out.focused = await js(`document.activeElement.id`);
    if (step === "chat-send") {
      // Send the pre-fill as the Ask turn and wait for the turn to end.
      await js(`document.querySelector("#composer").requestSubmit()`);
      await until(`state.busy === true`, 5000).catch(() => {});
      await until(`state.busy === false`, 120000);
      await sleep(500);
      out.transcript = await js(`document.querySelector("#transcript").innerText`);
    }
  }
  console.log(JSON.stringify(out));
  await finish(0);
} catch (error) {
  console.error(String(error && error.stack || error));
  await finish(1);
}
