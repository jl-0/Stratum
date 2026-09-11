#!/usr/bin/env bash
# Create the S3 bucket that holds Terraform state. Run ONCE per account. Idempotent.
#
# This exists only because Terraform cannot create the bucket that stores its own state.
# It has nothing to do with site requirements - no IAM, no roles, no permissions boundary.
set -euo pipefail
. "$(dirname "$0")/common.sh"

BUCKET="$(state_bucket)"
echo "account $(account_id) / region $AWS_REGION / bucket $BUCKET"

if aws s3api head-bucket --bucket "$BUCKET" --profile "$AWS_PROFILE" 2>/dev/null; then
  echo "bucket already exists - ensuring settings"
else
  aws s3api create-bucket --bucket "$BUCKET" --profile "$AWS_PROFILE" --region "$AWS_REGION" \
    --create-bucket-configuration "LocationConstraint=$AWS_REGION"
fi

# Versioning is the only recovery path from a corrupted or truncated state file.
aws s3api put-bucket-versioning --bucket "$BUCKET" --profile "$AWS_PROFILE" \
  --versioning-configuration Status=Enabled

aws s3api put-bucket-encryption --bucket "$BUCKET" --profile "$AWS_PROFILE" \
  --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'

# State holds every value Terraform touches, in plaintext. This bucket is a secret store.
aws s3api put-public-access-block --bucket "$BUCKET" --profile "$AWS_PROFILE" \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

echo "done. next: make tf-init"
