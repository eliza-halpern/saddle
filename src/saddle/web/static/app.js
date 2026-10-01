"use strict";
/* The transcript is built from the same event stream the terminal renderer
   consumes, so the two cannot disagree about what happened.

   Two behaviours are worth naming because they are the ones that make a
   long turn readable:

   - Reasoning opens itself while it streams and closes itself once the
     answer starts. You see the model think without having to click, and you
     are not left scrolling past it afterwards. It is never truncated:
     collapsing hides text, it does not discard it.
   - A tool row carries one label that changes tense with its state --
     "Running pytest -q" while it runs, "Ran pytest -q" when it succeeds,
     "Failed to run pytest -q" when it does not. The label comes from the
     server so the terminal and the browser say the same words. */

const $ = (sel) => document.querySelector(sel);

const state = {
  sessionId: null, stream: null, busy: false,
  turnNode: null, assistantNode: null, reasoningNode: null, reasoningBody: null,
  tools: new Map(), terminals: new Map(), attachments: [], personas: {}, folder: null, mode: "ask",
};



const painting = new Set();
let paintFrame = 0;

function flushPaint() {
  if (paintFrame) { cancelAnimationFrame(paintFrame); paintFrame = 0; }
  if (!painting.size) return;
  const was = atBottom();
  for (const node of painting) {
    if (node.dataset.reasoning) {
      node.appendChild(document.createTextNode(node.dataset.pending || ""));
      node.dataset.pending = "";
    } else {
      paintStream(node);
    }
  }
  painting.clear();
  stickToBottom(was);
}

function schedulePaint(node) {
  painting.add(node);
  if (paintFrame) return;
  paintFrame = requestAnimationFrame(() => { paintFrame = 0; flushPaint(); });
}

/* ---------- transcript pieces ---------- */

function newTurn(prompt) {
  const turn = el("div", "turn");
  if (prompt) turn.appendChild(el("div", "user", prompt));
  $("#transcript").appendChild(turn);
  state.turnNode = turn;
  state.assistantNode = null;
  state.reasoningNode = null;
  return turn;
}

function reasoningBlock() {
  if (state.reasoningNode) return state.reasoningNode;
  const details = el("details", "reasoning streaming");
  const summary = el("summary");
  summary.appendChild(el("span", null, "Thinking"));
  details.appendChild(summary);
  const body = el("div", "reasoning-body");
  details.appendChild(body);
  details.open = true;                      // opens itself while it streams
  details.dataset.started = String(Date.now());
  state.turnNode.appendChild(details);
  state.reasoningNode = details;
  state.reasoningBody = body;
  return details;
}

/* Time, not character count: "thought for 12s" is what a reader is actually
   judging -- how long they waited -- and it stays meaningful whether the
   model is terse or verbose. */
function foldReasoning() {
  const node = state.reasoningNode;
  if (!node) return;
  node.open = false;
  node.classList.remove("streaming");
  const started = Number(node.dataset.started || 0);
  const seconds = started ? (Date.now() - started) / 1000 : 0;
  const shown =
    seconds >= 60
      ? `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`
      : `${seconds < 10 ? seconds.toFixed(1) : Math.round(seconds)}s`;
  node.querySelector("summary span").textContent = `Thought for ${shown}`;
}

function assistantBlock() {
  if (state.assistantNode) return state.assistantNode;
  if (state.reasoningNode) foldReasoning();  // the answer began: fold it away
  const node = el("div", "assistant");
  node.dataset.raw = "";
  state.turnNode.appendChild(node);
  state.assistantNode = node;
  return node;
}

function toolRow(event) {
  const details = el("details", "tool running");
  const summary = el("summary");
  summary.appendChild(el("span", "dot"));
  const label = el("span", "label", event.present);   // present tense, while running
  summary.appendChild(label);
  summary.appendChild(el("span", "ms", ""));
  details.appendChild(summary);
  const detail = el("div", "tool-detail");
  if (event.name === "run_command") detail.classList.add("terminal");
  detail.textContent = "…";
  details.appendChild(detail);
  state.turnNode.appendChild(details);
  state.tools.set(event.id, { details, label, detail, name: event.name });
  return details;
}

function finishTool(event) {
  const row = state.tools.get(event.id);
  if (!row) return;
  row.details.classList.remove("running");
  row.details.classList.add(event.ok ? "ok" : "failed");
  row.label.textContent = event.label;               // -> past tense, or "Failed to …"
  row.details.querySelector(".ms").textContent =
    event.duration_ms >= 1000
      ? `${(event.duration_ms / 1000).toFixed(1)}s`
      : `${event.duration_ms}ms`;
  fillToolDetail(row.details, row.detail, event.detail);
  markUnsandboxed(row.details, event.detail);
  if (event.preview) showPreview(row.details, event.preview, event.version);
  if (!event.ok) row.details.open = true;             // a failure should not need a click
}

/* A background command keeps producing output after the tool call that
   started it returned, so it gets its own block that fills as it runs
   rather than appearing all at once when someone reads it. */
function terminalBlock(id) {
  if (state.terminals.has(id)) return state.terminals.get(id);
  const details = el("details", "tool running");
  const summary = el("summary");
  summary.appendChild(el("span", "dot"));
  summary.appendChild(el("span", "label", `Terminal ${id}`));
  summary.appendChild(el("span", "ms", "live"));
  details.appendChild(summary);
  // Output events carry no label, so the session's setting marks the block.
  // A sandboxed command still streaming when access is turned on reads as
  // unsandboxed: wrong in the safe direction only.
  if (state.fullAccess) badgeUnsandboxed(details);
  const body = el("div", "tool-detail terminal");
  details.appendChild(body);
  details.open = true;
  (state.turnNode || $("#transcript")).appendChild(details);
  state.terminals.set(id, body);
  return body;
}

