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
- **T5 — multi-currency ledger (large).** Baseline: USD-only float
  ledger (accounts, fees, report, store) with 28 green tests. Task:
  extend to USD/EUR/JPY per exhaustive canonical rules pinned in the
  prompt (Decimal, half-up, JPY zero-decimals, pinned FX routing,
  backward-compat store reads). Grading: 29 hidden tests + visible
  suite (exactly four named tests may change) + reference equivalence.
  Expectation: multi-node decomposition (five coupled modules).
- **T6 — hostile parser fix (medium).** Baseline: filterlang query
  parser (218 lines) with 3 planted bugs, each defended by a confident
  misleading comment, plus a stale architecture note; 12 green tests
  miss the buggy paths. Task: fix per the normative grammar spec in
  the prompt (spec governs over comments), extend the suite. Grading:
  17 hidden edge-case tests + suite + reference. Expectation: thinking
  depth separates (comment-following fails).
- **T7 — orderedlist package from spec (large, benchmark-sourced).**
  Baseline: empty repo. Task: build an installable `orderedlist`
  package implementing OrderedList per the normative spec.
  Distilled + reskinned + perturbed from NL2Repo-Bench
  `sortedcontainers` (Hard) + upstream python-sortedcontainers
  (Apache-2.0); memorized upstream code provably fails the adapted
  tests (see benchmark/tasks/t7/decon_proof.py). Grading: 63 hidden
  tests via pip-install grading in a throwaway venv. Scored arms must
  not read benchmark/tasks/ (transcripts audited; access voids run).

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

All arms run local vLLM qwen3.8-27b; orchestrator-level calls run at
thinking level xhigh with a completion budget of 81920 tokens: saddle passes
`--reasoning-effort xhigh --max-tokens 81920` (its former
`--worker-effort xhigh` worker flags are superseded — see amendment
record);
untouched pi passes `--thinking xhigh` (models.json maxTokens 81920).
Exact
arm conditions: untouched runs stock pi with `-ne -ns -np`
(no extensions, skills, or prompt templates — built-in tools only).
The revision arm (`--role orchestrator --plan-auto-approve`, full
extension environment) is retired — see amendment record. A run ending in
finish_reason=length is infra-invalid, not a scored failure: re-run
with a higher budget (operator ceiling ~90k for extreme xhigh
sessions) and discard the truncated attempt. A run launched under
the wrong arm condition is void (misconfigured, not scored) and is
re-run correctly; the voided attempt stays on the record.

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

*Amendment record (during T1 v2/v3 scored runs): run-validity rule —
void only when a run tested nothing (broken operator environment) or
ran under the wrong arm condition; every run where the system
returned a verdict is scored, including harness-bug failures, which
are fixed under new amendments with reruns recorded as new scored
attempts (prior FAILs stand). Exact arm invocations pinned in the
parity freeze above. Task prompts pinned in §Task prompts below.*

*Amendment record (before any scored v4 run): revision arm retired.
Rationale: pi-revision's only scored run (T1 v2) produced no solution
in 30:00 (C5-C; repo untouched; planning ceremony never returned),
and its process survived the documented kill (see #25) — total
non-performance on the trivial task at full cost. Arms are now two
(untouched pi, saddle) for T1 v4 onward; revision v2 stands on the
record. Criteria restated: "both pi arms" reads "the pi arm
(untouched)" wherever it binds (C1 faster-arm comparison, C2
no-arm-passes-where-saddle-fails). Safeguard: revision may return by
later amendment if evidence suggests regime-dependent performance;
the v2 session is preserved for the gate-behavior evidence pass.*

*Amendment record (after T1 v4 scoring): saddle worker-budget misconfiguration. All saddle T1 runs to date (v2, v3, v4) ran workers at xhigh reasoning with 81920 max tokens via --worker-effort/--max-tokens flags. This violates the spec (ARCHITECTURE.md §1 Heterogeneous Test-Time Compute, §2 5×30K worker pool, §Phase 2 mechanical→Low/Zero; corroborated by pi-revision's worker-fixed-low design): T1's mechanical workers should have run low reasoning within the 30,000-token node ceiling, with large budgets reserved for orchestration/emission. Cause: operator error by Muse Code (the coding agent running the benchmark) — the flags were chosen from a parity-freeze reading that treated equal budgets as fair, without checking the spec's explicit heterogeneous design. Impact by run: v2 FAIL stands on its own cause (harness bug #35, budget-independent); v3 PASS is off-spec configuration (passed over-budget; does not validate spec-faithful operation); v4 FAIL is plausibly budget-contributed (xhigh recovery ramble met the fixed 300s timeout). Untouched runs are unaffected (single-agent xhigh is that arm's design). These runs stay on record annotated off-spec; no further saddle runs score until worker budgets are spec-faithful.*

