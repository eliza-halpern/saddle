"""P1's extraction: the three code-blind passes, the mini-references, the sealed file.

`extract` runs once per task text, before any tree is audited:

- **P-a propose** (one call, `PROPOSE_TEMPERATURE`): for every unit of the
  task text, 1-3 inputs on a boundary the unit draws, or a
  `not-executable` mark from the closed list. No outcomes.
- **P-b predict** (`K_PREDICTORS` calls at `PREDICT_TEMPERATURE` with
  distinct `PREDICT_SEEDS`, each blind to the others): every input's
  outcome, a derivation, the `decides` span, and per unit one
  mini-reference (`def ref(...)` or a discriminating `def ok(inp, out)`).
- The mini-references run here, once (`run_references`): whitelisted
  (`task_examples.reference_problem`), in an empty directory with no repo
  on the path (`python -I -S`), sandboxed and capped like the examples,
  `REFERENCE_CALL_TIMEOUT_S` per call and `REFERENCE_TOTAL_TIMEOUT_S` per
  predictor. Anything that goes wrong leaves its example a question.
- **P-c alternatives** (one call): other readings of every input the
  predictors agreed on, each with the words that allow it.

Every pass sees the task text, its units and the baseline's public
signatures (`baseline_listing`); the docstring of every name the task text
mentions is hidden (D-2). None sees the tree under audit or a test file.
The model is called only through `VllmClient.complete`; every threshold
(k, temperatures, seeds, caps, timeouts, allowlists) is a constant here or
in `task_examples`, never the model's.

- **Probes** (D-9, no model): the known-correct implementations given to
  `extract` run every example once (`task_requirements.run_probes`), and
  their outcomes are sealed. With none, which is the product without a user
  reference, route (b) can only ask.

Layering: calls the model and runs subprocesses; above `task_requirements`.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import re
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Final, Protocol

from saddle.evidence import run_capture, tree_memory_limit
from saddle.task_examples import (
    K_PREDICTORS,
    MAX_EXAMPLES,
    MAX_INPUTS_PER_UNIT,
    NOT_EXECUTABLE,
    REFERENCE_CALL_TIMEOUT_S,
    REFERENCE_MODULES,
    REFERENCE_TOTAL_TIMEOUT_S,
    Example,
    Outcome,
    args_problem,
    decode_value,
    encode_value,
    literal_text,
    parse_value,
    reference_problem,
)
from saddle.task_prompts import ALTERNATIVES, PREDICT, PROPOSE, SNIPPET_RULES
from saddle.task_requirements import ProbeTree, Runner, probe_listing, run_probes, seal
from saddle.task_units import Units, task_units
from saddle.vllm import VllmResponseError

PROPOSE_TEMPERATURE: Final = 0.6
PREDICT_TEMPERATURE: Final = 0.8
"""Above zero: k samples at temperature 0.0 are one sample."""
ALTERNATIVES_TEMPERATURE: Final = 0.6
PREDICT_SEEDS: Final[tuple[int, ...]] = (11, 23, 37)
PROPOSE_SEED: Final = 5
ALTERNATIVES_SEED: Final = 7
PASS_MAX_TOKENS: Final = 16384
PASS_EFFORT: Final = "low"
RETRY_SEED_OFFSET: Final = 1000
"""A reply holding no JSON object, whole or cut, is asked again once, at its seed plus this:
the same seed would reproduce the same bytes."""

HIDDEN: Final = "(docstring hidden: the task text refers to this name)"
"""What replaces the docstring of a baseline name the task text mentions (D-2)."""

EMPTY_BASELINE: Final = "(none: the repository has no Python source before the task)"

_TEST_NAMES: Final = re.compile(r"(^|/)(test_[^/]*|[^/]*_test)\.py$|(^|/)conftest\.py$")
_TEST_DIRS: Final = frozenset({"tests", "test"})


class Completer(Protocol):
    """The one client call P1 makes (`VllmClient.complete`)."""

    def complete(
        self,
        prompt: str,
        *,
        max_tokens: int = ...,
        temperature: float = ...,
        reasoning_effort: str = ...,
        seed: int | None = ...,
    ) -> str: ...


# -- D-2: what the passes see of the baseline ---------------------------------


def _is_test(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return bool(_TEST_NAMES.search(path)) or bool(_TEST_DIRS & set(parts[:-1]))


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> str:
    if isinstance(node, ast.ClassDef):
        bases = ", ".join(ast.unparse(b) for b in node.bases)
        return f"class {node.name}({bases})" if bases else f"class {node.name}"
    return f"def {node.name}({ast.unparse(node.args)})"


def _mentioned(name: str, text: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text) is not None


def baseline_listing(sources: Mapping[str, str], task_text: str) -> tuple[str, list[str]]:
    """The public signatures and docstrings of the baseline's non-test sources.

    Returns the listing and the names whose docstrings it hid: every public
    name the task text mentions (as a code span, a call or a bare token),
    and every public name of a module whose file the text names.
    """
    lines: list[str] = []
    hidden: list[str] = []
    for path in sorted(p for p in sources if p.endswith(".py") and not _is_test(p)):
        try:
            tree = ast.parse(sources[path])
        except SyntaxError:
            continue
        module = PurePosixPath(path)
        whole = module.name in task_text or _mentioned(module.stem, task_text)
        found: list[str] = []
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                continue
            if node.name.startswith("_"):
                continue
            members = [node]
            if isinstance(node, ast.ClassDef):
                members += [
                    m
                    for m in node.body
                    if isinstance(m, ast.FunctionDef | ast.AsyncFunctionDef)
                    and (not m.name.startswith("_") or m.name == "__init__")
                ]
            for m in members:
                qual = m.name if m is node else f"{node.name}.{m.name}"
                indent = "" if m is node else "    "
                found.append(f"{indent}{_signature(m)}")
                doc = ast.get_docstring(m)
                if doc is None:
                    continue
                if whole or _mentioned(m.name, task_text):
                    hidden.append(f"{path}:{qual}")
                    found.append(f"{indent}    {HIDDEN}")
                else:
                    found.extend(f"{indent}    {line}" for line in doc.splitlines())
        if found:
            lines += [f"# {path}", *found, ""]
    return ("\n".join(lines).rstrip() or EMPTY_BASELINE), hidden


def baseline_sources(repo: Path, baseline: str) -> dict[str, str]:
    """Every tracked `.py` file at `baseline`, by path; {} for an unborn or absent repo."""
    listed = subprocess.run(
        ["git", "-C", str(repo), "ls-tree", "-r", "-z", "--name-only", baseline],
        capture_output=True,
        text=True,
        check=False,
    )
    if listed.returncode != 0:
        return {}
    paths = [p for p in listed.stdout.split("\0") if p.endswith(".py")]
    return {
        path: subprocess.run(
            ["git", "-C", str(repo), "show", f"{baseline}:{path}"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        for path in paths
    }


# -- replies ------------------------------------------------------------------


def reply_json(raw: str) -> dict[str, Any] | None:
    """The JSON object a reply holds (its outermost braces), or None."""
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        found: dict[str, Any] = json.loads(raw[start : end + 1])  # braces: an object or an error
    except ValueError:
        return None
    return found


def _units_text(units: Units) -> str:
    return "\n".join(f"{u.id} ({u.modality}): {u.text}" for u in units.units)


@dataclass(frozen=True)
class Call:
    """One model call, sealed whole."""

    name: str
    seed: int
    temperature: float
    raw: str
    error: str = ""
    cut_at: int | None = None
    """The token cap the reply was cut at (`finish_reason=length`): the
    `max_tokens` the call was sent. None when it ended by itself, or failed
    some other way."""

    def to_dict(self) -> dict[str, object]:
        return {
            "pass": self.name,
            "seed": self.seed,
            "temperature": self.temperature,
            "raw_sha256": hashlib.sha256(self.raw.encode()).hexdigest(),
            "raw": self.raw,
            **({"error": self.error} if self.error else {}),
            **({"cut_at": self.cut_at} if self.cut_at is not None else {}),
        }


def _call(client: Completer, name: str, prompt: str, seed: int, temperature: float) -> Call:
    try:
        raw = client.complete(
            prompt,
            max_tokens=PASS_MAX_TOKENS,
            temperature=temperature,
            reasoning_effort=PASS_EFFORT,
            seed=seed,
        )
    except Exception as exc:  # a failed call decides nothing; it is sealed as such
        cut = isinstance(exc, VllmResponseError) and exc.finish_reason == "length"
        error = f"{type(exc).__name__}: {exc}"
        return Call(name, seed, temperature, "", error, PASS_MAX_TOKENS if cut else None)
    return Call(name, seed, temperature, raw)


def _call_parsed(
    client: Completer, name: str, prompt: str, seed: int, temperature: float
) -> list[Call]:
    """`_call`, asked once more on a fresh seed when its reply holds no JSON
    object, whole or cut at the token cap; every call made, in order. The
    last one is the pass's reply. A call that failed otherwise (the server)
    is sealed as what it was, not asked again."""
    first = _call(client, name, prompt, seed, temperature)
    if (first.error and first.cut_at is None) or reply_json(first.raw) is not None:
        return [first]
    return [first, _call(client, name, prompt, seed + RETRY_SEED_OFFSET, temperature)]


def cut_calls(record: Mapping[str, Any]) -> str:
    """The sealed record's calls cut at their token cap, named, for a summary
    line; "" when none was. A cut reply is never a quiet absence."""
    calls = [c for c in record.get("calls", []) if isinstance(c, dict)]
    cut = [c for c in calls if c.get("cut_at") is not None]
    if not cut:
        return ""
    named = ", ".join(f"{c.get('pass')} seed {c.get('seed')}" for c in cut)
    caps = sorted({int(c["cut_at"]) for c in cut})
    cap = "/".join(str(n) for n in caps)
    return f"{len(cut)} of {len(calls)} model call(s) cut at the {cap}-token cap ({named})"


# -- P-a ------------------------------------------------------------------------


@dataclass(frozen=True)
class Proposed:
    inputs: list[dict[str, Any]]
    not_executable: list[dict[str, str]]
    cut: list[str]
    unanswered: list[str]


def proposed(reply: dict[str, Any] | None, units: Units) -> Proposed:
    """P-a's inputs, capped and checked; its marks from the closed list only.

    An input citing no known unit is dropped; inputs beyond
    `MAX_INPUTS_PER_UNIT` per unit or `MAX_EXAMPLES` in all are cut and
    named. A unit with no input and no valid mark is named `unanswered`:
    the model cannot remove a unit silently.
    """
    known = {u.id for u in units.units}
    raw_inputs = reply.get("inputs", []) if reply else []
    raw_marks = reply.get("not_executable", []) if reply else []
    marks = [
        {"unit": str(m.get("unit")), "reason": str(m.get("reason"))}
        for m in raw_marks
        if isinstance(m, dict) and m.get("unit") in known and m.get("reason") in NOT_EXECUTABLE
    ]
    per_unit: dict[str, int] = {}
    inputs: list[dict[str, Any]] = []
    cut: list[str] = []
    for raw in raw_inputs:
        if not isinstance(raw, dict) or not isinstance(raw.get("call"), str):
            continue
        cited = [str(u) for u in raw.get("units", []) if u in known]
        if not cited:
            continue
        first = cited[0]
        if per_unit.get(first, 0) >= MAX_INPUTS_PER_UNIT or len(inputs) >= MAX_EXAMPLES:
            cut.append(first)
            continue
        per_unit[first] = per_unit.get(first, 0) + 1
        inputs.append(
            {
                "id": f"E-{len(inputs) + 1:03d}",
                "units": cited,
                "setup": [str(s) for s in raw.get("setup", []) if isinstance(s, str)],
                "call": raw["call"],
                "args": [str(a) for a in raw.get("args", []) if _literal(str(a))],
            }
        )
    marked = {m["unit"] for m in marks}
    covered = {u for i in inputs for u in i["units"]} | marked | set(cut)
    unanswered = [u.id for u in units.units if u.id not in covered]
    return Proposed(inputs, marks, sorted(set(cut)), unanswered)


def _literal(text: str) -> bool:
    try:
        parse_value(text)
    except ValueError:
        return False
    return True


# -- P-b and the mini-references -------------------------------------------------


REFERENCE_DRIVER: Final = r"""
import inspect, json, signal, sys
from decimal import Decimal
from fractions import Fraction
job_path, out_path = sys.argv[1], sys.argv[2]
with open(job_path, encoding="utf-8") as fh:
    job = json.load(fh)
