locals {
  compartment_ocid = var.compartment_ocid != "" ? var.compartment_ocid : var.tenancy_ocid
}

# --- Availability domains ----------------------------------------------------
# The retry wrapper cycles availability_domain_index; the modulo keeps a 1-AD
# region from erroring out on index 1 or 2.

data "oci_identity_availability_domains" "this" {
  compartment_id = var.tenancy_ocid
}

locals {
  availability_domains = data.oci_identity_availability_domains.this.availability_domains
  ad_count             = length(local.availability_domains)
  ad_index             = var.availability_domain_index % local.ad_count
  availability_domain  = local.availability_domains[local.ad_index].name
}

# --- Existing network --------------------------------------------------------
# Looked up, never created. The VCN and public subnet already exist in
# us-chicago-1 and are not this configuration's to own or destroy.

data "oci_core_vcns" "this" {
  compartment_id = local.compartment_ocid
  display_name   = var.vcn_display_name
}

# Deliberately not filtered by vcn_id. Passing a null vcn_id when the VCN
# lookup misses produces an opaque "invalid parameter" error from the provider
# before any precondition can run, so the VCN filter is applied in locals below
# where a miss surfaces as a readable message.
data "oci_core_subnets" "this" {
  compartment_id = local.compartment_ocid
}

locals {
  matched_vcns = data.oci_core_vcns.this.virtual_networks
  vcn_id       = length(local.matched_vcns) > 0 ? local.matched_vcns[0].id : null

  all_subnets = [for s in data.oci_core_subnets.this.subnets : s if s.vcn_id == local.vcn_id]

  # Either the subnet named in tfvars, or the first one that permits public IPs.
  named_subnets  = [for s in local.all_subnets : s if s.display_name == var.public_subnet_display_name]
  public_subnets = [for s in local.all_subnets : s if s.prohibit_public_ip_on_vnic == false]
  candidate_subnets = (
    var.public_subnet_display_name != "" ? local.named_subnets : local.public_subnets
  )
  subnet_id = length(local.candidate_subnets) > 0 ? local.candidate_subnets[0].id : null
}

# --- Image -------------------------------------------------------------------
# Resolved by name rather than pinned to an OCID, because image OCIDs are
# per-region and rotate on every Canonical release. Filtering on the A1 shape
# is what makes this aarch64; "Minimal" builds are dropped because they omit
# cloud-init modules this config relies on.

data "oci_core_images" "ubuntu" {
  compartment_id           = local.compartment_ocid
  operating_system         = "Canonical Ubuntu"
  operating_system_version = "24.04"
  shape                    = "VM.Standard.A1.Flex"
  sort_by                  = "TIMECREATED"
  sort_order               = "DESC"
}

locals {
  ubuntu_images = [
    for img in data.oci_core_images.ubuntu.images : img
    if !can(regex("(?i)minimal", img.display_name))
  ]
  image_id   = length(local.ubuntu_images) > 0 ? local.ubuntu_images[0].id : null
  image_name = length(local.ubuntu_images) > 0 ? local.ubuntu_images[0].display_name : null
}

# --- Network security group --------------------------------------------------
# An NSG attached to this instance's VNIC, rather than editing the existing
# subnet's security list. Additive and self-contained: destroying this stack
# leaves the shared VCN exactly as it was found.

resource "oci_core_network_security_group" "k3s" {
  compartment_id = local.compartment_ocid
  vcn_id         = local.vcn_id
  display_name   = "${var.instance_display_name}-nsg"

  freeform_tags = {
    project = "evalgate"
    phase   = "P2"
  }
}

resource "oci_core_network_security_group_security_rule" "egress_all" {
  network_security_group_id = oci_core_network_security_group.k3s.id
  direction                 = "EGRESS"
  protocol                  = "all"
  destination               = "0.0.0.0/0"
  destination_type          = "CIDR_BLOCK"
  description               = "All outbound. k3s pulls images and charts."
}

resource "oci_core_network_security_group_security_rule" "ingress_ssh" {
  network_security_group_id = oci_core_network_security_group.k3s.id
  direction                 = "INGRESS"
  protocol                  = "6" # TCP
  source                    = var.admin_cidr
  source_type               = "CIDR_BLOCK"
  description               = "SSH"

  tcp_options {
    destination_port_range {
      min = 22
      max = 22
    }
  }
}

