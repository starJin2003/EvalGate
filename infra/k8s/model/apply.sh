#!/usr/bin/env bash
# Apply the model server to k3s. Run from the repo root with the SSH tunnel up
# and KUBECONFIG pointed at ~/.kube/evalgate.yaml.
#
#   KUBECONFIG=~/.kube/evalgate.yaml \
#   EVALGATE_NODE_SSH="ssh -i ~/.ssh/evalgate_ed25519 ubuntu@<node-ip>" \
#     ./infra/k8s/model/apply.sh
#
# Preconditions checked rather than assumed:
#   - the cluster is reachable
#   - the evalgate namespace exists (created by the Postgres stack)
#   - the weights are on the node with the right digest
#   - the image is present in containerd's k8s.io namespace
#
# After the rollout it runs two assertions that are deliberately NOT probes,
# because probes must stay cheap and these must not be cheap. See the bottom of
# this file.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${HERE}/../../.." && pwd)"
NS=evalgate
IMAGE="${EVALGATE_MODEL_IMAGE:-evalgate-llama-server:b10210}"
MANIFEST="${REPO_ROOT}/training/artifacts/p13/gguf_manifest.json"

log() { printf '[model-apply] %s\n' "$*"; }

kubectl version -o json >/dev/null 2>&1 || {
  log "FATAL: cannot reach the cluster. Is the SSH tunnel up, and KUBECONFIG set?"
  exit 1
}

kubectl get namespace "$NS" >/dev/null 2>&1 || {
  log "FATAL: namespace ${NS} does not exist. Deploy the Postgres stack first."
  exit 1
}

[ -f "$MANIFEST" ] || { log "FATAL: ${MANIFEST} not found."; exit 1; }

read -r ARTIFACT EXPECTED_SHA EXPECTED_SIZE <<EOF
$(python3 -c "
import json, sys
m = json.load(open(sys.argv[1]))
print(m['artifact'], m['sha256'], m['size_bytes'])
" "$MANIFEST")
EOF

# Same reasoning as the api stack's image check: without this, a missing file
# surfaces as Init:CrashLoopBackOff several minutes from now instead of a
# one-line failure here that names the fix.
if [ -n "${EVALGATE_NODE_SSH:-}" ]; then
  log "Checking the weights are on the node."
  # shellcheck disable=SC2029  # $ARTIFACT is meant to expand locally
  ${EVALGATE_NODE_SSH} "test -f /var/lib/evalgate/models/${ARTIFACT}" || {
    log "FATAL: /var/lib/evalgate/models/${ARTIFACT} is missing on the node."
    log "Run ./infra/k8s/model/upload-weights.sh first."
    exit 1
  }

  log "Checking ${IMAGE} exists in containerd namespace k8s.io."
  # shellcheck disable=SC2029  # $IMAGE is meant to expand locally
  ${EVALGATE_NODE_SSH} "sudo k3s ctr -n k8s.io images list -q | grep -qx 'docker.io/library/${IMAGE}'" || {
    log "FATAL: ${IMAGE} is not in containerd's k8s.io namespace on the node."
    log "Build it there first: ./infra/k8s/model/build-on-node.sh"
    exit 1
  }
else
  log "EVALGATE_NODE_SSH unset; skipping the node-side weight and image checks."
fi

# The digest ConfigMap is GENERATED from the manifest, never committed - the
# same rule the Postgres init-SQL ConfigMap follows. One source of truth for the
# hash means the initContainer cannot drift from the artifact record.
log "Generating the weight-manifest ConfigMap from gguf_manifest.json."
kubectl -n "$NS" create configmap evalgate-model-manifest \
  --from-literal=GGUF_FILENAME="$ARTIFACT" \
  --from-literal=GGUF_SHA256="$EXPECTED_SHA" \
  --from-literal=GGUF_SIZE_BYTES="$EXPECTED_SIZE" \
  --dry-run=client -o yaml | kubectl apply -f -

log "Applying StorageClass, PV, PVC, Deployment, and Service."
kubectl apply -f "${HERE}/10-storageclass.yaml"
kubectl apply -f "${HERE}/20-pv.yaml"
kubectl apply -f "${HERE}/30-pvc.yaml"
kubectl apply -f "${HERE}/40-deployment.yaml"
kubectl apply -f "${HERE}/50-service.yaml"

if kubectl get crd servicemonitors.monitoring.coreos.com >/dev/null 2>&1; then
  log "ServiceMonitor CRD present; applying the scrape config."
  kubectl apply -f "${HERE}/60-servicemonitor.yaml"
else
  log "ServiceMonitor CRD absent; skipping (install kube-prometheus-stack first)."
fi

log "Waiting for the rollout (up to 10 minutes; the model is 1,056 MiB)."
ROLLOUT_START=$(date +%s)
kubectl -n "$NS" rollout status deployment/evalgate-model --timeout=600s
ROLLOUT_END=$(date +%s)
log "Rollout wall time: $((ROLLOUT_END - ROLLOUT_START))s"

kubectl -n "$NS" get pod,svc,pvc -l app.kubernetes.io/name=evalgate-model -o wide

# ---------------------------------------------------------------------------
# Post-rollout assertion. NOT a probe.
#
# All three probes in 40-deployment.yaml are httpGet /health, which is honest
# about whether weights are LOADED but says nothing about WHICH weights. A
# server that loaded a different GGUF, or one built against a different context
# size, passes every probe. This is what catches that, and it runs once per
# deploy rather than every 15 seconds.
#
# Run from the NODE against the Service ClusterIP, not from inside the pod: the
# runtime image is built with LLAMA_CURL=OFF and has no HTTP client, and
# Ubuntu's /bin/sh is dash, which has no /dev/tcp. The node reaches ClusterIPs
# through the same kube-proxy iptables rules it installs on the host, so this
# also proves the Service routes - which `kubectl get svc` does not.
# ---------------------------------------------------------------------------
if [ -n "${EVALGATE_NODE_SSH:-}" ]; then
  CLUSTER_IP=$(kubectl -n "$NS" get svc evalgate-model -o jsonpath='{.spec.clusterIP}')
  log "Assertion: /props reports the model and context we intended (via ${CLUSTER_IP}:8080)."
  # shellcheck disable=SC2029  # $CLUSTER_IP is meant to expand locally
  PROPS=$(${EVALGATE_NODE_SSH} "curl -sS --max-time 20 http://${CLUSTER_IP}:8080/props")
  printf '%s' "$PROPS" | python3 -c '
import json, sys
props = json.load(sys.stdin)
artifact = sys.argv[1]
settings = props.get("default_generation_settings", {}) or {}
n_ctx = settings.get("n_ctx") or props.get("n_ctx")
model = props.get("model_path", "")
assert artifact in model, f"loaded model {model!r} is not {artifact!r}"
assert int(n_ctx) == 8192, f"n_ctx is {n_ctx}, expected 8192"
print(f"  model_path  {model}")
print(f"  n_ctx       {n_ctx}")
' "$ARTIFACT" || { log "FATAL: /props did not match expectations."; exit 1; }
else
  log "EVALGATE_NODE_SSH unset; skipping the /props assertion."
fi

log "Deployed. Run ./infra/k8s/model/smoke.sh for the 3-case generation check."
