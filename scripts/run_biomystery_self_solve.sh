#!/usr/bin/env bash
# Self-solve the BioMysteryBench preview with the LOCAL `claude` CLI as the agent
# (Opus 4.8 by default). The harness runs on the host so it can call `claude`;
# each tool command executes inside the biomystery container with local BLAST
# mounted. No remote model API key is required.
#
# Env (all optional):
#   MODEL_ID             results label / provider id (default: local/claude-opus-4.8)
#   LOCAL_CLAUDE_MODEL   --model passed to the claude CLI (default: claude-opus-4-8)
#   IMAGE                exec container image (default: prometheus-biomysterybench:latest)
#   BLASTDB_DIR          host BLAST DB dir (default: ${HOME}/blastdb)
#
# Extra args are appended verbatim and override the defaults below
# (e.g. --episodes 1 --problem-limit 1 for a quick smoke).
set -euo pipefail
cd "$(dirname "$0")/.."

MODEL_ID="${MODEL_ID:-local/claude-opus-4.8}"
LOCAL_CLAUDE_MODEL="${LOCAL_CLAUDE_MODEL:-claude-opus-4-8}"
IMAGE="${IMAGE:-prometheus-biomysterybench:latest}"
BLASTDB_DIR="${BLASTDB_DIR:-${HOME}/blastdb}"
DATE="$(date +%F)"

uv run --with huggingface_hub python -m prometheus_biomysterybench.biomystery \
  --models "$MODEL_ID" \
  --local-claude-model "$LOCAL_CLAUDE_MODEL" \
  --exec-image "$IMAGE" \
  --exec-blastdb "$BLASTDB_DIR" \
  --native-tools \
  --allow-network \
  --episodes 2 \
  --max-turns 40 \
  --max-tokens 8192 \
  --model-attempts 4 \
  --llm-timeout 300 \
  --command-timeout 900 \
  --task-timeout 3600 \
  --max-output-chars 65536 \
  --private-out ".eval_results_private/biomystery_preview_localopus48_${DATE}.json" \
  --public-out "results/biomystery_preview_localopus48_${DATE}.json" \
  "$@"
