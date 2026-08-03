# DAGs baked into the Airflow image, built on the node.
#
# Not git-sync. A sidecar per component is three always-on pollers on a
# CPU-bound node, and — the deciding reason — a push mid-run swaps the DAG file
# under a running DAG, so a run's code is not identifiable afterwards. See
# values.yaml.
#
# Build context is infra/k8s/airflow/, not the repo root: DAGs are the only
# thing that goes in.
#
#   nerdctl --namespace k8s.io build -t evalgate-airflow:3.2.2-dags infra/k8s/airflow
#
# Base verified on the node before use: apache/airflow:3.2.2 is linux/arm64,
# 2.476 GB unpacked, and ships airflow.providers.fab — which the chart's default
# auth_manager requires and which `airflow users create` lives in.
FROM apache/airflow:3.2.2

# --chown because the airflow user (uid 50000) must be able to read them, and
# COPY defaults to root-owned.
COPY --chown=airflow:root dags/ /opt/airflow/dags/
