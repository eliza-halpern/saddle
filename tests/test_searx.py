"""`saddle search`: a local SearXNG for the web reader, started safely.

Known-good: setup writes JSON-enabled settings and a secret nobody can read but
the owner, starts one labelled container from a pinned image published on
127.0.0.1 only with no restart policy, and waits until it answers; status and
stop report and end it.

Known-bad: a container of the same name that is not saddle's is never started,
stopped or removed; a port outside this machine is refused; the secret is never
printed or put on a command line; a backend that never answers is a named error.
"""

from __future__ import annotations

import io
import json
import re
import stat
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml  # type: ignore[import-untyped]

from saddle import searx
from saddle.searx import (
    CONTAINER,
    IMAGE,
    SETTINGS_TEXT,
    ensure_settings,
    port_of,
    reachable,
    run_args,
    run_search,
    setup,
    status,
    stop,
)

URL = "http://127.0.0.1:8888"
OURS = json.dumps({"io.saddle.managed": "searxng"})


class FakeDocker:
    """A docker that answers from a table and records every call."""

    def __init__(
        self,
        *,
        info: int = 0,
        container: tuple[str, str] | None = None,
        fail: set[str] | None = None,
    ) -> None:
        self.info = info
        self.container = container  # (labels json, running "true"/"false") or None: absent
        self.fail = fail or set()
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        call = list(args)
        self.calls.append(call)
        verb = call[0]
        if verb == "info":
            return subprocess.CompletedProcess(call, self.info, "", "daemon down")
        if verb == "container":  # inspect
            if self.container is None:
                return subprocess.CompletedProcess(call, 1, "", "No such container")
            labels, running = self.container
            return subprocess.CompletedProcess(call, 0, f"{labels}|{running}\n", "")
        code = 1 if verb in self.fail else 0
        return subprocess.CompletedProcess(call, code, "", f"{verb} failed" if code else "")

    def verbs(self) -> list[str]:
        return [call[0] for call in self.calls]


def answering(status_code: int = 200, body: Any = None) -> httpx.Client:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"results": []} if body is None else body)

    return httpx.Client(transport=httpx.MockTransport(handle))


def refusing() -> httpx.Client:
    def handle(request: httpx.Request) -> httpx.Response:
        msg = "refused"
        raise httpx.ConnectError(msg)

    return httpx.Client(transport=httpx.MockTransport(handle))


# -- settings and secret ------------------------------------------------------------


def test_the_settings_turn_json_on_and_the_limiter_off_and_parse_as_yaml() -> None:
    parsed = yaml.safe_load(SETTINGS_TEXT)
    assert parsed["use_default_settings"] is True
    assert parsed["server"]["limiter"] is False
    assert parsed["search"]["formats"] == ["html", "json"]
    assert "secret_key" not in parsed["server"]  # the secret is not in the file the container reads


def test_the_secret_is_fresh_private_and_not_in_the_settings_file(tmp_path: Path) -> None:
    settings, secret = ensure_settings(tmp_path / "cfg")
    body = secret.read_text()
    assert re.fullmatch(r"SEARXNG_SECRET=[0-9a-f]{64}\n", body)
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600
    assert stat.S_IMODE(settings.stat().st_mode) == 0o644
    assert body.split("=")[1].strip() not in settings.read_text()
    _, other = ensure_settings(tmp_path / "other")
    assert other.read_text() != body  # a different install, a different secret


def test_existing_settings_and_secret_are_never_overwritten(tmp_path: Path) -> None:
    settings, secret = ensure_settings(tmp_path)
    settings.write_text("# my edits\n")
    before = secret.read_text()
    again_settings, again_secret = ensure_settings(tmp_path)
    assert (again_settings, again_secret) == (settings, secret)
    assert settings.read_text() == "# my edits\n"
    assert secret.read_text() == before


def test_the_config_directory_is_in_the_saddle_config_unless_told_otherwise(
    tmp_path: Path,
) -> None:
    assert searx.config_dir({}) == Path("~/.config/saddle/searxng").expanduser()
    assert searx.config_dir({"SADDLE_SEARXNG_DIR": str(tmp_path)}) == tmp_path


# -- the container ---------------------------------------------------------------------


