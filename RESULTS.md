# Results

Local self-solve of the **BioMysteryBench 5-task public preview** using the
`claude` CLI as the agent (Opus 4.8), with each tool command executed inside the
`prometheus-biomystery` container and the four small local BLAST databases
mounted read-only.

Raw transcripts, problem prompts, and answer rubrics are kept private and are
**not** included here, per the benchmark's terms.

## Harness configuration

| Setting | Value |
|---|---|
| Provider | `local/claude-opus-4.8` (local `claude` CLI, `--model claude-opus-4-8`) |
| Tool protocol | native function tool calls (`run_shell`, `submit_answer`) |
| Command sandbox | `prometheus-biomystery:latest` container, per-task |
| Local BLAST | `taxdb`, `swissprot`, `pdbaa`, `pdbnt` at `/blastdb` (read-only) |
| Network | enabled, gated by each task's `allowed_domains` |
| Budgets | `--max-turns 30 --command-timeout 900 --task-timeout 2400 --model-attempts 4` |

## Harness validation (single task)

`hb020` ("what organism does this crystal structure belong to?", inputs with
scrubbed metadata) — **solved, 1/1**, final answer *Homo sapiens*.

Two harness lessons surfaced and were fixed while bringing this up:

1. **Let the model reason before acting.** Forcing "emit only a JSON action,
   no prose" made Opus guess impulsively on the scrubbed input (a wrong
   one-look answer). Allowing brief reasoning and parsing the *last* action
   object from the reply fixed it — the model analyzed the structure and
   answered correctly.
2. **Discourage guessing in the task prompt.** Telling the agent that
   identifying metadata is often scrubbed and to derive the answer from the
   data (e.g. extract sequences and BLAST) — and not to guess — matters for
   this benchmark and helps any model run through the same harness.

## 5-task preview, Opus 4.8

_Run in progress; this section is updated when the full 1-episode run
completes. The preview set is 3 human-solvable + 2 human-difficult tasks._

For reference, the published Opus 4.8 numbers on the **full 99-task**
BioMysteryBench are Human-Solvable 80.4% and Human-Difficult 40.0% (Claude
Fable 5 / Mythos 5 system card). The 5-task preview is too small for those
rates to transfer directly; it is a tooling/repeatability check, not official
parity.
