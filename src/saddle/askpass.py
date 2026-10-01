"""A person's password for a command's `sudo`, under full access (#125).

`sudo` reads a password from a terminal, and a model's commands have none,
so `sudo` fails. Its own answer to that is `-A`: run `SUDO_ASKPASS` and read
the password from its output. While a session has full access, its commands
find a `sudo` on PATH that runs the real one with `-A`, and `SUDO_ASKPASS`
names a helper that asks saddle, over a socket in a private (0700)
directory, and saddle asks the person.

The password goes from the person to the helper to `sudo`, and nowhere
else: it is never returned to the model, never put in an event, a message, a
span or a log. `ask` returning None (the person cancelled, or nobody
answered) makes the helper exit 1, and `sudo` fails as if no password was
given. Inside the sandbox none of this exists: there `sudo` cannot elevate.
"""

from __future__ import annotations

import json
import shlex
import shutil
import socket
import sys
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Final

HELPER: Final = """
import json, os, socket, sys
conn = socket.socket(socket.AF_UNIX)
conn.connect(os.environ["SADDLE_ASKPASS_SOCKET"])
conn.sendall((json.dumps({"prompt": " ".join(sys.argv[1:])}) + "\\n").encode())
data = b""
while not data.endswith(b"\\n"):
    chunk = conn.recv(4096)
    if not chunk:
        break
    data += chunk
reply = json.loads(data or b"{}")
if "password" not in reply:
    sys.exit(1)
sys.stdout.write(reply["password"] + "\\n")
"""
"""The `SUDO_ASKPASS` program: one question, one answer, standard library only."""


def real_sudo() -> str | None:
    """The system's `sudo`, which the wrapper runs with `-A`."""
    return shutil.which("sudo")


class Askpass:
    """The helper, the `sudo` wrapper and the socket they answer on, for one
    full-access sandbox. `ask(prompt)` returns the person's password, or None."""

    def __init__(self, ask: Callable[[str], str | None]) -> None:
        self.ask = ask
        self.dir = Path(tempfile.mkdtemp(prefix="saddle-askpass-"))  # mode 0700
        self.socket_path = self.dir / "sock"
        self.helper = self.dir / "askpass"
        self.helper.write_text(f"#!{sys.executable}\n{HELPER}")
        self.helper.chmod(0o700)
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        sudo = real_sudo()
        if sudo is not None:
            wrapper = self.bin / "sudo"
            wrapper.write_text(f'#!/bin/sh\nexec {shlex.quote(sudo)} -A "$@"\n')
            wrapper.chmod(0o700)
        self.server = socket.socket(socket.AF_UNIX)
        self.server.bind(str(self.socket_path))
        self.server.listen()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def env(self, path: str) -> dict[str, str]:
        """What a command needs to reach the wrapper and the helper."""
        return {
            "PATH": f"{self.bin}:{path}",
            "SUDO_ASKPASS": str(self.helper),
            "SADDLE_ASKPASS_SOCKET": str(self.socket_path),
        }

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return  # closed
            threading.Thread(target=self._answer, args=(conn,), daemon=True).start()

    def _answer(self, conn: socket.socket) -> None:
        with conn:
            data = b""
            while not data.endswith(b"\n"):
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
            try:
                prompt = str(json.loads(data).get("prompt", ""))
            except ValueError:
                prompt = ""
            password = self.ask(prompt)
            reply = {"cancel": True} if password is None else {"password": password}
            conn.sendall((json.dumps(reply) + "\n").encode())
            del password, reply

    def close(self) -> None:
        # `close` alone does not wake a thread blocked in `accept` on Linux;
        # `shutdown` does, so the serving thread ends instead of lingering.
        try:
            self.server.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass  # already shut
        self.server.close()
        self.thread.join(timeout=5)
        shutil.rmtree(self.dir, ignore_errors=True)
