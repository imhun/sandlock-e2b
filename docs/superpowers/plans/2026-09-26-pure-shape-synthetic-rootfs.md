# Pure 形态合成 rootfs 实施计划

> **执行状态（2026-09-27 更新）**：**Task 1–14 已落地**，四档全量 lane 验收全 `0 failed`（权威表在 `docs/pure-shape-decision.md` §7）。
> **仍有效的决定**：骨架**每沙箱一份**（`<base>/_pure_rootfs/<id>`，含拆箱清理）——共享骨架会给别的租户一个存在性 oracle。**已作废的假设**：`E2B_PURE_ROOTFS=synth` + `E2B_REAL_ROOT=0` 这个组合（**结构性不成立**，改成 `create_app` 当场 loud 拒绝的配置守卫）。**未定**：`E2B_PURE_ROOTFS` 默认值 `off` 要不要切 `synth` —— 留给用户拍板（`docs/deploy-clusters.md` §11.2）。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 pure 形态（无 base image）合成一个真实的 rootfs（普通目录 + bind 宿主系统目录 + workspace + 卷），让它也能吃 fork 的 `real_root`，从而那 33 条 `PURE_UNGATED` 从"在宿主路径空间解析"变成"在沙箱自己的树里解析"。

**Architecture:** E2B 侧把"有没有可进入的根"泛化成一个谓词（镜像 rootfs ∪ 合成 rootfs），pure 走与镜像同一条分支；合成根是**每沙箱一份的空目录骨架**（`<base>/_pure_rootfs/<sandbox_id>`，0755），mount 目标由 E2B 预建（fork 的 `realroot::build` 要求目标存在、会跳过源不存在的挂载），真正的 bind 在每沙箱自己的 mount ns 里由 fork 完成（`unshare` → 绑策略挂载 → 递归自绑根 → `pivot_root` → 丢 `CAP_SYS_ADMIN`）。fork 侧**不新增参数、不新增字段**：合成根复用 `chroot` 这一个既有入口（它同时是"中介被激活"的开关），"镜像根 vs 合成根"的差别留在 E2B 内部。

**Tech Stack:** Python 3.14（envd_service / pytest）、Rust（`third_party/sandlock` submodule：`sandlock-core` 的 `realroot` / `chroot` / `landlock`）、k0s arm64 车队 + Docker lane（worker seccomp 档 + 生产 cap 形状）。

## Global Constraints

- `E2B_REAL_ROOT` 是**部署级、形状无关**的布尔开关，默认 `false`（`envd_service/config.py:196`）；在有根的形态里它翻成 fork 的 `real_root`，在没有根的 pure 形态里它是一条 loud-once warning（`envd_service/executors/sandlock.py:2124-2138`、`2403-2417`）。
- fork 的 `real_root` **硬前置是"必须有 chroot root"**，没有就 `fail!("real_root requires a chroot root (the image rootfs)")`（`third_party/sandlock/crates/sandlock-core/src/context.rs:893-896`）。
- **`E2B_REAL_ROOT=0` 的模拟形态必须保留**（用户 2026-09-26 拍板）：本计划的每一处产品改动都要在 `E2B_REAL_ROOT=0` 与 `=1` 两态下各留一份结果，不接受"只在真根下验过"。
- 生产**永远是 image-rootfs**（`deploy/k8s/worker.yaml:364-367` 钉 digest、`deploy/k8s/control-plane.yaml:224`、所有 `deploy/compose/*.yml` 全都设 `E2B_BASE_IMAGE`）⇒ 本计划改变的是 dev/lane 形态 + N14-S5（"真根成为唯一形态"）的可达性，不是线上行为。
- worker seccomp 档只允许 **fstype == 0** 的 `mount`（`deploy/seccomp/sandlock-worker.json:837-852` 的 `"index": 2, "value": 0`），注释原话是 *"tmpfs, procfs, overlayfs and the rest stay refused for every process in this container"* ⇒ 合成根只能是**普通目录 + bind**，不许 tmpfs/procfs/overlayfs。
- 合成根的骨架目录必须是 **o+rx（0755）**：`unshare`/`bind`/`chdir` 是在沙箱**自己的 user namespace** 里、以**沙箱自己的宿主 uid** 跑的，它不拥有这些目录（0700 会在 bind 处 EACCES，建箱直接失败）。
- 临时文件（探针脚本、scratch、日志）一律放项目内 `tmp/`，容器内即 `/workspace/tmp/...`；不用系统 `/tmp`、不用 `$TMPDIR`。
- 测试断言必须**精确匹配**（禁 `toContain` / `includes` / `assertIn` 等部分匹配）；禁止 SKIP 或过滤失败输出；失败先看日志再改代码。
- 本机单测命令是 `tmp/testenv/bin/python -m pytest`（`.venv` 缺 `fakeredis`），基线 `tests/unit` = **16 failed / 1164 passed**（`docs/pure-shape-decision.md` §6 那张表，4 组已知红：gateway / priv_helpers / real_root_gate / xfs_quotactl），本计划不许改动这个数字。
- 容器 lane 与基线：`deploy/scripts/acceptance/gateA-full.sh`（镜像形态，`E2B_BASE_IMAGE=python-mcp:3.14`）= **1772 passed / 6 skipped / 3 xfailed / 0 failed**；`deploy/scripts/acceptance/gateB-full.sh`（pure，`E2B_BASE_IMAGE=`）= **1765 / 13 / 3 / 0**；`deploy/scripts/acceptance/phase2.sh`（非 root worker）= **57 passed / 1 skipped / 0 failed**。
- `deploy/scripts/arm-lane/x86-security.sh <E2B_REAL_ROOT 0|1> <log>` **只跑镜像形态**（它写死 `E2B_BASE_IMAGE=python-mcp:3.14`）⇒ pure 的两态 security 必须另起一条 lane，不能拿它冒充。
- fork 是 git submodule（`.gitmodules`：`path = third_party/sandlock`、`branch = upstream-pr/netns-free-clean`）⇒ 改动先在 submodule 里提交，再在父仓提交指针；两者的 commit 不是一个。
- fork 的套件通过数是**逐套钉死**的（`third_party/sandlock/docs/test-baseline.md` 的 `core_integ = 560`、`core_lib = 913`；`scripts/test-all.sh` 按"相等"判定），加用例必须同步改那一行。
- wheel 必须用 `deploy/scripts/build-sandlock-wheels.sh` 重建后再跑 lane（fork 的改动不进 wheel 就只在 cargo 测试里可见）。
- pure 形态**必须有 route-B 槽位**：拿不到槽位时中介以 euid 0 跑会被产品守卫拒（fail closed，`envd_service/executors/sandlock.py:1404-1420`），没有"能跑但不中介"的降级档。
- 越界路径的 errno 是**契约**：N15 之后"授权外"答 EACCES 而不是 ENOENT（`docs/pure-shape-decision.md` §6 末段）。合成根会改变一部分路径的 errno，改了就按逐字节三元组写进契约，不许"顺带变了"。
- 提交纪律：每个 Task 结束就提交（`git add` 精确文件，不 `git add -A`）；fork 的改动与父仓的改动分成两个 commit。
- **`tmp/` 在 `.gitignore` 里**（`.gitignore:5`）⇒ 本计划所有 `git add tmp/…` 的步骤都要写 `git add -f`，否则静默漏掉证据文件（Task 1 已踩过）。
- **架构**：Task 1 的全部证据是 `x86_64`，而部署目标是全 arm64 k0s（探针的 `SYS_PIVOT_ROOT` 表覆盖了 `aarch64: 41`，但从未在 arm64 上跑过）。**Task 9 的两条 lane 至少要有一条落在 arm 侧**（`deploy/scripts/arm-lane/` 已有现成通道），否则"合成根在部署架构上可用"这句话没有证据。
- **userns 身份那一半由既有路径覆盖，不属于本计划的新增面**：`context.rs:725-755` 每个 generation 都 `unshare(CLONE_NEWUSER)`，per-sandbox uid 形态下父进程写 `0 -> run_as`（`write_id_maps` `:284-294`）；生产今天跑的 route-B 槽位**每个建箱都走这段**（`test_route_b_restores_guest_root_with_and_without_pid_ns` 的 `id -u = 0` 就是它的验收）。Task 1 的探针不写 uid_map，所以它证明的是"在你已有的 userns 里 mount+pivot 能走通"——**两者的组合**（映射好的 userns + 合成根）由 Task 9 的 lane 覆盖，不需要单独造探针。
- **worker seccomp 档在集群上是在位的**（2026-09-26 实测）：两台节点 `/var/lib/k0s/kubelet/seccomp/sandlock-worker.json` 都是 14927 字节、sha256 `071486c0…`，与 `deploy/seccomp/sandlock-worker.json` 逐字节相同。所以 `fstype==0` 那条限制在真集群上成立（`docs/deploy-clusters.md:177` 那段"从未 apply"是历史叙述，§7 记的是 2026-09-25 已补）。

---

## 文件结构（本计划会碰到的文件与职责）

**E2B 侧（父仓）**

| 文件 | 职责 | 本计划对它做什么 |
|---|---|---|
| `envd_service/executors/sandlock.py` | 策略/形态/挂载表的唯一产地 | 加合成根的谓词、骨架物化、挂载表装配；镜像分支保持逐字不变 |
| `envd_service/config.py` | 全部 env 开关的入口 | 加 `E2B_PURE_ROOTFS`（`off`/`synth`）与 `E2B_PURE_ROOTFS_DIR` |
| `envd_service/executors/factory.py` | settings → executor 的装配点 | 把上面两个值传进 executor |
| `gateway_common/paths.py` | 平台自有的顶层命名空间清单 | `_pure_rootfs` 入 `RESERVED_PLATFORM_NAMESPACES` |
| `envd_service/agent.py` | 拆箱（`_delete_sandbox_runtime`） | 与 `_runtime/<id>` 一起收掉 `_pure_rootfs/<id>` |
| `tmp/k0s/` | 探针脚本 + lane runner + 日志 | 新增 4 个脚本（见 Task 1/9/12），产物都留在这里 |
| `tests/unit/*` | 形状/策略/清账的单测 | 新 `test_pure_rootfs_shape.py`、`test_pure_rootfs_config.py`；扩 `test_executor_policy.py`、`test_path_safety.py`、`test_platform_disk.py` |
| `tests/contract/*`、`tests/security/*` | 契约与安全套件 | 把形状前提过期的那几条迁移/重钉 |
| `docs/*` | 事实来源 | `n14-retire-the-emulation.md`、`pure-shape-decision.md`、`open-issues.md`、`task-backlog.md`、`chroot-workspace-exec.md` 收口 |

**fork 侧（submodule）**

| 文件 | 职责 | 本计划对它做什么 |
|---|---|---|
| `third_party/sandlock/crates/sandlock-core/tests/integration/test_instance_chroot.rs` | 中介 × chroot 的集成面 | 新增合成根用例（普通目录 + bind + `real_root` 两态） |
| `third_party/sandlock/docs/test-baseline.md` | 套件通过数基线 | `core_integ` 560 → 561 |
| （只读）`realroot.rs` / `context.rs` / `landlock.rs` / `network/rules.rs` | 真根与中介 | 探针若证明它们对合成根已成立，就保持一行不改；要改代码必须先有一条红的用例 |

**关键既有事实（写在计划里，免得实现者到处找）**

- 形态判据的唯一来源是 `sandlock.py:1889-1891` 的 `_is_image_rootfs`，全文件另有 6 处重抄同一表达式（`2025`、`2131`、`2164`、`2248`、`2410`、`2440`）。
- fork 侧 `landlock.rs:553-570` 的规则：**chroot 一旦非空，挂载点的授权按"挂载源的宿主路径"下发**（rootfs 里的挂载点只是空壳）。所以合成根必须把每个系统目录都写进 `fs_mount`（虚拟路径 = 宿主路径），否则绑进来的内容在 Landlock 下没有规则 ⇒ EACCES。
- `compose_virtual_etc_hosts`（`network/rules.rs:648-688`）只在 `root == "/"` 特判不读宿主的 `/etc/hosts`；合成根不是 `"/"`，所以它会去读 `<skeleton>/etc/hosts` —— 骨架里没有这个文件 ⇒ 回落到 loopback 基线，与今天等价（但**绝不能**绑宿主 `/etc`，否则等于把 N15 修掉的 wildcard 绕过重新灌回来）。
- `/etc/hosts` 的内容是中介合成的（`procfs.rs:1083-1094` 的 `handle_etc_hosts_open`，调用点 `seccomp/dispatch.rs:451-465`），与根里有没有这个文件无关。

---

### Task 1: 生产 cap 形状下的 go/no-go 探针（P1 + P5）

**Files:**
- Create: `deploy/scripts/acceptance/probe-pure-synth-root-plaindir.py`
- Create: `deploy/scripts/acceptance/probe-pure-synth-root.sh`
- Test: `tmp/k0s/pure-synth-root-prodshape.log`、`tmp/k0s/pure-synth-root-tmpfs.log`、`tmp/k0s/pure-synth-root-symlinks.log`

