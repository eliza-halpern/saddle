"""Brave Answers for the reader's `ask_answers` tool: a lead, under a dollar budget.

Contract: with the `answers` capability on and a key, only the reader may ask Brave
Answers; the answer is labelled a lead, its cited addresses (and no others) become
openable, and a report may cite only pages the reader read; the call streams with
citations (Brave refuses citations on a blocking call), and the answer is the
streamed text with its tags removed and its cited addresses in citation-number
order; a call costs what the stream's own `<usage>` tag says it cost, else $0.004
plus $5 per million input and output tokens from the usage it reports, else an
estimate, refused before a call that the month's cap
cannot cover, reset each UTC month, exhausted by Brave's own limit refusals; no
dollar amount, count or key ever reaches a model-facing text.

All with a fake HTTP transport and a fake clock; nothing reads the person's key.
"""

from __future__ import annotations

import io
import json
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_brave import Clock
from test_mcp_config import BOTH
from test_research import Rig, W, needs_bwrap, tool

from saddle.answers import (
    ESTIMATE_COST,
    LABEL,
    UNAVAILABLE,
    BraveAnswers,
    cost,
    run_answers,
)
from saddle.brave import BraveKeyError, load_key
from saddle.capabilities import Switches, status
from saddle.research import ASK_SCHEMA, ReaderGate, Researcher, validate_report
from saddle.tools import ToolContext, attach_mcp, scope_turn

KEY = "brv-" + "answers-fake-" + "9152"
OCT = datetime(2026, 10, 15, tzinfo=UTC).timestamp()
NOV = datetime(2026, 11, 1, 0, 0, 5, tzinfo=UTC).timestamp()
CITED = "https://docs.example/cited"
ANSWER = "ANSWER-TEXT-QQ the flag is --frobnicate"
SAMPLE = Path(__file__).parent / "fixtures" / "brave_answers_stream.sse"
"""A real streamed reply (stream and citations on), trimmed: favicons dropped,
snippets cut short, the text up to its second bullet."""
BLOCKING_REFUSAL = "Blocking response doesn't support 'enable_citations' option"


def cite(number: int, url: str) -> str:
    return "<citation>" + json.dumps({"number": number, "url": url, "snippet": "s"}) + "</citation>"


def stream(*pieces: str, usage: dict[str, int] | None = None, done: bool = True) -> str:
    """Server-sent events as Brave streams them: one chunk per piece, then a
    final chunk with `finish_reason` and the usage object, then `[DONE]`."""

    def chunk(content: str, finish: str | None, used: Any = None) -> str:
        choice = {"delta": {"role": "assistant", "content": content}, "finish_reason": finish}
        body = {"object": "chat.completion.chunk", "choices": [choice], "usage": used}
        return "data: " + json.dumps(body) + "\n\n"

    events = [chunk(piece, None) for piece in pieces]
    if done:
        events += [chunk("", "stop", usage), "data: [DONE]\n\n"]
    return "".join(events)


