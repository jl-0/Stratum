#!/usr/bin/env bash
# Adds the welcome banner to ~/.bashrc, once. Runs as part of onCreateCommand,
# so a prebuild bakes it in. Nothing secret is involved.
set -euo pipefail
cd "$(dirname "$0")/../.."
MARK="# stratum-demo-welcome"
RC="$HOME/.bashrc"
if grep -qF "$MARK" "$RC" 2>/dev/null; then echo "[welcome] already installed in $RC"; exit 0; fi
REPO="$(pwd -P)"
cat >> "$RC" <<RCEOF

# stratum-demo-welcome
if [ -n "\$PS1" ] && [ -x "$REPO/.devcontainer/scripts/welcome.sh" ]; then
  "$REPO/.devcontainer/scripts/welcome.sh"
fi
RCEOF
echo "[welcome] installed in $RC"
