"""Sessions and personas: what the sidebar lists and what the system prompt says.

A session is a directory: metadata plus the message history as JSONL, so a
crash loses at most the turn in flight and a session can be read with `cat`.
Nothing here is a database -- the transcript is the record, the same way the
journal is for a run.

Personas are named system prompts. They are data, not code, so adding one is
editing a JSON file rather than a release.
"""

from __future__ import annotations

import json
import shutil
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Final

from saddle.engine import CHAT_TEMPERATURE

DEFAULT_ROOT: Final = Path.home() / ".saddle" / "sessions"

DEFAULT_WORKDIR: Final = Path.home() / "saddle-ranch"
"""Where new sessions work unless told otherwise.

Not the current directory, which meant a session's folder depended on where
the server happened to be started from -- and not $HOME either, which is a
lot of blast radius for a default. A named folder is somewhere a session can
make a mess without it mattering."""
SETTINGS_FILE: Final = "settings.json"
PERSONA_FILE: Final = "personas.json"
DEFAULT_TITLE: Final = "New session"
RUNS_FILE: Final = "runs.json"
"""A session's run index: one small row per task started in it, beside its
`session.json`. The ledger is the record of what a run did; this is only
enough to list it in the sidebar after the server restarts."""
UNDO_DELETE_S: Final = 10.0
"""How long a deleted session can be brought back. Deleting marks the
session; the directory goes only once this has passed."""
DEFAULT_PERSONA: Final = "engineer"
DEFAULT_EFFORT: Final = "xhigh"
DEFAULT_TEMPERATURE: Final = CHAT_TEMPERATURE
"""One definition, imported. Holding the number in two places meant the
session's value always won and the engine's was decorative: changing
CHAT_TEMPERATURE alone moved nothing, which is a divergence no test could
see because both happened to read 1.0."""
MIN_TEMPERATURE: Final = 0.0
MAX_TEMPERATURE: Final = 2.0
MAX_PERSONA_NAME: Final = 40

BUILTIN_PERSONAS: Final[dict[str, str]] = {
    "engineer": (
        "You are a careful software engineer working in the user's repository. "
        "Read before you write. Prefer small, verifiable changes. When you run "
        "a long command, start it in the background and say what you started. "
        "State plainly when something failed and what the error was.\n\n"
        "Keep the user with you. Open with a sentence saying what you are "
        "about to do, before any long stretch of work. Say what you found "
        "between steps rather than saving it all for the end, and stop to "
        "ask when a choice is the user's to make. A long silence with "
        "nothing but thinking in it is a worse answer than a short one that "
        "arrives."
    ),
    "reviewer": (
        "You review code for defects. You do not rewrite it unless asked. For "
        "each finding give the file, the line, the concrete failure it causes, "
        "and how to reproduce it. Say when you are uncertain."
    ),
    "explainer": (
        "You explain code and systems to someone competent who is new to this "
        "codebase. Lead with the shape of the thing, then the detail. Use the "
        "repository's own names. Do not invent behaviour you have not read."
    ),
    "plain": "",
}


# `SessionStore.list` shadows the builtin for every annotation after it in
# the class body, so the message list is named once here instead.
type Messages = list[dict[str, Any]]
type RunRows = list[dict[str, Any]]


SESSION_MODES: tuple[str, ...] = ("ask", "edit", "task")
"""The session's lane: what Enter does. ask: a conversation turn with
read-only tools. edit: a conversation turn that may write files and run
commands in the folder, unaudited. task: open the Run strip for an audited
`saddle auto` episode. A stored mode outside this set (the pre-lane "chat",
which was the default, not a choice) reads as ask: nothing was opted in."""


