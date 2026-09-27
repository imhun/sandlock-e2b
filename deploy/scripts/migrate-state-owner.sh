#!/usr/bin/env bash
# C1 wave 2 Task 6 —— 平台态属主的一次性迁移：递归 `chown 65534:65534`：不改权限位、不删东西、不动内容。
#
# 背景：wave 1/2 之后 worker 以 uid 65534 跑，而**今天**这台 NAS 上的平台态是 root worker
# 写下的（0600/0700）。非 root worker 读不了 root 的文件 ⇒ 这一步是硬前置。
# 沙箱树 `<export>/workspaces/<id>` 属于池 uid（不是 worker 的），**绝不在迁移范围内**：
# 要迁的目录是一份显式的路径计划（引擎里的 `PLATFORM_TARGETS` + 调用方用 `--target` 追加的
# 条目），每一条都过 `normalize_target()`：树根下**只放行 `workspaces/_migrate` 这一条确切
# 条目**（控制面的迁移暂存，`workspace-root-init` 建的就是它），其余任何落在 `workspaces/`
# 之下 —— 包括用 `..` 或符号链接绕过去的拼写 —— 一律拒绝并点名。
#
# 迁移目标（相对 `<export>` = `/var/lib/e2b-sandboxes`，**只改属主**）：
#   state/**     记录、命令日志、.checkpoints、.route-b、.uid_reservations、.uid_pool.lock
#   workspaces/_migrate   控制面迁移暂存（N27 之后它在树根之下；worker 只写它下面自己那份）
#   _images      OCI layout / rootfs 缓存
#   _secrets     沙箱 secret 文件
#   _snapshots   快照
#   _templates   模板
#   _builds      构建产物
#   （`_volumes` 不在其中：它的数据要被 bind 进沙箱，属主是池 uid 的账，另做。）
#
# 用法：
#   deploy/scripts/migrate-state-owner.sh                       # dry-run（默认，只看不写）
#   deploy/scripts/migrate-state-owner.sh --print-plan          # 只打印计划（不连集群，退出 0）
#   deploy/scripts/migrate-state-owner.sh --render-job          # 只渲染 Job YAML（不连集群）
#   deploy/scripts/migrate-state-owner.sh --apply               # 建 configmap + Job，收日志后清理
#   deploy/scripts/migrate-state-owner.sh --root DIR [--apply]  # 本机彩排（不连集群）
#   deploy/scripts/migrate-state-owner.sh --apply --keep-job    # 跑完留着 Job（排查用）
#   MIGRATE_JOB_TIMEOUT=1800 … --apply                          # 等 Job 的上限（秒）
#
# 硬性质（tests/unit/test_state_owner_migrate.py 逐条钉住）：
#   * DRY_RUN=1 是默认；只有显式 --apply（以及 Job 里的 --in-cluster --apply）才写
#   * 唯一的写操作是 `chown -R 65534:65534`：不改权限位、不删任何东西、不拷内容
#   * worker 不在 0 副本、或还有 worker pod 在跑，就拒绝（观测，不是猜）
#   * 路径计划只放行那一条 <export>/workspaces/_migrate；其余任何落在 <export>/workspaces/
#     之下的路径都拒绝（含 `..` / 符号链接的拼写）
#   * 先跑一遍只读的计划（经控制面 pod），再让 Job 去写；跑完每个目录 stat 留证
#   * 任何一步失败都非零退出并**保留现场**（没有 trap、没有 try/finally 式清理）
#
# 上线顺序：worker 缩到 0 → 本脚本 --apply → 起 worker → 验证
set -euo pipefail

SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

NAMESPACE="${MIGRATE_NAMESPACE:-sandlock}"
CONFIGMAP="state-owner-migrate"
JOB="state-owner-migrate"
JOB_MANIFEST="$REPO_ROOT/deploy/k8s-k0s/state-owner-migrate.yaml"
CP_POD="${MIGRATE_CP_POD:-deploy/control-plane}"
CP_CONTAINER="${MIGRATE_CP_CONTAINER:-control-plane}"
EXPORT_IN_POD="${MIGRATE_EXPORT_IN_POD:-/var/lib/e2b-sandboxes}"
JOB_TIMEOUT="${MIGRATE_JOB_TIMEOUT:-900}"
VERSION_FILE="$REPO_ROOT/deploy/stack/.version"

