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
| "Evaluating LLM-Based Test Generation Under Software Evolution" | arXiv 2603.23443 | D6; audit §9 (re-attribution) | >99% of failing single-shot LLM tests passed on the original program while executing the changed region; per-model 40.7–82.9%; the 40.7% model is Nemotron-3-Nano, listed as 30B, so a ~27B model near 41% is an extrapolation | PUBLISHED-WITH-MEASUREMENT. Scope: single-shot tests, not agent loops |
| TEBench | arXiv 2605.06125 | audit §9 corroboration for the test-node hazard (#44, F5) | Identification F1 (locating the affected tests) 45.7–49.4% for seven agent configurations, all on frontier-class models; a heuristic baseline is reported alongside | PUBLISHED-WITH-MEASUREMENT (corroborating only) |
| "Variation in Verification" | arXiv 2509.17995 | §0 reframe (weak generator + independent verifier) | True-negative rate 0.68 → 0.17 in one heatmap cell; a fixed verifier closed 75.7% of the gap in one 181-problem Maths bin; whole-domain 30–50% | CORRECTED: the repo over-generalised one cell and one bin |
| SpecBench | arXiv 2605.21384 | §0; D11; D13 (reward hacking under longer search) | Validation/held-out gap; longer search worsens reward hacking; "weaker models cheat more" | VERIFIED with caveat: no model under ~35B or quantized tested; 27pp is a P90 bound (R²=0.21, v2 §3.1; the v2 abstract says 28pp and Fig. 2's legend "+28pp per 10× LOC, R²=0.25"). ImpossibleBench (arXiv 2510.20270) finds the opposite direction; present both |
| ImpossibleBench | arXiv 2510.20270 | audit §9 counter-evidence to SpecBench | Stronger models cheat more (GPT-5 76%) | PUBLISHED-WITH-MEASUREMENT; cited as the disagreement |
| Property-Generated Solver / "Effective LLM Code Refinement via Property-Oriented and Structurally Minimal Feedback" | arXiv 2506.18315 v1/v2 | D6; D9 (properties, not examples); D17; property-test presence gate (#62) | Property generation is easier than solving: v2: 87.0 vs 63.0 (hard 76.5 vs 32.4) with DeepSeek-R1-32B on 100 LiveCodeBench problems; v1's 82.4 vs 62.4 (Easy split) does not appear in v2 and was not re-verified against v1 | CORRECTED: "83 vs 62" and the LiveCodeBench +4.2–17.4% range were not in the paper. The presence gate as built is SPECULATIVE (presence ≠ soundness) |
| MAKER, "Solving a Million-Step LLM Task with Zero Errors" | arXiv 2511.09030 | D11 (parallel-k), D12, D15 (decomposition granularity); audit §9 | Micro-decomposition plus exact-match voting; k_min = 3 for gpt-4.1-mini, 15 for qwen-3, Θ(ln s); red-flag parser discards over-long responses | PUBLISHED on Towers of Hanoi only; SPECULATIVE for code. CORRECTED: "k=2–3" was wrong |
| CRANE, "Reasoning with Constrained LLM Generation" | arXiv 2502.09061 | D14 (constrain output, never reasoning); the worker diff schema | Alternate constrained and unconstrained segments; strict constrained decoding costs up to 8pp against unconstrained chain-of-thought (Table 1: 13 vs 21, DeepSeek-R1-Distill-Llama-8B; 38 vs 43, QwQ-32B), which CRANE recovers, while against unconstrained decoding without chain-of-thought it is within one point or better on all nine models; TC⁰ result is for finite-output grammars only | CORRECTED: "10pp loss" was CRANE's gain; the 27pp EMNLP figure unverified |
| Snell et al., "Scaling LLM Test-Time Compute Optimally" | arXiv 2408.03314 | D11; best-of-k proposals | Sequential vs parallel compute; compute-optimal allocation beats best-of-N with >4× less compute; sequential revision suits easy questions, and the hardest gain little; √N appears only as an illustrative allocation and a beam width | PUBLISHED. The gate as built was VACUOUS (k=3 at temperature 0.0 is one sample; CLAUDE.md vacuity rule) |
| Weaver, "Shrinking the Generation-Verification Gap with Weak Verifiers" | arXiv 2506.18203 | D12 (evidence independence) | Weighted weak-verifier ensembles; 400M distillation | PUBLISHED (medium confidence per audit). Caveat: the 11.2-point gain over naive averaging uses weights learned from ~50k labelled pairs; the unsupervised model assumes conditionally independent verifiers |
| Olausson et al., "Is Self-Repair a Silver Bullet for Code Generation?" | arXiv 2306.09896 | D16 (repair feedback quality) | Self-repair is bottlenecked by feedback quality (1.58×) | PUBLISHED-WITH-MEASUREMENT |
| Vericoding benchmark | arXiv 2509.22908 | D7 (requirement structure; weak specs get "cheated") | 82/44/27% Dafny/Verus/Lean; cheating is caught by validation, while weak specs admit trivial but valid solutions to a different task (≈9% too weak, ≈15% mistranslated among sampled successes); adding natural-language descriptions did not help | PUBLISHED-WITH-MEASUREMENT |
| traceSDD, "Citation Discipline in Spec-Driven Development" | arXiv 2606.30689 | D7 (REQ citation, orphan detection); requirement binding | REQ citation discipline; orphan detection | PUBLISHED (traceability). The coverage check as built is SPECULATIVE: the planner writes its own REQs, so coverage is tautological |
| "Don't Build Multi-Agents" (Cognition blog) | cognition.com | D13 (composition gate) | Share full context (full agent traces, not just messages); actions carry implicit decisions | Blog; argument, no number |
| VP-Control, "Engineering Reliable Commit Gates for Agentic AI" | arXiv 2609.10969 | D12; D18 (budget toward verification) | Evidence independence worth 3.6× model diversity; 62.9%/22.9%/40.9/11.3 verbatim | VERIFIED verbatim. Scope: non-code data-ops benchmark (n=849), small verifiers run as quantized Ollama checkpoints (Table II's quantization column reads "Q4 K M"), authors disclaim transfer, FinQA reversal. Directional only for code |
| "A Systematic Methodology for Evaluating Failure Independence in LLM-Generated Code" | arXiv 2607.02808 | D11 (forced diversity) | LLM implementations fail in correlated ways | PUBLISHED |
| AlphaCode | arXiv 2203.07814 | D10 (behavioural clustering); best-of-k filtering | Cluster candidates by behaviour on generated inputs; filter by example tests | PUBLISHED for competitive programming; SPECULATIVE for repo code (saddle has no input generator) |
| Six Sigma Agent | arXiv 2601.22290 | D11 | Decomposition plus consensus with an explicit correlation penalty | PUBLISHED |
| AgentPRM | arXiv 2511.08325 | D22 (distil a PRM from the journal) | Process reward over outcome reward | PUBLISHED; research track |
| EndWatch | arXiv 2312.03335 | D1 (HANG distinct from FAIL) | Non-termination detection | PUBLISHED |
| "LLMs versus the Halting Problem" | arXiv 2601.18987 | D1 | Program-termination reasoning limits | PUBLISHED |
| "Detecting Flaky Tests by Controlling Nondeterministic API Behavior" (ChaosAPI) | OOPSLA 2026 | D3 (repeat the baseline before trusting red) | Flake detection via API nondeterminism control | Figure unverifiable per audit; the 3-sample red baseline rests on CI rerun practice instead |
| Context Rot (Chroma) | trychroma.com | D4 (stop maximising node context) | No notable variation across 11 needle positions (on Repeated Words, best near the start); direction only | CORRECTED: the ">30% drop" and "98.6% → 88%" figures are not in the post |
| Martin & Roger, long-context degradation | arXiv 2605.12366 | D4 | Wrong or degraded answers 2× to 30× more often as context fills; recall 98.6% → 88% from prepending 800k benign tokens | PUBLISHED-WITH-MEASUREMENT (re-attributed from Chroma by the audit) |
| Agentless | arXiv 2407.01489 | D2 (patch application); WORKPLAN item at line ~1148 | A deterministic pipeline beats agent loops | PUBLISHED-WITH-MEASUREMENT |
| Diff-XYZ | arXiv 2510.12487 | D2 | Diff-understanding benchmark; patch-format failure modes | PUBLISHED |
| DebugHarness | arXiv 2604.03610 | D2; D16 | Dynamic debugging for repair | PUBLISHED |
| "Why LLMs Fail: Failure Analysis for Automated Security Patch Generation" | arXiv 2603.10072 | D2 | Patch failure taxonomy | PUBLISHED |
| InspectCoder | arXiv 2510.18327 | D16 | Runtime state as repair feedback | PUBLISHED |
| TraceCoder | arXiv 2602.06875 | D16 | Trace-driven multi-agent debugging | PUBLISHED |
| SEDCoT | arXiv 2607.04092 | D17 (minimal failing input) | Minimal counterexamples improve repair without extra LLM calls (§3.4.2); delta debugging still costs executions | PUBLISHED |
| ELFuzz | arXiv 2506.10323 | D9; D21 (coverage-guided input generation) | LLM-driven input synthesis | PUBLISHED; research track |
| HarnessFix | arXiv 2606.06324 v2 | D25 (audit against a harness-flaw taxonomy) | ETCLOVG taxonomy; 6.3–18.4 pp absolute from repairing the harness alone | PUBLISHED-WITH-MEASUREMENT (v2). The audit's correction ("a misread; no human-harness comparison in the paper") was wrong for v2, and the original wording was supported: +11.1% average over the initial harness in 20 settings (§V.A); 6.3 points above human-designed harnesses with GPT-5 mini (range 1.9–10.0; Table IV) |
| ReasoningBank | arXiv 2509.25140 | D23 (journal as experience memory) | Reasoning memory for self-evolving agents | PUBLISHED; research track |
| "Learning When to Remember: Abstention-Aware Memory Retrieval" | arXiv 2604.27283 | D23 | When not to retrieve | PUBLISHED; research track |
| "Quantifying and Mitigating Self-Preference Bias of LLM Judges" | arXiv 2604.22891 | "The tension with No LLM grading" | Self-preference bias measured with authorship unlabeled, on equal-quality pairs; a structured multi-dimensional evaluation strategy (cognitive-load decomposition) cuts it 31.5% on average | CORRECTED: "persists even when authorship is hidden" → "measured with authorship unlabeled" |
| "Automating Invariant Filtering" | Springer 978-3-031-89277-6_4 | D20 (inferred oracles) | LLMs filter inferred invariants | PUBLISHED; research track. Identity only (Crossref: Fischer and Klammer, SWQD 2025, LNBIP pp. 51–71); the text is paywalled, so the claim is not checked against it |
| "Integrating Symbolic Execution with LLMs for Program Specifications" | arXiv 2506.09550 | D20 | Symbolic execution for spec generation | PUBLISHED; research track |

## B. Sources named by the audit as corroborating, not cited in the design

From `docs/AUDIT-2026-09-18.md` §9 "new sources with a use here"; none
changes a Tier 0–2 item. Confidence is the audit's.

| Source | Use |
|---|---|
| MUTGEN, arXiv 2506.02954 v2 (medium) | PITest mutants fed into an LLM test-generation prompt, 204 subjects; mutmut occurs 0 times in v2 and v8; v8: 74% of 50 inspected unfixed failures were wrong oracles; relevant to T2-4's basis field |
| xgrammar-2 blog, 2026-05-04 (high) | Engine roadmap for the constrained decoder |
| aider unified-diffs page (high) | PROVEN-IN-PRODUCTION edit format, for #61 |
| pytest-timeout docs (high) | Per-test hang detection, an alternative to `SHELL_TIMEOUT` |
| Qwen3.8-27B HF README (high) | The worker's documented reasoning-effort semantics |
| Cleverest, arXiv 2501.11086; arXiv 2604.27296, 2604.26102 v2, 2605.08680, 2605.26128, 2604.03616, 2604.24712, 2607.22880, 2512.02304, 2604.14437, 2607.22883 (low–medium) | Corroborating only; not cited in the workplan |

## C. Raised in the 2026-09-25 session, checked the same day

Named from memory while answering "what does the research say about the
saddle arm's 0-for-42 on T1". Each maps to a hypothesis or to a W6 root
cause (`../saddle-notes/parallel/out/W6/report.md` §2). **Status:
UNVERIFIED-FROM-MEMORY for every row as raised.** Check the id, the claim and the
scope against the source before citing in a doc. Each row has since been
checked against the held source (`../saddle-research/reports/Rule D question
rate calibration.md` §5b); the Status column gives the result, the Source
and Claim columns carry the ids and corrections it added, and a CORRECTED
status quotes the recalled wording that was wrong.

| Source | Maps to | Claim | Status (checked 2026-09-25) |
|---|---|---|---|
| Krakovna et al., "Specification gaming" (DeepMind blog, 2020) | RC2 / hypothesis "Goodhart on the gate" | Proxy objectives satisfied in unintended ways | Blog of 21 Apr 2020; the definition matches; about 60 collected examples |
| Skalse et al., "Defining and Characterizing Reward Hacking" (arXiv 2209.13085, 2022) | same | Formalisation of proxy/target divergence | PUBLISHED (NeurIPS 2022): over all stochastic policies, two rewards are unhackable only if one is constant |
| METR, "Recent Frontier Models Are Reward Hacking" (5 Jun 2025) | same | Models tamper with tests and graders when the score is the objective (o3: 39 of 128 RE-Bench runs, 30.4%; 8 of 1,087 HCAST runs, 0.7%) | CORRECTED: "test hard-coding" was wrong (the post has no "hard-cod" and no "system card"); the OpenAI system-card half was not held and is dropped |
| CodeT, Chen et al. (arXiv 2207.10397, 2022) | RC2 | Model-written tests work as a consistency filter, not a binding spec | PUBLISHED-WITH-MEASUREMENT: consensus is scored by solutions × tests (§2.2) and "toxic" tests are measured; HumanEval pass@1 65.8%, +18.8 |
| Petrović & Ivanković, "State of Mutation Testing at Google" (ICSE-SEIP 2018) | RC2 / E1 survivors on guard lines | Mutants on arid nodes (code judged uninteresting to mutate) are suppressed by rules grown from developers' "not useful" feedback; the reported usefulness of surfaced mutants rose from 20% to 80% | CORRECTED: "unproductive", "guard" and "defensive" occur 0 times in the paper; the concept is arid nodes. Id, venue and year hold (doi:10.1145/3183519.3183521) |
| Just et al., "Are Mutants a Valid Substitute for Real Faults?" (FSE 2014) | D8 | Mutation score correlates with real fault detection at suite level | CORRECTED: "not as a per-diff bar" is not in the paper and is dropped. The rest holds: detection correlates with real faults, more strongly than statement coverage for 4 of 5 programs (357 faults, 230,000 mutants; 73% coupled, 17% not) |
| Cemri et al., "Why Do Multi-Agent LLM Systems Fail?" (arXiv 2503.13657, 2025) | RC1 / hypothesis "decomposition destroys information" | Failure taxonomy of 14 modes in 3 categories; system design issues and inter-agent misalignment dominate: 44.2% and 32.3% of 1,642 traces, task verification 23.5% (Fig. 1; Figs. 2 and 4 give other shares) | PUBLISHED-WITH-MEASUREMENT (NeurIPS 2025 Datasets and Benchmarks). The first category's v3 name is "System Design Issues", not "specification" |
| Wang et al., "Self-Consistency" (arXiv 2203.11171, 2022; ICLR 2023) | RC1 (one temperature-0 plan) | Sampling and voting beats one greedy draw | PUBLISHED-WITH-MEASUREMENT: beats greedy chain-of-thought by 17.9 (GSM8K), 11.0 (SVAMP), 12.2 (AQuA), 6.4 (StrategyQA) and 3.9 (ARC-c) points |
| ClarifyGPT, Mu et al. (arXiv 2310.10996, 2023) | The user's principle "asking is not a failure"; M1-H | Code models improve when they detect ambiguity and ask | PUBLISHED-WITH-MEASUREMENT: GPT-4 Pass@1 on MBPP-sanitized 70.96% → 80.80% with real user feedback; with simulated feedback over four benchmarks 68.02% → 75.75% (GPT-4) and 58.55% → 67.22% (ChatGPT). The FSE 2024 version (doi:10.1145/3660810) is from search only, not checked |

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
