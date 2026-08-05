#!/usr/bin/env bash
# Prepare the node directories that hold the model weights and the eval inputs.
# Run from the repo root on the workstation:
#
#   ./infra/k8s/model/node-model-setup.sh
#
# Idempotent. Creates two directories on the BOOT volume and refuses to run if
# either one resolves onto the Postgres block volume.
#
# Why the boot volume and not /mnt/pgdata:
#
#   The weights are re-derivable. `stage_selected_adapter.sh` plus the P1.3
#   pipeline reproduce this exact sha256 from committed adapters. The database is
#   not re-derivable and has no backup (PROGRESS section 6). /mnt/pgdata is
#   guarded three ways precisely because losing it is unrecoverable -
#   `prevent_destroy` on the terraform volume, `Retain` on the PV and the
#   StorageClass, and node-volume-setup.sh refusing to format a disk that already
#   carries a filesystem. Putting a 1 GB file that can be rebuilt in 32 s inside
#   that blast radius means every future model swap is a write against the one
#   volume the project cannot lose. A full disk there stops being a model
#   incident and becomes a database incident.
#
#   The PV shape also does not permit it cleanly: postgres/20-pv.yaml is a static
#   local PV over /mnt/pgdata/data with a single RWO claim. A second consumer
#   needs either a second PV over the same filesystem - two capacity declarations
#   over one real disk, which is fiction - or a share of that single claim.
#
# The guard below is the load-bearing part of this script. Everything else is
# mkdir.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

MODELS_DIR=/var/lib/evalgate/models
EVAL_DIR=/var/lib/evalgate/eval
# Results live apart from weights and inputs. Owned by the container's uid, not
# by ubuntu: this backs a `local` PV, and `fsGroup` does not relabel local
# volumes, so a pod running as 10001 cannot fix the ownership for itself. The
# first full-suite attempt died on exactly this, at the final write, after the
# inference was already done.
RESULTS_DIR=/var/lib/evalgate/results
# Must match runAsUser/runAsGroup in 40-deployment.yaml and 70-eval-job.yaml.
CONTAINER_UID=10001
FORBIDDEN_MOUNT=/mnt/pgdata

log() { printf '[model-setup] %s\n' "$*"; }

if [ -z "${EVALGATE_NODE_SSH:-}" ]; then
  NODE_SSH="$(cd "${HERE}/../../terraform" && AWS_REQUEST_CHECKSUM_CALCULATION=when_required terraform output -raw ssh_command 2>/dev/null)" || true
  [ -n "${NODE_SSH:-}" ] || {
    log "FATAL: EVALGATE_NODE_SSH is unset and 'terraform output ssh_command' failed."
    log 'Set it explicitly:  EVALGATE_NODE_SSH="ssh -i ~/.ssh/evalgate_ed25519 ubuntu@<node-ip>"'
    exit 1
  }
  # terraform emits a literal ~, which does not expand out of a variable.
  NODE_SSH="${NODE_SSH//\~/$HOME}"
else
  NODE_SSH="${EVALGATE_NODE_SSH}"
fi

log "Preparing ${MODELS_DIR} and ${EVAL_DIR} on the node."

# shellcheck disable=SC2029  # every variable here is meant to expand locally
$NODE_SSH "set -euo pipefail

  # The guard, before any mkdir. findmnt --target walks up to the mount point
  # that actually backs the path, so this is answered by the kernel's mount
  # table rather than by string-matching a path prefix - a bind mount or a
  # symlink into /mnt/pgdata would defeat the string check and not this one.
  #
  # It runs against the PARENT that already exists, because findmnt on a path
  # that does not exist yet resolves to the nearest existing ancestor anyway;
  # naming /var/lib/evalgate makes that explicit rather than incidental.
  pgdata_src=\$(findmnt -no SOURCE --target ${FORBIDDEN_MOUNT} 2>/dev/null || true)
  target_src=\$(findmnt -no SOURCE --target /var/lib/evalgate)

  if [ -n \"\$pgdata_src\" ] && [ \"\$pgdata_src\" = \"\$target_src\" ]; then
    echo \"FATAL: /var/lib/evalgate is backed by \$target_src, which is also ${FORBIDDEN_MOUNT}.\" >&2
    echo 'The model weights must not live on the Postgres block volume. Refusing.' >&2
    exit 1
  fi

  sudo mkdir -p ${MODELS_DIR} ${EVAL_DIR} ${RESULTS_DIR}
  # Owned by ubuntu so the upload does not need sudo on every rsync, and the
  # kubelet reads these through readOnly mounts regardless of owner.
  sudo chown ubuntu:ubuntu ${MODELS_DIR} ${EVAL_DIR}
  sudo chmod 755 ${MODELS_DIR} ${EVAL_DIR}
  # Results are the one directory a container must WRITE, so it is owned by the
  # container's uid rather than by ubuntu.
  sudo chown ${CONTAINER_UID}:${CONTAINER_UID} ${RESULTS_DIR}
  sudo chmod 775 ${RESULTS_DIR}

  echo \"models  -> ${MODELS_DIR} on \$target_src\"
  echo \"eval    -> ${EVAL_DIR} on \$target_src\"
  echo \"results -> ${RESULTS_DIR} on \$target_src (uid ${CONTAINER_UID}, writable)\"
  echo \"pgdata  -> \${pgdata_src:-<not mounted>} (must differ from the above)\"
  df -h /var/lib/evalgate | tail -1
"

log "Done."
