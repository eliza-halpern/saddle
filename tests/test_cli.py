"""Tests for saddle.cli."""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest

from saddle.cli import (
    DagOptions,
    RunError,
    RunOptions,
    build_emit_prompt,
    build_parser,
    build_worker_prompt,
    check_server,
    main,
    render_dag_plan,
    run_dag,
    run_doctor,
    run_task,
    run_verify,
)
from saddle.dag import Dag, Node
from saddle.evidence import run_argv
from saddle.journal import (
    GateOutput,
    append_record,
    append_span,
    build_record,
    build_span,
    read_records,
    read_spans,
)
from saddle.vllm import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    DagEmission,
    DiffProposal,
    VllmAuthError,
    VllmClient,
)

TASK = "Fix f to return 2 and add a passing test."

DIFF = (
    "diff --git a/n.py b/n.py\n"
    "--- a/n.py\n"
    "+++ b/n.py\n"
    "@@ -1,2 +1,2 @@\n"
    " def f():\n"
    "-    return 1\n"
    "+    return 2\n"
    "diff --git a/test_n.py b/test_n.py\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/test_n.py\n"
    "@@ -0,0 +1,5 @@\n"
    "+from n import f\n"
    "+\n"
    "+\n"
    "+def test_f_returns_two():  # REQ-001\n"
    "+    assert f() == 2\n"
)


def _git_repo(root: Path) -> None:
    for argv in (
        ["git", "init"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "test"],
    ):
        assert run_argv(argv, root) == 0
    (root / "n.py").write_text("def f():\n    return 1\n")
    (root / "README.md").write_text("demo\n")
    assert run_argv(["git", "add", "n.py", "README.md"], root) == 0
    assert run_argv(["git", "commit", "-m", "baseline"], root) == 0


def _node_dict(node_id: str = "n1", budget: str = "low") -> dict[str, Any]:
    return {
        "id": node_id,
        "dependencies": [],
        "task_prompt": "Fix f and test it.",
        "requirement_ids": ["REQ-001"],
        "execution_constraints": {
            "reasoning_budget": budget,
            "allowed_tools": ["read_file"],
            "max_context_tokens": 5000,
        },
        "deterministic_gate": {
            "test_command": "pytest test_n.py",
            "changed_line_coverage_min": 100.0,
            "red_phase_required": True,
            "mutation_sample": {
                "scope": "changed-lines",
                "max_mutants": 10,
                "kill_threshold": 85.0,
            },
        },
    }


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
    return _emit_response({"diff": diff})


def _scripted_client(script: list[httpx.Response], seen: list[dict[str, Any]]) -> VllmClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
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


def test_build_emit_prompt_names_task_and_rules() -> None:
    prompt = build_emit_prompt("Do the thing.")
    assert "Do the thing." in prompt
    assert "reasoning_budget is one of: zero, low, medium, xhigh." in prompt
    assert "read_file, write_file, run_tests, lint" in prompt
    assert "over test files only" in prompt
    assert "fewest nodes" in prompt


def test_build_worker_prompt_covers_format_rules_and_files() -> None:
    node = Node.model_validate(_node_dict())
    prompt = build_worker_prompt(
        task=TASK, node=node, files=["n.py", "m.py"], contents={"n.py": "x = 1\n"}
    )
    assert TASK in prompt
    assert "REQ-001" in prompt
    assert "Gate command: pytest test_n.py" in prompt
    assert "n.py" in prompt
    assert "m.py" in prompt
    assert "--- n.py ---\nx = 1\n" in prompt
    assert 'diff --git a/<file> b/<file>" header line' in prompt
    assert "new file mode 100644" in prompt
    assert "two blank lines between top-level definitions" in prompt
    assert "sorted import blocks" in prompt
    assert "Output ONLY the diff" in prompt


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
    node_dict["requirement_ids"] = ["REQ-001", "REQ-002"]
    node = Node.model_validate(node_dict)
    prompt = build_worker_prompt(
        task=TASK,
        node=node,
        files=["a.py", "b.py"],
        contents={"a.py": "1\n", "b.py": "2\n"},
    )
    assert "Requirements: REQ-001, REQ-002\n" in prompt
    assert "--- a.py ---\n1\n\n\n--- b.py ---\n2\n" in prompt


