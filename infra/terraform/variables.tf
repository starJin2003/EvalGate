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
    Source CIDR allowed to reach SSH (22) and the k3s API (6443). Defaults to
    the whole internet so an unattended retry loop is never blocked on a
    changed home IP. Narrow this to <your-ip>/32 in terraform.tfvars once the
    instance exists. Ports 80 and 443 are open to 0.0.0.0/0 regardless.
  EOT
  type        = string
  default     = "0.0.0.0/0"
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
  description = "Path to the SSH public key injected into the ubuntu user."
  type        = string
  default     = "~/.ssh/id_ed25519.pub"
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
