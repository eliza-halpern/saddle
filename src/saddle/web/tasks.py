"""A task started from the chat: `saddle auto`, watched from a card.

T5-7, decided: a chat turn that asks for a task starts a gated episode on
the *same* code path the command line uses. So this module owns no loop.
It calls `auto.run_auto` -- the function `saddle auto` calls -- and turns
what that run does into things the chat can show:

- engine events, wrapped as `TaskEvent`, for what is happening right now;
- the run's ledger, tailed and rendered as `TaskLine` session lines (T5-6),
  so every line on the card is a sealed record with a hash to cite;
- `TaskState` whenever the card's state changes: running, needs_you,
  finished, stopped, failed.

The final state is read from the ledger's outcome span by the packet
compiler, never from the thread that ran it, so the chat cannot show a
task as finished without a finish record.

When the run ends the chat journal gets a `run-ref` span naming the run's
journal and verdict, and the chat's own context gets the packet's text
recap (T5-9), never the run's transcript.
"""

from __future__ import annotations

import queue
import threading
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from saddle.auto import AutoError, AutoOptions, ledger_path, repo_root, run_auto
from saddle.events import (
    Answered,
    Event,
    Question,
    RunProgress,
    TaskEvent,
    TaskLine,
    TaskState,
)
from saddle.feed import Arm, default_auditor
from saddle.feed import AuditorFactory as FeedAuditorFactory
from saddle.journal import SpanRecord, append_span, build_span, read_entries, read_spans
from saddle.packet import compile_packet, render_packet_text
from saddle.transcript import session_line

RECAP_PREFIX: Final = "[saddle task "
"""How a run's recap message opens in the chat's stored messages. The page
recognises it and draws the task card in its place on reload."""

RUN_REF: Final = "run-ref"
"""The chat-journal span that references a run (T5-7 (3)). Not `run`:
`transcript.is_run_end` and `saddle tail` read a span named `run` as the
end of a slice run."""

ANSWER_POLL_S: Final = 0.25

type Audit = Callable[[str, str, str], Sequence[Event]]
type AuditorFactory = Callable[["TaskRun"], Audit | None]


@dataclass
class TaskRun:
    """One chat-started run, while and after it runs."""

    run_id: str
    session_id: str
    task: str
    time_budget_s: float
    token_budget: int
    state: str = "running"
    journal: Path | None = None
    question: Question | None = None
    cancelled: bool = False
    lines: list[TaskLine] = field(default_factory=list)
    answers: queue.Queue[str] = field(default_factory=queue.Queue)
    seen: int = 0
    progress: TaskEvent | None = None
    """The last spend report, so a page that reconnects mid-run shows it."""
    lock: threading.Lock = field(default_factory=threading.Lock)

    def state_event(self, detail: str = "") -> TaskState:
        return TaskState(
            run_id=self.run_id,
            state=self.state,
            task=self.task,
            detail=detail,
            time_budget_s=self.time_budget_s,
            token_budget=self.token_budget,
            question=(
                {
                    "id": self.question.id,
                    "text": self.question.text,
                    "options": self.question.options,
                }
                if self.question is not None and self.state == "needs_you"
                else None
            ),
        )

    def new_lines(self) -> list[TaskLine]:
        """Ledger entries sealed since the last call, as session lines."""
        if self.journal is None or not self.journal.is_file():
            return []
        with self.lock:
            entries = read_entries(self.journal)
            fresh = entries[self.seen :]
            self.seen = len(entries)
            out = []
            for entry in fresh:
                line = session_line(entry)
                if line is None:
                    continue
                task_line = TaskLine(
                    run_id=self.run_id,
                    mark=line.mark,
                    text=line.text,
                    cite=line.cite,
                    tone=line.tone,
                )
                self.lines.append(task_line)
                out.append(task_line)
            return out

    def wait_for_answer(self, question: Question) -> str | None:
        """Block the run until the user answers, or None if it is stopped."""
        while not self.cancelled:
            try:
                return self.answers.get(timeout=ANSWER_POLL_S)
            except queue.Empty:
                continue
        return None


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def recap_message(run: TaskRun, recap: str) -> dict[str, Any]:
    """What the chat's model sees of a run: the compiled recap, not the transcript."""
    return {
        "role": "user",
        "content": (
            f"{RECAP_PREFIX}{run.run_id}] {run.task}\n\n"
            "Recap compiled from the run's ledger (not model-written):\n"
            f"{recap}"
        ),
    }


