#!/usr/bin/env bash
# E6.4 NFS 部署验证探针（共享存储形态 + XFS project quota 专项）。
#
# 在 Docker 主机上运行：启动一个容器内内核 NFS 服务器（XFS loop +
# prjquota 导出），两个 worker 客户端容器通过 NFS 挂载同一导出，实测：
#   A. projid 继承：client 在 volume/<id>/ 创建的文件 projid 正确继承
#   B. sync 挂载超限：ENOSPC 及时返回（服务器端 EDQUOT 经 NFS 传播）
#   C. async 挂载超限：延迟写 close()/fsync() 才报错（E2.6 关注点）
#   D. 多 worker 独立限额：各自子目录限额独立、projid 不冲突
#   E. root_squash / uid 映射：对 projid 继承与配额记账的影响
#   F. 共享路径语义 + 迁移保留文件：两个挂载点看到同一份文件且内容一致
#
# 用法：./deploy/scripts/nfs_quota_probe.sh [--limit-mb 16] [--keep]
#   --limit-mb  每个测试 projid 的硬限额（MB），默认 16
#   --keep      结束后保留探针容器（默认清理）
#
# 需要：宿主机 docker（--privileged 容器、host 网络）。实测记录与部署
# 注意事项见 docs/production-deployment-requirements.md §5。

set -euo pipefail

PROBE_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVER_IMAGE="e2b-nfs-probe:local"
CLIENT_IMAGE="e2b-nfs-client:local"
SERVER_NAME="e2b-nfs-probe-$$"
DATA_VOLUME="e2b-nfs-probe-data"
SQUASH_DATA_VOLUME="e2b-nfs-probe-data-squash"
LIMIT_MB="${NFS_QUOTA_LIMIT_MB:-16}"
KEEP=0

while [ $# -gt 0 ]; do
    case "$1" in
        --limit-mb) LIMIT_MB="$2"; shift ;;
        --keep) KEEP=1 ;;
        *) echo "未知参数: $1" >&2; exit 1 ;;
    esac
    shift
done

PASS=0
FAIL=0
RESULTS=()

note() { printf '\n==> %s\n' "$*"; }

pass() { RESULTS+=("PASS $1"); PASS=$((PASS + 1)); }
fail() {
    RESULTS+=("FAIL $1")
    FAIL=$((FAIL + 1))
    echo "    ^ 检查失败: $1" >&2
}

cleanup() {
    if [ "$KEEP" != "1" ]; then
        docker rm -f "$SERVER_NAME" >/dev/null 2>&1 || true
    else
        note "保留探针容器：$SERVER_NAME"
    fi
}
trap cleanup EXIT

command -v docker >/dev/null || { echo "需要 docker" >&2; exit 1; }

server_exec() { docker exec "$SERVER_NAME" bash -lc "$1"; }

server_setup() {
    docker cp "$PROBE_DIR/nfs-probe/server-setup.sh" "$SERVER_NAME:/setup.sh" >/dev/null
    server_exec "bash /setup.sh $*"
}

