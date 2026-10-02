# 建箱本地优先：设计（Task 1 的产品，Task 2/3/4 的权威）

> **2026-10-02 在集群实测**。集群：自建 k0s，2 节点全 arm64（`172.18.80.94` /
> `172.18.80.140`），namespace `sandlock`，版本 `0.1.0-887-g7ef319b-20261002-100406`
> （Task 0 根重切已上线：`E2B_TREES_SHARED=1`，树与快照载荷仍在共享卷上）。
>
> **这份文档是 Task 2/3/4 的权威**：Task 2（快照打成 tar）用它 §4 的复核结论，
> Task 3（树搬节点本地）用它 §2/§3 的介质账与**淘汰上限**，Task 4（本节点 state 分家）
> 用它 §5 的裁定。凡写进这里的数字都有 §1 的探针与 §7 的命令；**没有一条是转抄旧报告的**。
>
> 原始读数（逐条 `SUMMARY` 行、JSON）：`tmp/task1/`（gitignored，会随 `tmp/` 清掉）。
> 要复现请跑本仓 `deploy/scripts/acceptance/` 里的脚本，不要依赖 `tmp/`。

---

## 0. 结论先说

1. **介质判据必须按"操作类型"分，不能按"存储"整体分**（§1）：节点本地盘在**元数据**上快
   **480×**（同样 200 个 64 B 文件：2627.2 ms vs 5.5 ms），在**大块字节**上反而慢
   **4 倍**（125 MB/s vs 505 MB/s）。**§3.1 那条"本地 1027 MB/s"是突发相位，不是稳态**（§1.2）。
2. **容量不是问题，页缓存才是**（§2/§3）：每节点树 ≤ 8 GiB（`E2B_NODE_DISK_MB=8192` ÷
   `E2B_DEFAULT_DISK_MB=1024`）+ 镜像缓存 4 GiB，占 68–75 GiB 空闲的 16%；
   但**一次 900 MiB 的恢复拷贝就把 `maint` 的页缓存顶到 441 MiB / 512 MiB 限额的 86%，
   10/10 次 `memory.current` 都触到 512.0 MiB 天花板**（§3.1 给了上限配置）。
3. **`_snapshots` 合一复核通过**（§4）：8 个 id 四类（记录+载荷 4 / 只有记录 2 / 只有载荷 2 /
   空 0）、无遗漏、迁移的"合并不是覆盖"具名拒绝**一次都没触发**（这份数据上没有同名条目）。
   Task 2 的路径推导不再建在流沙上。

---

## 1. 测量一：块设备（Step 1 ⓪ + ①）

**探针**：`deploy/scripts/acceptance/local_first_storage_probe.py`（⓪，三种块大小 × 两个根）
与 `deploy/scripts/acceptance/local_first_sequential_write_probe.py`（①，沙箱内）。
命令见 §7.1/§7.2；两笔都在 `e2b-worker-0`/其上的沙箱里跑，日期 2026-10-02。

跑之前先说清两种"形状"，因为**今天没有"树在本地"这个形状**（Task 3 才翻）：

| 标签 | 今天的真实路径 | 说明 |
|---|---|---|
| **NAS 树** | 沙箱 `/workspace` → `<workspace base>/<id>` → NFS4 `/var/lib/e2b-sandboxes` | 就是沙箱看到的那条 |
| **节点本地盘** | worker 容器 `/var/lib/e2b-images`（`hostPath`，`/dev/nvme0n1p2` xfs） | **模拟**：Task 3 的目标是同一块盘上的 `/var/lib/e2b/workspaces`，不是真"树在本地" |

### 1.1 小文件（200 × 64 B，open+write+close，n=200）

| 形状 | 总耗时 | 每个文件 | 删除（200 个） |
|---|---|---|---|
| NAS 树（`/var/lib/e2b-sandboxes/workspaces`） | 2627.2 ms | **13.136 ms** | 1658.4 ms |
| 节点本地盘（`/var/lib/e2b-images`） | 5.5 ms | **0.0274 ms** | 1.6 ms |
| 倍率 | | **480×** | 1037× |

这和 `docs/create-local-first-layout.md` §3.1 的 466× 是同一件事（每次 `open(create)` 一个
NFSv4 同步 RPC vs 本地微秒）。**这条是本地化的真正收益**，量级不变。

### 1.2 顺序写：三种块大小，n=10（**这一条推翻了 §3.1 的本地读数**）

