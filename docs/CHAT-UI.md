# The chat UI (`saddle web`)

```bash
saddle web                      # opens http://127.0.0.1:8777/
saddle web --workdir ~/code/x --port 9000 --no-open
```

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
                                                └──>  SSE → app.js (saddle web)
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

Commands run under `bwrap` when it is present (read-only system, writable
workdir) and as the invoking user when it is not. `Sandbox.isolation` says
which, rather than implying protection that is absent.

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

## Nothing is stingy

`saddle up` sent a flat `max_tokens: 8192` on every reply. Against a server
reporting a **175,000**-token window that is a 5% cap, and a turn reasoning
at `xhigh` can spend 40,000-80,000 tokens thinking before it writes a word
-- so the reply arrives truncated mid-thought.

The budget is now sized per call from the window left after the
conversation, the rule `saddle run` has used since T6-17:

| | |
|---|---|
| fresh turn, 175k window | **172,952** tokens |
| after a 400k-character history | 72,952 |
| floor, however full the window | 8,192 |

The window is read from the server (`max_model_len`), not assumed.
Reasoning effort is per session and defaults to `xhigh`, because F21.17
measured every degenerate draw of round 3e at effort `low` and every clean
one at `xhigh`.

Other limits, all deliberate and none of them 8192: 24 tool rounds per
turn (was 10), 200,000 characters per file read, 400,000 per terminal
capture (head-and-tail, so a runaway loop cannot exhaust memory), and
reasoning itself is **never** truncated in the UI.

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
