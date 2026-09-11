#!/usr/bin/env bash
# terraform, with .env loaded and the backend config pointed at the right root.
#   scripts/tf.sh platform init | plan | apply | destroy ...
set -euo pipefail
. "$(dirname "$0")/common.sh"

ROOT="${1:?usage: tf.sh <platform|deployment> <terraform args...>}"; shift
DIR="$REPO_ROOT/terraform/$ROOT"
[ -d "$DIR" ] || { echo "no such root: $DIR" >&2; exit 1; }

if [ "${1:-}" = "init" ]; then
  "$REPO_ROOT/scripts/write-backend.sh" "$ROOT"
  exec terraform -chdir="$DIR" init -backend-config="$REPO_ROOT/terraform/backend.hcl" "${@:2}"
fi
exec terraform -chdir="$DIR" "$@"
