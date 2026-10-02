// @ts-check
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

/**
 * The elements of index.html that are not plain HTMLElements, by id.
 * @typedef {{
 *   "#settings": HTMLButtonElement,
 *   "#new-session": HTMLButtonElement,
 *   "#notify-toggle": HTMLButtonElement,
 *   "#persona": HTMLSelectElement,
 *   "#temp": HTMLInputElement,
 *   "#effort": HTMLSelectElement,
 *   "#pick-folder": HTMLButtonElement,
 *   "#menu": HTMLButtonElement,
 *   "#title": HTMLInputElement,
 *   "#full-access-off": HTMLButtonElement,
 *   "#theme-toggle": HTMLButtonElement,
 *   "#composer": HTMLFormElement,
 *   "#jump": HTMLButtonElement,
 *   "#tc-test-edits": HTMLInputElement,
 *   "#tc-premise": HTMLInputElement,
 *   "#tc-stall": HTMLInputElement,
 *   "#tc-cancel": HTMLButtonElement,
 *   "#tc-start": HTMLButtonElement,
 *   "#lane-chip": HTMLButtonElement,
 *   "#attach": HTMLButtonElement,
 *   "#input": HTMLTextAreaElement,
 *   "#send": HTMLButtonElement,
 *   "#full-access-open": HTMLButtonElement,
 *   "#file-input": HTMLInputElement,
 *   "#folder-dialog": HTMLDialogElement,
 *   "#folder-name-new": HTMLInputElement,
 *   "#folder-make": HTMLButtonElement,
 *   "#folder-up": HTMLButtonElement,
 *   "#folder-cancel": HTMLButtonElement,
 *   "#folder-use": HTMLButtonElement,
 *   "#settings-dialog": HTMLDialogElement,
 *   "#default-persona": HTMLSelectElement,
 *   "#default-temp": HTMLInputElement,
 *   "#default-effort": HTMLSelectElement,
 *   "#persona-pick": HTMLSelectElement,
 *   "#persona-name": HTMLInputElement,
 *   "#persona-prompt": HTMLTextAreaElement,
 *   "#persona-new": HTMLButtonElement,
 *   "#persona-delete": HTMLButtonElement,
 *   "#persona-save": HTMLButtonElement,
 *   "#settings-close": HTMLButtonElement,
 *   "#procs-chip": HTMLButtonElement,
 *   "#procs-dialog": HTMLDialogElement,
 *   "#procs-close": HTMLButtonElement,
 *   "#procs-stop-all": HTMLButtonElement,
 *   "#outside-chip": HTMLButtonElement,
 *   "#outside-dialog": HTMLDialogElement,
 *   "#outside-close": HTMLButtonElement,
 *   "#outside-restore": HTMLButtonElement,
 *   "#outside-remove": HTMLButtonElement,
 *   "#outside-confirm-yes": HTMLButtonElement,
 *   "#outside-confirm-no": HTMLButtonElement,
 *   "#outside-processes": HTMLButtonElement,
 *   "#full-access-dialog": HTMLDialogElement,
 *   "#fa-keep": HTMLButtonElement,
 *   "#fa-grant": HTMLButtonElement,
 *   "#password-dialog": HTMLDialogElement,
 *   "#pw-input": HTMLInputElement,
 *   "#pw-cancel": HTMLButtonElement,
 *   "#pw-send": HTMLButtonElement,
 *   "#approval-dialog": HTMLDialogElement,
 *   "#approval-title": HTMLElement,
 *   "#approval-lines": HTMLElement,
 *   "#approval-decline": HTMLButtonElement,
 *   "#approval-approve": HTMLButtonElement,
 *   "#rewind-dialog": HTMLDialogElement,
 *   "#rewind-text": HTMLTextAreaElement,
 *   "#rewind-cancel": HTMLButtonElement,
 *   "#rewind-go": HTMLButtonElement,
 * }} IdTypes
 */

/**
 * The page's fixed elements, by selector. index.html owns the ids; the type
 * is the element index.html declares for an id listed in IdTypes, else a
 * plain HTMLElement.
 * @template {string} S
 * @param {S} sel
 * @returns {S extends keyof IdTypes ? IdTypes[S] : HTMLElement}
 */
const $ = (sel) => /** @type {any} */ (document.querySelector(sel));

/**
 * A message from the server's event stream or a history reload. The wire
 * format is the server's; `kind` says which fields follow.
 * @typedef {{kind: string, [field: string]: any}} ServerEvent
 */

/** @typedef {{details: HTMLDetailsElement, label: HTMLElement, detail: HTMLElement, name: string}} ToolRowParts */
/** @typedef {HTMLElement & {output?: string}} TerminalBody */
/** @typedef {{name: string, path: string, isImage: boolean}} Attachment */
/** @typedef {{id: number, pid: number, command: string, started: number, ran: string, processes: number}} ProcessRow */
/** @typedef {{path: string, change: string, backup: string, via: string, command: string | null}} OutsideFile */
/** @typedef {{files: OutsideFile[], downloads: {source: string, host: string, path: string | null, size: number | null}[], packages: {manager: string, action: string, packages: string[], exit: number | null}[], not_tracked: {command: string, reasons: string[]}[], undone: {restored: string[], deleted: string[]}[], processes: ProcessRow[], limits: string, can_restore: number, can_delete: number, empty: boolean}} OutsideView */

