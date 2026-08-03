#!/usr/bin/env bash
# Build the llama.cpp server image ON the node, natively for arm64.
#
#   ./infra/k8s/model/build-on-node.sh [tag]
#
# Same pattern as infra/k8s/api/build-on-node.sh and
# infra/k8s/airflow/build-on-node.sh, now proven five times: stream the build
# context over SSH as a tar, build with BuildKit's containerd worker straight
# into k3s's k8s.io namespace. No registry, no buildx, no QEMU, and nothing
# built on the M1.
#
# The context is the Dockerfile plus the vendored b10210 source tarball (35 MB)
# from gitignored training/.scratch/llamacpp/. That tarball is the same source
# that built the binaries behind the local 96-case baseline, which is what makes
# the local-vs-node comparison a hardware comparison rather than also a version
# comparison.

set -euo pipefail

TAG="${1:-evalgate-llama-server:b10210}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${HERE}/../../.." && pwd)"
SRC_TARBALL="${REPO_ROOT}/training/.scratch/llamacpp/src.tar.gz"
REMOTE_DIR=/home/ubuntu/build/evalgate-llama-server

log() { printf '[model-build] %s\n' "$*"; }

[ -f "$SRC_TARBALL" ] || {
  log "FATAL: ${SRC_TARBALL} not found."
  log "It is gitignored scratch. Re-fetch the b10210 source tarball - see README P1.3."
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

log "Streaming build context to the node (Dockerfile + 35 MB source tarball)."
# shellcheck disable=SC2086
$NODE_SSH "rm -rf ${REMOTE_DIR} && mkdir -p ${REMOTE_DIR}"
tar -czf - -C "$HERE" Dockerfile | $NODE_SSH "tar -xzf - -C ${REMOTE_DIR}"
# The tarball is already gzipped; piping it through tar -z again would spend CPU
# on both ends to make it slightly larger.
$NODE_SSH "cat > ${REMOTE_DIR}/src.tar.gz" < "$SRC_TARBALL"

log "Building ${TAG} on the node. Compiling llama.cpp on 4 OCPU takes a while."
BUILD_START=$(date +%s)
# --namespace k8s.io is load-bearing: containerd namespaces are hard isolation
# and kubelet looks only in k8s.io. An image built into `default` is invisible
# to k3s while `ctr images list` still shows it.
# shellcheck disable=SC2086
$NODE_SSH "cd ${REMOTE_DIR} && sudo nerdctl --address /run/k3s/containerd/containerd.sock --namespace k8s.io build -t ${TAG} ."
BUILD_END=$(date +%s)
log "Image build wall time: $((BUILD_END - BUILD_START))s"

log "Verifying the image and its architecture."
# shellcheck disable=SC2086
$NODE_SSH "sudo nerdctl --address /run/k3s/containerd/containerd.sock --namespace k8s.io images | grep -E 'REPOSITORY|${TAG%%:*}'
  echo '--- llama-server version, read out of the built image ---'
  # --net none because nerdctl's default bridge network needs the standard CNI
  # plugins in /opt/cni/bin, which this node does not have - k3s ships flannel
  # and its own CNI path. The container only has to print a version string, so
  # no network is the correct amount of network.
  sudo nerdctl --address /run/k3s/containerd/containerd.sock --namespace k8s.io run --rm --net none ${TAG} --version 2>&1 | head -5"
