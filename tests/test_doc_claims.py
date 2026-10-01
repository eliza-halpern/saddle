"""What README.md, CONTRIBUTING.md and CLAUDE.md say about the repository.

These read top-level files that the mutation work copy does not hold, so they
live apart from test_docs.py, which must pass there whole.
"""

from __future__ import annotations

import argparse
import ast
import re
import shlex
from collections.abc import Collection
from pathlib import Path

import pytest
from doc_links import fenced_code_lines, inline_code_spans
from doc_tree import ROOT, require_checkout, tracked

from saddle import gates

# ---------------------------------------------------------------------------
# What README.md, CONTRIBUTING.md and CLAUDE.md say about the repository.
#
# Contract: a path, `saddle` subcommand, `--flag` or Python name that these
# three files put in code formatting exists in the tree they describe, and the
# two behavioural claims CONTRIBUTING.md makes about the code (an `impl` node
# may not edit tests; `gates` imports `evidence` only for type checking) hold.
# A doc that names something the code no longer has is wrong in a way no
# reader will notice until they copy the command.
# ---------------------------------------------------------------------------

CHECKED_DOCS = ("README.md", "CONTRIBUTING.md", "CLAUDE.md")
# Docs whose Python names are checked against src/saddle. README.md also names
# modules, but as module files (checked as paths); CONTRIBUTING.md is the one
# that cites the symbols its rules turn on.
SYMBOL_DOCS = ("CONTRIBUTING.md",)

# A code-formatted word is a repo path when it holds a `/` or ends in one of
# these, and is built only from the characters below (so a `~/` home path, a
# `$VAR`, a `<placeholder>`, a glob or a `key=value` is never one).
PATH_EXTENSIONS = (
    *(".py", ".md", ".sh", ".toml", ".cff", ".json", ".jsonc", ".js", ".mjs"),
    *(".svg", ".png", ".lock", ".yml", ".yaml", ".html", ".css"),
)
PATH_CHARS = re.compile(r"[\w./{},+-]+")
# Directories that exist in the project saddle works on, or in a reader's own
# checkout after setup, not in this repository's tracked files.
RUNTIME_DIRS = (".saddle/", ".venv/", "venv/")

# `--flag` spans that belong to another tool, with the reason. A bare flag in
# code formatting is otherwise taken to be a saddle flag.
FOREIGN_FLAGS = {
    "--no-cov": "pytest-cov's, named in CONTRIBUTING.md beside pytest",
    "--no-index": "pip's, in the README's account of the wheel-folder install",
    "--find-links": "pip's, in the README's account of the wheel-folder install",
    "--only-binary": "pip's, in the README's account of the wheel-folder install",
}

# Names CONTRIBUTING.md puts in code formatting that look like saddle names but
# are not defined in src/saddle, each with the reason.
FOREIGN_SYMBOLS = {
    "PATH": "the OS search path variable",
    "TYPE_CHECKING": "typing.TYPE_CHECKING, the constant gates.py imports",
    "FileNotFoundError": "a Python builtin",
}

SADDLE_SRC = ROOT / "src" / "saddle"


def _shell_words(unit: str) -> list[str]:
    try:
        return shlex.split(unit)
    except ValueError:
        return unit.split()


def code_units(text: str) -> list[str]:
    """Every inline code span and every line of a fenced block, in the doc."""
    return [*inline_code_spans(text), *fenced_code_lines(text)]


def path_candidates(unit: str) -> list[str]:
    """The repo paths a code unit names, with `{a,b}` alternatives expanded."""
    found: list[str] = []
    for word in _shell_words(unit):
        word = word.rstrip(".,;:)").lstrip("(")
        if (
            not PATH_CHARS.fullmatch(word)
            or word.startswith("/")
            or not re.search("[A-Za-z]", word)
        ):
            continue
        word = word.removeprefix("./")
        if word.startswith(RUNTIME_DIRS) or not ("/" in word or word.endswith(PATH_EXTENSIONS)):
            continue
        found.extend(_expand_braces(word))
    return found


def _expand_braces(word: str) -> list[str]:
    match = re.search(r"\{([^{}]*)\}", word)
    if match is None:
        return [word]
    return [
        expanded
        for choice in match.group(1).split(",")
        for expanded in _expand_braces(word[: match.start()] + choice + word[match.end() :])
    ]


