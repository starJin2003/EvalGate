# infra/k8s

k3s manifests for the API, Postgres, model server, kube-prometheus-stack, and Airflow.
Lands across P2 and P3.

| Directory | Status |
|---|---|
| `postgres/` | **Live as of 2026-08-03.** Postgres 16.14 + pgvector 0.8.6 on a dedicated OCI block volume |
| `api/` | **Live as of 2026-08-03.** 2 replicas, Postgres-backed, public on port 80 via traefik |
| `monitoring/` | **Live as of 2026-08-03.** kube-prometheus-stack 88.1.3, 5 pods, 592 Mi, committed dashboard |
| model server | Not built yet (P2) |
| airflow | Not built yet (P3) |

---

## Reaching the cluster

Port 6443 has no ingress rule at all — the k3s API is not on the public
internet. Everything below goes through an SSH tunnel.

```bash
# Terminal 1. Leave it open.
cd infra/terraform && terraform output -raw kubectl_tunnel_command | sh

# Terminal 2. The node's kubeconfig already points at 127.0.0.1:6443,
# which is exactly what the tunnel forwards, so it is used unmodified.
scp -i ~/.ssh/evalgate_ed25519 ubuntu@<node-ip>:~/.kube/config ~/.kube/evalgate.yaml
chmod 600 ~/.kube/evalgate.yaml
export KUBECONFIG=~/.kube/evalgate.yaml
kubectl get nodes -o wide
```

If `terraform output` fails with `403 SignatureDoesNotMatch`, the cause is not
your credentials. Export `AWS_REQUEST_CHECKSUM_CALCULATION=when_required` — see
the root README's State section.

---

## postgres/

Postgres 16 with pgvector, on the 50 GB OCI block volume created by
`infra/terraform/storage.tf`. Same image as `docker-compose.dev.yml`, so dev and
prod run the same database.

| File | What it is |
|---|---|
| `node-volume-setup.sh` | One-time, on the node: `mkfs.ext4`, `/etc/fstab` by `LABEL=pgdata`, mount at `/mnt/pgdata`. Idempotent |
| `00-namespace.yaml` | `evalgate` namespace |
| `10-storageclass.yaml` | `pgdata-local`, `no-provisioner`, `reclaimPolicy: Retain` |
| `20-pv.yaml` | Static `local` PV over `/mnt/pgdata/data`, node-affine to `evalgate-k3s`, `Retain` |
| `30-pvc.yaml` | `pgdata` claim, bound to the PV by name |
| `40-statefulset.yaml` | `postgres-0`, `pgvector/pgvector:pg16` |
| `50-service.yaml` | ClusterIP `postgres:5432` |
| `apply.sh` | Applies the above in dependency order and waits for Ready |

The init SQL ConfigMap is **not** a file here. `apply.sh` generates it from
`infra/postgres/init/`, which is the same directory the dev compose stack
mounts, so the two cannot drift on what a fresh database gets.

### First-time setup

**1. Prepare the disk on the node.** Once per volume. The script refuses to
format a disk that already carries a filesystem, so re-running it is safe.

```bash
scp -i ~/.ssh/evalgate_ed25519 infra/k8s/postgres/node-volume-setup.sh ubuntu@<node-ip>:/tmp/
ssh -i ~/.ssh/evalgate_ed25519 ubuntu@<node-ip> 'sudo bash /tmp/node-volume-setup.sh'
```

**2. Create the password secret out-of-band.**

This is the one step that is deliberately manual, and it is manual because this
repository is public. A password committed to a manifest is a password in every
clone and every fork of that repo, permanently — rotating it afterwards does not
retract the copies. `apply.sh` checks that this secret exists and **fails rather
than creating one**, so the database can only ever get a password a human typed.

```bash
export KUBECONFIG=~/.kube/evalgate.yaml
kubectl apply -f infra/k8s/postgres/00-namespace.yaml

kubectl -n evalgate create secret generic postgres-credentials \
  --from-literal=POSTGRES_USER=evalgate \
  --from-literal=POSTGRES_DB=evalgate \
  --from-literal=POSTGRES_PASSWORD="$(LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 32)"
```

