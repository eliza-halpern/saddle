"""Context compaction: keep a long session inside the window, visibly.

A chat that runs for hours outgrows any context window, and so does an
autonomous run that reads and tests for thirty minutes. The failure mode to
avoid is the silent one -- a harness that quietly drops the middle of a
conversation and then answers as though it still knew it.

So compaction here is **deterministic, staged, and reported**. No model call
is involved: a summariser that is itself a model call adds latency, a
failure mode, and a second thing to distrust (the Complexity Trap,
arXiv:2508.21433, measured observation masking matching an LLM summary on
solve rate at lower cost, with summaries lengthening trajectories). The
stages remove the bulkiest and least re-derivable material first, and what
was removed is always announced as a `Compaction` event.

Stage 1  truncate oversized tool results in older turns, keeping a head and
         a tail plus a marker, and every `FAILED `/`ERROR ` line between
         them (a pytest failure list is what the next step needs)
Stage 2  drop whole older exchanges, oldest first: an assistant message
         with tool calls always goes together with its tool results
         (OpenHands' tool-call matching; aider snaps its split the same
         way), replaced with one user-role note recording how many went
         and what they were about
Stage 3  still over: shrink tool results inside the recent tail too, all
         but the latest round's

Never removed: the system prompt, the most recent exchange, and the pinned
user message -- the task of an autonomous run (its first user message;
OpenHands `keep_first`, SWE-agent and the Complexity Trap keep the task
prompt verbatim) or, in a chat, the question being answered.

The note is a *user* message, never a second system message: the served
Qwen3.8 template raises "System message must be at the beginning." on a
system message after index 0. An autonomous run's note also carries a
state block rebuilt from the run's own records at every compaction
(`run_state`); the note is replaced, never compacted itself.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from typing import Any, Final, Literal

CHARS_PER_TOKEN: Final = 4
"""Deliberately crude. An exact tokeniser would tie compaction to one model,
and the decision this feeds is "is there room", not "how many exactly"."""

IMAGE_TOKENS: Final = 1200
"""What one image part is charged against the window."""

KEEP_RECENT: Final = 6
"""Messages at the tail that are never touched, whatever the pressure."""

RESULT_HEAD: Final = 1200
RESULT_TAIL: Final = 400
ELIDED: Final = "characters elided by compaction"
COMPACTION_ROLE: Final = "user"
"""Not "system": the served Qwen3.8 template raises on a system message after
index 0, and in an autonomous run a user-role note also keeps a user query in
the request once older turns are gone."""

FAILURE_PREFIXES: Final = ("FAILED ", "ERROR ")
"""Lines stage 1 keeps from the elided middle of a tool result, and the
state block keeps from the last test run: pytest's short test summary."""
FAILURE_LINES: Final = 50
"""At most this many such lines per result, so a 5,000-test failure cannot
refill the window it was elided from."""

NOTE_HEAD: Final = "[Earlier conversation compacted: "
_NOTE_COUNT: Final = re.compile(r"\[Earlier conversation compacted: (\d+) earlier message")
_NOTE_TOPICS: Final = "Dropped topics: "
TOPICS_KEPT: Final = 5

ASK_USER: Final = "Ask the user if you need detail that is no longer in context."
REREAD: Final = (
    "Re-read files and re-run commands with the tools if you need detail "
    "that is no longer in context."
)
"""The hint for an autonomous run: nobody is there to ask."""

EDIT_TOOLS: Final = ("edit_file", "write_file")

TEST_WORDS: Final = ("pytest", "unittest", "tox", "nox", "npm test", "cargo test", "go test")
"""A `run_command` whose command contains one of these is a test run, for
the state block's last test run and the task card's phase alike."""

Pin = Literal["first", "last"]
"""Which user message is never compacted: "first" is an autonomous run's
task, "last" is the chat question being answered."""


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """Rough token count for a message list.

    Counts kept reasoning (`reasoning_content`, the key the served template
    renders; `--keep-reasoning` sends it on every assistant message of the
    run): leaving it out under-counted one recorded run 1.78x.
    """
    total = 0
    for message in messages:
        content = message.get("content") or ""
        if isinstance(content, list):
            # Multimodal parts. Image data is base64 and enormous, so it is
            # counted at a flat rate rather than by length: charging a photo
            # 200,000 tokens would evict the whole conversation around it.
            for part in content:
                if part.get("type") == "text":
                    total += len(part.get("text") or "") // CHARS_PER_TOKEN
                else:
                    total += IMAGE_TOKENS
            continue
        total += len(content) // CHARS_PER_TOKEN
        total += len(message.get("reasoning_content") or "") // CHARS_PER_TOKEN
        for call in message.get("tool_calls") or []:
            total += len(str(call)) // CHARS_PER_TOKEN
    return total


