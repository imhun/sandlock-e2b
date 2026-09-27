# N14 的第二半：退役"模拟根"，以及它会不会放大风险

**结论先说：不会放大 —— 对 `Open 桶`里最危险的那两条反而是"关闭"。**
但"简化"能删的东西比"3492 行中介"小得多，有两处**不是安全中性的**（§3），
而它们决定了这件事只能分阶段做（§4）。

> **引用约定（2026-09-27 更新）**：正文里的 `tmp/**`（`.log`、`tmp/k0s/task12/` 等）都在仓库
> `.gitignore` 里（**不是仓库路径**）。可重跑脚本已迁到 [`deploy/scripts/acceptance/`](../deploy/scripts/acceptance/)
> （原名不变）；`tmp/**.log` 一律是**原始日志**（会被清、可重跑，脚本见 `deploy/scripts/acceptance/`）。

---

## 0. 这件事是什么

N14 当初的收益写的是一句话：**路径空间变成内核不变量 ⇒ 拦截清单的完整性不再承担安全职责**。

真根（mount ns + `pivot_root`，`E2B_REAL_ROOT=1`）2026-09-25 已上线（见
`docs/deploy-clusters.md` §7），"内核自己解析路径"这一半兑现了。但**模拟那套一行没退**：
`chroot/dispatch.rs` 3492 行 / 27 个 handler、`procfs.rs` 1951 行、`chroot/resolve.rs` 714 行，
在默认形态下仍承担安全职责（`docs/open-issues.md` N14）。

本文回答两个问题：**退役它会不会放大风险**、**能退的到底是什么**。

---

## 1. 实测（2026-09-25，线上 k0s 车队，真根开，发布档在位）

全部在**生产沙箱里**跑，不是推演：

| 探针 | 结果 | 说明 |
|---|---|---|
| `chdir("/var/lib/e2b-sandboxes")` | **ENOENT(2)** | 该路径在**节点上真实存在**（1777 的 workspace base）。内核根若还是宿主，这一步必然进得去 |
| `chdir("/var/lib/k0s")` | ENOENT(2) | 宿主 k0s 状态目录，同样够不着 |
| `chdir("/var/lib/e2b-images")` | ENOENT(2) | 宿主镜像缓存 |
| `chdir("/workspace")` | **OK** | 正向对照：沙箱自己的挂载点在 |
| `cat /proc/kcore` | EACCES(1) | Landlock 仍在拦 |
| `mkdir /tmp/m && mount -t tmpfs none /tmp/m` | EPERM(1) | 工作负载仍不能挂载 |
| `unshare -m true` | EPERM(1) | 工作负载仍不能自建 mount ns |
| `listmount(458)` / `statmount(457)` / `fchmodat2(452)` | **ENOSYS(38)** | `Open 桶`的三条，部署形态下够不着 |

**第一行是整件事的关键**：`chdir` 在真根下是**透传给内核**的（`dispatch.rs:2797`，
注释写着"模拟形态做不到、真根里 guest 的拼写本来就是对的"）。所以它测的正是
"一个中介不翻译的路径，解析落在哪里"——答案是**沙箱自己的树**。

---

## 2. 为什么在安全上是单向的

两种形态的差别不是"谁拦得多"，而是**安全论证落在一个不变量上，还是落在一个内核事实上**：

| | 模拟根（默认关真根时） | 真根（现在线上） |
|---|---|---|
| 内核根 | **仍是宿主 `/`** | 沙箱自己的 rootfs |
| 未拦截的路径调用 | 在**宿主路径空间**解析 | 在**沙箱自己的树**里解析 |
| 安全依赖 | "拦截清单是完整的"——一条**代码从未检查过的散文不变量** | 内核的 root + mount ns |
| 已经出过的事 | OBS-1：`chroot(2)` 不在清单里 ⇒ 沙箱能 `chroot` 到宿主目录（`path_surface.rs` 文件头自己记着这条） | —— |