DRY_RUN=1
IN_CLUSTER=0
KEEP_JOB=0
PRINT_PLAN=0
RENDER_JOB=0
OFFLINE_ROOT=""
#: 本地观测到的 worker 副本数（`check_worker_stopped` 填；渲染 Job 时用它）。
observed_replicas=""
#: 计划之外的追加条目（`--target`，成对存 `--target` + 值）。追加一条已经在默认计划里的
#: 路径是幂等的（引擎会去重），落在 workspaces/ 之下则一律拒绝。
TARGETS=()

refuse() {
    printf 'REFUSE(%s): %s\n' "$1" "$2" >&2
    exit "$1"
}

warn() {
    printf 'WARN: %s\n' "$*" >&2
}

usage() {
    # 头部注释块（到第一个非注释行为止）就是用法。
    sed -n '2,40p' "$SCRIPT_PATH"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --apply) DRY_RUN=0 ;;
        --print-plan) PRINT_PLAN=1 ;;
        --render-job) RENDER_JOB=1 ;;
        --in-cluster) IN_CLUSTER=1 ;;
        --keep-job) KEEP_JOB=1 ;;
        --target) shift; TARGETS[${#TARGETS[@]}]="--target"; TARGETS[${#TARGETS[@]}]="${1:-}" ;;
        --target=*) TARGETS[${#TARGETS[@]}]="--target"; TARGETS[${#TARGETS[@]}]="${1#--target=}" ;;
        --root) shift; OFFLINE_ROOT="${1:-}" ;;
        --root=*) OFFLINE_ROOT="${1#--root=}" ;;
        -h|--help) usage; exit 0 ;;
        *) refuse 2 "未知参数：$1（--help 看用法）" ;;
    esac
    shift
done

