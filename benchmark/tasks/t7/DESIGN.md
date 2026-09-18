# T7 adaptation design (decontaminated distillation)

## Provenance

- Benchmark: NL2Repo-Bench task `sortedcontainers` (difficulty Hard),
  dataset repo multimodal-art-projection/NL2RepoBench @ 781a1da.
- Upstream project: grantjenks/python-sortedcontainers @ 3ac3586
  (Apache-2.0; spec states the license). Tests adapted from the
  upstream suite with attribution headers (derivative permitted).
- This task is a SUBSET (SortedList only) + RESKIN (renamed API) +
  PERTURBATION (3 behavior deltas). It is not the benchmark task
  itself; it inherits the problem structure and test philosophy.

## Subset scope

SortedList public API only. EXCLUDED: SortedKeyList/SortedDict/
SortedSet, all view classes, `key=` constructor support, benchmarks,
docs, all `_`-private members (`_check/_reset/_load/_len/_maxes/
_lists/_index/_update/_clear/_build_index`, `.key`).

Source test files (upstream):
- tests/test_coverage_sortedlist.py (58 tests) -> adapt minus
  test_build_index, test_check (pure internals), test_repr_recursion
  (needs internal list surgery; untestable via public API),
  test_pickle (keep round-trip + equality, drop _load asserts).
- tests/test_stress_sortedlist.py (1 test, repeat=1000, seed 0) ->
  adapt with public-API rewrites (see rules).

## Reskin map (mechanical decontamination)

| Upstream | Adapted |
|---|---|
| package `sortedcontainers`, `src/` layout | package `orderedlist`, flat `orderedlist.py` + `pyproject.toml` (`pip install -e .` must work; `from orderedlist import OrderedList`) |
| `SortedList` | `OrderedList` |
| `add(value)` | `insert(value)` |
| `update(iterable)` | `extend(iterable)` |
| `bisect_left/right(value)` | `rank_left/right(value)` |
| `bisect(value)` (= right) | `rank(value)` (= rank_right) |
| `islice(start, stop, reverse)` | `iter_slice(start, stop, reverse)` |
| `irange(minimum, maximum, inclusive, reverse)` | `iter_range(minimum, maximum, inclusive, reverse)` |
| `clear/copy/count/index/pop/remove/discard` | same names (see perturbations) |
| dunders, `reverse()`, `__setitem__` | same names, same raising behavior |
| `repr` | `OrderedList([...])` via `type(self).__name__` |

## Perturbations (semantic decontamination)

- P1: `insert(value)` returns the insertion index (bisect-left
  position; new element sorts BEFORE existing equals). Upstream
  `add` returns None.
- P2: `remove(value)` removes ALL occurrences, returns the count
  removed. Missing value still raises ValueError. Upstream removes
  one, returns None.
- P3: `discard(value)` returns True if an element was removed, else
  False. Upstream returns None.

New hidden tests (~12 asserts): P1 index incl. duplicates/empty;
P2 count + all-gone + missing-ValueError; P3 True/False;
`__setitem__` raises NotImplementedError (upstream untested).

## Adaptation rules (for the test adapter)

1. Rename per map; strip EVERY `_`-private/internal use; delete the
   3 dropped tests; adjust the ~6 tests asserting old P1-P3 behaviors
   (test_add* need no change — return unused; test_discard's
   `== None` asserts become P3 asserts; test_remove* become P2
   asserts with new expected contents).
2. Stress file: replace `slt._len` with `len(slt)`; rewrite the
   `len > 2000` shortening block with public API only
   (clear + extend a 1000-prefix); keep seed 0 and repeat=1000
   (operator may reduce after measuring reference runtime).
3. Keep every remaining assert semantically identical; report
   assert counts before/after per file.
4. No new behaviors beyond the P-tests + setitem test. No invented
   APIs. Deterministic only (seeded).
5. Deliver to /tmp/t7-build/hidden_tests/ (test_hidden_orderedlist.py
   + test_hidden_stress.py), attribution header on each.

## Grading

Throwaway venv per grading: `pip install -e .` on a COPY of the
arm's tree, `python -m pytest hidden` with 300s budget (reference
runtime measured; suite must complete — budget kills only
pathological implementations). Correctness graded, not speed.

## Decontamination proof (operator)

/tmp/t7-build/decon_proof.py: import upstream SortedList, shim the
adapted names onto it (insert=add, rank_left=bisect_left, ...),
run the P1-P3 hidden tests -> MUST FAIL. Memorized upstream code,
even name-aliased, cannot pass.
