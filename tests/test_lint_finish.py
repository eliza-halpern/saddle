"""Tier 0 at finish (tightened).

The contract: a tree the in-run `finish` accepts is a tree the post-hoc
`saddle audit --tiered` tier 0 accepts, on the changed files. In the M3
live runs every EAF-t5 tree finished with a passing in-run audit and was
refused post hoc for `ruff format --check` and B904/F401 (`refuse:lint`),
because `feed.AuditFeed.final` dispatched tiers 1 and 2 only.

Both halves: the recorded EAF-t5 s6 tree is refused at finish with the
tier-0 reason (red on a2acb8e), a clean change finishes, and tier 0 runs
over the changed files only, so an unformatted file the change never
touched does not refuse finish (as post hoc it would not).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from saddle.auditor import Auditor, Finding, Findings
from saddle.feed import AuditFeed

FIXTURE = Path(__file__).parent / "fixtures" / "eaf_t5_s6_tree.json"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def write_tree(root: Path, files: dict[str, str]) -> None:
    for entry in list(root.iterdir()):
        if entry.name != ".git":
            shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)


@pytest.fixture
def recorded() -> dict:  # type: ignore[type-arg]
    loaded: dict = json.loads(FIXTURE.read_text())  # type: ignore[type-arg]
    return loaded


@pytest.fixture
def repo(tmp_path: Path, recorded: dict) -> Path:  # type: ignore[type-arg]
    """The M3 EAF-t5 baseline, committed; the worktree is the baseline."""
    root = tmp_path / "repo"
    root.mkdir()
    write_tree(root, recorded["baseline"])
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "baseline")
    return root


class Tier0Only(Auditor):
    """The real tier 0; tiers 1 and 2 pass, so tier 0 alone decides finish.

    The finish audit's tier-1/2 verdict on these trees is not in question
    (M3 sealed them passing in-run); the suite and mutation runs they cost
    are not what this contract is about.
    """

    def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self.tier0_files: list[str] = []

    def tier0(self, path: str, new_text: str) -> Findings:
        self.tier0_files.append(path)
        return super().tier0(path, new_text)

    def _pass(self, tier: int, gate: str) -> Findings:
        found = Finding(gate, tier, "pass", "code-wrong", "1 passed", ("stub",))
        return Findings(tier=tier, key=f"k{tier}", findings=(found,))

    def tier1(self, tree: Path | None = None) -> Findings:
        return self._pass(1, "tests")

    def tier2(self, tree: Path | None = None) -> Findings:
        return self._pass(2, "mutation")


def feed_for(repo: Path, tmp_path: Path) -> tuple[AuditFeed, Tier0Only]:
    holder: list[Tier0Only] = []

    def factory(worktree: Path, baseline: str, config):  # type: ignore[no-untyped-def]
        holder.append(Tier0Only(worktree, baseline, config))
        return holder[-1]

    fed = AuditFeed(
        worktree=repo,
        baseline=git(repo, "rev-parse", "HEAD"),
        journal=tmp_path / "j.jsonl",
        run_span="s",
        factory=factory,
        sanctioned_test_rewrites=(
            "test_balance_type_is_float",
            "test_apply_fee_basic",
            "test_apply_fee_larger_amount",
            "test_saved_schema_is_usd_only",
        ),
    )
    return fed, holder[0]


def changed_python(repo: Path, recorded: dict) -> list[str]:  # type: ignore[type-arg]
    """What the post-hoc `Auditor.audit` sends to tier 0: AMR-changed .py files."""
    git(repo, "add", "-A")
    names = git(repo, "diff", "--cached", "--name-only", "--diff-filter=AMR", "HEAD").split()
    git(repo, "reset", "-q")
    return sorted(n for n in names if n.endswith(".py"))


def test_the_recorded_eaf_t5_s6_tree_is_refused_at_finish_for_the_tier0_reason(
    repo: Path,
    tmp_path: Path,
    recorded: dict,  # type: ignore[type-arg]
) -> None:
    write_tree(repo, recorded["finished"])
    fed, auditor = feed_for(repo, tmp_path)
    accepted, text = fed.final()
    assert not accepted
    # The recorded project configures no ruff (#130), so the post-hoc refusal's
    # format findings no longer apply; its one B904 in money.py still refuses.
    assert "- ruff (tier 0): fail" in text
    assert "ruff format would reformat it" not in text
    assert "money.py:67 B904" in text
    refused = [f for f in fed.results[-1].findings if f.verdict == "fail"]
    assert {f.gate for f in refused} == {"ruff"}
    assert len(refused) == 1
    assert all(f.tier == 0 for f in refused)
    assert fed.unresolved() == [
        {"gate": "ruff", "reason": "code-wrong", "cites": ["saddle.gates.check_ruff"]}
    ]
    # tier 0 ran over exactly the changed Python files, once each
    assert auditor.tier0_files == changed_python(repo, recorded)
    assert "money.py" in auditor.tier0_files  # an added file counts (diff-filter A)


def test_the_recorded_tree_is_also_refused_for_format_where_the_project_configures_ruff(
    repo: Path,
    tmp_path: Path,
    recorded: dict,  # type: ignore[type-arg]
) -> None:
    """The format half of the original refusal, on a baseline that chose ruff."""
    (repo / "ruff.toml").write_text("line-length = 88\n")
    git(repo, "add", "ruff.toml")
    git(repo, "commit", "-q", "-m", "configure ruff")
    write_tree(repo, recorded["finished"])
    fed, _auditor = feed_for(repo, tmp_path)
    accepted, text = fed.final()
    assert not accepted
    assert "ruff format would reformat it" in text
    assert "money.py:67 B904" in text
    refused = [f for f in fed.results[-1].findings if f.verdict == "fail"]
    assert {f.gate for f in refused} == {"ruff"}
    # each format finding quotes its own file's diff, so it can be fixed without ruff
    formats = [f for f in refused if f.detail.startswith("ruff format would reformat it")]
    assert len(formats) == 4
    assert all(f"--- {f.path}\n" in f.detail for f in formats)


def test_a_clean_change_to_the_same_baseline_finishes(repo: Path, tmp_path: Path) -> None:
    (repo / "money.py").write_text('"""Money helpers."""\n\n\ndef zero() -> int:\n    return 0\n')
    fed, auditor = feed_for(repo, tmp_path)
    accepted, text = fed.final()
    assert (accepted, text) == (True, "")
    assert auditor.tier0_files == ["money.py"]
    assert fed.results[-1].passed
    assert [f.tier for f in fed.results[-1].findings] == [0, 0, 0, 1, 2]


def test_tier0_at_finish_sees_the_changed_files_only(repo: Path, tmp_path: Path) -> None:
    """An unformatted file the change never touched is not the change's lint;
    post hoc it is not audited either, so refusing on it would break agreement."""
    (repo / "legacy.py").write_text("x=1\n")  # ruff format would rewrite this
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "unformatted legacy")
    (repo / "money.py").write_text('"""Money helpers."""\n\n\ndef zero() -> int:\n    return 0\n')
    fed, auditor = feed_for(repo, tmp_path)
    accepted, _ = fed.final()
    assert accepted
    assert auditor.tier0_files == ["money.py"]
