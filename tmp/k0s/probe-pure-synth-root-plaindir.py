#!/usr/bin/env python3
"""合成根在生产 cap 形状下能不能 pivot？以及 /dev、/proc 该怎么装。

顺序照抄 `realroot::build`（`third_party/sandlock/crates/sandlock-core/src/realroot.rs:219-300`），
**前面还差生产沙箱自己的那一步** `unshare(CLONE_NEWUSER)`：realroot 的 mount ns 是
"owned by our own user namespace"（`realroot.rs:222-224`），而 real_root 在 fork 里的位置
就是"*after* the user namespace above -- that namespace owns the mount namespace created
here, and CAP_SYS_ADMIN inside it is what authorises every mount and the pivot_root"
（`context.rs:884-896`）。少了它，在无 SYS_ADMIN 的生产档里第一步 `unshare(CLONE_NEWNS)`
就 EPERM —— 简报第一版探针正是漏了这一步，原始输出留在
`tmp/k0s/pure-synth-root-prodshape-verbatim.log` / `pure-synth-root-tmpfs-verbatim.log`。

补上 userns 之后：unshare(CLONE_NEWNS) → `/` 设 MS_REC|MS_PRIVATE → 逐个 bind 到 `<root>/<virtual>` →
**递归自绑 root** → `chdir(root)` + `pivot_root(".", ".")` + `umount2(".", MNT_DETACH)` + `chdir("/")`。

与 `tmp/k0s/probe-pure-realroot.py` 的三点区别（这是本轮的方法论修正点）：
1. **不用 tmpfs**：`deploy/seccomp/sandlock-worker.json:837-852` 只允许 fstype==0 的 mount，
   tmpfs 在生产档下必然 EPERM（`docs/chroot-workspace-exec.md` §9.7 第 3 条已实测）；
   合成根 = 普通目录 + bind。
2. **跑在生产 cap 形状**（无 SYS_ADMIN + worker 那五个 cap + worker seccomp 档），不是特权容器。
3. scratch 一律放 `/workspace/tmp/k0s/scratch/`（= 宿主的项目 `tmp/`，AGENTS.md）。
"""
from __future__ import annotations

import ctypes
import os
import shutil
import stat
import sys

libc = ctypes.CDLL("libc.so.6", use_errno=True)

CLONE_NEWUSER = 0x10000000
CLONE_NEWNS = 0x00020000
MS_REC = 16384
MS_PRIVATE = 1 << 18
MS_BIND = 4096
MNT_DETACH = 2

#: pivot_root 的 syscall 号按架构不同（x86_64 = 155，aarch64/riscv64 = 41）。
SYS_PIVOT_ROOT = {"x86_64": 155, "aarch64": 41, "riscv64": 41}[os.uname().machine]

#: 宿主侧有、而合成根里不该有的东西。**必须在 pivot 之前就定成绝对路径**：传进来的是相对
#: 路径时，pivot 前后的 cwd 不同（`/workspace` → `/`），同一个字符串量的是两棵不同的树 ——
#: `hidden` 会因此可能变绿，而与被断言的那个路径无关。
HOST_ONLY = os.path.abspath(os.environ.get("HOST_ONLY", "/src"))

#: 与 E2B 侧 `_SYNTHETIC_ROOTFS_SYSTEM_DIRS` 同一份清单。
SYSTEM_DIRS = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/opt")

SCRATCH = "/workspace/tmp/k0s/scratch"

PART = "b2"


def note(msg: str) -> None:
    print(f"[part {PART}] {msg}")


def ok(step: str, rc: int) -> bool:
    if rc != 0:
        err = ctypes.get_errno()
        note(f"{step}: FAILED errno={err} ({os.strerror(err)})")
        return False
    note(f"{step}: ok")
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
    """生产顺序：先自己的 user ns（它才拥有下面这个 mount ns、并在里面握着 CAP_SYS_ADMIN），
    再 mount ns，再把 `/` 设 private。"""
    return (
        ok("unshare(CLONE_NEWUSER)", libc.unshare(CLONE_NEWUSER))
        and ok("unshare(CLONE_NEWNS)", libc.unshare(CLONE_NEWNS))
        and ok("mount(/ private)", mount(None, "/", None, MS_REC | MS_PRIVATE))
    )


def self_bind_and_pivot(root: str) -> bool:
    if not ok(f"self-bind {root}", mount(root, root, None, MS_BIND | MS_REC)):
        return False
    os.chdir(root)
    if not ok("pivot_root('.', '.')", libc.syscall(SYS_PIVOT_ROOT, b".", b".")):
        return False
    if not ok("umount2('.', MNT_DETACH)", libc.umount2(b".", MNT_DETACH)):
        return False
    os.chdir("/")
    return True


def fresh(name: str) -> str:
    """项目内的 scratch 目录（容器里 = 宿主的 tmp/），每次先清干净。"""
    path = os.path.join(SCRATCH, name)
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path, exist_ok=True)
    return path


