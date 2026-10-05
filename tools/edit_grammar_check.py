"""Validate EDIT_GRAMMAR against real file content. Run after ANY edit to it.

Same shape and same harness as `diff_grammar_check.py` -- the grammar is
enforced by xgrammar, not by this repo's venv, so the corpus travels to
wherever a real xgrammar lives:

    python tools/edit_grammar_check.py --emit > /tmp/cases.json
    uv run --no-project --with xgrammar python tools/edit_grammar_check.py --run

The cases file carries the grammar text itself, so `--run` compiles what
this checkout has and never imports `saddle`.

Why it exists, concretely. The first shape of this grammar spelled a
content line `"-" [^\\n]* "\\n"`, so `-\\n` was legal and `oline+` admitted
an unbounded run of them. Nothing in the repo could see that: the parser
accepts a blank content line (correctly -- files have blank lines), and
the offline acceptance corpus at the time asked only whether well-formed
blocks were admitted, never whether a legal prefix could go on forever.
A live draw against a two-line file emitted the header, one correct
search line, and then 11 900 characters of `-`. The MUST_NOT_STOP cases
below are the half that catches it: a runaway is a legal *prefix*, so
byte-acceptance alone cannot see it, and only a stop token can ask
"having emitted this, may the model be done?".
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

CASES_PATH = "/tmp/cases.json"
REPO = Path(__file__).resolve().parent.parent  # git runs here, whatever the cwd

_E = "edit n.py\n"

MUST_REJECT = {
    "prose": "Sure! I will fix that.\n",
    "markdown fence": "```\nedit n.py\n-a\n=======\n+b\n>>>>>>>\n```\n",
    "json wrapper": '{"edits": "edit n.py\\n"}',
    "no operation keyword": "n.py\n-a\n=======\n+b\n>>>>>>>\n",
    "unprefixed content line": _E + "-a\n=======\nb\n>>>>>>>\n",
    "missing separator": _E + "-a\n+b\n>>>>>>>\n",
    "empty search block": _E + "=======\n+b\n>>>>>>>\n",
    # `path` may not begin with `/`, so nothing outside the worktree is
    # even spellable. The applier refuses a traversal too; this is the
    # half that keeps the model from emitting one in the first place.
    "absolute path": "edit /etc/passwd\n-a\n=======\n+b\n>>>>>>>\n",
    # The runaway and its neighbours. Three consecutive blanks anywhere,
    # or a block that begins or ends on one, is the shape that had no
    # exit; two is ordinary (PEP 8 between top-level definitions).
    "three blank search lines": _E + "-a\n-\n-\n-\n-b\n=======\n+b\n>>>>>>>\n",
    "three blank replace lines": _E + "-a\n=======\n+b\n+\n+\n+\n+c\n>>>>>>>\n",
    "search block starts blank": _E + "-\n-a\n=======\n+b\n>>>>>>>\n",
    "search block ends blank": _E + "-a\n-\n=======\n+b\n>>>>>>>\n",
    "replace block starts blank": _E + "-a\n=======\n+\n+b\n>>>>>>>\n",
    "replace block ends blank": _E + "-a\n=======\n+b\n+\n>>>>>>>\n",
    # The live draw itself: one correct search line, then nothing else.
    # It belongs here and not in MUST_NOT_STOP -- a case the grammar
    # rejects outright passes a stop check vacuously.
    "the live runaway": _E + "-RATE = 0.05\n" + "-\n" * 8,
}

# Byte offset at which a MUST_REJECT case has to die. A case rejected at
# the wrong byte is being refused by the wrong rule.
REJECT_AT = {
    # The `b` where a `+`-prefixed line is required.
    "unprefixed content line": len(_E + "-a\n=======\n"),
    # The `+` where the separator's `=` is required: `obody` cannot end.
    "missing separator": len(_E + "-a\n"),
    # The `=` where a search line is required: `obody` needs one `ofull`.
    "empty search block": len(_E),
    "absolute path": len("edit "),
    # The third `-`'s newline: two blanks are legal, the third is not.
    "three blank search lines": len(_E + "-a\n-\n-\n-"),
    "three blank replace lines": len(_E + "-a\n=======\n+b\n+\n+\n+"),
    # The newline that would close a leading blank.
    "search block starts blank": len(_E + "-"),
    "replace block starts blank": len(_E + "-a\n=======\n+"),
    # A trailing blank is only wrong once the block tries to close, so it
    # dies on the terminator, not on the blank.
    "search block ends blank": len(_E + "-a\n-\n"),
    "replace block ends blank": len(_E + "-a\n=======\n+b\n+\n"),
    # The third consecutive blank's newline.
    "the live runaway": len(_E + "-RATE = 0.05\n" + "-\n" * 2 + "-"),
}

# Valid prefixes the grammar must refuse to END on. This is the check the
# live runaway needed: every one of these is a legal prefix.
MUST_NOT_STOP = {
    "header only": _E,
    "search block, no separator": _E + "-a\n",
    "separator, no terminator": _E + "-a\n=======\n",
    "replacement, no terminator": _E + "-a\n=======\n+b\n",
    "create without terminator": "create n.py\n+a\n",
    "one blank after content": _E + "-a\n-\n",
    "two blanks after content": _E + "-a\n-\n-\n",
}

MUST_STOP = {
    "complete edit": _E + "-a\n=======\n+b\n>>>>>>>\n",
    "complete delete": "delete n.py\n",
    "complete create": "create n.py\n+a\n>>>>>>>\n",
    "empty replacement": _E + "-a\n=======\n>>>>>>>\n",
    "empty created file": "create n.py\n>>>>>>>\n",
}

SYNTHETIC = {
    "_edit": _E + "-x = 1\n=======\n+x = 2\n>>>>>>>\n",
    "_edit_one_interior_blank": _E + "-a\n-\n-b\n=======\n+a\n+\n+b\n>>>>>>>\n",
    "_edit_two_interior_blanks": (
        _E + "-def a():\n-    pass\n-\n-\n-def b():\n"
        "=======\n+def a():\n+    pass\n+\n+\n+def c():\n>>>>>>>\n"
    ),
    "_edit_indented": _E + "-    return 1\n=======\n+    return 2\n>>>>>>>\n",
    # A line of spaces is blank for the bound's purposes and still legal
    # between two that are not: files carry trailing whitespace.
    "_edit_whitespace_only_interior": _E + "-a\n-   \n-b\n=======\n+a\n>>>>>>>\n",
    # The prefix discipline's whole point: content that looks like the
    # format's own punctuation round-trips.
    "_edit_content_is_a_separator": _E + "-=======\n=======\n+>>>>>>>\n>>>>>>>\n",
    "_edit_content_starts_with_dash": _E + "--x = 1\n=======\n+-x = 2\n>>>>>>>\n",
    "_edit_content_has_a_quote": _E + '-say("hi")\n=======\n+say("bye")\n>>>>>>>\n',
    "_delete": "delete n.py\n",
    "_create": "create a/b.py\n+x = 1\n>>>>>>>\n",
    "_create_empty": "create a/b.py\n>>>>>>>\n",
    "_two_ops": _E + "-x = 1\n=======\n+x = 2\n>>>>>>>\ndelete m.py\n",
    "_nested_path": "edit src/saddle/cli.py\n-a\n=======\n+b\n>>>>>>>\n",
}


def edit_section(path: str, content: str) -> str:
    """*content* as a search block: the whole file, replaced by itself.

    Real content is the point. If the envelope can name a span of
    `vllm.py` -- backslashes, quotes, the grammars' own text -- it can
    name a span of whatever a worker is pointed at. A file whose first or
    last line is blank is returned empty: that block is unrepresentable
    by construction, and stripping it silently would hide the fact.
    """
    lines = content.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not lines or not lines[0].strip() or not lines[-1].strip():
        return ""
    search = "".join(f"-{line}\n" for line in lines)
    replace = "".join(f"+{line}\n" for line in lines)
    return f"edit {path}\n{search}=======\n{replace}>>>>>>>\n"


def _repo_grammar() -> str:
    """The grammar as this checkout defines it (emit runs in the repo)."""
    sys.path.insert(0, str(REPO / "src"))
    from saddle.edits import EDIT_GRAMMAR

    return str(EDIT_GRAMMAR)


def build_cases(commits: int = 40, *, max_bytes: int = 1_500_000) -> dict[str, Any]:
    """The corpus plus the grammar it is checked against.

    The admit half is every distinct text blob this repo wrote in its last
    *commits* commits, rendered as a search block naming the whole file.
    Binary blobs are skipped: a shallow checkout's one commit lists every
    file, images too.
    Any file with a blank first or last line is skipped, and how many were
    skipped is reported by `--emit` on stderr so the number is never zero
    by accident.
    """

    def git(args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(args, cwd=REPO, capture_output=True, text=True, check=False)

    shas = git(["git", "log", "--format=%H", f"-{commits}"]).stdout.split()
    cases = dict(SYNTHETIC)
    seen: set[str] = set()
    total = skipped = 0
    for sha in shas:
        names = git(["git", "show", "--name-only", "--format=", sha]).stdout.split("\n")
        for name in (n for n in names if n.strip()):
            blob = git(["git", "rev-parse", f"{sha}:{name}"])
            if blob.returncode != 0 or blob.stdout.strip() in seen:
                continue
            seen.add(blob.stdout.strip())
            shown = subprocess.run(
                ["git", "show", f"{sha}:{name}"], cwd=REPO, capture_output=True, check=False
            )
            try:
                text = shown.stdout.decode()
            except UnicodeDecodeError:  # an image: a worker never writes one as text
                continue
            if shown.returncode != 0 or not text:
                continue
            if total + len(text) > max_bytes:
                continue
            section = edit_section(name, text)
            if not section:
                skipped += 1
                continue
            total += len(text)
            cases[f"{sha[:8]}:{name}"] = section
    note = f"{len(cases)} admit cases, {skipped} files skipped (blank first/last line)"
    print(note, file=sys.stderr)
    return {
        "grammar": _repo_grammar(),
        "admit": cases,
        "reject": MUST_REJECT,
        "reject_at": REJECT_AT,
        "not_stop": MUST_NOT_STOP,
        "stop": MUST_STOP,
    }


def emit(commits: int = 40) -> None:
    """Write the corpus: this repo's own files plus the awkward constructs."""
    print(json.dumps(build_cases(commits)))


if __name__ == "__main__":
    if "--emit" in sys.argv:
        emit()
        sys.exit(0)
    if not Path(CASES_PATH).exists():
        print(f"no {CASES_PATH}: run --emit from the repo checkout first")
        sys.exit(2)
    # `--run` is the one path that needs the sibling tool, and it runs
    # under an interpreter that has xgrammar but not this repo installed,
    # so the root goes on the path here rather than at import time.
    sys.path.insert(0, str(REPO))
    from tools.diff_grammar_check import run

    sys.exit(run())
