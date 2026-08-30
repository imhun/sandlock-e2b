#!/bin/sh
# Build the worker / autoscaler images (the control plane + gateway run as
# the merged e2b-sandlock-control-plane-gateway image, see
# Dockerfile.control-plane-gateway / deploy/scripts/build-and-push.sh).
#
# Naming convention: the image NAME distinguishes the service and the TAG
# distinguishes the version:
#   $REGISTRY/e2b-sandlock-{worker,autoscaler}:$VERSION
#
# Usage:
#   REGISTRY=myrepo/e2b VERSION=1.0 PLATFORMS=linux/amd64 ./scripts/build-images.sh
#   REGISTRY=registry.cn-shanghai.aliyuncs.com/byteplan VERSION=1.0 PUSH=1 ./scripts/build-images.sh
#
# Multi-platform output must go to a registry (buildx --push); single
# platform defaults to --load. Defaults push nothing.
set -eu

REGISTRY="${REGISTRY:-registry.cn-shanghai.aliyuncs.com/byteplan}"
VERSION="${VERSION:-$(git describe --tags --always 2>/dev/null || echo 0.1.0)}"
PLATFORMS="${PLATFORMS:-linux/amd64,linux/arm64}"
PUSH="${PUSH:-0}"

case "$PLATFORMS" in
*","*)
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

echo "==> building $REGISTRY/e2b-sandlock-worker:$VERSION ($PLATFORMS)"
docker buildx build "$OUT_FLAG" \
    --platform "$PLATFORMS" \
    -f Dockerfile.envd \
    -t "$REGISTRY/e2b-sandlock-worker:$VERSION" \
    .

echo "==> building $REGISTRY/e2b-sandlock-autoscaler:$VERSION ($PLATFORMS)"
docker buildx build "$OUT_FLAG" \
    --platform "$PLATFORMS" \
    -f Dockerfile.autoscaler \
    -t "$REGISTRY/e2b-sandlock-autoscaler:$VERSION" \
    .

echo "done:"
echo "  worker:        $REGISTRY/e2b-sandlock-worker:$VERSION"
echo "  autoscaler:    $REGISTRY/e2b-sandlock-autoscaler:$VERSION"
