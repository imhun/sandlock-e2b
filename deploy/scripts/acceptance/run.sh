#!/bin/sh
# The local aarch64 lane's kernel: qemu-system-aarch64 under TCG (this host is
# x86_64, so there is no hardware acceleration), one Ubuntu arm64 cloud image
# per checkout. It exists because the container lane's `--platform linux/arm64`
# is qemu-user, which answers ptrace and process_vm_writev with ENOSYS -- so the
# checkpoint/restore tests cannot run there at all. See docs/arm-cr-s0-evidence.md.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
qemu=/usr/local/opt/qemu/bin/qemu-system-aarch64
fw=/usr/local/opt/qemu/share/qemu
exec "$qemu" \
  -M virt -cpu max -smp 4 -m 4096 \
  -drive if=pflash,format=raw,readonly=on,file="$fw/edk2-aarch64-code.fd" \
  -drive if=pflash,format=raw,file="$here/efivars.fd" \
  -drive if=virtio,format=qcow2,file="$here/disk.qcow2" \
  -drive if=virtio,format=raw,readonly=on,file="$here/seed.iso" \
  -netdev user,id=net0,hostfwd=tcp:127.0.0.1:2222-:22 \
  -device virtio-net-pci,netdev=net0 \
  -display none \
  -serial "file:$here/console.log" \
  -monitor "unix:$here/mon.sock,server,nowait" \
  -pidfile "$here/qemu.pid"
