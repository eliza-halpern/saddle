# Using saddle (Phase 2: agent plus auditor)

This guide covers the Phase 2 features: autonomous runs with an auditor, the chat's
Task mode, and the evidence packet. It describes branch `phase2-ui3` (d61cfea). Each
behaviour below was checked against the code, its tests, or a run against a scripted
fake model. Anything marked **(unverified)** was not checked.

Related: [AUDIT-TIERS.md](AUDIT-TIERS.md) (what each check proves) and
[CLI.md](CLI.md) (every flag).

## 1. Install

You need Python 3.12 or newer, git, and an OpenAI-compatible model server. Saddle is
built for vLLM serving `qwen3.8-27b` (see README, Requirements).

```bash
git clone https://github.com/eliza-halpern/saddle.git && cd saddle
uv sync                      # runtime deps plus the `dev` dependency group
export SADDLE_VLLM_API_KEY=...   # or put it in ~/.config/saddle/env
```

The auditor runs `pytest`, `coverage`, `ruff` and `mutmut` by bare name, so the venv
must be on `PATH` when saddle runs: `export PATH="$PWD/.venv/bin:$PATH"`. `mutmut`,
`pytest` and `ruff` are dev-group dependencies, not runtime ones. The repository you
point saddle at must be a git repository whose tests run with `python -m pytest -q`.

The default server is `http://127.0.0.1:18020/v1` with model `qwen3.8-27b`. Pass
`--base-url` and `--model` to use another one. Every command that talks to the model
refuses to start without an API key (`saddle.cli._api_key`).

## 2. Start the chat

```bash
saddle chat            # `saddle web` is an alias
```

This serves on `http://127.0.0.1:8777/` and opens a browser. New sessions start in
`~/saddle-ranch` unless you pass `--workdir`. If you bind a non-loopback `--host`,
saddle generates an access token and prints it in the URL (`web.app.chat_token`).

## 3. Chat mode and Task mode

Each session has a mode, set by the **Chat | Task** switch under the message box. The
mode is saved with the session (`Session.mode`, default `chat`) and shown as a chip
beside the session title.

| | Chat | Task |
|---|---|---|
| Enter | sends a message | opens the Run strip; nothing runs yet |
| Where it works | directly in your folder | a new git worktree of the folder |
| Tools | read, write, edit, list, search, run commands | the same, plus `finish` |
| Audited | no | yes (arm E+A+F) |
| Result | the conversation | a branch and an evidence packet |
| Temperature | 1.0 (`engine.CHAT_TEMPERATURE`) | 0.0 |

Chat mode edits your checkout directly. Nothing in it is audited and it produces no
packet. Task mode needs a folder: with none, Enter shows "Pick a folder first" and
starts nothing. If the folder is not inside a git repository, the run ends `failed`
before it starts (`web.tasks.execute`, `auto.AutoError`). Shift+Enter inserts a newline
in both modes.

![Task mode](screenshots/task-mode.png)

## 4. The Run strip

In Task mode, Enter shows the strip. Enter or Ctrl+Enter starts the run; Escape
closes the strip.

![Run strip](screenshots/run-strip.png)

- **Lane.** The strip always says "Small lane". The Ask, Feature, Breadth and Long lanes
  from the design are not built, and nothing chooses a lane automatically.
- **Budgets.** Time is in minutes (default 30) and tokens in thousands (default 100k
  *generated* tokens; the prompt is not counted). The run stops when either one runs
  out.
- **Allow test edits.** Ticked by default in the chat (`web.tasks.SMALL_LANE_TEST_EDITS`).
  `saddle auto` defaults the other way: tests are read-only. When it is unticked, any
  write to a test path is refused at the tool. Test paths are the repo's pytest
  `testpaths` (or `tests/`), `test_*.py` and `conftest.py`. When it is ticked, the model
  can add tests, but the **assertion-preservation** gate still fails a change that
  removes or weakens an assertion in a test that existed before the run. A failing gate
  means `finish` is refused. The gate does not read this checkbox.

## 5. What happens during a run

