# Derived from grantjenks/python-sortedcontainers@3ac3586 tests, Apache-2.0,
# adapted for T7 per DESIGN.md: NEW tests for the SPEC's perturbed/new
# behaviors P1-P3 plus __setitem__ (no upstream counterpart).
import pytest

from orderedlist import OrderedList


def test_insert_returns_index_empty():
    slt = OrderedList()
    assert slt.insert(5) == 0
    assert list(slt) == [5]


def test_insert_returns_index_middle():
    slt = OrderedList([1, 3, 5])
    assert slt.insert(4) == 2
    assert list(slt) == [1, 3, 4, 5]


def test_insert_returns_leftmost_index_duplicates():
    slt = OrderedList([1, 2, 2, 2, 3])
    assert slt.insert(2) == 1
    assert list(slt) == [1, 2, 2, 2, 2, 3]


def test_remove_returns_count_and_removes_all():
    slt = OrderedList([1, 2, 2, 2, 3, 3, 5])
    assert slt.remove(2) == 3
    assert list(slt) == [1, 3, 3, 5]
    assert 2 not in slt


def test_remove_missing_raises():
    slt = OrderedList([1, 2, 3])
    with pytest.raises(ValueError):
        slt.remove(99)
    slt = OrderedList()
    with pytest.raises(ValueError):
        slt.remove(1)


def test_discard_returns_bool():
    slt = OrderedList([1, 2, 2, 3])
    assert slt.discard(2) is True
    assert list(slt) == [1, 2, 3]
    assert slt.discard(99) is False
    assert slt.discard(2) is True
    assert list(slt) == [1, 3]
    assert slt.discard(2) is False


def test_setitem_raises():
    slt = OrderedList([1, 2, 3])
    with pytest.raises(NotImplementedError):
        slt[0] = 9
    with pytest.raises(NotImplementedError):
        slt[0:2] = [7, 8]