def test_build_worker_prompt_truncates_large_context() -> None:
    node = Node.model_validate(_node_dict())
    big = "x" * 9000
    prompt = build_worker_prompt(task=TASK, node=node, files=["n.py"], contents={"n.py": big})
    assert "[file context truncated]" in prompt
    assert "x" * 9000 not in prompt
    context = prompt.split("File contents:\n")[1].split("\n\nProduce a unified diff")[0]
    assert context.endswith("[file context truncated]")


def test_build_worker_prompt_keeps_exactly_max_context() -> None:
    from saddle.cli import MAX_CONTEXT_CHARS

    node = Node.model_validate(_node_dict())
    body = "y" * (MAX_CONTEXT_CHARS - len("--- n.py ---\n"))
    prompt = build_worker_prompt(task=TASK, node=node, files=["n.py"], contents={"n.py": body})
    assert "[file context truncated]" not in prompt


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
    assert [call["reasoning_effort"] for call in seen] == ["medium", "low"]
    assert seen[0]["max_tokens"] == 8192
    assert seen[0]["temperature"] == 0.0
    assert seen[1]["temperature"] == 0.0
    assert f"- Task: {TASK}\n" in out
    assert TASK in _prompt(seen[1])
    assert "--- n.py ---\ndef f():\n    return 1\n" in _prompt(seen[1])
    assert "--- README.md ---" not in _prompt(seen[1])


