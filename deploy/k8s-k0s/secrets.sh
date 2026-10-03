#!/usr/bin/env bash
#
# O3 Task 1（Step 0 零窗口加固）：把 k0s 上的 `e2b-secrets` 交给脚本，不再手工
# `kubectl -n sandlock create secret generic ...`（docs/k8s-deployment.md §2 第 2 步
# 那份手工命令的 k0s 版）。
#
# 它管五个键，加上轮换窗口那个列表键（只在点名 `--rotate-internal-key` /
# `--finalize-internal-key-rotation` 时写）—— 正好是清单里 `secretKeyRef` 读的六个名字：
#
#   E2B_API_KEYS            外部 API key（可放多个，逗号分隔；见 control_plane/config.py）
#   E2B_INTERNAL_API_KEY    worker / control-plane 之间的内部 key
#   E2B_INTERNAL_API_KEYS   internal key 的双窗列表（窗口之外为空/不存在）
#   E2B_REDIS_PASSWORD      redis `--requirepass` + CP 拼出的 redis URL
#   E2B_SECRET_MASTER_KEY   `_secrets/**` 与 redis `e2b:secret:*` 的落盘加密主 key
#   E2B_C3_AGENT_TOKEN      C3（Task 3）CP→agent 的凭据：只给 control-plane 与 agent
#                           pod，绝不进 worker 清单/镜像（硬规则 5：worker↔agent 不存在）
#
# 为什么最后一个键是这条 task 的全部理由：没有它 `SecretRegistry` 退回"内存 +
# 明文落盘"（只打一条启动告警，然后照常起），而 `<workspace_base>/_secrets/**`
# 在**共享 NAS 卷**上、worker pod 把整卷 RW 挂进来（deploy/k8s/worker.yaml），
# 于是任何拿到 pod root 的人都能读走每个租户的 secret。清单里那两个
# `secretKeyRef` 是 `optional: true`：先 apply 清单再跑本脚本不会让 CP 起不来
# （临时仍是降级态 + 告警），键补上后
# `kubectl -n sandlock rollout restart deploy/control-plane` 才真正生效。
#
# 幂等：**已有的键默认原样保留**，只补缺失的键；要换值必须显式点名
# `--rotate <KEY>`。落盘走 k8s 侧唯一可重放的写法：
#   kubectl create secret generic ... --dry-run=client -o yaml | kubectl apply -f -
# （Secret 里除这四个键之外的键——例如轮换窗口用的 E2B_SECRET_MASTER_KEYS ——
# 一律原样带过去，不会被 apply 抹掉。）
#
# 不打印任何明文：只有 `sha256(前16)` 指纹与长度。**不开 shell trace** —— trace 会把
# 每个展开的变量打出来。
#
# 用法：
#   KUBECONFIG=... deploy/k8s-k0s/secrets.sh                          # 只补缺（幂等）
#   KUBECONFIG=... deploy/k8s-k0s/secrets.sh --fingerprint            # 只打印，不改动
#   KUBECONFIG=... deploy/k8s-k0s/secrets.sh --rotate E2B_REDIS_PASSWORD
#
# **集群身份闸门**：碰任何 kubectl 之前先跑 `deploy/scripts/lib/cluster-guard.sh` 的
# `require_target_cluster`（KUBECONFIG 必须显式设置且文件存在、server 含 `+k0s`、
# 节点 2 × arm64 × `+k0s`，不符即 exit 2 并点名实际值）。**所有模式都要过**，包括
# 只读的 `--fingerprint` —— 闸门判的是"连对集群没有"，不是"会不会写"：不带
# KUBECONFIG 时 kubectl 会安静地用 ~/.kube/config 的 current-context，那是另一套
# 阿里云 ACK 集群（docs/deploy-clusters.md §1/§7.34.1）。
#
# 双窗轮换（O3 Task 3）。语义照 deploy/scripts/upgrade.sh:122-168：旧 key 留在
# 列表里继续可用，新 key 进单值槽（internal）或追加进列表（api），等消费者都滚到
# 新 key 之后再由 finalize 摘掉旧 key —— 中间不断服（唯一的中断点是 worker 滚动，
# 见 docs/k8s-deployment.md §4.5）：
#   KUBECONFIG=... deploy/k8s-k0s/secrets.sh --rotate-internal-key
#   KUBECONFIG=... deploy/k8s-k0s/secrets.sh --finalize-internal-key-rotation sha256:<前16>
#   KUBECONFIG=... deploy/k8s-k0s/secrets.sh --rotate-api-keys
#   KUBECONFIG=... deploy/k8s-k0s/secrets.sh --finalize-api-key-rotation sha256:<前16>
# finalize 的地址也可以是 key 本身（upgrade.sh 的形状）；用指纹是推荐做法 ——
# 操作者本来只看得到指纹（值从不离开脚本，本脚本也从不回显地址）。
#
# `--rotate <KEY>` 仍是**单槽换值**（旧值立刻失效，要窗口没有）：对
# E2B_API_KEYS / E2B_INTERNAL_API_KEY 它会打一条指路告警。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"