每个尺寸跑 10 次，8 MiB 分块、每 64 MiB 一次 `fsync`，每次写完 unlink：

| 块大小 | NAS 树 p50 | NAS min–max | 节点本地盘 p50 | 本地 min–max |
|---|---|---|---|---|
| 64 MiB | **531.9 MB/s**（0.164 s） | 391.2–531.9 | **1017.1 MB/s**（0.520 s） | 123.2–**1017.1** |
| 256 MiB | **525.3 MB/s**（0.584 s） | 438.6–525.3 | **125.0 MB/s**（2.052 s） | 124.7–125.0 |
| 1024 MiB | **505.3 MB/s**（2.222 s） | 460.9–505.3 | **124.9 MB/s**（8.202 s） | 124.9–124.9 |

**本地那一列的"双峰"就是全部答案**：64 MiB ×10 的逐次读数是
`1017.1, 912.9, 189.0, 125.5, 125.4, 123.2, 125.6, 125.6, 126.1, 123.4`，
同一序列的逐 fsync 窗口是 `1018.3, 913.8, 189.1, 125.5, …`——**前 ~128–192 MiB 是突发
（云盘突发额度／延迟分配），之后恒定在 125 MB/s**；256/1024 MiB 的每一窗口都是 123–126 MB/s。

所以：

* `docs/create-local-first-layout.md` §3.1 的"本地 **1027** MB/s"是**突发相位的读数**（或未
  fsync 的页缓存读数），**不是稳态**；把 1 GiB 写满就会看到它掉到 125。
* 计划里那条被作废的"本地 **186** / NAS **381**"在**量级上是对的**（本地 125–186、
  NAS 381–505）。这次重测把两边都钉在 2026-10-02 的同一台设备上：
  **大块字节：NAS 505 MB/s > 本地 125 MB/s（本地慢 4.0×）**。
* 结论一句话：**本地化买到的是元数据延迟，代价是大块吞吐**。判据必须按负载形状分，
  §3.1 的"把东西搬到本地盘不是单调变快"这条本身没被推翻 —— 被推翻的是它那一行的数字。

### 1.3 沙箱里的 1 GB 写（Step 1 ①）

沙箱自身受 `E2B_DEFAULT_DISK_MB=1024`（xfs project 配额）约束，所以三种量法都给：

| 量法 | 结果 |
|---|---|
| 沙箱 `/workspace` 写 **1024 MiB**（`dd conv=fsync`） | **写不进去**：`1023+0 records out`（1073740282 B）后 `dd: error writing '/workspace/big.bin': File too large`，rc=1 |
| 沙箱 `/workspace` 写 **1000 MiB**（单次） | **502.9 MB/s，1.989 s** |
| 沙箱 `/workspace` 写 **900 MiB ×10**（n=10） | **p50 512.4 MB/s（1.989 s）**，min 452.5，max 512.4，mean 485.0 |
| worker 节点本地盘写 **1024 MiB ×10**（n=10） | **p50 124.9 MB/s（8.202 s）**，min/max 124.9 |

两条附带事实（都影响 Task 3 的验收写法）：

1. **默认配额下"1 GB 顺序写"根本完不成** —— 1024 MiB 配额连一个 1 GiB 文件都放不下
   （1023.9 MiB 处 EFBIG）。要用 1 GB 量级测，得先调 `diskMB`（今天 SDK 不暴露该参数）。
2. **配额释放有延迟**：`rm` + `sync` 之后 `df` 仍报 501M 占用，约 2 s 后才归零（实测：连续
   第二次 900 MiB 写被 EFBIG 拒绝）。所以沙箱内的重复测量必须等配额回收，探针的
   `--settle-s` 就是为此加的（实测最长等 3.092 s）。

### 1.4 前一版的元数据/拷贝读数（**照录，本轮未重测**）

来源：`tmp/local-disk-eval-measurements.md`（2026-10-01/02 同一集群、同一容器，gitignored；
本轮 §7.1 的探针就是它的升格版）。**这一小节是照录**，Task 3 若拿它当验收基准，
应先按 §7.1 复测；本轮只重测了它的**大块吞吐那一行**（§1.2，结论被推翻）。

单位毫秒，n=30（NAS = `/var/lib/e2b-sandboxes/workspaces`，本地 = `/var/lib/e2b-images`，
overlay = 容器根）：

