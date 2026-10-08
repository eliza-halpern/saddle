"""`saddle run` and `saddle dag` are labelled legacy, not removed (#89).

They are the Phase 1 multi-node pipeline: they run only on a server with
constrained decoding, and `saddle auto` is the lane measured since Phase 2.
Removing them was the irreversible option, for no user-visible gain.

Known-bad: their help reads like any supported command, and a run says nothing.
Known-good: each says it is legacy in its own help and prints one notice naming
`saddle auto` each time it starts; every other command prints no notice; both
still parse and run.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from saddle import cli


@pytest.mark.parametrize("command", ["run", "dag"])
def test_the_help_says_legacy_and_names_the_supported_lane(
    command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as done:
        cli.main([command, "--help"])
    assert done.value.code == 0
    out = " ".join(capsys.readouterr().out.split())  # argparse wraps the description
    assert "Legacy and unmeasured" in out
    assert "`saddle auto` is the supported lane" in out


@pytest.mark.parametrize("command", ["run", "dag"])
def test_each_start_prints_one_notice_naming_saddle_auto(command: str, tmp_path: Path) -> None:
    err = io.StringIO()
    cli.main([command, "add a function", "--repo", str(tmp_path)], stdout=io.StringIO(), stderr=err)
    notices = [line for line in err.getvalue().splitlines() if "legacy" in line]
    assert notices == [cli.LEGACY_NOTICE.format(command=command)]
    assert "`saddle auto` is the supported lane" in notices[0]


def test_a_supported_command_prints_no_notice(tmp_path: Path) -> None:
    err = io.StringIO()
    cli.main(["verify", str(tmp_path / "missing.jsonl")], stdout=io.StringIO(), stderr=err)
    assert "legacy" not in err.getvalue()
