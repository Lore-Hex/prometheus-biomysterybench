from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "https://api.trustedrouter.com/v1"
DEFAULT_MODELS = (
    "deepseek/deepseek-v4-pro",
    "openai/gpt-5.5",
    "moonshotai/kimi-k2.6",
    "google/gemini-3.1-pro-preview",
    "google/gemini-3-flash-preview",
)
FINAL_RE = re.compile(r"FINAL_ANSWER\s*:\s*(.+)", re.IGNORECASE | re.DOTALL)
TRANSIENT_HTTP_CODES = {408, 409, 425, 429, 500, 502, 503, 504}
LOCAL_MODEL_PREFIX = "local/"
DEFAULT_LOCAL_CLAUDE_MODEL = "claude-opus-4-8"
EXEC_TIMEOUT_BACKSTOP = 20.0
_LOCAL_NATIVE_INSTRUCTION = (
    "\n\nRESPONSE FORMAT: You have no tools of your own; the harness runs commands for you. "
    "First reason briefly about what to do next, then END your reply with your chosen action as a "
    "single JSON object that is the LAST thing in your message. Use "
    '{"tool": "run_shell", "command": "<shell command>", "timeout_seconds": <optional integer>} '
    "to run one non-destructive shell command in the working directory, or "
    '{"tool": "submit_answer", "answer": "<short final biological answer>"} only once the actual '
    "command outputs shown above support a specific answer. "
    "Emit exactly ONE action and then STOP. Do NOT write, imagine, or assume the command's output — "
    "the real output is returned to you on the next turn, and you base your next step on it."
)
_LOCAL_JSON_INSTRUCTION = (
    "\n\nRESPONSE FORMAT: Reply with EXACTLY one minified JSON object and nothing else: "
    '{"cmd": "<shell command>"} to inspect/analyze files, or '
    '{"final": "<short final answer>"} when ready. No prose, no markdown fences.'
)

# TrustedRouter Fusion: model id "trustedrouter/fusion" runs a panel of models
# and (with selection_strategy "synthesize", the gateway default) a judge that
# synthesizes across all non-refusing panel answers into one response. The
# panel drives the same native run_shell/submit_answer loop as a single model.
FUSION_MODEL = "trustedrouter/fusion"
DEFAULT_FUSION_PANEL = (
    "openai/gpt-5.5",
    "anthropic/claude-opus-4.8",
    "moonshotai/kimi-k2.7-code",
    "z-ai/glm-5.1",
    "minimax/minimax-m3",
    "google/gemini-3.5-flash",
    "google/gemini-3.1-pro-preview",
    "google/gemini-2.5-pro",
)
DEFAULT_FUSION_JUDGE_MODEL = "anthropic/claude-opus-4.8"
DEFAULT_FUSION_SELECTION = "synthesize"


def fusion_tool(
    *,
    panel: Sequence[str],
    judge_model: str,
    max_completion_tokens: int = 4096,
    selection_strategy: str = DEFAULT_FUSION_SELECTION,
) -> dict[str, Any]:
    return {
        "type": "trustedrouter:fusion",
        "parameters": {
            "analysis_models": list(panel),
            "model": judge_model,
            "max_completion_tokens": max_completion_tokens,
            "selection_strategy": selection_strategy,
        },
    }
BIO_TOOL_COMMANDS = (
    "samtools",
    "bcftools",
    "bedtools",
    "blastn",
    "blastp",
    "bwa",
    "minimap2",
    "seqkit",
    "esearch",
    "efetch",
    "Rscript",
    "python",
)


@dataclass(frozen=True)
class Problem:
    id: str
    question: str
    answer_rubric: str
    allowed_domains: tuple[str, ...]
    human_solvable: str


def _api_key_from_env(explicit: str | None) -> str:
    if explicit:
        return explicit
    for name in (
        "BIOMYSTERY_API_KEY",
        "PROMETHEUSBENCH_API_KEY",
        "TRUSTEDROUTER_API_KEY",
        "TR_API_KEY_FOR_SELF_HEAL",
    ):
        value = os.environ.get(name)
        if value:
            return value
    raise SystemExit(
        "Missing API key. Set BIOMYSTERY_API_KEY, PROMETHEUSBENCH_API_KEY, "
        "TRUSTEDROUTER_API_KEY, TR_API_KEY_FOR_SELF_HEAL, or pass --api-key."
    )


def ensure_preview_dataset(root: Path) -> Path:
    dataset_dir = root / "biomystery_preview"
    problems = dataset_dir / "problems.csv"
    data_zip = dataset_dir / "data.zip"
    data_dir = dataset_dir / "data"
    if problems.exists() and data_dir.exists():
        return dataset_dir

    try:
        from huggingface_hub import hf_hub_download
    except ImportError as exc:  # pragma: no cover - checked in CLI use
        raise SystemExit("Install huggingface_hub to download BioMysteryBench preview.") from exc

    dataset_dir.mkdir(parents=True, exist_ok=True)
    for filename in ("README.md", "LICENSE", "problems.csv", "data.zip"):
        hf_hub_download(
            "Anthropic/BioMysteryBench-preview",
            filename=filename,
            repo_type="dataset",
            local_dir=dataset_dir,
        )

    if data_dir.exists():
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True)
    with zipfile.ZipFile(data_zip) as zf:
        zf.extractall(data_dir)
    return dataset_dir


def load_problems(dataset_dir: Path) -> list[Problem]:
    with (dataset_dir / "problems.csv").open(newline="", encoding="utf-8") as f:
        return [
            Problem(
                id=row["id"],
                question=row["question"],
                answer_rubric=row["answer_rubric"],
                allowed_domains=tuple(
                    part.strip().lower()
                    for part in row.get("allowed_domains", "").split(",")
                    if part.strip()
                ),
                human_solvable=row["human_solvable"],
            )
            for row in csv.DictReader(f)
        ]