def failure_lines(text: str) -> list[str]:
    """The `FAILED `/`ERROR ` lines of `text`, at most FAILURE_LINES of them."""
    return [ln for ln in text.splitlines() if ln.startswith(FAILURE_PREFIXES)][:FAILURE_LINES]


def _truncate_result(text: str) -> str:
    if len(text) <= RESULT_HEAD + RESULT_TAIL or ELIDED in text:
        return text  # short, or already elided: a compaction is never compacted
    dropped = len(text) - RESULT_HEAD - RESULT_TAIL
    kept = failure_lines(text[RESULT_HEAD:-RESULT_TAIL])
    kept_text = ("\n".join(kept) + "\n") if kept else ""
    return (
        text[:RESULT_HEAD]
        + f"\n\n[... {dropped} {ELIDED} ...]\n"
        + kept_text
        + "\n"
        + text[-RESULT_TAIL:]
    )


def _content_text(content: Any) -> str:
    """A message's content as plain text, whichever shape it is stored in.

    A string is the common case. An uploaded image makes it a list of
    content parts instead (`engine._user_message` builds
    `[{"type": "text", ...}, {"type": "image_url", ...}]`), so this takes
    the text of the first text part -- the same rule `engine._last_asked`
    applies to a retry, kept in sync by hand since `memory` must not import
    `engine`.
    """
    if isinstance(content, list):
        return next((p.get("text", "") for p in content if p.get("type") == "text"), "")
    return str(content or "")


def is_note(message: dict[str, Any]) -> bool:
    """Whether `message` is a compaction note (this module's own)."""
    content = message.get("content")
    return (
        message.get("role") == COMPACTION_ROLE
        and isinstance(content, str)
        and content.startswith(NOTE_HEAD)
    )


def pinned_index(messages: list[dict[str, Any]], pin: Pin) -> int:
    """The user message `pin` names, never a note; -1 if there is none."""
    users = [i for i, m in enumerate(messages) if m.get("role") == "user" and not is_note(m)]
    if not users:
        return -1
    return users[0] if pin == "first" else users[-1]


def is_test_command(command: str) -> bool:
    return any(word in command for word in TEST_WORDS)


def run_state(
    *,
    files: Sequence[str],
    test: tuple[str, str] | None,
    audit: str,
    spent_tokens: int,
    token_budget: int,
    elapsed_s: float,
    time_budget_s: float,
) -> str:
    """An autonomous run's state block, from the run's own records.

    Every field is a value the run's ledger also seals (the outcome
    sidecar's `files_changed`, the tool span of the last test command, the
    `audit:delivered` span, the `auto:spend` running total), read here from
    the engine's copy, which is not capped at the span's 500 characters. No
    model writes any of it. The idea is Aider's repo map and MemGPT's core
    memory: state regenerated deterministically each time, so it cannot
    drift or be summarised away.

    `files` are the paths changed against the run's starting commit, so a
    file edited and then put back is not listed. `test` is the newest test
    command and its result. `audit` is the newest audit text the model was
    *delivered*; an audit withheld from it (arm E+A) is never passed here.
    """
    lines = ["Run state, rebuilt from the run's records (not model-written):"]
    lines.append("- task: the first user message above, kept word for word")
    lines.append("- files changed since the run's starting commit: " + (", ".join(files) or "none"))
    if test is None:
        lines.append("- last test run: none yet")
    else:
        command, result = test
        body = result.strip().splitlines()
        exit_line = body[0] if body else ""
        tail = next((ln for ln in reversed(body[1:]) if ln.strip()), "")
        lines.append(f"- last test run: {command} -> {exit_line}" + (f"; {tail}" if tail else ""))
        lines += [f"  {ln}" for ln in failure_lines(result)]
    finding_lines = audit.strip().splitlines()
    unproven = [ln for ln in finding_lines if ln.startswith("(not proven")]
    findings = [ln for ln in finding_lines if not ln.startswith("(not proven")]
    if findings:
        lines.append("- latest audit delivered to you:")
        lines += [f"  {ln}" for ln in findings]
    else:
        lines.append("- latest audit delivered to you: none")
    lines.append("- not proven: " + ("" if unproven else "none"))
    lines += [f"  {ln}" for ln in unproven]
    lines.append(
        f"- budget: {spent_tokens} of {token_budget} generated tokens used, "
        f"{max(token_budget - spent_tokens, 0)} left; {elapsed_s:.0f}s of "
        f"{time_budget_s:.0f}s used, {max(time_budget_s - elapsed_s, 0):.0f}s left"
    )
    return "\n".join(lines)


def _edited_path(call: dict[str, Any]) -> str | None:
    fn = call.get("function") or {}
    if fn.get("name") not in EDIT_TOOLS:
        return None
    try:
        args = json.loads(fn.get("arguments") or "{}")
    except ValueError:
        return None
    path = args.get("path") if isinstance(args, dict) else None
    return path if isinstance(path, str) else None


