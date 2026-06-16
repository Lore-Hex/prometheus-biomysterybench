from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from prometheus_biomysterybench import biomystery as biomystery_preview
from prometheus_biomysterybench.biomystery import (
    Problem,
    _claims_unrun_search,
    _docker_exec_argv,
    _json_post,
    _local_action_to_tool_calls,
    _map_claude_usage,
    _render_messages_for_local,
    aggregate,
    biomystery_tools,
    call_model,
    expected_answers,
    format_tool_inventory,
    grade_answer,
    local_blast_databases,
    local_claude_complete,
    parse_action,
    public_summary,
    safe_run_command,
)


def test_parse_action_accepts_json_command() -> None:
    assert parse_action('{"cmd": "python3 analyze.py"}') == {"cmd": "python3 analyze.py"}


def test_parse_action_accepts_embedded_json_command() -> None:
    assert parse_action('I will inspect the file.\n{"cmd": "head -20 input.fasta"}') == {
        "cmd": "head -20 input.fasta"
    }


def test_parse_action_accepts_fenced_json_command() -> None:
    assert parse_action('```json\n{"cmd": "head -20 input.fasta"}\n```') == {"cmd": "head -20 input.fasta"}


def test_parse_action_accepts_json_final() -> None:
    assert parse_action('{"final": "Bacillus licheniformis"}') == {"final": "Bacillus licheniformis"}


def test_parse_action_accepts_final_answer_marker() -> None:
    assert parse_action("FINAL_ANSWER: Homo sapiens") == {"final": "Homo sapiens"}


def test_parse_action_rejects_plain_prose_as_final_answer() -> None:
    action = parse_action("I should inspect the FASTA file first.")
    assert action == {"invalid": "I should inspect the FASTA file first."}


def test_expected_answers_extracts_single_answer() -> None:
    rubric = "Expected answer is: Bacillus licheniformis. Score 1.0 if exact."
    assert expected_answers(rubric) == ["Bacillus licheniformis"]


def test_expected_answers_extracts_sample_list() -> None:
    rubric = "The answer is Sample_01, Sample_02, Sample_08. Score 1.0 if all samples are named."
    assert expected_answers(rubric) == ["Sample_01", "Sample_02", "Sample_08"]


def test_grade_answer_single_answer_is_normalized() -> None:
    rubric = "Expected answer is: Homo sapiens. Score 1.0 if correct."
    assert grade_answer("The organism is homo-sapiens.", rubric) == 1.0


def test_grade_answer_sample_list_requires_all_samples() -> None:
    rubric = "The answer is Sample_01, Sample_02, Sample_08. Score 1.0 if all samples are named."
    assert grade_answer("Sample_01 and Sample_08", rubric) == 0.0
    assert grade_answer("Sample_01, Sample_02, and Sample_08", rubric) == 1.0


def test_format_tool_inventory_lists_available_and_missing_tools() -> None:
    rendered = format_tool_inventory({"blastn": True, "samtools": False})
    assert rendered == "available: blastn; missing: samtools"


def test_public_summary_redacts_transcript_and_final_answer() -> None:
    rows = public_summary(
        [
            {
                "model": "model-a",
                "problem_id": "hb001",
                "episode": 1,
                "human_solvable": "yes",
                "score": 1.0,
                "final_answer": "private answer",
                "error": "http_500: sensitive body",
                "latency_ms": 100,
                "turns": 2,
                "usage": {"total_tokens": 12},
                "transcript": [{"assistant": "private transcript"}],
            }
        ]
    )
    assert rows == [
        {
            "model": "model-a",
            "problem_id": "hb001",
            "episode": 1,
            "human_solvable": "yes",
            "score": 1.0,
            "completed": False,
            "error": "http_500",
            "latency_ms": 100,
            "turns": 2,
            "usage": {"total_tokens": 12},
        }
    ]


