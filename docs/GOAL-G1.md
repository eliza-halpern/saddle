# Goal G1 — the canonical text

**This file is the authoritative copy.** A Stop hook recites G1 at the end
of every turn from a copy held app-side, which no file on disk controls —
neither the agent host's project settings (no `hooks` key) nor anything
in its app configuration, checked including the binary stores. That copy
lags: as of 2026-09-22 it still says `fees.py` reached 1334 lines (the
tree holds 1337), that round 3e's reasoning is unread (read, F21.35 —
re-derived a second time on 2026-09-22 as F21.60 and retracted, because
the hook said so again), that the known-bad needs freezing (frozen and
byte-identical at `tests/fixtures/degenerate_round3e.diff`), and that
T6-31's gate-detail routing is where the wording changes (it is not —
that draw was attempt 1 and no gate had run).

When the hook and this file disagree, **this file wins**. Before acting on
any claim in either that something is unread, unbuilt or unfixed, grep the
repo — docstrings included — and `../saddle-bench/runs/FINDINGS.md` for the
correction first. Make that grep the FIRST action of such a turn, not a
check performed afterwards: on 2026-09-22 the reading was already done
and written up before the grep happened, twice, and both write-ups had to
be retracted (F21.57, F21.60).

**A superlative is a census claim.** "First", "never", "every", "always",
"only" each name a whole population, so none may be written from the runs
at hand. This file asserted a first that was not one; `find runs -name
proofs.jsonl` over all 48 journals shows the first test node sealing in
NINE runs. The population is also wider than the obvious name — the first
test node is `node-1` in some runs and `n1`, `test-multi-currency` or
`test-filterlang-spec` in others, so grepping one spelling confirms a
false claim rather than refuting it.

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

DONE WHEN three seeds at temperature 0.7 end in agreement, drawn from at
most five seeds on ONE pinned harness surface, with the denominator
reported: every node seals and the oracle passes, or a named node fails
and the oracle fails for a reason that node's gate detail already gave.

They need not be consecutive. The earlier wording said so and was wrong
in a way worth naming: "three in a row" fires with probability 1 for any
success rate above zero -- keep drawing and it always arrives, sooner if
the mechanism is good and later if it is bad. It measures patience, not
reliability, and nothing in this file ever argued for it. The argument
below is for THREE, and it survives intact. What consecutiveness was
doing honestly is done better by the two clauses it hid behind: a fixed
denominator, and one pinned surface.

So state N. Three of five is a 60% floor; three of three is a stronger
claim than three-in-a-row ever was. If it takes more than five seeds to
bank three, that IS the finding -- record the rate and repair the
mechanism rather than extending the draw.

ONE PINNED SURFACE means the harness core -- slice, gates, evidence,
runner, survivors, dag, journal, scheduler, sandbox -- is byte-identical
across every seed in the denominator. Verify with `git diff` over those
paths between each seed's recorded HEAD, and record the result beside the
count. Landing any change to them empties the denominator and starts a
new one. That is the real cost of a mid-count fix, and it is worth paying
when the fix addresses the failure the denominator just recorded.

A run where every gate passes and the oracle fails is not partial
progress, and it is not merely a miss. It is a gate defect -- saddle
certified work that is wrong -- is recorded as one, and resets the count
to zero. That reset stays, and it is the one place a reset belongs: a
false pass disqualifies the instrument, not just the sample. A run where
a node FAILS and the oracle fails for some other reason is a miss: it
counts against the denominator and resets nothing.

One seed cannot separate a mechanism from the spread that gave round 3d
attempts of 581 s, 534 s and 2494 s; three is the floor.

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

LANDED IS NOT BUILT, and this text says "BUILT" in four places. A fix
that its own suite exercises may still be inert in production, and one
was: T6-75 added `compelled_lines` so `coverage` would stop failing a
node for lines `public-deletions` forbids it to delete, and shipped with
a wiring test written for exactly this hazard -- its docstring says
"removing the argument at the call site left all 949 tests green, which
is T6-63's shape exactly -- a threaded value that would have shipped
inert." The wiring was real. The test hand-built `changed={("n1.py", 2)}`
-- workdir-relative -- while `runner.py`'s only production caller builds
`changed = {(str(workdir / path), line) ...}` -- absolute. The exemption
is applied as `spared = changed & set(compelled)`, so in every real run
it intersects absolute against relative and spares NOTHING. `g1-79cd848`
reproduced T6-75's trap in full on a tree carrying T6-75's fix: node-2
failed `coverage` on `accounts.py:101-122`, deleted those four functions
next attempt, and drew `public-deletions` naming `Account.__eq__`,
`__repr__`, `from_dict`, `to_dict` (F21.62).