NAMESPACE="${NAMESPACE:-sandlock}"
SECRET_NAME="${SECRET_NAME:-e2b-secrets}"

#: 本脚本负责创建的键，顺序 = 新 Secret 里的书写顺序。
KEYS=(E2B_API_KEYS E2B_INTERNAL_API_KEY E2B_REDIS_PASSWORD E2B_SECRET_MASTER_KEY E2B_C3_AGENT_TOKEN)

#: 主 key 单独对待：换它是**不可逆**的两窗操作，本脚本拒绝就地换（见 die 那句）。
MASTER_KEY=E2B_SECRET_MASTER_KEY

#: 生成的新值长度：32 hex 字节（64 字符），与 deploy/scripts/upgrade.sh 的
#: `E2B_SECRET_MASTER_KEY` 同一口径。
RAND_BYTES=32

#: 轮换窗口用的两个键。它们**不在 KEYS 里**：没有点名轮换时，本脚本绝不生成、
#: 也不改写它们（空列表 = 没有窗口，正是常态）；只有点名的 subcommand 才写。
INTERNAL_KEY=E2B_INTERNAL_API_KEY
INTERNAL_KEYS_LIST=E2B_INTERNAL_API_KEYS
API_KEYS_LIST=E2B_API_KEYS

say() { printf '%s\n' "$*" >&2; }
die() { printf 'secrets.sh: %s\n' "$*" >&2; exit 1; }

usage() {
    sed -n "2,/^set -euo pipefail\$/p" "$0" | sed '$d' | sed 's/^# \{0,1\}//' >&2
}

# 指纹：值只从 printf 进管道到 sha256，绝不落到 stdout / 日志 / argv。
fingerprint() {
    if command -v sha256sum >/dev/null 2>&1; then
        printf '%s' "$1" | sha256sum | cut -c1-16
    else
        printf '%s' "$1" | shasum -a 256 | cut -c1-16
    fi
}

# 指纹的书写形状（与 --fingerprint 的表一致）。消息里只出现它，不出现值。
fp_label() { printf 'sha256:%s' "$(fingerprint "$1")"; }

# ---------------------------------------------------------------------------
# 逗号列表（双窗轮换用）
# ---------------------------------------------------------------------------
# 取列表里的第 n 个成员；越界返回非零。用于"逐成员打印指纹"。
list_at() {  # 1 = 逗号列表, 2 = 下标
    local -a parts
    IFS=',' read -r -a parts <<<"$1"
    [ "$2" -lt "${#parts[@]}" ] || return 1
    printf '%s' "${parts[$2]}"
}

# 列表里有没有这个 key（精确相等，不做子串匹配）。
list_has() {  # 1 = 逗号列表, 2 = key
    local key
    local -a parts
    IFS=',' read -r -a parts <<<"$1"
    for key in "${parts[@]}"; do
        [ "$key" = "$2" ] && return 0
    done
    return 1
}

