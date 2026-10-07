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

Stage 0  clear the reasoning of every assistant message but the newest
         `REASONING_KEPT` (handed to `archive` whole). Old reasoning is the
         bulk least needed again; files the model is editing are not. When
         this alone reaches the target, nothing else is touched.
Stage 1  truncate oversized tool results in older turns, oldest first and only
         until the target is met, keeping a head and
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
from pathlib import PurePosixPath
from typing import Any, Final, Literal

from saddle.vision import is_image_followup

CHARS_PER_TOKEN: Final = 4
CALL_ARGS_SHOWN: Final = 300
"""How much of a call's arguments labels its result in an archive."""
"""Deliberately crude. An exact tokeniser would tie compaction to one model,
and the decision this feeds is "is there room", not "how many exactly"."""

IMAGE_TOKENS: Final = 1200
"""What one image part is charged against the window."""

KEEP_RECENT: Final = 6
"""Messages at the tail that are never touched, whatever the pressure."""

REASONING_KEPT: Final = 4
"""How many of the newest assistant messages keep their reasoning through a
compaction (stage 0). Keeping the latest reasoning scores as keeping all of it,
and dropping all of it costs more rounds and wall time (saddle's keep-reasoning
A/B; research library, "Reasoning retention in agent context")."""

REASONING_LINE: Final = (
    "Reasoning from earlier rounds was cleared to fit the window; the newest "
    f"{REASONING_KEPT} rounds keep theirs. What the earlier rounds established is in "
    "the files, the tool results and the run state."
)
"""The note's line once stage 0 has cleared any reasoning, kept on every later note."""

RESULT_HEAD: Final = 1200
RESULT_TAIL: Final = 400
ELIDED: Final = "characters elided by compaction"
IMAGE_ELIDED: Final = "[image elided by compaction; take the screenshot or read the file again]"
"""What an old tool image becomes (stage 1): a picture of the screen is stale
once a newer one exists, and costs IMAGE_TOKENS for as long as it stays. An
image the person attached is never elided: it cannot be taken again."""
SCREENSHOTS_KEPT: Final = 3
SCREENSHOTS_SLACK: Final = 3
"""Tool images kept and let pile up before `trim_screenshots` runs: the newest
KEPT stay, and the older ones go once there are more than KEPT + SLACK."""

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
_NOTE_UNREAD: Final = "File contents no longer in context: "
UNREAD_HOW: Final = ". Read each with read_file again before you edit it."
"""The note's line for files whose contents compaction took (#177): right
after one, a worker wrote "I don't remember the original body precisely" of
the file it was editing. A file counts while no intact `read_file` result or
`write_file` call for it is left; entries are `path` or `path (lines a-b)`."""

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


def describe_call(call: dict[str, Any]) -> str:
    """`name(arguments)` for an assistant's tool call, its arguments cut short."""
    function = call.get("function") or {}
    args = str(function.get("arguments") or "")
    if len(args) > CALL_ARGS_SHOWN:
        args = args[:CALL_ARGS_SHOWN] + "..."
    return f"{function.get('name', '?')}({args})"


def is_note(message: dict[str, Any]) -> bool:
    """Whether `message` is a compaction note (this module's own)."""
    content = message.get("content")
    return (
        message.get("role") == COMPACTION_ROLE
        and isinstance(content, str)
        and content.startswith(NOTE_HEAD)
    )


def trim_screenshots(messages: list[dict[str, Any]], *, limit: int | None = None) -> int:
    """Turn all but the newest SCREENSHOTS_KEPT tool images into IMAGE_ELIDED
    once more than SCREENSHOTS_KEPT + SCREENSHOTS_SLACK are in `messages`, in
    place; the number elided. A picture of the screen is stale once newer ones
    exist (live, 38 of them were ~46k of a ~52k-token context). They go in a
    batch, not one per action: each trim rewrites the prompt from the oldest
    picture on, which the server must then read again instead of reusing its
    cache. Images the person attached stay.

    `limit`, the most images the server takes in one request
    (`vision.image_limit`), also elides the oldest tool images until no more
    than `limit` images remain in all, the person's counted among them: a
    server that takes one image refuses the whole request otherwise."""
    shown = [
        m
        for m in messages
        if is_image_followup(m)
        and isinstance(m.get("content"), list)
        and any(p.get("type") != "text" for p in m["content"])
    ]
    elided = 0
    if len(shown) > SCREENSHOTS_KEPT + SCREENSHOTS_SLACK:
        for message in shown[:-SCREENSHOTS_KEPT]:
            elided += sum(p.get("type") != "text" for p in message["content"])
            message["content"] = _elide_images(message["content"])
    if limit is None:
        return elided
    total = sum(
        p.get("type") != "text"
        for m in messages
        if m.get("role") == "user" and isinstance(m.get("content"), list)
        for p in m["content"]
    )
    for message in shown:
        if total <= limit:
            break
        content = message["content"]
        for index, part in enumerate(content):
            if total <= limit:
                break
            if part.get("type") != "text":
                content[index] = {"type": "text", "text": IMAGE_ELIDED}
                total -= 1
                elided += 1
    return elided


