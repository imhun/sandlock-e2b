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

能放行的候选（读到确认的）：`getcwd`（真根下内核给的 cwd 就是对的）、`statfs`、
`inotify_add_watch`（历史上那条 OBS 泄露正是因为它在宿主路径空间解析，真根下由内核解析）。
**注意**：`/proc` 那一路不能靠"这个 handler 没有 /proc 分支"来判断——`/proc` 的目录 fd 是
在 `open` 里合成的，所以 `getdents`/`statfs` 对 `/proc` 的可见性依赖上游。

**这条路买到什么、买不到什么**（说清楚，免得当成安全改进）：

* **买到**：真根下这些 syscall **不再执行翻译代码** ⇒ 翻译里的 bug 到不了生产；内核直接给答案，
  省一次中介往返；而且**每个 handler 都可分别验收**，因为 security 套件本来就跑两态
  （arm64 lane 的 `E2B_REAL_ROOT=0/1` 两份日志）。
* **买不到**：代码不会变少（两个分支都在）；**安全上也不加分**——那是 S1 已经交付的（§5）。

---

## 5. S2 的答案：pure 形态**结构上**吃不了真根 —— 于是 S1 已经把收益交付了

S2 原本问的是"pure 形态能不能也吃真根"。读了代码，答案是**不能，而且是结构性的**：
`real_root` 的前置就是"必须有一个 chroot root"，没有它直接 `fail!`
（`crates/sandlock-core/src/context.rs`：
*"real_root requires a chroot root (the image rootfs)"*）。pure 形态按定义没有 image rootfs，
没有东西可以 `pivot_root` 进去 —— 给它"真根"就等于给它一个 rootfs，那是另一个功能，不是配置。

而**两套生产清单都设了 `E2B_BASE_IMAGE`**（k8s 那份钉着 digest，compose 那份从 env 取），
所以生产**永远是 image-rootfs 形态**，也就永远是能吃真根的那个形态。pure 是开发/本地形态。

**这条推论比 S2 本身重要**：既然生产的路径封闭靠的是**内核根**，那么

> **S1（真根上线）已经把 N14 的收益交付了。**

`chroot_path_syscalls()` 的完整性在生产里**不再承担安全职责** —— 它现在是为
`E2B_REAL_ROOT=0` 的部署和 pure 形态兜底（defense in depth），而生产里"漏一条拦截"的后果
从"在宿主路径空间解析"变成了"在沙箱自己的树里解析"。

于是"退役模拟"这件事的性质变了：**它不再是安全改进，而是代码卫生**（并且被 pure 形态挡着——
只要 pure 还要跑，翻译代码就得留着）。要不要做它，取决于愿不愿意为"少维护一套模拟"付重构代价，
而**不做不再是欠一道防线**。

---

## 6. 阶段与验收（每阶段独立可验）

| 阶段 | 做什么 | 验收 |
|---|---|---|
| S1 | ✅ **已完成**：真根成为线上形态，并写进清单（**收益已交付**，见 §5） | `kubectl diff` 为空；两形态对照表（`deploy-clusters.md` §7） |
| S2 | ✅ **已回答**：pure 形态**结构上**吃不了真根（没有 rootfs 可 pivot）⇒ 枢纽从"让 pure 吃真根"变成"**pure 还要不要存在**" | 结论见 §5，依据是 `context.rs` 的前置与两套生产清单都设了 base image |
| S3 | **优先走 §4.1 的分路径**：真根下的 handler 改成 `Continue`（沿用 `exec`/`chdir` 已有的 `child_is_pivoted` 判据），翻译只留给模拟根。真正删代码要等 S5 | 每个改动过的 handler 在 `E2B_REAL_ROOT=0/1` 两态下 security 套件全绿；不可放行的那批（`open`/`write`/`stat`/`statx`/`readlink`/`xattr`/`utimensat`）**保持不变**，并在提交信息里写明为什么 |
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
