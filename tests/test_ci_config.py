"""The CI workflow, the lockfile and the version pins are checked, not trusted.

`check.sh` is the gate, and CI is only worth having if it runs that gate and
nothing weaker. A job that ran `pytest` directly, ended `./check.sh` with
`|| true`, or set `continue-on-error` would still show a green tick while the
local gate and CI disagreed. The checks here read the real files and run the
real tools; each has a known-good and a known-bad instance.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn

import pytest
from packaging.specifiers import SpecifierSet
from packaging.version import Version

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
CHECK_SH = ROOT / "check.sh"
VENV_BIN = Path(sys.executable).parent


# --- a reader for the YAML a workflow uses -------------------------------------------
#
# PyYAML is only a transitive dependency here and has no type stubs, so the
# workflow is read by a small reader for the subset GitHub workflows use:
# block mappings and sequences, plain and quoted scalars, flow lists of
# scalars, `|` and `>` block scalars, comments. Anything else (anchors, tags,
# flow mappings, multi-line plain scalars, tabs, several documents) raises, so
# a workflow shape the reader does not understand fails loudly instead of
# being read as something it is not. Keys stay strings: `on` is "on".


class UnsupportedYamlError(ValueError):
    pass


def _refuse(reason: str) -> NoReturn:
    raise UnsupportedYamlError(reason)


def _strip_comment(line: str) -> str:
    quote = ""
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "'\"" and (i == 0 or line[i - 1] in " \t[,:-"):
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i].rstrip()
    return line.rstrip()


def _scalar(text: str) -> Any:
    text = text.strip()
    if not text or text in ("null", "~"):
        return None
    if text in ("true", "false"):
        return text == "true"
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if text[0] == '"':
        if len(text) < 2 or text[-1] != '"':
            _refuse(f"unterminated double-quoted scalar: {text!r}")
        return text[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    if text[0] == "'":
        if len(text) < 2 or text[-1] != "'":
            _refuse(f"unterminated single-quoted scalar: {text!r}")
        return text[1:-1].replace("''", "'")
    if text[0] == "[":
        if text[-1] != "]":
            _refuse(f"multi-line flow list: {text!r}")
        inner = text[1:-1].strip()
        return [_scalar(part) for part in inner.split(",")] if inner else []
    if text[0] in "{&*!@`%|>":
        _refuse(f"unsupported scalar start: {text!r}")
    return text


def _split_key(text: str) -> tuple[str, str]:
    match = re.match(r"^([A-Za-z0-9_][A-Za-z0-9_.\-/]*):(?: +(.*))?$", text)
    if not match:
        _refuse(f"not a plain `key: value` line: {text!r}")
    return match.group(1), match.group(2) or ""


def read_yaml(text: str) -> Any:
    """Parse the supported YAML subset into dicts, lists and scalars."""
    raw = text.split("\n")
    if "\t" in text:
        _refuse("tab characters")
    if any(line.startswith(("---", "...")) for line in raw):
        _refuse("document markers")
    # (indent, text, source line index) for every line that carries content.
    entries: list[tuple[int, str, int]] = []
    for n, line in enumerate(raw):
        body = _strip_comment(line)
        if body.strip():
            entries.append((len(body) - len(body.lstrip(" ")), body.strip(), n))
    pos = 0

    def advance_past(line_no: int) -> None:
        nonlocal pos
        while pos < len(entries) and entries[pos][2] <= line_no:
            pos += 1

    def block(indent: int) -> Any:
        nonlocal pos
        if entries[pos][1] == "-" or entries[pos][1].startswith("- "):
            return sequence(indent)
        return mapping(indent)

    def value_after(indent: int, rest: str, line_no: int) -> Any:
        """The value that follows `key:` (or `-`) whose content is `rest`."""
        nonlocal pos
        if rest[:1] in ("|", ">"):
            if not re.fullmatch(r"[|>][+-]?", rest):
                _refuse(f"unsupported block scalar header: {rest!r}")
            body: list[str] = []
            last = line_no
            for n in range(line_no + 1, len(raw)):
                line = raw[n]
                if line.strip() and len(line) - len(line.lstrip(" ")) <= indent:
                    break
                body.append(line)
                if line.strip():
                    last = n
            body = body[: last - line_no]
            width = min((len(b) - len(b.lstrip(" ")) for b in body if b.strip()), default=0)
            lines = [b[width:] for b in body]
            advance_past(last)
            joined = "\n".join(lines) if rest[0] == "|" else " ".join(x for x in lines if x)
            return joined + "\n" if rest[1:] != "-" else joined
        if rest:
            value = _scalar(rest)
            if pos < len(entries) and entries[pos][0] > indent:
                _refuse(f"continuation line after a scalar: {entries[pos][1]!r}")
            return value
        if pos < len(entries) and entries[pos][0] > indent:
            return block(entries[pos][0])
        if pos < len(entries) and entries[pos][0] == indent and entries[pos][1].startswith("- "):
            return sequence(indent)
        return None

    def mapping(indent: int) -> dict[str, Any]:
        nonlocal pos
        out: dict[str, Any] = {}
        while pos < len(entries) and entries[pos][0] == indent:
            _, body, line_no = entries[pos]
            if body == "-" or body.startswith("- "):
                break
            key, rest = _split_key(body)
            if key in out:
                _refuse(f"duplicate key {key!r}")
            pos += 1
            out[key] = value_after(indent, rest, line_no)
        if pos < len(entries) and entries[pos][0] > indent:
            _refuse(f"unexpected indentation at {entries[pos][1]!r}")
        return out

    def sequence(indent: int) -> list[Any]:
        nonlocal pos
        out: list[Any] = []
        while pos < len(entries) and entries[pos][0] == indent:
            _, body, line_no = entries[pos]
            if not (body == "-" or body.startswith("- ")):
                break
            rest = body[1:].lstrip(" ")
            if not rest:
                pos += 1
                out.append(value_after(indent, "", line_no))
                continue
            offset = len(body) - len(rest)
            looks_like_key = re.match(r"^[A-Za-z0-9_][A-Za-z0-9_.\-/]*:(?: |$)", rest)
            if looks_like_key and rest[:1] not in ("|", ">"):
                # `- key: value` opens a mapping whose keys sit at indent + offset.
                entries[pos] = (indent + offset, rest, line_no)
                out.append(mapping(indent + offset))
            else:
                pos += 1
                out.append(value_after(indent, rest, line_no))
        return out

    if not entries:
        _refuse("empty document")
    result = block(entries[0][0])
    if pos != len(entries):
        _refuse(f"unparsed content at {entries[pos][1]!r}")
    return result


# --- what CI is allowed to run ---------------------------------------------------------

ALLOWED_ACTIONS = {"actions/checkout", "astral-sh/setup-uv", "actions/setup-node"}
# job -> (job keys allowed, regexes the `run` steps must fullmatch, in order)
JOB_POLICY: dict[str, tuple[set[str], list[str]]] = {
    "check": ({"runs-on", "steps"}, [r"\./check\.sh"]),
    "mutation": (
        {"if", "runs-on", "timeout-minutes", "steps"},
        [r"uv sync --frozen", r'\./ci-mutate\.sh "\$\{\{ [^{}"]+ \}\}"'],
    ),
}
MUTATION_JOB_CONDITION = "github.event_name == 'workflow_dispatch'"


def _find_key(node: Any, key: str) -> bool:
    if isinstance(node, dict):
        return key in node or any(_find_key(v, key) for v in node.values())
    if isinstance(node, list):
        return any(_find_key(v, key) for v in node)
    return False


def ci_violations(workflow: Any, root: Path) -> list[str]:
    """Every way `workflow` lets CI run something other than the local gate."""
    if not isinstance(workflow, dict):
        return ["the workflow is not a mapping"]
    found: list[str] = []
    if _find_key(workflow, "continue-on-error"):
        found.append("continue-on-error is set: a failing step would not fail the job")
    extra = set(workflow) - {"name", "on", "jobs"}
    if extra:
        found.append(f"unexpected workflow keys {sorted(extra)}")
    jobs = workflow.get("jobs")
    if not isinstance(jobs, dict):
        return [*found, "the workflow has no jobs mapping"]
    if set(jobs) != set(JOB_POLICY):
        found.append(f"jobs are {sorted(jobs)}, expected {sorted(JOB_POLICY)}")
    for name, (allowed_keys, run_patterns) in JOB_POLICY.items():
        job = jobs.get(name)
        if not isinstance(job, dict):
            found.append(f"job {name} is missing")
            continue
        if set(job) - allowed_keys:
            found.append(f"job {name} has unexpected keys {sorted(set(job) - allowed_keys)}")
        if name == "mutation" and job.get("if") != MUTATION_JOB_CONDITION:
            found.append("job mutation is not limited to workflow_dispatch")
        steps = job.get("steps")
        if not isinstance(steps, list) or not steps:
            found.append(f"job {name} has no steps")
            continue
        runs: list[str] = []
        seen_run = False
        used: list[str] = []
        for step in steps:
            if not isinstance(step, dict):
                found.append(f"job {name} has a step that is not a mapping")
                continue
            if "run" in step:
                seen_run = True
                if set(step) - {"run", "name"}:
                    found.append(
                        f"job {name} run step has extra keys {sorted(set(step) - {'run', 'name'})}"
                    )
                runs.append(str(step["run"]).strip())
            elif "uses" in step:
                action = str(step["uses"]).split("@")[0]
                if "@" not in str(step["uses"]) or action not in ALLOWED_ACTIONS:
                    found.append(f"job {name} uses {step['uses']!r}, which is not allowed")
                if seen_run:
                    found.append(f"job {name} sets up {action} after a run step")
                if set(step) - {"uses", "with", "name"}:
                    found.append(f"job {name} step {action} has extra keys")
                used.append(action)
                if action == "actions/setup-node":
                    found.extend(_node_setup_violations(step.get("with"), root))
            else:
                found.append(f"job {name} has a step that neither runs nor uses anything")
        for needed in ("actions/checkout", "astral-sh/setup-uv"):
            if needed not in used:
                found.append(f"job {name} does not set up {needed}")
        if name == "check" and "actions/setup-node" not in used:
            found.append("job check does not set up Node, which check.sh needs")
        if len(runs) != len(run_patterns) or not all(
            re.fullmatch(p, r) for p, r in zip(run_patterns, runs, strict=False)
        ):
            found.append(f"job {name} runs {runs}, expected steps matching {run_patterns}")
        elif not (isinstance(steps[-1], dict) and "run" in steps[-1]):
            found.append(f"job {name} does not end with its run step")
    return found


def _node_setup_violations(config: Any, root: Path) -> list[str]:
    if not isinstance(config, dict):
        return ["setup-node has no `with` mapping"]
    found: list[str] = []
    version_file = config.get("node-version-file")
    if not isinstance(version_file, str) or not (root / version_file).is_file():
        found.append(f"setup-node node-version-file {version_file!r} is not a file")
    elif not re.fullmatch(r"\d+\n?", (root / version_file).read_text()):
        found.append(f"{version_file} is not a bare Node major version")
    if config.get("cache") != "npm":
        found.append("setup-node does not cache npm")
    lockfile = config.get("cache-dependency-path")
    if not isinstance(lockfile, str) or not (root / lockfile).is_file():
        found.append(f"setup-node cache-dependency-path {lockfile!r} is not a file")
    elif Path(lockfile).name != "package-lock.json":
        found.append("setup-node caches on something other than package-lock.json")
    if set(config) - {"node-version-file", "cache", "cache-dependency-path"}:
        found.append("setup-node has options this check does not know")
    return found


def _real_workflow_text() -> str:
    """The tracked workflow, read when a test runs and never at import: mutmut's
    work copy carries no `.github/`, and a read at module level there fails
    collection for every mutant run (`tests/test_mutmut_layout.py`)."""
    return WORKFLOW.read_text()


def _replace(old: str, new: str) -> Callable[[str], str]:
    def apply(text: str) -> str:
        assert old in text, f"mutation target missing: {old!r}"
        return text.replace(old, new, 1)

    return apply


# --- the reader ------------------------------------------------------------------------


def test_reader_parses_the_shapes_a_workflow_uses() -> None:
    text = (
        "name: t  # trailing comment\n"
        "on:\n"
        "  push:\n"
        "    branches: [main, 'x y']\n"
        "jobs:\n"
        "  a:\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "        with:\n"
        "          fetch-depth: 0\n"
        '      - run: ./ci-mutate.sh "${{ a || b }}"  # why\n'
        "      - run: |\n"
        "          echo one\n"
        "\n"
        "          echo '# not a comment'\n"
        "      - run: echo done\n"
        "    other:\n"
        "      - x\n"
        "      -\n"
        "        y: 1\n"
    )
    assert read_yaml(text) == {
        "name": "t",
        "on": {"push": {"branches": ["main", "x y"]}},
        "jobs": {
            "a": {
                "steps": [
                    {"uses": "actions/checkout@v4", "with": {"fetch-depth": 0}},
                    {"run": './ci-mutate.sh "${{ a || b }}"'},
                    {"run": "echo one\n\necho '# not a comment'\n"},
                    {"run": "echo done"},
                ],
                "other": ["x", {"y": 1}],
            }
        },
    }


@pytest.mark.parametrize(
    "text",
    [
        "a: &anchor 1\n",
        "a: {b: 1}\n",
        "a: !!str x\n",
        "a: 1\n\tb: 2\n",
        "a: 1\n  continued\n",
        "a: 1\na: 2\n",
        "---\na: 1\n",
        "a: [1,\n  2]\n",
        "a: 'open\n",
        'a: "open\n',
        "a: |x\n  y\n",
        "- a\nb: 1\n",
        "",
    ],
)
def test_reader_refuses_what_it_does_not_understand(text: str) -> None:
    with pytest.raises(UnsupportedYamlError):
        read_yaml(text)


# --- B19: CI runs the gate and nothing else --------------------------------------------


def test_ci_runs_exactly_the_local_gate() -> None:
    workflow = read_yaml(_real_workflow_text())
    assert ci_violations(workflow, ROOT) == []
    # The reader saw the steps: the two commands CI runs are the ones the policy names.
    runs = {
        name: [s["run"] for s in job["steps"] if "run" in s]
        for name, job in workflow["jobs"].items()
    }
    assert runs["check"] == ["./check.sh"]
    assert runs["mutation"][0] == "uv sync --frozen"
    assert runs["mutation"][1].startswith("./ci-mutate.sh ")
    assert (ROOT / "ci-mutate.sh").is_file()


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (_replace("      - run: ./check.sh\n", "      - run: pytest\n"), "job check runs"),
        (
            _replace("      - run: ./check.sh\n", "      - run: ./check.sh\n      - run: pytest\n"),
            "job check runs",
        ),
        (_replace("./check.sh\n", "./check.sh || true\n"), "job check runs"),
        (_replace("./check.sh\n", "./check.sh; true\n"), "job check runs"),
        (
            _replace(
                "      - run: ./check.sh\n", "      - run: ./check.sh\n        shell: bash {0}\n"
            ),
            "extra keys",
        ),
        (
            _replace(
                "      - run: ./check.sh\n",
                "      - run: ./check.sh\n        continue-on-error: true\n",
            ),
            "continue-on-error",
        ),
        (
            _replace("  check:\n    runs-on", "  check:\n    continue-on-error: true\n    runs-on"),
            "continue-on-error",
        ),
        (
            _replace("      - run: ./check.sh\n", "      - if: false\n        run: ./check.sh\n"),
            "extra keys",
        ),
        (
            _replace("  check:\n    runs-on", "  check:\n    if: false\n    runs-on"),
            "job check has unexpected keys",
        ),
        (
            _replace(
                "      - run: ./check.sh\n", "      - run: ./check.sh\n      - run: echo ok\n"
            ),
            "job check runs",
        ),
        (
            _replace(
                "      - uses: actions/checkout@v4\n      - uses: astral", "      - uses: astral"
            ),
            "does not set up actions/checkout",
        ),
        (
            _replace("      - uses: actions/setup-node@v4\n", "      - uses: someone/else@v1\n"),
            "not allowed",
        ),
        (_replace("          cache: npm\n", ""), "does not cache npm"),
        (
            _replace("node-version-file: .node-version", "node-version-file: .missing-version"),
            "is not a file",
        ),
        (
            _replace("      - run: uv sync --frozen\n", ""),
            "job mutation runs",
        ),
        (
            _replace("      - run: uv sync --frozen\n", "      - run: uv sync\n"),
            "job mutation runs",
        ),
        (
            _replace('"${{ github.event.pull_request.base.sha || github.event.before }}"', ""),
            "job mutation runs",
        ),
        (
            _replace('github.event.before }}"', 'github.event.before }}" || true'),
            "job mutation runs",
        ),
        (
            _replace("    if: github.event_name == 'workflow_dispatch'\n", ""),
            "not limited to workflow_dispatch",
        ),
        (
            _replace(
                "jobs:\n",
                "jobs:\n  extra:\n    runs-on: ubuntu-latest\n    steps:\n      - run: pytest\n",
            ),
            "jobs are",
        ),
        (_replace("jobs:\n", "env:\n  A: b\njobs:\n"), "unexpected workflow keys"),
    ],
)
def test_ci_that_could_diverge_from_the_local_gate_is_refused(
    mutate: Callable[[str], str], expected: str
) -> None:
    found = ci_violations(read_yaml(mutate(_real_workflow_text())), ROOT)
    assert any(expected in f for f in found), found


def test_a_node_version_that_is_not_a_bare_major_is_refused(tmp_path: Path) -> None:
    (tmp_path / "package-lock.json").write_text("{}")
    config = {
        "node-version-file": ".node-version",
        "cache": "npm",
        "cache-dependency-path": "package-lock.json",
    }
    (tmp_path / ".node-version").write_text("26\n")
    assert _node_setup_violations(config, tmp_path) == []
    (tmp_path / ".node-version").write_text("v26.7.0\n")
    assert _node_setup_violations(config, tmp_path) == [
        ".node-version is not a bare Node major version"
    ]


# --- B18: actionlint over the workflows ------------------------------------------------


def _check_sh_commands(prefix: str) -> list[list[str]]:
    """The tokenised `check.sh` lines that start with `prefix` (comments skipped)."""
    lines = [ln.strip() for ln in CHECK_SH.read_text().splitlines()]
    return [shlex.split(ln) for ln in lines if ln.startswith(prefix) and not ln.startswith("#")]


def _actionlint(files: list[Path], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run actionlint with check.sh's own arguments, the glob replaced by `files`."""
    commands = _check_sh_commands("uv run actionlint")
    assert len(commands) == 1, f"check.sh must run actionlint exactly once: {commands}"
    flags = [a for a in commands[0][3:] if "*" not in a]
    env = {**os.environ, "PATH": f"{VENV_BIN}{os.pathsep}{os.environ['PATH']}"}
    return subprocess.run(
        [str(VENV_BIN / "actionlint"), *flags, *map(str, files)],
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
        check=False,
    )