`path_surface.rs` 的文件头把话说得很直白：那句
*"只有 chroot 翻译过的路径会被加进规则，宿主路径不加，所以任何 seccomp fallthrough 都被
Landlock 拦（fail-closed）"* —— **使"拦截清单完整"成为一个安全不变量，而代码从没检查过它**。
真根把这个前提从"论证"换成了"内核事实"。

**还有一条更具体的**：`statmount`/`listmount` 这类**不取路径、只取 mount id** 的调用，
Landlock 对它们**说不上话**（没有 inode 可判）。能挡住它们的只有一样东西：**调用者有没有自己的
mount ns**。模拟根形态下沙箱与容器共享 mount ns ⇒ 它们枚举的是**容器的挂载表**；
真根形态下沙箱有自己的 ns ⇒ 只枚举自己的。**这一条是真根关掉的泄露，不是它引入的。**

（部署形态下它们还额外被挡在运行时之外——上表最后一行 ENOSYS。）

---

## 3. 两处不能被"简化"带走的东西 —— 这是真正的边界

**① `/proc` 是中介合成的，且只能这样。** 真根下内核**没有** procfs 可挂：在自建 userns 里
挂 procfs 三种形态实测全 EPERM（无 pid ns / 有 pid ns / 挂在自建 tmpfs 上），
真 procfs 只能特权方挂（`docs/chroot-workspace-exec.md` §9.6 第 2 条）。
所以真根下**内核看到的 `/proc` 是空的**，guest 看到的每一件 `/proc` 内容都来自 `procfs.rs`。
⇒ **凡是带 `/proc` 分支的 handler 都必须留着那个分支**，否则 `/proc` 直接消失。

**② 磁盘活账本是从拦截点采样的。** `dispatch.rs` 里有 9 处 `ctx.mark_dirty(...)`，
外加 `inject_watched`（持有写描述符）与 `settle_closed_writes`（exec 前结账）——
账本"知道刚写了多少"靠的就是这些拦截点。退掉它们，账本失去观察点，
而账本是 N25/N31/N32 的闸门（OBS-5 说得很清楚：pure 形态没有磁盘硬上限，
正是因为活账本依赖中介）。**这属于功能回归，不是路径逃逸**，但它是"简化"必须先回答的问题。

---

## 4. 于是"简化"的真实形状

**不是"删掉中介"**（策略、COW、账本仍然需要它），也不是"把 27 个 handler 删成 5 个"
（§3 的两条钉住了一大批）。真实形状是：

> **让真根成为唯一形态，然后删掉"替内核造一个根"的那部分代码**
> ——路径改写（`/proc/self/fd/N`、buffer 重写）、magic link 重写、
> 按虚拟路径做的策略翻译、以及 13 个 `legacy_*` 转发。

前提是**每个交付形态都得能吃真根**：image-rootfs 已可（线上在跑）；pure 有**两条线** —— N15 的
identity 翻译（`chroot_root="/"`，默认，`E2B_PURE_ROOTFS=off`）与 N16 的合成根
（`E2B_PURE_ROOTFS=synth`，普通目录 + bind + `pivot_root`，见 §5.3）。**过渡期两种形态并存 ⇒ 翻译代码在过渡期不能删**——
否则关了开关的部署会立刻退回"未拦截即在宿主解析"，那才是真正的放大。

### 4.1 另一条路：按形态分路径（只有**模拟根**走翻译）

既然过渡期不能删代码，那有没有比"等 pure 吃完真根再删"更好的做法？
有：**不删，改为在真根下让 handler 直接 `Continue`，把翻译留给模拟根。**

先把名字摆正，这是最容易搞错的一处：

| 形态 | 路径中介 | 走翻译吗 |
|---|---|---|
| **pure**（无 base image / 无 rootfs） | **没有**（`sandlock.py`：*"auto keeps the pure (no-chroot) shape in-process: it mediates nothing"*） | **不走**——它连中介都不起，正是 N15 那 33 条 `PURE_UNGATED` 的来处 |
| **chroot + 模拟根**（`E2B_REAL_ROOT=0`） | 全量翻译 | **走** |
| **chroot + 真根**（生产） | 内核解析；中介只做自己的宿主侧工作 | **也在走**（这才是不该的） |

