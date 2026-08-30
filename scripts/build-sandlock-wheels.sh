#!/bin/sh
# Cross-compile the sandlock fork wheels (manylinux_2_34 for amd64 + arm64)
# with zig inside a pypa manylinux_2_34 builder and land them in wheels/fork/.
# Runs in one native builder — no QEMU, no native arm64 node — so both wheels
# build in a single pass, then auditwheel verifies/repairs the manylinux tag.
#
# Requires the sandlock fork submodule at third_party/sandlock
# (`git submodule update --init`) and the builder files under
# third_party/sandlock-wheel-builder/ (both versioned in this repo).
#
# Usage:
#   ./scripts/build-sandlock-wheels.sh
#
# Environment:
#   PLATFORM    buildx platform for the native builder (default: host arch)
#   BUILDER     buildx builder name (default: multiarch)
#   BASE_IMAGE  manylinux builder image (default: host-arch manylinux_2_28)
set -eu

BUILDER="${BUILDER:-multiarch}"

case "$(uname -m)" in
    x86_64)
        PLATFORM="${PLATFORM:-linux/amd64}"
        BASE_IMAGE="${BASE_IMAGE:-quay.io/pypa/manylinux_2_34_x86_64}" ;;
    arm64 | aarch64)
        PLATFORM="${PLATFORM:-linux/arm64}"
        BASE_IMAGE="${BASE_IMAGE:-quay.io/pypa/manylinux_2_34_aarch64}" ;;
    *)
        PLATFORM="${PLATFORM:-linux/amd64}"
        BASE_IMAGE="${BASE_IMAGE:-quay.io/pypa/manylinux_2_28_x86_64}" ;;
esac

echo "==> cross-building sandlock wheels ($PLATFORM builder, targets amd64+arm64, manylinux_2_34)"
docker buildx build --builder "$BUILDER" --platform "$PLATFORM" \
    --build-arg BASE_IMAGE="$BASE_IMAGE" \
    -f third_party/sandlock-wheel-builder/Dockerfile \
    -o type=local,dest=wheels/fork \
    .

echo "==> wheels in wheels/fork/:"
ls -lh wheels/fork/*.whl
