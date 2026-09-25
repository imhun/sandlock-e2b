# chroot（生产）形态下 workspace 里的可执行文件：三类形状、一张实测表、以及 A/B 的详细账

**2026-09-23。证据文档 + 一个待拍的决定，不含代码改动。** 它接着 **N35②** 往下走：
上一轮把问题定成"shebang 脚本被拒"，定向探针跑完发现**范围比 shebang 大**，也把
**A（真根 N14）与 B（中介补 shebang）** 的账算清楚了。

## 0. 摘要

1. 生产（image-rootfs / chroot）形态下，**workspace 里只有"动态 ELF"能执行** ——
   而它能执行纯属巧合：中介把动态 ELF 拷进一个**匿名 memfd**再注入 fd，而匿名文件不在
   Landlock 规则管辖内。
2. **同一份静态 ELF，放镜像 rootfs 里能跑、放 workspace 里 EACCES(13)**；shebang 脚本
   两种位置都 EACCES（解释器由内核在**宿主路径空间**解析，那个空间一条规则都没有）。
3. **A 的拦路虎不是代码，是部署**：沙箱子进程已经有自己的 userns 和全部 caps（实测），
   内核**允许**它 unshare mount ns 并 bind mount，但**worker 容器的 seccomp 档**在
   生产形状下把 `mount`/`umount2`/`pivot_root`/`chroot` 全部回 EPERM（实测）。
4. **B 有一条不可绕过的语义缺口**：seccomp-notify 不能改 syscall 参数，而 argv 数组
   的地址固定且与 envp 数组紧邻 ⇒ **无法构造内核语义的 argv**，只能走"改写 shebang 行"
   的变体，代价是脚本里的 `$0`/`sys.path[0]` 变成 `/proc/self/fd/M`。
5. **§7 那条已经落地**（2026-09-23，本文档同轮）：workspace 里的**静态 ELF 现在能跑**
   （改前 EACCES、改后 `tini version 0.19.0`，同一份用例在旧镜像上红、新镜像上绿）。
   同时它让**被掩盖的一拍窗口显形**：同一条命令里"写静态二进制→立刻执行"现在报
   **ETXTBSY**，隔一拍（`sleep 1`）就正常——这条以前被 EACCES 挡在前面，看不见。

## 1. 实测（prod-shaped lane，root worker + route B 槽位；**下表是 §7 修复之前**）

探针 `tmp/k0s/probe_n35_exec_gate.py`（每条腿独立 executor、逐条命令 30 s 超时），
lane 入口 `tmp/k0s/n35-lane.sh`，日志 `tmp/k0s/n35-chroot{,2..6}.log`、`tmp/k0s/n35-pure.log`。
`python:3.11-slim` rootfs 由 `resolve_test_rootfs` 提供；静态 ELF 用 lane 镜像里的
`/usr/sbin/docker-init`（tini 0.19.0，无 PT_INTERP）。

| 腿 | chroot（生产） | pure（无 chroot） |
|---|---|---|
| 镜像里的 ELF `/bin/echo` | OK | OK |
| workspace 里**动态 ELF**（`cp /bin/echo` 后 exec，同命令内写+chmod+exec） | **OK**（经 memfd） | OK |
| workspace 里**静态 ELF** | **EACCES，rc 126** | **OK**（`tini version 0.19.0`） |
| **同一个静态 ELF 放镜像 rootfs**（`/usr/local/bin/n35_static_bin`） | **OK** | 不适用 |
| workspace 里 **shebang 脚本**（新写的 / 启动前就存在的，都一样） | **EACCES，rc 126** | OK |
| 镜像 rootfs 里的 shebang 脚本 | **EACCES，rc 126** | 不适用 |
| 脚本用 `sh <file>` 解释执行 | OK | OK |
| 精确 errno（guest 的 python `os.execv`） | **13 EACCES**（不存在的路径是 2 ENOENT） | — |
| 20 次"写脚本→exec"（同命令 / 拆两条命令） | 全部 EACCES，**无 ETXTBSY** | 20/20 OK |
| 20 次"写动态 ELF→exec"（同命令） | **20/20 OK，无 ETXTBSY** | — |
| **对照**：宿主 `/var/tmp/n35-uncovered/`（无任何规则覆盖）里的静态 ELF | — | **EACCES，rc 126** |
| 判别：`#!/usr/local/bin/python3.14`（只在宿主存在） | **126 而不是 127** | `py (3, 14)`，OK |
| 判别：`#!/workspace/interp_bin`（只在 guest workspace 存在） | **126，而不是跑起来** | — |

判别腿的意思：**内核解析 `#!` 时既不在"宿主文件存在"的语义里，也不在 guest 的路径空间里** ——
它落在"宿主路径 + 没规则"的交叉点上，所以"文件不存在"和"文件存在"给出同一个 EACCES。

**§7 落地后的复测**（同一探针、同一 lane，镜像换成含修复的 wheel）：

