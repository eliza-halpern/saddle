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
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
};

const state = {
  sessionId: null, stream: null, busy: false,
  turnNode: null, assistantNode: null, reasoningNode: null, reasoningBody: null,
  tools: new Map(), attachments: [], personas: {}, folder: null,
};

/* ---------- content rendering ---------- */

/* A small markdown renderer. Everything is inserted as text nodes and
   built elements -- never innerHTML -- so model output cannot inject
   markup. It covers what a coding assistant actually emits: fenced code,
   headings, bullet and numbered lists, blockquotes, bold, italic, inline
   code and links. Anything it does not know stays as literal text, which
   is the safe failure: an unrendered asterisk is ugly, a swallowed line is
   a lie about what was said. */

const INLINE = /(`[^`]+`|\*\*[^*]+\*\*|__[^_]+__|\*[^*\n]+\*|_[^_\n]+_|\[[^\]]+\]\([^)\s]+\))/g;

function inlineInto(parent, text) {
  for (const bit of text.split(INLINE)) {
    if (!bit) continue;
    if (bit.startsWith("`") && bit.endsWith("`") && bit.length > 2) {
      parent.appendChild(el("code", null, bit.slice(1, -1)));
    } else if ((bit.startsWith("**") && bit.endsWith("**") && bit.length > 4) ||
               (bit.startsWith("__") && bit.endsWith("__") && bit.length > 4)) {
      parent.appendChild(el("strong", null, bit.slice(2, -2)));
    } else if ((bit.startsWith("*") && bit.endsWith("*") && bit.length > 2) ||
               (bit.startsWith("_") && bit.endsWith("_") && bit.length > 2)) {
      parent.appendChild(el("em", null, bit.slice(1, -1)));
    } else if (bit.startsWith("[") && bit.includes("](")) {
      const cut = bit.indexOf("](");
      const link = el("a", null, bit.slice(1, cut));
      const href = bit.slice(cut + 2, -1);
      link.href = /^https?:|^\//.test(href) ? href : "#";  // no javascript: hrefs
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      parent.appendChild(link);
    } else {
      parent.appendChild(document.createTextNode(bit));
    }
  }
}

function renderMarkdown(target, text) {
  target.textContent = "";
  const lines = text.split("\n");
  let index = 0;
  let list = null;

  const closeList = () => { list = null; };

  while (index < lines.length) {
    const line = lines[index];

    const fence = line.match(/^\s*```(\w*)\s*$/);
    if (fence) {                                   // fenced code
      closeList();
      const body = [];
      index += 1;
      while (index < lines.length && !/^\s*```/.test(lines[index])) body.push(lines[index++]);
      index += 1;
      const pre = el("pre");
      pre.appendChild(el("code", fence[1] ? `lang-${fence[1]}` : null, body.join("\n")));
      target.appendChild(pre);
      continue;
    }

    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    if (heading) {
      closeList();
      const node = el(`h${Math.min(heading[1].length + 2, 6)}`);
      inlineInto(node, heading[2]);
      target.appendChild(node);
      index += 1;
      continue;
    }

    const bullet = line.match(/^\s*[-*+]\s+(.*)$/);
    const numbered = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (bullet || numbered) {
      const wanted = bullet ? "ul" : "ol";
      if (!list || list.tagName.toLowerCase() !== wanted) {
        list = el(wanted);
        target.appendChild(list);
      }
      const item = el("li");
      inlineInto(item, (bullet || numbered)[1]);
      list.appendChild(item);
      index += 1;
      continue;
    }

    const quote = line.match(/^\s*>\s?(.*)$/);
    if (quote) {
      closeList();
      const node = el("blockquote");
      inlineInto(node, quote[1]);
      target.appendChild(node);
      index += 1;
      continue;
    }

    if (!line.trim()) { closeList(); index += 1; continue; }

    closeList();                                    // paragraph: gather until blank
    const para = [];
    while (index < lines.length && lines[index].trim() &&
           !/^\s*```|^#{1,4}\s|^\s*[-*+]\s|^\s*\d+[.)]\s|^\s*>/.test(lines[index])) {
      para.push(lines[index++]);
    }
    const node = el("p");
    inlineInto(node, para.join(" "));
    target.appendChild(node);
  }
}

