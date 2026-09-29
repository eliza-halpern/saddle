"""P1 at audit time: the sealed requirements file and the example driver.

`task-requirements.json` is written once per task text by the extraction
(`task_passes.extract`) and read here. `load` checks every hash in it
before a single example runs: the task text's, the parser's
(`task_units`), the whitelists' (`task_examples`), the prompt templates'
(`task_prompts`) and the file's own; the units are recomputed from the
task text and must be the ones sealed. Any mismatch is a
`RequirementsError`, which the gate reports as a question ("P1 could not
run: ..."), never a pass, never `not-proven`, never a refusal.

`run_examples` runs every example on a tree in one subprocess
(`DRIVER`), through `evidence.run_capture` under the tree's memory cap and
`sandbox.confine`, the tests gate's boundary: each example in a fresh
namespace, under a per-example timer (`EXAMPLE_TIMEOUT_S`), with the lines
of the tree it ran traced. `check_tree` is the gate on one tree.

Layering: executes tree code through `evidence`, so it sits beside
`rule_d_run`, above `gates`.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import shutil
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from saddle import task_examples, task_prompts, task_units
from saddle.evidence import (
    CapturedRun,
    changed_statements,
    git_diff,
    run_capture,
    tree_memory_limit,
)
from saddle.gates import TaskRequirementsCheck, check_task_requirements
from saddle.rule_d_run import TREE_TIMEOUT_S
from saddle.task_examples import (
    EXAMPLE_TIMEOUT_S,
    NOT_EXECUTABLE,
    Example,
    TreeOutcome,
    snippet_problem,
)
from saddle.task_units import Units

FILE_VERSION: Final = 1

Runner = Callable[..., CapturedRun]


class RequirementsError(Exception):
    """Why P1 could not use a requirements file; the gate's question says it."""


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _module_sha(module: object) -> str:
    return sha256_text(inspect.getsource(module))  # type: ignore[arg-type]


def build_hashes() -> dict[str, str]:
    """The hashes of the P1 build a file must have been sealed by."""
    return {
        "parser_sha256": _module_sha(task_units),
        "whitelist_sha256": _module_sha(task_examples),
        "templates_sha256": sha256_text("\x00".join(task_prompts.TEMPLATES)),
    }


def canonical(record: Mapping[str, Any]) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def seal(record: Mapping[str, Any]) -> dict[str, Any]:
    """`record` with the build's hashes, the task's, the units and its own hash.

    `record` holds `task_text` and `examples`, and may hold
    `not_executable`, `cut` and the extraction's own evidence (model id,
    seeds, temperatures, raw outputs, hidden docstrings)."""
    text = str(record["task_text"])
    body = {
        **record,
        **build_hashes(),
        "version": FILE_VERSION,
        "task_sha256": sha256_text(text),
        "units": [{"id": u.id, "text": u.text} for u in task_units.task_units(text).units],
    }
    body.pop("file_sha256", None)
    return {**body, "file_sha256": sha256_text(canonical(body))}


@dataclass(frozen=True)
class Requirements:
    """A requirements file every hash of which checked out."""

    task_text: str
    units: Units
    examples: tuple[Example, ...]
    not_executable: tuple[tuple[str, str], ...]
    cut: tuple[str, ...]
    sha256: str
    unanswered: tuple[str, ...] = ()
    """Units P-a gave neither an input nor a reason for: named, never dropped."""


MISMATCH: Final = "requirements file does not match the task"


