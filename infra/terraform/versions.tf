terraform {
  required_version = ">= 1.6.0"

  required_providers {
    oci = {
      source = "oracle/oci"
      # No upper pin here on purpose. `terraform init` resolves the current
      # major and writes the exact version to .terraform.lock.hcl, which IS
      # committed. The lock file is the pin; this constraint is the floor.
      version = ">= 6.0.0"
    }
  }
}

# Credentials come from ~/.oci/config, never from a .tfvars file or the repo.
# `oci setup config` writes that file; see README.md.
provider "oci" {
  auth                = "ApiKey"
  config_file_profile = var.oci_config_profile
  region              = var.region
}