/**
 * What the page remembers between events. Fields after `mode` are set the
 * first time they are needed, so they are optional.
 * @typedef {object} State
 * @property {string | null} sessionId
 * @property {EventSource | null} stream
 * @property {boolean} busy
 * @property {HTMLElement | null} turnNode
 * @property {HTMLElement | null} assistantNode
 * @property {HTMLDetailsElement | null} reasoningNode
 * @property {HTMLElement | null} reasoningBody
 * @property {Map<string, ToolRowParts>} tools
 * @property {Map<string, TerminalBody>} terminals
 * @property {Attachment[]} attachments
 * @property {Record<string, string>} personas
 * @property {string | null} folder
 * @property {string} mode
 * @property {string | null} [activeTask]
 * @property {HTMLElement | null} [pendingTaskTurn]
 * @property {string | null} [historyFor]
 * @property {{index: number, editing: boolean}} [rewind]
 * @property {string | null} [passwordId]
 * @property {string | null} [approvalId]
 * @property {boolean} [fullAccess]
 * @property {ProcessRow[]} [processes]
 * @property {OutsideView} [outside]
 * @property {string | null} [suggestion]
 * @property {string[]} [builtinPersonas]
 * @property {string[]} [editablePersonas]
 */

/** @type {State} */
const state = {
  sessionId: null,
  stream: null,
  busy: false,
  turnNode: null,
  assistantNode: null,
  reasoningNode: null,
  reasoningBody: null,
  tools: new Map(),
  terminals: new Map(),
  attachments: [],
  personas: {},
  folder: null,
  mode: "ask",
};

/** @type {Set<HTMLElement>} */
const painting = new Set();
let paintFrame = 0;

function flushPaint() {
  if (paintFrame) {
    cancelAnimationFrame(paintFrame);
    paintFrame = 0;
  }
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

/** @param {HTMLElement} node */
function schedulePaint(node) {
  painting.add(node);
  if (paintFrame) return;
  paintFrame = requestAnimationFrame(() => {
    paintFrame = 0;
    flushPaint();
  });
}

/* ---------- transcript pieces ---------- */

/**
 * @param {string | null} prompt
 * @returns {HTMLElement}
 */
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
  details.open = true; // opens itself while it streams
  details.dataset.started = String(Date.now());
  /** @type {HTMLElement} */ (state.turnNode).appendChild(details);
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
  /** @type {Element} */ (node.querySelector("summary span")).textContent = `Thought for ${shown}`;
}

function assistantBlock() {
  if (state.assistantNode) return state.assistantNode;
  if (state.reasoningNode) foldReasoning(); // the answer began: fold it away
  const node = el("div", "assistant");
  node.dataset.raw = "";
  /** @type {HTMLElement} */ (state.turnNode).appendChild(node);
  state.assistantNode = node;
  return node;
}

/**
 * @param {ServerEvent} event
 * @returns {HTMLDetailsElement}
 */
function toolRow(event) {
  const details = el("details", "tool running");
  const summary = el("summary");
  summary.appendChild(el("span", "dot"));
  const label = el("span", "label", event.present); // present tense, while running
  summary.appendChild(label);
  summary.appendChild(el("span", "ms", ""));
  details.appendChild(summary);
  const detail = el("div", "tool-detail");
  if (event.name === "run_command") detail.classList.add("terminal");
  detail.textContent = "…";
  details.appendChild(detail);
  /** @type {HTMLElement} */ (state.turnNode).appendChild(details);
  state.tools.set(event.id, { details, label, detail, name: event.name });
  return details;
}

/** @param {ServerEvent} event */
function finishTool(event) {
  const row = state.tools.get(event.id);
  if (!row) return;
  row.details.classList.remove("running");
  row.details.classList.add(event.ok ? "ok" : "failed");
  row.label.textContent = event.label; // -> past tense, or "Failed to …"
  /** @type {Element} */ (row.details.querySelector(".ms")).textContent =
    event.duration_ms >= 1000 ? `${(event.duration_ms / 1000).toFixed(1)}s` : `${event.duration_ms}ms`;
  fillToolDetail(row.details, row.detail, event.detail);
  markUnsandboxed(row.details, event.detail);
  if (event.preview) showPreview(row.details, event.preview, event.version);
  if (!event.ok) row.details.open = true; // a failure should not need a click
}

/* A background command keeps producing output after the tool call that
   started it returned, so it gets its own block that fills as it runs
   rather than appearing all at once when someone reads it. */
/**
 * @param {string} id
 * @returns {TerminalBody}
 */
function terminalBlock(id) {
  if (state.terminals.has(id)) return /** @type {TerminalBody} */ (state.terminals.get(id));
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

/** @typedef {{id: string, title: string, run_state?: string | null, run_task?: string}} SessionInfo */

/**
 * What a caught value says: an Error's message, else the value itself.
 * @param {unknown} error
 */
function errorText(error) {
  return String((error && /** @type {{message?: unknown}} */ (error).message) || error);
}

/**
 * @param {string} text
 * @param {string} [kind]
 */
function notice(text, kind) {
  const node = el("div", `notice ${kind || ""}`, text);
  ($("#transcript").lastElementChild || $("#transcript")).appendChild(node);
}

/* ---------- event stream ---------- */

/** @param {ServerEvent} event */
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
    case "reasoning.delta": {
      reasoningBlock();
      const thought = /** @type {HTMLElement} */ (state.reasoningBody);
      // Appending a text node per frame, not `textContent +=`, which
      // re-reads and rewrites the whole transcript of the thought.
      thought.dataset.reasoning = "1";
      thought.dataset.pending = (thought.dataset.pending || "") + event.text; // never truncated
      schedulePaint(thought);
      break;
    }
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
    case "approval.request":
      askApproval(event);
      break;
    case "approval.done":
      if (state.approvalId === event.id) closeApproval();
      break;
    case "terminal.output": {
      // Append text nodes; the raw text rides along so the button in the
      // row's header copies what the command said, not what the DOM holds.
      const body = terminalBlock(event.id);
      if (!body.output) {
        const row = /** @type {ParentNode} */ (body.parentNode);
        attachCopy(
          /** @type {HTMLElement} */ (row.querySelector("summary")),
          () => body.output || "",
          "terminal output",
        );
      }
      body.output = (body.output || "") + event.chunk;
      body.appendChild(document.createTextNode(event.chunk));
      break;
    }
    case "context": {
      const pct = Math.min(100, Math.round((event.used / event.limit) * 100));
      const meter = $("#meter");
      /** @type {HTMLElement} */ (meter.querySelector("i")).style.width = `${pct}%`;
      /** @type {HTMLElement} */ (meter.querySelector("b")).textContent =
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
      foldReasoning(); // a turn with no answer text still folds
      setStatus("idle");
      state.busy = false;
      refreshProcesses();
      refreshOutside();
      break;
    case "idle":
      flushPaint();
      restampTurns();
      setStatus("idle");
      state.busy = false;
      state.activeTask = null;
      break;
    default:
      handleTask(event); // task.* events: the task card (tasks.js)
  }
  stickToBottom(was);
}

