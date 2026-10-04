"""The compositor's own pointer, spoken over the Wayland wire protocol.

Under a Wayland compositor only the compositor moves windows, and it does so
with the real pointer; X input cannot. Live (labwc), the virtual-pointer
protocol moved a dialog by its title bar. The contract: `WaylandPointer`
finds `zwlr_virtual_pointer_manager_v1` in the registry, binds it, creates a
pointer, and sends absolute moves and button presses, each followed by a
frame; a compositor without the protocol, one that refuses a request, or one
that hangs up is a WaylandError, never a silent no-op. A fake compositor on
a real socket answers here: no test reaches the person's session.
"""

from __future__ import annotations

import socket
import struct
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from saddle import wlpointer
from saddle.wlpointer import (
    MANAGER,
    WaylandError,
    WaylandPointer,
    events,
    global_name,
    message,
    string,
)
from saddle.wlpointer import socket_path as real_socket_path


def announce(name: int, interface: str, version: int) -> bytes:
    """A `wl_registry.global` event (registry is object 2)."""
    return message(2, 0, struct.pack("<I", name) + string(interface) + struct.pack("<I", version))


class Compositor:
    """Answers one client: the registry it is given, then each sync with its
    callback's done; records every request. `error` answers the first sync with
    a protocol error; `hang_up` closes instead of answering."""

    def __init__(
        self,
        sock: socket.socket,
        globals_: list[tuple[int, str]],
        *,
        error: bool = False,
        hang_up: bool = False,
    ) -> None:
        self.sock, self.globals, self.error, self.hang_up = sock, globals_, error, hang_up
        self.requests: list[tuple[int, int, bytes]] = []
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self) -> None:
        buffer = b""
        while True:
            try:
                chunk = self.sock.recv(65536)
            except OSError:
                return
            if not chunk:
                return
            found, buffer = events(buffer + chunk)
            for obj, opcode, body in found:
                self.requests.append((obj, opcode, body))
                if (obj, opcode) == (1, 1):  # get_registry
                    for name, interface in self.globals:
                        self.sock.sendall(announce(name, interface, 2))
                if (obj, opcode) == (1, 0):  # sync
                    if self.hang_up:
                        self.sock.close()
                        return
                    if self.error:
                        self.sock.sendall(message(1, 0, struct.pack("<II", 4, 0) + string("no")))
                        continue
                    (callback,) = struct.unpack("<I", body[:4])
                    self.sock.sendall(message(callback, 0, struct.pack("<I", 0)))


@pytest.fixture
def pair() -> Iterator[tuple[socket.socket, socket.socket]]:
    client, server = socket.socketpair()
    client.settimeout(5)
    yield client, server
    client.close()
    server.close()


def test_the_pointer_binds_the_manager_and_creates_itself(
    pair: tuple[socket.socket, socket.socket],
) -> None:
    client, server = pair
    fake = Compositor(server, [(11, "wl_seat"), (12, MANAGER)])
    pointer = WaylandPointer(client, clock=lambda: 1.5)
    pointer.open()
    pointer.move(10, 20, (1280, 720))
    pointer.button("left", pressed=True)
    pointer.button("right", pressed=False)
    pointer.close()
    fake.thread.join(timeout=5)
    asked = [(obj, opcode) for obj, opcode, _ in fake.requests]
    assert asked == [
        (1, 1),  # get_registry
        (1, 0),  # sync
        (2, 0),  # bind the manager
        (4, 0),  # create the pointer
        (5, 1),
        (5, 4),  # motion_absolute, frame
        (5, 2),
        (5, 4),  # button, frame
        (5, 2),
        (5, 4),
        (5, 8),  # destroy
        (1, 0),  # the closing sync
    ]
    bind = fake.requests[2][2]
    assert bind == struct.pack("<I", 12) + string(MANAGER) + struct.pack("<II", 1, 4)
    assert fake.requests[4][2] == struct.pack("<IIIII", 1500, 10, 20, 1280, 720)
    assert fake.requests[6][2] == struct.pack("<III", 1500, 0x110, 1)
    assert fake.requests[8][2] == struct.pack("<III", 1500, 0x111, 0)


