#!/usr/bin/env bash
set -euo pipefail
# E6.4 NFS probe server: XFS (prjquota) export via kernel nfsd inside a
# privileged container. The export path is /srv/nfs; nfsd on 20499 and
# mountd on 20049 are pinned so clients do not depend on rpcbind state.
# Ports deliberately differ from 2049/20048: OrbStack hosts bind the
# standard NFS ports for macOS file sharing, which would otherwise steal
# the probe's mounts (ESTALE).
for i in $(seq 0 63); do [ -e /dev/loop$i ] || mknod /dev/loop$i b 7 $i 2>/dev/null || true; done
IMG=/var/lib/nfs-probe/xfs.img
export_dir="${NFS_EXPORT_DIR:-/srv/nfs}"
export_opts="${NFS_EXPORT_OPTS:-rw,sync,no_subtree_check,no_root_squash,insecure}"
mkdir -p /var/lib/nfs-probe "$export_dir" /proc/fs/nfsd
# Detach loops from previous runs (the data volume keeps the same XFS image,
# and the kernel keeps loop attachments after the old container is removed).
for loop in $(losetup -j "$IMG" 2>/dev/null | cut -d: -f1); do
    losetup -d "$loop" 2>/dev/null || true
done
if [ ! -f "$IMG" ]; then
  truncate -s "${XFS_IMG_SIZE:-4G}" "$IMG"
  mkfs.xfs -q "$IMG"
fi
losetup -f "$IMG"
loopdev=$(losetup -j "$IMG" | head -1 | cut -d: -f1)
mkdir -p /mnt/xfs
mount -o prjquota "$loopdev" /mnt/xfs
mkdir -p /mnt/xfs/export
mount --bind /mnt/xfs/export "$export_dir"
printf '%s %s(%s)\n' "$export_dir" "*" "$export_opts" > /etc/exports
rpcbind || true
/usr/sbin/rpc.statd --no-notify 2>/dev/null || true
/usr/sbin/rpc.mountd -p 20049 2>/dev/null || true
# 清掉同 VM 内核 nfsd 里可能残留的旧导出：容器移除后内核导出表仍会保留，
# 若 XFS 镜像被重建（fsid 变化），残留条目会让客户端挂载/操作返回 ESTALE。
# 先按路径 unexport，再重新导出当前配置。
printf '%s %s(ro)\n' "$export_dir" "*" > /etc/exports
exportfs -au || true
exportfs -r || true
printf '%s %s(%s)\n' "$export_dir" "*" "$export_opts" > /etc/exports
exportfs -r
echo "nfs probe server ready: export=$export_dir opts=$export_opts"
/usr/sbin/rpc.nfsd -p 20499 8 || /usr/sbin/rpc.nfsd -p 20499
exec tail -f /dev/null
