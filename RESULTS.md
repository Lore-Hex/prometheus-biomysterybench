# Results

Opus 4.8 on the **BioMysteryBench 5-task public preview**, run through the
native tool-calling loop with each command executed inside the
`prometheus-biomysterybench` container and the local BLAST databases mounted
read-only.

Raw transcripts, problem prompts, and answer rubrics are kept private and are
**not** included here, per the benchmark's terms.

## Method (and why it's set up this way)

| Setting | Value |
|---|---|
| Model | `anthropic/claude-opus-4.8` (via TrustedRouter, native tool calls) |
| Tool protocol | native function tool calls: `run_shell`, `submit_answer` |
| Command sandbox | `prometheus-biomysterybench` container, one per task |
| Local BLAST | `pdbaa`, `pdbnt`, `swissprot` at `/blastdb` (read-only); `refseq_protein` when available |
| Network | enabled, gated per task by `allowed_domains` |
| System prompt | neutral task + tools description — **no** strategy hints, answer coaching, or guards |
| Budgets | `--episodes 2 --max-turns 60 --command-timeout 900 --task-timeout 3600` |

The benchmark itself drops the model into a shell + bioinformatics-tool
container, which is exactly what `run_shell` + `submit_answer` is. Every model
(Claude or any OpenAI-compatible id) runs through the **same** loop, so a result
reflects the model, not the scaffold.

**The one thing that matters most: real tool boundaries.** With native
tool-calling the model emits a tool call and the turn *ends*; it only continues
after the harness returns the real output. It is therefore impossible for the
model to "imagine" a tool result. We do **not** change the benchmark (dataset,
rubric, grader) and we do **not** coach the model or add guards that correct its
mistakes — those would bias the measurement.

## Why an honest scaffold mattered here (a debugging note)

An earlier exploration drove the eval with the local `claude` CLI as a
single-action text oracle (its own tools off, "emit one JSON action"). That
*text-mode shim has no hard tool-call boundary*, and it exposed a real failure
mode: on two identification tasks Opus **fabricated a BLAST hit it never ran**
("top hit, 99.9% identity, database-confirmed") and answered from it. We briefly
patched it with a guard that rejected such answers — but that is a crutch that
props up the score. The correct fix is the faithful loop above: with native
tool-calling the model *cannot* fabricate a result, so no guard or coaching is
needed. The local-CLI mode remains in the repo as a no-API-cost convenience,
clearly marked as less faithful.

## 5-task preview, Opus 4.8 (native tool-calling, 2 episodes)

The preview is 3 human-solvable + 2 human-difficult tasks.

_Definitive run in progress; numbers land here when it completes. Early
confirmation: on `hb020` (organism of a scrubbed crystal structure) Opus
extracts the sequence and runs real `blastp` — no fabrication — and simply needs
a generous turn budget to finish, which this run provides._

For reference, the published Opus 4.8 numbers on the **full 99-task**
BioMysteryBench are Human-Solvable 80.4% and Human-Difficult 40.0% (Claude
Fable 5 / Mythos 5 system card). A 5-task preview is far too small for those
rates to transfer; this is a tooling/repeatability check, not official parity.
