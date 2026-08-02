terraform {
  # OCI has no native Terraform backend. Object Storage exposes an
  # S3-compatible API, so the standard `s3` backend drives it directly.
  #
  # This is a PARTIAL configuration. The tenancy-specific half lives in
  # backend.hcl, which is gitignored, and is supplied at init:
  #
  #   terraform init -backend-config=backend.hcl
  #
  # Splitting it that way keeps a namespace and bucket that only exist in one
  # tenancy out of a public repo, and lets a stranger point the same
  # configuration at their own bucket by copying backend.hcl.example.
  backend "s3" {
    key = "evalgate/p2/terraform.tfstate"

    # Everything below is about talking to something that is not AWS.
    #
    # use_path_style: OCI serves buckets as /<bucket>/<key>, not as a
    # virtual-host subdomain.
    #
    # skip_s3_checksum: the AWS SDK adds integrity headers that OCI's
    # S3 layer rejects. Without this, every state write fails.
    #
    # The skip_* validation flags each call an AWS-only endpoint (STS,
    # IMDS, region catalogue) that does not exist here.
    use_path_style              = true
    skip_s3_checksum            = true
    skip_credentials_validation = true
    skip_region_validation      = true
    skip_metadata_api_check     = true
    skip_requesting_account_id  = true

    # S3-native locking via a conditional PUT of a .tflock object. Verified
    # working against OCI Object Storage; see README.md. This is what stops a
    # laptop and a CI runner from writing state at the same time.
    use_lockfile = true
  }
}