client_run() {
    # client_run <容器名> <mount_opts> <script> — 一个 worker 客户端（host
    # 网络）。统一负责挂载（ESTALE 竞态时重试一次）、执行用例脚本、卸载。
    local name="$1"
    local mount_opts="$2"
    local body="$3"
    docker run --rm --privileged --network host \
        --security-opt seccomp=unconfined --cap-add NET_ADMIN \
        --name "$name" "$CLIENT_IMAGE" bash -lc "
            set -e
            mkdir -p /mnt/nfs
            MOUNTED=0
            for _try in 1 2 3; do
                if timeout 20 mount -t nfs -o $mount_opts \
                    127.0.0.1:/srv/nfs /mnt/nfs 2>/dev/null; then
                    MOUNTED=1
                    break
                fi
                sleep 2
            done
            [ \"\$MOUNTED\" = 1 ] || { echo 'mount failed'; exit 1; }
            cd /mnt/nfs
            $body
            cd / && umount /mnt/nfs 2>/dev/null || true
        "
}

server_wait() {
    local deadline=$((SECONDS + 30))
    while [ "$SECONDS" -lt "$deadline" ]; do
        if docker logs "$SERVER_NAME" 2>&1 | grep -q "nfs probe server ready"; then
            break
        fi
        sleep 1
    done
    # nfsd 线程与导出表在 "ready" 之后才真正生效：轮询 exportfs 输出，
    # 再留 2 秒让 mountd/nfsd 稳定，避免客户端在窗口内拿到旧句柄。
    while [ "$SECONDS" -lt "$deadline" ]; do
        if docker exec "$SERVER_NAME" bash -lc 'exportfs -v 2>/dev/null | grep -q /srv/nfs' \
            >/dev/null 2>&1; then
            sleep 2
            return 0
        fi
        sleep 1
    done
    docker logs "$SERVER_NAME" 2>&1 | tail -20
    echo "NFS 探针服务器未就绪" >&2
    exit 1
}

server_start() {
    # server_start <export_opts>
    local opts="$1"
    docker rm -f "$SERVER_NAME" >/dev/null 2>&1 || true
    docker run -d --name "$SERVER_NAME" --privileged --network host \
        -v "$DATA_VOLUME:/var/lib/nfs-probe" \
        -e NFS_EXPORT_OPTS="$opts" "$SERVER_IMAGE" >/dev/null
    server_wait
}

reset_kernel_nfsd() {
    # 清除同 VM 内核里残留的 nfsd 线程（容器内核 nfsd 的已知问题），
    # 保证 2049 端口与导出表干净。必须确认线程归零：残留线程会用旧导出表
    # 应答新挂载（旧 XFS fsid → ESTALE）。
    for _try in 1 2 3 4 5; do
        out="$(docker run --rm --privileged --network host --entrypoint bash \
            "$CLIENT_IMAGE" -c '
                mkdir -p /proc/fs/nfsd
                mount -t nfsd nfsd /proc/fs/nfsd 2>/dev/null || true
                echo 0 > /proc/fs/nfsd/threads 2>/dev/null || true
                # 清空 nfsd 的 fh/export sunrpc 缓存：残留缓存会把旧 XFS
                # （不同 fsid）的文件句柄交给新导出 → ESTALE。
                echo 1 > /proc/net/rpc/nfsd.fh/flush 2>/dev/null || true
                echo 1 > /proc/net/rpc/nfsd.export/flush 2>/dev/null || true
                cat /proc/fs/nfsd/threads 2>/dev/null || echo N/A
            ' 2>/dev/null || true)"
        if [ "$(printf '%s' "$out" | tail -1 | tr -d '[:space:]')" = "0" ]; then
            echo "nfsd threads after reset: 0"
            sleep 1
            return 0
        fi
        sleep 1
    done
    echo "无法清零内核 nfsd 线程（残留: $out）；请手动排查后重试" >&2
    exit 1
}

# --- 构建镜像 ---------------------------------------------------------------
note "构建 NFS 探针镜像"
docker build -q -f "$PROBE_DIR/nfs-probe/Dockerfile.nfs-server" \
    -t "$SERVER_IMAGE" "$PROBE_DIR/nfs-probe" >/dev/null
docker build -q -f "$PROBE_DIR/nfs-probe/Dockerfile.nfs-client" \
    -t "$CLIENT_IMAGE" "$PROBE_DIR/nfs-probe" >/dev/null

MOUNT_OPTS="vers=3,proto=tcp,port=20499,mountport=20049,noresvport"
MOUNT_OPTS_SYNC="$MOUNT_OPTS,sync"
MOUNT_OPTS_ASYNC="$MOUNT_OPTS"

# --- 场景 A/B/C/D/F（no_root_squash）---------------------------------------
reset_kernel_nfsd
note "启动 NFS 服务器（no_root_squash）"
server_start "rw,sync,no_subtree_check,no_root_squash,insecure"

server_setup "$LIMIT_MB" case_a 1001 case_b 1002 case_c 1003 \
    w1 1004 w2 1005 mig 1006

note "A. projid 继承（client 创建文件）"
client_run "e2b-nfs-case-a" "$MOUNT_OPTS_SYNC" \
    "echo data > volumes/case_a/file.txt"
projid_a="$(server_exec 'lsattr -p /mnt/xfs/export/volumes/case_a/file.txt | awk "{print \$1}"')"
if [ "$projid_a" = "1001" ]; then pass "A projid 继承"; else fail "A projid 继承 (got $projid_a)"; fi

note "B. sync 挂载超限 → ENOSPC 及时返回"
client_out="$(client_run "e2b-nfs-case-b" "$MOUNT_OPTS_SYNC" "
python3 - <<'PY'
import os
f = os.open('volumes/case_b/big.bin', os.O_CREAT|os.O_WRONLY, 0o644)
block = b'x' * (1024*1024)
total = 0
err = None
for _ in range($((LIMIT_MB + 8))):
    try:
        os.write(f, block); total += 1
    except OSError as e:
        err = e.errno; break
try: os.close(f)
except OSError as e: err = err or e.errno
print(f'{total} {err}')
PY
")"
set -- $client_out
if [ "$1" = "$LIMIT_MB" ] && [ "$2" = "28" ]; then
    pass "B sync ENOSPC (errno 28 at ${LIMIT_MB}MiB)"
else
    fail "B sync ENOSPC (got: $client_out)"
fi

note "C. async 挂载超限 → close()/fsync() 延迟报错"
client_out="$(client_run "e2b-nfs-case-c" "$MOUNT_OPTS_ASYNC" "
python3 - <<'PY'
import os
f = os.open('volumes/case_c/async.bin', os.O_CREAT|os.O_WRONLY, 0o644)
block = b'x' * (1024*1024)
for _ in range($((LIMIT_MB + 8))):
    os.write(f, block)
try:
    os.fsync(f); print('fsync ok')
except OSError as e:
    print(f'fsync errno {e.errno}')
PY
")"
if [[ "$client_out" == *"fsync errno 28"* ]]; then
    pass "C async fsync ENOSPC"
else
    fail "C async fsync ENOSPC (got: $client_out)"
fi

note "D. 多 worker 独立限额"
client_run "e2b-nfs-w1" "$MOUNT_OPTS_SYNC" "
python3 - <<'PY'
import os
f = os.open('volumes/w1/w.bin', os.O_CREAT|os.O_WRONLY, 0o644)
for _ in range($((LIMIT_MB + 8))):
    try: os.write(f, b'x' * (1024*1024))
    except OSError as e: print(e.errno); break
PY
" &
PID_W1=$!
client_run "e2b-nfs-w2" "$MOUNT_OPTS_SYNC" "
python3 - <<'PY'
import os
f = os.open('volumes/w2/w.bin', os.O_CREAT|os.O_WRONLY, 0o644)
for _ in range($((LIMIT_MB + 8))):
    try: os.write(f, b'x' * (1024*1024))
    except OSError as e: print(e.errno); break
PY
" &
PID_W2=$!
wait "$PID_W1" "$PID_W2"
usage="$(server_exec "xfs_quota -x -c 'report -p /mnt/xfs/export/volumes/w1 /mnt/xfs/export/volumes/w2' /mnt/xfs | awk '\$1 ~ /^#100[45]$/ {print \$1, \$2}'")"
if echo "$usage" | grep -q "1004 $((LIMIT_MB * 1024))" && echo "$usage" | grep -q "1005 $((LIMIT_MB * 1024))"; then
    pass "D 多 worker 独立限额"
else
    fail "D 多 worker 独立限额 (report: $usage)"
fi

note "F. 共享路径语义 + 迁移保留文件"
client_out="$(client_run "e2b-nfs-mig" "$MOUNT_OPTS_SYNC" "
mkdir -p /mnt/nfsB
mount -t nfs -o $MOUNT_OPTS_SYNC 127.0.0.1:/srv/nfs /mnt/nfsB
mkdir -p volumes/mig/data/sub
echo 'keep-me' > volumes/mig/data/notes.txt
head -c 65536 /dev/urandom > volumes/mig/data/sub/blob.bin
SUM_A=\$(md5sum < volumes/mig/data/sub/blob.bin)
SUM_B=\$(md5sum < /mnt/nfsB/volumes/mig/data/sub/blob.bin)
test \"\$SUM_A\" = \"\$SUM_B\" && test -f volumes/mig/data/notes.txt && echo SAME
cd / && umount /mnt/nfsB 2>/dev/null || true
")"
if [[ "$client_out" == *"SAME"* ]]; then
    pass "F 共享路径/迁移保留"
else
    fail "F 共享路径/迁移保留 (got: $client_out)"
fi

# --- 场景 E（root_squash）---------------------------------------------------
# 独立数据卷 + 全新服务器实例（root_squash）。避免同卷重启：XFS 在同一
# img 上反复 detach/reattach 会在 OrbStack VM 中出现 superblock 损坏。
reset_kernel_nfsd
note "启动 root_squash 服务器（独立数据卷）"
docker rm -f "$SERVER_NAME" >/dev/null 2>&1 || true
docker run -d --name "$SERVER_NAME" --privileged --network host \
    -v "$SQUASH_DATA_VOLUME:/var/lib/nfs-probe" \
    -e NFS_EXPORT_OPTS="rw,sync,no_subtree_check,root_squash,insecure" \
    "$SERVER_IMAGE" >/dev/null
server_wait
server_setup "$LIMIT_MB" squash_world 2001 squash_ro 2002
server_exec "chmod 1777 /mnt/xfs/export/volumes/squash_world; chmod 755 /mnt/xfs/export/volumes/squash_ro"

client_out="$(client_run "e2b-nfs-squash" "$MOUNT_OPTS_SYNC" "
ls volumes/squash_world >/dev/null
python3 - <<'PY'
import os
f = os.open('volumes/squash_world/root.bin', os.O_CREAT|os.O_WRONLY, 0o644)
total = 0
for _ in range($((LIMIT_MB + 8))):
    try: os.write(f, b'x' * (1024*1024)); total += 1
    except OSError as e: break
try: os.close(f)
except OSError: pass
try:
    os.open('volumes/squash_ro/denied.bin', os.O_CREAT|os.O_WRONLY, 0o644)
    ro = 'allowed'
except OSError as e:
    ro = str(e.errno)
print(f'{total} {ro}')
PY
")"
set -- $client_out
owner="$(server_exec 'stat -c %U /mnt/xfs/export/volumes/squash_world/root.bin')"
projid_e="$(server_exec 'lsattr -p /mnt/xfs/export/volumes/squash_world/root.bin | awk "{print \$1}"')"
if [ "$1" = "$LIMIT_MB" ] && [ "$2" = "13" ] && [ "$owner" = "nobody" ] && [ "$projid_e" = "2001" ]; then
    pass "E root_squash 影响"
else
    fail "E root_squash 影响 (wrote=$1 root-only-err=$2 owner=$owner projid=$projid_e)"
fi

# --- 汇总 -------------------------------------------------------------------
note "结果汇总"
for line in "${RESULTS[@]}"; do echo "  $line"; done
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" = "0" ]
