"""`tools/structured_output_probe.py`: what it sends, what it records, what it refuses to run.

The probe exists to settle questions about a live model server, so no test
here says what the server does. They hold the probe's own claims instead:
each arm reaches the wire as built, every answer is written down whole
(rejections included), the response shape it summarises is read correctly,
and it refuses to run when its own control has stopped discriminating.

The server is `httpx.MockTransport` behind the probe's real `httpx.Client`,
so URL joining, headers and JSON encoding are httpx's own.
"""

from __future__ import annotations

import json
import runpy
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from saddle.vllm import DEFAULT_BASE_URL, DEFAULT_MODEL, DIFF_GRAMMAR
from tools import structured_output_probe as probe

KEY = "test-key-not-a-real-credential"  # a made-up value; the probe must never echo it
DIFF = "diff --git a/n.py b/n.py\n--- /dev/null\n+++ b/n.py\n"

Handler = Callable[[httpx.Request], httpx.Response]


def _envelope(content: str, reasoning: str | None = None, finish: str = "stop") -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if reasoning is not None:
        message["reasoning"] = reasoning
    return {"choices": [{"finish_reason": finish, "message": message}]}


class _Server:
    """A scripted server: records every request, answers by `handler`."""

    def __init__(self, handler: Handler | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.client_kwargs: dict[str, Any] = {}
        self.handler = handler or self.ok

    def ok(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"info": {"version": "9.9"}, "components": {}})
        return httpx.Response(200, json=_envelope("a sentence", "thinking"))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)

    def posted(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests if r.method == "POST"]


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Server:
    """The probe's `httpx.Client` with a scripted server behind it, in a scratch cwd."""
    scripted = _Server()
    real = httpx.Client

    def client(**kwargs: Any) -> httpx.Client:
        scripted.client_kwargs = kwargs
        return real(transport=httpx.MockTransport(scripted), **kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    monkeypatch.setenv("SADDLE_VLLM_API_KEY", KEY)
    monkeypatch.delenv("VLLM_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    return scripted


def _read(outdir: Path, arm: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((outdir / f"{arm}.json").read_text())
    return loaded


# ---------------------------------------------------------------- payloads


def test_an_arm_is_the_shared_payload_plus_its_own_keys() -> None:
    payload = probe._base({"structured_outputs": {"grammar": "g"}, "temperature": 0.5}, "hello")

    assert payload["model"] == DEFAULT_MODEL
    assert payload["messages"] == [{"role": "user", "content": "hello"}]
    assert payload["structured_outputs"] == {"grammar": "g"}
    # An arm's own key wins over the shared default, so an arm can vary it.
    assert payload["temperature"] == 0.5
    assert probe._base({})["temperature"] == 0.0
    assert probe._base({})["messages"][0]["content"] == probe.PROMPT


def test_the_shared_payload_asks_for_a_short_low_effort_answer_with_reasoning_returned() -> None:
    """The questions are about reasoning on, so reasoning is requested and visible.

    A greedy, bounded, low-effort call keeps the arms comparable: the only
    thing that varies between them is the constraint under test.
    """
    assert probe._base({}) == {
        "model": DEFAULT_MODEL,
        "messages": [{"role": "user", "content": probe.PROMPT}],
        "temperature": 0.0,
        "max_tokens": 1024,
        "reasoning_effort": "low",
        "include_reasoning": True,
    }


def test_every_arm_is_sent_to_the_server_as_built(server: _Server) -> None:
    assert probe.main(["probe"]) == 0

    assert server.posted() == list(probe.ARMS.values())
    assert {r.url.path for r in server.requests if r.method == "POST"} == {"/v1/chat/completions"}


def test_the_key_goes_in_the_authorization_header_and_nowhere_else(
    server: _Server, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    assert probe.main(["probe"]) == 0

    assert {r.headers["authorization"] for r in server.requests} == {f"Bearer {KEY}"}
    shown = capsys.readouterr()
    written = "".join(p.read_text() for p in tmp_path.iterdir())
    assert KEY not in shown.out + shown.err + written


def test_the_client_waits_as_long_as_a_reasoning_call_can_take(server: _Server) -> None:
    assert probe.main(["probe"]) == 0

    assert server.client_kwargs["timeout"] == 600.0
    assert server.client_kwargs["base_url"] == DEFAULT_BASE_URL


def test_the_fallback_key_name_is_used_when_the_primary_is_unset(
    server: _Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SADDLE_VLLM_API_KEY")
    monkeypatch.setenv("VLLM_API_KEY", "other-test-value")

    assert probe.main(["probe"]) == 0

    assert {r.headers["authorization"] for r in server.requests} == {"Bearer other-test-value"}


def test_the_primary_key_name_wins_over_the_fallback(
    server: _Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VLLM_API_KEY", "other-test-value")

    assert probe.main(["probe"]) == 0

    assert {r.headers["authorization"] for r in server.requests} == {f"Bearer {KEY}"}


def test_with_no_key_it_refuses_before_sending_anything(
    server: _Server, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("SADDLE_VLLM_API_KEY")

    assert probe.main(["probe"]) == 2

    assert server.requests == []
    assert capsys.readouterr().err == "error: SADDLE_VLLM_API_KEY is not set\n"


# ---------------------------------------------------------------- the control


def test_it_refuses_to_run_when_the_unimplemented_key_leaks_into_another_arm(
    server: _Server, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Known-bad: the control name appears in a second arm, so a 200 proves nothing."""
    leaky = {
        **probe.ARMS,
        "grammar": probe._base({"structured_outputs": {probe.UNIMPLEMENTED_KEY: True}}),
    }
    monkeypatch.setattr(probe, "ARMS", leaky)

    assert probe.main(["probe"]) == 3

    assert server.requests == []
    assert "leaked=['grammar']" in capsys.readouterr().err


def test_it_refuses_to_run_when_the_unknown_key_arm_lost_its_key(
    server: _Server, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Known-bad: renamed to something real, the control arm no longer discriminates."""
    renamed = {**probe.ARMS, "unknown_key": probe._base({"structured_outputs": {"grammar": "g"}})}
    monkeypatch.setattr(probe, "ARMS", renamed)

    assert probe.main(["probe"]) == 3

    assert server.requests == []
    assert "no longer discriminates" in capsys.readouterr().err


def test_the_shipped_arms_keep_the_unimplemented_key_to_one_arm() -> None:
    """Known-good half: the real table passes the same guard the two tests above trip."""
    holders = [
        arm for arm, payload in probe.ARMS.items() if probe.UNIMPLEMENTED_KEY in str(payload)
    ]

    assert holders == ["unknown_key"]


# ---------------------------------------------------------------- the record


def test_each_arm_is_recorded_whole_with_its_distinguishing_keys(
    server: _Server, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert probe.main(["probe", str(tmp_path / "out" / "nested")]) == 0

    outdir = tmp_path / "out" / "nested"
    assert {p.name for p in outdir.iterdir()} == {
        *(f"{arm}.json" for arm in probe.ARMS),
        "openapi_structural_tag.json",
    }
    record = _read(outdir, "enable_in_reasoning_top")
    assert record["arm"] == "enable_in_reasoning_top"
    assert record["status"] == 200
    assert record["request_extra"] == {
        "structured_outputs": {"grammar": DIFF_GRAMMAR},
        "enable_in_reasoning": True,
    }
    assert record["body"] == _envelope("a sentence", "thinking")
    assert _read(outdir, "control")["request_extra"] == {}
    out = capsys.readouterr().out
    assert f"grammar: HTTP 200 -> {outdir / 'grammar.json'}" in out


def test_records_are_written_indented_so_a_reader_can_diff_two_runs(
    server: _Server, tmp_path: Path
) -> None:
    assert probe.main(["probe", str(tmp_path / "o")]) == 0

    for name in ("grammar.json", "openapi_structural_tag.json"):
        text = (tmp_path / "o" / name).read_text()
        assert text == json.dumps(json.loads(text), indent=2) + "\n"


def test_an_accepted_arm_gets_a_shape_and_a_summary_line(
    server: _Server, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    server.handler = lambda request: (
        server.ok(request)
        if request.method == "GET"
        else httpx.Response(200, json=_envelope(DIFF + "@@", "I think", finish="length"))
    )

    assert probe.main(["probe", str(tmp_path / "o")]) == 0

    shape = _read(tmp_path / "o", "grammar")["shape"]
    assert shape["finish_reason"] == "length"
    assert shape["content_is_diff_head"] is True
    assert shape["reasoning_is_diff_head"] is False
    assert (
        "  finish=length reasoning_len=7 reasoning_is_diff_head=False"
        f" content_len={len(DIFF) + 2} content_is_diff_head=True"
    ) in capsys.readouterr().out


def test_a_rejection_is_recorded_with_the_servers_own_words(
    server: _Server, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The refusal is the finding for the structural-tag arms, so it is kept verbatim."""
    message = "Invalid structured_outputs structural_tag specification " + "x" * 600

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return server.ok(request)
        if "structural_tag" in request.content.decode():
            return httpx.Response(400, json={"error": {"message": message}})
        return server.ok(request)

    server.handler = handler

    assert probe.main(["probe", str(tmp_path / "o")]) == 0

    refused = _read(tmp_path / "o", "structural_tag_so")
    assert refused["status"] == 400
    assert refused["body"] == {"error": {"message": message}}
    assert "shape" not in refused
    assert "shape" in _read(tmp_path / "o", "grammar")
    out = capsys.readouterr().out
    rejected = [line for line in out.splitlines() if line.startswith("  rejected: ")]
    assert rejected
    # Shown truncated, so one long message cannot bury the table; the file keeps it all.
    assert all(len(line) == len("  rejected: ") + 400 for line in rejected)


def test_a_body_that_is_not_json_is_kept_instead_of_lost(server: _Server, tmp_path: Path) -> None:
    server.handler = lambda request: (
        server.ok(request)
        if request.method == "GET"
        else httpx.Response(502, text="<html>bad gateway</html>" + "y" * 3000)
    )

    assert probe.main(["probe", str(tmp_path / "o")]) == 0

    record = _read(tmp_path / "o", "grammar")
    assert record["status"] == 502
    assert record["body"] == {"non_json_body": ("<html>bad gateway</html>" + "y" * 3000)[:2000]}


def test_the_output_directory_defaults_to_the_current_one(server: _Server, tmp_path: Path) -> None:
    assert probe.main(["probe", str(tmp_path / "elsewhere")]) == 0
    assert not (tmp_path / "control.json").exists()
    assert (tmp_path / "elsewhere" / "control.json").exists()

    assert probe.main(["probe"]) == 0

    assert (tmp_path / "control.json").exists()


# ---------------------------------------------------------------- _shape and _tag_span


def test_the_shape_reads_the_envelope_a_server_returns() -> None:
    body = _envelope(DIFF + "rest", "I reason " + "z" * 500, finish="stop")

    shape = probe._shape(body)

    assert shape["envelope_keys"] == ["choices"]
    assert shape["choice_keys"] == ["finish_reason", "message"]
    assert shape["message_keys"] == ["content", "reasoning", "role"]
    assert shape["finish_reason"] == "stop"
    assert shape["reasoning_len"] == len("I reason ") + 500
    assert shape["content_len"] == len(DIFF + "rest")
    assert shape["content_is_diff_head"] is True
    assert shape["reasoning_is_diff_head"] is False
    assert shape["reasoning_head"] == ("I reason " + "z" * 500)[:400]
    assert shape["content_head"] == (DIFF + "rest")[:400]
    assert shape["tag_span"] is None


def test_the_heads_are_cut_at_four_hundred_characters() -> None:
    shape = probe._shape(_envelope("c" * 500, "r" * 500))

    assert (len(shape["content_head"]), len(shape["reasoning_head"])) == (400, 400)
    assert (shape["content_len"], shape["reasoning_len"]) == (500, 500)


def test_the_shape_tells_a_diff_in_the_reasoning_from_one_in_the_content() -> None:
    shape = probe._shape(_envelope("plain", DIFF + "x"))

    assert shape["reasoning_is_diff_head"] is True
    assert shape["content_is_diff_head"] is False


@pytest.mark.parametrize(
    "body",
    [
        {"choices": []},
        {},
        {"choices": [{"message": None}]},
        {"choices": [{"message": {"content": None, "reasoning": None}}]},
    ],
)
def test_the_shape_of_an_empty_answer_is_zero_lengths_not_an_error(body: dict[str, Any]) -> None:
    shape = probe._shape(body)

    assert shape["reasoning_len"] == 0
    assert shape["content_len"] == 0
    assert shape["content_is_diff_head"] is False
    assert shape["tag_span"] is None


def test_the_shape_of_a_body_that_is_not_an_object_has_no_envelope_keys() -> None:
    shape = probe._shape(["not", "an", "envelope"])

    assert shape["envelope_keys"] is None
    assert shape["finish_reason"] is None


def test_a_fired_trigger_reports_what_came_before_and_what_it_enclosed() -> None:
    span = probe._tag_span('The answer is 6. <diff>{"diff": "x", "n": 1}</diff> done')

    assert span == {
        "before_tag": "The answer is 6. ",
        "inside_tag": '{"diff": "x", "n": 1}',
        "inside_is_json_object": True,
        "inside_json_keys": ["diff", "n"],
    }


def test_the_first_trigger_is_the_one_reported() -> None:
    span = probe._tag_span('one <diff>{"a": 1}</diff> two <diff>{"b": 2}</diff>')

    assert span is not None
    assert span["before_tag"] == "one "
    assert span["inside_json_keys"] == ["a"]


def test_no_trigger_means_no_span() -> None:
    assert probe._tag_span("just a sentence, no tag") is None
    assert probe._tag_span("an end tag alone </diff>") is None


@pytest.mark.parametrize(
    ("inside", "is_object"),
    [("not json at all", False), ("[1, 2]", False), ('"a string"', False), ("{}", True)],
)
def test_only_a_json_object_inside_the_tag_counts_as_one(inside: str, is_object: bool) -> None:
    span = probe._tag_span(f"<diff>{inside}</diff>")

    assert span is not None
    assert span["inside_is_json_object"] is is_object
    assert span["inside_json_keys"] == ([] if is_object else None)


def test_an_unclosed_tag_takes_the_rest_of_the_content_as_its_inside() -> None:
    span = probe._tag_span('before <diff>{"diff": "x"}')

    assert span is not None
    assert span["inside_tag"] == '{"diff": "x"}'
    assert span["inside_is_json_object"] is True


# ---------------------------------------------------------------- the server's own models


def test_the_build_models_named_by_the_probe_are_saved_with_the_version(
    server: _Server, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    schemas = {
        "StructuralTagResponseFormat": {"type": "object"},
        "LegacyStructuralTag": {"type": "string"},
        "SomethingElse": {"type": "null"},
    }
    server.handler = lambda request: (
        httpx.Response(200, json={"info": {"version": "0.28"}, "components": {"schemas": schemas}})
        if request.method == "GET"
        else server.ok(request)
    )

    assert probe.main(["probe", str(tmp_path / "o")]) == 0

    saved = json.loads((tmp_path / "o" / "openapi_structural_tag.json").read_text())
    assert saved == {
        "version": "0.28",
        "schemas": {
            "StructuralTagResponseFormat": {"type": "object"},
            "LegacyStructuralTag": {"type": "string"},
        },
    }
    assert (
        "structural_tag models named by this build: "
        "['LegacyStructuralTag', 'StructuralTagResponseFormat']"
    ) in capsys.readouterr().out
    assert [r.url.path for r in server.requests if r.method == "GET"] == ["/openapi.json"]


def test_a_build_that_names_none_of_the_models_saves_an_empty_set(
    server: _Server, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    server.handler = lambda request: (
        httpx.Response(200, json={}) if request.method == "GET" else server.ok(request)
    )

    assert probe.main(["probe", str(tmp_path / "o")]) == 0

    saved = json.loads((tmp_path / "o" / "openapi_structural_tag.json").read_text())
    assert saved == {"version": None, "schemas": {}}
    assert "named by this build: []" in capsys.readouterr().out


# ---------------------------------------------------------------- as a script


def test_run_as_a_script_it_exits_with_mains_status(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("SADDLE_VLLM_API_KEY", raising=False)
    monkeypatch.delenv("VLLM_API_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", [probe.__file__ or "", "out"])

    with pytest.raises(SystemExit) as exc_info:
        runpy.run_path(probe.__file__ or "", run_name="__main__")

    assert exc_info.value.code == 2
    assert "SADDLE_VLLM_API_KEY is not set" in capsys.readouterr().err
