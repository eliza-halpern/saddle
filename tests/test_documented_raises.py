"""K2 D4: a docstring's `Raises` section is asked about, never refused.

For each changed function whose docstring names a type T under `Raises`
(Google or NumPy style), the audit asks when the body raises no T (the
pydoclint/ruff DOC502 shape) and when no test asserts T around a call of
the function. The finding is a `question`: the docstring and the code share
an author, so it never refuses and is never counted as an X1 catch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from saddle.auditor import Auditor, AuditorConfig
from saddle.evidence import run_argv
from saddle.gates import (
    DOCUMENTED_RAISES,
    DOCUMENTED_RAISES_HELD,
    check_documented_raises,
    documented_raises,
)

GOOGLE = """Parse a port.

    Args:
        s: the text.

    Raises:
        ValueError: if `s` is not a number
            from 1 to 65535.
        errors.ParseError: if `s` is empty.
        When neither, nothing is raised.

    Returns:
        KeyError: not an entry of Raises.
    """

NUMPY = """Parse a port.

    Raises
    ------
    ValueError
        If `s` is out of range.
    pkg.ParseError
        If `s` is empty.

    Returns
    -------
    KeyError
    """


def test_google_and_numpy_sections_name_their_types_and_nothing_else() -> None:
    assert documented_raises(GOOGLE) == ["ValueError", "ParseError"]
    assert documented_raises(NUMPY) == ["ValueError", "ParseError"]
    # known-bad: prose that says "raises", a heading with no rule, no section
    assert documented_raises("Raises the bar.\n\nValueError: no.") == []
    assert documented_raises("Do it.\n\nRaises\nValueError\n") == []
    assert documented_raises("Do it.\n\nRaises\n") == []
    assert documented_raises("") == []
    # a NumPy section that runs to the end, and one a dedent ends
    assert documented_raises("Do it.\n\nRaises\n------\nKeyError\n") == ["KeyError"]
    nested = "Do it.\n\n  Raises\n  ------\n  KeyError\nTypeError\n"
    assert documented_raises(nested) == ["KeyError"]
    prose = "Do it.\n\nRaises\n------\nKeyError\nwhen the key is missing\n"
    assert documented_raises(prose) == ["KeyError"]


def _function(body: str, doc: str = "Raises:\n        ValueError: if x < 0.") -> str:
    return f'def f(x):\n    """Check x.\n\n    {doc}\n    """\n{body}'


RAISES = "    if x < 0:\n        raise ValueError(x)\n    return x\n"
ASSERTS = (
    "import pytest\nfrom m import f\n\n\ndef test_f():\n"
    "    with pytest.raises(ValueError):\n        f(-1)\n"
)


def _asks(source: str, tests: str = ASSERTS, changed: set[int] | None = None) -> list[str]:
    lines = changed if changed is not None else set(range(1, 20))
    check = check_documented_raises({"m.py": source}, {("m.py", n) for n in lines}, {"t.py": tests})
    assert check.passed
    return [] if check.detail == DOCUMENTED_RAISES_HELD else check.detail.split("; ")


def test_a_raised_and_asserted_type_asks_nothing() -> None:
    assert _asks(_function(RAISES)) == []


def test_a_documented_type_the_body_never_raises_is_asked() -> None:
    asks = _asks(_function("    return x\n"))
    assert asks == [
        "`f` (m.py:1) documents raising ValueError, but its body raises no ValueError: "
        "should it raise ValueError, or should the docstring not name it?"
    ]


def test_a_documented_type_no_test_asserts_is_asked_and_exception_does_not_assert_it() -> None:
    no_test = "from m import f\n\n\ndef test_f():\n    assert f(1) == 1\n"
    hollow = ASSERTS.replace("pytest.raises(ValueError)", "pytest.raises(Exception)")
    other = ASSERTS.replace("f(-1)", "g(-1)")
    for tests in (no_test, hollow, other):
        assert _asks(_function(RAISES), tests) == [
            "`f` (m.py:1) documents raising ValueError, but no test asserts it: "
            "add a test that calls f inside `pytest.raises(ValueError)`"
        ], tests


def test_the_other_assertion_spellings_count() -> None:
    call_form = (
        "import pytest\nfrom m import f\n\n\ndef test_f():\n    pytest.raises(ValueError, f, -1)\n"
    )
    unittest = (
        "import unittest\nfrom m import f\n\n\nclass T(unittest.TestCase):\n"
        "    def test_f(self):\n        with self.assertRaises((KeyError, ValueError)):\n"
        "            f(-1)\n"
    )
    for tests in (call_form, unittest):
        assert _asks(_function(RAISES), tests) == [], tests
    # a with-block that is not an assertion, and a test file that does not parse
    plain = "def test_f():\n    with open('x') as h:\n        f(-1)\n"
    assert len(_asks(_function(RAISES), plain)) == 1
    assert len(_asks(_function(RAISES), "def (:\n")) == 1


def test_a_bare_reraise_raises_what_its_handler_caught_and_a_nested_raise_does_not() -> None:
    reraise = (
        "    try:\n        return int(x)\n    except (ValueError, TypeError):\n        raise\n"
    )
    assert _asks(_function(reraise)) == []
    nested = "    def g():\n        raise ValueError(x)\n    return g\n"
    assert len(_asks(_function(nested))) == 1
    lam = "    g = lambda: x\n    return g\n"
    assert len(_asks(_function(lam))) == 1
    dotted = "    if x < 0:\n        raise errors.ValueError(x)\n    return x\n"
    assert _asks(_function(dotted)) == []
    indexed = "    if x < 0:\n        raise ERRORS[0]\n    return x\n"
    assert len(_asks(_function(indexed))) == 1


def test_an_unchanged_or_undocumented_function_asks_nothing() -> None:
    assert _asks(_function("    return x\n"), changed=set()) == []
    assert _asks('def f(x):\n    """Check x."""\n    return x\n') == []
    assert _asks("def (:\n") == []


def test_a_constructor_is_asserted_through_its_class() -> None:
    source = (
        "class P:\n    def __init__(self, x):\n"
        '        """Make one.\n\n        Raises:\n            ValueError: if x < 0.\n        """\n'
        "        if x < 0:\n            raise ValueError(x)\n"
    )
    tests = ASSERTS.replace("from m import f", "from m import P").replace("f(-1)", "P(-1)")
    assert _asks(source, tests) == []
    assert len(_asks(source, ASSERTS)) == 1


# -- end to end: a question finding, never a refusal ----------------------------


def _git(root: Path, *argv: str) -> None:
    assert run_argv(["git", *argv], root) == 0


BASE = "def f(x):\n    return x\n"
TREE = _function(RAISES)
TESTED = "import pytest\n\nfrom m import f\n\n\ndef test_f():\n    assert f(1) == 1\n"
ASSERTED = TESTED + "\n\ndef test_neg():\n    with pytest.raises(ValueError):\n        f(-1)\n"
HOLLOW = ASSERTED.replace("pytest.raises(ValueError)", "pytest.raises(Exception)")
"""Runs the raise, so coverage holds, but asserts no type (the B017 shape)."""


@pytest.mark.parametrize(
    ("test", "asked"), [(HOLLOW, True), (ASSERTED, False)], ids=["ask", "held"]
)
def test_the_audit_asks_and_never_refuses(tmp_path: Path, test: str, asked: bool) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    for argv in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        _git(root, *argv)
    (root / "m.py").write_text(BASE)
    (root / "test_m.py").write_text(TESTED)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    (root / "m.py").write_text(TREE)
    (root / "test_m.py").write_text(test)
    found = Auditor(root, config=AuditorConfig(journal=tmp_path / "proofs.jsonl")).tier1()
    documented = [f for f in found.findings if f.gate == DOCUMENTED_RAISES]
    assert found.passed, [(f.gate, f.verdict, f.detail) for f in found.findings]
    assert found.needs_you is asked
    if asked:
        (finding,) = documented
        assert (finding.verdict, finding.reason) == ("question", "evidence-thin")
        assert "no test asserts it" in finding.detail
    else:
        assert documented == []


def test_a_listed_file_the_tree_no_longer_holds_is_left_out(tmp_path: Path) -> None:
    from saddle.auditor import documented_raises_finding

    root = tmp_path / "tree"
    root.mkdir()
    for argv in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        _git(root, *argv)
    (root / "m.py").write_text(BASE)
    (root / "test_gone.py").write_text(ASSERTED)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    (root / "m.py").write_text(TREE)
    # known-good half: with the test file present, nothing is asked
    assert documented_raises_finding(root, "HEAD") is None
    (root / "test_gone.py").unlink()
    finding = documented_raises_finding(root, "HEAD")
    assert finding is not None
    assert "no test asserts it" in finding.detail
