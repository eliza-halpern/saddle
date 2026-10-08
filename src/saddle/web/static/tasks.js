// @ts-check
"use strict";
/* Task cards: a run of `saddle auto`, started from the chat and read here.

   The card is built for the two moments the Daily Driver page says a
   person is needed: answering a question mid-run, and reading the packet
   at the end. Everything in between is glanceable -- a state, two meters,
   and the ledger's own lines as they are sealed.

   What is evidence and what is not is visible in the styling, not only in
   the words: packet rows carry cites that open the record they came from;
   the model's narrative is set apart, in another face, and labelled
   "narrative, not evidence"; a row with nothing behind it says so. The
   model-activity strip under the ledger lines is the live stream, set apart
   the same way and labelled "not evidence": it reads the run's events and
   writes nothing that a verdict, the ledger or the packet reads. */

/** @type {Record<string, {word: string, glyph: string}>} */
const TASK_STATES = {
  running: { word: "running", glyph: "●" },
  needs_you: { word: "needs you", glyph: "?" },
  finished: { word: "finished", glyph: "✓" },
  stopped: { word: "stopped", glyph: "■" },
  // Ended needing you: the finish audit asked a question. Not stopped, not
  // failed, and nothing waits on an answer any more.
  asked: { word: "needs you", glyph: "?" },
  unchanged: { word: "unchanged", glyph: "=" },
  failed: { word: "no outcome", glyph: "!" },
  loading: { word: "loading", glyph: "…" },
};
const ENDED = new Set(["finished", "stopped", "unchanged", "asked", "failed"]);

/**
 * What each state a run can report -- every name `TASK_STATES` has, plus
 * "stopping", which only this page's Stop button uses -- leaves in the topbar's
 * status pill, for #85 item 3: in a task session the topbar read a state the
 * run's own card read beside it, so the pill holds only what that card leaves
 * unsaid once it scrolls off a phone screen. `null` hides the pill; an entry
 * shows it as the state the topbar paints, spelled its own way.
 *
 * `needs_you` and `asked` are held as `needs` on purpose: it is the state that
 * needs you, the class the topbar already paints for it, and the name the
 * phone's answer control follows. `paintRunStatus` follows this table.
 * @type {Record<string, {kind: string, detail?: string} | null>}
 */
const PILL_STATES = {
  running: null, // the card's own "● running"
  needs_you: { kind: "needs", detail: "needs you" }, // the card's "? needs you", held on purpose
  finished: null, // the card's own "✓ finished"
  // "stopped" is the state that made this pill: the run was ended by you, which
  // the chat has no state of its own to say. The word it reads is the name of
  // the state, which the topbar spells from the face it wears.
  stopped: { kind: "stopped" },
  asked: { kind: "needs", detail: "needs you" }, // ended needing you, with nothing left to answer
  unchanged: null, // the card's own "= unchanged"
  failed: { kind: "failed", detail: "no outcome" }, // the card's word for it, in the class the topbar colours
  loading: null, // a run that has not started yet, which the card spells out in its own words
  // This page asked for the stop, and the card reads "stopping" until the run's
  // own state arrives: the pill says so beside it, and stops saying so when it does.
  stopping: { kind: "working", detail: "stopping…" },
};

/**
 * Say a run's state in the topbar (#85 item 3): `PILL_STATES` is the rule.
 *
 * The face the topbar wears, and the send button that follows it, stay what
 * they were for the states they already named: a run that is going or waiting
 * on you still stops on Send, and one the card ends holds no pill at all, so
 * the next thing this session does shows its own state instead of the last
 * run's. A state the card says for itself leaves the pill holding nothing; a
 * state the card does not name -- "stopped", "no outcome", the "stopping…"
 * this page asked for -- puts its own word there.
 * @param {string} reported the state a run reported, or "stopping"
 */
function paintRunStatus(reported) {
  state.taskPill = PILL_STATES[reported] ?? null;
  if (reported === "needs_you") {
    setStatus("needs", "needs you");
  } else if (reported === "running") {
    setStatus("working", "task running");
  } else {
    paintStatus();
  }
}

/** @typedef {{box: HTMLElement, track: HTMLElement, fill: HTMLElement, value: HTMLElement}} Meter */

/**
 * The model-activity strip's parts and counters (see activityStrip).
 * @typedef {object} Activity
 * @property {HTMLDetailsElement} box
 * @property {HTMLElement} glance
 * @property {HTMLElement} doing
 * @property {HTMLElement} streamed
 * @property {HTMLElement} tool
 * @property {HTMLElement} tailLabel
 * @property {HTMLElement} tail
 * @property {string} mode
 * @property {number} chars
 * @property {string} text
 * @property {string} stream
 * @property {{text: string, ok: boolean | null} | null} lastTool
 * @property {boolean} fresh
 * @property {boolean} queued
 * @property {number} spentAtStart
 * @property {boolean} joined
 */

/**
 * One task card: its elements and what the page knows of the run.
 * @typedef {object} TaskCard
 * @property {string} runId
 * @property {string} task
 * @property {HTMLElement} node
 * @property {HTMLElement} pill
 * @property {HTMLButtonElement} stop
 * @property {HTMLElement} testsChip
 * @property {Meter} time
 * @property {Meter} tokens
 * @property {HTMLElement} meters
 * @property {HTMLElement} now
 * @property {HTMLDetailsElement} log
 * @property {HTMLElement} logSummary
 * @property {HTMLElement} lines
 * @property {HTMLElement} ask
 * @property {HTMLElement} packet
 * @property {Activity} activity
 * @property {string} state
 * @property {number} elapsed
 * @property {number} elapsedAt
 * @property {number} timeBudget
 * @property {number} tokenBudget
 * @property {number} spent
 * @property {string | null} tokenSource the sealed outcome's token source, "usage" when
 *   the server counted every token; null while a run is going (#85 item 4)
 * @property {number} timer
 * @property {number} count
 * @property {string} phase
 * @property {number} round
 * @property {number} lastEventAt
 * @property {number} ageTimer
 * @property {boolean} [stopping]
 * @property {string} [detail]
 */

/**
 * One row of an evidence packet as the server compiles it.
 * @typedef {object} PacketRow
 * @property {string} key
 * @property {string} title
 * @property {string} status
 * @property {string} text
 * @property {string[]} items
 * @property {string[]} cites
 * @property {string} [summary]
 */

/** @typedef {{elapsed_s?: number, tokens?: number, time_budget_s?: number, token_budget?: number}} Spend */

/**
 * @typedef {object} Packet
 * @property {string} verdict
 * @property {string} verdict_text
 * @property {string} [task]
 * @property {PacketRow[]} rows
 * @property {string[]} header
 * @property {{text: string, flagged?: boolean}[]} narrative
 * @property {string} narrative_label
 * @property {Record<string, any>} records
 * @property {string[]} [questions]
 * @property {boolean | null} [test_edits]
 * @property {Spend} [spend]
 * @property {string} [token_source]
 * @property {boolean} [offer_test_edits]
 */

/** What `/api/.../branch` says about a run's branch; the server owns the fields. */
/** @typedef {{[field: string]: any}} BranchInfo */

/** @type {Map<string, TaskCard>} */
const tasks = new Map();

