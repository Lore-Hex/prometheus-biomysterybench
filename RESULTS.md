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

The preview is 3 human-solvable + 2 human-difficult tasks. Run 2026-06-16,
`anthropic/claude-opus-4.8` via TrustedRouter, native tool calls, `--max-turns 60`.

| Subset | Solved (≥1 of 2 episodes) |
|---|---|
| **Human-solvable** | **3 / 3** |
| Human-difficult | 0 / 2 |
| **Overall problems** | **3 / 5** |
| Per-attempt | 5 / 10 |

| Task | HS | ep1 | ep2 | Note |
|---|---|---|---|---|
| hb020 — organism of a crystal structure | yes | ✓ Homo sapiens | ✓ Homo sapiens | real `blastp`, both episodes |
| hb002 — bacterium in a genome | yes | ✗ network `TimeoutError` | ✓ Bacillus licheniformis | solved via remote BLAST; ep1 was a transient read timeout, not a wrong answer |
| recq… — TF from ChIP peaks | yes | ✓ CTCF | ✓ CTCF | real MEME motif discovery |
| hb022 — which samples were drug-treated | no | ✗ wrong condition | ✗ wrong condition | split the samples correctly but picked the wrong group as Erastin-treated (a 50/50 call) |
| hb053 — stress on a transcriptome | no | ✗ "light stress" | ✗ "light stress" | concluded light vs the expected heat stress |

**Reading it:** on the honest harness — real tool boundaries, no fabrication,
no coaching — Opus 4.8 solves **all three human-solvable tasks**, doing genuine
BLAST and MEME analysis (vs ~0 on the earlier text-mode harness, where it
fabricated results). The two human-difficult misses are genuinely hard and a
tiny sample.

This is a faithful **approximation**, not exact replication. The published
Opus 4.8 numbers on the **full 99-task** BioMysteryBench are Human-Solvable
**80.4%** and Human-Difficult **40.0%** (Claude Fable 5 / Mythos 5 system card).
Our 3/3 human-solvable is consistent with 80.4%; 0/2 human-difficult is within
noise of 40.0% on a two-task sample. Exact-percentage parity needs the full
99-task set (not public) — but the *behavior* reproduces cleanly.

`hb002` was solved with **remote** BLAST (with one flaky timeout), so the
in-progress local `refseq_protein` will make species identification *reliable*
across episodes rather than being the difference between solving and failing.
