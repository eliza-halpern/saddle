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
T0-3 (gate names test) ──► T2-2 (extends the same test module)
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
       assert names == ["syntax", "ruff", "tests", "coverage", "red-phase",
                        "node-scope", "property-coverage", "assertion-preservation",
                        "requirement-binding", "mutation"]
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
Files: `CLAUDE.md:78`, `:164`; `docs/DESIGN-NOTES.md:5`, `:605`;
`docs/BENCHMARK-RECORD.md:61`, `:89`.
Contract: every path in these files resolves from the repo root; every
statement about a run matches the run record.
Direction: docs-only
Evidence: VERIFIED — audit §2.8, §6. The v3 record
(`../saddle-bench/runs/PROGRESS.log:35`) says "exit-128 corrupt patches" and every
sealed attempt in `../saddle-bench/runs/t1-saddle/.saddle/proofs.jsonl` carries
`error: corrupt patch at line 12`; "No valid patches in input" appears in no run
file.
Steps:
- CLAUDE.md:78 `died on `git apply: No valid patches in input`` → `died on
  `git apply` exit 128 (`error: corrupt patch at line 12`, every attempt;
  ../saddle-bench/runs/PROGRESS.log:35)`.
- CLAUDE.md:164, DESIGN-NOTES:5, BENCHMARK-RECORD:61/:89:
  `saddle-bench/runs/FINDINGS.md` → `../saddle-bench/runs/FINDINGS.md` and add
  once, at CLAUDE.md:164, "(sibling checkout, not in this repo)".
- DESIGN-NOTES:605 D19 "specified, one global" → "wired per node via
  `reasoning_budget` → `BUDGET_TO_EFFORT` (`cli.py:402-404`); benefit unmeasured
  (T4-3)".