**Interfaces:**
- Consumes: nothing
- Produces: `probe-pure-synth-root-plaindir.py <b2|tmpfs|proc|dev|devbase|devdiff|symlinks>`（stdout 每条结论一行，退出码 0 = 该 part 的判定成立）；`probe-pure-synth-root.sh <part> <log>` 在生产 cap 形状的容器里跑它

- [ ] **Step 1: 写探针脚本**

```python
#!/usr/bin/env python3
"""合成根在生产 cap 形状下能不能 pivot？以及 /dev、/proc 该怎么装。

顺序照抄 `realroot::build`（`third_party/sandlock/crates/sandlock-core/src/realroot.rs:219-300`）：
unshare(CLONE_NEWNS) → `/` 设 MS_REC|MS_PRIVATE → 逐个 bind 到 `<root>/<virtual>` →
**递归自绑 root** → `chdir(root)` + `pivot_root(".", ".")` + `umount2(".", MNT_DETACH)` + `chdir("/")`。

与 `deploy/scripts/acceptance/probe-pure-realroot.py` 的三点区别（这是本轮的方法论修正点）：
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
import sys

libc = ctypes.CDLL("libc.so.6", use_errno=True)

CLONE_NEWNS = 0x00020000
MS_REC = 16384
MS_PRIVATE = 1 << 18
MS_BIND = 4096
MNT_DETACH = 2

#: pivot_root 的 syscall 号按架构不同（x86_64 = 155，aarch64/riscv64 = 41）。
SYS_PIVOT_ROOT = {"x86_64": 155, "aarch64": 41, "riscv64": 41}[os.uname().machine]

#: 宿主侧有、而合成根里不该有的东西（lane 里仓库就挂在 /src）。
HOST_ONLY = os.environ.get("HOST_ONLY", "/src")

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
    return ok("unshare(CLONE_NEWNS)", libc.unshare(CLONE_NEWNS)) and ok(
        "mount(/ private)", mount(None, "/", None, MS_REC | MS_PRIVATE)
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
            if os.path.exists(src):
                mount(src, os.path.join(dev, node), None, MS_BIND | MS_REC)
    if not self_bind_and_pivot(root):
        return 1
    present = sorted(os.listdir("/dev")) if os.path.isdir("/dev") else []
    note(f"variant={variant}")
    note(f"ls /dev: {present[:12]}")
    note(f"/dev/shm exists: {os.path.exists('/dev/shm')}")
    note(f"/dev/fd exists: {os.path.exists('/dev/fd')}")
    note(f"echo >/dev/null: {os.system('echo x > /dev/null') == 0}")
    note(f"head -c1 /dev/urandom: {os.system('head -c1 /dev/urandom > /dev/null') == 0}")
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
```

- [ ] **Step 2: 写 lane runner（生产 cap 形状，无 SYS_ADMIN）**

```sh
#!/bin/sh
# 合成根探针的 runner：**生产 cap 形状**（worker 声明的那五个 cap，无 SYS_ADMIN）
# + 出厂 seccomp 档 + 项目内 scratch。用法：probe-pure-synth-root.sh <part> <log>
#
# 退出码就是判定契约：0 = 该 part 的判定成立；1 = 某一步 FAILED（或 b2 的 host-only
# 在 pivot 后仍可见）；2 = VACUOUS（只有 b2：传进来的 HOST_ONLY 在 pivot 前就不存在，
# 那句 `hidden` 会白给）。见 §Step 3。
# （这份是初版副本；rc 契约以 deploy/scripts/acceptance/probe-pure-synth-root.sh 为准 —— Task 3 起
#  `dev` 与 `devdiff` 也会返回 1/2，枚举见 Task 3 的 Step 2b。）
set -eu
cd "$(dirname "$0")/../.."
part="$1"
log="$2"
mkdir -p tmp/k0s/scratch
docker run --rm --init --network host \
    --cap-drop ALL \
    --cap-add NET_BIND_SERVICE --cap-add SETUID --cap-add SETGID \
    --cap-add CHOWN --cap-add DAC_OVERRIDE \
    --security-opt seccomp="$(pwd)/deploy/seccomp/sandlock-worker.json" \
    --security-opt apparmor=unconfined \
    -e HOST_ONLY="${HOST_ONLY:-/workspace/AGENTS.md}" \
    -e DEV_VARIANT="${DEV_VARIANT:-host-tree}" \
    -v "$(pwd):/workspace" -w /workspace \
    e2b-sandlock-test:latest \
    python deploy/scripts/acceptance/probe-pure-synth-root-plaindir.py "$part" > "$log" 2>&1
```

- [ ] **Step 3: 跑它（b2 = 这条路线能不能做）**

Run: `sh deploy/scripts/acceptance/probe-pure-synth-root.sh b2 tmp/k0s/pure-synth-root-prodshape.log && cat tmp/k0s/pure-synth-root-prodshape.log`
Expected: 最后三行是 `[part b2] bound system dirs: <n>`、`[part b2] host-only /workspace/AGENTS.md: hidden`、`[part b2] verdict: PASS (plain directory + bind + pivot_root works in the pinned shape)`，退出码 0。
`HOST_ONLY` 的默认值 `/workspace/AGENTS.md` 是 lane 镜像里**真有**的路径，所以不传 `-e HOST_ONLY` 也必须是 PASS/rc 0；**VACUOUS/rc 2 只留给"显式传了一个 pivot 之前不存在的路径"**（例如 `HOST_ONLY=/src` —— 本 lane 的仓库挂在 `/workspace`，`/src` 不存在）。探针在 `enter_ns()` 之前就会先打一行 `[part b2] host-only <path> exists pre-pivot: <bool>`：`True` 才继续做后面的隔离判定，`False` 直接 `verdict: VACUOUS` + rc 2（见 2026-09-26 Task 1 评审修 M3/M4）。证据：`tmp/k0s/pure-synth-root-prodshape-default.log`（默认值 PASS）、`tmp/k0s/pure-synth-root-prodshape-vacuous-explicit.log`（显式不存在 ⇒ VACUOUS）。**注意**：本 Task Step 1 里内嵌的那份探针代码是初版副本，已落后于 `deploy/scripts/acceptance/probe-pure-synth-root-plaindir.py`（两轮评审修：`unshare(CLONE_NEWUSER)`、VACUOUS 判定、`dev` 的设备身份断言），以那个文件为准。

- [ ] **Step 4: 跑对照臂（tmpfs 必须是 EPERM）**

Run: `sh deploy/scripts/acceptance/probe-pure-synth-root.sh tmpfs tmp/k0s/pure-synth-root-tmpfs.log && cat tmp/k0s/pure-synth-root-tmpfs.log`
Expected: 含 `[part tmpfs] mount(tmpfs): FAILED errno=1 (Operation not permitted)` 与 `[part tmpfs] verdict: PASS-NEGATIVE (tmpfs unavailable; plain directory + bind is the route)`

- [ ] **Step 5: 跑软链表（P5，宿主侧，直接用本机 python）**

Run: `tmp/testenv/bin/python deploy/scripts/acceptance/probe-pure-synth-root-plaindir.py symlinks | tee tmp/k0s/pure-synth-root-symlinks.log`
Expected: 逐行是 `/usr: real directory`、`/bin: symlink -> usr/bin` 这类事实行，末行 `[part b2] verdict: PASS (this table is the input for the bind list)`；这张表就是 Task 4 的系统目录清单的输入

- [ ] **Step 6: 提交**

```bash
git add deploy/scripts/acceptance/probe-pure-synth-root-plaindir.py deploy/scripts/acceptance/probe-pure-synth-root.sh tmp/k0s/pure-synth-root-prodshape.log tmp/k0s/pure-synth-root-tmpfs.log tmp/k0s/pure-synth-root-symlinks.log
git commit -m "probe(pure): a plain directory + bind + pivot_root works under the pinned worker shape"
```

### Task 2: `/proc` 判定（P2：骨架里到底要不要建空 `/proc`）

**Files:**
- Modify: `deploy/scripts/acceptance/probe-pure-synth-root-plaindir.py` 的 `part_proc`（Task 1 已写入）
- Test: `tmp/k0s/pure-synth-root-proc.log`

**Interfaces:**
- Consumes: Task 1 的 `part_proc`
- Produces: 一句可执行事实 —— 骨架常量里必须/不必有 `"proc"`，这是 Task 4 的输入

- [ ] **Step 1: 跑 part_proc，把"骨架里没有 /proc"的真实结果记下来**

Run: `sh deploy/scripts/acceptance/probe-pure-synth-root.sh proc tmp/k0s/pure-synth-root-proc.log && cat tmp/k0s/pure-synth-root-proc.log`
Expected: 打印 `[part proc] stat /proc: ENOENT`，退出码 0

- [ ] **Step 2: 把"今天 pure 的 /proc 由中介合成"这条事实钉住（只读）**

Run: `grep -n "handle_etc_hosts_open\|fn handle_proc\|canon_proc_self" third_party/sandlock/crates/sandlock-core/src/procfs.rs third_party/sandlock/crates/sandlock-core/src/chroot/dispatch.rs | head -20`
Expected: 命中 `procfs.rs` 的 `/proc` 合成入口，以及 `dispatch.rs:3384` 的 `canon_proc_self("/etc/passwd", 42)` —— `/proc` 的内容来自中介，与根里那个目录存不存在无关；目录只在 `ls /proc`（列目录本身）时被内核看到

- [ ] **Step 3: 把结论写成骨架常量（Task 4 照抄这一段）**

```python
#: The pure shape's skeleton. `proc` is in the list although the fork never
#: reads a file under it: `ls /proc` after the pivot lists the *directory*, and
#: today's pure shape (root "/") answers it with the mediator's empty view. A
#: skeleton without `/proc` answers ENOENT instead, so the directory is part of
#: the shape, not of the mounts. Measured: tmp/k0s/pure-synth-root-proc.log.
_SYNTHETIC_ROOTFS_SKELETON_DIRS: tuple[str, ...] = (
    "proc",
    "dev",
    "etc",
    "tmp",
    "root",
    "run",
    "var",
    "srv",
    "media",
    "mnt",
    "home",
    "workspace",
)
```

- [ ] **Step 4: 提交**

```bash
git add tmp/k0s/pure-synth-root-proc.log
git commit -m "probe(pure): the skeleton needs an empty /proc (ENOENT otherwise)"
```

### Task 3: `/dev` 判定表（P3，三条候选的差集）

**Files:**
- Modify: `deploy/scripts/acceptance/probe-pure-synth-root-plaindir.py` 的 `part_dev`（Task 1 已写入）
- Test: `tmp/k0s/pure-synth-root-dev-hosttree.log`、`tmp/k0s/pure-synth-root-dev-minimal.log`

**Interfaces:**
- Consumes: Task 1 的 `part_dev` + `DEV_VARIANT`
- Produces: `/dev` 的选定集合（Task 4 的 `_synthetic_rootfs_mounts()` 照抄）与"比今天多什么/少什么"的差集

- [ ] **Step 1: 跑 host-tree 候选（= 今天 pure 看到的 /dev）**

Run: `DEV_VARIANT=host-tree sh deploy/scripts/acceptance/probe-pure-synth-root.sh dev tmp/k0s/pure-synth-root-dev-hosttree.log && cat tmp/k0s/pure-synth-root-dev-hosttree.log`
Expected（**按实测修正**，见 5358cef 报告 §4.1/§4.2）: `variant=host-tree` 段里
`ls /dev count: 14`（全量、不再 `[:12]` 截断）、`/dev/shm exists: True`、
`/dev/fd exists: **False**`（这条是 `-> /proc/self/fd` 的软链，`exists` 答的是**目标**在不在；
骨架里没有真 procfs ⇒ 悬空 —— 节点本身在，别把它读成"没绑上 /dev"）、
`skeleton has /lib64: True`（必须出现在 exec 两行**之前**）、`echo >/dev/null: True`、
`head -c1 /dev/urandom: True`

- [ ] **Step 2: 跑 minimal_dev 候选（fork 的六节点）**

Run: `DEV_VARIANT=minimal sh deploy/scripts/acceptance/probe-pure-synth-root.sh dev tmp/k0s/pure-synth-root-dev-minimal.log && cat tmp/k0s/pure-synth-root-dev-minimal.log`
Expected（同样按实测修正）: `variant=minimal` 段里 `ls /dev count: 6`、`/dev/shm exists: False`、
`/dev/fd exists: False`（连节点都没有 ⇒ `/dev/fd: unavailable (FileNotFoundError errno=2)`，
与候选① 的"节点在、目标不在"是**两种** False）

- [ ] **Step 2b: 把"集合相等"做成可复算的产物（`devdiff` part，评审 Important 2）**

`part dev` 的 rc 0 只说明"装配走通了"；"14 = 14、无增无减"这个判定由探针自己的 `devdiff`
part 复算 —— 它读两份日志的 `ls /dev` 清单按**集合**比，rc 语义是
**0 = 集合相等 / 1 = 有增删 / 2 = VACUOUS**（日志读不到、取不到自洽的 `ls /dev` 清单或
count 对不上、两份日志是同一个文件 —— 这些情况下"相等"没有信息量，不许报 0）。

