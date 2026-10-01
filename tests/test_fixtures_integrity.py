"""Every file under tests/fixtures is well formed, and some test reads it.

Contract: each tracked fixture passes the integrity rule for its type (JSON and
JSONL parse, a diff behaves under `git apply --check` as its table row says, a
Jinja template loads and renders, text is UTF-8, `.py.txt` is Python), and each
is reached from at least one test; a new file type, a new diff, or an
unreferenced fixture fails until someone decides what it should do.

Why the rules are not "every diff applies": most diffs here are captured model
outputs, kept byte for byte because a gate or the diff reader is tested on
their defects (a miscounted hunk header, a fence around the diff, a hunk header
with no range). For those, "passes `git apply --check`" would be the wrong
contract. `DIFF_EXPECTATIONS` records, per diff, what git does with it and why,
so a fixture cannot drift from malformed to well formed (or back) unnoticed.

The tracked set comes from `git ls-files`, never a recursive glob, which skips
dot-directories. Inside the mutation work copy there is no repository of its
own, so the walk of the copied tree stands in; the same tracked-or-copied files
are what the work copy holds.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import jinja2
import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES_REL = "tests/fixtures"
THIS_MODULE = "tests/test_fixtures_integrity.py"


# -- the tracked set ----------------------------------------------------------


def _is_own_toplevel(root: Path) -> bool:
    """True when `root` is itself a git working tree (not a copy inside one)."""
    top = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    return top.returncode == 0 and Path(top.stdout.strip()).resolve() == root.resolve()


def tracked(root: Path, under: str) -> list[str]:
    """Repo-relative paths of the tracked files below `under`, sorted.

    A failing `git ls-files` raises: an empty list from a failed lookup would
    read as "no fixtures, nothing to check".
    """
    if _is_own_toplevel(root):
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--", under],
            capture_output=True,
            text=True,
            check=True,
        )
        return sorted(name for name in out.stdout.split("\0") if name)
    # No repository of its own (the mutation work copy, or a temp tree in a
    # test): walk the files that are there, skipping bytecode.
    return sorted(
        path.relative_to(root).as_posix()
        for path in (root / under).rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    )


def fixture_paths(root: Path = ROOT) -> list[str]:
    """Fixture paths relative to tests/fixtures."""
    return [name.removeprefix(FIXTURES_REL + "/") for name in tracked(root, FIXTURES_REL)]


FIXTURE_PATHS = fixture_paths()


def test_the_fixture_set_is_not_empty() -> None:
    """The parametrised checks below cover exactly this list; empty means they cover nothing."""
    assert len(FIXTURE_PATHS) > 20
    assert "qwen38_chat_template.jinja" in FIXTURE_PATHS
    assert "coverage_text/E-t5-s1/tree/money.py.txt" in FIXTURE_PATHS


# -- one rule per type --------------------------------------------------------

# Types with a rule here. `.mjs` and `.py` fixtures are covered by the
# JavaScript and Python linters, and run by the tests that use them.
HANDLED_SUFFIXES = {".json", ".jsonl", ".diff", ".txt", ".jinja", ".mjs", ".py"}


def json_problem(text: str) -> str | None:
    if not text.strip():
        return "empty"
    try:
        json.loads(text)
    except ValueError as exc:
        return f"not JSON: {exc}"
    return None


def jsonl_problem(text: str) -> str | None:
    """Every line is one JSON value; a blank line is a defect.

    A blank line is refused (the journal readers split on lines and a blank one
    is neither a record nor a terminator); a missing final newline is allowed.
    """
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not lines:
        return "empty"
    for number, line in enumerate(lines, 1):
        if not line.strip():
            return f"line {number} is blank"
        try:
            json.loads(line)
        except ValueError as exc:
            return f"line {number} is not JSON: {exc}"
    return None


def text_problem(data: bytes, *, python: bool) -> str | None:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        return f"not UTF-8: {exc}"
    if not text.strip():
        return "empty"
    if python:
        try:
            ast.parse(text)
        except SyntaxError as exc:
            return f"not Python: {exc}"
    return None


def _raise(message: str) -> None:
    raise ValueError(message)


def render_problem(source: str, messages: list[dict[str, object]]) -> str | None:
    """Load a chat template and render it with `messages`; None when it renders.

    The environment is the one the served template needs: `loopcontrols` and a
    `raise_exception` global that raises.
    """
    env = jinja2.Environment(extensions=["jinja2.ext.loopcontrols"])
    env.globals["raise_exception"] = _raise
    try:
        template = env.from_string(source)
        template.render(
            messages=messages,
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "f",
                        "description": "d",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            add_generation_prompt=True,
            reasoning_effort="low",
        )
    except (jinja2.TemplateError, ValueError) as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


CONVERSATION: list[dict[str, object]] = [
    {"role": "system", "content": "sys"},
    {"role": "user", "content": "hello"},
    {
        "role": "assistant",
        "content": "",
        "reasoning_content": "RSN",
        "tool_calls": [
            {"id": "1", "type": "function", "function": {"name": "f", "arguments": {"a": 1}}}
        ],
    },
    {"role": "tool", "content": "result"},
    {"role": "assistant", "content": "done", "reasoning_content": "R2"},
    {"role": "user", "content": "again"},
]


def test_json_checker_accepts_json_and_refuses_each_defect() -> None:
    assert json_problem('{"a": [1, 2]}') is None
    assert json_problem("[]") is None
    assert json_problem('{"a": ') is not None
    assert json_problem("{'a': 1}") is not None
    assert json_problem("  \n") == "empty"


def test_jsonl_checker_accepts_records_and_refuses_each_defect() -> None:
    assert jsonl_problem('{"a": 1}\n{"b": 2}\n') is None
    assert jsonl_problem('{"a": 1}\n{"b": 2}') is None  # no final newline
    assert jsonl_problem('{"a": 1}\n\n{"b": 2}\n') == "line 2 is blank"
    assert "line 2 is not JSON" in (jsonl_problem('{"a": 1}\n{"b": \n') or "")
    assert jsonl_problem("") == "empty"
    assert jsonl_problem("\n") == "line 1 is blank"


def test_text_checker_accepts_text_and_refuses_each_defect() -> None:
    assert text_problem(b"hello\n", python=False) is None
    assert text_problem(b"x = 1\n", python=True) is None
    assert "not UTF-8" in (text_problem(b"\xff\xfe\x00", python=False) or "")
    assert text_problem(b" \n", python=False) == "empty"
    assert "not Python" in (text_problem(b"def f(:\n", python=True) or "")
    # Prose is not Python: only `.py.txt` fixtures are parsed.
    assert text_problem(b"def f(:\n", python=False) is None


def test_the_template_checker_accepts_a_template_and_refuses_each_defect() -> None:
    good = "{%- for m in messages %}{{ m.role }}:{{ m.content }}\n{%- endfor %}"
    assert render_problem(good, CONVERSATION) is None
    assert "TemplateSyntaxError" in (render_problem("{% for m in messages %}", CONVERSATION) or "")
    # A template that refuses its input says so through `raise_exception`.
    refusing = "{%- if not messages %}{{ raise_exception('none') }}{%- endif %}"
    assert render_problem(refusing, []) == "ValueError: none"
    assert render_problem(refusing, CONVERSATION) is None
    assert "UndefinedError" in (render_problem("{{ messages.nope.deeper }}", CONVERSATION) or "")


@pytest.mark.parametrize("name", FIXTURE_PATHS)
def test_every_fixture_has_a_rule_for_its_type(name: str) -> None:
    suffix = PurePosixPath(name).suffix
    assert suffix in HANDLED_SUFFIXES, (
        f"tests/fixtures/{name}: no integrity rule for {suffix!r}; add one in "
        "test_fixtures_integrity (or an explicit 'not checked, because')"
    )


@pytest.mark.parametrize("name", [n for n in FIXTURE_PATHS if n.endswith(".json")])
def test_json_fixture_parses(name: str) -> None:
    assert json_problem((ROOT / FIXTURES_REL / name).read_text(encoding="utf-8")) is None


@pytest.mark.parametrize("name", [n for n in FIXTURE_PATHS if n.endswith(".jsonl")])
def test_jsonl_fixture_parses_line_by_line(name: str) -> None:
    assert jsonl_problem((ROOT / FIXTURES_REL / name).read_text(encoding="utf-8")) is None


@pytest.mark.parametrize("name", [n for n in FIXTURE_PATHS if n.endswith(".txt")])
def test_text_fixture_is_utf8_and_not_empty(name: str) -> None:
    data = (ROOT / FIXTURES_REL / name).read_bytes()
    assert text_problem(data, python=name.endswith(".py.txt")) is None


@pytest.mark.parametrize("name", [n for n in FIXTURE_PATHS if n.endswith(".jinja")])
def test_jinja_fixture_loads_and_renders_the_conversation_its_test_uses(name: str) -> None:
    """The served chat template, rendered as test_keep_reasoning renders it.

    The assertions read the output: kept reasoning reaches the assistant turn,
    a tool call is in the template's own format, and the prompt ends opened for
    the model, so a template that renders but drops any of these still fails.
    """
    source = (ROOT / FIXTURES_REL / name).read_text(encoding="utf-8")
    assert render_problem(source, CONVERSATION) is None
    assert render_problem(source, []) == "ValueError: No messages provided."
    env = jinja2.Environment(extensions=["jinja2.ext.loopcontrols"])
    env.globals["raise_exception"] = _raise
    out = env.from_string(source).render(
        messages=CONVERSATION, add_generation_prompt=True, reasoning_effort="low"
    )
    assert "<think>\nRSN\n</think>" in out
    assert "<function=f>" in out
    assert out.endswith("<|im_start|>assistant\n<think>\n")


# -- diffs --------------------------------------------------------------------


@dataclass(frozen=True)
class DiffExpect:
    """What `git apply --check` does with a fixture diff, and why.

    `strict`: exits 0 as written. `recount`: exits 0 with `--recount`, which
    ignores the hunk headers' line counts. `tree`: what it is checked against.
    `unwrapped`: for a diff wrapped in a fence, the same pair after the fence
    is removed (what the reader's unwrapping rule hands to git).
    """

    strict: bool
    recount: bool
    tree: str
    reason: str
    unwrapped: tuple[bool, bool] | None = None


# tree values:
#   "old side" -- the repository the diff was captured against is not in this
#       repo, so the tree is rebuilt from the diff's own `-` and context lines
#       (a new file has no old side). This proves the hunks agree with
#       themselves, not that they fit the original tree.
#   "tree/"    -- the captured post-change tree sits beside the diff, so the diff
#       is reverse-applied to it, a real-tree check.
DIFF_EXPECTATIONS: dict[str, DiffExpect] = {
    "coverage_text/E-t5-s1/changes.diff": DiffExpect(
        strict=True,
        recount=True,
        tree="tree/",
        reason="well formed; reverse-applies to the captured tree (checked by "
        "test_the_coverage_diff_reverse_applies_to_its_tree_and_not_forward)",
    ),
    "repair_deletes_public_round3d.diff": DiffExpect(
        strict=True,
        recount=True,
        tree="old side",
        reason="recovered from a run's blobs with exact counts; test_gates reads "
        "both sides of it as a repair that deleted the public API",
    ),
    "implementation_round3e.diff": DiffExpect(
        strict=False,
        recount=True,
        tree="old side",
        reason="a model's whole-file draw: the fees.py hunk header's counts disagree "
        "with its body, so git refuses it as written; test_gates reads the new side "
        "from the body and does not apply it",
    ),
    "degenerate_round3e.diff": DiffExpect(
        strict=False,
        recount=True,
        tree="old side",
        reason="the same draw plus a repeated dead block; the miscounted header is "
        "the model's own, kept verbatim for the dead-code gate",
    ),
    "whole_file_round3i.diff": DiffExpect(
        strict=False,
        recount=True,
        tree="old side",
        reason="a line-1 hunk whose header declares 113 new lines over a longer "
        "body (test_slice asserts the header text); only `--recount` applies it, "
        "which is why the reader reconstructs the file from the body instead",
    ),
    "fenced_draw_round3e.diff": DiffExpect(
        strict=False,
        recount=False,
        tree="old side",
        reason="two blank lines and a ```diff fence around the diff, no final "
        "newline: git refuses it as written even with `--recount`; test_slice "
        "checks the unwrapping that removes the packaging",
        unwrapped=(False, True),
    ),
    "rangeless_hunk_round3i.diff": DiffExpect(
        strict=False,
        recount=False,
        tree="old side",
        reason="hunk headers are a bare `@@` with no ranges: not a patch at all, "
        "and test_slice asserts the reader recovers nothing from it",
    ),
}

_SECTION = re.compile(r"(?m)^(?=diff --git )")
_PATH = re.compile(r"diff --git a/(\S+) b/")


def old_side_tree(diff: str) -> dict[str, str]:
    """Each file the diff changes as it stood before, from its `-` and context lines.

    A section whose old side is `/dev/null` (a new file) is left out.
    """
    tree: dict[str, str] = {}
    for section in _SECTION.split(diff):
        found = _PATH.match(section)
        if not found or "\n--- /dev/null" in section:
            continue
        lines = section.splitlines(keepends=True)
        hunk = next((i for i, line in enumerate(lines) if line.startswith("@@")), None)
        if hunk is None:
            continue
        tree[found.group(1)] = "".join(
            line[1:] for line in lines[hunk + 1 :] if line.startswith(("-", " "))
        )
    return tree


def apply_outcome(
    work: Path, diff: str, tree: Mapping[str, str], *, reverse: bool = False
) -> tuple[bool, bool]:
    """(strict, recount): whether `git apply --check` accepts `diff` in a copy of `tree`."""
    work.mkdir(parents=True, exist_ok=True)
    for name, text in tree.items():
        target = work / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    patch = work / "check.diff"
    patch.write_text(diff)
    results = []
    for extra in ([], ["--recount"]):
        args = ["git", "apply", "--check", *(["-R"] if reverse else []), *extra, patch.name]
        results.append(subprocess.run(args, cwd=work, capture_output=True).returncode == 0)
    return results[0], results[1]


def unwrap_fence(raw: str) -> str:
    """The diff inside a ```diff fence, as test_slice's unwrapping hands it to git."""
    return raw.strip().removeprefix("```diff\n").removesuffix("\n```") + "\n"


VALID = (
    "diff --git a/m.py b/m.py\n--- a/m.py\n+++ b/m.py\n@@ -1,2 +1,3 @@\n x = 1\n+y = 2\n z = 3\n"
)
MISCOUNTED = VALID.replace("@@ -1,2 +1,3 @@", "@@ -1,2 +1,9 @@")
RANGELESS = VALID.replace("@@ -1,2 +1,3 @@", "@@ ")


def test_the_diff_checker_tells_exact_miscounted_and_garbage_apart(tmp_path: Path) -> None:
    """Known-good, known-bad and the case between them, on the checker itself.

    The middle one is the shape most fixtures here have: git refuses the header
    as written and accepts it once the counts are recomputed.
    """
    assert apply_outcome(tmp_path / "a", VALID, old_side_tree(VALID)) == (True, True)
    assert apply_outcome(tmp_path / "b", MISCOUNTED, old_side_tree(MISCOUNTED)) == (False, True)
    assert apply_outcome(tmp_path / "c", RANGELESS, old_side_tree(RANGELESS)) == (False, False)
    assert apply_outcome(tmp_path / "d", "not a diff\n", {}) == (False, False)
    # Reverse mode: VALID's new side is the tree, and it reverse-applies.
    new_side = {"m.py": "x = 1\ny = 2\nz = 3\n"}
    assert apply_outcome(tmp_path / "e", VALID, new_side, reverse=True) == (True, True)
    assert apply_outcome(tmp_path / "f", VALID, new_side) == (False, False)


def test_the_old_side_tree_leaves_a_new_file_out() -> None:
    created = (
        "diff --git a/n.py b/n.py\nnew file mode 100644\n--- /dev/null\n+++ b/n.py\n"
        "@@ -0,0 +1,1 @@\n+x = 1\n"
    )
    assert old_side_tree(created + VALID) == {"m.py": "x = 1\nz = 3\n"}


DIFF_FIXTURES = [n for n in FIXTURE_PATHS if n.endswith(".diff")]


def test_every_diff_fixture_is_classified_and_every_row_is_a_diff_fixture() -> None:
    """A new diff fixture, or a renamed one, forces a decision here."""
    assert DIFF_FIXTURES, "no diff fixtures found: the parametrisation below would be empty"
    assert sorted(DIFF_EXPECTATIONS) == sorted(DIFF_FIXTURES)
    assert all(row.reason for row in DIFF_EXPECTATIONS.values())


@pytest.mark.parametrize("name", sorted(DIFF_EXPECTATIONS))
def test_a_diff_fixture_behaves_under_git_apply_as_its_row_says(name: str, tmp_path: Path) -> None:
    row = DIFF_EXPECTATIONS[name]
    path = ROOT / FIXTURES_REL / name
    diff = path.read_text(encoding="utf-8")
    if row.tree == "tree/":
        tree = {
            file.name.removesuffix(".txt"): file.read_text(encoding="utf-8")
            for file in (path.parent / "tree").iterdir()
        }
        got = apply_outcome(tmp_path / "w", diff, tree, reverse=True)
    else:
        assert row.tree == "old side"
        got = apply_outcome(tmp_path / "w", diff, old_side_tree(diff))
    assert got == (row.strict, row.recount), row.reason
    if row.unwrapped is not None:
        inner = unwrap_fence(diff)
        assert apply_outcome(tmp_path / "u", inner, old_side_tree(inner)) == row.unwrapped


def test_the_coverage_diff_reverse_applies_to_its_tree_and_not_forward(tmp_path: Path) -> None:
    """The one diff whose tree is in the repo: the discriminating pair."""
    base = ROOT / FIXTURES_REL / "coverage_text" / "E-t5-s1"
    diff = (base / "changes.diff").read_text(encoding="utf-8")
    tree = {f.name.removesuffix(".txt"): f.read_text() for f in (base / "tree").iterdir()}
    assert sorted(tree) == ["money.py", "store.py"]
    assert apply_outcome(tmp_path / "r", diff, tree, reverse=True) == (True, True)
    # As a forward patch it collides with the tree (money.py already exists).
    assert apply_outcome(tmp_path / "f", diff, tree) == (False, False)


# -- every fixture is read by some test ----------------------------------------

# Fixtures no test reads, each with why it is still here. Empty today.
UNREFERENCED_EXCEPTIONS: dict[str, str] = {}

REFERRER_SUFFIXES = (".py", ".js", ".mjs", ".cjs")
# A call that reads a whole directory: a fixture directory read this way is
# reached without any of its files being named.
WHOLESALE = re.compile(r"\.iterdir\(|\.glob\(|\.rglob\(|copytree\(|os\.walk\(|listdir\(")


def referrer_texts(root: Path = ROOT) -> dict[str, str]:
    """The test sources that may reference a fixture, by repo-relative path.

    Fixtures themselves are not referrers (a driver naming a sibling is not a
    test reading it) and neither is this module, whose tables name every diff.
    """
    texts: dict[str, str] = {}
    for name in tracked(root, "tests"):
        if name.startswith(FIXTURES_REL + "/") or name == THIS_MODULE:
            continue
        if name.endswith(REFERRER_SUFFIXES):
            texts[name] = (root / name).read_text(encoding="utf-8")
    return texts


def _names(text: str, name: str) -> bool:
    """`name` as a whole token: `proofs.jsonl` is not found inside `pre-basis-proofs.jsonl`."""
    return re.search(rf"(?<![\w.-]){re.escape(name)}(?![\w])", text) is not None


def is_referenced(fixture: str, texts: Mapping[str, str]) -> bool:
    """Whether some referrer reads `fixture` (path relative to tests/fixtures).

    A file directly in tests/fixtures must have its whole name in a referrer.
    A file in a subdirectory D needs a referrer that quotes `"D"` and also
    (a) names the file, or (b) quotes its stem (`"E-t8-s1"` for `E-t8-s1.json`:
    the file is built from a parametrised name), or (c) reads a directory
    wholesale (`iterdir`, `glob`, `copytree`, ...). Naming only the file
    (`proofs.jsonl`) is not enough there: that name appears all over the suite.
    """
    path = PurePosixPath(fixture)
    if len(path.parts) == 1:
        return any(_names(text, path.name) for text in texts.values())
    top = path.parts[0]
    stem = path.name.rsplit(".", 1)[0]
    for text in texts.values():
        if f'"{top}"' not in text:
            continue
        if _names(text, path.name) or f'"{stem}"' in text or WHOLESALE.search(text):
            return True
    return False


def unreferenced(fixtures: list[str], texts: Mapping[str, str]) -> list[str]:
    return [name for name in fixtures if not is_referenced(name, texts)]


def test_every_fixture_is_referenced_by_a_test() -> None:
    texts = referrer_texts()
    assert len(texts) > 50, "the referrer set is nearly empty: the check below would be vacuous"
    missing = [n for n in unreferenced(FIXTURE_PATHS, texts) if n not in UNREFERENCED_EXCEPTIONS]
    assert missing == [], (
        f"fixtures no test reads (delete, or use, or list with a reason): {missing}"
    )


def test_every_listed_exception_is_still_a_fixture_and_still_unreferenced() -> None:
    """An exception that stopped being true must go, or the list rots into a mute."""
    texts = referrer_texts()
    for name, reason in UNREFERENCED_EXCEPTIONS.items():
        assert reason
        assert name in FIXTURE_PATHS, f"{name}: no longer a fixture"
        assert not is_referenced(name, texts), f"{name}: a test reads it now; drop the exception"


def test_the_reference_rule_flags_an_unmentioned_fixture_and_accepts_a_mentioned_one() -> None:
    """Known-bad and known-good for the rule itself, so it cannot be vacuous."""
    texts = {
        "tests/test_a.py": (
            'FX = HERE / "fixtures"\n'
            'a = (FX / "used.json").read_text()\n'
            'b = FX / "dir_named" / "inside.json"\n'
            'd = FX / "dir_stem" / f"{name}.json"\nnames = ["picked"]\n'
        ),
        "tests/test_b.py": 'c = [p.name for p in (FX / "dir_read").iterdir()]\n',
    }
    assert is_referenced("used.json", texts)
    assert is_referenced("dir_named/inside.json", texts)
    assert is_referenced("dir_read/anything.txt", texts)  # directory read wholesale
    assert not is_referenced("ghost.json", texts)
    assert not is_referenced("dir_named/ghost.json", texts)  # directory named, file not
    assert not is_referenced("other_dir/inside.json", texts)  # file named, directory not
    assert not is_referenced("dir_stem/ghost.json", texts)  # no stem quoted
    assert is_referenced("dir_stem/picked.json", texts)  # stem quoted in the same referrer
    assert unreferenced(["used.json", "ghost.json"], texts) == ["ghost.json"]


def test_a_longer_name_does_not_stand_for_a_shorter_one() -> None:
    """`proofs.jsonl` must not be found inside `pre-basis-proofs.jsonl`."""
    texts = {"tests/test_a.py": 'p = FX / "pre-basis-proofs.jsonl"\nq = "x.proofs.jsonl.bak"\n'}
    assert not is_referenced("proofs.jsonl", texts)
    assert is_referenced("pre-basis-proofs.jsonl", texts)
    # In a subdirectory the bare name alone never counts without the directory.
    assert not is_referenced("auto_pre_chain/proofs.jsonl", {"t.py": '"proofs.jsonl"'})


def _write(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)


def test_referrers_exclude_fixtures_and_this_module(tmp_path: Path) -> None:
    """A fixture is not read by its sibling or by the table that lists it."""
    _write(tmp_path, "tests/test_real.py", "FX / 'real.json'\n")
    _write(tmp_path, "tests/fixtures/driver_cdp.mjs", "import 'ghost.json'\n")
    _write(tmp_path, "tests/fixtures/sub/helper.py", "'ghost.json'\n")
    _write(tmp_path, THIS_MODULE, "'ghost.json'\n")
    _write(tmp_path, "tests/notes.md", "ghost.json\n")
    _write(tmp_path, "tests/__pycache__/x.py", "ghost.json\n")
    assert sorted(referrer_texts(tmp_path)) == ["tests/test_real.py"]
    assert unreferenced(["ghost.json", "real.json"], referrer_texts(tmp_path)) == ["ghost.json"]


def test_the_tracked_listing_reads_git_and_fails_loudly_when_git_does(tmp_path: Path) -> None:
    """Inside a repository the listing is `git ls-files` (a dot-directory and an
    untracked file included or left out accordingly); a failing git is an error."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    _write(tmp_path, "tests/fixtures/.hidden/in.json", "{}\n")
    _write(tmp_path, "tests/fixtures/tracked.json", "{}\n")
    _write(tmp_path, "tests/fixtures/untracked.json", "{}\n")
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "add",
            "tests/fixtures/.hidden/in.json",
            "tests/fixtures/tracked.json",
        ],
        check=True,
    )
    assert fixture_paths(tmp_path) == [".hidden/in.json", "tracked.json"]
    # Break the repository: the lookup must raise, not return [].
    (tmp_path / ".git" / "index").write_bytes(b"corrupt")
    with pytest.raises(subprocess.CalledProcessError):
        fixture_paths(tmp_path)


def test_the_tracked_listing_walks_a_tree_that_is_not_a_repository(tmp_path: Path) -> None:
    _write(tmp_path, "tests/fixtures/a.json", "{}\n")
    _write(tmp_path, "tests/fixtures/sub/.b.json", "{}\n")
    _write(tmp_path, "tests/fixtures/__pycache__/c.pyc", "x")
    assert fixture_paths(tmp_path) == ["a.json", "sub/.b.json"]
