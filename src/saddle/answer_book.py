"""The answer book (S3-4 final, 2026-09-25): what the user, the spec and a convention decided.

Record kinds are `registration`, `exact` (source `user` or `references`),
`class`, `spec`, `convention` and `revoke`. The book is append-only JSON
lines in a sha256 hash chain (contract C); every entry carries provenance
(E); `resolve` applies the precedence exact user > class user > spec >
convention > references pin once (F), so `authorities` hands rule D one
label per input in the slot that won (O). A class answer reaches inputs
the user never saw only through the preview gate (H), and "either" is
scope-narrowed: a registered answerer, a second confirmation over a
detected miss, and disclosure (I).

The module is pure in the layering sense: the panel, the sealed references'
answers and the convention registry are passed in as `Ctx`, and nothing
here imports `evidence` or `runner`. Prompts take their streams as
arguments (`ux.ask_confirm`). The key-function registry `KEY_FNS` is saddle
source, never written by the model.

The class key has three parts: the key function's
value, the sealed references' signature and the panel votes. K3 alone
merges `u..x@example.com` with 578 Fix-8 inputs, 369 of them valid by every
grammar; without the panel votes a "trim is fine" answer pins `' '`.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Final

from saddle import rule_d
from saddle.ux import ask_confirm

RESERVED: Final = frozenset({"", "spec", "convention", "references", "worker"})
ASK_POINTS: Final = ("plan", "verdict", "decide")

Record = dict[str, Any]


class AnswersError(Exception):
    """A record the book refuses, or a file whose chain is broken."""


def canon(o: object) -> str:
    return json.dumps(o, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def sha(o: object) -> str:
    return hashlib.sha256(canon(o).encode()).hexdigest()


# ------------------------------------------------------------------ key functions (a registry)


def _cc(c: str) -> str:
    cat = unicodedata.category(c)[0]
    return "ZC" if cat in ("Z", "C") else cat


def k3(s: str) -> tuple[object, ...]:
    """The K3 shape key, copied from the calibration tool that defined it."""
    if s and (_cc(s[0]) == "ZC" or _cc(s[-1]) == "ZC"):
        return ("ws",)
    if "@" in s:
        local, domain = s.rsplit("@", 1)
    else:
        local, domain = s, ""
    unusual: tuple[str, ...]
    if local.startswith('"') and local.endswith('"'):
        unusual = ('"',)
    else:
        unusual = tuple(sorted({c for c in local if not c.isalnum() and c not in "._+-"}))
    ip = domain.startswith("[")
    single = (not ip) and "." not in domain
    domodd = (not ip) and any(not c.isalnum() and c not in ".-" for c in domain)
    return ("core", unusual, single, ip, domodd)


def trailing_lf(s: str) -> bool:
    """`trailing-lf@1`: the newline clause's own key, so its class is exactly the clause (M)."""
    return s.endswith("\n")


KEY_FNS: Final[Mapping[str, Callable[[str], object]]] = {"k3@1": k3, "trailing-lf@1": trailing_lf}

Convention = Callable[[str], bool | None]
Panel = Callable[[str], str]


# ------------------------------------------------------------------ what the S3-6 seam knows


@dataclass
class Ctx:
    """The sealed references' answers per input (a T/F string in the frozen order), the panel
    (a function from input to its "T,F,..." votes; `None` when the task registers none, in which
    case every label is "none"), the panel's sha256, and the adoptable conventions."""

    refs: Mapping[str, str]
    panel: Panel | None = None
    panel_sha: str = ""
    reference_set_id: str = "refs-12"
    conventions: Mapping[str, Convention] = field(default_factory=dict)
    _votes: dict[str, str] = field(default_factory=dict)

    def signature(self, s: str) -> list[str]:
        v = self.refs.get(s)
        if v is None:
            return ["unsealed"]
        return ["unanimous", v[0]] if len(set(v)) == 1 else ["split", v]

    def panel_label(self, s: str) -> str:
        if self.panel is None:
            return "none"
        if s not in self._votes:
            self._votes[s] = self.panel(s)
        return self._votes[s]


def class_key(fn: str, s: str, ctx: Ctx) -> list[object]:
    """A user class entry's key: the shape, the references' signature, the panel's votes."""
    return [fn, repr(KEY_FNS[fn](s)), ctx.signature(s), ctx.panel_label(s)]


def spec_key(fn: str, s: str, ctx: Ctx) -> list[object]:
    """A spec entry's key: no reference signature (the spec overrules the references)."""
    return [fn, repr(KEY_FNS[fn](s)), ctx.panel_label(s)]


def ck(fn: str, s: str, ctx: Ctx) -> str:
    return canon(class_key(fn, s, ctx))


def sk(fn: str, s: str, ctx: Ctx) -> str:
    return canon(spec_key(fn, s, ctx))


