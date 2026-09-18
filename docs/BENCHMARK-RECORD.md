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
per task. Results live in `saddle-bench/runs/`, findings in
`../saddle-bench/runs/FINDINGS.md`.

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

Measured, not speculative — see ../saddle-bench/runs/FINDINGS.md for evidence.

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

Round 2 in progress. #38 and the follow-ups above are prerequisites for a
meaningful saddle result, not production hardening: round 2 measures
saddle *without* them and is the control against which they are judged.
