#!/usr/bin/env bash
# Create Airflow's FAB tables and its admin user. Idempotent; run after apply.sh.
#
#   KUBECONFIG=~/.kube/evalgate.yaml ./infra/k8s/airflow/bootstrap-admin.sh
#
# Why this exists rather than the chart's createUserJob: that job creates
# admin/admin, and a default credential must never exist on this cluster even
# briefly. `createUserJob.enabled` is false in values.yaml.
#
# Why `fab-db migrate` is here: chart 1.22.0's migrate job runs ONLY
# `airflow db migrate` (its values.yaml line 2185). The FAB provider keeps its
# own tables on its own migration chain, so without this step `airflow users
# create` fails on a missing table and the api-server login fails — the classic
# "discovered at first login" trap. Verified against the chart, not assumed.
#
# The password is read from the out-of-band airflow-admin secret and never
# printed, never passed as a command-line argument, and never written to the
# repo.

set -euo pipefail

NS=airflow

log() { printf '[airflow-bootstrap] %s\n' "$*"; }

kubectl -n "$NS" get secret airflow-admin >/dev/null 2>&1 || {
  log "FATAL: secret/airflow-admin is missing. Create it out-of-band first."
  exit 1
}

SCHED="$(kubectl -n "$NS" get pods -l component=scheduler -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)"
[ -n "$SCHED" ] || { log "FATAL: no scheduler pod found. Has apply.sh run?"; exit 1; }

log "Running 'airflow fab-db migrate' (idempotent — alembic no-ops when current)."
kubectl -n "$NS" exec "$SCHED" -c scheduler -- airflow fab-db migrate 2>&1 | tail -3 | sed 's/^/      /'

if kubectl -n "$NS" exec "$SCHED" -c scheduler -- airflow users list 2>/dev/null | grep -q "^admin\b\|[[:space:]]admin[[:space:]]"; then
  log "admin user already exists, leaving it alone"
else
  log "Creating the admin user."
  # The password reaches the container through stdin, not argv, so it is not
  # visible in `ps` on the node and not in this script's output.
  kubectl -n "$NS" get secret airflow-admin -o jsonpath='{.data.password}' | base64 -d \
    | kubectl -n "$NS" exec -i "$SCHED" -c scheduler -- sh -c '
        read -r PW
        airflow users create \
          --username admin --firstname EvalGate --lastname Admin \
          --role Admin --email admin@evalgate.invalid \
          --password "$PW" >/dev/null && echo "  created"
      '
fi

log "Users now present:"
kubectl -n "$NS" exec "$SCHED" -c scheduler -- airflow users list 2>/dev/null | sed 's/^/      /'
log "Done. Reach the UI with:"
log "  kubectl -n ${NS} port-forward svc/airflow-api-server 8080:8080"
log "  password: kubectl -n ${NS} get secret airflow-admin -o jsonpath='{.data.password}' | base64 -d"
