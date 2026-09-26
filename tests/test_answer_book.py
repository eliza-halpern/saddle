"""The answer book's known-good / known-bad pairs (S3-4-final.md, 51 pairs; names carry the id).

Fixtures are R2's real per-input rows for M1-F's Fix-8 set and the case list
(`tests/fixtures/t1_reference_rows.jsonl`: the 12 frozen references'
signature and R1's four-grammar votes per input, 2009 rows), and R1's
published-grammar panel, vendored below as `PANEL` so the panel votes are
recomputed here rather than trusted. Pair P0 checks the vendored panel
against the recorded votes on every row: both halves of the fixture.

The two spec entries are the user's decision of 2026-09-25 21:11 EDT
(phase2/measurements/T1-rescore-t1_probe_g.md, "Decisions").
"""

from __future__ import annotations

import hashlib
import io
import ipaddress
import json
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from saddle import answer_book as ab
from saddle import rule_d
from saddle.answer_book import (
    FROZEN_CLASS,
    FROZEN_SPEC,
    AnswersError,
    Book,
    Ctx,
    answer_either,
    ask_question,
    authorities,
    canon,
    ck,
    class_dissent,
    confirm_either,
    confirm_override,
    detected_misses,
    frozen_hash,
    k3,
    order_questions,
    question_groups,
    render_pinned_tests,
    sha,
    shortest_first,
)

# ------------------------------------------------------------------ R1's grammar panel (vendored)


def _r5322(nonascii: bool) -> re.Pattern[str]:
    na = r"\u0080-\U0010ffff" if nonascii else ""
    fws = r"(?:(?:[ \t]*\r\n)?[ \t]+)"
    qp = r"(?:\\[\x21-\x7e \t" + na + r"])"
    ctext = r"[\x21-\x27\x2a-\x5b\x5d-\x7e" + na + r"]"
    comment = rf"(?:\((?:{fws}?(?:{ctext}|{qp}))*{fws}?\))"
    cfws = rf"(?:(?:{fws}?{comment})+{fws}?|{fws})"
    atext = r"[A-Za-z0-9!#$%&'*+\-/=?^_`{|}~" + na + r"]"
    dot_atom = rf"{cfws}?{atext}+(?:\.{atext}+)*{cfws}?"
    qtext = r"[\x21\x23-\x5b\x5d-\x7e" + na + r"]"
    quoted = rf'{cfws}?"(?:{fws}?(?:{qtext}|{qp}))*{fws}?"{cfws}?'
    dtext = r"[\x21-\x5a\x5e-\x7e" + na + r"]"
    dlit = rf"{cfws}?\[(?:{fws}?{dtext})*{fws}?\]{cfws}?"
    return re.compile(rf"(?:{dot_atom}|{quoted})@(?:{dot_atom}|{dlit})")


_RE5322 = _r5322(False)
_RE6532 = _r5322(True)


def rfc5322(s: str) -> bool:
    return _RE5322.fullmatch(s) is not None


def rfc6532(s: str) -> bool:
    return _RE6532.fullmatch(s) is not None


_ATEXT = r"[A-Za-z0-9!#$%&'*+\-/=?^_`{|}~]"
_LOCAL5321 = re.compile(
    rf'{_ATEXT}+(?:\.{_ATEXT}+)*|"(?:[\x20\x21\x23-\x5b\x5d-\x7e]|\\[\x20-\x7e])*"'
)
_DOMAIN5321 = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*"
)
_SNUM = re.compile(r"[0-9]{1,3}")


def _addr_literal_5321(d: str) -> bool:
    if not (d.startswith("[") and d.endswith("]")):
        return False
    body = d[1:-1]
    if body.startswith("IPv6:"):
        try:
            ipaddress.IPv6Address(body[5:])
        except ValueError:
            return False
        return True
    parts = body.split(".")
    return len(parts) == 4 and all(_SNUM.fullmatch(p) and int(p) <= 255 for p in parts)


def rfc5321(s: str) -> bool:
    if not s.isascii() or "@" not in s:
        return False
    local, _, domain = s.rpartition("@")
    if not _LOCAL5321.fullmatch(local):
        return False
    if len(local.encode()) > 64 or len(domain.encode()) > 255:
        return False
    return bool(_DOMAIN5321.fullmatch(domain)) or _addr_literal_5321(domain)


_WHATWG = re.compile(
    r"^[a-zA-Z0-9.!#$%&'*+\/=?^_`{|}~-]+@[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
    r"(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*$"
)


def whatwg(s: str) -> bool:
    return _WHATWG.fullmatch(s) is not None


GRAMMARS: dict[str, Callable[[str], bool]] = {
    "rfc5322": rfc5322,
    "rfc6532": rfc6532,
    "rfc5321": rfc5321,
    "whatwg": whatwg,
}


def votes(s: str) -> str:
    return ",".join("T" if f(s) else "F" for f in GRAMMARS.values())


PANEL_SHA = "748c5ce2a55c9962" + "0" * 48  # R1 grammars.py sha256 prefix; the rest is a fixture
FIXTURE = Path(__file__).parent / "fixtures" / "t1_reference_rows.jsonl"

# ------------------------------------------------------------------ fixtures (R2's real rows)


def _load_rows() -> dict[str, dict[str, str]]:
    rows: dict[str, dict[str, str]] = {}
    for line in FIXTURE.read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        rows[r["input"]] = r
    return rows


ROWS = _load_rows()
REFS: dict[str, str] = {s: r["signature"] for s, r in ROWS.items()}
R2VOTES: dict[str, str] = {s: r["panel_vote"] for s, r in ROWS.items()}
ALL = list(ROWS)


def sel(
    *, k3_key: str | None = None, signature: str | None = None, vote: str | None = None
) -> list[str]:
    out = []
    for s in ALL:
        if k3_key is not None and repr(k3(s)) != k3_key:
            continue
        if signature is not None and REFS[s] != signature:
            continue
        if vote is not None and R2VOTES[s] != vote:
            continue
        out.append(s)
    return out


DD = "u..x@example.com"
DD_MEMBERS = sel(signature=REFS[DD], vote=R2VOTES[DD], k3_key=repr(k3(DD)))
LEAD = ".u@example.com"
LEAD_MEMBERS = sel(signature=REFS[LEAD], vote=R2VOTES[LEAD], k3_key=repr(k3(LEAD)))
WS_TRIM = sel(k3_key="('ws',)", signature="F" * 12, vote="T,T,F,F")
WS_DEAD = sel(k3_key="('ws',)", signature="F" * 12, vote="F,F,F,F")
NEWLINES = [s for s in ALL if s.endswith("\n")]
PCT = sel(k3_key="('core', ('%',), False, False, False)", vote="T,T,T,T")
PCT_OUT = sel(k3_key="('core', ('%',), False, False, False)", vote="F,F,F,F")
LF_ORACLE = "user@example.com\n"  # t1_probe_g's literal; not in R2's tables
LF_SPLIT = "user@domain.com\n"  # 10 of 12 references accept it
# Two inputs not in R2's tables, for the grouping pair only; references synthetic (labelled).
SYNTH = {"user@[192.168.1.1]": "F" * 12, "user@[2001:db8::1]": "F" * 12}
REFS.update(SYNTH)