Run: `sh deploy/scripts/acceptance/probe-pure-synth-root.sh devdiff tmp/k0s/pure-synth-root-devdiff-hosttree.log`
Expected: rc **0**，末行 `verdict: EQUAL (set of 14 entries, no additions, no removals)`

Run: `DEVDIFF_LOGS=tmp/k0s/pure-synth-root-dev-baseline-container.log:tmp/k0s/pure-synth-root-dev-minimal-guarded.log sh deploy/scripts/acceptance/probe-pure-synth-root.sh devdiff tmp/k0s/pure-synth-root-devdiff-minimal.log`
Expected: rc **1**，`removed: ['fd', 'full', 'mqueue', 'random', 'shm', 'stderr', 'stdin', 'stdout']`、`added: []`

- [ ] **Step 3: 把差集表写进决定（D1 的拍板输入）**

```
| 项 | 今天 pure（宿主 /dev） | 候选① host-tree | 候选② minimal_dev |
|---|---|---|---|
| ls /dev | 容器 /dev 的全部（14 条） | 同左（14 条，集合逐条相等） | 六个节点 |
| /dev/shm 存在 | True | True | False |
| /dev/fd 存在 | True（`/proc` 是真 procfs ⇒ 软链解析得开） | **False**（同一软链，悬空） | False（连节点都没有） |
| echo >/dev/null | True | True | True |
| head -c1 /dev/urandom | True | True | True |
```

（上面 `/dev/fd` 与 `ls /dev` 两行是按 5358cef 的实测改过的原表：初版把候选① 写成
`/dev/fd exists: True`，实测是 False —— 见 Task 3 报告 §4.1。候选② 少的也不是 2 条而是
**8 条**：`fd`、`full`、`mqueue`、`random`、`shm`、`stderr`、`stdin`、`stdout`。）

规则：本计划的默认是**候选①（递归 bind 容器的 `/dev`）**——它"一个都不多、一个都不少"地等于今天 pure 的可见集合。候选② 是一次**收紧**（实测少 8 条：`fd`、`full`、`mqueue`、`random`、`shm`、`stderr`、`stdin`、`stdout`），要做就必须作为独立的行为变更单独立项与验收。

本轮（Task 3 评审修复）补的两条守卫，都在探针里，且都只用 rc **2** 说话：

1. **源缺失不再是静默跳过**（评审 Important 1）：`minimal` 臂的候选节点在宿主上不存在时，
   骨架里那个预造的普通同名占位文件会留在 `/dev/<node>` 上替它作答（纯 Python 探照样答 True）
   ⇒ 点名 `skip <src> (absent on this host)` 并返回 rc **2**（形状不是声称的六节点，属空洞）；
2. **exec 型结论不再依赖巧合**（评审 Important 3）：`echo >/dev/null` / `head -c1 /dev/urandom`
   这两行前面加了 `/lib64` 守卫（骨架没有它就 rc **2**），让它们依赖 `/dev` 而不是"骨架恰好
   有 lib64"。

- [ ] **Step 4: 提交**

```bash
git add tmp/k0s/pure-synth-root-dev-hosttree.log tmp/k0s/pure-synth-root-dev-minimal.log
git commit -m "probe(pure): /dev candidate diff table (host-tree is the equivalence choice)"
```

（**这两个日志名是 Task 1 那轮已提交的历史证据，不要覆盖**。本轮的两臂日志因此是
`tmp/k0s/pure-synth-root-dev-hosttree-guarded.log` /
`-minimal-guarded.log`，`devdiff` 的产物是
`tmp/k0s/pure-synth-root-devdiff-{hosttree,minimal}.log`，rc 2 的演示是
`tmp/k0s/pure-synth-root-dev-minimal-srcabsent.log`。）

### Task 4: E2B 侧形态谓词 + 骨架物化 + 挂载表（单元级，macOS 可跑）

**Files:**
- Modify: `envd_service/executors/sandlock.py:346-364`（在 `_minimal_dev_mounts` 之后追加常量与两个 helper）
- Modify: `envd_service/executors/sandlock.py:618-680`（构造器新参数 + real-root 闸门）
- Modify: `envd_service/executors/sandlock.py:1889-1948`（谓词、`_chroot_root`、`_view_cwd`）
- Modify: `envd_service/executors/sandlock.py:2025-2051`、`2124-2138`、`2164-2247`（`_policy_ceiling` 的形状分支）
- Modify: `envd_service/executors/sandlock.py:2309-2325`、`2403-2417`、`2440-2496`（`_build_sandbox` 的同名分支）
- Test: `tests/unit/test_pure_rootfs_shape.py`（新建）、`tests/unit/test_executor_policy.py`

**Interfaces:**
- Consumes: Task 2 的骨架目录结论、Task 3 的 `/dev` 结论
- Produces: `SandlockExecutor(..., pure_rootfs_dir: Path | str | None = None)`；属性 `_synthetic_rootfs -> Path | None`、`_has_sandbox_root -> bool`、`_sandbox_root -> Path | None`；方法 `_materialize_root(root: Path) -> dict[str, str]`；模块函数 `_synthetic_rootfs_mounts() -> dict[str, str]`、`_materialize_synthetic_rootfs(root: Path, mounts: dict[str, str]) -> None`；常量 `_SYNTHETIC_ROOTFS_SYSTEM_DIRS`、`_SYNTHETIC_ROOTFS_SKELETON_DIRS`

- [ ] **Step 1: 写会失败的测试**

```python
"""The pure shape's synthesized root: predicates, skeleton, mount map.

Off-Linux by construction: the native sandlock module is absent, so
``_build_instance_policy`` returns the plain namespace the fork would receive,
and materializing the skeleton only creates directories.
"""
from __future__ import annotations

import os
from pathlib import Path

from envd_service.executors.sandlock import (
    SandlockExecutor,
    _SYNTHETIC_ROOTFS_SYSTEM_DIRS,
    _synthetic_rootfs_mounts,
)


def _executor(tmp_path: Path, **overrides) -> SandlockExecutor:
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    kwargs = dict(
        workspace_dir=str(ws),
        base_image=None,
        image_rootfs=None,
        memory_mb=512,
        cpu_percent=100,
        disk_mb=1024,
        max_processes=64,
        max_open_files=4096,
        allow_internet_access=False,
        enable_network=False,
        sandbox_id="sbx_synth",
        pure_rootfs_dir=str(tmp_path / "_pure_rootfs"),
    )
    kwargs.update(overrides)
    return SandlockExecutor(**kwargs)


def test_the_synthetic_root_is_the_sandboxs_own_directory(tmp_path: Path) -> None:
    ex = _executor(tmp_path)
    assert ex._synthetic_rootfs == tmp_path / "_pure_rootfs" / "sbx_synth"
    assert ex._has_sandbox_root is True
    assert ex._chroot_root == str(tmp_path / "_pure_rootfs" / "sbx_synth")


def test_without_the_switch_the_pure_shape_keeps_the_identity_root(tmp_path: Path) -> None:
    ex = _executor(tmp_path, pure_rootfs_dir=None)
    assert ex._synthetic_rootfs is None
    assert ex._has_sandbox_root is False
    assert ex._chroot_root == "/"


def test_an_image_sandbox_ignores_the_pure_rootfs_switch(tmp_path: Path) -> None:
    rootfs = tmp_path / "image"
    rootfs.mkdir()
    ex = _executor(tmp_path, base_image="python:3.11-slim", image_rootfs=rootfs)
    assert ex._synthetic_rootfs is None
    assert ex._chroot_root == str(rootfs)


def test_the_synthetic_root_does_not_change_the_allow_list(tmp_path: Path) -> None:
    """The route's whole claim: same visible set, different resolution."""
    off = _executor(tmp_path / "off", pure_rootfs_dir=None)._build_instance_policy()
    on = _executor(tmp_path / "on")._build_instance_policy()
    assert list(on.fs_readable) == list(off.fs_readable)
    assert list(on.fs_writable) == list(off.fs_writable)
    assert list(on.fs_denied) == list(off.fs_denied)
    assert on.chroot == str(tmp_path / "on" / "_pure_rootfs" / "sbx_synth")
    assert off.chroot == "/"


def test_the_synthetic_root_mount_map_is_workspace_volumes_system_dirs_dev(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    vol = tmp_path / "vol"
    vol.mkdir()
    ex = _executor(
        tmp_path,
        fs_mounts={"/workspace/mnt/data": str(vol), "/home/user/mnt/data": str(vol)},
    )
    policy = ex._build_instance_policy()
    expected = {
        "/home/user": str(ws),
        "/workspace": str(ws),
        "/workspace/mnt/data": str(vol),
        "/home/user/mnt/data": str(vol),
    }
    expected.update(_synthetic_rootfs_mounts())
    assert dict(policy.fs_mount) == expected
    # Declaration order is load-bearing: the fork breaks host-source ties by it.
    assert list(dict(policy.fs_mount))[:2] == ["/home/user", "/workspace"]


def test_the_skeleton_is_traversable_and_has_every_target(tmp_path: Path) -> None:
    ex = _executor(tmp_path)
    policy = ex._build_instance_policy()
    root = ex._synthetic_rootfs
    assert root is not None
    assert (root / "proc").is_dir()
    assert (root / "home" / "user").is_dir()
    for virtual in policy.fs_mount:
        assert (root / str(virtual).lstrip("/")).exists(), virtual
    assert oct(root.stat().st_mode & 0o777) == "0o755"
    assert oct((root / "proc").stat().st_mode & 0o777) == "0o755"


def test_system_dirs_are_filtered_by_host_existence(tmp_path: Path) -> None:
    """A missing host directory must not become an empty stub in the sandbox."""
    expected = {d: d for d in _SYNTHETIC_ROOTFS_SYSTEM_DIRS if os.path.isdir(d)}
    expected["/dev"] = "/dev"
    assert _synthetic_rootfs_mounts() == expected
```

- [ ] **Step 2: 跑它，确认失败**

Run: `tmp/testenv/bin/python -m pytest tests/unit/test_pure_rootfs_shape.py -q`
Expected: FAIL with `ImportError: cannot import name '_SYNTHETIC_ROOTFS_SYSTEM_DIRS' from 'envd_service.executors.sandlock'`

- [ ] **Step 3: 最小实现 —— 常量、挂载表、物化（插在 `_minimal_dev_mounts` 之后）**

```python
#: The system directories a synthesized pure root (N16) binds from the host.
#: Filtered by host existence at build time: the fork *skips* a mount whose
#: source is gone but *fails* on a missing target, so a directory the host does
#: not have must never reach the map (it would leave an empty stub behind and
#: make `/opt` exist or not depending on the node). Measured table:
#: tmp/k0s/pure-synth-root-symlinks.log.
_SYNTHETIC_ROOTFS_SYSTEM_DIRS: tuple[str, ...] = (
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
    "/opt",
)

#: Everything the sandbox must see as a directory in its own root, whether or
#: not anything is bound onto it. The list is the shape, not a convenience:
#: `/proc` is only ever *listed* through it (its content is the mediator's
#: synthesis) and the rest is where today's pure shape answers `EACCES` for a
#: path outside the allow-list -- an empty directory answers the same way, a
#: missing one answers `ENOENT` and changes the contract.
_SYNTHETIC_ROOTFS_SKELETON_DIRS: tuple[str, ...] = (
    "proc",
    "dev",
    "etc",
    "tmp",
    "root",
    "run",
    "var",
    "srv",
    "media",
    "mnt",
    "home",
    "workspace",
)


def _synthetic_rootfs_mounts() -> dict[str, str]:
    """The bind mounts that fill a synthesized pure root.

    `/dev` is bound as the whole container tree, not as ``minimal_dev``'s six
    nodes: the pure shape's `/dev` has always *been* that tree, and the six
    nodes would silently drop `/dev/shm` plus seven more entries -- a tightening
    of the tenant's view this route must not do by accident. What the whole bind
    buys is **node-level** equivalence: the same 14 entries, with the four
    symlinks into ``/proc/self/fd`` (``fd``/``stdin``/``stdout``/``stderr``)
    preserved in *shape*. Whether those four resolve is the `/proc` synthesis /
    mediator's line of business, not this mount's -- and it is **not** a promise
    that bash process substitution works in a synthesized root (in this lane the
    four dangle; today's pure shape resolves them only because its `/proc` is a
    real procfs, while the skeleton's is an empty directory).
    Measured: tmp/k0s/pure-synth-root-dev-hosttree-guarded.log vs
    tmp/k0s/pure-synth-root-dev-minimal-guarded.log (this round's rerun: full
    listing, no `[:12]` truncation, `skeleton has /lib64: True` ahead of the exec
    lines). The "sets are equal" call is re-runnable as the probe's ``devdiff``
    part: tmp/k0s/pure-synth-root-devdiff-hosttree.log (rc 0 = equal, 14 = 14).
    """
    mounts = {
        directory: directory
        for directory in _SYNTHETIC_ROOTFS_SYSTEM_DIRS
        if Path(directory).is_dir()
    }
    mounts["/dev"] = "/dev"
    return mounts


def _materialize_synthetic_rootfs(root: Path, mounts: dict[str, str]) -> None:
    """Create the skeleton and every mount target a synthesized root needs.

    ``0755`` on purpose: the binds and the ``chdir`` into the root happen in the
    sandbox's own user namespace as the sandbox's own host uid, which owns none
    of these directories (a ``0700`` root fails the bind with EACCES before the
    sandbox ever starts). ``_ensure_chroot_mount_points`` then creates the
    targets the fork refuses to run without.
    """
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o755)
    for name in _SYNTHETIC_ROOTFS_SKELETON_DIRS:
        (root / name).mkdir(mode=0o755, exist_ok=True)
    (root / "home" / "user").mkdir(mode=0o755, exist_ok=True)
    _ensure_chroot_mount_points(root, mounts)
```

