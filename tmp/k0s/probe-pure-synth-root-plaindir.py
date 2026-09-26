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
    """骨架里到底要不要那个空 /proc：两个方向各量一次，不是推断。

    `PROC_VARIANT=absent`（默认，= 简报 Step 1 的形状）故意不建 /proc，量的是"没有这个
    目录时内核答什么"；`PROC_VARIANT=emptydir` 建一个空 /proc（0755，与真骨架同模式），
    量的是"有这个目录时内核答什么"。`/proc` 里的**内容**由中介合成（fork 的
    `procfs.rs::handle_proc_open`），与这个目录存不存在是两件事 —— 本 part 量的是
    "列目录本身"看到的东西，所以两臂都要有数字，"要不要建"才不是推断出来的。
    """
    variant = os.environ.get("PROC_VARIANT", "absent")
    if variant not in ("absent", "emptydir"):
        note(f"unknown PROC_VARIANT={variant!r} (expected absent|emptydir)")
        return 2
    root = fresh("synth-root-proc")
    for name in ("usr", "bin", "lib", "dev", "etc", "tmp", "home", "home/user", "workspace"):
        os.makedirs(os.path.join(root, name), mode=0o755, exist_ok=True)
    if variant == "emptydir":
        os.makedirs(os.path.join(root, "proc"), mode=0o755, exist_ok=True)
    if not enter_ns():
        return 1
    for src in ("/usr", "/bin", "/lib", "/dev"):
        if os.path.isdir(src):
            mount(src, os.path.join(root, src.lstrip("/")), None, MS_BIND | MS_REC)
    if not self_bind_and_pivot(root):
        return 1
    note(f"variant={variant}")
    note("stat /proc: " + ("exists" if os.path.exists("/proc") else "ENOENT"))
    listed = sorted(os.listdir("/proc"))[:5] if os.path.isdir("/proc") else "ENOTDIR/ENOENT"
    note(f"listdir /proc: {listed}")
    if variant == "absent":
        if os.path.exists("/proc"):
            note("verdict: VACUOUS (this arm is 'no /proc', yet /proc exists after the pivot)")
            return 2
        note("verdict: ENOENT -- without the directory the kernel has nothing to show")
        return 0
    if not os.path.isdir("/proc"):
        note("verdict: FAILED (the empty directory did not survive the pivot)")
        return 1
    if listed:
        note(f"verdict: FAILED (the directory is not empty: {listed})")
        return 1
    note("verdict: exists and lists 0 entries -- the directory, not a mount, answers here")
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


#: 两条候选要比的节点（`minimal` 的六节点就是 fork 的 `minimal_dev`：
#: `builder.rs:938-952` 的 `/dev/{ptmx,pts,null,urandom,zero,tty}`）。
DEV_INTEREST = ("null", "zero", "urandom", "tty", "ptmx", "pts")

#: 只用来打印形状、**不参与任何候选的装配**：`/dev/fd`、`/dev/std{in,out,err}` 在容器里
#: 都是 `-> /proc/self/fd/…` 的软链，悬空与否取决于 `/proc`，不是取决于 `/dev` 装了什么。
DEV_SYMLINKS = ("fd", "stdin", "stdout", "stderr")


def dev_node_shape(node: str) -> str:
    """节点在合成根里**是什么**：软链（带目标与是否解析得开）/ 目录 / 设备身份。

    比 `dev_node_identity()` 多两类：`/dev/fd`、`/dev/stderr` 这类**软链**（`stat` 只答它的
    目标在不在），以及 `/dev/pts`、`/dev/shm` 这类**目录**。差集表要按这一行区分
    "① 少了节点" 与 "① 有节点、但目标不在"——`os.path.exists` 会把两者都答成 False。
    """
    if os.path.islink(node):
        return f"symlink -> {os.readlink(node)} (resolves: {os.path.exists(node)})"
    if os.path.isdir(node):
        return "directory"
    return dev_node_identity(node)


