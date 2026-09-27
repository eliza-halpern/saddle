# Contributing to saddle

saddle is a harness whose job is to refuse work that has not been shown to
be correct. The rules below hold the harness itself to the same standard.
Each one exists because the opposite habit let a defect through.

## Test adequacy: contract mutants, not a kill percentage

Every change that carries a contract names one to three mutations of *that
contract* and shows that a test fails on each. Record the mutants and their
verdicts in the commit message.

Neither coverage nor a kill percentage is the bar:

- Full line coverage shows a line ran, not that a test would notice it
  changing. A passthrough of `SHELL_TIMEOUT` was fully covered, yet a
  mutant that deleted it survived: the default value happened to work on
  the one path a test exercised.
- A kill percentage scales with line count, not with how much logic a
  change carries. A four-line regex produced two mutants and two shallow
  tests killed both; a 218-line module produced zero, and a gate reading
  "100% of zero" passed having tested nothing.
- Few, targeted mutants outperform many generic ones. Meta's ACH work
  ("Mutation-Guided LLM-based Test Generation at Meta",
  [arXiv:2501.12862](https://arxiv.org/abs/2501.12862)) generates a small
  number of specific mutants on purpose, kills 15% of them against 2.4% for
  a coverage-driven generator, and found that 277 of its 571
  mutant-killing tests added no line coverage at all.

So: write the contract as one sentence, mutate that sentence's code, and
show the test goes red. A surviving *contract* mutant is a missing test. A
surviving arbitrary mutant may simply be equivalent.

```bash
# apply one mutation, run the scoped tests, expect a failure, restore
sed -i 's/if exit_code == SHELL_TIMEOUT:/if exit_code != SHELL_TIMEOUT:/' src/saddle/gates.py
git diff --stat -- src/saddle/gates.py            # must list the file
.venv/bin/python -m pytest tests/test_gates.py -q --no-cov   # must fail
git checkout -- src/saddle/
find src tests -name __pycache__ -exec rm -rf {} +
```

### Three ways the mutant harness misleads you

1. **Commit before you mutate.** `git checkout -- src/` restores HEAD. If
   the work under test is uncommitted, the first revert deletes it, and
   every later mutant "dies" on an import error against code that no
   longer has the feature.
2. **Confirm the mutation applied.** A substitution whose target text is
   absent changes nothing and reports SURVIVED. A formatter joining a
   multi-line call is enough to break an exact match. Stop the run when the
   target is missing instead of printing a verdict.
3. **Delete bytecode after each revert.** Python trusts a cached `.pyc`
   when the source's size and modification time (one-second resolution)
   match. A mutant that keeps the file's length (for example `A` → `M`) and
   is reverted within the same second leaves a cache that still holds the
   mutated code, so the next run can fail correct source or pass mutated
   source. Remove the `__pycache__` directories after every revert.

A SURVIVED you cannot explain is more likely a broken harness than a
missing test. Check that the file really changed, and that the bytecode is
not older than the source you are reading.

## Defect claims need discrimination evidence

Do not claim that a gate is hollow, that a bug exists, or that a fix works
on the strength of reading the code. Show a test that fails before the
change and passes after it, and give the command that reproduces it. A
confident "this is now fully to spec" that nothing in the repo could check
is exactly the kind of claim this rule exists to stop.

## Six quick checks before you write a claim down

Most wrong claims are checkable in under a minute, usually without running
anything. The commonest error is treating a subset as the whole.

1. **Check the denominator.** Before saying "here are all of them", work
   out how many there should be and compare. `glob.glob("**/*.json",
   recursive=True)` silently skips directories whose names start with a
   dot, so a census of `.saddle/` can read a fraction of the files and
   raise no error. Use `os.walk`, and compare the count with
   `find ... | wc -l`.
2. **Date a count against the code it describes.** A failure count means
   something only for a fixed tree. If the code changed between the runs
   you are pooling, the pooled count measures history and reads repaired
   defects as open ones. Restrict to runs on one revision, or carry the
   revision as a column.
3. **Search before proposing something as new.** Grep the repo, docstrings
   included. An idea proposed as missing is often already written down, or
   already implemented.
4. **Re-read the source before correcting yourself.** A correction is a
   claim with the same burden. "N attempts died on `finish_reason=length`"
   is a misreading of "N attempts recorded a `finish_reason`, all `stop`";
   the second is a true statement, and "correcting" it into the first
   makes it false.
5. **Read the consumer before claiming a dependency.** "Changing the
   worker's response envelope means re-plumbing the gates" is false:
   `changed_lines` comes from `git_diff(workdir, baseline)`, so the gates
   read the tree and never see the envelope. Likewise a surviving mutant may
   be equivalent: `_check_property_oracle` branches only on `total` and
   `killed`, so nothing passed in `survivors` can change its output. Prove
   equivalence by reading the consumer; never assume it.
6. **Run the mutant even when the change looks obviously covered.** A value
   threaded from the runner into the mutation collector once passed the
   whole suite with the threading deleted, because the test `mutmut` stub
   always exits 0 and the branch could not be reached end to end. The fix
   would have shipped inert. Of these six checks, this is the one that most
   often catches a wrong assumption before it becomes a claim.

Cite symbols, not line numbers. Line numbers drift within days; a symbol
either still exists or visibly does not.

## A constraint is verified by instances, never by its presence

Asserting that a constraint *exists* says nothing about what it *accepts*.
A test like this is the banned shape:

```python
assert field["pattern"].startswith("^diff --git ")  # pins the text, not the behaviour
```

A test of exactly that shape once passed while the pattern it pinned made
every working diff impossible to emit. JSON Schema defines `pattern` as an
unanchored partial match, but the constrained decoder compiled it as a
*full* match, and the grammar it produced accepted one 11-character string:

```
root_prop_0 ::= (("\"" "d" "i" "f" "f" " " "-" "-" "g" "i" "t" " " "\""))
```

Every patch the model produced was then corrupt, at any temperature,
because nothing else was legal to generate.

For every constraint (schema, pattern, grammar, threshold, enum):

- Assert that a **known-good instance passes** and a **known-bad instance
  fails**. A test with only one half is decorative.
- Never assert on the constraint's own text or on its presence. A test that
  only knows a string is in a dict pins the bug instead of catching it.
- Widen the inputs as part of the test: a diff whose code contains `"`, a
  diff touching several files, a rename. A candidate regex that excluded
  quotes looked right until someone tried a diff containing a quote.

## Match the enforcing engine's semantics, not your library's

`jsonschema` would have accepted the broken pattern above, because it
implements the specification (partial match) and the decoder does not (full
match). A local check that disagrees with the component doing the enforcing
is worse than no check: it reports a safety it cannot see.

- Test with the engine's semantics explicitly (`re.fullmatch`, not
  `re.match`), or run the check against the engine itself.
- A pattern that describes the whole value (`^REQ-\d{3}$`) survives
  full-match compilation. A prefix pattern over free-form text does not.
  Know which one you wrote.

## Loosening needs proof that the contract was wrong

A failing gate is not permission to lower the bar. Before weakening a
threshold, constraint or assertion, show that the contract **rejects a
legitimate input**, not merely that something fails against it. Tests
passing again is not the goal; the tests exist to carry weight.

- State the direction of every contract change in the commit message:
  *tightened*, *loosened* or *scope narrowed*. Unlabelled changes drift
  loose.
- When a loosening or narrowing admits something new, exhibit it: add a
  test that shows the known-bad case the rule now lets through, so the cost
  is on the record rather than described.
- Never pair a loosening with a compensating tightening you have not
  verified. A commit once announced that a malformed response was made
  "unrepresentable, then retryable". The loosening (fatal to retryable) was
  real and correct; "unrepresentable" was not true, and nothing checked it.
- A guarantee claimed in a commit message is a claim like any other and
  needs the same discrimination evidence.

## A test that flips from red to green is a loosening

A new test that goes red and then green proves it can see a defect. The
reverse edit does not prove anything on its own: an existing assertion
whose expected outcome changes direction, a `pytest.raises` that becomes a
plain call, or an expected-failure fixture that now expects success. The
same diff fits two stories: the old expectation pinned a bug and the fix is
right, or it pinned the contract and the fix broke it.

So a flip carries the loosening rule's burden, in the commit message:

- Label it `flip: <test name>`, with the evidence that the old expectation
  was wrong. "It fails now" is not evidence.
- Show the old expectation pinned a defect rather than the contract: a
  known-good instance the old assertion rejected, or a real run in which it
  let something wrong through. For example, a test can be green only
  because a leftover file from an earlier step is still in the worktree;
  showing a run where the same shape produced a wrong result is the
  evidence, not the fix itself.
- Say what the old assertion was protecting by accident and add a
  red-then-green test for that, or state that it protected nothing. A code
  path covered only by the tests being flipped needs a direct test before
  they change.

A commit that flips a test without such a line is treated as a loosening:
revert the flip or produce the evidence. The worker model cannot do this
at all (`check_node_scope` forbids an implementation node from editing
tests); the rule is for human contributors and anything else that can.

## A mechanism must be able to do what it reports

Check new machinery for vacuity before trusting its output:

- `k` samples at temperature 0.0 are one sample. A retry that resends an
  unchanged prompt at temperature 0.0 reproduces the same bytes.
- A threshold the model itself supplies lets the model set its own bar:
  a planner-chosen `max_mutants=1`, or a node `kind` that selects its own
  gate exemptions.
- If a mechanism reports a count ("3 distinct of 3 samples"), a test must
  show the count can vary.
- A lookup that fails must never read as "nothing found"; report the
  failure.

## Do not edit files during a measurement run

`coverage` and `mutmut` read the sources while they run. Editing a file
mid-run shifts line numbers and produces a false report: a clean suite
once showed dozens of phantom missing lines in one module purely because a
prompt string was edited while the run was in flight. Let the run finish,
or re-run once the edit has settled.

## Running the suite

The gate runners call `coverage` and `ruff` by bare name, so the virtual
environment must be on `PATH`, or a few dozen tests fail with
`FileNotFoundError`:

```bash
uv sync --frozen
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest -q
```

The suite runs at 100% line and branch coverage; `ruff check .` and
`ruff format --check .` must also pass.

## Layering

`dag → gates → evidence → runner → slice`. `gates` holds pure predicates,
with runners injected at the boundary; it must not import `evidence` at
runtime (only under `TYPE_CHECKING`). Exit codes the gates *interpret*
live in `gates.py` beside `PYTEST_TESTS_FAILED`; subprocess policy such as
`DEFAULT_TEST_TIMEOUT_S` lives in `evidence.py`.

## Where the reasoning lives

- `docs/ARCHITECTURE.md`: the specification.
- `docs/DESIGN-NOTES.md`: the hardening plan, priority-ordered with
  citations. Read its §0 before proposing a gate change.
- `docs/RESEARCH.md`: the external sources behind the design, with a
  status label for each.
