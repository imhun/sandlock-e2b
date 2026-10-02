# 沙箱盘上材料的介质归属（根重切后的目录地图）

> **Task 0 Step 1 的产品**，2026-10-02 在集群实测（自建 k0s，2 节点全 arm64，
> `sandlock` namespace）。判定标准只有一条：**这份数据被"另一个节点上的读者"
> 需要吗** —— 需要就必须共享，否则可以本地。
>
> 原始的逐条清单在 `tmp/sandbox-tree-inventory.md`（gitignored），本文件是它的
> 结论面 + 根重切之后的目标形状。

## 0. 为什么要有这份地图

今天一根 `E2B_WORKSPACE_BASE` 同时决定三件事：

1. 沙箱树在哪（`<shared>/workspaces/<id>`）；
2. 平台自己的共享命名空间挂在它下面的哪（`_snapshots` 载荷、`_migrate`）；
3. "树是不是共享"这个迁移判据取什么值
   （`control_plane/api/sandboxes.py:2687` 的 `shared = bool(settings.shared_workspace_root)`）。

三件事共用一根，所以"把树搬去节点本地盘"不是改一个值：改了它，平台命名空间会一起
被拖到本地盘上，跨节点的读者就再也看不到它们了。根重切要做的就是**把这根拆开**。

---

## 1. 五个根（根重切之后）

| 变量 | 重切前的值 | 重切后的值 | 介质 | 谁写 | 谁读（跨节点？） |
|---|---|---|---|---|---|
| `E2B_WORKSPACE_BASE` | `<shared>/workspaces` | 同左（Task 3 才改指 `/var/lib/e2b/workspaces`） | 共享 → **Task 3 起节点本地** | worker / agent 建树；**沙箱自己**在其上读写 | ① 沙箱（同节点，bind mount）② worker files API（同节点）③ **迁移目标节点**（经裁定 2 的 tar 中转）④ agent 拍快照（同节点） |
| `E2B_NODE_STATE_BASE`（新） | — | `/var/lib/e2b/state` | **节点本地** hostPath | Task 4 起：`.creating` / disk-stats / `.route-b/**` / uid 池本地件 | 只有本节点（同节点） |
| `E2B_STATE_BASE` | `<shared>/state` | 同左 | 共享 | worker（`_runtime` 记录、命令日志）、沙箱 slot 进程（checkpoint） | ① 本节点 worker ② **别的节点的 worker**（`uid_pool._recorded_uids`）③ CP 只推路径，不直读（远程形态是代理） |
| `E2B_SHARED_VOLUME_ROOT` | `<shared>` | 同左（`_snapshots`、`_migrate` 搬进来） | 共享 | CP（`_builds`/`_templates`/`_volumes/_meta`/`_snapshots` 记录）、agent（`_snapshots` 载荷） | CP 两个副本之间；`_oci.tar` 是唯一的真·节点间交付面 |
| `E2B_IMAGE_CACHE_DIR` | `/var/lib/e2b-images` | 同左 | **节点本地** hostPath | 本节点 worker（解包 rootfs） | 只有本节点 |

`<shared>` = `/var/lib/e2b-sandboxes`，共享 PVC（阿里云 NAS）。

**要点**：重切**不动介质**，只把"谁在哪"写清楚。介质翻转是 Task 3 的事，那时只改
`E2B_WORKSPACE_BASE` 的指向与 `E2B_TREES_SHARED` 这一个判据。

---

## 2. 两条搬家

### 2.1 `_snapshots`：两个命名空间 → 一个（**实测矩阵**）

`control_plane/registry/snapshots.py:340-361` 的注释说记录与载荷是同处同一对象，
但实测是两个目录、同一批 id：

