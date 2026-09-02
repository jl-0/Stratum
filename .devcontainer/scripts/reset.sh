#!/usr/bin/env bash
# Removes the run directories, products and results site so the walkthrough
# starts again at the plan. Leaves the downloaded assets, the index and the
# artifact cache alone: they are the slow parts, and a re-run over them is
# what demonstrates content addressing.
set -euo pipefail
cd "$(dirname "$0")/../.."
source .devcontainer/scripts/common.sh
pkill -f "http.server $PORT" >/dev/null 2>&1 || true
rm -rf "$RUNS_DIR" "$PRODUCTS_DIR" "$SITE_DIR"
echo "[reset] removed runs, products and the results site under $OUT_DIR"
echo "[reset] kept $INDEX_DIR, $OUT_DIR/assets and $OUT_DIR/cache"
echo "[reset] to start completely fresh:  rm -rf $INDEX_DIR $OUT_DIR"
echo "[reset] run .devcontainer/get-started.sh to begin again"