def shortest_first(xs: Iterable[str]) -> list[str]:
    return sorted(xs, key=lambda s: (len(s), repr(s)))


# ------------------------------------------------------------------ records

PROV: Final = ("callable", "by", "at", "question_hash", "tree_hash", "run_id")
REQUIRED: Final[Mapping[str, tuple[str, ...]]] = {
    "registration": (
        "callable",
        "by",
        "at",
        "key_fn",
        "either_by",
        "either_at",
        "panel_id",
        "panel_sha",
    ),
    "spec": (
        *PROV,
        "adopted_by",
        "clause_id",
        "clause",
        "sources",
        "answer",
        "key_fn",
        "anchors",
        "panel_votes",
        "panel_sha",
        "scope",
        "decision_ref",
        "entry_hash",
    ),
    "convention": (*PROV, "adopted_by", "conv_id", "version", "conv_sha", "clause", "sources"),
    "exact": (*PROV, "input", "answer", "source", "ask_point"),
    "class": (
        *PROV,
        "key",
        "anchors",
        "answer",
        "scope",
        "source",
        "ask_point",
        "reference_set_id",
        "panel_sha",
        "class_hash",
        "preview_hash",
        "shown_misses",
        "second_confirmation",
        "overrides",
    ),
    "revoke": ("target", "by", "at", "reason"),
}
DEFAULT_REG: Final[Record] = {
    "key_fn": None,
    "either_by": [],
    "either_at": ["plan", "verdict"],
    "panel_id": None,
    "panel_sha": None,
}
FROZEN_CLASS: Final = (
    "callable",
    "key",
    "anchors",
    "answer",
    "scope",
    "reference_set_id",
    "panel_sha",
    "overrides",
)
FROZEN_SPEC: Final = (
    "callable",
    "clause_id",
    "answer",
    "key_fn",
    "anchors",
    "panel_votes",
    "panel_sha",
    "scope",
)


def frozen_hash(rec: Record, fields: Sequence[str]) -> str:
    return sha({k: rec[k] for k in fields})


@dataclass
class Resolution:
    """`resolve`'s output: pins (input -> accepted), per-input provenance, retired inputs
    ("either"), and the conflicts the user is told about (F)."""

    pins: dict[str, bool] = field(default_factory=dict)
    prov: dict[str, Record] = field(default_factory=dict)
    retired: list[str] = field(default_factory=list)
    conflicts: list[Record] = field(default_factory=list)


@dataclass
class Preview:
    """The preview gate's report (H, M): `ok` when every check holds, else `reasons`."""

    ok: bool
    reasons: list[str]
    members: int
    others: list[str]
    key: list[object] | None = None
    user_conflicts: list[str] = field(default_factory=list)

    def frozen(self) -> Record:
        return {
            "ok": self.ok,
            "reasons": self.reasons,
            "members": self.members,
            "others": self.others,
            "key": self.key,
            "user_conflicts": self.user_conflicts,
        }


SpecEntry = tuple[Record, set[str]]


