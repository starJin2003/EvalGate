# --- Auth and placement -----------------------------------------------------

variable "oci_config_profile" {
  description = "Profile in ~/.oci/config to authenticate with."
  type        = string
  default     = "DEFAULT"
}

variable "region" {
  description = "OCI region. Permanent per tenancy once the home region is set."
  type        = string
  default     = "us-chicago-1"
}

variable "tenancy_ocid" {
  description = "Tenancy OCID. Console: profile menu, then the tenancy row."
  type        = string

  validation {
    condition     = startswith(var.tenancy_ocid, "ocid1.tenancy.")
    error_message = "tenancy_ocid must start with ocid1.tenancy."
  }
}

variable "compartment_ocid" {
  description = "Compartment to launch into. Leave empty to use the root compartment (the tenancy)."
  type        = string
  default     = ""
}

variable "availability_domain_index" {
  description = <<-EOT
    Which availability domain to try, 0-based, taken in the order OCI lists
    them. The retry wrapper cycles this over the ADs the region reports; it is
    a variable rather than an AD name so the loop never needs the names up
    front. Wraps modulo the AD count, so a 1-AD region still applies cleanly.
  EOT
  type        = number
  default     = 0

  validation {
    condition     = var.availability_domain_index >= 0
    error_message = "availability_domain_index must be 0 or greater."
  }
}

# --- Existing network (data sources, never created here) ---------------------

variable "vcn_display_name" {
  description = "Display name of the existing VCN. Looked up, not created."
  type        = string
  default     = "evalgate-vcn"
}

variable "public_subnet_display_name" {
  description = <<-EOT
    Display name of the existing public subnet inside that VCN. Leave empty to
    auto-select the first subnet in the VCN that permits public IPs. Copy it
    verbatim from the console including spaces, e.g. "public subnet-evalgate-vcn".
  EOT
  type        = string
  default     = ""
}

variable "admin_cidr" {
  description = <<-EOT
    Source CIDR allowed to reach SSH (port 22). This is the ONLY thing it
    gates. The k3s API is not exposed to the internet at all; reach it with an
    SSH tunnel (see the kubectl_tunnel_command output). Public web traffic is
    governed separately by public_ingress_cidr, which never inherits this
    value, so narrowing this can never take the API offline.

    Set it to <your-ip>/32. Find it with: curl -s https://checkip.amazonaws.com

    LOCKED OUT BECAUSE YOUR IP CHANGED? Do not panic and do not rebuild. Edit
    the NSG rule in the OCI console from any browser:
      Networking > Virtual cloud networks > evalgate-vcn >
      Network security groups > evalgate-k3s-nsg > Security rules >
      edit the port 22 ingress rule's source CIDR.
    Then update admin_cidr here so terraform stops trying to revert it. The
    recovery path is a browser, not SSH, so a wrong value here is never fatal.
  EOT
  type        = string
  default     = "0.0.0.0/0"

  validation {
    # World-open SSH is almost never what is wanted, and the console recovery
    # path above means a too-narrow value costs a browser visit rather than the
    # instance. Being wrong in the safe direction is cheap here.
    condition     = var.admin_cidr != "0.0.0.0/0"
    error_message = "admin_cidr must not be 0.0.0.0/0. Set it to <your-ip>/32; if the address changes, fix the rule in the OCI console."
  }
}

variable "public_ingress_cidr" {
  description = <<-EOT
    Source CIDR for the deliberately-public web ports, 80 and 443. Separate
    from admin_cidr on purpose and by default open to the whole internet.

    These have to stay public: from P3 onward the eval gate is called by GitHub
    Actions runners, whose egress addresses are a large, rotating range that
    cannot be usefully allowlisted. Keeping them in their own variable means
    tightening SSH access is a one-line change that cannot accidentally take
    the public API down, and opening the web ports can never widen SSH.
  EOT
  type        = string
  default     = "0.0.0.0/0"
}

variable "extra_tls_sans" {
  description = <<-EOT
    Additional SANs for the k3s API server certificate. Empty is correct while
    the API is reachable only through an SSH tunnel, since k3s always includes
    127.0.0.1.

    The node's own public IP is deliberately NOT fetched at boot: OCI's
    instance metadata service (/opc/v2/vnics/) reports only private addressing
    and has no publicIp field at all. If the API is ever exposed to a fixed
    /32, put the address here rather than reintroducing a metadata lookup.
  EOT
  type        = list(string)
  default     = []
}

# --- Instance ---------------------------------------------------------------

variable "instance_display_name" {
  description = "Name and hostname label for the instance. Letters, digits, hyphens."
  type        = string
  default     = "evalgate-k3s"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,62}$", var.instance_display_name))
    error_message = "instance_display_name must be a valid hostname label: lowercase, starts with a letter."
  }
}

variable "ocpus" {
  description = <<-EOT
    OCPUs on the VM.Standard.A1.Flex shape. 4 is the Always Free allowance on a
    PAYG tenancy and is what this project is sized for. Dropping to 2 is a
    documented contingency in BUILD_PLAN.md P2, not a workaround for a capacity
    error, and it is a DECISION POINT. retry-apply.sh refuses to run below 4
    without an explicit --allow-downsize flag.
  EOT
  type        = number
  default     = 4

  validation {
    condition     = var.ocpus >= 1 && var.ocpus <= 4 && floor(var.ocpus) == var.ocpus
    error_message = "ocpus must be a whole number from 1 to 4 to stay inside the free allowance."
  }
}

variable "memory_in_gbs" {
  description = "Memory in GB. The A1 free allowance is 6 GB per OCPU, so 24 at 4 OCPUs."
  type        = number
  default     = 24

  validation {
    condition     = var.memory_in_gbs >= 1 && var.memory_in_gbs <= 24
    error_message = "memory_in_gbs must be between 1 and 24 to stay inside the free allowance."
  }
}

variable "boot_volume_size_in_gbs" {
  description = <<-EOT
    Boot volume size. The Always Free block storage allowance is 200 GB total
    across all volumes; 50 leaves room for the Postgres PVC added later in P2.
  EOT
  type        = number
  default     = 50

  validation {
    condition     = var.boot_volume_size_in_gbs >= 50 && var.boot_volume_size_in_gbs <= 200
    error_message = "boot_volume_size_in_gbs must be between 50 and 200."
  }
}

variable "ssh_public_key_path" {
  description = <<-EOT
    Path to the SSH public key injected into the ubuntu user. Defaults to a
    project-scoped key rather than ~/.ssh/id_ed25519.pub so this stack does not
    depend on, or claim, the machine's default identity. Generate with:
      ssh-keygen -t ed25519 -f ~/.ssh/evalgate_ed25519 -N "" -C "evalgate-oci-k3s"
  EOT
  type        = string
  default     = "~/.ssh/evalgate_ed25519.pub"

  validation {
    condition     = endswith(var.ssh_public_key_path, ".pub")
    error_message = "ssh_public_key_path must point at the PUBLIC key, ending in .pub."
  }
}

variable "k3s_version" {
  description = <<-EOT
    k3s version to pin, e.g. "v1.33.4+k3s1". Empty installs the current stable
    channel. Pin this after the first successful boot so the node is
    reproducible; leaving it empty means a rebuild can land on a different k3s.
  EOT
  type        = string
  default     = ""
}
