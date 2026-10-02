#!/usr/bin/env bash
# 写侧脚本的「集群身份闸门」。可被 `source`（bash / sh 都行），成功返回 0，失败 `exit 2`。
#
# 为什么要这道闸：2026-10-02 一次 `apply.sh -h`（脚本没有 `-h` 分支）在**没设 KUBECONFIG**
# 的情况下走完了正常流程，把整套负载 apply 到了本机默认 context 指的**另一套阿里云 ACK
# 集群**上（见 docs/deploy-clusters.md §7.34.1）。根因是两条：写侧脚本假设调用者已经
# `export KUBECONFIG=tmp/k0s/kubeconfig`（没设时 kubectl 会**安静地**用默认 context），
# 以及没有写侧脚本去断言目标集群的形状。
#
# 用法：
#   . "$(dirname "$0")/lib/cluster-guard.sh"
#   require_target_cluster            # 用 $KUBECONFIG
#   require_target_cluster <path>     # 用显式路径（会 export KUBECONFIG=<path>）
#
# 判据（全部 fail-closed；任何一条不过就打印具名错误并 exit 2）：
#   1. KUBECONFIG（或传入的路径）**已显式设置**、是单一路径、且文件存在 —— 否则
#      kubectl 会退回去用 ~/.kube/config 的 current-context（本机默认 = ACK 集群）；
#   2. `kubectl version -o json` 的 `serverVersion.gitVersion` 含 `+k0s`
#      （用 `-o json`：`--short` 已废弃）；
#   3. `kubectl get nodes -o json`：节点数 == 期望值（`E2B_TARGET_NODES`，默认 2）、
#      每台 `status.nodeInfo.architecture == arm64`（`E2B_TARGET_ARCH`）、
#      每台 `status.nodeInfo.kubeletVersion` 含 `+k0s`（`E2B_TARGET_KUBELET_SUBSTR`）。
#
# 成功时打**一行**确认：`✓ target cluster: context=<name> server=<gitVersion> nodes=<N> (<names>)`。
# 这行走 **stderr**：apply.sh 的 DRY_RUN 把 stdout 当"喂给 kubectl 的数据流"，不能混进
# 任何非 YAML 行（见 deploy/k8s-k0s/apply.sh 头注释）。
#
# 失败信息点名**实际看到的值与期望值**（context、server gitVersion、节点数、每台架构与
# kubeletVersion），一眼能看出连错了哪套。

#: 目标集群的形状 —— 期望值常量只有这一处（open-cluster-tunnel.sh 不再自带一份）。
CLUSTER_GUARD_EXPECTED_NODES="${E2B_TARGET_NODES:-2}"
CLUSTER_GUARD_EXPECTED_ARCH="${E2B_TARGET_ARCH:-arm64}"
CLUSTER_GUARD_EXPECTED_KUBELET_SUBSTR="${E2B_TARGET_KUBELET_SUBSTR:-+k0s}"

require_target_cluster() {
    _cg_kubeconfig="${1:-${KUBECONFIG:-}}"

    if [ -z "$_cg_kubeconfig" ]; then
        printf '%s\n' \
            "cluster-guard: refusing to run kubectl against the default context: KUBECONFIG is not set" \
            "cluster-guard:   没设 KUBECONFIG 时 kubectl 会用 ~/.kube/config 的 current-context —— 本机默认指的" \
            "cluster-guard:   是另一套阿里云 ACK 集群（7 节点 / v1.34.3-aliyun.1 / 没有 sandlock namespace），" \
            "cluster-guard:   不是本项目的 k0s 集群（2 节点 arm64 / +k0s）。先显式指向目标集群：" \
            "cluster-guard:     export KUBECONFIG=\"\$PWD/tmp/k0s/kubeconfig\"" \
            "cluster-guard:   没有这份文件就先跑 deploy/scripts/open-cluster-tunnel.sh（它建通道并写它）" >&2
        exit 2
    fi

    case "$_cg_kubeconfig" in
        *:*)
            printf '%s\n' \
                "cluster-guard: refusing to run kubectl: KUBECONFIG 是一个路径列表（含 ':'），不是一个目标集群" \
                "cluster-guard:   闸门只接受单一目标集群的一份 kubeconfig（见 docs/deploy-clusters.md §0）：" \
                "cluster-guard:     export KUBECONFIG=\"\$PWD/tmp/k0s/kubeconfig\"" >&2
            exit 2
            ;;
    esac

    if [ ! -f "$_cg_kubeconfig" ]; then
        printf '%s\n' \
            "cluster-guard: refusing to run kubectl: KUBECONFIG is set to $_cg_kubeconfig but that file does not exist" \
            "cluster-guard:   读不到 kubeconfig 就不去猜目标集群。修正路径，或用" \
            "cluster-guard:   deploy/scripts/open-cluster-tunnel.sh 重新取一份（写 tmp/k0s/kubeconfig）；" \
            "cluster-guard:   绝不要退回去用 ~/.kube/config —— 那是另一套阿里云 ACK 集群。" >&2
        exit 2
    fi

    command -v kubectl >/dev/null 2>&1 ||
        { printf 'cluster-guard: 缺少 kubectl —— 闸门连自己的身份都认不了\n' >&2; exit 2; }
    command -v python3 >/dev/null 2>&1 ||
        { printf 'cluster-guard: 缺少 python3（解析 kubectl version/get nodes 的 -o json 用）\n' >&2; exit 2; }

    KUBECONFIG="$_cg_kubeconfig"
    export KUBECONFIG

    # 命令替换的失败不能被 `set -e` 隐式带走：先接住退出码，再把报告打到 stderr，最后 exit 2。
    _cg_rc=0
    _cg_report="$(python3 - \
        "$CLUSTER_GUARD_EXPECTED_NODES" \
        "$CLUSTER_GUARD_EXPECTED_ARCH" \
        "$CLUSTER_GUARD_EXPECTED_KUBELET_SUBSTR" <<'CLUSTER_GUARD_PY'
