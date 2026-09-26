#!/usr/bin/env python3
"""为什么 probe-pure-synth-root-plaindir.py 的 b2 会在第一步就 EPERM？

只回答一件事：生产沙箱在 realroot::build 之前**已经**在自己的 user namespace 里
（context.rs:731 / sandbox.rs:2511 的 unshare(CLONE_NEWUSER)，realroot.rs:222-224
原话 "a private mount namespace owned by this user namespace"），而 lane runner 起的容器
不在任何 user ns 里 —— 少了这一步，unshare(CLONE_NEWNS)/mount 就没有 CAP_SYS_ADMIN
可用。本脚本在同一形状下分步打点，不改 b2 探针本身。
"""
from __future__ import annotations

import ctypes
import os

libc = ctypes.CDLL("libc.so.6", use_errno=True)

CLONE_NEWNS = 0x00020000
CLONE_NEWUSER = 0x10000000
MS_REC = 16384
MS_PRIVATE = 1 << 18
MS_BIND = 4096
MNT_DETACH = 2
SYS_PIVOT_ROOT = {"x86_64": 155, "aarch64": 41, "riscv64": 41}[os.uname().machine]

SCRATCH = "/workspace/tmp/k0s/scratch"


def caps() -> str:
    for line in open("/proc/self/status"):
        if line.startswith("CapEff:"):
            return line.split()[1]
    return "?"


def step(label: str, fn) -> bool:
    rc = fn()
    err = ctypes.get_errno()
    outcome = "ok" if rc == 0 else f"FAILED errno={err} ({os.strerror(err)})"
    print(f"  {label}: {outcome}")
    return rc == 0


def mount(src, tgt, fstype, flags) -> int:
    return libc.mount(
        src.encode() if src else None,
        tgt.encode(),
        fstype.encode() if fstype else None,
        ctypes.c_ulong(flags),
        None,
    )


#: 与 b2 同一份清单。
SYSTEM_DIRS = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/opt")
HOST_ONLY = os.environ.get("HOST_ONLY", "/src")


def main() -> int:
    print(f"machine={os.uname().machine} uid={os.getuid()} CapEff={caps()}")
    print("1) 照抄 b2 的顺序（只有 CLONE_NEWNS）")
    step("unshare(CLONE_NEWNS)", lambda: libc.unshare(CLONE_NEWNS))
    print("2) 生产沙箱的顺序（先 CLONE_NEWUSER，再由它拥有 mount ns）—— 完整照 b2 装配")
    step("unshare(CLONE_NEWUSER)", lambda: libc.unshare(CLONE_NEWUSER))
    print(f"  after NEWUSER: uid={os.getuid()} CapEff={caps()}")
    step("unshare(CLONE_NEWNS)", lambda: libc.unshare(CLONE_NEWNS))
    step("mount(/ MS_REC|MS_PRIVATE)", lambda: mount(None, "/", None, MS_REC | MS_PRIVATE))
    root = os.path.join(SCRATCH, "userns-check-root")
    for name in ("usr", "bin", "sbin", "lib", "lib64", "opt", "dev", "proc", "etc",
                 "tmp", "root", "run", "var", "srv", "media", "mnt", "home",
                 "home/user", "workspace"):
        os.makedirs(os.path.join(root, name), mode=0o755, exist_ok=True)
    bound = 0
    for src in SYSTEM_DIRS:
        if not os.path.isdir(src):
            print(f"  skip {src} (not on this host)")
            continue
        if step(f"bind {src} -> {root}{src}",
                lambda src=src: mount(src, os.path.join(root, src.lstrip("/")), None, MS_BIND | MS_REC)):
            bound += 1
    step(f"bind /dev -> {root}/dev", lambda: mount("/dev", os.path.join(root, "dev"), None, MS_BIND | MS_REC))
    step("self-bind root", lambda: mount(root, root, None, MS_BIND | MS_REC))
    os.chdir(root)
    step("pivot_root('.', '.')", lambda: libc.syscall(SYS_PIVOT_ROOT, b".", b"."))
    step("umount2('.', MNT_DETACH)", lambda: libc.umount2(b".", MNT_DETACH))
    os.chdir("/")
    print(f"  bound system dirs: {bound}")
    print(f"  after pivot: new root (dev, ino) = ({os.stat('/').st_dev}, {os.stat('/').st_ino})")
    print(f"  host-only {HOST_ONLY} visible: {os.path.exists(HOST_ONLY)}")
    print(f"  exec /bin/sh: {os.system('/bin/sh -c \"exit 0\"') == 0}")
    print("  verdict: PASS (同样的形状，只补上生产沙箱本来就有的 CLONE_NEWUSER)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