def _workflow(tmp_path: Path, runs_on: str, steps: str) -> Path:
    path = tmp_path / "w.yml"
    path.write_text(f"name: t\non: push\njobs:\n  a:\n    runs-on: {runs_on}\n    steps:\n{steps}")
    return path


def test_actionlint_is_green_on_every_workflow_in_the_repo() -> None:
    glob = [a for a in _check_sh_commands("uv run actionlint")[0] if "*" in a]
    assert glob, "check.sh must name the workflows by glob so a new workflow is covered"
    files = sorted(ROOT.glob(glob[0]))
    on_disk = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
    assert files == on_disk != []
    result = _actionlint(files, ROOT)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


def test_actionlint_is_green_on_a_good_workflow(tmp_path: Path) -> None:
    good = _workflow(tmp_path, "ubuntu-latest", "      - id: s\n        run: echo hi\n")
    assert _actionlint([good], tmp_path).returncode == 0


@pytest.mark.parametrize(
    ("runs_on", "steps", "expected"),
    [
        ("ubuntu-latest", "      - run: echo hi\n        bogus: 1\n", 'unexpected key "bogus"'),
        ("${{ foo.bar }}", "      - run: echo hi\n", 'undefined variable "foo"'),
        (
            "ubuntu-latest",
            "      - run: echo hi\n      - run: echo ${{ steps.nope.outputs.x }}\n",
            'property "nope" is not defined',
        ),
        ("ubuntu-latest", "      - run: echo $UNQUOTED\n", "SC2086"),
    ],
)
def test_actionlint_refuses_a_broken_workflow(
    tmp_path: Path, runs_on: str, steps: str, expected: str
) -> None:
    result = _actionlint([_workflow(tmp_path, runs_on, steps)], tmp_path)
    assert result.returncode != 0
    assert expected in result.stdout


