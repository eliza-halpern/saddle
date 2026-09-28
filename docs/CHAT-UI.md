# The chat UI (`saddle chat`)

```bash
saddle chat                     # opens http://127.0.0.1:8777/
saddle chat --workdir ~/code/x --port 9000 --no-open
```

`saddle web` is kept as an alias. The key does not need to be exported
first: when neither `SADDLE_VLLM_API_KEY` nor `VLLM_API_KEY` is set, it is
read from `~/.config/saddle/env`. Only those two names are read from that
file, and an exported value always wins.

Sessions live in `~/.saddle/sessions/<id>/` — one directory each, holding
`session.json`, `messages.jsonl`, `chat.jsonl` (the proof journal) and
`uploads/`. The transcript is the record; a crash loses the turn in flight
and nothing else.

## Why a browser and not a TUI

The features asked for — clicking a tool call open, dragging a file in,
seeing reasoning stream into a block that collapses itself — are things a
browser does well and a terminal does badly. `saddle up` keeps working for
terminal use; both consume the **same event stream**, so they cannot
disagree about what happened.

## The shape

```
engine.run_turn()  ──yields──>  events.Event  ──┬──>  Timeline   (saddle up)
                                                └──>  SSE → app.js (saddle chat)
```

`chat.py` used to call `Timeline` methods directly, which welded the loop to
one renderer. The engine now yields typed events and owns no formatting:
streaming, tool rounds, journaling and compaction. Adding a renderer adds no
risk to the engine.

## What each piece does

| module | |
|---|---|
| `events.py` | the typed event stream; every event JSON-serialisable |
| `engine.py` | one turn: stream, tool rounds, journal, seal, compact |
| `labels.py` | a tool call in three tenses (below) |
| `memory.py` | deterministic compaction; no model call |
| `sandbox.py` | the workdir boundary and background terminals |
| `tools.py` | read, write, list, search, run, and the terminal tools |
| `sessions.py` | sessions and personas on disk |
| `web/app.py` | HTTP, SSE fan-out, uploads, the folder picker |
| `web/static/` | the UI: one HTML file, one CSS file, one JS file, no build step |

## Three tenses

A tool row carries one label that changes with its state, and the label
comes from the **server** so the terminal and the browser say the same
words:

    Running pytest -q   ->   Ran pytest -q   |   Failed to run pytest -q

An unlabelled tool degrades to `Calling <name> <object>` rather than to a
raw JSON blob, and malformed arguments still produce a label instead of
raising.

## Reasoning

Opens itself while it streams, folds itself away when the answer begins,
and the summary says **how long it thought** (`Thought for 5.7s`) because
that is what the reader waited for. It is never truncated: collapsing hides
text, it does not discard it.

## The workdir is a boundary

It used to be a default `cwd` and nothing else, so
`read_file("../../.ssh/id_rsa")` worked. Every path is now resolved and
required to stay inside the root, **symlinks included** — resolve first,
then check containment, so a link out of the tree is caught by where it
lands rather than by how it is spelled.

Commands run under `bwrap` when it works on the box, and as the invoking
user when it does not. `Sandbox.isolation` says which, rather than implying
protection that is absent; Task runs refuse to start without it.

Under `bwrap` a command sees:

- the system directories and the interpreter, read-only;
- an empty HOME and an empty /tmp, except the venvs saddle's gate tools
  (`python`, `pytest`, `ruff`, `coverage`, `mutmut`) resolve to on the
  command's PATH or through `VIRTUAL_ENV`, read-only (`default_expose`);
- the workdir, writable, with its `.git` read-only again;
- an allowlisted environment, never saddle's own (no model key);
- in Task runs, no network.

**Every command is capped.** A command and everything it starts run in a
cgroup scope of their own (`systemd-run --user --scope`) with `MemoryMax`
(`SADDLE_MEMORY_MAX`, default 6 GiB), no swap and a task limit, so a runaway
allocation or a process storm is killed inside that scope and nowhere else.
The command's output ends with the reason and it reads as a failed command,
not a hung session. The audit gates' test, coverage and mutation runs get the
same cap. Without a user systemd manager the cap falls back to a per-process
address-space ceiling (`prlimit --as`).