# 保序去重的合并：入参可以给多个列表（或单个 key），空元素丢弃。
# upgrade.sh 的 rotate 就是"当前列表 + 旧主 key + 新 key"这一次合并。
join_keys() {
    local raw key result=""
    local -a parts
    for raw in "$@"; do
        [ -n "$raw" ] || continue
        IFS=',' read -r -a parts <<<"$raw"
        for key in "${parts[@]}"; do
            [ -n "$key" ] || continue
            case ",$result," in
                *",$key,"*) continue ;;
            esac
            result="${result:+$result,}$key"
        done
    done
    printf '%s' "$result"
}

# 从列表里去掉一个 key（去掉全部同值项；key 本身不进消息）。
drop_key() {  # 1 = 逗号列表, 2 = 要去掉的 key
    local key result=""
    local -a parts
    IFS=',' read -r -a parts <<<"$1"
    for key in "${parts[@]}"; do
        [ -n "$key" ] || continue
        [ "$key" = "$2" ] && continue
        result="${result:+$result,}$key"
    done
    printf '%s' "$result"
}

# finalize 的地址解析：地址可以是列表里的 key 本身（upgrade.sh 的形状），
# 也可以是它打印过的 `sha256:<前16>`（推荐 —— 操作者只看得到指纹）。
# 成功时打印那个 key 的值（进调用方的变量，不进输出）；失败返回非零，
# 调用方只拿地址的**指纹**报错，绝不回显地址。
resolve_key_address() {  # 1 = 逗号列表, 2 = 地址
    local key cand="$2"
    local -a parts
    if list_has "$1" "$2"; then
        printf '%s' "$2"
        return 0
    fi
    case "$cand" in
        sha256:*) cand="${cand#sha256:}" ;;
    esac
    case "$cand" in
        *[!0-9a-f]*) return 1 ;;
    esac
    [ "${#cand}" -eq 16 ] || return 1
    IFS=',' read -r -a parts <<<"$1"
    for key in "${parts[@]}"; do
        [ -n "$key" ] || continue
        if [ "$(fingerprint "$key")" = "$cand" ]; then
            printf '%s' "$key"
            return 0
        fi
    done
    return 1
}

# 逐成员打印指纹（值是操作者 finalize 时的地址，长度顺手带上）。
say_member_fingerprints() {  # 1 = 键名, 2 = 逗号列表
    local i=0 member
    while member="$(list_at "$2" "$i")"; do
        [ -n "$member" ] || { i=$((i + 1)); continue; }
        say "    $1 成员 $(fp_label "$member") len:${#member}"
        i=$((i + 1))
    done
}

# ---------------------------------------------------------------------------
# 读现有的 Secret
# ---------------------------------------------------------------------------
# 等价于手工的 `kubectl -n sandlock get secret e2b-secrets -o json`：先把已有的键读出来，
# 没点名 `--rotate` 的键原样保留，只补缺。
secret_exists() {
    kubectl -n "$NAMESPACE" get secret "$SECRET_NAME" >/dev/null 2>&1
}

live_keys() {
    kubectl -n "$NAMESPACE" get secret "$SECRET_NAME" \
        -o go-template='{{range $k, $v := .data}}{{$k}}{{"\n"}}{{end}}'
}

# `.data` 里键的个数。与 live_keys 的行数交叉校验：apply 覆盖的是**整份** data，
# 读漏一个键就等于把那个键删掉，所以读不完整时必须停。
live_key_count() {
    kubectl -n "$NAMESPACE" get secret "$SECRET_NAME" \
        -o go-template='{{len .data}}'
}

# base64 解码在 kubectl 自己的 go-template 里做：值不进 argv，也不落任何本地文件。
live_value() {
    kubectl -n "$NAMESPACE" get secret "$SECRET_NAME" \
        -o "go-template={{index .data \"$1\" | base64decode}}"
}

# ---------------------------------------------------------------------------
# 输出集合（键 + 值，按下标对齐）
# ---------------------------------------------------------------------------
out_keys=()
out_values=()

# 1 = key ⇒ 打印它的下标（0 表示找到）；找不到返回非零。
out_index() {
    local i=0
    while [ "$i" -lt "${#out_keys[@]}" ]; do
        if [ "${out_keys[$i]}" = "$1" ]; then
            printf '%s' "$i"
            return 0
        fi
        i=$((i + 1))
    done
    return 1
}