| id | `<shared>/_snapshots/<id>/`（记录，控制面写） | `<workspaces>/_snapshots/<id>/`（载荷，agent 写） | 时间 |
|---|---|---|---|
| `snap_015f907ace11c1ea` | `snapshot.json` | — | 09-27 |
| `snap_1ca5ab3332906e32` | `snapshot.json` | — | 09-27 |
| `snap_46dc467759dbbfb7` | `snapshot.json` | `.complete` + `fs/` | 10-01 |
| `snap_4bf1225dfbf54e0c` | `snapshot.json` | `.complete` + `fs/` | 10-01 |
| `snap_962c14802d6cbd50` | `.complete` + `fs/` + `snapshot.json` | — | **09-26** |
| `snap_ce90ef9852fc6809` | `snapshot.json` | `.complete` + `fs/` | 09-27 |
| `snap_2bb1fe18f81d4d77` | — | `.complete` + `fs/` | 10-01 |
| `snap_a6e1470cca1c2c09` | — | `.complete` + `fs/` | 10-01 |

三类，各有各的处置：

* **两边都有**（`46dc…`、`4bf1…`、`ce90…`）⇒ 把载荷目录**移到记录旁边**，一个 id
  一个目录。这是"合一"的正面情形，`rename(2)` 即可。
* **只有记录**（`015f…`、`1ca5…`）⇒ 有记录没载荷，恢复会失败。合并脚本**不许**替它
  编一个载荷，也不许把记录删掉；原样留着并**具名报告**（这是数据问题，不是格式问题）。
* **只有载荷**（`2bb1…`、`a6e1…`）⇒ 有载荷没记录。同样原样留着 + 具名报告：
  记录是控制面在成功之后写的，所以"载荷在、记录不在"意味着那次快照没成功收尾。

**时间线给的因果**：`snap_962c14802d6cbd50`（09-26）三样都在共享根，是**下沉前的
布局**；从 09-27 起载荷开始落在 `<workspaces>/_snapshots/`。所以这多半是 N27
"树根下沉"顺手把载荷根与记录根拆开了 —— 根重切把它合回来，正好和
`registry/snapshots.py` 的注释对上。

**合并后的形状**：

```
<shared>/_snapshots/<id>/snapshot.json      ← 控制面（记录）
<shared>/_snapshots/<id>/fs/ + .complete    ← agent（载荷；Task 2 换成 fs.tar）
```

### 2.2 `_migrate` 上浮到共享根

今天：`<workspaces>/_migrate`（`envd_service/agent.py:4199`、`:4260`、
`control_plane/api/sandboxes.py:2570`），是**空**的，只在
`if not shared:` 的迁移分支里用 —— 而那两条分支今天**从没跑过**
（`E2B_SHARED_WORKSPACE_ROOT` 在清单里设着，判据恒为 `1`）。

按裁定 2，跨节点一律经共享中转，所以这个中转面必须在**共享根**
（`<shared>/_migrate`）：它的读者是**另一个节点**的目标 agent，挂在树根命名空间下
那边根本看不到。

---

## 3. `<workspace>` 本地 vs 共享：实测与逐条评估

### 3.1 实测（2026-10-02，`e2b-worker-0` 内，串行，n=200；脚本见本节的复现命令）

| 负载 | 节点本地（容器 `/tmp`，同一块 nvme） | 共享 NAS（`/var/lib/e2b-sandboxes`） | 倍率 |
|---|---|---|---|
| 写 200 个 64 B 文件（open+write+close） | 5.6 ms（**0.028 ms/个**） | 2612 ms（**13.06 ms/个**） | **466×** |
| 64 MiB 顺序写（带 fsync） | **1027 MB/s** | 435 MB/s | 2.4× |

复现：

```
kubectl -n sandlock exec -i e2b-worker-0 -- python3 - <<'PY'
import os, shutil, time
def bench(label, root, n=200, size=64):
    d = os.path.join(root, "_bench.tmp.%d" % os.getpid())
    try:
        os.makedirs(d, exist_ok=True)
        t = time.perf_counter()
        for i in range(n):
            with open(os.path.join(d, "f%04d" % i), "wb") as f:
                f.write(b"x" * size)
        dt = time.perf_counter() - t
        print("%-28s %8.1f ms  %7.3f ms/file" % (label, dt*1000, dt/n*1000))
    finally:
        shutil.rmtree(d, ignore_errors=True)
bench("local", "/tmp")
bench("nas", "/var/lib/e2b-sandboxes/workspaces")
PY
```

