#!/usr/bin/env bash
# `stratum`, with the two things a bare shell is missing.
#
#   1. The pixi environment, because the CLI is installed there and not on PATH.
#   2. .env, because a cloud run's boto3 needs the same AWS profile Terraform uses. Without it
#      the credential chain finds nothing and the run dies on NoCredentialsError, part-way
#      through plan, after the downloads have already been paid for.
#
# Working directory is preserved, so relative manifest paths mean what you typed.
set -euo pipefail
. "$(dirname "$0")/common.sh"
exec pixi run --manifest-path "$REPO_ROOT/pyproject.toml" stratum "$@"
