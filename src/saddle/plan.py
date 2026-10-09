"""The worker's plan: the steps it says it will take, kept by the harness item by item (#209).

A long run loses its own intentions at compaction. In one traced run the worker found
that its docstring listed cases no fixture held and planned, in its reasoning only,
eight fixtures to add. Eight minutes later it restated the plan as six tests, and the
case it most needed (output names read by `ORDER BY`) was not among them. A compaction
then cleared the reasoning that held the first version. The case was never tested, and
the change refused correct SQL its own docstring listed as known-good. Reasoning is
the first thing compaction clears (`memory.REASONING_KEPT`), and a plan the worker
restates can lose items in the restating.

So the plan lives outside the conversation and changes one item at a time:

- `add` stores each step under the next id (P1, P2, ...), word for word: only runs
  of whitespace are made one space, so an item stays one line. A step already open,
  word for word, is not added twice.
- `done` closes items, saying what shows each is done (a test name, a command).
- `drop` closes items, saying why they will not be done.
- `show` lists the plan and changes nothing.

No call replaces the list. A restated plan that leaves a step out cannot lose it: an
item leaves only by `done` or `drop`, each with its reason. (A full rewrite of
accumulated context has been measured collapsing it from 18,282 tokens to 122,
arXiv:2510.04618.)

Where the plan is shown: every `plan` result lists the open items; after every
compaction the run's state block lists them word for word (`Plan.section`); a
`finish` while items are open is answered once with them (`engine`); and the run's
outcome and commit name any still open at its end. Nothing here refuses anything:
an open item never blocks a finish, because a check the worker cannot satisfy
invites a spiral.

The reminder (`Plan.reminder`): a reply whose text lists steps, none or few of
which are in the plan, is answered once with the steps quoted, so the worker can
add the ones it means. What counts as a list of steps (`listed_steps`): two or
more numbered or bulleted lines under a lead that announces work ("Tests to add:",
"Next steps", "I will"), or whose items mostly open with a verb that changes or
tests something ("add", "write", "fix"); or three or more items separated by
semicolons in parentheses after such a verb, the shape the traced run's plan had.
Investigation verbs ("read", "grep", "check") do not count: those are this round's
moves, not a plan, and neither does a list most of whose items carry a check
mark: that reports what is done. A list is quoted at most once.

Sizes are estimated tokens (`memory.CHARS_PER_TOKEN`), as compaction measures them.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from saddle.memory import CHARS_PER_TOKEN, is_note

ID_PREFIX: Final = "P"
"""What an item's id opens with: `P1`, `P2`, ... in the order items were added."""

ITEM_TOKENS: Final = 120
"""The most one item may hold. A step is one line; a longer one is refused whole,
with the limit named, and nothing is added."""

WHY_TOKENS: Final = 200
"""The most a `done` or `drop` reason may hold."""

SECTION_TOKENS: Final = 1500
"""The most the state block's plan section spends on open items. Over it, the oldest
open items are shown (the ones most at risk of being forgotten) and the rest are
counted, with `show` named to list them."""

MIN_STEPS: Final = 2
"""How many items, not in the plan, a listed set of steps needs before a reminder."""

INLINE_MIN: Final = 3
"""How many semicolon-separated items a parenthesized list needs to count as steps."""

QUOTED: Final = 8
"""How many steps a reminder quotes; it counts the rest. Eight holds the traced
plan whole: its lost step was the fifth, and a quoted step can be added as written."""

QUOTE_TOKENS: Final = 20
"""How much of one step a reminder quotes."""

COVERED: Final = 0.6
"""The share of a listed step's words one plan item must hold for the step to count
as planned. Steps are restated, not copied, so exact text would remind about steps
already in the plan."""

ACTIONS: Final = frozenset(
    {
        "add",
        "assert",
        "change",
        "cover",
        "create",
        "delete",
        "document",
        "extend",
        "extract",
        "fix",
        "guard",
        "handle",
        "implement",
        "make",
        "merge",
        "move",
        "pin",
        "refactor",
        "remove",
        "rename",
        "replace",
        "restore",
        "revert",
        "split",
        "support",
        "test",
        "update",
        "wire",
        "write",
    }
)
"""Verbs whose step changes or tests something. Reading and searching verbs are left
out on purpose: a list of reads is the round's own moves, not a plan."""