class Book:
    """An append-only, hash-chained answer book (contracts A, C, E, F, G, H, J, K, M, N)."""

    def __init__(self) -> None:
        self.records: list[Record] = []

    # --- contract C: append-only, hash-chained, deterministic bytes
    def head(self) -> str:
        return str(self.records[-1]["record_hash"]) if self.records else ""

    def dumps(self) -> str:
        return "".join(canon(r) + "\n" for r in self.records)

    @classmethod
    def loads(cls, text: str, path: str = "answers.jsonl") -> Book:
        b, prev = cls(), ""
        for i, line in enumerate(text.splitlines(), 1):
            try:
                r = json.loads(line)
            except ValueError as e:
                msg = f"{path}: record {i}: not JSON ({e})"
                raise AnswersError(msg) from None
            if not isinstance(r, dict):
                msg = f"{path}: record {i}: not a JSON object"
                raise AnswersError(msg)
            body = {k: v for k, v in r.items() if k != "record_hash"}
            if r.get("prev") != prev or sha(body) != r.get("record_hash"):
                msg = f"{path}: record {i}: chain broken (history was rewritten)"
                raise AnswersError(msg)
            b._validate(r)
            b.records.append(r)
            prev = str(r["record_hash"])
        return b

    @classmethod
    def load(cls, path: Path) -> Book:
        """A missing file is an empty book; a malformed or rewritten one raises."""
        if not path.exists():
            return cls()
        return cls.loads(path.read_text(encoding="utf-8"), str(path))

    def dump(self, path: Path) -> None:
        path.write_text(self.dumps(), encoding="utf-8")

    def append(self, rec: Record) -> str:
        rec = dict(rec, prev=self.head())
        self._validate(rec)
        rec["record_hash"] = sha(rec)
        self.records.append(rec)
        return str(rec["record_hash"])

    # --- contract E: provenance, and who may write what
    def _validate(self, r: Record) -> None:
        kind = r.get("kind")
        if not isinstance(kind, str) or kind not in REQUIRED:
            msg = f"unknown kind {kind!r}"
            raise AnswersError(msg)
        missing = [f for f in REQUIRED[kind] if f not in r]
        if missing:
            msg = f"{kind} entry lacks {missing}"
            raise AnswersError(msg)
        self._check_writer(r)
        if kind in ("exact", "class") and r["source"] == "user":
            if len(r["question_hash"]) != 64:
                msg = "question_hash must be the sha256 of the question shown"
                raise AnswersError(msg)
            if r["ask_point"] not in ASK_POINTS:
                msg = f"ask_point {r['ask_point']!r} is not one of {ASK_POINTS}"
                raise AnswersError(msg)
            if r["ask_point"] != "plan" and not r["tree_hash"]:
                msg = "tree_hash is required once a tree exists"
                raise AnswersError(msg)
            if not r["run_id"]:
                msg = "run_id is required"
                raise AnswersError(msg)
        if kind == "class":
            self._check_class(r)
        if kind == "spec":
            self._check_spec(r)
        if kind == "revoke":
            self._check_revoke(r)

    def _check_class(self, r: Record) -> None:
        if r["class_hash"] != frozen_hash(r, FROZEN_CLASS):
            msg = "class_hash does not match the entry's frozen fields"
            raise AnswersError(msg)
        if r["answer"] == "either":
            reg = self.registration(r["callable"])
            if r["by"] not in reg["either_by"] or r["ask_point"] not in reg["either_at"]:
                msg = f"{r['by']!r} may not answer 'either' at {r['ask_point']}"
                raise AnswersError(msg)
            if r["shown_misses"] and r["second_confirmation"] != "confirmed":
                msg = "'either' over a detected miss needs the second confirmation"
                raise AnswersError(msg)

    def _check_revoke(self, r: Record) -> None:
        targets = {x["record_hash"]: x for x in self.records}
        t = targets.get(r["target"])
        if t is None or t["kind"] in ("revoke", "registration") or t.get("source") == "references":
            msg = "revoke: no such user, spec or convention entry"
            raise AnswersError(msg)
        if any(x["kind"] == "revoke" and x["target"] == r["target"] for x in self.records):
            msg = "revoke: already revoked"
            raise AnswersError(msg)

    def _check_writer(self, r: Record) -> None:
        kind = r["kind"]
        if kind == "exact" and r["source"] == "references":
            if r["by"] != "references" or r["question_hash"] != "":
                msg = "a references pin is written by saddle and answers no question"
                raise AnswersError(msg)
            return
        if kind in ("spec", "convention"):
            if r["by"] != kind:
                msg = f"a {kind} entry is recorded as by={kind!r}"
                raise AnswersError(msg)
            if r["adopted_by"] in RESERVED:
                msg = f"only a user may adopt a {kind} entry"
                raise AnswersError(msg)
            if r["question_hash"] != "":
                msg = f"a {kind} entry is never asked"
                raise AnswersError(msg)
            return
        if kind in ("exact", "class") and r["source"] != "user":
            msg = f"{kind} entry source {r['source']!r}"
            raise AnswersError(msg)
        if r["by"] in RESERVED:
            msg = f"{kind} entry by {r['by']!r}: only a user may write it"
            raise AnswersError(msg)

    def _check_spec(self, r: Record) -> None:
        reg = self.registration(r["callable"])
        if not reg["panel_sha"]:
            msg = "a spec entry needs a registered panel"
            raise AnswersError(msg)
        if r["panel_sha"] != reg["panel_sha"]:
            msg = "the spec entry's panel is not the registered panel"
            raise AnswersError(msg)
        if r["key_fn"] not in KEY_FNS or r["answer"] not in ("accept", "reject"):
            msg = "spec entry: unknown key function or answer"
            raise AnswersError(msg)
        want = "T" if r["answer"] == "accept" else "F"
        for a in r["anchors"]:
            v = r["panel_votes"].get(a, "")
            if not v or len(set(v.split(","))) != 1:
                msg = f"the panel does not decide {a!r} ({v or 'no votes'}): ask the user"
                raise AnswersError(msg)
            if v[0] != want:
                msg = f"the panel contradicts {r['answer']} on {a!r} ({v})"
                raise AnswersError(msg)
        if r["entry_hash"] != frozen_hash(r, FROZEN_SPEC):
            msg = "entry_hash does not match the entry's frozen fields"
            raise AnswersError(msg)

    # --- live view
    def _live(self, callable_: str) -> list[Record]:
        revoked = {r["target"] for r in self.records if r["kind"] == "revoke"}
        return [
            r
            for r in self.records
            if r["record_hash"] not in revoked
            and r["kind"] != "revoke"
            and r.get("callable") == callable_
        ]

    def registration(self, callable_: str) -> Record:
        regs = [r for r in self._live(callable_) if r["kind"] == "registration"]
        return regs[-1] if regs else DEFAULT_REG

    def _class_keys(self, callable_: str, ctx: Ctx) -> tuple[dict[str, Record], list[Record]]:
        """Anchor-relative membership: each live class entry's key recomputed from its anchors.
        An entry whose anchors no longer share one key (a reference redraw split them) is
        suspended: it pins its anchors only, and its class is asked again (once per run)."""
        out: dict[str, Record] = {}
        split: list[Record] = []
        for c in (r for r in self._live(callable_) if r["kind"] == "class"):
            keys = {ck(c["key"][0], a, ctx) for a in c["anchors"]}
            if len(keys) != 1:
                split.append(c)
                continue
            out[keys.pop()] = c
        return out, split

    @staticmethod
    def _specs(live: Sequence[Record], ctx: Ctx) -> list[SpecEntry]:
        out: list[SpecEntry] = []
        for e in (r for r in live if r["kind"] == "spec"):
            if e["panel_sha"] != ctx.panel_sha:
                msg = f"spec entry {e['clause_id']}: its panel is not the panel in use"
                raise AnswersError(msg)
            out.append((e, {sk(e["key_fn"], a, ctx) for a in e["anchors"]}))
        return out

    @staticmethod
    def _spec_for(specs: Sequence[SpecEntry], x: str, ctx: Ctx) -> Record | None:
        for e, keys in specs:
            if x in e["anchors"] or (e["scope"] == "class" and sk(e["key_fn"], x, ctx) in keys):
                return e
        return None

    @staticmethod
    def _conv_answer(
        convs: Sequence[Record], x: str, ctx: Ctx
    ) -> tuple[Record | None, bool | None]:
        for cv in convs:
            cid = f"{cv['conv_id']}@{cv['version']}"
            f = ctx.conventions.get(cid)
            if f is None:
                msg = f"convention {cid} is adopted but not available"
                raise AnswersError(msg)
            a = f(x)
            if a is not None:
                return cv, a
        return None, None

    # --- contract F: precedence (exact user > class user > spec > convention > references)
    def resolve(self, callable_: str, inputs: Iterable[str], ctx: Ctx) -> Resolution:
        res = Resolution()
        live = self._live(callable_)
        reg = self.registration(callable_)
        exact_user = {r["input"]: r for r in live if r["kind"] == "exact" and r["source"] == "user"}
        exact_refs = {
            r["input"]: r for r in live if r["kind"] == "exact" and r["source"] == "references"
        }
        specs = self._specs(live, ctx)
        convs = [r for r in live if r["kind"] == "convention"]
        classes, split = self._class_keys(callable_, ctx)
        for c in split:
            res.conflicts.append({"kind": "class-split-by-references", "entry": c["record_hash"]})
        anchors_only = {
            a: c for c in classes.values() if c["scope"] == "anchors" for a in c["anchors"]
        }
        for c in split:
            for a in c["anchors"]:
                anchors_only.setdefault(a, c)
        for x in dict.fromkeys(inputs):
            user: Record | None = exact_user.get(x) or anchors_only.get(x)
            if user is None and reg["key_fn"]:
                hit = classes.get(ck(reg["key_fn"], x, ctx))
                if hit is not None and hit["scope"] == "class":
                    user = hit
            sp = self._spec_for(specs, x, ctx)
            if sp is not None:
                self._note_spec_conflicts(res, x, sp, convs, ctx)
            if user is not None:
                self._pin_user(res, x, user, sp)
                continue
            if sp is not None:
                self._pin_spec(res, x, sp)
                continue
            cv, a = self._conv_answer(convs, x, ctx)
            if cv is not None:
                res.pins[x] = bool(a)
                res.prov[x] = {
                    "source": "convention",
                    "by": "convention",
                    "entry": cv["record_hash"],
                    "clause": f"{cv['conv_id']}@{cv['version']}",
                }
                continue
            er = exact_refs.get(x)
            if er is not None:
                res.pins[x] = er["answer"] == "accept"
                res.prov[x] = {
                    "source": "references",
                    "by": "references",
                    "entry": er["record_hash"],
                }
        return res

    @staticmethod
    def _pin_user(res: Resolution, x: str, user: Record, sp: Record | None) -> None:
        ans = user["answer"]
        disagrees = sp is not None and (
            ans == "either" or (ans == "accept") != (sp["answer"] == "accept")
        )
        if sp is not None and disagrees and sp["clause_id"] not in user.get("overrides", []):
            res.conflicts.append(
                {
                    "kind": "user-vs-spec",
                    "input": x,
                    "clause": sp["clause_id"],
                    "entry": user["record_hash"],
                }
            )
            Book._pin_spec(res, x, sp)
            return
        if ans == "either":
            res.retired.append(x)
            res.prov[x] = {"source": "either", "by": user["by"], "entry": user["record_hash"]}
            return
        res.pins[x] = ans == "accept"
        exact = user["kind"] == "exact" or x in user["anchors"]
        res.prov[x] = {
            "source": "user" if exact else "class",
            "by": user["by"],
            "entry": user["record_hash"],
        }

    @staticmethod
    def _pin_spec(res: Resolution, x: str, sp: Record) -> None:
        res.pins[x] = sp["answer"] == "accept"
        res.prov[x] = {
            "source": "spec",
            "by": "spec",
            "clause": sp["clause_id"],
            "entry": sp["record_hash"],
        }

    def _note_spec_conflicts(
        self, res: Resolution, x: str, sp: Record, convs: Sequence[Record], ctx: Ctx
    ) -> None:
        want = sp["answer"] == "accept"
        v = ctx.refs.get(x)
        if v:
            dissent = sum(1 for a in v if (a == "T") != want)
            if dissent:
                res.conflicts.append(
                    {
                        "kind": "references-vs-spec",
                        "input": x,
                        "clause": sp["clause_id"],
                        "dissent": dissent,
                        "of": len(v),
                    }
                )
        cv, a = self._conv_answer(convs, x, ctx)
        if cv is not None and a != want:
            res.conflicts.append(
                {
                    "kind": "convention-vs-spec",
                    "input": x,
                    "clause": sp["clause_id"],
                    "convention": f"{cv['conv_id']}@{cv['version']}",
                }
            )

    def _spec_anchors(self, callable_: str) -> list[str]:
        return [a for r in self._live(callable_) if r["kind"] == "spec" for a in r["anchors"]]

    # --- contract M: adopting a spec entry
    def preview_spec(
        self,
        callable_: str,
        *,
        key_fn: str,
        answer: str,
        anchors: Sequence[str],
        scope: str,
        known: Iterable[str],
        ctx: Ctx,
    ) -> Preview:
        reg = self.registration(callable_)
        reasons = []
        if not reg["panel_sha"] or reg["panel_sha"] != ctx.panel_sha:
            reasons.append("no registered panel, or not the panel in use")
        keys = {sk(key_fn, a, ctx) for a in anchors}
        if scope == "class" and len(keys) != 1:
            reasons.append("the anchors are not one class")
        want = "T,T,T,T" if answer == "accept" else "F,F,F,F"
        for a in anchors:
            if ctx.panel_label(a) != want:
                reasons.append(f"the panel votes {ctx.panel_label(a)} on {a!r}")
        pool = list(dict.fromkeys([*known, *anchors]))
        members = [
            x for x in pool if x in anchors or (scope == "class" and sk(key_fn, x, ctx) in keys)
        ]
        cur = self.resolve(callable_, members, ctx)
        told = [
            x
            for x in members
            if cur.prov.get(x, {}).get("source") in ("user", "class", "either")
            and (x in cur.retired or cur.pins[x] != (answer == "accept"))
        ]
        others = shortest_first([x for x in members if x not in anchors])[:3]
        return Preview(not reasons, reasons, len(members), others, user_conflicts=told)

    def adopt_spec(
        self,
        callable_: str,
        spec: Record,
        *,
        known: Iterable[str],
        ctx: Ctx,
        adopted_by: str,
        at: str,
        run_id: str,
        decision_ref: str,
        confirmed_preview: bool = True,
    ) -> tuple[str, Preview]:
        pv = self.preview_spec(
            callable_,
            key_fn=spec["key_fn"],
            answer=spec["answer"],
            anchors=spec["anchors"],
            scope=spec["scope"],
            known=known,
            ctx=ctx,
        )
        if not pv.ok:
            msg = "spec preview failed: " + "; ".join(pv.reasons)
            raise AnswersError(msg)
        rec: Record = {
            "kind": "spec",
            "callable": callable_,
            "by": "spec",
            "adopted_by": adopted_by,
            "at": at,
            "question_hash": "",
            "tree_hash": "",
            "run_id": run_id,
            "decision_ref": decision_ref,
            "clause_id": spec["clause_id"],
            "clause": spec["clause"],
            "sources": spec["sources"],
            "answer": spec["answer"],
            "key_fn": spec["key_fn"],
            "anchors": sorted(spec["anchors"]),
            "panel_votes": {a: ctx.panel_label(a) for a in spec["anchors"]},
            "panel_sha": ctx.panel_sha,
            "scope": spec["scope"] if confirmed_preview else "anchors",
        }
        rec["entry_hash"] = frozen_hash(rec, FROZEN_SPEC)
        return self.append(rec), pv

    # --- contract N: adopting a convention
    def adopt_convention(
        self,
        callable_: str,
        *,
        conv_id: str,
        version: str,
        conv_sha: str,
        clause: str,
        sources: Sequence[str],
        known: Iterable[str],
        ctx: Ctx,
        adopted_by: str,
        at: str,
        run_id: str,
    ) -> tuple[str, list[str]]:
        f = ctx.conventions.get(f"{conv_id}@{version}")
        if f is None:
            msg = f"{conv_id}@{version} is not in the convention registry"
            raise AnswersError(msg)
        pool = list(dict.fromkeys([*known, *self._spec_anchors(callable_)]))
        cur = self.resolve(callable_, pool, ctx)
        contra = [
            x
            for x in shortest_first(pool)
            if cur.prov.get(x, {}).get("source") == "spec"
            and f(x) is not None
            and f(x) != cur.pins[x]
        ]
        if contra:
            msg = (
                f"{conv_id}@{version} contradicts the spec pin on {contra[0]!r} "
                f"({cur.prov[contra[0]]['clause']}) and {len(contra) - 1} more"
            )
            raise AnswersError(msg)
        told = [
            x
            for x in pool
            if cur.prov.get(x, {}).get("source") in ("user", "class")
            and f(x) is not None
            and f(x) != cur.pins[x]
        ]
        h = self.append(
            {
                "kind": "convention",
                "callable": callable_,
                "by": "convention",
                "adopted_by": adopted_by,
                "at": at,
                "question_hash": "",
                "tree_hash": "",
                "run_id": run_id,
                "conv_id": conv_id,
                "version": version,
                "conv_sha": conv_sha,
                "clause": clause,
                "sources": list(sources),
            }
        )
        return h, told

    # --- contract H: the preview gate for a user class answer
    def preview(
        self,
        callable_: str,
        answer: str,
        anchor_answers: Mapping[str, str],
        known: Iterable[str],
        ctx: Ctx,
        overrides: Sequence[str] = (),
    ) -> Preview:
        reg = self.registration(callable_)
        if not reg["key_fn"]:
            return Preview(False, ["no class key function is registered for this task"], 0, [])
        anchors = list(anchor_answers)
        key = ck(reg["key_fn"], anchors[0], ctx)
        reasons = []
        for a in anchors:
            if ck(reg["key_fn"], a, ctx) != key:
                reasons.append(f"does not cover {a!r}")
        for a, given in anchor_answers.items():
            if given != answer:
                reasons.append(f"does not reproduce the answer given for {a!r}")
        pool = list(dict.fromkeys([*known, *self._spec_anchors(callable_)]))
        members = [x for x in pool if ck(reg["key_fn"], x, ctx) == key]
        cur = self.resolve(callable_, members, ctx)
        for x in members:
            src = cur.prov.get(x, {}).get("source")
            if src in ("user", "class", "spec", "convention"):
                same = answer != "either" and cur.pins[x] == (answer == "accept")
                overridden = src in ("spec", "convention") and cur.prov[x]["clause"] in overrides
                if not same and not overridden:
                    reasons.append(f"contradicts the {src} pin on {x!r}")
        others = shortest_first([x for x in members if x not in anchor_answers])[:3]
        key_obj: list[object] = json.loads(key)
        return Preview(not reasons, reasons, len(members), others, key=key_obj)

    def save_class(
        self,
        callable_: str,
        answer: str,
        anchor_answers: Mapping[str, str],
        pv: Preview,
        *,
        confirmed_preview: bool,
        by: str,
        at: str,
        question: str,
        tree_hash: str,
        ask_point: str,
        run_id: str,
        ctx: Ctx,
        shown_misses: Sequence[str] = (),
        second_confirmation: str = "",
        overrides: Sequence[str] = (),
    ) -> str:
        scope = "class" if (pv.ok and confirmed_preview) else "anchors"
        reg = self.registration(callable_)
        rec: Record = {
            "kind": "class",
            "callable": callable_,
            "key": class_key(reg["key_fn"], next(iter(anchor_answers)), ctx),
            "anchors": sorted(anchor_answers),
            "answer": answer,
            "scope": scope,
            "source": "user",
            "by": by,
            "at": at,
            "question_hash": hashlib.sha256(question.encode()).hexdigest(),
            "tree_hash": tree_hash,
            "ask_point": ask_point,
            "run_id": run_id,
            "reference_set_id": ctx.reference_set_id,
            "panel_sha": ctx.panel_sha,
            "preview_hash": sha(pv.frozen()),
            "shown_misses": list(shown_misses),
            "second_confirmation": second_confirmation,
            "overrides": list(overrides),
        }
        rec["class_hash"] = frozen_hash(rec, FROZEN_CLASS)
        return self.append(rec)

    # --- contract J
    def revoke(self, target: str, *, by: str, at: str, reason: str) -> str:
        return self.append(
            {"kind": "revoke", "target": target, "by": by, "at": at, "reason": reason}
        )

    # --- contract K: never ask twice
    def to_ask(
        self, callable_: str, candidates: Sequence[str], ctx: Ctx, asked_this_run: set[str]
    ) -> list[str]:
        reg = self.registration(callable_)
        res = self.resolve(callable_, candidates, ctx)
        live_keys, _ = self._class_keys(callable_, ctx)
        out: list[str] = []
        for x in candidates:
            if x in res.pins or x in res.retired:
                continue
            k = ck(reg["key_fn"], x, ctx) if reg["key_fn"] else canon(["exact", x])
            if k in live_keys or k in asked_this_run or k in out:
                continue
            out.append(k)
        return out


