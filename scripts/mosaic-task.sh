#!/usr/bin/env bash
# Build a published run's layer mosaics in AWS: `stratum mosaic` as a one-shot Fargate task.
#
#   ./scripts/mosaic-task.sh <run id> [stratum mosaic args...]
#
# e.g.  ./scripts/mosaic-task.sh us-west-2025-grouping-r2-... --layer mineral_1 --force
#
# Starts the task in the viewer's cluster and network (terraform/mosaic.tf), waits for it to stop,
# and exits with the container's exit code. The log is in /ecs/stratum-<deployment>-mosaic.
set -euo pipefail
. "$(dirname "$0")/common.sh"

RUN_ID="${1:?usage: $(basename "$0") <run id> [stratum mosaic args...]}"
shift

AWS=(aws --profile "$AWS_PROFILE" --region "$AWS_REGION")
TF=(terraform -chdir="$REPO_ROOT/terraform")
tf_output() { "${TF[@]}" output -raw "$1" 2>/dev/null || true; }

CLUSTER="$(tf_output viewer_cluster)"
TASKDEF="$(tf_output mosaic_task_definition)"
if [ -z "$CLUSTER" ] || [ -z "$TASKDEF" ]; then
  cat >&2 <<EOF
no mosaic task is deployed.

  It runs the worker image in the viewer's network, so it needs both in .env, then a re-apply:

    STRATUM_IMAGE_DIGEST=sha256:...        # what 'make image' printed
    STRATUM_VPC_ID=vpc-...

    make stratum-infrastructure
EOF
  exit 1
fi

NETWORK="$(python3 - "$("${TF[@]}" output -json viewer_network)" <<'PY'
import json, sys
n = json.loads(sys.argv[1])
print(json.dumps({"awsvpcConfiguration": {
    "subnets": n["subnets"], "securityGroups": n["security_groups"],
    "assignPublicIp": n["assign_public_ip"]}}))
PY
)"
# The task definition's command is `stratum mosaic --root s3://<bucket>/ --workers N`; the run
# and anything else are appended here, so the definition never names a run.
OVERRIDES="$(python3 - "$("${AWS[@]}" ecs describe-task-definition --task-definition "$TASKDEF" \
  --query 'taskDefinition.containerDefinitions[0].command' --output json)" "$RUN_ID" "$@" <<'PY'
import json, sys
command = json.loads(sys.argv[1]) + ["--run", sys.argv[2], *sys.argv[3:]]
print(json.dumps({"containerOverrides": [{"name": "mosaic", "command": command}]}))
PY
)"

echo "mosaicking $RUN_ID in $CLUSTER ..."
ARN="$("${AWS[@]}" ecs run-task --cluster "$CLUSTER" --launch-type FARGATE \
      --task-definition "$TASKDEF" --network-configuration "$NETWORK" \
      --overrides "$OVERRIDES" --query 'tasks[0].taskArn' --output text)"
[ -n "$ARN" ] && [ "$ARN" != "None" ] || { echo "run-task returned no task" >&2; exit 1; }
echo "  $ARN"
echo "  log: aws logs tail /ecs/stratum-${STRATUM_DEPLOYMENT}-mosaic --follow"

# `ecs wait tasks-stopped` gives up after 10 minutes; a western run takes longer than that.
while :; do
  STATUS="$("${AWS[@]}" ecs describe-tasks --cluster "$CLUSTER" --tasks "$ARN" \
            --query 'tasks[0].lastStatus' --output text)"
  [ "$STATUS" = "STOPPED" ] && break
  printf '  %s  %s\n' "$(date +%H:%M:%S)" "$STATUS"
  sleep 30
done

CODE="$("${AWS[@]}" ecs describe-tasks --cluster "$CLUSTER" --tasks "$ARN" \
        --query 'tasks[0].containers[0].exitCode' --output text)"
if [ "$CODE" != "0" ]; then
  echo >&2
  echo "the task stopped with exit code $CODE. ECS gives the reason as:" >&2
  "${AWS[@]}" ecs describe-tasks --cluster "$CLUSTER" --tasks "$ARN" \
    --query 'tasks[0].{task:stoppedReason,container:containers[0].reason}' --output table >&2
  exit 1
fi
echo "done. The last lines of its log:"
"${AWS[@]}" logs tail "/ecs/stratum-${STRATUM_DEPLOYMENT}-mosaic" --since 1h | tail -5
