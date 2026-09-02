#!/usr/bin/env bash
# Guided walkthrough of the Stratum demo: build a mineral mosaic of one degree of
# northern Nevada from the EMIT archive, one step at a time.
#
# Safe to run at any time, as often as you like. It works out where you are by
# looking at what exists -- the environment, a credential, the index, the plan,
# the products, a server on the results port -- never a progress file that could
# disagree with reality. Stopping a codespace terminates every running process
# but keeps every file, and Stratum's cache means an interrupted run resumes
# where it stopped, so re-running a step is always the right answer.
set -uo pipefail
cd "$(dirname "$0")/.."
source .devcontainer/scripts/common.sh

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  B=$'\e[1m'; DIM=$'\e[2m'; OK=$'\e[32m'; WARN=$'\e[33m'; OFF=$'\e[0m'
else
  B=""; DIM=""; OK=""; WARN=""; OFF=""
fi

# Steps skipped this session. Skipping changes nothing on disk, so without this
# the same step would be offered again immediately.
SKIPPED=""
skipped() { case " $SKIPPED " in *" $1 "*) return 0 ;; *) return 1 ;; esac; }

TITLES=("Environment" "Earthdata login" "Build the index" "Plan the run" "Run the mosaic" "Open the results")
NOTES=("pixi solves GDAL, netCDF4, rasterio and Stratum itself" \
       "a free Earthdata account; ~/.netrc or two Codespaces secrets" \
       "one CMR query: 37 granules over the tile for 2026, no pixels yet" \
       "select, filter, freeze, work lists, a report -- and one download" \
       "regrid, resolve, reduce, publish -- ~5.6 GB, about 6 minutes" \
       "mineral map, agreement and support, on a forwarded port")
CMDS=(".devcontainer/scripts/setup-env.sh" \
      ".devcontainer/scripts/login.sh" \
      ".devcontainer/scripts/build-index.sh" \
      ".devcontainer/scripts/plan.sh" \
      ".devcontainer/scripts/run.sh" \
      ".devcontainer/scripts/show-results.sh")
BLURBS=("Install pixi if it is missing, check out the SpectralUtil submodule and solve the environment. A prebuilt codespace has already done this." \
        "Log in to Earthdata. earthaccess prompts for your username and password and writes ~/.netrc; nothing else ever sees them. If EARTHDATA_USERNAME and EARTHDATA_PASSWORD are set, that is used instead and this step is already done." \
        "Query CMR for every EMIT L2B mineral granule and its L1B geometry file over tile (-118, 41) in 2026, and write the index: URLs, footprints, times, cloud cover and SHA-512 checksums. No pixels are fetched." \
        "Plan the run: apply the cloud filter, check that every granule's class table agrees, freeze the granule set into the run directory, write the work lists and report.md. It downloads one geometry file to prove the readers and the credential work." \
        "Run every stage. Granules download on first touch into out/assets (verified against their checksums); regrid builds one cached lookup table per granule; resolve picks the most nadir look per cell per month; reduce votes across months; publish writes the products. Ctrl-C stops it; running again resumes from the cache." \
        "Render the mineral map, the agreement and the support count as images, and serve them with the legend on port 8080.")

step_state() {
  case $1 in
    1) env_ready && echo done || echo todo ;;
    2) have_credential && echo done || echo todo ;;
    3) [ -s "$INDEX_DIR/granules.parquet" ] && echo done || echo todo ;;
    4) ls "$RUNS_DIR"/*/plan.json >/dev/null 2>&1 && echo done || echo todo ;;
    5)
      if run_in_progress; then echo running
      elif products_ready; then echo done
      else echo todo
      fi ;;
    6) port_listening && echo done || echo todo ;;
  esac
}

ask() {
  local __var=$1 __default=${2:-q} __reply
  if read -r __reply 2>/dev/null < /dev/tty; then :
  elif ! read -r __reply; then __reply=$__default; fi
  printf -v "$__var" '%s' "$__reply"
}

mark() {
  case $1 in
    done)    printf '%s[x]%s' "$OK" "$OFF" ;;
    running) printf '%s[~]%s' "$WARN" "$OFF" ;;
    *)       printf '[ ]' ;;
  esac
}

banner() {
  local i state next=0
  printf '\n  %sStratum demo%s  %s%s%s\n\n' "$B" "$OFF" "$DIM" "tile (-118, 41), northern Nevada, EMIT 2026" "$OFF"
  for i in 1 2 3 4 5 6; do
    state=$(step_state $i)
    if [ "$next" = 0 ] && [ "$state" != done ] && ! skipped "$i"; then next=$i; fi
    printf '  %s %d. %-18s %s%s%s\n' "$(mark "$state")" "$i" "${TITLES[$((i-1))]}" "$DIM" "${NOTES[$((i-1))]}" "$OFF"
  done
  printf '\n'
  return $next
}

run_step() {
  local i=$1
  printf '%s' "$B"; printf '%s\n' "${BLURBS[$((i-1))]}" | fold -s -w 72 | sed 's/^/  /'
  printf '%s\n' "$OFF"
  printf '  This runs:  %s%s%s\n\n' "$DIM" "${CMDS[$((i-1))]}" "$OFF"
  printf '  %sEnter%s to run, %ss%s to skip, %sq%s to quit > ' "$B" "$OFF" "$B" "$OFF" "$B" "$OFF"
  ask reply
  case "$reply" in
    q|Q) printf '\n  Come back any time with .devcontainer/get-started.sh\n\n'; exit 0 ;;
    s|S) SKIPPED="$SKIPPED $i"; printf '\n  Skipped.\n'; return 2 ;;
  esac
  printf '\n'
  if ! bash "${CMDS[$((i-1))]}"; then
    printf '\n  %sThat step did not finish.%s Re-run .devcontainer/get-started.sh to try again;\n' "$WARN" "$OFF"
    printf '  every step resumes from what already exists.\n\n'
    exit 1
  fi
  return 0
}

while true; do
  banner; next=$?

  if [ "$next" = 0 ]; then
    if [ -n "$SKIPPED" ]; then
      printf '  %sNothing left to offer -- you skipped step(s):%s%s\n' "$WARN" "$OFF" "$SKIPPED"
      printf '  Run .devcontainer/get-started.sh again to be offered them afresh.\n\n'
      exit 0
    fi
    printf '  %sEverything is done.%s\n\n' "$OK" "$OFF"
    if [ -n "${CODESPACE_NAME:-}" ]; then
      printf '  Open the PORTS panel and click the globe icon on port %s.\n\n' "$PORT"
    else
      printf '  Open http://localhost:%s\n\n' "$PORT"
    fi
    printf '  %sThe run report:%s      pixi run stratum report --run %s/*\n' "$DIM" "$OFF" "$RUNS_DIR"
    printf '  %sRun it again (cached):%s .devcontainer/scripts/run.sh\n' "$DIM" "$OFF"
    printf '  %sStart over:%s           .devcontainer/scripts/reset.sh\n\n' "$DIM" "$OFF"
    exit 0
  fi

  if [ "$(step_state "$next")" = running ]; then
    printf '  Step %d is running in another terminal.\n\n' "$next"
    printf '  %sEnter%s to follow its progress, %ss%s to leave it > ' "$B" "$OFF" "$B" "$OFF"
    ask reply s
    case "$reply" in
      s|S) printf '\n  Still running. Check back with .devcontainer/get-started.sh\n\n'; exit 0 ;;
      *)   printf '\n'; bash .devcontainer/scripts/watch.sh; continue ;;
    esac
  fi

  run_step "$next" || true
  printf '\n'
done
