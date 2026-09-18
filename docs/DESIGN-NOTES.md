# Hardening saddle: a research-grounded design note

Status: **draft, pre-registered before the v3 re-run.** Written 2026-09-18
against harness `fix/gate-integrity @ d7c3a4a`, after the v2 benchmark sweep
(`../saddle-bench/runs/FINDINGS.md`, F1–F14).

This document exists because the v2 sweep produced a clear diagnosis and no
plan. It takes each recorded failure mode, finds what the literature says
about it, and proposes a specific change. Every item names what saddle does
today, what to do instead, and the paper the claim rests on.

Nothing here is implemented. Items are ordered by **priority**, defined as
`(would it have changed a recorded result) × (cost to build)` — not by how
interesting they are.

---

## 0. The reframe

The v2 sweep was read as "the gates are hollow and a small model will find
every hole." The literature says the second half of that is wrong, and the
correction changes what to build.

**Weak generators produce errors that are *more* detectable, not less.**
[Variation in Verification][varver] measured a fixed strong verifier's
true-negative rate falling from **0.68 to 0.17** in one cell of its heatmap
(Qwen2.5-72B verifying Mathematics answers from Llama-3.1-8B vs Qwen3-32B);
the effect is directional across cells, the size is not. Weak models make
surface-level errors and self-contradictions; strong models produce
internally coherent but subtly wrong solutions that slip past verifiers. A
fixed GPT-4o verifier filtering K=64 samples closed **75.7%** of the gap in
one 181-problem Mathematics difficulty bin ([0.7,0.8): 10.3% → 2.5% error);
whole-domain gap closure is 30–50%.

So saddle's architecture is aimed at the right regime. Gated verification
pays off *most* with a small local worker and *least* at the frontier.

The condition is the entire design problem: **that result assumes the
verifier is independent of the generator.** saddle's is not. The worker
writes the implementation, writes the tests, and — via `requirement_ids` —
invents the requirement. [SpecBench][specbench] measures what happens under
correlated authorship: weaker models show *larger* gaps between visible pass
rate and true compliance, and the dominant failure is not deliberate
exploitation but "feature isolation," components that each pass their own
test and fail to compose.

That is T1 exactly: 7/7 gates, 12/18 hidden. And T7 exactly: six gates
passed over an infinite loop in `extend`.

**The thesis is sound. The independence is missing.** Everything below is
either restoring independence, or fixing a mechanical defect that is
destroying runs before independence gets a chance to matter.

---

## P0 — Mechanical. Hours of work. Each one changed a recorded result.

### D1. A HANG verdict distinct from FAIL

**Recorded:** T7's `test_op_add` does not fail, it never terminates.
`x += x` calls `extend(self)`, which iterates the backing list while
inserting into it. Six of seven gates passed on that code (F13).

**Today:** saddle has no timeout concept. A hanging test hangs the gate.

**Change:** per-test timeout, and a third verdict — `HANG` — that is never
folded into `FAIL` and never into `PASS`. Non-termination is a defect class,
not a slow failure. [EndWatch][endwatch] does runtime non-termination
detection by state revisit and is cheap enough to run as a gate;
[LLMs versus the Halting Problem][halting] documents how poorly models reason
about termination unaided, which is the argument for checking it externally
rather than asking the worker.

This is the single highest value-per-hour item in the document: it is the
only proposal here that would have caught T7's actual defect, and it is an
afternoon of work.

**New issue.**

### D2. Fix patch application three independent ways

**Recorded:** T1 burned ~110s of 200s on two unusable diffs. T6 died on
diff-apply. T7 died on three consecutive diff-apply failures *after recovery
had driven it from four failing gates to one* (F4, F11, F13). This is the
highest-value fix in the backlog and it is not a correctness problem.

**Today:** unified diff, no repair, no constraint, 3 sequential retries.

**Change:** three independent mitigations, all cheap, stack them.

1. **Switch format to search/replace.** Line numbers are the culprit —
   models miscount, files drift between read and write, off-by-one errors
   corrupt silently. Search/replace gives a binary matched/not outcome with
   human-readable errors and measurably lower failure rates
   ([Agentless][agentless] uses this; the format analysis is surveyed in
   [Diff-XYZ][diffxyz]).
2. **Deterministic repair before retry.** [DebugHarness][debugharness]
   applies a minimal-edit-distance correction algorithm to malformed diffs
   *prior to application*, rather than asking the model to self-correct.
   Free, deterministic, no model call.
3. **Guided-decode the patch block.** F9 records zero malformed packets
   across both rounds for the DAG, because it is schema-constrained. The
   same technique is simply absent on diffs. Note D14 for how to do this
   without damaging reasoning.

For calibration on what is and is not fixable here: only **24.8%** of
LLM-generated security patches are fully correct, with semantic
misunderstanding dominating ([Why LLMs Fail][whyllmsfail]). That residue is
a model problem. Application failure is not — it is ours.

