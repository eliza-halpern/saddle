"""`read_file` on an image shows it to the model, in Ask and Edit, when the server can see.

Contract: with images on, `read_file` of a PNG, JPEG, WebP or GIF within the size
limit queues the image and the engine sends it in a user message after the tool
result; a file that is not an image by its own header, or is over the limit, never
becomes image content; a server that does not read images is named in the result.
A Task run never turns images on, so its `read_file` is unchanged.
"""

from __future__ import annotations

import json
import struct
import subprocess
import zlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

import saddle.tools as tools_module
from saddle.auto import AutoError, AutoOptions, run_auto
from saddle.engine import TurnOptions, run_turn
from saddle.events import Event, ToolEnd
from saddle.tools import ToolContext, execute_tool
from saddle.vision import (
    GLYPHS,
    IMAGE_FOLLOWUP,
    PROBE_QUESTION,
    PROBE_TEXT,
    data_url,
    image_info,
    is_image_followup,
    known_verdict,
    reads,
    reset_cache,
    server_accepts_images,
    solid_png,
    text_png,
)
from saddle.vllm import StreamToken, ToolCall, VllmAuthError, VllmClient, VllmRequestError
from saddle.web.app import history_for_display

RED = solid_png(16, 16, (255, 0, 0))
BLUE = solid_png(16, 16, (0, 0, 255))
PROBE_URL = data_url("image/png", text_png(PROBE_TEXT))


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    reset_cache()
    yield
    reset_cache()


def word(text: str) -> list[Any]:
    return [StreamToken(stream="content", text=text)]


def call(name: str, cid: str = "c1", **arguments: Any) -> ToolCall:
    return ToolCall(id=cid, name=name, arguments=json.dumps(arguments))


class Server:
    """A scripted server. A probe (it carries `PROBE_QUESTION`) is answered the way a
    model that sees images, or one that does not, would; anything else replays rounds."""

    def __init__(self, rounds: list[list[Any]], *, sees: bool = True, key: str = "s") -> None:
        self.rounds = list(rounds)
        self.sees = sees
        self.server_key = key
        self.probes = 0
        self.offered: Any = None
        self.system: Any = None
        self.asked: list[list[dict[str, Any]]] = []

    def stream_chat(self, messages: Any, **kwargs: Any) -> Any:
        self.offered = kwargs.get("tools")
        self.system = messages[0]["content"]
        content = messages[0]["content"]
        if isinstance(content, list) and content[0].get("text") == PROBE_QUESTION:
            self.probes += 1
            url = content[1]["image_url"]["url"]
            answer = (
                f"The text reads {PROBE_TEXT.lower()}."
                if self.sees and url == PROBE_URL
                else "none"
            )
            # reasoning arrives first and is not the answer: the text in it must not count
            thought = [StreamToken(stream="reasoning", text=f"is it {PROBE_TEXT}?")]
            return iter([*thought, *word(answer)])
        self.asked.append([dict(m) for m in messages])
        return iter(self.rounds.pop(0) if self.rounds else word("ok"))


def turn(server: Any, workdir: Path, ctx: ToolContext | None = None) -> list[Event]:
    options = TurnOptions(workdir=workdir, journal=workdir / "j.jsonl")
    context = ctx or ToolContext(workdir=workdir, images=True)
    messages: list[dict[str, Any]] = []
    return list(
        run_turn(cast(VllmClient, server), messages, "look", options, turn=1, context=context)
    )


def result_of(events: list[Event]) -> str:
    return next(e for e in events if isinstance(e, ToolEnd)).detail


