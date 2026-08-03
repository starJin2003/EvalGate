#!/usr/bin/env bash
#
# EvalGate: fresh clone plus credentials -> a running public API.
#
#   ./up.sh
#
# This is a COMPOSER, not an implementation. Every step below already exists as
# a script that works on its own and is independently idempotent; this file's
# only jobs are ordering, preconditions, and the environment the individual
# steps assume. If a step needs changing, change the step, not this file.
#
# Why the deploys are not inside terraform: a local-exec provisioner would
# attach stateless side effects to terraform state, which means a failed deploy
# marks the resource tainted — and on this project the instance is a
# VM.Standard.A1.Flex obtained through a capacity lottery and can never be
# replaced. Terraform stops at infrastructure. The wrapper composes.
#
# Running it twice on a live system is a no-op. See "Idempotence" in the README.

set -euo pipefail

# --- environment the steps assume ---------------------------------------------
#
# NOT left to the operator's shell profile. Without it, every terraform state
# operation against OCI Object Storage fails with
#   403 SignatureDoesNotMatch: The secret key required to complete
#   authentication could not be found
# which points squarely at credentials and is completely misleading: the real
# cause is a streaming checksum trailer OCI does not implement. An operator who
# has not read DECISIONS.md would spend an hour rotating a working key.
export AWS_REQUEST_CHECKSUM_CALCULATION=when_required

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

TF_DIR="$REPO_ROOT/infra/terraform"
KUBECONFIG_PATH="${EVALGATE_KUBECONFIG:-$HOME/.kube/evalgate.yaml}"
API_IMAGE="${EVALGATE_API_IMAGE:-evalgate-api:0.1.0}"
AIRFLOW_IMAGE="${EVALGATE_AIRFLOW_IMAGE:-evalgate-airflow:3.2.2-dags}"
MODEL_IMAGE="${EVALGATE_MODEL_IMAGE:-evalgate-llama-server:b10210}"

# The model server is the one component this script cannot bring up from a
# fresh clone alone, and it says so rather than failing obscurely. It needs two
# gitignored artifacts that are outputs of P1.2/P1.3 on an Apple Silicon
# machine: the llama.cpp source tarball the image is built from, and the ~1 GB
# quantized GGUF. Both are reproducible - see README P1.3 - but neither is in
# git, so on a machine that has not run the training pipeline the model steps
# below skip with an explanation instead of dying.
MODEL_SRC="$REPO_ROOT/training/.scratch/llamacpp/src.tar.gz"

STEP=0
step() { STEP=$((STEP + 1)); printf '\n\033[1m[%d/9] %s\033[0m\n' "$STEP" "$*"; }
log()  { printf '      %s\n' "$*"; }
die()  { printf '\n\033[31mFATAL: %s\033[0m\n' "$*" >&2; exit 1; }

# Secrets are generated into files in here, never into shell variables that
# could be echoed and never into anything under the repo. Mode 700, removed on
# exit however the script ends.
SECRET_TMP="$(mktemp -d)"
chmod 700 "$SECRET_TMP"

TUNNEL_PID=""
cleanup() {
  rm -rf "$SECRET_TMP"
  [ -n "$TUNNEL_PID" ] && kill "$TUNNEL_PID" 2>/dev/null || true
}
trap cleanup EXIT

# ==============================================================================
step "Preflight — everything checked before anything is changed"
# ==============================================================================
#
# All of it up front and on purpose. A wrapper that discovers a missing tfvars
# file after provisioning an instance has done partial work on a system whose
# instance cannot be recreated.

missing_tools=()
for t in terraform kubectl ssh scp curl tar base64; do
  command -v "$t" >/dev/null 2>&1 || missing_tools+=("$t")
