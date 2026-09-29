"""The prompt templates of P1's three code-blind passes (P-a, P-b, P-c).

Plain strings, hashed into every `task-requirements.json`
(`task_requirements.build_hashes`): a changed template can never reuse a
sealed extraction. Each pass sees the task text, its units and (under D-2)
the baseline's public signatures, never the tree under audit and never a
test file.
"""

from __future__ import annotations

from typing import Final

SNIPPET_RULES: Final = (
    "Each input is Python in a small fixed language: `setup` is a list of statements, "
    "each one `from <module> import <name>`, an assignment to a plain name (`x = ...`), "
    "an augmented assignment to a plain name (`x *= 2`), or an expression; `call` is ONE "
    "expression whose value, or the exception it raises, is the behaviour under test. "
    "Use only names, attributes, calls, literals, list/tuple/dict/set displays, "
    "subscripts, slices and operators. Never use lambda, comprehensions, `import x`, "
    "dunder attributes, open, eval, exec or getattr. Make the call return a plain value "
    "(wrap a container object in `list(...)` or similar) so that it can be compared."
)

_SOURCES: Final = (
    "TASK TEXT\n{task}\n\n"
    "UNITS (every sentence or list item of the task text, with an id)\n{units}\n\n"
    "PUBLIC NAMES OF THE REPOSITORY BEFORE THE TASK "
    "(signatures; a docstring the task text refers to is hidden)\n{baseline}\n\n"
)

PROPOSE: Final = (
    "You write inputs for checking code against a task text. You cannot see any code: "
    "work from the words below only.\n\n"
    + _SOURCES
    + "For every unit, either choose 1 to {max_inputs} inputs that sit on a boundary the "
    "unit's words draw (an edge, a duplicate, an empty case, the value just past a "
    "limit), or mark the unit not executable with exactly one reason from: {reasons}.\n\n"
    "{snippet_rules}\n\n"
    "`args` lists, as Python literals, the input values a small stand-alone function "
    "computing the unit's behaviour would take (for `b = Box([2, 1]); b *= 2` they are "
    "the list and the number). Write no expected outcomes.\n\n"
    "Reply with JSON only, in this shape:\n"
    '{{"inputs": [{{"units": ["S-001"], "setup": ["from pkg import Thing", '
    '"t = Thing([2, 1])"], "call": "list(t)", "args": ["[2, 1]"]}}], '
    '"not_executable": [{{"unit": "S-002", "reason": "prose-only"}}]}}\n'
)

PREDICT: Final = (
    "You predict what correct code does, from a task text alone. You cannot see any "
    "code.\n\n" + _SOURCES + "INPUTS\n{inputs}\n\n"
    "For every input, give the outcome correct code must produce: "
    '`{{"kind": "value", "text": "<Python literal>"}}`, '
    '`{{"kind": "raises", "text": "<exception class name>"}}`, or '
    '`{{"kind": "true", "text": ""}}` when the call is a comparison that must hold. '
    "Give a one-line derivation, and `decides`: the exact words, copied from one of the "
    "input's units, that decide the outcome.\n\n"
    "Then, for every unit that has inputs, write one small Python function from the "
    "unit's words only: `def ref(...)` taking the input's `args` in order and returning "
    "the expected value (or raising the expected exception). It may import only "
    "{modules}; nothing else, and no files, no eval, no dunders. If you cannot write "
    "one, omit it.\n\n"
    "Reply with JSON only, in this shape:\n"
    '{{"predictions": [{{"input": "I-001", "outcome": {{"kind": "value", '
    '"text": "[2, 4]"}}, "derivation": "...", "decides": "..."}}], '
    '"references": [{{"unit": "S-001", '
    '"source": "def ref(xs, n):\\n    return [x * n for x in xs]\\n"}}]}}\n'
)

ALTERNATIVES: Final = (
    "Other readers may read a task text differently. You cannot see any code.\n\n"
    "TASK TEXT\n{task}\n\nUNITS\n{units}\n\n"
    "INPUTS, each with the outcome other readers agreed on\n{decided}\n\n"
    "For every input, if the cited words allow another reasonable reading, give that "
    "reading's outcome (same shape: value, raises or true) and quote the words that "
    "allow it. List nothing for an input whose words allow only one reading.\n\n"
    "Reply with JSON only, in this shape:\n"
    '{{"alternatives": [{{"input": "I-001", "outcome": {{"kind": "value", '
    '"text": "[2]"}}, "words": "the first n items"}}]}}\n'
)

TEMPLATES: Final = (SNIPPET_RULES, PROPOSE, PREDICT, ALTERNATIVES)
