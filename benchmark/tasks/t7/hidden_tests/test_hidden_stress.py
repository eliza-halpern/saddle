# Derived from grantjenks/python-sortedcontainers@3ac3586 tests
# (tests/test_stress_sortedlist.py), Apache-2.0, adapted for T7 per DESIGN.md:
# SortedList -> OrderedList reskin; every _-private use replaced with public
# API (len(slt); clear + extend a 1000-prefix for shortening); seed 0 kept.
import bisect
import random
from orderedlist import OrderedList
from functools import wraps


random.seed(0)
actions = []


def frange(start, stop, step):
    while start < stop:
        yield start
        start += step


class actor:
    def __init__(self, count):
        self._count = count

    def __call__(self, func):
        actions.extend([func] * self._count)
        return func


def not_empty(func):
    @wraps(func)
    def wrapper(slt):
        if len(slt) < 100:
            stress_extend(slt)
        func(slt)

    return wrapper


@actor(1)
def stress_clear(slt):
    if random.randrange(100) < 10:
        slt.clear()
    else:
        values = list(slt)
        slt.clear()
        slt.extend(values[: int(len(values) / 2)])


@actor(1)
def stress_insert(slt):
    if random.randrange(100) < 10:
        slt.clear()
    slt.insert(random.random())


@actor(1)
def stress_extend(slt):
    slt.extend(random.random() for rpt in range(350))


@actor(1)
@not_empty
def stress_contains(slt):
    if random.randrange(100) < 10:
        slt.clear()
        assert 0 not in slt
    else:
        val = slt[random.randrange(len(slt))]
        assert val in slt
        assert 1 not in slt


@actor(1)
@not_empty
def stress_discard(slt):
    val = slt[random.randrange(len(slt))]
    slt.discard(val)


@actor(1)
def stress_discard2(slt):
    if random.randrange(100) < 10:
        slt.clear()
    slt.discard(random.random())


@actor(1)
def stress_remove(slt):
    if len(slt) > 0:
        val = slt[random.randrange(len(slt))]
        slt.remove(val)

    try:
        slt.remove(1)
        assert False
    except ValueError:
        pass

    try:
        slt.remove(-1)
        assert False
    except ValueError:
        pass


@actor(1)
@not_empty
def stress_delitem(slt):
    del slt[random.randrange(len(slt))]


@actor(1)
def stress_getitem(slt):
    if len(slt) > 0:
        pos = random.randrange(len(slt))
        assert slt[pos] == list(slt)[pos]

        try:
            slt[-(len(slt) + 5)]
            assert False
        except IndexError:
            pass

        try:
            slt[len(slt) + 5]
            assert False
        except IndexError:
            pass
    else:
        try:
            slt[0]
            assert False
        except IndexError:
            pass


@actor(1)
@not_empty
def stress_delitem_slice(slt):
    start, stop = sorted(random.randrange(len(slt)) for rpt in range(2))
    step = random.choice([-3, -2, -1, 1, 1, 1, 1, 1, 2, 3])
    del slt[start:stop:step]


@actor(1)
def stress_iter(slt):
    itr1 = iter(slt)
    itr2 = (slt[pos] for pos in range(len(slt)))
    assert all(tup[0] == tup[1] for tup in zip(itr1, itr2))


@actor(1)
def stress_reversed(slt):
    itr = reversed(list(reversed(slt)))
    assert all(tup[0] == tup[1] for tup in zip(slt, itr))


@actor(1)
def stress_iter_slice(slt):
    if len(slt) < 10:
        return
    start = random.randrange(len(slt) - 5)
    stop = random.randrange(start, len(slt))
    itr = slt.iter_slice(start, stop)
    assert all(slt[pos] == next(itr) for pos in range(start, stop))