def task_dir(dataset_dir: Path, problem_id: str) -> Path:
    path = dataset_dir / "data" / problem_id
    if not path.exists():
        raise FileNotFoundError(f"missing task directory for {problem_id}: {path}")
    return path


def file_manifest(path: Path, *, max_files: int = 40) -> str:
    rows = []
    for item in sorted(p for p in path.rglob("*") if p.is_file())[:max_files]:
        rel = item.relative_to(path).as_posix()
        rows.append(f"- {rel} ({item.stat().st_size} bytes)")
    return "\n".join(rows) if rows else "(no files)"


def tool_inventory() -> dict[str, bool]:
    return {tool: shutil.which(tool) is not None for tool in BIO_TOOL_COMMANDS}


def format_tool_inventory(inventory: dict[str, bool]) -> str:
    available = [tool for tool, present in inventory.items() if present]
    missing = [tool for tool, present in inventory.items() if not present]
    parts = [f"available: {', '.join(available) if available else 'none'}"]
    if missing:
        parts.append(f"missing: {', '.join(missing)}")
    return "; ".join(parts)


_CONTAINER_INVENTORY_CACHE: dict[str, dict[str, bool]] = {}


def container_tool_inventory(image: str) -> dict[str, bool]:
    """Probe which bioinformatics tools exist inside a Docker image (cached per image)."""
    if image in _CONTAINER_INVENTORY_CACHE:
        return _CONTAINER_INVENTORY_CACHE[image]
    probe = "; ".join(f'command -v {tool} >/dev/null 2>&1 && echo {tool}' for tool in BIO_TOOL_COMMANDS)
    completed = subprocess.run(
        ["docker", "run", "--rm", image, "bash", "-lc", probe],
        capture_output=True,
        text=True,
    )
    found = {line.strip() for line in completed.stdout.splitlines() if line.strip()}
    inventory = {tool: tool in found for tool in BIO_TOOL_COMMANDS}
    _CONTAINER_INVENTORY_CACHE[image] = inventory
    return inventory


def local_blast_databases(blastdb_dir: str | None) -> list[str]:
    """Return queryable BLAST database names found in a BLASTDB directory.

    Detects single-volume (``*.pin``/``*.nin``) and multi-volume alias
    (``*.pal``/``*.nal``) databases, collapsing volume suffixes like ``.00``.
    Support files such as ``taxdb`` (``*.btd``) are intentionally not listed
    because they are not query targets.
    """
    if not blastdb_dir:
        return []
    path = Path(blastdb_dir)
    if not path.is_dir():
        return []
    names: set[str] = set()
    for pattern in ("*.pin", "*.nin", "*.pal", "*.nal"):
        for item in path.glob(pattern):
            stem = item.name.rsplit(".", 1)[0]
            names.add(re.sub(r"\.\d+$", "", stem))
    return sorted(names)


def _json_post(
    url: str,
    *,
    headers: dict[str, str],
    body: dict[str, Any],
    timeout: float,
    max_attempts: int = 3,
    retry_delay: float = 1.0,
) -> dict[str, Any]:
    attempts = max(1, max_attempts)
    payload = json.dumps(body).encode("utf-8")
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(
            url,
            method="POST",
            headers={**headers, "Content-Type": "application/json"},
            data=payload,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:  # noqa: S310
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code not in TRANSIENT_HTTP_CODES or attempt == attempts:
                raise
        except urllib.error.URLError:
            if attempt == attempts:
                raise
        time.sleep(retry_delay * attempt)
    raise RuntimeError("unreachable retry loop")


def _extract_text(data: dict[str, Any]) -> str:
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    return "\n".join(
                        item["text"] for item in content if isinstance(item, dict) and isinstance(item.get("text"), str)
                    )
    return ""


def _extract_message(data: dict[str, Any]) -> dict[str, Any]:
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict) and isinstance(first.get("message"), dict):
            return first["message"]
    return {}


def _extract_tool_calls(data: dict[str, Any]) -> list[dict[str, Any]]:
    message = _extract_message(data)
    tool_calls = message.get("tool_calls")
    return tool_calls if isinstance(tool_calls, list) else []


def biomystery_tools() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "run_shell",
                "description": "Run one non-destructive shell command in the task working directory.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "The shell command to run. Keep it focused and non-destructive.",
                        },
                        "timeout_seconds": {
                            "type": "integer",
                            "description": (
                                "Optional command timeout in seconds. Use up to 900 for remote BLAST "
                                "or external database queries."
                            ),
                            "minimum": 1,
                        }
                    },
                    "required": ["command"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "submit_answer",
                "description": "Submit the final short biological answer for grading.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "answer": {
                            "type": "string",
                            "description": "The short final answer only.",
                        }
                    },
                    "required": ["answer"],
                    "additionalProperties": False,
                },
            },
        },
    ]


def _tool_call_name(tool_call: dict[str, Any]) -> str:
    function = tool_call.get("function")
    if isinstance(function, dict) and isinstance(function.get("name"), str):
        return function["name"]
    return ""


def _tool_call_arguments(tool_call: dict[str, Any]) -> dict[str, Any]:
    function = tool_call.get("function")
    if not isinstance(function, dict):
        return {}
    raw = function.get("arguments")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _message_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            item["text"] for item in content if isinstance(item, dict) and isinstance(item.get("text"), str)
        )
    return "" if content is None else str(content)