| 操作 | NAS p50 | 本地 xfs p50 | overlay p50 |
|---|---|---|---|
| `mkdir` | 5.210 | 0.018 | 0.018 |
| `chmod 0770` | 2.850 | 0.002 | 0.002 |
| `lchown` | 2.826 | 0.002 | 0.002 |
| `lstat`（每次新目录） | 4.364 | 0.002 | — |
| `open_chain`（5 段 `O_NOFOLLOW`） | 8.703 | 0.014 | 0.009 |
| `creat+write 16B+close` | 11.494 | 0.479 | 0.478 |
| `fsync` only | 4.380 | 0.003 | 0.003 |
| `unlink+creat` | 19.285 | 0.016 | 0.023 |
| `rename` | 10.570 | 0.015 | 0.019 |
| 空树 create 复合（= materialize 的存储动作） | 54.514 | **0.076** | 0.075 |
| 真实 `e2b-maint chown --recursive`（空树，n=10） | 25.807 | 0.621 | — |

真实 `materialize_tree`（生产同一条代码路径 + 真实 `e2b-maint`）：

| 用例 | n | NAS p50 | 本地 p50 |
|---|---|---|---|
| 空树 materialize | 15 | **70.828 ms** | **0.998 ms** |
| 空树拆除 `rmtree` | 15 | 25.494 ms | 0.049 ms |
| materialize 拷贝 40 文件 | 10–12 | **1144.556 ms（26.413 ms/条目）** | 2.294 ms（0.053 ms/条目） |

拷贝的四个方向（40 文件，p50；`本地→本地` 只贵 2.3 ms 说明**贵的是目标侧的 NAS 往返**）：
`NAS→NAS 1144.556` / `本地→NAS 954.433` / `NAS→本地 192.932` / `本地→本地 2.294`。

跨节点（worker-0@.94 → worker-1@.140，Calico 覆盖网，接收端真的落盘）：RTT p50 **0.0592 ms**、
2000 文件 **102.833 ms**（0.0514 ms/文件）、大块 **590 MB/s**。⇒ 网络不是瓶颈，
瓶颈是 NAS 的元数据 RPC；而且 **agent pod 之间被 `e2b-c3-agent` NetworkPolicy 挡住**
（只放行 CP→agent 的 49985/49986），跨节点恢复要么改策略、要么绕控制面。

---

## 2. 测量二：容量账（Step 1 ②）

**探针**：`deploy/scripts/acceptance/local_first_capacity_account.py`（一次普查，不是抽样：
n = 10 个命名空间 + 8 个快照 id + 2 个节点）。命令见 §7.3。

### 2.1 存量（2026-10-02，站在节点 `.140` 的 agent `maint` 里）

| 命名空间（`<export>/`） | 字节 | 文件数 | 说明 |
|---|---|---|---|
| `_images` | 3,923,022,946（3741.3 MiB） | 23,623 | 71 个 `_oci/*.oci.tar` + 解包缓存（`du` 报 3780 MiB）；`oci.tar` 是唯一的真·节点间交付面，**必须共享** |
| `_snapshots` | 2,051,201（2.0 MiB） | 4,055 | 8 个 id（§4 的表） |
| `state` | 1,695,722（1.6 MiB） | 2,894 | 运行时记录/checkpoint（裁定 3：留共享） |
| `_templates` | 424,361 | 87 | 控制面副本间共享 |
| `_builds` | 8,502 | 77 | 同上 |
| `_volumes` | 2,320 | 290 | 全是 `*.deleted` 记录 |
| `_migrate` / `_secrets` / `.uid_reservations` | 0 | 0 | 空 |
| `workspaces`（树） | **0** | 0 | 盘点时舰队是空的 |

**别把整卷用量当平台用量**：`statvfs` 报共享卷 10 PiB、已用 **553.3 G** —— 那是阿里云 NAS
`/sandlock` 这一整卷（还有别的占用者），平台自己的根只有上面这 ~3.7 GiB。
心跳里的 `usedDiskMB = 566,766 MB` 就是这一整卷的数，8 GiB 的调度预算是另一个口径 ——
这正是 `docs/create-local-first-layout.md` §3.2 ⑧ 说的错配。

### 2.2 节点本地盘（100 G xfs，与镜像缓存在同一块盘）

| 节点 | 已用 | 可用 | 镜像缓存实测 | 镜像缓存上限 |
|---|---|---|---|---|
| `.94`（`e2b-worker-0`） | 26 G | **75 G** | 3.8 G | `E2B_IMAGE_CACHE_MAX_BYTES=4294967296`（4 GiB） |
| `.140`（`e2b-worker-1`） | 33 G | **68 G** | 3.8 G | 同上 |

