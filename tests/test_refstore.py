"""Known-good / known-bad pairs for the S1-c reference store (vendored from parallel/out/S1C).

The S1-c test that loads the built T1 store is not vendored: the store lives outside the repo.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from saddle import refstore
from saddle.refstore import (
    AnswerTable,
    Reference,
    ReferenceSetError,
    Stamp,
    load_reference_set,
    set_id_of,
    write_reference_set,
)

STAMP = Stamp(
    model="m", sampling={"temperature": 0.8}, server_profile="vllm serve x --port 1", drawn="test"
)
LONG_CODE = (
    "from collections import namedtuple\n"
    'Token = namedtuple("Token", "a b")\n'
    'api_key = "sk-abcdefghijkl"\n'
    "def f(s):\r\n    return True\r\n"  # CRLF must survive
    "# " + "x" * 5000 + "\n"
)


def refs3() -> list[Reference]:
    return [
        Reference("r1", LONG_CODE, True, draw={"seed": 1}),
        Reference(
            "r2", "def f(s):\n    return False\n", False, why="does not import", raw=b'{"raw": 1}\n'
        ),
        Reference("r3", "def f(s):\n    return s == ''\n", True),
    ]


def rows(ids: list[str], n: int = 3) -> bytes:
    return b"".join(
        json.dumps({"input": f"in{i}", "refs": dict.fromkeys(ids, i % 2 == 0)}).encode() + b"\n"
        for i in range(n)
    )


def write3(store: Path, answers: bool = True) -> str:
    tables = [AnswerTable("t", "unit", "0" * 64, rows(["r1", "r2", "r3"]))] if answers else []
    return write_reference_set(
        store,
        task_id="tX",
        path="mod.py",
        function="f",
        stamp=STAMP,
        references=refs3(),
        answers=tables,
    )


def reseal(store: Path) -> str:
    """An attacker's consistent rewrite: regenerate SHA256SUMS from the tree, return the new id."""
    lines = []
    for root, _d, files in os.walk(store):
        for f in files:
            full = Path(root) / f
            rel = full.relative_to(store).as_posix()
            if rel != "SHA256SUMS":
                lines.append(f"{hashlib.sha256(full.read_bytes()).hexdigest()}  {rel}\n")
    data = "".join(sorted(lines, key=lambda line: line[66:])).encode()
    (store / "SHA256SUMS").write_bytes(data)
    return hashlib.sha256(data).hexdigest()


# ----------------------------------------------------------------- known-good


def test_reference_set_round_trip_is_byte_exact_and_human_checkable(tmp_path: Path) -> None:
    store = tmp_path / "s"
    set_id = write3(store)
    got = load_reference_set(store, set_id)
    assert [r.code for r in got.references] == [r.code for r in refs3()]
    assert got.references[0].code.count("\r\n") == 2
    assert got.references[1].raw == b'{"raw": 1}\n'
    assert got.references[0].raw is None
    assert [(r.id, r.valid, r.why) for r in got.references] == [
        ("r1", True, None),
        ("r2", False, "does not import"),
        ("r3", True, None),
    ]
    assert got.references[0].draw == {"seed": 1}
    assert got.task_id == "tX"
    assert got.path == "mod.py"
    assert got.function == "f"
    assert got.stamp == STAMP
    assert len(got.answers) == 1
    assert got.answers[0].rows == rows(["r1", "r2", "r3"])
    # coreutils-compatible, and the id is the sum of the sums file
    assert subprocess.run(["sha256sum", "-c", "--quiet", "SHA256SUMS"], cwd=store).returncode == 0
    out = subprocess.run(
        ["sha256sum", "SHA256SUMS"], cwd=store, capture_output=True, text=True
    ).stdout
    assert out[:64] == set_id == set_id_of(store)
    assert (
        store / "code" / "r1.py"
    ).read_bytes() == LONG_CODE.encode()  # verbatim: no redaction, no cap


# ------------------------------------------------------------------ known-bad


