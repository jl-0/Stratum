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

variable "asset_cache_budget_mb" {
  description = "Optional policy cap (in MB) on the node-local asset cache alone. 0 = off, which is the right answer here: /tmp is shared by the asset cache, the storage mirror and the runtime, so capping one directory does not bound the volume - it only decides which directory runs out first. A 5500 MB cap here left the mirror 4.5 GB of a 10 GB /tmp and resolve filled it. Use scratch_reserve_mb instead; this stays for a shared workstation, where hoarding is the problem rather than capacity."
  type        = number
  default     = 0
}

variable "scratch_reserve_mb" {
  description = "Bytes (in MB) to keep free on /tmp. Everything staged there is a cache of something durable - a verbatim DAAC granule, or a mirror of the bucket - so when free space drops below this the least-recently-used of it is evicted, across the asset cache and the mirror together. Access time, not write time: a tile's aux warp is read by 25 blocks x 52 epochs and must outlive a snapshot that is written once and never re-read. Run directories are never evicted. 0 disables eviction entirely."
  type        = number
  default     = 1536
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

# --- the deployment's network ------------------------------------------------------------------
# Deployment level, not per workload: everything of ours that needs a network needs this one.
# Today that is the viewer task; the data plane (08 section 2) will be the next.
#
# NOT the Lambda worker, which is deliberately not VPC-attached - see the note in lambda.tf.

variable "vpc_id" {
  description = "VPC for the deployment's networked workloads. The CIDR and, where they are not named explicitly, the subnets are derived from it. Empty means no workload that needs a network is created."
  type        = string
  default     = ""
}

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

# --- the preview viewer task (guide/viewing.html) ----------------------------------------------
# No network inputs of its own: the VPC above is the whole of it. The task runs in the VPC's
# subnets and behind a security group this root creates, because every choice those knobs offered
# was one a debug tool should not be asking.
#
# There is deliberately no `assign_public_ip`. With ingress open on the port, a public IP would put
# an unauthenticated server on the internet - so a subnet that cannot reach ECR is a subnet to fix,
# not one to route around.

variable "viewer_image_digest" {
  description = "Digest for the viewer task. Empty tracks image_digest - the worker's. Set it to pin the viewer separately, so rebuilding the viewer cannot repoint a worker you have frozen mid-campaign."
  type        = string
  default     = ""

  validation {
    condition     = var.viewer_image_digest == "" || can(regex("^sha256:[0-9a-f]{64}$", var.viewer_image_digest))
    error_message = "viewer_image_digest must be empty, or a full digest like sha256:<64 hex>. A tag is not a digest; run `make image` and use what it prints."
  }
}

variable "viewer_port" {
  description = "Port the viewer listens on."
  type        = number
  default     = 8787
}

variable "viewer_cpu" {
  description = "Fargate CPU units. 1024 = 1 vCPU; tile rendering is single-request and light."
  type        = number
  default     = 1024
}

variable "viewer_memory_mb" {
  description = "Fargate memory. Must be a valid pairing with viewer_cpu."
  type        = number
  default     = 2048
}

variable "viewer_storage_gib" {
  description = "Ephemeral storage for the raster mirror. 21 is the Fargate minimum above the 20 GiB default."
  type        = number
  default     = 21
}
