# Derived from grantjenks/python-sortedcontainers@3ac3586 tests
# (tests/test_coverage_sortedlist.py), Apache-2.0, adapted for T7 per DESIGN.md:
# SortedList -> OrderedList reskin with perturbations P1-P3; every _-private
# use stripped; test_build_index, test_check, test_repr_recursion deleted.
import random
from orderedlist import OrderedList
from itertools import chain
import pytest


def test_init():
    slt = OrderedList()

    slt = OrderedList(range(10000))
    assert all(tup[0] == tup[1] for tup in zip(slt, range(10000)))

    slt.clear()
    assert len(slt) == 0


def test_insert():
    random.seed(0)
    slt = OrderedList()
    for val in range(1000):
        slt.insert(val)

    slt = OrderedList()
    for val in range(1000, 0, -1):
        slt.insert(val)

    slt = OrderedList()
    for val in range(1000):
        slt.insert(random.random())


def test_extend():
    slt = OrderedList()

    slt.extend(range(1000))
    assert len(slt) == 1000

    slt.extend(range(100))
    assert len(slt) == 1100

    slt.extend(range(10000))
    assert len(slt) == 11100

    values = sorted(chain(range(1000), range(100), range(10000)))
    assert all(tup[0] == tup[1] for tup in zip(slt, values))


def test_contains():
    slt = OrderedList()
    assert 0 not in slt

    slt.extend(range(10000))

    for val in range(10000):
        assert val in slt

    assert 10000 not in slt


def test_discard():
    slt = OrderedList()

    assert slt.discard(0) is False
    assert len(slt) == 0

    slt = OrderedList([1, 2, 2, 2, 3, 3, 5])

    slt.discard(6)
    slt.discard(4)
    slt.discard(2)

    assert all(tup[0] == tup[1] for tup in zip(slt, [1, 2, 2, 3, 3, 5]))


def test_remove():
    slt = OrderedList()

    assert slt.discard(0) is False
    assert len(slt) == 0

    slt = OrderedList([1, 2, 2, 2, 3, 3, 5])

    assert slt.remove(2) == 3

    assert all(tup[0] == tup[1] for tup in zip(slt, [1, 3, 3, 5]))


def test_remove_valueerror1():
    slt = OrderedList()
    with pytest.raises(ValueError):
        slt.remove(0)


def test_remove_valueerror2():
    slt = OrderedList(range(100))
    with pytest.raises(ValueError):
        slt.remove(100)


def test_remove_valueerror3():
    slt = OrderedList([1, 2, 2, 2, 3, 3, 5])
    with pytest.raises(ValueError):
        slt.remove(4)


def test_delete():
    slt = OrderedList(range(20))
    for val in range(20):
        slt.remove(val)
    assert len(slt) == 0


def test_getitem():
    random.seed(0)
    slt = OrderedList()

    lst = list()

    for rpt in range(100):
        val = random.random()
        slt.insert(val)
        lst.append(val)

    lst.sort()

    assert all(slt[idx] == lst[idx] for idx in range(100))
    assert all(slt[idx - 99] == lst[idx - 99] for idx in range(100))


def test_getitem_slice():
    random.seed(0)
    slt = OrderedList()

    lst = list()

    for rpt in range(100):
        val = random.random()
        slt.insert(val)
        lst.append(val)

    lst.sort()

    assert all(slt[start:] == lst[start:] for start in [-75, -25, 0, 25, 75])

    assert all(slt[:stop] == lst[:stop] for stop in [-75, -25, 0, 25, 75])

    assert all(slt[::step] == lst[::step] for step in [-5, -1, 1, 5])

    assert all(
        slt[start:stop] == lst[start:stop]
        for start in [-75, -25, 0, 25, 75]
        for stop in [-75, -25, 0, 25, 75]
    )

    assert all(
        slt[:stop:step] == lst[:stop:step]
        for stop in [-75, -25, 0, 25, 75]
        for step in [-5, -1, 1, 5]
    )

    assert all(
        slt[start::step] == lst[start::step]
        for start in [-75, -25, 0, 25, 75]
        for step in [-5, -1, 1, 5]
    )

    assert all(
        slt[start:stop:step] == lst[start:stop:step]
        for start in [-75, -25, 0, 25, 75]
        for stop in [-75, -25, 0, 25, 75]
        for step in [-5, -1, 1, 5]
    )


def test_getitem_slice_big():
    slt = OrderedList(range(4))
    lst = list(range(4))

    itr = (
        (start, stop, step)
        for start in [-6, -4, -2, 0, 2, 4, 6]
        for stop in [-6, -4, -2, 0, 2, 4, 6]
        for step in [-3, -2, -1, 1, 2, 3]
    )

    for start, stop, step in itr:
        assert slt[start:stop:step] == lst[start:stop:step]


