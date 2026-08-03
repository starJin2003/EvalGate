#!/usr/bin/env bash
# Install helm on the k3s node. Run once, over SSH.
#
#   scp -i ~/.ssh/evalgate_ed25519 infra/k8s/monitoring/node-helm-setup.sh ubuntu@<node>:/tmp/
#   ssh -i ~/.ssh/evalgate_ed25519 ubuntu@<node> 'sudo bash /tmp/node-helm-setup.sh'
#
# helm is a client: it renders charts and applies manifests, and release state
# lives in the cluster as secrets rather than on disk, so where the client runs
# does not affect the result. It runs on the node anyway, for the same reason
# the image build does — the dev machine is an M1 with a live training run on it
# and nothing avoidable belongs there.

set -euo pipefail

# Deliberately the 3.x line, not the current 4.2.3. kube-prometheus-stack and
# the Airflow chart that follows in P3 are both developed and tested against
# helm 3, and this node is not the place to find out which chart hooks helm 4
# changed. Revisit when the charts declare helm 4 support.
HELM_VERSION="${HELM_VERSION:-3.21.3}"
ARCH=arm64

log() { printf '[node-helm-setup] %s\n' "$*"; }

[ "$(uname -m)" = "aarch64" ] || { log "FATAL: expected aarch64, got $(uname -m)."; exit 1; }

if command -v helm >/dev/null 2>&1; then
  log "helm already installed: $(helm version --short)"
  exit 0
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

log "Installing helm ${HELM_VERSION} (${ARCH})."
curl -fsSL -o "$tmp/helm.tgz" "https://get.helm.sh/helm-v${HELM_VERSION}-linux-${ARCH}.tar.gz"
tar -C "$tmp" -xzf "$tmp/helm.tgz"
install -m 0755 "$tmp/linux-${ARCH}/helm" /usr/local/bin/helm

log "Done."
helm version --short
