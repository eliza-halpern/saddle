"""P1-R: a validation raise the task text states, judged on real tree code.

Fixtures are synthetic (a port parser, a record reader); each test names the
K2 spec fixture it is (K2-1 .. K2-9b). The driver runs for real
(`task_requirements.run_examples`); predictions, references and probes are
built here, so each test fixes exactly the route it is about.
"""

from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from saddle import gates
from saddle.gates import TaskRequirementsCheck, check_task_requirements
from saddle.task_examples import (
    RAISE_NOISE,
    RAISE_ROW,
    Example,
    Outcome,
    Prediction,
    Probe,
    Reference,
    TreeOutcome,
    classify,
)
from saddle.task_requirements import (
    ProbeTree,
    check_tree,
    load,
    probe_listing,
    run_examples,
    run_probes,
)
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


# -- R-2: which type names refuse, and how a name resolves --------------------

TAKE_TEXT = """# Take

- `take(d)` raises an exception if `d` is empty.
- You may reject `d` with an error if it holds `None`.
"""
TAKE_UNITS = task_units(TAKE_TEXT)
TAKE_GOOD = """\
def take(d):
    if not d:
        raise ValueError("empty")
    return d[next(iter(d))]
"""
# the emptiness check replaced by `pass`: the lookup crashes on `None`
TAKE_BROKEN = TAKE_GOOD.replace('raise ValueError("empty")', "pass").replace(
    "next(iter(d))", "next(iter(d), None)"
)
EXCEPTION = Outcome.of("raises", "Exception")


def probe_raising(*names: str) -> Probe:
    """A known-correct user probe that raised `names[0]` (its classes, most derived first)."""
    return Probe("p" * 64, "ran", Outcome.of("raises", names[0]), source="user", raises=names)


def test_k2_2b_exception_types_nothing_so_a_fallthrough_crash_is_a_question(
    tmp_path: Path,
) -> None:
    probes = (probe_raising("ValueError", "Exception", "BaseException", "object"),)
    e = example("take({})", EXCEPTION, setup=("from ports import take",), probes=probes)
    klass = classify(e, TAKE_UNITS)
    assert (klass.eligible, klass.note) == (False, "no named type")
    broken = gate(module(tmp_path / "b", TAKE_BROKEN), e, units=TAKE_UNITS)
    assert (broken.rows[0].status, broken.rows[0].got) == ("question", "raises KeyError")
    assert broken.detail.endswith("[no named type]")
    # the parent raises the very type the known-correct probe raised
    parent = gate(module(tmp_path / "g", TAKE_GOOD), e, units=TAKE_UNITS)
    assert parent.rows[0].status == "pass"
    # with no probe there is no type to hold it to: never a pass by any crash
    bare = example("take({})", EXCEPTION, setup=("from ports import take",), probes=())
    assert gate(tmp_path / "g", bare, units=TAKE_UNITS).rows[0].status == "question"
    # nor a refusal of a tree that returns
    returns = TAKE_GOOD.replace('raise ValueError("empty")', "return None")
    assert gate(module(tmp_path / "r", returns), e, units=TAKE_UNITS).rows[0].status == "question"


def test_an_untyped_raise_on_a_delegating_unit_is_only_recorded(tmp_path: Path) -> None:
    e = example("take({None: 1})", EXCEPTION, setup=("from ports import take",), units=("S-002",))
    row = gate(module(tmp_path / "t", TAKE_GOOD), e, units=TAKE_UNITS).rows[0]
    assert (row.status, row.why) == (
        "not-judged",
        "recorded: the task leaves this to the implementer",
    )


SUBCLASS = """\
class PortError(ValueError):
    pass


def parse_port(s):
    if not s.isdigit():
        raise PortError(s)
    return int(s)
"""


def test_k2_4_a_subclass_of_the_named_type_passes(tmp_path: Path) -> None:
    e = example('parse_port("abc")', VALUE_ERROR)
    got = on_tree(module(tmp_path / "t", SUBCLASS), e)["E-001"]
    assert got.type_status("ValueError") == "match"
    assert gate(tmp_path / "t", e).rows[0].status == "pass"


