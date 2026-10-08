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
   `export` and quotes);
4. the built-in default: `http://127.0.0.1:18020/v1` and `qwen3.8-27b`.

Only `SADDLE_VLLM_API_KEY`, `VLLM_API_KEY`, `SADDLE_BASE_URL` and `SADDLE_MODEL` are
read from that file, and nothing in it is put into the environment. The chat server
builds its model client from the resolved values, so the task runs it starts use them
too.

`saddle doctor` prints where each value came from before its verdict, and never the key:

```text
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
- The value is a number of seconds above 0 and at most 86400 (a day). The keys saddle
  reads in `[tool.saddle]` are `test-timeout`, `test-workers`, `sandbox-expose` and
  `gate-checks` and `gate-stage-languages` (both below), `static-check`, and `prompt-benchmark` with its floor and
  margin (below).
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
built-in 300 s reported it as a hang. It also sets `test-workers = 8` (below).

## Running the tests on workers

A project whose suite is slow can ask the auditor to run it on pytest-xdist workers, in
the same table:

```toml
[tool.saddle]
test-workers = 8
```

- The auditor then runs the suite once as `pytest -n 8 --dist worksteal` under
  pytest-cov (a worker that runs out of tests takes the ones still queued on another; a
  `--dist` mode your own pytest options choose is kept instead), and the
  `tests` and `coverage` findings both read that one run. Each red-phase sample and each
  dead-code rerun uses the same workers. A red-phase sample records no coverage, since
  only its exit and output are read: when your own options start pytest-cov, it runs
  with `--no-cov`. This holds for the checkpoints and the finish audit of `saddle auto`
  and the chat's Task runs, and for `saddle audit`; `saddle run` runs its node gates
  serially.
- It is read like `test-timeout`: from the commit the work starts from, never from the
  tree being judged. The value is a whole number from 1 to 64, and 1 or no key is a
  serial run. A value that is not usable, such as `"8"`, `true`, `8.0` or `"auto"`,
  stops the audit (`error: cannot read the test worker count: pyproject.toml at
  <commit>: …`), and so does a key saddle does not read, such as the typo `test-worker`.
- The environment the tests run in (the project's virtualenv, or the `python` on your
  PATH) needs pytest-xdist and pytest-cov. Saddle asks pytest itself, in the audit's
  sandbox, which plugins it loads under your project's own options. When one is missing,
  or your options disable it (`-p no:xdist`, `-p no:cov`, `--no-cov`), the suite runs
  serially, as it does without the setting, and the `tests` finding says why:
  `'python -m pytest -q' exited 0; test-workers = 8 set but pytest-xdist is not
  installed: ran serially`. A test command that does not start pytest directly (`make
  test`, `tox`) runs serially with the same kind of note. A parallel run is recorded in
  the finding's basis as `test-workers=8`.
- The verdicts are the serial run's: the same tests pass or fail, and the same changed
  lines count as run, because pytest-cov combines what the controller and every worker
  ran. When your own pytest options start pytest-cov (`--cov=...` in `addopts`), its
  source and report stand. Otherwise saddle adds `--cov` with no source, which
  measures what `coverage run` measures (your coverage config's `source`, else
  everything).
- No coverage total decides an audit's run of your suite, on workers or not: whenever
  pytest-cov runs, saddle adds `--cov-fail-under=0`. The `coverage` finding judges the
  changed lines instead. A total your suite reaches in your own checks can be out of
  reach in the audit's sandbox, where tests that need what the sandbox withholds skip.
  Keep enforcing your total in your own check (saddle's `check.sh` does).
- A suite whose tests share a file, a port or other global state can fail on workers
  where it passes serially. Leave the setting out for such a suite.

Saddle's own repository sets `test-workers = 8`: its suite, with coverage, took 376 s
on 8 workers against 1209 to 1378 s serially, with the same tests passing and 100% line
and branch coverage. Its own `check.sh` runs the suite on the same number of workers.

## The project's gate stages

A project's own gate is more than its tests: lint, format, types, and linters for its
scripts, pages and docs. The audit runs the stages you name, so a change that the
project's gate would refuse is not passed on the strength of a green suite:

```toml
[tool.saddle]
gate-checks = [
    ["ruff", "check", "."],
    ["ruff", "format", "--check", "."],
    ["npx", "--no-install", "eslint", "."],
]
```

- Each entry is one command as a list of words, run with no shell (so a glob must be
  written out as the files). Name the fast stages only; the suite is the audit's own
  `tests` finding. `static-check` is one more stage and is not run twice when it is also
  listed.
- Every stage runs on the audited tree and on a checkout of the starting commit. The
  `project-gate` finding (tier 1) reads: pass when the audited tree passes the stage;
  fail when it fails a stage the starting commit passed, quoting the stage's first
  lines; not proven when the stage was already failing at the starting commit (so the
  change is not shown to have broken it), or when its command cannot start (the tool is
  missing in the sandbox, named in the finding). Not proven never refuses and never
  reads as a pass.
- The packet's Audit row carries the finding's first line, for example `Gate: base ✓,
  head ✓ (7 stages)` or `Gate: base ✗ (ruff format), head ✗ (ruff format, eslint)`. The
  finding also says that the suite's whole-project coverage total is judged by the
  project's own gate, not by the audit.
- It is read from the commit the work starts from, like `test-timeout`: a run that edits
  the list is judged by the list it started with, so it takes effect for runs that start
  after it is committed. A value that is not a list of non-empty word lists stops the
  audit (`error: cannot read the gate checks: pyproject.toml at <commit>: …`).
- The starting commit's results are kept per tree, so the many audits of one run run the
  stages on the starting commit once. A stage that needs files git ignores (such as
  `node_modules`) sees the tree as the audit copies it, without them.
- A tree with this finding is never reused for a format-only edit: a format stage judges
  exactly what such an edit changes, so its stages run again.

Saddle's own repository lists every fast stage of `check.sh` except `npm ci`, mypy (its
`static-check`) and the suite, and a test keeps the list equal to `check.sh`.

### Showing only what the change touches

The audit decides the language of every changed file once (a deleted file counts):
Python (`.py`, `.pyi`), JavaScript (`.js`, `.mjs`, `.cjs`), Markdown, shell, HTML and
CSS. A finding about a language the change does not touch is not shown: a Python-only
change carries no `js-*` findings, a JavaScript-only change carries no Python
coverage, mutation or dead-code findings, and a change touching both shows both. A
finding that refuses (a failure, a block, a question) is never hidden, so a JavaScript
change that breaks the Python suite is still refused, and the suite still runs.
A configuration file (`.toml`, `.json`, `.yaml`, ...) or a file of unknown type can
change what any tool does, so a change touching one hides nothing.

`gate-stage-languages` says which languages each gate stage is about:

```toml
[tool.saddle]
gate-stage-languages = { ruff = ["python"], eslint = ["javascript"], shellcheck = ["shell"] }
```

The key is the stage's name (`ruff format`) or its tool (`ruff`), the value a list of
`python`, `javascript`, `markdown`, `shell`, `html`, `css` or `config`. A stage runs,
and appears in the `Gate:` line, only when the change touches one of its languages; a
stage not named always runs. Like the stages, it is read from the starting commit.

## Commands the sandbox shows

The audit runs your tests in a sandbox that shows only the system directories, your
project's virtualenv and the working tree; your home directory is empty. A test that
needs a tool installed under it, such as `node` and a browser for a web page's tests,
does not find the tool, and a suite that skips such tests when the tool is missing
passes without having run them. A project names what its tests need:

```toml
[tool.saddle]
sandbox-expose = ["node", "google-chrome"]
```

- Each name is looked up on the PATH saddle runs with, and the directory the command is
  installed in (the parent of its `bin/`) is shown read-only, so a runtime finds its
  libraries. Nothing can be written there, and nothing else under your home directory
  is shown. A name that is not found is skipped; the command then fails inside the
  sandbox as it would have.
- The sandbox's network is unchanged: a test's own server on the loopback address works,
  and nothing else is reachable.
- It is read like `test-timeout`: from the commit the work starts from, never from the
  tree being judged, so an audited change cannot give itself a tool. An edit takes
  effect for runs that start after it is committed. The value is a list of bare command
  names; a path, a comma or a non-list stops the audit (`error: cannot read the
  sandbox's exposed commands: pyproject.toml at <commit>: …`).