def dev_shm_probe() -> str:
    """`/dev/shm` 不只是"目录在不在"：今天它是一个**可写的 tmpfs**（POSIX shm 的落点）。

    候选② 里这个目录根本不存在，所以这一行要同时量"在不在"和"能不能用"——只量存在性
    会把"少一个能用的 shm"读成"少一条列表项"。
    """
    if not os.path.isdir("/dev/shm"):
        return "absent"
    try:
        with open("/dev/shm/.dev-probe", "w") as fh:
            fh.write("x")
        os.unlink("/dev/shm/.dev-probe")
    except OSError as exc:
        return f"present, NOT writable ({type(exc).__name__} errno={exc.errno} {exc.strerror})"
    return f"present, writable, ismount={os.path.ismount('/dev/shm')}"


def part_dev() -> int:
    """三条 /dev 候选各自装配，跑同一组命令，产出可比较的一行。

    两个臂的骨架**字面相同**，只差 `dev` 里装的东西：
    - `host-tree`（= 候选①，也是今天 pure 的可见集合）：递归 bind 容器的整棵 `/dev`；
    - `minimal`（= 候选②）：先自建六个同名节点占位，再逐个 bind 宿主同名节点
      （`/dev/pts` 是目录）。绑定失败会 `FAILED` 而不是留在占位文件上（假绿入口）。

    两处与简报 Steps 不同的地方，都是为了让"差集"有信息量（简报 Files 允许改本 part）：
    1. 骨架补上 `lib64` 并绑定 `/lib64`：`/bin/sh -> dash` 是动态链接、PT_INTERP 是
       `/lib64/ld-linux-x86-64.so.2`，缺它就得不到 exec 型的答案（Task 1 §5 第 2 条 / §8.1）。
       任务要求的"exec 型命令"两行（`echo >/dev/null`、`head -c1 /dev/urandom`）因此才成立，
       且同一份日志里有 `skeleton has /lib64: True` 作为正向证据。
    2. `DEV_SKELETON_PROC=1` 时骨架里多一个空 `proc`（Task 2 已把 `proc` 钉进骨架常量）。
       默认关 = 简报 Steps 的形状；开 = Task 4 交付的形状。`/dev/fd -> /proc/self/fd`
       是软链，**答什么取决于 `/proc` 在不在**，所以两种形状都要有数字。
    """
    variant = os.environ.get("DEV_VARIANT", "host-tree")
    if variant not in ("host-tree", "minimal"):
        note(f"unknown DEV_VARIANT={variant!r} (expected host-tree|minimal)")
        return 2
    skeleton_proc = os.environ.get("DEV_SKELETON_PROC", "0") == "1"
    root = fresh(f"synth-root-dev-{variant}")
    for name in ("usr", "bin", "lib", "lib64", "etc", "tmp",
                 "home", "home/user", "workspace"):
        os.makedirs(os.path.join(root, name), mode=0o755, exist_ok=True)
    if skeleton_proc:
        os.makedirs(os.path.join(root, "proc"), mode=0o755, exist_ok=True)
    dev = os.path.join(root, "dev")
    os.makedirs(dev, mode=0o755, exist_ok=True)
    if variant == "minimal":
        for node in DEV_INTEREST:
            target = os.path.join(dev, node)
            if node == "pts":
                os.makedirs(target, exist_ok=True)
            else:
                open(target, "w").close()
    if not enter_ns():
        return 1
    for src in ("/usr", "/bin", "/lib", "/lib64"):
        if os.path.isdir(src):
            mount(src, os.path.join(root, src.lstrip("/")), None, MS_BIND | MS_REC)
    if variant == "host-tree":
        if not ok("bind /dev", mount("/dev", dev, None, MS_BIND | MS_REC)):
            return 1
    elif variant == "minimal":
        for node in DEV_INTEREST:
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
    note(f"skeleton_proc={int(skeleton_proc)} skeleton has /lib64: {os.path.exists('/lib64')}"
         f" /proc: {os.path.exists('/proc')}")
    #: 整份列表（原来是 `[:12]` 的截断）："/dev 一个都不多、一个都不少"要按条数比，
    #: 截断之后第 13 条及其后的增删都看不见。
    note(f"ls /dev count: {len(present)}")
    note(f"ls /dev: {present}")
    note(f"/dev/shm exists: {os.path.exists('/dev/shm')}")
    note(f"/dev/fd exists: {os.path.exists('/dev/fd')}")
    note(f"/dev/shm: {dev_shm_probe()}; ismount /dev/pts: {os.path.ismount('/dev/pts')}")
    for name in DEV_INTEREST + DEV_SYMLINKS:
        note(f"/dev/{name}: {dev_node_shape(os.path.join('/dev', name))}")
    #: `/dev/fd`（以及 `/dev/stdout`、`/dev/stderr`）是**软链**：`exists` 答的是"目标在不在"，
    #: 而目标 `/proc/self/fd` 由 `/proc` 决定 —— `/proc` 是骨架里的空目录时它不在，
    #: 于是软链悬空、`exists` = False。这一行把成因量出来，免得把 False 读成"没绑上 /dev"。
    note(f"stat /proc: {'exists' if os.path.exists('/proc') else 'ENOENT'}"
         f"; stat /proc/self/fd: {'exists' if os.path.exists('/proc/self/fd') else 'ENOENT'}")
    #: 这两行以前是 `os.system('echo x > /dev/null')` / `head -c1 /dev/urandom`，量到的却是
    #: exec 自己：`/bin/sh -> dash` 是动态链接，PT_INTERP 是绝对路径
    #: `/lib64/ld-linux-x86-64.so.2`，而本 part 当时的骨架（上面的目录清单）没有 `lib64`，
    #: 于是两个 `False` 与 `/dev` 毫无关系，看起来却像"/dev/null 不可用"。
    #: 现在：骨架补上 `lib64`（上面的正向证据行），所以这两行确确实实是 exec 型的答案；
    #: 再叠两行不经 exec 的纯 Python 探，交叉验证"解释器缺不缺"没在替 exec 作答。
    note(f"echo >/dev/null: {os.system('echo x > /dev/null') == 0}")
    note(f"head -c1 /dev/urandom: {os.system('head -c1 /dev/urandom > /dev/null') == 0}")
    note(f"open('/dev/null','w') writes: {write_dev_null()}")
    note(f"open('/dev/urandom','rb').read(1): {read_one_urandom()}")
    return 0


