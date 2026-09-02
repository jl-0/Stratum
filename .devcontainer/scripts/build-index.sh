#!/usr/bin/env bash
# One CMR query, one parquet file. No pixels move.
set -euo pipefail
cd "$(dirname "$0")/../.."
source .devcontainer/scripts/common.sh
have_credential || { echo "[index] no Earthdata credential yet - run .devcontainer/scripts/login.sh" >&2; exit 1; }
echo "[index] querying CMR for $MANIFEST"
pixi run stratum index build -m "$MANIFEST"
echo
echo "[index] the first few rows:"
pixi run stratum index query --index "$INDEX_DIR" --limit 5
echo
echo "[index] written to $INDEX_DIR/granules.parquet"
