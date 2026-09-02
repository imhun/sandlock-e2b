#!/usr/bin/env bash
# 在探针 NFS 服务器容器内为每个用例重置 volume 目录并设置 XFS project
# 配额：<dir> <projid> 成对出现；hard limit = $1 MB。目录先清理再重建，
# 保证重复运行配额用量归零（XFS 镜像本身复用，fsid 稳定 → 无 ESTALE）。
set -euo pipefail

LIMIT_MB="$1"
shift
while [ $# -gt 0 ]; do
    dir="$1"
    projid="$2"
    shift 2
    rm -rf "/mnt/xfs/export/volumes/$dir"
    xfs_quota -x -c "project -C -p /mnt/xfs/export/volumes/$dir $projid" \
        /mnt/xfs >/dev/null 2>&1 || true
    mkdir -p "/mnt/xfs/export/volumes/$dir"
    xfs_quota -x -c "project -s -p /mnt/xfs/export/volumes/$dir $projid" /mnt/xfs >/dev/null
    xfs_quota -x -c "limit -p bhard=${LIMIT_MB}m $projid" /mnt/xfs
done
