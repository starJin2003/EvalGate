#!/usr/bin/env bash
# Three real suite cases against the deployed model server.
#
#   KUBECONFIG=~/.kube/evalgate.yaml ./infra/k8s/model/smoke.sh [n_cases]
#
# Why this is not folded into apply.sh's probes: the probes answer "are weights
# loaded", and /props answers "which weights". Neither answers "does generation
# actually work end to end through the same prompt renderer the training data
# used" - and that is the question whose wrong answer looks like a model
# regression three weeks into P3 rather than like a broken deploy today.
#
# The prompts come from evalcore.prompt via the real suite, NOT from a
# hand-written test string. A synthetic prompt would exercise the HTTP path and
# skip the one thing most likely to be silently wrong: that the served chat
# template, the system prompt and the [C1]-style context rendering all still
# agree with what the model was trained on.
#
# Runs from the node against the Service ClusterIP, so it needs no port-forward
# and proves the Service routes - the same path P3's DAG will use.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${HERE}/../../.." && pwd)"
N_CASES="${1:-3}"
NS=evalgate
SUITE="${REPO_ROOT}/training/artifacts/p13/suite.json"

log() { printf '[smoke] %s\n' "$*"; }

[ -f "$SUITE" ] || {
  log "FATAL: ${SUITE} not found. Rebuild it:"
  log "  uv run evalgate-eval build-suite --golden training/artifacts/golden_set.jsonl --out ${SUITE}"
  exit 1
}

if [ -z "${EVALGATE_NODE_SSH:-}" ]; then
  NODE_SSH="$(cd "${REPO_ROOT}/infra/terraform" && AWS_REQUEST_CHECKSUM_CALCULATION=when_required terraform output -raw ssh_command 2>/dev/null)" || true
  [ -n "${NODE_SSH:-}" ] || { log "FATAL: EVALGATE_NODE_SSH unset and terraform output failed."; exit 1; }
  NODE_SSH="${NODE_SSH//\~/$HOME}"
else
  NODE_SSH="${EVALGATE_NODE_SSH}"
fi

CLUSTER_IP=$(kubectl -n "$NS" get svc evalgate-model -o jsonpath='{.spec.clusterIP}')
log "Target: ${CLUSTER_IP}:8080 (Service evalgate-model)"

# Render the request bodies locally with the real renderer, then send them from
# the node. Rendering here and sending there is what keeps the prompt identical
# to the one the local baseline used while still exercising the in-cluster path.
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

log "Rendering ${N_CASES} cases through evalcore.prompt (median prompt length)."
cd "$REPO_ROOT"
uv run python - "$SUITE" "$N_CASES" "$WORK" <<'PY'
import json, sys
from pathlib import Path

from evalcore.loader import load_suite
from evalcore.prompt import build_messages

suite_path, n, workdir = sys.argv[1], int(sys.argv[2]), sys.argv[3]
suite = load_suite(Path(suite_path))

# Sort by rendered prompt size and take from the middle. The smoke check should
# represent a typical case, not the cheapest one - a 1.2k-token case would make
# prefill look fast and prove nothing about the real workload.
sized = []
for case in suite.cases:
    msgs = build_messages(case.question, case.context)
    sized.append((sum(len(m["content"]) for m in msgs), case, msgs))
