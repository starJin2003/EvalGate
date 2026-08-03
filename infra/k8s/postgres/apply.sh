#!/usr/bin/env bash
# Apply the Postgres stack to k3s. Run from the repo root with the SSH tunnel
# up (see `terraform output kubectl_tunnel_command`) and KUBECONFIG pointed at
# ~/.kube/evalgate.yaml.
#
#   KUBECONFIG=~/.kube/evalgate.yaml ./infra/k8s/postgres/apply.sh
#
# Preconditions this script checks rather than assumes:
#   - the cluster is reachable
#   - the postgres-credentials secret already exists (it is created
#     out-of-band; see infra/k8s/README.md)
#   - /mnt/pgdata is mounted on the node (node-volume-setup.sh has run)

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${HERE}/../../.." && pwd)"
NS=evalgate

log() { printf '[postgres-apply] %s\n' "$*"; }

kubectl version -o json >/dev/null 2>&1 || {
  log "FATAL: cannot reach the cluster. Is the SSH tunnel up, and KUBECONFIG set?"
  exit 1
}

# Namespace and the cluster-scoped pieces first; the PVC cannot be created into
# a namespace that does not exist yet, and kubectl apply of a directory does not
# order by dependency.
log "Applying namespace, storage class, and PV."
kubectl apply -f "${HERE}/00-namespace.yaml"
kubectl apply -f "${HERE}/10-storageclass.yaml"
kubectl apply -f "${HERE}/20-pv.yaml"

# The secret is a hard precondition, not something to create here. Generating it
# in a script that lives in the repo is how a password ends up in shell history,
# in a CI log, or eventually in a commit.
if ! kubectl -n "$NS" get secret postgres-credentials >/dev/null 2>&1; then
  log "FATAL: secret/postgres-credentials is missing in namespace ${NS}."
  log "Create it out-of-band first — see infra/k8s/README.md."
  exit 1
fi

# Generated from the same SQL the dev compose stack mounts, rather than a copy
# pasted into a manifest. One source of truth for what a fresh database gets.
log "Generating postgres-init ConfigMap from infra/postgres/init/."
kubectl create configmap postgres-init \
  --namespace "$NS" \
  --from-file="${REPO_ROOT}/infra/postgres/init/" \
  --dry-run=client -o yaml | kubectl apply -f -

log "Applying PVC, StatefulSet, and Service."
kubectl apply -f "${HERE}/30-pvc.yaml"
kubectl apply -f "${HERE}/40-statefulset.yaml"
kubectl apply -f "${HERE}/50-service.yaml"

log "Waiting for postgres-0 to become Ready (up to 5 minutes)."
kubectl -n "$NS" rollout status statefulset/postgres --timeout=300s

log "Done."
kubectl -n "$NS" get pod,svc,pvc -o wide