def missing_paths(text: str, tracked_files: Collection[str]) -> list[str]:
    """Named paths that are neither a tracked file nor a directory holding one.

    A name with no directory part (`gates.py`) is a module of the package, so
    it is looked for under src/saddle at any depth; a path that is not found
    from the repository root is also tried under src/saddle (`web/app.py`).
    """
    files = set(tracked_files)
    missing: list[str] = []
    for unit in code_units(text):
        for path in path_candidates(unit):
            prefix = path.rstrip("/") + "/"
            roots = ("", "src/saddle/")
            found = any(
                (root + path.rstrip("/")) in files
                or any(f.startswith(root + prefix) for f in files)
                for root in roots
            )
            if "/" not in path:
                found = found or any(
                    f.startswith("src/saddle/") and f.rpartition("/")[2] == path for f in files
                )
            if not found:
                missing.append(path)
    return sorted(set(missing))


def command_tree(parser: argparse.ArgumentParser) -> dict[tuple[str, ...], set[str]]:
    """Every command path of an argparse parser mapped to the options it takes."""
    tree: dict[tuple[str, ...], set[str]] = {}

    def walk(path: tuple[str, ...], node: argparse.ArgumentParser) -> None:
        tree[path] = {opt for action in node._actions for opt in action.option_strings}
        for action in node._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, child in action.choices.items():
                    walk((*path, name), child)

    walk((), parser)
    return tree


FLAG_WORD = re.compile(r"(-[A-Za-z]|--[A-Za-z][\w-]*)(?:=\S*)?")
COMMAND_END = {"|", "||", "&&", ";", ">", ">>", "2>&1"}


def cli_findings(text: str, tree: dict[tuple[str, ...], set[str]]) -> tuple[list[str], int, int]:
    """Subcommands and flags the doc's code names that the parser lacks.

    Returns (problems, subcommands checked, flags checked). A `saddle ...`
    command in a span or fenced line is walked down the command tree; its
    flags must belong to the command it reaches. A span that is only a flag
    (`--wheel-dir DIR`) is a saddle flag unless FOREIGN_FLAGS says otherwise
    and must belong to some command. Flags of other tools are not looked at.
    """
    problems: list[str] = []
    commands = flags = 0
    every_flag = set().union(*tree.values())
    for unit in code_units(text):
        words = _shell_words(unit)
        if words and words[0].startswith("--"):
            for word in words:
                shown = FLAG_WORD.fullmatch(word.rstrip(".,;:)"))
                if shown and word.startswith("--") and shown.group(1) not in FOREIGN_FLAGS:
                    flags += 1
                    if shown.group(1) not in every_flag:
                        problems.append(f"`{unit}`: no saddle command has {shown.group(1)}")
            continue
        for index, word in enumerate(words):
            if word != "saddle":
                continue
            path: tuple[str, ...] = ()
            for later in words[index + 1 :]:
                if later in COMMAND_END or later.startswith("#"):
                    break
                shown = FLAG_WORD.fullmatch(later.rstrip(".,;:)"))
                if shown:
                    flags += 1
                    if shown.group(1) not in tree[path]:
                        problems.append(
                            f"`{unit}`: `saddle {' '.join(path)}` has no {shown.group(1)}"
                        )
                    continue
                children = {
                    p[len(path)] for p in tree if len(p) > len(path) and p[: len(path)] == path
                }
                if children and re.fullmatch(r"[a-z][\w-]*", later) and later in children:
                    path = (*path, later)
                    commands += 1
                elif children and re.fullmatch(r"[a-z][\w-]*", later) and not path:
                    problems.append(f"`{unit}`: `saddle {later}` is not a command")
                    break
    return sorted(set(problems)), commands, flags


def src_names(src: Path) -> set[str]:
    """Names defined or bound anywhere under `src`, plus SCREAMING_CASE string constants.

    A def, class, assignment target, attribute store, parameter or keyword
    counts; an ALL-CAPS string literal counts because that is how an
    environment variable is "defined" (`SADDLE_VLLM_API_KEY`).
    """
    names: set[str] = set()
    for path in src.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                names.add(node.name)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                names.add(node.id)
            elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
                names.add(node.attr)
            elif isinstance(node, ast.arg):
                names.add(node.arg)
            elif isinstance(node, ast.keyword) and node.arg:
                names.add(node.arg)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if re.fullmatch(r"[A-Z][A-Z0-9_]+", node.value):
                    names.add(node.value)
    return names