SAFE = {"Decimal": Decimal, "Fraction": Fraction, "frozenset": frozenset, "set": set}
SAFE["float"] = float


class _Hang(BaseException):
    pass


def _alarm(signum, frame):
    raise _Hang()


signal.signal(signal.SIGALRM, _alarm)


def lit(text):
    return eval(text, {"__builtins__": {}}, SAFE)


def timed(fn, *args):
    signal.setitimer(signal.ITIMER_REAL, job["timeout"])
    try:
        return fn(*args)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)


def run(item):
    namespace = {}
    try:
        timed(exec, compile(item["source"], "<p1-reference>", "exec"), namespace)
    except _Hang:
        return {"status": "timeout"}
    except BaseException as exc:
        return {"status": f"raised {type(exc).__name__} while defining it"}
    args = [lit(a) for a in item["args"]]
    if item["form"] == "ok":
        try:
            yes = timed(namespace["ok"], tuple(args), lit(item["predicted"]))
            no = timed(namespace["ok"], tuple(args), lit(item["other"]))
        except _Hang:
            return {"status": "timeout"}
        except BaseException as exc:
            return {"status": f"raised {type(exc).__name__}"}
        return {"status": "ran", "accepts": yes is True and no is False}
    try:
        # A call that cannot bind the input's args never ran: its TypeError is
        # about the signature, not the behaviour, so it is no outcome.
        inspect.signature(namespace["ref"]).bind(*args)
    except TypeError as exc:
        return {"status": f"could not call: {exc}"}
    try:
        value = timed(namespace["ref"], *args)
    except _Hang:
        return {"status": "timeout"}
    except BaseException as exc:
        return {"status": "ran", "raises": type(exc).__name__}
    try:
        return {"status": "ran", "value": encode_value(value)}
    except BaseException:
        return {"status": "not-canonical"}