/** @param {number} seconds */
function fmtDuration(seconds) {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, "0")}s`;
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
}

/** @param {number} n */
function fmtTokens(n) {
  return n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : String(Math.round(n));
}

/**
 * @param {string} label
 * @returns {Meter}
 */
function meter(label) {
  const box = el("div", "tmeter");
  box.appendChild(el("span", "tmeter-label", label));
  const track = el("span", "tmeter-track");
  const fill = el("i");
  track.appendChild(fill);
  box.appendChild(track);
  const value = el("span", "tmeter-value", "—");
  box.appendChild(value);
  return { box, track, fill, value };
}

/**
 * @param {Meter} m
 * @param {number} used
 * @param {number} limit
 * @param {string} text
 */
function setMeter(m, used, limit, text) {
  // With no limit there is nothing to fill: an empty track would read as a cap that
  // does not exist (runs have no budget by default), so it is not drawn (#85 item 4).
  m.track.hidden = !(limit > 0);
  const pct = limit > 0 ? Math.min(100, (used / limit) * 100) : 0;
  m.fill.style.width = `${pct}%`;
  m.box.classList.toggle("hot", pct >= 80);
  m.value.textContent = text;
}

/* ---------- the card ---------- */

/**
 * @param {string} runId
 * @param {string} task
 * @param {HTMLElement | null | undefined} turnNode
 * @returns {TaskCard}
 */
function taskCard(runId, task, turnNode) {
  const known = tasks.get(runId);
  if (known) return known;
  const node = el("article", "task-card");
  node.dataset.state = "running";
  node.dataset.run = runId;

  const head = el("header", "task-head");
  const pill = el("span", "task-pill");
  head.appendChild(pill);
  const title = el("span", "task-id", `task ${runId}`);
  head.appendChild(title);
  const testsChip = el("span", "task-tests");
  testsChip.hidden = true;
  head.appendChild(testsChip);
  const stop = el("button", "task-stop", "Stop");
  stop.type = "button";
  stop.title = "Stop at the next safe point. The run ends stopped, and what it did is kept.";
  stop.onclick = (event) => {
    event.preventDefault();
    stopTask(runId);
  };
  head.appendChild(stop);
  node.appendChild(head);

  const meters = el("div", "task-meters");
  const time = meter("time");
  const tokens = meter("tokens");
  meters.appendChild(time.box);
  meters.appendChild(tokens.box);
  node.appendChild(meters);

  const now = el("div", "task-now");
  node.appendChild(now);

  const log = el("details", "task-log");
  log.open = true;
  const logSummary = el("summary", null, "Session");
  log.appendChild(logSummary);
  const lines = el("ol", "task-lines");
  log.appendChild(lines);
  node.appendChild(log);

  const activity = activityStrip();
  node.appendChild(activity.box);

  const ask = el("div", "task-ask");
  ask.hidden = true;
  node.appendChild(ask);

  const packet = el("section", "packet");
  packet.hidden = true;
  node.appendChild(packet);

  (turnNode || state.turnNode || $("#transcript")).appendChild(node);
  /** @type {TaskCard} */
  const card = {
    runId,
    task,
    node,
    pill,
    stop,
    testsChip,
    time,
    tokens,
    meters,
    now,
    log,
    logSummary,
    lines,
    ask,
    packet,
    activity,
    state: "running",
    elapsed: 0,
    elapsedAt: Date.now(),
    timeBudget: 0,
    tokenBudget: 0,
    spent: 0,
    tokenSource: null,
    timer: 0,
    count: 0,
    phase: "starting",
    round: 1,
    lastEventAt: Date.now(),
    ageTimer: 0,
  };
  tasks.set(runId, card);
  paintState(card);
  return card;
}

/**
 * @param {TaskCard} card
 * @param {boolean | null | undefined} editable
 */
function paintTests(card, editable) {
  if (editable === null || editable === undefined) return;
  card.testsChip.hidden = false;
  card.testsChip.textContent = editable ? "tests editable" : "tests read-only";
  card.testsChip.classList.toggle("ro", !editable);
}

/** @param {TaskCard} card */
function paintState(card) {
  const look = TASK_STATES[card.state] || TASK_STATES.running;
  card.node.dataset.state = card.state;
  card.pill.textContent = "";
  card.pill.appendChild(el("b", null, look.glyph));
  card.pill.appendChild(document.createTextNode(` ${look.word}`));
  card.stop.hidden = ENDED.has(card.state) || card.state === "loading";
  // The live stream is for a run that is still going; an ended run's story
  // is its packet.
  card.activity.box.hidden = ENDED.has(card.state) || card.state === "loading";
  paintActivity(card);
  tick(card);
  clearInterval(card.timer);
  if (card.state === "running") card.timer = setInterval(() => tick(card), 1000);
  clearInterval(card.ageTimer);
  if (ENDED.has(card.state)) {
    card.now.textContent = "";
    card.now.hidden = true;
  } else if (card.state !== "loading") {
    paintNow(card);
    card.ageTimer = setInterval(() => paintNow(card), 1000);
  }
}

/* The head's live line: `phase · round N · last event Xs ago`. The phase
   and round come from the server (task.phase); the age is this page's clock. */
/** @param {TaskCard} card */
function paintNow(card) {
  if (ENDED.has(card.state)) return;
  const age = (Date.now() - card.lastEventAt) / 1000;
  const phase = card.state === "needs_you" ? "waiting for you" : card.phase;
  card.now.hidden = false;
  card.now.textContent = `${card.stopping ? "stopping · " : ""}${phase} · round ${card.round} · last event ${fmtDuration(age)} ago`;
}

/** @param {TaskCard} card */
function tick(card) {
  // Waiting on you is not charged to the time budget (engine._ask), so the
  // clock is frozen while the card says "needs you".
  const live = card.state === "running" ? (Date.now() - card.elapsedAt) / 1000 : 0;
  const elapsed = card.elapsed + live;
  const budget = card.timeBudget;
  // "~" marks a count that is partly estimated: a live run's in-flight reply, or an
  // outcome sealed with estimated tokens. A count the server measured has none.
  const approx = card.tokenSource === "usage" ? "" : "~";
  setMeter(
    card.time,
    elapsed,
    budget,
    budget ? `${fmtDuration(elapsed)} of ${fmtDuration(budget)}` : fmtDuration(elapsed),
  );
  setMeter(
    card.tokens,
    card.spent,
    card.tokenBudget,
    card.tokenBudget
      ? `${approx}${fmtTokens(card.spent)} of ${fmtTokens(card.tokenBudget)}`
      : `${approx}${fmtTokens(card.spent)} tokens`,
  );
}

/**
 * @param {TaskCard} card
 * @param {ServerEvent} line
 */
function addLine(card, line) {
  const item = el("li", `tl tone-${line.tone || "info"}`);
  item.appendChild(el("span", "tl-mark", line.mark));
  item.appendChild(el("span", "tl-text", line.text));
  item.appendChild(sessionCite(card, line.cite)); // click to open the sealed record, again to close
  item.title = `ledger record ${line.cite}`;
  card.lines.appendChild(item);
  card.count += 1;
  card.logSummary.textContent = `Session · ${card.count} ledger line${card.count === 1 ? "" : "s"}`;
  card.lines.scrollTop = card.lines.scrollHeight;
}

/* ---------- model activity: live, and not evidence ---------- */

/* What the model is doing right now, from the run's own events: whether it
   is thinking, writing or in a tool call, how much it has streamed this
   reply, the last tool call, and a short tail of the latest stream. None of
   it is sealed; it is folded by default and says so on its face. */
const ACTIVITY_OPEN = "saddle.activityOpen";
const STREAM_WINDOW = 40000; // chars of the current reply's stream kept for the scrollable monitor

function activityWanted() {
  try {
    return localStorage.getItem(ACTIVITY_OPEN) === "1";
  } catch {
    return false;
  }
}

/** @returns {Activity} */
function activityStrip() {
  const box = el("details", "task-activity");
  box.open = activityWanted();
  const head = el("summary", "act-head");
  head.appendChild(el("span", "act-title", "Model activity"));
  head.appendChild(el("span", "act-tag", "not evidence"));
  const glance = el("span", "act-glance");
  head.appendChild(glance);
  box.appendChild(head);
  const body = el("div", "act-body");
  const facts = el("dl", "act-facts");
  /** @param {string} label */
  const fact = (label) => {
    facts.appendChild(el("dt", null, label));
    const dd = el("dd");
    facts.appendChild(dd);
    return dd;
  };
  const doing = fact("now");
  const streamed = fact("this reply");
  const tool = fact("last tool");
  body.appendChild(facts);
  const tailLabel = el("div", "act-tail-label");
  const tail = el("blockquote", "act-tail");
  body.appendChild(tailLabel);
  body.appendChild(tail);
  body.appendChild(
    el(
      "p",
      "act-note",
      "Live from the model's stream. Nothing here is sealed or cited; the ledger lines above and the packet are the record.",
    ),
  );
  box.appendChild(body);
  box.addEventListener("toggle", () => {
    try {
      localStorage.setItem(ACTIVITY_OPEN, box.open ? "1" : "0");
    } catch {
      /* per-browser nicety only */
    }
  });
  return {
    box,
    glance,
    doing,
    streamed,
    tool,
    tailLabel,
    tail,
    mode: "waiting",
    chars: 0,
    text: "",
    stream: "",
    lastTool: null,
    fresh: true,
    queued: false,
    spentAtStart: 0, // the run's measured tokens when this reply began; the reply's exact count is the rise from here
    // A card rebuilt after a reload joins a reply part-way: its counts are
    // "since this page opened" until the next tool call starts a new reply.
    joined: true,
  };
}

/** @type {Record<string, string>} */
const ACT_WORDS = {
  waiting: "waiting for the model",
  thinking: "thinking",
  writing: "writing a reply",
  tool: "in a tool call",
  asking: "waiting for you",
};

/* One engine event of the run, read into the strip. A reply is counted from
   its first streamed character after a tool call; progress events (however
   often they come) do not reset it. */
/**
 * @param {TaskCard} card
 * @param {ServerEvent} inner
 */
function noteActivity(card, inner) {
  const a = card.activity;
  switch (inner.kind) {
    case "reasoning.delta":
    case "content.delta": {
      const stream = inner.kind === "reasoning.delta" ? "reasoning" : "reply";
      if (a.fresh) {
        a.chars = 0;
        a.text = "";
        a.fresh = false;
        a.spentAtStart = card.spent || 0;
      }
      if (a.stream !== stream) a.text = "";
      a.stream = stream;
      a.mode = stream === "reasoning" ? "thinking" : "writing";
      a.chars += (inner.text || "").length;
      a.text = (a.text + (inner.text || "")).slice(-STREAM_WINDOW);
      break;
    }
    case "tool.start":
      a.mode = "tool";
      a.fresh = true;
      a.joined = false;
      a.lastTool = { text: inner.present || inner.name || "a tool", ok: null };
      break;
    case "tool.end":
      a.mode = "waiting";
      a.lastTool = { text: inner.label || (a.lastTool && a.lastTool.text) || "a tool", ok: !!inner.ok };
      break;
    case "question":
      a.mode = "asking";
      break;
    case "question.answered":
      a.mode = "waiting";
      break;
    default:
      return;
  }
  scheduleActivity(card);
}

/* Deltas arrive a token at a time: paint at most once a frame. */
/** @param {TaskCard} card */
function scheduleActivity(card) {
  const a = card.activity;
  if (a.queued) return;
  a.queued = true;
  const run = () => {
    a.queued = false;
    paintActivity(card);
  };
  if (typeof requestAnimationFrame === "function" && !document.hidden) requestAnimationFrame(run);
  else setTimeout(run, 50);
}

/** @param {TaskCard} card */
function paintActivity(card) {
  const a = card.activity;
  const mode = card.state === "needs_you" ? "asking" : a.mode;
  const words = ACT_WORDS[mode] || mode;
  // Exact only: a reply's tokens are the server's measured count, known once
  // its round reports usage (the rise in the run's measured total since the
  // reply began). Until then there is no honest token number, so none is shown.
  const replyTokens = Math.max(0, (card.spent || 0) - (a.spentAtStart || 0));
  const tokens = replyTokens ? `${fmtTokens(replyTokens)} tokens` : "";
  a.box.dataset.mode = mode;
  a.glance.textContent = tokens && (mode === "thinking" || mode === "writing") ? `${words} · ${tokens}` : words;
  a.doing.textContent = words;
  const since = a.joined ? " since this page opened" : "";
  a.streamed.textContent = replyTokens
    ? `${tokens}${since} (measured)`
    : a.chars
      ? `streaming${since}; tokens are counted when the reply ends`
      : `nothing streamed${since || " yet"}`;
  a.tool.textContent = a.lastTool
    ? `${a.lastTool.text}${a.lastTool.ok === null ? " …" : a.lastTool.ok ? "" : " (failed)"}`
    : `none${since || " yet"}`;
  a.tailLabel.textContent =
    a.stream === "reasoning"
      ? "latest reasoning, not evidence"
      : a.stream === "reply"
        ? "latest reply text, not evidence"
        : "";
  // Show the whole current reply's stream in a scrollable box, so the reasoning
  // can be read back rather than only its last few tokens. Trim a partial first
  // word once the window fills. Follow the live end only when the reader is
  // already there, so scrolling up to read earlier thinking is never yanked.
  const shown = a.text.length >= STREAM_WINDOW ? `…${a.text.slice(a.text.indexOf(" ") + 1)}` : a.text;
  const atEnd = a.tail.scrollHeight - a.tail.scrollTop - a.tail.clientHeight < 40;
  a.tail.textContent = shown;
  a.tail.hidden = !shown;
  a.tailLabel.hidden = !shown;
  if (atEnd) a.tail.scrollTop = a.tail.scrollHeight;
}

/* ---------- needs you ---------- */

/**
 * @param {TaskCard} card
 * @param {{text: string, options?: string[]} | null | undefined} question
 */
function showQuestion(card, question) {
  const box = card.ask;
  box.textContent = "";
  if (!question) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  box.appendChild(el("div", "ask-kicker", "The run is paused until you answer"));
  box.appendChild(el("p", "ask-text", question.text));
  const input = el("textarea", "ask-input");
  input.rows = 2;
  input.placeholder = "Your answer, in words. It is sealed in the ledger.";
  const options = question.options || [];
  if (options.length) {
    const opts = el("div", "ask-options");
    for (const option of options) {
      const chip = el("button", "ask-option", option);
      chip.type = "button";
      chip.onclick = (event) => {
        event.preventDefault();
        for (const other of opts.children) other.classList.toggle("picked", other === chip);
        input.value = option;
        input.focus();
      };
      opts.appendChild(chip);
    }
    box.appendChild(opts);
  }
  box.appendChild(input);
  const row = el("div", "ask-row");
  const note = el("span", "ask-note", "");
  row.appendChild(note);
  const send = el("button", "ask-send primary", "Answer and continue");
  send.type = "button";
  send.onclick = async (event) => {
    event.preventDefault();
    const text = input.value.trim();
    if (!text) {
      note.textContent = "Type or pick an answer first.";
      return;
    }
    send.disabled = true;
    try {
      await api(`/api/tasks/${card.runId}/answer`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
      note.textContent = "Sent. Sealing it…";
    } catch (error) {
      note.textContent = errorText(error);
      send.disabled = false;
    }
  };
  row.appendChild(send);
  box.appendChild(row);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) send.click();
  });
}

/** @param {string} runId */
async function stopTask(runId) {
  const card = tasks.get(runId);
  if (card) {
    card.stopping = true;
    paintNow(card);
  }
  // The card reads "stopping", and the topbar's pill holds that word too until
  // the run's own state arrives (#85 item 3: it would otherwise stay "task
  // running" beside a card that no longer says so).
  paintRunStatus("stopping");
  try {
    await api(`/api/tasks/${runId}/stop`, { method: "POST" });
  } catch (error) {
    notice(errorText(error), "error");
  }
}

/* ---------- the packet ---------- */

/** @type {Record<string, string>} */
const STATUS_WORD = {
  proven: "proven",
  failed: "failed",
  observed: "recorded",
  absent: "no record",
  "not-proven": "not proven",
  question: "needs you",
  sanctioned: "sanctioned",
  narrative: "narrative, not evidence",
  cost: "sealed",
};

/**
 * @param {string} hash
 * @param {Record<string, unknown> | null | undefined} record
 */
function recordPanel(hash, record) {
  const box = el("dl", "record");
  box.dataset.hash = hash;
  for (const [key, value] of Object.entries(record || { hash, note: "not in this packet" })) {
    if (value === "" || value === null || value === undefined) continue;
    box.appendChild(el("dt", null, key));
    box.appendChild(el("dd", null, String(value)));
  }
  return box;
}

/**
 * @param {string} hash
 * @param {Record<string, unknown> | null | undefined} record
 * @param {HTMLElement} host
 */
function citeButton(hash, record, host) {
  const button = el("button", "cite", hash.slice(0, 8));
  button.type = "button";
  button.title = "Open the ledger record this row is drawn from";
  // The chip's visible text is eight hex digits, which names nothing a listener
  // can act on: the name says what the chip is and which record it opens.
  button.setAttribute("aria-label", `Open the sealed ledger record ${hash.slice(0, 8)}`);
  button.onclick = (event) => {
    event.preventDefault();
    const open = host.querySelector(`.record[data-hash="${hash}"]`);
    for (const other of host.querySelectorAll(".record")) other.remove();
    for (const other of host.querySelectorAll(".cite.open")) other.classList.remove("open");
    if (open) return;
    button.classList.add("open");
    host.appendChild(recordPanel(hash, record));
  };
  return button;
}

/* A session-log line's cite: unlike a packet cite it carries only the hash, so
   it fetches the sealed record from the run's ledger on demand, opens it under
   the line, and closes it on a second click. */
/**
 * @param {TaskCard} card
 * @param {string} hash
 */
function sessionCite(card, hash) {
  const button = el("button", "cite tl-cite", hash.slice(0, 8));
  button.type = "button";
  button.title = "Open this sealed ledger record";
  button.setAttribute("aria-label", `Open the sealed ledger record ${hash.slice(0, 8)}`);
  button.onclick = async (event) => {
    event.preventDefault();
    const host = card.lines;
    const open = host.querySelector(`.record[data-hash="${hash}"]`);
    for (const other of host.querySelectorAll(".record")) other.remove();
    for (const other of host.querySelectorAll(".cite.open")) other.classList.remove("open");
    if (open) return; // it was open: the removal above collapsed it
    button.classList.add("open");
    let record;
    try {
      record = await api(`/api/sessions/${state.sessionId}/tasks/${card.runId}/record/${hash}`);
    } catch (error) {
      record = { hash, note: `could not load this record: ${errorText(error)}` };
    }
    if (!button.classList.contains("open")) return; // a later click closed it while we fetched
    /** @type {Element} */ (button.closest("li")).after(recordPanel(hash, record));
  };
  return button;
}

/**
 * @param {Packet} packet
 * @returns {Node}
 */
function narrativeBlock(packet) {
  const quote = el("blockquote", "narrative-text");
  if (!packet.narrative.length) return document.createDocumentFragment();
  for (const sentence of packet.narrative) {
    if (sentence.flagged) {
      // Normal ink with a quiet note, not struck through in red: the model's
      // claim is not evidence, but crossing it out shouts louder than it should.
      const span = el("span", "flagged");
      span.appendChild(el("span", null, sentence.text));
      span.appendChild(el("em", "flagged-why", " (asserts a check result — not evidence)"));
      quote.appendChild(span);
    } else {
      quote.appendChild(el("span", null, sentence.text));
    }
    quote.appendChild(document.createTextNode(" "));
  }
  return quote;
}

/* `a command` in a row's text is shown as code; nothing else is parsed. */
/**
 * @param {HTMLElement} node
 * @param {string} text
 */
function codeSpans(node, text) {
  text.split("`").forEach((part, i) => {
    if (!part) return;
    node.appendChild(i % 2 ? el("code", null, part) : document.createTextNode(part));
  });
  return node;
}