FN = "validators.is_valid_email"
T0 = "2026-09-25T21:00:00Z"
T_DEC = "2026-09-26T01:11:00Z"  # 2026-09-25 21:11 EDT
Q = hashlib.sha256(b"Should ' use@example.com' be accepted?").hexdigest()
TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
DEC_REF = "phase2/measurements/T1-rescore-t1_probe_g.md@e206e1789037"
SPEC_PCT_CLASS: dict[str, object] = {
    "clause_id": "t1.local-pct@1",
    "clause": "'%' is atext; a '%' in the local part does not by itself make an address invalid",
    "sources": ["RFC 5322 3.2.3 atext", "RFC 5321 4.1.2", "RFC 3696 3"],
    "answer": "accept",
    "key_fn": "k3@1",
    "anchors": ["us%er@example.com"],
    "scope": "class",
}
# Row 6 (DECISIONS-2026-09-25, approved): the ADOPTED T1 entry is anchor-scoped. SPEC_PCT_CLASS
# stays as the fixture that exercises the class-scope mechanism (M1, M3, M5, N1, F-pairs, D1).
SPEC_PCT: dict[str, object] = dict(SPEC_PCT_CLASS, scope="anchors")
SPEC_LF: dict[str, object] = {
    "clause_id": "t1.trailing-lf@1",
    "clause": "a trailing newline is not part of an address; all four grammars reject it",
    "sources": [
        "RFC 5322 3.4.1 addr-spec",
        "RFC 6532 3.2",
        "RFC 5321 4.1.2 Mailbox",
        "WHATWG HTML valid email address",
        "R1 panel grammars.py@748c5ce2a55c",
    ],
    "answer": "reject",
    "key_fn": "trailing-lf@1",
    "anchors": [LF_ORACLE],
    "scope": "class",
}


def majority(s: str) -> bool | None:
    v = REFS.get(s)
    return None if v is None else v.count("T") * 2 > len(v)


_DOLLAR = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")
_FULL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+")


def dollar_validator(s: str) -> bool:  # the shape 18 of the 27 flipped trees used
    return _DOLLAR.match(s) is not None


def full_validator(s: str) -> bool:
    return _FULL.fullmatch(s) is not None


CONVS: dict[str, Callable[[str], bool | None]] = {
    "rfc5321-mailbox@2008": rfc5321,
    "py-dollar-anchor@1": dollar_validator,
    "majority-of-references@1": majority,
}


def ctx(
    *,
    panel: bool = True,
    refs: dict[str, str] | None = None,
    conventions: dict[str, Callable[[str], bool | None]] | None = None,
    reference_set_id: str = "refs-12",
) -> Ctx:
    return Ctx(
        refs if refs is not None else REFS,
        panel=votes if panel else None,
        panel_sha=PANEL_SHA if panel else "",
        reference_set_id=reference_set_id,
        conventions=conventions or {},
        _votes=dict(R2VOTES) if panel else {},
    )


def reg(
    book: Book,
    *,
    key_fn: str | None = "k3@1",
    either_by: Sequence[str] = ("eliza",),
    either_at: Sequence[str] = ("plan", "verdict"),
    panel: bool = True,
) -> str:
    return book.append(
        {
            "kind": "registration",
            "callable": FN,
            "by": "eliza",
            "at": T0,
            "key_fn": key_fn,
            "either_by": list(either_by),
            "either_at": list(either_at),
            "panel_id": "R1-grammars" if panel else None,
            "panel_sha": PANEL_SHA if panel else None,
        }
    )


def adopt(
    book: Book,
    spec: dict[str, object],
    c: Ctx,
    known: Sequence[str] = (),
    *,
    confirmed: bool = True,
) -> tuple[str, ab.Preview]:
    return book.adopt_spec(
        FN,
        spec,
        known=list(known),
        ctx=c,
        adopted_by="eliza",
        at=T_DEC,
        run_id="decision-2026-09-25-2111",
        decision_ref=DEC_REF,
        confirmed_preview=confirmed,
    )


def conv(
    book: Book, cid: str, version: str, c: Ctx, known: Sequence[str] = ()
) -> tuple[str, list[str]]:
    return book.adopt_convention(
        FN,
        conv_id=cid,
        version=version,
        conv_sha="0" * 64,
        clause=f"{cid} grammar",
        sources=[cid],
        known=list(known),
        ctx=c,
        adopted_by="eliza",
        at=T0,
        run_id="run-1",
    )


def exact(
    book: Book,
    x: str,
    answer: str,
    *,
    source: str = "user",
    by: str = "eliza",
    tree_hash: str = TREE,
    ask_point: str = "verdict",
    overrides: Sequence[str] = (),
) -> str:
    rec: dict[str, object] = {
        "kind": "exact",
        "callable": FN,
        "input": x,
        "answer": answer,
        "source": source,
        "by": by,
        "at": T0,
        "question_hash": Q if source == "user" else "",
        "tree_hash": tree_hash,
        "ask_point": ask_point,
        "run_id": "run-1",
    }
    if overrides:
        rec["overrides"] = list(overrides)
    return book.append(rec)


def klass(
    book: Book,
    anchors: dict[str, str],
    answer: str,
    c: Ctx,
    known: Sequence[str] = (),
    *,
    confirmed: bool = True,
    overrides: Sequence[str] = (),
    by: str = "eliza",
    ask_point: str = "verdict",
) -> tuple[str, ab.Preview]:
    pv = book.preview(FN, answer, anchors, list(known) or list(anchors), c, overrides=overrides)
    h = book.save_class(
        FN,
        answer,
        anchors,
        pv,
        confirmed_preview=confirmed,
        by=by,
        at=T0,
        question="q",
        tree_hash=TREE,
        ask_point=ask_point,
        run_id="run-1",
        ctx=c,
        overrides=overrides,
    )
    return h, pv


def either_at_plan(book: Book, anchor: str, known: Sequence[str], c: Ctx) -> str:
    return answer_either(
        book,
        FN,
        anchor,
        known,
        None,
        c,
        by="eliza",
        ask_point="plan",
        stdin=io.StringIO(""),
        stdout=io.StringIO(),
        run_id="run-1",
        tree_hash="",
        at=T0,
    )


def body(rec: dict[str, object]) -> dict[str, object]:
    return {k: v for k, v in rec.items() if k not in ("record_hash", "prev")}


# ------------------------------------------------------------------ pairs


def test_answer_book_p0_fixtures_agree_with_the_recorded_panel_votes() -> None:
    # both halves: the vendored panel reproduces R2's recorded votes on every row, and the
    # fixture's class sizes are the spec's (fact 1, fact 5, M)
    assert all(votes(s) == R2VOTES[s] for s in ALL), (
        "vendored panel drifted from the recorded votes"
    )
    assert votes("user@example.com") == "T,T,T,T"
    assert votes(".u@example.com") == "F,F,F,T"
    got = (
        len(ALL),
        len(DD_MEMBERS),
        len(LEAD_MEMBERS),
        len(WS_TRIM),
        len(WS_DEAD),
        len(NEWLINES),
        len(PCT),
        len(PCT_OUT),
    )
    assert got == (2009, 13, 28, 17, 121, 11, 28, 2)
    assert REFS[LF_SPLIT].count("T") == 10
    assert votes(LF_ORACLE) == "F,F,F,F"


# C: append-only chain


