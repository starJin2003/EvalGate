#!/usr/bin/env bash
# Report what the node has committed: pod count, CPU/memory requests and limits
# as a percentage of allocatable, plus actual usage.
#
#   KUBECONFIG=~/.kube/evalgate.yaml ./infra/k8s/model/node-commitment.sh [label]
#
# Exists because "requests 37%, limits 193%" is a number this project quotes
# repeatedly and had been read by hand out of `kubectl describe node` each time.
# Run it before and after a deploy and the delta is the deploy's real cost, not
# an estimate from the manifests.
#
# The distinction that matters on this node: REQUESTS are what the scheduler
# reserves and cannot exceed 100%; LIMITS are what the cgroup enforces and are
# deliberately overcommitted. A limits total above 100% is not an error, it is
# the statement that not everything runs flat out at once.

set -euo pipefail

LABEL="${1:-}"
NODE="${EVALGATE_NODE_NAME:-evalgate-k3s}"

kubectl version -o json >/dev/null 2>&1 || {
  echo "[commitment] FATAL: cannot reach the cluster. Is the tunnel up and KUBECONFIG set?" >&2
  exit 1
}

echo "=== node commitment ${LABEL:+(${LABEL})} ==="

kubectl get node "$NODE" -o json | python3 -c '
import json, sys
node = json.load(sys.stdin)
alloc = node["status"]["allocatable"]

def cpu_m(v):
    return int(v[:-1]) if v.endswith("m") else int(float(v) * 1000)

def mem_mi(v):
    units = {"Ki": 1 / 1024, "Mi": 1, "Gi": 1024, "K": 1e3 / 2**20, "M": 1e6 / 2**20, "G": 1e9 / 2**20}
    for suffix, mult in units.items():
        if v.endswith(suffix):
            return float(v[: -len(suffix)]) * mult
    return float(v) / 2**20

c = cpu_m(alloc["cpu"])
m = mem_mi(alloc["memory"])
print(f"allocatable   cpu {c}m   memory {m:.0f}Mi")
'

kubectl get pods --all-namespaces --field-selector "spec.nodeName=${NODE},status.phase=Running" -o json \
  | python3 -c '
import json, sys

def cpu_m(v):
    if not v: return 0
    return int(v[:-1]) if v.endswith("m") else int(float(v) * 1000)

def mem_mi(v):
    if not v: return 0.0
    units = {"Ki": 1/1024, "Mi": 1, "Gi": 1024, "K": 1e3/2**20, "M": 1e6/2**20, "G": 1e9/2**20}
    for s, m in units.items():
        if v.endswith(s):
            return float(v[:-len(s)]) * m
    return float(v) / 2**20

pods = json.load(sys.stdin)["items"]
rc = rm = lc = lm = 0.0
for p in pods:
    # initContainers do not add to the running total: they have exited. Only
    # the max of (sum of containers, max of initContainers) is reserved, and
    # once running it is the container sum that holds.
    for c in p["spec"]["containers"]:
        r = c.get("resources", {})
        rc += cpu_m(r.get("requests", {}).get("cpu"))
        rm += mem_mi(r.get("requests", {}).get("memory"))
        lc += cpu_m(r.get("limits", {}).get("cpu"))
        lm += mem_mi(r.get("limits", {}).get("memory"))
print(f"pods          {len(pods)}")
print(f"cpu requests  {rc:.0f}m")
print(f"cpu limits    {lc:.0f}m")
print(f"mem requests  {rm:.0f}Mi")
print(f"mem limits    {lm:.0f}Mi")
'

echo "--- actual usage ---"
kubectl top node "$NODE" 2>/dev/null || echo "(metrics-server not installed; use free/uptime on the node)"
