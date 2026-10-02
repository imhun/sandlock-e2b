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
# **集群身份闸门（在任何 kubectl 之前，DRY_RUN 也要过）**：调用
# `deploy/scripts/lib/cluster-guard.sh` 的 `require_target_cluster` —— KUBECONFIG 必须显式设置、
# server 必须含 `+k0s`、节点必须是 2 × arm64 × `+k0s`，不符即 exit 2 并点名实际值。这是
# 2026-10-02 那次「没带 KUBECONFIG 的 `apply.sh -h` 打到了默认 context 的 ACK 集群」的兜底
# （docs/deploy-clusters.md §7.34.1）。
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

#: 认集群：写操作（包括 DRY_RUN 的渲染）之前先断言目标集群的身份，不通过就 exit 2。
. "$REPO_ROOT/deploy/scripts/lib/cluster-guard.sh"
require_target_cluster

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

# --- rollout 闸门：先 agent，后 worker（顺序不能反）-------------------------
# C3 Task 7 退役了 C1 的 `ds/e2b-priv-broker` 与它的 rollout 闸门：worker 不再有
# socket 形态，也就不再和某个节点 daemon 共享一份"镜像契约"。
#
# agent 是 worker 的**上游**（`E2B_SLOT_IDENTITY=agent-grant`）：worker 起一个槽位时
# 要先由 CP 指令本节点的 agent 授予身份，agent 不在就没有回落路径（建箱直接失败并
# 点名）。所以它必须在 worker 之前收敛；只等 worker 的话，这道闸门可能在一个从未
# 起来的 agent 上放行。
echo "等待 C3 agent DaemonSet 滚动完成" >&2
kubectl -n "$NAMESPACE" rollout status ds/e2b-c3-agent --timeout=300s
echo "等待 worker 滚动完成" >&2
kubectl -n "$NAMESPACE" rollout status statefulset/e2b-worker --timeout=300s

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
