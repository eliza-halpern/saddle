"""Rule D in the run path: the plan-time census and the verdict after the gates.

`saddle run --rule-d` reads a frozen reference store (`saddle.refstore`)
and an answer book (`saddle.answer_book`), and does two things the run did
not do before:

1. **Plan time.** `census` lists every class of spec-silent split inputs the
   book does not already answer, under the book's registered key function
   (the three-part class key). The run prints every one and stops when there
   are more than the budget; it never drops a question to fit.
2. **Verdict time.** After an `impl` node's gates pass, `verdict` runs the
   tree's function on the table's inputs in a subprocess and hands rule D the
   tree's answers, the stored references' answers and the book's authorities.
   `refuse` fails the attempt with the misses and their cites; `question`
   (or more asks than the per-tree budget) halts the node with the question
   text; `route`, `accept`, `no-reference` and `not-applicable` let the proof
   seal. Every decision is sealed into the attempt sidecar.

Layering: this module executes tree code through `evidence.run_capture`, so it
sits beside `runner`, above `gates`; `rule_d` and `answer_book` stay pure.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from saddle import answer_book as ab
from saddle import rule_d
from saddle.evidence import CapturedRun, run_capture, tree_memory_limit
from saddle.journal import redact_secrets
from saddle.refstore import ReferenceSet, load_reference_set

CENSUS_BUDGET: Final = 50
"""Split classes at plan time, per task (a measured task needed 46)."""

VERDICT_BUDGET: Final = 5
"""Asks at verdict time, per tree (measured: 0-5)."""

TREE_TIMEOUT_S: Final = 120.0
"""Wall for one tree's answers over the whole input table."""

EXAMPLES: Final = 3
"""Examples shown per question group (at most three)."""

_DRIVER: Final = r"""
import importlib.util, inspect, json, sys
spec_path, out_path = sys.argv[1], sys.argv[2]
job = json.load(open(spec_path, encoding="utf-8"))
def done(obj):
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)
    sys.exit(0)
sys.path.insert(0, ".")
try:
    spec = importlib.util.spec_from_file_location("_saddle_rule_d_tree", job["path"])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
except FileNotFoundError as exc:
    done({"unusable": "missing", "detail": str(exc)})
except BaseException as exc:
    done({"unusable": "import-error", "detail": f"{type(exc).__name__}: {exc}"})
fn = getattr(mod, job["function"], None)
if not callable(fn):
    done({"unusable": "missing", "detail": f"no callable {job['function']}"})
try:
    inspect.signature(fn).bind("x")
except TypeError as exc:
    done({"unusable": "arity", "detail": str(exc)})
except ValueError:
    pass
answers = {}
for x in job["inputs"]:
    try:
        v = fn(x)
    except BaseException as exc:
        answers[x] = ["raise", type(exc).__name__]
        continue
    try:
        answers[x] = ["value", json.dumps(v, sort_keys=True)]
    except (TypeError, ValueError):
        answers[x] = ["opaque", repr(type(v))]
done({"answers": answers})
"""


PANEL_SHA_DEFINITION: Final = "sha256(canon(sorted((input, panel_vote) pairs of the table)))"
"""How `Ctx.panel_sha` is computed from a table, sealed beside the value for the freeze list."""


class RuleDError(RuntimeError):
    """The store, table or book cannot be used as configured."""


@dataclass(frozen=True)
class RuleDConfig:
    """What `--rule-d` was given: the sealed store, its table, the book, the budgets."""

    store: Path
    set_id: str
    table: str = "fix8"
    book: Path | None = None
    census_budget: int = CENSUS_BUDGET
    verdict_budget: int = VERDICT_BUDGET
    retry: bool = False
    """A refused tree fails its node with no second attempt unless this is set (scope narrowed:
    every Phase 1 batch scored one verdict per tree; `--rule-d-retry` opts into repair)."""


@dataclass(frozen=True)
class Loaded:
    """Everything the census and the verdict read, loaded and checked once."""

    config: RuleDConfig
    refset: ReferenceSet
    callable_: str
    inputs: tuple[str, ...]
    references: tuple[tuple[str, rule_d.Answers], ...]
    ctx: ab.Ctx
    book: ab.Book
    key_fn: str | None

    def key(self, x: str) -> str:
        """The class id rule D and the census group by: the book's key, else the input."""
        return ab.ck(self.key_fn, x, self.ctx) if self.key_fn else ab.canon(["exact", x])


@dataclass(frozen=True)
class CensusClass:
    key: str
    members: tuple[str, ...]
    signatures: tuple[str, ...]


