# prometheus-biomysterybench

A small, **local and reproducible** harness for running the
[BioMysteryBench](https://www.anthropic.com/research/Evaluating-Claude-For-Bioinformatics-With-BioMysteryBench)
public preview — the bioinformatics-capability eval where a model is dropped
into a container of real, messy biological data and asked a research question
(e.g. *"what organism does this crystal structure belong to?"*). It ships:

- a Docker image with the canonical bioinformatics toolchain (BLAST, samtools,
  bcftools, bedtools, bwa, minimap2, seqkit, Entrez Direct, BioPython, R, …),
- scripts to set up **local NCBI BLAST databases** so identification tasks don't
  depend on slow/flaky remote BLAST,
- a **native tool-calling agent loop** — the model emits `run_shell` /
  `submit_answer` tool calls, the turn ends at each call, and the harness runs
  the command in the container and returns the real result (so the model can't
  fabricate tool output); with per-command timeouts, a network allow-list,
  multi-episode aggregation, and sanitized public output. Every model runs
  through the identical loop, and
- an optional **local-`claude` offline mode** for no-API-cost runs (a text-mode
  shim — less faithful than the API path; see the caveat below).

It is a companion to [Lore-Hex/PrometheusBench](https://github.com/Lore-Hex/PrometheusBench)
(a short-prompt *refusal/permissiveness* benchmark). This repo is the
*capability* side: can the model actually do the bioinformatics?

> **Scope:** this targets the **5-task public preview**, not the full 99-task
> benchmark. The goal is a local, repeatable approximation with good tooling.

## What you get

```
prometheus_biomysterybench/biomystery.py   the harness (dataset, agent loop, grading, aggregation)
docker/biomystery/Dockerfile          bioinformatics container image
scripts/build_biomystery_env.sh       build the image
scripts/setup_blastdb.sh              download local BLAST databases
scripts/run_biomystery_self_solve.sh  self-solve with your local `claude` CLI
scripts/run_biomystery_preview_container.sh  run a remote model fully in-container
```

## Quickstart

### 1. Build the bioinformatics image

```bash
scripts/build_biomystery_env.sh        # builds prometheus-biomysterybench:latest
```

### 2. Set up local BLAST databases

The four small databases (~0.9 GB total) cover most preview identification
tasks and download in a couple of minutes. They go to a **persistent host
directory** that is mounted into the container at run time — never baked into
the image.

```bash
export BLASTDB_DIR="$HOME/blastdb"
scripts/setup_blastdb.sh               # taxdb swissprot pdbaa pdbnt
```

- `pdbaa` / `pdbnt` — PDB sequences (protein / nucleotide): the fast path for
  "what organism is this structure?" tasks.
- `swissprot` — curated proteins, good general protein identification.
- `taxdb` — lets BLAST print organism scientific names
  (`-outfmt "6 sacc staxid ssciname"`).

`update_blastdb.pl` in the bioconda BLAST build only probes `/usr/bin` and
`/usr/local/bin` for `curl` (conda installs it under `/opt/conda/bin`); both the
Dockerfile and `setup_blastdb.sh` add the symlink so downloads work.

#### Large databases (refseq_protein, nr, nt)

These are big — `refseq_protein` is ~138 GB compressed and wants ~300–500 GB
once indexed; `nr`/`nt` are far larger. Put `BLASTDB_DIR` on a dedicated
fast SSD (e.g. an external/NAS SSD) and let the disk guard protect you:

```bash
export BLASTDB_DIR=/Volumes/BLASTDB/blastdb     # SSD with room to spare
scripts/setup_blastdb.sh refseq_protein         # refuses if < MIN_FREE_GB free
# FORCE_BIG=1 to override; MIN_FREE_GB=400 by default
```

Keep the SSD as the hot path; a spinning NAS works for occasional queries but is
slow and bad under concurrent evals.

### 3. Run the eval (native tool-calling — recommended)

Every model — Claude included — runs through the **same** native tool-calling
loop: the model emits a `run_shell` / `submit_answer` tool call, the API ends
the turn, the harness executes the command inside the bio container (with local
BLAST mounted) and returns the *real* result on the next turn. The model cannot
fabricate tool output, and every model gets the identical scaffold — so the only
difference between runs is the model.

```bash
export PROMETHEUSBENCH_API_KEY="sk-..."        # TrustedRouter / any OpenAI-compatible endpoint
export BLASTDB_DIR="$HOME/blastdb"
uv run --with huggingface_hub python -m prometheus_biomysterybench.biomystery \
  --models anthropic/claude-opus-4.8 \
  --exec-image prometheus-biomysterybench:latest --exec-blastdb "$BLASTDB_DIR" \
  --native-tools --allow-network \
  --episodes 2 --max-turns 60 \
  --private-out .eval_results_private/opus48.json \
  --public-out results/opus48.json
```

Swap `--models` for any OpenAI-compatible id (`openai/gpt-5.5`,
`google/gemini-3-flash-preview`, …) to evaluate other models on the same loop.
`scripts/run_biomystery_preview_container.sh` runs the whole harness *inside*
the container instead, if you prefer not to install anything on the host.

### Offline alternative: local `claude` CLI (no API key)

`--models local/claude-opus-4.8` drives the eval with your local `claude` CLI as
a single-action oracle (its own tools disabled), so you can run with no API
cost. **Caveat:** this is a text-mode shim with no hard tool-call boundary, so
it is *less faithful* than the API path above — it can occasionally let the
model answer from an imagined result. Prefer the API path for reported numbers.

```bash
BLASTDB_DIR="$HOME/blastdb" scripts/run_biomystery_self_solve.sh --episodes 1 --problem-limit 1
```

## Run modes & key flags

| Flag | Meaning |
|---|---|
| `--models <id>` | model(s) to run, e.g. `anthropic/claude-opus-4.8`, `openai/gpt-5.5` (any OpenAI-compatible id) |
| `--native-tools` | native function tool calls (recommended); a JSON-in-text fallback exists for endpoints without tool calls |
| `--exec-image IMAGE` | run each shell command inside this Docker image (bio tools) |
| `--exec-blastdb DIR` | mount `DIR` read-only at `/blastdb` (`BLASTDB=/blastdb`) |
| `--allow-network` | permit fetches, still gated by each task's `allowed_domains` |
| `--episodes N` | run each task N times; aggregation reports solved-at-least-once |
| `--max-turns`, `--command-timeout`, `--task-timeout` | budgets (give a generous `--max-turns`, e.g. 60+) |
| `--models local/<id>` | offline mode: drive via the local `claude` CLI (`--local-claude-model` sets the CLI model). Less faithful — see caveat above. |

One harness, one loop, for every model — so a comparison reflects the models,
not the scaffold. The system prompt is a neutral task + tools description: no
strategy hints, no answer coaching, and no guards that "correct" the model.

## Sandbox & safety

- Commands run inside the container; with `--exec-blastdb` the BLAST dir is
  mounted **read-only**. Without `--allow-network` the sandbox container gets
  `--network none`.
- A network allow-list (`allowed_domains` per task) gates every `curl`/`wget`
  and any URL in a command, enforced on the host before the command runs.
- Destructive commands (`rm`, `mkfs`, `shutdown`, `sudo`, …) are blocked.
- In-container `timeout --signal=KILL` plus a host-side process-group kill
  ensure a hung child (e.g. a stalled remote BLAST) cannot outlive its deadline.

## Privacy of benchmark material

Raw transcripts and the answer rubrics are written only under
`.eval_results_private/` (git-ignored). The public artifact under `results/`
excludes problem prompts, final answers, rubrics, and tool traces — it keeps
only scores, completion, latency, turns, and token usage. **Do not publish
BioMysteryBench answer rubrics or model work traces.**

## Results

See [`RESULTS.md`](RESULTS.md) for the latest local Opus 4.8 self-solve numbers
and harness configuration.

## Development

```bash
uv run ruff check .
uv run pytest -q
```

## License

Apache-2.0.
