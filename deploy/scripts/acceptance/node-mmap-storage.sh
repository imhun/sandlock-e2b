#!/bin/sh
# Same kernel, four storages: does a mapped store past EOF extend the file?
#
#   1. XFS on the node's local nvme   (what a local-workspace route would use)
#   2. tmpfs                          (memory-backed, for contrast)
#   3. ext4 on a loop file            (the N30 image route)
#   4. NFS 4.0 -- the NAS we actually run on
set -u

PROBE=/tmp/mmap-probe.py
LOOP_IMG=/var/tmp/mmap-loop.img
LOOP_MNT=/mnt/mmap-loop
NAS_DIR=/mnt/nas-lock/.mmap-probe

python3 "$PROBE" /var/tmp/mmap-xfs "xfs (local nvme)"
python3 "$PROBE" /dev/shm/mmap-tmpfs "tmpfs"

truncate -s 256M "$LOOP_IMG"
mkfs.ext4 -q -F "$LOOP_IMG"
mkdir -p "$LOOP_MNT"
LOOP_DEV=$(losetup -f --show "$LOOP_IMG")
mount -t ext4 "$LOOP_DEV" "$LOOP_MNT"
python3 "$PROBE" "$LOOP_MNT/probe" "ext4 on loop (N30 route)"
umount "$LOOP_MNT"
losetup -d "$LOOP_DEV"
rm -f "$LOOP_IMG"

python3 "$PROBE" "$NAS_DIR" "nfs4.0 (the NAS)"
rmdir "$NAS_DIR" 2>/dev/null || true
