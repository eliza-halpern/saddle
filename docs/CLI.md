# CLI reference: `saddle auto`, `saddle audit --tiered`, `saddle verify`

This reference is taken from `saddle.cli.build_parser` and the command handlers. Run
`saddle <command> --help` for the same text. `auto`, like every command that talks
to the model server, needs an API key (`SADDLE_VLLM_API_KEY` or `VLLM_API_KEY`, or
`~/.config/saddle/env`). `audit` and `verify` do not.

## Server URL and model

Every command that talks to the model server (`doctor`, `dag`, `run`, `auto`, `chat`,
`up`) resolves `--base-url` and `--model` the same way. The first of these that is set
wins:

1. the flag (`--base-url URL`, `--model ID`), even when it names the default;
2. the environment variable (`SADDLE_BASE_URL`, `SADDLE_MODEL`); an empty one counts as unset;
3. a line in `~/.config/saddle/env`, parsed like the key's (`NAME=value`, optional
   `export ` and quotes);
4. the built-in default: `http://127.0.0.1:18020/v1` and `qwen3.8-27b`.

Only `SADDLE_VLLM_API_KEY`, `VLLM_API_KEY`, `SADDLE_BASE_URL` and `SADDLE_MODEL` are
read from that file, and nothing in it is put into the environment. The chat server
builds its model client from the resolved values, so the task runs it starts use them
too.

`saddle doctor` prints where each value came from before its verdict, and never the key:

```
base URL: http://127.0.0.1:18020/v1 (built-in default)
model: my-model (from environment SADDLE_MODEL)
OK: http://127.0.0.1:18020/v1 serves my-model (models: my-model)
```

The source is one of `from flag --base-url`, `from environment SADDLE_BASE_URL`,
`from file <path>` or `built-in default` (and the same for `--model`).

## The test time limit

Every time the auditor runs the project's tests, the run gets a time limit: in
`saddle auto` and the chat's Task runs (checkpoints and the finish audit), in
`saddle audit`, and in `saddle run` (each node's gate, its sampled candidates and the
merge-time suite). A suite still running at the limit is killed, and the `tests`
finding reads `'python -m pytest -q' hangs: no verdict within the time limit`.

The limit is 300 s unless the project sets its own in its `pyproject.toml`:

```toml
[tool.saddle]
test-timeout = 3600   # seconds
```

- It is read from the commit the work starts from, never from the tree being judged:
  the commit `saddle auto` makes its worktree from, `saddle audit`'s `--baseline`, and
  `saddle run`'s `HEAD`. A run that edits `pyproject.toml` does not change the limit it
  is judged under. An edit of yours takes effect once it is committed.
- The value is a number of seconds above 0 and at most 86400 (a day). `test-timeout`
  is the only key saddle reads in `[tool.saddle]`.
- A value that is not usable stops the audit instead of falling back to 300 s. This
  covers a string such as `"2400"`, `true`, zero, more than a day, another key in the
  table (such as the typo `test_timeout`), or a `pyproject.toml` that is not TOML. The
  error names the commit and the value: `error: cannot read the test time limit:
  pyproject.toml at <commit>: …`. `saddle audit` then exits 2, and in a Task run each
  audit is `blocked` with that reason. `saddle run` prints the error and exits 1
  before any node runs.
- The `pyproject.toml` read is the one at the root of the audited tree, where the tests
  run.

Saddle's own repository sets 3600 s. `check.sh` runs its suite in about 20 to 23
minutes (1209 to 1378 s in recorded serial runs), and the gate's own run of it, sandboxed
and under `coverage run`, took about 38 minutes (about 2300 s) in a 2-core slot. The
built-in 300 s reported it as a hang.

## saddle auto TASK

This command runs one task autonomously in a new worktree. The result is a branch.

