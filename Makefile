# Thin wrappers. Every target reads .env (gitignored) via scripts/common.sh, so nothing in this
# repository names an account, a profile, a bucket, or a site's IAM requirements.
SHELL := /usr/bin/env bash
.PHONY: help env-check bootstrap stratum-infrastructure infra infra-plan infra-output \
        cloud-run image image-local destroy test lint viewer viewer-up viewer-url \
        viewer-down viewer-logs

help:
	@echo "bootstrap               create the Terraform state bucket (once per account)"
	@echo "stratum-infrastructure  init + apply everything: bucket, ECR, secret, IAM roles, and"
	@echo "                        the Lambda worker if STRATUM_IMAGE_DIGEST is set in .env"
	@echo "infra-plan              plan the same, without applying"
	@echo "infra-output            show the outputs (storage root, function name, run command)"
	@echo "cloud-run               run examples/emit-cmr-nevada in AWS (MANIFEST=... to pick another)"
	@echo "image                   build the worker image and push it; prints the digest to pin"
	@echo "image-local             build it without pushing, for a local docker run"
	@echo "viewer                  serve a map of a products tree (ROOT=... to pick one)"
	@echo "viewer-up / -url        run the viewer as a Fargate task; print its private URL"
	@echo "viewer-down / -logs     stop it; tail its log"
	@echo "test lint               pixi run test / pixi run lint"
	@echo ""
	@echo "First time:  make bootstrap && make stratum-infrastructure"
	@echo "             ./scripts/put-edl-secret.sh"
	@echo "             make image      # then put the digest in .env"
	@echo "             make stratum-infrastructure   # again: now it creates the worker"

env-check:
	@test -f .env || { echo "no .env - copy .env.example and fill it in"; exit 1; }

bootstrap: env-check ; ./scripts/bootstrap-backend.sh

# One root, one apply. There is no privileged/unprivileged split here on purpose: AWS decides
# what a given role may change. Changing only the image digest plans only the function.
stratum-infrastructure: env-check
	./scripts/tf.sh init
	./scripts/tf.sh apply

infra: stratum-infrastructure
infra-plan:   env-check ; ./scripts/tf.sh plan
infra-output: env-check ; ./scripts/tf.sh output
destroy:      env-check ; ./scripts/tf.sh destroy

# A cloud run, end to end. NOT a smoke test: the default manifest is the worked example,
# which is a real mosaic - granule downloads, a few hundred Lambda invocations, products
# in S3. `ARGS=--dry-run` plans without executing.
#
# A cloud run, end to end: point a copy of the manifest at the deployment's bucket, look the
# function up in Terraform state, and run. `pixi run` because the CLI is in the pixi environment
# and not on PATH - make does not activate it for you.
MANIFEST ?= examples/emit-cmr-nevada/manifest.yaml
ARGS     ?=
cloud-run: env-check
	./scripts/cloud-manifest.sh $(MANIFEST)
	STRATUM_LAMBDA_FUNCTION=$$(./scripts/tf.sh output -raw function_name) \
	  ./scripts/stratum.sh run -m $(MANIFEST:.yaml=.cloud.yaml) --executor aws $(ARGS)

image:       env-check ; ./scripts/build-image.sh
image-local: env-check ; ./scripts/build-image.sh --local

# The viewer, two ways. `viewer` needs no deployment and no .env - it reads a local directory, or
# an s3:// root with your own credentials. The `viewer-*` targets run the same thing as a Fargate
# task on its own private IP, for showing someone else, and bill only while one is up.
ROOT ?= examples/emit-cmr-nevada/out
viewer: ; pixi run stratum preview --root $(ROOT)

viewer-up:   env-check ; ./scripts/viewer-task.sh up
viewer-url:  env-check ; ./scripts/viewer-task.sh url
viewer-down: env-check ; ./scripts/viewer-task.sh down
viewer-logs: env-check ; ./scripts/viewer-task.sh logs

test: ; pixi run test
lint: ; pixi run lint
