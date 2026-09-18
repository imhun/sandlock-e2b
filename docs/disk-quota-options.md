# 磁盘配额方案评估（共享存储为前提，2026-09-18）

> 背景：compose 线停用后，k8s 主线**没有每沙箱磁盘硬限**（backlog N25）。
> 前提由运维确认：**共享存储必须保留**（多副本、迁移、N13 那套都建立在它上面）。
> 本文回答：在这个前提下，有哪些**不受存储类型限制**的配额实现，以及各自代价。

## 1. 先把「配额」拆成三个不同的需求

混在一起谈，很容易得出「没有 XFS 就没办法」这种过强的结论。实际是三件事：

| | 要防什么 | 需要什么粒度 |
|---|---|---|
| **R1 保护共享卷本身** | 一个沙箱把 50 GiB 卷写满，连带影响同一 base 上所有 worker | 卷级水位即可，**不需要每沙箱** |
| **R2 每沙箱可预测容量** | API 里的 `disk_size_mb`/`diskMB` 要可兑现；超了应当写不进去，而不是封顶邻居 | 每沙箱**计量 + 执行点** |
| **R3 准入** | 不把超过卷容量的量卖出去 | **卷级台账**（不是每节点各记一份） |

## 2. 现状（先把事实摆出来）

| 能力 | 现状 |
|---|---|
| 准入 | **两层**：① 全局 `E2B_MAX_TOTAL_DISK_MB`（**代码默认 10240**，Σ 活沙箱 `disk_mb`）；② 每节点 `E2B_NODE_DISK_MB`（这台 2×8192 MB = 16 GiB）。②在共享卷下是"各记一份的预算"，只有①代表"这片 slice 一共卖出去多少" |
| 监控 | 心跳带 `diskUsedMB`/`diskTotalMB` —— `shutil.disk_usage(workspace_base)`，即**整卷**（每个 worker 报同一个数）；`quota_maintenance` 按比例算 `diskWarnCount`/`diskErrorCount` |
| 有没有动作 | **没有**。warn/error 计数只进节点视图，没有任何消费者（`control_plane/api/internal.py:95-100` 只是收下） |
| 每沙箱用量 | **没有**。XFS project quota 表是唯一来源，NFS 形态不存在；agent 形态要求 agent 跑在存储服务端，托管 NAS 放不了 |
| 每沙箱硬限 | **没有** |

所以今天在 k8s 上，**既没有每沙箱的"量"，也没有每沙箱的"限"**，只有整卷的水位（而且没人看）。

## 3. 存储侧方案：我们的存储其实**支持**（最便宜，建议先用）

阿里云 NAS 的**目录配额**（通用型 NFS；OpenAPI `SetDirQuota` / `CancelDirQuota`）：

* `QuotaType=Enforcement` 是**硬限**：超限后「创建文件或目录、追加写入等操作失败」——
  正是 E2B `disk_size_mb` 的语义（写不进去），而且**由服务端执行**，worker 零改动、零特权、零热路径；
* 还能限 `FileCountLimit`（文件数）——**inode 耗尽同样是整卷故障**，这条我们现在连 XFS 形态都没做；
* 粒度 = NAS 文件系统里的**绝对路径目录**，与我们的 `<base>/<sandbox_id>` 一一对应（深度 1）；
* ACK 的 CSI 已经用同一能力做「NAS 卷子目录配额」，所以这是产品化过的路径，不是野路子。

**必须写进容量规划的约束**：

1. **每个文件系统最多 500 个目录配额、最大深度 8 层** ⇒ 每沙箱一份配额时，**并发沙箱上限 = 500**
   （可用多个文件系统分片、或把额度下放到 tenant/项目级来规避）；
2. `SizeLimit` 的单位是 **GiB 整数**，而我们的 `diskMB` 是 MiB（常见 512/1024）⇒
   要么向上取整到 GiB、要么明确"目录配额只做上限兜底、精细额度另算"；
3. **只有通用型 NAS 支持** —— 这台是不是通用型要你确认一次（极速型不支持目录配额）；
4. 需要 RAM 凭据（`nas:SetDirQuota`）。这台 ECS **目前没挂 RAM 角色**（见
   `deploy/k8s-k0s/storage-nas.yaml` 的注释），所以要给角色或把 AK/SK 放进 secret。