class Fake:
    """Brave's Answers endpoint: records requests, answers from the fields."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status = 200
        self.headers: dict[str, str] = {
            "X-Request-Tokens-In": "1000",
            "X-Request-Tokens-Out": "500",
        }
        self.body: Any = None
        self.raises: Exception | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.raises is not None:
            raise self.raises
        body = self.body
        if body is None:
            body = stream("ANSWER-TEXT-QQ the flag", " is --frobnicate", cite(1, CITED))
        if isinstance(body, str):
            return httpx.Response(self.status, text=body, headers=self.headers)
        return httpx.Response(self.status, json=body, headers=self.headers)


def make(tmp: Path, clock: Clock, fake: Fake, cap: float = 4.50) -> BraveAnswers:
    return BraveAnswers(
        KEY,
        tmp / "answers.json",
        cap,
        httpx.Client(transport=httpx.MockTransport(fake)),
        clock,
    )


def write_state(tmp: Path, **over: Any) -> None:
    state: dict[str, Any] = {
        "month": "2026-10",
        "spent": 0.0,
        "calls": 0,
        "estimated": 0,
        "exhausted": False,
    }
    state.update(over)
    (tmp / "answers.json").write_text(json.dumps(state))


def spent(tmp: Path) -> float:
    return float(json.loads((tmp / "answers.json").read_text())["spent"])


# -- the money ----------------------------------------------------------------------------


def test_a_call_costs_the_query_plus_the_reported_tokens(tmp_path: Path) -> None:
    fake = Fake()
    answers = make(tmp_path, Clock(OCT), fake)
    found = answers.ask("what is the flag?")
    assert (found.text, found.urls, found.unavailable) == (ANSWER, [CITED], False)
    assert spent(tmp_path) == pytest.approx(0.004 + 1000 * 5e-6 + 500 * 5e-6)
    assert cost(1_000_000, 0) == pytest.approx(5.004)  # input tokens are charged
    assert cost(0, 1_000_000) == pytest.approx(5.004)  # so are output tokens
    (request,) = fake.requests
    assert request.headers["x-subscription-token"] == KEY
    sent = json.loads(request.content)
    assert (sent["model"], sent["stream"], sent["enable_citations"]) == ("brave", True, True)
    assert sent["messages"] == [{"role": "user", "content": "what is the flag?"}]


def test_usage_in_a_tag_or_the_body_is_read_when_the_headers_have_none(tmp_path: Path) -> None:
    fake = Fake()
    fake.headers = {}
    tag = '<usage>{"X-Request-Tokens-In": 2000, "X-Request-Tokens-Out": 1000}</usage>'
    fake.body = stream("a", tag)
    make(tmp_path, Clock(OCT), fake).ask("q")
    assert spent(tmp_path) == pytest.approx(0.004 + 3000 * 5e-6)
    fake.body = stream("b", cite(1, CITED), usage={"prompt_tokens": 100, "completion_tokens": 50})
    found = make(tmp_path, Clock(OCT), fake).ask("q")
    assert found.urls == [CITED]
    assert spent(tmp_path) == pytest.approx(0.004 + 3000 * 5e-6 + 0.004 + 150 * 5e-6)


def test_the_streams_own_total_cost_is_what_is_charged(tmp_path: Path) -> None:
    fake = Fake()
    tag = (
        '<usage>{"X-Request-Tokens-In": 2000, "X-Request-Tokens-Out": 1000, '
        '"X-Request-Total-Cost": 0.25}</usage>'
    )
    fake.body = stream("a", tag, usage={"prompt_tokens": 2000, "completion_tokens": 1000})
    make(tmp_path, Clock(OCT), fake).ask("q")
    assert spent(tmp_path) == pytest.approx(0.25)  # not cost(2000, 1000) = 0.019
    fake.body = stream("a", '<usage>{"X-Request-Total-Cost": "lots"}</usage>')
    make(tmp_path, Clock(OCT), fake).ask("q")  # unreadable total: the headers' tokens
    assert spent(tmp_path) == pytest.approx(0.25 + cost(1000, 500))


def test_a_response_with_no_usage_is_charged_a_conservative_estimate_and_says_so(
    tmp_path: Path,
) -> None:
    fake = Fake()
    fake.headers = {"X-Request-Tokens-In": "oops"}
    fake.body = stream("a", '<usage>{"X-Request-Total-Cost": -1}</usage>')
    answers = make(tmp_path, Clock(OCT), fake)
    answers.ask("q")
    assert spent(tmp_path) == pytest.approx(ESTIMATE_COST)
    assert ESTIMATE_COST > cost(1000, 500)
    assert answers.snapshot()["estimated"] == 1
    assert "reported no usage" in "\n".join(answers.status_lines())


@pytest.mark.parametrize(
    "body",
    [
        "not json",
        '{"choices": [{"message": {"content": "a blocking reply"}}]}',
        "data: {broken\n\n" + stream("text"),
        stream("text that stops", done=False),
        "data: [1]\n\n" + stream("text"),
    ],
    ids=["not-events", "blocking-json", "bad-event", "no-end", "not-a-chunk"],
)
def test_a_stream_that_cannot_be_read_is_still_billed_and_unavailable(
    tmp_path: Path, body: str
) -> None:
    fake = Fake()
    fake.body = body
    answers = make(tmp_path, Clock(OCT), fake)
    assert answers.ask("q").unavailable
    assert spent(tmp_path) == pytest.approx(ESTIMATE_COST)
    assert "answered badly" in answers.snapshot()["problem"]
    fake.body = "data: [DONE]\n\n" + stream("after the end")
    assert answers.ask("q").unavailable  # nothing before [DONE]: no answer, not the rest


def test_the_recorded_stream_reads_as_its_text_its_cited_pages_and_its_own_cost(
    tmp_path: Path,
) -> None:
    fake = Fake()
    fake.headers = {}
    fake.body = SAMPLE.read_text(encoding="utf-8")
    found = make(tmp_path, Clock(OCT), fake).ask("which DLLs does dgVoodoo2 provide?")
    assert found.text == (
        "**dgVoodoo2** provides the following DLL files for **DirectX 1\u20137** games:\n\n"
        "*   **ddraw.dll**: Required for **DirectDraw** (2D surface management and "
        "blitting) and basic 3D.\n"
        "*   **d3dimm.dll**: Often needed for **Direct3D** games from the late 1990s."
    )
    assert found.urls == [
        "https://dgvoodoo2.com/dgvoodoo2-dlls-you-must-place-in-your-game-folder/",
        "https://grokipedia.com/page/dgVoodoo_2",
        "https://dgvoodoo2.com/set-up-dgvoodoo2-for-any-old-games/",
        "https://dgvoodoo2.com/",
    ]  # citation 4 repeats citation 1's page
    assert spent(tmp_path) == pytest.approx(0.047875)
    assert not found.unavailable


def test_cited_pages_come_in_citation_number_order_once_each(tmp_path: Path) -> None:
    fake = Fake()
    first, second = "https://a.example/1", "https://b.example/2"
    numberless = "<citation>" + json.dumps({"url": "https://c.example/3"}) + "</citation>"
    fake.body = stream("x", cite(2, second), numberless, cite(1, first), cite(3, second))
    found = make(tmp_path, Clock(OCT), fake).ask("q")
    assert found.urls == [first, second, "https://c.example/3"]


def test_brave_refuses_citations_on_a_blocking_call_so_the_call_streams(tmp_path: Path) -> None:
    """Brave's real behaviour: `stream: false` with `enable_citations` is a 422."""

    def brave(request: httpx.Request) -> httpx.Response:
        sent = json.loads(request.content)
        if sent.get("enable_citations") and not sent.get("stream"):
            return httpx.Response(422, json={"detail": BLOCKING_REFUSAL})
        if not sent.get("enable_citations"):
            return httpx.Response(200, text=stream("an answer with no citations"))
        return httpx.Response(200, text=SAMPLE.read_text(encoding="utf-8"))

    answers = BraveAnswers(
        KEY,
        tmp_path / "answers.json",
        http=httpx.Client(transport=httpx.MockTransport(brave)),
        clock=Clock(OCT),
    )
    found = answers.ask("q")
    assert not found.unavailable
    assert len(found.urls) == 4
    assert answers.snapshot()["problem"] == ""


