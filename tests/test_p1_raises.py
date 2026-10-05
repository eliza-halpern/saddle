"""P1-R: a validation raise the task text states, judged on real tree code.

Fixtures are synthetic (a port parser, a record reader); each test names the
K2 spec fixture it is (K2-1 .. K2-9b). The driver runs for real
(`task_requirements.run_examples`); predictions, references and probes are
built here, so each test fixes exactly the route it is about.
"""

from __future__ import annotations

from pathlib import Path

from saddle.gates import TaskRequirementsCheck, check_task_requirements
from saddle.task_examples import (
    Example,
    Outcome,
    Prediction,
    Probe,
    Reference,
    TreeOutcome,
    classify,
)
from saddle.task_requirements import ProbeTree, probe_listing, run_examples, run_probes
from saddle.task_units import Units, task_units

PORT_TEXT = """# Ports

- `parse_port(s)` raises `ValueError` if `s` is not a number from 1 to 65535.
- `parse_port(s)` returns the number `s` writes.
"""
PORT_UNITS = task_units(PORT_TEXT)

PORT_GOOD = """\
def parse_port(s):
    if not s.isdigit():
        raise ValueError(s)
    n = int(s)
    if n < 1 or n > 65535:
        raise ValueError(s)
    return n
"""

# The digit check replaced by `pass`: "abc" falls through to a TypeError
# three frames inside the callee.
PORT_FALLTHROUGH = """\
def parse_port(s):
    if not s.isdigit():
        pass
    return _a(s)


def _a(s):
    return _b(s)


def _b(s):
    return _c(s)


def _c(s):
    return int(s) if s.isdigit() else s + 1
"""

WRAPS = """\
import functools


def checked(f):
    @functools.wraps(f)
    def wrapper(*a, **k):
        return f(*a, **k)

    return wrapper


"""

BARE_WRAPPER = """\
def checked(f):
    def wrapper(*a, **k):
        return f(*a, **k)

    return wrapper


"""


def module(root: Path, source: str, name: str = "ports") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{name}.py").write_text(source)
    return root


def decorated(source: str, prefix: str) -> str:
    return prefix + source.replace("def parse_port(", "@checked\ndef parse_port(", 1)


def example(
    call: str,
    expected: Outcome,
    *,
    units: tuple[str, ...] = ("S-001",),
    setup: tuple[str, ...] = ("from ports import parse_port",),
    probes: tuple[Probe, ...] | None = None,
    eid: str = "E-001",
) -> Example:
    """A route (b) example: three agreeing predictions and references, and,
    unless `probes` says otherwise, three known-correct probes that agree."""
    return Example(
        id=eid,
        units=units,
        setup=setup,
        call=call,
        predictions=tuple(Prediction(expected, "", f"h{i}") for i in range(3)),
        references=tuple(Reference("ran", expected) for _ in range(3)),
        probes=tuple(Probe(f"{i}" * 64, "ran", expected) for i in range(3))
        if probes is None
        else probes,
    )


def on_tree(root: Path, *examples: Example) -> dict[str, TreeOutcome]:
    got = run_examples(root, examples)
    assert isinstance(got, dict)
    return got


def gate(
    root: Path, *examples: Example, units: Units = PORT_UNITS, licensed: bool = True
) -> TaskRequirementsCheck:
    return check_task_requirements(
        on_tree(root, *examples), units, list(examples), licensed=licensed
    )


VALUE_ERROR = Outcome.of("raises", "ValueError")


# -- R-3: "could not call" is decided before the call ------------------------


def test_k2_3_a_fallthrough_type_error_inside_the_callee_is_the_trees_outcome(
    tmp_path: Path,
) -> None:
    e = example('parse_port("abc")', VALUE_ERROR)
    got = on_tree(module(tmp_path / "t", PORT_FALLTHROUGH), e)["E-001"]
    assert (got.kind, got.raises[0]) == ("raises", "TypeError")
    row = gate(tmp_path / "t", e).rows[0]
    assert (row.status, row.got) == ("code-wrong", "raises TypeError")
    # known-good half: the correct tree raises the named type
    assert gate(module(tmp_path / "g", PORT_GOOD), e).rows[0].status == "pass"