@actor(1)
def stress_iter_range(slt):
    values = sorted(set(slt))
    slt.clear()
    slt.extend(values)
    if len(slt) < 10:
        return
    start = random.randrange(len(slt) - 5)
    stop = random.randrange(start, len(slt))
    itr = slt.iter_range(slt[start], slt[stop], inclusive=(True, False))
    assert all(slt[pos] == next(itr) for pos in range(start, stop))


@actor(1)
def stress_rank_left(slt):
    values = list(slt)
    value = random.random()
    values.sort()
    assert bisect.bisect_left(values, value) == slt.rank_left(value)


@actor(1)
def stress_rank(slt):
    values = list(slt)
    value = random.random()
    values.sort()
    assert bisect.bisect(values, value) == slt.rank(value)


@actor(1)
def stress_rank_right(slt):
    values = list(slt)
    value = random.random()
    values.sort()
    assert bisect.bisect_right(values, value) == slt.rank_right(value)


@actor(1)
@not_empty
def stress_dups(slt):
    pos = min(random.randrange(len(slt)), 300)
    val = slt[pos]
    for rpt in range(pos):
        slt.insert(val)


@actor(1)
@not_empty
def stress_count(slt):
    values = list(slt)
    val = slt[random.randrange(len(slt))]
    assert slt.count(val) == values.count(val)


@actor(1)
@not_empty
def stress_pop(slt):
    pos = random.randrange(len(slt)) + 1
    assert slt[-pos] == slt.pop(-pos)


@actor(1)
@not_empty
def stress_index(slt):
    values = set(slt)
    slt.clear()
    slt.extend(values)
    pos = random.randrange(len(slt))
    assert slt.index(slt[pos]) == pos


@actor(1)
@not_empty
def stress_index2(slt):
    values = list(slt)[:3] * 200
    slt = OrderedList(values)
    for idx, val in enumerate(slt):
        assert slt.index(val, idx) == idx


@actor(1)
def stress_mul(slt):
    values = list(slt)
    mult = random.randrange(10)
    values *= mult
    values.sort()
    assert (slt * mult) == values


@actor(1)
def stress_imul(slt):
    mult = random.randrange(10)
    slt *= mult


@actor(1)
@not_empty
def stress_reversed(slt):
    itr = reversed(slt)
    pos = random.randrange(1, len(slt))
    for rpt in range(pos):
        val = next(itr)
    assert val == slt[-pos]


@actor(1)
@not_empty
def stress_eq(slt):
    values = []
    assert not (values == slt)


@actor(1)
@not_empty
def stress_lt(slt):
    values = list(slt)
    assert not (values < slt)
    values = OrderedList(value - 1 for value in values)
    assert values < slt
    values = []
    assert values < slt
    assert not (slt < values)


def test_stress(repeat=1000):
    slt = OrderedList(random.random() for rpt in range(1000))

    for rpt in range(repeat):
        action = random.choice(actions)
        action(slt)

        fourth = int(len(slt) / 4)
        count = 0 if fourth == 0 else random.randrange(-fourth, fourth)

        while count > 0:
            slt.insert(random.random())
            count -= 1

        while count < 0:
            pos = random.randrange(len(slt))
            del slt[pos]
            count += 1

        while len(slt) > 2000:
            # Shorten the ordered list using only the public API:
            # keep a 1000-element prefix.
            values = list(slt)[:1000]
            slt.clear()
            slt.extend(values)

    stress_extend(slt)

    while len(slt) > 0:
        pos = random.randrange(len(slt))
        del slt[pos]


if __name__ == '__main__':
    import sys
    from datetime import datetime

    start = datetime.now()

    print('Python', sys.version_info)

    try:
        num = int(sys.argv[1])
        print('Setting iterations to', num)
    except:
        print('Setting iterations to 1000 (default)')
        num = 1000

    try:
        pea = int(sys.argv[2])
        random.seed(pea)
        print('Setting seed to', pea)
    except:
        print('Setting seed to 0 (default)')
        random.seed(0)

    try:
        test_stress(num)
    except:
        raise
    finally:
        print('Exiting after', (datetime.now() - start))