- [ ] **Step 4: 最小实现 —— 谓词、构造器参数、挂载表装配、四个形状分支**

```python
    # __init__ 的签名（`fs_mounts` 旁）与赋值：
    #     pure_rootfs_dir: Path | str | None = None,
    #     self._pure_rootfs_dir = Path(pure_rootfs_dir) if pure_rootfs_dir else None
    # real-root 闸门（今天写的是 `self._base_image and self._image_rootfs is not None`）：
        if self._real_root and self._has_sandbox_root:
            reason = _real_root_capability()

    @property
    def _synthetic_rootfs(self) -> Path | None:
        """The synthesized root this pure sandbox pivots into, or None (N16).

        Keyed on the *absence* of a base image: a sandbox with an image always
        uses the image. ``E2B_PURE_ROOTFS=off`` (the default) leaves the pure
        shape on N15's identity translation, so this is a shape switch an
        operator flips -- not a silent change to a fleet.
        """
        if self._base_image or self._image_rootfs is not None:
            return None
        if self._pure_rootfs_dir is None:
            return None
        return Path(self._pure_rootfs_dir) / (self._sandbox_id or "unnamed")

    @property
    def _has_sandbox_root(self) -> bool:
        """Whether this sandbox has a root of its own to be confined to."""
        return self._is_image_rootfs or self._synthetic_rootfs is not None

    @property
    def _sandbox_root(self) -> Path | None:
        """The root the path mediator and the fork's real root work on."""
        if self._is_image_rootfs:
            return Path(self._image_rootfs)
        return self._synthetic_rootfs

    def _volume_only_mount_map(self) -> dict[str, str]:
        """N15's mount map: the workspace under both aliases, plus the volumes.

        Named so the two builders stop carrying a third copy of these five
        lines (``_policy_ceiling`` and ``_build_sandbox``).
        """
        mount_map = {
            "/home/user": self._workspace_dir,
            "/workspace": self._workspace_dir,
        }
        mount_map.update(self._fs_mounts)
        return mount_map

    def _materialize_root(self, root: Path) -> dict[str, str]:
        """This sandbox's mount map, with every target created on disk.

        The image branch is verbatim today's code (it is production; its
        pre-created ``workspace``/``home/user``/``dev`` entries exist because
        slim images extract without them). The synthesized branch adds the host
        system directories and the whole-tree ``/dev`` bind on top of the
        workspace aliases and the volumes.
        """
        mount_map = self._volume_only_mount_map()
        if self._is_image_rootfs:
            for mount_point in ("workspace", "home/user", "dev"):
                root.joinpath(mount_point).mkdir(parents=True, exist_ok=True)
            for virtual in self._fs_mounts:
                root.joinpath(virtual.removeprefix("/")).mkdir(parents=True, exist_ok=True)
            mount_map.update(_minimal_dev_mounts())
            _ensure_chroot_mount_points(root, mount_map)
            return mount_map
        mount_map.update(_synthetic_rootfs_mounts())
        _materialize_synthetic_rootfs(root, mount_map)
        return mount_map

    @property
    def _chroot_root(self) -> str:
        """The root the path mediator confines this sandbox to (N15/N16)."""
        root = self._sandbox_root
        return str(root) if root is not None else "/"

    def _view_cwd(self, config: ExecConfig) -> str | None:
        cwd = (config.cwd or "").strip()
        if self._has_sandbox_root:
            # Both rooted shapes answer with the virtual spelling: the fork
            # joins it under the root before its real chdir, and both roots
            # carry /home/user as the workspace's canonical alias.
            if not cwd or cwd.startswith(str(self._workspace_dir)):
                return "/home/user"
            return cwd or None
        return cwd or str(self._workspace_dir)
```

然后把形状分支按下面的形状收口（`_policy_ceiling` 与 `_build_sandbox` 各一处）：

```python
        fs_denied: list[str] = []
        if self._is_image_rootfs:
            # 逐字保留今天的镜像分支（`fs_readable += ["/"]`、
            # `fs_writable += ["/workspace", "/home/user", *卷]`、
            # `fs_denied = ["/proc/kcore", "/sys"]`）。
        else:
            # N15 的 pure 允许表，开不开合成根都一样：合成根**不加** `"/"`
            # （那在镜像形态里意思是"整颗 rootfs"，在这里只会指到骨架）、
            # `fs_denied` 保持空（Landlock 的允许表已经拒了表外的路径）。
            fs_writable = self._volume_only_mount_map_writable(fs_writable)

        root = self._sandbox_root
        if root is not None:
            kwargs["chroot"] = self._chroot_root
            kwargs["fs_mount"] = self._materialize_root(root)
            if not self._is_image_rootfs:
                # 写回 kwargs：这个 builder 的 dict 在形状分支**之前**就建好了，
                # 只有镜像形态能省掉这次写回（它的 `fs_readable` 含 "/"）。
                kwargs["fs_writable"] = fs_writable
        else:
            # N15，没有根时的兜底：宿主根 + identity 翻译。
            kwargs["chroot"] = self._chroot_root
            kwargs["fs_mount"] = self._volume_only_mount_map()
            kwargs["fs_writable"] = fs_writable

        if self._real_root:
            if self._has_sandbox_root:
                kwargs["real_root"] = True
            else:
                logger.warning(
                    "E2B_REAL_ROOT is set but sandbox %s has no image rootfs "
                    "and no synthesized root (pure shape): real_root has no "
                    "effect for it",
                    self._sandbox_id or "<unnamed>",
                )
```

`_volume_only_mount_map_writable` 是今天 pure 分支那三行的实名版本（抽出来只为让两个 builder 不重抄第三遍）：

```python
    def _volume_only_mount_map_writable(self, fs_writable: list[str]) -> list[str]:
        """The mount points the pure shape must declare writable.

        Load-bearing twice over: the fork derives a mount *source*'s rights from
        what the policy declares for its mount point, and the per-exec cwd
        (`/home/user`) has to be inside the instance ceiling or the exec is
        refused outright ("exec params exceed the instance policy ceiling").
        """
        return (
            list(fs_writable)
            + ["/workspace", "/home/user"]
            + [str(virtual) for virtual in self._fs_mounts]
        )
```

`2248` 的 `if http_allow and self._image_rootfs is not None:`（CA 注入）**不动**，它按定义只对镜像成立。

- [ ] **Step 5: 跑测试，确认通过**

Run: `tmp/testenv/bin/python -m pytest tests/unit/test_pure_rootfs_shape.py tests/unit/test_executor_policy.py tests/unit/test_sandlock_executor_instance.py tests/unit/test_sandlock_executor_route_b.py -q`
Expected: PASS（`test_pure_rootfs_shape.py` 7 条全绿；其余三个文件逐条与改动前相同 —— 尤其是 `test_non_chroot_cwd_is_the_host_path_the_fork_can_chdir_to`，它在 `pure_rootfs_dir=None` 下仍然描述今天的行为）

- [ ] **Step 6: 跑整个 `tests/unit`，确认基线没动**

Run: `tmp/testenv/bin/python -m pytest tests/unit -q | tail -3`
Expected: `16 failed, 1171 passed`（原 1164 + 本 Task 的 7 条）；若 failed 数变了，先归因再改代码，**不许**改基线

- [ ] **Step 7: 提交**

```bash
git add envd_service/executors/sandlock.py tests/unit/test_pure_rootfs_shape.py
git commit -m "feat(sandlock): the pure shape gets a synthesized root (E2B side)"
```

### Task 5: 开关与配置管道（`E2B_PURE_ROOTFS` / `E2B_PURE_ROOTFS_DIR`）

**Files:**
- Modify: `gateway_common/paths.py:126-241`（新常量 + 入 `RESERVED_PLATFORM_NAMESPACES`）
- Modify: `envd_service/config.py:24-44`（`_pure_rootfs_dir`）、`envd_service/config.py:120-130`（两个新字段）
- Modify: `envd_service/executors/factory.py:186-192`（装配处传参）
- Test: `tests/unit/test_pure_rootfs_config.py`（新建）、`tests/unit/test_path_safety.py`

**Interfaces:**
- Consumes: Task 4 的 `SandlockExecutor(pure_rootfs_dir=...)`
- Produces: `gateway_common.paths.PURE_ROOTFS_DIR_NAME`（`"_pure_rootfs"`）；`Settings.pure_rootfs: str`（`"off"` / `"synth"`）、`Settings.pure_rootfs_dir: Path`

- [ ] **Step 1: 写会失败的测试**

```python
"""E2B_PURE_ROOTFS / E2B_PURE_ROOTFS_DIR: the shape switch and its landing spot."""
from __future__ import annotations

from gateway_common.paths import PURE_ROOTFS_DIR_NAME, is_reserved_platform_namespace
from envd_service.config import Settings


def test_the_switch_defaults_to_off(monkeypatch) -> None:
    monkeypatch.delenv("E2B_PURE_ROOTFS", raising=False)
    assert Settings().pure_rootfs == "off"


def test_the_switch_is_normalised(monkeypatch) -> None:
    monkeypatch.setenv("E2B_PURE_ROOTFS", " SYNTH ")
    assert Settings().pure_rootfs == "synth"


def test_the_root_dir_defaults_beside_the_sandbox_trees(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("E2B_PURE_ROOTFS_DIR", raising=False)
    monkeypatch.setenv("E2B_WORKSPACE_BASE", str(tmp_path / "base"))
    assert Settings().pure_rootfs_dir == (tmp_path / "base").resolve() / PURE_ROOTFS_DIR_NAME


def test_the_root_dir_can_be_pinned(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("E2B_PURE_ROOTFS_DIR", str(tmp_path / "elsewhere"))
    assert Settings().pure_rootfs_dir == (tmp_path / "elsewhere").resolve()


def test_the_namespace_is_reserved() -> None:
    assert PURE_ROOTFS_DIR_NAME == "_pure_rootfs"
    assert is_reserved_platform_namespace(PURE_ROOTFS_DIR_NAME) is True


def test_a_reserved_shaped_name_is_not_a_sandbox_tree(tmp_path) -> None:
    """The reserved list is a statement about names, not about records."""
    from gateway_common.paths import is_sandbox_workspace_dir

    tree = tmp_path / "sbx_keep"
    tree.mkdir()
    assert is_sandbox_workspace_dir(tree) is True
    assert is_sandbox_workspace_dir(tmp_path / PURE_ROOTFS_DIR_NAME) is False
```

- [ ] **Step 2: 跑它，确认失败**

Run: `tmp/testenv/bin/python -m pytest tests/unit/test_pure_rootfs_config.py -q`
Expected: FAIL with `ImportError: cannot import name 'PURE_ROOTFS_DIR_NAME' from 'gateway_common.paths'`

- [ ] **Step 3: 最小实现（`gateway_common/paths.py` + `envd_service/config.py` + `factory.py`）**