/**
 * @param {TaskCard} card
 * @param {Packet} packet
 */
function renderPacket(card, packet) {
  const box = card.packet;
  box.textContent = "";
  box.hidden = false;
  box.appendChild(el("div", "packet-kicker", "Evidence packet · compiled from the ledger"));

  const verdict = el("div", `verdict v-${packet.verdict}`);
  const word =
    {
      finished: "Finished",
      stopped: "Stopped",
      unchanged: "Unchanged",
      needs_you: "Needs you",
      unrecorded: "No outcome",
    }[packet.verdict] || packet.verdict;
  const top = el("div", "verdict-top");
  top.appendChild(el("span", "verdict-word", word));
  const audited = packet.rows.some((r) => r.key === "audit" || r.status === "proven");
  if (packet.verdict === "finished" && !audited) {
    top.appendChild(el("span", "verdict-tag", "not audited"));
  }
  verdict.appendChild(top);
  if (packet.task) verdict.appendChild(el("p", "verdict-task", packet.task));
  verdict.appendChild(el("p", "verdict-text", packet.verdict_text));
  paintTests(card, packet.test_edits);
  card.tokenSource = packet.token_source || null;
  paintSpend(card, packet.spend);
  if (packet.offer_test_edits) verdict.appendChild(testEditOffer(card, packet));
  if (packet.header.length) {
    const meta = el("div", "verdict-meta");
    for (const item of packet.header) meta.appendChild(el("span", null, item));
    verdict.appendChild(meta);
  }
  box.appendChild(verdict);
  const questions = packet.questions || [];
  if (questions.length) box.appendChild(askedBox(questions));

  box.appendChild(actionRow(card, packet));
  box.appendChild(summaryBand(packet));

  // First screen: the band, then Scope, then Audit folded to its count,
  // then the edit checks (tier 0) on their own line.
  // Everything else is under Details. A row this layout does not know goes
  // to Details too, so no row the packet compiles is ever dropped.
  const byKey = new Map(packet.rows.map((row) => [row.key, row]));
  const rows = el("div", "rows");
  const scope = byKey.get("scope");
  if (scope) rows.appendChild(packetRow(scope, packet));
  const audit = byKey.get("audit");
  if (audit) {
    const fold = el("details", `audit-fold s-${audit.status}`);
    const summary = el("summary", "audit-summary");
    summary.appendChild(el("span", "prow-title", "Audit"));
    summary.appendChild(el("span", "audit-count", audit.text.replace(/ findings?(?= passed)/, "").replace(/\.$/, "")));
    fold.appendChild(summary);
    fold.appendChild(packetRow(audit, packet));
    rows.appendChild(fold);
  }
  // The task-text check (P1): whether it ran and what it judged, on the first screen.
  const p1 = byKey.get("p1");
  if (p1) rows.appendChild(packetRow(p1, packet));
  // Tier-0 edit checks: their own line beside Audit, never in its count.
  const editChecks = byKey.get("edit-checks");
  if (editChecks) rows.appendChild(packetRow(editChecks, packet));
  box.appendChild(rows);
  const details = el("details", "packet-details");
  details.appendChild(el("summary", "details-summary", "Details"));
  const placed = new Set(["tests", "mutation", "not-proven", "scope", "audit", "p1", "edit-checks"]);
  for (const key of DETAIL_ORDER) {
    const detailRow = byKey.get(key);
    if (detailRow) details.appendChild(packetRow(detailRow, packet));
  }
  for (const row of packet.rows) {
    if (!placed.has(row.key) && !DETAIL_ORDER.includes(row.key)) {
      details.appendChild(packetRow(row, packet));
    }
  }
  box.appendChild(details);
  // Reading the packet is the point now; the session is one click away.
  if (card.count) card.log.open = false;
}

