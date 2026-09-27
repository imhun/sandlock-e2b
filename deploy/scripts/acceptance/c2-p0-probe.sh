#!/usr/bin/env bash
# C2 P0 探针的 runner：把 `probe_c2_ownership_p0.py` 送进集群、在那份 RWX claim 上跑一次、
# 收日志、删 Job。测量的是 `docs/c2-ownership-frontload.md` §7 那两条 NAS 事实。
#
#   deploy/scripts/acceptance/c2-p0-probe.sh --print-plan      # 不连集群，打印身份/fixture/矩阵
#   deploy/scripts/acceptance/c2-p0-probe.sh --render-job      # 不连集群，渲染 Job YAML
#   deploy/scripts/acceptance/c2-p0-probe.sh --root DIR        # 本机彩排（要 root；不连集群）
#   deploy/scripts/acceptance/c2-p0-probe.sh --apply           # 建 configmap + Job，收日志后清理
#   deploy/scripts/acceptance/c2-p0-probe.sh --apply --drop-dac-override
#                                                              # P0-a 第二条臂：uid 0 不带
#                                                              # DAC_OVERRIDE（摘掉客户端那层）
#   deploy/scripts/acceptance/c2-p0-probe.sh --apply --keep-job  # 跑完留着 Job（排查用）
#   PROBE_JOB_TIMEOUT=900 … --apply                            # 等 Job 的上限（秒）
#
# 硬性质：
#   * 先认集群：KUBECONFIG 必须是 `tmp/k0s/kubeconfig` 那一份，节点形状不对就拒绝（见
#     docs/deploy-clusters.md §0/§2 —— 本机默认 context 指的是另一套 ACK 集群）。
#   * 探针是只读 + 自清理的：唯一的写是它自己的 `<export>/_probes/c2-p0-*/`；runner 只多两样
#     可回收的东西（configmap 与 Job），默认都在跑完后删掉。
#   * `--apply` 是唯一会碰集群的开关；`--print-plan`/`--render-job`/`--root` 都不需要 KUBECONFIG。
#   * 探针自己带 `--require-fstype nfs`：scratch 不在 NFS 上就 exit 4，绝不把"本地文件系统说
#     什么都行"记成 NAS 的答案。
set -euo pipefail

SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

NAMESPACE="${PROBE_NAMESPACE:-sandlock}"
CONFIGMAP="c2-p0-probe"
JOB="c2-p0-probe"
JOB_MANIFEST="$REPO_ROOT/deploy/k8s-k0s/c2-p0-probe.yaml"
PROBE="$SCRIPT_DIR/probe_c2_ownership_p0.py"
VERSION_FILE="$REPO_ROOT/deploy/stack/.version"
EXPORT_IN_POD="${PROBE_EXPORT_IN_POD:-/var/lib/e2b-sandboxes}"
JOB_TIMEOUT="${PROBE_JOB_TIMEOUT:-900}"
#: 与部署同一口径的池起始 uid（`E2B_UID_POOL_START`）；探针默认也是 10000。
PROBE_POOL_UID="${PROBE_POOL_UID:-10000}"
#: 真实跑：必须证明 scratch 在 NFS 上（本地文件系统对什么都答"行"）。
PROBE_EXTRA=(--require-fstype nfs --json
             --pool-uid "$PROBE_POOL_UID" --pool-gid "$PROBE_POOL_UID")
#: 本机彩排：不要求 fstype，其余同形（结论只对那台机器成立 —— 探针自己会打印 fstype）。
PROBE_OFFLINE_EXTRA=(--json --pool-uid "$PROBE_POOL_UID" --pool-gid "$PROBE_POOL_UID")
PROBE_ARGS=(--root "$EXPORT_IN_POD" "${PROBE_EXTRA[@]}")

APPLY=0
KEEP_JOB=0
DROP_DAC=0
PRINT_PLAN=0
RENDER_JOB=0
OFFLINE_ROOT=""

refuse() {
    printf 'REFUSE(%s): %s\n' "$1" "$2" >&2
    exit "$1"
}

usage() {
    sed -n '2,22p' "$SCRIPT_PATH"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --apply) APPLY=1 ;;
        --keep-job) KEEP_JOB=1 ;;
        --drop-dac-override) DROP_DAC=1 ;;
        --print-plan) PRINT_PLAN=1 ;;
        --render-job) RENDER_JOB=1 ;;
        --root) shift; OFFLINE_ROOT="${1:-}" ;;
        --root=*) OFFLINE_ROOT="${1#--root=}" ;;
        -h|--help) usage; exit 0 ;;
        *) refuse 2 "未知参数：$1（--help 看用法）" ;;
    esac
    shift
done

require_python() {
    command -v python3 >/dev/null || refuse 2 "缺少 python3（探针与集群自检都用它）"
}

# --- 只读的集群自检（与 migrate-state-owner.sh 同一姿态） ---------------------

IDENTITY_CHECK='
import json, sys
items = json.load(sys.stdin)["items"]
nodes = [
    (
        node["metadata"]["name"],
        node["status"]["nodeInfo"]["architecture"],
        node["status"]["nodeInfo"]["kubeletVersion"],
    )
    for node in items
]
bad = []
if len(nodes) != 2:
    bad.append("节点数 %d != 2" % len(nodes))