所以"把 pure 和真根分开、只有 pure 走兜底"这个方向是对的，但**兜底服务的不是 pure，是模拟根的
那个配置**。

**机制已经存在，不用发明**：`exec` 与 `chdir` 早就按 `ctx.child_is_pivoted(pid)` 分路径
（`dispatch.rs:1649` / `2797`），真根下直接 `Continue` 把 syscall 交回内核；`getdents`
更是**无条件** `Continue`（它的注释：fd 已由 open 注入成宿主目录，内核直接列即可）。
所以这条路是"把已有的做法推广到别的 handler"，不是新机制。

**边界由"宿主侧工作"划，而不是由"是不是只读"划。** 逐 handler 过一遍
（`/proc` 分支 / 账本 `mark_dirty` / COW / fd 注入），不可放行的是那些**翻译之外还做事**的：

| handler | 为什么不能只 `Continue` |
|---|---|
| `open` | COW + 写监控 + fd 注入，三者都在这里 |
| `write` | 追加记账（N25 的 pushed appends）与 COW |
| `stat` / `statx` | **沙箱写过的东西必须报 COW 副本的元数据**，内核看到的是原件 |
| `readlink` / `xattr` / `utimensat` | 同上（COW 视图） |
| `exec` | 已分路径，但真根下仍要记账（`settle_closed_writes`）与写虚拟 exe |
| 13 个 `legacy_*` | 大多是薄转发，**但它们是否触达账本要读代码定**，不能靠 grep 断言 |

**S3 第一步已做（2026-09-25，fork）：放行了三个候选里的一个 —— `getcwd`。**
`ctx.child_is_pivoted(pid)` 为真时直接 `Continue`，翻译只留给模拟根。它是三者中唯一
**纯翻译**的那个，读代码确认过：

* handler 里没有策略判定，只有"把记录下来的 cwd 写回子进程缓冲区"；
* 真根下内核给的 cwd 就是沙箱自己的拼法（任务的根就是 rootfs），而且**比记录更准** ——
  内核会解析符号链接（经由软链进入的 cwd 报规范路径，与容器一致），handler 那道 ERANGE
  检查（拿"记录下来的拼法"量长度）也随之消失；
* 代码与验收：fork 的 `handle_chroot_getcwd` + `test_getcwd_under_a_real_root_is_the_kernels_answer`
  （判别性已证：把判据强制为假 ⇒ 第三段断言红在 `OK /alias`）。

**另外两个候选读完否掉了**（这正是"边界由宿主侧工作划"的意思，两者都不是只做翻译）：

* `inotify_add_watch`：它在翻译之外还做 `can_read(&virtual_path)` 的**策略判定**，而真根下
  策略只以 `fs_mount` 表 + Landlock 表达（`realroot.rs`：只挂 `fs_mount`）——**嵌套 deny
  （挂载点内部再 deny，例如 `fs_deny("/workspace/mnt/data")`）挂载集根本表达不了**，全靠
  逐 syscall 的这一句兜着。放行它等于把那条 deny 从 inotify 这条路上拿掉 = 真实的放大。
  它的另一半（翻译）确实冗余了，但省下的只有一次 `dup_fd_from_pid` + 注册，不值这条风险。
* `statfs`：唯一的工作是"虚拟路径 → 宿主路径 → `libc::statfs` → 写回缓冲区"，但
  `resolve_chroot_path_existing` 会经过 `canon_proc_self`/`canon_proc_cwd`，而 `/proc` 的
  目录 fd 是在 `open` 里**合成**的（§3 那条）⇒ 对 `/proc/...` 的可见性，真根内核与中介
  给的答案不同（内核看到的是 rootfs 里那个空目录）。放行不会扩大权限，但会让 `/proc`
  下的 `statfs` 换一个语义，属于"没验收就先改行为"。

