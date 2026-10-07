"""One rule says what is test code, and the sites that ask agree with it (#184, #185).

Several rules told test code from source and disagreed: pytest's collection
patterns alone (`gates._is_test_file`, `impact.is_test_file`), and two near
copies of `gates.is_test_code` that missed `__tests__/` and the `*.spec.*`
spellings (`auditor._test_side`, `task_passes._is_test`). Each disagreement was
a site asking "is this test code?" with the wrong rule: #181 (mutation mutated
a helper), #183 (red-phase left helpers at the baseline) and #184 here: the
deletion check counted every helper under `tests/` and every `conftest.py` as
public API, so a change that dropped a fixture or a helper was refused with
"other modules and later nodes still expect them".

Known-good: removing a helper or a fixture passes the deletion check, a plan
that says to is routed, and a helper's docstring is not asked about. Known-bad:
removing a public source function still fails, a plan that says to is still
caught, and a source function's docstring is still asked about. The sites that
classify a path answer as `is_test_code` does for the same paths.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_auditor import _init
from test_runner import _node

from saddle.auditor import baseline_has_tests, documented_raises_finding
from saddle.evidence import run_argv
from saddle.gates import is_test_code, plan_prescribes_deletion
from saddle.runner import run_node_gate
from saddle.task_passes import baseline_listing

SOURCE = "def f():\n    return 1\n\n\ndef g():\n    return 2\n"
HELPER = "def make():\n    return 1\n\n\ndef spare():\n    return 0\n"
FIXTURES = (
    "import pytest\n\n\n@pytest.fixture\ndef one():\n    return 1\n\n\n"
    "@pytest.fixture\ndef unused():\n    return 0\n"
)
TEST = (
    "from helper import make\n\nfrom n import f\n\n\n"
    "def test_f(one):  # REQ-001\n    assert f() == make() == one\n"
)
BASELINE = {"n.py": SOURCE, "tests/helper.py": HELPER, "tests/conftest.py": FIXTURES}


def deletions(tmp_path: Path, files: dict[str, str]) -> tuple[bool, str]:
    """The public-deletions check of a change that writes `files` over `BASELINE`."""
    root = tmp_path / "tree"
    (root / "tests").mkdir(parents=True)
    _init(root, {**BASELINE, "tests/test_n.py": TEST})
    for name, text in files.items():
        (root / name).write_text(text)
    assert run_argv(["git", "add", "-A"], root) == 0
    result = run_node_gate(_node("python -m pytest tests -q", kind="refactor"), root)
    check = next(c for c in result.checks if c.name == "public-deletions")
    return check.passed, check.detail


def test_removing_a_helper_and_a_fixture_deletes_no_api(tmp_path: Path) -> None:
    passed, detail = deletions(
        tmp_path,
        {
            "tests/helper.py": "def make():\n    return 1\n",
            "tests/conftest.py": "import pytest\n\n\n@pytest.fixture\ndef one():\n    return 1\n",
        },
    )
    assert (passed, detail) == (True, "every public definition the baseline had is still defined")


def test_removing_a_public_source_function_still_deletes_api(tmp_path: Path) -> None:
    passed, detail = deletions(tmp_path, {"n.py": "def f():\n    return 1\n"})
    assert not passed
    assert detail.startswith("n.py no longer defines g;")


def test_a_plan_to_drop_test_code_is_routed_and_one_to_drop_source_api_is_caught() -> None:
    assert plan_prescribes_deletion("Remove spare() from tests/helper.py.", BASELINE) is None
    assert plan_prescribes_deletion("Remove the unused fixture in conftest.py.", BASELINE) is None
    caught = "Remove g() from n.py; no test exercises it."
    assert plan_prescribes_deletion(caught, BASELINE) == caught


RAISES = (
    'def make(n):\n    """Make it.\n\n    Raises:\n        ValueError: when n is negative.\n'
    '    """\n    return n\n'
)


def test_a_helpers_docstring_is_not_asked_about_and_a_sources_still_is(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    (root / "tests").mkdir(parents=True)
    _init(root, {"n.py": RAISES.replace("make", "made"), "tests/helper.py": RAISES})
    (root / "tests" / "helper.py").write_text(RAISES.replace("return n\n", "return n + 0\n"))
    assert documented_raises_finding(root, "HEAD") is None
    made = RAISES.replace("make", "made")
    (root / "n.py").write_text(made.replace("return n\n", "return n + 0\n"))
    asked = documented_raises_finding(root, "HEAD")
    assert asked is not None
    assert asked.verdict == "question"
    assert "`made`" in asked.detail
    assert "`make`" not in asked.detail


PATHS = (
    "tests/helper.py",
    "conftest.py",
    "pkg/tests/fixtures/data.py",
    "web/__tests__/helpers.py",
    "src/test_utils.py",
    "src/app_test.py",
    "src/utils.py",
    "pkg/testing.py",
)


@pytest.mark.parametrize("path", PATHS)
def test_the_baseline_listing_leaves_out_exactly_the_test_code(path: str) -> None:
    listing, _hidden = baseline_listing({path: "def x():\n    return 1\n"}, "")
    assert (f"# {path}" in listing) is not is_test_code(path)


@pytest.mark.parametrize("path", ["web/__tests__/helpers.py", "src/app.spec.ts", "src/utils.py"])
def test_a_baseline_has_tests_when_it_tracks_test_code(tmp_path: Path, path: str) -> None:
    root = tmp_path / "tree"
    (root / Path(path).parent).mkdir(parents=True)
    _init(root, {"README.md": "x\n", path: "x = 1\n"})
    assert baseline_has_tests(root, "HEAD") is is_test_code(path)