for name, arch, version in nodes:
    if arch != "arm64":
        bad.append("%s 架构 %s != arm64" % (name, arch))
    if "+k0s" not in version:
        bad.append("%s 版本 %s 不含 +k0s" % (name, version))
for name, arch, version in nodes:
    print("   节点 %s %s %s" % (name, arch, version), file=sys.stderr)
if bad:
    sys.exit("；".join(bad))
'

check_kubeconfig() {
    local want got
    want="$REPO_ROOT/tmp/k0s/kubeconfig"
    if [ -z "${KUBECONFIG:-}" ]; then
        refuse 2 "KUBECONFIG 没设：export KUBECONFIG=\"\$PWD/tmp/k0s/kubeconfig\"（本机默认 context 指的是另一套 ACK 集群，见 docs/deploy-clusters.md §1）"
    fi
    got="$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$KUBECONFIG")"
    if [ "$got" != "$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$want")" ]; then
        refuse 2 "KUBECONFIG=$KUBECONFIG 不是本项目那一份（要 ${want}）——绝不要把命令打到别的集群上，见 docs/deploy-clusters.md §0"
    fi
}

check_cluster_identity() {
    local nodes
    nodes="$(kubectl get nodes -o json)" ||
        refuse 2 "kubectl get nodes 失败 —— 通道在吗？deploy/scripts/open-cluster-tunnel.sh"
    if ! printf '%s' "$nodes" | python3 -c "$IDENTITY_CHECK"; then
        refuse 2 "集群身份自检没过（上面那几行）——本机默认 context 指的是另一套 ACK 集群，见 docs/deploy-clusters.md §2"
    fi
}

# --- 渲染 / 运行 -------------------------------------------------------------

version() {
    [ -f "$VERSION_FILE" ] || refuse 2 "缺 $VERSION_FILE（build-and-push.sh 会写它）"
    tr -d '[:space:]' < "$VERSION_FILE"
}

render_job() {
    local ver rendered arg caps
    ver="$(version)"
    caps="{}"
    if [ "$DROP_DAC" = "1" ]; then
        caps="{capabilities: {drop: [DAC_OVERRIDE]}}"
    fi
    rendered=""
    for arg in "$@"; do
        rendered="${rendered}\"${arg}\", "
    done
    rendered="${rendered%, }"
    sed -e "s#__IMAGE_VERSION__#${ver}#" -e "s#__PROBE_ARGS__#${rendered}#" \
        -e "s#__CAPS__#${caps}#" "$JOB_MANIFEST"
}

job_run() {
    if [ "$DROP_DAC" = "1" ]; then
        printf 'arm: root *without* DAC_OVERRIDE（P0-a 第二条臂）\n' >&2
    else
        printf 'arm: 容器默认 cap 集（root 带 DAC_OVERRIDE）\n' >&2
    fi
    kubectl -n "$NAMESPACE" create configmap "$CONFIGMAP" \
        --from-file="probe_c2_ownership_p0.py=$PROBE" \
        --dry-run=client -o yaml | kubectl -n "$NAMESPACE" apply -f -
    render_job "${PROBE_ARGS[@]}" | kubectl -n "$NAMESPACE" apply -f -

    local status
    if ! kubectl -n "$NAMESPACE" wait --for=condition=complete "job/$JOB" \
            --timeout="${JOB_TIMEOUT}s" >/dev/null 2>&1; then
        status="$(kubectl -n "$NAMESPACE" get job/"$JOB" \
            -o jsonpath='{.status.conditions[*].type}:{.status.conditions[*].status}' 2>/dev/null || true)"
        printf 'job/%s 没在 %ss 内完成（conditions=%s）——下面是它的日志\n' \
            "$JOB" "$JOB_TIMEOUT" "${status:-unknown}" >&2
    fi
    kubectl -n "$NAMESPACE" logs -l app="$JOB" --tail=-1
    local code
    code="$(kubectl -n "$NAMESPACE" get pod -l app="$JOB" \
        -o jsonpath='{.items[0].status.containerStatuses[0].state.terminated.exitCode}' 2>/dev/null || true)"
    if [ "$KEEP_JOB" = "0" ]; then
        kubectl -n "$NAMESPACE" delete job/"$JOB" --ignore-not-found >/dev/null
        kubectl -n "$NAMESPACE" delete configmap/"$CONFIGMAP" --ignore-not-found >/dev/null
    else
        printf 'kept: job/%s and configmap/%s（--keep-job）\n' "$JOB" "$CONFIGMAP" >&2
    fi
    case "${code:-}" in
        ""|"0") return 0 ;;
        *) printf 'probe exit code %s（见上面的 P0-VERDICT/P0-EXIT 行）\n' "$code" >&2; return 1 ;;
    esac
}

if [ "$PRINT_PLAN" = "1" ]; then
    exec python3 "$PROBE" --root "${OFFLINE_ROOT:-$EXPORT_IN_POD}" --print-plan
fi

if [ -n "$OFFLINE_ROOT" ]; then
    exec python3 "$PROBE" --root "$OFFLINE_ROOT" "${PROBE_OFFLINE_EXTRA[@]}"
fi

if [ "$RENDER_JOB" = "1" ]; then
    render_job "${PROBE_ARGS[@]}"
    exit 0
fi

if [ "$APPLY" = "0" ]; then
    refuse 2 "默认什么都不做：加 --apply 才碰集群（--help 看全部用法）"
fi

require_python
check_kubeconfig
check_cluster_identity
job_run