| 腿 | 改前 | 改后 |
|---|---|---|
| workspace 里**静态 ELF**（探针进程预先放好） | EACCES 126 | **`tini version 0.19.0`，rc 0** |
| 同一条命令里"guest 写静态 ELF → 立刻执行" | EACCES 126 | **ETXTBSY 126**；`sleep 1` 后 **rc 0** |
| 同一条命令里"写**动态** ELF → 立刻执行"×20 | 20/20 OK | 20/20 OK（走 memfd，不碰源 inode） |
| shebang 脚本（预先存在 / 同拍写入） | EACCES | **EACCES**（解释器）/**ETXTBSY**（同拍） |
| pure 形态各腿 | — | **不变**（静态 ELF OK、未覆盖目录 EACCES、脚本 OK、20/20） |

## 2. 机制：三条规则 + 一个内核步骤

**已实测钉住的三条可执行性规则**（chroot 形态）：

1. **镜像 rootfs 内的 inode：可执行**（`/bin/echo`、`/usr/local/bin/n35_static_bin`）。
   原因是规则集的路径都被 `chroot_root.join(...)` 翻译到 rootfs 之下（`landlock.rs` 第 4 步）。
2. **宿主文件系统上的 inode：不可执行**（workspace、`/var/tmp`）—— 规则里根本没有这条路径：
   `fs_writable` 传进来就是**宿主拼写**（实测 policy dump：`/tmp/tmpXXXX-ws`），翻译成
   `rootfs/tmp/tmpXXXX-ws` 后不存在，于是 `continue`，**不装规则**；Landlock 是白名单，未覆盖即拒。
3. **匿名 memfd：可执行**（这就是"workspace 里的动态 ELF 能跑"的唯一原因）。

**一个内核步骤**：`handle_chroot_exec` 只处理**被 exec 的那个目标**（打开成 fd；有 `PT_INTERP`
的再拷成 memfd 并把解释器一起注入），随后把调用方内存里的路径改写成 `/proc/self/fd/N`。
**`#!` 是内核在同一个 syscall 内部接着做的**，没有第二次 seccomp 通知，fork 里也没有 shebang
分支 ⇒ 解释器路径由内核按字面量解析，落在**宿主路径空间**（今天没有真根、没有 mount ns：
`CLONE_NEWNS` 在本仓库只作为"禁止 guest 用的常量"存在，`mount`/`pivot_root` 在 guest 的
seccomp 黑名单里）。

合起来就是：**能过的只有"rootfs inode"和"匿名 memfd"两种落点**，而 shebang 解释器
（宿主空间）和静态 ELF（宿主 inode）都不属于这两种。

## 3. 为什么一直没被发现

* **最常见的形状恰好被 memfd 救了**：`python`、`node`、`bash` 都是动态 ELF，走 memfd 复制，
  于是"在 workspace 里跑个二进制"看起来一切正常。
* **测试面只覆盖这类形状**：chroot 形态的用例读 `/etc/os-release`、`/dev/*`，exec 的是
  `/bin/cat`、`/bin/sh` 这类镜像内 ELF；唯一"装 CLI 再跑"的用例是 **pure 形态**
  （`test_user_cli_install_within_workspace_persists`，`base_image=None`）。
* **平台自己那条 shebang 路径已经绕开**：MCP gateway 用 `/usr/local/bin/python3 <script>` 启动
  （`envd_service/runtime/context.py` 的注释写着原因）。

## 4. 影响面（生产形态）

| 形状 | 今天 | 谁会撞上 |
|---|---|---|
| 镜像里的 console script（`pip3`、`npm`/`yarn` shim） | 直接 exec 不了 | `pip install`、`npm i -g` 之后的日常调用 |
| workspace 里的脚本（`~/.local/bin`、用户 `run.sh`） | 直接 exec 不了 | `pip install --user`、自建入口脚本 |
| venv console script（shebang 指向 `/workspace/.venv/bin/python`） | 两头都不行（见 §6.3） | `uv venv` / `python -m venv` 之后的标准工作流 |
| workspace 里的**静态**二进制 | 直接 exec 不了 | Go/Rust 工具、musl 构建、静态 busybox |
| workspace 里的动态 ELF | 可以（memfd 巧合） | — |

## 5. 选项 A：真根（mount ns + pivot_root）——**已实现，`E2B_REAL_ROOT=1` 打开**

**2026-09-23 落地**（fork `realroot` 模块 + 中介的 pivoted-aware 分支 + 部署档放宽）。
打开后：sandbox 自己建 mount ns、把策略里的挂载（workspace/卷/六个 `/dev` 节点）绑进镜像
rootfs、`pivot_root` 进去，然后**丢掉 `CAP_SYS_ADMIN`** 再启动工作负载。实测效果：
**绝对与相对路径的脚本、静态/动态 ELF、"写完立刻执行"（同一条命令）全部可跑**，
而**工作负载仍然不能 mount/unshare/chroot**（下面的 9.5）。默认**关闭**：worker 容器的
seccomp 档必须先放宽（同批提交里已改），否则每个建箱都会 EPERM。

```sh
# 单机/lane 打开
E2B_REAL_ROOT=1 ./deploy/scripts/test-prod-shaped.sh tests/security
```

### 5.0 实现里被实测纠正的三个假设（留给后来人）

1. **挂载顺序**：必须"先绑策略挂载、再 `MS_BIND|MS_REC` 自绑 rootfs"。反过来的话，
   后加的挂载挂在**原** rootfs mount 上，而 `pivot_root` 移走的是**自绑**那份 ⇒ 新根里
   只剩空的挂载点目录（症状：相对路径 exec ENOENT、绝对路径正常，因为中介走的是宿主路径）。
2. **中介的 fd 路径改写**（`/proc/self/fd/N`）在真根里**必定失败**：新根没有真 procfs
   （`/proc` 是中介合成的），内核解析不到 ⇒ exec ENOENT。真根下 exec handler 改为
   **直接 `Continue`**，让内核在沙箱自己的树里解析（也正因此解释器变成镜像自己的那份）。
3. **chdir 过去"只记账、不动内核 cwd"**（`handle_chroot_chdir` 的老注释：*the child's own
   cwd never moves*）——模拟根下无所谓，真根下内核自己解析相对路径（`execve("./x")`）
   用的是 stale cwd ⇒ 相对路径 exec ENOENT。真根下改为**让内核真正执行 chdir**（先做
   存在性/越界校验与记账，再 `Continue`）。

另外为让"写→立刻执行"这一形状在真根下成立，**每次 exec 前都会"结算"一次写 watch**：
把**所有"沙箱已经关闭、而中介还握着 held 副本"的条目**按 pump 的方式读一次终值（记账不失真）、
标记脏路径（账本 walk 仍会重读）、再释放掉副本（这正是 ETXTBSY 的来源）。写者仍在运行的条目
**故意不动**——那时内核拒绝是对的（沙箱自己的描述符还开着）。这条通用规则同时覆盖了 exec 目标、
脚本的解释器、以及其它"内核自己解析"的路径（早期版本是按路径定点释放，已被它取代）。

### 5.1 现状盘点（实测）

从一个 chroot 形态沙箱的**外部**读 `/proc/<pid>/*`（guest 的 /proc 是中介合成的，不可信）：

```
child of mediator pid=21
    root=/ cwd=/tmp/tmpXXX-cache/python_3.11-slim-.../rootfs   <- 没有 chroot
    mnt=mnt:[4026532453]            <- 与容器同一个 mount ns（没有自己的）
    user=user:[4026532704]          <- 有自己的 userns（≠ 容器 4026531837）
    CapEff=000001ffffffffff         <- 在这层 userns 里是全部 caps（含 CAP_SYS_ADMIN）
```

即：**缺的只是 mount ns 和"去用这些 caps"的代码**，不是权限本身。探针
`tmp/k0s/probe_n35_realmount.py` 验证了这一点：在**带 SYS_ADMIN 的 lane 形状**里，
`unshare(CLONE_NEWUSER)` → `unshare(CLONE_NEWNS)` → `mount --bind` **全部成功**。

### 5.2 部署账（这是 A 真正的成本）

生产 worker 的 seccomp 档（`deploy/seccomp/sandlock-worker.json`）把
`mount/umount2/*mount API` 放在 **CAP_SYS_ADMIN 条件**里，`chroot` 放在 CAP_SYS_CHROOT 条件里，
而 `pivot_root` **根本没列** ⇒ 落 defaultAction。`deploy/k8s/worker.yaml` 的 worker 只有
`NET_BIND_SERVICE, SETUID, SETGID, CHOWN, DAC_OVERRIDE` 五个 cap。实测（`PROD_DROP_CAPS=SYS_ADMIN,SYS_CHROOT`）：

```
unshare(CLONE_NEWUSER): ok        (profile 允许 unshare，无条件)
  after userns: CapEff=000001ffffffffff   <- 仍然拿到全部 caps
unshare(CLONE_NEWNS):  ok
mount --bind src root: EPERM             <- 与 5.1 里同一个操作，只差容器没 SYS_ADMIN
pivot_root:            EPERM
chroot(root):          EPERM
```

结论：**A 需要改部署**，二选一 ——（i）给 profile 加上不带 cap 门闩的
`mount/umount2/pivot_root`（或 `chroot`）允许项，或（ii）把 SYS_ADMIN 还给 worker（A6/A7
刚刚刻意去掉的东西）。选 (i) 也要过一次安全评审：容器里"任何进程都能 mount"是
defense-in-depth 的一个面（userns 的 mount 仍受内核的 userns 限制，且 guest 自己的
`mount` 仍在 fork 的黑名单里）。

### 5.3 代码账

* fork 侧要新增一整块**挂载机制**（今天一行都没有）：`unshare(CLONE_NEWNS)` → 把 `/` 设成
  private → 把 rootfs 变成 mount point → 在 rootfs 里建/绑 workspace、卷、六个 `/dev` 节点 →
  `pivot_root` → 卸载旧根；以及**退出时**的 umount 生命周期与失败回滚。
* 之后要想清楚"哪些模拟继续留"：`chroot/dispatch.rs` 现在有 **27 个 handler**（3353 行）、
  `/proc` 合成在 `procfs.rs`（1951 行）、挂载解析在 `chroot/resolve.rs`（714 行）。
  真根落地后，**路径翻译**的大部分可以退役（内核自己会解析），但策略、COW、磁盘活账本
  仍然需要中介 —— 也就是说 A 是"换掉虚拟根那半"，不是"删掉中介"。
* 与 N15 的交叉：pure 形态要不要也吃真根（`chroot_root="/"` 的 identity 翻译）会决定
  两套路径空间是否最终收敛；N27（平台状态可见性）也在这条线上。

### 5.4 收益

修掉的是**一整类**，不只是 shebang：静态 ELF、`binfmt_misc` 处理器、任何"内核自己解析路径"
的动作；同时 `$0` / `sys.path[0]` / `/proc/self/root` / `mountinfo` / 工具自己再 exec 路径
（§9.2 那个 `timeout` 现象）全部回归"普通文件系统语义"；规则翻译也不再需要"猜拼写"。

### 5.5 风险与验收

* 风险：mount/umount 时序（半成品挂载点、propagation、rootfs 必须是 mount point）、
  容器安全档的放宽、以及"真根之后 `/proc` 怎么办"（要么留合成，要么配 PID ns 挂 procfs）。
* 验收：现有 security 套件（尤其"宿主文件系统不可达"）必须保持绿；新用例：workspace 里的
  静态 ELF 与 shebang 脚本（image 解释器 + venv 解释器）都要能跑；`mountinfo`/`/proc/self/root`
  的行为要么变真、要么继续合成但要写明。

## 6. 选项 B：中介补 shebang 分支

### 6.1 唯一可行的形态：改写 shebang 行 + 注入解释器 fd

复用现成机制：`memfd_with_patched_interp`（已经在做"memfd 拷贝 + 在盘上改字节"这件事）、
`SECCOMP_IOCTL_NOTIF_ADDFD`（按指定 fd 号注入）、`rewrite_exec_path_to_fd`（改写路径 + 修
`argv[0]` 指针）。新代码：解析 `#!`（跳过空格/制表、路径 ≤127 字节、整行 ≤255、可选参数按
内核规则"整段算一个参数"），在**guest 视图里**解析解释器（必须 mount-aware，否则 venv 直接失效），
把脚本 memfd 的 shebang 行改成 `#!/proc/self/fd/<N>`（保留原可选参数），注入解释器 fd，递归靠
通知循环天然覆盖（解释器本身是脚本时会被再拦一次）。

### 6.2 为什么"内核语义的 argv"做不到（这是 B 的硬边界）

内核给脚本构造的是 `argv = [解释器, 可选参数, 脚本路径, 原 argv[1:]...]`，比原 argv **多 2 项**。
而 seccomp-notify **不能修改 syscall 参数**（寄存器不可写；只有 ADDFD 能注入 fd），
`argv_ptr` / `envp_ptr` 都是内核按调用时的寄存器值读的固定地址，且两个数组在栈上**紧邻**：
原 argv 数组 `[script, a1..ak, NULL]` 只够 `k+2` 槽，新数组要 `k+4` 槽 ⇒ 后两项必然压到
`envp[0..1]`，而 envp 又必须从 `envp_ptr` 开始。**没有 slack 可用 ⇒ 无解**（除非改内核/换 ptrace）。

### 6.3 于是 B 的语义代价是明确的

* **脚本自己的 `$0`/`sys.path[0]` 变成 `/proc/self/fd/M`**（因为内核把"被 exec 的路径字符串"
  当 argv[1] 传下去，而那条路径已被改写成 fd 路径）。`cd "$(dirname "$0")"` 这类写法静默失效；
  Python 脚本的 `sys.path[0]` 变成 `/proc/self/fd` ⇒ 同级模块 `import` 直接报错。
* **venv 解释器**（`/workspace/.venv/bin/python`，`uv venv` 的标准产物）：
  - 注入它的 fd ⇒ 那就是"宿主 inode 上的 exec"；**§7 落地前这条必然 EACCES**
    （与静态 ELF 同因），**§7 落地后该 inode 已有 EXECUTE/读权限，这条通路就通了**
    （前置已满足，见 §7）；
  - 或者把它也拷成 memfd ⇒ 能跑，但 python 会从 `/proc/self/exe`/`argv[0]` 判断自己**不是 venv**，
    于是静默用错解释器/site-packages —— 比报错更糟。
* 仍然漏掉：`binfmt_misc` 之类其它"内核侧解析"（今天同样在宿主空间）。

### 6.4 成本与收益

**无部署改动、局部、可测**：改动集中在 `handle_chroot_exec`（+ 一个新 shebang 解析函数与
一组形态用例），落 B 之前先落 §7。它能让"镜像解释器的脚本"（`pip3`、npm shim、`~/.local/bin`
里 shebang 指向镜像 python 的那种）今天就跑起来，代价是 §6.3 的第一条。

## 7. 已落地：`fs_writable` 的 Landlock 翻译（原"应当单独修的 bug"）

`fs_writable` 是宿主拼写，却在 chroot 形态被 `chroot_root.join(...)` 翻译 ⇒ 规则根本没装上
（§2.2）。**2026-09-23 已修**，做法不是"猜拼写"，而是把两件事分开说清楚：

1. **调用方（envd）**在 chroot 形态下把**挂载点**（`/workspace`、`/home/user`、每个卷的
   虚拟路径）也声明进 `fs_writable` —— 宿主拼写继续保留，因为中介的 on-behalf 闸门
   （`deny_open_verdict`）是拿**真实宿主路径**去比对的（两侧各司其职，写在
   `envd_service/executors/sandlock.py` 同一段注释里）。
2. **fork（`landlock.rs`）**在 chroot 形态下，按挂载点声明的权利给**挂载源**（宿主路径）
   装规则：声明为可写 ⇒ 写掩码；只在可读集合里 ⇒ `READ_ACCESS`；两者都不沾 ⇒ **不装规则**
   （fail-closed）。只读挂载永远不拿写掩码。抽成纯函数 `path_rule_rights`，4 条单测钉住。

**为什么不采用"翻译不到就回落到原路径"**：那等于在 chroot 形态下允许策略路径指向宿主
（`/usr`、`/data` 这类路径在镜像里不存在时会静默变成宿主路径），把"chroot 形态只能命名
沙箱内的东西"这条不变量打掉；而挂载源只可能来自**已声明的挂载**，边界清楚。

**验证**：`cargo test -p sandlock-core --lib mount_source_rights` 4/4；新用例
`tests/security/test_chroot_exec_shebang.py::test_static_binary_in_the_workspace_runs`
在**旧镜像**（`e2b-sandlock-test:pre-n35-fswritable`）上**红**（EACCES 126）、新镜像上**绿** ——
`tests/security` 整套因此从 `1 failed / 39 passed / 1 skipped / 2 xfailed` 变成
**`40 passed / 1 skipped / 2 xfailed`**（skip 与 xfail 都未变）；`tests/unit` + `tests/contract`
里既有的 8 条环境依赖失败（MCP gateway 镜像 / netns / 多节点 pause / boxed memory quota）与
XFS 报错在同一命令下**逐一在旧镜像复现**，与本改动无关。

它**不**解决 shebang：解释器 `/bin/sh` 在宿主空间解析，规则若覆盖宿主 `/bin` 就是把
**宿主解释器**放进沙箱（会看到 `py (3, 14)` 这种"宿主版本"），那是 C 路线，与
`test_image_rootfs_cannot_reach_host_filesystem` 钉住的隔离相冲突 —— shebang 仍必须由
A 或 B 来解。

## 8. 建议与决策点

1. ~~先落 §7~~ **已完成（2026-09-23）**：静态 ELF 通了，B 的 venv 前置也满足；
   同一轮里"同拍写静态二进制→执行"暴露出的 **ETXTBSY** 归 N35①/N15 的写描述符释放策略
   （改前被 EACCES 挡着看不见），不是本次改动的回退——改前是"永远跑不了"，改后是"晚一拍能跑"。
2. 然后 **A 还是 B 取决于两个问题**：
   * 产品是否要求"沙箱里的文件系统就是普通文件系统语义"（`$0`、`sys.path[0]`、静态二进制、
     `mountinfo`、任何内核侧解析）？若是 ⇒ **A**，并接受一次 seccomp 档/安全评审的部署改动。
   * 若只是要尽快解锁 console script（且能接受 `$0` 变 fd 路径的写作差异）⇒ **B**，
     作为止血，并在文档/发布说明里写明这条差异。
3. **不建议 C**（放宽规则覆盖宿主解释器目录）：它把宿主二进制当镜像二进制用，是静默的
   语义替换，与隔离目标冲突。

## 9. A 方案的安全评估

A（真根：mount ns + pivot_root）是**形态级**改动，安全评估按"它动了哪些边界"逐条给结论，
依据是本文档 §5 的实测（userns/caps、四条 EPERM、代码面）。

### 9.1 它扩大了什么（唯一的实质面：容器内 mount 能力）

今天生产 worker 容器里 `mount`/`umount2`/`pivot_root`/`chroot` 全是 EPERM（实测），
而 A 必须让 **sandlock-init 自己能挂载**。两条实现路线：

| 路线 | 放宽的东西 | 风险 |
|---|---|---|
| (i) seccomp 档加**不带 cap 门闩**的 `mount/umount2/pivot_root` | 容器里**任何**进程都能调用这几个 syscall | 中：内核的 userns 规则仍然兜底（非 FS_USERNS_MOUNT 的文件系统挂不上、locked mount 不能绑），但"容器内不可 mount"这条 defense-in-depth 消失 |
| (ii) 把 `CAP_SYS_ADMIN` 还给 worker | 容器拿到全套 mount/新 mount API/setns 等 | **高**：这正是 A6/A7 刻意删掉的形状，等于回退那次收敛，且 cap 是"全有或全无"的粒度 |

建议若走 A，取 (i) 而不是 (ii)：粒度更细、可审计、且不改变 worker 的 cap 集。

### 9.2 它缩小了什么（同一改动带来的正收益）

* **路径策略从"模拟"变"真实"**：内核侧路径解析（shebang 解释器、`binfmt_misc`、
  将来任何新解析）落到沙箱自己的树里，规则拼写不再需要"猜"——§2 那三条规则里的
  第二条（宿主 inode 无规则）本来就与"文件系统语义"矛盾，A 把它从根上消掉。
* **fd 注入改写退场**：`/proc/self/fd/N` 那种"把调用方内存里的路径改掉"的手法不再必要，
  随之消失的还有它的一串副作用（工具自己再 exec 一次撞 fd 路径 §10.2、`$0` 变异、
  memfd 拷贝的 `/proc/self/exe` 差异）。
* **隔离不变量的表达更硬**：`fs_denied`/只读挂载/`/dev` 六节点从"中介逐个 syscall 改写"
  变成"挂载表 + Landlock 规则"，可被 `mountinfo` 直接观察与审计。

### 9.3 它引入的新风险（按严重度）

1. **挂载/卸载生命周期**：半成品挂载点、umount 失败、propagation（必须把 `/` 设成
   private，否则宿主会看到沙箱的挂载）、并发建箱时的 mount 计数。缓解：挂载全部在
   沙箱自己的 mount ns 内完成、失败即 `_exit`、退出路径幂等 umount，并在 lane 里加
   "建箱→销毁×N 后 `mount` 计数不涨"的回归。
2. **`/proc` 的去留**：今天 `/proc` 是中介**合成**的（1951 行 `procfs.rs`），含虚拟
   hostname/uptime/mounts 等。真根之后要么继续合成（那 `/proc/self/root` 之类仍是"假"的，
   但至少 fs 语义是真的），要么配 PID ns 挂真 procfs（更强，但要重做 PID ns 与
   `/proc/<pid>` 的可见性策略）。这一条如果不做，A 的收益是"文件系统真、/proc 仍假"。
3. **与 N15 的路径收敛**：pure 形态要不要一起换真根（`chroot_root="/"` 的 identity 形态）
   决定是"一套路径设计"还是"两套"。不收敛的话，两套的差异会长期存在（但 pure 本来就没有
   镜像可 pivot，属于设计选择而非漏洞）。
4. **越权面**：真根之后沙箱**看得到**的是自己的树，但 cap 面没变（userns 里本来就是全 caps，
   实测）。风险点是**挂载源的选择**：`fs_mount` 的来源必须只来自 worker 的可信输入
   （今天已是如此：workspace/卷/dev 节点），否则"给沙箱挂宿主任意目录"就成了新的越权口。
   缓解：把"哪些路径允许作为挂载源"写成策略侧校验（例如必须位于 workspace/卷 base 之下），
   而不是靠调用方自觉。
5. **回滚成本**：形态级改动的回滚要同时回滚 seccomp 档、镜像与部署清单；建议先用
   `E2B_REAL_ROOT=1`（灰度开关）在 lane + 单节点跑通，再进生产清单一轮。

### 9.4 与 B 的安全对比（同样按边界）

| 维度 | A | B |
|---|---|---|
| 容器 capability/seccomp | **需要放宽**（§9.1） | 不动 |
| 沙箱内新增可达面 | 无（树是自己的，cap 面不变） | 无（多注入一两个 fd；fd 只指向解释器与脚本） |
| 内核侧解析的正确性 | 完全正确（真 fs 语义） | 只正确到"中介能改写的那些"；`binfmt_misc` 等仍会落到宿主空间 |
| 语义副作用 | 需要复核 `/proc`、mountinfo、COW、账本（工程量大） | `$0`/`sys.path[0]` 变 fd 路径（**静默**的行为差异） |
| 可回滚性 | 差（形态级 + 部署） | 好（一个 handler + 一个开关） |

结论（安全角度）：**A 的净收益是"消掉一整类路径语义缺口"，代价是"容器内允许 mount"这一次
显式的放宽**；B 不动部署边界，但把缺口留在了内核侧、并引入一个静默的语义差异。若走 A，
务必取 §9.1 的 (i) 路线、并把 9.3.1/9.3.4 两条缓解做成硬性前置。

### 9.5 能不能"只让沙箱根 mount、不让沙箱内 mount"？—— 能，而且今天已经是这样

这是 A 最关键的边界问题：**建根阶段必须能挂载，工作负载必须不能**。答案分三层，前两层是
实测，第三层是本次要补的保险。

**第一层（实测）：今天沙箱里的工作负载已经挂不了任何东西。** 在一个**容器档明确允许
mount 家族**的 lane 里（默认 lane 带 SYS_ADMIN，所以 cap 门闩的那条规则被 runc 保留），
让 guest 自己试：

```
uid=0                                    # userns 里的 root
mkdir /workspace/mnt -> ok               # 自己的可写树
mount -t tmpfs none /workspace/mnt   -> permission denied (rc 32)
mount -t proc  none /workspace/mnt   -> permission denied (rc 32)
umount /workspace/mnt                -> must be superuser (rc 32)
unshare -m true                      -> Operation not permitted
chroot / true                        -> Operation not permitted
```

同一条 lane 里，**沙箱自身**的 setup 相（userns root + 全部 caps）能 bind mount 成功
（§5.1 的 `probe_n35_realmount.py`）。**同一个进程、同一套 caps**，唯一的差别就是 fork
装的那层 seccomp ⇒ 判据是 `DEFAULT_BLOCKLIST_SYSCALLS`（含 `mount`/`umount2`/`pivot_root`/
`open_tree`/`chroot`/`unshare`），由 `confine_child` 在 **exec 之前**装好，并被工作负载及其
后代继承。`unshare -m` 与 `chroot /` 的 EPERM 尤其说明问题：这两条在容器档里是**允许**的
（`unshare` 无条件放行，`chroot` 是本 lane 给了 CAP_SYS_CHROOT 才保留），却仍然被拒。

**第二层（本次要做的）：把挂载序列放进 setup 相。** —— **已实现（2026-09-23）**，见 §5 的落地说明与 5.0 的三条实测纠正；顺序是"先绑策略挂载 → 递归自绑 rootfs → chdir → pivot_root → 卸载旧根 → chdir 到 guest cwd"。`confine_child` 的顺序本来就是
"建 userns → 装 Landlock → 装 seccomp → exec"，挂载序列插在 **userns 建好之后、两道上锁
之前**（也就是今天那一段空档）：`unshare(CLONE_NEWNS)` → 把 `/` 设 private → 绑 rootfs/
workspace/卷/六个 `/dev` → `pivot_root` → 然后照旧装 Landlock + seccomp → `execve` 工作负载。
工作负载拿到的是一个**已经封好**的进程：它继承的过滤器里 `mount` 就是 EPERM。
容器档只需要开 `mount`/`umount2`/`pivot_root`（不带 cap 门闩）；**不需要** `chroot`（走
`pivot_root`）。

**第三层（建议本次一起补的保险）：setup 完成后 capset 只丢 `CAP_SYS_ADMIN`。**
 —— **已实现（2026-09-23）**：`realroot::drop_cap_sys_admin` 在 pivot 之后、Landlock/seccomp
之前清掉 effective/permitted/inheritable 里的 bit 21，其余 caps 保留。
今天 fork 里没有任何 capset 代码，沙箱进程在整个生命周期里都握着 userns 里的全部 caps
（实测 `CapEff=000001ffffffffff`），所以"guest 挂不了"目前**只靠那一层 seccomp**。
丢 `CAP_SYS_ADMIN`（其余 caps 保留 —— `CHOWN`/`DAC_OVERRIDE`/`FOWNER`/`SETUID`/`SETGID`
正是"沙箱内 root"这套契约要用的）之后，即使将来容器档被再放宽一次，内核也会拒：
两道独立机制，任一层失效都不会立刻变成"guest 能挂载"。

**残余放宽（要写进部署文档，别留在纸上）**：容器档放宽之后，容器里**任何**进程都能
"调用"这几个 syscall；内核仍按"该 userns 里的 CAP_SYS_ADMIN"判。worker 本身没有 caps
（EPERM），而"先 unshare 一个 userns、再做 userns 允许的挂载"这条路**今天对沙箱就已经开着**
（沙箱从建箱起就在自己的 userns 里）。也就是说这是"把沙箱已有的姿态复制给 worker 进程"，
不是新的越权面 —— 但它确实变了，且必须与 fork 改动**同批上线**（容器档单独放宽、fork 还没
把 guest 封好，等于把没有任何路径中介的 mount 交给沙箱里的进程）。

**验收（应进 lane 的形态用例）**：① 在容器档允许 mount 家族的形状里，guest 的
`mount`/`unshare`/`chroot` 必须 EPERM，而 setup 相的 bind mount 必须成功 —— **已落地为
`tests/security/test_chroot_exec_shebang.py::test_the_workload_cannot_mount`（两种形状都跑）**；
② 建箱→销毁 N 次后宿主 `mount` 计数回到基线（挂载泄漏）——**已补**为
`tests/security/test_real_root_mounts.py`（§9.6 第 4 条）；
③ 丢 `CAP_SYS_ADMIN` 之后，`chown`/`chmod`/低端口这些"沙箱内 root"行为不变
（按现有 security 套件回归）——**已验（2026-09-23 复跑，lane `tmp/k0s/n35-lane.sh`）**：
`tests/security` 两种形状分别 **45 passed / 1 skipped / 1 xfailed（真根开，XFAIL 是 pure 形态
的既有残余）** 与 **42 passed / 1 skipped / 4 xfailed（默认关，4 条 XFAIL 就是真根能救的那 4 条）**；
同轮 `tests/unit` 全档 **1236 passed / 4 skipped**（4 条 skip 都是形态要求：XFS、非 root worker、
`kubectl` 缺失）。

### 9.6 打开真根之后仍存在的缺口（本轮实测）

1. **同拍写入的文件当"别人的解释器"的 ETXTBSY —— 已修（2026-09-23）**：做法已升级为**通用规则**——
   每次 exec 前结算并释放**所有"沙箱已关闭、而中介还握着 held 副本"的条目**（读终值 → 标脏 → forget），
   因此 exec 目标、脚本的解释器、以及其它内核侧解析路径一并覆盖，不需要专门解析 `#!`
   （早期的按路径定点+shebang 解析版本已被删除）。用例：
   `tests/security/test_chroot_exec_shebang.py::test_script_whose_interpreter_was_written_in_the_same_command_runs`
   （venv console script 形状），真根开=通过、关=xfail；探针腿 `shebangguest` 由 126 变 rc=0。
2. **`/proc` 仍是合成的 —— 已定案：本平台无法在沙箱侧挂真 procfs（实测，不是没做）**。
   三条实测：① 在自建 userns 里 `mount("proc", …)` **EPERM**（没有自己的 pid ns 时内核按规则拒）；
   ② 加 `unshare(CLONE_NEWPID)` 并在 fork 之后（pid=1）再挂，**仍然 EPERM**；
   ③ 先把目标放在**自己挂的 tmpfs** 上（排除"目标 mount 不属于本 ns"这一条），带与不带 pid ns
   **都 EPERM**；把探针从头就以 slot uid（1000）运行、make 两份 map 也复现同样结果
   ⇒ 在这台内核（OrbStack 7.0.14）上，单条目 map 的非特权 userns **挂不了 procfs**，
   要真 procfs 只能由特权方（runc 式 root setup 或带 cap 的 broker）来挂，那是本轮明确
   不引入的部署改动。同时中介合成的 `/proc` 本来就是产品侧更想要的那一份（pid 过滤、
   虚拟 hostname/uptime/meminfo、隐藏宿主路径，`procfs.rs`），所以这条按"设计选择 + 平台限制"
   收口。**代价**（保留在案）：内核侧对 `/proc` 的解析仍不成立，例如 guest 用
   `execve("/proc/self/fd/N")` 这种技巧；guest 自己读 `/proc` 走中介合成 ✓ 不受影响。
3. **部署依赖：两份档必须同批到位（2026-09-23 又补了两个机制）**。`deploy/seccomp/
   sandlock-worker.json` 已加无门闩的 `mount/umount2/pivot_root` 允许项，**节点先应用这份档、
   再打开 `E2B_REAL_ROOT`**，否则建箱全 EPERM。今天不靠"记得"：
   * **启动即自检（本轮新增）**：worker 第一次建 `SandlockExecutor`（结果进程内缓存）时用一个
     **子进程**走一遍真根的关键步骤——userns → mount ns → `MS_REC|MS_PRIVATE` → bind →
     `pivot_root` → `umount2(MNT_DETACH)`——失败就在构造函数里抛 `RuntimeError`，把**哪一步失败**
     与**下一步该做什么**（应用 `deploy/seccomp/sandlock-worker.json`）一起写进消息；不再是
     "每个建箱都 instance is closed、却没有任何原因"（那正是本轮踩过的）。用子进程而不是
     `os.fork()`：worker 是多线程的（`asyncio.to_thread`），在线程里 fork 出的子进程可能死在
     别的线程持有的锁上，那会让"防呆"本身变成卡死；子进程还自带 30 s 超时。
     **实测**：worker 档在位 → 探针返回空串（可以建根）；换成本轮之前那份旧档（无 `pivot_root`）
     → `pivot_root (the profile must admit it): Operation not permitted`。用例
     `tests/unit/test_real_root_gate.py`（5 条：拒绝时的消息、开关关闭/无 rootfs 时不探测、
     探针 stdout 即诊断原文、ok/超时/无理由退出三条边界）。
   * **DaemonSet 里内嵌的那份档已补同步（本轮修掉的真缺口）**：`deploy/k8s/seccomp-installer.yaml`
     的 ConfigMap 是 profile 的**内嵌副本**（不是引用文件），而它在本轮之前停在 **`8ea5909`**
     那份 profile —— 两次 N35 改动都只改了 profile、没同步内嵌副本，所以**只 `kubectl apply`
     DaemonSet 的节点会拿到旧档**（旧档里连无门闩的 `mount` 都没有）。现已按该文件自己写的
     流程重嵌，并把 `checksum/profile` 注解更新为 profile 文本的 sha256（`071486c0…`）；
     `tests/unit/test_worker_manifest_permissions.py` 同时钉住"内嵌副本与 profile 逐字节相等"
     与"注解 == sha256(profile 文本)"，重嵌工具 `tmp/k0s/sync-seccomp-installer.py`（自检是
     "把盘上 payload 重新编码必须逐字节复现"，先证明编码器可信再改写）。
4. **已补的验收（§9.5 的 ②）**：`tests/security/test_real_root_mounts.py` 钉住"建箱→销毁 3 次后
   worker 自己的挂载表一字不变"——两种形状都跑，任何"把挂载做在宿主命名空间里"的实现会立刻红。
5. **binfmt_misc（及其它内核侧格式解析）—— 已实测并钉住（2026-09-23）**：注册一个自己的
   magic 处理器（`:n35probe:M::#N35BINFMT::/bin/sh:`，解释器是**镜像内**路径），在 workspace 里
   放一个以该 magic 开头的可执行文件：
   * **真根开**：内核在**镜像自己的树**里解析 `/bin/sh` ⇒ payload 真的跑起来（rc=0）；
   * **模拟形态**：解释器落在**宿主路径空间**，chroot 翻译后的规则集没有覆盖 ⇒ **EACCES 126**
     （与 shebang 同一机制，之前没人报过是因为没人用 binfmt 形状的镜像）。
   ⇒ 结论：真根同样修好这一类；前提是**解释器必须存在于镜像里**（注册表属于 worker 的内核，
   解释器路径由内核在沙箱的根里解析）。用例：
   `tests/security/test_chroot_exec_shebang.py::test_a_format_handler_resolves_its_interpreter_inside_the_sandbox`
   （自己挂 `binfmt_misc`、自己注册，真根开=通过 / 关=xfail）。**剩余**：解释器只在宿主存在的
   处理器仍然不可用（这是"注册表在宿主机、镜像里没有该解释器"的必然结果，不是缺口）。

### 9.7 真根实现的安全复核（2026-09-23，逐条实测）

结论：**净变化以"收权"为主**，唯一的放宽（容器档允许 mount）已用参数过滤收到"只能 bind/传播"。
逐条（数字都是本机 lane 实测）：

1. **沙箱能力下降**：工作负载 `CapEff` 由 `000001ffffffffff` → **`000001ffffdfffff`**
   —— 正好只清掉 bit 21（`CAP_SYS_ADMIN = 0x200000`）；`NNP=1`、`Seccomp=2` 不变。
   恢复无路：`unshare` 在沙箱自己的黑名单里、`clone` 有 namespace 参数过滤、`NNP=1` 让文件
   caps 失效、permitted 已清 ⇒ 拿不回来。
2. **工作负载只继承 stdio**：修复后 `/proc/<pid>/fd` 只有 0/1/2。
   **本轮复核发现并修掉一个洞**：诊断用的 `SANLOCK_REALROOT_TRACE` 文件描述符原先会被
   工作负载继承（实测 fd 20 指向宿主上的 trace 文件——等于把宿主文件的写句柄交给沙箱）；
   现在显式 `FD_CLOEXEC`。exec **失败**时该标志不生效，所以"exec 失败面包屑"仍能用。
3. **容器级放宽已收窄**：`mount` 只允许 **fstype == NULL**（bind / 传播操作，真根只用到这些），
   `umount2`/`pivot_root` 不加参数过滤（这两条参数表太短，加索引会读到垃圾寄存器，libseccomp
   也会直接拒，故拆成两条规则）。实测（**钉死的生产 cap 形状**，容器无 SYS_ADMIN）：
   **容器里任何 userns-root 进程都挂不了任何文件系统**（tmpfs/procfs 全 EPERM），
   而真根的 bind + `pivot_root` + `umount2(MNT_DETACH)` 照常工作——真根在该形状下**端到端全通**
   （脚本/相对路径/venv 解释器/静态二进制/20 次循环），guest 的 mount 仍 EPERM。
4. **"内核侧解析落到宿主空间"这一类被消除**（而不是被 Landlock 兜住）：真根把旧根 `MNT_DETACH`，
   `#!`、ELF 的 `PT_INTERP`、binfmt 处理器都在**镜像树内**解析。实测 binfmt：真根=成功，
   模拟形态=EACCES 126 ⇒ 对比证明是"路径空间"变了。
5. **沙箱有独立的 mount ns**：实测 `mnt:[4026532705] ≠ 容器 4026532453`；且建箱→销毁 3 次
   宿主的挂载表一字不变（`tests/security/test_real_root_mounts.py`）。
6. **共享镜像 rootfs 仍只读**：Landlock 对 rootfs 的规则是 `READ_ACCESS`（§7 的"挂载源写掩码"
   只作用于 workspace/卷），沙箱写 rootfs 仍走中介的 COW/拒绝；既有
   `test_write_outside_workspace_denied` 在两种形状下都过。
7. **本轮复核的两条附注（已实测校正）**：
   a. **工作负载的 fd 表只有 0/1/2（实测 `/proc/<pid>/fd`）**。seccomp notify listener
      （`keep_fd`）与诊断用的 trace fd 都留在**沙箱 init** 手里——init 是平台自己的进程，
      只 fork+exec 工作负载、**从不执行 guest 代码**（`init/mod.rs::spawn` 里对 `CONTROL_FD`
      设 `FD_CLOEXEC`、stdio 重定位、子进程 exec），所以"沙箱自答自己通知"这条路对 guest
      **不成立**。保留为**可选加固**（握手后把 init 的表也收干净），不是洞。
      trace fd 的继承问题确实存在过，但受害者也可能是**工作负载**（早先实测 fd 20 落在它表里），
      已用 `FD_CLOEXEC` 修掉（fork 提交 `447454d`）。
   b. 本内核不在 `/proc/<pid>/status` 打印 Landlock 字段，所以"沙箱确有 Landlock 域"这一条
      **没有**被独立验证；安全结论依赖既有 security 套件（两种形状都过），不依赖该字段。
8. **被策略拒绝的路径在真根下没有变松（实测，两形状逐字节一致 —— 2026-09-23 复核）**：
   真根开/关两形状下逐条实测，结果**完全相同**（用例 `tests/security/test_real_root_denials.py`
   按精确三元组钉住，两种形状各跑一遍）：

   | 探针 | (rc, stdout, stderr) |
   |---|---|
   | `cat /proc/kcore` | `(1, b"", "cat: /proc/kcore: Permission denied\n")` |
   | `ls /sys` | `(2, b"", "ls: cannot access '/sys': Permission denied\n")` |
   | `ls /sys/kernel` | `(2, b"", "ls: cannot access '/sys/kernel': Permission denied\n")` |
   | `ls /proc` | `(0, b"", "")` |
   | `echo x > /usr/bin/n35-probe` | `(2, b"", "/bin/sh: 1: cannot create /usr/bin/n35-probe: Permission denied\n")`，且宿主侧镜像树里没有这个文件 |

   **一处早先的解释被实测纠正**：`/sys`、`/sys/kernel` 在真根下**不是**"变成镜像树内的空目录"
   —— 两形状都是 `EACCES`，也就是说拒绝来自 **Landlock 的 `fs_denied` 规则**（与目标在树里存不存在
   无关），不是"树里恰好是空的"这种巧合；真正"空"的只有 `/proc`，因为它是中介合成的视图
   （列出成功、内容为空，没有宿主 pid/宿主树）。结论不变、而且更强：真根没有让任何一条被拒路径变松。
9. **checkpoint/restore：已从"没人用"升级为"实测不兼容并被显式拒绝"（2026-09-23）**。
   **① envd 确实没用**：`envd_service/` 全树没有任何 fork C/R 符号（`checkpoint` /
   `restore_interactive` / `restore_skipped`），现由 `tests/unit/test_checkpoint_restore_unused.py`
   守住——将来谁要用，这条会先红，提醒先解决下面的兼容问题。
   **② 兼容性是实测出来的，不是"应该没事"**：`Sandbox::restore_interactive` 把 **restore stub
   当宿主路径 exec**（`resume::stub_path` 是构建产物）并把它塞进 `fs_readable`，而**任何 chroot
   根**（模拟的、真根的）都把 workload 的路径解析到 rootfs 里面 —— 于是 stub 在那里根本不存在。
   在 `chroot + real_root(true)` 政策下实测原文：

   ```
   sandlock child: execvp '/src/target/debug/build/sandlock-core-*/out/restore-stub':
   No such file or directory (os error 2)
   restore stub never signalled READY within 10000ms: exited with restore-stub code 127
   ```

   **关掉 `real_root`（只用模拟 chroot）同样复现** ⇒ 这条**不是真根独有**，是"带 root 的形态"
   通病；fork 自己的 OCI 用例里早有一句注记"restore of a chrooted checkpoint is a separate
   limitation"，与此一致。**已做的处理（fork `43cc62a`）**：在配置了 chroot 根而 stub 不在该根内时
   **立刻拒绝**并写明 stub、根与两条出路（用策略挂载把 stub 带进根里 / 恢复到无 chroot 的政策），
   不再让人等 10 s 超时；无 chroot 的恢复**不受影响**（`test_restore_glibc_vdso_program_resumes`
   仍绿），两种根形态的"立即拒绝"由新用例 `test_restore_resumes_inside_a_real_root` 钉住。
   **③ 对 E2B 的含义**：E2B 的生产形态就是 chroot 根（`E2B_BASE_IMAGE=python-mcp:3.14` ⇒
   image-rootfs），所以在把 stub 送进根里之前，**"恢复沙箱"这类功能在这条 API 上不可用**——这条
   写成前置条件，真要做时按它开工，而不是"将来若启用需重新验证"这种含糊说法。
   **④ 可行方案见 §11**（关键未知量——按 fd 执行是否绕过 Landlock——已实测，结论是"不绕过权限、
   但绕开路径解析"，所以方案是"fd 执行 + 给 stub 宿主路径一条执行授权"）。
   **⑤ 前置条件已满足（2026-09-24，fork `a6f6b04`）**：§11.1 的 A 方案已实现 —— stub 改由
   描述符投递（`execveat(fd, "", …, AT_EMPTY_PATH)`，fd 6，与它自己的 CTRL/READY/GO 同一套），
   规则集按**宿主路径**给这一个文件 `EXECUTE|READ_FILE`（`Sandbox::fs_readable_host`，
   `serde(skip)` 保证镜像/wire 布局不变），中介对 `AT_EMPTY_PATH` 的空路径直接放行（原先会把
   dirfd 当目录 fd 去翻译、把整次调用拒成 EACCES）。实测：**模拟 chroot 与真根两种形态都能恢复**
   （计数器继续前进、`restore_skipped` 只有 stdio、恢复后 fd 表断言"无 restore-stub / notify
   listener / memfd"）；投递 fd 用 `dup3(O_CLOEXEC)`，否则实测会以 `6 -> …/restore-stub` 漏进
   恢复后的进程。**剩余的是 E2B 自己那一半**（镜像存储/属主/配额/清理、pause/resume 生命周期、
   `restore_skipped` 的语义），守卫 `tests/unit/test_checkpoint_restore_unused.py` 仍是入口。


## 10. 未结线索（不算结论）

1. **两次 30 s 卡死，未复现**：最初两轮（trace 开、前面已跑十来条腿）里，20 次"写 + 拒绝 exec"
   的循环卡在 `access(2)` 通知上（supervisor 侧空闲）；随后同一段命令在独立容器里重跑
   （`loop` 单独、`script,loop`、`loop+binloop`、`sloop`）都是几秒完成。
2. **`timeout` 包一层就变味**：`timeout 10 /usr/local/bin/pip --version` 报
   `timeout: failed to run command '/proc/self/fd/5': No such file or directory` —— 中介把
   路径改写成 `/proc/self/fd/N` 之后，**由上层工具自己再 exec 一次**的形状会撞上这个改写。
   真根下这条**自然消失**（pivoted 的子进程不再被改写路径，见 §5.0.2），也就是 A 的收益之一。
3. **失败可见性**：真根这条路原本是"静默死"（子进程 stderr 在 slot 形状里还没接上）。
   本轮加了 `SANLOCK_REALROOT_TRACE=<file>`：把每一步、以及 `fail!`/`child_fail`/exec 的
   失败原文写进同一个**先开好的 fd**（pivot 之后路径已不可解析，必须用 fd）——排查这类
   "instance is closed 但没有原因"的问题就靠它。
4. **`init` 的 exec 路径读了两遍 errno —— 已定位并已修（2026-09-23，fork `f1fecba`）**。

   **现象**：`deploy/scripts/test-prod-shaped.sh` phase 1 有两条红 ——
   `test_route_b_executor.py::test_missing_binary_exits_127_through_the_slot` 与
   `test_sandbox_lifecycle_rebuild.py::test_nonexistent_binary_exits_127_with_no_output`，
   钉的是"缺失的可执行文件＝退出码 127 且**没有任何输出**"。实测拿到的却是
   `sandlock-init: exec "/nonexistent-e2b-bin" failed (errno 13)`。

   **根因**：`init/mod.rs::spawn` 的 exec 分支里，`execvp` 返回后先调
   `realroot::record_failure(...)`（把失败写进 trace 文件），**之后**才读 `errno` 决定要不要
   打 FUP-26 那一行。而 `record_failure` 会 `open("/tmp/sandlock-real-root-error")`
   （`SANLOCK_REALROOT_TRACE` 未设时的默认路径）——**沙箱的规则集不给 `/tmp` 写权限**
   （实测 guest 里 `open /tmp/sandlock-real-root-error` = **EACCES(13)**，见
   `tmp/k0s/probe_127_errno.py` 的矩阵），于是这次 open 的 EACCES **盖掉了** execvp 真正的
   ENOENT(2)，`errno != ENOENT` 成立、诊断行被打印，契约随之破掉。

   **为什么"开着 trace 就没事"**：`trace_enabled()` 就是 `env::var(..).is_ok()`
   （`realroot.rs:114`），而 `note("exec …")` 就在 `execvp` **之前**、且只在 trace 开时调用 ——
   它先把 trace 文件开了一遍（TRACE 单元被初始化），`record_failure` 于是不再 open、也就不会
   改 errno。实测矩阵（同一 phase-1 形状，只改这一个环境变量，脚本 `tmp/k0s/phase1-probe2.sh`）：
   * **未设** → `test_route_b_executor.py` **1 failed / 13 passed**（errno 13）；
   * 设成任意值（`/workspace/tmp/...`、`/tmp/...`，甚至**空字符串**）→ **14 passed**。

   **不是本轮 N35 引入**：把工作树切到本轮之前的 `0021a9f`（`git worktree`，同容器同形状）跑同样
   两条 → 同样 2 failed；且 fork 自己的
   `test_exec_failure_names_the_kernel_errno_instead_of_exiting_127_silently`（FUP-26）在
   **root 形态门禁**（`--privileged`）下也是红的：它用符号链接环钉 ELOOP(40)，实得
   **errno 13** —— 同一处 errno 覆盖，只是它只在"trace 默认路径不可写"的形状下暴露。

   **修法**（`init/mod.rs`，一处）：`execvp` 返回后**立刻**读一次 errno，面包屑与判定共用这一个
   值（同文件里 chdir 分支本来就是这么写的，只有 exec 这条把诊断插在了中间）。修完实测：
   * fork 门禁里那条 FUP-26 用例 **RED(`errno 13` vs 期望 40) → GREEN**；
   * E2B 侧两条契约在 phase-1 形状、**trace 未设**（= 线上默认）下 **2 failed → 16 passed**
     （`tests/contract/test_route_b_executor.py` 整个文件）；
   * phase 1 全档 **1711 passed / 2 failed → 1714 passed / 6 skipped / 4 xfailed / 0 failed**；
     phase 2 `51 passed / 1 skipped`；`E2B_REAL_ROOT=1` 的 `tests/security` `45 passed / 1 skipped /
     1 xfailed`（wheel 重建，manifest HEAD=`f1fecba07d93…`）。

   **新钉的用例**：`tests/contract/test_route_b_executor.py::
   test_the_missing_binary_contract_survives_the_diagnostic_trace` —— 同一形状下**把 trace 开与
   关各跑一遍**，都要求 `(127, b"", b"")`，这样契约不再依赖跑它的人有没有恰好设这个变量
   （N35 的 lane 默认就设，正是它把这个差异盖住的原因）。

   形状细节：两条用例都是 **pure 形态**（`base_image=None` / 无 rootfs，现场 `chroot=None`），
   与真根无关；这是"errno 只该读一次"的通用缺陷，任何"诊断自身会失败"的路径都可能踩到。


## 11. 真根下 checkpoint/restore 的可行方案（2026-09-23，关键未知量已实测）

**问题（§9.7.9 实测）**：`restore_interactive` 把 restore stub 当**宿主路径** exec；任何
chroot 根（模拟的、真根的）都把 workload 路径解析到 rootfs 内 ⇒
`execvp '…/restore-stub': No such file or directory` 后 10 s READY 超时。当前已临时改为
**立即拒绝**（fork `43cc62a`），方案落地前拒绝对话就是正确行为。

**关键未知量：按 fd 执行（`execveat(fd, "", …, AT_EMPTY_PATH)`）能不能绕过 Landlock？**
探针 `tmp/k0s/probe_landlock_execveat.py` 自建 Landlock 域（decoy 目录全权、`/` 与 `/tmp`
只给 search），用**静态**二进制（`tests/rootfs-helper`）当 stub —— 第一版用 `/bin/true`
时 EACCES 其实来自"动态解释器读不到"，属测量污染，记在这里以免后来人重踩。

| 规则集 | `execve(stub 路径)` | `execveat(stub_fd, "", AT_EMPTY_PATH)` |
|---|---|---|
| stub 在授权之外（decoy 之外） | EACCES(13) | **EACCES(13)** |
| 额外把 stub 的**宿主文件**授 `EXECUTE\|READ` | 跑起来（helper usage，exit 1） | **跑起来** |

结论：**fd 形式不是权限后门**（Landlock 仍按文件真实路径判定），但它**绕开了路径解析** ——
而这正是 chroot/真根形态下唯一坏掉的那一环。所以方案 = **"fd 执行" + "给 stub 的宿主路径一条
执行授权"** 两条同时给，缺一条都不成立。

### 11.1 推荐方案（改动都在 fork 内，E2B 侧零改动）

> **状态（2026-09-24）：已实现**，fork `a6f6b04`。落地要点与实测见 §9.7.9 ⑤ 与本节末尾；
> 下面保留原始设计与理由，作为评审记录。

**先厘清为什么 restore 非要有 stub**（这决定方案只能怎么改）：
`restore_interactive` 的契约是"把 checkpoint 的进程镜像放回一个**已经在沙箱里**的进程"。
沙箱的每一次启动都要 exec 一个程序；restore 时 exec 的就是 stub，因为它是唯一满足下面三条的
东西：

1. **干净地址空间**：`execve` 会把地址空间整体换掉，stub 之后只剩它自己几页（链接在
   `STUB_BASE`，任何 workload 都用不到的高位窗口）+ 内核给的初始栈/auxv/vDSO，而它随后把
   这些也搬走/清掉。checkpoint 的区域要按原地址 `MAP_FIXED` 铺回来，先有"没有别的东西"才能
   保证恢复后的映射集合==镜像记录的集合（`test_restore.rs` 专门断言"没有 stray mapping"）。
   这正是它取代掉的 ptrace 注入引擎做不到的事：那套在一个 parked libc launcher 上重建镜像，
   launcher 自己的 text/heap/stack 会留在恢复后的地址空间里且**可达**。
2. **必须在沙箱内部**：恢复出来的进程要活在同一个 user/mount/net/pid ns、同一个 uid、同一套
   Landlock 域和 seccomp 过滤里。supervisor 在另一个 uid/名字空间里，它只能做两件事：把内存
   写进那个进程（`process_vm_writev`）和通过 fd 驱动它——所以沙箱里必须有一个**肯配合**的进程，
   那就是 stub。
3. **在任何策略下都能跑**：freestanding、`-static -nostdlib -no-pie`、不调 vDSO、只用极少
   几个 syscall；控制 blob 读进 `.bss` 而不是映射（避免被 `MAP_FIXED` 覆盖）。

协作协议就是为此设计的：fd 3/4/5 固定（控制 blob / READY eventfd / GO 管道），stub 铺完区域
先发 READY，supervisor 再 `process_vm_writev` 写入匿名页，stub 收 GO 后 `mprotect` 回checkpoint
的权限、清掉自己启动残留、按原编号重开 fd 表、恢复 fs/gs base，最后 `rt_sigreturn` **跳进
checkpoint 的寄存器上下文**——不是让原程序自己跑起来再注入。

**代价正是 §9.7.9 那条**：这个"沙箱里 exec 的程序"目前是按**宿主路径**执行的，于是任何 chroot
根都解析不到它。注意 stub 的**控制 fd 本来就已经是注入进去的**（`extra_fds`）——只有"二进制
本身"还走路径。所以 11.1 的两条改动，本质是"把二进制也换成 fd 投递"，与它自己的控制通道保持
一致。另有一个被否掉的设计可以佐证取舍：最初想用 `userfaultfd` 给 stub 做按需分页，但 uffd 在
sandlock 默认黑名单里、seccomp 又是单向的 ⇒ 那会把一个众所周知的可利用原语**永久**授给每个
恢复过的沙箱；改成 supervisor 侧 `process_vm_writev` 不需要任何策略改动（`checkpoint/resume.rs`
头部记着这段）。

1. **`restore_interactive`**：把 stub 以 `O_PATH|O_CLOEXEC` 打开，fd 走**已有**的注入通道
   （`resume::StubChannel::extra_fds()` → `rt.extra_fds`，restore 本来就在用它传 blob 通道），
   并把 `fs_readable.push(stub)` 换成"给 **stub 的宿主路径** 授 `EXECUTE|READ`"的规则 ——
   Landlock 规则集在 `pivot_root` **之前**按宿主路径构建，表达这条没有障碍。
   为什么必须"换成"：`landlock.rs::build_ruleset` 在 chroot 形态把 `fs_readable`/`fs_writable`
   的每条都当**虚拟路径**处理（`root.join(strip_prefix("/"))`），`!host.exists()` 就 `continue`
   ——"host paths are NOT added（fail-closed）"。stub 的宿主路径在 rootfs 里当然不存在，
   于是**今天它一条规则都没拿到**（这解释了为什么"只按 fd 执行"仍然 EACCES）。
   同一函数下方对 `fs_mount` 的**挂载源**已经在按宿主路径授"挂载点声明的权利"，理由一模一样
   （内核按文件真实层级判定），stub 要的正是这一条路的形态。
2. **init / `create_interactive`**：新增"以 fd 执行"的入口（program spec 里带 `exec_fd`），
   child 侧用 `execveat(exec_fd, "", argv, envp, AT_EMPTY_PATH)` 代替 `execvp(path)`。
   `keep_fds`/`extra_fds` 已保证该 fd 到达 child，init 的 exec 分支只需多一个模式。
3. **中介（supervisor）无需改动**：fd 执行不经过路径翻译；真根形态 mediator 本来就对该 execve
   答 `Continue`（`child_is_pivoted` 分支），这套改动恰好补上"无法注入 fd 时怎么办"。
   模拟 chroot 形态同样受益：不再需要把宿主路径翻译进 rootfs。
4. **验收（RED → GREEN）**：
   * 把 `test_restore_resumes_inside_a_real_root` 从"两种根形态立即拒绝"翻成"两种根形态都能
     恢复、计数器继续前进"，chroot-free 的 `test_restore_glibc_vdso_program_resumes` 继续做正向对照；
   * 再加一条负向用例：**授权缺失时立即拒绝并点名 stub**（防止退化成 10 s 超时）；
   * E2B 侧：`E2B_REAL_ROOT=1` 跑 `tests/security` + 一条"恢复后计数器前进"的形态用例，并解除
     `tests/unit/test_checkpoint_restore_unused.py` 的守卫（该用例就是为这一刻留的门）。
5. **风险与待验**：
   * 该改动把 stub 放进沙箱的**可执行授权集**（仅这一个文件、只读+执行）。stub 是平台自己的
     静态二进制、workload 不可写，但按规矩仍要做一次评审确认这不是新的越权面；
   * 需要 file 级 `EXECUTE` 规则（本机 Landlock ABI **8** ✓；老内核要按 ABI 探测降级）；
   * 沙箱已设 `no_new_privs`，`execveat` 无额外前提 ✓。

### 11.2 备选（fd 路线若在某个内核/ABI 上不成立）

* **B1 策略挂载把 stub 带进根里**：把持有 stub 的目录（只读）bind 到 rootfs 内**已存在**的目录上。
  真根形态可行（realroot 就是按策略挂载做 bind）；**模拟 chroot 形态不行**（没有内核挂载，
  路径必须真实存在于 rootfs）。代价：要在共享镜像树里放挂载点，需 per-sandbox 命名与清理。
* **B2 E2B 侧 staging**：把 stub 拷进沙箱自己的 workspace（per-sandbox、可写、随沙箱销毁），
  core 的 restore API 接受"调用方指定的 stub 位置"。代价：恢复语义变成"宿主先给一个可写位置"。
* **B3 直接往 rootfs 拷 stub**：单租户/独占 rootfs 下最简单，但污染共享镜像，不建议用于 E2B 现形态。

**对 E2B 的含义**：生产形态是 chroot(+`real_root`) ⇒ 在 11.1 落地前"恢复沙箱"类功能不可用
（`test_checkpoint_restore_unused.py` 挡住误用）；落地后按 11.1 第 4 条的验收重跑即可。

### 11.3 "先存镜像文件、再从文件恢复" —— 引擎已经支持，且与 11.1 正交（不是它的替代）

fork 里已有目录式镜像：`Checkpoint::save(dir)` / `Checkpoint::load(dir)`
（`checkpoint/image.rs`，格式 v2、先写 `*.tmp` 再原子 rename），FFI 出
`sandlock_checkpoint_save` / `sandlock_checkpoint_load`，Python `_sdk` 也绑了；`sandlock-oci`
就是这条路：`checkpoint <id> --image-path <dir>` → `restore <id> --image-path <dir>`，
`run_supervisor_restore` 在**另一个进程**里 `Checkpoint::load` 再恢复（OCI 用例甚至恢复到
第二个容器）。**镜像内容**：`meta.json`（name / cow_snapshot / version）、**`policy.dat`
（bincode 序列化的 Sandbox 政策）**、可选 `app_state.bin`、`process/{info.json（pid/cwd/exe）、
fds.json、memory_map.json（**每一条映射**，含文件后备区域，供恢复时按原文件重映射）、
threads/0.bin（寄存器+FP）、memory/<start_hex>.bin（匿名区被捕获的字节）}`。恢复时用的是
**镜像里保存的政策**（`cp.policy.clone()`），所以镜像是"自含怎么建这个沙箱"的。

**为什么它不是 11.1 的替代**：文件形态只改变"镜像**从哪来**"（内存 → 磁盘），不改变
"镜像**怎么进到沙箱进程里**"——恢复流程仍然是"按政策起一个新沙箱 → 在里面 exec stub →
supervisor 把页写进去"。OCI 那条 bonus 注记（"restore of a chrooted checkpoint is a separate
limitation"，因为 bundle 形态带 chroot）正好印证：文件形态一样卡在 stub 上。

**要用它做 E2B 的"暂停/恢复"，文件形态另外带来这些前提**（都要单列验收）：
* 只能**同内核**恢复（引擎自述），且只有 x86_64 / riscv64 引擎；
* `restore_skipped` 覆盖 socket/pipe/memfd —— **连接类 fd 不会回来**（对沙箱内 MCP gateway
  这类形状是硬约束）；
* `memory_map.json` 与 `fds.json` 记的是**宿主路径**，恢复时经 `restore_blob::plan` 按 chroot/
  挂载翻译 ⇒ 镜像只在"镜像 rootfs 与各卷挂载仍按同样方式解析"时有效；`cow_snapshot` 记的是
  **路径**，上层目录必须还在（或随镜像一起快照）；
* 镜像里含政策与进程内存 ⇒ 存储位置、属主/权限、配额与清理要和沙箱数据同级对待。

### 11.4 除 exec stub 之外的可选路线（谁在沙箱里搬内存，以及各自的代价）

恢复要同时满足三件事：① 有一个**在沙箱内部**（同 ns/uid/Landlock/seccomp）的进程；
② 它的地址空间**干净到能把镜像按原地址铺回去**；③ 有人把内存/寄存器/fd 搬回去。围绕
"② 与 ③ 由谁提供"，可控的路线只有这些：

| 路线 | 谁做②/③ | 绕过 §11 的两道关？ | 代价（实测/代码事实） |
|---|---|---|---|
| **A. execve stub（现状）** | 一个 freestanding 静态二进制：exec 提供干净地址空间，stub 自己铺区域，supervisor `process_vm_writev` 写页 | **否**（要能 exec 到它 + 要一条 Landlock 执行授权） | 需要 stub 可达 + 固定基址 + 按架构维护；这正是 §11 要修的 |
| **B. 不 exec，进程内恢复载荷** | 复用已有的 `in_child_main: Option<fn()>`（OCI 的沙箱内 init 就走它："run this function in-process instead of `execve`-ing a workload … **nothing is exec'd, so Landlock has no execution to authorize**"） | **是**（无 exec ⇒ 无路径、无执行授权） | 地址空间是"fork 自 supervisor 的脏镜像"（tokio/缓冲/libc 全在），要自己证明能清到与镜像一致（现有的 stray-mapping 断言是为 stub 立的，得另立等价证明）；fd 表也**不再**由 exec+CLOEXEC 自动清零，要显式关干净再按原编号重开 |
| **C. 旧 ptrace 注入引擎** | 外部 ptrace 附着到沙箱里一个 parked launcher，直接写镜像 | 是（不 exec 额外二进制） | **已明确放弃**：launcher 的 text/heap/stack 会留在恢复后的地址空间里且可达（`test_restore.rs` 的 stray-mapping 断言就是钉这个）；还要处理"何时停住它"的竞态与 route-B 下的 ptrace 权限 |
| **D. userfaultfd 按需分页**（原设计） | stub 建 uffd，按缺页惰性供给 | 否（仍要 stub） | **被否**：uffd 在默认黑名单、seccomp 单向 ⇒ 等于把众所周知的利用原语永久授给每个恢复过的沙箱；`UFFD_USER_MODE_ONLY` 还服务不了内核态缺页。变体（init 在锁前建 uffd、以 fd 交给 stub、只用 ioctl）能绕开策略问题，但复杂度更高且仍需 stub |
| **E. CRIU** | 外部工具：注入 parasite 代码 + 写内存 + 跳转（`restore_blob.rs` 的注释直接引了它的 restart 修复做法） | 是（不 exec 我们的 stub） | 大依赖 + 需要特权/ptrace + 要在本架构（per-sandbox userns/Landlock/seccomp）里做一遍适配；好处是 socket/连接类资源和跨版本经验比自研强 |
| **F. 不重建（冻结/续跑）** | 没有②③：SIGSTOP/cgroup freezer 冻住整棵树，"恢复"= SIGCONT | — | **零成本但沙箱必须还活着**：不能跨机、不能跨 worker 重启、占用内存；这就是 E2B 今天 `paused` 的语义 |
| **G. 应用级** | 镜像格式里已有的 `app_state`（不透明字节）+ 应用自己 dump/restore；或记录输入/系统调用流做 replay | — | 可移植、不碰内核，但要求应用配合、副作用要处理 |

**对 E2B 的建议**：要在**生产形态（chroot + real_root）**上落地，A+§11 是改动最小的路；
B 是"彻底不要 stub"的路（正中 §11 的病根，但要用一份"无 stray mapping + fd 表干净"的新证明
换掉 stub 自带的两个免费性质），值得作为备选做一次原型；C/E 都要付"脏地址空间"或大依赖的代价；
F 只能覆盖"沙箱还活着"的暂停。

### 11.5 B 路线的**正确形态**：不是"就地调用函数"，而是"把 stub 用 mmap 装进来再跳进去"

先说清楚最容易走歪的一点：**"在 supervisor 里调一个 restore 函数"这种进程内写法是不可行的**，
因为⑤"要执行的代码"恰好就是留在地址空间里的那部分 —— supervisor 是 PIE（随机基址）+ libc +
tokio + 堆，无法与镜像区域解耦。**可行的形态是**：supervisor 打开 stub、把它的 ELF 段
`mmap` 到**固定保留窗口**（`STUB_BASE`，stub 本来就是按这个基址链接的）、切到私有栈、跳进它的
入口 —— 也就是"**把 stub 的投递方式从 `execve` 换成 `fd + mmap`**"。这样：

* **两条 §11 的关一起消失**：不 `execve` ⇒ 没有路径解析、**也不需要给 stub 任何 Landlock 执行
  授权**（不 exec 就没有可授权的执行；maps 里的代码直接跑）。相比 §11 的"给 stub 宿主路径一条
  `EXECUTE|READ`"，B 在**规则集面上更干净**（不新增任何权限）。
* **干净地址空间这条还能保住**：`restore_blob::plan_sweep(current, cp)` 本来就是通用实现 ——
  它拿"子进程**当前**的 maps"减掉"镜像记录的 maps + `STUB_BASE` 窗口"算出要 unmap 的残留，
  stub 侧再执行；它不关心载荷是怎么进来的。exec 形态下 `current` 只有 stub 自己几页，
  mmap 形态下 `current` 是"fork 自 supervisor 的一整套映射"，机制不变、**规模变了**。

**代价与待验（六条，都要单独验收）**：

1. **sweep 规模**：`MAX_SWEEP 256` / `resume::MAX_SWEEP_ENTRIES` 是照 exec 形态定的；fork 形态
   下 glibc + tokio 的映射可能上百条，要实测并把上限当契约写死（超限必须报错，不能静默漏 unm）。
2. **多线程 fork**：子进程是 supervisor 的 fork（route B 的 supervisor 带 tokio/事件泵）。fork
   只保留调用线程，**别的线程持有的锁会冻结在锁住状态** ⇒ 载荷必须"零分配、不取锁"（现在的 stub
   天生如此；`init/mod.rs::child_fail` 的注释也写着"glibc 的堆在这条单线程回路上是 fork 安全的"）。
3. **fd 表不再免费清零**：`execve` + `CLOEXEC` 是白送的一步；mmap 路线要自己关掉 supervisor 的
   **整张**表（控制 socket、notify listener、trace fd…），再按镜像记录的原编号重开。E2B 刚因
   "trace fd 落进 workload" 修过一次同类问题 ⇒ 验收项应是"恢复后的 fd 表 == 镜像记录，且不含任何
   平台 fd"。
4. **文件后备区域自己开**：匿名页以外（text / so / 解释器）走 `RemapFromFile`，要由载荷在
   **Landlock 域内**按 plan 翻译过的虚拟路径打开（stub 今天也是这么做），保持"超出可读集就按
   plan 的规则处理"的现状。
5. **入口要小改**：stub 的 `_start` 依赖内核给的初始栈/auxv（只为 `AT_SYSINFO_EHDR` 取 vDSO 基址）。
   mmap 装机没有这套 ⇒ 加个 shim 合成最小 auxv，或把 vDSO 基址当参数传进去（镜像里本来就记了
   vDSO 的落点）。有界改动，约十几行。
6. **成本对比**：exec 形态付出的是"一次 execve + 一个二进制要可达"；mmap 形态付出的是"fork 一份
   supervisor 的 COW 地址空间 + 全量 sweep"。两者都要实测（页面数、时间、负载下的稳定性）。

**结论**：B 不是"更省事的 A"，而是把风险从"沙箱内能不能 exec 到 stub（路径 + 一条授权）"换到
"fork 出来的脏地址空间 + 全量 fd 表能不能清干净"。**规则集面上它更干净**（不加权限），机制上
`plan_sweep`/`restore_blob`/stub 主体都可复用 —— 所以值得做一个原型：在 `chroot + real_root(true)`
下用 mmap 装机恢复一次、断言"计数器前进 + 无 stray mapping + fd 表干净"，再把上面六条逐条量出来。

### 11.6 A（exec+fd+授权）与 B（mmap 装机）的实测对比（2026-09-23，B 原型已跑通）

原型（fork `d16daae`）按 §11.5 的形态实现并实测：真根下 no-exec 恢复**成功**（计数器继续前进，
`restore_skipped` 只有三个 stdio，恢复后 maps 通过"无 stray mapping"断言），无 chroot 形态同样成功
（这条 bisect 证明先前的 SIGSEGV 与"根"无关）。下面按你关心的两个轴给结论，数字都是本轮实测。

**① 功能完整一致性**

| | A：exec + fd 投递 + 一条授权（§11.1） | B：memfd 装机 + 跳入（原型） |
|---|---|---|
| 引擎语义 | 与现有 exec 路线**逐字节相同**（同一 blob/协议/sweep/rt_sigreturn） | 同上，**但多了三条必须成立的不变量** |
| 真根/chroot | 可用（这正是 §11.1 要修的） | 可用（实测：真根 61 maps / 45 sweep，计数器恢复） |
| 每次恢复的开销 | exec 一次 + 一个两页进程（实测 exec 路线 **17 maps / 1 sweep**） | fork 一份 supervisor 的 COW 地址空间 + 全量 sweep（实测 **61 maps / 45 sweep**，`MAX_SWEEP 256` 余量从 ~255 掉到 ~211） |
| per-task 内核状态 | exec 全部重置（白送） | **要约自己清**：实测 fork 继承 glibc 的 rseq 注册、而 sweep 把 rseq 区 unmap ⇒ 恢复后进程**第一条指令就 SIGSEGV**；关掉 `glibc.pthread.rseq` 同一轮直接通过 ⇒ 载荷现在显式 `rseq(NULL,0,0)` + 关 altstack（已修） |
| fd 表 | exec + CLOEXEC 白送干净 | **未修**：实测恢复出的进程表里有 `6->/memfd:sandlock-restore-stub` 与 **`22->anon_inode:seccomp notify`**（stub 镜像 + **supervisor 的 notify listener**），即"平台 fd 落进 guest"这一类 |
| 对 supervisor 进程状态的依赖 | 无（exec 后一切重置） | **有**：载荷跑在 supervisor 的 fork 上，进程里已有别的线程/测试状态时，实测**在 READY 之前就被 SIGSEGV 杀掉**（两种形态都复现）⇒ 原型用例已用 `SANLOCK_NOEXEC_PROTOTYPE=1` 显式开关，门禁保持绿（`core_integ 548`） |
| 引擎固有边界（两条路线相同） | 单线程恢复、socket/pipe/memfd 走 `restore_skipped`、同内核、x86_64/riscv64 | 同左 |

**结论（功能）**：A 的行为**与今天已经能跑的 chroot-free 路线完全一致**，只是把投递方式换成 fd
并补一条授权；B 能在真根下跑通，但要**额外满足三条不变量**，其中两条目前还没达标（fd 表未清、
进程状态敏感）。也就是说 B 的"功能完整"是**有条件的**，A 的是**无条件的**。

**② 安全风险**

| 风险面 | A：exec + fd + 一条授权 | B：memfd 装机 |
|---|---|---|
| 规则集变化 | **+1 条**：stub 的**宿主文件** `EXECUTE\|READ`（§11.1 第 5 条要求评审） | **零变化**（不 exec 就没有执行可授权；memfd 是匿名 inode，没有路径可判） |
| 新增"平台物"出现在沙箱内 | 只有 stub 自己的 4 个 LOAD 段（exec 后的干净镜像） | 载荷与 stub 都在 supervisor 的 fork 之上；**未清理前，supervisor 的 fd（含 notify listener）会留在恢复出的进程里**（实测） |
| 恢复后地址空间可审计性 | 白盒：只有镜像 + vdso + stub 窗口（现有 stray-mapping 断言） | 同样可断言（原型已过），但 sweep 规模大 45 倍，错误模式从"漏映射"变成"多 unmap ⇒ 崩" |
| 失败模式 | 失败=拒绝对话/超时（响亮） | 失败可能是**静默的部分清理**（需要断言兜住）；本轮实测的是响亮崩溃（SIGSEGV） |
| 对 workload 的额外暴露 | 一条"能执行一个平台二进制"的规则（guest 无法命名该路径，fd 表 stdio-only） | 无新增权限；但 **notify listener 泄漏**若不清，等于把 supervisor 原语交给 guest（严重，必须修） |

**结论（安全）**：**B 在"权限面"更干净（零新增规则），A 在"进程内面"更干净（沙箱里只多一个
已 exec 的静态 stub，没有任何继承物）**。B 目前那条 fd 泄漏（notify listener）在修好之前是**不可
接受的**（它正是 E2B 上一轮修过的同一类洞），修好之后 B 的规则集优势才成立。

**③ 建议（按你给的两个轴排序）**

1. **先做 A+§11.1**：功能与今天可跑的路线逐字节一致、无附加不变量，风险集中且可评审（一条
   `EXECUTE|READ`，对象是平台自己的静态 stub，guest 无法命名它）。满足"功能完整一致性"这一轴。
2. **把 B 作为并行推进的备选**，但要按顺序补齐三件事才有资格进入选择：① 载荷在跳转前**关掉
   未记录的 fd**（含 notify listener；验收=恢复后 fd 表 == 镜像记录）；② 解决"fork 自多线程
   supervisor"的敏感性（例如从**专门的单线程 helper 进程** fork，而不是从 supervisor 本体 fork）；
   ③ 为 sweep 规模设一个实测契约（当前 45/256，需给出上限与超限行为）。这三条做完再评 B 的
   规则集优势是否值这个复杂度。

#### 11.6.1 A 那条授权（stub 宿主路径 `EXECUTE|READ_FILE`）的安全评估

**先摆事实：它不是新增的权限种类。** fork 的 `fs_readable` 一律按 `READ_ACCESS` 安装，而
`READ_ACCESS = EXECUTE | READ_FILE | READ_DIR`（`landlock.rs:25`），`add_path_rule` 对非目录会把
目录类权限掩掉（`ACCESS_FILE`，`landlock.rs:32`）⇒ 落到文件上**正好就是 `EXECUTE|READ_FILE`**。
**纯形态（今天就能跑恢复的形态）其实已经在装这一条**：`restore_interactive` 里那句
`fs_readable.push(stub)` 在无 chroot 时就是宿主路径。§11.1 的改动只是让 **chroot 形态**把
**同一条**授权写在宿主路径上（现在它被 chroot 翻译成"rootfs 里不存在的路径"而被静默丢弃）。

**规则的内核语义（决定了风险边界）**：

| 维度 | 实际含义 |
|---|---|
| 对象 | **一个文件**（stub 构建产物），不是目录 ⇒ 没有子项，目录类权限被掩掉 |
| 权利 | 实测**最小集 = `EXECUTE \| READ_FILE`**：只给 `EXECUTE` 会被拒（EACCES）—— 内核把可执行文件
的段映射进来需要 READ_FILE；`EXECUTE\|READ_FILE` 下两种 exec 形式都通过 |
| 作用域 | 该沙箱**自己的** Landlock 域（每沙箱各自构建规则集，域随进程树消亡），被恢复出的程序及其
后代继承 |
| 祖先目录 | **不需要授**（§11.1 用 fd 投递）：实测只授权文件本身即可跑起来，路径形式在 chroot/
真根下依旧不可解析 |
| 不涉及 | 写、创建、删除、目录遍历以外的 REFER、seccomp、uid/caps、chroot/pivot 行为 |

**谁能用它**：生产形态（chroot/`real_root`）下 stub 的宿主路径**不在沙箱路径空间里**（guest 的 `/`
是 rootfs），workload 无法命名它；workload 的 fd 表只有 stdio（早前实测），stub 的 fd 全是 CLOEXEC
⇒ **实际只有恢复路径自己用得到这条权利**。纯形态里 guest 理论上能命名宿主路径 —— 但那正是**今天
已有**的状态，chroot 形态加这条不扩大纯形态之外的暴露。

**残余风险清单（逐条给可达性与影响）**：

1. **workload 若能命名该文件，就能执行它** —— 执行 stub 不构成提权：它跑在**同一个沙箱域**内
   （Landlock/seccomp/uid 都已就位），没有 blob/计划时只会 `_exit(2)`；即便伪造 blob，它能做的也只是
   "map 若干区域 + 跳到某地址"，与 guest 自己调 `mmap` 等价。影响：无提权，最多是可用性噪声。
2. **能读/映射 stub 二进制** —— 内容是平台自己的几 KB 代码，无秘密。影响：无。
3. **规则锚在"构建产物路径"上** —— 若该文件所在目录**可被宿主侧非特权用户写**，有人可以替换 stub，
   沙箱会执行被替换的文件。边界很窄：能替换的人已经在宿主上拥有比沙箱更大的权限，且被替换的代码仍在
   沙箱域内运行（Landlock/seccomp/uid 已装、CAP 已丢）⇒ 不会因此拿到宿主权限。**运维要求**：stub 所在
   目录不得对非特权宿主用户可写（当前是构建目录；要更严可迁到 worker 的受保护目录）。
4. **审计面 +1 条规则** —— 收法：用一条用例把它的**形状**钉住（只允许这一个文件、只允许
   `EXECUTE|READ_FILE`、只在需要它的形态里出现），漂移即红。
5. **与 B 的相对风险** —— B 不加规则，但原型实测把 **supervisor 的 seccomp notify listener** 留在了
   恢复进程的 fd 表里（`22->anon_inode:seccomp notify`）：那是**内核原语句柄**层面的暴露，比"一个文件的
   路径权限"高一个量级，也是"先做 A"的主要理由之一。

**评审清单（可直接对照）**：① 只授文件、不授目录；② 只授 `EXECUTE|READ_FILE`（实测最小集）；
③ 只在有 chroot 根的形态里加，纯形态沿用现有 `fs_readable`；④ 继续用 fd 投递，不回到路径 exec
（这样祖先目录一条都不用授）；⑤ 加"授权形状"用例（两种形态各跑一遍）；⑥ 运维确认 stub 目录不可被
非特权用户写。

**实现后复核（fork `a6f6b04`，2026-09-24）**：清单 ①②④⑤ 都按此落地（`fs_readable_host` 只放
stub 这一个文件、`add_path_rule` 对文件自动掩到 `EXECUTE|READ_FILE`、投递走 fd 6、用例
`test_restore_resumes_inside_a_chroot_root` 两种形态各跑一遍并断言 fd 表干净）。**新增一条实测
教训**：投递 fd 必须 `dup3(…, O_CLOEXEC)`——第一版用 `dup2` 时，stub 镜像以
`6 -> …/restore-stub` 留在了**恢复后的进程**里（通道 fd 3/4/5 相反，必须活到 stub 内部，故仍是
可继承的 dup2）。③ 与 ⑥ 留作运维/评审项（当前 stub 位于构建目录）。

### 11.7 架构前提：**线上是 aarch64 时，阻塞点在引擎，不在投递**

**状态（2026-09-24，S0–S5 全部落地后）**：下面这段是本轮**开始前**的盘点，它当时是对的，
现在只是历史。引擎移植已经做完（S0 spike → S1 capture → S2 帧/blob → S3 stub → S4 门槛与构建 →
S5 lane 与验收），**aarch64 上 checkpoint/restore 已经能跑**：本地 Lima qemu VM（真 6.14 内核、
真 ptrace、真 `rt_sigreturn`，不往任何线上节点推测试二进制）上 `integration::test_restore::`
4 passed / 0 failed，其中 `test_restore_resumes_inside_a_chroot_root` 两种根形态都过；fork 的五条
arm64 相位（含 C-ABI）与 x86_64 基线**逐条相同**（157/4/9/104/51，见本节末尾的 S1–S3 段、
下面的 S4/S5 状态，以及 `third_party/sandlock/docs/test-baseline.md` 的 arm64 段）。于是"投递
（A）还是装机（B）"现在**是个真问题**了，而 §11.6 的结论不变：投递本身没有架构假设，S3 落地
**没有让 A 返工**。逐阶段判据与证据在 fork 的 `docs/fork-plan-2026-09-aarch64-restore.md`。

（这条 lane 本身怎么搭、怎么跑、踩过哪些坑，见 `docs/cross-platform-lanes.md`；脚本与 VM
定义在 `deploy/scripts/arm-lane/`。）

**S5 的 arm64 两态（本节更新的一部分）**：`tests/security` 在 arm64 lane 上两态都绿 ——
`E2B_REAL_ROOT=0` **35 passed / 9 skipped / 4 xfailed**、`E2B_REAL_ROOT=1` **38 passed /
9 skipped / 1 xfailed**（同轮 x86_64 生产形态 lane：43/1/4 与 46/1/1；两边的 passed+xfailed
都是 39/47，差的那 8 条是 arm64 guest 没有 docker 守护进程/socket/CLI 导致的 skip）。
日志：`deploy/scripts/arm-lane/evidence/s5-arm-security-realroot{0,1}.log` 与 `deploy/scripts/arm-lane/evidence/s5-x86-security-realroot{0,1}.log`；
x86 那两条是用 `deploy/scripts/arm-lane/x86-security.sh` 取的（`test-prod-shaped.sh` 的 phase 1 跑整个
`tests` 树，无法只跑一个目录，见 pitfalls B4），镜像、cap 集、seccomp 档、registry mirror 与它
一致。arm64 的 `E2B_REAL_ROOT=0` 那一条没有重跑探针修复后的代码：探针唯一的调用点在
`if self._real_root and …` 里面，这个形态根本不会走到它。
真根形态比模拟根多出的 3 条通过，正是本文 N35 的那三条 shebang/binfmt 用例 —— 它们在没有真根时
是**运行期** `pytest.xfail()`，真根下走到断言并通过，所以 xfailed 从 4 降到 1。

**这一路在 aarch64 上抓到的最有价值的一条不是 fork 的**：`E2B_REAL_ROOT=1` 在 arm64 上**根本
打不开** —— worker 的 `_REAL_ROOT_PROBE` 把 `pivot_root(2)` 的号写死成 x86_64 的 155，而 aarch64
用 generic 表（`pivot_root` = 41，155 = `sched_getattr`），探针**从没问过内核 pivot_root**，
却把 ESRCH 报成"seccomp 档没放行"，把运维指向 seccomp。线上是 aarch64，所以这个开关在那之前
不可能被打开。已修（按架构派发 + 未知架构 fail closed），并加了只在 generic 表架构上会红的
单元钉（拿掉修复即 ESRCH 红，arm lane 实测）。细节见 `docs/build-test-deploy-pitfalls.md` B13。

**结论先说（历史，2026-09-24 之前）**：A（fd 投递 + 一条宿主授权）**没有任何架构相关代码** ——
`execveat`/`AT_EMPTY_PATH`、`fs_readable_host`、中介的放行分支都是 arch-neutral 的；而
checkpoint/restore **引擎本身只支持 x86_64 与 riscv64**。所以在 aarch64 上，"A 还是 B"这个问题
还不成立：**两条路都跑不了**，因为 `restore_interactive` 在第一步就按架构拒绝。

代码事实（本轮核对）：
* `sandbox.rs::restore_interactive` 开头 `cfg!(not(any(x86_64, riscv64)))` ⇒ 其它架构直接
  `Err("checkpoint restore is only implemented on x86_64 and riscv64")`；
* `checkpoint/restore-stub.c` 只写了 `__x86_64__` / `__riscv` 两套 syscall 号与入口，
  其它架构 `#error "unsupported architecture"`；`build.rs` 的 `is_restore_arch` 同样只含这两个
  架构，因此**非这两个架构上 stub 构建失败只是 warning**（wheel 照常出 aarch64，`stub_path()`
  指向一个不存在的文件）；