> 结论：**如果这台是通用型，R2 的硬限今天就能落地**，且是唯一「硬 + 零运行时成本」的选项。
> 代价是把它变成一项**云侧依赖**（CP 要能调 NAS OpenAPI），以及 500 目录这个容量边界。

## 4. 不受存储类型限制的方案（本文的正题）

| 方案 | 硬/软 | 成本 | 关键风险 / 为什么不行 |
|---|---|---|---|
| **A. 写路径记账**：fork 侧拦 `write`/`pwrite64`/`writev`/`pwritev2`/`ftruncate`/`fallocate`/`copy_file_range`/`sendfile`/`splice`，按字节累加、超限返回 ENOSPC | **硬**（对覆盖到的 syscall） | **高：热路径税** | 这些正是沙箱最高频的 syscall，现在**全部不在中介集合里**（`third_party/sandlock/crates/sandlock-core/src/sys/path_surface.rs:368` 的 `NON_PATH_SYSCALLS` 明确列着 `write`/`pwrite64`/`writev`/`ftruncate`/`fallocate`），把它们拉进通知/fd 传递路径＝给每个 I/O 加一次 supervisor 往返（该仓库量过同类代价：裸形态 +80~90 µs/次）。另需一份 syscall 面账本（同 N15 方法论），漏一条就是绕过 |
| **B. 增量测量**：`inotify`/`fanotify` 事件 + 只对**变化过**的文件 `stat` 增量；再周期扫沙箱进程的 open-write fd，覆盖"一直开着写"的长写者 | 软（发现即停） | 低：不扫全树 | 事件面同样要账本（create/close_write/rename/link/mknod/mmap…）；**mmap 写不产生事件，但它无法增长文件**（增长必须走 `write`/`ftruncate`/`fallocate`），所以"增长"仍可覆盖；停箱语义 ≠ ENOSPC |
| **C. 周期全树扫描 + 超限停箱** | 软（窗口内可超额） | 中：O(文件数)，与 GC 扫描同源可复用 | 大步长写者能在两次扫描之间超额（例：500 MB/s × 30 s = 15 GiB） |
| **D. 卷级水位闸门**：`statfs`（极便宜）+ 停止准入/告警；必要时按已测用量挑最大占用者停 | 软（兜底） | **极低** | 不提供每沙箱额度 —— 但它是 **R1/R3 的正确实现**，与 R2 用什么机制无关 |
| **E. 每沙箱镜像文件 + `loop` 挂载**：把工作区放进一个定长镜像文件，由内层文件系统给出硬限 | 硬 | 高（见 §4.1 实测） | 密度与吞吐都要付钱，且要把"扫树/迁移/GC/模板"整套改成"挂镜像"；需要新权限面 |
| **F. 每沙箱独立卷**（CSI/PVC/子目录配额） | 硬 | 高（数千对象） | 运维复杂度高；且 CSI 的子目录配额本质仍是**存储侧能力**，绕回第 3 节 |
| **G.（排除）cgroup v2** | — | — | cgroup 只有 IO 带宽/IOPS，**没有空间配额**。常见误解，先关掉这条 |
| **H.（排除）稀疏文件"占位"预分配** | 在本地 FS 上成立 | 低 | **NFS/CephFS 上稀疏文件不保留块** ⇒ 占位无效，而我们的存储正是 NFS |

一句话：**「硬 + 不受存储限制 + 便宜」三者不可兼得。** 要硬就得有人在写路径上拦（A/E，代价大），
要便宜就只能测量（B/C，有窗口），要么就把执行点交给存储（第 3 节）。

### 4.1 loop 镜像这条路，实测（2026-09-18，`.94` 上挂着同一个 NAS export）

**机制**（就是下面这五步，没有别的）：

1. 在共享卷上给每个沙箱建一个**定长镜像文件**（`truncate -s <disk_mb>M sbx.img`）；
2. worker 把它挂成块设备（`losetup --find --show --direct-io=off` —— 对 NFS 后端建议关掉
   direct IO，尽管这台实测**默认也能挂**）；
3. 在镜像里建一个**内层文件系统**（`mkfs.ext4`）并挂载；
4. 沙箱的工作区就是**这个挂载点**（chroot / bind 进去）；
5. **配额 = 内层文件系统的容量**。它物理上不可能超过镜像大小，块分配器到顶就返回 ENOSPC
   —— 不需要任何计量、不需要审计写路径、也不依赖存储支持什么。

**实测数字**：

