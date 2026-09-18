# Working on saddle

## Test adequacy: contract mutants, not kill percentage

**Superseded:** the practice from #37 of chasing 100% mutant kill on repo
code. **Current standard:** every contract-bearing change names 1–3
mutations of its own contract and demonstrates a test dies on each.

Coverage is not the bar and neither is a kill percentage:

- 100% line coverage proves a line ran, not that anything could have
  caught it changing. `SHELL_TIMEOUT`'s passthrough sat at 100% coverage
  while a mutant that deleted it survived, because the default happened
  to work in the one path a test exercised.
- Kill percentage scales with *line count*, not logic density. A
  four-line regex admitted 2 mutants in T1 and two shallow tests cleared
  it at 100%; a 218-line module admitted **0** in T7 and the gate passed
  having tested nothing.
- Meta's ACH generates "relatively few, highly specific mutants, by
  design," stopping at the first buildable mutant per class, and kills
  **15%** of mutants against a coverage-driven tool's **2.4%**. **49%**
  of its accepted tests added no line coverage at all
  ([arXiv:2501.12862](https://arxiv.org/html/2501.12862v1)).

So: state the contract in a sentence, mutate *that*, show the test dies.
A surviving contract mutant is a missing test; a surviving arbitrary
mutant may just be an equivalent one.

```bash
# the shape: apply one mutation, run the scoped tests, expect red
sed -i 's/if exit_code == SHELL_TIMEOUT:/if exit_code != SHELL_TIMEOUT:/' src/saddle/gates.py
.venv/bin/python -m pytest tests/test_gates.py -q --no-cov   # must fail
git checkout -- src/saddle/
```

Record the mutants and their verdicts in the commit message.

**Two ways this harness lies, both observed:**

1. **Commit first.** `git checkout -- src/` reverts to HEAD. Mutating
   uncommitted work deletes it, and every later mutant then "dies" on an
   ImportError against code that no longer has the feature.
2. **Verify the mutation applied.** A replacement whose target string is
   absent is a silent no-op and reports SURVIVED. `ruff format` collapsing
   a multi-line call is enough to break an exact match. Abort the run if
   the target is not found rather than printing a verdict.

A SURVIVED that you cannot explain is more likely a broken harness than a
missing test. Check the file actually changed before believing it.

## Defect claims require discrimination evidence

Do not assert a gate is hollow, a bug exists, or a fix works. Show a test
that fails before the change and passes after, and name the command that
reproduces it. "Now it's completely to spec, no more facades" is the
sentence that started this project's rewrite; it was false and nothing in
the repo could have caught it.

## Running the suite

The gate runners shell out to `coverage` and `ruff` by bare name, so the
venv must be on `PATH` or ~39 tests fail with `FileNotFoundError`:

```bash
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest -q
```

## Layering

`dag → gates → evidence → runner → slice`. `gates` holds pure predicates
with runners injected at the boundary; it must not import `evidence` at
runtime (only under `TYPE_CHECKING`). Exit codes the gates *interpret*
live in `gates.py` beside `PYTEST_TESTS_FAILED`; subprocess policy such
as `DEFAULT_TEST_TIMEOUT_S` lives in `evidence.py`.

## Where the reasoning lives

- `docs/ARCHITECTURE.md` — the spec.
- `docs/DESIGN-NOTES.md` — the hardening plan (D1–D25), priority-ordered
  with citations. Read §0 before proposing gate changes.
- `saddle-bench/runs/FINDINGS.md` — F1–F14, what the benchmark actually
  showed. Claims about saddle's behaviour should cite an F-number or a
  run log, not intuition.
