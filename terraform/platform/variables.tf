# ---------------------------------------------------------------------------------------------
# Site-specific values. NO DEFAULTS ON PURPOSE: a missing value must fail the plan loudly rather
# than silently apply something a site policy would reject. Fill them in terraform.tfvars, which
# is gitignored. Nothing site-specific may ever reach a tracked file.
# ---------------------------------------------------------------------------------------------

variable "permissions_boundary_arn" {
  description = "IAM permissions boundary applied to every role this root creates. Empty string if the site requires none."
  type        = string
}

variable "role_name_prefix" {
  description = "Required prefix for IAM role names at this site. Empty string if the site requires none."
  type        = string
}

# ---------------------------------------------------------------------------------------------
# Ordinary configuration. Safe to default, safe to commit.
# ---------------------------------------------------------------------------------------------

variable "aws_profile" {
  description = "Local AWS profile name. PLACEHOLDER - change to the profile your login produces."
  type        = string
  default     = "mosaic-gen"
}

variable "region" {
  description = "Pinned by spec 08 section 3: Step Functions ItemReader and the source data are both us-west-2."
  type        = string
  default     = "us-west-2"
}

variable "deployment" {
  description = "Deployment name. One deployment serves many runs; a new experiment is a new manifest, never a new deployment (ADR-0002)."
  type        = string
  default     = "dev"
}