| 观测量 | 结果 |
|---|---|
| 挂载可行性 | `losetup` 默认与 `--direct-io=off` **都成功**（`/dev/loop0`、`/dev/loop1`）；`mkfs.ext4` + `mount` 正常 |
| 限额是不是硬的 | 32 MiB 镜像里写 64 MiB：`dd: error writing '…': No space left on device`，**exit=1**，写进 25 MiB 就停 —— 与 E2B `disk_size_mb` 的语义完全一致 |
| 镜像在共享卷上是不是**瘦**的 | **不是**。`truncate -s 256M` 之后 `du` 直接就是 **256 MiB**（NAS 立刻分配）。写多少都不再变 |
| 吞吐（同一 NAS，128 MiB + `fsync`） | 镜像内 **0.50 s** vs 普通文件 **0.37 s** ⇒ 约 **+35%** |

**两个结论**：

* **好的一面**：正因为镜像不瘦，**它同时把 R1/R3 也解决了** —— 每个沙箱在共享卷上真的
  占住自己那份，`truncate` 失败（卷满）就是天然的卷级闸门，不需要额外台账。这是它相对
  "B/C 只测量"的实质优势。
* **坏的一面（也是为什么不推荐首选）**：
  1. **密度**：50 GiB 卷 ÷ 1 GiB 沙箱 = **50 个并发沙箱**（还比 NAS 目录配额的 500 目录上限更紧）；
     而且**每个空沙箱也占满自己的额度**；
  2. **吞吐**：实测 +35%（顺序大块 + fsync；小文件随机写通常更差）；
  3. **权限面**：worker 需要 `mount` + `/dev/loop-control`。A6 已删 `CAP_SYS_ADMIN`，
     seccomp 里 `mount` 家族只在有 SYS_ADMIN 时放行、`pivot_root` 根本不在允许集里
     （N14 已把这条路径的量做掉：无新宿主特权可行，但**必须**改 profile）；pod 还要能看到
     `/dev/loop*`（新设备面）；
  4. **模型冲击**：树变成**不透明镜像** ⇒ 现有"扫树"（reconcile 的磁盘扫描、孤儿 GC、
     fleet-wide 围栏）、模板/snapshot 的 `copytree`、以及跨节点迁移（要 `umount`→`mount` +
     独占协调）全都要重做；worker 崩溃还会留下**残留挂载与泄漏的 loop 设备**，需要新的清理路径。

即：**loop 镜像是"存储什么都不支持"时的兜底**，能力上成立、语义上最正；但在我们这台 NAS 上，
它比第 3 节的目录配额**更贵**（密度 5× 差距 + 35% 吞吐 + 权限/机制改造），所以顺序是
「目录配额 → B/C+停箱 → loop 镜像」，而不是反过来。

## 5. 推荐（组合，按优先级）

1. **确认这台 NAS 是不是通用型**。是 → 用**目录配额**做 R2 的硬限：
   CP 建箱时 `SetDirQuota(Enforcement, SizeLimit≈disk_mb 取整 GiB[, FileCountLimit])`，
   删箱时 `CancelDirQuota`；并接受 **500 目录/文件系统** 的并发上限（或把额度下放到 tenant 级）。
2. **无论 1 是否成立，都补 D（卷级水位闸门）** —— 它保护的是共享卷本身，与 R2 的机制正交。
   这需要把「卷」变成一等记账对象：**共享存储下，磁盘准入不该是每节点一份声明预算，
   而应是卷级台账**（CP 侧一份：卷容量 + 已售出 + 实测 used；实测可从任一 worker 的
   `diskUsedMB/diskTotalMB` 取，因为它本来就是整卷的数）。动作：WARN → 停止建箱（503 且信息明确）
   → 必要时用 B/C 的测量挑最大占用者停。
3. **若 1 不可行且必须硬** → 走 **B**（增量测量 + 停箱），并把语义写进 API 文档与错误信息
   （"配额是准入与守护，超限会被暂停/终止"），**不要**做 A（性能）与 E（特权 + 语义破坏）。
4. **把文件数（inode）也纳入配额语义**：目录配额有 `FileCountLimit`；XFS 路径可设 inode 限。
   inode 耗尽与容量耗尽一样会让整卷不可用。

## 6. 判据（做完怎么算数）

### 5.1 卷级闸门到底慢不慢、信号好不好（2026-09-18 实测）

**成本：不慢，而且这次 syscall 本来就在发生。**