def _elide_images(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        p if p.get("type") == "text" else {"type": "text", "text": IMAGE_ELIDED} for p in content
    ]


def pinned_index(messages: list[dict[str, Any]], pin: Pin) -> int:
    """The user message `pin` names, never a note and never the user-role message
    that carries a tool's image (a screen-acting chat has one per action, and
    pinning one dropped the person's task); -1 if there is none."""
    users = [
        i
        for i, m in enumerate(messages)
        if m.get("role") == "user" and not is_note(m) and not is_image_followup(m)
    ]
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
    lines.append(_budget_line(spent_tokens, token_budget, elapsed_s, time_budget_s))
    return "\n".join(lines)


def _budget_line(spent_tokens: int, token_budget: int, elapsed_s: float, time_s: float) -> str:
    """The state block's budget line. A budget of 0 or less is no limit, as the
    engine reads it (`engine.NO_LIMIT`, every run's default): read as a number,
    it told every uncapped run it had 0 tokens and 0 s left (#187)."""
    tokens = (
        f"{spent_tokens} of {token_budget} generated tokens used, "
        f"{max(token_budget - spent_tokens, 0)} left"
        if token_budget > 0
        else f"{spent_tokens} generated tokens used, no cap"
    )
    time = (
        f"{elapsed_s:.0f}s of {time_s:.0f}s used, {max(time_s - elapsed_s, 0):.0f}s left"
        if time_s > 0
        else f"{elapsed_s:.0f}s used, no time limit"
    )
    return f"- budget: {tokens}; {time}"


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


def _read_span(args: dict[str, Any]) -> str:
    """The lines a `read_file` call asked for, as the note names them."""
    offset, limit = args.get("offset"), args.get("limit")
    first = offset if isinstance(offset, int) else 1
    if isinstance(limit, int):
        return f" (lines {first}-{first + limit - 1})"
    return f" (from line {first})" if first > 1 else ""


def _file_bodies(messages: list[dict[str, Any]]) -> tuple[list[str], set[str]]:
    """Every file whose contents `messages` showed, as note entries in order,
    and the paths whose contents are still there whole: an unshortened
    `read_file` result, or a `write_file` call, which carries the body."""
    entries: list[str] = []
    reads: dict[str, str] = {}
    held: set[str] = set()
    for message in messages:
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                continue
            path = args.get("path") if isinstance(args, dict) else None
            if not isinstance(path, str):
                continue
            name = fn.get("name")
            if name == "read_file":
                entry = path + _read_span(args)
                reads[str(call.get("id"))] = path
            elif name == "write_file":
                entry = path
                held.add(path)
            else:  # an edit_file call shows a fragment, never the body
                continue
            if entry not in entries:
                entries.append(entry)
        path = reads.get(str(message.get("tool_call_id")))
        if message.get("role") == "tool" and path is not None:
            if ELIDED not in str(message.get("content") or ""):
                held.add(path)
    return entries, held


def _entry_path(entry: str) -> str:
    return entry.split(" (", 1)[0]


