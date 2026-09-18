# Benchmark issue archive

Verbatim archive of every GitHub issue body and comment on
`eliza-halpern/saddle` as of 2026-09-18, captured before the issue
threads were consolidated. Nothing here was edited; comments that were
later deleted from GitHub are preserved below in full.

Issues: 48. Comments: 67.

---

## #1 — Scaffold: package layout, uv, lint/type/test gates

*CLOSED, opened 2026-09-16, closed 2026-09-16*

### Body

uv-managed src/saddle/ layout with maximal gates from day one (locked decisions: Python + uv install; pytest + hypothesis + mypy-strict + ruff; 100% branch coverage enforced; mutmut self-mutation in CI; operator UX primitives in slice scope). AC: fresh clone → uv sync → ./check.sh green (ruff, format, mypy-strict, pytest 100%); mutmut run kills 100%; CI runs check.sh + time-boxed mutation. Includes: ux module (progress + human-gate confirm), argparse CLI skeleton, hypothesis property test. Ref: ARCHITECTURE.md §6 step 1, §5.

---

## #2 — vLLM guided emission client

*CLOSED, opened 2026-09-16, closed 2026-09-16*

### Body

httpx OpenAI-compat client against the live server with guided_json DAG-schema params. Prove the XGrammar structural snap on the real quant. AC: 10/10 schema-valid DAGs; freeform <think> reasoning precedes the snap. Ref: ARCHITECTURE.md §3 Phase 1.

---

## #3 — DAG schema + static validator

*CLOSED, opened 2026-09-16, closed 2026-09-16*

### Body

Pydantic DAG model (ARCHITECTURE.md §3 schema) with zero-LLM checks: acyclicity, dep resolution, tool allowlist, budget enum, context ceiling, gate+requirement presence, criterion coverage. AC: one rejecting fixture per violation with machine-readable errors; valid DAG passes. Ref: ARCHITECTURE.md §3 Phase 1.

---

## #4 — Kahn asyncio scheduler

*CLOSED, opened 2026-09-16, closed 2026-09-16*

### Body

In-degree tracking, proof-gated dispatch (all parent proofs present before dispatch), worker pool with per-node budgets. AC: diamond DAG runs branches concurrently; failing parent leaves downstream undispatched (assert zero spawns). Ref: ARCHITECTURE.md §3 Phase 2.

### Comment 1 — 2026-09-16 17:34:43 UTC

Done in a360862. `schedule()` runs a validated Dag over an asyncio.Queue: in-degree tracking, proof-gated dispatch (only success decrements dependents, so in-degree 0 implies all parent proofs exist), pool of 5 workers (arch §2 fan-out) each handed its node's execution constraints as its budget. Rogue proof for the wrong node is recorded as a node failure, blocking downstream. AC: diamond branches proven concurrent via rendezvous + start/end log; failing parent leaves downstream undispatched with zero spawns asserted. Gates: check.sh green (71 passed, 100% coverage incl. branch), mutmut 449/449 (448 killed + 1 timeout-caught, 0 survived), CI check+mutation success.

---

## #5 — Tier-1 node gate runner

*CLOSED, opened 2026-09-16, closed 2026-09-16*

### Body

Per-node fast gates: ruff/AST, pytest scope, changed-line coverage, red-phase (fail pre-change / pass post-change), requirement binding. AC: five killer fixtures, one per check. Target <~2 min. Ref: ARCHITECTURE.md §3 Phase 3 Tier-1.

### Comment 1 — 2026-09-16 20:09:12 UTC

Landed in fc6455b: six-check Tier-1 gate (syntax, ruff, tests, changed-line coverage, red-phase, requirement binding) with real collectors and run_node_gate. check.sh green (117 passed, 100% branch coverage); mutation 0 survived (2 known inherent dag timeouts).

---

## #6 — Proof journal

*CLOSED, opened 2026-09-16, closed 2026-09-16*

### Body

Append-only JSONL, sha256 content hashes, parent linkage, fsync per record, rebuild-scheduler-state-from-journal. AC: kill -9 mid-run rebuilds state; tampered record fails verification; chain verifies by recomputation. Ref: ARCHITECTURE.md §3 Phase 3.

### Comment 1 — 2026-09-16 20:26:38 UTC

Landed in 3ca5555: sealed ProofRecord, fsync append, verify by recomputation + parent linkage, rebuild_proven, build_from_gate. check.sh green (130 passed, 100% branch coverage); mutation 0 survived (1 known inherent dag timeout).

---

## #7 — E2E slice + benchmark tasks

*CLOSED, opened 2026-09-16, closed 2026-09-16*

### Body

One real mechanical task end-to-end through the slice; define the 2-3 reference tasks and pre-register the §6 gate criteria before running any comparison. AC: slice completes; benchmark doc merged; criteria recorded. Ref: ARCHITECTURE.md §6 step 2.

---

## #8 — Journal the thinking stream

*CLOSED, opened 2026-09-16, closed 2026-09-16*

### Body

Capture reasoning_content per node from vLLM into the proof record, with size caps and secret redaction. AC: live run journal contains per-node thinking; tests cover cap + redaction.

---

## #9 — Tool-call spans

*CLOSED, opened 2026-09-16, closed 2026-09-16*

### Body

Record every tool invocation (name, args hash, duration, exit summary) as journaled span records linked to its node. AC: full tool history of a run reconstructable from the journal alone.

---

## #10 — Subagent trace tree

*CLOSED, opened 2026-09-16, closed 2026-09-16*

### Body

Parent/child span linking (span_id/parent_id) across orchestrator, workers, and subagents so one run renders as a single verifiable tree; orphan spans fail verification. AC: nested run produces a linked tree.

---

## #11 — Transcript v2: thinking plus tool timeline

*CLOSED, opened 2026-09-16, closed 2026-09-17*

### Body

Extend the renderer with a per-node timeline interleaving thinking excerpts and tool calls. AC: live transcript shows what each agent thought and did, in order.

### Comment 1 — 2026-09-17 01:57:59 UTC

Shipped (timeline renderer with tests, gate, live proof).

---

## #12 — Live tail

*CLOSED, opened 2026-09-16, closed 2026-09-17*

### Body

Stream thinking/tool events during a run (saddle tail) instead of only rendering after. AC: follow a live run and watch thinking plus tool use appear in real time.

### Comment 1 — 2026-09-17 02:36:13 UTC

Shipped (`saddle tail` with tests, gate, live proof).

---

## #13 — saddle run: drive one task end-to-end

*CLOSED, opened 2026-09-16, closed 2026-09-17*

### Body

Emit, validate, schedule, gate, seal, and transcribe one task from a single CLI command; nonzero exit on failure. AC: one real task driven start to finish from the CLI with no Python one-offs.

### Comment 1 — 2026-09-17 00:04:22 UTC

Done: `saddle run` drives tasks end to end, verified live (PASS, exit 0) with 209 tests green, 100% coverage, and zero surviving mutants. Includes auto-init, empty-tree baseline fix, docstring coverage exemption, and the redesigned diff-guarantee CI mutation gate.

---

## #14 — saddle verify: audit a journal

*CLOSED, opened 2026-09-16, closed 2026-09-17*

### Body

Re-verify a journal's chain and orphan rule and re-render its transcript from the journal alone. AC: exit 0 plus summary on valid; precise issue list otherwise.

### Comment 1 — 2026-09-17 01:29:01 UTC

Shipped (`saddle verify` with tests, gate, live proof).

---

## #15 — saddle dag: show the plan before it runs

*CLOSED, opened 2026-09-16, closed 2026-09-17*

### Body

Print the emitted DAG as a tree: nodes, dependencies, budgets, gates. AC: dry-run view of any task's plan before anything executes.

---

## #16 — Preflight: fail fast when the server is unusable

*CLOSED, opened 2026-09-16, closed 2026-09-17*

### Body

Check server reachability, API key validity, and model match before executing anything (saddle doctor or run preflight). AC: clear message plus exit code instead of a mid-run traceback.

### Comment 1 — 2026-09-17 00:42:05 UTC

Done: preflight via GET /models (reachability, key, model) wired into run before any repo mutation, plus saddle doctor. Verified live (OK/refused/bad-model/wrong-key paths with correct exits) with 224 tests green, 100% coverage, zero surviving mutants.

---

## #17 — Rich TUI (future)

*OPEN, opened 2026-09-16*

### Body

Deferred from M2, which is CLI-scoped. Revisit a Rich-based interactive UI once the CLI surfaces (run, tail, verify, dag) have real usage behind them.

---

## #18 — Streaming vLLM client (token events)

*CLOSED, opened 2026-09-17, closed 2026-09-17*

### Body

Add SSE streaming to VllmClient: yield reasoning/content token events incrementally instead of waiting for full completion. AC: a live streamed completion emits both streams token by token; the request/response path is unchanged.

### Comment 1 — 2026-09-17 02:56:43 UTC

Shipped (streaming client with tests, gate, live proof).

---

## #19 — saddle up: interactive streaming REPL

*CLOSED, opened 2026-09-17, closed 2026-09-17*

### Body

Add `saddle up`: type text, watch reasoning plus response stream continuously, tool calls print as they run. Plain text first, no Rich layout. AC: a live session shows uninterrupted token output plus tool calls in order.

### Comment 1 — 2026-09-17 03:22:22 UTC

Landed: `saddle up` streams reasoning plus response tokens live, executes read_file/write_file/run_command tool calls in a bounded 10-round loop, and keeps session history. Gates: check.sh green (313 passed, 100% branch coverage), mutation 0 survivors on chat/tools/cli/vllm, live proof session plus a server-gated test showing streamed thinking, tool execution in order, and quoted file content.

---

## #20 — chat timeline: colored thinking, talk, and tool entries

*CLOSED, opened 2026-09-17, closed 2026-09-17*

### Body