**下一步（S3 余下）**：把上面的判定方法推广到 13 个 `legacy_*` 与 `/proc` 家族 —— 判据是
"翻译之外还做不做事"，而不是"看起来像不像直通"。每个放行都要在 `E2B_REAL_ROOT=0/1`
两态下留下 security 套件的结果。

### 4.2 判据跑完全家族的结果：S3 就到 `getcwd` 为止（2026-09-25）

按同一判据把 `chroot/dispatch.rs` 里每个 `handle_chroot_*` 过了一遍（按函数体数"翻译之外
还调了什么"），结论是**没有第二个可放行的**：

| 家族 | 翻译之外还做什么 | 能不能放行 |
|---|---|---|
| `open`（791 行）、`write`（463 行） | COW + 写监控 + fd 注入（open）；追加记账 + COW（write） | ❌ |
| `exec`、`chdir` | 账本收尾（`settle_closed_writes`）／cwd 记账 | 已分路径（真根下 `Continue`，记账仍在） |
| `stat`/`statx`/`readlink`/`xattr`/`utimensat` | 策略判定 + **COW 视图**（沙箱写过的东西必须报副本元数据） | ❌ |
| `getdents`、`fchdir` | 无（`getdents` 早就无条件 `Continue`，`fchdir` 只跟记账） | 已放行 |
| `inotify_add_watch`、`statfs` | 策略判定（`can_read`）／`/proc` 合成路径 | ❌（理由见 §4.1） |
| 12 个 `legacy_*`（`open`/`stat`/`lstat`/`access`/`readlink`/`unlink`/`rmdir`/`mkdir`/`rename`/`symlink`/`link`/`chmod`） | **什么都不做**：11–19 行的参数重排，转交给对应的 `*at` handler | ⚪ 不是"候选"——它们没有自己的翻译可退役，转交目标不放行它们就不能放行 |
| `legacy_chown` | **不是壳**：207 行，11 处策略判定（自己实现了一套 chown 语义） | ❌ |

所以 S3 的真实边界是：**真根下"纯翻译"的 handler 只有 `getcwd` 一个**。剩下的中介工作不是
翻译，而是策略（Landlock/挂载集表达不了的那部分）、COW 视图与磁盘活账本 —— 那三样**不能靠
分路径消掉**，只能靠 S4（账本换观察点）与 S5（模拟形态整体退役）解决。这也回答了"简化那半
会不会放大安全风险"：**分路径本身不放大**（放行的 `getcwd` 里没有策略），但**按名字猜着放行
会**（`inotify_add_watch` 就是那个例子）。

**这条路买到什么、买不到什么**（说清楚，免得当成安全改进）：

* **买到**：真根下这些 syscall **不再执行翻译代码** ⇒ 翻译里的 bug 到不了生产；内核直接给答案，
  省一次中介往返；而且**每个 handler 都可分别验收**，因为 security 套件本来就跑两态
  （arm64 lane 的 `E2B_REAL_ROOT=0/1` 两份日志）。
* **买不到**：代码不会变少（两个分支都在）；**安全上也不加分**——那是 S1 已经交付的（§5）。

---

## 5. S2 的答案：pure 能不能走真根？——**能，但要给它合成一个 rootfs**

S2 原本问的是"pure 形态能不能也吃真根"。分三层答，每层都有实测。

**① 今天不行，因为 `real_root` 要求有 chroot root。** 没有它直接 `fail!`
（`context.rs`：*"real_root requires a chroot root (the image rootfs)"*），而 pure 从不设 chroot
（`sandlock.py:2001` 那句 `kwargs["chroot"] = …` 只在 `base_image and image_rootfs` 分支里）。

**② "把 chroot 设成 `/` 再开真根"也不行**，两条路都试过（`deploy/scripts/acceptance/probe-pure-realroot.py`，
特权容器、照抄 `realroot::build` 的顺序）：

