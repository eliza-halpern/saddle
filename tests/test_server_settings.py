"""Where the server URL and model come from (#119).

Precedence, one path for every subcommand: an explicit flag, then the
environment variable, then a line in the env file, then the built-in
default. The file is read for the named settings only; nothing in it is
put into the environment.
"""

from __future__ import annotations

import io
import os
from pathlib import Path
from typing import Any, ClassVar

import pytest

from saddle import cli
from saddle.cli import main
from saddle.vllm import DEFAULT_BASE_URL, DEFAULT_MODEL

OTHER_URL = "http://127.0.0.1:18999/v1"
FLAG_URL = "http://127.0.0.1:18777/v1"
FILE_URL = "http://127.0.0.1:18555/v1"


class _Client:
    """Records how it was built; serves whichever model it was built for."""

    made: ClassVar[list[dict[str, Any]]] = []

    def __init__(self, **kwargs: Any) -> None:
        _Client.made.append(kwargs)
        self._model = str(kwargs["model"])

    def __enter__(self) -> _Client:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def list_models(self) -> list[str]:
        return [self._model]

    def max_model_len(self) -> int | None:
        return None


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> type[_Client]:
    _Client.made.clear()
    monkeypatch.setattr(cli, "VllmClient", _Client)
    monkeypatch.setenv("SADDLE_VLLM_API_KEY", "k-secret-value")
    return _Client


def _env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str) -> Path:
    path = tmp_path / "env"
    path.write_text(text)
    monkeypatch.setattr(cli, "KEY_FILE", str(path))
    return path


def _doctor(argv: list[str]) -> str:
    out = io.StringIO()
    assert main(["doctor", *argv], stdout=out) == 0
    return out.getvalue()


# Contract 1: with no flag, a set SADDLE_BASE_URL is used.