function notice(text, kind) {
  const node = el("div", `notice ${kind || ""}`, text);
  ($("#transcript").lastElementChild || $("#transcript")).appendChild(node);
}

/* ---------- event stream ---------- */

function handle(event) {
  const was = atBottom();
  switch (event.kind) {
    case "session.info":
      renderHistory(event);
      break;
    case "session.title":
      // The first turn named its session. Take it only if the user is not
      // mid-rename: stealing focus mid-edit would discard what they typed.
      if (document.activeElement !== $("#title")) $("#title").value = event.title;
      loadSessions();
      break;
    case "turn.start":
      newTurn(null);
      break;
    case "reasoning.delta":
      reasoningBlock();
      // Appending a text node per frame, not `textContent +=`, which
      // re-reads and rewrites the whole transcript of the thought.
      state.reasoningBody.dataset.reasoning = "1";
      state.reasoningBody.dataset.pending =
        (state.reasoningBody.dataset.pending || "") + event.text;   // never truncated
      schedulePaint(state.reasoningBody);
      break;
    case "content.delta": {
      const node = assistantBlock();
      node.dataset.raw += event.text;
      schedulePaint(node);
      break;
    }
    case "tool.start":
      if (!state.turnNode) newTurn(null);
      // A turn is rounds of "think, say something, call a tool" and the
      // blocks are cached by round. Without releasing them here, the text
      // of round two was appended into round one's block -- which sits
      // above the tool rows -- so the answer appeared before the tools that
      // produced it. It read correctly again on reload, because history is
      // rebuilt in stored order, which is what made it look cosmetic.
      state.assistantNode = null;
      state.reasoningNode = null;
      toolRow(event);
      setStatus("working", event.present.toLowerCase());
      break;
    case "tool.end":
      finishTool(event);
      break;
    case "password.request":
      askPassword(event);
      break;
    case "password.done":
      if (state.passwordId === event.id) closePassword();
      break;
    case "terminal.output": {
      // Append text nodes; the raw text rides along so the button in the
      // row's header copies what the command said, not what the DOM holds.
      const body = terminalBlock(event.id);
      if (!body.output) {
        attachCopy(body.parentNode.querySelector("summary"),
          () => body.output || "", "terminal output");
      }
      body.output = (body.output || "") + event.chunk;
      body.appendChild(document.createTextNode(event.chunk));
      break;
    }
    case "context": {
      const pct = Math.min(100, Math.round((event.used / event.limit) * 100));
      const meter = $("#meter");
      meter.querySelector("i").style.width = `${pct}%`;
      meter.querySelector("b").textContent =
        `${Math.round(event.used / 1000)}k / ${Math.round(event.limit / 1000)}k`;
      meter.classList.toggle("full", pct > 80);
      break;
    }
    case "compaction":
      notice(`Context compacted — ${event.summary}`);
      break;
    case "error":
      notice(event.message, "error");
      setStatus("error");
      break;
    case "turn.end":
      flushPaint();
      foldReasoning();                       // a turn with no answer text still folds
      setStatus("idle");
      state.busy = false;
      break;
    case "idle":
      flushPaint();
      restampTurns();
      setStatus("idle");
      state.busy = false;
      state.activeTask = null;
      break;
    default:
      handleTask(event);        // task.* events: the task card (tasks.js)
  }
  stickToBottom(was);
}

function renderHistory(info) {
  $("#title").value = info.title;
  $("#folder-name").textContent = info.workdir.split("/").slice(-2).join("/") || info.workdir;
  state.folder = info.workdir;
  $("#persona").value = info.persona;
  if (info.reasoning_effort) $("#effort").value = info.reasoning_effort;
  if (info.temperature !== undefined) showTemperature(info.temperature);
  showMode(info.mode);
  showFullAccess(info.full_access);
  showWhere(info.workdir, info.branch);
  // Show the meter on load, not only after the next turn ends.
  if (info.context_limit) {
    handle({ kind: "context", used: info.context_used || 0, limit: info.context_limit });
  }
  // An EventSource reconnects on its own, and every connect re-sends
  // session.info. Rebuilding on each one deleted the reasoning blocks and
  // tool rows the reader had just watched stream in -- they are not part of
  // a reload's history in the same shape, so they simply vanished. Rebuild
  // only when this session's transcript is not already on screen.
  if (state.historyFor === info.session_id) return;
  state.historyFor = info.session_id;
  const t = $("#transcript");
  t.textContent = "";
  const shown = (info.messages || []).filter(
    (m) => m.role === "user" || m.role === "assistant");
  if (!shown.length) {
    t.appendChild(el("div", "empty", "nothing here yet — what are we making?"));
    return;
  }
  for (const message of shown) {
    const turn = el("div", "turn");
    if (message.role === "user" && recapCard(turn, message.content)) {
      t.appendChild(turn);
      continue;
    }
    if (message.role === "user") {
      turn.dataset.index = String(message.index);
      turn.appendChild(turnTools(message.index));
      // Content is a string, or a list of parts when images were attached.
      // Rendering the list with `|| ""` printed "[object Object]".
      const box = el("div", "user");
      if (Array.isArray(message.content)) {
        const shots = el("div", "shots");
        for (const part of message.content) {
          if (part.type === "text") {
            box.appendChild(document.createTextNode(part.text || ""));
          } else if (part.type === "image_url" && part.image_url?.url) {
            const img = el("img", "shot");
            img.src = part.image_url.url;
            shots.appendChild(img);
          }
        }
        if (shots.childElementCount) box.appendChild(shots);
      } else {
        box.textContent = message.content || "";
      }
      turn.appendChild(box);
    } else {
      pastToolRows(turn, message.tools || []);
      if (message.content) {
        const node = el("div", "assistant");
        renderMarkdown(node, message.content);
        turn.appendChild(node);
      }
    }
    t.appendChild(turn);
  }
  followBottom();          // a freshly opened session starts at the end
}