```python
# gateway_common/paths.py，紧挨 RUNTIME_DIR_NAME（126 行附近）：
#: The per-sandbox root of the pure shape (N16): an empty skeleton that the
#: sandbox's own mount namespace binds the host's system directories, the
#: workspace and the volumes into. It has to be traversable by the sandbox's own
#: host uid -- the binds and the ``chdir`` run inside the sandbox's user
#: namespace -- which rules out both the sandbox's own tree (it owns it and
#: could delete its own root) and ``_runtime/<id>`` (0700 and worker-owned, so a
#: 0700 parent cannot be traversed by the sandbox). Reserved like the rest, so
#: no scan and no park surface ever treats it as a tenant tree.
PURE_ROOTFS_DIR_NAME = "_pure_rootfs"

# 同文件 RESERVED_PLATFORM_NAMESPACES（225-241 行）里加一行：
        PURE_ROOTFS_DIR_NAME,

# envd_service/config.py，紧挨 _image_cache_dir（24-44 行）：
def _pure_rootfs_dir() -> Path:
    """``E2B_PURE_ROOTFS_DIR``, else ``<workspace base>/_pure_rootfs``."""
    raw = os.getenv("E2B_PURE_ROOTFS_DIR")
    if raw:
        return Path(raw).resolve()
    base = Path(os.getenv("E2B_WORKSPACE_BASE", "tmp/sandboxes")).resolve()
    return base / PURE_ROOTFS_DIR_NAME

# envd_service/config.py，紧挨 base_image（126 行）：
    #: Shape of the pure (no base image) sandbox's root (N16). ``off`` keeps
    #: N15's identity translation (the mediator's root is the host's "/");
    #: ``synth`` materializes a real root per sandbox -- a plain directory the
    #: sandbox's own mount namespace binds the host system directories, the
    #: workspace and the volumes into -- so the pure shape can use the fork's
    #: ``real_root`` as well. ``off`` is the default: this is a shape an
    #: operator flips, not a silent change to a running fleet.
    pure_rootfs: str = field(
        default_factory=lambda: os.getenv("E2B_PURE_ROOTFS", "off").strip().lower()
    )
    pure_rootfs_dir: Path = field(default_factory=_pure_rootfs_dir)

# envd_service/executors/factory.py 的 SandlockExecutor(...) 调用里（image_rootfs 旁）：
                pure_rootfs_dir=(
                    settings.pure_rootfs_dir
                    if settings.pure_rootfs == "synth"
                    else None
                ),
```

- [ ] **Step 4: 把新命名空间加进既有保留名单断言（`tests/unit/test_path_safety.py`）**

```python
    for reserved in (
        "_volumes",
        "_snapshots",
        "_migrate",
        "_templates",
        "_secrets",
        "_pure_rootfs",
        "snap_0040ce7e44f6365f",
        "bad.name",
    ):
        assert is_sandbox_workspace_dir(tmp_path / reserved) is False, reserved
```

- [ ] **Step 5: 跑测试，确认通过**

Run: `tmp/testenv/bin/python -m pytest tests/unit/test_pure_rootfs_config.py tests/unit/test_path_safety.py tests/unit/test_executor_factory_sandlock_health.py -q`
Expected: PASS（`test_pure_rootfs_config.py` 6 条全绿）

- [ ] **Step 6: 提交**

```bash
git add gateway_common/paths.py envd_service/config.py envd_service/executors/factory.py tests/unit/test_pure_rootfs_config.py tests/unit/test_path_safety.py
git commit -m "feat(config): E2B_PURE_ROOTFS (off|synth) and its reserved namespace"
```

> **注意（这一步不能省）**：`tests/security/conftest.py::route_b_sandbox` 是把安全套件接到
> 真实形态上的**唯一入口**（它的 docstring 写着 "no test hand-builds a shape the deployment
> does not have"）。它今天只镜像 `E2B_REAL_ROOT`，**不读** `E2B_PURE_ROOTFS` —— 不改它的话，
> `gateB-pure-rootfs.sh` 里的 pure 沙箱会静默落在 N15 形态上，lane 变成假绿。

### Task 5b: 把形状开关接进安全套件的入口

**Files:**
- Modify: `tests/security/conftest.py:216-258`（`route_b_sandbox` 的 fields）
- Test: `tests/unit/test_pure_rootfs_config.py`（追加两条）

**Interfaces:**
- Consumes: Task 5 的 `Settings.pure_rootfs` 语义（`"off"` / `"synth"`）
- Produces: `route_b_sandbox(None, None)` 在 `E2B_PURE_ROOTFS=synth` 下产出的 executor 的
  `_has_sandbox_root is True`、`_chroot_root` 指向 `<E2B_PURE_ROOTFS_DIR>/<sandbox_id>`

- [ ] **Step 1: 写会失败的测试**

```python
def test_the_security_helper_mirrors_the_shape_switch(monkeypatch, tmp_path) -> None:
    """A lane that sets the env must reach the executor the tests build.

    `route_b_sandbox` is the only place the security suite builds a shape, so a
    switch it does not read is a lane that silently tests the old shape.
    """
    from tests.security.conftest import route_b_sandbox

    monkeypatch.setenv("E2B_PURE_ROOTFS", "synth")
    monkeypatch.setenv("E2B_PURE_ROOTFS_DIR", str(tmp_path / "_pure_rootfs"))
    executor, _workspace = route_b_sandbox(None, None)
    try:
        assert executor._has_sandbox_root is True
        assert executor._chroot_root.startswith(str(tmp_path / "_pure_rootfs"))
    finally:
        executor.close()


def test_the_security_helper_stays_on_the_identity_root_by_default(
    monkeypatch, tmp_path
) -> None:
    from tests.security.conftest import route_b_sandbox

    monkeypatch.delenv("E2B_PURE_ROOTFS", raising=False)
    executor, _workspace = route_b_sandbox(None, None)
    try:
        assert executor._has_sandbox_root is False
        assert executor._chroot_root == "/"
    finally:
        executor.close()
```

- [ ] **Step 2: 跑它，确认失败**

Run: `tmp/testenv/bin/python -m pytest tests/unit/test_pure_rootfs_config.py -q -k security_helper`
Expected: FAIL on `assert executor._has_sandbox_root is True`（helper 没读这个 env）

- [ ] **Step 3: 最小实现（`tests/security/conftest.py`）**

```python
    # Mirror `Settings.real_root` (E2B_REAL_ROOT) so the suite can be run in
    # both shapes: the emulated root (default) and the real one the fork builds
    # with a mount namespace + pivot_root (see docs/chroot-workspace-exec.md).
    real_root = os.environ.get("E2B_REAL_ROOT", "0").strip() == "1"
    # ...and `Settings.pure_rootfs` (E2B_PURE_ROOTFS) for the same reason: the
    # pure shape has two roots now (N15's host root, N16's synthesized skeleton)
    # and a lane that sets the switch has to reach the executor this helper
    # builds, or it silently measures the other one.
    pure_rootfs = os.environ.get("E2B_PURE_ROOTFS", "off").strip().lower()
    pure_rootfs_dir = os.environ.get("E2B_PURE_ROOTFS_DIR") or str(
        sandbox_tmpdir(suffix="-pure-rootfs")
    )
    fields: dict = dict(
        workspace_dir=str(
            workspace if workspace is not None else sandbox_tmpdir(suffix="-ws")
        ),
        base_image=image,
        image_rootfs=rootfs,
        host_uid=host_uid,
        per_sandbox_uid=per_sandbox_uid,
        real_root=real_root,
        pure_rootfs_dir=(Path(pure_rootfs_dir) if pure_rootfs == "synth" else None),
        memory_mb=512,
        cpu_percent=100,
        disk_mb=1024,
        max_processes=64,
        max_open_files=4096,
        allow_internet_access=False,
        enable_network=False,
        sandbox_id=f"sbx_slot_{next(_slot_serial)}",
        route_b=RouteBConfig(
            mode="auto" if with_route_b else "off",
            uid_start=host_uid if host_uid is not None else SANDBOX_UID,
            uid_size=2,
            tmp_root=sandbox_tmpdir(suffix="-route-b"),
        ),
    )
    fields.update(overrides)
    executor = SandlockExecutor(**fields)
    return executor, Path(fields["workspace_dir"])
```

注意 `pure_rootfs_dir` 只对 `image is None` 的调用生效（`_synthetic_rootfs` 自己按
`base_image` / `image_rootfs` 判形态），所以这段改动对镜像形态的用例是恒等的。

- [ ] **Step 4: 跑测试 + 确认默认态没变**

Run: `tmp/testenv/bin/python -m pytest tests/unit/test_pure_rootfs_config.py -q`
Expected: PASS（8 条）

- [ ] **Step 5: 提交**

```bash
git add tests/security/conftest.py tests/unit/test_pure_rootfs_config.py
git commit -m "test(security): the suite's shape entry point mirrors E2B_PURE_ROOTFS"
```

### Task 6: 拆箱清账（`_pure_rootfs/<id>` 与 tree/runtime 同时消失）

**Files:**
- Modify: `envd_service/agent.py:1343-1360`（`_delete_sandbox_runtime` 里 `_runtime/<id>` 的 rmtree 之后）
- Test: `tests/unit/test_platform_disk.py`

**Interfaces:**
- Consumes: Task 5 的 `PURE_ROOTFS_DIR_NAME`
- Produces: 拆箱后 `<base>/_pure_rootfs/<id>` 不存在

- [ ] **Step 1: 写会失败的测试（加进 `tests/unit/test_platform_disk.py`，复用该文件既有的 registry / settings 构造 helper）**

```python
def test_teardown_removes_the_synthesized_root_too(tmp_path):
    """A leftover skeleton is a leftover *root*: it goes with the tree."""
    from envd_service import agent as agent_mod
    from gateway_common.paths import PURE_ROOTFS_DIR_NAME

    base = tmp_path / "base"
    sandbox_id = "sbx_synth_teardown"
    workspace = base / sandbox_id
    workspace.mkdir(parents=True)
    (workspace / "keep.txt").write_text("x", encoding="utf-8")
    skeleton = base / PURE_ROOTFS_DIR_NAME / sandbox_id
    (skeleton / "proc").mkdir(parents=True)

    registry = _registry_for(base)        # 本文件既有的构造，见同文件其它用例
    settings = _settings_for(base)        # 同上
    agent_mod._delete_sandbox_runtime(
        registry, settings, sandbox_id, workspace_dir=workspace
    )
    assert workspace.exists() is False
    assert skeleton.exists() is False
```

- [ ] **Step 2: 跑它，确认失败**

Run: `tmp/testenv/bin/python -m pytest tests/unit/test_platform_disk.py -q -k synthesized_root`
Expected: FAIL on `assert skeleton.exists() is False`（workspace 已经不在，骨架还在）

- [ ] **Step 3: 最小实现（`envd_service/agent.py`）**

```python
        shutil.rmtree(
            sandbox_runtime_dir(
                _registry_workspace_base(runtime_registry, settings), sandbox_id
            ),
            ignore_errors=True,
        )
        # The pure shape's synthesized root lives beside the tree for the same
        # reason the runtime record does: it must be traversable by the
        # sandbox's own host uid, which neither the tree itself (the sandbox
        # owns it) nor `_runtime/<id>` (0700, worker-owned) can be. It goes with
        # the teardown like everything else -- a skeleton whose sandbox is gone
        # is a root nobody owns.
        shutil.rmtree(
            _registry_workspace_base(runtime_registry, settings)
            / PURE_ROOTFS_DIR_NAME
            / sandbox_id,
            ignore_errors=True,
        )
```

（`PURE_ROOTFS_DIR_NAME` 加进 `envd_service/agent.py` 顶部那条 `from gateway_common.paths import ...`。）

- [ ] **Step 4: 跑测试**

Run: `tmp/testenv/bin/python -m pytest tests/unit/test_platform_disk.py tests/unit/test_path_safety.py -q`
Expected: PASS（新用例绿；`test_platform_disk.py` 其余逐条与改动前相同）

- [ ] **Step 5: 确认"孤儿骨架"与拆箱是同一把扫帚（只读核对，不改代码）**

Run: `grep -n "def _delete_sandbox_runtime" envd_service/agent.py; grep -n "_delete_sandbox_runtime" envd_service/agent.py`
Expected: 命中 `1190`（定义）、拆箱调用点与 `2171`（reconcile 收孤儿的调用点）—— 三个入口共用一处实现，所以 Step 3 的改动对"worker 重启后收孤儿"同样生效

- [ ] **Step 6: 提交**

```bash
git add envd_service/agent.py tests/unit/test_platform_disk.py
git commit -m "fix(agent): tear the synthesized root down with the sandbox"
```

### Task 7: fork 侧合成根端到端用例（不新增参数，先证不改产品代码）

**Files:**
- Modify: `third_party/sandlock/crates/sandlock-core/tests/integration/test_instance_chroot.rs`（文件末尾追加）
- Modify: `third_party/sandlock/docs/test-baseline.md:336`
- Test: `third_party/sandlock/crates/sandlock-core/tests/integration/test_instance_chroot.rs`

**Interfaces:**
- Consumes: Task 1 的步骤顺序结论、Task 3 的 `/dev` 结论
- Produces: 被 pin 住的判据 —— "普通目录 + bind + `real_root(true)`"下宿主独有路径不可见、绝对与相对 exec 都能跑、骨架里的 `/proc` 是空目录

- [ ] **Step 1: 写用例**

