#!/usr/bin/env bash
# Download pre-formatted NCBI BLAST databases into a persistent host directory
# using the biomystery container's BLAST tools (no host BLAST install needed).
#
# Usage:
#   BLASTDB_DIR=/Users/jperla/blastdb scripts/setup_blastdb.sh                # small DBs
#   BLASTDB_DIR=/Volumes/BLASTDB scripts/setup_blastdb.sh refseq_protein      # large DB (needs the disk)
#
# Env:
#   BLASTDB_DIR   target directory for the DBs (default: ${HOME}/blastdb)
#   IMAGE         biomystery image to use (default: prometheus-biomysterybench:latest)
#   MIN_FREE_GB   refuse refseq_protein/nr/nt unless this many GB are free (default: 400)
#   FORCE_BIG     set to 1 to bypass the large-DB disk guard
set -euo pipefail

BLASTDB_DIR="${BLASTDB_DIR:-${HOME}/blastdb}"
IMAGE="${IMAGE:-prometheus-biomysterybench:latest}"
MIN_FREE_GB="${MIN_FREE_GB:-400}"
FORCE_BIG="${FORCE_BIG:-0}"

DBS=("$@")
if [[ ${#DBS[@]} -eq 0 ]]; then
  DBS=(taxdb swissprot pdbaa pdbnt)
fi

# Guard against pulling huge DBs onto a disk that cannot hold them.
BIG_DBS_REGEX='^(refseq_protein|nr|nt|refseq_rna|env_nr|env_nt)$'
wants_big=0
for db in "${DBS[@]}"; do
  if [[ "$db" =~ $BIG_DBS_REGEX ]]; then wants_big=1; fi
done
if [[ "$wants_big" -eq 1 && "$FORCE_BIG" != "1" ]]; then
  mkdir -p "$BLASTDB_DIR"
  free_kb=$(df -Pk "$BLASTDB_DIR" | awk 'NR==2 {print $4}')
  free_gb=$(( free_kb / 1024 / 1024 ))
  if [[ "$free_gb" -lt "$MIN_FREE_GB" ]]; then
    echo "Refusing to download large BLAST DBs: only ${free_gb} GB free at ${BLASTDB_DIR}," >&2
    echo "need >= ${MIN_FREE_GB} GB. Point BLASTDB_DIR at the 8TB NAS SSD, or set FORCE_BIG=1." >&2
    exit 3
  fi
fi

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "Image '$IMAGE' not found. Build it first: scripts/build_biomystery_env.sh" >&2
  exit 2
fi

mkdir -p "$BLASTDB_DIR"
echo "Downloading BLAST DBs into ${BLASTDB_DIR}: ${DBS[*]}"

# `ln -sf` covers older images built before the Dockerfile baked in the curl fix;
# bioconda's update_blastdb.pl only probes /usr/bin and /usr/local/bin for curl.
docker run --rm \
  -v "${BLASTDB_DIR}:/blastdb" \
  -w /blastdb \
  "$IMAGE" \
  bash -lc 'ln -sf /opt/conda/bin/curl /usr/bin/curl 2>/dev/null || true; \
            update_blastdb.pl --decompress '"${DBS[*]}"

echo
echo "Done. Databases now in ${BLASTDB_DIR}:"
docker run --rm -v "${BLASTDB_DIR}:/blastdb:ro" -e BLASTDB=/blastdb "$IMAGE" \
  bash -lc 'cd /blastdb && blastdbcmd -list /blastdb 2>/dev/null || ls -1 /blastdb | sed -E "s/\.[^.]+$//" | sort -u'