Done when: `grep -rn "No valid patches" CLAUDE.md` prints nothing;
`grep -rn "[^.]saddle-bench/runs/FINDINGS" CLAUDE.md docs/` prints nothing.

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
| CLAUDE.md:21-22 | `**49%** of its accepted tests added no line coverage at all` | `**49%** of its 571 mutant-killing tests (277) added no line coverage; engineer acceptance (73%) was measured on a separate 191-test sample` | arXiv 2501.12862 |
| DESIGN-NOTES:25-27 | `true-negative rate falling from **0.68 on weak-generator output to 0.17 on strong-generator output**` | `true-negative rate falling from **0.68 to 0.17** in one cell of its heatmap (Qwen2.5-72B verifying Mathematics answers from Llama-3.1-8B vs Qwen3-32B); the effect is directional across cells, the size is not` | arXiv 2509.17995 |
| DESIGN-NOTES:29-31 | `Pairing a weak generator with an independent verifier closed **75.7%** of the gap to a model three times its size.` | `A fixed GPT-4o verifier filtering K=64 samples closed **75.7%** of the gap in one 181-problem Mathematics difficulty bin ([0.7,0.8): 10.3% → 2.5% error); whole-domain gap closure is 30–50%.` | arXiv 2509.17995 v2 |
| DESIGN-NOTES:139/:712 | `**>30% accuracy drop**` | `wrong or degraded answers **2× to 30× more often** as the context fills (arXiv 2605.12366, Martin & Roger); the Chroma report is cited for the direction only` | audit §5 L139 row; arXiv 2605.12366. The >30% figure was not found in the Chroma report |
| DESIGN-NOTES:182-187 | `[Zylos][zylos] calls it … Their study across **22,374 program variants** found **>99% of failing agent-written tests passed on the original program while executing the changed region**` | `[Zylos][zylos] names it; the measurement is arXiv 2603.23443: across **22,374 mutated CodeNet variants** and 8 models, **>99%** of failing single-shot LLM tests passed on the original program while executing the changed region. Per-model pass rates on the same benchmark run 40.7% (Nemotron-3-Nano 30B) to 82.9%, so a ~27B model sits near **41%**. This measures single-shot test generation, not agent loops.` | arXiv 2603.23443; corroborated by 2605.06125 (TEBench, F1 45.7–49.4%) |
| DESIGN-NOTES:265-266/:695 | `**~83% accurate at generating properties** versus **62% at directly solving**` | `**82.4% vs 62.4%** on the Easy split (Medium 62.8/17.5, Hard 48.9/1.1; v1 Table IV); v2 (May 2026, retitled "Effective LLM Code Refinement via Property-Oriented and Structurally Minimal Feedback") reports 87.0 vs 63.0 overall with DeepSeek-R1-32B on 100 LiveCodeBench problems` | arXiv 2506.18315 v1/v2 |
| DESIGN-NOTES:281-282 | `**+23.1–37.3%** pass@1 … and **+4.2–17.4%** on LiveCodeBench` | `**+23.1–37.3% relative** pass@1 (v1); v2 reports "up to 13.4%". The LiveCodeBench range in v1 could not be located; drop it.` | arXiv 2506.18315 |
| DESIGN-NOTES:296 | `filtering removes **>99%** of the candidate pool` | `filtering removes **the large majority** of the candidate pool (the >99% figure was not verified against the paper)` | AlphaCode (unverified) |
| DESIGN-NOTES:318/:697 | `**k=2–3 votes** per step` | `**k=3 votes** per step for gpt-4.1-mini; k_min ranges 3–29 across models (qwen-3: 15; gpt-oss-20B: 6; llama-3.2-3B unusable), scaling as Θ(ln s) with p>0.5 exact-match agreement required` | arXiv 2511.09030 |
| DESIGN-NOTES:319 | `[Snell et al.][testtime] find sequential revision wins on easy problems,` | keep, append ` (not re-verified)` | — |
| DESIGN-NOTES:344-348 | (VP-Control paragraph) | keep the numbers (verbatim, verified: 62.9%, 22.9%, 40.9, 11.3); append: `Scope: a non-code data-operations benchmark (n=849) with Qwen3-4B / Phi-4-mini Q4_K_M verifiers; the authors disclaim cross-domain transfer and report a reversal on FinQA (17% vs 20%). Directional support only for code.` | arXiv 2609.10969 |
| DESIGN-NOTES:401-403 | `measures up to **10pp** loss on GSM-Symbolic; EMNLP 2024 found up to **27pp** on math` | `measures up to **8pp** loss from strict constrained decoding (DeepSeek-R1-Distill-Llama-8B; QwQ-32B 5pp) on GSM-Symbolic, which CRANE recovers; the TC⁰ result (Prop. 3.1) is for finite-output grammars — a diff grammar is infinite and falls under Prop. 3.3. The 27pp figure was not verified.` | arXiv 2502.09061 |
| DESIGN-NOTES:417-420 | `**100–500 tokens** is the cited range … ~35% slower … cut cost **62%**` | `No source for these three figures was found; treat granularity bounds as a design heuristic until T4 measures them.` | UNSUPPORTED |
| DESIGN-NOTES:549-551 | `**+11.1% average** … beating human-designed harnesses by 6.3%` | `**6.3–18.4 pp absolute** gains from repairing the harness alone (v2); 11.1 is the v2 Table III all-model average. The "beating human harnesses by 6.3%" reading is not in the paper.` | arXiv 2606.06324 v2 |

