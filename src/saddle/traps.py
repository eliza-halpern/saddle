"""Trap signatures over a run's timeline, and the verdict they give (#164).

Each signature reads a run's timeline (`saddle.triage`) and returns the rounds it
rests on, or None. All are deterministic, and each comes from a run that was trapped
that way:

- overflow, the harness's: the server refused a request as over its window (#80a1 r1).
- unsatisfiable, the harness's: `check` answered, on trees the model kept changing,
  that only a finish summary's flip lines could clear its findings; a check takes no
  summary (#80a1 r2).
- misreport, the harness's: finish was told a test had no flip line while its summary
  had a line naming that test (#80a1 r2's node ids).
- environment, the harness's: the same tests failed at audit after audit on changed
  trees, and the run never edited their files (#101 r1). Saddle's records keep the
  failing tests' names, not their failure text, so a missing tool is not read here.
- summary-names, the model's: finish returned the summary for naming code that no
  file holds (#191).
- repeat, the model's: the same calls with the same results three rounds running.

A stopped run with a harness sign is the harness's block; with only model signs, the
model's; with none, "unclassified". Never a guess. For a harness block, the trap is
the first round whose result a harness sign rests on. The rewind point is the latest
request before that result reached the model that ends on a tool result, with the
files every later edit touched, so a resume knows whether the branch's tree is the
tree at that point.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Final, Literal

from saddle.engine import SUMMARY_NAMES_ABSENT
from saddle.feed import FLIPS_FIRST
from saddle.journal import STALL_STOP
from saddle.transcript import tier_finding
from saddle.triage import EDIT_TOOLS, Round, Timeline

Block = Literal["harness", "model"]

FINISHED: Final = "auto:finished"

NEEDS_YOU: Final = "stopped: needs you: "
"""How a run that ended needing a person opens its outcome (`journal.AUDIT_QUESTION_STOP`
and its siblings): a question, a dispute, a refusal or a block the model named, which
ends a run with a verdict. A stall eject has the same form but is a run that stopped
making progress (`journal.STALL_STOP`)."""

_OVERFLOW: Final = re.compile(
    r"exceeds the context|maximum context length|prompt is too long|too many tokens",
    re.IGNORECASE,
)
"""How a server words a request over its window (Strata's, then vLLM's and others')."""

_FLIPS_FIRST_HEAD: Final = FLIPS_FIRST.split(". ")[0]
"""The part of the note naming what clears it: a finish summary's flip lines."""

_TREE: Final = re.compile(r"\[audit [^\]]*? on tree ([0-9a-f]+): ")

_NO_FLIP: Final = re.compile(
    r"^- \S+: (?P<label>'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\") \[no flip line\]",
    re.MULTILINE,
)
"""A test the flip judge says no line labels (`flips`: `- path: 'name' [no flip line]`)."""

_FLIP_SEPARATOR: Final = re.compile(" -- | \u2014 | \u2013 | - |: ")
"""What ends a flip line's label: the separators `flips` accepts before evidence."""

_NAMES_HEAD: Final = SUMMARY_NAMES_ABSENT.split("{names}")[0]

_FAILING: Final = re.compile(r"exited \d+: (?P<count>\d+) failing: (?P<names>.*)", re.DOTALL)

REPEATS: Final = 3
"""How many rounds running the same calls must get the same results to be a sign."""


@dataclass(frozen=True)
class Sign:
    """One signature that fired: its name, whose block, the rounds it rests on."""

    name: str
    block: Block
    rounds: tuple[int, ...]
    why: str


@dataclass(frozen=True)
class Rewind:
    """Where a resume would continue from."""

    request: int
    round: int
    edited_after: tuple[str, ...]
    """Files an edit call changed at or after this round: the branch holds them."""
    commands_after: int
    """Commands run at or after this round, any of which may have written a file."""


@dataclass(frozen=True)
class Verdict:
    """Whose block a run's stop was, and where to resume a harness one."""

    outcome: str
    """The outcome span's name, or "none" for a run that has not ended."""
    ended: Literal["finished", "needs you", "stopped", "none"]
    """How the run ended: with a verdict (finished, needs you), or not."""
    last_progress: int | None
    """The last round that edited a file or got a new audit answer."""
    block: Block | Literal["unclassified"] | None
    """None for a run that ended with a verdict: its signs are notes, not a block."""
    signs: tuple[Sign, ...]
    trap: int | None
    rewind: Rewind | None


def _results(round_: Round) -> Iterator[tuple[str, str, str]]:
    """Each call of a round as (name, arguments, what it returned): from the log when
    it holds the round, else from the ledger's spans, whose text is cut."""
    if round_.calls:
        for call in round_.calls:
            text = call.result if call.result is not None else ""
            if not text and call.span is not None:
                text = call.span.detail
            yield call.name, call.arguments, text
        return
    for span in round_.spans:
        if span.kind == "tool" and len(span.argv) >= 2:
            yield span.argv[0], span.argv[1], span.detail


def _edits(round_: Round) -> list[str]:
    """The files the round's edit calls changed, from the ledger."""
    paths: list[str] = []
    for span in round_.spans:
        if span.kind == "tool" and span.argv[:1] and span.argv[0] in EDIT_TOOLS:
            try:
                path = json.loads(span.argv[1]).get("path") if span.exit_code == 0 else None
            except (IndexError, ValueError, AttributeError):
                path = None
            if isinstance(path, str):
                paths.append(path)
    return paths


def overflow(seen: Timeline) -> Sign | None:
    outcome = seen.outcome
    if outcome is None or "model error:" not in outcome.detail:
        return None
    if _OVERFLOW.search(outcome.detail) is None:
        return None
    last = seen.rounds[-1].number if seen.rounds else 0
    said = outcome.detail[outcome.detail.index("model error:") :][:240]
    return Sign(
        "overflow",
        "harness",
        (last,) if last else (),
        f"the request after round {last} was refused as over the window: {said}",
    )


def unsatisfiable(seen: Timeline) -> Sign | None:
    hits: list[tuple[int, str]] = []
    for round_ in seen.rounds:
        for name, _arguments, text in _results(round_):
            if name == "check" and _FLIPS_FIRST_HEAD in text:
                tree = _TREE.search(text)
                hits.append((round_.number, tree.group(1) if tree else ""))
    trees = {tree for _, tree in hits if tree}
    if len(trees) < 2:
        return None
    return Sign(
        "unsatisfiable",
        "harness",
        tuple(number for number, _ in hits),
        f"check answered {len(hits)} times, on {len(trees)} trees, that only a finish "
        "summary's flip lines clear its findings, and a check takes no summary",
    )


def _flip_labels(summary: str) -> list[str]:
    """What each `flip:` line names, before its evidence."""
    labels: list[str] = []
    for line in summary.splitlines():
        found = re.match(r"\s*flip:\s*(.*)", line, re.IGNORECASE)
        if found is not None:
            labels.append(_FLIP_SEPARATOR.split(found.group(1), maxsplit=1)[0])
    return labels


def misreport(seen: Timeline) -> Sign | None:
    hits: list[tuple[int, str]] = []
    for round_ in seen.rounds:
        for name, arguments, text in _results(round_):
            if name != "finish" or "[no flip line]" not in text:
                continue
            try:
                summary = json.loads(arguments).get("summary", "")
            except (ValueError, AttributeError):
                continue
            labels = _flip_labels(summary if isinstance(summary, str) else "")
            for found in _NO_FLIP.finditer(text):
                test = str(ast.literal_eval(found.group("label")))
                named = re.compile(rf"(?<!\w){re.escape(test)}(?!\w)")
                if any(named.search(label) for label in labels):
                    hits.append((round_.number, test))
    if not hits:
        return None
    tests = sorted({test for _, test in hits})
    return Sign(
        "misreport",
        "harness",
        tuple(sorted({number for number, _ in hits})),
        f"finish was told {len(tests)} test(s) had no flip line while its summary had a "
        f"line naming each: {', '.join(tests[:5])}",
    )


def _failing(text: str) -> tuple[int, tuple[str, ...]] | None:
    """A failing test finding's count and the names it lists whole: a list that runs
    to the end of a cut line loses its last name, which the cut may have split."""
    found = _FAILING.search(text)
    if found is None:
        return None
    rest = found.group("names")
    end = re.search(r"; impact| and \d+ more", rest)
    names = rest[: end.start()].split(", ") if end is not None else rest.split(", ")[:-1]
    return int(found.group("count")), tuple(name.strip() for name in names if name.strip())


def environment(seen: Timeline) -> Sign | None:
    seen_at: dict[tuple[int, tuple[str, ...]], list[tuple[int, str]]] = {}
    edited: set[str] = set()
    for round_ in seen.rounds:
        edited.update(_edits(round_))
        tree = ""
        for span in round_.spans:
            found = _TREE.search(span.detail)
            if found is not None:
                tree = found.group(1)
        for span in round_.spans:
            finding = tier_finding(span.name, span.detail, span.exit_code)
            if finding is None or finding.gate != "tests" or finding.verdict != "fail":
                continue
            failing = _failing(finding.detail)
            if failing is not None and failing[1]:
                seen_at.setdefault(failing, []).append((round_.number, tree))
    for (count, names), where in seen_at.items():
        trees = {tree for _, tree in where if tree}
        files = {name.split("::")[0] for name in names}
        if len(where) >= 2 and len(trees) != 1 and not files & edited:
            on = f"on {len(trees)} trees" if trees else "on trees the records do not name"
            return Sign(
                "environment",
                "harness",
                tuple(number for number, _ in where),
                f"the same {count} test(s) failed at {len(where)} audits {on}, none in a "
                f"file the run edited: {', '.join(names[:3])}",
            )
    return None


def summary_names(seen: Timeline) -> Sign | None:
    rounds = tuple(
        round_.number
        for round_ in seen.rounds
        for name, _arguments, text in _results(round_)
        if name == "finish" and text.startswith(_NAMES_HEAD)
    )
    if not rounds:
        return None
    return Sign(
        "summary-names",
        "model",
        rounds,
        f"finish returned the summary {len(rounds)} time(s) for naming code no file holds",
    )


def repeat(seen: Timeline) -> Sign | None:
    run: list[int] = []
    longest: list[int] = []
    before: tuple[tuple[str, str, str], ...] | None = None
    for round_ in seen.rounds:
        now = tuple(_results(round_))
        run = [*run, round_.number] if now and now == before else [round_.number]
        before = now
        if len(run) > len(longest):
            longest = run
    if len(longest) < REPEATS:
        return None
    return Sign(
        "repeat",
        "model",
        tuple(longest),
        f"the same calls got the same results {len(longest)} rounds running",
    )


SIGNATURES: Final[tuple[Callable[[Timeline], Sign | None], ...]] = (
    overflow,
    unsatisfiable,
    misreport,
    environment,
    summary_names,
    repeat,
)


def _last_progress(seen: Timeline) -> int | None:
    """The last round that edited a file or got audit findings not seen before."""
    last: int | None = None
    answers: set[frozenset[str]] = set()
    for round_ in seen.rounds:
        if _edits(round_):
            last = round_.number
        for name, _arguments, text in _results(round_):
            if name in ("check", "finish"):
                findings = frozenset(line for line in text.splitlines() if line.startswith("- "))
                if findings and findings not in answers:
                    answers.add(findings)
                    last = round_.number
    return last


def rewind(seen: Timeline, trap: int) -> Rewind | None:
    """The latest request at or before round `trap`'s that ends on a tool result, and
    what was edited from there on; None when the log holds no such request."""
    for round_ in reversed(seen.rounds[:trap]):
        request = round_.request
        if request is None or not request.messages:
            continue
        if request.messages[-1].get("role") != "tool":
            continue
        after = seen.rounds[round_.number - 1 :]
        edited = sorted({path for later in after for path in _edits(later)})
        commands = sum(
            1 for later in after for name, _a, _t in _results(later) if name == "run_command"
        )
        return Rewind(request.number, round_.number, tuple(edited), commands)
    return None


def _ended(seen: Timeline) -> Literal["finished", "needs you", "stopped", "none"]:
    if seen.outcome is None:
        return "none"
    if seen.outcome.name == FINISHED:
        return "finished"
    detail = seen.outcome.detail
    if detail.startswith(NEEDS_YOU) and not detail.startswith(f"stopped: {STALL_STOP}"):
        return "needs you"
    return "stopped"


def verdict(seen: Timeline) -> Verdict:
    """Whose block the run's stop was, from the signatures alone."""
    signs = tuple(sign for check in SIGNATURES if (sign := check(seen)) is not None)
    outcome = seen.outcome.name if seen.outcome is not None else "none"
    ended = _ended(seen)
    harness = [sign for sign in signs if sign.block == "harness"]
    block: Block | Literal["unclassified"] | None
    if ended in ("finished", "needs you"):
        block = None
    elif harness:
        block = "harness"
    elif signs:
        block = "model"
    else:
        block = "unclassified"
    placed = [min(sign.rounds) for sign in harness if sign.rounds]
    trap = min(placed) if block == "harness" and placed else None
    return Verdict(
        outcome=outcome,
        ended=ended,
        last_progress=_last_progress(seen),
        block=block,
        signs=signs,
        trap=trap,
        rewind=rewind(seen, trap) if trap is not None else None,
    )


def _rounds(numbers: tuple[int, ...]) -> str:
    shown = ", ".join(str(n) for n in numbers[:8]) + (", ..." if len(numbers) > 8 else "")
    return f"round {shown}" if len(numbers) == 1 else f"rounds {shown}"


def report(run: str, seen: Timeline, found: Verdict) -> str:
    """The verdict as text, each claim citing the rounds it rests on (`saddle triage`)."""
    said = seen.outcome.detail if seen.outcome is not None else "no outcome sealed"
    lines = [
        f"run {run}: {found.ended} ({said[:160]})",
        f"{len(seen.rounds)} rounds; conversation log: {seen.log}",
        "last progress: "
        + (f"round {found.last_progress}" if found.last_progress is not None else "none")
        + " (an edit, or audit findings not seen before)",
    ]
    if found.block is None:
        lines.append("block: none, the run ended with a verdict")
    elif found.block == "unclassified":
        lines.append("block: unclassified, no signature fits this stop")
    else:
        lines.append(f"block: the {found.block}'s")
    for sign in found.signs:
        lines.append(f"  {sign.name} (the {sign.block}'s), {_rounds(sign.rounds)}: {sign.why}")
    if found.trap is not None:
        lines.append(f"trap: round {found.trap}, the first round a harness sign rests on")
    rewind = found.rewind
    if rewind is not None:
        lines.append(
            f"rewind: request {rewind.request} (round {rewind.round}); after it "
            f"{len(rewind.edited_after)} file(s) changed by an edit call"
            + (f" ({', '.join(rewind.edited_after[:5])})" if rewind.edited_after else "")
            + f", and {rewind.commands_after} command(s) ran, any of which may have written "
            "a file"
        )
        lines.append(
            "  --request-out FILE writes that request for saddle auto --resume-messages FILE"
        )
    elif found.trap is not None:
        lines.append("rewind: none, the log holds no request before the trap to resume from")
    if found.block == "harness":
        lines.append("issue draft (from the records; nothing in it is model-written):")
        names = ", ".join(sign.name for sign in found.signs if sign.block == "harness")
        lines.append(f"  title: run {run} stopped on the harness: {names}")
        for sign in found.signs:
            if sign.block == "harness":
                lines.append(f"  - {sign.name}, {_rounds(sign.rounds)}: {sign.why}")
    return "\n".join(lines) + "\n"


def as_record(run: str, seen: Timeline, found: Verdict) -> dict[str, object]:
    """The verdict as JSON-ready data (`saddle triage --json`)."""
    return {
        "run": run,
        "ended": found.ended,
        "outcome": found.outcome,
        "outcome_detail": seen.outcome.detail if seen.outcome is not None else None,
        "rounds": len(seen.rounds),
        "log": seen.log,
        "last_progress": found.last_progress,
        "block": found.block,
        "signs": [
            {"name": s.name, "block": s.block, "rounds": list(s.rounds), "why": s.why}
            for s in found.signs
        ],
        "trap": found.trap,
        "rewind": (
            None
            if found.rewind is None
            else {
                "request": found.rewind.request,
                "round": found.rewind.round,
                "edited_after": list(found.rewind.edited_after),
                "commands_after": found.rewind.commands_after,
            }
        ),
    }