def test_doctor_probes_the_url_in_the_environment_when_no_flag_is_given(
    client: type[_Client], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SADDLE_BASE_URL", OTHER_URL)
    monkeypatch.setenv("SADDLE_MODEL", "other-model")
    _doctor([])
    assert client.made[0]["base_url"] == OTHER_URL
    assert client.made[0]["model"] == "other-model"


def test_with_nothing_set_the_built_in_defaults_are_used(client: type[_Client]) -> None:
    _doctor([])
    assert client.made[0]["base_url"] == DEFAULT_BASE_URL
    assert client.made[0]["model"] == DEFAULT_MODEL


def test_an_empty_variable_counts_as_unset(
    client: type[_Client], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SADDLE_BASE_URL", "")
    _doctor([])
    assert client.made[0]["base_url"] == DEFAULT_BASE_URL


@pytest.mark.parametrize(
    "argv",
    [
        ["dag", "Do it."],
        ["up", "--workdir", "."],
        ["run", "--yes", "Do it."],
        ["auto", "Do it."],
    ],
)
def test_every_server_command_reads_the_environment(
    client: type[_Client], monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    """One resolution for all of them, not one per subcommand: each command
    here is stopped at its preflight (the model is not served), which is
    after the client was built from the resolved values."""
    monkeypatch.setenv("SADDLE_BASE_URL", OTHER_URL)
    monkeypatch.setenv("SADDLE_MODEL", "other-model")
    monkeypatch.setattr(_Client, "list_models", lambda self: ["nothing-useful"])
    err = io.StringIO()
    main(argv, stdout=io.StringIO(), stderr=err)
    assert client.made, err.getvalue()
    assert client.made[0]["base_url"] == OTHER_URL
    assert client.made[0]["model"] == "other-model"


def test_the_chat_server_is_given_the_resolved_values(
    client: type[_Client], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from saddle.web import app as web_app

    served: dict[str, object] = {}
    monkeypatch.setattr(web_app, "serve", lambda **kw: served.update(kw))
    monkeypatch.setenv("SADDLE_BASE_URL", OTHER_URL)
    _env_file(tmp_path, monkeypatch, "SADDLE_MODEL=file-model\n")
    argv = ["chat", "--no-open", "--workdir", str(tmp_path / "w")]
    assert main(argv, stdout=io.StringIO()) == 0
    assert served["base_url"] == OTHER_URL
    assert served["model"] == "file-model"


# Contract 2: a flag beats the environment variable.


def test_a_flag_beats_the_environment_variable(
    client: type[_Client], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SADDLE_BASE_URL", OTHER_URL)
    monkeypatch.setenv("SADDLE_MODEL", "other-model")
    _doctor(["--base-url", FLAG_URL, "--model", "flag-model"])
    assert client.made[0]["base_url"] == FLAG_URL
    assert client.made[0]["model"] == "flag-model"


def test_a_flag_equal_to_the_default_still_beats_the_environment(
    client: type[_Client], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Asking for the default by name is a choice, not an absence of one."""
    monkeypatch.setenv("SADDLE_BASE_URL", OTHER_URL)
    _doctor(["--base-url", DEFAULT_BASE_URL])
    assert client.made[0]["base_url"] == DEFAULT_BASE_URL


# Contract 3: the environment variable beats the file.


def test_the_environment_variable_beats_the_file(
    client: type[_Client], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _env_file(tmp_path, monkeypatch, f"SADDLE_BASE_URL={FILE_URL}\nSADDLE_MODEL=file-model\n")
    monkeypatch.setenv("SADDLE_BASE_URL", OTHER_URL)
    monkeypatch.setenv("SADDLE_MODEL", "other-model")
    _doctor([])
    assert client.made[0]["base_url"] == OTHER_URL
    assert client.made[0]["model"] == "other-model"


def test_the_file_is_used_when_the_environment_is_silent(
    client: type[_Client], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _env_file(
        tmp_path,
        monkeypatch,
        f"export SADDLE_BASE_URL=\"{FILE_URL}\"\nSADDLE_MODEL='file-model'\n",
    )
    _doctor([])
    assert client.made[0]["base_url"] == FILE_URL
    assert client.made[0]["model"] == "file-model"


def test_the_key_line_is_not_read_as_a_url(
    client: type[_Client], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Only the named setting is taken from the file, not whatever comes first."""
    _env_file(tmp_path, monkeypatch, "SADDLE_VLLM_API_KEY=file-key\nSADDLE_BASE_URL_OLD=x\n")
    _doctor([])
    assert client.made[0]["base_url"] == DEFAULT_BASE_URL
    assert client.made[0]["model"] == DEFAULT_MODEL


# Contract 4: the file's other lines are never exported.


def test_reading_the_file_exports_nothing(
    client: type[_Client], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("SADDLE_VLLM_API_KEY")
    monkeypatch.delenv("VLLM_API_KEY", raising=False)
    monkeypatch.delenv("UNRELATED", raising=False)
    _env_file(
        tmp_path,
        monkeypatch,
        "UNRELATED=leave-me-alone\n"
        "SADDLE_VLLM_API_KEY=file-key\n"
        f"SADDLE_BASE_URL={FILE_URL}\n"
        "SADDLE_MODEL=file-model\n",
    )
    before = dict(os.environ)
    _doctor([])
    assert client.made[0]["base_url"] == FILE_URL
    assert client.made[0]["api_key"] == "file-key"
    assert dict(os.environ) == before


# `saddle doctor` says where each value came from, and never shows the key.


def test_doctor_names_the_source_of_each_value(
    client: type[_Client], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env_file = _env_file(tmp_path, monkeypatch, "SADDLE_MODEL=file-model\n")
    monkeypatch.setenv("SADDLE_BASE_URL", OTHER_URL)
    assert _doctor([]) == (
        f"base URL: {OTHER_URL} (from environment SADDLE_BASE_URL)\n"
        f"model: file-model (from file {env_file})\n"
        f"OK: {OTHER_URL} serves file-model (models: file-model)\n"
    )
    client.made.clear()
    out = _doctor(["--base-url", FLAG_URL])
    assert f"base URL: {FLAG_URL} (from flag --base-url)\n" in out
    monkeypatch.setattr(cli, "KEY_FILE", str(tmp_path / "absent"))
    out = _doctor(["--model", "flag-model"])
    assert "model: flag-model (from flag --model)\n" in out
    assert f"base URL: {OTHER_URL} (from environment SADDLE_BASE_URL)\n" in out
    monkeypatch.delenv("SADDLE_BASE_URL")
    out = _doctor([])
    assert f"base URL: {DEFAULT_BASE_URL} (built-in default)\n" in out
    assert f"model: {DEFAULT_MODEL} (built-in default)\n" in out


def test_doctor_names_the_source_even_when_the_preflight_fails(
    client: type[_Client], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing preflight is when the source matters most."""
    monkeypatch.setenv("SADDLE_BASE_URL", OTHER_URL)
    monkeypatch.setattr(_Client, "list_models", lambda self: [])
    out = io.StringIO()
    assert main(["doctor"], stdout=out) == 1
    assert out.getvalue().startswith(f"base URL: {OTHER_URL} (from environment SADDLE_BASE_URL)\n")


def test_doctor_never_prints_the_key(
    client: type[_Client], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    out = _doctor([])
    assert "k-secret-value" not in out
    monkeypatch.delenv("SADDLE_VLLM_API_KEY")
    monkeypatch.delenv("VLLM_API_KEY", raising=False)
    _env_file(tmp_path, monkeypatch, "SADDLE_VLLM_API_KEY=file-secret-value\n")
    out = _doctor([])
    assert client.made[-1]["api_key"] == "file-secret-value"
    assert "file-secret-value" not in out