def _render_messages_for_local(messages: list[dict[str, Any]]) -> tuple[str, str]:
    """Split OpenAI-style messages into (system_text, rendered_conversation) for the claude CLI."""
    system_parts: list[str] = []
    convo: list[str] = []
    for message in messages:
        role = message.get("role")
        text = _message_content_text(message.get("content"))
        if role == "system":
            if text:
                system_parts.append(text)
        elif role == "user":
            convo.append(f"USER:\n{text}")
        elif role == "assistant":
            tool_calls = message.get("tool_calls")
            if isinstance(tool_calls, list) and tool_calls:
                rendered = "; ".join(
                    f"{_tool_call_name(call)}({json.dumps(_tool_call_arguments(call))})" for call in tool_calls
                )
                prefix = f"ASSISTANT (issued action: {rendered})"
                convo.append(f"{prefix}\n{text}" if text else prefix)
            else:
                convo.append(f"ASSISTANT:\n{text}")
        elif role == "tool":
            convo.append(f"TOOL RESULT:\n{text}")
    return "\n\n".join(system_parts), "\n\n".join(convo)


def _parse_claude_cli_json(stdout: str) -> dict[str, Any] | None:
    stripped = stdout.strip()
    if not stripped:
        return None
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        for line in reversed(stripped.splitlines()):
            candidate = line.strip()
            if candidate.startswith("{"):
                try:
                    data = json.loads(candidate)
                except json.JSONDecodeError:
                    continue
                return data if isinstance(data, dict) else None
        return None
    return data if isinstance(data, dict) else None


def _map_claude_usage(usage: Any) -> dict[str, int]:
    if not isinstance(usage, dict):
        return {}

    def _value(key: str) -> int:
        raw = usage.get(key)
        return raw if isinstance(raw, int) and not isinstance(raw, bool) else 0

    prompt = _value("input_tokens") + _value("cache_creation_input_tokens") + _value("cache_read_input_tokens")
    completion = _value("output_tokens")
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}


def _make_tool_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _extract_action_object(text: str) -> dict[str, Any] | None:
    """Return the LAST JSON object in ``text`` that looks like a harness action.

    The model is allowed to reason before acting, so we scan all decodable JSON
    objects (preferring fenced blocks) and pick the final one carrying an action
    key. This ignores any illustrative JSON mentioned earlier in the reasoning.
    """
    decoder = json.JSONDecoder()
    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.I | re.S)
    candidates: list[dict[str, Any]] = []
    for source in [*fenced, text]:
        for match in re.finditer(r"\{", source):
            try:
                data, _end = decoder.raw_decode(source[match.start() :])
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                candidates.append(data)
    action_keys = {"tool", "command", "answer", "cmd", "final"}
    for data in reversed(candidates):
        if action_keys & data.keys():
            return data
    return None


def _local_action_to_tool_calls(result_text: str) -> list[dict[str, Any]]:
    action = _extract_action_object(result_text)
    if not isinstance(action, dict):
        return []
    tool = action.get("tool")
    answer = action.get("answer")
    command = action.get("command")
    if tool == "submit_answer" and isinstance(answer, str):
        return [_make_tool_call("submit_answer", {"answer": answer})]
    if tool == "run_shell" and isinstance(command, str):
        args: dict[str, Any] = {"command": command}
        timeout_seconds = action.get("timeout_seconds")
        if isinstance(timeout_seconds, int) and not isinstance(timeout_seconds, bool):
            args["timeout_seconds"] = timeout_seconds
        return [_make_tool_call("run_shell", args)]
    if isinstance(action.get("final"), str):
        return [_make_tool_call("submit_answer", {"answer": action["final"]})]
    if isinstance(action.get("cmd"), str):
        return [_make_tool_call("run_shell", {"command": action["cmd"]})]
    return []


def local_claude_complete(
    *,
    messages: list[dict[str, Any]],
    native_tools: bool,
    local_model: str,
    timeout: float,
    max_attempts: int = 3,
    retry_delay: float = 2.0,
) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    """Use the local ``claude`` CLI as a single-action provider for the harness loop.

    Claude's own tools are disabled (``--tools ""``) and its persona is replaced
    (``--system-prompt``) so it acts as a clean next-action oracle: the harness,
    not Claude Code, executes the resulting shell command. Transient OAuth
    "Not logged in" responses (token-refresh races) are retried with backoff.
    """
    system_text, convo_text = _render_messages_for_local(messages)
    instruction = _LOCAL_NATIVE_INSTRUCTION if native_tools else _LOCAL_JSON_INSTRUCTION
    system_prompt = (system_text + instruction).strip()
    prompt = (convo_text + "\n\nEmit your next action now as a single JSON object.").strip()
    argv = [
        "claude",
        "-p",
        prompt,
        "--model",
        local_model,
        "--system-prompt",
        system_prompt,
        "--tools",
        "",
        "--strict-mcp-config",
        "--max-turns",
        "1",
        "--output-format",
        "json",
        "--no-session-persistence",
    ]
    attempts = max(1, max_attempts)
    last_error = "local claude provider produced no usable response"
    for attempt in range(1, attempts + 1):
        try:
            completed = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            last_error = "local claude CLI timed out"
        else:
            data = _parse_claude_cli_json(completed.stdout)
            result_text = data.get("result") if isinstance(data, dict) else None
            usable = (
                isinstance(data, dict)
                and not data.get("is_error")
                and isinstance(result_text, str)
                and "Not logged in" not in result_text
            )
            if usable:
                usage = _map_claude_usage(data.get("usage"))
                tool_calls = _local_action_to_tool_calls(result_text) if native_tools else []
                return result_text, usage, tool_calls
            if isinstance(result_text, str) and result_text:
                last_error = result_text[:200]
            elif completed.stderr.strip():
                last_error = completed.stderr.strip()[:200]
        if attempt < attempts:
            time.sleep(retry_delay * attempt)
    raise RuntimeError(f"local claude provider failed: {last_error}")


