# WORKPLAN — saddle, 2026-09-18

Companion to `docs/AUDIT-2026-09-18.md`. Every item below is scoped for an
executing agent that has **not** read this conversation and may be less capable
than the author. Each item names the files and lines it may touch, the contract
in one sentence, the known-good and known-bad instance that pin it, the 1–3
contract mutants that must die, the evidence it rests on, and the observable
"done" condition. Line numbers are as of `fix/gate-integrity` HEAD `3669f9a`
(main `a47c911`). If a line number no longer matches its quoted text, **stop and
report** rather than guess.

Evidence labels (from the audit §8/§9): **PROVEN-IN-PRODUCTION** (shipped
software or industry practice), **PUBLISHED-WITH-MEASUREMENT** (peer-reviewed or
arXiv, with a number the workflow verified), **VERIFIED** (this repo's own run
records or tests), **UNCLASSIFIED**/**SPECULATIVE** (no measurement). Nothing in
Tiers 0–2 rests on a SPECULATIVE claim.

---

## 0. Rules for the executing agent (binding)

These restate `CLAUDE.md` and the project handoff. They are not optional.

1. **Branch and commits.** Work on `fix/gate-integrity`. Plain commits only.
   `amend`, force-push, rebase, new branches and tags each need a fresh,
   explicit ask from the user. One work item per commit. Never commit `.venv/`,
   `mutants/`, coverage artifacts, `*.jsonl` journals, or secrets. End every
   commit message with the attribution trailer the session provides.
2. **Secrets and infrastructure.** The vLLM API key belongs to the user: read it
   from `SADDLE_VLLM_API_KEY` or `VLLM_API_KEY` (see `src/saddle/cli.py:652`),
   never print it, never write it into any file, never search the user's files
   for it. The vLLM container `qwen38-27b-rtx3090-single-1` at
   `http://127.0.0.1:18020` is the user's protected infrastructure: if it is
   down or unhealthy, report and ask; never restart it.
3. **Naming.** The stock single-agent comparison system is called the
   **"baseline arm"** in anything written into this repo, its issues, or its
   commits. Do not write its product name.
4. **Suite.** The gate runners call `coverage` and `ruff` by bare name, so:
   ```bash
   PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest -q
   ```
   Full check (must exit 0 before any Tier ≥1 commit): `./check.sh`.
5. **Contract mutants.** Every contract-bearing change lists 1–3 mutations of
   its own contract in the commit message with the verdict for each. Sequence:
   commit first; apply one mutant with `sed -i`; **confirm the file changed**
   (`git diff --stat` must show the file, else abort); run the scoped tests
   with `--no-cov` and expect red; `git checkout -- src/ tests/`. A mutant whose
   target string is absent is a harness failure, not a SURVIVED.
6. **Direction label.** Every contract change is labelled in the commit
   message as *tightened*, *loosened*, or *scope narrowed*. Loosening needs
   proof the contract rejected a legitimate input.
7. **Known-good and known-bad.** A constraint is pinned by one instance it
   accepts and one it rejects, never by asserting the constraint's text exists.
8. **No edits during measurement runs** (`coverage`, `mutmut`, benchmark
   sweeps).
9. **Scope.** Touch only the files the item lists. If the premise of an item
   turns out to be false (the line is already fixed, the function moved, the
   test already exists), stop and report; do not improvise a larger change.
10. **Issues.** Do not close GitHub issues. When an item resolves one, write
    the proposed closing comment in the commit message body and in the final
    report; the user closes.

## 1. Item template

```
### <ID> — <title>
Files: <path:lines>, ...
Contract: <one sentence>
Direction: tightened | loosened | scope narrowed | docs-only
Evidence: <label>; <citation or repo record>
Issue: #<n> | none
Steps: <numbered, with exact commands>
Known-good: <instance the contract must accept>
Known-bad: <instance the contract must reject>
Contract mutants: <1–3 sed one-liners + expected red test>
Done when: <observable condition>
Stop if: <premise check that, if false, ends the item>
```

## 2. Sequencing

```
T0-1 ──► T1-4 (check.sh green needs the staged file gone)
T1-1, T1-2, T1-3 ──► T1-4
T0-11 ──► T1-4 (check.sh runs `ruff format --check .`, which covers Markdown)
T0-3 (gate names test) ──► T2-2 (extends the same test module)
T2-2a (honest impl fixtures) ──► T2-2 (the tightened check rejects the old fixtures)
T2-1 ──► T2-1b (the seal-count test T2-1 left behind)
T2-1 ──► T4-1 (a benchmark rerun with vacuous k is not worth the GPU time)
T4-4 ──► any future D14 work (deferred)
T4-1 ──► #61 decision
```
Everything in Tier 0 is independent of everything else and can go in any order.

---

## 3. Tier 0 — make the documentation true (docs-only, no contract changes)

### T0-1 — Remove the staged stray test file
Files: `tests/test_recommendations.py` (staged, `A` in `git status`)
Contract: the working tree contains no test module the repo did not author.
Direction: docs-only (tree hygiene)
Evidence: VERIFIED — audit §1: the file is the only source of mypy errors at
lines 31/43 and ruff `E741` at line 35; it tests `RECOMMENDATIONS.md`, an
untracked scratch file.
Issue: none
Steps:
1. `git diff --cached --stat` — confirm the only staged path is
   `tests/test_recommendations.py`. If anything else is staged, stop.
2. `git restore --staged tests/test_recommendations.py && rm tests/test_recommendations.py`
3. Leave `RECOMMENDATIONS.md`, `SUGGESTIONS.md`, `conversation-*.txt`,
   `saddle_logo*`, `.claude/` untouched (untracked, user's).
Done when: `git status --porcelain | grep -c '^A '` prints 0 and
`uv run mypy src tests` reports 6 errors (the remaining dag/evidence/slice ones).
Stop if: the file is tracked at HEAD (`git ls-files tests/test_recommendations.py` non-empty).

### T0-2 — README describes the repository that exists
Files: `README.md:9`, `:14`, `:18`, `:20-25`
Contract: every sentence in README is true of HEAD.
Direction: docs-only
Evidence: VERIFIED — audit §2.1
Issue: none
Steps: replace the four passages:
- L9 `**Status:** pre-implementation. …` → `**Status:** vertical slice built and
  benchmarked (see docs/BENCHMARK-RECORD.md). Tier-1 gates, the Kahn scheduler,
  the proof journal and the CLI exist; Tier-2 merge gates, Phase -1/0 and Phase 4
  are specified in docs/ARCHITECTURE.md and not yet built.`
- L14 `Engine, scheduler, and gates land at the top level …` → a layout list:
  `src/saddle/{dag,gates,evidence,runner,slice,scheduler,journal,vllm,cli}.py`
  with one clause each, in layering order `dag → gates → evidence → runner → slice`,
  plus `transcript.py`, `timeline.py`, `ux.py`, `tools.py`, `chat.py` as
  "reporting and CLI support" (one line, no claims about `chat.py` beyond
  "present; see #23").
- L18 Quickstart → the CLI surface as built: `saddle doctor`, `saddle dag`,
  `saddle run`, `saddle tail`, `saddle verify`, `saddle up`
  (`src/saddle/cli.py:582-641`), the key env vars `SADDLE_VLLM_API_KEY` /
  `VLLM_API_KEY`, and the suite command from rule 4.
- L20 `## Requirements (planned)` → `## Requirements`; L25 `pytest, coverage.py,
  mutmut (Tier-2 sampled)` → `pytest, coverage.py, ruff, mutmut (sampled per node
  in Tier 1; the merge-time Tier 2 is not built)`.
Known-good/known-bad: n/a (prose). Add no claims that the audit did not verify.
Done when: `grep -n "pre-implementation\|(planned)\|land at the top level" README.md` prints nothing.

### T0-3 — Gate count is stated once, correctly, and pinned by a test
Files: `src/saddle/gates.py:1-7` (module docstring), `:365`, `:403`, `:544`
(`"""Run all seven Tier-1 checks…"""`); `docs/ARCHITECTURE.md:159-165`;
`tests/test_gates.py` (new test).
Contract: `run_tier1` runs exactly these ten checks in this order:
`syntax, ruff, tests, coverage, red-phase, node-scope, property-coverage,
assertion-preservation, requirement-binding, mutation`.
Direction: docs-only + tightened (new pin)
Evidence: VERIFIED — `run_tier1` body `src/saddle/gates.py:543-571`; names at
gates.py:93,101,131,157,180,322,378,411,447,517.
Issue: none
Steps:
1. Docstrings: gates.py:1-7 list the ten names in run order; :544 → `"""Run all
   ten Tier-1 checks against `node`'s gate spec and aggregate."""`; :365 and
   :403 `all seven gates` → `every Tier-1 gate then in force` (these are
   historical narrations of T4; do not renumber them to ten).
2. ARCHITECTURE.md:159-165: keep items 1–5, append 6–10 with one line each
   (node-scope, property-coverage, assertion-preservation, sampled mutation
   moved here from Tier 2 — cross-reference §2.3 of the audit — and ruff as
   part of item 1). Mark Tier 2 (L167-171) per T0-7.
3. Test, in `tests/test_gates.py` next to `test_run_tier1_all_green_passes`
   (L294), reusing `_passing_inputs()` (L59):
   ```python
   def test_run_tier1_runs_exactly_the_ten_documented_checks_in_order() -> None:
       names = [check.name for check in run_tier1(_node(), _passing_inputs()).checks]
       assert names == [
           "syntax",
           "ruff",
           "tests",
           "coverage",
           "red-phase",
           "node-scope",
           "property-coverage",
           "assertion-preservation",
           "requirement-binding",
           "mutation",
       ]
   ```
Known-good: the list above. Known-bad: the same list with `"mutation"` removed
must make the test fail (that is mutant 1).
Contract mutants (run `.venv/bin/python -m pytest tests/test_gates.py -q --no-cov` after each):
1. `sed -i '/check_mutation(inputs.mutation, sample.kill_threshold),/d' src/saddle/gates.py` → red (nine checks).
2. Swap order: `sed -i 's/check_syntax(inputs.sources),/check_ruff(inputs.ruff_files, inputs.ruff_runner),/' src/saddle/gates.py` → red (duplicate ruff, no syntax).
Done when: the test passes at HEAD+change, both mutants red, `grep -n "all seven" src/saddle/gates.py` prints nothing.

### T0-4 — ARCHITECTURE.md example node is a valid `Node`
Files: `docs/ARCHITECTURE.md:122-138` (the JSON fence); new `tests/test_docs.py`.
Contract: the first ```json fence in ARCHITECTURE.md validates as `saddle.dag.Node`.
Direction: docs-only + tightened (new pin)
Evidence: VERIFIED — audit §2.7: fence lacks `kind` and `requirements`, uses
`requirement_ids` and `allowed_tools: ["glsl_compiler_check"]` (not in
`RUN_ALLOWLIST`, `cli.py:41`).
Issue: none
Steps:
1. Rewrite the fence as a node that `Node.model_validate` accepts: add
   `"kind": "impl"`, replace `requirement_ids` with
   `"requirements": [{"id": "REQ-014", "statement": "…"}, …]` (shape per
   `tests/test_slice.py:66`), keep `reasoning_budget: "xhigh"`, use only
   `read_file`/`write_file`/`run_tests`/`lint` in `allowed_tools`, keep the
   gate block (`changed_line_coverage_min: 100.0`, `kill_threshold: 85.0`).
2. `tests/test_docs.py`: read `docs/ARCHITECTURE.md`, take the text between
   the first "```json\n" and the next "```", `json.loads` it, then
   `Node.model_validate(obj)`.
Known-good: the rewritten fence. Known-bad: the same object with `kind`
deleted raises `ValidationError` (assert with `pytest.raises`).
Contract mutants:
1. In the doc, delete the `"kind": "impl",` line → test red.
2. In the test, replace `Node.model_validate(obj)` with `dict(obj)` → the
   known-bad half goes green, i.e. the test must fail on its own known-bad
   assertion. (This mutant is on the test; it checks the test is load-bearing.)
Done when: `pytest tests/test_docs.py -q --no-cov` green; mutant 1 red.

### T0-5 — Guided-decoding wording matches what is built
Files: `docs/ARCHITECTURE.md:21`, `:118-119`; `src/saddle/vllm.py:450`
(docstring of `propose_diff`: "guided to one JSON string field").
Contract: the docs say the DAG is emitted under a JSON-schema `structured_outputs`
constraint on the **whole** completion and the diff under an EBNF grammar
(`vllm.py:206`); no "structural tag snap after `<think>`" exists.
Direction: docs-only
Evidence: VERIFIED — audit §2.4; workflow §9: vLLM `structured_outputs.grammar`
is first-class EBNF; `structural_tag` alternation semantics unresolved
(refuted 1-2). D14 stays deferred (T4-4 precedes it).
Steps: replace L21 and L118-119 with two sentences stating the above and
pointing to `vllm.py:206`; change vllm.py:450 to "guided by the EBNF diff
grammar (DIFF_GRAMMAR)". Add `> *Status (2026-09-18): structural-tag emission
is specified, not built; see D14.*` after L119.
Done when: `grep -n "Structural Tag Snap\|dag_schema" docs/ARCHITECTURE.md` shows only lines carrying the status callout.

### T0-6 — Concurrency claims are labelled unmeasured
Files: `docs/ARCHITECTURE.md:150`, `:188`, `:192`; `src/saddle/slice.py:6`;
`src/saddle/ux.py:4`.
Contract: no sentence claims workers overlap; `_run_node` is awaited one at a
time (`slice.py:597-602`) and F7 records no run with >1 node in flight.
Direction: docs-only
Evidence: VERIFIED — audit §2.5, F7.
Steps: after L150 and L188 add `> *Status: the scheduler is asyncio but the
worker is synchronous; no run has overlapped two nodes (F7). Measured by
WORKPLAN T4-2.*`; L192 "five concurrent workers" → "up to five workers
(concurrency unmeasured, T4-2)"; slice.py:6 and ux.py:4 reword to say the
slice runs one node at a time.
Done when: `grep -n "concurrent workers" docs/ARCHITECTURE.md` prints only the reworded line.

### T0-7 — "Specified, not built" callouts
Files: `docs/ARCHITECTURE.md` at: Phase -1/0 sections, `:143` (Rolling Wave),
`:153` (Tool Masking), `:167-171` (Tier 2), Phase 4 section, `:200` (stall),
and the resume paragraph if any.
Contract: every mechanism the audit §4 lists as unbuilt carries a one-line
`> *Status (2026-09-18): specified, not built — <pointer>*` callout directly
under its heading or bullet.
Direction: docs-only
Evidence: VERIFIED — audit §2.6, §4.
Steps: insert the callout in the seven places; pointers: Tier 2 → `#60`,
WORKPLAN T2-3; resume → `slice.py:584-587`, T3-1; tool masking →
`cli.py:41` (`RUN_ALLOWLIST` is consumed by nothing), T3-4; Phase -1/0,
rolling wave, stall, Phase 4 → "deferred, WORKPLAN §7".
Done when: `grep -c "specified, not built" docs/ARCHITECTURE.md` prints 7.

### T0-8 — Path and wording errors
Files: `CLAUDE.md:79`, `:165`; `docs/DESIGN-NOTES.md:5`, `:631`;
`docs/BENCHMARK-RECORD.md:61`, `:89`.
Anchors re-checked 2026-09-18 after T0-6/T0-9 landed; the quoted strings are
unique in each file, so match on the string, not the number.
Contract: every path in these files resolves from the repo root; every
statement about a run matches the run record.
Direction: docs-only
Evidence: VERIFIED — audit §2.8, §6. The v3 record
(`../saddle-bench/runs/PROGRESS.log:35`) says "exit-128 corrupt patches" and every
sealed attempt in `../saddle-bench/runs/t1-saddle/.saddle/proofs.jsonl` carries
`error: corrupt patch at line 12`; "No valid patches in input" appears in no run
file.
Steps:
- CLAUDE.md:79 `died on `git apply: No valid patches in input`` → `died on
  `git apply` exit 128 (`error: corrupt patch at line 12`, every attempt;
  ../saddle-bench/runs/PROGRESS.log:35)`.
- CLAUDE.md:165, DESIGN-NOTES:5, BENCHMARK-RECORD:61:
  `saddle-bench/runs/FINDINGS.md` → `../saddle-bench/runs/FINDINGS.md` and add
  once, at CLAUDE.md:165, "(sibling checkout, not in this repo)".
- BENCHMARK-RECORD:89 reads `see FINDINGS.md for evidence` (bare name, no
  directory): `see FINDINGS.md` → `see ../saddle-bench/runs/FINDINGS.md`.
- DESIGN-NOTES:631 D19 "specified, one global" → "wired per node via
  `reasoning_budget` → `BUDGET_TO_EFFORT` (`cli.py:402-404`); benefit unmeasured
  (T4-3)".
Done when: `grep -rn "No valid patches" CLAUDE.md` prints nothing;
`grep -rn "[^.]saddle-bench/runs/FINDINGS" CLAUDE.md docs/` prints nothing;
`grep -n "see FINDINGS.md" docs/BENCHMARK-RECORD.md` prints nothing.

### T0-9 — Citation corrections (exact replacement text)
Files: `CLAUDE.md:21-22`; `docs/DESIGN-NOTES.md` lines listed below.
Contract: every number attributed to a source is the number the source states,
with the scope the source gives it.
Direction: docs-only
Evidence: audit §5 (local verification) and §9 (deep-research workflow, 25
claims verified, 6 refuted). Each row cites the arXiv id the executing agent
can open to confirm.
Issue: none
Replace, verbatim:

| Where | Now | Replace with | Source |
|---|---|---|---|
| CLAUDE.md:21-22 | `of its accepted tests added no line coverage at all` | `of its 571 mutant-killing tests (277) added no line coverage; engineer acceptance (73%) was measured on a separate 191-test sample` | arXiv 2501.12862 |
| DESIGN-NOTES:25-27 | `true-negative rate falling from **0.68 on weak-generator output to 0.17 on strong-generator output**` | `true-negative rate falling from **0.68 to 0.17** in one cell of its heatmap (Qwen2.5-72B verifying Mathematics answers from Llama-3.1-8B vs Qwen3-32B); the effect is directional across cells, the size is not` | arXiv 2509.17995 |
| DESIGN-NOTES:29-31 | `Pairing a weak generator with an independent verifier closed **75.7%** of the gap to a model three times its size.` | `A fixed GPT-4o verifier filtering K=64 samples closed **75.7%** of the gap in one 181-problem Mathematics difficulty bin ([0.7,0.8): 10.3% → 2.5% error); whole-domain gap closure is 30–50%.` | arXiv 2509.17995 v2 |
| DESIGN-NOTES:139 | `**>30% accuracy drop**` | `wrong or degraded answers **2× to 30× more often** as the context fills (arXiv 2605.12366, Martin & Roger); the Chroma report is cited for the direction only` | audit §5 L139 row; arXiv 2605.12366. The >30% figure was not found in the Chroma report |
| DESIGN-NOTES:182 | `[Zylos][zylos] calls it *correlated error*:` | `[Zylos][zylos] names it *correlated error*:` | — |
| DESIGN-NOTES:185-187 | `Their study across **22,374 program variants** found **>99% of failing agent-written tests passed on the original program while executing the changed region**` | `The measurement is arXiv 2603.23443: across **22,374 mutated CodeNet variants** and 8 models, **>99%** of failing single-shot LLM tests passed on the original program while executing the changed region (per-model pass rates 40.7%–82.9%, so a ~27B model sits near **41%**; this measures single-shot test generation, not agent loops)` | arXiv 2603.23443; corroborated by 2605.06125 (TEBench, F1 45.7–49.4%) |
| DESIGN-NOTES:265-266 | `**~83% accurate at generating properties** versus **62% at directly solving**` | `**82.4% vs 62.4%** on the Easy split (Medium 62.8/17.5, Hard 48.9/1.1; v1 Table IV); v2 (May 2026, retitled "Effective LLM Code Refinement via Property-Oriented and Structurally Minimal Feedback") reports 87.0 vs 63.0 overall with DeepSeek-R1-32B on 100 LiveCodeBench problems` | arXiv 2506.18315 v1/v2 |
| DESIGN-NOTES:281-282 | `Report gains of **+23.1–37.3%** pass@1 over TDD baselines on HumanEval and **+4.2–17.4%** on LiveCodeBench.` | `Report gains of **+23.1–37.3% relative** pass@1 over TDD baselines on HumanEval (v1; v2 headlines "up to 13.4%"). The LiveCodeBench range could not be located in the paper.` | arXiv 2506.18315 |
| DESIGN-NOTES:296 | `filtering removes **>99%** of the candidate pool` | `filtering removes **the large majority** of the candidate pool (the >99% figure was not verified against the paper)` | AlphaCode (unverified) |
| DESIGN-NOTES:318 | `**k=2–3 votes** per step` | `**k=3 votes** per step for gpt-4.1-mini; k_min ranges 3–29 across models (qwen-3: 15; gpt-oss-20B: 6; llama-3.2-3B unusable), scaling as Θ(ln s) with p>0.5 exact-match agreement required` | arXiv 2511.09030 |
| DESIGN-NOTES:319 | `[Snell et al.][testtime] find sequential revision wins on easy problems,` | keep, append ` (not re-verified)` | — |
| DESIGN-NOTES:344-348 | (VP-Control paragraph) | keep the numbers (verbatim, verified: 62.9%, 22.9%, 40.9, 11.3); append: `Scope: a non-code data-operations benchmark (n=849) with Qwen3-4B / Phi-4-mini Q4_K_M verifiers; the authors disclaim cross-domain transfer and report a reversal on FinQA (17% vs 20%). Directional support only for code.` | arXiv 2609.10969 |
| DESIGN-NOTES:401-403 | `measures up to **10pp** loss on GSM-Symbolic; EMNLP 2024 found up to **27pp** on math` | `measures up to **8pp** loss from strict constrained decoding (DeepSeek-R1-Distill-Llama-8B; QwQ-32B 5pp) on GSM-Symbolic, which CRANE recovers; the TC⁰ result (Prop. 3.1) is for finite-output grammars — a diff grammar is infinite and falls under Prop. 3.3. The 27pp figure was not verified.` | arXiv 2502.09061 |
| DESIGN-NOTES:418 | `**100–500 tokens** is the cited range;` | `**100–500 tokens** is a working estimate (no source found; T4 measures it);` | UNSUPPORTED |
| DESIGN-NOTES:419-420 | `Decomposed workflows run ~35% slower on average but cut cost **62%** when paired with smaller models.` | `Claims that decomposed workflows run slower but cheaper with smaller models are unsourced; T4 measures this.` | UNSUPPORTED |
| DESIGN-NOTES:549-551 | `reports **+11.1% average** across GAIA, SWE-Bench Verified, AppWorld and Terminal-Bench from repairing the *harness alone* — beating human-designed harnesses by 6.3%.` | `reports **6.3–18.4 pp absolute** gains across GAIA, SWE-Bench Verified, AppWorld and Terminal-Bench from repairing the *harness alone* (v2; 11.1 is the v2 Table III all-model average). The paper does not compare against human-designed harnesses.` | arXiv 2606.06324 v2 |

| DESIGN-NOTES:119 | `**2% and 16%** of test failures in large projects are flaky` | `**2% and 16%** of test failures in large projects are flaky (range not re-verified: the ACM page returns 403 and the abstract does not carry it)` | ChaosAPI (unverifiable) |
| DESIGN-NOTES:142-143 | `prepending benign tokens ([Chroma, Context Rot][contextrot]).` | `prepending 800k benign tokens (arXiv 2605.12366, Martin & Roger; the Chroma report does not carry this figure).` | arXiv 2605.12366 |
| DESIGN-NOTES:218 | `"specification hacking" — models exploiting weak formal specs` | `"cheating" (the paper's term) — models exploiting weak formal specs` | Vericoding |
| DESIGN-NOTES:318 | `plus schema validation` | `plus a red-flag parser that discards over-long or misformatted responses` | arXiv 2511.09030 |
| DESIGN-NOTES:382-383 | `same conclusion from practice: parallelise reads, keep writes single-threaded.` | `same conclusion from practice: its subtask agents only answer questions and never write code in parallel.` | Cognition post (the reads/writes wording is not in it) |
| DESIGN-NOTES:576-577 | `**persists even when authorship is hidden**` | `**is measured with authorship unlabeled** (randomized unlabeled pairs; equal-quality pairs cut the bias by 31.5%)` | arXiv 2604.22891 |
| DESIGN-NOTES:620 | `specification hacking shows weak specs get exploited even when formal.` | `"cheating" (their term) shows weak specs get exploited even when formal.` | Vericoding |
| DESIGN-NOTES:695 | `83% property accuracy vs 62% solving` | `82.4% vs 62.4% property accuracy vs solving (Easy split)` | arXiv 2506.18315 |
| DESIGN-NOTES:697 | `k=2–3 voting` | `k=3 voting (k_min 3–29 across models)` | arXiv 2511.09030 |
| DESIGN-NOTES:702 | `82/44/27% Dafny/Verus/Lean; specification hacking` | `82/44/27% Dafny/Verus/Lean; "cheating" of weak specs` | Vericoding |
| DESIGN-NOTES:712 | `U-shaped position curve, >30% mid-context drop` | `U-shaped position curve; magnitude per arXiv 2605.12366, not the Chroma post` | arXiv 2605.12366 |
| DESIGN-NOTES:720 | `— persists even when authorship is hidden` | `— measured with authorship unlabeled` | arXiv 2604.22891 |

Apply :218 before :620 and :702, or use `sed` with a line address, so each
"Replace with" lands once. Every "Now" string above occurs exactly once in its
file after whitespace joining (checked 2026-09-18). The table above is the complete list; nothing else is in scope.
Matching note: the "Now" strings are quoted with the source's line wrapping
removed. Several span a line break in the file (DESIGN-NOTES:29-31, :142-143,
:185-187, :265-266, :281-282, :401-403, :419-420, :549-551, :576-577), so grep
for the first 25 characters of the string with `grep -n -F`, or join lines
first, squeezing runs of whitespace to one space:
`tr -s '[:space:]' ' ' < docs/DESIGN-NOTES.md | grep -o -F '<Now string>'`.
Done when: each "Now" string returns 0 hits after `tr -s '[:space:]' ' '` joining, and
each "Replace with" string returns 1 hit.
Stop if: any "Now" string is not found after joining (line drift) — report it.

### T0-10 — Clean-room naming
Files: `docs/DESIGN-NOTES.md:483-484`; `docs/BENCHMARK-RECORD.md:13-18`;
`docs/BENCHMARK.md` (12 lines); `docs/benchmark-archive.md` (22 lines).
Contract: the comparison system is referred to only as "the baseline arm"
(or "baseline arm, thinking xhigh" etc.); its product name does not appear in
the repo.
Direction: docs-only
Evidence: project handoff rule (clean room); audit §7.
Steps:
1. Find every hit without typing the name: 
   `grep -rnwE 'p[i]|p[i]-task' docs/ CLAUDE.md README.md src/ tests/ | cut -c1-120`
2. Replace each with "baseline arm" (or "the baseline arm's probe runs" for
   the "probe arms" at DESIGN-NOTES:483). Keep flags like `-ne -ns -np` only
   if the sentence still makes sense; otherwise drop them.
3. Do **not** rewrite `docs/benchmark-archive.md` history beyond the name.
Done when: the grep in step 1 prints nothing.
Stop if: the grep matches inside a URL or a code identifier that the runs
depend on — report, do not edit.

### T0-11 — Formatter debt
Files: `CLAUDE.md:65` area, `docs/benchmark-archive.md:~1033-1075`,
`docs/WORKPLAN.md:164-169` (the code block in T0-3), and
`tests/test_gates.py:295-298` (the run-order assertion T0-3 added in 71e21c1,
which ruff wants one-name-per-line). ruff formats fenced Python blocks in
Markdown here, so the Markdown files count.
Contract: `uv run ruff format --check .` exits 0.
Direction: docs-only
Steps: `uv run ruff format CLAUDE.md docs/benchmark-archive.md docs/WORKPLAN.md tests/test_gates.py`;
inspect the diff is whitespace/fence-only; commit.
Done when: `uv run ruff format --check .` exits 0.

---

### T0-12 — Clean-room identifiers in the stall checker
Files: `benchmark/stall_check.py:3`, `:8`, `:51`, `:125`, `:135-136`;
`tests/test_stall_check.py:14-15`, `:51`, `:56`, `:70`, `:82`, `:86`, `:94`,
`:99`, `:104`, `:109`, `:115`.
Contract: the product name of the comparison system appears nowhere in the
repo, including code identifiers, flags, docstrings and help text; the stall
checker's behaviour is unchanged.
Direction: renamed, no behaviour change (a CLI flag changes name; nothing
else in this repo passes that flag — `grep -rn "p[i]-session" --exclude-dir=.venv .`
lists only these two files).
Evidence: project handoff rule (clean room); T0-10 stopped on these hits
because its stop-if line reserves code identifiers for a separate item.
Issue: none
Steps (do not type the old name; build it from the grep pattern):
1. `benchmark/stall_check.py`: rename the function at :51 to
   `baseline_tool_calls` (and its call at :136); the flag at :125 to
   `--baseline-session` with help `"Baseline-arm session JSONL (repeatable)."`;
   `args.pi_session` at :135 becomes `args.baseline_session`; docstring :3
   `Reads <name> session JSONL` → `Reads baseline-arm session JSONL`; :8
   `so B is <name>-only` → `so B is baseline-only`.
2. `tests/test_stall_check.py`: rename `_pi_session` (:14, called at :51,
   :70, :86, :99) to `_baseline_session`; every old-flag string literal (the one the grep finds)
   (:56, :82, :94, :104, :109) → `"--baseline-session"`; docstrings :15 and
   :115 `<name> session` → `baseline-arm session`.
3. Run `PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest tests/test_stall_check.py -q --no-cov`.
Known-good: the suite above is green after the rename. Known-bad: the old
flag is rejected — `.venv/bin/python benchmark/stall_check.py <old flag> x`
exits 2 with `unrecognized arguments` (type the flag from the grep pattern).
Contract mutants (`pytest tests/test_stall_check.py -q --no-cov`):
1. `sed -i 's/"--baseline-session", action="append"/"--baseline-sessions", action="append"/' benchmark/stall_check.py` → red (argparse rejects every test's flag).
Done when: `grep -rniwE 'p[i]|p[i]-task' --exclude-dir=.venv --exclude-dir=.git .`
prints nothing; the stall-check tests are green; mutant red; `./check.sh` green.
Stop if: any file outside the two named above matches the grep — report it.
Noticed, not touched (pre-declared): scripts in the sibling `../saddle-bench`
checkout may still pass the old flag; that checkout is outside this repo and
outside this item.

---

## 4. Tier 1 — `./check.sh` back to green

### T1-1 — `Literal[float]` thresholds satisfy mypy without loosening the grammar
Files: `src/saddle/dag.py:24`, `:78`.
Contract: `kill_threshold` accepts exactly {85.0, 90.0, 95.0, 100.0} and
`changed_line_coverage_min` exactly {100.0}, and both reach the decoder as
`enum`/`const` (not `minimum`).
Direction: none (type-checker only); the pins below already exist and stay.
Evidence: VERIFIED — `tests/test_dag.py:336` (known-bad 0.0/50.0/80.0 and
0.0/80.0/99.9 rejected) and `:357` (schema carries enum/const); mypy
`[valid-type]` at dag.py:24/:78 is PEP 586 (floats are not legal `Literal`
parameters for the type checker) while pydantic accepts them at runtime.
Issue: none
Steps: append `  # type: ignore[valid-type]` to both lines with a comment
above each: `# PEP 586 forbids float Literals for the type checker; pydantic
compiles them to a JSON-schema enum, which is what the decoder needs (see
test_gate_thresholds_constrain_the_token_mask_not_just_validation).` Do not
convert to `Annotated[float, …]` with a validator: that would move the floor
from the token mask to post-hoc validation, the exact class of leak
`test_dag.py:357` exists to prevent.
Known-good: `kill_threshold` 85.0/90.0/95.0/100.0 validate (add these four to a
new `test_gate_thresholds_at_the_floor_are_representable` if no test asserts
acceptance of all four; `tests/test_dag.py:44` covers 85.0 only).
Known-bad: existing `:336`.
Contract mutants:
1. `sed -i 's/Literal\[85.0, 90.0, 95.0, 100.0\]/Literal[80.0, 85.0, 90.0, 95.0, 100.0]/' src/saddle/dag.py` → `test_gate_thresholds_below_the_spec_floor_are_unrepresentable` red.
2. `sed -i 's/Literal\[100.0\] = 100.0/float = 100.0/' src/saddle/dag.py` → `:336` and `:357` red.
Done when: `uv run mypy src` reports no dag.py errors; `pytest tests/test_dag.py -q --no-cov` green; both mutants red. `warn_unused_ignores = true` (pyproject:49) will flag the ignore if mypy ever starts accepting float Literals — that is the intended alarm.

### T1-2 — `_best_of_samples` guards the `None` result
Files: `src/saddle/slice.py:314-318`.
Contract: a candidate whose evaluation returns no `Tier1Result` is skipped, not
scored.
Direction: none (type narrowing; `_evaluate_candidate` returns `result=None`
only when `unappliable is not None`, which line 315 already `continue`s on).
Evidence: VERIFIED — mypy `[union-attr]`/`[misc]` at slice.py:318.
Steps: change line 315 `if unappliable is not None:` → `if unappliable is not None or result is None:`.
If the `[misc]` error persists, change L318 to
`failures = sum(not check.passed for check in result.checks)`.
Known-good/known-bad: existing `tests/test_slice.py` best-of tests (`:372`
asserts the seal detail) stay green; no behaviour change is expected, so no
new test. Contract mutant: n/a (no contract moved). State this in the commit.
Done when: `uv run mypy src` reports no slice.py errors; `pytest tests/test_slice.py -q --no-cov` green.

### T1-3 — Test imports exit codes from where they live
Files: `tests/test_evidence.py:16-19` (the names sit at :18-19 inside the block opened at :16).
Contract: `SHELL_TIMEOUT` and `TOOL_UNAVAILABLE` are defined in
`saddle.gates` (`gates.py:29`, `:35`) per the layering rule; `evidence.py:28`
imports them and does not re-export.
Direction: none
Steps: move the two names out of the `from saddle.evidence import (…)` block
into `from saddle.gates import SHELL_TIMEOUT, TOOL_UNAVAILABLE`; run
`uv run ruff check tests/test_evidence.py` (import order) and fix with
`uv run ruff check --fix tests/test_evidence.py`.
Done when: `uv run mypy src tests` (the check.sh invocation) exits 0. `mypy tests`
alone reports ~53 pre-existing `[import-untyped]` errors and is not the gate.
Stop if: `grep -n "^SHELL_TIMEOUT\|^TOOL_UNAVAILABLE" src/saddle/gates.py` prints nothing.

### T1-5 — Every legal threshold value is representable (known-good half of T1-1)
Files: `tests/test_dag.py` (next to `test_gate_thresholds_below_the_spec_floor_are_unrepresentable`, ~L347).
Contract: `Node` accepts every `KillThreshold` member (85.0, 90.0, 95.0, 100.0)
and `changed_line_coverage_min=100.0`; only 85.0 is exercised today (L44).
Direction: none (test only)
Evidence: CLAUDE.md "A constraint is verified by a known-good instance"; T1-1's
executor flagged the gap and did not add the test.
Steps: add `test_gate_thresholds_at_the_floor_and_above_are_representable`,
parametrised over the four values, building a node via the L44 fixture pattern
and asserting no `ValidationError`.
Known-good: the four values. Known-bad: 80.0 (already covered at ~L347).
Mutant: `sed -i 's/Literal\[85.0, 90.0, 95.0, 100.0\]/Literal[85.0, 100.0]/' src/saddle/dag.py`
→ the new test must fail for 90.0 and 95.0. Revert with `git checkout -- src/`.
Done when: `pytest tests/test_dag.py -q --no-cov` green at HEAD and red under the mutant.

### T1-4 — `./check.sh` exits 0
Files: none new.
Contract: ruff check, ruff format --check, mypy (strict) and the suite all pass at HEAD.
Steps: after T0-1, T0-11 (which must run first; it is not optional for this item),
T1-1..T1-3: `./check.sh`; paste the tail of its
output in the commit message of whichever item lands last.
Done when: `./check.sh; echo exit=$?` prints `exit=0`.

---

## 5. Tier 2 — gate integrity (contract changes; each needs mutants and a direction label)

### T2-1 — Best-of-k sampling can actually vary
Files: `src/saddle/cli.py:70-81` (`RunOptions`), `:434-439` (`propose` return),
`:611-630` (`run` parser), `:709-718` (`RunOptions(...)` construction);
`tests/test_cli.py` (new test beside the `_FakeClient` users at `:1087`/`:1140`).
Contract: first-attempt worker proposals (`failure is None`) are sampled at
`options.sample_temperature` (default 0.7); recovery proposals
(`failure is not None`) keep `options.temperature`. Emission (`dag`) is
unchanged.
Direction: **tightened** (a reported "N distinct of 3 samples" now can vary;
today `PROPOSAL_SAMPLES = 3` at `slice.py:87` draws three identical samples at
temperature 0.0, so `_best_of_samples` (`slice.py:290-332`) dedups to one and
the seal at `slice.py:376` always reads "1 distinct of 3 sample(s)").
Evidence: CLAUDE.md "A mechanism must be able to do what it reports";
PUBLISHED-WITH-MEASUREMENT for diversity-then-filter (AlphaCode, pass@k
literature; MAKER arXiv 2511.09030 requires distinct votes); VERIFIED
vacuity: `tests/test_slice.py:372` pins the seal string to "1 distinct", which
is the wrong invariant and must change to assert the count *can* be 3 with a
proposer that returns three different diffs.
Issue: #59 (best-of-k), closes nothing on its own.
Steps:
1. `RunOptions`: add `sample_temperature: float = 0.7` after `temperature`.
2. Parser (after L618): `run.add_argument("--sample-temperature", type=float,
   default=0.7, help="Temperature for first-attempt worker samples (recovery
   uses --temperature).")`; wire `sample_temperature=args.sample_temperature`
   at L709-718.
3. cli.py:437: `temperature=options.temperature,` →
   `temperature=options.sample_temperature if failure is None else options.temperature,`.
4. Test in `tests/test_cli.py`: build `RunOptions(..., temperature=0.0,
   sample_temperature=0.7)`, install `_FakeClient` via
   `monkeypatch.setattr("saddle.cli.VllmClient", _FakeClient)`, drive
   `run_task` far enough that `propose` is called with `failure=None` and once
   with a failure string (the existing end-to-end `run_task` test at
   `:1140` shows the fixture), then assert on `_FakeClient.calls`: every
   `{"diff": kwargs}` entry made with `failure is None` has
   `kwargs["temperature"] == 0.7`, and the recovery call has `0.0`.
   If reaching a recovery call through `run_task` is impractical, call the
   `propose` closure directly: extract it by having the test pass a stub
   `run_slice` via `monkeypatch.setattr("saddle.cli.run_slice", capture)` that
   records the `propose` kwarg, then invoke `propose(node, None)` and
   `propose(node, "tests red")` and inspect `_FakeClient.calls[-2:]`.
5. `tests/test_slice.py:372`: replace the "1 distinct" pin with a test whose
   proposer yields three distinct appliable diffs and asserts the seal detail
   is `f"3 distinct of {PROPOSAL_SAMPLES} sample(s)"` (known-good: count can
   reach 3) and a second whose proposer is constant and asserts "1 distinct"
   (known-bad-for-diversity: dedup still works).
Known-good: first-attempt call carries 0.7. Known-bad: recovery call at 0.7
fails the test.
Contract mutants (`pytest tests/test_cli.py -q --no-cov -k sample_temperature`):
1. `sed -i 's/temperature=options.sample_temperature if failure is None else options.temperature,/temperature=options.temperature,/' src/saddle/cli.py` → red.
2. `sed -i 's/if failure is None else options.temperature,/if failure is not None else options.temperature,/' src/saddle/cli.py` → red (both halves swap).
3. `sed -i 's/sample_temperature: float = 0.7/sample_temperature: float = 0.0/' src/saddle/cli.py` → red only if the test relies on the default; the test must construct `RunOptions` explicitly *and* have one assertion on the parser default (`build_parser().parse_args(["run","x"]).sample_temperature == 0.7`) so this mutant dies.
Done when: all three mutants red; `./check.sh` green; `saddle run --help`
shows `--sample-temperature`; commit body records the direction and mutants.
Stop if: `grep -n "temperature=options.temperature," src/saddle/cli.py` returns anything other than five hits (run_task emit ~L387, recovery `complete` ~L423, `propose` return ~L437, `replan` ~L447, run_dag ~L517). Only the `propose` one, the argument of `client.propose_diff(`, changes; the other four stay.
Note for T4-1: rerun the benchmark only after this lands; a k=3 arm at
temperature 0.0 measures nothing about k.

### T2-1b — The seal's sample count can vary (T2-1 step 5, split out)
Files: `tests/test_slice.py:372` (the `"1 distinct of ..."` pin) and the
test around it.
Contract: `_best_of_samples` reports how many distinct proposals it saw;
with three different appliable diffs the seal detail is
`"3 distinct of 3 sample(s)"`, with a constant proposer it is `"1 distinct of 3 sample(s)"`.
Direction: **tightened** (the pin at :372 asserted the count is always 1,
which is the vacuity CLAUDE.md "A mechanism must be able to do what it
reports" forbids; T2-1 landed the temperature routing but its session
treated `Files:` as authoritative and left this test alone).
Evidence: VERIFIED — `slice.py:376` builds the string from `distinct`;
`PROPOSAL_SAMPLES = 3` at `slice.py:87`.
Issue: #59
Steps:
1. Replace the test containing :372 with two tests. Known-good: a proposer
   that returns three distinct appliable diffs (vary a comment or the
   return value across calls) → the worker span detail is
   `f"3 distinct of {PROPOSAL_SAMPLES} sample(s)"`. Known-bad-for-diversity:
   a constant proposer → `f"1 distinct of {PROPOSAL_SAMPLES} sample(s)"`
   (dedup still works). Model both on the existing test; keep its fixtures.
2. `PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest tests/test_slice.py -q --no-cov -k distinct`.
Contract mutants (`pytest tests/test_slice.py -q --no-cov -k distinct`):
1. `sed -i 's/sampling = f"{distinct} distinct of/sampling = f"1 distinct of/' src/saddle/slice.py` → known-good red.
2. `sed -i 's/for index in range(PROPOSAL_SAMPLES):/for index in range(1):/' src/saddle/slice.py` → known-good red (only one sample drawn).
Done when: both mutants red; `./check.sh` green.
Stop if: `grep -c 'distinct} distinct of' src/saddle/slice.py` is not 1.

### T2-2a — Fixtures stop modelling the #65 exploit as the happy path
Files: `tests/test_slice.py:86-96` (`_slice_repo` — NOT `_git_repo` at :48,
which serves the low-level `_apply_diff` tests and stays untouched), `:61-64`
(`_node_dict`, `"kind": "refactor"`), `:99-116` (`GOOD_DIFF`, the `test_n.py`
new-file hunk), `:119` (`BAD_DIFF`), `:121-129` (`FIX_DIFF`), and the T2-1b
test near `:412` whose proposer yields diff variants;
`tests/test_cli.py:60-78` (`DIFF`), `:81-91` (`_git_repo`), `:94-99`
(`_node_dict`), `:283` (`"new file mode 100644" in prompt`), `:2081`
(`'"refactor"' in prompt`); `tests/test_runner.py:18-25` (`_node`,
`"kind": "refactor"`), every `_worktree(` call without `baseline_test=`
(12 of 15).
Contract: the end-to-end fixtures describe an honest `impl` node: the
baseline commit already holds `test_n.py` (written by a test node earlier),
and the worker diff edits `n.py` only. No production code changes.
Direction: test-only, no contract change. (Today every fixture is a
`refactor` node whose one diff edits `n.py` **and creates** `test_n.py` —
the exact shape #65 describes, and the reason T2-2 broke 31 tests when it
was first attempted: the tightened node-scope check rejects the fixtures
because they are the exploit.)
Evidence: VERIFIED — first T2-2 session, 2026-09-18: 31 failures across
`test_cli.py` (14), `test_runner.py` (5), `test_slice.py` (12), all
`node-scope: refactor node added file(s): test_n.py`. `gates.py`
`check_red_phase` already documents that an `impl` node's tests "were
written by the test node it depends on and already fail at its baseline",
so an `impl` node with a pre-seeded failing test takes the real
differential and passes red-phase without shipping tests.
Issue: #65 (fixture half)
Steps:
1. `tests/test_slice.py`: in `_slice_repo` (the helper every `run_slice`
   test pairs with `_node_dict` and `GOOD_DIFF`), write `test_n.py` with the
   exact body the `GOOD_DIFF` hunk adds (`from n import f`, blank, blank,
   `def test_f():  # REQ-001`, `    assert f() == 2`) and `git add` + commit
   it with `n.py`; delete the `test_n.py` hunk from `GOOD_DIFF` (keep the
   `n.py` hunk); set `_node_dict`'s kind to `"impl"`.
   The recovery fixtures currently model the retry by rewriting the *test*
   (`BAD_DIFF` asserts 3, `FIX_DIFF` edits `test_n.py` back to 2). An `impl`
   node may not touch tests, so move the fault into the source:
   `BAD_DIFF = GOOD_DIFF.replace("+    return 2\n", "+    return 3\n")` and
   `FIX_DIFF` becomes an `n.py` hunk `-    return 3` / `+    return 2` with
   the same header shape as `GOOD_DIFF`'s first hunk. The seeded test still
   fails at baseline (f returns 1), still fails after `BAD_DIFF` (returns 3),
   and passes after `FIX_DIFF`, so every retry/replan test keeps its shape.
   The T2-1b test's three "distinct" proposals must likewise differ in the
   `n.py` hunk, not in `test_n.py`.
2. `tests/test_cli.py`: same three moves on `DIFF`, `_git_repo` (which also
   commits `README.md`; keep that) and `_node_dict`. `:283` asserted the
   worker prompt echoes `"new file mode 100644"` from the diff; the diff no
   longer has one, so assert `"+++ b/n.py"` is in the prompt instead. `:2081`
   asserted `'"refactor"'` is in the planner prompt; check what that test is
   pinning (the kind enum listing, or the node's own kind?) and keep the
   assertion true for what the prompt actually renders — if it lists all
   kinds, it still holds; if it echoes the node's kind, it becomes `'"impl"'`.
3. `tests/test_runner.py`: `_node` kind → `"impl"`; every `_worktree(` call
   that omits `baseline_test=` gets `baseline_test=<the same test body it
   passes as test_body>` so the test exists at baseline. Tests that
   deliberately exercise "new test file appears in the diff" (look for
   `baseline_test=None` semantics or a test name mentioning new tests) keep
   a `refactor` or `test` node explicitly and a comment saying why.
4. Full suite: `PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest -q`.
   Expect the red-phase baseline leg to now exit 1 (assertion) for every
   fixture, not 2 (collection) — if a test pinned "exit 2 / greenfield",
   that is a deliberate greenfield case: give it `kind="test"` or leave the
   file uncreated at baseline with a comment, do not delete the assertion.
Known-good: full suite green at HEAD (before T2-2). Known-bad: temporarily
setting `_node_dict`'s kind back to `"impl"` while the diff still creates
`test_n.py` must fail node-scope with `impl node changed test file(s)` —
run this once in `test_slice.py` to prove the fixture is now load-bearing,
then restore.
Contract mutants: none (test-only; the known-bad run above is the check).
Done when: `./check.sh` green; `grep -c '"kind": "refactor"' tests/test_slice.py tests/test_cli.py tests/test_runner.py` prints 0 for the default fixtures (explicit per-test overrides may remain, each with a comment); no file under `src/` in `git show --stat`.
Stop if: making the fixtures honest requires changing anything under `src/`
— report which gate rejects an honest `impl` node and why.

### T2-2 — A `refactor` node cannot create files (closes the cheapest half of #65)
Files: `src/saddle/evidence.py:244-254` (add `git_added_files` beside
`git_changed_files`); `src/saddle/gates.py:396-426` (`check_node_scope`),
`:470-485` (`Tier1Inputs`), `:565` (the `check_node_scope(...)` call in
`run_tier1`); `src/saddle/runner.py:191-205` (`Tier1Inputs(...)`);
`tests/test_gates.py:586-600` area; `tests/test_evidence.py`;
`tests/test_runner.py` (`test_run_node_gate_records_tool_spans`,
`test_run_node_gate_ignores_stale_bytecode`: two refactor fixtures whose
test was still new-at-baseline, plus the tool-span pin);
`tests/test_slice.py` (`test_run_slice_pass_end_to_end` tool-span pin).
Status 2026-09-18: finished by the reviewer after two executor stops; the
text below is the shape that landed. Both stops were correct.
Requires: T2-2a landed first (the fixtures in `test_slice.py`, `test_cli.py`,
`test_runner.py` must already be honest `impl` nodes, or this check rejects
them — 31 failures on the first attempt). A reference implementation of
this item, unfinished only because of those fixtures, is saved outside the
repo; re-derive from the steps below rather than hunting for it.
Contract: `check_node_scope(kind, changed_files, added_files)` fails a
`refactor` node that adds any file; a behaviour-preserving move may edit
existing sources and tests but the file set it creates is empty.
Direction: **tightened** (`refactor` currently returns pass unconditionally,
gates.py:411, and `kind` is chosen by the emitting model — #65: a node can
select three exemptions, gates.py:250/:321/:376/:410, by naming itself
`refactor`).
Evidence: VERIFIED — #65 (measured exploit, T4 record); this item removes one
of the three exemptions with a check that needs no semantic judgement (a git
`--diff-filter=A` list). The other two (`red-phase` and `property-coverage`
skips at gates.py:250/:321) need a behaviour-preservation oracle and stay
deferred (§7). Rename-detection: `git diff --diff-filter=A` does not list a
pure rename when `diff.renames` is on; run with `--no-renames` so a rename
shows as A+D and a refactor that *moves* a module is rejected too. If that is
judged too strict, the loosening needs its own item with proof.
Issue: #65 (partial).
Steps:
1. `evidence.py` after L254:
   ```python
   def git_added_files(cwd: Path, ref: str, *, recorder: SpanRecorder | None = None) -> list[str]:
       """Worktree-relative paths that do not exist at `ref` (renames count as added)."""
       argv = ["git", "-C", str(cwd), "diff", "--no-renames", "--diff-filter=A",
               "--name-only", ref, "--", "."]
       ...same shape as git_changed_files...
   ```
   Staged adds only. Do **not** append `git ls-files --others`: the
   worker introduces files only through its diff, which `_apply_diff`
   applies with `--index`, so every file a node creates is staged; the
   untracked files in a worktree are the harness's own (`.saddle/proofs.jsonl`
   is the default journal path, `.coverage.tier1`, bytecode), and listing
   them fails every `refactor` node on its first retry. The first draft of
   this item had that clause; the fixtures did not catch it because the
   T2-2a fixtures are `impl` nodes, which ignore `added_files`.
2. `Tier1Inputs`: add `added_files: Collection[str] = ()` as the **last**
   field (frozen dataclass; a default keeps the 13 existing constructors in
   `tests/test_gates.py:59` etc. valid).
3. `check_node_scope(kind, changed_files, added_files=())`: replace the two lines starting at `if kind == "refactor":` (~L411) with
   ```python
   if kind == "refactor":
       if added_files:
           return GateCheck(
               name="node-scope",
               passed=False,
               detail=f"refactor node added file(s): {', '.join(sorted(added_files))}",
           )
       return GateCheck(
           name="node-scope", passed=True, detail="refactor: edits both sides, adds nothing"
       )
   ```
4. `run_tier1` L565: pass `inputs.added_files`.
5. `runner.py`: `added = git_added_files(workdir, baseline, recorder=recorder)`
   next to L122 and `added_files=[str(workdir / p) for p in added]` in the
   `Tier1Inputs(...)` call (match the absolute-path convention of `changed`).
Known-good (`tests/test_gates.py`): `check_node_scope("refactor",
["orders.py", "tests/test_orders.py"], added_files=[]).passed is True`
(extend `test_refactor_node_may_touch_both`).
Known-bad: `check_node_scope("refactor", ["orders.py", "new_module.py"],
added_files=["new_module.py"]).passed is False` and the detail names
`new_module.py`. Plus in `tests/test_evidence.py`: a tmp repo with one commit,
create `b.py`, `git add b.py`, assert `git_added_files(tmp, "HEAD") == ["b.py"]`
and that `git_changed_files` also lists it (so the two helpers agree on
staged adds); an untracked `c.py` and a `.coverage.tier1` are **not** listed.
Contract mutants (`pytest tests/test_gates.py -q --no-cov -k scope`):
1. `sed -i 's/        if added_files:/        if False:/' src/saddle/gates.py` → red (`-k "refactor_node or node_scope"`; plain `-k scope` selects one unrelated test and reports SURVIVED).
2. `sed -i 's/"--diff-filter=A",/"--diff-filter=M",/' src/saddle/evidence.py` → the test_evidence known-good red (`-k added_files`). Quote it: the bare string also sits in the error message. Same byte length, so drop `__pycache__` after the revert (CLAUDE.md hazard 3).
3. `sed -i '/^def check_node_scope/,/^def check_property_coverage/s/if kind == "refactor":/if kind == "test":/' src/saddle/gates.py` → red (refactor falls to the impl/test branch and the known-good fails). Range-scoped: `check_assertion_preservation` has the same line and an unscoped inverse sed corrupts it.
Verdicts 2026-09-18 (reviewer, pre-commit, tree restored to the same hash after each): 1 KILLED, 2 KILLED, 3 KILLED.
6. Two `test_runner.py` refactor fixtures still created their test file
   (kept that way by T2-2a for the RED_PHASE_SAMPLES span shape and the
   stale-bytecode edit). Seed the test at baseline for both: the span test
   gets a weaker baseline assertion and appends the binding one (append-only,
   so assertion-preservation holds, and the signature change still triggers
   three samples); the bytecode test stages `test_n.py` alone instead of
   `git add -A` (which would stage the first run's coverage file as an add)
   and its expected failure list gains `assertion-preservation`, because the
   rewrite now drops an assertion that exists at baseline.
7. The two tool-span pins (`test_runner.py`, `test_slice.py`) gain one
   `"git"` entry right after `git_diff`'s: the staged-adds probe.
Done when: mutants red; `./check.sh` green; `git show --stat` lists exactly
the seven files above.
Stop if: `grep -c 'if kind == "refactor":' src/saddle/gates.py` is not 1 (it is at ~L411; T0-3 shifted it by one).

### T2-3 — Merge-time full-suite gate (the missing Tier 2, minimal form)
Files: `src/saddle/slice.py:636-656` (run-end block); `src/saddle/evidence.py`
(no change; use `run_shell`, L133); `tests/test_slice.py`.
Status 2026-09-18: finished by the reviewer after one correct executor
stop (the first draft said `kind="gate"`; `SpanRecord.kind` is
`Literal["tool", "agent"]` and a suite run is a tool invocation). The text
below is the shape that landed.
Contract: after the schedule/replan loop exits, if at least one node was
proven, the **unscoped** suite command `merge_command` runs once in `workdir`; a
non-zero exit sets the run verdict to failed and seals a **tool** span
`name="merge-suite"`, `node_id=""`, parented to the run span, with the exit
code; a zero exit seals the same span with exit 0. Per-node verdicts are
not revisited. The run span's detail gains `, merge exit N` only when the
gate ran (no proven node → no merge run, detail unchanged).
Direction: **tightened** (today a run whose every node passed its *own*
`test_command` is reported passed even if the union of diffs breaks the
suite — ARCHITECTURE.md:167-171 promises this gate; #60 and D13 record the
gap).
Evidence: PROVEN-IN-PRODUCTION (every merge queue runs the whole suite on
the merged tree; this is the least contestable gate in CI practice);
VERIFIED gap: `grep -n "merge" src/saddle/*.py` finds no merge-time run.
Issue: #60 (closes its minimal form; the budgeted mutmut half of Tier 2 stays
deferred).
Steps:
1. Add a parameter to `run_slice` (L568-576): `merge_command: str | None = "pytest -q"`.
   `None` disables the gate (the tests that assert exact span sequences can
   pass `None`; do not weaken those tests).
2. Insert **after** the `while True:` loop ends and **before**
   `tools = tool_spans_by_node(read_spans(journal_path))`, so the span is
   sealed before the run span:
   ```python
   merge_exit = 0
   merge_ran = merge_command is not None and bool(proofs)
   if merge_command is not None and proofs:
       merge_start = perf_counter()
       merge_exit = run_shell(merge_command, workdir)
       timed_out = " (timed out)" if merge_exit == SHELL_TIMEOUT else ""
       append_span(
           journal_path,
           build_span(
               node_id="",
               argv=shlex.split(merge_command),
               duration_ms=_elapsed_ms(merge_start),
               exit_code=merge_exit,
               detail=f"merge-time full suite: exit {merge_exit}{timed_out}",
               name="merge-suite",
               parent_id=run_span_id,
           ),
       )
   ```
   (`kind` stays the default `"tool"`.) Change the verdict line to
   `passed = not failed_unexcused and not undispatched and merge_exit == 0`.
   Extend the run span detail with
   `+ (f", merge exit {merge_exit}" if merge_ran else "")` — conditional,
   because three tests pin the exact detail of runs with nothing proven.
   Import `run_shell` from `saddle.evidence`, `SHELL_TIMEOUT` from
   `saddle.gates`, and `shlex`.
3. cli.py: no change; the default `"pytest -q"` applies. Do not add a CLI
   flag in this item.
Known-good (`tests/test_slice.py`, model on `test_run_slice_pass_end_to_end`):
one node, good diff, `merge_command="true"` → `result.passed is True`, the
journal holds a tool span named `merge-suite` with `exit_code == 0` under
the run span, and the run detail ends `, merge exit 0`.
Known-bad: same DAG, `merge_command="false"` → `result.passed is False`, the
node's own proof record is still present (per-node verdict untouched), the
`merge-suite` span has `exit_code == 1`, the transcript verdict is FAIL.
Skip case: a node that never proves → no `merge-suite` span, no suffix.
Only the two tests that mock `perf_counter` with four ticks
(`test_run_slice_pass_end_to_end`, `..._records_distinct_sample_count`) take
`merge_command=None`; every other `run_slice` call keeps the default and
runs `pytest -q` in its fixture repo at the end.
Contract mutants (`pytest tests/test_slice.py -q --no-cov -k merge`):
1. `sed -i 's/and merge_exit == 0/and True/' src/saddle/slice.py` → known-bad red.
2. `sed -i 's/if merge_command is not None and proofs:/if False:/' src/saddle/slice.py` → known-good span assertion red.
3. `sed -i 's/exit_code=merge_exit,/exit_code=0,/' src/saddle/slice.py` → known-bad span exit assertion red.
Verdicts 2026-09-18 (reviewer, pre-commit, `-k merge_suite`, tree hash identical after each revert, caches dropped): 1 KILLED (known-bad), 2 KILLED (known-good and known-bad), 3 KILLED (known-bad).
Done when: mutants red; every pre-existing `tests/test_slice.py` test green
(those that count spans get `merge_command=None` with a one-line comment);
`./check.sh` green.
Stop if: `grep -c "passed = not failed_unexcused and not undispatched" src/saddle/slice.py` is not 1.

### T2-4 — Gate outputs carry their evidence basis
Files: `src/saddle/gates.py` (`GateCheck` at ~:80, `check_changed_line_coverage`
~:150-175, `check_mutation` ~:520-560); `src/saddle/journal.py` (`GateOutput`
~:28, `build_from_gate` ~:378); `.gitignore` (one exception line);
`tests/fixtures/pre-basis-proofs.jsonl` (new, generated at d944b08 — see
below); `tests/test_gates.py`; `tests/test_journal.py` (two hand-built hash
payloads plus two new tests); `tests/test_slice.py` (one assertion block in
the merge-suite known-good test).
Status 2026-09-18: done by the reviewer directly (agreed after the T2-2 and
T2-3 stops). The text below is the shape that landed.
Contract: `GateCheck` and `GateOutput` have an optional `basis: str | None =
None`; the mutation check records `"sampled n=<mutants in the sample>"` on
every branch (0 when none were decided), the coverage check records
`"changed-lines=<n>"`; all other checks leave it `None`; `build_from_gate`
copies it into the proof record. A journal written before the field parses
unchanged and its sealed `record_hash` still verifies.
Direction: **scope narrowed** (a reader can now tell a mutation verdict on
0 mutants — the T7 "218 lines, 0 mutants, passed" case in CLAUDE.md — from
one on 5). No verdict changes.
Evidence: VERIFIED — CLAUDE.md "Kill percentage scales with line count";
ACH (arXiv 2501.12862) reports mutant counts alongside kill rates, never a
bare percentage. First draft said to parse the counts out of the detail
strings; the coverage detail has no count and the failing mutation detail
only carries one for small samples, so the count now travels on the check
itself, set where it is known.
Issue: none (supports #62/#63 discussions).
Steps:
1. `GateCheck` (a dataclass): add `basis: str | None = None` last. Every
   `check_mutation` return gets `basis=f"sampled n={outcome.total}"` (the
   three `total == 0` branches say `"sampled n=0"`); every
   `check_changed_line_coverage` return gets `basis=f"changed-lines={len(changed)}"`.
2. `GateOutput`: add `basis: str | None = None` (keeps `extra="forbid"`).
   `build_from_gate` passes `basis=check.basis`.
3. Hash compatibility, how it holds: `build_record` hashes
   `output.model_dump()`, which now includes `"basis": null` for every
   output, so new records carry the key and re-read as set. Verification
   hashes `model_dump(exclude={"record_hash"}, exclude_unset=True)`; an old
   record's JSON has no `basis` key, so it parses unset, is excluded, and
   the old hash matches. Two pre-existing tests in `test_journal.py` rebuild
   the payload by hand and now include `"basis": None`.
4. Fixture: `tests/fixtures/pre-basis-proofs.jsonl` is a journal produced by
   the slice fixture at d944b08 (one proof record, no `basis` keys). It is
   the known-good for the compatibility half and must never be regenerated.
   `.gitignore` ignores `*.jsonl`; the exception `!tests/fixtures/*.jsonl` is
   part of this item. (The benchmark journals in `../saddle-bench/runs/`
   were the first draft's fixture; t7's has no proof records at all, and
   the sibling checkout is outside an executor's read scope.)
Known-good: `check_mutation` over 20 mutants → `basis == "sampled n=20"`;
`check_changed_line_coverage` over one line → `"changed-lines=1"`; a record
built with a basis round-trips through `append_record`/`read_records` and
`verify_journal` reports nothing; the slice known-good's sealed record says
`mutation: sampled n=5`, `coverage: changed-lines=1`, `tests: None`; the
pre-basis fixture verifies, reads back with every basis `None`, and
`rebuild_proven` returns its one node. Known-bad: `GateOutput` with an
unknown extra key raises `ValidationError`.
Contract mutants:
1. `sed -i 's/basis=check.basis)/basis=None)/' src/saddle/journal.py` (ruff keeps the call on one line, hence no trailing comma) → slice known-good basis assertions red (`pytest tests/test_slice.py -k merge_suite_gate_runs_once -q --no-cov`).
2. `sed -i 's/    basis: str | None = None/    basis: str = ""/' src/saddle/journal.py` (one hit in that file) → pre-basis fixture test red (`pytest tests/test_journal.py -k pre_basis -q --no-cov`: the field becomes required, the old record fails to parse).
3. `sed -i '/^    percent = 100.0 \* outcome.killed/,/^def run_tier1/s/basis=f"sampled n={outcome.total}"/basis="sampled n=0"/' src/saddle/gates.py` (range-scoped: the two f-string sites sit below `percent = ...`; the three `total == 0` branches above it already read `"sampled n=0"` and an unscoped inverse would rewrite them) → `pytest tests/test_gates.py -k mutation -q --no-cov` red.
Drop `__pycache__` after each revert (CLAUDE.md hazard 3).
Verdicts 2026-09-18 (reviewer, pre-commit, tree hash identical after each revert): 1 KILLED (slice known-good), 2 KILLED (pre-basis fixture fails to parse), 3 KILLED (two mutation tests).
Done when: `.venv/bin/saddle verify tests/fixtures/pre-basis-proofs.jsonl`
reports no issues; mutants red; `./check.sh` green.
Stop if: `grep -c 'model_dump(exclude={"record_hash"}, exclude_unset=True)' src/saddle/journal.py`
is not 1 (the verification payload, ~:265; `exclude_unset` also appears once
elsewhere) — then old records would hash differently and this item needs a
design decision.

---

## 6. Tier 3 — architecture (one item each; no benchmark needed)

### T3-1 — Resume a journal instead of refusing it
Files: `src/saddle/slice.py` (`run_slice`: the fresh-journal refusal ~:595,
the schedule loop ~:610, the `worker` closure ~:606); `src/saddle/journal.py`
(no change; `rebuild_proven` ~:336 is the mechanism, `_verified_contents`
~:318-331 the refusal); `tests/test_slice.py` (`test_run_slice_refuses_stale_journal`
replaced by three tests).
Status 2026-09-18: done by the reviewer directly. The text below is the
shape that landed.
Contract: `run_slice` on a journal that verifies seeds `proofs` from
`rebuild_proven(journal_path)`, schedules only unproven nodes, and a node
scheduled after its parents were proven (this run or an earlier one) still
cites every parent's proof hash in its record; a journal that does not
verify raises `ValueError` before any node runs; a resume with nothing
left to run seals a run span and passes without calling the proposer.
Direction: **loosened**, with proof: the refusal rejected a legitimate input
(a verified journal from a crashed run; ARCHITECTURE.md promises resume and
`rebuild_proven`'s own docstring says "a crash therefore loses at most the
in-flight node").
Evidence: VERIFIED — `rebuild_proven` exists and is tested in
`tests/test_journal.py`; the refusal was `slice.py:594-596`. Two things the
first draft did not know, found by running it: (1) the loop built
`Dag(nodes=_schedulable_nodes(...))` unguarded and `Dag` requires one node,
so an all-proven resume raised a `ValidationError`; (2) `_schedulable_nodes`
hands the scheduler a copy with proven dependencies stripped, and the
worker sealed *that* copy, so a node whose parent was proven before it was
scheduled recorded `parent_proofs=[]`. That second one already affected the
replan path (a replacement node scheduled after its parent's proof) and is
fixed for both.
Issue: none
Steps (as landed):
1. Replace the refusal with `proofs: dict[str, str] = rebuild_proven(journal_path)`
   (import it; `read_records` stays, the transcript uses it) and let its
   `ValueError` propagate.
2. Loop: `ready = _schedulable_nodes(...)`; `if not ready: break`; then
   `Dag(nodes=ready)`.
3. `worker`: look the original node up in `remaining.nodes` by id and run
   that, so `_run_node` computes `parents` from the full dependency list.
4. Tests: `test_run_slice_resumes_a_verified_journal_and_reuses_its_proofs`
   (first run proves n1; second run's DAG adds a `refactor` n2 depending on
   n1 whose diff appends a comment to `n.py` — `TIDY_DIFF`; the proposer
   asserts it never sees n1; n1's hash is reused; n2's record cites it; the
   run detail is `2 proven, 0 failed, 0 undispatched, merge exit 0`),
   `test_run_slice_resume_with_nothing_left_runs_no_worker`, and
   `test_run_slice_refuses_a_journal_that_does_not_verify` (one byte of the
   sealed thinking changed → `failed verification: bad-hash@line N`).
Known-good: n1's proof is reused, n1's `propose` is never called, n2 cites
n1. Known-bad: a journal with a broken hash raises before any node runs.
Contract mutants (`pytest tests/test_slice.py -k "resume or refuses" -q --no-cov`):
1. `sed -i 's/    proofs: dict\[str, str\] = rebuild_proven(journal_path)/    proofs: dict[str, str] = {}/' src/saddle/slice.py` → n1 re-proposed, resume test red.
2. `sed -i 's/    if hard:/    if False:/' src/saddle/journal.py` → corrupt-journal test red.
3. `sed -i 's/        if not ready:/        if False:/' src/saddle/slice.py` → nothing-left test red (`Dag` of zero nodes).
4. `sed -i 's/return await _run_node(original,/return await _run_node(node,/' src/saddle/slice.py` → n2's `parent_proofs` assertion red.
Each target occurs once in its file. Drop `__pycache__` after each revert.
Verdicts 2026-09-18 (reviewer, pre-commit, tree hash identical after each revert): 1 KILLED (both resume tests), 2 KILLED (corrupt journal accepted), 3 KILLED (Dag of zero nodes), 4 KILLED (n2 loses its parent).
Done when: mutants red; `./check.sh` green — 532 passed at commit time.

### T3-2 — `target_files` on `Node` (issue #64, supersedes #51)
Files: `src/saddle/dag.py` (`Node`: new field + validator, imports);
`src/saddle/gates.py` (`check_target_files`, `Tier1Inputs.touched_files`,
the call in `run_tier1` after `check_node_scope`); `src/saddle/runner.py`
(`touched` from `git_changed_files` ∪ `added`, threaded into `Tier1Inputs`);
`src/saddle/cli.py` (planner rule in `build_emit_prompt`; `targets:` line in
`render_dag_plan` when declared); `docs/ARCHITECTURE.md` (Tier-1 list: item 7,
later items renumbered); `tests/test_dag.py`, `tests/test_gates.py` (three
unit tests, the two gate-order pins, the `len(...) == 11` pin),
`tests/test_runner.py` (`_node(target_files=...)`, one end-to-end test, the
tool-span pin), `tests/test_slice.py` (tool-span pin), `tests/test_cli.py`
(prompt rule, render).
Status 2026-09-18: done by the reviewer directly. The text below is the
shape that landed.
Contract: a node may declare `target_files: list[str]` (default empty). Each
entry is a repo-relative POSIX path: no leading `/`, no backslash, no `..`
segment, no surrounding whitespace, non-empty — rejected at validation.
If the list is non-empty, a Tier-1 check named `target-scope` (after
`node-scope`) fails when any file the diff changed or added — as `git`
lists them, so deletions and non-Python files count — is outside the list,
naming the strays. An empty list is unrestricted.
Direction: **tightened**, opt-in; the planner can only narrow a node's
scope with this field, never widen it (empty is the status quo).
Evidence: PUBLISHED-WITH-MEASUREMENT — Agentless (arXiv 2407.01489)
localisation-then-repair; #64 records T4's wrong-module edit. Design
choices made by running it: (1) the paths are validated by a pydantic
`field_validator`, not a JSON-schema `pattern` — the decoder compiles a
pattern as a full match (CLAUDE.md), and a wrong one would make every path
unrepresentable, silently; the emitted schema therefore carries an
unconstrained optional array and pydantic rejects bad entries post-hoc,
which the existing invalid-emission retry already handles; (2) `touched`
comes from `git_changed_files` (one more `git` tool span per node, so the
two tool-span pins moved again) rather than from `changed_lines`, which
only sees Python statement lines.
Issue: #64
Steps: as the Files line; the gate is `check_target_files(target_files,
touched_files)`; `Tier1Inputs.touched_files` defaults to `()`; the runner
computes `sorted(set(git_changed_files(workdir, baseline)) | set(added))`.
Known-good: `Node` with `["n.py", "src/app/login.py"]` validates; default is
`[]`; `check_target_files(["n.py", "tests/test_n.py"], ["n.py"])` passes; the
runner end-to-end with `target_files=["n.py"]` passes and reports
`1 touched file(s) within 1 target(s)`. Known-bad: each of `/etc/passwd`,
`../n.py`, `src/../n.py`, `src\\n.py`, `" n.py"`, `""` raises
`ValidationError`; `check_target_files(["orders.py"], ["orders.py",
"discounts.py", "new_module.py"])` fails naming both strays; the runner
end-to-end with `target_files=["other.py"]` fails only `target-scope`,
naming `n.py`.
Contract mutants (each target occurs once in its file; drop `__pycache__` after each revert):
1. `sed -i 's/    if not target_files:/    if False:/' src/saddle/gates.py` → `pytest tests/test_gates.py -k target_files -q --no-cov` red (empty list no longer unrestricted).
2. `sed -i 's/or ".." in PurePosixPath(path).parts/or False/' src/saddle/dag.py` → `pytest tests/test_dag.py -k target_files -q --no-cov` red (`../n.py` accepted).
3. `sed -i 's/        touched_files=touched,/        touched_files=(),/' src/saddle/runner.py` → `pytest tests/test_runner.py -k target_files -q --no-cov` red (the wrong-file node passes).
Verdicts 2026-09-18 (reviewer, pre-commit, tree hash identical after each revert, caches dropped): 1 KILLED, 2 KILLED (`../n.py` and `src/../n.py` accepted), 3 KILLED.
Done when: mutants red; `./check.sh` green — 544 passed at commit time.

### T3-3 — Property-coverage soundness (issue #62)
Files: none to edit (decision item). Read only `src/saddle/gates.py`
`check_property_coverage` (~:353-380 after T3-2; the `kind != "test"`
exemption at ~:368 is its first branch).
Not scheduled. `check_property_coverage` verifies a property
test *exists* for a `test` node, which CLAUDE.md classifies as a presence
check. Making it sound needs an oracle (run the property test against a
mutant and require a kill), which is T2-4's data plus a design decision.
Record the decision in #62 before writing code.

### T3-4 — `RUN_ALLOWLIST` / `allowed_tools` is decorative
Files: `src/saddle/cli.py:41` (`RUN_ALLOWLIST`), `:128` (the planner rule
naming the four tools), `:356` (its only use: `validate_dag(..., allowed_tools=RUN_ALLOWLIST)`
at emission), `:504` (`tools:` line in `render_dag_plan`); `src/saddle/dag.py:96`
(`ExecutionConstraints.allowed_tools`); `docs/ARCHITECTURE.md:137` (the
example node's `allowed_tools`), `:149` (static-validation bullet), `:165`
(tool-masking bullet, already marked "specified, not built" by T0-7).
`RUN_ALLOWLIST` constrains what the DAG may *name* in `allowed_tools`; nothing
consumes the list at execution (the worker emits a diff; it has no tools).
Two honest options, user's choice: (a) keep it as documentation of intent
and say so at ARCHITECTURE.md:165 (T0-7's callout already does most of
this); (b) remove the field from `Node` and the schema. Do not build tool masking to make the field
true; that is Phase 2 machinery that no measurement asks for.

### T3-5 — Issue hygiene (comments only; the user closes)
Files: none in the repo. Write the drafts to `../saddle-notes/issue-comments.md`
(outside the repo; the human posts them). Read only the commits named below
via `git show --stat <sha>` and `src/saddle/slice.py` `_apply_diff` (~:136,
for the `--recount` claim).
- #66: fixed by `21c179e` ("Fix mechanically what ruff can fix, before the
  worker is asked to") — propose closing with that SHA.
- #51: superseded by #64 — propose closing #51 with a pointer.
- #61: the grammar landed in `3669f9a`; the issue's remaining question is
  whether the CFG stops the exit-128 corrupt-patch failure. It cannot by
  construction (a CFG cannot enforce hunk-header line counts, audit §9), so
  #61 stays open pending T4-1's measurement and a decision between (i)
  accept-and-retry on `git apply` exit 128 (already the ladder's behaviour)
  and (ii) post-hoc `--recount`-style repair (already on every rung: see
  `git apply --index --recount` in `_apply_diff`). Comment with this framing.
- #23, #17: unchanged; both are future-facing.

### T3-6 — `slice.py` decomposition (optional, no contract change)
Files: `src/saddle/slice.py` (`run_slice`, ~:580-720); `tests/test_slice.py`
(read as the oracle; do not edit).
`run_slice` is now ~130 lines with the resume seed (T3-1), the replan loop,
the merge gate (T2-3), sealing and the verdict inline. Split into
`_schedule_until_done`, `_merge_gate`, `_seal_run`.
Pure refactor: the existing `tests/test_slice.py` is the oracle; no new
tests; no mutants; commit labelled "refactor, no contract change".

---

## 7. Tier 4 — measurement (needs the container; each run is a record, not a code change)

Preconditions for every item: container healthy (`saddle doctor` green), key
in `SADDLE_VLLM_API_KEY`, no source edits during the run, results written
under `../saddle-bench/runs/<id>/` with the pre-registered prediction copied
in before the run starts.

### T4-1 — Rerun the v3 T1 arm against the HEAD grammar (F15)
Files: `tools/diff_grammar_check.py` (`--emit` here; `--run` inside the
container, per its docstring); `src/saddle/cli.py` (`--sample-temperature`,
~:626; landed in T2-1); `docs/DESIGN-NOTES.md` (§"Pre-registered prediction",
~:656-670, and the D2 status line); `docs/BENCHMARK-RECORD.md` (append);
`../saddle-bench/runs/FINDINGS.md` and `../saddle-bench/runs/PROGRESS.log`
(sibling checkout — named here so reading and appending to them is in scope).
Pre-registered prediction (DESIGN-NOTES §"Pre-registered prediction", ~L656): the grammar removes the
"no valid patches"/markdown-wrapper failure class; it does **not** remove
exit-128 `corrupt patch at line N` because hunk line counts are not
context-free. Expected: the failure mix shifts, not vanishes.
Steps:
1. `python tools/diff_grammar_check.py --emit > $SCRATCH/cases.json`, copy into
   the container as its docstring says, run `--run`; record `N/N cases correct`.
   Stop if any case fails: the grammar is wrong before the model is.
2. Run T1 with `--sample-temperature 0.7` (T2-1 landed it; 0.7 is the
   default, so the flag is documentation), three seeds.
3. Record per-attempt `git apply` exit codes and the first stderr line; count
   `corrupt patch` vs `does not apply` vs success.
4. Write F15 into `../saddle-bench/runs/FINDINGS.md`; update DESIGN-NOTES D2
   status; append to `docs/BENCHMARK-RECORD.md`.
Decision this unblocks: #61.

### T4-2 — Does anything overlap? (F7 follow-up)
Files: `src/saddle/scheduler.py:75` (`schedule(dag, worker, *, max_workers=5)`);
`src/saddle/slice.py` (the `schedule(schedulable, worker)` call in the loop,
~:619 — no `max_workers` is passed, so 5 applies); the run's journal spans;
`docs/ARCHITECTURE.md:162` and `:213` (the two "scheduler is asyncio but the
worker is synchronous; no run has overlapped" status lines T0-6 added).
A three-node DAG with no edges, each node a trivial independent edit, with
per-node worker spans timestamped. If `max_workers=5` yields serial spans
(end_i ≤ start_{i+1}), the scheduler's asyncio is decorative and the two
ARCHITECTURE status lines get a "measured serial" note; the fix (a
threadpool around `_run_node`, or an async client) is a Tier 3 item to be
written after this number exists.

### T4-3 — D19 effort sweep
Files: `src/saddle/cli.py:48` (`BUDGET_TO_EFFORT`), `:407` (its use in
`propose`), `:638` (`--worker-effort`); `docs/DESIGN-NOTES.md` (D19 status
line, ~:631); `docs/BENCHMARK-RECORD.md` (append); `../saddle-bench/runs/`
(sibling checkout, results).
`--worker-effort` low/medium/high/xhigh on the T1 task, same seeds as T4-1.
Report pass rate and median worker tokens. This decides whether
`BUDGET_TO_EFFORT` earns its complexity.

### T4-4 — `structural_tag` / `enable_in_reasoning` against vLLM 0.28 (precedes D14)
Files: `src/saddle/vllm.py:57` (`DIFF_GRAMMAR`), `:206` (the request that
sends `structured_outputs={"grammar": DIFF_GRAMMAR}`); a new probe script
`tools/structured_output_probe.py` (the only file to create); `docs/DESIGN-NOTES.md`
D14 (~:415) status line. Needs `SADDLE_VLLM_API_KEY` in the environment and
`saddle doctor` green; never print the key.
The workflow could not confirm (0-3) that vLLM 0.28's structured outputs
honour an `enable_in_reasoning`-style flag or that `structural_tag`
alternation gives "free text then constrained" semantics. Write a 20-line
probe: one request with `structured_outputs={"grammar": DIFF_GRAMMAR}` and
reasoning on, inspect whether the `<think>` block is constrained (it should
not be) and whether the visible completion is. Record the raw response
shape. D14 (structural tags) stays deferred until this is known.

---

## 8. Deferred and not recommended

| Item | Why not now | Would need |
|---|---|---|
| D10 candidate clustering (#63) | SPECULATIVE for code (AlphaCode clustered by *behaviour* on generated inputs; saddle has no input generator); the >99% filtering figure is unverified | an execution-based clustering oracle and a measured pass@k gap |
| D12 parallel-k voting for code | PUBLISHED only on a fixed-strategy game (MAKER, Towers of Hanoi); k_min for qwen-3 was 15, not 3 | a code-domain replication |
| D14 structural tags | engine semantics unresolved (audit §9, refuted 1-2) | T4-4 |
| D15, D16–D18, D20–D25 | SPECULATIVE or UNCLASSIFIED in audit §8; HarnessFix (D25) numbers were misread (T0-9 fixes the text) | a measurement each |
| Rolling wave, stall detection, Phase -1/0, Phase 4 | specified, no measurement asks for them | a failing run that they would have caught |
| Remaining `refactor` exemptions (gates.py:250, :321) | need a behaviour-preservation oracle | design in #65 after T2-2 |
| HANG as a first-class verdict field | already surfaced in `detail` (#55 closed); a field is T2-4's job | nothing |
| Rich TUI (#17), chat-driven runs (#23) | future-facing | user priority |
| Property soundness (#62) | see T3-3 | design decision |

---

## 9. Evidence label legend (as used above)

- **PROVEN-IN-PRODUCTION** — shipped and load-bearing somewhere with a public
  record (git's apply semantics, merge-queue full-suite runs, vLLM grammar
  API, Meta ACH's contract mutants).
- **PUBLISHED-WITH-MEASUREMENT** — an arXiv/venue paper with a number the
  deep-research workflow verified against the source (audit §9), with the
  scope qualifier the paper gives it.
- **VERIFIED** — this repo's own tests, run logs or issue records.
- **SPECULATIVE** — an argument without a number, or a number the workflow
  could not find (audit §9 "unsupported"/"unverified").
