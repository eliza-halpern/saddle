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

- every diff in this repo's own history must be ADMITTED, plus rename,
  mode-change, delete and "\\ No newline at end of file";
- prose, markdown fences, a JSON wrapper, a `diff -u` header and a hunk
  that follows `diff --git` with no `--- `/`+++ ` lines must be REJECTED,
  the last one at the `@@` itself (`reject_at`) -- rejecting it later
  would mean some other rule had swallowed the header;
- and the model must be FORBIDDEN FROM STOPPING on a header with no hunk.
  That third check is the token-mask guarantee itself, and it needs a stop
  token: a header-without-hunk is a valid *prefix*, so byte-acceptance
  alone cannot see it. A contract mutant (`hunk+` -> `hunk*`) survived the
  first two checks and is killed only by this one.

A construct missing from the grammar is a diff the worker cannot express,
which is the exact failure the grammar exists to prevent.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

CASES_PATH = "/tmp/cases.json"
REPO = Path(__file__).resolve().parent.parent  # git runs here, whatever the cwd

MUST_REJECT = {
    "prose": "Sure! I will fix that.\n",
    "markdown fence": "```diff\ndiff --git a/n.py b/n.py\n",
    "json wrapper": '{"diff": "diff --git a/n.py b/n.py\\n"}',
    "wrong header": "diff -u a/n.py b/n.py\n@@ -1 +1 @@\n-x\n+y\n",
    "hunk before header": "@@ -1 +1 @@\n-x = 1\n+x = 2\n",
    "hunk without file lines": "diff --git a/x b/x\n@@ -1 +1 @@\n-a\n+b\n",
    # T6-32: a creation without `new file mode` applied as `dev/null` (F21.14).
    "creation without mode line": (
        "diff --git a/n.py b/n.py\n--- /dev/null\n+++ b/n.py\n@@ -0,0 +1 @@\n+x = 1\n"
    ),
    "deletion without mode line": (
        "diff --git a/n.py b/n.py\n--- a/n.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x = 1\n"
    ),
}

# Byte offset at which a MUST_REJECT case has to die. Absence of a working
# diff is not the only way to fail: a case rejected at the wrong byte is
# being refused by the wrong rule.
REJECT_AT = {
    "hunk without file lines": len("diff --git a/x b/x\n"),
    # The `/` of `/dev/null`: a modify side may not start with `/`.
    "creation without mode line": len("diff --git a/n.py b/n.py\n--- "),
    "deletion without mode line": len("diff --git a/n.py b/n.py\n--- a/n.py\n+++ "),
}

# Valid prefixes the grammar must refuse to END on.
MUST_NOT_STOP = {
    "header only": "diff --git a/n.py b/n.py\n",
    "header no hunk": "diff --git a/n.py b/n.py\n--- a/n.py\n+++ b/n.py\n",
}

MUST_STOP = {
    "complete diff": (
        "diff --git a/n.py b/n.py\n--- a/n.py\n+++ b/n.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
    ),
}

SYNTHETIC = {
    "_rename": (
        "diff --git a/old.py b/new.py\nsimilarity index 95%\nrename from old.py\n"
        "rename to new.py\n--- a/old.py\n+++ b/new.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
    ),
    "_mode": (
        "diff --git a/s.sh b/s.sh\nold mode 100644\nnew mode 100755\n"
        "--- a/s.sh\n+++ b/s.sh\n@@ -1 +1 @@\n-echo a\n+echo b\n"
    ),
    "_nonewline": (
        "diff --git a/n.py b/n.py\n--- a/n.py\n+++ b/n.py\n@@ -1 +1 @@\n"
        "-x = 1\n\\ No newline at end of file\n+x = 2\n"
    ),
    "_delete": (
        "diff --git a/gone.py b/gone.py\ndeleted file mode 100644\n"
        "--- a/gone.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-x = 1\n-y = 2\n"
    ),
    # T6-32: the creation shape git itself emits, mode then index then /dev/null.
    "_create": (
        "diff --git a/new.py b/new.py\nnew file mode 100644\nindex 0000000..e69de29\n"
        "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1,2 @@\n+x = 1\n+y = 2\n"
    ),
}


def _repo_grammar() -> str:
    """The grammar as this checkout defines it (emit runs in the repo)."""
    sys.path.insert(0, str(REPO / "src"))
    from saddle.vllm import DIFF_GRAMMAR

    return str(DIFF_GRAMMAR)


def build_cases(commits: int = 40) -> dict[str, Any]:
    """The corpus plus the grammar it is checked against.

    The grammar travels inside the cases file so that `--run` compiles
    exactly the text this checkout has, with no import of `saddle` in the
    container.
    """
    shas = subprocess.run(
        ["git", "log", "--format=%H", f"-{commits}"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    cases = dict(SYNTHETIC)
    for sha in shas:
        diff = subprocess.run(
            ["git", "show", sha, "--format=", "--no-color"],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        if diff.strip():
            cases[sha[:8]] = diff
    return {
        "grammar": _repo_grammar(),
        "admit": cases,
        "reject": MUST_REJECT,
        "reject_at": REJECT_AT,
        "not_stop": MUST_NOT_STOP,
        "stop": MUST_STOP,
    }


def emit(commits: int = 40) -> None:
    """Write the corpus: this repo's own diffs plus the awkward constructs."""
    print(json.dumps(build_cases(commits)))


def run() -> int:
    """Compile the emitted grammar with the real xgrammar and check both halves."""
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
        if may_stop(text):
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