**两条推论**：

* 共享卷的死穴是**写小文件**，不是带宽。13 ms 是每次 `open(create) + write + close`
  的 RPC 往返（NFSv4 的 create/write/close-commit），挂载参数已经调过
  （`vers=4.0 / rsize=wsize=1M / hard`，见 `deploy/k8s-k0s/storage-nas.yaml`）
  且 v4.0 是锁语义的硬要求 —— 这不是调参能消掉的。
* **计划里那条"节点 ESSD 写 186 / 冷读 125，慢于 NAS 381 / 165"的旧读数是错的**
  （本机重测：本地 1027 / NAS 435）。那次本地数很可能测偏了（当时是 agent
  `maint` 容器 512 MiB 限额下、刚 OOMKilled 重启之后）。**在 Task 1 Step 1 重测
  之前，不拿它当判据。**

沙箱自己的 `/workspace` 就是 bind mount 到 worker 里的这条 NAS 路径
（`envd_service/executors/sandlock.py:2226`），所以在共享形态下，沙箱里
`npm install` 一个三万文件的依赖树，串行口径是 **约 400 秒 vs 0.8 秒**（真实负载有
并发会好一些，量级不变）。**这是产品级的差别，比建箱那 40 ms 重要得多。**

### 3.2 跨节点：放本地到底有没有问题

结论先说：**计划内的迁移是可以做的，而且是"写好了但从没跑过"的代码；真正的硬伤是
"源节点不可达"。**

**① 判据不换，迁移会静默变成空操作（最危险的一条）**

`control_plane/api/sandboxes.py:2687` 是 `shared = bool(settings.shared_workspace_root)`，
而这一个 `shared` 同时决定三件事：要不要导 tar、要不要在目标节点建树、源节点的树
留不留（`_destroy_on_node(..., keep_files=shared)`）。清单里
`E2B_SHARED_WORKSPACE_ROOT` 是设着的 —— 只把 `E2B_WORKSPACE_BASE` 指到本地而不换判据，
迁移会变成：**不导出 → 改记录 → 在目标节点建一个空树 → 源节点的树留下**。
记录说沙箱在 B，树其实还在 A，而且 A 那棵树**不会被回收**（它的 id 在记录里，
GC 判它是 `protected` 而不是 orphan）。这就是 Task 0 立 `E2B_TREES_SHARED` 的原因：
介质与判据必须分开命名。

**② 源节点不可达 = 树不可达，而且无法事后补救（硬伤）**

导出端点本身在**源节点**上（`_export_sandbox_archive` 打的是
`{source}/agent/sandboxes/{id}/export`）。所以：

* 今天（共享）：节点没了，树还在 NAS 上，记录也在（`state/_runtime` 共享），
  把记录 re-home 到别的节点就能继续；数据至少还在，能人工取。
* 本地化后：节点没了就是树没了，**而且"迁走"这条路也没了**（导出要源节点活着）。
  ⇒ "先迁走、再下线/排水"是唯一可用的操作顺序，节点意外掉线时没有兜底。

**裁定（2026-10-02，裁定 6）：沙箱不是持久对象** —— `/workspace` 随节点一起消失是
可接受的；**需要持久化的用户文件走额外挂载的卷**（卷切片在共享卷上、按
`volume_node_id` 钉住节点）。所以这一条不需要代码兜底，但"**先迁走、再下线**"是
操作纪律：节点意外掉线时没有事后补救的路。

**③ 迁移的数据在途：整棵树进控制面内存**

今天是 `_export_sandbox_archive` 写 tar 到控制面的 `_migrate`，再
`_import_sandbox_archive` 把 tar 读成 `content=` POST 给目标 agent。两处都是
**整棵树进内存**（`resp.content` / `tar_path.read_bytes()`），而控制面限额 **2 GiB**。
一棵 1 GB 的树迁移就是一个 2 GiB 的进程。另外两端各 `timeout=120`，
按 NAS 上 13 ms/文件算，**约 4600 个文件就到上限**。共享形态下迁移从不搬字节，
所以这两条今天从来没暴露过；一旦本地化，它们立刻是活的。

