# M3 benchmark: consolidated record

One place for what was run, what it showed, and what was thrown away.
Replaces ~38 issue comments of run-by-run churn; every one of them is
preserved verbatim in [benchmark-archive.md](benchmark-archive.md).

## What is being tested

Does a decomposition harness (saddle: planner → Kahn scheduler →
Tier-1-gated nodes → hash-chained proof journal) produce better outcomes
than a single agent on the same local model (Qwen3.8-27B via vLLM)?

**Arms.** `saddle` (planner at xhigh, workers sized by the planner) and
`untouched` (stock baseline arm, `-ne -ns -np`, thinking xhigh). A third arm,
`revision` (baseline arm with the orchestrator role and full extensions), was
retired after its only scored run produced no solution in 30 minutes;
an evidence pass on its session showed its advertised gates never
engaged. Unscored baseline arm probes at medium/low/none run alongside.

**Criteria.** C1 wall-clock, C2 correctness against a pre-registered
oracle, C3 zero malformed-packet retries, C4 verifying journal plus
readable transcript, C5 no-progress termination.

## Run history

### Round 1 (retired)

T1 went through five attempts. v1 was voided as a pilot; v2 failed on a
real harness bug (coverage gate always reported 0%, #35); v3 passed; v4
failed on a recovery-call timeout. All of v2–v4 then turned out to have
run **off-spec**: every saddle worker was pinned to xhigh reasoning with
an 81920-token budget via `--worker-effort`/`--max-tokens`, contradicting
ARCHITECTURE.md §2 and §Phase 2, which size mechanical workers at Low
inside a ~30K node ceiling. v5, the first spec-faithful run, passed in
0:57 — against 6–9 minutes for the over-budget attempts.

T2, T3 and T4 followed under the corrected budgets and all passed.

**Why the whole round is retired:** see Harness defects below. Every one
of those runs was gated by a red-phase check that could not fail. Their
PASS verdicts do not mean what they appear to mean. They are kept on the
record, not cited as results.

Two further items from that round, recorded rather than buried:

- **T4's scoring oracle was rewritten after the results were known.** The
  oracle seeded before any arm ran said a fix touching `discounts.py`
  fails C2. Both arms fixed `discounts.py`; the bar was then withdrawn
  mid-run, both arms scored PASS, and the issue was closed. The
  ambiguity argument has merit, but changing a pre-registered oracle
  after seeing outcomes defeats the purpose of pre-registering it. T4 v1
  is disclosed-invalid: not scored, not cited.
- **A runaway process voided a scored run.** The retired revision arm's
  process survived its documented kill and held the GPU for 3.5 hours,
  contending saddle's first v4 attempt into a timeout.

### Round 2 (current)

Re-run from T1 under the fixed harness (`fix/gate-integrity`), five arms
per task. Results and findings live in the author's internal benchmark
notes, which are not public.

### Round 3 (viability sweep, 2026-09-20)

Thirteen saddle-only runs against per-task correctness oracles frozen
before the first run (internal benchmark revision `d9bd23b`), `--sample-temperature 0.7`,
one seed on T1-T4 and three on T5-T7. The full record is in
the internal benchmark notes (not public).

**5 of 13 pass their oracle. 3 of 13 on the strict reading** that also
requires a sealed proof covering the implementation. **Not one of
the nine large-task runs completed**: eight hit the 1800 s wall, the ninth
exhausted its replans.

| task | seeds | oracle PASS | how the runs ended |
|---|---|---|---|
| T1 | 1 | 0/1 | verdict; 22 of 22 gates passed on wrong code (11/18) |
| T2 | 1 | 1/1 | verdict after a replan; 4 attempts lost to lint |
| T3 | 1 | 1/1 | verdict |
| T4 | 1 | 1/1 | verdict |
| T5 | 3 | 0/3 | timeout x3, nothing sealed, worktrees untouched |
| T6 | 3 | 2/3 | timeout x3; the two passes are unproven residue |
| T7 | 3 | 0/3 | timeout x2, verdict x1; no implementation produced |

All four pre-registered predictions but one were falsified; they are
recorded unedited in the internal benchmark notes.

**What round 3 establishes that round 2 could not.** Round 2's verdicts
were void because red-phase could not fail. This round the gates
demonstrably reject work -- nine node-attempt failures across T2, T6 and
T7 -- and T2/T3/T4 correctness was measured for the first time against
oracles nobody could tune afterwards.

**The defect to fix first.** The worker's output cap is
`WORKER_OUTPUT_TOKENS[effort]`, and `effort` comes from the *planner's*
`reasoning_budget`. `propose()` (`cli.py:505-542`) recomputes that same
cap on every retry, so `vllm.py:314`'s "retry with more max_tokens" is
advice no caller acts on -- and the retry lengthens the prompt while
holding the budget fixed. vLLM's histogram shows 7 generations in the
20k-50k band and none above 50k against a nominal 81920, consistent with
`medium = 32768`. T5's three seeds fail identically to the second:
budget-bound, not content-bound. Labelled **harness**, not model ceiling.

**Reading the T6 passes correctly.** t6-s1 and t6-s2 pass every
oracle check on a `filterlang.py` saddle never proved: the sealed proof
covers `tests/test_filterlang.py` alone, and the corrected source is
residue from an impl attempt that failed its gates and was not reverted.
An oracle PASS is not a saddle PASS, and the columns must
stay separate.

Eleven follow-on items, each labelled harness or model-ceiling with its
run log as evidence, are in the internal notes.

### Round 3b (the truncation-ladder measurement, 2026-09-20)

One T5 seed against the node-size pre-flight and truncation ladder
(`9b18dad`), predictions frozen in the internal benchmark notes (not
public) before the run. Full record in the same notes. **TIMEOUT at
1800 s, 0 sealed proofs**, worktree clean at exit.

```
attempt 1/3: 3 gate(s) failed: syntax, tests, red-phase
Attempt 2 of 3: completion truncated at 32624 output tokens
Attempt 3 of 3: completion truncated at 32768 output tokens
```

**The ladder works.** The truncation message names its cap, the
advice no caller acted on is gone, and attempt 3 ran a ladder rung above
attempt 2 — where round 3a's three seeds resent an identical cap forever.

**Two harness defects defeat it**, both solved to the token. The emission
estimate is computed from the *live* worktree, so attempt 1's 699 lines
of failed output inflated attempt 2's budget: `194 + 699 - 6 = 887`
lines, and `16384 + 887*16 + 2048 = 32624`, the observed cap exactly.
Degeneration produces bigger files, bigger files produce a bigger cap.
And that inflated cap landed 144 tokens below the 32768 rung, so the
escalation bought 0.4 % more room.

**The failure underneath is degeneration, not budget.** Attempt 1 did not
truncate; it emitted a complete, applying diff containing 19
byte-identical copies of one test and a 20th cut off mid-signature. The
`syntax` gate caught it correctly. `temperature` is the only sampling
parameter saddle sends — no `repetition_penalty`, `top_p`, `top_k` or
`min_p` appears anywhere in `src/` — and retries drop to temperature 0.0,
the most degeneration-prone setting available.

That reframes round 3a: its truncations were plausibly this same
degeneration meeting a lower ceiling, in which case the ladder converted a
truncation into a syntax error rather than curing the cause. Progress —
the gate now sees the failure and the work reaches the worktree — but not
the cure. Labelled **harness**: the truncations were at a quarter of the
ladder's top, so the honest model-ceiling row the ladder predicted was never
reached.

## Harness defects found and fixed

Three checks reported PASS without verifying their property. All three
were found by execution, not by reading — the code reads correctly in
each case.

1. **Mutation was never wired in.** The gate was emitted in the schema
   and never enforced. Fixed and enforced (#37).
2. **Red-phase could not fail.** The baseline leg ran the gate command
   against a tree that never contained the node's new tests, so a new
   test file produced "file or directory not found" and any nonzero exit
   scored as red. Every greenfield node cleared it vacuously. The planner
   could also waive it per node, and did on every scored run. Fixed: the
   node's own tests are copied over the pre-change tree, both legs run
   the same command, only a genuine failure or an attributable collection
   error counts, and the waiver is pinned to `const: true` on the wire so
   guided decoding cannot emit it (#41, #43).
3. **A journal proving nothing audited as PASS.** The audit verdict used
   `all()` over sealed gate outputs, and `all()` of nothing is true (#48).

Plus: `max_context_tokens` (an input ceiling per §2) was being spent as
the output cap, truncating a worker mid-diff and failing T6 with the
worktree untouched.

## Open structural gaps

Measured, not speculative; the run evidence is in the internal benchmark notes (not public).

- **Gates verify the test run, not the requirement.** T1 round 2: saddle
  passed all seven gates and shipped a validator failing 6 of 18
  behavioural cases; three of four ungated arms scored 18/18.
- **Requirement-binding is circular.** A worker receives
  `Requirements: REQ-001, REQ-002` — opaque strings with no statement —
  invents what they mean, writes the test checking its own invention, and
  the gate greps for the substring. This is #38.
- **The planner sets its own gate thresholds.** One node set its mutation
  kill bar at 50%. Same species as the red-phase waiver.
- **Mutation strength scales with mutants generated.** Four dense lines
  admitted two mutants and reported 100%.

## Status

Round 3 complete (2026-09-20). The gate-integrity work landed: the gates
are now demonstrably capable of failing, and correctness is measured by
oracles frozen before the runs. On small tasks saddle is correct 3 times
in 4 and slower than a single agent; on large tasks it produced a proven
implementation **zero** times in nine attempts.

The crossover hypothesis -- saddle loses on small tasks and wins on large
ones -- remains untested, because the large tasks never reached a verdict.
The planner-set output ceiling is fixed (`9b18dad`) and round 3b confirms the
ladder engages. The blocker moved rather than cleared: the emission
estimate is inflated by failed attempts, the escalation step can be 144
tokens, and the worker call sends no repetition controls at all, so the
model degenerates into a loop and fills whatever budget it is given.
A baseline-arm comparison still has nothing to
compare on the large tasks: across four T5 attempts in two rounds,
saddle has sealed zero proofs.
