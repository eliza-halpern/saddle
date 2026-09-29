<img width="492" alt="saddle" src="docs/saddle-logo.svg" />

# Saddle

Break your local model to harness.

Saddle is a deterministic execution harness that turns a non-deterministic local LLM into a dependable coding tool: constrained DAG planning, parallel proof-gated workers, machine-checked gates. No retries on unverified foundations.

**Status:** early. The agent-plus-auditor loop below is built and measured on a small benchmark (details under *Status* below). Don't trust it unattended yet: on harder tasks the auditor has accepted wrong work (#109).

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

**Status as of 2026-09-28:**

- **Measured, no harm:** on two benchmark tasks, six seeds each, the audited agent's
  false-done rate was 0 of 6 on both, the same as the plain agent's, at 0.95x and 0.69x
  of its wall time to a finished evidence packet.
- **Measured, detection:** a rate on planted defects, not a pass. Re-audited offline on
  `48eb6d6`, the auditor refused 65 of 65 planted-bug trees, 49 of 65 for the right
  reason. Every right-reason refusal came from the agent's own tests failing; on one
  task the other gates refused correct and wrong trees alike.
- **Observed, not yet measured:** in single unscored runs on two harder tasks, the
  auditor accepted a tree that failed the task's hidden checks (#109). Measuring that
  rate is next.
- **Not measured:** detection of defects that were not planted.
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

## Install and first run

Check the Requirements below first: saddle needs a model server you run yourself, and
Linux with a working `bwrap`.

```bash
uv tool install saddle-harness      # or: pipx install saddle-harness
```

This puts one command, `saddle`, on your PATH. The tools the auditor runs (`pytest`,
`coverage`, `ruff`, `mutmut`) are installed with it.

Give saddle your server's API key. Either export `SADDLE_VLLM_API_KEY`, or put it in
`~/.config/saddle/env`, which saddle reads by itself:

```bash
mkdir -p ~/.config/saddle
read -rsp 'API key: ' key; echo
(umask 077; printf 'SADDLE_VLLM_API_KEY=%s\n' "$key" > ~/.config/saddle/env); unset key
```

The key is read with echo off, so it stays out of your shell history, and the file is
readable only by you.

Check that saddle can reach the server, then start the chat:

```bash
saddle doctor     # server reachable, key accepted, model served
saddle chat       # opens http://127.0.0.1:8777/ in your browser
```

The default server is `http://127.0.0.1:18020/v1` serving the model id `qwen3.8-27b`.
For any other server, add `SADDLE_BASE_URL=...` and `SADDLE_MODEL=...` lines to the same
file (or export them); `saddle doctor` prints where each value came from.

If the project you point saddle at has tests that import third-party packages, give it
its own virtualenv at `.venv/` or `venv/` in the project folder, with `pytest`,
`coverage` and `mutmut` installed into it beside your packages:

```bash
.venv/bin/python -m pip install pytest coverage mutmut
```

A Task run finds that venv by itself (`sandbox.project_env`; an activated
`VIRTUAL_ENV` counts when the folder has none) and runs both the model's commands and
the auditor's tests on it, read-only. Its packet says which environment the tests ran
on. An audited Task run on a venv that lacks one of those three tools stops before it
starts with a setup error naming it. Without a project venv, the auditor uses the
first `python` on your PATH; with none, a `python3` on PATH that can import all three
(with `mutmut` on PATH too); and otherwise saddle's own interpreter, which has none of
your project's packages (#113). `saddle audit` does not look for the project venv:
activate it first.

A Task run can also install a package the task turns out to need, if you allow it.
Start it with `--allow-installs` (on `saddle auto` or `saddle web`) and put the wheels
in a local folder: `--wheel-dir`, else `SADDLE_WHEEL_DIR` (exported or in the same env
file), else `~/.local/share/saddle/wheels`. The model then has an `install` tool, and
every request is put to you as a question (Install or Refuse; a run nobody can answer,
such as `saddle auto` in a terminal, takes Refuse). An approved install goes into an
environment of the run's own, under `.saddle/runs/<run-id>/overlay`, layered on the
project venv and removed when the run ends; your project venv is never written. pip
runs with `--no-index --find-links <folder> --only-binary=:all:` inside `bwrap` with
no network, so nothing is downloaded and no package's build code runs. A package with
no wheel in the folder is refused as missing, and a run with installs allowed does not
start without a project venv or with a missing or empty wheel folder.

`saddle auto` keeps the project's tests read-only unless you pass `--allow-test-edits`:
a write to a test file is refused at the tool. When the audit wants a test the run may
not write, the run asks whether to allow test edits, but a run in a terminal cannot
answer, so the default (keep them read-only) is sealed and the run cannot write the
test. For a task that asks for a new or changed test, `--allow-test-edits` is the
normal flag. The chat's Task lane ticks **Allow test edits** by default.

To have every Task run from the chat also check the task text's own examples (P1),
start the chat with `saddle web --extract-requirements`, or put
`SADDLE_EXTRACT_REQUIREMENTS=1` in the env file once. The examples are extracted
beside the worker (extra model calls); their findings only ask, never refuse, and a run
whose finish audit asks ends "needs you" with the question on the task card. The card's
Task text row says how long the extraction took and how many units it judged.