def test_getitem_slicezero():
    slt = OrderedList(range(100))
    with pytest.raises(ValueError):
        slt[::0]


def test_getitem_indexerror1():
    slt = OrderedList()
    with pytest.raises(IndexError):
        slt[5]


def test_getitem_indexerror2():
    slt = OrderedList(range(100))
    with pytest.raises(IndexError):
        slt[200]


def test_getitem_indexerror3():
    slt = OrderedList(range(100))
    with pytest.raises(IndexError):
        slt[-101]


def test_delitem():
    random.seed(0)

    slt = OrderedList(range(100))
    while len(slt) > 0:
        pos = random.randrange(len(slt))
        del slt[pos]

    slt = OrderedList(range(100))
    del slt[:]
    assert len(slt) == 0


def test_delitem_slice():
    slt = OrderedList(range(100))
    del slt[10:40:1]
    del slt[10:40:-1]
    del slt[10:40:2]
    del slt[10:40:-2]


def test_iter():
    slt = OrderedList(range(10000))
    itr = iter(slt)
    assert all(tup[0] == tup[1] for tup in zip(range(10000), itr))


def test_reversed():
    slt = OrderedList(range(10000))
    rev = reversed(slt)
    assert all(tup[0] == tup[1] for tup in zip(range(9999, -1, -1), rev))


def test_reverse():
    slt = OrderedList(range(10000))
    with pytest.raises(NotImplementedError):
        slt.reverse()


def test_iter_slice():
    sl = OrderedList()

    assert [] == list(sl.iter_slice())

    values = list(range(53))
    sl.extend(values)

    for start in range(53):
        for stop in range(53):
            assert list(sl.iter_slice(start, stop)) == values[start:stop]

    for start in range(53):
        for stop in range(53):
            assert (
                list(sl.iter_slice(start, stop, reverse=True)) == values[start:stop][::-1]
            )

    for start in range(53):
        assert list(sl.iter_slice(start=start)) == values[start:]
        assert list(sl.iter_slice(start=start, reverse=True)) == values[start:][::-1]

    for stop in range(53):
        assert list(sl.iter_slice(stop=stop)) == values[:stop]
        assert list(sl.iter_slice(stop=stop, reverse=True)) == values[:stop][::-1]


def test_iter_range():
    sl = OrderedList()

    assert [] == list(sl.iter_range())

    values = list(range(53))
    sl.extend(values)

    for start in range(53):
        for end in range(start, 53):
            assert list(sl.iter_range(start, end)) == values[start : (end + 1)]
            assert (
                list(sl.iter_range(start, end, reverse=True))
                == values[start : (end + 1)][::-1]
            )

    for start in range(53):
        for end in range(start, 53):
            assert list(range(start, end)) == list(sl.iter_range(start, end, (True, False)))

    for start in range(53):
        for end in range(start, 53):
            assert list(range(start + 1, end + 1)) == list(
                sl.iter_range(start, end, (False, True))
            )

    for start in range(53):
        for end in range(start, 53):
            assert list(range(start + 1, end)) == list(
                sl.iter_range(start, end, (False, False))
            )

    for start in range(53):
        assert list(range(start, 53)) == list(sl.iter_range(start))

    for end in range(53):
        assert list(range(0, end)) == list(sl.iter_range(None, end, (True, False)))

    assert values == list(sl.iter_range(inclusive=(False, False)))

    assert [] == list(sl.iter_range(53))
    assert values == list(sl.iter_range(None, 53, (True, False)))


def test_len():
    slt = OrderedList()

    for val in range(10000):
        slt.insert(val)
        assert len(slt) == (val + 1)


def test_rank_left():
    slt = OrderedList()
    assert slt.rank_left(0) == 0
    slt = OrderedList(range(100))
    slt.extend(range(100))
    assert slt.rank_left(50) == 100
    assert slt.rank_left(200) == 200


def test_rank():
    slt = OrderedList()
    assert slt.rank(10) == 0
    slt = OrderedList(range(100))
    slt.extend(range(100))
    assert slt.rank(10) == 22
    assert slt.rank(200) == 200


def test_rank_right():
    slt = OrderedList()
    assert slt.rank_right(10) == 0
    slt = OrderedList(range(100))
    slt.extend(range(100))
    assert slt.rank_right(10) == 22
    assert slt.rank_right(200) == 200


def test_copy():
    alpha = OrderedList(range(100))
    beta = alpha.copy()
    alpha.insert(100)
    assert len(alpha) == 101
    assert len(beta) == 100


def test_copy_copy():
    import copy

    alpha = OrderedList(range(100))
    beta = copy.copy(alpha)
    alpha.insert(100)
    assert len(alpha) == 101
    assert len(beta) == 100