def test_a_422_is_recorded_as_a_refusal_and_not_billed(tmp_path: Path) -> None:
    fake = Fake()
    fake.status, fake.body = 422, {"detail": BLOCKING_REFUSAL}
    answers = make(tmp_path, Clock(OCT), fake)
    assert answers.ask("q").unavailable
    snap = answers.snapshot()
    assert snap["problem"] == "Answers refused the request (HTTP 422)"
    assert (snap["spent"], snap["calls"], snap["exhausted"]) == (0.0, 0, False)


def test_the_cap_is_checked_before_the_call_against_a_per_call_estimate(tmp_path: Path) -> None:
    fake = Fake()
    clock = Clock(OCT)
    write_state(tmp_path, spent=4.50 - ESTIMATE_COST + 0.001)
    answers = make(tmp_path, clock, fake)
    assert not answers.available()
    assert answers.ask("q").unavailable
    assert fake.requests == []  # refused before any call
    write_state(tmp_path, spent=4.50 - ESTIMATE_COST - 0.001)
    assert answers.available()
    assert not answers.ask("q").unavailable
    assert len(fake.requests) == 1


def test_a_new_utc_month_starts_from_zero(tmp_path: Path) -> None:
    fake = Fake()
    clock = Clock(OCT)
    write_state(tmp_path, spent=4.49, calls=300, exhausted=True)
    answers = make(tmp_path, clock, fake)
    assert answers.ask("q").unavailable
    assert fake.requests == []
    clock.t = NOV
    assert not answers.ask("q").unavailable
    snap = answers.snapshot()
    assert (snap["month"], snap["calls"]) == ("2026-11", 1)
    assert snap["spent"] == pytest.approx(0.0115)
    assert not snap["exhausted"]


