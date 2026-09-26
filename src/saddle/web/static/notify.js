"use strict";
/* A run finds you (UX review, brief 1): the tab title, the favicon, a
   browser notification and the sidebar all say what a run is doing, so a
   run that needs you or has ended is visible from another tab or another
   session.

   One entry point, `runState(sid, state, task)`, is fed by the active
   session's event stream (tasks.js) and by a poll of `/api/sessions` for
   every other session. It paints the sidebar dot for any session, the tab
   for the session on screen, and speaks and notifies only on a transition
   it saw happen -- never for a state it merely found on load.

   Notifications are opt-in twice over: the browser's permission, asked for
   only when "Notify me" is clicked, and this page's remembered choice. And
   they fire only while the tab is hidden; a visible tab has the title,
   the card and the live region already. */

const RUN_LOOK = {
  running:   { glyph: "●", word: "running",    color: "#7fb6e6" },
  needs_you: { glyph: "?", word: "needs you",  color: "#ff9d4d" },
  finished:  { glyph: "✓", word: "finished",   color: "#5fcf86" },
  stopped:   { glyph: "■", word: "stopped",    color: "#e8697a" },
  failed:    { glyph: "!", word: "no outcome", color: "#e8697a" },
};
const RUN_LIVE = new Set(["running", "needs_you"]);
const RUN_ENDED = new Set(["finished", "stopped", "failed"]);
const NOTIFY_KEY = "saddle.notify";

const runWatch = {
  seen: new Map(),      // session id -> the last run state this page saw
  names: new Map(),     // session id -> title
  tasks: new Map(),     // session id -> the latest run's task
  current: null,        // { state, task } shown in the tab, or null when idle
};

/* A dot with a dark ring on a light halo, so it reads on a dark tab strip
   and a light one alike. Idle is a hollow rose ring. */
function faviconFor(runState) {
  const look = RUN_LOOK[runState];
  const body = look
    ? `<circle cx='8' cy='8' r='6.5' fill='#fff'/><circle cx='8' cy='8' r='5' fill='${look.color}' stroke='#211d22' stroke-width='1.5'/>`
    : `<circle cx='8' cy='8' r='6.5' fill='#fff'/><circle cx='8' cy='8' r='4.5' fill='none' stroke='#d9749a' stroke-width='2.5'/>`;
  return `data:image/svg+xml,${encodeURIComponent(`<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'>${body}</svg>`)}`;
}

function sessionName(sid) {
  return runWatch.names.get(sid) || "saddle";
}

function paintTab() {
  const now = runWatch.current;
  const name = sessionName(state.sessionId);
  const look = now && RUN_LOOK[now.state];
  document.title = look ? `${look.glyph} ${look.word} · ${now.task || name}` : name;
  let link = document.querySelector("link[rel='icon']");
  if (!link) {
    link = document.createElement("link");
    link.rel = "icon";
    document.head.appendChild(link);
  }
  link.href = faviconFor(look ? now.state : null);
}

function paintDot(sid, runState) {
  const row = document.querySelector(`.session[data-sid="${sid}"]`);
  if (!row) return;
  let dot = row.querySelector(".run-dot");
  const look = RUN_LOOK[runState];
  if (!look) {
    if (dot) dot.remove();
    return;
  }
  if (!dot) {
    dot = document.createElement("span");
    row.insertBefore(dot, row.querySelector(".kill"));
  }
  dot.className = `run-dot rs-${runState}`;
  dot.dataset.state = runState;
  dot.textContent = look.word;
  dot.title = `Latest run: ${look.word}`;
}

function announce(text) {
  const live = document.getElementById("run-live");
  if (live) live.textContent = text;
}

function notifyPref() {
  try { return localStorage.getItem(NOTIFY_KEY) === "on"; } catch { return false; }
}