| 位置 | `statvfs` 单次 |
|---|---|
| 共享 NAS（`/var/lib/e2b-sandboxes`） | **2.2 ms**（p50 2.2 / p95 2.5 / max 4.0） |
| 节点本地 hostPath（XFS） | 1.3 µs |
| 容器 overlay（`/`、`/tmp`） | ~1 µs |

而 worker 的心跳**每 5 秒**已经调一次 `shutil.disk_usage(workspace_base)`
（`envd_service/agent.py::_heartbeat_usage_payload`）—— 所以闸门真正增加的是**一次比较**，
不是一次 syscall。就算让它自己 1 Hz 轮询，2.2 ms/s 也只是单核的 0.2%，仍然不慢。

**但慢不是这里的风险，"在请求路径上同步读"才是**：PV 的挂载参数就是
`hard timeo=600 retrans=2`（`storage-nas.yaml`），NAS 不可达时一次 statfs 会**无限重试**。
所以闸门必须**消费心跳已经采到的值**（后台任务挂住不影响建箱）；要是必须现场读，就放线程 +
超时 + 缓存。省下的那 2 ms 不重要，挂住一个建箱请求才是事故。

**新鲜度：立刻可见，没有缓存。** 往共享卷写 256 MiB 后，**下一次** statfs 的差值就是
`256.0 MiB`；删掉也立刻回收。配合 5 s 心跳 ⇒ 陈旧度 ≤5 s —— 对一个"人类尺度"的准入闸门
完全够（它管的是"别再卖出去了"，不是"立刻掐掉正在写的那个"）。

**⚠ 但这次测量推翻了"用 statfs 比例当水位"这个前提：**

```
pod 里 df /var/lib/e2b-sandboxes : nfs4  10P 总  553G 已用  1%（那是整个 NAS 文件系统）
控制面节点视图（两个 worker 报的是同一个数）:
    usedDiskMB       = 565,738      (≈0.5 TiB)   ← 全 NAS 的用量
    diskTotalMB      = 10,737,418,240 (≈10240 TiB) ← 全 NAS 的容量
    totalDiskMB(准入预算) = 8,192     (8 GiB)      ← 我们真正在用的那个数
    used/total       = 0.0053%  ⇒ 百分比阈值（diskWarn/diskError）**永远不会触发**
```

即：**我们的 PV 声明 50 GiB 只是一个标称，NAS 没给它任何边界**；`df` 看到的是整台 10 PB 的
文件系统（还是所有租户共用的）。所以"卷级水位闸门"在这台存储上**不能建立在 statfs 比例上**，
必须建立在我们自己的台账上（已售出 / 实测之和）；statfs 只能当"整个 NAS 被别人塞满"的旁证，
而那跟我们基本无关。

这一条与第 3 节合流成一个结论：**想给我们的 slice 一个真实边界，只能显式设一个**——
要么目录配额的那 50 GiB（Enforcement，超了写入就失败），要么我们自己的台账 + 停准入。
`df` 永远不会告诉你边界在哪。

* **R2 硬限**（若走第 3 节）：沙箱内 `dd` 超过 `diskMB` → 写入失败；**同卷邻居不受影响**；
* **R1**：把若干沙箱写到各自上限 → 卷水位闸门触发、建箱 503、**既有沙箱仍可写**；
* **删除路径成对收尾**：配额与记账都必须随删除释放（N12/N24 的教训：计量与执行要在同一条路径上收口）；
* **迁移**：额度跟着树走（目录配额与路径绑定 ⇒ 目标节点重新 `Set`；只有真删除才 `Cancel`）；
* 判据脚本：扩展现有冒烟 —— 建一个 `diskMB` 很小的沙箱 + `dd`，并加一条"邻居不受影响"的断言。

### 5.2 每沙箱整树 walk 到底贵不贵（2026-09-18 实测，改掉了 L2b 的设计）

原计划（L2b）打算走 **inotify 增量**（事件 + 只 `stat` 变化的文件），前提是"整树 walk 太贵"。
**实测把这个前提推翻了**，所以 L2b 现在**没有 inotify**，就是周期性整树 walk。

在**线上共享 NAS** 上量（`kubectl exec` 进控制面 pod，同一个 `/var/lib/e2b-sandboxes` 挂载，
`os.walk` + `stat` 求和，与 `priv_helpers.dir_size` 同一口径）：

