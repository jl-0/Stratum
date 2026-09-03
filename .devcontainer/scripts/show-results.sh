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
  # The environment's own interpreter, like every other step: the base image
  # carries no promise of a system python3, and a missing one used to leave the
  # walkthrough claiming it had served a page that was never there.
  py="$PWD/.pixi/envs/default/bin/python"
  [ -x "$py" ] || py=$(command -v python3 || true)
  [ -n "$py" ] || { echo "[results] no python to serve with" >&2; exit 1; }
  log="$SITE_DIR/server.log"
  # setsid (util-linux) detaches the server from this terminal's session on Linux and
  # Codespaces; macOS has no setsid, and nohup alone is enough there.
  detach=(nohup)
  command -v setsid >/dev/null 2>&1 && detach=(setsid nohup)
  "${detach[@]}" "$py" -m http.server "$PORT" --bind 0.0.0.0 -d "$SITE_DIR" >"$log" 2>&1 < /dev/null &
  for _ in 1 2 3 4 5 6 7 8 9 10; do port_listening && break; sleep 0.5; done
  if ! port_listening; then
    echo "[results] nothing is listening on port $PORT; $log says:" >&2
    tail -n 5 "$log" >&2 || true
    exit 1
  fi
  echo "[results] serving $SITE_DIR on port $PORT"
fi
if [ -n "${CODESPACE_NAME:-}" ]; then
  echo "[results] open the PORTS panel and click the globe icon on port $PORT"
else
  echo "[results] open http://localhost:$PORT"
fi