def test_a_png_reaches_the_model_as_image_content_after_the_tool_result(tmp_path: Path) -> None:
    (tmp_path / "shot.png").write_bytes(RED)
    server = Server([[call("read_file", path="shot.png")], word("It is red.")])
    events = turn(server, tmp_path)
    seen = server.asked[1]
    tool_at = next(i for i, m in enumerate(seen) if m["role"] == "tool")
    assert "PNG image, 16x16" in seen[tool_at]["content"]
    assert f"{len(RED):,} bytes" in seen[tool_at]["content"]
    followup = seen[tool_at + 1]
    assert followup["role"] == "user"
    assert is_image_followup(followup)
    assert followup["content"][0]["text"] == f"{IMAGE_FOLLOWUP}c1 (shot.png):"
    assert followup["content"][1] == {
        "type": "image_url",
        "image_url": {"url": data_url("image/png", RED)},
    }
    assert "PNG image" in result_of(events)


def test_two_images_in_one_round_follow_all_the_tool_results(tmp_path: Path) -> None:
    (tmp_path / "a.png").write_bytes(RED)
    (tmp_path / "b.png").write_bytes(BLUE)
    server = Server(
        [[call("read_file", "c1", path="a.png"), call("read_file", "c2", path="b.png")], word("ok")]
    )
    turn(server, tmp_path)
    roles = [m["role"] for m in server.asked[1]]
    assert roles[-3:] == ["tool", "tool", "user"]
    urls = [p["image_url"]["url"] for p in server.asked[1][-1]["content"] if "image_url" in p]
    assert urls == [data_url("image/png", RED), data_url("image/png", BLUE)]


def test_a_server_that_cannot_see_is_named_and_gets_no_image(tmp_path: Path) -> None:
    (tmp_path / "shot.png").write_bytes(RED)
    server = Server([[call("read_file", path="shot.png")], word("ok")], sees=False)
    events = turn(server, tmp_path)
    assert "does not accept images" in result_of(events)
    assert not any(is_image_followup(m) for m in server.asked[1])


def test_support_is_asked_once_per_server_and_remembered(tmp_path: Path) -> None:
    (tmp_path / "shot.png").write_bytes(RED)
    server = Server([[call("read_file", path="shot.png")], word("ok")] * 2)
    turn(server, tmp_path)
    assert server.probes == 1  # one picture of text
    turn(server, tmp_path)
    assert server.probes == 1  # not again


def _pixels(png: bytes) -> list[bytes]:
    """The RGB rows of a PNG `text_png` drew (filter byte 0 on every row)."""
    width, height = struct.unpack(">II", png[16:24])
    start = png.index(b"IDAT") + 4
    length = struct.unpack(">I", png[start - 8 : start - 4])[0]
    raw = zlib.decompress(png[start : start + length])
    stride = 1 + 3 * width
    return [raw[y * stride + 1 : (y + 1) * stride] for y in range(height)]


def test_the_probe_picture_draws_each_letter_of_its_text() -> None:
    """Known-good: every cell of each glyph is black where its bitmap is inked
    and white where not, at its place in the line. Known-bad: drawing another
    text gives another picture, so the answer depends on what was drawn."""
    scale = 2
    rows = _pixels(text_png(PROBE_TEXT, scale))
    for index, letter in enumerate(PROBE_TEXT):
        for cy, line in enumerate(GLYPHS[letter]):
            for col, mark in enumerate(line):
                x = (2 + index * 6 + col) * scale
                y = (2 + cy) * scale
                assert rows[y][3 * x : 3 * x + 3] == (b"\0\0\0" if mark == "X" else b"\xff\xff\xff")
    margin = rows[0]
    assert set(margin) == {255}  # two blank cells above the text
    assert text_png("K7", scale) != text_png("7K", scale)
    info = image_info(text_png(PROBE_TEXT))
    assert info is not None
    assert (info.width, info.height) == ((6 * 6 + 3) * 8, 11 * 8)


def test_reading_back_means_the_text_once_case_and_punctuation_aside() -> None:
    assert reads("K7PM3X")
    assert reads("The text says: k7pm-3x.")
    assert not reads("none")
    assert not reads("K7PM3K")  # one letter misread
    assert not reads("K7PM")
    assert not reads("K7PM3X, or maybe K7PM3X")  # said twice: not one reading