@dataclass
class Session:
    id: str
    title: str
    workdir: str
    persona: str = "engineer"
    system_prompt: str = ""
    reasoning_effort: str = "xhigh"
    temperature: float = DEFAULT_TEMPERATURE
    """0 is greedy and reproducible; higher samples more widely. Per session,
    because one conversation wanting a deterministic answer and the next
    wanting range is normal."""
    mode: str = "ask"
    """One of SESSION_MODES. A setting of the session like its persona, so a
    reload comes back in the mode it was left in. "ask" (read-only tools) is
    the default; "edit" writes into the folder unaudited and is opt-in."""
    auto_title: bool = True
    """False once someone renames the session by hand: a title the user chose
    is never overwritten by the model."""
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    deleted_at: float | None = None
    """Set by `trash`; the session is hidden and purged after UNDO_DELETE_S."""

    def __post_init__(self) -> None:
        if self.mode not in SESSION_MODES:
            self.mode = "ask"

    def prompt_text(self, personas: Mapping[str, str] | None = None) -> str:
        """The system prompt actually sent: an override, else the persona.

        `personas` is the store's table when there is one, so an edited or
        user-written persona is what actually reaches the model rather than
        the builtin of the same name.
        """
        if self.system_prompt.strip():
            return self.system_prompt
        table = BUILTIN_PERSONAS if personas is None else personas
        return table.get(self.persona, "")


