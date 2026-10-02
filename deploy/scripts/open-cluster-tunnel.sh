#!/usr/bin/env bash
# 打开到**本项目唯一目标集群**（自建 k0s）的本地通道，然后断言连对了集群。
#
# 为什么要有这个脚本：本机 kubectl 的默认 context 指向**另一套阿里云 ACK 集群** ——
# 不加 KUBECONFIG 时 `kubectl get nodes` 会安静地成功，只是连错了地方。所以这里
# ① 通道只往一个地方开；② 开完自检节点数/架构/发行版，不符就非零退出。
# 环境事实与坑的完整记录见 docs/deploy-clusters.md。
#
# 用法：
#   deploy/scripts/open-cluster-tunnel.sh           # 建通道 + 自检
#   deploy/scripts/open-cluster-tunnel.sh --check   # 只自检（通道已经在时）
set -euo pipefail

. "$(cd "$(dirname "$0")" && pwd)/lib/helpers.sh"
#: 自检 = `deploy/scripts/lib/cluster-guard.sh` 的 `require_target_cluster`（写侧脚本用的同一道
#: 闸门；期望值只在那一个文件里）。这里只负责判定，建通道仍是本脚本的活。
. "$SCRIPT_DIR/lib/cluster-guard.sh"

REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
CONTROL_PLANE_IP="${K0S_CONTROL_PLANE_IP:-172.18.80.94}"
LOCAL_API_PORT="${K0S_LOCAL_API_PORT:-16443}"
KUBECONFIG_PATH="${K0S_KUBECONFIG:-$REPO_ROOT/tmp/k0s/kubeconfig}"
SOCK="${K0S_BASTION_SOCK:-/tmp/k0s-bastion-root.sock}"
CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

export K0S_BASTION_SOCK="$SOCK"

if [ "$CHECK_ONLY" != "1" ]; then
    say "① 跳板机可复用连接（$BASTION_USER@$BASTION_HOST）"
    # 关键：建立这条连接必须用 -i "$SSH_KEY"。裸 `ssh -L` 在没有 ControlMaster 时只会
    # 报 `Permission denied (publickey)` —— 上一个版本（tmp/k0s/open-tunnels.sh）就是这么坏的。
    # 这里自带 expect（不依赖 gitignored 的 tmp/），口令经环境传，不进 argv。
    if ssh -o ControlPath="$SOCK" -o BatchMode=yes "$BASTION_USER@$BASTION_HOST" true 2>/dev/null; then
        echo "   已存在，复用"
    else
        require_expect
        expect <<'EXP'
set timeout [expr {[info exists env(TASK_TIMEOUT)] ? $env(TASK_TIMEOUT) : 60}]
set ssh_key      $env(SSH_KEY)
set bastion_user $env(BASTION_USER)
set bastion_host $env(BASTION_HOST)
set passphrase   $env(SSH_PASSPHRASE)
set sock         $env(K0S_BASTION_SOCK)
log_user 0
spawn ssh -M -S $sock -o ControlPersist=1800 -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null -o ConnectTimeout=15 -o ServerAliveInterval=30 \
    -o ExitOnForwardFailure=yes -i $ssh_key $bastion_user@$bastion_host true
expect {
    -re "passphrase for key" { send -- "$passphrase\r"; exp_continue }
    -re "(P|p)assword:"      { send -- "$passphrase\r"; exp_continue }
    -re "Permission denied"  { puts stderr "AUTH FAILED"; exit 2 }
    timeout                  { puts stderr "TIMEOUT"; exit 3 }
    eof
}
catch wait result
exit [lindex $result 3]
EXP
        echo "   已建立 $SOCK"
    fi

    say "② 控制面隧道 $LOCAL_API_PORT -> $CONTROL_PLANE_IP:6443"
    if lsof -nP -iTCP:"$LOCAL_API_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
        echo "   端口已占用，复用"
    else
        ssh -o ControlPath="$SOCK" -o ExitOnForwardFailure=yes \
            -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
            -f -N -L "$LOCAL_API_PORT:$CONTROL_PLANE_IP:6443" \
            "$BASTION_USER@$BASTION_HOST"
        echo "   已建立"
    fi

    if [ ! -s "$KUBECONFIG_PATH" ]; then
        say "③ kubeconfig 不在（$KUBECONFIG_PATH；tmp/ 是 gitignored）—— 从控制面节点取一份"
        fetch="$(printf '%s' 'k0s kubeconfig admin' | base64)"
        mkdir -p "$(dirname "$KUBECONFIG_PATH")"
        TARGET_HOST="$CONTROL_PLANE_IP" expect "$SCRIPT_DIR/lib/run-target.exp" "$fetch" root \
            | sed -E "s#^[[:space:]]*server: .*#    server: https://127.0.0.1:${LOCAL_API_PORT}#" \
            > "$KUBECONFIG_PATH"
        echo "   已写入 $KUBECONFIG_PATH（server 改指 127.0.0.1:$LOCAL_API_PORT）"
        echo "   ⚠ 这份文件含集群凭据：不要入库、不要贴出去"
    fi
fi

export KUBECONFIG="$KUBECONFIG_PATH"

say "④ 自检：连到的是不是本项目的集群（require_target_cluster）"
require_target_cluster "$KUBECONFIG_PATH"

echo "   sandlock namespace: $(kubectl -n sandlock get pods --no-headers 2>/dev/null | wc -l | tr -d ' ') 个 pod"

cat <<EOF

通道就绪。后续命令显式带上：
  export KUBECONFIG="$KUBECONFIG_PATH"
EOF