def test_k2_5_another_resolvable_type_refuses_and_is_counted_by_type(tmp_path: Path) -> None:
    """The admitted cost of a named type (pin: fa33e45 refused it too): the
    text says ValueError, the tree raises KeyError. Reported on its own."""
    other = PORT_GOOD.replace("raise ValueError(s)", "raise KeyError(s)")
    e = example('parse_port("abc")', VALUE_ERROR)
    check = gate(module(tmp_path / "t", other), e)
    assert (check.rows[0].status, check.rows[0].by_type, check.by_type) == ("code-wrong", True, 1)
    assert "1 code-wrong by exception type" in (check.basis or "")
    # a tree that returns where the raise is due is code-wrong, but not by type
    returns = gate(module(tmp_path / "r", PORT_FALLTHROUGH.replace("s + 1", "0")), e)
    assert (returns.rows[0].status, returns.by_type) == ("code-wrong", 0)
    assert "by exception type" not in (returns.basis or "")


PARSE_TEXT = """# Records

- `read(line)` raises `ParseError` if `line` has no `=`.
"""
PARSE_UNITS = task_units(PARSE_TEXT)
PARSE_ERROR = Outcome.of("raises", "ParseError")
READ_GOOD = """\
class ParseError(ValueError):
    pass


def read(line):
    if "=" not in line:
        raise ParseError(line)
    key, value = line.split("=", 1)
    return {key: value}
"""
READ_BROKEN = READ_GOOD.replace("raise ParseError(line)", "pass").replace(
    'key, value = line.split("=", 1)\n    return {key: value}', "return {None: {}[line]}"
)


def read_example(probes: tuple[Probe, ...] | None = None) -> Example:
    return example('read("x")', PARSE_ERROR, setup=("from records import read",), probes=probes)


def test_k2_9_a_name_the_tree_does_not_define_is_a_question(tmp_path: Path) -> None:
    no_class = READ_GOOD.replace("class ParseError(ValueError):\n    pass\n\n\n", "").replace(
        "raise ParseError(line)", "raise ValueError(line)"
    )
    root = module(tmp_path / "t", no_class, "records")
    row = gate(root, read_example(), units=PARSE_UNITS).rows[0]
    assert (row.status, row.why) == ("question", "named type ParseError not found in the tree")


def test_k2_9b_a_name_in_the_callees_module_resolves_and_refuses_a_fallthrough(
    tmp_path: Path,
) -> None:
    broken = module(tmp_path / "b", READ_BROKEN, "records")
    got = on_tree(broken, read_example())["E-001"]
    assert (got.raises[0], got.type_status("ParseError")) == ("KeyError", "differ")
    assert gate(broken, read_example(), units=PARSE_UNITS).rows[0].status == "code-wrong"
    good = module(tmp_path / "g", READ_GOOD, "records")
    assert gate(good, read_example(), units=PARSE_UNITS).rows[0].status == "pass"


ERRORS = "class ParseError(ValueError):\n    pass\n"
READ_ELSEWHERE = READ_GOOD.replace(
    "class ParseError(ValueError):\n    pass\n\n\n", "import errors\n\n\n"
).replace("raise ParseError(line)", "raise errors.ParseError(line)")


def test_a_name_defined_in_exactly_one_other_module_of_the_tree_resolves(tmp_path: Path) -> None:
    root = module(tmp_path / "t", READ_ELSEWHERE, "records")
    module(root, ERRORS, "errors")
    assert gate(root, read_example(), units=PARSE_UNITS).rows[0].status == "pass"
    # the same class raised from a module the run never loaded cannot be it
    (root / "records.py").write_text(
        READ_ELSEWHERE.replace("raise errors.ParseError", "raise KeyError")
    )
    assert gate(root, read_example(), units=PARSE_UNITS).rows[0].status == "code-wrong"
    # two modules defining it: ambiguous, a question
    module(root, ERRORS, "more_errors")
    row = gate(root, read_example(), units=PARSE_UNITS).rows[0]
    assert (row.status, row.why) == (
        "question",
        "named type ParseError ambiguous: defined in 2 modules of the tree",
    )


