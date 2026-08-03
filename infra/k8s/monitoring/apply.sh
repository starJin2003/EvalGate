#!/usr/bin/env bash
# Install or upgrade kube-prometheus-stack and load the committed dashboard.
# Run from the repo root with the SSH tunnel up.
#
#   KUBECONFIG=~/.kube/evalgate.yaml \
#   EVALGATE_NODE_SSH="ssh -i ~/.ssh/evalgate_ed25519 ubuntu@<node>" \
#     ./infra/k8s/monitoring/apply.sh
#
# helm runs ON THE NODE. It is only a client — release state lives in the
# cluster as secrets, so where it runs does not change the result — but the dev
# machine has a live training run on it and nothing avoidable belongs there.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NS=monitoring
RELEASE=monitoring
CHART_VERSION=88.1.3

log() { printf '[monitoring-apply] %s\n' "$*"; }

kubectl version -o json >/dev/null 2>&1 || {
  log "FATAL: cannot reach the cluster. Is the SSH tunnel up, and KUBECONFIG set?"
  exit 1
}

[ -n "${EVALGATE_NODE_SSH:-}" ] || {
  log "FATAL: set EVALGATE_NODE_SSH, e.g."
  log '  EVALGATE_NODE_SSH="ssh -i ~/.ssh/evalgate_ed25519 ubuntu@<node-ip>"'
  exit 1
}

kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f -

# Out-of-band, same rule as the Postgres password and the API token: this repo
# is public, and a credential in a values file is a credential in every fork.
kubectl -n "$NS" get secret grafana-admin >/dev/null 2>&1 || {
  log "FATAL: secret/grafana-admin is missing in namespace ${NS}."
  log "Create it first — see infra/k8s/README.md."
  exit 1
}

log "Copying values.yaml to the node."
# shellcheck disable=SC2086
${EVALGATE_NODE_SSH} "cat > /tmp/monitoring-values.yaml" < "${HERE}/values.yaml"

log "helm upgrade --install ${RELEASE} (chart ${CHART_VERSION}), on the node, foreground."
# shellcheck disable=SC2086
${EVALGATE_NODE_SSH} "sudo env KUBECONFIG=/etc/rancher/k3s/k3s.yaml helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true"
# shellcheck disable=SC2086
${EVALGATE_NODE_SSH} "sudo env KUBECONFIG=/etc/rancher/k3s/k3s.yaml helm repo update >/dev/null"
# shellcheck disable=SC2086
${EVALGATE_NODE_SSH} "sudo env KUBECONFIG=/etc/rancher/k3s/k3s.yaml helm upgrade --install ${RELEASE} prometheus-community/kube-prometheus-stack \
  --version ${CHART_VERSION} --namespace ${NS} --values /tmp/monitoring-values.yaml --wait --timeout 10m"

# Generated from the committed JSON rather than pasted into a chart value, so
# the file on disk is the single source. The label is what the Grafana sidecar
# watches for.
log "Loading the committed dashboard."
kubectl -n "$NS" create configmap evalgate-dashboard \
  --from-file="${HERE}/dashboards/" \
  --dry-run=client -o yaml \
  | kubectl label --local -f - grafana_dashboard=1 -o yaml \
  | kubectl apply -f -

log "Done."
kubectl -n "$NS" get pods
