#!/usr/bin/env bash
# terraform against the single root, with .env loaded.
#   scripts/tf.sh init | plan | apply | output | destroy ...
set -euo pipefail
. "$(dirname "$0")/common.sh"

DIR="$REPO_ROOT/terraform"

if [ "${1:-}" = "init" ]; then
  "$REPO_ROOT/scripts/write-backend.sh"
  exec terraform -chdir="$DIR" init -backend-config="$DIR/backend.hcl" "${@:2}"
fi
exec terraform -chdir="$DIR" "$@"
