#!/usr/bin/env bash
# Run the BioMysteryBench preview harness fully inside the biomystery container
# (model calls go to the remote API). Mounts local BLAST DBs when BLASTDB_DIR is set.
#
# Env:
#   PROMETHEUSBENCH_API_KEY   required (remote model API key)
#   BLASTDB_DIR               optional host BLAST DB dir, mounted read-only at /blastdb
set -euo pipefail

if [[ -z "${PROMETHEUSBENCH_API_KEY:-}" ]]; then
  echo "PROMETHEUSBENCH_API_KEY must be set" >&2
  exit 2
fi

cd "$(dirname "$0")/.."

mount_args=()
if [[ -n "${BLASTDB_DIR:-}" ]]; then
  mount_args+=( -v "${BLASTDB_DIR}:/blastdb:ro" -e BLASTDB=/blastdb )
fi

docker run --rm \
  -e PROMETHEUSBENCH_API_KEY \
  -v "$PWD:/workspace" \
  -w /workspace \
  "${mount_args[@]}" \
  prometheus-biomysterybench:latest \
  bash -lc 'python -m pip install --root-user-action=ignore -e . huggingface_hub >/tmp/prometheusbench-pip.log && python -m prometheus_biomysterybench.biomystery "$@"' \
  bash "$@"