def test_count():
    slt = OrderedList()

    assert slt.count(0) == 0

    for iii in range(100):
        for jjj in range(iii):
            slt.insert(iii)

    for iii in range(100):
        assert slt.count(iii) == iii

    assert slt.count(100) == 0


def test_pop():
    slt = OrderedList(range(10))
    assert slt.pop() == 9
    assert slt.pop(0) == 0
    assert slt.pop(-2) == 7
    assert slt.pop(4) == 5


def test_pop_indexerror1():
    slt = OrderedList(range(10))
    with pytest.raises(IndexError):
        slt.pop(-11)


def test_pop_indexerror2():
    slt = OrderedList(range(10))
    with pytest.raises(IndexError):
        slt.pop(10)


def test_pop_indexerror3():
    slt = OrderedList()
    with pytest.raises(IndexError):
        slt.pop()


def test_index():
    slt = OrderedList(range(100))

    for val in range(100):
        assert val == slt.index(val)

    assert slt.index(99, 0, 1000) == 99

    slt = OrderedList(0 for rpt in range(100))

    for start in range(100):
        for stop in range(start, 100):
            assert slt.index(0, start, stop + 1) == start

    for start in range(100):
        assert slt.index(0, -(100 - start)) == start

    assert slt.index(0, -1000) == 0


def test_index_valueerror1():
    slt = OrderedList([0] * 10)
    with pytest.raises(ValueError):
        slt.index(0, 10)


def test_index_valueerror2():
    slt = OrderedList([0] * 10)
    with pytest.raises(ValueError):
        slt.index(0, 0, -10)


def test_index_valueerror3():
    slt = OrderedList([0] * 10)
    with pytest.raises(ValueError):
        slt.index(0, 7, 3)


def test_index_valueerror4():
    slt = OrderedList([0] * 10)
    with pytest.raises(ValueError):
        slt.index(1)


def test_index_valueerror5():
    slt = OrderedList()
    with pytest.raises(ValueError):
        slt.index(1)


def test_index_valueerror6():
    slt = OrderedList(range(10))
    with pytest.raises(ValueError):
        slt.index(3, 5)


def test_index_valueerror7():
    slt = OrderedList([0] * 10 + [2] * 10)
    with pytest.raises(ValueError):
        slt.index(1, 0, 10)


def test_mul():
    this = OrderedList(range(10))
    that = this * 5
    assert this == list(range(10))
    assert that == sorted(list(range(10)) * 5)
    assert this != that


def test_imul():
    this = OrderedList(range(10))
    this *= 5
    assert this == sorted(list(range(10)) * 5)


def test_op_add():
    this = OrderedList(range(10))
    assert (this + this + this) == (this * 3)

    that = OrderedList(range(10))
    that += that
    that += that
    assert that == (this * 4)


def test_eq():
    this = OrderedList(range(10))
    assert this == list(range(10))
    assert this == tuple(range(10))
    assert not (this == list(range(9)))


def test_ne():
    this = OrderedList(range(10))
    assert this != list(range(9))
    assert this != tuple(range(11))
    assert this != [0, 1, 2, 3, 3, 5, 6, 7, 8, 9]
    assert this != (val for val in range(10))
    assert this != set()


def test_lt():
    this = OrderedList(range(10, 15))
    assert this < [10, 11, 13, 13, 14]
    assert this < [10, 11, 12, 13, 14, 15]
    assert this < [11]


def test_le():
    this = OrderedList(range(10, 15))
    assert this <= [10, 11, 12, 13, 14]
    assert this <= [10, 11, 12, 13, 14, 15]
    assert this <= [10, 11, 13, 13, 14]
    assert this <= [11]


def test_gt():
    this = OrderedList(range(10, 15))
    assert this > [10, 11, 11, 13, 14]
    assert this > [10, 11, 12, 13]
    assert this > [9]


def test_ge():
    this = OrderedList(range(10, 15))
    assert this >= [10, 11, 12, 13, 14]
    assert this >= [10, 11, 12, 13]
    assert this >= [10, 11, 11, 13, 14]
    assert this >= [9]


def test_repr():
    this = OrderedList(range(10))
    assert repr(this) == 'OrderedList([0, 1, 2, 3, 4, 5, 6, 7, 8, 9])'


def test_repr_subclass():
    class CustomOrderedList(OrderedList):
        pass

    this = CustomOrderedList([1, 2, 3, 4])
    assert repr(this) == 'CustomOrderedList([1, 2, 3, 4])'


def test_pickle():
    import pickle

    alpha = OrderedList(range(10000))
    beta = pickle.loads(pickle.dumps(alpha))
    assert alpha == beta