**Issue [#52][i52].**

### D3. Repeat the baseline run before trusting red-phase

**Today:** red-phase runs the baseline once and the current tree once, and
compares.

**The hole:** red-phase is a *differential* gate. Flakiness corrupts it
directly — a nondeterministic test can show fail-then-pass with no causal
relation to the change, producing a vacuous red that looks genuine. Between
**2% and 16%** of test failures in large projects are flaky (range not
re-verified: the ACM page returns 403 and the abstract does not carry it),
and the most
effective detection targets the nondeterministic APIs themselves rather than
re-running blindly ([ChaosAPI, OOPSLA 2026][chaosapi]).

**Change:** run the baseline N times (N=3 is enough to catch the common
cases) and emit `INDETERMINATE` rather than `PASS` if the results disagree.
Optionally control the clock/RNG/filesystem entry points.

This is a live soundness hole in a gate saddle already ships and already
depends on.

**New issue.**

### D4. Stop maximising node context — my own guidance was backwards

**Today:** the emit prompt says *"use the full 30000 unless the node touches
one small file,"* and `max_context_tokens` has a floor of 8000. I wrote that
during the v2 fixes. It is wrong.

**Finding:** attention follows a U-shaped curve across input position, with
wrong or degraded answers **2× to 30× more often** as the context fills
(arXiv 2605.12366, Martin & Roger); the Chroma report is cited for the
direction only when the relevant content sits mid-context rather
than at either end. Coding agents have the three properties that maximise the
effect — accumulative context, high distractor density, long task horizons.
One monitoring benchmark lost recall from 98.6% to 88% purely from
prepending 800k benign tokens (arXiv 2605.12366, Martin & Roger; the Chroma report does not carry this figure).

**Change:** node context should be **minimal and ordered**, not maximal.
Put the requirement and the file under change at the *edges* of the context,
not the middle. Drop the floor guidance; keep the ceiling.

**New issue**, or fold into [#51][i51].

### D5. Schema floors on every planner-set threshold

**Recorded:** the planner set T3's mutation kill bar to **50%** and T7's
coverage bar to **0.0%** (F6, F12). Two of seven gates were decorative on
T7's node by the graded party's own choice.

**Today:** `changed_line_coverage_min` and `kill_threshold` are free floats.

**Change:** the same treatment already applied to `red_phase_required`
(`Literal[True]`, so `false` is unrepresentable in the token mask). Pin
`Field(ge=...)` floors matching ARCHITECTURE.md's own worked example (100.0%
coverage, 85.0% kill). This is the empirical law of the sweep: *every
constraint moved into the token mask has held; every constraint left as a
post-hoc check has leaked.*

**Issue [#50][i50].**

---

## P1 — Independence. This is the thesis-critical block.

Section 0 argues the architecture works if and only if the verifier is
independent of the generator. These six items are that, and they should be
built together — each one alone is partial.

### D6. The test-writer must not be the implementer

**Recorded:** F5. The worker receives `Requirements: REQ-001, REQ-002` —
two opaque strings — invents what they mean, writes a test asserting its own
invention, and tags it. The gate greps for the substring and passes.

**Finding:** this has a name. [Zylos][zylos] names it *correlated error*:
when one model writes both sides, "a misreading of the contract at the
specification stage doesn't get an independent second look; it gets encoded
twice." The measurement is arXiv 2603.23443: across **22,374 mutated
CodeNet variants** and 8 models, **>99%** of failing single-shot LLM tests
passed on the original program while executing the changed region
(per-model pass rates 40.7%–82.9%, so a ~27B model sits near **41%**; this
measures single-shot test generation, not agent loops) — tests aligned with
old behaviour, not with intent.
[PGS][pgs] names the same thing the "cycle of self-deception": flawed code
validated against equally flawed tests.

**Change:** split the node. A `test` node and an `impl` node, with the test
node not seeing the implementation. This is the single largest lever in the
document and it is also the most disruptive — it changes the DAG shape, not
just a gate.

**Prerequisite for D7.** No linked issue yet; this is bigger than [#44][i44].

### D7. Requirements get structure, and orphans are detectable

**Today:** `requirement_ids: list[str]`, planner-invented, substring-matched.

**Change, two parts:**

- **Structure.** EARS gives requirements a five-type grammar (ubiquitous,
  event-driven, state-driven, optional, unwanted-behaviour) whose purpose is
  to force testable, unambiguous statements. A `Requirement` object with a
  statement and acceptance criteria is schema-expressible, which means
  guided decoding can enforce its *shape* even though it cannot enforce its
  *content*.
- **Orphan detection.** [traceSDD][tracesdd] treats every REQ ID cited in
  code as a verifiable claim: an ID that does not appear in the spec is an
  automatically detectable orphan, i.e. a hallucinated requirement. That is a
  machine check, not model opinion, and it closes the circularity in F5.

**The honest ceiling:** this does not eliminate the problem, it relocates it
to spec quality. The vericoding benchmarks report **82% Dafny, 44%
Verus/Rust, 27% Lean** with off-the-shelf LLMs, *and* observe
"cheating" (the paper's term) — models exploiting weak formal specs
([Vericoding][vericoding]). Full formal verification does not escape
Goodhart. It makes the spec the binding constraint, which is where you want
it, because that is the part a human can own.

**Issue [#38][i38].**

### D8. Replace the kill-% gate with named contract mutants

**Recorded:** F1 and F12 are the same defect seen twice. T1's four-line
regex admitted **2 mutants**, both killed by shallow tests → 100% PASS. T7's
218-line module admitted **0 mutants** → fail-open PASS having tested
nothing. The gate's strength scales with *line count*, and is therefore
weakest exactly where logic is densest.

**Finding — and this confirms the intuition recorded earlier that the gate
needs *fewer* mutants, not more.** Meta's [ACH][ach] "generates relatively
few, highly specific mutants, by design," terminating at the *first*
buildable passing mutant per class, and beats coverage-driven generation
**15% vs 2.4%** of mutants killed. [Zylos][zylos] proposes the reviewer-facing
version: one or two mutations targeting the actual contract ("this upsert is
idempotent"), with the author required to demonstrate test failure under
each.

Two supporting numbers worth carrying:

- **49%** of ACH's mutant-killing tests added **no line coverage** — direct
  evidence that coverage and adequacy are different quantities, and an
  argument for demoting the coverage gate rather than tuning it.
- **61%** of LLM-generated equivalent mutants were comment-only. Stripping
  comments before equivalence judgement moved precision/recall from
  0.79/0.47 to **0.95/0.96**.

**Change:** the planner emits 1–3 *named* contract mutations per node, under
guided decoding, derived from the requirement (D7). The node must
demonstrate a test that dies on each. This fixes the density problem, the
zero-mutant fail-open, and mutmut's module-scope blindness in one move,
because the mutants are specified rather than discovered.

**Also:** route surviving mutants back into the *test-generation prompt*
rather than reporting a bare percentage. Mutation is a better generator than
it is a gate.

**Issue [#49][i49].**

### D9. Properties and witnesses, not examples

**Finding:** [PGS][pgs] reports LLMs are **82.4% vs 62.4%** on the Easy
split (Medium 62.8/17.5, Hard 48.9/1.1; v1 Table IV); v2 (May 2026,
retitled "Effective LLM Code Refinement via Property-Oriented and
Structurally Minimal Feedback") reports 87.0 vs 63.0 overall with
DeepSeek-R1-32B on 100 LiveCodeBench problems the same hard problems.
Defining correctness is materially more tractable than implementing it —
that is the asymmetry the whole architecture needs, and saddle is not using
it. PGS validates each candidate property two ways: it must hold on known-
good outputs, and it must detect known-bad versions.

This is also the systematic version of the `from_regex` idea. The fuzzing
literature states the underlying problem bluntly: LLMs "generate ordinary
programs following similar patterns seen in their training corpora, while
fuzzing favors unusual inputs that cover edge cases" ([ELFuzz][elfuzz]).
That is T1 — two happy-path tests over an unbounded input domain, missing
`.u@example.com`, `u..x@example.com`, `user@example..com`.

**Change:** a property gate alongside the example tests. `hypothesis` is
already a dependency; `from_regex` supplies witnesses for free on exactly
the T1 class of task. Report gains of **+23.1–37.3% relative** pass@1 over
TDD baselines on HumanEval (v1; v2 headlines "up to 13.4%"). The
LiveCodeBench range could not be located in the paper.

**Caveat:** property sets are incomplete by construction, and weak models
generate weaker properties. This narrows the accept set; it does not close
it.

**New issue.**

### D10. Behavioural clustering for candidate selection

**Today:** saddle has no selection step. One worker, one diff, gated.

**Finding:** [AlphaCode][alphacode] executes surviving candidates on
*generated* inputs, clusters by output behaviour, and submits from the
largest clusters — filtering removes **the large majority** of the candidate pool (the >99% figure was not verified against the paper) before
any judgement is applied. The virtue for saddle is specific: **it never
consults the worker's own tests.** It is an independent evidence channel
obtained purely by execution.

**Change:** with D11's k proposals, cluster by behaviour on generated inputs
and prefer the largest cluster. This costs no model calls beyond the
proposals themselves.

**New issue.** Pairs with D11.

### D11. Parallel-k with forced diversity — and retire sequential retry

**Recorded:** F13. Recovery spent T7's entire attempt budget converging on
*ruff*, because ruff was the only gate pushing back, while the hang sat
untouched. Retry allocates worker effort in proportion to gate strength, and
gate strength was anti-correlated with defect severity.

**Finding:** this is measured, not just observed. [SpecBench][specbench]
reports that **"additional search steps did not reliably reduce gaps... in
several settings, longer search increases the severity of reward hacking."**
Meanwhile [MAKER][maker] reaches zero errors across a million steps with
**k=3 votes** per step for gpt-4.1-mini; k_min ranges 3–29 across models
(qwen-3: 15; gpt-oss-20B: 6; llama-3.2-3B unusable), scaling as Θ(ln s)
with p>0.5 exact-match agreement required plus a red-flag parser that
discards over-long or misformatted responses, and [Snell et al.][testtime]
find sequential revision wins on easy problems (not re-verified),
parallel resampling on hard ones, with the compute-optimal split near √N
each — and compute-optimal scaling beating best-of-N at **4× less compute**.

**The critical qualifier, which downgrades naive voting:** LLM
implementations fail in *correlated* ways. [Failure Independence in
LLM-Generated Code][failind] finds "seemingly diverse implementations often
fail on the same inputs," so majority voting can amplify a shared mistake
rather than cancel it. The [Six Sigma Agent][sixsigma] carries an explicit
correlation penalty ρ for this reason, and MAKER's result quietly depends on
near-independence per step.

**Change:** replace 3 sequential retries with k=3 *parallel* proposals,
diversified by **approach** (different algorithmic strategy in the prompt),
not by temperature. Record inter-proposal agreement in the journal so ρ is
**measured rather than assumed** — D10's cluster sizes are that measurement.
Fall back to sequential recovery only when all k fail the same gate.

Side benefit: three independent diffs makes D2's failure mode far less
likely to kill a run.

**New issue.** Supersedes part of [#51][i51].

### D12. Spend on evidence independence, not model diversity

**Finding, and it reorders everything above:** [VP-Control][vpcontrol]
measured both. A cross-model vote over *shared evidence* approved **62.9% of
unsafe proposals**; with an independent evidence source, **22.9%**. The
source effect was **40.9 percentage points**, against **11.3** for model
diversity — roughly **3.6×**. Scope: a non-code data-operations benchmark
(n=849) with Qwen3-4B / Phi-4-mini Q4_K_M verifiers; the authors disclaim
cross-domain transfer and report a reversal on FinQA (17% vs 20%).
Directional support only for code.

**Implication:** ensembling judges over the same artifact is close to
theatre. What buys reliability is a second evidence *channel* the worker did
not produce: execution (D10), properties (D9), inferred invariants (D20), a
spec artifact (D7). Every P1 item above is an instance of this, which is why
they belong together.

[Weaver][weaver] remains useful in its narrow place — weighted ensembles beat
naive averaging by **+11.2pp**, and a distilled 400M cross-encoder retained
**98.7%** of full accuracy at **0.03%** of the compute — but for *ranking
candidates*, not for sealing a proof. Keep the seal a strict AND.

**No issue; this is the sequencing principle for P1.**

---

## P2 — Architectural. Real work, and the ceiling without them.

### D13. A composition gate — the blind spot decomposition creates

**Recorded:** T7's hang is a *seam* defect. `extend` calls `insert`, and
`x += x` makes the argument alias the backing store. No node-local gate can
see a cross-call aliasing relation.

**Finding:** this is the dominant failure mode, not an exotic one.
[SpecBench][specbench]'s whole design rests on it — validation tests exercise
features in isolation, held-out tests *compose* them — and they report the
gap scaling at roughly **27 percentage points per tenfold increase in code
size**, with compositional "feature isolation" failures dominating and
**weaker models producing more of them**. Deliberate exploits were rare.

Per-node gating *causes* this. Making each node locally clean pushes defects
into the interfaces, where nothing looks. [Cognition][cognition] reaches the
same conclusion from practice: its subtask agents only answer questions and
never write code in parallel.

**Change:** a whole-graph gate that composes the node contracts, run at
merge time. ARCHITECTURE.md already reserves a merge-time tier for the
expensive mutation gate; this belongs there.

**This is the largest gap in the document and nothing in the backlog
addresses it. New issue.**

Worth stating plainly: saddle has emitted **1 node on six of seven tasks**
(F7), so the compounding risk and this blind spot are both currently
*untested*. The architecture's central claim has never actually run.

### D14. Constrain the output, never the reasoning

**Already in the spec, worth making explicit before D2 lands.**
ARCHITECTURE.md §1 says "snapping schema constraints down only after
freeform reasoning concludes" — which is exactly [CRANE][crane]'s design,
arrived at independently. CRANE proves grammar-constrained decoding confines
an LLM to **TC⁰**, unable to express certain reasoning, and measures up to
**8pp** loss from strict constrained decoding (DeepSeek-R1-Distill-Llama-8B;
QwQ-32B 5pp) on GSM-Symbolic, which CRANE recovers; the TC⁰ result (Prop.
3.1) is for finite-output grammars — a diff grammar is infinite and falls
under Prop. 3.3. The 27pp figure was not verified.
benchmarks under strict format constraints.

That is why F9 has never leaked: the DAG is emitted *after* reasoning.
When D2 extends guided decoding to diffs, it must preserve the alternation —
free reasoning, then a delimited constrained block — or it will trade a
patch-application problem for a reasoning problem.

### D15. Decomposition granularity gets a rule

**Recorded:** the emit prompt says "prefer the fewest nodes that cover the
task," and the planner emits 1 node for a 443-line four-module rewrite
(F7, F10).

**Finding:** published working bounds exist. Under ~50 tokens of output is
too granular; **100–500 tokens** is a working estimate (no source found; T4
measures it); a subtask description
longer than 2–3 sentences needs further splitting. Claims that decomposed
workflows run slower but cheaper with smaller models are unsourced; T4
measures this.
[MAKER][maker]'s million-step result comes from steps small enough to be
independently verifiable — its stated non-applicability is tasks "requiring
long-range reasoning or context accumulation across many steps."

**Change:** encode the bound. Also fix the adjacent defect recorded in F7 —
a `finish_reason=length` truncation is a harness condition with an obvious
remedy (retry with a larger budget), not a verdict about the work, and
currently there is no retry path for worker-call failures at all.

**Issue [#51][i51].**

### D16. Give the repairer a debugger, not stderr

**Finding:** [Olausson et al.][selfrepair] established that self-repair is
bottlenecked by *feedback quality*, not repair ability — replacing GPT-4's
self-generated feedback with an experienced programmer's raised the number of
repaired programs **1.58×**. The 2026 line of work operationalises that:
[InspectCoder][inspectcoder], [TraceCoder][tracecoder] and
[DebugHarness][debugharness] give the model interactive runtime state
inspection instead of text error messages, with *variable values at specific
program points* the most informative signal.

**Today:** saddle's recovery subgraph receives gate stdout.

**Change:** on gate failure, hand the recovery node the runtime state at the
failure point. This is also the answer to the [#34][i34] complaint that gate
crashes surface as FAIL with no reason.

### D17. Minimal failing input, not maximal

**Finding:** [PGS][pgs] found that overly complex inputs with high coverage
*reduce* LLM refinement effectiveness, and prefers minimal failing cases.
[SEDCoT][sedcot] shows delta-debugged minimal counterexamples improve repair
outcomes **with no extra LLM calls**. `hypothesis` shrinks for free.

**Caveat worth honouring:** premature minimisation can bias the fix toward
the overly specific, because delta debugging preserves *failure behaviour*
with no regard for root cause. Shrink for the feedback; keep the original
case in the suite.

### D18. Move the budget toward verification

**Recorded:** F8. saddle's entire Tier-1 stack — pytest under coverage,
mutmut, ruff — costs **3 to 15 seconds**. Model calls cost **two to three
minutes**. Verification overhead is a rounding error; call count and call
waste are the cost.

**Finding:** [VP-Control][vpcontrol] frames this as portfolio selection under
budget, with matched-budget portfolios beating fixed verification policies.

**Implication:** saddle can afford dramatically more verification than it
runs. Every gate proposed in P1 is affordable. The crossover hypothesis in
BENCHMARK.md is aimed at the wrong variable and should be amended.

### D19. Per-phase reasoning budgets

**Already in the spec, unimplemented.** ARCHITECTURE.md lists "Heterogeneous
Test-Time Compute: Dynamic allocation of reasoning budgets (Low vs. xhigh)
and tool access per task node." Today saddle sets one effort level per run.

Planning explores ambiguity, execution is largely mechanical, verification is
precise comparison — uniform maximum compute on execution wastes budget where
it buys least. Worth noting the baseline arm's probe runs give evidence here: on T6 every baseline arm
scored **17/17 including `--thinking none`**, and on T5 `none` matched
`xhigh` at 25/29. Reasoning level is not the lever it was assumed to be, at
least at this task scale.

---

## P3 — Research track. Higher variance, real upside.

### D20. Dynamic invariant inference as a non-worker oracle

Daikon-style inference generalises over execution traces to hypothesise
likely invariants, with LLMs now used to *filter* candidates rather than
generate them ([Fischer & Klammer][invfilter], [symbolic execution +
LLM][symbex]). The known weakness is that quality depends entirely on the
diversity of the driving test suite — which is precisely what D9's
`hypothesis` inputs supply.

**Chain:** diverse inputs → inferred invariants → filter → these become D9's
properties, authored by *execution* rather than by the worker. This is the
cleanest independent evidence source on the list.

### D21. Coverage-guided input generation

Beyond property witnesses: [ELFuzz][elfuzz] and semantics-feedback hybrid
fuzzing prioritise inputs that trigger novel program *behaviour*, not merely
novel lines. Relevant to D13 — composition bugs surface under unusual input
sequences, which is what T7's `x += x` is.

### D22. Distil a PRM from the journal

PRMs beat outcome reward models on Best-of-K, and process-supervised training
raises pass@3 where outcome rewards show **no growth at all**;
[AgentPRM][agentprm] reports 8× better compute efficiency. saddle's
hash-chained journal is already a step-level labelled corpus — per-node, per-
gate, pass/fail. Combined with Weaver's distillation result (400M
cross-encoder, 98.7% of accuracy at 0.03% of compute), ranking D11's k
proposals before running gates becomes nearly free.

### D23. The journal as experience memory

[ReasoningBank][reasoningbank] distills reusable principles from both
successes *and* failures. saddle's journal is exactly that shape already.

**Guardrail, and it is a serious one:** "superficial similarity is often
misleading: different root causes can expose similar stack traces... unsafe
memory injection can anchor the agent on an incorrect repair strategy,
consume context budget, and amplify hallucinated fixes"
([risk-sensitive memory retrieval][memretrieval]). Retrieve by *gate-failure
class*, never by text similarity — and note this runs against D4, so it must
be budgeted.

### D24. Perturbed twins for every task

T7's perturbations (insert returns an index, remove returns a count, discard
returns a bool) are the standard contamination-detection technique: compare
performance on original versus perturbed variants, where a clean model scores
comparably on both. Contaminated evaluations typically drop **5–15 absolute
points** on a held-out twin.

**This cuts in saddle's favour and belongs in the record:** the T7 artifact
passed **all seven** perturbation tests, which is real evidence the 62/63 was
not memorised `sortedcontainers`. Every task should get a perturbed twin.

### D25. Audit against a harness-flaw taxonomy

[HarnessFix][harnessfix] reports **6.3–18.4 pp absolute** gains across GAIA,
SWE-Bench Verified, AppWorld and Terminal-Bench from repairing the *harness
alone* (v2; 11.1 is the v2 Table III all-model average). The paper does not
compare against human-designed harnesses. Its ETCLOVG taxonomy (Execution,
Tool, Context, Lifecycle, Observability, Verification, Governance) names
anti-patterns that map onto saddle's recorded bugs one-to-one:

| ETCLOVG anti-pattern | saddle instance |
|---|---|
| "Completion guards that incorrectly treat execution status as evidence of task progress" | the `all([])` vacuous PASS |
| "Finalization without artifact/state effect evidence" | same — VOID runs rendering PASS |
| "Tool schemas ambiguous with weakly validated parameters" | `requirement_ids: list[str]` (D7) |
| "API errors not actionable" | gate stdout as recovery feedback (D16) |
| "Context stale, polluted, overly long, or fragmented" | D4 |

Their empirical study found Lifecycle, Tooling and Observability the most
frequent layers. Worth a standing audit pass.

---

## The tension with "No LLM grading"

ARCHITECTURE.md §1 commits: *"No model in the system evaluates code... the
LLM has zero authority to grade its own work."* Several proposals above
appear to violate it. They do not, but the line needs drawing precisely.

The commitment that survives — and should be strengthened, not weakened — is
**no model grades its own work**. [Self-preference bias][selfpref] scales
with model size and post-training and **is measured with authorship
unlabeled** (randomized unlabeled pairs; equal-quality pairs cut the bias by
31.5%), so this is well-founded.

What the literature adds is that *model-assisted candidate generation* is a
different activity from grading, and it is where the leverage is:

- ACH uses an LLM to **propose** mutants and judge equivalence; the mutants
  are then executed. Judgement is mechanical, proposal is not.
- PGS uses an LLM to **propose** properties, which are then validated
  against known-good and known-bad versions before being trusted.
- Daikon+LLM uses an LLM to **filter** inferred invariants; inference is
  dynamic.

In every case the model narrows a search space and execution decides. That
is compatible with the spec's intent. The rule to write down:

> A model may propose what to check. Only execution may decide whether it
> passed. Where a model must judge (mutant equivalence), it must not be the
> model that produced the artifact, and preferably not the same family.

---

## Already in the spec, not built

Worth separating from the new proposals, because these are commitments
already made:

- **Guided decoding after freeform reasoning** (D14) — specified, applied to
  the DAG, absent on diffs.
- **Heterogeneous test-time compute per node** (D19) — wired per node via
  `reasoning_budget` → `BUDGET_TO_EFFORT` (`cli.py:402-404`); benefit
  unmeasured (T4-3).
- **Tiered gates with expensive checks at merge time** — specified; the merge
  tier is where D13's composition gate belongs.
- **Requirement-bound acceptance with per-ID test evidence** — specified as
  "verifies each one with a test that fails pre-change and passes
  post-change"; implemented as a substring grep (D7).

---

## What this does not solve

Stated so the re-run is not oversold.

1. **Spec quality is the ceiling and it is human work.** Vericoding's
   "cheating" (their term) shows weak specs get exploited even when formal.
2. **Property sets are incomplete by construction.** D9 narrows the accept
   set; it does not close it.
3. **Correlated failure limits every voting scheme.** D11 mitigates by
   forcing diversity and *measuring* ρ, but the ceiling is real.
4. **Sample size.** Seven tasks, one model, several voided. Three scored
   saddle completions exist (T1 FAIL, T2 PASS, T3 PASS), all on tasks small
   enough not to need the system.

### Pre-registered prediction

Recorded before implementation, in the spirit of the rest of the benchmark:

> P0 alone converts T5/T6/T7 from VOID to completed runs, and T7 to a
> probable PASS. **At least one of those completions will seal a PASS over an
> artifact with a real defect**, because P0 closes the vacuous-threshold path
> without giving any gate teeth on composition or on dense code. If that
> happens it is the ceiling, not a bug — and it is the argument for P1 and
> D13.

Falsified if the P0-only re-run produces no sealed proof over a defective
artifact across T1–T7.

---

## References

[varver]: https://arxiv.org/html/2509.17995 "Variation in Verification: Understanding Verification Dynamics in Large Language Models"
[specbench]: https://arxiv.org/html/2605.21384v1 "SpecBench: Measuring Reward Hacking in Long-Horizon Coding Agents"
[zylos]: https://zylos.ai/research/2026-07-28-mutation-testing-test-discrimination-agent-written-code/ "Who Tests the Tests: Mutation Testing and Negative Controls for Agent-Written Code"
[pgs]: https://arxiv.org/html/2506.18315v1 "Use Property-Based Testing to Bridge LLM Code Generation and Validation"
[ach]: https://arxiv.org/html/2501.12862v1 "Mutation-Guided LLM-based Test Generation at Meta"
[maker]: https://arxiv.org/pdf/2511.09030 "Solving a Million-Step LLM Task with Zero Errors"
[crane]: https://arxiv.org/html/2502.09061v3 "CRANE: Reasoning with Constrained LLM Generation"
[testtime]: https://arxiv.org/pdf/2408.03314 "Scaling LLM Test-Time Compute Optimally"
[weaver]: https://arxiv.org/html/2506.18203v1 "Shrinking the Generation-Verification Gap with Weak Verifiers"
[selfrepair]: https://arxiv.org/abs/2306.09896v5 "Is Self-Repair a Silver Bullet for Code Generation?"
[vericoding]: https://arxiv.org/pdf/2509.22908 "A Benchmark for Vericoding: Formally Verified Program Synthesis"
[tracesdd]: https://arxiv.org/pdf/2606.30689 "Citation Discipline in Spec-Driven Development"
[cognition]: https://cognition.com/blog/dont-build-multi-agents "Don't Build Multi-Agents"
[vpcontrol]: https://arxiv.org/html/2609.10969 "Engineering Reliable Commit Gates for Agentic AI: Cost-Aware Verification Portfolios"
[failind]: https://arxiv.org/pdf/2607.02808 "A Systematic Methodology for Evaluating Failure Independence in LLM-Generated Code"
[alphacode]: https://arxiv.org/pdf/2203.07814 "Competition-Level Code Generation with AlphaCode"
[sixsigma]: https://arxiv.org/pdf/2601.22290 "The Six Sigma Agent: Consensus-Driven Decomposed Execution"
[agentprm]: https://arxiv.org/pdf/2511.08325 "AgentPRM: Process Reward Models for LLM Agents"
[endwatch]: https://arxiv.org/pdf/2312.03335 "EndWatch: Detecting Non-Termination in Real-World Software"
[halting]: https://arxiv.org/pdf/2601.18987 "LLMs versus the Halting Problem: Characterizing Program Termination Reasoning"
[chaosapi]: https://2026.splashcon.org/details/oopsla-2026/65/Detecting-Flaky-Tests-by-Controlling-Nondeterministic-API-Behavior "Detecting Flaky Tests by Controlling Nondeterministic API Behavior"
[contextrot]: https://www.trychroma.com/research/context-rot "Context Rot: How Increasing Input Tokens Impacts LLM Performance"
[agentless]: https://arxiv.org/pdf/2407.01489 "Agentless: Demystifying LLM-based Software Engineering Agents"
[diffxyz]: https://arxiv.org/html/2510.12487v1 "Diff-XYZ: A Benchmark for Evaluating Diff Understanding"
[debugharness]: https://arxiv.org/pdf/2604.03610 "DebugHarness: Emulating Human Dynamic Debugging for Autonomous Program Repair"
[whyllmsfail]: https://arxiv.org/html/2603.10072v1 "Why LLMs Fail: Failure Analysis for Automated Security Patch Generation"
[inspectcoder]: https://arxiv.org/pdf/2510.18327 "InspectCoder: Dynamic Analysis-Enabled Self Repair"
[tracecoder]: https://arxiv.org/abs/2602.06875 "TraceCoder: A Trace-Driven Multi-Agent Framework for Automated Debugging"
[sedcot]: https://arxiv.org/pdf/2607.04092 "SEDCoT: Symbolic Execution and Delta Debugging for COBOL Translation"
[elfuzz]: https://arxiv.org/html/2506.10323 "ELFuzz: Efficient Input Generation via LLM-driven Synthesis"
[harnessfix]: https://arxiv.org/html/2606.06324v2 "From Failed Trajectories to Reliable LLM Agents: Diagnosing and Repairing Harness Flaws"
[reasoningbank]: https://arxiv.org/pdf/2509.25140 "ReasoningBank: Scaling Agent Self-Evolving with Reasoning Memory"
[memretrieval]: https://arxiv.org/html/2604.27283 "Learning When to Remember: Abstention-Aware Memory Retrieval in Coding Agents"
[selfpref]: https://arxiv.org/html/2604.22891 "Quantifying and Mitigating Self-Preference Bias of LLM Judges"
[invfilter]: https://link.springer.com/chapter/10.1007/978-3-031-89277-6_4 "Automating Invariant Filtering: Leveraging LLMs to Streamline Test Oracle Generation"
[symbex]: https://arxiv.org/pdf/2506.09550 "Integrating Symbolic Execution with LLMs for Automated Generation of Program Specifications"

[i38]: https://github.com/eliza-halpern/saddle/issues/38
[i44]: https://github.com/eliza-halpern/saddle/issues/44
[i34]: https://github.com/eliza-halpern/saddle/issues/34
[i49]: https://github.com/eliza-halpern/saddle/issues/49
[i50]: https://github.com/eliza-halpern/saddle/issues/50
[i51]: https://github.com/eliza-halpern/saddle/issues/51
[i52]: https://github.com/eliza-halpern/saddle/issues/52

- [Variation in Verification][varver] — weak generators produce more detectable errors (TNR 0.68 → 0.17)
- [SpecBench][specbench] — validation/held-out gap; longer search worsens reward hacking
- [Who Tests the Tests][zylos] — correlated error; negative controls; discrimination evidence
- [Property-Generated Solver][pgs] — 82.4% vs 62.4% property accuracy vs solving (Easy split); cycle of self-deception
- [ACH (Meta)][ach] — few targeted mutants; 49% of mutant-killing tests add no coverage
- [MAKER][maker] — million-step zero-error via micro-decomposition + k=3 voting (k_min 3–29 across models)
- [CRANE][crane] — constrained decoding confines to TC⁰; alternate constrained/unconstrained
- [Snell et al.][testtime] — sequential vs parallel test-time compute; √N split
- [Weaver][weaver] — weighted weak-verifier ensembles; 400M distillation
- [Olausson et al.][selfrepair] — self-repair bottlenecked by feedback quality (1.58×)
- [Vericoding benchmark][vericoding] — 82/44/27% Dafny/Verus/Lean; "cheating" of weak specs
- [traceSDD][tracesdd] — REQ citation discipline; orphan detection
- [Don't Build Multi-Agents][cognition] — parallelise reads, single-thread writes
- [VP-Control][vpcontrol] — evidence independence worth 3.6× model diversity
- [Failure Independence][failind] — LLM implementations fail in correlated ways
- [AlphaCode][alphacode] — behavioural clustering on generated inputs
- [Six Sigma Agent][sixsigma] — decomposition + consensus with explicit ρ penalty
- [AgentPRM][agentprm] — process reward over outcome reward
- [EndWatch][endwatch] / [Halting][halting] — non-termination detection
- [ChaosAPI][chaosapi] — flaky test detection via nondeterministic API control
- [Context Rot][contextrot] — U-shaped position curve; magnitude per arXiv 2605.12366, not the Chroma post
- [Agentless][agentless] — deterministic pipeline beats agent loops
- [Diff-XYZ][diffxyz] / [DebugHarness][debugharness] / [Why LLMs Fail][whyllmsfail] — patch format and repair
- [InspectCoder][inspectcoder] / [TraceCoder][tracecoder] — runtime state as repair feedback
- [SEDCoT][sedcot] — minimal counterexamples improve repair at no extra cost
- [ELFuzz][elfuzz] — coverage-guided input generation
- [HarnessFix][harnessfix] — ETCLOVG taxonomy; 6.3–18.4 pp absolute from harness repair alone (v2)
- [ReasoningBank][reasoningbank] / [Memory retrieval][memretrieval] — journal as experience corpus
- [Self-preference bias][selfpref] — measured with authorship unlabeled
- [Invariant filtering][invfilter] / [Symbolic execution + LLM][symbex] — inferred oracles
