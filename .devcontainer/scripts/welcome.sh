#!/usr/bin/env bash
# Printed by ~/.bashrc in every new shell. A few lines that point at the
# walkthrough rather than trying to be it.
cd "$(dirname "$0")/../.." 2>/dev/null || exit 0
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then B=$'\e[1m'; DIM=$'\e[2m'; OFF=$'\e[0m'; else B=""; DIM=""; OFF=""; fi
printf '\n  %sStratum demo%s\n' "$B" "$OFF"
printf '  Build a mineral mosaic of northern Nevada from the EMIT archive, one step at a time.\n\n'
printf '      %s.devcontainer/get-started.sh%s\n\n' "$B" "$OFF"
printf '  %sNothing has been started for you -- every step is yours to run.%s\n\n' "$DIM" "$OFF"
