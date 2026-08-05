#!/usr/bin/env bash
# Install or upgrade Airflow on k3s, and load the baked DAG image.
#
#   KUBECONFIG=~/.kube/evalgate.yaml \
#   EVALGATE_NODE_SSH="ssh -i ~/.ssh/evalgate_ed25519 ubuntu@<node>" \
#     ./infra/k8s/airflow/apply.sh
#
# helm runs ON THE NODE, same as the monitoring stack. Release state lives in
# the cluster as secrets, so the client's location does not change the result.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NS=airflow
RELEASE=airflow
CHART_VERSION=1.22.0          # appVersion 3.2.2. Pinned exactly, never a range.
IMAGE="${EVALGATE_AIRFLOW_IMAGE:-evalgate-airflow:3.2.2-dags}"

log() { printf '[airflow-apply] %s\n' "$*"; }

kubectl version -o json >/dev/null 2>&1 || {
  log "FATAL: cannot reach the cluster. Is the SSH tunnel up, and KUBECONFIG set?"
  exit 1
}

[ -n "${EVALGATE_NODE_SSH:-}" ] || {
  log "FATAL: set EVALGATE_NODE_SSH, e.g."
  log '  EVALGATE_NODE_SSH="ssh -i ~/.ssh/evalgate_ed25519 ubuntu@<node-ip>"'
  exit 1
}

# The committed manifest, not `kubectl create namespace --dry-run`. Creating it
# bare here and applying the labelled manifest elsewhere makes the two overwrite
# each other's last-applied-configuration on every run — the exact churn that
# made `namespace/evalgate configured` appear forever in up.sh.
kubectl apply -f "${HERE}/00-namespace.yaml" >/dev/null

# The shared log volume, created here rather than by the chart: the chart's PVC
# template hardcodes ReadWriteMany and this cluster has no RWX class. See
# 10-logs-pvc.yaml for the full reasoning.
#
# Deliberately NOT gated on Bound. local-path is WaitForFirstConsumer — it has
# to be, since a node-local provisioner cannot pick a directory before a
# consumer is scheduled — so this claim stays Pending until helm creates the
# first Airflow pod. Waiting here would block forever on the expected state.
kubectl apply -f "${HERE}/10-logs-pvc.yaml" >/dev/null

# Cross-namespace pod launching for the daily eval DAG. The chart binds its
# pod-launcher Role in the `airflow` namespace only, and the DAG's pods run in
# `evalgate`. Applied here rather than by hand because the failure it prevents is
# a 403 at 03:00 on a night nobody is watching.
kubectl apply -f "${HERE}/20-evalgate-pod-rbac.yaml" >/dev/null

# --- secrets: checked, never generated ----------------------------------------
#
# Same rule as postgres-credentials. Three of these are not merely hygiene: the
# chart regenerates jwt, api-secret-key and fernet-key on every helm upgrade if
# they are unset, which breaks running DAGs and makes already-stored connections
# undecryptable. Pinning them is what makes `helm upgrade` a no-op.
MISSING=0
for s in airflow-metadata airflow-jwt airflow-api-secret-key airflow-fernet-key airflow-admin; do
  kubectl -n "$NS" get secret "$s" >/dev/null 2>&1 || { log "MISSING secret/$s"; MISSING=1; }
done
if [ "$MISSING" = 1 ]; then
  log "FATAL: create the missing secrets out-of-band first — see infra/k8s/README.md."
  log "Nothing was applied."
  exit 1
fi

# --- image --------------------------------------------------------------------
# There is no registry, so imagePullPolicy IfNotPresent means kubelet uses what
# is already in containerd and never fetches. Checking here turns an
# ErrImageNeverPull several minutes from now into an immediate, named failure.
log "Checking ${IMAGE} exists in containerd namespace k8s.io."
# shellcheck disable=SC2029
${EVALGATE_NODE_SSH} "sudo k3s ctr -n k8s.io images list -q | grep -qx 'docker.io/library/${IMAGE}'" || {
  log "FATAL: ${IMAGE} is not in containerd's k8s.io namespace on the node."
  log "Build it there first: ./infra/k8s/airflow/build-on-node.sh"
  exit 1
}

log "Copying values.yaml to the node."
# shellcheck disable=SC2086
${EVALGATE_NODE_SSH} "cat > /tmp/airflow-values.yaml" < "${HERE}/values.yaml"

log "helm upgrade --install ${RELEASE} (chart ${CHART_VERSION}, appVersion 3.2.2)."
# shellcheck disable=SC2086
${EVALGATE_NODE_SSH} "sudo env KUBECONFIG=/etc/rancher/k3s/k3s.yaml helm repo add apache-airflow https://airflow.apache.org >/dev/null 2>&1 || true"
# shellcheck disable=SC2086
${EVALGATE_NODE_SSH} "sudo env KUBECONFIG=/etc/rancher/k3s/k3s.yaml helm repo update >/dev/null"
# shellcheck disable=SC2086
${EVALGATE_NODE_SSH} "sudo env KUBECONFIG=/etc/rancher/k3s/k3s.yaml helm upgrade --install ${RELEASE} apache-airflow/airflow \
  --version ${CHART_VERSION} --namespace ${NS} --values /tmp/airflow-values.yaml --wait --timeout 15m"

log "Done."
kubectl -n "$NS" get pods