def test_answer_book_c1_identical_bytes_and_exact_inputs_survive_a_round_trip(
    tmp_path: Path,
) -> None:
    b = Book()
    reg(b)
    for x in ("x\n", "\x00", "café@x.com"):
        exact(b, x, "reject")
    t = b.dumps()
    assert t == b.dumps()
    assert Book.loads(t).dumps() == t
    assert [r["input"] for r in Book.loads(t).records if r["kind"] == "exact"] == [
        "x\n",
        "\x00",
        "café@x.com",
    ]
    path = tmp_path / "answers.jsonl"
    assert Book.load(path).records == []  # a missing file is an empty book
    b.dump(path)
    assert Book.load(path).dumps() == t


def test_answer_book_c2_a_rewritten_or_deleted_record_fails_load_where_the_chain_breaks() -> None:
    b = Book()
    reg(b)
    exact(b, WS_TRIM[0], "accept")
    exact(b, WS_TRIM[1], "reject")
    lines = b.dumps().splitlines()
    r = json.loads(lines[1])
    r["answer"] = "reject"
    with pytest.raises(AnswersError, match="record 2: chain broken"):
        Book.loads("\n".join([*lines[:1], canon(r), *lines[2:]]) + "\n")
    r["record_hash"] = sha({k: v for k, v in r.items() if k != "record_hash"})  # re-hashed
    with pytest.raises(AnswersError, match="record 3: chain broken"):
        Book.loads("\n".join([*lines[:1], canon(r), *lines[2:]]) + "\n")
    with pytest.raises(AnswersError, match="record 2: chain broken"):
        Book.loads("\n".join([*lines[:1], *lines[2:]]) + "\n")
    with pytest.raises(AnswersError, match=r"answers\.jsonl: record 1: not JSON"):
        Book.loads("{not json\n")
    with pytest.raises(AnswersError, match="record 1: not a JSON object"):
        Book.loads("[1, 2]\n")


# E: entry kinds and provenance


def test_answer_book_e1_every_entry_kind_with_full_provenance_appends_and_reloads() -> None:
    c = ctx(conventions=CONVS)
    b = Book()
    reg(b)
    adopt(b, SPEC_LF, c, known=NEWLINES)
    conv(b, "rfc5321-mailbox", "2008", c, known=DD_MEMBERS)
    exact(b, WS_TRIM[1], "reject")
    exact(b, WS_TRIM[2], "reject", source="references", by="references")
    h, _pv = klass(b, {WS_TRIM[0]: "accept"}, "accept", c, known=WS_TRIM[:1])
    b.revoke(h, by="eliza", at=T0, reason="test")
    assert Book.loads(b.dumps()).dumps() == b.dumps()
    kinds = [r["kind"] for r in b.records]
    assert kinds == ["registration", "spec", "convention", "exact", "exact", "class", "revoke"]


def test_answer_book_e2_a_user_answer_without_question_hash_tree_hash_or_run_id_is_refused() -> (
    None
):
    b = Book()
    reg(b)
    base: dict[str, object] = {
        "kind": "exact",
        "callable": FN,
        "input": "a@b",
        "answer": "accept",
        "source": "user",
        "by": "eliza",
        "at": T0,
        "question_hash": Q,
        "tree_hash": TREE,
        "ask_point": "verdict",
        "run_id": "run-1",
    }
    with pytest.raises(AnswersError, match="question_hash"):
        b.append(dict(base, question_hash=""))
    with pytest.raises(AnswersError, match="tree_hash"):
        b.append(dict(base, tree_hash=""))
    with pytest.raises(AnswersError, match="run_id"):
        b.append(dict(base, run_id=""))
    with pytest.raises(AnswersError, match="lacks"):
        b.append({k: v for k, v in base.items() if k != "at"})
    with pytest.raises(AnswersError, match="ask_point"):
        b.append(dict(base, ask_point="later"))
    with pytest.raises(AnswersError, match="unknown kind"):
        b.append(dict(base, kind="note"))
    b.append(dict(base, tree_hash="", ask_point="plan"))  # plan time: no tree exists, "" is honest


def test_answer_book_e3_a_class_entry_whose_frozen_fields_changed_after_hashing_is_refused() -> (
    None
):
    c = ctx()
    b = Book()
    reg(b)
    klass(b, {WS_TRIM[0]: "accept"}, "accept", c, known=WS_TRIM)
    r = body(b.records[-1])
    r["answer"] = "reject"
    b2 = Book()
    reg(b2)
    with pytest.raises(AnswersError, match="class_hash"):
        b2.append(r)


def test_answer_book_e4_reserved_names_cannot_write_user_entries_and_spec_is_never_asked() -> None:
    c = ctx()
    b = Book()
    reg(b)
    with pytest.raises(AnswersError, match="only a user"):
        exact(b, "a@b", "accept", by="worker")
    with pytest.raises(AnswersError, match="only a user"):
        exact(b, "a@b", "accept", by="spec")
    with pytest.raises(AnswersError, match="source 'model'"):
        exact(b, "a@b", "accept", source="model")
    with pytest.raises(AnswersError, match="answers no question"):
        exact(b, "a@b", "accept", source="references", by="worker")
    adopt(b, SPEC_LF, c)
    rec = body(b.records[-1])
    with pytest.raises(AnswersError, match="never asked"):
        b.append(dict(rec, question_hash=Q))
    with pytest.raises(AnswersError, match="only a user may adopt"):
        b.append(dict(rec, adopted_by="references"))
    with pytest.raises(AnswersError, match="recorded as by='spec'"):
        b.append(dict(rec, by="eliza"))


# M: spec entries, worked on the user's two T1 decisions


def test_answer_book_m1_pct_accept_and_newline_reject_pin_their_classes_with_the_clause() -> None:
    c = ctx()
    b = Book()
    reg(b)
    _h, pv_pct = adopt(b, SPEC_PCT_CLASS, c, known=ALL)
    _h, pv_lf = adopt(b, SPEC_LF, c, known=ALL)
    r = b.resolve(FN, [*ALL, LF_ORACLE], c)
    pct = [
        x
        for x in r.pins
        if r.prov[x]["source"] == "spec" and r.prov[x]["clause"] == "t1.local-pct@1"
    ]
    lf = [
        x
        for x in r.pins
        if r.prov[x]["source"] == "spec" and r.prov[x]["clause"] == "t1.trailing-lf@1"
    ]
    assert sorted(pct) == sorted(PCT)
    assert all(r.pins[x] for x in pct)
    assert sorted(lf) == sorted([*NEWLINES, LF_ORACLE])
    assert not any(r.pins[x] for x in lf)
    assert (pv_pct.members, pv_lf.members) == (28, 12)
    assert pv_lf.others == shortest_first(NEWLINES)[:3]
    e = b.records[-1]
    assert (e["by"], e["adopted_by"], e["at"], e["question_hash"], e["panel_votes"]) == (
        "spec",
        "eliza",
        T_DEC,
        "",
        {LF_ORACLE: "F,F,F,F"},
    )
    assert (e["decision_ref"], e["panel_sha"]) == (DEC_REF, PANEL_SHA)