/** @param {ServerEvent} info */
function renderHistory(info) {
  $("#title").value = info.title;
  $("#folder-name").textContent = info.workdir.split("/").slice(-2).join("/") || info.workdir;
  state.folder = info.workdir;
  $("#persona").value = info.persona;
  if (info.reasoning_effort) $("#effort").value = info.reasoning_effort;
  if (info.temperature !== undefined) showTemperature(info.temperature);
  showMode(info.mode);
  showFullAccess(info.full_access);
  refreshProcesses();
  refreshOutside(true);
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
    (/** @type {ServerEvent} */ m) => m.role === "user" || m.role === "assistant",
  );
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
  followBottom(); // a freshly opened session starts at the end
}

/**
 * @param {Element} after
 * @param {string} path
 * @param {string | undefined} version
 */
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

/**
 * @param {ServerEvent} call
 * @returns {HTMLDetailsElement}
 */
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

/**
 * @param {HTMLElement} turn
 * @param {ServerEvent[]} calls
 */
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
  /** @type {ServerEvent[]} */
  let stored;
  try {
    stored = await api(`/api/sessions/${state.sessionId}/messages`);
  } catch {
    return; // the buttons are a convenience, not the turn
  }
  const asked = stored.map((message, index) => (message.role === "user" ? index : -1)).filter((index) => index >= 0);
  // Only the turns that carry a question: an assistant reply gets its own
  // .turn node, so counting all of them never matched and the restamp
  // silently never ran for a live turn.
  const turns = /** @type {HTMLElement[]} */ ([...$("#transcript").querySelectorAll(".turn")]).filter((turn) =>
    turn.querySelector(".user"),
  );
  if (turns.length !== asked.length) return; // mid-stream; try again next idle
  turns.forEach((turn, position) => {
    turn.dataset.index = String(asked[position]);
    // A task's bubble stands for a run, not a question: no retry on it.
    if (!turn.querySelector(".turn-tools") && !turn.querySelector(".task-ask-bubble")) {
      turn.insertBefore(turnTools(asked[position]), turn.firstChild);
    }
  });
}

/**
 * @param {number} index
 * @returns {HTMLElement}
 */
function turnTools(index) {
  const tools = el("div", "turn-tools");
  for (const [glyph, title, edit] of /** @type {[string, string, boolean][]} */ ([
    ["↻", "Run this again", false],
    ["✎", "Edit and run again", true],
  ])) {
    const button = el("button", null, glyph);
    button.type = "button";
    button.title = title;
    button.onclick = (event) => {
      event.preventDefault();
      openRewind(index, edit);
    };
    tools.appendChild(button);
  }
  return tools;
}

/**
 * @param {number} index
 * @param {boolean} editing
 */