**④ 迁移中断的残留：不会丢数据，但会留一份永远不被回收的副本**

顺序是"记录先切到目标（`record.node_id = target` 并落盘）→ 再导 tar / 建树"。
控制面崩在这两步之间，记录就指向一个**还没有树的节点**。此时：

* 源节点那棵树的 id **在记录里** ⇒ GC 判它是 `protected`、不删 ⇒ **不会丢数据**；
* 但它也不会被回收 —— 直到该沙箱被删除、记录消失，源节点那棵树才变成 orphan 被清掉。

也就是说：**数据安全，代价是一段有界的磁盘泄漏**（源节点上留下整棵树），
以及"记录指向的节点上没有树"这个状态需要一个具名的修复动作。

**⑤ 孤儿回收不用改，本地化之后反而更准**

回收已经是"**agent 巡检 → 控制面决策 → agent 执行**"的 per-node 设计
（`control_plane/self_heal.py`：`run_sweep` 一次只判一个节点）。今天每个 agent
因为共享挂载**看得见全舰队的树**，同一棵 orphan 会被多个节点各报一次；
本地化后每个 agent 只报自己节点上真实存在的树，判定反而更干净。
`run_sweep` 里那个 `DEFER_NO_SHARED_STORE` 说的是**记录存储**不共享就推迟
（`fleet_id_snapshot().shared` 来自注册表有没有 `_record_store`），
**和树在哪没有关系** —— 记录按裁定 3 留在共享，这条守卫就一直是满足的。

**⑥ 快照：生成略快，恢复快 6 倍**

* 生成：今天树在 NAS、载荷也在 NAS（NAS→NAS 约 26 ms/条目）；本地化后是
  本地→NAS 约 22 ms/条目。差别不大 —— 而且 Task 2 把它换成整棵 tar 之后，
  口径从"按条目"变成"按字节"。
* 恢复：今天 NAS→NAS 26 ms/条目；本地化后是 NAS→本地 **4.4 ms/条目**。

**⑦ 卷：不受影响**

卷切片按 `volume_node_id` 把沙箱钉在节点上，`migrate` 遇到卷钉在源节点时直接
409 拒绝（`_migration_volume_node_id`）。这条路本地/共享一个样。

**⑧ 容量与读数：本地化会让读数第一次有意义**

* `E2B_NODE_DISK_MB=8192`（调度额度）+ 每沙箱默认 `E2B_DEFAULT_DISK_MB=1024`
  ⇒ **8 个沙箱/节点**的硬上限；节点盘实际还有 75 G 空闲（镜像缓存同盘，
  被 `E2B_IMAGE_CACHE_MAX_BYTES=4 GiB` 限住）。
* 今天节点心跳报的 `usedDiskMB` 是 **NAS 的用量**（实测 566,766 MB），
  而同一行里的调度预算是 **8,192 MB** —— 两个口径并存，正是"树在共享、额度按
  节点算"的错配。本地化之后这两个数才同源。

### 3.3 裁定建议

**采本地**，理由写进计划时用"运行时 I/O"（466×）而不是"建箱延迟"（40 ms）。
跟着必须一起做的三件事：① 判据换 `E2B_TREES_SHARED`（上面 ①）；② 迁移途径的
内存与超时上限（上面 ③）；③ 明确"沙箱不是持久对象、持久面是快照"（上面 ②），
并在排水流程里写死"先迁走再下线"。

---

## 4. 刻意不动

已经在共享根、且其读者确实跨节点或跨 CP 副本的：`_images`（`_oci/*.oci.tar` 是
唯一的真·节点间交付面）、`_templates`、`_builds`、`_volumes`、`_secrets`、`state/`。

`_cow` 是空的保留名（`gateway_common/paths.py` 的 `RESERVED_PLATFORM_NAMESPACES`），
名单不删。

卷数据（`_volumes/<id>/` 切片）按 `volume_node_id` 钉在节点上，本轮不动 —— 它的
"可以本地"取决于调度器是否愿意把卷也放本地，那要单独立项。