# --- B20: the lockfile matches pyproject.toml ------------------------------------------


def _uv_lock_check(
    tmp_path: Path, extra_dependency: str | None
) -> subprocess.CompletedProcess[str]:
    """Run check.sh's `uv lock --check` (offline) in a copy of pyproject.toml and uv.lock."""
    commands = _check_sh_commands("uv lock")
    assert len(commands) == 1, f"check.sh must run `uv lock` exactly once: {commands}"
    assert commands[0][:2] == ["uv", "lock"]
    uv = shutil.which("uv")
    assert uv is not None, "uv is required to run check.sh"
    for name in ("pyproject.toml", "uv.lock", "README.md", "LICENSE"):
        shutil.copy(ROOT / name, tmp_path / name)
    if extra_dependency is not None:
        project = (tmp_path / "pyproject.toml").read_text()
        anchor = '    "ruff>=0.8",\n'
        assert anchor in project
        (tmp_path / "pyproject.toml").write_text(
            project.replace(anchor, anchor + f'    "{extra_dependency}",\n', 1)
        )
    before = (tmp_path / "uv.lock").read_bytes()
    result = subprocess.run(
        [uv, *commands[0][1:], "--offline"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=False,
    )
    # `--check` must never rewrite the lock: a plain `uv lock` would.
    assert (tmp_path / "uv.lock").read_bytes() == before
    return result


def test_uv_lock_check_is_green_on_the_real_lock(tmp_path: Path) -> None:
    assert _uv_lock_check(tmp_path, None).returncode == 0


def test_uv_lock_check_refuses_a_dependency_missing_from_the_lock(tmp_path: Path) -> None:
    # pyyaml is already locked (as libcst's dependency), so a plain `uv lock` would
    # resolve it from the lock and rewrite the file; only `--check` refuses.
    result = _uv_lock_check(tmp_path, "pyyaml>=6")
    assert result.returncode != 0
    assert "needs to be updated" in result.stderr


# --- B21: the Python pin satisfies requires-python -------------------------------------


def pin_satisfies(pin: str, requires_python: str) -> bool:
    text = pin.strip()
    if not re.fullmatch(r"\d+\.\d+(\.\d+)?", text):
        reason = f"not a plain Python version: {text!r}"
        raise ValueError(reason)
    return SpecifierSet(requires_python).contains(Version(text))


def _requires_python() -> str:
    import tomllib

    with (ROOT / "pyproject.toml").open("rb") as handle:
        value = tomllib.load(handle)["project"]["requires-python"]
    assert isinstance(value, str)
    return value


def test_the_python_pin_satisfies_requires_python() -> None:
    assert pin_satisfies((ROOT / ".python-version").read_text(), _requires_python())


@pytest.mark.parametrize(
    ("pin", "requires", "expected"),
    [
        ("3.12\n", ">=3.12", True),
        ("3.13.1", ">=3.12", True),
        ("3.11\n", ">=3.12", False),
        ("3.12", ">=3.12,<3.13", True),
        ("3.13", ">=3.12,<3.13", False),
    ],
)
def test_pin_satisfies_known_instances(pin: str, requires: str, expected: bool) -> None:
    assert pin_satisfies(pin, requires) is expected


@pytest.mark.parametrize("pin", ["", "3", "cpython-3.12", "pypy@3.12", "3.12-dev"])
def test_a_pin_that_is_not_a_plain_version_is_refused(pin: str) -> None:
    with pytest.raises(ValueError, match="not a plain Python version"):
        pin_satisfies(pin, ">=3.12")


# --- B21: taplo ------------------------------------------------------------------------


def _taplo(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(VENV_BIN / "taplo"), *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        check=False,
    )


def _with_repo_config(tmp_path: Path, body: str) -> Path:
    """A temp directory holding the repo's taplo.toml and one TOML file."""
    shutil.copy(ROOT / "taplo.toml", tmp_path / "taplo.toml")
    target = tmp_path / "sample.toml"
    target.write_text(body)
    return target


def test_taplo_accepts_the_repo_style(tmp_path: Path) -> None:
    target = _with_repo_config(
        tmp_path,
        '[tool.x]\nitems = [\n    "a",\n    "b",\n]\nname = "n"\nz = 1\nb = 2\n',
    )
    result = _taplo(["fmt", "--check", target.name], tmp_path)
    assert result.returncode == 0, result.stderr


def test_taplo_refuses_a_misindented_array(tmp_path: Path) -> None:
    target = _with_repo_config(tmp_path, '[tool.x]\nitems = [\n  "a",\n        "b",\n]\n')
    result = _taplo(["fmt", "--check", target.name], tmp_path)
    assert result.returncode != 0
    assert "not properly formatted" in result.stderr


def test_taplo_keeps_keys_in_the_order_written(tmp_path: Path) -> None:
    # `reorder_keys` would sort `z` after `a`; the config must leave the order alone.
    target = _with_repo_config(tmp_path, "[t]\nz = 1\na = 2\n")
    assert _taplo(["fmt", "--check", target.name], tmp_path).returncode == 0


def test_taplo_leaves_uv_lock_alone(tmp_path: Path) -> None:
    # taplo's default include (`**/*.toml`) never matches uv.lock, so the exclusion
    # only matters once an include is widened. Widen it here: with the repo's
    # exclusion the machine-written lock is skipped, and the same text under a name
    # that is not excluded is refused.
    config = (ROOT / "taplo.toml").read_text()
    (tmp_path / "taplo.toml").write_text('include = ["uv.lock", "other.toml"]\n' + config)
    layout = '[[package]]\nname = "a"\ndependencies = [\n  { name = "b" },\n]\n'
    (tmp_path / "uv.lock").write_text(layout)
    (tmp_path / "other.toml").write_text(layout)
    assert _taplo(["fmt", "--check", "uv.lock"], tmp_path).returncode == 0
    assert _taplo(["fmt", "--check", "other.toml"], tmp_path).returncode != 0
    without = config.replace('"uv.lock", ', "")
    assert without != config
    (tmp_path / "taplo.toml").write_text('include = ["uv.lock", "other.toml"]\n' + without)
    assert _taplo(["fmt", "--check", "uv.lock"], tmp_path).returncode != 0