/* An ended run's meters show what its outcome sealed -- the numbers the
   packet's Cost row gives in words -- whether the card watched the run or
   was drawn from its recap after a reload. */
/**
 * @param {TaskCard} card
 * @param {Spend | null | undefined} spend
 */
function paintSpend(card, spend) {
  if (!spend || typeof spend.elapsed_s !== "number") return;
  card.elapsed = spend.elapsed_s;
  card.elapsedAt = Date.now();
  if (typeof spend.tokens === "number") card.spent = spend.tokens;
  if (spend.time_budget_s) card.timeBudget = spend.time_budget_s;
  if (spend.token_budget) card.tokenBudget = spend.token_budget;
  card.meters.hidden = false;
  tick(card);
}

/* The questions an ended run's audit asked: what the person decides. Never
   drawn as a pass or a fail; "would refuse at full strength" is a tag. */
const WOULD_REFUSE = "[would refuse at full strength]";

/** @param {string[]} questions */
function askedBox(questions) {
  const box = el("div", "task-asked");
  box.appendChild(
    el(
      "div",
      "ask-kicker",
      `The audit asks you ${questions.length === 1 ? "a question" : `${questions.length} questions`}`,
    ),
  );
  for (const text of questions) {
    const p = el("p", "ask-text");
    const parts = text.split(WOULD_REFUSE);
    parts.forEach((part, i) => {
      if (part) p.appendChild(document.createTextNode(part));
      if (i < parts.length - 1) p.appendChild(el("span", "asked-tag", "would refuse at full strength"));
    });
    box.appendChild(p);
  }
  box.appendChild(
    el(
      "p",
      "asked-note",
      "Not a pass and not a failure: the run ended so you can decide. Read the branch, then merge it, discard it, or run the task again with your answer.",
    ),
  );
  return box;
}

