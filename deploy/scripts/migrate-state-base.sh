#!/usr/bin/env bash
# N27 Task 6 —— 一次性把平台状态从「与树根同一个目录」搬到「树根下沉一级后的兄弟目录」。
#
# 布局（2026-09-26 决策 1：同挂载 + 树根下沉）：
#
#   <export>/workspaces/<id>   ← <export>/<id>          沙箱树（逐条 rename）
#   <export>/state/_runtime    ← <export>/_runtime      记录 + 命令日志 + .checkpoints
#   <export>/state/.route-b    ← <export>/.route-b      route-B 槽位
#   <export>/state/            （新建 1777）
#   <export>/workspaces/       （新建 1777）+ 其中的 _migrate（1777）
#   <export>/.uid_reservations 确认空 → 留在原地（--delete-after 才 rmdir）
#   <export>/.uid_pool.lock    不搬（锁文件，新位置按需重建）
#   <export>/_builds … _volumes  六个平台命名空间留在 export 根
#
# 为什么主路径是 `rename(2)`：它的边界是**挂载点**，不是服务器上的同一个文件系统。
# 这两棵新树与旧位置在同一个挂载里（deploy/k8s-k0s/storage-nas.yaml 的文件头解释了
# 为什么必须如此），所以每一步都是秒级元数据操作 —— checkpoint 镜像是 GiB 级，整树
# 拷贝 + 逐文件校验那条路（决策 1 里被否决的 A 形态）在这里既不必要也不可接受。
#
# 用法：
#   deploy/scripts/migrate-state-base.sh                     # dry-run（默认，只看不写）
#   deploy/scripts/migrate-state-base.sh --apply             # 建 configmap + Job，收日志后清理
#   deploy/scripts/migrate-state-base.sh --rollback          # 反向计划（只看不写）
#   deploy/scripts/migrate-state-base.sh --rollback --apply  # 按同一份 journal 原路退回
#   deploy/scripts/migrate-state-base.sh --root DIR [--apply] # 本机彩排（不连集群）
#   deploy/scripts/migrate-state-base.sh --apply --keep-job   # 跑完留着 Job（排查用）
#   MIGRATE_JOB_TIMEOUT=1800 … --apply                       # 等 Job 的上限（秒）
#
# 硬性质（tests/unit/test_migrate_state_base_script.py 逐行钉住）：
#   * DRY_RUN=1 是默认；只有显式 --apply（以及 Job 里的 --in-cluster --apply）才写
#   * 一切搬迁都是 rename，不 copy；删除只有 --delete-after 做的那两件：unlink 那个陈旧
#     锁文件、rmdir 那几个空壳（没有任何递归删除）
#   * 自己创建的每一个文件 0600（umask 077 + journal 显式 chmod）——它创建在
#     <export>/state 下，`_migrate` 那种 1777 目录里的东西一个都不碰
#   * worker 不在 0 副本、或还有 worker pod 在跑，就拒绝
#   * 先跑一遍只读的计划（经控制面 pod），再让 Job 去写
#
# 上线顺序（与 deploy/k8s/worker.yaml、deploy/k8s/control-plane.yaml 的注释一致）：
#   worker 缩到 0 → 本脚本 --apply → apply.sh（新清单）→ 起 worker → 验证
set -euo pipefail

# 本脚本创建的每一个文件都必须 0600（硬要求：`_migrate` 是 1777，暂存文件若按 0644
# 建，迁移窗口内猜到名字的沙箱就能读别人的归档）。目录由调用点显式 chmod。
umask 077

SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

NAMESPACE="${MIGRATE_NAMESPACE:-sandlock}"
CONFIGMAP="state-base-migrate"
JOB="state-base-migrate"
JOB_MANIFEST="$REPO_ROOT/deploy/k8s-k0s/state-base-migrate.yaml"
CP_POD="${MIGRATE_CP_POD:-deploy/control-plane}"
CP_CONTAINER="${MIGRATE_CP_CONTAINER:-control-plane}"
EXPORT_IN_POD="${MIGRATE_EXPORT_IN_POD:-/var/lib/e2b-sandboxes}"
JOB_TIMEOUT="${MIGRATE_JOB_TIMEOUT:-900}"

DRY_RUN=1
IN_CLUSTER=0
KEEP_JOB=0
DELETE_AFTER=0
ENGINE_MODE=forward
OFFLINE_ROOT=""
#: 本地观测到的 worker 副本数（`check_worker_stopped` 填；渲染 Job 时用它）。
observed_replicas=""

refuse() {
    printf 'REFUSE(%s): %s\n' "$1" "$2" >&2
    exit "$1"
}

warn() {
    printf 'WARN: %s\n' "$*" >&2
}

