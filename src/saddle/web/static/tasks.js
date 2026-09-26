"use strict";
/* Task cards: a run of `saddle auto`, started from the chat and read here.

   The card is built for the two moments the Daily Driver page says a
   person is needed: answering a question mid-run, and reading the packet
   at the end. Everything in between is glanceable -- a state, two meters,
   and the ledger's own lines as they are sealed.

   What is evidence and what is not is visible in the styling, not only in
   the words: packet rows carry cites that open the record they came from;
   the model's narrative is set apart, in another face, and labelled
   "narrative, not evidence"; a row with nothing behind it says so. */

const TASK_STATES = {
  running:   { word: "running",   glyph: "●" },
  needs_you: { word: "needs you", glyph: "?" },
  finished:  { word: "finished",  glyph: "✓" },
  stopped:   { word: "stopped",   glyph: "■" },
  failed:    { word: "no outcome", glyph: "!" },
  loading:   { word: "loading",   glyph: "…" },
};
const ENDED = new Set(["finished", "stopped", "failed"]);

const tasks = new Map();

function fmtDuration(seconds) {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, "0")}s`;
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
}

function fmtTokens(n) {
  return n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : String(Math.round(n));
}

function meter(label) {
  const box = el("div", "tmeter");
  box.appendChild(el("span", "tmeter-label", label));
  const track = el("span", "tmeter-track");
  const fill = el("i");
  track.appendChild(fill);
  box.appendChild(track);
  const value = el("span", "tmeter-value", "—");
  box.appendChild(value);
  return { box, fill, value };
}

function setMeter(m, used, limit, text) {
  const pct = limit > 0 ? Math.min(100, (used / limit) * 100) : 0;
  m.fill.style.width = `${pct}%`;
  m.box.classList.toggle("hot", pct >= 80);
  m.value.textContent = text;
}

/* ---------- the card ---------- */

function taskCard(runId, task, turnNode) {
  if (tasks.has(runId)) return tasks.get(runId);
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
  stop.onclick = (event) => { event.preventDefault(); stopTask(runId); };
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

  const ask = el("div", "task-ask");
  ask.hidden = true;
  node.appendChild(ask);

  const packet = el("section", "packet");
  packet.hidden = true;
  node.appendChild(packet);

  (turnNode || state.turnNode || $("#transcript")).appendChild(node);
  const card = {
    runId, task, node, pill, stop, testsChip, time, tokens, now, log, logSummary, lines, ask, packet,
    state: "running", elapsed: 0, elapsedAt: Date.now(), timeBudget: 0, tokenBudget: 0,
    spent: 0, timer: 0, count: 0,
  };
  tasks.set(runId, card);
  paintState(card);
  return card;
}

function paintTests(card, editable) {
  if (editable === null || editable === undefined) return;
  card.testsChip.hidden = false;
  card.testsChip.textContent = editable ? "tests editable" : "tests read-only";
  card.testsChip.classList.toggle("ro", !editable);
}

function paintState(card) {
  const look = TASK_STATES[card.state] || TASK_STATES.running;
  card.node.dataset.state = card.state;
  card.pill.textContent = "";
  card.pill.appendChild(el("b", null, look.glyph));
  card.pill.appendChild(document.createTextNode(` ${look.word}`));
  card.stop.hidden = ENDED.has(card.state) || card.state === "loading";
  card.meters = card.meters || null;
  tick(card);
  clearInterval(card.timer);
  if (card.state === "running") card.timer = setInterval(() => tick(card), 1000);
  if (ENDED.has(card.state)) {
    card.now.textContent = "";
    card.now.hidden = true;
  }
}

function tick(card) {
  // Waiting on you is not charged to the time budget (engine._ask), so the
  // clock is frozen while the card says "needs you".
  const live = card.state === "running" ? (Date.now() - card.elapsedAt) / 1000 : 0;
  const elapsed = card.elapsed + live;
  const budget = card.timeBudget;
  setMeter(card.time, elapsed, budget,
           budget ? `${fmtDuration(elapsed)} of ${fmtDuration(budget)}` : fmtDuration(elapsed));
  setMeter(card.tokens, card.spent, card.tokenBudget,
           card.tokenBudget ? `~${fmtTokens(card.spent)} of ${fmtTokens(card.tokenBudget)}` : "—");
}

function addLine(card, line) {
  const item = el("li", `tl tone-${line.tone || "info"}`);
  item.appendChild(el("span", "tl-mark", line.mark));
  item.appendChild(el("span", "tl-text", line.text));
  item.appendChild(el("span", "tl-cite", line.cite.slice(0, 8)));
  item.title = `ledger record ${line.cite}`;
  card.lines.appendChild(item);
  card.count += 1;
  card.logSummary.textContent = `Session · ${card.count} ledger line${card.count === 1 ? "" : "s"}`;
  card.lines.scrollTop = card.lines.scrollHeight;
}

/* ---------- needs you ---------- */

function showQuestion(card, question) {
  const box = card.ask;
  box.textContent = "";
  if (!question) { box.hidden = true; return; }
  box.hidden = false;
  box.appendChild(el("div", "ask-kicker", "The run is paused until you answer"));
  box.appendChild(el("p", "ask-text", question.text));
  const input = el("textarea", "ask-input");
  input.rows = 2;
  input.placeholder = "Your answer, in words. It is sealed in the ledger.";
  if ((question.options || []).length) {
    const opts = el("div", "ask-options");
    for (const option of question.options) {
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
    if (!text) { note.textContent = "Type or pick an answer first."; return; }
    send.disabled = true;
    try {
      await api(`/api/tasks/${card.runId}/answer`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
      note.textContent = "Sent. Sealing it…";
    } catch (error) {
      note.textContent = String(error.message || error);
      send.disabled = false;
    }
  };
  row.appendChild(send);
  box.appendChild(row);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) send.click();
  });
}

async function stopTask(runId) {
  const card = tasks.get(runId);
  if (card) card.now.textContent = "stopping at the next safe point…";
  try {
    await api(`/api/tasks/${runId}/stop`, { method: "POST" });
  } catch (error) {
    notice(String(error.message || error), "error");
  }
}

/* ---------- the packet ---------- */

const STATUS_WORD = {
  proven: "proven", failed: "failed", observed: "recorded", absent: "no record",
  "not-proven": "not proven", narrative: "narrative, not evidence", cost: "sealed",
};

function citeButton(hash, record, host) {
  const button = el("button", "cite", hash.slice(0, 8));
  button.type = "button";
  button.title = "Open the ledger record this row is drawn from";
  button.onclick = (event) => {
    event.preventDefault();
    const open = host.querySelector(`.record[data-hash="${hash}"]`);
    for (const other of host.querySelectorAll(".record")) other.remove();
    for (const other of host.querySelectorAll(".cite.open")) other.classList.remove("open");
    if (open) return;
    button.classList.add("open");
    const box = el("dl", "record");
    box.dataset.hash = hash;
    for (const [key, value] of Object.entries(record || { hash, note: "not in this packet" })) {
      if (value === "" || value === null || value === undefined) continue;
      box.appendChild(el("dt", null, key));
      box.appendChild(el("dd", null, String(value)));
    }
    host.appendChild(box);
  };
  return button;
}

function narrativeBlock(packet) {
  const quote = el("blockquote", "narrative-text");
  if (!packet.narrative.length) return document.createDocumentFragment();
  for (const sentence of packet.narrative) {
    if (sentence.flagged) {
      const span = el("span", "struck");
      span.appendChild(el("s", null, sentence.text));
      span.appendChild(el("em", "struck-why", "asserts a check result — not evidence"));
      quote.appendChild(span);
    } else {
      quote.appendChild(el("span", null, sentence.text));
    }
    quote.appendChild(document.createTextNode(" "));
  }
  return quote;
}

/* `a command` in a row's text is shown as code; nothing else is parsed. */
function codeSpans(node, text) {
  text.split("`").forEach((part, i) => {
    if (!part) return;
    node.appendChild(i % 2 ? el("code", null, part) : document.createTextNode(part));
  });
  return node;
}