```rust
/// N16 (E2B `docs/superpowers/plans/2026-09-26-pure-shape-synthetic-rootfs.md`):
/// the pure shape's root is a *plain directory* the sandbox binds the host's
/// system directories into -- not an extracted image, and not a tmpfs (the
/// shipped worker profile refuses every `mount` whose fstype is not NULL).
///
/// The fork has no "image" concept on this path at all: `real_root` takes a root
/// path and a `(virtual, host)` mount list, so nothing here should need a
/// production change. The case exists to make that claim falsifiable -- each
/// assertion is one place an image assumption could have hidden.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn test_a_plain_directory_root_pivots_and_hides_the_host() {
    let base = temp_dir("synth-root");
    let rootfs = base.join("skeleton");
    let work = base.join("work");
    let host_only = base.join("host-only");
    std::fs::create_dir_all(&host_only).expect("create host-only dir");
    std::fs::write(host_only.join("SECRET"), b"secret\n").expect("write sentinel");
    for dir in ["usr/bin", "dev", "proc", "etc", "tmp"] {
        std::fs::create_dir_all(rootfs.join(dir)).expect("create skeleton dir");
    }
    std::fs::create_dir_all(&work).expect("create host work dir");
    std::fs::write(work.join("hello.txt"), b"hello\n").expect("write workspace file");
    let helper = helper_binary();
    let dest = rootfs.join("usr/bin/rootfs-helper");
    std::fs::hard_link(&helper, &dest)
        .or_else(|_| std::fs::copy(&helper, &dest).map(|_| ()))
        .expect("install rootfs-helper into the skeleton");

    let euid = unsafe { libc::geteuid() };
    let egid = unsafe { libc::getegid() };
    let mut builder = Sandbox::builder()
        .chroot(&rootfs)
        .real_root(true)
        .user(euid, egid)
        .fs_read("/usr")
        .fs_mount("/work", &work)
        .fs_mount("/dev", "/dev")
        .fs_write("/work")
        .cwd("/work");
    builder.userns_self_map = true;
    let policy = builder.build().expect("synthetic-root policy builds");
    let mut inst = SandboxInstance::launch_exec_only(policy).await.expect("launch");

    // ① absolute exec through the bound host directory
    let (status, stdout, stderr) =
        exec_capture(&mut inst, &["/usr/bin/rootfs-helper", "pwd"]).await;
    assert_eq!(status, ExitStatus::Code(0), "absolute exec failed: {stderr}");
    assert_eq!(stdout, "/work\n");
    // ② relative exec -- the symptom of a self-bind taken in the wrong order
    let (status, stdout, stderr) =
        exec_capture(&mut inst, &["rootfs-helper", "cat", "hello.txt"]).await;
    assert_eq!(status, ExitStatus::Code(0), "relative exec failed: {stderr}");
    assert_eq!(stdout, "hello\n");
    // ③ the host-only path is gone: this is what the pivot bought
    let (status, _stdout, _stderr) = exec_capture(
        &mut inst,
        &["rootfs-helper", "stat", &host_only.display().to_string()],
    )
    .await;
    assert_eq!(
        status,
        ExitStatus::Code(1),
        "the host-only path is still reachable"
    );
    // ④ the skeleton's own /proc is an empty directory, not a missing one
    let (status, stdout, stderr) = exec_capture(&mut inst, &["rootfs-helper", "ls", "/proc"]).await;
    assert_eq!(status, ExitStatus::Code(0), "ls /proc failed: {stderr}");
    assert_eq!(stdout, "");

    inst.shutdown().await.expect("shutdown");
    cleanup(&rootfs);
    cleanup(&base);
}
```

- [ ] **Step 2: 跑它（fork 的门禁容器，仓库挂 `/src`）**

Run: `cd third_party/sandlock && chmod -R a+rwX tmp && docker run --privileged --rm -v "$PWD":/src -w /src sandlock-dev:latest cargo test -p sandlock-core --offline --test integration test_a_plain_directory_root_pivots_and_hides_the_host -- --test-threads=1 --nocapture`
Expected: PASS。红了的处理规则：③/④ 红 ⇒ 骨架内容规则错了，回 Task 2/Task 3 改结论（不是改断言）；①/② 红 ⇒ `realroot::build` 对"根不是镜像"有隐含假设，**这时才允许动 `realroot.rs`**，并在 commit message 里写明是哪一步、哪条实测

- [ ] **Step 3: 同步 fork 的套件基线（不改会被判"数量漂移"）**

Run: `cd third_party/sandlock && sed -n '336p' docs/test-baseline.md`
Expected: 输出以 `core_integ = 560 #` 开头；把它改成

```
core_integ = 561 # 2026-09-26: 560 -> 561, +1: N16 (E2B pure shape's synthesized root)
```

- [ ] **Step 4: 跑 fork 的非 root 相位确认没有连带**

Run: `cd third_party/sandlock && chmod -R a+rwX tmp && docker run --privileged --rm -v "$PWD":/src -w /src sandlock-dev:latest sh scripts/test-all.sh`
Expected: 每个 label 的通过数与 `docs/test-baseline.md` **相等**（`core_lib = 913`、`core_integ = 561`、`ffi = 104`、`cli = 98`、`supervise = 55`），输出里没有 `suite FAILED`

- [ ] **Step 5: 提交（先 submodule，后父仓指针）**

```bash
git -C third_party/sandlock add crates/sandlock-core/tests/integration/test_instance_chroot.rs docs/test-baseline.md
git -C third_party/sandlock commit -m "test(chroot): a plain-directory root pivots and hides the host (N16)"
git add third_party/sandlock
git commit -m "chore(fork): bump the sandlock submodule for the synthesized-root case"
```

### Task 8: 重建 wheel（fork 的改动必须进 lane 才可见）

**Files:**
- Test: `deploy/scripts/build-sandlock-wheels.sh` 的输出
- Test: `tmp/k0s/build-n16-wheel.log`

**Interfaces:**
- Consumes: Task 7 的 submodule commit
- Produces: 装上 fork HEAD 的 wheel，Task 9–13 的 lane 都在它上面跑

- [ ] **Step 1: 记录起点（哪个 fork commit 进 wheel）**

Run: `git -C third_party/sandlock rev-parse --short HEAD`
Expected: 打印 Task 7 的 commit 短哈希

- [ ] **Step 2: 重建**

Run: `deploy/scripts/build-sandlock-wheels.sh 2>&1 | tee tmp/k0s/build-n16-wheel.log | tail -5`
Expected: 退出码 0，末几行含 wheel 落盘路径与构建产物名（`sandlock-*.whl`）

- [ ] **Step 3: 确认 lane 用的就是这份 wheel**

Run: `grep -n "sandlock-\|\.whl" tmp/k0s/build-n16-wheel.log | tail -10`
Expected: 命中 wheel 文件名与构建命令，目录与 `deploy/scripts/build-sandlock-wheels.sh` 的目标一致（不是另一份陈旧的 wheel）

- [ ] **Step 4: 提交（wheel 产物不入库，只记录日志）**

```bash
git add tmp/k0s/build-n16-wheel.log
git commit -m "chore(wheel): rebuild sandlock wheels for the N16 probe lanes"
```

### Task 9: 纯形态 lane + 工作负载普查（P4：三形态逐字节 diff）

**Files:**
- Create: `deploy/scripts/acceptance/gateB-pure-rootfs.sh`
- Create: `deploy/scripts/acceptance/probe-pure-workload-census.py`
- Test: `tmp/k0s/pure-workload-census.log`

**Interfaces:**
- Consumes: Task 5 的 `E2B_PURE_ROOTFS=synth`、Task 8 的 wheel
- Produces: `sh deploy/scripts/acceptance/gateB-pure-rootfs.sh <0|1> <log> [pytest 目标...]`（pure + 合成根 + 指定 `E2B_REAL_ROOT` 的 lane）；一份三形态三元组 diff 清单

- [ ] **Step 1: 写 lane runner（`gateB-full.sh` 的孪生，只多两个 env 与一个可选目标）**

```sh
#!/bin/sh
# gate B 的孪生：pure 形态 + 合成根（E2B_PURE_ROOTFS=synth）。
# 用法：gateB-pure-rootfs.sh <E2B_REAL_ROOT 0|1> <log> [pytest 目标...]
set -eu
cd /Users/polus/project/ai/sandlock-e2b
real_root="$1"
log="$2"
shift 2
target="${*:-tests}"
SECCOMP_PROFILE="$(pwd)/deploy/seccomp/sandlock-worker.json"
docker run --rm --init --network host \
    --cap-drop ALL \
    --cap-add SYS_ADMIN --cap-add SYS_PTRACE --cap-add NET_BIND_SERVICE --cap-add NET_RAW \
    --cap-add SYS_CHROOT --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER --cap-add FSETID \
    --cap-add KILL --cap-add SETGID --cap-add SETUID --cap-add SETPCAP --cap-add AUDIT_WRITE \
    --cap-add SETFCAP --cap-add NET_ADMIN \
    --security-opt seccomp="$SECCOMP_PROFILE" \
    --security-opt apparmor=unconfined \
    -e E2B_HOST_PROJECT="$(pwd)" \
    -e E2B_TEST_STRICT_SKIPS=1 \
    -e E2B_BASE_IMAGE= \
    -e E2B_PURE_ROOTFS=synth \
    -e E2B_MAX_CONCURRENT_COMMANDS_PER_SANDBOX=2 \
    -e E2B_REGISTRY_MIRRORS=registry-1.docker.io=127.0.0.1:5080 \
    -e E2B_REAL_ROOT="$real_root" \
    -v "$HOME/.orbstack/run/docker.sock:/var/run/docker.sock" \
    -v "$(pwd):/workspace" -w /workspace \
    e2b-sandlock-test:latest \
    python -m pytest $target -q -p no:cacheprovider > "$log" 2>&1
```

- [ ] **Step 2: 跑一份最小烟测，确认 lane 本身通**

Run: `sh deploy/scripts/acceptance/gateB-pure-rootfs.sh 1 tmp/k0s/pure-rootfs-smoke.log tests/unit/test_pure_rootfs_shape.py && tail -2 tmp/k0s/pure-rootfs-smoke.log`
Expected: 末行形如 `7 passed in ...s`，无 failed / error。若报 `E2B_REAL_ROOT is on, but this worker cannot build a sandbox root: ...`，说明 Task 8 的 wheel 没进 lane，回 Task 8

- [ ] **Step 3: 写普查脚本（同一份代表性工作负载在三形态下逐字节比较）**

```python
#!/usr/bin/env python3
"""同一组命令在三种形态下的 (rc, stdout, stderr)，逐字节 diff。

形态：① pure + N15（E2B_PURE_ROOTFS=off，根 = 宿主 /）
      ② pure + 合成根 + E2B_REAL_ROOT=0（模拟根 + 中介）
      ③ pure + 合成根 + E2B_REAL_ROOT=1（真根）
每条差异都要能归因到"路径缺失"或"errno 变化"，归不到就是 bug。

用 `tests/security/conftest.py` 的 `route_b_sandbox` / `run_sh`：那是本仓库把形状接到
真实 worker 形态上的唯一入口（它镜像 E2B_REAL_ROOT 与 E2B_PURE_ROOTFS），所以这个脚本
量的就是产品形态，不是另搭的一套。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from tests.security.conftest import route_b_sandbox, run_sh

COMMANDS = [
    "echo hi",
    "python3 -c 'print(1+1)'",
    "pwd && pwd -P",
    "ls /",
    "ls /usr/bin | head -3",
    "cat /etc/hosts",
    "cat /proc/version",
    "ls /proc",
    "nproc",
    "cat /etc/passwd",
    "stat -c %i /usr/bin/python3",
    "head -c 4 /dev/urandom | wc -c",
    "echo x > /dev/null && echo ok",
    "echo x > /tmp/n16-probe && cat /tmp/n16-probe",
    "ls /dev | head -3",
    # The four `/proc/self/fd` symlinks the whole-tree `/dev` bind preserves *in shape*.
    # The probe lane only ever measured the bare answer in a synthesized tree (they
    # dangle there, because the skeleton's `/proc` is an empty directory); whether they
    # resolve end-to-end is a `/proc`-synthesis question -- so it gets an accept command
    # here, in the census, instead of staying an open question in a report.
    "test -e /dev/fd; echo fd=$?",
    "test -e /dev/stdout; echo stdout=$?",
]


async def run_shape(label: str) -> dict[str, list]:
    executor, workspace = route_b_sandbox(None, None)
    out: dict[str, list] = {}
    try:
        for command in COMMANDS:
            code, stdout, stderr = await run_sh(executor, workspace, command)
            out[command] = [
                code,
                stdout.decode("utf-8", "replace"),
                stderr.decode("utf-8", "replace"),
            ]
    finally:
        executor.close()
    return out


async def main() -> int:
    base = Path(os.environ["E2B_HOST_PROJECT"]) / "tmp/k0s/scratch/census"
    base.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("E2B_PURE_ROOTFS_DIR", str(base / "_pure_rootfs"))
    shapes = {
        "n15": {"E2B_PURE_ROOTFS": "off", "E2B_REAL_ROOT": "0"},
        "synth-emulated": {"E2B_PURE_ROOTFS": "synth", "E2B_REAL_ROOT": "0"},
        "synth-realroot": {"E2B_PURE_ROOTFS": "synth", "E2B_REAL_ROOT": "1"},
    }
    results: dict[str, dict] = {}
    for label, env in shapes.items():
        for key, value in env.items():
            os.environ[key] = value
        results[label] = await run_shape(label)
    reference = results["n15"]
    diffs = 0
    for label in ("synth-emulated", "synth-realroot"):
        for command, triple in results[label].items():
            if triple != reference[command]:
                diffs += 1
                print(
                    f"DIFF [{label}] {command}\n  n15  = {reference[command]}\n"
                    f"  this = {triple}"
                )
    print(f"commands={len(COMMANDS)} shapes={len(shapes)} diffs={diffs}")
    (base / "census.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
```

