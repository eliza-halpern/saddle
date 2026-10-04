"""A pointer driven through the Wayland compositor (`computer` clicks and drags).

X input tools only steer the X world: under a Wayland compositor they cannot
move a window (the compositor moves windows with the real pointer), and the
first press after the pointer comes in from a native window can be lost. A
wlroots compositor (labwc, sway, cage; also headless, `WLR_BACKENDS=headless`,
as a server without a screen would run one) offers the virtual-pointer
protocol, `zwlr_virtual_pointer_manager_v1`, which drives the session's real
pointer. Live, a drag through it moved a dialog by its title bar.

This is a client for just that protocol, written against the Wayland wire
format directly so it needs no library and no build: each message is the
object id, then the size and opcode in one 32-bit word, then the arguments,
32-bit little-endian, strings length-prefixed and padded to four bytes.
"""

from __future__ import annotations

import os
import socket
import struct
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Final

MANAGER: Final = "zwlr_virtual_pointer_manager_v1"
BUTTONS: Final = {"left": 0x110, "right": 0x111}
"""Linux input codes (BTN_LEFT, BTN_RIGHT), as the protocol takes them."""

DISPLAY_ID: Final = 1
_REGISTRY: Final = 2
_SYNC: Final = 3
_MANAGER_ID: Final = 4
_POINTER: Final = 5
_LAST_SYNC: Final = 6

# Requests (object, opcode) from the protocol XML.
_GET_REGISTRY: Final = 1
_DISPLAY_SYNC: Final = 0
_BIND: Final = 0
_CREATE_POINTER: Final = 0
_MOTION_ABSOLUTE: Final = 1
_BUTTON: Final = 2
_FRAME: Final = 4
_DESTROY_POINTER: Final = 8
# Events.
_GLOBAL: Final = 0
_DISPLAY_ERROR: Final = 0


class WaylandError(Exception):
    """The compositor could not be reached, refused a request, or lacks the protocol."""


def socket_path(env: Mapping[str, str]) -> Path | None:
    """Where the session's compositor listens, or None outside a Wayland session."""
    runtime, display = env.get("XDG_RUNTIME_DIR"), env.get("WAYLAND_DISPLAY")
    if not runtime or not display:
        return None
    return Path(display) if display.startswith("/") else Path(runtime) / display


def message(obj: int, opcode: int, payload: bytes = b"") -> bytes:
    """One request on the wire."""
    return struct.pack("<II", obj, ((8 + len(payload)) << 16) | opcode) + payload


def string(text: str) -> bytes:
    """A Wayland string: length with the NUL, the bytes, padding to four."""
    data = text.encode() + b"\0"
    return struct.pack("<I", len(data)) + data + b"\0" * (-len(data) % 4)


def events(data: bytes) -> tuple[list[tuple[int, int, bytes]], bytes]:
    """The whole messages in `data` as (object, opcode, body), and the rest."""
    found: list[tuple[int, int, bytes]] = []
    while len(data) >= 8:
        obj, word = struct.unpack("<II", data[:8])
        size = word >> 16
        if size < 8 or len(data) < size:
            break
        found.append((obj, word & 0xFFFF, data[8:size]))
        data = data[size:]
    return found, data


def global_name(body: bytes, interface: str) -> int | None:
    """The registry name in a `wl_registry.global` body when it announces
    `interface`, else None."""
    name, length = struct.unpack("<II", body[:8])
    return name if body[8 : 8 + length - 1].decode(errors="replace") == interface else None


class WaylandPointer:
    """One virtual pointer on the session's seat. `connect` opens it; every
    position is absolute, in the compositor's layout, scaled by `extent`."""

    def __init__(self, sock: socket.socket, clock: Callable[[], float] = time.monotonic) -> None:
        self.sock = sock
        self.clock = clock
        self._buffer = b""

    @classmethod
    def connect(cls, env: Mapping[str, str]) -> WaylandPointer:
        """The session's pointer, or WaylandError when there is no compositor
        or it does not offer `MANAGER`."""
        path = socket_path(env)
        if path is None or not path.exists():
            msg = "no Wayland compositor is running in this session"
            raise WaylandError(msg)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(5)
        try:
            sock.connect(os.fspath(path))
        except OSError as exc:
            sock.close()
            msg = f"the Wayland compositor could not be reached ({exc})"
            raise WaylandError(msg) from exc
        pointer = cls(sock)
        try:
            pointer.open()
        except (OSError, WaylandError):
            sock.close()
            raise
        return pointer

    def _send(self, obj: int, opcode: int, payload: bytes = b"") -> None:
        self.sock.sendall(message(obj, opcode, payload))

    def _roundtrip(self, callback: int) -> list[tuple[int, int, bytes]]:
        """Every event up to the answer to a `wl_display.sync`; a protocol error
        raises WaylandError."""
        self._send(DISPLAY_ID, _DISPLAY_SYNC, struct.pack("<I", callback))
        seen: list[tuple[int, int, bytes]] = []
        while True:
            chunk = self.sock.recv(65536)
            if not chunk:
                msg = "the Wayland compositor closed the connection"
                raise WaylandError(msg)
            found, self._buffer = events(self._buffer + chunk)
            for obj, opcode, body in found:
                if obj == callback:
                    return seen
                if obj == DISPLAY_ID and opcode == _DISPLAY_ERROR:
                    msg = "the Wayland compositor refused a pointer request"
                    raise WaylandError(msg)
                seen.append((obj, opcode, body))

    def open(self) -> None:
        """Find `MANAGER` and create the pointer, or raise WaylandError."""
        self._send(DISPLAY_ID, _GET_REGISTRY, struct.pack("<I", _REGISTRY))
        names = [
            name
            for obj, opcode, body in self._roundtrip(_SYNC)
            if obj == _REGISTRY and opcode == _GLOBAL
            for name in [global_name(body, MANAGER)]
            if name is not None
        ]
        if not names:
            msg = f"this compositor does not offer {MANAGER}"
            raise WaylandError(msg)
        bind = struct.pack("<I", names[0]) + string(MANAGER) + struct.pack("<II", 1, _MANAGER_ID)
        self._send(_REGISTRY, _BIND, bind)
        self._send(_MANAGER_ID, _CREATE_POINTER, struct.pack("<II", 0, _POINTER))

    def _time(self) -> int:
        return int(self.clock() * 1000) & 0xFFFFFFFF

    def move(self, x: int, y: int, extent: tuple[int, int]) -> None:
        """Put the pointer at (x, y) of a layout `extent` wide and high."""
        args = struct.pack("<IIIII", self._time(), x, y, extent[0], extent[1])
        self._send(_POINTER, _MOTION_ABSOLUTE, args)
        self._send(_POINTER, _FRAME)

    def button(self, button: str, *, pressed: bool) -> None:
        """Press or release `button` (`BUTTONS`)."""
        args = struct.pack("<III", self._time(), BUTTONS[button], 1 if pressed else 0)
        self._send(_POINTER, _BUTTON, args)
        self._send(_POINTER, _FRAME)

    def close(self) -> None:
        """Remove the pointer, wait until the compositor has every request, and
        hang up (so nothing is still in flight when the caller looks)."""
        try:
            self._send(_POINTER, _DESTROY_POINTER)
            self._roundtrip(_LAST_SYNC)
        finally:
            self.sock.close()
