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
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Final

DEFAULT_ROOT: Final = Path.home() / ".saddle" / "sessions"

BUILTIN_PERSONAS: Final[dict[str, str]] = {
    "engineer": (
        "You are a careful software engineer working in the user's repository. "
        "Read before you write. Prefer small, verifiable changes. When you run "
        "a long command, start it in the background and say what you started. "
        "State plainly when something failed and what the error was."
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


@dataclass
class Session:
    id: str
    title: str
    workdir: str
    persona: str = "engineer"
    system_prompt: str = ""
    reasoning_effort: str = "xhigh"
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)

    def prompt_text(self) -> str:
        """The system prompt actually sent: an override, else the persona."""
        if self.system_prompt.strip():
            return self.system_prompt
        return BUILTIN_PERSONAS.get(self.persona, "")


class SessionStore:
    """Sessions on disk, one directory each."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root or DEFAULT_ROOT)
        self.root.mkdir(parents=True, exist_ok=True)

    def _dir(self, session_id: str) -> Path:
        # A session id is generated here and never taken from a client, but
        # the join is still guarded: an id containing "../" would otherwise
        # address any directory on the machine.
        safe = "".join(c for c in session_id if c.isalnum() or c in "-_")
        if not safe or safe != session_id:
            msg = f"invalid session id {session_id!r}"
            raise ValueError(msg)
        return self.root / safe

    def create(self, *, title: str = "New session", workdir: str = ".",
               persona: str = "engineer") -> Session:
        session = Session(id=uuid.uuid4().hex[:12], title=title,
                          workdir=str(Path(workdir).expanduser().resolve()),
                          persona=persona)
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

    def list(self) -> list[Session]:
        out: list[Session] = []
        for directory in self.root.iterdir():
            meta = directory / "session.json"
            if meta.is_file():
                try:
                    out.append(Session(**json.loads(meta.read_text(encoding="utf-8"))))
                except (ValueError, TypeError):
                    continue  # a half-written session must not break the list
        return sorted(out, key=lambda s: s.updated, reverse=True)

    def delete(self, session_id: str) -> None:
        shutil.rmtree(self._dir(session_id), ignore_errors=True)

    # -- messages ----------------------------------------------------------

    def messages_path(self, session_id: str) -> Path:
        return self._dir(session_id) / "messages.jsonl"

    def load_messages(self, session_id: str) -> list[dict[str, Any]]:
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

    def save_messages(self, session_id: str, messages: list[dict[str, Any]]) -> None:
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

    def journal_path(self, session_id: str) -> Path:
        return self._dir(session_id) / "chat.jsonl"


def personas() -> dict[str, str]:
    return dict(BUILTIN_PERSONAS)