# ------------------------------------------------------------------ contract O: the seam to rule D


def _lab(v: bool, cite: str, overrides: str = "") -> rule_d.Label:
    return rule_d.Label(rule_d.Answer("value", "true" if v else "false"), cite, overrides)


def authorities(res: Resolution, ctx: Ctx, book: Book, callable_: str) -> rule_d.Authorities:
    """Row 3 + S6 addendum: rule D's `Authorities`. Precedence was resolved once in `resolve`,
    so every input sits in exactly one slot, with the source that won and a non-empty cite:
    spec `clause_id:entry`, convention `conv_id@version:entry`, user or class `source:by:entry`,
    either `either:entry`, references pin `references-pin:entry`."""
    recs = {r["record_hash"]: r for r in book.records}
    live = book._live(callable_)
    specs = Book._specs(live, ctx)
    convs = [r for r in live if r["kind"] == "convention"]
    conv_ids = tuple(dict.fromkeys(f"{r['conv_id']}@{r['version']}" for r in convs))

    def clause_cite(x: str) -> tuple[str | None, str]:
        sp = Book._spec_for(specs, x, ctx)
        if sp is not None:
            return sp["clause_id"], f"{sp['clause_id']}:{sp['record_hash']}"
        cv, _a = Book._conv_answer(convs, x, ctx)
        if cv is not None:
            cid = f"{cv['conv_id']}@{cv['version']}"
            return cid, f"{cid}:{cv['record_hash']}"
        return None, ""

    def named_override(x: str, rec: Record) -> str:
        cid, cite = clause_cite(x)
        return cite if cid is not None and cid in rec.get("overrides", []) else ""

    user: dict[str, rule_d.Label] = {}
    spec: dict[str, rule_d.Label] = {}
    either: dict[str, rule_d.Either] = {}
    conv: dict[str, rule_d.Label] = {}
    refp: dict[str, rule_d.Label] = {}
    spec_id = ""
    for x in res.retired:
        rec = recs[res.prov[x]["entry"]]
        either[x] = rule_d.Either(
            f"either:{res.prov[x]['entry']}", named_override(x, rec), rec["scope"] == "anchors"
        )
    for x, v in res.pins.items():
        p = res.prov[x]
        if p["source"] == "spec":
            spec[x] = _lab(v, f"{p['clause']}:{p['entry']}")
            spec_id = ctx.panel_sha
        elif p["source"] == "convention":
            conv[x] = _lab(v, f"{p['clause']}:{p['entry']}")
        elif p["source"] == "references":
            refp[x] = _lab(v, f"references-pin:{p['entry']}")
        else:
            cite = f"{p['source']}:{p['by']}:{p['entry']}"
            user[x] = _lab(v, cite, named_override(x, recs[p["entry"]]))
    return rule_d.Authorities(
        user=user,
        spec=spec,
        spec_id=spec_id,
        either=either,
        convention=conv,
        convention_ids=conv_ids if conv else (),
        reference_pins=refp,
    )


