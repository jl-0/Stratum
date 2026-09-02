#!/usr/bin/env bash
# Follows a run started in another terminal by tailing the newest results file.
set -uo pipefail
cd "$(dirname "$0")/../.."
source .devcontainer/scripts/common.sh
run_dir=$(latest_run_dir)
[ -n "$run_dir" ] || { echo "[watch] no run directory yet"; exit 0; }
echo "[watch] $run_dir/work  (Ctrl-C to stop watching; the run keeps going)"
while run_in_progress; do
  for f in "$run_dir"/work/*.results.jsonl; do
    [ -f "$f" ] && printf '  %-28s %s items\n' "$(basename "$f" .results.jsonl)" "$(wc -l < "$f" | tr -d ' ')"
  done
  sleep 5
  printf '\n'
done
echo "[watch] the run has finished"
