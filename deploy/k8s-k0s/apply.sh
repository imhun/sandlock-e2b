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
#   SKIP_WARM=1 ... deploy/k8s-k0s/apply.sh           # 不预热 base image
#
# **stdout 只放渲染结果，其余（进度、诊断、错误）一律 stderr。** 这样 DRY_RUN 的
# 输出是可以直接喂给 kubectl 的数据流：
#   DRY_RUN=1 deploy/k8s-k0s/apply.sh 2>/dev/null | kubectl apply --dry-run=server -f -
# 以前的版本把「版本 … 已 pin」打在 stdout，于是上面这条命令的第 1 行不是 YAML，
# 管道那一侧只会报一个与真正原因无关的解析错误。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
NAMESPACE="${NAMESPACE:-sandlock}"

#: The in-container warmer (`deploy/scripts/warm_base_image.py`): the agent
#: listens on the container's own 0.0.0.0:49983, so the script travels to the
#: pod over stdin instead of being dialled from here.
WARM_HELPER="$REPO_ROOT/deploy/scripts/warm_base_image.py"

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
echo "版本 ${VERSION}：${count} 个镜像引用已 pin" >&2

if [ "${DRY_RUN:-0}" = "1" ]; then
    printf '%s\n' "$rendered"
    exit 0
fi

printf '%s\n' "$rendered" | kubectl apply -f -

# --- rollout 之后预热 base image（N25 / §22.5.10 那条运维事实）---------------
# 一次滚动重启可以打断正在进行的解包，缓存目录里只剩 `…sha256_….lock`（没有实体
# 目录）；此时该节点的 `Sandbox.create()` 回 **428 warm_required** —— 而 e2b SDK
# 既不发 `X-Sandbox-Id` 也不认识 428，于是"节点冷"表现成一次与沙箱无关的冒烟失败。
# 在这里补一个预热步骤把窗口关掉：GET 只查询（不落地），**POST 才真的解包**，两者
# 都幂等。打在每个 pod 自己的 agent 上，所以 `kubectl exec` 进容器跑。
if [ "${SKIP_WARM:-0}" = "1" ]; then
    echo "跳过 base image 预热（SKIP_WARM=1）" >&2
    exit 0
fi

[ -f "$WARM_HELPER" ] || { echo "缺少 $WARM_HELPER" >&2; exit 1; }

echo "等待 worker 滚动完成" >&2
kubectl -n "$NAMESPACE" rollout status statefulset/e2b-worker --timeout=300s

image="$(kubectl -n "$NAMESPACE" get statefulset e2b-worker \
    -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="E2B_BASE_IMAGE")].value}')"
if [ -z "$image" ]; then
    echo "无法从 statefulset/e2b-worker 读到 E2B_BASE_IMAGE" >&2
    exit 1
fi
key="$(kubectl -n "$NAMESPACE" get secret e2b-secrets \
    -o jsonpath='{.data.E2B_INTERNAL_API_KEY}' | base64 -d)"
if [ -z "$key" ]; then
    echo "无法从 secret/e2b-secrets 读到 E2B_INTERNAL_API_KEY" >&2
    exit 1
fi

warm_failures=0
while read -r pod; do
    [ -n "$pod" ] || continue
    echo "预热 $pod：$image" >&2
    if ! kubectl -n "$NAMESPACE" exec -i "$pod" -- \
        python3 - --image "$image" --key "$key" < "$WARM_HELPER"; then
        warm_failures=$((warm_failures + 1))
    fi
done < <(kubectl -n "$NAMESPACE" get pods -l app=e2b-worker \
    -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')

if [ "$warm_failures" != 0 ]; then
    echo "有 $warm_failures 个 worker 预热失败：这些节点的首个 create 会回 428 warm_required" >&2
    exit 1
fi
echo "base image 已在每个 worker 就绪（peek cached=true）" >&2
