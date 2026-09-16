# Slice proof run (issue 7)

Live run, 2026-09-16, self-operated vLLM (qwen3.8-27b). Emission succeeded first try at temp 0.0 after switching to the live-test prompt shape; worker diffs at temp 0.7. Note: the emission set red_phase_required=false, so red-phase shows "not required" below — benchmark runs should pin it true. Verbatim transcript follows.

---

# Saddle slice transcript

- Task: Fix f() in n.py to return 2 and add a passing test.
- Started: 2026-09-16T20:50:32.747038+00:00
- Finished: 2026-09-16T20:51:19.715012+00:00
- Verdict: PASS

## Node node-1

- Requirements: REQ-001
- Gate syntax: PASS (2 file(s) parsed)
- Gate ruff: PASS (2 file(s) clean)
- Gate tests: PASS ('pytest test_n.py' exited 0)
- Gate coverage: PASS (100.0% >= 100.0%)
- Gate red-phase: PASS (not required)
- Gate requirement-binding: PASS (1 requirement(s) bound)
- Proof: d7aa58dcc8ef04f94948202004be7b48910ac8512581d552a8c31501e281f97b

## Node node-2

- Requirements: REQ-002
- Gate syntax: PASS (2 file(s) parsed)
- Gate ruff: PASS (2 file(s) clean)
- Gate tests: PASS ('pytest test_n.py' exited 0)
- Gate coverage: PASS (100.0% >= 100.0%)
- Gate red-phase: PASS (not required)
- Gate requirement-binding: PASS (1 requirement(s) bound)
- Proof: 4d4716443e4e7b9f9167b4fd45b09d32badc932a51b7b889209d5650897ae21a

## Journal

- Path: /tmp/slice-live/proofs.jsonl
- Proven nodes: 2
- Issues: none (chain verifies)
