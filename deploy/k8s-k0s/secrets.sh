#!/usr/bin/env bash
#
# O3 Task 1（Step 0 零窗口加固）：把 k0s 上的 `e2b-secrets` 交给脚本，不再手工
# `kubectl -n sandlock create secret generic ...`（docs/k8s-deployment.md §2 第 2 步
# 那份手工命令的 k0s 版）。
#
# 它管四个键 —— 正好是清单里 `secretKeyRef` 读的名字：
#
#   E2B_API_KEYS            外部 API key（可放多个，逗号分隔；见 control_plane/config.py）
#   E2B_INTERNAL_API_KEY    worker / control-plane / autoscaler 之间的内部 key
#   E2B_REDIS_PASSWORD      redis `--requirepass` + CP 拼出的 redis URL
#   E2B_SECRET_MASTER_KEY   `_secrets/**` 与 redis `e2b:secret:*` 的落盘加密主 key
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
#   KUBECONFIG=... deploy/k8s-k0s/secrets.sh --rotate E2B_API_KEYS,E2B_INTERNAL_API_KEY
set -euo pipefail

NAMESPACE="${NAMESPACE:-sandlock}"
SECRET_NAME="${SECRET_NAME:-e2b-secrets}"

#: 本脚本负责创建的键，顺序 = 新 Secret 里的书写顺序。
KEYS=(E2B_API_KEYS E2B_INTERNAL_API_KEY E2B_REDIS_PASSWORD E2B_SECRET_MASTER_KEY)

#: 主 key 单独对待：换它是**不可逆**的两窗操作，本脚本拒绝就地换（见 die 那句）。
MASTER_KEY=E2B_SECRET_MASTER_KEY

#: 生成的新值长度：32 hex 字节（64 字符），与 deploy/scripts/upgrade.sh 的
#: `E2B_SECRET_MASTER_KEY` 同一口径。
RAND_BYTES=32

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

command -v kubectl >/dev/null 2>&1 || die "缺少 kubectl"

have_secret=0
if secret_exists; then
    have_secret=1
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
            rotated+=("$key")
        else
            say "补缺 $key（新生成）"
        fi
        set_out "$key" "$(openssl rand -hex "$RAND_BYTES")"
    done

    if [ "${#rotated[@]}" -gt 0 ] && is_rotate E2B_REDIS_PASSWORD; then
        say ""
        say "⚠ E2B_REDIS_PASSWORD 轮换的窗口：redis 带着新口令重启、到 control-plane /"
        say "  autoscaler 滚动完成拿到新口令之间，共享后端（配额/节点视图/限流/单飞）"
        say "  不可用，建箱与路由失败 —— **10–30 s 中断**。2026-09-26 用户裁定：**接受**"
        say "  这段中断，不做 ACL 双用户热轮换（docs/superpowers/plans/2026-09-26-decisions.md 第 5 条）。"
        say "  沙箱本身不经过 redis，不受影响；redis 是 appendonly yes ⇒ 数据不丢。"
        say "  步骤：① 排维护窗口 ② 本脚本 --rotate E2B_REDIS_PASSWORD"
        say "        ③ kubectl -n $NAMESPACE rollout restart deploy/redis"
        say "        ④ kubectl -n $NAMESPACE rollout restart deploy/control-plane deploy/autoscaler"
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
