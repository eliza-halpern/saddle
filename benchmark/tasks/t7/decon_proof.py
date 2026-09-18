"""Decontamination proof: memorized upstream code cannot pass T7.

Shims the adapted T7 API names onto the REAL upstream SortedList and
runs the perturbation hidden tests. If contamination (memorized
sortedcontainers) were sufficient, this would pass. It must FAIL on
P1-P3 (only the shared setitem behavior passes).
"""

import subprocess
import sys

SHIM = """
import sys, types
sys.path.insert(0, '/tmp/sc-upstream/src')
from sortedcontainers import SortedList

class OrderedList(SortedList):
    insert = SortedList.add
    extend = SortedList.update
    rank_left = SortedList.bisect_left
    rank_right = SortedList.bisect_right
    rank = SortedList.bisect
    iter_slice = SortedList.islice
    iter_range = SortedList.irange

mod = types.ModuleType('orderedlist')
mod.OrderedList = OrderedList
sys.modules['orderedlist'] = mod
"""


if __name__ == '__main__':
    # Direct check: exec shim, run P-asserts.
    namespace: dict = {}
    exec(SHIM, namespace)
    OrderedList = namespace['mod'].OrderedList
    failures = 0

    got = OrderedList([1, 3, 5]).insert(4)
    print(f'P1 insert returns index: got {got!r} (want 2)')
    failures += got != 2

    shim = OrderedList([1, 2, 2, 2, 3])
    got = shim.remove(2)
    print(f'P2 remove returns count: got {got!r} (want 3), '
          f'remaining {list(shim)} (want [1, 3])')
    failures += (got != 3) or (list(shim) != [1, 3])

    shim = OrderedList([1, 2, 3])
    got = shim.discard(2)
    print(f'P3 discard returns bool: got {got!r} (want True)')
    failures += got is not True

    print(f'DECON_PROOF: {failures}/3 perturbed behaviors fail on '
          f'memorized upstream code')
    sys.exit(0 if failures == 3 else 1)