Alphanumeric only, so the value can be dropped into a `DATABASE_URL` without
percent-encoding. Nothing prints the password; read it back when you need it:

```bash
kubectl -n evalgate get secret postgres-credentials \
  -o jsonpath='{.data.POSTGRES_PASSWORD}' | base64 -d; echo
```

To rotate it, update the secret, then `ALTER ROLE evalgate PASSWORD '…'` inside
the database and restart the pod. Changing the secret alone does nothing — the
`POSTGRES_PASSWORD` env var is only read by `initdb`, on a **first** boot against
an empty data directory.

**3. Deploy.**

```bash
KUBECONFIG=~/.kube/evalgate.yaml ./infra/k8s/postgres/apply.sh
```

### Connecting

In-cluster: `postgres.evalgate.svc.cluster.local:5432`.

From your laptop, with the kube tunnel already up:

```bash
kubectl -n evalgate port-forward svc/postgres 15432:5432
PGPASSWORD="$(kubectl -n evalgate get secret postgres-credentials \
  -o jsonpath='{.data.POSTGRES_PASSWORD}' | base64 -d)" \
  psql -h 127.0.0.1 -p 15432 -U evalgate -d evalgate
```

Port 15432 rather than 5432 because a native Postgres owns 5432 on the dev
machine, and 5433 is the dev compose stack.

### What is deliberately guarded

The database holds the paid-for state: 1,900 teacher answers, every eval run,
every promoted baseline. Three independent things have to be defeated to lose it.

- `prevent_destroy` on `oci_core_volume.pgdata` — terraform cannot delete the disk.
- `reclaimPolicy: Retain` on both the PV and the StorageClass — deleting the PVC
  releases the volume rather than wiping it. The default `local-path` class is
  `Delete`, which is exactly why Postgres is not on it.
- `node-volume-setup.sh` hard-stops on any existing filesystem instead of
  formatting.

None of that is a backup. The tenancy has 5 free OCI volume backups available
(`terraform output pgdata_volume_id` for the OCID); scheduling one is not done yet.

### Verifying

```bash
kubectl -n evalgate get pod,svc,pvc
kubectl -n evalgate exec -it postgres-0 -- \
  psql -U evalgate -d evalgate -c 'SELECT version();' -c '\dx'
```

Persistence, the way it was proven on 2026-08-03 — note the pod's uid changes and
`restartCount` stays 0, which is what distinguishes a new pod from a container
restart inside a surviving one:

```bash
kubectl -n evalgate exec -i postgres-0 -- psql -U evalgate -d evalgate \
  -c "CREATE TABLE t (id serial PRIMARY KEY, note text);" \
  -c "INSERT INTO t (note) VALUES ('before delete');"
kubectl -n evalgate delete pod postgres-0
kubectl -n evalgate rollout status statefulset/postgres --timeout=300s
kubectl -n evalgate exec -i postgres-0 -- psql -U evalgate -d evalgate \
  -c "SELECT * FROM t;" -c "DROP TABLE t;"
```

---

## api/

`apps/api` on k3s: 2 replicas, backed by the Postgres above, published on port 80
through traefik at `http://64.181.195.241`.

| File | What it is |
|---|---|
| `node-build-setup.sh` | One-time, on the node: installs nerdctl + BuildKit and runs `buildkitd` as a systemd unit against k3s's containerd |
| `build-on-node.sh` | Streams the build context over SSH and builds the image on the node |
| `10-deployment.yaml` | 2 replicas, `evalgate-api:0.1.0`, `IfNotPresent` |
| `20-service.yaml` | ClusterIP `evalgate-api:80` |
| `30-ingress.yaml` | traefik Ingress, no host match, HTTP |
| `apply.sh` | Checks preconditions, applies, waits for the rollout |

### There is no registry

The image exists only in the node's containerd, **in the `k8s.io` namespace**.
That namespace is not a detail: containerd namespaces are hard isolation and
kubelet looks in `k8s.io` and nowhere else, so an image built into `default` is
invisible to k3s while `ctr images list` cheerfully shows it present. Both
`buildkitd` and `nerdctl` are pointed at `k8s.io` explicitly.