# ------------------------------------------------------------------ contracts I and L


def detected_misses(
    members: Iterable[str], tree: Mapping[str, bool], ctx: Ctx, res: Resolution
) -> list[Record]:
    """Members on which the tree's answer is contradicted by a live pin, or by at least one valid
    reference (the routed case). Shortest first. Empty when no tree exists (plan time)."""
    out: list[Record] = []
    for x in shortest_first(members):
        if x not in tree:
            continue
        t = tree[x]
        if x in res.pins:
            if res.pins[x] != t:
                out.append({"input": x, "tree": t, "by": res.prov[x]["source"]})
            continue
        v = ctx.refs[x]
        dissent = sum(1 for a in v if (a == "T") != t)
        if dissent:
            out.append({"input": x, "tree": t, "dissent": dissent, "of": len(v)})
    return out


def class_dissent(misses: Sequence[Record]) -> float:
    """The largest share of valid references that contradict the tree on a class member (0.0 when
    no tree exists). A band member scores 1.0; on a split member it is k/n, never 0."""
    return max((m["dissent"] / m["of"] for m in misses if "dissent" in m), default=0.0)


def order_questions(qs: Sequence[Record]) -> list[Record]:
    """Contract L: the most consequential question first: the class where the most references
    contradict the tree, then the class that pins more inputs, then key text (deterministic)."""
    return sorted(qs, key=lambda q: (-q["dissent"], -q["members"], q["key"]))