def symbol_candidates(unit: str) -> list[str]:
    """Saddle-looking Python names a code span is made of.

    A span that is one identifier (a call's argument list is dropped) with an
    underscore inside it, in SCREAMING_CASE, or in CamelCase. A single
    lowercase word (`gates`, `kind`, `total`) is also an ordinary word or a
    tool's name, so it is not claimed.
    """
    match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)(?:\(.*\))?", unit, re.DOTALL)
    if match is None:
        return []
    name = match.group(1)
    snake = "_" in name.strip("_")
    camel = re.fullmatch(r"[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*)+", name) is not None
    shouting = name.isupper() and len(name) > 1
    return [name] if snake or camel or shouting else []


def missing_symbols(text: str, defined: Collection[str]) -> list[str]:
    """Saddle-looking names the doc puts in inline code that nothing defines."""
    found = {
        name
        for span in inline_code_spans(text)
        for name in symbol_candidates(span)
        if name not in FOREIGN_SYMBOLS
    }
    return sorted(found - set(defined))


def runtime_imports(source: str, module: str) -> list[int]:
    """Lines of imports of `saddle.<module>` that run at import or call time.

    An import under `if TYPE_CHECKING:` does not run; one inside a function
    does, and so does a top-level one.
    """

    def names_module(node: ast.AST) -> bool:
        if isinstance(node, ast.Import):
            return any(a.name in {module, f"saddle.{module}"} for a in node.names)
        if isinstance(node, ast.ImportFrom):
            base = node.module or ""
            return (base in {module, f"saddle.{module}"}) or (
                base in {"", "saddle"} and any(a.name == module for a in node.names)
            )
        return False

    lines: list[int] = []

    def visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.If) and _is_type_checking(child.test):
                for other in child.orelse:
                    visit_one(other)
                continue
            visit_one(child)

    def visit_one(node: ast.AST) -> None:
        if names_module(node):
            lines.append(node.lineno)  # type: ignore[attr-defined]
        visit(node)

    visit(ast.parse(source))
    return sorted(lines)


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def module_constants(source: str) -> set[str]:
    """Names assigned at module level."""
    names: set[str] = set()
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _doc(name: str) -> str:
    require_checkout()
    return (ROOT / name).read_text(encoding="utf-8")


def test_every_path_the_docs_name_exists() -> None:
    files = tracked()
    found = {name: missing_paths(_doc(name), files) for name in CHECKED_DOCS}
    assert found == {name: [] for name in CHECKED_DOCS}


def test_the_path_check_finds_the_paths_in_the_real_docs() -> None:
    # A checker that extracted nothing would pass everything: the docs name
    # these, so these must be among what it looks at.
    seen = {
        path for name in CHECKED_DOCS for u in code_units(_doc(name)) for path in path_candidates(u)
    }
    assert {"tools/githooks", "check.sh", "docs/ARCHITECTURE.md", "src/saddle/gates.py"} <= seen
    assert {"src/saddle/dag.py", "src/saddle/cli.py", "gates.py", "web/branch_actions.py"} <= seen


def test_the_path_check_accepts_what_exists_and_skips_what_is_not_a_repo_path() -> None:
    files = [
        "check.sh",
        "docs/a.md",
        "src/saddle/gates.py",
        "src/saddle/web/app.py",
        "tools/githooks/pre-push",
    ]
    text = (
        "Run `./check.sh`, see `docs/a.md`, `tools/githooks`, `gates.py`, `web/app.py`, "
        "a nested module `app.py` and "
        "`src/saddle/{gates,web/app}.py`.\n\n"
        "```bash\ngit config core.hooksPath tools/githooks\n```\n"
        "Not paths: `~/.config/saddle/env`, `/etc/hosts`, `$HOME/x.py`, `.venv/bin/python`, "
        "`.saddle/runs/<id>/x.json`, `tests/test_<name>.py`, `key=docs/none.md`, "
        "`.pyc`, `e.g.`, `...`, `coverage`.\n"
    )
    assert missing_paths(text, files) == []


@pytest.mark.parametrize(
    ("text", "missing"),
    [
        ("see `docs/GONE.md`", ["docs/GONE.md"]),
        ("see `GONE.md`", ["GONE.md"]),
        ("see `gone.py`", ["gone.py"]),
        ("```bash\ngit config core.hooksPath tools/nothing\n```", ["tools/nothing"]),
        ("`src/saddle/{gates,gone}.py`", ["src/saddle/gone.py"]),
        ("`./nowhere.sh`", ["nowhere.sh"]),
        # A bare module name is looked for in the package only.
        ("`other.py`", ["other.py"]),
        ("`tools/` and `src/saddle/web/`", ["tools"]),
    ],
)
def test_the_path_check_names_a_missing_path(text: str, missing: list[str]) -> None:
    files = ["src/saddle/gates.py", "src/saddle/web/app.py", "docs/a.md", "docs/other.py"]
    found = missing_paths(text, files)
    assert [m.rstrip("/") for m in found] == missing