def test_reference_set_flipped_validity_is_refused(tmp_path: Path) -> None:
    store = tmp_path / "s"
    set_id = write3(store)
    m = json.loads((store / "manifest.json").read_text())
    m["references"][1]["valid"] = True
    (store / "manifest.json").write_text(json.dumps(m, indent=1, sort_keys=True) + "\n")
    with pytest.raises(ReferenceSetError, match=r"manifest\.json"):
        load_reference_set(store, set_id)
    # the consistent rewrite (SHA256SUMS regenerated to match) is caught by the sealed id
    reseal(store)
    with pytest.raises(ReferenceSetError, match="set id mismatch"):
        load_reference_set(store, set_id)


def test_reference_set_unlisted_files_are_refused_including_dotted_dirs(tmp_path: Path) -> None:
    store = tmp_path / "s"
    set_id = write3(store)
    (store / "code" / "r99.py").write_text("def f(s): return True\n")
    with pytest.raises(ReferenceSetError, match=r"unlisted file in store: code/r99\.py"):
        load_reference_set(store, set_id)
    (store / "code" / "r99.py").unlink()
    (store / ".hidden").mkdir()
    (store / ".hidden" / "x").write_text("x")
    with pytest.raises(ReferenceSetError, match=r"unlisted file in store: \.hidden/x"):
        load_reference_set(store, set_id)
    (store / ".hidden" / "x").unlink()
    (store / ".hidden").rmdir()
    load_reference_set(store, set_id)  # the untouched store still loads


def test_reference_set_one_changed_byte_is_named(tmp_path: Path) -> None:
    store = tmp_path / "s"
    set_id = write3(store)
    p = store / "code" / "r3.py"
    p.write_bytes(p.read_bytes().replace(b"== ''", b"!= ''"))
    with pytest.raises(ReferenceSetError, match=r"digest mismatch: code/r3\.py"):
        load_reference_set(store, set_id)


def test_reference_set_consistent_rewrite_fails_the_sealed_id(tmp_path: Path) -> None:
    store = tmp_path / "s"
    set_id = write3(store)
    (store / "code" / "r1.py").write_text("def f(s): return True\n")
    new_id = reseal(store)
    assert new_id != set_id
    assert (
        subprocess.run(["sha256sum", "-c", "--quiet", "SHA256SUMS"], cwd=store).returncode == 0
    )  # a naive check passes
    with pytest.raises(ReferenceSetError, match="set id mismatch"):
        load_reference_set(store, set_id)
    load_reference_set(store, new_id)  # and the rewrite is a valid *different* set


def test_reference_set_checks_survive_python_optimize(tmp_path: Path) -> None:
    store = tmp_path / "s"
    set_id = write3(store)
    p = store / "code" / "r3.py"
    p.write_bytes(p.read_bytes().replace(b"== ''", b"!= ''"))
    code = (
        "from saddle.refstore import load_reference_set; "
        f"load_reference_set({str(store)!r}, {set_id!r})"
    )
    src = str(Path(refstore.__file__).resolve().parents[1])
    r = subprocess.run(
        [sys.executable, "-O", "-c", code],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": src},
    )
    assert r.returncode != 0
    assert "ReferenceSetError" in r.stderr
    assert "code/r3.py" in r.stderr


def test_reference_set_paths_outside_the_store_are_refused_before_opening(tmp_path: Path) -> None:
    store = tmp_path / "s"
    write3(store)
    outside = tmp_path / "x"
    outside.write_text("secret\n")
    sums = (store / "SHA256SUMS").read_text()
    for bad in ("../x", str(outside)):  # the file exists and carries its true digest
        (store / "SHA256SUMS").write_text(
            sums + f"{hashlib.sha256(outside.read_bytes()).hexdigest()}  {bad}\n"
        )
        new_id = set_id_of(store)
        with pytest.raises(ReferenceSetError, match="outside the store"):
            load_reference_set(store, new_id)


