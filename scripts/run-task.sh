#!/usr/bin/env bash
# Run a manifest in AWS without this machine: `stratum orchestrate` as a Fargate task.
#
#   ./scripts/run-task.sh <manifest.yaml> [--workers N] [--dry-run]
#
# Writes the manifest's cloud variant, stages it and the files beside it in the bucket, starts
# the task (terraform/run.tf) and RETURNS. The run carries on with the laptop closed; follow it
# in the log this prints. Interrupting or re-running is safe: finished items are cache hits.
#
# What is staged: the manifest's own directory (minus out/), and the plain files of the
# directory above it, which is where the examples keep a shared zone registry. A manifest that
# names files anywhere else needs them moved beside it.
set -euo pipefail
. "$(dirname "$0")/common.sh"

SRC="${1:?usage: $(basename "$0") <manifest.yaml> [--workers N] [--dry-run]}"
shift
[ -f "$SRC" ] || { echo "no such manifest: $SRC" >&2; exit 1; }

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
OVERRIDES="$(python3 - "$BUNDLE/" "$NAME/$(basename "$CLOUD")" "$@" <<'PY'
import json, sys
command = ["stratum", "orchestrate", "--bundle", sys.argv[1], "-m", sys.argv[2], *sys.argv[3:]]
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