| 形状 | walk p50 | 每次 stat |
|---|---|---|
| 2 000 文件 / 1 目录 | **10.9 ms** | 5.5 µs |
| 10 000 文件 / 1 目录 | **35.7 ms** | 3.6 µs |
| 10 000 文件 / 100 目录 | **273 ms** | 27.3 µs |

读出来的是两条：

1. **每文件成本是亚线性的**（NFSv4 `readdirplus` 一次 RPC 带回整个目录的属性）——
   10 000 个文件只要 36 ms；单文件"贵"在**目录**上（≈2.5 ms/目录），不是文件上；
2. **写文件才是慢的那一头**：同一个卷上创建 2 000 个小文件花了 33 s（16 ms/文件）、
   10 000 个花 167 s。也就是说"为了省 walk 而引入增量记账"，省下来的远小于它引入的
   复杂度（递归 watch 管理、NFS 上的事件语义、`mmap` 写不产生事件、跨节点写看不见）。

因此 L2b 定为：**worker 周期性整树 walk + 超限暂停**，并加一个**扫描预算**（`_DISK_SCAN_BUDGET_S`
= 1 s/轮，超了就下一轮从下一个沙箱接着扫，游标轮转，保证每个树都会被扫到）。

顺带核出两件与它相关的事实（都是**设计使然**，不是 bug，但要知道）：

* `_provision_remote` 结尾**显式** `record.workspace_dir = None` —— 远端沙箱的树归 worker 管，
  控制面不插手（否则删除路径会有两个主人）。所以**"谁测量"只能是 worker**；
* 由此，`GET /sandboxes/{id}/metrics` 在 k8s 上 **`diskUsed` 恒为 0**
  （`SandboxRecord.sample_metric()` 只在 `workspace_dir` 非空时才 walk，而它永远是 `None`）。
  SDK 的 `get_metrics()` 因此在 k8s 上看不到磁盘占用 —— 这是 N25 的另一半，见 §7 的 L2b 收口。

### 5.3 inotify / COW / mmap 能不能当账源（2026-09-18 实测）

这一节是"要不要用事件/COW 替代 walk"的判据，全部在线上 NAS + 6.12 内核上实测。

**inotify（watcher 在控制面 pod，watcher/写者节点关系已核对）**

| 项 | 结果 |
|---|---|
| 同机写（写者与 watcher 同节点） | ✅ `CREATE` + `MODIFY` + `CLOSE_WRITE` 都到 |
| **跨节点写**（worker-0 在 `.94` 写、watcher 在 `.140`） | ❌ **0 个事件**（文件确实存在：12 B，`ls` 可见） |
| 一次 256 MiB `dd` | **~155 个 `MODIFY`**（不是 1 个；合并程度取决于读者排空速度）+ 1 `CLOSE_WRITE` |
| 524 目录（venv 形状）挂 watch | **1215 ms（2315 µs/目录）**，且每新建目录都要补 |
| 上限 | `max_user_watches=58688`（**per-uid**）、`max_user_instances=128`、`max_queued_events=16384` ⇒ 58688/525 ≈ **111 个该形状沙箱/节点**，且不能"每沙箱一个 fd" |

⇒ inotify 只能当**本机提示**，不能当账本：跨节点写静默少记，队列会丢事件
（必须把"溢出"翻译成"全量重扫"）。它的唯一优势是**不动 Rust**。

**COW（代码 + 既有 probe 报告 `docs/sandbox-disk-quota.md` §1.1.1）**：不是"记账"，
而是**把 walk 搬到最热路径**——`cow/seccomp.rs:1085`/`:1161` 在**每次写 open** 都
`recalc_disk_used()` → `dir_size(&self.upper)`（`:82`，整树递归），注释写明是为了把
"已注入 fd 写进去的字节"算回来。对照实测（同一份报告）：单次 open 写 256 MiB 通过、
`ftruncate/pwrite/mmap/fallocate/O_DIRECT/稀疏/子进程/静态二进制` 全不拦，只有**下一个 open**
才 ENOSPC；且它在 E2B 形态下**根本没激活**（我们发 `max_disk` 但从不发 `workdir`，
门槛是 `!no_supervisor && workdir.is_some()`，`sandbox.rs:2078`）。

**mmap 扩容**：文件内写 ✅ 正常；**越 EOF 写直接 `SIGBUS`（exit 135）**——这台 NFS 上
共享映射**长不了文件**（与"稀疏文件不保留"同类）。所以"事件驱动会漏 mmap 增长"这条常见
反对理由在这里**不成立**；但它是环境事实，换存储要重新验证。