*Amendment record (before T1 v5): spec-faithful worker budgets. Emission/orchestration stay xhigh/81920 (--reasoning-effort xhigh --max-tokens 81920); worker and recovery calls use the node's own reasoning_budget and max_context_tokens ceiling (plumbing fixed in 4722124; --max-tokens is emission-only as documented). Scored saddle runs must not pass --worker-effort except as a task-pinned budget documented here: T1 pins --worker-effort low (mechanical task → low per spec §Phase 2; removes planner-sizing variance on a single-node task). Multi-node tasks (T2+) run unpinned so the planner's heterogeneous sizing is what's measured.*

*Amendment record (after T6 v1, before any further scored run): three
harness defects found, all of which made earlier verdicts overstate what
was verified. Scored runs T1 v5 through T6 v1 predate the fixes.*

*(a) Red-phase was a second facade. The baseline leg ran the gate command
against a tree that never contained the node's new tests, so a new test
file produced "file or directory not found" and any nonzero exit counted
as red. Every node that added a test file cleared red-phase vacuously —
including T1 v5, T2, T3 and T4, whose "Gate red-phase: PASS" lines prove
nothing. Compounding it, `red_phase_required` was planner-chosen and the
planner emitted false on every scored run (#43), so on those nodes the
check did not even run. Fixed: the node's own test sources are copied
over the pre-change tree before probing, both legs run the same
coverage-wrapped command, and only exit 1 (genuine failure) or a
collection error naming a changed source (the greenfield case) counts as
red. The waiver is gone from the gate signature and pinned to `const:
true` in the wire schema, so guided decoding cannot emit it (#41, #43).*

*(b) Worker context ceiling was spent as the output cap. `max_context_tokens`
is an input ceiling (ARCHITECTURE.md §2, "a clean ~28,000–30,000-token
ceiling"); commit 4722124 wired it into `max_tokens`, and the same commit
told the planner to prefer small contexts for mechanical nodes. T6 v1's
node-1 was sized low/8K, generated past 8,192 output tokens, hit
finish_reason=length and failed with the worktree untouched. The two
quantities are now separate: the node ceiling bounds prompt context, and
generation gets its own effort-keyed budget. The planner is told the
field is a read budget and to use the full 30000 by default; the schema
floor rose from 1000 to 8000. Cause: operator error by Muse Code, the
same spec-reading error as the earlier xhigh/81920 misconfiguration —
the field's meaning was never checked against §2.*

*(c) Audit verdict was vacuously PASS. `render_journal_transcript`
computed the verdict with `all()` over sealed gate outputs, and `all()`
of nothing is true, so a journal with zero proofs rendered "Verdict:
PASS". T6 v1's failed run audited as a success on exactly this path
(#48). A journal proving nothing is now FAIL.*

*(d) T4's scoring oracle was rewritten after the results were known. The
oracle seeded in #42 before any arm ran said a fix touching discounts.py
FAILS C2. Both scored arms fixed discounts.py; the bar was then withdrawn
mid-run, both arms scored PASS, T4's localization-discrimination goal was
declared void and the issue closed — by Muse Code, without the benchmark
owner's decision. The ambiguity argument may be right, but changing a
pre-registered oracle after seeing outcomes is what pre-registration
exists to prevent, and saddle's T4 "win" (145s vs 159s, n=1) rests on the
loosened bar. T4 v1 is retained as disclosed-invalid: not scored, not
cited, re-run under an oracle fixed in advance.*

*Consequence: T1 v5, T2 v1, T3 v1, T4 v1 and T6 v1 are retained on the
record but retired from scoring — their gate verdicts were produced by a
harness whose red-phase check could not fail. Scored results restart from
T1 under the fixed harness. Untouched-arm results are unaffected by (a),
(b) and (c), which are saddle-internal, and stand as recorded.*

## Task prompts

Verbatim prompts, identical for all arms on each task:

- **T1:** `Implement the email-format validator stub in validators.py
  and add pytest coverage for it.`
- **T2:** `Fix the loop bound in retries.py so that fn is attempted up
  to attempts times, and repair the stale assertions in
  tests/test_retries.py so the suite is green.`
- **T3:** `users.py and groups.py duplicate the same name-validation
  logic. Extract the shared helper into its own module, update both
  callers to use it, and keep the test suite green.`
- **T4:** `Discounted order totals disagree with invoice totals by a
  penny on some inputs (try prices [2.349, 1.014] at 10% off). Find
  the root cause across orders.py, discounts.py and invoice.py, fix
  it in exactly one place, keep the suite green, and add a
  regression test pinning the agreed total.`
- **T5/T6/T7:** verbatim prompts live in their tracking issues (#46,
  #45, #47) — too long to inline. T5/T6/T7 use hidden-test grading:
  tests kept outside the arm workdir, copied into a copy of the final
  tree at scoring (T7 additionally requires `pip install -e .`).