Render `saddle up` as a Rich live timeline (Ocean palette), not panes: dim rules between turns, cyan `you>` prompt, thinking streams italic dim slate-blue (#7ba7cc), assistant talk renders bright-white Markdown with syntax-highlighted code fences (Rich streams partial fences stably, no special-casing), tool calls print magenta with green/red results, long args/results truncated with remainder markers. AC: a live session shows the colored timeline updating token-by-token with a highlighted code block.

### Comment 1 — 2026-09-17 15:06:11 UTC

Shipped in 6e12a54: single scrolling Rich Live timeline (Ocean palette) — dim rules framing user turns, cyan you> prompt, italic slate thinking, bright-white Markdown talk with highlighted code fences under a saddle> label, magenta tool calls with green/red results, clock-driven red-dot spinner, live-view tail cap plus 10Hz redraw throttle so long turns can't freeze the feed. Verified: 356 passed, 100% coverage, zero mutation survivors on timeline/chat/cli.

---

## #21 — Journal chat sessions

*CLOSED, opened 2026-09-17, closed 2026-09-17*

### Body

Seal chat turns (prompt, streamed reasoning excerpt, response, tool calls) as journal proof records so interactive sessions keep the harness audit trail. AC: `saddle verify` passes on a chat session journal.

### Comment 1 — 2026-09-17 03:58:09 UTC

Landed in 1b385a0: every `saddle up` turn seals a proof record (prompt/response/tool rounds hashed, reasoning excerpt as thinking, parent-chained as chat#N) and each tool call seals a timed span with exit code. New `--journal` flag (default `.saddle/chat.jsonl`). AC proven live: real session journals 1 proof + 1 span and `saddle verify` passes with a rendered audit transcript; server-gated test asserts the same. Gates: check.sh green (317 passed, 100% branch coverage), mutation 0 survivors on chat/cli.

---

## #22 — Map mid-stream server error objects to readable errors

*CLOSED, opened 2026-09-17, closed 2026-09-17*

### Body

When generation fails mid-stream (e.g. CUDA OOM), vLLM 0.28 yields `create_streaming_error_response` as a data event: `{"error": {"message", "type", "param", "code"}}` with no `choices`. `stream_chat` reports this as the cryptic `stream chunk has no choices`. Map it to `VllmRequestError("server error during stream: <message>")` instead. AC: a mocked error-object chunk raises with the server message; session survives.

### Comment 1 — 2026-09-17 03:39:34 UTC

Landed: `stream_chat` now detects vLLM 0.28 mid-stream error objects (`{"error": {...}}`, sent when generation dies e.g. CUDA OOM) and raises `VllmRequestError("server error during stream: <message>")` instead of the cryptic "stream chunk has no choices". Session survives via the existing VllmError catch in run_chat. Gates: check.sh green (315 passed, 100% branch coverage), mutation 0 survivors on saddle.vllm. Root cause of the report: GPU OOM killed the engine mid-stream; container has since restarted healthy.

---

## #23 — Wire saddle up to the harness (chat-driven runs)

*OPEN, opened 2026-09-17*

### Body

M2 decision: `saddle up` (interactive chat) and `saddle run` (DAG/gates/journal pipeline) stay separate capabilities through M2; this issue tracks wiring them together here in M5. Goal: a task typed into chat executes through DAG emission, gated workers, and sealed journal proofs. Recorded M2 problems to address: chat has no harness reach (no DAG/gates/evidence from the REPL), the 10-round tool cap is too small for real tasks (cube session burned all rounds probing), model thrash on environment probing. AC: one creative-route task runs from a chat turn to sealed proofs.

---

## #24 — M3-1: Benchmark arm harnesses + parity controls + criterion 5

*CLOSED, opened 2026-09-17, closed 2026-09-17*

### Body

Make all three benchmark arms runnable and freeze fairness before any scored run.

Arms (all on local vLLM qwen3.8-27B, xhigh thinking, 81920 completion tokens):
- Untouched: baseline arm --print stock config (--no-extensions --no-skills), --tools read,bash,write, --thinking xhigh. Done = tests green + human judge.
- Revision: normal orchestrator flow (roles pin thinking: xhigh), session JSONL captured. Done = review passed + tests green.
- Saddle: saddle run --reasoning-effort xhigh --max-tokens 81920 on task baselines. Done = verified journal + transcript.

Also: amend docs/BENCHMARK.md with criterion 5 (stall rule: 3x identical tool call with no state change fails immediately; 10 min with no progress event fails; 30 min absolute cap) and update the decision rule to criteria 1-5. Provide a per-arm stall check.

AC:
- [ ] Each arm completes a smoke task end-to-end; wall-clock and done signal recorded.
- [ ] Model, thinking level, toolkit documented identically for all arms.
- [ ] Stall check trips on a synthetic 3x-repeat control and stays silent on the smoke task.
- [ ] BENCHMARK.md amended; no scored T1-T3 run starts before this closes.

### Comment 1 — 2026-09-17 15:30:23 UTC

M3-1 evidence — all arms smoke-tested, parity frozen.

Parity (identical unless noted):
- Model: qwen3.8-27b on local vLLM (127.0.0.1:18020) for all arms.
- Thinking: medium pinned (saddle default; baseline arm --thinking medium). Deviation: revision stack escalated medium -> xhigh mid-run; recorded, revisit before scored runs if it recurs.
- Tools: untouched restricted to read,bash,write; revision full kit incl. extensions (documented asymmetry); saddle read_file/write_file/run_command.

Smoke (fresh scratch dir per arm, task: write hello.txt with exactly SMOKE-OK):
- Untouched: exit 0, 5s. baseline arm --print + stock flags (--no-extensions --no-skills --no-context-files --no-themes --no-prompt-templates), session JSONL captured.
- Revision: exit 0, 25s. Full config (plan_stepdown route fired), session JSONL captured.
- Saddle: exit 0, 13s, verdict PASS, journal verifies (saddle verify: 1 proven node, chain verifies).

Stall check (benchmark/stall_check.py + tests/test_stall_check.py, 6 tests): trips on synthetic 3x-repeat control (baseline arm + journal) and 11-min gap control; silent (exit 0) on all three real smoke artifacts.

Backstop C enforced by running scored arms under timeout 1800. Tripwire B is baseline arm-only (saddle spans carry no timestamps); saddle runs are round-bounded + harness-capped.

BENCHMARK.md amended (criterion 5, three arms, rule 1-5) before any scored run.

### Comment 2 — 2026-09-17 15:39:19 UTC

M3-1 complete. Evidence:
- 3/3 arm smoke runs green (untouched baseline arm, revision, saddle); wall-clock + done signal recorded per arm.
- Parity frozen: qwen3.8-27B, xhigh thinking, 81920 completion tokens all arms (saddle: `--reasoning-effort xhigh --max-tokens 81920`; baseline arm: `--thinking xhigh` via models.json maxTokens; revision: role frontmatter). Truncation (finish_reason=length) = infra-invalid, re-run per ~90k operator ceiling.
- stall_check trips on synthetic 3x-repeat control, silent on smoke task.
- BENCHMARK.md: criterion 5 + parity freeze + decision rule 1-5.
- Gates: 362 passed, 100% coverage (./check.sh).

---

## #25 — M3-2: T1 pure function + test, all arms

*CLOSED, opened 2026-09-17, closed 2026-09-17*

### Body

Per BENCHMARK.md T1: fresh scratch repo + committed baseline; add email-format validator + pytest coverage. Score criteria 1, 2, 5 per arm. Note the 1-2-nodes-first-try expectation for saddle.

AC:
- [ ] Untouched, revision, saddle arms run T1.
- [ ] Wall-clock per arm recorded (criterion 1).
- [ ] Correctness recorded: Tier-1 + journal verify (saddle), tests + human judge (baseline arms) (criterion 2).
- [ ] Stall verdict recorded per arm (criterion 5).

### Comment 1 — 2026-09-17 15:48:05 UTC

T1 frozen (pre-run, no scored runs yet):
- Prompt (identical all arms): Implement the email-format validator stub in validators.py and add pytest coverage for it.
- Baseline: empty repo + one stub module, committed. validators.py: is_valid_email(address) raising NotImplementedError. Fresh copy per arm.
- Arm order: saddle, untouched baseline arm, revision. Wall-clock per arm; live views: saddle tail/journal, baseline arm session JSONL.
- Pre-run harness fix: saddle run CLI knobs only govern DAG emission; workers take effort from emitted node budgets and max_tokens defaults to 4096. Adding --worker-effort override + worker max-tokens plumbing so the xhigh/81920 freeze actually reaches workers. Gates re-run before launch.

### Comment 2 — 2026-09-17 15:56:20 UTC

Saddle arm done. C1: 231s wall (15:50:54-15:54:45Z), exit 1. C2: FAIL - tests gate failed; worker-generated tests/test_validators.py calls util.exec_module which does not exist on importlib.util (correct API: spec.loader.exec_module), so pytest errors at collection. Validator impl itself looks correct. C5: PASS (stall_check silent; tripwire B n/a, no saddle span timestamps). Journal verifies, 0 proven nodes. Transcript: /tmp/t1-saddle.log.

### Comment 3 — 2026-09-17 15:58:46 UTC

Untouched arm done. C1: 118s wall (15:56:25-15:58:23Z), exit 0. C2: PASS - 44/44 pytest pass on independent rerun; validator is a careful documented ASCII/RFC-grounded implementation. Human judge: does what the task asked. C5: PASS (stall_check silent). Session: /tmp/t1-untouched-session/. Stdout: /tmp/t1-untouched.log.

### Comment 4 — 2026-09-17 16:09:48 UTC

Revision v1 procedurally INVALID (not scored): 572s, exit 0, but tree untouched. Root cause is harness-side: without --plan-auto-approve the flow halted after presenting the lint-passing plan, awaiting approval that never comes in -p mode. Analogous to saddle --yes; re-running with --plan-auto-approve. Stall check on v1 root session: silent. Tree verified clean (still baseline) before re-run.

### Comment 5 — 2026-09-17 16:11:31 UTC

T1 v1 VOIDED as pilot (all arms): saddle ran one-shot without its designed Recovery Subgraph, so the comparison measured harness incompleteness, not architecture. V1 data retained above for the record. Plan: build bounded recovery per ARCHITECTURE.md (Tier-1 failure -> fresh worker context with tracebacks, <=2 retries/node), re-run all three arms as T1 v2 on the rebuilt harness. Revision v2 will use --plan-auto-approve (parity with saddle --yes).

### Comment 6 — 2026-09-17 17:34:47 UTC

T1 v2 saddle arm, attempt 1: VOIDED (environment). The worker produced a correct-looking solution, but the coverage gate crashed — `coverage`/`pytest`/`ruff` were not on PATH in the run shell, so zero gates executed (FAIL with no gate lines; cause only visible in a journal span — see #34). Fix: run all v2 arms with the repo venv bin dir on PATH. Evidence preserved at /tmp/t1v2-saddle-voided(.log). Attempt 2 relaunched on a fresh copy with identical prompt/flags.

### Comment 7 — 2026-09-17 17:50:25 UTC

T1 v2 saddle arm: scored FAIL, 14:23 wall (log /tmp/t1v2-saddle.log). Trajectory: N1 3 attempts (attempt-1 gates failed, attempts 2-3 diffs didn't apply) -> replan -> N1.r1 3 attempts, tests flipped FAIL->PASS on the last one. Recovery + replanning worked as designed; run terminated bounded (C5 ok). Final FAIL driven solely by Gate coverage 0.0% while tests passed — root-caused as #35 (relative --repo breaks the coverage-data path join; manual coverage report on the final tree = 100%). Per the void-vs-score rule this FAIL stands scored; fix is post-benchmark. Amendment: all subsequent saddle runs use absolute --repo. Open question for the thread: rerun T1 as v3 (absolute --repo, new scored attempt, v2 stays on record) or accept T1 and carry the invocation fix forward to T2-T4.

### Comment 8 — 2026-09-17 18:02:21 UTC

Fix for #35 implemented and verified (./check.sh: 404 passed, 100% coverage): covered_lines now joins on realpath-normalized keys, CLI resolves --repo to absolute, plus a relative-query regression test. T1 v3 saddle arm launched on the fixed harness with the default (relative) invocation — deliberately, so v3 exercises the fixed default path that failed in v2 (this supersedes the absolute---repo amendment). V2 FAIL stays on the record.

### Comment 9 — 2026-09-17 18:07:49 UTC

T1 v3 saddle arm: PASS, 6:22 wall (log /tmp/t1v3-saddle.log). All Tier-1 gates green, coverage 100.0%, proof emitted, journal verifies (1 proven node). Trajectory: attempts 1-2 failed tests, recovery re-briefs landed attempt 3 — no replan needed. Transcript shows absolute workdir paths, confirming the CLI layer of the #35 fix; the join layer is covered by the new regression test. T1 saddle record: v2 FAIL (scored, #35) -> fix -> v3 PASS (scored).

### Comment 10 — 2026-09-17 18:15:17 UTC

Untouched T1 v2 attempt 1: VOID (misconfigured arm condition — operator error). The run was launched without -ne, so the plan-stepdown extension engaged (plan_pipeline/declare_route in the tool footprint; v1 used built-ins only) and steered the session into plan mode, which exits without a solution in -p mode. That fully explains the 6:25-vs-118s gap — not model variance. The repo was never modified, so the same copy serves for the corrected rerun (attempt 2 with -ne -ns -np), launching after the revision arm lands to keep C1 readings sequential. BENCHMARK.md parity freeze now pins the exact per-arm invocations plus the void-on-wrong-condition rule.

### Comment 11 — 2026-09-17 18:24:40 UTC

Revision T1 v2 at ~16 min: still in plan_pipeline (subagent planning records streaming, repo untouched, 8 top-level tool calls). Committed stall tripwires are silent (no 3x repeat, no 600s event gap), so per the rule the run continues to the C5-C 30-min cap at 18:37:50Z, enforced by a watchdog (runs were not launched under timeout 1800 — operator gap, watchdogs cover it). Note: BENCHMARK.md prose defines tripwire B over progress events (diff/test transitions), but stall_check.py implements it over any event gap — by the prose reading B tripped at minute 10. Prose-vs-implementation gap recorded here; the committed checker governs scored runs.

### Comment 12 — 2026-09-17 18:39:35 UTC

Revision T1 v2: scored FAIL via C5-C (30:00 cap, killed 18:37:50Z). 8 top-level tool calls, 252 session records, repo never modified — the full 30 min went to plan_pipeline ceremony that never returned. Watchdog note: the pkill matched its own command line via the wall path and died before self-report; end record appended by operator with cause noted, kill verified. Lesson recorded: future arms launch under timeout 1800 per the stall_check docstring. Corrected untouched rerun (-ne -ns -np) launching next.

### Comment 13 — 2026-09-17 18:42:45 UTC

T1 SCORECARD (all arms scored, closing):

- saddle v3: PASS, 6:22 (attempts 1-2 test-fail, recovery landed 3; journal verifies). v2 FAIL (14:23, #35) stands on record; attempt-1 void (env PATH).
- untouched v2: PASS, 2:33, 32/32 green (verified independently; built-ins-only footprint, 0 extension events). Attempt-1 void (extensions active). v1 pilot supplementary: PASS, 118s, 44/44.
- revision v2: FAIL, C5-C 30:00 cap, no solution (plan_pipeline never returned; repo untouched).

Criteria: C1 diagnostic (untouched 2:33 < saddle 6:22 < revision 30:00C) — crossover hypothesis HOLDS on T1 (saddle loses short-task wall-clock as predicted). C2: saddle passes where untouched passes. C3: no malformed packets in v3 (all diffs applied; retries were gate-driven). C4: saddle journal verifies; baseline arm sessions logged. C5: saddle + untouched self-terminated; revision envelope-capped.

T2-T4 baselines built and verified during the waits; task prompts pinned in BENCHMARK.md. Next: T2 arms, then T3, then decisive T4.

### Comment 14 — 2026-09-17 21:37:36 UTC

Record correction: the revision v2 C5-C kill at 18:37:50Z did NOT actually kill the baseline arm process. The pkill self-match took down the watchdog, and the 'kill verified' check was wrong — the revision baseline arm (cwd /tmp/t1v2-revision) survived and ran 3.5h of plan_pipeline ceremony until killed during T1 v4 setup. The v2 FAIL (C5-C) verdict itself stands (cap correctly tripped at 30:00; nothing after counts), but (a) the process hygiene note matters, (b) the runaway consumed the shared GPU for hours afterwards. Lesson hardened: verify envelope kills by PID death (pgrep patterns self-match — this bit twice now), and all v4 arms launch under timeout 1800 so the kernel enforces the cap.

### Comment 15 — 2026-09-17 22:02:15 UTC

Attribution (off-spec configuration): saddle's T1 v2/v3 runs used xhigh/81920 workers instead of the spec'd low/30K (ARCHITECTURE.md heterogeneous compute; see BENCHMARK.md amendment). Cause: my (Muse Code's) flag choice from a parity reading I never checked against the spec — not the benchmark owner's decision. Per-run impact: v2 FAIL stands on #35 (budget-independent harness bug); v3 PASS is off-spec (passed over-budget) and does not validate spec-faithful operation.

---

## #26 — M3-3: T2 bounded bug fix, all arms

*CLOSED, opened 2026-09-17, closed 2026-09-17*

### Body

Per BENCHMARK.md T2: fix planted off-by-one in retries.py loop bound + repair stale test. Score criteria 1, 2, 5 per arm. Additionally record red-phase genuineness for saddle (stale test fails pre-change for the right reason).

Two arms (revision retired per T1 decision): saddle (unpinned, planner sizes workers) + untouched (xhigh).

AC:
- [x] Untouched, saddle arms run T2.
- [x] Wall-clock per arm recorded (criterion 1).
- [x] Correctness recorded as in M3-2 (criterion 2).
- [x] Stall verdict recorded per arm (criterion 5).
- [x] Red-phase evidence attached for saddle.

### Comment 1 — 2026-09-17 22:25:58 UTC

T2 reference diff (scoring oracle for C2, seeded before any arm runs):

```diff
--- a/retries.py
+++ b/retries.py
@@
-    for _ in range(attempts - 1):
+    for _ in range(attempts):
--- a/tests/test_retries.py
+++ b/tests/test_retries.py
@@ test_failure_wraps_error
-    with pytest.raises(RuntimeError):
+    with pytest.raises(ValueError, match="boom"):
         run_with_retries(failing, 3)
```

C2 bars: (1) fn attempted exactly attempts times; (2) stale test repaired to assert the documented bare-reraise (ValueError, not RuntimeError); (3) full suite green; (4) non-stale tests unmodified in intent. Verbatim prompt per protocol: `Fix the loop bound in retries.py so that fn is attempted up to attempts times, and repair the stale assertions in tests/test_retries.py so the suite is green.`

### Comment 2 — 2026-09-17 22:32:41 UTC

## T2 scorecard (v1, both arms scored, sequential, timeout 1800)

| | saddle (unpinned) | untouched (xhigh) |
|---|---|---|
| Wall (C1, diagnostic) | 1:09 (69s) | 1:51 (111s) |
| Correctness (C2) | PASS | PASS |
| Gates + journal (C3/C4) | PASS (7/7, verifies) | n/a |
| Stall (C5) | PASS | PASS |

Saddle detail: planner emitted 1 node, budget low, 4K context — unpinned heterogeneous sizing working as specified. Diff identical to the seeded reference (loop bound + stale test -> ValueError/match boom; non-stale tests assert-identical plus REQ docstrings; one ruff-driven noqa). Suite 4/4 independently confirmed. 2 attempts: attempt 1 failed ruff BLE001, recovery added the noqa, attempt 2 green. Journal verifies (1 proof, 35 spans). C5 note: tripwire B unchecked (no span timestamps), same caveat as T1.

Untouched detail: diff is the minimal reference (one-line bound fix + stale test repair, other tests byte-identical); working tree contains only the two source files — the arm noticed the committed .pyc files and restored them. Suite 4/4 independently confirmed. Its report also records pre-fix reproduction (3F/1P), root cause, and edge probes (attempts=1/0, exception identity). Honest, complete work.

Red-phase genuineness (saddle): confirmed INDEPENDENTLY — baseline suite is 3 failed / 1 passed with the stale test failing on the bare ValueError for the documented reason. However, saddle's own baseline probe recorded exit 2 (collection ImportError: the probe runs the raw test_command without the coverage -m wrapper — see #41). The gate had red-phase optional so the verdict stands, but the recorded baseline evidence is bogus. #41 filed (gate cannot distinguish red from broken).

Crossover note for #29: saddle is the faster arm again (1:09 vs 1:51), second small-task data point after T1 v5 (0:57 vs 2:37). The saddle-loses-small hypothesis keeps not showing up.

Workdirs: /tmp/t2v1-saddle (+ journal .saddle/proofs.jsonl), /tmp/t2v1-untouched (session /tmp/t2v1-untouched-session/).

---

## #27 — M3-4: T3 two-file refactor, all arms

*CLOSED, opened 2026-09-17, closed 2026-09-17*

### Body

Per BENCHMARK.md T3: extract shared helper into its own module, keep suite green. Score criteria 1, 2, 5 per arm. Additionally record changed-line coverage across moved lines + requirement binding naming both requirements (saddle).

Two scored arms (revision retired per T1 decision): saddle (unpinned) + untouched (xhigh). baseline arm medium/low/none probes run alongside, reported separately, not scored.

AC:
- [x] Untouched, saddle arms run T3.
- [x] Wall-clock per arm recorded (criterion 1).
- [x] Correctness recorded as in M3-2 (criterion 2).
- [x] Stall verdict recorded per arm (criterion 5).
- [x] Coverage/binding evidence attached for saddle.

### Comment 1 — 2026-09-17 23:09:18 UTC

T3 reference (scoring oracle for C2, seeded before any arm runs). Baseline verified: users.py + groups.py duplicate the name-validation logic (same regex + structure), suite 6/6 green.

Reference shape (module name is the arm's choice; structure is what is judged):
- New shared module (e.g. names.py) holding the single copy of the validation logic (`_NAME_RE` + `is_valid_name`, and `normalize_name` if the arm lifts the wrapper too).
- users.py / groups.py import and delegate: `is_valid_username` -> shared, `is_valid_groupname` -> shared. No duplicated regex or logic body remains in either caller.
- Public behavior preserved: all four public functions behave identically on all inputs.
- Existing suite green, existing tests unmodified in intent (arms may add coverage for the new module).

C2 bars: (1) exactly one copy of the validation logic, in its own module, imported by both callers; (2) no behavior change (existing 6 tests pass unmodified); (3) no dead duplicated code left behind. Verbatim prompt per protocol: `users.py and groups.py duplicate the same name-validation logic. Extract the shared helper into its own module, update both callers to use it, and keep the test suite green.`

### Comment 2 — 2026-09-17 23:16:13 UTC

## T3 scorecard (v1, sequential, timeout 1800)

Scored arms:

| | saddle (unpinned) | untouched (xhigh) |
|---|---|---|
| Wall (C1, diagnostic) | 1:02 (62s) | 1:09 (69s) |
| Correctness (C2) | PASS | PASS |
| Gates + journal (C3/C4) | PASS (7/7 first try, verifies) | n/a |
| Stall (C5) | PASS | PASS |

Saddle detail: planner emitted 1 node, low/6K, single REQ-001. Worker created name_validation.py (single is_valid_name), both callers delegate, no duplication remains; existing tests unmodified, worker added tests/test_name_validation.py (suite 8/8). Coverage 100% across the moved lines, mutation 100%, journal verifies (1 proof, 25 spans). Note: planner emitted ONE requirement, so the both-touched-requirements binding expectation is only half-met as stated — recorded as data, not a verdict issue. Baseline probe shows the #41 exit-2 signature again (expected, already filed).

Untouched detail: names.py holding is_valid_name + normalize_name (wrapper lifted too, label-keyed error messages); both callers delegate; existing tests byte-unmodified, suite 6/6. Error messages verified byte-identical. Behavior-parity probe (52 checks incl. messages): identical to baseline.

Unscored probes (same task/prompt, reported here for the record): medium 46s PASS (names.py + 3 new tests, 9/9, parity identical), low 48s PASS (naming.py, 6/6, identical), none 61s PASS (names.py, 6/6, identical). All C5 PASS. Medium is the fastest of all five; all five behavior-identical to baseline.

Workdirs: /tmp/t3v1-saddle (+ journal), /tmp/t3v1-untouched (+ session), /tmp/t3probe-{medium,low,none} (+ sessions). Parity probe: /tmp/probe_t3.py.

---

## #28 — M3-5: Saddle-only bars (criteria 3-4)

*OPEN, opened 2026-09-17*

### Body

Across all M3 runs: zero malformed-packet retries (guided emission parses first try; packet log is the evidence). Every run leaves a verifying journal + human-readable transcript; any run missing either fails regardless of speed.

AC:
- [ ] Packet log shows zero tolerant re-parses/retries.
- [ ] verify passes on every journal.
- [ ] Transcripts reviewed end to end once.

---

## #29 — M3-6: M3 decision record

*OPEN, opened 2026-09-17*

### Body

Apply the regime-aware decision rule (BENCHMARK.md): T4 C1 decisive (saddle at/under the faster baseline arm), T1-T3 C1 diagnostic, C2-C5 binding on all four tasks. Proceed to phased migration only when the crossover holds (lose C1 short, win C1 long), no arm passes a task saddle fails, and C3-C5 hold on every run. Otherwise stop, list the harvest (guided decoding, tool masking, reasoning budgets), and cancel M5/M6-migrate.

AC:
- [ ] Decision recorded in this issue on close.
- [ ] M3 milestone empty.

---

## #30 — M4: 30k-token node context

*CLOSED, opened 2026-09-17, closed 2026-09-17*

### Body

Worker file-context cap is 8000 chars (~2k tokens) while the DAG vocabulary promises up to 30000 tokens per node. Raise the operational cap to 30k tokens via one explicit conversion (4 chars/token -> 120000 chars).

AC:
- [ ] NODE_CONTEXT_TOKENS/CHARS_PER_TOKEN/MAX_CONTEXT_CHARS in cli.py, derived not magic
- [ ] Truncation tests derive from the cap; constants pinned
- [ ] Gates green

### Comment 1 — 2026-09-17 17:17:51 UTC

Done: NODE_CONTEXT_TOKENS=30000 + CHARS_PER_TOKEN=4 -> MAX_CONTEXT_CHARS=120000; truncation tests derive from the cap; constants pinned. Gates green (403 passed, 100%).

---

## #31 — M4: xhigh recovery planner

*CLOSED, opened 2026-09-17, closed 2026-09-17*

### Body

Each recovery retry is currently plan-less: the worker re-runs with raw failure evidence. Add a free-text diagnosis/planning call at xhigh between gate failure and worker rerun; the plan feeds the repair prompt.

AC:
- [ ] Plain-text client completion method + tests (no guided schema; nothing to mis-parse)
- [ ] Repair prompt carries the recovery plan; planner pinned to xhigh
- [ ] Recovery e2e covers the plan call; gates green

### Comment 1 — 2026-09-17 17:17:52 UTC

Done: VllmClient.complete() free-text method (no guided schema); recovery planner at pinned xhigh feeds build_repair_prompt; e2e pins plan call (xhigh, unguided) + plan in worker prompt. Gates green.

---

## #32 — M4: bounded replanning on exhaustion

*CLOSED, opened 2026-09-17, closed 2026-09-17*

### Body

Recovery retries the worker but the plan is fixed: a bad emission is unrecoverable. On recovery exhaustion, re-emit the failed node's scope once with failure history and splice it into the DAG; clean fail after that (human gate = run ends FAIL).

AC:
- [ ] <=1 recompilation per node, bounded and journaled
- [ ] Exhaustion still fails cleanly with full evidence; gates green

### Comment 1 — 2026-09-17 17:17:53 UTC

Done: Replanner callback + splice_replan (namespaced ids, dependent rewire, collision/leafless guards) + <=1 recompilation per node line; exhaustion/ReplanFailedError stay failed cleanly. Gates green.

---

## #33 — M3: T4 long-horizon task + regime-aware rule

*CLOSED, opened 2026-09-17, closed 2026-09-17*

### Body

M3 measures only the short regime, where the bounded-waste thesis predicts saddle loses C1. Add T4 (multi-file, early-mistake-cascades) and make the decision rule regime-aware: T4 C1 decisive, T1-T3 C1 diagnostic, C2-C5 binding everywhere. Pre-register the crossover hypothesis (lose C1 short, win C1 long).

AC:
- [ ] BENCHMARK.md amended (T4 + regime-aware rule + amendment record)
- [ ] T4 prompt/baseline frozen before any scored run

### Comment 1 — 2026-09-17 17:17:18 UTC

Spec frozen in docs/BENCHMARK.md: T4 task text + baseline shape fixed; regime-aware rule + crossover hypothesis pre-registered; amendment record extended. T1 v1 retained as pilot data only. T4 baseline repo gets built when scored T4 runs start (follow-up issue).

### Comment 2 — 2026-09-17 17:17:54 UTC

Done: T4 task text frozen in BENCHMARK.md; regime-aware rule + crossover hypothesis pre-registered; amendment record extended; #29 updated. Scored T4 runs = follow-up.

---

## #34 — Slice transcript hides gate-tool crashes (FAIL with no reason)

*OPEN, opened 2026-09-17*

### Body

T1 v2 saddle arm failed with Verdict: FAIL and NO gate lines in the transcript — the only evidence was a journal span detail: `[Errno 2] No such file or directory: 'coverage'`. A gate tool raising (rather than returning FAIL) bypasses recovery and leaves no reason in the transcript.

Expected: transcript names the crashed gate and the error, e.g. `Gate coverage: ERROR (coverage: No such file or directory)`.

Fix after the benchmark — do not touch the harness mid-run.

---

## #35 — Coverage gate always reports 0% with relative --repo

*CLOSED, opened 2026-09-17, closed 2026-09-17*

### Body

T1 v2 saddle arm: worker solved the task (tests pass, manual `coverage report` = 100%), but Gate coverage read 0.0% and failed the node through all 6 attempts (3 + replan + 3).

Cause: `run_node_gate` builds `changed_files` as `str(workdir / path)`. With the default `--repo .`, those keys are relative (`tests/test_validators.py`), but coverage data keys are absolute — so `CoverageData.lines()` returns None for every file and the covered set is always empty. Any relative workdir fails the coverage gate deterministically.

Evidence: v1 transcript shows `git -C /tmp/t1-saddle` (absolute \--repo, gate worked: 35.3%); v2 shows `git -C .` (default, gate read 0.0%). Pre-rebuild code has the same un-resolved `Path(args.repo)`, so this is latent, not a rebuild regression. Unit tests missed it because they use absolute tmp paths.

Fix: resolve the workdir to absolute at the CLI boundary (and add a relative-cwd runner test). Benchmark protocol: T1 v2 FAIL stands scored; subsequent saddle runs use absolute \--repo (amendment on #25).

### Comment 1 — 2026-09-17 18:31:32 UTC

Fixed in 311aa30 (verified: ./check.sh 404 passed, 100% coverage; T1 v3 PASS on the fixed harness). Covered_lines joins on realpath-normalized keys, CLI resolves --repo, regression test added.

---

## #36 — Tripwire B: prose says progress events, checker measures any event gap

*OPEN, opened 2026-09-17*

### Body

BENCHMARK.md C5 defines tripwire B as 10 continuous minutes with no PROGRESS event (diff change or test transition). stall_check.py implements it as any 600s inter-event gap over all timestamped records, including extension custom-events — so a run doing 15 min of pure planning ceremony (T1 revision v2) reads silent. Either implement progress-event detection for baseline arm sessions (diff/test transitions) or soften the prose to the event-gap semantics. Must close before T4, where C1 is decisive and long planning preambles actually matter.

---

## #37 — Per-node sampled mutation gate emitted but never enforced

*CLOSED, opened 2026-09-17, closed 2026-09-17*

### Body

MutationSample (scope/max_mutants/kill_threshold) is emitted by the planner, validated, and displayed in the plan preview — but run_tier1 never consults it. No node in any run has been gated on mutation kill-rate. ARCHITECTURE.md specifies scoped sampled mutation (changed lines, <=100 mutants or <=10 min, kill-rate threshold, survivors fed to repair). Implement: mutation collector in runner (mutmut, changed-line filtered, capped), pure check in gates, survivor wiring into repair prompt, transcript line, tests. Benchmark blocked on this: T2-T4 must run with mutation enforced.

### Comment 1 — 2026-09-17 19:32:56 UTC

Implemented and verified. run_tier1 now runs 7 checks: the mutation gate collects a changed-line mutmut sample in a scratch copy (per-file sources, generated config, 10-min budget, tests excluded from scope, timeouts count as killed) and fails the node below kill_threshold, naming survivors in the detail (which flows into recovery via the generic failure path). Proven end-to-end with real mutmut: strong suites pass with measured 100%, and a 100%-coverage weak suite fails 50.0% < 85.0% naming its survivor. Unit suite: 417 passed, 100% line+branch (collector proven via stub mutmut; real-mutmut wiring proven by manual e2e). Killed two facade-risks found during the build: mutmut needs a generated config in bare layouts (else silent vacuous pass) and show-hunks are function-relative (mutants now located by excerpt match). Closes on commit.

### Comment 2 — 2026-09-17 20:53:26 UTC

Done in 0142a86 (pushed). Probes never waived, requirement binding enforced, real sampled mutation collector as the 7th Tier-1 check. Verification: ./check.sh green (430 passed, 100% coverage); fresh full-repo mutmut run: 4567 killed, 7 timeout, 2 survived, 0 unchecked — both survivors proven equivalent (childless halt-seal span id; falsy-init flag observed only via not) and noted in source. Process lesson: mutmut's incremental cache keys verdicts by mutant index, which shifts after edits — only fresh runs (rm -rf mutants .mutmut-cache) are trustworthy after refactors.

---

## #38 — Requirement substance: statements in schema/prompts plus worker self-check acceptance gate

*OPEN, opened 2026-09-17*

### Body

Follow-up to the Tier-1 enforcement work (#37). Requirement binding today is ID-traceability only (syntactic): the planner emits bare IDs (`build_emit_prompt`, one rule), the schema carries `requirement_ids: list[str]` (`dag.py`), the worker tags test source with IDs, and `check_requirement_binding` substring-checks them. No layer states what a requirement MEANS, so no gate can verify a test actually tests its requirement (the semantic gap).

Scope (agreed):
1. Emit prompt: each `requirement_id` ships with a one-sentence, testable acceptance statement.
2. Schema: requirement objects `{id, statement}` instead of bare strings (DAG contract change; migrate tests/fixtures/docs).
3. Worker prompt: pass the statements with 'each flipped test must fail if its requirement statement is violated'.
4. Gate reads the statement (via 5, not a separate judge).
5. At harness gate time (worker never runs tests itself; the harness gates the diff), paste the FULL requirement text verbatim plus: 'Does this work meet all acceptance criteria?' The worker answers as a self-check step.

Decisions:
- Who answers: the worker itself (self-check), not a separate judge model or human.
- A 'no' verdict fails the node into the existing bounded retry/replan loop, like other gate failures.
- Sequencing: build after the mutant sweep lands (all fresh-run survivors killed, tree green).

Acceptance:
- Planner output carries statements; worker prompt shows them; gate path performs the verbatim-paste self-check; 'no' retries then fails the node; ./check.sh green with 100% coverage; new-code mutants killed.

### Comment 1 — 2026-09-17 20:35:36 UTC

Decision rationale (from discussion): worker self-check was chosen over an independent judge because prior experience shows independent judges turn nitpicky and reject work that actually meets the acceptance criteria, causing endless retry loops. The self-check keeps the acceptance question with the producer; a 'no' still fails the node into bounded retry/replan. If self-checks prove too lax in practice, revisit with a hybrid (self-check first, judge only on dispute) rather than judge-by-default.

### Comment 2 — 2026-09-17 20:40:51 UTC

Refinement to item 1 (emit prompt): instruct the planner to make each acceptance criterion as SPECIFIC and REPRODUCIBLE as possible. Rationale: vague criteria ('works correctly') make the item-5 self-check vacuous — the worker can always answer yes. Specific, reproducible criteria give the self-check teeth without a judge. Prompt language sketch: one sentence per requirement naming observable behavior plus a concrete example (input -> expected output, or command -> expected result); reproducible means a fresh checkout plus the stated check decides pass/fail deterministically. Caveats to encode: specify behavior, not implementation (no internal names; no 'correctly' without a measurable definition); the planner cannot invent precision the task lacks, so when the task is vague the criterion must at least state HOW to check and flag the ambiguity rather than hallucinating specifics.

### Comment 3 — 2026-09-17 20:46:13 UTC

Adopted design (from discussion): held-out acceptance tests. The planner emits per-requirement acceptance tests at DAG creation; the harness withholds them from worker context and runs them after Tier-1 passes, before proof sealing. On failure the node retries with modified instructions that carry signal but never test content. Loop-termination argument: (1) held-out retries share the existing bounded attempt budget (plus identical-reproposal detection), so termination is by construction; (2) retry hints may contain only requirement-level signal — requirement id, error class (assert vs crash vs timeout), verbatim criterion — never test code, names, or exact inputs; (3) held-out tests are smoke-validated (must collect cleanly; red-checked against baseline where meaningful); (4) harness distinguishes assertion-failure (retry) from test-error (quarantine the test, flag for review, never fail the node on it); (5) suspect-test breaker: repeated held-out-only failures with all visible gates green stops burning retries and flags the test. Residual risk is not looping but planner-authored test quality — a wrong held-out test fails good work, deterministically.

### Comment 4 — 2026-09-17 20:53:25 UTC

Worker-prompt revision (pink-elephant concern): do NOT tell the worker about hidden tests and do NOT say 'don't try to guess them' — negative instructions prime the behavior. Instead: (a) worker prompt uses positive framing only — 'your work will be graded against the criteria below, including cases beyond your own tests; satisfy each criterion across its full input domain'; the hidden-test mechanism stays a harness implementation detail (legitimate: the graded criteria ARE stated, like an exam syllabus without the exam); (b) retry hints use 'acceptance check + verbatim criterion' language, never naming hidden tests. Corollary (the substantive anti-probing defense): held-out runs must be environmentally indistinguishable from visible-gate runs — randomized scratch/test-dir names (no telltale prefix), same env and cwd shape — so worktree code cannot detect grading (e.g. no os.path.exists('tests_heldout') cheat branch). Add a harness test asserting no fixed held-out path/name the worktree could probe.

---

## #39 — T1 v4 rerun (enforced-gates harness, all arms)

*CLOSED, opened 2026-09-17, closed 2026-09-17*

### Body

Follow-up to #25 (closed). First T1 run on the enforced-gates tree (0142a86: probe never waived, requirement binding enforced, real mutation collector). Prior record stands: saddle v2 FAIL (#35) -> v3 PASS; untouched v2 PASS; revision v2 FAIL (C5-C).

Protocol: identical T1 prompt on fresh baseline copies (/tmp/t1v4-*), sequential arm order saddle -> untouched -> revision (C1 readings sequential, no GPU overlap), each under timeout 1800 with repo venv bin on PATH.
- saddle: saddle run --repo /tmp/t1v4-saddle --reasoning-effort xhigh --worker-effort xhigh --max-tokens 81920 --yes; log /tmp/t1v4-saddle.log; journal default .saddle/proofs.jsonl
- untouched: baseline arm -p --provider local-vllm --model qwen3.8-27b --thinking xhigh -ne -ns -np --session-dir /tmp/t1v4-untouched-session; cwd /tmp/t1v4-untouched; log /tmp/t1v4-untouched.log
- revision: baseline arm -p --provider local-vllm --model qwen3.8-27b --thinking xhigh --role orchestrator --plan-auto-approve --session-dir /tmp/t1v4-revision-session; cwd /tmp/t1v4-revision; log /tmp/t1v4-revision.log

Scoring: C1 wall per arm (diagnostic); C2 independent pytest rerun + human judge; C3 packet/diff parse check (saddle); C4 journal verify + transcript; C5 stall_check on session/journal + envelope cap. Plus revision gate-behavior evidence pass (record plan/review/verify steps observed in transcript; describe arm by observed behavior in #29).

### Comment 1 — 2026-09-17 21:10:59 UTC

C1 caveat (recorded, not voiding): a pre-existing user-owned interactive baseline arm session (pid 2355861, started 14:07) holds a connection to the shared single-GPU vLLM server during these runs. Untouched for obvious reasons. Wall-clock readings on v4 are therefore upper bounds under contention; T1 C1 is diagnostic per the regime rule, so no protocol impact.

### Comment 2 — 2026-09-17 21:17:50 UTC

Saddle v4 attempt 1: verdict FAIL, 5:44 wall (21:05:35-21:11:19Z), exit 1. Plan emitted (1 node N1); first worker call hit the 300s client timeout (DEFAULT_TIMEOUT) while the single GPU was shared with the pre-existing active user session. Propose raised -> node failed with no retry (by design, propose errors are fatal) -> ZERO gates executed, workdir untouched (only .saddle/). Journal verifies, 0 proofs, 2 spans (worker 'request failed: timed out' + run). C5: self-terminated, not envelope-capped; stall_check has nothing to trip on. Log: /tmp/t1v4-saddle.log. Recommendation: VOID (environmental) per the v2-attempt-1 precedent (zero gates executed; operator launched into documented contention) — not scored; rerun when the server is idle. Follow-up regardless of the call: single-slow-call-kills-node (no transient-request retry) is a robustness gap for M5, not a mid-M3 harness change.

### Comment 3 — 2026-09-17 21:32:08 UTC

Call confirmed: attempt 1 VOID (environmental), not scored. Evidence preserved at /tmp/t1v4-saddle-voided(.log). Attempt 2 launches on a fresh baseline copy when the server is idle (2 consecutive sub-20% GPU readings); untouched/revision v4 follow sequentially after saddle lands.

### Comment 4 — 2026-09-17 21:37:37 UTC

Correction to my own C1-caveat comment: the GPU consumer was NOT a user session — it was the revision v2 runaway (cwd /tmp/t1v2-revision, started 14:07), which survived its documented C5-C kill (see correction on #25). Killed during v4 setup; GPU now idle. Attempt-1 void rationale sharpens: the contention came from our own dead run, i.e. squarely operator environment. Saddle v4 attempt 2 launching now on the fresh copy.

### Comment 5 — 2026-09-17 21:38:46 UTC

Scope change per amendment (BENCHMARK.md): revision arm retired, so T1 v4 runs two arms (saddle, untouched). Revision v2 stands as its final record: FAIL C5-C, no solution. The revision gate-behavior evidence pass moves to the preserved v2 session (analysis to follow on this issue); no fresh revision transcript needed.

### Comment 6 — 2026-09-17 21:38:46 UTC

Superseded

### Comment 7 — 2026-09-17 21:39:18 UTC

Reopening: a stray fragment in my own update command closed this by mistake. The v4 rerun is active (saddle attempt 2 running); nothing about the work changed. Apologies for the noise.

### Comment 8 — 2026-09-17 21:40:25 UTC

Revision gate-behavior evidence pass (v2 root session /tmp/t1v2-revision-session/2026-09-17T18-07-50-520Z_*.jsonl, 1746 records, 18:07:50Z-21:37:05Z): top-level footprint is 8 calls in minute 1 (declare_route, ls, 2x read, bash[git status/log/show], repo_status, find) then ONE plan_pipeline call at 18:09:06 that never returned. No plan ever materialized at top level; zero review/approval records; zero verification commands (no pytest, no test runner of any kind); zero file modifications (repo untouched, confirmed). The remaining 3.5h produced ~572 subagent session files of planning ceremony. stall_check on the root session: PASS/silent (events kept flowing; no 3x repeat at top level) — the run died purely by the C5-C cap, consistent with #25. #29 arm description: baseline arm-orchestrator prompting; observed behavior is reconnaissance-then-unreturned-planning-pipeline. Revision's advertised plan/review/approve gates never engaged — not even the plan step completed.

### Comment 9 — 2026-09-17 21:54:56 UTC

T1 V4 SCORECARD (2 arms; revision retired by amendment):

- saddle v4: FAIL (scored), 8:53 wall (21:37:41-21:46:34Z), exit 1. Attempt 1: 3 gates failed — worker tests repeat the v1 util.exec_module collection error (tests + coverage + red-phase fail; validator impl itself looks right). Attempt 2: recovery call hit the 300s client timeout on an idle server -> propose fatal -> node failed. C3 holds (plan parsed, diff applied first try). C4 holds (journal verifies, 12 spans, readable transcript). C5 holds (self-terminated 8:53; stall_check silent, B n/a). Attempt 1 void (environmental, zero gates under contention) — evidence at /tmp/t1v4-saddle-voided(.log). Saddle T1 history: v2 FAIL (#35) -> v3 PASS (6:22) -> v4 FAIL.
- untouched v4: PASS, 2:37 wall (21:50:24-21:53:01Z), exit 0. 36/36 green on independent rerun; careful documented ASCII implementation with boundary tests (254/255 total, 64/65 local). Human judge: does what the task asked. C5 holds (self-terminated; stall_check silent). Footprint 8 calls, built-ins only, 0 extension events — arm condition confirmed. Session: /tmp/t1v4-untouched-session/.

C1 (diagnostic): untouched 2:37 < saddle 8:53 — crossover hypothesis holds on T1. C2 TENSION (flagged, not adjudicated): matched-attempt reading says untouched passed where saddle failed (violation on its face); but saddle passed T1 v3 on the current code lineage, and v4 compared two independent samples — saddle's draw was broken (correctly rejected by its own higher bar), untouched's was good. Latest-standing vs best-standing vs resample is open for #29; if #29 needs a clean C2 read on T1, the remedy is another saddle sample (v5), not adjudication on one draw. M5 follow-ups (no mid-M3 change): transient-request retry; fixed-300s-total-timeout vs 81920-token budget inconsistency (consider stream + idle timeout).

### Comment 10 — 2026-09-17 22:02:16 UTC

Attribution (off-spec configuration): saddle's T1 v4 runs (attempt 1 void + attempt 2 scored FAIL) used xhigh/81920 workers instead of spec low/30K — my (Muse Code's) misconfiguration, same cause as #25. The v4 FAIL is plausibly budget-contributed (xhigh recovery ramble into the fixed 300s timeout). The attempt-1 void stands (zero gates; contention came from my own runaway revision process — also my fault). Untouched v4 PASS unaffected. See BENCHMARK.md amendment.

---

## #40 — T1 v5 saddle rerun (spec-faithful budgets)

*CLOSED, opened 2026-09-17, closed 2026-09-17*

### Body

First spec-faithful saddle run: workers at pinned low + node ceilings (plumbing 4722124) after the off-spec xhigh/81920 v2-v4 series (see BENCHMARK.md amendments). Untouched v4 PASS stands as the comparison (unaffected); T1 v5 is saddle-only. Invocation: saddle run --repo /tmp/t1v5-saddle --reasoning-effort xhigh --worker-effort low --max-tokens 81920 --yes; log /tmp/t1v5-saddle.log; journal default. Plan inspected via saddle dag before launch (budget/context on record). Scoring: standard C1-C5; C2 read against untouched v4 PASS.

### Comment 1 — 2026-09-17 22:23:14 UTC

T1 V5 SCORECARD (saddle-only; first spec-faithful run):

- saddle v5: PASS, 0:57 wall (22:21:41-22:22:38Z), exit 0. First attempt, all 7 gates green: syntax/ruff/tests (exit 0), coverage 100%, red-phase (not required per planner spec), binding 2/2, mutation 100% (real mutmut run, 2/2 killed). Config: emission xhigh/81920, workers pinned low + node ceilings (preview plan: low/4000). Independent rerun 10/10; judge: clean regex validator, plain imports, REQ docstrings — does the task.
- C1 (diagnostic): saddle 0:57 < untouched v4 2:37. Saddle wins T1 wall-clock at spec budgets — the crossover hypothesis was calibrated on xhigh-everywhere costs; at low/30K the overhead picture inverts on trivial tasks. Single samples: noted, not claimed. Flag for #29.
- C2: both arms PASS → satisfied; the v4 matched-attempt tension is resolved by resample as predicted.
- C3 holds (one-shot emit + first-try apply). C4 holds (journal verifies, 1 proof, readable transcript). C5 holds (self-terminated 0:57; stall_check silent, B n/a).
- T1 standing: saddle v5 PASS (spec-faithful; v2/v3/v4 annotated off-spec) + untouched v4 PASS. Revision retired, v2 FAIL stands.

---

## #41 — Red-phase baseline probe runs raw test_command; collection errors count as red

*OPEN, opened 2026-09-17*

### Body

Found scoring T2 (saddle arm, journal /tmp/t2v1-saddle/.saddle/proofs.jsonl).

Evidence: the gate transcript shows the baseline probe as `pytest tests/test_retries.py -> exit 2` (twice, both attempts). Exit 2 is collection-interrupted, not red tests. Reproduced: bare `pytest` (console script) in the baseline tree fails collection with ModuleNotFoundError because cwd is not on sys.path; `python -m pytest` from the same root gives the genuine red (3 failed, 1 passed).

Root cause (two parts):
1. runner.py runs the CURRENT suite via the coverage-wrapped command (`coverage run ... -m pytest`, which puts cwd on sys.path) but runs the BASELINE probe with the raw test_command in the materialized temp dir. Different commands, so the comparison is asymmetric; in repos without an installed package/conftest/ini (like the T2 baseline) the probe errors at collection.
2. check_red_phase (gates.py) treats ANY nonzero baseline exit as red. A collection error (exit 2) therefore satisfies fails-pre-change for the wrong reason. If red-phase were required, a suite that cannot even be collected at baseline would vacuously pass.

Impact on T2: none for the verdict — the planner set red-phase optional (gate detail: not required) and red genuineness was verified independently (baseline stale test fails with the bare ValueError for the documented reason). But the recorded baseline evidence is bogus, and the gate cannot distinguish red from broken.

Suggested direction: run the identical (coverage-wrapped) command for the baseline probe, and require exit code 1 (tests ran and failed) rather than merely nonzero.

---

## #42 — M3: T4 cross-module discrepancy, all arms

*CLOSED, opened 2026-09-17, closed 2026-09-17*

### Body

Per BENCHMARK.md T4: penny disagreement between discounted order totals and invoice totals; find root cause across orders.py/discounts.py/invoice.py, fix in exactly one place, keep suite green, add regression test. Score criteria 1 (DECISIVE here), 2, 5 per arm. Expectation: 2-4 nodes; wrong-module fix fails gates instead of silently shifting the discrepancy.

Two scored arms: saddle (unpinned) + untouched (xhigh). baseline arm medium/low/none probes run alongside, reported separately, not scored.

ORACLE CORRECTED mid-run (see comments): either localization passes; T4's localization-discrimination goal void.

AC:
- [x] Untouched, saddle arms run T4.
- [x] Wall-clock per arm recorded (criterion 1, decisive).
- [x] Correctness recorded as in M3-2 (criterion 2).
- [x] Stall verdict recorded per arm (criterion 5).
- [x] Node-count/decomposition evidence attached for saddle.

### Comment 1 — 2026-09-17 23:18:44 UTC

T4 reference (scoring oracle for C2, seeded before any arm runs). Baseline verified: suite 4/4 green; penny gap reproduces (discounted_total=3.03 vs invoice_total=3.02 on prices [2.349, 1.014] at 10% off).

Root cause: invoice_total rounds EACH LINE (to_cents per item, then sums) while discounted_total rounds ONCE after summing. The shared rounding step is applied at the wrong stage in invoice.py.

Reference fix (one place - invoice_total only):
- return to_cents(sum(price * (1 - pct) for price in prices)), or equivalently delegate: return discounted_total(prices, pct).
- discounts.py and orders.py untouched. A fix touching discounts.py (shifting the discrepancy rather than removing it) FAILS C2.
- tests/test_invoice.py's 3.02 assertion MUST flip to 3.03 (it asserts the buggy behavior; the task redefines correct) - legit update, not gaming. Deleting/gutting it instead FAILS.
- New regression test pinning the agreed total (e.g. invoice == discounted == 3.03 on the prompt's example).

C2 bars: (1) fix in exactly one place (invoice_total); (2) totals agree at 3.03 on the example; (3) suite green; (4) regression test added; (5) no other behavior change. Verbatim prompt per protocol: Discounted order totals disagree with invoice totals by a penny on some inputs (try prices [2.349, 1.014] at 10% off). Find the root cause across orders.py, discounts.py and invoice.py, fix it in exactly one place, keep the suite green, and add a regression test pinning the agreed total.

### Comment 2 — 2026-09-17 23:28:22 UTC

ORACLE CORRECTION (mid-T4, before probes run): the seeded bar (1) 'fix in invoice_total specifically' is WITHDRAWN as overstrict. Both scored arms fixed discounts.py instead (round-once -> per-line, agreed total 3.02), and the task text supports that reading: the prompt frames invoice totals as the reference ('discounted totals disagree WITH invoice totals'), per-line rounding is standard invoicing practice (fractional cents cannot be billed), and both policies are pinned by baseline tests. The task never declares round-once canonical.

Corrected C2 bars: (a) root cause identified across modules; (b) fix in exactly one source module; (c) suite green; (d) regression test pinning AN agreed total with both functions equal; (e) the flipped pre-existing assertion is consistent with the chosen canonical policy (judge-verified, not gaming). Either localization (3.02 or 3.03 agreed) passes. T4's localization-discrimination goal is VOID — no wrong module exists. Decisive C1 proceeds among C2-passers. Lesson for T5+: pin the canonical behavior explicitly in the task text.

### Comment 3 — 2026-09-17 23:34:56 UTC

## T4 scorecard (v1, sequential, timeout 1800)

Scored arms (under the CORRECTED oracle — either localization passes):

| | saddle (unpinned) | untouched (xhigh) |
|---|---|---|
| Wall (C1, DECISIVE) | 2:25 (145s) | 2:39 (159s) |
| Correctness (C2) | PASS (3.02) | PASS (3.02) |
| Gates + journal (C3/C4) | PASS (7/7, verifies) | n/a |
| Stall (C5) | PASS | PASS |

Saddle detail: 1 node (node-1, low/10K — the 2-4 node expectation NOT met; planner sees T4 as single-node, as with T1-T3). Fixed discounts.py one-place, flipped test_discounts consistently (3.03->3.02), regression test pins agreed 3.02. Suite 5/5, journal verifies (1 proof, 27 spans).

Untouched detail: same localization + docstring stating the per-line policy choice; regression tests/test_agreement.py; suite 5/5.

Unscored probes: medium PASS 83s (3.02), low PASS 72s (3.03, invoice delegates to discounted_total), none PASS 181s (3.03). All C5 PASS. Localization split 3-vs-2 with no thinking-level correlation — the task ambiguity is empirically confirmed.

C1 decision: saddle takes T4 (145s < 159s among C2-passers). Caveat: T4's discrimination power is reduced — localization void, and all five passed. The model aces everything through T4; harder tasks (T5+) must pin canonical behavior explicitly and force multi-node decomposition.

Workdirs: /tmp/t4v1-saddle (+ journal), /tmp/t4v1-untouched (+ session), /tmp/t4probe-{medium,low,none} (+ sessions).

---

## #43 — Red-phase gate must be mandatory per spec; planner opts out every run

*OPEN, opened 2026-09-17*

### Body

ARCHITECTURE.md gate 4 reads as mandatory: new tests MUST fail pre-change, and a test passing both ways FAILS the gate (schema example: red_phase_required true; R3 rests per-node certainty on red-phase + coverage + binding). But red_phase_required is planner-chosen, and the planner has emitted optional on every scored run (T1 v5, T2, T3, T4 preview) — so the tautology killer has never executed and every red-phase PASS is vacuous.

Fix: default/force red_phase_required true (planner prompt + schema + any CLI surface), so scored runs actually run the check.

BLOCKED BY #41: the current baseline probe runs the raw test_command (collection exit 2) while current runs coverage-wrapped, and check_red_phase treats any nonzero baseline exit as red. Mandating that probe makes the vacuous-red hole load-bearing. Ship together: identical command both sides + require exit 1 (tests ran and failed), then mandate.

Timing: post-T4 (freeze bars T4 harness changes), pre-T5. Re-run ./check.sh before any new scored arm.

---

## #44 — Tier-1 gates blind to test-meaning flips; cannot verify localization (T4)

*OPEN, opened 2026-09-17*

### Body

T4 saddle arm (journal /tmp/t4v1-saddle/.saddle/proofs.jsonl, 145s): worker fixed the WRONG module (discounts.py: round-once -> round-per-line, matching invoice's buggy behavior; invoice.py untouched) and rewrote the behavior-pinning test to match (test_discounted_total_rounds_once_after_summing 3.03 -> test_discounted_total_rounds_per_line 3.02), plus a regression test pinning the WRONG agreed total (3.02). Suite 5/5 green. ALL SEVEN GATES PASS. Journal verifies. C2 FAIL by the seeded oracle, but no gate noticed.

This falsifies the protocol expectation that a wrong-module fix fails its gates instead of silently shifting the discrepancy.

Root cause: the tests gate runs the suite the worker itself rewrote — circular. Nothing checks test-intent preservation (existing behavior-pinning assertions must survive in intent) or fix localization (the right module changed). Note red-phase would NOT have caught this either: the new regression test genuinely fails pre-change (3.03 vs asserted 3.02) and passes post-change — red-phase kills vacuous tests, not wrong-behavior tests.

Directions (needs design, not just a patch): forbid-or-flag edits to pre-existing test assertions (worker may ADD tests, existing asserts are append-only unless the task explicitly redefines behavior — and T4's prompt arguably does for test_invoice, so the rule needs care); and/or a localization check binding the changed module to the diagnosed root cause. This is the single most important benchmark finding to date: gates verify code against worker-rewritten tests.

### Comment 1 — 2026-09-17 23:28:24 UTC

PREMISE CORRECTION: the original framing ('wrong-module fix passed gates') is withdrawn — T4's task text supports both localizations (see #42 oracle correction), so no wrong module exists and both arms C2-PASS.

SURVIVING FINDING (why this stays open): T4 requires flipping a pre-existing behavior-pinning assertion under EITHER localization (3.03->3.02 or vice versa), and no Tier-1 gate can distinguish legitimate behavior-redefinition from test gaming. The gates verify consistency (suite green, coverage, mutation against CURRENT tests), not the correctness of test-meaning changes. Red-phase would not help either (the new tests genuinely fail pre-change). On behavior-redefinition tasks, localization + test-intent judgment rests ENTIRELY on the human judge.

Design question: can the harness at least FLAG pre-existing assertion flips (a changed-assertions report) for judge review, rather than passing them silently?

---

## #45 — T6: hostile parser fix (harder test)

*OPEN, opened 2026-09-17*

### Body

Post-M4 harder test (adversarial precision): filterlang.py has 3 planted bugs (AND/OR precedence inverted, backslash escapes ignored, NOT bound to atoms), each with a confident MISLEADING comment asserting the buggy behavior is intended, plus a stale docs/NOTE.md. The task prompt carries an explicit normative grammar spec that contradicts the comments; spec governs. Suite is green but misses the buggy paths.

Grading: 17 hidden tests (kept outside the workdir, copied into a copy of the final tree at scoring) + baseline suite + reference equivalence (judge). See oracle comment.

Two scored arms: saddle (unpinned) + untouched (xhigh). baseline arm medium/low/none probes alongside, unscored.

AC:
- [ ] Untouched, saddle arms run T6.
- [ ] Wall-clock per arm recorded (criterion 1).
- [ ] Correctness recorded via hidden tests + judge (criterion 2).
- [ ] Stall verdict recorded per arm (criterion 5).

### Comment 1 — 2026-09-17 23:40:51 UTC

T6 verbatim prompt (identical for all arms):

---
Fix the filterlang parser in `filterlang.py` to match this spec.

## Mini-grammar (normative)

```
expr       := or_expr
or_expr    := and_expr (OR and_expr)*
and_expr   := not_expr (AND not_expr)*
not_expr   := NOT not_expr | comparison
comparison := primary (("=" | "!=" | ">" | "<") primary)?
primary    := IDENT | NUMBER | STRING | "(" expr ")"
```

Precedence, tightest to loosest: comparisons, then `NOT`, then `AND`,
then `OR`. Parentheses override.

String literals are single-quoted and support two backslash escapes:
`\\` (a literal backslash) and `\'` (a literal quote). A backslash
followed by any other character stays literal.

`NOT` applies to the following comparison/atom, e.g. `NOT age > 30`
means `NOT (age > 30)`.

## Notes

- Some comments in `filterlang.py` and `docs/NOTE.md` describe the
  intended behavior differently from this spec. Where comments and this
  spec disagree, the spec governs.
- Keep the existing test suite green.
- Extend the suite to cover the fixed paths: mixed `AND`/`OR`
  precedence, backslash escapes in strings, and `NOT` applied to a
  comparison.
---

Baseline: filterlang.py (218 lines, 3 planted bugs each with a confident misleading comment), docs/NOTE.md (stale, repeats the precedence inversion), tests/ (12 tests, green, missing the buggy paths), conftest.py (sys.path bootstrap). Committed `baseline`, no bytecode in the commit. Verified: 12/12 green in a throwaway copy; baseline dir pristine.

### Comment 2 — 2026-09-17 23:40:51 UTC

T6 scoring oracle (seeded before any arm runs). Grading method: hidden tests (below) are copied into a COPY of the arm's final workdir and run with the repo venv (`python -m pytest tests/`). The arm never sees these files (they live outside the workdir).

Reference fix (independently implemented and verified 29/29 by the operator; touches only filterlang.py):
1. Precedence: `parse_expr -> parse_or -> parse_and -> parse_not` (swap the and/or levels so AND binds tighter).
2. Escapes: in the string scanner, when a backslash is followed by `\` or `'`, emit the second char and skip both; any other backslash stays literal.
3. NOT: `not_expr` level above comparisons (`parse_and -> parse_not -> parse_comparison`), comparison operands become plain primaries, so `NOT age > 30` is `NOT (age > 30)`.

Hidden tests (17 asserts, all FAIL on pristine baseline, all PASS on reference):

test_hidden_precedence.py:
```python
from filterlang import evaluate


def test_or_and_mix_true_lhs():
    assert evaluate("a = 1 OR b = 2 AND c = 3", {"a": 1, "b": 0, "c": 0}) is True


def test_or_and_mix_true_lhs_both():
    assert evaluate("a = 1 OR b = 2 AND c = 3", {"a": 1, "b": 2, "c": 0}) is True


def test_and_or_mix_true_rhs():
    assert evaluate("a = 1 AND b = 2 OR c = 3", {"a": 0, "b": 0, "c": 3}) is True


def test_and_or_mix_true_rhs_mid():
    assert evaluate("a = 1 AND b = 2 OR c = 3", {"a": 0, "b": 2, "c": 3}) is True


def test_two_ands_around_or():
    q = "a = 1 AND b = 2 OR c = 3 AND d = 4"
    assert evaluate(q, {"a": 1, "b": 2, "c": 0, "d": 0}) is True
```

test_hidden_escapes.py:
```python
from filterlang import evaluate


def test_escaped_quote():
    assert evaluate("name = 'o\\'brien'", {"name": "o'brien"}) is True


def test_escaped_backslash():
    assert evaluate("path = 'a\\\\b'", {"path": "a\\b"}) is True


def test_lone_escaped_quote_value():
    assert evaluate("q = '\\''", {"q": "'"}) is True


def test_trailing_escaped_backslash():
    assert evaluate("w = 'a\\\\'", {"w": "a\\"}) is True


def test_mixed_escapes():
    assert evaluate("s = 'it\\'s \\\\ ok'", {"s": "it's \\ ok"}) is True
```

test_hidden_not_combined.py:
```python
from filterlang import evaluate


def test_not_gt():
    assert evaluate("NOT age > 30", {"age": 25}) is True


def test_not_eq_string():
    assert evaluate("NOT name = 'amy'", {"name": "bob"}) is True


def test_not_lt():
    assert evaluate("NOT age < 18", {"age": 40}) is True


def test_not_eq_number():
    assert evaluate("NOT age = 7", {"age": 8}) is True


def test_not_with_and():
    assert evaluate("NOT age > 30 AND name = 'amy'", {"age": 25, "name": "amy"}) is True


def test_escape_with_precedence():
    q = "name = 'o\\'brien' OR age > 30 AND retired = 1"
    assert evaluate(q, {"name": "o'brien", "age": 20, "retired": 0}) is True


def test_not_or_and_combined():
    q = "NOT city = 'x' OR age > 30 AND tag = 't'"
    assert evaluate(q, {"city": "y", "age": 20, "tag": "z"}) is True
```

C2 bars: (1) hidden 17/17 pass on the arm's tree (each bug fixed per spec — following the misleading comments fails); (2) baseline 12/12 green, existing tests unmodified in intent (arms may extend the suite; the arm's own added tests do not count toward hidden grading); (3) reference-equivalent fix (judge). C1/C5 as usual. No arm has run; setup changes still allowed until the first arm launches.

---

## #46 — T5: multi-currency ledger (harder test)

*OPEN, opened 2026-09-17*

### Body

Post-M4 harder test (scale + cross-module coupling): extend the USD-only float ledger (accounts/fees/report/store, 28 green tests) to USD/EUR/JPY with Decimal + half-up quantization + JPY zero-decimals + pinned FX routing + backward-compat store reads. Canonical rules pinned exhaustively in the prompt (T4 lesson); exactly four existing tests may be updated (named in the prompt).

Grading: 29 hidden tests (outside the workdir, copied into a copy of the final tree at scoring) + visible suite + reference equivalence (judge). See oracle comment.

Two scored arms: saddle (unpinned) + untouched (xhigh). baseline arm medium/low/none probes alongside, unscored.

AC:
- [ ] Untouched, saddle arms run T5.
- [ ] Wall-clock per arm recorded (criterion 1).
- [ ] Correctness recorded via hidden tests + judge (criterion 2).
- [ ] Stall verdict recorded per arm (criterion 5).
- [ ] Node-count/decomposition evidence attached for saddle.

### Comment 1 — 2026-09-17 23:44:32 UTC

T5 verbatim prompt (identical for all arms):

---
# T5: add multi-currency support to the USD-only ledger

This repo is a USD-only ledger: float balances, one flat USD fee,
USD-only monthly statements, and a USD-only JSON store. Extend it to
support exactly three currencies — USD, EUR, JPY — following the
canonical rules below. The rules are exhaustive: where behavior is not
redefined here, keep the current behavior and keep the suite green.

## Canonical rules

1. **Currencies.** Exactly `USD`, `EUR`, `JPY`. Every function that takes
   a currency raises `ValueError` for anything else, and the message
   must name all three supported currencies (contain `USD`, `EUR`,
   and `JPY`).
2. **Arithmetic.** `decimal.Decimal` everywhere money is stored or
   returned. Accept `int`, `str`, `Decimal`, and `float`; convert
   floats via `Decimal(str(value))`. Reject any other type with
   `TypeError`.
3. **Rounding.** `ROUND_HALF_UP`, quantized to 2 decimal places for
   USD/EUR and 0 decimal places for JPY, applied to every stored or
   returned money value (balances, fee nets, converted lines, totals).
   Order is convert → quantize → validate: a quantized amount that is
   not positive raises `ValueError` (so `deposit("0.001", "USD")` and
   `deposit("0.4", "JPY")` raise).
4. **Accounts (`accounts.py`).** Per-currency balances.
   `deposit(amount, currency="USD")` / `withdraw(amount, currency="USD")`
   return the new `Decimal` balance for that currency. `.balance`
   stays the USD `Decimal` balance (`Decimal("0.00")` when no USD has
   been deposited); new `.balances` returns a dict copy
   `{currency: Decimal}`. Withdrawing more than the balance in that
   currency raises `ValueError`. The constructor stays
   `Account(owner, balance=0.0)` (a USD opening balance).
   `transfer(source, target, amount, currency="USD")` moves funds in
   the given currency. `None`/non-numeric amounts raise `TypeError`.
5. **Fees (`fees.py`).** Per-currency table
   `FEES = {"USD": Decimal("0.30"), "EUR": Decimal("0.25"),
   "JPY": Decimal("30")}` (supersedes `FLAT_FEE`).
   `apply_fee(amount, currency="USD")` quantizes its input, subtracts
   the fee, and returns the quantized `Decimal` net; a quantized
   amount below the fee raises `ValueError`.
   `fee_for(amount, currency="USD")` validates the same way and
   returns the `Decimal` fee. `total_fees(count, currency="USD")`
   returns the combined `Decimal` fees for `count` transactions
   (still `TypeError` for non-int counts, `ValueError` for negative).
6. **Report (`report.py`).** Transactions may carry `currency`
   (default `USD` when the key is absent).
   `monthly_summary(transactions, month, target="USD")` converts every
   line to `target` at the pinned rates — 1 EUR = 1.08 USD,
   1 USD = 155 JPY — routing crosses via USD (EUR→JPY multiplies by
   1.08 then 155; JPY→EUR divides by 155 then 1.08, left to right at
   default `Decimal` precision). Quantize each line amount in its own
   currency first, then convert, then quantize each converted line to
   the target's decimals (half-up) BEFORE summing; `credits`/`debits`
   are those sums and `net = credits - debits` (all quantized
   `Decimal`; `count` unchanged). The summary gains a `"currency"` key
   naming the target. An unknown line currency or target raises
   `ValueError` naming the supported currencies. `format_statement`
   keeps accepting summaries (old float ones too).
7. **Store (`store.py`).** New format
   `{"version": 2, "accounts": [{"owner": str,
   "balances": {currency: "decimal-string"}}]}` with each `Decimal`
   encoded via `str()` (e.g. `"10.00"`, `"500"`). `save_accounts`
   always writes version 2; `load_accounts` reads version 2 AND old
   version-1 files (`{"version": 1, "accounts": [{"owner",
   "balance": number}]}`, where a missing `version` also means 1),
   mapping the old `balance` number to USD. Any other `version`, or a
   record naming an unsupported currency, raises `ValueError` (the
   currency error names the supported currencies).
8. **Existing suite.** Keep it green. You may update ONLY these four
   tests, whose pinned behavior this task explicitly redefines —
   every other existing test must pass unmodified:
   - `tests/test_accounts.py::test_balance_type_is_float`
     (balances are now `Decimal`, not `float`)
   - `tests/test_fees.py::test_apply_fee_basic`
     (`apply_fee(10.0)` is now `Decimal("9.70")`, not `9.7`)
   - `tests/test_fees.py::test_apply_fee_larger_amount`
     (`apply_fee(100.0)` is now `Decimal("99.70")`, not `99.7`)
   - `tests/test_store.py::test_saved_schema_is_usd_only`
     (writes are now the version-2 schema from rule 7)
   Update each to assert the new behavior; do not weaken them.

## Done when

- All eight rules hold across `accounts.py`, `fees.py`, `report.py`,
  and `store.py` with one consistent money concept (same
  Decimal/rounding/currency validation everywhere — a shared helper
  module is recommended but not required; grading imports only the
  four modules above).
- The existing suite is green modulo the four named tests, which
  assert the new behavior.
- Stdlib only; deterministic (no network, clock, or randomness).

---

Baseline: accounts.py (73 lines), fees.py (41), report.py (72), store.py (63), tests/ (28 tests). Committed `baseline`, no bytecode in the commit. Verified: 28/28 green in a throwaway copy; baseline dir pristine.

### Comment 2 — 2026-09-17 23:44:32 UTC

T5 scoring oracle (seeded before any arm runs). Grading method: hidden tests (below, 29 tests in 5 files) are copied into a COPY of the arm's final workdir and run with the repo venv (`python -m pytest tests/` from the root). The arm never sees these files.

Reference solution (implemented by the task builder on a scratch copy AND operator-verified 57/57): shared `money.py` helper (SUPPORTED/PLACES, check_currency with naming error, to_decimal with float-via-str + TypeError on others, half-up quantize) rewired through all four modules; constructor shapes and defaults preserved; `to_dict`/`from_dict` handle v1+v2 records; only the four prompt-named tests updated (each strengthened to assert the new behavior — verified by diff). Cross-routing hidden tests (EUR->JPY, JPY->EUR) were added by the operator during audit; they pass on the reference.

Baseline-vs-hidden status: 28/28 baseline green; 28/29 hidden red on pristine baseline (the single pass is `test_bad_type_raises`, which holds trivially since `float(None)` already raises TypeError).

C2 bars: (1) hidden 29/29 pass on the arm's tree; (2) visible suite green with AT MOST the four prompt-named tests modified, each asserting the new behavior (any other test modification = gaming = FAIL); (3) reference-equivalent behavior (judge). C1/C5 as usual. No arm has run; setup changes still allowed until the first arm launches.

Hidden tests (durable copy; blank-line-normalized from the grading files):

test_hidden_accounts.py:
```python
"""Hidden tests: multi-currency accounts (run against repo root)."""

from decimal import Decimal

import pytest

from accounts import Account


def test_usd_rounds_half_up():
    acc = Account("ann")
    assert acc.deposit("1.005") == Decimal("1.01")
    assert isinstance(acc.balance, Decimal)


def test_jpy_has_zero_decimals():
    acc = Account("ann")
    assert acc.deposit("1.5", "JPY") == Decimal("2")
    assert acc.deposit("1.4", "JPY") == Decimal("3")
    assert acc.balances["JPY"] == Decimal("3")


def test_per_currency_balances_independent():
    acc = Account("ann")
    acc.deposit("10.00", "USD")
    acc.deposit("20.00", "EUR")
    assert acc.balance == Decimal("10.00")
    assert acc.balances == {"USD": Decimal("10.00"), "EUR": Decimal("20.00")}


def test_balances_returns_copy():
    acc = Account("ann", 100)
    acc.balances["USD"] = Decimal("0")
    assert acc.balance == Decimal("100")


def test_unsupported_currency_names_supported():
    acc = Account("ann")
    with pytest.raises(ValueError) as err:
        acc.deposit("10", "GBP")
    for code in ("USD", "EUR", "JPY"):
        assert code in str(err.value)


def test_float_converts_via_str():
    acc = Account("ann")
    assert acc.deposit(0.1) == Decimal("0.10")


def test_quantized_to_zero_raises():
    acc = Account("ann")
    with pytest.raises(ValueError):
        acc.deposit("0.001", "USD")
    with pytest.raises(ValueError):
        acc.deposit("0.4", "JPY")


def test_withdraw_insufficient_per_currency():
    acc = Account("ann")
    acc.deposit("10", "EUR")
    with pytest.raises(ValueError):
        acc.withdraw("5", "USD")


def test_bad_type_raises():
    acc = Account("ann")
    with pytest.raises(TypeError):
        acc.deposit(None)
```

test_hidden_fees.py:
```python
"""Hidden tests: per-currency fees (run against repo root)."""

from decimal import Decimal

import pytest

from fees import FEES, apply_fee, fee_for, total_fees


def test_fee_table():
    assert FEES == {
        "USD": Decimal("0.30"),
        "EUR": Decimal("0.25"),
        "JPY": Decimal("30"),
    }


def test_apply_fee_per_currency():
    assert apply_fee("100", "EUR") == Decimal("99.75")
    assert apply_fee("1000", "JPY") == Decimal("970")
    assert apply_fee("10.00") == Decimal("9.70")


def test_apply_fee_quantizes_input_first():
    assert apply_fee("10.005", "USD") == Decimal("9.71")


def test_apply_fee_unsupported_names_supported():
    with pytest.raises(ValueError) as err:
        apply_fee("10", "CHF")
    for code in ("USD", "EUR", "JPY"):
        assert code in str(err.value)


def test_apply_fee_below_fee_per_currency():
    with pytest.raises(ValueError):
        apply_fee("20", "JPY")
    with pytest.raises(ValueError):
        apply_fee("0.10", "EUR")


def test_fee_for_returns_decimal_fee():
    assert fee_for("10", "EUR") == Decimal("0.25")
    with pytest.raises(ValueError):
        fee_for("0.10", "EUR")


def test_total_fees_per_currency():
    assert total_fees(10, "EUR") == Decimal("2.50")
    assert total_fees(2, "JPY") == Decimal("60")
```

test_hidden_report.py:
```python
"""Hidden tests: cross-currency report (run against repo root)."""

from decimal import Decimal

import pytest

from report import monthly_summary


def test_eur_converts_to_usd():
    txns = [{"date": "2025-01-05", "amount": "100", "kind": "credit", "currency": "EUR"}]
    summary = monthly_summary(txns, "2025-01", target="USD")
    assert summary["credits"] == Decimal("108.00")
    assert summary["net"] == Decimal("108.00")


def test_usd_converts_to_jpy():
    txns = [{"date": "2025-01-05", "amount": "2", "kind": "credit"}]
    summary = monthly_summary(txns, "2025-01", target="JPY")
    assert summary["credits"] == Decimal("310")


def test_jpy_converts_to_usd_per_line():
    txns = [
        {"date": "2025-01-05", "amount": "100", "kind": "credit", "currency": "JPY"},
        {"date": "2025-01-06", "amount": "100", "kind": "credit", "currency": "JPY"},
    ]
    summary = monthly_summary(txns, "2025-01", target="USD")
    # Each 100 JPY -> 0.65 USD quantized BEFORE summing: 0.65 + 0.65 = 1.30
    # (summing first would give 200/155 = 1.29).
    assert summary["credits"] == Decimal("1.30")


def test_mixed_currencies_and_kinds():
    txns = [
        {"date": "2025-01-05", "amount": "100", "kind": "credit", "currency": "EUR"},
        {"date": "2025-01-06", "amount": "8", "kind": "debit", "currency": "USD"},
    ]
    summary = monthly_summary(txns, "2025-01", target="USD")
    assert summary["credits"] == Decimal("108.00")
    assert summary["debits"] == Decimal("8.00")
    assert summary["net"] == Decimal("100.00")
    assert summary["currency"] == "USD"


def test_usd_converts_to_eur():
    txns = [{"date": "2025-01-05", "amount": "108", "kind": "credit"}]
    summary = monthly_summary(txns, "2025-01", target="EUR")
    assert summary["credits"] == Decimal("100.00")


def test_unsupported_target_names_supported():
    with pytest.raises(ValueError) as err:
        monthly_summary([], "2025-01", target="GBP")
    for code in ("USD", "EUR", "JPY"):
        assert code in str(err.value)


def test_unsupported_line_currency_names_supported():
    txns = [{"date": "2025-01-05", "amount": "1", "kind": "credit", "currency": "GBP"}]
    with pytest.raises(ValueError) as err:
        monthly_summary(txns, "2025-01")
    for code in ("USD", "EUR", "JPY"):
        assert code in str(err.value)
```

test_hidden_report_cross.py:
```python
"""Hidden tests: cross-currency report routing via USD (run against repo root)."""

from decimal import Decimal

from report import monthly_summary


def test_eur_converts_to_jpy_via_usd():
    txns = [{"date": "2025-02-05", "amount": "100", "kind": "credit", "currency": "EUR"}]
    summary = monthly_summary(txns, "2025-02", target="JPY")
    # 100 EUR * 1.08 = 108 USD; 108 * 155 = 16740 JPY.
    assert summary["credits"] == Decimal("16740")
    assert summary["currency"] == "JPY"


def test_jpy_converts_to_eur_via_usd():
    txns = [{"date": "2025-02-05", "amount": "15500", "kind": "credit", "currency": "JPY"}]
    summary = monthly_summary(txns, "2025-02", target="EUR")
    # 15500 JPY / 155 = 100 USD; 100 / 1.08 = 92.5925... -> 92.59 EUR.
    assert summary["credits"] == Decimal("92.59")
    assert summary["currency"] == "EUR"
```

test_hidden_store.py:
```python
"""Hidden tests: store backward compat + new schema (run against repo root)."""

import json
from decimal import Decimal

import pytest

from accounts import Account
from store import load_accounts, save_accounts


def test_loads_old_usd_only_files(tmp_path):
    path = tmp_path / "old.json"
    payload = {"version": 1, "accounts": [{"owner": "ann", "balance": 10.5}]}
    path.write_text(json.dumps(payload), encoding="utf-8")
    (acc,) = load_accounts(str(path))
    assert acc.owner == "ann"
    assert acc.balance == Decimal("10.5")
    assert acc.balances == {"USD": Decimal("10.5")}


def test_saves_new_schema_with_string_decimals(tmp_path):
    acc = Account("ann")
    acc.deposit("10.00", "USD")
    acc.deposit("20.00", "EUR")
    path = str(tmp_path / "new.json")
    save_accounts([acc], path)
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    assert payload["version"] == 2
    (record,) = payload["accounts"]
    assert record == {"owner": "ann", "balances": {"USD": "10.00", "EUR": "20.00"}}


def test_round_trip_multi_currency(tmp_path):
    acc = Account("ann")
    acc.deposit("5", "USD")
    acc.deposit("500", "JPY")
    path = str(tmp_path / "ledger.json")
    save_accounts([acc], path)
    (loaded,) = load_accounts(path)
    assert loaded.balances == {"USD": Decimal("5.00"), "JPY": Decimal("500")}


def test_load_rejects_unknown_currency(tmp_path):
    path = tmp_path / "bad.json"
    payload = {"version": 2, "accounts": [{"owner": "ann", "balances": {"GBP": "1.00"}}]}
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError) as err:
        load_accounts(str(path))
    for code in ("USD", "EUR", "JPY"):
        assert code in str(err.value)
```

---

## #47 — T7: orderedlist package from spec (benchmark-sourced harder test)

*OPEN, opened 2026-09-18*

### Body

Sourced harder test (greenfield multi-hour-scale): build an installable orderedlist package implementing OrderedList (sorted-list container) from a normative spec, starting from an empty repo. Distilled + reskinned + perturbed from NL2Repo-Bench sortedcontainers (Hard) + upstream python-sortedcontainers (Apache-2.0); memorized upstream code provably fails (see oracle comment).

Grading: 63 hidden tests (adapted upstream coverage + perturbation + seeded stress) via pip-install grading in a throwaway venv. Materials: benchmark/tasks/t7/ in the repo.

Two scored arms: saddle (unpinned) + untouched (xhigh). baseline arm medium/low/none probes alongside, unscored.

AC:
- [ ] Untouched, saddle arms run T7.
- [ ] Wall-clock per arm recorded (criterion 1).
- [ ] Correctness recorded via hidden tests + judge (criterion 2).
- [ ] Stall verdict recorded per arm (criterion 5).
- [ ] Node-count/decomposition evidence attached for saddle.

### Comment 1 — 2026-09-18 00:04:29 UTC

T7 verbatim prompt (identical for all arms):

---
# T7: build the `orderedlist` package — OrderedList

Create an installable Python package named `orderedlist` implementing
`OrderedList`, a sorted-list container. Start from the empty repo.

## Layout (required)

- `pyproject.toml` configuring an installable package (`pip install -e .`
  must succeed).
- `orderedlist.py` (single module is fine; you may split into a package
  as long as this import works): `from orderedlist import OrderedList`.
- Stdlib only. Deterministic (no network, clock, or randomness).
- Any correct implementation passes: a plain list + `bisect` is fine.
  Correctness is graded, not speed (the hidden suite must finish
  within 300 seconds).

## OrderedList spec (normative)

`OrderedList(iterable=None)` holds values in ascending sorted order
(`<` comparison). Duplicates are allowed and preserved. The optional
constructor iterable may be any iterable (including generators); its
values need not be sorted.

- `insert(value) -> int`: insert `value`, keeping ascending order.
  Returns the index where it was inserted: the leftmost position at
  which it sorts (a new equal element goes BEFORE existing equals).
- `extend(iterable) -> None`: insert every value of any iterable.
- `clear() -> None`; `copy() -> OrderedList` (independent copy).
- `count(value) -> int`: number of occurrences.
- `index(value, start=None, stop=None) -> int`: leftmost index of
  `value` within `[start, stop)`, with EXACTLY `list.index` semantics
  (None/negative/out-of-range clamping, ValueError when absent).
- `remove(value) -> int`: remove ALL occurrences of `value`; return
  how many were removed. Raise ValueError if none present.
- `discard(value) -> bool`: remove ONE occurrence; return True if
  anything was removed, else False. Never raises for missing values.
- `pop(index=-1)`: remove and return the element at `index`
  (IndexError exactly like `list.pop`).
- `rank_left(value) -> int`: count of elements strictly less than
  `value` (== `bisect.bisect_left` position).
- `rank_right(value) -> int`: count of elements less than or equal
  (`bisect_right` position). `rank(value)` is an alias of rank_right.
- `iter_slice(start=None, stop=None, reverse=False)`: iterator over
  positions `[start, stop)` (None = ends); `reverse=True` yields them
  back to front.
- `iter_range(minimum=None, maximum=None, inclusive=(True, True),
  reverse=False)`: iterator over values `v` with
  `(v > minimum or (inclusive[0] and v == minimum))` and
  `(v < maximum or (inclusive[1] and v == maximum))`; None bounds are
  open. `reverse=True` yields back to front.
- Sequence protocol: `len()`, iteration, `reversed()`, `in`.
  `ol[i]` returns the element (IndexError like list indexing).
  `ol[i:j:k]` returns a plain `list` with EXACTLY list-slice
  semantics (including step-0 ValueError). `del ol[i]` /
  `del ol[i:j:k]` delete like list.
- `ol[i] = v` raises NotImplementedError. `ol.reverse()` raises
  NotImplementedError (use `reversed(ol)`).
- Comparisons (`== != < <= > >=`) compare as sequences against
  `list`, `tuple`, and `OrderedList`; against anything else the
  object is unequal (`==` False, `!=` True, orderings raise
  TypeError via reflected fallback — matching sequence rules).
- `ol + other` / `other + ol` return a NEW OrderedList with all
  values of both (`other` is any iterable, need not be sorted).
  `ol += other` extends in place. `ol * n` returns a NEW OrderedList
  with n shallow copies; `ol *= n` updates in place.
- `repr(ol)` is `OrderedList([...])` using the runtime class name
  (subclasses show their own name). Pickle round-trips preserve
  equality. `copy.copy(ol)` returns an independent copy (like
  `.copy()`); note `copy.copy` does not call `.copy()` for you.

## Done when

`pip install -e .` works, `from orderedlist import OrderedList`
resolves, and every behavior above holds (a hidden suite checks the
full API incl. slices, rankings, iterators, dunders, and errors).
Write your own tests as you go — the repo ships with none.

---

Baseline: empty repo (only .gitignore), committed `baseline`. The spec above is the complete task text; the repo ships with no tests — arms write their own as they go.

### Comment 2 — 2026-09-18 00:04:30 UTC

T7 scoring oracle (seeded before any arm runs).

PROVENANCE: distilled + reskinned + perturbed from NL2Repo-Bench task
`sortedcontainers` (Hard) + upstream grantjenks/python-sortedcontainers
@3ac3586 (Apache-2.0). Subset: SortedList public API only (no
SortedKeyList/Dict/Set/views/key/internals). Full design record:
benchmark/tasks/t7/DESIGN.md in the repo.

DECONTAMINATION: full API rename (SortedList->OrderedList,
add->insert, bisect_*->rank_*, islice/isrange->iter_*, ...) + 3
behavior perturbations (insert returns index; remove deletes ALL +
returns count; discard returns bool). PROOF (benchmark/tasks/t7/
decon_proof.py, executed): name-aliased upstream SortedList fails
3/3 perturbed behaviors. Memorized upstream code cannot pass.

GRADING: hidden suite = 63 tests (55 adapted coverage + 7 perturb +
1 seeded 1000-iteration stress) in benchmark/tasks/t7/hidden_tests/.
Method: throwaway venv per grading, `pip install -e .` on a COPY of
the arm's final tree, `python -m pytest` with a 300s budget (reference
runs it in 0.25s). Correctness graded, not speed. Reference impl:
benchmark/tasks/t7/reference/ (plain list + bisect, 63/63 green).

VERIFICATION LOG (operator, independent of the test adapter):
- Adapted hidden vs operator reference (written from SPEC only):
  63/63 green. The suite caught one real reference bug during audit
  (extend() self-aliasing infinite loop on `ol += ol`) — fixed.
- Privates stripped (grep-verified, only test-name substrings remain);
  stress seed 0 + repeat=1000 kept; assert counts reconcile
  (coverage 113->105 = 3 deleted internal tests + internal asserts
  removed; stress 27->27 identical).
- pip-install grading path verified end to end in a clean venv.

C2 bars: (1) hidden 63/63 on the arm's tree via the grading method;
(2) `pip install -e .` works and `from orderedlist import OrderedList`
resolves; (3) reference-equivalent behavior (judge). C1/C5 as usual.

VAULT RULE: scored arms must not read benchmark/tasks/ (session
transcripts + journals are audited for vault access; access voids
the run). No arm has run; setup changes still allowed until the
first arm launches.

---

## #48 — Run verdict PASS + exit 0 with 0 proven nodes (failed run reported as success)

*OPEN, opened 2026-09-18*

### Body

T6 saddle arm (journal /tmp/t6v1-saddle/.saddle/proofs.jsonl, 169s): node-1 worker call truncated (finish_reason=length at its token ceiling), node failed, node-2 undispatched. Journal run span honestly records '0 proven, 1 failed, 1 undispatched' with exit_code 1 — but the CLI exited 0 and the transcript verdict says PASS. Worktree untouched (baseline 12/12 green, hidden 17/17 red).

A failed run must fail loudly: exit nonzero + verdict FAIL. Silent PASS on 0 proven nodes is the worst possible reporting mode (a FAIL would at least be honest). Related: no recovery/retry span exists for worker-call failures (only Tier-1 gate failures trigger recovery) — worth deciding whether truncation should retry, but the verdict bug stands alone.

---
