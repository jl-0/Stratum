#!/usr/bin/env bash
# Earthdata Login. Prefers the two environment variables (Codespaces secrets);
# otherwise earthaccess asks for a username and password and writes ~/.netrc.
# Nothing here prints or stores the credential anywhere else.
set -euo pipefail
cd "$(dirname "$0")/../.."
source .devcontainer/scripts/common.sh

if [ -n "${EARTHDATA_USERNAME:-}" ] && [ -n "${EARTHDATA_PASSWORD:-}" ]; then
  echo "[login] EARTHDATA_USERNAME / EARTHDATA_PASSWORD are set; checking they work"
  pixi run python -c "import earthaccess; a=earthaccess.login(strategy='environment'); raise SystemExit(0 if a.authenticated else 1)" \
    && echo "[login] ok" || { echo "[login] Earthdata rejected those variables" >&2; exit 1; }
  exit 0
fi

if grep -qs "urs.earthdata.nasa.gov" "$HOME/.netrc"; then
  echo "[login] ~/.netrc already has an Earthdata entry; checking it works"
  pixi run python -c "import earthaccess; a=earthaccess.login(strategy='netrc'); raise SystemExit(0 if a.authenticated else 1)" \
    && echo "[login] ok" || { echo "[login] Earthdata rejected the ~/.netrc entry; edit it or delete the line and run this again" >&2; exit 1; }
  exit 0
fi

cat <<'TXT'
[login] You need a free Earthdata Login account: https://urs.earthdata.nasa.gov
[login] earthaccess will ask for the username and password and write them to
[login] ~/.netrc (mode 600) so every later step can use them.
TXT
pixi run python -c "import earthaccess; a=earthaccess.login(strategy='interactive', persist=True); raise SystemExit(0 if a.authenticated else 1)" \
  && echo "[login] ok - saved to ~/.netrc" || { echo "[login] login failed" >&2; exit 1; }