def part_devbase() -> int:
    """**今天 pure 的可见集合**：pure 的 `chroot` 是 `"/"`（N15），所以就是容器的 `/dev`。

    不 `unshare`、不 `pivot`：这份日志是"候选① 把 `/dev` 绑进合成根之后应当与之相等"的
    那一端（`ls /dev` 的条数与逐条类型）。差集表左边那一列由此来，而不是由"host-tree 臂
    大概等于宿主 /dev"这句推断来。
    """
    present = sorted(os.listdir("/dev"))
    note(f"ls /dev count: {len(present)}")
    note(f"ls /dev: {present}")
    note(f"/dev/shm exists: {os.path.exists('/dev/shm')}")
    note(f"/dev/fd exists: {os.path.exists('/dev/fd')}")
    note(f"/dev/shm: {dev_shm_probe()}; ismount /dev/pts: {os.path.ismount('/dev/pts')}")
    for name in DEV_INTEREST + DEV_SYMLINKS:
        note(f"/dev/{name}: {dev_node_shape(os.path.join('/dev', name))}")
    note(f"stat /proc: {'exists' if os.path.exists('/proc') else 'ENOENT'}"
         f"; stat /proc/self/fd: {'exists' if os.path.exists('/proc/self/fd') else 'ENOENT'}")
    note(f"echo >/dev/null: {os.system('echo x > /dev/null') == 0}")
    note(f"head -c1 /dev/urandom: {os.system('head -c1 /dev/urandom > /dev/null') == 0}")
    note("verdict: PASS (the tree today's pure shape reads; the left column of the diff)")
    return 0


PARTS = {
    "b2": part_b2,
    "tmpfs": part_tmpfs,
    "symlinks": part_symlinks,
    "proc": part_proc,
    "dev": part_dev,
    "devbase": part_devbase,
}

if __name__ == "__main__":
    PART = (sys.argv[1] if len(sys.argv) > 1 else "b2").lower()
    sys.exit(PARTS[PART]())