def test_reference_set_is_write_once_and_ids_are_tokens(tmp_path: Path) -> None:
    store = tmp_path / "s"
    write3(store)
    before = {p: p.read_bytes() for p in store.rglob("*") if p.is_file()}
    with pytest.raises(ReferenceSetError, match="not an empty directory"):
        write3(store)
    assert {p: p.read_bytes() for p in store.rglob("*") if p.is_file()} == before
    fresh = tmp_path / "f"
    with pytest.raises(ReferenceSetError, match="file-name-safe"):
        write_reference_set(
            fresh,
            task_id="t",
            path="m.py",
            function="f",
            stamp=STAMP,
            references=[Reference("a/b", "x", True)],
        )
    assert not fresh.exists()
    fresh2 = tmp_path / "f2"
    with pytest.raises(ReferenceSetError, match="file-name-safe"):
        write_reference_set(
            fresh2,
            task_id="t",
            path="m.py",
            function="f",
            stamp=STAMP,
            references=[Reference("ok", "x", True)],
            answers=[AnswerTable("../t", "u", "0" * 64, rows(["ok"]))],
        )
    assert not fresh2.exists()


def test_reference_set_manifest_reference_without_code_file_is_named(tmp_path: Path) -> None:
    store = tmp_path / "s"
    write3(store)
    m = json.loads((store / "manifest.json").read_text())
    m["references"].append({"id": "r4", "valid": True, "why": None, "raw": False, "draw": {}})
    (store / "manifest.json").write_text(json.dumps(m, indent=1, sort_keys=True) + "\n")
    new_id = reseal(store)
    with pytest.raises(ReferenceSetError, match=r"reference 'r4': code/r4\.py is not listed"):
        load_reference_set(store, new_id)


def test_reference_set_answer_rows_must_cover_exactly_the_reference_ids(tmp_path: Path) -> None:
    # on write
    bad = rows(["r1", "r2"])  # r3 missing
    with pytest.raises(ReferenceSetError, match=r"missing \['r3'\]"):
        write_reference_set(
            tmp_path / "w",
            task_id="t",
            path="m.py",
            function="f",
            stamp=STAMP,
            references=refs3(),
            answers=[AnswerTable("t", "u", "0" * 64, bad)],
        )
    assert not (tmp_path / "w").exists()
    # on load, with the sums and id rewritten to match (so only the row check can catch it)
    store = tmp_path / "s"
    write3(store)
    (store / "answers" / "t.jsonl").write_bytes(rows(["r1", "r2", "r3", "r9"]))
    new_id = reseal(store)
    with pytest.raises(ReferenceSetError, match=r"extra \['r9'\]"):
        load_reference_set(store, new_id)
    # a known-good table with an extra non-refs column still loads
    store2 = tmp_path / "s2"
    good = b"".join(
        json.dumps(
            {"input": "a", "refs": {"r1": True, "r2": False, "r3": True}, "panel": "x"}
        ).encode()
        + b"\n"
        for _ in range(2)
    )
    sid = write_reference_set(
        store2,
        task_id="t",
        path="m.py",
        function="f",
        stamp=STAMP,
        references=refs3(),
        answers=[AnswerTable("t", "u", "0" * 64, good)],
    )
    assert load_reference_set(store2, sid).answers[0].rows == good


def test_reference_set_id_argument_is_required_and_checked(tmp_path: Path) -> None:
    store = tmp_path / "s"
    set_id = write3(store)
    with pytest.raises(TypeError):
        load_reference_set(store)  # type: ignore[call-arg]
    with pytest.raises(ReferenceSetError, match="64 lowercase hex"):
        load_reference_set(store, set_id.upper())
    with pytest.raises(ReferenceSetError, match="set id mismatch"):
        load_reference_set(store, "0" * 64)


# ------------------------------------------------------------ vendored into saddle (P2-3)
# Known-bad pairs for the checks the S1-c tests left unexercised: each defect is
# built on a valid store, resealed so only the named check can catch it.


def _edit_manifest(store: Path, edit: Any) -> str:
    m = json.loads((store / "manifest.json").read_text())
    edit(m)
    (store / "manifest.json").write_text(json.dumps(m))
    return reseal(store)