| Flag | Default | Meaning |
|---|---|---|
| `TASK` | required | what to do, in words |
| `--repo REPO` | `.` | git repository to work on |
| `--time-budget S` | `1800` | wall-clock seconds before an honest stop |
| `--token-budget N` | `100000` | generated tokens before an honest stop (the prompt is not counted) |
| `--allow-test-edits` | off (tests read-only) | let the run edit test files. The normal flag for a task that needs a new or changed test: a terminal run cannot answer the "allow test edits?" question (USING-SADDLE.md §5, item 6), so without the flag its tests stay read-only |
| `--no-feedback` | | arm E+A: audit and journal, but deliver nothing and never refuse finish |
| `--no-audit` | | arm E: no auditor at all (mutually exclusive with `--no-feedback`) |
| `--sanctioned-test-rewrite NAME` | none | repeatable; see AUDIT-TIERS.md |
| `--finish-refusal-cap N` | `3` | stop `audit unresolved` after N consecutive refusals on an unchanged finding set; must be ≥ 1 |
| `--tier2 {score,shortlist}` | `score` | tier-2 mutation verdict: `score` is the 85% kill-rate bar; `shortlist` makes an open changed-line survivor `not-proven` (surfaced, not refused) and names it, with coverage as a locator only (USING-SADDLE.md §7a) |
| `--mutant-shortlist N` | `5` | with `--tier2 shortlist`: how many surviving mutants a refused finish names, one per changed line first |
| `--keep-reasoning` / `--no-keep-reasoning` | on | send each round's reasoning back to the model for the rest of the run; sealed as `prompt_shape.keep_reasoning`. `saddle chat` takes the same pair for the runs it starts |
| `--check-tool` | off | offer the model a `check` tool that runs audit tiers 0 and 1 on the current tree before finish (arm E+A+F only) |
| `--allow-installs` | off | offer the model an `install` tool; you approve each request, and an approved one installs from the wheel folder into an overlay of the run's own on the project venv (no network; the project venv is not changed). The run needs a project venv and a wheel folder holding wheels, or it does not start. A terminal run cannot answer, so every request there is refused |
| `--wheel-dir DIR` | `$SADDLE_WHEEL_DIR`, else `~/.local/share/saddle/wheels` | with `--allow-installs`: the local folder of wheels installs come from |
| `--base-url URL` | `$SADDLE_BASE_URL`, else `http://127.0.0.1:18020/v1` | model server (see [Server URL and model](#server-url-and-model)) |
| `--model ID` | `$SADDLE_MODEL`, else `qwen3.8-27b` | model id |
| `--temperature T` | `0.0` | sampling temperature |
| `--reasoning-effort` | `medium` | one of `none`, `low`, `medium`, `xhigh` |

With neither `--no-feedback` nor `--no-audit`, the arm is E+A+F: audits are delivered
to the model, and a failing finish audit refuses `finish`.

While it runs, the command prints one line per tool call. At the end it prints:

```
finished: finish called (arm E+A+F)
branch saddle/auto/<id> at <commit> (worktree <repo>/.saddle/worktrees/<id>)
ledger <repo>/.saddle/runs/<id>/proofs.jsonl
```

Audit findings are not printed to the terminal. Read them in the ledger or the chat's
packet (there is no CLI packet renderer).

Exit codes:

| Code | Meaning |
|---|---|
| 0 | finished |
| 3 | stopped (a budget, `audit unresolved`, no tool call in 3 consecutive rounds, a model error, cancelled, needs you: a question, or a change to saddle's own judging code) |
| 1 | setup error (not a git repo, worktree failed, bad arm or cap) or no API key |

## saddle audit --tiered [REV]

This command runs the tiered battery on a diff with no plan: tier 0 on each changed
`.py` file, then tier 1, then tier 2.

| Flag | Default | Meaning |
|---|---|---|
| `REV` | none | commit to audit, taken from a fresh clone; without it, `--repo`'s working tree including untracked files |
| `--repo REPO` | `.` | git repository |
| `--baseline REV` | `HEAD`, or `REV^` when REV is given | what to audit against |
| `--test-command CMD` | `python -m pytest -q` | how the tests are run |
| `--json` | off | print `{"verdict", "tiers": [...]}` only |
| `--cache DIR` | `~/.cache/saddle/audit` | verdict cache |
| `--no-cache` | off | neither read nor write the cache |
| `--tiered` | off | without it, `saddle audit` runs the older flat audit (`audit.audit_tree`) |
| `--tier2 {score,shortlist}` | `score` | as for `auto`; `shortlist` implies `--tiered` |
| `--mutant-shortlist N` | `5` | with `--tier2 shortlist`: how many surviving mutants the mutation finding names |

The text output prints each tier as a header, then one line per finding in the form
`verdict gate [reason] detail`, then `verdict: accept|refuse`.

| Exit | Meaning |
|---|---|
| 0 | accept (every tier passed) |
| 1 | refuse |
| 2 | could not audit (bad repo or revision) |
| 3 | nothing to audit (the tree equals the baseline) |

## saddle verify [JOURNAL]

| Arg | Default |
|---|---|
| `JOURNAL` | `.saddle/proofs.jsonl` (a run's ledger is `.saddle/runs/<id>/proofs.jsonl`) |
| `--anchor [REPO]` | off; with no REPO, the checkout the ledger sits in. Also checks each run's outcome against the `Saddle-Outcome` trailer on its branch (USING-SADDLE.md §8) |

Exit 0 prints the `OK: … ledger verifies …` line and a transcript. Exit 1 prints one
`code@line N: message` per issue. The codes are listed in USING-SADDLE.md §8.

## saddle chat / saddle web

| Flag | Default |
|---|---|
| `--workdir` | `~/saddle-ranch` |
| `--host` | `127.0.0.1` (a non-loopback host gets a token in the URL) |
| `--port` | `8777` |
| `--sessions` | `~/.saddle/sessions` |
| `--base-url`, `--model` | as for `auto` |
| `--no-open` | do not open a browser |
| `--keep-reasoning` / `--no-keep-reasoning` | on: task runs the chat starts keep each round's reasoning, as `auto` does |
| `--allow-installs`, `--wheel-dir DIR` | off, as for `auto`: with it, task runs the chat starts may install from the wheel folder, each request approved on the task card |
| `--extract-requirements` / `--no-extract-requirements` | `$SADDLE_EXTRACT_REQUIREMENTS` (or that line in the env file; `1`/`0`, `on`/`off`, `true`/`false`, `yes`/`no`), else off: task runs the chat starts check the task text's own examples (P1) as `auto --extract-requirements` does, at question strength. A run whose finish audit asks ends "needs you" with the question on the card. Any other value stops the command with exit 2. `auto` does not read the variable |

Exit 2 if the token file cannot be created (`FileExistsError`), or if `$SADDLE_EXTRACT_REQUIREMENTS` holds a value that is not on or off.

## saddle up

The terminal chat. Same lanes and tool lists as the web chat's Ask and Edit
(`tools.scope_turn`).

| Flag | Default | Meaning |
|---|---|---|
| `--mode {ask,edit}` | `ask` | `ask`: read-only tools (`read_file`, `list_dir`, `search`), any other call refused; `edit`: may write files and run commands in `--workdir`, unaudited |
| `--workdir DIR` | `.` | directory tools run in |
| `--journal PATH` | `.saddle/chat.jsonl` | journal path |
| `--max-tokens N` | `8192` | reply max tokens |
| `--temperature T` | `0.0` | sampling temperature |
| `--reasoning-effort` | `medium` | one of `none`, `low`, `medium`, `xhigh` |
| `--base-url`, `--model` | as for `auto` |
