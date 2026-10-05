"""K2 D3: the in-loop raise-entry obligation, off by default.

With `--raise-obligation`, each checkpoint audit also names every changed
`raise` no test enters (the packet's §3.3 row) and every type-free raise
assertion in a changed test (`pytest.raises(Exception)`, flake8-bugbear
B017), and asks for a test that asserts the exception by its type. It is
feedback: it is no finding, so it never refuses `finish`, and without the
switch nothing is said. The switch needs `--allow-test-edits`, since the
obligation asks for tests.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any, cast

import pytest

from saddle import cli
from saddle.auditor import Finding, Findings
from saddle.auto import AutoOptions, AutoResult
from saddle.evidence import run_argv
from saddle.feed import RAISE_OBLIGATION_HEAD, AuditFeed, raise_obligation
from saddle.gates import untyped_raise_asserts
from saddle.vllm import VllmClient


def test_only_a_type_free_raise_assertion_is_named() -> None:
    tests = {
        "t.py": (
            "import builtins\nimport pytest\n\n\ndef test_a():\n"
            "    with pytest.raises(Exception):\n        f()\n"
            "    with pytest.raises(builtins.BaseException):\n        f()\n"
            "    with pytest.raises(ValueError):\n        f()\n"
            "    with pytest.raises((Exception, ValueError)):\n        f()\n"
            "    with open('x'):\n        f()\n"
        ),
        "u.py": "import unittest\n\n\nclass T(unittest.TestCase):\n"
        "    def test_a(self):\n        self.assertRaises(Exception, f)\n",
        "v.py": "def (:\n",
    }
    assert untyped_raise_asserts(tests) == [
        ("t.py", 6, "pytest.raises(Exception)"),
        ("t.py", 8, "pytest.raises(builtins.BaseException)"),
        ("u.py", 6, "self.assertRaises(Exception)"),
    ]


def _git(root: Path, *argv: str) -> None:
    assert run_argv(["git", *argv], root) == 0


BASE = "def f(x):\n    return x\n\n\ndef g(x):\n    return x\n"
TREE = (
    "def f(x):\n    if x < 0:\n        raise ValueError(x)\n    return x\n\n\n"
    "def g(x):\n    if x < 0:\n        raise KeyError(x)\n    return x\n"
)
BASE_TEST = "from n import f, g\n\n\ndef test_f():\n    assert f(1) == g(1) == 1\n"
HOLLOW = BASE_TEST + (
    "\n\ndef test_neg():\n    import pytest\n\n    with pytest.raises(Exception):\n        f(-1)\n"
)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    for argv in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        _git(root, *argv)
    (root / "n.py").write_text(BASE)
    (root / "test_n.py").write_text(BASE_TEST)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    (root / "n.py").write_text(TREE)
    (root / "test_n.py").write_text(HOLLOW)
    return root


def _checkpoint(feed: AuditFeed) -> str:
    feed.after_tool("write_file", ok=True)
    feed.before_tool("run_command")
    assert feed._pending is not None
    feed._pending.result()
    return feed.collect()


@pytest.mark.parametrize("armed", [True, False], ids=["on", "off-by-default"])
def test_a_checkpoint_names_the_unentered_raise_and_the_hollow_test(
    tmp_path: Path, armed: bool
) -> None:
    root = _repo(tmp_path)
    kw: dict[str, Any] = {"raise_obligation": True} if armed else {}
    feed = AuditFeed(
        worktree=root, baseline="HEAD", journal=tmp_path / "j.jsonl", run_span="s", **kw
    )
    text = _checkpoint(feed)
    feed.close()
    assert "- coverage (tier 1): fail" in text, text
    if not armed:
        assert RAISE_OBLIGATION_HEAD not in text
        assert all(r.obligation == "" for r in feed.results)
        return
    owed = text[text.index(RAISE_OBLIGATION_HEAD) :].splitlines()
    assert owed == [
        RAISE_OBLIGATION_HEAD,
        "- raise in `g` (`n.py:9`): no test enters this branch. Add a test that takes this "
        "branch and asserts the exception by the type raised there, `pytest.raises(<that "
        "type>)`, not `Exception`.",
        "- test_n.py:11 asserts `pytest.raises(Exception)`: it passes on any exception, a "
        "crash before the check included. Assert the type the code raises.",
    ]
    assert feed.results[-1].to_dict()["raise_obligation"] == "\n".join(owed)


def test_nothing_owed_says_nothing(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "test_n.py").write_text(BASE_TEST)
    passing = Finding("coverage", 1, "pass", "evidence-thin", "every changed line runs", ("c",))
    assert raise_obligation(root, "HEAD", [passing]) == ""
    assert raise_obligation(root, "HEAD", []) == ""


class Unproven:
    """Every tier passes but coverage, which names `g`'s raise as not proven:
    the finish audit accepts, whatever the obligation said at the checkpoint."""

    def _found(self, tier: int) -> Findings:
        gate, verdict, detail = (
            ("coverage", "not-proven", "no test runs n.py:9")
            if tier == 1
            else ("tests", "pass", "1 passed")
        )
        found = Finding(gate, tier, verdict, "evidence-thin", detail, ("fake",))  # type: ignore[arg-type]
        return Findings(tier=tier, key=f"k{tier}", findings=(found,))

    def tier0(self, path: str, new_text: str) -> Findings:
        return self._found(0)

    def tier1(self, tree: Path | None = None) -> Findings:
        return self._found(1)

    def tier2(self, tree: Path | None = None) -> Findings:
        return self._found(2)


def test_the_obligation_never_refuses_finish(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    feed = AuditFeed(
        worktree=root,
        baseline="HEAD",
        journal=tmp_path / "j.jsonl",
        run_span="s",
        raise_obligation=True,
        factory=lambda *a: Unproven(),
    )
    assert RAISE_OBLIGATION_HEAD in _checkpoint(feed)  # armed, and it fired
    accepted, _ = feed.final()
    feed.close()
    assert accepted
    assert feed.results[-1].point == "finish"
    assert feed.results[-1].obligation == ""


def _args(*flags: str) -> Any:
    return cli.build_parser().parse_args(["auto", "t", "--repo", ".", *flags])


def test_the_switch_needs_test_edits_and_reaches_the_feed(monkeypatch: pytest.MonkeyPatch) -> None:
    out = io.StringIO()
    assert (
        cli.run_auto_command(_args("--raise-obligation"), cast(VllmClient, None), stdout=out) == 2
    )
    assert out.getvalue() == f"error: {cli.RAISE_OBLIGATION_NEEDS_TESTS}\n"
    seen: list[bool] = []

    def spy(options: AutoOptions, client: Any, **kw: Any) -> AutoResult:
        seen.append(options.raise_obligation)
        raise SystemExit(0)

    monkeypatch.setattr(cli, "run_auto", spy)
    for flags in (("--raise-obligation", "--allow-test-edits"), ("--allow-test-edits",)):
        with pytest.raises(SystemExit):
            cli.run_auto_command(_args(*flags), cast(VllmClient, None), stdout=io.StringIO())
    assert seen == [True, False]
