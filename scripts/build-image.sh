#!/usr/bin/env bash
# Build the worker image and push it to the deployment's ECR repository, then print the digest.
#
#   ./scripts/build-image.sh            build, push, print the digest
#   ./scripts/build-image.sh --local    build only; nothing leaves this machine
#
# Terraform never builds (ADR-0003). This script is the build, and the digest it prints is what
# terraform/ pins - which is why the repository's tags are immutable and why a dirty
# working tree is refused: a digest has to identify a commit somebody else can check out.
set -euo pipefail
. "$(dirname "$0")/common.sh"

LOCAL_ONLY=0
[ "${1:-}" = "--local" ] && LOCAL_ONLY=1

PLATFORM="${STRATUM_IMAGE_PLATFORM:-linux/arm64}"

if ! git -C "$REPO_ROOT" diff --quiet HEAD -- src plugins vendor pyproject.toml pixi.lock \
        Dockerfile docker .dockerignore 2>/dev/null; then
  if [ "${ALLOW_DIRTY:-0}" != "1" ] && [ "$LOCAL_ONLY" = "0" ]; then
    echo "refusing to push an image built from uncommitted changes." >&2
    echo "commit first, or set ALLOW_DIRTY=1 to push anyway, or use --local." >&2
    git -C "$REPO_ROOT" status --short -- src plugins vendor pyproject.toml pixi.lock \
        Dockerfile docker .dockerignore >&2
    exit 1
  fi
fi

TAG="$(git -C "$REPO_ROOT" rev-parse --short HEAD)"
[ "${ALLOW_DIRTY:-0}" = "1" ] && TAG="${TAG}-dirty-$(date -u +%Y%m%dT%H%M%SZ)"

if [ "$LOCAL_ONLY" = "1" ]; then
  IMAGE="stratum-${STRATUM_DEPLOYMENT}:${TAG}"
  echo "building ${IMAGE} for ${PLATFORM} (local only)"
  docker build --platform "$PLATFORM" -t "$IMAGE" "$REPO_ROOT"
  echo
  echo "built ${IMAGE}.  Try:  docker run --rm ${IMAGE} stratum plugins list"
  exit 0
fi

ACCOUNT="$(account_id)"
REGISTRY="${ACCOUNT}.dkr.ecr.${AWS_REGION}.amazonaws.com"
REPO="stratum-${STRATUM_DEPLOYMENT}"
IMAGE="${REGISTRY}/${REPO}:${TAG}"

echo "logging in to ${REGISTRY}"
aws ecr get-login-password --region "$AWS_REGION" --profile "$AWS_PROFILE" \
  | docker login --username AWS --password-stdin "$REGISTRY" >/dev/null

echo "building and pushing ${IMAGE} for ${PLATFORM}"
# --provenance=false: an OCI attestation manifest makes the pushed reference a manifest LIST,
# and Lambda refuses one. The image must be a single-platform manifest.
docker buildx build --platform "$PLATFORM" --provenance=false --push \
  -t "$IMAGE" "$REPO_ROOT"

DIGEST="$(aws ecr describe-images --region "$AWS_REGION" --profile "$AWS_PROFILE" \
  --repository-name "$REPO" --image-ids "imageTag=${TAG}" \
  --query 'imageDetails[0].imageDigest' --output text)"

echo
echo "pushed  ${IMAGE}"
echo "digest  ${DIGEST}"
echo
echo "This is what the deployment pins. Record it in .env, replacing any earlier line:"
echo
echo "    STRATUM_IMAGE_DIGEST=${DIGEST}             # the worker, and the viewer with it"
echo
echo "One image, two pins. To move the viewer WITHOUT repointing a worker you have frozen,"
echo "leave STRATUM_IMAGE_DIGEST alone and set the viewer's instead:"
echo
echo "    STRATUM_VIEWER_IMAGE_DIGEST=${DIGEST}      # the viewer only"
echo
echo "then  make stratum-infrastructure"
