"""OrderedList: a sorted-list container (T7 reference implementation)."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import MutableSequence, Sequence
from itertools import chain


class OrderedList(MutableSequence):
    """A list that maintains its values in ascending sorted order."""

    def __init__(self, iterable=None):
        self._list = sorted(iterable) if iterable is not None else []

    # -- mutation ----------------------------------------------------

    def insert(self, value):
        """Insert value; return the index where it went (leftmost)."""
        pos = bisect_left(self._list, value)
        self._list.insert(pos, value)
        return pos

    def extend(self, iterable):
        """Insert every value of any iterable."""
        # Materialize first: `other` may alias this very list
        # (`ol += ol` must terminate).
        self._list.extend(list(iterable))
        self._list.sort()

    def clear(self):
        """Remove all values."""
        self._list.clear()

    def copy(self):
        """Return an independent copy."""
        new = self.__class__()
        new._list = list(self._list)
        return new

    def __copy__(self):
        return self.copy()

    def __reduce__(self):
        return (self.__class__, (list(self._list),))

    def remove(self, value):
        """Remove ALL occurrences; return count removed. ValueError if none."""
        start = bisect_left(self._list, value)
        stop = bisect_right(self._list, value)
        count = stop - start
        if count == 0:
            raise ValueError(f"{value!r} not in list")
        del self._list[start:stop]
        return count

    def discard(self, value):
        """Remove ONE occurrence; True if anything was removed."""
        pos = bisect_left(self._list, value)
        if pos < len(self._list) and self._list[pos] == value:
            del self._list[pos]
            return True
        return False

    def pop(self, index=-1):
        """Remove and return the element at index (list.pop rules)."""
        return self._list.pop(index)

    def reverse(self):
        """Not supported: sorted order cannot be reversed in place."""
        raise NotImplementedError("use ``reversed(ol)`` instead")

    # -- lookup ------------------------------------------------------

    def count(self, value):
        """Number of occurrences of value."""
        return self._list.count(value)

    def index(self, value, start=None, stop=None):
        """Leftmost index of value in [start, stop); list.index rules."""
        total = len(self._list)
        if start is None:
            start = 0
        elif start < 0:
            start = max(0, total + start)
        if stop is None:
            stop = total
        elif stop < 0:
            stop = max(0, total + stop)
        return self._list.index(value, start, stop)

    def rank_left(self, value):
        """Count of elements strictly less than value."""
        return bisect_left(self._list, value)

    def rank_right(self, value):
        """Count of elements less than or equal to value."""
        return bisect_right(self._list, value)

    def rank(self, value):
        """Alias of rank_right."""
        return self.rank_right(value)

    def iter_slice(self, start=None, stop=None, reverse=False):
        """Iterator over positions [start, stop)."""
        if start is None:
            start = 0
        if stop is None:
            stop = len(self._list)
        positions = range(start, stop)
        if reverse:
            positions = reversed(positions)
        for pos in positions:
            yield self._list[pos]

    def iter_range(self, minimum=None, maximum=None, inclusive=(True, True),
                   reverse=False):
        """Iterator over values within the bounds (see SPEC)."""
        if minimum is None:
            start = 0
        elif inclusive[0]:
            start = bisect_left(self._list, minimum)
        else:
            start = bisect_right(self._list, minimum)
        if maximum is None:
            stop = len(self._list)
        elif inclusive[1]:
            stop = bisect_right(self._list, maximum)
        else:
            stop = bisect_left(self._list, maximum)
        positions = range(start, stop)
        if reverse:
            positions = reversed(positions)
        for pos in positions:
            yield self._list[pos]

    # -- sequence protocol -------------------------------------------

    def __len__(self):
        return len(self._list)

    def __iter__(self):
        return iter(self._list)

    def __reversed__(self):
        return reversed(self._list)

    def __contains__(self, value):
        pos = bisect_left(self._list, value)
        return pos < len(self._list) and self._list[pos] == value

    def __getitem__(self, index):
        if isinstance(index, slice):
            return self._list[index]
        return self._list[index]

    def __delitem__(self, index):
        del self._list[index]

    def __setitem__(self, index, value):
        raise NotImplementedError(
            "use ``del ol[index]`` and ``ol.insert(value)`` instead"
        )

    # -- comparisons (sequence rules; non-Sequence -> NotImplemented)

    def _compare(self, other, op):
        if not isinstance(other, Sequence):
            return NotImplemented
        return op(list(self._list), list(other))

    def __eq__(self, other):
        result = self._compare(other, lambda a, b: a == b)
        return result if result is not NotImplemented else NotImplemented

    def __ne__(self, other):
        result = self._compare(other, lambda a, b: a != b)
        return result if result is not NotImplemented else NotImplemented

    def __lt__(self, other):
        return self._compare(other, lambda a, b: a < b)

    def __le__(self, other):
        return self._compare(other, lambda a, b: a <= b)

    def __gt__(self, other):
        return self._compare(other, lambda a, b: a > b)

    def __ge__(self, other):
        return self._compare(other, lambda a, b: a >= b)

    # -- concatenation / repetition ----------------------------------

    def __add__(self, other):
        return self.__class__(chain(self._list, other))

    __radd__ = __add__

    def __iadd__(self, other):
        self.extend(other)
        return self

    def __mul__(self, num):
        return self.__class__(list(self._list) * num)

    __rmul__ = __mul__

    def __imul__(self, num):
        values = list(self._list) * num
        self.clear()
        self.extend(values)
        return self

    def __repr__(self):
        return f"{type(self).__name__}({list(self._list)!r})"