def part_b2() -> int:
    """普通目录（非 tmpfs）作根 + bind 系统目录 + 递归自绑 + pivot。"""
    root = fresh("synth-root")
    for name in ("usr", "bin", "sbin", "lib", "lib64", "opt", "dev", "proc",
                 "etc", "tmp", "root", "run", "var", "srv", "media", "mnt",
                 "home", "home/user", "workspace"):
        os.makedirs(os.path.join(root, name), mode=0o755, exist_ok=True)
    #: 隔离断言只有在"这个路径 pivot 之前真的存在"时才有信息量。默认的 `/src` 在本 lane
    #: （仓库挂在 `/workspace`）并不存在，那样 `host-only ...: hidden` 是一条**假绿**：
    #: pivot 之前就看不见，pivot 之后当然也看不见。先记事实，再决定能不能判。
    host_only_pre_pivot = os.path.exists(HOST_ONLY)
    note(f"host-only {HOST_ONLY} exists pre-pivot: {host_only_pre_pivot}")
    if not host_only_pre_pivot:
        note("verdict: VACUOUS (HOST_ONLY does not exist before the pivot, so `hidden` below "
             "would be free -- pass -e HOST_ONLY=<a path that exists in this container>)")
        return 2
    if not enter_ns():
        return 1
    bound = 0
    for src in SYSTEM_DIRS:
        if not os.path.isdir(src):
            note(f"skip {src} (not on this host)")
            continue
        if not ok(f"bind {src} -> {root}{src}",
                  mount(src, os.path.join(root, src.lstrip("/")), None, MS_BIND | MS_REC)):
            return 1
        bound += 1
    if not ok(f"bind /dev -> {root}/dev",
              mount("/dev", os.path.join(root, "dev"), None, MS_BIND | MS_REC)):
        return 1
    if not self_bind_and_pivot(root):
        return 1
    note(f"bound system dirs: {bound}")
    note(f"host-only {HOST_ONLY}: " + ("hidden" if not os.path.exists(HOST_ONLY) else "VISIBLE"))
    note("exec /bin/sh: " + ("ok" if os.system("/bin/sh -c 'exit 0'") == 0 else "FAILED"))
    if os.path.exists(HOST_ONLY):
        return 1
    note("verdict: PASS (plain directory + bind + pivot_root works in the pinned shape)")
    return 0


def part_tmpfs() -> int:
    """对照臂：同一个位置换成 tmpfs，生产档下必须是 EPERM。"""
    root = fresh("synth-root-tmpfs")
    if not enter_ns():
        return 1
    if mount("tmpfs", root, "tmpfs", 0) == 0:
        note("mount(tmpfs): ok")
        note("verdict: FAIL-NEGATIVE (tmpfs was allowed; the pinned profile is not in place)")
        return 1
    err = ctypes.get_errno()
    note(f"mount(tmpfs): FAILED errno={err} ({os.strerror(err)})")
    if err not in (1, 13):
        note("verdict: FAIL-NEGATIVE (expected EPERM/EACCES, got something else)")
        return 1
    note("verdict: PASS-NEGATIVE (tmpfs unavailable; plain directory + bind is the route)")
    return 0


def part_symlinks() -> int:
    """哪些顶层项是软链、bind 之后在新根里是什么（宿主侧枚举，不需要 ns）。"""
    for src in SYSTEM_DIRS:
        if not os.path.lexists(src):
            note(f"{src}: absent")
        elif os.path.islink(src):
            note(f"{src}: symlink -> {os.readlink(src)} (resolved {os.path.realpath(src)})")
        else:
            note(f"{src}: real directory")
    note("verdict: PASS (this table is the input for the bind list)")
    return 0


def part_proc() -> int:
    """骨架里**故意不建** /proc：能不能起、里面是什么。"""
    root = fresh("synth-root-proc")
    for name in ("usr", "bin", "lib", "dev", "etc", "tmp", "home", "home/user", "workspace"):
        os.makedirs(os.path.join(root, name), mode=0o755, exist_ok=True)
    if not enter_ns():
        return 1
    for src in ("/usr", "/bin", "/lib", "/dev"):
        if os.path.isdir(src):
            mount(src, os.path.join(root, src.lstrip("/")), None, MS_BIND | MS_REC)
    if not self_bind_and_pivot(root):
        return 1
    note("stat /proc: " + ("exists" if os.path.exists("/proc") else "ENOENT"))
    listed = sorted(os.listdir("/proc"))[:5] if os.path.isdir("/proc") else "ENOTDIR/ENOENT"
    note(f"listdir /proc: {listed}")
    return 0


def write_dev_null() -> str:
    """纯 Python 探 `/dev/null`：不经 exec，所以不会被"解释器不在骨架里"污染。

    返回的是**带 errno 文本的结论字符串**（与 `ok()` 的 `FAILED errno=…` 同一风格），
    因为这一行会被 Task 3 当事实读；只答 True/False 会丢掉"为什么"。
    """
    try:
        with open("/dev/null", "w") as fh:
            fh.write("x")
    except OSError as exc:
        return f"False ({type(exc).__name__} errno={exc.errno} {exc.strerror})"
    return "True"