def question_groups(inputs: Iterable[str], ctx: Ctx, key_fn: str) -> list[list[str]]:
    """Contract L: one question covers several inputs only when they share one class key, and the
    key carries the panel label, so a group never mixes panel classes."""
    groups: dict[str, list[str]] = {}
    for x in inputs:
        groups.setdefault(ck(key_fn, x, ctx), []).append(x)
    return list(groups.values())


def render_pinned_tests(res: Resolution, callable_name: str) -> str:
    """Contract D: one pytest test per pin, shortest input first, citing its source and its clause
    or entry hash; a retired input gets none."""
    mod, fn = callable_name.rsplit(".", 1)
    out = [f"from {mod} import {fn}", ""]
    for i, x in enumerate(shortest_first(res.pins)):
        p = res.prov[x]
        out += [
            f"def test_pin_{i}():",
            f"    # source={p['source']} {p.get('clause', p.get('entry', ''))}",
            f"    assert {fn}({x!r}) is {res.pins[x]!r}",
            "",
        ]
    return "\n".join(out)


# ------------------------------------------------------------------ S3-5 prompts


def ask_question(
    inp: str, *, offer_either: bool, stdin: IO[str], stdout: IO[str], attempts: int = 3
) -> str:
    """S3-5 B: `[y/n/e]` only when "either" is offered; `e` counts as unparseable otherwise."""
    hint = "y/n/e" if offer_either else "y/n"
    for _ in range(attempts):
        stdout.write(f"Should {inp!r} be accepted? [{hint}] ")
        a = stdin.readline().strip().lower()
        if a == "":
            return "skipped"
        if a in ("y", "yes"):
            return "yes"
        if a in ("n", "no"):
            return "no"
        if offer_either and a in ("e", "either"):
            return "either"
        stdout.write("Please answer y or n" + (", or e for either" if offer_either else "") + ".\n")
    return "skipped"