async function openRewind(index, editing) {
  // Ask the server what this would change *before* offering the button, so
  // the warning is the real list of files rather than a guess.
  let preview;
  try {
    preview = await api(`/api/sessions/${state.sessionId}/rewind?index=${index}`);
  } catch (error) {
    notice(errorText(error), "error");
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
  /** @type {{index: number, text?: string}} */
  const body = { index };
  if (editing) body.text = $("#rewind-text").value;
  $("#rewind-dialog").close();

  // Drop the turns being replaced before the new one streams in, so the
  // transcript never shows both attempts at once.
  for (const node of /** @type {HTMLElement[]} */ ([...$("#transcript").children])) {
    const at = Number(node.dataset.index);
    if (!Number.isNaN(at) && at >= index) node.remove();
  }
  state.assistantNode = null;
  state.reasoningNode = null;

  let result;
  try {
    result = await api(`/api/sessions/${state.sessionId}/rewind`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (error) {
    notice(errorText(error), "error");
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

/** @param {string} sessionId */
function connect(sessionId) {
  if (state.stream) state.stream.close();
  state.stream = new EventSource(`/api/sessions/${sessionId}/events`);
  state.stream.onmessage = (message) => handle(JSON.parse(message.data));
  state.stream.onerror = () => setStatus("error");
}

/**
 * @param {string} kind
 * @param {string} [detail]
 */
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

/**
 * The server's JSON replies are untyped here; each caller reads the
 * fields it knows.
 * @param {string} path
 * @param {RequestInit} [options]
 * @returns {Promise<any>}
 */
async function api(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) throw new Error((await response.json()).error || response.statusText);
  return response.json();
}

/** @returns {Promise<SessionInfo[]>} */
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
      await deleteSession(session); // runs.js: hidden now, gone after the undo toast
    };
    row.appendChild(kill);
    row.onclick = () => select(session.id);
    list.appendChild(row);
  }
  noteSessions(sessions);
  loadRuns();
  return sessions;
}

/** @param {string} sessionId */
function select(sessionId) {
  drawer(false);
  state.sessionId = sessionId;
  clearOutside(); // the previous session's count must not outlive the switch
  state.historyFor = null; // a different transcript: rebuild it
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
    await api("/api/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    sessions = await loadSessions();
  }
  const remembered = localStorage.getItem("saddle.session");
  select(remembered !== null && sessions.some((s) => s.id === remembered) ? remembered : sessions[0].id);
}

/* ---------- composer ---------- */

/* The lane, per session: what Enter does. The words are what the code does
   -- an Ask turn is offered read-only tools and the server refuses any other
   call; an Edit turn runs the file and shell tools in the folder itself, with
   no auditor; a task runs `saddle auto` in a worktree and ends in a packet.
   Feature, Breadth and Long are shown so the shape is visible, and cannot be
   chosen: `enabled` is false and nothing below selects a disabled lane. */
/** @typedef {{id: string, enabled: true, label: string, desc: string, placeholder: string}} Lane */
/** @typedef {{id: string, enabled: false, label: string}} DisabledLane */

/** @type {(Lane | DisabledLane)[]} */
const LANES = [
  {
    id: "ask",
    enabled: true,
    label: "Ask",
    desc: "Ask: read-only. It reads and searches your folder, then answers. It cannot edit files or run commands.",
    placeholder: "ask about this folder (read-only, nothing changes)",
  },
  {
    id: "edit",
    enabled: true,
    label: "Edit",
    desc: "Edit: unaudited, edits your folder. It writes files and runs commands directly; nothing checks the work and there is no packet.",
    placeholder: "this will edit your folder directly, unaudited",
  },
  {
    id: "task",
    enabled: true,
    label: "Task · Small",
    desc: "Task · Small: an audited run on a new branch in a copy of your folder, ending in an evidence packet. Enter shows the run before it starts.",
    placeholder: "describe the job; Enter shows the run before it starts",
  },
  { id: "feature", enabled: false, label: "Feature" },
  { id: "breadth", enabled: false, label: "Breadth" },
  { id: "long", enabled: false, label: "Long" },
];
/**
 * @param {string | undefined} id
 * @returns {Lane | undefined}
 */
const laneOf = (id) => /** @type {Lane | undefined} */ (LANES.find((lane) => lane.id === id && lane.enabled));
// Kept by name for the code that already reads it (tasks.js, older tests).
const MODES = Object.fromEntries(LANES.filter((l) => l.enabled).map((l) => [l.id, l]));

/** @param {string | undefined} mode */
function showMode(mode) {
  // "ask" is always an enabled lane, so the fallback is never undefined.
  const lane = /** @type {Lane} */ (laneOf(mode) || laneOf("ask"));
  state.mode = lane.id;
  $("#lane-name").textContent = lane.label;
  $("#lane-chip").dataset.lane = lane.id;
  for (const option of /** @type {NodeListOf<HTMLElement>} */ (document.querySelectorAll("#lane-menu li"))) {
    option.setAttribute("aria-selected", String(option.dataset.lane === lane.id));
  }
  $("#mode-desc").textContent = lane.desc;
  $("#mode-chip").textContent = lane.id;
  $("#mode-chip").dataset.mode = lane.id;
  document.body.dataset.mode = lane.id; // page-level hook: task mode quiets the chat-only chrome
  $("#input").placeholder = lane.placeholder;
  $("#mode-note").hidden = true;
  paintFullAccess();
  if (lane.id !== "task") closeRunConfirm();
  paintSuggestion();
}

/** @param {string | undefined} mode */
async function setMode(mode) {
  if (!laneOf(mode)) return; // a disabled or unknown lane is never chosen
  showMode(mode);
  $("#input").focus();
  try {
    const session = await api(`/api/sessions/${state.sessionId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode: state.mode }),
    });
    showFullAccess(session.full_access); // leaving Edit ends full access
    reportStopped(session.stopped);
  } catch (error) {
    notice(errorText(error), "error");
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

/** @param {boolean | undefined} on */
function showFullAccess(on) {
  state.fullAccess = Boolean(on);
  paintFullAccess();
}

/** @param {boolean} on */
async function setFullAccess(on) {
  const body = on ? { on: true, confirm: $("#fa-grant").dataset.confirm } : { on: false };
  try {
    const session = await api(`/api/sessions/${state.sessionId}/full-access`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    showFullAccess(session.full_access);
    reportStopped(session.stopped);
  } catch (error) {
    notice(errorText(error), "error");
  }
}

/* ---------- processes (#136) ----------
   What a session's commands started and left running, listed by the server
   (the session's cgroups, so a program that detached is found too). The chip
   shows while anything runs; its dialog stops one entry or all of them. Ending
   access stops everything, and the page says what was stopped. */

/** @param {{command: string}[] | undefined} stopped */
function stoppedText(stopped) {
  if (!stopped || !stopped.length) return "";
  const plural = stopped.length === 1 ? "" : "s";
  return `Stopped ${stopped.length} program${plural} still running from this session: ${stopped
    .map((p) => p.command)
    .join(", ")}.`;
}

/** @param {{command: string}[] | undefined} stopped */
function reportStopped(stopped) {
  const text = stoppedText(stopped);
  if (text) notice(text);
  refreshProcesses();
}

/**
 * @param {ProcessRow[]} rows
 * @param {string} tracking
 */
function paintProcesses(rows, tracking) {
  state.processes = rows;
  $("#procs-chip").hidden = rows.length === 0;
  $("#procs-count").textContent = String(rows.length);
  const dialog = $("#procs-dialog");
  if (!dialog.open) return;
  const list = $("#procs-list");
  list.textContent = "";
  for (const row of rows) {
    const item = el("li", "proc");
    const text = el("div", "proc-text");
    text.appendChild(el("code", "proc-command", row.command));
    const when = new Date(row.started * 1000).toLocaleTimeString();
    const more = row.processes > 1 ? ` · ${row.processes} processes` : "";
    text.appendChild(el("span", "proc-meta", `started ${when} · group ${row.id}${more}`));
    if (row.ran !== row.command) text.appendChild(el("span", "proc-meta", `from: ${row.ran}`));
    item.appendChild(text);
    const stop = el("button", "danger", "Stop");
    stop.type = "button";
    stop.setAttribute("aria-label", `Stop ${row.command}`);
    stop.onclick = () => stopProcesses({ id: row.id });
    item.appendChild(stop);
    list.appendChild(item);
  }
  $("#procs-empty").hidden = rows.length > 0;
  $("#procs-stop-all").hidden = rows.length === 0;
  const note = $("#procs-note");
  note.hidden = tracking === "cgroup";
  note.textContent =
    "No user systemd manager here, so only each command's own process group is tracked: a program that detached itself (setsid) is not listed.";
}

async function refreshProcesses() {
  if (!state.sessionId || document.hidden) return;
  if (state.mode !== "edit") {
    // Only the Edit lane runs commands, and leaving it stopped them all.
    paintProcesses([], "cgroup");
    return;
  }
  try {
    const listed = await api(`/api/sessions/${state.sessionId}/processes`);
    paintProcesses(listed.processes, listed.tracking);
  } catch {
    // The list is a convenience; a failed look leaves the last one showing.
  }
}

/** @param {{id: number} | {all: true}} which */
async function stopProcesses(which) {
  try {
    const done = await api(`/api/sessions/${state.sessionId}/processes/stop`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(which),
    });
    reportStopped(done.stopped);
  } catch (error) {
    notice(errorText(error), "error");
  }
}

$("#procs-chip").onclick = async () => {
  $("#procs-dialog").showModal();
  await refreshProcesses();
};
$("#procs-close").onclick = () => $("#procs-dialog").close();
$("#procs-stop-all").onclick = () => stopProcesses({ all: true });
setInterval(refreshProcesses, 3000);

/* ---------- outside changes (#137) ----------
   What a full-access session changed outside its folder: files (each backed
   up first), downloads, packages, and the commands that could not be tracked.
   The chip shows once there is anything to review; the dialog lists it and
   offers Undo. Undo restores backed-up files; removing files the session
   created asks first. */

/** @param {number | null} bytes */
function sizeText(bytes) {
  if (bytes === null) return "size unknown";
  if (bytes < 1024) return `${bytes} bytes`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/**
 * @param {string} id
 * @param {string} heading
 * @param {HTMLElement[]} rows
 */
function fillOutsideSection(id, heading, rows) {
  const box = $(id);
  box.textContent = "";
  box.hidden = rows.length === 0;
  if (rows.length === 0) return;
  box.appendChild(el("h4", "outside-heading", heading));
  const list = el("ul", "outside-list");
  for (const row of rows) list.appendChild(row);
  box.appendChild(list);
}

/**
 * @param {string} head
 * @param {string} meta
 * @param {string} [badge]
 */
function outsideRow(head, meta, badge) {
  const item = el("li", "outside-row");
  const top = el("div", "outside-head");
  top.appendChild(el("code", "outside-path", head));
  if (badge) top.appendChild(el("span", `outside-badge ${badge}`, badge));
  item.appendChild(top);
  if (meta) item.appendChild(el("span", "proc-meta", meta));
  return item;
}

/** @param {string} command */
function shortCommand(command) {
  return command.length > 70 ? `${command.slice(0, 69)}…` : command;
}

/** @param {OutsideFile} f */
function fileMeta(f) {
  const how = f.command ? `${f.via}: ${shortCommand(f.command)}` : f.via;
  const original = f.change === "created" ? "new file, no original" : f.backup;
  return `${original} · by ${how}`;
}

/** @param {OutsideView} view */
function paintOutside(view) {
  state.outside = view;
  const count = view.files.length + view.downloads.length + view.packages.length + view.not_tracked.length;
  $("#outside-chip").hidden = view.empty;
  $("#outside-count").textContent = String(count);
  const dialog = $("#outside-dialog");
  if (!dialog.open) return;
  fillOutsideSection(
    "#outside-files",
    "Files",
    view.files.map((f) => outsideRow(f.path, fileMeta(f), f.change)),
  );
  fillOutsideSection(
    "#outside-downloads",
    "Downloads",
    view.downloads.map((d) => outsideRow(d.source, `${sizeText(d.size)}${d.path ? ` · saved to ${d.path}` : ""}`)),
  );
  fillOutsideSection(
    "#outside-packages",
    "Packages",
    view.packages.map((p) => outsideRow(`${p.manager} ${p.action} ${p.packages.join(" ")}`, "reverse these by hand")),
  );
  fillOutsideSection(
    "#outside-untracked",
    "Not tracked",
    view.not_tracked.map((n) => outsideRow(n.command, n.reasons.join("; "), "not tracked")),
  );
  fillOutsideSection(
    "#outside-undone",
    "Undone",
    view.undone.map((u) => outsideRow(`${u.restored.length} restored, ${u.deleted.length} removed`, "")),
  );
  $("#outside-limits").textContent = view.limits;
  $("#outside-empty").hidden = !view.empty;
  const running = (view.processes || []).length;
  $("#outside-processes").hidden = running === 0;
  $("#outside-processes").textContent = `${running} program${running === 1 ? "" : "s"} still running: open the list`;
  $("#outside-restore").disabled = view.can_restore === 0;
  $("#outside-restore").textContent = `Restore ${view.can_restore} file${view.can_restore === 1 ? "" : "s"}`;
  $("#outside-remove").hidden = view.can_delete === 0;
  $("#outside-remove").textContent = `Remove ${view.can_delete} created file${view.can_delete === 1 ? "" : "s"}…`;
  $("#outside-confirm-text").textContent =
    `Remove the ${view.can_delete} file${view.can_delete === 1 ? "" : "s"} this session created? This cannot be undone.`;
}

/** The chip shows only the session on screen: none until its own record is read. */
function clearOutside() {
  state.outside = undefined;
  $("#outside-chip").hidden = true;
  $("#outside-count").textContent = "0";
}

/** @param {boolean} [always] read the record even outside the Edit lane (a session just opened) */
async function refreshOutside(always = false) {
  if (!state.sessionId || document.hidden) return;
  if (!always && state.mode !== "edit" && !$("#outside-dialog").open) return;
  const sessionId = state.sessionId;
  try {
    const view = await api(`/api/sessions/${sessionId}/outside`);
    if (state.sessionId === sessionId) paintOutside(view);
  } catch {
    // The record is a convenience to look at; a failed look leaves the last one showing.
  }
}

/** @param {boolean} deleteCreated */
async function undoOutside(deleteCreated) {
  $("#outside-confirm").hidden = true;
  try {
    const body = deleteCreated ? { delete_created: true, confirm: true } : {};
    const done = await api(`/api/sessions/${state.sessionId}/outside/undo`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const parts = [`${done.restored.length} restored`];
    if (deleteCreated) parts.push(`${done.deleted.length} removed`);
    if (done.failed.length) parts.push(`${done.failed.length} could not be restored (no backup)`);
    notice(`Undo: ${parts.join(", ")}.`);
    paintOutside({ ...done.record, processes: state.outside ? state.outside.processes : [] });
    refreshOutside();
  } catch (error) {
    notice(errorText(error), "error");
  }
}

$("#outside-chip").onclick = async () => {
  $("#outside-confirm").hidden = true;
  $("#outside-dialog").showModal();
  await refreshOutside();
};
$("#outside-close").onclick = () => $("#outside-dialog").close();
$("#outside-restore").onclick = () => undoOutside(false);
$("#outside-remove").onclick = () => {
  $("#outside-confirm").hidden = false;
};
$("#outside-confirm-no").onclick = () => {
  $("#outside-confirm").hidden = true;
};
$("#outside-confirm-yes").onclick = () => undoOutside(true);
$("#outside-processes").onclick = () => {
  $("#outside-dialog").close();
  $("#procs-chip").click();
};
setInterval(refreshOutside, 3000);

/* The server puts this on the first line of every command result while full
   access is on (`tools.UNSANDBOXED`); the badge sits on the summary, so it
   shows while the row is folded, live and after a reload alike. */
const UNSANDBOXED_MARK = "[UNSANDBOXED:";
/** @param {HTMLElement} details */
function badgeUnsandboxed(details) {
  details.classList.add("unsandboxed");
  const summary = /** @type {HTMLElement} */ (details.querySelector("summary"));
  if (summary.querySelector(".unsandboxed-badge")) return;
  const badge = el("span", "unsandboxed-badge", "unsandboxed");
  badge.title = "Full access was on: this command ran as you, outside the sandbox";
  summary.insertBefore(badge, summary.querySelector(".label"));
}

/**
 * @param {HTMLElement} details
 * @param {unknown} text
 */
function markUnsandboxed(details, text) {
  if (typeof text === "string" && text.startsWith(UNSANDBOXED_MARK)) badgeUnsandboxed(details);
}

/* A full-access command's sudo asks for a password (#125). What is typed
   goes to that command only: posted once, then the field is cleared. */
/** @param {ServerEvent} event */
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

/** @param {boolean} cancel */
async function answerPassword(cancel) {
  const id = state.passwordId;
  if (!id) return;
  const body = cancel ? { id, cancel: true } : { id, password: $("#pw-input").value };
  closePassword();
  try {
    await api(`/api/sessions/${state.sessionId}/password`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (error) {
    notice(errorText(error), "error");
  }
}

$("#pw-send").onclick = () => answerPassword(false);
$("#pw-cancel").onclick = () => answerPassword(true);
$("#pw-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    answerPassword(false);
  }
});
$("#password-dialog").addEventListener("cancel", (event) => {
  // Escape
  event.preventDefault();
  answerPassword(true);
});

/* Something needs a yes or no (#139, #93): an MCP server's descriptions, a
   large download, a command running what the web reader brought back. The
   lines are shown as text, never as markup. */
/** @param {ServerEvent} event */
function askApproval(event) {
  state.approvalId = event.id;
  $("#approval-title").textContent = event.title || "Approve?";
  $("#approval-lines").textContent = (event.lines || []).join("\n");
  if (!$("#approval-dialog").open) $("#approval-dialog").showModal();
  $("#approval-decline").focus();
}

function closeApproval() {
  state.approvalId = null;
  if ($("#approval-dialog").open) $("#approval-dialog").close();
}

/** @param {boolean} approve */
async function answerApproval(approve) {
  const id = state.approvalId;
  if (!id) return;
  closeApproval();
  try {
    await api(`/api/sessions/${state.sessionId}/approval`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id, approve }),
    });
  } catch (error) {
    notice(errorText(error), "error");
  }
}

$("#approval-approve").onclick = () => answerApproval(true);
$("#approval-decline").onclick = () => answerApproval(false);
$("#approval-dialog").addEventListener("cancel", (event) => {
  // Escape declines
  event.preventDefault();
  answerApproval(false);
});

$("#full-access-open").onclick = () => $("#full-access-dialog").showModal();
$("#fa-keep").onclick = () => $("#full-access-dialog").close();
$("#fa-grant").onclick = () => {
  $("#full-access-dialog").close();
  setFullAccess(true);
};
$("#full-access-off").onclick = () => setFullAccess(false);

/* Shift+Tab: the next enabled lane, wrapping. */
/**
 * @param {string} from
 * @param {number} [step]
 */
function nextLane(from, step = 1) {
  const usable = LANES.filter((lane) => lane.enabled);
  const at = usable.findIndex((lane) => lane.id === from);
  return usable[(at + step + usable.length) % usable.length].id;
}

/* A guess from the words alone, shown as a hint and never acted on: only
   Tab (the user) takes it. Choosing the lane silently is how "ran a task by
   accident" comes back. */
const TASK_VERB =
  /^(please\s+)?(make|fix|add|remove|rename|refactor|change|implement|write|update|delete|move|replace|convert|split)\b/i;
const FILE_NAME = /\b[\w./-]+\.(py|js|mjs|ts|tsx|md|json|toml|css|html|rs|go|c|h|java|ya?ml|txt|sh)\b/i;
/** @param {string} text */
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
  box.textContent =
    state.suggestion === "task"
      ? "looks like a task → Tab"
      : state.suggestion === "ask"
        ? "looks like a question → Tab for Ask"
        : "";
}

/** @param {boolean} open */
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
/** @param {number} step */
function moveLaneFocus(step) {
  const options = [...document.querySelectorAll('#lane-menu li:not([aria-disabled="true"])')];
  const at = options.findIndex((o) => o.classList.contains("active"));
  const next = options[(at + step + options.length) % options.length];
  for (const o of options) o.classList.toggle("active", o === next);
}

/**
 * @param {string | undefined} workdir
 * @param {string | undefined} branch
 */
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
  const body = others.length ? `${text}\n\n[also attached in uploads/: ${others.join(", ")}]` : text;
  newTurn(input.value.trim());
  followBottom(); // sending is an intent to watch the reply
  input.value = "";
  paintSuggestion();
  input.style.height = "auto";
  state.attachments = [];
  $("#attachments").textContent = "";
  state.busy = true;
  setStatus("working");
  try {
    await api(`/api/sessions/${state.sessionId}/message`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: body, images }),
    });
  } catch (error) {
    notice(errorText(error), "error");
    setStatus("error");
    state.busy = false;
  }
}

/** @param {FileList | File[]} files */
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
      img.src = URL.createObjectURL(/** @type {File} */ (file));
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

/** @param {string} [path] */
async function openFolders(path) {
  const data = await api(`/api/browse?path=${encodeURIComponent(path || state.folder || "")}`);
  $("#folder-path").textContent = data.path;
  $("#folder-dialog").dataset.path = data.path;
  $("#folder-dialog").dataset.parent = data.parent;
  const list = $("#folder-entries");
  list.textContent = "";
  for (const entry of data.entries) {
    const button = el("button", null, entry.name + "/");
    button.onclick = (event) => {
      event.preventDefault();
      openFolders(entry.path);
    };
    list.appendChild(button);
  }
  $("#folder-error").textContent = "";
  if (!$("#folder-dialog").open) $("#folder-dialog").showModal();
}

async function makeFolder() {
  const name = $("#folder-name-new").value.trim();
  const here = $("#folder-dialog").dataset.path;
  if (!name) {
    $("#folder-error").textContent = "give the folder a name";
    return;
  }
  const reply = await fetch("/api/browse", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path: here, name }),
  });
  const body = await reply.json();
  if (!reply.ok) {
    $("#folder-error").textContent = body.error || "could not create";
    return;
  }
  $("#folder-name-new").value = "";
  // Step into it: making a folder here almost always means working in it.
  await openFolders(body.path);
}

$("#folder-make").onclick = (event) => {
  event.preventDefault();
  makeFolder();
};
$("#folder-name-new").addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    makeFolder();
  }
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
    event.preventDefault(); // the user took the hint; nothing else does
    setMode(state.suggestion);
    return;
  }
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    submitComposer();
  }
  if (event.key === "Escape" && !$("#task-confirm").hidden) {
    event.preventDefault();
    closeRunConfirm();
  }
});
$("#task-confirm").addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    event.preventDefault();
    closeRunConfirm();
    $("#input").focus();
  } else if (event.key === "Enter" && !event.shiftKey && /** @type {HTMLElement} */ (event.target).id !== "tc-cancel") {
    // Enter or Ctrl+Enter anywhere in the strip starts; Cancel keeps its own Enter.
    event.preventDefault();
    startTask();
  }
});
$("#lane-chip").onclick = (event) => {
  event.preventDefault();
  openLaneMenu(/** @type {boolean} */ ($("#lane-menu").hidden));
};
for (const option of /** @type {NodeListOf<HTMLElement>} */ (document.querySelectorAll("#lane-menu li"))) {
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
  if (!$("#lane-menu").hidden && !(/** @type {Element} */ (event.target).closest(".lane"))) openLaneMenu(false);
});
$("#input").addEventListener("input", paintSuggestion);
$("#input").addEventListener("input", (event) => {
  const box = /** @type {HTMLTextAreaElement} */ (event.target);
  box.style.height = "auto";
  box.style.height = Math.min(box.scrollHeight, 220) + "px";
});
$("#attach").onclick = () => $("#file-input").click();
$("#tc-cancel").onclick = (event) => {
  event.preventDefault();
  closeRunConfirm();
};
$("#tc-start").onclick = (event) => {
  event.preventDefault();
  startTask();
};
$("#tc-test-edits").onchange = paintTestPolicy;
$("#file-input").onchange = (event) => {
  const picker = /** @type {HTMLInputElement} */ (event.target);
  upload(/** @type {FileList} */ (picker.files));
  picker.value = "";
};
$("#transcript").addEventListener("dragover", (e) => e.preventDefault());
$("#transcript").addEventListener("drop", (event) => {
  event.preventDefault();
  const dropped = /** @type {DataTransfer} */ (event.dataTransfer).files;
  if (dropped.length) upload(dropped);
});
$("#new-session").onclick = async (event) => {
  // The server returns the unstarted session if one exists, so this is
  // idempotent; disabling the button also stops a burst of clicks from
  // queueing requests that each wait on the same lock.
  const button = /** @type {HTMLButtonElement} */ (event.currentTarget);
  if (button.disabled) return;
  button.disabled = true;
  try {
    // A new chat starts in the server's default folder (~/saddle-ranch), not
    // whatever the current session happens to be in; sending no workdir lets
    // the server fill its default. `saddle-dev work` still overrides it by
    // PATCHing the session's workdir after it creates the session.
    const session = await api("/api/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
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
  for (const id of /** @type {("#persona" | "#default-persona" | "#persona-pick")[]} */ ([
    "#persona",
    "#default-persona",
    "#persona-pick",
  ])) {
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

/** @param {string} name */
function personaIsBuiltin(name) {
  return (state.builtinPersonas || []).includes(name);
}

/** @param {string} name */
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
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      persona: $("#default-persona").value,
      reasoning_effort: $("#default-effort").value,
      temperature: Number($("#default-temp").value),
    }),
  });
}

/* The sidebar is a drawer on a narrow screen. Picking a session closes it,
   because on a phone the thing you just chose is behind it. */
/** @param {boolean} open */
function drawer(open) {
  $("#sidebar").classList.toggle("open", open);
  $("#scrim").classList.toggle("open", open);
  $("#scrim").hidden = !open;
}
watchScrolling();
$("#jump").onclick = (event) => {
  event.preventDefault();
  followBottom();
};

$("#menu").onclick = (event) => {
  event.preventDefault();
  drawer(!$("#sidebar").classList.contains("open"));
};
$("#scrim").onclick = () => drawer(false);

$("#settings").onclick = (event) => {
  event.preventDefault();
  openSettings();
};
$("#settings-close").onclick = (event) => {
  event.preventDefault();
  $("#settings-dialog").close();
};
$("#default-persona").onchange = saveDefaults;
$("#default-effort").onchange = saveDefaults;
$("#persona-pick").onchange = (event) => showPersona(/** @type {HTMLSelectElement} */ (event.target).value);
$("#persona-new").onclick = (event) => {
  event.preventDefault();
  $("#persona-pick").value = "";
  showPersona("");
  $("#persona-name").focus();
};
$("#persona-save").onclick = async (event) => {
  event.preventDefault();
  const name = $("#persona-name").value.trim();
  if (!name) {
    $("#persona-note").textContent = "A persona needs a name.";
    return;
  }
  const reply = await fetch(`/api/personas/${encodeURIComponent(name)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
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
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title: /** @type {HTMLInputElement} */ (event.target).value }),
  });
  loadSessions();
};
$("#effort").onchange = async (event) => {
  await api(`/api/sessions/${state.sessionId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ reasoning_effort: /** @type {HTMLSelectElement} */ (event.target).value }),
  });
};
/** @param {number | string} value */
function showTemperature(value) {
  // The slider and its readout are one control; setting the value without
  // the label leaves the number lying about what is selected.
  $("#temp").value = String(value);
  $("#temp-value").textContent = Number(value).toFixed(1);
}

// `input` fires per step, which would PATCH on every pixel of a drag, so the
// session is written on `change` -- when the handle is let go.
$("#temp").oninput = (event) => {
  $("#temp-value").textContent = Number(/** @type {HTMLInputElement} */ (event.target).value).toFixed(1);
};
$("#temp").onchange = async (event) => {
  await api(`/api/sessions/${state.sessionId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ temperature: Number(/** @type {HTMLInputElement} */ (event.target).value) }),
  });
};
$("#default-temp").oninput = (event) => {
  $("#default-temp-value").textContent = Number(/** @type {HTMLInputElement} */ (event.target).value).toFixed(1);
};
$("#default-temp").onchange = saveDefaults;