@dataclass(frozen=True)
class RuleDDecision:
    """One verdict-time decision, as the run consumes it."""

    verdict: str
    halt: bool
    refuse: bool
    detail: str
    evidence: Mapping[str, Any] = field(default_factory=dict)
    retry: bool = False


def callable_name(refset: ReferenceSet) -> str:
    """`validators.py` + `is_valid_email` -> `validators.is_valid_email` (the book's spelling)."""
    module = refset.path.removesuffix(".py").replace("/", ".")
    return f"{module}.{refset.function}"


def _answer(v: object) -> rule_d.Answer:
    return rule_d.Answer("value", json.dumps(v, sort_keys=True))


def load(config: RuleDConfig) -> Loaded:
    """Load the sealed store (C1), pick the table, build the book's context."""
    refset = load_reference_set(config.store, config.set_id)
    tables = {t.name: t for t in refset.answers}
    if config.table not in tables:
        msg = f"reference set has no answers table {config.table!r} (has {sorted(tables)})"
        raise RuleDError(msg)
    rows = [json.loads(line) for line in tables[config.table].rows.splitlines() if line.strip()]
    valid = [r.id for r in refset.references if r.valid]
    by_input: dict[str, dict[str, Any]] = {}
    for row in rows:
        by_input.setdefault(row["input"], row)
    inputs = tuple(by_input)
    refs: dict[str, str] = {}
    for x, row in by_input.items():
        got = [row["refs"][rid] for rid in valid]
        if not all(isinstance(v, bool) for v in got):
            msg = f"table {config.table!r}: input {x!r} has a non-boolean reference answer"
            raise RuleDError(msg)
        refs[x] = "".join("T" if v else "F" for v in got)
    references: list[tuple[str, rule_d.Answers]] = []
    for r in refset.references:
        if not r.valid:
            references.append((r.id, rule_d.Unusable("import-error", r.why or "invalid")))
            continue
        references.append((r.id, {x: _answer(row["refs"][r.id]) for x, row in by_input.items()}))
    votes = {x: row["panel_vote"] for x, row in by_input.items() if "panel_vote" in row}
    panel: ab.Panel | None = votes.__getitem__ if len(votes) == len(by_input) else None
    panel_sha = ab.sha(sorted(votes.items())) if panel is not None else ""
    ctx = ab.Ctx(refs, panel=panel, panel_sha=panel_sha, reference_set_id=refset.set_id)
    book = ab.Book.load(config.book) if config.book is not None else ab.Book()
    name = callable_name(refset)
    reg = book.registration(name)
    if reg["panel_sha"] and reg["panel_sha"] != panel_sha:
        msg = (
            f"answer book registers panel {reg['panel_sha']} for {name}; "
            f"table {config.table!r} carries panel {panel_sha or '(none)'}"
        )
        raise RuleDError(msg)
    return Loaded(config, refset, name, inputs, tuple(references), ctx, book, reg["key_fn"])


def census(loaded: Loaded) -> list[CensusClass]:
    """Plan time: every split class the book does not answer, in first-appearance order.

    Tree-independent: a split input's question does not depend on
    any tree, so the whole list is known before the first draw.
    """
    split = [x for x in loaded.inputs if len(set(loaded.ctx.refs[x])) > 1]
    keys = loaded.book.to_ask(loaded.callable_, split, loaded.ctx, set())
    members: dict[str, list[str]] = {k: [] for k in keys}
    for x in split:
        k = loaded.key(x)
        if k in members:
            members[k].append(x)
    return [
        CensusClass(
            k,
            tuple(ab.shortest_first(m)),
            tuple(dict.fromkeys(loaded.ctx.refs[x] for x in m)),
        )
        for k, m in members.items()
    ]


def render_census(classes: Sequence[CensusClass], budget: int) -> str:
    """Every census question, numbered; none is left out whatever the count."""
    head = f"rule D census: {len(classes)} split class(es) to answer (budget {budget})\n"
    lines = [head]
    for i, c in enumerate(classes, 1):
        shown = ", ".join(repr(x) for x in c.members[:EXAMPLES])
        lines.append(
            f"  Q{i} {c.key}: {len(c.members)} input(s), e.g. {shown}; "
            f"references {', '.join(c.signatures)}\n"
        )
    return "".join(lines)


Runner = Callable[..., CapturedRun]