Two tag traps follow from having no registry. `imagePullPolicy: Always` sends
kubelet to Docker Hub for `docker.io/library/evalgate-api` and lands in
`ImagePullBackOff`; and **a `:latest` tag implicitly forces `Always`** whatever
the manifest says. So the tag is an explicit `:0.1.0` and the policy is
`IfNotPresent` — which also survives the eventual move to GHCR unchanged, where
`Never` would not.

Builds are **native**. The node is aarch64 and the image is aarch64: no buildx,
no QEMU, no emulation. Nothing is ever built on the dev machine.

### First-time setup

```bash
# 1. Install the builder on the node. Once.
scp -i ~/.ssh/evalgate_ed25519 infra/k8s/api/node-build-setup.sh ubuntu@<node-ip>:/tmp/
ssh -i ~/.ssh/evalgate_ed25519 ubuntu@<node-ip> 'sudo bash /tmp/node-build-setup.sh'

# 2. Create the write token, out-of-band, exactly like the DB password.
export KUBECONFIG=~/.kube/evalgate.yaml
kubectl -n evalgate create secret generic evalgate-api-token \
  --from-literal=EVALGATE_API_TOKEN="$(LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 40)"

# 3. Build on the node, then deploy.
./infra/k8s/api/build-on-node.sh
EVALGATE_NODE_SSH="ssh -i ~/.ssh/evalgate_ed25519 ubuntu@<node-ip>" ./infra/k8s/api/apply.sh
```

### Redeploying after a code change

The tag does not change between builds, so `kubectl apply` sees no diff and will
not restart anything. Force it:

```bash
./infra/k8s/api/build-on-node.sh
kubectl -n evalgate rollout restart deployment/evalgate-api
kubectl -n evalgate rollout status deployment/evalgate-api
```

### Authentication

Writes need a bearer token; reads, `/health`, `/ready`, and `/gate` do not — so
k6 and the P3 demo need no credential.

```bash
TOKEN=$(kubectl -n evalgate get secret evalgate-api-token \
  -o jsonpath='{.data.EVALGATE_API_TOKEN}' | base64 -d)

curl http://64.181.195.241/suites                                  # open
curl -X PUT http://64.181.195.241/suites/demo \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d @suite.json
```

With no token configured the write endpoints return **503**, not open access. An
unset credential disabling writes is the only safe default for a service on a
public port.

### Configuration

`DATABASE_URL` is assembled in the Deployment from three `secretKeyRef` env vars
rather than injected with `envFrom`. That is required, not stylistic:
Kubernetes `$(VAR)` expansion only sees variables defined earlier in the *same*
`env` list, and anything from `envFrom` is invisible to it — `DATABASE_URL` would
be set to the literal string `$(POSTGRES_USER)`.

The app picks its store from the environment: `DATABASE_URL` set means Postgres,
unset means in-memory. Tests and CI therefore need no database and no flag.

The schema is created at startup from `store.py`'s `SCHEMA`, under a Postgres
advisory lock — required, because `CREATE TABLE IF NOT EXISTS` is not atomic
against a concurrent creator and both replicas start at once. **This is not a
migration system.** The first schema change after P4 has real data needs Alembic.

### Running the tests against real Postgres

The `PostgresStore` half of `apps/api/tests/test_store.py` needs a live database,
which only exists on this node, so the tests run inside the cluster:

```bash
PW=$(kubectl -n evalgate get secret postgres-credentials \
  -o jsonpath='{.data.POSTGRES_PASSWORD}' | base64 -d)

ssh -i ~/.ssh/evalgate_ed25519 ubuntu@<node-ip> \
  "cd /home/ubuntu/build/evalgate-api && sudo nerdctl --address /run/k3s/containerd/containerd.sock \
   --namespace k8s.io build --target test -f apps/api/Dockerfile -t evalgate-api:0.1.0-test ."

kubectl -n evalgate run api-tests --rm -i --restart=Never \
  --image=evalgate-api:0.1.0-test --image-pull-policy=IfNotPresent \
  --env="EVALGATE_TEST_DATABASE_URL=postgresql://evalgate:${PW}@postgres.evalgate.svc.cluster.local:5432/evalgate_test" \
  --command -- pytest apps/api -q -p no:cacheprovider
```

