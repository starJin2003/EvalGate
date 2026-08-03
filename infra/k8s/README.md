# infra/k8s

k3s manifests for the API, Postgres, model server, kube-prometheus-stack, and Airflow.
Lands across P2 and P3.

| Directory | Status |
|---|---|
| `postgres/` | **Live as of 2026-08-03.** Postgres 16.14 + pgvector 0.8.6 on a dedicated OCI block volume |
| api | Not built yet (P2) |
| model server | Not built yet (P2) |
| kube-prometheus-stack | Not built yet (P2) |
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

### Growing the volume

The remaining Always Free allowance is 100 GB (200 GB total, 50 boot + 50 pgdata).
Raise `pgdata_volume_size_in_gbs`, apply, then extend the filesystem on the node —
there is no partition table in the way, so it is a bare `resize2fs`:

```bash
ssh -i ~/.ssh/evalgate_ed25519 ubuntu@<node-ip> 'sudo resize2fs /dev/sdb'
```

Then raise `storage:` on the PV and PVC. Expansion is online and never touches
the instance.
