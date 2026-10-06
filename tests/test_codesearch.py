"""`saddle.codesearch` and the `code_search` tool.

Contract: `saddle index` embeds each tracked, readable file once per content
(an unchanged file is never embedded again, a changed one is); `code_search`
ranks the indexed windows by similarity to the question, names each by
path:start-end, and always says how many of the indexable files it searched;
nothing indexed, or a server that is down, is an error and never reads as "no
matches". The tool is offered only when the `embeddings` switch is on and the
server answers.
"""

from __future__ import annotations

import io
import math
import subprocess
from pathlib import Path
from typing import Any

import pytest

from saddle import cli, codesearch, tools
from saddle.codesearch import CHUNK_LINES, Index, chunks, readable, run_index
from saddle.embed import EmbedError
from saddle.tools import CODE_SEARCH_TOOL, ToolContext, execute_tool, offer_code_search
from saddle.vllm import ToolCall

WORDS = ("retry", "image", "parse")


DOWN = "no embeddings server at http://e"


class FakeEmbed:
    """Vectors from which of `WORDS` a text names, so ranking is predictable."""

    def __init__(self, *, up: bool = True) -> None:
        self.up = up
        self.embedded: list[str] = []

    def health(self) -> dict[str, Any]:
        if not self.up:
            raise EmbedError(DOWN)
        return {"model": "fake", "dim": len(WORDS) + 1}

    def embed(self, items: list[Any], dimensions: int | None = None) -> list[list[float]]:
        if not self.up:
            raise EmbedError(DOWN)
        assert dimensions == codesearch.DIMENSIONS
        vectors = []
        for item in items:
            assert isinstance(item, str)
            self.embedded.append(item)
            raw = [float(item.count(w)) for w in WORDS] + [0.1]
            norm = math.sqrt(sum(x * x for x in raw))
            vectors.append([x / norm for x in raw])
        return vectors


def _repo(root: Path, files: dict[str, str | bytes]) -> Path:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    for name, body in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(body, bytes):
            path.write_bytes(body)
        else:
            path.write_text(body)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    return root


def _index(root: Path, cache: Path, client: FakeEmbed | None = None) -> Index:
    return Index(root, client or FakeEmbed(), cache)  # type: ignore[arg-type]


def test_an_index_leaves_out_generated_large_binary_and_undecodable_files(
    tmp_path: Path,
) -> None:
    root = _repo(
        tmp_path / "r",
        {
            "a.py": "x = 1\n",
            "uv.lock": "lock\n",
            "blob.bin": b"\x00\x01",
            "latin.txt": b"caf\xe9\n",
            "big.txt": "y" * (codesearch.MAX_FILE_BYTES + 1),
        },
    )
    kept = [n for n in codesearch.tracked_files(root) if readable(root, n) is not None]
    assert kept == ["a.py"]
    assert readable(root, "gone.py") is None


def test_chunks_are_numbered_windows_and_blank_windows_are_left_out() -> None:
    text = "\n".join(["a"] * CHUNK_LINES + [""] * CHUNK_LINES + ["b", "c"])
    found = chunks("f.py", text)
    assert [(c.start, c.end) for c in found] == [
        (1, CHUNK_LINES),
        (2 * CHUNK_LINES + 1, 2 * CHUNK_LINES + 2),
    ]
    assert found[1].text == "b\nc"


def test_build_embeds_each_content_once_and_again_when_it_changes(tmp_path: Path) -> None:
    root = _repo(tmp_path / "r", {"a.py": "def retry(): ...\n", "b.py": "parse\n"})
    cache = tmp_path / "cache"
    client = FakeEmbed()
    assert _index(root, cache, client).build() == (2, 2)
    assert client.embedded == [
        "title: a.py:1-1 | text: def retry(): ...",
        "title: b.py:1-1 | text: parse",
    ]
    assert _index(root, cache, client).build() == (0, 2)
    (root / "a.py").write_text("def image(): ...\n")
    heard: list[tuple[str, int]] = []
    assert _index(root, cache, client).build(lambda n, w: heard.append((n, w))) == (1, 2)
    assert heard == [("a.py", 1)]


def test_search_ranks_by_meaning_and_says_how_much_it_searched(tmp_path: Path) -> None:
    root = _repo(
        tmp_path / "r",
        {"net.py": "def again():\n    retry retry\n", "img.py": "image\n", "p.py": "parse\n"},
    )
    cache = tmp_path / "cache"
    _index(root, cache).build()
    found = _index(root, cache).search("retry", top=2)
    lines = found.splitlines()
    assert lines[0].startswith("Searched 3 of 3 indexable files. Nearest first")
    assert lines[1].startswith("net.py:1-2 (1.00)")
    assert lines[2:4] == ["    def again():", "        retry retry"]
    assert sum(1 for line in lines if not line.startswith(" ")) == 3  # header + top 2
    (root / "copy.py").write_text("image\n")  # the same text is the same vectors
    (root / "new.py").write_text("image parse\n")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    assert (
        _index(root, cache)
        .search("image")
        .startswith(
            "Searched 4 of 5 indexable files; not indexed (changed since `saddle index`, or new): "
            "new.py."
        )
    )


