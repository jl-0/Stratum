#!/usr/bin/env bash
# Shared settings for the demo scripts. Sourced from the repository root.
#
# Everything the demo produces lives under examples/emit-cmr-nevada/ -- the manifest
# puts its storage root at ./out and its index at ./index, both git-ignored
# there -- so the walkthrough's idea of "done" is simply whether those files
# exist. Nothing is kept outside the repository and nothing is tracked.

MANIFEST="${STRATUM_DEMO_MANIFEST:-examples/emit-cmr-nevada/manifest.yaml}"
EXAMPLE_DIR="$(dirname "$MANIFEST")"
INDEX_DIR="$EXAMPLE_DIR/index"
OUT_DIR="$EXAMPLE_DIR/out"
RUNS_DIR="$OUT_DIR/runs"
PRODUCTS_DIR="$OUT_DIR/products"
SITE_DIR="$OUT_DIR/site"
PORT="${STRATUM_DEMO_PORT:-8080}"

# pixi installs to ~/.pixi/bin. devcontainer.json puts it on PATH for the
# editor's shells; this covers a plain SSH shell or a fresh login.
case ":$PATH:" in *":$HOME/.pixi/bin:"*) ;; *) PATH="$HOME/.pixi/bin:$PATH" ;; esac
export PATH

env_ready() {
  command -v pixi >/dev/null 2>&1 \
    && [ -f vendor/SpectralUtil/pyproject.toml ] \
    && [ -x .pixi/envs/default/bin/stratum ]
}

# A credential is either the two environment variables earthaccess reads, or a
# ~/.netrc entry for URS. Neither value is ever printed.
have_credential() {
  if [ -n "${EARTHDATA_USERNAME:-}" ] && [ -n "${EARTHDATA_PASSWORD:-}" ]; then return 0; fi
  grep -qs "urs.earthdata.nasa.gov" "$HOME/.netrc"
}

products_ready() {
  ls "$PRODUCTS_DIR"/*/*/*/item.json >/dev/null 2>&1
}

# The run is a foreground process in whichever terminal started it; this only
# needs to notice one exists.
run_in_progress() {
  pgrep -f "stratum (run|exec) .*$(basename "$EXAMPLE_DIR")" >/dev/null 2>&1
}

port_listening() {
  if command -v ss >/dev/null 2>&1; then ss -ltn 2>/dev/null | grep -q ":$PORT "
  else (echo > "/dev/tcp/127.0.0.1/$PORT") >/dev/null 2>&1; fi
}

# The newest run directory, if any.
latest_run_dir() {
  ls -dt "$RUNS_DIR"/*/ 2>/dev/null | head -1 | sed 's:/$::'
}
