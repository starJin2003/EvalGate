#!/usr/bin/env bash
# Build the Airflow+DAGs image ON the node, natively for arm64.
#
#   ./infra/k8s/airflow/build-on-node.sh [tag]
#
# Same pattern as infra/k8s/api/build-on-node.sh, proven three times: stream the
# build context over SSH as a tar, build with BuildKit's containerd worker
# straight into k3s's k8s.io namespace. No registry, no buildx, no QEMU, and
# nothing built on the operator's machine.

set -euo pipefail

TAG="${1:-evalgate-airflow:3.2.2-dags}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REMOTE_DIR=/home/ubuntu/build/evalgate-airflow

log() { printf '[airflow-build] %s\n' "$*"; }

if [ -z "${EVALGATE_NODE_SSH:-}" ]; then
  NODE_SSH="$(cd "${HERE}/../../terraform" && AWS_REQUEST_CHECKSUM_CALCULATION=when_required terraform output -raw ssh_command 2>/dev/null)" || true
  [ -n "${NODE_SSH:-}" ] || {
    log "FATAL: EVALGATE_NODE_SSH is unset and 'terraform output ssh_command' failed."
    exit 1
  }
  NODE_SSH="${NODE_SSH//\~/$HOME}"
else
  NODE_SSH="${EVALGATE_NODE_SSH}"
fi

log "Streaming build context to the node."
# shellcheck disable=SC2086
$NODE_SSH "rm -rf ${REMOTE_DIR} && mkdir -p ${REMOTE_DIR}"
# COPYFILE_DISABLE=1 and the ._* exclusion are not tidiness. macOS bsdtar writes
# any file carrying extended attributes as a second AppleDouble member named
# `._<file>`, so `dags/evalgate_daily_eval.py` shipped an extra
# `dags/._evalgate_daily_eval.py` into the image. It ends in `.py`, so Airflow's
# dag-processor parses it, fails on the binary content, and reports a permanent
# import error next to a DAG that is in fact fine — measured on the first build
# of this image.
COPYFILE_DISABLE=1 tar --exclude='__pycache__' --exclude='*.pyc' --exclude='._*' \
  -czf - -C "$HERE" Dockerfile dags \
  | $NODE_SSH "tar -xzf - -C ${REMOTE_DIR}"

log "Building ${TAG} on the node (foreground)."
# --namespace k8s.io is load-bearing: containerd namespaces are hard isolation
# and kubelet looks only in k8s.io. An image built into `default` is invisible
# to k3s while `ctr images list` still shows it.
# shellcheck disable=SC2086
$NODE_SSH "cd ${REMOTE_DIR} && sudo nerdctl --address /run/k3s/containerd/containerd.sock --namespace k8s.io build -t ${TAG} ."

log "Verifying the image and its architecture."
# shellcheck disable=SC2086
$NODE_SSH "sudo nerdctl --address /run/k3s/containerd/containerd.sock --namespace k8s.io images | grep -E 'REPOSITORY|${TAG%%:*}'"
