#!/usr/bin/env bash
# Thread-count sweep for llama-server under this node's real cgroup.
#
#   KUBECONFIG=~/.kube/evalgate.yaml EVALGATE_NODE_SSH="..." \
#     ./infra/k8s/model/bench-threads.sh
#
# THE DECISION RULE WAS COMMITTED BEFORE ANY NUMBER EXISTED. It is reproduced
# here verbatim so a reader can check the numbers against it rather than take
# the conclusion on trust:
#
#   Sweep -t auto, 2, 3, 4 with the pod at its 1500m/2500m budget, 3 identical
#   cases per arm. Pick the lowest MEDIAN WALL TIME PER CASE. A tie within 5%
#   goes to the LOWER thread count, because leftover quota is what keeps the
#   HTTP thread answerable and what the co-tenant API borrows.
#
# One refinement, also made before seeing any arm's results and recorded in
# DECISIONS.md rather than applied silently: the 3 cases are drawn near the
# median on BOTH rendered prompt length and the LOCAL run's output length. The
# original rule said median prompt only, but 2 of the 3 median-prompt cases run
# to the 512-token cap, which would weight the measurement toward generation far
# more heavily than the real suite does (3 of 96 cases hit the cap). Output
# length is a property of the case, and the local run already measured it.
#
# Why this must run inside the pod's cgroup rather than as a bare `nerdctl run
# --cpus 2.5`: the thing being measured IS the interaction between llama.cpp's
# spin-barrier threadpool and CFS quota exhaustion. A different cgroup with the
# same nominal quota is a different scheduler context.
#
# Arms are compared on wall time, but throttling is recorded too, read from the
# container's own /sys/fs/cgroup/cpu.stat. That is the number that explains WHY
# an arm lost, and it is the one a future reader will want when the CPU budget
# changes.
#
# Thread count does not change model output - ggml partitions matmul by output
# row, so no reduction is split across threads - so this sweep is safe to run
# before the score comparison.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${HERE}/../../.." && pwd)"
NS=evalgate
SUITE="${REPO_ROOT}/training/artifacts/p13/suite.json"
LOCAL_RUN="${REPO_ROOT}/training/artifacts/p13/run_v1_iter2000.json"
N_CASES="${BENCH_CASES:-3}"
ARMS="${BENCH_ARMS:--1 2 3 4}"
OUT="${REPO_ROOT}/training/artifacts/p13/thread_sweep.json"

log() { printf '[bench] %s\n' "$*"; }

for f in "$SUITE" "$LOCAL_RUN"; do
  [ -f "$f" ] || { log "FATAL: ${f} not found."; exit 1; }
done

if [ -z "${EVALGATE_NODE_SSH:-}" ]; then
  NODE_SSH="$(cd "${REPO_ROOT}/infra/terraform" && AWS_REQUEST_CHECKSUM_CALCULATION=when_required terraform output -raw ssh_command 2>/dev/null)" || true
  [ -n "${NODE_SSH:-}" ] || { log "FATAL: EVALGATE_NODE_SSH unset and terraform output failed."; exit 1; }
  NODE_SSH="${NODE_SSH//\~/$HOME}"
else
  NODE_SSH="${EVALGATE_NODE_SSH}"
fi

CLUSTER_IP=$(kubectl -n "$NS" get svc evalgate-model -o jsonpath='{.spec.clusterIP}')
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

SUMMARISE="${WORK}/summarise.py"
cat > "$SUMMARISE" <<'PY'
import json, sys
r = json.loads(sys.argv[1])
print(f"  median wall  {r['median_wall_s']:.1f}s   "
      f"prefill {r['prefill_tok_s']} tok/s   gen {r['gen_tok_s']} tok/s")
print(f"  throttled    {r['cfs_nr_throttled']}/{r['cfs_nr_periods']} periods "
      f"({r['cfs_throttled_pct']}%)  {r['cfs_throttled_usec']/1e6:.1f}s")
PY

log "Selecting ${N_CASES} representative cases."
cd "$REPO_ROOT"
uv run python - "$SUITE" "$LOCAL_RUN" "$N_CASES" "$WORK" <<'PY'
import json, statistics, sys
from pathlib import Path

from evalcore.loader import load_suite
from evalcore.prompt import build_messages

suite_path, local_run_path, n, workdir = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
suite = load_suite(Path(suite_path))
local = {r["case_id"]: r for r in json.loads(Path(local_run_path).read_text())["results"]}

rows = []
for case in suite.cases:
    msgs = build_messages(case.question, case.context)
    prompt_chars = sum(len(m["content"]) for m in msgs)
    out_chars = len(local[case.case_id]["output"]) if case.case_id in local else 0
    rows.append({"case": case, "msgs": msgs, "prompt": prompt_chars, "out": out_chars})

med_p = statistics.median(r["prompt"] for r in rows)
med_o = statistics.median(r["out"] for r in rows)