def either_text(misses: Sequence[Record], n_members: int, anchor: str) -> str:
    m = misses[0]
    verb, other = ("accepts", "reject") if m["tree"] else ("rejects", "accept")
    if "dissent" in m:
        why = f"{m['dissent']} of {m['of']} independent implementations {other} it"
    else:
        why = f"the {m['by']} answer is to {other} it"
    more = f" (+{len(misses) - 1} more)" if len(misses) > 1 else ""
    return (
        f'Answering "either" stops checking {n_members} input(s) like {anchor!r} for every later '
        f"tree of this task. Your code {verb} {m['input']!r}; {why}{more}. "
        "Stop checking this class anyway?"
    )


def confirm_either(
    misses: Sequence[Record], n_members: int, anchor: str, *, stdin: IO[str], stdout: IO[str]
) -> bool:
    """I: the second confirmation over a detected miss. Enter, EOF and garbage all mean no."""
    return ask_confirm(
        either_text(misses, n_members, anchor), stdin=stdin, stdout=stdout, default=False
    )


def confirm_override(x: str, sp: Record, *, stdin: IO[str], stdout: IO[str]) -> bool:
    """F: the user overrides a spec clause only by name, confirmed `[y/N]`."""
    word = "accepted" if sp["answer"] == "accept" else "rejected"
    return ask_confirm(
        f"{x!r} is decided by {sp['clause_id']} ({'; '.join(sp['sources'])}): it must be "
        f"{word}. Your answer contradicts it. Override the spec for this input?",
        stdin=stdin,
        stdout=stdout,
        default=False,
    )