### 2.3 日增量与保留窗口

**先说没有的东西**：这个部署**没有任何快照 TTL / GC**（`control_plane/registry/snapshots.py`
里只有跨副本的拷贝租约，没有按时间/容量的淘汰；`DELETE /snapshots/{id}` 是唯一出口）。
所以"日增量 × 保留窗口"里的**保留窗口今天是不存在的**，能作为有界量的只有：

| 输入 | 值 | 来源 |
|---|---|---|
| 一棵树的上限 | 1024 MiB | 每沙箱默认 `E2B_DEFAULT_DISK_MB=1024`（代码默认，env 未覆盖） |
| 每节点树的调度上限 | 8192 MiB（= **8 个沙箱**） | `E2B_NODE_DISK_MB=8192` |
| 一个快照的大小 | **≈ 1× 树**（实测：900 MiB 的树 → 900 MiB 载荷） | §3 的 900 MiB 快照 |
| 存量（今天的舰队） | `_snapshots` 2.0 MiB / 8 个 id | §2.1 |

**每节点需要多少 G（Task 3 之后的稳态）**：

| 项 | 上界 | 备注 |
|---|---|---|
| 沙箱树 | 8 GiB | `E2B_NODE_DISK_MB=8192` 的调度额度（8 × 1 GiB） |
| 镜像解包缓存 | 4 GiB | 实测 3.8 G，上限 `E2B_IMAGE_CACHE_MAX_BYTES` |
| 本节点 state（Task 4） | ≪ 1 G | `.creating`/disk-stats/route-B/uid 池本地件，都是小文件 |
| **合计** | **≈ 12 GiB** | 占 68–75 GiB 空闲的 **16%** |

**如果把快照仓也放本地（本计划不这么做，但这是 75 G 那道题的正解）**：

| 场景 | 需要的盘 | 结论 |
|---|---|---|
| 8 沙箱 × 1 GiB × 每天 1 个快照 × 留 7 天 | 56 GiB | 勉强塞得下，与树/镜像缓存相加 68 GiB ⇒ **已经越界** |
| 8 沙箱 × 1 GiB × 每天 8 个快照 × 留 7 天 | 448 GiB | **不可能** |

⇒ **快照仓必须留共享**（这正是 Task 2 "快照打成 tar 落共享"的容量理由，不只是恢复路径
的理由）。节点本地只承担树，树的量由 `E2B_NODE_DISK_MB` 本身封顶。

**Task 3 要用的"淘汰上限"**：节点侧的硬上限今天已经存在，就是 `E2B_NODE_DISK_MB=8192`
（调度额度，本地化后第一次与真实盘同源），镜像缓存 4 GiB 是第二个可加项；两者相加
≈ 12 GiB，对 68–75 GiB 空闲留出 4–5 倍余量。**不要**另造一个更小的树上限去"提前淘汰" ——
本轮的测量（§1.2/§3）说的是**大块吞吐会掉 4 倍、恢复页缓存会顶到 512 MiB**，
没有一条说 8 GiB 的树装不下。上限该压的是**单次拷贝的在途量**（§3.1），不是树的存量。

### 2.4 归属清单的核心裁定（**照录** Task 0 的盘点）

来源：`tmp/sandbox-tree-inventory.md`（2026-10-02 只读盘点，gitignored；25 行的清单表和
逐条 `file:line` 证据在那里）。判定标准只有一条：**这份数据被"另一个节点上的读者"需要吗**。
本节只搬结论面，Task 3/4 实现时按它分介质。

| 必须共享 | 一句话证据 |
|---|---|
| `<ws>/<id>`（沙箱树 / `/workspace`） | 共享形态下迁移目标节点**直接读它**，不传字节（`control_plane/api/sandboxes.py` 的 `if not shared` 两处） |
| `<export>/_snapshots/<id>/`（记录 + 载荷） | 建箱恢复方可能不是拍快照的节点；记录还要两个 CP 副本共读（§4 复核过） |
| `<state>/_runtime/<id>/sandbox.json` | 每个 worker 枚举**所有节点**的记录做 uid 记账（`envd_service/uid_pool.py`；裁定 3：留共享） |
| `<state>/_runtime/.checkpoints/<id>/latest` | 迁移会改 `record.node_id`，resume 会在新节点读旧节点写的镜像（**代码推导，未实测**） |
| `_volumes/_meta/**`、`_templates/**`、`_builds/**` | CP 两个副本共写共读 |
| `_images/_oci/*.oci.tar`（3.7 G） | "CP 构建一次、每个节点解一次"的交付面 |

