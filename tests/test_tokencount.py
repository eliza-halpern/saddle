"""Counting with the served model's own tokenizer when the server counts none (#80a1 r1).

Strata serves no /tokenize, so every limit saddle keeps in tokens had no count and fell
back to lines: one command's output of a few very long lines (417,831 characters, a
grep through node_modules' source maps) went whole into a run's context, and the next
request overflowed a 131,072-token window. `SADDLE_TOKENIZER` names the model's
`tokenizer.json`; `VllmClient.count_tokens` asks the server first and counts with it
only when the server answers no count.

Known-good: the server's count when it gives one; the local count when it does not;
the few-long-lines output cut to the token limit. Known-bad: a tokenizer that is named
but cannot be loaded, or a missing `tokenizers` package, stopping saddle at the start
rather than running uncounted; an image counted as if it were text.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from tokenizers import Tokenizer, models, pre_tokenizers  # type: ignore[import-untyped]

from saddle import tools
from saddle.tokencount import TOKENIZER_ENV, LocalTokenizer, TokenizerError, framed
from saddle.tools import ToolContext
from saddle.vllm import VllmClient

MESSAGE = [{"role": "user", "content": "hello world"}]


@pytest.fixture
def tokenizer_file(tmp_path: Path) -> Path:
    """A real, tiny tokenizer: one token per whitespace-split word, the chat
    markers each one special token."""
    vocab = {"[UNK]": 0, "user": 1, "hello": 2, "world": 3}
    tok = Tokenizer(models.WordLevel(vocab, unk_token="[UNK]"))
    tok.pre_tokenizer = pre_tokenizers.WhitespaceSplit()
    tok.add_special_tokens(["<|im_start|>", "<|im_end|>"])
    path = tmp_path / "tokenizer.json"
    tok.save(str(path))
    return path


def _client(status: int, body: dict[str, Any], local: LocalTokenizer | None) -> VllmClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)

    return VllmClient(
        api_key="k", base_url="http://h/v1", transport=httpx.MockTransport(handler), tokenizer=local
    )


# -- the framing ------------------------------------------------------------------


def test_a_message_is_framed_as_the_template_frames_it_its_content_trimmed() -> None:
    said = framed([{"role": "user", "content": "  hi there \n"}])
    assert said == "<|im_start|>user\nhi there<|im_end|>\n"


def test_reasoning_tool_calls_and_tool_results_are_framed_in_their_tags() -> None:
    call = {"id": "c1", "function": {"name": "f", "arguments": "{}"}}
    said = framed(
        [
            {
                "role": "assistant",
                "content": "ok",
                "reasoning_content": " think ",
                "tool_calls": [call],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "done"},
        ]
    )
    assert said is not None
    assert said.startswith("<|im_start|>assistant\n<think>\nthink\n</think>\n\nok\n<tool_call>\n")
    assert json.dumps(call) in said
    assert said.endswith("<|im_start|>user\n<tool_response>\ndone\n</tool_response><|im_end|>\n")


def test_a_message_holding_an_image_is_not_counted_as_text(tokenizer_file: Path) -> None:
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    holding = [{"role": "user", "content": [{"type": "text", "text": "see"}, image]}]
    assert framed(holding) is None
    assert LocalTokenizer.load(tokenizer_file).count(holding) is None
    assert framed([{"role": "user", "content": {"not": "text"}}]) is None
    parts = [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
    assert framed([{"role": "user", "content": parts}]) == "<|im_start|>user\nab<|im_end|>\n"
    assert framed([{"role": "assistant", "content": None}]) == "<|im_start|>assistant\n<|im_end|>\n"


# -- loading ----------------------------------------------------------------------


def test_an_unset_name_loads_nothing_and_a_set_one_loads_its_tokenizer(
    tokenizer_file: Path,
) -> None:
    assert LocalTokenizer.from_env({}) is None
    assert LocalTokenizer.from_env({TOKENIZER_ENV: "  "}) is None
    local = LocalTokenizer.from_env({TOKENIZER_ENV: str(tokenizer_file)})
    assert local is not None
    # <|im_start|> user hello world <|im_end|>: five tokens of the real tokenizer
    assert local.count(MESSAGE) == 5
    assert local.count([{"role": "user", "content": "hello hello world"}]) == 6


@pytest.mark.parametrize("contents", [None, "not a tokenizer"])
def test_a_named_tokenizer_that_cannot_be_loaded_stops_saddle_naming_it(
    tmp_path: Path, contents: str | None
) -> None:
    path = tmp_path / "tokenizer.json"
    if contents is not None:
        path.write_text(contents)
    with pytest.raises(TokenizerError, match="not a readable tokenizer") as raised:
        LocalTokenizer.from_env({TOKENIZER_ENV: str(path)})
    assert str(path) in str(raised.value)


def test_without_the_tokenizers_package_a_named_tokenizer_says_which_extra(
    tokenizer_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "tokenizers", None)
    with pytest.raises(TokenizerError, match=r"install saddle\[tokenizer\]"):
        LocalTokenizer.load(tokenizer_file)


def test_the_client_reads_the_name_from_the_environment_and_refuses_a_broken_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TOKENIZER_ENV, str(tmp_path / "missing.json"))
    with pytest.raises(TokenizerError):
        VllmClient(api_key="k", base_url="http://h/v1")


def test_the_tools_schemas_are_counted_too(tokenizer_file: Path) -> None:
    local = LocalTokenizer.load(tokenizer_file)
    tool = {"type": "function", "function": {"name": "read_file"}}
    plain = local.count(MESSAGE)
    with_tool = local.count(MESSAGE, tools=[tool])
    assert plain is not None
    assert with_tool is not None
    assert with_tool > plain


# -- the server first, then the model's own tokenizer -----------------------------


def test_the_servers_count_wins_when_it_gives_one(tokenizer_file: Path) -> None:
    client = _client(200, {"count": 948}, LocalTokenizer.load(tokenizer_file))
    assert client.count_tokens(MESSAGE) == 948


def test_a_server_that_counts_none_is_counted_with_the_models_tokenizer(
    tokenizer_file: Path,
) -> None:
    local = LocalTokenizer.load(tokenizer_file)
    assert _client(404, {"error": {"message": "not found"}}, local).count_tokens(MESSAGE) == 5
    assert _client(200, {"no": "count"}, local).count_tokens(MESSAGE) == 5
    assert _client(404, {}, None).count_tokens(MESSAGE) is None


def test_an_unreachable_server_is_counted_with_the_models_tokenizer(tokenizer_file: Path) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        msg = "refused"
        raise httpx.ConnectError(msg, request=request)

    client = VllmClient(
        api_key="k",
        base_url="http://h/v1",
        transport=httpx.MockTransport(down),
        tokenizer=LocalTokenizer.load(tokenizer_file),
    )
    assert client.count_tokens(MESSAGE) == 5


# -- the run that overflowed ------------------------------------------------------


def test_a_few_very_long_lines_are_cut_to_the_token_limit_on_a_server_that_counts_none(
    tokenizer_file: Path,
) -> None:
    """#80a1 r1's shape: a handful of lines, each far over the limit. With no count it
    came back whole (a few lines are under the line fallback); counted, it is cut."""
    text = "\n".join(" ".join(["hello"] * 20_000) for _ in range(3)) + "\n"
    counted = _client(404, {}, LocalTokenizer.load(tokenizer_file))
    ctx = ToolContext(workdir=Path("."))
    ctx.count_tokens = lambda t: counted.count_tokens([{"role": "user", "content": t}])
    fitted = tools._fit_output(ctx, text)
    assert len(fitted.split()) < tools.READ_TOKENS, len(fitted.split())
    assert "lines elided" in fitted
    assert "tokens" in fitted.split("lines elided")[1].split("\n")[0]
    uncounted = ToolContext(workdir=Path("."))
    uncounted.count_tokens = lambda t: _client(404, {}, None).count_tokens(
        [{"role": "user", "content": t}]
    )
    assert tools._fit_output(uncounted, text) == text  # no tokenizer named: still whole