def test_a_name_that_is_no_exception_class_is_a_question(tmp_path: Path) -> None:
    rebound = READ_ELSEWHERE.replace("import errors\n", "import errors\n\nParseError = 3\n")
    root = module(tmp_path / "t", rebound, "records")
    module(root, ERRORS, "errors")
    row = gate(root, read_example(), units=PARSE_UNITS).rows[0]
    assert (row.status, row.why) == ("question", "named type ParseError not an exception class")
    builtin = example('parse_port("abc")', Outcome.of("raises", "len"))
    row = gate(module(tmp_path / "p", PORT_GOOD), builtin).rows[0]
    assert (row.status, row.why) == ("question", "named type len not an exception class")


def test_a_reading_the_driver_did_not_resolve_falls_back_to_class_names() -> None:
    """Only a recorded reading can reach `matches` unresolved, and there the
    fallback can only turn a refusal into a question."""
    got = TreeOutcome("raises", raises=("ParseError", "ValueError"), types=(("Other", "differ"),))
    assert got.matches(PARSE_ERROR, 0.0) == "equal"
    assert got.matches(Outcome.of("raises", "Other"), 0.0) == "differ"
    assert TreeOutcome("value").matches(PARSE_ERROR, 0.0) == "differ"


# -- R-1: the raise obligation ---------------------------------------------------

RANGE_GOOD = """\
def parse_port(s):
    n = int(s)
    if n < 1 or n > 65535:
        raise ValueError(s)
    return n
"""
RANGE_BROKEN = RANGE_GOOD.replace("raise ValueError(s)", "pass")
DECIDES = "raises `ValueError` if `s` is not a number from 1 to 65535"


REF = RANGE_GOOD.replace("def parse_port(s):", "def ref(s):")
"""The predictors' mini-reference: the stated rule, written from the text."""


def _reference(s: str) -> object:
    namespace: dict[str, Any] = {}
    exec(REF, namespace)
    return namespace["ref"](s)


def port_client(inputs: list[str], conditions: list[str]) -> Any:
    from test_task_passes import Scripted

    proposal = {
        "inputs": [
            {
                "units": ["S-001"],
                "setup": ["from ports import parse_port"],
                "call": f"parse_port({lit})",
                "args": [lit],
            }
            for lit in inputs
        ],
        "not_executable": [{"unit": "S-002", "reason": "prose-only"}],
        "raise_conditions": [{"unit": "S-001", "conditions": conditions}],
    }

    def outcome(lit: str) -> dict[str, str]:
        try:
            return {"kind": "value", "text": repr(_reference(ast.literal_eval(lit)))}
        except ValueError:
            return {"kind": "raises", "text": "ValueError"}

    def predict(seed: int) -> str:
        return json.dumps(
            {
                "predictions": [
                    {
                        "input": f"E-{i:03d}",
                        "outcome": outcome(lit),
                        "derivation": f"draw {seed}",
                        "decides": DECIDES,
                    }
                    for i, lit in enumerate(inputs, 1)
                ],
                "references": [{"unit": "S-001", "source": REF}],
            }
        )

    return Scripted(proposal, predict=predict)


def port_requirements(tmp_path: Path, inputs: list[str], conditions: list[str]) -> Path:
    from saddle.task_passes import extract

    probe = ProbeTree(module(tmp_path / "probe", RANGE_GOOD), "user")
    record = extract(PORT_TEXT, port_client(inputs, conditions), probes=[probe])
    path = tmp_path / "requirements.json"
    path.write_text(json.dumps(record))
    return path