function atBottom() {
  const t = $("#transcript");
  return t.scrollHeight - t.scrollTop - t.clientHeight < 120;
}
function stickToBottom(was) {
  if (was) $("#transcript").scrollTop = $("#transcript").scrollHeight;
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
  row.detail.textContent = event.detail || "(no output)";
  if (!event.ok) row.details.open = true;             // a failure should not need a click
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
    case "turn.start":
      newTurn(null);
      break;
    case "reasoning.delta":
      reasoningBlock();
      state.reasoningBody.textContent += event.text;   // never truncated
      break;
    case "content.delta": {
      const node = assistantBlock();
      node.dataset.raw += event.text;
      renderMarkdown(node, node.dataset.raw);
      break;
    }
    case "tool.start":
      if (!state.turnNode) newTurn(null);
      toolRow(event);
      break;
    case "tool.end":
      finishTool(event);
      break;
    case "compaction":
      notice(`Context compacted — ${event.summary}`);
      break;
    case "error":
      notice(event.message, "error");
      setStatus("error");
      break;
    case "turn.end":
      foldReasoning();                       // a turn with no answer text still folds
      setStatus("idle");
      state.busy = false;
      break;
    case "idle":
      setStatus("idle");
      state.busy = false;
      break;
  }
  stickToBottom(was);
}

function renderHistory(info) {
  $("#title").value = info.title;
  $("#folder-name").textContent = info.workdir.split("/").slice(-2).join("/") || info.workdir;
  state.folder = info.workdir;
  $("#persona").value = info.persona;
  if (info.reasoning_effort) $("#effort").value = info.reasoning_effort;
  const t = $("#transcript");
  t.textContent = "";
  const shown = (info.messages || []).filter((m) => m.role === "user" || m.role === "assistant");
  if (!shown.length) {
    t.appendChild(el("div", "empty", "nothing here yet — what are we making?"));
    return;
  }
  for (const message of shown) {
    const turn = el("div", "turn");
    if (message.role === "user") {
      turn.appendChild(el("div", "user", message.content || ""));
    } else {
      const node = el("div", "assistant");
      renderMarkdown(node, message.content || "");
      turn.appendChild(node);
    }
    t.appendChild(turn);
  }
  t.scrollTop = t.scrollHeight;
}

function connect(sessionId) {
  if (state.stream) state.stream.close();
  state.stream = new EventSource(`/api/sessions/${sessionId}/events`);
  state.stream.onmessage = (message) => handle(JSON.parse(message.data));
  state.stream.onerror = () => setStatus("error");
}

function setStatus(kind) {
  const node = $("#status");
  node.className = `status ${kind === "idle" ? "idle" : kind}`;
  node.textContent = kind === "working" ? "working" : kind;
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
    row.appendChild(el("span", "name", session.title));
    const kill = el("button", "kill", "×");
    kill.title = "Delete session";
    kill.onclick = async (event) => {
      event.stopPropagation();
      await fetch(`/api/sessions/${session.id}`, { method: "DELETE" });
      if (session.id === state.sessionId) state.sessionId = null;
      await boot();
    };
    row.appendChild(kill);
    row.onclick = () => select(session.id);
    list.appendChild(row);
  }
  return sessions;
}

function select(sessionId) {
  state.sessionId = sessionId;
  state.tools.clear();
  localStorage.setItem("saddle.session", sessionId);
  loadSessions();
  connect(sessionId);
}

async function boot() {
  state.personas = await api("/api/personas");
  const picker = $("#persona");
  picker.textContent = "";
  for (const name of Object.keys(state.personas)) {
    picker.appendChild(el("option", null, name)).value = name;
  }
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

async function send() {
  const input = $("#input");
  let text = input.value.trim();
  if (!text || state.busy) return;
  if (state.attachments.length) {
    const names = state.attachments.map((a) => a.name).join(", ");
    text += `\n\n[attached in uploads/: ${names}]`;
  }
  const turn = newTurn(input.value.trim());
  turn.scrollIntoView({ block: "end" });
  input.value = "";
  input.style.height = "auto";
  state.attachments = [];
  $("#attachments").textContent = "";
  state.busy = true;
  setStatus("working");
  try {
    await api(`/api/sessions/${state.sessionId}/message`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
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
    state.attachments.push(saved);
    const chip = el("span", "chip");
    const file = [...files].find((f) => f.name === saved.name);
    if (file && file.type.startsWith("image/")) {
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
  if (!$("#folder-dialog").open) $("#folder-dialog").showModal();
}

/* ---------- wiring ---------- */

$("#composer").addEventListener("submit", (event) => { event.preventDefault(); send(); });
$("#input").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); send(); }
});
$("#input").addEventListener("input", (event) => {
  event.target.style.height = "auto";
  event.target.style.height = Math.min(event.target.scrollHeight, 220) + "px";
});
$("#attach").onclick = () => $("#file-input").click();
$("#file-input").onchange = (event) => { upload(event.target.files); event.target.value = ""; };
$("#transcript").addEventListener("dragover", (e) => e.preventDefault());
$("#transcript").addEventListener("drop", (event) => {
  event.preventDefault();
  if (event.dataTransfer.files.length) upload(event.dataTransfer.files);
});
$("#new-session").onclick = async () => {
  const session = await api("/api/sessions", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ workdir: state.folder || "." }),
  });
  select(session.id);
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
  $("#folder-dialog").close();
};

boot();