def test_answer_book_m2_a_spec_entry_the_panel_does_not_decide_or_contradicts_is_refused() -> None:
    c = ctx()
    b = Book()
    reg(b)
    dots = dict(
        SPEC_LF, clause_id="t1.leading-dot@1", answer="reject", key_fn="k3@1", anchors=[LEAD]
    )
    with pytest.raises(AnswersError, match="the panel votes F,F,F,T"):
        adopt(b, dots, c)
    with pytest.raises(AnswersError, match="the panel votes F,F,F,F"):
        adopt(b, dict(SPEC_LF, answer="accept"), c)
    two = dict(SPEC_LF, anchors=[LF_ORACLE, " "], scope="class")
    with pytest.raises(AnswersError, match="the anchors are not one class"):
        adopt(b, two, c)
    adopt(b, SPEC_LF, c)
    rec = body(b.records[-1])
    forged = dict(rec, anchors=[LEAD], panel_votes={LEAD: "F,F,F,T"})
    forged["entry_hash"] = frozen_hash(forged, FROZEN_SPEC)
    with pytest.raises(AnswersError, match="does not decide"):
        b.append(forged)
    contra = dict(rec, answer="accept")
    contra["entry_hash"] = frozen_hash(contra, FROZEN_SPEC)
    with pytest.raises(AnswersError, match="the panel contradicts accept"):
        b.append(contra)
    with pytest.raises(AnswersError, match="entry_hash"):
        b.append(dict(rec, scope="anchors"))  # a frozen field edited without re-hashing
    for bad_field in ({"key_fn": "k9@1"}, {"answer": "maybe"}):
        wrong = dict(rec, **bad_field)
        wrong["entry_hash"] = frozen_hash(wrong, FROZEN_SPEC)
        with pytest.raises(AnswersError, match="unknown key function or answer"):
            b.append(wrong)
    with pytest.raises(AnswersError, match="not the registered panel"):
        b.append(dict(rec, panel_sha="f" * 64))
    b2 = Book()
    reg(b2, panel=False)
    with pytest.raises(AnswersError, match="no registered panel"):
        adopt(b2, SPEC_LF, c)
    with pytest.raises(AnswersError, match="needs a registered panel"):
        b2.append(rec)


def test_answer_book_m3_a_spec_pinned_input_is_never_asked_though_the_references_split() -> None:
    c = ctx()
    b = Book()
    reg(b)
    cands = NEWLINES + PCT
    assert len(b.to_ask(FN, cands, c, set())) == 4  # without the entries: 4 classes to ask
    adopt(b, SPEC_PCT_CLASS, c, known=cands)
    adopt(b, SPEC_LF, c, known=cands)
    assert b.to_ask(FN, cands, c, set()) == []


def test_answer_book_m4_references_never_revoke_a_spec_pin_and_are_logged_as_defective() -> None:
    c = ctx()
    b = Book()
    reg(b)
    h, _ = adopt(b, SPEC_LF, c, known=NEWLINES)
    exact(b, LF_SPLIT, "accept", source="references", by="references")
    r = b.resolve(FN, [LF_SPLIT], c)
    assert (r.pins[LF_SPLIT], r.prov[LF_SPLIT]["source"]) == (False, "spec")
    assert {
        "kind": "references-vs-spec",
        "input": LF_SPLIT,
        "clause": "t1.trailing-lf@1",
        "dissent": 10,
        "of": 12,
    } in r.conflicts
    redraw = ctx(refs={**REFS, **dict.fromkeys(NEWLINES, "T" * 12)}, reference_set_id="refs-redraw")
    r2 = b.resolve(FN, NEWLINES, redraw)
    assert all(r2.pins[x] is False for x in NEWLINES)
    assert (
        sum(1 for cf in r2.conflicts if cf["kind"] == "references-vs-spec" and cf["dissent"] == 12)
        == 11
    )
    with pytest.raises(AnswersError, match="only a user"):
        b.revoke(h, by="references", at=T0, reason="12 of 12 accept")
    b.revoke(h, by="eliza", at=T0, reason="the user withdrew it")
    r3 = b.resolve(FN, [LF_SPLIT], c)
    assert (r3.pins[LF_SPLIT], r3.prov[LF_SPLIT]["source"]) == (True, "references")


def test_answer_book_m5_the_pct_class_excludes_members_the_panel_rejects_for_another_reason() -> (
    None
):
    c = ctx()
    b = Book()
    reg(b)
    adopt(b, SPEC_PCT_CLASS, c, known=ALL)
    r = b.resolve(FN, PCT_OUT, c)
    assert sorted(PCT_OUT) == ["us%er@example..com", "user_%40special@.com"]
    assert not any(x in r.pins for x in PCT_OUT)


def test_answer_book_m6_under_k3_the_newline_clause_would_reach_a_space() -> None:
    c = ctx()
    b = Book()
    reg(b)
    pv = b.preview_spec(
        FN, key_fn="k3@1", answer="reject", anchors=[LF_ORACLE], scope="class", known=ALL, ctx=c
    )
    assert (pv.others, pv.members) == ([" ", "\t", "  "], 128)
    pv = b.preview_spec(
        FN,
        key_fn="trailing-lf@1",
        answer="reject",
        anchors=[LF_ORACLE],
        scope="class",
        known=ALL,
        ctx=c,
    )
    assert pv.members == 12
    assert all(x.endswith("\n") for x in pv.others)


# F: precedence


def test_answer_book_f1_exact_user_beats_class_beats_spec_beats_convention_beats_references() -> (
    None
):
    c = ctx(conventions=CONVS)
    b = Book()
    reg(b)
    adopt(b, SPEC_LF, c, known=NEWLINES)
    klass(b, {WS_TRIM[0]: "accept"}, "accept", c, known=WS_TRIM)
    exact(b, WS_TRIM[1], "reject")
    conv(b, "rfc5321-mailbox", "2008", c, known=DD_MEMBERS + WS_TRIM)
    exact(b, DD, "accept", source="references", by="references")
    exact(b, LF_SPLIT, "accept", source="references", by="references")
    exact(b, "user@domaiin.com\n", "accept", overrides=["t1.trailing-lf@1"])
    xs = [DD, LF_SPLIT, WS_TRIM[1], WS_TRIM[2], "user@domaiin.com\n"]
    r = b.resolve(FN, xs, c)
    got = [(r.pins[x], r.prov[x]["source"]) for x in xs]
    assert got == [
        (False, "convention"),
        (False, "spec"),
        (False, "user"),
        (True, "class"),
        (True, "user"),
    ]


def test_answer_book_f2_a_user_answer_or_either_that_contradicts_a_spec_pin_does_not_beat_it() -> (
    None
):
    c = ctx()
    b = Book()
    reg(b)
    split3 = sel(k3_key="('ws',)", signature=REFS[LF_SPLIT], vote="F,F,F,F")
    assert sorted(split3) == sorted(["user@domain.om\n", LF_SPLIT, "user@domaiin.com\n"])
    either_at_plan(b, LF_SPLIT, split3, c)
    assert sorted(b.resolve(FN, split3, c).retired) == sorted(split3)  # before the spec entry
    _h, pv = adopt(b, SPEC_LF, c, known=split3)
    assert sorted(pv.user_conflicts) == sorted(split3)  # the user is told at adoption
    exact(b, LF_ORACLE, "accept")
    r = b.resolve(FN, [*split3, LF_ORACLE], c)
    assert r.retired == []
    assert all(r.pins[x] is False for x in [*split3, LF_ORACLE])
    assert sorted(cf["input"] for cf in r.conflicts if cf["kind"] == "user-vs-spec") == sorted(
        [*split3, LF_ORACLE]
    )


def test_answer_book_f3_a_user_class_answer_outranks_a_references_pin() -> None:
    c = ctx()
    b = Book()
    reg(b)
    exact(b, WS_TRIM[2], "reject", source="references", by="references")
    klass(b, {WS_TRIM[0]: "accept"}, "accept", c, known=WS_TRIM)
    r = b.resolve(FN, [WS_TRIM[2]], c)
    assert (r.pins[WS_TRIM[2]], r.prov[WS_TRIM[2]]["source"]) == (True, "class")