STEP_LEAD: Final = re.compile(
    r"(?i)(\bplan\b|\bsteps?\b|\bto-?dos?\b|\bto do\b|\bnext\b|\bremaining\b|\bleft to\b"
    r"|\bstill (?:need|have) to\b|\bneed to\b|\bi(?:'ll| will| should| must)\b|\bwe(?:'ll| will)\b"
    r"|\bto (?:add|write|fix|change|test|cover|update|implement|handle)\b|\bchecklist\b"
    r"|\bfollow-?ups?\b|\btasks?\b)"
)
"""A lead line that announces work: what the line above a list says when the list is
a plan ("Tests to add (concise):", "Next steps:", "I will:")."""

LIST_ITEM: Final = re.compile(r"^[ \t]{0,8}(?:\(?\d{1,2}[.):]|[-*•])[ \t]+(?P<text>\S.*)$")
"""One numbered (`1.`, `1)`, `(1)`) or bulleted (`-`, `*`, `•`) line. Not `+`: a diff
in the reasoning marks its added lines with it."""

INLINE_LIST: Final = re.compile(
    r"(?i)\b(?:add|write|test|cover|fix|handle|implement)\b[^()\n]{0,80}"
    r"(?<=\s)\((?P<items>[^()\n]*;[^()\n]*;[^()\n]*)\)"
)
"""Semicolon-separated items in parentheses after a verb that changes or tests:
"add the few fixtures that are worth having (VALUES alias list; ...; quoted alias)".
The parenthesis follows a space, so a call such as `parse("a;b;c")` is no list."""

DONE_MARK: Final = "\u2713"
"""A check mark: a list most of whose items carry one reports what is done (a status
list), not what is to do."""

_WORD: Final = re.compile(r"[a-z0-9]+")
"""A step's words, identifiers split at `_`, `-` and `.`: a test named in a list
(`test_a_values_source_holds_...`) and the same step written out in words match."""
_STOP: Final = frozenset(
    {"a", "an", "and", "as", "at", "be", "by", "for", "from", "in", "is", "it", "its"}
    | {"of", "on", "or", "that", "the", "this", "to", "with"}
)
_ID: Final = re.compile(rf"^{ID_PREFIX}?(\d+)$", re.IGNORECASE)
_ADDED: Final = re.compile(rf"^Added {ID_PREFIX}(\d+): (.*)$")
_CLOSED: Final = re.compile(rf"^Closed {ID_PREFIX}(\d+) as (done|dropped): ")
_OPEN_LINE: Final = re.compile(rf"^  {ID_PREFIX}(\d+): (.*)$")
_CLOSED_LINE: Final = re.compile(rf"{ID_PREFIX}(\d+) (done|dropped)")
_HIDDEN_LINE: Final = re.compile(r"^  \.\.\. and (\d+) more open item")

FINISH_RETURNED: Final = "finish returned, not refused: your plan has "
"""How `engine.PLAN_OPEN` opens: a resumed run whose conversation holds a `finish`
answered this way has had its one reading of the open items (`Plan.from_messages`)."""

SECTION_HEAD: Final = "- plan, in your own words as the plan tool keeps them: "
"""How the state block's plan section opens; `Plan.from_messages` finds it there."""

UNRECOVERED: Final = "(open; its words were not in the conversation this run resumed from)"
"""A resumed plan's text for an open item the note had no room to show."""

ARGUMENTS: Final = frozenset({"action", "items", "ids", "why"})
USAGE: Final = (
    'Call plan with {"action": "add", "items": ["a step"]}, '
    '{"action": "done", "ids": ["P1"], "why": "what shows it"}, '
    '{"action": "drop", "ids": ["P2"], "why": "why not"} or {"action": "show"}.'
)

REMINDER: Final = (
    "[plan] Your last reply listed steps that are not in your plan:\n{steps}\n"
    'Add the ones you will take with plan ({{"action": "add", "items": [...]}}), so '
    "they stay in view after a compaction. If the list was not a plan, ignore this: "
    "it is a reminder, not a check."
)

Status = Literal["open", "done", "dropped"]