def test_the_probe_needs_the_text_read_back() -> None:
    class Colours:
        """Sees the picture's colour, cannot read it: the old probe's yes."""

        server_key = "c"

        def stream_chat(self, messages: Any, **_: Any) -> Any:
            return iter(word("black and white"))

    assert server_accepts_images(Colours()) is False

    class Reads:
        server_key = "r"
        shown: Any = None

        def stream_chat(self, messages: Any, **_: Any) -> Any:
            self.shown = messages[0]["content"][1]["image_url"]["url"]
            return iter(word(PROBE_TEXT))

    reader = Reads()
    assert known_verdict(reader) is None
    assert server_accepts_images(reader) is True
    assert reader.shown == PROBE_URL
    assert known_verdict(reader) is True


def test_a_card_that_declares_no_images_is_a_no_without_a_request() -> None:
    class TextOnly:
        server_key = "t"
        asked = 0

        def declares_images(self) -> bool | None:
            return False

        def stream_chat(self, messages: Any, **_: Any) -> Any:
            self.asked += 1
            return iter(word(PROBE_TEXT))

    text_only = TextOnly()
    assert server_accepts_images(text_only) is False
    assert text_only.asked == 0

    class Silent(TextOnly):
        server_key = "s"

        def declares_images(self) -> bool | None:
            return None  # the card says nothing: ask the model

    silent = Silent()
    assert server_accepts_images(silent) is True
    assert silent.asked == 1

    class NoCard(TextOnly):
        server_key = "n"

        def declares_images(self) -> bool | None:
            msg = "server returned HTTP 404"
            raise VllmRequestError(msg)

    no_card = NoCard()
    assert server_accepts_images(no_card) is True  # a card it cannot read decides nothing
    assert no_card.asked == 1


def test_a_refusing_server_is_a_remembered_no_but_a_failure_is_not() -> None:
    class Refuses:
        server_key = "r"
        asked = 0

        def stream_chat(self, messages: Any, **_: Any) -> Any:
            self.asked += 1
            msg = "server returned HTTP 400: images unsupported"
            raise VllmRequestError(msg)

    class Flaky:
        server_key = "f"
        asked = 0

        def stream_chat(self, messages: Any, **_: Any) -> Any:
            self.asked += 1
            msg = "request failed: timeout"
            raise VllmRequestError(msg)

    refuses, flaky = Refuses(), Flaky()
    assert server_accepts_images(refuses) is False
    assert server_accepts_images(refuses) is False
    assert refuses.asked == 1
    for _ in range(2):
        with pytest.raises(VllmRequestError):
            server_accepts_images(flaky)
    assert flaky.asked == 2  # a transient failure is not cached as "cannot see"


def test_an_auth_failure_is_not_a_no() -> None:
    class Denied:
        server_key = "d"

        def stream_chat(self, messages: Any, **_: Any) -> Any:
            msg = "server rejected the API key (HTTP 401)"
            raise VllmAuthError(msg)

    with pytest.raises(VllmAuthError):
        server_accepts_images(Denied())


def test_a_check_that_fails_says_so_in_the_result(tmp_path: Path) -> None:
    (tmp_path / "shot.png").write_bytes(RED)

    def broken() -> bool:
        msg = "request failed: timeout"
        raise VllmRequestError(msg)

    ctx = ToolContext(workdir=tmp_path, images=True, accepts_images=broken)
    out = execute_tool(call("read_file", path="shot.png"), workdir=tmp_path, context=ctx)
    assert "could not be checked" in out
    assert "timeout" in out
    assert ctx.attachments == []


def test_images_on_with_no_way_to_ask_says_the_model_was_not_shown(tmp_path: Path) -> None:
    (tmp_path / "shot.png").write_bytes(RED)
    ctx = ToolContext(workdir=tmp_path, images=True)
    out = execute_tool(call("read_file", path="shot.png"), workdir=tmp_path, context=ctx)
    assert "does not accept images" in out
    assert ctx.attachments == []


