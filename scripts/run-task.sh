#!/usr/bin/env bash
# Run a manifest in AWS without this machine: `stratum orchestrate` as a Fargate task.
#
#   ./scripts/run-task.sh <manifest.yaml> [--attempts N] [--workers N] [--dry-run]
#
# Writes the manifest's cloud variant, stages it and the files beside it in the bucket, starts
# the task (terraform/run.tf) and RETURNS. The run carries on with the laptop closed; follow it
# in the log this prints. Interrupting or re-running is safe: finished items are cache hits.
#
# Two things are decided here rather than in the image, so neither needs a rebuild:
#
#   --attempts N   the task runs the whole run up to N times (default 3), stopping at the first
#                  that succeeds. A stage is all-or-nothing, so one upstream outage - a burst of
#                  502s from the archive - fails a run hours in; the next attempt finds
#                  everything that finished in the cache and redoes only what did not. A refused
#                  budget (exit 2) is never retried.
#   --workers N    invocations in flight. When not given, the worker function's reserved
#                  concurrency, which is the most the dispatcher will use anyway.
#
# What is staged: the manifest's own directory (minus out/), and the plain files of the
# directory above it, which is where the examples keep a shared zone registry. A manifest that
# names files anywhere else needs them moved beside it.
set -euo pipefail
. "$(dirname "$0")/common.sh"

SRC="${1:?usage: $(basename "$0") <manifest.yaml> [--attempts N] [--workers N] [--dry-run]}"
shift
[ -f "$SRC" ] || { echo "no such manifest: $SRC" >&2; exit 1; }

ATTEMPTS=3
PASS=()
HAS_WORKERS=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --attempts) ATTEMPTS="${2:?--attempts needs a number}"; shift 2 ;;
    --workers)  HAS_WORKERS=1; PASS+=("$1" "${2:?--workers needs a number}"); shift 2 ;;
    *)          PASS+=("$1"); shift ;;
  esac
done
case "$ATTEMPTS" in ''|*[!0-9]*|0) echo "--attempts must be a positive integer" >&2; exit 64 ;; esac

AWS=(aws --profile "$AWS_PROFILE" --region "$AWS_REGION")
TF=(terraform -chdir="$REPO_ROOT/terraform")
tf_output() { "${TF[@]}" output -raw "$1" 2>/dev/null || true; }

CLUSTER="$(tf_output viewer_cluster)"
TASKDEF="$(tf_output run_task_definition)"
ROOT="$(tf_output storage_root)"
if [ -z "$CLUSTER" ] || [ -z "$TASKDEF" ] || [ -z "$ROOT" ]; then
  cat >&2 <<EOF
no run task is deployed.

  It runs the worker image in the viewer's network, so it needs both in .env, then a re-apply:

    STRATUM_IMAGE_DIGEST=sha256:...        # what 'make image' printed
    STRATUM_VPC_ID=vpc-...

    make stratum-infrastructure
EOF
  exit 1
fi

"$REPO_ROOT/scripts/cloud-manifest.sh" "$SRC"
CLOUD="${SRC%.yaml}.cloud.yaml"
DIR="$(cd "$(dirname "$SRC")" && pwd)"
NAME="$(basename "$DIR")"
BUNDLE="${ROOT%/}/bundles/$(basename "${SRC%.yaml}")-$(date -u +%Y%m%dT%H%M%SZ)"

echo "staging $DIR -> $BUNDLE/"
"${AWS[@]}" s3 sync "$DIR" "$BUNDLE/$NAME" --exclude "out/*" --exclude "*.html" --only-show-errors
for f in "$DIR"/../*; do
  [ -f "$f" ] && "${AWS[@]}" s3 cp "$f" "$BUNDLE/$(basename "$f")" --only-show-errors
done

NETWORK="$(python3 - "$("${TF[@]}" output -json viewer_network)" <<'PY'
import json, sys
n = json.loads(sys.argv[1])
print(json.dumps({"awsvpcConfiguration": {
    "subnets": n["subnets"], "securityGroups": n["security_groups"],
    "assignPublicIp": n["assign_public_ip"]}}))
PY
)"
if [ "$HAS_WORKERS" = 0 ]; then
  FUNCTION="$(tf_output function_name)"
  RESERVED="$("${AWS[@]}" lambda get-function-concurrency --function-name "$FUNCTION" \
              --query ReservedConcurrentExecutions --output text 2>/dev/null || true)"
  case "$RESERVED" in ''|*[!0-9]*) ;; *) PASS+=(--workers "$RESERVED") ;; esac
fi

# The retry is a shell loop around the command, not code in the image: it re-runs the whole run,
# which is cheap because every finished item is a cache hit, and it changes nothing a cache key
# is made from.
OVERRIDES="$(python3 - "$ATTEMPTS" "$BUNDLE/" "$NAME/$(basename "$CLOUD")" ${PASS[@]+"${PASS[@]}"} <<'PY'
import json, shlex, sys
attempts = int(sys.argv[1])
run = shlex.join(["stratum", "orchestrate", "--bundle", sys.argv[2], "-m", sys.argv[3],
                  *sys.argv[4:]])
loop = (f"n=1; until {run}; do rc=$?; "
        f'if [ "$rc" -eq 2 ] || [ "$n" -ge {attempts} ]; then exit "$rc"; fi; '
        f'echo "attempt $n of {attempts} failed (exit $rc); starting attempt $((n+1)) in 60 s" >&2; '
        "n=$((n+1)); sleep 60; done")
command = ["bash", "-c", loop] if attempts > 1 else shlex.split(run)
print(json.dumps({"containerOverrides": [{"name": "run", "command": command}]}))
PY
)"

ARN="$("${AWS[@]}" ecs run-task --cluster "$CLUSTER" --launch-type FARGATE \
      --task-definition "$TASKDEF" --network-configuration "$NETWORK" \
      --overrides "$OVERRIDES" --query 'tasks[0].taskArn' --output text)"
[ -n "$ARN" ] && [ "$ARN" != "None" ] || { echo "run-task returned no task" >&2; exit 1; }

P="--profile $AWS_PROFILE --region $AWS_REGION"
cat <<EOF
started. The run no longer needs this machine.

  task    $ARN
  follow  aws $P logs tail /ecs/stratum-${STRATUM_DEPLOYMENT}-run --follow
  state   aws $P ecs describe-tasks --cluster $CLUSTER --tasks $ARN \\
            --query 'tasks[0].{status:lastStatus,exit:containers[0].exitCode,why:stoppedReason}'
  stop    aws $P ecs stop-task --cluster $CLUSTER --task $ARN
EOF
