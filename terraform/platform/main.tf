# platform/ - the PRIVILEGED root. Creates IAM roles, the data bucket, the ECR repository and
# the EDL secret container. Applied rarely, by someone permitted to create IAM roles.
# Everything applied often lives in deployment/, which creates no IAM.

terraform {
  required_version = ">= 1.10" # use_lockfile needs 1.10+
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0" # the committed .terraform.lock.hcl is what actually pins this
    }
  }
  backend "s3" {} # values come from -backend-config=../backend.hcl (gitignored)
}

provider "aws" {
  region  = var.region
  profile = var.aws_profile

  default_tags {
    tags = {
      Project    = "stratum"
      Deployment = var.deployment
      ManagedBy  = "terraform"
      Root       = "platform"
    }
  }
}

data "aws_caller_identity" "current" {}

locals {
  account_id  = data.aws_caller_identity.current.account_id
  data_bucket = "stratum-${var.deployment}-${data.aws_caller_identity.current.account_id}"

  # Every role name carries the site prefix. Sites that impose no prefix set it to "".
  role_name = {
    lambda    = "${var.role_name_prefix}stratum-${var.deployment}-lambda"
    task      = "${var.role_name_prefix}stratum-${var.deployment}-task"
    task_exec = "${var.role_name_prefix}stratum-${var.deployment}-task-exec"
  }
}