def test_a_long_list_of_unindexed_files_is_counted_not_dropped(tmp_path: Path) -> None:
    root = _repo(tmp_path / "r", {"a.py": "retry\n"})
    cache = tmp_path / "cache"
    _index(root, cache).build()
    for n in range(7):
        (root / f"n{n}.py").write_text(f"new {n}\n")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    assert "n4.py and 2 more." in _index(root, cache).search("retry")


def test_nothing_indexed_is_an_error_not_no_matches(tmp_path: Path) -> None:
    root = _repo(tmp_path / "r", {"a.py": "retry\n"})
    found = _index(root, tmp_path / "cache").search("retry")
    assert found.startswith("error: none of the 1 indexable files is indexed yet")
    assert "saddle index" in found


def test_a_folder_that_is_not_a_repository_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(codesearch.CodeSearchError, match="not a git repository"):
        codesearch.tracked_files(tmp_path)


def test_the_cache_lives_where_the_environment_says() -> None:
    assert codesearch.cache_root({"SADDLE_EMBED_CACHE": "/c"}) == Path("/c")
    assert codesearch.cache_root({"XDG_CACHE_HOME": "/x"}) == Path("/x/saddle/embeddings")
    assert codesearch.cache_root({}).parts[-3:] == (".cache", "saddle", "embeddings")


def test_a_corrupt_cache_entry_is_embedded_again(tmp_path: Path) -> None:
    root = _repo(tmp_path / "r", {"a.py": "retry\n"})
    cache = tmp_path / "cache"
    _index(root, cache).build()
    for entry in (cache / "fake-4d").iterdir():
        entry.write_text("{}")
    assert _index(root, cache).build() == (1, 1)


def test_saddle_index_reports_what_it_embedded_or_why_it_could_not(tmp_path: Path) -> None:
    root = _repo(tmp_path / "r", {"a.py": "retry\n"})
    out, err = io.StringIO(), io.StringIO()
    monkey = pytest.MonkeyPatch()
    monkey.setenv("SADDLE_EMBED_CACHE", str(tmp_path / "cache"))
    try:
        assert run_index(root, stdout=out, stderr=err, client=FakeEmbed()) == 0  # type: ignore[arg-type]
        assert out.getvalue().splitlines() == [
            "embedded a.py (1 windows)",
            "1 indexable files; 1 embedded now, 0 already cached (fake-4d).",
        ]
        assert run_index(root, stdout=out, stderr=err, client=FakeEmbed(up=False)) == 1  # type: ignore[arg-type]
        assert err.getvalue().startswith("saddle index: no embeddings server")
        monkey.setattr(codesearch, "EmbedClient", FakeEmbed)
        assert cli.main(["index", str(root)], stdout=out, stderr=err) == 0
        assert out.getvalue().endswith("0 embedded now, 1 already cached (fake-4d).\n")
    finally:
        monkey.undo()


def test_code_search_is_offered_only_when_switched_on_and_the_server_answers(
    tmp_path: Path,
) -> None:
    base = [tools.TOOLS[0]]
    off = ToolContext(workdir=tmp_path)
    assert offer_code_search(base, off, lambda: True) == base
    on = ToolContext(workdir=tmp_path, embeddings=True, allowed=("read_file",))
    assert offer_code_search(base, on, lambda: False) == base
    assert on.allowed == ("read_file",)
    offered = offer_code_search(base, on, lambda: True)
    assert [t["function"]["name"] for t in offered] == ["read_file", CODE_SEARCH_TOOL]
    assert tuple(on.allowed or ()) == ("read_file", CODE_SEARCH_TOOL)
    assert offer_code_search(offered, on, lambda: True) == offered


def test_the_default_health_check_asks_the_embeddings_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    on = ToolContext(workdir=tmp_path, embeddings=True)
    monkeypatch.setattr(tools, "EmbedClient", FakeEmbed)
    assert len(offer_code_search([], on)) == 1
    monkeypatch.setattr(tools, "EmbedClient", lambda: FakeEmbed(up=False))
    assert offer_code_search([], on) == []


def test_the_tool_searches_the_working_directory_and_reports_a_down_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _repo(tmp_path / "r", {"a.py": "retry\n"})
    monkeypatch.setenv("SADDLE_EMBED_CACHE", str(tmp_path / "cache"))
    _index(root, tmp_path / "cache").build()
    monkeypatch.setattr(tools, "EmbedClient", FakeEmbed)
    call = ToolCall("1", CODE_SEARCH_TOOL, '{"query": "retry"}')
    assert execute_tool(call, workdir=root).startswith("Searched 1 of 1 indexable files")
    monkeypatch.setattr(tools, "EmbedClient", lambda: FakeEmbed(up=False))
    assert execute_tool(call, workdir=root) == (
        "error: code_search could not search: no embeddings server at http://e. "
        "Use `search` for exact text."
    )


@pytest.mark.parametrize(
    ("name", "args"),
    [
        (CODE_SEARCH_TOOL, '{"query": "x", "top": 9}'),
        ("read_text", '{"path": "a.png", "lang": "en"}'),
        ("compare_images", '{"first": "a.png", "second": "b.png", "fuzz": 5}'),
    ],
)
def test_the_optional_tools_refuse_arguments_they_do_not_declare(
    tmp_path: Path, name: str, args: str
) -> None:
    found = execute_tool(ToolCall("1", name, args), workdir=tmp_path)
    assert found.startswith(f"error: {name} does not take ")