def test_the_run_arguments_publish_on_loopback_only_never_restart_and_pin_the_image(
    tmp_path: Path,
) -> None:
    args = run_args(tmp_path / "settings.yml", tmp_path / "secret.env", 8888)
    joined = " ".join(args)
    assert args[args.index("--publish") + 1] == "127.0.0.1:8888:8080"
    assert args[args.index("--restart") + 1] == "no"
    assert args[args.index("--name") + 1] == CONTAINER
    assert args[args.index("--label") + 1] == "io.saddle.managed=searxng"
    assert args[args.index("--cap-drop") + 1] == "ALL"
    assert args[-1] == IMAGE
    assert re.search(r"@sha256:[0-9a-f]{64}$", IMAGE)  # a digest, not just a tag
    assert ":latest" not in joined
    assert "--privileged" not in args
    assert "0.0.0.0:" not in joined
    assert args.count("--publish") == 1
    assert f"{tmp_path / 'settings.yml'}:/etc/searxng/settings.yml:ro" in args
    assert "SEARXNG_SECRET" not in joined  # the secret is in the env file, not on the line


def test_setup_creates_and_starts_one_container_and_never_prints_the_secret(
    tmp_path: Path,
) -> None:
    docker = FakeDocker()
    code, text = setup(docker, tmp_path, URL, http=answering())
    assert code == 0
    assert "created and started" in text
    assert "127.0.0.1" in text or "loopback" in text
    assert docker.verbs() == ["info", "container", "run"]
    secret = (tmp_path / "secret.env").read_text().split("=")[1].strip()
    assert secret not in text
    assert all(secret not in " ".join(call) for call in docker.calls)


def test_setup_leaves_a_running_container_alone_and_restarts_a_stopped_one(
    tmp_path: Path,
) -> None:
    running = FakeDocker(container=(OURS, "true"))
    code, text = setup(running, tmp_path, URL, http=answering())
    assert (code, "already running" in text) == (0, True)
    assert running.verbs() == ["info", "container"]  # nothing started, nothing changed
    stopped = FakeDocker(container=(OURS, "false"))
    code, text = setup(stopped, tmp_path, URL, http=answering())
    assert (code, "started" in text) == (0, True)
    assert stopped.calls[-1] == ["start", CONTAINER]


@pytest.mark.parametrize(
    "labels", ["{}", "null", "not json", json.dumps({"io.saddle.managed": "x"})]
)
def test_a_container_of_the_same_name_that_is_not_saddles_is_never_touched(
    tmp_path: Path, labels: str
) -> None:
    for running in ("true", "false"):
        docker = FakeDocker(container=(labels, running))
        operations: list[Callable[[FakeDocker], tuple[int, str]]] = [
            lambda d: setup(d, tmp_path, URL, http=answering()),
            lambda d: stop(d),
            lambda d: status(d, URL, answering()),
        ]
        for operation in operations:
            code, text = operation(docker)
            assert code == 1
            assert "is not saddle's" in text
        assert not set(docker.verbs()) & {"run", "start", "stop", "rm", "restart", "kill"}


def test_setup_names_each_way_it_can_fail(tmp_path: Path) -> None:
    code, text = setup(FakeDocker(info=1), tmp_path, URL)
    assert (code, "docker is not usable" in text) == (1, True)
    code, text = setup(FakeDocker(fail={"run"}), tmp_path, URL)
    assert (code, "could not create" in text, "run failed" in text) == (1, True, True)
    code, text = setup(FakeDocker(container=(OURS, "false"), fail={"start"}), tmp_path, URL)
    assert (code, "could not start" in text) == (1, True)


@pytest.mark.parametrize(
    "url", ["https://search.example.com", "http://192.168.1.5:8888", "http://[::2]:1"]
)
def test_setup_runs_a_local_instance_only(tmp_path: Path, url: str) -> None:
    docker = FakeDocker()
    code, text = setup(docker, tmp_path, url)
    assert code == 2
    assert "runs a local instance only" in text
    assert docker.calls == []


def test_the_port_comes_from_the_url_and_defaults_to_the_documented_one() -> None:
    assert port_of("http://127.0.0.1:9100") == 9100
    assert port_of("http://localhost") == 8888
    assert port_of("http://[::1]:7000") == 7000