def estimate(text: str) -> int:
    """`text`'s size in estimated tokens, as compaction counts it."""
    return -(-len(text) // CHARS_PER_TOKEN)


def one_line(text: str) -> str:
    """`text` with every run of whitespace made one space, and none at the ends."""
    return " ".join(text.split())


def _words(text: str) -> frozenset[str]:
    return frozenset(w for w in _WORD.findall(text.lower()) if w not in _STOP)


def _quote(text: str) -> str:
    cut = QUOTE_TOKENS * CHARS_PER_TOKEN
    if len(text) <= cut:
        return text
    return text[:cut].rsplit(" ", 1)[0] + " ..."


@dataclass
class Item:
    """One step: its number, its text word for word, and how it was closed."""

    number: int
    text: str
    status: Status = "open"
    why: str = ""
    """For `done`, what shows it; for `drop`, why not; "" while open."""

    @property
    def id(self) -> str:
        return f"{ID_PREFIX}{self.number}"

    def record(self) -> dict[str, object]:
        return {"id": self.id, "text": self.text, "status": self.status, "why": self.why}


def listed_steps(text: str) -> list[list[str]]:
    """Every list of steps `text` holds, each as its items' text, in order.

    A run of two or more numbered or bulleted lines is a list of steps when the
    nearest line above it announces work (`STEP_LEAD`) or when at least half of its
    items open with one of `ACTIONS`. A parenthesized run of `INLINE_MIN` or more
    semicolon-separated items after a verb that changes or tests is one too."""
    found: list[list[str]] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        first = LIST_ITEM.match(lines[index])
        if first is None:
            index += 1
            continue
        start = index
        items: list[str] = []
        blank = 0
        while index < len(lines):
            line = lines[index]
            match = LIST_ITEM.match(line)
            if match is not None:
                items.append(one_line(match["text"]))
                blank = 0
            elif not line.strip():
                blank += 1
                if blank > 1:
                    break  # two blank lines end a list
            elif line[:1] not in " \t":
                break  # an unindented line that is no item ends it; an indented one continues one
            index += 1
        lead = next((ln for ln in reversed(lines[:start]) if ln.strip()), "")
        done = sum(DONE_MARK in item for item in items)
        if len(items) < MIN_STEPS or done * 2 >= len(items):
            continue
        if STEP_LEAD.search(lead) or _mostly_actions(items):
            found.append(items)
    for match in INLINE_LIST.finditer(text):
        items = [one_line(part) for part in match["items"].split(";") if part.strip()]
        if len(items) >= INLINE_MIN:
            found.append(items)
    return found


def _mostly_actions(items: Sequence[str]) -> bool:
    def acts(item: str) -> bool:
        # The first whole token, markup stripped: `test_a_values_...` is a name, not
        # the verb "test".
        first = item.lower().lstrip("*`_ ").split(maxsplit=1)
        return bool(first) and first[0].strip("*`_:,.;()") in ACTIONS

    return sum(map(acts, items)) * 2 >= len(items)


@dataclass
class Plan:
    """A run's plan: its items, in the order they were added, never removed."""

    items: list[Item] = field(default_factory=list)
    finish_told: bool = False
    """Set once a `finish` was answered with the open items (`engine._finish`):
    that happens once per run, and the next `finish` goes on to the audit."""
    reminded: set[frozenset[str]] = field(default_factory=set)
    """The step lists already quoted back, each as its items' word sets."""
    reminders: int = 0

    # --- the tool -----------------------------------------------------------------

    def apply(self, arguments: str) -> str:
        """Carry out one `plan` call and say what it did, then the plan as it stands.
        Every refusal starts "error: ", changes nothing, and says how to call."""
        try:
            args = json.loads(arguments) if arguments.strip() else {}
        except ValueError as exc:
            return f"error: plan's arguments are not valid JSON: {exc}. {USAGE}"
        if not isinstance(args, dict):
            return f"error: plan's arguments must be a JSON object. {USAGE}"
        extra = sorted(set(args) - ARGUMENTS)
        if extra:
            return f"error: plan does not take {', '.join(extra)}. {USAGE}"
        action = args.get("action")
        if action == "add":
            return self._add(args.get("items"))
        if action == "done" or action == "drop":
            return self._close(action, args.get("ids"), args.get("why"))
        if action == "show":
            return self.listing()
        return f"error: plan's action must be add, done, drop or show, not {action!r}. {USAGE}"

    def _add(self, given: object) -> str:
        texts = _strings(given)
        if texts is None or not texts:
            return f"error: add needs items, a list of one-line steps. {USAGE}"
        steps = [one_line(t) for t in texts]
        if any(not s for s in steps):
            return f"error: an item is empty; nothing was added. {USAGE}"
        long = [s for s in steps if estimate(s) > ITEM_TOKENS]
        if long:
            return (
                f"error: an item may hold at most {ITEM_TOKENS} tokens; this one holds "
                f"about {estimate(long[0])}: {_quote(long[0])!r}. Nothing was added; split "
                "it into shorter steps."
            )
        said: list[str] = []
        for step in steps:
            same = next((i for i in self.items if i.status == "open" and i.text == step), None)
            if same is not None:
                said.append(f"Already open as {same.id}: {step}")
                continue
            item = Item(number=len(self.items) + 1, text=step)
            self.items.append(item)
            said.append(f"Added {item.id}: {step}")
        return "\n".join(said) + "\n\n" + self.listing()

    def _close(self, action: str, given: object, why: object) -> str:
        status: Status = "done" if action == "done" else "dropped"
        wanted = "what shows each is done" if status == "done" else "why each will not be done"
        if not isinstance(why, str) or not one_line(why):
            return f"error: {action} needs why: {wanted}. Nothing was closed. {USAGE}"
        reason = one_line(why)
        if estimate(reason) > WHY_TOKENS:
            return (
                f"error: why may hold at most {WHY_TOKENS} tokens; this holds about "
                f"{estimate(reason)}. Nothing was closed."
            )
        numbers = _numbers(given)
        if not numbers:
            return f"error: {action} needs ids, the items to close (P1, P2, ...). {USAGE}"
        unknown = [n for n in numbers if not 1 <= n <= len(self.items)]
        if unknown:
            known = ", ".join(i.id for i in self.open_items()) or "none"
            return (
                f"error: no item {', '.join(f'{ID_PREFIX}{n}' for n in unknown)} in the plan; "
                f"its open items are {known}. Nothing was closed."
            )
        said: list[str] = []
        for number in dict.fromkeys(numbers):
            item = self.items[number - 1]
            if item.status != "open":
                said.append(f"{item.id} was already {item.status}: {item.why}")
                continue
            item.status, item.why = status, reason
            said.append(f"Closed {item.id} as {status}: {reason}")
        return "\n".join(said) + "\n\n" + self.listing()

    # --- what is shown --------------------------------------------------------------

    def open_items(self) -> list[Item]:
        return [i for i in self.items if i.status == "open"]

    def counts(self) -> str:
        done = sum(i.status == "done" for i in self.items)
        dropped = sum(i.status == "dropped" for i in self.items)
        return f"{len(self.open_items())} open, {done} done, {dropped} dropped"

    def listing(self) -> str:
        """What every `plan` result ends with: the counts, then each open item."""
        if not self.items:
            return "Plan: no items yet."
        lines = [f"Plan: {self.counts()}."]
        opened = self.open_items()
        if opened:
            lines.append("Open:")
            lines += [f"{i.id}: {i.text}" for i in opened]
        return "\n".join(lines)

    def section(self) -> str:
        """The state block's plan section: the counts, the open items word for word,
        oldest first within `SECTION_TOKENS`, then the closed ids."""
        if not self.items:
            return "- plan: no items yet (add the steps you will take with the plan tool)"
        lines = [SECTION_HEAD + self.counts()]
        spent = 0
        hidden = 0
        for item in self.open_items():
            line = f"  {item.id}: {item.text}"
            if hidden or spent + estimate(line) > SECTION_TOKENS:
                hidden += 1
                continue
            spent += estimate(line)
            lines.append(line)
        if hidden:
            lines.append(f"  ... and {hidden} more open item(s): plan with action show lists them")
        closed = [f"{i.id} {i.status}" for i in self.items if i.status != "open"]
        if closed:
            lines.append("  closed: " + ", ".join(closed))
        return "\n".join(lines)

    def record(self) -> list[dict[str, object]]:
        """Every item, open and closed, for the run's sealed outcome."""
        return [i.record() for i in self.items]

    # --- the reminder ---------------------------------------------------------------

    def unplanned(self, steps: Iterable[str]) -> list[str]:
        """The steps no plan item covers (`COVERED` of the step's words)."""
        held = [_words(i.text) for i in self.items]
        left: list[str] = []
        for step in steps:
            words = _words(step)
            if not words:
                continue
            if any(len(words & item) >= COVERED * len(words) for item in held):
                continue
            left.append(step)
        return left

    def reminder(self, reasoning: str, reply: str) -> str | None:
        """Called once per model round: the reminder to append to what the model
        reads next, or None. It quotes the steps of each list in this round's text
        that has `MIN_STEPS` or more not in the plan and was not quoted before.

        No gap between reminders: over eleven recorded runs a gap of two rounds cut
        reminders by under a tenth, and a gap of five suppressed the one the traced
        run needed, three rounds after another."""
        fresh: list[str] = []
        # Newest list first: the last list a reply writes is usually its decision,
        # and the quote has room for only `QUOTED` steps.
        for steps in reversed(listed_steps(f"{reasoning}\n{reply}")):
            left = self.unplanned(steps)
            key = frozenset(" ".join(sorted(_words(s))) for s in left)
            if len(left) < MIN_STEPS or key in self.reminded:
                continue
            self.reminded.add(key)
            fresh += [s for s in left if s not in fresh]
        if not fresh:
            return None
        self.reminders += 1
        quoted = [f'- "{_quote(s)}"' for s in fresh[:QUOTED]]
        if len(fresh) > QUOTED:
            quoted.append(f"- and {len(fresh) - QUOTED} more")
        return REMINDER.format(steps="\n".join(quoted))

    # --- a resumed run ----------------------------------------------------------------

    @classmethod
    def from_messages(cls, messages: Sequence[Mapping[str, Any]]) -> Plan:
        """The plan as `messages` left it, for a run resumed from a recorded request.

        The newest compaction note's plan section gives the items open then and the
        ids already closed (their text is not kept there); the `plan` results after
        it in the messages add and close items by the ids they name. A result the
        note already reflects changes nothing: an id is added only once and closed
        only while open. A `finish` already answered with the open items
        (`FINISH_RETURNED`) is not answered so again."""
        plan = cls()
        calls: set[str] = set()
        told = False
        for message in messages:
            if is_note(dict(message)):
                parsed = cls._from_note(str(message.get("content") or ""))
                if parsed is not None:
                    plan = parsed
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                if function.get("name") == "plan":
                    calls.add(str(call.get("id")))
            if message.get("role") == "tool" and str(message.get("tool_call_id")) in calls:
                plan._replay(str(message.get("content") or ""))
            if message.get("role") == "tool" and str(message.get("content")).startswith(
                FINISH_RETURNED
            ):
                told = True
        plan.finish_told = told
        return plan

    @classmethod
    def _from_note(cls, note: str) -> Plan | None:
        lines = note.splitlines()
        head = next((n for n, ln in enumerate(lines) if ln.startswith(SECTION_HEAD)), None)
        if head is None:
            return None
        known: dict[int, Item] = {}
        hidden = 0
        for line in lines[head + 1 :]:
            opened = _OPEN_LINE.match(line)
            more = _HIDDEN_LINE.match(line)
            if opened is not None:
                known[int(opened[1])] = Item(number=int(opened[1]), text=opened[2])
            elif more is not None:
                hidden = int(more[1])
            elif line.startswith("  closed: "):
                for number, status in _CLOSED_LINE.findall(line):
                    state: Status = "done" if status == "done" else "dropped"
                    known[int(number)] = Item(int(number), "", state, "")
            elif not line.startswith("  "):
                break
        plan = cls()
        # Ids run from P1 with none removed, so the section's items and the ones it
        # had no room for are all of them; an id neither listed open nor closed is an
        # open item it did not show, its words not in the conversation.
        for number in range(1, max(len(known) + hidden, max(known, default=0)) + 1):
            plan.items.append(known.get(number, Item(number, UNRECOVERED)))
        return plan

    def _replay(self, result: str) -> None:
        for line in result.splitlines():
            added = _ADDED.match(line)
            if added is not None and int(added[1]) == len(self.items) + 1:
                self.items.append(Item(number=int(added[1]), text=added[2]))
                continue
            closed = _CLOSED.match(line)
            if closed is not None and 1 <= int(closed[1]) <= len(self.items):
                item = self.items[int(closed[1]) - 1]
                if item.status == "open":
                    item.status = "done" if closed[2] == "done" else "dropped"
                    item.why = line.split(": ", 1)[1]


def _strings(given: object) -> list[str] | None:
    """`items` as a list of strings: one string is a list of one."""
    if isinstance(given, str):
        return [given]
    if isinstance(given, list) and all(isinstance(t, str) for t in given):
        return list(given)
    return None


def _numbers(given: object) -> list[int]:
    """`ids` as item numbers: "P3", "p3", "3" and 3 all name item 3; anything else
    names nothing."""
    values = given if isinstance(given, list) else [given]
    numbers: list[int] = []
    for value in values:
        if isinstance(value, bool):
            return []
        if isinstance(value, int):
            numbers.append(value)
            continue
        match = _ID.match(value.strip()) if isinstance(value, str) else None
        if match is None:
            return []
        numbers.append(int(match[1]))
    return numbers
