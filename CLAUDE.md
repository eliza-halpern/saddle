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

## A constraint is verified by a known-good instance, never by its existence

Asserting that a constraint is *present* proves nothing about what it
*accepts*. The shipped test for the worker diff schema was:

```python
assert field["pattern"].startswith("^diff --git ")   # BANNED SHAPE
```

That passed for weeks while the constraint it pinned made a working diff
**unrepresentable**: JSON Schema defines `pattern` as an unanchored
partial match, the decoder compiles it as a *full* match, and xgrammar
reduced `^diff --git ` to a closed literal —

```
root_prop_0 ::= (("\"" "d" "i" "f" "f" " " "-" "-" "g" "i" "t" " " "\""))
```

— a grammar for exactly one 11-character string. Every v3 T1 attempt
died on `git apply: No valid patches in input`, identically at
temperature 0.0 and 0.8, because nothing else was legal to emit.

So, for every constraint (schema, pattern, grammar, threshold, enum):

- Assert a **known-good instance passes** and a **known-bad instance
  fails**. Both halves, or the test is decorative.
- Never assert on the constraint's own text or presence. If the only
  thing a test knows is that a string is in a dict, it is pinning the
  bug, not catching it.
- Widening a constraint's *inputs* is part of the test: a diff whose code
  contains `"`, a multi-file diff, a rename. The quote-excluding regex
  candidate looked correct until a diff containing a quote was tried.

## Match the enforcing engine's semantics, not your library's

`jsonschema` would have **passed** the broken pattern, because it
implements the spec (partial match) and the decoder does not (full
match). A local validation that disagrees with the component doing the
enforcing is worse than no test: it reports safety it cannot see.

- Test with the engine's semantics explicitly — `re.fullmatch`, not
  `re.match` — or run the check against the engine itself.
- A whole-value pattern (`^REQ-\d{3}$`) survives full-match compilation.
  A prefix pattern over free-form text does not. Know which you wrote.

## Loosening needs proof the contract was wrong, not that it was failing

A red gate is not a licence to lower the bar. Before weakening any
threshold, constraint or assertion, show that the contract **rejects a
legitimate input** — that it is wrong — not merely that something is
failing against it. "The tests pass now" is not the goal; the tests
exist to be load-bearing.

- Say which direction every contract change moves, in the commit
  message: *tightened*, *loosened*, or *scope narrowed*. Unlabelled
  changes drift loose.
- Never pair a loosening with an unverified compensating tightening.
  `0cd4042` is titled "Make a non-diff worker response unrepresentable,
  then retryable". The loosening (fatal → retryable) was real and
  correct; "unrepresentable" was fiction and cost the whole v3 sweep.
- A claimed guarantee in a commit message is a claim like any other and
  needs the same discrimination evidence as a defect claim.

## A mechanism must be able to do what it reports

Check new machinery for vacuity before trusting its output:

- `k` samples at temperature 0.0 is **one** sample. A retry that resends
  an unchanged prompt at temperature 0.0 reproduces the same bytes.
- A threshold the model itself supplies lets the model set its own bar —
  the `max_mutants=1` exploit (#50), and `kind == "refactor"` selecting
  three gate exemptions (#65).
- If a mechanism reports a count ("3 distinct of 3 samples"), a test must
  show that count is capable of varying.

## Do not edit files during a measurement run

`coverage` and `mutmut` read sources while running. Editing a file
mid-run shifts line numbers and yields a false report — a clean suite
showed 97% and 39 phantom missing lines in `cli.py` purely because a
prompt string was edited while the run was in flight. Let the run finish,
or re-run after the edit settles.

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
