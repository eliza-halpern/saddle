"""A P1 pass reply cut at its token cap is recorded as cut, with the cap.

On a floor extraction, all three prediction (P-b) replies of one task and one
of another's stopped at `PASS_MAX_TOKENS` (`finish_reason=length`). The
sealed calls said only "completion truncated (finish_reason=length)", with no
cap; every example then carried three empty predictions and nothing said
why. Driven here through a real `VllmClient` over a fake server whose
prediction replies stop at the cap, as the served model's did.
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_p1_audit import good
from test_task_examples import val
from test_task_passes import PROPOSAL, TASK, Scripted, predict_reply

from saddle import cli
from saddle.auto import extraction_counts
from saddle.gates import check_task_requirements
from saddle.task_examples import EMPTY_SHA256, SPLIT_NOTE, Prediction, undecided
from saddle.task_passes import PASS_MAX_TOKENS, PREDICT_SEEDS, cut_calls, extract
from saddle.task_requirements import load
from saddle.vllm import VllmClient, VllmResponseError

__all__ = ["good"]

CUT_REPLY = '{"predictions": [{"input": "E-001", "outcome": {"kind": "value", "te'


class CapServer:
    """Answers each P1 pass from `Scripted`; a P-b request whose seed is in
    `cut` stops at its `max_tokens` mid-JSON, finish_reason "length"."""

    def __init__(self, scripted: Scripted, cut: tuple[int, ...]) -> None:
        self.scripted = scripted
        self.cut = cut
        self.caps: list[int] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        prompt = str(payload["messages"][-1]["content"])
        seed = payload.get("seed")
        self.caps.append(int(payload["max_tokens"]))
        if prompt.startswith("You predict") and seed in self.cut:
            message = {"role": "assistant", "content": CUT_REPLY, "reasoning": "r" * 90}
            usage = {"prompt_tokens": 900, "completion_tokens": payload["max_tokens"]}
            choice = {"index": 0, "message": message, "finish_reason": "length"}
            return httpx.Response(200, json={"choices": [choice], "usage": usage})
        reply = self.scripted.complete(prompt, seed=seed, temperature=payload["temperature"])
        message = {"role": "assistant", "content": reply}
        return httpx.Response(
            200, json={"choices": [{"index": 0, "message": message, "finish_reason": "stop"}]}
        )

    def client(self, **kw: Any) -> VllmClient:
        return VllmClient(**{"api_key": "test-key", **kw}, transport=httpx.MockTransport(self))


def test_the_client_names_the_cap_a_completion_was_cut_at() -> None:
    server = CapServer(Scripted(PROPOSAL), cut=(PREDICT_SEEDS[0],))
    with pytest.raises(VllmResponseError) as raised:
        server.client().complete("You predict", max_tokens=321, seed=PREDICT_SEEDS[0])
    assert str(raised.value) == "completion truncated at 321 output tokens (finish_reason=length)"
    assert (raised.value.max_tokens, raised.value.finish_reason) == (321, "length")


def test_every_prediction_reply_cut_is_sealed_as_cut_with_its_cap() -> None:
    """Every P-b reply cut: each call says so and names the cap, and each
    example's three missing predictions say why."""
    server = CapServer(Scripted(PROPOSAL), cut=PREDICT_SEEDS)
    record = extract(TASK, server.client())
    predicting = [c for c in record["calls"] if c["pass"] == "P-b"]
    assert [c["cut_at"] for c in predicting] == [PASS_MAX_TOKENS] * 3
    assert {c["error"] for c in predicting} == {
        f"VllmResponseError: completion truncated at {PASS_MAX_TOKENS} output tokens "
        "(finish_reason=length)"
    }
    assert server.caps == [PASS_MAX_TOKENS] * 4
    said = f"a prediction reply was cut at the {PASS_MAX_TOKENS}-token cap"
    assert [[p["missing"] for p in e["predictions"]] for e in record["examples"]] == [
        [said] * 3
    ] * len(record["examples"])
    assert cut_calls(record) == (
        f"3 of 4 model call(s) cut at the {PASS_MAX_TOKENS}-token cap "
        "(P-b seed 11, P-b seed 23, P-b seed 37)"
    )
    assert extraction_counts(record).endswith(f", {cut_calls(record)}")


def test_one_prediction_reply_cut_leaves_the_other_two_whole() -> None:
    server = CapServer(Scripted(PROPOSAL), cut=(PREDICT_SEEDS[2],))
    record = extract(TASK, server.client())
    # No example has three agreeing predictions, so no P-c call is made.
    assert [c.get("cut_at") for c in record["calls"]] == [None, None, None, PASS_MAX_TOKENS]
    first = record["examples"][0]["predictions"]
    assert [p["outcome"] is not None for p in first] == [True, True, False]
    assert ["missing" in p for p in first] == [False, False, True]
    assert cut_calls(record).startswith(f"1 of 4 model call(s) cut at the {PASS_MAX_TOKENS}")


def test_a_run_with_no_cut_reply_says_nothing_of_one() -> None:
    record = extract(TASK, CapServer(Scripted(PROPOSAL), cut=()).client())
    assert cut_calls(record) == ""
    assert extraction_counts(record).endswith("probe(s)")
    assert all("missing" not in p for e in record["examples"] for p in e["predictions"])