- `SADDLE_SANDBOX_EXPOSE` (comma-separated names, set where saddle is launched) names
  commands for every project; the two add together.
- The audit's own runs read it (`saddle audit`, and the checkpoints, finish audit and
  test-impact map of `saddle auto` and the chat's Task runs). `saddle run` and the
  commands the model itself runs in its sandbox read only the environment variable.

## A prompt benchmark for changed prompts

No test, coverage figure or mutant can judge what a changed prompt does to a model, and
a mutant inside a prompt string is a text-only mutant the audit leaves out. So when a
change edits prompt text, the audit says so in a tier-2 finding named `prompt-effect`:
measured when the project provides a way to measure it, otherwise not proven, by name.

What counts as prompt text is deliberately narrow (`saddle.prompt_changes`): a
module-level UPPER_CASE constant whose last name segment is `PROMPT`, `PROMPTS`, `RULE`,
`RULES`, `PERSONAS`, `GRAMMAR` or `INSTRUCTIONS` and that holds prose (`SYSTEM_PROMPT`,
`EDIT_RULES`), or a function or method whose name ends in `_prompt`, judged by the string
text in its body (an f-string by its whole expression; docstrings and `raise` messages
excluded). Values are compared as the parser reads them, so re-wrapping a string, joining
`"a" + "b"` or reordering a dict is no change. Not detected: a prompt assembled across
helpers or under another name, an inline argument, a tool description, a prompt kept in a
data file. A changed Python file that cannot be parsed on one side is named in the
finding, never read as unchanged.