set_out() {  # 1 = key, 2 = value（已有则覆盖，没有则追加）
    local idx
    if idx="$(out_index "$1")"; then
        out_values[$idx]="$2"
    else
        out_keys+=("$1")
        out_values+=("$2")
    fi
}

# 取当前输出集合里某个键的值；不存在时打印空串（调用方自己判空）。
out_value() {  # 1 = key
    local idx
    if idx="$(out_index "$1")"; then
        printf '%s' "${out_values[$idx]}"
    fi
}

load_live() {
    local key keys_raw count printed=0
    # 命令替换（不是进程替换）：kubectl 失败 ⇒ 赋值失败 ⇒ `set -e` 立刻退出。
    keys_raw="$(live_keys)"
    count="$(live_key_count)"
    if [ -n "$keys_raw" ]; then
        while IFS= read -r key; do
            [ -n "$key" ] || continue
            printed=$((printed + 1))
            set_out "$key" "$(live_value "$key")"
        done <<<"$keys_raw"
    fi
    [ "$printed" = "$count" ] ||
        die "读 $NAMESPACE/$SECRET_NAME 对不上：Secret 里 $count 个键，只读出 $printed 个 —— 拒绝在不完整的视图上写回（apply 覆盖整份 data，漏读等于删键）"
}

# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------
mode=apply
rotate_raw=""
rotate_internal=0
finalize_internal=""
rotate_api=0
finalize_api=""

while [ "$#" -gt 0 ]; do
    case "$1" in
        --rotate)
            [ "$#" -ge 2 ] || die "--rotate 需要参数（可轮换：${KEYS[*]}）"
            rotate_raw="${rotate_raw:+$rotate_raw,}$2"
            shift 2
            ;;
        --rotate=*)
            rotate_raw="${rotate_raw:+$rotate_raw,}${1#--rotate=}"
            shift
            ;;
        --rotate-internal-key)
            rotate_internal=1
            shift
            ;;
        --finalize-internal-key-rotation)
            [ "$#" -ge 2 ] && [ -n "$2" ] ||
                die "--finalize-internal-key-rotation 需要参数（旧 key 或它的 sha256 前 16 位；不能是空串）"
            finalize_internal="$2"
            shift 2
            ;;
        --rotate-api-keys)
            rotate_api=1
            shift
            ;;
        --finalize-api-key-rotation)
            [ "$#" -ge 2 ] && [ -n "$2" ] ||
                die "--finalize-api-key-rotation 需要参数（旧 key 或它的 sha256 前 16 位；不能是空串）"
            finalize_api="$2"
            shift 2
            ;;
        --fingerprint)
            mode=fingerprint
            shift
            ;;
        -h | --help)
            usage
            exit 0
            ;;
        *)
            say "未知参数：$1"
            usage
            exit 2
            ;;
    esac
done

rotate_keys=()
if [ -n "$rotate_raw" ]; then
    IFS=',' read -r -a rotate_keys <<<"$rotate_raw"
fi

is_rotate() {
    local rk
    [ "${#rotate_keys[@]}" -gt 0 ] || return 1
    for rk in "${rotate_keys[@]}"; do
        [ "$rk" = "$1" ] && return 0
    done
    return 1
}

if [ "${#rotate_keys[@]}" -gt 0 ]; then
    for rk in "${rotate_keys[@]}"; do
        known=0
        for key in "${KEYS[@]}"; do
            [ "$key" = "$rk" ] && known=1
        done
        [ "$known" = 1 ] || die "--rotate 不认识 $rk（可轮换：${KEYS[*]}）"
        [ "$rk" = "$MASTER_KEY" ] &&
            die "--rotate $MASTER_KEY 被拒：换主 key 是**不可逆**的两窗操作 —— 旧 key 必须先进 E2B_SECRET_MASTER_KEYS 并留在那里，等所有副本都用新 key 重新加密完才能删；就地换值会让既有 _secrets/** 与 redis 的 e2b:secret:* 密文永远解不开。走 deploy/k8s-k0s/rotate-secret-master.sh（rotate → 滚 CP → finalize）。"
    done