def load(path: Path, task_text: str | None = None) -> Requirements:
    """The requirements file at `path`, every hash checked; `RequirementsError` if any fails.

    With `task_text`, the file must also be the one sealed for that text.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        msg = f"requirements file cannot be read: {exc}"
        raise RequirementsError(msg) from exc
    if not isinstance(data, dict) or data.get("version") != FILE_VERSION:
        msg = "requirements file is not a version-1 task-requirements record"
        raise RequirementsError(msg)
    body = {k: v for k, v in data.items() if k != "file_sha256"}
    if data.get("file_sha256") != sha256_text(canonical(body)):
        msg = f"{MISMATCH}: its own hash does not match its contents"
        raise RequirementsError(msg)
    text = data.get("task_text")
    if not isinstance(text, str) or data.get("task_sha256") != sha256_text(text):
        msg = f"{MISMATCH}: the task text hash differs"
        raise RequirementsError(msg)
    if task_text is not None and sha256_text(task_text) != data["task_sha256"]:
        msg = f"{MISMATCH}: it was sealed for another task text"
        raise RequirementsError(msg)
    for key, value in build_hashes().items():
        if data.get(key) != value:
            msg = f"{MISMATCH}: sealed by another P1 build ({key.removesuffix('_sha256')} differs)"
            raise RequirementsError(msg)
    units = task_units.task_units(text)
    if data.get("units") != [{"id": u.id, "text": u.text} for u in units.units]:
        msg = f"{MISMATCH}: the units differ from the task text's"
        raise RequirementsError(msg)
    try:
        examples = tuple(Example.from_dict(e) for e in data.get("examples", ()))
        marks = tuple((str(m["unit"]), str(m["reason"])) for m in data.get("not_executable", ()))
        cut = tuple(str(u) for u in data.get("cut", ()))
        unanswered = tuple(str(u) for u in data.get("unanswered", ()))
    except (KeyError, TypeError, ValueError) as exc:
        msg = f"requirements file is malformed: {exc!r}"
        raise RequirementsError(msg) from exc
    bad = [r for _, r in marks if r not in NOT_EXECUTABLE]
    if bad:
        msg = f"requirements file is malformed: not-executable reason {bad[0]!r} is not allowed"
        raise RequirementsError(msg)
    return Requirements(text, units, examples, marks, cut, str(data["file_sha256"]), unanswered)


DRIVER_BODY: Final = r"""
import json, os, signal, sys
job_path, out_path = sys.argv[1], sys.argv[2]
with open(job_path, encoding="utf-8") as fh:
    job = json.load(fh)
root = os.path.realpath(".")
sys.path[:0] = [os.path.join(root, "src"), root]
HERE = "<p1-example>"
rels = {}
ran = []
seen = set()


def _rel(filename):
    if filename not in rels:
        real = os.path.realpath(filename) if not filename.startswith("<") else ""
        inside = real.startswith(root + os.sep) and not any(
            part in real for part in ("/site-packages/", "/.venv/", "/venv/")
        )
        rels[filename] = os.path.relpath(real, root) if inside else None
    return rels[filename]


def _local(frame, event, arg):
    if event == "line":
        key = (frame.f_code.co_filename, frame.f_lineno)
        if key not in seen:
            seen.add(key)
            rel = _rel(frame.f_code.co_filename)
            name = getattr(frame.f_code, "co_qualname", frame.f_code.co_name)
            ran.append(f"{rel}:{frame.f_lineno} ({name})")
    return _local


def _global(frame, event, arg):
    # Module and class bodies run at import, not because the example called
    # them: only function frames (CO_NEWLOCALS) are traced.
    if not frame.f_code.co_flags & 0x2:
        return None
    return _local if _rel(frame.f_code.co_filename) is not None else None


class _Hang(BaseException):
    pass


def _alarm(signum, frame):
    raise _Hang()


signal.signal(signal.SIGALRM, _alarm)


def _innermost(exc):
    tb = exc.__traceback__
    last = None
    while tb is not None:
        last, tb = tb, tb.tb_next
    return last.tb_frame.f_code.co_filename if last is not None else ""


def _interface(exc):
    if isinstance(exc, ImportError):
        return True
    return isinstance(exc, (NameError, AttributeError, TypeError)) and _innermost(exc) == HERE