# 引擎正文。刻意写成**函数里的 heredoc** 而不是 `X="$(cat <<'PY')"`：后者在 macOS 自带的
# bash 3.2 上会被解析坏（它扫 `$( )` 时会跟着 heredoc 正文里的单引号走，而 python 到处都是
# 单引号），这台开发机默认就是那份 bash。
py_engine() {
    cat <<'PY_ENGINE'
#!/usr/bin/env python3
"""C1 wave 2 平台态属主一次性迁移引擎：**只 chown**：不改权限位、不删东西、不动内容。

由 `deploy/scripts/migrate-state-owner.sh` 内嵌并 `python3 -` 执行（本机 `--print-plan`
与 `--root` 彩排直接跑，真迁移时由 Job 在容器里跑）。

唯一的写操作是 `chown -R 65534:65534 <target>`；这里不改权限位、没有 unlink/rmdir、
没有任何形式的删除 —— 迁移改的只是属主。路径计划是显式数据，每一条都过
`normalize_target()`：归一化（`posixpath.normpath` 把 `..` 消掉）之后只要落在
`<export>/workspaces/` 之下就拒绝（**唯一例外**是那一条确切条目
`workspaces/_migrate`，控制面的迁移暂存）—— 其余的都是池 uid 的沙箱树，不是 worker 的。
根目录在盘上时再过一道 `realpath`：放行的那一条必须**就是它自己那个真实目录**，符号
链接绕过去的拼写（包括换个名字解析到它的）同样被拒。
"""

from __future__ import annotations

import argparse
import os
import posixpath
import stat
import subprocess
import sys

EXIT_PLAN = 2
EXIT_SHAPE = 3
EXIT_CHOWN = 4

WORKER_UID = 65534
WORKER_GID = 65534

#: 平台态：worker（uid 65534）要读写它们，所以属主得是 worker。顺序即 TARGET 顺序。
#: `_volumes` 不在其中 —— 它的数据要被 bind 进沙箱，属主是池 uid 的账，另做。
PLATFORM_TARGETS = (
    "state",
    "workspaces/_migrate",
    "_images",
    "_secrets",
    "_snapshots",
    "_templates",
    "_builds",
)

#: 沙箱树：池 uid 的树，不是 worker 的。这个前缀下除 `MIGRATE_STAGING` 外一律拒绝。
SANDBOX_TREES = "workspaces"

#: 树根下唯一放行的那一条：控制面的迁移暂存（N27 之后它在 `<export>/workspaces/` 之下，
#: 不在 export 根上 —— 这条路径与 `workspace-root-init`、控制面 subPath、worker 的迁移
#: 端点四处同名）。写白名单而不是"解析到它就放行"：只有这一条确切条目能过。
MIGRATE_STAGING = "workspaces/_migrate"


class Refuse(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def emit(line):
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def normalize_target(raw, root):
    """一条计划路径 -> `<export>` 相对的 posix 路径；越界/落在沙箱树下都拒绝。"""
    if not raw:
        raise Refuse(EXIT_PLAN, "拒绝：计划里有一条空路径")
    text = raw
    if text.startswith("/"):
        base = root.rstrip("/") or "/"
        normalized = posixpath.normpath(text)
        if normalized == base:
            raise Refuse(EXIT_PLAN, "拒绝：%s 就是 export 根本身（没有可迁的目录）" % raw)
        if not normalized.startswith(base + "/"):
            raise Refuse(EXIT_PLAN, "拒绝：%s 不在 export 根 %s 之下" % (raw, root))
        text = normalized[len(base):]
    rel = posixpath.normpath(text)
    parts = [part for part in rel.split("/") if part not in ("", ".")]
    if not parts:
        raise Refuse(EXIT_PLAN, "拒绝：%s 就是 export 根本身（没有可迁的目录）" % raw)
    if parts[0] == "..":
        raise Refuse(EXIT_PLAN, "拒绝：%s 用 .. 爬到 export 根之外" % raw)
    rel = "/".join(parts)
    if parts[0] == SANDBOX_TREES and rel != MIGRATE_STAGING:
        raise Refuse(
            EXIT_PLAN,
            "拒绝：%s 落在 %s/ 之下（沙箱树属于池 uid，不是 worker 的；树根下唯一放行的"
            "是 %s）" % (raw, SANDBOX_TREES, MIGRATE_STAGING),
        )
    return rel


def assert_no_symlink_escape(root, rel, raw):
    """`..` 由词法归一化挡住；符号链接只有 `realpath` 看得见。"""
    real_root = os.path.realpath(root)
    real = os.path.realpath(os.path.join(root, rel))
    trees = os.path.join(real_root, SANDBOX_TREES)
    if rel == MIGRATE_STAGING:
        # 放行的那一条必须**就是它自己**：`<export>/workspaces/_migrate` 若是一条指向
        # 别处的符号链接（指向沙箱树、指向 export 之外都算），realpath 就落到别处了。
        expected = os.path.join(trees, "_migrate")
        if real != expected:
            raise Refuse(
                EXIT_PLAN,
                "拒绝：%s 经符号链接落到 %s —— 迁移只碰真实目录，且树根下放行的就是它自己"
                "那一个（停下来人看）" % (raw, real),
            )
        return
    if real == trees or real.startswith(trees + os.sep):
        raise Refuse(
            EXIT_PLAN,
            "拒绝：%s 经符号链接落在 %s/ 之下（%s）—— 沙箱树属于池 uid，不是 worker 的"
            % (raw, SANDBOX_TREES, real),
        )
    if real != real_root and not real.startswith(real_root + os.sep):
        raise Refuse(EXIT_PLAN, "拒绝：%s 经符号链接跑到 export 根之外（%s）" % (raw, real))


def counts_of(path):
    """(文件数, 目录数) —— 不含 path 自己。chown 前后必须一致。"""
    files = dirs = 0
    for _cur, subdirs, names in os.walk(path):
        dirs += len(subdirs)
        files += len(names)
    return files, dirs


def assert_export_shape(root):
    """形状闸门（与 migrate-state-base.sh 的"这不像是本平台的 export 根"同形）。

    判据：`<root>/state` 与 `<root>/state/_runtime` **都不在**才拒绝 —— 那说明挂进来的
    不是这份卷（PVC 没挂上、挂错了），或者根给错了。只要有一个在，就当它是合法 export
    根：`state/_runtime` 缺失是"这个集群还没有任何沙箱记录"的合法状态，不该拦住迁移。
    运行的是 `--plan-only`（纯路径计划，不读盘）时这道闸门不参与 —— 它是对实物的判断。
    """
    state = os.path.join(root, "state")
    runtime = os.path.join(root, "state", "_runtime")
    if os.path.lexists(state) or os.path.lexists(runtime):
        return
    raise Refuse(
        EXIT_SHAPE,
        "没有任何目标被 chown：%s 下既没有 state 也没有 state/_runtime —— 这不像是本平台的 "
        "export 根（卷没挂上？--root/MIGRATE_ROOT 给错了？），停下来人看" % root,
    )


def describe(rel, path, prefix):
    """打印一个目录的 stat（属主/权限位）与条目数 —— 跑完便于和迁移前对比。"""
    st = os.lstat(path)
    files, dirs = counts_of(path)
    emit(
        "%s rel=%s uid=%d gid=%d mode=0%o files=%d dirs=%d"
        % (prefix, rel, st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode), files, dirs)
    )
    return st.st_uid, st.st_gid, files, dirs


def main(argv):
    parser = argparse.ArgumentParser(prog="migrate-state-owner", add_help=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--mode", choices=("plan", "apply"), default="plan")
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--target", action="append", default=[])
    args = parser.parse_args(argv)

    root = args.root
    # 计划去重（追加一条已经在默认计划里的路径不会 chown 两遍），顺序保持声明顺序。
    declared = list(dict.fromkeys(PLATFORM_TARGETS + tuple(args.target)))
    try:
        plan = [(raw, normalize_target(raw, root)) for raw in declared]
        if os.path.isdir(root):
            for raw, rel in plan:
                assert_no_symlink_escape(root, rel, raw)
            # 形状闸门要在**打印与动手之前**：挂上来的不是这份卷时，一条计划都别当真。
            if not args.plan_only:
                assert_export_shape(root)

        mode = "apply" if (args.mode == "apply" and not args.plan_only) else "plan"
        emit("== state-owner-migration(C1 wave 2) ==")
        emit(
            "mode=%s root=%s dry_run=%d uid=%d gid=%d"
            % (mode, root, 1 if mode == "plan" else 0, WORKER_UID, WORKER_GID)
        )
        if not os.path.isdir(root):
            if not args.plan_only:
                raise Refuse(EXIT_SHAPE, "--root 不是目录：%s" % root)
            emit("NOTE %s 不在本机盘上：只打印路径计划（不 stat、不 chown）" % root)
        for _raw, rel in plan:
            emit("TARGET rel=%s owner=%d:%d" % (rel, WORKER_UID, WORKER_GID))

        chowned = missing = 0
        for _raw, rel in plan:
            path = os.path.join(root, rel)
            if not os.path.lexists(path):
                emit("MISSING rel=%s（不在盘上：跳过 —— 这条对第二次跑就是幂等的）" % rel)
                missing += 1
                continue
            if os.path.islink(path):
                raise Refuse(EXIT_SHAPE, "%s 是符号链接 —— 迁移只碰真实目录（停下来人看）" % rel)
            if not os.path.isdir(path):
                raise Refuse(EXIT_SHAPE, "%s 不是目录" % rel)
            before = describe(rel, path, "STAT")
            if mode == "plan":
                continue
            proc = subprocess.run(
                ["chown", "-R", "%d:%d" % (WORKER_UID, WORKER_GID), path],
                capture_output=True,
                text=True,
            )
            if proc.returncode != 0:
                raise Refuse(
                    EXIT_CHOWN,
                    "chown -R %d:%d %s 失败（rc=%d）：%s"
                    % (WORKER_UID, WORKER_GID, rel, proc.returncode, proc.stderr.strip()),
                )
            after = describe(rel, path, "AFTER")
            if (after[0], after[1]) != (WORKER_UID, WORKER_GID):
                raise Refuse(
                    EXIT_CHOWN,
                    "%s chown 之后属主还是 %d:%d（存储把 chown 当成 no-op？）—— 停下来查"
                    % (rel, after[0], after[1]),
                )
            if (after[2], after[3]) != (before[2], before[3]):
                raise Refuse(
                    EXIT_CHOWN,
                    "%s chown 前后条目数变了（files %d->%d, dirs %d->%d）—— 停下来查"
                    % (rel, before[2], after[2], before[3], after[3]),
                )
            emit("CHOWN rel=%s uid=%d gid=%d" % (rel, WORKER_UID, WORKER_GID))
            chowned += 1

        # apply 模式什么也没 chown 就不算成功（"跑完了"必须是"真的迁了"，否则运维会以为
        # 迁移完成，起来 worker 才发现平台态还是 root 的 0600）。形状闸门先兜"卷没挂上"，
        # 这一条兜住"计划与实物都对不上"的其它样子。
        if mode == "apply" and chowned == 0:
            raise Refuse(
                EXIT_SHAPE,
                "没有任何目标被 chown：apply 模式跑完 %d 条计划、一条都没落地 —— 卷没挂上、"
                "或者 --root/MIGRATE_ROOT 给错了根，停下来人看" % len(plan),
            )

        emit(
            "SUMMARY mode=%s targets=%d chowned=%d missing=%d"
            % (mode, len(plan), chowned, missing)
        )
        return 0
    except Refuse as exc:
        sys.stderr.write("REFUSE(%d): %s\n" % (exc.code, exc.message))
        sys.stderr.flush()
        return exc.code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
PY_ENGINE
}

# --- 只读的集群自检（与 migrate-state-base.sh 同一姿态：先认集群，再敲命令） ----

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

require_python() {
    command -v python3 >/dev/null || refuse 2 "缺少 python3（迁移引擎与集群自检都用它）"
}

check_kubeconfig() {
    local want got
    want="$REPO_ROOT/tmp/k0s/kubeconfig"
    if [ -z "${KUBECONFIG:-}" ]; then
        refuse 2 "KUBECONFIG 没设：export KUBECONFIG=\"\$PWD/tmp/k0s/kubeconfig\"（本机默认 context 指的是另一套 ACK 集群，见 docs/deploy-clusters.md §1）"
    fi
    got="$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$KUBECONFIG")"
    if [ "$got" != "$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$want")" ]; then
        refuse 2 "KUBECONFIG=$KUBECONFIG 不是本项目那一份（要 ${want}）——绝不要把写操作打到别的集群上，见 docs/deploy-clusters.md §0"
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

check_worker_stopped() {
    local replicas pods want_replicas
    want_replicas=0
    # 两条读盘都显式 `|| refuse`：`local x="$(cmd)"` 会把 cmd 的失败吞掉（`local` 自己返回 0），
    # 而且读不到就等于"停写没有被验证过"—— 点名拒绝，不要被 `set -e` 静默带走。
    replicas="$(kubectl -n "$NAMESPACE" get statefulset/e2b-worker -o jsonpath='{.spec.replicas}')" ||
        refuse 2 "kubectl get statefulset/e2b-worker 失败 —— 停写没有被验证过，拒绝继续（通道断了？RBAC？）"
    if [ "$replicas" != "$want_replicas" ]; then
        refuse 2 "statefulset/e2b-worker 有 $replicas 个副本（要 want_replicas=0）——先停写：kubectl -n $NAMESPACE scale statefulset/e2b-worker --replicas=0 && kubectl -n $NAMESPACE wait --for=delete pod -l app=e2b-worker --timeout=300s"
    fi
    pods="$(kubectl -n "$NAMESPACE" get pods -l app=e2b-worker -o name)" ||
        refuse 2 "kubectl get pods -l app=e2b-worker 失败 —— 停写没有被验证过，拒绝继续（通道断了？RBAC？）"
    if [ -n "$pods" ]; then
        refuse 2 "app=e2b-worker 还有 pod 在跑（停写没完成）：$(printf '%s' "$pods" | tr '\n' ' ')"
    fi
    # 观到的副本数从 stdout 交给调用点（只有这一个数 Job 自己看不到）。它此刻必然是 0；
    # 万一没走到这里，渲染出来的空值也会让 Job 自己拒绝。
    printf '%s' "$replicas"
}

#: 渲染出来 / 观测到的副本数必须是 0：这份 Job 只允许在"有人确认过 worker 停写"之后跑。
#: 直接 apply 原清单时这个值是字符串 `__WORKER_REPLICAS__`，于是这里立刻拒。
gate_replicas_zero() {
    local replicas="$1"
    if [ "$replicas" != "0" ]; then
        refuse 2 "MIGRATE_WORKER_REPLICAS=${replicas:-<未设>} 不是 0：worker 停写没有被验证过（scale statefulset/e2b-worker --replicas=0 再确认没有 worker pod）。这份 Job 必须由 deploy/scripts/migrate-state-owner.sh 渲染后 apply，直接 apply 清单是不会 chown 任何东西的"
    fi
}

# --- 三种执行路径 ---------------------------------------------------------

run_engine() {
    local root="$1"
    shift
    py_engine | python3 - --root "$root" "$@"
}

cluster_engine() {
    py_engine | kubectl -n "$NAMESPACE" exec -i "$CP_POD" -c "$CP_CONTAINER" -- python3 - --root "$EXPORT_IN_POD" "$@"
}

#: 渲染 Job 时填进 `args:` 的那串 YAML（`__ENGINE_FLAGS__` 在清单里是唯一一项）。
render_flags() {
    printf '%s' '"--in-cluster", "--apply"'
}

render_job() {
    local version replicas
    version="${VERSION:-}"
    if [ -z "$version" ]; then
        version="$(cat "$VERSION_FILE" 2>/dev/null || true)"
    fi
    if [ -z "$version" ]; then
        refuse 2 "拿不到镜像版本：设 VERSION=… 或让 $VERSION_FILE 存在（与 apply.sh 同一口径）"
    fi
    # 优先用本地（或显式传入）的观测值：这份 Job 拿到的 0 必须是有人真的看过
    # `statefulset/e2b-worker` 才写进去的。两者都没有时留空 —— 这里就拒绝。
    replicas="${MIGRATE_WORKER_REPLICAS:-${observed_replicas:-}}"
    gate_replicas_zero "$replicas"
    python3 - "$JOB_MANIFEST" "$version" "$replicas" "$(render_flags)" <<'PY_RENDER'
import pathlib
import re
import sys

manifest, version, replicas, flags = sys.argv[1:5]
text = pathlib.Path(manifest).read_text(encoding="utf-8")
for token, value in (
    ("__IMAGE_VERSION__", version),
    ("__WORKER_REPLICAS__", replicas),
    ("__ENGINE_FLAGS__", flags),
):
    if token not in text:
        sys.exit("清单缺少占位符 %s（%s）" % (token, manifest))
    text = text.replace(token, value)
leftover = sorted(set(re.findall(r"__[A-Z_]+__", text)))
if leftover:
    sys.exit("渲染后仍有占位符：%s" % " ".join(leftover))
sys.stdout.write(text)
PY_RENDER
}

job_wait() {
    local deadline succeeded failed
    deadline=$(( $(date +%s) + JOB_TIMEOUT ))
    while :; do
        succeeded="$(kubectl -n "$NAMESPACE" get job "$JOB" -o jsonpath='{.status.succeeded}' 2>/dev/null || true)"
        failed="$(kubectl -n "$NAMESPACE" get job "$JOB" -o jsonpath='{.status.failed}' 2>/dev/null || true)"
        if [ "${succeeded:-0}" != "0" ]; then
            return 0
        fi
        if [ -n "$failed" ] && [ "$failed" != "0" ]; then
            return 1
        fi
        if [ "$(date +%s)" -ge "$deadline" ]; then
            warn "等 job/$JOB 超过 ${JOB_TIMEOUT}s"
            return 1
        fi
        sleep 3
    done
}

job_run() {
    local rendered
    rendered="$(render_job)"
    # 变量紧挨着全角字符时必须写 `${VAR}`：macOS 自带的 bash 3.2 会把多字节字符的首字节吞进
    # 变量名（`JOB\xef: unbound variable`），`set -u` 下直接中止。
    warn "迁移由 Job 执行：${JOB}（runAsUser 0，PVC sandbox-shared 以 RW 挂到 ${EXPORT_IN_POD}）"
    kubectl -n "$NAMESPACE" create configmap "$CONFIGMAP" \
        --from-file="migrate-state-owner.sh=$SCRIPT_PATH" --dry-run=client -o yaml |
        kubectl -n "$NAMESPACE" apply -f - >&2
    printf '%s\n' "$rendered" | kubectl -n "$NAMESPACE" apply -f - >&2
    if ! job_wait; then
        warn "job/$JOB 没有成功完成，日志与 pod 状态如下（对象保留，便于排查）"
        kubectl -n "$NAMESPACE" logs "job/$JOB" || true
        kubectl -n "$NAMESPACE" get pods -l job-name="$JOB" -o wide || true
        refuse 1 "Job 失败：看上面的日志。job/${JOB} 与 configmap/${CONFIGMAP} 都留着（查完手动 kubectl -n $NAMESPACE delete job/${JOB} configmap/${CONFIGMAP}）"
    fi
    kubectl -n "$NAMESPACE" logs "job/$JOB"
    if [ "$KEEP_JOB" = "1" ]; then
        warn "保留 job/${JOB} 与 configmap/${CONFIGMAP}（--keep-job）"
        return 0
    fi
    kubectl -n "$NAMESPACE" delete job "$JOB" --ignore-not-found
    kubectl -n "$NAMESPACE" delete configmap "$CONFIGMAP" --ignore-not-found
}

# --- 主流程 ---------------------------------------------------------------
#
# 给引擎传 `--target` 时一律写 `${TARGETS[@]+"${TARGETS[@]}"}`：bash 3.2 + `set -u` 下，
# 空数组的 `"${arr[@]}"` 会被当成"未绑定变量"直接报错，而这个写法让空数组展开成空。

# `--print-plan`：只打印路径计划，**不连集群**（单测与上线前都靠它）。根目录在本机盘上
# 时顺带把 stat/条目数也打出来（只读）；不在盘上就只打计划。
if [ "$PRINT_PLAN" = "1" ]; then
    require_python
    run_engine "${OFFLINE_ROOT:-${MIGRATE_ROOT:-$EXPORT_IN_POD}}" \
        --mode plan --plan-only ${TARGETS[@]+"${TARGETS[@]}"} || exit $?
    exit 0
fi

# `--render-job`：只把渲染后的 Job YAML 打到 stdout，**不连集群**（给单测与 review 用）。
if [ "$RENDER_JOB" = "1" ]; then
    require_python
    render_job
    exit 0
fi

if [ -n "$OFFLINE_ROOT" ]; then
    printf 'OFFLINE 彩排：root=%s（不连集群、不查 worker=0、不做集群身份自检）\n' "$OFFLINE_ROOT"
    if [ "$DRY_RUN" = "0" ]; then
        run_engine "$OFFLINE_ROOT" --mode apply ${TARGETS[@]+"${TARGETS[@]}"} || exit $?
    else
        run_engine "$OFFLINE_ROOT" --mode plan ${TARGETS[@]+"${TARGETS[@]}"} || exit $?
    fi
    exit 0
fi

if [ "$IN_CLUSTER" = "1" ]; then
    ROOT="${MIGRATE_ROOT:-$EXPORT_IN_POD}"
    # Job 的清单把它固定成 <export>（PVC 以 RW 挂在那里）。这里只兜底"不是 /、而且真的存在"，
    # 因为运维也可能在节点上以这个模式对着挂载点跑。
    if [ "$ROOT" = "/" ] || [ ! -d "$ROOT" ]; then
        refuse 2 "MIGRATE_ROOT=$ROOT 不是一个可迁移的 export 根（不存在，或者就是 /）"
    fi
    gate_replicas_zero "${MIGRATE_WORKER_REPLICAS:-}"
    # 与外面同一条纪律：DRY_RUN=1 是默认，只有显式 --apply（Job 的 args）才写。
    if [ "$DRY_RUN" = "0" ]; then
        run_engine "$ROOT" --mode apply ${TARGETS[@]+"${TARGETS[@]}"} || exit $?
    else
        run_engine "$ROOT" --mode plan ${TARGETS[@]+"${TARGETS[@]}"} || exit $?
    fi
    exit 0
fi

require_python
check_kubeconfig
check_cluster_identity
# 观测到的副本数（此刻必然是 0）带进 Job 的 env：`refuse` 在子 shell 里 exit 非零会让
# 这个赋值非零、`set -e` 立刻收工，所以被拒时的行为与直接调用它一样。
observed_replicas="$(check_worker_stopped)"

# 写操作之前先把只读的计划跑一遍：经控制面 pod（它那份 export 是只读挂载，正好只够读计划），
# 任何口径不一致都会在这里先响。
if [ "$DRY_RUN" = "1" ]; then
    cluster_engine --mode plan ${TARGETS[@]+"${TARGETS[@]}"}
    exit $?
fi
cluster_engine --mode plan ${TARGETS[@]+"${TARGETS[@]}"} || exit $?
job_run
