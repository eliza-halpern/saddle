"""A chat session's tools state what is true for that session (trial findings F10-F15).

Known-good: through the web chat's real turn path, a session where a fact holds
is offered the sentence that states it -- in a tool description, which is sent
with every request.

Known-bad: a session where it does not hold (no display, no full access, images
off or unseen by the server, no process tool, the Ask lane) is not told it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import pytest
from test_chat_server import ScriptedClient, _server_of, engine_app

from saddle.sessions import FULL_ACCESS_CONFIRM, SessionStore
from saddle.vllm import StreamToken, VllmError

CD = "`cd` does not carry over to the next command"
DISPLAY = "opens its window on their screen"
IMAGE = "An image file (PNG, JPEG, WebP, GIF) is shown to you"
KILL = "stop them with it, never with pkill or killall -f"
ASK_THEM = "ask them and end your turn instead of guessing"


class Recording(ScriptedClient):
    asked: ClassVar[list[list[dict[str, Any]]]] = []

    def stream_chat(self, _messages: Any, **kw: Any) -> Any:
        Recording.asked.append(kw["tools"])
        return super().stream_chat(_messages, **kw)


def offered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    mode: str = "edit",
    full: bool = False,
    display: bool = True,
    images_ok: bool | Exception = True,
    processes: bool = True,
) -> dict[str, str]:
    """The tool descriptions the model is sent on a web chat's first request."""
    if display:
        monkeypatch.setenv("DISPLAY", ":0")
    else:
        for name in ("DISPLAY", "WAYLAND_DISPLAY"):
            monkeypatch.delenv(name, raising=False)

    def probe(_client: Any) -> bool:
        if isinstance(images_ok, Exception):
            raise images_ok
        return images_ok

    monkeypatch.setattr("saddle.engine.server_accepts_images", probe)
    Recording.asked = []
    store = SessionStore(tmp_path / "s")
    with engine_app(store, tmp_path, [[StreamToken(stream="content", text="ok")]]) as (
        client,
        app,
    ):
        server = _server_of(app)
        server.client_factory = Recording
        sid = client.post("/api/sessions", json={"workdir": str(tmp_path)}).json()["id"]
        client.patch(f"/api/sessions/{sid}", json={"mode": mode})
        if full:
            client.post(
                f"/api/sessions/{sid}/full-access",
                json={"on": True, "confirm": FULL_ACCESS_CONFIRM},
            )
        if not processes:
            server.ledger = lambda _sid: None  # type: ignore[method-assign,assignment,return-value]
        server._run(sid, "hello")
    return {t["function"]["name"]: t["function"]["description"] for t in Recording.asked[0]}


def test_a_chat_edit_session_is_told_cd_does_not_carry_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert CD in offered(tmp_path, monkeypatch)["run_command"]


def test_the_ask_lane_has_no_commands_and_so_no_such_sentence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = offered(tmp_path, monkeypatch, mode="ask")
    assert "run_command" not in tools
    assert not any(CD in text or ASK_THEM in text for text in tools.values())


def test_the_person_is_to_be_asked_what_only_they_can_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert ASK_THEM in offered(tmp_path, monkeypatch)["run_command"]


def test_full_access_with_a_display_says_windows_open_on_their_screen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert DISPLAY in offered(tmp_path, monkeypatch, full=True)["run_command"]


@pytest.mark.parametrize(("full", "display"), [(False, True), (True, False)])
def test_no_full_access_or_no_display_is_not_told_windows_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, full: bool, display: bool
) -> None:
    assert DISPLAY not in offered(tmp_path, monkeypatch, full=full, display=display)["run_command"]


def test_an_image_is_shown_when_images_are_on_and_the_server_accepts_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert IMAGE in offered(tmp_path, monkeypatch)["read_file"]


def test_an_image_the_server_cannot_see_is_not_promised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert IMAGE not in offered(tmp_path, monkeypatch, images_ok=False)["read_file"]


def test_the_process_tool_is_named_as_the_way_to_stop_programs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = offered(tmp_path, monkeypatch)
    assert "processes" in tools
    assert KILL in tools["run_command"]


def test_without_the_process_tool_nothing_points_at_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = offered(tmp_path, monkeypatch, processes=False)
    assert "processes" not in tools
    assert KILL not in tools["run_command"]


def test_a_probe_that_fails_states_nothing_about_images(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = offered(tmp_path, monkeypatch, images_ok=VllmError("server unreachable"))
    assert IMAGE not in tools["read_file"]