fi

# 双窗轮换的互斥：一次只做一件事，混着来会让人读不懂这次 apply 到底换了什么。
if [ "$mode" = "fingerprint" ] &&
    { [ "$rotate_internal" = 1 ] || [ -n "$finalize_internal" ] ||
        [ "$rotate_api" = 1 ] || [ -n "$finalize_api" ]; }; then
    die "--fingerprint 只读，不能与 --rotate-internal-key / --rotate-api-keys / --finalize-* 一起用"
fi
if is_rotate "$INTERNAL_KEY" && [ "$rotate_internal" = 1 ]; then
    die "$INTERNAL_KEY 不能同时用 --rotate 与 --rotate-internal-key"
fi
if is_rotate "$API_KEYS_LIST" && [ "$rotate_api" = 1 ]; then
    die "$API_KEYS_LIST 不能同时用 --rotate 与 --rotate-api-keys"
fi

command -v kubectl >/dev/null 2>&1 || die "缺少 kubectl"

#: 认集群（写操作与 `--fingerprint` 一起过）：`exit 2` 由闸门自己发出，并点名实际
#: 看到的 context / server 版本 / 每台节点的架构与版本。
. "$REPO_ROOT/deploy/scripts/lib/cluster-guard.sh"
require_target_cluster

have_secret=0
if secret_exists; then
    have_secret=1
fi

# 双窗轮换的前置：没有 Secret 就没有"当前 key"可轮换，也没有列表可摘。
if [ "$rotate_internal" = 1 ] || [ -n "$finalize_internal" ] ||
    [ "$rotate_api" = 1 ] || [ -n "$finalize_api" ]; then
    [ "$have_secret" = 1 ] ||
        die "--rotate-internal-key / --rotate-api-keys / --finalize-* 都需要先有 $NAMESPACE/$SECRET_NAME：先跑一次不带这些参数的本脚本把它建出来"
fi

if [ "$have_secret" = 0 ]; then
    [ "$mode" = "fingerprint" ] &&
        die "$NAMESPACE/$SECRET_NAME 不存在（先跑一次不带 --fingerprint 的本脚本创建它）"
    say "$NAMESPACE/$SECRET_NAME 不存在 ⇒ 全新建（四个键都生成）"
else
    load_live
fi