def tree_answers(
    workdir: Path,
    path: str,
    function: str,
    inputs: Sequence[str],
    *,
    runner: Runner = run_capture,
) -> rule_d.Answers:
    """The tree's answer on every input, from a subprocess under the test memory ceiling."""
    with tempfile.TemporaryDirectory(prefix="saddle-rule-d-") as tmp:
        job, out = Path(tmp) / "job.json", Path(tmp) / "out.json"
        job.write_text(
            json.dumps({"path": path, "function": function, "inputs": list(inputs)}),
            encoding="utf-8",
        )
        run = runner(
            [sys.executable, "-c", _DRIVER, str(job), str(out)],
            workdir,
            timeout=TREE_TIMEOUT_S,
            memory_limit=tree_memory_limit(),
        )
        if run.timed_out:
            return rule_d.Unusable("timeout", f"no answers within {TREE_TIMEOUT_S} s")
        try:
            got = json.loads(out.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return rule_d.Unusable("crashed", f"exit {run.exit_code}: {run.stderr[-500:]}")
    if "unusable" in got:
        return rule_d.Unusable(got["unusable"], got["detail"])
    return {x: rule_d.Answer(kind, text) for x, (kind, text) in got["answers"].items()}


def _show(a: rule_d.Answer) -> str:
    return a.text if a.kind == "value" else f"{a.kind} {a.text}"


def question_text(result: rule_d.DifferentialResult) -> str:
    """What the user is asked: one group per class, at most three examples each."""
    lines: list[str] = []
    for b in result.band:
        lines.append(f"band class {b.cls}: {len(b.inputs)} input(s)")
        lines += [
            f"  {x!r}: tree {_show(g)}, references {_show(e)}"
            for x, g, e in list(zip(b.inputs, b.got, b.expected, strict=True))[:EXAMPLES]
        ]
    for q in result.questions:
        lines.append(f"split class {q.cls}: {len(q.inputs)} input(s)")
        got = q.got or tuple(rule_d.Answer("opaque", "") for _ in q.inputs)
        lines += [
            f"  {x!r}: tree {_show(g)}" for x, g in list(zip(q.inputs, got, strict=True))[:EXAMPLES]
        ]
    return "\n".join(lines)


def miss_text(result: rule_d.DifferentialResult) -> str:
    """Why a tree was refused: every miss with the authority that decided it."""
    if result.tree_unusable is not None:
        u = result.tree_unusable
        return f"rule D refused: tree unusable ({u.reason}: {u.detail})"
    lines = [f"rule D refused ({result.reason}):"]
    lines += [
        f"  {m.input!r}: tree {_show(m.got)}, expected {_show(m.expected)}"
        f" [{m.source}{': ' + m.cite if m.cite else ''}]"
        for m in result.disagreements
    ]
    return "\n".join(lines)


def sealed_fields(result: rule_d.DifferentialResult, loaded: Loaded) -> dict[str, Any]:
    """The rule D evidence the attempt sidecar carries: verdict, misses, cites, asks."""
    return {
        "verdict": result.verdict,
        "reason": result.reason,
        "reference_set_id": loaded.refset.set_id,
        "table": loaded.config.table,
        "book_head": loaded.book.head(),
        "spec_id": result.spec_id,
        "panel_sha": loaded.ctx.panel_sha,
        "panel_sha_definition": PANEL_SHA_DEFINITION,
        "convention_ids": list(result.convention_ids),
        "checked": result.checked,
        "unanimous": result.unanimous,
        "d": result.d,
        "references": result.references,
        "asks": list(result.asks),
        "disagreements": [
            {
                "input": m.input,
                "got": _show(m.got),
                "expected": _show(m.expected),
                "source": m.source,
                "cite": m.cite,
            }
            for m in result.disagreements
        ],
        "provenance": dict(Counter(p.source for p in result.provenance)),
        "cites": sorted({p.cite for p in result.provenance if p.cite}),
        "tree_unusable": (
            None
            if result.tree_unusable is None
            else [result.tree_unusable.reason, result.tree_unusable.detail]
        ),
        **asked_inputs(result),
    }


def asked_inputs(result: rule_d.DifferentialResult) -> dict[str, Any]:
    """Every asked class with its channel's inputs and each input's provenance.

    `band` (a question: the references agree, the tree does not) carries every input with the
    tree's and the references' answers; `questions` (split, spec-silent) carries every input
    with the tree's answer, "" when the tree gave none. `source` is that input's
    `Provenance.source` and is looked up, never defaulted: an asked input with no provenance
    raises KeyError here rather than sealing a record that a scorer would pass vacuously.
    `asks` stays the bare class ids; these two lists say which channel each came from.
    """
    src = {p.input: p.source for p in result.provenance}
    band = [
        {
            "cls": b.cls,
            "inputs": [
                {"input": x, "got": _show(g), "expected": _show(e), "source": src[x]}
                for x, g, e in zip(b.inputs, b.got, b.expected, strict=True)
            ],
        }
        for b in result.band
    ]
    questions = [
        {
            "cls": q.cls,
            "inputs": [
                {"input": x, "got": _show(g) if g is not None else "", "source": src[x]}
                for x, g in zip(q.inputs, q.got or (None,) * len(q.inputs), strict=True)
            ],
        }
        for q in result.questions
    ]
    return {"band": band, "questions": questions}


BOOK_SHA256_DEFINITION: Final = (
    "book_sha256 = sha256 of the answer book file's bytes as read at plan time "
    '("" when no book file exists); book_head = record_hash of its last record '
    '("" for an empty book). Both are sealed: the bytes pin the file, the head pins the chain '
    "every attempt sidecar's rule_d.book_head must equal."
)

PLAN_RECORD: Final = "rule_d_plan.jsonl"
"""Beside the journal: one line per `saddle run --rule-d` plan step, appended, never rewritten."""


def book_sha256(config: RuleDConfig) -> str:
    """sha256 of the book file's bytes, "" when there is no book file (an empty book)."""
    if config.book is None or not config.book.exists():
        return ""
    return hashlib.sha256(config.book.read_bytes()).hexdigest()


def plan_record(loaded: Loaded, classes: Sequence[CensusClass]) -> dict[str, Any]:
    """The run-level seal: the plan-time census and the book's identity (file bytes + head)."""
    live, _suspended = loaded.book._class_keys(loaded.callable_, loaded.ctx)
    return {
        "schema": "saddle-rule-d-plan/1",
        "reference_set_id": loaded.refset.set_id,
        "table": loaded.config.table,
        "callable": loaded.callable_,
        "panel_sha": loaded.ctx.panel_sha,
        "book_path": "" if loaded.config.book is None else str(loaded.config.book),
        "book_sha256": book_sha256(loaded.config),
        "book_head": loaded.book.head(),
        "book_sha256_definition": BOOK_SHA256_DEFINITION,
        "book_classes": sorted(live),
        "census_budget": loaded.config.census_budget,
        "over_budget": len(classes) > loaded.config.census_budget,
        "census": {
            "split": [
                {"cls": c.key, "members": list(c.members), "signatures": list(c.signatures)}
                for c in classes
            ],
        },
    }


def _redacted(value: Any) -> Any:
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, Mapping):
        return {k: _redacted(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_redacted(v) for v in value]
    return value


def seal_plan(journal_path: Path, record: Mapping[str, Any]) -> Path:
    """Append the plan record, secrets redacted as in a sidecar, beside the journal."""
    path = journal_path.parent / PLAN_RECORD
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(_redacted(record), sort_keys=True) + "\n")
    return path


