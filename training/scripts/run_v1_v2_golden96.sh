#!/usr/bin/env bash
# The P1 finale: v1 vs v2 on the golden 96, both on THIS Mac, same backend,
# back to back.
#
#   ./training/scripts/run_v1_v2_golden96.sh
#
# WHY NOT ON THE NODE
#
# The OCI comparison measured what a backend change alone does to this suite:
# 0.004272 of suite score on byte-identical weights, from 79 of 96 answers
# diverging under greedy decoding across Metal and ggml CPU. Any v1-v2 result
# assembled across two backends carries that contamination in the same range as
# the effect being measured. Local is also 5.12 s/case against the node's 108.89,
# so both runs cost ~16 minutes instead of ~5.8 hours.
#
# Both servers get IDENTICAL flags. The only thing that differs between the two
# runs is the GGUF.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

SCRATCH=training/.scratch
OUT=training/artifacts/p14
SUITE=training/artifacts/p13/suite.json
PORT=8080

V1_GGUF="${SCRATCH}/evalgate-qwen3-1.7b-v1-iter2000.Q4_K_M.gguf"
V2_GGUF="${SCRATCH}/evalgate-qwen3-1.7b-v2-iter1200.Q4_K_M.gguf"

log() { printf '[golden96] %s\n' "$*"; }
die() { printf '\n[golden96] FATAL: %s\n' "$*" >&2; exit 1; }

for f in "$V1_GGUF" "$V2_GGUF" "$SUITE"; do
  [ -f "$f" ] || die "missing: $f"
done
mkdir -p "$OUT"

# Distinct weights, asserted rather than assumed: if a build step had silently
# produced the same file twice, every downstream number would be a comparison of
# a model with itself and would look like "no regression detected".
V1_SHA=$(shasum -a 256 "$V1_GGUF" | awk '{print $1}')
V2_SHA=$(shasum -a 256 "$V2_GGUF" | awk '{print $1}')
log "v1 gguf ${V1_SHA}"
log "v2 gguf ${V2_SHA}"
[ "$V1_SHA" != "$V2_SHA" ] || die "the two GGUFs are byte-identical"

stop_server() {
  pkill -f "llama-server .*--port ${PORT}" 2>/dev/null || true
  for _ in $(seq 1 30); do
    curl -sf --max-time 2 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1 || return 0
    sleep 1
  done
  die "a server is still listening on ${PORT}"
}
trap stop_server EXIT

run_one() {
  local version="$1" gguf="$2" model_version="$3"
  log "=== ${version} ==="
  stop_server

  # --cache-ram 2048: bounded explicitly on BOTH runs. The default is 8192 MiB,
  # which is what OOM-killed the node's server, and this machine has 16 GB total.
  # A cached KV entry is bit-identical to a recomputed one, so this changes speed
  # and memory, never output -- and it is the same on both runs regardless.
  nohup llama-server -m "$gguf" -c 8192 --parallel 1 --cache-ram 2048 \
    --host 127.0.0.1 --port "$PORT" \
    > "${SCRATCH}/llama-server-${version}.log" 2>&1 &

  log "waiting for the model to load"
  local ready=0
  for _ in $(seq 1 180); do
    if curl -sf --max-time 2 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
      ready=1; break
    fi
    sleep 1
  done
  [ "$ready" = 1 ] || die "${version} server never became healthy"

  # Prove WHICH weights are loaded before scoring anything.
  local loaded
  loaded=$(curl -sS "http://127.0.0.1:${PORT}/props" | python3 -c 'import json,sys; print(json.load(sys.stdin)["model_path"])')
  log "serving ${loaded}"
  case "$loaded" in *"$(basename "$gguf")") ;; *) die "server is serving ${loaded}, expected $(basename "$gguf")";; esac

  local started=$(date +%s)
  uv run evalgate-eval run \
    --suite "$SUITE" \
    --out "${OUT}/run_${version}.json" \
    --progress-jsonl "${OUT}/run_${version}.progress.jsonl" \
    --server-url "http://127.0.0.1:${PORT}" \
    --model-version "$model_version" \
    --quantization Q4_K_M \
    --run-id "${version}-golden96" \
    --judge \
    --timeout-s 300
  log "${version} wall time: $(( $(date +%s) - started ))s"
  stop_server
}

run_one v1 "$V1_GGUF" v1-iter2000
run_one v2 "$V2_GGUF" v2-iter1200

log "done -> ${OUT}/run_v1.json  ${OUT}/run_v2.json"
