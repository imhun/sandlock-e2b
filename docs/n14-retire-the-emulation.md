# N14 的第二半：退役"模拟根"，以及它会不会放大风险

**结论先说：不会放大 —— 对 `Open 桶`里最危险的那两条反而是"关闭"。**
但"简化"能删的东西比"3492 行中介"小得多，有两处**不是安全中性的**（§3），
而它们决定了这件事只能分阶段做（§4）。

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

前提是**每个交付形态都得能吃真根**：image-rootfs 已可（线上在跑）；pure 形态是 N15 那条线
（`chroot_root="/"` 的 identity 翻译）。**过渡期两种形态并存 ⇒ 翻译代码在过渡期不能删**——
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

**② "把 chroot 设成 `/` 再开真根"也不行**，两条路都试过（`tmp/k0s/probe-pure-realroot.py`，
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

---

## 6. 阶段与验收（每阶段独立可验）

| 阶段 | 做什么 | 验收 |
|---|---|---|
| S1 | ✅ **已完成**：真根成为线上形态，并写进清单（**收益已交付**，见 §5） | `kubectl diff` 为空；两形态对照表（`deploy-clusters.md` §7） |
| S2 | ✅ **已回答**：pure 走真根**可行但要合成 rootfs**，而那份 rootfs 的内容正好是它今天的 Landlock 白名单 ⇒ 这同时是 **N15 的一条替代路线**（一次合成换掉 33 条闸门） | 结论与实测见 §5；三个探针 `tmp/k0s/probe-pure-realroot.py`（A=EBUSY、A2=同树无隔离、B=合成根真隔离） |
| S3 | **进行中（第一步已做，2026-09-25）**：真根下的 handler 改成 `Continue`（沿用 `exec`/`chdir` 已有的 `child_is_pivoted` 判据），翻译只留给模拟根。**已放行：`getcwd`**（理由与另外两个候选被否掉的原因见 §4.1）。余下：13 个 `legacy_*` 与 `/proc` 家族逐个读代码定，真正删代码要等 S5 | **已验收（`getcwd`）**：fork 的 `core_integ` 559（+1 新用例，判别性已证）+ 两态 security 套件 —— `E2B_REAL_ROOT=0` **43 passed / 1 skipped / 4 xfailed**、`=1` **46 passed / 1 skipped / 1 xfailed**，与改动前的基线逐字相同（crate 侧 `test_chroot` 51 / `test_instance_exec` 28 / `test_cow` 26 / `test_restore` 5 全绿）。每个新放行的 handler 都要这样留两态结果；不可放行的那批（`open`/`write`/`stat`/`statx`/`readlink`/`xattr`/`utimensat`）**保持不变**，并在提交信息里写明为什么 |
| S4 | 账本换观察点（或证明周期扫描足够），再退写拦截 | 磁盘门禁的单测与集群验收不变 |
| S5 | 真根成为**唯一**形态，模拟那套整体退役。**注意这是代码卫生，不是安全改进**（§5） | 没有 `E2B_REAL_ROOT=0` 也能全绿 |

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