* `restore_blob.rs` 的 `STUB_BASE`、FP 帧常量、`rearm_restartable_syscall` 都按 x86_64/riscv64
  写；`capture.rs` 的寄存器捕获虽然在 `not(any(…, aarch64, …))` 里留了 aarch64 的名字，
  但一条 aarch64 路径都没被这两个架构之外的测试跑过；
* 我加的唯一架构代码在 **B 原型**里（`noexec.rs` 的跳转 asm，已按 x86_64/riscv64 隔离，其它架构
  `_exit(95)`）——而 B 在 aarch64 上本来也无从运行：它要跳进的 stub 在那里根本不存在。

**要在 aarch64 上线 checkpoint/restore，需要一个引擎移植（建议单独立项）**，清单：
1. `restore-stub.c` 增 aarch64 分支：syscall 号、`_start`（把入口 sp 交给 `_start_c`、切到 `.bss`
   私有栈）、`rt_sigreturn` 帧布局（arm64 `sigcontext` = `fault_address, regs[31], sp, pc,
   pstate` + FP/SVE 上下文，与 x86_64 完全不同）；
2. **线程指针**：arm64 的 TLS 在 `TPIDR_EL0`，**不在 `sigcontext` 里** ⇒ 捕获侧要读
   `PTRACE_GETREGSET(NT_ARM_TLS)`，恢复侧要在 `rt_sigreturn` 之前用 `msr tpidr_el0, xN` 写回
   （默认不被 `SCTLR_EL1.TIDCP` 陷入；需在目标内核上实测确认）；