| 做法 | 结果 |
|---|---|
| 自绑 `/` 再 `pivot_root(".", ".")` | **EBUSY** —— 不能 pivot 进自己已经在的根 |
| 把 `/` 绑到**另一个路径**再 pivot | 成功，但 `/src`（宿主独有）**仍然看得见** ⇒ 这不是隔离，只是把同一个树重新挂了一次 |

**③ 但合成一个 rootfs 就可以——而且那就是 pure 被允许看的那些路径。**
tmpfs + 绑几个系统目录再 pivot：`/src` **看不见了**（新根 ino=1，是 tmpfs）。

### 5.1 这条为什么比"能不能"更重要

pure 今天的 `Landlock` 白名单是 `fs_readable = ["/usr", "/lib", "/bin", "/opt"]` +
`fs_writable = [workspace]`（`sandlock.py:1830`；`/` 只在 chroot 分支被加进去）。
也就是说 **pure 沙箱能看到的，正好就是那几个系统目录加自己的 workspace**。

而它剩下的缺口是 `PURE_UNGATED` 那 33 条 —— `stat`/`lstat`/`statx`/`statfs`/`access`/
`readlink`/`chdir`/`chmod`/xattr/`inotify_add_watch`…… **都是 Landlock 表达不了的路径访问**，
于是它们对着**宿主根**解析（`test_pure_shape_inotify_still_reaches_the_host_root` 钉着这条残余）。

**给 pure 合成一个 rootfs，内容就是那"几个系统目录 + workspace"，等于把今天的可见集合原样
搬进一个内核根**：沙箱能看到的**一个都不少、一个都不多**，但那 33 条**从"够得着宿主"变成
"在沙箱自己的树里解析"** —— 缺口不是被"拦住"，是**不存在了**。

N15 选的路是"补一个中介 + identity 翻译，把那 33 条一条条闸住"，比这重得多，而且只是**闸住**。
所以这条不只是"S2 的答案"，它是 **N15 的一条替代路线**：用一次 rootfs 合成，换掉 33 条闸门。

**要付的账（都还不知道答案，得实测，不能推演）**：

* **`/proc`**：pure **没有中介**，所以合成根里要么挂真 procfs、要么没有 `/proc`。
  文档里已有的实测是"自建 userns 挂不了 procfs（三形态全 EPERM）"，但那是**沙箱自己**挂；
  由 worker（容器内 root）在 pivot 前挂好、再让沙箱继承，是另一条路，**没试过**。
* **`/dev`**：chroot 形态有 `minimal_dev` 六节点，pure 今天用的是容器自己的 `/dev`。
* **别的东西**：pure 现在还能走 `/etc`（DNS）等路径吗？白名单说不能 —— 这与"N15 要闸住它"是一致的，
  但真要合成根时，**每个真实用法都得过一遍**，不能只看白名单。

### 5.2 于是生产那边

**两套生产清单都设了 `E2B_BASE_IMAGE`**（k8s 那份钉着 digest，compose 那份从 env 取），
所以生产**永远是 image-rootfs 形态**，也就永远是已经吃上真根的那个形态。pure 是开发/本地形态。

**推论**：既然生产的路径封闭靠的是**内核根**，那么

> **S1（真根上线）已经把 N14 的收益交付了。**

`chroot_path_syscalls()` 的完整性在生产里**不再承担安全职责** —— 它现在为
`E2B_REAL_ROOT=0` 的部署兜底（defense in depth），而生产里"漏一条拦截"的后果从"在宿主路径空间解析"
变成了"在沙箱自己的树里解析"。

于是"退役模拟"这件事的性质变了：**它不再是安全改进，而是代码卫生**（并且被
`E2B_REAL_ROOT=0` 这个配置挡着）。要不要做它，取决于愿不愿意为"少维护一套模拟"付重构代价，
而**不做不再是欠一道防线**。

### 5.3 已选路线与实测（2026-09-26，N16）