@pytest.mark.parametrize(
    ("code", "text"),
    [(402, ""), (429, "monthly usage limit reached"), (403, "spend limit exceeded")],
)
def test_a_brave_limit_refusal_marks_the_month_exhausted(
    tmp_path: Path, code: int, text: str
) -> None:
    fake = Fake()
    fake.status, fake.body = code, text or "payment required"
    answers = make(tmp_path, Clock(OCT), fake)
    assert answers.ask("q").unavailable
    assert answers.snapshot()["exhausted"]
    assert answers.ask("again").unavailable
    assert len(fake.requests) == 1  # the second never called Brave
    assert spent(tmp_path) == 0.0  # a refusal is not billed


def test_other_failures_are_unavailable_but_not_exhausted_and_never_echo_the_key(
    tmp_path: Path,
) -> None:
    fake = Fake()
    clock = Clock(OCT)
    answers = make(tmp_path, clock, fake)
    fake.raises = httpx.ConnectError(f"cannot reach with {KEY}")
    assert answers.ask("q").unavailable
    fake.raises = None
    for code, note in ((429, "HTTP 429"), (500, "HTTP 500"), (401, "key is invalid")):
        fake.status, fake.body = code, "rate limited, slow down"
        assert answers.ask("q").unavailable
        snap = answers.snapshot()
        assert not snap["exhausted"]
        assert note in snap["problem"]
    shown = (tmp_path / "answers.json").read_text() + "\n".join(answers.status_lines())
    assert KEY not in shown
    assert stat.S_IMODE((tmp_path / "answers.json").stat().st_mode) == 0o600


# -- the key ------------------------------------------------------------------------------


def test_the_answers_key_has_its_own_line_and_the_same_mode_check(tmp_path: Path) -> None:
    path = tmp_path / "brave.env"
    path.write_text(f"BRAVE_API_KEY=search-only\nexport BRAVE_ANSWERS_API_KEY='{KEY}'\n")
    path.chmod(0o600)
    env = {"SADDLE_BRAVE_ENV_FILE": str(path)}
    found = BraveAnswers.from_env(env)
    assert found is not None
    assert (found.key, found.cap) == (KEY, 4.50)
    assert load_key(env) == "search-only"
    path.chmod(0o644)
    with pytest.raises(BraveKeyError, match="chmod 600") as caught:
        BraveAnswers.from_env(env)
    assert KEY not in str(caught.value)
    path.chmod(0o600)
    path.write_text("BRAVE_API_KEY=search-only\n")
    assert BraveAnswers.from_env(env) is None  # the search key alone is not an Answers key
    assert BraveAnswers.from_env({**env, "SADDLE_BRAVE_ANSWERS_API_KEY": KEY}) is not None
    capped = BraveAnswers.from_env(
        {**env, "SADDLE_BRAVE_ANSWERS_API_KEY": KEY, "SADDLE_BRAVE_ANSWERS_CAP": "2.25"}
    )
    assert capped is not None
    assert capped.cap == 2.25
    bad = BraveAnswers.from_env(
        {**env, "SADDLE_BRAVE_ANSWERS_API_KEY": KEY, "SADDLE_BRAVE_ANSWERS_CAP": "lots"}
    )
    assert bad is not None
    assert bad.cap == 4.50