# N: conventions


def test_answer_book_n1_an_adopted_rfc5321_convention_decides_the_dot_classes_below_spec() -> None:
    c = ctx(conventions=CONVS)
    b = Book()
    reg(b)
    adopt(b, SPEC_PCT_CLASS, c, known=ALL)
    conv(b, "rfc5321-mailbox", "2008", c, known=ALL)
    r = b.resolve(FN, [DD, LEAD, "us%er@example.com"], c)
    assert [(r.pins[x], r.prov[x]["source"]) for x in (DD, LEAD)] == [(False, "convention")] * 2
    assert r.prov["us%er@example.com"] == {
        "source": "spec",
        "by": "spec",
        "clause": "t1.local-pct@1",
        "entry": b.records[1]["record_hash"],
    }
    with pytest.raises(AnswersError, match="adopted but not available"):
        b.resolve(FN, [DD], ctx())


def test_answer_book_n2_a_convention_that_contradicts_a_spec_pin_is_refused_at_adoption() -> None:
    c = ctx(conventions=CONVS)
    b = Book()
    reg(b)
    adopt(b, SPEC_LF, c, known=NEWLINES)
    with pytest.raises(AnswersError, match="contradicts the spec pin") as e1:
        conv(b, "majority-of-references", "1", c, known=NEWLINES)
    with pytest.raises(AnswersError, match="contradicts the spec pin") as e2:
        conv(b, "py-dollar-anchor", "1", c, known=NEWLINES)
    assert "t1.trailing-lf@1" in str(e1.value)
    assert "t1.trailing-lf@1" in str(e2.value)
    with pytest.raises(AnswersError, match="not in the convention registry"):
        conv(b, "nobody", "0", c)
    assert [r["kind"] for r in b.records] == ["registration", "spec"]


def test_answer_book_n3_a_convention_adopted_before_the_spec_entry_loses_to_it() -> None:
    c = ctx(conventions=CONVS)
    b = Book()
    reg(b)
    conv(b, "majority-of-references", "1", c, known=NEWLINES)
    assert b.resolve(FN, [LF_SPLIT], c).pins[LF_SPLIT] is True  # the hazard: 10 of 12 accept
    adopt(b, SPEC_LF, c, known=NEWLINES)
    r = b.resolve(FN, [LF_SPLIT], c)
    assert r.pins[LF_SPLIT] is False
    assert any(cf["kind"] == "convention-vs-spec" for cf in r.conflicts)


# G: the class key


def test_answer_book_g1_a_class_answered_on_one_tree_covers_a_new_member_on_a_later_tree() -> None:
    c = ctx()
    b = Book()
    reg(b)
    klass(b, {WS_TRIM[0]: "accept"}, "accept", c, known=WS_TRIM[:5])
    later = WS_TRIM[10]
    r = b.resolve(FN, [later], c)
    assert (r.pins[later], r.prov[later]["source"]) == (True, "class")
    assert ck("k3@1", later, c) == ck("k3@1", later, ctx())


def test_answer_book_g2_a_leading_dot_answer_decides_neither_consecutive_dots_nor_plain() -> None:
    c = ctx()
    b = Book()
    reg(b)
    assert k3(LEAD) == k3(DD) == k3("user@example.com")  # K3 alone merges all three
    klass(b, {LEAD: "reject"}, "reject", c, known=[*LEAD_MEMBERS, *DD_MEMBERS, "user@example.com"])
    r = b.resolve(FN, [LEAD_MEMBERS[1], DD, "user@example.com"], c)
    assert r.pins.get(LEAD_MEMBERS[1]) is False
    assert DD not in r.pins
    assert "user@example.com" not in r.pins


def test_answer_book_g3_a_trim_is_fine_answer_does_not_make_a_space_valid() -> None:
    c = ctx()
    b = Book()
    reg(b)
    klass(b, {WS_TRIM[0]: "accept"}, "accept", c, known=WS_TRIM + WS_DEAD)
    r = b.resolve(FN, [WS_DEAD[0], " "], c)
    assert r.pins.get(" ") is not True
    assert r.pins.get(WS_DEAD[0]) is not True


def test_answer_book_g4_with_no_key_function_registered_answers_stay_exact() -> None:
    c = ctx()
    b = Book()
    reg(b, key_fn=None)
    pv = b.preview(FN, "accept", {WS_TRIM[0]: "accept"}, WS_TRIM, c)
    assert not pv.ok
    assert "no class key function" in pv.reasons[0]
    exact(b, WS_TRIM[0], "accept")
    assert b.to_ask(FN, WS_TRIM[:3], c, set()) == [canon(["exact", x]) for x in WS_TRIM[1:3]]


def test_answer_book_g5_a_redraw_that_splits_a_class_suspends_it() -> None:
    c = ctx()
    b = Book()
    reg(b)
    klass(b, {WS_TRIM[0]: "accept", WS_TRIM[1]: "accept"}, "accept", c, known=WS_TRIM)
    assert len(b.resolve(FN, WS_TRIM[:3], c).pins) == 3
    redraw = ctx(refs={**REFS, WS_TRIM[1]: "T" + "F" * 11}, reference_set_id="refs-redraw")
    r = b.resolve(FN, WS_TRIM[:3], redraw)
    assert r.pins == {WS_TRIM[0]: True, WS_TRIM[1]: True}
    assert [cf["kind"] for cf in r.conflicts] == ["class-split-by-references"]
    asked: set[str] = set()
    q = b.to_ask(FN, WS_TRIM, redraw, asked)
    assert len(q) == 1
    asked.update(q)
    assert b.to_ask(FN, WS_TRIM, redraw, asked) == []


# H: the preview gate


def test_answer_book_h1_the_gate_passes_shows_three_members_shortest_first_and_freezes() -> None:
    c = ctx()
    b = Book()
    reg(b)
    pv = b.preview(FN, "accept", {WS_TRIM[5]: "accept"}, WS_TRIM, c)
    assert pv.ok
    assert pv.members == len(WS_TRIM) == 17
    assert pv.others == shortest_first([x for x in WS_TRIM if x != WS_TRIM[5]])[:3]
    h = b.save_class(
        FN,
        "accept",
        {WS_TRIM[5]: "accept"},
        pv,
        confirmed_preview=True,
        by="eliza",
        at=T0,
        question="q",
        tree_hash=TREE,
        ask_point="verdict",
        run_id="run-1",
        ctx=c,
    )
    rec = b.records[-1]
    assert (rec["record_hash"], rec["class_hash"], rec["scope"]) == (
        h,
        frozen_hash(rec, FROZEN_CLASS),
        "class",
    )
    assert rec["preview_hash"] == sha(pv.frozen())


def test_answer_book_h2_anchors_from_two_classes_or_two_answers_in_one_class_fail_the_gate() -> (
    None
):
    c = ctx()
    b = Book()
    reg(b)
    pv = b.preview(FN, "accept", {WS_TRIM[0]: "accept", WS_DEAD[0]: "accept"}, WS_TRIM + WS_DEAD, c)
    assert not pv.ok
    assert any("does not cover" in x for x in pv.reasons)
    pv = b.preview(FN, "accept", {WS_TRIM[0]: "accept", WS_TRIM[1]: "reject"}, WS_TRIM, c)
    assert not pv.ok
    assert any("does not reproduce" in x for x in pv.reasons)


