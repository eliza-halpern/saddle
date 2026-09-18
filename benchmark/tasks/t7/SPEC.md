# T7: build the `orderedlist` package — OrderedList

Create an installable Python package named `orderedlist` implementing
`OrderedList`, a sorted-list container. Start from the empty repo.

## Layout (required)

- `pyproject.toml` configuring an installable package (`pip install -e .`
  must succeed).
- `orderedlist.py` (single module is fine; you may split into a package
  as long as this import works): `from orderedlist import OrderedList`.
- Stdlib only. Deterministic (no network, clock, or randomness).
- Any correct implementation passes: a plain list + `bisect` is fine.
  Correctness is graded, not speed (the hidden suite must finish
  within 300 seconds).

## OrderedList spec (normative)

`OrderedList(iterable=None)` holds values in ascending sorted order
(`<` comparison). Duplicates are allowed and preserved. The optional
constructor iterable may be any iterable (including generators); its
values need not be sorted.

- `insert(value) -> int`: insert `value`, keeping ascending order.
  Returns the index where it was inserted: the leftmost position at
  which it sorts (a new equal element goes BEFORE existing equals).
- `extend(iterable) -> None`: insert every value of any iterable.
- `clear() -> None`; `copy() -> OrderedList` (independent copy).
- `count(value) -> int`: number of occurrences.
- `index(value, start=None, stop=None) -> int`: leftmost index of
  `value` within `[start, stop)`, with EXACTLY `list.index` semantics
  (None/negative/out-of-range clamping, ValueError when absent).
- `remove(value) -> int`: remove ALL occurrences of `value`; return
  how many were removed. Raise ValueError if none present.
- `discard(value) -> bool`: remove ONE occurrence; return True if
  anything was removed, else False. Never raises for missing values.
- `pop(index=-1)`: remove and return the element at `index`
  (IndexError exactly like `list.pop`).
- `rank_left(value) -> int`: count of elements strictly less than
  `value` (== `bisect.bisect_left` position).
- `rank_right(value) -> int`: count of elements less than or equal
  (`bisect_right` position). `rank(value)` is an alias of rank_right.
- `iter_slice(start=None, stop=None, reverse=False)`: iterator over
  positions `[start, stop)` (None = ends); `reverse=True` yields them
  back to front.
- `iter_range(minimum=None, maximum=None, inclusive=(True, True),
  reverse=False)`: iterator over values `v` with
  `(v > minimum or (inclusive[0] and v == minimum))` and
  `(v < maximum or (inclusive[1] and v == maximum))`; None bounds are
  open. `reverse=True` yields back to front.
- Sequence protocol: `len()`, iteration, `reversed()`, `in`.
  `ol[i]` returns the element (IndexError like list indexing).
  `ol[i:j:k]` returns a plain `list` with EXACTLY list-slice
  semantics (including step-0 ValueError). `del ol[i]` /
  `del ol[i:j:k]` delete like list.
- `ol[i] = v` raises NotImplementedError. `ol.reverse()` raises
  NotImplementedError (use `reversed(ol)`).
- Comparisons (`== != < <= > >=`) compare as sequences against
  `list`, `tuple`, and `OrderedList`; against anything else the
  object is unequal (`==` False, `!=` True, orderings raise
  TypeError via reflected fallback — matching sequence rules).
- `ol + other` / `other + ol` return a NEW OrderedList with all
  values of both (`other` is any iterable, need not be sorted).
  `ol += other` extends in place. `ol * n` returns a NEW OrderedList
  with n shallow copies; `ol *= n` updates in place.
- `repr(ol)` is `OrderedList([...])` using the runtime class name
  (subclasses show their own name). Pickle round-trips preserve
  equality. `copy.copy(ol)` returns an independent copy (like
  `.copy()`); note `copy.copy` does not call `.copy()` for you.

## Done when

`pip install -e .` works, `from orderedlist import OrderedList`
resolves, and every behavior above holds (a hidden suite checks the
full API incl. slices, rankings, iterators, dunders, and errors).
Write your own tests as you go — the repo ships with none.