class SessionStore:
    """Sessions on disk, one directory each."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root or DEFAULT_ROOT)
        self.root.mkdir(parents=True, exist_ok=True)
        # Creating a session reuses an unstarted one, which is a read
        # followed by a write: two clicks arriving together would both read
        # "none" and both create.
        self._create_lock = threading.Lock()

    # -- settings and personas ---------------------------------------------

    def _read_json(self, name: str) -> dict[str, Any]:
        try:
            data = json.loads((self.root / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_json(self, name: str, data: dict[str, Any]) -> None:
        (self.root / name).write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    @staticmethod
    def clamp_temperature(value: Any) -> float:
        """A temperature the server will accept, from whatever arrived.

        The knob is a slider, but the API takes anything, and vLLM refuses
        an out-of-range request outright rather than clipping -- which the
        UI would show as a turn that produced nothing.
        """
        try:
            number = float(value)
        except (TypeError, ValueError):
            return DEFAULT_TEMPERATURE
        return max(MIN_TEMPERATURE, min(MAX_TEMPERATURE, number))

    def settings(self) -> dict[str, Any]:
        """What a new session starts as, with the shipped values as the floor.

        New sessions used to start at "engineer"/"xhigh" whatever the last
        one was set to, which reads as the app forgetting. Making that a
        stated default rather than a reset means the behaviour is chosen.
        """
        stored = self._read_json(SETTINGS_FILE)
        return {
            "persona": str(stored.get("persona") or DEFAULT_PERSONA),
            "reasoning_effort": str(stored.get("reasoning_effort") or DEFAULT_EFFORT),
            "temperature": (
                DEFAULT_TEMPERATURE
                if stored.get("temperature") is None
                else self.clamp_temperature(stored.get("temperature"))
            ),
        }

    def update_settings(self, **changes: Any) -> dict[str, Any]:
        current = self.settings()
        for key, value in changes.items():
            if key not in current or value is None:
                continue
            # `if value:` would silently drop a temperature of 0 -- the one
            # setting whose most meaningful value is falsy.
            if key == "temperature":
                current[key] = self.clamp_temperature(value)
            elif value != "":
                current[key] = str(value)
        self._write_json(SETTINGS_FILE, current)
        return current

    def personas(self) -> dict[str, str]:
        """Builtin personas, overlaid with the user's own.

        A stored entry under a builtin name shadows it, which is what makes
        "edit engineer" possible; deleting that entry restores the builtin.
        """
        table = dict(BUILTIN_PERSONAS)
        for name, prompt in self._read_json(PERSONA_FILE).items():
            if isinstance(prompt, str):
                table[str(name)] = prompt
        return table

    def custom_personas(self) -> dict[str, str]:
        """Only the stored ones -- what the editor may delete or reset."""
        return {
            str(name): prompt
            for name, prompt in self._read_json(PERSONA_FILE).items()
            if isinstance(prompt, str)
        }

    def save_persona(self, name: str, prompt: str) -> dict[str, str]:
        clean = name.strip()
        if not clean:
            msg = "a persona needs a name"
            raise ValueError(msg)
        if len(clean) > MAX_PERSONA_NAME:
            msg = f"persona name is longer than {MAX_PERSONA_NAME} characters"
            raise ValueError(msg)
        stored = self.custom_personas()
        stored[clean] = prompt
        self._write_json(PERSONA_FILE, dict(stored))
        return self.personas()

    def delete_persona(self, name: str) -> dict[str, str]:
        """Remove a stored persona. A builtin of that name comes back."""
        stored = self.custom_personas()
        if name not in stored:
            msg = f"no editable persona named {name!r}"
            raise KeyError(msg)
        del stored[name]
        self._write_json(PERSONA_FILE, dict(stored))
        return self.personas()

    def _dir(self, session_id: str) -> Path:
        # A session id is generated here and never taken from a client, but
        # the join is still guarded: an id containing "../" would otherwise
        # address any directory on the machine.
        safe = "".join(c for c in session_id if c.isalnum() or c in "-_")
        if not safe or safe != session_id:
            msg = f"invalid session id {session_id!r}"
            raise ValueError(msg)
        return self.root / safe

    def unstarted(
        self, *, persona: str, reasoning_effort: str, temperature: float
    ) -> Session | None:
        """An *untouched* session: unwritten, and set up the way a new one would be.

        Clicking "new session" five times should not leave five identical
        empty sessions, so an unwritten session is handed back rather than
        another being made. But "unwritten" is not enough on its own, and
        assuming it was is what broke picking a new persona: with a leftover
        session sitting at the old persona, asking for a new one returned
        that, silently ignoring the persona just chosen. The session looked
        new and answered as the old one.

        So a session is only the same session while it still matches what a
        fresh one would be. Configure it -- a persona, a thinking level, a
        system prompt -- and it is yours; asking for a new session then
        gives a new session rather than handing back your setup.
        """
        for session in self.list():
            if self.load_messages(session.id):
                continue
            if session.persona != persona or session.reasoning_effort != reasoning_effort:
                continue
            if session.temperature != temperature:
                continue
            if session.system_prompt.strip():
                continue
            return session
        return None

    def create(
        self,
        *,
        title: str = DEFAULT_TITLE,
        workdir: str = ".",
        persona: str | None = None,
        reasoning_effort: str | None = None,
        temperature: float | None = None,
        reuse_unstarted: bool = False,
    ) -> Session:
        defaults = self.settings()
        want_persona = persona or defaults["persona"]
        want_effort = reasoning_effort or defaults["reasoning_effort"]
        want_temp = (
            defaults["temperature"]
            if temperature is None
            else self.clamp_temperature(temperature)
        )
        with self._create_lock:
            if reuse_unstarted:
                existing = self.unstarted(
                    persona=want_persona,
                    reasoning_effort=want_effort,
                    temperature=want_temp,
                )
                if existing is not None:
                    # Honour a folder the caller asked for; leave the rest,
                    # since the session may already have been set up by hand.
                    resolved = str(Path(workdir).expanduser().resolve())
                    if resolved != existing.workdir:
                        return self.update(existing.id, workdir=resolved)
                    return existing
            return self._create(
                title=title,
                workdir=workdir,
                persona=want_persona,
                reasoning_effort=want_effort,
                temperature=want_temp,
            )

    def _create(
        self,
        *,
        title: str,
        workdir: str,
        persona: str,
        reasoning_effort: str,
        temperature: float,
    ) -> Session:
        session = Session(
            id=uuid.uuid4().hex[:12],
            title=title,
            workdir=str(Path(workdir).expanduser().resolve()),
            persona=persona,
            reasoning_effort=reasoning_effort,
            temperature=temperature,
        )
        directory = self._dir(session.id)
        (directory / "uploads").mkdir(parents=True, exist_ok=True)
        self._save_meta(session)
        (directory / "messages.jsonl").touch()
        return session

    def _save_meta(self, session: Session) -> None:
        session.updated = time.time()
        path = self._dir(session.id) / "session.json"
        path.write_text(json.dumps(asdict(session), indent=1), encoding="utf-8")

    def get(self, session_id: str) -> Session:
        data = json.loads((self._dir(session_id) / "session.json").read_text(encoding="utf-8"))
        return Session(**data)

    def update(self, session_id: str, **changes: Any) -> Session:
        session = self.get(session_id)
        for key, value in changes.items():
            if hasattr(session, key) and value is not None:
                setattr(session, key, value)
        self._save_meta(session)
        return session

    def list(self, *, include_deleted: bool = False) -> list[Session]:
        out: list[Session] = []
        for directory in self.root.iterdir():
            meta = directory / "session.json"
            if meta.is_file():
                try:
                    session = Session(**json.loads(meta.read_text(encoding="utf-8")))
                except (ValueError, TypeError):
                    continue  # a half-written session must not break the list
                if session.deleted_at is None or include_deleted:
                    out.append(session)
        return sorted(out, key=lambda s: s.updated, reverse=True)

    def delete(self, session_id: str) -> None:
        shutil.rmtree(self._dir(session_id), ignore_errors=True)

    def trash(self, session_id: str, *, now: float | None = None) -> Session:
        """Hide a session, keeping it on disk so it can be restored."""
        session = self.get(session_id)
        session.deleted_at = time.time() if now is None else now
        path = self._dir(session_id) / "session.json"
        path.write_text(json.dumps(asdict(session), indent=1), encoding="utf-8")
        return session

    def restore(self, session_id: str) -> Session:
        session = self.get(session_id)
        session.deleted_at = None
        path = self._dir(session_id) / "session.json"
        path.write_text(json.dumps(asdict(session), indent=1), encoding="utf-8")
        return session

    def purge_expired(self, *, now: float | None = None) -> set[str]:
        """Delete every trashed session whose undo window has passed."""
        clock = time.time() if now is None else now
        gone: set[str] = set()
        for session in self.list(include_deleted=True):
            if session.deleted_at is not None and clock - session.deleted_at >= UNDO_DELETE_S:
                self.delete(session.id)
                gone.add(session.id)
        return gone

    # -- runs --------------------------------------------------------------

    def runs(self, session_id: str) -> RunRows:
        try:
            data = json.loads((self._dir(session_id) / RUNS_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []

    def record_run(self, session_id: str, row: dict[str, Any]) -> None:
        """Insert or update one run's row (keyed by `run_id`) in the index."""
        rows = self.runs(session_id)
        for i, old in enumerate(rows):
            if old.get("run_id") == row["run_id"]:
                rows[i] = {**old, **row}
                break
        else:
            rows.append(dict(row))
        path = self._dir(session_id) / RUNS_FILE
        if not path.parent.is_dir():
            return  # the session was purged while its run was still going
        path.write_text(json.dumps(rows, indent=1, ensure_ascii=False), encoding="utf-8")

    # -- messages ----------------------------------------------------------

    def messages_path(self, session_id: str) -> Path:
        return self._dir(session_id) / "messages.jsonl"

    def load_messages(self, session_id: str) -> Messages:
        path = self.messages_path(session_id)
        if not path.is_file():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue  # a torn tail loses one message, not the session
        return out

    def save_messages(self, session_id: str, messages: Messages) -> None:
        path = self.messages_path(session_id)
        path.write_text(
            "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in messages),
            encoding="utf-8",
        )
        self._save_meta(self.get(session_id))

    def uploads_dir(self, session_id: str) -> Path:
        path = self._dir(session_id) / "uploads"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def undo_dir(self, session_id: str) -> Path:
        """Where a session's file snapshots live, beside its transcript."""
        return self._dir(session_id) / "undo"

    def journal_path(self, session_id: str) -> Path:
        return self._dir(session_id) / "chat.jsonl"
