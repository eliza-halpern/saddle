// Streams a reply into the chat page in headless Chrome, in both orders a frame
// and the turn's end can arrive, and prints one JSON line of what was painted.
// Used by tests/test_paint_order.py; node >= 22 and Chrome only.
//
// A streamed delta is painted on the next animation frame (schedulePaint); the
// turn's end paints at once and cancels a frame still pending (flushPaint).
// Which comes first was left to timing, so whether each path ran differed from
// run to run. Here each round calls the page's own event handler in one order:
//   frame-first: a delta, two frames, then turn.end;
//   end-first:   a delta and turn.end in one task, then two frames.
// The turn's end must clear the frame it cancels: if the id is left behind, the
// next delta's schedulePaint returns early and that reply is never painted.
// Then two deltas inside one frame, and a task card's tool line in each state.
//
// usage: node paint_cdp.mjs <base-url> <session-id>
import { withPage } from "./cdp_page.mjs";

const [base, sid] = process.argv.slice(2);

// One round, run in the page. `order` is "frame-first" or "end-first"; `text` is
// the delta, unique per round so its paints can be counted.
const ROUND = `async (order, text) => {
  const count = () => document.querySelector("#transcript").textContent.split(text).length - 1;
  const frames = () => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
  handle({ kind: "turn.start" });
  handle({ kind: "content.delta", text });
  const pending = paintFrame !== 0;
  const beforePaint = count();
  if (order === "frame-first") {
    await frames();
    const painted = count();
    const frameLeft = paintFrame;
    handle({ kind: "turn.end" });
    return { order, pending, beforePaint, painted, frameLeft, atEnd: count() };
  }
  handle({ kind: "turn.end" });
  const painted = count();
  const frameLeft = paintFrame;
  await frames();
  return { order, pending, beforePaint, painted, frameLeft, afterFrames: count() };
}`;

// Two deltas inside one frame: the second finds the first's frame pending and
// joins it (schedulePaint's early return), so the reply is painted once, whole.
const TWO_IN_ONE_FRAME = `async () => {
  const joined = "fifth streamed reply, in two deltas";
  const count = () => document.querySelector("#transcript").textContent.split(joined).length - 1;
  const frames = () => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
  handle({ kind: "turn.start" });
  handle({ kind: "content.delta", text: "fifth streamed reply," });
  const first = paintFrame;
  handle({ kind: "content.delta", text: " in two deltas" });
  const sameFrame = first !== 0 && paintFrame === first;
  const beforePaint = count();
  await frames();
  const painted = count();
  const frameLeft = paintFrame;
  handle({ kind: "turn.end" });
  return { sameFrame, beforePaint, painted, frameLeft, atEnd: count() };
}`;

// A task card's tool line, painted at most once a frame (scheduleActivity): a
// frame inside a tool call shows it running, a failed call says so, and a call
// that starts and ends inside one frame shows only its result. Which of these a
// real run painted was left to timing; here each state gets its frame.
const TOOL_LINE = `async () => {
  const settle = async () => {
    await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
    await new Promise((r) => setTimeout(r, 100)); // the hidden-page path paints on a 50 ms timer
  };
  const run = "paint-test-run";
  handleTask({ kind: "task.state", run_id: run, task: "paint the tool line", state: "running" });
  const card = tasks.get(run);
  const line = () => card.activity.tool.textContent;
  const send = (inner) => handleTask({ kind: "task.event", run_id: run, event: inner });
  send({ kind: "tool.start", name: "read_file" });
  await settle();
  const running = line();
  send({ kind: "tool.end", ok: false });
  await settle();
  const failed = line();
  send({ kind: "tool.start", name: "list_dir" });
  send({ kind: "tool.end", ok: true });
  await settle();
  return { running, failed, oneFrame: line() };
}`;

try {
  const out = await withPage(base, async (page) => {
    // page.chat waits for the session's first render: started any sooner, a
    // round's turns could be wiped by the session.info that rebuilds the transcript.
    await page.chat(sid);
    const rounds = [];
    for (const [order, text] of [
      ["frame-first", "first streamed reply"],
      ["frame-first", "second streamed reply"],
      ["end-first", "third streamed reply"],
      ["frame-first", "fourth streamed reply"],
    ]) {
      rounds.push(await page.js(`(${ROUND})(${JSON.stringify(order)}, ${JSON.stringify(text)})`));
    }
    const two = await page.js(`(${TWO_IN_ONE_FRAME})()`);
    const toolLine = await page.js(`(${TOOL_LINE})()`);
    return { rounds, two, toolLine };
  });
  console.log(JSON.stringify(out));
  process.exit(0);
} catch (error) {
  console.error(String((error instanceof Error && error.stack) || error));
  process.exit(1);
}