| 可以本地 | 省什么 | 前提 |
|---|---|---|
| `<state>/_runtime/<id>/.creating`、`disk-stats` | 建箱 `prepare` 的 NFS 原子写 | 只有本节点读；**但它与 `sandbox.json` 同目录**，要拆先拆路径（Task 4） |
| `<state>/.route-b/**` | slot 起停的小写 | 读者是本节点 slot 进程（沙箱 uid），权限要跟着走 |
| `<state>/.uid_pool.lock`、`.uid_reservations/` | 池操作的 NFS 往返 | 全舰队口径来自 `sandbox.json`，不是这里 |
| `<state>/_runtime/<id>/command-logs.jsonl` | 日志写的往返 | 裁定 1：远程形态 CP 是**代理**读，直读只属 `local://` |
| 卷切片 `<volume>/<id>` | 挂载/配额几步 | 卷已按 `volume_node_id` 把沙箱钉在节点上；本轮**不动** |
| `_untrusted.trees/`、`_pure_rootfs/`、`_migrate` | 小 | 同节点；`_migrate` 已上浮（Task 0） |

**已经是本地**：`/var/lib/e2b-images`（rootfs 缓存、`.digests`、`_oci/*.link`、`secrets/`，
`hostPath`，实测 3.8 G）。

**刻意不动**：`_images`、`_templates`、`_builds`、`_volumes`、`_secrets`、`state/`（已在共享根），
`_cow` 保留名不删；checkpoint 本轮仍留共享（它的"本地化"取决于是否禁止迁移 paused 沙箱，
要单独立项）；**不做**"树本地 + 跨节点冗余"（节点掉线丢树由裁定 6 承担）。

**盘点里明确"未验证"的三条**（Task 3/4 别当成已知）：`last-restore.json` 的写者/读者未逐行确认；
`state/_sandboxes/` 是否仍被写未确认；"paused 沙箱迁移后跨节点 resume"是代码推导，没在集群跑过。

---

## 3. 测量三：页缓存账（Step 1 ③）

**探针**：`deploy/scripts/acceptance/local_first_pagecache_probe.py`（在被测容器**内部**读
自己的 cgroup）+ `deploy/scripts/acceptance/local_first_pagecache_acceptance.py`（用公开 API
驱动建箱/快照/恢复）。命令见 §7.4。记账字段是 `memory.stat:file`（内核记在**写者** cgroup
上的页缓存）与 `memory.current` 峰值。

**先纠一个数**：计划里写"worker / 控制面各 **2 GiB**"是 N58 之前的基线
（`deploy/k8s-k0s/worker-capacity.patch.yaml` 的注释里写着基线是 `limits: cpu 2 / memory 2Gi`）。
**线上今天的 worker 限额是 4 GiB**（同一份 patch 把 limits 显式改成 `cpu 4 / memory 4Gi`，
`E2B_NODE_MEMORY_MB=4096`）。下表用**实测限额**列。

| 负载（900 MiB 的树） | 容器（限额） | n | 耗时 p50（min–max） | 峰值页缓存 `file` p50（min–max） | 占限额 | 峰值 `memory.current` p50（max） |
|---|---|---|---|---|---|---|
| 建箱（空树 materialize） | agent `maint`（512 MiB） | 10 | 346.5 ms（179.6–799.1） | **32.0 MiB**（32.0–32.0） | 6.3% | 102.9 MiB（107.9） |
| 快照（`shutil.copytree` 树→载荷） | worker（**4096 MiB**） | 3 | 5267.3 ms（2104.9–15122.0） | **2204.6 MiB**（1805.1–2451.7） | **44.1–59.9%** | 2302.8 MiB（2551.7） |
| 从快照建箱（`copy_tree` 载荷→树） | agent `maint`（512 MiB） | 10 | 7834.4 ms（7575.4–8555.6） | **441.1 MiB**（441.1–441.3） | **86.1%** | **511.9 MiB（max 512.0）** |

