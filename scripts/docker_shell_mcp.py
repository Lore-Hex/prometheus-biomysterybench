"""Minimal MCP stdio server exposing a single ``run_shell`` tool that executes
inside a task's sandbox Docker container.

Used by the harness's agentic-local provider (model prefix ``agent/``): the
local ``claude`` CLI drives a genuine tool-use loop, and each tool call runs a
REAL command in the per-task container — so the model gets real output and
cannot hallucinate results (unlike the text-oracle local-claude provider).

The target container name is read from ``$BIOMYSTERY_EXEC_CONTAINER``. Launched
by the CLI via ``--mcp-config``; run with ``uv run --with mcp python ...``.
"""
import os
import shlex
import subprocess

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("shell")


@mcp.tool()
def run_shell(command: str, timeout_seconds: int = 120) -> str:
    """Run ONE non-destructive shell command in the working directory /work and
    return its real stdout/stderr. Bioinformatics tools (blastn, diamond, seqkit,
    samtools, bcftools, makeblastdb, efetch, ...) and BLAST databases at /blastdb
    (with $BLASTDB set) are available. Use timeout_seconds up to ~900 for slow
    analyses like remote BLAST."""
    container = os.environ["BIOMYSTERY_EXEC_CONTAINER"]
    t = max(1, min(int(timeout_seconds), 900))
    inner = f"timeout --signal=KILL {t} bash -lc {shlex.quote(command)}"
    try:
        proc = subprocess.run(
            ["docker", "exec", "-w", "/work", container, "bash", "-lc", inner],
            capture_output=True,
            text=True,
            timeout=t + 20,
        )
    except subprocess.TimeoutExpired:
        return f"(command timed out after {t}s)"
    out = proc.stdout or ""
    if proc.stderr:
        out += "\n[stderr]\n" + proc.stderr
    return out[:65536] or f"(exit {proc.returncode}, no output)"


if __name__ == "__main__":
    mcp.run()
