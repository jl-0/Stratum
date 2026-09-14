#!/usr/bin/env bash
# Write a cloud variant of an example manifest: the same science, pointed at this deployment's
# storage root. The output is gitignored, because the root names the account.
#
# This exists only because `-p/--patch` is not built yet (09 section 3). With patches this would
# be `stratum run -m manifest.yaml -p outputs.bucket=$(root)` and no file at all.
set -euo pipefail
. "$(dirname "$0")/common.sh"

SRC="${1:-$REPO_ROOT/examples/emit-cmr-nevada/manifest.yaml}"
case "$SRC" in *.cloud.yaml) echo "$SRC is already a generated cloud manifest" >&2; exit 1 ;; esac
OUT="${SRC%.yaml}.cloud.yaml"          # foo.yaml -> foo.cloud.yaml

ROOT="$(terraform -chdir="$REPO_ROOT/terraform" output -raw storage_root)"
[ -n "$ROOT" ] || { echo "no storage_root output - run 'make stratum-infrastructure' first" >&2; exit 1; }

python3 - "$SRC" "$OUT" "$ROOT" <<'PY'
import re, sys
src, out, root = sys.argv[1:4]
text = open(src).read()
new, n = re.subn(r'^(\s*bucket:\s*)\S+.*$',
                 lambda m: f"{m.group(1)}{root}", text, count=1, flags=re.M)
if n != 1:
    sys.exit(f"could not find outputs.bucket in {src}")
open(out, "w").write(new)
PY
echo "wrote $OUT"
grep -n "bucket:" "$OUT" | head -1
