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

Outstanding items only (rewritten 2026-09-20; everything done is
recorded in its own item's status paragraph, and the retired
dependencies with it). An arrow reads "left must land before right".

```
T6-17, T6-15, T6-18, T6-9 (DONE) ──► T6-19 (round 3c: one T5 seed with full evidence and a deadline sized for this model)
T6-1 (retrospective over the thirteen labelled runs, no GPU) ──► T6-6 (Proposal A goes ahead only if row A separates PASS from FAIL)
T6-1 ──► T6-4, T6-5 (both proceed regardless; T6-1 decides thresholds and whether T6-5 is worth a session of its own)
T6-2, T6-3 ──► nothing (known-bad is T2's log; independent of T6-1 and of each other)
T6-0 ──► nothing (docs-only pointer; any sitting)
T6-9 (deadline) ──► the next timed sweep (a run on a clock must seal what it has, or its evidence is what T6-12 saved and no more)
T6-10 ──► nothing (scope narrowing with proof; independent)
T6-7 ──► T4-6b (bench-side; baselines change only after the round's table is written, or T4-6b is not comparable to T4-6a)
T3-26 (`saddle run --dag FILE`) ──► T4-5 (half A runs a hand-written DAG)
T4-6a table (DONE) ──► T4-6b (same seven tasks, same frozen oracles)
T6-2, T6-3, T6-4, T6-5 ──► T4-1, T4-5, T4-2, T4-3, T4-6b (measure the harness after the specification hole is closed, not before)
T5-0 ──► T5-7 ──► T5-9 ──► T5-1, T5-3, T5-2 (rewritten against the chat surface) ──► T5-4, T5-5 ──► T5-6
T6-9, T5 seed ──► Tier 5 (the chat surface is built on a harness that can seal what it has)
T5-7, T5-8 ──► nothing (decisions, not work)
```