def answer_either(
    book: Book,
    callable_: str,
    anchor: str,
    known: Sequence[str],
    tree: Mapping[str, bool] | None,
    ctx: Ctx,
    *,
    by: str,
    ask_point: str,
    stdin: IO[str],
    stdout: IO[str],
    run_id: str,
    tree_hash: str,
    at: str,
) -> str:
    """I: "either" is offered only to a registered user at a registered point; over a detected
    miss it needs the second confirmation. Returns `not-offered`, `skipped` or `either`."""
    reg = book.registration(callable_)
    if by not in reg["either_by"] or ask_point not in reg["either_at"]:
        return "not-offered"
    pv = book.preview(callable_, "either", {anchor: "either"}, known, ctx)
    key = ck(reg["key_fn"], anchor, ctx)
    members = [x for x in known if ck(reg["key_fn"], x, ctx) == key]
    misses = detected_misses(members, tree or {}, ctx, book.resolve(callable_, members, ctx))
    second = ""
    if misses:
        if not confirm_either(misses, pv.members, anchor, stdin=stdin, stdout=stdout):
            return "skipped"
        second = "confirmed"
    book.save_class(
        callable_,
        "either",
        {anchor: "either"},
        pv,
        confirmed_preview=True,
        by=by,
        at=at,
        question=f"Should {anchor!r} be accepted?",
        tree_hash=tree_hash,
        ask_point=ask_point,
        run_id=run_id,
        ctx=ctx,
        shown_misses=[m["input"] for m in misses],
        second_confirmation=second,
    )
    return "either"