def test_run_task_honors_sampling_options(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    seen: list[dict[str, Any]] = []
    script = [_emit_response({"nodes": [_node_dict()]}), _diff_response()]
    client = _scripted_client(script, seen)
    options = _options(tmp_path, max_tokens=100, temperature=0.5, reasoning_effort="low")
    code, _ = _run(options, client)
    assert code == 0
    assert seen[0]["max_tokens"] == 100
    assert seen[0]["temperature"] == 0.5
    assert seen[0]["reasoning_effort"] == "low"
    assert seen[1]["temperature"] == 0.5


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
    assert len(seen) == 4
    assert _prompt(seen[0]) == build_emit_prompt(TASK)
    assert TASK in _prompt(seen[1])
    assert TASK in _prompt(seen[2])
    assert "Previous attempt failed:\ncontent is not valid JSON" in _prompt(seen[1])
    assert "content is not valid JSON" in _prompt(seen[2])
    assert "at least 1 item" in _prompt(seen[2])
    assert "(char 0)\n1 validation error for Dag" in _prompt(seen[2])


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
    script = [_emit_response({"nodes": [_node_dict()]}), _diff_response("not a diff\n")]
    client = _scripted_client(script, seen)
    code, out = _run(_options(tmp_path), client)
    assert code == 1
    assert "- Verdict: FAIL\n" in out


def test_run_task_zero_budget_maps_to_none(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    seen: list[dict[str, Any]] = []
    script = [_emit_response({"nodes": [_node_dict(budget="zero")]}), _diff_response()]
    client = _scripted_client(script, seen)
    code, _ = _run(_options(tmp_path), client)
    assert code == 0
    assert [call["reasoning_effort"] for call in seen] == ["medium", "none"]


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
    first = _node_dict("n1", "low")
    second = _node_dict("n2", "medium")
    second["dependencies"] = ["n1"]
    second["task_prompt"] = "Wire it up."
    second["requirement_ids"] = ["REQ-001", "REQ-002"]
    second["execution_constraints"]["allowed_tools"] = ["read_file", "write_file"]
    second["execution_constraints"]["max_context_tokens"] = 8000
    second["deterministic_gate"]["test_command"] = "pytest test_w.py"
    second["deterministic_gate"]["changed_line_coverage_min"] = 80.0
    second["deterministic_gate"]["red_phase_required"] = False
    second["deterministic_gate"]["mutation_sample"]["max_mutants"] = 5
    second["deterministic_gate"]["mutation_sample"]["kill_threshold"] = 90.0
    third = _node_dict("n3", "xhigh")
    third["dependencies"] = ["n1", "n2"]
    third["task_prompt"] = "Polish."
    third["execution_constraints"]["allowed_tools"] = ["lint"]
    third["execution_constraints"]["max_context_tokens"] = 12000
    third["deterministic_gate"]["test_command"] = "pytest test_p.py"
    third["deterministic_gate"]["changed_line_coverage_min"] = 90.0
    third["deterministic_gate"]["mutation_sample"]["max_mutants"] = 3
    third["deterministic_gate"]["mutation_sample"]["kill_threshold"] = 95.0
    dag = Dag.model_validate({"nodes": [first, second, third]})
    assert render_dag_plan("Do the thing.", dag) == (
        "Task: Do the thing.\n"
        "Plan: 3 node(s): n1, n2, n3\n"
        "├── n1 [budget: low, context: 5000 tokens]\n"
        "│   task: Fix f and test it.\n"
        "│   requirements: REQ-001\n"
        "│   depends on: (none)\n"
        "│   tools: read_file\n"
        "│   gate: pytest test_n.py (coverage >= 100.0%, red-phase required, "
        "mutation 10 @ 85.0% changed-lines)\n"
        "├── n2 [budget: medium, context: 8000 tokens]\n"
        "│   task: Wire it up.\n"
        "│   requirements: REQ-001, REQ-002\n"
        "│   depends on: n1\n"
        "│   tools: read_file, write_file\n"
        "│   gate: pytest test_w.py (coverage >= 80.0%, red-phase optional, "
        "mutation 5 @ 90.0% changed-lines)\n"
        "└── n3 [budget: xhigh, context: 12000 tokens]\n"
        "    task: Polish.\n"
        "    requirements: REQ-001\n"
        "    depends on: n1, n2\n"
        "    tools: lint\n"
        "    gate: pytest test_p.py (coverage >= 90.0%, red-phase required, "
        "mutation 3 @ 95.0% changed-lines)\n"
    )


def test_render_dag_plan_single_node() -> None:
    dag = Dag.model_validate({"nodes": [_node_dict("n1", "low")]})
    assert render_dag_plan("Do it.", dag) == (
        "Task: Do it.\n"
        "Plan: 1 node(s): n1\n"
        "└── n1 [budget: low, context: 5000 tokens]\n"
        "    task: Fix f and test it.\n"
        "    requirements: REQ-001\n"
        "    depends on: (none)\n"
        "    tools: read_file\n"
        "    gate: pytest test_n.py (coverage >= 100.0%, red-phase required, "
        "mutation 10 @ 85.0% changed-lines)\n"
    )


def _dag_options() -> DagOptions:
    return DagOptions(task=TASK, base_url="http://x/v1", model="m")


def test_run_dag_prints_plan() -> None:
    second = _node_dict("n2", "low")
    second["dependencies"] = ["n1"]
    seen: list[httpx.Request] = []
    client = _dag_client(["m"], {"nodes": [_node_dict("n1", "low"), second]}, seen)
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
        "├── n1 [budget: low, context: 5000 tokens]\n"
        "│   task: Fix f and test it.\n"
        "│   requirements: REQ-001\n"
        "│   depends on: (none)\n"
        "│   tools: read_file\n"
        "│   gate: pytest test_n.py (coverage >= 100.0%, red-phase required, "
        "mutation 10 @ 85.0% changed-lines)\n"
        "└── n2 [budget: low, context: 5000 tokens]\n"
        "    task: Fix f and test it.\n"
        "    requirements: REQ-001\n"
        "    depends on: n1\n"
        "    tools: read_file\n"
        "    gate: pytest test_n.py (coverage >= 100.0%, red-phase required, "
        "mutation 10 @ 85.0% changed-lines)\n"
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


def test_main_run_missing_key_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("SADDLE_VLLM_API_KEY", raising=False)
    monkeypatch.delenv("VLLM_API_KEY", raising=False)
    assert main(["run", "--repo", str(tmp_path), "Do it."]) == 1
    assert capsys.readouterr().err == "error: set SADDLE_VLLM_API_KEY (or VLLM_API_KEY)\n"


def test_main_run_missing_key_uses_explicit_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SADDLE_VLLM_API_KEY", raising=False)
    monkeypatch.delenv("VLLM_API_KEY", raising=False)
    err = io.StringIO()
    assert main(["run", "--repo", str(tmp_path), "Do it."], stderr=err) == 1
    assert err.getvalue() == "error: set SADDLE_VLLM_API_KEY (or VLLM_API_KEY)\n"


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
            "--yes",
            TASK,
        ],
        stdout=out,
    )
    assert code == 0
    assert _FakeClient.calls[0]["emit"]["max_tokens"] == 100
    assert _FakeClient.calls[0]["emit"]["temperature"] == 0.5
    assert _FakeClient.calls[0]["emit"]["reasoning_effort"] == "low"


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
        "temperature": 0.0,
        "reasoning_effort": "medium",
        "yes": False,
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
            "--reasoning-effort",
            "low",
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
        "temperature": 0.5,
        "reasoning_effort": "low",
        "yes": True,
    }