resource "oci_core_network_security_group_security_rule" "ingress_kube_api" {
  network_security_group_id = oci_core_network_security_group.k3s.id
  direction                 = "INGRESS"
  protocol                  = "6"
  source                    = var.admin_cidr
  source_type               = "CIDR_BLOCK"
  description               = "k3s API server for kubectl and helm from the Mac"

  tcp_options {
    destination_port_range {
      min = 6443
      max = 6443
    }
  }
}

resource "oci_core_network_security_group_security_rule" "ingress_http" {
  network_security_group_id = oci_core_network_security_group.k3s.id
  direction                 = "INGRESS"
  protocol                  = "6"
  source                    = "0.0.0.0/0"
  source_type               = "CIDR_BLOCK"
  description               = "HTTP to the traefik ingress"

  tcp_options {
    destination_port_range {
      min = 80
      max = 80
    }
  }
}

resource "oci_core_network_security_group_security_rule" "ingress_https" {
  network_security_group_id = oci_core_network_security_group.k3s.id
  direction                 = "INGRESS"
  protocol                  = "6"
  source                    = "0.0.0.0/0"
  source_type               = "CIDR_BLOCK"
  description               = "HTTPS to the traefik ingress"

  tcp_options {
    destination_port_range {
      min = 443
      max = 443
    }
  }
}

# Without this, path MTU discovery breaks and large responses hang instead of
# failing, which is a miserable thing to debug over a public API.
resource "oci_core_network_security_group_security_rule" "ingress_icmp_pmtu" {
  network_security_group_id = oci_core_network_security_group.k3s.id
  direction                 = "INGRESS"
  protocol                  = "1" # ICMP
  source                    = "0.0.0.0/0"
  source_type               = "CIDR_BLOCK"
  description               = "Path MTU discovery"

  icmp_options {
    type = 3
    code = 4
  }
}

# --- Instance ----------------------------------------------------------------

resource "oci_core_instance" "k3s" {
  availability_domain = local.availability_domain
  compartment_id      = local.compartment_ocid
  display_name        = var.instance_display_name
  shape               = "VM.Standard.A1.Flex"

  shape_config {
    ocpus         = var.ocpus
    memory_in_gbs = var.memory_in_gbs
  }

  source_details {
    source_type             = "image"
    source_id               = local.image_id
    boot_volume_size_in_gbs = var.boot_volume_size_in_gbs
  }

  create_vnic_details {
    subnet_id        = local.subnet_id
    assign_public_ip = true
    hostname_label   = var.instance_display_name
    nsg_ids          = [oci_core_network_security_group.k3s.id]
  }

  metadata = {
    ssh_authorized_keys = file(pathexpand(var.ssh_public_key_path))
    user_data = base64encode(templatefile("${path.module}/cloud-init/k3s.yaml.tftpl", {
      k3s_version = var.k3s_version
    }))
  }

  agent_config {
    are_all_plugins_disabled = false
    is_management_disabled   = false
    is_monitoring_disabled   = false
  }

  # Every dependency is resolved before OCI is asked for capacity, so a failed
  # attempt fails on capacity alone and the retry log stays readable.
  lifecycle {
    precondition {
      condition     = local.vcn_id != null
      error_message = "No VCN named '${var.vcn_display_name}' in this compartment and region. Check vcn_display_name, compartment_ocid, and region."
    }

    precondition {
      condition = local.subnet_id != null
      error_message = (
        var.public_subnet_display_name != ""
        ? "No subnet named '${var.public_subnet_display_name}' in VCN '${var.vcn_display_name}'. Copy the display name verbatim from the console."
        : "No subnet in VCN '${var.vcn_display_name}' permits public IPs. Set public_subnet_display_name explicitly."
      )
    }

    precondition {
      condition     = local.image_id != null
      error_message = "No Canonical Ubuntu 24.04 aarch64 image found for VM.Standard.A1.Flex in ${var.region}."
    }

    # A new Canonical image release must never destroy and recreate an instance
    # that took days of capacity retries to obtain.
    ignore_changes = [source_details[0].source_id]
  }

  # Shorter than the 20 minute provider default. A launch that has not returned
  # in 15 minutes is not going to, and the retry loop should move to the next AD
  # rather than sit on a stalled call.
  timeouts {
    create = "15m"
  }

  freeform_tags = {
    project = "evalgate"
    phase   = "P2"
    role    = "k3s-server"
  }
}