def test_an_image_over_the_limit_is_refused_by_name_and_one_at_it_is_shown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "shot.png").write_bytes(RED)
    ctx = ToolContext(workdir=tmp_path, images=True, accepts_images=lambda: True)
    monkeypatch.setattr(tools_module, "IMAGE_MAX_BYTES", len(RED))
    ok = execute_tool(call("read_file", path="shot.png"), workdir=tmp_path, context=ctx)
    assert "PNG image" in ok
    assert len(ctx.attachments) == 1
    monkeypatch.setattr(tools_module, "IMAGE_MAX_BYTES", len(RED) - 1)
    ctx.attachments.clear()
    big = execute_tool(call("read_file", path="shot.png"), workdir=tmp_path, context=ctx)
    assert big.startswith("error: shot.png is an image")
    assert "limit" in big
    assert ctx.attachments == []


def test_a_text_file_named_png_is_read_as_text(tmp_path: Path) -> None:
    (tmp_path / "notes.png").write_text("not really a picture\n")
    ctx = ToolContext(workdir=tmp_path, images=True, accepts_images=lambda: True)
    out = execute_tool(call("read_file", path="notes.png"), workdir=tmp_path, context=ctx)
    assert out == "not really a picture\n"
    assert ctx.attachments == []


def test_an_image_outside_the_folder_is_refused_as_any_read_is(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    (tmp_path / "shot.png").write_bytes(RED)
    ctx = ToolContext(workdir=work, images=True, accepts_images=lambda: True)
    out = execute_tool(call("read_file", path="../shot.png"), workdir=work, context=ctx)
    assert out.startswith("error: ")
    assert ctx.attachments == []


def test_without_images_on_read_file_is_what_it_was(tmp_path: Path) -> None:
    (tmp_path / "shot.png").write_bytes(RED)
    ctx = ToolContext(workdir=tmp_path, accepts_images=lambda: True)
    assert ctx.images is False
    out = execute_tool(call("read_file", path="shot.png"), workdir=tmp_path, context=ctx)
    assert out == (tmp_path / "shot.png").read_text(encoding="utf-8", errors="replace")
    assert ctx.attachments == []


def _task_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "shot.png").write_bytes(RED)
    for args in (
        ["init", "-q", "-b", "main"],
        ["add", "-A"],
        ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "i"],
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    return repo


def _started(repo: Path, run_id: str) -> str:
    """The run's `auto:start` span detail, where its settings are recorded."""
    journal = next((repo / ".saddle" / "runs" / run_id).glob("proofs.jsonl"))
    for line in journal.read_text().splitlines():
        record = json.loads(line)
        if record.get("argv", [""])[0] == "auto:start":
            return str(record["detail"])
    msg = "no auto:start span"
    raise AssertionError(msg)


def test_a_task_run_with_the_images_switch_on_shows_the_image_and_records_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SADDLE_CAPABILITIES", "images")
    repo = _task_repo(tmp_path)
    server = Server(
        [[call("read_file", "c1", path="shot.png")], [call("finish", "c2", summary="d")]]
    )
    run_auto(AutoOptions(task="t", repo=repo, run_id="r1", arm="E"), cast(VllmClient, server))
    assert server.probes == 1
    followup = server.asked[1][-1]
    assert is_image_followup(followup)
    assert followup["content"][1]["image_url"]["url"] == data_url("image/png", RED)
    assert "; images on" in _started(repo, "r1")


def test_a_broken_switch_file_refuses_a_task_run_before_it_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SADDLE_CAPABILITIES", "imagez")
    repo = _task_repo(tmp_path)
    with pytest.raises(AutoError, match="not a capability name"):
        run_auto(
            AutoOptions(task="t", repo=repo, run_id="r1", arm="E"), cast(VllmClient, Server([]))
        )
    assert not (repo / ".saddle" / "runs" / "r1").exists()