def test_run_help_pins_every_option(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit, match=r"^0$"):
        main(["run", "--help"])
    assert capsys.readouterr().out == (
        "usage: saddle run [-h] [--repo REPO] [--journal JOURNAL] [--base-url BASE_URL]\n"
        "                  [--model MODEL] [--max-tokens MAX_TOKENS]\n"
        "                  [--temperature TEMPERATURE]\n"
        "                  [--reasoning-effort {none,low,medium,xhigh}] [--yes]\n"
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
        "  --temperature TEMPERATURE\n"
        "                        Sampling temperature.\n"
        "  --reasoning-effort {none,low,medium,xhigh}\n"
        "                        Emission reasoning effort.\n"
        "  --yes                 Skip the plan confirmation.\n"
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
    assert capsys.readouterr().err == "error: set SADDLE_VLLM_API_KEY (or VLLM_API_KEY)\n"


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
        "usage: saddle dag [-h] [--base-url BASE_URL] [--model MODEL]\n"
        "                  [--max-tokens MAX_TOKENS] [--temperature TEMPERATURE]\n"
        "                  [--reasoning-effort {none,low,medium,xhigh}]\n"
        "                  task\n"
        "\n"
        "positional arguments:\n"
        "  task                  Task description to decompose into a plan.\n"
        "\n"
        "options:\n"
        "  -h, --help            show this help message and exit\n"
        "  --base-url BASE_URL   vLLM base URL.\n"
        "  --model MODEL         Model id.\n"
        "  --max-tokens MAX_TOKENS\n"
        "                        Emission max tokens.\n"
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
    assert capsys.readouterr().err == "error: set SADDLE_VLLM_API_KEY (or VLLM_API_KEY)\n"


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
        "└── n1 [budget: low, context: 5000 tokens]\n"
        "    task: Fix f and test it.\n"
        "    requirements: REQ-001\n"
        "    depends on: (none)\n"
        "    tools: read_file\n"
        "    gate: pytest test_n.py (coverage >= 100.0%, red-phase required, "
        "mutation 10 @ 85.0% changed-lines)\n"
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
    assert body.startswith(f"OK: {journal}: 1 proof(s), 1 span(s), chain verifies\n\n")
    assert "# Saddle slice transcript\n" in body
    assert "- Verdict: PASS\n" in body
    assert "## Node n1\n" in body
    assert f"- Path: {journal}\n" in body


def test_run_verify_missing_journal_is_fresh(tmp_path: Path) -> None:
    journal = tmp_path / "proofs.jsonl"
    out = io.StringIO()
    assert run_verify(journal, stdout=out) == 0
    assert out.getvalue().startswith(f"OK: {journal}: 0 proof(s), 0 span(s), chain verifies\n\n")
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
    assert out.startswith(f"OK: {journal}: 1 proof(s), 1 span(s), chain verifies\n\n")


def test_main_verify_defaults_to_repo_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = tmp_path / ".saddle" / "proofs.jsonl"
    _sealed_journal(journal)
    monkeypatch.chdir(tmp_path)
    out = io.StringIO()
    assert main(["verify"], stdout=out) == 0
    assert out.getvalue().startswith("OK: .saddle/proofs.jsonl: 1 proof(s)")


def test_verify_help_pins_every_option(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit, match=r"^0$"):
        main(["verify", "--help"])
    assert capsys.readouterr().out == (
        "usage: saddle verify [-h] [journal]\n"
        "\n"
        "positional arguments:\n"
        "  journal     Journal path (default: .saddle/proofs.jsonl).\n"
        "\n"
        "options:\n"
        "  -h, --help  show this help message and exit\n"
    )