def test_k2_3b_a_call_that_cannot_bind_never_runs(tmp_path: Path) -> None:
    other_arity = PORT_GOOD.replace("def parse_port(s):", "def parse_port(s, base):")
    e = example('parse_port("abc")', VALUE_ERROR)
    got = on_tree(module(tmp_path / "t", other_arity), e)["E-001"]
    assert got.kind == "could-not-call"
    assert got.detail.startswith("call: TypeError: missing a required argument: 'base'")
    assert got.ran == ()  # the body never ran
    assert gate(tmp_path / "t", e).rows[0].status == "not-proven"


def test_k2_3c_a_wraps_decorator_is_bound_against_the_inner_signature(tmp_path: Path) -> None:
    """A correct tree, a wrong-arity example: the inner TypeError is raised
    from the wrapper's frame, inside the tree, and must not read as the
    tree's outcome."""
    root = module(tmp_path / "t", decorated(PORT_GOOD, WRAPS))
    e = example('parse_port("80", 10)', VALUE_ERROR)
    got = on_tree(root, e)["E-001"]
    assert got.kind == "could-not-call"
    assert "does not bind to the callee's signature" in got.detail
    check = gate(root, e)
    assert (check.rows[0].status, check.passed) == ("not-proven", True)


def test_k2_3d_a_wraps_decorator_does_not_hide_a_real_fallthrough(tmp_path: Path) -> None:
    root = module(tmp_path / "t", decorated(PORT_FALLTHROUGH, WRAPS))
    e = example('parse_port("abc")', VALUE_ERROR)
    row = gate(root, e).rows[0]
    assert (row.status, row.got) == ("code-wrong", "raises TypeError")


def test_k2_3e_a_bare_wrapper_hides_the_signature_and_the_probe_guards_it(
    tmp_path: Path,
) -> None:
    """The admitted residual: without `functools.wraps`, `bind` succeeds
    against `(*a, **k)` and the inner arity error is an outcome. Route (b)
    needs a known-correct probe that accepted the call; a probe wrapped the
    same way gives the TypeError too, so the example is probe-rejected and
    refuses nothing."""
    e = Example("E-001", ("S-001",), ("from ports import parse_port",), 'parse_port("80", 10)', ())
    probe = ProbeTree(module(tmp_path / "probe", decorated(PORT_GOOD, BARE_WRAPPER)), "user")
    listed = probe_listing([probe])
    sealed = run_probes([probe], listed, [e])["E-001"]
    assert sealed[0]["outcome"] == {"kind": "raises", "text": "TypeError"}
    probes = tuple(Probe.from_dict({**p, "source": "user"}) for p in sealed)
    routed = example('parse_port("80", 10)', VALUE_ERROR, probes=probes)
    assert classify(routed, PORT_UNITS).probe_rejected.endswith("gives raises TypeError")
    root = module(tmp_path / "t", decorated(PORT_GOOD, BARE_WRAPPER))
    check = gate(root, routed)
    assert (check.rows[0].status, check.passed) == ("not-judged", True)


GENERATED = """\
_namespace = {"__name__": __name__}
exec("def parse_port(s):\\n    return s.digits\\n", _namespace)
parse_port = _namespace["parse_port"]
"""


def test_a_bound_callee_compiled_from_a_string_raises_the_trees_outcome(tmp_path: Path) -> None:
    """Code compiled from a string (as `dataclasses` and `namedtuple` generate
    theirs) has the filename `<string>`, the driver's own: an exception from
    inside it after binding is still the tree's, never "could not call"."""
    e = example('parse_port("abc")', VALUE_ERROR)
    got = on_tree(module(tmp_path / "t", GENERATED), e)["E-001"]
    assert (got.kind, got.raises[0]) == ("raises", "AttributeError")
    assert gate(tmp_path / "t", e).rows[0].status == "code-wrong"