3. `restore_blob.rs`：给 aarch64 一个 `STUB_BASE`（3 TiB 在 48 位 VA 内可用，需实测）、arm64 的
   FP 图像帧（`fpsimd_context` magic `0x46508001`、必要时 `sve_context`）、以及 arm64 版本的
   `rearm_restartable_syscall`（`svc #0` 之后的 pc/返回值语义）；
4. `sandbox.rs` 的架构门槛加 aarch64；`build.rs` 的 `is_restore_arch` 同步（让缺 stub 在 aarch64
   上变成**致命**而不是 warning）；
5. 验收必须在 **aarch64 lane** 上跑（当前 fork 门禁是 x86_64 容器）：至少
   `test_restore_glibc_vdso_program_resumes` + `test_restore_resumes_inside_a_chroot_root`
   的两种根形态，以及 `--oci-root` 的 C/R 用例；
6. 之后 E2B 侧那一半（镜像存储/属主/配额、pause/resume 生命周期、`restore_skipped` 语义）照
   §11.6 的清单走，与架构无关。

**对当前决策的影响（2026-09-24 更新）**：移植已经做完，所以这只剩 A/B 本身的选择，§11.6 的
结论直接生效 —— A 的投递改动全程**一行没动**（S3 改的全在 `#[cfg(target_arch = "aarch64")]` 里）。