def verdict(loaded: Loaded, workdir: Path, *, runner: Runner = run_capture) -> RuleDDecision:
    """Verdict time: rule D on the tree in `workdir`, with the book's authorities."""
    tree = tree_answers(
        workdir, loaded.refset.path, loaded.refset.function, loaded.inputs, runner=runner
    )
    res = loaded.book.resolve(loaded.callable_, loaded.inputs, loaded.ctx)
    authorities = ab.authorities(res, loaded.ctx, loaded.book, loaded.callable_)
    result = rule_d.consensus_verdict(
        loaded.refset.function,
        loaded.inputs,
        tree,
        loaded.references,
        authorities=authorities,
        classes={x: loaded.key(x) for x in loaded.inputs},
    )
    fields = sealed_fields(result, loaded)
    budget = loaded.config.verdict_budget
    over = len(result.asks) > budget
    fields["over_budget"] = over
    if result.verdict == "refuse":
        retry = loaded.config.retry
        fields["retry"] = retry
        return RuleDDecision(result.verdict, False, True, miss_text(result), fields, retry)
    if result.verdict == "question" or over:
        head = f"rule D {result.verdict}: {len(result.asks)} ask(s) (budget {budget})"
        if over:
            head += "; over budget, stopping (no ask dropped)"
        text = f"{head}\n{question_text(result)}"
        fields["question"] = text
        return RuleDDecision(result.verdict, True, False, text, fields)
    return RuleDDecision(result.verdict, False, False, f"rule D {result.verdict}", fields)


def hook(loaded: Loaded) -> Callable[[str, Path], RuleDDecision | None]:
    """The run's verdict step: impl nodes whose tree holds the store's module only."""

    def check(kind: str, workdir: Path) -> RuleDDecision | None:
        if kind != "impl" or not (workdir / loaded.refset.path).is_file():
            return None
        return verdict(loaded, workdir)

    return check