def run_ref_span(run: TaskRun, verdict: str, detail: str, run_span: str) -> SpanRecord:
    return build_span(
        node_id=f"task:{run.run_id}",
        argv=[RUN_REF, run.run_id, run.task, str(run.journal), run_span],
        duration_ms=0,
        exit_code={"finished": 0, "stopped": 3}.get(verdict, 1),
        detail=f"{verdict}: {detail}",
        kind="agent",
        name=RUN_REF,
    )


def journal_for(chat_journal: Path, run_id: str) -> Path | None:
    """The run journal a session's own chat journal names for `run_id`.

    The packet endpoint reads only a path this session recorded, so a
    crafted run id cannot point it at an arbitrary file.
    """
    if not chat_journal.is_file():
        return None
    for span in read_spans(chat_journal):
        if span.name == RUN_REF and len(span.argv) >= 4 and span.argv[1] == run_id:
            return Path(span.argv[3])
    return None


def execute(
    run: TaskRun,
    *,
    workdir: Path,
    client: Any,
    publish: Callable[[Event], None],
    chat_journal: Path,
    reasoning_effort: str,
    audit: Audit | None,
    arm: Arm = "E+A+F",
    feed_auditor: FeedAuditorFactory = default_auditor,
    allow_test_edits: bool = False,
) -> tuple[str, dict[str, Any] | None]:
    """Run the task to its end; return its ledger verdict and the recap message.

    The one call that does the work is `run_auto`, exactly as `saddle auto`
    makes it; everything else here is watching. `arm` defaults to
    `saddle auto`'s own default, E+A+F: the audit feed (`feed.AuditFeed`,
    the real `auditor.Auditor`) is the source of findings, and they reach
    the card as the ledger's `audit-tier<N>:<gate>` and `audit:delivered`
    lines. `audit` is the chat's question seam beside it.
    """
    try:
        root = repo_root(workdir)
    except AutoError as exc:
        run.state = "failed"
        publish(run.state_event(str(exc)))
        return "failed", None
    run.journal = ledger_path(root, run.run_id)

    def on_event(event: Event) -> None:
        wrapped = TaskEvent(run_id=run.run_id, event=event.payload())
        publish(wrapped)
        if isinstance(event, RunProgress):
            run.progress = wrapped
        if isinstance(event, Question):
            run.question = event
            run.state = "needs_you"
        elif isinstance(event, Answered):
            run.question = None
            run.state = "running"
        for line in run.new_lines():
            publish(line)
        if isinstance(event, Question | Answered):
            publish(run.state_event())

    options = AutoOptions(
        task=run.task,
        repo=workdir,
        run_id=run.run_id,
        time_budget_s=run.time_budget_s,
        token_budget=run.token_budget,
        reasoning_effort=reasoning_effort,
        arm=arm,
        auditor_factory=feed_auditor,
        allow_test_edits=allow_test_edits,
    )
    try:
        run_auto(
            options,
            client,
            on_event=on_event,
            audit=audit,
            answer=run.wait_for_answer,
            cancel=lambda: run.cancelled,
        )
    except AutoError as exc:
        run.state = "failed"
        publish(run.state_event(str(exc)))
        return "failed", None
    for line in run.new_lines():
        publish(line)
    packet = compile_packet(run.journal, run_id=run.run_id)
    run.state = packet.verdict if packet.verdict in ("finished", "stopped") else "failed"
    start = next((s for s in read_spans(run.journal) if s.name == "auto:start"), None)
    append_span(
        chat_journal,
        run_ref_span(run, packet.verdict, packet.verdict_text, start.span_id if start else ""),
    )
    publish(run.state_event(packet.verdict_text))
    return packet.verdict, recap_message(run, render_packet_text(packet))