- [ ] **Step 4: 跑普查（在 lane 里跑，三种形态都在位）**

Run: `sh deploy/scripts/acceptance/gateB-pure-rootfs.sh 1 tmp/k0s/pure-rootfs-census-lane.log tests/unit/test_pure_rootfs_shape.py && docker run --rm --network host --cap-drop ALL --cap-add SYS_ADMIN --security-opt seccomp="$(pwd)/deploy/seccomp/sandlock-worker.json" --security-opt apparmor=unconfined -e E2B_HOST_PROJECT="$(pwd)" -e E2B_BASE_IMAGE= -v "$(pwd):/workspace" -w /workspace e2b-sandlock-test:latest python deploy/scripts/acceptance/probe-pure-workload-census.py | tee tmp/k0s/pure-workload-census.log`
Expected: 末行 `commands=17 shapes=3 diffs=<n>`（17 = 原 15 条 + 四条软链的两条 accept）；`diffs=0` 直接进 Task 10；`diffs>0` 时把每条 `DIFF [...]` 行分类到"路径缺失 / errno 变化"，分类结果写进 Task 11 —— 不接受"看一眼觉得没事"

- [ ] **Step 5: 提交**

```bash
git add deploy/scripts/acceptance/gateB-pure-rootfs.sh deploy/scripts/acceptance/probe-pure-workload-census.py tmp/k0s/pure-workload-census.log tmp/k0s/pure-rootfs-census-lane.log
git commit -m "probe(pure): three-shape workload census for the synthesized root"
```

### Task 10: pure + 合成根的 security 两态

**Files:**
- Modify: `tests/unit/test_pure_rootfs_shape.py`（追加一条）
- Test: `tmp/k0s/pure-rootfs-sec-realroot0.log`、`tmp/k0s/pure-rootfs-sec-realroot1.log`

**Interfaces:**
- Consumes: Task 9 的 lane
- Produces: 两态（`E2B_REAL_ROOT=0/1`）的 security 结果 + "平台状态不再可见"这条新增收益的用例

- [ ] **Step 1: 跑两态 security**

Run: `sh deploy/scripts/acceptance/gateB-pure-rootfs.sh 0 tmp/k0s/pure-rootfs-sec-realroot0.log tests/security && sh deploy/scripts/acceptance/gateB-pure-rootfs.sh 1 tmp/k0s/pure-rootfs-sec-realroot1.log tests/security && tail -1 tmp/k0s/pure-rootfs-sec-realroot0.log && tail -1 tmp/k0s/pure-rootfs-sec-realroot1.log`
Expected: 两行都是 `... passed, ... skipped, ... xfailed`、**没有 failed / error**。参照系是镜像形态的 `deploy/scripts/arm-lane/x86-security.sh`（`44 passed / 1 skipped / 3 xfailed`）—— pure 形态 skip 更多属正常，failed 必须 0

- [ ] **Step 2: 捞现场证据（正例验收：越界路径变 ENOENT 而不是被拒）**

Run: `grep -n "Permission denied\|No such file" tmp/k0s/pure-rootfs-sec-realroot1.log | head -20`
Expected: 命中 `tests/security/test_real_root_denials.py` 那组精确三元组（`cat /proc/kcore` = `Permission denied`、`ls /sys` = `Permission denied`）；把 `=1` 与 `=0` 两份日志里**同一条**输出的差异逐条抄进 Task 11

- [ ] **Step 3: 追加"N27 在 pure 上的残留在合成根下消失"的用例**

```python
def test_the_synthetic_root_hides_the_platform_state(tmp_path: Path) -> None:
    """`stat <base>/_runtime` used to answer EACCES for the pure shape.

    With a synthesized root the base is not in the sandbox's tree at all, so the
    residue N27 documented for this shape is gone rather than merely denied.
    Evidence: tmp/k0s/pure-rootfs-sec-realroot1.log.
    """
    base = tmp_path / "base"
    workspace = base / "sbx_n27"
    workspace.mkdir(parents=True)
    (base / "_runtime" / "sbx_n27").mkdir(parents=True)
    ex = SandlockExecutor(
        workspace_dir=str(workspace),
        base_image=None,
        image_rootfs=None,
        sandbox_id="sbx_n27",
        pure_rootfs_dir=str(base / "_pure_rootfs"),
        memory_mb=512,
        cpu_percent=100,
        disk_mb=1024,
        max_processes=64,
        max_open_files=4096,
        allow_internet_access=False,
        enable_network=False,
    )
    root = ex._synthetic_rootfs
    assert root is not None
    ex._materialize_root(root)
    assert (base / "_runtime" / "sbx_n27").is_dir() is True
    assert "_runtime" not in {p.name for p in root.iterdir()}
    assert root.joinpath("_runtime").exists() is False
```

- [ ] **Step 4: 跑它，确认通过**

Run: `tmp/testenv/bin/python -m pytest tests/unit/test_pure_rootfs_shape.py -q`
Expected: PASS（8 条）

- [ ] **Step 5: 提交**

```bash
git add tests/unit/test_pure_rootfs_shape.py tmp/k0s/pure-rootfs-sec-realroot0.log tmp/k0s/pure-rootfs-sec-realroot1.log
git commit -m "test(pure): security suite in both real-root states, N27 residue gone"
```

### Task 11: 契约迁移（别名 cwd + 越界 errno）

**Files:**
- Modify: `tests/contract/test_shared_volume_relative_cwd.py:7-15`、`:35-43`
- Modify: `tests/contract/test_pure_shape_workspace_ownership.py:1-33`、`:85-90`
- Create: `tests/security/test_pure_root_errno_contract.py`
- Test: `tmp/k0s/pure-rootfs-alias-realroot1.log`、`tmp/k0s/pure-rootfs-errno-realroot0.log`、`tmp/k0s/pure-rootfs-errno-realroot1.log`

**Interfaces:**
- Consumes: Task 9 的 lane、Task 10 的差异清单
- Produces: "pure 没有 `/home/user` 别名"这句**过期前提**从测试与文档里消失；越界路径的 errno 变成写明的契约

- [ ] **Step 1: 先证伪那条 skip 理由（N15 之后 pure 已经有 `/home/user` 挂载了）**

Run: `sh deploy/scripts/acceptance/gateB-pure-rootfs.sh 1 tmp/k0s/pure-rootfs-contracts-before.log tests/contract/test_shared_volume_relative_cwd.py tests/contract/test_pure_shape_workspace_ownership.py; tail -1 tmp/k0s/pure-rootfs-contracts-before.log`
Expected: `test_shared_volume_relative_cwd.py` 的用例被 `_IMAGE_ROOTFS_ONLY` 整文件 skip（理由是 `image-rootfs contract requires a non-empty E2B_BASE_IMAGE`）—— 记下 skipped 的数量作为迁移前的基线

- [ ] **Step 2: 迁移 skip 判据（从"要镜像"改成"要一个可进入的根"）**

```python
#: Both shapes have a root to be confined to now: the image's rootfs, or the
#: pure shape's synthesized skeleton (N16, E2B_PURE_ROOTFS=synth). The old skip
#: reason ("the pure shape runs with the host workspace cwd and has no
#: /home/user alias") stopped being true when N15 put the workspace under both
#: aliases in *both* shapes.
_ROOTED_SHAPE_ONLY = pytest.mark.skipif(
    not (
        os.environ.get("E2B_BASE_IMAGE")
        or os.environ.get("E2B_PURE_ROOTFS") == "synth"
    ),
    reason=(
        "the workspace aliases are resolved by the mount table, which needs a "
        "sandbox root: set E2B_BASE_IMAGE (image shape) or "
        "E2B_PURE_ROOTFS=synth (pure shape)"
    ),
)
```

同文件把 `@_IMAGE_ROOTFS_ONLY` 全部换成 `@_ROOTED_SHAPE_ONLY`，并改写文件头那段 `Shape scope`（它现在还写着"pure 形态忽略 fs_mounts"，那是 N15 之前的事实）；`tests/contract/test_pure_shape_workspace_ownership.py` 的文件头同段说明一并核对（`_NO_BASE_IMAGE` 的判据本身不变 —— 那条契约本来就只针对 pure）。

- [ ] **Step 3: 跑迁移后的契约（有根的两档各一次）**

Run: `sh deploy/scripts/acceptance/gateB-pure-rootfs.sh 1 tmp/k0s/pure-rootfs-alias-realroot1.log tests/contract/test_shared_volume_relative_cwd.py && tail -1 tmp/k0s/pure-rootfs-alias-realroot1.log`
Expected: `5 passed`（含 `test_relative_paths_resolve_from_both_workspace_aliases` 的 `hello\nhello\nhello\n` 与 `pwd && pwd -P` = `/home/user\n/home/user\n`），无 skipped

- [ ] **Step 4: 确认镜像形态没被改坏**

Run: `sh deploy/scripts/acceptance/gateA-full.sh tmp/k0s/n16-gateA-alias.log; grep -c "passed" tmp/k0s/n16-gateA-alias.log; tmp/testenv/bin/python -m pytest tests/contract/test_shared_volume_relative_cwd.py -q`
Expected: gate A 的汇总行仍是 `1772 passed, 6 skipped, 3 xfailed`；本机（既无 `E2B_BASE_IMAGE` 也无 `E2B_PURE_ROOTFS`）整文件 skipped，行数与迁移前一致

- [ ] **Step 5: 把越界路径的 errno 写成明文契约**

```python
"""The errno contract for a path outside every grant, in the rooted pure shape.

N15 fixed "authorised-outside answers EACCES with one diagnostic line"
(docs/pure-shape-decision.md §6). The synthesized root splits that sentence in
two, and the split is the whole point of the route: a path that is inside the
skeleton but outside the allow-list answers EACCES, while a path that is in no
tree at all answers ENOENT -- the second class used to be answered by the *host*.

Run through `route_b_sandbox(None, None)` so the shape comes from the same entry
point every other security case uses (tests/security/conftest.py mirrors
E2B_REAL_ROOT and E2B_PURE_ROOTFS).
"""
from __future__ import annotations

import pytest

from tests.security.conftest import require_mediation_capable, route_b_sandbox, run_sh


@pytest.mark.usefixtures("require_sandlock")
async def test_the_errno_for_a_path_outside_every_grant_is_pinned():
    executor, workspace = route_b_sandbox(None, None)
    try:
        require_mediation_capable(executor)
        assert await run_sh(executor, workspace, "cat /etc/passwd") == (
            1,
            b"",
            b"cat: /etc/passwd: Permission denied\n",
        )
        assert await run_sh(executor, workspace, "cat /src/host-only/SECRET") == (
            1,
            b"",
            b"cat: /src/host-only/SECRET: No such file or directory\n",
        )
        assert await run_sh(executor, workspace, "stat /src") == (
            1,
            b"",
            b"stat: cannot statx '/src': No such file or directory\n",
        )
    finally:
        executor.close()
```

三元组就是 `run_sh` 的返回值本身（`code, stdout, stderr`，不 decode、不过滤），与同目录
`tests/security/test_real_root_denials.py` 的写法一致；`/src` 是 lane 里宿主独有而沙箱树里
不存在的路径（镜像形态下它本来就被 Landlock 拒成 EACCES，合成根下变成 ENOENT —— 正是本条
契约要钉的那处变化）。

- [ ] **Step 6: 跑它，确认通过（两态）**

Run: `sh deploy/scripts/acceptance/gateB-pure-rootfs.sh 0 tmp/k0s/pure-rootfs-errno-realroot0.log tests/security/test_pure_root_errno_contract.py && sh deploy/scripts/acceptance/gateB-pure-rootfs.sh 1 tmp/k0s/pure-rootfs-errno-realroot1.log tests/security/test_pure_root_errno_contract.py && tail -1 tmp/k0s/pure-rootfs-errno-realroot0.log && tail -1 tmp/k0s/pure-rootfs-errno-realroot1.log`
Expected: 两态都 `1 passed` 且三重元逐字节相同 —— 这就是 D4"模拟形态必须保留"的落地证据

- [ ] **Step 7: 提交**

```bash
git add tests/contract/test_shared_volume_relative_cwd.py tests/contract/test_pure_shape_workspace_ownership.py tests/security/test_pure_root_errno_contract.py tmp/k0s/pure-rootfs-alias-realroot1.log tmp/k0s/pure-rootfs-errno-realroot0.log tmp/k0s/pure-rootfs-errno-realroot1.log tmp/k0s/pure-rootfs-contracts-before.log
git commit -m "test(contract): both rooted shapes resolve the aliases; pin the errno contract"
```

### Task 12: pause/resume 在合成根 + 真根下复验（P6）

**Files:**
- Create: `deploy/scripts/acceptance/probe-pure-restore-synthroot.sh`
- Test: `tmp/k0s/pure-rootfs-restore.log`