function showPreview(after, path, version) {
  // A picture the model just drew belongs in the transcript, not behind a
  // filename the reader has to go and open somewhere else.
  const figure = el("figure", "preview");
  const img = el("img");
  // The version this message produced, not whatever the file says now: a
  // frog drawn in one message and edited in a later one showed the edited
  // frog in both, and the conversation stopped making sense.
  const base = `/api/sessions/${state.sessionId}/file?path=${encodeURIComponent(path)}`;
  img.src = version ? `${base}&v=${encodeURIComponent(version)}` : base;
  img.alt = path;
  img.loading = "lazy";
  // A file that will not load says so, rather than leaving a broken icon.
  img.onerror = () => {
    figure.textContent = "";
    figure.appendChild(el("div", "preview-failed", `could not display ${path}`));
  };
  figure.appendChild(img);
  figure.appendChild(el("figcaption", null, path));
  after.insertAdjacentElement("afterend", figure);
}

function pastToolRow(call) {
  // The same row the live stream produced, in its settled state: the label
  // is already in the tense the outcome calls for, because the server wrote
  // it with the outcome in hand.
  const details = el("details", `tool ${call.ok ? "ok" : "failed"}`);
  const summary = el("summary");
  summary.appendChild(el("span", "dot"));
  summary.appendChild(el("span", "label", call.label));
  details.appendChild(summary);
  const detail = el("div", "tool-detail");
  if (call.name === "run_command") detail.classList.add("terminal");
  // The same filling the live row gets: a diff was coloured while you
  // watched it and turned back into flat text on reload, because history
  // set textContent directly.
  fillToolDetail(details, detail, call.detail);
  markUnsandboxed(details, call.detail);
  details.appendChild(detail);
  return details;
}

function pastToolRows(turn, calls) {
  for (const call of calls) {
    const row = pastToolRow(call);
    turn.appendChild(row);
    if (call.preview) showPreview(row, call.preview, call.version);
  }
}

/* ---------- retry and edit ---------- */

async function restampTurns() {
  // A live turn does not know its own index -- that only exists once the
  // message is stored -- so after a turn settles the transcript is matched
  // against the store, in order, and the retry/edit buttons appear on the
  // turn that just finished as well as on the older ones.
  let stored;
  try {
    stored = await api(`/api/sessions/${state.sessionId}/messages`);
  } catch {
    return;                       // the buttons are a convenience, not the turn
  }
  const asked = stored
    .map((message, index) => (message.role === "user" ? index : -1))
    .filter((index) => index >= 0);
  // Only the turns that carry a question: an assistant reply gets its own
  // .turn node, so counting all of them never matched and the restamp
  // silently never ran for a live turn.
  const turns = [...$("#transcript").querySelectorAll(".turn")].filter(
    (turn) => turn.querySelector(".user"));
  if (turns.length !== asked.length) return;   // mid-stream; try again next idle
  turns.forEach((turn, position) => {
    turn.dataset.index = String(asked[position]);
    // A task's bubble stands for a run, not a question: no retry on it.
    if (!turn.querySelector(".turn-tools") && !turn.querySelector(".task-ask-bubble")) {
      turn.insertBefore(turnTools(asked[position]), turn.firstChild);
    }
  });
}

function turnTools(index) {
  const tools = el("div", "turn-tools");
  for (const [glyph, title, edit] of [
    ["↻", "Run this again", false],
    ["✎", "Edit and run again", true],
  ]) {
    const button = el("button", null, glyph);
    button.type = "button";
    button.title = title;
    button.onclick = (event) => { event.preventDefault(); openRewind(index, edit); };
    tools.appendChild(button);
  }
  return tools;
}

async function openRewind(index, editing) {
  // Ask the server what this would change *before* offering the button, so
  // the warning is the real list of files rather than a guess.
  let preview;
  try {
    preview = await api(`/api/sessions/${state.sessionId}/rewind?index=${index}`);
  } catch (error) {
    notice(String(error.message || error), "error");
    return;
  }
  state.rewind = { index, editing };
  $("#rewind-title").textContent = editing ? "Edit and run again" : "Run this again";
  $("#rewind-hint").textContent = editing
    ? "The answer below is replaced by a new one."
    : "The same question, answered again. Sampling makes it come out differently.";
  const box = $("#rewind-text");
  box.hidden = !editing;
  box.value = preview.text || "";

  const files = [...(preview.reverted || []), ...(preview.deleted || [])];
  const warning = $("#rewind-warning");
  warning.hidden = files.length === 0;
  const list = $("#rewind-files");
  list.textContent = "";
  for (const path of preview.reverted || []) {
    list.appendChild(el("li", null, `${path} — put back`));
  }
  for (const path of preview.deleted || []) {
    list.appendChild(el("li", null, `${path} — deleted (it did not exist before)`));
  }
  $("#rewind-dialog").showModal();
  if (editing) box.focus();
}

