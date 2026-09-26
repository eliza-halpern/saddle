"""Rule D: the differential verdict on a tree's answers (S1-a final, 2026-09-25).

`consensus_verdict` decides by authority, then by band, then by question:

1. an input's expected answer comes from the first authority that holds it
   (user override, spec panel, "either", adopted convention, references pin;
   contract A0), and disagreeing with an authority refuses at any d (A2');
2. on inputs no authority decides, the usable references decide only when
   they are unanimous; a disagreement there is a band disagreement, and the
   tree is refused only through MEASURE §M1-G's band, d / u >= F_REFUSE
   (contract B). Below the band the verdict is `question`;
3. a split input (comparable reference answers that differ) is never
   refused: it is asked, one `Question` per class id (A3').

`baseline_verdict` is the draft's A5 form and takes no authorities.

The functions are pure: no I/O, no state, and nothing from `evidence` at
runtime (layering `dag -> gates -> evidence -> runner -> slice`; this
module sits beside `gates`). Every label and class id is handed in as data;
the rule follows the label it is given and computes none (pair 15).
Docstrings cite MEASURE §M1-D, §M1-G and the Rule D calibration report's
per-input rule table (S1a-final.md).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Literal

MIN_VALID_REFERENCES: Final = 8
"""MEASURE §M1-C/§M1-E stop rule: fewer usable references read nothing."""

MIN_UNANIMOUS_SHARE: Final = 0.40
"""MEASURE Case 3; DECISIONS-2026-09-25 row 1 (an inference from the 0.45 and 0.84 points)."""

F_REFUSE: Final = 0.05
"""MEASURE §M1-G band, frozen before G-b (phase1/tools/m1g_apply_d.F_REFUSE); inclusive."""

AnswerKind = Literal["value", "raise", "opaque"]
UnusableReason = Literal["missing", "import-error", "arity", "timeout", "crashed"]
Source = Literal[
    "user",
    "spec",
    "either",
    "convention",
    "reference-pin",
    "references",
    "split",
    "incomparable",
    "unreferenced",
]
AuthoritySource = Literal["user", "spec", "convention", "reference-pin"]
DisagreementSource = Literal[
    "user", "spec", "convention", "reference-pin", "references", "baseline"
]
Verdict = Literal["refuse", "question", "route", "accept", "no-reference", "not-applicable"]
Mode = Literal["consensus", "baseline"]


@dataclass(frozen=True)
class Answer:
    """One recorded answer: a JSON value, an exception type, or `opaque` (uncomparable)."""

    kind: AnswerKind
    text: str

    def same(self, other: Answer) -> bool:
        """Comparable and equal. `opaque` is never the same as anything, itself included."""
        if self.kind == "opaque" or other.kind == "opaque":
            return False
        return (self.kind, self.text) == (other.kind, other.text)


@dataclass(frozen=True)
class Unusable:
    reason: UnusableReason
    detail: str


Answers = Mapping[str, Answer] | Unusable


@dataclass(frozen=True)
class Label:
    answer: Answer
    cite: str
    overrides: str = ""
    """User labels only: the cite of the spec/convention clause the user overrode (row 2)."""


@dataclass(frozen=True)
class Either:
    """An "either is fine" answer (contract E; addendum item 3)."""

    cite: str
    overrides: str = ""
    anchor: bool = False


@dataclass(frozen=True)
class Authorities:
    """Per-input labels, one slot per source. The answer book (S3-4) builds these."""

    user: Mapping[str, Label] = field(default_factory=dict)
    spec: Mapping[str, Label] = field(default_factory=dict)
    spec_id: str = ""
    either: Mapping[str, Either] = field(default_factory=dict)
    convention: Mapping[str, Label] = field(default_factory=dict)
    convention_ids: tuple[str, ...] = ()
    reference_pins: Mapping[str, Label] = field(default_factory=dict)


@dataclass(frozen=True)
class Provenance:
    input: str
    source: Source
    cite: str
    expected: Answer | None


@dataclass(frozen=True)
class Disagreement:
    input: str
    got: Answer
    expected: Answer
    source: DisagreementSource
    cite: str
    support: int


@dataclass(frozen=True)
class ReferenceDefect:
    input: str
    reference: str
    got: Answer
    expected: Answer
    source: Literal["spec", "convention"]
    cite: str


@dataclass(frozen=True)
class Question:
    cls: str
    inputs: tuple[str, ...]
    signatures: tuple[tuple[Answer, ...], ...]
    got: tuple[Answer, ...]


@dataclass(frozen=True)
class BandQuestion:
    cls: str
    inputs: tuple[str, ...]
    expected: tuple[Answer, ...]
    got: tuple[Answer, ...]


@dataclass(frozen=True)
class DifferentialResult:
    verdict: Verdict
    mode: Mode
    function: str
    checked: int
    unanimous: int
    pinned: int
    decided: int
    incomparable: int
    disagreements: tuple[Disagreement, ...]
    d: int
    band: tuple[BandQuestion, ...]
    unspecified: tuple[str, ...]
    questions: tuple[Question, ...]
    asks: tuple[str, ...]
    either: tuple[str, ...]
    provenance: tuple[Provenance, ...]
    reference_defects: tuple[ReferenceDefect, ...]
    spec_id: str
    convention_ids: tuple[str, ...]
    tree_unusable: Unusable | None
    references: int
    unusable_references: tuple[tuple[str, Unusable], ...]
    reason: str


def _check_label(s: str, lab: Label, where: str) -> None:
    if lab.answer.kind == "opaque":
        msg = f"{where} label for {s!r} is opaque"
        raise ValueError(msg)
    if not lab.cite:
        msg = f"{where} label for {s!r} has no cite"
        raise ValueError(msg)


def _authority(
    s: str, a: Authorities
) -> tuple[AuthoritySource | Literal["either"] | None, Label | None, str]:
    """Contract A0: user (as a named override only) > spec > either > convention > pin."""
    if s in a.user:
        _check_label(s, a.user[s], "user")
        for where, tab in (("spec", a.spec), ("convention", a.convention)):
            if s in tab and a.user[s].overrides != tab[s].cite:
                msg = (
                    f"user label for {s!r} meets {where} clause {tab[s].cite!r} "
                    "without naming it in overrides"
                )
                raise ValueError(msg)
        return "user", a.user[s], a.user[s].cite
    if s in a.spec:
        _check_label(s, a.spec[s], "spec")
        return "spec", a.spec[s], a.spec[s].cite
    if s in a.either:
        e = a.either[s]
        if not e.cite:
            msg = f"either answer for {s!r} has no cite"
            raise ValueError(msg)
        if s in a.convention and not (e.anchor or e.overrides == a.convention[s].cite):
            msg = (
                f"either answer for {s!r} meets convention clause {a.convention[s].cite!r} "
                "without a named override or anchor scope"
            )
            raise ValueError(msg)
        return "either", None, e.cite
    if s in a.convention:
        _check_label(s, a.convention[s], "convention")
        if not any(a.convention[s].cite.startswith(i + ":") for i in a.convention_ids):
            msg = f"convention label for {s!r} cites no adopted id in convention_ids"
            raise ValueError(msg)
        return "convention", a.convention[s], a.convention[s].cite
    if s in a.reference_pins:
        _check_label(s, a.reference_pins[s], "reference-pin")
        if not a.reference_pins[s].cite.startswith("references-pin:"):
            msg = f"reference pin for {s!r} is not cited references-pin:"
            raise ValueError(msg)
        return "reference-pin", a.reference_pins[s], a.reference_pins[s].cite
    return None, None, ""


def _check_authorities(a: Authorities) -> None:
    if a.spec and not a.spec_id:
        msg = "spec labels given without spec_id"
        raise ValueError(msg)
    if a.convention and not a.convention_ids:
        msg = "convention labels given without convention_ids"
        raise ValueError(msg)
    if len(set(a.convention_ids)) != len(a.convention_ids):
        msg = "convention_ids repeats an id"
        raise ValueError(msg)
    for s in a.reference_pins:
        if s in a.user or s in a.spec or s in a.either or s in a.convention:
            msg = f"reference pin for {s!r} shares an input with another slot (S3-4 O: one slot)"
            raise ValueError(msg)


def _split_references(
    references: Sequence[tuple[str, Answers]],
) -> tuple[list[tuple[str, Mapping[str, Answer]]], tuple[tuple[str, Unusable], ...]]:
    usable = [(n, t) for n, t in references if not isinstance(t, Unusable)]
    unusable = tuple((n, t) for n, t in references if isinstance(t, Unusable))
    return usable, unusable


def consensus_verdict(
    function: str,
    inputs: Sequence[str],
    tree: Answers,
    references: Sequence[tuple[str, Answers]],
    *,
    authorities: Authorities | None = None,
    classes: Mapping[str, str],
) -> DifferentialResult:
    """The consensus form (MEASURE §M1-D; §M1-G band; S1a-final A0-A6, B, E, P).

    Order: check the authority tables (P); dedupe `inputs` (A6); split the
    references; find each checked input's source (A0) and compare (A2', B,
    A3'); return `not-applicable` when every input is "either" (E); apply the
    floor (A4'); then refuse, `no-reference` by share, `question`, `route`,
    `accept`, in that order.
    """
    a = authorities if authorities is not None else Authorities()
    _check_authorities(a)
    checked = list(dict.fromkeys(inputs))
    usable, unusable_refs = _split_references(references)
    tree_table: Mapping[str, Answer] | None = None if isinstance(tree, Unusable) else tree
    tree_bad: Unusable | None = tree if isinstance(tree, Unusable) else None
    enough = len(usable) >= MIN_VALID_REFERENCES

    def empty(
        verdict: Verdict,
        reason: str,
        prov: tuple[Provenance, ...] = (),
        either: tuple[str, ...] = (),
        dis: tuple[Disagreement, ...] = (),
        tree_unusable: Unusable | None = None,
        pinned: int = 0,
        decided: int = 0,
    ) -> DifferentialResult:
        return DifferentialResult(
            verdict=verdict,
            mode="consensus",
            function=function,
            checked=len(checked),
            unanimous=0,
            pinned=pinned,
            decided=decided,
            incomparable=0,
            disagreements=dis,
            d=0,
            band=(),
            unspecified=(),
            questions=(),
            asks=(),
            either=either,
            provenance=prov,
            reference_defects=(),
            spec_id=a.spec_id,
            convention_ids=a.convention_ids,
            tree_unusable=tree_unusable,
            references=len(usable),
            unusable_references=unusable_refs,
            reason=reason,
        )

    if not checked:
        return empty("not-applicable", "no inputs")
    for s in checked:
        if tree_table is not None and s not in tree_table:
            msg = f"tree has no answer for {s!r}"
            raise ValueError(msg)
        if enough:
            for n, t in usable:
                if s not in t:
                    msg = f"reference {n} has no answer for {s!r}"
                    raise ValueError(msg)

    prov: list[Provenance] = []
    dis: list[Disagreement] = []
    defects: list[ReferenceDefect] = []
    either: list[str] = []
    band_in: list[str] = []
    split: list[str] = []
    unanimous = pinned = decided = incomparable = 0
    for s in checked:
        src, lab, cite = _authority(s, a)
        if src == "either":
            prov.append(Provenance(s, "either", cite, None))
            either.append(s)
            continue
        ra = [t[s] for _, t in usable] if enough else []
        comparable = enough and all(x.kind != "opaque" for x in ra)
        unan = comparable and all(x.same(ra[0]) for x in ra)
        if unan:
            unanimous += 1
        if src is not None:
            assert lab is not None
            pinned += src == "user"
            decided += src in ("spec", "convention")
            prov.append(Provenance(s, src, cite, lab.answer))
            if tree_table is not None and not tree_table[s].same(lab.answer):
                dis.append(Disagreement(s, tree_table[s], lab.answer, src, cite, 0))
            if src in ("spec", "convention") and enough:
                defects += [
                    ReferenceDefect(s, n, t[s], lab.answer, src, cite)
                    for n, t in usable
                    if not t[s].same(lab.answer)
                ]
            continue
        if not enough:
            prov.append(Provenance(s, "unreferenced", "", None))
            continue
        if s not in classes:
            msg = f"no class for spec-silent input {s!r}"
            raise ValueError(msg)
        if unan:
            prov.append(Provenance(s, "references", "", ra[0]))
            if tree_table is not None and not tree_table[s].same(ra[0]):
                dis.append(Disagreement(s, tree_table[s], ra[0], "references", "", len(usable)))
                band_in.append(s)
        elif comparable:
            prov.append(Provenance(s, "split", "", None))
            split.append(s)
        else:
            prov.append(Provenance(s, "incomparable", "", None))
            incomparable += 1

    authority_dis = tuple(x for x in dis if x.source != "references")
    if not [p for p in prov if p.source != "either"]:
        return empty("not-applicable", "every input is answered either", tuple(prov), tuple(either))
    if not enough:
        has_authority = any(
            p.source in ("user", "spec", "convention", "reference-pin") for p in prov
        )
        if has_authority and (tree_bad is not None or authority_dis):
            return empty(
                "refuse",
                "an authority-decided input is not honoured",
                tuple(prov),
                tuple(either),
                authority_dis,
                tree_bad,
                pinned,
                decided,
            )
        return empty(
            "no-reference",
            f"{len(usable)} usable references; {MIN_VALID_REFERENCES} needed",
            tuple(prov),
            tuple(either),
            (),
            None,
            pinned,
            decided,
        )

    order = {s: i for i, s in enumerate(checked)}
    band_groups: dict[str, list[str]] = {}
    for s in band_in:
        band_groups.setdefault(classes[s], []).append(s)
    expected_of = {p.input: p.expected for p in prov}
    band = tuple(
        BandQuestion(
            k,
            tuple(m),
            tuple(_expected(expected_of[x]) for x in m),
            tuple(_got(tree_table, x) for x in m),
        )
        for k, m in band_groups.items()
    )
    q_groups: dict[str, list[str]] = {}
    for s in split:
        q_groups.setdefault(classes[s], []).append(s)
    questions = []
    for k, m in q_groups.items():
        sigs = tuple(dict.fromkeys(tuple(t[x] for _, t in usable) for x in m))
        got = tuple(tree_table[x] for x in m) if tree_table is not None else ()
        questions.append(Question(k, tuple(m), sigs, got))
    firsts: dict[str, int] = {}
    for k, m in list(band_groups.items()) + list(q_groups.items()):
        firsts[k] = min(firsts.get(k, len(order)), min(order[x] for x in m))
    asks = tuple(sorted(firsts, key=lambda k: firsts[k]))
    d = len(band_in)
    live = len(checked) - len(either)
    verdict: Verdict
    if tree_bad is not None or authority_dis or (d > 0 and d / unanimous >= F_REFUSE):
        verdict = "refuse"
        if tree_bad is not None:
            reason = "tree unusable"
        elif authority_dis:
            reason = "an authority-decided input is not honoured"
        else:
            reason = f"band: d/u = {d}/{unanimous} >= {F_REFUSE}"
    elif unanimous < MIN_UNANIMOUS_SHARE * live:
        verdict, reason = (
            "no-reference",
            f"unanimous share {unanimous}/{live} < {MIN_UNANIMOUS_SHARE}",
        )
    elif band:
        verdict, reason = "question", ""
    elif questions:
        verdict, reason = "route", ""
    else:
        verdict, reason = "accept", ""
    return DifferentialResult(
        verdict=verdict,
        mode="consensus",
        function=function,
        checked=len(checked),
        unanimous=unanimous,
        pinned=pinned,
        decided=decided,
        incomparable=incomparable,
        disagreements=tuple(dis),
        d=d,
        band=band,
        unspecified=tuple(split),
        questions=tuple(questions),
        asks=asks,
        either=tuple(either),
        provenance=tuple(prov),
        reference_defects=tuple(defects),
        spec_id=a.spec_id,
        convention_ids=a.convention_ids,
        tree_unusable=tree_bad,
        references=len(usable),
        unusable_references=unusable_refs,
        reason=reason,
    )


def _expected(x: Answer | None) -> Answer:
    assert x is not None
    return x


def _got(tree_table: Mapping[str, Answer] | None, x: str) -> Answer:
    assert tree_table is not None
    return tree_table[x]


def baseline_verdict(
    function: str, inputs: Sequence[str], tree: Answers, baseline: Answers
) -> DifferentialResult:
    """The baseline form (draft contract A5, unchanged by the revision).

    A baseline whose function is `missing` or fails `arity` gives
    `not-applicable`; `import-error`, `timeout` or `crashed` gives
    `no-reference`; `tree_unusable` stays `None` in both. Otherwise the tree
    is refused when unusable, and on every input whose answer is not the
    same as the baseline's (`source="baseline"`, `support=1`). Both sides
    `opaque` is incomparable; every input incomparable is `not-applicable`.
    Never raises on an unusable side, sets no questions, `references == 0`.
    """
    checked = list(dict.fromkeys(inputs))

    def result(
        verdict: Verdict,
        reason: str,
        *,
        dis: tuple[Disagreement, ...] = (),
        prov: tuple[Provenance, ...] = (),
        incomparable: int = 0,
        tree_unusable: Unusable | None = None,
    ) -> DifferentialResult:
        return DifferentialResult(
            verdict=verdict,
            mode="baseline",
            function=function,
            checked=len(checked),
            unanimous=0,
            pinned=0,
            decided=0,
            incomparable=incomparable,
            disagreements=dis,
            d=0,
            band=(),
            unspecified=(),
            questions=(),
            asks=(),
            either=(),
            provenance=prov,
            reference_defects=(),
            spec_id="",
            convention_ids=(),
            tree_unusable=tree_unusable,
            references=0,
            unusable_references=(),
            reason=reason,
        )

    if not checked:
        return result("not-applicable", "no inputs")
    if isinstance(baseline, Unusable):
        if baseline.reason in ("missing", "arity"):
            return result("not-applicable", f"baseline {baseline.reason}: {baseline.detail}")
        return result("no-reference", f"baseline {baseline.reason}: {baseline.detail}")
    for s in checked:
        if s not in baseline:
            msg = f"baseline has no answer for {s!r}"
            raise ValueError(msg)
    if isinstance(tree, Unusable):
        return result("refuse", "tree unusable", tree_unusable=tree)
    for s in checked:
        if s not in tree:
            msg = f"tree has no answer for {s!r}"
            raise ValueError(msg)
    dis: list[Disagreement] = []
    prov: list[Provenance] = []
    incomparable = 0
    for s in checked:
        got, want = tree[s], baseline[s]
        if got.kind == "opaque" and want.kind == "opaque":
            prov.append(Provenance(s, "incomparable", "", None))
            incomparable += 1
            continue
        prov.append(Provenance(s, "references", "", want))
        if not got.same(want):
            dis.append(Disagreement(s, got, want, "baseline", "", 1))
    if incomparable == len(checked):
        return result(
            "not-applicable",
            "every input is opaque on both sides",
            prov=tuple(prov),
            incomparable=incomparable,
        )
    if dis:
        return result("refuse", "", dis=tuple(dis), prov=tuple(prov), incomparable=incomparable)
    return result("accept", "", prov=tuple(prov), incomparable=incomparable)
