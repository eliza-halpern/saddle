# CLI reference: `saddle auto`, `saddle audit --tiered`, `saddle verify`

This reference is taken from `saddle.cli.build_parser` and the command handlers. Run
`saddle <command> --help` for the same text. `auto` needs an API key
(`SADDLE_VLLM_API_KEY` or `VLLM_API_KEY`, or `~/.config/saddle/env`). `audit` and
`verify` do not.

## saddle auto TASK

This command runs one task autonomously in a new worktree. The result is a branch.

| Flag | Default | Meaning |
|---|---|---|
| `TASK` | required | what to do, in words |
| `--repo REPO` | `.` | git repository to work on |
| `--time-budget S` | `1800` | wall-clock seconds before an honest stop |
| `--token-budget N` | `100000` | generated tokens before an honest stop (the prompt is not counted) |
| `--allow-test-edits` | off (tests read-only) | let the run edit test files |
| `--no-feedback` | | arm E+A: audit and journal, but deliver nothing and never refuse finish |
| `--no-audit` | | arm E: no auditor at all (mutually exclusive with `--no-feedback`) |
| `--sanctioned-test-rewrite NAME` | none | repeatable; see AUDIT-TIERS.md |
| `--finish-refusal-cap N` | `3` | stop `audit unresolved` after N consecutive refusals on an unchanged finding set; must be ≥ 1 |
| `--base-url URL` | `http://127.0.0.1:18020/v1` | model server |
| `--model ID` | `qwen3.8-27b` | model id |
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
| 3 | stopped (a budget, `audit unresolved`, a model error, cancelled, needs you) |
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

Exit 2 if the token file cannot be created (`FileExistsError`).
