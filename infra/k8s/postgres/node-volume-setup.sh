#!/usr/bin/env bash
# Prepare the OCI block volume created by infra/terraform/storage.tf to back the
# Postgres PV. Run once on the k3s node, over SSH, as a user with sudo.
#
#   scp -i ~/.ssh/evalgate_ed25519 infra/k8s/postgres/node-volume-setup.sh \
#       ubuntu@<node>:/tmp/ && ssh -i ~/.ssh/evalgate_ed25519 ubuntu@<node> \
#       'sudo bash /tmp/node-volume-setup.sh'
#
# This does NOT live in cloud-init/k3s.yaml.tftpl, and cannot: user_data runs
# only at first boot, and this node will never boot fresh again — the A1 shape
# is a capacity lottery and the instance is not replaceable on demand. So this
# is a standalone script instead of a bootstrap edit, and it is idempotent so a
# re-run on an already-provisioned node is a no-op rather than a data loss.

set -euo pipefail

DEVICE="${1:-/dev/sdb}"
LABEL="pgdata"
MOUNTPOINT="/mnt/pgdata"
DATADIR="${MOUNTPOINT}/data"

# The postgres uid/gid inside pgvector/pgvector:pg16, which is the official
# postgres image plus the extension.
PG_UID=999
PG_GID=999

log() { printf '[node-volume-setup] %s\n' "$*"; }

[ -b "$DEVICE" ] || { log "FATAL: $DEVICE is not a block device."; exit 1; }

# --- Filesystem --------------------------------------------------------------
# The single most destructive thing this script could do is mkfs a disk that
# already holds the database, so existence of ANY filesystem is a hard stop on
# formatting, not a prompt. blkid exits 2 on a device with no signature.
if existing_fstype="$(blkid -s TYPE -o value "$DEVICE" 2>/dev/null)"; then
  existing_label="$(blkid -s LABEL -o value "$DEVICE" 2>/dev/null || true)"
  log "$DEVICE already has a $existing_fstype filesystem labelled '${existing_label:-<none>}'. Not formatting."
  if [ "$existing_label" != "$LABEL" ]; then
    log "FATAL: expected label '$LABEL'. Refusing to touch an unrecognised disk."
    exit 1
  fi
else
  log "No filesystem on $DEVICE. Creating ext4 labelled '$LABEL'."
  # No partition table. A whole-disk filesystem is one less layer to resize
  # later: an OCI online volume expansion becomes a bare resize2fs, with no
  # partition table edit on a mounted disk in between.
  mkfs.ext4 -L "$LABEL" -m 0 "$DEVICE"
fi

# --- Mount -------------------------------------------------------------------
mkdir -p "$MOUNTPOINT"

# By LABEL, never by device path. Device names are assigned in attach order and
# are not stable across a reboot or a second volume; the label travels with the
# filesystem. _netdev and nofail together mean a volume that is slow to attach
# delays the boot instead of dropping the node to an emergency shell — which,
# on a host reachable only over SSH, is the difference between a wait and a
# trip to the OCI serial console.
FSTAB_LINE="LABEL=${LABEL}  ${MOUNTPOINT}  ext4  defaults,_netdev,nofail  0  2"
if grep -q "LABEL=${LABEL}[[:space:]]" /etc/fstab; then
  log "/etc/fstab already has a LABEL=${LABEL} entry."
else
  log "Adding LABEL=${LABEL} to /etc/fstab."
  printf '%s\n' "$FSTAB_LINE" >> /etc/fstab
fi

# systemd generates mnt-pgdata.mount from /etc/fstab at reload, not at write.
# Without this the entry is correct on disk but unknown to the running systemd,
# and `findmnt --verify` warns about exactly that.
systemctl daemon-reload

if mountpoint -q "$MOUNTPOINT"; then
  log "$MOUNTPOINT already mounted."
else
  log "Mounting $MOUNTPOINT."
  mount "$MOUNTPOINT"
fi

# Proves the fstab entry itself is correct, not just that a manual mount worked.
findmnt --verify --fstab >/dev/null || { log "FATAL: /etc/fstab does not verify."; exit 1; }

# --- Data directory ----------------------------------------------------------
# Postgres gets a subdirectory rather than the mount root: ext4 puts lost+found
# at the root of the filesystem, and initdb refuses to initialise a directory
# that is not empty.
mkdir -p "$DATADIR"
chown "${PG_UID}:${PG_GID}" "$DATADIR"
chmod 0700 "$DATADIR"

log "Done."
findmnt -no SOURCE,TARGET,FSTYPE,OPTIONS "$MOUNTPOINT"
df -h "$MOUNTPOINT" | tail -1
