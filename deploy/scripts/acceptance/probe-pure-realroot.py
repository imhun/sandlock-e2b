#!/usr/bin/env python3
"""pure 能不能走真根？把 pivot_root 的两种用法实测一遍。

顺序照抄 `realroot::build`：unshare → / 设 private → 绑进 rootfs → **递归自绑 root** →
`chdir(root)` + `pivot_root(".", ".")` + `umount2(".", MNT_DETACH)`（老根叠在新根上再摘掉，
所以不需要预先建 oldroot 目录 —— 第一版探针就是这里写错、报了 ENOENT）。

A：`chroot = "/"`（N15 那条 identity 翻译）+ 真根 —— 自绑 `/` 再 pivot 进去。
B：**合成一个 rootfs**（tmpfs + 绑 /usr /bin）+ 真根。

判据只看一件事：**宿主独有的路径还看得见吗**。A 若看得见，它就不是隔离，只是把同一个树
重新挂了一次；B 若看不见，那才叫真根 —— 但那已经不是 pure 了（pure 的 `/` 就是宿主树）。
"""
from __future__ import annotations

import ctypes
import os
import sys
import tempfile

libc = ctypes.CDLL("libc.so.6", use_errno=True)

CLONE_NEWNS = 0x00020000
MS_REC = 16384
MS_PRIVATE = 1 << 18
MS_BIND = 4096
MNT_DETACH = 2

#: pivot_root 的 syscall 号按架构不同（x86_64 = 155，aarch64/riscv64 = 41）——
#: 这正是主仓 B13 记的那条坑：写死 155 会让 aarch64 上问错内核。
SYS_PIVOT_ROOT = {"x86_64": 155, "aarch64": 41, "riscv64": 41}[os.uname().machine]

#: 宿主侧有、而一个"合成 rootfs"里不该有的东西（容器里仓库就挂在 /src）。
HOST_ONLY = os.environ.get("HOST_ONLY", "/src")


def fail(step: str, rc: int):
    err = ctypes.get_errno()
    print(f"  {step}: FAILED errno={err} ({os.strerror(err)})")
    return False


def ok(step: str, rc: int) -> bool:
    if rc != 0:
        return fail(step, rc)
    return True


def mount(src, tgt, fstype, flags) -> int:
    return libc.mount(
        src.encode() if src else None,
        tgt.encode(),
        fstype.encode() if fstype else None,
        ctypes.c_ulong(flags),
        None,
    )


def enter_ns() -> bool:
    """自己的 mount ns + / 设 private（realroot::build 第 1、2 步）。"""
    return ok("unshare(CLONE_NEWNS)", libc.unshare(CLONE_NEWNS)) and ok(
        "mount(/ private)", mount(None, "/", None, MS_REC | MS_PRIVATE)
    )


def self_bind_and_pivot(root: str) -> bool:
    """第 4、5 步：递归自绑 root，再 chdir + pivot_root('.','.') + detach。"""
    if not ok(f"self-bind {root}", mount(root, root, None, MS_BIND | MS_REC)):
        return False
    os.chdir(root)
    rc = libc.syscall(SYS_PIVOT_ROOT, b".", b".")
    if not ok("pivot_root('.', '.')", rc):
        return False
    if not ok("umount2('.', MNT_DETACH)", libc.umount2(b".", MNT_DETACH)):
        return False
    os.chdir("/")
    return True


def report(label: str):
    st = os.stat("/")
    seen = "看得见" if os.path.exists(HOST_ONLY) else "看不见"
    print(f"   {label}：宿主独有路径 {HOST_ONLY} -> {seen}")
    print(f"   新根 (dev, ino) = ({st.st_dev}, {st.st_ino})")


def part_a():
    print("A. chroot='/' + 真根（自绑 / 再 pivot 进去）")
    if not enter_ns() or not self_bind_and_pivot("/"):
        return 1
    report("pivot 成功")
    return 0


def part_a2():
    """A 的变体：把 `/` 绑到**另一个路径**再 pivot 进去。

    用来区分两种解释：EBUSY 是"自绑在同一个挂载点"造成的，还是"pivot 进宿主根的副本"
    本身就不被内核允许。
    """
    print("A2. chroot='/' + 真根，但绑到另一个路径再 pivot")
    if not enter_ns():
        return 1
    stage = tempfile.mkdtemp(prefix="rootcopy-", dir="/tmp")
    if not ok(f"bind / -> {stage}", mount("/", stage, None, MS_BIND | MS_REC)):
        return 1
    if not self_bind_and_pivot(stage):
        return 1
    report("pivot 成功")
    return 0


def part_b():
    print("B. 合成一个 rootfs（tmpfs + 绑系统目录）+ 真根")
    stage = tempfile.mkdtemp(prefix="synthroot-", dir="/tmp")
    if not enter_ns():
        return 1
    if not ok("mount(tmpfs, stage)", mount("tmpfs", stage, "tmpfs", 0)):
        return 1
    for d in ("usr", "bin", "sbin", "lib", "lib64", "etc", "proc", "dev", "tmp"):
        os.makedirs(os.path.join(stage, d), exist_ok=True)
    for d in ("usr", "bin"):
        src = os.path.join("/", d)
        if os.path.isdir(src) and not ok(f"bind {src}", mount(src, os.path.join(stage, d), None, MS_BIND | MS_REC)):
            return 1
    if not self_bind_and_pivot(stage):
        return 1
    report("pivot 成功")
    return 0


if __name__ == "__main__":
    which = (sys.argv[1] if len(sys.argv) > 1 else "a").lower()
    print(f"== part {which} ==")
    sys.exit({"a": part_a, "a2": part_a2, "b": part_b}[which]())
