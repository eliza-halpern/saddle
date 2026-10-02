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
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

import saddle.tools as tools_module
from saddle.auto import AutoOptions, run_auto
from saddle.engine import TurnOptions, run_turn
from saddle.events import Event, ToolEnd
from saddle.tools import ToolContext, execute_tool
from saddle.vision import (
    IMAGE_FOLLOWUP,
    PROBE_QUESTION,
    data_url,
    image_info,
    is_image_followup,
    reset_cache,
    server_accepts_images,
    solid_png,
)
from saddle.vllm import StreamToken, ToolCall, VllmAuthError, VllmClient, VllmRequestError
from saddle.web.app import history_for_display

RED = solid_png(16, 16, (255, 0, 0))
BLUE = solid_png(16, 16, (0, 0, 255))
COLOURS = {data_url("image/png", RED): "Red", data_url("image/png", BLUE): "Blue"}


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
            answer = COLOURS.get(url, "?") if self.sees else "Green"
            # reasoning arrives first and is not the answer: "red" in it must not count
            thought = [StreamToken(stream="reasoning", text="not red, not blue")]
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
    assert server.probes == 2  # one red, one blue
    turn(server, tmp_path)
    assert server.probes == 2  # not again


def test_the_probe_needs_both_colours_right() -> None:
    class Always:
        server_key = "k"

        def stream_chat(self, messages: Any, **_: Any) -> Any:
            return iter(word("red"))

    assert server_accepts_images(Always()) is False  # answers red to blue too

    class Hedges:
        server_key = "h"

        def stream_chat(self, messages: Any, **_: Any) -> Any:
            return iter(word("red or blue"))  # names both, so it has told us nothing

    assert server_accepts_images(Hedges()) is False


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
