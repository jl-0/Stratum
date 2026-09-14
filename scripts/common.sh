# Sourced by every script here. Loads .env and maps it to the TF_VAR_* names Terraform reads,
# so the private file uses friendly names and the repository contains no account detail.
# Not executable on purpose: `. scripts/common.sh`.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${STRATUM_ENV:-$REPO_ROOT/.env}"

if [ ! -f "$ENV_FILE" ]; then
  echo "no $ENV_FILE - copy .env.example to .env and fill it in" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

# Non-empty required.
for v in AWS_PROFILE AWS_REGION STRATUM_DEPLOYMENT; do
  if [ -z "${!v:-}" ]; then echo "$v is empty in $ENV_FILE" >&2; exit 1; fi
done
# Must be present, may legitimately be empty ("this site imposes none").
for v in STRATUM_PERMISSIONS_BOUNDARY_ARN STRATUM_ROLE_NAME_PREFIX; do
  if [ -z "${!v+set}" ]; then echo "$v is not set in $ENV_FILE (use \"\" if none)" >&2; exit 1; fi
done

export TF_VAR_aws_profile="$AWS_PROFILE"
export TF_VAR_region="$AWS_REGION"
export TF_VAR_deployment="$STRATUM_DEPLOYMENT"
export TF_VAR_permissions_boundary_arn="$STRATUM_PERMISSIONS_BOUNDARY_ARN"
export TF_VAR_role_name_prefix="$STRATUM_ROLE_NAME_PREFIX"

# Empty until `make image` has printed a digest, and empty is valid: the root then creates the
# bucket, repository, secret and roles and no function. That is the bootstrap order, because the
# repository must exist before there is an image to push to it.
export TF_VAR_image_digest="${STRATUM_IMAGE_DIGEST:-}"
export TF_VAR_budget_alert_email="${STRATUM_BUDGET_ALERT_EMAIL:-}"

account_id() { aws sts get-caller-identity --profile "$AWS_PROFILE" --query Account --output text; }
state_bucket() { echo "stratum-tfstate-$(account_id)"; }