/* ---------- the packet's first screen ---------- */

const DETAIL_ORDER = ["contract", "narrative", "cost", "reproduce"];
const BAND_KEYS = ["tests", "mutation", "not-proven"];
/** @type {Record<string, string>} */
const GLYPH = {
  ok: "✓",
  bad: "✗",
  info: "◐",
  none: "○",
  warn: "!",
};

/* A band line's tone: from the row's status alone. "Not proven" is ok only
   when it lists nothing. */
/** @param {PacketRow} row */
function lineTone(row) {
  if (row.key === "not-proven") return row.items.length ? "warn" : "ok";
  return { proven: "ok", failed: "bad", observed: "info", absent: "none", sanctioned: "info" }[row.status] || "none";
}

/** @param {string} text */
function firstSentence(text) {
  const match = /^(.*?[.!?])(\s|$)/.exec(text);
  return match ? match[1] : text;
}

/* Tests, Mutation, Not proven: the three rows that decide "can I merge".
   The band is green only for a finished run whose three lines are all ok;
   a stop is amber whatever its lines say, and its unresolved findings are
   listed here, not under a fold. */
/** @param {Packet} packet */
function summaryBand(packet) {
  const byKey = new Map(packet.rows.map((row) => [row.key, row]));
  const lines = /** @type {PacketRow[]} */ (BAND_KEYS.filter((key) => byKey.has(key)).map((key) => byKey.get(key)));
  const tones = lines.map(lineTone);
  const tone =
    packet.verdict !== "finished"
      ? "stop"
      : tones.includes("bad")
        ? "bad"
        : tones.length === BAND_KEYS.length && tones.every((t) => t === "ok")
          ? "ok"
          : "partial";
  const band = el("div", `band band-${tone}`);
  band.dataset.tone = tone;
  lines.forEach((row, i) => {
    // Each line opens to its full row, cites and all: the band summarises
    // rows, it does not replace them.
    const line = el("details", `band-line t-${tones[i]} k-${row.key}`);
    line.dataset.key = row.key;
    line.dataset.tone = tones[i];
    const head = el("summary", "band-head");
    const glyph = el("span", "band-glyph", GLYPH[tones[i]]);
    glyph.setAttribute("aria-hidden", "true");
    head.appendChild(glyph);
    head.appendChild(el("span", "band-title", row.title));
    const text =
      row.key === "not-proven" && row.items.length
        ? `${row.items.length} thing${row.items.length === 1 ? "" : "s"} this packet cannot vouch for.`
        : firstSentence(row.text);
    head.appendChild(codeSpans(el("span", "band-text"), text));
    const listed = row.key === "not-proven" && row.items.length > 0;
    if (listed) {
      const list = el("ul", "band-items");
      for (const item of row.items) list.appendChild(el("li", null, item));
      head.appendChild(list);
    }
    line.appendChild(head);
    // The head already lists Not proven's items; its fold adds the cites
    // only, so each item is drawn once, as packet.md prints it once.
    line.appendChild(packetRow(row, packet, { listed }));
    band.appendChild(line);
  });
  return band;
}

/**
 * @param {PacketRow} row
 * @param {Packet} packet
 * @param {{listed?: boolean}} [options]
 */
function packetRow(row, packet, { listed = false } = {}) {
  const item = el("div", `prow s-${row.status} k-${row.key}`);
  const label = el("div", "prow-label");
  label.appendChild(el("span", "prow-title", row.title));
  label.appendChild(
    el("span", "prow-status", row.key === "narrative" ? packet.narrative_label : STATUS_WORD[row.status] || row.status),
  );
  item.appendChild(label);
  const body = el("div", "prow-body");
  if (row.key === "narrative") {
    body.appendChild(narrativeBlock(packet));
    body.appendChild(el("p", "prow-note", row.text));
  } else if (!listed) {
    body.appendChild(codeSpans(el("p", "prow-text"), row.text));
  }
  // The row's English, compiled from the record it cites (the Mutation
  // row's mutant summary): beneath the count line, inside the row's fold.
  if (row.summary) body.appendChild(el("pre", "prow-summary", row.summary));
  if (row.items.length && !listed) {
    const list = el("ul", "prow-items");
    for (const text of row.items) {
      const li = el("li");
      if (row.key === "reproduce") li.appendChild(el("code", null, text));
      else li.textContent = text;
      list.appendChild(li);
    }
    body.appendChild(list);
  }
  if (row.cites.length) {
    const cites = el("div", "cites");
    cites.appendChild(el("span", "cites-label", "ledger"));
    for (const hash of row.cites) cites.appendChild(citeButton(hash, packet.records[hash], body));
    body.appendChild(cites);
  }
  item.appendChild(body);
  return item;
}

