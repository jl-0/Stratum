#!/usr/bin/env bash
# terraform, with .env loaded and the backend config pointed at the right root.
#   scripts/tf.sh platform init | plan | apply | destroy ...
set -euo pipefail
. "$(dirname "$0")/common.sh"

ROOT="${1:?usage: tf.sh <platform|deployment> <terraform args...>}"; shift
DIR="$REPO_ROOT/terraform/$ROOT"
[ -d "$DIR" ] || { echo "no such root: $DIR" >&2; exit 1; }

if [ "$ROOT" = "deployment" ]; then
  export_state_bucket   # deployment/ reads platform/'s outputs from it
  # `init` sets up the backend and `output` reads state; neither reads the digest. Requiring it
  # for those couples backend setup to having built an image, which are unrelated steps.
  case "${1:-}" in init|output) needs_digest=false ;; *) needs_digest=true ;; esac
  if [ "$needs_digest" = true ] && [ -z "${TF_VAR_image_digest:-}" ]; then
    echo "STRATUM_IMAGE_DIGEST is not set in $ENV_FILE." >&2
    echo "Run 'make image', then put the digest it prints in .env." >&2
    exit 1
  fi
fi

if [ "${1:-}" = "init" ]; then
  "$REPO_ROOT/scripts/write-backend.sh" "$ROOT"
  exec terraform -chdir="$DIR" init -backend-config="$DIR/backend.hcl" "${@:2}"
fi
exec terraform -chdir="$DIR" "$@"