**移植计划已落**：fork 仓 `docs/fork-plan-2026-09-aarch64-restore.md`（2026-09-24，提交
`17e4711`）—— 含代码坐标（capture/restore_blob/stub/门槛/构建）、**S0 三条 spike 先行**（手工
信号帧 `rt_sigreturn`、`TPIDR_EL0` 往返 + `NT_ARM_TLS`、`STUB_BASE`/vDSO 对目标内核）、
S1–S5 的阶段与每阶段判据、SVE/MTE-PAC 先 fail-closed 的处置、arm64 lane 的搭法与验收命令、
以及"S0 不成立就停在 S0、改走冻结/replay"的止损点。

**S0 已跑完（2026-09-24）**：fork 提交 `0f1c168`（`docs/arm-cr-s0-evidence.md` +
`spikes/arm-s0/`）。在线上同族的两个 aarch64 节点（Rocky 10.2 / `6.12.0-211.34.1.el10_2.aarch64`）
上逐字段实测，两节点一致：帧布局钉死为 `info@0 / uc@0x80 / sigcontext@frame+0x130`、
`regs[8]` 就是 syscall 号、fpsimd 记录 `magic 0x46508001 size 0x210 vregs@+16`、`__reserved@sc+0x120`；
**手工帧 `rt_sigreturn` 成功**且内核不碰 `TPIDR_EL0`（⇒ stub 必须自己写回）；EL0 能写 `TPIDR_EL0`
（TIDCP 未陷入），`NT_ARM_TLS` 经 ptrace 往返且子进程会读到新值；VA 上限 48 位、3 TiB 可用。
两处**要按实测改计划**：SVE 的 fail-closed 判据（本机普通 glibc 进程的 `NT_ARM_SVE` 本来就有
内容，按"有内容就拒"会把一切拒掉；判据最后定成**只看 `SVE_PT_REGS_SVE` 那一位**，见下）；vdso
搬迁必须**同 delta 搬 `[vvar]`+`[vdso]`**（只搬 `[vdso]` 实测 `SEGV_MAPERR @ new_vdso-0x4000`）。
**S1 已落地（2026-09-24，fork `40527d8`）**：capture 学会读 aarch64 的线程指针
（`NT_ARM_TLS`；x86_64/riscv64 返回 `None`），`ProcessState` 增加 `tls`、`IMAGE_VERSION` 2→3、
镜像新增 `process/threads/tls.bin`（aarch64 缺该字段的 v3 镜像直接拒绝）。方式是**本地 zig 镜像
交叉编译 + 推二进制到 aarch64 节点跑**（recipe 与"宿主 linker 要钉回 cc"的坑写在 fork 的
`docs/arm-cr-s0-evidence.md` §7）。证据：aarch64 上 `checkpoint::` 子集 32 passed / 0 failed
（改前 28），x86_64 同子集 40 passed / 0 failed。

