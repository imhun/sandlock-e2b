#!/usr/bin/env bash
# Build the multi-arch images on this machine and push them to ACR.
# Naming: image name distinguishes the service, tag distinguishes the version:
#   <registry>/<ns>/e2b-sandlock-{control-plane-gateway,worker,autoscaler,quota-agent}:<VERSION>
#   base image mirror: <registry>/<ns>/<BASE_IMAGE> (Docker Hub unreachable on the target)
#
# worker/autoscaler/quota-agent are built by build-images.sh (A6 added the
# quota-agent there); control-plane-gateway and the base images are built below.
#
# Usage: ./deploy/scripts/build-and-push.sh
# Env:   VERSION (default: <git describe>-<timestamp> so every dev build gets
#        a fresh tag; set VERSION=1.0.0 for a release-style fixed tag),
#        PLATFORMS (default: linux/amd64,linux/arm64),
#        BASE_IMAGE (default: python:3.14-slim), MIRROR_BASE_IMAGE (default: 1)
#
# Requires: docker buildx with the multiarch builder (created automatically),
#           ACR_USERNAME / ACR_PASSWORD (or deploy/scripts/acr.env).

set -euo pipefail
. "$(cd "$(dirname "$0")" && pwd)/lib/helpers.sh"

if [ -z "${VERSION:-}" ]; then
    BASE="$(git -C "$SCRIPT_DIR/../.." describe --tags --always 2>/dev/null || echo dev)"
    VERSION="${BASE}-$(date +%Y%m%d-%H%M%S)"
fi
PLATFORMS="${PLATFORMS:-linux/amd64,linux/arm64}"
BASE_IMAGE="${BASE_IMAGE:-python:3.14-slim}"
MIRROR_BASE_IMAGE="${MIRROR_BASE_IMAGE:-1}"
REGISTRY_URL="$ACR_REGISTRY/$ACR_NAMESPACE"

require_acr_creds

say "检查 sandlock wheels（构建 worker 镜像需要）"
WHEELS="$(cd "$SCRIPT_DIR/../.." && pwd)/wheels/fork"
if ! ls "$WHEELS"/*.whl >/dev/null 2>&1; then
    echo "wheels/fork/ 缺少 wheel，先执行 ./deploy/scripts/build-sandlock-wheels.sh" >&2
    exit 1
fi

say "登录 ACR $REGISTRY_URL"
printf '%s' "$ACR_PASSWORD" | docker login "$ACR_REGISTRY" -u "$ACR_USERNAME" --password-stdin >/dev/null

say "准备 multiarch buildx builder（docker.io -> daocloud 镜像加速）"
if ! docker buildx inspect multiarch >/dev/null 2>&1; then
    docker buildx create --name multiarch --driver docker-container \
        --platform linux/amd64,linux/arm64 \
        --config "$LIB_DIR/buildkitd.toml" --use
else
    docker buildx use multiarch
fi

say "build & push e2b-sandlock images ($VERSION, $PLATFORMS)"
# BUILDX_NO_DEFAULT_ATTESTATIONS: ACR rejects the OCI empty manifest that
# buildx adds for provenance attestation.
BUILDX_NO_DEFAULT_ATTESTATIONS=1 \
REGISTRY="$REGISTRY_URL" \
VERSION="$VERSION" \
PLATFORMS="$PLATFORMS" \
PUSH=1 \
"$SCRIPT_DIR/build-images.sh"

say "build & push merged image control-plane-gateway (single port :3000: API + gateway)"
BUILDX_NO_DEFAULT_ATTESTATIONS=1 docker buildx build \
    --builder multiarch \
    --platform "$PLATFORMS" \
    --push \
    -f "$SCRIPT_DIR/../docker/Dockerfile.control-plane-gateway" \
    -t "$REGISTRY_URL/e2b-sandlock-control-plane-gateway:$VERSION" \
    "$SCRIPT_DIR/../.."

if [ "$MIRROR_BASE_IMAGE" = "1" ]; then
    MIRROR_TAG="$REGISTRY_URL/$BASE_IMAGE"
    case "$BASE_IMAGE" in
        "$ACR_REGISTRY"/*) say "BASE_IMAGE 已在 ACR，跳过镜像" ;;
        *)
            say "镜像基础镜像到 ACR：$MIRROR_TAG"
            BUILDX_NO_DEFAULT_ATTESTATIONS=1 docker buildx build \
                --builder multiarch \
                --platform "$PLATFORMS" \
                --build-arg "BASE_IMAGE=$BASE_IMAGE" \
                -t "$MIRROR_TAG" --push -f - . <<'DOCKERFILE'
ARG BASE_IMAGE=python:3.14-slim
FROM ${BASE_IMAGE}
DOCKERFILE
            ;;
    esac
fi

say "build & push MCP-capable base image (mcp + uvicorn + mcp-gateway)"
BUILDX_NO_DEFAULT_ATTESTATIONS=1 docker buildx build \
    --builder multiarch \
    --platform "$PLATFORMS" \
    -f "$SCRIPT_DIR/../docker/Dockerfile.mcp-base" \
    -t "$REGISTRY_URL/python-mcp:3.14" \
    --push \
    "$SCRIPT_DIR/../.."

# E6.2: print the pushed base-image digest so the operator can pin
# E2B_BASE_IMAGE to it (upgrade.sh refuses tag-only refs in production).
# Non-fatal: the image is already pushed; the operator can query it later.
if digest="$(docker buildx imagetools inspect "$REGISTRY_URL/python-mcp:3.14" \
    --format '{{.Manifest.Digest}}' 2>/dev/null)"; then
    echo "python-mcp:3.14 digest: $digest"
else
    echo "python-mcp:3.14 digest: 解析失败（镜像已推送；用 docker buildx imagetools inspect 手动查询）" >&2
fi

printf '%s\n' "$VERSION" > "$VERSION_FILE"
say "记录本次构建版本：$VERSION -> $VERSION_FILE"
say "完成。升级目标机：./deploy/scripts/upgrade.sh"
