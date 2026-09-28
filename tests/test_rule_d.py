"""Rule D's known-good / known-bad pairs (the rule D spec's pairs 1-30).

Numbering follows the spec: `test_differential_p01_*` is pair 1. Pair 8 and
the baseline half of pair 9 are the draft's (`baseline_verdict`, contract
A5). Fixtures are synthetic unless marked "real vector": the real vectors
are the 12 frozen references' recorded answers in `W-a-1 ... W-c-4` order
(recorded answer signatures and a per-reference probe table).

Every pair except 6 sets `MIN_UNANIMOUS_SHARE` to 0.0 to isolate its own
contract; pair 6 uses the shipped 0.40.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

import pytest

from saddle import rule_d
from saddle.rule_d import (
    Answer,
    Authorities,
    BandQuestion,
    Disagreement,
    Either,
    Label,
    Provenance,
    Unusable,
    baseline_verdict,
    consensus_verdict,
)


def v(x: object) -> Answer:
    return Answer("value", json.dumps(x, sort_keys=True))


def r(exc: str) -> Answer:
    return Answer("raise", exc)


T, F = v(True), v(False)
REAL = [
    "W-a-1",
    "W-a-2",
    "W-a-3",
    "W-a-4",
    "W-b-1",
    "W-b-2",
    "W-b-3",
    "W-b-4",
    "W-c-1",
    "W-c-2",
    "W-c-3",
    "W-c-4",
]
# 'user@domain.com\n'  TTTFFTTTTTTT (10 of 12 accept)
NL_VEC = dict(zip(REAL, [T, T, T, F, F, T, T, T, T, T, T, T], strict=True))
# 'user@domain.c'      FFFTFFFFFFFF (W-a-4 alone accepts)
LONE_VEC = dict(zip(REAL, [F, F, F, T, F, F, F, F, F, F, F, F], strict=True))
# 'us}er@domain.com'   W-a-4 alone accepts
BRACE_VEC = dict(zip(REAL, [F, F, F, T, F, F, F, F, F, F, F, F], strict=True))

Refs = list[tuple[str, Mapping[str, Answer] | Unusable]]


def refs_const(n: int, table: Mapping[str, Answer]) -> Refs:
    return [(f"r{i + 1}", dict(table)) for i in range(n)]


def plain(n: int, start: int = 0) -> list[str]:
    return [f"p{i}@x.com" for i in range(start, start + n)]


SHARE = rule_d.MIN_UNANIMOUS_SHARE


@pytest.fixture(autouse=True)
def _share(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rule_d, "MIN_UNANIMOUS_SHARE", 0.0)


# ---- kept from the draft (A1-A4, A6, caller errors), re-read under the revision


def test_differential_p01_unchecked_input_cannot_refuse() -> None:
    refs = refs_const(8, {"a@x.com": T, "u+t@x.com": T})
    tree = {"a@x.com": T, "u+t@x.com": F}
    cls = {"a@x.com": "plain", "u+t@x.com": "plus"}
    res = consensus_verdict("m.f", ["a@x.com"], tree, refs, classes=cls)
    assert (res.verdict, res.disagreements, res.checked) == ("accept", (), 1)
    res = consensus_verdict("m.f", ["a@x.com", "u+t@x.com"], tree, refs, classes=cls)
    assert (res.verdict, res.d, res.unanimous) == ("refuse", 1, 2)  # band: 1/2 >= 0.05
    assert res.disagreements == (Disagreement("u+t@x.com", F, T, "references", "", 8),)


def test_differential_p02_majority_is_not_unanimity() -> None:
    vec = [F] * 11 + [T]
    refs: Refs = [(f"r{i}", {".u@x.com": a}) for i, a in enumerate(vec)]
    res = consensus_verdict(
        "m.f", [".u@x.com"], {".u@x.com": T}, refs, classes={".u@x.com": "dots"}
    )
    assert (res.verdict, res.unspecified, res.disagreements) == ("route", (".u@x.com",), ())
    assert [(q.cls, q.inputs, q.signatures) for q in res.questions] == [
        ("dots", (".u@x.com",), (tuple(vec),))
    ]


def test_differential_p03_questions_do_not_read_the_tree() -> None:
    vec = [F] * 11 + [T]
    refs: Refs = [(f"r{i}", {".u@x.com": a, "a@x.com": T}) for i, a in enumerate(vec)]
    cls = {".u@x.com": "dots", "a@x.com": "plain"}
    inputs = [".u@x.com", "a@x.com"]
    res_a = consensus_verdict("m.f", inputs, {".u@x.com": T, "a@x.com": T}, refs, classes=cls)
    res_b = consensus_verdict("m.f", inputs, {".u@x.com": F, "a@x.com": T}, refs, classes=cls)
    assert (res_a.verdict, res_b.verdict) == ("route", "route")
    assert (res_a.unspecified, res_b.unspecified) == ((".u@x.com",), (".u@x.com",))
    assert [(q.cls, q.inputs, q.signatures) for q in res_a.questions] == [
        (q.cls, q.inputs, q.signatures) for q in res_b.questions
    ]
    assert (res_a.questions[0].got, res_b.questions[0].got) == ((T,), (F,))


def test_differential_p04_user_pin_is_final_against_unanimous_references() -> None:
    s, q = "!u@x.com", "q@x.com"
    refs: Refs = [(f"r{i}", {s: F, q: (T if i < 4 else F)}) for i in range(8)]
    auth = Authorities(user={s: Label(T, "book#1"), q: Label(T, "book#2")})
    cls = {s: "bang", q: "q"}  # a pinned input needs no class; given so M-A4 dies by assertion
    res = consensus_verdict("m.f", [s, q], {s: T, q: T}, refs, authorities=auth, classes=cls)
    assert (res.verdict, res.pinned, res.unspecified) == ("accept", 2, ())
    res = consensus_verdict("m.f", [s, q], {s: F, q: T}, refs, authorities=auth, classes=cls)
    assert res.verdict == "refuse"
    assert res.disagreements == (Disagreement(s, F, T, "user", "book#1", 0),)


def test_differential_p05_seven_is_not_enough() -> None:
    s = "a@x.com"
    res = consensus_verdict("m.f", [s], {s: F}, refs_const(7, {s: T}), classes={s: "c"})
    assert (res.verdict, res.disagreements) == ("no-reference", ())
    assert "7" in res.reason
    assert "8" in res.reason
    res = consensus_verdict("m.f", [s], {s: F}, refs_const(8, {s: T}), classes={s: "c"})
    assert res.verdict == "refuse"
    refs = [*refs_const(7, {s: T}), ("r8", Unusable("timeout", ""))]
    res = consensus_verdict("m.f", [s], {s: F}, refs, classes={s: "c"})
    assert res.verdict == "no-reference"
    assert [n for n, _ in res.unusable_references] == ["r8"]


def test_differential_p06_share(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rule_d, "MIN_UNANIMOUS_SHARE", SHARE)  # the shipped value, 0.40
    assert SHARE == 0.40

    def case(n_unan: int, n_split: int, *, bad: bool = False, n_either: int = 0) -> str:
        uni, spl = plain(n_unan), [f"s{i}@x.com" for i in range(n_split)]
        refs: Refs = [
            (f"r{i}", {**dict.fromkeys(uni, T), **dict.fromkeys(spl, T if i < 4 else F)})
            for i in range(8)
        ]
        tree = dict.fromkeys(uni + spl, T)
        if bad:
            tree[uni[0]] = F
        auth = Authorities(either={x: Either("book#e") for x in spl[:n_either]})
        return consensus_verdict(
            "m.f", uni + spl, tree, refs, authorities=auth, classes=dict.fromkeys(uni + spl, "c")
        ).verdict

    assert case(3, 7) == "no-reference"  # 3 < 0.40 * 10
    assert case(4, 6) == "route"  # 4 >= 0.40 * 10: the boundary is inclusive
    assert case(3, 7, n_either=3) == "route"  # share over non-"either" inputs: 3 >= 0.40 * 7
    assert case(3, 7, bad=True) == "refuse"  # a refusal outranks Case 3


def test_differential_p07_unusable_tree_is_refused() -> None:
    s, u = "a@x.com", ".u@x.com"
    refs: Refs = [(f"r{i}", {s: T, u: (F if i else T)}) for i in range(8)]
    res = consensus_verdict(
        "m.f", [s, u], Unusable("missing", "AttributeError"), refs, classes={s: "p", u: "dots"}
    )
    assert (res.verdict, res.tree_unusable) == ("refuse", Unusable("missing", "AttributeError"))
    assert res.disagreements == ()
    assert [(q.inputs, q.got) for q in res.questions] == [((u,), ())]


def test_differential_p08_baseline_lacks_the_function() -> None:
    s = "a@x.com"
    res = baseline_verdict("m.f", [s], {s: T}, Unusable("missing", "AttributeError"))
    assert (res.verdict, res.tree_unusable, res.mode) == ("not-applicable", None, "baseline")
    res = baseline_verdict("m.f", [s], {s: T}, Unusable("arity", "2 != 1"))
    assert (res.verdict, res.tree_unusable) == ("not-applicable", None)
    for reason in ("timeout", "import-error", "crashed"):
        res = baseline_verdict("m.f", [s], {s: T}, Unusable(reason, ""))
        assert (res.verdict, res.tree_unusable, res.references) == ("no-reference", None, 0)
    res = baseline_verdict("m.f", [s], Unusable("missing", "AttributeError"), {s: T})
    assert (res.verdict, res.disagreements) == ("refuse", ())
    assert res.tree_unusable == Unusable("missing", "AttributeError")


def test_differential_p09_opaque_is_never_the_same() -> None:
    assert not Answer("opaque", "set").same(Answer("opaque", "set"))
    assert v(1).same(v(1))
    opaque = Answer("opaque", "set")
    res = consensus_verdict(
        "m.f", ["a"], {"a": opaque}, refs_const(8, {"a": opaque}), classes={"a": "a"}
    )
    assert (res.incomparable, res.unanimous) == (1, 0)
    # the baseline half (A5): exception types are answers; opaque on one side is a difference
    res = baseline_verdict("m.f", ["a"], {"a": r("TypeError")}, {"a": r("ValueError")})
    assert res.verdict == "refuse"
    assert res.disagreements == (
        Disagreement("a", r("TypeError"), r("ValueError"), "baseline", "", 1),
    )
    res = baseline_verdict("m.f", ["a"], {"a": r("ValueError")}, {"a": r("ValueError")})
    assert (res.verdict, res.disagreements, res.questions, res.unspecified) == (
        "accept",
        (),
        (),
        (),
    )
    res = baseline_verdict(
        "m.f", ["a", "b"], {"a": opaque, "b": opaque}, {"a": opaque, "b": opaque}
    )
    assert (res.verdict, res.incomparable) == ("not-applicable", 2)
    res = baseline_verdict("m.f", ["a", "b"], {"a": opaque, "b": T}, {"a": v([1, 2]), "b": T})
    assert (res.verdict, res.incomparable) == ("refuse", 0)
    assert res.disagreements == (Disagreement("a", opaque, v([1, 2]), "baseline", "", 1),)


def test_differential_p10_no_inputs() -> None:
    assert (
        consensus_verdict("m.f", [], {}, refs_const(8, {}), classes={}).verdict == "not-applicable"
    )
    res = consensus_verdict(
        "m.f", ["a", "a"], {"a": T}, refs_const(8, {"a": T}), classes={"a": "a"}
    )
    assert res.checked == 1
    assert baseline_verdict("m.f", [], {}, {}).verdict == "not-applicable"
    assert baseline_verdict("m.f", ["a", "a"], {"a": T}, {"a": T}).checked == 1


def test_differential_p11_caller_errors() -> None:
    refs = refs_const(8, {"a": T, "b": T})
    cls = {"a": "a", "b": "b"}
    with pytest.raises(ValueError, match="'b'"):
        consensus_verdict("m.f", ["a", "b"], {"a": T}, refs, classes=cls)
    auth = Authorities(spec={"a": Label(T, "")}, spec_id="x")
    with pytest.raises(ValueError, match="no cite"):
        consensus_verdict("m.f", ["a"], {"a": T}, refs, authorities=auth, classes={})
    auth = Authorities(user={"a": Label(Answer("opaque", "set"), "book#9")})
    with pytest.raises(ValueError, match="opaque"):
        consensus_verdict("m.f", ["a"], {"a": T}, refs, authorities=auth, classes={})
    auth = Authorities(spec={"a": Label(T, "c")})
    with pytest.raises(ValueError, match="spec_id"):
        consensus_verdict("m.f", ["a"], {"a": T}, refs, authorities=auth, classes={})
    auth = Authorities(convention={"a": Label(T, "c")})
    with pytest.raises(ValueError, match="convention_ids"):
        consensus_verdict("m.f", ["a"], {"a": T}, refs, authorities=auth, classes={})
    with pytest.raises(ValueError, match="no class for spec-silent input 'b'"):
        consensus_verdict("m.f", ["a", "b"], {"a": T, "b": T}, refs, classes={"a": "a"})
    bad: Refs = [("r1", {"a": T, "b": T}), ("r2", {"a": T}), *refs_const(6, {"a": T, "b": T})]
    with pytest.raises(ValueError, match="reference r2 has no answer for 'b'"):
        consensus_verdict("m.f", ["a", "b"], {"a": T, "b": T}, bad, classes=cls)
    auth = Authorities(either={"a": Either("")})
    with pytest.raises(ValueError, match="no cite"):
        consensus_verdict("m.f", ["a"], {"a": T}, refs, authorities=auth, classes={})
    # both forms: a table without an answer for a checked input raises, never reads as a pass
    with pytest.raises(ValueError, match="tree has no answer for 'b'"):
        baseline_verdict("m.f", ["a", "b"], {"a": T}, {"a": T, "b": T})
    with pytest.raises(ValueError, match="baseline has no answer for 'b'"):
        baseline_verdict("m.f", ["a", "b"], {"a": T, "b": T}, {"a": T})


# ---- new: the authority order


def test_differential_p12_user_outranks_spec() -> None:
    s = "us%er@example.com"
    refs = refs_const(8, {s: T})
    auth = Authorities(
        user={s: Label(F, "book#3", overrides="T1-PCT")},
        spec={s: Label(T, "T1-PCT")},
        spec_id="sha256:p",
    )
    res = consensus_verdict("m.f", [s], {s: F}, refs, authorities=auth, classes={})
    assert (res.verdict, res.provenance) == ("accept", (Provenance(s, "user", "book#3", F),))
    assert res.reference_defects == ()  # decision 6: a user answer logs no reference defect
    res = consensus_verdict("m.f", [s], {s: T}, refs, authorities=auth, classes={})
    assert (res.verdict, res.disagreements) == (
        "refuse",
        (Disagreement(s, T, F, "user", "book#3", 0),),
    )


def test_differential_p13_spec_outranks_either() -> None:
    s = "user@domain.com\n"
    auth = Authorities(
        spec={s: Label(F, "T1-NL")}, spec_id="sha256:p", either={s: Either("book#4")}
    )
    res = consensus_verdict("m.f", [s], {s: T}, refs_const(8, {s: T}), authorities=auth, classes={})
    assert (res.verdict, res.either, res.disagreements[0].cite) == ("refuse", (), "T1-NL")
    res = consensus_verdict("m.f", [s], {s: F}, refs_const(8, {s: T}), authorities=auth, classes={})
    assert (res.verdict, res.either) == ("accept", ())


def test_differential_p14_either_outranks_convention() -> None:
    s, t = '"john doe"@x.com', "a@x.com"
    auth = Authorities(
        either={s: Either("book#5", anchor=True)},
        convention={s: Label(F, "whatwg-html@2025-09:valid-e-mail-address")},
        convention_ids=("whatwg-html@2025-09",),
    )
    res = consensus_verdict(
        "m.f",
        [s, t],
        {s: T, t: T},
        refs_const(8, {s: T, t: T}),
        authorities=auth,
        classes={t: "plain"},
    )
    assert (res.verdict, res.either, res.disagreements, res.unanimous) == ("accept", (s,), (), 1)
    assert res.provenance[0] == Provenance(s, "either", "book#5", None)


# ---- new: class (a) spec-decided, and the references never override it


def test_differential_p15_unanimous_references_do_not_override_the_panel() -> None:
    s = "user@example.com"  # a contrarian label: the rule follows the label it is handed
    refs = refs_const(12, {s: T})
    auth = Authorities(spec={s: Label(F, "clause-X")}, spec_id="sha256:contrarian")
    res = consensus_verdict("m.f", [s], {s: T}, refs, authorities=auth, classes={s: "c"})
    assert (res.verdict, res.disagreements) == (
        "refuse",
        (Disagreement(s, T, F, "spec", "clause-X", 0),),
    )
    assert [
        (x.input, x.reference, x.got, x.expected, x.source, x.cite) for x in res.reference_defects
    ] == [(s, f"r{i + 1}", T, F, "spec", "clause-X") for i in range(12)]
    assert (res.spec_id, res.references, res.provenance) == (
        "sha256:contrarian",
        12,
        (Provenance(s, "spec", "clause-X", F),),
    )
    res = consensus_verdict("m.f", [s], {s: F}, refs, authorities=auth, classes={s: "c"})
    assert (res.verdict, len(res.reference_defects)) == ("accept", 12)


def test_differential_p16_trailing_newline_on_the_real_vector() -> None:
    s = "user@domain.com\n"
    refs: Refs = [(n, {s: a}) for n, a in NL_VEC.items()]
    panel = Authorities(spec={s: Label(F, "T1-NL")}, spec_id="sha256:t1")
    res = consensus_verdict("m.f", [s], {s: T}, refs, authorities=panel, classes={s: "ws"})
    assert (res.verdict, res.disagreements) == (
        "refuse",
        (Disagreement(s, T, F, "spec", "T1-NL", 0),),
    )
    assert sorted(x.reference for x in res.reference_defects) == sorted(
        n for n, a in NL_VEC.items() if a == T
    )
    assert len(res.reference_defects) == 10
    res = consensus_verdict("m.f", [s], {s: F}, refs, authorities=panel, classes={s: "ws"})
    assert (res.verdict, res.references, res.unspecified) == ("accept", 12, ())
    res = consensus_verdict("m.f", [s], {s: F}, refs, classes={s: "ws"})  # no panel: asked
    assert (res.verdict, res.unspecified, res.disagreements) == ("route", (s,), ())


def test_differential_p17_lone_dissent_on_the_real_vector() -> None:
    s = "user@domain.c"
    refs: Refs = [(n, {s: a}) for n, a in LONE_VEC.items()]
    res = consensus_verdict("m.f", [s], {s: T}, refs, classes={s: "tld1"})
    assert (res.verdict, res.disagreements) == ("route", ())
    assert [q.signatures for q in res.questions] == [(tuple(LONE_VEC.values()),)]
    panel = Authorities(spec={s: Label(T, "RFC5322+6532+5321+WHATWG accept")}, spec_id="sha256:r1")
    res = consensus_verdict("m.f", [s], {s: T}, refs, authorities=panel, classes={s: "tld1"})
    assert (res.verdict, len(res.reference_defects)) == ("accept", 11)
    res = consensus_verdict("m.f", [s], {s: F}, refs, authorities=panel, classes={s: "tld1"})
    assert res.verdict == "refuse"


def test_differential_p18_a_defective_reference_keeps_its_other_votes() -> None:
    a, b = "us}er@domain.com", "user@domain.com\n"
    ps = plain(9)
    refs: Refs = [(n, {a: BRACE_VEC[n], b: NL_VEC[n], **dict.fromkeys(ps, T)}) for n in REAL]
    panel = Authorities(
        spec={a: Label(T, "RFC 5322 atext"), b: Label(F, "T1-NL")}, spec_id="sha256:p"
    )
    tree = {a: T, b: F, **dict.fromkeys(ps, T)}
    cls = dict.fromkeys(ps, "plain")
    res = consensus_verdict("m.f", [a, b, *ps], tree, refs, authorities=panel, classes=cls)
    assert (res.verdict, res.references, len(res.reference_defects)) == ("accept", 12, 21)
    assert [p.source for p in res.provenance] == ["spec", "spec"] + ["references"] * 9
    res = consensus_verdict(
        "m.f", [a, b, *ps], {**tree, ps[0]: F}, refs, authorities=panel, classes=cls
    )
    assert res.verdict == "refuse"
    assert res.disagreements == (Disagreement(ps[0], F, T, "references", "", 12),)


def test_differential_p19_one_spec_disagreement_in_a_hundred_refuses() -> None:
    s, ps = "user@domain.com\n", plain(99)
    refs = refs_const(8, {s: F, **dict.fromkeys(ps, T)})
    panel = Authorities(spec={s: Label(F, "T1-NL")}, spec_id="sha256:p")
    res = consensus_verdict(
        "m.f",
        [s, *ps],
        {s: T, **dict.fromkeys(ps, T)},
        refs,
        authorities=panel,
        classes=dict.fromkeys(ps, "plain"),
    )
    assert (res.verdict, res.d) == ("refuse", 0)
    assert res.disagreements == (Disagreement(s, T, F, "spec", "T1-NL", 0),)


# ---- new: class (b) spec-silent, the band backstop, one question per class


def test_differential_p20_band_boundary_is_inclusive() -> None:
    for n, want in ((20, "refuse"), (21, "question")):
        ps = plain(n)
        tree = dict.fromkeys(ps, T)
        tree[ps[0]] = F
        res = consensus_verdict(
            "m.f", ps, tree, refs_const(8, dict.fromkeys(ps, T)), classes=dict.fromkeys(ps, "plain")
        )
        assert (res.d, res.unanimous, res.verdict) == (1, n, want)


def test_differential_p21_a_thin_silent_disagreement_is_asked_not_refused() -> None:
    ps, u = plain(99), ".u@x.com"
    refs = refs_const(12, {**dict.fromkeys(ps, T), u: F})
    res = consensus_verdict(
        "m.f",
        [*ps, u],
        {**dict.fromkeys(ps, T), u: T},
        refs,
        classes={**dict.fromkeys(ps, "plain"), u: "dots"},
    )
    assert (res.verdict, res.d, res.unanimous) == ("question", 1, 100)
    assert res.band == (BandQuestion("dots", (u,), (F,), (T,)),)
    assert res.asks == ("dots",)


def test_differential_p22_one_question_per_class() -> None:
    ws = [" a@x.com", "\ta@x.com", "a@x.com "]
    sp = ["\va@x.com", "\x0ca@x.com"]
    ps = plain(95)
    refs: Refs = [
        (
            f"r{i}",
            {
                **dict.fromkeys(ws, F),
                sp[0]: (T if i < 6 else F),
                sp[1]: (T if i < 3 else F),
                **dict.fromkeys(ps, T),
            },
        )
        for i in range(12)
    ]
    tree = {**dict.fromkeys(ws, T), **dict.fromkeys(sp, T), **dict.fromkeys(ps, T)}
    res = consensus_verdict(
        "m.f",
        [*ws, *sp, *ps],
        tree,
        refs,
        classes={**dict.fromkeys(ws + sp, "ws"), **dict.fromkeys(ps, "plain")},
    )
    assert (res.verdict, res.d, res.unanimous) == ("question", 3, 98)
    assert [(b.cls, b.inputs) for b in res.band] == [("ws", tuple(ws))]
    assert [(q.cls, q.inputs, len(q.signatures)) for q in res.questions] == [("ws", tuple(sp), 2)]
    assert res.asks == ("ws",)


def test_differential_p23_either_is_out_of_d_and_u() -> None:
    ps, u = plain(20), ".u@x.com"
    refs = refs_const(8, {**dict.fromkeys(ps, T), u: F})
    tree = {**dict.fromkeys(ps, T), u: F}
    tree[ps[0]] = F
    cls = {**dict.fromkeys(ps, "plain"), u: "dots"}
    res = consensus_verdict("m.f", [*ps, u], tree, refs, classes=cls)
    assert (res.verdict, res.d, res.unanimous) == ("question", 1, 21)
    auth = Authorities(either={u: Either("book#7")})
    res = consensus_verdict("m.f", [*ps, u], tree, refs, authorities=auth, classes=cls)
    assert (res.verdict, res.d, res.unanimous, res.either) == ("refuse", 1, 20, (u,))  # 1/20
    auth = Authorities(either={ps[0]: Either("book#8")})
    res = consensus_verdict("m.f", [*ps, u], tree, refs, authorities=auth, classes=cls)
    assert (res.verdict, res.d, res.unanimous, res.either, res.band) == (
        "accept",
        0,
        20,
        (ps[0],),
        (),
    )
    assert res.provenance[0] == Provenance(ps[0], "either", "book#8", None)


# ---- new: class (c) adopted convention


def test_differential_p24_a_convention_decides_only_when_adopted() -> None:
    s = '"a b"@x.com'
    refs = refs_const(8, {s: T})
    cite = "whatwg-html@2025-09:valid-e-mail-address"
    conv = Authorities(convention={s: Label(F, cite)}, convention_ids=("whatwg-html@2025-09",))
    res = consensus_verdict("m.f", [s], {s: T}, refs, authorities=conv, classes={s: "quoted"})
    assert (res.verdict, res.convention_ids) == ("refuse", ("whatwg-html@2025-09",))
    assert res.disagreements == (Disagreement(s, T, F, "convention", cite, 0),)
    assert len(res.reference_defects) == 8
    assert {x.source for x in res.reference_defects} == {"convention"}
    res = consensus_verdict("m.f", [s], {s: T}, refs, classes={s: "quoted"})
    assert (res.verdict, res.convention_ids, res.provenance[0].source) == (
        "accept",
        (),
        "references",
    )


# ---- new: A4' and the all-either set


def test_differential_p25_authorities_decide_below_the_reference_floor() -> None:
    s, t = "user@domain.com\n", "a@x.com"
    panel = Authorities(spec={s: Label(F, "T1-NL")}, spec_id="sha256:p")
    for refs in ([], refs_const(7, {s: T, t: T})):
        res = consensus_verdict("m.f", [s, t], {s: T, t: T}, refs, authorities=panel, classes={})
        assert (res.verdict, res.disagreements) == (
            "refuse",
            (Disagreement(s, T, F, "spec", "T1-NL", 0),),
        )
        assert [p.source for p in res.provenance] == ["spec", "unreferenced"]
        assert res.reference_defects == ()
        res = consensus_verdict("m.f", [s, t], {s: F, t: T}, refs, authorities=panel, classes={})
        assert (res.verdict, res.disagreements) == ("no-reference", ())
    bad_tree = Unusable("missing", "AttributeError")
    res = consensus_verdict("m.f", [s, t], bad_tree, [], authorities=panel, classes={})
    assert (res.verdict, res.tree_unusable) == ("refuse", bad_tree)
    res = consensus_verdict("m.f", [t], bad_tree, [], classes={})
    assert (res.verdict, res.tree_unusable) == ("no-reference", None)  # the draft's A4, unchanged


def test_differential_p26_every_input_either_is_not_applicable() -> None:
    s = "a@x.com"
    auth = Authorities(either={s: Either("book#6")})
    res = consensus_verdict("m.f", [s], {s: T}, refs_const(8, {s: T}), authorities=auth, classes={})
    assert (res.verdict, res.either) == ("not-applicable", (s,))
    res = consensus_verdict("m.f", [s], {s: T}, [], authorities=auth, classes={})
    assert (res.verdict, res.either) == ("not-applicable", (s,))  # below the floor too


# ---- final: spec beats user unless the user overrides a named clause


def test_differential_p27_a_user_label_on_a_decided_input_must_name_the_clause() -> None:
    s = "user@domain.com\n"
    refs = refs_const(8, {s: F})
    for ov in ("", "T1-PCT"):  # no override; an override naming another clause
        auth = Authorities(
            user={s: Label(T, "book#9", overrides=ov)},
            spec={s: Label(F, "T1-NL")},
            spec_id="sha256:p",
        )
        with pytest.raises(ValueError, match="T1-NL"):
            consensus_verdict("m.f", [s], {s: T}, refs, authorities=auth, classes={})
    auth = Authorities(
        user={s: Label(T, "book#9", overrides="T1-NL")},
        spec={s: Label(F, "T1-NL")},
        spec_id="sha256:p",
    )
    assert (
        consensus_verdict("m.f", [s], {s: T}, refs, authorities=auth, classes={}).verdict
        == "accept"
    )
    conv = Authorities(
        user={s: Label(T, "book#9")},
        convention={s: Label(F, "whatwg@1:x")},
        convention_ids=("whatwg@1",),
    )
    with pytest.raises(ValueError, match="whatwg@1:x"):
        consensus_verdict("m.f", [s], {s: T}, refs, authorities=conv, classes={})
    free = Authorities(
        user={"a@x.com": Label(T, "book#10")}
    )  # an undecided input needs no override
    res = consensus_verdict(
        "m.f",
        ["a@x.com"],
        {"a@x.com": T},
        refs_const(8, {"a@x.com": F}),
        authorities=free,
        classes={},
    )
    assert res.verdict == "accept"


# ---- addendum 2026-09-25 (answer book interface items)


def test_differential_p28_reference_pins_have_their_own_slot() -> None:
    s, q = "u..x@example.com", "a@x.com"
    refs = refs_const(8, {s: F, q: T})
    pin = Authorities(reference_pins={s: Label(F, "references-pin:ab12")})
    res = consensus_verdict("m.f", [s, q], {s: T, q: T}, refs, authorities=pin, classes={q: "p"})
    assert res.verdict == "refuse"
    assert res.disagreements == (Disagreement(s, T, F, "reference-pin", "references-pin:ab12", 0),)
    assert (res.pinned, res.d, res.reference_defects) == (0, 0, ())  # final at any d; no defects
    assert res.provenance[0] == Provenance(s, "reference-pin", "references-pin:ab12", F)
    res = consensus_verdict("m.f", [s, q], {s: F, q: T}, refs, authorities=pin, classes={q: "p"})
    assert res.verdict == "accept"
    res = consensus_verdict("m.f", [s], {s: T}, refs_const(3, {s: F}), authorities=pin, classes={})
    assert res.verdict == "refuse"  # a pin decides below the floor too
    # the overload once proposed is NOT equivalent: as a user label on a spec-decided input
    # it raises
    nl = "user@domain.com\n"
    over = Authorities(
        user={nl: Label(F, "references-pin:cd34")}, spec={nl: Label(F, "T1-NL")}, spec_id="sha256:p"
    )
    with pytest.raises(ValueError, match="T1-NL"):
        consensus_verdict(
            "m.f", [nl], {nl: F}, refs_const(8, {nl: F}), authorities=over, classes={}
        )
    for bad in (
        Authorities(reference_pins={s: Label(F, "book#1")}),
        Authorities(reference_pins={s: Label(F, "references-pin:x")}, user={s: Label(F, "book#1")}),
    ):
        with pytest.raises(ValueError, match=r"references-pin|slot"):
            consensus_verdict("m.f", [s], {s: F}, refs, authorities=bad, classes={})


def test_differential_p29_several_conventions_each_label_names_an_adopted_id() -> None:
    s, t = '"a b"@x.com', "a@b"
    refs = refs_const(8, {s: T, t: T})
    conv = Authorities(
        convention={
            s: Label(F, "whatwg-html@2025-09:valid"),
            t: Label(F, "rfc5321-strict@1:domain"),
        },
        convention_ids=("whatwg-html@2025-09", "rfc5321-strict@1"),
    )
    res = consensus_verdict("m.f", [s, t], {s: F, t: F}, refs, authorities=conv, classes={})
    assert (res.verdict, res.convention_ids) == (
        "accept",
        ("whatwg-html@2025-09", "rfc5321-strict@1"),
    )
    for bad in (
        Authorities(convention={s: Label(F, "other@1:x")}, convention_ids=("whatwg-html@2025-09",)),
        Authorities(
            convention={s: Label(F, "whatwg-html@2025-09x:valid")},
            convention_ids=("whatwg-html@2025-09",),
        ),
        Authorities(convention={s: Label(F, "a@1:x")}, convention_ids=("a@1", "a@1")),
    ):
        with pytest.raises(ValueError, match="convention"):
            consensus_verdict("m.f", [s], {s: F}, refs, authorities=bad, classes={})


def test_differential_p30_either_over_a_convention_input_needs_an_override_or_anchor() -> None:
    s = '"john doe"@x.com'
    refs = refs_const(8, {s: T})
    conv_label = {s: Label(F, "whatwg-html@2025-09:valid")}
    ids = ("whatwg-html@2025-09",)
    for e in (
        Either("either:e1", anchor=True),
        Either("either:e1", overrides="whatwg-html@2025-09:valid"),
    ):
        auth = Authorities(either={s: e}, convention=conv_label, convention_ids=ids)
        res = consensus_verdict("m.f", [s], {s: T}, refs, authorities=auth, classes={})
        assert (res.verdict, res.either) == ("not-applicable", (s,))
    for e in (Either("either:e1"), Either("either:e1", overrides="whatwg-html@2025-09:other")):
        auth = Authorities(either={s: e}, convention=conv_label, convention_ids=ids)
        with pytest.raises(ValueError, match="whatwg-html@2025-09:valid"):
            consensus_verdict("m.f", [s], {s: T}, refs, authorities=auth, classes={})
    auth = Authorities(either={s: Either("either:e2")})
    res = consensus_verdict("m.f", [s], {s: T}, refs, authorities=auth, classes={})
    assert res.verdict == "not-applicable"  # no convention: a plain "either" suffices
