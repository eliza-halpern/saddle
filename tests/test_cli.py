"""Tests for saddle.cli."""

from __future__ import annotations

import pytest

from saddle.cli import main


def test_main_no_args_returns_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert capsys.readouterr().out == ""


def test_version_flag_prints_and_exits(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit, match=r"^0$"):
        main(["--version"])
    out = capsys.readouterr().out
    assert out.startswith("saddle 0.1.0")


def test_help_flag_shows_exact_description(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit, match=r"^0$"):
        main(["--help"])
    out = capsys.readouterr().out
    assert "\nDeterministic harness for local LLMs.\n" in out
