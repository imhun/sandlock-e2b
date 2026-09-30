#!/bin/sh
# Build the worker / agent / quota-agent images (the control plane + gateway run
# as the merged e2b-sandlock-control-plane-gateway image, see
# deploy/docker/Dockerfile.control-plane-gateway / deploy/scripts/build-and-push.sh).
#
# There is no autoscaler image any more (2026-09-30): the worker fleet's
# autoscaler is a task of the control plane on the k8s path, and the local
# Docker pool it used to drive is retired. Its loop ships inside the control
# plane image (`deploy/docker/Dockerfile.control-plane-gateway` copies
# `autoscaler/`).
#
# Naming convention: the image NAME distinguishes the service and the TAG
# distinguishes the version:
#   $REGISTRY/e2b-sandlock-{worker,agent,quota-agent}:$VERSION
#
# Usage:
#   REGISTRY=myrepo/e2b VERSION=1.0 PLATFORMS=linux/amd64 ./deploy/scripts/build-images.sh
#   REGISTRY=registry.cn-shanghai.aliyuncs.com/byteplan VERSION=1.0 PUSH=1 ./deploy/scripts/build-images.sh
#
# Multi-platform output must go to a registry (buildx --push); single
# platform defaults to --load. Defaults push nothing.
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
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
    -f "$SCRIPT_DIR/../docker/Dockerfile.envd" \
    -t "$REGISTRY/e2b-sandlock-worker:$VERSION" \
    "$SCRIPT_DIR/../.."

# C3 (Task 3): the per-node agent. Its own image, deliberately *not* the
# worker's: the worker image must be assertable as "carries no privileged
# binary" (Task 4 removes the two C1 binaries from it), and that only holds if
# the agent's copy lives somewhere the worker image never builds from. It ships
# exactly `as_uid` + `e2b-maint` (with their file capabilities) and the
# control-plane channel service -- nothing of the sandbox runtime.
echo "==> building $REGISTRY/e2b-sandlock-agent:$VERSION ($PLATFORMS)"
docker buildx build "$OUT_FLAG" \
    --platform "$PLATFORMS" \
    -f "$SCRIPT_DIR/../docker/Dockerfile.agent" \
    -t "$REGISTRY/e2b-sandlock-agent:$VERSION" \
    "$SCRIPT_DIR/../.."

# A6: the quota-agent is the deployment's quota source (it is where
# CAP_SYS_ADMIN lives, see docs/production-deployment-requirements.md §2.4.3),
# so it ships through the same release flow as the worker. No sandlock wheels
# needed -- it only runs xfs_quota/lsattr server-side.
echo "==> building $REGISTRY/e2b-sandlock-quota-agent:$VERSION ($PLATFORMS)"
docker buildx build "$OUT_FLAG" \
    --platform "$PLATFORMS" \
    -f "$SCRIPT_DIR/../docker/Dockerfile.quota-agent" \
    -t "$REGISTRY/e2b-sandlock-quota-agent:$VERSION" \
    "$SCRIPT_DIR/../.."

echo "done:"
echo "  worker:        $REGISTRY/e2b-sandlock-worker:$VERSION"
echo "  agent:         $REGISTRY/e2b-sandlock-agent:$VERSION"
echo "  quota-agent:   $REGISTRY/e2b-sandlock-quota-agent:$VERSION"
echo "  (control-plane-gateway is built by build-and-push.sh)"
