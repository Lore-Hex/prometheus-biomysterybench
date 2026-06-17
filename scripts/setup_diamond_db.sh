#!/usr/bin/env bash
# Build a DIAMOND protein database for fast local protein search.
#
# Why DIAMOND: blastp against a large protein set (nr / refseq_protein) is slow
# enough that models time out, which is why the big BLAST DB was dropped. DIAMOND
# searches the same data orders of magnitude faster, so the model can do real
# organism/protein identification locally instead of falling back to remote BLAST.
#
# Strategy (matters): the source BLAST volumes are pulled from NCBI's AWS Open
# Data mirror (s3://ncbi-blast-databases), which is ~50x faster than NCBI FTP and
# supports parallel/resumable transfer. They are staged on the ARCHIVE dir (point
# this at the NAS — large, slow, fine for a sequential makedb read). The finished
# .dmnd is written to the OUT dir, which MUST be local SSD: DIAMOND memory-maps
# the db with random access at query time, and a network/SMB mount would be
# slower than the remote BLAST we are trying to avoid.
#
# Organism names come from the sequence deflines (nr/refseq titles carry
# "[Organism]"), so no NCBI taxonomy-map download is needed; query with
#   diamond blastp -d nr -q q.faa --outfmt 6 qseqid sseqid pident evalue stitle
#
# Usage:
#   ARCHIVE=/Users/jperla/nas/datasets/ncbi OUT=/Users/jperla/blastdb \
#     scripts/setup_diamond_db.sh refseq_protein
#   scripts/setup_diamond_db.sh nr            # bigger, ~same build path
#
# Env:
#   DB         protein BLAST db to convert (default: refseq_protein); also $1
#   ARCHIVE    where the source BLAST volumes are synced (default: $HOME/nas/datasets/ncbi)
#   OUT        local-SSD dir for the .dmnd, mounted at /blastdb by the harness (default: $HOME/blastdb)
#   IMAGE      biomystery image (default: prometheus-biomysterybench:latest)
#   SNAPSHOT   S3 snapshot dir (default: auto-detect newest)
#   MIN_FREE_GB  refuse the build unless this many GB free on OUT (default: 250)
set -euo pipefail

DB="${1:-${DB:-refseq_protein}}"
ARCHIVE="${ARCHIVE:-${HOME}/nas/datasets/ncbi}"
OUT="${OUT:-${HOME}/blastdb}"
IMAGE="${IMAGE:-prometheus-biomysterybench:latest}"
MIN_FREE_GB="${MIN_FREE_GB:-250}"
BUCKET="s3://ncbi-blast-databases"

command -v aws >/dev/null || { echo "need the aws CLI (brew install awscli)" >&2; exit 2; }
docker image inspect "$IMAGE" >/dev/null 2>&1 || { echo "build the image first: scripts/build_biomystery_env.sh" >&2; exit 2; }

SNAPSHOT="${SNAPSHOT:-$(aws s3 ls --no-sign-request "${BUCKET}/" | awk '/PRE [0-9]{4}-/{print $2}' | sort | tail -1)}"
SNAPSHOT="${SNAPSHOT%/}"
echo "snapshot: ${SNAPSHOT}  db: ${DB}  archive: ${ARCHIVE}  out: ${OUT}"

# Faster, resumable parallel transfer.
aws configure set default.s3.max_concurrent_requests 24 >/dev/null 2>&1 || true

mkdir -p "${ARCHIVE}/${DB}"
echo "[$(date +%H:%M:%S)] syncing ${DB} BLAST volumes from AWS mirror -> ${ARCHIVE}/${DB}"
aws s3 sync --no-sign-request \
  --exclude "*" --include "${DB}.*" \
  "${BUCKET}/${SNAPSHOT}/" "${ARCHIVE}/${DB}/"
# taxdb gives blastdbcmd readable organism strings during extraction
aws s3 sync --no-sign-request \
  --exclude "*" --include "taxdb.*" \
  "${BUCKET}/${SNAPSHOT}/" "${ARCHIVE}/${DB}/" || true

free_gb=$(( $(df -Pk "$OUT" | awk 'NR==2{print $4}') / 1024 / 1024 ))
echo "[$(date +%H:%M:%S)] local SSD free at ${OUT}: ${free_gb} GB (need >= ${MIN_FREE_GB})"
[ "$free_gb" -ge "$MIN_FREE_GB" ] || { echo "ABORT: not enough local SSD free for ${DB}.dmnd" >&2; exit 3; }

mkdir -p "$OUT"
NCPU=$(getconf _NPROCESSORS_ONLN 2>/dev/null || sysctl -n hw.ncpu)
echo "[$(date +%H:%M:%S)] blastdbcmd -> diamond makedb -> ${OUT}/${DB}.dmnd (${NCPU} threads)"
docker run --rm \
  -v "${ARCHIVE}/${DB}":/src:ro -e BLASTDB=/src \
  -v "${OUT}":/out \
  "$IMAGE" bash -lc "
    set -e -o pipefail
    blastdbcmd -db /src/${DB} -entry all 2>/dev/null \
      | diamond makedb --db /out/${DB} --threads ${NCPU}
    ls -la /out/${DB}.dmnd
  "

echo "[$(date +%H:%M:%S)] verify: diamond blastp (human hemoglobin beta) vs ${DB}"
docker run --rm -v "${OUT}":/blastdb:ro -e BLASTDB=/blastdb -w /tmp "$IMAGE" bash -lc "
  printf '>q\nMVHLTPEEKSAVTALWGKVNVDEVGGEALGRLLVVYPWTQRFFESFGDLSTPDAVMGNPKVKAHGKKVLGAFSDGLAHLDNLKGTFATLSELHCDKLHVDPENFRLLGNVLVCVLAHHFGKEFTPPVQAAYQKVVAGVANALAHKYH\n' > q.faa
  diamond blastp -d /blastdb/${DB} -q q.faa \
    --outfmt 6 qseqid sseqid pident evalue stitle --max-target-seqs 3 --quiet 2>/dev/null | head -3
"
echo "[$(date +%H:%M:%S)] DONE: ${OUT}/${DB}.dmnd ready (source archived at ${ARCHIVE}/${DB})"
