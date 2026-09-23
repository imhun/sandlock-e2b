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

## 1. 实测（`e2b-sandlock-test:latest`，prod-shaped lane，root worker + route B 槽位）

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

## 5. 选项 A：真根（mount ns + pivot_root）

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
  - 注入它的 fd ⇒ 那就是"宿主 inode 上的 exec"，**今天的实测结果就是静态 ELF 那条 EACCES**
    （§1），除非**先修 §7 的规则翻译**；
  - 或者把它也拷成 memfd ⇒ 能跑，但 python 会从 `/proc/self/exe`/`argv[0]` 判断自己**不是 venv**，
    于是静默用错解释器/site-packages —— 比报错更糟。
* 仍然漏掉：`binfmt_misc` 之类其它"内核侧解析"（今天同样在宿主空间）。

### 6.4 成本与收益

**无部署改动、局部、可测**：改动集中在 `handle_chroot_exec`（+ 一个新 shebang 解析函数与
一组形态用例），落 B 之前先落 §7。它能让"镜像解释器的脚本"（`pip3`、npm shim、`~/.local/bin`
里 shebang 指向镜像 python 的那种）今天就跑起来，代价是 §6.3 的第一条。

## 7. 第三条路：`fs_writable` 的 Landlock 翻译是个**应当单独修的 bug**

`fs_writable` 是宿主拼写，却在 chroot 形态被 `chroot_root.join(...)` 翻译 ⇒ 规则根本没装上
（§2.2）。修它（例如给"宿主拼写"和"虚拟拼写"分开，或让 fork 对不存在的翻译结果回落到原路径）
带来的直接收益是：**workspace 里的静态 ELF 立刻能跑**，而且它是 B 的 venv 分支的前置。
它**不**能单独解决 shebang：解释器 `/bin/sh` 在宿主空间解析，规则若覆盖宿主 `/bin`
就是把**宿主解释器**放进沙箱（会看到 `py (3, 14)` 这种"宿主版本"），那是 C 路线，与
`test_image_rootfs_cannot_reach_host_filesystem` 钉住的隔离相冲突 —— 这也是为什么 shebang
必须由 A 或 B 来解。

## 8. 建议与决策点

1. **先落 §7**（独立 bug 修，收益明确、无部署改动、无形态风险）。
2. 然后 **A 还是 B 取决于两个问题**：
   * 产品是否要求"沙箱里的文件系统就是普通文件系统语义"（`$0`、`sys.path[0]`、静态二进制、
     `mountinfo`、任何内核侧解析）？若是 ⇒ **A**，并接受一次 seccomp 档/安全评审的部署改动。
   * 若只是要尽快解锁 console script（且能接受 `$0` 变 fd 路径的写作差异）⇒ **B**，
     作为止血，并在文档/发布说明里写明这条差异。
3. **不建议 C**（放宽规则覆盖宿主解释器目录）：它把宿主二进制当镜像二进制用，是静默的
   语义替换，与隔离目标冲突。

## 9. 未结线索（不算结论）

1. **两次 30 s 卡死，未复现**：最初两轮（trace 开、前面已跑十来条腿）里，20 次"写 + 拒绝 exec"
   的循环卡在 `access(2)` 通知上（supervisor 侧空闲）；随后同一段命令在独立容器里重跑
   （`loop` 单独、`script,loop`、`loop+binloop`、`sloop`）都是几秒完成。
2. **`timeout` 包一层就变味**：`timeout 10 /usr/local/bin/pip --version` 报
   `timeout: failed to run command '/proc/self/fd/5': No such file or directory` —— 中介把
   路径改写成 `/proc/self/fd/N` 之后，**由上层工具自己再 exec 一次**的形状会撞上这个改写。
   这条在 A 下自然消失，也是 A 的收益之一。
