# Using saddle (Phase 2: agent plus auditor)

This guide covers the Phase 2 features: autonomous runs with an auditor, the chat's
lanes (Ask, Edit, Task · Small), and the evidence packet. It describes branch
`phase2-integ` after the anchor, UI3, notify, ask, lane-chip, fix and packet merges,
plus the UXREVIEW2 fixes. Each behaviour
below was checked against the code, its tests, or a run against a scripted fake model.
Anything marked **(unverified)** was not checked.

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

## 3. Lanes: Ask, Edit and Task · Small

Each session has a lane, chosen with the **lane chip** inside the message box. The lane
is saved with the session (`Session.mode`, one of `sessions.SESSION_MODES` =
`ask`, `edit`, `task`) and shown as a chip beside the session title. The header also
shows the session's folder name and git branch (`web.app.git_branch`).

| | Ask (default) | Edit | Task · Small |
|---|---|---|---|
| Enter | sends a message | sends a message | opens the Run strip; nothing runs yet |
| Where it works | your folder, read-only | directly in your folder | a new git worktree of the folder |
| Tools | `read_file`, `list_dir`, `search` only | read, write, edit, list, search, run commands | Edit's tools plus `finish` |
| Audited | no (it cannot write) | **no** | yes (arm E+A+F) |
| Result | the conversation | the conversation | a branch and an evidence packet |
| Temperature | 1.0 (`engine.CHAT_TEMPERATURE`) | 1.0 | 0.0 |

- **Ask is the default and cannot write.** A new session starts in Ask. Its turns are
  offered only the read-only tools (`tools.READ_ONLY_TOOLS`), and `ToolContext.allowed`
  refuses any other call before it runs, so a model that emits `edit_file` anyway gets a
  tier-0 refusal and your folder is unchanged. A session stored with an unknown mode,
  including the old `chat`, reads as Ask.
- **Edit is opt-in and unaudited.** It is the old Chat mode: it edits your checkout
  directly, nothing is audited and no packet is produced. `PATCH mode=chat` is now
  refused with 400; use `edit`.
- **Task · Small** starts an audited run (sections 4 to 7). It needs a folder: with none,
  Enter shows "Pick a folder first" and starts nothing. If the folder is not inside a git
  repository, the run ends `failed` before it starts (`web.tasks.execute`,
  `auto.AutoError`).
- The chip's menu also lists Feature, Breadth and Long, greyed and unselectable: those
  lanes are not built.
- **Shift+Tab** cycles the enabled lanes. A ghost hint ("looks like a task → Tab",
  "looks like a question → Tab for Ask") may appear under the input; it never changes the
  lane by itself. The placeholder and the line under the input state what the current
  lane will do. Shift+Enter inserts a newline in every lane.

![Task mode](screenshots/task-mode.png)
*(This shot predates the lane chip: it shows the old Chat | Task switch.)*

## 4. The Run strip

In the Task · Small lane, Enter shows the strip. Enter or Ctrl+Enter starts the run; Escape
closes the strip.

![Run strip](screenshots/run-strip.png)

- **Lane.** The strip always says "Small lane". Feature, Breadth and Long are not built,
  and nothing chooses a lane automatically.
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
6. **Needs you: two questions.** A run can pause and ask you, once each, through
   `engine._ask`. The card shows a question with its options, and the answer goes to
   `POST /api/tasks/<id>/answer`.
   - **Test edits.** When `finish` is refused, tests are read-only, and the last audit has
     a failing coverage or evidence-thin finding, the run asks: "The auditor needs a test
     that covers <file:line>. Tests are read-only in this run. Allow test edits for the
     rest of this run?" [Allow, Keep read-only]. Allow lifts the test-path guard for the
     rest of the run, seals `test_edits_granted: true` in the outcome sidecar, and reopens
     the run if this refusal hit the cap. In the chat this only arises when you untick
     **Allow test edits**, since the box is ticked by default.
   - **Budget.** When generated tokens or wall time first reach 80% of the budget
     (`engine.BUDGET_ASK_AT`) without finishing, the run asks whether to extend that
     budget by the same amount again or stop at the limit [Extend, Stop at limit].
     Extend doubles that budget once and seals `budget_extended`. A single round that
     jumps from below 80% to past 100% is not asked.

   The default (Keep read-only, Stop at limit) is the old behaviour. A run with no answer
   channel (headless `saddle auto`, which prints the question and the default) or one
   stopped while waiting never blocks: it seals "unanswered, default taken: …", and the
   packet's Contract row says no answer came. A reply that is not one of the options
   takes the default. Time spent waiting for you is not charged to the time budget.
7. The run commits whatever it left, even an empty change, as
   `saddle auto <id>: <outcome> (<reason>)`. The model's summary goes in the commit body
   under "Narrative (model-written, not evidence)". Bytecode and `.saddle/` are never
   committed (`auto.UNSTAGED`).

![A checkpoint finding delivered to the model](screenshots/live-finding.png)
*(This shot predates UI3: the ▶ Run button in the composer is gone. Task mode replaced it.)*

## 5a. Knowing a run's state from another tab

You do not have to watch the card. The page reports each run's state (`static/notify.js`,
fed by the active session's task events and a 3 s poll of `/api/sessions`, whose rows
carry `run_state` and `run_task`):

- **Tab title:** `● running · <task>`, `? needs you · <task>`, `✓ finished · <task>`,
  `■ stopped · <task>`, `! no outcome · <task>`; the session title when idle. An ended
  prefix clears when you come back to the tab.
