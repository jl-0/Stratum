# deployment/ - the UNPRIVILEGED root. It creates no IAM role, so anyone with bucket and image
# repository write may apply it. That is what makes "add a scorer" an unprivileged operation:
# a new scorer is a new image and an apply here, and it touches no role (ADR-0002, ADR-0003).
#
# It also does not BUILD anything. `make image` builds and pushes; this root points a function at
# the digest that produced. Terraform running docker would need Docker on the applier's machine
# and would make the apply depend on a working copy rather than on a recorded artifact.

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }
  backend "s3" {} # ./backend.hcl, generated for THIS root: a separate state key from platform/
}

provider "aws" {
  region  = var.region
  profile = var.aws_profile

  default_tags {
    tags = {
      Project    = "stratum"
      Deployment = var.deployment
      ManagedBy  = "terraform"
      Root       = "deployment"
    }
  }
}

# Everything this root needs from the privileged root: role ARNs, the bucket, the secret, the
# repository. Read-only, and a deployer who cannot read platform state gets a clear failure here
# rather than a confusing one later.
data "terraform_remote_state" "platform" {
  backend = "s3"
  config = {
    bucket  = var.state_bucket
    key     = "platform/terraform.tfstate"
    region  = var.region
    profile = var.aws_profile
  }
}

locals {
  platform = data.terraform_remote_state.platform.outputs
  name     = "stratum-${var.deployment}-worker"
  # The image is pinned by digest, never by tag. The repository's tags are immutable, so a tag
  # would be nearly as good - but "nearly" is not a property to rest reproducibility on, and the
  # digest is what goes into provenance.
  image_uri = "${local.platform.ecr_repository_url}@${var.image_digest}"
}