1. Saddle creates a worktree at `.saddle/worktrees/<run-id>/` on a new branch,
   `saddle/auto/<run-id>`. The run id is 12 hex characters. It also writes
   `.saddle/.gitignore` containing `*`, so your `git status` does not change. Your
   checkout is never edited.
2. The ledger is `.saddle/runs/<run-id>/proofs.jsonl`, plus `attempts/` sidecars. It sits
   outside the worktree, so the model's tools cannot reach it.
3. **Tier-0 guard, at the tool.** Two checks run on every write:
   - A write that would leave a `.py` file unparseable is refused. This does not apply
     if the file was already unparseable.
   - A test file write is refused while tests are read-only.

   Refused edits show on the Scope row. The Auditor's other tier-0 checks (ruff on the
   file, import resolution) do **not** run during a run. They only run in
   `saddle audit --tiered` (see AUDIT-TIERS.md).
4. **Checkpoints.** A burst of edits ends when the model calls any tool other than
   `write_file` or `edit_file`. Saddle then copies the tree and runs tier 1 on the copy
   in a background thread. The model does not wait for it. When that audit completes,
   its failures are appended to the model's next tool result, for example
   `[audit checkpoint 1 on tree …: FAIL] - coverage (tier 1): fail, evidence-thin: …`.
5. **Finish audit.** When the model calls `finish`, tiers 1 and 2 run on the final tree.
   If any finding is `fail` or `blocked` (and not `sanctioned`), `finish` is refused.
   The model gets the findings back and keeps working within its budgets.
6. **Needs you.** The engine can pause on a question and wait for your answer
   (`engine._ask`). The card has a question box for this. However, `saddle chat` starts
   its server with no question source (`web.app.serve` passes no `auditor`), and nothing
   in `src/` creates a question. So today a real run never asks you anything. Only the
   tests and the screenshot harness exercise this path.
7. The run commits whatever it left, even an empty change, as
   `saddle auto <id>: <outcome> (<reason>)`. The model's summary goes in the commit body
   under "Narrative (model-written, not evidence)". Bytecode and `.saddle/` are never
   committed (`auto.UNSTAGED`).

![A checkpoint finding delivered to the model](screenshots/live-finding.png)
*(This shot predates UI3: the ▶ Run button in the composer is gone. Task mode replaced it.)*

## 6. How a run ends

The ledger records exactly one outcome span: `auto:finished` or `auto:stopped`.

- **finished.** The model called `finish` and, in arm E+A+F, the finish audit passed.
  `saddle auto` exits 0. In arms E and E+A, "finished" only means the model called
  `finish`; it makes no claim that the change works.
- **stopped: token budget exhausted / time budget exhausted.** A budget ran out. The
  branch holds the partial work. Exit 3.
- **stopped: audit unresolved.** `finish` was refused `--finish-refusal-cap` times in a
  row (default 3) on the same set of failing findings. "Same set" compares gate, reason
  and cites; a change in the set restarts the count. The packet lists the findings.
  When a finding is coverage or evidence-thin and tests were read-only, the card offers
  **Run again with test edits allowed**. Exit 3.
- Other stops, also exit 3: `model error: …`, `cancelled` (you pressed Stop), and
  `needs you: …` (a question with no answer).

A stop never reads as done: the verdict text says "It did not finish, and nothing here
says it did."

![Audit unresolved, with the rerun offer](screenshots/audit-unresolved-offer.png)

## 7. Reading the evidence packet

The packet is built from the ledger alone (`packet.compile_packet`) and makes no model
call. `check_packet` rejects any row that makes a claim without citing a record in the
ledger. Each row has a status:

- `proven` / `failed`: an auditor verdict.
- `observed`: a sealed fact about what the run did.
- `absent`: the evidence does not exist. Rows are shown even when absent, never
  dropped.