def _clear_reasoning(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Clear the reasoning of every assistant message but the newest
    `REASONING_KEPT`, in place; each cleared message as it was, for `archive`."""
    assistants = [m for m in messages if m.get("role") == "assistant"]
    cleared: list[dict[str, Any]] = []
    for message in assistants[: max(len(assistants) - REASONING_KEPT, 0)]:
        if not (message.get("reasoning_content") or message.get("reasoning")):
            continue
        cleared.append(dict(message))
        for key in ("reasoning_content", "reasoning"):
            if key in message:
                message[key] = ""
    return cleared


STALE_COPY: Final = (
    "\nThe last compaction dropped this file's contents from your context (the note "
    "lists {path}), so this edit was written from memory. Read {path} again with "
    "read_file, then edit what it says now."
)
"""What an edit refusal adds when its file is one of `stale_files` (#189): the
refusal alone read as a puzzle ("line 1 reads the same text... Odd") to a worker
that had edited from its memory of a file the note said to read again."""


def stale_files(messages: list[dict[str, Any]]) -> set[str]:
    """The files the compaction note in `messages` says are no longer in context,
    less those an intact `read_file` result or a `write_file` call has brought
    back since (`_file_bodies`, the rule the note itself is written by)."""
    note = next((m for m in messages if is_note(m)), None)
    if note is None:
        return set()
    _, held = _file_bodies(messages)
    gone = {PurePosixPath(_entry_path(e)).as_posix() for e in _previous_unread(note)}
    return gone - {PurePosixPath(path).as_posix() for path in held}


def stale_copy(arguments: str, messages: list[dict[str, Any]]) -> str:
    """`STALE_COPY` for the file an edit call's `arguments` name, when it is one of
    `stale_files`; "" otherwise."""
    path = _edited_path({"function": {"name": "edit_file", "arguments": arguments}})
    if path is None or PurePosixPath(path).as_posix() not in stale_files(messages):
        return ""
    return STALE_COPY.format(path=path)


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


def _previous_unread(note: dict[str, Any]) -> list[str]:
    """The file entries an older note said were no longer in context."""
    for line in str(note.get("content") or "").splitlines():
        if line.startswith(_NOTE_UNREAD):
            return line.removeprefix(_NOTE_UNREAD).removesuffix(UNREAD_HOW).split("; ")
    return []


def compact(
    messages: list[dict[str, Any]],
    *,
    limit_tokens: int,
    pin: Pin = "last",
    hint: str = ASK_USER,
    state: Callable[[], str] | None = None,
    measure: Callable[[list[dict[str, Any]]], int] | None = None,
    target_tokens: int | None = None,
    archive: Callable[[list[dict[str, Any]]], object] | None = None,
) -> tuple[int, str]:
    """Bring `messages` under `limit_tokens` in place.

    `limit_tokens` is the trigger; once over it, compaction works down to
    `target_tokens` (default: the limit itself), so the turns after it
    append to an unchanged prefix instead of compacting again. Each pass
    rewrites messages near the start, which costs the server its cached
    prefix: compacting to just under the limit fired 163 times in one run,
    and a turn right after one waited 38 s for its first token against
    14 s for one that did not.

    Returns (messages dropped, human summary). `(0, "")` means nothing was
    needed, which is the common case and must stay cheap. `state`, when
    given (an autonomous run), is called once per compaction and its text
    goes in the note, which is rebuilt, not appended to, each time.

    `archive`, when given, is handed what compaction takes, before it goes:
    the full text of each tool result it shortens and each message it drops,
    every tool result carrying its call as `call` (`recall`). A result already
    shortened is not handed over again: its full text went the first time.
    """
    size = measure or estimate_tokens
    if size(messages) <= limit_tokens:
        return 0, ""
    goal = min(limit_tokens, target_tokens) if target_tokens is not None else limit_tokens

    keep = pinned_index(messages, pin)
    shown, _ = _file_bodies(messages)

    def protected(index: int) -> bool:
        if messages[index].get("role") == "system" or index == keep:
            return True
        return index >= len(messages) - KEEP_RECENT

    calls = {
        call.get("id"): describe_call(call)
        for message in messages
        for call in message.get("tool_calls") or []
    }

    def keep_whole(lost: list[dict[str, Any]]) -> None:
        if archive is None:
            return
        whole = [
            {**m, "call": calls.get(m.get("tool_call_id"), "a tool call")}
            if m.get("role") == "tool"
            else dict(m)
            for m in lost
            if not (m.get("role") == "tool" and ELIDED in str(m.get("content") or ""))
        ]
        if whole:
            archive(whole)

    old = next((i for i, m in enumerate(messages) if is_note(m)), None)
    block = state() if state is not None else ""
    prior = _previous_unread(messages[old]) if old is not None else []
    reasoned = old is not None and REASONING_LINE in str(messages[old].get("content"))

    def unread() -> list[str]:
        _, held = _file_bodies(messages)
        return [e for e in dict.fromkeys(prior + shown) if _entry_path(e) not in held]

    def renote(summary: str) -> tuple[int, str]:
        if old is not None and state is not None:
            count, topics = _previous(messages[old])
            messages[old] = _note(count, topics, [], unread(), block, hint, reasoned)
        return 0, summary

    # Stage 0: old reasoning goes first, whole to `archive`, before any tool
    # result or exchange; a compaction rewrites the prompt anyway, so it costs no
    # second re-prefill.
    cleared = _clear_reasoning(messages)
    if cleared:
        keep_whole(cleared)
        reasoned = True
        said = f"reasoning of {len(cleared)} earlier round(s) cleared"
        if size(messages) <= goal:
            return renote(said)

    # Stage 1: shrink old tool results, and turn old images into a line.
    pictures = False
    for index, message in enumerate(messages):
        if protected(index):
            continue
        if size(messages) <= goal:
            break  # oldest first, and no further than the target needs
        content = message.get("content") or ""
        if is_image_followup(message) and isinstance(content, list):
            message["content"] = _elide_images(content)
            pictures = True
        elif message.get("role") == "tool" and len(content) > RESULT_HEAD + RESULT_TAIL:
            keep_whole([message])
            message["content"] = _truncate_result(content)
    elided = "older screenshots elided" if pictures else "large tool results elided"
    if cleared:
        elided = f"{said}; {elided}"

    if size(messages) <= goal:
        return renote(elided)

    # Stage 2: drop the oldest exchanges, a tool call always with its results.
    count = 0
    topics: list[str] = []
    at = None
    if old is not None:
        count, topics = _previous(messages.pop(old))
        at = old
        keep = pinned_index(messages, pin)
    dropped = 0
    edited: list[str] = []
    fresh: list[str] = []

    def drafted() -> dict[str, Any]:
        # A chat has no run to rebuild state from: it names the files edited in
        # what went. An autonomous run's state block lists what is changed now.
        named = [] if state is not None else edited
        topics_kept = (topics + fresh)[:TOPICS_KEPT]
        return _note(count + dropped, topics_kept, named, unread(), block, hint, reasoned)

    def reserve() -> int:
        # What the note will cost, measured whole: counting only the state
        # block left 57 passes of one run over the limit having dropped nothing.
        return size([*messages, drafted()]) - size(messages)

    def droppable() -> tuple[int, int] | None:
        oldest = next((i for i in range(len(messages)) if not protected(i)), None)
        if oldest is None:
            return None  # nothing left that may be dropped; report what we managed
        end = oldest + 1
        while end < len(messages) and messages[end].get("role") == "tool":
            end += 1
        if any(protected(i) for i in range(oldest, end)):
            return None  # the call's results are in the protected tail; never split them
        return oldest, end

    while size(messages) + reserve() > goal:
        span = droppable()
        if span is None:
            break
        oldest, end = span
        keep_whole(messages[oldest:end])
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
    if size(messages) + reserve() > goal:
        last = max((i for i, m in enumerate(messages) if m.get("role") == "assistant"), default=-1)
        for message in messages[:last]:
            content = message.get("content") or ""
            if message.get("role") == "tool" and len(content) > RESULT_HEAD + RESULT_TAIL:
                keep_whole([message])
                message["content"] = _truncate_result(content)

    if not dropped and old is None:
        return 0, elided

    summary = f"{dropped} earlier message(s) compacted"
    if fresh:
        summary += ": " + "; ".join(fresh[:TOPICS_KEPT])
    messages.insert(at if at is not None else 0, drafted())
    return dropped, summary


def _note(
    count: int,
    topics: list[str],
    edited: list[str],
    unread: list[str],
    block: str,
    hint: str,
    reasoned: bool = False,
) -> dict[str, Any]:
    lines = [f"{NOTE_HEAD}{count} earlier message(s) dropped to fit the window.]"]
    if topics:
        lines.append(_NOTE_TOPICS + "; ".join(topics))
    if edited:
        lines.append("Files edited in the dropped part: " + ", ".join(edited))
    if unread:
        lines.append(_NOTE_UNREAD + "; ".join(unread) + UNREAD_HOW)
    if reasoned:
        lines.append(REASONING_LINE)
    if block:
        lines.append(block)
    lines.append(hint)
    return {"role": COMPACTION_ROLE, "content": "\n".join(lines)}