§5.1 那三条"都还不知道答案"的前提（`/proc`、`/dev`、"还有没有别的真实用法"）本轮全部量过了，
所以这一节写的是**已选的路线**，不是推演。

**选了什么**：`E2B_PURE_ROOTFS=synth`（**2026-09-27 起是默认**；`off` 是退回杆，见下）时，pure 沙箱拿到
**每沙箱一份的合成骨架** `<base>/_pure_rootfs/<id>`（普通目录，`0755`，名字进了
`gateway_common.paths.RESERVED_PLATFORM_NAMESPACES`），骨架里 bind 系统目录
（`/usr /bin /sbin /lib /lib64 /opt`，源不存在的跳过）与**整棵容器的 `/dev`**；真正的挂载由 fork
在沙箱自己的 mount ns 里做（`unshare → 绑策略挂载 → 递归自绑根 → pivot_root → 丢 CAP_SYS_ADMIN`），
workspace 仍以 `/home/user`、`/workspace` 两个别名进树，拆箱时骨架与 `<base>/_runtime/<id>`
一起收掉。

**为什么骨架是"每沙箱一份 + 平台保留命名空间"**：`_runtime/<id>` 是 `0700`，而
`unshare`/`bind`/`chdir` 是在沙箱**自己的 user namespace 里、以沙箱自己的宿主 uid** 跑的 ——
它不拥有那些目录（`0700` 会先在 bind 处撞 EACCES）；而放进沙箱自己的树，又会被它自己删掉。
代价明说：每沙箱多一个顶层目录 + 一次拆箱清理。

**`/dev` 取哪个集合**：递归 bind 容器的 `/dev`，也就是**把今天 pure 的可见集合原样搬进树**
（`devdiff` 量到集合相等：14 / 14）。**不**取镜像形态的 `minimal_dev` 六节点 —— 那是一次收紧
（实测少 8 条：`fd`、`full`、`mqueue`、`random`、`shm`、`stderr`、`stdin`、`stdout`）。
顺带一条边界：骨架的 `/proc` 是**空目录**（§5.1 那条"自建 userns 挂不上真 procfs"的实测），
所以那四个指向 `/proc/self/fd` 的符号链接在合成根下是**悬空**的 —— 形状保留，能否解析是中介
`/proc` 合成的活。

**`/etc` 有意不绑**：骨架里它是空目录。绑上就等于把宿主的名字表重新灌进沙箱，正是 N15 修掉的
那条 wildcard 绕过（`compose_virtual_etc_hosts` 的 `root="/"` 特判就是为它加的）；同理也不绑任何
凭据目录。

**两处 fork 特例在合成根下重新变成"活分支"**（不是新 bug，是判据的前提换了）：

| fork 特例 | `root="/"`（identity，N15） | 合成根（N16） |
|---|---|---|
| `compose_virtual_etc_hosts` 的 `if root != "/"`（`network/rules.rs:665`） | 特判跳过 —— `root="/"` 时读的是**宿主** `/etc/hosts`，会把宿主可解析的名字以字面 IP 灌进沙箱 | **活分支**：读的是**骨架**的 `/etc/hosts`，骨架没有这个文件 ⇒ 回落到 loopback 基线，**与今天等价** |
| 凭据暴露警告的 `.filter(\|root\| root != "/")`（`sandbox/builder.rs:1261`） | 特判跳过 —— `root="/"` 会让**任何**宿主路径都算"在授权内"，警告变成噪音 | **活分支**：按**骨架**的授权集判定，骨架里没有任何凭据文件 ⇒ **不产生噪音** |

前提写在明处：**合成根绝不绑宿主 `/etc` 或凭据目录**。绑了这两条就从"活分支回到等价行为"变成
**真漏洞**（前者把 wildcard 绕过灌回来，后者把凭据放进沙箱的授权集）。今天这条约束由骨架常量
`_SYNTHETIC_ROOTFS_SYSTEM_DIRS` 表达（里面没有 `/etc`），并由上表两处判据守着。