def _exec_container_name(workdir: Path) -> str:
    raw = "_".join(part for part in workdir.parts[-3:] if part not in ("", "/"))
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw).strip("_") or "task"
    return f"biomystery_{safe}_{os.getpid()}"


def start_exec_container(
    *,
    image: str,
    workdir: Path,
    blastdb: str | None,
    allow_network: bool,
) -> str:
    """Start a long-lived sandbox container for a task and return its name."""
    name = _exec_container_name(workdir)
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True)
    argv = ["docker", "run", "-d", "--rm", "--name", name, "-v", f"{workdir}:/work", "-w", "/work"]
    if blastdb:
        argv += ["-v", f"{blastdb}:/blastdb:ro", "-e", "BLASTDB=/blastdb"]
    if not allow_network:
        argv += ["--network", "none"]
    argv += [image, "sleep", "infinity"]
    completed = subprocess.run(argv, capture_output=True, text=True)
    if completed.returncode != 0:
        raise RuntimeError(f"failed to start exec container: {completed.stderr.strip()[:300]}")
    return name


def stop_exec_container(name: str) -> None:
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True)


def _docker_exec_argv(container: str, command: str, timeout: float) -> list[str]:
    """Wrap a command for execution inside the sandbox container.

    The in-container ``timeout`` enforces a hard process-tree kill so a hung
    child (e.g. a stalled remote BLAST) cannot outlive the deadline even if the
    host-side ``docker exec`` client is reaped first.
    """
    inner = f"timeout --signal=KILL {max(1, int(timeout))} bash -lc {shlex.quote(command)}"
    return ["docker", "exec", "-w", "/work", container, "bash", "-lc", inner]


def call_model(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, Any]],
    timeout: float,
    max_tokens: int,
    max_attempts: int = 3,
    native_tools: bool = False,
    local_model: str = DEFAULT_LOCAL_CLAUDE_MODEL,
    fusion_panel: Sequence[str] = DEFAULT_FUSION_PANEL,
    fusion_judge_model: str = DEFAULT_FUSION_JUDGE_MODEL,
    fusion_selection: str = DEFAULT_FUSION_SELECTION,
    fusion_max_completion_tokens: int = 4096,
) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    if model.startswith(LOCAL_MODEL_PREFIX):
        return local_claude_complete(
            messages=messages,
            native_tools=native_tools,
            local_model=local_model,
            timeout=timeout,
            max_attempts=max_attempts,
        )
    body = {
        "model": model,
        "messages": messages,
        "temperature": 0,
        "max_tokens": max_tokens,
    }
    tools: list[dict[str, Any]] = []
    if native_tools:
        tools = list(biomystery_tools())
        body["tool_choice"] = "auto"
    if model == FUSION_MODEL:
        # The panel drives the same agentic loop; the judge synthesizes per turn.
        tools.append(
            fusion_tool(
                panel=fusion_panel,
                judge_model=fusion_judge_model,
                max_completion_tokens=fusion_max_completion_tokens,
                selection_strategy=fusion_selection,
            )
        )
    if tools:
        body["tools"] = tools
    data = _json_post(
        base_url.rstrip("/") + "/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        body=body,
        timeout=timeout,
        max_attempts=max_attempts,
    )
    return (
        _extract_text(data),
        data.get("usage") if isinstance(data.get("usage"), dict) else {},
        _extract_tool_calls(data),
    )


def _host_allowed(host: str, allowed_domains: Sequence[str]) -> bool:
    normalized = host.lower().strip(".")
    return any(normalized == domain or normalized.endswith(f".{domain}") for domain in allowed_domains)


def _urls_in_command(command: str) -> list[str]:
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    urls: list[str] = []
    for part in parts:
        if part.startswith(("http://", "https://", "ftp://")):
            urls.append(part)
    return urls


def _network_policy_error(command: str, *, allow_network: bool, allowed_domains: Sequence[str]) -> str | None:
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    tools = {Path(part).name for part in parts[:2]}
    urls = _urls_in_command(command)
    uses_fetcher = bool({"curl", "wget"} & tools)
    if not uses_fetcher and not urls:
        return None
    if not allow_network:
        return "network access is disabled for this run"
    if uses_fetcher and not urls:
        return "curl/wget command must include an explicit URL so the harness can enforce allowed domains"
    for url in urls:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https", "ftp"} or not parsed.hostname:
            return f"unsupported URL in command: {url}"
        if not _host_allowed(parsed.hostname, allowed_domains):
            return f"network host {parsed.hostname!r} is not in this task's allowed_domains"
    return None