So a check is closed only when its known-good and known-bad are built
the way the PRODUCTION CALLER builds them. A fixture whose shape the
caller never produces tests the fixture. Where a gate input is a set of
`(path, line)` pairs, name which spelling it is in, and prefer one
producer over three that each choose independently.

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

A gate's `basis` -- the sealed half, where counts live once T6-44 took
them out of `detail` -- is NOT in the sidecar: every `gates[]` entry
there records `basis: null`. It survives only in the journal, under
`gate_outputs[]` on the sealed-node record -- whose `record_type` is
**`proof`**, while its `kind` is `test`, so keying on the wrong one of
those two finds nothing. (`record_type` takes `plan`, `span`, `proof`;
there is no top-level `checks` key anywhere in proofs.jsonl.) A census
that reads sidecars, or that guesses either key, finds zero and reports
the mechanism never fired when it has merely looked in the wrong place. Confirm any such census is
non-vacuous first: count the records that carry a non-empty basis, and
check a control mechanism that DID fire (`deferred-lines=`, T6-53)
appears where expected.

WHERE THE COUNT STANDS (dated 2026-09-22, recount before citing).
**Three of four, on surface `g1-30bde7b`. The stopping rule is MET.**
Seeds s1, s3 and s4 each sealed all four planned nodes with the oracle
passing all four of its checks. s2 sealed three, failed n4 and its replan
n4.r1, and the oracle failed -- a miss, not a gate defect, since nodes
failed; it counts against the denominator and resets nothing. No gate
defect has been recorded on this surface. Three agreements from a
denominator of four, inside the cap of five.

Surface verified 2026-09-22 by `git diff` over the nine paths this text
names, between 30bde7b and HEAD: EMPTY. All nine resolve to real modules
(slice, gates, evidence, runner, survivors, dag, journal, scheduler,
sandbox). Note what the check does NOT cover, and why that is still
correct here: `cli.py` DID change mid-count (`5a2d2fa`, 12:32 -- s1 ran
before it, s2 through s4 after), and `cli.py` is not among the nine. Its
diff is confined to the `chat` subcommand, which `saddle run` never
enters. The denominator holds. But a future `cli.py` change touching the
run path would pass this check while moving the surface, so widen the
list before leaning on it again.

Census over all 53 journals, 29 of which carry a plan record, recounted
2026-09-22 after s4 and cross-checked against `find | wc -l`. Count them
with `os.walk`, never `glob(recursive=True)`: the latter silently skips
`.saddle/` and reported 7 of the 52 on 2026-09-22, with no error.

- Five runs sealed every planned node: `g1-30bde7b` t5-s1, t5-s3 and
  t5-s4 (four-node plans) and `regress-99e3986` t1 and t6 (two-node
  plans).
- Of the twelve `g1-*` runs, the eight on surfaces earlier than 30bde7b
  sealed **zero impl nodes between them** -- five sealed nothing, three
  sealed only their test node. On 30bde7b the four seeds sealed eleven
  impl nodes between them (3, 2, 3, 3).
- So the first-impl-node barrier, which every earlier revision of this
  paragraph named as the binding constraint, BROKE on surface 30bde7b.
  Nodes 3 and 4 are no longer undispatched: both sealed in s1, s3 and s4.

Read that shape before choosing what to work on. Reaching the impl nodes
is no longer the constraint. On the single failure this surface has
produced, the constraint was node 4 on `store.py`: its mutation gate
reported survivors in `load_accounts` while the oracle reported store.py
still carrying the untouched baseline. Emission, by contrast, measured
SOLVED in 3j; treating round 3e's shape as the live problem is reading a
fixed defect as an open one, which is exactly what a census pooled across
gate revisions does (F21.36 -> F21.37, and it cost a session).

Before acting on any claim in THIS text that something is unread, unbuilt
or unfixed: grep the repo -- docstrings included -- and the bench's
FINDINGS.md for the correction first. Four of this goal's original
instructions had already been carried out rounds before it stopped asking
for them, and the re-derivation cost a session. Line numbers in standing
text drift; cite the symbol and confirm it still exists.
