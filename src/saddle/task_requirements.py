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

"Could not call" is decided before the call (K2 R-3): every call in an
example's expression that reaches a callee defined in the tree (after
`inspect.unwrap`) first binds its arguments to `inspect.signature(callee)`.
A signature that cannot be read or cannot bind means the example could not
call the tree; once it binds, every exception the call raises, a
`TypeError` or `AttributeError` from inside the callee included, is the
tree's outcome. A wrapper written with `functools.wraps` is bound against
the inner function; one without it hides the inner signature, and only a
known-correct probe guards that case.

The known-correct probes (D-9) run through the same driver, once, at
extraction (`run_probes`): each probe tree is an implementation whose
correctness comes from outside the model (`task_examples.PROBE_SOURCES`),
named by its content hash (`tree_sha256`), and every example's outcome on
it is sealed in the file. Nothing about a probe runs per audit, except the
check that the tree under audit is not itself one of them.

Layering: executes tree code through `evidence`, so it sits beside
`rule_d_run`, above `gates`.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
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
    PROBE_SOURCES,
    Example,
    Outcome,
    TreeOutcome,
    decode_value,
    literal_text,
    raise_names,
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
    probes: tuple[tuple[str, str], ...] = ()
    """The sealed known-correct probes, as (sha256, source)."""


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
        probes = _sealed_probes(data)
        examples = tuple(
            Example.from_dict(_with_sources(e, dict(probes))) for e in data.get("examples", ())
        )
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
    return Requirements(
        text, units, examples, marks, cut, str(data["file_sha256"]), unanswered, probes
    )