**Interfaces:**
- Consumes: Task 9 的 lane、`tests/contract/test_pause_resume_sandlock.py`
- Produces: 一句"恢复成立 / 被显式拒绝"的原文（不许 10 s 超时）

- [ ] **Step 1: 写探针（两态都跑，日志不过滤）**

```sh
#!/bin/sh
# pure + 合成根下的 pause/resume：restore stub 从"根内"变成"根外"，正是
# docs/chroot-workspace-exec.md §11.6.1 那条 fd 路线要覆盖的新情形。
# 用法：probe-pure-restore-synthroot.sh <log>
set -eu
cd /Users/polus/project/ai/sandlock-e2b
log="$1"
: > "$log"
for real_root in 0 1; do
    printf '===== E2B_REAL_ROOT=%s =====\n' "$real_root" >> "$log"
    sh deploy/scripts/acceptance/gateB-pure-rootfs.sh "$real_root" \
        "tmp/k0s/pure-rootfs-restore-$real_root.log" \
        tests/contract/test_pause_resume_sandlock.py >> "$log" 2>&1 || true
    tail -1 "tmp/k0s/pure-rootfs-restore-$real_root.log" >> "$log"
done
```

- [ ] **Step 2: 跑它**

Run: `sh deploy/scripts/acceptance/probe-pure-restore-synthroot.sh tmp/k0s/pure-rootfs-restore.log && cat tmp/k0s/pure-rootfs-restore.log`
Expected: 两段各一行 pytest 汇总，都是 `... passed` 或 `... skipped`（**没有 failed / error**）。若出现 `restore stub never signalled READY within 10000ms` 或 `No such file or directory (os error 2)`，那是 `docs/chroot-workspace-exec.md` §11 的"立即拒绝"路径：把它作为**前置条件未满足**写进 `docs/checkpoint-restore-e2b-half.md`，并且**先别合**这条纯形态路径

- [ ] **Step 3: 盯住那条守卫（envd 仍然不用 fork 的 checkpoint/restore）**

Run: `tmp/testenv/bin/python -m pytest tests/unit/test_checkpoint_restore_unused.py -q`
Expected: PASS

- [ ] **Step 4: 提交**

```bash
git add deploy/scripts/acceptance/probe-pure-restore-synthroot.sh tmp/k0s/pure-rootfs-restore.log
git commit -m "probe(pure): pause/resume under the synthesized root, both real-root states"
```

### Task 13: 两态全量验收

**Files:**
- Test: `tmp/k0s/n16-gateA.log`、`tmp/k0s/n16-gateB-off.log`、`tmp/k0s/n16-gateB-synth-realroot0.log`、`tmp/k0s/n16-gateB-synth-realroot1.log`、`tmp/k0s/n16-phase2.log`

**Interfaces:**
- Consumes: Task 8 的 wheel、Task 9 的 lane
- Produces: 一张"逐档与基线相等或更好"的验收表

- [ ] **Step 1: gate A（镜像形态，不能被这次改动碰到）**

Run: `sh deploy/scripts/acceptance/gateA-full.sh tmp/k0s/n16-gateA.log && tail -1 tmp/k0s/n16-gateA.log`
Expected: `1772 passed, 6 skipped, 3 xfailed` 且 `0 failed`（与 `docs/pure-shape-decision.md` §6 逐字相同）

- [ ] **Step 2: gate B（pure，开关关 = N15 的今天）**

Run: `sh deploy/scripts/acceptance/gateB-full.sh tmp/k0s/n16-gateB-off.log && tail -1 tmp/k0s/n16-gateB-off.log`
Expected: `1765 passed, 13 skipped, 3 xfailed` 且 `0 failed`（默认 `off` 时与今天逐字相同 —— 这是"可回退"的证明）

- [ ] **Step 3: gate B 的孪生（pure + 合成根）两态**

Run: `sh deploy/scripts/acceptance/gateB-pure-rootfs.sh 0 tmp/k0s/n16-gateB-synth-realroot0.log && sh deploy/scripts/acceptance/gateB-pure-rootfs.sh 1 tmp/k0s/n16-gateB-synth-realroot1.log && tail -1 tmp/k0s/n16-gateB-synth-realroot0.log && tail -1 tmp/k0s/n16-gateB-synth-realroot1.log`
Expected: 两态都 `0 failed`；skipped 允许多于 gate B（形态相关的 skip 是设计的一部分），passed 数必须 ≥ `1765`

- [ ] **Step 4: phase 2（非 root worker）与 fork 全量**

Run: `sh deploy/scripts/acceptance/phase2.sh tmp/k0s/n16-phase2.log && tail -1 tmp/k0s/n16-phase2.log`
Expected: `57 passed, 1 skipped` 且 `0 failed`

Run: `cd third_party/sandlock && chmod -R a+rwX tmp && docker run --privileged --rm -v "$PWD":/src -w /src sandlock-dev:latest sh scripts/test-all.sh 2>&1 | tail -8`
Expected: 每个 label 的计数与 `docs/test-baseline.md` 相等（`core_integ = 561`），无 `suite FAILED`

- [ ] **Step 5: 记录验收表并提交**

```
| 档 | 命令 | 结果 | 基线 | 判定 |
|---|---|---|---|---|
| gate A | deploy/scripts/acceptance/gateA-full.sh | ... | 1772/6/3/0 | 相等 |
| gate B（off） | deploy/scripts/acceptance/gateB-full.sh | ... | 1765/13/3/0 | 相等 |
| gate B + 合成根（REAL_ROOT=0） | deploy/scripts/acceptance/gateB-pure-rootfs.sh 0 | ... | — | 0 failed |
| gate B + 合成根（REAL_ROOT=1） | deploy/scripts/acceptance/gateB-pure-rootfs.sh 1 | ... | — | 0 failed |
| phase 2 | deploy/scripts/acceptance/phase2.sh | ... | 57/1/0 | 相等 |
| security 两态 | tmp/k0s/pure-rootfs-sec-realroot{0,1}.log | ... | 镜像档 44/1/3 | 0 failed |
| fork | third_party/sandlock/scripts/test-all.sh | ... | core_integ 561 | 相等 |
| 本机单测 | tmp/testenv/bin/python -m pytest tests/unit -q | ... | 16 failed / 1171 passed | 相等 |
```

```bash
git add tmp/k0s/n16-gateA.log tmp/k0s/n16-gateB-off.log tmp/k0s/n16-gateB-synth-realroot0.log tmp/k0s/n16-gateB-synth-realroot1.log tmp/k0s/n16-phase2.log tmp/k0s/n16-gateA-alias.log
git commit -m "chore(lanes): the N16 acceptance table (image, pure off, pure+synth both states)"
```

### Task 14: 文档收口（这一轮的隐性成本就出在文档漂移上）

**Files:**
- Modify: `docs/n14-retire-the-emulation.md:190-260`（§5 / §5.1 追加"已选路线与实测"）
- Modify: `docs/pure-shape-decision.md:99-161`（§6 之后追加 N16 段）
- Modify: `docs/open-issues.md:40-41`（N14 / N15 行）
- Modify: `docs/task-backlog.md:113`（N14 行）
- Modify: `docs/chroot-workspace-exec.md:173-198`（§5.3 的交叉引用）

**Interfaces:**
- Consumes: Task 13 的验收表
- Produces: "pure 的根是 `/`"这句话在仓库里要么被改写、要么被明确标注为条件成立；两处 fork 特例的现状写明

- [ ] **Step 1: 把还成立/已过期的"pure 根 = /"逐条捞出来**

Run: `rg -n "identity 翻译|identity translation|chroot_root=\"/\"|chroot_root = \"/\"" docs/ envd_service/ tests/ --glob '!**/__pycache__/**'`
Expected: 命中列表（`docs/pure-shape-decision.md` §6、`docs/n14-retire-the-emulation.md` §5、`sandlock.py:1894-1917` 的 docstring、`tests/security/conftest.py:199-205`…）；逐条决定"改写 / 保留并注明条件"，不留中间态

- [ ] **Step 2: 写明两处 fork 特例在新形态下的身份**

Run: `sed -n '655,660p' third_party/sandlock/crates/sandlock-core/src/network/rules.rs; sed -n '1258,1262p' third_party/sandlock/crates/sandlock-core/src/sandbox/builder.rs`
Expected: 命中 `if root != std::path::Path::new("/")`（`compose_virtual_etc_hosts`）与 `.filter(|root| root.as_path() != std::path::Path::new("/"))`（凭据暴露警告）。在 `docs/n14-retire-the-emulation.md` §5 写下：合成根**不是** `"/"`，所以这两条特例在合成根下重新变成**活分支** —— `/etc/hosts` 读的是骨架里的文件（骨架没有它 ⇒ 回落到 loopback 基线，与今天等价）、凭据暴露警告按骨架的授权集判定（骨架里没有任何凭据文件 ⇒ 不产生噪音）。前提写清楚：**合成根绝不绑宿主 `/etc` 或凭据目录**，绑了这两条就会变成真漏洞

- [ ] **Step 3: 更新状态行与索引**

```markdown
# docs/open-issues.md，N14 行追加：
**S2 已有落地（2026-09-26，N16）**：pure 形态的合成根已实现 —— `E2B_PURE_ROOTFS=synth` 时
每沙箱一份骨架（`<base>/_pure_rootfs/<id>`，普通目录 + bind 系统目录 + 整棵 /dev + pivot_root），
`E2B_REAL_ROOT=0/1` 两态全绿；这同时是 §6 S5（真根成为唯一形态）的前提。
证据：`docs/superpowers/plans/2026-09-26-pure-shape-synthetic-rootfs.md` 与 `tmp/k0s/n16-*.log`。

# docs/task-backlog.md，N14 行把"pure 是 N15 那条线"改成：
"pure 有两条线：N15 的 identity 翻译（默认，`E2B_PURE_ROOTFS=off`）与 N16 的合成根
（`E2B_PURE_ROOTFS=synth`，普通目录 + bind + pivot_root）"。
```

- [ ] **Step 4: 全仓再扫一遍，确认没引入新的模糊表述**

Run: `rg -n "TBD|TODO|稍后|待定|将来若|if needed" docs/n14-retire-the-emulation.md docs/pure-shape-decision.md docs/open-issues.md docs/task-backlog.md docs/chroot-workspace-exec.md | head`
Expected: 无**新增**命中（既有的保持原样）

- [ ] **Step 5: 提交**

```bash
git add docs/n14-retire-the-emulation.md docs/pure-shape-decision.md docs/open-issues.md docs/task-backlog.md docs/chroot-workspace-exec.md
git commit -m "docs(n14/n16): the pure shape's synthesized root is implemented, and what it changes"
```

---

## 拍板点（执行前需要人确认，各一行）

1. **D2 骨架落点**：本计划取"每沙箱一份 + 平台保留命名空间"（`<base>/_pure_rootfs/<id>`）——理由是 `_runtime/<id>` 是 0700 而 bind/`chdir` 以沙箱自己的 uid 跑（穿不过去），沙箱自己的树又会被它自己删掉。代价是每沙箱多一个顶层目录与一次拆箱清理（Task 6）。
2. **D1 `/dev` 集合**：本计划取候选①（递归 bind 容器的 `/dev`，= 今天 pure 的可见集合，14 条集合相等）。若你要求对齐镜像形态的 `minimal_dev` 六节点，那是**收紧**（实测少 8 条：`fd`、`full`、`mqueue`、`random`、`shm`、`stderr`、`stdin`、`stdout`），Task 3 的表与 `devdiff` 是输入，Task 4 的常量是落点。
3. **D2 `/etc`**：本计划**不绑**宿主 `/etc`（骨架里是空目录）。绑了会把宿主的名字表与凭据面重新灌进沙箱，正是 N15 修掉的那条 wildcard 绕过。
4. **D4 过渡态**：本计划允许"合成根 + `E2B_REAL_ROOT=0`"（两态都跑、都验收），这样开关可以灰度。
5. **D5 `minimal_dev` 显式化**：本计划**没有**把"/dev 用哪套"变成策略字段（那是纯重构），只在 `_synthetic_rootfs_mounts()` 的 docstring 里写清两个形态各自的取法。
6. **收益面**：生产永远是 image-rootfs，所以这条路线只对 dev/lane 有效，外加让 N14-S5 可达 —— 是否值得投入，需要在开工前承认。

## 与 N15 的关系（不是替代，是叠加）

`real_root` 的硬前置就是"必须有 chroot root"（`context.rs:893-896`），而 pure 今天谈得上"能吃真根"，靠的正是 N15 把 `kwargs["chroot"]` 设成了 `"/"`。合成根**只改这个值**，中介本身一行不退（`/proc` 的合成、磁盘活账本、策略判定全在中介里）。因此沉没成本只有三处：`_chroot_root` 返回 `"/"` 那句、`_view_cwd` 的 pure 分支、以及两处 fork 特例从"必须"降为"条件成立"。29 条测试前提迁移不是沉没成本 —— 它买到的"pure 也必须走中介、也必须有 route B 槽位"在合成根下依然成立，而且更硬。