**两态**（`E2B_REAL_ROOT` 的读法按 2026-09-26 的追加裁定：`=0` ⇔ N15 identity、
`=1` ⇔ 合成根 + 真根）：两态与四档全量（gate A / gate B off / 合成根 / phase 2）的逐档数字、
基线与差，**唯一权威表在 `docs/pure-shape-decision.md` §7**，本节不再复述数字（两份各自带数字的
表迟早有一份是错的）。原始 lane 日志在 gitignored 的 `tmp/k0s/n16-*.log`，日志若被清以 §7 的数字为准。
**`E2B_PURE_ROOTFS=synth` 配 `E2B_REAL_ROOT=0` 没有"这一档"**：那个组合结构性不成立
（bind 只在真根那条路径上发生，模拟形态把虚拟路径翻译进**空骨架** ⇒ 生成期 `execvp("/bin/sh")`
errno 13 ⇒ container 崩塌 ⇒ 之后每个 verb 都答 `InstanceClosed`），所以 `create_app` 当场 loud
拒绝、exit 1（原句见追加裁定与 Task 13 报告 §2）。**一条验收注记（Task 12）**：
`E2B_PAUSE_CHECKPOINT` 默认**关**，所以合成根下 pause/resume 的第一次探针是**空洞的**（pause 不写图、
`resume` 面对活会话走 thaw 分支 ⇒ restore verb 一次都不调）；真跑恢复链的是"用 worker 自己的两个
入口 + 把 restore stub 指到树外"那一版，两态两轮都 `restored: true`、恢复后仍能 exec（`tmp/k0s/task12/`）。

**这也不是"pure 要维护第二套根"**：合成根**只改 `kwargs["chroot"]` 那一个值**，中介一行不退
（`/proc` 合成、策略判定、COW 视图、磁盘活账本全在中介里，见 §3）。它还顺手答了 §6 里 S5
（"真根成为唯一形态"）的前提：每个交付形态都能吃真根之后，就没有 `E2B_REAL_ROOT=0` 兜不住的形态了。

**（2026-09-27 更新：默认已切，S5 的前提就此成立）** —— `E2B_PURE_ROOTFS` 默认从 `off` 翻到
`synth`，且 `E2B_REAL_ROOT` 未显式设置时**跟着合成根走**（`envd_service/config.py::resolve_real_root`）：
于是**每一个默认交付形态都装真根**（image 形态本来就走真根那条路；pure 形态现在也有骨架可 pivot，
`E2B_REAL_ROOT` 的存在与否不再是一个交付形态的分叉）。合成根 + 显式 `E2B_REAL_ROOT=0` 这对仍然被守卫
当场拒绝（那句错误信息里带着退路）。要回到模拟根只有两条显式路径：`E2B_PURE_ROOTFS=off`（pure 形态）或
`E2B_REAL_ROOT=0`（image 形态）；S5 的"没有 `E2B_REAL_ROOT=0` 也能全绿"因此变成了**默认档**的验收口径。
代价与影响面逐处列在 `docs/pure-shape-decision.md` §7 的「默认已切（2026-09-27）」一节。

---

## 6. 阶段与验收（每阶段独立可验）