/* ---------- what to do with the run's branch ---------- */

/**
 * @param {string} label
 * @param {string} cls
 */
function actionButton(label, cls) {
  const button = el("button", `act ${cls}`, label);
  button.type = "button";
  return button;
}

/**
 * @param {TaskCard} card
 * @param {string} action
 * @param {string} branch
 * @param {object} [extra]
 */
async function postAction(card, action, branch, extra = {}) {
  return api(`/api/sessions/${state.sessionId}/tasks/${card.runId}/${action}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ confirm: branch, ...extra }),
  });
}

/**
 * @param {HTMLElement} panel
 * @param {{path: string, patch: string}[]} files
 */
function showDiff(panel, files) {
  panel.textContent = "";
  if (!files.length) {
    panel.appendChild(el("p", "act-note", "The run's branch changes no file."));
    return;
  }
  for (const file of files) {
    const one = el("details", "diff-file");
    one.open = true;
    one.dataset.path = file.path;
    one.appendChild(el("summary", "diff-path", file.path));
    const body = el("div", "tool-detail diff-body");
    renderDiff(body, file.patch);
    one.appendChild(body);
    // The run's own diff is code on screen like any other: it gets the
    // standard button, in the file's header, copying the patch as git
    // wrote it.
    attachCopy(/** @type {Element} */ (one.firstElementChild), file.patch, file.path);
    panel.appendChild(one);
  }
}

/* The confirm step lives in the page and names branch and target. Nothing
   changes until its own button is pressed; Cancel and Escape change nothing. */
/**
 * @param {HTMLElement} panel
 * @param {string} text
 * @param {string} go
 * @param {() => void} onYes
 */
function confirmStrip(panel, text, go, onYes) {
  panel.textContent = "";
  panel.dataset.showing = "confirm";
  const strip = el("div", "act-confirm");
  strip.setAttribute("role", "alertdialog");
  strip.appendChild(el("p", "act-confirm-text", text));
  const yes = actionButton(go, "act-yes primary");
  const no = actionButton("Cancel", "act-no");
  const cancel = () => {
    panel.textContent = "";
  };
  no.onclick = cancel;
  strip.addEventListener("keydown", (event) => {
    if (event.key === "Escape") cancel();
  });
  yes.onclick = () => {
    yes.disabled = true;
    no.disabled = true;
    onYes();
  };
  strip.appendChild(yes);
  strip.appendChild(no);
  panel.appendChild(strip);
  no.focus();
}

/**
 * @param {HTMLElement} panel
 * @param {boolean} ok
 * @param {string} text
 * @param {boolean} [sealed]
 */
function actionResult(panel, ok, text, sealed) {
  panel.textContent = "";
  panel.dataset.showing = "result";
  const box = el("div", `act-result ${ok ? "ok" : "error"}`);
  box.appendChild(el("pre", "act-output", text));
  // The button sits in the box, not in the pre: the pre's text is quoted
  // verbatim by the card's readers, and a button label would sit inside it.
  attachCopy(box, text, "output");
  if (sealed === false) {
    box.appendChild(
      el(
        "p",
        "act-note",
        "Not sealed in the ledger: there is no ledger record for user actions. Logged to this session's actions.log.",
      ),
    );
  }
  panel.appendChild(box);
}

/**
 * @param {TaskCard} card
 * @param {Packet} packet
 */
function actionRow(card, packet) {
  const wrap = el("div", "actions");
  const row = el("div", "act-row");
  const panel = el("div", "act-panel");
  const view = actionButton("View diff", "act-diff");
  const merge = actionButton("Merge", "act-merge");
  // Offered only when the current branch has an upstream to push to.
  const push = actionButton("Merge and push", "act-push");
  push.hidden = true;
  // Push the checkout's current (work) branch to its remote: offered off the main branch.
  const pushBranch = actionButton("Push branch", "act-pushbranch");
  pushBranch.hidden = true;
  const discard = actionButton("Discard branch", "act-discard");
  const chat = actionButton("Ask about this run", "act-chat");
  const download = actionButton("Download full report", "act-download");
  for (const b of [view, merge, push, discard]) b.disabled = true;
  const why = el("p", "act-why");
  row.append(view, merge, push, pushBranch, discard, chat, download);
  wrap.append(row, why, panel);

  /** @type {BranchInfo | null} */
  let info = null;
  api(`/api/sessions/${state.sessionId}/tasks/${card.runId}/branch`)
    .then((got) => {
      info = got;
      view.disabled = !got.exists;
      discard.disabled = !got.exists;
      merge.disabled = !got.exists || !!got.merge_refusal;
      // Merge needs one auditor verdict, not all of them: a run with no proven
      // Mutation row may merge, and the button says what it is merging without.
      const mutation = packet.rows.find((r) => r.key === "mutation");
      const unproven = !mutation || mutation.status !== "proven";
      // The "(mutation unproven)" suffix explains why an ENABLED merge lands
      // without a mutation proof; on a disabled button it only adds noise, so drop it.
      merge.textContent =
        (got.target ? `Merge into ${got.target}` : "Merge") +
        (unproven && !merge.disabled ? " (mutation unproven)" : "");
      merge.classList.toggle("unproven", unproven);
      push.hidden = !got.upstream;
      push.disabled = merge.disabled || !!got.push_refusal;
      push.textContent =
        `Merge and push to ${got.upstream}` + (unproven && !push.disabled ? " (mutation unproven)" : "");
      push.classList.toggle("unproven", unproven);
      // A run the self-guard held changed saddle's judges: its finish audit
      // passed, but only a person may land it. Merge becomes Approve and merge.
      const guarded = got.guarded_paths && got.guarded_paths.length > 0;
      merge.dataset.guarded = guarded ? "1" : "";
      if (guarded) {
        merge.disabled = !got.exists || !!got.approve_refusal;
        merge.textContent = got.target ? `Approve and merge into ${got.target}` : "Approve and merge";
        merge.classList.remove("unproven");
        push.hidden = true;
      }
      const pb = got.push_branch;
      pushBranch.hidden = !pb || !pb.branch || pb.on_main;
      if (pb && !pushBranch.hidden) {
        pushBranch.textContent = `Push ${pb.branch} to ${pb.remote}`;
        pushBranch.disabled = !!pb.refusal;
        pushBranch.title = pb.refusal || "";
      }
      if (!got.exists) why.textContent = `The branch ${got.branch} is gone.`;
      else if (guarded && got.approve_refusal) why.textContent = `Approve is off: ${got.approve_refusal}`;
      else if (guarded)
        why.textContent = `Changes saddle's judges: ${got.guarded_paths.join(", ")}. Only you can approve it.`;
      else if (got.merge_refusal) why.textContent = `Merge is off: ${got.merge_refusal}`;
      else if (got.upstream && got.push_refusal) why.textContent = `Push is off: ${got.push_refusal}`;
    })
    .catch((error) => {
      why.textContent = `No branch actions: ${errorText(error)}`;
    });

  view.onclick = async () => {
    if (panel.dataset.showing === "diff") {
      panel.textContent = "";
      panel.dataset.showing = "";
      return;
    }
    try {
      const got = await api(`/api/sessions/${state.sessionId}/tasks/${card.runId}/diff`);
      showDiff(panel, got.files);
      panel.dataset.showing = "diff";
    } catch (error) {
      actionResult(panel, false, errorText(error));
    }
  };
  merge.onclick = () => {
    if (!info) return;
    const branchInfo = info;
    if (merge.dataset.guarded) {
      const files = branchInfo.guarded_paths.join(", ");
      confirmStrip(
        panel,
        `Approve and merge ${branchInfo.branch} into ${branchInfo.target}? This run changed code that judges runs: ${files}. Its finish audit passed; you are approving these changes to saddle's judges as a person, and the approval is logged with your git identity.`,
        `Approve and merge into ${branchInfo.target}`,
        async () => {
          try {
            const got = await postAction(card, "approve", branchInfo.branch, { paths: files });
            actionResult(panel, true, got.output, got.sealed);
            merge.disabled = true;
          } catch (error) {
            actionResult(panel, false, errorText(error), false);
          }
        },
      );
      return;
    }
    confirmStrip(
      panel,
      `Merge ${branchInfo.branch} into ${branchInfo.target}? Fast-forward if possible, else cherry-pick its commits.`,
      `Merge into ${branchInfo.target}`,
      async () => {
        try {
          const got = await postAction(card, "merge", branchInfo.branch);
          actionResult(panel, true, got.output, got.sealed);
          merge.disabled = true;
          push.disabled = true;
        } catch (error) {
          actionResult(panel, false, errorText(error), false);
        }
      },
    );
  };
  push.onclick = () => {
    if (!info) return;
    const branchInfo = info;
    confirmStrip(
      panel,
      `Merge ${branchInfo.branch} into ${branchInfo.target}, then push ${branchInfo.target} to ${branchInfo.upstream}? The push is public to anyone who can read ${branchInfo.upstream}.`,
      `Merge and push to ${branchInfo.upstream}`,
      async () => {
        try {
          const got = await postAction(card, "merge-push", branchInfo.branch);
          actionResult(panel, true, got.output, got.sealed);
          merge.disabled = true;
          push.disabled = true;
        } catch (error) {
          const text = errorText(error);
          // The merge landed even though the push failed: it cannot merge twice.
          if (text.includes("Merged locally")) merge.disabled = push.disabled = true;
          actionResult(panel, false, text, false);
        }
      },
    );
  };
  pushBranch.onclick = () => {
    if (!info) return;
    const branchInfo = info;
    const pb = branchInfo.push_branch;
    confirmStrip(
      panel,
      `Push ${pb.branch} to ${pb.remote}? It goes up as a branch of the same name for a pull request; main is not touched. The leak guard runs first when the repo uses it.`,
      `Push ${pb.branch}`,
      async () => {
        try {
          const got = await api(`/api/sessions/${state.sessionId}/tasks/${card.runId}/push-branch`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ confirm: pb.branch }),
          });
          actionResult(panel, true, got.output, got.sealed);
        } catch (error) {
          actionResult(panel, false, errorText(error), false);
        }
      },
    );
  };
  discard.onclick = () => {
    if (!info) return;
    const branchInfo = info;
    confirmStrip(
      panel,
      `Delete the branch ${branchInfo.branch} and its run worktree? ${branchInfo.target || "Your checkout"} is not touched.`,
      `Delete ${branchInfo.branch}`,
      async () => {
        try {
          const got = await postAction(card, "discard", branchInfo.branch);
          actionResult(panel, true, got.output, got.sealed);
          for (const b of [view, merge, push, discard]) b.disabled = true;
        } catch (error) {
          actionResult(panel, false, errorText(error), false);
        }
      },
    );
  };
  // One markdown file, rendered fresh from the sealed ledger by the server
  // (packet.md beside the run's proofs.jsonl); the click is in actions.log.
  download.onclick = () => {
    const a = document.createElement("a");
    a.href = `/api/sessions/${state.sessionId}/tasks/${card.runId}/packet.md`;
    a.download = "";
    document.body.appendChild(a);
    a.click();
    a.remove();
  };
  chat.onclick = async () => {
    let recap = info && info.recap;
    if (!recap) {
      recap = `verdict: ${packet.verdict} — ${packet.verdict_text}`;
    }
    // Leave Task for the read-only talk lane; Edit stays an explicit choice.
    if (typeof setMode === "function" && state.mode !== "ask") await setMode("ask");
    const input = $("#input");
    // The compact recap, then where the full packet is on disk: the Ask
    // lane's read_file can open it on demand; the human sees a short message.
    const report = info && info.report ? `\n\nFull report: ${info.report}` : "";
    // One line of what to check, since the message may be sent as it stands:
    // the review is only as honest as what it was told to look for.
    const checks =
      "In your review: code only tests call is a defect; a changed " +
      "old test needs a flip: label and evidence other than the new code " +
      "failing it; skipped and not-proven items are findings.";
    input.value = `About the run "${packet.task}":\n\n${recap}\n\n${checks}${report}\n`;
    input.dispatchEvent(new Event("input"));
    input.focus();
  };
  return wrap;
}