function maybeNotify(sid, runState, task) {
  if (document.visibilityState !== "hidden") return;
  if (typeof Notification === "undefined" || Notification.permission !== "granted") return;
  if (!notifyPref()) return;
  const look = RUN_LOOK[runState];
  const note = new Notification(`${look.glyph} ${look.word} · ${sessionName(sid)}`,
                                { body: task || "", tag: `saddle-${sid}` });
  note.onclick = () => {
    window.focus();
    if (sid !== state.sessionId) select(sid);
    note.close();
  };
}

function runState(sid, runStateNow, task) {
  const prev = runWatch.seen.get(sid);
  runWatch.seen.set(sid, runStateNow);
  if (task) runWatch.tasks.set(sid, task);
  paintDot(sid, runStateNow);
  const changed = prev !== runStateNow;
  if (sid === state.sessionId && changed) {
    runWatch.current = RUN_LOOK[runStateNow] ? { state: runStateNow, task: runWatch.tasks.get(sid) } : null;
    paintTab();
  }
  if (!changed || prev === undefined || !RUN_LOOK[runStateNow]) return;
  const look = RUN_LOOK[runStateNow];
  const where = sid === state.sessionId ? "" : ` in ${sessionName(sid)}`;
  announce(`Task ${look.word}${where}: ${runWatch.tasks.get(sid) || ""}`);
  if (runStateNow === "needs_you" || RUN_ENDED.has(runStateNow)) maybeNotify(sid, runStateNow, runWatch.tasks.get(sid));
}

/* Called with every `/api/sessions` listing, from loadSessions and the poll. */
function noteSessions(sessions) {
  for (const s of sessions) {
    runWatch.names.set(s.id, s.title);
    runState(s.id, s.run_state || null, s.run_task);
  }
  paintTab();
}

/* The tab is back on the session it shows: a live run stays in the title,
   an ended one has been seen and the tab returns to the session name. */
function runSeen() {
  if (document.visibilityState === "hidden") return;
  if (runWatch.current && RUN_ENDED.has(runWatch.current.state)) {
    runWatch.current = null;
    paintTab();
  }
}

/* A different session on screen: show its run if one is live. */
function runSelected(sid) {
  const st = runWatch.seen.get(sid);
  runWatch.current = RUN_LIVE.has(st) ? { state: st, task: runWatch.tasks.get(sid) } : null;
  paintTab();
}

function paintNotifyControl() {
  const button = document.getElementById("notify-toggle");
  if (!button) return;
  if (typeof Notification === "undefined") {
    button.textContent = "Notifications unavailable";
    button.disabled = true;
    return;
  }
  const on = Notification.permission === "granted" && notifyPref();
  button.setAttribute("aria-pressed", String(on));
  button.textContent = Notification.permission === "denied"
    ? "Notifications blocked"
    : on ? "Notifying when hidden" : "Notify me";
  button.title = Notification.permission === "denied"
    ? "The browser blocks notifications for this page; allow them in its site settings."
    : "Notify when a run needs you or ends while this tab is in the background.";
}

async function toggleNotify() {
  if (typeof Notification === "undefined") return;
  const wasOn = Notification.permission === "granted" && notifyPref();
  let permission = Notification.permission;
  if (permission === "default") permission = await Notification.requestPermission();
  try {
    localStorage.setItem(NOTIFY_KEY, permission === "granted" && !wasOn ? "on" : "off");
  } catch { /* storage refused: nothing is remembered, and nothing fires */ }
  paintNotifyControl();
}

function pollSessions() {
  api("/api/sessions").then(noteSessions, () => {});
}

document.addEventListener("visibilitychange", runSeen);
document.addEventListener("pointerdown", runSeen);
document.addEventListener("keydown", runSeen);
document.addEventListener("DOMContentLoaded", () => {
  const button = document.getElementById("notify-toggle");
  if (button) button.onclick = toggleNotify;
  paintNotifyControl();
});
// Looked up on each tick, so a test can take the poll out and see the
// event path alone.
setInterval(() => pollSessions(), 3000);