def _real_tree() -> dict[tuple[str, ...], set[str]]:
    require_checkout()
    from saddle.cli import build_parser

    return command_tree(build_parser())


def test_every_saddle_command_and_flag_the_docs_name_is_in_the_parser() -> None:
    tree = _real_tree()
    problems: list[str] = []
    commands = flags = 0
    for name in CHECKED_DOCS:
        found, c, f = cli_findings(_doc(name), tree)
        problems += [f"{name}: {p}" for p in found]
        commands += c
        flags += f
    assert problems == []
    # The docs do name commands and flags: checking none would prove nothing.
    assert commands >= 1
    assert flags >= 1


def test_the_real_command_tree_has_the_commands_the_readme_runs() -> None:
    tree = _real_tree()
    for command in ("doctor", "chat", "web", "audit", "auto", "verify", "up"):
        assert (command,) in tree
    assert "--anchor" in tree[("verify",)]
    assert ("requirements", "census") in tree


def _toy_tree() -> dict[tuple[str, ...], set[str]]:
    parser = argparse.ArgumentParser(prog="saddle")
    sub = parser.add_subparsers(dest="command")
    run = sub.add_parser("run", aliases=["go"])
    run.add_argument("--fast")
    nested = sub.add_parser("req").add_subparsers(dest="sub")
    nested.add_parser("census").add_argument("--limit")
    return command_tree(parser)


def test_command_tree_walks_subparsers_aliases_and_options() -> None:
    tree = _toy_tree()
    assert set(tree) == {(), ("run",), ("go",), ("req",), ("req", "census")}
    assert {"--fast", "-h", "--help"} <= tree[("run",)]
    assert "--limit" in tree[("req", "census")]


def test_cli_findings_accept_real_commands_and_flags_and_skip_other_tools() -> None:
    text = (
        "`saddle run --fast` and `saddle go`, `saddle req census --limit 3`, `--fast`, "
        "`saddle run --fast=1.`, `pytest --no-cov`, `--no-cov`, `uv run saddle run | head`.\n\n"
        "```bash\nsaddle run --fast   # --nonsense is in a comment\nsaddle req census\n```\n"
    )
    problems, commands, flags = cli_findings(text, _toy_tree())
    assert problems == []
    assert (commands, flags) == (9, 5)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("`saddle fly`", "`saddle fly` is not a command"),
        ("`saddle run --slow`", "`saddle run` has no --slow"),
        ("`saddle req census --fast`", "`saddle req census` has no --fast"),
        ("`--slow`", "no saddle command has --slow"),
        ("```bash\nsaddle run --slow\n```", "`saddle run` has no --slow"),
        ("`saddle --fast`", "`saddle ` has no --fast"),
    ],
)
def test_cli_findings_report_an_unknown_command_or_flag(text: str, expected: str) -> None:
    problems, _, _ = cli_findings(text, _toy_tree())
    assert any(expected in p for p in problems), problems


def test_every_saddle_name_contributing_cites_is_defined_in_the_package() -> None:
    require_checkout()
    defined = src_names(SADDLE_SRC)
    found = {name: missing_symbols(_doc(name), defined) for name in SYMBOL_DOCS}
    assert found == {name: [] for name in SYMBOL_DOCS}


def test_the_symbol_check_looks_at_the_symbols_the_docs_rely_on() -> None:
    spans = [s for u in inline_code_spans(_doc("CONTRIBUTING.md")) for s in symbol_candidates(u)]
    assert {
        "check_node_scope",
        "_check_property_oracle",
        "SHELL_TIMEOUT",
        "PYTEST_TESTS_FAILED",
        "DEFAULT_TEST_TIMEOUT_S",
        "ALLOWED_VALUES",
        "changed_lines",
        "git_diff",
    } <= set(spans)
    defined = src_names(SADDLE_SRC)
    assert {"check_node_scope", "SADDLE_VLLM_API_KEY", "git_diff"} <= defined


def test_symbol_candidates_pick_saddle_looking_names_only() -> None:
    assert symbol_candidates("check_node_scope") == ["check_node_scope"]
    assert symbol_candidates("_check_property_oracle") == ["_check_property_oracle"]
    assert symbol_candidates("git_diff(workdir, baseline)") == ["git_diff"]
    assert symbol_candidates("SHELL_TIMEOUT") == ["SHELL_TIMEOUT"]
    assert symbol_candidates("IsolationUnavailableError") == ["IsolationUnavailableError"]
    for plain in ("gates", "kind", "k", "pytest", "a b", "x=1", "dag.py", "re.match", "A", "M"):
        assert symbol_candidates(plain) == [], plain