**`evalgate_test`, never `evalgate`.** Fixture teardown runs
`TRUNCATE baselines, runs, suites CASCADE`, so pointing that variable at the
application database destroys everything in it — which is exactly what happened
the first time these ran. The fixture now reads `current_database()` and aborts
the run unless the name ends in `_test`, but the habit is the real protection.

Note that this passes the password on a command line, so it lands in the pod spec
until the pod is removed. `--rm` deletes it when the run finishes.

### Verifying

```bash
curl http://64.181.195.241/health     # {"status":"ok"}   liveness, no DB
curl http://64.181.195.241/ready      # {"status":"ready"} readiness, queries Postgres
```

To prove the state really is in Postgres rather than in process memory, write
through the Service and then read back from **each pod individually** — under an
in-memory store the two would disagree:

```bash
for p in $(kubectl -n evalgate get pods -l app.kubernetes.io/name=evalgate-api \
             -o jsonpath='{.items[*].metadata.name}'); do
  echo "--- $p"
  kubectl -n evalgate exec "$p" -- /app/.venv/bin/python -c \
    "import json,urllib.request;print(json.load(urllib.request.urlopen('http://127.0.0.1:8000/suites')))"
done
```

---

## monitoring/

kube-prometheus-stack 88.1.3 (operator v0.93.0) on a 4 OCPU / 24 GB node that
also has to hold Airflow in P3 and Kafka in P4.

| File | What it is |
|---|---|
| `node-helm-setup.sh` | One-time: installs helm 3.21.3 on the node |
| `values.yaml` | Every component sized explicitly; five subcharts disabled |
| `dashboards/evalgate.json` | The committed dashboard, 13 panels across 3 rows |
| `apply.sh` | Runs helm on the node, then loads the dashboard ConfigMap |

```bash
scp -i ~/.ssh/evalgate_ed25519 infra/k8s/monitoring/node-helm-setup.sh ubuntu@<node-ip>:/tmp/
ssh -i ~/.ssh/evalgate_ed25519 ubuntu@<node-ip> 'sudo bash /tmp/node-helm-setup.sh'

export KUBECONFIG=~/.kube/evalgate.yaml
kubectl create namespace monitoring
kubectl -n monitoring create secret generic grafana-admin \
  --from-literal=admin-user=admin \
  --from-literal=admin-password="$(LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 32)"

EVALGATE_NODE_SSH="ssh -i ~/.ssh/evalgate_ed25519 ubuntu@<node-ip>" \
  ./infra/k8s/monitoring/apply.sh
```

helm 3, **not** 4 — kube-prometheus-stack and the P3 Airflow chart are tested
against 3, and a node that cannot be rebuilt is the wrong place to find out which
hooks helm 4 changed. It runs on the node for the same reason the image build
does; release state lives in-cluster as secrets, so the client's location does
not affect the result.

### Sizing

Chart defaults set **no resources at all** on Prometheus, and none on Grafana's
two sidecars. Every container is sized in `values.yaml`.

| Component | Requests | Limits | Measured |
|---|---|---|---|
| Prometheus | 400Mi / 200m | 1Gi / 1 | 223Mi |
| Grafana | 128Mi / 100m | 320Mi / 200m | 167Mi |
| Grafana sidecars (×2) | 48Mi / 10m | 96Mi / 50m | 72Mi each |
| kube-state-metrics | 48Mi / 20m | 128Mi / 100m | 20Mi |
| node-exporter | 32Mi / 20m | 64Mi / 100m | 9Mi |
| operator + config-reloader | 80Mi / 60m | 160Mi / 250m | 28Mi |
| **Total** | **784Mi** | **~1.8Gi** | **592Mi** |

That is 8% of the node's memory requests with everything else running, leaving
roughly 22 GB for Airflow and Kafka. **Memory is not the constraint on this node;
CPU is** — 4 OCPU, already 143% committed in limits.

**Watch `grafana.sidecar.resources`, not just `grafana.resources`.** Sizing the
Grafana container alone leaves two thirds of the pod unbounded, and
`kubectl top pods` reports the pod total, so it reads as if the Grafana limit
were being exceeded. Use `--containers`.

