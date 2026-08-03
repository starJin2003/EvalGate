#!/usr/bin/env bash
# Upload the quantized GGUF to the node and PROVE it arrived intact.
# Run from the repo root on the workstation:
#
#   ./infra/k8s/model/upload-weights.sh
#
# The expected digest, byte count and filename all come from
# training/artifacts/p13/gguf_manifest.json. They are not repeated here: a
# second copy of a hash is a second thing to forget to update, and the manifest
# is the committed artifact that names which checkpoint these weights are.
#
# Transfer flags, each for a reason:
#   --partial --inplace   a dropped 1 GB upload resumes instead of restarting
#   --progress            a silent 10-minute transfer is indistinguishable from a hang
#   no -z                 Q4_K_M is already compressed; -z burns CPU on both
#                         ends for nothing, and one end is a 4-OCPU node
#
# The verification is a GATE, not a report. On a digest mismatch the partial
# file is DELETED before exiting non-zero. A truncated 1 GB GGUF still loads and
# still answers - it just answers wrong - so leaving one on disk is how it ends
# up being served. The Deployment re-checks the same digest in an initContainer
# on every pod start, because this script runs once and a disk lives for months.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${HERE}/../../.." && pwd)"

MANIFEST="${REPO_ROOT}/training/artifacts/p13/gguf_manifest.json"
LOCAL_DIR="${REPO_ROOT}/training/.scratch"
REMOTE_DIR=/var/lib/evalgate/models

log() { printf '[upload-weights] %s\n' "$*"; }

[ -f "$MANIFEST" ] || { log "FATAL: ${MANIFEST} not found."; exit 1; }

read -r ARTIFACT EXPECTED_SHA EXPECTED_SIZE <<EOF
$(python3 -c "
import json, sys
m = json.load(open(sys.argv[1]))
print(m['artifact'], m['sha256'], m['size_bytes'])
" "$MANIFEST")
EOF

LOCAL_FILE="${LOCAL_DIR}/${ARTIFACT}"

log "artifact  ${ARTIFACT}"
log "sha256    ${EXPECTED_SHA}"
log "size      ${EXPECTED_SIZE} bytes"

[ -f "$LOCAL_FILE" ] || {
  log "FATAL: ${LOCAL_FILE} not found."
  log "The GGUF is gitignored scratch. Rebuild it with the P1.3 pipeline."
  exit 1
}

if [ -z "${EVALGATE_NODE_SSH:-}" ]; then
  NODE_SSH="$(cd "${REPO_ROOT}/infra/terraform" && AWS_REQUEST_CHECKSUM_CALCULATION=when_required terraform output -raw ssh_command 2>/dev/null)" || true
  [ -n "${NODE_SSH:-}" ] || {
    log "FATAL: EVALGATE_NODE_SSH is unset and 'terraform output ssh_command' failed."
    exit 1
  }
  NODE_SSH="${NODE_SSH//\~/$HOME}"
else
  NODE_SSH="${EVALGATE_NODE_SSH}"
fi

# rsync needs the ssh invocation and the destination split apart; the terraform
# output is a single "ssh -i KEY user@host" string.
SSH_CMD="${NODE_SSH% *}"
NODE_HOST="${NODE_SSH##* }"

# Verify the local copy first. Uploading a file that is already wrong wastes
# ten minutes and then blames the network.
log "Verifying the local copy before sending it."
LOCAL_SIZE=$(wc -c < "$LOCAL_FILE" | tr -d ' ')
[ "$LOCAL_SIZE" = "$EXPECTED_SIZE" ] || {
  log "FATAL: local size ${LOCAL_SIZE} != manifest ${EXPECTED_SIZE}."
  exit 1
}
LOCAL_SHA=$(shasum -a 256 "$LOCAL_FILE" | awk '{print $1}')
[ "$LOCAL_SHA" = "$EXPECTED_SHA" ] || {
  log "FATAL: local sha256 ${LOCAL_SHA} != manifest ${EXPECTED_SHA}."
  exit 1
}
log "Local copy matches the manifest."

log "Transferring to ${NODE_HOST}:${REMOTE_DIR}/ ..."
TRANSFER_START=$(date +%s)
rsync --partial --inplace --progress \
  -e "${SSH_CMD} -o BatchMode=yes" \
  "$LOCAL_FILE" "${NODE_HOST}:${REMOTE_DIR}/"
TRANSFER_END=$(date +%s)
TRANSFER_S=$((TRANSFER_END - TRANSFER_START))
log "Transfer wall time: ${TRANSFER_S}s"
if [ "$TRANSFER_S" -gt 0 ]; then
  log "Throughput: $(python3 -c "print(f'{${EXPECTED_SIZE}/${TRANSFER_S}/1e6:.1f} MB/s  ({${EXPECTED_SIZE}*8/${TRANSFER_S}/1e6:.1f} Mbit/s)')" 2>/dev/null || echo 'n/a')"
fi

log "Verifying on the node. A truncated upload that still loads is the failure this catches."
# shellcheck disable=SC2029  # every variable here is meant to expand locally
$NODE_SSH "set -euo pipefail
  f=${REMOTE_DIR}/${ARTIFACT}
  actual_size=\$(stat -c %s \"\$f\")
  if [ \"\$actual_size\" != '${EXPECTED_SIZE}' ]; then
    echo \"FATAL: size on node \$actual_size != expected ${EXPECTED_SIZE}. Deleting the partial file.\" >&2
    rm -f \"\$f\"
    exit 1
  fi
  actual_sha=\$(sha256sum \"\$f\" | awk '{print \$1}')
  if [ \"\$actual_sha\" != '${EXPECTED_SHA}' ]; then
    echo \"FATAL: sha256 on node \$actual_sha != expected ${EXPECTED_SHA}. Deleting the corrupt file.\" >&2
    rm -f \"\$f\"
    exit 1
  fi
  chmod 444 \"\$f\"
  echo \"verified  \$actual_sha\"
  echo \"size      \$actual_size bytes\"
  ls -l \"\$f\"
"

log "Weights are on the node and the digest matches."
