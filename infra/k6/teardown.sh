#!/usr/bin/env bash
# Remove exactly the rows seed.sh created, and prove the counts returned to
# where they started.
#
#   KUBECONFIG=~/.kube/evalgate.yaml ./infra/k6/teardown.sh [expected_suites expected_runs expected_baselines]
#
# Scoped DELETEs on the `k6-` id prefix, in foreign-key order. Deliberately NOT
# TRUNCATE, NOT DROP, and NOT CASCADE.
#
# The reason that is spelled out rather than assumed: on 2026-08-03 a test
# fixture pointed at this same database ran TRUNCATE on teardown and destroyed
# rows it had not created. Nothing here can reach a row this run did not make,
# and the counts are asserted afterwards rather than trusted.

set -euo pipefail

NS=evalgate
EXP_SUITES="${1:-0}"
EXP_RUNS="${2:-0}"
EXP_BASELINES="${3:-0}"

log() { printf '[k6-teardown] %s\n' "$*"; }

psql_do() {
  kubectl -n "$NS" exec -i postgres-0 -- psql -U evalgate -d evalgate -X -t -A "$@"
}

log "Deleting rows with the k6- prefix, in FK order."
psql_do -c "DELETE FROM baselines WHERE suite_id LIKE 'k6-%';" \
        -c "DELETE FROM runs      WHERE suite_id LIKE 'k6-%';" \
        -c "DELETE FROM suites    WHERE suite_id LIKE 'k6-%';" \
  | sed 's/^/  /'

counts=$(psql_do -c "SELECT (SELECT count(*) FROM suites), (SELECT count(*) FROM runs), (SELECT count(*) FROM baselines);")
IFS='|' read -r s r b <<< "$counts"

log "Counts now: suites=${s} runs=${r} baselines=${b}"
log "Expected:   suites=${EXP_SUITES} runs=${EXP_RUNS} baselines=${EXP_BASELINES}"

if [ "$s" != "$EXP_SUITES" ] || [ "$r" != "$EXP_RUNS" ] || [ "$b" != "$EXP_BASELINES" ]; then
  log "FATAL: counts did not return to the pre-run baseline."
  log "Nothing was force-deleted. Investigate before re-running; do not TRUNCATE."
  exit 1
fi

log "Database restored to its pre-run state."