def test_the_symbol_check_reports_a_stale_name_and_honours_the_exceptions(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text(
        "LIMIT_S = 1\n\ndef live_helper(arg_one, *, kw_two=1):\n    self.field_three = 1\n"
        "class LiveThing:\n    pass\nENV = 'SADDLE_SOME_KEY'\nok = dict(kw_four=1)\n"
    )
    defined = src_names(tmp_path)
    assert {"LIMIT_S", "live_helper", "arg_one", "kw_two", "field_three", "LiveThing"} <= defined
    assert {"SADDLE_SOME_KEY", "kw_four"} <= defined
    text = (
        "`LIMIT_S` `live_helper(1)` `arg_one` `field_three` `LiveThing` `SADDLE_SOME_KEY` "
        "`PATH` `TYPE_CHECKING` `FileNotFoundError` and the stale `renamed_helper` "
        "and `OLD_LIMIT_S` "
        "and `StaleThing`.\n"
    )
    assert missing_symbols(text, defined) == ["OLD_LIMIT_S", "StaleThing", "renamed_helper"]


def test_check_node_scope_forbids_an_implementation_node_from_editing_tests() -> None:
    # CONTRIBUTING.md: "`check_node_scope` forbids an implementation node from
    # editing tests", in any language (`gates.is_test_code`): a run once rewrote
    # a JavaScript test because only pytest modules counted as tests.
    for test_file in ("tests/test_x.py", "tests/markdown.test.js", "tests/fixtures/driver.mjs"):
        refused = gates.check_node_scope("impl", [test_file], may_create=True)
        assert refused.passed is False, test_file
        assert test_file in refused.detail
    allowed = gates.check_node_scope("impl", ["src/saddle/gates.py"], may_create=True)
    assert allowed.passed is True
    # The split is by kind: the node that is meant to write tests may.
    assert gates.check_node_scope("test", ["tests/test_x.py"], may_create=True).passed is True


def test_gates_imports_evidence_only_for_type_checking() -> None:
    source = (SADDLE_SRC / "gates.py").read_text(encoding="utf-8")
    assert runtime_imports(source, "evidence") == []


@pytest.mark.parametrize(
    ("source", "lines"),
    [
        ("from saddle import evidence\n", [1]),
        ("from saddle.evidence import git_diff\n", [1]),
        ("import saddle.evidence\n", [1]),
        ("from . import evidence\n", [1]),
        ("from .evidence import git_diff\n", [1]),
        ("def f():\n    from saddle.evidence import git_diff\n    return git_diff\n", [2]),
        ("if TYPE_CHECKING:\n    pass\nelse:\n    from saddle import evidence\n", [4]),
        ("try:\n    import saddle.evidence\nexcept ImportError:\n    pass\n", [2]),
    ],
)
def test_a_runtime_import_of_evidence_is_found(source: str, lines: list[int]) -> None:
    assert runtime_imports(source, "evidence") == lines


@pytest.mark.parametrize(
    "source",
    [
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from saddle.evidence import X\n",
        (
            "import typing\nif typing.TYPE_CHECKING:\n"
            "    from saddle import evidence\n    import saddle.evidence\n"
        ),
        "from saddle import dag\n",
        "import os\n",
    ],
)
def test_type_checking_imports_and_other_modules_are_not_runtime_imports(source: str) -> None:
    assert runtime_imports(source, "evidence") == []


def test_exit_codes_and_timeouts_live_where_contributing_says() -> None:
    gates_src = (SADDLE_SRC / "gates.py").read_text(encoding="utf-8")
    evidence_src = (SADDLE_SRC / "evidence.py").read_text(encoding="utf-8")
    assert {"PYTEST_TESTS_FAILED", "SHELL_TIMEOUT"} <= module_constants(gates_src)
    assert "DEFAULT_TEST_TIMEOUT_S" in module_constants(evidence_src)
    # ... and not the other way round: the claim distinguishes the two files.
    assert "DEFAULT_TEST_TIMEOUT_S" not in module_constants(gates_src)
    assert "SHELL_TIMEOUT" not in module_constants(evidence_src)


def test_claude_md_imports_a_file_that_exists() -> None:
    files = tracked()
    imports = re.findall(r"^@(\S+)$", _doc("CLAUDE.md"), re.MULTILINE)
    assert imports == ["CONTRIBUTING.md"]
    assert set(imports) <= set(files)