```toml
[tool.saddle]
prompt-benchmark = ["python", "tools/prompt_bench.py"]
prompt-benchmark-floor = 0.8     # optional: a head score below this fails
prompt-benchmark-margin = 0.05   # optional: a head score more than this below the baseline's fails
```

- With no `prompt-benchmark`, the finding is `not-proven` and reads "prompt text changed:
  `path: NAME`; no test, coverage or mutation result measures a prompt's effect, and this
  project configures no prompt benchmark". It refuses nothing, and the packet's Not proven
  row lists it. **Saddle's own repository configures none**: its benchmark needs a model
  server and lives outside the repository, so a change to saddle's prompts is reported as
  not proven.
- With one, the audit runs the command on the head tree and on a copy of the baseline
  commit's tree (the baseline's readable score is kept for the rest of the run), under the
  same sandbox, memory cap and `test-timeout` as the static check. The sandbox has no
  network, so the command can score cases that run in the tree (a recorded-response
  replay, a deterministic checker over stored model outputs) and cannot call a model
  server. It must exit 0 and its **last stdout line** must be JSON such as
  `{"score": 0.83, "n": 12, "label": "smoke"}`: `score` a finite number, `n` (cases,
  optional) a non-negative integer, `label` (optional) a string. Earlier lines are the
  command's own log.
- The finding then reads `base 0.8 (n=12) -> head 0.83 (n=12)` and the whole record
  (command, both readings, the bars) is sealed with it. With neither bar set it stays
  `not-proven`: the numbers are reported, not judged.
