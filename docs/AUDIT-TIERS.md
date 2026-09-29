# Audit tiers

The auditor (`src/saddle/auditor.py`, class `Auditor`) runs saddle's 13 gates plus one
extra check, `check_imports`. The checks are split into three tiers by when they run.
Each finding carries a gate, a tier, a verdict (`pass`, `fail`, `not-applicable` or
`blocked`), a reason class, a detail, and `cites` (the function that decided it).

## Reason classes

The reason says what a *failure* of that gate would claim about the change. It is
printed on passing findings too, for example `pass tests [code-wrong]`.

| Reason | Meaning |
|---|---|
| `code-wrong` | the code is broken |
| `evidence-thin` | the code may be right, but the tests do not show it |
| `scope` | the change strayed outside what it was allowed to touch |
| `unknown` | the check could not decide: `blocked`, or the mutation tool failed or did not measure |
| `sanctioned` | an assertion-preservation failure naming only tests the task said to rewrite |

## Tier 0: one file's proposed text

| Check | Reason | Proves |
|---|---|---|
| `syntax` (`gates.check_syntax`) | code-wrong | the file parses |
| `ruff` (`gates.check_ruff`) | code-wrong | no ruff finding *introduced* relative to the baseline copy; format check |
| `imports` (`auditor.check_imports`, not one of the 13) | code-wrong | every absolute top-level import is stdlib, under the repo, `src/` or the file's directory, or installed in saddle's interpreter or in the `python` the tests run on; a root `setup.py`'s imports of its `[build-system] requires` (setuptools when there is no such table) are not looked up, since a build frontend installs them in its own environment; relative imports are counted, not checked |

`saddle audit --tiered` runs all of tier 0 on every changed `.py` file. An autonomous
run does not call `Auditor.tier0`. Its tier-0 guard is in the tools instead
(`tools.ToolContext.guard`): it refuses a write that breaks syntax and refuses test
edits when tests are read-only. Ruff and imports are not checked at edit time.

## Tier 1: per checkpoint

One `runner.run_node_gate(..., tier2=False)` run, which runs the project's suite once:
the red-phase baseline samples are taken at tier 2, the only tier that reports red-phase.

| Gate | Reason | Proves |
|---|---|---|
| `tests` | code-wrong | the test command (`python -m pytest -q`) exits 0 within the project's test time limit (below) |
| `coverage` | evidence-thin | every changed line is run by some test, except lines set aside and counted in `basis`: lines `public-deletions` compels (`compelled-lines=N`), a test module's lines no passing pytest run executes (`exempt-test-lines=N`), and the repository-root `setup.py`, which a build frontend runs and pytest never does (`packaging-lines=N`, also written at the end of the detail as "N packaging lines not judged (root setup.py)"). Logic placed in the root `setup.py` is therefore not coverage-judged; a `setup.py` below the root is judged like any other module |
| `dead-code` | evidence-thin | every private definition added is used elsewhere |
| `public-deletions` | code-wrong | every public definition in the baseline still exists |
| `node-scope` | scope | the diff fits its plan node's kind (`not-applicable` without a plan) |
| `target-scope` | scope | only the plan's target files changed (`not-applicable` without a plan) |
| `assertion-preservation` | evidence-thin | pre-existing tests keep their assertions |

Every run of the test command is bounded by the project's test time limit
(`evidence.suite_limit`): the suite, each red-phase sample, each dead-code rerun, and
the baseline run that checks a sanctioned rewrite. It is 300 s unless the
`pyproject.toml` committed at the baseline sets `[tool.saddle] test-timeout` (seconds;
[CLI.md](CLI.md#the-test-time-limit)). A run past the limit is killed, and the `tests`
finding reads `'python -m pytest -q' hangs: no verdict within the time limit`. The
limit is read at the baseline, never from the tree audited, so a change cannot raise
the limit it is judged under.

The same committed table can set `test-workers = N` (a whole number, 2 or more). Those
runs then use N pytest-xdist workers: the suite runs once as `pytest -n N` under
pytest-cov, and the `tests` and `coverage` findings both read that run, with
`test-workers=N` in the `tests` basis ([CLI.md](CLI.md#running-the-tests-on-workers)).
The verdicts are the serial run's. When pytest-xdist or pytest-cov is missing from the
test environment, or the project's pytest options disable one, the runs are serial and
the `tests` finding ends with the reason, for example `test-workers = 8 set but
pytest-xdist is not installed: ran serially`. Like the limit, the setting is read at the
baseline, never from the tree audited.

When the project's own pytest options start pytest-cov (`--cov` in its `addopts`), the
suite is recorded by that pytest-cov, serially too: under `coverage run` it would take
the tracer over and leave the gate's data empty. Either way no coverage total decides a
run (the gate adds `--cov-fail-under=0`); the `coverage` finding judges the changed
lines.

## Tier 2: before "done"

| Gate | Reason | Proves |
|---|---|---|
| `mutation` | evidence-thin | changed-line mutants are killed; untested mutants count as survivors |
| `property-coverage` | evidence-thin | property tests are paired with an implementation (`not-applicable` without a plan) |
| `red-phase` | evidence-thin | the new tests fail before the change and pass after |
| `requirement-binding` | evidence-thin | declared requirement examples are asserted (`not-applicable` without a plan) |
| `full-suite` | code-wrong | the whole suite passes (the `tests` result of the full run) |

Tier 2 never runs on a tree whose tier 1 failed. It returns a single finding instead:
`mutation blocked [unknown] tier 1 failed (…); tier 2 not run`.

In an autonomous run, tier 1 runs in the background at each checkpoint. Tiers 1 and 2
run in the foreground when the model calls `finish` (`feed.AuditFeed.final`). Tier 2
does not run at checkpoints.

A tier *passes* when every finding is `pass`, `not-applicable` or `sanctioned`. In
arm E+A+F, any other finding refuses `finish`.

## Cache by tree hash

Each verdict is cached under a sha256 of these inputs:

- the tier;
- the tree id (`git write-tree` over the tree, untracked files staged);
- the resolved baseline (which also fixes the test time limit, read from it);
- the test command and the plan node;
- `audit.gate_surface()`: the gate modules' bytes, the tool versions and the Python
  version;
- the sanctioned test list, when there is one.

Tier 0 is keyed by the file's path and a hash of its bytes instead of a tree.

- An identical tree is not gated twice at the same tier. The output says `cached`, not
  `fresh`.
- A cache hit is not journaled again. The finish audit on an unchanged tree reuses the
  checkpoint's tier-1 result.
- `saddle audit --tiered` keeps the cache on disk in `~/.cache/saddle/audit`
  (`--cache`, `--no-cache`). An autonomous run's cache lives in memory only and lasts
  one run (the feed sets no `cache_dir`).
- Upgrading ruff, mutmut or Python, or editing a gate module, changes the key.

## Sanctioned test rewrites

Some tasks legitimately order a test rewritten. For these, pass
`saddle auto --sanctioned-test-rewrite TEST_NAME` (repeatable). The flag affects only
failing `assertion-preservation` findings (`auditor.sanction`):

- If *every* test the finding names is on the list, the verdict stays `fail`. The
  reason becomes `sanctioned` and the detail gains "(all sanctioned by the task)". The
  model sees it as `(info)`, and it neither blocks tier 2 nor refuses `finish`.
- If the finding names any test that is not on the list, it fails as usual.
- The list is sealed in the `auto:start` span and the outcome sidecar, and is part of
  the cache key.

The chat's Run strip has no field for this flag; it is CLI only.