$("#rewind-cancel").onclick = (event) => {
  event.preventDefault();
  $("#rewind-dialog").close();
};
$("#rewind-go").onclick = async (event) => {
  event.preventDefault();
  const { index, editing } = state.rewind || {};
  if (index === undefined) return;
  const body = { index };
  if (editing) body.text = $("#rewind-text").value;
  $("#rewind-dialog").close();

  // Drop the turns being replaced before the new one streams in, so the
  // transcript never shows both attempts at once.
  for (const node of [...$("#transcript").children]) {
    const at = Number(node.dataset.index);
    if (!Number.isNaN(at) && at >= index) node.remove();
  }
  state.assistantNode = null;
  state.reasoningNode = null;

  let result;
  try {
    result = await api(`/api/sessions/${state.sessionId}/rewind`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (error) {
    notice(String(error.message || error), "error");
    return;
  }
  state.busy = true;
  setStatus("working");
  const changed = [...(result.reverted || []), ...(result.deleted || [])];
  if (changed.length) {
    notice(`Put back ${changed.length} file(s): ${changed.join(", ")}`, "warn");
  }
  for (const path of result.failed || []) {
    notice(`Could not restore ${path}`, "error");
  }
};

function connect(sessionId) {
  if (state.stream) state.stream.close();
  state.stream = new EventSource(`/api/sessions/${sessionId}/events`);
  state.stream.onmessage = (message) => handle(JSON.parse(message.data));
  state.stream.onerror = () => setStatus("error");
}

function setStatus(kind, detail) {
  const node = $("#status");
  node.className = `status ${kind === "idle" ? "idle" : kind}`;
  node.textContent = detail || kind;
  // While a turn runs the send button stops it: one control, two jobs, so
  // the thing you reach for is always under the cursor you just used.
  const sendButton = $("#send");
  const working = kind === "working" || kind === "needs";
  sendButton.textContent = working ? "■" : "↑";
  sendButton.title = working ? "Stop" : "Send";
  sendButton.classList.toggle("stopping", working);
}

/* ---------- sessions ---------- */

async function api(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) throw new Error((await response.json()).error || response.statusText);
  return response.json();
}

async function loadSessions() {
  const sessions = await api("/api/sessions");
  const list = $("#session-list");
  list.textContent = "";
  for (const session of sessions) {
    const row = el("div", `session${session.id === state.sessionId ? " active" : ""}`);
    row.dataset.sid = session.id;
    row.appendChild(el("span", "name", session.title));
    const kill = el("button", "kill", "×");
    kill.title = "Delete session";
    kill.type = "button";
    kill.setAttribute("aria-label", `Delete session ${session.title}`);
    kill.onclick = async (event) => {
      event.stopPropagation();
      await deleteSession(session);  // runs.js: hidden now, gone after the undo toast
    };
    row.appendChild(kill);
    row.onclick = () => select(session.id);
    list.appendChild(row);
  }
  noteSessions(sessions);
  loadRuns();
  return sessions;
}

function select(sessionId) {
  drawer(false);
  state.sessionId = sessionId;
  state.historyFor = null;          // a different transcript: rebuild it
  state.tools.clear();
  state.terminals.clear();
  localStorage.setItem("saddle.session", sessionId);
  // Busy, the live task and the header pill belong to the session on
  // screen: the new session's stream replays its own live run (app.events),
  // so nothing of the last one -- not its stop button -- is carried over.
  state.busy = false;
  state.activeTask = null;
  setStatus("idle");
  runSelected(sessionId);
  loadSessions();
  connect(sessionId);
}

async function boot() {
  await loadPersonas();
  let sessions = await loadSessions();
  if (!sessions.length) {
    await api("/api/sessions", { method: "POST", headers: { "Content-Type": "application/json" },
                                 body: JSON.stringify({}) });
    sessions = await loadSessions();
  }
  const remembered = localStorage.getItem("saddle.session");
  select(sessions.some((s) => s.id === remembered) ? remembered : sessions[0].id);
}

/* ---------- composer ---------- */

/* The lane, per session: what Enter does. The words are what the code does
   -- an Ask turn is offered read-only tools and the server refuses any other
   call; an Edit turn runs the file and shell tools in the folder itself, with
   no auditor; a task runs `saddle auto` in a worktree and ends in a packet.
   Feature, Breadth and Long are shown so the shape is visible, and cannot be
   chosen: `enabled` is false and nothing below selects a disabled lane. */
const LANES = [
  { id: "ask", enabled: true, label: "Ask",
    desc: "Ask: read-only. It reads and searches your folder, then answers. It cannot edit files or run commands.",
    placeholder: "ask about this folder (read-only, nothing changes)" },
  { id: "edit", enabled: true, label: "Edit",
    desc: "Edit: unaudited, edits your folder. It writes files and runs commands directly; nothing checks the work and there is no packet.",
    placeholder: "this will edit your folder directly, unaudited" },
  { id: "task", enabled: true, label: "Task · Small",
    desc: "Task · Small: an audited run on a new branch in a copy of your folder, ending in an evidence packet. Enter shows the run before it starts.",
    placeholder: "describe the job; Enter shows the run before it starts" },
  { id: "feature", enabled: false, label: "Feature" },
  { id: "breadth", enabled: false, label: "Breadth" },
  { id: "long", enabled: false, label: "Long" },
];
const laneOf = (id) => LANES.find((lane) => lane.id === id && lane.enabled);
// Kept by name for the code that already reads it (tasks.js, older tests).
const MODES = Object.fromEntries(LANES.filter((l) => l.enabled).map((l) => [l.id, l]));

function showMode(mode) {
  const lane = laneOf(mode) || laneOf("ask");
  state.mode = lane.id;
  $("#lane-name").textContent = lane.label;
  $("#lane-chip").dataset.lane = lane.id;
  for (const option of document.querySelectorAll("#lane-menu li")) {
    option.setAttribute("aria-selected", String(option.dataset.lane === lane.id));
  }
  $("#mode-desc").textContent = lane.desc;
  $("#mode-chip").textContent = lane.id;
  $("#mode-chip").dataset.mode = lane.id;
  document.body.dataset.mode = lane.id;  // page-level hook: task mode quiets the chat-only chrome
  $("#input").placeholder = lane.placeholder;
  $("#mode-note").hidden = true;
  paintFullAccess();
  if (lane.id !== "task") closeRunConfirm();
  paintSuggestion();
}

async function setMode(mode) {
  if (!laneOf(mode)) return;          // a disabled or unknown lane is never chosen
  showMode(mode);
  $("#input").focus();
  try {
    const session = await api(`/api/sessions/${state.sessionId}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode: state.mode }),
    });
    showFullAccess(session.full_access);   // leaving Edit ends full access
  } catch (error) {
    notice(String(error.message || error), "error");
  }
}

/* ---------- full access (#124) ----------
   A session's Edit commands can run outside the sandbox, only after the
   dialog's explicit yes; its default answer is No ("Keep sandboxed" has the
   focus, and Enter or Escape keeps it). While it is on, the topbar says so
   on every screen and each command it ran carries a badge. */
function paintFullAccess() {
  const on = Boolean(state.fullAccess);
  $("#full-access-banner").hidden = !on;
  $("#full-access-open").hidden = on || state.mode !== "edit";
  document.body.dataset.fullAccess = on ? "on" : "off";
}

function showFullAccess(on) {
  state.fullAccess = Boolean(on);
  paintFullAccess();
}

async function setFullAccess(on) {
  const body = on ? { on: true, confirm: $("#fa-grant").dataset.confirm } : { on: false };
  try {
    const session = await api(`/api/sessions/${state.sessionId}/full-access`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    showFullAccess(session.full_access);
  } catch (error) {
    notice(String(error.message || error), "error");
  }
}

/* The server puts this on the first line of every command result while full
   access is on (`tools.UNSANDBOXED`); the badge sits on the summary, so it
   shows while the row is folded, live and after a reload alike. */
const UNSANDBOXED_MARK = "[UNSANDBOXED:";
function badgeUnsandboxed(details) {
  details.classList.add("unsandboxed");
  const summary = details.querySelector("summary");
  if (summary.querySelector(".unsandboxed-badge")) return;
  const badge = el("span", "unsandboxed-badge", "unsandboxed");
  badge.title = "Full access was on: this command ran as you, outside the sandbox";
  summary.insertBefore(badge, summary.querySelector(".label"));
}

function markUnsandboxed(details, text) {
  if (typeof text === "string" && text.startsWith(UNSANDBOXED_MARK)) badgeUnsandboxed(details);
}

/* A full-access command's sudo asks for a password (#125). What is typed
   goes to that command only: posted once, then the field is cleared. */
function askPassword(event) {
  state.passwordId = event.id;
  $("#pw-prompt").textContent = event.prompt || "password:";
  $("#pw-input").value = "";
  if (!$("#password-dialog").open) $("#password-dialog").showModal();
  $("#pw-input").focus();
}

function closePassword() {
  $("#pw-input").value = "";
  state.passwordId = null;
  if ($("#password-dialog").open) $("#password-dialog").close();
}

async function answerPassword(cancel) {
  const id = state.passwordId;
  if (!id) return;
  const body = cancel ? { id, cancel: true } : { id, password: $("#pw-input").value };
  closePassword();
  try {
    await api(`/api/sessions/${state.sessionId}/password`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
  } catch (error) {
    notice(String(error.message || error), "error");
  }
}

$("#pw-send").onclick = () => answerPassword(false);
$("#pw-cancel").onclick = () => answerPassword(true);
$("#pw-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter") { event.preventDefault(); answerPassword(false); }
});
$("#password-dialog").addEventListener("cancel", (event) => {   // Escape
  event.preventDefault();
  answerPassword(true);
});

$("#full-access-open").onclick = () => $("#full-access-dialog").showModal();
$("#fa-keep").onclick = () => $("#full-access-dialog").close();
$("#fa-grant").onclick = () => { $("#full-access-dialog").close(); setFullAccess(true); };
$("#full-access-off").onclick = () => setFullAccess(false);

/* Shift+Tab: the next enabled lane, wrapping. */
function nextLane(from, step = 1) {
  const usable = LANES.filter((lane) => lane.enabled);
  const at = usable.findIndex((lane) => lane.id === from);
  return usable[(at + step + usable.length) % usable.length].id;
}

/* A guess from the words alone, shown as a hint and never acted on: only
   Tab (the user) takes it. Choosing the lane silently is how "ran a task by
   accident" comes back. */
const TASK_VERB = /^(please\s+)?(make|fix|add|remove|rename|refactor|change|implement|write|update|delete|move|replace|convert|split)\b/i;
const FILE_NAME = /\b[\w./-]+\.(py|js|mjs|ts|tsx|md|json|toml|css|html|rs|go|c|h|java|ya?ml|txt|sh)\b/i;
function suggestLane(text) {
  const t = text.trim();
  if (!t) return null;
  if (t.endsWith("?")) return "ask";
  if (TASK_VERB.test(t) && FILE_NAME.test(t)) return "task";
  return null;
}
function paintSuggestion() {
  const box = $("#lane-suggest");
  const want = suggestLane($("#input").value);
  state.suggestion = want && want !== state.mode ? want : null;
  box.hidden = !state.suggestion;
  box.textContent = state.suggestion === "task" ? "looks like a task → Tab"
    : state.suggestion === "ask" ? "looks like a question → Tab for Ask" : "";
}

function openLaneMenu(open) {
  const menu = $("#lane-menu");
  menu.hidden = !open;
  $("#lane-chip").setAttribute("aria-expanded", String(open));
  if (open) {
    for (const option of menu.querySelectorAll("li")) {
      option.classList.toggle("active", option.dataset.lane === state.mode);
    }
    menu.focus();
  }
}
function moveLaneFocus(step) {
  const options = [...document.querySelectorAll('#lane-menu li:not([aria-disabled="true"])')];
  const at = options.findIndex((o) => o.classList.contains("active"));
  const next = options[(at + step + options.length) % options.length];
  for (const o of options) o.classList.toggle("active", o === next);
}

function showWhere(workdir, branch) {
  $("#where-folder").textContent = (workdir || "").split("/").filter(Boolean).pop() || workdir || "";
  $("#where-folder").title = workdir || "";
  $("#where-branch").textContent = branch || "";
}

function submitComposer() {
  if (!$("#lane-menu").hidden) openLaneMenu(false);
  if (state.mode === "task") openRunConfirm();
  else send();
}

async function send() {
  const input = $("#input");
  const text = input.value.trim();
  if (!text || state.busy) return;
  // Images ride along as real content parts the model can look at; other
  // files are named so it knows to read them from uploads/.
  const images = state.attachments.filter((a) => a.isImage).map((a) => a.path);
  const others = state.attachments.filter((a) => !a.isImage).map((a) => a.name);
  const body = others.length
    ? `${text}\n\n[also attached in uploads/: ${others.join(", ")}]`
    : text;
  newTurn(input.value.trim());
  followBottom();          // sending is an intent to watch the reply
  input.value = "";
  paintSuggestion();
  input.style.height = "auto";
  state.attachments = [];
  $("#attachments").textContent = "";
  state.busy = true;
  setStatus("working");
  try {
    await api(`/api/sessions/${state.sessionId}/message`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: body, images }),
    });
  } catch (error) {
    notice(String(error.message || error), "error");
    setStatus("error");
    state.busy = false;
  }
}

async function upload(files) {
  const form = new FormData();
  for (const file of files) form.append("files", file);
  const result = await api(`/api/sessions/${state.sessionId}/upload`, { method: "POST", body: form });
  for (const saved of result.saved) {
    const file = [...files].find((f) => f.name === saved.name);
    saved.isImage = Boolean(file && file.type.startsWith("image/"));
    state.attachments.push(saved);
    const chip = el("span", "chip");
    if (saved.isImage) {
      const img = el("img");
      img.src = URL.createObjectURL(file);
      chip.appendChild(img);
    }
    chip.appendChild(el("span", null, saved.name));
    const remove = el("button", null, "×");
    remove.onclick = () => {
      state.attachments = state.attachments.filter((a) => a.name !== saved.name);
      chip.remove();
    };
    chip.appendChild(remove);
    $("#attachments").appendChild(chip);
  }
}

/* ---------- folder picker ---------- */

async function openFolders(path) {
  const data = await api(`/api/browse?path=${encodeURIComponent(path || state.folder || "")}`);
  $("#folder-path").textContent = data.path;
  $("#folder-dialog").dataset.path = data.path;
  $("#folder-dialog").dataset.parent = data.parent;
  const list = $("#folder-entries");
  list.textContent = "";
  for (const entry of data.entries) {
    const button = el("button", null, entry.name + "/");
    button.onclick = (event) => { event.preventDefault(); openFolders(entry.path); };
    list.appendChild(button);
  }
  $("#folder-error").textContent = "";
  if (!$("#folder-dialog").open) $("#folder-dialog").showModal();
}

async function makeFolder() {
  const name = $("#folder-name-new").value.trim();
  const here = $("#folder-dialog").dataset.path;
  if (!name) { $("#folder-error").textContent = "give the folder a name"; return; }
  const reply = await fetch("/api/browse", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path: here, name }),
  });
  const body = await reply.json();
  if (!reply.ok) { $("#folder-error").textContent = body.error || "could not create"; return; }
  $("#folder-name-new").value = "";
  // Step into it: making a folder here almost always means working in it.
  await openFolders(body.path);
}

$("#folder-make").onclick = (event) => { event.preventDefault(); makeFolder(); };
$("#folder-name-new").addEventListener("keydown", (event) => {
  if (event.key === "Enter") { event.preventDefault(); makeFolder(); }
});

/* ---------- wiring ---------- */

$("#composer").addEventListener("submit", (event) => {
  event.preventDefault();
  if (state.busy && state.activeTask) {
    stopTask(state.activeTask);
    return;
  }
  if (state.busy) {
    fetch(`/api/sessions/${state.sessionId}/stop`, { method: "POST" });
    setStatus("working", "stopping…");
    return;
  }
  submitComposer();
});
$("#input").addEventListener("keydown", (event) => {
  if (event.key === "Tab" && event.shiftKey) {
    event.preventDefault();
    setMode(nextLane(state.mode));
    return;
  }
  if (event.key === "Tab" && !event.shiftKey && state.suggestion) {
    event.preventDefault();         // the user took the hint; nothing else does
    setMode(state.suggestion);
    return;
  }
  if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); submitComposer(); }
  if (event.key === "Escape" && !$("#task-confirm").hidden) { event.preventDefault(); closeRunConfirm(); }
});
$("#task-confirm").addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    event.preventDefault();
    closeRunConfirm();
    $("#input").focus();
  } else if (event.key === "Enter" && !event.shiftKey && event.target.id !== "tc-cancel") {
    // Enter or Ctrl+Enter anywhere in the strip starts; Cancel keeps its own Enter.
    event.preventDefault();
    startTask();
  }
});
$("#lane-chip").onclick = (event) => {
  event.preventDefault();
  openLaneMenu($("#lane-menu").hidden);
};
for (const option of document.querySelectorAll("#lane-menu li")) {
  option.onclick = (event) => {
    event.preventDefault();
    if (option.getAttribute("aria-disabled") === "true") return;
    openLaneMenu(false);
    setMode(option.dataset.lane);
  };
}
$("#lane-menu").addEventListener("keydown", (event) => {
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    moveLaneFocus(event.key === "ArrowDown" ? 1 : -1);
  } else if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    const active = $("#lane-menu li.active");
    openLaneMenu(false);
    if (active) setMode(active.dataset.lane);
  } else if (event.key === "Escape" || event.key === "Tab") {
    event.preventDefault();
    openLaneMenu(false);
    $("#input").focus();
  }
});
document.addEventListener("click", (event) => {
  if (!$("#lane-menu").hidden && !event.target.closest(".lane")) openLaneMenu(false);
});
$("#input").addEventListener("input", paintSuggestion);
$("#input").addEventListener("input", (event) => {
  event.target.style.height = "auto";
  event.target.style.height = Math.min(event.target.scrollHeight, 220) + "px";
});
$("#attach").onclick = () => $("#file-input").click();
$("#tc-cancel").onclick = (event) => { event.preventDefault(); closeRunConfirm(); };
$("#tc-start").onclick = (event) => { event.preventDefault(); startTask(); };
$("#tc-test-edits").onchange = paintTestPolicy;
$("#file-input").onchange = (event) => { upload(event.target.files); event.target.value = ""; };
$("#transcript").addEventListener("dragover", (e) => e.preventDefault());
$("#transcript").addEventListener("drop", (event) => {
  event.preventDefault();
  if (event.dataTransfer.files.length) upload(event.dataTransfer.files);
});
$("#new-session").onclick = async (event) => {
  // The server returns the unstarted session if one exists, so this is
  // idempotent; disabling the button also stops a burst of clicks from
  // queueing requests that each wait on the same lock.
  const button = event.currentTarget;
  if (button.disabled) return;
  button.disabled = true;
  try {
    // A new chat starts in the server's default folder (~/saddle-ranch), not
    // whatever the current session happens to be in; sending no workdir lets
    // the server fill its default. `saddle-dev work` still overrides it by
    // PATCHing the session's workdir after it creates the session.
    const session = await api("/api/sessions", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    select(session.id);
  } finally {
    button.disabled = false;
  }
};

/* ---------- settings and personas ---------- */

async function loadPersonas() {
  const reply = await api("/api/personas");
  state.personas = reply.personas;
  state.builtinPersonas = reply.builtin;
  state.editablePersonas = reply.editable;
  for (const id of ["#persona", "#default-persona", "#persona-pick"]) {
    const picker = $(id);
    if (!picker) continue;
    const had = picker.value;
    picker.textContent = "";
    for (const name of Object.keys(state.personas)) {
      picker.appendChild(el("option", null, name)).value = name;
    }
    if (had) picker.value = had;
  }
}

function personaIsBuiltin(name) {
  return (state.builtinPersonas || []).includes(name);
}

function showPersona(name) {
  $("#persona-name").value = name || "";
  $("#persona-prompt").value = (state.personas || {})[name] || "";
  const edited = (state.editablePersonas || []).includes(name);
  const builtin = personaIsBuiltin(name);
  const remove = $("#persona-delete");
  remove.disabled = !edited;
  remove.textContent = builtin ? "Reset to built-in" : "Delete";
  $("#persona-note").textContent = !name
    ? "New persona: give it a name and a prompt."
    : builtin && edited
      ? "Built-in, edited. Resetting restores the shipped prompt."
      : builtin
        ? "Built-in. Saving keeps your version until you reset it."
        : "Yours.";
}

async function openSettings() {
  const settings = await api("/api/settings");
  await loadPersonas();
  $("#default-persona").value = settings.persona;
  $("#default-effort").value = settings.reasoning_effort;
  $("#default-temp").value = String(settings.temperature);
  $("#default-temp-value").textContent = Number(settings.temperature).toFixed(1);
  $("#persona-pick").value = settings.persona;
  showPersona(settings.persona);
  $("#settings-dialog").showModal();
}

async function saveDefaults() {
  await api("/api/settings", {
    method: "PATCH", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      persona: $("#default-persona").value,
      reasoning_effort: $("#default-effort").value,
      temperature: Number($("#default-temp").value),
    }),
  });
}

/* The sidebar is a drawer on a narrow screen. Picking a session closes it,
   because on a phone the thing you just chose is behind it. */
function drawer(open) {
  $("#sidebar").classList.toggle("open", open);
  $("#scrim").classList.toggle("open", open);
  $("#scrim").hidden = !open;
}
watchScrolling();
$("#jump").onclick = (event) => { event.preventDefault(); followBottom(); };

$("#menu").onclick = (event) => {
  event.preventDefault();
  drawer(!$("#sidebar").classList.contains("open"));
};
$("#scrim").onclick = () => drawer(false);

$("#settings").onclick = (event) => { event.preventDefault(); openSettings(); };
$("#settings-close").onclick = (event) => {
  event.preventDefault();
  $("#settings-dialog").close();
};
$("#default-persona").onchange = saveDefaults;
$("#default-effort").onchange = saveDefaults;
$("#persona-pick").onchange = (event) => showPersona(event.target.value);
$("#persona-new").onclick = (event) => {
  event.preventDefault();
  $("#persona-pick").value = "";
  showPersona("");
  $("#persona-name").focus();
};
$("#persona-save").onclick = async (event) => {
  event.preventDefault();
  const name = $("#persona-name").value.trim();
  if (!name) { $("#persona-note").textContent = "A persona needs a name."; return; }
  const reply = await fetch(`/api/personas/${encodeURIComponent(name)}`, {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt: $("#persona-prompt").value }),
  });
  if (!reply.ok) {
    $("#persona-note").textContent = (await reply.json()).error || "could not save";
    return;
  }
  await loadPersonas();
  $("#persona-pick").value = name;
  showPersona(name);
  $("#persona-note").textContent = "Saved.";
};
$("#persona-delete").onclick = async (event) => {
  event.preventDefault();
  const name = $("#persona-name").value.trim();
  await fetch(`/api/personas/${encodeURIComponent(name)}`, { method: "DELETE" });
  await loadPersonas();
  const next = personaIsBuiltin(name) ? name : Object.keys(state.personas)[0];
  $("#persona-pick").value = next;
  showPersona(next);
  $("#persona-note").textContent = personaIsBuiltin(name) ? "Reset." : "Deleted.";
};
$("#title").onchange = async (event) => {
  await api(`/api/sessions/${state.sessionId}`, {
    method: "PATCH", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title: event.target.value }),
  });
  loadSessions();
};
$("#effort").onchange = async (event) => {
  await api(`/api/sessions/${state.sessionId}`, {
    method: "PATCH", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ reasoning_effort: event.target.value }),
  });
};
function showTemperature(value) {
  // The slider and its readout are one control; setting the value without
  // the label leaves the number lying about what is selected.
  $("#temp").value = String(value);
  $("#temp-value").textContent = Number(value).toFixed(1);
}

// `input` fires per step, which would PATCH on every pixel of a drag, so the
// session is written on `change` -- when the handle is let go.
$("#temp").oninput = (event) => {
  $("#temp-value").textContent = Number(event.target.value).toFixed(1);
};
$("#temp").onchange = async (event) => {
  await api(`/api/sessions/${state.sessionId}`, {
    method: "PATCH", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ temperature: Number(event.target.value) }),
  });
};
$("#default-temp").oninput = (event) => {
  $("#default-temp-value").textContent = Number(event.target.value).toFixed(1);
};
$("#default-temp").onchange = saveDefaults;

$("#persona").onchange = async (event) => {
  await api(`/api/sessions/${state.sessionId}`, {
    method: "PATCH", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ persona: event.target.value }),
  });
};
$("#pick-folder").onclick = (event) => { event.preventDefault(); openFolders(); };
$("#folder-up").onclick = (event) => { event.preventDefault(); openFolders($("#folder-dialog").dataset.parent); };
$("#folder-cancel").onclick = (event) => { event.preventDefault(); $("#folder-dialog").close(); };
$("#folder-use").onclick = async (event) => {
  event.preventDefault();
  const path = $("#folder-dialog").dataset.path;
  await api(`/api/sessions/${state.sessionId}`, {
    method: "PATCH", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ workdir: path }),
  });
  state.folder = path;
  $("#folder-name").textContent = path.split("/").slice(-2).join("/");
  showWhere(path, "");
  $("#folder-dialog").close();
};

/* Theme: an explicit choice wins and persists; with none, follow the system
   setting (and its live changes). The CSS reads data-theme on <html>. */
function storedTheme() {
  try { return localStorage.getItem("saddle.theme"); } catch { return null; }
}
function systemTheme() {
  try { return matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"; }
  catch { return "dark"; }
}
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme === "light" ? "light" : "dark";
}
function initTheme() {
  applyTheme(storedTheme() || systemTheme());
  try {
    matchMedia("(prefers-color-scheme: dark)").addEventListener("change", (event) => {
      if (!storedTheme()) applyTheme(event.matches ? "dark" : "light");  // only while unset
    });
  } catch { /* older browsers: no live follow, the initial read still holds */ }
}
$("#theme-toggle").onclick = () => {
  const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
  try { localStorage.setItem("saddle.theme", next); } catch { /* per-browser nicety only */ }
  applyTheme(next);
};
initTheme();

boot();