| 阶段 | 做什么 | 验收 |
|---|---|---|
| S1 | ✅ **已完成**：真根成为线上形态，并写进清单（**收益已交付**，见 §5） | `kubectl diff` 为空；两形态对照表（`deploy-clusters.md` §7） |
| S2 | ✅ **已回答**：pure 走真根**可行但要合成 rootfs**，而那份 rootfs 的内容正好是它今天的 Landlock 白名单 ⇒ 这同时是 **N15 的一条替代路线**（一次合成换掉 33 条闸门） | 结论与实测见 §5；三个探针 `deploy/scripts/acceptance/probe-pure-realroot.py`（A=EBUSY、A2=同树无隔离、B=合成根真隔离） |
| S3 | **已跑完（2026-09-25）**：真根下的 handler 改成 `Continue`（沿用 `exec`/`chdir` 已有的 `child_is_pivoted` 判据），翻译只留给模拟根。**放行了 `getcwd`，其余全家族读完后否掉**（§4.1 的三个候选 + §4.2 的判据表）——真根下"纯翻译"的 handler 只有它一个；剩下的中介工作不是翻译，而是策略 / COW 视图 / 磁盘活账本，归 S4 与 S5 | **已验收（`getcwd`）**：fork 的 `core_integ` 559（+1 新用例，判别性已证）+ 两态 security 套件 —— `E2B_REAL_ROOT=0` **43 passed / 1 skipped / 4 xfailed**、`=1` **46 passed / 1 skipped / 1 xfailed**，与改动前的基线逐字相同（crate 侧 `test_chroot` 51 / `test_instance_exec` 28 / `test_cow` 26 / `test_restore` 5 全绿）。不可放行的那批（`open`/`write`/`stat`/`statx`/`readlink`/`xattr`/`utimensat`）**保持不变**，每个否掉的都在 §4.1/§4.2 写明为什么 |
| S4 | 账本换观察点（或证明周期扫描足够），再退写拦截 | 磁盘门禁的单测与集群验收不变 |
| S5 | 真根成为**唯一**形态，模拟那套整体退役。**注意这是代码卫生，不是安全改进**（§5） | **默认档已经是真根**（2026-09-27 起 pure 默认也是合成根 + 真根 ⇒ "没有 `E2B_REAL_ROOT=0` 也能全绿"成立）；模拟形态只剩两条显式退路（pure 的 `E2B_PURE_ROOTFS=off`、image 的 `E2B_REAL_ROOT=0`），它们才是"退役"要清掉的最后两个入口 |

> **（2026-09-27 更新：S4/S5 仍未做 —— `docs/open-issues.md` N14 的"简化那半未做"就是这个）** ——
> `chroot/dispatch.rs`、`procfs.rs`、`chroot/resolve.rs` 全套仍在位，真根"退役模拟"的收益（拦截清单
> 不再承担安全职责）**在生产里已由 S1 交付**，所以这一步现在只是**代码卫生**、不是欠一道防线；
> 它被 `E2B_REAL_ROOT=0` 这个配置挡着（**2026-09-27 之后**：pure 形态要看模拟根，需要同时写
> `E2B_PURE_ROOTFS=off` 与 `E2B_REAL_ROOT=0`；只写后者会被 `PURE_ROOTFS_WITHOUT_REAL_ROOT_ERROR`
> 拒绝 —— 也就是说模拟根现在**只出现在显式声明的地方**）。要不要做、什么时候做，按 §7 的账单独评估。

**S3 之前不要动翻译代码**：现在删除任何一条，都会在 `E2B_REAL_ROOT=0` 的部署上
把"被拦截"变成"在宿主解析"。

---

## 7. 不做会怎样

保持现状 = 继续背"清单完整"这条不变量。它并非无人看守：`path_surface.rs` 的
`every_arch_syscall_is_classified` 会在内核/`syscalls` crate 新增系统调用时变红，
`tests/unit/test_worker_manifest_permissions.py` 钉住 worker 档只放宽了
`pidfd_getfd`/`unshare` 与 N35 那一对。所以**不做是"欠一笔简化"，不是"欠一道防线"**。
真正决定它的，是 S2 能不能低成本落地 —— 那才是把真根从"可选形态"变成"唯一形态"的前提。

---

## 8. 出处

* 真根与两种根形态：`docs/chroot-workspace-exec.md` §5、§9.6、§9.7
* 线上部署与两形态对照：`docs/deploy-clusters.md` §7
* 拦截清单与 `Open 桶`：fork `crates/sandlock-core/src/sys/path_surface.rs`
* 真根泄漏用例：`tests/security/test_real_root_mounts.py`、`test_real_root_denials.py`
* 索引条目：`docs/open-issues.md` N14、backlog N14 行
