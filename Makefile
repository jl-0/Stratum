# Thin wrappers. Every target reads .env (gitignored) via scripts/common.sh, so nothing in this
# repository names an account, a profile, a bucket, or a site's IAM requirements.
SHELL := /usr/bin/env bash
.PHONY: help env-check bootstrap tf-init tf-plan tf-apply tf-output \
        image image-local tf-deploy-init tf-deploy-plan tf-deploy tf-deploy-output test lint

help:
	@echo "bootstrap  create the Terraform state bucket (once per account)"
	@echo "tf-init    write backend.hcl from .env and terraform init the platform root"
	@echo "tf-plan    terraform plan the platform root"
	@echo "tf-apply   terraform apply the platform root  (PRIVILEGED - creates IAM)"
	@echo "tf-output  show the platform root's outputs"
	@echo
	@echo "image        build the worker image and push it; prints the digest to pin"
	@echo "image-local  build it without pushing (docker run IMAGE stratum plugins list)"
	@echo "tf-deploy    apply the deployment root: the Lambda worker, from that digest"
	@echo "tf-deploy-output  show the deployment root's outputs (the function name)"
	@echo
	@echo "test lint  pixi run test / pixi run lint"

env-check:
	@test -f .env || { echo "no .env - copy .env.example and fill it in"; exit 1; }

bootstrap: env-check ; ./scripts/bootstrap-backend.sh
tf-init:   env-check ; ./scripts/tf.sh platform init
tf-plan:   env-check ; ./scripts/tf.sh platform plan
tf-apply:  env-check ; ./scripts/tf.sh platform apply
tf-output: env-check ; ./scripts/tf.sh platform output

image:       env-check ; ./scripts/build-image.sh
image-local: env-check ; ./scripts/build-image.sh --local

tf-deploy-init:   env-check ; ./scripts/tf.sh deployment init
tf-deploy-plan:   env-check ; ./scripts/tf.sh deployment plan
tf-deploy:        env-check ; ./scripts/tf.sh deployment apply
tf-deploy-output: env-check ; ./scripts/tf.sh deployment output

test: ; pixi run test
lint: ; pixi run lint
