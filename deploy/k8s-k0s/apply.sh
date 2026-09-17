#!/usr/bin/env bash
# 把 deploy/k8s-k0s 渲染成「固定当次版本」的清单并 apply。
#
# 为什么要渲染后再 apply：基线清单里的镜像 tag 是占位符 `:0.1.0`（docs/k8s-deployment.md §2）。
# compose 侧由 upgrade.sh 读 deploy/stack/.version 来 pin tag，k8s 侧没有等价机制，
# 所以这里显式替换 —— 只替换 `byteplan/e2b-sandlock-*`，redis 的 tag 不动。
#
# 用法：
#   KUBECONFIG=... deploy/k8s-k0s/apply.sh            # 版本取自 deploy/stack/.version
#   VERSION=1.2.3 KUBECONFIG=... deploy/k8s-k0s/apply.sh
#   DRY_RUN=1 ... deploy/k8s-k0s/apply.sh             # 只渲染不 apply
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"

VERSION="${VERSION:-$(cat "$REPO_ROOT/deploy/stack/.version" 2>/dev/null || true)}"
if [ -z "$VERSION" ]; then
    echo "缺少版本：先跑 deploy/scripts/build-and-push.sh，或显式传 VERSION=" >&2
    exit 1
fi

command -v kubectl >/dev/null || { echo "缺少 kubectl" >&2; exit 1; }

# 只改 e2b-sandlock-* 的 tag；redis/基础镜像的 tag 是有意义的，不能跟着换。
rendered="$(kubectl kustomize "$HERE" \
    | sed -E "s#(image: registry\.cn-shanghai\.aliyuncs\.com/byteplan/e2b-sandlock-[a-z-]+):[^[:space:]]+#\1:${VERSION}#g")"

count="$(printf '%s\n' "$rendered" | grep -c "byteplan/e2b-sandlock-.*:${VERSION}$" || true)"
echo "版本 ${VERSION}：${count} 个镜像引用已 pin"

if [ "${DRY_RUN:-0}" = "1" ]; then
    printf '%s\n' "$rendered"
    exit 0
fi

printf '%s\n' "$rendered" | kubectl apply -f -