def test_answer_book_h3_a_class_answer_that_contradicts_a_user_or_spec_pin_fails_the_gate() -> None:
    c = ctx()
    b = Book()
    reg(b)
    exact(b, WS_TRIM[3], "reject")
    pv = b.preview(FN, "accept", {WS_TRIM[0]: "accept"}, WS_TRIM, c)
    assert not pv.ok
    assert pv.reasons == [f"contradicts the user pin on {WS_TRIM[3]!r}"]
    adopt(b, SPEC_LF, c, known=NEWLINES)
    pv = b.preview(FN, "accept", {LF_SPLIT: "accept"}, NEWLINES, c)
    assert not pv.ok
    assert all("contradicts the spec pin" in x for x in pv.reasons)


def test_answer_book_h4_without_a_panel_the_preview_shows_the_degenerate_members_first() -> None:
    c = ctx(panel=False)
    b = Book()
    reg(b, panel=False)
    known = [WS_TRIM[0], *reversed(WS_DEAD)]
    pv = b.preview(FN, "accept", {WS_TRIM[0]: "accept"}, known, c)
    assert pv.others == [" ", "\t", "  "]


def test_answer_book_h5_a_declined_preview_pins_the_answered_input_only() -> None:
    c = ctx()
    b = Book()
    reg(b)
    klass(b, {WS_TRIM[0]: "accept"}, "accept", c, known=WS_TRIM, confirmed=False)
    r = b.resolve(FN, WS_TRIM[:3], c)
    assert r.pins == {WS_TRIM[0]: True}
    assert r.prov[WS_TRIM[0]]["source"] == "user"


# I: "either"


def test_answer_book_i1_either_retires_its_members() -> None:
    c = ctx()
    b = Book()
    reg(b)
    assert either_at_plan(b, LEAD, LEAD_MEMBERS, c) == "either"
    r = b.resolve(FN, [*LEAD_MEMBERS, "user@example.com"], c)
    assert sorted(r.retired) == sorted(LEAD_MEMBERS)
    assert "user@example.com" not in r.retired
    assert "user@example.com" not in r.pins


def test_answer_book_i2_either_by_an_unregistered_user_or_at_an_unregistered_point_is_refused() -> (
    None
):
    c = ctx()
    b = Book()
    reg(b, either_by=())
    assert either_at_plan(b, LEAD, LEAD_MEMBERS, c) == "not-offered"
    pv = b.preview(FN, "either", {LEAD: "either"}, LEAD_MEMBERS, c)
    with pytest.raises(AnswersError, match="may not answer 'either'"):
        b.save_class(
            FN,
            "either",
            {LEAD: "either"},
            pv,
            confirmed_preview=True,
            by="eliza",
            at=T0,
            question="q",
            tree_hash="",
            ask_point="plan",
            run_id="run-1",
            ctx=c,
        )
    b2 = Book()
    reg(b2, either_at=("plan",))
    got = answer_either(
        b2,
        FN,
        DD,
        DD_MEMBERS,
        {DD: True},
        c,
        by="eliza",
        ask_point="verdict",
        stdin=io.StringIO("y\n"),
        stdout=io.StringIO(),
        run_id="r",
        tree_hash=TREE,
        at=T0,
    )
    assert got == "not-offered"


def test_answer_book_i3_either_over_the_routed_trees_miss_fires_the_second_confirmation() -> None:
    c = ctx()
    b = Book()
    reg(b)
    tree = {DD: True}  # the 9 routed wrong trees accept u..x@example.com
    out_ = io.StringIO()
    got = answer_either(
        b,
        FN,
        DD,
        DD_MEMBERS,
        tree,
        c,
        by="eliza",
        ask_point="verdict",
        stdin=io.StringIO(""),
        stdout=out_,
        run_id="run-1",
        tree_hash=TREE,
        at=T0,
    )
    text = out_.getvalue()
    assert "'u..x@example.com'" in text
    assert "5 of 12 independent implementations reject it" in text
    assert "13 input(s)" in text
    assert text.endswith("[y/N] ")
    assert got == "skipped"
    assert not [r for r in b.records if r["kind"] == "class"]
    assert DD not in b.resolve(FN, [DD], c).retired


def test_answer_book_i4_y_at_the_second_confirmation_saves_either_with_the_miss_shown() -> None:
    c = ctx()
    b = Book()
    reg(b)
    got = answer_either(
        b,
        FN,
        DD,
        DD_MEMBERS,
        {DD: True},
        c,
        by="eliza",
        ask_point="verdict",
        stdin=io.StringIO("y\n"),
        stdout=io.StringIO(),
        run_id="run-1",
        tree_hash=TREE,
        at=T0,
    )
    rec = b.records[-1]
    assert (got, rec["shown_misses"], rec["second_confirmation"]) == ("either", [DD], "confirmed")
    rec2 = body(rec)
    rec2["second_confirmation"] = ""
    rec2["class_hash"] = frozen_hash(rec2, FROZEN_CLASS)
    with pytest.raises(AnswersError, match="second confirmation"):
        b.append(rec2)


def test_answer_book_i5_at_plan_time_either_needs_no_second_confirmation() -> None:
    c = ctx()
    b = Book()
    reg(b)
    out_ = io.StringIO()
    got = answer_either(
        b,
        FN,
        DD,
        DD_MEMBERS,
        None,
        c,
        by="eliza",
        ask_point="plan",
        stdin=io.StringIO(""),
        stdout=out_,
        run_id="run-1",
        tree_hash="",
        at=T0,
    )
    assert (got, out_.getvalue()) == ("either", "")


# J: revocation


def test_answer_book_j1_a_revoked_class_answer_stops_pinning_and_its_record_remains() -> None:
    c = ctx()
    b = Book()
    reg(b)
    h, _ = klass(b, {WS_TRIM[0]: "accept"}, "accept", c, known=WS_TRIM)
    assert b.resolve(FN, [WS_TRIM[3]], c).pins == {WS_TRIM[3]: True}
    b.revoke(h, by="eliza", at=T0, reason="wrong answer")
    assert b.resolve(FN, [WS_TRIM[3]], c).pins == {}
    t = b.dumps()
    assert any(json.loads(line)["record_hash"] == h for line in t.splitlines())
    assert Book.loads(t).dumps() == t


def test_answer_book_j2_revoking_twice_a_references_pin_or_as_the_worker_is_refused() -> None:
    c = ctx()
    b = Book()
    reg(b)
    h, _ = klass(b, {WS_TRIM[0]: "accept"}, "accept", c, known=WS_TRIM)
    b.revoke(h, by="eliza", at=T0, reason="r")
    with pytest.raises(AnswersError, match="already revoked"):
        b.revoke(h, by="eliza", at=T0, reason="r")
    hr = exact(b, WS_TRIM[4], "reject", source="references", by="references")
    with pytest.raises(AnswersError, match="no such user"):
        b.revoke(hr, by="eliza", at=T0, reason="r")
    h2, _ = klass(b, {WS_TRIM[0]: "accept"}, "accept", c, known=WS_TRIM)
    with pytest.raises(AnswersError, match="only a user"):
        b.revoke(h2, by="worker", at=T0, reason="r")


def test_answer_book_j3_a_revoked_either_returns_its_members_to_checking() -> None:
    c = ctx()
    b = Book()
    reg(b)
    either_at_plan(b, LEAD, LEAD_MEMBERS, c)
    b.revoke(b.records[-1]["record_hash"], by="eliza", at=T0, reason="restore")
    r = b.resolve(FN, LEAD_MEMBERS, c)
    assert r.retired == []
    assert r.pins == {}


