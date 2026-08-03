#!/usr/bin/env bash
# Apply the API stack to k3s. Run from the repo root with the SSH tunnel up and
# KUBECONFIG pointed at ~/.kube/evalgate.yaml.
#
#   KUBECONFIG=~/.kube/evalgate.yaml ./infra/k8s/api/apply.sh
#
# Preconditions checked rather than assumed:
#   - the cluster is reachable
#   - the evalgate namespace exists (created by the Postgres stack)
#   - postgres-credentials and evalgate-api-token secrets exist
#   - the image is present in containerd's k8s.io namespace

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NS=evalgate
IMAGE="${EVALGATE_API_IMAGE:-evalgate-api:0.1.0}"

log() { printf '[api-apply] %s\n' "$*"; }

kubectl version -o json >/dev/null 2>&1 || {
  log "FATAL: cannot reach the cluster. Is the SSH tunnel up, and KUBECONFIG set?"
  exit 1
}

# The namespace belongs to the Postgres stack. Creating it here too would mean
# two owners for one object and a deploy order that silently matters.
kubectl get namespace "$NS" >/dev/null 2>&1 || {
  log "FATAL: namespace ${NS} does not exist. Deploy the Postgres stack first."
  exit 1
}

for secret in postgres-credentials evalgate-api-token; do
  kubectl -n "$NS" get secret "$secret" >/dev/null 2>&1 || {
    log "FATAL: secret/${secret} is missing in namespace ${NS}."
    log "Both are created out-of-band — see infra/k8s/README.md."
    exit 1
  }
done

# There is no registry, so imagePullPolicy: IfNotPresent means kubelet will use
# whatever is already in containerd and never fetch. If the image is not there,
# the failure is ErrImageNeverPull several minutes from now; checking here says
# so immediately, and names the namespace that is almost always the reason.
if [ -n "${EVALGATE_NODE_SSH:-}" ]; then
  log "Checking ${IMAGE} exists in containerd namespace k8s.io."
  # shellcheck disable=SC2029  # $IMAGE is meant to expand locally
  ${EVALGATE_NODE_SSH} "sudo k3s ctr -n k8s.io images list -q | grep -qx 'docker.io/library/${IMAGE}'" || {
    log "FATAL: ${IMAGE} is not in containerd's k8s.io namespace on the node."
    log "Build it there first — see infra/k8s/README.md. An image built into the"
    log "'default' namespace is invisible to kubelet even though ctr lists it."
    exit 1
  }
else
  log "EVALGATE_NODE_SSH unset; skipping the containerd image check."
fi

log "Applying Deployment, Service, and Ingress."
kubectl apply -f "${HERE}/10-deployment.yaml"
kubectl apply -f "${HERE}/20-service.yaml"
kubectl apply -f "${HERE}/30-ingress.yaml"

log "Waiting for the rollout (up to 5 minutes)."
kubectl -n "$NS" rollout status deployment/evalgate-api --timeout=300s

log "Done."
kubectl -n "$NS" get pod,svc,ingress -l app.kubernetes.io/name=evalgate-api -o wide
