#!/usr/bin/env bash
# Renders the products as images and serves them with the legend on $PORT.
# The server is detached from this shell so it survives the walkthrough exiting.
set -euo pipefail
cd "$(dirname "$0")/../.."
source .devcontainer/scripts/common.sh
products_ready || { echo "[results] no products yet - run .devcontainer/scripts/run.sh" >&2; exit 1; }
prod=$(ls -d "$PRODUCTS_DIR"/*/*/*/ | tail -1)
mkdir -p "$SITE_DIR"
pixi run python .devcontainer/tools/make_site.py "$prod" "$SITE_DIR"
if port_listening; then
  echo "[results] a server is already listening on port $PORT"
else
  # setsid (util-linux) detaches the server from this terminal's session on Linux and
  # Codespaces; macOS has no setsid, and nohup alone is enough there.
  if command -v setsid >/dev/null 2>&1; then
    setsid nohup python3 -m http.server "$PORT" --bind 0.0.0.0 -d "$SITE_DIR" >"$SITE_DIR/server.log" 2>&1 < /dev/null &
  else
    nohup python3 -m http.server "$PORT" --bind 0.0.0.0 -d "$SITE_DIR" >"$SITE_DIR/server.log" 2>&1 < /dev/null &
  fi
  for _ in 1 2 3 4 5 6 7 8 9 10; do port_listening && break; sleep 0.5; done
  echo "[results] serving $SITE_DIR on port $PORT"
fi
if [ -n "${CODESPACE_NAME:-}" ]; then
  echo "[results] open the PORTS panel and click the globe icon on port $PORT"
else
  echo "[results] open http://localhost:$PORT"
fi
