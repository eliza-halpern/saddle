"""A local SearXNG for the web reader's `search` tool (#93): `saddle search`.

The reader needs a way from a question to pages. A self-hosted SearXNG gives it
one without an account or a hosted API: it asks the search engines it is set to
use and returns title, address and snippet as JSON. The reader calls its JSON
API directly (`research.Researcher._search`), with no MCP server between: one
HTTP GET is fewer moving parts than a process speaking JSON-RPC to make the same
GET, and the address rule and the "snippets stay with the reader" rule are
enforced where the results arrive.

What this module does, and refuses to:

- `saddle search setup` writes `settings.yml` (JSON output on, the limiter off
  for local use) and a fresh secret key (in `secret.env`, never printed) and starts ONE
  container, `saddle-searxng`, from a pinned image, published on `127.0.0.1`
  only and with no restart policy: nothing here starts on boot, and
  `saddle search stop` removes it.
- It touches only a container that carries saddle's label. A container of the
  same name that is not saddle's, and every other container on the machine, is
  left alone: it is not stopped, restarted, removed or reconfigured.
- Privacy: a query the reader sends goes from your SearXNG to the search engines
  its settings enable (by default several public ones), as any metasearch query
  does; nothing else leaves the machine, and the reader is the only thing that
  calls it.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import IO, Final
from urllib.parse import urlsplit

import httpx

SEARCH_URL_ENV: Final = "SADDLE_SEARCH_URL"
"""The base URL of the SearXNG the reader searches with, when not the default."""
DEFAULT_SEARCH_URL: Final = "http://127.0.0.1:8888"
"""Where `saddle search setup` binds it, on this machine only."""

IMAGE: Final = (
    "searxng/searxng:2026.10.2-19ffbcd30"
    "@sha256:36b0907eb86348d64c77097dee2a67a862ea2bb051555e40da1680c4408b9d50"
)
"""The image, pinned by tag and digest (the tag is the day's rolling release;
the digest is what runs). Bumping it is a reviewed change, not a `latest`."""
CONTAINER: Final = "saddle-searxng"
LABEL_KEY: Final = "io.saddle.managed"
LABEL_VALUE: Final = "searxng"
DEFAULT_PORT: Final = 8888
DIR_ENV: Final = "SADDLE_SEARXNG_DIR"
DEFAULT_DIR: Final = Path("~/.config/saddle/searxng")
READY_TIMEOUT_S: Final = 90.0

type Docker = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def docker_cli(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """The real `docker`; a test hands `setup` and `stop` a fake instead."""
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=False, timeout=600
    )


def search_url_from_env(environ: Mapping[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    return env.get(SEARCH_URL_ENV) or DEFAULT_SEARCH_URL


def config_dir(environ: dict[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    return Path(env.get(DIR_ENV) or DEFAULT_DIR).expanduser()


SETTINGS_TEXT: Final = (
    "# Written by `saddle search setup`. Edit freely; saddle never overwrites it.\n"
    "# The secret key is not here: it is in secret.env beside this file.\n"
    "use_default_settings: true\n"
    "server:\n"
    "  limiter: false\n"
    "  image_proxy: false\n"
    '  bind_address: "0.0.0.0"  # inside the container; docker publishes it on 127.0.0.1 only\n'
    "  port: 8080\n"
    "search:\n"
    "  formats:\n"
    "    - html\n"
    "    - json\n"
)
"""`settings.yml`: the defaults, JSON output on, the limiter off (this is a
private instance bound to loopback)."""


def ensure_settings(directory: Path) -> tuple[Path, Path]:
    """(settings file, secret file), written on first use and left alone after.

    The secret key is generated here, goes only into `secret.env` (readable by its
    owner alone, passed to docker as an env file so it is on no command line and
    never printed), and is never in the settings file the container reads. The
    settings file is mounted read-only as a single file, because mounting the
    directory lets the image's entrypoint hand it to its own user."""
    settings = directory / "settings.yml"
    secret = directory / "secret.env"
    directory.mkdir(parents=True, exist_ok=True)
    if not settings.exists():
        settings.write_text(SETTINGS_TEXT, encoding="utf-8")
        settings.chmod(0o644)  # the container's user is not the person
    if not secret.exists():
        descriptor = os.open(secret, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(f"SEARXNG_SECRET={secrets.token_hex(32)}\n")
    return settings, secret


def port_of(url: str) -> int:
    """The port `url` names, which must be on this machine: `setup` only ever
    runs a local instance."""
    parts = urlsplit(url)
    if parts.hostname not in ("127.0.0.1", "localhost", "::1"):
        msg = f"{url} is not on this machine; `saddle search setup` runs a local instance only"
        raise ValueError(msg)
    return parts.port or DEFAULT_PORT


def run_args(settings: Path, secret: Path, port: int) -> list[str]:
    """The `docker run` arguments: one named, labelled container, published on
    loopback only, never restarted on its own, with the capabilities the image's
    entrypoint needs and no others."""
    return [
        "run",
        "--detach",
        "--name",
        CONTAINER,
        "--label",
        f"{LABEL_KEY}={LABEL_VALUE}",
        "--restart",
        "no",
        "--publish",
        f"127.0.0.1:{port}:8080",
        "--env-file",
        str(secret),
        "--volume",
        f"{settings}:/etc/searxng/settings.yml:ro",
        "--cap-drop",
        "ALL",
        "--cap-add",
        "CHOWN",
        "--cap-add",
        "SETGID",
        "--cap-add",
        "SETUID",
        "--log-driver",
        "json-file",
        "--log-opt",
        "max-size=1m",
        "--log-opt",
        "max-file=1",
        IMAGE,
    ]


def _ours(docker: Docker) -> tuple[bool, bool, bool]:
    """(exists, is saddle's, running) for the container named `CONTAINER`."""
    seen = docker(
        [
            "container",
            "inspect",
            CONTAINER,
            "--format",
            "{{json .Config.Labels}}|{{.State.Running}}",
        ]
    )
    if seen.returncode != 0:
        return False, False, False
    labels, _, running = seen.stdout.strip().rpartition("|")
    try:
        mine = (json.loads(labels) or {}).get(LABEL_KEY) == LABEL_VALUE
    except ValueError:
        mine = False
    return True, mine, running == "true"


def reachable(url: str, http: httpx.Client | None = None) -> str | None:
    """None when a JSON search at `url` answers, else why not."""
    client = http or httpx.Client(timeout=5)
    try:
        reply = client.get(url.rstrip("/") + "/search", params={"q": "saddle", "format": "json"})
    except httpx.HTTPError as exc:
        return f"not answering ({exc})"
    if reply.status_code == 403:
        return "answering, but JSON output is off (HTTP 403); `search.formats` needs `json`"
    try:
        reply.raise_for_status()
        reply.json()["results"]
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        return f"answering badly: {exc!r}"
    return None


def setup(
    docker: Docker,
    directory: Path,
    url: str,
    *,
    http: httpx.Client | None = None,
    wait_s: float = READY_TIMEOUT_S,
) -> tuple[int, str]:
    """Start the container (creating it, and the settings, the first time) and wait
    until it answers. (exit code, what to tell the person)."""
    try:
        port = port_of(url)
    except ValueError as exc:
        return 2, f"error: {exc}"
    if docker(["info"]).returncode != 0:
        return 1, "error: docker is not usable here (`docker info` failed); start docker first"
    exists, mine, running = _ours(docker)
    if exists and not mine:
        return 1, (
            f"error: a container named {CONTAINER} exists and is not saddle's; it was left "
            "alone. Remove or rename it yourself, or free the name."
        )
    settings, secret = ensure_settings(directory)
    if running:
        started = "already running"
    elif exists:
        done = docker(["start", CONTAINER])
        if done.returncode != 0:
            return 1, f"error: could not start {CONTAINER}: {done.stderr.strip()}"
        started = "started"
    else:
        done = docker(run_args(settings, secret, port))
        if done.returncode != 0:
            return 1, f"error: could not create {CONTAINER}: {done.stderr.strip()}"
        started = "created and started"
    deadline = time.monotonic() + wait_s
    problem = reachable(url, http)
    while problem is not None and time.monotonic() < deadline:
        time.sleep(1.0)
        problem = reachable(url, http)
    if problem is not None:
        return 1, f"error: {CONTAINER} {started} but {url} is {problem}"
    return 0, (
        f"{CONTAINER} {started}: {url} (loopback only; no restart policy). Its settings are "
        f"{settings}. Stop it with `saddle search stop`."
    )


def status(docker: Docker, url: str, http: httpx.Client | None = None) -> tuple[int, str]:
    """Whether the container is running and the endpoint answers JSON."""
    if docker(["info"]).returncode != 0:
        return 1, "docker is not usable here (`docker info` failed)"
    exists, mine, running = _ours(docker)
    if exists and not mine:
        return 1, f"a container named {CONTAINER} exists and is not saddle's"
    if not running:
        return 1, f"{CONTAINER} is not running: start it with `saddle search setup`"
    problem = reachable(url, http)
    if problem is not None:
        return 1, f"{CONTAINER} is running but {url} is {problem}"
    return 0, f"{CONTAINER} is running and {url} answers JSON searches"


def stop(docker: Docker) -> tuple[int, str]:
    """Stop and remove saddle's container, and nothing else."""
    exists, mine, _ = _ours(docker)
    if not exists:
        return 0, f"{CONTAINER} does not exist; nothing to stop"
    if not mine:
        return 1, (
            f"error: a container named {CONTAINER} exists and is not saddle's; it was left alone"
        )
    for step in (["stop", CONTAINER], ["rm", CONTAINER]):
        done = docker(step)
        if done.returncode != 0:
            return 1, f"error: `docker {' '.join(step)}` failed: {done.stderr.strip()}"
    return 0, f"{CONTAINER} stopped and removed (its settings are kept)"


def run_search(
    action: str,
    *,
    stdout: IO[str],
    stderr: IO[str],
    docker: Docker = docker_cli,
    http: httpx.Client | None = None,
) -> int:
    """`saddle search setup|status|stop`, against `$SADDLE_SEARCH_URL` (default
    `http://127.0.0.1:8888`)."""
    url = search_url_from_env()
    if action == "setup":
        code, text = setup(docker, config_dir(), url, http=http)
    elif action == "status":
        code, text = status(docker, url, http)
    else:
        code, text = stop(docker)
    print(text, file=stderr if code else stdout)
    return code
