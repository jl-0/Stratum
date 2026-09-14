# Filled from .env by scripts/common.sh. Nothing site-specific may reach a tracked file.

# ---------------------------------------------------------------------------------------------
# Site requirements. NO DEFAULTS ON PURPOSE: a missing value must fail the plan loudly rather
# than quietly apply something a site policy would reject. Empty string means "none required",
# which is a different statement from leaving it unset.
# ---------------------------------------------------------------------------------------------

variable "permissions_boundary_arn" {
  description = "IAM permissions boundary applied to every role. Empty if the site requires none."
  type        = string
}

variable "role_name_prefix" {
  description = "Required prefix for IAM role names at this site. Empty if the site requires none."
  type        = string
}

# ---------------------------------------------------------------------------------------------
# The code being deployed.
# ---------------------------------------------------------------------------------------------

variable "image_digest" {
  description = "The worker image as `sha256:...`, printed by `make image` and set as STRATUM_IMAGE_DIGEST in .env. EMPTY IS VALID and means infrastructure only: the bucket, repository, secret and roles are created and no function is. That is the bootstrap order, because the repository has to exist before there is an image to push to it."
  type        = string
  default     = ""

  validation {
    condition     = var.image_digest == "" || can(regex("^sha256:[0-9a-f]{64}$", var.image_digest))
    error_message = "image_digest must be empty, or a full digest like sha256:<64 hex>. A tag is not a digest; run `make image` and use what it prints."
  }
}

# ---------------------------------------------------------------------------------------------
# Worker sizing. Platform concerns, not run parameters (ADR-0002): a manifest never names them.
# ---------------------------------------------------------------------------------------------

variable "memory_mb" {
  description = "Worker memory. Lambda gives proportional CPU, so this is the compute knob too. A 720x720 block with up to 14 candidates measures well under 3 GB; 4 GB leaves room for a fuller stack."
  type        = number
  default     = 4096
}

variable "timeout_seconds" {
  description = "Per-item ceiling. Lambda's hard maximum is 900. The slowest measured item is a regrid of a granule covering the whole tile, about 19 s single-threaded; the margin is for a cold start plus a granule download."
  type        = number
  default     = 900

  validation {
    condition     = var.timeout_seconds > 0 && var.timeout_seconds <= 900
    error_message = "Lambda's maximum timeout is 900 seconds (08 section 2). An item that needs longer is a smaller block, or Batch."
  }
}

variable "ephemeral_storage_mb" {
  description = "/tmp, which holds the storage mirror and the staged granules. Lambda's maximum is 10240."
  type        = number
  default     = 10240
}

variable "reserved_concurrency" {
  description = "Invocations this function may run at once. A guard rail, not a throughput target: it stops one run exhausting the account's Lambda concurrency and starving another (08 section 6). -1 leaves the function unreserved."
  type        = number
  default     = 64
}

variable "log_retention_days" {
  description = "CloudWatch retention. The default for a log group is 'never expire', a real line item at tens of thousands of invocations."
  type        = number
  default     = 30
}

variable "budget_usd" {
  description = "Monthly cost ceiling for this deployment's tagged resources. An alarm, not a cap - AWS does not stop the work. 0 creates no budget."
  type        = number
  default     = 100
}

variable "budget_alert_email" {
  description = "Where the budget alert goes. Empty creates the budget with no subscriber, which is nearly useless."
  type        = string
  default     = ""
}

# ---------------------------------------------------------------------------------------------
# Ordinary configuration.
# ---------------------------------------------------------------------------------------------

variable "aws_profile" {
  description = "Local AWS profile name. PLACEHOLDER - change to the profile your login produces."
  type        = string
  default     = "mosaic-gen"
}

variable "region" {
  description = "Pinned by spec 08 section 3: the source data and the deployment share a region."
  type        = string
  default     = "us-west-2"
}

variable "deployment" {
  description = "Deployment name. One deployment serves many runs; a new experiment is a new manifest, never a new deployment (ADR-0002)."
  type        = string
  default     = "dev"
}