import json
import shutil
import subprocess
import sys

want_nodes = int(sys.argv[1])
want_arch = sys.argv[2]
want_kubelet = sys.argv[3]


def kubectl(args):
    return subprocess.run(["kubectl", *args], capture_output=True, text=True, check=False)


def parse(text):
    """kubectl 的 -o json 输出；容忍前面混进来的告警行（有的版本会打在 stdout）。"""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        pass
    start = text.find("{")
    if start < 0:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[start:])
        return obj
    except ValueError:
        return None


def expectation():
    return (
        "cluster-guard:   期望：节点数 == %d；每台 architecture=%s、kubeletVersion 含 %s；"
        "serverVersion.gitVersion 含 %s"
        % (want_nodes, want_arch, want_kubelet, want_kubelet)
    )


def refuse(lines):
    print("\n".join(lines))
    sys.exit(2)


if shutil.which("kubectl") is None:
    refuse(["cluster-guard: ✗ cluster-guard refused this run: 找不到 kubectl（修 PATH 再来）"])

ctx = (kubectl(["config", "current-context"]).stdout or "").strip() or "(unknown)"

version = kubectl(["version", "-o", "json"])
server = None
parsed_version = parse(version.stdout)
if isinstance(parsed_version, dict):
    server = ((parsed_version.get("serverVersion") or {}).get("gitVersion")) or None
if not server:
    refuse(
        [
            "cluster-guard: ✗ cluster-guard refused this run: 读不到 server 版本"
            "（kubectl version -o json 退出码 %d）" % version.returncode,
            "cluster-guard:   context=%s" % ctx,
            expectation(),
            "cluster-guard:   kubectl stderr: %s"
            % ((version.stderr.strip() or "<空>").replace("\n", " | ")),
            "cluster-guard:   通道没开就先跑 deploy/scripts/open-cluster-tunnel.sh"
            "（server=https://127.0.0.1:16443）",
        ]
    )

if want_kubelet not in server:
    refuse(
        [
            "cluster-guard: ✗ cluster-guard refused this run: 连到的不是本项目的 k0s 集群"
            "（refusing to run kubectl against this cluster）",
            "cluster-guard:   context=%s  server=%s" % (ctx, server),
            expectation(),
            "cluster-guard:   实际：serverVersion.gitVersion=%s 不含 %s —— 这形状就是本机默认 context 指的"
            % (server, want_kubelet),
            "cluster-guard:   阿里云 ACK 集群（7 节点 / v1.34.3-aliyun.1 / 没有 sandlock namespace）",
            "cluster-guard:   核对与修法见 docs/deploy-clusters.md §1/§2；对准目标集群："
            "export KUBECONFIG=\"$PWD/tmp/k0s/kubeconfig\"",
        ]
    )

nodes_call = kubectl(["get", "nodes", "-o", "json"])
parsed_nodes = parse(nodes_call.stdout)
items = parsed_nodes.get("items") if isinstance(parsed_nodes, dict) else None
if not isinstance(items, list):
    refuse(
        [
            "cluster-guard: ✗ cluster-guard refused this run: 读不到节点清单"
            "（kubectl get nodes -o json 退出码 %d）" % nodes_call.returncode,
            "cluster-guard:   context=%s  server=%s" % (ctx, server),
            expectation(),
            "cluster-guard:   kubectl stderr: %s"
            % ((nodes_call.stderr.strip() or "<空>").replace("\n", " | ")),
        ]
    )

observed = []
for item in items:
    name = ((item.get("metadata") or {}).get("name")) or "(unnamed)"
    info = (item.get("status") or {}).get("nodeInfo") or {}
    observed.append(
        (name, info.get("architecture") or "(unknown)", info.get("kubeletVersion") or "(unknown)")
    )

bad = []
if len(observed) != want_nodes:
    bad.append("节点数 %d != %d" % (len(observed), want_nodes))
for name, arch, kubelet in observed:
    if arch != want_arch:
        bad.append("%s: architecture=%s != %s" % (name, arch, want_arch))
    if want_kubelet not in kubelet:
        bad.append("%s: kubeletVersion=%s 不含 %s" % (name, kubelet, want_kubelet))

if bad:
    refuse(
        [
            "cluster-guard: ✗ cluster-guard refused this run: 连到的不是本项目的 k0s 集群"
            "（refusing to run kubectl against this cluster）",
            "cluster-guard:   context=%s  server=%s  nodes=%d" % (ctx, server, len(observed)),
            expectation(),
            "cluster-guard:   实际（每台）：",
        ]
        + [
            "cluster-guard:     %s  architecture=%s  kubeletVersion=%s" % node
            for node in observed
        ]
        + ["cluster-guard:   不符项："] + ["cluster-guard:     - %s" % item for item in bad]
        + ["cluster-guard:   核对与修法见 docs/deploy-clusters.md §1/§2"]
    )

print(
    "✓ target cluster: context=%s server=%s nodes=%d (%s)"
    % (ctx, server, len(observed), " ".join(name for name, _, _ in observed))
)
CLUSTER_GUARD_PY
)" || _cg_rc=$?

    printf '%s\n' "$_cg_report" >&2
    if [ "$_cg_rc" != 0 ]; then
        exit 2
    fi
    unset _cg_kubeconfig _cg_rc _cg_report
}