def _previous(note: dict[str, Any]) -> tuple[int, list[str]]:
    """How many messages an older note said were dropped, and its topics."""
    text = str(note.get("content") or "")
    match = _NOTE_COUNT.match(text)
    topics = next(
        (
            ln.removeprefix(_NOTE_TOPICS).split("; ")
            for ln in text.splitlines()
            if ln.startswith(_NOTE_TOPICS)
        ),
        [],
    )
    return (int(match.group(1)) if match else 0), topics


def compact(
    messages: list[dict[str, Any]],
    *,
    limit_tokens: int,
    pin: Pin = "last",
    hint: str = ASK_USER,
    state: Callable[[], str] | None = None,
) -> tuple[int, str]:
    """Bring `messages` under `limit_tokens` in place.

    Returns (messages dropped, human summary). `(0, "")` means nothing was
    needed, which is the common case and must stay cheap. `state`, when
    given (an autonomous run), is called once per compaction and its text
    goes in the note, which is rebuilt, not appended to, each time.
    """
    if estimate_tokens(messages) <= limit_tokens:
        return 0, ""

    keep = pinned_index(messages, pin)

    def protected(index: int) -> bool:
        if messages[index].get("role") == "system" or index == keep:
            return True
        return index >= len(messages) - KEEP_RECENT

    # Stage 1: shrink old tool results.
    for index, message in enumerate(messages):
        if protected(index) or message.get("role") != "tool":
            continue
        content = message.get("content") or ""
        if len(content) > RESULT_HEAD + RESULT_TAIL:
            message["content"] = _truncate_result(content)

    old = next((i for i, m in enumerate(messages) if is_note(m)), None)
    block = state() if state is not None else ""
    if estimate_tokens(messages) <= limit_tokens:
        if old is not None and state is not None:
            count, topics = _previous(messages[old])
            messages[old] = _note(count, topics, [], block, hint)
        return 0, "large tool results elided"

    # Stage 2: drop the oldest exchanges, a tool call always with its results.
    count, topics = 0, []
    at = None
    if old is not None:
        count, topics = _previous(messages.pop(old))
        at = old
        keep = pinned_index(messages, pin)
    reserve = len(block) // CHARS_PER_TOKEN
    dropped = 0
    edited: list[str] = []
    fresh: list[str] = []
    while estimate_tokens(messages) + reserve > limit_tokens:
        oldest = next((i for i in range(len(messages)) if not protected(i)), None)
        if oldest is None:
            break  # nothing left that may be dropped; report what we managed
        end = oldest + 1
        while end < len(messages) and messages[end].get("role") == "tool":
            end += 1
        if any(protected(i) for i in range(oldest, end)):
            break  # the call's results are in the protected tail; never split them
        for message in messages[oldest:end]:
            dropped += 1
            for call in message.get("tool_calls") or []:
                path = _edited_path(call)
                if path is not None and path not in edited:
                    edited.append(path)
            if message.get("role") == "user":
                lines = _content_text(message.get("content")).strip().splitlines()
                if lines:
                    fresh.append(lines[0][:60])
        del messages[oldest:end]
        at = oldest if at is None else min(at, oldest)
        if keep > oldest:
            keep -= end - oldest

    # Stage 3: still over (the protected tail itself is too big): shrink tool
    # results in the tail too, all but the latest round's.
    if estimate_tokens(messages) + reserve > limit_tokens:
        last = max((i for i, m in enumerate(messages) if m.get("role") == "assistant"), default=-1)
        for message in messages[:last]:
            content = message.get("content") or ""
            if message.get("role") == "tool" and len(content) > RESULT_HEAD + RESULT_TAIL:
                message["content"] = _truncate_result(content)

    if not dropped and old is None:
        return 0, "large tool results elided"

    summary = f"{dropped} earlier message(s) compacted"
    if fresh:
        summary += ": " + "; ".join(fresh[:TOPICS_KEPT])
    # A chat has no run to rebuild state from: it names the files edited in
    # what went. An autonomous run's state block lists what is changed now.
    named = [] if state is not None else edited
    note = _note(count + dropped, (topics + fresh)[:TOPICS_KEPT], named, block, hint)
    messages.insert(at if at is not None else 0, note)
    return dropped, summary


def _note(
    count: int, topics: list[str], edited: list[str], block: str, hint: str
) -> dict[str, Any]:
    lines = [f"{NOTE_HEAD}{count} earlier message(s) dropped to fit the window.]"]
    if topics:
        lines.append(_NOTE_TOPICS + "; ".join(topics))
    if edited:
        lines.append("Files edited in the dropped part: " + ", ".join(edited))
    if block:
        lines.append(block)
    lines.append(hint)
    return {"role": COMPACTION_ROLE, "content": "\n".join(lines)}
