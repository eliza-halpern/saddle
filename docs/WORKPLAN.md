# WORKPLAN — saddle, 2026-09-18

Companion to `docs/AUDIT-2026-09-18.md`. Every item below is scoped for an
executing agent that has **not** read this conversation and may be less capable
than the author. Each item names the files and lines it may touch, the contract
in one sentence, the known-good and known-bad instance that pin it, the 1–3
contract mutants that must die, the evidence it rests on, and the observable
"done" condition. Line numbers are as of `fix/gate-integrity` HEAD `52a0e05`
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
Passing instance at HEAD: <one existing test that already walks the path this item extends> | NONE (then this is a finding item first)
Dry run: <commit at which the author applied it and ran ./check.sh> | not run
```

Two authoring rules, added 2026-09-18 after the stops on T2-2, T2-3 and T3-3
(all correct stops on items written from reading alone):

- **Passing instance.** An item that adds a fixture, a node kind, a gate
  branch or a new path through the gates names one test that already passes
  through that path at HEAD. If none exists, the item is not an item yet:
  write the finding (as T3-7 was) and decide before anyone implements. This
  check is a grep; it needs no run, and it would have caught T3-3's
  unsatisfiable known-good.
- **Dry run.** A contract change (anything with a `Direction:` other than
  docs-only) is applied and run through `./check.sh` by the author before it
  is handed off, and the item says at which commit; the blast radius of a
  gate change across 500 tests is not visible from reading (T2-2: 31
  failures, T2-3: a span kind that did not exist, both found only by
  running). An item marked `not run` may still be handed off, but the
  executor and the reviewer both know a stop is likely and cheap.

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
T3-8 ──► T3-10 (the tree-snapshot helper is reused for the proven-tree ref)
T3-9 ──► T3-10 (T3-10 adds a field to the record shape T3-9 defines)
T3-9 ──► T3-11 (the `node_hash` filter is what drops a foreign `.r1` proof)
T3-4 ──► T3-12, T3-15 (both edit lines T3-4 rewrites: `cli.py:128-131`, `ARCHITECTURE.md:180`)
T3-4 is independent of T3-8, T3-13, T3-14, T3-16 (T3-4 owns `check_node_scope`/`run_tier1`; none of these edit them)
T3-8, T3-9, T3-10 ──► T3-6 (decompose `run_slice` after the resume seed settles)
T3-7a ──► T3-3 (the oracle runs on the impl node a red specification feeds)
T3-7a ──► T3-12 (the planner prompt must also say a test node's tests are expected to fail)
T3-4 ──► T3-7a's test-node fixtures (a test node that creates its test file needs `write_file`)
T3-8 ──► any two-node fixture or run (until it lands, the second node is gated against `HEAD` and fails `node-scope` on the first node's staged files)
T3-7a, T3-8 ──► T3-7b (the smoke run, session 20a) ──► T3-3, T3-9, T3-10, T3-11 (write them after watching the path execute)
T3-7b ──► T3-17, T3-18, T3-19, T3-20, T3-21 (each is a smoke-run finding; T3-17 needs the user's word, T3-18 needs the container)
T3-17, T3-18, T3-19 ──► T4-1 (a benchmark against a merge gate that cannot import, a grammar that admits headerless hunks, or a planner that cannot see the repo measures those defects, not the harness)
T3-7b (20b) ──► T3-23, T3-24, T3-25 (each is a rerun finding; T3-24 needs the user's word)
T3-23, T3-24 ──► T4-1 (a benchmark in which no replacement node can pass `red-phase` and no impl node can pass `requirement-binding` measures those two defects, not the harness)
T3-22 ──► nothing (saddle's own self-mutation score; not a 20b or T4 prerequisite)
T3-9 ──► T3-21's task line (the verdict half stands alone)
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
`tests/test_gates.py:295-298` (the run-order assertion T0-3 added in 27eb9a1,
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
`tests/fixtures/pre-basis-proofs.jsonl` (new, generated at 9854b5f — see
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
   the slice fixture at 9854b5f (one proof record, no `basis` keys). It is
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
unrepresentable, silently; the emitted schema therefore carries an optional
array of non-empty strings (`minLength: 1`, no shape rule) and pydantic
rejects bad entries post-hoc, which the existing invalid-emission retry
already handles; (2) `touched`
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

### T3-3 — Property coverage becomes an oracle on the `impl` node (issue #62)
Status 2026-09-19 (later): DONE -- landed by the main session in the commit
that carries this line, as the rewrite below specifies, with these
differences from the original steps: `check_property_coverage` keeps its
two positional parameters and gains keyword-only `oracle` and `targets`;
the `impl` branch lives in `_check_property_oracle`; `property_modules`
matches an import by dotted path against the changed file's path without
`.py` (a package `__init__.py` is its directory), including relative
imports; the runner computes `property_targets` for every kind and runs
the oracle only for `impl` nodes with targets. Evidence: the real-engine
test `test_mutation_sample_run_tests_restricts_which_tests_the_engine_runs`
(mutmut 3.8: example alone kills, property alone survives); end-to-end
known-good `test_run_node_gate_impl_node_property_oracle_passes_with_basis`
and known-bad `..._property_that_cannot_discriminate_fails` (failing set
exactly `{property-coverage}`, via a stub whose `run` reads the scratch
config); the t1-then-n1 slice test asserts `n1`'s record basis. Contract
mutants, scoped run (21 tests): M0 config drops `run_tests` -> 3 red; M1
`if oracle.killed == 0` -> `if False` -> 2 red; M2 runner passes
`property_oracle=None` -> 3 red; M3 `property_modules` never matches -> 4
red. `./check.sh` green at 100%. #62 stays open for T3-5's comment.
Earlier status 2026-09-19: STOPPED by session 21 (no change to the tree) and
REWRITTEN below; owner became the main session (contract change to
`mutation_sample`). The premise the first text rested on is false:
`mutation_sample`'s `test_files` is the *mutation-exclusion* set (paths
subtracted from `source_paths` in the scratch `pyproject.toml`,
`evidence.py` ~:515, and mutants locating into them dropped, ~:551); the
tests that *run* are whatever pytest collects from the whole scratch tree
(`_mutmut_scratch_config`, ~:470, is not parameterised). Step 2's original
call `mutation_sample(..., test_files=set(targets))` therefore widened the
mutation scope and left the run set untouched, and its "oracle" verdict was
the main gate's verdict relabelled. Session 21's probe: a property with no
discriminating power (`assert isinstance(f(), int)`) beside an example
(`assert f() == 2`), mutant `return 2 -> return 3`: the prescribed call
reported `killed=1` with either `test_files` value, while the property
module alone passes the mutant under pytest. Reviewer's engine probe
(mutmut 3.8.0, real run, 2026-09-19): with the test path appended to
`pytest_add_cli_args`, `test_prop.py` alone -> `survived`; `test_ex.py`
alone -> `killed`; no path -> `killed`. So mutmut honours a collection path
there, and that is the mechanism the rewrite uses.
Step 0 (contract change, main session): `_mutmut_scratch_config(sources,
run_tests=())` appends `sorted(run_tests)` to `pytest_add_cli_args` when
non-empty (the existing exact-string pin `test_mutmut_scratch_config_exact`
stays true for the no-argument form); `mutation_sample` gains keyword-only
`run_tests: Collection[str] = ()` and passes it through; the docstring
says "`test_files` excludes paths from mutation; `run_tests` restricts
which tests pytest collects". Known-good/known-bad for step 0 use the real
engine (override the conftest `_stub_mutmut` PATH), the probe workdir
above: `run_tests={"test_ex.py"}` -> `killed=1`; `run_tests={"test_prop.py"}`
-> `killed=0, survivors=("n.x_f__mutmut_1",)`; the unit half pins the config
string `pytest_add_cli_args = ["-q", "-x", "-p", "no:cacheprovider", "test_prop.py"]`.
Step 2 becomes: `oracle = mutation_sample(workdir, changed, sample.max_mutants,
test_files=test_sources, run_tests=set(targets), recorder=recorder)` --
the exclusion set is unchanged from the main gate; only the run set narrows.
Fixtures: the conftest stub answers every call identically, so the runner
end-to-end known-bad needs a stub whose `run` inspects `pyproject.toml` and
writes survivors to its results file when a test path appears in
`pytest_add_cli_args` and kills otherwise; then the main gate passes, the
oracle fails, and the failing set is exactly `{"property-coverage"}`.
Contract mutant 0 (add to the table): `_mutmut_scratch_config` ignores
`run_tests` -> the real-engine known-bad goes green (property alone reports
a kill) -> red. Additional stop-if: a mutmut whose `pytest_add_cli_args`
does not restrict collection (re-run the engine probe first).
Original text follows, for the parts that still hold.
Decision 2026-09-18: build the oracle (option b), placed per T3-7 option A.
Requires T3-7a (a `test` node can pass) and T2-4 (`basis`, landed).
Files: `src/saddle/gates.py` (`check_property_coverage` ~:353-380: keeps
the presence half for `test` nodes, gains an `oracle: MutationOutcome | None`
half for `impl` nodes; `_has_property` ~:306 stays; `Tier1Inputs` ~:528:
`property_oracle: MutationOutcome | None = None`; the call in `run_tier1`);
`src/saddle/evidence.py` (new `property_modules(test_sources, changed_files)`
beside `mutation_sample` ~:385; `statement_lines` ~:501 unused here — the
mutants come from the node's own `changed` set); `src/saddle/runner.py`
(~:188-212: a second `mutation_sample` for `impl` nodes whose changed files
are imported by a property-bearing test module, with `test_files` =
those modules only); `docs/ARCHITECTURE.md:182` (item 8); `tests/test_gates.py`,
`tests/test_evidence.py`, `tests/test_runner.py` (the T3-7a fixtures).
Contract: `property-coverage` has two halves by kind. `test` node: at least
one of its test modules drives a hypothesis property (today's check; the
red specification must include a property). `impl` node: if any
property-bearing test module in the workdir imports one of the node's
changed modules, those modules alone, as the test set, must kill at least
one mutant of the node's changed lines (the same `changed` set the
mutation gate sampled); the check records `basis="oracle: killed k of n
mutant(s) by <module>"`; if no property module imports the changed
modules it passes "not required: no property targets this change" with
`basis=None`; an oracle that did not run when it should have fails.
`refactor`: not required, as today.
Direction: **tightened** (a `@given` written by the test node that cannot
discriminate on the implementation now fails the `impl` node that
implemented it; F1's regex would have been caught here, at the node that
shipped it).
Evidence: VERIFIED — F1; CLAUDE.md "contract mutants"; PUBLISHED — ACH
(arXiv 2501.12862) accepts a generated test only when it kills. Placement
by T3-7: a property run at the `test` node fails on the unmutated
baseline too (the implementation is not there yet), so every kill would
be vacuous; at the `impl` node the code exists and a kill means
discrimination.
Issue: #62 (closes it when landed; comment first — T3-5 carries the draft).
Steps:
1. `evidence.py`: `property_modules(test_sources, changed_files) -> dict[str, str]`:
   the subset of `test_sources` whose AST has a `@given` decorator and
   whose `import x` / `from x import` names resolve to a path in
   `changed_files` (compare module name to the changed file's stem /
   package path). Put the `@given` walk in `evidence.py`; `gates.py` keeps
   its own `_has_property` (no runtime import from `evidence`).
2. `runner.py`, `impl` nodes only, after the existing `mutation_sample`:
   `targets = property_modules(test_sources, changed_files)`; if `targets`:
   `oracle = mutation_sample(workdir, changed, sample.max_mutants, test_files=set(targets), recorder=recorder)`
   else `None`; pass `property_oracle=oracle` and also `property_targets=tuple(targets)`
   (so the check can tell "no targets" from "did not run").
3. `check_property_coverage(kind, test_sources, *, oracle=None, targets=())`:
   `test` → presence as today; `impl` → no `targets` → pass "not required";
   `targets` and `oracle is None` → fail "property oracle did not run";
   `oracle.total == 0` → fail "no mutants sampled for the property oracle";
   `oracle.killed == 0` → fail "property killed 0 of n mutant(s): no
   discriminating power"; else pass with the basis. `refactor` → not required.
4. ARCHITECTURE item 8: "A test node must state a property; the impl node
   that implements it must show the property alone kills a sampled mutant."
Known-good: unit — `check_property_coverage("impl", {...}, oracle=MutationOutcome(killed=3, total=5, generated=5, survivors=("m4","m5")), targets=("test_n.py",))` passes with the basis; `("impl", {...}, targets=())` passes "not required"; evidence — `property_modules({"test_n.py": <@given, from n import f>, "test_x.py": <@given, import os>}, [".../n.py"])` returns only `test_n.py`; runner end-to-end — T3-7a's `impl` fixture with a property-bearing `test_n.py` and the conftest stub (5 killed) passes with `basis == "oracle: killed 5 of 5 mutant(s) by test_n.py"`.
Known-bad: unit — `killed=0` fails naming 0 of n; `total=0` fails; `targets` non-empty with `oracle=None` fails "did not run"; `refactor` "not required"; runner end-to-end — the same `impl` fixture with a `PATH` stub built by `tests/test_evidence.py::_stub_mutmut` (~:392) reporting `m1..m5: survived` fails `property-coverage` and no other gate (the main mutation gate reads the conftest stub's 5 kills only if the survivor stub is scoped to the second call — simplest: make the survivor stub answer `results` with survivors on every call, and assert the failing set is exactly `{"mutation", "property-coverage"}`; state which in the test's docstring).
Contract mutants (write the exact seds when the code exists; unique targets or range-scoped; drop `__pycache__` after each revert):
1. `if oracle.killed == 0:` → `if False:` → unit known-bad red.
2. runner passes `property_oracle=None` while passing `property_targets` → end-to-end known-good red ("did not run").
3. `property_modules` returns `{}` → end-to-end known-good red ("not required" where the basis was expected).
Done when: mutants red; `./check.sh` green; the T3-7a slice test (`t1` then `n1`) still passes with `n1`'s record carrying the oracle basis.
Stop if: T3-7a has not landed, or `mutation_sample`'s signature is not `(workdir, changed, max_mutants, *, test_files, timeout_s, recorder)`.
Passing instance at HEAD: `test_run_node_gate_end_to_end_pass` (the `impl` mutation path the oracle re-runs with a narrower test set); the property half has NONE until T3-7a's fixture exists.
Dry run: not run.

### T3-4 — Bind `allowed_tools` to harness behaviour (RUN_ALLOWLIST stops being decorative)
Status 2026-09-18: DONE — dbf8062 (executor, session 19) plus the reviewer's
follow-up commit below. Landed as specified, with these differences from the
text that follows: `build_worker_prompt` replaces the context block with
`CONTENTS_WITHHELD` rather than emptying `contents` (`run_task` still reads
the files; only the prompt withholds them); the captured-run filter is
`_run_is_allowed` over a `CAPTURED_RUN_TOOL` map in `slice.py`, and a run
that resolves to no binding (git, mutmut) is kept; `check_node_scope`'s
`may_create` is required keyword-only while `added_files` keeps its `()`
default. Reviewer re-ran the committed table's three mutants and two more
(`"ruff": "lint"` → `"run_tests"`; `run_tier1`'s `may_create=` → `True`):
all five KILLED, tree hash identical after each revert, caches dropped;
`./check.sh` green at dbf8062 (558 passed, 100%). Process defect, reported
by the executor itself: the commit message's mutant table was written
before the mutants ran (HANDOFF-PROMPT forbids this); every recorded
verdict was then reproduced, first by the executor and then here.
Follow-up (reviewer, **tightened**): `_run_is_allowed` matched `argv[0]`
literally and `test_command` is an unconstrained string, so
`python3 -m pytest` or `.venv/bin/pytest` reached the repair prompt
without `run_tests` — the same class as T3-13's unmatched spellings.
`_captured_run_tool` now matches the executable's basename and, for
`python -m X`, the module X.
`test_repair_prompt_binding_matches_how_a_plan_spells_the_command`
parametrizes twelve spellings: known-good and known-bad for each binding,
and no-binding runs kept. Mutants (pre-commit, inverse replacement, tree
hash identical after each): basename → `argv[0]` KILLED (3 cases), python
branch → `if False` KILLED (3), `-m` module → `"python"` KILLED (1).
Noticed, not touched: `tests/test_vllm_live.py`'s `LIVE_PROMPT` is a
hand-written copy of the emit prompt's rules and now lacks the bindings
paragraph; it is skipped without a key, so switching it to
`build_emit_prompt` is left for T4-4's live session.
Decision 2026-09-18: neither keep-as-documentation nor delete. Each tool
name becomes a capability the harness actually honours, so the planner's
choice changes what a node gets and may do, and a name without a binding
fails a test.
Files: `src/saddle/cli.py:41` (`RUN_ALLOWLIST` → derived from a
`TOOL_BINDINGS` registry), `:128` (planner rule: state each tool's effect),
`~:150-215` (`build_worker_prompt`: file contents gated by `read_file`),
`~:409-412` (`run_task` builds `files`/`contents` from `git_ls_files`);
`src/saddle/slice.py` (`format_attempt_failure` ~:213: captured output
filtered by the node's tools; its call in `_run_node` ~:421 passes the node);
`src/saddle/gates.py` (`check_node_scope` ~:443: `write_file` governs file
creation for every kind; `run_tier1` passes `node.execution_constraints.allowed_tools`);
`docs/ARCHITECTURE.md:149` (static-validation bullet), `:165` (tool-masking
bullet, rewritten to what the bindings are); `tests/test_cli.py`,
`tests/test_slice.py`, `tests/test_gates.py`, `tests/test_runner.py`
(fixtures that create files must list `write_file`: `_node()` there declares
`["read_file"]` only — `test_run_node_gate_new_test_passing_pre_change_is_not_red`,
`test_run_node_gate_flaky_baseline_is_caught_end_to_end`,
`test_run_node_gate_greenfield_tautology_no_longer_clears_red_phase` create
a test file and would gain a second failing check; they assert only
red-phase, so give them `write_file` for honesty rather than leaving the
extra failure silent).
Contract — one binding per name, nothing else in the list:
- `read_file`: the worker prompt carries the repo files' contents (as today,
  truncated to `max_context_tokens`). Without it the prompt lists file names
  only. This is the context-cost lever DESIGN-NOTES D15/D16 argue for and
  nothing measures yet.
- `write_file`: the node may create files, subject to its kind's scope rule.
  Without it, `added_files` must be empty whatever the kind (`node-scope`
  fails: "node may not create files: write_file not in allowed_tools"). Uses
  T2-2's staged-adds probe; no new subprocess.
- `run_tests`: after a failed attempt the recovery prompt includes the
  captured test-command output (as today). Without it, the prompt carries
  the gate verdict lines only.
- `lint`: same as `run_tests` for ruff's captured output. `autofix` (#66)
  stays unconditional — it is harness hygiene that prevented F13, not a
  tool the node chooses.
- `RUN_ALLOWLIST = tuple(TOOL_BINDINGS)`; a test asserts every name in the
  registry is exercised by at least one of the tests below, so adding a
  name without a behaviour fails the suite.
Direction: **tightened** for `write_file` (a node that did not declare it
can no longer create files); **scope narrowed** for `read_file`, `run_tests`,
`lint` (omitting them removes prompt content the node used to get for free;
the default planner output today lists all four, so nothing changes until a
plan chooses otherwise). Journal format unchanged.
Evidence: VERIFIED — `RUN_ALLOWLIST` has one use (`cli.py:356`, emission
validation); `build_worker_prompt` always inlines contents; `format_attempt_failure`
always inlines every non-zero captured run; F13 for keeping autofix
unconditional. SPECULATIVE — whether omitting `read_file`/`run_tests` saves
tokens without costing pass rate; that is a T4 question (see T4-3's sweep,
which gains a tools axis).
Issue: none (ARCHITECTURE §Tool Masking).
Steps:
1. `cli.py`: `TOOL_BINDINGS: Final[dict[str, str]] = {"read_file": "...", "write_file": "...", "run_tests": "...", "lint": "..."}`
   with one-line effects; `RUN_ALLOWLIST = tuple(TOOL_BINDINGS)`; the planner
   rule renders the four effects so the choice is deliberate.
2. `build_worker_prompt`: if `"read_file"` not in `node.execution_constraints.allowed_tools`,
   `contents = {}` and the prompt says "(file contents withheld: read_file
   not in allowed_tools)"; file names still listed.
3. `format_attempt_failure(result, captured, *, attempt, max_attempts, tools)`:
   keep a captured run only if its tool is allowed — `argv[0]` in
   `("pytest", "coverage", "python")` → `run_tests`; `"ruff"` → `lint`.
4. `check_node_scope(kind, changed_files, added_files, *, may_create)`:
   before the kind branches, `if added_files and not may_create: fail`.
   `run_tier1` passes `may_create="write_file" in node.execution_constraints.allowed_tools`.
5. ARCHITECTURE :165: replace the schema-injection sentence with the four
   bindings; :149 unchanged except "⊆ global allowlist (each name bound to a
   harness behaviour, item 6 and the recovery prompt)".
Known-good: a node listing all four behaves exactly as today (prompt with
contents, recovery with pytest and ruff output, files may be created);
`check_node_scope("impl", ["n.py"], [], may_create=False)` passes.
Known-bad: `read_file` omitted → the worker prompt contains no file body and
carries the withheld notice; `run_tests` omitted → the recovery prompt has
no `--- \`pytest ...\`` block while a `lint`-only node still gets ruff's;
`write_file` omitted → `check_node_scope("refactor", ["n.py"], ["new.py"], may_create=False)`
fails naming `write_file`; a registry name with no test coverage fails the
registry test.
Contract mutants (`-k "tool or allowed or may_create"`):
1. `if added_files and not may_create:` → `if False:` → known-bad red.
2. `if "read_file" in tools:` (prompt) → `if True:` → withheld-notice test red.
3. captured-run filter → keep all → the `run_tests`-omitted recovery test red.
Write the exact `sed` lines when the code exists; unique targets or range-scoped; drop `__pycache__` after each revert.
Done when: mutants red; `./check.sh` green; `saddle dag` on a plan that
omits `read_file` still renders `tools:` correctly (no render change).
Measurement follow-up: T4-3's sweep gains an axis — the T1 task with
`read_file` omitted, and with `run_tests` omitted — reporting pass rate
and median worker tokens beside the effort axis.

### T3-5 — Issue hygiene (comments only; the user closes)
Status 2026-09-19: DONE by the reviewer (session 16's scope, run in the
main session while 28 held `slice.py`). Drafts at
`../saddle-notes/issue-comments.md`; posted with the user's word to
#66, #51, #64, #67, #65, #62, #61; the user closed #66, #51, #67, #62.
#64 stays open narrowed to the size budget, #61 until T4-1 reports,
#65 open on its residual (`kind` is still a planner-written label
selecting the differential red-phase leg). #44 was already closed; #23
and #17 untouched.
Files: none in the repo. Write the drafts to `../saddle-notes/issue-comments.md`
(outside the repo; the human posts them). Read only the commits named below
via `git show --stat <sha>` and `src/saddle/slice.py` `_apply_diff` (~:136,
for the `--recount` claim).
- #66: fixed by `693d766` ("Fix mechanically what ruff can fix, before the
  worker is asked to") — propose closing with that SHA.