**Commits are host-side.** A command cannot write `.git`, so `git commit`
from a command fails in every lane. saddle commits a run's work itself when
the run ends, and the packet's merge and discard actions (each confirmed by
the user) carry it into the checkout. Every git command saddle runs there
passes `-c core.fsmonitor= -c core.hooksPath=/dev/null`, so a hook or an
fsmonitor in the repo's config is not run on its behalf.

## Background terminals

A build takes minutes; blocking the conversation on it is what makes an
agent feel dead.

```
run_command(command, background=true)  ->  terminal id, immediately
read_terminal(id)                      ->  output so far
wait_for_terminal(id, timeout)         ->  waits, bounded
```

**A timeout is not a kill.** The command keeps running and can be waited on
again, which is what lets the model start work, say what it started, and
come back to the result. Output is capped head-and-tail per terminal, so a
runaway `yes` loop cannot take the session down.

Output **streams into the UI as it happens**. A background command keeps
producing output after the tool call that started it has returned, so it
cannot be handed back from that call -- the sandbox pushes each chunk to a
listener, the server publishes it as a `terminal.output` event, and the
browser fills a terminal block keyed by terminal id. Verified with a
command printing one number per second: the numbers arrive one per second,
not all at once at the end. A listener that raises is dropped rather than
allowed to stop the command.

## Nothing is stingy

`saddle up` sent a flat `max_tokens: 8192` on every reply. Against a server
reporting a **175,000**-token window that is a 5% cap, and a turn reasoning
at `xhigh` can spend 40,000-80,000 tokens thinking before it writes a word
-- so the reply arrives truncated mid-thought.

The budget is now sized per call from the window left after the
conversation, the rule `saddle run` also uses:

| | |
|---|---|
| fresh turn, 175k window | **172,952** tokens |
| after a 400k-character history | 72,952 |
| floor, however full the window | 8,192 |

The window is read from the server (`max_model_len`), not assumed.
Reasoning effort is per session and defaults to `xhigh`, because in round 3e
every degenerate draw was at effort `low` and every clean one at `xhigh`.

Other limits, all deliberate and none of them 8192: 24 tool rounds per
turn (was 10), 200,000 characters per file read, 400,000 per terminal
capture (head-and-tail, so a runaway loop cannot exhaust memory), and
reasoning itself is **never** truncated in the UI.

## Images

This model is vision-capable, so an uploaded image becomes real
`image_url` content the model looks at -- not a filename it is told about.
Verified end to end: a generated test picture came back as *"A red
rectangular border frames a blue circle and a yellow square on a cream
background"*, which is exactly what was drawn.

Non-image uploads are named in the prompt instead, so the model knows to
`read_file` them from `uploads/`.

Only files the session actually uploaded may be attached: a request naming
any other path is filtered, so a crafted call cannot read an arbitrary
file off the machine. Image parts are charged a flat 1,200 tokens in
compaction rather than by their base64 length -- charging a photo 200,000
tokens would evict the conversation around it.

## Compaction

Deterministic and staged — no model call, because a summariser that is
itself a model call adds latency, a failure mode, and a second thing to
distrust. Oversized tool results are elided head-and-tail first; only then
are whole old exchanges dropped, and a note recording what went is inserted
in-band. The system prompt and the recent tail are never removed, and the
UI shows a `Compaction` notice — compaction is never silent.

## Event fan-out

Each connected client gets **its own queue**. A single shared queue looked
right and was not: `queue.get` hands each event to exactly one consumer, so
a second tab — or a reconnect that briefly overlaps the connection it
replaces — silently splits the stream and both render half a conversation.

## Markdown

Small on purpose: fenced code, headings, lists, blockquotes, bold, italic,
inline code, links. Everything is inserted as text nodes and built
elements, never `innerHTML`, so model output cannot inject markup; a
`javascript:` link is rewritten to `#`. What the renderer does not know
stays literal, which is the safe failure — an unrendered asterisk is ugly,
a swallowed line is a lie about what was said.
