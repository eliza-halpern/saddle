"""Tests for saddle.ux: progress math plus every confirm path."""

from __future__ import annotations

import io

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from saddle.ux import ProgressReporter, ProgressState, ask_confirm


def test_fraction_zero_total_is_complete() -> None:
    assert ProgressState(total=0).fraction == 1.0
    assert ProgressState(total=-3).fraction == 1.0


def test_fraction_clamps_both_directions() -> None:
    assert ProgressState(total=4, done=2).fraction == 0.5
    assert ProgressState(total=4, done=9).fraction == 1.0
    assert ProgressState(total=4, done=-2).fraction == 0.0


def test_advance_accumulates_and_returns_fraction() -> None:
    state = ProgressState(total=4)
    assert state.advance() == 0.25
    assert state.advance(3) == 1.0


@given(st.integers(), st.integers(), st.integers(min_value=1, max_value=10))
@settings(suppress_health_check=[HealthCheck.too_slow])
def test_fraction_always_clamped(total: int, done: int, step: int) -> None:
    state = ProgressState(total=total, done=done)
    assert 0.0 <= state.fraction <= 1.0
    assert 0.0 <= state.advance(step) <= 1.0


def test_reporter_writes_label_and_percent() -> None:
    stream = io.StringIO()
    reporter = ProgressReporter(stream, "nodes", 4)
    assert reporter.fraction == 0.0
    reporter.advance(2)
    assert reporter.fraction == 0.5
    assert stream.getvalue() == "nodes: 50%\n"


def test_reporter_default_step_is_one() -> None:
    stream = io.StringIO()
    reporter = ProgressReporter(stream, "nodes", 4)
    reporter.advance()
    assert stream.getvalue() == "nodes: 25%\n"


def _confirm(stdin_text: str, **kwargs: object) -> tuple[bool, str]:
    stdin = io.StringIO(stdin_text)
    stdout = io.StringIO()
    result = ask_confirm("Proceed?", stdin=stdin, stdout=stdout, **kwargs)  # type: ignore[arg-type]
    return result, stdout.getvalue()


def _prompt_count(out: str) -> int:
    return out.count("[y/N]") + out.count("[Y/n]")


def test_confirm_empty_takes_default_immediately() -> None:
    result, out = _confirm("\n")
    assert result is False
    assert _prompt_count(out) == 1
    result, out = _confirm("\n", default=True)
    assert result is True
    assert _prompt_count(out) == 1


def test_confirm_accepts_all_yes_forms() -> None:
    assert _confirm("y\n")[0] is True
    assert _confirm("YES\n")[0] is True


def test_confirm_accepts_all_no_forms_without_retry() -> None:
    for text in ("n\n", "No\n"):
        result, out = _confirm(text)
        assert result is False
        assert _prompt_count(out) == 1
        assert "Please answer" not in out


def test_confirm_retries_garbage_then_accepts() -> None:
    result, out = _confirm("maybe\nn\n")
    assert result is False
    assert "[y/N] Please answer y or n.\n" in out
    assert _prompt_count(out) == 2


def test_confirm_exhausted_attempts_fall_back_to_default() -> None:
    assert _confirm("maybe\n", attempts=1)[0] is False
    assert _confirm("maybe\n", attempts=1, default=True)[0] is True
    result, out = _confirm("zzz\n", attempts=0)
    assert result is False
    assert _prompt_count(out) == 1


def test_confirm_default_attempts_is_three() -> None:
    result, out = _confirm("bad\nbad\nbad\ny\n")
    assert result is False
    assert _prompt_count(out) == 3


def test_confirm_hint_names_default() -> None:
    assert "[y/N]" in _confirm("\n")[1]
    assert "[Y/n]" in _confirm("\n", default=True)[1]
