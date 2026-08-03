# --- Postgres data volume ----------------------------------------------------
#
# A dedicated block volume for the Postgres PVC, rather than k3s local-path on
# the boot volume. The reason is contention, not purity: by the end of P4 this
# single node also carries the Prometheus TSDB, Airflow, Kafka log segments, and
# trace rows, all of which grow unattended. The database is the one dataset on
# this node that cannot be regenerated from the repo, so it does not share a
# 44 GB disk with four services that can.
#
# Everything in this file is ADDITIVE. Nothing here modifies, taints, or
# replaces oci_core_instance.k3s — the A1 shape is a capacity lottery and that
# instance is not reobtainable on demand. The instance is only ever READ here
# (.id and .availability_domain), which produces no diff on it.

resource "oci_core_volume" "pgdata" {
  compartment_id = local.compartment_ocid
  display_name   = "${var.instance_display_name}-pgdata"

  # Read off the instance rather than recomputed from local.availability_domain.
  # A block volume can only attach to an instance in its own AD, and
  # local.availability_domain is derived from var.availability_domain_index,
  # which is the retry loop's cursor — it records which AD was tried last, not
  # which one the capacity lottery actually paid out on. Sourcing the AD from
  # the instance makes the two impossible to disagree.
  availability_domain = oci_core_instance.k3s.availability_domain

  size_in_gbs = var.pgdata_volume_size_in_gbs

  # Balanced. The existing 50 GB boot volume already runs at VPU 10 inside the
  # Always Free allowance at zero cost, so this is a proven-free setting rather
  # than an assumed-free one.
  vpus_per_gb = 10

  # Explicitly off. Performance auto-tuning raises VPU in response to load,
  # which is exactly how a zero-dollar volume quietly starts costing money.
  is_auto_tune_enabled = false

  lifecycle {
    # The database is the paid-for state on this node: 1,900 teacher answers and
    # every eval run and baseline. Terraform is never the right tool to delete
    # it, so it is not permitted to. Removing this block is a deliberate act.
    prevent_destroy = true
  }

  freeform_tags = {
    project = "evalgate"
    phase   = "P2"
    role    = "postgres-data"
  }
}

# Paravirtualized rather than iSCSI. An iSCSI attachment needs iscsiadm login
# commands run inside the guest after every attach, and needs them again after
# a reboot; a paravirtualized one appears as a plain /dev/sdX block device that
# the kernel finds on its own. On a node whose bootstrap can never be re-run
# from cloud-init, fewer manual in-guest steps is the whole game.
resource "oci_core_volume_attachment" "pgdata" {
  attachment_type = "paravirtualized"
  instance_id     = oci_core_instance.k3s.id
  volume_id       = oci_core_volume.pgdata.id
  display_name    = "${var.instance_display_name}-pgdata-attach"

  # Nothing in the guest addresses this disk by device path. The filesystem is
  # mounted by LABEL=pgdata from /etc/fstab, so a rename from /dev/sdb to
  # /dev/sdc across a reboot is a non-event.
}

output "pgdata_volume_id" {
  description = "OCID of the Postgres data volume. Needed to take an OCI volume backup; the tenancy has 5 free."
  value       = oci_core_volume.pgdata.id
}

output "pgdata_volume_summary" {
  description = "Postgres data volume as actually applied, for the DECISIONS.md record."
  value       = "${var.pgdata_volume_size_in_gbs} GB block volume, VPU 10, paravirtualized, in ${oci_core_instance.k3s.availability_domain}"
}