**取舍**：inotify 与 mediator 脏集合**覆盖面完全相同（都只到本机）**，后者免费（`openat`
本来就在拦）且更准，唯一代价是要动 Rust。因此：**愿意动 Rust 走脏集合，不愿意才用 inotify
（且只能当提示）**；完整设计见 [`docs/disk-accounting-dirty-dirs.md`](disk-accounting-dirty-dirs.md)。

## 7. 结论：方案

### L1 —— 已落地（2026-09-18，`0.1.0-360-…`）

**磁盘准入从「每节点预算」变成「卷级台账」，并且拒绝时说得清楚。**

先说两个更正，都是这轮核出来的：

1. **台账机制一直存在**，而且**一直在生效**：`manager.py::_quota_allows_locked` 的 global
   `"disk"` 维度 + `E2B_MAX_TOTAL_DISK_MB`，其**代码默认值是 10240**。所以线上从来不是
   "没有卷级准入"（我先前按「清单里没设 + env 里看不到」误判了一次 —— 代码默认值不在 env 里），
   只是那个数藏在默认值里、没人知道它存在、也看不到它用了多少；
2. compose 时代另设过同一个值（`deploy/stack/.env` = 10240），与默认值相同 ——
   即**两个栈一直是同一口径：只卖 PVC 声明（50 GiB）的 1/5**。

所以 L1 不是"新建机制"，而是"**把它显式化 + 可观测 + 拒绝可解释**"：

* `deploy/k8s-k0s/control-plane-nfs.patch.yaml` 把 `E2B_MAX_TOTAL_DISK_MB` **显式写成
  10240**（= 现状，不改行为），并写清口径与"要放大就改这里"；基线 `control-plane.yaml`
  留注释说明托管集群也该显式写出来（那里没有 overlay 兜底，值藏在代码默认值里）；
* 拒绝时给**专门的错误**：`shared workspace disk budget exhausted: <已售> MiB reserved of
  <上限> MiB` —— 以前所有拒绝都说 "No resources available"，会把"工作区塞满了"误导成
  "内存不够"；非磁盘维度的拒绝**保持原文案**（E9.3/E9.4 的重试路径按那句话写的）；
* `GET /internal/fleet/metrics` 新增 `workspaceDisk {reservedMB, limitMB, warn, saturated}`
  —— 这是**唯一**反映我们这片 slice 的磁盘信号（节点视图里的 `usedDiskMB/diskTotalMB`
  是整台 NAS，见 §5.1）；
* `SandboxRegistry.global_reserved()` 暴露舰队台账（内存与 Redis 两种后端都能读）。

**语义边界（写清楚，避免误读）**：这个数约束的是**卖出去多少**（Σ 活沙箱 `disk_mb`），
不是盘上实际字节 —— paused 沙箱释放预留（E9.2），它们的数据仍在卷上；实测用量的来源
是 L2b 的事。

⚠ 实现时踩到一个真坑，留个记录：`_quota_denied_message` 最初调 `global_reserved()` 取数字，
而它是在**已持有 `self._lock`**（普通 `threading.Lock`，不可重入）的路径里被调用的 ⇒ 死锁，
整个测试套挂住。现在锁内调用点把已知值直接传进去，锁外（Redis 路径）才去读。

**收口（同日，上线后复验时发现文案没透出）**：闸门确实拦住了，客户端拿到的却是旧的
`503: No resources available`。`_CapacityExhausted(str(exc))` 这一路是对的，问题都在上游，
三条：

1. **`SandboxRegistry.create()` 的 Redis 分支是硬编码文案** —— 多副本（= 线上）走的是
   `self._quota_store` 那一支，它没被上一次改动碰到 ⇒ "内存分支说得清、线上说不清"。
   现在和 `hold_quota` 一样，**只在拒绝那一次**多读一次台账来分类维度
   （`_store_would_refuse`），非磁盘维度仍回 "No resources available"；
