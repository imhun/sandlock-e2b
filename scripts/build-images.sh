#!/bin/sh
# Build the final control-plane and worker images (multi-arch by default).
#
# Usage:
#   TAG=myrepo/e2b:1.0 PLATFORMS=linux/amd64,linux/arm64 ./scripts/build-images.sh
#
# Defaults push nothing; set PUSH=1 to push after building.
set -eu

TAG="${TAG:-e2b-sandlock:latest}"
PLATFORMS="${PLATFORMS:-linux/amd64,linux/arm64}"
PUSH="${PUSH:-0}"

CONTROL_TAG="${CONTROL_TAG:-$TAG-control-plane}"
WORKER_TAG="${WORKER_TAG:-$TAG-worker}"

case "$PLATFORMS" in
*","*)
    # Multi-platform output must go to a registry.
    if [ "$PUSH" != "1" ]; then
        echo "multi-platform builds require PUSH=1 (output goes to a registry)" >&2
        exit 1
    fi
    OUT_FLAG="--push"
    ;;
*)
    OUT_FLAG="--load"
    ;;
esac

echo "==> building $CONTROL_TAG ($PLATFORMS)"
docker buildx build "$OUT_FLAG" \
    --platform "$PLATFORMS" \
    -f Dockerfile.control-plane \
    -t "$CONTROL_TAG" \
    .

echo "==> building $WORKER_TAG ($PLATFORMS)"
docker buildx build "$OUT_FLAG" \
    --platform "$PLATFORMS" \
    -f Dockerfile.envd \
    -t "$WORKER_TAG" \
    .

AUTOSCALER_TAG="${AUTOSCALER_TAG:-$TAG-autoscaler}"
echo "==> building $AUTOSCALER_TAG ($PLATFORMS)"
docker buildx build "$OUT_FLAG" \
    --platform "$PLATFORMS" \
    -f Dockerfile.autoscaler \
    -t "$AUTOSCALER_TAG" \
    .

echo "done:"
echo "  control-plane: $CONTROL_TAG"
echo "  worker:        $WORKER_TAG"
echo "  autoscaler:    $AUTOSCALER_TAG"
