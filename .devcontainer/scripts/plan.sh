#!/usr/bin/env bash
# Plan: select, filter, check class tables, freeze, write work lists and the
# report. Downloads one geometry file to prove the readers and the credential.
set -euo pipefail
cd "$(dirname "$0")/../.."
source .devcontainer/scripts/common.sh
pixi run stratum plan -m "$MANIFEST"
run_dir=$(latest_run_dir)
echo
echo "[plan] run directory: $run_dir"
echo "[plan] the report (also: pixi run stratum report --run $run_dir):"
echo
sed -n '1,60p' "$run_dir/report.md"
echo
echo "[plan] ... read the fan-out and the budget lines before spending bandwidth on the run."