Order of sessions from here (T6-17, T6-15, T6-18, T6-9, T6-19 done):
T6-22 to T6-29c, T6-3, T6-31, T6-32, T6-33, T6-33a done, T6-30
withdrawn (T6-33a answered round 3e's first question: n2 does not clear
85% over the behavioural population, 82.0%, so 3e is not a rerun) →
round 3e, one T5 seed, with `--survivor-effort low` (does a survivor
round kill anything on a real gap; is T6-29a's 0/10 an effort effect?)
→ T6-4 with T6-5 done (main session, while 3e ran) → round 3e done
(F21.16: mutation 88.8% passes; UP031 kept the survivor round
unreachable; the sidecar scrub broke T6-27's check) → T6-36 done (the
scrub) → T6-37 done, option (a) (pinned isolated rule set, `ruff=` and
`ruff_rules=` sealed) → T6-38 done (`blank-lines` rung) → T6-41 done
(the `dead-code` gate; Goal G1) → T6-42 done (a repair may not delete
what the metric measured — round 3d's repair reasoning) → T6-44 done
(behaviour, not a ratio, in every string the worker reads) → T6-45 done
(the sealed server version is one saddle asked for) → T6-47 done (an
attempt records its reasoning effort; blocks every replay cell) → T6-48
done (the apply ladder forgives packaging; a loosening with proof, and
the precondition for T6-43 touching the grammar) → T6-34 done (attempt
refs, stale comment) → T6-49 done (round 3f; F21.20: P1-P5 held, P9 is a
disagreement, G1's count stays at zero, and both plans died on the same
gate) → T6-50 done (a call-shaped example binds to the test that
performs it; the blocker, with the rejected tree now passing) → T6-51
done (the sidecar keeps its reasoning whole) → T6-52 done (round 3g;
F21.21: Q8 a disagreement in the opposite direction — the gate rejected
an artifact ground truth accepts) →
**T6-54** (a recovery plan may not prescribe what a gate rejects; small,
known-bad in hand) → **T6-53** (coverage may not demand what the graded
node cannot supply; option (c) as the user chose, premise re-verified
against the task prompt's §7) → **T6-56** (round 3h died at node 1 on
`commit-tree` exit 128: saddle's snapshots borrowed an identity from the
host's git config and the host stopped supplying one) → round 3h, the next
G1 seed → **T6-57** (a failed node reports the gate verdicts it actually
produced; F21.26 -- two impl nodes' real verdicts were absent from their
run's own report, and the scoring picture was wrong until the sidecars
were read) → **T6-58** (a requirement may not restate the gate; T6-44
fixed saddle's own sentence and the planner still regenerates the proxy
into requirement statements the worker reads — shown on 3g's retained
prompt, post-T6-44) → T6-46 (still no admissible example; round 3g checked and does
not qualify) → T6-1 (with the reasoning read, over rounds
3-3f; row B tunes `REQ_NEAR_MISS_K`, row C reads T6-5's cost) → T6-39,
T6-40 →
T6-2 → T6-6 if T6-1 says
→ T6-10, T6-0 as filler → T3-26 → Tier 5 (T5-0, T5-7, T5-9, then the rest) → T4-1, T4-5,
T4-2, T4-3 → T6-7 → T4-6b. T6-11 is recorded as not an item.
T6-3 sits before round 3e rather than after T6-1 with T6-2: it is a
round-3e blocker in T6-31's class, where T6-2 is not. A node failed for
lint its own baseline carried burns an attempt for a non-reason, and the
sidecar cannot say which (T6-3's F21.12c; T6-31's F21.13d, ruff 2/2 in
round 3d). T6-2 cannot cost an attempt today --
`git_added_files` counts staged adds only, so generated artefacts never
reach `touched_files`, and `check_target_files` is one-directional, so a
declared `.pyc` is inert. It guards a node *passing*, not failing.
T6-1 row 6a is therefore a post-hoc confirmation of T6-3, scored against
runs 1-13 as before, and row 6b the same for T6-2: both keep their slot
after the retro on priority, not dependency (line 109 stands).
Contract changes stay in the main session; executors take docs,
fixtures and measurement.

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

Decided 2026-09-20: `saddle up` is the surface and `saddle run` is an
episode inside it. A task typed into the chat runs `run_slice` with its
own journal, plan record and proofs, exactly as `run` does today; the
chat is a front end to a proof-producing episode, never a way around
one. `run` stays as the tested core and the bench driver. The session,
not the run, is the long-lived thing, so context management is a Tier 5
anchor rather than polish: runs are referenced in the chat by a
compiled recap and recalled from their journals, and the session itself
compacts deterministically (T5-9). No model call is made to summarise.
Sequencing: T5-0 first (it is the evidence the rest cites), then T5-7
(the chat-driven run) and T5-9 (recap, recall, compaction) in that
order; then T5-1, T5-3, T5-2 rewritten against the chat surface
(each is one session), then T5-4 and T5-5 in either order, T5-6 last.
T5-8 is a decision, not work.

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

### T5-7 — Chat-driven runs: `saddle up` starts a gated episode (Major; decided)
Files: `src/saddle/chat.py` (`run_chat` ~:164, the tool loop), `src/saddle/cli.py`
(`run_task` ~:564: the part after `_emit_valid_dag` ~:571 and the
confirmation ~:589 becomes callable from the chat with a `RunOptions`;
`run_slice` call ~:656), `src/saddle/journal.py` (a `run` reference
record in the chat journal: task, journal path, run span id, verdict;
beside `SpanRecord` ~:102), `tests/test_chat.py`, `tests/test_cli.py`.
Decision 2026-09-20, the user's: `run` is to become unnecessary as a
command; every capability it has is reachable from the chat.
Contract: (1) a chat turn that asks for a task starts an episode with
its own journal under the workdir's `.saddle/runs/<id>/proofs.jsonl`;
the plan is shown and confirmed in the chat; the run's live events
stream into the chat as `render_event` lines; (2) the episode's
gates, plan record, sidecars and proofs are byte-for-byte what `run`
produces for the same task (one code path, not a second one); (3) when
the episode ends, the chat journal gets a `run` reference record naming
the journal and verdict, and the chat context gets the compiled recap
of T5-9, not the transcript. Direction: none for the gates; tightened
for the chat (a task can no longer be "done" in chat without a proof).
Known-good: a scripted chat that asks for the fixture task yields a
run journal that `saddle verify` passes and a chat journal with one
`run` reference; the recap in the next turn's context names the verdict.
Known-bad: a chat that edits files through tools without starting an
episode produces no proof and the recap says so.
Contract mutants (each target occurs once; abort if `grep -c` is not 1; drop `__pycache__` after each revert):
1. the episode calls a copy of the loop with one gate removed -> the
   byte-equality test against `run`'s journal red.
2. the `run` reference record not appended -> the chat-journal test red.
Done when: mutants red; `./check.sh` green; T5-0's transcript re-done
through the chat with no step that needs `run`. Owner: main session.
The load-bearing clause is (2): an implementation that ends up with two
loops has failed the item even if every test passes.

### T5-8 — Rich TUI (#17): not now
Every item above is plain stdout/stderr on purpose: it works over ssh,
in CI logs, and in `saddle run > file`. A TUI is a second renderer over
the same journal and can come after T5-0 is rerun on the finished tier
and still shows time spent reading. Keep #17 open, unchanged.

### T5-9 — The session compacts without a model: run recap, journal recall, deterministic summary (Major; tightened)
Files: `src/saddle/transcript.py` (new `render_run_recap(journal)`:
verdict, nodes proven and failed, files changed, gates that failed
with one line each, attempts, wall time; fixed section order; beside
`render_journal_transcript` ~:160), `src/saddle/chat.py` (a `recall`
tool and the compaction hook; `run_chat` ~:164), `src/saddle/journal.py`
(`read_entries` ~:569 over a run journal by span or proof id; sidecar
lookup by span id via `attempt_sidecar_path` ~:280), `src/saddle/cli.py`
(`saddle status` reuses `render_run_recap`, T5-5), `tests/test_transcript.py`,
`tests/test_chat.py`.
Why: the session is long-lived (T5-7) and accumulates runs. A model-
written summary drifts, costs a call, and cannot be verified; the
journal is already a content-addressed record of everything a recap
needs, so the recap is compiled from it and the same input always
gives the same bytes.
Contract: (1) `render_run_recap` is a pure function of the journal and
its sidecars; (2) a `recall` tool takes a span id, proof id, node id,
or a search term and returns the matching records or sidecar text,
capped at a fixed byte budget with a marker naming what was omitted and
how to page; it reads the journal files, never the chat context; (3)
when the chat context passes a threshold, the turns before the tail
are replaced by one block: session goal, decisions and preferences the
user stated (quoted, not paraphrased), files touched, every run's
recap, outstanding failures, then the last N turns verbatim; no model
call; a footer says what was compacted and that `recall` recovers
exact text. Direction: tightened (a summary is now derivable and
checkable; before, none existed).
Known-good: two runs (one PASS, one FAIL on a named gate) in a scripted
session compact to a block that names both verdicts and the failed
gate, and `recall <span id>` returns that attempt's sidecar thinking;
rendering twice gives identical bytes. Known-bad: a recap with a
tampered sidecar fails `verify` and the recap says the sidecar does not
hash; a `recall` over a term with no match returns an empty result and
the marker, not an error.
Contract mutants (each target occurs once; abort if `grep -c` is not 1; drop `__pycache__` after each revert):
1. the FAIL run's gate line dropped from the recap -> the two-run test red.
2. `recall` reads the chat context instead of the journal -> the
   tampered-sidecar test red.
3. the byte cap removed -> the paging test red.
Done when: mutants red; `./check.sh` green; T5-0's transcript, run
through the chat, compacts once and the next turn still names the
verdicts. Owner: main session. Not in this item: model-driven
observation and reflection workers (§10).

---

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

**Added 2026-09-20 (user's question):** the retrospective reads the
model's reasoning. Every failed attempt in rounds 3c and 3d has a
sidecar with the full reasoning text (T6-12) and, from round 3e, the
emitted diff (T6-27); `saddle explain JOURNAL --attempt PREFIX` prints
one. Nobody has read them end to end. Required section: for each failed
attempt, what the model said it was doing when it failed, and the
failure mode named -- did n2 know `money.py:33` was uncovered and could
not act, or never notice? This is the cheapest evidence the plan has
not yet used.
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
as today (#66). The detail names the introduced findings' rules
(`E501 x2, F401`), not only the exit: round 3c's n2.r2 sidecar reads
`ruff check exited 1, format exited 0` and a reader cannot tell lint
from formatting without re-running (F21.12c).
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

**Status (2026-09-20):** DONE, `b192535`, main session, with T6-31.
`RuffFinding` and `introduced_findings` in gates (pure), `ruff_findings`
in evidence (the JSON run; stdout rendered as `path:row:col: CODE
message` for briefs), two legs in the runner (baseline snapshot before
red-phase writes into it). The detail names up to five introduced
findings and the inherited count. Mutants three (with T6-31's), all
killed; `./check.sh` green.
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

**Status (2026-09-20):** DONE, `(commit below)`, main session, with T6-5, landed
in a worktree while round 3e ran against the checkout's `src/`. Landed as
written except: `REQ_NEAR_MISS_K` and `edit_distance` live in `dag.py`,
not `gates.py` (dag sits below gates in the layering, and the validator
that enforces the bar is the model's own); the examples check is a
clause of `check_requirement_binding` (`examples=`, `suite=`), not a
twelfth check, so the transcript's gate list is unchanged; it binds a
`test` node only (an impl node cannot edit tests, a refactor preserves
them) and reads the suite as the node leaves it (baseline plus the
node's diff), so a survivor round's test node inherits the sealed
specification's assertions rather than restating them; presence means
the literal sits in an `assert`, under a `with` (a `raises` block) or in
a decorator (a `parametrize` table) of a `test*` function, a string by
value and any other constant by its spelling; "the suite is green" is
not this clause's to check (a test node's suite is red by design) and
stays the impl node's tests gate. The planner and worker prompts carry
the rule; the worker prompt lists every example under its requirement.
Wire schema: both arrays are `minItems: 1` (pydantic `min_length`), no
pattern. NOT yet verified against the decoder: xgrammar documents
`minItems`, but the check CLAUDE.md asks for runs in the serving
container, which the main session may not exec into -- round 3f's first
plan is that check (a plan with `accepts`/`rejects` decoded at all, and
`saddle explain` on any 'plan invalid' for a distance failure). Known
cost: every fixture requirement in the tests now carries
`accepts=["2"], rejects=["3"]`, and every `test`-node fixture asserts on
both. ARCHITECTURE.md's example node carries examples too.

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

**Status (2026-09-20):** DONE, `(commit below)`, main session, bundled with T6-4.
Deviation: the polarity classifier lives in `gates.py` beside
`_has_property`, not in `evidence.py` -- gates may not import evidence at
runtime (CLAUDE.md layering) and the check is a pure predicate over
sources. Negative forms: `assert not ...`, `... is False`, `... == False`,
a `with pytest.raises(...)` / `raises(...)` block, inside a `@given`
function; `!=`, `is True`, `== 1` and an `assert not` in a plain example
do not count. One observation from landing it: ruff's SIM201 rewrites
`assert not f() == 3` to `f() != 3`, so a worker whose rejection is a
comparison must spell it `is False` or `not valid(x)` to satisfy both
gates; the worker prompt names those forms. Fixtures whose only property
was positive (SPEC_DIFF, SPEC_TEST, SPEC_SOURCE, `_flag_test`) gained a
rejecting property over the `3` reject. Flip, with its evidence: the
former `test_hypothesis_given_counts_as_a_property` asserted that T1's
own shape (one positive property over a regex of valid addresses)
passes; F1 is the run where that shape let a validator accepting
`.u@example.com` through 7/7 gates, so the old expectation pinned the
defect; the test now holds that shape as the known-bad and a rejecting
property as the known-good. The survivor round (T6-29c) is bound by this
floor too: a kept candidate must reject an input or the spliced test
node fails property-coverage; round 3f measures the cost.

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
Status 2026-09-20: DONE by the main session, with T6-14. Landed in
dag.py: `TOKENS_PER_LINE = 16`, `DIFF_OVERHEAD_TOKENS = 2048`,
`emission_estimate(node, file_lines)`, and two validators run by
`validate_dag` only when the caller passes `file_lines` (a repo is in
view): `undeclared-scope` for an `impl`/`refactor` node with empty
`target_files`, and `node-too-large` when the estimate exceeds the
caller's `emission_budget(node)` -- cli.py defines it as the top of the
ladder minus the node's reasoning allowance. `_emit_valid_dag` passes
both, so the planner sees the issue text and re-plans like any other
validation error; the planner prompt says `target_files` is required for
impl and refactor and why; ARCHITECTURE gate 7 no longer says opt-in.
Deviation from the item, stated: the requirement is enforced in
`validate_dag` on the emit path, not in the `Node` model, so fixtures
that build nodes directly keep working and the planner still cannot
leave scope empty. Correction to the item's known-bad: round-3 T5's
plan (four modules, 249 lines, ~6k tokens) is NOT oversized by this
measure and validates -- its failure was the cap (T6-14); the known-bad
fixture declares an 8000-line file. Mutants, all KILLED: M4 `estimate >
budget` -> `> 10**9` (the oversized node runs); M5 the undeclared clause
-> `if False`; M6 `_emit_valid_dag` passes `file_lines=None` (the emit
path skips both checks). `./check.sh`: 672 passed, 3 skipped, 100%.
Closes #64's remaining half and supersedes T3-2's opt-in decision; the
user closes the issue.
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
Status 2026-09-20: DONE by the main session (commit below). Landed:
`run_slice(..., deadline_s=None, clock=perf_counter)`; a `_Deadline`
(end time, clock, node walls so far) threaded into `_schedule_until_done`
and `_run_node`. A node is not started when the time left is under the
median node wall so far, or gone (`_DeadlineSkipError`: undispatched,
not failed; no replan past the deadline); no attempt is started past the
deadline (the give-up path, `_abandon`, restores the tree as on any
exhaustion; the node counts as failed after the attempts it made); the
attempt in flight finishes and may seal. `_seal_run` writes exit code 3
(`DEADLINE_EXIT`) and `deadline: N proven, M failed, K undispatched` in
the run span; `SliceResult.deadline_hit`; `saddle run --deadline
SECONDS` exits 3. The merge suite still runs over what was proven.
Bench: `run_arm.sh` passes `--deadline "${DEADLINE:-1700}"` and its
`timeout` is now a backstop 600 s past it (uncommitted in saddle-bench).
Deviations from the item text: the in-flight attempt is not interrupted
at the deadline (the item's known-bad, asserted: a 3 s deadline inside a
5 s call still seals); the deadline give-up reason is not sealed as its
own span -- the run span carries `deadline:` and the node's last attempt
span carries its gate verdict. Mutants: M1 dispatch guard → `if False`
KILLED; M2 between-attempt guard → `if False` KILLED; M3 exit code 3 →
never KILLED. check.sh 696 passed, 100%. Sizing note for the T5 seed:
with T6-17 a worker call can run 15-20 min, so `DEADLINE` for that seed
should be set from the run's own node count, not 1700 by habit.
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
Status 2026-09-20: DONE by the main session, with T6-13, in the commit
carrying this line. Landed: `SpanRecord.attempt_hash` (written only when
set, so pre-T6-12 spans hash as before); `write_attempt_sidecar` /
`attempt_sidecar_path` in journal.py (`attempts/<span_id>.json` beside
the journal, scrubbed like every journaled text); `_seal_attempt` writes
one for every attempt -- truncation (partial reasoning, content length,
usage, the cap, `finish_reason=length`), identical re-proposal, apply
failure, gate failure (with every gate's outcome), and the sealing
attempt -- plus one summary per sample from `_best_of_samples` (diff
hash, reasoning, outcome). `VllmResponseError` carries `reasoning`,
`content`, `usage`, `max_tokens`, `finish_reason` on a truncation;
`DiffProposal` carries `usage` and `max_tokens` (excluded from
equality). `verify_journal` reports `attempt-sidecar` (missing / does
not hash). Deviation, stated: the cap and usage live in the sidecar, not
as span fields, so span hashes and `saddle tail` are unchanged. Known-bad
shown by mutant (the old code is the mutant): M1 truncation drops the
partial reasoning -> `'' != 'thought so far'`; M2 verify ignores
`attempt_hash` -> the edited-sidecar test finds no issue; M3 sidecar
only on success -> the failed attempt's sidecar is missing. All KILLED.
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
Also carries: the `max_tokens` each attempt sent (T6-14 clause 1, not
implemented there; round 3b could verify its prediction 2 only by
arithmetic because of it -- F21.9 follow-on 17).

### T6-13 — The journal records what was asked, not only what happened: a `plan` record (tightened; journal format)
Status 2026-09-20: DONE by the main session, with T6-12. Landed:
`PlanRecord`/`PlanNode`, `build_plan`, `append_plan`, `read_plans`;
`run_slice` seals the plan right after `_seed_proofs` and before any
worker call, and `_schedule_until_done` seals each replan's replacement
nodes with `replaces=<failed id>`; `verify_journal` reports
`unplanned-proof` for a proof sealed *after* a plan record whose
`node_hash` no plan names (proofs before any plan predate T6-13 and are
not judged -- deviation from the item, so old journals and hand-seeded
resume fixtures still verify); `render_event` renders a plan as a header
and one line per node, so `saddle tail` shows it; `saddle verify` prints
`N plan(s)` and the plan lines before the transcript. Also landed here,
from T5-1/F21.3: `saddle run` flushes stdout after the plan line, so a
killed run keeps at least that. Known-bad by mutant: M1 plan not sealed
-> the journal's first entry is not a plan; M2 verify skips the check ->
the stray proof passes; M3 the replan plan forgets `replaces`. All
KILLED. ARCHITECTURE's journal paragraph names both record kinds. Not
done here: T6-9's deadline (the external kill still ends the run; the
evidence now survives it).
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
Superseded in part 2026-09-20 by T6-17: the per-effort allowance and the truncation ladder are gone; the baseline-tree sizing and the pre-flight stand.
Status 2026-09-20: DONE by the main session, with T6-8, in the commit
that carries this line. Landed: `worker_output_cap(node, effort,
file_lines, escalations)` in cli.py -- the effort's entry in
`WORKER_OUTPUT_TOKENS` is now the *reasoning allowance*; a scoped node's
cap is `emission_estimate` (dag.py, 16 tokens per baseline line of its
`target_files` + 2048) plus that allowance; an unscoped node keeps the
allowance alone. `propose` counts one escalation per attempt whose
`failure` carries `finish_reason=length` (persisting across a later
apply failure) and climbs `WORKER_OUTPUT_STEPS = (8192, 16384, 32768,
65536, 131072)` one step per escalation, capped at the top. The vllm
truncation message names the cap it hit and no longer advises a retry.
Known-bad shown red: old vllm.py against the new message test; the old
same-cap behaviour is reproduced by mutants M1 and M2 below. Mutants,
all KILLED: M1 `range(escalations)` -> `range(0)` (attempt 2 at 18464
not 32768); M2 `+ (estimate or 0)` -> `+ 0` (scoped cap equals the
allowance); M3 message drops the cap (recovery prompt lacks `truncated
at N`). Fixture correction: `tests/test_cli.py::_node_dict` now declares
`target_files: ["n.py"]`, so every cap pin became
`_expected_cap(repo, effort)`; four plan-render pins gained the `targets:
n.py` line.
Measurement (session 34, round 3b, F21.9; saddle b91e996, bench a20a4fc
+ 37f494f): one T5 seed. The ladder worked as coded and a feedback loop
defeated it. Attempt 1 did not truncate: it applied a 699-line diff that
was 19 byte-identical copies of one test and failed `syntax`. Attempt 2
was then sized from the *live* tree -- the failed diff still applied --
so its cap rose to 32624 (arithmetic matches to the token), 144 tokens
under the 32768 rung; the "escalation" for attempt 3 bought 0.4% and it
truncated at once. Prediction lines 1 and 3 held, 2 held by arithmetic
only (the cap is journaled nowhere), 4 falsified (the tree was clean at
exit). Label: HARNESS; no model-ceiling row, the ladder never came near
131072. Two corrections to this item, landed by the main session in the
commit carrying this paragraph (tightened in effect): the estimate is
taken from the node's baseline tree, cached on the node's first
proposal, never from the tree a failed attempt left behind (F21.9a);
and an escalation is `max(next rung, 2 x cap)`, so a step is measured
from the cap, not from where the rung table happens to fall (F21.9b).
Known-bad shown by mutant: M7 size from the live tree again -> attempt 2
reads 23264 in the 300-line-bloat fixture (`[18464, ..., 23264, 18464]
!= all 18464`); M8 escalation lands on the nearest rung -> `32768 >=
2*32624` red. Both KILLED; check.sh 673 passed, 100%. Clause (1)'s "the
attempt span records the cap used" is NOT implemented -- the executor is
right -- and is folded into T6-12's sidecar, which is where the cap
belongs with the finish reason and usage. The degeneration itself
(F21.9c) is T6-15 and T6-16; a second T5 seed (session 35) follows them,
not this fix alone, because F21.9c says the next failure would be the
same loop hitting a higher wall.
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

### T6-15 — Retries are not greedy (tightened in effect; measured first, F21.10)
Status 2026-09-20 (later): code DONE by the main session (commit below),
with T6-18. `worker_temperature(options, failure)` in cli.py: first
attempts at `--sample-temperature`; retries at `--recovery-temperature`
when given, else `--sample-temperature`; `--temperature` still governs
planning and the recovery-plan prose. flip:
`test_run_task_sample_temperature_routes_first_attempt_vs_recovery` --
it pinned T2-1's greedy retry (0.3 = `--temperature`); F21.10 arm (c) is
the evidence the old expectation pinned a defect (three seeds at 0.0,
one sample, 3/3 truncated); the retry now asserts the sample temperature
and the recovery-plan call is asserted at `--temperature`, which the old
test left unpinned. Mutants: M1 retry → `options.temperature` KILLED; M2
`--recovery-temperature` ignored KILLED. No sampling parameter other
than temperature is sent (penalty deferred, §10).
Status 2026-09-20: measurement DONE by session 35 (bench 17636b7
pre-registration, 496e3f6 result; F21.10; twelve response bodies under
`runs/round3b/degeneration/`). Contract (1), an unconditional
`repetition_penalty = 1.05`, is WITHDRAWN on that evidence: the arm that
carried it failed 3/3 (nine repeated `fees.py` sections; a reasoning-only
response with `content: null`; a 350-cycle `+`/`+`/`\ No newline` motif
that applies cleanly), while today's shape produced a complete diff 3/3.
The item is narrowed to contract (2) below. The measurement's other
result -- temperature-0.0 retries truncated 3/3 with 17571 of 20256
tokens spent on reasoning, byte-identical across seeds 1-3 -- is the
evidence for (2) and for T6-17. Code: main session, after T6-17.
Files: `src/saddle/vllm.py` (`_build_diff_payload` ~:238: the request sends
`temperature` only -- no `repetition_penalty`, `top_p`, `top_k`, `min_p`
anywhere in `src/`), `src/saddle/cli.py` (`propose`: `temperature=
options.sample_temperature if failure is None else options.temperature`,
and `--temperature` defaults to 0.0, so every retry is greedy), `tests/test_vllm.py`,
`tests/test_cli.py`; `../saddle-bench/runs/round3b/t5-s1/` (the evidence).
Finding (F21.9c): round-3b T5 attempt 1 emitted 19 byte-identical copies
of one test function inside a well-formed diff; attempts 2 and 3 ran at
temperature 0.0 and truncated. Greedy decoding over a long constrained
generation with no repetition control is the textbook degeneration
setup, and the grammar constrains form, not content.
Contract (narrowed 2026-09-20): a recovery attempt does not drop to
temperature 0.0 by default. `--temperature` keeps its meaning for
planning and recovery-plan text; worker diff retries use
`--sample-temperature` unless `--recovery-temperature` is given, so an
attempt that follows a failed one is not the same greedy walk, and k
recovery samples are k samples. Direction: tightened in effect (a
failure the harness invited is discouraged; no gate changes). The T2-1
argument for greedy recovery (reproducible repair) is superseded by
F21.10 arm (c): three seeds, one sample, three truncations; say so in
the commit. No sampling parameter other than temperature is added; the
penalty is deferred (§10) until a node that repeats *without* one is on
record (F21.11 follow-on 22, 23).
Measurement: done, see the status paragraph and F21.10.
Known-good: the cli test asserts a retry's worker call carries the
sample temperature by default and `--recovery-temperature` when given;
the recovery-plan `complete` call still carries `--temperature`.
Known-bad: a retry at `options.temperature` (the old shape) fails it.
Contract mutants (each target occurs once; abort if `grep -c` is not 1; drop `__pycache__` after each revert):
1. retry temperature -> `options.temperature` again -> the cli retry test red.
2. `--recovery-temperature` ignored (always sample temperature) -> the flag test red.
Done when: mutants red; `./check.sh` green; T2-1's greedy-recovery
sentence in ARCHITECTURE/cli docstrings updated to cite F21.10.

### T6-16 — Does `DIFF_GRAMMAR` aggravate repetition? (measurement; no code)
Status 2026-09-20: DONE by session 35 (bench 00ac5ee; F21.11). Result:
unanswered. Neither arm repeated anything (0 repeated bodies in all six
responses), so the registered discriminator ("(d) has fewer than half of
(a)'s repeated bodies") could not fire; recorded as a test that could
not run, not a result. **Re-asked 2026-09-21** on the node T6-16 said it needed
-- round 3e's n2 attempt 1, which repeats with no penalty set -- by
replaying that attempt's retained prompt at its own three seeds and
temperature, once with `DIFF_GRAMMAR` and once without, the two payloads
differing in exactly one key. **Read the verdict as F21.18 in
`../saddle-bench/runs/FINDINGS.md` before round 3f** -- F21.17 is the
first pass and was not a control (it replayed a `low` attempt at
`xhigh`); read it only for what F21.18 corrects. Reproducer and raw
per-draw records under `../saddle-bench/runs/round3e-probe/`. A
grammar-implicated degeneration changes what T6-43 should fix (the
grammar, not only the sampler) and what T6-38 and T6-39 should
measure.

**First pass, superseded. Raw result read by the main session
2026-09-21, before F21.17 was written.** The discriminator could not
fire, for the second time and a new reason: **neither arm degenerated**.
All six draws are clean, with
most-repeated-line counts of 3 to 6 and 2 to 5 distinct blocks, against
an original that repeated a 21-line block 60 times.

| arm | reasoning tokens | completion | content chars | top line |
| --- | --- | --- | --- | --- |
| original s1 | 14 100 | 22 491 | 23 201 (diff) | x60 |
| grammar-on s0/s1/s2 | 38 030 / 75 854 / 45 467 | 40 814 / 79 036 / 48 273 | 9 899 / 11 320 / 9 996 | x5 / x4 / x4 |
| grammar-off s0/s1/s2 | 78 864 / 58 400 / 68 641 | 82 095 / 61 180 / 71 602 | 11 485 / 9 883 / 10 389 | x6 / x3 / x3 |

So this cell says nothing about the grammar. It is not a null result for
`DIFF_GRAMMAR`; it is a replay that did not reproduce its own baseline.
The likely reason is on the record: the replay billed **10 627 prompt
tokens against the original's 10 615**, so the retained prompt is not
byte-identical to what was sent, and at temperature 0.7 over 40 000+
reasoning tokens a 12-token prefix difference diverges completely.
Reasoning ran three to five times the original in **both** arms, which
tracks a different request rather than a manipulated variable.

**Cause identified, and it is not the grammar.** The sidecar does not
record `reasoning_effort`, so the probe had to guess it and chose
`xhigh`. Resending the retained prompt at each effort against the live
server bills 10 615 at `low`, 10 585 at `medium` and 10 627 at `xhigh`,
so the original attempt ran at **`low`** and the replay ran at `xhigh`.
The retained prompt is byte-exact; the effort was the uncontrolled
variable, and it accounts for both the twelve tokens and the three-to-
fivefold reasoning. That is T6-47.

Two consequences. First, no cell built on replay is interpretable until
T6-47 lands. Second, for T6-43: a repetition penalty
cannot be measured against a phenomenon that appeared in 2 of 3 draws
once and 0 of 6 on a near-replay. The base rate has to be established
over many draws first, or the penalty's effect is unfalsifiable. This
does not weaken Goal G1's three-seed floor -- G1 asks for agreement
between gates and oracle per run, not for a reproducible draw -- but it
does say a degenerate emission is a probabilistic event, which is the
spread the floor exists to cover. Side findings: 2 of 3 unconstrained outputs were
markdown-fenced and unappliable (the grammar earns its keep on form);
the grammar permits unbounded repetition at `section+` and `hline+`, and
the only degeneration seen used both, but always under the penalty, so
cause is not established. Re-asking needs a node that repeats without a
penalty (§10).

**Status 2026-09-21: ANSWERED, as F21.18.** The re-ask was run twice
more, held at `low` -- the effort the run actually used, confirmed per
draw by `prompt_tokens == 10615` -- with the grammar as the only
manipulated variable: a seed cell (seeds 0/1/2, three concurrent per
arm) and a replicate cell (one payload three times, sequential per arm).
Eighteen draws.

Both halves of the side finding above are now settled, and both are
worse than it said. On form: not 2 of 3 but **0 of 9** unconstrained
draws would have landed a change in saddle -- 8 of 8 non-empty
grammar-on draws end with the newline `git apply` requires and 0 of 9
grammar-off draws do (p = 4.1e-05), three of the nine are ```diff-fenced
and all nine open with two blank lines. The grammar earns its keep on
form decisively: 7/9 against 0/9 on landing at all (p = 0.0023). Six of
those nine failures are packaging rather than content, which is T6-48.

On repetition: the registered discriminator fires this time and in the
opposite direction to "no difference". At `low`, **4 of 6 grammar-on
draws degenerate, 1 emits nothing, 1 is clean; 0 of 6 grammar-off draws
degenerate** (p = 0.061; 0.015 counting the empty emission), with a
content spread of 10 235-45 332 chars against 9 381-12 057. Cause is
still not established and F21.18 does not claim it: `root ::= section+`
accepts after any complete `hline`, so the grammar does not prevent
stopping. What is established is that the arm with the unbounded
`section+` / `hline+` is the only one that overruns. T6-43 owns the
experiment; do not let it buy a sampler penalty on this.

Two constraints this places on every later cell. The server is not
seed-deterministic -- six identical requests returned six distinct
outputs -- so no replay is a control for a recorded draw, and Goal G1's
three consecutive seeds means three runs. And `blocks_of`, the statistic
F21.17 scored its whole table with, cannot see the increment costume at
all: the worst draw here scores 5, because each of its 148 filler stubs
has a different name. Score with `check_dead_additions` (T6-41), not
with a max-repeat count.
Files: `../saddle-bench/runs/round3b/degeneration/` (T6-15's
`prompt.txt`, `settings.json`, arm (a) responses), `tools/
structured_output_probe.py` (the request shape), `src/saddle/vllm.py`
(`_build_text_payload`, the unconstrained shape), `../saddle-bench/runs/
FINDINGS.md` (F21.11). Question (F21.9c, point 3): constrained decoding
narrows the legal token set; does the same prompt repeat less without
the grammar? Corrected 2026-09-20: there is no recorded attempt-1 prompt;
use the one T6-15's measurement regenerated, and arm (a) as the
with-grammar arm (do not re-run it). Arm (d): the same prompt, cap and
temperature 0.7, seeds 1-3, as the exact `_build_text_payload` dict (no
`structured_outputs`), saved as `d-s<seed>.json`. Count repeated bodies
and `completion_tokens` as in T6-15, and record whether each (d) output
is a diff that `git apply --check` accepts against the scratch copy.
Pre-register in the same `PREDICTION.md` before the first (d) request:
repetition is present in both arms (the loop is the model's); (d) is not
markedly better (fewer than half of (a)'s repeated bodies in every seed
would be "markedly"). If (d) is markedly better, D2/D14 get a note and
T6-15's penalty matters more. No conclusion is written without the
twelve outputs on disk. Owner: executor.

---

### T6-17 — Reasoning is not budgeted: the worker's `max_tokens` is the context that is left (scope narrowed; supersedes T6-14's ladder)
Status 2026-09-20: DONE by the main session (commit below). Landed:
`worker_max_tokens(prompt, context_window)` = window − len(prompt)//3 −
2048 (three chars per token over-counts code, so the request never
exceeds the window; F21.10's prompt was 3.76); `diff_budget(node,
window)` = window − max_context_tokens − 2048 replaces `emission_budget`
in the T6-8 pre-flight; `server_context_window(client, override)` reads
`VllmClient.max_model_len()` (new; `max_model_len` of the served model
on `GET /models`), else `--context-window` (new flag on `run` and
`dag`), else 175000. Removed: `WORKER_OUTPUT_TOKENS`,
`WORKER_OUTPUT_STEPS`, `MAX_WORKER_OUTPUT`, `worker_output_cap`,
`emission_budget`, the `escalations` and `baseline_lines` state in
`propose`. Every worker call, the recovery-plan `complete` included, is
sized from its own prompt; a longer repair brief or a bloated tree buys
less room, never more (F21.9a's fix is now structural). Deviations from
the item text: the prompt count uses chars//3, not the server's previous
`prompt_tokens` (the retry prompt differs from the call it would be
taken from); `dag.py` is unchanged (the budget was always the caller's).
Direction, honestly: scope narrowed for the cap, and `node-too-large`'s
threshold LOOSENED from 131072 − allowance (114688 for `low`) to window −
read ceiling − margin (164952 for a node reading 8000), because the
allowance it subtracted was not a cap (F21.10) -- the known-bad fixture
grew from 8000 to 11000 lines to stay over it. Mutants: M1 `max_tokens`
→ constant 16384 KILLED; M2 `node-too-large` → `if False` KILLED; M3
`max_model_len` ignored → fallback KILLED. T6-14's "one step up the
ladder after a truncation" and "reasoning allowance per effort" are
superseded; its baseline-tree sizing (F21.9a) and T6-8's pre-flight
stand.
Files: `src/saddle/cli.py` (`WORKER_OUTPUT_TOKENS`, `WORKER_OUTPUT_STEPS`,
`MAX_WORKER_OUTPUT`, `worker_output_cap`, `emission_budget`, `propose`'s
`escalations`), `src/saddle/dag.py` (`validate_dag(..., emission_budget=)`,
`node-too-large`), `src/saddle/vllm.py` (`check_server`/`doctor`: read
`max_model_len` from `GET /models`), `docs/ARCHITECTURE.md` (gate 7 /
budget paragraph), `tests/test_cli.py`, `tests/test_dag.py`, `tests/test_vllm.py`.
Premise (the user's, 2026-09-20, and F21.10): this model's strategy is
long test-time compute; it reasoned 17571 tokens at effort `low` against
a 16384 "allowance", and vLLM enforces no split between reasoning and
content. T6-14's model -- a reasoning allowance per effort plus an
emission estimate, escalated on truncation -- caps the thing that makes
the model work and then pays for it with a cut-off diff. Contract: (1)
every worker call sends `max_tokens = context_window - prompt_tokens -
margin`, where `context_window` comes from the server's `max_model_len`
(fallback flag `--context-window`, default 175000, the container's
`MAX_LEN`) and `prompt_tokens` from the server's own count of the
previous call when known, else `len(prompt) // CHARS_PER_TOKEN`;
`reasoning_effort` is still sent, as advice. (2) The ladder,
`escalations`, and `WORKER_OUTPUT_TOKENS` go; `finish_reason=length` is
then a genuine ceiling and stays a retryable attempt failure with its
evidence in the sidecar (T6-12). (3) T6-8's pre-flight keeps
`node-too-large`, computed against `context_window - node.max_context_tokens
- margin` instead of `emission_budget(node)`. Direction: scope narrowed
(budgets stop pretending to cap reasoning; no gate loosens, and
`node-too-large` stays). Evidence: F21.10 arm (c) 3/3 `length` at 20256
with 87% of tokens on reasoning; F21.9 truncations at 32624/32768.
Known-good: a worker call against a fake `/models` reporting
`max_model_len` 175000 and a 6586-token prompt sends `max_tokens` =
175000 - 6586 - margin. Known-bad: the old cap (20256 for the F21.10
node) is not what is sent; an oversized node still fails `node-too-large`.
Contract mutants (each target occurs once; abort if `grep -c` is not 1; drop `__pycache__` after each revert):
1. `max_tokens` -> a constant 16384 -> known-good red.
2. `node-too-large` check -> `if False:` -> the pre-flight known-bad red.
3. `max_model_len` ignored (fallback always) -> the fake-server test red.
Done when: mutants red; `./check.sh` green; T6-14's status says which
of its clauses this supersedes. Owner: main session. Then T6-15, T6-18,
T6-9, and one T5 seed with `--deadline` sized for this model (a call at
this cap can run 15-20 min on the 3090; `run_arm.sh`'s `timeout 1800`
is the clock that killed round 3's T5, not the harness).

### T6-18 — A worker response with no content is a named failure, not an empty diff (tightened)
Status 2026-09-20: DONE by the main session (commit below), with T6-15.
`_parse_message`'s no-content branch (shared by emit, complete and
propose_diff) now raises `message has no text content (finish_reason=
<reason|unknown>, N reasoning chars)` carrying reasoning, usage,
max_tokens and finish_reason like a truncation does, so `_error_evidence`
puts the think block in the sidecar unchanged. Known-good: the b-s2
shape end to end through `run_task` (attempt fails with that message,
retry runs, sidecar holds the reasoning and usage, repair prompt names
it). Mutant: reasoning dropped → `""` KILLED (vllm test and the cli
integration test). Wording deviation from the item: the message keeps
the old prefix so the five malformed-envelope pins still read as one
family; "the think block ran out" is what the count says.
Files: `src/saddle/vllm.py` (`_parse_diff_response`: the `content is None`
/ empty path), `src/saddle/slice.py` (`_error_evidence`), `tests/test_vllm.py`.
Evidence: F21.10 b-s2 -- HTTP 200, `finish_reason: "stop"`, `content:
null`, 45428 chars of reasoning that stop mid-word, and the server's
`reasoning_tokens: 0`. Today this is either a grammar-parse error or
whatever the empty-string path does; neither names what happened.
Contract: a response whose content is null or empty raises
`VllmResponseError("worker emitted no diff (finish_reason=stop, N
reasoning chars)")` carrying the reasoning, usage and `finish_reason`
(the T6-12 fields), so the attempt fails retryably and the sidecar
shows the think block ran out. Do not trust `reasoning_tokens` (F21.10
follow-on 20). Direction: tightened (a distinct verdict for a shape that
was folded into another). Known-good: the b-s2 body, reduced to its
shape, produces that message and the reasoning in the sidecar.
Known-bad: a normal diff response is unaffected.
Contract mutants (each target occurs once; abort if `grep -c` is not 1; drop `__pycache__` after each revert):
1. the null-content branch -> falls through to the old path -> known-good red.
Done when: mutant red; `./check.sh` green. Owner: main session, with T6-15.

---

### T6-19 — Round 3c: one T5 seed under T6-17, T6-15, T6-18, T6-9, T6-12 and T6-13 (measurement; F21.12; no code)
Files: `../saddle-bench/runs/round3c/PREDICTION.md` (new; committed
before the run), `../saddle-bench/run_arm.sh` (as committed at bench
b4661a2: `--deadline "$DEADLINE"`, outer timeout = DEADLINE + 600; do
not edit), `../saddle-bench/oracles/oracle_t5.py` (run after; do not
edit), `../saddle-bench/runs/FINDINGS.md` (F21.12),
`../saddle-bench/runs/PROGRESS.log`. Nothing under saddle `src/` or
`tests/` changes; the harness is saddle HEAD 60eaea0.
Why: the last two T5 seeds (round 3a x3, round 3b x1) all ended with
nothing sealed. Since then: reasoning is not budgeted and the worker
gets the window left after its prompt (T6-17); retries are not greedy
(T6-15); a no-content response is named (T6-18); the run stops on its
own clock and seals what it has (T6-9); every attempt leaves a sidecar
and the plan is journaled (T6-12, T6-13). This seed is the first run in
which a failure is readable from the journal alone, and the first test
of whether T5's failure class was the harness's budget model.
Pre-register in `runs/round3c/PREDICTION.md`, committed in saddle-bench
before `run_arm.sh` starts: P1 no worker call ends with
`finish_reason=length` (T6-17; every sidecar's `finish_reason` is `stop`
or the attempt failed for another named reason). P2 `saddle verify` on
the run's journal passes, every `worker:*` attempt span has a sidecar
that hashes, and the journal holds one `plan` record per plan emitted.
P3 the run ends with saddle's own exit code (0, 1 or 3), never the outer
`timeout`'s 124; if 3, the run span reads `deadline:`. P4 node 1 (the
first impl node) seals a proof (F21.10 arm (a) completed 3/3 at a 20256
cap; the cap is now ~165k). P5, stated as uncertain: the oracle passes.
Procedure: `set -a; . ~/.config/saddle/env; set +a;` on every command
that needs the server; `saddle doctor` green; `RUN_DIR=runs/round3c/t5-s1
SAMPLE_TEMP=0.7 DEADLINE=7200 ./run_arm.sh t5 saddle` (two hours: with
T6-17 one worker call can run 15-20 min on the 3090, so a four-node plan
with retries needs room; the outer backstop is 7800 s). Follow with
`saddle tail runs/round3c/t5-s1/t5-saddle/.saddle/proofs.jsonl` in a
second shell; do not edit any file while it runs. After: `saddle verify`
on that journal; `python3 oracles/oracle_t5.py runs/round3c/t5-s1/t5-saddle`;
a per-attempt table from the sidecars (node, attempt, `finish_reason`,
`completion_tokens`, `max_tokens` sent, gates failed, wall) -> F21.12,
with each of P1-P5 marked HELD or FALSIFIED and every failure labelled
HARNESS or MODEL with the sidecar that shows it. One line in
PROGRESS.log. Stop and report, without retrying, if `run_arm.sh` exits
90 (workdir not pristine) or the server is down.
Done when: PREDICTION.md committed before the run, the run directory and
F21.12 committed after it, P1-P5 each resolved with evidence. Owner:
executor (needs the key). Not in this item: any fix; a FALSIFIED P1-P3
is a main-session item, a FALSIFIED P4/P5 is data.

**Status (2026-09-20):** DONE, bench `c1e60bd` (PREDICTION.md before the
run) and `50ebbb8` (`runs/round3c/t5-s1`, F21.12). P1, P2, P3 HELD: 18
worker calls all `finish_reason=stop` (largest sample 52442 tokens against
a 165642 cap); verify OK, 7/7 sidecars hash, one `plan` record per plan;
exit 1 at 5222 s, 1978 s inside the deadline. P4 and P5 FALSIFIED. P4's
cause is HARNESS (F21.12a, fixed by T6-22 below), not model: no impl node
ever faced a mutation gate that could pass. Also found: F21.12b (a timed
out request skips `_abandon`, 530-line `money.py` left staged unproven;
`_error_evidence` empty for non-`VllmResponseError`), stale `samples` in
retry sidecars, the sampler scores without `autofix`, ties fall to index,
`--3way` unreachable (T6-10 confirmed), ruff detail names no rule. Each is
a main-session item in turn (T6-23, T6-24); T6-19 fixed nothing.

### T6-22 — The mutation gate runs the node's declared pytest scope, not the whole suite (F21.12a; scope narrowed)

Files: `src/saddle/evidence.py` (`pytest_scope`, `_mutmut_scratch_config`
order), `src/saddle/runner.py` (the gate's `mutation_sample` call),
`tests/test_evidence.py`, `tests/test_runner.py`.

The gate's `mutation_sample` call passed no `run_tests`, so mutmut
baselined against the whole scratch tree. On a TDD plan the test node
writes every module's specification red up front and impl nodes green
them one at a time, so the suite is red until the last impl node lands;
the tests gate honoured the declared scope and passed, the mutation gate
ignored it and died with `failed to collect stats` on all three impl
attempts of round 3c. Reproduced in the bench with real mutmut (arm A
unscoped: exit 1; arm B the declared scope: 331 mutants). No impl node
could seal on this plan or any plan shaped like it, which is a candidate
cause for eight T5 seeds completing nothing.

Contract: the mutation gate's engine collects the same tests the tests
gate ran. `pytest_scope(test_command)` is the arguments after the first
`pytest` token; the runner passes it as `run_tests`. A command that never
names pytest yields nothing and the engine runs its whole tree as before.
`_mutmut_scratch_config` keeps a sequence's order (a `-k expr` pair) and
still sorts an unordered collection. The property oracle's narrower
`run_tests` is unchanged. Direction: scope narrowed. A mutant is judged by
the node's declared tests only; before, a green whole suite could kill it
with any test and a red one issued no verdict.

Tests: red first, `test_run_node_gate_mutation_runs_the_declared_scope_not_the_red_suite`
fails on the pre-change source with the production detail and passes
after; known-bad against the real engine,
`test_mutation_sample_scoped_run_baselines_past_a_red_sibling_specification`
(unscoped: stats failure; scoped: 1 of 1 killed); `pytest_scope`
known-good/known-bad; config order. Mutants (three, all KILLED): runner
passes `run_tests=()`; `pytest_scope` keeps the `pytest` token;
`_mutmut_scratch_config` always sorts.

**Status (2026-09-20):** DONE, `2af0d5d`, main session; `./check.sh`
green (700 passed, 3 skipped, 100%).

### T6-23 — Every give-up path abandons the node's diff and keeps its evidence (F21.12b; tightened)

Files: `src/saddle/slice.py` (`_run_node` `except BaseException`,
`_error_evidence`), `tests/test_slice.py`.

Round 3c's n2.r2 attempt 2 timed out after 1826 s. The `except
BaseException` branch seals the attempt and re-raises without calling
`_abandon`, so the applied diff stayed in the worktree: `money.py` (530
lines) staged with no proof, and the oracle graded it. `_error_evidence`
returns `{}` for anything that is not a `VllmResponseError`, so the
sidecar had four keys and thirty minutes of generation are unrecorded.
Contract: a node that ends on any exception leaves the worktree at its
baseline, and its sidecar carries whatever the client had (exception
type and message at least; reasoning and usage where the client has
them). Known-bad: a propose that raises a timeout leaves no staged diff
and a sidecar naming the exception. Owner: main session.

**Status (2026-09-20):** DONE, `073f9bc`, main session. The `except
BaseException` branch seals, then `_abandon`s, then re-raises; every
failed worker call's sidecar carries `error_type`, and this branch's
carries `samples`. A transport failure has no envelope, so there is no
reasoning or usage to keep; what was added is what the client knew.
Whether a transport error should be a spent attempt rather than a dead
node is a separate loosening, not made. Mutants three, all killed;
`./check.sh` green.

### T6-24 — The sampler scores what the gate will see, and a retry's sidecar reports its own draws (F21.12c; tightened)

Files: `src/saddle/slice.py` (`_best_of_samples`, `_evaluate_candidate`,
`_run_node` `samples`), `tests/test_slice.py`.

Two defects from the same run. (a) `samples` is built once per node and
reassigned only on attempt 1, so attempts 2 and 3 of n2 carried attempt
1's samples byte for byte (identical SHA-256). Contract: a sidecar's
`samples` are the calls that attempt made; a retry with one call records
one. (b) `_evaluate_candidate` goes apply then gate while the real path
runs `autofix` between them, so a sample's score was one higher than its
real failure count on three of four nodes and `failures == 0` never ended
sampling early. Contract: a candidate is scored by the same sequence the
node is gated by. Ties still fall to sample index; recorded, not changed
(a tie is a tie). Owner: main session.

**Status (2026-09-20):** DONE, `a1c3586`, main session. `samples` resets
at the top of every attempt; a retry's single call records one entry, a
retry whose call fails records none. `_evaluate_candidate` autofixes the
candidate copy before its gate run. Mutants three: the reset mutant
survived the first two tests (every non-failing retry path reassigns)
and a third test for the failed-retry-call path killed it. `./check.sh`
green.

### T6-25 — Draw k samples concurrently; evaluate serially in seed order; seal the first pass (tightened in effect)

Files: `src/saddle/slice.py` (`_best_of_samples`), `src/saddle/vllm.py`
(seed on the request; a pool over the sync client), `src/saddle/cli.py`
(the proposer), `tests/test_slice.py`, `tests/test_vllm.py`.

Premise (the user's, 2026-09-20): the model is local, tokens cost
nothing, and vLLM batches concurrent requests (the container's own
table: 1 stream 111 tok/s, 2 streams 191, 4 streams 268). Serial k
sampling saves nothing and costs k-1 full worker latencies whenever
sample 1 fails. Round 3c sized it: n1's first attempt drew three samples
one after another for 1028 s of wall and n2's for 722 s; every other node
waits on n1. Concurrent draws cost roughly one draw's wall.

Contract: (1) `_best_of_samples` issues its k worker calls concurrently,
each with its own `seed`, at the sample temperature; (2) the returned
diffs are evaluated serially in the worktree in seed order (arrival
order is not deterministic) and the first that passes seals, gates
unchanged; (3) retries draw k too, since T6-15 made them non-greedy;
(4) the distinct-sample count still varies and a test shows it (CLAUDE.md
vacuity rule); (5) k is bounded by the context left: round 3c's largest
sample was 52442 tokens on a 175k window, so three at that size fit and
three at the 165k cap do not (vLLM preempts and the draws go serial
again). Interaction with T6-24b: with k paid up front, early stop on
`failures == 0` no longer saves wall; scoring through `autofix` then
matters for picking the right sample, not for time.

Known-good: a fake server that records request start times shows k
calls overlapping; a scripted set of k distinct diffs where only the
last-by-seed passes seals that one after evaluating the others in seed
order. Known-bad: k identical samples at temperature 0.0 still report
`1 distinct of k`. Mutants: (1) the pool size forced to 1 -> the overlap
test red; (2) evaluation in arrival order -> the seed-order test red
(the fake makes arrival order adversarial). Owner: main session.

**Status (2026-09-20):** DONE, `e270fd7`, main session. Clauses (1)-(4)
landed: thread pool of PROPOSAL_SAMPLES over the sync client, `seed` on
the diff request (0..k-1; a retry's seed follows), evaluation in seed
order with the rest recorded `not evaluated: an earlier sample passed`,
the constant-proposer pin still `1 distinct of k`. Clause (5), bounding k
by the context left, is **not** implemented: vLLM handles KV pressure by
preemption, not rejection, and round 3c's largest sample was 52442 tokens
on a 175k window, so three fit; T6-26 observation (d) measures whether
three in flight ever preempt, and a bound is added on that evidence.
Eleven tests that pinned one worker call per passing node now pin k, each
named in the commit. Mutants three, all killed; `./check.sh` green.

### T6-26 — Round 3d: one T5 seed under T6-22 to T6-25 (measurement; F21.13; no code)

Same procedure as T6-19 (`RUN_DIR=runs/round3d/t5-s1 SAMPLE_TEMP=0.7
DEADLINE=7200 ./run_arm.sh t5 saddle`, PREDICTION.md before the run).
This is the first run in which an impl node faces a mutation gate that
can pass, so P4 (the first impl node seals) is the live question. P1-P3
as in T6-19. P5 (oracle) uncertain. Pre-registered observations, not
predictions: (a) round 3c's retry output collapsed 29439 -> 14246 -> 3250
completion tokens on n2, opposite to F21.9a; does it recur once retry
sidecars report their own draws (T6-24a)? (b) sample attempts decoded at
89-102 tok/s and the two single-call retries at 58-61; speculative
decoding acceptance differing between a long sample and a short retry
is a guess, written here as one. (c) coverage failed at 88.9%/89.7% on
the one impl node that reached it (MODEL); does it recur? (d) with
T6-25's three draws in flight, does the server preempt (vLLM logs, or a
sample wall far above the others'); the sidecar walls and the container
log answer it, and T6-25's clause (5) waits on it. Owner:
executor (needs the key). Not in this item: any fix.

**Status (2026-09-20):** DONE, bench `8cb4096` (PREDICTION.md before the
run) and `d3ad1c8` (`runs/round3d/t5-s1`, F21.13). P1-P3 HELD (13 worker
calls all `stop`; 6/6 sidecars hash, one plan record; exit 1 at 4524 s).
P4, P5 FALSIFIED. Confirmed live: T6-22 (the mutation gate measured,
`75.3% < 85.0%` and `78.3%` with named survivors, where round 3c could
only say the tool failed), T6-23 (a 2494 s timeout left the tree at
baseline with an `error_type` sidecar and no staged `money.py`), T6-25
(the same three-draw attempt 1028 s -> 257 s, 213.6 tok/s aggregate).
Observations: (a) no recurrence; (b) **refuted**: the 58-61 tok/s figure
was computed from the stale sidecars T6-24 fixed, so the gap never
existed; (c) recurs at 89.9%; (d) no preemption signature in aggregate
throughput, container log not gathered (out of the executor's scope).
New HARNESS findings, each an item below: F21.13c the request timeout is
inter-chunk, not total (T6-30); F21.13d the repair brief withheld ruff's
output and ruff failed 2/2 while the gates whose detail is inline
improved (T6-31); F21.13e `money.py:33` is a spec-mandated branch no
sealed test exercises and the impl node may not write one, so it had no
legal move (T6-29); F21.13f the journal attests and cannot explain
(T6-27, T6-28). n1 sealed on attempt 3: the first T5 node sealed in four
rounds. Everything remains n=1 per configuration.

### T6-27 — The journal explains a failure, not only attests to it (tightened; journal format)

Files: `src/saddle/journal.py` (`SpanRecord`, `PlanNode`, verify),
`src/saddle/slice.py` (`_seal_attempt`, `_proposal_evidence`,
`_error_evidence`, `_best_of_samples`), `src/saddle/cli.py` (the
proposer; a `saddle explain` command), `src/saddle/vllm.py` (cached
prompt tokens from usage), `tests/test_journal.py`, `tests/test_slice.py`,
`tests/test_cli.py`, `tests/fixtures/pre-basis-proofs.jsonl` (read, never
regenerated).

Round 3d's audit of its own evidence (F21.13f): no record carries an
absolute time (0 ISO-8601 fields, 0 epoch fields; spans have
`duration_ms` only); no sidecar carries the seed T6-25 sent, the sampling
temperature, or the served model id; every worker span is appended with
an empty argv, so its `args_hash` is the hash of `[]` and cannot tell two
calls apart; the submitted diff survives as a hash only and `git apply`'s
stderr is dropped; the plan record omits each node's `allowed_tools`, so
F21.13d's cause is not recoverable from evidence. One design decision
surfacing six times: the chain was built to prove integrity, and a hash
is designed to not give its input back. Diagnosis needs the inputs.

Contract, alongside the hashes, never instead of them:
1. Every span carries `started_at` (UTC ISO-8601 from the run's `now`)
   beside `duration_ms`; the run span carries the served model id and
   server version (from `/models`), the sample and recovery temperatures,
   and the context window used.
2. A worker span's `argv` is `["worker", node_id, "attempt", n]`, its
   `args_hash` is the hash of the prompt, and the prompt text is retained
   in the sidecar.
3. Every sample and retry draw records its `seed`, `temperature`,
   `started_at` and wall, the diff text (not only its hash), the cached
   prompt tokens the server reported, and on an apply failure `git
   apply`'s stderr and which rung of the ladder failed.
4. `PlanNode` carries `allowed_tools`.
5. `saddle explain <journal> [--attempt SPAN]` renders the redacted tier
   (identifiers, times, counts, hashes, gate verdicts, finding codes) and,
   for one named attempt, its raw sidecar. Nothing else prints prompt or
   diff text.
6. `verify` reads both shapes: the pre-basis fixture still verifies, a
   new journal verifies, and a sidecar whose retained diff does not hash
   to `diff_hash` is a new verify code.
Direction: tightened (the record says more, and one more thing can fail
verification). Known-bad: a failed attempt that cannot be replayed from
its sidecar (no seed, or no prompt); a retained diff that does not hash
to its `diff_hash` passing verify. Known-good: replaying a recorded
attempt's prompt, seed and temperature through a fake client reproduces
the recorded diff hash. Mutants: (1) `started_at` written as the
duration; (2) `args_hash` back over `[]`; (3) the retained-diff check
removed from verify. Owner: main session. Ahead of T6-1: the
retrospective is scored on exactly the evidence this item keeps.

**Status (2026-09-20):** DONE, `647e76f`, main session. Clauses 1-6
landed; clause 2 with one deviation: `args_hash` stays the hash of argv
(one rule for every span) and the prompt's sha256 is an argv element, so
the hash differs per call and the prompt hash is visible. Cached prompt
tokens needed no new field: `usage` has carried `cached_tokens` since
T6-12. verify gained `sidecar-diff-hash`; the pre-basis fixture still
verifies unchanged. Mutants three, all killed; `./check.sh` green.
Observed while verifying an outside claim: the worker's requests do not
stream (only `saddle up`'s chat payload does), so `usage` arrives in the
body and every round-3d sidecar carries it; but when the think block
never closes the server's `reasoning_tokens` stays 0 beside a full
`completion_tokens` (round 3d n1 attempt 2: 20823 and 0), so
`completion_tokens` is the count to trust when content is empty, and
`explain` prints it.

### T6-28 — The run samples the server it is talking to (bench-side; measurement infrastructure; no saddle code)

Files: `../saddle-bench/run_arm.sh`, a new `../saddle-bench/metrics_sample.py`
and `metrics_report.py`, `../saddle-bench/runs/FINDINGS.md`.

Round 3d's observation (d) was answerable only from aggregate throughput,
and the rate gap in observation (b) took a round to refute. The server
publishes what both needed on its metrics endpoint, on the port saddle
already uses. Contract: while `run_arm.sh` runs an arm, a sampler GETs
`/metrics` once a second (read-only, no container access) and appends one
NDJSON row per sample with the UTC time and these counters: requests
running and waiting, KV-cache usage percent, request queue, prefill and
decode time sums and counts, time to first token, prompt tokens and
cached prompt tokens, generation tokens, speculative-decode draft and
accepted tokens, plus one `nvidia-smi` sample (utilisation, memory). The
report joins the rows to the run's attempt spans by `started_at` once
T6-27 provides it, and answers per attempt: queued or slow (queue time),
preempted or not (KV usage with requests waiting), acceptance rate
(accepted over draft), prefill share (prefill over decode). Never records
prompts or the key. Known-good: a fake metrics endpoint whose counters
step on schedule yields a report with the expected per-attempt values;
known-bad: rows with no attempt in their window are reported as idle, not
attributed. Owner: executor (needs the container up, not the key). After
T6-27; before the next seed, so round 3e answers (d) rather than guessing.

### T6-29 — A surviving mutant or an uncovered branch inserts a test node, briefed with the requirement and the function, never the code (tightened in effect)

Files: `src/saddle/slice.py` (recovery path, `_best_of_samples` with a
union combiner), `src/saddle/runner.py` (`_stub_module` reuse; the
enclosing-function lookup), `src/saddle/cli.py` (the test node's brief;
`replan` insertion), `src/saddle/gates.py` (`check_node_scope`
unchanged), `tests/test_slice.py`, `tests/test_runner.py`, `tests/test_cli.py`.

F21.13e: n2 had to cover `money.py:33`, a branch the spec mandates and no
sealed test exercises, and `check_node_scope` forbids an impl node from
editing tests. Its legal moves were to delete the branch, contort the
function, or fail. F21.13a's caveat: the survivors at 75.3% and 78.3% are
over different populations (`max_mutants=100` resampled per attempt), so
"killed" and "not sampled" are indistinguishable across attempts.

Design, as agreed 2026-09-20. The gap is a plan defect, and the recovery
path answers it with a test node, not another impl retry:
1. **Locate.** Each survivor and each uncovered line maps to its enclosing
   function by AST (deterministic). The mutant sample is seeded by node id
   so attempts compare the same population.
2. **Brief.** The inserted test node receives the node's requirement ids
   and text, the changed modules replaced by their signature-preserving
   stubs (`_stub_module`, as red-phase already does), and the names of
   the functions found in step 1. It never sees the implementation, a
   line number, or a mutant. The expected values must come from the
   spec; the function name only aims the draw.
3. **Draw.** The node draws k samples concurrently through T6-25's
   sampler at `reasoning_effort: none` (thinking disabled: each draw is
   its prompt plus the test file it emits, so ten draws fit the KV pool
   with room). Each sample writes its own new test file named by
   requirement and seed, so k diffs never conflict. k defaults to 10;
   k=1 is the node-sized version on the same code path.
4. **Select, deterministically.** A sample is kept when its tests fail
   against the stubbed tree, pass against the real tree, and kill at
   least one still-surviving mutant or execute a still-uncovered line.
   Samples that pass the first two and add nothing are dropped. Kept
   samples are spliced in by the ordinary apply; node scope still forbids
   a test node from touching source.
5. **Retry, bounded.** Survivors left standing get one more round of k.
   After two rounds they are recorded as surviving; for an equivalent
   mutant that is the correct verdict. Then the impl node retries with
   the kept tests in scope, its coverage and mutation gates re-run, and a
   kept test that fails against the real tree is the impl node's next
   brief (a real defect, found by a spec-derived test).
What this is not: a sample is never told which mutant to kill. A test
that asserts what the code does pins the implementation; a wrong branch
then scores 100% with a green test protecting it. The kill check happens
after the draw, against the real code, and the score stays a
measurement of whether the spec would have caught a wrong
implementation. Direction: tightened in effect (more tests, from the
spec, on the gaps the gates found; no threshold moves). Measure first: a
ten-draw probe at effort `none` on round 3d's n2 gap, counting how many
draws pass all three filters (executor, needs the key); if it is one in
fifty rather than one in ten, effort goes to `low` for these draws and k
drops. Known-bad: a draw briefed with the implementation (the mutant
kill test passes on a wrong branch) is not representable; a sample that
kills nothing is not kept; a third round is not started. Mutants: (1)
the stub replaced by the real module in the sandbox; (2) the kill filter
inverted; (3) the round bound removed. Owner: main session, after T6-27.

### T6-29a — Measure first for T6-29: ten reasoning-off draws on round 3d's gap (measurement; F21.14; no saddle code)

Files: `../saddle-bench/probes/t6_29_probe.py` (new),
`../saddle-bench/runs/round3d-probe/`, `../saddle-bench/runs/FINDINGS.md`.

The question T6-29 needs answered before k=10 at effort `none` is the
default: how many such draws pass all three filters. Procedure: take
round 3d's n2 tree at its baseline ref (`runs/round3d/t5-s1/t5-saddle`,
`refs/saddle/baseline/n2` plus the sealed `money.py`); build the brief
exactly as T6-29 step 2 says -- the node's requirement ids and text from
the T5 prompt, `money.py` replaced by its signature-preserving stub
(`saddle.runner._stub_module`), the function name `to_decimal`, the
existing test file conventions -- and nothing from the implementation;
draw ten times with `reasoning_effort: none`, seeds 0-9, temperature
0.7, each into `tests/test_money_probe_<seed>.py`; run each file against
the stubbed tree (must fail), the real tree (must pass), and record
whether it executes `money.py:33` and which of round 3d's surviving
mutants it kills (mutmut over the same `source_paths`, seeded). Report
the 10x4 table and the pass count. Ten draws at a few seconds each: one
short session. Owner: executor (needs the key). After T6-27.

**Status (2026-09-20):** DONE, bench `495014e`, F21.14. Two of ten
reasoning-off draws pass all three filters (seeds 1 and 6: red on the
stub, green on the real tree, execute `money.py:33`, kill no survivor);
four do not parse; three are plausible-looking tests that are simply
wrong (`Decimal('NaN') == Decimal('NaN')` asserted, `DID NOT RAISE` on
inputs the spec accepts); one never stopped (32768 tokens). No draw
returned empty content. Numeric length budgets in the brief are ignored
at effort `none`; a cardinality bound (one section, one hunk) is
honoured 6/6. Two harness findings: a creation without `new file mode`
is representable and never applies (T6-32), and 34 of n2's 66 survivors
edit only message text no requirement constrains (T6-33). The brief was
iterated five times on emission shape only; both versions are committed.

### T6-29b — Survivor-driven test node, the machinery: locate, stub, brief, filter (tightened in effect; new module; executor)

Files: `src/saddle/survivors.py` (new), `tests/test_survivors.py` (new).
Nothing under `src/saddle/slice.py`, `runner.py`, `cli.py`, `gates.py`
or `evidence.py` is edited; the module may import from them.

The pure half of T6-29, so the splice into the recovery loop (T6-29c,
main session) is the only part that touches the node loop. Contracts,
each with a known-good and a known-bad and one mutant:
1. `enclosing_functions(workdir, survivors, uncovered) -> dict[str, set[str]]`:
   for each changed module, the names of the functions (by `ast`, nested
   functions reported by their outermost def) that contain a surviving
   mutant's line (from `MutationOutcome.survivors` names via `mutmut
   show`-style `path:row`, as `mutation_sample` already resolves them)
   or an uncovered line. A line outside any def maps to the module's
   top level, named `<module>`. Known-bad: a line in an unchanged module
   is not reported.
2. `stubbed_sandbox(workdir, changed_modules) -> Path`: a throwaway copy
   of the tree (as `_evaluate_candidate` copies it, with the index
   refreshed) in which every changed module is replaced by its
   signature-preserving stub (`saddle.runner._stub_module`). Known-good:
   the stub imports and exposes every def; known-bad: the sandbox
   contains none of the implementation's bodies (grep for a body line).
3. `build_survivor_brief(node, requirements_text, stubs, functions,
   test_conventions) -> str`: the prompt for one draw, carrying the
   node's requirement ids and text, the stub source of each changed
   module, the function names from (1), and the names and one sample of
   the existing test files. Known-bad: the brief contains no line of the
   real implementation and no mutant name (assert on both).
4. `candidate_test_path(requirement_id, seed) -> str`:
   `tests/test_<req>_s<seed>.py`, distinct per seed, so k diffs never
   collide.
5. `keep_candidate(stub_tree, real_tree, candidate_file, survivors_before,
   uncovered_before, *, run) -> Verdict`: the three filters as one
   deterministic decision: the file's tests must fail against the stub
   tree, pass against the real tree, and either kill at least one
   mutant in `survivors_before` (re-run `mutation_sample` scoped to that
   file with the same seeded sample) or execute at least one line in
   `uncovered_before`. Returns which mutants it killed and which lines
   it covered. Known-good: a spec-derived test that kills a survivor is
   kept; known-bad: a test that passes on both trees is dropped (it
   pins nothing), a test that fails on the real tree is returned as
   `failing` (T6-29c hands it to the impl node), a test that passes both
   filters and kills nothing is dropped.
Mutants: (1) filter one inverted (a candidate green on the stub is
kept); (2) filter three removed (a candidate that kills nothing is
kept); (3) the stub replaced by the real module in the sandbox. Owner:
executor. Measure-first result from T6-29a goes into the docstring of
(5) when it lands.

**Status (2026-09-20):** DONE, `2e41e43`, executor (session 40).
`src/saddle/survivors.py` with the five contracts; `keep_candidate`
takes an injected `CandidateRunner` and no subprocess runner ships (that
is T6-29c's). Noticed: `_stub_module` breaks a module that decorates a
function with one of its own (the decorator stubs to `raise`); and
`MutationOutcome` names survivors only, so killed sets are a difference
of two runs.

### T6-29c — Survivor-driven test node, the splice (tightened in effect; main session)

Files: `src/saddle/slice.py` (recovery path, `_best_of_samples` union
combiner), `src/saddle/cli.py` (the test node's proposer at effort
`none`; `replan` insertion), `tests/test_slice.py`, `tests/test_cli.py`.

Uses T6-29b's module. When an impl node fails coverage or mutation and
the gap's enclosing functions (T6-29b.1) are in no sealed test's
citations, the recovery path does not retry the impl node: it inserts a
test node scoped to those requirements, briefed by T6-29b.3, drawing k
samples concurrently through T6-25's sampler with each sample written
to T6-29b.4's file and judged by T6-29b.5; kept samples are spliced by
the ordinary apply, at most two rounds per requirement, then the impl
node retries with the kept tests in scope. A kept candidate that fails
on the real tree becomes the impl node's brief. k from T6-29a's result
(default 10 at effort `none`). Known-good: end to end on the runner
fixture with an uncovered branch, the run seals a test node then the
impl node. Known-bad: a third round is not started; a node whose gap is
already cited by a sealed test retries as today. Owner: main session,
after T6-29b reports.

**Amended 2026-09-20 on F21.14:** (a) a candidate red on the real tree
is dropped and recorded, never handed to the impl node as its brief:
three of ten probe draws were plausible tests that were simply wrong;
(b) the filters run parse-first (`syntax` before pytest), since four of
ten did not parse and a syntax error must not score as "fails against
the stub"; (c) the draws run at effort `low` by default with a token
cap sized for one test file and a cut to the last complete test
function before the filters -- `none` yielded two usable drafts in ten
and no kills, and numeric length instructions in the brief do not land;
(d) the brief carries the cardinality bound (one section, one hunk)
that the probe showed the model honours; (e) T6-33 lands first, so the
survivors this node is asked to kill are ones a specification can pin.
k and effort stay parameters and round 3e measures them.

**Status (2026-09-20):** DONE, `85ef057`, main session. As amended: (a) a candidate red on the real tree is
dropped and journaled, (b) parse-first with a cut to the last complete
test function, (c) `--survivor-effort` (default `low`) under
`SURVIVOR_MAX_TOKENS` (6000), (d) the brief ends with the cardinality
bound, (e) T6-33 landed first. Deviations from the original text: the
kept files are not `tests/test_<req>_s<seed>.py` but land beside the
first test file the node's gate command runs, as
`test_<req>_r<round>_s<seed>.py` (a flat suite's candidate must import
what that suite imports; the round keeps two rounds apart); `k` defaults
to 10 as `SURVIVOR_SAMPLES` and `--survivor-samples`; the "sealed test's
citations" rule reads a sealed test node's own files (from its attempt
sidecars' diffs, as they stand in the worktree) for the gap function's
name, and the recovery's own test nodes do not count. The verdict now
carries what it left unpinned (`Tier1Result.survivors`/`gaps`,
`MutationOutcome.survivor_lines`), so no gate is re-run to brief. Every
round is one `survivor-tests` tool span under the failed node with one
verdict per seed. Round 3e measures whether a `low`-effort draw kills
anything on a real gap; T6-29a's reasoning-off result (0/10 kills)
stands until then.

### T6-32 — A creation carries its mode line: the diff grammar's create and delete forms (tightened)

Files: `src/saddle/vllm.py` (`DIFF_GRAMMAR`), `tools/diff_grammar_check.py`
(corpus), `tests/test_vllm.py`.

F21.14: `new file mode` was optional metadata, so a creation without it
was representable and `git apply` read `/dev/null` as a path. Contract:
`section ::= header (create | delete | modify)`; `create` requires `new
file mode` before `--- /dev/null`, `delete` requires `deleted file mode`
before `+++ /dev/null`, and a modify side's path never begins with `/`.
Known-bad in the corpus: a creation and a deletion without their mode
line, each rejected at the `/`. Known-good: git's own creation shape.
Done when: `tools/diff_grammar_check.py --run` passes in the serving
container (xgrammar lives there, not in the venv), as T3-18 was closed.
Owner: main session for the change; the container run is the user's or
an executor's with the container up.

**Status (2026-09-20):** DONE, `592f906`, main session; structural pin
and corpus cases committed. Container run of the corpus: **56/56 cases
correct** (user, 2026-09-20, `tools/diff_grammar_check.py --run` in the
serving container at `592f906`; 53 before, plus the two rejects and the
creation admit). Closed.

### T6-33 — Text-only mutants leave the mutation population (scope narrowed, with proof)

Files: `src/saddle/evidence.py` (`text_only_mutant`, `mutation_sample`,
`MutationOutcome.text_only`), `src/saddle/gates.py` (`check_mutation`
detail), `tests/test_evidence.py`, `tests/test_gates.py`.

F21.14: of the 66 survivors n2 was asked to kill, 34 changed only the
text inside a string literal. No requirement constrains those words, so
no spec-derived test can kill them, and a test that does pins the
implementation's wording; `78.3% < 85.0%` was never "behaviour 78%
pinned". Contract: a mutant whose removed and added lines tokenize
identically with string contents blanked (f-string text dropped) and
whose strings differ leaves the population and is counted; one that does
not tokenize line by line stays. The detail reports the survivor count
before the first five names and the excluded count. Direction: scope
narrowed; the proof is the probe's classification (34 of 66). What this
is not: a threshold change; a node still has to kill 85% of the mutants
that remain, all of which a specification could pin.

**Status (2026-09-20):** DONE, `592f906`, main session. Whether n2's
existing tests clear the bar over the behavioural population is round
3e's first question.

### T6-33a — Measure T6-33 on round 3d's n2 without a draw (measurement; F21.15; no code)

Files: `../saddle-bench/probes/t6_33_measure.py` (new),
`../saddle-bench/runs/round3d-measure/`, `../saddle-bench/runs/FINDINGS.md`.

Round 3d's n2 scored `78.3% < 85.0%` over a population the probe showed
was half text-only. Procedure: with saddle at `592f906` or later,
reconstruct n2's attempt-2 tree as T6-29a did (`runs/round3d/t5-s1`,
baseline ref plus the sealed diff), run `saddle.evidence.mutation_sample`
over the same changed lines with the same `max_mutants` and scoped
`run_tests` the gate would use, and report: total, killed, survivors,
`text_only`, the percentage, and the gate verdict `check_mutation`
would give at 85%. Also the same for attempt 1. Needs no key and no
container. Known-good: `text_only` is greater than zero and the
survivors list contains no message-only mutant; known-bad: a mutant the
probe classed behavioural is still in the population. Owner: executor.
Before round 3e: if n2 clears the bar on its existing tests, round 3e is
a rerun; if not, the gap is real and T6-29c closes it.

**Status (2026-09-20):** DONE, bench `6773087`, session 41 (F21.15).
Over the behavioural population n2's attempt 2 goes 78.3% -> 82.0%
(41/50, 10 text-only mutants excluded, 9 survivors) and attempt 1 75.3%
-> 79.5% (58/73, 12 excluded, 15 survivors); both still fail at 85%
(attempt 2 is two kills short). Known-good and known-bad both held
(every excluded mutant was one the probe classed text-only; every kept
survivor was one it classed behavioural). Reconstruction reproduced
round 3d's coverage detail, first five survivors and percentage on all
three trees. So round 3e is not a rerun: the gap is real and T6-29c is
what closes it. Correction to F21.14 recorded as F21.15: "66" was the
whole-tree survivor count, not the gate's denominator (13 on attempt 2).
The `check_mutation` comment in `src/saddle/gates.py` still says "a
66-survivor gap"; T6-34 fixes the wording.

### T6-36 — The attempt sidecar retains its diff whole and scrubs every nesting (tightened)
Files: `src/saddle/journal.py` (`write_attempt_sidecar`: recursive
`_scrub_evidence`; `redact_secrets` split out of `scrub_thinking`),
`tests/test_journal.py`.
Contract: a sidecar's retained `diff` hashes to its `diff_hash` at any
length, `prompt` is retained whole, both redacted; every other string at
any depth (nested `samples[i].thinking` included) is redacted and capped
at `MAX_THINKING_CHARS`.
Direction: tightened (the sidecar now retains what T6-27 said it
retained; nested text is now redacted at all).
Known-bad from the run: round 3e's three sidecars at 4024 chars with a
`[truncated N chars]` tail, `sidecar-diff-hash` x3, run aborted, `saddle
explain` refused; nested thinking 8 194-83 912 chars unredacted.
Contract mutants (each target occurs once; `__pycache__` dropped after each revert):
1. `_RETAINED_WHOLE` loses `"diff"` -> KILLED (the run's failure).
2. the Mapping branch returns `dict(value)` unscrubbed -> KILLED.
3. a retained key returns `value` unredacted -> KILLED.
Done when: mutants red; `./check.sh` green. Owner: main session.

**Status (2026-09-20):** DONE, commit below, main session, the same day
as F21.16.

### T6-37 — The ruff gate's rule set is saddle's, not the installed ruff's default (decision, then tightened)
Files: `src/saddle/evidence.py` (`ruff_findings` and the format leg:
`--isolated` plus an explicit `--select`, or a config saddle writes),
`src/saddle/cli.py` (settings seal the selection), `tests/test_evidence.py`.
Finding (F21.16): the graded workdir carried no ruff config at any
level, yet UP031, DTZ007 and the YTT rules fired: ruff 0.16.7's default
rule set. A gate whose verdict changes with `pip install -U ruff` is not
deterministic in the sense this project means, and T6-3's
baseline-relative rule does not help when the *baseline's own idiom*
(`%`-formatting in 4 of 5 modules) is what the new code copies. Two
candidate contracts, the user's call: (a) pin the selection to what a
gate should catch (F, E4/E7/E9, B, plus the format leg) and record it in
the run settings beside `ruff=<version>` (T6-34); (b) keep the default
set but exempt any rule the baseline already violates anywhere
(style the repo has is style the node may use) -- a loosening with
F21.16 as its evidence. Known-bad for either: round 3e's n2 attempt 1
fails `ruff` today on `accounts.py:65 UP031` and would not. Known-good:
a genuine new defect (F821 undefined name) still fails.
Owner: main session after the decision. Before round 3f: with UP031
gone, n2's failure set is `{coverage}` and the survivor round is
reachable for the first time.

**Status (2026-09-21): DONE, option (a) (tightened).** The user chose
(a). `evidence.RUFF_RULES = ("F", "E4", "E7", "E9", "B")` and
`ruff_argv(command, *args)` build every ruff invocation the harness
makes: `ruff <command> --isolated`, plus `--select F,E4,E7,E9,B` when the
command is `check`. `ruff_findings`, the runner's format leg
(`ruff format --check`) and the slice's `autofix` (`check --fix`,
`format`) all go through it, so no `pyproject.toml`, `ruff.toml` or user
config in or above the workdir can widen, narrow or restyle the verdict;
`--isolated` also fixes the format leg's line length at ruff's default 88.
`ruff_version()` reads `ruff --version` (second word, `"unavailable"` on
OSError, non-zero exit or malformed output) and the run settings seal
`ruff=<version>` and `ruff_rules=F,E4,E7,E9,B`, which lands the version
half of T6-34.
Known-good/known-bad (`tests/test_evidence.py`): a workdir whose
`pyproject.toml` selects `ALL` and whose file uses `%`-formatting passes
with no findings; `def f(items=[]): return missing + items` in the same
workdir yields exactly `B006, F821`. `test_cli.py` pins both settings keys
in the sealed argv and asserts the version is not `"unavailable"` on the
dev box.
Contract mutants (each red on the scoped tests, restored, caches
dropped): drop `--isolated` -> pyproject-ALL known-good goes red (1 failed);
drop `--select` -> the default set fires on the known-good (1 failed);
seal `"ruff": "unavailable"` instead of `ruff_version()` -> settings pin
red (1 failed). Full `./check.sh` green before commit.
Not done here: T6-34's attempt-tree refs and the stale gates.py comment
remain T6-34.

**Verified against the round-3e artifacts, 2026-09-21** (the check this
item predicted but had not measured). Both fixtures applied to
`refs/saddle/baseline/n2` and run through saddle's own leg
(`_apply_diff` -> `autofix` -> `ruff_findings` -> `introduced_findings`,
`PATH=.venv/bin:$PATH`):

| rule set | degenerate draw | honest implementation |
| --- | --- | --- |
| HEAD, `--isolated --select F,E4,E7,E9,B` | exit 1, E402 x60 introduced | exit 0, no findings |
| ruff 0.16.7 default (what round 3e ran) | FURB157 x5, UP031 x4, PIE790, I001 | FURB157 x4, UP031 x4, I001 |

So the run's rule set failed the *correct* implementation on nine
findings, none of them a defect, and that is why n2 never reached
`{coverage}` alone. At HEAD the correct implementation passes and the
degenerate one fails sixty times on `E402` -- one per repeated
`import sys`, a finding that names the repetition itself. Known-good and
known-bad, measured rather than predicted.

### T6-38 — An apply rung for blank-line drift (tightened in effect)
Files: `src/saddle/slice.py` (`_APPLY_MODES`, `_apply_diff`),
`tests/test_slice.py`.
Finding (F21.16): 3 of round 3e's 7 lost draws died with `fees.py:
patch does not apply` at all four rungs. The hunk was the whole file
(`@@ -1,42 +1,56 @@`) with two blank lines misplaced in its context.
`--ignore-whitespace` ignores whitespace *within* a line; a missing or
extra blank line is a line-level insertion and no rung addresses it,
although the comment above `_APPLY_MODES` names exactly this cause.
Candidate: a rung that re-derives the hunk against the file with blank
context lines dropped from both sides (or GNU `patch --fuzz=3`, which
tolerates mismatched context at hunk edges only -- test which one takes
the round-3e diff; the sidecar retains it). Known-bad: attempt 2's diff
from `runs/round3e/t5-s1/evidence/attempts/4fd718eb….json` must apply;
known-good: a diff whose *code* lines mismatch must still be refused
(a drifted `return` is a wrong edit, not drift).
Owner: main session. After T6-37.

**Status (2026-09-21): DONE (tightened in effect).** A fifth rung,
`blank-lines`, after `three-way` in `_apply_diff`: `_reanchor_blank_lines`
rewrites every hunk against a file the tree has so that its blank old
lines are the file's (`_reanchor_hunk`), then applies the result with the
strict flags. A hunk's non-blank old lines (context and deletions) must
occur exactly once, in order, at or after the previous hunk's end, with
only blank lines between consecutive ones, each compared with whitespace
collapsed (the tolerance `ignore-whitespace` already grants); otherwise
the rung is skipped whole and the failure still names `three-way`. Blank
lines the file has become context, blank lines only the hunk has are
dropped (the rung never deletes a blank line), and additions keep their
place relative to the old lines around them. Hunks against files the
diff creates pass through.
Evidence, not the sidecar's diff: the top-level `diff` of every round-3e
sidecar is cut at 4000 chars by the T6-36 defect, and the `-1,42 +1,56`
hunk F21.16 §5 reconstructs is sample 0 of n2's attempt 1 (nested
`samples[0].diff`, 30 943 chars, whole). Reproduced on a copy of the
baseline: all four rungs refuse it; GNU `patch --fuzz=3` rejects the
same diff as malformed at its accounts.py hunk, so fuzz was not an
option. With the rung the fees.py hunk applies and the file's non-blank
lines are exactly the hunk's new side. The whole sample still fails, on
accounts.py, and correctly: that hunk lists two import lines as context
the file never had and omits `_usd`, a wrong edit the rung refuses.
Attempt 2's own failure (`fees.py:58`, hunk `-58,1300`) was against
attempt 1's tree, whose 1334-line degenerate fees.py the run's git object
store still holds; its diff is truncated in the sidecar and was not
reproduced. The "restored to the node's baseline" reading in F21.16 §5
is contradicted by that tree: T6-40 stands.
Tests (`tests/test_slice.py`): the round-3e baseline and hunk verbatim
apply as `blank-lines`; the same hunk with one deleted or one context
code line altered is refused with the tree untouched; an anchor the
file has twice is refused; three placement cases pin where additions
land (file has the blank, hunk has the blank, two edits with drift
between them).
Contract mutants (scoped `-k apply_diff`, each red; restored, caches
dropped): rung absent (`reanchored = None`) -> 3 failed; anchor text not
compared -> 3 failed; first match instead of unique -> 1 failed; hunk
blank lines never consumed -> 1 failed (survived until the two-edit
case was added: the reduced-context rung hides it for a single edit).
Full `./check.sh` green before commit.

### T6-41 — Code nothing depends on is inadmissible (tightened) — DONE 2026-09-21
Files: `src/saddle/gates.py` (`check_dead_additions`, `_identifiers`,
`_without_dead_additions`), `src/saddle/runner.py` (`suite_without`),
`tests/test_gates.py`, `tests/test_runner.py`,
`tests/fixtures/{degenerate_round3e,implementation_round3e,tests_round3d}.diff`.

Contract: a node fails when a private top-level definition it adds is
mentioned by no line of the tree and the suite still passes once it and
the statements naming it are removed. Twelfth Tier-1 check, `dead-code`,
after `coverage`; substituted with "not required" for a `test` node.

**Premise corrected before the code was written.** The first reading of
F21.16 §1 was that round 3e's draw gamed the coverage gate. The reasoning
says otherwise and the record is now: that draw is n2 **attempt 1**, which
carries no failure brief; its 47 586 characters of reasoning name no gate,
no percentage, and none of the emitted helpers; it does not loop (the
most repeated fragment is `validate_currency(currency)`, 14 times, spread
evenly) and it ends coherently on "Let me finalize and write the diff".
The repetition begins only in the content tokens after it. The worker's
prompt uses "executed" exactly once -- "Every changed line must be
executed by the new tests" -- which is **already** the behavioural
phrasing, not a percentage, and it did not help. Execution is a proxy
under any wording; `pass` satisfies it and admits no mutant, so mutation
read 88.8% over 80 mutants on a tree 95% of which was repeated dead code.

So this gate reads no intent and counts no lines. In `keep_candidate`'s
image, it removes what nothing mentions and re-runs the suite in a copy:
the artifact fails by construction. Public names are exempt -- the node
that writes the tests naming a new public function may not have run yet,
and round 3e's `fee_for` is new, public and mentioned nowhere in the tree
it was gated in. The identifier set counts string constants, so `__all__`,
a marker and a `getattr` all protect a name; it decides what is NOT dead,
so it errs wide.

Known-bad: `tests/fixtures/degenerate_round3e.diff` (the real
`samples[1].diff`, whole, 23 201 chars) fails, naming
`_ensure_executed (60 copies)`, `_hypothesis_helper (60 copies)`,
`_noop (59 copies)`, `_check`. Known-good: `implementation_round3e.diff`
-- the identical draw cut before the block -- passes with no candidate at
all, so what the gate rejects is the block and nothing else about the
draw. Also known-good: a private helper whose only caller the node also
wrote (the suite goes red without it), a name reached only as a string, a
new public function no test names yet, an unparseable module (left to the
syntax gate), and a suite already red (removing code from a failing tree
proves nothing). End to end in `test_runner.py`: a node adding
`_touched()` fails on `dead-code` alone and the worktree the other gates
measured is unchanged, because the question is asked in a copy.

Contract mutants (scoped `-k dead_additions`, each red; restored, caches
dropped; each target confirmed unique before applying): verdict inverted
-> 1 failed; the suite never re-run -> 2 failed; public names admitted ->
1 failed; string constants not counted as mentions -> 1 failed; the
known-bad fixture repointed at the known-good -> 1 failed (so the fixture
does the work, not the path). Full `./check.sh` green.

Scope limit: this makes one shape of unobservable code inadmissible. It
does not make gaming inadmissible, and the measure is Goal G1's
gate/oracle agreement, not a count of blocked shapes.

Two instrument facts found while closing the diagnostic mandate on this
item. **`saddle explain` cannot read round 3e at all**: it refuses the
journal with `sidecar-diff-hash@line 24, 289, 294` -- the T6-36 defect --
in every mode, `--attempt` included, so the tool the mandate requires was
unavailable for the run it had to diagnose. Round 3d verifies clean, and
explaining it corrects a premise: **the plan had four nodes**, `1 proven,
1 failed, 2 undispatched`, which is why `report.py` and `store.py` were
never implemented. It also shows a loss mode not yet itemised -- n1
attempt 2, "worker call failed: message has no text content
(finish_reason=stop, 67268 reasoning chars)". `metrics_report.py` on
round 3e joins 2394 samples to 4 attempts: no preemption anywhere, KV
never above 0.573, accept 0.73-0.87, and n2 attempt 1 at 1058.6 s for
115 116 generated tokens -- the degenerate sample's 72 450 dominates the
attempt, which is the wall cost T6-43 and T6-39 share.

### T6-42 — A repair may not raise a metric by deleting what it measured (tightened)
Files: `src/saddle/slice.py` (the repair brief and the retry's gate
inputs), `src/saddle/gates.py`, `tests/test_slice.py`.
Finding, and the sharpest evidence yet for the goal: round 3d's n2
attempt 2 is the **first draw in either round whose reasoning names the
gates** -- mutation 26 times, gate 25, coverage 14, uncovered 12, kill 9,
against **zero** for every first attempt in rounds 3d and 3e. The brief
(T6-31) is what put them there, and what it produced is explicit:

- "So to fix coverage, I need to either: 1. Remove the uncovered code
  (to_dict, from_dict, `__eq__`, `__repr__`, and the `if value <= 0` in
  transfer) 2. Or add tests that cover them (but the instructions say
  'do not modify any test file')" -- and it took option 1, three times
  ("let me just take a completely different approach... Removes uncovered
  code from accounts.py").
- "mutation: The simplified transfer will have **fewer mutation sites**,
  and all will be killed by the tests."

That is the `max_mutants=1` exploit (#50) re-derived by the model, on its
own, from the brief: the kill *rate* rises when the denominator falls.
`store.py` needs `to_dict`/`from_dict`, so the repair proposed deleting
required behaviour to raise two metrics. Both gates are gameable by
deletion and the brief is what points at them. WORKPLAN §0.6 forbids
exactly this move for items; nothing forbids it for the worker.
Contract: a retry may not remove a public definition present in the
previous attempt's tree, or shrink the mutant population, unless a
requirement it cites changed. Known-bad: round 3d n2 attempt 2's diff
(recoverable from the run's object store; its sidecar's top-level `diff`
is empty and the sample reads `retry draw, gated in place`). Known-good:
a repair that adds a test-covered branch and leaves the population alone.
Owner: main session. Before round 3f, because the brief reaches the
worker on every retry.

Status 2026-09-21: DONE by the main session (commit below). Landed a
thirteenth Tier-1 check, `public-deletions`, between `dead-code` and
`red-phase`: `check_public_deletions(baseline_sources, sources)` in
`gates.py` compares public definitions before against after, per module,
and fails naming each one that went. Public means top-level functions
and classes and the methods of public classes; a dunder counts as public
(three of round 3d's four deletions were dunders, so a rule keyed on the
leading underscore alone admits the artifact). Unparsable sources on
either side report nothing -- the syntax gate owns that. `Tier1Inputs`
gained `baseline_sources`, filled in `runner.py` from the materialized
baseline tree before the red-phase loops write stubs and the node's own
tests into it. A spec node substitutes `not required: no source changed`,
as coverage, dead-code and mutation already do.

Deviations from the item text: the contract is narrower than "may not
remove a public definition present in the previous attempt's tree, or
shrink the mutant population, unless a requirement it cites changed". It
compares against the node's **baseline**, not the previous attempt, and
it says nothing about the mutant population. The requirement-citing
escape is not implemented: no node may delete public API, full stop.
Reason: the population half needs a discriminator that does not fire on
an honest refactor that deletes dead code, and "unless a requirement it
cites changed" is a threshold the model supplies -- the `max_mutants=1`
shape (#50) the item itself is about. `slice.py` is untouched.

Known-bad, frozen: `tests/fixtures/repair_deletes_public_round3d.diff`,
the real repair, recovered from the run's dangling blob `234923be`
against `refs/saddle/baseline/n2:accounts.py` -- the sidecar's `diff` is
empty and there is no attempt snapshot (T6-34). It is rejected naming
`Account.__eq__`, `Account.__repr__`, `Account.from_dict`,
`Account.to_dict`. Known-good, and the discriminating half: BOTH round
3e draws -- `implementation_round3e.diff` and `degenerate_round3e.diff`
-- pass, because each rewrites `accounts.py` end to end and re-adds all
four. A check reading the diff would have rejected them; this one reads
the tree the node left. The degenerate draw is in the known-good set on
purpose: T6-41 is what rejects its repeated block, and T6-42 must not
double as that gate.

Direction: TIGHTENED. A node that could seal by deleting untested public
API now cannot; nothing that previously failed now passes.

Mutants, all KILLED: M1 `_is_public` drops the dunder branch (the round
3d fixture then reports 2 deletions, not 4); M2 `_public_definitions`
stops descending into a public class's body (the fixture reports 0,
because `Account` is still defined); M3 `sorted(before - after)` →
`sorted(after - before)` (additions reported instead of deletions); M4
`run_tier1` always substitutes `_not_required("public-deletions")` (the
gate never runs on an impl node, and the check-order pins still pass);
M5 `runner.py` passes `baseline_sources=sources` (the node's own tree as
its own baseline, so nothing is ever missing). `./check.sh` green: 820
passed, 3 skipped, 100% line+branch.

### T6-43 — Verbatim repetition in an emission is a defect, not a draw (tightened)
Files: `src/saddle/vllm.py` (the sampler), `src/saddle/slice.py`
(candidate scoring), `tests/test_vllm.py`.
Finding, corrected 2026-09-21 (F21.18): **3 of the 3** samples in round
3e's n2 attempt 1 degenerated, each in a different costume. Sample 1
repeats a 21-line block 60 times (23 201 chars; `_hypothesis_helper`,
`_ensure_executed` and `_noop` 60/60/59 times, 183 of its 196 `+def`
lines underscore filler). Sample 2 repeats "# Expose the property test
so pytest can discover it." 333 times and `@given(` 151 times across
186 264 chars and 72 450 completion tokens. **Sample 0 is not clean** --
this item said it was, on no evidence: it defines `fee_for` 24 times,
`apply_fee` 23 and `total_fees` 23 inside a single `fees.py` hunk, and
`blocks_of` already scored it 72. The base rate for this node at
`low` with the grammar on is 3/3, not 2/3. In both cases the
reasoning is coherent and the repetition begins after it, so this is an
emission-level pathology, not a planning one, and the cheap levers are
the sampler's (a repetition or frequency penalty) and a candidate filter
that drops a draw whose diff repeats a multi-line block beyond a plain
duplicate. Note the interaction with T6-39: the 186 264-char draw also
dominates the attempt's wall.
Owner: main session. Measure first -- the penalty is a decode change and
one seed cannot separate it from the 3x wall spread (goal clause 3).
The measurement is T6-16's re-ask, reported as **F21.18** (F21.17 is the
first, unfaithful pass; read both). Held at `low`, the effort the run
used, with the grammar as the only manipulated variable and n=6 per arm
across two cells of opposite scheduling: 4 of 6 grammar-on draws
degenerate, 1 emits nothing, 1 is clean; 0 of 6 grammar-off draws
degenerate (Fisher two-tailed p = 0.061, or 0.015 counting the empty
emission). So the grammar is implicated and a sampler penalty is not the
first lever. It is **not** a mechanism, and F21.18 refuses to claim one:
`root ::= section+` is accepting after any complete `hline`, so the
grammar never prevents stopping -- the grammar-off draws stop exactly on
the diff's last line. What is established is that the arm holding the
unbounded `section+` / `hline+` is the only one that overruns. Bounding
them, and restricting the `"\\"` alternative to where git emits it, is
this item's candidate; it must be measured, and F21.18's Result 5 sets
the price: the server returns six distinct outputs for six identical
requests (10 235-40 358 chars at one payload), so n=3 arms cannot
separate a grammar or sampler change from noise.
Do not drop the grammar: it is worth 7/9 against 0/9 on whether a draw
lands at all. Anything that weakens it needs T6-48 landed first, because
T6-48 is what forgives the packaging the grammar currently enforces.

### T6-48 — The apply ladder discards a correct diff over its packaging (loosening, with proof)

Files: `src/saddle/slice.py` (`_unwrapped`, `_apply_diff`),
`tests/test_slice.py`, `tests/fixtures/fenced_draw_round3e.diff`.

Status 2026-09-21: DONE. `_unwrapped` in slice.py runs once at the top
of `_apply_diff`, before the structural precheck, so the precheck, all
four `_APPLY_MODES` rungs and T6-38's `blank-lines` reanchor all see the
same bytes. It does exactly two things: takes the body of one markdown
fence that encloses the whole answer, and guarantees a final newline.
The bytes the server sent are still what the sidecar records -- only the
ladder sees the repair, so the evidence stays faithful to the draw.

The fence must enclose everything and its body must not itself contain a
fence. Both halves are load-bearing and both are pinned: `search` in
place of `fullmatch(diff.strip())` admits prose with a fenced diff in
it, and dropping the body guard splices two fenced blocks into one body
with a stray fence inside. Refusing the second case also declines to
unwrap a fenced diff *of* a fenced document, which is the conservative
direction for a loosening.

Direction: LOOSENED, with the proof the rule requires -- F21.18 shows
the contract rejecting six legitimate inputs, which is a claim that the
contract is wrong, not that something is failing against it. No
compensating tightening is claimed: nothing here weakens what the ladder
does once the diff is unwrapped, and `7282e1e`'s mistake was pairing a
real loosening with a fictional "unrepresentable".

Deviations from the item as written, both stated rather than quietly
taken.

1. **The apply known-goods are not the bench draws.** The item said to
   take fixtures from `round3e-probe/*.json` and not hand-write them.
   Those draws patch round 3e's `money.py` / `accounts.py` / `fees.py`
   baseline, which this suite does not have, so making them *apply* in a
   unit test would mean dragging a three-module tree into `test_slice`.
   Instead the apply tests take `_PACKAGED` -- a diff this suite already
   applies -- and put each packaging defect on it, and the real bytes are
   frozen as `tests/fixtures/fenced_draw_round3e.diff` (round 3e n2
   attempt 1, seed 0, grammar off: two leading blank lines, a ```diff
   fence, no final newline) with a test that the unwrap changes its
   packaging and nothing else. F21.18 is the evidence that these defects
   occur in real draws; the fixture is the evidence that the unwrap
   handles them as they actually arrive.
2. **Mutant 3 as the item predicted it was dropped.** "Append the newline
   unconditionally" is an equivalent mutation -- appending to a string
   that already ends in one is a no-op -- so it would have SURVIVED and
   said nothing. It was replaced by two that pin the known-bads.

Not done, deliberately: the unfenced case keeps its leading blank lines
(all nine grammar-off draws open with two, and `git apply` tolerates
them). Only the fence path strips them, because they sit outside the
fence.

Known-good: a diff that applies, minus its final newline, applies on
`strict`; the same diff inside a ```diff fence applies on `strict`; the
frozen round-3e draw unwraps to exactly the bytes inside its fence plus
a newline. Known-bad: prose around a fenced diff, and two fenced blocks,
are both still the named precheck failure.

Contract mutants, each with a verified target count of 1, all KILLED:
M1 drop the trailing-newline half -> the missing-newline known-good red
(and the fixture test). M2 never unwrap a fence -> the fence known-good
red. M3 drop the "body still holds a fence" guard -> the two-blocks
known-bad red. M4 `_FENCE.search(diff)` instead of
`fullmatch(diff.strip())` -> the prose known-bad red.

**Vacuity, restated now that it has landed.** With `DIFF_GRAMMAR` on,
the worker's output always ends with `\n` and never carries a fence
(8/8 and 0/9 in F21.18), so this cannot fire in the production arm
today. It is a precondition for T6-43 touching the grammar at all, and
what the ladder owes if `--worker-effort`, a bounded grammar or a later
model ever emits without one. It does not fix a live failure, and this
item does not claim it does.

`./check.sh` green: 829 passed, 3 skipped, 100% line+branch.

Finding (F21.18). Of nine unconstrained draws of round 3e's n2 attempt 1
-- two cells, both efforts -- **none applies as saddle sends it, and six
of the nine are correct diffs rejected over packaging alone**. Four fail
`corrupt patch at line N` for want of a final newline, and appending one
`\n` makes them apply on the `strict` rung. Two more are wrapped in a
complete ```diff fence, rejected by the precheck at `slice.py:364`
before git runs; unfencing plus that newline also lands them on
`strict`. Three genuinely do not apply and should not. All nine also
open with two blank lines, which the ladder already tolerates.

Nothing normalizes on the way: `run_stdin_capture` passes the text
verbatim to stdin and `DiffProposal(diff=content, ...)` (vllm.py:476)
does not touch it.

Contract: a missing final newline, and a single enclosing markdown
fence, are packaging and not content; the ladder judges the diff inside
them. Direction: **LOOSENED**, and this is the proof the loosening rule
requires -- the contract as written rejects six legitimate inputs, which
is a claim that the contract is wrong, not that something is failing
against it. The compensating tightening is not "unrepresentable"
(`7282e1e`'s mistake): nothing here weakens what the ladder does once
the diff is unwrapped, and a fence with anything outside it, or two
fences, is still not a diff.

Known-good: each of the six draws named above applies after the change,
from the bytes the server returned, with no other edit. Known-bad: a
response that is prose with a fenced diff embedded in it, and a response
with two fenced blocks, are both still rejected by the precheck -- the
allowance is one fence that encloses the whole content, nothing less.
Fixtures come from `../saddle-bench/runs/round3e-probe/*.json`; do not
hand-write them.

Contract mutants (each target occurs once; abort if `grep -c` is not 1;
drop `__pycache__` after each revert):
1. Strip the fence but not the trailing newline -> the four
   newline-only draws stay red on `corrupt patch`.
2. Accept a fence anywhere rather than one enclosing the whole content
   -> the prose-with-embedded-fence known-bad passes.
3. Append the newline unconditionally, including to a diff that already
   ends with one -> a grammar-on draw that applied on `strict` before
   must still apply (this mutant should be *survivable*; if it is, say
   so and keep the guard for shape, not for behaviour).

**Vacuity note, stated honestly.** With `DIFF_GRAMMAR` on, the worker's
output always ends with `\n` and never carries a fence (8/8 and 0/9 in
F21.18), so this cannot fire in the production arm today. It is not
dead: it is the precondition for T6-43 touching the grammar at all, and
it is what the ladder owes if `--worker-effort` or a future model ever
emits without one. Say this in the commit rather than implying the item
fixes a live failure.

Owner: main session; after T6-47, before any T6-43 grammar change.

### T6-44 — A requirement states a behaviour, and a gate detail names no ratio (tightened)

Files: `src/saddle/cli.py` (the worker rules block, ~449),
`src/saddle/gates.py` (`check_coverage`, `check_mutation` details),
`tests/test_cli.py`, `tests/test_gates.py`.

Goal G1: "State the behaviour, never the number." Two strings carry
numbers to the worker. The standing rule is `- Every changed line must
be executed by the new tests.` (cli.py:449, the prompt's only use of
"executed"), and the coverage verdict is
`f"{percent:.1f}% < {minimum:.1f}%: uncovered {gaps}"` (gates.py:339),
routed to the worker since T6-31.

Round 3e n2 attempt 1 is the evidence, and it is narrower than it first
looked. That attempt carried no failure brief, so the worker never saw a
percentage; the 47,586 characters of reasoning name no gate, no metric
and none of the emitted helpers, and end `Let me finalize and write the
diff.` The implementation is correct and terminates properly on its
`__all__` line. What follows it is a docstring that paraphrases the
standing rule -- "Placeholder to satisfy the requirement that every
changed line is executed by tests" -- and then a second, `Ensure all
code paths in this module are reachable from tests`. So the rule, not
the verdict, is what the emission read literally. Both are in scope: the
verdict is what a *retry* reads, and T6-31 now delivers it.

The rewrite is not a rewording of execution. Execution is a proxy under
any phrasing and `_noop()` satisfies it; the non-gameable form is
mutation -- a line is exercised when a test fails if its behaviour
changes, and `pass` admits no mutant. So the rule states that, and the
coverage detail names the statement it is about and drops the ratio.

Contract: the coverage gate's `detail` -- the field
`format_attempt_failure` routes to the worker -- names the lines no test
runs and carries no ratio, and the standing rule states the mutation
form. `basis` is sealed evidence rather than worker-facing text and
keeps its counts. Direction: tightened. Known-good: a coverage failure
whose detail names `node.py:3, node.py:4` and no percentage. Known-bad:
a detail carrying `0.0% < 100.0%` reaches the worker, and the standing
rule asking for lines to be executed. Mutant: restore the percentage
into the detail -> the known-bad red.

**Scope narrowed on evidence, 2026-09-21, and here is what that admits.**
The first draft of this item said *no* worker-facing string carries a
ratio, which would also strip `14.3% < 85.0%: survived 6: ...` from the
mutation gate. That is now deliberately left in place, and the admitted
known-bad is exactly that string still reaching the worker. The reason
is the goal's own test -- "only the second is satisfiable by a no-op".
Coverage is: `_noop()` raises it. Mutation is not: a `pass` body admits
no mutant, so filler cannot move the number. And round 3d is measured
evidence that the mutation number produces the right behaviour, where
n2's two attempts went 75.3% -> 78.3% by writing real tests, while the
coverage percentage in round 3e produced the 1334-line file. Strip the
one that rewards filler; keep the one the worker has been seen using
honestly. If a later round shows a draw inflating mutation score by
adding mutable-but-meaningless lines, this narrowing is wrong and the
item reopens. Owner: main session;
owed before round 3f, because the brief reaches the worker on every
retry. Pairs with T6-46 (the refusal path), which is the other half of
Goal G1's instruction on wording: a rule the worker cannot satisfy
honestly needs somewhere to go that is not a fabricated satisfaction.

---

### T6-45 — The sealed server version is one saddle actually asked for (tightened)

Files: `src/saddle/vllm.py` (`server_version`), `tests/test_vllm.py`.

Found 2026-09-21 against the live server. `DEFAULT_BASE_URL` is
`http://127.0.0.1:18020/v1`, and `server_version` issues
`self._client.get("/version")` against it, which resolves to
`/v1/version`. vLLM serves its version at the **root**, not under `/v1`:

| request | authenticated | keyless |
| --- | --- | --- |
| `/version` | 200 `{"version":"0.28.0"}` | 200 |
| `/v1/version` | 404 `{"detail":"Not Found"}` | 401 |

`served_version` then catches `VllmRequestError` and returns `"unknown"`
(cli.py:624), whose docstring reads "`unknown` when it will not say".
The server says. Saddle asks the wrong URL and records the answer as a
refusal, so T6-27's sealed version can never be anything but `unknown`
with the default base URL, and the swallow makes that look deliberate.
This is the T6-37 failure shape exactly: a setting sealed as a value
nobody obtained.

Contract: the version request is made against the server root, and a
version the server does supply is sealed verbatim. Direction: tightened.
Known-good: a client whose base URL ends `/v1` seals `0.28.0`.
Known-bad: the same client seals `unknown` while the server answers 200
at its root. Mutant: restore the `/v1`-relative path -> the known-good
red. Also worth deciding, not assumed: whether `served_version` should
keep swallowing at all, since a 404 and a server that has no version
endpoint are different facts and only one is `unknown`. Owner: main
session; small, and independent of round 3f.

**Discriminator settled (2026-09-21). T6-46 stays unimplemented, now
for a reason rather than a deferral.**

The item's own candidate answer does not survive its own vacuity rule.
"A refusal is admissible when the node's own tests are red" is true of
every impl node at attempt 1 *by construction* -- a red suite before the
change is the red-phase gate's premise, not evidence about the
requirement. That condition admits every node on its first attempt, so
it makes refusal the cheapest move available, which is the failure the
open question was asking about.

The discriminator that does have teeth: **a refusal is only admissible
alongside the work.** It carves out one named requirement; it is never a
way out of the node. Concretely, in `keep_candidate`'s image
(`survivors.py:308`), which drops a candidate that passes against a
counterfactual tree: re-run the node's gates with the refused
requirement removed from the binding gate's population, and admit the
refusal only if **every other gate passes unchanged** and the requirement
the worker named is one the plan assigned to this node. If the rest of
the node cannot seal, there is no refusal -- it is an ordinary failed
attempt, recorded as one.

Three things follow. First, refusing is now strictly *more* expensive
than implementing whenever implementation is possible, and cheaper only
when the requirement genuinely cannot be implemented -- which is the
case the goal wants admitted. Second, it is not a third attempt outcome
at all: the node seals, with the refusal attached to its record, so "a
refusal is not counted as a failed attempt" falls out rather than being
granted -- there is nothing left to retry. Third, it composes with Goal
G1 instead of fighting it: a refused requirement predicts that the
oracle fails on exactly that requirement, for the reason the gate detail
already gave, which is G1's definition of **agreement**, not a
disagreement to be explained away.

And the decision immediately disqualifies the only case offered for the
loosening. Round 3e's n2 attempt 1 could not have refused: it failed
`ruff` and `coverage`, so it did not seal the rest of the node, and
under this discriminator no refusal would have been recorded. F21.19
sharpens the same point from the other side -- that draw was not aiming
at a gate at all, so a refusal channel is not what it needed.

So T6-46 has a settled discriminator and **still has no admissible
example**, which is exactly what the loosening rule asks for before it
lands: proof that the contract rejects a legitimate input. Until a run
produces a node that seals every requirement but one and names that one,
implementing the outcome would be buying a mechanism with no case. Owner
unchanged; the trigger is now specific -- the first run in which a node
seals every requirement but one.

**Round 3g checked against this trigger and does NOT meet it
(2026-09-21, F21.21).** `n2.r1` attempt 1 failed exactly one gate, which
looks like the trigger and is not it. The discriminator admits a refusal
when a **requirement** cannot be implemented; REQ-002 *was* implemented,
correctly — the restored tree passes 16 of 16 hidden accounts+fees
tests. What could not be satisfied was the `coverage` gate, and a
refusal channel is the wrong repair for that: the node had nothing to
refuse. T6-53 is the right item. T6-46 still has no admissible example
and stays unimplemented for the same stated reason.

---

### T6-46 — A node may refuse a requirement instead of faking it (loosening, with evidence)

Files: `src/saddle/cli.py` (the worker rules block and the node brief),
`src/saddle/slice.py` (attempt outcome), `tests/test_slice.py`.

Goal G1, verbatim: "Give an honest way out, and say it is not penalised:
if a requirement cannot be satisfied without code that exists only to
satisfy a gate, say so and stop. A recorded refusal naming the
requirement beats a passing artifact that does not implement it, and is
not a failed attempt. Much gaming comes from failure being unavailable:
in round 3e the worker's options were produce something, or burn the
attempt."

That reading is supported by the round-3e draw. The attempt had no
failure brief and its reasoning names no gate, so nothing in it was
aiming at a metric; what it did was satisfy a rule it could not satisfy
honestly, in the only currency it had. The worker had no way to say
"this rule asks for something that is not an implementation".

Contract: an attempt may end in a third outcome beside sealed and
failed -- `refused`, carrying the requirement id and the worker's
sentence -- and a refusal is not counted as a failed attempt against
the retry budget. Direction: loosening of "an attempt either seals or
fails", and it needs the loosening rule's proof before it lands: a
recorded case where refusing was the correct move. Round 3e's n2 attempt
1 is a candidate and not yet proof, because the honest move there was to
write the implementation and stop at `__all__`, which is what the first
part of the emission actually did.

Open question to settle before implementing, not after: a refusal the
harness cannot verify is a cheaper exit than the work, and T6-45's
mechanism-vacuity rule applies -- what stops every hard node from
refusing? Candidate answer: a refusal is only admissible when the node's
own tests are red and the worker names a requirement id the plan
assigned to it, so it is a claim about the spec that a human reads, not
a way to pass. Owner: main session, after T6-44. Do not implement the
outcome before the discriminator is decided.

Status 2026-09-21: **still blocked, and the candidate discriminator is
now falsified, not merely unproven.** The complete impl-node record for
rounds 3c-3g is assembled in `../saddle-bench/runs/IMPL-NODE-TABLE.md`
(six nodes reached a gate; F21.26). Taken one at a time, refusing would
have been the correct move in **none** of them:

| node | failure | would refusing have been right? |
|---|---|---|
| 3c n2 | `coverage` 88.9%/89.7%, `mutation` tool death | no — its own tests pass and the artifact largely works |
| 3c n2.r2 | pre-T6-3 `ruff`, `mutation` tool death | no — `coverage: 100.0%`; both failures are the harness's, not the spec's |
| 3d n2 | `coverage` 98.5% (one line), `mutation` 78.3% | no — one uncovered line is not an unsatisfiable requirement |
| 3e n2 | `ruff` UP031 ×4, `coverage` 71.4% | not yet — the honest move was to implement and stop, which the emission's first part did (unchanged from the paragraph above) |
| 3g n2 | own tests fail, `red-phase`, `property-coverage`, `mutation` death | no — the tests are red because the implementation is wrong |
| 3g n2.r1 | `coverage` only, on lines only a later test node can cover | no — the gate was wrong (F21.21) and the remedy was T6-53's deferral; a refusal would have recorded a false claim about the spec and hidden the defect |

Now apply the candidate discriminator — admissible only when the node's
own tests are red and the worker names a requirement id the plan
assigned to it. Of the six, exactly one has red tests at its gate: **3g
n2**, the one node whose artifact was genuinely broken. The
discriminator therefore admits precisely the case where refusing is
wrong and excludes all five where the node had a real grievance. It is
not a filter on honesty; it is a filter on brokenness, and it points the
wrong way.

So T6-46 needs either (a) a different discriminator, or (b) closure. Do
not implement it on the current one. A case that would qualify has a
recognisable shape and has not occurred: a node whose gate demands
something the task prompt does not require and no later node can supply,
where the worker says so and stops. 3g n2.r1 is the nearest miss and
fails it on the last clause — a later node could supply it, which is why
T6-53 was the right answer there.

---

### T6-47 — An attempt records the reasoning effort it ran at (tightened)

Files: `src/saddle/vllm.py` (`propose_diff`'s retained record),
`src/saddle/slice.py` (the sidecar), `tests/test_slice.py`,
`tests/test_vllm.py`, `docs/ARCHITECTURE.md`.

Status 2026-09-21: DONE. `DiffProposal` gained `reasoning_effort` (empty
by default, excluded from equality like the other T6-27 fields);
`propose_diff` fills it from **`payload["reasoning_effort"]`** rather
than from its own argument, on both the success path and the
`exc.evidence` path, so the record is read back from the request that
was actually sent and cannot drift from it; `_proposal_evidence` emits
it, and `_error_evidence` inherits it through `_call_evidence`.
ARCHITECTURE.md's sidecar sentence now names the call it retains
(prompt, seed, temperature, effort) -- it named none of them before, so
T6-27's fields are documented in the same touch.

Deviation from the item, and it is a weakening of the promise, stated
because the item's own wording overpromised. The contract written here
is **fidelity**, not replay: "a replay built from the sidecar bills the
attempt's own `prompt_tokens`". It is no longer "reproduces the draw",
because F21.18 measured this deployment returning six distinct outputs
for six byte-identical requests (10 235-40 358 characters at one
payload, three per grammar arm). No field added to any record can buy a
reproduction here. What it buys is a cell that varies the variable it
means to vary, which is exactly what round 3e's grammar cell could not
do.

One case left deliberately empty: the survivor/test-node union at
slice.py:1506 constructs a `DiffProposal` with no request behind it, and
records `""`. That is the honest value -- there was no effort, because
there was no draw.

Known-good: a proposal's retained effort equals the `reasoning_effort`
of the payload the client posted, at two values neither of which is
`DEFAULT_REASONING_EFFORT` (one value would pass against a hardcoded
constant); a failed call carries it on `exc.evidence` for both the
transport and the truncation path, which matters because the draw most
worth replaying is often the one that failed -- round 3e's zero-content
emission is a `VllmResponseError`; and a sealed attempt's sidecar names
`low`, from which a replay keyed on the record draws the arm the attempt
drew while a guess of `xhigh` draws a different one. Known-bad: the
sidecar as it stood, from which the only way to pick an effort was to
guess -- and F21.18 records what the guess cost.

Direction: TIGHTENED (a field added to the seal; nothing loosened).

Contract mutants, each with a verified target count of 1, all KILLED:
M1 `_proposal_evidence` drops the `reasoning_effort` key (the slice
known-good red); M2 `propose_diff` records `DEFAULT_REASONING_EFFORT`
instead of `payload["reasoning_effort"]` (both parametrized vllm cases
red -- this is the mutant the single-value version of that test would
have survived); M3 the `exc.evidence` path drops the key (the failed-call
known-good red).
`./check.sh` green: 824 passed, 3 skipped, 100% line+branch.


The sidecar retains prompt, seed, temperature and `max_tokens`. It does
not retain `reasoning_effort`, and that field changes both the request
and the emission, so no attempt can be replayed from its own record.

Found by refutation, which is worth keeping in the item. The grammar
cell's replay billed 10 627 prompt tokens against the attempt's 10 615,
and the first reading here was that the retained prompt was not the sent
bytes. Measured against the live server, resending that exact retained
string at each effort:

| `reasoning_effort` | `prompt_tokens` |
| --- | --- |
| low | **10 615** -- the original attempt |
| medium | 10 585 |
| xhigh | 10 627 -- the replay |

(`zero` and `high` are rejected by this server; the accepted set is
`low`, `medium`, `xhigh`.) So the retained prompt is byte-exact and the
earlier reading was wrong. The effort is the uncontrolled variable: the
cell replayed a `low` attempt at `xhigh`, which is why reasoning ran
14 100 tokens originally and 38 030 to 78 864 on replay.

Contract: an attempt's sidecar records the `reasoning_effort` of the
request, and a replay built from the sidecar bills the attempt's own
`prompt_tokens`. Direction: tightened. Known-good: a recorded attempt
resent from its own sidecar bills its own `prompt_tokens`. Known-bad:
the current sidecar, from which the only way to pick an effort is to
guess. Mutant: drop the field from the retained record -> the known-good
red. Owner: main session; blocks T6-16 and any re-ask of T6-43, because
neither is interpretable while the replay cannot reproduce its baseline.

**Second finding, was a hypothesis, now partly measured (F21.18).**
That node ran at `low`. The planner's own instruction (cli.py, `reasoning_budget`) is to
"reserve medium/xhigh for complex algorithmic nodes", and this node
wrote three modules with per-currency rounding. At `low` it degenerated
in 3 of 3 draws (F21.18 corrects the 2 of 3 this item first recorded);
at `xhigh` it did not degenerate in 6.

That contrast was uncontrolled -- different n, one prompt, no
repetition, and confounded with the grammar, since every round 3e worker
draw had the grammar on. F21.18 removed the grammar confound by holding
`low` and varying the grammar alone over 12 more draws: the grammar-on
arm degenerates 4 of 6 and the grammar-off arm 0 of 6, so the `low` arm
of the effort contrast was really *low + grammar*. Effort itself is
still untested as a lever -- no cell has varied effort with the grammar
held off -- so the sentence that stands is narrower: the next cell that
wants to blame effort must run that arm. Note also that the planner
chose `low` for this node, so if effort matters the lever may be the
planner's budget assignment, not the sampler.

One thing T6-47 can no longer promise. F21.18 showed this deployment
returns six distinct outputs for six byte-identical requests, so
recording `reasoning_effort` buys a cell that is *faithful*, never one
that is a *reproduction*. Write the contract as fidelity (a replay built
from the sidecar bills the attempt's own `prompt_tokens`) and do not
claim replay.

---

### T6-39 — `_best_of_samples` scores candidates as they arrive (wall)
Files: `src/saddle/slice.py`. `list(pool.map(...))` blocks on the
slowest draw before scoring the first; `Executor.map` already yields in
seed order, so scoring while later draws are still in flight keeps
determinism and removes the wait. Cost in round 3e: ~9 min on one
attempt. Contract: the seal is unchanged (first pass in seed order).
Owner: main session; small; after T6-38.

### T6-40 — A repair brief names only files the retry's tree has (verify first)
Session 42 read attempt 2's brief as citing `money.py:28` after the
tree had lost `money.py`. The code says a retry lands on the previous
attempt's tree (`applied` is not restored between attempts), so the file
should have been there; the `money.py: No such file` came from the
executor's own `--recount` reproduction, not from saddle's rungs, which
all reported `fees.py`. Verify from the sidecar before treating this as
a defect; if the brief and the tree can disagree, the brief must be
built from the tree the retry applies to. Owner: main session; after
T6-38.

### T6-35 — Round 3e: one T5 seed under T6-29c (measurement; F21.16; no code)

Same procedure as T6-26 (`RUN_DIR=runs/round3e/t5-s1 SAMPLE_TEMP=0.7
DEADLINE=7200 ./run_arm.sh t5 saddle`, PREDICTION.md before the run),
with saddle at `85ef057` or later. `--survivor-effort low` and
`--survivor-samples 10` are the defaults, so `run_arm.sh` needs no
change; the run span's settings record both. Before the run, one
bench-side fix from T6-28's preflight: `metrics_sample.py` declares
`vllm:prefix_cache_queries` and `vllm:prefix_cache_hits` where this
server exposes them with a `_total` suffix (17/19 names matched); spell
them as the server does, so the preflight reads 19/19 and the report has
the prefix-cache hit rate. P1-P3 as T6-19. P4 (the first impl node
seals) held in round 3d for n1. The live questions are T6-29c's: P6, the
first impl node that fails only coverage or mutation gets a
`survivor-tests` span rather than a retry; P7, at least one of its ten
draws is kept (kills a survivor or covers a gap line); P8, the impl node
then seals behind the spliced test node, and the run's proven count is
higher than round 3d's one. Pre-registered observations: (a) the
survivor draws' walls and finish reasons at effort `low` under the 6000
token cap (T6-29a's reasoning-off draws were 1/10 truncated; does `low`
fit the cap?); (b) how many draws parse (T6-29a: 6/10) and how many are
dropped as red on the real tree (T6-29a: 3/10 plausible-but-wrong); (c)
whether a kept candidate's test node passes its own gate (red on the
baseline, property present, ruff clean) or the recovery burns on the
gate; (d) `saddle explain` on every failed attempt, quoted in the
finding (T6-1's reasoning read starts here). Known-bad worth stating:
if no node reaches a coverage or mutation miss, P6-P8 are unresolved,
not falsified. Owner: executor (needs the key and the container up).
Not in this item: any fix; a FALSIFIED P6-P8 is a main-session item.

**Status (2026-09-20):** DONE by session 42 (bench `b77ea92` preflight
fix, `ff88372` PREDICTION.md before the run, `caa80a3` + `217b90e`
F21.16). `EXIT=1 WALL=2393`, oracle FAIL. P1 HELD (12/12 `stop`), P2
FALSIFIED (`sidecar-diff-hash` x3: the sidecar scrub capped `diff` at
4000 chars, so every large attempt failed T6-27's own check, the run
aborted on its journal and `saddle explain` refused it -- T6-36, fixed
in the commit below), P3 HELD, P4 HELD for n1 (a test node; the first
impl node did not seal), P5 FALSIFIED, P6/P7/P8 UNRESOLVED: n2's failure
set was `{ruff, coverage}` on attempts 1 and 3 (4x UP031 each, on
`%`-formatting the baseline itself uses in 4 of 5 modules), and `ruff`
is not in `SURVIVOR_GATES`, so the survivor round never became
eligible; attempt 2 died at apply (`fees.py: patch does not apply` at
all four rungs: two blank lines misplaced in a whole-file hunk, which
`--ignore-whitespace` cannot touch -- T6-38). The one number that
moved: **the mutation gate passed, 88.8% over 80 mutants**, against
75.3/78.3% in round 3d and 82.0% re-scored (T6-33a). Verified by the
main session against the bench: the four commits, F21.16, the three
sidecars at exactly 4024 characters with nested samples up to 186 264,
the worker spans' details. Observation (d) blocked by the same defect.
Where UP031 came from: no config file in the workdir or any ancestor,
no user config, no env -- ruff 0.16.7's own default rule set includes
UP, DTZ, YTT (`ruff check --show-settings` in the workdir), so the ruff
gate's verdict is a function of the installed ruff version (T6-37).

### T6-49 — Round 3f: one T5 seed under T6-36..T6-48 and T6-34, and the first of Goal G1's three seeds (measurement; no code) — DONE 2026-09-21

Same procedure as T6-35 (`RUN_DIR=runs/round3f/t5-s1 SAMPLE_TEMP=0.7
DEADLINE=7200 ./run_arm.sh t5 saddle`, PREDICTION.md committed before
the run), with saddle at the T6-34 commit or later. Everything between
round 3e and this run is committed: T6-36 (the sidecar scrub), T6-37
(saddle's own ruff rule set, sealed), T6-38 (the blank-lines apply
rung), T6-41 (the dead-code gate), T6-42 (public deletions), T6-44
(behaviour, not ratios, in every string the worker reads), T6-45 (the
sealed server version), T6-47 (the effort on the record), T6-48 (the
apply ladder forgives packaging), T6-34 (the attempt refs).

**`--worker-effort` stays unset.** `run_arm.sh:66` passes
`--reasoning-effort xhigh`, which is planner and replan only; the worker
falls through `cli.py:677` to the plan's per-node `reasoning_budget`,
which the planner sets to `low`. That is what rounds 3b-3e ran, and
round 3f exists to count agreement under a stack that changed for other
reasons -- raising the worker effort here would change two things at
once and make the comparison with 3e unreadable. It also costs: F21.18
timed `low` at 149-296 s a draw against `xhigh` at 721-1517 s, and
`PROPOSAL_SAMPLES` draws on four nodes with retries does not fit
DEADLINE=7200 at the high end. What this leaves open, stated so it is
not mistaken for a result: **effort remains untested as a lever.** No
cell has varied it with the grammar on and everything else held
(F21.18's `low`/`xhigh` cells were confounded with the grammar). That
is its own measurement, not this one.

Pre-registered predictions. P1: every worker call finishes `stop`
(12/12 in 3e). P2: the journal verifies and `saddle explain` runs on
every attempt -- 3e's P2 was falsified by the 4000-character scrub cap
that T6-36 fixed. P3: no gate verdict depends on a rule saddle did not
select; `ruff=` and `ruff_rules=` are on the run span, and UP031 cannot
fire from the pinned set (T6-37). P4: no attempt fails at apply for
blank-line drift (T6-38) or for packaging -- a missing final newline or
one enclosing fence (T6-48); an apply failure for any other reason is
not a falsification. P5: every attempt that reaches a gate leaves
`refs/saddle/attempt/<node-slug>/<n>` and its sidecar names that tree
(T6-34) -- a known-good on live data, and its failure is a harness
defect, not a worker one. P6: the survivor round becomes eligible on at
least one impl node (3e's blocker was `ruff`, which is not in
`SURVIVOR_GATES`; the pinned rule set removes the UP031 that kept it
red). P7: at least one survivor draw is kept. P8: the impl node then
seals behind the spliced test node and the proven count beats round
3d's one.

P9 is Goal G1 and is the reason for the run: **the run's gate verdicts
and `oracles/oracle_t5.py` agree.** Either every node seals and the
oracle passes, or a named node fails and the oracle fails for a reason
that node's gate detail already gave. The oracle is never a gate and
the worker never sees it. A run where every gate passes and the oracle
fails is a gate defect and resets G1's count to zero; three consecutive
agreeing seeds at temperature 0.7 is the floor, and this is seed one.

Observations, pre-registered but not predictions: (a) `saddle explain`
on every failed attempt, quoted in the finding (T6-1's reasoning read);
(b) the reasoning effort each attempt ran at, read from the sidecar --
the first round where it is on the record at all (T6-47); (c) whether
any draw repeats a definition, and what the dead-code and
public-deletions gates said about it (T6-41, T6-42, T6-43's population);
(d) wall per draw and the metrics report's prefix-cache hit rate at the
node budget, as the baseline the effort cell will be compared against.

Do not `git gc` the run worktree: T6-34's refs are what makes a failed
attempt's tree re-scorable without `lost-found` (F21.15). No verdict
from a summary line -- read the reasoning whole, read `samples[i].diff`
whole and count repeats, reproduce any disputed gate with saddle's own
functions on a restored copy, and say which steps were done and which
were not. Owner: executor (needs the key and the container up). Not in
this item: any fix. A falsified P2-P8, or a P9 disagreement, is a
main-session item.

**DONE 2026-09-21 -- P9 DISAGREEMENT; Goal G1's count stays at zero.
F21.20.** `EXIT=1 WALL=1810`, 0 proven, 1 failed, 6 undispatched, oracle
FAIL. Neither `n1` nor its replacement `n1.r1` passed
`requirement-binding` on any of its three attempts; every other gate
passed every attempt, and no impl node ran, so the survivor round was
still not reached. P1-P5 all held -- including T6-34's refs on live data
(5 gated attempts, 5 refs, every sidecar `tree` equal to `<ref>^{tree}`,
the attempt that never applied carrying `tree=None` and no ref) and
T6-47's effort on the record. P6-P8 unresolved. P10 half-held: the
degenerate draw appeared at attempt 1 exactly as predicted, `dead-code`
did not name it, and ruff's `F811` did -- fifteen errors, one per
shadowed copy -- so the sampler discarded it and promoted the coherent
candidate. T6-25, T6-37 and T6-48's ladder all did their jobs on live
data.

The disagreement is narrow and names its own repair: the gate's reason
("the test file does not quote the planner's example text") is not the
oracle's reason (multi-currency is not implemented). Two items follow,
T6-50 (the blocker) and T6-51 (the evidence the next round needs). No
third: the one thing left uncovered, duplicated test bodies under
distinct names, has no instance in this run and so has no known-bad.

### T6-50 — A requirement example is satisfiable by the test that asserts the behaviour (loosened for calls, tightened against quoting; with proof) — DONE 2026-09-21

Files: `src/saddle/gates.py` (`_asserted_literals`,
`check_requirement_binding`), `tests/test_gates.py`, a fixture cut from
round 3f's `n1` attempt 3.

**Proof the contract is wrong, not merely failing** (F21.20). The
planner writes REQ-001's examples as call expressions -- `deposit('10.00',
'USD')`, `deposit('10.00', 'USX')` -- and `_asserted_literals` collects
`ast.Constant` only. A call expression is not a constant, so no test can
put that text in the set. Reproduced with saddle's own functions on the
tree restored from `refs/saddle/attempt/n1/3`: 61 asserted literals,
none containing `deposit(`, and `check_requirement_binding` returning
the run's own detail byte for byte. Unsatisfiable for three independent
reasons: a behavioural test contributes only the constants `10.00` and
`USD`; the one source that does satisfy it quotes the example and
asserts nothing, which Goal G1 declares inadmissible; and `autofix` runs
`ruff format --isolated` first, normalising `'10.00'` to `"10.00"` and
changing the literal before the gate sees it. This is the
`^diff --git ` episode in a second component -- a constraint whose tests
pinned its presence while it made the correct artifact unrepresentable.

Contract after: an example that **parses as a call expression** is
satisfied when some `test*` function both calls that callee and asserts
on every constant argument of the example, compared by value. An example
that parses as anything else keeps today's rule -- T6-4's own tests use
constant examples and must stay green, which is a known-good this change
may not move.

Direction, both halves, in the commit message: **loosened** for
call-shaped examples, which become satisfiable at all; **tightened**
against the escape, because a test that quotes the example text as a
string and asserts nothing no longer satisfies it -- it calls nothing
and the constants are not in its asserted set. Comparing constants by
value is also what removes `ruff format` from the verdict.

Known-good: `assert acc.deposit("10.00", "USD") == Decimal("10.00")`
satisfies `deposit('10.00', 'USD')`, under either quote style; and every
existing constant-shaped example still passes.
Known-bad: (a) `assert "deposit('10.00', 'USD')" == "deposit('10.00',
'USD')"`, today's only satisfying input, now fails; (b) a test that
asserts `10.00` and `USD` but never calls `deposit`; (c) a test that
calls `deposit` but asserts neither constant; (d) a suite with no
`test*` function.
Vacuity: the fixture from `n1` attempt 3 must fail before the change and
pass after only if it actually asserts the behaviour -- if the same
fixture passes both ways the rule is not deciding anything.

Owner: main session. Precondition for round 3g: no T5 seed can reach an
impl node until this lands, because both plans die on their first test
node.

**DONE 2026-09-21 (`b5a0e04`).** Implemented as written, plus one thing
the item did not anticipate: the operation is matched on its final name,
so an example naming a receiver binds to the same test. Discrimination
on live data rather than a fixture -- the tree of
`refs/saddle/attempt/n1/3`, the one round 3f rejected, now passes both
requirements' example clauses, and its only remaining failure is the
citation clause, because the tests never mention REQ-002. That is a
different and correct verdict the worker can act on. Four contract
mutants, all killed. ./check.sh 836 passed, 100%, exit 0.

### T6-51 — An attempt's reasoning is in its sidecar, whole (loosening, with proof) — DONE 2026-09-21

Files: `src/saddle/journal.py` (`_RETAINED_WHOLE`),
`tests/test_journal.py`.

**Proof the contract is wrong** (F21.20). T6-36 fixed a real defect --
the one-level scrub capped a 4001+ character `diff` so it no longer
hashed to its `diff_hash`, every large attempt failed verification and
`saddle explain` refused the journal -- and the fix made the scrub
recursive. The cap then reached `samples[i].thinking`, where nearly all
of a sidecar's reasoning lives: round 3f lost 31 911, 58 927 and 45 345
characters, every nested `thinking` cut to 4024. Goal G1's verification
procedure opens with "read the model's reasoning first
(`samples[i].thinking`, whole)", which is now impossible from the
record; F21.19 could be written only because round 3e predates T6-36.

Scope is small and is the reason this is safe: `_scrub_evidence` has
exactly one caller, `write_attempt_sidecar` (`journal.py:316`). The
journal's own entries cap thinking by a different path
(`scrub_thinking`, `journal.py:166`) and are untouched. So adding
`thinking` to `_RETAINED_WHOLE` moves the sidecar alone -- `proofs.jsonl`
and `saddle tail` stay small, which is what the cap is for.

Direction: **loosened**, for the sidecar only.

Known-good: a sidecar whose nested `samples[i].thinking` exceeds
`MAX_THINKING_CHARS` keeps every character, and the same text in a
journal entry is still capped. Redaction is unchanged: a key planted in
nested `thinking` must still not appear, which is the half that may not
be lost when the cap goes.
Known-bad: T6-36's own tests stay green -- a 4001+ character `diff`
still hashes to its `diff_hash`, and a nested string under any other key
is still capped.
Vacuity: the same test must show the cap is still capable of applying,
by carrying a nested non-`thinking` string of the same length that is
truncated in the same sidecar. Otherwise the test cannot tell "retained"
from "nothing was long enough".

Owner: main session, before round 3g -- the next run is the one whose
reasoning has to be readable.

**DONE 2026-09-21 (`04a2503`).** One line of contract, three contract
mutants, all killed. The flip is labelled in the commit and what the old
assertion was protecting -- that the scrub descends and redacts -- is
carried in the same test by a nested non-retained string of the same
length, still truncated. ./check.sh 833 passed, 100%, exit 0.

### T6-52 — Round 3g: the first seed that can reach an impl node, and Goal G1 seed 1 of 3 (measurement; no code) — DONE 2026-09-21

Same procedure as T6-49 (`RUN_DIR=runs/round3g/t5-s1 SAMPLE_TEMP=0.7
DEADLINE=7200 ./run_arm.sh t5 saddle`, PREDICTION.md committed before
the run), with saddle at the T6-50 commit or later. `--worker-effort`
stays unset for the same reason as in T6-49: the node budget is the
baseline the effort cell will be compared against, and changing two
things at once forfeits both answers.

Round 3f never reached an impl node -- both plans died on their first
test node, on a clause no test could satisfy -- so P6, P7 and P8 are
still unanswered and this is the first run that can answer them.

Predictions, in the order they would be falsified:

Q1. Every worker call ends `finish_reason=stop` (held in 3e and 3f).

Q2. The journal verifies and `saddle explain` renders every attempt.

Q3. T6-51's first live use: no nested `samples[i].thinking` carries a
`[truncated` marker, and the reasoning is readable whole. If this fails,
every reasoning-dependent verdict in the finding is void and says so.

Q4. T6-50's first live use: no node fails `requirement-binding` on a
call-shaped example whose operation its tests perform and whose values
they assert. A binding failure, if one happens, names which half is
missing, and that half is true of the tree.

Q5. T6-34's refs again: one ref per gated attempt, each sidecar `tree`
equal to `<ref>^{tree}`, and `tree=None` with no ref for an attempt that
never applied (held exactly in 3f).

Q6. An impl node is dispatched. This is the weakest prediction in the
set -- it is the thing two rounds have failed to do -- and it is stated
so that failing it is visible rather than absorbed.

Q7. The survivor round runs at least once, so T6-29a's 0/10 gets its
second reading.

Q8. Goal G1: the run's gate verdicts and `oracles/oracle_t5.py` agree --
either every node seals and the oracle passes, or a named node fails and
the oracle fails for a reason that node's gate detail already gave. This
is seed 1 of the three consecutive seeds; a disagreement resets to zero
and is recorded as a gate defect, not as partial progress.

Observations, pre-registered but not predictions: (a) `saddle explain`
on every failed attempt, quoted; (b) whether any draw repeats a
definition and what named it -- round 3f's answer was ruff's `F811`, not
`dead-code`, which does not run on test nodes; (c) wall per draw and the
prefix-cache hit rate at the node budget, against 3f's 59-739 s and
0.45-0.78; (d) whether a node seals every requirement but one, which is
the trigger T6-46 is waiting for.

Do not `git gc` the run worktree. No verdict from a summary line: a
per-sample `outcome` in a sidecar is a **count** of failed gates with no
names, and reading one as though it named them is what produced the
wrong first reading of round 3f (F21.20's correction). Reproduce the
gate with saddle's own functions on the tree its ref names, and say
which steps were done and which were not. Owner: main session or
executor (needs the key and the container up). Not in this item: any
fix. A falsified Q3-Q7, or a Q8 disagreement, is a main-session item.

**DONE 2026-09-21 — Q8 DISAGREEMENT; G1's count stays at zero. F21.21.**
`EXIT=1 WALL=6406`, 1 proven, 1 failed, 2 undispatched, oracle FAIL.
**Not** a first on either count, contrary to this entry's first draft:
round 3c proved two nodes (`n1` and `n2.r1`) and 3d proved `n1`, and 3e
reached `n2`. Only 3f died before an impl node, so Q6's pre-registered
premise was wrong, and on proven count 3g is behind 3c. A second
correction: 3c's `n2.r1` is a **test** node, not an impl one — 3c
replanned its failed impl node into a test node and that is what sealed.
Read from the plan record, not the node id. Across every round to date
**no impl node has ever sealed** (F21.22).
Q2–Q6 held; **Q3 and Q4 are T6-51's and T6-50's
first live uses and both held** — no nested `thinking` truncated
(115 239 characters retained at most) and `n1` sealed with
`requirement-binding: PASS (1 requirement(s) bound, 4 example(s)
asserted)`. Q1 falsified: two calls ended `length`, two ended `stop`
with no text content. Q7 unresolved.

The disagreement is the opposite of round 3f's. `n2.r1` attempt 1 failed
exactly one gate, `coverage`, on eleven lines; restored from its attempt
ref that tree passes **16 of 16** hidden accounts+fees tests, while the
tree the run kept fails the hidden suite at collection. The gate
rejected the correct artifact. It is not broken — reproduced to the same
eleven lines with saddle's own arithmetic — but its demand is outside
the graded node's power. Items T6-53 and T6-54 follow.

### T6-53 — The coverage gate may not demand what the graded node cannot supply (design decision, then a contract change)

Files: `src/saddle/gates.py` (`check_changed_line_coverage`, and what
fills `changed`), `src/saddle/runner.py`, `docs/DESIGN-NOTES.md`.

**Proof the contract is wrong** (F21.21, corrected by F21.22).
`n2.r1`'s eleven uncovered lines are the **bodies** of `to_dict`
(`:97`), `from_dict` (`:105-109`), `__eq__` (`:114`), `__repr__`
(`:117`), two `raise TypeError` guards for a `bool` amount and one
`raise ValueError` in `transfer`. They are added lines, not baseline
code the worker reproduced: REQ-002 replaces the scalar `balance` with a
per-currency `_balances` map, so those four bodies *must* be rewritten
or they reference a field that no longer exists.

This entry's first draft said the tests that exercise them belong to
REQ-004 / node `n4`, "which had not run". That is wrong. The plan
record's target files say `to_dict` and friends live in `accounts.py` —
**`n2`'s own target** — and the only file that can exercise
`accounts.py` is `tests/test_accounts.py`, which belongs to **`n1`**
(REQ-001). `n1` **sealed before `n2` was dispatched**, and its sealed
tests touch those four members zero times. `n4` targets `report.py` and
`store.py` and is irrelevant here.

So the set of nodes that could ever cover those lines is empty at the
moment the gate demands them, and one of them has already finished. The
node is caught between three of its own gates:
`coverage` requires those lines executed, `public-deletions` (T6-42)
forbids removing them — they are the same four members round 3d's repair
deleted — and `check_node_scope` forbids an impl node writing the test
that would execute them, which in this plan is the test file a
**sealed** node owns. The only move left is a call that exists so a
line is recorded as executed, which Goal G1 rules inadmissible as
specification, not as exhortation.

So this is not the worker failing. It is a plan whose DAG order makes a
gate unsatisfiable for a node, and the gate firing exactly as written.

**The decision is the user's, and it is a real fork.** Three candidates,
none free:

(a) *Grade coverage over the plan, not the node.* The honest reading —
a line added for REQ-004 is gradeable once `n4` has run. Costs: the
proof stops being per-node, and a failure arrives late, after more work
is built on it.

(b) *Narrow `changed` to lines the node's own requirements reach.* Cheap
and local, but it is a population narrowing, so WORKPLAN §0.6 applies:
it may not land without exhibiting the known-bad it now admits — and
the obvious one is real, a node adding unreachable code under a
requirement nobody will test later.

(c) *Defer the undecidable lines rather than pass them.* A changed line
whose only plausible exerciser belongs to a requirement no sealed node
owns is recorded as **deferred**, the node seals, and the run fails at
the end if any deferred line is still uncovered when the DAG drains.
Keeps the bar, moves the verdict to where the evidence exists; costs a
new outcome state and a drain check.

I recommend **(c)**: it neither lowers the bar nor pretends the question
is answerable at the time it is currently asked, and unlike (b) it
cannot be satisfied by narrowing. But (a) is simpler to reason about and
the choice changes what a proof means, so it is not mine to make.

Whichever lands: known-good is `refs/saddle/attempt/n2.r1/1` frozen as a
fixture — it must be accepted, since ground truth accepts it. Known-bad
is a node that adds a private helper nothing will ever reach, which must
still fail. Both halves, or not done.

**DONE 2026-09-21 — deferral without a drain failure; loosened, scope
narrowed.** `pending_test_nodes` (dag.py) names the nodes a plan still
owes tests from — kind other than `impl`, not yet proven — and
`check_changed_line_coverage` takes it as `owed`. Non-empty defers the
uncovered lines and seals the node, recording
`changed-lines=N deferred-lines=M owed=…` in `basis`; empty is exactly
the pre-T6-53 check.

The user chose deferral **without** the run-end failure the original (c)
carried, and the reason is G1: a deferred line is uncovered against the
arm's own suite, while the oracle runs a different hidden suite, so a
drain failure would fail runs whose artifact is correct — gates-fail
with oracle-pass, which is not agreement.

Direction: **loosened**, and §0.6's exhibit is a test, not a sentence.
`test_coverage_deferral_admits_a_node_that_adds_code_nothing_runs` and
`test_run_slice_defers_an_uncovered_line_while_a_test_node_is_owed`
both show the admission: a node may add code nothing runs and seal,
while any test node is owed. Proof the contract was wrong is F21.21 —
round 3g rejected a tree that passes 16 of 16 hidden accounts-and-fees
tests.

Mutants, all killed: **M1** `owed` ignored, always defer; **M2**
deferral never fires; **M3** `impl` nodes counted as able to write
tests; **M4** proven nodes still counted as owed; **M5** the slice
wiring forced to `()`. **M5 SURVIVED on its first run against all 857
tests**, the same hollow-wiring shape T6-54 hit an hour earlier — the
predicate and the gate were both tested, and nothing drove `run_slice`
with an owed test node. Closed by the integration test above, whose
known-bad sibling
(`test_run_slice_gate_fail_leaves_dependent_undispatched`) is the
identical diff and identical uncovered `unused` in a plan owing no
tests, which still fails. `./check.sh` green, 858 passed, 100%.

**Retracted block, kept for the record.**
The record shape those eleven lines implement is the one the task prompt
specifies under **§7 Store** (`{"owner": str, "balances": {currency:
"decimal-string"}}`), i.e. **REQ-004**, node `n4`; §4 Accounts requires
no serialization change and explicitly keeps `.balance`. So the
behaviour is REQ-004's, the code sits in `accounts.py` which only `n2`
may edit, and the test is `tests/test_store.py` which belongs to `n3`.
The covering node is absent **when the gate asks** and present later —
exactly the shape a deferral answers. Coverage at drain is contingent on
`n4` routing `save_accounts` through `Account.to_dict`, and if it does
not, failing the run is correct: nothing ever exercised REQ-004's record
format. The G1-disagreement objection does not arise, because the oracle
cannot pass while those lines are unexercised — the hidden store suite is
what exercises them. Proceed with **(c)** as chosen.

**Retracted block, kept for the record.**
The user chose **(c)** on 2026-09-21, on this entry's first-draft
reading that the exerciser "belongs to a requirement no sealed node
owns" — i.e. that a *later* node would supply the coverage. F21.22
shows the owning test node is `n1`, which had **already sealed**. Under
the corrected structure (c) does not work:

- The DAG always drains with those lines still uncovered, because no
  node in it can cover them. Deferral converts an attempt-2 gate failure
  into an end-of-run failure and changes no verdict.
- In the case where `n3` and `n4` then succeed, the oracle **passes**
  while the run **fails** on the deferred lines. That is a manufactured
  G1 disagreement — the precise outcome the goal exists to eliminate —
  produced by the fix rather than by a defect.

(a) survives the correction: grading coverage over the plan rather than
the node still answers the question later, but "later" now has to mean
*after a node that can write the test has run*, and no such node exists
in this plan either. (b) survives as a narrowing and still owes §0.6 its
known-bad.

A fourth candidate the fork did not contain, and which the correction
points at:

(d) *Fix the plan, not the gate.* The defect may be in planning: the
planner assigned `tests/test_accounts.py` to `n1` and `accounts.py` to
`n2`, so the test node for a file seals before the impl node that
rewrites it. A plan that keeps a file's tests open until its
implementation has sealed — or that pairs test and impl over the same
files with the test node ordered *after* — dissolves the trap without
touching a gate, and leaves `coverage` demanding exactly what it demands
today. Cost: it is a planner change, the largest of the four, and it
cannot retrofit a plan the model already emitted.

Round 3c is the recorded evidence that the DAG *can* produce a node able
to cover the lines: 3c replanned its failed impl node into a **test**
node, which sealed. But that discharges no implementation requirement,
and 3c still ended in an oracle FAIL.

The fork needs re-deciding against the corrected premise before any of
this lands. Until then T6-53 does not start; T6-54 is independent and
goes first.

### T6-55 — Round 3h: does an impl node seal? (measurement)

Owner: main session. Pre-registration at `../saddle-bench/runs/round3h/PREDICTION.md`
(bench `9919fa5`), run under T6-53 and T6-54.

**This round is the falsifiable test of the gate-fixing loop, not just
the next data point.** Rounds 3c–3g each ended by naming one gate defect
and fixing it. Across all five, **no impl node has ever sealed** — every
seal in the benchmark's history is a test node, read from the plan
record at the head of each run's `proofs.jsonl`:

| round | sealed | kinds |
|---|---|---|
| 3c | n1, n2.r1 | test, test |
| 3d | n1 | test |
| 3e | n1 | test |
| 3f | — | — |
| 3g | n1 | test |

So the loop has a stated falsifier: **if 3h seals no impl node,
gate-fixing is not the bottleneck and the next item may not be a sixth
gate.** Q1 is that question. Q2 and Q3 are T6-53's and T6-54's first
live uses; Q4 is Goal G1, whose count is at zero.

Q1 is resolved **together with Q4, never alone**: T6-53 by design admits
a node that adds code nothing runs, so a seal that happened only because
deferral swallowed a real gap is hollow and must be reported as such.
Three pre-registered ways a question ends UNRESOLVED rather than
falsified are recorded in PREDICTION.md, so a quiet run is not read as a
pass.

### T6-54 — A recovery plan may not prescribe what a gate rejects (tightened)

Files: `src/saddle/slice.py` or wherever the recovery text is built and
routed (T6-31), `src/saddle/gates.py` (reuse the `public-deletions`
predicate), `tests/`, plus the frozen brief as a fixture.

**Known-bad, already in hand, verbatim from round 3g's attempt-2 sidecar:**

> 2. In `accounts.py`, remove the `to_dict()`, `from_dict()`, `__eq__`,
> and `__repr__` methods (no test exercises them).
> 3. In `accounts.py`, remove the `if quantized <= 0: raise ValueError(...)`
> guard from `transfer()` ...

That is round 3d's behaviour — the behaviour T6-42's gate exists to
reject — **prescribed to the worker by the harness itself**. The worker
followed it and burned 1871 s and 149 151 output tokens ending `length`
with no diff.

Contract: a recovery plan that names a public definition the baseline
has, in a removal instruction, is not routed to the worker. The
predicate already exists — `public-deletions` computes exactly this set
from the baseline tree — so this is a reuse, not a new judgement, and it
stays deterministic: no model reads the plan to decide whether it is
good advice.

Direction: **tightened**. Nothing the worker could legally do becomes
illegal; one thing the harness could say to the worker becomes
unsayable.

Known-good: a recovery plan that names those same methods without
proposing their removal ("`to_dict` is untested; the test that covers it
belongs to REQ-004") is routed unchanged. A plan proposing removal of a
**private** helper is routed — `public-deletions` permits that and so
does this. Known-bad: the frozen brief above, and a plan proposing
removal of a dunder, since three of round 3d's four deletions were
dunders and T6-42 already counts them public.
Vacuity: a test must show the check is capable of passing a plan that
mentions the same names, or it is pinning the string rather than the
instruction.

**DONE 2026-09-21 — `plan_prescribes_deletion` (gates.py) + the routing
at cli.py's `propose`; tightened.** A removal verb governs the names in
its own clause, and a negator in that clause turns it around; a plan
that offends is withheld and `build_repair_prompt` omits the section
rather than emitting an empty heading. Test files are excluded from the
protected set, since a test node may legitimately be told to drop a test
it wrote. The round 3g brief is frozen at
`tests/fixtures/recovery_plan_deletes_public_round3g.txt`.

Mutants, all killed: **M1** negation ignored (5 tests died); **M2**
clause split dropped so the whole line is one clause (2); **M3** test
files counted as public API (1); **M4** the verb governs the whole
clause rather than what follows it (1); **M5** the call-site wiring
bypassed so every plan routes (1).

**M5 SURVIVED on the first run, against the whole 850-test suite**, and
that is the finding worth keeping. The predicate was well tested and
`build_repair_prompt(plan=None)` was well tested, but nothing drove
`run_task` with a plan that prescribes a deletion, so deleting the
wiring changed no test. `cli.py` was at 100% line *and* branch coverage
throughout — other tests took both sides of the conditional — which is
CLAUDE.md's opening case reproduced exactly: coverage proves a line ran,
not that anything could catch it changing. Closed by
`test_run_task_withholds_a_recovery_plan_that_prescribes_a_deletion`,
parametrized over a withheld plan and a routed one so the diagnosis step
does not become dead weight. `./check.sh` green, 852 passed, 100%.

Owner: main session. Independent of T6-53 and much smaller; it can land
first.

### T6-56 — Every commit saddle creates carries saddle's identity (tightened) — DONE 2026-09-21

Files: `src/saddle/evidence.py` (`SADDLE_COMMIT_IDENTITY`,
`snapshot_tree`), `src/saddle/cli.py` (`_ensure_repo`),
`tests/test_evidence.py`, `tests/test_cli.py`.

**Round 3h did not fail a gate. It never reached one.** The run ended
`EXIT=1 WALL=733` with nothing sealed, and the transcript's first node
read:

```
## Node n1
- Requirements: REQ-001
- Proof: none
- Timeline:
  - tool git: exit 0 in 1ms: git -C .../t5-saddle add -u
  - tool git: exit 0 in 1ms: git -C .../t5-saddle write-tree
  - tool git: exit 128 in 6ms: git -C .../t5-saddle commit-tree 008ada01... -p HEAD -m saddle snapshot refs/saddle/baseline/n1
```

Reproduced by hand: `fatal: unable to auto-detect email address (got
'eliza@pop-os.(none)')`. The host's DNS domain had gone away since round
3g, whose commits in the same kind of worktree are authored `Eliza H
<eliza@pop-os.mynetworksettings.com>` — an identity git *derived*, not
one anybody set. There is no identity in the worktree, none global, and
none in the bench's `run_arm.sh`. Sixteen milliseconds into the slice,
733 seconds of wall clock spent, no gate verdict, no oracle comparison,
no G1 seed.

`_ensure_repo` has passed `-c user.name=saddle -c user.email=saddle@local`
for its baseline commit since the command existed. The snapshot commits
T6-34 added did not — so from T6-34 onward a run's survival depended on
git config saddle never set and on the machine's DNS, and the failure
surfaces as a node with no proof rather than as anything a gate can say.

Contract: **every commit saddle creates is authored `saddle
<saddle@local>`, whatever the operator's git config says or fails to
say.** One constant in `evidence.py`, used by both call sites.

Known-bad, red before the change and green after:

- a repo carrying `user.useConfigOnly=true` and no identity, so git
  refuses the guess deliberately the way this host refused it by
  accident. `snapshot_baseline` raised `RuntimeError`; `_ensure_repo`
  raised `RunError` before a plan could be drawn.
- a repo that *does* carry an operator identity: the snapshot came back
  signed `test <test@example.com>`. Saddle's own bookkeeping ref, the
  operator's name on it.

Mutants against `tests/test_evidence.py tests/test_cli.py`, all KILLED:
M1 drop the identity from `snapshot_tree` (2 failed); M2 drop it from
`_ensure_repo`'s call site (5 failed); M3 the email is the operator's (5
failed); M4 the name is the operator's (5 failed); M5 the identity moved
onto the `add -u` call, where it is accepted and does nothing (3 failed).

Direction: **tightened** — saddle now supplies what it previously
borrowed. Nothing a gate accepts or rejects changes.

Three existing assertions read the git subcommand as `span.argv[3]`,
which the two `-c` pairs displaced. Repaired by reading it by shape
(`_git_subcommand`) rather than by index: the expected list of four named
git runs is unchanged, so a fifth run or a different subcommand still
fails, and the evidence one now also pins the identity to the commit
alone — a run that passed it to every git call reads identically by
subcommand and is caught here. Not a flip: no expectation changed
direction.

`./check.sh` green, 861 passed, 3 skipped, 100% line and branch.

Owner: main session. Blocks round 3h: with this unfixed every run on
this host dies at node 1.

### T6-57 — A failed node reports the gate verdicts it actually produced (tightened)

Files: `src/saddle/slice.py` (`_transcribe`), `tests/test_slice.py`.

`_transcribe` chooses a failed node's gate table from the **type** of its
terminal failure, not from what the node's attempts recorded:

```python
    if isinstance(failure, NodeGateFailedError):
        checks = failure.result.checks
    elif isinstance(failure, NodeUnappliableError):
        checks = ()
    else:
        checks = ()
```

The attempt loop retries both gate failures and non-applying diffs. A
transport failure -- `request failed: timed out`, `worker call failed` --
is sealed and then **re-raised unchanged** by the loop's bare
`except BaseException`. It arrives as itself, lands in the `else`, and
the node renders `- Proof: none`, a complete tool timeline, and not one
gate line. Every earlier attempt's verdict is gone from the report.

A non-applying *final* attempt does not do this, because that path raises
`_HaltRecoveryError`, which carries the retained gate result and is
converted to `NodeGateFailedError`. So the same run reports two nodes
with identical histories differently, decided by which exception happened
to be in flight when the node gave up.

Observed (F21.26, ../saddle-bench/runs/IMPL-NODE-TABLE.md):

| node | last attempt | gate table in the transcript? | on disk |
|---|---|---|---|
| 3g n2.r1 | diff did not apply | yes, 13 gates | `coverage` FAIL, 12 PASS |
| 3c n2 | gates failed | yes, 11 gates | `coverage`, `mutation` FAIL |
| 3c n2.r2 | `request failed: timed out` | **no** | `ruff`, `mutation` FAIL; `coverage: 100.0%` PASS |
| 3d n2 | `request failed: timed out` | **no** | two gated attempts; `coverage 98.5%`, one line |

The cost is not cosmetic. 3d `n2` attempt 2 is the closest an impl node
has come to sealing on a percentage coverage verdict -- 98.5%, uncovered
`money.py:33` -- and it was invisible in the run's report, in
`saddle explain`, and in every summary written from them. An operator's
picture of how close the harness has ever got was wrong for a day
because of a formatting branch. T3-25's comment beside `_seal_attempt`
already states the premise ("the transcript renders its last attempt
only, so the journal is the one place an earlier attempt's verdict can
be read back from") and the journal does hold the failed gate *names*;
only the sidecar holds each gate's *detail*, and nothing in the report
sends a reader there.

Change: retain the last `GateResult` the node produced and render it
whatever the terminal failure was, labelled with the attempt it came from
so a reader cannot mistake it for the final attempt's.

Direction: **tightened** -- the report gains verdicts it was dropping;
no gate's accept/reject set changes.

- **known-good:** a node whose final attempt gates renders that
  attempt's table, unlabelled-as-stale (today's behaviour, must not
  change), and a node that never gated renders none.
- **known-bad:** a node with a gated attempt 1 and a transport failure
  on attempt 2 renders no table -- today's behaviour, so the test is red
  before the change.

Contract mutants to record: (1) drop the retained result so the
transport path renders `()` again; (2) render the retained result
without its attempt label; (3) render a retained result for a node that
never gated.

Owner: main session. Independent of everything queued; blocks nothing,
but every measurement round until it lands can hide a verdict from the
person scoring it.

### T6-58 — A requirement may not restate the gate (tightened)

Files: `src/saddle/cli.py` (`build_emit_prompt`, and the repair prompt),
`src/saddle/gates.py` (a new plan-side predicate in
`plan_prescribes_deletion`'s image), `tests/test_gates.py`,
`tests/test_cli.py`.

T6-44 removed the execution proxy from **saddle's own** worker rule and
replaced it with the mutation form. It did not touch the instruction that
regenerates the proxy, and the proxy still reaches the worker — laundered
through the plan.

`build_emit_prompt` still tells the planner (cli.py:347):

```
- changed_line_coverage_min is always 100.0: every line you change must
  be executed by a test.
```

The planner then writes that into the artefacts the worker reads as
binding. Round 3g's `n2.r1` prompt, retained whole (64 399 chars, and
`e52912b` (T6-44) is an ancestor of the run's HEAD `9de4367` — so this is
**after** the fix):

```
Node n2.r1: ... quantize with ROUND_HALF_UP to 2dp for USD/EUR and 0dp
for JPY. Keep all changed lines covered by the gate command.
Requirements (each test must fail if its statement is violated):
  REQ-002: In accounts.py and fees.py, the system shall implement the
  shared Decimal/USD-EUR-JPY money rules, with fee_for returning the fee
  ..., and every changed line executed by tests/test_accounts.py and
  tests/test_fees.py.
```

Two separate model-authored strings — the node description and the
requirement statement — both restating the coverage gate as though it
were behaviour, inside the one block headed "each test must fail if its
statement is violated". Saddle's own rules block contains no such
sentence any more (`grep` finds it only in `build_emit_prompt`).

This is the exact shape Goal G1 names: a requirement satisfiable by a
no-op. "Every changed line executed by tests/test_accounts.py" is
satisfied by calling a function whose body is `pass`; "fee_for returns
the fee for a positive amount below the fee" is not. The round-3e
known-bad's placeholder docstring paraphrases a requirement of the first
kind — "Placeholder to satisfy the requirement that every changed line is
executed by tests" — and says so in the word *requirement*. That draw's
own prompt cannot be read back (a pre-T6-36 record truncates it at 4 000
chars with `[truncated 33168 chars]`), so 3e is consistent with this and
does not prove it; 3g's retained prompt is the proof.

A prompt change alone would be decorative (T6-45's rule: an instruction
the harness cannot verify). So the item has two halves:

1. **Instruction.** `build_emit_prompt` states the field's purpose
   without restating it as a requirement the planner should echo, and
   says requirements describe behaviour a test can falsify — never how
   the gate measures.
2. **Check.** A plan-side predicate, in `plan_prescribes_deletion`'s
   image, rejects a plan whose requirement statement or node description
   asserts that changed/added lines are *executed* or *covered*, or names
   a coverage ratio. Rejected the way T6-54 rejects a recovery plan that
   prescribes a deletion — the plan is refused and redrawn, not silently
   rewritten.

Direction: **tightened**. Nothing a gate accepts or rejects changes; the
population of admissible *plans* narrows.

- **known-good:** a requirement that names falsifiable behaviour passes
  (3g REQ-002 with the trailing clause removed; 3f's and 3h's statements
  as written), and a test node's requirement that legitimately names a
  test file is not caught — the predicate keys on the execution/coverage
  claim, not on the file name.
- **known-bad:** 3g `n2.r1`'s REQ-002 verbatim, and its node description
  sentence "Keep all changed lines covered by the gate command", both
  frozen as fixtures from the retained prompt.

Contract mutants to record: (1) drop the coverage-claim clause so the
known-bad passes; (2) key the predicate on the gate's test-file names
instead of the claim, so the known-good test-node requirement is caught;
(3) bypass the call site in `run_task` (the T6-53/T6-54 wiring-mutant
lesson — a predicate's mutants say nothing about whether anything calls
it).

Noted in passing, and not this item: the plan record journals requirement
**ids** only, never their statements. The text a node was held to is
recoverable solely from an attempt prompt, so a run whose prompts are
truncated (anything before T6-36) cannot be audited on this at all.

Owner: main session, after round 3h. Does not block 3h.

### T6-34 — A gated attempt's tree survives `git gc`, and the run seals the ruff it autofixed with (tightened)

Files: `src/saddle/slice.py` (`_run_node`, after each gate run),
`src/saddle/evidence.py` (a `refs/saddle/attempt/<node>/<n>` snapshot
beside `proven_ref`), `src/saddle/cli.py` (`ruff --version` in the run
settings), `src/saddle/gates.py` (the "66-survivor" comment),
`tests/test_slice.py`, `tests/test_cli.py`.

Session 41 (F21.15) had to reconstruct round 3d's attempt-1 trees from
dangling blobs in `lost-found`, because a failed attempt leaves no ref:
`refs/saddle/baseline/<node>` is the pre-node tree and
`refs/saddle/proven/<slug>` exists only for a pass, so any `git gc`
prunes what a failed attempt was graded on. And the sidecar's diff
(T6-27) is the pre-autofix text; the tree the gates saw is that diff
plus `ruff check --fix` and `ruff format` at a version recorded
nowhere (two of round 3d's blobs differ only by autofix). Contract: (1)
every attempt that reaches a gate run is snapshotted as
`refs/saddle/attempt/<node-slug>/<n>` after autofix and before the
gate, through `snapshot_tree`, journaled as the other snapshots are;
(2) the run span's settings carry `ruff=<version>` from `ruff
--version`; (3) the `check_mutation` comment says "13 survivors on the
gate's population, 66 on the tree". Direction: tightened (more is
retained; nothing is loosened). Known-good: a failed attempt's ref
resolves to a tree holding the autofixed diff; the run span's argv has
the ruff version. Known-bad: a run under the current code has no
attempt ref for a failed node (round 3d's worktree: four refs, no
`proven/n2`). Mutants: (1) the snapshot taken before autofix (the ref
then differs from the gated tree); (2) the ruff version dropped from the
settings. What this is not: committing the worktree into the bench
(machine-local by design; the refs make the blobs durable where they
are). Owner: main session; small; after round 3e unless 3e needs it.

**Status (2026-09-21): DONE.** All three clauses.

Clause 1. `attempt_ref(node_id, attempt)` beside `proven_ref` in
`evidence.py`, over `ATTEMPT_REF_PREFIX = "refs/saddle/attempt/"` and
the same `_ref_slug` rule, and one `snapshot_tree` call in `_run_node`
placed after `autofix` and before `run_node_gate`. Its tree id goes into
the sidecar as `"tree"` on both post-gate seals -- the pass and the
gate-failure -- so the record names the tree and the ref keeps the
blobs. The snapshot is journaled like the other two: four more `git`
spans (`add -u`, `write-tree`, `commit-tree`, `update-ref`) per attempt.

Clause 2 was already in the tree: T6-37 landed `"ruff": ruff_version()`
in `run_task`'s settings (`cli.py:750`), and
`test_run_task_seals_the_runs_settings_on_the_run_span` pins it with
`ruff_version() != "unavailable"` as the vacuity guard. Verified by
running this item's own second mutant against it rather than taking the
earlier commit's word: deleting the line turns the run span's argv into
a 12-element list and the test dies on the missing `ruff=0.16.7`.

Clause 3: the `check_mutation` survivor comment now carries F21.15's
re-measurement (13 on the gate's population, 66 on the tree) and says
why the two populations differ, instead of F21.14's order-of-magnitude
reading of a five-name prefix.

Red first. With `slice.py` reverted to HEAD and the new `evidence.py`
helper kept (so the import resolves and the red is the contract, not a
collection error), both new slice tests fail on `fatal: invalid object
name 'refs/saddle/attempt/n1/1'`. Restoring `slice.py` makes them pass.

Tests (3 new):
`test_attempt_ref_names_the_node_and_the_attempt_separately`
(`tests/test_evidence.py`) -- the attempt number is its own component,
plus the digest fallback for an id `check-ref-format` rejects;
`test_a_gated_attempt_leaves_a_ref_for_the_autofixed_tree_the_gate_saw`
-- `SLOPPY_DIFF` is the one fixture whose applied text and graded text
differ, so `git show <ref>:n.py` carrying the single space is what pins
"after autofix", and the sidecar's `"tree"` is asserted to be that same
tree; `test_each_attempt_of_a_node_keeps_its_own_graded_tree` -- attempt
1 fails its gate (`return 3`), attempt 2 repairs it in place (`return
2`), both refs resolve to different trees and each sidecar names its
own. That last one is the vacuity guard the item's known-bad asks for:
a ref per node rather than per attempt reports two attempts as one.

Mutants, all KILLED:
(1) the snapshot moved before `autofix` -- the ref holds `return  2`
    and the autofix test dies on the double space;
(2) `attempt_ref` drops the `/{attempt}` component -- the evidence test
    dies on `refs/saddle/attempt/n2` and the per-attempt test dies
    because `/1` no longer resolves;
(3) the pass-path sidecar drops `"tree"` -- both slice tests die,
    one on the key, one on `KeyError`;
(4) the item's second mutant, `"ruff": ruff_version()` deleted from the
    settings -- `test_run_task_seals_the_runs_settings_on_the_run_span`
    dies (above).

Two existing enumerations were extended, and neither is a flip: both
still assert the same closed property, over a set the contract enlarges
by exactly the item's snapshot. `test_run_slice_pass_end_to_end` pins
the whole tool-span sequence, which is what proves the snapshot is
journaled at all -- an unjournaled `git` call would now fail it in the
other direction. `test_run_slice_two_nodes_are_gated_against_their_own_baselines`
pins `for-each-ref refs/saddle/`, which is what proves no stray ref is
written outside the three namespaces.

`./check.sh`: 832 passed, 3 skipped, 100% line+branch, exit 0.

### T6-30 — A worker request has a total deadline, not only an inter-chunk one (tightened)

Files: `src/saddle/vllm.py` (`DEFAULT_TIMEOUT` and its comment, the
streaming read loop), `src/saddle/cli.py` (a `--request-timeout` flag if
one is wanted), `tests/test_vllm.py`.

F21.13c: the comment on `DEFAULT_TIMEOUT` says the request is
non-streaming so the timeout is the whole-generation deadline; the
payload sets `stream: true`, and an httpx timeout on a streamed response
bounds the gap between reads. Round 3c's timed-out attempt ran 1826 s and
round 3d's 2494 s against a documented 1800 s bound. Contract: the
streaming loop checks a monotonic clock against a total budget on every
chunk and raises `VllmRequestError("request exceeded N s")` carrying the
partial reasoning, content and usage seen so far (so T6-27's sidecar has
them); the inter-chunk timeout stays as the stall detector. Direction:
tightened. Known-good: a fake stream that emits a chunk every second for
longer than the total budget is cut at the budget, with the partial text
in the error. Known-bad: the same stream under the old code runs to the
end. Mutants: (1) the clock check removed; (2) the partial text dropped
from the error. Owner: main session; small, lands with T6-27.

**Status (2026-09-20): WITHDRAWN on evidence, scope narrowed to T6-27.**
Only the chat REPL's payload (`_build_chat_payload`, `saddle up`) sets
`stream: true`; the worker's diff and text requests are plain POSTs, so
the 1800 s read timeout is a whole-request bound for them and the
`DEFAULT_TIMEOUT` comment is right for the path it governs. The 2494 s
attempt was two requests, the recovery-plan `complete()` call and the
diff call, each inside its own bound; the plan call was simply never
recorded, which T6-27 fixes by recording every call's start and wall. A
total bound on the chat stream is a T5 concern, and an inter-chunk
stall detector is the right semantic there.

### T6-31 — The output of a gate the node failed always reaches the worker (loosening of T3-4's withholding, with evidence)

Files: `src/saddle/slice.py` (`format_attempt_failure`, `_run_is_allowed`),
`tests/test_slice.py`.

F21.13d: n2's repair brief for a failed ruff gate was the one line `ruff
check exited 1, format exited 0`, because T3-4 withholds a captured run's
output unless the node's `allowed_tools` names the tool. Ruff failed 2/2.
In the same two attempts coverage went 89.9% -> 98.5% and mutation 75.3%
-> 78.3%, because those verdicts carry file:line and mutant names inline.
The worker repairs what it can see. Whether the plan declared `lint` is
not recoverable (the plan record omits `allowed_tools`; T6-27 fixes
that), and the contract is right either way: `allowed_tools` bounds what
the worker may *do*; the verdict of a gate the node failed is the node's
own evidence, not a capability. Contract: a captured run whose exit code
made a gate fail is included in the brief regardless of `allowed_tools`;
a run for a gate that passed stays governed by T3-4. Direction: loosening
of T3-4's withholding, evidenced by F21.13d. Known-good: a node without
`lint` that fails ruff sees ruff's rule, file and line; known-bad: the
same node with ruff passing sees no ruff output. Mutant: the
failed-gate exception removed -> the known-good red. Owner: main session;
lands with T6-3 (which names the rule in the gate detail).

**Status (2026-09-20):** DONE, `b192535`, with T6-3. `GATE_TOOL` maps a
failed gate to the tool whose output it is; a passed gate's run stays
under T3-4. One labelled flip (the T3-4 withholding test's fixture has
a failed tests gate); its passed-gate half is now its own known-bad.

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
| Model-driven memory workers (observe / reflect / prune) for the chat session | they are model calls whose output the agent then trusts; under "everything deterministic" they are opt-in at most | T5-9 measured as insufficient on a real long session |
| `repetition_penalty` / `top_p` / `min_p` on worker calls (F21.9c item 14) | at 1.05 it failed 3/3 where today's shape passed 3/3 (F21.10) | a node that repeats without a penalty on record, then a value sweep with the penalty-on/grammar-off cell (F21.11 items 22, 23) |
| Repetition oracle over sections and motifs (F21.10 item 19) | nothing to gate on until degeneration recurs without the penalty | the same record |

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