- **Favicon:** a coloured dot per state; a hollow ring when idle.
- **Notification:** on needs-you and on an ended run, only while the tab is hidden, and
  only if you turned on **Notify me** in the sidebar. The browser's permission prompt is
  shown from that button, never on page load.
- **Sidebar:** a state pill on every session row, including sessions not on screen.
- **Screen readers:** a polite live region (`#run-live`) announces each transition.

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
| Cost | Wall time against budget, and generated tokens against the budget the run ended with (doubled if you answered Extend), labelled *measured* only when the server reported usage (otherwise "estimated", chars/4). Then the model's tool-call count; audit records are not counted (`journal.AUDIT_SPAN_PREFIXES`). |
| Reproduce | The `saddle verify` command, and `git log -p main..saddle/auto/<id>`. When the packet was compiled with an anchor repo, it states the anchor result and the command gains `--anchor`. |

![Packet](screenshots/packet.png)
*(This shot predates the summary band and the action row described below.)*

### How the chat lays the packet out

The chat card does not show the rows above in table order (`static/tasks.js`,
`renderPacket`). From the top:

1. **Verdict**: Finished, Stopped or No outcome, the task, one sentence, and chips for
   the branch, files changed, test policy and ledger size.
2. **Action row**: **View diff**, **Merge into <branch>**, **Discard branch**,
   **Continue in chat**.
   - View diff shows the run's changes per file (merge base to the run branch).
   - Merge is enabled only for a finished run with no failed row and at least one
     proven Tests, Mutation or Audit row (`web.branch_actions.merge_refusal`), and only
     on a clean checkout that is on a branch. When the Mutation row is not proven, the
     button reads "Merge into main (mutation unproven)" in amber: the merge is allowed,
     and the label says what it goes without. When merge is off, the reason is shown
     under the row, for example "Merge is off: The run is stopped, not finished; only a
     finished, audited run merges."
   - Merge and Discard each ask you in the page first, naming the branch and the
     target. Merge fast-forwards if it can, otherwise cherry-picks the run's commits,
     and aborts cleanly on a conflict. Discard deletes the branch and its
     `.saddle/worktrees/<id>` worktree; the ledger is kept. Neither is sealed in the
     ledger: each attempt is appended to the session's `actions.log`.
   - Continue in chat switches the session to Ask and puts the packet's text in the
     message box. It sends nothing.
3. **Summary band**: Tests, Mutation and Not proven, one line each with a glyph (✓
   proven, ✗ failed, ◐ observed, ○ no record, ! something unproven). Each line opens to
   its full row. The band is green only for a finished run whose three lines are all
   ✓, blue ("partial") for a finished run with any other line (a missing mutation
   record is the common case), red with a ✗, and amber for any stop, which lists its
   unresolved findings in the band itself.
4. **Scope**, then **Audit** folded to "N of M passed".
5. **Details** (folded): Contract, Narrative, Cost and Reproduce.

Caveat found while writing this guide: the Reproduce command always says `main`. If
your default branch has another name, substitute it.

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
  sidecar, in order;
- for a run sealed with an audit list (`audit_span_hashes`, every run since the
  phase2-fix merge), that its audit records match that list. A deleted audit record
  gives `audit-span-missing`, an inserted one `audit-span-unlisted`. Outcomes sealed
  before that have no list, and their audit records are not judged.

### Anchoring the outcome in the branch

The ledger alone can be resealed: delete a span, drop it from the outcome's list and
recompute the hashes, and plain `verify` still passes. So every run's final commit on
`saddle/auto/<run-id>` ends with a trailer paragraph:

```
Saddle-Outcome: <outcome span record_hash>
Saddle-Ledger: .saddle/runs/<run-id>/proofs.jsonl
```

```bash
saddle verify .saddle/runs/<run-id>/proofs.jsonl --anchor [REPO]
```

`--anchor` is opt-in; REPO defaults to the checkout the ledger sits in. It reads the
newest commit on the run's branch carrying `Saddle-Outcome` (later commits of yours do
not hide it) and adds three issue codes: `anchor-mismatch` (the ledger's outcome is not
the one the branch recorded), `anchor-missing` (an outcome but no anchor or no branch),
and `outcome-missing-anchored` (an anchor but no outcome). Neither outcome nor anchor
means the run is still in flight. Limit: the anchor is only as strong as the branch
history; rewriting both the ledger and the branch tip with a recomputed trailer still
passes.

The transcript printed after the OK line takes the task from the run's `auto:start`
span and the verdict from its own outcome span: `FINISHED`, `STOPPED (<reason>)` or
`NO OUTCOME (...)`. It lists the model's tool calls in full, arguments included, and
does not summarise the audit; for that, read the packet.

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
| `audit-span-missing` | an audit record in the outcome's audit list is not in the journal |
| `audit-span-unlisted` | an audit record of the run is not in the outcome's audit list |

Two of these were checked by tampering with a real run: deleting a `run_command` line
gave `span-missing`, and editing exit codes gave `bad-hash`.

## 9. Getting the change into your repo

The result is an ordinary branch in your repository:

```bash
git log -p main..saddle/auto/<run-id>     # read it
git merge saddle/auto/<run-id>            # or cherry-pick, or open a PR from it
git worktree remove .saddle/worktrees/<run-id>   # when done
```

In the chat, the packet's **Merge** and **Discard branch** buttons do the same from the
page (section 7). Saddle never pushes, and it never merges or deletes anything without
that confirm step; `saddle auto` itself leaves the branch and its worktree for you.