- #51: superseded by #64 — propose closing #51 with a pointer.
- #61: the grammar landed in `52a0e05`; the issue's remaining question is
  whether the CFG stops the exit-128 corrupt-patch failure. It cannot by
  construction (a CFG cannot enforce hunk-header line counts, audit §9), so
  #61 stays open pending T4-1's measurement and a decision between (i)
  accept-and-retry on `git apply` exit 128 (already the ladder's behaviour)
  and (ii) post-hoc `--recount`-style repair (already on every rung: see
  `git apply --index --recount` in `_apply_diff`). Comment with this framing.
- #23, #17: unchanged; both are future-facing.

### T3-6 — `slice.py` decomposition (optional, no contract change)
Status 2026-09-19: DONE by session 28, commit 7947b54 (refactor, no
contract change). `run_slice` now wires `_seed_proofs`,
`_schedule_until_done` (worker closure plus the schedule/replan loop),
`_merge_gate` and `_seal_run` in the original order; the diff touches
`slice.py` only; the five pinned private names are untouched and the
patched globals are still looked up through the module. Reviewer re-ran
`./check.sh` at 7947b54: 662 passed, 3 skipped, 100%.
Files: `src/saddle/slice.py` (`run_slice`, :764-960 at e305e8c);
`tests/test_slice.py` (read as the oracle; do not edit).
`run_slice` is now ~195 lines with the resume seed (T3-1/T3-9/T3-10,
:804), the replan loop (:860-895, `taken=` since T3-11), the merge gate
(T2-3, :898), sealing and the verdict inline. Split into
`_schedule_until_done`, `_merge_gate`, `_seal_run`.
Pure refactor: the existing `tests/test_slice.py` is the oracle; no new
tests; no mutants; commit labelled "refactor, no contract change".
Pre-verified 2026-09-19 by the reviewer against e305e8c. Constraints the
oracle pins, so the split must keep them: the tests import
`_apply_diff`, `_HaltRecoveryError`, `_run_node`, `_schedulable_nodes`
and `_utcnow` by name (keep those names and signatures); they
monkeypatch `slice_module.perf_counter`, `append_span` and
`append_record` (the new helpers must call those through the module's
globals, as `run_slice` does now -- do not bind them as defaults or
import them locally); and several tests pin the exact `git`/agent span
sequence, so the order of subprocess and span calls must not move
across helper boundaries. Done when: `git diff --stat` touches
`src/saddle/slice.py` only and `./check.sh` is green at 100%.

### T3-7 — A `test`-kind node cannot pass Tier-1 (finding; decision needed)
Files: none to edit until decided. Read `src/saddle/gates.py`
(`check_tests` ~:120-140, `check_red_phase` ~:230-300, `check_mutation`
~:560-600, `check_node_scope` ~:443), `src/saddle/evidence.py`
(`mutation_sample` ~:385-450, the `test_files` exclusion ~:411),
`docs/ARCHITECTURE.md:180` (Node-Scope) and the DAG kind docstring in
`src/saddle/dag.py` (~:101-110).
Finding (VERIFIED 2026-09-18 by the first T3-3 session, confirmed by the
reviewer): the `test`/`impl` split (F5, #44) is enforced by node-scope, but
the Tier-1 path a `test` node must walk cannot end green:
- `check_tests` requires the node's `test_command` to exit 0 for every kind;
  a `test` node writes tests for behaviour that does not exist yet, so its
  tests fail — or, if they pass, they prove nothing.
- `check_red_phase` has no branch a `test` node can satisfy: it changes no
  source, so its tests behave identically on the baseline leg and the
  current leg; "fail pre-change, pass post-change" is impossible, and the
  behaviour-preserved branch is `refactor`-only (`gates.py:267`).
- `check_mutation`: the node's changed lines are all in test files, which
  `mutation_sample` excludes from the mutant scope by design, so
  `total == 0` and every such branch fails.
Consequently every end-to-end fixture in the suite is `impl` or `refactor`;
no `test`-kind node has ever been run through the gates (`grep -rn
'"kind": "test"' tests/` finds only unit tests of single checks), and the
planner has never been told it may not emit one. A DAG that follows the
split as documented cannot run.
Options (user's choice; each is its own tightened/loosened contract with
known-good and known-bad):
- **(A) `test` node = red specification.** For kind `test`: `check_tests`
  passes when the command exits `PYTEST_TESTS_FAILED` (1) with at least one
  test collected and failing, and fails on 0 (nothing specified) or on
  collection errors that name no source the dependent impl will create;
  `check_red_phase` for a `test` node is satisfied by that same red run
  (the pre-change leg *is* the current leg); `check_mutation` and coverage
  are "not required: no source changed" with `basis="test node"`; T3-3's
  property oracle moves to the **impl** node that depends on it — after
  the impl's own mutation gate, run the sampled mutants again with only the
  property-bearing test modules as the test set and require ≥1 kill, so the
  property is shown to discriminate on the code that now exists. The
  dependent `impl` node's tests gate already requires green, which is the
  other half of red-then-green. Direction: loosened for `test` nodes (three
  checks that could only fail now have a satisfiable contract), tightened
  for `impl` nodes with properties (the oracle).
- **(B) Drop the `test` kind.** Keep `impl` (may not touch tests) and
  `refactor` (both sides, no new files); tests are written by... nobody in
  the DAG, which contradicts F5/#44's whole point. Listed for completeness;
  not recommended.
- **(C) Merge test+impl into one node kind with a two-phase gate.** The
  node's diff carries tests and implementation; the harness applies the
  test files first, runs the command (must be red), then applies the rest
  (must be green). Direction: replaces node-scope's split with an in-node
  ordering. Bigger change; loses the independent second look that the split
  exists to provide.
Recommendation: (A). It is what ARCHITECTURE's Node-Scope text already
implies, it needs no schema change, and it gives T3-3's oracle the node
where it means something.
**Decision 2026-09-18: (A).** Implemented by T3-7a; T3-3 is rewritten
against it below. Still to do for this item: the comment on #62/#44.
Done when: the comment is posted (T3-5 carries the draft).
Stop if: n/a (decision item).
Passing instance at HEAD: NONE for the `test`-node path — that is the finding.
Dry run: n/a (decision item).

### T3-7a — A `test` node is a red specification (implements T3-7 option A)
Status 2026-09-19: DONE by the reviewer (commit below) after session 20
stopped correctly on step 1's premise: there is no `check_tests(test_command,
exit_code)`; the gate is `check_test_command(test_command, run)`, a
runner-injected predicate that never receives an exit code. Landed within
that shape rather than inverting it: `check_test_command(test_command, run,
*, kind="impl", output="", workdir_modules=())` still calls `run` for the
exit code; `output` (the suite's captured text) and `workdir_modules` (the
worktree's importable top-level names) are two new `Tier1Inputs` fields the
runner fills from the capture it already took, so the predicate stays
subprocess-free. The `test` verdict is `_red_specification`: exit 1 with
`N failed` in the output passes ("red specification: N failing test(s)");
exit 2 whose output says `No module named 'X'` with X not in
`workdir_modules` passes ("module 'X' does not exist yet"); exit 0, exit 1
with no failing count, exit 2 naming an existing module or none, and every
other exit fail; hang and unavailable fail as for every kind.
`check_red_phase(..., red_spec: GateCheck | None = None)` returns for
`kind == "test"` before any baseline observation is read. `run_tier1`
substitutes `_not_required("coverage")` and `_not_required("mutation")`
(`basis="test node"`) and evaluates syntax, ruff, tests in the same order
as before, so the runner invocation order is unchanged. `runner.py` takes
zero baseline samples and skips mutmut for a `test` node; the suite still
runs once, under coverage. The planner prompt gains the sentence; ARCHITECTURE
items 2, 3, 4, 10 gain the clause.
Fixture corrections: `tests/test_gates.py`'s `_red` helper defaulted the
differential path to `kind="test"` and three red-phase tests named it so;
that is the T3-7 mismodelling in the fixtures, and they now say `impl`. The
gate's ruff run has isort on, so the fixture test files use one import per
line with `n` first-party (blank line before it) and a greenfield `m`
third-party (no blank line).
Not landed here: the two-node slice fixture (`t1` test → `n1` impl). It
cannot pass until T3-8: `n1` is gated against `HEAD`, where `t1`'s staged
test file reads as a test change by an `impl` node and fails `node-scope`.
It moves to T3-8's known-good.
Mutants (pre-commit, exact replacement, tree hash identical after each
revert, caches dropped; `pytest tests/test_gates.py tests/test_runner.py -k
"spec or test_node or tier1 or red_phase or tests_impl" --no-cov`):
1. `if kind == "test":` (tests check) → `if False:` — KILLED (8 tests).
2. greenfield rule drops `and missing not in workdir_modules` — KILLED.
3. red-phase test branch → `if True:` — KILLED (3, incl. the end-to-end known-bad).
4. `is_spec = node.kind == "test"` → `is_spec = True` — KILLED (all-green coverage pin, impl test).
5. runner `samples = 0 if node.kind == "test"` → `if False` — KILLED (one `coverage` span expected).
6. runner mutmut skip `if node.kind == "test"` → `if False` — KILLED (`mutmut` span absent).
Noticed, not touched: ARCHITECTURE's Tier-2 status line still reads
"specified, not built" although T2-3 landed the full-suite half — T3-15's.
Files: `src/saddle/gates.py` (`check_tests` ~:120-140: a `kind` branch;
`check_red_phase` ~:230-300: a `test`-kind branch before the sampling
logic; `check_changed_line_coverage` ~:150-175 and `check_mutation`
~:560-600 are unchanged — `run_tier1` ~:615 substitutes a "not required"
`GateCheck` for `test` nodes before calling them); `src/saddle/runner.py`
(~:126-175: skip the coverage data file, the baseline samples and
`mutation_sample` for `test` nodes; still run the suite once and capture
it); `src/saddle/cli.py` (`build_emit_prompt` ~:110: one sentence: a `test`
node's tests are expected to fail until the `impl` node that depends on it
lands); `docs/ARCHITECTURE.md:176-183` (items 2, 3, 4, 10 gain the
`test`-node clause); `tests/test_gates.py` (three unit tests + the order
pins unchanged); `tests/test_runner.py` (the first end-to-end `test`-node
fixture: `_node(kind="test")`, `_worktree(..., baseline_code=fixed_code)`
so the source is untouched and a new failing test file is the diff — needs
`write_file` once T3-4 lands; until then the fixture is a `test` node
creating a test file, which node-scope allows for that kind);
`tests/test_slice.py` (one two-node slice: `test` node `t1` then `impl`
node `n1` depending on it; `_slice_repo` without `test_n.py` at baseline
for this test only).
Contract: for `kind == "test"`: (1) `tests` passes when the node's
`test_command` exits `PYTEST_TESTS_FAILED` with at least one test
collected ("red specification: N failing test(s)"), or exits
`PYTEST_COLLECTION_ERROR` whose output names a module that does not exist
in the workdir (the greenfield spec: the `impl` node will create it); it
fails on exit 0 ("tests pass against the current code: nothing
specified"), on 0 collected, on a timeout, and on any other exit. (2)
`red-phase` for a `test` node is the same observation: it passes iff (1)
passed ("red by construction: the specification fails now"), with no
baseline leg. (3) `coverage` and `mutation` for a `test` node are "not
required: no source changed", `basis="test node"`. Every other kind is
unchanged: an `impl` node still needs exit 0, the differential red-phase,
coverage and mutation. The `impl` node that depends on a `test` node is
gated as today; its tests gate turning green is the other half of
red-then-green.
Direction: **loosened** for `test` nodes, with proof — T3-7 shows the
contract rejected every legitimate `test` node, so the split ARCHITECTURE
documents was unexecutable; **tightened** in one place — a `test` node
whose tests already pass now fails (it specified nothing), where before it
failed for the wrong reason (mutation) and the reason was invisible.
Evidence: VERIFIED — T3-7 (three gates, none passable); `check_red_phase`'s
own docstring already says an `impl` node's "tests were written by the
test node it depends on and already fail at its baseline", i.e. the
design assumes a red spec exists; `_names_changed_source` ~:300 is the
existing greenfield rule to mirror for the collection-error case.
Issue: #44 (the split), #62 (property presence stays on the `test` node).
Steps:
1. `check_tests(test_command, exit_code, *, kind="impl", collected: int | None = None, output: str = "", workdir_modules: Collection[str] = ())`
   — keep the old signature working for every existing caller by
   defaulting `kind`; the `test` branch as in the contract. The runner
   already captures the suite (`run_shell_capture` ~:129); parse the
   collected count from pytest's summary line, and pass the workdir's
   module names (`sources.keys()` without `.py`) for the greenfield rule.
2. `check_red_phase`: first lines: `if kind == "test": return <mirror of the tests verdict>` — pass iff the tests check passed; detail "red by construction".
3. `run_tier1`: for `test` nodes replace the coverage and mutation entries with `GateCheck(name=..., passed=True, detail="not required: no source changed", basis="test node")`; the order pin stays eleven names.
4. `runner.py`: branch on `node.kind == "test"` to skip `covered_lines`, the baseline loop and `mutation_sample`; `Tier1Inputs` gets `covered=set()`, `baseline_exits=()`, `mutation=MutationOutcome(0, 0, 0, ())` for that kind (values the substituted checks never read).
5. Prompt sentence and ARCHITECTURE clauses.
Known-good: unit — `check_tests("pytest test_n.py", 1, kind="test", collected=1)` passes with the red-spec detail; `check_tests("pytest test_n.py", 2, kind="test", collected=0, output="ModuleNotFoundError: No module named 'm'", workdir_modules={"n"})` passes (greenfield); runner end-to-end — a `test` node whose new `test_n.py` asserts `f() == 2` against `n.py` returning 1 passes all eleven checks, with `tests`, `red-phase` reading red-spec, `coverage`/`mutation` "not required", `property-coverage` passing because the file also carries a `@given`; slice end-to-end — `t1` (test) then `n1` (impl, `GOOD_DIFF`'s `n.py` hunk only) both prove and the run reads `2 proven, 0 failed, 0 undispatched, merge exit 0`.
Known-bad: unit — `check_tests(..., 0, kind="test", collected=1)` fails "nothing specified"; `(..., 1, kind="test", collected=0)` fails; `(..., 2, kind="test", output="ModuleNotFoundError: No module named 'n'", workdir_modules={"n"})` fails (the module exists: a real collection error); `check_tests(..., 1)` with the default kind still fails as today; runner end-to-end — a `test` node whose test asserts `f() == 1` (already true) fails only `tests` and `red-phase`.
Contract mutants (write the exact seds when the code exists; unique targets or range-scoped; drop `__pycache__` after each revert):
1. the `test`-kind branch of `check_tests` accepts exit 0 → known-bad "nothing specified" red.
2. `check_red_phase`'s `test` branch returns pass unconditionally → the runner known-bad (`f() == 1`) red on red-phase count.
3. `run_tier1` substitutes "not required" for every kind, not only `test` → `test_run_tier1_all_green_passes`'s coverage detail pin red.
Done when: mutants red; `./check.sh` green; `grep -rn 'kind="test"' tests/test_runner.py tests/test_slice.py` finds the two fixtures. (Landed: three `kind="test"` fixtures in `tests/test_runner.py`; the slice fixture waits on T3-8.)
Stop if: `check_tests`'s signature at HEAD is not `(test_command, exit_code)` (~:120), or pytest's summary line is not parseable for the collected count with `--no-cov -q` (then count collected tests from `-rA` output instead and say so here).
Passing instance at HEAD: `test_run_node_gate_end_to_end_pass` (the `impl` path this must leave untouched); NONE for the `test` path, by T3-7.
Dry run: not run.

### T3-7b — Smoke run: one task through the harness on the container (sessions 20a, 20b)
Status 2026-09-19: DONE (session 20a; record `../saddle-bench/runs/smoke-2026-09-19/`,
untracked there). Outcome: known-bad — `1 proven, 1 failed, 0 undispatched,
merge exit 2`, run exit 1, 454 s wall. The planner split the task into
`node-1` (test) → `node-2` (impl). `node-1` proved on the live path: all
eleven gates, `tests: PASS (red specification: module 'src' does not exist
yet)`, `3 distinct of 3 sample(s)`. `node-2` applied its first sample and
failed two gates — `target-scope: FAIL (touched file(s) outside target_files:
src/__init__.py)` and `mutation: FAIL (no mutants decided)` — then every
recovery diff (16 of 16 `git apply` attempts, both nodes, all four
fallbacks) died with `patch fragment without header at line 3`. The merge
suite failed with `ModuleNotFoundError: No module named 'src'` although
the same tree passes `python -m pytest -q` (6 passed). Confirmed working:
T3-7a on the live path, T3-8 (every span names `refs/saddle/baseline/<node>`,
none `HEAD`), non-vacuous sampling, journal chain integrity.
Rerun (session 20b), after T3-17..T3-21 landed (6d43dd1, 3938d32 +
8ee25cc, 12ab1ed, 44c2538, 15a349f): same task, same scratch-repo shape in
a FRESH `/tmp/saddle-smoke` (delete the old one first), same record shape,
written to a NEW directory `../saddle-bench/runs/smoke-2026-09-19b/` —
never overwrite the first record. Expected: `N proven, 0 failed, 0
undispatched, merge exit 0`, and `saddle verify` on the journal agreeing
with the run's verdict (T3-21). Known open at run time: T3-12 has not
landed, so the planner may still name a package `src` or leave an
`__init__.py` out of `target_files`; if it does, the mutation gate now
reads `mutation tool failed: mutmut run exited 1: ...Module name starts
with "src."...` (T3-20) — record that as confirming T3-20 and T3-12, not as
a new finding.
Second outcome (session 20b, 2026-09-19; record
`../saddle-bench/runs/smoke-2026-09-19b/`, reviewer-verified against the
code): known-bad — `1 proven, 1 failed, 0 undispatched, merge exit 0`, run
exit 1, 554 s wall, 7 of 7 `git apply` spans exit 0 (20a: 16 of 16 failed).
Same split, `node-1` (test, REQ-001) → `node-2` (impl, REQ-002). `node-1`
proved first attempt, all eleven gates. `node-2` failed three attempts:
`coverage: FAIL (15.8% < 100.0%: uncovered .../test_n_req002.py:3, ...)`,
`node-scope: FAIL (impl node changed test file(s): .../test_n_req002.py)`,
`target-scope: FAIL (touched file(s) outside target_files:
test_n_req002.py)`, `requirement-binding: FAIL (undeclared requirements
cited: REQ-001)`. The replan produced `node-2.r1`, which failed three
attempts on `red-phase: FAIL (tests pass pre-change; prove nothing)` and
the same `requirement-binding` line. Confirmed live: T3-19 (the plan
targets the existing `n.py`; S6 gone), T3-21 (`saddle verify` prints
`Verdict: FAIL` for this failed run; 20a printed PASS), T3-8, T3-7a,
non-vacuous sampling, chain integrity. Not discriminated, and recorded as
such: T3-17 (merge exit 0, but both `pytest -q` and `python -m pytest -q`
pass on this tree, so S1's trigger never arose), T3-20 (no `mutmut run`
failed), T3-18 (one clean run is not proof the corrupt-patch class is
gone). Findings, each now an item: R1 a failed node's applied diffs stay
in the worktree, so its replacement snapshots a baseline that already
carries the work (`git show refs/saddle/baseline/node-2.r1:n.py` is the
implemented function; the red probes split 1,1,1,1,1,1,1 on `node-2`
against 0,0,0 on `node-2.r1`) and the merge suite ran green over unproven
edits → T3-23; R2 `requirement-binding` rejects an impl node whose
dependency's tests cite the dependency's requirement, so every test → impl
split with distinct ids fails on the impl node → T3-24; R3 `target_files`
omitted a file the model wrote (a second test file), the mirror of S3 →
T3-12 and T3-13, second instance; R4 which gates failed on `node-2`
attempt 1 cannot be recovered from the record (the seal keeps a count) →
T3-25. Also seen: `.coverage.tier1` is left untracked in the repo under
test after the run (evidence.py ~:323 keeps it out of the tree by design;
no item).
Rerun (session 20c), after the 20b findings landed — T3-23 `ae29677`,
T3-25 `ae9339f`, T3-24 `9b65f2a`, T3-12 `1efcc57` + `321d180` (each must
be an ancestor of HEAD; record the harness commit): same task, same
scratch-repo shape in a FRESH `/tmp/saddle-smoke` (delete the 20b
leftovers first), same record shape, written to a NEW directory
`../saddle-bench/runs/smoke-2026-09-19c/` — never overwrite 20a or 20b.
Expected: `N proven, 0 failed, 0 undispatched, merge exit 0`, run exit 0,
`saddle verify` printing `Verdict: PASS`. Confirm live, and say which
span or gate line shows it: R1 → T3-23 (if any node gives up, a
`restore-baseline` span follows and the replacement's
`refs/saddle/baseline/<node>.r1` equals the pre-node tree; with no
give-up, record "not discriminated"); R2 → T3-24 (the impl node's
`requirement-binding: PASS (... bound)` although its id is cited only by
the test node's file); R3 → T3-12 (the worker prompt names the node's
`target_files`; no file outside any node's list appears in the worktree);
R4 → T3-25 (any failed attempt's seal reads `attempt i/N: k gate(s)
failed: <names>`). Also record whether the planner obeyed the four rules
`321d180` added: the test node's spec cites every node's id, its
`target_files` names every test file, no package named `src`. Known open
at run time: nothing in the gates; a planner that ignores the cite rule
fails the impl node with `unbound requirements: REQ-00n` — record that as
T3-12's rule not followed, quote the plan, fix nothing.
Third outcome (session 20c, 2026-09-19; record
`../saddle-bench/runs/smoke-2026-09-19c/`, reviewer re-ran `saddle verify`
on the journal: 2 proofs, 65 spans, chain verifies): **known-good** —
`2 proven, 0 failed, 0 undispatched, merge exit 0`, run exit 0, 230 s
wall, `saddle verify` → `Verdict: PASS`. The first run of this item to
meet its known-good. Harness `321d180`. Same split, `node-1` (test) →
`node-2` (impl), both declaring `REQ-001, REQ-002`; `node-1` proved first
attempt, `node-2` on attempt 2 after `attempt 1/3: 1 gate(s) failed:
mutation` (R4 → T3-25 confirmed verbatim); 3 of 3 `git apply` clean.
R3 → T3-12 confirmed on the observable half (worktree holds exactly
`n.py`, `tests/test_n.py`; all four new planner rules obeyed). R2 → T3-24
NOT discriminated: the planner gave both nodes both ids, so no citation
was an orphan under the old rule either. R1 → T3-23 not discriminated (no
node gave up). Newly discriminated: T3-17 — on the final tree bare
`pytest -q` exits 2 (`No module named 'n'`) while `python -m pytest -q`
passes, and the merge suite ran the latter. Still undiscriminated: T3-18,
T3-20. Still open: T3-9 (`Task: (unknown)` in `saddle verify`). A first
attempt launched without `.venv/bin` on `PATH` (every gate tool exit 127,
each reported as an explicit `unavailable` FAIL) was discarded and kept
as `smoke-2026-09-19c-aborted-path/`; it measured nothing. Caveats for
anyone reading this as more than it is: one sample of a two-node toy task
on a 27B model; the mutation gate steered `node-2` to `return x + x` and a
bare `raise ValueError` to starve mutmut (recorded, no item).
Findings, each now an item (reviewer-verified against the code, 2026-09-19):
S1 merge suite and node gates disagree on `sys.path` → T3-17; S2 the
grammar lets a hunk follow `diff --git` with no `---`/`+++` lines → T3-18;
S3 `target_files` omitted the `__init__.py` the implementation needed →
T3-12; S4 `mutmut run` exited 1 in 658 ms and the gate said "no mutants
decided" — reproduced: mutmut 3.8.0 asserts `Module name starts with
"src.", which is invalid` for a package literally named `src`, and
`mutation_sample` discards the run's exit code → T3-20 and T3-12; S5
`saddle verify` prints `Verdict: PASS` for this failed run because the
verdict is recomputed from proof records alone, and `Task: (unknown)` → T3-21
and T3-9; S6 the planner never sees the repository listing
(`build_emit_prompt(task)` takes only the task) and built a parallel
`src/f.py` beside the existing `n.py` → T3-19; S7 step 1 said `saddle
verify --journal`, the CLI takes a positional → fixed below.
Files: none in this repo change. Outputs go to `../saddle-bench/runs/smoke-<date>/`
(the planner's DAG, the journal, the transcript, `saddle doctor` output, the
exit code); the reviewer adds the outcome to this item's Status line.
Contract: none — this is a measurement of whether the path every later item
is built on executes at all: planner emission → worker diff under the EBNF
grammar → Tier-1 gates → merge-suite. It exists because no run has happened
since `52a0e05`, the audit (§8) says the corrupt-patch class is still
representable under the grammar, and T3-7 was found by an executor building
a fixture, not by anyone running the harness. The next premise error of that
kind is cheapest to find here, before T3-3 and T3-9..T3-11 are written
against a path nobody has watched execute.
Preconditions: T3-7a landed (a `test` node can pass) and T3-8 landed
(until it does, every node after the first is gated against `HEAD`, so any
plan with two or more nodes fails `node-scope` on its second node — a
one-node task is the only smoke possible before T3-8, and it does not
exercise the test/impl split). Container healthy (`saddle doctor` green),
key in `SADDLE_VLLM_API_KEY`, user present; no source edits during the run.
Direction: none (no code change).
Evidence: VERIFIED — the T3-8 probe (its Evidence paragraph) shows the
second node's gates reading the first node's staged diff; T3-7 shows the
test path unexecutable at `dbf8062`.
Issue: none.
Steps:
1. A scratch repo outside this checkout (`/tmp/saddle-smoke`): `git init`,
   one committed file `n.py` reading `def f(x):\n    return x\n`, no test
   file. Task: "Make f(x) return twice x and raise ValueError when x is
   negative." `saddle doctor`, then `saddle dag "<task>"` (save stdout as
   `dag.txt`), then one `saddle run "<task>" --repo /tmp/saddle-smoke --yes`
   with the default merge command (save stdout+stderr as `run.log` and the
   exit code; copy `/tmp/saddle-smoke/.saddle/proofs.jsonl`), then
   `saddle verify <that copy>` (positional; save the transcript).
2. Record, per node: kind; whether a `test` node passed as a red
   specification (`tests` and `red-phase` details); whether the worker's
   diff applied (`git apply` exit); every failed gate's verbatim detail;
   wall-clock; and the merge exit.
3. Fix nothing during the session. Each failure becomes a finding line
   under this item; a failure that falsifies the premise of a later item
   becomes a note on that item, written by the reviewer.
Known-good: a run that ends `N proven, 0 failed, 0 undispatched, merge exit 0`.
Known-bad: any gate failure or `git apply` failure — recorded, not fixed.
Contract mutants: none (no code).
Done when: the run record exists under `../saddle-bench/runs/`; this item's
Status lists the outcome of steps 1–2 and the first failure's verbatim
detail, if any.
Stop if: `saddle doctor` is red (report; never restart the container); the
key is absent; T3-7a or T3-8 has not landed (then run the one-node variant
only, and say so in the record).
Passing instance at HEAD: none — that is the point of the run.
Dry run: not applicable.

**Audit 2026-09-18 (post T3-2), items T3-8 – T3-16.** An audit of the T2-2,
T3-1 (`c93cdab`) and T3-2 (`f262521`) changes found nine things no later
item covers (T3-7 above is a separate finding from the T3-3 session).
Ranked: **Major** — T3-8 (every node after the first is gated
against `HEAD`, not its own baseline), T3-9 (a proof record names neither
the task nor the node it proves), T3-10 (resume trusts the journal and never
looks at the worktree); **Medium** — T3-11 (replacement ids across resume),
T3-12 (the planner prompt contradicts the gates), T3-13 (`target_files`
admits spellings the gate can never match); **Minor** — T3-14 (rename case
untested), T3-15 (docs drift), T3-16 (three consistency fixes). Anchors are
as of `15e5c3e`. T3-4 owns `check_node_scope` and `run_tier1`; nothing
below edits them.

### T3-8 — Gate every node against its own baseline, not `HEAD` (Major)
Status 2026-09-19: DONE — executor (session 22) plus the reviewer (commit
below). The executor landed `snapshot_baseline` / `_ref_slug` /
`BASELINE_REF_PREFIX` in `evidence.py`, the `baseline` threading through
`_run_node`, `autofix`, `run_node_gate`, `_best_of_samples` and
`_evaluate_candidate`, six `snapshot_baseline` tests, the two-node
known-good and own-stray known-bad, `_two_module_repo` and a mutmut stub
for two modules, and the `t1` test → `n1` impl slice deferred from T3-7a
(passing). It stopped, correctly, on the T3-1 resume fixture: its `n2` was
a comment-only refactor (`TIDY_DIFF`) that passed only while gated against
`HEAD` and credited with `n1`'s `return 2`; against its own baseline the
diff has no statement line, so coverage has nothing to cover and mutation
nothing to mutate, and red-phase fails "no mutants decided". That verdict
is right; the fixture was the same class of defect this item fixes.
Reviewer: `n2` is now `REFACTOR_DIFF` (`return 2` → `return 1 + 1`; the
same test pins it, mutants relocated by `_refactor_mutmut`); the
comment-only diff stays as the known-bad
`test_run_slice_resumed_comment_only_refactor_proves_nothing`; the stub
helper is generalised to `_mutmut_stub(name → (file, removed line))`; and
the two-node test pins in the journal that every `git diff --name-only`
span for `n2` names `refs/saddle/baseline/n2` and never `HEAD`, which is
the only observable of autofix's ref (ruff is idempotent on an earlier
node's clean files, so no behavioural fixture separates the two).
Deviations from the steps, both reported by the executor: the snapshot is
guarded by `if baseline is None:` rather than inside `attempt == 1` (mypy
cannot narrow `str | None` across that join; same condition, same
recorder, still before `_best_of_samples`); a non-repo raises naming
`git … add -u -- .`, the first run, not `write-tree`. Three span pins
moved, not one: `pass_end_to_end`, `unappliable_diff_fails_without_checks`
and `distinct_unappliable_diffs_exhaust_attempts` count git spans and now
name the four snapshot argvs.
Mutants (pre-commit, exact replacement, tree hash identical after each
revert, caches dropped; `pytest tests/test_slice.py tests/test_evidence.py
-k "two_nodes or snapshot or distinct_unappliable or resum or own_stray or
test_node_then_impl" --no-cov`):
1. `return ref` → `return "HEAD"` — KILLED (5 tests).
2. `git("add", "-u", "--", ".")` → `-A` — KILLED (4; untracked artefacts enter the tree).
3. `_evaluate_candidate` gates without `baseline=` — KILLED (2).
4. `if baseline is None:` → `if True:` (snapshot every attempt) — KILLED (span parents).
5. `autofix(workdir, baseline=…)` → default — SURVIVED on behaviour, KILLED once the journal pin above landed.
6. in-place `run_node_gate` without `baseline=` — KILLED (3).
Issue: not opened by the executor (outward-facing) — T3-5 drafts it,
quoting the executor's two `git diff` outputs (`n2` against `HEAD`
carrying `n1`'s `return 2`; against its own ref, the comment only) and the
four failing-gate lines. Noticed: requirement binding is suite-granular —
a second test module's `REQ-` tag is charged to every node in the slice
(`_declares_both_requirements` in the fixture); T3-15 gains a line for
ARCHITECTURE.
Files: `src/saddle/evidence.py` (new `snapshot_baseline` beside
`git_added_files` ~:256, recorded through `_record` ~:41 like its
neighbours); `src/saddle/slice.py` (`_run_node` ~:336: the snapshot in
attempt 1; `autofix(workdir, recorder=recorder)` ~:401 and
`run_node_gate(node, workdir, recorder=recorder, capture=captured)` ~:403
gain `baseline=`; `_evaluate_candidate` ~:262 and its
`run_node_gate(node, candidate)` ~:289 gain a `baseline` parameter;
`_best_of_samples` ~:293 threads it); `src/saddle/runner.py` (no change:
`run_node_gate(..., baseline="HEAD")` ~:101 already takes the ref);
`tests/test_evidence.py` (helper tests beside `_git_repo` ~:42);
`tests/test_slice.py` (two-node test beside `_slice_repo` ~:87 /
`GOOD_DIFF` ~:103; the span pin in
`test_run_slice_propose_error_seals_worker_span` ~:733 moves if it counts
`git` spans — `tests/test_runner.py::test_run_node_gate_records_tool_spans`
~:143 does not, the runner's own span list is unchanged).
Contract: every gate run for a node — `autofix` scoping, `run_node_gate` in
place, `_evaluate_candidate` on a copy — diffs against a snapshot of the
worktree taken in that node's first attempt before any proposal is drawn,
never against `HEAD`. The snapshot is a commit object at
`refs/saddle/baseline/<slug>` (`<slug>` is the node id when
`git check-ref-format refs/saddle/baseline/<id>` accepts it, else the
first 16 hex of the id's sha256), built as `git add -u -- .` (tracked
files only, so `.coverage.tier1` and `__pycache__` stay out),
`git write-tree`, `git commit-tree <tree> -p HEAD -m "saddle baseline <id>"`,
`git update-ref <ref> <commit>`. `HEAD`, every branch and the index's
tracked set are untouched. A node's coverage, red-phase, node-scope,
target-scope and mutation sample therefore see only that node's own diff;
a single-node slice is gated exactly as before (its snapshot tree is
`HEAD`'s tree).
Direction: **scope narrowed**, with proof. The probe below rejects a
legitimate input: node `n2`, `target_files=["m.py"]`, whose diff touches
only `m.py`, fails `target-scope: FAIL (touched file(s) outside
target_files: n.py)`, `coverage: FAIL (0.0% < 100.0%: uncovered .../n.py:2)`
and `red-phase: FAIL`, because `n1`'s proven edit to `n.py` is still staged
when `n2` runs (2026-09-18, scratch run of the fixture in step 4). No gate
accepts anything it rejected correctly before.
Evidence: VERIFIED — `_run_node` applies, autofixes, gates and seals but
never commits (~:395-412), so `git` sees every proven node's edit as
staged; `run_node_gate` defaults `baseline="HEAD"` (runner.py ~:101) and
all three callers in `slice.py` take the default (~:289, ~:401, ~:403).
The snapshot shape was verified in a scratch repo: after `write-tree` /
`commit-tree -p HEAD` / `update-ref`, `git diff --name-only <ref>`,
`git diff --no-renames --diff-filter=A --name-only <ref>` and
`git archive <ref>` all resolve, with `git rev-parse HEAD` and
`git status --porcelain` unchanged. `_evaluate_candidate` copies `.git`
(~:280), so the ref resolves in the candidate tree too.
Issue: none — open one: "Every node after the first is gated against HEAD,
not its own baseline", quoting the probe output; cross-reference #64
(target-scope is what makes the pollution visible).
Steps:
1. `snapshot_baseline(cwd: Path, node_id: str, *, recorder=None) -> str`
   in `evidence.py`: the four `git` runs above through `_record`; raise
   `RuntimeError` naming the failing argv on a non-zero exit; the last
   line is `    return ref` (mutant 1 targets it). `_ref_slug(node_id)`
   beside it.
2. `_run_node`: `baseline: str | None = None` before the loop (~:357);
   inside the `attempt == 1` branch (~:371), before `_best_of_samples`,
   `baseline = snapshot_baseline(workdir, node.id, recorder=recorder)` —
   under that attempt's recorder so the spans link to a sealed attempt
   (`_evaluate_candidate`'s docstring explains why an orphan span is
   unacceptable). Attempts 2..N keep the same ref: a recovery diff lands on
   the previous attempt's tree and the proof is the accumulated diff
   (`"\n".join(applied)` ~:407).
3. Pass `baseline=baseline` to `autofix` ~:401 and `run_node_gate` ~:403;
   give `_best_of_samples` and `_evaluate_candidate` a `baseline: str`
   parameter and call `run_node_gate(node, candidate, baseline=baseline)`
   (keep that spelling, mutant 3 targets it).
4. Fixture for the slice test: `_slice_repo` plus `m.py`
   (`def g():\n    return 1\n`) and `test_m.py` (`from m import g\n\n\ndef
   test_g():  # REQ-002\n    assert g() == 2\n`) committed at baseline;
   `n1` = `_node_dict("n1", [])` with `GOOD_DIFF`; `n2` = `_node_dict("n2",
   ["n1"])` with `test_command="pytest test_m.py"`, `target_files=["m.py"]`
   and `GOOD_DIFF` rewritten for `m.py`/`g`. Both nodes declare `REQ-001`
   and `REQ-002`: requirement-binding is suite-granular (runner.py
   ~:110-112 — every discovered test source counts once the suite flips),
   so the `REQ-002` tag in `test_m.py` is "cited" for `n1` too, and a
   fixture that declares it on `n2` only fails `n1` with "undeclared
   requirements cited: REQ-002" (the probe did exactly this first). The
   proposer dispatches on `node.id` and records each call.
Known-good: (a) the two-node slice passes with both nodes proven, the run
span reads `2 proven, 0 failed, 0 undispatched, merge exit 0`, and the
proposer was called once for `n2`; (b) every existing single-node
`tests/test_slice.py` case passes unchanged; (c) `snapshot_baseline` on a
repo with a staged new file and an unstaged edit to a tracked file:
`git diff --name-only <ref>` is empty, `git archive <ref>` carries both
files at worktree content, `git rev-parse HEAD` is unchanged; (d) a node
id `git check-ref-format` rejects (`"a b"`) still gets a resolvable ref.
Also, deferred from T3-7a: the two-node slice `t1` (`kind="test"`, creates
`test_n.py` with a `@given` test asserting `f() == 2`) then `n1` (`impl`,
depends on `t1`, `GOOD_DIFF`'s `n.py` hunk) both prove and the run reads
`2 proven, 0 failed, 0 undispatched`; `_slice_repo` without `test_n.py` at
baseline for this test only. At HEAD it fails `node-scope` on `n1` — the
probe this item exists for.
Known-bad: (e) an `n2` whose diff also edits `n.py` fails `target-scope`
naming `n.py` — the gate still sees the node's own stray, just not `n1`'s;
(f) an untracked `.coverage.tier1` in the worktree is absent from
`git archive <ref>`; (g) `snapshot_baseline` on a directory that is not a
repo raises `RuntimeError` naming `git write-tree`.
Contract mutants (each target occurs once; abort if `grep -c` is not 1; drop `__pycache__` after each revert):
1. `sed -i 's/^    return ref$/    return "HEAD"/' src/saddle/evidence.py` → `pytest tests/test_slice.py -k two_nodes -q --no-cov` red (`n2` fails `target-scope` naming `n.py` again).
2. `sed -i 's/"add", "-u", "--", "."/"add", "-A", "--", "."/' src/saddle/evidence.py` → `pytest tests/test_evidence.py -k snapshot -q --no-cov` red (the untracked file lands in the archive).
3. `sed -i 's/run_node_gate(node, candidate, baseline=baseline)/run_node_gate(node, candidate)/' src/saddle/slice.py` → `pytest tests/test_slice.py -k two_nodes -q --no-cov` red: the first sample scores `target-scope` red against `HEAD`, so `_best_of_samples` never takes its `failures == 0` exit (~:319) and draws all three, and known-good (a)'s call count for `n2` is 3, not 1.
Done when: mutants red; `./check.sh` green; `git for-each-ref refs/saddle/`
after the two-node test lists two refs.
Stop if: `_run_node` already commits between nodes on this branch (it does
not at `15e5c3e`), or `git diff --name-only <ref>` fails to list a staged
new file (it lists it: tracked-ness comes from the index, not the ref).
Passing instance at HEAD: `test_run_slice_resumes_a_verified_journal_and_reuses_its_proofs`
(two nodes through `run_slice` and `_run_node` across two runs) and
`test_run_node_gate_target_files_binds_end_to_end` (the `target-scope` path).
Dry run: not run (the probe fixture was run in a scratch copy at `15e5c3e`, not the fix).

### T3-9 — A proof record names the task and the node it proves (Major)
Status 2026-09-19: DONE by session 23, commit 64ff581 (tightened).
Landed: `ProofRecord` gains `task_hash`, `node_hash`, `kind`,
`target_files` (defaulted, sealed inside the hash; older journals still
parse and re-verify because verification dumps with `exclude_unset`);
`hash_node`, `proven_records`, `build_record`/`build_from_gate` seal the
four; `slice._seed_proofs` reuses a proof only for the same task and the
same node hash -- a record sealed for another task raises `ValueError`
before any node runs (the CLI prints `error: ...` and exits 1), a record
whose node changed or that predates `node_hash` is dropped and the node
rescheduled, and a `resume` span under the run span records the decision
(`is_run_end` does not match it, pinned). Tests: four in test_journal
(seals, altered task_hash fails verification, `hash_node` pinned to a
fixed digest, `proven_records` last-per-node) and four in test_slice
(resume span names reuse, different-task refusal, node-changed
reschedule, pre-node_hash record dropped). Mutants, all KILLED: M1
`"task_hash": task_hash` -> `""` (`assert '' == 'aaa...'`); M2 node-hash
compare -> `True` (edited n1 reused, `assert [] == ['n1']`); M3 task
guard -> `False` (`DID NOT RAISE ValueError`). Reviewer verified at
HEAD: the three targets each occur once, the commit is clean-room, and
`./check.sh` on the settled tree is green (649 passed, 3 skipped, 100%).
Deviations, stated by the executor and accepted: known-bad (a) shown via
M3 rather than pre-change in order; known-bad (b) proved with `n1` as a
refactor in both runs because an impl node cannot be red-phase-red on a
green worktree; `build_record` takes `Sequence[str] = ()` (ruff B006);
`_run_node`'s `task_hash` is keyword-only and required; the optional
transcript display was not taken. Issue #67 "Resume reuses proofs across
tasks and node edits" opened by the reviewer.
Note for the record: sessions 23 and 37 shared one worktree; each used
byte-copy restore instead of `git checkout --` and both re-ran the gate
on a settled tree. Two sessions in one checkout is not to be repeated.
Files: `src/saddle/journal.py` (`ProofRecord` ~:41: four new fields;
`build_record` ~:112: parameters and payload; `build_from_gate` ~:383:
computes and passes them; new `proven_records(path) -> dict[str,
ProofRecord]` beside `rebuild_proven` ~:341, which keeps its contract —
`test_rebuild_proven_maps_nodes_to_hashes` ~:299 is the pin);
`src/saddle/slice.py` (`run_slice` ~:597: the seed; `_run_node` ~:406:
passes `task_hash` to `build_from_gate`; `run_slice` already receives
`task` ~:571); `src/saddle/transcript.py` (`render_journal_transcript`
~:138: show `kind` and `target_files` when set — optional);
`tests/test_journal.py` (beside `test_pre_basis_journal_still_verifies`
~:147); `tests/test_slice.py` (beside the resume tests ~:1226-1284).
Contract: every proof record carries `task_hash` (sha256 of the `task`
string `run_slice` received), `node_hash` (sha256 of
`node.model_dump_json()` for the node as validated — the original,
not the dependency-stripped scheduler copy the `worker` closure ~:606
already swaps out), and readable `kind` and `target_files`, all inside
the hashed payload. On resume, `run_slice` reuses a journal proof for
node id `X` only when the record's `node_hash` equals the current DAG's
`X` and its `task_hash` equals the current task's. A candidate record
whose non-empty `task_hash` differs from the current task's raises
`ValueError` before any node runs, naming both hashes and pointing at
`--journal` (cli.py ~:620) for a fresh path; a record whose node changed,
or that predates these fields, is not reused and the node is scheduled
again. When the journal held any proof, the seed's outcome is sealed as
one agent span named `resume` under the run span (`detail`: `reused n1;
dropped n2 (node changed)`) so `saddle tail` and the transcript show what
resume did; `is_run_end` (transcript.py ~:70) matches `run` only, so tail
keeps following.
Direction: **tightened** (T3-1 loosened the refusal; this binds what it
lets through to the task and node it was sealed for). Old journals still
verify: the fields default to `""`/`[]`, verification hashes with
`exclude_unset=True` (journal.py ~:270) and
`test_pre_basis_journal_still_verifies` is the pin.
Evidence: VERIFIED by reading — `ProofRecord` ~:41-53 holds `node_id`,
`diff_hash`, `parent_proofs`, `gate_outputs`, `requirement_ids`,
`thinking`, `attempts` and nothing that names the task or the node's
content; `run_slice` ~:597 seeds `proofs = rebuild_proven(journal_path)`
with no comparison, so a journal from task A run against task B's DAG
counts every same-id node as proven and never schedules it, and the CLI
default journal is per repo (cli.py ~:720), so that is the default flow
for a second task. Write known-bad (a) first and watch it pass (the
defect) before changing anything.
Issue: none — open one: "Resume reuses proofs across tasks and node edits";
T3-10 is its second half.
Steps:
1. `ProofRecord`: `task_hash: str = ""`, `node_hash: str = ""`, `kind: str =
   ""`, `target_files: list[str] = Field(default_factory=list)`.
2. `build_record`: keyword parameters with the same defaults, written into
   `payload` after `"attempts"` — the hash covers them.
3. `build_from_gate(node, ..., task_hash: str = "")`: `node_hash =
   hashlib.sha256(node.model_dump_json().encode()).hexdigest()`,
   `kind=node.kind`, `target_files=list(node.target_files)`.
4. `journal.proven_records(path)`: last verified record per node id, same
   corruption policy as `rebuild_proven` (share `_verified_records` ~:335).
5. `run_slice`: `task_hash = hashlib.sha256(task.encode()).hexdigest()`;
   `expected = {node.id: <node_hash> for node in dag.nodes}`; seed `proofs`
   from `proven_records` with the two predicates spelled
   `record.node_hash == expected.get(record.node_id)` and
   `record.task_hash and record.task_hash != task_hash` (mutants 2 and 3
   target these); the `resume` span via `append_span`/`build_span` with
   `parent_id=run_span_id` like `merge-suite` ~:650.
Known-good: `_first_run` then the T3-1 resume test unchanged (same task,
same `n1` → reused, not re-proposed) and a `resume` span with detail
`reused n1`; a record built by `build_from_gate` has 64-hex `task_hash`
and `node_hash` and `kind == "impl"`; `test_pre_basis_journal_still_verifies`
still passes; `saddle verify` on a journal written before this item still
prints `chain verifies`.
Known-bad: (a) `_first_run` with task `"Fix f."`, then `run_slice("Fix g.",
...)` on the same journal → `ValueError` naming both task hashes, no
proposer call; (b) `_first_run`, then the same task with `n1`'s
`task_prompt` changed → `n1` is proposed again, the journal gains a second
`n1` record with a different `node_hash`, the `resume` span reads
`dropped n1 (node changed)`; (c) a sealed record with `task_hash` altered
via `model_copy` fails `verify_journal` (the field is inside the hash).
Contract mutants (each target occurs once; abort if `grep -c` is not 1; drop `__pycache__` after each revert):
1. `sed -i 's/        "task_hash": task_hash,/        "task_hash": "",/' src/saddle/journal.py` → `pytest tests/test_journal.py -k task_hash -q --no-cov` red (the record no longer carries the task; (c) stops firing).
2. `sed -i 's/record.node_hash == expected.get(record.node_id)/True/' src/saddle/slice.py` → `pytest tests/test_slice.py -k node_changed -q --no-cov` red (the edited `n1` is reused).
3. `sed -i 's/record.task_hash and record.task_hash != task_hash/False/' src/saddle/slice.py` → `pytest tests/test_slice.py -k different_task -q --no-cov` red (task B resumes on task A's journal).
Done when: mutants red; `./check.sh` green.
Stop if: `model_dump_json()` for the same node differs across the pydantic
pinned in the lock file and its next minor (pin one hash in a test and
check it survives `uv sync`); then hash `json.dumps(node.model_dump(mode="json"),
sort_keys=True)` instead.
Passing instance at HEAD: `test_run_slice_resumes_a_verified_journal_and_reuses_its_proofs`
(the seed path) and `test_pre_basis_journal_still_verifies` (the compat path).
Dry run: not run.

### T3-10 — Resume checks the worktree it resumes onto (Major)
Status 2026-09-19: DONE by session 24, commit 206894e (tightened).
Landed: each proof seals `tree_hash` (the `git write-tree` id of the
tracked worktree its gate passed on), kept at `refs/saddle/proven/<slug>`
(`proven_ref`, `PROVEN_REF_PREFIX` in evidence.py; `snapshot_tree` is the
shared helper, commit message now `saddle snapshot <ref>`); a resume
whose worktree hashes to anything else raises `ValueError` before any
node runs, naming both ids and the `git restore --source` that puts the
proven tree back; `saddle run` reports it as `error:` exit 1;
ARCHITECTURE.md:227 says what resume checks. Mutants, both KILLED: M1
`if current != expected` -> `if False` (two resume-tree tests `DID NOT
RAISE`); M2 `"tree_hash": tree_hash` -> `""` (`assert '' == 'bbb...'`).
Reviewer verified at HEAD: both targets occur once, clean-room, and
`./check.sh` on the settled tree green (see the commit that carries this
line). Corrections to this item, found by the executor: the reproduce
command below (`git checkout -- n.py`) does not provoke the defect -- a
proven edit is staged, so an index checkout restores it; the known-bad
uses `git checkout HEAD -- n.py`, which is what a user reaches for after
`_ensure_clean` refuses a crashed run's tree. The test docstring that
still said the old command was fixed by the reviewer in the next commit.
Two existing pins were extended, not flipped: the end-to-end tool-span
sequence (four more `git` spans) and the two-node ref set (`proven/n1`,
`proven/n2`). Known limit, stated by the executor: a resume that reuses
some proofs and drops others (a changed node beside an unchanged one)
finds the dropped node's edits still in the worktree, so the tree does
not hash to the reused proof's `tree_hash` and the run stops; the escape
is a fresh `--journal`. No test reaches it; T3-11 should decide whether
that flow deserves its own message.
Files: `src/saddle/evidence.py` (T3-8's helper generalised to
`snapshot_tree(cwd, ref, *, recorder)`; `snapshot_baseline` calls it);
`src/saddle/journal.py` (`ProofRecord`/`build_record`/`build_from_gate`:
`tree_hash: str = ""`, as T3-9's steps 1-3); `src/saddle/slice.py`
(`_run_node` ~:404: the snapshot after `if result.passed:` and before
`build_from_gate`; `run_slice` ~:597: the check after the seed);
`src/saddle/cli.py` (`_ensure_clean` ~:314 docstring: the resume flow);
`docs/ARCHITECTURE.md:227` (with T3-15); `tests/test_journal.py`;
`tests/test_slice.py` (beside the resume tests ~:1226-1284).
Contract: a proof record carries `tree_hash`, the `git write-tree` id of
the worktree's tracked files (after `git add -u -- .`, as in T3-8) taken
after the gate passed and before the record is sealed, and the same tree
is kept at `refs/saddle/proven/<slug>`. On resume, after T3-9's seed,
`run_slice` computes the current tree the same way (no ref) and requires
it to equal the `tree_hash` of the last reused proof in journal order;
otherwise it raises `ValueError` before any node runs, naming both ids
and the restore command
`git restore --source refs/saddle/proven/<id> --staged --worktree -- .`.
A committed proven state matches (a commit does not change the tree id).
The CLI flow after a crash: `_ensure_clean` (~:314) refuses the dirty
tree, the user commits the staged proven edits (`git add -u && git commit`)
and runs again; the check passes.
Direction: **tightened**. Nothing that resumes correctly today stops
resuming; a resume onto a tree that is not the proven one now fails
instead of gating new nodes against unproven code.
Evidence: VERIFIED by reading — `run_slice` ~:597-604 seeds from the
journal and reads nothing from the worktree; `rebuild_proven`'s docstring
(journal.py ~:344-345) promises "a crash therefore loses at most the
in-flight node", which holds only while the worktree still carries the
proven edits. Reproduce: `_first_run`, `git checkout -- n.py` (n1's edit
gone), resume with a dependent `n2`: `n2` is gated on a tree without
`n1`'s change and the run seals `2 proven`.
Issue: the T3-9 issue, second half.
Steps: 1. field, parameter and payload entry `"tree_hash": tree_hash,`
(mutant 2 targets it). 2. `_run_node` after `if result.passed:`:
`tree = snapshot_tree(workdir, f"refs/saddle/proven/{_ref_slug(node.id)}",
recorder=recorder)`; pass `tree_hash=tree` to `build_from_gate`. 3.
`run_slice`, after the seed, when at least one proof was reused:
`expected = <last reused record>.tree_hash`; `current = <git add -u;
git write-tree>`; `if current != expected:` (keep that spelling, mutant 1
targets it) raise with both ids and the command. Records with an empty
`tree_hash` are never reused (T3-9 already drops them: their `node_hash`
is empty too).
Known-good: `_first_run` then the T3-1 resume test on the untouched
worktree → resumes; the same with `git add -u && git commit -m proven`
between the runs → resumes (tree id equal); the record's `tree_hash`
equals `git write-tree` run by the test after the first run.
Known-bad: `_first_run`, then `git checkout -- n.py` → `ValueError` naming
both tree ids and the restore command, no proposer call; `_first_run`,
then an edit to an unrelated tracked file → `ValueError` (strict equality:
the proof is about one tree; a fresh `--journal` is the escape hatch); a
sealed record with `tree_hash` altered via `model_copy` fails
`verify_journal`.
Contract mutants (each target occurs once; abort if `grep -c` is not 1; drop `__pycache__` after each revert):
1. `sed -i 's/    if current != expected:/    if False:/' src/saddle/slice.py` → `pytest tests/test_slice.py -k resume_tree -q --no-cov` red (the reverted worktree resumes).
2. `sed -i 's/        "tree_hash": tree_hash,/        "tree_hash": "",/' src/saddle/journal.py` → `pytest tests/test_journal.py -k tree_hash -q --no-cov` red.
Done when: mutants red; `./check.sh` green; `docs/ARCHITECTURE.md:227` says
what resume checks.
Stop if: `git restore --source <ref> --staged --worktree -- .` does not
recreate a file the ref has and the worktree lacks (check on the installed
git before writing it into the error message), or T3-8/T3-9 have not
landed (this reuses their helper and field plumbing).
Passing instance at HEAD: `test_run_slice_resumes_a_verified_journal_and_reuses_its_proofs`
(the seed path this extends).
Dry run: not run.

### T3-11 — Replacement ids are unique across the journal (Medium)
Status 2026-09-19: session 25 STOPPED before editing, correctly: point
(2) as first written claimed `_seed_proofs` already emits `dropped n2.r1
(not in DAG)` and "needs only its test". It does not -- the executor ran
`_seed_proofs` against a synthetic journal (`n1` proven, `n2.r1` proven,
DAG `{n1, n2}`) and got `dropped n2.r1 (node changed)`: T3-9's filter
has no branch for an id the DAG lacks, so it falls into the catch-all.
Item corrected below: (2) now authorises that branch in `_seed_proofs`
(the drop itself was already right; only the reason string changes), and
the line numbers are current at 6e39747. Re-run as session 25b.
Session 25b STOPPED again, also correctly: step 1 as written makes the
DAG-collision raise in `splice_replan` unreachable, and
`test_splice_replan_rejects_collision` pinned that raise; the item
authorised neither its removal nor a flip. Reviewer landed the item in
the main session (contract change): `splice_replan(..., taken=())`
numbers each replacement past `dag.nodes | taken` with `while candidate
in existing:`; `run_slice` passes the journal's record ids; the
collision raise is gone and its test is flipped to
`test_splice_replan_numbers_past_ids_the_dag_already_holds` --
`flip:` evidence: `a.r1` is a legal, non-duplicate node id (`dag.py`
`_duplicate_ids` guards duplicates on its own), so the raise refused a
legitimate input and protected no other contract; the flipped test now
pins `a.r2`. `_seed_proofs` gained the `(not in DAG)` branch. Known-bad
shown red first: the new tests on the old source raise `collides` and
produce a second `n1.r1`. Mutants, all KILLED: M1 `while candidate in
existing:` -> `while False:` (`['a.r1'] == ['a.r2']`); M2 `elif
record.node_id not in expected:` -> `elif False:` (span reads `(node
changed)`); M3 `splice_replan(remaining, node_id, new, taken=taken)`
without `taken` (`['n1.r1'] == ['n1.r2']`). check.sh 657 passed, 100%.
Tests: `test_splice_replan_numbers_past_taken_ids`,
`test_run_slice_replan_across_resume_numbers_past_sealed_ids`.
Files: `src/saddle/slice.py` (`splice_replan` ~:576: `mapping` numbers
`.r<index>` from 1 and checks collisions against the current DAG only,
~:584-585; `run_slice` ~:745: `replanned_from`/`generated` start empty
on every run; `_seed_proofs` ~:698-742: the reuse filter, whose `else`
branch reads `(node changed)` for every non-matching hash, including an
id the DAG does not contain); `tests/test_slice.py`
(beside `test_splice_replan_rejects_collision` ~:996 and
`test_run_slice_replan_recovers_failed_node` ~:1051; `_first_run` ~:1210).
Contract: (1) a replacement id is unique across the journal, not just the
run: `splice_replan` takes the ids already sealed in the journal and
numbers past them, so a resumed run that replans `n2` after an earlier run
sealed `n2.r1` produces `n2.r2`; (2) a journal proof whose id is not in
the DAG being run (`n2.r1` from an earlier run's replan) is never reused —
T3-9's `node_hash` comparison has no DAG node to compare it with, and
today that case falls into the `(node changed)` catch-all — and the
`resume` span lists it as `dropped n2.r1 (not in DAG)`, so a reader can
tell a replan leftover from an edited node.
Direction: **tightened** for (1) (ids that could collide no longer can);
(2) keeps T3-9's drop and only names its reason (span text, tightened).
Evidence: VERIFIED by reading — `generated` and `replanned_from` are fresh
sets at ~:602-603 and `splice_replan`'s collision check ~:457 sees only
`dag.nodes`, so a second run restarts at `.r1`; `rebuild_proven` keeps the
*last* record per id (~:347), so a second `n2.r1` shadows the first in
`saddle verify` output and in the transcript's `sealed` map (~:645).
Issue: none.
Steps: 1. `splice_replan(dag, failed_id, new, *, taken: Collection[str] =
())`: `existing = {node.id for node in dag.nodes} | set(taken)`; number
each replacement with the smallest index whose id is not in `existing`
(spell the loop `while candidate in existing:`, mutant 1 targets it).
`run_slice` passes `taken={record.node_id for record in
read_records(journal_path)}`. 2. In `_seed_proofs`, after the reuse
branch add `elif record.node_id not in expected:` appending `dropped
{record.node_id} (not in DAG)`, before the `no node hash` branch (an
absent id is more specific than a missing hash). Test (2): `_first_run`, append a
`build_from_gate` record for a node `n2.r1` with T3-9's fields, resume
with DAG `{n1, n2}` where `n2` fails once and the replanner returns one
replacement: `n2.r2` is proposed, the `resume` span reads `dropped n2.r1
(not in DAG)`, and the journal has exactly one record per id.
Known-good: a replan in a fresh journal still yields `n2.r1`
(`test_run_slice_replan_recovers_failed_node` unchanged); a resumed replan
after a sealed `n2.r1` yields `n2.r2`.
Known-bad: with the old numbering the journal holds two `n2.r1` records —
the test asserts one record per id.
Contract mutants (each target occurs once; abort if `grep -c` is not 1; drop `__pycache__` after each revert):
1. `sed -i 's/while candidate in existing:/while False:/' src/saddle/slice.py` → `pytest tests/test_slice.py -k replan_across_resume -q --no-cov` red (two `n2.r1` records).
2. `sed -i 's/elif record.node_id not in expected:/elif False:/' src/saddle/slice.py` → same test red (span reads `(node changed)`).
Done when: mutant red; `./check.sh` green.
Stop if: `_seed_proofs` at HEAD already has a `not in expected` branch
(then step 2's source change is done and only its test is owed).
Passing instance at HEAD: `test_run_slice_replan_recovers_failed_node` (a replan
through `splice_replan` and back into the loop).
Dry run: not run.

### T3-12 — The planner prompt says what the gates enforce (Medium)
Status 2026-09-19: DONE in two commits. 1efcc57 (executor, session 27)
landed steps 1–3 exactly as written: both sentences present, `never use
"/"` absent, mutants 1–2 red (reviewer re-ran both at HEAD), check.sh
632/100%. The executor declined the four "Added 2026-09-19" rules below
for lacking pinned sentences and a named mutant; the main session landed
them in the follow-up commit (prompt-only, tightened): the planner prompt
gains `Never name a package "src"` (under the file-ownership rule), the
cite-every-node rule (after the REQ- id rule), and two `target_files`
sentences (created files incl. `__init__.py`; a test node names every test
file it writes). The worker prompt now carries the node's `target_files`
too — "Touch only these files: ...; the gate rejects a diff that names any
other file" — since R3 was a worker that had never seen the list; absent
when the list is empty (both halves tested). Five contract mutants, each
deleting one sentence (or the worker's `{scope}` slot) by exact
replacement with a count check, all red against
`pytest tests/test_cli.py -k "emit_prompt or worker_prompt"`. The same
commit rewrites ARCHITECTURE gate 5 (T3-15's T3-8 addendum, updated for
T3-24): binding is a citation in some discovered test source, suite-
granular, orphan = an id no node of the plan declares.
Files: `src/saddle/cli.py` (`build_emit_prompt`: the refactor rule
~:110-111; the `target_files` rule ~:129-131 — after T3-4's edit at ~:128);
`tests/test_cli.py` (`test_build_emit_prompt_names_task_and_rules` ~:241,
its `target_files` assertion ~:253;
`test_emit_prompt_asks_for_the_test_impl_split` ~:2079).
Contract: the emission prompt states (1) that a `refactor` node may not
create or rename files — `check_node_scope` fails "refactor node added
file(s): ..." (gates.py ~:465), and a staged `git mv` is an add to the
gate (T3-14); and (2) that a `target_files` entry looks like the example:
"never start an entry with "/" and never use ".."" — replacing the current
`never use "/" or ".." in an entry`, which forbids the slash its own
example `["src/app/login.py"]` contains.
Direction: prompt-only, no gate contract changes. The prompt is the only
place the planner learns a rule; a rule the gate enforces and the prompt
withholds is a plan the planner keeps emitting and the gate keeps
rejecting (#57 was this shape for the test/impl split).
Evidence: VERIFIED — `cli.py:110-111` describes `refactor` as "the code and
its tests move together" and says nothing about files; `cli.py:130` reads
`never use "/" or ".." in an entry` one line after an example with two
slashes. No run record shows a planner obeying the literal rule yet (T4
predates `target_files`); T4-1's rerun will.
Issue: none; note it on #64.
Steps: 1. After the refactor sentence: "A refactor node may not create or
rename files; it edits existing code and tests in place." 2. Replace the
`target_files` rule's second sentence with: "Entries look like the example:
never start one with "/" and never use ".."." 3. Tests: assert both new
sentences are in `build_emit_prompt("x")` and that `never use "/"` is not.
Known-good: the prompt contains `may not create or rename files` and
`never start one with "/"`. Known-bad: the prompt contains `never use "/"`
— a prompt has no input to reject, so the absence assertion is the
discriminating half.
Contract mutants (each target occurs once; abort if `grep -c` is not 1; drop `__pycache__` after each revert):
1. `sed -i 's/A refactor node may not create or rename files; //' src/saddle/cli.py` → `pytest tests/test_cli.py -k emit_prompt -q --no-cov` red.
2. `sed -i 's/never start one with "\/" and never use ".."\./never use "\/" or ".." in an entry./' src/saddle/cli.py` → same command red (the old wording is back; `grep -c 'never use "/"' src/saddle/cli.py` prints 1 before the run).
Done when: mutants red; `./check.sh` green.
Stop if: T3-4 has rewritten these lines into a per-tool list — then put the
two sentences where T3-4 put the kind and `target_files` rules.
Passing instance at HEAD: `test_build_emit_prompt_names_task_and_rules`.
Dry run: not run.
Added 2026-09-19 (smoke run S3, S4): two more rules the prompt must state.
(1) `target_files` lists every file the node will create as well as edit,
including a new package's `__init__.py` — `node-2` declared `src/f.py`,
needed `src/__init__.py`, and `target-scope` correctly failed it. (2) Never
name a package `src`: mutmut 3.8 refuses to instrument it (`Module name
starts with "src.", which is invalid`) and the mutation gate then fails
with no mutants decided (T3-20 makes that failure name itself). Known-bad
for both: the smoke record's `dag.txt`.
Added 2026-09-19 (smoke rerun 20b, R2 and R3; after T3-24 landed as
9b65f2a): two more. (3) A `test` node's specification cites, on its tests,
the requirement ids of every node it specifies — the impl node that makes
it pass declares its own id and the binding gate's unbound half requires
some test to cite it; in 20b `node-1`'s `test_n.py` cited `REQ-001` only,
so with T3-24 in place `node-2` (`REQ-002`) would now fail "unbound
requirements: REQ-002" instead of the orphan it failed then. Citing an id
another node of the plan declares is allowed since 9b65f2a; citing one no
node declares is not. (4) `target_files` of a `test` node names every
test file it will write, and the worker is told the gate rejects any
other — `node-2` in 20b wrote a second file `test_n_req002.py` that no
node listed (R3, the mirror of 20a's S3). Known-bad for both: the 20b
record's `dag.txt` and `gates-failed.txt`. Tests: the two new sentences
present, the mutant shape as above (delete each sentence → red).

### T3-13 — `target_files` spellings the gate can never match (Medium)
Status 2026-09-19: DONE by session 26, commit 66f2c9a (tightened).
`_repo_relative_posix` rejects any `/`-separated segment that is `""`,
`"."` or `".."` (subsumes the old `startswith("/")` clause). Known-good
includes `.github/x.yml`; known-bad adds `./n.py`, `a//b.py`,
`src/./x.py`, `dir/`. Both mutants KILLED (`("..",)`: 5 failed;
`("", ".")`: 2 failed). Reviewer verified at HEAD; check.sh 661 passed.
Files: `src/saddle/dag.py` (`_repo_relative_posix` ~:126-138);
`src/saddle/gates.py` (`check_target_files` ~:323, read only: it compares
strings exactly against what `git` prints); `tests/test_dag.py` (the
known-good ~:466 and the parametrized known-bad ~:480).
Contract: a `target_files` entry is rejected when any `/`-separated
segment is empty, `.` or `..` — so `./n.py`, `a//b.py`, `src/./x.py`,
`dir/` and `/etc/passwd` all fail validation — in addition to the
existing backslash and surrounding-whitespace rules. An accepted entry is
spelled the way `git diff --name-only` prints it, so it can match its own
file.
Direction: **tightened**, with proof: `./n.py` validates today
(`PurePosixPath("./n.py").parts == ("n.py",)`, so the `..` check never
sees the dot), the gate then compares `"./n.py" == "n.py"` and fails
`target-scope` on the node's own file — a declared list that can only
reject. Same for `a//b.py` (`parts == ("a", "b.py")`) and `dir/`.
Evidence: VERIFIED — `dag.py:134` normalises through `PurePosixPath`
before the `..` check; `gates.py:323` subtracts raw strings;
`tests/test_dag.py:480` lists no `.`-segment or empty-segment case.
Issue: none; note it on #64.
Steps: replace the `startswith("/")` and `PurePosixPath` clauses with
`any(segment in ("", ".", "..") for segment in path.split("/"))` (keep
that spelling, the mutants target it); keep the backslash and strip
checks; extend the message to "...without empty, '.' or '..' segments";
drop the `PurePosixPath` import if unused.
Known-good: `n.py`, `src/app/login.py`, `a.b/c-d_e.py`, `.github/x.yml`
(a name starting with a dot is not a `.` segment) validate.
Known-bad: `./n.py`, `a//b.py`, `src/./x.py`, `dir/`, plus the six existing
cases, each raise `ValidationError` naming the entry.
Contract mutants (each target occurs once; abort if `grep -c` is not 1; drop `__pycache__` after each revert):
1. `sed -i 's/segment in ("", ".", "..")/segment in ("..",)/' src/saddle/dag.py` → `pytest tests/test_dag.py -k target_files -q --no-cov` red (`./n.py`, `a//b.py`, `dir/`, `/etc/passwd` accepted).
2. `sed -i 's/segment in ("", ".", "..")/segment in ("", ".")/' src/saddle/dag.py` → same command red (`../n.py` accepted — the existing case must still bite).
Done when: mutants red; `./check.sh` green; `.github/x.yml` validates (the
known-good guards against over-tightening to "no segment starts with a
dot").
Stop if: `git diff --name-only` ever prints a `./` prefix (it does not from
the repo root, and the runner's `-C workdir` is the root).
Passing instance at HEAD: the `target_files` known-good in `tests/test_dag.py`
~:466 (the validator path).
Dry run: not run.

### T3-14 — `git_added_files` sees a staged rename (Minor, test-only)
Status 2026-09-19: session 26 STOPPED, correctly: the item's known-good
said `git_changed_files` lists both paths of a staged rename; it listed
only `m.py`, because that diff had no `--no-renames` and git's default
rename detection folds the move into one `R100` entry. That is a gap in
target-scope, not just a wrong sentence: the path a node deleted never
reached the gate, so `target_files: [m.py]` let `git mv n.py m.py`
through. Reviewer landed the item in the main session (contract change,
**tightened**): `git_changed_files` gains `--no-renames` and its
docstring says why; one test
`test_git_diff_helpers_see_a_staged_rename_as_both_paths` asserts
`git_added_files == ["m.py"]` and `git_changed_files == ["m.py",
"n.py"]`. Known-bad shown red on the old source (`['m.py'] == ['m.py',
'n.py']`). Mutants, both KILLED: M1 drop `--no-renames` from
`git_added_files` (`[] == ['m.py']`); M2 drop it from
`git_changed_files` (`['m.py'] == ['m.py', 'n.py']`). check.sh 662
passed, 100%. The leftover check (evidence.py `snapshot`/leftover
caller) now sees a renamed-away path too, which is the same direction.
Files: `tests/test_evidence.py` (beside
`test_git_added_files_lists_staged_adds_and_ignores_untracked` ~:254);
`src/saddle/evidence.py:270` (`"--no-renames"`, read only).
Contract: a staged `git mv n.py m.py` reports `["m.py"]` from
`git_added_files`, so a `refactor` node that renames a file fails
`node-scope` as an add and a `target_files` list must name the new path.
Direction: none (test-only). CLAUDE.md asks for the rename as a widened
input; the flag that makes this true has no test that dies without it.
Evidence: VERIFIED 2026-09-18 in a scratch repo on git 2.43: with
`--no-renames` the command prints `m.py`; without it `diff.renames`
defaults on, the change shows as `R100` and `--diff-filter=A` prints
nothing.
Issue: none.
Steps: one test: `_git_repo`, `git mv n.py m.py`, assert
`git_added_files(root, "HEAD") == ["m.py"]` and that
`git_changed_files(root, "HEAD")` lists both paths.
Known-good: the assertion above. Known-bad: none needed — the mutant is
the discriminating half.
Contract mutants (each target occurs once; abort if `grep -c` is not 1; drop `__pycache__` after each revert):
1. `sed -i 's/        "--no-renames",//' src/saddle/evidence.py` → `pytest tests/test_evidence.py -k rename -q --no-cov` red (returns `[]`).
Done when: mutant red; `./check.sh` green.
Stop if: none.
Passing instance at HEAD: `test_git_added_files_lists_staged_adds_and_ignores_untracked`.
Dry run: not run.

### T3-15 — Docs and docstrings say what landed (Minor, docs-only)
Status 2026-09-19: DONE — 61eea7d (executor, session 27). The four edits
as specified; gate 6 also carries the "no kind adds files without
`write_file`" clause since T3-4 (dbf8062) had landed. `run_tier1`'s
docstring already said eleven at HEAD. The item's grep still matches
`gates.py:510` ("is exempt" in assertion-preservation's note), which is not
one of the three targets. Open: the T3-8 addendum below, whose wording is
now stale — since T3-24 (9b65f2a) the orphan half excludes every id the
plan declares. ARCHITECTURE's Requirement Binding item (:179, which also
said "failing-pre/passing-post test") was rewritten in the main session's
T3-12 follow-up commit; the addendum below is closed by it.
Files: `docs/ARCHITECTURE.md:180` (gate 6: "`refactor` is exempt"), `:227`
(status line: "resume specified, not built — `slice.py:584-587` raises");
`src/saddle/gates.py:3-5` (module docstring's gate order omits
`target-scope`), `:615` (`run_tier1`: "all ten Tier-1 checks");
`tests/test_gates.py:300` (the eleven-name order pin, read only), `:351`
(`len(...) == 11`, read only).
Contract: ARCHITECTURE gate 6 reads "an `impl` node may not edit tests; a
`test` node may not ship the implementation; a `refactor` node may edit
both but may not add files" (T2-2; once T3-4 lands, add "and no kind adds
files without `write_file`" there, once); `:227` reads "resume built
(T3-1): a verified journal seeds the proven set and only unproven nodes
run; stall detection deferred, WORKPLAN §7"; the gates docstring lists
`target-scope` after `node-scope`; `run_tier1`'s docstring says eleven.
The two `test_gates.py` pins already enforce the count and order in code;
this makes the prose agree.
Direction: docs-only.
Evidence: VERIFIED — `ARCHITECTURE.md:180` still says "`refactor` is
exempt" after T2-2 removed the exemption (`gates.py:465` fails a refactor
that adds files); `:227` cites a refusal T3-1 deleted; `gates.py:4-5`
lists ten names and `:615` says "ten" while `test_gates.py:351` pins `== 11`.
Issue: none.
Steps: the four edits; afterwards
`grep -n "is exempt\|specified, not built.*resume\|all ten" docs/ARCHITECTURE.md src/saddle/gates.py`
prints nothing.
Known-good / Known-bad / Contract mutants: none — prose. `tests/test_docs.py`
pins only the example node JSON, which this does not touch.
Done when: the grep above is empty; `./check.sh` green.
Stop if: T3-4 has already rewritten `ARCHITECTURE.md:180` — then fix only
`:227` and the two docstrings.
Passing instance at HEAD: n/a (prose).
Dry run: n/a (docs-only).
Added 2026-09-19 (T3-8 finding): ARCHITECTURE's Requirement Binding item should say the check is suite-granular — citations are read from every discovered test source, not from the node's scoped command, so a plan with two test modules must declare every `REQ-` id either module cites on every node whose gate can see it (see `_declares_both_requirements` in `tests/test_slice.py`).

### T3-16 — Three consistency fixes (Minor)
Status 2026-09-19: DONE — bbcc961 (executor, session 27). (a) union
removed after the staged-new-file test passed both with and without it;
(b) `entry is entries[-1]`, two tail tests; (c) already correct at HEAD
(dc82327 had reworded the T3-2 paragraph), no edit. Reviewer re-ran both
mutants at HEAD (2 failed each) and check.sh: 632 passed, 100%.
Files: `src/saddle/runner.py:128-130` (`touched` and its comment);
`src/saddle/cli.py` (`run_tail` loop ~:561-577); `src/saddle/transcript.py`
(`is_run_end` ~:70, read only); `tests/test_runner.py` (beside
`test_run_node_gate_target_files_binds_end_to_end` ~:249); `tests/test_cli.py`
(beside `test_run_tail_streams_cold_journal_without_sleeping` ~:1682); this
file (T3-2's Evidence paragraph ~:1021, corrected in the commit that adds
these items).
Contract:
(a) `touched` in `run_node_gate` is `sorted(git_changed_files(...))` alone:
`git diff --name-only <ref>` already lists a staged new file (tracked-ness
comes from the index), so `| set(added)` at `:130` adds nothing. Prove it
before removing it: a runner test that stages a new file outside
`target_files` and asserts `target-scope` names it must pass before and
after the change. If it fails without the union, keep the union and record
why here.
(b) `saddle tail` exits on a run-end span only when that span is the last
entry read: `if is_run_end(entry) and entry is entries[-1]:`. Today
(~:574) it returns at the first run-end span, so a cold journal with two
completed runs prints the first run and exits before the second, and a
journal with a finished run followed by a run in flight stops at the old
run's end.
(c) T3-2's Evidence says the emitted schema carries "an unconstrained
optional array"; `dag_json_schema()` emits `items: {type: string,
minLength: 1}`. Corrected in this file; nothing else to do.
Direction: (a) none if the proof holds (equivalent code); (b) **tightened**
(a stricter exit; a finished journal still exits at once, since its
run-end span is its last entry); (c) docs-only.
Evidence: (a) VERIFIED — `git diff HEAD --name-only` lists a file added with
`git add` (git's tracked-set semantics; the T3-2 end-to-end test exercises
a staged new file only through `added`). (b) VERIFIED by reading —
`cli.py:574` returns inside the `for entry in entries[shown:]` loop at the
first `is_run_end`. (c) VERIFIED — `dag_json_schema()` run at `15e5c3e`
prints `{'items': {'minLength': 1, 'type': 'string'}, ...}` for the field.
Issue: none.
Steps: (a) the test, then the one-line change and its comment at `:128-129`;
(b) the condition above, plus a test with two complete runs in one journal
(`_first_run` resumed once, or `_tail_journal` twice) asserting both run
spans render and the exit is 0, and one with a run-end span followed by a
new run's first span asserting tail keeps following.
Known-good: (a) a staged new file outside `target_files` is named by
`target-scope`; (b) a one-run journal still exits 0 without sleeping
(`test_run_tail_streams_cold_journal_without_sleeping` unchanged).
Known-bad: (b) with the old condition the second run's lines are absent from
the output — the two-run test asserts they are present.
Contract mutants (each target occurs once; abort if `grep -c` is not 1; drop `__pycache__` after each revert):
1. `sed -i 's/if is_run_end(entry) and entry is entries\[-1\]:/if is_run_end(entry):/' src/saddle/cli.py` → `pytest tests/test_cli.py -k tail -q --no-cov` red.
2. `sed -i 's/    touched = sorted(git_changed_files(workdir, baseline, recorder=recorder))/    touched = []/' src/saddle/runner.py` → `pytest tests/test_runner.py -k target_files -q --no-cov` red (the wrong-file node passes) — confirms the staged-add test still discriminates once the union is gone.
Done when: mutants red; `./check.sh` green.
Stop if: (a)'s proof fails — then keep the union and delete (a) from this
item.
Passing instance at HEAD: `test_run_tail_streams_cold_journal_without_sleeping`
((b)) and `test_run_node_gate_target_files_binds_end_to_end` ((a)).
Dry run: not run.

---

### T3-17 — The merge suite runs the way the node gates run tests (Major; decision needed)
Status 2026-09-19: DONE by the reviewer on the user's word (the loosening
confirmed in chat). `MERGE_COMMAND = "python -m pytest -q"` is
`run_slice`'s default; the merge span's argv records it. Known-good
`test_run_slice_merge_suite_runs_as_the_node_gates_do` (a `pkg/` package
imported by `tests/test_m.py`, no conftest, no packaging: the smoke run's
layout) proves and merges; known-bad
`test_run_slice_bare_pytest_merge_cannot_import_what_the_gates_could` keeps
`merge_command="pytest -q"` on the same tree and reads `merge exit 2`, so
the reason for the default stays load-bearing. ARCHITECTURE's Tier 2
status line, stale since T2-3, now says item 2 is built and how. Mutants
(pre-commit, exact replacement, tree hash identical): default → `"pytest
-q"` KILLED; merge block ignores its parameter KILLED (2).
Files: `src/saddle/slice.py` (`run_slice(..., merge_command: str | None = "pytest -q", ...)`
~:648 and the merge block after the loop); `tests/test_slice.py` (the three
merge-suite tests; one new known-good); `docs/ARCHITECTURE.md` (Tier 2 item 2).
Contract: the merge-time full suite runs under the same interpreter form
as every node's `tests` gate — `python -m pytest -q` — so it measures the
union of the node diffs and not a different import environment. The node
gate runs `coverage run -m pytest …` (module form: cwd on `sys.path`); bare
`pytest` does not insert cwd, so a repo whose tests import a top-level
package without packaging passes every gate and fails the merge for a
reason no gate can observe.
Direction: **loosened** for the merge gate, with proof — the smoke run's
final tree: `pytest -q` → exit 2 `ModuleNotFoundError: No module named
'src'`; `python -m pytest -q` → 6 passed; `coverage run … -m pytest` → 6
passed (record S1). The merge gate rejected a tree every node gate had
accepted under the harness's own runner. The alternative (bare `pytest`
for the node gates too) would reject any unpackaged `src`-layout repo at
the impl node instead, which is the same input rejected one gate earlier.
Evidence: VERIFIED — `under_coverage` (`evidence.py`) rewrites `pytest` to
`coverage run … -m pytest`; `run_slice`'s default is bare `pytest -q`.
Issue: none — open one quoting the three probe lines.
Steps: 1. default `merge_command="python -m pytest -q"`; `_run_is_allowed`
already maps `python -m pytest` to `run_tests` (T3-4 follow-up). 2. A
known-good: `_slice_repo` variant with `pkg/__init__.py`, `pkg/m.py` and
`tests/test_m.py` importing `pkg.m`, no conftest — the merge suite passes
under the new default and fails under `merge_command="pytest -q"` (the
known-bad, kept as a test that pins why). 3. ARCHITECTURE Tier 2 item 2
names the form. 4. T3-4's binding text unchanged.
Known-good: the packaged fixture proves and merges; every existing
merge-suite test passes with the default swapped.
Known-bad: the same fixture with `merge_command="pytest -q"` reads
`merge exit 2`.
Contract mutants: 1. default → `"pytest -q"` → known-good red. 2. merge
block skips `shlex.split` form → the existing three merge-suite tests red.
Done when: mutants red; `./check.sh` green.
Stop if: the user has not confirmed the loosening (this item is a
decision; do not start it from a handoff without the word).
Passing instance at HEAD: `test_run_slice_merge_suite_gate_runs_once_and_is_journaled`.
Dry run: not run.

### T3-18 — The diff grammar requires the `---`/`+++` lines before a hunk (Major; container)
Status 2026-09-19: DONE. Grammar edit, structural test and the tool's
reject entry landed by the executor (3938d32, session 36); the reviewer
re-ran four mutants, all KILLED (`from to` dropped from `section`;
`"--- " | "+++ "` back in `meta_pfx`; the reject entry deleted from the
tool; `REJECT_AT` off by one). Container half run by the user against the
serving container's xgrammar: `53/53 cases correct`, exit 0 — 44 admit
(40 real diffs + rename, mode, delete, no-newline), 6 reject with the
headerless section refused at byte 19 (its `@@`), 2 must-not-stop, 1
must-stop. The tool's output is the record: the first attempt died with
`ModuleNotFoundError: No module named 'saddle'` because `--run` imported
the grammar from a `src/` that does not exist beside `/tmp/check.py` in
the container — the docstring's own three lines had never worked. Fixed
by the reviewer (tightened): `build_cases()` puts the checkout's grammar
into the cases file, `run()` compiles that and refuses a file without it,
git is pinned to the repo root so `--emit` works from any cwd; a test
pins the emitted grammar byte-identical to `DIFF_GRAMMAR`. Mutants:
grammar key dropped KILLED; grammar altered in transit KILLED; git not
pinned to the repo KILLED. Issue not opened (the user's, §0.10). Finding
carried out: the mutmut work copy fails tests that read `docs/` or
`tools/` whatever the mutation → T3-22.
Files: `src/saddle/vllm.py` (`DIFF_GRAMMAR` ~:57-68); `tools/diff_grammar_check.py`
(run inside the serving container — the only xgrammar); `tests/test_vllm.py:676`
(`assert DIFF_GRAMMAR.startswith("root ::= section+")` — the banned
constraint-text shape; replace it with the known-good/known-bad pair below).
Contract: a section is `header meta* from to hunk+` with `from ::= "--- "
line "\n"` and `to ::= "+++ " line "\n"`; `"--- "` and `"+++ "` leave
`meta_pfx`. A hunk can no longer follow `diff --git` (or `index`, or `new
file mode`) directly, which is the shape behind every one of the smoke
run's 16 `patch fragment without header at line 3` failures. Hunk line
counts stay unenforceable (a CFG cannot count); this closes the header
half of the class, not the count half.
Direction: **tightened**.
Evidence: MEASURED — smoke record S2: 16/16 recovery applies, both nodes,
all four fallbacks. VERIFIED — the grammar lists `"--- "` and `"+++ "` as
optional `meta`. `tests/test_vllm.py:676` pins the grammar's first line,
which is the shape CLAUDE.md bans.
Issue: none — open one quoting the four distinct apply errors.
Steps: 1. the grammar edit. 2. `tools/diff_grammar_check.py` in the container
(user present): the 40 real diffs still accept; add the known-bad
`diff --git a/x b/x\n@@ -1 +1 @@\n-a\n+b\n` to its reject list and assert
it rejects at the `@@`. 3. Replace the `startswith` test with a local
structural check that does not pretend to be xgrammar: parse
`DIFF_GRAMMAR` for the `section` rule and assert it names `from` and `to`
between `meta*` and `hunk+`; and pin the check tool's reject list contains
the known-bad. 4. Record the tool's output in the item.
Known-good: all 40 real diffs accept in xgrammar.
Known-bad: the headerless section rejects in xgrammar.
Contract mutants (grammar text): 1. `from to` removed from `section` →
the structural test red and the tool's reject list red in the container.
Done when: the tool passes both halves in the container; `./check.sh` green.
Stop if: `tools/diff_grammar_check.py` cannot run (no container access) —
then land the grammar edit and the local test only, and mark the
container half not run here.
Passing instance at HEAD: the 40-diff accept set in the tool.
Dry run: not run.

### T3-19 — The planner is shown the repository it is planning for (Major)
Status 2026-09-19: DONE by the reviewer (12ab1ed). `build_emit_prompt(task,
files=())` lists the tracked files (capped like the worker prompt) and
states the existing-file rule; `_emit_valid_dag(..., files=)` is fed from
`run_task`, the replan path and `run_dag`; `saddle dag --repo` (default
`.`) lists the repo and a non-repo cwd lists nothing (`_listable_files`),
since `dag` is a preview. The `saddle dag --help` golden gained the
option. Mutants: listing emptied KILLED (3); cap removed KILLED; `run_dag`
passes `()` KILLED; `run_task` passes `()` KILLED.
Files: `src/saddle/cli.py` (`build_emit_prompt(task)` ~:116 → `build_emit_prompt(task, files)`;
`run_task` ~:423 and `run_dag` ~:557 call `git_ls_files` before emitting —
`run_task` already does at ~:451, after the plan); `tests/test_cli.py`.
Contract: the decomposition prompt carries the repository's tracked file
list (first `MAX_FILES_IN_PROMPT`, then "… and N more"), and a rule: a task
about an existing module changes that module; a new module is created only
when no listed file owns the behaviour. The planner today sees the task
sentence only — the smoke run's plan built `src/f.py` beside the `n.py`
that already defined `f`, so the subject function ended up twice with
different behaviour (S6).
Direction: **tightened** (planner input; no gate changes).
Evidence: VERIFIED — `build_emit_prompt(task: str)`; `prompt =
build_emit_prompt(task)` at ~:372 with no file argument; smoke `dag.txt`.
Issue: none.
Steps: 1. the signature and the listing block (same shape as
`build_worker_prompt`'s file list). 2. the rule sentence. 3. `run_dag`
lists files from `--repo`'s worktree (add `--repo` to `dag`, default `.`).
4. Tests: the prompt for `["n.py"]` names `n.py` and the rule; with 150
files it names 100 and "… and 50 more"; `saddle dag` in a scratch repo
passes its files (mocked client records the prompt).
Known-good: prompt lists the files and the rule. Known-bad: an empty
listing prints "(no tracked files)" and the rule still.
Contract mutants: 1. listing block dropped → prompt test red. 2. `run_dag`
passes `[]` → the CLI test red.
Done when: mutants red; `./check.sh` green.
Passing instance at HEAD: `test_emit_prompt_asks_for_the_test_impl_split`.
Dry run: not run.

### T3-20 — A failed `mutmut run` names itself (Medium)
Status 2026-09-19: DONE by the reviewer (44c2538). `mutation_sample` binds
the `mutmut run` capture; an exit other than 0 or `SHELL_TIMEOUT` returns
`survivors=("mutmut run exited N: <last output line>",)` before `results`
is read (mutmut exits 0 with survivors — checked on a real run, so
non-zero is the tool); `check_mutation` renders that as `mutation tool
failed: …`. Mutants: exit check → `if False` KILLED (2); timeout carve-out
removed KILLED; wording dropped KILLED (2).
Files: `src/saddle/evidence.py` (`mutation_sample` ~:449-500: the
`run_capture(["timeout", …, "mutmut", "run"], …)` result is discarded);
`src/saddle/gates.py` (`check_mutation` detail for the tool-failure case);
`tests/test_evidence.py`, `tests/test_gates.py`, `tests/test_runner.py`
(the `_surviving_mutmut`-style stub with `run` exiting 1).
Contract: when `mutmut run` exits non-zero, the outcome is
`MutationOutcome(0, 0, 0, survivors=("mutmut run exited N: <last stderr
line>",))` and the mutation check fails with that text — never "no
mutants decided", which describes a run that happened. The smoke run's
mutmut exited 1 in 658 ms because mutmut 3.8 refuses a package named
`src` (`Failed trampoline hit. Module name starts with "src.", which is
invalid`); the gate reported the absence of a verdict, not the tool.
Direction: **tightened** (same fail-closed verdict; the reason is now
true). Already-failed-closed: yes.
Evidence: MEASURED — reproduced 2026-09-19 on a copy of the smoke
worktree with the harness's own scratch config: exit 1, the assertion
above. VERIFIED — the `run_capture` result at ~:487 is unbound.
Issue: none.
Steps: 1. bind the run; on non-zero exit (and not `SHELL_TIMEOUT`, which
is the budget binding and keeps today's path) return the outcome above
before `results`. 2. `check_mutation`: a survivor string starting with
`mutmut run exited` renders as `mutation tool failed: …`. 3. A stub whose
`run` exits 1 printing one stderr line; runner end-to-end asserts the
detail.
Known-good: the conftest stub (exit 0) unchanged. Known-bad: the exit-1
stub → `mutation: FAIL (mutation tool failed: mutmut run exited 1: …)`.
Contract mutants: 1. the exit check → `if False` → known-bad reads "no
mutants decided" → red. 2. `SHELL_TIMEOUT` carve-out removed → the
existing timeout test red.
Done when: mutants red; `./check.sh` green.
Passing instance at HEAD: `test_run_node_gate_full_sample_catches_what_a_small_cap_hid`.
Dry run: not run.

### T3-21 — `saddle verify` cannot call a failed run PASS (Medium)
Status 2026-09-19: DONE by the reviewer (15a349f). The last `run` span's
exit governs when one exists (`run_ok`); a journal with no run span keeps
the proof-only verdict, so the golden fixture and a live `tail` are
unchanged — a deliberate narrowing of the item's "no run span → FAIL",
recorded here. The `N failed` parse was not needed: the run span's exit
is 0 iff the run passed. Mutants: run-span clause removed KILLED; first
run governs instead of last KILLED.
Files: `src/saddle/transcript.py` (`render_journal_transcript` ~:138-185);
`src/saddle/cli.py` (`run_verify` ~:575); `tests/test_transcript.py` or
`tests/test_cli.py` (verify tests); the smoke journal shape as the fixture
(`_first_run` plus one failed node).
Contract: the audit verdict is FAIL when the journal's last `run` span
exited non-zero or its detail counts any failed or undispatched node,
whatever the sealed proofs say; PASS requires both that and every sealed
gate output passed. Today the verdict is recomputed from proof records
alone, and only proven nodes have records, so a run with one proven and
one failed node re-renders as PASS (S5: the smoke journal, `1 proven, 1
failed`, verifies as `Verdict: PASS`). Task, started and finished stay
"(unknown)" until T3-9 puts the task in the record.
Direction: **tightened**.
Evidence: MEASURED — `verify.txt` in the smoke record. VERIFIED —
`render_journal_transcript` reads `records` for the verdict and `spans`
only for tool lists.
Issue: none — mention in T3-9's issue.
Steps: 1. find the last span with `name == "run"`; if none, FAIL
("no run span"); parse `N failed` and `M undispatched` from its detail. 2.
verdict as above. 3. Tests: a journal with one proof and a run span `1
proven, 1 failed` → FAIL; the proof-only happy journal → PASS unchanged; a
journal with no run span → FAIL naming it.
Known-good: `test_run_verify_*` happy path unchanged.
Known-bad: the smoke shape → `- Verdict: FAIL`.
Contract mutants: 1. the run-span clause removed → known-bad red. 2. `N
failed` parse → `0` → known-bad red.
Done when: mutants red; `./check.sh` green.
Passing instance at HEAD: the existing verify happy-path test.
Dry run: not run.

### T3-22 — saddle's own mutmut work copy kills every mutant by accident (Minor)
Files: `pyproject.toml` (`[tool.mutmut]`, `also_copy` ~:69);
`tests/test_mutmut_layout.py` (`--collect-only`); `tests/test_docs.py`
(reads `docs/ARCHITECTURE.md`); `tests/test_vllm.py`
(`test_diff_grammar_requires_the_file_lines_before_a_hunk` imports
`tools.diff_grammar_check` inside the test body).
Contract: every test that passes in the repo also passes, unmutated, in the
`mutants/` work copy mutmut builds from `source_paths` + `also_copy` +
`tests`. A test that fails there whatever the mutation kills every mutant,
so the Tier-2 self-mutation score (ARCHITECTURE §3, time-boxed in CI)
reads 100% while measuring nothing — the vacuity class of CLAUDE.md's "a
mechanism must be able to do what it reports".
Direction: **tightened** (self-check; no gate change).
Evidence: MEASURED 2026-09-19 — the layout rebuilt from pyproject
(`src/saddle`, `benchmark`, `tests`) in a scratch directory: both
`tests/test_docs.py` tests `FileNotFoundError: .../mutants/docs/ARCHITECTURE.md`;
the T3-18 grammar test `ModuleNotFoundError: No module named 'tools'`.
`test_mutmut_layout` is collect-only and cannot see either: collection
succeeds, the failures are at run time. Found by the T3-18 executor
(`test_docs`), confirmed by the reviewer (plus the T3-18 test itself).
Issue: none.
Steps: 1. `also_copy = ["benchmark/", "docs/", "tools/"]`. 2.
`test_mutmut_layout` runs, in the rebuilt layout, the modules that read
outside `src`/`tests` — `tests/test_docs.py` and `tests/test_vllm.py -k
file_lines` — and requires them green; collect-only stays for the rest (a
full-suite run in the layout is minutes and belongs in CI, not `check.sh`).
3. If a later test reads outside the copy, it fails this test, not mutmut.
Known-good: the rebuilt layout runs both modules green.
Known-bad: `also_copy` without `docs/` → the layout test red on
`FileNotFoundError`.
Contract mutants: 1. `docs/` dropped from `also_copy` → layout test red.
2. `tools/` dropped → layout test red. 3. Harness check, not a test
mutant: with the layout test's run step reverted to `--collect-only`,
mutants 1 and 2 must SURVIVE — proof that the run step carries the
contract.
Done when: mutants 1 and 2 red, 3 survives as stated; `./check.sh` green.
Stop if: mutmut's `also_copy` cannot carry a directory outside
`source_paths` — then move the reads (a `docs`-relative fixture, a
`tools` package under `src`) instead of the copy.
Status 2026-09-19: DONE -- landed by the main session in the commit that
carries this line (session 37's slot; no executor). What landed:
`also_copy = ["benchmark/", "docs/", "tools/"]` (pyproject, reason in its
comment); `tests/test_mutmut_layout.py` rebuilds the copy once per module
(fixture), keeps the collect-only test, and adds
`test_mutant_layout_runs_the_tests_that_read_outside_src`, parametrized
over `OUTSIDE_READERS` (`tests/test_docs.py`; the T3-18 grammar test by
node id, so a rename is pytest exit 4 -- red, not silence); the grammar
test's in-function-import comment now says why (tools/ arrives via
also_copy and the layout test proves it). Stop-if did not fire: mutmut
3.8.0's `copy_also_copy_files` copytrees any listed directory
(`benchmark/` already was one outside `source_paths`).
Evidence, in order. Known-bad first: the new run test against the
unchanged `also_copy` -- both readers red (`FileNotFoundError:
.../mutants/docs/ARCHITECTURE.md`; `ModuleNotFoundError: No module named
'tools'`), collect-only green as before. Known-good after the fix: 3
passed. Contract mutants (exact replacement, count-checked, restored from
a saved copy, `__pycache__` dropped): M1 `docs/` dropped -> 1 failed
(the test_docs reader); M2 `tools/` dropped -> 1 failed (the grammar
reader); M3 harness check, run step reverted to `--collect-only` with M1
applied -> 3 passed (SURVIVED), then with M2 -> 3 passed (SURVIVED): the
run step carries the contract, as the item required.
Gate: `./check.sh` in the shared tree read 91% (journal.py, slice.py)
because session 23 was editing those two files mid-run (CLAUDE.md, "do
not edit files during a measurement run"); the gate was re-run on a
`git archive HEAD` copy plus the three changed files with `PYTHONPATH`
pointing at the copy's `src`: ruff, format and mypy clean; 640 passed, 3
skipped, 100% line+branch. The copy's single failure
(`test_grammar_check_corpus_carries_the_grammar_it_checks`) runs `git
log`, and an archive has no `.git`; it passes in the repo (1 passed).
Limit, stated: step 3 holds for the modules in `OUTSIDE_READERS`. A later
outside reader that is not listed still kills every mutant in CI until it
is added; the full-suite run in the layout is the net for that and belongs
in the CI mutation job, not `check.sh`.
Passing instance at HEAD: none — the layout test collects, it does not run.
Dry run: not run.

### T3-23 — A failed node's work leaves the worktree before anything else runs on it (Major)
Status 2026-09-19: DONE by the reviewer. `restore_baseline(cwd, ref, *,
recorder)` in evidence.py runs the one `git restore --source <ref> --staged
--worktree -- .`, records it as a span named `restore-baseline` (the
`SpanRecorder.record` gained a `name=` override for it), then asserts
`git diff --name-only <ref>` and `git diff --diff-filter=A <ref>` are
empty and raises `RuntimeError` naming the ref and the paths otherwise.
`_abandon(workdir, baseline, applied, recorder)` in slice.py guards on
`applied and baseline is not None` and is called from the
`_HaltRecoveryError` handler and the loop fall-through; the span hangs off
the attempt that ended the node (for a halt, the identical-re-proposal
attempt; its two verification diffs hang there too). Known-bad shown red
at HEAD before the fix, four tests: the replacement node in the step-3
fixture fails as **unappliable** after 2 attempts (its `GOOD_DIFF` cannot
apply to the `return 2` tree `n1` left), not as `red-phase` as predicted
below — same defect, earlier gate, because the replacement proposes the
whole fix; 20b's diff happened to apply to both trees. Known-good: `1
proven, 0 failed, 0 undispatched, merge exit 0` (a replanned node is
excused, so the tally reads 0 failed, not 1) and both baseline refs show
`return 1`. Mutants: 1 restore call deleted KILLED (replacement test,
replan test); 2 `--staged --worktree` → `--worktree` KILLED (the
exhausted-node tree test's `git diff --cached <ref>` and the evidence
staged-edit test); 3 `if applied` → `if not applied` KILLED (replacement
and exhausted-node tests, plus both unappliable-diff slice tests, which
the restore-on-nothing-applied now touches). Tests changed as this item
required: `test_run_slice_replan_recovers_failed_node`,
`test_run_slice_replan_continues_past_failed_emission` and
`tests/test_cli.py::test_run_task_replan_recovers_exhausted_node` all
proposed a repair of `return 3` for the replacement and pinned the
defect; each now proposes the whole fix. `test_run_slice_replanned_node_
failure_stays_failed` needed no change (its replacement now fails gates
instead of not applying; still 2 attempts). That accidental coverage of
`_apply_diff`'s "did not apply cleanly" path is replaced by a direct test.
Files: `src/saddle/slice.py` (`_run_node` ~:404: the two `raise
NodeGateFailedError` sites ~:516 and ~:523 and the `NodeUnappliableError`
site ~:521; `run_slice` ~:656: the merge-suite call ~:729);
`src/saddle/evidence.py` (new `restore_baseline(cwd, ref, *, recorder)`
beside `materialize_baseline` ~:366); `tests/test_slice.py` (beside
`test_run_slice_replan_recovers_failed_node` ~:1244 and
`test_run_slice_exhausted_retries_fail_with_attempts` ~:754);
`tests/test_evidence.py`.
Contract: when `_run_node` gives up on a node after at least one diff was
applied, the worktree and index are restored to that node's own baseline
ref (`refs/saddle/baseline/<slug>`, the T3-8 snapshot) before the
exception leaves `_run_node`: `git diff <ref>` is empty and `git diff
--diff-filter=A <ref>` is empty, so (1) a replacement node's
`snapshot_baseline` freezes the tree the failed node started from, and its
`red-phase` pre leg can be red again; (2) the merge suite runs over proven
edits only. A node that applied nothing restores nothing (the tree is
already its baseline). The restore is one recorded span (name
`restore-baseline`, exit code of the git call) under the attempt that
failed, so the journal shows it happened.
Direction: **tightened** — nothing that proves today changes; a run that
today gates a replacement against unproven work, and merges over it, now
cannot.
Evidence: session 20b, this item's finding R1 (T3-7b Status): `git show
refs/saddle/baseline/node-2:n.py` is `def f(x): return x`,
`refs/saddle/baseline/node-2.r1:n.py` raises `ValueError` and returns
`2 * x`; `node-2.r1` failed `red-phase: FAIL (tests pass pre-change; prove
nothing)` on all three attempts with red probe exits 0,0,0 against
`node-2`'s 1,1,1,1,1,1,1. VERIFIED by reading: `_run_node` ~:466
`_apply_diff` → `applied.append`, no revert on any exit path ~:512-523;
`splice_replan` ~:526 keeps the failed node "so the transcript keeps its
verdict" and the replacement's first attempt snapshots the current worktree
~:436; the merge ~:729 runs `merge_command` in `workdir` as it stands.
After the run, `/tmp/saddle-smoke` carried `M n.py`, `A test_n.py`,
`A test_n_req002.py` staged and the merge suite passed over them.
Issue: the T3-7b issue (T3-5 drafts it), R1 paragraph.
Stop if: `git restore --source <ref> --staged --worktree -- .` does not
delete a file that `git apply --index` added since the snapshot. Probed
2026-09-19 (reviewer, scratch repo, git as installed): a staged edit to
`n.py` and a staged new `test_n.py` after the snapshot → restore exit 0,
`git status --porcelain` empty, `git diff <ref>` empty, `test_n.py` gone
from disk. The one line suffices; no `git rm`/unlink fallback is needed.
Steps: 1. `restore_baseline` in evidence.py: `git -C <cwd> restore
--source <ref> --staged --worktree -- .`, then assert both diffs are
empty and raise `RuntimeError` naming the ref if not; record the span.
2. In `_run_node`, wrap the give-up paths: before `raise
NodeUnappliableError` and both `raise NodeGateFailedError`, `if applied
and baseline is not None: restore_baseline(workdir, baseline,
recorder=recorder)`. The `_HaltRecoveryError` handler and the fall-through
after the loop both need it; one helper `_abandon(...)` called from both
is the shape. 3. Fixture: the T3-8 two-node `_slice_repo` shape where
`n1` is an impl node whose proposer returns a diff that turns the suite
green but fails another gate (`target_files=["other.py"]` so
`target-scope` fails while `tests` passes), then a `replan` that returns
a one-node DAG with the correct target. Known-good: `n1.r1` proves, the
run reads `1 proven, 1 failed, 0 undispatched, merge exit 0` with `n1`
in `replanned_from`, and `git show refs/saddle/baseline/n1.r1:n.py`
equals `refs/saddle/baseline/n1:n.py`. Known-bad: before step 2 the same
fixture fails `n1.r1` with `red-phase: FAIL (tests pass pre-change; prove
nothing)` — that is the 20b run in miniature and must be shown red first.
4. A second test: after `test_run_slice_exhausted_retries_fail_with_attempts`
fails its node, `git diff refs/saddle/baseline/<id>` in the fixture repo is
empty and the added file is gone. 5. `tests/test_evidence.py`:
`restore_baseline` on a repo with a staged edit and a staged new file
since the ref → both gone, `HEAD` unchanged; on a repo already at the ref →
no-op, exit 0 span recorded.
Contract mutants (write the exact seds when the code exists; each target
occurs once; abort if `grep -c` is not 1; drop `__pycache__` after each
revert): 1. delete the `restore_baseline` call in `_abandon` → step 3's
test red (the replacement's red-phase); 2. `--staged --worktree` → `--worktree`
only → step 4's test red (the index still carries the diff);
3. `if applied and baseline` → `if not applied and baseline` → step 3
red (never restores after an applied diff) and the no-op case of step 5
may go red too — record which.
Known-good at HEAD, and a test this item MUST change:
`test_run_slice_replan_recovers_failed_node` ~:1244 proposes `BAD_DIFF`
(`return 3`) for `n1` and `FIX_DIFF` (`-    return 3` / `+    return 2`)
for the replacement — the replacement's diff only applies BECAUSE the
failed diff is still in the worktree. That test pins the defect. After
step 2 it must propose `GOOD_DIFF` for the replacement (the replacement
starts from `n1`'s baseline, `return 1`), and the assertion set stays.
`test_run_slice_replanned_node_failure_stays_failed` ~:1280 is the same
shape; check it too.
Dry run: not run.

### T3-24 — An impl node may cite the requirements its dependencies' tests specify (Major; decision needed)
Status 2026-09-19: DONE by the reviewer under the user's word for A, with
one correction to A's declared set, **loosened, with proof**. Step 5 as
written does not reproduce 20b: run at HEAD, the two-node fixture with
`n1` `REQ-001` only and `n2` `REQ-002` only fails **`n1`**, not `n2`, with
`undeclared requirements cited: REQ-002` (`Proven nodes: 0`), because
`flipped_tests` is every discovered test source and the committed
`test_m.py` cites `n2`'s id before `n2` exists; the 20b shape (`test` node
`t1` declaring `REQ-001` writes a spec citing `REQ-001` and `REQ-002`,
`impl` node `n1` declares `REQ-002`) fails `t1` the same way. An upstream-
only set rescues neither, since the cited id is declared *downstream*:
implemented as mutant 3 below, both slice tests stay at `Proven nodes: 0`.
So the orphan half subtracts the ids declared by **every node of the
plan** (`planned_requirement_ids(dag) -> tuple[str, ...]` in `dag.py`,
sorted union; `check_requirement_binding(..., *, planned_ids=())`;
`Tier1Inputs.planned_requirements`; `run_node_gate(...,
planned_requirements=())`; `_run_node`/`_best_of_samples`/
`_evaluate_candidate` carry it; run_slice's worker computes it from
`remaining`, so a replacement node's ids count and a replaced node's do
not). Same rationale as A — an id some node of the plan declares is
planned, not hallucinated — and what stays tight is unchanged: an id
declared by no node is still an orphan (known-bad `REQ-003`), and a node's
own ids must still be cited (unbound half untouched; the plan does not
rescue it). Tests: gates known-good/known-bad/unbound/own-orphan-with-
no-plan, `run_tier1` passthrough, runner end to end, dag helper (union
over a chain plus a sibling; single node), and the two slice tests above,
both shown red at HEAD. Mutants, all KILLED: 1. `- set(planned_ids)`
deleted (6 tests); 2. `cited - declared - set(planned_ids)` → `cited -
set(planned_ids)` (9, including the own-orphan case); 3. helper returns
the dependencies' ids only — option A as written (dag + both slice
tests); 4. the worker hands the gate an empty plan (both slice tests).
`./check.sh` green: 629 passed, 3 skipped, 100%. The T3-8 fixture
`_declares_both_requirements` is left as it was.
Files: `src/saddle/gates.py` (`check_requirement_binding` ~:600; the call
in `run_tier1` ~:793); `src/saddle/runner.py` (`run_node_gate` ~:100: a
new parameter; `Tier1Inputs`); `src/saddle/slice.py` (`_run_node` ~:404
and `schedule`: the dag is needed to compute the upstream set);
`tests/test_gates.py`, `tests/test_slice.py` (the T3-8 two-node fixture
~:1244 declares both ids on both nodes to dodge exactly this — leave it,
add the distinct-ids case).
Contract today (gates.py ~:600): the orphan half rejects any `REQ-\d{3}`
cited in ANY discovered test source that the node did not declare
(`flipped_tests=test_sources`, runner.py ~:222, is every `test_*.py`,
not the tests this node's diff touched). Under T3-7a's test → impl split
the planner gives the test node REQ-001 and the impl node REQ-002; the
test node's file cites REQ-001; the impl node, whose whole job is to make
that file pass, is then rejected for "citing" REQ-001. No impl node in a
two-node plan with distinct ids can pass this gate.
Evidence: session 20b, T3-7b finding R2 — `node-2` and `node-2.r1`, six
attempts, every one `requirement-binding: FAIL (undeclared requirements
cited: REQ-001)`; `node-2` declares `REQ-002` only (dag.txt). VERIFIED by
reading as above. The suite-granular note in T3-8 step 4 (~:1668) already
describes the mechanism and worked around it in the fixture.
Decision (user's word before an executor starts): (A, recommended) the
declared set for the orphan check is the node's own ids plus the ids
declared by its transitive dependencies — an id that appears anywhere
upstream is a planned requirement, not a hallucinated one, and the
"unbound" half (every id the node itself declares must be cited) stays as
it is. (B) leave the gate; T3-12's planner prompt tells the planner that
an impl node declares every id its dependency's tests cite. B keeps the
gate honest but makes every plan hinge on the planner reading one
sentence, and 20b shows what a missed sentence costs.
Direction under A: **loosened, with proof** — 20b is the legitimate input
the contract rejects (an impl node fulfilling the specification its
dependency wrote). What stays tight: an id declared nowhere in the DAG is
still an orphan; a declared id no test cites is still unbound.
Issue: the T3-7b issue, R2 paragraph.
Steps under A: 1. `check_requirement_binding(requirement_ids,
flipped_tests, *, upstream_ids: Collection[str] = ())`; `orphans = sorted(
cited - declared - set(upstream_ids))`; detail unchanged. 2. `Tier1Inputs`
gains `upstream_requirements: tuple[str, ...] = ()`; `run_node_gate` takes
`upstream_requirements` and passes it through; `run_tier1` ~:793 passes
`inputs.upstream_requirements`. 3. `_run_node` receives the transitive
union — compute it once per node in `schedule` from the DAG it was given
(`Dag` already validates dependencies; a helper `upstream_requirement_ids(
dag, node_id) -> tuple[str, ...]` in `dag.py` keeps the layering) and pass
it to `run_node_gate`; `_best_of_samples`/`_evaluate_candidate` carry it
the way they carry `baseline`. 4. Tests, gates: known-good — node declares
`REQ-002`, upstream is `("REQ-001",)`, sources cite both → PASS `1
requirement(s) bound`; known-bad — sources cite `REQ-003`, declared
nowhere → `undeclared requirements cited: REQ-003`; unbound — node
declares `REQ-002`, no source cites it → `unbound requirements: REQ-002`
(upstream does not rescue the unbound half). 5. Test, slice: the T3-8
two-node fixture with `n1` declaring `REQ-001` only and `n2` `REQ-002`
only, `test_m.py` citing both → `2 proven`; before step 1 the same fixture
fails `n2` with `undeclared requirements cited: REQ-001` (show it red
first; that is 20b in miniature).
Contract mutants (write the exact seds when the code exists; each target
occurs once; abort if `grep -c` is not 1; drop `__pycache__` after each
revert): 1. `- set(upstream_ids)` deleted → step 5 red; 2. `cited -
declared - set(upstream_ids)` → `cited - set(upstream_ids)` → the
known-good in step 4 still passes but a node citing its own undeclared id
must fail: add that case (declares `REQ-002`, cites `REQ-002` and
`REQ-009`, upstream empty → orphan `REQ-009`) and show it red; 3. the
transitive helper returns direct dependencies only → a three-node chain
test (n3 cites n1's id) red.
Passing instance at HEAD: `test_run_tier1` cases for requirement-binding
in `tests/test_gates.py` (single-node, own ids only) — untouched by A.
Dry run: not run.

### T3-25 — A failed attempt's seal names the gates that failed (Minor)
Status 2026-09-19: DONE by the reviewer. The seal reads `attempt i/N: k
gate(s) failed: <name>, <name>` in check order; the exhausted-retries
fixture now pins `tests, red-phase, mutation` on attempt 1 and `tests,
coverage, red-phase, mutation` on attempts 2–3, the dependent-undispatched
fixture pins `coverage`, and the passing seal is still exactly `1 distinct
of 3 sample(s)`. Mutants: 1 `if not check.passed` → `if check.passed`
KILLED (seal named the eight passing gates); 2 names append deleted
KILLED (two tests).
Files: `src/saddle/slice.py` (`_run_node` ~:502: the `failed_count` seal
detail `f"attempt {attempt}/{max_attempts}: {failed_count} gate(s)
failed"`); `tests/test_slice.py` (beside
`test_run_slice_exhausted_retries_fail_with_attempts` ~:754, which reads
attempt spans).
Contract: the seal of a failed attempt reads `attempt i/N: k gate(s)
failed: <name>, <name>` with the failed gate names in check order, so a
failed node's earlier attempts can be read from the journal; a passing
attempt's seal is unchanged; the transcript's per-node gate list (final
attempt) is unchanged.
Direction: **tightened** (more is recorded; nothing is accepted that was
refused).
Evidence: session 20b finding R4 and 20a's S5 second half — `node-2`
attempt 1 "2 gate(s) failed", attempts 2–3 "4 gate(s) failed"; which two
is unrecoverable from `proofs.jsonl`, `run.log` or the transcript, since a
failed node has no proof record and the transcript renders the last
attempt only.
Steps: 1. `names = ", ".join(check.name for check in result.checks if not
check.passed)`; append `f": {names}"` to the detail. 2. Test: run the
exhausted-retries fixture, read the attempt spans for the node, assert
each detail ends with the failed gate names in order (`tests`, then the
others as the fixture produces them); assert a passing seal (the single-
node proven fixture) has no colon after `sample(s)`.
Contract mutants (each target occurs once; abort if `grep -c` is not 1;
drop `__pycache__` after each revert): 1. `if not check.passed` → `if
check.passed` → the seal names the passing gates, test red; 2. delete the
`f": {names}"` append → test red.
Dry run: not run.

### T3-26 — `saddle run --dag FILE`: run a hand-written DAG (small; prerequisite for T4-5)
Files: `src/saddle/cli.py` (`run_run` ~:481: the emission block before
`ask_confirm`; `build_parser` ~:734: the `run` subparser); `src/saddle/dag.py`
(`Dag.model_validate`, `validate_dag`, read only); `tests/test_cli.py`.
Contract: `saddle run --dag FILE TASK` reads a JSON DAG from FILE, runs
`validate_dag` on it exactly as an emitted DAG is validated, and skips the
planner call; every later step (confirmation, gates, replan, journal) is
unchanged, so a run from a file and a run from the planner differ only in
where the DAG came from. An invalid file stops with `error:` naming the
first issue and exit 1 before any model call.
Direction: none for the gates (the DAG still passes `validate_dag`); say
so. Measurement needs it: without a fixed DAG a gate can only be
exercised when the planner happens to emit the shape that reaches it.
Steps: 1. `--dag` (type `Path`, default `None`); in `run_run`, if set:
`dag = Dag.model_validate(json.loads(path.read_text()))`, then the same
`validate_dag` check and `error:` path the emitted DAG takes. 2. Tests:
a valid file runs the two-node pass fixture end to end with no `emit_dag`
call (assert the stub client's emit was never invoked); an invalid file
(a node depending on a missing id) prints `error:` and exits 1 with no
client call; `--dag` with `--yes` needs no confirmation.
Known-good: the fixture DAG from `tests/test_cli.py` written to a file
runs to the same transcript as the emitted one. Known-bad: a file whose
node lists `target_files: ["../x.py"]` is rejected at validation.
Contract mutants (each target occurs once; abort if `grep -c` is not 1; drop `__pycache__` after each revert):
1. skip `validate_dag` on the file path -> the invalid-file test red.
2. call `emit_dag` even when `--dag` is set -> the never-invoked test red.
Done when: mutants red; `./check.sh` green. Owner: main session (contract change).

---

## 7. Tier 4 — measurement (needs the container; each run is a record, not a code change)

Preconditions for every item: container healthy (`saddle doctor` green), key
in `SADDLE_VLLM_API_KEY`, no source edits during the run, results written
under `../saddle-bench/runs/<id>/` with the pre-registered prediction copied
in before the run starts.

### T4-6a — Viability sweep: saddle alone on all seven tasks, oracles fixed first (F21; RUN FIRST)
Status 2026-09-20: DONE by session 33. Commits: saddle 9e30280
(BENCHMARK-RECORD round-3 section, docs-only); `../saddle-bench` d9bd23b
(the freeze: seven oracles, hidden suites, patched `run_arm.sh`,
PREDICTION.md, committed 01:02:21, earliest journal 01:10:39), 93157ce
(F21 + PROGRESS.log), dff4b81 (the T1/T2 hardening notes). Thirteen runs,
not twelve: the item's own allocation (1 seed x 4 + 3 seeds x 3) is
thirteen and its summary count was the reviewer's arithmetic error;
PREDICTION.md repeats the slip and was left frozen. Two corrections to
this item's premises, both found by the executor and approved by the
user before run 1: `../saddle-bench` was not a git checkout (it was an
untracked directory of the parent repo), so it was initialised as its
own repo for the freeze; and `run_arm.sh` "as it is" could not run the
item (it fetched the key by exec into the container, passed no
`--sample-temperature`, and `rm -rf`'d the round-2 run directories), so
it was patched additively -- env key when set, `RUN_DIR`, `SAMPLE_TEMP`
-- with defaults unchanged and the patch inside the freeze commit.
Result: **5 of 13 runs pass their oracle; 3 of 13 on the strict reading**
(oracle PASS and a sealed proof covering the implementation). T1 0/1
(11/18, all 22 gates passed -- F21.6), T2 1/1, T3 1/1, T4 1/1, T5 0/3
(timeout x3, truncate/apply-fail/truncate, worktree untouched), T6 2/3
oracle but 0/3 strict (the correct module is unproven residue of a
failed impl attempt, F21.2), T7 0/3 (two timeouts, one exhausted replan;
test nodes sealed, no implementation). Not one of the nine large-task
runs completed. Prediction lines 1, 2 and 4 FALSIFIED, line 3 held. Line
4's wording was the reviewer's error: it meant an impl diff touching
tests (the #44 shape) and as written it is falsified by `test`-kind
nodes sealing their own test files, which is that kind's contract; the
#44 shape did not occur in the round. Every failure is labelled HARNESS
in F21; the model-ceiling column is empty because no large task was
asked in a shape the model could answer (F21.1: the worker's output cap
is `WORKER_OUTPUT_TOKENS[planner-chosen effort]`, recomputed on every
retry, and the truncation message's advice is acted on by no caller; T5's
three seeds fail identically to the second). Oracle contract mutants,
all three KILLED after two needed a constructed known-bad (F21.7: a
"ghost arm" holding only `pyproject.toml` scored 61/63 with provenance
off; a memory ceiling was added after the grader reached 57 GB). Three
mid-sweep claims by the executor were wrong and are recorded as
corrections in F21, not fixed silently. Reviewer verified: clean-room in
the saddle repo, `./check.sh` at 9e30280 green (see the commit carrying
this line). Follow-on: Tier 6 (§9), whose items cite F21.x.
Files: `../saddle-bench/sweep.sh`, `run_arm.sh` (as they are),
`../saddle-bench/baselines/t1..t7`, `prompts/t1..t7.prompt`, `oracles/`
(one per-task correctness oracle to add for each task that lacks one),
`oracles/test_quality.py` (as is), `../saddle-bench/runs/FINDINGS.md`
(F21), `runs/PROGRESS.log`, `docs/BENCHMARK-RECORD.md` (round 3 section).
Requires: container healthy, key in the environment, nothing else. This
item is deliberately ahead of T4-1..T4-5: it answers the one question
the user needs answered first, and its journals feed the others.
Question: can the local model, driven by saddle at HEAD, take each of the
seven benchmark tasks from prompt to a verified journal whose worktree a
pre-committed oracle accepts?
Pre-registered, written into `runs/round3/PREDICTION.md` and the oracles
committed in the bench checkout before the first run:
- Oracles. Every task has a per-task correctness oracle in `oracles/`
  that scores a worktree PASS/FAIL without knowing which arm made it.
  T4's: "the two totals agree AND the regression test pins the value the
  prompt's example implies"; its docstring records that round 1 withdrew
  a stricter bar and why this is not that bar. An oracle changed after
  its task has run voids that task for the round.
- Seeds: one on T1-T4 (T4-1 and T4-5 add nine more there later), three
  on T5-T7. Thirteen saddle runs (4 + 9); `--sample-temperature 0.7`.
- Per run record: oracle verdict; `saddle verify` verdict; wall time;
  node count, kinds, `target_files` declared or empty; sealed proofs; the
  first failing gate per failed node with its detail; and how the run
  ended -- verdict, truncation, diff-apply failure, or timeout. The last
  column is the one that separates "saddle refused a wrong diff" (the
  harness working) from "the model could not produce the diff" (the
  model's ceiling on this hardware), and the record must name which.
- Prediction (numbers first, then do not touch them): T1-T4 pass the
  oracle; T5 and T6 complete (F10's "nothing" is gone) and at least one
  of three seeds passes each; T7 is the open case, predicted to complete
  and fail the oracle on at least one seed; no run seals a proof the
  oracle fails whose diff touches a test file.
Falsified if any line comes out the other way; each is its own finding.
Steps: 1. Oracles + PREDICTION.md, committed in the bench checkout.
2. `sweep.sh tN saddle` for N=1..7 (three passes for N=5..7), journal
moved aside between passes; nothing edited in either checkout while a
run is in flight. 3. Score with the frozen oracles. 4. F21: the seven-row
table (task, seeds, oracle pass count, how each run ended, first failing
gate) plus a twelve-row appendix; BENCHMARK-RECORD.md round-3 section.
5. Every failed line becomes a workplan item with the run log as its
Evidence, labelled harness or model-ceiling.
Done when: PREDICTION.md and every oracle predate every journal in
`runs/round3/`; thirteen runs recorded (fewer only with each gap
explained); F21 written with the seven-row table and the verdicts.
Stop if: container down (report); an oracle is found wrong mid-round
(void that task, do not rewrite); any run crashes rather than fails a
gate (T3-shaped defect: record and stop the sweep). Never print the key.

### T4-6b — Round 3 comparison: the baseline arm on the same seven tasks and oracles (F22)
Files: `../saddle-bench/sweep.sh`, `run_arm.sh` (as they are; one GPU, arms
never overlap), `../saddle-bench/baselines/t1..t7`, `prompts/t1..t7.prompt`,
`oracles/test_quality.py` (the arm-agnostic grader), `oracles/` (one
per-task oracle file to add for each task that lacks one),
`../saddle-bench/runs/FINDINGS.md` (F21), `runs/PROGRESS.log`,
`docs/BENCHMARK-RECORD.md` (round 3 section). Requires T4-6a (its oracles and saddle runs are reused as-is;
only the baseline arm's runs are new).
Why: T4-1 and T4-5 answer one question each on one task. "Saddle handles
tasks" is a claim about the task distribution and about a comparison,
and round 2 (F10) recorded that saddle produced nothing on T5 and T6
while the baseline arm beat it on T1 correctness. Nothing since has
re-measured that; every Tier 2-3 item was verified on fixtures.
Pre-registered, all of it written into `runs/round3/PREDICTION.md`
before the first arm runs, and the oracles frozen in the same commit:
- Oracles. Every task has a per-task correctness oracle in `oracles/`
  that scores a worktree PASS/FAIL without reading which arm made it,
  committed before any round-3 arm runs. T4's is the contested one:
  write it as "the two totals agree AND the regression test pins the
  value the prompt's example implies", and record in the oracle's
  docstring that round 1 withdrew a stricter bar and why this one is
  not that bar. An oracle changed after its task has run voids that
  task for the round (the round-1 error, written down so it cannot
  repeat).
- Arms: `saddle` and `untouched` (the baseline arm at its default
  effort). The three probe arms are optional and cost a day; run them
  only if the two-arm result is close.
- Seeds: three per task per arm (`--sample-temperature 0.7` for saddle;
  the baseline arm's own default). 7 tasks x 2 arms x 3 seeds = 42 runs;
  at round-2 wall times that is one long day on the container, T5-T7
  dominating.
- Per run, record: oracle verdict, `test_quality.py` tautology rate and
  kill rate, wall time, and for saddle: node count, kinds, `target_files`
  declared or not, sealed proofs, the first failing gate per failed
  node, and whether the run ended by verdict or by truncation/timeout.
- Prediction (write the numbers before running, then do not touch
  them): saddle completes T5 and T6 (F10's "nothing" is gone: T2-1's
  truncation retry and the grammar); saddle's oracle pass rate on T1-T4
  is at least the baseline arm's; on T5-T7 saddle is at or below the
  baseline arm on pass rate, because decomposition is still unforced
  (#64); saddle's tautology rate is lower and kill rate higher than the
  baseline arm's on every task where both complete, because those are
  what the gates gate; no saddle run seals a proof the oracle fails
  AND whose diff touches a test file (the #44 shape, after T4-5).
Falsified if: any of those five lines comes out the other way. Each is
its own finding; the fifth is the one that matters most and is the
only one that would send an item back to Tier 2.
Steps: 1. Oracles and PREDICTION.md, committed in the bench checkout.
2. `sweep.sh tN saddle untouched` for N=1..7, three passes, PROGRESS.log
timestamps as the audit trail; nothing edited in either checkout while
a run is in flight. 3. Score with the frozen oracles and
`test_quality.py`. 4. F21: one table per prediction line, 42 rows in an
appendix; BENCHMARK-RECORD.md round-3 section with the verdicts.
5. Every falsified line becomes a workplan item with the run log as its
Evidence.
Known-good / Known-bad: the prediction is the bar.
Done when: PREDICTION.md and every oracle predate every journal in
`runs/round3/`; 42 runs recorded (or fewer with each gap explained);
F21 written with five verdicts.
Stop if: container down (report); an oracle turns out to be wrong
mid-round (record the task as void for this round, do not rewrite it);
any saddle run crashes rather than fails a gate (T3-shaped defect:
record and stop the sweep).
Decision this feeds: the sentence "saddle handles tasks", which is not
to be written anywhere before this item's F21 exists.

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
`BUDGET_TO_EFFORT` earns its complexity. After T3-4, add a tools axis at the
best effort: the same task with `read_file` omitted from every node, then
with `run_tests` omitted — the two bindings that remove prompt content —
so D15/D16's context-cost argument gets its first number.

### T4-5 — Rerun the T4 arm against HEAD's gates: does anything catch the #44 shape? (F20)
Files: `../saddle-bench/run_arm.sh` (the `saddle` arm; add `--dag` for
half A), `../saddle-bench/baselines/t4` (`orders.py`, `discounts.py`,
`invoice.py`, `tests/`), `../saddle-bench/runs/T4-RESULT.md` (the round-2
record and the withdrawn-oracle note), `../saddle-bench/runs/t4-saddle.log`
(the run that passed all seven gates), `docs/BENCHMARK-RECORD.md` (append),
`../saddle-bench/runs/FINDINGS.md` (F16). Requires T3-26 for half A.
Why this item exists: Tier 4 was drafted before Tiers 2-3 built the gates
that answer #44, and no item measured them on the task that motivated
them. T4-1 measures the grammar; this measures the gates.
What the old run did (from `t4-saddle.log:48`, harness at 975bf52): one
node, no `kind`, no `target_files`; the diff changed `discounts.py` and
`tests/test_discounts.py` together (the ruff span lists both), and all
seven gates passed. T4-RESULT.md records that the C2 bar was withdrawn
mid-run and that which module is "right" is genuinely undeclared by the
prompt; this item does not reopen that. It asks a narrower question that
does not depend on module policy: **can a single node still edit a
source module and its test in the same diff and seal a proof?**
Pre-registered prediction (copy into the run directory before starting):
- Half A (fixed DAG via `--dag`, three seeds): one `impl` node with
  `target_files: ["invoice.py", "tests/test_invoice.py"]` and `test_command:
  "pytest tests/"`. Prediction: any diff touching `discounts.py` fails
  `target-scope` naming it; any diff touching `tests/test_invoice.py` fails
  `node-scope` (an impl node may not touch tests); a diff confined to
  `invoice.py` passes both and reaches red-phase. Expected outcome: at
  least one of three seeds is stopped by one of those two gates, and no
  seed seals a proof whose diff touches `discounts.py` or `tests/`.
- Half B (planner-driven, `run_arm.sh t4 saddle`, three seeds): record per
  seed the node count, each node's `kind` and `target_files` (declared or
  empty), and the gate that failed first, if any. Prediction: the planner
  emits an `impl` node for the fix; if it also emits a `test` node the
  red-specification path (T3-7a) is exercised on a benchmark task for the
  first time; a plan with an empty `target_files` is recorded as such
  (this is the #64 number). Expected: no sealed proof whose diff edits a
  source file and a test file together. If one seals, that is the finding
  and it names the gate that let it through.
Falsified if: any seed in either half seals a proof over a diff that
touches both a source module and a test file, or (half A) over a diff
that touches `discounts.py`.
Steps: 1. Copy the prediction above into `../saddle-bench/runs/t4v3/PREDICTION.md`
before the first run. 2. Half A: write `../saddle-bench/prompts/t4.dag.json`
(the one-node DAG above; validate it with `saddle run --dag ... --yes`
against an empty scratch repo first, expecting a target-scope or
node-scope failure, not a crash). Run three seeds
(`--sample-temperature 0.7`, journal per seed). 3. Half B: `run_arm.sh
t4 saddle` three times with the journal moved aside between runs. 4. For
every seed, record: files in each attempt's diff (from the journal
records' diffs), the first failing gate and its detail, whether a proof
sealed, the sealed diff's file set. 5. F20 in FINDINGS.md: one table,
six rows; BENCHMARK-RECORD.md gets the summary and the prediction's
verdict. Never print the key.
Known-good / Known-bad: the prediction is the bar; both halves are records.
Done when: PREDICTION.md predates every journal in `runs/t4v3/`, six
seeds recorded, F20 written with the verdict (held / falsified) and, if
falsified, the gate named.
Stop if: the container is down (report); T3-26 not landed (run half B
only and say so); any seed crashes rather than fails a gate (that is a
T3-shaped defect, record and stop).
Decision this feeds: whether `target_files` stays opt-in (#64) and
whether the #65 residual needs a run of its own.

### T4-4 — `structural_tag` / `enable_in_reasoning` against vLLM 0.28 (precedes D14)
Status 2026-09-19: DONE by session 29, commit f955883 (measurement +
record; no contract change). Prediction written before any request;
records in `../saddle-bench/runs/t4-4-structured-output-2026-09-19/`
(F16-F19 in its RESULT.md; the F-numbers after F15 are therefore taken
through F19). Result: with `structured_outputs={"grammar":
DIFF_GRAMMAR}` and reasoning on, the think block is byte-identical to
the unconstrained control and the completion is a diff -- the D14
alternation is already what the shipped payload does, no flag involved.
An `enable_in_reasoning`-style key is silently dropped (200, identical
output, same as a made-up key), so it must never be written into a
payload. Structural tags validate only in the legacy `response_format`
shape, carry a JSON Schema rather than a grammar, and fire only if the
model chooses to emit the tag: D14 is closed against them, with the
reasoning in DESIGN-NOTES D14. Three probe mutants KILLED, one after the
executor added a guard because its first form survived silently -- the
right call, recorded. Reviewer re-ran `./check.sh` at f955883 (see the
commit carrying this line). Noticed by the executor, left for a docs
item: DESIGN-NOTES:425 dangling fragment and :629 summary line now
understate D14; item line numbers drifted (`DIFF_GRAMMAR` :66, request
:217).
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

## 8. Tier 5 — the harness feels like a session, not a pipeline (QoL)
Added 2026-09-19 after Tier 3 closed. Same template and rules as Tiers 2-3: every
item names a contract, a direction, known-good and known-bad instances,
and 1-3 contract mutants. UX work goes through the same gate; nothing in
this tier may loosen a check.

Goal, in the user's words: "I would like the QoL to be really nice for
this harness, where it feels like using claude code." What that feel is,
concretely: one command takes a task in prose and you watch it work in
the same terminal; when it stops, the last line tells you what to type
next; picking a run back up needs no journal plumbing; the defaults are
where you left them.

Facts at HEAD the items lean on (verified by reading `cli.py`):
`saddle run TASK` emits, asks `ask_confirm` unless `--yes`, runs, then
writes the finished transcript to stdout at the end (cli.py:571); live
progress exists only as `saddle tail JOURNAL` in a second terminal
(`render_event`, cli.py:687); every failure is an `error: ...` line and
exit 1, with the T3-10 mismatch the only one that names a next command;
the API key is the only environment input (cli.py:781); `--base-url`,
`--model`, efforts and temperatures are flags with hard-coded defaults
on five subcommands.

Sequencing: T5-0 first (it is the evidence the rest cites), then T5-1,
T5-3, T5-2 in that order (each is one session), then T5-4 and T5-5 in
either order, T5-6 last. T5-7 and T5-8 are decisions, not work.

### T5-0 — Dogfood transcript: every manual step of one real run (record; no code)
Files: none in the repo. Write `../saddle-notes/dogfood-2026-XX-XX.md`.
Contract: one person runs one small real task through `saddle run` on a
scratch repo against the container, from an empty shell to a verified
journal, and writes down every command typed, every output read, every
moment of "what do I do now", and the wall-clock time of each. Then
repeats after a deliberate failure (kill the run mid-node with Ctrl-C,
then get it back to green).
Direction: none (record).
Evidence: this is the evidence; T5-1..T5-6 cite its line numbers the
way Tier 3 cites F-numbers. An item below with no line in this record
behind it is speculation and waits.
Steps: run, record, count. The count that matters: commands typed
between "task in hand" and "verified journal", and seconds spent
reading output that did not tell you what to do next.
Known-good / Known-bad: n/a. Done when: the record exists and each
T5 item below carries a `Dogfood:` line citing it.
Stop if: the container is down (report, do not restart it).

### T5-1 — `saddle run` shows the run as it happens (Major)
Files: `src/saddle/cli.py` (`run_run` ~:481-571, the final
`stdout.write(result.transcript)`; `run_tail` ~:660-695 is the renderer
to reuse); `src/saddle/slice.py` (`run_slice` takes a `now` callable
today; it needs an `on_event: Callable[[ProofRecord | SpanRecord],
None] | None = None` it calls after every `append_record`/`append_span`);
`src/saddle/transcript.py` (`render_event`); `tests/test_cli.py`,
`tests/test_slice.py`.
Contract: while `saddle run` is running, every journal entry is
rendered to stderr by `render_event` the moment it is sealed, in
journal order, so the user never opens a second terminal; the finished
transcript still goes to stdout at the end unchanged, so `saddle run
... > transcript.md` keeps working; `--quiet` suppresses the live
lines and nothing else.
Direction: none for the gates (presentation only); the journal is still
the record and `saddle tail` still works on it. Say so in the commit.
Dogfood: cite the T5-0 line where the second terminal was opened.
Steps: 1. `run_slice(..., on_event=None)`: after each
`append_record(journal_path, record)` and `append_span(journal_path,
span)` call `if on_event: on_event(entry)`. There are several call
sites; grep `append_record(\|append_span(` in slice.py and cover each.
2. `run_run` passes `on_event=lambda e: _emit(stderr, e)` where `_emit`
writes `render_event(e)` lines and flushes; `--quiet` passes `None`.
3. Tests: a `run_slice` test with a recording `on_event` asserts the
callback sequence equals `read_entries(journal)` (same objects, same
order); a `run_run` test asserts stderr carries the `[n1] sealed` line
and stdout carries the transcript; `--quiet` asserts stderr is empty.
Known-good: the two-node pass fixture streams N lines to stderr and
stdout equals today's transcript byte for byte. Known-bad: an
`on_event` that raises must not corrupt the journal -- the entry is
already appended before the callback; test that a raising callback
leaves `verify_journal` clean.
Contract mutants (each target occurs once; abort if `grep -c` is not 1; drop `__pycache__` after each revert):
1. drop the `on_event(record)` call after the sealing `append_record` -> the callback-sequence test red (missing the proof entry).
2. `run_run`: pass `on_event=None` unconditionally -> the stderr test red.
Done when: mutants red; `./check.sh` green; T5-0's second-terminal step is gone on a rerun.
Stop if: `run_slice` already has an event callback (then only the CLI half is owed).

### T5-2 — Picking a run back up needs no journal plumbing (Major)
Files: `src/saddle/cli.py` (`run_run`; the T3-10 `ValueError` path that
prints `git restore --source ...`); `src/saddle/slice.py`
(`_seed_proofs`, `_ensure_clean`); `tests/test_cli.py`, `tests/test_slice.py`.
Contract: (1) `saddle run TASK` with the default journal already holding
proofs for this task says, before the plan confirmation, what it will
reuse and what it will redo (`resuming: 2 proven, 1 to run, 1 dropped
(node changed)`), from `_seed_proofs`'s existing summary; (2) when the
worktree does not match the proven tree, `saddle run --restore` performs
the `git restore --source <ref> --staged --worktree -- .` itself after
`ask_confirm` shows the exact command, then continues; without
`--restore` the message is unchanged and the exit is 1; (3) a journal
sealed for a different task still stops the run, but the message names
the two commands that resolve it (`--journal PATH` for a new journal,
or `saddle status` to see what the old one holds).
Direction: (2) is a convenience over an action the user already had to
take by hand -- it is **not** a loosening because the restore only runs
on the tree hash the proof sealed, and only after confirmation; say so.
Dogfood: cite the T5-0 lines for the restore-by-hand and the
"which journal is this" moment.
Steps: 1. `run_run` calls `_seed_proofs` (or a thin `plan_resume`
wrapper in slice.py that returns the summary without running) before
`ask_confirm` and prints the summary line. 2. Add `--restore`; on the
mismatch `ValueError`, if set, confirm and run the restore via
`run_argv`, then call `run_slice` again once. 3. Tests: the mismatch
fixture from T3-10 with `--restore --yes` ends green and the journal
holds the resumed proof; without `--restore` it is byte-identical to
today's error; a different-task journal's message contains
`--journal`.
Known-good: resume after a clean Ctrl-C mid-node ends with the same
proofs a fresh run would seal. Known-bad: `--restore` on a tree that
has *uncommitted user edits outside the proven files* must refuse, not
restore -- `_ensure_clean`'s check runs first; test that it does.
Contract mutants (each target occurs once; abort if `grep -c` is not 1; drop `__pycache__` after each revert):
1. `--restore`: skip `ask_confirm` -> the confirmation test red.
2. `--restore`: run the restore without re-checking `_ensure_clean` -> the dirty-tree known-bad red.
Done when: mutants red; `./check.sh` green.
Stop if: T5-1 has not landed (the summary line reuses its stderr channel).

### T5-3 — Every stop names the next command (Medium)
Files: `src/saddle/cli.py` (every `return 1` path: ~:496, :503, :570,
:581, :644, :655, :682, :685, :800, :822, :854); `src/saddle/ux.py`;
`tests/test_cli.py`.
Contract: every line `saddle` prints that begins `error:` is followed by
a line beginning `next:` that holds one runnable command or one
sentence naming the file to edit; the `next:` line is produced by one
helper (`ux.stop(reason, next_command)`) so no path can forget it.
Direction: none (messages). Evidence: T3-10's restore message is the
one example of this today and the T5-0 record shows the rest.
Steps: 1. `ux.stop(stderr, reason, *, then)` writes both lines. 2.
Replace each `error:` write with it; the `then` text per path: missing
key -> `export SADDLE_VLLM_API_KEY=... (or put it in ~/.config/saddle/env)`;
doctor failure -> `saddle doctor --base-url ...`; invalid DAG ->
`saddle dag "TASK"` to see the plan; journal corrupt -> `saddle verify
JOURNAL`; declined confirmation -> `saddle run --yes ...`; T3-10
mismatch -> `saddle run --restore ...` (after T5-2) or the git command
(before). 3. Test: parametrize over every failure fixture in
`test_cli.py` and assert `next:` follows `error:` in the captured
stderr; add a test that greps `cli.py` for `"error:` outside `ux.stop`
and asserts zero -- that is the structural half, and it is decorative
alone, so keep the behavioural parametrization as the load-bearing one.
Known-good: each fixture's `next:` is a command that, typed, resolves
that failure (the test for the confirmation path actually runs it).
Known-bad: an `error:` with no `next:` fails the parametrized test.
Contract mutants (each target occurs once; abort if `grep -c` is not 1; drop `__pycache__` after each revert):
1. `ux.stop`: drop the `next:` write -> every parametrized case red.
Done when: mutant red; `./check.sh` green; `grep -c '"error:' src/saddle/cli.py` counts only the helper.

### T5-4 — Defaults live in a config file, flags override (Medium)
Files: `src/saddle/cli.py` (`build_parser` ~:700-770, `_api_key` ~:781);
new `src/saddle/config.py`; `tests/test_cli.py`, `tests/test_config.py`.
Contract: `saddle` reads `REPO/.saddle/config.toml` then
`~/.config/saddle/config.toml` (repo wins) for `base_url`, `model`,
`reasoning_effort`, `worker_effort`, `temperature`,
`sample_temperature`, `max_tokens`, `journal`; precedence is flag >
repo file > user file > built-in default, and `saddle doctor` prints
where each effective value came from. The API key stays environment
only (never in a file the repo could see; the existing rule).
Direction: none for the gates. Note that `.saddle/config.toml` must be
listed in the harness's own untracked-artefact set wherever
`.saddle/proofs.jsonl` is (T2-2's untracked rule) -- grep for
`proofs.jsonl` in `gates.py`/`evidence.py` docstrings.
Dogfood: cite the T5-0 lines where `--base-url`/`--model` were retyped.
Steps: 1. `config.load(repo) -> Settings` (a frozen dataclass; `tomllib`
is stdlib). 2. `build_parser` takes `defaults=Settings` and sets
`default=` from it; `argparse` then gives flag-over-file for free. 3.
`doctor` prints `model: qwen... (from .saddle/config.toml)` per key. 4.
Tests: precedence for one key across all four layers (four tests, one
per winner); a key the file misspells raises with the file path and
key named; the API key present in a file is *ignored* and a warning
names the rule.
Known-good: an empty file changes nothing (byte-identical `--help`).
Known-bad: `api_key = "..."` in the file is ignored and warned.
Contract mutants (each target occurs once; abort if `grep -c` is not 1; drop `__pycache__` after each revert):
1. swap the merge order (user file over repo file) -> the precedence test red.
2. read `api_key` from the file into the client -> the warned-and-ignored test red.
Done when: mutants red; `./check.sh` green.

### T5-5 — `saddle status [JOURNAL]`: what a journal holds, without running anything (Medium)
Files: `src/saddle/cli.py` (new subcommand beside `verify`);
`src/saddle/journal.py` (`proven_records`, read only); `src/saddle/slice.py`
(`_seed_proofs` needs a DAG; `status` reports what it can without one);
`tests/test_cli.py`.
Contract: prints, from the journal alone: the task hash and the first
record's timestamp; each proven node with its record hash, kind,
target files and tree hash; the last span (so an interrupted run shows
where it stopped); whether the current worktree's `git write-tree`
equals the last reused proof's tree hash (`worktree: matches proof
n2` / `worktree: differs from proof n2 -- saddle run --restore`); and
the chain verdict from `verify_journal`. Exit 0 if the chain verifies,
1 otherwise; never writes.
Direction: none (read-only). Dogfood: cite the "which journal is this"
line.
Steps: `run_status(journal, repo, stdout)`; render via a small function
in `transcript.py` beside `render_journal_transcript`; tests over the
T3-10 fixtures (matching, mismatching, empty journal, missing file ->
`error:` + `next: saddle run`).
Known-good: after the two-node pass fixture, `status` lists n1, n2 and
`matches`. Known-bad: after `git checkout HEAD -- n.py`, `status` says
`differs` and names the restore.
Contract mutants (each target occurs once; abort if `grep -c` is not 1; drop `__pycache__` after each revert):
1. compare the tree hash against `""` instead of the worktree -> the `differs` test red.
Done when: mutant red; `./check.sh` green.

### T5-6 — The live view reads like a session transcript (Minor)
Files: `src/saddle/transcript.py` (`render_event` :80-104); `tests/test_transcript.py`.
Contract: the live lines carry, per node, a header when a node starts
(`── n2  impl  target: n.py`), one line per attempt (`attempt 2/3`),
one line per gate with a pass/fail mark and its detail, elapsed time
per node at seal, and the run verdict as the last line; the journal
format does not change (this is rendering of spans that already exist
-- check each field is already sealed before rendering it; a field
that is not there is a T2-4-shaped change and out of scope).
Direction: none. Depends on T5-1 (nobody sees this without it).
Dogfood: cite the lines spent reading `tool git: exit 0 in 12ms: ...`.
Steps: extend `render_event`; keep `is_run_end` untouched; snapshot
tests on a fixed journal fixture (the existing `verify` fixtures), one
per line kind.
Known-good: a fixture journal renders to the expected text. Known-bad:
a span with `exit_code != 0` renders the fail mark, not the pass mark.
Contract mutants (each target occurs once; abort if `grep -c` is not 1; drop `__pycache__` after each revert):
1. render the pass mark unconditionally -> the fail-mark test red.
Done when: mutant red; `./check.sh` green.

### T5-7 — Chat-driven runs (#23): decision needed
`saddle up` is a streaming chat with tools; `saddle run` is the gated
pipeline. #23 asks for a chat turn to execute through the pipeline.
Options: (A) a `/run TASK` command inside `up` that calls `run_run`
with `on_event` streaming into the chat pane -- small once T5-1 exists;
(B) the model in `up` gets a `saddle_run` tool -- lets the model decide
to run, which is the #65 shape (the model choosing its own gate path)
and is not recommended; (C) leave separate. Recommend A after T5-1..3.
No steps until decided.

### T5-8 — Rich TUI (#17): not now
Every item above is plain stdout/stderr on purpose: it works over ssh,
in CI logs, and in `saddle run > file`. A TUI is a second renderer over
the same journal and can come after T5-0 is rerun on the finished tier
and still shows time spent reading. Keep #17 open, unchanged.

## 9. Tier 6 — the specification is the blind spot (from round 3)

Added 2026-09-20 from the executor's mid-sweep notes on T1 and T2
(`../saddle-bench/runs/round3/DESIGN-NOTES-T1-T2.md`, commit dff4b81; F21.6
records the diagnosis, F21.8 the executor's own follow-on list, which this
tier supersedes item by item). The finding: T1 sealed a pristine proof
over an implementation the oracle scored 11/18, and the worker met its
specification exactly; the planner's requirement statements were weaker
than the task, the test node's reading became the contract (red-phase),
and every gate then verified consistency with that reading. Eleven
gates, one premise. Nothing here makes the harness *right* about an
under-specified task; the goal is that it deterministically knows when
it cannot certify. Design constraint, set by the user: **no adversarial
review nodes.** Every mechanism below is procedural code whose verdict
the model cannot argue with and whose thresholds live in `gates.py`,
never in the plan.

Sequencing: T6-14 and T6-8 first, one sitting (the T5 class: a
truncated attempt is retried with more room, the emission budget stops
being a side effect of the reasoning effort, and a node-too-large plan
is rejected before any model call; none needs the retrospective because
T5's journal is the known-bad), T6-12 and T6-13 second, one sitting (a failed attempt's reasoning
and the plan itself are journaled, so the next T5-shaped failure is
diagnosable from the journal alone), then T6-1 (one session, no GPU; it scores everything else
against the thirteen labelled runs and decides which items proceed).
T6-2 and T6-3 have their known-bad already (T2's log) and follow
directly. T6-4 with T6-5 in one main-session sitting. T6-6 only if T6-1
says Proposal A separates. T6-7 is bench-side and waits for the round to
close. Then Tier 5 (T5-0..), then the rest of Tier 4 (T4-1, T4-5, T4-2,
T4-3, T4-6b): measurement of a harness that no longer has this hole is
worth more than measurement of one that does.

### T6-0 — F21.6 is the diagnosis; DESIGN-NOTES gets the pointer (docs-only)
Files: `docs/DESIGN-NOTES.md` (D-list entry pointing at F21.6 and T6-*;
also fix the D14 :425 fragment and :629 summary the T4-4 executor
noticed), `docs/BENCHMARK-RECORD.md` (already carries round 3). F21.6 in
`../saddle-bench/runs/FINDINGS.md` records the "eleven gates, one
premise" diagnosis with the run paths; nothing in the repo restates it
beyond a pointer. Done when: DESIGN-NOTES names F21.6 and every T6 item
below cites its F21.x.
### T6-1 — Retrospective: score every candidate gate on the round-3 corpus (no GPU)
Files: `../saddle-bench/oracles/retro/` (new; one script per candidate,
each takes a run worktree + journal and prints FIRE/QUIET with a
number), `../saddle-bench/runs/round3/RETRO.md` (the table), read only:
every `runs/round3/*/` worktree and journal. Requires T4-6a complete
(13 runs with gate verdict and oracle verdict both recorded).
Question: for each of {A behavioural mutation, B accepts/rejects
near-miss rule, C property polarity, 6a baseline-relative ruff, 6b
generated-artefact exclusion}, would the gate have fired on the
oracle-FAIL runs and stayed quiet on the oracle-PASS runs, on runs that
already happened?
Pre-registered (write into RETRO.md before scoring): A fires on T1 and
on no PASS; B fires on T1 (REQ-002 has no reject) and on no PASS; C
fires on T1 and possibly on PASS runs with positive-only properties
(record which -- that is its false-positive rate); 6a is QUIET on every
run's *introduced* lint but FIRE on T2's baseline lint (i.e. today's
gate failed a node for lint the baseline carried); 6b fires on T2's
replan (bytecode declared as targets) and nowhere else.
Steps: one script per candidate, deterministic, no model calls; A on
predicate-shaped nodes only, flipping the entry point's return at each
probe input derived from the requirement literals by regex, one suite
run per probe (cap 200, sampled, seed recorded); B and C as AST/regex
over the journal's DAG and test diff; 6a as `ruff check` on the baseline
vs the node's worktree, diffed by (file, line-of-introduction); 6b as the
touched-file list with the fixed exclusion set applied. RETRO.md: one
row per run per candidate, then a summary per candidate: fires on
FAILs, fires on PASSes, verdict (worth implementing / tax / decorative).
Known-good / Known-bad: the prediction is the bar.
Done when: RETRO.md has 13 x 5 cells and five verdicts; each T6 item
below carries a `Retro:` line citing its row. Stop if: fewer than 13
runs have both verdicts (score what exists, say so).

### T6-2 — target-scope ignores generated artefacts (scope narrowed; tightened in effect)
Files: `src/saddle/gates.py` (`check_target_files` ~:416; new module
constant `GENERATED_ARTEFACTS` beside `PYTEST_TESTS_FAILED`),
`src/saddle/runner.py` (where `touched` is computed from
`git_changed_files` | `git_added_files`), `tests/test_gates.py`,
`tests/test_runner.py`.
Contract: a touched path is excluded from target-scope when any of its
`/`-segments is `__pycache__`, `.pytest_cache`, `.hypothesis`, or the
file name matches `*.pyc`, `.coverage*`; the list is a frozen tuple in
`gates.py` and nothing in the plan or the node can add to it. Excluded
paths are named in the gate detail (`ignored 2 generated file(s)`) so
the proof still says they moved.
Direction: scope narrowed (fewer paths compared), and tightened in
effect: a node can no longer pass target-scope by declaring bytecode as
a deliverable, which T2's replan did (`target_files = ['retries.py',
'__pycache__/retries.cpython-312.pyc', ...]`) -- declaring a generated
path is rejected at validation.
Evidence: T2 s1 log (`runs/round3/t2-s1/`), the replan's target list;
6b in the executor's note, hypothesis weakened but the fix standing.
Retro: T6-1 row 6b.
Steps: 1. `GENERATED_ARTEFACTS` + `is_generated(path) -> bool`. 2.
`check_target_files` filters `touched_files` through it and reports the
count. 3. `Node._repo_relative_posix` rejects a `target_files` entry
that `is_generated`. 4. Tests: known-good `check_target_files(["n.py"],
["n.py", "__pycache__/n.cpython-312.pyc", ".coverage.tier1"])` passes
with `ignored 2`; known-bad `["n.py"], ["n.py", "other.py"]` still fails
naming `other.py`; `target_files: ["__pycache__/x.pyc"]` raises
`ValidationError`.
Contract mutants (each target occurs once; abort if `grep -c` is not 1; drop `__pycache__` after each revert):
1. `GENERATED_ARTEFACTS` -> `()` -> the known-good red (bytecode counted as a stray).
2. drop the validator clause -> the declared-bytecode known-bad red.
Done when: mutants red; `./check.sh` green.

### T6-3 — ruff fails a node only for lint its own diff introduced (scope narrowed)
Files: `src/saddle/gates.py` (`check_ruff` ~:106), `src/saddle/runner.py`
(the ruff runner: it must run twice, baseline leg on the T3-8 snapshot
and current leg, and diff the findings), `src/saddle/evidence.py`,
`tests/test_gates.py`, `tests/test_runner.py`.
Contract: the ruff gate fails when the current worktree has a finding
(rule, file, line-content) absent from the node's baseline; a finding
the baseline already carried is reported in the detail as `inherited: N`
and does not fail the node. Formatting (`ruff format --check`) is
unchanged: the harness already formats the diff, so a format failure is
always introduced. Autofixable findings are still fixed before the gate,
as today (#66).
Direction: scope narrowed. Not a loosening of the standard: every
finding the node introduces still fails it. The evidence CLAUDE.md asks
for: T2's impl node burned three attempts on BLE001 at a line
`baselines/t2/retries.py` already shipped, and the accepted fix was
`# noqa: BLE001` on pre-existing code -- suppression, not repair. A
correct `range(attempts)` fix was refused three times over lint the
node did not write. Same principle as T3-8 (gate the node against its
own baseline), applied to lint.
Retro: T6-1 row 6a.
Steps: 1. runner captures `ruff check --output-format json` on both
legs (the baseline leg from the T3-8 snapshot ref, same as red-phase).
2. `check_ruff(introduced, inherited, format_exit)` becomes the
predicate; match findings by (code, path, stripped source line) so a
pure line shift is not "introduced". 3. Tests: known-good a baseline
with BLE001 and a diff that adds a clean function passes with
`inherited: 1`; known-bad a diff adding `except Exception:` to a clean
baseline fails naming BLE001; a diff that moves the inherited line down
three lines still passes.
Contract mutants (each target occurs once; abort if `grep -c` is not 1; drop `__pycache__` after each revert):
1. compare against `set()` instead of the baseline findings -> the known-good red (inherited counted as introduced).
2. `if introduced:` -> `if False:` -> the known-bad red (introduced BLE001 no longer fails).
Done when: mutants red; `./check.sh` green. Owner: main session.

### T6-4 — Requirements carry accepts and rejects, and rejects must be near-misses (Proposal B; tightened)
Files: `src/saddle/dag.py` (`Requirement` ~:36: new `accepts:
list[str]`, `rejects: list[str]`, both `min_length=1`; validator: every
reject within edit distance `REQ_NEAR_MISS_K` of some accept),
`src/saddle/gates.py` (`REQ_NEAR_MISS_K` constant; new
`check_requirement_examples(examples, run)`: the node's implementation
is exercised on every cited example through the node's own test command
by a generated example test, or -- simpler and preferred -- the examples
are required to appear as literal assertions in the test node's diff and
the gate checks presence AND that the suite is green), `src/saddle/cli.py`
(planner prompt ~:172-178 gains the two arrays and the near-miss rule
with T1's own `"user"` as the counter-example; worker prompt ~:258
prints them), `src/saddle/vllm.py` (schema: the arrays, `minItems: 1`;
NOT the distance rule -- pydantic post-hoc, per T3-2's lesson on
patterns), `tests/test_dag.py`, `tests/test_gates.py`, `tests/test_cli.py`.
Contract: a requirement without at least one accept and one reject is
invalid; a reject farther than `k` edits from every accept is invalid
(`k` in gates.py; start at 3 and let T6-1's row B tune it); the
requirement-binding gate additionally fails when a cited example has no
assertion in the node's tests.
Direction: tightened. Known-bad from the run: T1's REQ-002 has no reject
at all; REQ-001's `"user"` is 12 edits from `user@example.com`.
Retro: T6-1 row B.
Known-good: `accepts=["user@example.com"], rejects=["user@@example.com",
"user@example.com."]` validates; known-bad: `rejects=["user"]` raises
naming the distance; empty `rejects` raises.
Contract mutants (each target occurs once; abort if `grep -c` is not 1; drop `__pycache__` after each revert):
1. the distance check `> REQ_NEAR_MISS_K` -> `> 10**6` -> `"user"` accepted, known-bad red.
2. `min_length=1` on `rejects` -> `0` -> empty-rejects known-bad red.
3. the examples-present check -> `True` -> the gate known-bad red.
Done when: mutants red; `./check.sh` green; the planner prompt test pins
the new sentence (T3-12 style). Owner: main session (schema + prompt +
gate; the planner-schema change must be verified against the decoder,
CLAUDE.md "Match the enforcing engine").

### T6-5 — A test node's properties must include a rejecting one (Proposal C; tightened, floor)
Files: `src/saddle/gates.py` (`check_property_coverage` ~:446: new
polarity clause), `src/saddle/evidence.py` (the AST walk that finds
`@given` functions gains a polarity classifier: a property whose
assertions are all `is True` / `== True` / truthy on the target call is
positive; one with `is False` / `not f(x)` / `pytest.raises` is
negative), `tests/test_gates.py`, `tests/test_evidence.py`.
Contract: a `test` node passes property-coverage only if at least one
property is negative-polarity. Direction: tightened. Known-bad: T1's
single positive-only property. Known limit, stated in the detail: a lazy
negative generator passes this floor; T6-6 is the real defence.
Retro: T6-1 row C (its false-positive rate on PASS runs decides whether
this lands at all).
Contract mutants (each target occurs once; abort if `grep -c` is not 1; drop `__pycache__` after each revert):
1. polarity clause `-> True` -> T1-shaped known-bad red.
Done when: mutant red; `./check.sh` green. Bundle with T6-4 in one sitting.

### T6-6 — Behavioural mutation on predicate nodes (Proposal A; tightened; pilot)
Files: `src/saddle/evidence.py` (new `behaviour_probe(...)`: derive probe
inputs from the requirement literals -- single-character edits: insert
space, duplicate dot, move dot to a boundary, second `@`, hyphen at a
label edge, delete one char; wrap the entry point so it returns the
negation at exactly that input; run the suite; count unconstrained),
`src/saddle/gates.py` (`check_behaviour_constraint(ratio)`,
`BEHAVIOUR_CONSTRAINED_MIN` beside `PYTEST_TESTS_FAILED`),
`src/saddle/runner.py` (runs it for `impl` nodes whose target function
returns bool -- detected from the annotation or from the suite's
assertion shapes; otherwise `not required: non-predicate`),
`tests/test_evidence.py`, `tests/test_gates.py`, `tests/test_runner.py`.
Contract: for a predicate-shaped impl node, the fraction of probe inputs
whose flipped answer the suite detects must be at least
`BEHAVIOUR_CONSTRAINED_MIN`; the ratio and the probe count are in the
proof. The threshold and the edit set live in gates.py.
Direction: tightened. Prerequisite: T6-1 row A shows separation (fires
on T1, quiet on PASSes); otherwise this item is not started.
Known-good: a validator with both-sided tests on the T1 stub is
constrained on most probes; known-bad: the T1 shipped two-liner with its
shipped suite reports ~4/200.
Costs, stated: one suite run per probe; cap and sampling in evidence.py
with the seed sealed; non-boolean returns out of scope for the pilot.
Contract mutants (each target occurs once; abort if `grep -c` is not 1; drop `__pycache__` after each revert):
1. `BEHAVIOUR_CONSTRAINED_MIN` -> `0.0` -> known-bad red.
2. the flip `not original(x)` -> `original(x)` -> no probe is ever detected, so the known-good's ratio reads 0 -> red.
Done when: mutants red; `./check.sh` green; a rerun of T1 alone (one
seed, container) shows the node not sealing with the ratio in the
transcript. Owner: main session.

### T6-7 — Baselines stop committing bytecode (bench-side; after the round)
Files: `../saddle-bench/baselines/t2,t3,t4` (tracked `.pyc`: 2, 4, 6),
`../saddle-bench/.gitignore`, `runs/round3/BASELINES.txt` (record the
before/after HEADs). Not before T4-6a's table is written: the baselines
are frozen pre-registration material for the round. Then: remove the
tracked bytecode, add `__pycache__/` to the ignore, record new HEADs,
and note in BENCHMARK-RECORD that round 4 baselines differ from round 3
in this and only this.

### T6-8 — Node-size pre-flight: a plan whose node cannot be emitted in one diff is rejected before execution (#64; tightened)
Files: `src/saddle/dag.py` (`validate_dag` ~:330; new `_oversized_nodes(dag,
sizes, budget)` beside `_uncovered_requirements` ~:288; `Node.target_files`
becomes required non-empty for `impl` and `refactor` nodes), `src/saddle/cli.py`
(`WORKER_OUTPUT_TOKENS` ~:77 is the budget source; `_emit_valid_dag` ~:419
feeds the issue back to the planner as a replan reason; planner prompt
~:186 gains the rule), `src/saddle/slice.py` (`splice_replan` path: a
replacement DAG is validated the same way), `tests/test_dag.py`,
`tests/test_cli.py`.
Contract: for every node, `sum(baseline line count of each target_file)`
times a fixed per-line token factor must not exceed
`WORKER_OUTPUT_TOKENS[effort]` for the node's effort; otherwise
`validate_dag` yields a `node-too-large` issue naming the node, its
estimate and the budget, and the run re-plans before any worker call. A
missing file counts by the node's own estimate (a new file is what it
adds, so it is bounded by the budget alone). An `impl` or `refactor`
node with empty `target_files` is invalid: the check cannot be evaded
by declaring nothing. The factor and the rule live in `dag.py`/`gates.py`,
not in the plan. The budget must be the *emission* budget: on this
server thinking tokens count against the same `max_tokens` cap, so
`WORKER_OUTPUT_TOKENS[effort]` minus the reasoning allowance for that
effort is what the diff can occupy -- T5 s1 truncated twice at 81920
output tokens for a diff that needs ~8 KB, which is reasoning filling
the cap. The allowance per effort is a constant next to the factor,
measured by T4-3, not guessed; until T4-3 reports use the round-3
journals' `usage` once T6-12 records it. Whether the planner keeps
choosing the worker's `reasoning_budget` at all is T4-3's decision
(#50-shaped: a resource, not a bar, so integrity is not at stake, but
viability is).
Direction: tightened. Evidence: round-3 T5 s1 (`runs/round3/t5-s1/`):
one node over four modules of a 443-line repo; attempt 1 truncated
(`finish_reason=length`), attempt 2 unappliable, attempt 3 the same;
worktree untouched after ~1800 s. Round 2 F10 recorded the identical
shape. #64 asked for exactly this check; #51's part 1 is the same defect.
Known-good: a one-node DAG over a 40-line file at medium effort
validates; the same node with `target_files` naming four 100+-line files
raises `node-too-large`; the two-node split of it validates. Known-bad:
T5's emitted DAG from the round-3 journal, replayed through
`validate_dag`, is rejected.
Contract mutants (each target occurs once; abort if `grep -c` is not 1; drop `__pycache__` after each revert):
1. the comparison `estimate > budget` -> `estimate > 10**9` -> T5's DAG validates, known-bad red.
2. the non-empty `target_files` rule -> dropped -> an `impl` node with `[]` validates, red.
3. `_emit_valid_dag` ignores `node-too-large` when deciding to re-plan -> the CLI test red (worker called on an oversized node).
Done when: mutants red; `./check.sh` green; then one container run of T5
(one seed) to see whether the planner produces a plan that validates,
and how many nodes -- that run is the measurement, recorded in F21's
successor. Owner: main session. Note: this supersedes the "opt-in"
decision in T3-2 and closes #64's remaining half; say so in the commit.

### T6-9 — A run on a clock seals what it has (deadline; no gate change)
Files: `src/saddle/slice.py` (`run_slice(..., deadline_s: float | None =
None)`; `_schedule_until_done` stops dispatching new nodes when
`remaining < median node wall so far`, or at the deadline, and
`_seal_run` writes the run span with `detail="deadline: N proven, M
undispatched"`), `src/saddle/cli.py` (`--deadline SECONDS`),
`tests/test_slice.py`, `tests/test_cli.py`; `../saddle-bench/run_arm.sh`
(pass `--deadline 1700` instead of relying on `timeout 1800` alone).
Contract: with a deadline, a run never ends with an empty journal when
at least one node has sealed, and never leaves a failed attempt's diff in
the worktree: the in-flight node is given until the deadline to finish
its current attempt, then `_abandon` runs for it exactly as on any other
give-up path (T3-23), so the worktree holds proven edits only -- F21.2's
"unproven residue" (t6-s1, t6-s2: the correct module in the tree, the
proof covering only the test file) cannot recur under an external kill
that saddle can see coming. F21.8 item 4 is this clause; proofs already sealed stay sealed, no node is started that cannot
finish, exit code is distinct (3) and the run span says so; a later `saddle run` on the same journal resumes (T3-1,
T3-9, T3-10). Direction: none for the gates. Evidence: round 2 twice and
round 3 T5 ended with "produced nothing, worktree untouched" under an
external kill that saddle could not see. Known-good: a three-node
fixture with a deadline that fits one node seals one proof and exits 3;
resuming with no deadline finishes the other two. Known-bad: a node
already running at the deadline is not killed mid-gate -- it finishes
and seals; assert that.
Contract mutants (each target occurs once; abort if `grep -c` is not 1; drop `__pycache__` after each revert):
1. the dispatch guard -> `if False:` -> the three-node known-good dispatches all three, red.
Done when: mutant red; `./check.sh` green.

### T6-10 — The 3-way rung of the apply ladder is dead for model diffs (scope narrowed, with proof)
Files: `src/saddle/slice.py` (`_apply_diff` ~:171; `_APPLY_MODES`),
`tests/test_slice.py`. Evidence: round-3 T5 s1 attempt 2: `error:
repository lacks the necessary blob to perform 3-way merge`. A worker
diff carries invented `index <a>..<b>` lines; `git apply --3way` needs
those blobs to exist in the object store, so the last rung has never
been able to succeed on any model-emitted diff and only adds a failed
subprocess and its stderr to every apply failure.
Contract: `_apply_diff` strips `index ...` lines from the proposal
before the ladder runs (they carry no information git needs for a
non-3-way apply), and the 3-way rung is removed; the ladder is `strict
-> ignore-whitespace -> reduced-context`. Direction: scope narrowed
(one rung fewer; nothing that could apply before fails to apply now --
prove it: the existing apply fixtures pass on every remaining rung
exactly as before, byte-identical spans minus the 3-way one). Known-good:
a diff with fake index lines that applies on the strict rung still
applies; known-bad: a diff that only 3-way could apply -- none exists
for a model diff, and the test asserts the 3-way rung is not attempted.
Contract mutants (each target occurs once; abort if `grep -c` is not 1; drop `__pycache__` after each revert):
1. re-add the 3-way rung to `_APPLY_MODES` -> the not-attempted test red (an extra `git apply --3way` span).
2. skip the `index` strip -> the test that asserts the applied diff carries no `index` line red.
Done when: mutant red; `./check.sh` green; the T2-1/T3-8 span pins updated with the reason in the commit.

### T6-11 — Not an item: sequential sampling already stops early
Recorded so it is not proposed again. `_best_of_samples` draws one
sample at a time, gates it, and `break`s on the first with zero failing
checks (T2-1). T1's 119 s / 126 s per attempt were spent gating
candidates (each runs full Tier-1 including mutmut), not drawing
redundant samples. If sampling cost is worth attacking, the item is a
cheaper gate ordering (syntax, ruff, tests before mutation) measured on
the round-3 journals' span durations -- write that item only after the
numbers are read.

### T6-12 — A failed attempt keeps its evidence: reasoning, finish reason, token usage (tightened; journal format)
Files: `src/saddle/vllm.py` (`propose_diff` ~:313: the truncation branch
raises `VllmResponseError` and drops the partial `reasoning`/`content`
and `usage` the response carried; the error must carry them out),
`src/saddle/slice.py` (`_seal_attempt` ~:301: the agent span for a
failed attempt today has `detail` only; `_best_of_samples` ~:358: a
sample that fails gates or does not apply loses its reasoning as well),
`src/saddle/journal.py` (`SpanRecord`: new optional `attempt_hash`;
new sidecar `attempts/<span_id>.json` beside the journal holding
`thinking`, `finish_reason`, `usage` (prompt/completion/reasoning
tokens), `failure`, `diff_hash`; the sidecar's sha256 is the span's
`attempt_hash`, so `verify_journal` can check it and a missing or edited
sidecar is a verify failure), `src/saddle/transcript.py` (`saddle verify`
renders "attempt 1 of 3: truncated at 81920 tokens, 61k reasoning, see
attempts/…" for failed attempts), `tests/test_vllm.py`,
`tests/test_slice.py`, `tests/test_journal.py`, `tests/test_transcript.py`.
Finding (round-3 T5 s1, executor): `thinking` is written only onto sealed
proof records. T1 sealed two proofs and both carry ~4 KB of worker
reasoning; T5 sealed nothing and its journal is 9.2 KB of git spans --
three attempts, two of them truncated at the 81920 cap, at least
160k output tokens, none retained. The one artefact that could say why a
node truncated is the one discarded; a successful node's reasoning is
kept although its diff and eleven gate results already stand for it.
`build_replan_task` consumes the failure history in memory and drops it.
Contract: every worker attempt, sealed or not, leaves a sidecar whose
hash is sealed in its agent span; a truncated call records
`finish_reason=length`, the cap it hit, the token usage the server
reported, and whatever reasoning and content arrived; a sample that
failed gates records its gate outcomes and diff hash; `saddle verify`
fails on a span whose sidecar is missing or does not hash. Sealed proof
records are unchanged (their `thinking` stays where it is; the record
hash does not move, so existing journals still verify).
Direction: tightened (more is sealed; nothing is accepted that was not).
Size: the sidecar can be hundreds of KB per attempt; it lives beside the
journal, not in it, so `saddle tail` and the chain stay small; a
`--no-attempt-sidecars` flag is NOT offered -- the evidence is the point.
Known-good: a two-attempt fixture (first fails ruff, second seals) leaves
two sidecars, the failed one carrying its reasoning and `failure`, both
hashed into their spans, `verify` OK. Known-bad: a fixture whose worker
raises the truncation error leaves a sidecar with `finish_reason=length`
and the partial reasoning, and the transcript line says so; editing one
byte of a sidecar makes `verify` fail naming the span.
Contract mutants (each target occurs once; abort if `grep -c` is not 1; drop `__pycache__` after each revert):
1. the truncation branch raises without attaching `reasoning`/`usage` -> the truncation known-bad red (sidecar lacks them).
2. `attempt_hash` not checked in `verify_journal` -> the edited-sidecar known-bad red.
3. `_seal_attempt` writes the sidecar only when `exit_code == 0` -> the failed-attempt known-good red.
Done when: mutants red; `./check.sh` green; T5 rerun after T6-8 shows
its attempts' sidecars in `runs/…/.saddle/attempts/`. Owner: main
session (journal format change; the vllm error type changes shape).

### T6-13 — The journal records what was asked, not only what happened: a `plan` record (tightened; journal format)
Files: `src/saddle/journal.py` (new record type `plan`: the validated DAG
as emitted -- node ids, kinds, `target_files`, requirement ids and
statements, `execution_constraints` including `reasoning_budget` and
`max_context_tokens`, `deterministic_gate` -- plus `task_hash` and the
`hash_node` of every node; sealed into the chain like a span),
`src/saddle/slice.py` (`run_slice` appends it before the first node is
dispatched, and again after every `splice_replan` with `replaces:
<failed id>`), `src/saddle/cli.py` (`saddle verify` and `saddle status`
render it: "plan: 1 node; node-1 impl xhigh, 4 target files, 30000 ctx"),
`tests/test_journal.py`, `tests/test_slice.py`, `tests/test_cli.py`.
Finding (round-3 T5 s1): the run timed out with an empty log (buffered
stdout killed by SIGTERM) and a journal of spans only; nothing recorded
which kind the node was, what effort the planner assigned it, or what
files it declared, so the reasoning-budget hypothesis for the truncation
could not be checked from the artefacts. The journal records outcomes
and not the plan that produced them.
Contract: every run's journal carries the plan it executed before any
worker call, and every replan's replacement; the record is in the hash
chain; `verify` fails a journal whose proofs cite a node the plan
records do not contain (a proof for a node nobody planned). T3-9's
`node_hash` on each proof must equal the `hash_node` the plan record
carries for that id, checked by `verify`.
Direction: tightened. Known-good: the two-node fixture's journal has one
plan record with two nodes, and a replan fixture has two plan records,
the second naming `replaces: n1`; `verify` OK. Known-bad: a proof whose
`node_hash` differs from the plan's -> `verify` names the node; a
journal with proofs and no plan record -> `verify` fails.
Contract mutants (each target occurs once; abort if `grep -c` is not 1; drop `__pycache__` after each revert):
1. `run_slice` appends the plan record after the first node instead of before -> the span-order test red.
2. `verify` skips the node_hash-vs-plan check -> the mismatched-hash known-bad red.
Done when: mutants red; `./check.sh` green. Owner: main session. Cheap;
lands with T6-12. Note: `max_context_tokens` is capped at 30000 by the
schema (`dag.py:98`) while the server holds 175k; that is a read
ceiling, not what truncated T5 (an output cap), and is left alone until
a run shows a worker starved of context.

### T6-14 — A truncated attempt is retried with more room, and the emission budget is not the reasoning effort's side effect (tightened in effect; #51 part 2 reopened)
Files: `src/saddle/cli.py` (`WORKER_OUTPUT_TOKENS` ~:77 with its comment
"thinking dominates output, so the budget tracks reasoning effort";
`propose` ~:505-541: `output_tokens = WORKER_OUTPUT_TOKENS[effort]` is
recomputed identically on every call, so the retry after a truncation
resends with the same cap, after an extra recovery-plan call and a
longer repair prompt), `src/saddle/vllm.py` (~:314 the message "retry
with more max_tokens" -- advice no caller acts on; a mechanism must be
able to do what it reports), `src/saddle/slice.py` (`_run_node` ~:418:
the retry loop must tell `propose` this is a post-truncation retry),
`tests/test_cli.py`, `tests/test_slice.py`, `tests/test_vllm.py`.
Finding (round-3 T5 s1, executor, every link read from source): the
worker's output cap is `WORKER_OUTPUT_TOKENS[effort]` where effort comes
from the planner's `reasoning_budget`; the server's lifetime histogram
shows 7 generations in the 20k-50k band and none above 50k, consistent
with `medium = 32768` as the ceiling that truncated node-1 twice; the
`--max-tokens 81920` flag governs emission and planning only; the
truncation error names a remedy nothing applies; the retry adds a
recovery-plan call and a longer prompt under the same cap. The #51
comment posted 2026-09-19 said b127e55 retries "at the next larger
output budget" -- b127e55 made truncation retryable and rewrote the
planner's sizing rule; it did not raise the budget. Correct the comment.
Contract: (1) a worker attempt that follows a `finish_reason=length`
failure runs with an output cap at least one step larger
(`WORKER_OUTPUT_TOKENS` order, capped at the server's `max_model_len`
minus the prompt), and the attempt span records the cap used; (2) the
emission budget is computed from the node's declared work -- the
baseline size of its `target_files` times the T6-8 factor plus a fixed
diff-overhead constant -- and the reasoning effort governs reasoning
only, via the request's `reasoning_effort` field; if the server counts
thinking against `max_tokens`, the cap sent is emission budget plus the
reasoning allowance for the effort (the T6-8 constant), so buying room
to write never buys room to think and vice versa; (3) the truncation
message says what will happen ("retrying with N tokens") or, on the last
attempt, that nothing will.
Direction: tightened in effect (a failure the harness manufactured is
removed; no gate changes). Evidence: T5 s1's three attempts; the
histogram; `propose` as read. Known-good: a fixture worker that
truncates once at cap C and succeeds at the next step seals with two
attempt spans whose caps differ; known-bad: with the old code the second
attempt's cap equals the first (assert on the recorded `max_tokens` in
the stub client's calls).
Contract mutants (each target occurs once; abort if `grep -c` is not 1; drop `__pycache__` after each revert):
1. the escalation step -> `+ 0` -> the two-caps-differ test red.
2. emission budget computed from `effort` again instead of the declared work -> the "same effort, larger files, larger cap" test red.
3. the message: keep "retry with more max_tokens" unconditionally -> the last-attempt message test red.
Done when: mutants red; `./check.sh` green; T5 rerun (one seed) after
T6-8+T6-14 shows attempt caps in the spans and no truncation, or a
truncation at the server's real ceiling -- which would then be the
model-ceiling column, honestly. Owner: main session. Also: post the
correction on #51 (the user's word first).

---

## 10. Deferred and not recommended

| Item | Why not now | Would need |
|---|---|---|
| D10 candidate clustering (#63) | SPECULATIVE for code (AlphaCode clustered by *behaviour* on generated inputs; saddle has no input generator); the >99% filtering figure is unverified | an execution-based clustering oracle and a measured pass@k gap |
| D12 parallel-k voting for code | PUBLISHED only on a fixed-strategy game (MAKER, Towers of Hanoi); k_min for qwen-3 was 15, not 3 | a code-domain replication |
| D14 structural tags | engine semantics unresolved (audit §9, refuted 1-2) | T4-4 |
| D15, D16–D18, D20–D25 | SPECULATIVE or UNCLASSIFIED in audit §8; HarnessFix (D25) numbers were misread (T0-9 fixes the text) | a measurement each |
| Rolling wave, stall detection, Phase -1/0, Phase 4 | specified, no measurement asks for them | a failing run that they would have caught |
| Remaining `refactor` exemptions (gates.py:250, :321) | need a behaviour-preservation oracle | design in #65 after T2-2 |
| HANG as a first-class verdict field | already surfaced in `detail` (#55 closed); a field is T2-4's job | nothing |
| Rich TUI (#17), chat-driven runs (#23) | see T5-7 and T5-8 | Tier 5 |
| Property soundness (#62) | see T3-3 | design decision |

---

## 11. Evidence label legend (as used above)

- **PROVEN-IN-PRODUCTION** — shipped and load-bearing somewhere with a public
  record (git's apply semantics, merge-queue full-suite runs, vLLM grammar
  API, Meta ACH's contract mutants).
- **PUBLISHED-WITH-MEASUREMENT** — an arXiv/venue paper with a number the
  deep-research workflow verified against the source (audit §9), with the
  scope qualifier the paper gives it.
- **VERIFIED** — this repo's own tests, run logs or issue records.
- **SPECULATIVE** — an argument without a number, or a number the workflow
  could not find (audit §9 "unsupported"/"unverified").