**S2 已落地（2026-09-24，fork `6a6fdf2`）**：`restore_blob` 的 arm64 分支 —— `STUB_BASE` 在
aarch64 上复用 x86_64 的 3 TiB（S0c 实测可 mmap、48 位 VA）；FP 图像做成 `rt_sigreturn` 要的
`fpsimd_context` 记录（把 ptrace 的 `user_fpsimd_state` 重排并丢掉两个内核保留字，长度不是 0/528
就拒）；`rearm_restartable_syscall` 在 arm64 上是**空操作**（外加"`x0` 里还留着重启哨兵就
fail-closed"）。最后一条是本阶段最有价值的实测：新探针 `spikes/arm-s0/s0e-restart.c` 在目标内核
上量到，`PTRACE_INTERRUPT` 停点**之前**内核已经把 `pc` 掰回 `svc`、把 `x0` 还原成原参数，`x8`
里还是原 syscall 号（内核注释原文 *"so that a debugger will see the already changed PC"*）⇒
x86_64 那套"`pc` 减 4、重载 `orig_rax`"在 arm64 上会执行到 `svc` 的后半条，而 `orig_x0` 又不在
`user_pt_regs` 里、根本重载不了。capture 侧另加了 SVE 的 fail-closed（读 `NT_ARM_SVE`，只有
`SVE_PT_REGS_SVE` 位表示向量寄存器真的活着才拒）：**S0 写的 `vl>16` 那半条判据被撤回**，因为
`vl` 是线程 VL、在有 SVE 的机器上默认等于系统 VL，用长度判会把 Graviton 上每一个普通进程都拒掉。
证据：aarch64 `checkpoint::` 子集 33 passed / 8 failed（8 条全是**本地 lane 的 qemu-user 不实现
`ptrace`/`process_vm_writev`**，新增的帧布局/哨兵/FP 长度/SVE 判据全绿），x86_64 同子集
40 passed / 0 failed。