def run(example):
    namespace = {"__name__": "__p1_example__"}
    stage = "setup"
    try:
        setup = compile("\n".join(example["setup"]), HERE, "exec")
        call = compile(example["call"], HERE, "eval")
        signal.setitimer(signal.ITIMER_REAL, job["timeout"])
        sys.settrace(_global)
        try:
            exec(setup, namespace)
            stage = "call"
            value = eval(call, namespace)
        finally:
            sys.settrace(None)
            signal.setitimer(signal.ITIMER_REAL, 0)
    except _Hang:
        return {"kind": "hang", "detail": f"no outcome within {job['timeout']} s"}
    except BaseException as exc:
        said = f"{type(exc).__name__}: {str(exc)[:200]}"
        if stage == "setup" or _interface(exc):
            return {"kind": "could-not-call", "detail": f"{stage}: {said}"}
        names = [c.__name__ for c in type(exc).__mro__]
        return {"kind": "raises", "raises": names, "detail": said}
    try:
        return {"kind": "value", "value": encode_value(value)}
    except BaseException as exc:
        return {"kind": "opaque", "detail": f"{type(value).__name__} ({type(exc).__name__})"}


results = {}
for example in job["examples"]:
    del ran[:]
    seen.clear()
    got = run(example)
    got["ran"] = list(ran)
    results[example["id"]] = got
with open(out_path, "w", encoding="utf-8") as fh:
    json.dump(results, fh)
"""

DRIVER: Final = (
    "from __future__ import annotations\n"
    + inspect.getsource(task_examples.encode_value)
    + DRIVER_BODY
)
"""The example driver: `encode_value`'s own source, then the loop."""


def run_examples(
    workdir: Path, examples: Sequence[Example], *, runner: Runner = run_capture
) -> dict[str, TreeOutcome] | str:
    """Every example's outcome on the tree at `workdir`, or why the driver could not run.

    An example the snippet whitelist refuses is never sent; it reads as
    "could not call". A driver that exceeds `TREE_TIMEOUT_S`, or leaves no
    readable result, is a reason string, which the gate turns into a question.
    """
    stdlib = frozenset(sys.stdlib_module_names)
    results: dict[str, TreeOutcome] = {}
    send = []
    for e in examples:
        problem = snippet_problem(e.setup, e.call, stdlib)
        if problem is None:
            send.append({"id": e.id, "setup": list(e.setup), "call": e.call})
        else:
            results[e.id] = TreeOutcome("could-not-call", detail=f"snippet refused: {problem}")
    if not send:
        return results
    with tempfile.TemporaryDirectory(prefix="saddle-p1-") as tmp:
        job, out = Path(tmp) / "job.json", Path(tmp) / "out.json"
        job.write_text(json.dumps({"examples": send, "timeout": EXAMPLE_TIMEOUT_S}))
        run = runner(
            ["python", "-c", DRIVER, str(job), str(out)],
            workdir,
            timeout=TREE_TIMEOUT_S,
            memory_limit=tree_memory_limit(),
            writable=(Path(tmp),),
        )
        if run.timed_out:
            return f"the example driver exceeded its {TREE_TIMEOUT_S:g} s wall"
        try:
            got = json.loads(out.read_text(encoding="utf-8"))
            parsed = {str(k): TreeOutcome.from_dict(v) for k, v in got.items()}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return f"the example driver crashed (exit {run.exit_code}): {run.stderr[-300:].strip()}"
    return {**results, **parsed}


def check_tree(
    copy: Path, baseline: str, requirements: Path, *, runner: Runner = run_capture
) -> TaskRequirementsCheck:
    """The `task-requirements` gate on the tree at `copy` against `baseline`."""
    try:
        req = load(requirements)
    except RequirementsError as exc:
        return check_task_requirements({}, Units((), frozenset()), [], cannot_run=str(exc))
    with tempfile.TemporaryDirectory(prefix="saddle-p1-tree-") as scratch:
        # A private copy: the examples run beside the tests (`Auditor`), and
        # nothing they write may reach the tree the tests are reading.
        private = Path(scratch) / "tree"
        shutil.copytree(copy, private, ignore=shutil.ignore_patterns(".git"))
        results = run_examples(private, req.examples, runner=runner)
    if isinstance(results, str):
        return check_task_requirements({}, req.units, req.examples, cannot_run=results)
    changed = {
        f"{Path(path).relative_to(copy)}:{line}"
        for path, line in changed_statements(copy, git_diff(copy, baseline))
    }
    return check_task_requirements(
        results,
        req.units,
        req.examples,
        changed=changed,
        not_executable=req.not_executable,
        cut=req.cut,
        unanswered=req.unanswered,
    )
