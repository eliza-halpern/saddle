"""Opt-in capabilities: what the person turned on, and whether it works (#139, #93).

Web research and MCP servers are not part of installing or running saddle. Each
of four capabilities is a switch that is **off** until the person turns it on,
and the model is offered a tool only when its switch is on **and** the thing it
needs is actually there:

- `mcp`: the Edit lane may call the allowlisted tools of `access: acting` MCP
  servers (`mcpclient`). Needs the SDK (`saddle-harness[mcp]`) and a server.
- `research`: the `research` tool, a quarantined reader (`research`) that reads
  the web for the acting model. Needs the SDK, an `access: reader` server in the
  allowlist, and isolation.
- `search`: the reader's `search` tool, from a local SearXNG (`searx`). Needs it
  running. Without it `research` still works from addresses the person gives it,
  and says search is unavailable.
- `browser`: the reader's `browser_*` tools (a Playwright server) for pages that
  need clicks. Needs a reader server that offers them.

The switches live in `capabilities.json` beside the MCP allowlist
(`$SADDLE_CAPABILITIES_FILE` overrides the path); `$SADDLE_CAPABILITIES` overrides
the file for one process (`research,search` or `research=on,browser=off`). Nothing
is on by default, and `status` says, for each, `on`, `off`, or `unavailable` with
the reason.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import IO, Final, Literal

import httpx

from saddle.answers import BraveAnswers
from saddle.brave import BraveKeyError, BraveSearch, key_file
from saddle.mcpclient import McpConfigError, ServerSpec, load_config, sdk_problem
from saddle.research import reader_problem
from saddle.searx import reachable, search_url_from_env

NAMES: Final = ("mcp", "research", "search", "browser", "answers")
FILE_ENV: Final = "SADDLE_CAPABILITIES_FILE"
OVERRIDE_ENV: Final = "SADDLE_CAPABILITIES"
DEFAULT_FILE: Final = Path("~/.config/saddle/capabilities.json")

_ON: Final = frozenset({"", "1", "on", "true", "yes"})
_OFF: Final = frozenset({"0", "off", "false", "no"})


class CapabilityError(ValueError):
    """The switches could not be read. Names the file or the setting and why."""


@dataclass(frozen=True)
class Switches:
    """What the person turned on. Every switch defaults to off."""

    mcp: bool = False
    research: bool = False
    search: bool = False
    browser: bool = False
    answers: bool = False

    def get(self, name: str) -> bool:
        return bool(getattr(self, name))


@dataclass(frozen=True)
class Status:
    name: str
    state: Literal["on", "off", "unavailable"]
    reason: str = ""

    def as_json(self) -> dict[str, str]:
        return {"name": self.name, "state": self.state, "reason": self.reason}


def file_path(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    return Path(env.get(FILE_ENV) or DEFAULT_FILE).expanduser()


def _from_file(path: Path) -> dict[str, bool]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        msg = f"cannot read the capabilities file {path}: {exc}"
        raise CapabilityError(msg) from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        msg = f"the capabilities file {path} is not valid JSON: {exc}"
        raise CapabilityError(msg) from exc
    if not isinstance(data, dict):
        msg = f'the capabilities file {path} must be an object like {{"research": true}}'
        raise CapabilityError(msg)
    for name, value in data.items():
        if name not in NAMES or not isinstance(value, bool):
            msg = (
                f"the capabilities file {path}: {name!r} must be one of "
                f"{', '.join(NAMES)} set to true or false"
            )
            raise CapabilityError(msg)
    return dict(data)


def _from_env(text: str) -> dict[str, bool]:
    found: dict[str, bool] = {}
    for token in (t.strip() for t in text.split(",") if t.strip()):
        name, _, value = token.partition("=")
        name, value = name.strip().lower(), value.strip().lower()
        if name not in NAMES or value not in _ON | _OFF:
            msg = (
                f"${OVERRIDE_ENV}: {token!r} is not a capability name ({', '.join(NAMES)}) "
                "optionally followed by =on or =off"
            )
            raise CapabilityError(msg)
        found[name] = value in _ON
    return found


def load(environ: Mapping[str, str] | None = None) -> Switches:
    """The switches: the file, then `$SADDLE_CAPABILITIES` over it. All off when
    there is neither; a file or setting that cannot be read is an error."""
    env = os.environ if environ is None else environ
    chosen = {**_from_file(file_path(env)), **_from_env(env.get(OVERRIDE_ENV, ""))}
    return replace(Switches(), **chosen)


def save(name: str, on: bool, environ: Mapping[str, str] | None = None) -> Path:
    """Turn `name` on or off in the capabilities file, keeping the others."""
    if name not in NAMES:
        msg = f"{name!r} is not a capability ({', '.join(NAMES)})"
        raise CapabilityError(msg)
    path = file_path(environ)
    data = _from_file(path)
    data[name] = on
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _servers(access: str) -> tuple[dict[str, ServerSpec], str | None]:
    try:
        config = load_config()
    except McpConfigError as exc:
        return {}, f"the MCP allowlist is broken: {exc}"
    return {n: s for n, s in config.items() if s.access == access}, None


def status(switches: Switches | None = None, http: httpx.Client | None = None) -> list[Status]:
    """Each capability: on, off, or unavailable with the reason. Reads the
    allowlist and asks the search backend; starts no server. A search or browser
    switch that is on while research is off is `on` with a note: only research
    uses them."""
    on = switches if switches is not None else load()
    found: list[Status] = []
    for name in NAMES:
        if not on.get(name):
            found.append(Status(name, "off"))
            continue
        problem = _problem(name, on, http)
        if problem is not None:
            found.append(Status(name, "unavailable", problem))
        elif name in ("search", "browser", "answers") and not on.research:
            note = "used by research, which is off"
            found.append(Status(name, "on", "; ".join(filter(None, [note, _provider(name)]))))
        else:
            found.append(Status(name, "on", _provider(name)))
    return found


def _brave() -> tuple[BraveSearch | None, str]:
    try:
        return BraveSearch.from_env(), ""
    except BraveKeyError as exc:
        return None, str(exc)


def _provider(name: str) -> str:
    """What `search` searches with when it is Brave (and its budget) or a Brave key was
    refused; empty for plain SearXNG, as before."""
    if name == "answers":
        return "Brave Answers (`saddle answers status` shows the spend)"
    if name != "search":
        return ""
    brave, problem = _brave()
    if brave is not None:
        return f"Brave: {brave.budget_line()}"
    return f"SearXNG; Brave key refused: {problem}" if problem else ""


def _problem(name: str, on: Switches, http: httpx.Client | None) -> str | None:
    """Why `name`, which is switched on, cannot work now; None when it can."""
    if name == "answers":
        try:
            if BraveAnswers.from_env() is not None:
                return None
        except BraveKeyError as exc:
            return str(exc)
        return f"no Answers key: add BRAVE_ANSWERS_API_KEY=... to {key_file()}"
    if name == "search":
        if _brave()[0] is not None:
            return None  # Brave answers; SearXNG is only its labelled fallback
        found = reachable(search_url_from_env(), http)
        return None if found is None else f"{found}; start it with `saddle search setup`"
    problem = sdk_problem()
    if problem is not None:
        return problem
    if name == "mcp":
        servers, broken = _servers("acting")
        if broken is not None:
            return broken
        return None if servers else "no `access: acting` server is in the MCP allowlist"
    servers, broken = _servers("reader")
    if broken is not None:
        return broken
    return reader_problem(servers, need_browser=name == "browser", browser_on=on.browser)


def run(
    action: str,
    name: str | None,
    *,
    stdout: IO[str],
    stderr: IO[str],
    http: httpx.Client | None = None,
) -> int:
    """`saddle capabilities [status|enable NAME|disable NAME]`: print each
    capability's state; enable and disable first write the switch."""
    try:
        if action in ("enable", "disable"):
            if name is None:
                print(f"error: {action} needs a capability ({', '.join(NAMES)})", file=stderr)
                return 2
            path = save(name, action == "enable")
            print(f"{name} is now {'on' if action == 'enable' else 'off'} ({path})", file=stdout)
        rows = status(http=http)
    except CapabilityError as exc:
        print(f"error: {exc}", file=stderr)
        return 1
    for row in rows:
        print(f"{row.name:<9} {row.state:<12} {row.reason}".rstrip(), file=stdout)
    return 0