快照那一行的耗时离散（15.1 s / 2.1 s / 5.3 s）是**冷热差**，不是噪声：第一次要把 900 MiB
从 NAS 读进缓存（15.1 s），后两次源树已在页缓存里（2–5 s），也因此**拷贝越快、页缓存堆得
越多**（1805 → 2205 → 2452 MiB）。恢复那一行
**10/10 次都把 `memory.current` 顶到 511.9–512.0 MiB**，即**零余量**；本次没有 OOM
（全程 `kubectl get pods` 的 `maint` `restartCount` 保持 0），因为页缓存可回收 ——
但这是"贴着天花板跑"，再大一点就是 §3.2 那次 OOM 的重演。

**读法**：

* **拷贝逻辑让页缓存翻倍**：900 MiB 的源 → 快照在 worker 上记了 **1.8–2.5 GiB**（p50
  2204.6 MiB ≒ 源的 2.4 倍：源读缓存 + 目标写缓存各一份）。worker 4 GiB 里这一次就吃掉
  ≥44%，而同一 pod 还要装所有沙箱进程；
  `docs/create-local-first-layout.md` §3.2 记的那次 `maint` OOM 就是这条曲线的另一头。
* **恢复那一行最危险**：900 MiB 的恢复把 `maint` 顶到 **512.0 MiB / 512 MiB**（10 次全部
  在 511.9–512.0，余量 ≈ 0）。这次没 OOM 是因为页缓存可回收（内核边写边回收，代价是抖动）；
  但**"每沙箱 1 GiB"的默认配额与"512 MiB 的恢复容器"是矛盾的** —— 大一点的树就会把
  `maint` 打成 OOM（那不是数据损坏，是恢复失败 + 容器重启）。
* **空树建箱的页缓存是常数级**（32.0 MiB，10 次一模一样，6.3%）：它只是把服务自己的
  二进制与库读进来，不含任何树内容。所以这条账**只由拷贝大小决定**，与建箱频率无关。

### 3.1 上限配置（`E2B_IMAGE_*` 的同款做法）

`E2B_IMAGE_CACHE_MAX_BYTES`（`0` = 不限）那种形状 = **一个具名字节上限 + 越界时的具名处置**
（镜像缓存是"最旧优先逐出"；树/快照不能逐出，只能**具名拒绝**）。建议三件一起做：

| 配置 | 形状 | 值（依据上表） |
|---|---|---|
| `E2B_TREE_COPY_MAX_BYTES` | agent face B 与 worker 共读；`0` = 不限；超过即 `MaterializeRefusal(reason="tree-too-large")` / 快照 413 | 默认 **1 GiB**（= `E2B_DEFAULT_DISK_MB`）；它必须 ≥ 单沙箱配额，否则恢复必然被拒 |
| `E2B_SNAPSHOT_COPY_WINDOW_BYTES` | 拷贝按 N 字节分窗，每窗后 `posix_fadvise(POSIX_FADV_DONTNEED)` 丢掉源/目标缓存 | 默认 **64 MiB**：把峰值从"树大小 ×2"压到"窗口 + 在途脏页"，与 §7.1 里"每 64 MiB fsync"同一个手法（那次修正正是为了不把 `maint` 打 OOM） |
| `maint` 的 `memory` limit | `deploy/k8s/c3-agent.yaml` | **512 MiB → ≥ 2 GiB**（`agent` face A 可以不动：它一个卷都没挂）。若不动限额，就必须把上面的 tree 上限压到 **≤ 256 MiB** —— 但没有哪个默认沙箱配额比 256 MiB 更小，所以现实里是"抬限额" |

---

## 4. 复核：`_snapshots` 合一（Step 2）

**探针**：`deploy/scripts/acceptance/local_first_snapshot_verify.py`（只读），命令见 §7.5。
Task 0 把两个命名空间合成一处之后，这里独立复核"**合并无遗漏、重复 id 没有静默取一个**"。

### 4.1 逐 id 表（2026-10-02，站在控制面 pod 里读共享卷）