/* A run that stopped "audit unresolved" with tests read-only, on a finding a
   new test closes, may be right and merely unable to prove it. Say so, and
   offer the same task again with test edits allowed. */
/**
 * @param {TaskCard} card
 * @param {Packet} packet
 */
function testEditOffer(card, packet) {
  const box = el("div", "offer");
  box.appendChild(
    el(
      "p",
      "offer-text",
      "Tests were read-only, so it could not write the test the auditor asked for. " +
        "The change may be correct; this run cannot show it.",
    ),
  );
  const again = el("button", "offer-run primary", "Run again with test edits allowed");
  again.type = "button";
  again.onclick = (event) => {
    event.preventDefault();
    again.disabled = true;
    launchTask(/** @type {string} */ (packet.task), card.timeBudget, card.tokenBudget, true);
  };
  box.appendChild(again);
  return box;
}

/** @param {TaskCard} card */
async function loadPacket(card) {
  // A run that failed before it produced a packet -- e.g. its folder was not a
  // git repository -- has no ledger to compile. Show why it failed, not a raw
  // "no such task" from trying to load a packet that never existed.
  if (card.state === "failed" && card.detail) {
    card.packet.hidden = false;
    card.packet.textContent = "";
    card.packet.appendChild(el("p", "notice", `This run did not start: ${card.detail}`));
    return;
  }
  try {
    const packet = await api(`/api/sessions/${state.sessionId}/tasks/${card.runId}/packet`);
    renderPacket(card, packet);
    const was = atBottom();
    stickToBottom(was);
  } catch (error) {
    card.packet.hidden = false;
    card.packet.textContent = "";
    const msg = card.detail
      ? `This run did not start: ${card.detail}`
      : card.state === "failed"
        ? "This run failed before it produced a packet (for example, its folder was not a git repository)."
        : `could not load the packet: ${errorText(error)}`;
    card.packet.appendChild(el("p", card.detail || card.state === "failed" ? "notice" : "notice error", msg));
  }
}

/* ---------- events ---------- */

/**
 * @param {ServerEvent} event
 * @returns {boolean}
 */