def test_aggregate_reports_problem_solved_once_across_episodes() -> None:
    rows = [
        {
            "model": "model-a",
            "problem_id": "p1",
            "episode": 1,
            "human_solvable": "yes",
            "score": 0.0,
            "completed": True,
            "error": "",
            "latency_ms": 10,
            "turns": 1,
            "usage": {},
        },
        {
            "model": "model-a",
            "problem_id": "p1",
            "episode": 2,
            "human_solvable": "yes",
            "score": 1.0,
            "completed": True,
            "error": "",
            "latency_ms": 10,
            "turns": 1,
            "usage": {},
        },
        {
            "model": "model-a",
            "problem_id": "p2",
            "episode": 1,
            "human_solvable": "no",
            "score": 0.0,
            "completed": False,
            "error": "task_timeout",
            "latency_ms": 10,
            "turns": 1,
            "usage": {},
        },
    ]

    summary = aggregate(rows)[0]

    assert summary["total"] == 3
    assert summary["score_total_rate"] == 1 / 3
    assert summary["problems"] == 2
    assert summary["solved_problems"] == 1
    assert summary["solved_problem_rate"] == 0.5
    assert summary["human_solvable_problems"] == 1
    assert summary["human_solvable_solved_problem_rate"] == 1.0


def test_safe_run_command_blocks_network_fetches(tmp_path: Path) -> None:
    returncode, output = safe_run_command("curl https://example.com", cwd=tmp_path, timeout=1, max_output_chars=100)
    assert returncode == 126
    assert "Blocked command" in output


def test_safe_run_command_allows_explicit_allowed_domain_url(tmp_path: Path) -> None:
    returncode, output = safe_run_command(
        "echo https://ncbi.nlm.nih.gov/datasets",
        cwd=tmp_path,
        timeout=1,
        max_output_chars=100,
        allow_network=True,
        allowed_domains=("ncbi.nlm.nih.gov",),
    )
    assert returncode == 0
    assert "ncbi.nlm.nih.gov" in output


def test_safe_run_command_blocks_url_outside_allowed_domains(tmp_path: Path) -> None:
    returncode, output = safe_run_command(
        "echo https://example.com/data",
        cwd=tmp_path,
        timeout=1,
        max_output_chars=100,
        allow_network=True,
        allowed_domains=("ncbi.nlm.nih.gov",),
    )
    assert returncode == 126
    assert "not in this task's allowed_domains" in output


def test_safe_run_command_times_out(tmp_path: Path) -> None:
    returncode, _output = safe_run_command(
        "python3 -c 'import time; time.sleep(2)'",
        cwd=tmp_path,
        timeout=0.01,
        max_output_chars=100,
    )
    assert returncode == 124


def test_safe_run_command_timeout_preserves_partial_output(tmp_path: Path) -> None:
    returncode, output = safe_run_command(
        "python3 -c 'import sys, time; sys.stdout.buffer.write(b\"partial out\"); "
        "sys.stdout.flush(); sys.stderr.buffer.write(b\"partial err\"); sys.stderr.flush(); time.sleep(2)'",
        cwd=tmp_path,
        timeout=0.05,
        max_output_chars=200,
    )
    assert returncode == 124
    assert "partial out" in output
    assert "partial err" in output


def test_safe_run_command_timeout_kills_child_process_group(tmp_path: Path) -> None:
    started = __import__("time").monotonic()
    returncode, _output = safe_run_command(
        "python3 -c 'import subprocess, time; subprocess.Popen([\"sleep\", \"5\"]); time.sleep(5)'",
        cwd=tmp_path,
        timeout=0.05,
        max_output_chars=200,
    )
    elapsed = __import__("time").monotonic() - started
    assert returncode == 124
    assert elapsed < 2


def test_safe_run_command_executes_non_destructive_command(tmp_path: Path) -> None:
    (tmp_path / "input.txt").write_text("hello\n", encoding="utf-8")
    returncode, output = safe_run_command("cat input.txt", cwd=tmp_path, timeout=1, max_output_chars=100)
    assert returncode == 0
    assert output == "hello\n"


def test_safe_run_command_truncates_large_output(tmp_path: Path) -> None:
    returncode, output = safe_run_command(
        "python3 -c 'print(\"a\" * 500)'",
        cwd=tmp_path,
        timeout=1,
        max_output_chars=100,
    )
    assert returncode == 0
    assert "...[truncated]..." in output
    assert len(output) < 140


