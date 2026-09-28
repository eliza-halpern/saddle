"""Validate DIFF_GRAMMAR against real diffs. Run this after ANY edit to it.

The grammar is enforced by the serving container's xgrammar, not by this
repo's venv, so this check runs there:

    python tools/diff_grammar_check.py --emit > /tmp/cases.json
    docker cp /tmp/cases.json <container>:/tmp/cases.json
    docker cp tools/diff_grammar_check.py <container>:/tmp/check.py
    docker exec <container> /app/venv/bin/python /tmp/check.py --run

The cases file carries the grammar text itself, so the container needs
nothing from this repo: `--run` reads the grammar from the file, never
from `src/` (there is no `src/` beside `/tmp/check.py`; importing it
there was the first thing the lines above did when actually run).

Why it exists: `DIFF_SCHEMA` once carried `pattern = "^diff --git "`. JSON
Schema calls that an unanchored partial match; the decoder compiles it as
a full match, so xgrammar reduced it to a closed literal admitting exactly
one 11-character string, and no working diff was representable. The test
guarding it asserted the pattern *existed*. Existence was never the
question -- so this checks both halves, on real inputs:

- every FILE this repo has actually written must be ADMITTED as a write
  section -- the envelope's job is to carry arbitrary real content, so
  the corpus is the content itself (quotes, backslashes, regexes, the
  grammar string, unicode), rendered whole-file;
- prose, markdown fences, a JSON wrapper, a `diff -u` header, a hunk with
  no file lines, and -- the envelope's own contract -- a CONTEXT line, a
  REMOVAL line, or an anchor that is not line 1 must be REJECTED, each at
  the byte where its own rule dies (`reject_at`); rejecting later would
  mean some other rule had swallowed it;
- and the model must be FORBIDDEN FROM STOPPING on a header with no body.
  That third check is the token-mask guarantee itself, and it needs a stop
  token: a header-without-body is a valid *prefix*, so byte-acceptance
  alone cannot see it. A contract mutant (`bline+` -> `bline*`) survives
  the first two checks and is killed only by this one.

A construct missing from the grammar is a file the worker cannot express,
which is the exact failure the grammar exists to prevent. Under the whole-file
envelope that means file CONTENT, not diff syntax: the old corpus was this
repo's own `git show` output, which the envelope refuses by design.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

CASES_PATH = "/tmp/cases.json"
REPO = Path(__file__).resolve().parent.parent  # git runs here, whatever the cwd

_W = "diff --git a/n.py b/n.py\n--- /dev/null\n+++ b/n.py\n"

MUST_REJECT = {
    "prose": "Sure! I will fix that.\n",
    "markdown fence": "```diff\ndiff --git a/n.py b/n.py\n",
    "json wrapper": '{"diff": "diff --git a/n.py b/n.py\\n"}',
    "wrong header": "diff -u a/n.py b/n.py\n@@ -0,0 +1 @@\n+y\n",
    "hunk before header": "@@ -0,0 +1 @@\n+x = 1\n",
    "hunk without file lines": "diff --git a/x b/x\n@@ -0,0 +1 @@\n+b\n",
    # The envelope's own contract, all three halves. A write carries the
    # new file and nothing else, so there is no original side to drift
    # against.
    "context line in body": _W + "@@ -0,0 +1,2 @@\n+x = 1\n x = 2\n",
    "removal line in body": _W + "@@ -0,0 +1,2 @@\n+x = 1\n-x = 2\n",
    "anchor past line 1": _W + "@@ -0,0 +2,2 @@\n+x = 1\n",
    "original side not empty": _W + "@@ -1,2 +1,2 @@\n+x = 1\n",
    "second hunk in one write": _W + "@@ -0,0 +1 @@\n+x = 1\n@@ -0,0 +1 @@\n+y = 2\n",
    "deletion without mode line": ("diff --git a/n.py b/n.py\n--- a/n.py\n+++ /dev/null\n"),
    # `path` may not begin with `/`, so `/dev/null` is unrepresentable
    # wherever a real file belongs. Without these two the rule is unpinned:
    # a mutant widening `path` to `[^\n]*` passed every other case.
    "write to /dev/null": (
        "diff --git a/n.py b/n.py\n--- /dev/null\n+++ /dev/null\n@@ -0,0 +1 @@\n+x\n"
    ),
    "delete from /dev/null": (
        "diff --git a/n.py b/n.py\ndeleted file mode 100644\n--- /dev/null\n"
    ),
}

# Byte offset at which a MUST_REJECT case has to die. Absence of a working
# write is not the only way to fail: a case rejected at the wrong byte is
# being refused by the wrong rule.
REJECT_AT = {
    "hunk without file lines": len("diff --git a/x b/x\n"),
    "context line in body": len(_W + "@@ -0,0 +1,2 @@\n+x = 1\n"),
    "removal line in body": len(_W + "@@ -0,0 +1,2 @@\n+x = 1\n"),
    # The `2` where the anchor's literal `1` is required.
    "anchor past line 1": len(_W + "@@ -0,0 +"),
    # The `1` where the original side's literal `0,0` is required.
    "original side not empty": len(_W + "@@ -"),
    # The `@` where only a new `diff --git ` section may begin.
    "second hunk in one write": len(_W + "@@ -0,0 +1 @@\n+x = 1\n"),
    # The `a` of `a/n.py`: a write's original side must be `/dev/null`, and
    # a delete must announce itself with `deleted file mode` first.
    "deletion without mode line": len("diff --git a/n.py b/n.py\n--- "),
    "write to /dev/null": len("diff --git a/n.py b/n.py\n--- /dev/null\n+++ "),
    "delete from /dev/null": len("diff --git a/n.py b/n.py\ndeleted file mode 100644\n--- "),
}

# Valid prefixes the grammar must refuse to END on.
MUST_NOT_STOP = {
    "header only": "diff --git a/n.py b/n.py\n",
    "write without anchor": _W,
    "anchor without body": _W + "@@ -0,0 +1,2 @@\n",
}

MUST_STOP = {
    "complete write": _W + "@@ -0,0 +1 @@\n+x = 2\n",
    "complete delete": (
        "diff --git a/gone.py b/gone.py\ndeleted file mode 100644\n--- a/gone.py\n+++ /dev/null\n"
    ),
}

SYNTHETIC = {
    "_write": _W + "@@ -0,0 +1,2 @@\n+x = 1\n+y = 2\n",
    # git spells a one-line new side without the comma.
    "_write_single_line_anchor": _W + "@@ -0,0 +1 @@\n+x = 1\n",
    "_write_blank_line": _W + "@@ -0,0 +1,3 @@\n+x = 1\n+\n+y = 2\n",
    "_write_noeol": (_W + "@@ -0,0 +1 @@\n+x = 1\n\\ No newline at end of file\n"),
    # Metadata git emits on a creation stays legal, and a write may carry
    # `new file mode` -- it is simply no longer required.
    "_write_with_meta": (
        "diff --git a/new.py b/new.py\nnew file mode 100644\n"
        "index 0000000..e69de29\n--- /dev/null\n+++ b/new.py\n"
        "@@ -0,0 +1,2 @@\n+x = 1\n+y = 2\n"
    ),
    "_two_files": (
        _W + "@@ -0,0 +1 @@\n+x = 1\n"
        "diff --git a/m.py b/m.py\n--- /dev/null\n+++ b/m.py\n"
        "@@ -0,0 +1 @@\n+y = 2\n"
    ),
    "_delete": (
        "diff --git a/gone.py b/gone.py\ndeleted file mode 100644\n--- a/gone.py\n+++ /dev/null\n"
    ),
    "_rename_as_delete_and_write": (
        "diff --git a/old.py b/old.py\ndeleted file mode 100644\n"
        "--- a/old.py\n+++ /dev/null\n"
        "diff --git a/new.py b/new.py\n--- /dev/null\n+++ b/new.py\n"
        "@@ -0,0 +1 @@\n+x = 1\n"
    ),
}


def write_section(path: str, content: str) -> str:
    """*content* as the envelope spells it: one write section, whole file.

    This is the encoder the corpus is built with and the exact inverse of
    `slice.whole_file_reconstruction`, which is the decoder the run uses.
    """
    lines = content.split("\n")
    tail = ""
    if lines and lines[-1] == "":
        lines.pop()
    else:
        tail = "\\ No newline at end of file\n"
    body = "".join(f"+{line}\n" for line in lines)
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- /dev/null\n+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n{body}{tail}"
    )


def _repo_grammar() -> str:
    """The grammar as this checkout defines it (emit runs in the repo)."""
    sys.path.insert(0, str(REPO / "src"))
    from saddle.vllm import DIFF_GRAMMAR

    return str(DIFF_GRAMMAR)


def build_cases(commits: int = 40, *, max_bytes: int = 1_500_000) -> dict[str, Any]:
    """The corpus plus the grammar it is checked against.

    The admit half is every distinct blob this repo wrote in its last
    *commits* commits, each rendered as a write section. Real content is
    the point: `vllm.py` carries the grammar's own backslashes, `slice.py`
    carries regexes, and the docs carry unicode. If the envelope can spell
    those it can spell what a worker emits.

    The grammar travels inside the cases file so that `--run` compiles
    exactly the text this checkout has, with no import of `saddle` in the
    container.
    """

    def git(args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(args, cwd=REPO, capture_output=True, text=True, check=False)

    shas = git(["git", "log", "--format=%H", f"-{commits}"]).stdout.split()
    cases = dict(SYNTHETIC)
    seen: set[str] = set()
    total = 0
    for sha in shas:
        names = git(["git", "show", "--name-only", "--format=", sha]).stdout.split("\n")
        for name in (n for n in names if n.strip()):
            blob = git(["git", "rev-parse", f"{sha}:{name}"])
            if blob.returncode != 0 or blob.stdout.strip() in seen:
                continue
            seen.add(blob.stdout.strip())
            shown = git(["git", "show", f"{sha}:{name}"])
            if shown.returncode != 0 or not shown.stdout:
                continue
            if total + len(shown.stdout) > max_bytes:
                continue
            total += len(shown.stdout)
            cases[f"{sha[:8]}:{name}"] = write_section(name, shown.stdout)
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


def run() -> int:
    """Compile the emitted grammar with the real xgrammar and check both halves.

    Shared by `edit_grammar_check.py`: everything it needs travels in the
    cases file, so the same harness checks either grammar.
    """
    # Only present in the serving container; mypy follows this file now that
    # tests/test_vllm.py imports the reject list from it.
    import xgrammar as xgr  # type: ignore[import-not-found]

    cases = json.loads(Path(CASES_PATH).read_text())
    grammar = cases.get("grammar")
    if not grammar:
        print("cases file carries no grammar: re-run --emit from the repo checkout")
        return 2
    # A byte vocab plus an explicit stop token is what lets the third check
    # ask "may the model stop here?" rather than only "is this byte legal?".
    vocab = [bytes([i]).decode("latin-1") for i in range(256)] + ["<eos>"]
    stop_id = 256
    info = xgr.TokenizerInfo(vocab, vocab_type=xgr.VocabType.RAW, stop_token_ids=[stop_id])
    compiled = xgr.GrammarCompiler(info).compile_grammar(xgr.Grammar.from_ebnf(grammar))

    def first_reject(text: str) -> int | None:
        matcher = xgr.GrammarMatcher(compiled)
        for index, byte in enumerate(text.encode()):
            if not matcher.accept_string(bytes([byte])):
                return index
        return None

    def may_stop(text: str) -> bool:
        matcher = xgr.GrammarMatcher(compiled)
        for byte in text.encode():
            if not matcher.accept_string(bytes([byte])):
                return False
        return bool(matcher.accept_token(stop_id))

    bad = []
    for name, text in sorted(cases["not_stop"].items()):
        # A case the grammar refuses outright cannot say anything about
        # stopping: `may_stop` is False for it either way, so crediting it
        # would be measuring nothing. It has to be a legal prefix first.
        at = first_reject(text)
        if at is not None:
            bad.append(f"MUST NOT STOP is not even a legal prefix, rejected at byte {at}: {name}")
        elif may_stop(text):
            bad.append(f"MUST NOT STOP but grammar allows ending: {name}")
    for name, text in sorted(cases["stop"].items()):
        if not may_stop(text):
            bad.append(f"MUST STOP but grammar forbids ending: {name}")
    for name, diff in sorted(cases["admit"].items()):
        at = first_reject(diff)
        if at is not None:
            bad.append(f"MUST ADMIT but rejected at byte {at}: {name}")
    reject_at = cases.get("reject_at", {})
    for name, text in sorted(cases["reject"].items()):
        at = first_reject(text)
        if at is None:
            bad.append(f"MUST REJECT but admitted: {name}")
        elif name in reject_at and at != reject_at[name]:
            bad.append(f"MUST REJECT at byte {reject_at[name]} but rejected at {at}: {name}")
    total = sum(len(cases[k]) for k in ("admit", "reject", "not_stop", "stop"))
    print(f"{total - len(bad)}/{total} cases correct")
    for line in bad:
        print(f"  {line}")
    return 1 if bad else 0


if __name__ == "__main__":
    if "--emit" in sys.argv:
        emit()
        sys.exit(0)
    sys.exit(run())
