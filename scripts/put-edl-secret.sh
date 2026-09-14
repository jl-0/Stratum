#!/usr/bin/env bash
# Populate the Earthdata Login secret. Interactive, and never echoes or logs the value.
#
# The secret holds key-value JSON, which the Lambda maps onto EARTHDATA_USERNAME /
# EARTHDATA_PASSWORD (or EARTHDATA_TOKEN) - the `environment` strategy in access/auth.py.
# Terraform creates the container only; the value never passes through it, because anything
# Terraform touches lands in state.
set -euo pipefail
. "$(dirname "$0")/common.sh"

SECRET_ID="stratum/${STRATUM_DEPLOYMENT}/earthdata"
MODE="${1:-password}"    # password | token

umask 077
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT INT TERM

case "$MODE" in
  password)
    read -r  -p "Earthdata username: " EDL_USER
    read -rs -p "Earthdata password: " EDL_PASS; echo
    [ -n "$EDL_USER" ] && [ -n "$EDL_PASS" ] || { echo "both are required" >&2; exit 1; }
    EDL_USER="$EDL_USER" EDL_PASS="$EDL_PASS" python3 -c '
import json, os, sys
json.dump({"username": os.environ["EDL_USER"], "password": os.environ["EDL_PASS"]},
          open(sys.argv[1], "w"))' "$TMP"
    ;;
  token)
    read -rs -p "Earthdata token: " EDL_TOKEN; echo
    [ -n "$EDL_TOKEN" ] || { echo "a token is required" >&2; exit 1; }
    EDL_TOKEN="$EDL_TOKEN" python3 -c '
import json, os, sys
json.dump({"token": os.environ["EDL_TOKEN"]}, open(sys.argv[1], "w"))' "$TMP"
    ;;
  *) echo "usage: $0 [password|token]" >&2; exit 1 ;;
esac

# file:// keeps the value out of shell history and out of the process list.
aws secretsmanager put-secret-value \
  --secret-id "$SECRET_ID" \
  --secret-string "file://$TMP" \
  --profile "$AWS_PROFILE" \
  --query 'VersionId' --output text >/dev/null

echo "set $SECRET_ID ($MODE). The value was never echoed, logged, or written to history."