def test_call_model_builds_native_tool_request(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_post(
        url: str,
        *,
        headers: dict[str, str],
        body: dict[str, Any],
        timeout: float,
        max_attempts: int,
    ) -> dict[str, Any]:
        captured.update(
            {"url": url, "headers": headers, "body": body, "timeout": timeout, "max_attempts": max_attempts}
        )
        return {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "run_shell", "arguments": "{\"command\":\"ls\"}"},
                            }
                        ],
                    }
                }
            ],
            "usage": {"total_tokens": 5},
        }

    monkeypatch.setattr("prometheus_biomysterybench.biomystery._json_post", fake_post)

    text, usage, tool_calls = call_model(
        base_url="https://api.trustedrouter.com/v1",
        api_key="sk-test",
        model="anthropic/claude-opus-4.8",
        messages=[{"role": "user", "content": "test"}],
        timeout=10,
        max_tokens=256,
        native_tools=True,
    )

    body = captured["body"]
    assert text == ""
    assert usage == {"total_tokens": 5}
    assert tool_calls[0]["function"]["name"] == "run_shell"
    assert body["tools"] == biomystery_tools()
    assert body["tool_choice"] == "auto"


def test_solve_problem_uses_native_tool_calls(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from prometheus_biomysterybench import biomystery as biomystery_preview

    calls = [
        (
            "",
            {"total_tokens": 7},
            [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "run_shell", "arguments": "{\"command\":\"cat input.txt\"}"},
                }
            ],
        ),
        (
            "",
            {"total_tokens": 3},
            [
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {
                        "name": "submit_answer",
                        "arguments": "{\"answer\":\"Bacillus licheniformis\"}",
                    },
                }
            ],
        ),
    ]

    def fake_call_model(**_kwargs: Any) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
        return calls.pop(0)

    monkeypatch.setattr(biomystery_preview, "call_model", fake_call_model)
    monkeypatch.setattr(
        biomystery_preview,
        "safe_run_command",
        lambda command, **_kwargs: (0, "tool output") if command == "cat input.txt" else (1, "bad"),
    )
    (tmp_path / "input.txt").write_text("hello", encoding="utf-8")

    result = biomystery_preview.solve_problem(
        base_url="https://api.trustedrouter.com/v1",
        api_key="sk-test",
        model="anthropic/claude-opus-4.8",
        episode=1,
        problem=Problem(
            id="p1",
            question="question",
            answer_rubric="Expected answer is: Bacillus licheniformis. Score 1.0 if exact.",
            allowed_domains=(),
            human_solvable="yes",
        ),
        workdir=tmp_path,
        max_turns=4,
        llm_timeout=10,
        command_timeout=10,
        task_timeout=60,
        max_tokens=256,
        model_attempts=1,
        max_output_chars=1000,
        allow_network=False,
        native_tools=True,
        progress=False,
    )

    assert result["score"] == 1.0
    assert result["episode"] == 1
    assert result["final_answer"] == "Bacillus licheniformis"
    assert result["usage"] == {"total_tokens": 10}
    assert result["transcript"][0]["action"]["tool"] == "run_shell"
    assert result["transcript"][1]["action"]["tool"] == "submit_answer"


class _FakeHTTPResponse:
    def __init__(self, body: str) -> None:
        self._body = body

    def __enter__(self) -> _FakeHTTPResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body.encode("utf-8")


def test_json_post_retries_transient_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def fake_urlopen(_request: object, *, timeout: float) -> _FakeHTTPResponse:
        nonlocal calls
        calls += 1
        assert timeout == 10
        if calls == 1:
            raise urllib.error.HTTPError(
                "https://api.example.test",
                502,
                "Bad Gateway",
                {},
                io.BytesIO(b'{"error":"temporary"}'),
            )
        return _FakeHTTPResponse('{"ok": true}')

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    assert _json_post("https://api.example.test", headers={}, body={}, timeout=10, max_attempts=2) == {"ok": True}
    assert calls == 2