def test_answer_book_j4_a_revoked_class_is_asked_again_at_most_once_in_the_next_run() -> None:
    c = ctx()
    b = Book()
    reg(b)
    h, _ = klass(b, {WS_TRIM[0]: "accept"}, "accept", c, known=WS_TRIM)
    b.revoke(h, by="eliza", at=T0, reason="r")
    asked: set[str] = set()
    first = b.to_ask(FN, WS_TRIM, c, asked)
    assert len(first) == 1
    asked.update(first)
    assert b.to_ask(FN, WS_TRIM, c, asked) == []


# K: never ask twice


def test_answer_book_k1_a_class_answered_on_tree_1_is_not_asked_on_tree_2s_other_member() -> None:
    c = ctx()
    b = Book()
    reg(b)
    klass(b, {WS_TRIM[0]: "accept"}, "accept", c, known=WS_TRIM[:2])
    assert b.to_ask(FN, [WS_TRIM[9], WS_TRIM[12]], c, set()) == []


def test_answer_book_k2_an_unanswered_class_is_asked_once_per_run() -> None:
    c = ctx()
    b = Book()
    reg(b)
    asked: set[str] = set()
    q = b.to_ask(FN, WS_TRIM[:4] + DD_MEMBERS[:3], c, asked)
    assert len(q) == 2
    asked.update(q)
    assert b.to_ask(FN, WS_TRIM[4:8] + DD_MEMBERS[3:6], c, asked) == []


def test_answer_book_k3_a_declined_preview_still_marks_the_class_asked() -> None:
    c = ctx()
    b = Book()
    reg(b)
    klass(b, {WS_TRIM[0]: "accept"}, "accept", c, known=WS_TRIM, confirmed=False)
    assert b.to_ask(FN, WS_TRIM[1:5], c, set()) == []


# L: delivery


def test_answer_book_l1_the_class_most_references_contradict_goes_first_at_verdict_time() -> None:
    c = ctx()
    b = Book()
    reg(b)
    tree = {DD: True, LEAD: True, WS_TRIM[0]: True}  # a trimming tree that accepts both dot forms
    qs = []
    for name, members in (
        ("dots-consecutive", DD_MEMBERS),
        ("dots-edge", LEAD_MEMBERS),
        ("ws-trim", WS_TRIM),
    ):
        m = detected_misses(members, tree, c, b.resolve(FN, members, c))
        qs.append({"key": name, "members": len(members), "dissent": class_dissent(m)})
    assert [round(float(str(q["dissent"])), 3) for q in qs] == [0.417, 0.5, 1.0]
    assert [q["key"] for q in order_questions(qs)] == ["ws-trim", "dots-edge", "dots-consecutive"]
    plan = [dict(q, dissent=class_dissent([])) for q in qs]
    assert [q["key"] for q in order_questions(plan)] == ["dots-edge", "ws-trim", "dots-consecutive"]


def test_answer_book_l2_one_question_never_covers_the_ipv4_and_ipv6_literals() -> None:
    c = ctx()
    assert k3("user@[192.168.1.1]") == k3("user@[2001:db8::1]")
    gs = question_groups(["user@[192.168.1.1]", "user@[2001:db8::1]"], c, "k3@1")
    assert len(gs) == 2


# D (amended): pinned tests come from resolve