done
[ ${#missing_tools[@]} -eq 0 ] || die "missing required tools: ${missing_tools[*]}"

# Deliberately NOT checked for, and deliberately not used: docker, nerdctl,
# buildkit, podman. The API image is built on the node, natively for its own
# aarch64. Nothing is ever built on the operator's machine — the dev box here is
# an M1 that routinely has a multi-hour training run on it, and an image build is
# exactly the memory and IO spike that has already cost this project one run.
for t in docker podman nerdctl; do
  command -v "$t" >/dev/null 2>&1 && log "note: $t is installed but will not be used; images build on the node"
done

[ -f "$TF_DIR/terraform.tfvars" ] || die \
  "$TF_DIR/terraform.tfvars is missing. Copy terraform.tfvars.example and fill in tenancy_ocid and admin_cidr."
[ -f "$TF_DIR/backend.hcl" ] || die \
  "$TF_DIR/backend.hcl is missing. Copy backend.hcl.example and fill in your Object Storage namespace."
[ -f "$HOME/.oci/config" ] || die \
  "~/.oci/config is missing. Run 'oci setup config' and upload the public key in the console."

# The s3 backend reads the OCI Customer Secret Key from ~/.aws/credentials under
# the profile named in backend.hcl. Checked by name rather than assumed, because
# a missing profile produces that same misleading 403.
AWS_PROFILE_NAME="$(sed -n 's/^[[:space:]]*profile[[:space:]]*=[[:space:]]*"\(.*\)".*/\1/p' "$TF_DIR/backend.hcl" | head -1)"
if [ -n "$AWS_PROFILE_NAME" ]; then
  [ -f "$HOME/.aws/credentials" ] || die \
    "~/.aws/credentials is missing, but backend.hcl names profile '$AWS_PROFILE_NAME'. See infra/terraform/README.md."
  grep -q "^\[$AWS_PROFILE_NAME\]" "$HOME/.aws/credentials" || die \
    "profile '[$AWS_PROFILE_NAME]' not found in ~/.aws/credentials. It holds the OCI Customer Secret Key, not the API signing key."
fi

SSH_PUB="$(sed -n 's/^[[:space:]]*ssh_public_key_path[[:space:]]*=[[:space:]]*"\(.*\)".*/\1/p' "$TF_DIR/terraform.tfvars" | head -1)"
SSH_PUB="${SSH_PUB:-$HOME/.ssh/evalgate_ed25519.pub}"
SSH_PUB="${SSH_PUB/#\~/$HOME}"
[ -f "$SSH_PUB" ] || die "SSH public key not found at $SSH_PUB. Generate one: ssh-keygen -t ed25519 -f ${SSH_PUB%.pub} -N '' -C evalgate-oci-k3s"
[ -f "${SSH_PUB%.pub}" ] || die "SSH private key not found at ${SSH_PUB%.pub}. Terraform only reads the .pub half, but this script needs the private key to reach the node."

log "tools, credentials, tfvars, backend config, and SSH keypair all present"

# ==============================================================================
step "Terraform — infrastructure only, never deploys"
# ==============================================================================
(
  cd "$TF_DIR"
  terraform init -input=false -backend-config=backend.hcl >/dev/null
  # -auto-approve because this is the unattended path. The plan is still the
  # gate on a live system: with nothing to change, apply is a refresh and exits
  # having done nothing.
  terraform apply -input=false -auto-approve
)

# Parsed into its parts rather than string-munged. `terraform output ssh_command`
# is "ssh -i <key> ubuntu@<ip>"; the key and host are what scp and the tunnel
# need separately, and deriving an scp command by editing the ssh one is how you
# end up silently dropping the last option.
SSH_RAW="$(cd "$TF_DIR" && terraform output -raw ssh_command)"
# terraform emits a literal ~, which does not expand out of a variable.
SSH_RAW="${SSH_RAW//\~/$HOME}"
SSH_KEY="$(awk '{print $3}' <<<"$SSH_RAW")"
SSH_HOST="$(awk '{print $4}' <<<"$SSH_RAW")"
SSH_OPTS=(-i "$SSH_KEY" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15)
PUBLIC_IP="$(cd "$TF_DIR" && terraform output -raw public_ip)"

# The sub-scripts word-split this, so it has to be a plain string.
export EVALGATE_NODE_SSH="ssh ${SSH_OPTS[*]} $SSH_HOST"
log "node is $PUBLIC_IP"

# ==============================================================================
step "Wait for the node — SSH, then cloud-init's k3s bootstrap"
# ==============================================================================
#
# terraform apply returns as soon as OCI reports the instance RUNNING, which is
# minutes before cloud-init has finished installing k3s. On a live system both
# loops exit on the first attempt.

for i in $(seq 1 60); do
  ssh "${SSH_OPTS[@]}" "$SSH_HOST" true 2>/dev/null && break
  [ "$i" = 60 ] && die "SSH to $PUBLIC_IP never came up. Check admin_cidr covers your current address."
  sleep 5
done
log "ssh reachable"

for i in $(seq 1 120); do
  ssh "${SSH_OPTS[@]}" "$SSH_HOST" 'test -f /var/lib/evalgate/bootstrap.done' 2>/dev/null && break
  [ "$i" = 120 ] && die "k3s bootstrap never completed. Inspect: $(cd "$TF_DIR" && terraform output -raw bootstrap_status_command)"
  sleep 5
done
log "k3s bootstrap complete"

# ==============================================================================
step "Kubeconfig and the SSH tunnel"
# ==============================================================================
#
# Port 6443 has no ingress rule at all, so kubectl reaches the API through an
# SSH tunnel. The node's kubeconfig already points at 127.0.0.1:6443, so it is
# used unmodified.

mkdir -p "$(dirname "$KUBECONFIG_PATH")"
scp "${SSH_OPTS[@]}" "$SSH_HOST:.kube/config" "$KUBECONFIG_PATH" >/dev/null 2>&1 \
  || die "could not fetch the kubeconfig from the node"
chmod 600 "$KUBECONFIG_PATH"
export KUBECONFIG="$KUBECONFIG_PATH"

if kubectl version -o json >/dev/null 2>&1; then
  # An operator who already has a tunnel open should not have this script fail
  # on "address already in use", nor silently open a second one.
  log "cluster already reachable; reusing the existing tunnel"
else
  # Backgrounded with & rather than ssh -f, so $! is a real PID the trap can
  # kill. With -f, ssh forks and the parent exits, leaving nothing to clean up.
  ssh "${SSH_OPTS[@]}" -N -L 6443:127.0.0.1:6443 \
      -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 "$SSH_HOST" &
  TUNNEL_PID=$!
  for i in $(seq 1 15); do
    kubectl version -o json >/dev/null 2>&1 && break
    [ "$i" = 15 ] && die "opened a tunnel on 6443 but the cluster is still unreachable"
    sleep 1
  done
  log "tunnel opened as pid $TUNNEL_PID (closed on exit)"
fi
kubectl get nodes --no-headers | sed 's/^/      /'

# ==============================================================================
step "Node preparation — disk, image builder, helm"
# ==============================================================================
#
# All three refuse to redo work: node-volume-setup.sh hard-stops rather than
# formatting a disk that already has a filesystem, and the other two exit early
# if their binary is already installed.

for s in infra/k8s/postgres/node-volume-setup.sh \
         infra/k8s/api/node-build-setup.sh \
         infra/k8s/monitoring/node-helm-setup.sh; do
  name="$(basename "$s")"
  scp "${SSH_OPTS[@]}" "$s" "$SSH_HOST:/tmp/$name" >/dev/null
  ssh "${SSH_OPTS[@]}" "$SSH_HOST" "sudo bash /tmp/$name" 2>&1 | sed 's/^/      /'
done

# ==============================================================================
step "Secrets — generated if absent, never rotated, never printed"
# ==============================================================================
#
# A fresh clone has none of these, and none of them may ever enter the repo.
# The policy is generate-if-absent:
#
#   * generate, so that one command really does reach a running API;
#   * if-absent, so that a second run cannot rotate a credential out from under
#     a running system, which is what would break idempotence;
#   * never printed, and never passed as a command-line argument either — the
#     values go into mode-600 files in a temp dir and reach kubectl via
#     --from-file, so they are not visible in `ps` even momentarily.
#
# Read them back later with the commands printed at the end.

# The namespaces have to exist before the secrets can go into them, but the
# committed manifest is still the source: creating a bare namespace here and
# letting postgres/apply.sh add its labels a moment later means the two
# overwrite each other on every run — harmless, but it reports `configured`
# forever and that is exactly the noise idempotence is supposed to remove.
kubectl apply -f infra/k8s/postgres/00-namespace.yaml >/dev/null
kubectl create namespace monitoring --dry-run=client -o yaml | kubectl apply -f - >/dev/null

randpw() { LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c "${1:-32}"; }

# Alphanumeric only, deliberately: the API composes DATABASE_URL from these in
# its Deployment, and a password containing URL-significant characters would
# need percent-encoding that nothing in the chain performs.
if kubectl -n evalgate get secret postgres-credentials >/dev/null 2>&1; then
  log "postgres-credentials: exists, leaving alone"
else
  # The dangerous case: the secret is gone but the database is not.
  # POSTGRES_PASSWORD is read only by initdb, on a first boot against an empty
  # data directory — so a freshly generated password would produce a pod that
  # cannot authenticate against the existing volume. Refuse rather than create
  # a broken system.
  if kubectl -n evalgate get statefulset postgres >/dev/null 2>&1; then
    die "secret/postgres-credentials is missing but the postgres StatefulSet exists.
       A new password would not match the existing database: POSTGRES_PASSWORD is
       only read by initdb on a first boot. Recreate the secret with the ORIGINAL
       password, or set a new one with ALTER ROLE first. See infra/k8s/README.md."
  fi
  printf '%s' "evalgate"      > "$SECRET_TMP/POSTGRES_USER"
  printf '%s' "evalgate"      > "$SECRET_TMP/POSTGRES_DB"
  printf '%s' "$(randpw 32)"  > "$SECRET_TMP/POSTGRES_PASSWORD"
  kubectl -n evalgate create secret generic postgres-credentials \
    --from-file="$SECRET_TMP/POSTGRES_USER" \
    --from-file="$SECRET_TMP/POSTGRES_DB" \
    --from-file="$SECRET_TMP/POSTGRES_PASSWORD" >/dev/null
  log "postgres-credentials: generated"
fi

if kubectl -n evalgate get secret evalgate-api-token >/dev/null 2>&1; then
  log "evalgate-api-token: exists, leaving alone"
else
  printf '%s' "$(randpw 40)" > "$SECRET_TMP/EVALGATE_API_TOKEN"
  kubectl -n evalgate create secret generic evalgate-api-token \
    --from-file="$SECRET_TMP/EVALGATE_API_TOKEN" >/dev/null
  log "evalgate-api-token: generated"
fi

if kubectl -n monitoring get secret grafana-admin >/dev/null 2>&1; then
  log "grafana-admin: exists, leaving alone"
else
  printf '%s' "admin"        > "$SECRET_TMP/admin-user"
  printf '%s' "$(randpw 32)" > "$SECRET_TMP/admin-password"
  kubectl -n monitoring create secret generic grafana-admin \
    --from-file="$SECRET_TMP/admin-user" \
    --from-file="$SECRET_TMP/admin-password" >/dev/null
  log "grafana-admin: generated"
fi

# ==============================================================================
step "Build the API image — on the node, natively for aarch64"
# ==============================================================================
#
# Streams the build context over SSH as a tar and builds with BuildKit's
# containerd worker straight into k3s's k8s.io namespace. No registry, no
# buildx, no QEMU, and nothing built here. With unchanged sources this is a
# BuildKit cache hit producing the same image digest, so the deploy below sees
# no diff and no pods roll.
./infra/k8s/api/build-on-node.sh "$API_IMAGE" 2>&1 \
  | grep -vE "LIBARCHIVE|^#[0-9]+ (CACHED|extracting|sha256|transferring|DONE|resolve)" \
  | sed 's/^/      /' || true

# Airflow's DAGs are baked into an image rather than git-synced, so they need a
# build too. Same node, same BuildKit, same k8s.io namespace.
./infra/k8s/airflow/build-on-node.sh "$AIRFLOW_IMAGE" 2>&1 \
  | grep -vE "LIBARCHIVE|^#[0-9]+ (CACHED|extracting|sha256|transferring|DONE|resolve)" \
  | sed 's/^/      /' || true

# llama.cpp for the model server. Compiles from the pinned b10210 source on the
# node - ~4.5 min cold, a BuildKit cache hit thereafter.
if [ -f "$MODEL_SRC" ]; then
  ./infra/k8s/model/build-on-node.sh "$MODEL_IMAGE" 2>&1 \
    | grep -vE "LIBARCHIVE|^#[0-9]+ (CACHED|extracting|sha256|transferring|DONE|resolve)" \
    | sed 's/^/      /' || true
else
  log "model server: skipped, ${MODEL_SRC#"$REPO_ROOT"/} is absent (see README P1.3)"
fi

# ==============================================================================
step "Deploy — postgres, then monitoring, then api"
# ==============================================================================
#
# The order is a real dependency chain, not a preference:
#   postgres   creates the evalgate namespace the API's manifests live in;
#   monitoring installs the ServiceMonitor CRD, which api/apply.sh applies only
#              if present — so running api before monitoring silently skips the
#              scrape config;
#   api        needs both, plus the image built in the previous step.
./infra/k8s/postgres/apply.sh   2>&1 | sed 's/^/      /'
./infra/k8s/monitoring/apply.sh 2>&1 | sed 's/^/      /'
./infra/k8s/api/apply.sh        2>&1 | sed 's/^/      /'

# --- model server -------------------------------------------------------------
#
# After api and before airflow: it lives in the evalgate namespace postgres
# created, wants the ServiceMonitor CRD monitoring installed, and is a
# dependency of the daily eval DAG rather than of the public API.
#
# Gated on the weights actually being on the node. Applying without them gives
# Init:CrashLoopBackOff from the digest check - which is the correct behaviour
# for a corrupt file, but a confusing one for a file that was simply never
# uploaded, so the distinction is drawn here.
MODEL_GGUF="$(python3 -c "
import json
print(json.load(open('training/artifacts/p13/gguf_manifest.json'))['artifact'])
" 2>/dev/null || true)"

if [ -n "$MODEL_GGUF" ] && $EVALGATE_NODE_SSH "test -f /var/lib/evalgate/models/${MODEL_GGUF}" 2>/dev/null; then
  # EVALGATE_NODE_SSH is already exported above, so apply.sh inherits it.
  ./infra/k8s/model/apply.sh 2>&1 | sed 's/^/      /'
else
  log "model server: skipped, weights are not on the node"
  log "  ./infra/k8s/model/node-model-setup.sh && ./infra/k8s/model/upload-weights.sh"
fi

# --- airflow, last ------------------------------------------------------------
#
# After api, not before it. The public API is what this script advertises as
# live and what P2's exit criterion names; the orchestrator is not on its
# critical path and should not delay it.
#
# Airflow's secrets are provisioned here rather than in the secrets step above
# because the metadata secret requires a role and database inside Postgres, and
# Postgres only exists a few lines ago. Same generate-if-absent, never-rotate,
# never-print rule as the others; airflow/apply.sh still only ever checks.
kubectl apply -f infra/k8s/airflow/00-namespace.yaml >/dev/null

if kubectl -n airflow get secret airflow-metadata >/dev/null 2>&1; then
  log "airflow-metadata: exists, leaving alone"
else
  # Refuse rather than generate if the database already exists without the
  # secret — a fresh password would not match it, exactly as for Postgres.
  if kubectl -n evalgate exec postgres-0 -- psql -U evalgate -d postgres -tAc \
       "SELECT 1 FROM pg_database WHERE datname='airflow'" 2>/dev/null | grep -q 1; then
    die "secret/airflow-metadata is missing but database 'airflow' already exists.
       A new password would not match it. Recreate the secret with the ORIGINAL
       password, or ALTER ROLE airflow first. See infra/k8s/README.md."
  fi
  printf '%s' "$(randpw 32)" > "$SECRET_TMP/airflow_pw"
  kubectl -n evalgate exec -i postgres-0 -- psql -U evalgate -d postgres -v ON_ERROR_STOP=1 \
    -c "CREATE ROLE airflow LOGIN PASSWORD '$(cat "$SECRET_TMP/airflow_pw")';" >/dev/null
  kubectl -n evalgate exec -i postgres-0 -- psql -U evalgate -d postgres -v ON_ERROR_STOP=1 \
    -c "CREATE DATABASE airflow OWNER airflow;" >/dev/null
  printf 'postgresql://airflow:%s@postgres.evalgate.svc.cluster.local:5432/airflow' \
    "$(cat "$SECRET_TMP/airflow_pw")" > "$SECRET_TMP/connection"
  kubectl -n airflow create secret generic airflow-metadata \
    --from-file="$SECRET_TMP/connection" >/dev/null
  log "airflow-metadata: generated (role + database created)"
fi

# jwt, api-secret-key and fernet-key are NOT hygiene. The chart regenerates each
# on every helm upgrade when unset, which breaks running DAGs and — for the
# fernet key — makes everything already stored in the metadata DB undecryptable.
for pair in airflow-jwt:jwt-secret airflow-api-secret-key:api-secret-key airflow-fernet-key:fernet-key; do
  name="${pair%%:*}"; key="${pair##*:}"
  if kubectl -n airflow get secret "$name" >/dev/null 2>&1; then
    log "$name: exists, leaving alone"
  else
    if [ "$key" = "fernet-key" ]; then
      # Fernet needs a 32-byte urlsafe-base64 key, not an arbitrary string.
      python3 -c "import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode(),end='')" > "$SECRET_TMP/$key"
    else
      printf '%s' "$(randpw 48)" > "$SECRET_TMP/$key"
    fi
    kubectl -n airflow create secret generic "$name" --from-file="$SECRET_TMP/$key" >/dev/null
    log "$name: generated"
  fi
done

if kubectl -n airflow get secret airflow-admin >/dev/null 2>&1; then
  log "airflow-admin: exists, leaving alone"
else
  printf '%s' "$(randpw 32)" > "$SECRET_TMP/password"
  kubectl -n airflow create secret generic airflow-admin --from-file="$SECRET_TMP/password" >/dev/null
  log "airflow-admin: generated"
fi

./infra/k8s/airflow/apply.sh 2>&1 | sed 's/^/      /'
# Creates the FAB tables and the admin user. Idempotent, and required: the
# chart's migrate job runs only `airflow db migrate`, so FAB's own tables would
# otherwise be missing and the first login would fail.
./infra/k8s/airflow/bootstrap-admin.sh 2>&1 | tail -4 | sed 's/^/      /'

# ==============================================================================
step "Verify — against the public address, not a port-forward"
# ==============================================================================
#
# svclb and traefik can take a few seconds to publish on a freshly booted node.
for i in $(seq 1 30); do
  code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 "http://$PUBLIC_IP/health" 2>/dev/null || echo 000)"
  [ "$code" = "200" ] && break
  [ "$i" = 30 ] && die "the API never answered on http://$PUBLIC_IP/health (last status $code)"
  sleep 4
done
log "GET /health  -> $(curl -sS --max-time 10 "http://$PUBLIC_IP/health")"
log "GET /ready   -> $(curl -sS --max-time 10 "http://$PUBLIC_IP/ready")"

# ==============================================================================
printf '\n\033[1;32mEvalGate is up.\033[0m\n\n'
cat <<EOF
  Public API   http://$PUBLIC_IP

    curl http://$PUBLIC_IP/health
    curl http://$PUBLIC_IP/ready
    curl http://$PUBLIC_IP/suites

  Writes need the bearer token; reads, /health, /ready and /gate do not.

    export KUBECONFIG=$KUBECONFIG_PATH
    TOKEN=\$(kubectl -n evalgate get secret evalgate-api-token \\
      -o jsonpath='{.data.EVALGATE_API_TOKEN}' | base64 -d)

  Grafana is ClusterIP only — there is no TLS on a bare IP, so the admin
  login stays off the public internet:

    kubectl -n monitoring port-forward svc/monitoring-grafana 3000:80
    kubectl -n monitoring get secret grafana-admin \\
      -o jsonpath='{.data.admin-password}' | base64 -d; echo

  Load test:  ./infra/k6/seed.sh && ./infra/k6/run.sh && ./infra/k6/teardown.sh
EOF
