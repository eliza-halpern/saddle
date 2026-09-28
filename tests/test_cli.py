"""Tests for saddle.cli."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from textwrap import dedent
from typing import Any, ClassVar, Final, cast

import httpx
import pytest

from saddle.cli import (
    CONTENTS_WITHHELD,
    DEFAULT_CONTEXT_WINDOW,
    EDIT_RULES,
    EMIT_ROUNDS,
    MAX_FILES_IN_PROMPT,
    OUTPUT_MARGIN,
    PROMPT_CHARS_PER_TOKEN,
    RUN_ALLOWLIST,
    TOOL_BINDINGS,
    WHOLE_FILE_RULES,
    DagOptions,
    RunError,
    RunOptions,
    _emit_valid_dag,
    _ensure_repo,
    _file_lines,
    build_emit_prompt,
    build_parser,
    build_recovery_plan_prompt,
    build_repair_prompt,
    build_replan_task,
    build_structured_prompt,
    build_task_first_prompt,
    build_worker_prompt,
    check_server,
    diff_budget,
    main,
    render_dag_plan,
    run_dag,
    run_doctor,
    run_explain,
    run_tail,
    run_task,
    run_verify,
    served_version,
    server_context_window,
    survivor_drawer,
    worker_max_tokens,
)
from saddle.dag import DIFF_OVERHEAD_TOKENS, REQ_NEAR_MISS_K, TOKENS_PER_LINE, Dag, Node
from saddle.edits import apply_edits, parse_edits
from saddle.evidence import CapturedRun, git_ls_files, ruff_version, run_argv
from saddle.gates import GateCheck, Tier1Result, check_node_scope
from saddle.journal import (
    GateOutput,
    ProofRecord,
    SpanRecord,
    append_record,
    append_span,
    attempt_sidecar_path,
    build_record,
    build_span,
    read_records,
    read_spans,
    write_attempt_sidecar,
)
from saddle.slice import (
    PROPOSAL_SAMPLES,
    SURVIVOR_SAMPLES,
    TASK_FIRST_PREAMBLE,
    format_attempt_failure,
)
from saddle.vllm import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    DagEmission,
    DiffProposal,
    VllmAuthError,
    VllmClient,
    VllmRequestError,
)

TASK = "Fix f to return 2 and add a passing test."


def whole_file(path: str, *lines: str) -> str:
    """One write section: the envelope the worker grammar admits.

    The inverse of `slice._payload_sections`. Tests spell payloads through
    this rather than by hand so that a change to the envelope breaks in
    one place instead of ninety.
    """
    body = "".join(f"+{line}\n" for line in lines)
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- /dev/null\n+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n{body}"
    )


DIFF = whole_file("n.py", "def f():", "    return 2")


def _git_repo(root: Path) -> None:
    for argv in (
        ["git", "init"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "test"],
    ):
        assert run_argv(argv, root) == 0
    (root / "n.py").write_text("def f():\n    return 1\n")
    (root / "test_n.py").write_text(
        "from n import f\n\n\ndef test_f_returns_two():  # REQ-001\n    assert f() == 2\n"
    )
    (root / "README.md").write_text("demo\n")
    assert run_argv(["git", "add", "n.py", "test_n.py", "README.md"], root) == 0
    assert run_argv(["git", "commit", "-m", "baseline"], root) == 0


def _node_dict(
    node_id: str = "n1",
    budget: str = "low",
    kill_threshold: float = 85.0,
    *,
    tools: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": node_id,
        "kind": "impl",
        "dependencies": [],
        # Declared scope is required of an impl node (the planner cannot
        # leave it empty); the fixture's diff touches `n.py` alone.
        "target_files": ["n.py"],
        "task_prompt": "Fix f and test it.",
        "requirements": [
            {"id": "REQ-001", "statement": "REQ-001 holds.", "accepts": ["2"], "rejects": ["3"]}
        ],
        "execution_constraints": {
            "reasoning_budget": budget,
            # All four by default: each name is bound to a behaviour, and
            # the default fixture is the known-good "behaves exactly as
            # today" node. Tests that pin a binding pass a shorter list.
            "allowed_tools": tools if tools is not None else list(RUN_ALLOWLIST),
            "max_context_tokens": 8000,
        },
        "deterministic_gate": {
            "test_command": "pytest test_n.py",
            "changed_line_coverage_min": 100.0,
            "red_phase_required": True,
            "mutation_sample": {
                "scope": "changed-lines",
                "max_mutants": 100,
                "kill_threshold": kill_threshold,
            },
        },
    }


def _expected_cap(call: dict[str, Any], window: int = DEFAULT_CONTEXT_WINDOW) -> int:
    """The `max_tokens` a worker call earns: the window left after its own
    prompt; nothing is held back for reasoning."""
    return window - len(_prompt(call)) // PROMPT_CHARS_PER_TOKEN - OUTPUT_MARGIN


def _emit_response(payload: dict[str, Any]) -> httpx.Response:
    body = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": json.dumps(payload),
                    "reasoning": "a solid plan",
                },
                "finish_reason": "stop",
            }
        ],
        "model": "m",
    }
    return httpx.Response(200, json=body)


def _diff_response(diff: str = DIFF) -> httpx.Response:
    """The diff is raw grammar-constrained text, not a JSON-wrapped string."""
    return _text_response(diff)


def _text_response(text: str) -> httpx.Response:
    body = {
        "choices": [
            {
                "message": {"role": "assistant", "content": text, "reasoning": ""},
                "finish_reason": "stop",
            }
        ],
        "model": "m",
    }
    return httpx.Response(200, json=body)


def _is_diff_request(payload: dict[str, Any]) -> bool:
    """A guided diff call, as opposed to emission or free-text recovery."""
    outputs = payload.get("structured_outputs")
    return isinstance(outputs, dict) and "grammar" in outputs


def _scripted_client(script: list[httpx.Response], seen: list[dict[str, Any]]) -> VllmClient:
    """Serve a script that describes *attempts*, not individual calls.

    The first attempt now draws PROPOSAL_SAMPLES independent proposals
    (#59), so one scripted diff response answers that whole sampling
    round rather than a single call. Identical samples are evaluated once
    by the selector, so repeating the response is faithful: the worker
    really was asked k times and really did say the same thing.
    """
    pending: list[int] = [0]
    # The k draws of one sampling round arrive concurrently; the
    # counter and the script are shared state, so the handler serialises.
    lock = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        with lock:
            seen.append(payload)
            if _is_diff_request(payload) and pending[0] < PROPOSAL_SAMPLES - 1:
                pending[0] += 1
                return script[0]
            pending[0] = 0
            return script.pop(0)

    return VllmClient(api_key="k", transport=httpx.MockTransport(handler))


def _options(repo: Path, **overrides: Any) -> RunOptions:
    base: dict[str, Any] = {
        "task": TASK,
        "repo": repo,
        "journal": repo / "proofs.jsonl",
        "yes": True,
    }
    return RunOptions(**(base | overrides))


def _models_client(payload: Any, *, status: int = 200) -> VllmClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    return VllmClient(api_key="k", transport=httpx.MockTransport(handler))


def _unreachable_client() -> VllmClient:
    def handler(request: httpx.Request) -> httpx.Response:
        msg = "refused"
        raise httpx.ConnectError(msg)

    return VllmClient(api_key="k", transport=httpx.MockTransport(handler))


def _dag_client(
    models: list[str], dag: dict[str, Any], seen: list[httpx.Request] | None = None
) -> VllmClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": name} for name in models]})
        return _emit_response(dag)

    return VllmClient(api_key="k", transport=httpx.MockTransport(handler))


def test_main_no_args_returns_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert capsys.readouterr().out == ""


def test_version_flag_prints_and_exits(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit, match=r"^0$"):
        main(["--version"])
    out = capsys.readouterr().out
    assert out.startswith("saddle 0.1.0")


def test_help_flag_shows_exact_description(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit, match=r"^0$"):
        main(["--help"])
    out = capsys.readouterr().out
    assert "\nDeterministic harness for local LLMs.\n" in out
    assert "    dag                 Show the plan before it runs.\n" in out
    assert "    doctor              Check the server is usable.\n" in out
    assert "    run                 Drive one mechanical task end to end.\n" in out
    assert "    verify              Audit a journal and re-render its transcript.\n" in out
    assert "    tail                Follow a live run as it happens.\n" in out
    assert "    up                  Open an interactive streaming chat session.\n" in out


def test_build_emit_prompt_names_task_and_rules() -> None:
    prompt = build_emit_prompt("Do the thing.")
    assert "Do the thing." in prompt
    assert "reasoning_budget is one of: zero, low, medium, xhigh." in prompt
    assert "take low or zero; reserve medium/xhigh" in prompt
    # Was: `assert "use the full 30000 unless the node touches one small"`.
    # The suite pinned the guidance #56 removes -- context is a cost, not
    # an allowance -- so the assertion is inverted rather than deleted.
    assert "use the full 30000" not in prompt
    assert "smallest figure that covers the files" in prompt
    assert "red_phase_required is always true." in prompt
    assert "read_file, write_file, run_tests, lint" in prompt
    assert "target_files lists the repo-relative files the node may touch" in prompt
    assert "Required for impl and refactor nodes" in prompt
    assert "rejected before it runs" in prompt
    assert "may not create or rename files" in prompt
    assert 'never start one with "/"' in prompt
    assert 'never use "/"' not in prompt
    # The four rules two smoke runs showed the planner needs.
    assert 'Never name a package "src"' in prompt
    assert "cite the requirement id of every node they specify" in prompt
    assert "every file the node will create as well as edit" in prompt
    assert "names every test file it will write" in prompt
    assert "over test files only" in prompt
    # Was: `assert "fewest nodes" in prompt`. The suite pinned the
    # guidance #51 removes: "prefer the fewest nodes" is right for T1 and
    # actively wrong for T5, whose 443 lines across four modules went to
    # one worker as a single diff and died on finish_reason=length.
    # Inverted rather than deleted, as with the #56 context guidance.
    assert "fewest nodes" not in prompt
    assert "Size the plan to the work" in prompt
    assert "one worker's single" in prompt


def test_build_worker_prompt_tells_the_worker_its_target_files() -> None:
    """A measured round's second node wrote a test file no node listed: the
    gate rejected it, but the worker had never been told the list."""
    scoped = Node.model_validate({**_node_dict(), "target_files": ["n.py", "m.py"]})
    prompt = build_worker_prompt(task=TASK, node=scoped, files=["n.py"], contents={})
    assert "Touch only these files: n.py, m.py." in prompt
    assert "rejects a diff that names any other file" in prompt
    unscoped = Node.model_validate({**_node_dict(), "target_files": []})
    prompt = build_worker_prompt(task=TASK, node=unscoped, files=["n.py"], contents={})
    assert "Touch only these files" not in prompt


def test_build_worker_prompt_covers_format_rules_and_files() -> None:
    node = Node.model_validate(_node_dict())
    prompt = build_structured_prompt(
        task=TASK, node=node, files=["n.py", "m.py"], contents={"n.py": "x = 1\n"}
    )
    assert TASK in prompt
    assert "REQ-001" in prompt
    assert "Gate command: pytest test_n.py" in prompt
    assert "n.py" in prompt
    assert "m.py" in prompt
    assert "--- n.py ---\nx = 1\n" in prompt
    assert 'diff --git a/<file> b/<file>" header line' in prompt
    # The envelope's own rules, each the half a worker could get wrong. A
    # brief that asks for whole files but still says "patch" is the defect
    # this pins.
    assert "COMPLETE NEW CONTENTS" in prompt
    assert 'each prefixed with "+"' in prompt
    assert 'A line starting with " " or "-" is' in prompt
    assert "Write each file at most once" in prompt
    assert "deleted file mode 100644" in prompt
    assert "no original side to" in prompt
    assert "two blank lines between top-level definitions" in prompt
    assert "sorted import blocks" in prompt
    assert "Output ONLY the file sections" in prompt


def test_build_worker_prompt_caps_long_file_lists() -> None:
    node = Node.model_validate(_node_dict())
    files = [f"f{i}.py" for i in range(150)]
    prompt = build_worker_prompt(task=TASK, node=node, files=files, contents={})
    assert "f0.py\nf1.py\n" in prompt
    assert "f99.py" in prompt
    assert "f100.py" not in prompt
    assert "... and 50 more" in prompt
    empty = build_worker_prompt(task=TASK, node=node, files=[], contents={})
    assert "Repo files:\n(no tracked files)\n" in empty
    exact = build_worker_prompt(
        task=TASK, node=node, files=[f"f{i}.py" for i in range(100)], contents={}
    )
    assert "more" not in exact


def test_build_worker_prompt_joins_requirements_and_context() -> None:
    node_dict = _node_dict()
    node_dict["requirements"] = [
        {"id": "REQ-001", "statement": "REQ-001 holds.", "accepts": ["2"], "rejects": ["3"]},
        {"id": "REQ-002", "statement": "REQ-002 holds.", "accepts": ["2"], "rejects": ["3"]},
    ]
    node = Node.model_validate(node_dict)
    prompt = build_structured_prompt(
        task=TASK,
        node=node,
        files=["a.py", "b.py"],
        contents={"a.py": "1\n", "b.py": "2\n"},
    )
    assert "  REQ-001: REQ-001 holds.\n    accepts: '2'\n    rejects: '3'\n" in prompt
    assert "  REQ-002: REQ-002 holds.\n    accepts: '2'\n    rejects: '3'\n" in prompt
    assert "--- a.py ---\n1\n\n\n--- b.py ---\n2\n" in prompt
    # The worker hears both rules (assert on each example; one property
    # rejects an input), as rules it can follow.
    assert "binds every listed accept and reject, and the gate\n  fails one" in prompt
    assert "PERFORMS that operation and asserts on the\n  constant values" in prompt
    assert "At least one property must reject an\n  input" in prompt
    assert "(`assert not ...`, `is False`, or `pytest.raises`)" in prompt


def test_build_worker_prompt_lists_every_example() -> None:
    node_dict = _node_dict()
    node_dict["requirements"] = [
        {
            "id": "REQ-001",
            "statement": "Rejects a local part ending in a dot.",
            "accepts": ["user@example.com", "a.b@example.com"],
            "rejects": ["user@@example.com", "user@example.com."],
        }
    ]
    prompt = build_structured_prompt(
        task=TASK, node=Node.model_validate(node_dict), files=[], contents={}
    )
    assert (
        "  REQ-001: Rejects a local part ending in a dot.\n"
        "    accepts: 'user@example.com', 'a.b@example.com'\n"
        "    rejects: 'user@@example.com', 'user@example.com.'\n"
    ) in prompt


def test_build_worker_prompt_truncates_large_context() -> None:
    from saddle.cli import MAX_CONTEXT_CHARS

    node = Node.model_validate(_node_dict())
    big = "x" * (MAX_CONTEXT_CHARS + 1000)
    prompt = build_structured_prompt(task=TASK, node=node, files=["n.py"], contents={"n.py": big})
    assert "[file context truncated]" in prompt
    assert "x" * (MAX_CONTEXT_CHARS + 1000) not in prompt
    # The node task is restated after the contents (#56), so the context
    # section now ends at that restatement rather than at the diff rules.
    context = prompt.split("File contents:\n")[1].split("\n\nNode ")[0]
    assert context.endswith("[file context truncated]")


def test_context_budget_is_thirty_k_tokens_at_four_chars_each() -> None:
    from saddle.cli import CHARS_PER_TOKEN, MAX_CONTEXT_CHARS, NODE_CONTEXT_TOKENS

    assert NODE_CONTEXT_TOKENS == 30000
    assert CHARS_PER_TOKEN == 4
    assert MAX_CONTEXT_CHARS == 120000


def test_build_worker_prompt_keeps_exactly_max_context() -> None:
    from saddle.cli import CHARS_PER_TOKEN

    node = Node.model_validate(_node_dict())
    budget = node.execution_constraints.max_context_tokens * CHARS_PER_TOKEN
    body = "y" * (budget - len("--- n.py ---\n"))
    prompt = build_worker_prompt(task=TASK, node=node, files=["n.py"], contents={"n.py": body})
    assert "[file context truncated]" not in prompt


def test_build_worker_prompt_truncates_at_node_ceiling_not_global() -> None:
    """`max_context_tokens` bounds the node's reading, per ARCHITECTURE.md §2."""
    from saddle.cli import CHARS_PER_TOKEN, MAX_CONTEXT_CHARS

    node_dict = _node_dict()
    node_dict["execution_constraints"]["max_context_tokens"] = 8000
    node = Node.model_validate(node_dict)
    budget = 8000 * CHARS_PER_TOKEN
    assert budget < MAX_CONTEXT_CHARS
    body = "y" * (budget + 1000)
    prompt = build_worker_prompt(task=TASK, node=node, files=["n.py"], contents={"n.py": body})
    assert "[file context truncated]" in prompt


def test_build_repair_prompt_adds_failure_evidence() -> None:
    node = Node.model_validate(_node_dict())
    prompt = build_repair_prompt(
        task=TASK,
        node=node,
        files=["n.py"],
        contents={"n.py": "def f():\n    return 3\n"},
        failure="Attempt 1 of 3 failed 1 gate(s):\n- tests: exited 1\n",
        plan="1. Change the return value.\n",
    )
    assert TASK in prompt
    assert "Fix forward" in prompt
    assert "CURRENT tree state" in prompt
    assert "write the files above" in prompt
    assert "starting from the CURRENT tree state shown" in prompt
    assert "so that the failure below is repaired." in prompt
    assert "Attempt 1 of 3 failed 1 gate(s):" in prompt
    assert "Recovery plan:\n1. Change the return value.\n" in prompt
    assert "1. Change the return value.\n\n\nAttempt 1 of 3" in prompt
    assert "--- n.py ---\ndef f():\n    return 3\n" in prompt


def test_the_repair_prompt_does_not_tell_an_edit_worker_to_restate_the_files() -> None:
    """Known-good and known-bad on one function, from `runs/g1-0f6b83d`.

    The initial worker prompt there drew three edit payloads that applied
    and were gated. The repair prompt carried the same `EDIT_RULES` and,
    after them, "write the files above as they should now be" -- and that
    worker spent 84 493 output tokens emitting no payload at all, its
    reasoning saying three times over that it would restate each file in
    full inside a search block. A whole file there costs that file twice.

    The whole-file arm still says the sentence, because under that
    envelope it is the correct instruction and the economy would be
    wrong; that half is the known-good.
    """
    node = Node.model_validate(_node_dict())
    contents = {"n.py": "def f():\n    return 3\n"}
    failure = "Attempt 1 of 3 failed 1 gate(s):\n- tests: exited 1\n"
    whole = build_repair_prompt(
        task=TASK,
        node=node,
        files=["n.py"],
        contents=contents,
        failure=failure,
        plan=None,
        emission="whole-file",
    )
    edit = build_repair_prompt(
        task=TASK,
        node=node,
        files=["n.py"],
        contents=contents,
        failure=failure,
        plan=None,
        emission="edit",
    )
    assert "write the files above as they should now be" in whole
    assert "write the files above as they should now be" not in edit
    assert "Do not restate a whole file" in edit
    assert "costs that file TWICE" in edit
    # Both still say what "fix forward" means, and both still carry the
    # failure they are repairing -- only the economy differs.
    assert "Fix forward" in whole
    assert "Fix forward" in edit
    assert failure in whole
    assert failure in edit


def test_build_replan_task_names_scope_and_history() -> None:
    node = Node.model_validate(_node_dict())
    text = build_replan_task(task=TASK, node=node, history="Node 'n1' failed.\n")
    assert f"Original task: {TASK}" in text
    assert "Node n1 failed and must be re-planned" in text
    assert "Requirements to cover: REQ-001" in text
    assert "Gate command the new nodes must satisfy: pytest test_n.py" in text
    assert "Failure history (do not repeat it):\nNode 'n1' failed.\n" in text


def test_build_replan_task_joins_multiple_requirements() -> None:
    data = _node_dict()
    data["requirements"] = [
        {"id": "REQ-001", "statement": "REQ-001 holds.", "accepts": ["2"], "rejects": ["3"]},
        {"id": "REQ-002", "statement": "REQ-002 holds.", "accepts": ["2"], "rejects": ["3"]},
    ]
    node = Node.model_validate(data)
    text = build_replan_task(task=TASK, node=node, history="x\n")
    assert "Requirements to cover: REQ-001, REQ-002" in text


def test_build_recovery_plan_prompt_asks_for_diagnosis() -> None:
    node = Node.model_validate(_node_dict())
    prompt = build_recovery_plan_prompt(
        task=TASK,
        node=node,
        files=["n.py"],
        contents={"n.py": "def f():\n    return 3\n"},
        failure="Attempt 1 of 3 failed 1 gate(s):\n",
    )
    assert TASK in prompt
    assert "Diagnose the root cause" in prompt
    assert "numbered plan, no diff" in prompt
    assert (
        "against the CURRENT tree state above, then outline the minimal fix steps. "
        "Write a short numbered plan" in prompt
    )
    assert "no commentary outside the plan.\n\nAttempt 1 of 3" in prompt
    assert "Attempt 1 of 3 failed 1 gate(s):" in prompt


def _run(
    options: RunOptions,
    client: VllmClient,
    stdin_text: str = "",
) -> tuple[int, str]:
    stdout = io.StringIO()
    code = run_task(options, client, stdin=io.StringIO(stdin_text), stdout=stdout)
    return code, stdout.getvalue()


def test_run_task_end_to_end_pass(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    seen: list[dict[str, Any]] = []
    script = [_emit_response({"nodes": [_node_dict()]}), _diff_response()]
    client = _scripted_client(script, seen)
    code, out = _run(_options(tmp_path), client)
    assert code == 0
    assert "- Verdict: PASS\n" in out
    assert "Plan: 1 node(s): n1\n" in out
    assert len(read_records(tmp_path / "proofs.jsonl")) == 1
    assert [s.name for s in read_spans(tmp_path / "proofs.jsonl") if s.kind == "agent"] == [
        "worker:n1",
        "run",
    ]
    # One planner call, then k concurrent worker draws, all at the node's effort.
    assert [call["reasoning_effort"] for call in seen] == ["medium", *["low"] * PROPOSAL_SAMPLES]
    assert seen[0]["max_tokens"] == 8192
    assert seen[0]["temperature"] == 0.0
    assert seen[1]["temperature"] == 0.7
    assert f"- Task: {TASK}\n" in out
    assert TASK in _prompt(seen[1])
    assert "--- n.py ---\ndef f():\n    return 1\n" in _prompt(seen[1])
    assert "--- README.md ---" not in _prompt(seen[1])


def test_run_task_honors_sampling_options(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    seen: list[dict[str, Any]] = []
    script = [_emit_response({"nodes": [_node_dict()]}), _diff_response()]
    client = _scripted_client(script, seen)
    options = _options(
        tmp_path, max_tokens=100, temperature=0.5, sample_temperature=0.9, reasoning_effort="low"
    )
    code, _ = _run(options, client)
    assert code == 0
    assert seen[0]["max_tokens"] == 100
    assert seen[0]["temperature"] == 0.5
    assert seen[0]["reasoning_effort"] == "low"
    assert seen[1]["max_tokens"] == _expected_cap(seen[1])
    assert seen[1]["temperature"] == 0.9


def test_run_task_worker_output_budget_is_not_the_context_ceiling(tmp_path: Path) -> None:
    """Regression: spending the read ceiling as the output cap truncated work.

    `max_context_tokens` bounds what the worker reads; generation gets
    the rest of the window.
    """

    _git_repo(tmp_path)
    seen: list[dict[str, Any]] = []
    node = _node_dict()
    node["execution_constraints"]["max_context_tokens"] = 8000
    script = [_emit_response({"nodes": [node]}), _diff_response()]
    client = _scripted_client(script, seen)
    options = _options(tmp_path, max_tokens=100, temperature=0.5)
    code, _ = _run(options, client)
    assert code == 0
    assert seen[0]["max_tokens"] == 100
    assert seen[1]["max_tokens"] == _expected_cap(seen[1])
    assert seen[1]["max_tokens"] > 100_000
    assert seen[1]["reasoning_effort"] == "low"


def test_run_task_worker_effort_overrides_node_budget(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    seen: list[dict[str, Any]] = []
    script = [_emit_response({"nodes": [_node_dict()]}), _diff_response()]
    client = _scripted_client(script, seen)
    options = _options(tmp_path, worker_effort="xhigh")
    code, _ = _run(options, client)
    assert code == 0
    # One planner call, then k concurrent worker draws, all at the node's effort.
    assert [call["reasoning_effort"] for call in seen] == ["medium", *["xhigh"] * PROPOSAL_SAMPLES]


def test_run_task_transport_error_surfaces(tmp_path: Path) -> None:
    _git_repo(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        msg = "down"
        raise httpx.ConnectError(msg)

    client = VllmClient(api_key="k", transport=httpx.MockTransport(handler))
    code, out = _run(_options(tmp_path), client)
    assert code == 1
    assert out == "error: request failed: down\n"


def test_run_task_confirms_before_running(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    seen: list[dict[str, Any]] = []
    script = [_emit_response({"nodes": [_node_dict()]}), _diff_response()]
    client = _scripted_client(script, seen)
    code, out = _run(_options(tmp_path, yes=False), client, stdin_text="y\n")
    assert code == 0
    assert "Run 1 node(s)? [y/N]" in out


def test_run_task_decline_aborts_without_running(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    seen: list[dict[str, Any]] = []
    script = [_emit_response({"nodes": [_node_dict()]}) for _ in range(2)]
    client = _scripted_client(script, seen)
    for stdin_text in ("n\n", "\n"):
        code, out = _run(_options(tmp_path, yes=False), client, stdin_text=stdin_text)
        assert code == 1
        assert out == "Plan: 1 node(s): n1\nRun 1 node(s)? [y/N] aborted.\n"
    assert len(seen) == 2
    assert not (tmp_path / "proofs.jsonl").exists()


def test_run_task_plan_lists_all_nodes(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    seen: list[dict[str, Any]] = []
    dag = {"nodes": [_node_dict("n1"), _node_dict("n2")]}
    client = _scripted_client([_emit_response(dag)], seen)
    code, out = _run(_options(tmp_path, yes=False), client, stdin_text="n\n")
    assert code == 1
    assert out == "Plan: 2 node(s): n1, n2\nRun 2 node(s)? [y/N] aborted.\n"


def _prompt(call: dict[str, Any]) -> str:
    message = call["messages"][0]
    content = message["content"]
    assert isinstance(content, str)
    return content


def test_run_task_retries_invalid_emissions(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    seen: list[dict[str, Any]] = []
    script = [
        httpx.Response(200, json={"choices": [{"message": {"content": "not json"}}]}),
        _emit_response({"nodes": []}),
        _emit_response({"nodes": [_node_dict()]}),
        _diff_response(),
    ]
    client = _scripted_client(script, seen)
    code, out = _run(_options(tmp_path), client)
    assert code == 0
    assert "- Verdict: PASS\n" in out
    assert _prompt(seen[0]) == build_emit_prompt(TASK, git_ls_files(tmp_path))
    assert TASK in _prompt(seen[1])
    assert TASK in _prompt(seen[2])
    assert "Previous attempt failed:\ncontent is not valid JSON" in _prompt(seen[1])
    assert "content is not valid JSON" in _prompt(seen[2])
    assert "at least 1 item" in _prompt(seen[2])
    assert "(char 0)\n1 validation error for Dag" in _prompt(seen[2])


def test_run_task_redraws_a_plan_whose_requirement_restates_the_gate(tmp_path: Path) -> None:
    """The restated-gate rule, the wiring. A predicate nothing calls protects
    nothing, so this pins the call site rather than the rule:
    the offending plan is refused, the reason reaches the planner as a
    validation error, and the redraw is what runs. The statement is round
    3g's `n2.r1` REQ-002, frozen."""
    _git_repo(tmp_path)
    seen: list[dict[str, Any]] = []
    statement = (
        (Path(__file__).parent / "fixtures" / "requirement_restates_gate_round3g.txt")
        .read_text()
        .strip()
    )
    bad = _node_dict()
    bad["requirements"] = [
        {"id": "REQ-001", "statement": statement, "accepts": ["2"], "rejects": ["3"]}
    ]
    client = _scripted_client(
        [
            _emit_response({"nodes": [bad]}),
            _emit_response({"nodes": [_node_dict()]}),
            _diff_response(),
        ],
        seen,
    )
    code, out = _run(_options(tmp_path), client)
    assert code == 0, out
    redraw = _prompt(seen[1])
    assert "plan-restates-gate: a node description or requirement statement" in redraw
    assert "every changed line executed by" in redraw


def test_emit_valid_dag_redraws_a_subplan_that_takes_a_pending_node_s_file(
    tmp_path: Path,
) -> None:
    """The reserved-files rule, the wiring. A predicate nothing calls protects nothing.

    Round 3i's shape: the subplan replacing `n1` declares `accounts.py`,
    which pending `n2` still owes. The first emission is refused, the
    reason reaches the planner as a validation error naming the file,
    and the redraw is what comes back.
    """
    _git_repo(tmp_path)
    seen: list[dict[str, Any]] = []
    takes = {**_node_dict("n1.r2"), "target_files": ["accounts.py"]}
    clear = {**_node_dict("n1.r2"), "target_files": ["n.py"]}
    client = _scripted_client(
        [_emit_response({"nodes": [takes]}), _emit_response({"nodes": [clear]})],
        seen,
    )
    dag = _emit_valid_dag(
        client,
        "Replan n1.",
        files=["n.py", "accounts.py"],
        file_lines={"n.py": 4, "accounts.py": 4},
        max_tokens=2048,
        temperature=0.0,
        reasoning_effort="low",
        reserved=["accounts.py", "fees.py"],
    )
    assert [node.target_files for node in dag.nodes] == [["n.py"]]
    redraw = _prompt(seen[1])
    assert "plan-retargets-reserved: a replacement node takes a file another pending" in redraw
    assert "node 'n1.r2' declares accounts.py" in redraw
    assert "Scope the subplan to the failed node." in redraw
    # Only the clash is named: `fees.py` is reserved but untouched here,
    # and naming it would send the planner after the wrong file.
    assert "fees.py" not in redraw.split("plan-retargets-reserved")[1]


def test_emit_valid_dag_reserves_nothing_for_the_first_plan(tmp_path: Path) -> None:
    """Known-good: the initial emission passes `()`, so a plan may declare
    any file. If the check fired here no run could ever start."""
    _git_repo(tmp_path)
    seen: list[dict[str, Any]] = []
    client = _scripted_client([_emit_response({"nodes": [_node_dict()]})], seen)
    dag = _emit_valid_dag(
        client,
        "Plan it.",
        files=["n.py"],
        file_lines={"n.py": 4},
        max_tokens=2048,
        temperature=0.0,
        reasoning_effort="low",
    )
    assert [node.id for node in dag.nodes] == ["n1"]
    assert len(seen) == 1


def test_emit_prompt_states_the_coverage_field_without_restating_it() -> None:
    """The restated-gate rule's instruction half. The bullet that taught the proxy said
    "every line you change must be executed by a test"; the planner wrote
    that back into two strings the worker reads as binding (round 3g).
    A prompt change alone would be decorative, so this is the half the
    check above enforces -- but the sentence must still be gone."""
    prompt = build_emit_prompt(TASK, ("n.py",))
    assert "every line you change must be executed" not in prompt
    assert "changed_line_coverage_min is always 100.0" in prompt
    assert "requirement statement may restate it" in prompt
    assert "rejected and redrawn" in prompt
    assert "describes behaviour a test can falsify" in prompt


def test_run_task_exhausted_emission_fails(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    seen: list[dict[str, Any]] = []
    bad_node = _node_dict()
    bad_node["dependencies"] = ["nope"]
    script = [_emit_response({"nodes": [bad_node]}) for _ in range(3)]
    client = _scripted_client(script, seen)
    code, out = _run(_options(tmp_path), client)
    assert code == 1
    single = "unknown-dependency: node 'n1' depends on unknown node 'nope'"
    assert out == f"error: could not emit a valid DAG in 3 rounds: {single}; {single}; {single}\n"
    assert len(seen) == 3


def test_run_task_dirty_repo_fails_fast(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    (tmp_path / "n.py").write_text("def f():\n    return 9\n")
    seen: list[dict[str, Any]] = []
    client = _scripted_client([], seen)
    code, out = _run(_options(tmp_path), client)
    assert code == 1
    assert out == f"error: {str(tmp_path)!r} has uncommitted changes; commit or stash first\n"
    assert seen == []


def test_run_task_inits_plain_directory(tmp_path: Path) -> None:
    (tmp_path / "n.py").write_text("def f():\n    return 1\n")
    seen: list[dict[str, Any]] = []
    script = [_emit_response({"nodes": [_node_dict()]})]
    client = _scripted_client(script, seen)
    code, out = _run(_options(tmp_path, yes=False), client, stdin_text="n\n")
    assert code == 1
    notice = f"created baseline commit in {str(tmp_path)!r}\n"
    assert out == notice + "Plan: 1 node(s): n1\nRun 1 node(s)? [y/N] aborted.\n"
    assert run_argv(["git", "rev-parse", "HEAD"], tmp_path) == 0
    assert run_argv(["git", "show", "HEAD:n.py"], tmp_path) == 0
    assert run_argv(["git", "diff-index", "--quiet", "HEAD", "--"], tmp_path) == 0
    log = subprocess.run(
        ["git", "log", "-1", "--format=%an%n%ae%n%s"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert log.stdout.splitlines() == ["saddle", "saddle@local", "saddle baseline"]


def test_run_task_inits_missing_nested_directory(tmp_path: Path) -> None:
    repo = tmp_path / "a" / "b"
    seen: list[dict[str, Any]] = []
    script = [_emit_response({"nodes": [_node_dict()]})]
    client = _scripted_client(script, seen)
    code, out = _run(_options(repo, yes=False), client, stdin_text="n\n")
    assert code == 1
    notice = f"created baseline commit in {str(repo)!r}\n"
    assert out == notice + "Plan: 1 node(s): n1\nRun 1 node(s)? [y/N] aborted.\n"
    assert run_argv(["git", "rev-parse", "HEAD"], repo) == 0


def test_run_task_baselines_repo_without_commits(tmp_path: Path) -> None:
    assert run_argv(["git", "init", "--quiet"], tmp_path) == 0
    (tmp_path / "n.py").write_text("def f():\n    return 1\n")
    seen: list[dict[str, Any]] = []
    script = [_emit_response({"nodes": [_node_dict()]})]
    client = _scripted_client(script, seen)
    code, out = _run(_options(tmp_path, yes=False), client, stdin_text="n\n")
    assert code == 1
    notice = f"created baseline commit in {str(tmp_path)!r}\n"
    assert out == notice + "Plan: 1 node(s): n1\nRun 1 node(s)? [y/N] aborted.\n"
    assert run_argv(["git", "show", "HEAD:n.py"], tmp_path) == 0


def test_run_task_repo_path_is_file_fails(tmp_path: Path) -> None:
    target = tmp_path / "n.py"
    target.write_text("x = 1\n")
    seen: list[dict[str, Any]] = []
    client = _scripted_client([], seen)
    code, out = _run(_options(target), client)
    assert code == 1
    assert out == f"error: {str(target)!r} is not a directory\n"
    assert seen == []


def test_run_task_broken_git_dir_fails(tmp_path: Path) -> None:
    (tmp_path / ".git").write_text("garbage\n")
    seen: list[dict[str, Any]] = []
    client = _scripted_client([], seen)
    code, out = _run(_options(tmp_path), client)
    assert code == 1
    assert out == f"error: could not stage baseline in {str(tmp_path)!r}\n"
    assert seen == []


def test_run_task_init_failure_reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_run_argv = run_argv

    def fake(argv: list[str], cwd: Path, **kwargs: Any) -> int:
        if "init" in argv:
            return 1
        return real_run_argv(argv, cwd, **kwargs)

    monkeypatch.setattr("saddle.cli.run_argv", fake)
    seen: list[dict[str, Any]] = []
    client = _scripted_client([], seen)
    code, out = _run(_options(tmp_path), client)
    assert code == 1
    assert out == f"error: could not init a git repo in {str(tmp_path)!r}\n"
    assert seen == []


def test_run_task_commit_failure_reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_run_argv = run_argv

    def fake(argv: list[str], cwd: Path, **kwargs: Any) -> int:
        if "commit" in argv:
            return 1
        return real_run_argv(argv, cwd, **kwargs)

    monkeypatch.setattr("saddle.cli.run_argv", fake)
    seen: list[dict[str, Any]] = []
    client = _scripted_client([], seen)
    code, out = _run(_options(tmp_path), client)
    assert code == 1
    assert out == f"error: could not commit baseline in {str(tmp_path)!r}\n"
    assert seen == []


def test_run_task_fail_verdict_returns_one(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    seen: list[dict[str, Any]] = []
    script = [
        _emit_response({"nodes": [_node_dict()]}),
        _diff_response("not a diff\n"),
        _text_response("1. Write a real diff.\n"),
        _diff_response("not a diff\n"),
        _emit_response({"nodes": []}),
        _emit_response({"nodes": []}),
        _emit_response({"nodes": []}),
    ]
    client = _scripted_client(script, seen)
    code, out = _run(_options(tmp_path), client)
    assert code == 1
    assert "- Verdict: FAIL\n" in out
    # Each gated attempt now costs PROPOSAL_SAMPLES diff calls instead of
    # one (#59), so count the calls by role rather than by a fixed total.
    diff_calls = [call for call in seen if _is_diff_request(call)]
    # The first attempt samples k times; each recovery attempt proposes once.
    assert len(diff_calls) >= PROPOSAL_SAMPLES


def test_run_task_retry_repairs_failing_tests(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    seen: list[dict[str, Any]] = []
    bad = DIFF.replace("+    return 2\n", "+    return 3\n")
    fix = whole_file("n.py", "def f():", "    return 2")
    script = [
        _emit_response({"nodes": [_node_dict()]}),
        _diff_response(bad),
        _text_response("1. Change the return value.\n"),
        _diff_response(fix),
    ]
    client = _scripted_client(script, seen)
    code, out = _run(_options(tmp_path, max_tokens=100, temperature=0.5), client)
    assert code == 0
    assert "- Attempts: 2\n" in out
    # Each gated attempt now costs PROPOSAL_SAMPLES diff calls instead of
    # one (#59), so count the calls by role rather than by a fixed total.
    diff_calls = [call for call in seen if _is_diff_request(call)]
    # The first attempt samples k times; each recovery attempt proposes once.
    assert len(diff_calls) >= PROPOSAL_SAMPLES
    recovery = next(call for call in seen if "Diagnose the root cause" in _prompt(call))
    assert recovery["reasoning_effort"] == "low"
    assert recovery["max_tokens"] == _expected_cap(recovery)
    assert recovery["temperature"] == 0.5
    assert "structured_outputs" not in recovery

    assert f"Task: {TASK}" in _prompt(seen[2])
    assert f"Task: {TASK}" in _prompt(seen[3])
    assert "The previous attempt failed" in _prompt(diff_calls[-1])
    assert "Fix forward" in _prompt(diff_calls[-1])
    assert "Attempt 1 of 3" in _prompt(diff_calls[-1])
    assert "1. Change the return value." in _prompt(diff_calls[-1])
    assert "The previous attempt failed" not in _prompt(seen[1])


def test_run_task_sample_temperature_routes_first_attempt_vs_recovery(
    tmp_path: Path,
) -> None:
    """flip: this test pinned the greedy recovery
    -- a retry diff at `--temperature`, 0.0 by default. Three seeds at 0.0
    were one byte-identical sample and truncated 3/3, so a retry after a
    degenerate attempt walked the same way. Retries now sample at
    `--sample-temperature` unless `--recovery-temperature` is given; the
    recovery-plan prose still runs at `--temperature`."""
    _git_repo(tmp_path)
    seen: list[dict[str, Any]] = []
    bad = DIFF.replace("+    return 2\n", "+    return 3\n")
    fix = whole_file("n.py", "def f():", "    return 2")
    script = [
        _emit_response({"nodes": [_node_dict()]}),
        _diff_response(bad),
        _text_response("1. Change the return value.\n"),
        _diff_response(fix),
    ]
    client = _scripted_client(script, seen)
    options = _options(tmp_path, temperature=0.3, sample_temperature=0.9)
    code, _out = _run(options, client)
    assert code == 0
    diff_calls = [call for call in seen if _is_diff_request(call)]
    first_attempt = [c for c in diff_calls if "The previous attempt failed" not in _prompt(c)]
    recovery = [c for c in diff_calls if "The previous attempt failed" in _prompt(c)]
    assert first_attempt
    assert recovery
    assert all(c["temperature"] == 0.9 for c in first_attempt)
    assert all(c["temperature"] == 0.9 for c in recovery)
    plan = next(c for c in seen if "Diagnose the root cause" in _prompt(c))
    assert plan["temperature"] == 0.3


def test_run_task_recovery_temperature_flag_sets_the_retry_temperature(tmp_path: Path) -> None:
    """Known-good: `--recovery-temperature 0.4` is what retry diff
    calls sample at; first attempts keep `--sample-temperature`. Known-bad:
    the old routing (retries at `--temperature`, 0.3 here) is gone."""
    _git_repo(tmp_path)
    seen: list[dict[str, Any]] = []
    bad = DIFF.replace("+    return 2\n", "+    return 3\n")
    fix = whole_file("n.py", "def f():", "    return 2")
    script = [
        _emit_response({"nodes": [_node_dict()]}),
        _diff_response(bad),
        _text_response("1. Change the return value.\n"),
        _diff_response(fix),
    ]
    options = _options(tmp_path, temperature=0.3, sample_temperature=0.9, recovery_temperature=0.4)
    code, _out = _run(options, _scripted_client(script, seen))
    assert code == 0
    diff_calls = [call for call in seen if _is_diff_request(call)]
    recovery = [c for c in diff_calls if "The previous attempt failed" in _prompt(c)]
    first_attempt = [c for c in diff_calls if c not in recovery]
    assert recovery
    assert all(c["temperature"] == 0.4 for c in recovery)
    assert all(c["temperature"] == 0.9 for c in first_attempt)
    assert not any(c["temperature"] == 0.3 for c in diff_calls)


def test_run_task_replan_recovers_exhausted_node(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    seen: list[dict[str, Any]] = []
    bad = DIFF.replace("+    return 2\n", "+    return 3\n")
    # The replacement starts from `n1`'s baseline (`return 1`), not from the
    # tree `n1`'s failed diff left behind, so it proposes the whole fix.
    script = [
        _emit_response({"nodes": [_node_dict()]}),
        _diff_response(bad),
        _text_response("1. Change the return value.\n"),
        _diff_response(bad),
        _emit_response({"nodes": [_node_dict("m1")]}),
        _diff_response(DIFF),
    ]
    client = _scripted_client(script, seen)
    code, out = _run(_options(tmp_path), client)
    assert code == 0
    # Each gated attempt now costs PROPOSAL_SAMPLES diff calls instead of
    # one (#59), so count the calls by role rather than by a fixed total.
    diff_calls = [call for call in seen if _is_diff_request(call)]
    # The first attempt samples k times; each recovery attempt proposes once.
    assert len(diff_calls) >= PROPOSAL_SAMPLES
    replan = next(call for call in seen if "must be re-planned" in _prompt(call))
    assert replan["max_tokens"] == 8192
    assert replan["temperature"] == 0.0
    assert "Failure history" in _prompt(replan)
    assert f"Original task: {TASK}" in _prompt(replan)
    assert "failed Tier-1 after" in _prompt(replan)
    assert "## Node n1\n" in out
    assert "## Node n1.r1\n" in out
    assert out.count("- Attempts: 2\n") == 1


def test_run_task_replan_is_refused_for_taking_a_pending_node_s_file(tmp_path: Path) -> None:
    """End to end: the reserved set the scheduler computes reaches the planner.

    The two predicates and the scheduler's call are each tested alone;
    this pins the one link between them -- `replan` forwarding `reserved`
    into `_emit_valid_dag`. Without it that argument is inert and round
    3i repeats: `n2` is pending and owes `README.md`, and every replan of
    `n1` here declares it, so each round is refused and the reason names
    the file.
    """
    _git_repo(tmp_path)
    seen: list[dict[str, Any]] = []
    bad = DIFF.replace("+    return 2\n", "+    return 3\n")
    pending = {**_node_dict("n2"), "dependencies": ["n1"], "target_files": ["README.md"]}
    takes = {**_node_dict("n1.r1"), "target_files": ["README.md"]}
    script = [
        _emit_response({"nodes": [_node_dict(), pending]}),
        _diff_response(bad),
        _text_response("1. Change the return value.\n"),
        _diff_response(bad),
        # One spare: the scripted client holds a recovery diff back for the
        # sampling fan-out, and the first replan round consumes it.
        *[_emit_response({"nodes": [takes]}) for _ in range(EMIT_ROUNDS + 1)],
    ]
    client = _scripted_client(script, seen)
    code, _out = _run(_options(tmp_path), client)
    assert code == 1
    replans = [_prompt(call) for call in seen if "must be re-planned" in _prompt(call)]
    assert len(replans) == EMIT_ROUNDS
    refusals = [text for text in replans if "plan-retargets-reserved" in text]
    assert refusals, "the reserved set never reached the planner"
    assert "node 'n1.r1' declares README.md" in refusals[-1]
    assert "Scope the subplan to the failed node." in refusals[-1]


def test_run_task_replan_emission_failure_keeps_verdict_fail(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    seen: list[dict[str, Any]] = []
    script = [
        _emit_response({"nodes": [_node_dict()]}),
        _diff_response("nope one\n"),
        _text_response("1. Write a real diff.\n"),
        _diff_response("nope two\n"),
        _text_response("1. Write a real diff.\n"),
        _diff_response("nope three\n"),
        _emit_response({"nodes": []}),
        _emit_response({"nodes": []}),
        _emit_response({"nodes": []}),
    ]
    client = _scripted_client(script, seen)
    code, out = _run(_options(tmp_path), client)
    assert code == 1
    assert "- Verdict: FAIL\n" in out
    # Each gated attempt now costs PROPOSAL_SAMPLES diff calls instead of
    # one (#59), so count the calls by role rather than by a fixed total.
    diff_calls = [call for call in seen if _is_diff_request(call)]
    # The first attempt samples k times; each recovery attempt proposes once.
    assert len(diff_calls) >= PROPOSAL_SAMPLES


def test_run_task_zero_budget_maps_to_none(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    seen: list[dict[str, Any]] = []
    script = [_emit_response({"nodes": [_node_dict(budget="zero")]}), _diff_response()]
    client = _scripted_client(script, seen)
    code, _ = _run(_options(tmp_path), client)
    assert code == 0
    # One planner call, then k concurrent worker draws, all at the node's effort.
    assert [call["reasoning_effort"] for call in seen] == ["medium", *["none"] * PROPOSAL_SAMPLES]


def test_run_task_stale_journal_fails(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    (tmp_path / "proofs.jsonl").write_text("{}\n")
    seen: list[dict[str, Any]] = []
    script = [_emit_response({"nodes": [_node_dict()]}), _diff_response()]
    client = _scripted_client(script, seen)
    code, out = _run(_options(tmp_path), client)
    assert code == 1
    assert "failed verification" in out


def test_run_task_auth_error_surfaces(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    seen: list[dict[str, Any]] = []
    script = [httpx.Response(401, json={"error": "no"})]
    client = _scripted_client(script, seen)
    code, out = _run(_options(tmp_path), client)
    assert code == 1
    assert "rejected the API key" in out


def test_check_server_returns_ids_when_model_served() -> None:
    client = _models_client({"data": [{"id": "m"}, {"id": "other"}]})
    assert check_server(client, base_url="http://x/v1", model="m") == ["m", "other"]


def test_check_server_maps_client_failures() -> None:
    cases: list[tuple[VllmClient, str]] = [
        (_unreachable_client(), "request failed: refused"),
        (
            _models_client({"error": "nope"}, status=401),
            "server rejected the API key (HTTP 401)",
        ),
        (_models_client("nope"), "models envelope must be an object"),
    ]
    for client, cause in cases:
        with pytest.raises(RunError) as exc_info:
            check_server(client, base_url="http://x/v1", model="m")
        assert str(exc_info.value) == f"preflight failed at http://x/v1: {cause}"


def test_check_server_rejects_unserved_model() -> None:
    client = _models_client({"data": [{"id": "a"}, {"id": "b"}]})
    with pytest.raises(RunError) as exc_info:
        check_server(client, base_url="http://x/v1", model="m")
    assert str(exc_info.value) == (
        "preflight failed at http://x/v1: model 'm' is not served (served: a, b)"
    )
    bare = _models_client({"data": []})
    with pytest.raises(RunError) as exc_info:
        check_server(bare, base_url="http://x/v1", model="m")
    assert str(exc_info.value) == (
        "preflight failed at http://x/v1: model 'm' is not served (served: (none))"
    )


def test_run_doctor_reports_ok() -> None:
    client = _models_client({"data": [{"id": "m"}, {"id": "n"}]})
    out = io.StringIO()
    assert run_doctor("http://x/v1", "m", client, stdout=out) == 0
    assert out.getvalue() == "OK: http://x/v1 serves m (models: m, n)\n"


def test_run_doctor_reports_failure() -> None:
    out = io.StringIO()
    assert run_doctor("http://x/v1", "m", _unreachable_client(), stdout=out) == 1
    assert out.getvalue() == ("error: preflight failed at http://x/v1: request failed: refused\n")


def test_render_dag_plan_lists_nodes_with_gates() -> None:
    first = _node_dict("n1", "low", kill_threshold=85.0)
    second = _node_dict("n2", "medium")
    second["dependencies"] = ["n1"]
    second["task_prompt"] = "Wire it up."
    second["requirements"] = [
        {"id": "REQ-001", "statement": "REQ-001 holds.", "accepts": ["2"], "rejects": ["3"]},
        {"id": "REQ-002", "statement": "REQ-002 holds.", "accepts": ["2"], "rejects": ["3"]},
    ]
    second["execution_constraints"]["allowed_tools"] = ["read_file", "write_file"]
    second["execution_constraints"]["max_context_tokens"] = 8000
    second["deterministic_gate"]["test_command"] = "pytest test_w.py"
    second["deterministic_gate"]["mutation_sample"]["kill_threshold"] = 90.0
    third = _node_dict("n3", "xhigh")
    third["dependencies"] = ["n1", "n2"]
    third["task_prompt"] = "Polish."
    third["execution_constraints"]["allowed_tools"] = ["lint"]
    third["execution_constraints"]["max_context_tokens"] = 12000
    third["deterministic_gate"]["test_command"] = "pytest test_p.py"
    third["deterministic_gate"]["mutation_sample"]["kill_threshold"] = 95.0
    dag = Dag.model_validate({"nodes": [first, second, third]})
    assert render_dag_plan("Do the thing.", dag) == (
        "Task: Do the thing.\n"
        "Plan: 3 node(s): n1, n2, n3\n"
        "├── n1 [budget: low, context: 8000 tokens]\n"
        "│   task: Fix f and test it.\n"
        "│   requirements: REQ-001\n"
        "│   depends on: (none)\n"
        "│   tools: read_file, write_file, run_tests, lint\n"
        "│   targets: n.py\n"
        "│   gate: pytest test_n.py (coverage >= 100.0%, red-phase required, "
        "mutation 100 @ 85.0% changed-lines)\n"
        "├── n2 [budget: medium, context: 8000 tokens]\n"
        "│   task: Wire it up.\n"
        "│   requirements: REQ-001, REQ-002\n"
        "│   depends on: n1\n"
        "│   tools: read_file, write_file\n"
        "│   targets: n.py\n"
        "│   gate: pytest test_w.py (coverage >= 100.0%, red-phase required, "
        "mutation 100 @ 90.0% changed-lines)\n"
        "└── n3 [budget: xhigh, context: 12000 tokens]\n"
        "    task: Polish.\n"
        "    requirements: REQ-001\n"
        "    depends on: n1, n2\n"
        "    tools: lint\n"
        "    targets: n.py\n"
        "    gate: pytest test_p.py (coverage >= 100.0%, red-phase required, "
        "mutation 100 @ 95.0% changed-lines)\n"
    )


def test_render_dag_plan_shows_target_files_only_when_declared() -> None:
    node = _node_dict("n1", "low", kill_threshold=85.0)
    node["target_files"] = ["n.py", "test_n.py"]
    dag = Dag.model_validate({"nodes": [node]})
    text = render_dag_plan("Do the thing.", dag)
    assert (
        "    tools: read_file, write_file, run_tests, lint\n"
        "    targets: n.py, test_n.py\n"
        "    gate: "
    ) in text
    bare = Dag.model_validate(
        {"nodes": [{**_node_dict("n1", "low", kill_threshold=85.0), "target_files": []}]}
    )
    assert "targets:" not in render_dag_plan("Do the thing.", bare)


def test_render_dag_plan_single_node() -> None:
    dag = Dag.model_validate({"nodes": [_node_dict("n1", "low", kill_threshold=85.0)]})
    assert render_dag_plan("Do it.", dag) == (
        "Task: Do it.\n"
        "Plan: 1 node(s): n1\n"
        "└── n1 [budget: low, context: 8000 tokens]\n"
        "    task: Fix f and test it.\n"
        "    requirements: REQ-001\n"
        "    depends on: (none)\n"
        "    tools: read_file, write_file, run_tests, lint\n"
        "    targets: n.py\n"
        "    gate: pytest test_n.py (coverage >= 100.0%, red-phase required, "
        "mutation 100 @ 85.0% changed-lines)\n"
    )


def _dag_options() -> DagOptions:
    return DagOptions(task=TASK, base_url="http://x/v1", model="m")


def test_run_dag_prints_plan() -> None:
    second = _node_dict("n2", "low", kill_threshold=85.0)
    second["dependencies"] = ["n1"]
    seen: list[httpx.Request] = []
    client = _dag_client(
        ["m"], {"nodes": [_node_dict("n1", "low", kill_threshold=85.0), second]}, seen
    )
    out = io.StringIO()
    assert run_dag(_dag_options(), client, stdout=out) == 0
    body = json.loads(seen[1].content)
    assert body["max_tokens"] == 8192
    assert body["temperature"] == 0.0
    assert body["reasoning_effort"] == "medium"
    assert body["messages"][0]["content"] == build_emit_prompt(TASK)
    assert out.getvalue() == (
        f"Task: {TASK}\n"
        "Plan: 2 node(s): n1, n2\n"
        "├── n1 [budget: low, context: 8000 tokens]\n"
        "│   task: Fix f and test it.\n"
        "│   requirements: REQ-001\n"
        "│   depends on: (none)\n"
        "│   tools: read_file, write_file, run_tests, lint\n"
        "│   targets: n.py\n"
        "│   gate: pytest test_n.py (coverage >= 100.0%, red-phase required, "
        "mutation 100 @ 85.0% changed-lines)\n"
        "└── n2 [budget: low, context: 8000 tokens]\n"
        "    task: Fix f and test it.\n"
        "    requirements: REQ-001\n"
        "    depends on: n1\n"
        "    tools: read_file, write_file, run_tests, lint\n"
        "    targets: n.py\n"
        "    gate: pytest test_n.py (coverage >= 100.0%, red-phase required, "
        "mutation 100 @ 85.0% changed-lines)\n"
    )


def test_run_dag_preflight_failure_reports() -> None:
    out = io.StringIO()
    assert run_dag(_dag_options(), _unreachable_client(), stdout=out) == 1
    assert out.getvalue() == ("error: preflight failed at http://x/v1: request failed: refused\n")


def test_run_dag_model_mismatch_reports() -> None:
    client = _dag_client(["other"], {"nodes": [_node_dict("n1", "low")]})
    out = io.StringIO()
    assert run_dag(_dag_options(), client, stdout=out) == 1
    assert out.getvalue() == (
        "error: preflight failed at http://x/v1: model 'm' is not served (served: other)\n"
    )


def test_run_dag_bad_emission_reports() -> None:
    bad = _node_dict("n1", "low")
    bad["dependencies"] = ["nope"]
    client = _dag_client(["m"], {"nodes": [bad]})
    out = io.StringIO()
    assert run_dag(_dag_options(), client, stdout=out) == 1
    single = "unknown-dependency: node 'n1' depends on unknown node 'nope'"
    assert out.getvalue() == (
        f"error: could not emit a valid DAG in 3 rounds: {single}; {single}; {single}\n"
    )


class _FakeClient:
    made: ClassVar[list[dict[str, Any]]] = []
    calls: ClassVar[list[dict[str, Any]]] = []

    def __init__(self, **kwargs: Any) -> None:
        _FakeClient.made.append(kwargs)
        self._model = str(kwargs.get("model", DEFAULT_MODEL))

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def emit_dag(self, *args: Any, **kwargs: Any) -> DagEmission:
        _FakeClient.calls.append({"emit": kwargs})
        return DagEmission(dag={"nodes": [_node_dict()]}, reasoning="plan", raw_content="{}")

    def propose_diff(self, *args: Any, **kwargs: Any) -> DiffProposal:
        _FakeClient.calls.append({"diff": kwargs})
        return DiffProposal(diff=DIFF, reasoning="work")

    def list_models(self) -> list[str]:
        return [self._model]

    def max_model_len(self) -> int | None:
        return None

    def server_version(self) -> str | None:
        return None


def _refusing_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SADDLE_VLLM_API_KEY", "k")

    def boom(self: _FakeClient) -> list[str]:
        msg = "server rejected the API key (HTTP 401)"
        raise VllmAuthError(msg)

    monkeypatch.setattr("saddle.cli.VllmClient", _FakeClient)
    monkeypatch.setattr(_FakeClient, "list_models", boom)


def test_main_run_preflight_failure_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _refusing_setup(monkeypatch)
    assert main(["run", "--repo", str(tmp_path), "--yes", TASK]) == 1
    assert capsys.readouterr().err == (
        f"error: preflight failed at {DEFAULT_BASE_URL}: server rejected the API key (HTTP 401)\n"
    )
    assert not (tmp_path / ".git").exists()
    assert not (tmp_path / ".saddle").exists()


def test_main_run_preflight_failure_uses_explicit_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refusing_setup(monkeypatch)
    err = io.StringIO()
    assert main(["run", "--repo", str(tmp_path), "--yes", TASK], stderr=err) == 1
    assert err.getvalue() == (
        f"error: preflight failed at {DEFAULT_BASE_URL}: server rejected the API key (HTTP 401)\n"
    )


def _no_key_message() -> str:
    """What `main` prints with no key anywhere, built from the module's own
    KEY_FILE so the conftest guard's path is not duplicated in five places."""
    from saddle import cli

    return f"error: no API key. Export SADDLE_VLLM_API_KEY, or put it in {cli.KEY_FILE}\n"


def test_main_run_missing_key_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("SADDLE_VLLM_API_KEY", raising=False)
    monkeypatch.delenv("VLLM_API_KEY", raising=False)
    assert main(["run", "--repo", str(tmp_path), "Do it."]) == 1
    assert capsys.readouterr().err == _no_key_message()


def test_main_run_missing_key_uses_explicit_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SADDLE_VLLM_API_KEY", raising=False)
    monkeypatch.delenv("VLLM_API_KEY", raising=False)
    err = io.StringIO()
    assert main(["run", "--repo", str(tmp_path), "Do it."], stderr=err) == 1
    assert err.getvalue() == _no_key_message()


def test_main_run_wires_options_and_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git_repo(tmp_path)
    _FakeClient.made.clear()
    _FakeClient.calls.clear()
    monkeypatch.setenv("SADDLE_VLLM_API_KEY", "k1")
    monkeypatch.setattr("saddle.cli.VllmClient", _FakeClient)
    out = io.StringIO()
    code = main(["run", "--repo", str(tmp_path), "--yes", TASK], stdout=out)
    assert code == 0
    assert "- Verdict: PASS\n" in out.getvalue()
    assert f"- Task: {TASK}\n" in out.getvalue()
    assert (tmp_path / ".saddle" / "proofs.jsonl").is_file()
    assert _FakeClient.made[0]["api_key"] == "k1"
    assert _FakeClient.made[0]["model"] == "qwen3.8-27b"
    assert _FakeClient.made[0]["base_url"] == DEFAULT_BASE_URL
    assert _FakeClient.calls[0]["emit"]["max_tokens"] == 8192
    assert _FakeClient.calls[0]["emit"]["temperature"] == 0.0
    assert _FakeClient.calls[0]["emit"]["reasoning_effort"] == "medium"


def test_main_run_passes_sampling_flags_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git_repo(tmp_path)
    _FakeClient.made.clear()
    _FakeClient.calls.clear()
    monkeypatch.setenv("SADDLE_VLLM_API_KEY", "k1")
    monkeypatch.setattr("saddle.cli.VllmClient", _FakeClient)
    out = io.StringIO()
    code = main(
        [
            "run",
            "--repo",
            str(tmp_path),
            "--max-tokens",
            "100",
            "--temperature",
            "0.5",
            "--reasoning-effort",
            "low",
            "--worker-effort",
            "xhigh",
            "--yes",
            TASK,
        ],
        stdout=out,
    )
    assert code == 0
    assert _FakeClient.calls[0]["emit"]["max_tokens"] == 100
    assert _FakeClient.calls[0]["emit"]["temperature"] == 0.5
    assert _FakeClient.calls[0]["emit"]["reasoning_effort"] == "low"
    assert _FakeClient.calls[1]["diff"]["reasoning_effort"] == "xhigh"


def test_main_run_confirms_through_explicit_streams(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git_repo(tmp_path)
    _FakeClient.made.clear()
    monkeypatch.setenv("SADDLE_VLLM_API_KEY", "k1")
    monkeypatch.setattr("saddle.cli.VllmClient", _FakeClient)
    out = io.StringIO()
    code = main(
        ["run", "--repo", str(tmp_path), TASK],
        stdin=io.StringIO("y\n"),
        stdout=out,
    )
    assert code == 0
    assert "- Verdict: PASS\n" in out.getvalue()


def test_main_run_falls_back_to_legacy_key_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git_repo(tmp_path)
    _FakeClient.made.clear()
    monkeypatch.delenv("SADDLE_VLLM_API_KEY", raising=False)
    monkeypatch.setenv("VLLM_API_KEY", "k2")
    monkeypatch.setattr("saddle.cli.VllmClient", _FakeClient)
    out = io.StringIO()
    code = main(
        ["run", "--repo", str(tmp_path), "--journal", str(tmp_path / "j.jsonl"), "--yes", TASK],
        stdout=out,
    )
    assert code == 0
    assert (tmp_path / "j.jsonl").is_file()
    assert _FakeClient.made[0]["api_key"] == "k2"


def test_main_run_rejects_bad_effort() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["run", "--reasoning-effort", "bogus", "Do it."])
    assert exc_info.value.code == 2


def test_the_edit_rules_worked_example_is_itself_a_payload_that_applies() -> None:
    """The example shown to the worker must be one saddle would accept.

    A real node emitted 194 search lines, every one prefixed "- " rather
    than "-", and nothing matched: the rules said "prefixed with -" and
    never showed it. The example is the fix, so the example is now the
    contract -- it is extracted from the rules, parsed by the real
    parser, and applied to the file the rules display right above it.

    That last step is what pins the defect. A prefix of "- " still
    parses; it simply yields a search block with a space the file does
    not have, so the apply is what refuses it, exactly as the live draw
    was refused.
    """
    # The prose half, pinned only by presence: it is an instruction to a
    # model, so nothing here can test what it achieves. The example below
    # is the half with a semantic check behind it.
    assert '"-def fee(amount):", never "- def fee(amount):"' in EDIT_RULES
    assert "Worked example." in EDIT_RULES, "the rules no longer show the worker an example"
    shown = dedent(EDIT_RULES.split("containing:\n\n")[1].split("\n\nto change")[0]) + "\n"
    start = EDIT_RULES.index("edit fees.py")
    example = EDIT_RULES[start : EDIT_RULES.index(">>>>>>>\n", start) + len(">>>>>>>\n")]

    (edit,) = parse_edits(example)
    assert edit.search == "    return amount * 0.03\n"
    assert edit.replace == "    return amount * RATE\n"

    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "fees.py").write_text(shown)
        apply_edits(root, example)
        assert (root / "fees.py").read_text() == shown.replace("0.03", "RATE")


def test_the_worker_prompt_states_the_envelope_it_will_be_graded_in() -> None:
    """The prompt and the grammar have to agree or the draw is wasted.

    Known-good: asking for edits puts the edit rules in front of the model
    and leaves the whole-file rules out. Known-bad: the default still says
    "COMPLETE NEW CONTENTS", so a run that does not ask for edits is not
    quietly switched.
    """
    node = Node.model_validate(_node_dict())
    files = ["a.py"]
    contents = {"a.py": "x = 1\n"}

    edits = build_worker_prompt(
        task=TASK, node=node, files=files, contents=contents, emission="edit"
    )
    assert "Produce EDITS to the files you change" in edits
    assert "COMPLETE NEW CONTENTS" not in edits
    assert '"edit <file>"' in edits

    whole = build_worker_prompt(task=TASK, node=node, files=files, contents=contents)
    assert "COMPLETE NEW CONTENTS" in whole
    assert "Produce EDITS to the files you change" not in whole


def test_run_parser_defaults_and_overrides() -> None:
    parser = build_parser()
    defaults = parser.parse_args(["run", "Do it."])
    assert vars(defaults) == {
        "command": "run",
        "task": "Do it.",
        "repo": ".",
        "journal": None,
        "base_url": DEFAULT_BASE_URL,
        "model": "qwen3.8-27b",
        "max_tokens": 8192,
        "context_window": None,
        "temperature": 0.0,
        "sample_temperature": 0.7,
        "recovery_temperature": None,
        "deadline": None,
        "reasoning_effort": "medium",
        "worker_effort": None,
        "survivor_effort": "low",
        "survivor_samples": SURVIVOR_SAMPLES,
        "emission": "whole-file",
        "yes": False,
        "rule_d": False,
        "rule_d_store": None,
        "rule_d_set_id": None,
        "rule_d_table": "fix8",
        "rule_d_book": None,
        "rule_d_census_budget": 50,
        "rule_d_verdict_budget": 5,
        "rule_d_retry": False,
    }
    full = parser.parse_args(
        [
            "run",
            "--repo",
            "/r",
            "--journal",
            "/r/j.jsonl",
            "--base-url",
            "http://x/v1/",
            "--model",
            "m",
            "--max-tokens",
            "100",
            "--temperature",
            "0.5",
            "--sample-temperature",
            "0.9",
            "--reasoning-effort",
            "low",
            "--worker-effort",
            "xhigh",
            "--emission",
            "edit",
            "--yes",
            "Do it.",
        ]
    )
    assert vars(full) == {
        "command": "run",
        "task": "Do it.",
        "repo": "/r",
        "journal": "/r/j.jsonl",
        "base_url": "http://x/v1/",
        "model": "m",
        "max_tokens": 100,
        "context_window": None,
        "temperature": 0.5,
        "sample_temperature": 0.9,
        "recovery_temperature": None,
        "deadline": None,
        "reasoning_effort": "low",
        "worker_effort": "xhigh",
        "survivor_effort": "low",
        "survivor_samples": SURVIVOR_SAMPLES,
        "emission": "edit",
        "yes": True,
        "rule_d": False,
        "rule_d_store": None,
        "rule_d_set_id": None,
        "rule_d_table": "fix8",
        "rule_d_book": None,
        "rule_d_census_budget": 50,
        "rule_d_verdict_budget": 5,
        "rule_d_retry": False,
    }


def test_run_options_sample_temperature_default() -> None:
    options = RunOptions(task=TASK, repo=Path("."), journal=Path("j.jsonl"))
    assert options.sample_temperature == 0.7


def test_run_help_pins_every_option(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit, match=r"^0$"):
        main(["run", "--help"])
    assert capsys.readouterr().out == (
        "usage: saddle run [-h] [--repo REPO] [--journal JOURNAL] [--base-url BASE_URL]\n"
        "                  [--model MODEL] [--max-tokens MAX_TOKENS]\n"
        "                  [--context-window CONTEXT_WINDOW]\n"
        "                  [--temperature TEMPERATURE]\n"
        "                  [--sample-temperature SAMPLE_TEMPERATURE]\n"
        "                  [--recovery-temperature RECOVERY_TEMPERATURE]\n"
        "                  [--emission {whole-file,edit}]\n"
        "                  [--reasoning-effort {none,low,medium,xhigh}]\n"
        "                  [--worker-effort {none,low,medium,xhigh}]\n"
        "                  [--deadline DEADLINE]\n"
        "                  [--survivor-effort {none,low,medium,xhigh}]\n"
        "                  [--survivor-samples SURVIVOR_SAMPLES] [--yes] [--rule-d]\n"
        "                  [--rule-d-store RULE_D_STORE]\n"
        "                  [--rule-d-set-id RULE_D_SET_ID]\n"
        "                  [--rule-d-table RULE_D_TABLE] [--rule-d-book RULE_D_BOOK]\n"
        "                  [--rule-d-census-budget RULE_D_CENSUS_BUDGET]\n"
        "                  [--rule-d-verdict-budget RULE_D_VERDICT_BUDGET]\n"
        "                  [--rule-d-retry]\n"
        "                  task\n"
        "\n"
        "positional arguments:\n"
        "  task                  Task description to decompose and execute.\n"
        "\n"
        "options:\n"
        "  -h, --help            show this help message and exit\n"
        "  --repo REPO           Directory to work in (repo created if missing).\n"
        "  --journal JOURNAL     Journal path (default: REPO/.saddle/proofs.jsonl).\n"
        "  --base-url BASE_URL   vLLM base URL.\n"
        "  --model MODEL         Model id.\n"
        "  --max-tokens MAX_TOKENS\n"
        "                        Emission max tokens.\n"
        "  --context-window CONTEXT_WINDOW\n"
        "                        Model context length in tokens (default: what the\n"
        "                        server reports, else 175000).\n"
        "  --temperature TEMPERATURE\n"
        "                        Sampling temperature.\n"
        "  --sample-temperature SAMPLE_TEMPERATURE\n"
        "                        Temperature for worker diff samples, first attempt and\n"
        "                        retries alike.\n"
        "  --recovery-temperature RECOVERY_TEMPERATURE\n"
        "                        Temperature for retry diff samples (default: --sample-\n"
        "                        temperature).\n"
        "  --emission {whole-file,edit}\n"
        "                        What the worker emits: 'whole-file' restates every\n"
        "                        file it touches; 'edit' names the sites it changes, so\n"
        "                        a change costs its own size.\n"
        "  --reasoning-effort {none,low,medium,xhigh}\n"
        "                        Emission reasoning effort.\n"
        "  --worker-effort {none,low,medium,xhigh}\n"
        "                        Worker effort override (default: per-node budget).\n"
        "  --deadline DEADLINE   Seconds after which no node or attempt starts; the run\n"
        "                        seals what it has (exit 3).\n"
        "  --survivor-effort {none,low,medium,xhigh}\n"
        "                        Effort for survivor-round test draws.\n"
        "  --survivor-samples SURVIVOR_SAMPLES\n"
        "                        Candidate test draws per survivor round.\n"
        "  --yes                 Skip the plan confirmation.\n"
        "  --rule-d              Judge each impl tree with rule D against a sealed\n"
        "                        reference store (default off).\n"
        "  --rule-d-store RULE_D_STORE\n"
        "                        Reference store directory (see saddle.refstore).\n"
        "  --rule-d-set-id RULE_D_SET_ID\n"
        "                        The store's sealed set id (sha256 of SHA256SUMS).\n"
        "  --rule-d-table RULE_D_TABLE\n"
        "                        Answers table whose inputs rule D checks.\n"
        "  --rule-d-book RULE_D_BOOK\n"
        "                        Answer book (JSON lines); default: an empty book.\n"
        "  --rule-d-census-budget RULE_D_CENSUS_BUDGET\n"
        "                        Plan-time split classes allowed before the run stops.\n"
        "  --rule-d-verdict-budget RULE_D_VERDICT_BUDGET\n"
        "                        Asks per tree allowed before the node halts.\n"
        "  --rule-d-retry        Retry a refused tree with its misses in the repair\n"
        "                        prompt (default: the node fails).\n"
    )


def test_doctor_help_pins_every_option(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit, match=r"^0$"):
        main(["doctor", "--help"])
    assert capsys.readouterr().out == (
        "usage: saddle doctor [-h] [--base-url BASE_URL] [--model MODEL]\n"
        "\n"
        "options:\n"
        "  -h, --help           show this help message and exit\n"
        "  --base-url BASE_URL  vLLM base URL.\n"
        "  --model MODEL        Model id.\n"
    )


def test_main_doctor_missing_key_reports(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("SADDLE_VLLM_API_KEY", raising=False)
    monkeypatch.delenv("VLLM_API_KEY", raising=False)
    assert main(["doctor"]) == 1
    assert capsys.readouterr().err == _no_key_message()


def test_main_doctor_passes_flags_through(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SADDLE_VLLM_API_KEY", "k")
    _FakeClient.made.clear()
    monkeypatch.setattr("saddle.cli.VllmClient", _FakeClient)
    assert main(["doctor", "--base-url", "http://x/v1", "--model", "m"]) == 0
    assert _FakeClient.made[0]["api_key"] == "k"
    assert _FakeClient.made[0]["base_url"] == "http://x/v1"
    assert _FakeClient.made[0]["model"] == "m"
    assert capsys.readouterr().out == "OK: http://x/v1 serves m (models: m)\n"


def test_dag_help_pins_every_option(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit, match=r"^0$"):
        main(["dag", "--help"])
    assert capsys.readouterr().out == (
        "usage: saddle dag [-h] [--repo REPO] [--base-url BASE_URL] [--model MODEL]\n"
        "                  [--max-tokens MAX_TOKENS] [--context-window CONTEXT_WINDOW]\n"
        "                  [--temperature TEMPERATURE]\n"
        "                  [--reasoning-effort {none,low,medium,xhigh}]\n"
        "                  task\n"
        "\n"
        "positional arguments:\n"
        "  task                  Task description to decompose into a plan.\n"
        "\n"
        "options:\n"
        "  -h, --help            show this help message and exit\n"
        "  --repo REPO           Repository whose files the planner is shown.\n"
        "  --base-url BASE_URL   vLLM base URL.\n"
        "  --model MODEL         Model id.\n"
        "  --max-tokens MAX_TOKENS\n"
        "                        Emission max tokens.\n"
        "  --context-window CONTEXT_WINDOW\n"
        "                        Model context length in tokens (default: what the\n"
        "                        server reports, else 175000).\n"
        "  --temperature TEMPERATURE\n"
        "                        Sampling temperature.\n"
        "  --reasoning-effort {none,low,medium,xhigh}\n"
        "                        Emission reasoning effort.\n"
    )


def test_main_dag_missing_key_reports(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("SADDLE_VLLM_API_KEY", raising=False)
    monkeypatch.delenv("VLLM_API_KEY", raising=False)
    assert main(["dag", "Do it."]) == 1
    assert capsys.readouterr().err == _no_key_message()


def test_main_dag_passes_flags_through(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SADDLE_VLLM_API_KEY", "k")
    _FakeClient.made.clear()
    _FakeClient.calls.clear()
    monkeypatch.setattr("saddle.cli.VllmClient", _FakeClient)
    argv = [
        "dag",
        "--base-url",
        "http://x/v1",
        "--model",
        "m",
        "--max-tokens",
        "100",
        "--temperature",
        "0.5",
        "--reasoning-effort",
        "low",
        "Do it.",
    ]
    assert main(argv) == 0
    assert _FakeClient.made[0]["api_key"] == "k"
    assert _FakeClient.made[0]["base_url"] == "http://x/v1"
    assert _FakeClient.made[0]["model"] == "m"
    assert _FakeClient.calls[0]["emit"]["max_tokens"] == 100
    assert _FakeClient.calls[0]["emit"]["temperature"] == 0.5
    assert _FakeClient.calls[0]["emit"]["reasoning_effort"] == "low"
    assert capsys.readouterr().out == (
        "Task: Do it.\n"
        "Plan: 1 node(s): n1\n"
        "└── n1 [budget: low, context: 8000 tokens]\n"
        "    task: Fix f and test it.\n"
        "    requirements: REQ-001\n"
        "    depends on: (none)\n"
        "    tools: read_file, write_file, run_tests, lint\n"
        "    targets: n.py\n"
        "    gate: pytest test_n.py (coverage >= 100.0%, red-phase required, "
        "mutation 100 @ 85.0% changed-lines)\n"
    )


def test_main_dag_uses_defaults(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SADDLE_VLLM_API_KEY", "k")
    _FakeClient.made.clear()
    _FakeClient.calls.clear()
    monkeypatch.setattr("saddle.cli.VllmClient", _FakeClient)
    assert main(["dag", "Do it."]) == 0
    assert _FakeClient.made[0]["base_url"] == DEFAULT_BASE_URL
    assert _FakeClient.made[0]["model"] == DEFAULT_MODEL
    assert _FakeClient.calls[0]["emit"]["max_tokens"] == 8192
    assert _FakeClient.calls[0]["emit"]["temperature"] == 0.0
    assert _FakeClient.calls[0]["emit"]["reasoning_effort"] == "medium"


def test_main_dag_preflight_failure_reports(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _refusing_setup(monkeypatch)
    argv = ["dag", "--base-url", "http://x/v1", "--model", "m", "Do it."]
    assert main(argv) == 1
    assert capsys.readouterr().out == (
        "error: preflight failed at http://x/v1: server rejected the API key (HTTP 401)\n"
    )


def test_main_doctor_uses_defaults(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SADDLE_VLLM_API_KEY", "k")
    _FakeClient.made.clear()
    monkeypatch.setattr("saddle.cli.VllmClient", _FakeClient)
    assert main(["doctor"]) == 0
    assert _FakeClient.made[0]["base_url"] == DEFAULT_BASE_URL
    assert _FakeClient.made[0]["model"] == DEFAULT_MODEL
    out = capsys.readouterr().out
    assert out == (f"OK: {DEFAULT_BASE_URL} serves {DEFAULT_MODEL} (models: {DEFAULT_MODEL})\n")


def _sealed_journal(path: Path) -> None:
    record = build_record(
        evidence_id="n1#1",
        node_id="n1",
        diff="diff",
        parent_proofs=[],
        gate_outputs=[GateOutput(name="tests", passed=True, detail="ok")],
        requirement_ids=["REQ-001"],
        thinking="",
    )
    append_record(path, record)
    append_span(
        path,
        build_span(node_id="n1", argv=["pytest"], duration_ms=3, exit_code=0, detail=""),
    )


def test_run_verify_reports_ok_with_rerendered_transcript(tmp_path: Path) -> None:
    journal = tmp_path / "proofs.jsonl"
    _sealed_journal(journal)
    out = io.StringIO()
    assert run_verify(journal, stdout=out) == 0
    body = out.getvalue()
    assert body.startswith(
        f"OK: {journal}: ledger verifies: 2 records (1 proof(s), 1 span(s), 0 plan(s)), "
        "no outcome span list (no autonomous run)\n\n"
    )
    assert "# Saddle slice transcript\n" in body
    assert "- Verdict: PASS\n" in body
    assert "## Node n1\n" in body
    assert f"- Path: {journal}\n" in body
    assert "- Timeline:\n" in body
    assert "  - tool pytest: exit 0 in 3ms: pytest\n" in body


def test_run_verify_missing_journal_is_fresh(tmp_path: Path) -> None:
    journal = tmp_path / "proofs.jsonl"
    out = io.StringIO()
    assert run_verify(journal, stdout=out) == 0
    assert out.getvalue().startswith(
        f"OK: {journal}: ledger verifies: 0 records (0 proof(s), 0 span(s), 0 plan(s)), "
        "no outcome span list (no autonomous run)\n\n"
    )
    assert "- Proven nodes: 0\n" in out.getvalue()


def test_run_verify_tampered_record_lists_issue(tmp_path: Path) -> None:
    journal = tmp_path / "proofs.jsonl"
    _sealed_journal(journal)
    journal.write_text(journal.read_text().replace('"n1"', '"n2"', 1))
    out = io.StringIO()
    assert run_verify(journal, stdout=out) == 1
    assert out.getvalue() == "bad-hash@line 1: record hash mismatch for node 'n2'\n"


def test_run_verify_orphan_span_lists_issue(tmp_path: Path) -> None:
    journal = tmp_path / "proofs.jsonl"
    _sealed_journal(journal)
    append_span(
        journal,
        build_span(
            node_id="n1",
            argv=["pytest"],
            duration_ms=3,
            exit_code=0,
            detail="",
            parent_id="nope",
            span_id="child",
        ),
    )
    out = io.StringIO()
    assert run_verify(journal, stdout=out) == 1
    assert out.getvalue() == "orphan-span@line 3: span 'child' cites unknown parent 'nope'\n"


def test_main_verify_needs_no_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    journal = tmp_path / "proofs.jsonl"
    _sealed_journal(journal)
    monkeypatch.delenv("SADDLE_VLLM_API_KEY", raising=False)
    monkeypatch.delenv("VLLM_API_KEY", raising=False)
    assert main(["verify", str(journal)]) == 0
    out = capsys.readouterr().out
    assert out.startswith(
        f"OK: {journal}: ledger verifies: 2 records (1 proof(s), 1 span(s), 0 plan(s)), "
        "no outcome span list (no autonomous run)\n\n"
    )


def test_main_verify_defaults_to_repo_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = tmp_path / ".saddle" / "proofs.jsonl"
    _sealed_journal(journal)
    monkeypatch.chdir(tmp_path)
    out = io.StringIO()
    assert main(["verify"], stdout=out) == 0
    assert out.getvalue().startswith(
        "OK: .saddle/proofs.jsonl: ledger verifies: 2 records (1 proof(s)"
    )


def test_verify_help_pins_every_option(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit, match=r"^0$"):
        main(["verify", "--help"])
    assert capsys.readouterr().out == (
        "usage: saddle verify [-h] [--anchor [REPO]] [journal]\n"
        "\n"
        "positional arguments:\n"
        "  journal          Journal path (default: .saddle/proofs.jsonl).\n"
        "\n"
        "options:\n"
        "  -h, --help       show this help message and exit\n"
        "  --anchor [REPO]  Also check each autonomous run's outcome against the\n"
        "                   Saddle-Outcome trailer on its branch in REPO (default: the\n"
        "                   checkout the ledger sits in; write the journal first).\n"
    )


def _tail_journal(path: Path) -> ProofRecord:
    run = build_span(
        node_id="",
        argv=[],
        duration_ms=3000,
        exit_code=0,
        detail="1 proven, 0 failed, 0 undispatched",
        kind="agent",
        name="run",
    )
    worker = build_span(
        node_id="n1",
        argv=[],
        duration_ms=30,
        exit_code=0,
        detail="",
        kind="agent",
        name="worker:n1",
        parent_id=run.span_id,
    )
    record = build_record(
        evidence_id="n1#1",
        node_id="n1",
        diff="diff",
        parent_proofs=[],
        gate_outputs=[GateOutput(name="tests", passed=True, detail="ok")],
        requirement_ids=["REQ-001"],
        thinking="Fix it.",
    )
    append_span(
        path, build_span(node_id="n1", argv=["pytest"], duration_ms=3, exit_code=0, detail="")
    )
    append_record(path, record)
    append_span(path, worker)
    append_span(path, run)
    return record


def _tail_expected(record: ProofRecord) -> str:
    return (
        "[n1] tool pytest: exit 0 in 3ms: pytest\n"
        f"[n1] sealed {record.record_hash[:8]} (1/1 gates passed)\n"
        "[n1] thought: Fix it.\n"
        "[n1] worker:n1: exit 0 in 30ms\n"
        "run: exit 0 in 3000ms: 1 proven, 0 failed, 0 undispatched\n"
    )


class _FlushCounter(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.flushes = 0

    def flush(self) -> None:
        super().flush()
        self.flushes += 1


def test_run_tail_streams_cold_journal_without_sleeping(tmp_path: Path) -> None:
    journal = tmp_path / "proofs.jsonl"
    record = _tail_journal(journal)
    out = _FlushCounter()
    sleeps: list[float] = []
    assert run_tail(journal, stdout=out, sleep=sleeps.append) == 0
    assert out.getvalue() == _tail_expected(record)
    assert sleeps == []
    assert out.flushes == 4


def test_run_tail_reads_a_cold_journal_with_two_complete_runs(tmp_path: Path) -> None:
    """A run-end span ends the tail only when it is the last
    entry read, not the first one seen -- a cold journal already holding
    two finished runs must render and exit past both, not stop at the
    first run's end span while the second run's lines sit unread below."""
    journal = tmp_path / "proofs.jsonl"
    first = _tail_journal(journal)
    second = _tail_journal(journal)
    out = io.StringIO()
    sleeps: list[float] = []
    assert run_tail(journal, stdout=out, sleep=sleeps.append) == 0
    assert out.getvalue() == _tail_expected(first) + _tail_expected(second)
    assert sleeps == []


def test_run_tail_keeps_following_past_a_finished_run(tmp_path: Path) -> None:
    """Known-bad: with the old first-is_run_end-wins condition,
    a finished run followed by a second run already in progress would stop
    at the first run's end span and never render the second run's lines."""
    journal = tmp_path / "proofs.jsonl"
    finished_run = build_span(
        node_id="",
        argv=[],
        duration_ms=1000,
        exit_code=0,
        detail="1 proven, 0 failed, 0 undispatched",
        kind="agent",
        name="run",
    )
    append_span(journal, finished_run)
    second_tool = build_span(node_id="n2", argv=["pytest"], duration_ms=5, exit_code=0, detail="")
    append_span(journal, second_tool)
    second_run_end = build_span(
        node_id="",
        argv=[],
        duration_ms=500,
        exit_code=0,
        detail="1 proven, 0 failed, 0 undispatched",
        kind="agent",
        name="run",
    )

    def sleep(secs: float) -> None:
        sleeps.append(secs)
        append_span(journal, second_run_end)

    sleeps: list[float] = []
    out = io.StringIO()
    assert run_tail(journal, stdout=out, sleep=sleep) == 0
    assert out.getvalue() == (
        "run: exit 0 in 1000ms: 1 proven, 0 failed, 0 undispatched\n"
        "[n2] tool pytest: exit 0 in 5ms: pytest\n"
        "run: exit 0 in 500ms: 1 proven, 0 failed, 0 undispatched\n"
    )
    assert sleeps == [0.2]


def test_run_tail_follows_growing_journal(tmp_path: Path) -> None:
    journal = tmp_path / "proofs.jsonl"
    append_span(
        journal,
        build_span(node_id="n1", argv=["pytest"], duration_ms=3, exit_code=0, detail=""),
    )
    record = build_record(
        evidence_id="n1#1",
        node_id="n1",
        diff="diff",
        parent_proofs=[],
        gate_outputs=[GateOutput(name="tests", passed=True, detail="ok")],
        requirement_ids=["REQ-001"],
        thinking="Fix it.",
    )
    run = build_span(
        node_id="",
        argv=[],
        duration_ms=3000,
        exit_code=0,
        detail="",
        kind="agent",
        name="run",
    )
    pending: list[ProofRecord | SpanRecord] = [record, run]

    def sleep(secs: float) -> None:
        sleeps.append(secs)
        entry = pending.pop(0)
        if isinstance(entry, ProofRecord):
            append_record(journal, entry)
        else:
            append_span(journal, entry)

    sleeps: list[float] = []
    out = io.StringIO()
    assert run_tail(journal, stdout=out, sleep=sleep) == 0
    assert out.getvalue() == (
        "[n1] tool pytest: exit 0 in 3ms: pytest\n"
        f"[n1] sealed {record.record_hash[:8]} (1/1 gates passed)\n"
        "[n1] thought: Fix it.\n"
        "run: exit 0 in 3000ms\n"
    )
    assert sleeps == [0.2, 0.2]


def test_run_tail_waits_for_missing_journal_once(tmp_path: Path) -> None:
    journal = tmp_path / "proofs.jsonl"
    record = build_record(
        evidence_id="n1#1",
        node_id="n1",
        diff="diff",
        parent_proofs=[],
        gate_outputs=[GateOutput(name="tests", passed=True, detail="ok")],
        requirement_ids=["REQ-001"],
        thinking="",
    )
    run = build_span(
        node_id="",
        argv=[],
        duration_ms=1,
        exit_code=0,
        detail="",
        kind="agent",
        name="run",
    )
    created = False

    def sleep(secs: float) -> None:
        nonlocal created
        if not created:
            created = True
            append_record(journal, record)
            append_span(journal, run)

    out = io.StringIO()
    assert run_tail(journal, stdout=out, sleep=sleep) == 0
    assert out.getvalue() == (
        f"waiting for {journal} to appear...\n"
        f"[n1] sealed {record.record_hash[:8]} (1/1 gates passed)\n"
        "run: exit 0 in 1ms\n"
    )


def test_run_tail_refuses_corruption(tmp_path: Path) -> None:
    journal = tmp_path / "proofs.jsonl"
    journal.write_text("garbage\n")
    out = io.StringIO()
    assert run_tail(journal, stdout=out, sleep=lambda secs: None) == 1
    assert out.getvalue().startswith(f"error: journal {str(journal)!r} failed verification: ")


def test_run_tail_refuses_truncation(tmp_path: Path) -> None:
    journal = tmp_path / "proofs.jsonl"
    first = _tail_journal(journal)
    lines = journal.read_text().splitlines()
    journal.write_text("\n".join(lines[:2]) + "\n")
    out = io.StringIO()
    truncated = False

    def sleep(secs: float) -> None:
        nonlocal truncated
        if not truncated:
            truncated = True
            journal.write_text(lines[0] + "\n")

    assert run_tail(journal, stdout=out, sleep=sleep) == 1
    assert out.getvalue() == (
        "[n1] tool pytest: exit 0 in 3ms: pytest\n"
        f"[n1] sealed {first.record_hash[:8]} (1/1 gates passed)\n"
        "[n1] thought: Fix it.\n"
        f"error: {journal} was truncated; restart tail\n"
    )


def test_run_tail_keyboard_interrupt_exits_130(tmp_path: Path) -> None:
    journal = tmp_path / "proofs.jsonl"
    append_span(
        journal,
        build_span(node_id="n1", argv=["pytest"], duration_ms=3, exit_code=0, detail=""),
    )

    def sleep(secs: float) -> None:
        raise KeyboardInterrupt

    out = io.StringIO()
    assert run_tail(journal, stdout=out, sleep=sleep) == 130
    assert out.getvalue() == "[n1] tool pytest: exit 0 in 3ms: pytest\n"


def test_main_tail_needs_no_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    journal = tmp_path / "proofs.jsonl"
    record = _tail_journal(journal)
    monkeypatch.delenv("SADDLE_VLLM_API_KEY", raising=False)
    monkeypatch.delenv("VLLM_API_KEY", raising=False)
    assert main(["tail", str(journal)]) == 0
    assert capsys.readouterr().out == _tail_expected(record)


def test_main_tail_defaults_to_repo_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = tmp_path / ".saddle" / "proofs.jsonl"
    record = _tail_journal(journal)
    monkeypatch.chdir(tmp_path)
    out = io.StringIO()
    assert main(["tail"], stdout=out) == 0
    assert out.getvalue() == _tail_expected(record)


def test_tail_help_pins_every_option(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit, match=r"^0$"):
        main(["tail", "--help"])
    assert capsys.readouterr().out == (
        "usage: saddle tail [-h] [journal]\n"
        "\n"
        "positional arguments:\n"
        "  journal     Journal path (default: .saddle/proofs.jsonl).\n"
        "\n"
        "options:\n"
        "  -h, --help  show this help message and exit\n"
    )


def test_up_help_pins_every_option(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit, match=r"^0$"):
        main(["up", "--help"])
    assert capsys.readouterr().out == (
        "usage: saddle up [-h] [--workdir WORKDIR] [--journal JOURNAL]\n"
        "                 [--base-url BASE_URL] [--model MODEL]\n"
        "                 [--max-tokens MAX_TOKENS] [--temperature TEMPERATURE]\n"
        "                 [--reasoning-effort {none,low,medium,xhigh}]\n"
        "                 [--mode {ask,edit}]\n"
        "\n"
        "options:\n"
        "  -h, --help            show this help message and exit\n"
        "  --workdir WORKDIR     Directory tools run in (default: .).\n"
        "  --journal JOURNAL     Journal path (default: .saddle/chat.jsonl).\n"
        "  --base-url BASE_URL   vLLM base URL.\n"
        "  --model MODEL         Model id.\n"
        "  --max-tokens MAX_TOKENS\n"
        "                        Reply max tokens.\n"
        "  --temperature TEMPERATURE\n"
        "                        Sampling temperature.\n"
        "  --reasoning-effort {none,low,medium,xhigh}\n"
        "                        Reply reasoning effort.\n"
        "  --mode {ask,edit}     ask (default): read-only tools, as in the web chat's\n"
        "                        Ask lane. edit: may write files and run commands in\n"
        "                        --workdir, unaudited.\n"
    )


def test_up_parser_defaults_and_overrides() -> None:
    parser = build_parser()
    defaults = parser.parse_args(["up"])
    assert vars(defaults) == {
        "command": "up",
        "workdir": ".",
        "journal": ".saddle/chat.jsonl",
        "base_url": DEFAULT_BASE_URL,
        "model": "qwen3.8-27b",
        "max_tokens": 8192,
        "temperature": 0.0,
        "reasoning_effort": "medium",
        "mode": "ask",
    }
    full = parser.parse_args(
        [
            "up",
            "--workdir",
            "/w",
            "--journal",
            "/w/chat.jsonl",
            "--base-url",
            "http://x/v1",
            "--model",
            "m",
            "--max-tokens",
            "100",
            "--temperature",
            "0.5",
            "--reasoning-effort",
            "low",
            "--mode",
            "edit",
        ]
    )
    assert vars(full) == {
        "command": "up",
        "workdir": "/w",
        "journal": "/w/chat.jsonl",
        "base_url": "http://x/v1",
        "model": "m",
        "max_tokens": 100,
        "temperature": 0.5,
        "reasoning_effort": "low",
        "mode": "edit",
    }


def test_main_up_missing_key_reports(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("SADDLE_VLLM_API_KEY", raising=False)
    monkeypatch.delenv("VLLM_API_KEY", raising=False)
    assert main(["up"]) == 1
    assert capsys.readouterr().err == _no_key_message()


def test_main_up_preflight_failure_reports(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _refusing_setup(monkeypatch)
    assert main(["up"]) == 1
    assert capsys.readouterr().err == (
        f"error: preflight failed at {DEFAULT_BASE_URL}: server rejected the API key (HTTP 401)\n"
    )


def test_main_up_preflight_failure_uses_explicit_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _refusing_setup(monkeypatch)
    err = io.StringIO()
    assert main(["up"], stderr=err) == 1
    assert err.getvalue() == (
        f"error: preflight failed at {DEFAULT_BASE_URL}: server rejected the API key (HTTP 401)\n"
    )


def _chat_recorder(seen: list[dict[str, Any]]) -> Any:
    def record(options: Any, client: Any, *, stdin: Any, console: Any) -> int:
        seen.append({"options": options, "client": client, "stdin": stdin, "console": console})
        return 0

    return record


def test_main_up_wires_options_and_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _FakeClient.made.clear()
    seen: list[dict[str, Any]] = []
    monkeypatch.setenv("SADDLE_VLLM_API_KEY", "k1")
    monkeypatch.setattr("saddle.cli.VllmClient", _FakeClient)
    monkeypatch.setattr("saddle.cli.run_chat", _chat_recorder(seen))
    out = io.StringIO()
    stdin = io.StringIO("/quit\n")
    code = main(["up", "--workdir", str(tmp_path)], stdin=stdin, stdout=out)
    assert code == 0
    assert _FakeClient.made[0]["api_key"] == "k1"
    assert _FakeClient.made[0]["base_url"] == DEFAULT_BASE_URL
    assert _FakeClient.made[0]["model"] == DEFAULT_MODEL
    assert len(seen) == 1
    options = seen[0]["options"]
    assert options.workdir == tmp_path
    assert options.journal == Path(".saddle/chat.jsonl")
    assert options.max_tokens == 8192
    assert options.temperature == 0.0
    assert options.reasoning_effort == "medium"
    assert options.mode == "ask"
    assert seen[0]["stdin"] is stdin
    assert seen[0]["console"].file is out
    assert isinstance(seen[0]["client"], _FakeClient)


def test_main_up_passes_flags_through(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeClient.made.clear()
    seen: list[dict[str, Any]] = []
    monkeypatch.setenv("SADDLE_VLLM_API_KEY", "k1")
    monkeypatch.setattr("saddle.cli.VllmClient", _FakeClient)
    monkeypatch.setattr("saddle.cli.run_chat", _chat_recorder(seen))
    out = io.StringIO()
    code = main(
        [
            "up",
            "--workdir",
            str(tmp_path),
            "--journal",
            str(tmp_path / "chat.jsonl"),
            "--base-url",
            "http://x/v1",
            "--model",
            "m",
            "--max-tokens",
            "100",
            "--temperature",
            "0.5",
            "--reasoning-effort",
            "low",
            "--mode",
            "edit",
        ],
        stdin=io.StringIO("/quit\n"),
        stdout=out,
    )
    assert code == 0
    assert _FakeClient.made[0]["base_url"] == "http://x/v1"
    assert _FakeClient.made[0]["model"] == "m"
    options = seen[0]["options"]
    assert options.workdir == tmp_path
    assert options.journal == tmp_path / "chat.jsonl"
    assert options.max_tokens == 100
    assert options.temperature == 0.5
    assert options.reasoning_effort == "low"
    assert options.mode == "edit"


def test_main_up_uses_default_streams(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeClient.made.clear()
    seen: list[dict[str, Any]] = []
    monkeypatch.setenv("SADDLE_VLLM_API_KEY", "k1")
    monkeypatch.setattr("saddle.cli.VllmClient", _FakeClient)
    monkeypatch.setattr("saddle.cli.run_chat", _chat_recorder(seen))
    assert main(["up"]) == 0
    assert seen[0]["stdin"] is sys.stdin
    assert seen[0]["console"].file is sys.stdout


def test_emit_prompt_does_not_push_the_planner_toward_maximum_context() -> None:
    """Context is a cost, not a budget to spend.

    The prompt used to say "use the full 30000 unless the node touches one
    small file". Attention follows a U-shaped curve: accuracy drops >30%
    when the relevant content sits mid-context rather than at an edge,
    and coding agents maximise the effect (accumulative context, high
    distractor density, long horizons). Padding a node's read budget
    buries the file it actually has to change.
    """
    prompt = build_emit_prompt("Do the thing.")
    assert "use the full 30000" not in prompt
    assert "smallest" in prompt
    # #50 pinned max_mutants; the prompt must not still offer a range.
    assert "max_mutants is 1-1000" not in prompt


def test_worker_prompt_restates_the_node_task_at_the_tail() -> None:
    """The node's task must appear at both high-attention positions.

    File contents sit in the middle of the prompt, which is exactly the
    region the U-shaped curve says is attended to worst. The instruction
    that matters has to bracket it, not sit only in front of it.
    """
    node = Node.model_validate(_node_dict("n1", "low"))
    prompt = build_structured_prompt(
        task="Do the thing.",
        node=node,
        files=["n.py"],
        contents={"n.py": "x = 1\n" * 500},
    )
    head, _, tail = prompt.partition("File contents:")
    assert node.task_prompt in head
    assert node.task_prompt in tail
    assert tail.index(node.task_prompt) > tail.index("x = 1")


def test_worker_prompt_inlines_contents_for_a_node_with_read_file() -> None:
    """Known-good: all four names behaves exactly as it did before."""
    node = Node.model_validate(_node_dict("n1", "low"))
    prompt = build_worker_prompt(
        task="Do the thing.", node=node, files=["n.py"], contents={"n.py": "MARKER = 1\n"}
    )
    assert "--- n.py ---" in prompt
    assert "MARKER = 1" in prompt
    assert CONTENTS_WITHHELD not in prompt


def test_worker_prompt_withholds_contents_without_read_file() -> None:
    """Known-bad: `read_file` is what buys the file bodies.

    `allowed_tools` was validated against the global allowlist and then
    consumed by nothing, so a plan that omitted `read_file` still had the
    whole repo inlined -- the context-cost lever DESIGN-NOTES D15/D16
    argue for did not exist. The file *names* stay: omitting the tool is
    a budget choice, not blindness, and the node can still say which file
    it needs.
    """
    node = Node.model_validate(_node_dict("n1", "low", tools=["write_file", "run_tests"]))
    prompt = build_worker_prompt(
        task="Do the thing.", node=node, files=["n.py"], contents={"n.py": "MARKER = 1\n"}
    )
    assert "MARKER = 1" not in prompt
    assert "--- n.py ---" not in prompt
    assert CONTENTS_WITHHELD in prompt
    # The listing is not the contents, and it survives.
    assert "Repo files:\nn.py" in prompt


def test_run_allowlist_is_derived_from_the_bindings_registry() -> None:
    """A name is admissible because the harness honours it, not because
    someone typed it into a second list that can drift from the first."""
    assert RUN_ALLOWLIST == tuple(TOOL_BINDINGS)


def test_emit_prompt_states_what_each_tool_buys() -> None:
    """The planner cannot choose deliberately against effects it is not told.

    Rendered from the registry, so a binding cannot change without the
    prompt changing with it.
    """
    prompt = build_emit_prompt("Do the thing.")
    for name, effect in TOOL_BINDINGS.items():
        assert f"- {name}: {effect}." in prompt


def _prompt_for(tools: list[str]) -> str:
    return build_worker_prompt(
        task="T",
        node=Node.model_validate(_node_dict("n1", tools=tools)),
        files=["n.py"],
        contents={"n.py": "MARKER = 1\n"},
    )


def _read_file_changes_behaviour() -> bool:
    """With `read_file` the worker prompt carries file bodies; without it, not."""
    return "MARKER = 1" in _prompt_for(["read_file"]) and "MARKER = 1" not in _prompt_for(["lint"])


def _write_file_changes_behaviour() -> bool:
    """With `write_file` an added file clears node-scope; without it, not."""
    with_tool = check_node_scope("impl", ["n.py"], ["new.py"], may_create=True)
    without = check_node_scope("impl", ["n.py"], ["new.py"], may_create=False)
    return with_tool.passed is True and without.passed is False


def _captured(argv: tuple[str, ...], marker: str) -> list[CapturedRun]:
    return [CapturedRun(argv=argv, exit_code=1, stdout=marker, stderr="")]


def _failed_gate() -> Tier1Result:
    # A failed gate that maps to no tool: a failed tests or
    # ruff gate's output reaches the worker regardless, so the probes
    # below must measure the binding on a gate that passed.
    return Tier1Result(
        node_id="n1",
        passed=False,
        checks=(GateCheck(name="node-scope", passed=False, detail="impl node changed tests"),),
    )


def _repair_prompt(captured: list[CapturedRun], tools: list[str]) -> str:
    return format_attempt_failure(_failed_gate(), captured, attempt=1, max_attempts=3, tools=tools)


def _run_tests_changes_behaviour() -> bool:
    """With `run_tests` the repair prompt carries the suite output; without it, not."""
    captured = _captured(("coverage", "run", "-m", "pytest"), "SUITE-MARKER")
    return "SUITE-MARKER" in _repair_prompt(captured, ["run_tests"]) and (
        "SUITE-MARKER" not in _repair_prompt(captured, ["lint"])
    )


def _lint_changes_behaviour() -> bool:
    """With `lint` the repair prompt carries ruff's output; without it, not."""
    captured = _captured(("ruff", "check", "n.py"), "RUFF-MARKER")
    return "RUFF-MARKER" in _repair_prompt(captured, ["lint"]) and (
        "RUFF-MARKER" not in _repair_prompt(captured, ["run_tests"])
    )


_TOOL_BEHAVIOUR_PROBES: dict[str, Callable[[], bool]] = {
    "read_file": _read_file_changes_behaviour,
    "write_file": _write_file_changes_behaviour,
    "run_tests": _run_tests_changes_behaviour,
    "lint": _lint_changes_behaviour,
}


def test_every_registry_name_is_a_behaviour_the_harness_honours() -> None:
    """The allowlist's own guard: a name with no binding fails the suite.

    `RUN_ALLOWLIST` used to be four strings validated against emissions
    and consumed nowhere, so adding a fifth would have cost nothing and
    bought nothing. Each probe here shows the *difference* listing a name
    makes -- both halves, per CONTRIBUTING.md -- and the set comparison means a
    registry entry without a probe cannot be added quietly.
    """
    assert set(_TOOL_BEHAVIOUR_PROBES) == set(TOOL_BINDINGS)
    for name, probe in _TOOL_BEHAVIOUR_PROBES.items():
        assert probe() is True, f"{name}: listing it changed nothing the harness does"


def test_emit_prompt_asks_for_the_test_impl_split() -> None:
    """The scope gate rejects plans the planner would otherwise keep
    making, so the prompt has to describe the split it enforces (#57)."""
    prompt = build_emit_prompt("Do the thing.")
    assert '"test"' in prompt
    assert '"impl"' in prompt
    assert '"refactor"' in prompt
    assert "may not" in prompt
    # The impl node has to depend on the tests it must turn green.
    assert "depends on" in prompt.lower()


def test_worker_prompt_asks_test_nodes_for_a_property() -> None:
    """The property gate otherwise just rejects what the worker keeps
    writing: examples probe the cases already in mind (#58)."""
    node = Node.model_validate(_node_dict("n1", "low"))
    prompt = build_structured_prompt(
        task="Do the thing.", node=node, files=["n.py"], contents={"n.py": "x = 1\n"}
    )
    assert "@given" in prompt
    assert "from_regex" in prompt


def test_emit_prompt_states_the_example_rule_with_the_run_counter_example() -> None:
    """The planner hears the near-miss rule with the
    bar the validator enforces and T1's own `"user"` as the counter-example."""
    prompt = build_emit_prompt("Do the thing.")
    assert '- Each requirement also carries "accepts" and "rejects"' in prompt
    assert f"within {REQ_NEAR_MISS_K} edits of some accept" in prompt
    assert '"user" is 12 edits away' in prompt
    assert "the plan is invalid with it" in prompt
    assert 'A "test" node\'s tests assert on every accept and every reject' in prompt


def test_emit_prompt_says_a_test_node_is_expected_to_fail() -> None:
    """The planner is told a test node's suite must be red when it runs,
    or it keeps planning test nodes whose tests pass and specify nothing."""
    prompt = build_emit_prompt("Do the thing.")
    assert "expected to fail" in prompt
    assert "fails if they already pass" in prompt


def test_emit_prompt_lists_the_repository_and_the_existing_file_rule() -> None:
    """Known-good: the planner sees what it is planning for."""
    prompt = build_emit_prompt("Make f return twice x.", ["n.py", "tests/test_n.py"])
    assert "Repository files (tracked):\nn.py\ntests/test_n.py\n" in prompt
    assert "changes that file" in prompt
    assert "never\n  create a parallel copy" in prompt


def test_emit_prompt_caps_the_listing_like_the_worker_prompt() -> None:
    files = [f"m{index}.py" for index in range(MAX_FILES_IN_PROMPT + 50)]
    prompt = build_emit_prompt("Do the thing.", files)
    assert f"m{MAX_FILES_IN_PROMPT - 1}.py\n... and 50 more\n" in prompt
    assert f"m{MAX_FILES_IN_PROMPT}.py" not in prompt


def test_emit_prompt_without_files_says_so() -> None:
    """Known-bad: an empty listing is stated, and the rule still stands."""
    prompt = build_emit_prompt("Do the thing.")
    assert "Repository files (tracked):\n(no tracked files)\n" in prompt
    assert "changes that file" in prompt


def test_run_dag_shows_the_planner_the_repo_files(tmp_path: Path) -> None:
    """`saddle dag --repo` lists the repo's tracked files in the emit prompt;
    without a repo the prompt lists nothing (the options default)."""
    _git_repo(tmp_path)
    seen: list[httpx.Request] = []
    client = _dag_client(["m"], {"nodes": [_node_dict("n1", "low", kill_threshold=85.0)]}, seen)
    options = DagOptions(task=TASK, base_url="http://x/v1", model="m", repo=tmp_path)
    assert run_dag(options, client, stdout=io.StringIO()) == 0
    body = json.loads(seen[1].content)
    assert body["messages"][0]["content"] == build_emit_prompt(TASK, git_ls_files(tmp_path))
    assert (
        "Repository files (tracked):\nREADME.md\nn.py\ntest_n.py\n"
        in (body["messages"][0]["content"])
    )
    # Outside a repository the preview still runs and lists nothing.
    empty = DagOptions(task=TASK, base_url="http://x/v1", model="m", repo=tmp_path / "nowhere")
    seen.clear()
    client = _dag_client(["m"], {"nodes": [_node_dict("n1", "low", kill_threshold=85.0)]}, seen)
    assert run_dag(empty, client, stdout=io.StringIO()) == 0
    assert "(no tracked files)" in json.loads(seen[1].content)["messages"][0]["content"]


def _truncated_response() -> httpx.Response:
    body = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "diff --git a/n.py", "reasoning": ""},
                "finish_reason": "length",
            }
        ],
        "model": "m",
    }
    return httpx.Response(200, json=body)


def _sidecar(journal: Path, span: SpanRecord) -> dict[str, Any]:
    """The attempt sidecar a span seals, checked against its hash."""
    path = attempt_sidecar_path(journal, span.span_id)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == span.attempt_hash
    loaded: dict[str, Any] = json.loads(path.read_bytes())
    return loaded


def _no_content_response(reasoning: str) -> httpx.Response:
    """Seen in a probe run: HTTP 200, `finish_reason: "stop"`, `content: null`, all reasoning."""
    body = {
        "choices": [
            {
                "message": {"role": "assistant", "content": None, "reasoning": reasoning},
                "finish_reason": "stop",
            }
        ],
        "usage": {"completion_tokens": 12436, "completion_tokens_details": {"reasoning_tokens": 0}},
        "model": "m",
    }
    return httpx.Response(200, json=body)


def test_run_task_no_content_response_is_a_named_failure_with_its_reasoning_kept(
    tmp_path: Path,
) -> None:
    """Known-good: a worker response with no content
    fails the attempt with a message that says so, the run retries, and
    the attempt's sidecar holds the reasoning that ran out. Known-bad was
    the bare "message has no text content" with the think block discarded."""
    _git_repo(tmp_path)
    seen: list[dict[str, Any]] = []
    silent_first = PROPOSAL_SAMPLES + 1
    thinking = "3. Third-party imports\n4. Local/fir"

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.append(payload)
        if not _is_diff_request(payload):
            if "Decompose the mechanical coding task" in _prompt(payload):
                return _emit_response({"nodes": [_node_dict()]})
            return _text_response("Plan: emit the diff this time.")
        diffs = sum(1 for call in seen if _is_diff_request(call))
        return _no_content_response(thinking) if diffs <= silent_first else _diff_response()

    client = VllmClient(api_key="k", transport=httpx.MockTransport(handler))
    code, out = _run(_options(tmp_path), client)
    assert code == 0, out
    assert "- Attempts: 2\n" in out
    journal = tmp_path / "proofs.jsonl"
    agents = [s for s in read_spans(journal) if s.kind == "agent" and s.name == "worker:n1"]
    assert agents[0].exit_code == 1
    expected = f"message has no text content (finish_reason=stop, {len(thinking)} reasoning chars)"
    assert expected in agents[0].detail
    sidecar = _sidecar(journal, agents[0])
    assert sidecar["thinking"] == thinking
    assert sidecar["finish_reason"] == "stop"
    assert sidecar["partial_content_chars"] == 0
    assert sidecar["usage"]["completion_tokens"] == 12436
    recovery = next(c for c in seen if "Diagnose the root cause" in _prompt(c))
    assert expected in _prompt(recovery)


def test_run_task_deadline_seals_what_it_has_and_exits_3(tmp_path: Path) -> None:
    """Deadline wiring: a deadline that leaves no room starts no node; the run
    still writes its plan and run span, says `deadline:` in the span, and
    exits 3, not 0 or 1."""
    _git_repo(tmp_path)
    seen: list[dict[str, Any]] = []
    script = [_emit_response({"nodes": [_node_dict()]})]
    code, out = _run(_options(tmp_path, deadline_s=0.0), _scripted_client(script, seen))
    assert code == 3, out
    assert "- Verdict: FAIL\n" in out
    assert not any(_is_diff_request(call) for call in seen)
    run = [s for s in read_spans(tmp_path / "proofs.jsonl") if s.name == "run"][-1]
    assert run.exit_code == 3
    assert run.detail == "deadline: 0 proven, 0 failed, 1 undispatched"


def test_run_task_truncated_attempt_is_retried_with_a_larger_cap(tmp_path: Path) -> None:
    """Known-good: attempt 1's samples and its fallback all come back
    `finish_reason=length`; attempt 2 -- the recovery plan and the diff --
    runs one ladder step up. Known-bad was the old `propose`: every call
    recomputed the same cap, so round-3 T5 truncated three attempts in a
    row at 32768 with a longer prompt each time."""
    _git_repo(tmp_path)
    seen: list[dict[str, Any]] = []
    truncate_first = PROPOSAL_SAMPLES + 1  # the k samples and attempt 1's fallback

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.append(payload)
        if not _is_diff_request(payload):
            if "Decompose the mechanical coding task" in _prompt(payload):
                return _emit_response({"nodes": [_node_dict()]})
            return _text_response("Plan: emit the whole diff.")
        diffs = sum(1 for call in seen if _is_diff_request(call))
        return _truncated_response() if diffs <= truncate_first else _diff_response()

    client = VllmClient(api_key="k", transport=httpx.MockTransport(handler))
    code, out = _run(_options(tmp_path), client)
    assert code == 0, out
    assert "- Verdict: PASS\n" in out
    assert "- Attempts: 2\n" in out
    diff_calls = [call for call in seen if _is_diff_request(call)]
    assert len(diff_calls) == truncate_first + 1
    # No ladder. Every call, the retry included, is given the window
    # left after its own prompt; the repair brief is longer, so the retry
    # has slightly less room, never a "step up" that was fiction anyway.
    assert [call["max_tokens"] for call in diff_calls] == [_expected_cap(c) for c in diff_calls]
    first = diff_calls[0]["max_tokens"]
    assert first > 100_000
    assert diff_calls[-1]["max_tokens"] < first
    recovery = next(call for call in seen if "Diagnose the root cause" in _prompt(call))
    assert recovery["max_tokens"] == _expected_cap(recovery)
    assert f"completion truncated at {first} output tokens" in _prompt(recovery)
    assert "retry with more" not in _prompt(recovery)


def test_worker_max_tokens_is_the_window_left_after_the_prompt() -> None:
    """Known-good: a recorded 24739-char prompt against the container's
    175000 window gets everything but the over-counted prompt and the
    margin; the old cap for that node (20256) is nowhere in sight. A prompt
    that fills the window still gets the margin, never zero or less.
    Known-bad for the pre-flight: a node's diff budget is the window minus
    its read ceiling and the margin, with nothing subtracted for reasoning."""
    prompt = "x" * 24739
    assert worker_max_tokens(prompt, 175000) == 175000 - 24739 // 3 - OUTPUT_MARGIN
    assert worker_max_tokens(prompt, 175000) == 164706
    assert worker_max_tokens(prompt, 100000) == 89706
    assert worker_max_tokens("x" * (3 * 175000), 175000) == OUTPUT_MARGIN
    base = _node_dict()
    constraints = {**base["execution_constraints"], "max_context_tokens": 30000}
    node = Node.model_validate({**base, "execution_constraints": constraints})
    assert diff_budget(node, 175000) == 175000 - 30000 - OUTPUT_MARGIN
    assert diff_budget(node, 175000) == 142952
    assert diff_budget(node, 100000) == 67952


class _WindowClient:
    """A client whose /models answer is scripted: a length, or an error."""

    def __init__(self, reported: int | None, error: Exception | None = None) -> None:
        self._reported = reported
        self._error = error

    def max_model_len(self) -> int | None:
        if self._error is not None:
            raise self._error
        return self._reported


def test_server_context_window_prefers_the_flag_then_the_server_then_the_default() -> None:
    """The server's `max_model_len` sizes worker calls unless the
    user overrides it; a server that does not report one, or cannot be
    asked, falls back to the container's 175000."""
    assert server_context_window(cast("VllmClient", _WindowClient(131072)), None) == 131072
    assert server_context_window(cast("VllmClient", _WindowClient(131072)), 4096) == 4096
    assert server_context_window(cast("VllmClient", _WindowClient(None)), None) == 175000
    failing = _WindowClient(None, VllmRequestError("request failed: refused"))
    assert server_context_window(cast("VllmClient", failing), None) == DEFAULT_CONTEXT_WINDOW


def test_run_task_sizes_worker_calls_from_the_context_window_it_is_given(tmp_path: Path) -> None:
    """Known-good: the window `main` resolved (here 100000, as a
    server reporting `max_model_len` would give) is what every worker call
    is sized against; the default is not used when a window is known."""
    _git_repo(tmp_path)
    seen: list[dict[str, Any]] = []
    script = [_emit_response({"nodes": [_node_dict()]}), _diff_response()]
    code, _ = _run(_options(tmp_path, context_window=100000), _scripted_client(script, seen))
    assert code == 0
    assert seen[1]["max_tokens"] == _expected_cap(seen[1], 100000)
    assert seen[1]["max_tokens"] != _expected_cap(seen[1])
    assert 80_000 < seen[1]["max_tokens"] < 100_000


def test_main_run_takes_the_context_window_from_the_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`GET /models` reports `max_model_len`; `run` sizes against it,
    and `--context-window` overrides it."""
    _FakeClient.calls.clear()
    monkeypatch.setenv("SADDLE_VLLM_API_KEY", "k")
    monkeypatch.setattr("saddle.cli.VllmClient", _FakeClient)
    monkeypatch.setattr(_FakeClient, "max_model_len", lambda self: 120000)
    captured: list[RunOptions] = []

    def fake_run_task(options: RunOptions, *args: Any, **kwargs: Any) -> int:
        captured.append(options)
        return 0

    monkeypatch.setattr("saddle.cli.run_task", fake_run_task)
    assert main(["run", "--repo", str(tmp_path), "--yes", TASK]) == 0
    assert captured[-1].context_window == 120000
    assert main(["run", "--repo", str(tmp_path), "--yes", "--context-window", "65536", TASK]) == 0
    assert captured[-1].context_window == 65536


def test_run_task_emission_rejects_an_undeclared_scope_and_replans(tmp_path: Path) -> None:
    """Known-bad: the planner's first plan leaves `target_files` empty
    on an impl node; it is fed back as `undeclared-scope` and the second
    plan, which declares, runs."""
    _git_repo(tmp_path)
    seen: list[dict[str, Any]] = []
    script = [
        _emit_response({"nodes": [{**_node_dict(), "target_files": []}]}),
        _emit_response({"nodes": [_node_dict()]}),
        _diff_response(),
    ]
    code, out = _run(_options(tmp_path), _scripted_client(script, seen))
    assert code == 0, out
    assert "undeclared-scope: node 'n1' is impl and declares no target_files" in _prompt(seen[1])


def test_run_task_emission_rejects_a_node_too_large_before_any_worker_call(tmp_path: Path) -> None:
    """Known-bad: a node whose declared files cannot be diffed within
    the largest cap the harness sends is rejected at planning time with the
    estimate and budget named; no worker call happens for it. Round-3 T5's
    plan (four modules, 249 lines) is NOT this case -- its failure was the
    cap -- so the fixture declares a genuinely oversized file: 11000
    lines is 178048 tokens against the 164952 the default window leaves a
    node that reads 8000 (the old 8000-line fixture, 130048, now
    fits, because nothing is held back for reasoning any more)."""
    _git_repo(tmp_path)
    (tmp_path / "big.py").write_text("x = 1\n" * 11000)
    assert run_argv(["git", "add", "big.py"], tmp_path) == 0
    assert run_argv(["git", "commit", "-q", "-m", "big"], tmp_path) == 0
    seen: list[dict[str, Any]] = []
    script = [
        _emit_response({"nodes": [{**_node_dict(), "target_files": ["n.py", "big.py"]}]}),
        _emit_response({"nodes": [_node_dict()]}),
        _diff_response(),
    ]
    code, out = _run(_options(tmp_path), _scripted_client(script, seen))
    assert code == 0, out
    assert "node-too-large: node 'n1' declares 2 file(s)" in _prompt(seen[1])
    budget = diff_budget(Node.model_validate(_node_dict()), DEFAULT_CONTEXT_WINDOW)
    lines = 11000 + (tmp_path / "n.py").read_text().count("\n")
    estimate = lines * TOKENS_PER_LINE + DIFF_OVERHEAD_TOKENS
    assert f"estimated at {estimate} tokens" in _prompt(seen[1])
    assert f"over its {budget}-token emission budget" in _prompt(seen[1])
    assert not _is_diff_request(seen[0])
    assert not _is_diff_request(seen[1])


def test_file_lines_skips_files_that_do_not_read_as_text(tmp_path: Path) -> None:
    """A binary or missing file has no line count to size a diff from, so
    it contributes nothing rather than failing the plan."""
    (tmp_path / "n.py").write_text("a\nb\n")
    (tmp_path / "blob.bin").write_bytes(b"\xff\xfe\x00\x80")
    assert _file_lines(tmp_path, ["n.py", "blob.bin", "missing.py"]) == {"n.py": 2}


def test_run_task_cap_is_sized_from_the_node_baseline_not_a_failed_attempts_tree(
    tmp_path: Path,
) -> None:
    """Known-bad (round 3b): attempt 1 applied 699 lines of
    degenerate output and failed `syntax`; attempt 2's cap was then sized
    from the bloated tree and rose from 21536 to 32624. Now a call
    is given the window left after its own prompt, and the prompt shows the
    live tree: a bloated tree costs the next attempt room, it never buys
    any. Here attempt 1 bloats `n.py` by 300 lines and fails syntax;
    attempt 2's cap must be below attempt 1's.

    flip: this test's `Gate syntax: FAIL` assertion (whole-file writes). It held
    because attempt 2's modify-diff could not apply to the tree attempt 1
    had bloated, so the node never recovered and the run's verdict WAS
    attempt 1's failure. A whole-file write does not care what the tree
    holds, so attempt 2 now repairs it and the run passes. That is the
    defect the old expectation was resting on, not a contract: the
    contract here is the cap, and it is unchanged and still asserted.
    Attempt 1 must still bloat and still fail `syntax` or the cap proves
    nothing, so that half is asserted directly instead of through the
    run's verdict."""
    _git_repo(tmp_path)
    seen: list[dict[str, Any]] = []
    # Still 300 bodiless defs, so the tree is still bloated and still
    # fails `syntax`; only the envelope changed.
    bloat = whole_file("n.py", "def f():", "    return 2", *[f"def g{i}():" for i in range(300)])

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.append(payload)
        if not _is_diff_request(payload):
            if "Decompose the mechanical coding task" in _prompt(payload):
                return _emit_response({"nodes": [_node_dict()]})
            return _text_response("Plan: fix the syntax.")
        diffs = sum(1 for call in seen if _is_diff_request(call))
        return _diff_response(bloat) if diffs <= PROPOSAL_SAMPLES else _diff_response()

    client = VllmClient(api_key="k", transport=httpx.MockTransport(handler))
    _, out = _run(_options(tmp_path), client)
    diff_calls = [call for call in seen if _is_diff_request(call)]
    assert len(diff_calls) >= PROPOSAL_SAMPLES + 1, out
    assert "- syntax:" in _prompt(diff_calls[PROPOSAL_SAMPLES]), out
    assert [call["max_tokens"] for call in diff_calls] == [_expected_cap(c) for c in diff_calls]
    assert diff_calls[-1]["max_tokens"] < diff_calls[0]["max_tokens"]


def test_run_verify_prints_the_plan_a_run_sealed(tmp_path: Path) -> None:
    """`saddle verify` shows what was asked before what happened."""
    _git_repo(tmp_path)
    script = [_emit_response({"nodes": [_node_dict()]}), _diff_response()]
    code, _ = _run(_options(tmp_path), _scripted_client(script, []))
    assert code == 0
    out = io.StringIO()
    assert run_verify(tmp_path / "proofs.jsonl", stdout=out) == 0
    body = out.getvalue()
    assert "1 proof(s), " in body
    assert ", 1 plan(s)), no outcome span list (no autonomous run)\n" in body
    assert "plan: 1 node(s)\n  n1  impl  budget=low  ctx=8000  targets: n.py\n" in body


def test_run_task_flushes_the_plan_line_before_any_node_runs(tmp_path: Path) -> None:
    """A killed run (SIGTERM from `timeout`) took its buffered
    log with it; the plan line reaches the file before the first worker call."""
    _git_repo(tmp_path)
    flushed_at: list[str] = []

    class _Stream(io.StringIO):
        def flush(self) -> None:
            flushed_at.append(self.getvalue())
            super().flush()

    out = _Stream()
    script = [_emit_response({"nodes": [_node_dict()]}), _diff_response()]
    code = run_task(
        _options(tmp_path), _scripted_client(script, []), stdin=io.StringIO(), stdout=out
    )
    assert code == 0
    assert any(value.endswith("Plan: 1 node(s): n1\n") for value in flushed_at)


def test_run_task_seals_the_runs_settings_on_the_run_span(tmp_path: Path) -> None:
    """Model, server version, temperatures, window and efforts are
    the run span's argv, so two rounds can be compared on record."""
    _git_repo(tmp_path)
    seen: list[dict[str, Any]] = []
    client = _scripted_client([_emit_response({"nodes": [_node_dict()]}), _diff_response()], seen)
    code, _ = _run(_options(tmp_path, model="m", server_version="0.28.0"), client)
    assert code == 0
    run = next(s for s in read_spans(tmp_path / "proofs.jsonl") if s.name == "run")
    assert run.argv == [
        "run",
        f"context_window={DEFAULT_CONTEXT_WINDOW}",
        "emission=whole-file",
        "model=m",
        "reasoning_effort=medium",
        "recovery_temperature=0.7",
        f"ruff={ruff_version()}",
        "ruff_rules=F,E4,E7,E9,B",
        "sample_temperature=0.7",
        "server=0.28.0",
        "survivor_effort=low",
        "survivor_samples=10",
        "temperature=0.0",
        "worker_effort=node budget",
    ]
    assert ruff_version() != "unavailable"
    # The k draws arrive in any order; each carries its own seed.
    assert sorted(call["seed"] for call in seen[1:]) == list(range(PROPOSAL_SAMPLES))


def test_served_version_is_unknown_when_the_server_will_not_say() -> None:
    client, _ = _json_client_cli({"version": "0.28.0"})
    assert served_version(client) == "0.28.0"
    client, _ = _json_client_cli({})
    assert served_version(client) == "unknown"

    def refuse(request: httpx.Request) -> httpx.Response:
        down_msg = "down"
        raise httpx.ConnectError(down_msg)

    down = VllmClient(api_key="k", transport=httpx.MockTransport(refuse))
    assert served_version(down) == "unknown"


def _json_client_cli(payload: Any) -> tuple[VllmClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=payload)

    return VllmClient(api_key="k", transport=httpx.MockTransport(handler)), seen


def test_run_explain_is_redacted_by_default_and_raw_for_one_attempt(tmp_path: Path) -> None:
    """Known-good: the default tier names times, seeds, tokens and
    verdicts and never a prompt, a diff or the reasoning; `--attempt`
    prints one sidecar whole. Known-bad: an ambiguous or unknown attempt
    prefix is refused, not guessed."""
    _git_repo(tmp_path)
    seen: list[dict[str, Any]] = []
    client = _scripted_client([_emit_response({"nodes": [_node_dict()]}), _diff_response()], seen)
    assert _run(_options(tmp_path, server_version="0.28.0"), client)[0] == 0
    journal = tmp_path / "proofs.jsonl"
    out = io.StringIO()
    assert run_explain(journal, attempt=None, stdout=out) == 0
    body = out.getvalue()
    assert body.startswith(
        f"journal: {journal}\nverify: clean\nplan:\n  n1 impl budget=low tools: "
    )
    assert "server=0.28.0" in body
    assert "worker:n1 (" in body
    assert "attempt=1 prompt_sha256=" in body
    assert "seed=0" in body
    assert "samples: 0 gate(s) failed" in body
    assert TASK not in body
    assert "diff --git" not in body
    worker = next(s for s in read_spans(journal) if s.name == "worker:n1")
    out = io.StringIO()
    assert run_explain(journal, attempt=worker.span_id[:8], stdout=out) == 0
    raw = json.loads(out.getvalue())
    assert TASK in raw["prompt"]
    assert raw["diff"].startswith("diff --git")
    out = io.StringIO()
    assert run_explain(journal, attempt="zzzz", stdout=out) == 1
    assert out.getvalue() == "error: 0 attempt(s) match 'zzzz'\n"
    out = io.StringIO()
    assert run_explain(journal, attempt="", stdout=out) == 1
    assert out.getvalue().startswith("error: 2 attempt(s) match ''")


def test_explain_command_is_wired_and_needs_no_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SADDLE_VLLM_API_KEY", raising=False)
    monkeypatch.delenv("VLLM_API_KEY", raising=False)
    journal = tmp_path / "proofs.jsonl"
    _sealed_journal(journal)
    out = io.StringIO()
    assert main(["explain", str(journal)], stdout=out) == 0
    assert out.getvalue().startswith(f"journal: {journal}\nverify: clean\n")


def test_run_explain_handles_spans_without_sidecars_and_bare_sidecars(tmp_path: Path) -> None:
    """Edges: a worker span sealed before attempts had sidecars has none and is
    listed from its span alone; a sidecar with no samples prints no
    samples line; `--attempt` on a span whose sidecar file is gone is an
    error, not a crash."""
    journal = tmp_path / "proofs.jsonl"
    append_span(
        journal,
        build_span(
            node_id="n1",
            argv=[],
            duration_ms=10,
            exit_code=1,
            detail="old shape",
            kind="agent",
            name="worker:n1",
            span_id="a" * 32,
        ),
    )
    attempt_hash = write_attempt_sidecar(journal, "b" * 32, {"seed": 4, "samples": []})
    append_span(
        journal,
        build_span(
            node_id="n1",
            argv=["worker", "n1", "attempt=2", "prompt_sha256=x"],
            duration_ms=10,
            exit_code=0,
            detail="sealed",
            kind="agent",
            name="worker:n1",
            span_id="b" * 32,
            attempt_hash=attempt_hash,
            started_at="2026-09-20T18:00:00+00:00",
        ),
    )
    out = io.StringIO()
    assert run_explain(journal, attempt=None, stdout=out) == 0
    body = out.getvalue()
    assert f"  ?  {0.01:8.1f}s  exit 1  \n    old shape\n" in body
    assert "sidecar bbbbbbbbbbbb: seed=4\n" in body
    assert "samples:" not in body
    out = io.StringIO()
    assert run_explain(journal, attempt="aaaa", stdout=out) == 1
    assert out.getvalue() == f"error: no sidecar for {'a' * 32}\n"
    # A journal that fails to load is reported, not raised through.
    attempt_sidecar_path(journal, "b" * 32).unlink()
    out = io.StringIO()
    assert run_explain(journal, attempt=None, stdout=out) == 1
    assert out.getvalue().startswith("error: journal ")


# --- the survivor-test drawer ------------------------------------------------


def test_survivor_drawer_runs_at_low_effort_under_the_token_cap(tmp_path: Path) -> None:
    """Known-good: a candidate test draw is one diff call
    at the survivor effort (`low` by default), capped at the survivor token
    budget, at the sample temperature, with its own seed."""
    seen: list[dict[str, Any]] = []
    client = _scripted_client([_diff_response(), _diff_response()], seen)
    options = _options(tmp_path)
    draw = survivor_drawer(client, options)
    node = Node.model_validate(_node_dict())
    proposal = draw(node, "Write ONE new test file.", 7)
    assert proposal.diff.startswith("diff --git ")
    (call,) = seen
    assert call["reasoning_effort"] == "low"
    assert call["max_tokens"] == options.survivor_max_tokens
    assert call["temperature"] == options.sample_temperature
    assert call["seed"] == 7
    assert call["messages"][-1]["content"] == "Write ONE new test file."
    # Known-bad for the defaults: both knobs are options, not constants.
    other = survivor_drawer(
        client, _options(tmp_path, survivor_effort="none", survivor_max_tokens=99)
    )
    other(node, "Write ONE new test file.", 0)
    assert (seen[1]["reasoning_effort"], seen[1]["max_tokens"]) == ("none", 99)


def test_run_parses_the_survivor_knobs() -> None:
    args = build_parser().parse_args(
        ["run", "--survivor-effort", "none", "--survivor-samples", "4", "task"]
    )
    assert (args.survivor_effort, args.survivor_samples) == ("none", 4)
    defaults = build_parser().parse_args(["run", "task"])
    assert (defaults.survivor_effort, defaults.survivor_samples) == ("low", SURVIVOR_SAMPLES)


def test_build_repair_prompt_omits_the_section_when_no_plan_survives() -> None:
    """A withheld plan leaves no `Recovery plan:` heading behind.

    The worker fixes forward on the failure alone rather than being
    handed an empty heading, which reads as a diagnosis that found
    nothing.
    """
    node = Node.model_validate(_node_dict())
    prompt = build_repair_prompt(
        task=TASK,
        node=node,
        files=["n.py"],
        contents={"n.py": "def f():\n    return 3\n"},
        failure="Attempt 1 of 3 failed 1 gate(s):\n- tests: exited 1\n",
        plan=None,
    )
    assert "Recovery plan:" not in prompt
    # Under the whole-file envelope a repair IS the whole file, so the
    # instruction that used to sit here ("do not restate the whole
    # change") would now contradict the brief.
    assert "Do not restate" not in prompt
    assert "keep what was right and change what was not.\n\nAttempt 1 of 3" in prompt
    assert "--- n.py ---\ndef f():\n    return 3\n" in prompt


@pytest.mark.parametrize(
    ("plan", "routed"),
    [
        ("Plan: remove `f` from `n.py`; no test calls it.", False),
        ("Plan: `f` returns the wrong value, so return 2 instead.", True),
    ],
)
def test_run_task_withholds_a_recovery_plan_that_prescribes_a_deletion(
    tmp_path: Path, plan: str, routed: bool
) -> None:
    """The wiring, not the predicate.

    Known-bad is round 3g's attempt 2: the diagnosis step answered a
    coverage failure with "remove the `to_dict()`, `from_dict()`,
    `__eq__`, and `__repr__` methods" and saddle handed that to the
    worker, which spent 1871 s and 149 151 output tokens ending `length`
    with no diff -- a deletion `public-deletions` would have rejected.
    The routed half is here because a check that withheld every plan
    would pass a one-sided test while making the diagnosis step dead
    weight: the same run must still carry a plan that only says what is
    wrong.
    """
    _git_repo(tmp_path)
    seen: list[dict[str, Any]] = []
    truncate_first = PROPOSAL_SAMPLES + 1

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.append(payload)
        if not _is_diff_request(payload):
            if "Decompose the mechanical coding task" in _prompt(payload):
                return _emit_response({"nodes": [_node_dict()]})
            return _text_response(plan)
        diffs = sum(1 for call in seen if _is_diff_request(call))
        return _truncated_response() if diffs <= truncate_first else _diff_response()

    client = VllmClient(api_key="k", transport=httpx.MockTransport(handler))
    code, out = _run(_options(tmp_path), client)
    assert code == 0, out
    repair = _prompt([call for call in seen if _is_diff_request(call)][-1])
    assert "Attempt 1 of 3" in repair
    assert ("Recovery plan:" in repair) is routed
    assert (plan in repair) is routed


def test_ensure_repo_baselines_where_git_will_not_guess_an_identity(tmp_path: Path) -> None:
    """Known-bad: an initialised repo with no HEAD, on a host that
    supplies no identity and will not invent one. `saddle run` creates the
    baseline commit itself, so if it has no identity of its own the command
    fails before a plan is ever drawn."""
    assert run_argv(["git", "init"], tmp_path) == 0
    assert run_argv(["git", "config", "user.useConfigOnly", "true"], tmp_path) == 0
    assert _ensure_repo(tmp_path) is True
    author = subprocess.run(
        ["git", "log", "-1", "--format=%an <%ae>"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert author == "saddle <saddle@local>"


def test_web_starts_the_chat_ui_and_prints_where_it_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`saddle web` wires argv to serve(); nothing here may reach the network."""
    import webbrowser

    from saddle.web import app as web_app

    served: dict[str, object] = {}
    opened: list[str] = []
    monkeypatch.setattr(web_app, "serve", lambda **kw: served.update(kw))
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.setenv("SADDLE_VLLM_API_KEY", "k")

    out = io.StringIO()
    code = main(
        [
            "web",
            "--host",
            "127.0.0.1",
            "--port",
            "8123",
            "--workdir",
            str(tmp_path),
            "--sessions",
            str(tmp_path / "s"),
        ],
        stdout=out,
    )

    assert code == 0
    assert out.getvalue() == "saddle chat UI on http://127.0.0.1:8123/\n"
    assert opened == ["http://127.0.0.1:8123/"]
    assert served["host"] == "127.0.0.1"
    assert served["port"] == 8123
    assert served["workdir"] == tmp_path.resolve()
    assert served["sessions_root"] == tmp_path / "s"


def test_web_no_open_does_not_launch_a_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Over ssh or in a container there is no browser to open, and opening one
    # on the wrong machine is worse than not opening one at all.
    import webbrowser

    from saddle.web import app as web_app

    opened: list[str] = []
    monkeypatch.setattr(web_app, "serve", lambda **kw: None)
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.setenv("SADDLE_VLLM_API_KEY", "k")

    out = io.StringIO()
    assert main(["web", "--no-open", "--workdir", str(tmp_path)], stdout=out) == 0
    assert opened == []
    assert "saddle chat UI on" in out.getvalue()


def test_web_defaults_sessions_root_to_none_so_the_store_picks_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from saddle.web import app as web_app

    served: dict[str, object] = {}
    monkeypatch.setattr(web_app, "serve", lambda **kw: served.update(kw))
    monkeypatch.setenv("SADDLE_VLLM_API_KEY", "k")
    main(["web", "--no-open", "--workdir", str(tmp_path)], stdout=io.StringIO())
    assert served["sessions_root"] is None


def test_chat_and_web_are_the_same_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`saddle chat` reads like `saddle up` and `saddle run`; `web` still works."""
    from saddle.web import app as web_app

    monkeypatch.setenv("SADDLE_VLLM_API_KEY", "k")
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(web_app, "serve", lambda **kw: calls.append(kw))

    for name in ("chat", "web"):
        assert main([name, "--no-open", "--workdir", str(tmp_path)], stdout=io.StringIO()) == 0
    assert len(calls) == 2
    assert calls[0] == calls[1]


def test_web_on_a_non_loopback_host_prints_and_opens_a_token_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bind address beyond loopback needs a token, so the printed
    link -- and the one this opens in a browser -- has to carry it, and the
    same value has to be the one `serve` is told to require."""
    import webbrowser

    from saddle.web import app as web_app

    served: dict[str, object] = {}
    opened: list[str] = []
    monkeypatch.setattr(web_app, "serve", lambda **kw: served.update(kw))
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.setenv("SADDLE_VLLM_API_KEY", "k")
    monkeypatch.setenv("SADDLE_CHAT_TOKEN", "t0k3n")

    out = io.StringIO()
    code = main(
        ["web", "--host", "100.64.0.7", "--port", "8123", "--workdir", str(tmp_path)],
        stdout=out,
    )

    assert code == 0
    assert out.getvalue() == "saddle chat UI on http://100.64.0.7:8123/?token=t0k3n\n"
    assert opened == ["http://100.64.0.7:8123/?token=t0k3n"]
    assert served["token"] == "t0k3n"
    assert served["host"] == "100.64.0.7"


def test_web_on_loopback_passes_no_token_and_the_url_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from saddle.web import app as web_app

    served: dict[str, object] = {}
    monkeypatch.setattr(web_app, "serve", lambda **kw: served.update(kw))
    monkeypatch.setenv("SADDLE_VLLM_API_KEY", "k")

    out = io.StringIO()
    main(["web", "--no-open", "--workdir", str(tmp_path)], stdout=out)

    assert out.getvalue() == "saddle chat UI on http://127.0.0.1:8777/\n"
    assert served["token"] is None


def test_the_key_is_read_from_the_env_file_when_it_is_not_exported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Without this every command needs `set -a; . ~/.config/saddle/env; set +a`
    # in front of it, which is the friction that ends with a key pasted
    # somewhere it should not be.
    from saddle import cli

    env_file = tmp_path / "env"
    env_file.write_text(
        '# a comment\nUNRELATED=leave-me-alone\nexport SADDLE_VLLM_API_KEY="from-the-file"\n'
    )
    monkeypatch.delenv("SADDLE_VLLM_API_KEY", raising=False)
    monkeypatch.delenv("VLLM_API_KEY", raising=False)
    monkeypatch.setattr(cli, "KEY_FILE", str(env_file))

    assert cli._api_key() == "from-the-file"
    # Only the key is taken; this is not a dotenv loader.
    import os as _os

    assert "UNRELATED" not in _os.environ


def test_an_exported_key_beats_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from saddle import cli

    env_file = tmp_path / "env"
    env_file.write_text("SADDLE_VLLM_API_KEY=from-the-file\n")
    monkeypatch.setattr(cli, "KEY_FILE", str(env_file))
    monkeypatch.setenv("SADDLE_VLLM_API_KEY", "exported")
    assert cli._api_key() == "exported"


@pytest.mark.parametrize(
    ("line", "want"),
    [
        ("SADDLE_VLLM_API_KEY=plain", "plain"),
        ('SADDLE_VLLM_API_KEY="double"', "double"),
        ("SADDLE_VLLM_API_KEY='single'", "single"),
        ("export  SADDLE_VLLM_API_KEY=exported", "exported"),
        ("  SADDLE_VLLM_API_KEY = spaced  ", "spaced"),
        ("VLLM_API_KEY=fallback-name", "fallback-name"),
        ("SADDLE_VLLM_API_KEY=", None),  # present but empty is no key
        ("NOT_THE_KEY=value", None),
        ("SADDLE_VLLM_API_KEY_SUFFIXED=value", None),
        ("no equals sign here", None),
    ],
)
def test_the_env_file_is_parsed_the_way_a_shell_would(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, line: str, want: str | None
) -> None:
    from saddle import cli

    env_file = tmp_path / "env"
    env_file.write_text(line + "\n")
    monkeypatch.delenv("SADDLE_VLLM_API_KEY", raising=False)
    monkeypatch.delenv("VLLM_API_KEY", raising=False)
    monkeypatch.setattr(cli, "KEY_FILE", str(env_file))
    assert cli._api_key() == want


def test_a_missing_env_file_is_not_an_error_just_no_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from saddle import cli

    monkeypatch.delenv("SADDLE_VLLM_API_KEY", raising=False)
    monkeypatch.delenv("VLLM_API_KEY", raising=False)
    monkeypatch.setattr(cli, "KEY_FILE", str(tmp_path / "absent"))
    assert cli._api_key() is None

    err = io.StringIO()
    assert main(["chat", "--no-open"], stderr=err) == 1
    assert str(tmp_path / "absent") in err.getvalue()


def test_chat_defaults_its_workdir_to_the_ranch_and_makes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not "." -- a session's folder should not depend on where the server
    happened to be started from, which is not something the person using it
    can see."""
    from saddle.web import app as web_app

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    served: dict[str, object] = {}
    monkeypatch.setattr(web_app, "serve", lambda **kw: served.update(kw))
    monkeypatch.setenv("SADDLE_VLLM_API_KEY", "k")

    import saddle.cli as cli_module

    monkeypatch.setattr(cli_module, "DEFAULT_WORKDIR", home / "saddle-ranch")
    main(["chat", "--no-open"], stdout=io.StringIO())

    assert served["workdir"] == (home / "saddle-ranch").resolve()
    assert (home / "saddle-ranch").is_dir()  # made, not just named


def test_an_explicit_workdir_still_wins_and_is_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from saddle.web import app as web_app

    served: dict[str, object] = {}
    monkeypatch.setattr(web_app, "serve", lambda **kw: served.update(kw))
    monkeypatch.setenv("SADDLE_VLLM_API_KEY", "k")
    wanted = tmp_path / "elsewhere" / "deep"
    main(["chat", "--no-open", "--workdir", str(wanted)], stdout=io.StringIO())

    assert served["workdir"] == wanted.resolve()
    assert wanted.is_dir()


# --- `saddle audit` ---------------------------------------------------------
#
# Every fixture's changed source line is exactly `    return 2`: the conftest's
# autouse `mutmut` stub reports its killed mutants on that line, so a healthy
# tree passes the mutation gate for the stated reason.

AUDIT_BASE_CODE = "def f():\n    return 1\n"
AUDIT_FIXED_CODE = "def f():\n    return 2\n"
AUDIT_TEST_BODY = "from n import f\n\n\ndef test_f():\n    assert f() == {value}\n"


def _audit_git(root: Path, *argv: str) -> str:
    run = subprocess.run(["git", *argv], cwd=root, capture_output=True, text=True, check=True)
    return run.stdout.strip()


def _audit_commit(root: Path, files: dict[str, str], message: str) -> str:
    for name, text in files.items():
        (root / name).write_text(text)
    _audit_git(root, "add", "-A")
    _audit_git(root, "commit", "-m", message)
    return _audit_git(root, "rev-parse", "HEAD")


def _audit_repo(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True)
    _audit_git(root, "init")
    _audit_git(root, "config", "user.email", "test@example.com")
    _audit_git(root, "config", "user.name", "test")
    _audit_commit(root, files, "baseline")
    return root


@pytest.fixture
def audit_clean(tmp_path: Path) -> Path:
    """An accepted change: `n.py` fixed, its test new and untracked."""
    root = _audit_repo(tmp_path / "clean", {"n.py": AUDIT_BASE_CODE})
    (root / "n.py").write_text(AUDIT_FIXED_CODE)
    (root / "test_n.py").write_text(AUDIT_TEST_BODY.format(value=2))
    return root


@pytest.fixture
def audit_untracked_module(tmp_path: Path) -> Path:
    """A tested change plus an untracked `m.py` no test imports: coverage refuses."""
    root = _audit_repo(
        tmp_path / "refuse",
        {"n.py": AUDIT_BASE_CODE, "test_n.py": AUDIT_TEST_BODY.format(value=1)},
    )
    (root / "n.py").write_text(AUDIT_FIXED_CODE)
    (root / "test_n.py").write_text(AUDIT_TEST_BODY.format(value=2))
    (root / "m.py").write_text("def g():\n    return 7\n")
    return root


@pytest.fixture
def audit_two_commits(tmp_path: Path) -> tuple[Path, str, str]:
    """c1 -> c2 is a tested change; the working tree adds an uncommitted, untested `m.py`.

    c1 has no test: editing an existing assertion would trip assertion-preservation,
    and this fixture must be accepted for the stated reason only.
    """
    root = _audit_repo(tmp_path / "history", {"n.py": AUDIT_BASE_CODE})
    c1 = _audit_git(root, "rev-parse", "HEAD")
    c2 = _audit_commit(
        root,
        {"n.py": AUDIT_FIXED_CODE, "test_n.py": AUDIT_TEST_BODY.format(value=2)},
        "fix",
    )
    (root / "m.py").write_text("def g():\n    return 7\n")
    return root, c1, c2


def _audit_main(
    tmp_path: Path, repo: Path, *extra: str, cache: bool = True
) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    argv = ["audit", *extra, "--repo", str(repo)]
    if cache:
        argv += ["--cache", str(tmp_path / "cache")]
    code = main(argv, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def _check_lines(stdout: str) -> list[str]:
    return [ln for ln in stdout.splitlines() if ln.startswith(("PASS", "FAIL", "n/a"))]


def test_audit_needs_no_api_key(
    audit_clean: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SADDLE_VLLM_API_KEY", raising=False)
    monkeypatch.delenv("VLLM_API_KEY", raising=False)
    code, out, err = _audit_main(tmp_path, audit_clean)
    assert (code, err) == (0, "")
    assert out.rstrip().endswith("verdict: accept")


def test_audit_accept_prints_the_battery(audit_clean: Path, tmp_path: Path) -> None:
    code, out, _err = _audit_main(tmp_path, audit_clean)
    assert code == 0
    assert out.rstrip().splitlines()[-1] == "verdict: accept"
    lines = _check_lines(out)
    assert len(lines) == 13
    assert len([ln for ln in lines if ln.startswith("n/a")]) == 4
    header = out.splitlines()[0]
    assert header.endswith("fresh")
    assert not header.startswith(("PASS", "FAIL", "n/a"))


def test_audit_refuse_is_exit_one_with_the_failing_check(
    audit_untracked_module: Path, tmp_path: Path
) -> None:
    code, out, _err = _audit_main(tmp_path, audit_untracked_module)
    assert code == 1
    assert out.rstrip().endswith("verdict: refuse")
    coverage = [ln for ln in _check_lines(out) if "coverage" in ln.split()[1:2]]
    assert len(coverage) == 1
    assert coverage[0].startswith("FAIL")


def test_audit_nothing_to_audit_is_exit_three(tmp_path: Path) -> None:
    root = _audit_repo(tmp_path / "same", {"n.py": AUDIT_BASE_CODE})
    code, out, _err = _audit_main(tmp_path, root)
    assert code == 3
    assert out.rstrip().endswith("verdict: nothing to audit")
    assert _check_lines(out) == []


def test_audit_json_is_exactly_the_result_dict(audit_clean: Path, tmp_path: Path) -> None:
    code, out, _err = _audit_main(tmp_path, audit_clean, "--json")
    assert code == 0
    decoder = json.JSONDecoder()
    value, end = decoder.raw_decode(out)
    assert out[end:].strip() == ""
    assert value["verdict"] == "accept"
    assert set(value) == {
        "verdict",
        "tree",
        "baseline",
        "test_command",
        "checks",
        "mutation",
        "surface",
        "cached",
    }


def test_audit_rev_mode_ignores_the_working_tree(
    audit_two_commits: tuple[Path, str, str], tmp_path: Path
) -> None:
    root, c1, c2 = audit_two_commits
    before = _audit_git(root, "status", "--porcelain")
    # The fixture can tell the modes apart: the working tree against c1 is refused.
    tree_code, _out, _err = _audit_main(tmp_path, root, "--baseline", c1)
    assert tree_code == 1
    code, out, err = _audit_main(tmp_path, root, c2)
    assert (code, err) == (0, "")
    assert out.rstrip().endswith("verdict: accept")
    assert _audit_git(root, "status", "--porcelain") == before
    assert c1[:12] in out.splitlines()[0]


def test_audit_rev_mode_takes_an_explicit_baseline(
    audit_two_commits: tuple[Path, str, str], tmp_path: Path
) -> None:
    root, c1, c2 = audit_two_commits
    code, out, _err = _audit_main(tmp_path, root, c2, "--baseline", c2)
    assert code == 3
    assert out.rstrip().endswith("verdict: nothing to audit")
    assert c1[:12] not in out


def test_audit_unknown_revision_is_exit_two(audit_clean: Path, tmp_path: Path) -> None:
    code, out, err = _audit_main(tmp_path, audit_clean, "no-such-rev")
    assert code == 2
    assert out == ""
    assert err.startswith("error: ")
    assert "no-such-rev" in err


def test_audit_unknown_baseline_is_exit_two(audit_clean: Path, tmp_path: Path) -> None:
    code, _out, err = _audit_main(tmp_path, audit_clean, "--baseline", "no-such-base")
    assert code == 2
    assert "no-such-base" in err


def test_audit_a_non_repository_is_exit_two(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    for extra in ((), ("HEAD",)):
        code, out, err = _audit_main(tmp_path, plain, *extra)
        assert (code, out) == (2, "")
        assert err.startswith("error: ")


def test_audit_no_cache_writes_no_cache(audit_clean: Path, tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    code, _out, _err = _audit_main(tmp_path, audit_clean, "--no-cache", cache=False)
    assert code == 0
    assert not cache.exists()


def test_audit_cache_serves_the_second_run(audit_clean: Path, tmp_path: Path) -> None:
    _audit_main(tmp_path, audit_clean)
    assert (tmp_path / "cache").is_dir()
    code, out, _err = _audit_main(tmp_path, audit_clean)
    assert code == 0
    assert out.splitlines()[0].endswith("cached")


def test_audit_parser_defaults() -> None:
    from saddle import audit as audit_module

    args = build_parser().parse_args(["audit"])
    assert (args.rev, args.repo, args.baseline, args.json, args.no_cache) == (
        None,
        ".",
        None,
        False,
        False,
    )
    assert args.test_command == audit_module.AUDIT_TEST_COMMAND
    assert args.cache == audit_module.DEFAULT_AUDIT_CACHE


# --- checker probes for `saddle audit` ---------------------------------------


def test_audit_test_command_reaches_the_audit_in_both_modes(
    audit_two_commits: tuple[Path, str, str], tmp_path: Path
) -> None:
    """`--test-command` is the audit's command, working-tree or REV mode."""
    root, c1, c2 = audit_two_commits
    command = "python -m pytest -q -p no:cacheprovider"
    for extra in (("--baseline", c1), (c2,)):
        code, out, _err = _audit_main(tmp_path, root, *extra, "--test-command", command, "--json")
        assert json.loads(out)["test_command"] == command
        assert code in (0, 1)


def test_audit_rev_mode_uses_the_cache(
    audit_two_commits: tuple[Path, str, str], tmp_path: Path
) -> None:
    """The verdict cache serves a REV-mode rerun (its key holds no path)."""
    root, _c1, c2 = audit_two_commits
    first = _audit_main(tmp_path, root, c2)
    second = _audit_main(tmp_path, root, c2)
    assert first[0] == second[0] == 0
    assert first[1].splitlines()[0].endswith("fresh")
    assert second[1].splitlines()[0].endswith("cached")


def test_audit_rev_mode_accepts_a_relative_repo(
    audit_two_commits: tuple[Path, str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--repo .` (the default) must survive the clone running elsewhere."""
    root, _c1, c2 = audit_two_commits
    monkeypatch.chdir(root)
    out, err = io.StringIO(), io.StringIO()
    code = main(
        ["audit", c2, "--repo", ".", "--cache", str(tmp_path / "cache")],
        stdout=out,
        stderr=err,
    )
    assert (code, err.getvalue()) == (0, "")
    assert out.getvalue().rstrip().endswith("verdict: accept")


def test_chat_with_an_empty_token_file_is_a_clean_error_and_never_serves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contract B: `chat_token()` refuses an empty file with a message
    that names the path and the remedy; the CLI shows that one line and exits
    2 instead of a traceback, before any URL, browser or server."""
    import webbrowser

    from saddle.web import app as web_app

    token_file = tmp_path / "chat-token"
    token_file.write_text("")
    monkeypatch.setenv("SADDLE_VLLM_API_KEY", "k")
    monkeypatch.delenv("SADDLE_CHAT_TOKEN", raising=False)
    monkeypatch.setattr(web_app, "TOKEN_FILE", token_file)
    monkeypatch.setattr(web_app, "serve", lambda **kw: pytest.fail("served"))
    monkeypatch.setattr(webbrowser, "open", lambda url: pytest.fail("opened"))

    out, err = io.StringIO(), io.StringIO()
    # No `--no-open`: the browser opener is patched to fail, so it must not be reached.
    code = main(["chat", "--host", "100.64.0.1"], stdout=out, stderr=err)

    assert code == 2
    assert out.getvalue() == ""
    assert (
        err.getvalue()
        == f"error: {token_file} exists but is empty; write a token into it or delete it\n"
    )


def _p2_1_node(kind: str = "impl") -> Node:
    """Task-first fixture: two requirements with literals, a gate, one target file."""
    data = _node_dict()
    data["kind"] = kind
    data["target_files"] = ["validators.py"]
    data["task_prompt"] = "Write is_valid_code in validators.py."
    data["requirements"] = [
        {
            "id": "REQ-001",
            "statement": "A code is three digits.",
            "accepts": ["LITERAL-123"],
            "rejects": ["LITERAL-12a"],
        },
        {"id": "REQ-002", "statement": "Empty is invalid.", "accepts": ["ok"], "rejects": [""]},
    ]
    data["deterministic_gate"]["test_command"] = "pytest test_validators.py"
    return Node.model_validate(data)


P2_1_TASK: Final = "Add a validator for three-digit codes."
P2_1_BODY: Final = "def is_valid_code(text):\n    return FILE_BODY_MARKER\n"
# Everything the structured prompt carries that the task-first one drops.
P2_1_STRUCTURED_ONLY: Final = (
    "Requirements",
    "Gate command",
    "REQ-",
    "LITERAL-123",
    "LITERAL-12a",
    "hypothesis",
    "ruff",
)


def _p2_1_prompt(kind: str = "impl", failure: str | None = None) -> str:
    return build_worker_prompt(
        task=P2_1_TASK,
        node=_p2_1_node(kind),
        files=["validators.py"],
        contents={"validators.py": P2_1_BODY},
        failure=failure,
    )


def test_first_impl_attempt_prompt_is_task_first() -> None:
    """Task-first contract A known-good: task, node, file body, wire format and
    scope line, preceded by the system sentence; no requirement ids, literals,
    gate command or working rules. Known-bad is the next test: the
    same node's repair prompt carries every one of them."""
    prompt = _p2_1_prompt()
    assert prompt.startswith(TASK_FIRST_PREAMBLE + "\n")
    assert TASK_FIRST_PREAMBLE == (
        "You are an expert coding assistant. Read the task and the files, "
        "then write the finished code."
    )
    for present in (
        f"Task: {P2_1_TASK}",
        "Node n1: Write is_valid_code in validators.py.",
        "--- validators.py ---\n" + P2_1_BODY,
        WHOLE_FILE_RULES,
        "- Touch only these files: validators.py.",
        "Output ONLY the file sections, no commentary.",
    ):
        assert present in prompt, present
    for absent in P2_1_STRUCTURED_ONLY:
        assert absent not in prompt, absent
    # Order as the spec lists it: task, files, rules, scope, closing line.
    order = [
        prompt.index("Task: "),
        prompt.index("Repo files:"),
        prompt.index(WHOLE_FILE_RULES),
        prompt.index("- Touch only these files"),
        prompt.index("Output ONLY"),
    ]
    assert order == sorted(order)
    edit = build_worker_prompt(
        task=P2_1_TASK,
        node=_p2_1_node(),
        files=["validators.py"],
        contents={},
        emission="edit",
    )
    assert EDIT_RULES in edit
    assert WHOLE_FILE_RULES not in edit


def test_repair_and_retry_prompts_stay_structured() -> None:
    """Task-first contract B known-bad for the first-attempt test: every string
    that test requires absent is present in the retry prompts, beside the
    failure, and the preamble that marks the task-first shape is not."""
    kwargs: dict[str, Any] = {
        "task": P2_1_TASK,
        "node": _p2_1_node(),
        "files": ["validators.py"],
        "contents": {"validators.py": P2_1_BODY},
        "failure": "FAILURE-TEXT-9",
    }
    retry = _p2_1_prompt(failure="FAILURE-TEXT-9")
    repair = build_repair_prompt(**kwargs, plan="1. fix")
    recovery = build_recovery_plan_prompt(**kwargs)
    for prompt in (repair, recovery):
        for present in (*P2_1_STRUCTURED_ONLY, "FAILURE-TEXT-9", "- Touch only these files"):
            assert present in prompt, present
        assert TASK_FIRST_PREAMBLE not in prompt
    for present in P2_1_STRUCTURED_ONLY:
        assert present in retry, present
    assert not retry.startswith(TASK_FIRST_PREAMBLE)
    assert retry == build_structured_prompt(
        task=P2_1_TASK,
        node=_p2_1_node(),
        files=["validators.py"],
        contents={"validators.py": P2_1_BODY},
    )


def test_test_and_refactor_nodes_keep_the_structured_first_prompt() -> None:
    """Task-first contract C: only an impl node's first attempt changes shape."""
    for kind in ("test", "refactor"):
        prompt = _p2_1_prompt(kind)
        assert "Requirements" in prompt
        assert "Gate command: pytest test_validators.py" in prompt
        assert not prompt.startswith(TASK_FIRST_PREAMBLE)
    assert "Requirements" not in _p2_1_prompt("impl")


def test_task_first_prompt_withholds_contents_without_read_file() -> None:
    """The `read_file` binding holds on the task-first prompt too: names, no bodies."""
    data = _node_dict(tools=["write_file"])
    prompt = build_task_first_prompt(
        task="T", node=Node.model_validate(data), files=["n.py"], contents={"n.py": "MARKER = 1\n"}
    )
    assert CONTENTS_WITHHELD in prompt
    assert "MARKER = 1" not in prompt
    assert "Repo files:\nn.py" in prompt
    unscoped = _node_dict()
    unscoped["kind"] = "test"
    unscoped["target_files"] = []
    bare = build_task_first_prompt(
        task="T", node=Node.model_validate(unscoped), files=[], contents={}
    )
    assert "Touch only" not in bare
    assert "(no tracked files)" in bare


def test_run_seals_prompt_shape_per_attempt(tmp_path: Path) -> None:
    """Task-first contract D through the real caller: `saddle run`'s propose sends
    the task-first prompt on attempt 1 of an impl node and the structured
    repair prompt on attempt 2, and each attempt's sidecar says which.
    Known-bad: attempt 2's sidecar does not read "task-first"."""
    _git_repo(tmp_path)
    seen: list[dict[str, Any]] = []
    bad = DIFF.replace("+    return 2\n", "+    return 3\n")
    fix = whole_file("n.py", "def f():", "    return 2")
    script = [
        _emit_response({"nodes": [_node_dict()]}),
        _diff_response(bad),
        _text_response("1. Change the return value.\n"),
        _diff_response(fix),
    ]
    code, out = _run(_options(tmp_path), _scripted_client(script, seen))
    assert code == 0, out
    diff_calls = [call for call in seen if _is_diff_request(call)]
    assert _prompt(diff_calls[0]).startswith(TASK_FIRST_PREAMBLE)
    assert "Requirements" not in _prompt(diff_calls[0])
    assert "Requirements" in _prompt(diff_calls[-1])
    journal = tmp_path / "proofs.jsonl"
    agents = [s for s in read_spans(journal) if s.kind == "agent" and s.name == "worker:n1"]
    shapes = [_sidecar(journal, span)["prompt_shape"] for span in agents]
    assert shapes == ["task-first", "structured"]