def _pytest_failures(tmp_path: Path, src: str, validator_src: str) -> int:
    """Run the rendered tests under pytest, the enforcing engine, against a validator module."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "validators.py").write_text(validator_src, encoding="utf-8")
    (tmp_path / "test_pins.py").write_text(src, encoding="utf-8")
    p = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-o",
            "addopts=",
            "test_pins.py",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    m = re.search(r"(\d+) failed", p.stdout)
    return int(m.group(1)) if m else 0


def test_answer_book_d1_spec_pins_tests_fail_a_dollar_anchored_validator_and_pass_fullmatch(
    tmp_path: Path,
) -> None:
    c = ctx()
    b = Book()
    reg(b)
    adopt(b, SPEC_PCT_CLASS, c, known=PCT)
    adopt(b, SPEC_LF, c, known=NEWLINES)
    either_at_plan(b, LEAD, LEAD_MEMBERS, c)
    r = b.resolve(FN, ["us%er@example.com", LF_ORACLE, LF_SPLIT, *LEAD_MEMBERS], c)
    src = render_pinned_tests(r, FN)
    assert not any(repr(x) in src for x in LEAD_MEMBERS)
    assert "t1.trailing-lf@1" in src
    dollar = (
        "import re\n"
        '_R = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\\.[A-Za-z0-9-]+)+$")\n'
        "def is_valid_email(s):\n    return _R.match(s) is not None\n"
    )
    full = dollar.replace("_R.match(s)", "_R.fullmatch(s)")
    assert _pytest_failures(tmp_path / "dollar", src, dollar) == 2
    assert _pytest_failures(tmp_path / "full", src, full) == 0


# Final (DECISIONS-2026-09-25): rows 3 and 6, L18


def test_answer_book_m7_the_adopted_pct_entry_pins_its_one_anchor_only() -> None:
    c = ctx()
    b = Book()
    reg(b)
    adopt(b, SPEC_PCT, c, known=ALL)
    r = b.resolve(FN, [*ALL, "us%er@example.com"], c)
    pct = [x for x in r.pins if r.prov[x].get("clause") == "t1.local-pct@1"]
    assert pct == ["us%er@example.com"]
    assert "%user@example.com" not in r.pins
    assert b.records[-1]["scope"] == "anchors"


def test_answer_book_n4_a_class_answer_contradicting_a_convention_pin_needs_an_override() -> None:
    c = ctx(conventions=CONVS)
    b = Book()
    reg(b)
    conv(b, "rfc5321-mailbox", "2008", c, known=ALL)
    pv = b.preview(FN, "accept", {DD: "accept"}, DD_MEMBERS, c)
    assert not pv.ok
    assert any("convention pin" in x for x in pv.reasons)
    pv = b.preview(FN, "accept", {DD: "accept"}, DD_MEMBERS, c, overrides=("rfc5321-mailbox@2008",))
    assert pv.ok


def test_answer_book_p1_authorities_carry_the_winning_source_and_a_cite_per_input() -> None:
    c = ctx(conventions=CONVS)
    b = Book()
    reg(b)
    adopt(b, SPEC_LF, c, known=ALL)
    conv(b, "rfc5321-mailbox", "2008", c, known=ALL)
    exact(b, LF_SPLIT, "accept")  # no override: spec must win
    exact(b, "user@example.com", "accept")
    klass(b, {LEAD: "either"}, "either", c, known=LEAD_MEMBERS)
    r = b.resolve(FN, [*ALL, LF_ORACLE, "user@example.com"], c)
    a = authorities(r, c, b, FN)
    assert isinstance(a, rule_d.Authorities)
    slots = ("user", "spec", "either", "convention", "reference_pins")
    for x in set(r.pins) | set(r.retired):
        assert sum(x in getattr(a, k) for k in slots) == 1, x
    assert a.spec[LF_SPLIT].answer == rule_d.Answer("value", "false")
    assert LF_SPLIT not in a.user
    assert a.user["user@example.com"].answer == rule_d.Answer("value", "true")
    assert LEAD in a.either
    assert (a.spec_id, a.convention_ids) == (PANEL_SHA, ("rfc5321-mailbox@2008",))
    assert DD in a.convention
    assert a.convention[DD].cite.startswith("rfc5321-mailbox@2008:")
    assert all(v.cite for k in ("user", "spec", "convention") for v in getattr(a, k).values())
    assert all(a.spec[x].cite.startswith("t1.trailing-lf@1:") for x in a.spec)
    b2 = Book()
    reg(b2)
    exact(b2, "x@y.io", "reject", source="references", by="references")
    a2 = authorities(b2.resolve(FN, ["x@y.io"], c), c, b2, FN)
    assert "x@y.io" not in a2.user
    assert a2.reference_pins["x@y.io"].cite.startswith("references-pin:")
    assert a2.convention_ids == ()
    # rule D itself accepts the object: each input resolves to exactly the slot emitted here
    for x in [LF_SPLIT, "user@example.com", LEAD, DD]:
        want = next(k for k in ("user", "spec", "either", "convention") if x in getattr(a, k))
        assert rule_d._authority(x, a)[0] == want, x


def test_answer_book_p2_overrides_and_anchor_scope_reach_rule_d_so_its_checks_pass() -> None:
    c = ctx(conventions=CONVS)
    b = Book()
    reg(b)
    adopt(b, SPEC_LF, c, known=ALL)
    conv(b, "rfc5321-mailbox", "2008", c, known=ALL)
    exact(b, LF_ORACLE, "accept", overrides=("t1.trailing-lf@1",))  # a confirmed, named override
    klass(b, {LEAD: "either"}, "either", c, known=LEAD_MEMBERS)  # over a convention pin: anchors
    r = b.resolve(FN, [LF_ORACLE, LEAD], c)
    a = authorities(r, c, b, FN)
    assert a.user[LF_ORACLE].overrides.startswith("t1.trailing-lf@1:")
    assert a.either[LEAD].anchor is True
    assert a.either[LEAD].cite.startswith("either:")
    # rule D checks a user label against a clause in the same object; hand it both
    conv_cite = f"rfc5321-mailbox@2008:{b.records[2]['record_hash']}"
    both = rule_d.Authorities(
        user=dict(a.user),
        spec={
            LF_ORACLE: rule_d.Label(rule_d.Answer("value", "false"), a.user[LF_ORACLE].overrides)
        },
        spec_id=PANEL_SHA,
        either=dict(a.either),
        convention={LEAD: rule_d.Label(rule_d.Answer("value", "false"), conv_cite)},
        convention_ids=("rfc5321-mailbox@2008",),
    )
    assert rule_d._authority(LF_ORACLE, both)[0] == "user"
    assert rule_d._authority(LEAD, both)[0] == "either"
    res = rule_d.consensus_verdict(
        FN,
        [LF_ORACLE, LEAD],
        {LF_ORACLE: rule_d.Answer("value", "true"), LEAD: rule_d.Answer("value", "false")},
        [],
        authorities=both,
        classes={},
    )
    assert (res.verdict, res.either) == ("no-reference", (LEAD,))


# X: S3-5 prompts


def test_answer_book_x1_e_is_not_an_answer_unless_either_is_offered() -> None:
    o = io.StringIO()
    assert (
        ask_question(DD, offer_either=False, stdin=io.StringIO("e\ne\ne\n"), stdout=o) == "skipped"
    )
    assert "[y/n]" in o.getvalue()
    assert "[y/n/e]" not in o.getvalue()
    assert (
        ask_question(DD, offer_either=True, stdin=io.StringIO("e\n"), stdout=io.StringIO())
        == "either"
    )
    assert (
        ask_question(DD, offer_either=True, stdin=io.StringIO(""), stdout=io.StringIO())
        == "skipped"
    )
    assert (
        ask_question(DD, offer_either=True, stdin=io.StringIO("yes\n"), stdout=io.StringIO())
        == "yes"
    )
    o = io.StringIO()
    assert ask_question(DD, offer_either=True, stdin=io.StringIO("maybe\nno\n"), stdout=o) == "no"
    assert "or e for either" in o.getvalue()


def test_answer_book_x2_the_second_confirmation_defaults_to_no_on_enter_eof_and_garbage() -> None:
    m: list[dict[str, object]] = [{"input": DD, "tree": True, "dissent": 5, "of": 12}]
    for s in ("", "\n", "maybe\nmaybe\nmaybe\n"):
        assert confirm_either(m, 13, DD, stdin=io.StringIO(s), stdout=io.StringIO()) is False, repr(
            s
        )
    assert confirm_either(m, 13, DD, stdin=io.StringIO("y\n"), stdout=io.StringIO()) is True
    # the text over a pinned miss, a rejecting tree, and several misses
    pinned: list[dict[str, object]] = [
        {"input": LEAD, "tree": False, "by": "user"},
        {"input": DD, "tree": False, "dissent": 7, "of": 12},
    ]
    text = ab.either_text(pinned, 28, LEAD)
    assert "Your code rejects '.u@example.com'; the user answer is to accept it (+1 more)." in text


def test_answer_book_x3_the_spec_override_prompt_names_the_clause_and_defaults_to_no() -> None:
    o = io.StringIO()
    assert confirm_override(LF_ORACLE, SPEC_LF, stdin=io.StringIO(""), stdout=o) is False
    assert "t1.trailing-lf@1" in o.getvalue()
    assert "must be rejected" in o.getvalue()
    assert (
        confirm_override(LF_ORACLE, SPEC_LF, stdin=io.StringIO("y\n"), stdout=io.StringIO()) is True
    )
    o = io.StringIO()
    assert (
        confirm_override("us%er@example.com", SPEC_PCT, stdin=io.StringIO("y\n"), stdout=o) is True
    )
    assert "must be accepted" in o.getvalue()


# Edge branches the pairs leave dark (coverage, not contract): each names the branch it reaches.


def test_answer_book_edges_quoted_local_part_panel_mismatch_plain_user_cite_and_agreeing_pin() -> (
    None
):
    assert k3('"a b"@example.com') == ("core", ('"',), False, False, False)  # quoted local part
    c = ctx()
    b = Book()
    reg(b)
    adopt(b, SPEC_LF, c, known=NEWLINES)
    other = Ctx(REFS, panel=votes, panel_sha="f" * 64, _votes=dict(R2VOTES))
    with pytest.raises(AnswersError, match="not the panel in use"):
        b.resolve(FN, [LF_SPLIT], other)
    # a user answer with an override that names no live clause carries an empty override cite
    exact(b, "user@example.com", "accept", overrides=("nothing@1",))
    a = authorities(b.resolve(FN, ["user@example.com"], c), c, b, FN)
    assert a.user["user@example.com"].overrides == ""
    # detected_misses: an agreeing pin is not a miss, a disagreeing one is a miss "by" its source,
    # a member every reference agrees with is not a miss, and a member the tree lacks is skipped
    exact(b, DD, "reject")
    exact(b, WS_TRIM[0], "accept")
    plain = next(x for x in ALL if REFS[x] == "T" * 12 and x != "user@example.com")
    r = b.resolve(FN, [DD, LEAD, WS_TRIM[0], plain], c)
    tree = {DD: False, LEAD: True, WS_TRIM[0]: False, plain: True}
    misses = detected_misses([DD, LEAD, WS_TRIM[0], plain, WS_TRIM[1]], tree, c, r)
    assert sorted((m["input"], m.get("by"), m.get("dissent")) for m in misses) == sorted(
        [(WS_TRIM[0], "user", None), (LEAD, None, 6)]
    )