# Rank by combined normalised distance from both medians, so a case that is
# typical in prompt but extreme in output does not get picked.
for r in rows:
    r["dist"] = abs(r["prompt"] - med_p) / med_p + abs(r["out"] - med_o) / max(med_o, 1)
rows.sort(key=lambda r: r["dist"])
chosen = rows[:n]

index = []
for i, r in enumerate(chosen):
    body = {
        "messages": r["msgs"],
        "temperature": 0.0,
        "max_tokens": 512,
        "chat_template_kwargs": {"enable_thinking": False},
        # Off, or every arm after the first measures a KV cache hit instead of
        # inference. Does not affect output.
        "cache_prompt": False,
    }
    Path(f"{workdir}/req{i}.json").write_text(json.dumps(body))
    index.append({"i": i, "case_id": r["case"].case_id, "category": r["case"].category.value,
                  "prompt_chars": r["prompt"], "local_out_chars": r["out"]})
    print(f"  {r['case'].case_id}  {r['case'].category.value:<12} "
          f"prompt {r['prompt']:>6}ch (median {med_p:.0f})  "
          f"local out {r['out']:>5}ch (median {med_o:.0f})")
Path(f"{workdir}/index.json").write_text(json.dumps(index))
PY

log "Shipping request bodies to the node."
$NODE_SSH "mkdir -p /tmp/evalgate-bench && rm -f /tmp/evalgate-bench/*"
COPYFILE_DISABLE=1 tar -czf - -C "$WORK" . | $NODE_SSH "tar -xzf - -C /tmp/evalgate-bench"

# Deliberately NOT inside $WORK. $WORK is removed by the exit trap, so an
# arm-4 crash used to destroy arms 1-3 as well - which happened twice while this
# script was being written, at ~6 minutes of measurement per arm. Completed arms
# are expensive; they get a path that survives.
RESULTS="${REPO_ROOT}/training/artifacts/p13/thread_sweep_raw.jsonl"
: > "$RESULTS"