# -- the reader ---------------------------------------------------------------------------


def reader(tmp: Path, fake: Fake, **state: Any) -> Researcher:
    if state:
        write_state(tmp, **state)
    return Researcher({}, None, tmp / "dl", answers=make(tmp, Clock(OCT), fake))  # type: ignore[arg-type]


def test_the_answer_reaches_the_reader_as_a_lead_and_only_its_citations_become_openable(
    tmp_path: Path,
) -> None:
    fake = Fake()
    fake.body = stream(
        f"{ANSWER} see https://elsewhere.example/mentioned",
        f"<citation>{json.dumps({'url': CITED})}</citation>",
    )
    researcher = reader(tmp_path, fake)
    gate = ReaderGate()
    shown = researcher._ask("what is the flag?", gate, None)
    assert shown.startswith(LABEL)
    assert ANSWER in shown
    assert CITED in shown
    assert gate.check("fetch", {"url": CITED}) is None
    assert gate.check("fetch", {"url": "https://elsewhere.example/mentioned"}) is not None
    assert gate.visited == {}  # told of, not read


def test_a_report_citing_only_the_answers_response_is_refused_until_the_page_is_read(
    tmp_path: Path,
) -> None:
    researcher = reader(tmp_path, Fake())
    gate = ReaderGate()
    researcher._ask("q", gate, None)
    summary = {"kind": "summary", "summary": "The flag is frobnicate [1].", "sources": [CITED]}
    refused = validate_report(summary, gate, None)
    assert isinstance(refused, str)
    assert "did not read" in refused
    gate.note("fetch", {"url": CITED}, "the page says the flag is frobnicate")
    assert not isinstance(validate_report(summary, gate, None), str)
    # a url value taken from the answer is not a read page either, but it was cited: allowed
    assert researcher._ask("   ", gate, None) == "error: ask_answers needs a question"


@needs_bwrap
def test_the_reader_is_offered_ask_answers_and_a_report_on_the_lead_alone_is_refused(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path)
    fake = Fake()
    rig.researcher.answers = make(tmp_path, Clock(OCT), fake)
    try:
        result, model = rig.run(
            [
                [tool("ask_answers", question="what is the flag?")],
                [tool("report", kind="summary", summary="Frobnicate [1].", sources=[CITED])],
                [tool(W + "fetch", url=CITED)],
                [tool("report", kind="value", value_type="version", value="1.2.3")],
            ],
            want="summary",
        )
        assert any(t["function"]["name"] == "ask_answers" for t in model.asked[0]["tools"])
        prompt = model.asked[0]["messages"][0]["content"]
        assert "Answers is costly" in prompt
        told = json.dumps(model.asked[1]["messages"])
        assert "a lead, not a source" in told
        assert CITED in told
        assert "you did not read" in json.dumps(model.asked[2]["messages"])
        assert rig.calls() == [f"fetch {CITED}"]
        assert "1.2.3" in result
    finally:
        rig.close()