function handleTask(event) {
  switch (event.kind) {
    case "task.state": {
      let host = null;
      let watched = false;
      if (!tasks.has(event.run_id)) {
        // The turn startTask made for it, or -- a reload mid-run -- a new
        // one with the task's bubble, so the card never appears unexplained.
        watched = !!state.pendingTaskTurn;
        host = state.pendingTaskTurn || taskTurn(event.task);
        state.pendingTaskTurn = null;
      }
      const card = taskCard(event.run_id, event.task, host);
      if (watched) card.activity.joined = false; // this page saw the run start
      const was = card.state;
      if (event.time_budget_s) card.timeBudget = event.time_budget_s;
      if (event.token_budget) card.tokenBudget = event.token_budget;
      paintTests(card, event.test_edits);
      if (typeof event.elapsed_s === "number") {
        // The run's own budget says what it has spent (#114): a card built
        // after a reload starts here, not at zero.
        card.elapsed = event.elapsed_s;
        card.elapsedAt = Date.now();
      } else {
        if (was === "running" && event.state !== "running") {
          card.elapsed += (Date.now() - card.elapsedAt) / 1000;
        }
        if (event.state === "running" && was !== "running") card.elapsedAt = Date.now();
      }
      if (typeof event.tokens === "number") card.spent = event.tokens;
      card.state = event.state;
      card.lastEventAt = Date.now();
      paintState(card);
      showQuestion(card, event.state === "needs_you" ? event.question : null);
      state.activeTask = ENDED.has(event.state) ? null : event.run_id;
      runState(/** @type {string} */ (state.sessionId), event.state, event.task); // notify.js: tab, dot, live region
      // #85 item 3: the topbar's pill holds only what the run's own card says
      // instead, and `PILL_STATES` says which states hold what.
      paintRunStatus(event.state);
      if (ENDED.has(event.state)) {
        if (event.detail && event.state === "failed") {
          card.detail = event.detail; // why it failed, for loadPacket when there is no packet
          card.now.textContent = event.detail;
        }
        loadPacket(card);
      }
      return true;
    }
    case "task.line": {
      const card = tasks.get(event.run_id);
      if (card) {
        addLine(card, event);
        card.lastEventAt = Date.now();
      }
      return true;
    }
    case "task.phase": {
      const card = tasks.get(event.run_id);
      if (!card) return true;
      card.phase = event.phase;
      card.round = event.round;
      card.lastEventAt = Date.now();
      paintNow(card);
      return true;
    }
    case "task.event": {
      const card = tasks.get(event.run_id);
      if (!card) return true;
      const inner = event.event || {};
      card.lastEventAt = Date.now();
      if (inner.kind === "run.progress") {
        // Once a reply today; more often if the engine reports mid-reply.
        // Same shape either way: each one is the run's total so far.
        if (typeof inner.elapsed_s === "number") {
          card.elapsed = inner.elapsed_s;
          card.elapsedAt = Date.now();
        }
        if (typeof inner.tokens === "number") card.spent = inner.tokens;
        if (inner.time_budget_s) card.timeBudget = inner.time_budget_s;
        if (inner.token_budget) card.tokenBudget = inner.token_budget;
        tick(card);
        scheduleActivity(card); // a new measured total means the reply's exact tokens can show
      } else {
        noteActivity(card, inner);
      }
      paintNow(card);
      return true;
    }
    default:
      return false;
  }
}

/** @param {string} task */
function taskBubble(task) {
  const bubble = el("div", "user task-ask-bubble");
  bubble.appendChild(el("span", "task-badge", "▶ task"));
  bubble.appendChild(document.createTextNode(task));
  return bubble;
}

/** @param {string} task */
function taskTurn(task) {
  for (const empty of document.querySelectorAll("#transcript .empty")) empty.remove();
  const turn = newTurn(null);
  turn.appendChild(taskBubble(task));
  return turn;
}

/* A run's recap is stored as a message so the chat's model sees it.
   On reload, that message is drawn as the card it stands for. */
const RECAP = /^\[saddle task ([0-9a-f]+)\] ([^\n]*)/;

/**
 * @param {HTMLElement} turn
 * @param {unknown} content
 * @returns {boolean}
 */
function recapCard(turn, content) {
  const match = RECAP.exec(typeof content === "string" ? content : "");
  if (!match) return false;
  const [, runId, task] = match;
  turn.appendChild(taskBubble(task));
  const card = taskCard(runId, task, turn);
  card.state = "loading";
  card.log.hidden = true;
  card.meters.hidden = true; // until the packet says what the run spent
  paintState(card);
  loadPacket(card).then(() => {
    const verdict = card.packet.querySelector(".verdict");
    const cls = verdict ? [...verdict.classList].find((c) => c.startsWith("v-")) : null;
    card.state = cls
      ? { "v-finished": "finished", "v-stopped": "stopped", "v-unchanged": "unchanged", "v-needs_you": "asked" }[cls] ||
        "failed"
      : "failed";
    paintState(card);
  });
  return true;
}

/* ---------- starting one ---------- */

function openRunConfirm() {
  const text = $("#input").value.trim();
  if (!text || state.busy) {
    $("#input").focus();
    return;
  }
  if (!state.folder) {
    $("#mode-note").textContent = "Pick a folder first (Folder, in the sidebar). A task works on a copy of it.";
    $("#mode-note").hidden = false;
    return;
  }
  $("#mode-note").hidden = true;
  $("#tc-what").textContent = text;
  $("#tc-folder").textContent = state.folder || "this folder";
  api("/api/task-policy")
    .then((policy) => {
      $("#tc-test-edits").checked = !!policy.test_edits;
      $("#tc-p1").hidden = !policy.task_text_check;
      paintTestPolicy();
    })
    .catch(() => {});
  $("#task-confirm").hidden = false;
  $("#tc-start").focus();
}

function paintTestPolicy() {
  $("#tc-tests").textContent = $("#tc-test-edits").checked
    ? "it may edit test files"
    : "test files are read-only to it";
}

function closeRunConfirm() {
  $("#task-confirm").hidden = true;
}

async function startTask() {
  const input = $("#input");
  const text = input.value.trim();
  // Nothing runs unless the strip is on screen: it is the one place the
  // folder, lane and test policy are shown before a run.
  if (!text || state.busy || $("#task-confirm").hidden) return;
  closeRunConfirm();
  input.value = "";
  input.style.height = "auto";
  // No time or token limit (engine.NO_LIMIT): a run stops at finish, an
  // error, the stall check or your Stop button, never at a clock.
  await launchTask(text, 0, 0, $("#tc-test-edits").checked, $("#tc-premise").checked, $("#tc-stall").checked);
}

/**
 * @param {string} text
 * @param {number} timeBudget
 * @param {number} tokenBudget
 * @param {boolean} allowTestEdits
 * @param {boolean} [premiseCheck]
 * @param {boolean} [stallCheck]
 */
async function launchTask(text, timeBudget, tokenBudget, allowTestEdits, premiseCheck = false, stallCheck = false) {
  if (state.busy) return;
  state.pendingTaskTurn = taskTurn(text);
  followBottom();
  state.busy = true;
  setStatus("working", "starting task");
  try {
    await api(`/api/sessions/${state.sessionId}/task`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text,
        time_budget_s: timeBudget || 0,
        token_budget: tokenBudget || 0,
        allow_test_edits: allowTestEdits,
        premise_check: premiseCheck,
        stall_check: stallCheck,
      }),
    });
  } catch (error) {
    notice(errorText(error), "error");
    setStatus("error");
    state.busy = false;
  }
}