2. **节点闸门（`select_and_reserve` 返回 `None`）也只说明"没有资源"**。k0s 上 fleet 限额
   （10240）先于节点限额（8192×2）触发，所以线上靠第 1 条就够了；但**单节点形态下节点先触发**
   （compose 时代就是：节点 `E2B_NODE_DISK_MB` 4096 < fleet 10240）；而且测试 harness 里
   in-process 节点的 `total_disk_mb` **就等于** fleet 限额 ⇒ 只修第 1 条，本地和单节点上
   这个修复**看不见**。现在 `NodeRecord.blocking_dimension()` 返回**先失败的那个维度**
   （顺序与 `can_fit` 一致，`can_fit` 改成它的薄封装），`NodeRegistry.refusal()` 只在
   **所有可放置节点都因同一个维度失败**时给出该维度并附上这些节点的磁盘聚合
   （混合原因、或集群里一个可放置节点都没有 ⇒ `None` ⇒ 保持中性文案，不挑一个"赢家"）；
3. 两条闸门的措辞由 `workspace_disk_refusal()` **同一处**产出，避免两句话各自漂移；
   resume 钉在特定节点上，所以按**该节点**的 slice 报数，而不是"舰队其他节点还能装什么"。

回归保护：`test_the_shared_store_budget_refuses_and_says_so`（Redis 后端单元）、
`test_redis_multireplica_disk_budget_names_itself`（整流链路的 API 契约，形如线上拓扑）。
两处都验证过"去掉修复即失败"。

### L2 —— 每沙箱那一层（L1 之后的下一步）

> ⛔ **L2a（目录配额）已被否决（2026-09-18，运维决定：不想引入云 API 依赖）。**
> 记录在此是因为它的结论仍然有效 —— 它本来是**唯一"硬 + 零运行时成本"**的选项
> （`Enforcement` 服务端执行、还能免费拿到每沙箱用量）。被否决的不是能力，而是代价：
> CP 要多一条对 NAS OpenAPI 的运行时依赖（凭据、限流、故障面），以及 500 目录/文件系统、
> `SizeLimit` 是 GiB 整数这两个约束。**下面的 L2b 因此从"备选"变成"下一步"。**

**L2a（~~通用型 NAS → 目录配额~~，已否决）：**

* 建箱：`SetDirQuota(Path=<base>/<sandbox_id>, QuotaType=Enforcement,
  SizeLimit=ceil(disk_mb/1024) GiB [, FileCountLimit=<按需>])`；删箱：`CancelDirQuota`
  —— **成对收尾**（N12/N24 的教训）；
* 每沙箱用量走它的**统计能力**（不需要我们扫树），喂给 §7 L1 的台账；
* 顺手给我们的 slice 根（`/sandlock`）设一个 50 GiB 的 Enforcement 配额 ——
  **这才是"50 GiB"变成真边界的方式**（§5.1：`df` 永远显示 10 PiB）；
* 已知约束：**500 目录/文件系统**（⇒ 并发沙箱上限，写进容量规划）、`SizeLimit` 是 **GiB 整数**
  （`diskMB` 向上取整，粒度写进 API 文档）、需要 **RAM 凭据**（`nas:SetDirQuota`，
  这台 ECS 目前没有实例角色）。

**L2b（选定路线）→ 测量 + 停箱：**

* worker 增量测量（inotify 事件 + 只 `stat` 变化过的文件；长写者用 open-write fd 扫描兜住），
  超过 `diskMB` → **暂停沙箱**（复用现有 pause：保留状态、释放准入），而不是返回 ENOSPC；
* 语义必须写进 API 文档与错误信息：「配额 = 准入与守护；超限会被暂停」，并给出恢复路径。

**L2b 已落地（2026-09-18，`0.1.0-362-…`）：** 实现与上面的草稿有一处重要差别 ——
**没有 inotify**，因为 §5.2 的实测推翻了它赖以成立的前提（整树 walk 只要 10.9 ms/2 000 文件）。

* **worker 测**：`RuntimeRegistry.disk_usage_snapshot(budget_s=…)` 周期性 walk 每棵沙箱树
  （与 `/metrics` 的 `priv_helpers.dir_size` 同一口径，所以"你看到的数" == "判你超限的数"），
  预算 1 s/轮、游标轮转（保证每棵树都会被扫到，不会永远饿死队尾）；间隔
  `E2B_DISK_ENFORCE_INTERVAL_S`（默认 30 s，**0 = 关闭**），结果缓存后跟随后续心跳重发；
