# Research sources used in saddle, and how each was used

Collected 2026-09-25 from every citation in `docs/DESIGN-NOTES.md`,
`docs/AUDIT-2026-09-18.md` §9, `docs/WORKPLAN.md` (citation-correction
table and item text), `CLAUDE.md`, and the 2026-09-25 dispatcher session.
Before this file, citations lived only where they were used. Add a row here
whenever a new source is cited anywhere in the repo or the notes.

**Status labels** (the audit's legend, `docs/WORKPLAN.md` §11):
PROVEN-IN-PRODUCTION, PUBLISHED-WITH-MEASUREMENT (number checked against
the source), VERIFIED (this repo's own runs), SPECULATIVE (no number, or a
number the audit could not find). Two extra labels used only here:
**CORRECTED** (the audit found the repo's original quotation wrong and the
WORKPLAN table gives the corrected wording) and **UNVERIFIED-FROM-MEMORY**
(named in conversation, not yet checked against the source; do not cite in
a doc until checked).

Where a source's number was corrected, the corrected wording is the one in
`docs/WORKPLAN.md` "citation-correction" table (rows at lines ~460–488) and
the audit §9 rows; this file summarises, it does not restate them.

## A. Sources behind the design notes (D1–D25)

| Source | Where | Used for | What we took from it | Status / caveat |
|---|---|---|---|---|
| Meta ACH, "Mutation-Guided LLM-based Test Generation at Meta" | arXiv 2501.12862 | CLAUDE.md test-adequacy rule; D8 (contract mutants); WORKPLAN T2-4 basis; audit §9 | Few, specific mutants beat coverage-driven ones: 15% vs 2.4% killed; 277 of 571 mutant-killing tests added no line coverage; a test is accepted only when it kills | PROVEN-IN-PRODUCTION (one deployment, unreplicated). CORRECTED: "49% of accepted tests" was wrong; acceptance (73%) was a separate 191-test sample |
| "Who Tests the Tests: Mutation Testing and Negative Controls for Agent-Written Code" (Zylos blog) | zylos.ai, 2026-07-28 | D6 (test-writer ≠ implementer); D8; "discrimination evidence" doctrine | The name *correlated error* and the negative-control idea | Blog; names the failure only. CORRECTED: the 22,374-variant measurement is arXiv 2603.23443, not Zylos |
| Single-shot LLM test generation on mutated CodeNet programs | arXiv 2603.23443 | D6; audit §9 (re-attribution) | >99% of failing single-shot LLM tests passed on the original program while executing the changed region; per-model 40.7–82.9%, so a ~27B model sits near 41% | PUBLISHED-WITH-MEASUREMENT. Scope: single-shot tests, not agent loops |
| TEBench | arXiv 2605.06125 | audit §9 corroboration for the test-node hazard (#44, F5) | Test-writing F1 45.7–49.4% for ~27B-class models | PUBLISHED-WITH-MEASUREMENT (corroborating only) |
| "Variation in Verification" | arXiv 2509.17995 | §0 reframe (weak generator + independent verifier) | True-negative rate 0.68 → 0.17 in one heatmap cell; a fixed verifier closed 75.7% of the gap in one 181-problem Maths bin; whole-domain 30–50% | CORRECTED: the repo over-generalised one cell and one bin |
| SpecBench | arXiv 2605.21384 | §0; D11; D13 (reward hacking under longer search) | Validation/held-out gap; longer search worsens reward hacking; "weaker models cheat more" | VERIFIED with caveat: no model under ~35B or quantized tested; 27pp is a P90 bound (R²=0.21). ImpossibleBench (arXiv 2510.20270) finds the opposite direction; present both |
| ImpossibleBench | arXiv 2510.20270 | audit §9 counter-evidence to SpecBench | Stronger models cheat more (GPT-5 76%) | PUBLISHED-WITH-MEASUREMENT; cited as the disagreement |
| Property-Generated Solver / "Effective LLM Code Refinement via Property-Oriented and Structurally Minimal Feedback" | arXiv 2506.18315 v1/v2 | D6; D9 (properties, not examples); D17; property-test presence gate (#62) | Property generation is easier than solving: 82.4 vs 62.4 (Easy split, v1); v2: 87.0 vs 63.0 with DeepSeek-R1-32B on 100 LiveCodeBench problems | CORRECTED: "83 vs 62" and the LiveCodeBench +4.2–17.4% range were not in the paper. The presence gate as built is SPECULATIVE (presence ≠ soundness) |
| MAKER, "Solving a Million-Step LLM Task with Zero Errors" | arXiv 2511.09030 | D11 (parallel-k), D12, D15 (decomposition granularity); audit §9 | Micro-decomposition plus exact-match voting; k_min = 3 for gpt-4.1-mini, 15 for qwen-3, Θ(ln s); red-flag parser discards over-long responses | PUBLISHED on Towers of Hanoi only; SPECULATIVE for code. CORRECTED: "k=2–3" was wrong |
| CRANE, "Reasoning with Constrained LLM Generation" | arXiv 2502.09061 | D14 (constrain output, never reasoning); the worker diff schema | Alternate constrained and unconstrained segments; strict constrained decoding costs up to 8pp, which CRANE recovers; TC⁰ result is for finite-output grammars only | CORRECTED: "10pp loss" was CRANE's gain; the 27pp EMNLP figure unverified |
| Snell et al., "Scaling LLM Test-Time Compute Optimally" | arXiv 2408.03314 | D11; best-of-k proposals | Sequential vs parallel compute; √N split | PUBLISHED. The gate as built was VACUOUS (k=3 at temperature 0.0 is one sample; CLAUDE.md vacuity rule) |
| Weaver, "Shrinking the Generation-Verification Gap with Weak Verifiers" | arXiv 2506.18203 | D12 (evidence independence) | Weighted weak-verifier ensembles; 400M distillation | PUBLISHED (medium confidence per audit) |
| Olausson et al., "Is Self-Repair a Silver Bullet for Code Generation?" | arXiv 2306.09896 | D16 (repair feedback quality) | Self-repair is bottlenecked by feedback quality (1.58×) | PUBLISHED-WITH-MEASUREMENT |
| Vericoding benchmark | arXiv 2509.22908 | D7 (requirement structure; weak specs get "cheated") | 82/44/27% Dafny/Verus/Lean; weak specs are gamed | PUBLISHED-WITH-MEASUREMENT |
| traceSDD, "Citation Discipline in Spec-Driven Development" | arXiv 2606.30689 | D7 (REQ citation, orphan detection); requirement binding | REQ citation discipline; orphan detection | PUBLISHED (traceability). The coverage check as built is SPECULATIVE: the planner writes its own REQs, so coverage is tautological |
| "Don't Build Multi-Agents" (Cognition blog) | cognition.com | D13 (composition gate) | Parallelise reads, single-thread writes | Blog; argument, no number |
| VP-Control, "Engineering Reliable Commit Gates for Agentic AI" | arXiv 2609.10969 | D12; D18 (budget toward verification) | Evidence independence worth 3.6× model diversity; 62.9%/22.9%/40.9/11.3 verbatim | VERIFIED verbatim. Scope: non-code data-ops benchmark (n=849), Q4_K_M small verifiers, authors disclaim transfer, FinQA reversal. Directional only for code |
| "A Systematic Methodology for Evaluating Failure Independence in LLM-Generated Code" | arXiv 2607.02808 | D11 (forced diversity) | LLM implementations fail in correlated ways | PUBLISHED |
| AlphaCode | arXiv 2203.07814 | D10 (behavioural clustering); best-of-k filtering | Cluster candidates by behaviour on generated inputs; filter by example tests | PUBLISHED for competitive programming; SPECULATIVE for repo code (saddle has no input generator) |
| Six Sigma Agent | arXiv 2601.22290 | D11 | Decomposition plus consensus with an explicit correlation penalty | PUBLISHED |
| AgentPRM | arXiv 2511.08325 | D22 (distil a PRM from the journal) | Process reward over outcome reward | PUBLISHED; research track |
| EndWatch | arXiv 2312.03335 | D1 (HANG distinct from FAIL) | Non-termination detection | PUBLISHED |
| "LLMs versus the Halting Problem" | arXiv 2601.18987 | D1 | Program-termination reasoning limits | PUBLISHED |
| "Detecting Flaky Tests by Controlling Nondeterministic API Behavior" (ChaosAPI) | OOPSLA 2026 | D3 (repeat the baseline before trusting red) | Flake detection via API nondeterminism control | Figure unverifiable per audit; the 3-sample red baseline rests on CI rerun practice instead |
| Context Rot (Chroma) | trychroma.com | D4 (stop maximising node context) | U-shaped position curve; direction only | CORRECTED: the ">30% drop" and "98.6% → 88%" figures are not in the post |
| Martin & Roger, long-context degradation | arXiv 2605.12366 | D4 | Wrong or degraded answers 2× to 30× more often as context fills; recall 98.6% → 88% from prepending 800k benign tokens | PUBLISHED-WITH-MEASUREMENT (re-attributed from Chroma by the audit) |
| Agentless | arXiv 2407.01489 | D2 (patch application); WORKPLAN item at line ~1148 | A deterministic pipeline beats agent loops | PUBLISHED-WITH-MEASUREMENT |
| Diff-XYZ | arXiv 2510.12487 | D2 | Diff-understanding benchmark; patch-format failure modes | PUBLISHED |
| DebugHarness | arXiv 2604.03610 | D2; D16 | Dynamic debugging for repair | PUBLISHED |
| "Why LLMs Fail: Failure Analysis for Automated Security Patch Generation" | arXiv 2603.10072 | D2 | Patch failure taxonomy | PUBLISHED |
| InspectCoder | arXiv 2510.18327 | D16 | Runtime state as repair feedback | PUBLISHED |
| TraceCoder | arXiv 2602.06875 | D16 | Trace-driven multi-agent debugging | PUBLISHED |
| SEDCoT | arXiv 2607.04092 | D17 (minimal failing input) | Minimal counterexamples improve repair at no extra cost | PUBLISHED |
| ELFuzz | arXiv 2506.10323 | D9; D21 (coverage-guided input generation) | LLM-driven input synthesis | PUBLISHED; research track |
| HarnessFix | arXiv 2606.06324 v2 | D25 (audit against a harness-flaw taxonomy) | ETCLOVG taxonomy; 6.3–18.4 pp absolute from repairing the harness alone | CORRECTED: "+11.1% average, beats human harnesses by 6.3%" was a misread; no human-harness comparison in the paper |
| ReasoningBank | arXiv 2509.25140 | D23 (journal as experience memory) | Reasoning memory for self-evolving agents | PUBLISHED; research track |
| "Learning When to Remember: Abstention-Aware Memory Retrieval" | arXiv 2604.27283 | D23 | When not to retrieve | PUBLISHED; research track |
| "Quantifying and Mitigating Self-Preference Bias of LLM Judges" | arXiv 2604.22891 | "The tension with No LLM grading" | Self-preference bias measured with authorship unlabeled; equal-quality pairs cut it 31.5% | CORRECTED: "persists even when authorship is hidden" → "measured with authorship unlabeled" |
| "Automating Invariant Filtering" | Springer 978-3-031-89277-6_4 | D20 (inferred oracles) | LLMs filter inferred invariants | PUBLISHED; research track |
| "Integrating Symbolic Execution with LLMs for Program Specifications" | arXiv 2506.09550 | D20 | Symbolic execution for spec generation | PUBLISHED; research track |

## B. Sources named by the audit as corroborating, not cited in the design

From `docs/AUDIT-2026-09-18.md` §9 "new sources with a use here"; none
changes a Tier 0–2 item. Confidence is the audit's.

| Source | Use |
|---|---|
| MUTGEN, arXiv 2506.02954 v2 (medium) | LLM-generated mutants vs mutmut; relevant to T2-4's basis field |
| xgrammar-2 blog, 2026-05-04 (high) | Engine roadmap for the constrained decoder |
| aider unified-diffs page (high) | PROVEN-IN-PRODUCTION edit format, for #61 |
| pytest-timeout docs (high) | Per-test hang detection, an alternative to `SHELL_TIMEOUT` |
| Qwen3.8-27B HF README (high) | The worker's documented reasoning-effort semantics |
| Cleverest, arXiv 2501.11086; arXiv 2604.27296, 2604.26102 v2, 2605.08680, 2605.26128, 2604.03616, 2604.24712, 2607.22880, 2512.02304, 2604.14437, 2607.22883 (low–medium) | Corroborating only; not cited in the workplan |

## C. Raised in the 2026-09-25 session, not yet checked

Named from memory while answering "what does the research say about the
saddle arm's 0-for-42 on T1". Each maps to a hypothesis or to a W6 root
cause (`../saddle-notes/parallel/out/W6/report.md` §2). **Status:
UNVERIFIED-FROM-MEMORY for every row.** Check the id, the claim and the
scope against the source before citing in a doc.

| Source (as recalled) | Maps to | Claim as recalled |
|---|---|---|
| Krakovna et al., "Specification gaming" (DeepMind blog, 2020) | RC2 / hypothesis "Goodhart on the gate" | Proxy objectives satisfied in unintended ways |
| Skalse et al., "Defining and Characterizing Reward Hacking" (2022) | same | Formalisation of proxy/target divergence |
| METR and OpenAI system-card reports on test hard-coding (2025) | same | Code models game tests when tests are the objective |
| CodeT, Chen et al. (arXiv 2207.10397, 2022) | RC2 | Model-written tests work as a consistency filter, not a binding spec |
| Petrović & Ivanković, "State of Mutation Testing at Google" (ICSE-SEIP 2018) | RC2 / E1 survivors on guard lines | Mutants on guards and defensive checks are "unproductive"; engineers reject most arbitrary mutants |
| Just et al., "Are Mutants a Valid Substitute for Real Faults?" (FSE 2014) | D8 | Mutation score correlates with real fault detection at suite level, not as a per-diff bar |
| Cemri et al., "Why Do Multi-Agent LLM Systems Fail?" (arXiv 2503.13657, 2025) | RC1 / hypothesis "decomposition destroys information" | Failure taxonomy; specification and inter-agent misalignment dominate |
| Wang et al., "Self-Consistency" (arXiv 2203.11171, 2022) | RC1 (one temperature-0 plan) | Sampling and voting beats one greedy draw |
| ClarifyGPT, Mu et al. (2023) | The user's principle "asking is not a failure"; M1-H | Code models improve when they detect ambiguity and ask |

## D. Internal evidence records (not research, but where claims are checked)

- `../saddle-bench/runs/FINDINGS.md` — F-numbered findings from every benchmark round. Claims about saddle's behaviour cite an F-number or a run log.
- `../saddle-notes/phase1/MEASURE.md` and `runs/CHECKER.md` — pre-registered measurements (hashes in `runs/M1*/PREREG.sha256`) and their verdicts.
- `docs/AUDIT-2026-09-18.md` §9 — the citation audit: 8 claims verified, 6 refuted; the source of every CORRECTED row above.
- `docs/WORKPLAN.md` citation-correction table — the corrected wording for each refuted claim.
- `../saddle-notes/parallel/out/W6/report.md` — root causes RC1–RC3 for the saddle arm's T1/T3 failures, with measured experiments E1/E2 and pre-registered interventions I1–I5.

## Maintenance

- A new citation anywhere in the repo or the notes gets a row here the same day, with a status label.
- A status of PUBLISHED-WITH-MEASUREMENT requires the number to have been checked against the source; otherwise label it SPECULATIVE or UNVERIFIED-FROM-MEMORY.
- Never cite a number from a blog when the paper it summarises is known; cite the paper (the Zylos / 2603.23443 and Chroma / 2605.12366 rows are the two cases where this went wrong).
