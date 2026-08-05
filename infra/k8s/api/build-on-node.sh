#!/usr/bin/env bash
# Build the API image ON the k3s node, natively for arm64. Run from the repo
# root on the workstation:
#
#   ./infra/k8s/api/build-on-node.sh [tag]
#
# The build never runs locally. The dev machine is an M1 with a live training
# run on it, and an image build is exactly the kind of memory and IO spike that
# has already cost this project one run. The node is also aarch64, so building
# there is native for its own architecture — no buildx, no QEMU, no emulation.
#
# The build context is streamed over SSH as a tar rather than cloned or rsynced:
# no git on the node, no credentials on the node, and a context that carries
# precisely the files the Dockerfile needs.

set -euo pipefail

TAG="${1:-evalgate-api:0.1.0}"
REMOTE_DIR=/home/ubuntu/build/evalgate-api

log() { printf '[build-on-node] %s\n' "$*"; }

[ -f pyproject.toml ] || { log "FATAL: run this from the repo root."; exit 1; }

# Derived from terraform rather than defaulted to a literal address. A
# hardcoded IP here works on exactly one machine and silently targets the wrong
# host — or nothing — from a fresh clone.
if [ -z "${EVALGATE_NODE_SSH:-}" ]; then
  NODE_SSH="$(cd infra/terraform && AWS_REQUEST_CHECKSUM_CALCULATION=when_required terraform output -raw ssh_command 2>/dev/null)" || true
  [ -n "${NODE_SSH:-}" ] || {
    log "FATAL: EVALGATE_NODE_SSH is unset and 'terraform output ssh_command' failed."
    log 'Set it explicitly:  EVALGATE_NODE_SSH="ssh -i ~/.ssh/evalgate_ed25519 ubuntu@<node-ip>"'
    exit 1
  }
  # terraform emits a literal ~, which does not expand out of a variable.
  NODE_SSH="${NODE_SSH//\~/$HOME}"
else
  NODE_SSH="${EVALGATE_NODE_SSH}"
fi

# Exactly what apps/api/Dockerfile COPYs, and nothing else. training/.scratch is
# 9.5 GB and training/artifacts is 244 MB and being written to by the live run;
# neither belongs anywhere near a build context.
CONTEXT_PATHS=(
  pyproject.toml
  uv.lock
  .python-version
  .dockerignore
  apps/api
  packages/evalcore
  packages/sdk/pyproject.toml
  training/pyproject.toml
)

log "Streaming build context to the node."
# shellcheck disable=SC2086
$NODE_SSH "rm -rf ${REMOTE_DIR} && mkdir -p ${REMOTE_DIR}"
# COPYFILE_DISABLE=1 plus the ._* exclusion: macOS bsdtar emits an AppleDouble
# `._<file>` member for every file with extended attributes. Harmless here, but
# the same tar line in the airflow build shipped a `._*.py` into the DAG folder
# and gave the dag-processor a permanent import error. Same fix, both places.
COPYFILE_DISABLE=1 tar --exclude='__pycache__' --exclude='*.pyc' --exclude='._*' \
  -czf - "${CONTEXT_PATHS[@]}" \
  | $NODE_SSH "tar -xzf - -C ${REMOTE_DIR}"

log "Building ${TAG} on the node (foreground)."
# --namespace k8s.io is the whole point: containerd namespaces are hard
# isolation and kubelet only looks in k8s.io. An image built into the default
# namespace is invisible to k3s while `ctr images list` still shows it.
$NODE_SSH "cd ${REMOTE_DIR} && sudo nerdctl --address /run/k3s/containerd/containerd.sock --namespace k8s.io build -f apps/api/Dockerfile -t ${TAG} ."

log "Verifying the image landed in the k8s.io namespace."
$NODE_SSH "sudo k3s ctr -n k8s.io images list | grep '${TAG%%:*}'"