sized.sort(key=lambda t: t[0])
mid = len(sized) // 2
chosen = sized[max(0, mid - n // 2): max(0, mid - n // 2) + n]

index = []
for i, (chars, case, msgs) in enumerate(chosen):
    body = {
        "messages": msgs,
        "temperature": 0.0,
        "max_tokens": 512,
        "chat_template_kwargs": {"enable_thinking": False},
        # cache_prompt off, for measurement honesty. llama-server reuses the KV
        # cache when a request shares a prefix with the previous one, so a
        # re-run of this script otherwise reports "prompt 1 tok / 0.16s" and a
        # per-case wall time an order of magnitude below the truth. It does not
        # change the OUTPUT either way - a cached KV entry is bit-identical to
        # the recomputed one - so this costs accuracy nothing and buys a
        # prefill number that means something.
        "cache_prompt": False,
    }
    path = f"{workdir}/req{i}.json"
    with open(path, "w") as fh:
        json.dump(body, fh)
    index.append({"i": i, "case_id": case.case_id, "category": case.category.value, "chars": chars})
    print(f"  case {case.case_id}  {case.category.value:<12} {chars:>6} prompt chars")
with open(f"{workdir}/index.json", "w") as fh:
    json.dump(index, fh)
PY

log "Sending them from the node."
$NODE_SSH "mkdir -p /tmp/evalgate-smoke && rm -f /tmp/evalgate-smoke/*"
# COPYFILE_DISABLE=1 stops macOS tar from emitting ._AppleDouble companions for
# every file. They are junk on Linux, and they come back on the return trip as
# entries that macOS tar then refuses to extract.
COPYFILE_DISABLE=1 tar -czf - -C "$WORK" . | $NODE_SSH "tar -xzf - -C /tmp/evalgate-smoke"

# shellcheck disable=SC2029  # CLUSTER_IP and N_CASES are meant to expand locally
$NODE_SSH "
set -eu
for i in \$(seq 0 \$(( ${N_CASES} - 1 ))); do
  start=\$(date +%s.%N)
  code=\$(curl -sS -o /tmp/evalgate-smoke/resp\$i.json -w '%{http_code}' \
      --max-time 900 -H 'Content-Type: application/json' \
      -X POST --data-binary @/tmp/evalgate-smoke/req\$i.json \
      http://${CLUSTER_IP}:8080/v1/chat/completions)
  end=\$(date +%s.%N)
  echo \"case \$i  http \$code  \$(echo \"\$end - \$start\" | bc)s\"
done
"

log "Pulling responses back and asserting on them."
# Only the responses, named explicitly. Pulling the whole directory back would
# also drag the request bodies - ~11 KB of prompt each - for no reason.
$NODE_SSH "tar -czf - -C /tmp/evalgate-smoke \$(cd /tmp/evalgate-smoke && ls resp*.json | tr '\n' ' ')" \
  | tar -xzf - -C "$WORK"

uv run python - "$WORK" "$N_CASES" <<'PY'
import json, sys

workdir, n = sys.argv[1], int(sys.argv[2])
index = {e["i"]: e for e in json.load(open(f"{workdir}/index.json"))}

failures = []
for i in range(n):
    meta = index[i]
    payload = json.load(open(f"{workdir}/resp{i}.json"))
    if "error" in payload:
        failures.append(f"case {meta['case_id']}: server error {payload['error']}")
        continue
    content = payload["choices"][0]["message"]["content"]
    timings = payload.get("timings", {})
    usage = payload.get("usage", {})

    # Three assertions, each catching something different:
    #   non-empty      -> generation ran at all
    #   [C marker      -> the model still emits grounded citations, which means
    #                     the context block reached it in the trained shape
    #   no <think>     -> enable_thinking:false is being honoured by the served
    #                     chat template, not just by learned behaviour
    if not content.strip():
        failures.append(f"case {meta['case_id']}: empty completion")
    if "[C" not in content:
        failures.append(f"case {meta['case_id']}: no citation marker in output")
    if "<think>" in content:
        failures.append(f"case {meta['case_id']}: thinking block leaked into output")

    pp_n = timings.get("prompt_n", usage.get("prompt_tokens", 0))
    pp_s = timings.get("prompt_ms", 0) / 1000 or None
    tg_n = timings.get("predicted_n", usage.get("completion_tokens", 0))
    tg_s = timings.get("predicted_ms", 0) / 1000 or None
    print(f"  {meta['case_id']}  {meta['category']:<12}")
    print(f"    prompt     {pp_n:>5} tok"
          + (f"  {pp_s:6.2f}s  {pp_n/pp_s:7.2f} tok/s" if pp_s else ""))
    print(f"    generated  {tg_n:>5} tok"
          + (f"  {tg_s:6.2f}s  {tg_n/tg_s:7.2f} tok/s" if tg_s else ""))
    print(f"    output     {content.strip()[:110]!r}")

if failures:
    print("\nFAILED:")
    for f in failures:
        print(f"  {f}")
    sys.exit(1)
print("\nAll assertions passed.")
PY