def test_json_post_retries_url_error(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def fake_urlopen(_request: object, *, timeout: float) -> _FakeHTTPResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise urllib.error.URLError("Tunnel connection failed: 502 Bad Gateway")
        return _FakeHTTPResponse('{"ok": true}')

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    assert _json_post("https://api.example.test", headers={}, body={}, timeout=10, max_attempts=2) == {"ok": True}
    assert calls == 2


def test_json_post_does_not_retry_non_transient_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def fake_urlopen(_request: object, *, timeout: float) -> _FakeHTTPResponse:
        nonlocal calls
        calls += 1
        raise urllib.error.HTTPError(
            "https://api.example.test",
            400,
            "Bad Request",
            {},
            io.BytesIO(b'{"error":"bad request"}'),
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    with pytest.raises(urllib.error.HTTPError):
        _json_post("https://api.example.test", headers={}, body={}, timeout=10, max_attempts=3)
    assert calls == 1


class _FakeCompleted:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _cli_result(result: str, *, is_error: bool = False, usage: dict[str, Any] | None = None) -> str:
    payload: dict[str, Any] = {"type": "result", "is_error": is_error, "result": result}
    if usage is not None:
        payload["usage"] = usage
    return json.dumps(payload)


def test_render_messages_for_local_splits_system_and_renders_roles() -> None:
    system_text, convo = _render_messages_for_local(
        [
            {"role": "system", "content": "System rules"},
            {"role": "user", "content": "Find the organism"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"type": "function", "function": {"name": "run_shell", "arguments": '{"command":"ls"}'}}
                ],
            },
            {"role": "tool", "content": "input.fasta"},
        ]
    )
    assert system_text == "System rules"
    assert "USER:\nFind the organism" in convo
    assert "run_shell" in convo and '{"command": "ls"}' in convo
    assert "TOOL RESULT:\ninput.fasta" in convo


def test_local_action_to_tool_calls_parses_run_shell_with_timeout() -> None:
    tool_calls = _local_action_to_tool_calls('{"tool":"run_shell","command":"blastp -db pdbaa","timeout_seconds":120}')
    assert tool_calls[0]["function"]["name"] == "run_shell"
    args = json.loads(tool_calls[0]["function"]["arguments"])
    assert args == {"command": "blastp -db pdbaa", "timeout_seconds": 120}


def test_local_action_to_tool_calls_parses_submit_answer_from_prose() -> None:
    tool_calls = _local_action_to_tool_calls(
        'Here is my answer.\n{"tool":"submit_answer","answer":"Bacillus subtilis"}'
    )
    assert tool_calls[0]["function"]["name"] == "submit_answer"
    assert json.loads(tool_calls[0]["function"]["arguments"]) == {"answer": "Bacillus subtilis"}


def test_local_action_to_tool_calls_uses_last_action_after_reasoning() -> None:
    text = (
        "Let me think. I could try {\"tool\":\"run_shell\",\"command\":\"ls\"} first to look around.\n"
        "Actually the sequence is clear, so I'll answer.\n"
        '```json\n{"tool":"submit_answer","answer":"Homo sapiens"}\n```'
    )
    tool_calls = _local_action_to_tool_calls(text)
    assert tool_calls[0]["function"]["name"] == "submit_answer"
    assert json.loads(tool_calls[0]["function"]["arguments"]) == {"answer": "Homo sapiens"}


def test_local_action_to_tool_calls_tolerates_json_mode_keys() -> None:
    assert _local_action_to_tool_calls('{"cmd":"head input.txt"}')[0]["function"]["name"] == "run_shell"
    assert _local_action_to_tool_calls('{"final":"Homo sapiens"}')[0]["function"]["name"] == "submit_answer"
    assert _local_action_to_tool_calls("not json at all") == []


def test_map_claude_usage_sums_cache_and_output_tokens() -> None:
    usage = _map_claude_usage(
        {
            "input_tokens": 10,
            "cache_creation_input_tokens": 100,
            "cache_read_input_tokens": 5,
            "output_tokens": 20,
        }
    )
    assert usage == {"prompt_tokens": 115, "completion_tokens": 20, "total_tokens": 135}


def test_local_claude_complete_native_returns_tool_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> _FakeCompleted:
        captured["argv"] = argv
        return _FakeCompleted(
            stdout=_cli_result(
                '{"tool":"submit_answer","answer":"Bacillus subtilis"}',
                usage={"input_tokens": 10, "output_tokens": 5},
            )
        )

    monkeypatch.setattr(biomystery_preview.subprocess, "run", fake_run)

    text, usage, tool_calls = local_claude_complete(
        messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "q"}],
        native_tools=True,
        local_model="claude-opus-4-8",
        timeout=30,
    )

    assert tool_calls[0]["function"]["name"] == "submit_answer"
    assert json.loads(tool_calls[0]["function"]["arguments"]) == {"answer": "Bacillus subtilis"}
    assert usage["total_tokens"] == 15
    argv = captured["argv"]
    assert argv[0] == "claude"
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--model") + 1] == "claude-opus-4-8"
    assert "submit_answer" in text