usage() {
    sed -n '2,40p' "$SCRIPT_PATH"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --apply) DRY_RUN=0 ;;
        --rollback) ENGINE_MODE=rollback ;;
        --delete-after) DELETE_AFTER=1 ;;
        --in-cluster) IN_CLUSTER=1 ;;
        --keep-job) KEEP_JOB=1 ;;
        --root) shift; OFFLINE_ROOT="${1:-}" ;;
        --root=*) OFFLINE_ROOT="${1#--root=}" ;;
        -h|--help) usage; exit 0 ;;
        *) refuse 2 "未知参数：$1（--help 看用法）" ;;
    esac
    shift
done

# --- 引擎参数：bash 只做编排，真正读盘/改名的是内嵌的 python -----------------
ENGINE_ARGS="--mode plan"
if [ "$DRY_RUN" = "0" ]; then
    ENGINE_ARGS="--mode apply"
fi
if [ "$ENGINE_MODE" = "rollback" ]; then
    ENGINE_ARGS="--mode rollback"
    if [ "$DRY_RUN" = "1" ]; then
        ENGINE_ARGS="$ENGINE_ARGS --dry-run"
    fi
fi
if [ "$DELETE_AFTER" = "1" ]; then
    ENGINE_ARGS="$ENGINE_ARGS --delete-after"
fi

