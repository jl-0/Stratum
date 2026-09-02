#!/usr/bin/env bash
# Every stage, in this terminal. Ctrl-C stops it; running again resumes from the
# cache -- finished GLTs, snapshots and product blocks are hits.
set -euo pipefail
cd "$(dirname "$0")/../.."
source .devcontainer/scripts/common.sh
have_credential || { echo "[run] no Earthdata credential yet - run .devcontainer/scripts/login.sh" >&2; exit 1; }
start=$(date +%s)
pixi run stratum run -m "$MANIFEST"
echo
echo "[run] finished in $(( $(date +%s) - start )) s"
echo "[run] downloaded assets: $(du -sh "$OUT_DIR/assets" 2>/dev/null | cut -f1)   cache: $(du -sh "$OUT_DIR/cache" 2>/dev/null | cut -f1)"
echo "[run] products: $(ls -d "$PRODUCTS_DIR"/*/*/*/ 2>/dev/null | tail -1)"
