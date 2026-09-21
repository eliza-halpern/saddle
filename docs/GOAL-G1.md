# Goal G1 — the canonical text

**This file is the authoritative copy.** A Stop hook recites G1 at the end
of every turn from a copy held app-side, which no file on disk controls —
neither `.claude/settings*.json` (neither has a `hooks` key) nor anything
under `~/.config/Claude`, checked including the binary stores. That copy
lags: as of 2026-09-21 it still says `fees.py` reached 1334 lines (the
tree holds 1337), that round 3e's reasoning is unread (read, F21.35), that
the known-bad needs freezing (frozen and byte-identical at
`tests/fixtures/degenerate_round3e.diff`), and that T6-31's gate-detail
routing is where the wording changes (it is not — that draw was attempt 1
and no gate had run).

When the hook and this file disagree, **this file wins**. Before acting on
any claim in either that something is unread, unbuilt or unfixed, grep the
repo — docstrings included — and `../saddle-bench/runs/FINDINGS.md` for the
correction first.

---

Goal G1 -- a T5 run whose gates predict the oracle.

The objective is NOT that the gates pass. That objective is satisfiable
without implementing anything, and in round 3e it was: eleven gates saw a
fees.py grown from 41 lines to 1337 by repeating one 21-line block sixty
times, and nine passed it -- mutation included, at 88.8% over 80 mutants,
because a `pass` body admits no mutant while importing the module
executes it.

Do not read that as the worker gaming a metric. The record was corrected
and the correction changes what you build: that draw is n2 attempt 1,
which carries no failure brief at all (`failure` starts None in
`_run_node` and is assigned only after a gate run), so the
"71.4% < 100.0%" string was that attempt's OUTPUT, not its input. Its
47 586 characters name no gate, no percentage and none of the emitted
helpers, and they end coherently; the repetition begins in the content
tokens after the reasoning stops. It is an emission phenomenon. So the
gate cannot key on intent -- and neither does this goal. State what is
inadmissible, never what the worker meant.

The objective is AGREEMENT between the run's gate verdicts and
oracles/oracle_t5.py, which the worker never sees and which is never a
gate. It is ground truth because it is out of reach.

DONE WHEN three consecutive seeds at temperature 0.7 each end in
agreement: every node seals and the oracle passes, or a named node fails
and the oracle fails for a reason that node's gate detail already gave.

A run where every gate passes and the oracle fails is not partial
progress. It is a gate defect, is recorded as one, and resets the count
to zero. One seed cannot separate a mechanism from the spread that gave
round 3d attempts of 581 s, 534 s and 2494 s; three is the floor.

No item closes by narrowing a population, lowering a threshold or
exempting a rule unless it also exhibits the known-bad it now admits
(WORKPLAN §0.6). Every gate added under this goal is closed by a known-
good it accepts and a known-bad it rejects -- both halves, or not done.

Loosen gates as the evidence requires; never the oracle and never the
hidden suite. The moment those move, the measurement is unanchored and
every prior round becomes unreadable: they are the fixed point the rest
of the method hangs off. When a gate and the oracle disagree because the
TASK is ambiguous, the repair is the task text -- not either instrument.

INADMISSIBLE, as specification and not as exhortation: code whose only
purpose is to be executed is not an implementation. A function called
solely so that its body is recorded as executed satisfies no requirement,
and a node that adds one has not done its work. The check that enforces
this is BUILT -- T6-41, `check_dead_additions` -- in the image of
keep_candidate (survivors.py:308): it removes the candidate definitions
and the statements naming them and asks the suite, so the artifact fails
BY CONSTRUCTION, not by threshold. A gate that counts added lines is a
new number to optimise against.

It has two shapes, and a check that sees only the first is not finished.
Round 3e repeated ONE name sixty times. Round 3h's n2 attempt 1 draw 2
added 192 definitions under 192 DISTINCT names, 187 of them mentioned
nowhere but their own `def` line. Any wording built on "copies" misses
the second shape. The verdict belongs to the suite, not to the name.

The known-bad is frozen: tests/fixtures/degenerate_round3e.diff, from
samples[1].diff of sidecar 4a9e00b8 under
runs/round3e/t5-s1/evidence/attempts/. It passed NINE of eleven gates,
mutation included at 88.8%. `check_dead_additions` now rejects it
(dead-definitions=4), naming "_ensure_executed (60 copies)", and its
47 586 characters HAVE been read -- see that function's own docstring
before re-deriving anything about the draw. The all-distinct shape has
its pair too: T6-60 LANDED (8d23465). Both shapes are closed. What that
brief carried was saddle's OWN pre-T6-44 worker rule, "Every changed
line must be executed by the new tests", which the first emitted helper
paraphrases in the word *requirement*; T6-44 removed it, and only the
planner path (cli.py:347 -> T6-58) still launders the proxy.

Do not write "do not game the metrics". An instruction the harness cannot
verify is decorative, and naming a metric makes it salient. State the
behaviour, never the number: "quantize is never called with a JPY amount",
not "71.4% < 100.0%: uncovered fees.py:68". Only the second is satisfiable
by a no-op. T6-44 landed this for the coverage detail, which now reads
"no test runs accounts.py:65, ..." and carries no % character at all. The
mutation detail keeps its ratio on purpose: that is a recorded known-bad,
not an oversight.

Give an honest way out, and say it is not penalised: if a requirement
cannot be satisfied without code that exists only to satisfy a gate, say
so and stop. A recorded refusal naming the requirement beats a passing
artifact that does not implement it, and is not a failed attempt. The
justification is structural, not round 3e's -- that attempt carried no
failure brief and was never cornered. The path is T6-46, still unbuilt.

NO VERDICT FROM A SUMMARY LINE. Round 3e's span said "2 gate(s) failed:
ruff, coverage" and its detail said "introduced 4 finding(s)". Both were
accurate; neither mentioned the 1337-line file, and the conclusion drawn
from them was wrong. Before any verdict: read the model's reasoning first
(samples[i].thinking, whole -- look for loops, for a gate or percentage
named as the target, and for reasoning that contradicts its own diff);
read samples[i].diff whole and count repeats IN ADDED LINES ONLY (counting
occurrences in the diff TEXT reads a deletion as an emission, and did --
a draw that removed 1 156 lines was reported as writing 767); reproduce
the gate with saddle's own functions on a restored copy,
PATH=.venv/bin:$PATH; run `saddle explain` (the journal is positional --
there is no --repo); read metrics_report.py; and check whether the
sidecar says "gated in place", which means no baseline restore. State
which of these you did and which you could not.

Sidecars are written to BOTH <run>/t5-s1/t5-saddle/.saddle/attempts/ and
<run>/t5-s1/evidence/attempts/, and their schema varies per round and per
attempt -- a timed-out attempt has no `gates` key at all. Take the richer
of the two copies per stem; never probe one file's keys and generalise.
The same drift runs INSIDE a sidecar: a pre-T6-36 record truncates the
top-level `prompt` to 4 024 chars with "[truncated 33168 chars]" while
`samples[i]["prompt"]` holds all 37 168 with no marker. A truncated
prompt does not mean the prompt is lost; take the richer key too.
Read node kind from the plan record (the first line of proofs.jsonl),
never from the node id.

Before acting on any claim in THIS text that something is unread, unbuilt
or unfixed: grep the repo -- docstrings included -- and the bench's
FINDINGS.md for the correction first. Four of this goal's original
instructions had already been carried out rounds before it stopped asking
for them, and the re-derivation cost a session. Line numbers in standing
text drift; cite the symbol and confirm it still exists.