def test_setup_waits_for_the_backend_and_names_one_that_never_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("saddle.searx.time.sleep", lambda s: None)
    attempts: list[int] = []

    def flaky(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < 3:
            msg = "not yet"
            raise httpx.ConnectError(msg)
        return httpx.Response(200, json={"results": []})

    code, text = setup(
        FakeDocker(), tmp_path, URL, http=httpx.Client(transport=httpx.MockTransport(flaky))
    )
    assert code == 0
    assert len(attempts) == 3
    code, text = setup(FakeDocker(), tmp_path, URL, http=refusing(), wait_s=0.0)
    assert code == 1
    assert "created and started but http://127.0.0.1:8888 is not answering" in text


# -- reachable, status, stop --------------------------------------------------------------


def test_reachable_says_none_only_for_a_json_answer_with_results() -> None:
    assert reachable(URL, answering()) is None
    assert "not answering" in str(reachable(URL, refusing()))
    assert "JSON output is off" in str(reachable(URL, answering(403)))
    assert "answering badly" in str(reachable(URL, answering(500)))
    assert "answering badly" in str(reachable(URL, answering(200, {"no": "results"})))
    assert "answering badly" in str(reachable(URL, answering(200, [1])))
    html = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>")))
    assert "answering badly" in str(reachable(URL, html))


def test_reachable_builds_its_own_client_when_none_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert "not answering" in str(reachable("http://127.0.0.1:1"))


def test_status_reports_each_state_with_what_to_do() -> None:
    assert status(FakeDocker(info=1), URL)[1].startswith("docker is not usable")
    code, text = status(FakeDocker(), URL)
    assert (code, "start it with `saddle search setup`" in text) == (1, True)
    code, text = status(FakeDocker(container=(OURS, "false")), URL)
    assert (code, "is not running" in text) == (1, True)
    code, text = status(FakeDocker(container=(OURS, "true")), URL, refusing())
    assert (code, "is running but" in text) == (1, True)
    code, text = status(FakeDocker(container=(OURS, "true")), URL, answering())
    assert (code, "answers JSON searches" in text) == (0, True)


def test_stop_ends_only_saddles_container() -> None:
    gone = FakeDocker()
    assert stop(gone)[0] == 0
    assert gone.verbs() == ["container"]
    docker = FakeDocker(container=(OURS, "true"))
    code, text = stop(docker)
    assert (code, "stopped and removed" in text) == (0, True)
    assert docker.calls[-2:] == [["stop", CONTAINER], ["rm", CONTAINER]]
    code, text = stop(FakeDocker(container=(OURS, "true"), fail={"stop"}))
    assert (code, "`docker stop saddle-searxng` failed" in text) == (1, True)


# -- the command ------------------------------------------------------------------------------


def test_the_command_prints_results_to_stdout_and_failures_to_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SADDLE_SEARXNG_DIR", str(tmp_path))
    out, err = io.StringIO(), io.StringIO()
    assert run_search("setup", stdout=out, stderr=err, docker=FakeDocker(), http=answering()) == 0
    assert "created and started" in out.getvalue()
    assert err.getvalue() == ""
    out, err = io.StringIO(), io.StringIO()
    assert run_search("status", stdout=out, stderr=err, docker=FakeDocker(), http=answering()) == 1
    assert "is not running" in err.getvalue()
    out, err = io.StringIO(), io.StringIO()
    assert run_search("stop", stdout=out, stderr=err, docker=FakeDocker()) == 0
    assert "nothing to stop" in out.getvalue()


def test_the_cli_routes_search_and_needs_no_model_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from saddle.cli import main

    seen: list[str] = []

    def fake(action: str, **kwargs: Any) -> int:
        seen.append(action)
        return 3

    monkeypatch.setattr("saddle.cli.run_search", fake)
    assert main(["search", "status"]) == 3  # no API key was asked for: the key check is later
    assert seen == ["status"]


def test_the_real_docker_runner_runs_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        assert kwargs["check"] is False
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    monkeypatch.setattr("saddle.searx.subprocess.run", fake_run)
    assert searx.docker_cli(["info"]).stdout == "ok"
    assert seen == [["docker", "info"]]