@pytest.mark.parametrize(
    ("globals_", "kwargs", "said"),
    [
        ([(11, "wl_seat")], {}, f"this compositor does not offer {MANAGER}"),
        ([(12, MANAGER)], {"error": True}, "the Wayland compositor refused a pointer request"),
        ([(12, MANAGER)], {"hang_up": True}, "the Wayland compositor closed the connection"),
    ],
)
def test_a_compositor_that_cannot_give_a_pointer_is_an_error_never_a_no_op(
    pair: tuple[socket.socket, socket.socket],
    globals_: list[tuple[int, str]],
    kwargs: dict[str, bool],
    said: str,
) -> None:
    client, server = pair
    Compositor(server, globals_, **kwargs)
    with pytest.raises(WaylandError, match=said):
        WaylandPointer(client).open()


def test_the_wire_helpers_frame_pad_and_split() -> None:
    assert message(5, 4) == struct.pack("<II", 5, (8 << 16) | 4)
    assert string("abc") == struct.pack("<I", 4) + b"abc\0"
    assert string("abcd") == struct.pack("<I", 5) + b"abcd\0\0\0\0"
    two = message(2, 0, b"\1\0\0\0") + message(3, 0)
    found, rest = events(two + b"\x09\x00")
    assert found == [(2, 0, b"\1\0\0\0"), (3, 0, b"")]
    assert rest == b"\x09\x00"
    assert events(struct.pack("<II", 1, (4 << 16) | 0))[0] == []  # a size below the header
    body = struct.pack("<I", 7) + string(MANAGER) + struct.pack("<I", 2)
    assert global_name(body, MANAGER) == 7
    assert global_name(body, "wl_seat") is None


def test_the_socket_is_found_from_the_session(tmp_path: Path) -> None:
    assert real_socket_path({}) is None
    assert real_socket_path({"XDG_RUNTIME_DIR": "/run/user/1"}) is None
    env = {"XDG_RUNTIME_DIR": "/run/user/1", "WAYLAND_DISPLAY": "wayland-0"}
    assert real_socket_path(env) == Path("/run/user/1/wayland-0")
    absolute = {"XDG_RUNTIME_DIR": "/x", "WAYLAND_DISPLAY": str(tmp_path / "w")}
    assert real_socket_path(absolute) == tmp_path / "w"


def test_connect_reaches_a_listening_compositor_or_says_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(WaylandError, match="no Wayland compositor is running"):
        WaylandPointer.connect({})  # the suite's guard: no session socket
    path = tmp_path / "wayland-test"
    monkeypatch.setattr(wlpointer, "socket_path", lambda env: path)
    with pytest.raises(WaylandError, match="no Wayland compositor is running"):
        WaylandPointer.connect({})  # nothing there yet
    path.write_text("not a socket")
    with pytest.raises(WaylandError, match="could not be reached"):
        WaylandPointer.connect({})
    path.unlink()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)
    served: list[Compositor] = []

    def accept(globals_: list[tuple[int, str]]) -> None:
        conn, _ = listener.accept()
        served.append(Compositor(conn, globals_))

    try:
        thread = threading.Thread(target=accept, args=([(12, MANAGER)],), daemon=True)
        thread.start()
        pointer = WaylandPointer.connect({})
        pointer.close()
        thread.join(timeout=5)
        thread = threading.Thread(target=accept, args=([(11, "wl_seat")],), daemon=True)
        thread.start()
        with pytest.raises(WaylandError, match="does not offer"):
            WaylandPointer.connect({})  # and the socket is closed, not leaked
        thread.join(timeout=5)
    finally:
        listener.close()
    assert [obj for obj, _, _ in served[0].requests][-1] == 1  # the closing sync
