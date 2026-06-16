# prometheus-biomystery

A small, **local and reproducible** harness for running the
[BioMysteryBench](https://www.anthropic.com/research/Evaluating-Claude-For-Bioinformatics-With-BioMysteryBench)
public preview — the bioinformatics-capability eval where a model is dropped
into a container of real, messy biological data and asked a research question
(e.g. *"what organism does this crystal structure belong to?"*). It ships:

- a Docker image with the canonical bioinformatics toolchain (BLAST, samtools,
  bcftools, bedtools, bwa, minimap2, seqkit, Entrez Direct, BioPython, R, …),
- scripts to set up **local NCBI BLAST databases** so identification tasks don't
  depend on slow/flaky remote BLAST,
- an agent loop (OpenAI-style native tool calls or a JSON fallback) with
  per-command timeouts, a network allow-list, multi-episode aggregation, and
  sanitized public output, and
- a **local-claude self-solve mode**: use the `claude` CLI on your own machine
  as the solving agent (e.g. Opus 4.8) — no remote model API key required.

It is a companion to [Lore-Hex/PrometheusBench](https://github.com/Lore-Hex/PrometheusBench)
(a short-prompt *refusal/permissiveness* benchmark). This repo is the
*capability* side: can the model actually do the bioinformatics?

> **Scope:** this targets the **5-task public preview**, not the full 99-task
> benchmark. The goal is a local, repeatable approximation with good tooling.

## What you get

```
prometheus_biomystery/biomystery.py   the harness (dataset, agent loop, grading, aggregation)
docker/biomystery/Dockerfile          bioinformatics container image
scripts/build_biomystery_env.sh       build the image
scripts/setup_blastdb.sh              download local BLAST databases
scripts/run_biomystery_self_solve.sh  self-solve with your local `claude` CLI
scripts/run_biomystery_preview_container.sh  run a remote model fully in-container
```

## Quickstart

### 1. Build the bioinformatics image

```bash
scripts/build_biomystery_env.sh        # builds prometheus-biomystery:latest
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

### 3a. Self-solve with your local `claude` (Opus 4.8)

You *are* the model under test: the harness runs on the host and uses the
`claude` CLI as a single-action oracle, while each tool command executes inside
the bioinformatics container with local BLAST mounted. No remote API key.

```bash
BLASTDB_DIR="$HOME/blastdb" scripts/run_biomystery_self_solve.sh
# quick smoke: one task, one episode
BLASTDB_DIR="$HOME/blastdb" scripts/run_biomystery_self_solve.sh --episodes 1 --problem-limit 1
```

How it works: Claude's own tools are disabled (`--tools ""`) and its system
prompt is replaced (`--system-prompt`) so it acts purely as a next-action
oracle — the *harness*, not Claude Code, runs the shell command. Transient
OAuth "Not logged in" token-refresh races are retried with backoff.

### 3b. Run a remote model fully in-container

```bash
export PROMETHEUSBENCH_API_KEY="sk-..."     # any OpenAI-compatible endpoint
export BLASTDB_DIR="$HOME/blastdb"          # optional: mount local BLAST
scripts/run_biomystery_preview_container.sh \
  --models anthropic/claude-opus-4.8 \
  --native-tools --allow-network \
  --episodes 2 --max-turns 100
```

## Run modes & key flags

| Flag | Meaning |
|---|---|
| `--models local/<id>` | route to the local `claude` CLI (any `local/` prefix) |
| `--local-claude-model` | `--model` passed to the CLI (default `claude-opus-4-8`) |
| `--exec-image IMAGE` | run each shell command inside this Docker image |
| `--exec-blastdb DIR` | mount `DIR` read-only at `/blastdb` (`BLASTDB=/blastdb`) |
| `--native-tools` | OpenAI-style function tool calls (recommended) vs JSON-in-text |
| `--allow-network` | permit fetches, still gated by each task's `allowed_domains` |
| `--episodes N` | run each task N times; aggregation reports solved-at-least-once |
| `--max-turns`, `--command-timeout`, `--task-timeout` | budgets |

The same harness drives the local-claude provider and any remote
OpenAI-compatible model, so it doubles as a test bench for the agent loop itself
before pointing it at other LLMs.

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