| id | `snapshot.json`（记录） | `.complete` | `fs/`（载荷） | 记录里的 `status` | `created_at` | 载荷条目 |
|---|---|---|---|---|---|---|
| `snap_015f907ace11c1ea` | 有 | — | — | **failed** | 2026-09-27T05:17:45Z | — |
| `snap_1ca5ab3332906e32` | 有 | — | — | **failed** | 2026-09-27T05:12:09Z | — |
| `snap_2bb1fe18f81d4d77` | — | 有 | 有 | — | （无记录） | 2 |
| `snap_46dc467759dbbfb7` | 有 | 有 | 有 | completed | 2026-10-01T12:59:02Z | 2 |
| `snap_4bf1225dfbf54e0c` | 有 | 有 | 有 | completed | 2026-10-01T12:59:13Z | 2 |
| `snap_962c14802d6cbd50` | 有 | 有 | 有 | completed | 2026-09-26T01:42:14Z | 2002 |
| `snap_a6e1470cca1c2c09` | — | 有 | 有 | — | （无记录） | 41 |
| `snap_ce90ef9852fc6809` | 有 | 有 | 有 | completed | 2026-09-27T07:13:52Z | 2002 |

四类计数：**记录+载荷 4**、**只有记录 2**、**只有载荷 2**、空 0。

### 4.2 三条判据与结论

| 判据 | 读数 |
|---|---|
| 旧载荷根还在吗 | `<export>/workspaces/_snapshots` **不存在**；`<export>/workspaces` **空**（`_snapshots`/`_migrate` 都不在了） |
| 有没有 id 同时留在两个根（"静默取一个"） | **0 个**（`ids_in_both_roots: []`，`legacy_leftovers: []`） |
| 迁移的"合并不是覆盖"具名拒绝是否触发过 | **没有**：journal 的动词只有 `mkdir 4 / move 18 / rmdir 4`，`refusals: []`、`duplicate_move_targets: []`。逐条合一的三个 id（`46dc`/`4bf1`/`ce90`）是**两个不同名字**（`.complete`+`fs` 与 `snapshot.json`）落进同一个目录，**同名冲突在这份数据上从未出现** |

**结论（Task 2 可以据此推导路径）**：

1. `<export>/_snapshots/<id>/` 现在**同时**是记录与载荷的家；Task 2 把 `fs/` 换成 `fs.tar` +
   `.complete`，路径推导只依赖这一个根。
2. "只有载荷"的两个 id（`2bb1`/`a6e1`）**没有记录** ⇒ 控制面看不见它们
   （`GET /snapshots` 今天正好返回 6 个带记录的 id），它们是"载荷在、记录不在"的历史残留，
   不是合并丢的（合并只搬不删）。
3. "只有记录"的两个 id（`015f`/`1ca5`）**记录自己写着 `status: failed`** —— 没有载荷是
   **数据本来就如此**，不是合并漏搬；Task 2 的 tar 通道不得替它们造一个 tar。
4. 顺带纠正 `control_plane/registry/snapshots.py` 的 docstring：它说记录与载荷"同处同一对象"，
   这在 09-27 到 N58 之间是**错的**（两个根），N58 之后才重新为真。

---

## 5. 裁定（逐条照录）

### 5.1 计划的设计裁定（`docs/superpowers/plans/2026-10-02-local-first-create.md` §Global Constraints）

1. **节点必有 agent** ⇒ `command-logs.jsonl` 的远程形态一律"代理到该节点 worker"，直读只属于
   `local://`；因此它**可以本地**（Task 4）。
2. **跨节点 = 经共享中转**，不要求 p2p ⇒ `_migrate` 必须在共享根（Task 0 已完成），
   迁移的 tar 中转落共享（Task 3）。
3. **运行时记录 `_runtime/<id>/sandbox.json` 继续留共享** —— uid 记账的全舰队口径不动
   （Task 4 不动它）。
4. **设计原则：默认写本地；只有"有已知跨节点消费者"的对象才在写的时候落共享**，
   "只是可能被跨节点读"的一律按需取。
5. **快照在生成时就打成 tar 落共享**（Task 2）。
6. **沙箱不是持久对象**：`/workspace` 随节点消失可接受；持久面是**快照与卷**；
   排水顺序"**先迁走、再下线**"是操作纪律，不是代码兜底。

（计划的"文件结构"表里写"五条裁定"，实际列了 1–6 六条；这里按六条照录，不做删减。）

### 5.2 本任务的证据裁定（Task 1 的写法纪律）

1. **活文档不许把读者送去 `tmp/`**：本任务跑过的脚本一律进
   `deploy/scripts/acceptance/`（`tests/unit/test_docs_only_point_at_repo_artifacts.py` 是钉子）。
   原始读数可以在 `tmp/task1/` 里被点名"曾经存在"，但复现路径必须是仓库里的文件。
2. **⓪ 用同款脚本重测**：`docs/create-local-first-layout.md` §3.1 的内联 heredoc 升格为
   `deploy/scripts/acceptance/local_first_storage_probe.py`，三种块大小一次跑完。
