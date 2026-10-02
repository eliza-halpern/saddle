// Renders the chat page's key views in headless Chrome, in both themes, and
// writes one PNG per view plus a structural snapshot of each, for
// tests/test_ui_snapshots.py to compare against the committed references.
// node >= 22 and Chrome only. No model: the session is seeded by the test.
//
// Everything that varies between runs is fixed here: the viewport and device
// scale factor, the colour profile, font hinting, animations and the caret
// (a style rule, plus prefers-reduced-motion), and the run-dependent ids the
// packet prints (replaced by a fixed string of the same length before the
// picture and the snapshot are taken). Fonts are not fixed: they are whatever
// the machine resolves, which is why the output names the fonts Chrome used
// for each view and the test ties the pixel references to them.
//
// usage: node snapshot_cdp.mjs <base-url> <session-id> <out-dir> [extra-css]
//   extra-css is appended to the page after load (the test uses it to inject
//   a known defect and watch the comparison fail).
// stdout: one JSON line {chrome, platform, views: {"<theme>/<view>": {...}}}
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const [base, sid, outDir, extraCss = ""] = process.argv.slice(2);
const port = 9300 + Math.floor(Math.random() * 600);
const prof = mkdtempSync(join(tmpdir(), "cdp-snap-"));
const chrome = spawn(
  "google-chrome",
  [
    "--headless=new",
    "--no-sandbox",
    "--disable-gpu",
    "--hide-scrollbars",
    "--force-color-profile=srgb",
    "--font-render-hinting=none",
    "--disable-lcd-text",
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${prof}`,
    "about:blank",
  ],
  { stdio: "ignore" },
);
const pageErrors = [];
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
    } catch {
      /* chrome is still starting */
    }
    await sleep(200);
  }
  throw new Error("chrome did not start");
}

// The views. `from` and `to` name the first and last transcript element the
// picture covers (a CSS selector each); `select` is the list of
// [name, selector] pairs whose boxes, colours and text the snapshot records.
// A selector is looked up inside the transcript, first match.
const VIEWS = {
  chat: {
    from: ".turn:nth-child(1)",
    to: ".turn:nth-child(2)",
    select: [
      ["user", ".user"],
      ["heading", ".assistant h4"],
      ["paragraph", ".assistant p"],
      ["strong", ".assistant strong"],
      ["inline-code", ".assistant p code"],
      ["list", ".assistant ul"],
      ["list-item", ".assistant li"],
      ["code-block", ".assistant pre"],
      ["code-text", ".assistant pre code"],
      ["code-keyword", ".assistant pre .c-keyword"],
      ["copy", ".assistant pre .code-copy"],
    ],
  },
  "tool-collapsed": {
    from: "details.tool",
    to: "details.tool",
    closed: true,
    select: [
      ["row", "details.tool"],
      ["summary", "details.tool > summary"],
      ["dot", "details.tool .dot"],
      ["label", "details.tool .label"],
      ["copy", "details.tool > summary > .code-copy"],
    ],
  },
  "tool-expanded": {
    from: "details.tool",
    to: "details.tool",
    closed: false,
    select: [
      ["row", "details.tool"],
      ["summary", "details.tool > summary"],
      ["dot", "details.tool .dot"],
      ["label", "details.tool .label"],
      ["copy", "details.tool > summary > .code-copy"],
      ["output", "details.tool .tool-detail"],
    ],
  },
  packet: {
    from: ".packet",
    to: ".packet",
    closed: true,
    select: [
      ["packet", ".packet"],
      ["kicker", ".packet .packet-kicker"],
      ["verdict", ".packet .verdict"],
      ["verdict-word", ".packet .verdict-word"],
      ["actions", ".packet .actions"],
      ["view-diff", ".packet .act-diff"],
      ["merge", ".packet .act-merge"],
      ["discard", ".packet .act-discard"],
      ["ask", ".packet .act-chat"],
      ["download", ".packet .act-download"],
      ["band", ".packet .band"],
      ["tests-line", ".packet .band-line.k-tests"],
      ["mutation-line", ".packet .band-line.k-mutation"],
      ["not-proven-line", ".packet .band-line.k-not-proven"],
      ["glyph", ".packet .band-glyph"],
      ["rows", ".packet .rows"],
      ["details", ".packet .packet-details"],
    ],
  },
};
const THEMES = ["dark", "light"];
const VIEWPORT = { width: 1100, height: 1400 };

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
  const send = (method, params = {}) =>
    new Promise((resolve) => {
      const n = ++id;
      pending.set(n, resolve);
      ws.send(JSON.stringify({ id: n, method, params }));
    });
  const js = async (expression) => {
    const r = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
    if (r.result?.exceptionDetails) throw new Error(JSON.stringify(r.result.exceptionDetails));
    return r.result?.result?.value;
  };
  const until = async (cond, ms = 8000) => {
    for (let t = 0; t < ms; t += 100) {
      if (await js(cond).catch(() => false)) return true;
      await sleep(100);
    }
    throw new Error(`timed out waiting for ${cond}`);
  };

  const version = await send("Browser.getVersion");
  await send("Page.addScriptToEvaluateOnNewDocument", {
    source: `
    try { localStorage.setItem("saddle.session", ${JSON.stringify(sid)}); } catch { /* none */ }
  `,
  });
  await send("Emulation.setDeviceMetricsOverride", { ...VIEWPORT, deviceScaleFactor: 1, mobile: false });
  await send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-reduced-motion", value: "reduce" }] });
  await send("Page.enable");
  await send("Runtime.enable");
  await send("DOM.enable");
  await send("CSS.enable");
  await send("Page.navigate", { url: `${base}/` });
  await until(
    `!!document.querySelector(".assistant pre .code-copy") && !!document.querySelector("details.tool") && !!document.querySelector(".packet .band")`,
  );

  await js(`(() => {
    const style = document.createElement("style");
    style.textContent = ${JSON.stringify(
      "*,*::before,*::after,::details-content{animation:none!important;transition:none!important;caret-color:transparent!important}\n" +
        extraCss,
    )};
    document.head.append(style);
    // A run's ids, its branch name and the ledger's head hash are random per
    // run: a fixed string of the same length stands in, so neither the
    // picture nor the text moves.
    const fixed = "0123456789abcdef";
    const walker = document.createTreeWalker(document.getElementById("transcript"), NodeFilter.SHOW_TEXT);
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      n.nodeValue = n.nodeValue.replace(/\\b[0-9a-f]{8,16}\\b/g, (s) => fixed.slice(0, s.length));
    }
    return true;
  })()`);

  const doc = await send("DOM.getDocument", { depth: 0 });
  const fontsOf = async (selector) => {
    const found = await send("DOM.querySelector", { nodeId: doc.result.root.nodeId, selector });
    if (!found.result?.nodeId) return [];
    const r = await send("CSS.getPlatformFontsForNode", { nodeId: found.result.nodeId });
    return (r.result?.fonts || []).map(
      (f) => `${f.familyName}|${f.postScriptName}|${f.isCustomFont ? "web" : "system"}`,
    );
  };

  const out = {
    chrome: version.result.product,
    platform: process.platform,
    viewport: VIEWPORT,
    views: {},
  };
  for (const theme of THEMES) {
    await js(`(() => {
      try { localStorage.setItem("saddle.theme", ${JSON.stringify(theme)}); } catch { /* none */ }
      document.documentElement.dataset.theme = ${JSON.stringify(theme)};
      return true;
    })()`);
    for (const [name, view] of Object.entries(VIEWS)) {
      if (view.closed !== undefined) {
        await js(`document.querySelector("details.tool").open = ${!view.closed}; true`);
      }
      // One frame for layout after the theme or the fold changed.
      await js(`new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))`);
      const measured = await js(`(() => {
        const view = ${JSON.stringify(view)};
        const t = document.getElementById("transcript");
        const first = t.querySelector(view.from);
        const last = t.querySelector(view.to);
        first.scrollIntoView({ block: "start" });
        const a = first.getBoundingClientRect();
        const b = last.getBoundingClientRect();
        const x0 = Math.floor(Math.min(a.left, b.left));
        const y0 = Math.floor(Math.min(a.top, b.top));
        const x1 = Math.ceil(Math.max(a.right, b.right));
        const y1 = Math.ceil(Math.max(a.bottom, b.bottom));
        const clean = (s) => s.replace(/\\s+/g, " ").trim().slice(0, 240);
        const elements = {};
        for (const [label, selector] of view.select) {
          const node = t.querySelector(selector);
          if (!node) { elements[label] = null; continue; }
          const r = node.getBoundingClientRect();
          const cs = getComputedStyle(node);
          elements[label] = {
            selector,
            x: Math.round(r.left) - x0, y: Math.round(r.top) - y0,
            w: Math.round(r.width), h: Math.round(r.height),
            color: cs.color, background: cs.backgroundColor,
            display: cs.display, visibility: cs.visibility, opacity: cs.opacity,
            text: clean(node.innerText),
          };
        }
        return { clip: { x: x0, y: y0, width: x1 - x0, height: y1 - y0 }, elements,
                 inViewport: y0 >= 0 && y1 <= innerHeight && x0 >= 0 && x1 <= innerWidth };
      })()`);
      if (!measured.inViewport)
        throw new Error(`${theme}/${name} does not fit the ${VIEWPORT.width}x${VIEWPORT.height} viewport`);
      const fonts = new Set();
      for (const [, selector] of view.select) for (const f of await fontsOf(selector)) fonts.add(f);
      const shot = await send("Page.captureScreenshot", {
        format: "png",
        captureBeyondViewport: false,
        clip: { ...measured.clip, scale: 1 },
      });
      writeFileSync(join(outDir, `${theme}-${name}.png`), Buffer.from(shot.result.data, "base64"));
      out.views[`${theme}/${name}`] = {
        width: measured.clip.width,
        height: measured.clip.height,
        fonts: [...fonts].sort(),
        elements: measured.elements,
      };
    }
  }
  if (pageErrors.length) throw new Error(`page errors:\n${pageErrors.join("\n")}`);
  console.log(JSON.stringify(out));
  await finish(0);
} catch (error) {
  console.error(String((error && error.stack) || error));
  if (pageErrors.length) console.error(`page errors:\n${pageErrors.join("\n")}`);
  await finish(1);
}
