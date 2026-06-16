# Results

Local self-solve of the **BioMysteryBench 5-task public preview** using the
`claude` CLI as the agent (Opus 4.8), with each tool command executed inside the
`prometheus-biomysterybench` container and the four small local BLAST databases
mounted read-only.

Raw transcripts, problem prompts, and answer rubrics are kept private and are
**not** included here, per the benchmark's terms.

## Harness configuration

| Setting | Value |
|---|---|
| Provider | `local/claude-opus-4.8` (local `claude` CLI, `--model claude-opus-4-8`) |
| Tool protocol | native function tool calls (`run_shell`, `submit_answer`) |
| Command sandbox | `prometheus-biomysterybench:latest` container, per-task |
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

## 5-task preview, Opus 4.8 (1 episode)

The preview set is 3 human-solvable + 2 human-difficult tasks. Each iteration
below is a single episode; small-sample, so treat per-task outcomes as
illustrative, not precise rates.

| Task | Human-solvable | v1 | v2 (reasoning + anti-guess) | Note |
|---|---|---|---|---|
| hb020 (organism of a crystal structure) | yes | ✗ hallucinated `P. aeruginosa` | ✓ **`Homo sapiens`** via real BLAST (10 turns) | fixed by the fabrication fixes |
| hb002 (bacterium in a genome) | yes | ✗ hallucinated `S. aureus` | ✗ `Bacillus altitudinis` (genus right, species wrong) | needs `refseq_protein` / a 16S DB |
| recq… (TF from ChIP peaks) | yes | ✓ `CTCF` (15 turns) | ✓ `CTCF` (19 turns) | real multi-step analysis |
| hb022 (which samples were drug-treated) | no | ✓ (lucky, 2 turns) | ✗ picked the opposite condition group | genuine 50/50 direction call |
| hb053 (stress on a transcriptome) | no | ✓ `Heat stress` | ✓ `Heat stress` | |
| **Total** | | **3/5** | **3/5** (HS 2/3, HD 1/2) | |

The headline number held at 3/5, but the *composition* is the point. The two
human-solvable identification tasks were failing because the single-action
provider let Opus **fabricate a BLAST result it never ran** and answer from it.
Three harness fixes followed, each from an observed, reproducible failure:

1. **Allow reasoning, parse the last action.** Forcing "JSON only, no prose"
   made the model guess impulsively. → hb020 then did real analysis.
2. **Anti-guess task prompt.** Tell the agent metadata is scrubbed; derive the
   answer (extract sequences, BLAST) rather than recognize.
3. **Reject fabricated searches (harness guard).** If a `submit_answer`'s
   reasoning claims a search result (BLAST / top hit / % identity / e-value)
   but no search tool actually ran, the harness rejects it and tells the model
   to run the real command first. This is the principled fix for hb002, which
   was still inventing a "database-confirmed" 16S hit.

`hb002` also needs a database that can actually resolve it — `swissprot` (a
curated protein set) and the tiny `pdbnt` cannot pin a bacterial species. The
definitive run pairs the guard with **`refseq_protein`** (downloading) so the
agent can BLAST predicted proteins against a comprehensive set; that section
will be filled in after that run.

For reference, the published Opus 4.8 numbers on the **full 99-task**
BioMysteryBench are Human-Solvable 80.4% and Human-Difficult 40.0% (Claude
Fable 5 / Mythos 5 system card). A 5-task preview is far too small for those
rates to transfer; this is a tooling/repeatability check, not official parity.