**S3 已落地（2026-09-24，fork 本轮提交）**：`restore-stub.c` 的 `__aarch64__` 分支与
`build.rs`/`sandbox.rs`/`resume.rs` 的架构门槛都在，并且**第一次在真内核上端到端跑通** ——
本节开头那句"两条路都跑不了"从此只作历史：aarch64 上引擎已经能恢复进程。

代码面（改动全在 `#[cfg(target_arch = "aarch64")]` 里，x86_64/riscv64 路径未动）：
* `restore-stub.c` 的 aarch64 分支：通用 64 位 syscall 号（read63/write64/close57/lseek62/
  mmap222/mprotect226/munmap215/mremap216/dup3 24/exit93/openat56/rt_sigreturn139，**没有
  `arch_prctl`**、dup 改 `dup3`）；`sc6()` = `x8` + `x0..x5` + `svc #0`；`_start` 把入口 sp（auxv 在
  其上）交给 `_start_c` 再切到 `.bss` 私有栈；步骤 10 在栈上建 `struct rt_sf`，**一次 `memcpy`**
  覆盖 `regs[31]/sp/pc/pstate`（ptrace 交回的顺序就是帧顺序），FP 记录整段写 `__reserved`，帧偏移
  按 S0 表 1 加 `_Static_assert` 钉死；**步骤 9 `msr tpidr_el0`** —— `TPIDR_EL0` 既不在寄存器
  文件里也不在信号帧里，只能这样写回。