@pytest.mark.parametrize(
    ("edit", "match"),
    [
        (lambda m: m.update(format="x"), "format is not"),
        (lambda m: m.pop("function"), "missing 'function'"),
        (lambda m: m["references"].append(dict(m["references"][0])), "duplicate reference id"),
        (lambda m: m["references"][0].update(raw=True), r"raw/r1\.json is not listed"),
        (lambda m: m["references"][0].update(valid="yes"), "'valid' must be a boolean"),
        (lambda m: m.update(references=[]), "no references"),
        (lambda m: m["answers"][0].update(name="u"), r"answers/u\.jsonl is not listed"),
        (lambda m: m["answers"][0].update(rows=9), "row count 3 != manifest 9"),
    ],
)
def test_reference_set_each_manifest_defect_is_named(tmp_path: Path, edit: Any, match: str) -> None:
    store = tmp_path / "s"
    write3(store)
    new_id = _edit_manifest(store, edit)
    with pytest.raises(ReferenceSetError, match=match):
        load_reference_set(store, new_id)


def test_reference_set_manifest_and_sums_defects_are_named(tmp_path: Path) -> None:
    store = tmp_path / "s"
    write3(store)
    (store / "manifest.json").write_text("{not json")
    with pytest.raises(ReferenceSetError, match="not valid JSON"):
        load_reference_set(store, reseal(store))
    sums = store / "SHA256SUMS"
    lines = sums.read_text().splitlines(keepends=True)
    for bad, match in (
        ("".join(lines) + "\n" + "zz  x\n", "not a sha256sum line"),
        ("".join(lines) + lines[0], "listed twice"),
        ("".join(x for x in lines if "manifest.json" not in x), "manifest.json is not listed"),
        ("".join(lines) + "0" * 64 + "  gone.txt\n", "listed file missing: gone.txt"),
    ):
        sums.write_text(bad)
        with pytest.raises(ReferenceSetError, match=match):
            load_reference_set(store, hashlib.sha256(bad.encode()).hexdigest())
    (store / "SHA256SUMS").unlink()
    with pytest.raises(ReferenceSetError, match="missing"):
        load_reference_set(store, "0" * 64)


def test_reference_set_a_blank_row_is_skipped_and_a_dangling_link_is_not_a_file(
    tmp_path: Path,
) -> None:
    store = tmp_path / "s"
    table = AnswerTable("t", "unit", "0" * 64, rows(["r1", "r2", "r3"]) + b"\n")
    set_id = write_reference_set(
        store,
        task_id="tX",
        path="m.py",
        function="f",
        stamp=STAMP,
        references=refs3(),
        answers=[table],
    )
    (store / "dangling").symlink_to(store / "nowhere")
    got = load_reference_set(store, set_id)
    assert got.ids() == ("r1", "r2", "r3")
    assert got.answers[0].rows.endswith(b"\n\n")


@pytest.mark.parametrize(
    ("kw", "match"),
    [
        ({"references": []}, "at least one reference"),
        ({"references": [*refs3(), refs3()[0]]}, "duplicate reference id"),
        ({"references": [Reference("r1", b"x", True)]}, "code must be str"),  # type: ignore[arg-type]
        (
            {"answers": [AnswerTable("t", "", "", b""), AnswerTable("t", "", "", b"")]},
            "duplicate answers table",
        ),
        ({"answers": [AnswerTable("t", "", "", b"{bad\n")]}, "not JSON"),
        ({"answers": [AnswerTable("t", "", "", b'{"input": 1}\n')]}, "needs 'input'"),
    ],
)
def test_reference_set_write_refuses_each_defect_before_writing(
    tmp_path: Path, kw: dict[str, Any], match: str
) -> None:
    store = tmp_path / "s"
    args: dict[str, Any] = {"references": refs3(), "answers": []} | kw
    with pytest.raises(ReferenceSetError, match=match):
        write_reference_set(store, task_id="tX", path="m.py", function="f", stamp=STAMP, **args)
    assert not store.exists()