@needs_bwrap
def test_the_acting_session_never_sees_the_answer_text_or_the_tool(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.researcher.answers = make(tmp_path, Clock(OCT), Fake())
    try:
        result, model = rig.run(
            [
                [tool("ask_answers", question="what is the flag?")],
                [tool("report", kind="none", reason="not_found")],
            ]
        )
        assert "ANSWER-TEXT-QQ" in json.dumps(model.asked[1]["messages"])  # the reader has it
        assert "ANSWER-TEXT-QQ" not in result
        assert "frobnicate" not in result
        ctx = ToolContext(workdir=tmp_path, research=rig.researcher)
        for lane in ("ask", "edit", "task"):
            offered = scope_turn(ctx, lane)
            assert "ask_answers" not in [t["function"]["name"] for t in offered]
            assert "Answers" not in json.dumps(offered)
        assert rig.researcher.budget_note() == ""
    finally:
        rig.close()


@needs_bwrap
def test_a_reader_without_the_capability_has_no_tool_and_a_called_one_is_unknown(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path)
    try:
        _, model = rig.run(
            [
                [tool("ask_answers", question="q")],
                [tool("report", kind="none", reason="not_found")],
            ]
        )
        assert all(t["function"]["name"] != "ask_answers" for t in model.asked[0]["tools"])
        assert "Answers" not in model.asked[0]["messages"][0]["content"]
        assert "unknown tool" in json.dumps(model.asked[1]["messages"])
    finally:
        rig.close()


def test_when_unavailable_the_reader_gets_one_count_free_sentence(tmp_path: Path) -> None:
    fake = Fake()
    researcher = reader(tmp_path, fake, spent=4.49)
    assert researcher._ask("q", ReaderGate(), None) == UNAVAILABLE
    assert fake.requests == []
    fake.status, fake.body = 402, "x"
    write_state(tmp_path, spent=0.0)
    assert researcher._ask("q", ReaderGate(), None) == UNAVAILABLE
    assert not any(ch.isdigit() for ch in UNAVAILABLE)
    assert "$" not in UNAVAILABLE


@needs_bwrap
def test_no_dollar_amount_or_count_reaches_a_model_facing_text(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    write_state(tmp_path, spent=3.17, calls=211)  # $1.33 left: neither may be told
    rig.researcher.answers = make(tmp_path, Clock(OCT), Fake())
    try:
        result, model = rig.run(
            [
                [tool("ask_answers", question="what is the flag?")],
                [tool("report", kind="none", reason="not_found")],
            ]
        )
        everything = result + json.dumps(model.asked) + json.dumps(ASK_SCHEMA)
        for seen in ("3.17", "1.33", "211", "4.50", "$"):
            assert seen not in everything.replace("$5", "").replace("$4", "")
    finally:
        rig.close()
    # Brave's own text is the only place a dollar sign could come from: not here.
    assert "$" not in UNAVAILABLE + LABEL


# -- the capability and the person's view --------------------------------------------------


def test_the_answers_capability_is_off_by_default_and_unavailable_without_a_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = {r.name: r for r in status(Switches())}
    assert rows["answers"].state == "off"
    rows = {r.name: r for r in status(Switches(answers=True, research=True))}
    assert rows["answers"].state == "unavailable"
    assert "BRAVE_ANSWERS_API_KEY" in rows["answers"].reason
    monkeypatch.setenv("SADDLE_BRAVE_ANSWERS_API_KEY", KEY)
    rows = {r.name: r for r in status(Switches(answers=True))}
    assert rows["answers"].state == "on"
    assert rows["answers"].reason.startswith("used by research, which is off")
    assert KEY not in json.dumps([r.as_json() for r in rows.values()])
    bad = tmp_path / "brave.env"
    bad.write_text(f"BRAVE_ANSWERS_API_KEY={KEY}\n")
    bad.chmod(0o644)
    monkeypatch.delenv("SADDLE_BRAVE_ANSWERS_API_KEY")
    monkeypatch.setenv("SADDLE_BRAVE_ENV_FILE", str(bad))
    rows = {r.name: r for r in status(Switches(answers=True))}
    assert rows["answers"].state == "unavailable"
    assert "chmod 600" in rows["answers"].reason
    assert KEY not in rows["answers"].reason


def test_a_session_gets_answers_only_with_the_capability_and_a_usable_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp_support import write_config

    monkeypatch.setenv("SADDLE_MCP_CONFIG", str(write_config(tmp_path / "c.json", BOTH)))
    ctx = ToolContext(workdir=tmp_path)
    down = tmp_path / "downloads"
    attach_mcp(ctx, down, Switches(research=True, answers=True))
    assert ctx.research is not None  # no key
    assert ctx.research.answers is None
    monkeypatch.setenv("SADDLE_BRAVE_ANSWERS_API_KEY", KEY)
    attach_mcp(ctx, down, Switches(research=True, answers=True))
    assert ctx.research.answers is not None
    attach_mcp(ctx, down, Switches(research=True))
    assert ctx.research.answers is None
    monkeypatch.delenv("SADDLE_BRAVE_ANSWERS_API_KEY")
    bad = tmp_path / "brave.env"
    bad.write_text(f"BRAVE_ANSWERS_API_KEY={KEY}\n")
    bad.chmod(0o644)
    monkeypatch.setenv("SADDLE_BRAVE_ENV_FILE", str(bad))
    attach_mcp(ctx, down, Switches(research=True, answers=True))
    assert ctx.research.answers is None


def test_the_person_sees_the_spend_and_never_the_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out, err = io.StringIO(), io.StringIO()
    assert run_answers("status", stdout=out, stderr=err) == 0
    assert "no Answers key" in out.getvalue()
    monkeypatch.setenv("SADDLE_BRAVE_ANSWERS_API_KEY", KEY)
    monkeypatch.setenv("SADDLE_BRAVE_ANSWERS_STATE", str(tmp_path / "s.json"))
    month = datetime.now(UTC).strftime("%Y-%m")
    (tmp_path / "s.json").write_text(
        json.dumps(
            {
                "month": month,
                "spent": 3.17,
                "calls": 211,
                "estimated": 2,
                "exhausted": True,
                "problem": "p",
            }
        )
    )
    out = io.StringIO()
    assert run_answers("status", stdout=out, stderr=err) == 0
    text = out.getvalue()
    for want in ("$3.1700", "$1.3300", "211 calls", "2 calls reported no usage", "limit", "p"):
        assert want in text
    assert KEY not in text
    bad = tmp_path / "brave.env"
    bad.write_text(f"BRAVE_ANSWERS_API_KEY={KEY}\n")
    bad.chmod(0o644)
    monkeypatch.delenv("SADDLE_BRAVE_ANSWERS_API_KEY")
    monkeypatch.setenv("SADDLE_BRAVE_ENV_FILE", str(bad))
    assert run_answers("status", stdout=io.StringIO(), stderr=err) == 1
    assert "chmod 600" in err.getvalue()
    assert KEY not in err.getvalue()


def test_the_cli_routes_answers_status(monkeypatch: pytest.MonkeyPatch) -> None:
    from saddle.cli import main

    out = io.StringIO()
    assert main(["answers", "status"], stdout=out, stderr=io.StringIO()) == 0
    assert "no Answers key" in out.getvalue()


def test_odd_tags_and_a_non_text_answer_are_tolerated_without_inventing_a_citation(
    tmp_path: Path,
) -> None:
    fake = Fake()
    tags = "<citation>{broken</citation><citation>[1]</citation><usage>7</usage>"
    split = "<citation>" + json.dumps({"number": 1, "url": CITED})
    fake.body = stream("te", "xt" + tags, split[:20], split[20:] + "</citation>")
    found = make(tmp_path, Clock(OCT), fake).ask("q")
    assert (found.text, found.urls) == ("text", [CITED])  # a tag split across chunks is one tag
    fake.body = stream(cite(1, "ftp://files.example/x"), "  ")
    assert make(tmp_path, Clock(OCT), fake).ask("q").unavailable  # tags alone are no answer
    fake.body = stream("x").replace('"content": "x"', '"content": 7')
    assert make(tmp_path, Clock(OCT), fake).ask("q").unavailable
    nothing = 'data: {"choices": [{"delta": {"role": "assistant"}, "finish_reason": "stop"}]}'
    fake.body = 'data: {"choices": []}\n\n' + nothing + "\n\ndata: [DONE]\n\n"
    assert make(tmp_path, Clock(OCT), fake).ask("q").unavailable