def audited(tmp_path: Path, broken: str) -> Path:
    """A git repo whose baseline is the correct parser and whose tree is `broken`."""
    root = module(tmp_path / "audited", RANGE_GOOD)
    for args in (("init", "-q", "-b", "main"), ("add", "-A"), ("commit", "-q", "-m", "base")):
        subprocess.run(
            ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
            check=True,
            capture_output=True,
        )
    (root / "ports.py").write_text(broken)
    return root


def test_k2_1_the_obligation_names_the_unit_and_one_input_per_condition_sees_a_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gates, "P1_REFUSAL_LICENSED", True)
    tree = audited(tmp_path, RANGE_BROKEN)
    # first run: in-range inputs only. Nothing raises, and the unit is named
    first = tmp_path / "first"
    first.mkdir()
    check = check_tree(tree, "HEAD", port_requirements(first, ['"80"', '"1"'], []))
    assert check.verdict == "pass"
    assert f"{RAISE_ROW}: S-001" in check.unjudged
    assert "0 of 1 raise-named unit(s) have a raising example" in (check.basis or "")
    # second run: one raising input, the one the deletion does not touch
    second = tmp_path / "second"
    second.mkdir()
    check = check_tree(tree, "HEAD", port_requirements(second, ['"abc"'], ["not a number"]))
    assert check.verdict == "pass"  # the deletion is invisible to "abc" alone
    assert not [u for u in check.unjudged if u.startswith(RAISE_ROW)]
    assert "S-001 1/1" in (check.basis or "")
    # one input per stated condition: "0" sees the deleted range check
    third = tmp_path / "third"
    third.mkdir()
    conditions = ["not a number", "below 1", "above 65535"]
    path = port_requirements(third, ['"abc"', '"0"', '"65536"'], conditions)
    assert load(path).raise_conditions == (("S-001", 3),)
    check = check_tree(tree, "HEAD", path)
    assert check.verdict == "fail"
    assert [r.status for r in check.rows] == ["pass", "code-wrong", "code-wrong"]
    assert check.detail.startswith('S-001 "`parse_port(s)` raises `ValueError`')
    assert 'parse_port("0") expected raises ValueError, got 0; ran changed lines ports.py:' in (
        check.detail
    )
    assert "S-001 3/3" in (check.basis or "")
    # K2-1p, the known-good half: the correct tree passes the same three inputs
    # (written differently: the probe's own bytes would be the probe, never judged)
    (tree / "ports.py").write_text(RANGE_GOOD.replace("n < 1 or n > 65535", "not 1 <= n <= 65535"))
    check = check_tree(tree, "HEAD", path)
    assert (check.verdict, [r.status for r in check.rows]) == ("pass", ["pass"] * 3)


def test_the_propose_prompt_asks_for_one_input_per_raise_condition() -> None:
    from saddle.task_prompts import PROPOSE

    assert "give one input per condition" in PROPOSE
    assert "raise_conditions" in PROPOSE


def test_listed_raise_conditions_keep_only_known_units_and_text() -> None:
    from saddle.task_passes import proposed

    reply = {
        "raise_conditions": [
            {"unit": "S-001", "conditions": ["below 1", 3, "above 65535"]},
            {"unit": "S-999", "conditions": ["unknown unit"]},
            "junk",
            {"unit": "S-001", "conditions": ["not a number"]},
        ]
    }
    got = proposed(reply, PORT_UNITS).raise_conditions
    assert got == {"S-001": ["below 1", "above 65535", "not a number"]}
    assert proposed(None, PORT_UNITS).raise_conditions == {}


TWO_TEXT = """# Two

- `low(x)` raises `ValueError` if `x` is negative.
- `get(k)` raises `KeyError` if `k` is missing.
- You may reject `x` with an error if it is huge.
- `near(x)` returns `x` within rounding error.
"""
TWO_UNITS = task_units(TWO_TEXT)


def bare(eid: str, unit: str, expected: Outcome) -> Example:
    return example("f(1)", expected, units=(unit,), setup=(), eid=eid)