### What is disabled, and the part disabling does not do

`kubeControllerManager`, `kubeScheduler`, `kubeProxy`, `kubeEtcd`, `kubeApiServer`,
`alertmanager`, `defaultRules`, and Grafana's bundled dashboards are all off.
Reasons are in DECISIONS.md; the short version is that the first four would be
permanently `down` targets on k3s, `kubeApiServer` is the largest cardinality
source and feeds nothing here, and an Alertmanager with no receiver is the
appearance of monitoring rather than the substance.

**Disabling those subcharts does not remove their metrics on k3s.** k3s runs the
apiserver, etcd, controller manager, and kubelet in a single process, and that
process's `/metrics` serves all of them — so the toggles remove the separate
scrape *jobs* while the series keep arriving through the `kubelet` scrape. Left
alone, that was **44,260 of 53,201 active series**. What actually enforces the
decision is:

```yaml
kubelet:
  serviceMonitor:
    metricRelabelings:
      - sourceLabels: [__name__]
        regex: "(apiserver|etcd|workqueue|apiextensions_apiserver|aggregator|authentication|authorization)_.*"
        action: drop
```

which took total ingest to **16,192 series**. cadvisor is scraped from a
different endpoint with its own relabel list and is untouched.

### Retention and storage

`retention: 15d` **and** `retentionSize: 8GB`, on `local-path` over the boot
volume — not the Postgres block volume, because metrics are regenerable and the
remaining free block allowance is worth keeping for P4.

**`retentionSize` is the only real guard.** local-path is a hostPath bind with no
quota, so the `10Gi` PVC is advisory and would not stop Prometheus filling the
boot disk.

Scrape interval is 15s rather than the chart's 30s: k6 runs for minutes, and at
30s a load spike is four data points. Measured 1,582 samples/s → ~273 MB/day →
~4.1 GB at 15 days.

### The dashboard

`dashboards/evalgate.json` is the committed artifact. `apply.sh` generates a
ConfigMap from it labelled `grafana_dashboard=1`, which the Grafana sidecar
watches — so the file on disk stays the single source rather than being pasted
into a chart value.

Panels are grouped as **EvalGate API** (request rate, p95 latency, 5xx rate,
status classes, requests in flight), **Node** (CPU, RAM, disk split by mountpoint,
load average — this row is what P2's Record list asks for), and **Cluster**
(container memory as a percentage of its limit, container restarts).

Queries use a `datasource` template variable rather than a hard-coded UID, so the
dashboard imports cleanly regardless of what the provisioned datasource is named.

### Verifying

```bash
kubectl -n monitoring port-forward svc/monitoring-prometheus 9090:9090
curl -s 'http://127.0.0.1:9090/api/v1/targets?state=active' \
  | python3 -c "import json,sys;[print(t['health'], t['labels'].get('job')) for t in json.load(sys.stdin)['data']['activeTargets']]"

curl -s --get http://127.0.0.1:9090/api/v1/query \
  --data-urlencode 'query=sum by (handler) (rate(http_requests_total{job="evalgate-api"}[5m]))'
```

Grafana:

```bash
kubectl -n monitoring port-forward svc/monitoring-grafana 3000:80
kubectl -n monitoring get secret grafana-admin -o jsonpath='{.data.admin-password}' | base64 -d; echo
# then http://localhost:3000, dashboard uid evalgate-p2
```

When judging cardinality, use the per-job series count rather than
`prometheus_tsdb_head_series` — head keeps stale series for up to two hours after
a relabel change, so it lags by design.

---

## postgres/, continued

### Growing the volume

The remaining Always Free allowance is 100 GB (200 GB total, 50 boot + 50 pgdata).
Raise `pgdata_volume_size_in_gbs`, apply, then extend the filesystem on the node —
there is no partition table in the way, so it is a bare `resize2fs`:

```bash
ssh -i ~/.ssh/evalgate_ed25519 ubuntu@<node-ip> 'sudo resize2fs /dev/sdb'
```

Then raise `storage:` on the PV and PVC. Expansion is online and never touches
the instance.