def safe_run_command(
    command: str,
    *,
    cwd: Path,
    timeout: float,
    max_output_chars: int,
    allow_network: bool = False,
    allowed_domains: Sequence[str] = (),
    progress_label: str = "",
    progress_interval: float = 30.0,
    exec_container: str | None = None,
) -> tuple[int, str]:
    blocked = ("rm ", "rmdir", "mkfs", "shutdown", "reboot", "sudo", "ssh ", "scp ")
    if any(part in f" {command} " for part in blocked):
        return 126, f"Blocked command by preview harness policy: {command}"
    if error := _network_policy_error(command, allow_network=allow_network, allowed_domains=allowed_domains):
        return 126, f"Blocked command by preview network policy: {error}"
    if exec_container:
        process = subprocess.Popen(
            _docker_exec_argv(exec_container, command, timeout),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        host_timeout = timeout + EXEC_TIMEOUT_BACKSTOP
    else:
        process = subprocess.Popen(  # noqa: S602
            command,
            cwd=cwd,
            shell=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            executable="/bin/bash",
            start_new_session=True,
        )
        host_timeout = timeout
    started = time.monotonic()
    while True:
        elapsed = time.monotonic() - started
        remaining = host_timeout - elapsed
        if remaining <= 0:
            _kill_process_group(process)
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired as exc:
                stdout = _decode_command_output(exc.stdout)
                stderr = _decode_command_output(exc.stderr)
            output = _format_command_output(stdout, stderr, max_output_chars=max_output_chars)
            return 124, output
        try:
            stdout, stderr = process.communicate(timeout=min(progress_interval, remaining))
            output = _format_command_output(stdout, stderr, max_output_chars=max_output_chars)
            return process.returncode, output
        except subprocess.TimeoutExpired:
            if progress_label:
                _progress(f"{progress_label} still running after {round(elapsed + progress_interval)}s")


def _decode_command_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _format_command_output(stdout: str | bytes | None, stderr: str | bytes | None, *, max_output_chars: int) -> str:
    output = _decode_command_output(stdout)
    decoded_stderr = _decode_command_output(stderr)
    if decoded_stderr:
        output += "\n[stderr]\n" + decoded_stderr
    if len(output) > max_output_chars:
        output = output[: max_output_chars // 2] + "\n...[truncated]...\n" + output[-max_output_chars // 2 :]
    return output


def _kill_process_group(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except Exception:
        process.kill()


def _progress(message: str) -> None:
    print(f"[biomystery] {message}", file=sys.stderr, flush=True)


def parse_action(text: str) -> dict[str, str]:
    stripped = text.strip()
    if match := FINAL_RE.search(stripped):
        return {"final": match.group(1).strip()}
    data = _parse_first_json_object(stripped)
    if isinstance(data, dict):
        if isinstance(data.get("final"), str):
            return {"final": data["final"].strip()}
        if isinstance(data.get("cmd"), str):
            return {"cmd": data["cmd"].strip()}
    if stripped.startswith("python ") or stripped.startswith("python3 ") or stripped.startswith("cat "):
        return {"cmd": stripped}
    return {"invalid": stripped[:2000]}


def _parse_first_json_object(text: str) -> dict[str, Any] | None:
    decoder = json.JSONDecoder()
    candidates = [text]
    candidates.extend(match.group(1) for match in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.I | re.S))
    for candidate in candidates:
        for match in re.finditer(r"\{", candidate):
            try:
                data, _end = decoder.raw_decode(candidate[match.start() :])
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                return data
    return None


def expected_answers(rubric: str) -> list[str]:
    match = re.search(r"(?:answer is|Expected answer is)\s*:?\s*(.+?)(?:\s+Score\s+1\.0|$)", rubric, re.I | re.S)
    if not match:
        return []
    text = match.group(1).strip().strip(".")
    if "Sample_01" in text:
        return re.findall(r"Sample_\d+", text)
    return [text.strip("'\" ")]


def normalize_answer(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def grade_answer(answer: str, rubric: str) -> float:
    expected = expected_answers(rubric)
    if not expected:
        return 0.0
    normalized = normalize_answer(answer)
    if len(expected) > 1:
        found = {item for item in expected if normalize_answer(item) in normalized}
        return 1.0 if len(found) == len(expected) else 0.0
    return 1.0 if normalize_answer(expected[0]) in normalized else 0.0


def solve_problem(
    *,
    base_url: str,
    api_key: str,
    model: str,
    problem: Problem,
    episode: int,
    workdir: Path,
    max_turns: int,
    llm_timeout: float,
    command_timeout: float,
    task_timeout: float,
    max_tokens: int,
    model_attempts: int,
    max_output_chars: int,
    allow_network: bool,
    native_tools: bool,
    progress: bool,
    local_model: str = DEFAULT_LOCAL_CLAUDE_MODEL,
    exec_image: str | None = None,
    exec_blastdb: str | None = None,
    fusion_panel: Sequence[str] = DEFAULT_FUSION_PANEL,
    fusion_judge_model: str = DEFAULT_FUSION_JUDGE_MODEL,
    fusion_selection: str = DEFAULT_FUSION_SELECTION,
) -> dict[str, Any]:
    started = time.monotonic()
    deadline = started + task_timeout if task_timeout > 0 else None
    network_note = (
        f"allowed only for these domains: {', '.join(problem.allowed_domains)}"
        if allow_network
        else "disabled"
    )
    tools = container_tool_inventory(exec_image) if exec_image else tool_inventory()
    blast_dbs = local_blast_databases(exec_blastdb or os.environ.get("BLASTDB"))
    blast_note = (
        f"Local BLAST databases at $BLASTDB (query with blastp/blastn -db NAME): {', '.join(blast_dbs)}.\n\n"
        if blast_dbs
        else ""
    )
    system_prompt = (
        """\
        You are solving a BioMysteryBench task in a local working directory.
        Inspect and analyze the files by issuing shell commands through the run_shell tool.
        Bioinformatics tools, pip/conda, and any listed local BLAST databases are available.
        Call submit_answer with the short final biological answer when you are confident.
        Keep commands non-destructive.
        For remote BLAST or other external database queries, request timeout_seconds near 900.
        """
        if native_tools
        else """\
        You are solving a BioMysteryBench task in a local working directory.
        Inspect and analyze the files by issuing shell commands.
        Reply with exactly one JSON object per turn:
        {"cmd": "shell command"} to inspect/analyze files, or
        {"final": "short final answer"} when ready.
        Do not wrap the JSON in prose.
        Keep commands non-destructive.
        """
    )
    start_instruction = (
        "Start by inspecting the files with run_shell. When ready, call submit_answer."
        if native_tools
        else "Start by inspecting the files. When ready, provide only the final biological answer."
    )
    messages = [
        {
            "role": "system",
            "content": textwrap.dedent(system_prompt),
        },
        {
            "role": "user",
            "content": (
                f"Problem ID: {problem.id}\n"
                f"Question: {problem.question}\n\n"
                f"Files in working directory:\n{file_manifest(workdir)}\n\n"
                f"Bioinformatics tools: {format_tool_inventory(tools)}.\n\n"
                f"{blast_note}"
                f"Network access: {network_note}.\n\n"
                f"{start_instruction}"
            ),
        },
    ]
    transcript: list[dict[str, Any]] = []
    usage_totals: dict[str, int] = {}
    final = ""
    error = ""

    exec_container: str | None = None
    container_error = ""
    if exec_image:
        if progress:
            _progress(f"{model} {problem.id}: starting sandbox container from {exec_image}")
        try:
            exec_container = start_exec_container(
                image=exec_image,
                workdir=workdir,
                blastdb=exec_blastdb,
                allow_network=allow_network,
            )
        except Exception as exc:  # noqa: BLE001
            container_error = f"exec_container_failed: {type(exc).__name__}: {exc}"

    for turn in range(1, max_turns + 1):
        if container_error:
            error = container_error
            break
        if deadline is not None and time.monotonic() >= deadline:
            error = "task_timeout"
            break
        try:
            request_timeout = llm_timeout
            if deadline is not None:
                request_timeout = max(1.0, min(request_timeout, deadline - time.monotonic()))
            if progress:
                _progress(f"{model} {problem.id} turn {turn}: model call start")
            text, usage, tool_calls = call_model(
                base_url=base_url,
                api_key=api_key,
                model=model,
                messages=messages,
                timeout=request_timeout,
                max_tokens=max_tokens,
                max_attempts=model_attempts,
                native_tools=native_tools,
                local_model=local_model,
                fusion_panel=fusion_panel,
                fusion_judge_model=fusion_judge_model,
                fusion_selection=fusion_selection,
            )
            if progress:
                _progress(
                    f"{model} {problem.id} turn {turn}: model call done, "
                    f"text={len(text)} chars, tool_calls={len(tool_calls)}, "
                    f"tokens={usage.get('total_tokens', 0)}"
                )
        except urllib.error.HTTPError as exc:
            error = f"http_{exc.code}: {exc.read().decode('utf-8', errors='replace')[:600]}"
            break
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
            break

        for key, value in usage.items():
            if isinstance(value, int):
                usage_totals[key] = usage_totals.get(key, 0) + value

        if native_tools and tool_calls:
            messages.append({"role": "assistant", "content": text or "", "tool_calls": tool_calls})
            for index, tool_call in enumerate(tool_calls):
                tool_name = _tool_call_name(tool_call)
                tool_args = _tool_call_arguments(tool_call)
                tool_call_id = str(tool_call.get("id") or f"call_{turn}_{index}")
                if tool_name == "submit_answer":
                    answer = tool_args.get("answer")
                    final = answer.strip() if isinstance(answer, str) else ""
                    transcript.append(
                        {
                            "turn": turn,
                            "assistant": text[:4000],
                            "action": {"tool": tool_name, "final": bool(final)},
                            "tool_call_id": tool_call_id,
                        }
                    )
                    if not final:
                        error = "empty_final_answer"
                    break
                if tool_name != "run_shell":
                    output = f"Unknown tool: {tool_name}"
                    transcript.append(
                        {
                            "turn": turn,
                            "assistant": text[:4000],
                            "action": {"tool": tool_name},
                            "tool_call_id": tool_call_id,
                            "returncode": 127,
                            "output": output,
                        }
                    )
                    messages.append({"role": "tool", "tool_call_id": tool_call_id, "content": output})
                    continue
                command = tool_args.get("command")
                command = command.strip() if isinstance(command, str) else ""
                if not command:
                    output = "Missing command argument for run_shell"
                    transcript.append(
                        {
                            "turn": turn,
                            "assistant": text[:4000],
                            "action": {"tool": tool_name, "cmd": ""},
                            "tool_call_id": tool_call_id,
                            "returncode": 126,
                            "output": output,
                        }
                    )
                    messages.append({"role": "tool", "tool_call_id": tool_call_id, "content": output})
                    continue
                run_timeout = command_timeout
                requested_timeout = tool_args.get("timeout_seconds")
                if isinstance(requested_timeout, int | float):
                    run_timeout = max(1.0, min(command_timeout, float(requested_timeout)))
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        error = "task_timeout"
                        break
                    run_timeout = max(1.0, min(run_timeout, remaining))
                if progress:
                    _progress(
                        f"{model} {problem.id} turn {turn}: run_shell start, "
                        f"timeout={round(run_timeout)}s, command={command[:160]!r}"
                    )
                returncode, output = safe_run_command(
                    command,
                    cwd=workdir,
                    timeout=run_timeout,
                    max_output_chars=max_output_chars,
                    allow_network=allow_network,
                    allowed_domains=problem.allowed_domains,
                    progress_label=f"{model} {problem.id} turn {turn} run_shell" if progress else "",
                    exec_container=exec_container,
                )
                if progress:
                    _progress(
                        f"{model} {problem.id} turn {turn}: run_shell done, "
                        f"exit={returncode}, output={len(output)} chars"
                    )
                transcript.append(
                    {
                        "turn": turn,
                        "assistant": text[:4000],
                        "action": {"tool": tool_name, "cmd": command},
                        "tool_call_id": tool_call_id,
                        "returncode": returncode,
                        "output": output[:4000],
                    }
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": f"Command exit code: {returncode}\nOutput:\n{output}",
                    }
                )
            if final or error:
                break
            continue

        action = parse_action(text)
        transcript.append({"turn": turn, "assistant": text[:4000], "action": action})
        if "final" in action:
            final = action["final"]
            break
        if "invalid" in action:
            messages.append({"role": "assistant", "content": text})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Your last response was not valid for this harness. "
                        "Reply with exactly one JSON object: "
                        "{\"cmd\": \"shell command\"} or {\"final\": \"short final answer\"}."
                    ),
                }
            )
            continue
        command = action.get("cmd", "")
        if not command:
            error = "invalid_action"
            break
        run_timeout = command_timeout
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                error = "task_timeout"
                break
            run_timeout = max(1.0, min(run_timeout, remaining))
        if progress:
            _progress(
                f"{model} {problem.id} turn {turn}: command start, "
                f"timeout={round(run_timeout)}s, command={command[:160]!r}"
            )
        returncode, output = safe_run_command(
            command,
            cwd=workdir,
            timeout=run_timeout,
            max_output_chars=max_output_chars,
            allow_network=allow_network,
            allowed_domains=problem.allowed_domains,
            progress_label=f"{model} {problem.id} turn {turn} command" if progress else "",
            exec_container=exec_container,
        )
        if progress:
            _progress(
                f"{model} {problem.id} turn {turn}: command done, "
                f"exit={returncode}, output={len(output)} chars"
            )
        transcript[-1]["returncode"] = returncode
        transcript[-1]["output"] = output[:4000]
        messages.append({"role": "assistant", "content": json.dumps({"cmd": command})})
        messages.append({"role": "user", "content": f"Command exit code: {returncode}\nOutput:\n{output}"})
    else:
        error = "max_turns_exceeded"

    if exec_container:
        stop_exec_container(exec_container)

    score = grade_answer(final, problem.answer_rubric) if final else 0.0
    return {
        "model": model,
        "problem_id": problem.id,
        "episode": episode,
        "human_solvable": problem.human_solvable,
        "score": score,
        "final_answer": final,
        "error": error,
        "latency_ms": round((time.monotonic() - started) * 1000),
        "turns": len(transcript),
        "usage": usage_totals,
        "tool_inventory": tools,
        "transcript": transcript,
    }


