#!/usr/bin/env bash
# Run the P2 load test in the foreground, on the node, as a Job in the cluster.
#
#   KUBECONFIG=~/.kube/evalgate.yaml ./infra/k6/run.sh
#
# Does not seed and does not tear down — those are separate scripts on purpose,
# so a failed run leaves the fixture in place to inspect rather than deleting the
# evidence. Order is: seed.sh, run.sh, teardown.sh.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NS=evalgate

log() { printf '[k6-run] %s\n' "$*"; }

kubectl version -o json >/dev/null 2>&1 || {
  log "FATAL: cannot reach the cluster. Is the SSH tunnel up, and KUBECONFIG set?"
  exit 1
}

# Generated from the committed script rather than pasted into the manifest, so
# infra/k6/script.js stays the single source.
log "Loading script.js into a ConfigMap."
kubectl -n "$NS" create configmap k6-script \
  --from-file="${HERE}/script.js" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl -n "$NS" delete job k6-load --ignore-not-found >/dev/null 2>&1
log "Starting the Job."
kubectl apply -f "${HERE}/job.yaml"

# Foreground: stream k6's own output as it runs rather than polling for a result.
log "Waiting for the pod, then streaming k6 output (~7m30s)."
kubectl -n "$NS" wait --for=condition=PodReadyToStartContainers pod \
  -l job-name=k6-load --timeout=120s >/dev/null 2>&1 || true
sleep 3
kubectl -n "$NS" logs -f job/k6-load

# k6 exits non-zero when a threshold breaches, and the Job carries that through.
# A breach is the recorded result, not a reason to retune, so this reports the
# outcome rather than masking it.
if kubectl -n "$NS" wait --for=condition=complete job/k6-load --timeout=60s >/dev/null 2>&1; then
  log "RESULT: all thresholds held."
  exit 0
fi
if kubectl -n "$NS" wait --for=condition=failed job/k6-load --timeout=30s >/dev/null 2>&1; then
  log "RESULT: at least one threshold BREACHED. That is the number; record it."
  exit 1
fi
log "RESULT: job neither complete nor failed within the wait window; check manually."
exit 2