* `build.rs`：`is_restore_arch` 含 aarch64（那里"缺 stub"从 warning 变**致命**）；链接地址按
  工具链分派（GCC `-Wl,-Ttext-segment=`、zig/clang `-Wl,--image-base=`，实测 zig 拒绝前者）；
  `-mcmodel=large` 只有 x86_64 需要。
* `restore_blob.rs`：`HEADER_LEN` 64→80、`BLOB_VERSION` 2→3，header 末尾带 `tls/has_tls`；
  aarch64 两条 fail-closed（FP 记录不是 528 字节、`tls` 缺失都直接拒，不是降级）。
* 门槛：`sandbox.rs` 与 `tests/integration/test_restore.rs` 的架构门加 aarch64，`STUB_BASE` 复用
  3 TiB（S0c 已实测）。

**lane 变了**：S1/S2 是靠"本地交叉编译 + 推二进制到 aarch64 节点"取证的，S3 起改成本地
**Lima qemu VM**（Ubuntu 24.04 + 6.14 内核，宿主 amd64 Darwin）—— 真内核、真 ptrace、真
`rt_sigreturn`，**不再往线上节点推任何测试二进制**。四条搭 lane 的硬约束（不要写 `networks:`、
guest 内核 ≥6.10、工作区必须在 guest 本机文件系统、`/tmp` 重启即清）写在 fork
`docs/arm-cr-s0-evidence.md` §7。

**真内核第一次跑就抓到两个 RED**（都是 qemu-user 下测不到的东西）：
1. **测试自己写错了机器码**：aarch64 合成镜像用例里 `movk x2, #0x4500, lsl #48` 想构造
   `STACK + 0x800`，而 `STACK = 0x4500_0001_0000` 需要 `lsl #32`；载荷于是落到映射外，
   `SIGSEGV {si_code=SEGV_MAPERR, si_addr=0x4500000000010800}`。能一眼定位是因为
   `rt_sigreturn` **本身已经成功**（`rt_sigreturn({mask=[]}) = 0` 之后 pc 已落在 `CODE`）⇒
   帧、寄存器、`svc` 全对，错的只是载荷常量。
2. **FPCR 的 bit5 在模拟出的 CPU 上不可写**：原断言 `FPCR = 0x20`（bit5/IDE）实测写进去读回 0，
   而 `0xc00000`/`0x1000000` 正常往返。断言改用 **RMode = 0b11**（`0x00C0_0000`，用
   `1.0 + 2⁻⁶⁰` 的舍入行为钉住）：断言一个该实现丢掉的位等于在测 CPU 模型，而 RMode 是每个
   aarch64 实现都必须实现的字段。

**证据（全部本地，2026-09-24）**：
* RED：真内核 `checkpoint::` 子集 **45 passed / 1 failed**（上面第 1 条）。
* GREEN（aarch64 真内核）：`checkpoint::` **46 passed / 0 failed / 0 skipped**。关键一条
  `restore_stub_reconstructs_a_synthetic_image` 这次真的执行了 stub，payload 从恢复出的帧里读回
  `TPIDR_EL0 = 0x0000ffff88881000`、`FPSR = 0x10`、`FPCR = 0x00c00000`，逐位相等。
* GREEN（aarch64 真内核，端到端）：`integration::test_restore::` **4 passed / 0 failed** ——
  `test_restore_glibc_vdso_program_resumes`（vDSO 搬迁 + 寄存器/FP + fd 重开 + `rt_sigreturn`）
  与 `test_restore_resumes_inside_a_chroot_root` 的两种根形态都过；两条 noexec 原型按架构自述跳过。
* GREEN（x86_64 回归）：同子集 **42 passed / 0 failed**。

> "0 skipped" 是刻意查的：`stub_path()` 是编译期烘焙的绝对路径，路径不存在时那三条 stub 用例会
> **静默 `return`** —— 第一轮真内核的"46/46"就是这么来的假绿，真 SIGSEGV 被它盖住了。

剩下的两步（S4 门槛/构建、S5 lane 与验收登记）按 fork 计划继续，验收仍须在 arm64 lane 上做；
§11.6 里 E2B 侧那一半（镜像存储/属主/配额、pause/resume 生命周期、`restore_skipped` 语义）
与架构无关，不受影响。

**S3 之后：arm64 lane 首轮全量（2026-09-24，全部本地）**。把 fork 的 `core_lib` 整档搬到 arm64
真内核上跑，**904 passed / 0 failed**（x86_64 同档 **902/0**；四相位门禁也全绿：非 root 八档
对基线零漂移、`--oci-root` **157**、`--supervise-root` **4**、`--mediation-2uid` **9**）。首轮是
**899/4**，四条红**全部**落在"按架构写死的表"上，其中一条是**产品 bug**：

* **`struct epoll_event` 在 aarch64 上是 16 字节、`data` 在偏移 8**（x86_64 才是 packed 的 12/4；
  两边都用 C 探针量过）。`network/readiness.rs` 把 `12/4` 写死了 ⇒ **入站映射**（拦截
  `epoll_ctl(ADD|MOD)` 并合成 `epoll_wait` 结果那条路）在 aarch64 上读错半个记录、写回内核不认的
  记录。真内核上 RED 到位：解析出 `data = 0x0b0a090800000000`，真值是 `0x0f0e0d0c0b0a0908`。
  **这条与 C/R 无关、但与生产同源** —— 线上 worker 是 aarch64，任何走 epoll 入站映射的负载都会
  踩到它。已在两架构上 GREEN（fork `5937df0`）。
* `sys/path_surface.rs` 的三个横切检查按 x86_64 的**名字**比对，而 `chroot_path_syscalls()` 本就是
  **数字**表（`arch::sys_*()` 在 generic ABI 上返回 `None`）：aarch64 上 `open/stat/...` 根本不存在
  ⇒ 改成"按数字比对 + 每个 ABI 相关行由 arch helper 双向背书"。产品行为未变（fork `8a7e225`）。
* `NON_PATH_SYSCALLS` 补上 32 位 time64 组：`syscalls` crate 的 aarch64 表在 403..422 带了这些
  名字，内核在 64 位 ABI 上并不实现，原先被穷尽性检查当成"新 syscall"。
* `core_integ` 在 arm64 上**还没绿**：**506/42**，42 条同属 `net_fixture` 家族（http_acl /
  net_isolate / 网络注入 / named unix socket），缺的是**容器入口的 root prep** —— fixture 自己
  在报错里点名 "run the test container entrypoint (root prep) so the unprivileged fixtures
  exist"（那一步负责 seed `/etc/hosts` 与 `198.18.0.0/15` 地址）。已按"未收口"写进 fork 的
  `docs/test-baseline.md`，**没有**假装成基线。
* E2B 侧 `tests/security` 的 arm64 两态**尚未跑**，原因如实记下：本地 `e2b-sandlock-test` 镜像是
  amd64、fork 的 wheel 只出了 cp314、而 guest 是 python 3.12。下一步二选一：给 lane 装 docker 并
  载入 arm64 镜像（prod worker 镜像本来就是 arm64），或给 wheel 加 cp312 腿。

lane 本身也修了一处**取证陷阱**：Lima 的 9p 挂载在宿主原地重写文件后仍给 guest **旧内容**
（宿主 6→29 字节，guest 侧内容与 mtime 都不动，12 秒后依旧），rsync 的快速检查因此什么都不复制、
lane 会拿上一次的二进制跑出"结果"。现在源码走 tar-over-ssh、产物走 `limactl copy`，9p 只做随手看；
**依然不往线上节点推任何测试二进制**。