$("#persona").onchange = async (event) => {
  await api(`/api/sessions/${state.sessionId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ persona: /** @type {HTMLSelectElement} */ (event.target).value }),
  });
};
$("#pick-folder").onclick = (event) => {
  event.preventDefault();
  openFolders();
};
$("#folder-up").onclick = (event) => {
  event.preventDefault();
  openFolders($("#folder-dialog").dataset.parent);
};
$("#folder-cancel").onclick = (event) => {
  event.preventDefault();
  $("#folder-dialog").close();
};
$("#folder-use").onclick = async (event) => {
  event.preventDefault();
  const path = /** @type {string} */ ($("#folder-dialog").dataset.path);
  await api(`/api/sessions/${state.sessionId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
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
  try {
    return localStorage.getItem("saddle.theme");
  } catch {
    return null;
  }
}
function systemTheme() {
  try {
    return matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  } catch {
    return "dark";
  }
}
/** @param {string | null} theme */
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme === "light" ? "light" : "dark";
}
function initTheme() {
  applyTheme(storedTheme() || systemTheme());
  try {
    matchMedia("(prefers-color-scheme: dark)").addEventListener("change", (event) => {
      if (!storedTheme()) applyTheme(event.matches ? "dark" : "light"); // only while unset
    });
  } catch {
    /* older browsers: no live follow, the initial read still holds */
  }
}
$("#theme-toggle").onclick = () => {
  const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
  try {
    localStorage.setItem("saddle.theme", next);
  } catch {
    /* per-browser nicety only */
  }
  applyTheme(next);
};
initTheme();

boot();