with open(out_path, "w", encoding="utf-8") as fh:
    json.dump({item["id"]: run(item) for item in job["items"]}, fh)
"""


def _driver() -> str:
    return (
        "from __future__ import annotations\n" + inspect.getsource(encode_value) + REFERENCE_DRIVER
    )


def perturbed(outcome: Outcome) -> str | None:
    """A fixed mechanical perturbation of a value outcome, for the `ok` check:
    a list loses its last element (an empty one gains a 0), a number gains
    one, a bool flips, a string gains a character. None for anything else."""
    if outcome.kind != "value":
        return None
    value = outcome.value()
    if isinstance(value, bool):
        return literal_text(not value)
    if isinstance(value, int | float):
        return literal_text(value + 1)
    if isinstance(value, list):
        return literal_text(value[:-1] if value else [0])
    if isinstance(value, str):
        return literal_text(value + "x")
    return None


def _reference_form(source: str) -> str:
    tree = ast.parse(source)
    return next(n.name for n in tree.body if isinstance(n, ast.FunctionDef))


def run_references(
    sources: Mapping[str, str],
    examples: Sequence[Mapping[str, Any]],
    predictions: Mapping[str, Outcome | None],
    *,
    chosen: Mapping[str, list[str]] | None = None,
    runner: Runner = run_capture,
) -> dict[str, dict[str, Any]]:
    """One predictor's executed references: example id -> sealed `Reference` record.

    `sources` maps a unit id to that predictor's reference; an example uses
    the first of its units that has one. It is called with the args the
    predictor `chosen` for it, which bind to its own `ref`, when every value
    in them is written in the input (`args_problem`); those are sealed with
    the result. Without them, the proposal's `args` are used. Every call runs in one subprocess
    in an empty directory (`python -I -S`: no repo, no site-packages),
    whitelisted first. What cannot run is recorded as why, never as a result.
    """
    stdlib = frozenset(sys.stdlib_module_names)
    out: dict[str, dict[str, Any]] = {}
    items = []
    for e in examples:
        source = next((sources[u] for u in e["units"] if u in sources), None)
        predicted = predictions.get(e["id"])
        if source is None:
            out[e["id"]] = {"status": "missing"}
            continue
        problem = reference_problem(source, stdlib)
        if problem is not None:
            out[e["id"]] = {"status": f"refused: {problem}"}
            continue
        form = _reference_form(source)
        own = (chosen or {}).get(e["id"])
        if own is not None:
            problem = args_problem(own, e["setup"], e["call"])
            if problem is not None:
                out[e["id"]] = {"status": f"refused: its args: {problem}"}
                continue
        item = {
            "id": e["id"],
            "source": source,
            "form": form,
            "args": e["args"] if own is None else own,
            "chosen": own is not None,
        }
        if form == "ok":
            other = perturbed(predicted) if predicted is not None else None
            if predicted is None or other is None:
                out[e["id"]] = {"status": "not-discriminating", "form": "ok"}
                continue
            item |= {"predicted": predicted.text, "other": other}
        items.append(item)
    if not items:
        return out
    with tempfile.TemporaryDirectory(prefix="saddle-p1-ref-") as tmp:
        job, result = Path(tmp) / "job.json", Path(tmp) / "out.json"
        job.write_text(json.dumps({"items": items, "timeout": REFERENCE_CALL_TIMEOUT_S}))
        run = runner(
            [sys.executable, "-I", "-S", "-c", _driver(), str(job), str(result)],
            Path(tmp),
            timeout=REFERENCE_TOTAL_TIMEOUT_S,
            memory_limit=tree_memory_limit(),
        )
        try:
            got = json.loads(result.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            why = "timeout" if run.timed_out else f"crashed (exit {run.exit_code})"
            return out | {i["id"]: {"status": why} for i in items}
    for item in items:
        sealed = _sealed_reference(got.get(item["id"]), item["form"])
        out[item["id"]] = sealed | ({"args": item["args"]} if item["chosen"] else {})
    return out


def _sealed_reference(got: Any, form: str) -> dict[str, Any]:
    if not isinstance(got, dict) or got.get("status") != "ran":
        status = got.get("status") if isinstance(got, dict) else "no result"
        return {"status": str(status), "form": form}
    if form == "ok":
        return {"status": "ran", "form": "ok", "accepts": got.get("accepts") is True}
    if "raises" in got:
        return {"status": "ran", "outcome": {"kind": "raises", "text": str(got["raises"])}}
    try:
        text = literal_text(decode_value(got["value"]))
        Outcome.of("value", text)
    except (ValueError, KeyError, TypeError):
        return {"status": "not-canonical"}
    return {"status": "ran", "outcome": {"kind": "value", "text": text}}


def _inputs_text(inputs: Sequence[Mapping[str, Any]]) -> str:
    return "\n".join(
        json.dumps({k: i[k] for k in ("id", "units", "setup", "call", "args")}) for i in inputs
    )


def _outcome(raw: Any) -> Outcome | None:
    try:
        return Outcome.from_dict(raw) if isinstance(raw, dict) else None
    except (ValueError, KeyError):
        return None


@dataclass(frozen=True)
class Predictor:
    call: Call
    predictions: dict[str, dict[str, Any]]
    references: dict[str, str]
    args: dict[str, list[str]]
    """Input id -> the args it named for its reference (`run_references`)."""


def _missing(p: Predictor, listed: bool) -> str:
    """Why predictor `p` gave no outcome for one input: what happened, in
    words a question can quote (`task_examples.Prediction.missing`)."""
    call = p.call
    if call.cut_at is not None:
        return f"a prediction reply was cut at the {call.cut_at}-token cap"
    if call.error:
        return f"a prediction call failed ({call.error.split(':', 1)[0]})"
    if not call.raw.strip():
        return "a prediction call returned nothing"
    if reply_json(call.raw) is None:
        return "a prediction reply did not parse"
    if not listed:
        return "a prediction reply gave no outcome for this input"
    return "a prediction reply's outcome for this input did not parse"


def predictor(call: Call) -> Predictor:
    reply = reply_json(call.raw) or {}
    predictions = {
        str(p.get("input")): p for p in reply.get("predictions", []) if isinstance(p, dict)
    }
    references = {
        str(r.get("unit")): str(r.get("source"))
        for r in reply.get("references", [])
        if isinstance(r, dict) and isinstance(r.get("source"), str)
    }
    args = {
        i: [str(a) for a in p["args"]]
        for i, p in predictions.items()
        if isinstance(p.get("args"), list) and all(isinstance(a, str) for a in p["args"])
    }
    return Predictor(call, predictions, references, args)


# -- the whole extraction ---------------------------------------------------------


def extract(
    task_text: str,
    client: Completer,
    *,
    sources: Mapping[str, str] | None = None,
    model: str = "",
    runner: Runner = run_capture,
    probes: Sequence[ProbeTree] = (),
) -> dict[str, Any]:
    """Run P-a, P-b (with its mini-references) and P-c on `task_text`, then
    every example on each known-correct probe; the sealed record.

    `sources` is the baseline's Python sources by path (`baseline_sources`);
    None or {} for an empty repo. `probes` are checked before any model
    call (`probe_listing`); a bad one is a `RequirementsError`.
    """
    listed = probe_listing(probes)
    units = task_units(task_text)
    listing, hidden = baseline_listing(sources or {}, task_text)
    shown = {"task": task_text, "units": _units_text(units), "baseline": listing}
    *tried, propose = _call_parsed(
        client,
        "P-a",
        PROPOSE.format(
            **shown,
            max_inputs=MAX_INPUTS_PER_UNIT,
            reasons=", ".join(NOT_EXECUTABLE),
            snippet_rules=SNIPPET_RULES,
        ),
        PROPOSE_SEED,
        PROPOSE_TEMPERATURE,
    )
    plan = proposed(reply_json(propose.raw), units)
    calls = [*tried, propose]
    predictors: list[Predictor] = []
    if plan.inputs:
        prompt = PREDICT.format(
            **shown,
            inputs=_inputs_text(plan.inputs),
            modules=", ".join(sorted(REFERENCE_MODULES)),
        )
        with ThreadPoolExecutor(max_workers=K_PREDICTORS) as pool:
            done = list(
                pool.map(
                    lambda seed: _call_parsed(client, "P-b", prompt, seed, PREDICT_TEMPERATURE),
                    PREDICT_SEEDS,
                )
            )
        predictors = [predictor(c[-1]) for c in done]
        calls += [c for made in done for c in made]
    examples = []
    for inp in plan.inputs:
        preds = []
        for p in predictors:
            got = p.predictions.get(inp["id"], {})
            outcome = _outcome(got.get("outcome"))
            missing = "" if outcome is not None else _missing(p, inp["id"] in p.predictions)
            preds.append(
                {
                    "outcome": outcome.to_dict() if outcome is not None else None,
                    "decides": str(got.get("decides", "")),
                    "derivation": str(got.get("derivation", "")),
                    "raw_sha256": hashlib.sha256(p.call.raw.encode()).hexdigest(),
                    **({"missing": missing} if missing else {}),
                }
            )
        examples.append({**inp, "predictions": preds, "references": [], "alternatives": []})
    by_id = {e["id"]: e for e in examples}

    def run_one(p: Predictor) -> dict[str, dict[str, Any]]:
        predicted = {
            e["id"]: _outcome(p.predictions.get(e["id"], {}).get("outcome")) for e in examples
        }
        return run_references(p.references, plan.inputs, predicted, chosen=p.args, runner=runner)

    if predictors:
        with ThreadPoolExecutor(max_workers=K_PREDICTORS) as pool:
            for refs in pool.map(run_one, predictors):
                for eid, ref in refs.items():
                    by_id[eid]["references"].append(ref)
    decided = [e for e in examples if _agreed(e)]
    if decided:
        text = "\n".join(
            json.dumps(
                {
                    **{k: e[k] for k in ("id", "units", "setup", "call")},
                    "outcome": e["predictions"][0]["outcome"],
                }
            )
            for e in decided
        )
        *tried, alt = _call_parsed(
            client,
            "P-c",
            ALTERNATIVES.format(task=task_text, units=_units_text(units), decided=text),
            ALTERNATIVES_SEED,
            ALTERNATIVES_TEMPERATURE,
        )
        calls += [*tried, alt]
        for a in (reply_json(alt.raw) or {}).get("alternatives", []):
            _add_alternative(a, by_id, units)
    outcomes = run_probes(probes, listed, [Example.from_dict(e) for e in examples], runner=runner)
    for e in examples:
        e["probes"] = outcomes[e["id"]]
    return seal(
        {
            "task_text": task_text,
            "examples": examples,
            "not_executable": plan.not_executable,
            "cut": plan.cut,
            "unanswered": plan.unanswered,
            "model": model,
            "calls": [c.to_dict() for c in calls],
            "hidden_docstrings": hidden,
            "probes": listed,
        }
    )


def _agreed(example: Mapping[str, Any]) -> bool:
    outs = [_outcome(p["outcome"]) for p in example["predictions"]]
    first = outs[0] if outs else None
    return (
        len(outs) == K_PREDICTORS
        and first is not None
        and all(o is not None and o.same(first) for o in outs)
    )


def _add_alternative(raw: Any, by_id: Mapping[str, dict[str, Any]], units: Units) -> None:
    """Record P-c's reading if it names a known input, parses, differs from the
    decided outcome and quotes words of one of the input's units."""
    if not isinstance(raw, dict) or raw.get("input") not in by_id:
        return
    example = by_id[str(raw["input"])]
    outcome = _outcome(raw.get("outcome"))
    decided = _outcome(example["predictions"][0]["outcome"])
    words = str(raw.get("words", ""))
    texts = [u.text for u in units.units if u.id in example["units"]]
    if outcome is None or decided is None or outcome.same(decided):
        return
    if not words.strip() or not any(_squash(words) in _squash(t) for t in texts):
        return
    example["alternatives"].append({"outcome": outcome.to_dict(), "words": words})


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("`", "")).strip().lower()
