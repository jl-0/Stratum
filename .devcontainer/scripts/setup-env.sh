#!/usr/bin/env bash
# Environment: pixi, the SpectralUtil submodule, and the solved environment.
# Idempotent; each part is skipped when it is already there.
set -euo pipefail
cd "$(dirname "$0")/../.."
source .devcontainer/scripts/common.sh

if ! command -v pixi >/dev/null 2>&1; then
  echo "[env] installing pixi"
  curl -fsSL https://pixi.sh/install.sh | bash
  export PATH="$HOME/.pixi/bin:$PATH"
fi
echo "[env] pixi $(pixi --version | awk '{print $2}')"

if [ ! -f vendor/SpectralUtil/pyproject.toml ]; then
  echo "[env] checking out the SpectralUtil submodule"
  git submodule update --init vendor/SpectralUtil
fi

echo "[env] solving the environment (a few minutes the first time)"
pixi install
echo "[env] $(pixi run stratum --version)"
echo "[env] ready"
