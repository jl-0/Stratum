# Thin wrappers. Every target reads .env (gitignored) via scripts/common.sh, so nothing in this
# repository names an account, a profile, a bucket, or a site's IAM requirements.
SHELL := /usr/bin/env bash
.PHONY: help env-check bootstrap tf-init tf-plan tf-apply tf-output test lint

help:
	@echo "bootstrap  create the Terraform state bucket (once per account)"
	@echo "tf-init    write backend.hcl from .env and terraform init the platform root"
	@echo "tf-plan    terraform plan the platform root"
	@echo "tf-apply   terraform apply the platform root  (PRIVILEGED - creates IAM)"
	@echo "tf-output  show the platform root's outputs"
	@echo "test lint  pixi run test / pixi run lint"

env-check:
	@test -f .env || { echo "no .env - copy .env.example and fill it in"; exit 1; }

bootstrap: env-check ; ./scripts/bootstrap-backend.sh
tf-init:   env-check ; ./scripts/tf.sh platform init
tf-plan:   env-check ; ./scripts/tf.sh platform plan
tf-apply:  env-check ; ./scripts/tf.sh platform apply
tf-output: env-check ; ./scripts/tf.sh platform output

test: ; pixi run test
lint: ; pixi run lint