Also apply the remaining rows the audit §5 table marks for action (L121/L711,
L142-143/L695, L218/L620, L296, L319, L382-383, L403 SpecBench 28pp → "~27pp at
the P90 upper bound (R²=0.21)", L576) using the replacement text given there.
Matching note: the "Now" strings are quoted with the source's line wrapping
removed. Four of them span a line break in the file (CLAUDE.md:21-22,
DESIGN-NOTES:29-31, :265-266, :401-403), so grep for the first 25 characters
of the string with `grep -n -F`, or join lines first:
`tr '\n' ' ' < docs/DESIGN-NOTES.md | grep -o -F '<Now string>'`.
Done when: each "Now" string returns 0 hits after `tr '\n' ' '` joining, and
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
Files: `CLAUDE.md:65` area, `docs/benchmark-archive.md:~1033-1075`.
Contract: `uv run ruff format --check .` exits 0.
Direction: docs-only
Steps: `uv run ruff format CLAUDE.md docs/benchmark-archive.md`; inspect the
diff is whitespace/fence-only; commit.
Done when: `uv run ruff format --check .` exits 0.

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
Done when: `uv run mypy tests` reports no test_evidence errors.
Stop if: `grep -n "^SHELL_TIMEOUT\|^TOOL_UNAVAILABLE" src/saddle/gates.py` prints nothing.

### T1-4 — `./check.sh` exits 0
Files: none new.
Contract: ruff check, ruff format --check, mypy (strict) and the suite all pass at HEAD.
Steps: after T0-1, T0-11, T1-1..T1-3: `./check.sh`; paste the tail of its
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
Stop if: `grep -n "temperature=options.temperature," src/saddle/cli.py` returns anything other than two hits (L423 recovery `complete` and L437).
Note for T4-1: rerun the benchmark only after this lands; a k=3 arm at
temperature 0.0 measures nothing about k.

### T2-2 — A `refactor` node cannot create files (closes the cheapest half of #65)
Files: `src/saddle/evidence.py:244-254` (add `git_added_files` beside
`git_changed_files`); `src/saddle/gates.py:396-426` (`check_node_scope`),
`:470-485` (`Tier1Inputs`), `:565` (the `check_node_scope(...)` call in
`run_tier1`); `src/saddle/runner.py:191-205` (`Tier1Inputs(...)`);
`tests/test_gates.py:586-600` area; `tests/test_evidence.py`.
Contract: `check_node_scope(kind, changed_files, added_files)` fails a
`refactor` node that adds any file; a behaviour-preserving move may edit
existing sources and tests but the file set it creates is empty.
Direction: **tightened** (`refactor` currently returns pass unconditionally,
gates.py:410, and `kind` is chosen by the emitting model — #65: a node can
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
   Untracked files: `git diff` ignores them, so also append
   `git -C cwd ls-files --others --exclude-standard` output (the worker writes
   files without staging; `_apply_diff` uses `--index` at `slice.py:165` so
   applied diffs are staged, but the untracked case costs one line and closes
   a hole).
2. `Tier1Inputs`: add `added_files: Collection[str] = ()` as the **last**
   field (frozen dataclass; a default keeps the 13 existing constructors in
   `tests/test_gates.py:59` etc. valid).
3. `check_node_scope(kind, changed_files, added_files=())`: replace L410-411 with
   ```python
   if kind == "refactor":
       if added_files:
           return GateCheck(name="node-scope", passed=False,
                            detail=f"refactor node added file(s): {', '.join(sorted(added_files))}")
       return GateCheck(name="node-scope", passed=True, detail="refactor: edits both sides, adds nothing")
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
staged adds); an untracked `c.py` is listed too.
Contract mutants (`pytest tests/test_gates.py -q --no-cov -k scope`):
1. `sed -i 's/        if added_files:/        if False:/' src/saddle/gates.py` → red.
2. `sed -i 's/--diff-filter=A/--diff-filter=M/' src/saddle/evidence.py` → the test_evidence known-good red.
3. `sed -i 's/if kind == "refactor":/if kind == "test":/' src/saddle/gates.py` → red (refactor falls to the impl/test branch and the known-good fails).
Done when: mutants red; `./check.sh` green; `tests/test_slice.py` still green
with `_node_dict`'s default `"kind": "refactor"` (`tests/test_slice.py:61-64`) —
the slice fixtures edit existing files only; if one adds a file, that fixture
was relying on the exemption and must be given `kind="impl"` with an honest
note in the commit.
Stop if: gates.py:410 no longer reads `if kind == "refactor":`.

### T2-3 — Merge-time full-suite gate (the missing Tier 2, minimal form)
Files: `src/saddle/slice.py:636-656` (run-end block); `src/saddle/evidence.py`
(no change; use `run_shell`, L133); `tests/test_slice.py`.
Contract: after the schedule/replan loop exits, if at least one node was
proven, the **unscoped** suite command `merge_command` runs once in `workdir`; a
non-zero exit sets the run verdict to failed and seals a span
`kind="gate", name="merge-suite"` with the exit code; a zero exit seals the
same span with exit 0. Per-node verdicts are not revisited.
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
   `failed_unexcused = …` (L640):
   ```python
   merge_exit = 0
   if merge_command is not None and proofs:
       merge_start = perf_counter()
       merge_exit = run_shell(merge_command, workdir)
       append_span(journal_path, build_span(
           node_id="", argv=shlex.split(merge_command),
           duration_ms=_elapsed_ms(merge_start), exit_code=merge_exit,
           detail="merge-time full suite", kind="gate", name="merge-suite",
           span_id=uuid.uuid4().hex))
   ```
   and change L642 to `passed = not failed_unexcused and not undispatched and merge_exit == 0`.
   Extend the run span detail (L649-652) with `f", merge exit {merge_exit}"`.
   Import `run_shell` from `saddle.evidence` (L24) and `shlex`.
3. cli.py: no change; the default `"pytest -q"` applies. Do not add a CLI
   flag in this item.
Known-good (`tests/test_slice.py`, model on `test_run_slice_pass_end_to_end`
L298): one node, good diff, `merge_command="true"` → `result.passed is True`
and the journal holds a span named `merge-suite` with `exit_code == 0`.
Known-bad: same DAG, `merge_command="false"` → `result.passed is False`, the
node's own proof record is still present (per-node verdict untouched), and
the `merge-suite` span has `exit_code == 1`.
Contract mutants (`pytest tests/test_slice.py -q --no-cov -k merge`):
1. `sed -i 's/and merge_exit == 0/and True/' src/saddle/slice.py` → known-bad red.
2. `sed -i 's/if merge_command is not None and proofs:/if False:/' src/saddle/slice.py` → known-good span assertion red.
3. `sed -i 's/exit_code=merge_exit,/exit_code=0,/' src/saddle/slice.py` → known-bad span exit assertion red.
Done when: mutants red; every pre-existing `tests/test_slice.py` test green
(those that count spans get `merge_command=None` with a one-line comment);
`./check.sh` green.
Stop if: L642 does not read `passed = not failed_unexcused and not undispatched`.

### T2-4 — Gate outputs carry their evidence basis
Files: `src/saddle/journal.py:28-33` (`GateOutput`), `:395` (the one place a
`GateCheck` becomes a `GateOutput`), `:125` (proof payload), `:265`
(verification payload); `tests/test_journal.py`.
Contract: `GateOutput` has an optional `basis: str | None = None` field;
`mutation` outputs record `"sampled n=<mutants tried>"`, `coverage` records
`"changed-lines=<n>"`, all others `None`; old journals (no `basis` key) still
parse and their `record_hash` is unchanged.
Direction: **scope narrowed** (a reader can now tell a mutation verdict on
0 mutants — the T7 "218 lines, 0 mutants, passed" case in CLAUDE.md — from
one on 5). No verdict changes.
Evidence: VERIFIED — CLAUDE.md "Kill percentage scales with line count";
ACH (arXiv 2501.12862) reports mutant counts alongside kill rates, never a
bare percentage.
Issue: none (supports #62/#63 discussions).
Steps:
1. `GateOutput`: add `basis: str | None = None` (keeps `extra="forbid"`).
2. Where `GateCheck` → `GateOutput` is built, set `basis` for `mutation` and
   `coverage` from the check's `detail` (the counts are already in the
   detail string; parse with `re.fullmatch` on the exact format the check
   emits, and stop if the format has no count).
3. Hash: `build_proof` hashes `output.model_dump()` (journal.py:125) and
   verification hashes `model_dump(exclude={"record_hash"}, exclude_unset=True)`
   (journal.py:265). An old record's JSON has no `basis` key, so it is unset
   on parse and excluded from the verification payload: old hashes still
   verify. A new record dumps `basis` (even `None`) into the build payload
   and reads it back as set, so build and verify agree. Add the test that
   proves both: parse a HEAD-era journal line (copy one from
   `../saddle-bench/runs/t7-saddle/.saddle/proofs.jsonl` into the test as a
   string) and assert `_verified_records` accepts it; build a new record with
   `basis` and assert it re-verifies after a write/read round trip.
Known-good: a record with `basis="sampled n=5"` round-trips through
`read_records`. Known-bad: a record with an unknown extra key still fails
(`extra="forbid"` pin, exists already or add it).
Contract mutants: 1. drop the `basis=` assignment for mutation → the
known-good assertion on `basis` red. 2. make `basis: str = ""` (required,
non-optional) → old-journal parse test red.
Done when: `saddle verify` on `../saddle-bench/runs/t7-saddle/.saddle/proofs.jsonl`
(a HEAD-era journal) still passes; mutants red.
Stop if: journal.py:265 no longer uses `exclude_unset=True` — then old
records would hash differently and this item needs a design decision.

---

## 6. Tier 3 — architecture (one item each; no benchmark needed)

### T3-1 — Resume a journal instead of refusing it
Files: `src/saddle/slice.py:584-587`; `src/saddle/journal.py:336-342`
(`rebuild_proven`, currently test-only); `tests/test_slice.py:1062`
(`test_run_slice_refuses_stale_journal`).
Contract: `run_slice` on a journal that verifies seeds `proofs` from
`rebuild_proven(journal_path)` and schedules only unproven nodes; a journal
that does not verify still raises.
Direction: **loosened**, with proof: the refusal rejects a legitimate input
(a verified journal from a crashed run; ARCHITECTURE.md promises resume and
`rebuild_proven`'s own docstring says "a crash therefore loses at most the
in-flight node").
Evidence: VERIFIED — `rebuild_proven` exists and is tested in
`tests/test_journal.py`; the refusal is `slice.py:584-587`.
Steps: replace the refusal with `proofs = rebuild_proven(journal_path)` (let
its corruption error propagate); the `remaining` DAG is filtered by
`_schedulable_nodes` already. Rewrite `:1062` into two tests: a corrupt
journal still raises; a valid journal with n1 proven runs only n2 and the
final span says "2 proven".
Known-bad: a journal with a broken hash chain raises. Known-good: n1's proof
is reused, n1's `propose` is never called (assert via a proposer that fails
on n1).
Contract mutants: 1. `proofs = {}` instead of `rebuild_proven(...)` → n1
re-proposed, test red. 2. swallow the corruption error → corrupt-journal test red.
Done when: mutants red; `./check.sh` green.

### T3-2 — `target_files` on `Node` (issue #64, supersedes #51)
Files: `src/saddle/dag.py` (`Node`), `src/saddle/gates.py` (`check_node_scope`
or a new `check_target_files`), `runner.py`, `tests/test_dag.py`, `tests/test_gates.py`.
Contract: a node may declare `target_files: list[str]` (schema-constrained
to repo-relative paths, no `..`); if declared and non-empty, a diff that
touches any path outside the list fails a `target-scope` check.
Direction: **tightened**; opt-in.
Evidence: PUBLISHED-WITH-MEASUREMENT — Agentless (arXiv 2407.01489)
localisation-then-repair; #64 records T4's wrong-module edit.
Steps: follow the T2-2 shape (new field with default `[]`, new check, thread
through `Tier1Inputs`, known-good/known-bad, three mutants: empty list is
unrestricted; `..` rejected by schema; out-of-list path fails). Keep the
schema enum/const style so the constraint reaches the decoder
(`tests/test_dag.py:357` pattern). Do this after T2-2 so `added_files` is
already threaded.

### T3-3 — Property-coverage soundness (issue #62)
Not scheduled. `check_property_coverage` (gates.py:306; the `kind != "test"`
exemption at :321) verifies a property
test *exists* for a `test` node, which CLAUDE.md classifies as a presence
check. Making it sound needs an oracle (run the property test against a
mutant and require a kill), which is T2-4's data plus a design decision.
Record the decision in #62 before writing code.

### T3-4 — `RUN_ALLOWLIST` / `allowed_tools` is decorative
Files: `src/saddle/cli.py:41`, `:127`, `:352`, `:500`; `docs/ARCHITECTURE.md:153`.
`RUN_ALLOWLIST` constrains what the DAG may *name* in `allowed_tools`; nothing
consumes the list at execution (the worker emits a diff; it has no tools).
Two honest options, user's choice: (a) keep it as documentation of intent
and say so at ARCHITECTURE.md:153 (T0-7 covers the callout); (b) remove the
field from `Node` and the schema. Do not build tool masking to make the field
true; that is Phase 2 machinery that no measurement asks for.

### T3-5 — Issue hygiene (comments only; the user closes)
- #66: fixed by `21c179e` ("Fix mechanically what ruff can fix, before the
  worker is asked to") — propose closing with that SHA.
- #51: superseded by #64 — propose closing #51 with a pointer.
- #61: the grammar landed in `3669f9a`; the issue's remaining question is
  whether the CFG stops the exit-128 corrupt-patch failure. It cannot by
  construction (a CFG cannot enforce hunk-header line counts, audit §9), so
  #61 stays open pending T4-1's measurement and a decision between (i)
  accept-and-retry on `git apply` exit 128 (already the ladder's behaviour)
  and (ii) post-hoc `--recount`-style repair (already on every rung,
  `slice.py:165`). Comment with this framing.
- #23, #17: unchanged; both are future-facing.

### T3-6 — `slice.py` decomposition (optional, no contract change)
`run_slice` is ~90 lines with the replan loop, sealing and verdict inline.
Split into `_schedule_until_done`, `_merge_gate` (after T2-3), `_seal_run`.
Pure refactor: the existing `tests/test_slice.py` is the oracle; no new
tests; no mutants; commit labelled "refactor, no contract change".

---

## 7. Tier 4 — measurement (needs the container; each run is a record, not a code change)

Preconditions for every item: container healthy (`saddle doctor` green), key
in `SADDLE_VLLM_API_KEY`, no source edits during the run, results written
under `../saddle-bench/runs/<id>/` with the pre-registered prediction copied
in before the run starts.

### T4-1 — Rerun the v3 T1 arm against the HEAD grammar (F15)
Pre-registered prediction (DESIGN-NOTES §D2, L629): the grammar removes the
"no valid patches"/markdown-wrapper failure class; it does **not** remove
exit-128 `corrupt patch at line N` because hunk line counts are not
context-free. Expected: the failure mix shifts, not vanishes.
Steps:
1. `python tools/diff_grammar_check.py --emit > $SCRATCH/cases.json`, copy into
   the container as its docstring says, run `--run`; record `N/N cases correct`.
   Stop if any case fails: the grammar is wrong before the model is.
2. After T2-1: run T1 with `--sample-temperature 0.7`, three seeds.
3. Record per-attempt `git apply` exit codes and the first stderr line; count
   `corrupt patch` vs `does not apply` vs success.
4. Write F15 into `../saddle-bench/runs/FINDINGS.md`; update DESIGN-NOTES D2
   status; append to `docs/BENCHMARK-RECORD.md`.
Decision this unblocks: #61.

### T4-2 — Does anything overlap? (F7 follow-up)
A three-node DAG with no edges, each node a trivial independent edit, with
per-node worker spans timestamped. If `max_workers=5` yields serial spans
(end_i ≤ start_{i+1}), the scheduler's asyncio is decorative and
ARCHITECTURE.md:150/188/192 get a "measured serial" note; the fix (a
threadpool around `_run_node`, or an async client) is a Tier 3 item to be
written after this number exists.

### T4-3 — D19 effort sweep
`--worker-effort` low/medium/high/xhigh on the T1 task, same seeds as T4-1.
Report pass rate and median worker tokens. This decides whether
`BUDGET_TO_EFFORT` (cli.py:402-404) earns its complexity.

### T4-4 — `structural_tag` / `enable_in_reasoning` against vLLM 0.28 (precedes D14)
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