* **`None`（DAC 够不到、broker 也不覆盖）→ 不报**：`unknown ≠ 0`，不能把"测不到"读成"没占地方"；
* **CP 判**：`SandboxRegistry.enforce_disk_budget()` —— 只动
  **running 且实测 > `disk_size_mb`** 的记录，走**现有 E9.2 pause**（保留状态、释放 global/tenant
  预留，节点 slice 由心跳处理器 `_park_capacity` 释放）。**已暂停的跳过**，否则每个心跳都会往
  同一条记录追加一行 "sandbox paused"；
* **冻结推送失败不回滚**：滚回等于把预留还给一个还在写的沙箱；推送失败只 WARNING，
  下一个心跳继续推（幂等）。这条与用户主动 pause 的语义**故意不同**（那条要回滚，见 G1a）；
* resume 仍可发起：树回到预算内就不会再被暂停；**仍在超预算则会在下一个心跳（≤30 s）内被再次暂停**
  —— "暂停而不是 kill"的代价就是这个来回，换来的是现场不丢；
* 配套老实说清楚：`GET /sandboxes/{id}/metrics` 的 `diskUsed` 在 k8s 上**仍然是 0**
  （§5.2 第 2 条，远端记录 `workspace_dir=None` 是设计使然）。**这个数还没接到 API 上**，
  所以现阶段"看得见用量"的地方只有 worker/CP 的 WARNING 日志与
  `/internal/fleet/metrics` 的卷级台账 —— **下一步就是把每条记录的最后一次实测值落库并暴露出去**
  （它同时也是"resume 时直接告诉用户超了多少"的输入）。

**L2c（已记录，待实施）：mediator 脏目录记账 —— 把"每轮整树 walk"降级为"只重扫脏目录"。**
设计、盲区、接口、测试计划见 [`docs/disk-accounting-dirty-dirs.md`](disk-accounting-dirty-dirs.md)
（依据是 §5.2 的 walk 成本 + §5.3 的 inotify/COW/mmap 实测）。要点：脏信号取自**已经被拦的**
路径 syscall（`openat`/`truncate`/`rename`/`unlink`/`mkdir`…），零新增陷阱；粒度取**父目录**
（正好等于 NFS 的成本单位，2.4 ms/目录）；预期 1.27 s/轮 → 稳态 **2.4 ms/轮**且不随树增长；
**低频整树对账必须保留**（跨节点写看不见）；开关默认关，能力探测失败即回落今天的 walk。
两条前置事实：inotify 与它覆盖面相同但更贵（§5.3）；COW 的账本本身就是"每次写 open 整树
recalc"，比今天更贵（§5.3）。**另记两条更便宜的 fork 侧原语**（探索，同一文档 §10）：
**A. `RLIMIT_FSIZE = diskMB`** —— fork 从来没设过这个 rlimit，而 `max_disk` 在本形态下是死参数
⇒ 几行代码就能拿到"**单个文件不可能超过整树预算**"的**内核硬边界**（实测那种 `dd bs=1M
count=1200` 当场 EFBIG），且对树口径永不误伤，代价是 EFBIG/SIGXFSZ 语义 + 它同时管到卷/`tmp`；
**C. worker 侧纯 `/proc` 采样"打开的写 fd"**（`fdinfo.flags` + `stat`）——**零 fork 改动**，
实测 **86 µs/次**，天然覆盖 `ftruncate/fallocate/copy_file_range` 的结果，直接命中"一个 fd
一直在长"的跑飞形态。

### L3 —— 兜底（仅在 L2 两条都不接受、又必须 ENOSPC 时）

**loop 镜像**（§4.1 已实测）：语义最正、完全不依赖存储，代价是密度（50 GiB / 1 GiB =
50 个沙箱）、吞吐 +35%、`mount`+`/dev/loop` 的权限面，以及"扫树/迁移/GC/模板"整套改造。

### 不做

写路径记账（A：给最高频 syscall 加中介往返）、cgroup（没有空间配额）、稀疏占位
（NFS 不保留）、工作区改节点本地 XFS（与共享存储前提冲突）。

### 还需要你给的两个输入

1. **这台 NAS 的规格**：通用型（→ L2a 今天就能落地）还是极速型（→ 只能 L2b）？
   确认方式：NAS 控制台的「文件系统类型」，或直接试 `SetDirQuota`（通用型专属）。
2. **超限语义**：`ENOSPC`（L2a，与 E2B 契约一致）vs **暂停**（L2b，存储无关但语义偏离）。

> L1 与这两个输入无关，可以并行开工 —— 建议先落 L1，它让后面无论选哪条都有账可依。
