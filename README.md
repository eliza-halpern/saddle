<img width="492" height="90" alt="saddle_logo" src="https://github.com/user-attachments/assets/b2fb1593-833c-4008-8d52-32c81825adcf" />

# Saddle

Break your local model to harness.

Saddle is a deterministic execution harness that turns a non-deterministic local LLM into a dependable coding tool: constrained DAG planning, parallel proof-gated workers, machine-checked gates. No retries on unverified foundations.

**Status:** vertical slice built and benchmarked (see docs/BENCHMARK-RECORD.md). Tier-1 gates, the Kahn scheduler, the proof journal and the CLI exist; Tier-2 merge gates, Phase -1/0 and Phase 4 are specified in docs/ARCHITECTURE.md and not yet built.

## Phase 2: agent plus auditor

Phase 2 adds:

- `saddle auto`: one task, run autonomously in a git worktree, with a time and token
  budget. The result is a branch.
- A tiered auditor whose findings reach the model as tool results. `finish` is refused
  while the audit fails.
- A lane chip in the chat: **Ask** (the default, read-only: the model gets only read,
  list and search tools), **Edit** (opt-in, edits your folder directly, unaudited) and
  **Task · Small**, which starts the same audited run from the browser and shows an
  evidence packet compiled from the ledger.
- Run-state signals so a run finds you: tab title, favicon, an opt-in browser
  notification, and a state pill per session in the sidebar.
- Two questions a run can ask once each: whether tests may be edited when the auditor
  needs a test, and whether to extend a budget at 80%. Unanswered, each takes the old
  behaviour as its default.
- `saddle verify --anchor`, which checks each run's outcome against the
  `Saddle-Outcome` trailer on its branch.
- `saddle audit --tiered`, which runs the same battery on any diff.

- [docs/USING-SADDLE.md](docs/USING-SADDLE.md): install, the Ask / Edit / Task lanes,
  run-state signals, the two questions, how runs end, reading the packet,
  `saddle verify --anchor`.
- [docs/AUDIT-TIERS.md](docs/AUDIT-TIERS.md): the gates by tier, reason classes,
  caching.
- [docs/CLI.md](docs/CLI.md): every flag and exit code.

**Status as of 2026-09-26:**

- **Measured:** the suite and contract mutants for each piece (see commit messages), and
  runs against a scripted fake model.
- **Not measured:** there is no live E+A+F result yet. Nothing yet shows that the
  auditor lowers the false-done rate on real tasks, or what it costs in wall time.
- **Not built:** only the Small lane exists. Contracts are not sealed before
  implementation. The only questions a run asks are the two above.

## Layout

- `docs/ARCHITECTURE.md` — full architecture specification.
- `src/saddle/{dag,gates,evidence,runner,slice,scheduler,journal,vllm,cli}.py` — the vertical slice, in layering order `dag → gates → evidence → runner → slice`:
  - `dag.py` — DAG data model and validation.
  - `gates.py` — pure gate predicates.
  - `evidence.py` — subprocess policy and gate-runner adapters.
  - `runner.py` — node execution.
  - `slice.py` — end-to-end vertical slice orchestration.
  - `scheduler.py` — Kahn scheduler.
  - `journal.py` — proof journal.
  - `vllm.py` — vLLM client.
  - `cli.py` — command-line interface.
- `transcript.py`, `timeline.py`, `ux.py`, `tools.py`, `chat.py` — reporting and CLI support (`chat.py` present; see #23).

## Quickstart

The CLI as built (`src/saddle/cli.py:582-641`): `saddle doctor`, `saddle dag`, `saddle run`, `saddle tail`, `saddle verify`, `saddle up`. Configure the vLLM API key via `SADDLE_VLLM_API_KEY` or `VLLM_API_KEY`. Run the suite with:
```bash
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest -q
```

## Requirements

- Python 3.12+
- Self-operated vLLM ≥ 0.28 server with guided decoding (XGrammar) + KV offloading —
  the proven setup is [qwen38-27b-rtx3090](https://github.com/syv-ai/qwen38-27b-rtx3090)
- pytest, coverage.py, ruff, mutmut (sampled per node in Tier 1; the merge-time Tier 2 is not built)

## Reading the citations in the code

Comments, docstrings and docs cite findings and work items by ID: `F21.12a`,
`F13`, "round 3e", `T6-56`, "Rec 97", run names such as `M3F` or `CALIB`, and
lane names such as `FEEDFIX`. These refer to the author's internal benchmark
notes and work log (cited as `WORKPLAN`), which are not public. Each comment states the claim it
relies on, so it should read on its own; the ID only records where the
evidence came from.

## License

Copyright (C) 2026  Eliza Halpern and Eryn Lipkowitz. See [AUTHORS](AUTHORS).

Saddle is free software under the GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later). See [LICENSE](LICENSE).