@pytest.mark.parametrize(
    ("predict", "said"),
    [
        (lambda seed: RuntimeError("server down"), "a prediction call failed (RuntimeError)"),
        (lambda seed: "", "a prediction call returned nothing"),
        (lambda seed: "no json here", "a prediction reply did not parse"),
        (lambda seed: '{"predictions": []}', "a prediction reply gave no outcome for this input"),
        (
            lambda seed: json.dumps({"predictions": [{"input": "E-001", "outcome": {"k": 1}}]}),
            "a prediction reply's outcome for this input did not parse",
        ),
    ],
)
def test_a_missing_prediction_says_what_happened(predict: Any, said: str) -> None:
    record = extract(TASK, Scripted(PROPOSAL, predict=predict))
    assert record["examples"][0]["predictions"][0]["missing"] == said


def test_the_extract_command_names_the_cut_calls(
    good: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = CapServer(Scripted(PROPOSAL), cut=PREDICT_SEEDS)
    monkeypatch.setattr(cli, "_api_key", lambda: "test-key")
    monkeypatch.setattr(cli, "VllmClient", server.client)
    task = tmp_path / "task.md"
    task.write_text(TASK)
    out = io.StringIO()
    argv = ["requirements", "extract", str(task), "--out", str(tmp_path / "s.json")]
    assert cli.main([*argv, "--repo", str(good)], stdout=out) == 0
    assert (
        out.getvalue()
        .rstrip()
        .endswith(
            f"; 3 of 4 model call(s) cut at the {PASS_MAX_TOKENS}-token cap "
            "(P-b seed 11, P-b seed 23, P-b seed 37)"
        )
    )


# -- the question a missing prediction asks says what happened -----------------------


def questions(record: dict[str, Any], tmp_path: Path) -> list[str]:
    """What the gate says of each example the predictions did not decide, over
    the sealed record loaded as an audit loads it, on a tree that returns
    [1, 1, 2, 2], at full strength: each is a question, never a refusal and
    never a pass."""
    path = tmp_path / "task-requirements.json"
    path.write_text(json.dumps(record))
    req = load(path, TASK)
    results = {e.id: val([1, 1, 2, 2]) for e in req.examples}
    check = check_task_requirements(results, req.units, req.examples, licensed=True)
    undecided_rows = [r for r in check.rows if r.klass.route == "split"]
    assert undecided_rows
    assert {r.status for r in undecided_rows} == {"question"}
    return [r.why for r in undecided_rows]


def test_every_reply_cut_asks_why_not_a_disagreement(tmp_path: Path) -> None:
    server = CapServer(Scripted(PROPOSAL), cut=PREDICT_SEEDS)
    asked = questions(extract(TASK, server.client()), tmp_path)
    assert asked == [
        f"3 of 3 predictions missing (a prediction reply was cut at the {PASS_MAX_TOKENS}-token "
        "cap)"
    ] * len(asked)
    assert asked


def test_one_reply_cut_asks_with_the_two_readings_that_came(tmp_path: Path) -> None:
    server = CapServer(Scripted(PROPOSAL), cut=(PREDICT_SEEDS[2],))
    asked = questions(extract(TASK, server.client()), tmp_path)
    assert asked[0] == (
        f"1 of 3 predictions missing (a prediction reply was cut at the {PASS_MAX_TOKENS}-token "
        "cap): [1, 1, 2, 2]"
    )
    assert not any(SPLIT_NOTE in a for a in asked)


def test_a_cut_reply_and_two_that_differ_say_both(tmp_path: Path) -> None:
    def differing(seed: int) -> str:
        reply = json.loads(predict_reply(seed))
        if seed == PREDICT_SEEDS[1]:
            reply["predictions"][0]["outcome"]["text"] = "[1, 2, 1, 2]"
        return json.dumps(reply)

    server = CapServer(Scripted(PROPOSAL, predict=differing), cut=(PREDICT_SEEDS[2],))
    asked = questions(extract(TASK, server.client()), tmp_path)
    assert asked[0] == (
        f"1 of 3 predictions missing (a prediction reply was cut at the {PASS_MAX_TOKENS}-token "
        f"cap); {SPLIT_NOTE}: [1, 1, 2, 2] / [1, 2, 1, 2]"
    )


def test_a_prediction_sealed_without_a_reason_says_what_its_record_shows() -> None:
    """A file sealed before predictions carried `missing`: an empty reply's hash
    says the call returned nothing; any other says only that no outcome came."""
    empty = Prediction(None, "", EMPTY_SHA256)
    other = Prediction(None, "", "0" * 64)
    assert EMPTY_SHA256 == hashlib.sha256(b"").hexdigest()
    assert undecided((empty, empty, empty)) == (
        "3 of 3 predictions missing (a prediction call returned nothing)"
    )
    assert undecided((other, empty)) == (
        "2 of 2 predictions missing (a predictor gave no outcome for this input; a prediction "
        "call returned nothing); 2 predictions recorded where 3 are needed"
    )