def test_a_task_run_does_not_turn_images_on(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "shot.png").write_bytes(RED)
    for args in (
        ["init", "-q", "-b", "main"],
        ["add", "-A"],
        ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "i"],
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    server = Server(
        [[call("read_file", "c1", path="shot.png")], [call("finish", "c2", summary="d")]]
    )
    run_auto(AutoOptions(task="t", repo=repo, run_id="r1", arm="E"), cast(VllmClient, server))
    seen = server.asked[1]
    tool = next(m for m in seen if m["role"] == "tool")
    assert tool["content"] == (repo / "shot.png").read_text(encoding="utf-8", errors="replace")
    assert not any(is_image_followup(m) for m in seen)
    assert server.probes == 0
    # nothing about images was added to what the Task model is offered or told
    assert "image" not in json.dumps(server.offered).lower()
    assert "image" not in str(server.system).lower()
    assert "; images off" in _started(repo, "r1")


def test_the_page_does_not_show_the_image_message_as_the_persons(tmp_path: Path) -> None:
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "look"},
        {"role": "user", "content": [{"type": "text", "text": f"{IMAGE_FOLLOWUP}c1 (a.png):"}]},
        {"role": "user", "content": "next"},
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        {"role": "user", "content": []},
    ]
    shown = history_for_display(messages, tmp_path)
    assert [m["index"] for m in shown] == [0, 2, 3, 4]  # indices still name stored positions


def webp(chunk: bytes, body: bytes) -> bytes:
    return b"RIFF\0\0\0\0WEBP" + chunk + body


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (RED, ("image/png", "PNG", 16, 16)),
        (b"GIF89a" + struct.pack("<HH", 30, 20) + b"\0" * 4, ("image/gif", "GIF", 30, 20)),
        (
            webp(b"VP8X", b"\0" * 8 + (99).to_bytes(3, "little") + (49).to_bytes(3, "little")),
            ("image/webp", "WebP", 100, 50),
        ),
        (
            webp(b"VP8L", b"\0" * 5 + struct.pack("<I", (39 << 14) | 79)),
            ("image/webp", "WebP", 80, 40),
        ),
        (
            webp(b"VP8 ", b"\0" * 10 + struct.pack("<HH", 64, 48)),
            ("image/webp", "WebP", 64, 48),
        ),
        (
            b"\xff\xd8\xff\xe0"
            + struct.pack(">H", 4)
            + b"\0\0"
            + b"\xff\xc0"
            + struct.pack(">HBHH", 11, 8, 70, 90)
            + b"\0" * 4,
            ("image/jpeg", "JPEG", 90, 70),
        ),
    ],
)
def test_image_info_reads_each_format_by_its_header(
    data: bytes, expected: tuple[str, str, int, int]
) -> None:
    info = image_info(data)
    assert info is not None
    assert (info.mime, info.kind, info.width, info.height) == expected


@pytest.mark.parametrize(
    "data",
    [
        b"just some text",
        b"",
        RED[:20],
        b"\x89PNG\r\n\x1a\n" + b"\0" * 20,  # a PNG signature with no IHDR
        b"GIF89a",
        webp(b"XXXX", b"\0" * 20),
        webp(b"VP8X", b""),
        b"\xff\xd8\xff\xe0" + struct.pack(">H", 4) + b"\0\0" + b"\xff\xc4" + b"\0\x02" * 3,
        b"\xff\xd8" + b"\0" * 12,
    ],
)
def test_image_info_refuses_what_is_not_a_readable_image(data: bytes) -> None:
    assert image_info(data) is None


def test_a_client_names_its_server_and_model_for_the_cache() -> None:
    one = VllmClient(api_key="k", base_url="http://a.test:1/v1", model="m1")
    two = VllmClient(api_key="k", base_url="http://a.test:1/v1", model="m2")
    other = VllmClient(api_key="k", base_url="http://b.test:1/v1", model="m1")
    assert len({one.server_key, two.server_key, other.server_key}) == 3
    assert "a.test" in one.server_key
    assert "m1" in one.server_key