# ---------------------------------------------------------------------------
# 生成 + 落盘（`--fingerprint` 时整段跳过，只打印）
# ---------------------------------------------------------------------------
if [ "$mode" != "fingerprint" ]; then
    command -v openssl >/dev/null 2>&1 || die "缺少 openssl（生成新值用）"

    # ---- 双窗轮换（O3 Task 3）：先于补缺/单槽循环跑 ----
    # 顺序有讲究：补缺循环会把缺失的 E2B_INTERNAL_API_KEY 当成"要新生成"，
    # 那样 rotate 的"当前主 key"就变成了刚生成、还从未生效过的一把 —— 所以
    # 轮换的前置判断必须在它之前。算法本身照 deploy/scripts/upgrade.sh:122-168：
    # rotate = 旧 key 留在列表里 + 新 key 进单值槽；finalize = 从列表摘掉旧 key。
    if [ "$rotate_internal" = 1 ]; then
        current_primary="$(out_value "$INTERNAL_KEY")"
        [ -n "$current_primary" ] ||
            die "无法确定当前 internal key（Secret 缺 $INTERNAL_KEY）"
        new_primary="$(openssl rand -hex "$RAND_BYTES")"
        new_list="$(join_keys "$(out_value "$INTERNAL_KEYS_LIST")" "$current_primary" "$new_primary")"
        set_out "$INTERNAL_KEY" "$new_primary"
        set_out "$INTERNAL_KEYS_LIST" "$new_list"
        say "已轮换 internal key：新主 key 写入 $INTERNAL_KEY；旧 key 留在 $INTERNAL_KEYS_LIST（窗口内新旧都认，这一步不重启任何 pod）"
        say "  新主 key $(fp_label "$new_primary")"
        say "  窗口列表成员（finalize 用这里的指纹）："
        say_member_fingerprints "$INTERNAL_KEYS_LIST" "$new_list"
        say "  滚动顺序（唯一不断服的顺序；第 2 步会杀光全部 running 沙箱 ⇒ 放低峰/窗口）："
        say "    1) kubectl -n $NAMESPACE rollout restart deploy/control-plane && kubectl -n $NAMESPACE rollout status deploy/control-plane"
        say "    2) kubectl -n $NAMESPACE rollout restart statefulset/e2b-worker"
        say "       （这一步会杀光全部 running 沙箱 —— 树与卷数据保留，但放低峰/窗口做）"
        say "    3) 两处都滚完后：deploy/k8s-k0s/secrets.sh --finalize-internal-key-rotation $(fp_label "$current_primary")"
        say "       （autoscaler 自 2026-09-30 起是控制面里的一个任务，随第 1 步一起滚，没有第三次 rollout）"
    fi

    if [ -n "$finalize_internal" ]; then
        primary="$(out_value "$INTERNAL_KEY")"
        existing_list="$(out_value "$INTERNAL_KEYS_LIST")"
        [ -n "$existing_list" ] ||
            die "$INTERNAL_KEYS_LIST 为空：旧 key 已不在生效列表"
        removed="$(resolve_key_address "$existing_list" "$finalize_internal")" ||
            die "$INTERNAL_KEYS_LIST 里没有 $(fp_label "$finalize_internal")：地址既不是列表里的 key，也不是它打印过的 sha256 前 16 位"
        [ "$removed" != "$primary" ] ||
            die "不能移除当前主 key（$(fp_label "$removed")）：先 --rotate-internal-key 生成新主 key"
        set_out "$INTERNAL_KEYS_LIST" "$(drop_key "$existing_list" "$removed")"
        say "已从 $INTERNAL_KEYS_LIST 移除 $(fp_label "$removed")：该 key 立即失效"
    fi

    if [ "$rotate_api" = 1 ]; then
        current_api="$(out_value "$API_KEYS_LIST")"
        [ -n "$current_api" ] || die "无法确定当前 API key（Secret 缺 $API_KEYS_LIST）"
        new_api="$(openssl rand -hex "$RAND_BYTES")"
        new_api_list="$(join_keys "$current_api" "$new_api")"
        set_out "$API_KEYS_LIST" "$new_api_list"
        say "已轮换 API key：新 key 追加进 $API_KEYS_LIST（旧 key 仍有效，这一步不重启任何 pod）"
        say "  窗口列表成员（finalize 用这里的指纹）："
        say_member_fingerprints "$API_KEYS_LIST" "$new_api_list"
        say "  滚动顺序（只有 control-plane 读外部 API key）："
        say "    1) kubectl -n $NAMESPACE rollout restart deploy/control-plane && kubectl -n $NAMESPACE rollout status deploy/control-plane"
        say "    2) 客户端逐个切到新 key（$(fp_label "$new_api")）并验证"
        say "    3) 都切完：deploy/k8s-k0s/secrets.sh --finalize-api-key-rotation sha256:<要退役那把的指纹>"
    fi

    if [ -n "$finalize_api" ]; then
        current_api="$(out_value "$API_KEYS_LIST")"
        [ -n "$current_api" ] || die "$API_KEYS_LIST 为空：没有可移除的 key"
        removed="$(resolve_key_address "$current_api" "$finalize_api")" ||
            die "$API_KEYS_LIST 里没有 $(fp_label "$finalize_api")：地址既不是列表里的 key，也不是它打印过的 sha256 前 16 位"
        remaining="$(drop_key "$current_api" "$removed")"
        [ -n "$remaining" ] ||
            die "不能移除最后一个 API key：所有客户端会被锁在门外"
        set_out "$API_KEYS_LIST" "$remaining"
        say "已从 $API_KEYS_LIST 移除 $(fp_label "$removed")：该 key 立即失效"
    fi

    rotated=()
    for key in "${KEYS[@]}"; do
        cur=""
        if idx="$(out_index "$key")"; then
            cur="${out_values[$idx]}"
        fi

        if [ -n "$cur" ] && ! is_rotate "$key"; then
            say "保留 $key（已有值；要换值请显式 --rotate $key）"
            continue
        fi
        if [ -n "$cur" ]; then
            say "轮换 $key（生成新值；本次 apply 之后旧值只在消费者滚动前还认）"
            case "$key" in
                "$API_KEYS_LIST")
                    say "⚠ --rotate $API_KEYS_LIST 是单槽换值（旧 key 立刻失效）；双窗轮换请用 --rotate-api-keys"
                    ;;
                "$INTERNAL_KEY")
                    say "⚠ --rotate $INTERNAL_KEY 是单槽换值（旧 key 立刻失效）；双窗轮换请用 --rotate-internal-key"
                    ;;
            esac
            rotated+=("$key")
        else
            say "补缺 $key（新生成）"
        fi
        set_out "$key" "$(openssl rand -hex "$RAND_BYTES")"
    done

    if [ "${#rotated[@]}" -gt 0 ] && is_rotate E2B_REDIS_PASSWORD; then
        say ""
        say "⚠ E2B_REDIS_PASSWORD 轮换的窗口：redis 带着新口令重启、到 control-plane"
        say "  滚动完成拿到新口令之间，共享后端（配额/节点视图/限流/单飞）"
        say "  不可用，建箱与路由失败 —— **10–30 s 中断**。2026-09-26 用户裁定：**接受**"
        say "  这段中断，不做 ACL 双用户热轮换（docs/superpowers/plans/2026-09-26-decisions.md 第 5 条）。"
        say "  沙箱本身不经过 redis，不受影响；redis 是 appendonly yes ⇒ 数据不丢。"
        say "  步骤：① 排维护窗口 ② 本脚本 --rotate E2B_REDIS_PASSWORD"
        say "        ③ kubectl -n $NAMESPACE rollout restart deploy/redis"
        say "        ④ kubectl -n $NAMESPACE rollout restart deploy/control-plane"
        say "           （读 redis 的只有 control-plane —— 它同时托管 autoscaler，这一步把扩缩容循环一并重起）"
        say "        ⑤ redis-cli -u \"redis://:<新口令>@redis:6379\" ping 应为 PONG"
        say ""
    fi

    # 可重放的写法：client dry-run 渲染 → apply。
    from_literals=()
    for idx in "${!out_keys[@]}"; do
        from_literals+=("--from-literal=${out_keys[$idx]}=${out_values[$idx]}")
    done
    kubectl -n "$NAMESPACE" create secret generic "$SECRET_NAME" \
        "${from_literals[@]}" \
        --dry-run=client -o yaml | kubectl apply -f - >&2

    # kubectl create --dry-run=client 的输出不一定带 metadata.namespace（版本相关），
    # 那种情况下 kubectl apply 会安静地写进 kubeconfig 的当前 namespace。所以回读一次。
    secret_exists ||
        die "apply 之后 $NAMESPACE/$SECRET_NAME 仍不存在 —— 你的 kubectl 可能把 Secret 写进了别的 namespace（老版本 create --dry-run=client 不带 metadata.namespace）；检查 kubectl 版本后重跑"

    if [ "${#rotated[@]}" -gt 0 ]; then
        say "已轮换 ${rotated[*]}；把新值交给消费者还需要滚动："
        say "  kubectl -n $NAMESPACE rollout restart deploy/control-plane"
        say "  （轮换 E2B_API_KEYS / E2B_INTERNAL_API_KEY 且没有双窗列表时，旧 key 立刻失效："
        say "    未切换的客户端 401、worker 侧对 CP 的内部调用 401 —— 放在维护窗口做）"
    fi
fi

printf '%s\n' "# $NAMESPACE/$SECRET_NAME 指纹（sha256 前 16 位；值不出机器；未轮换的键指纹逐字不变）"
for idx in "${!out_keys[@]}"; do
    printf '%s sha256:%s len:%s\n' \
        "${out_keys[$idx]}" \
        "$(fingerprint "${out_values[$idx]}")" \
        "${#out_values[$idx]}"
done