def public_summary(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for row in results:
        rows.append(
            {
                "model": row["model"],
                "problem_id": row["problem_id"],
                "episode": row.get("episode", 1),
                "human_solvable": row["human_solvable"],
                "score": row["score"],
                "completed": not bool(row.get("error")),
                "error": row.get("error", "").split(":", 1)[0] if row.get("error") else "",
                "latency_ms": row["latency_ms"],
                "turns": row["turns"],
                "usage": row.get("usage", {}),
            }
        )
    return rows


def aggregate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    models = sorted({row["model"] for row in rows})
    out = []
    for model in models:
        subset = [row for row in rows if row["model"] == model]
        completed = [row for row in subset if row["completed"]]
        human = [row for row in subset if row["human_solvable"] == "yes"]
        hard = [row for row in subset if row["human_solvable"] == "no"]
        problem_ids = sorted({row["problem_id"] for row in subset})
        solved_problem_ids = {
            row["problem_id"]
            for row in subset
            if float(row["score"]) > 0
        }
        human_problem_ids = sorted({row["problem_id"] for row in human})
        hard_problem_ids = sorted({row["problem_id"] for row in hard})
        solved_human_problem_ids = solved_problem_ids & set(human_problem_ids)
        solved_hard_problem_ids = solved_problem_ids & set(hard_problem_ids)
        out.append(
            {
                "model": model,
                "score": sum(float(row["score"]) for row in subset),
                "total": len(subset),
                "completed": len(completed),
                "errors": len(subset) - len(completed),
                "problems": len(problem_ids),
                "solved_problems": len(solved_problem_ids),
                "solved_problem_rate": len(solved_problem_ids) / len(problem_ids) if problem_ids else 0.0,
                "score_total_rate": sum(float(row["score"]) for row in subset) / len(subset) if subset else 0.0,
                "score_completed_rate": (
                    sum(float(row["score"]) for row in completed) / len(completed)
                    if completed
                    else 0.0
                ),
                "human_solvable_score": sum(float(row["score"]) for row in human),
                "human_solvable_total": len(human),
                "human_solvable_problems": len(human_problem_ids),
                "human_solvable_solved_problems": len(solved_human_problem_ids),
                "human_solvable_solved_problem_rate": (
                    len(solved_human_problem_ids) / len(human_problem_ids)
                    if human_problem_ids
                    else 0.0
                ),
                "human_difficult_score": sum(float(row["score"]) for row in hard),
                "human_difficult_total": len(hard),
                "human_difficult_problems": len(hard_problem_ids),
                "human_difficult_solved_problems": len(solved_hard_problem_ids),
                "human_difficult_solved_problem_rate": (
                    len(solved_hard_problem_ids) / len(hard_problem_ids)
                    if hard_problem_ids
                    else 0.0
                ),
            }
        )
    return sorted(out, key=lambda row: (-row["score"], row["errors"], row["model"]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a BioMysteryBench preview reproduction.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--models", default=None)
    parser.add_argument("--work-root", default=".eval_work")
    parser.add_argument("--private-out", default=".eval_results_private/biomystery_preview_raw.json")
    parser.add_argument("--public-out", default="results/biomystery_preview_trustedrouter_2026-06-14.json")
    parser.add_argument("--max-turns", type=int, default=50)
    parser.add_argument("--llm-timeout", type=float, default=240)
    parser.add_argument("--command-timeout", type=float, default=600)
    parser.add_argument("--task-timeout", type=float, default=1800)
    parser.add_argument("--max-tokens", type=int, default=8192)
    parser.add_argument("--model-attempts", type=int, default=3)
    parser.add_argument("--max-output-chars", type=int, default=65536)
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument("--native-tools", action="store_true", help="Use OpenAI-style function tool calls.")
    parser.add_argument("--no-progress", action="store_true", help="Disable per-turn progress logs.")
    parser.add_argument(
        "--local-claude-model",
        default=DEFAULT_LOCAL_CLAUDE_MODEL,
        help="CLI model passed to the local `claude` provider for any model id prefixed with 'local/'.",
    )
    parser.add_argument(
        "--exec-image",
        default=None,
        help="Run each shell command inside this Docker image (bioinformatics tools + BLAST).",
    )
    parser.add_argument(
        "--exec-blastdb",
        default=None,
        help="Host BLAST DB directory mounted read-only at /blastdb (BLASTDB=/blastdb) in the exec container.",
    )
    parser.add_argument("--problem-limit", type=int, default=None)
    parser.add_argument(
        "--problem-ids",
        default=None,
        help="Comma-separated problem ids to run (e.g. hb022,hb053); default runs all.",
    )
    parser.add_argument("--episodes", type=int, default=1)
    parser.add_argument(
        "--fusion-panel",
        default=",".join(DEFAULT_FUSION_PANEL),
        help="Comma-separated analysis panel for model id 'trustedrouter/fusion'.",
    )
    parser.add_argument("--fusion-judge-model", default=DEFAULT_FUSION_JUDGE_MODEL)
    parser.add_argument(
        "--fusion-selection",
        default=DEFAULT_FUSION_SELECTION,
        help="synthesize (default; judge merges all non-refusing answers), first_success, or first_non_refusal.",
    )
    args = parser.parse_args(argv)
    fusion_panel = tuple(part.strip() for part in args.fusion_panel.split(",") if part.strip())

    models = (
        [part.strip() for part in args.models.split(",") if part.strip()]
        if args.models
        else list(DEFAULT_MODELS)
    )
    # The local `claude` provider authenticates itself, so an API key is only
    # required when at least one model is routed through the remote API.
    if all(model.startswith(LOCAL_MODEL_PREFIX) for model in models):
        api_key = args.api_key or ""
    else:
        api_key = _api_key_from_env(args.api_key)
    dataset_dir = ensure_preview_dataset(Path(args.work_root))
    problems = load_problems(dataset_dir)
    if args.problem_ids:
        wanted = {p.strip() for p in args.problem_ids.split(",") if p.strip()}
        problems = [p for p in problems if p.id in wanted]
        missing = wanted - {p.id for p in problems}
        if missing:
            raise SystemExit(f"unknown problem ids: {', '.join(sorted(missing))}")
    if args.problem_limit:
        problems = problems[: args.problem_limit]
    raw_results = []
    for model in models:
        for episode in range(1, max(1, args.episodes) + 1):
            for problem in problems:
                source = task_dir(dataset_dir, problem.id)
                run_dir = (
                    Path(args.work_root)
                    / "biomystery_runs"
                    / normalize_answer(model)
                    / f"episode_{episode}"
                    / problem.id
                )
                if run_dir.exists():
                    shutil.rmtree(run_dir)
                shutil.copytree(source, run_dir)
                print(f"running {model} {problem.id} episode {episode}", flush=True)
                raw_results.append(
                    solve_problem(
                        base_url=args.base_url,
                        api_key=api_key,
                        model=model,
                        problem=problem,
                        episode=episode,
                        workdir=run_dir,
                        max_turns=args.max_turns,
                        llm_timeout=args.llm_timeout,
                        command_timeout=args.command_timeout,
                        task_timeout=args.task_timeout,
                        max_tokens=args.max_tokens,
                        model_attempts=args.model_attempts,
                        max_output_chars=args.max_output_chars,
                        allow_network=args.allow_network,
                        native_tools=args.native_tools,
                        progress=not args.no_progress,
                        local_model=args.local_claude_model,
                        exec_image=args.exec_image,
                        exec_blastdb=args.exec_blastdb,
                        fusion_panel=fusion_panel,
                        fusion_judge_model=args.fusion_judge_model,
                        fusion_selection=args.fusion_selection,
                    )
                )

    created_at = datetime.now(UTC).isoformat()
    harness_meta = {
        "max_turns": args.max_turns,
        "llm_timeout": args.llm_timeout,
        "command_timeout": args.command_timeout,
        "task_timeout": args.task_timeout,
        "max_tokens": args.max_tokens,
        "model_attempts": args.model_attempts,
        "max_output_chars": args.max_output_chars,
        "allow_network": args.allow_network,
        "native_tools": args.native_tools,
        "progress": not args.no_progress,
        "episodes": max(1, args.episodes),
        "local_claude_model": args.local_claude_model,
        "exec_image": args.exec_image,
        "exec_blastdb": args.exec_blastdb,
        "fusion": (
            {"panel": list(fusion_panel), "judge_model": args.fusion_judge_model, "selection": args.fusion_selection}
            if FUSION_MODEL in models
            else None
        ),
    }
    private_payload = {
        "benchmark": "BioMysteryBench-preview reproduction",
        "created_at": created_at,
        "models": models,
        "harness": harness_meta,
        "results": raw_results,
    }
    private_out = Path(args.private_out)
    private_out.parent.mkdir(parents=True, exist_ok=True)
    private_out.write_text(json.dumps(private_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    public_rows = public_summary(raw_results)
    public_payload = {
        "benchmark": "BioMysteryBench-preview reproduction",
        "created_at": created_at,
        "models": models,
        "harness": harness_meta,
        "problems": [
            {"id": problem.id, "human_solvable": problem.human_solvable}
            for problem in problems
        ],
        "results": public_rows,
        "aggregate": aggregate(public_rows),
        "notes": "Raw transcripts and answer rubrics intentionally excluded from this public artifact.",
    }
    public_out = Path(args.public_out)
    public_out.parent.mkdir(parents=True, exist_ok=True)
    public_out.write_text(json.dumps(public_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(public_payload["aggregate"], indent=2))
    print(f"wrote {public_out} and private raw transcript {private_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