def test_local_claude_complete_retries_transient_not_logged_in(monkeypatch: pytest.MonkeyPatch) -> None:
    outputs = [
        _FakeCompleted(stdout=_cli_result("Not logged in · Please run /login", is_error=True)),
        _FakeCompleted(stdout=_cli_result('{"tool":"run_shell","command":"ls"}', usage={"output_tokens": 1})),
    ]
    calls = 0

    def fake_run(argv: list[str], **kwargs: Any) -> _FakeCompleted:
        nonlocal calls
        calls += 1
        return outputs.pop(0)

    monkeypatch.setattr(biomystery_preview.subprocess, "run", fake_run)
    monkeypatch.setattr(biomystery_preview.time, "sleep", lambda _seconds: None)

    _text, _usage, tool_calls = local_claude_complete(
        messages=[{"role": "user", "content": "q"}],
        native_tools=True,
        local_model="claude-opus-4-8",
        timeout=30,
        max_attempts=3,
    )

    assert calls == 2
    assert tool_calls[0]["function"]["name"] == "run_shell"


def test_local_claude_complete_raises_after_exhausting_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv: list[str], **kwargs: Any) -> _FakeCompleted:
        return _FakeCompleted(stdout=_cli_result("Not logged in · Please run /login", is_error=True))

    monkeypatch.setattr(biomystery_preview.subprocess, "run", fake_run)
    monkeypatch.setattr(biomystery_preview.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError):
        local_claude_complete(
            messages=[{"role": "user", "content": "q"}],
            native_tools=True,
            local_model="claude-opus-4-8",
            timeout=30,
            max_attempts=2,
        )


def test_call_model_routes_local_prefix_to_local_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_local(**kwargs: Any) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
        captured.update(kwargs)
        return "text", {"total_tokens": 3}, [{"type": "function", "function": {"name": "submit_answer"}}]

    monkeypatch.setattr(biomystery_preview, "local_claude_complete", fake_local)

    text, usage, tool_calls = call_model(
        base_url="https://api.trustedrouter.com/v1",
        api_key="",
        model="local/claude-opus-4.8",
        messages=[{"role": "user", "content": "q"}],
        timeout=30,
        max_tokens=256,
        native_tools=True,
        local_model="claude-opus-4-8",
    )

    assert text == "text"
    assert usage == {"total_tokens": 3}
    assert tool_calls[0]["function"]["name"] == "submit_answer"
    assert captured["local_model"] == "claude-opus-4-8"
    assert captured["native_tools"] is True


def test_docker_exec_argv_enforces_in_container_timeout() -> None:
    argv = _docker_exec_argv("bm_container", "blastp -db pdbaa -query q.faa", 45)
    assert argv[:5] == ["docker", "exec", "-w", "/work", "bm_container"]
    assert argv[5:7] == ["bash", "-lc"]
    assert "timeout --signal=KILL 45 bash -lc" in argv[7]
    assert "blastp -db pdbaa -query q.faa" in argv[7]


def test_claims_unrun_search_flags_fabricated_blast() -> None:
    # Claims a BLAST top hit but only inspected files -> fabricated.
    assert _claims_unrun_search(
        "The 16S rRNA gene best matches Bacillus altitudinis as the top hit (99.93% identity).",
        ["head -c 500 genome.fasta", "grep -c '>' genome.fasta"],
    )


def test_claims_unrun_search_ok_when_search_actually_ran() -> None:
    assert not _claims_unrun_search(
        "BLAST top hit is Homo sapiens at 100% identity.",
        ["blastp -query q.faa -db swissprot -outfmt 6"],
    )


def test_claims_unrun_search_ignores_non_search_answers() -> None:
    # A pandas/expression analysis answer that never mentions a search is fine.
    assert not _claims_unrun_search(
        "Differential expression shows heat-shock factors strongly upregulated.",
        ["python3 analyze.py"],
    )


def test_local_blast_databases_lists_query_targets_only(tmp_path: Path) -> None:
    for name in (
        "swissprot.pin",
        "pdbaa.pin",
        "pdbnt.nin",
        "refseq_protein.00.pin",
        "refseq_protein.pal",
        "taxdb.btd",
    ):
        (tmp_path / name).write_bytes(b"")
    assert local_blast_databases(str(tmp_path)) == ["pdbaa", "pdbnt", "refseq_protein", "swissprot"]
    assert local_blast_databases(None) == []
    assert local_blast_databases(str(tmp_path / "missing")) == []