- With `prompt-benchmark-floor`, a head score below it **fails**; with
  `prompt-benchmark-margin`, a head score more than that below a readable baseline score
  fails. Meeting every bar set is a `pass`. A margin with no readable baseline score is
  `not-proven`.
- A benchmark that exits non-zero, times out, cannot be launched or prints no readable
  score on its last line is `not-proven` ("ran but produced no readable score"), never a
  pass and never a fail: a command that cannot run in the sandbox is no evidence about
  the prompt. A person runs it.
- The three keys are read like `test-timeout`: from the commit the work starts from,
  never from the tree being judged, so an audited change can neither drop the benchmark
  nor lower its own floor. A floor or margin with no `prompt-benchmark`, a bar that is
  not a finite number (a margin below 0 included), or a command that is not a non-empty
  list of words stops the audit with `error: cannot read the prompt benchmark: …`.
- A prompt-only change also leaves mutation not proven (`module-level constants only`);
  the two notes are both listed, and neither says nothing is left unproven.

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
| `--save-reasoning` / `--no-save-reasoning` | on | keep each round's whole reasoning with the run, redacted and uncapped, in the sidecar beside its `auto:spend` record; `saddle reasoning` prints it. Narrative: no gate, audit or verdict reads it. Off is sealed in the outcome as `save_reasoning: false` |
| `--check-tool` / `--no-check-tool` | on in arm E+A+F, off in E and E+A | offer the model a `check` tool that runs audit tiers 0 and 1 on the current tree before finish, and tier 2 as finish runs it when the call sets `mutation` (refused with E or E+A) |
| `--allow-installs` | off | offer the model an `install` tool; you approve each request, and an approved one installs from the wheel folder into an overlay of the run's own on the project venv (no network; the project venv is not changed). The run needs a project venv and a wheel folder holding wheels, or it does not start. A terminal run cannot answer, so every request there is refused |
| `--wheel-dir DIR` | `$SADDLE_WHEEL_DIR`, else `~/.local/share/saddle/wheels` | with `--allow-installs`: the local folder of wheels installs come from |
| `--base-url URL` | `$SADDLE_BASE_URL`, else `http://127.0.0.1:18020/v1` | model server (see [Server URL and model](#server-url-and-model)) |
| `--model ID` | `$SADDLE_MODEL`, else `qwen3.8-27b` | model id |
| `--temperature T` | `0.0` | sampling temperature |
| `--reasoning-effort` | `medium` | one of `none`, `low`, `medium`, `xhigh` |

With neither `--no-feedback` nor `--no-audit`, the arm is E+A+F: audits are delivered
to the model, and a failing finish audit refuses `finish`.

While it runs, the command prints one line per tool call. At the end it prints:

```text
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
| `--attach-to LEDGER` | none | audit the commits after the ones a finished run's ledger covers and record the result in that ledger (below); implies `--tiered` |

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

Right after the `OK:` line, for each autonomous run in the ledger, verify says which
commits the ledger covers (shas are shortened to 12 hex digits):

| Line | Meaning |
|---|---|
| `covers: BASE..COMMIT on BRANCH (tree HASH)` | the run's own commit, sealed in the coverage file beside the ledger (`proofs.covers.jsonl`) |
| `covers: not recorded (ledger predates this field, …)` | no coverage file: no commit is named, none is guessed |
| `covers: nothing yet (the run has not ended)` | a run still going |
| `later commits on BRANCH: none` | the branch holds nothing after the covered commit |
| `not covered by this ledger: N later commit(s): SHA SUBJECT; …` | commits on top that no evidence in the ledger covers (the first 10 are named) |
| `later commits on the branch: not checked (WHY)` | the lookup could not be made (not a git repository, branch gone, history rewritten, git failing); never read as "none" |
| `coverage file: N commit record(s) name an outcome this ledger does not hold; they are not counted` | a coverage file from another run |

The later commits are looked up in the `--anchor` repository, else in the checkout the
ledger sits in. After a follow-up is attached, the first line reaches the new head, a
`the run itself ended at …` line follows, and each follow-up is listed as
`follow-up N: FROM..TO, K finding(s), verdict V (counts), audited after the run and attached to
this ledger, not the run's own`. The packet's Reproduce row says the same. The coverage
file is part of verification: an edited record, a second commit record for one outcome,
or a follow-up that does not continue the record before it fails with
`bad-hash`, `attempt-sidecar`, `committed-duplicate` or `followup-chain`. A follow-up
deleted from the end of the file cannot be seen, since the file is append-only.

## saddle reasoning JOURNAL

Prints each round's whole reasoning, in order, under a `--- round N of M` line: what
`--save-reasoning` kept beside each `auto:spend` record (redacted, never cut to the
outcome record's 4000 characters). A round that reasoned nothing is left out; a run
that saved none says `no saved reasoning in JOURNAL (M rounds)`. Exit 1, with nothing
printed, when there is no journal or it does not verify (each sidecar must hash to
the record its span sealed), so a record that cannot be trusted is never shown.

### Attaching follow-up commits: `saddle audit --attach-to LEDGER [REV]`

Commits made on a run's branch after the run (a review fix, say) are outside its
ledger. `saddle audit --repo REPO --attach-to LEDGER` audits the commits from the newest
commit the ledger covers up to `REV` (default: the run branch's tip) with the tiered
battery, prints the audit as usual, and appends its verdict and every finding to the
coverage file as a follow-up record chained to the one before it. A refusing audit is
recorded as refusing; the exit code is the audit's. It refuses with exit 2, writing
nothing, when the ledger does not verify, records no covered commit, `REV` is not on top
of the covered commit, there are no commits after it, or `--baseline` is given.

## Legacy: saddle run and saddle dag

`saddle run` and `saddle dag` are the Phase 1 multi-node pipeline, kept and labelled
legacy (#89): `saddle auto` is the supported lane, the one measured since Phase 2. They
run only on a server with constrained decoding (on one without it, such as Strata, they
refuse at their preflight), they are not measured, and each prints one notice on stderr
naming `saddle auto` every time it starts.

## saddle chat / saddle web

| Flag | Default |
|---|---|
| `--workdir` | `~/saddle-ranch` |
| `--host` | `127.0.0.1` (a non-loopback host gets a token in the URL) |
| `--port` | `8777` |
| `--sessions` | `~/.saddle/sessions` |
| `--base-url`, `--model` | as for `auto` |
| `--no-open` | do not open a browser |
| `--keep-reasoning` / `--no-keep-reasoning` | on: chat turns (Ask and Edit) and the task runs the chat starts keep each round's reasoning, as `auto` does |
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
| `--keep-reasoning` / `--no-keep-reasoning` | on | send each round's reasoning back to the model, in either mode |
| `--base-url`, `--model` | as for `auto` | |

## Opt-in capabilities: `saddle capabilities`

Nothing that reaches the web or runs a third-party server is part of installing
or running saddle. Five capabilities are **off until you turn them on**, and the
model is offered a tool only when its switch is on and what it needs works:

| Capability | What it gives | Needs |
|---|---|---|
| `mcp` | the Edit lane may call the allowlisted tools of `access: acting` MCP servers | the SDK, an `access: acting` server |
| `research` | the `research` tool (Ask and Edit): a quarantined reader that reads the web | the SDK, an `access: reader` server (a fetch server or a browser), `bwrap` |
| `search` | the reader's `search` tool, from a local SearXNG | `saddle search setup` running |
| `browser` | the reader's `browser_*` tools (a Playwright server) | a reader server that offers them |
| `answers` | the reader's `ask_answers` tool (Brave Answers, a costly lead; see below) | `BRAVE_ANSWERS_API_KEY` |

`saddle capabilities` prints each as `on`, `off`, or `unavailable` with the
reason; `saddle capabilities enable research` and `disable` write the switch to
`~/.config/saddle/capabilities.json` (`$SADDLE_CAPABILITIES_FILE`).
`$SADDLE_CAPABILITIES` overrides the file for one process (`research,search`, or
`research=on,browser=off`). The page reads the same status from `GET
/api/capabilities`. Disabled means absent from the tool list; enabled but
unavailable is stated, not offered: with `search` on and SearXNG down, `research`
is still offered (it works from addresses you give it) and its result says search
is unavailable.

**On by default: nothing.** The MCP SDK is the optional extra
`pip install 'saddle-harness[mcp]'`; a saddle without it imports, runs and reports
MCP as unavailable.

## saddle search

A local SearXNG for the reader's `search` tool. `saddle search setup` writes
`settings.yml` (JSON output on, the limiter off) and a fresh secret key
(`secret.env`, owner-readable only, passed to docker as an env file and never
printed) under `~/.config/saddle/searxng` (`$SADDLE_SEARXNG_DIR`), then starts one
container named `saddle-searxng` from the pinned image
`searxng/searxng:2026.10.2-19ffbcd30@sha256:36b0907e...` (`searx.IMAGE`), published
on `127.0.0.1:8888` only, with no restart policy: it does not start on boot.
`saddle search status` says whether it answers JSON; `saddle search stop` stops
and removes it. saddle touches only a container that carries its label
`io.saddle.managed=searxng`: a container of that name that is not saddle's, and
every other container on the machine, is left alone.

`$SADDLE_SEARCH_URL` points the reader at another SearXNG (default
`http://127.0.0.1:8888`; `setup` only runs a local one).

Privacy: a query the reader sends goes from your SearXNG to the search engines
its settings enable (SearXNG's defaults: several public engines), as any
metasearch query does. Nothing else leaves the machine, and only the reader
calls it. Result addresses may be opened by the reader; result snippets are page
text and stay inside the reader.

## Brave search (optional, budgeted)

With a Brave Search API key the reader's `search` uses Brave instead of the
local SearXNG, whose free engines rate-limit within an evening. Nothing is on
by default: `search` must be enabled as above, and a key must be present.

Store the key in a file only you can read:

```bash
install -m 600 /dev/null ~/.config/saddle/brave.env
$EDITOR ~/.config/saddle/brave.env     # add one line: BRAVE_API_KEY=...
```

(`$SADDLE_BRAVE_ENV_FILE` names another file; `$SADDLE_BRAVE_API_KEY` supplies
the key for one process.) A key file that group or others can read is refused
with a message that names the file and the `chmod 600` fix. The key goes only in
the request's `X-Subscription-Token` header; it is never written to a result, an
error, the journal, an event or the state file, and an error that echoes it has
it replaced.

The budget keeps the free tier (1000 searches a month, `$SADDLE_BRAVE_MONTHLY_QUOTA`)
from running out early:

- Searches accrue continuously at 1000 / hours-in-the-current-UTC-month per hour:
  about 1.34 an hour (6.7 per five hours) in a 31-day month such as October
  2026, 1.39 in a 30-day month, 1.49 in February. A search costs one. Unused
  searches carry forward, with no cap but the month's remaining quota (1000
  minus what was used this month, or Brave's own `X-RateLimit-Remaining` count
  when that says less). Everything resets at the start of each UTC month, so
  carry-over never crosses a month. A first-time person has one search at once.
- Requests are at least one second apart.
- A repeat of a query (case, spacing and punctuation ignored) is answered from a
  seven-day cache at no cost and labelled `(cached)`.
- When the balance or the month is used up, or Brave answers 429, `search` does
  not call Brave. It falls back to the SearXNG, labelled `(budget used up: free
  search, lower quality; Brave refills in ~X)`, or, with no SearXNG running,
  fails with a named error and the refill time. A 401 or 403 says the key is
  invalid.
- The model is never shown a count: the reader and the `research` tool carry
  fixed guidance (searches are scarce; one specific query; read result pages
  before searching again; never repeat a query; prefer fetching a known page).
  It learns the budget is used up only when a search is refused.

You see the numbers: `saddle search status` and `saddle capabilities` print the
provider, the searches available now and the monthly count left. The budget and
cache live in `~/.local/state/saddle/brave-budget.json` (`$SADDLE_BRAVE_STATE`,
mode 600).

Privacy: with a key, each query the reader sends goes to Brave (api.search.brave.com),
under Brave's terms, instead of to your SearXNG. Result snippets stay inside the
reader as before.

## Brave Answers (optional, budgeted)

The `answers` capability gives the **reader only** a tool, `ask_answers(question)`,
backed by Brave's Answers API (`/res/v1/chat/completions`). The acting session
never gets the tool and never sees the answer text. Off by default
(`saddle capabilities enable answers`; it also needs `research`).

Store its key beside the search key, in the same owner-only file:

```bash
$EDITOR ~/.config/saddle/brave.env     # add: BRAVE_ANSWERS_API_KEY=...
```

(`$SADDLE_BRAVE_ANSWERS_API_KEY` supplies it for one process. The file's mode is
checked as for the search key; the key is never written to a result, error,
journal or state file.)

- **A lead, not evidence.** The reader receives the answer labelled `[AI answer
  from Brave — a lead, not a source; open and read the pages it cites before
  relying on it]` with the pages it cites. Those addresses become openable; other
  addresses in the answer do not. A report may still cite only pages the reader
  actually read, so a report that rests on the answer alone is refused.
- **Dollar budget.** A call costs $0.004 plus $5 per million input and output
  tokens, from the usage Brave reports (a conservative estimate, recorded as
  such, when it reports none). The month's cap is $4.50 (`$SADDLE_BRAVE_ANSWERS_CAP`),
  under the account's own $5 credit; a call is refused up front when what is left
  would not cover a conservative per-call estimate. Spend resets at the start of
  each UTC month. Brave's usage- or spend-limit refusals (402, or 403/429 naming
  a limit) mark the month exhausted. Set the dashboard spend limit too.
- **The model never sees money.** It gets fixed guidance (costly: only after
  search and reading have not settled it; one precise question) and, when the tool
  cannot be used, `Answers is not available now; use search and read pages`.
- **You see the spend:** `saddle answers status` prints spent, remaining and calls
  this month. State: `~/.local/state/saddle/brave-answers.json`
  (`$SADDLE_BRAVE_ANSWERS_STATE`, mode 600).

Privacy: each question the reader asks goes to Brave, under Brave's terms.

## The research tool

`research(question, want)` hands a question to a separate worker turn, the
reader, whose only tools are the `access: reader` MCP servers (`fetch`, a
browser), `search` and `report`: no file, no shell, no project, no secret. Its
servers run in a sandbox over the session's download area
(`<session>/downloads`), never the project, in every session including a
full-access one, and without `bwrap` there is no reader.

- It opens only addresses the person typed, a search result gave, or a page it
  read linked, enforced in code; it types only into a search box; tools that run
  code or submit forms are refused even if the allowlist lists them.
- What comes back is a typed value (a version, yes or no, an identifier, a URL, a
  number, each matched whole by a pattern), a short cited summary (citing pages
  it read, never repeating a run of page text), or the path, size, source and
  hash of a download. Never raw page text.
- A download over 10 MiB is withheld, and deleted, unless you approve it in the
  page. The approval comes after the bytes land in the area the acting session
  cannot read and before it is told they exist.
- In a full-access session a command that names a file the reader downloaded or a
  URL it reported or cited is shown to you with its source before it runs.
- `$SADDLE_RESEARCH_DOMAINS` limits the reader to those domains; a session may
  fetch 40 pages.

Reference servers, both run through the reader on this machine (a scripted model
drove them against a local page; the download came back as path, size, hash and
source):

```json
{
  "servers": {
    "fetch": {
      "command": ["uvx", "mcp-server-fetch==2026.8.18"],
      "version": "2026.8.18", "access": "reader", "tools": ["fetch"],
      "expose": ["uv", "uvx"]
    },
    "browser": {
      "command": ["~/.local/share/saddle/pw-0.0.83/node_modules/.bin/playwright-mcp",
        "--browser", "chrome", "--executable-path", "/usr/bin/google-chrome",
        "--headless", "--isolated", "--no-sandbox"],
      "version": "0.0.83", "access": "reader",
      "tools": ["browser_navigate", "browser_snapshot", "browser_click", "browser_type"],
      "expose": ["node"], "expose_paths": ["~/.local/share/saddle/pw-0.0.83"]
    }
  }
}
```

`mcp-server-fetch` needs `uv` as well as `uvx` shown; Playwright's server is
installed once (`npm i @playwright/mcp@0.0.83` in that directory), and its
directory and `node` are shown through `expose_paths` and `expose`, because a
sandbox hides your home. Chrome needs `--no-sandbox` inside bwrap, and
`--isolated` keeps its profile in memory. A server's sandbox shows nothing else
of yours.

## saddle mcp

MCP (Model Context Protocol) servers the Edit lane may use. saddle starts only
servers the person names in an allowlist file, exposes only the tools named
there, and shows the person what each server says about its tools before the
first use.

Off by default (`saddle capabilities enable mcp`). In the web chat a server that
is not yet approved, or whose descriptions changed since, is shown to you in a
dialog when a turn needs it; in the terminal, approve it first with
`saddle mcp approve`.

`saddle mcp list` shows the allowlist. `saddle mcp approve NAME` starts the
server in a sandbox over an empty folder, prints every exposed tool's
description exactly as the server gave it, and records approval only on a typed
`y` or `yes`. There is no `--yes`. An approval is bound to the command and to
each exposed tool's name, description and input schema, so a server that
changes a description after an update is refused until the person approves
again.

The allowlist is `~/.config/saddle/mcp.json` (`$SADDLE_MCP_CONFIG` overrides it;
approvals are in `~/.config/saddle/mcp-approved.json`, `$SADDLE_MCP_APPROVALS`):

```json
{
  "servers": {
    "notes": {
      "command": ["uvx", "some-notes-server==1.2.3"],
      "version": "1.2.3",
      "access": "acting",
      "tools": ["search_notes", "read_note"]
    }
  }
}
```

- `command`: the argv that starts the server over stdio. It must name `version`,
  so a pin cannot be only decorative.
- `access`: `acting` (the session that edits files may call the tools) or
  `reader` (only the quarantined web reader may; never the acting session).
- `expose` (optional): command names (`node`, `uv`, `uvx`) shown read-only inside
  the server's sandbox, as `$SADDLE_EXPOSE` does for gates. `expose_paths`
  (optional): directories shown read-only at their own path (where the server is
  installed). Both are part of what you approve, and a path that is missing is a
  named failure.
- `tools`: exact tool names, no wildcards. The model sees `mcp__notes__search_notes`
  and nothing else the server has. A listed tool the server does not offer
  refuses the server.

A malformed file stops `saddle up --mode edit` and the web chat's Edit turns
with an error naming the fault; it is never read as an empty list. The Edit lane
alone offers these tools: Ask, Task, benchmark and unattended runs never do.
Every call is journaled like a built-in tool call. A server runs in the
session's sandbox (outside it only in a full-access session), inside its own
memory-capped scope, and is in the session's process list, so ending access or
stopping all processes stops it. A server that crashes, hangs past its
timeout or cannot start is reported by name, never as an empty result.

A full-access session can edit the allowlist and the approvals like any file in
the person's home, so the allowlist confines a sandboxed session, not a
full-access one.
