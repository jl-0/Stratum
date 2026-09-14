# One root. It owns the whole deployment: the bucket, the image repository, the Earthdata secret
# container, the IAM roles, and the Lambda worker.
#
# There is deliberately no privileged/unprivileged split in Terraform. The split that matters is
# enforced by AWS: everyone applies the same root, and a role that may not write IAM simply cannot.
# In steady state that costs nothing, because changing only the image digest produces a plan that
# touches only the function and makes no IAM call at all. If an apply fails on an IAM permission,
# something privileged genuinely needs to change - escalate, rather than route around it.
#
# What this root does NOT do is build. `make image` builds and pushes; this points a function at
# the digest that produced. Terraform running docker would make an apply depend on a working copy
# rather than on a recorded artifact (ADR-0002, ADR-0003).

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }
  backend "s3" {} # ./backend.hcl, generated from .env by scripts/write-backend.sh
}

provider "aws" {
  region  = var.region
  profile = var.aws_profile

  default_tags {
    tags = {
      Project    = "stratum"
      Deployment = var.deployment
      ManagedBy  = "terraform"
    }
  }
}

data "aws_caller_identity" "current" {}

locals {
  account_id  = data.aws_caller_identity.current.account_id
  data_bucket = "stratum-${var.deployment}-${data.aws_caller_identity.current.account_id}"
  name        = "stratum-${var.deployment}-worker"

  # Every role name carries the site prefix. Sites that impose no prefix set it to "".
  role_name = {
    lambda    = "${var.role_name_prefix}stratum-${var.deployment}-lambda"
    task      = "${var.role_name_prefix}stratum-${var.deployment}-task"
    task_exec = "${var.role_name_prefix}stratum-${var.deployment}-task-exec"
  }

  # Pinned by digest, never by tag. Tags here are immutable so a tag would be nearly as good, and
  # "nearly" is not a property to rest reproducibility on. This is what provenance records.
  deploy_worker = var.image_digest != ""
  image_uri     = local.deploy_worker ? "${aws_ecr_repository.stratum.repository_url}@${var.image_digest}" : ""
}
