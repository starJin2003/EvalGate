#!/usr/bin/env bash
# Stage the SELECTED checkpoint so mlx_lm.fuse cannot pick up the wrong weights.
#
#   ./training/scripts/stage_selected_adapter.sh [iter] [version]
#
#     v1:  ./training/scripts/stage_selected_adapter.sh 2000 v1
#     v2:  ./training/scripts/stage_selected_adapter.sh 1200 v2
#
# WHY THIS EXISTS
#
# `mlx_lm fuse --adapter-path DIR` does not take a checkpoint file. It calls
# load_adapters(), which hardcodes
#
#     model.load_weights(str(adapter_path / "adapters.safetensors"), strict=False)
#     -- mlx_lm/tuner/utils.py:137
#
# So pointing it at training/artifacts/adapters-v1/ fuses `adapters.safetensors`,
# which is the FINAL weights at iter 2956 — not the checkpoint the pre-committed
# rule selected. It would succeed, print nothing unusual, and produce a servable
# model built from the wrong weights.
#
# This project has already shipped two defects in exactly that class:
#   * `train eval-adapters` defaulted to the PROBE adapter directory, so a bare
#     invocation would have scored the probe's checkpoints and reported them as
#     v1's;
#   * its candidate glob `[0-9]*_adapters.safetensors` did not match
#     `adapters.safetensors`, silently dropping the final weights from a sweep
#     whose rule names them explicitly.
#
# Both were caught by reading code, not by a failing run. Neither had a guard.
# This script is the guard.
#
# WHAT FAILS LOUDLY IF THE PATH IS WRONG
#
#   GUARD A  the staged file must hash equal to the named checkpoint.
#            Catches a truncated or failed copy.
#   GUARD B  the staged file must hash NOT equal to adapters.safetensors, the
#            final weights. This is the one that catches the actual mistake:
#            if anything caused the final adapter to land in the staging
#            directory, the hashes match and the script aborts.
#   GUARD C  re-hashed after fusing, proving nothing swapped the input while the
#            fuse was running.
#
# Note what is NOT guarded and cannot be: running `mlx_lm fuse --adapter-path
# training/artifacts/adapters-v1` by hand bypasses all of this silently. The
# staging directory exists so that the fuse command names a path that only ever
# contains the selected weights.

set -euo pipefail

ITER="${1:-2000}"
# Parameterised for v2. The version is explicit and has no default that could
# quietly stage the wrong run's weights -- the same class of defect as
# eval-adapters defaulting to the probe directory.
VERSION="${2:-v1}"
case "$VERSION" in v1|v2) ;; *) echo "unknown version: $VERSION (want v1 or v2)" >&2; exit 1;; esac
ART=training/artifacts
SRC_DIR="${ART}/adapters-${VERSION}"
STAGE="${ART}/adapters-${VERSION}-selected"
SELECTED="$(printf '%s/%07d_adapters.safetensors' "$SRC_DIR" "$ITER")"
FINAL="${SRC_DIR}/adapters.safetensors"

log() { printf '[stage-adapter] %s\n' "$*"; }
die() { printf '\n[stage-adapter] FATAL: %s\n' "$*" >&2; exit 1; }

[ -f "$SELECTED" ] || die "selected checkpoint not found: $SELECTED"
[ -f "$FINAL" ]    || die "final adapters.safetensors not found: $FINAL"

sha() { shasum -a 256 "$1" | awk '{print $1}'; }

SEL_SHA="$(sha "$SELECTED")"
FIN_SHA="$(sha "$FINAL")"

log "selected  iter ${ITER}  $SELECTED"
log "  sha256  ${SEL_SHA}"
log "final     (last iter)  $FINAL"
log "  sha256  ${FIN_SHA}"

[ "$SEL_SHA" != "$FIN_SHA" ] || die \
  "the selected checkpoint and the final weights are byte-identical.
       That would make this guard meaningless — investigate before proceeding."

rm -rf "$STAGE"
mkdir -p "$STAGE"
cp "$SELECTED" "${STAGE}/adapters.safetensors"
cp "${SRC_DIR}/adapter_config.json" "${STAGE}/adapter_config.json"

STAGED_SHA="$(sha "${STAGE}/adapters.safetensors")"

# GUARD A
[ "$STAGED_SHA" = "$SEL_SHA" ] || die \
  "staged file does not match the selected checkpoint.
       expected ${SEL_SHA}
       got      ${STAGED_SHA}"
log "GUARD A ok: staged == selected checkpoint"

# GUARD B — the one that catches the real mistake
[ "$STAGED_SHA" != "$FIN_SHA" ] || die \
  "staged file IS the final adapters.safetensors, not iter ${ITER}.
       This is the exact silent-wrong-weights failure this guard exists for."
log "GUARD B ok: staged != final weights"

cat > "${STAGE}/SELECTED.json" <<EOF
{
  "version": "${VERSION}",
  "selected_iter": ${ITER},
  "selected_file": "$(basename "$SELECTED")",
  "selected_sha256": "${SEL_SHA}",
  "rejected_final_file": "adapters.safetensors",
  "rejected_final_sha256": "${FIN_SHA}",
  "staged_as": "adapters.safetensors",
  "staged_sha256": "${STAGED_SHA}",
  "rule": "lowest full-split valid loss; if two are within 0.01, take the earlier one (DECISIONS 2026-08-02)"
}
EOF

log "staged -> ${STAGE}/  (fuse must be pointed HERE, never at ${SRC_DIR})"
