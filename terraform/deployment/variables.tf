# Filled from .env by scripts/common.sh. Nothing site-specific may reach a tracked file.

variable "image_digest" {
  description = "The worker image, as `sha256:...`. Printed by `make image`; set STRATUM_IMAGE_DIGEST in .env. No default on purpose - a deployment must name the code it runs."
  type        = string

  validation {
    condition     = can(regex("^sha256:[0-9a-f]{64}$", var.image_digest))
    error_message = "image_digest must be a full digest, e.g. sha256:<64 hex>. A tag is not a digest; run `make image` and use what it prints."
  }
}

variable "state_bucket" {
  description = "Terraform state bucket, so this root can read the platform root's outputs. Derived from the account id by scripts/common.sh."
  type        = string
}

# ---------------------------------------------------------------------------------------------
# Sizing. Platform concerns, not run parameters (ADR-0002): a manifest never names them.
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
  description = "Invocations this function may run at once. It is a guard rail, not a throughput target: it stops one run exhausting the account's Lambda concurrency and starving another (08 section 6). -1 leaves the function unreserved."
  type        = number
  default     = 64
}

variable "log_retention_days" {
  description = "CloudWatch retention. The default for a log group is 'never expire', which is a real line item at tens of thousands of invocations."
  type        = number
  default     = 30
}

variable "budget_usd" {
  description = "Monthly cost ceiling for this deployment's tagged resources. An alarm, not a cap - AWS does not stop the work. 0 creates no budget."
  type        = number
  default     = 100
}

variable "budget_alert_email" {
  description = "Where the budget alert goes. Empty creates the budget with no subscriber, which is nearly useless: a runaway fan-out is otherwise discovered by the bill."
  type        = string
  default     = ""
}

# ---------------------------------------------------------------------------------------------
# Ordinary configuration, shared with the platform root.
# ---------------------------------------------------------------------------------------------

variable "aws_profile" {
  description = "Local AWS profile name."
  type        = string
  default     = "mosaic-gen"
}

variable "region" {
  description = "Pinned by spec 08 section 3: the source data and the deployment share a region."
  type        = string
  default     = "us-west-2"
}

variable "deployment" {
  description = "Deployment name. One deployment serves many runs (ADR-0002)."
  type        = string
  default     = "dev"
}
