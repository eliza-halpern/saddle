# Saddle benchmark gate (ARCHITECTURE.md §6 step 2)

Pre-registered before any comparison run. No moving goalposts: these tasks
and criteria are fixed; if the slice does not win decisively, stop.

## Reference tasks

All three are MECHANICAL (triage-router bypass of Phase 0). Each runs in a
fresh scratch repo with a committed baseline, through guided DAG emission →
Kahn scheduler → Tier-1-gated nodes → proof journal, and each produces a
human-readable transcript plus a verifying journal.

- **T1 — pure function plus test (tiny).** Baseline: empty repo with one
  stub module. Task: add an email-format validator plus pytest coverage.
  Expectation: 1–2 nodes, all gates pass first try.
- **T2 — bounded bug fix (small).** Baseline: a small module with a planted
  off-by-one in a retry loop plus a stale test. Task: fix the loop bound
  and repair the test. Expectation: red-phase flip is genuine (the stale
  test fails pre-change for the right reason).
- **T3 — two-file refactor (medium).** Baseline: duplicated validation
  logic across two modules with passing tests. Task: extract the shared
  helper into its own module, keep the suite green. Expectation: changed-
  line coverage holds across the moved lines; requirement binding names
  both touched requirements.
- **T4 — cross-module discrepancy (long).** Baseline: three modules
  (`orders.py`, `discounts.py`, `invoice.py`) with passing tests, plus a
  planted inconsistency: discounted order totals disagree with invoice
  totals because one shared rounding step rounds at the wrong stage.
  Task: find the root cause across modules, fix it in exactly one
  place, keep the suite green, add a regression test. Expectation: 2–4
  nodes; a fix localized to the wrong module fails its gates instead
  of silently shifting the discrepancy.

## Gate criteria

Each task runs on all three arms — untouched pi, pi-revision, and the
Saddle slice (see M3-1 for arm harnesses and parity controls):

1. **Wall-clock ≤ both pi arms** per task, measured from prompt to verified
   journal (saddle) or to each pi arm's equivalent done signal.
2. **Correctness ≥ both pi arms**: Saddle output must pass its own Tier-1 gates
   and journal verification; a human judge then confirms the change does
   what the task asked. A gate-passing wrong answer fails this criterion.
   Wherever a pi arm passes, saddle must pass.
3. **Zero malformed-packet retries**: guided emission (DAG plans and worker
   diffs) must parse first try — no tolerant re-parsing, no blind retries
   at the same temperature.
4. **Auditability**: every run leaves a verifying journal and a transcript
   a human can read end to end. A run without either fails regardless of
   speed or correctness.

5. **No-progress termination**: a run fails on (A) the same tool +
   normalized args 3x in a row with no working-tree diff change and no
   test-outcome transition between first and third, (B) 10 continuous
   minutes with no progress event (progress = diff change or test
   transition), or (C) 30 min absolute wall-clock, whichever trips first.

## Parity freeze (M3-1)

All arms run local vLLM qwen3.8-27b at thinking level xhigh with a
completion budget of 81920 tokens: saddle passes
`--reasoning-effort xhigh --max-tokens 81920`; untouched pi passes
`--thinking xhigh` (models.json maxTokens 81920); revision roles pin
`thinking: xhigh` with the same budget. A run ending in
finish_reason=length is infra-invalid, not a scored failure: re-run
with a higher budget (operator ceiling ~90k for extreme xhigh
sessions) and discard the truncated attempt.

## Decision rule (regime-aware)

Criterion 1 (wall-clock) is regime-dependent: on the short tasks (T1–T3)
it is diagnostic — recorded per arm but not decisive, since per-call
overhead dominates at that scale. On the long task (T4) it is decisive:
saddle must clock at or under the faster pi arm. Criteria 2–5 bind on
all four tasks.

Pre-registered crossover hypothesis: saddle loses C1 on T1 and wins C1
on T4. Proceed to phased migration (§6 step 3a) only when the crossover
holds, no arm passes a task saddle fails (criterion 2), and criteria
3–5 hold on every run. Otherwise stop at step 2 and harvest (§6
step 3b): guided decoding, tool masking, and reasoning budgets port over
regardless.

*Amendment record (M3-1, before any scored run): arms fixed at three with
parity controls; criterion 5 added (no-progress termination); decision
rule widened 1–4 → 1–5; parity frozen at xhigh thinking with an 81920
completion-token budget per arm plus a truncation-rerun rule. No scored
runs preceded this change.*

*Amendment record (T1 v1 voided as pilot; still before any scored run):
T4 added (long-horizon task); decision rule made regime-aware (T4 C1
decisive, T1–T3 C1 diagnostic, C2–C5 binding everywhere); crossover
hypothesis pre-registered. T1 v1 runs are retained as pilot data only.*