def test_k2_7_the_count_of_raise_named_units_with_a_raising_example_can_vary() -> None:
    assert [(u.id, u.modality) for u in TWO_UNITS.units][2] == ("S-003", "delegating")
    one = [bare("E-001", "S-001", VALUE_ERROR)]
    both = [*one, bare("E-002", "S-002", Outcome.of("raises", "KeyError"))]
    raised = TreeOutcome("raises", raises=("ValueError",), types=(("ValueError", "match"),))
    keyed = TreeOutcome("raises", raises=("KeyError",), types=(("KeyError", "match"),))
    check = check_task_requirements({"E-001": raised}, TWO_UNITS, one, licensed=True)
    assert "1 of 3 raise-named unit(s) have a raising example" in (check.basis or "")
    assert f"{RAISE_ROW}: S-002" in check.unjudged
    check = check_task_requirements(
        {"E-001": raised, "E-002": keyed}, TWO_UNITS, both, licensed=True
    )
    assert "2 of 3 raise-named unit(s) have a raising example" in (check.basis or "")
    assert f"{RAISE_ROW}: S-002" not in check.unjudged
    assert check.verdict == "pass"


def test_k2_7n_a_noise_match_is_a_row_never_a_refusal() -> None:
    """S-004 says "error" and describes no raise: the guard counts it, it gets
    a row, and the count says token matches include such uses."""
    near = bare("E-001", "S-004", Outcome.of("value", "1"))
    check = check_task_requirements(
        {"E-001": TreeOutcome("value", {"t": "int", "v": 1})}, TWO_UNITS, [near], licensed=True
    )
    assert (check.verdict, check.passed) == ("pass", True)
    assert f"{RAISE_ROW}: S-004" in check.unjudged
    assert RAISE_NOISE in (check.basis or "")
    assert (
        "raise conditions listed / raising examples decided: S-001 0/0, S-002 0/0, S-004 0/0"
        in (check.basis or "")
    )


def test_k2_6_a_delegating_raise_unit_never_refuses_and_owes_no_example() -> None:
    e = bare("E-001", "S-003", VALUE_ERROR)
    returned = TreeOutcome("value", {"t": "int", "v": 1}, types=(("ValueError", "differ"),))
    check = check_task_requirements({"E-001": returned}, TWO_UNITS, [e], licensed=True)
    assert (check.rows[0].status, check.passed) == ("not-judged", True)
    assert f"{RAISE_ROW}: S-003" not in check.unjudged


def test_a_p1_that_could_not_run_owes_no_count() -> None:
    check = check_task_requirements({}, TWO_UNITS, [], cannot_run="no file")
    assert "raise-named" not in (check.basis or "")


def test_k2_2_a_task_that_names_no_type_never_refuses_without_a_probe(tmp_path: Path) -> None:
    """Pin: "raises an error" with no type; P-b guesses KeyError; no probe."""
    text = "# T\n\n- `take(d)` raises an error if `d` is empty.\n"
    units = task_units(text)
    e = example(
        "take({})", Outcome.of("raises", "KeyError"), setup=("from ports import take",), probes=()
    )
    crashes = gate(module(tmp_path / "c", TAKE_BROKEN), e, units=units)
    assert crashes.rows[0].status == "pass"  # the crash happens to be the guess
    returns = TAKE_GOOD.replace('raise ValueError("empty")', "return None")
    check = gate(module(tmp_path / "r", returns), e, units=units)
    assert (check.rows[0].status, check.rows[0].why) == ("question", "no known-correct probe")


P1_SOURCES = ("task_prompts", "task_passes", "task_examples", "task_requirements", "task_units")
BENCH_NAMES = ("stockbook", "Inventory", "adjust", "top_n", "FEES", "apply_fee")


def test_p1_holds_no_benchmark_identifier() -> None:
    import saddle

    for name in P1_SOURCES:
        source = (Path(saddle.__file__).parent / f"{name}.py").read_text()
        assert not [b for b in BENCH_NAMES if b in source], name
