"use strict";
/* The sidebar's Runs group and the session delete toast.

   Runs: one row per run across every session, newest first, read from
   `/api/runs` (each session's run index on disk, with the server's live
   runs over it), so a run in a session that is not on screen -- above all
   one waiting on you -- is one click away, and the list survives a server
   restart. Times tick on the page's own clock; the server is asked again
   only on the poll.

   Delete: the row goes at once, but the session is only hidden. A toast
   offers Undo for UNDO_MS; when it runs out the page asks for the real
   delete, and the server purges anything hidden longer than that anyway. */

const RUN_WORDS = {
  running: "running", needs_you: "needs you", finished: "finished",
  stopped: "stopped", unchanged: "unchanged", asked: "needs you", failed: "no outcome", interrupted: "interrupted",
};
const RUN_ENDED_STATES = new Set(["finished", "stopped", "unchanged", "asked", "failed"]);
const UNDO_MS = 10000;
const runsView = { rows: [], skew: 0, timer: 0, undoMs: UNDO_MS };

function firstWords(text, n = 6) {
  const words = String(text || "").trim().split(/\s+/).filter(Boolean);
  return words.slice(0, n).join(" ") + (words.length > n ? "…" : "");
}

function shortAge(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

/* A run the index says was going, that no live server holds: cut off. */
function shownState(run) {
  if (!run.live && !RUN_ENDED_STATES.has(run.state)) return "interrupted";
  return run.state;
}

function runMeta(run) {
  const now = Date.now() / 1000 + runsView.skew;
  const st = shownState(run);
  if (st === "needs_you") return `waiting ${shortAge(now - (run.state_since || now))}`;
  const end = RUN_ENDED_STATES.has(st) || st === "interrupted" ? (run.ended || run.state_since) : now;
  return shortAge((end || now) - (run.started || now));
}

function paintRunTimes() {
  for (const row of document.querySelectorAll("#run-list .run-row")) {
    const run = runsView.rows.find((r) => r.run_id === row.dataset.run);
    if (run) row.querySelector(".run-time").textContent = runMeta(run);
  }
}

function renderRuns(payload) {
  runsView.rows = payload.runs || [];
  runsView.skew = (payload.now || Date.now() / 1000) - Date.now() / 1000;
  const box = document.getElementById("run-list");
  if (!box) return;
  box.textContent = "";
  box.hidden = !runsView.rows.length;
  if (!runsView.rows.length) return;
  box.appendChild(el("div", "run-group-head", "Runs"));
  for (const run of runsView.rows) {
    const st = shownState(run);
    const row = el("button", `run-row rs-${st}`);
    row.type = "button";
    row.dataset.run = run.run_id;
    row.dataset.sid = run.session_id;
    row.dataset.state = st;
    row.title = `${RUN_WORDS[st] || st} · ${run.task} · in ${run.session_title}`;
    const dot = el("span", `run-state-dot rs-${st}`, "●");
    dot.setAttribute("aria-label", RUN_WORDS[st] || st);
    row.appendChild(dot);
    row.appendChild(el("span", "run-task", firstWords(run.task)));
    row.appendChild(el("span", "run-time", runMeta(run)));
    row.appendChild(el("span", "run-lane", run.lane || "small"));
    row.onclick = () => jumpToRun(run.session_id, run.run_id);
    box.appendChild(row);
  }
}

function loadRuns() {
  return api("/api/runs").then(renderRuns, () => {});
}

async function jumpToRun(sid, runId) {
  if (sid !== state.sessionId) select(sid);
  for (let i = 0; i < 50; i++) {
    const card = document.querySelector(`.task-card[data-run="${runId}"]`);
    if (card && state.sessionId === sid) {
      card.scrollIntoView({ block: "center" });
      card.classList.add("flash");
      setTimeout(() => card.classList.remove("flash"), 1600);
      return true;
    }
    await new Promise((r) => setTimeout(r, 100));
  }
  return false;
}

/* ---------- delete with undo ---------- */

const pendingDeletes = new Map();

function hideToast() {
  const toast = document.getElementById("toast");
  if (toast) { toast.hidden = true; toast.textContent = ""; }
}

async function deleteSession(session) {
  await api(`/api/sessions/${session.id}`, { method: "DELETE" });
  const wasActive = session.id === state.sessionId;
  if (wasActive) state.sessionId = null;
  const expire = setTimeout(() => {
    pendingDeletes.delete(session.id);
    fetch(`/api/sessions/${session.id}?now=1`, { method: "DELETE" }).catch(() => {});
    hideToast();
  }, runsView.undoMs);
  pendingDeletes.set(session.id, expire);
  if (wasActive) await boot(); else await loadSessions();
  const toast = document.getElementById("toast");
  toast.textContent = "";
  toast.appendChild(el("span", "toast-text", `Deleted “${session.title}”.`));
  const undo = el("button", "toast-undo", "Undo");
  undo.type = "button";
  undo.onclick = () => undoDelete(session.id, wasActive);
  toast.appendChild(undo);
  toast.hidden = false;
  undo.focus();
}

async function undoDelete(sid, reselect) {
  clearTimeout(pendingDeletes.get(sid));
  pendingDeletes.delete(sid);
  hideToast();
  await api(`/api/sessions/${sid}/restore`, { method: "POST" });
  if (reselect) select(sid); else await loadSessions();
}

document.addEventListener("keydown", (event) => {
  const toast = document.getElementById("toast");
  if (event.key === "Escape" && toast && !toast.hidden) hideToast();
});

runsView.timer = setInterval(paintRunTimes, 1000);
// Looked up on each tick, like notify.js's poll, so a test can take it out.
setInterval(() => loadRuns(), 3000);