def read_one_urandom() -> str:
    """同上：`/dev/urandom` 能不能读出一个字节，与动态解释器无关。"""
    try:
        with open("/dev/urandom", "rb") as fh:
            data = fh.read(1)
    except OSError as exc:
        return f"False ({type(exc).__name__} errno={exc.errno} {exc.strerror})"
    if len(data) != 1:
        return f"False (short read: {len(data)} byte)"
    return "True"


def dev_node_identity(node: str) -> str:
    """`node` 在新根里的**设备身份**，不是"能不能写"。

    `minimal` 臂会先在骨架里 `open(target, "w")` 造一个普通同名文件，那条 bind 若悄悄失败，
    `open('/dev/null','w')` 照样成功 —— 它会替一个普通文件作证。所以身份必须单独量一次：
    真 `/dev/null` 是字符设备（`S_ISCHR`），普通文件会在这里现形。
    """
    try:
        st = os.stat(node)
    except OSError as exc:
        return f"unavailable ({type(exc).__name__} errno={exc.errno} {exc.strerror})"
    if stat.S_ISCHR(st.st_mode):
        kind = "char-device"
    elif stat.S_ISBLK(st.st_mode):
        kind = "block-device"
    else:
        kind = "NOT-a-device"
    return f"{kind} rdev={st.st_rdev} mode={oct(st.st_mode)}"


def part_dev() -> int:
    """三条 /dev 候选各自装配，跑同一组命令，产出可比较的一行。"""
    variant = os.environ.get("DEV_VARIANT", "host-tree")
    root = fresh(f"synth-root-dev-{variant}")
    for name in ("usr", "bin", "lib", "etc", "tmp", "home", "home/user", "workspace"):
        os.makedirs(os.path.join(root, name), mode=0o755, exist_ok=True)
    dev = os.path.join(root, "dev")
    os.makedirs(dev, mode=0o755, exist_ok=True)
    if variant == "minimal":
        for node in ("null", "zero", "urandom", "tty", "ptmx", "pts"):
            target = os.path.join(dev, node)
            if node == "pts":
                os.makedirs(target, exist_ok=True)
            else:
                open(target, "w").close()
    if not enter_ns():
        return 1
    for src in ("/usr", "/bin", "/lib"):
        if os.path.isdir(src):
            mount(src, os.path.join(root, src.lstrip("/")), None, MS_BIND | MS_REC)
    if variant == "host-tree":
        if not ok("bind /dev", mount("/dev", dev, None, MS_BIND | MS_REC)):
            return 1
    elif variant == "minimal":
        for node in ("null", "zero", "urandom", "tty", "ptmx", "pts"):
            src = os.path.join("/dev", node)
            if not os.path.exists(src):
                continue
            # 这条 bind 以前是静默的：失败时骨架里那个自造的普通同名文件留在原地，
            # 后面的 `open(...,'w')` 会替它答 True —— 假绿的入口就在这里。
            if not ok(f"bind {src} -> {dev}/{node}",
                      mount(src, os.path.join(dev, node), None, MS_BIND | MS_REC)):
                return 1
    if not self_bind_and_pivot(root):
        return 1
    present = sorted(os.listdir("/dev")) if os.path.isdir("/dev") else []
    note(f"variant={variant}")
    note(f"ls /dev: {present[:12]}")
    note(f"/dev/shm exists: {os.path.exists('/dev/shm')}")
    note(f"/dev/fd exists: {os.path.exists('/dev/fd')}")
    #: 这两行以前是 `os.system('echo x > /dev/null')` / `head -c1 /dev/urandom`，量到的却是
    #: exec 自己：`/bin/sh -> dash` 是动态链接，PT_INTERP 是绝对路径
    #: `/lib64/ld-linux-x86-64.so.2`，而本 part 的骨架（上面的目录清单）没有 `lib64`，
    #: 于是两个 `False` 与 `/dev` 毫无关系，看起来却像"/dev/null 不可用"。
    #: 现在：先记骨架里到底有没有 /lib64（正向证据），再用不经 exec 的纯 Python 探。
    note(f"skeleton has /lib64: {os.path.exists('/lib64')}")
    note(f"/dev/null identity: {dev_node_identity('/dev/null')}")
    note(f"/dev/urandom identity: {dev_node_identity('/dev/urandom')}")
    note(f"open('/dev/null','w') writes: {write_dev_null()}")
    note(f"open('/dev/urandom','rb').read(1): {read_one_urandom()}")
    return 0


PARTS = {
    "b2": part_b2,
    "tmpfs": part_tmpfs,
    "symlinks": part_symlinks,
    "proc": part_proc,
    "dev": part_dev,
}

if __name__ == "__main__":
    PART = (sys.argv[1] if len(sys.argv) > 1 else "b2").lower()
    sys.exit(PARTS[PART]())