The auditor gives each run of the project's tests 300 s and reports a suite that takes
longer as a hang. A project whose suite needs longer sets its own limit in its
`pyproject.toml`, as `test-timeout = <seconds>` under `[tool.saddle]` (saddle's own is
3600). The limit is read from the commit the work starts from, so commit the setting
first; nothing a run changes in its own tree can raise the limit it is judged under
([docs/CLI.md](docs/CLI.md#the-test-time-limit)). A slow suite can also be run on
pytest-xdist workers, with `test-workers = <N>` in the same table (saddle's own is 8;
[docs/CLI.md](docs/CLI.md#running-the-tests-on-workers)).

[docs/USING-SADDLE.md](docs/USING-SADDLE.md) walks through the chat, the lanes and the
evidence packet. Every command and flag is in [docs/CLI.md](docs/CLI.md): `saddle
doctor`, `dag`, `run`, `auto`, `audit`, `tail`, `verify`, `explain`, `chat` (alias
`web`) and `up`.

To work on saddle itself, see [CONTRIBUTING.md](CONTRIBUTING.md#start-here).

## Requirements

- Python 3.12+
- Self-operated vLLM ≥ 0.28 server with guided decoding (XGrammar) + KV offloading —
  the proven setup is [qwen38-27b-rtx3090](https://github.com/syv-ai/qwen38-27b-rtx3090)
- pytest, coverage.py, ruff, mutmut: installed with saddle since 0.1.1; a copy on your PATH wins
- Linux with `bwrap` (bubblewrap) that can start: Task runs refuse to run without it.
  On Ubuntu 24.04 unprivileged user namespaces are restricted by AppArmor, so `bwrap`
  needs the upstream `bwrap-userns-restrict` profile or an equivalent.
- A user systemd manager (`systemd-run --user --scope` works) for the per-command memory
  cap; without one saddle falls back to a weaker per-process address-space ceiling.
  `SADDLE_MEMORY_MAX` sets the cap (bytes, or a number with K, M, G or T; default `6G`).

## Security model

Saddle runs code a model wrote, on your machine, as you. It confines some of that and
not all of it. Run it only on code you would be willing to run yourself.

It needs a model server: a local OpenAI-compatible chat-completions endpoint (vLLM with
structured outputs; `--base-url`, default `vllm.DEFAULT_BASE_URL`,
`http://127.0.0.1:18020/v1`). Your prompts and the files the model reads go to that
server. Point `--base-url` (or `SADDLE_BASE_URL`) somewhere else and they go there instead.

**What is confined** (`sandbox.py`, `memcap.py`):

- **File tools stay in the workdir.** Every path a tool reads or writes is resolved,
  symlinks included, and refused if it lands outside the working directory
  (`sandbox.resolve_within`).
- **Commands run under `bwrap` when it can start** (`Sandbox._argv`). The root is built
  up from the system directories and the interpreter, read-only (`SYSTEM_DIRS`), plus
  the virtualenvs the tools on PATH live in (`default_expose`); HOME and /tmp are
  empty, except that a system `python3`'s user site-packages
  (`~/.local/lib/pythonX.Y/site-packages`, where `pip install --user` puts pytest) is
  shown read-only (`sandbox.user_site`); the workdir is the one writable place, and
  its `.git` is read-only (`Sandbox._git_binds`). Each command gets its own namespaces (sharing the
  host network unless the lane takes it away), its own session and process group, and
  its process group is killed when it ends.
- **The environment is an allowlist** (`sandbox.ENV_KEEP`), so the model key in
  saddle's own environment does not reach a command.
- **A Task run (`saddle auto`) requires isolation and has no network.** It refuses to
  start when `bwrap` is missing or cannot start (`require_isolation=True`,
  `IsolationUnavailableError`), and its commands see loopback only (`network="none"`).
- **Every command the model runs has a memory cap** (`memcap.cap`): a systemd user scope with
  `MemoryMax`, no swap and at most 4096 tasks, or a per-process `prlimit --as` ceiling
  where no user systemd manager is reachable. The cap also covers the audited tree's
  test command and `mutmut run` (`evidence.tree_memory_limit`). Default 6 GiB;
  `SADDLE_MEMORY_MAX` changes it. A command the cap kills is recorded as a failure.
- **The git calls of an autonomous run and of the packet's branch actions run with
  hooks and `core.fsmonitor` off** (`sandbox.HOST_GIT_GUARD`, used in `auto.py` and
  `web/branch_actions.py`), so config planted in `.git` does not run there. saddle's
  other git calls, such as the gates' diffs (`evidence.py`), do not pass it.
- **The chat server binds to loopback by default**; any other bind address requires a
  token (`web.app.needs_token`).

**What is not confined:**

- **The auditor's gates without a working `bwrap`.** Where `bwrap` starts, the gate
  runs of the tree's code (`pytest`, `coverage`, `mutmut run`) get the same boundary as
  a Task command, with no network (`sandbox.confine`, #104). Where it cannot, they run
  as you, with your read access and your network, under the memory cap and the
  allowlisted environment only.
- **Ask and Edit in the chat without a working `bwrap`.** Those lanes use `bwrap` when it
  starts; otherwise commands run as you and the sandbox reports `isolation: none`
  (`Sandbox.isolation`). They keep the host network either way. Ask offers the model
  read-only tools only (`tools.tools_for_mode`); Edit writes your folder directly,
  unaudited.
- **`git`, `ruff` and `coverage` bookkeeping calls** have no memory cap.

## Reading the comments

Comments and docstrings state the claim they rely on, and the evidence behind it, in
plain words. Some name a benchmark task and seed (`T5`, `t8-s1`) or a round of runs
("round 3e"); docs/BENCHMARK-RECORD.md describes the tasks and the rounds. The raw run
records behind those measurements are the author's own and are not public.

## License

Copyright (C) 2026  Eliza Halpern and Eryn Lipkowitz. See [AUTHORS](AUTHORS).

Saddle is free software under the GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later). See [LICENSE](LICENSE).