def _sealed_probes(data: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    """The file's probe list, each probe once, each from a known source."""
    probes = tuple((str(p["sha256"]), str(p["source"])) for p in data.get("probes", ()))
    if len({sha for sha, _ in probes}) != len(probes):
        msg = "the same probe tree is sealed twice"
        raise ValueError(msg)
    bad = [src for _, src in probes if src not in PROBE_SOURCES]
    if bad:
        msg = f"probe source {bad[0]!r} is not one of {', '.join(PROBE_SOURCES)}"
        raise ValueError(msg)
    return probes


def _with_sources(example: Mapping[str, Any], sources: Mapping[str, str]) -> dict[str, Any]:
    """`example` with each probe outcome's source from the file's probe list.

    Every sealed probe must have exactly one outcome on every example: an
    example missing one could hide the probe that disagrees with it."""
    got = [str(p["sha256"]) for p in example.get("probes", ())]
    if sorted(got) != sorted(sources):
        msg = f"example {example.get('id')} does not carry one outcome per sealed probe"
        raise ValueError(msg)
    return {
        **example,
        "probes": [{**p, "source": sources[str(p["sha256"])]} for p in example.get("probes", ())],
    }


DRIVER_BODY: Final = r"""
import ast, builtins, inspect, json, os, signal, sys
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


class _NoCall(BaseException):
    pass


# The tree's own outcomes: every exception that left a call into the tree
# after its arguments bound, by id, with the callee it left (both kept alive,
# so that no id is reused); and every callee of the tree the call entered.
from_tree = {}
entered = []


def _in_tree(fn):
    # `fn`, after `inspect.unwrap`, is defined in a module of the tree.
    try:
        target = inspect.unwrap(fn)
        name = getattr(target, "__module__", None)
        path = getattr(sys.modules.get(name), "__file__", None) if isinstance(name, str) else None
    except Exception:
        return False
    return isinstance(path, str) and _rel(path) is not None


def __p1_call__(fn, /, *args, **kwargs):
    # "Could not call" is decided here, before the call: a callee of the tree
    # whose signature cannot be read, or cannot bind these arguments, never
    # ran. Once bound, whatever the call raises is the tree's outcome.
    if not _in_tree(fn):
        return fn(*args, **kwargs)
    try:
        signature = inspect.signature(fn)
    except Exception as exc:
        raise _NoCall(f"{type(exc).__name__}: no signature to bind the call to ({exc})")
    try:
        signature.bind(*args, **kwargs)
    except TypeError as exc:
        raise _NoCall(f"TypeError: {exc} (the call does not bind to the callee's signature)")
    entered.append(fn)
    try:
        return fn(*args, **kwargs)
    except BaseException as exc:
        from_tree.setdefault(id(exc), (exc, fn))
        raise


DRIVER_FILE = __p1_call__.__code__.co_filename


class _Calls(ast.NodeTransformer):
    def visit_Call(self, node):
        self.generic_visit(node)
        hook = ast.Name("__p1_call__", ast.Load())
        return ast.copy_location(ast.Call(hook, [node.func, *node.args], node.keywords), node)


def _innermost(exc):
    tb = exc.__traceback__
    last = None
    while tb is not None:
        last, tb = tb, tb.tb_next
    return last.tb_frame.f_code.co_filename if last is not None else ""


def _interface(exc):
    # An exception the example's own expression raised (a name or attribute
    # that does not resolve, an operation on what the tree returned) outside
    # any bound call into the tree.
    if id(exc) in from_tree:
        return False
    if isinstance(exc, (ImportError, _NoCall)):
        return True
    return isinstance(exc, (NameError, AttributeError, TypeError)) and _innermost(exc) in (
        HERE,
        DRIVER_FILE,
    )


_classes = None


def _tree_classes():
    # Top-level class names -> the tree files defining one of that name.
    global _classes
    if _classes is None:
        _classes = {}
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(
                d for d in dirnames
                if not d.startswith(".") and d not in ("__pycache__", "site-packages", "venv")
            )
            for name in sorted(n for n in filenames if n.endswith(".py")):
                path = os.path.realpath(os.path.join(dirpath, name))
                if _rel(path) is None:
                    continue
                try:
                    with open(path, encoding="utf-8") as fh:
                        body = ast.parse(fh.read()).body
                except (OSError, SyntaxError, ValueError, UnicodeDecodeError):
                    continue
                for node in body:
                    if isinstance(node, ast.ClassDef):
                        _classes.setdefault(node.name, set()).add(path)
    return _classes


def _exception_class(found):
    return isinstance(found, type) and issubclass(found, BaseException)


def _resolve(name, exc, callee):
    # A raise outcome's type name, resolved on this tree (K2 R-2): a builtin
    # of that name; else the name in the module that defines the callee
    # (after `inspect.unwrap`); else a top-level class of that name defined
    # in exactly one module of the tree. "match" or "differ" once it resolves
    # to an exception class (`isinstance`); else why it does not resolve.
    missing = object()
    found = getattr(builtins, name, missing)
    if found is missing and callee is not None:
        try:
            module = sys.modules.get(inspect.unwrap(callee).__module__)
            found = vars(module).get(name, missing) if module is not None else missing
        except Exception:
            found = missing
    if found is missing:
        files = _tree_classes().get(name, set())
        if not files:
            return "not found in the tree"
        if len(files) > 1:
            return f"ambiguous: defined in {len(files)} modules of the tree"
        (path,) = files
        # Only a loaded module can hold the class of an exception raised here.
        loaded = [
            vars(m)[name]
            for m in list(sys.modules.values())
            if isinstance(getattr(m, "__file__", None), str)
            and os.path.realpath(m.__file__) == path
            and name in vars(m)
        ]
        if any(not _exception_class(c) for c in loaded):
            return "not an exception class"
        return "match" if exc is not None and isinstance(exc, tuple(loaded)) else "differ"
    if not _exception_class(found):
        return "not an exception class"
    return "match" if exc is not None and isinstance(exc, found) else "differ"


def _typed(got, example, exc):
    if got["kind"] in ("value", "raises", "opaque") and example.get("names"):
        callee = from_tree[id(exc)][1] if exc is not None and id(exc) in from_tree else (
            entered[-1] if entered else None
        )
        got["types"] = {n: _resolve(n, exc, callee) for n in example["names"]}
    return got


def run(example):
    namespace = {"__name__": "__p1_example__", "__p1_call__": __p1_call__}
    stage = "setup"
    try:
        setup = compile("\n".join(example["setup"]), HERE, "exec")
        hooked = _Calls().visit(ast.parse(example["call"], mode="eval"))
        call = compile(ast.fix_missing_locations(hooked), HERE, "eval")
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
        said = str(exc)[:300] if isinstance(exc, _NoCall) else (
            f"{type(exc).__name__}: {str(exc)[:200]}"
        )
        if stage == "setup" or _interface(exc):
            return {"kind": "could-not-call", "detail": f"{stage}: {said}"}
        names = [c.__name__ for c in type(exc).__mro__]
        return _typed({"kind": "raises", "raises": names, "detail": said}, example, exc)
    try:
        got = {"kind": "value", "value": encode_value(value)}
    except BaseException as exc:
        got = {"kind": "opaque", "detail": f"{type(value).__name__} ({type(exc).__name__})"}
    return _typed(got, example, None)


results = {}
for example in job["examples"]:
    del ran[:]
    seen.clear()
    from_tree.clear()
    del entered[:]
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
            send.append(
                {"id": e.id, "setup": list(e.setup), "call": e.call, "names": raise_names(e)}
            )
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


# -- the known-correct probes (D-9), run once at extraction ----------------------

PROBE_FILES: Final = (".py", ".toml", ".cfg")
"""The files a probe tree is identified by: its source and its packaging."""
_UNHASHED_DIRS: Final = frozenset({".git", "__pycache__", ".venv", "venv"})


def tree_sha256(root: Path) -> str:
    """A tree's identity: the sha256 over each `PROBE_FILES` file's relative
    path and content hash, in sorted order. Caches, git data and build
    leftovers do not change it."""
    digest = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _UNHASHED_DIRS)
        for name in sorted(n for n in filenames if n.endswith(PROBE_FILES)):
            path = Path(dirpath) / name
            content = hashlib.sha256(path.read_bytes()).hexdigest()
            digest.update(f"{path.relative_to(root).as_posix()}\0{content}\n".encode())
    return digest.hexdigest()


@dataclass(frozen=True)
class ProbeTree:
    """A known-correct implementation of the task, and where its correctness comes from."""

    path: Path
    source: str
    """One of `task_examples.PROBE_SOURCES`: `oracle-pass` (a bench tree the
    task's oracle passed) or `user` (a reference the user supplied)."""


def probe_listing(probes: Sequence[ProbeTree]) -> list[dict[str, str]]:
    """The sealed probe list, `RequirementsError` for a probe that cannot be one.

    Checked before any model call: a missing directory, an unknown source,
    or one tree given twice (it would count as two probes) stops the
    extraction."""
    listed: list[dict[str, str]] = []
    for probe in probes:
        if probe.source not in PROBE_SOURCES:
            msg = f"probe source {probe.source!r} is not one of {', '.join(PROBE_SOURCES)}"
            raise RequirementsError(msg)
        if not probe.path.is_dir():
            msg = f"probe tree {probe.path} is not a directory"
            raise RequirementsError(msg)
        sha = tree_sha256(probe.path)
        if any(p["sha256"] == sha for p in listed):
            msg = f"probe tree {probe.path} is the same tree as an earlier probe"
            raise RequirementsError(msg)
        listed.append({"sha256": sha, "source": probe.source})
    return listed


def run_probes(
    probes: Sequence[ProbeTree],
    listed: Sequence[Mapping[str, str]],
    examples: Sequence[Example],
    *,
    runner: Runner = run_capture,
) -> dict[str, list[dict[str, Any]]]:
    """Every example's sealed outcome on every probe: example id -> one record per probe.

    Each probe runs in a private copy, through `run_examples` (the audit's
    own driver, sandbox and caps). What could not run is recorded as why,
    never as an outcome, so it can only leave an example a question."""
    out: dict[str, list[dict[str, Any]]] = {e.id: [] for e in examples}
    for probe, entry in zip(probes, listed, strict=True):
        with tempfile.TemporaryDirectory(prefix="saddle-p1-probe-") as scratch:
            private = Path(scratch) / "tree"
            shutil.copytree(probe.path, private, ignore=shutil.ignore_patterns(".git"))
            got = run_examples(private, examples, runner=runner) if examples else {}
        for e in examples:
            record = (
                {"status": f"crashed: {got}"}
                if isinstance(got, str)
                else _probe_record(got.get(e.id))
            )
            out[e.id].append({"sha256": entry["sha256"], **record})
    return out


def _probe_record(got: TreeOutcome | None) -> dict[str, Any]:
    if got is None:
        return {"status": "no result"}
    if got.kind == "raises" and got.raises and got.raises[0].isidentifier():
        return {
            "status": "ran",
            "outcome": {"kind": "raises", "text": got.raises[0]},
            "raises": list(got.raises),
        }
    if got.kind == "value":
        try:
            text = literal_text(decode_value(got.value))
            Outcome.of("value", text)
        except (ValueError, KeyError, TypeError):
            return {"status": "not-canonical"}
        return {"status": "ran", "outcome": {"kind": "value", "text": text}}
    status = {"could-not-call": "could not call", "hang": "HANG"}.get(got.kind, got.kind)
    return {"status": f"{status}: {got.detail}" if got.detail else status}


PROBE_IS_TREE: Final = "the tree under audit is one of the sealed known-correct probes"


def check_tree(
    copy: Path, baseline: str, requirements: Path, *, runner: Runner = run_capture
) -> TaskRequirementsCheck:
    """The `task-requirements` gate on the tree at `copy` against `baseline`."""
    try:
        req = load(requirements)
    except RequirementsError as exc:
        return check_task_requirements({}, Units((), frozenset()), [], cannot_run=str(exc))
    if req.probes and tree_sha256(copy) in {sha for sha, _ in req.probes}:
        # A probe gates the examples; judging it with them would be tautological.
        return check_task_requirements({}, req.units, req.examples, cannot_run=PROBE_IS_TREE)
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
