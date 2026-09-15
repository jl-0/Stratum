#!/usr/bin/env bash
# Start, stop and locate the preview viewer task.
#
#   ./scripts/viewer-task.sh up      run one task and print its URL
#   ./scripts/viewer-task.sh url     the URL of whatever is already running
#   ./scripts/viewer-task.sh down    stop every running viewer task
#   ./scripts/viewer-task.sh logs    tail the container log
#
# There is no service and no load balancer, so the address is the task's own private IP and it
# changes each time you start one. For a debug tool that is the right trade: nothing runs, and
# nothing is billed, between looks.
set -euo pipefail
. "$(dirname "$0")/common.sh"

AWS=(aws --profile "$AWS_PROFILE" --region "$AWS_REGION")
TF=(terraform -chdir="$REPO_ROOT/terraform")

tf_output() { "${TF[@]}" output -raw "$1" 2>/dev/null || true; }

CLUSTER="$(tf_output viewer_cluster)"
TASKDEF="$(tf_output viewer_task_definition)"
if [ -z "$CLUSTER" ] || [ -z "$TASKDEF" ]; then
  cat >&2 <<EOF
no viewer is deployed.

  It needs an image digest AND a VPC. Set both in .env and re-apply:

    STRATUM_IMAGE_DIGEST=sha256:...        # what 'make image' printed
    STRATUM_VPC_ID=vpc-...                 # the deployment's VPC; nothing else is needed

    make stratum-infrastructure
EOF
  exit 1
fi

IMAGE_URI="$(tf_output viewer_image_uri)"
NET_JSON="$("${TF[@]}" output -json viewer_network)"
PORT="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["port"])' <<<"$NET_JSON")"
NETWORK="$(python3 - "$NET_JSON" <<'PY'
import json, sys
n = json.loads(sys.argv[1])
print(json.dumps({"awsvpcConfiguration": {
    "subnets": n["subnets"], "securityGroups": n["security_groups"],
    "assignPublicIp": n["assign_public_ip"]}}))
PY
)"

running_tasks() {
  "${AWS[@]}" ecs list-tasks --cluster "$CLUSTER" --desired-status RUNNING \
    --query 'taskArns[]' --output text
}

# The private IP is on the task's elastic network interface, which is two lookups away: the task
# carries the ENI id as an attachment detail, and the ENI carries the address.
task_ip() {
  local eni
  eni="$("${AWS[@]}" ecs describe-tasks --cluster "$CLUSTER" --tasks "$1" \
        --query "tasks[0].attachments[0].details[?name=='networkInterfaceId'].value | [0]" \
        --output text)"
  [ "$eni" != "None" ] && [ -n "$eni" ] || return 1
  "${AWS[@]}" ec2 describe-network-interfaces --network-interface-ids "$eni" \
    --query 'NetworkInterfaces[0].PrivateIpAddress' --output text
}

case "${1:-up}" in
up)
  echo "starting a viewer task in $CLUSTER ..."
  ARN="$("${AWS[@]}" ecs run-task --cluster "$CLUSTER" --launch-type FARGATE \
        --task-definition "$TASKDEF" --network-configuration "$NETWORK" \
        --query 'tasks[0].taskArn' --output text)"
  [ -n "$ARN" ] && [ "$ARN" != "None" ] || { echo "run-task returned no task" >&2; exit 1; }
  echo "  $ARN"
  echo "waiting for it to run (a cold image pull is a minute or two) ..."
  # The task runs in whatever subnets the VPC has, and one that cannot reach ECR fails the image
  # pull. ECS records why; without this you would be left guessing at a task that vanished.
  if ! "${AWS[@]}" ecs wait tasks-running --cluster "$CLUSTER" --tasks "$ARN"; then
    echo >&2
    echo "the task did not reach RUNNING. ECS gives the reason as:" >&2
    "${AWS[@]}" ecs describe-tasks --cluster "$CLUSTER" --tasks "$ARN" \
      --query 'tasks[0].{task:stoppedReason,container:containers[0].reason,status:lastStatus}' \
      --output table >&2
    echo >&2
    echo "A CannotPullContainerError means the subnet it landed in has no route to ECR:" >&2
    echo "  a NAT gateway, or VPC endpoints for ecr.api, ecr.dkr, s3 and logs." >&2
    exit 1
  fi
  IP="$(task_ip "$ARN")"
  cat <<EOF

  http://$IP:$PORT/

running ${IMAGE_URI##*@}
  The task definition pins a digest, so this is the code as of the last 'make image' plus
  'make stratum-infrastructure' - not your working tree. Iterate with 'make viewer' locally.

Reachable from inside the VPC only - there is no public address and no authentication.
Stop it with 'make viewer-down'; it bills while it runs.
EOF
  ;;

url)
  found=0
  for arn in $(running_tasks); do
    ip="$(task_ip "$arn")" && { echo "http://$ip:$PORT/   $arn"; found=1; }
  done
  [ "$found" = 1 ] && echo "running ${IMAGE_URI##*@}" || echo "no viewer task is running"
  ;;

down)
  found=0
  for arn in $(running_tasks); do
    "${AWS[@]}" ecs stop-task --cluster "$CLUSTER" --task "$arn" \
      --reason "stopped by make viewer-down" --query 'task.taskArn' --output text
    found=1
  done
  [ "$found" = 1 ] || echo "no viewer task was running"
  ;;

logs)
  "${AWS[@]}" logs tail "/ecs/stratum-${STRATUM_DEPLOYMENT}-viewer" --follow
  ;;

*)
  echo "usage: $(basename "$0") {up|url|down|logs}" >&2
  exit 64
  ;;
esac