# 引擎正文。刻意写成**函数里的 heredoc** 而不是 `X="$(cat <<'PY')"`：后者在
# macOS 自带的 bash 3.2 上会被解析坏（它在扫 `$( )` 时会跟着 heredoc 正文里的单引号
# 走，而 python 到处都是单引号），这台开发机默认就是那份 bash。
py_engine() {
    cat <<'PY_ENGINE'
#!/usr/bin/env python3
"""N27 state-base 迁移引擎：同挂载、逐条 `rename(2)`，默认只看不写。

由 `deploy/scripts/migrate-state-base.sh` 内嵌并 `python3 -` 执行：本机 dry-run 时
经 `kubectl exec -i` 送进控制面容器（它那份 export 是只读挂载，正好只够读计划），
真迁移时由 Job 在容器里直接跑（`--root /shared`）。

写操作只有三种：`os.mkdir`（三个目录）、`os.rename`（每一棵树/每一个状态目录）、
以及 `--delete-after` 的 `os.unlink`（一个普通文件）/`os.rmdir`（空目录）。没有任何
递归删除，也没有拷贝分支 —— 跨挂载（EXDEV）是拒绝，不是退化成整树拷贝。
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import os
import re
import stat
import sys
import time

EXIT_GATE = 2
EXIT_SHAPE = 3
EXIT_MOVE = 4

#: 平台自己的存储，挂在 **export 根**（`E2B_SHARED_WORKSPACE_ROOT`）：它们不是沙箱
#: 树，留在原地。
STAY_NAMESPACES = (
    "_builds",
    "_images",
    "_secrets",
    "_templates",
    "_snapshots",
    "_volumes",
)

#: 平台状态：搬到新 base（`E2B_STATE_BASE`）。顺序就是 `mv` 的顺序。
STATE_MOVES = ("_runtime", ".route-b")

#: 预约目录：必须为空（在途建箱时搬走会丢预约），搬完留在原处，只有 --delete-after
#: 才 rmdir 那个空壳。
RESERVATIONS = ".uid_reservations"

#: 陈旧的锁文件：新位置由 uid_pool 按需 `O_CREAT` 重建（`envd_service/uid_pool.py`），
#: 所以这个文件既不搬也不删，除非 --delete-after。
STALE_LOCK = ".uid_pool.lock"

#: 树根下的平台命名空间：这些是**相对树根**解析的（`_migrate` 是控制面的迁移暂存、
#: `_pure_rootfs` 是纯形态的每沙箱骨架、`_cow`/`_untrusted.trees` 是保留名），
#: 所以它们跟着树一起下沉。
SINK_NAMESPACES = ("_migrate", "_pure_rootfs", "_cow", "_untrusted.trees")
SINK_PREFIXES = ("snap_",)

STATE_DIR = "state"
TREES_DIR = "workspaces"

#: 迁移自己建的三层目录，顺序即创建顺序（state 先，因为状态目录要搬进去；
#: workspaces/_migrate 最后，因为它是控制面唯一的可写 subPath 源）。
NEW_DIRS = (
    (STATE_DIR, 0o1777),
    (TREES_DIR, 0o1777),
    (TREES_DIR + "/_migrate", 0o1777),
)

JOURNAL_NAME = ".state-base-migration.journal"
JOURNAL_REL = STATE_DIR + "/" + JOURNAL_NAME
ROLLED_BACK_JOURNAL = ".state-base-migration.journal.rolled-back"

#: 新布局自己的那两层目录名（`state` / `workspaces`）。两个名字都是**合法沙箱 id**，
#: 所以不能只按名字判它们"不是树"：要么带着新布局的标记，要么是空的；两个都不满足
#: 就拒绝，让人来看 —— 那很可能是一棵碰巧叫这个名字的沙箱树。
NEW_LAYOUT_MARKERS = {
    STATE_DIR: ("_runtime", ".route-b", RESERVATIONS, JOURNAL_NAME),
    TREES_DIR: ("_migrate",),
}

#: 镜像 `gateway_common.paths.validate_sandbox_id`：线上这些树的名字由客户端挑，
#: 但必须是合法 id。不合法又不是平台命名空间的顶层目录一律拒绝（人来看）。
SANDBOX_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

SAMPLE_LIMIT = 8
SAMPLE_MAX_BYTES = 8 * 1024 * 1024


class Refuse(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class Move:
    def __init__(self, kind, src, dst):
        self.kind = kind
        self.src = src
        self.dst = dst
        self.todo = True


def emit(line):
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def counts_of(path):
    """(文件数, 目录数, 字节数) —— 不含 path 自己。"""
    files = dirs = nbytes = 0
    if not os.path.isdir(path):
        try:
            st = os.lstat(path)
        except OSError:
            return 0, 0, 0
        return 1, 0, st.st_size
    for cur, subdirs, names in os.walk(path):
        dirs += len(subdirs)
        for name in names:
            st = os.lstat(os.path.join(cur, name))
            files += 1
            if stat.S_ISREG(st.st_mode):
                nbytes += st.st_size
    return files, dirs, nbytes


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sample_files(root, prefixes, limit=SAMPLE_LIMIT):
    """`prefixes` 下按路径排序的前若干个小文件（相对 root 的路径）。

    只抽 ≤ 8 MiB 的普通文件：checkpoint 镜像可能有 GiB 级，抽样是为了证明"搬的是
    同一份字节"，不是为了给整卷做校验和。
    """
    out = []
    for prefix in prefixes:
        base = os.path.join(root, prefix)
        if not os.path.isdir(base):
            continue
        for cur, dirs, names in os.walk(base):
            dirs.sort()
            names.sort()
            for name in names:
                path = os.path.join(cur, name)
                st = os.lstat(path)
                if not stat.S_ISREG(st.st_mode) or st.st_size > SAMPLE_MAX_BYTES:
                    continue
                out.append(os.path.relpath(path, root))
                if len(out) >= limit:
                    return out
    return out


def mount_summary(root):
    """三个根必须在同一个挂载上 —— `st_dev` 预检（rename 自己才是判据）。"""
    root_dev = os.lstat(root).st_dev
    parts = ["root_dev=%d" % root_dev]
    for rel in (TREES_DIR, STATE_DIR, "_runtime"):
        path = os.path.join(root, rel)
        if os.path.lexists(path):
            dev = os.lstat(path).st_dev
            if dev != root_dev:
                raise Refuse(
                    EXIT_GATE,
                    "%s 与 export 根不在同一个挂载上（st_dev %d != %d）——这正是 2026-09-26 "
                    "决策 1 里被否决的 A 形态（state 走独立挂载）：rename(2) 会 EXDEV，而本脚本按"
                    "裁定只实现同挂载 rename，不做整树拷贝 + 逐文件校验。请把两者放回同一个挂载"
                    "（deploy/k8s-k0s/storage-nas.yaml 的文件头），或先按决策 1 重新拍板。"
                    % (rel, dev, root_dev),
                )
            parts.append("%s_dev=%d" % (rel, dev))
        else:
            parts.append("%s_dev=-" % rel)
    parts.append("（st_dev 预检只是代理，rename 自身才是判据）")
    return " ".join(parts)


def is_empty_dir(path):
    with os.scandir(path) as entries:
        return next(entries, None) is None


def scan(root):
    tree_moves, unknown = [], []
    seen = set()
    stale = False
    reservations = False
    for name in sorted(os.listdir(root)):
        seen.add(name)
        path = os.path.join(root, name)
        st = os.lstat(path)
        if name in STAY_NAMESPACES:
            if not stat.S_ISDIR(st.st_mode):
                raise Refuse(EXIT_SHAPE, "平台命名空间 %s 不是目录" % name)
        elif name in STATE_MOVES:
            if not stat.S_ISDIR(st.st_mode):
                raise Refuse(EXIT_SHAPE, "平台状态 %s 不是目录（迁移只搬目录）" % name)
        elif name == RESERVATIONS:
            if not stat.S_ISDIR(st.st_mode):
                raise Refuse(EXIT_SHAPE, "%s 不是目录" % RESERVATIONS)
            reservations = True
        elif name == STALE_LOCK:
            if not stat.S_ISREG(st.st_mode):
                raise Refuse(EXIT_SHAPE, "%s 不是普通文件" % STALE_LOCK)
            stale = True
        elif name in (STATE_DIR, TREES_DIR):
            # 新布局自己的两层目录。名字都是合法沙箱 id，所以不能只按名字判：要么带着
            # 新布局的标记（state/_runtime、.route-b、journal、workspaces/_migrate…），
            # 要么是空的；两个都不满足就拒绝 —— 那很可能是一棵碰巧叫这个名字的树。
            if not stat.S_ISDIR(st.st_mode):
                raise Refuse(EXIT_SHAPE, "%s 不是目录" % name)
            has_marker = any(
                os.path.lexists(os.path.join(path, marker))
                for marker in NEW_LAYOUT_MARKERS[name]
            )
            if not has_marker and not is_empty_dir(path):
                raise Refuse(
                    EXIT_SHAPE,
                    "顶层目录 %s 既没有新布局的标记、也不是空的 —— 无法判断它是新布局的"
                    "一层、还是一棵碰巧叫这个名字的沙箱树。停下来让人看一眼（手工把它搬"
                    "到确定的位置，或先清空）" % name,
                )
        elif stat.S_ISDIR(st.st_mode):
            if not SANDBOX_ID.match(name):
                raise Refuse(
                    EXIT_SHAPE,
                    "顶层目录 %s 既不是平台命名空间、也不是合法的沙箱 id —— 停下来让人看一眼"
                    % name,
                )
            tree_moves.append(Move("tree", name, TREES_DIR + "/" + name))
        else:
            unknown.append(name)
    if unknown:
        raise Refuse(
            EXIT_SHAPE,
            "顶层有本脚本不认识的条目（既不是目录、也不是 %s）：%s"
            % (STALE_LOCK, " ".join(unknown)),
        )
    # 输出顺序固定成声明顺序，而不是字母序：stay 按 STAY_NAMESPACES（与 worker 清单里
    # 那份列表同序），平台状态按 STATE_MOVES（先 _runtime，再 .route-b）。
    ordered_stay = [name for name in STAY_NAMESPACES if name in seen]
    state_moves = [
        Move("state", name, STATE_DIR + "/" + name)
        for name in STATE_MOVES
        if name in seen
    ]
    return {
        "stay": ordered_stay,
        "moves": state_moves + tree_moves,
        "stale": stale,
        "reservations": reservations,
    }


def resolve_moves(root, plan):
    """给每一步定 todo；两边都在或两边都不在都拒绝（宁可不搬，也不猜）。"""
    for move in plan["moves"]:
        src = os.path.join(root, move.src)
        dst = os.path.join(root, move.dst)
        src_exists = os.path.lexists(src)
        dst_exists = os.path.lexists(dst)
        if src_exists and dst_exists:
            raise Refuse(
                EXIT_SHAPE,
                "两边都有，拒绝猜：%s 与 %s 都存在（上一次迁移中断？先人工看一眼）"
                % (move.src, move.dst),
            )
        if not src_exists and not dst_exists:
            raise Refuse(
                EXIT_SHAPE,
                "%s 既不在旧位置、也不在新位置（数据不见了？停下来查）" % move.src,
            )
        move.todo = src_exists
    dirs_todo = [
        rel for rel, _mode in NEW_DIRS if not os.path.isdir(os.path.join(root, rel))
    ]
    for rel, _mode in NEW_DIRS:
        path = os.path.join(root, rel)
        if os.path.lexists(path) and not os.path.isdir(path):
            raise Refuse(EXIT_SHAPE, "%s 存在但不是目录" % rel)
    old_runtime = os.path.lexists(os.path.join(root, "_runtime"))
    new_runtime = os.path.lexists(os.path.join(root, STATE_DIR, "_runtime"))
    if not old_runtime and not new_runtime:
        raise Refuse(
            EXIT_SHAPE,
            "既没有 _runtime 也没有 %s/_runtime —— 这不像是本平台的 export 根（--root 给错了？）"
            % STATE_DIR,
        )
    todo = len([move for move in plan["moves"] if move.todo]) + len(dirs_todo)
    return dirs_todo, todo


def check_reservations(root, plan):
    if not plan["reservations"]:
        return
    entries = sorted(os.listdir(os.path.join(root, RESERVATIONS)))
    if entries:
        raise Refuse(
            EXIT_GATE,
            "%s 非空（有在途建箱，搬走会丢预约）：%s" % (RESERVATIONS, " ".join(entries)),
        )


def open_journal(journal_path):
    """打开（或续写）回退 journal。**0600**：它是唯一活过本次运行的文件。"""
    fd = os.open(journal_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.chmod(journal_path, 0o600)
    return fd


def journal_append(fd, line):
    os.write(fd, (line + "\n").encode("utf-8"))
    os.fsync(fd)


def rename_or_refuse(src, dst):
    try:
        os.rename(src, dst)
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            raise Refuse(
                EXIT_MOVE,
                "%s -> %s 跨了挂载点（EXDEV）。这是 2026-09-26 决策 1 里被否决的 A 形态：本脚本"
                "按裁定只实现同挂载 rename，不做整树拷贝 + 逐文件校验（checkpoint 镜像是 GiB 级，"
                "那条路的代价不是线性可接受的）。请把 state/workspaces 放回与 export 根同一个"
                "挂载（deploy/k8s-k0s/storage-nas.yaml 的文件头），或先按决策 1 重新拍板。"
                % (src, dst),
            ) from exc
        raise


def parse_journal(journal_path):
    entries = []
    with open(journal_path, encoding="utf-8") as handle:
        for line in handle:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if parts[0] == "move" and len(parts) == 3:
                entries.append(("move", parts[1], parts[2]))
            elif parts[0] == "mkdir" and len(parts) == 2:
                entries.append(("mkdir", parts[1], parts[1]))
            else:
                raise Refuse(EXIT_SHAPE, "journal 有看不懂的行：%r" % line)
    if not entries:
        raise Refuse(EXIT_SHAPE, "journal 是空的：%s" % journal_path)
    # journal 是本脚本自己写的，可回退本身是破坏性动作 —— 路径形状再过一道闸门。
    for _kind, src, dst in entries:
        for rel in (src, dst):
            if not rel or rel.startswith("/") or ".." in rel.split("/"):
                raise Refuse(EXIT_SHAPE, "journal 里的路径不安全：%r" % rel)
    return entries


def summary(mode, moves, dirs, todo, done):
    emit(
        "SUMMARY mode=%s moves=%d dirs=%d todo=%d done=%d unknown=0"
        % (mode, moves, dirs, todo, done)
    )


def delete_leftovers(root, plan, dry_run):
    stale = os.path.join(root, STALE_LOCK)
    if plan["stale"] and os.path.lexists(stale):
        if dry_run:
            emit("WOULD-DELETE %s" % STALE_LOCK)
        else:
            os.unlink(stale)
            emit("DELETE %s" % STALE_LOCK)
    for rel in (RESERVATIONS, "_runtime", ".route-b"):
        path = os.path.join(root, rel)
        if not os.path.lexists(path):
            continue
        if dry_run:
            emit("WOULD-RMDIR %s" % rel)
            continue
        try:
            os.rmdir(path)
        except OSError as exc:
            emit(
                "LEFTOVER %s（rmdir 拒绝 %s：非空或不可删 —— 这是有意的，本脚本没有任何递归删除）"
                % (rel, errno.errorcode.get(exc.errno, exc.errno))
            )
            continue
        emit("RMDIR %s" % rel)


def relocate(rel, moves):
    """一个样本路径在搬迁之后的位置（按最长的 src 前缀匹配）。"""
    best = None
    for move in moves:
        if rel == move.src or rel.startswith(move.src + "/"):
            if best is None or len(move.src) > len(best.src):
                best = move
    if best is None:
        return rel
    return best.dst + rel[len(best.src) :]


def forward(root, args):
    dry_run = args.dry_run or args.mode == "plan"
    mode = "plan" if dry_run else "apply"
    if not os.path.isdir(root):
        raise Refuse(EXIT_SHAPE, "--root 不是目录：%s" % root)
    plan = scan(root)
    dirs_todo, todo = resolve_moves(root, plan)
    mount = mount_summary(root)
    check_reservations(root, plan)
    if todo == 0:
        if not dry_run:
            raise Refuse(
                EXIT_SHAPE,
                "没有可搬的条目（旧位置都空、新位置都在）——已经迁移过？journal：%s"
                % os.path.join(root, JOURNAL_REL),
            )
        emit("NOTE 没有可搬的条目（旧位置都空、新位置都在）——已经迁移过？")

    emit("== state-base-migration(N27) ==")
    emit("mode=%s root=%s dry_run=%d" % (mode, root, 1 if dry_run else 0))
    emit("MOUNT %s" % mount)
    for name in plan["stay"]:
        emit("STAY %s" % name)
    for move in plan["moves"]:
        where = move.src if move.todo else move.dst
        files, dirs, nbytes = counts_of(os.path.join(root, where))
        emit(
            "MOVE %s -> %s kind=%s files=%d dirs=%d bytes=%d"
            % (move.src, move.dst, move.kind, files, dirs, nbytes)
        )
    for rel, dir_mode in NEW_DIRS:
        emit("MKDIR %s mode=%o" % (rel, dir_mode))
    if plan["stale"]:
        emit("KEEP %s kind=regular" % STALE_LOCK)
    if plan["reservations"]:
        emit(
            "LEAVE %s kind=empty-dir（已确认空；只有 --delete-after 才删）" % RESERVATIONS
        )

    if dry_run:
        if args.delete_after:
            delete_leftovers(root, plan, dry_run=True)
        summary("plan", len(plan["moves"]), len(NEW_DIRS), todo, 0)
        return 0

    # 1) 先记下"搬之前"的 inode 与计数：搬完要比对（inode 不变 ⇒ 是 rename 不是 copy）
    before = {}
    for move in plan["moves"]:
        if not move.todo:
            continue
        st = os.lstat(os.path.join(root, move.src))
        before[move.src] = (st.st_dev, st.st_ino, counts_of(os.path.join(root, move.src)))

    checkpoints_before = None
    for move in plan["moves"]:
        if move.todo and move.src == "_runtime":
            checkpoints_before = counts_of(
                os.path.join(root, "_runtime", ".checkpoints")
            )

    samples = sample_files(root, [move.src for move in plan["moves"] if move.todo])
    sample_before = {}
    for rel in samples:
        st = os.lstat(os.path.join(root, rel))
        sample_before[rel] = (sha256_of(os.path.join(root, rel)), st.st_ino)

    # 2) 三个目录（state / workspaces / workspaces/_migrate）。**先建目录再开 journal**：
    #    journal 自己就住在 state/ 里。（若在这两步之间崩了，只是留下几个空目录，
    #    没有任何数据被碰过。）umask 077 只清位、不补位，所以 1777 要显式 chmod ——
    #    与 worker 的 workspace-root-init 对这三个目录的判据一致。
    created_dirs = []
    for rel, dir_mode in NEW_DIRS:
        path = os.path.join(root, rel)
        if os.path.isdir(path):
            continue
        os.mkdir(path, dir_mode)
        os.chmod(path, dir_mode)
        emit("CREATED %s mode=%o" % (rel, dir_mode))
        created_dirs.append(rel)

    journal_path = os.path.join(root, JOURNAL_REL)
    journal_fd = open_journal(journal_path)
    journal_append(
        journal_fd,
        "# state-base-migration-journal v1\troot=%s\tstarted=%d"
        % (root, int(time.time())),
    )
    for rel in created_dirs:
        journal_append(journal_fd, "mkdir\t%s" % rel)

    done = len(created_dirs)
    try:
        # 3) 逐条 rename（平台状态先，沙箱树后 —— 与控制面读记录的时序一致）
        for move in plan["moves"]:
            if not move.todo:
                continue
            src = os.path.join(root, move.src)
            dst = os.path.join(root, move.dst)
            rename_or_refuse(src, dst)
            journal_append(journal_fd, "move\t%s\t%s" % (move.src, move.dst))
            emit("RENAME %s -> %s" % (move.src, move.dst))
            done += 1
    except BaseException:
        # 半途失败：journal 已经逐条落盘（含 fsync），把已搬的写清楚再退出。
        os.fsync(journal_fd)
        os.close(journal_fd)
        emit(
            "INCOMPLETE 已搬 %d 项；journal=%s；用 --rollback --apply 原路退回"
            % (done, journal_path)
        )
        raise

    # 4) 对账：inode 必须没变（rename 的证明）、文件计数必须一致、抽样 sha256 必须相同
    for move in plan["moves"]:
        if not move.todo:
            continue
        dev, ino, (files, dirs, nbytes) = before[move.src]
        st = os.lstat(os.path.join(root, move.dst))
        after = counts_of(os.path.join(root, move.dst))
        same_inode = (st.st_dev, st.st_ino) == (dev, ino)
        src_gone = not os.path.lexists(os.path.join(root, move.src))
        emit(
            "VERIFY %s -> %s dev=%d ino=%d same_inode=%s src_gone=%s "
            "files=%d->%d dirs=%d->%d bytes=%d->%d"
            % (
                move.src,
                move.dst,
                st.st_dev,
                st.st_ino,
                "yes" if same_inode else "NO",
                "yes" if src_gone else "NO",
                files,
                after[0],
                dirs,
                after[1],
                nbytes,
                after[2],
            )
        )
        if not same_inode or not src_gone or after != (files, dirs, nbytes):
            raise Refuse(
                EXIT_MOVE,
                "%s -> %s 对账不通过：inode 变了说明是拷贝不是 rename，计数不符说明搬漏了。"
                "停下来查（journal 在 %s）" % (move.src, move.dst, journal_path),
            )
        if move.src == "_runtime":
            emit("COUNT _runtime files=%d dirs=%d bytes=%d" % (files, dirs, nbytes))
            if checkpoints_before is not None:
                emit(
                    "COUNT _runtime/.checkpoints files=%d dirs=%d bytes=%d"
                    % checkpoints_before
                )
            new_gate = counts_of(os.path.join(root, STATE_DIR, "_runtime", ".checkpoints"))
            emit(
                "COUNT %s/_runtime/.checkpoints files=%d dirs=%d bytes=%d"
                % ((STATE_DIR,) + new_gate)
            )

    for rel in samples:
        sha_before, ino_before = sample_before[rel]
        new_rel = relocate(rel, plan["moves"])
        path = os.path.join(root, new_rel)
        st = os.lstat(path)
        sha_after = sha256_of(path)
        emit(
            "SAMPLE %s sha_same=%s ino_same=%s"
            % (
                rel,
                "yes" if sha_after == sha_before else "NO",
                "yes" if st.st_ino == ino_before else "NO",
            )
        )
        if sha_after != sha_before or st.st_ino != ino_before:
            raise Refuse(
                EXIT_MOVE,
                "抽样 %s 的内容或 inode 变了（搬的不是同一份字节）——停下来查" % rel,
            )

    os.fsync(journal_fd)
    os.close(journal_fd)
    emit(
        "JOURNAL %s mode=0600 moves=%d mkdirs=%d"
        % (journal_path, len([m for m in plan["moves"] if m.todo]), len(dirs_todo))
    )

    if args.delete_after:
        delete_leftovers(root, plan, dry_run=False)

    summary("apply", len(plan["moves"]), len(NEW_DIRS), todo, done)
    return 0


def rollback(root, args):
    journal_path = args.journal or os.path.join(root, JOURNAL_REL)
    if not os.path.isfile(journal_path):
        raise Refuse(
            EXIT_SHAPE,
            "找不到 journal（%s）：回退要靠它记的那份映射，没有它就只能靠人翻记录" % journal_path,
        )
    entries = parse_journal(journal_path)
    moves = [entry for entry in entries if entry[0] == "move"]
    mkdirs = [entry for entry in entries if entry[0] == "mkdir"]

    reversed_moves = []
    for _kind, src, dst in reversed(moves):
        if not os.path.lexists(os.path.join(root, dst)):
            raise Refuse(
                EXIT_SHAPE,
                "journal 说 %s 现在应该在 %s，但它不在 —— 先人工看一眼再回退" % (src, dst),
            )
        if os.path.lexists(os.path.join(root, src)):
            raise Refuse(EXIT_SHAPE, "%s 已经又出现在旧位置了：拒绝覆盖" % src)
        reversed_moves.append((src, dst))

    emit("== state-base-migration(N27) rollback ==")
    emit("mode=rollback root=%s dry_run=%d" % (root, 1 if args.dry_run else 0))
    emit("JOURNAL %s" % journal_path)
    for src, dst in reversed_moves:
        emit("REVERSE %s <- %s" % (src, dst))
    for _kind, rel, _dst in reversed(mkdirs):
        emit("RMDIR %s" % rel)
    todo = len(reversed_moves) + len(mkdirs)

    if args.dry_run:
        summary("rollback", len(moves), len(mkdirs), todo, 0)
        return 0

    done = 0
    for src, dst in reversed_moves:
        rename_or_refuse(os.path.join(root, dst), os.path.join(root, src))
        emit("RESTORED %s <- %s" % (src, dst))
        done += 1

    # journal 是证据不是状态：它活过回退，只是搬出 state/（那个目录要能被 rmdir）。
    os.rename(journal_path, os.path.join(root, ROLLED_BACK_JOURNAL))
    for _kind, rel, _dst in reversed(mkdirs):
        path = os.path.join(root, rel)
        if not os.path.lexists(path):
            continue
        try:
            os.rmdir(path)
        except OSError as exc:
            emit(
                "LEFTOVER %s（rmdir 拒绝 %s：非空 —— 这是有意的，本脚本没有任何递归删除）"
                % (rel, errno.errorcode.get(exc.errno, exc.errno))
            )
            continue
        emit("UNMKDIR %s" % rel)
        done += 1

    summary("rollback", len(moves), len(mkdirs), todo, done)
    return 0


def main(argv):
    parser = argparse.ArgumentParser(prog="migrate-state-base", add_help=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--mode", choices=("plan", "apply", "rollback"), default="plan")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--delete-after", action="store_true")
    parser.add_argument("--journal", default="")
    args = parser.parse_args(argv)
    try:
        if args.mode == "rollback":
            return rollback(args.root, args)
        return forward(args.root, args)
    except Refuse as exc:
        sys.stderr.write("REFUSE(%d): %s\n" % (exc.code, exc.message))
        sys.stderr.flush()
        return exc.code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
PY_ENGINE
}

# --- 只读的集群自检 ---------------------------------------------------------

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
        refuse 2 "KUBECONFIG=$KUBECONFIG 不是本项目那一份（要 $want）——绝不要把写操作打到别的集群上，见 docs/deploy-clusters.md §0"
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
    local replicas pods
    want_replicas=0
    replicas="$(kubectl -n "$NAMESPACE" get statefulset/e2b-worker -o jsonpath='{.spec.replicas}')"
    if [ "$replicas" != "$want_replicas" ]; then
        refuse 2 "statefulset/e2b-worker 有 $replicas 个副本（要 want_replicas=0）——先停写：kubectl -n $NAMESPACE scale statefulset/e2b-worker --replicas=0 && kubectl -n $NAMESPACE wait --for=delete pod -l app=e2b-worker --timeout=300s"
    fi
    pods="$(kubectl -n "$NAMESPACE" get pods -l app=e2b-worker -o name)"
    if [ -n "$pods" ]; then
        refuse 2 "app=e2b-worker 还有 pod 在跑（停写没完成）：$(printf '%s' "$pods" | tr '\n' ' ')"
    fi
    # 观到的副本数带进 Job 的 env（只有这一个数 Job 自己看不到）。它此刻必然
    # 是 0；万一没走到这里，render 出来的空值也会让 Job 自己拒绝。
    observed_replicas="$replicas"
}

# --- 三种执行路径 -----------------------------------------------------------

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
    local out
    out="\"--in-cluster\""
    if [ "$DRY_RUN" = "0" ]; then
        out="$out, \"--apply\""
    fi
    if [ "$ENGINE_MODE" = "rollback" ]; then
        out="$out, \"--rollback\""
    fi
    if [ "$DELETE_AFTER" = "1" ]; then
        out="$out, \"--delete-after\""
    fi
    printf '%s' "$out"
}

render_job() {
    local version replicas
    version="${VERSION:-$(cat "$REPO_ROOT/deploy/stack/.version")}"
    # 优先用本地（或显式传入）的观测值：这份 Job 拿到的 0 必须是有人真的看过
    # `statefulset/e2b-worker` 才写进去的。两者都没有时留空 —— Job 那边会拒绝。
    replicas="${MIGRATE_WORKER_REPLICAS:-${observed_replicas:-}}"
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
    warn "迁移由 Job 执行：$JOB（runAsUser 0，PVC sandbox-shared 以 RW 挂到 /shared）"
    kubectl -n "$NAMESPACE" create configmap "$CONFIGMAP" \
        --from-file="migrate-state-base.sh=$SCRIPT_PATH" --dry-run=client -o yaml |
        kubectl -n "$NAMESPACE" apply -f - >&2
    printf '%s\n' "$rendered" | kubectl -n "$NAMESPACE" apply -f - >&2
    if ! job_wait; then
        warn "job/$JOB 没有成功完成，日志与 pod 状态如下（对象保留，便于排查）"
        kubectl -n "$NAMESPACE" logs "job/$JOB" || true
        kubectl -n "$NAMESPACE" get pods -l job-name="$JOB" -o wide || true
        refuse 1 "Job 失败：看上面的日志。job/$JOB 与 configmap/$CONFIGMAP 都留着（查完手动 kubectl -n $NAMESPACE delete job/$JOB configmap/$CONFIGMAP）"
    fi
    kubectl -n "$NAMESPACE" logs "job/$JOB"
    if [ "$KEEP_JOB" = "1" ]; then
        warn "保留 job/$JOB 与 configmap/$CONFIGMAP（--keep-job）"
        return 0
    fi
    kubectl -n "$NAMESPACE" delete job "$JOB" --ignore-not-found
    kubectl -n "$NAMESPACE" delete configmap "$CONFIGMAP" --ignore-not-found
}

# --- 主流程 ----------------------------------------------------------------

if [ -n "$OFFLINE_ROOT" ]; then
    printf 'OFFLINE 彩排：root=%s（不连集群、不查 worker=0、不做集群身份自检）\n' "$OFFLINE_ROOT"
    run_engine "$OFFLINE_ROOT" $ENGINE_ARGS || exit $?
    exit 0
fi

if [ "$IN_CLUSTER" = "1" ]; then
    ROOT="${MIGRATE_ROOT:-/shared}"
    # Job 的清单把它固定成 /shared（PVC 以 RW 挂在那里）。这里只兜底"不是 /、
    # 而且真的存在"，因为运维也可能在节点上以这个模式对着 <export> 的挂载点跑。
    if [ "$ROOT" = "/" ] || [ ! -d "$ROOT" ]; then
        refuse 2 "MIGRATE_ROOT=$ROOT 不是一个可迁移的 export 根（不存在，或者就是 /）"
    fi
    if [ "${MIGRATE_WORKER_REPLICAS:-}" != "0" ]; then
        refuse 2 "MIGRATE_WORKER_REPLICAS=${MIGRATE_WORKER_REPLICAS:-<未设>} 不是 0：worker 停写没有被验证过。这份 Job 必须由 deploy/scripts/migrate-state-base.sh 渲染后 apply（它把观测到的副本数填进来），直接 apply 清单是不会写任何东西的"
    fi
    run_engine "$ROOT" $ENGINE_ARGS || exit $?
    exit 0
fi

require_python
check_kubeconfig
check_cluster_identity
check_worker_stopped

# 写操作之前先把只读的计划跑一遍：经控制面 pod（它那份 export 是只读挂载，
# 正好只够读计划），任何口径不一致都会在这里先响。
if [ "$DRY_RUN" = "1" ]; then
    cluster_engine $ENGINE_ARGS
    exit $?
fi
cluster_engine $ENGINE_ARGS --dry-run || exit $?
job_run