function renderPacket(card, packet) {
  const box = card.packet;
  box.textContent = "";
  box.hidden = false;
  box.appendChild(el("div", "packet-kicker", "Evidence packet · compiled from the ledger"));

  const verdict = el("div", `verdict v-${packet.verdict}`);
  const word = {
    finished: "Finished", stopped: "Stopped", needs_you: "Needs you", unrecorded: "No outcome",
  }[packet.verdict] || packet.verdict;
  const top = el("div", "verdict-top");
  top.appendChild(el("span", "verdict-word", word));
  const audited = packet.rows.some((r) => r.key === "audit" || (r.status === "proven"));
  if (packet.verdict === "finished" && !audited) {
    top.appendChild(el("span", "verdict-tag", "not audited"));
  }
  verdict.appendChild(top);
  if (packet.task) verdict.appendChild(el("p", "verdict-task", packet.task));
  verdict.appendChild(el("p", "verdict-text", packet.verdict_text));
  paintTests(card, packet.test_edits);
  if (packet.offer_test_edits) verdict.appendChild(testEditOffer(card, packet));
  if (packet.header.length) {
    const meta = el("div", "verdict-meta");
    for (const item of packet.header) meta.appendChild(el("span", null, item));
    verdict.appendChild(meta);
  }
  box.appendChild(verdict);

  const rows = el("div", "rows");
  for (const row of packet.rows) {
    const item = el("div", `prow s-${row.status} k-${row.key}`);
    const label = el("div", "prow-label");
    label.appendChild(el("span", "prow-title", row.title));
    label.appendChild(el("span", "prow-status",
      row.key === "narrative" ? packet.narrative_label : (STATUS_WORD[row.status] || row.status)));
    item.appendChild(label);
    const body = el("div", "prow-body");
    if (row.key === "narrative") {
      body.appendChild(narrativeBlock(packet));
      body.appendChild(el("p", "prow-note", row.text));
    } else {
      body.appendChild(codeSpans(el("p", "prow-text"), row.text));
    }
    if (row.items.length) {
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
    rows.appendChild(item);
  }
  box.appendChild(rows);
  // Reading the packet is the point now; the session is one click away.
  if (card.count) card.log.open = false;
}

/* A run that stopped "audit unresolved" with tests read-only, on a finding a
   new test closes, may be right and merely unable to prove it. Say so, and
   offer the same task again with test edits allowed. */
function testEditOffer(card, packet) {
  const box = el("div", "offer");
  box.appendChild(el("p", "offer-text",
    "Tests were read-only, so it could not write the test the auditor asked for. "
    + "The change may be correct; this run cannot show it."));
  const again = el("button", "offer-run primary", "Run again with test edits allowed");
  again.type = "button";
  again.onclick = (event) => {
    event.preventDefault();
    again.disabled = true;
    launchTask(packet.task, card.timeBudget, card.tokenBudget, true);
  };
  box.appendChild(again);
  return box;
}

async function loadPacket(card) {
  try {
    const packet = await api(`/api/sessions/${state.sessionId}/tasks/${card.runId}/packet`);
    renderPacket(card, packet);
    const was = atBottom();
    stickToBottom(was);
  } catch (error) {
    card.packet.hidden = false;
    card.packet.textContent = "";
    card.packet.appendChild(el("p", "notice error", `could not load the packet: ${error.message || error}`));
  }
}

/* ---------- events ---------- */

function handleTask(event) {
  switch (event.kind) {
    case "task.state": {
      let host = null;
      if (!tasks.has(event.run_id)) {
        // The turn startTask made for it, or -- a reload mid-run -- a new
        // one with the task's bubble, so the card never appears unexplained.
        host = state.pendingTaskTurn || taskTurn(event.task);
        state.pendingTaskTurn = null;
      }
      const card = taskCard(event.run_id, event.task, host);
      const was = card.state;
      if (event.time_budget_s) card.timeBudget = event.time_budget_s;
      if (event.token_budget) card.tokenBudget = event.token_budget;
      paintTests(card, event.test_edits);
      if (was === "running" && event.state !== "running") {
        card.elapsed += (Date.now() - card.elapsedAt) / 1000;
      }
      if (event.state === "running" && was !== "running") card.elapsedAt = Date.now();
      card.state = event.state;
      paintState(card);
      showQuestion(card, event.state === "needs_you" ? event.question : null);
      state.activeTask = ENDED.has(event.state) ? null : event.run_id;
      if (event.state === "needs_you") {
        setStatus("needs", "needs you");
      } else if (event.state === "running") {
        setStatus("working", "task running");
      }
      if (ENDED.has(event.state)) {
        if (event.detail && event.state === "failed") card.now.textContent = event.detail;
        loadPacket(card);
      }
      return true;
    }
    case "task.line": {
      const card = tasks.get(event.run_id);
      if (card) addLine(card, event);
      return true;
    }
    case "task.event": {
      const card = tasks.get(event.run_id);
      if (!card) return true;
      const inner = event.event || {};
      if (inner.kind === "run.progress") {
        card.elapsed = inner.elapsed_s;
        card.elapsedAt = Date.now();
        card.spent = inner.tokens;
        card.timeBudget = inner.time_budget_s;
        card.tokenBudget = inner.token_budget;
        tick(card);
      } else if (inner.kind === "tool.start") {
        card.now.textContent = `${inner.present}…`;
      } else if (inner.kind === "reasoning.delta") {
        if (!card.now.textContent.startsWith("thinking")) card.now.textContent = "thinking…";
      } else if (inner.kind === "tool.end" || inner.kind === "turn.end") {
        card.now.textContent = "";
      }
      return true;
    }
    default:
      return false;
  }
}

function taskBubble(task) {
  const bubble = el("div", "user task-ask-bubble");
  bubble.appendChild(el("span", "task-badge", "▶ task"));
  bubble.appendChild(document.createTextNode(task));
  return bubble;
}

function taskTurn(task) {
  for (const empty of document.querySelectorAll("#transcript .empty")) empty.remove();
  const turn = newTurn(null);
  turn.appendChild(taskBubble(task));
  return turn;
}

/* A run's recap is stored as a message so the chat's model sees it (T5-9).
   On reload, that message is drawn as the card it stands for. */
const RECAP = /^\[saddle task ([0-9a-f]+)\] ([^\n]*)/;

function recapCard(turn, content) {
  const match = RECAP.exec(typeof content === "string" ? content : "");
  if (!match) return false;
  const [, runId, task] = match;
  turn.appendChild(taskBubble(task));
  const card = taskCard(runId, task, turn);
  card.state = "loading";
  card.log.hidden = true;
  card.node.querySelector(".task-meters").hidden = true;
  paintState(card);
  loadPacket(card).then(() => {
    const verdict = card.packet.querySelector(".verdict");
    const cls = verdict ? [...verdict.classList].find((c) => c.startsWith("v-")) : null;
    card.state = cls ? { "v-finished": "finished", "v-stopped": "stopped" }[cls] || "failed" : "failed";
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
  api("/api/task-policy").then((policy) => {
    $("#tc-test-edits").checked = !!policy.test_edits;
    paintTestPolicy();
  }).catch(() => {});
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
  // folder, lane, budgets and test policy are shown before a run.
  if (!text || state.busy || $("#task-confirm").hidden) return;
  const minutes = Number($("#tc-time").value) || 30;
  const thousands = Number($("#tc-tokens").value) || 100;
  closeRunConfirm();
  input.value = "";
  input.style.height = "auto";
  await launchTask(text, Math.round(minutes * 60), Math.round(thousands * 1000),
                   $("#tc-test-edits").checked);
}

async function launchTask(text, timeBudget, tokenBudget, allowTestEdits) {
  if (state.busy) return;
  state.pendingTaskTurn = taskTurn(text);
  followBottom();
  state.busy = true;
  setStatus("working", "starting task");
  try {
    await api(`/api/sessions/${state.sessionId}/task`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text, time_budget_s: timeBudget || 1800, token_budget: tokenBudget || 100000,
        allow_test_edits: allowTestEdits,
      }),
    });
  } catch (error) {
    notice(String(error.message || error), "error");
    setStatus("error");
    state.busy = false;
  }
}