for T in $ARMS; do
  if [ "$T" = "-1" ]; then
    LABEL="auto"
  else
    LABEL="$T"
  fi
  log "=== arm -t ${LABEL} ==="

  kubectl -n "$NS" set env deployment/evalgate-model \
    LLAMA_THREADS="$T" OMP_NUM_THREADS="$T" >/dev/null
  kubectl -n "$NS" rollout status deployment/evalgate-model --timeout=600s >/dev/null
  # The Service endpoint can lag the pod going Ready by a beat.
  sleep 5

  # Baseline the throttle counters for this arm.
  BEFORE=$(kubectl -n "$NS" exec deploy/evalgate-model -c llama-server -- cat /sys/fs/cgroup/cpu.stat)
  NR_BEFORE=$(echo "$BEFORE" | awk '/^nr_throttled/{print $2}')
  US_BEFORE=$(echo "$BEFORE" | awk '/^throttled_usec/{print $2}')
  PER_BEFORE=$(echo "$BEFORE" | awk '/^nr_periods/{print $2}')

  # What -t actually resolves to. /props does NOT expose n_threads in b10210,
  # so this is derived rather than asked for: llama.cpp's auto-detect is
  # std::thread::hardware_concurrency(), which reads the CPUs visible in the
  # pod's affinity mask - and Kubernetes enforces a CPU *limit* as a CFS quota,
  # not as an affinity mask. So for the auto arm the resolved count is the
  # visible CPU count, which is measured here rather than assumed.
  if [ "$T" = "-1" ]; then
    RESOLVED=$(kubectl -n "$NS" exec deploy/evalgate-model -c llama-server -- \
      sh -c 'ls /sys/devices/system/cpu | grep -cE "^cpu[0-9]+$"')
    MASK=$(kubectl -n "$NS" exec deploy/evalgate-model -c llama-server -- \
      sh -c 'grep Cpus_allowed_list /proc/1/status | tr -s " \t" " "')
    log "auto resolves to ${RESOLVED} threads (${MASK}); cgroup quota is $(kubectl -n "$NS" exec deploy/evalgate-model -c llama-server -- cat /sys/fs/cgroup/cpu.max)"
  else
    RESOLVED="$T"
  fi

  # shellcheck disable=SC2029  # expansions are deliberate
  TIMINGS=$($NODE_SSH "
    set -eu
    for i in \$(seq 0 \$(( ${N_CASES} - 1 ))); do
      start=\$(date +%s.%N)
      curl -sS -o /tmp/evalgate-bench/resp\$i.json \
        --max-time 1800 -H 'Content-Type: application/json' \
        -X POST --data-binary @/tmp/evalgate-bench/req\$i.json \
        http://${CLUSTER_IP}:8080/v1/chat/completions >/dev/null
      end=\$(date +%s.%N)
      python3 -c \"
import json
d = json.load(open('/tmp/evalgate-bench/resp\$i.json'))
t = d.get('timings', {})
print(json.dumps({
  'i': \$i,
  'wall_s': round(\$end - \$start, 3),
  'prompt_n': t.get('prompt_n'), 'prompt_ms': t.get('prompt_ms'),
  'predicted_n': t.get('predicted_n'), 'predicted_ms': t.get('predicted_ms'),
}))\"
    done
  ")

  AFTER=$(kubectl -n "$NS" exec deploy/evalgate-model -c llama-server -- cat /sys/fs/cgroup/cpu.stat)
  NR_AFTER=$(echo "$AFTER" | awk '/^nr_throttled/{print $2}')
  US_AFTER=$(echo "$AFTER" | awk '/^throttled_usec/{print $2}')
  PER_AFTER=$(echo "$AFTER" | awk '/^nr_periods/{print $2}')

  python3 - "$LABEL" "$RESOLVED" \
    "$((NR_AFTER - NR_BEFORE))" "$((US_AFTER - US_BEFORE))" "$((PER_AFTER - PER_BEFORE))" \
    <<PY >> "$RESULTS"
import json, statistics, sys
label, resolved, nr, us, periods = sys.argv[1:6]
rows = [json.loads(l) for l in '''${TIMINGS}'''.strip().splitlines() if l.strip()]
walls = sorted(r["wall_s"] for r in rows)
pp_n = sum(r["prompt_n"] or 0 for r in rows)
pp_ms = sum(r["prompt_ms"] or 0 for r in rows)
tg_n = sum(r["predicted_n"] or 0 for r in rows)
tg_ms = sum(r["predicted_ms"] or 0 for r in rows)
out = {
    "arm": label,
    "n_threads_reported": resolved,
    "median_wall_s": statistics.median(walls),
    "walls_s": walls,
    "prefill_tok_s": round(pp_n / (pp_ms / 1000), 2) if pp_ms else None,
    "gen_tok_s": round(tg_n / (tg_ms / 1000), 2) if tg_ms else None,
    "prompt_tokens": pp_n,
    "generated_tokens": tg_n,
    "cfs_nr_throttled": int(nr),
    "cfs_throttled_usec": int(us),
    "cfs_nr_periods": int(periods),
    "cfs_throttled_pct": round(100 * int(nr) / int(periods), 2) if int(periods) else None,
}
print(json.dumps(out))
PY

  # The arm summary takes its JSON as argv, NOT on stdin.
  #
  # Two bugs were fixed here, both of which cost a full sweep:
  #   `python3 -c '...'`  - a single-quoted shell string turns the \" of an
  #                         f-string subscript into a literal backslash, so
  #                         Python raises SyntaxError.
  #   `... | python3 <<PY` - a heredoc IS stdin, so the piped JSON never
  #                         reaches json.load(sys.stdin) and it fails on an
  #                         empty read.
  # argv sidesteps both.
  python3 "$SUMMARISE" "$(tail -1 "$RESULTS")"
done

log "=== applying the pre-committed rule ==="
# Same stdin conflict as above: `python3 - "$OUT" < "$RESULTS" <<'PY'` gives the
# heredoc and the file redirect to the same fd, so the script would arrive on
# stdin and the data would not arrive at all. Script to a file, data on stdin.
DECIDE="${WORK}/decide.py"
cat > "$DECIDE" <<'PY'
import json, sys
from pathlib import Path

rows = [json.loads(l) for l in sys.stdin if l.strip()]
rows_sorted = sorted(rows, key=lambda r: r["median_wall_s"])
best = rows_sorted[0]

# "A tie within 5% goes to the LOWER thread count." Ties are resolved against
# the resolved thread count, not the label, so the auto arm competes as whatever
# it actually resolved to.
def thread_key(r):
    try:
        return int(r["n_threads_reported"])
    except (TypeError, ValueError):
        return 99

tied = [r for r in rows_sorted if r["median_wall_s"] <= best["median_wall_s"] * 1.05]
winner = min(tied, key=thread_key)

print(f"{'arm':>6} {'n_thr':>6} {'median wall':>12} {'prefill':>10} {'gen':>9} {'throttled':>11}")
for r in rows_sorted:
    mark = "  <-- winner" if r is winner else ""
    print(f"{r['arm']:>6} {str(r['n_threads_reported']):>6} {r['median_wall_s']:>11.1f}s "
          f"{str(r['prefill_tok_s']):>9} {str(r['gen_tok_s']):>8} "
          f"{str(r['cfs_throttled_pct'])+'%':>11}{mark}")

if len(tied) > 1:
    print(f"\n{len(tied)} arms within 5% of the best; rule takes the lower thread count.")
print(f"\nWINNER: -t {winner['arm']} (resolved {winner['n_threads_reported']})")

Path(sys.argv[1]).write_text(json.dumps(
    {"rule": "lowest median wall time per case; tie within 5% to the lower thread count",
     "arms": rows_sorted, "winner": winner}, indent=2) + "\n")
print(f"\nwritten -> {sys.argv[1]}")
PY
python3 "$DECIDE" "$OUT" < "$RESULTS"

log "Set the winner in 40-deployment.yaml (LLAMA_THREADS and OMP_NUM_THREADS) and re-apply."