| Row | What it says |
|---|---|
| Contract | Always `absent` in the Small lane, which seals no contract. If questions were answered, lists them. |
| Tests | The auditor's latest `tests`/`full-suite` finding (`proven`/`failed`). If there is none, the model's own last pytest run (`observed`, "not an auditor verdict"). |
| Mutation | The latest tier-2 `mutation` finding, e.g. "killed 1 of 1 changed-line mutants". `blocked` (tier 1 failed, so tier 2 did not run) is shown as `failed`. |
| Scope | The files changed on the branch, every edit refused by the tier-0 guard, and whether tests were editable. |
| Audit | Every other gate's latest finding, ✓ or ✗. Only the latest finding per gate counts, so a failure that a later audit cleared is history. |
| Not proven | What the packet cannot vouch for: no auditor ran, the run stopped, an unanswered question, ledger issues, estimated token counts, unresolved findings. |
| Narrative | The model's `finish` summary. **It is not evidence.** Sentences that pair a check word with a result word ("All tests pass") are struck through. This check is lexical English, so it misses some claims and flags some harmless sentences (`packet.flag_narrative`). |
| Cost | Wall time against budget, and generated tokens, labelled *measured* only when the server reported usage (otherwise "estimated", chars/4). Then a tool-call count, which currently includes audit records (see caveat). |
| Reproduce | The `saddle verify` command, and `git log -p main..saddle/auto/<id>`. |

![Packet](screenshots/packet.png)

Caveats found while writing this guide:

- The Cost row counts every `kind == "tool"` span as a tool call, including
  `audit:*` and `audit-tier*` records. A run with 4 model tool calls reported "18 tool
  calls".
- The Reproduce command always says `main`. If your default branch has another name,
  substitute it.

## 8. Verifying a ledger by hand

```bash
saddle verify .saddle/runs/<run-id>/proofs.jsonl
```

On success, verify prints
`OK: …: ledger verifies: N records (P proof(s), S span(s), L plan(s)), outcome span list intact (K autonomous run(s))`,
then a transcript, and exits 0. Records are hashed one by one; they are not a hash
chain. What verify establishes:

- each record's own hash;
- that its parent links resolve;
- for an autonomous run, that the tool spans match the list sealed in the outcome's
  sidecar, in order.

Audit records are not held to that list. An inserted or deleted audit record is
therefore not caught; its own hash still is.

Known quirk: the transcript printed after the OK line is the slice renderer's. For an
autonomous run it shows `Task: (unknown)` and `Verdict: FAIL`, even for a finished run
whose audit passed. Read the outcome from the packet or the `auto:*` span, not from
that line.

On failure, verify prints one `code@line N: message` per issue and exits 1:

| Code | Meaning |
|---|---|
| `torn-tail` | last line unterminated (discarded) |
| `unparseable-line` | a line is not JSON |
| `invalid-record` | JSON that is not a valid record |
| `bad-hash` | a record's contents do not match its hash (edited) |
| `attempt-sidecar` | a span's sidecar is missing or does not hash to the sealed value |
| `sidecar-diff-hash` | a sidecar's retained diff does not match its `diff_hash` |
| `unknown-parent` | a proof cites a parent proof not in the journal |
| `unplanned-proof` | a proof matches no plan record |
| `orphan-span` | a span cites a parent span not in the journal |
| `outcome-missing` | a run sealed a proof but has no outcome span |
| `outcome-duplicate` | a run has two outcome spans |
| `span-after-outcome` | a tool span follows the outcome |
| `outcome-list` | the outcome's sidecar has no tool-span list |
| `span-missing` | a listed tool span is not in the journal (deleted) |
| `span-unlisted` | a tool span is not in the list (inserted) |
| `span-order` | tool spans are out of the listed order |

Two of these were checked by tampering with a real run: deleting a `run_command` line
gave `span-missing`, and editing exit codes gave `bad-hash`.

## 9. Getting the change into your repo

The result is an ordinary branch in your repository:

```bash
git log -p main..saddle/auto/<run-id>     # read it
git merge saddle/auto/<run-id>            # or cherry-pick, or open a PR from it
git worktree remove .saddle/worktrees/<run-id>   # when done
```

Saddle never merges or pushes. It also never removes its worktrees; that is left to
you (no removal code found in `auto.py`).
