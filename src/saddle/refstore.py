"""A frozen, content-addressed store for differential-rule references.

This module is standalone: it imports nothing from saddle, calls no model and needs no
network or GPU. It follows the rule D draft's storage and hashing design
(contracts C1 and C2), with one extension: optional answer tables and a provenance
stamp (task id, model, sampling, server profile) in the manifest.

Layout of a store directory::

    SHA256SUMS            "<64 hex>  <relpath>\\n" per file below, sorted by path; coreutils
                          `sha256sum -c` compatible; not listed itself
    manifest.json         {"format": "saddle-refstore/1", "task_id", "path", "function",
                           "stamp": {"model", "sampling", "server_profile", "drawn"},
                           "references": [{"id", "valid", "why", "draw": {...}}, ...],
                           "answers": [{"name", "source", "source_sha256", "rows"}, ...]}
    code/<id>.py          the reference's module source, byte for byte
    raw/<id>.json         optional: the draw as recorded, byte for byte
    answers/<name>.jsonl  optional: one JSON object per line with "input" and "refs"
                          ({reference id: answer}) and any other columns, byte for byte

**Set id.** ``set_id = sha256(bytes of SHA256SUMS)``. Because every file is listed with its
digest, the id binds every byte the store holds. ``sha256sum SHA256SUMS | cut -c1-64`` prints it.

**Contract C1 (the sealed id binds everything a reader reads).** ``load_reference_set(store,
set_id)`` returns the set only when all hold: the sha256 of SHA256SUMS equals ``set_id``;
every listed path is relative, contains no ``..`` component, exists, and has its digest;
``manifest.json`` is listed; ``code/<id>.py`` is listed for every reference in the manifest;
every answers table named in the manifest is listed, and every one of its rows has a
``refs`` object whose keys are exactly the manifest's reference ids; every regular file under
``store`` other than ``SHA256SUMS`` (found with ``os.walk``, dotted directories included) is
listed. Anything else raises ``ReferenceSetError`` naming the first offending path, and does
so under ``python -O`` too (no ``assert`` on the checked path).

**Contract C2 (verbatim and write-once).** ``write_reference_set`` writes code and raw records
byte for byte, then the manifest, then SHA256SUMS, and returns the set id. It raises
``ReferenceSetError`` and writes nothing when ``store`` exists and is not empty, or when any
reference id or table name is not a file-name-safe token ``[A-Za-z0-9_.-]+``.

Direction relative to the phase1 freeze (`t1_reference`): tightened (validity is listed,
unlisted files are refused, checks survive ``-O``, answer tables are bound to the id).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FORMAT = "saddle-refstore/1"
_TOKEN = re.compile(r"^[A-Za-z0-9_.-]+$")
_SUMS = "SHA256SUMS"
_MANIFEST = "manifest.json"


class ReferenceSetError(RuntimeError):
    """Raised for every store defect. Never an assert."""


@dataclass(frozen=True)
class Reference:
    id: str
    code: str
    valid: bool
    why: str | None = None
    raw: bytes | None = None
    draw: Mapping[str, Any] = field(
        default_factory=dict
    )  # per-draw facts: seed, phrasing, effort, usage...


@dataclass(frozen=True)
class AnswerTable:
    name: str
    source: str  # where the table came from (a path or a description)
    source_sha256: str
    rows: bytes  # the .jsonl bytes, verbatim


@dataclass(frozen=True)
class Stamp:
    model: str
    sampling: Mapping[
        str, Any
    ]  # e.g. {"temperature": 0.8, "max_tokens": 6000, "reasoning_effort": "low"}
    server_profile: str | None  # the `vllm serve ...` line, or None when not recorded at draw time
    drawn: str  # date/run reference of the draws
    note: str | None = None


@dataclass(frozen=True)
class ReferenceSet:
    set_id: str
    task_id: str
    path: str
    function: str
    stamp: Stamp
    references: tuple[Reference, ...]
    answers: tuple[AnswerTable, ...] = ()

    def ids(self) -> tuple[str, ...]:
        return tuple(r.id for r in self.references)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _check_token(kind: str, value: str) -> None:
    if not isinstance(value, str) or not _TOKEN.match(value) or value in (".", ".."):
        msg = f"{kind} {value!r} is not a file-name-safe token [A-Za-z0-9_.-]+"
        raise ReferenceSetError(msg)


# ---------------------------------------------------------------------------- write


def write_reference_set(
    store: Path,
    *,
    task_id: str,
    path: str,
    function: str,
    stamp: Stamp,
    references: Sequence[Reference],
    answers: Sequence[AnswerTable] = (),
) -> str:
    """Write a store (C2) and return its set id. Fails before writing anything."""
    store = Path(store)
    if store.exists() and (not store.is_dir() or any(store.iterdir())):
        msg = f"store {store} exists and is not an empty directory"
        raise ReferenceSetError(msg)
    if not references:
        msg = "a reference set needs at least one reference"
        raise ReferenceSetError(msg)
    seen: set[str] = set()
    for r in references:
        _check_token("reference id", r.id)
        if r.id in seen:
            msg = f"duplicate reference id {r.id!r}"
            raise ReferenceSetError(msg)
        seen.add(r.id)
        if not isinstance(r.code, str):
            msg = f"reference {r.id!r}: code must be str"
            raise ReferenceSetError(msg)
    tnames: set[str] = set()
    ids = [r.id for r in references]
    for t in answers:
        _check_token("answers table name", t.name)
        if t.name in tnames:
            msg = f"duplicate answers table {t.name!r}"
            raise ReferenceSetError(msg)
        tnames.add(t.name)
        _check_answer_rows(t.name, t.rows, ids)

    files: dict[str, bytes] = {}
    for r in references:
        files[f"code/{r.id}.py"] = r.code.encode("utf-8")
        if r.raw is not None:
            files[f"raw/{r.id}.json"] = bytes(r.raw)
    for t in answers:
        files[f"answers/{t.name}.jsonl"] = bytes(t.rows)
    manifest = {
        "format": FORMAT,
        "task_id": task_id,
        "path": path,
        "function": function,
        "stamp": {
            "model": stamp.model,
            "sampling": dict(stamp.sampling),
            "server_profile": stamp.server_profile,
            "drawn": stamp.drawn,
            "note": stamp.note,
        },
        "references": [
            {
                "id": r.id,
                "valid": bool(r.valid),
                "why": r.why,
                "raw": r.raw is not None,
                "draw": dict(r.draw),
            }
            for r in references
        ],
        "answers": [
            {
                "name": t.name,
                "source": t.source,
                "source_sha256": t.source_sha256,
                "rows": _count_lines(t.rows),
            }
            for t in answers
        ],
    }
    files[_MANIFEST] = (
        json.dumps(manifest, indent=1, ensure_ascii=False, sort_keys=True) + "\n"
    ).encode("utf-8")
    sums = "".join(f"{_sha256(files[p])}  {p}\n" for p in sorted(files))
    sums_bytes = sums.encode("utf-8")

    store.mkdir(parents=True, exist_ok=True)
    for rel, data in files.items():
        dst = store / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        with open(dst, "wb") as fh:  # binary: no newline translation
            fh.write(data)
    with open(store / _SUMS, "wb") as fh:
        fh.write(sums_bytes)
    return _sha256(sums_bytes)


def _count_lines(rows: bytes) -> int:
    return sum(1 for line in rows.splitlines() if line.strip())


def _check_answer_rows(name: str, rows: bytes, ids: Sequence[str]) -> None:
    want = set(ids)
    for n, line in enumerate(rows.splitlines(), 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            msg = f"answers/{name}.jsonl line {n}: not JSON ({e.msg})"
            raise ReferenceSetError(msg) from None
        if not isinstance(obj, dict) or "input" not in obj or not isinstance(obj.get("refs"), dict):
            msg = f"answers/{name}.jsonl line {n}: needs 'input' and a 'refs' object"
            raise ReferenceSetError(msg)
        if set(obj["refs"]) != want:
            missing = sorted(want - set(obj["refs"]))
            extra = sorted(set(obj["refs"]) - want)
            msg = (
                f"answers/{name}.jsonl line {n}: refs keys differ from the manifest ids "
                f"(missing {missing}, extra {extra})"
            )
            raise ReferenceSetError(msg)


# ----------------------------------------------------------------------------- load


def _parse_sums(sums_bytes: bytes) -> dict[str, str]:
    listed: dict[str, str] = {}
    for n, line in enumerate(sums_bytes.decode("utf-8").splitlines(), 1):
        if not line:
            continue
        m = re.fullmatch(r"([0-9a-f]{64}) [ *](.+)", line)
        if not m:
            msg = f"{_SUMS} line {n}: not a sha256sum line"
            raise ReferenceSetError(msg)
        digest, rel = m.group(1), m.group(2)
        if rel in listed:
            msg = f"{_SUMS} line {n}: {rel!r} listed twice"
            raise ReferenceSetError(msg)
        listed[rel] = digest
    return listed


def _check_relpath(rel: str) -> None:
    p = Path(rel)
    if p.is_absolute() or rel.startswith(("/", "\\")) or ".." in p.parts or rel == _SUMS:
        msg = f"{_SUMS} names a path outside the store or itself: {rel!r}"
        raise ReferenceSetError(msg)


def _walk_files(store: Path) -> set[str]:
    found: set[str] = set()
    for root, _dirs, files in os.walk(store):  # os.walk: dotted directories included
        for f in files:
            full = Path(root) / f
            if full.is_file():
                found.add(full.relative_to(store).as_posix())
    return found


def load_reference_set(store: Path, set_id: str) -> ReferenceSet:
    """Load a store only when it is exactly the set sealed as ``set_id`` (C1)."""
    store = Path(store)
    if not isinstance(set_id, str) or not re.fullmatch(r"[0-9a-f]{64}", set_id):
        msg = f"set_id must be 64 lowercase hex characters, got {set_id!r}"
        raise ReferenceSetError(msg)
    sums_path = store / _SUMS
    if not sums_path.is_file():
        msg = f"missing {sums_path}"
        raise ReferenceSetError(msg)
    sums_bytes = sums_path.read_bytes()
    actual_id = _sha256(sums_bytes)
    if actual_id != set_id:
        msg = f"set id mismatch: {_SUMS} hashes to {actual_id}, expected {set_id}"
        raise ReferenceSetError(msg)

    listed = _parse_sums(sums_bytes)
    for rel in listed:
        _check_relpath(rel)  # before any listed path is opened
    if _MANIFEST not in listed:
        msg = f"{_MANIFEST} is not listed in {_SUMS}"
        raise ReferenceSetError(msg)

    contents: dict[str, bytes] = {}
    for rel in sorted(listed):
        full = store / rel
        if not full.is_file():
            msg = f"listed file missing: {rel}"
            raise ReferenceSetError(msg)
        data = full.read_bytes()
        if _sha256(data) != listed[rel]:
            msg = f"digest mismatch: {rel}"
            raise ReferenceSetError(msg)
        contents[rel] = data

    unlisted = sorted(_walk_files(store) - set(listed) - {_SUMS})
    if unlisted:
        msg = f"unlisted file in store: {unlisted[0]}"
        raise ReferenceSetError(msg)

    try:
        manifest = json.loads(contents[_MANIFEST].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        msg = f"{_MANIFEST}: not valid JSON ({e})"
        raise ReferenceSetError(msg) from None
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        msg = f"{_MANIFEST}: format is not {FORMAT!r}"
        raise ReferenceSetError(msg)
    for key in ("task_id", "path", "function", "stamp", "references"):
        if key not in manifest:
            msg = f"{_MANIFEST}: missing {key!r}"
            raise ReferenceSetError(msg)

    refs: list[Reference] = []
    ids: list[str] = []
    for entry in manifest["references"]:
        rid = entry.get("id")
        _check_token("reference id", rid)
        if rid in ids:
            msg = f"{_MANIFEST}: duplicate reference id {rid!r}"
            raise ReferenceSetError(msg)
        code_rel = f"code/{rid}.py"
        if code_rel not in listed:
            msg = f"reference {rid!r}: {code_rel} is not listed"
            raise ReferenceSetError(msg)
        raw_rel = f"raw/{rid}.json"
        has_raw = bool(entry.get("raw", False))
        if has_raw and raw_rel not in listed:
            msg = f"reference {rid!r}: {raw_rel} is not listed"
            raise ReferenceSetError(msg)
        if not isinstance(entry.get("valid"), bool):
            msg = f"reference {rid!r}: 'valid' must be a boolean"
            raise ReferenceSetError(msg)
        refs.append(
            Reference(
                id=rid,
                code=contents[code_rel].decode("utf-8"),
                valid=entry["valid"],
                why=entry.get("why"),
                raw=contents[raw_rel] if has_raw else None,
                draw=dict(entry.get("draw") or {}),
            )
        )
        ids.append(rid)
    if not refs:
        msg = f"{_MANIFEST}: no references"
        raise ReferenceSetError(msg)

    tables: list[AnswerTable] = []
    for entry in manifest.get("answers", []):
        name = entry.get("name")
        _check_token("answers table name", name)
        rel = f"answers/{name}.jsonl"
        if rel not in listed:
            msg = f"answers table {name!r}: {rel} is not listed"
            raise ReferenceSetError(msg)
        rows = contents[rel]
        _check_answer_rows(name, rows, ids)
        if _count_lines(rows) != entry.get("rows"):
            msg = (
                f"answers table {name!r}: row count {_count_lines(rows)}"
                f" != manifest {entry.get('rows')}"
            )
            raise ReferenceSetError(msg)
        tables.append(
            AnswerTable(
                name=name,
                source=entry.get("source", ""),
                source_sha256=entry.get("source_sha256", ""),
                rows=rows,
            )
        )

    s = manifest["stamp"]
    stamp = Stamp(
        model=s.get("model", ""),
        sampling=dict(s.get("sampling") or {}),
        server_profile=s.get("server_profile"),
        drawn=s.get("drawn", ""),
        note=s.get("note"),
    )
    return ReferenceSet(
        set_id=set_id,
        task_id=manifest["task_id"],
        path=manifest["path"],
        function=manifest["function"],
        stamp=stamp,
        references=tuple(refs),
        answers=tuple(tables),
    )


def set_id_of(store: Path) -> str:
    """The id a store *claims* (sha256 of its SHA256SUMS).

    Not a check; pass it to load_reference_set.
    """
    return _sha256((Path(store) / _SUMS).read_bytes())