3. **① 不许把模拟当真实形状**：今天没有"树在本地"这个形状，所以本地那一列一律标注
   "worker 容器 `/var/lib/e2b-images`（同盘模拟）"，不写成"树在本地"。
4. **③ 只出测量与上限建议，不改代码**（改代码是别的任务）：峰值页缓存 vs 两个限额
   （worker 4 GiB 实测 / `maint` 512 MiB）+ §3.1 的具名上限形状。
5. **Step 2 的结论必须点名撞车**：`_snapshots` 合一的"合并不是覆盖"拒绝**没有触发过**
   （§4.2），Task 2 的路径推导据此落地。
6. **每个数字都要有出处**：§1–§4 的每一行都写出探针 + 日期 + 命令（§7）。

---

## 6. 对 Task 2/3/4 的约束（可直接抄进实现）

| 任务 | 这份文档给的约束 |
|---|---|
| Task 2（快照 tar） | 路径只有一个根：`<export>/_snapshots/<id>/{snapshot.json, fs.tar, .complete}`（§4.1）；"只有记录"的 `015f`/`1ca5` 是 `status: failed`，不得替它们造载荷（§4.2）；tar 通道要带 §3.1 的 `E2B_TREE_COPY_MAX_BYTES`（快照 ≤ 树上限 1 GiB） |
| Task 3（树本地） | 节点预算 8 GiB 树 + 4 GiB 镜像缓存 ≈ 12 GiB / 68–75 GiB 空闲（§2.3）；**大块顺序写会从 505 MB/s 掉到 125 MB/s**（§1.2），元数据快 480×（§1.1）——验收必须用"运行时 I/O"（小文件）而不是建箱延迟立论；**淘汰上限 = `E2B_TREE_COPY_MAX_BYTES` + 拷贝窗口**（§3.1），因为恢复路径 900 MiB 已到 `maint` 的 86% |
| Task 4（state 分家） | 裁定 1/3：`command-logs.jsonl` 可以本地、`_runtime/<id>/sandbox.json` 留共享（§5.1）；容量上本节点 state 是小文件，不是容量项（§2.3） |

---

## 7. 复现命令（全部 2026-10-02 实跑）

```bash
cd /Users/polus/project/ai/sandlock-e2b
deploy/scripts/open-cluster-tunnel.sh          # 通道 + 集群身份自检
export KUBECONFIG="$PWD/tmp/k0s/kubeconfig"
kubectl get nodes -o wide                       # 2 节点 / arm64 / v1.36.4+k0s
```

### 7.1 ⓪ 三种块大小 × 两个根（§1.1/§1.2）

```bash
kubectl -n sandlock exec -i e2b-worker-0 -- python3 - \
  --root nas:/var/lib/e2b-sandboxes/workspaces \
  --root local:/var/lib/e2b-images \
  --seq-mb 64,256,1024 --repeat 10 --small-n 200 --chunk-log \
  < deploy/scripts/acceptance/local_first_storage_probe.py
```

### 7.2 ① 沙箱里的顺序写（§1.3）

```bash
export E2B_API_URL=http://172.18.78.49:3000 E2B_SANDBOX_URL=http://172.18.78.49:3000
export E2B_API_KEY=$(kubectl -n sandlock get secret e2b-secrets \
    -o jsonpath='{.data.E2B_API_KEYS}' | base64 -d | cut -d, -f1)
env -u http_proxy -u https_proxy -u all_proxy tmp/venv/bin/python \
  deploy/scripts/acceptance/local_first_sequential_write_probe.py --seq-mb 900 --repeat 10
```

### 7.3 ② 容量（§2）

```bash
kubectl -n sandlock exec -i <agent-pod> -c maint -- python3 - \
  < deploy/scripts/acceptance/local_first_capacity_account.py
```

### 7.4 ③ 页缓存（§3）

```bash
env -u http_proxy -u https_proxy -u all_proxy tmp/venv/bin/python \
  deploy/scripts/acceptance/local_first_pagecache_acceptance.py \
  --repeat 10 --snapshot-repeat 3 --sampler-seconds 18 --restore-seconds 10
```

### 7.5 Step 2 复核（§4）

```bash
kubectl -n sandlock exec -i <cp-pod> -c control-plane -- python3 - \
  < deploy/scripts/acceptance/local_first_snapshot_verify.py
```
