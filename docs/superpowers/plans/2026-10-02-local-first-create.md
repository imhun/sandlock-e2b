# 建箱存储本地优先（local-first）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按"**默认本地写、只有需要共享时才落共享**"改造存储布局，收益有两笔，**主笔是运行时 I/O 不是建箱**：① 沙箱里写一个小文件 **13.06 → 0.028 ms**（466×，这是用户每天感受得到的那笔）；② 建箱 **127 → ~60 ms** —— 但**只有 Task 3 + Task 4 同时到位才拿得到**（建箱地板是 `max(materialize, prepare)`，单独做任一条只值个位数毫秒）；③ 快照从 **26 ms/条目** 拉向**按字节付费**。

**Architecture:** 三个对象换位置 —— ① 快照落成 **tar** 放共享（恢复任何节点都可做，不需要 p2p）；② 沙箱树搬**节点本地**，跨节点（迁移）经控制面/共享的 `_migrate` 中转；③ `.creating`/disk-stats/route-B/uid 池本地件搬到**节点本地的 state**。共享侧只保留：运行时记录 `sandbox.json`、checkpoint、快照 tar 与记录、卷与 `_meta`、`_templates`/`_builds`/`_oci.tar`。**逐条评估（含跨节点迁移的五个失效面）见 `docs/create-local-first-layout.md` §3。**

**Tech Stack:** Python（`control_plane/`、`c3_agent/`、`envd_service/`、`gateway_common/`）、k8s（k0s、arm64）、NFS（阿里云 NAS）+ 节点 ESSD、tar。

## 当前进度（2026-10-02，**Task 0 已完成并上线**）

**已完成 —— Task 0 全部（Step 1–8），线上版本 `0.1.0-887-g7ef319b-20261002-100406`**

| 项 | 状态 | 证据 |
|---|---|---|
| Task 0 Step 1 目录地图 + `_snapshots` 实录 | ✅ | `docs/create-local-first-layout.md`（含 §3 的本地 vs 共享逐条评估）。实测：写一个小文件 **共享 13.06 ms / 本地 0.028 ms（466×）**；顺序写 **NAS 435 / 本地 1027 MB/s**（旧的"本地 186"作废）。`_snapshots` 8 个 id：3 个两边都有、2 个只有记录、2 个只有载荷 |
| Task 0 Step 2/3 失败用例 + 先红 | ✅ | `tests/unit/test_root_reslice.py` 17 条；先红 16 条（留 1 条绿的是"今天的根列表不许变"） |
| Task 0 Step 4 实现 | ✅ | commit `308b543`。见下方 Step 4 的实现清单 |
| Task 0 Step 5 清单 | ✅ | 本批提交。control-plane 的 `_migrate` subPath + `E2B_TREES_SHARED`；`workspace-root-init` 的两个根搬到 `$shared`；worker 与 compose 三份**核过不用改** |
| Task 0 Step 6 迁移脚本 N58 | ✅ | 本批提交。`scan_uplift()`：上浮 + 合一 + 具名拒绝同名；`rmdir` 拆壳带 journal 与回退；判据加"兄弟层已是新布局" |
| 回归 | ✅ | `tests/unit` **2211 passed / 12 skipped / 3 failed**，与基线（2194 + 新增 17）逐条对齐，3 条是已知 macOS-only。`tests/contract/test_migration.py` 的 6 条失败**在干净树上同样失败**（macOS 没有 container id），非本轮引入 |
| 回归（本批） | ✅ | `tests/unit` **2221 passed / 12 skipped / 3 failed**（+10：本轮新增 11 条，多出的那一条 failed 是 `test_create_deferred_persist` 在满载下的时序抖动，单独跑绿）；3 条仍是那三条 macOS-only |
| 彩排（本机） | ✅ | `deploy/scripts/acceptance/migrate_state_base_rehearsal.py`：dry-run 不写、apply 后 inode 不变、再跑报「没有可搬的条目」、`--rollback --apply` 原样退回 |
| 彩排（集群，**只读**） | ✅ | 在控制面 pod 里跑 `--mode plan`：`moves=9 dirs=3 todo=14`，无冲突（见 Step 7） |
| 上线（Step 7 的停机那一半） | ✅ | `docs/deploy-clusters.md` §7.29。顺序：关自动扩缩器（新坑，见 §7.29）→ worker 缩 0 → 迁移 → apply → 起 worker；迁移 `done=14`，`GET /sandboxes`=`[]`，`DRY_RUN` diff 0 行 |
| 上线当场抓到的一次回归 | ✅ 已前滚修 | `derive_materialize` 的 `copy_from` 指向树根 ⇒ 从快照建箱全 502；`7ef319b` 修 + 复验（4/4 老快照恢复）|

**没做**：Task 1–6 全部未开始（介质翻转在 Task 3，快照 tar 在 Task 2）。

⚠ **上线顺序已执行过一次**（`308b543` + `4ac410b` 同一次上线，2026-10-02）。下一个动到根的提交要照 §7.29 那套走：**先 `E2B_AS_ENABLED=false` 关掉抢副本的循环，再 worker 缩到 0 → 迁移（先 dry-run）→ apply 新清单 → 起 worker → 验证**。回退窗口仍然开着：`state/.state-base-migration.journal` + `migrate-state-base.sh --rollback --apply`，但**回退后必须同时回退镜像**。

**Task 0 先行（目录与介质归属）**：今天一根 `E2B_WORKSPACE_BASE` 同时决定三件事 —— 沙箱树在哪、平台共享命名空间（`_snapshots` / `_migrate`）挂在它下面的哪、以及"树是不是共享"这个迁移开关取什么值（`control_plane/api/sandboxes.py:2687` 的 `shared = bool(settings.shared_workspace_root)`）。三件事共用一根，所以"把树搬本地"不是改一个值的事：改了它，平台命名空间会一起被拖到节点本地盘。先拆成**五个具名根 + 一个具名判据**（见 Task 0 的表），顺手把 `_snapshots` 的两个命名空间合成一个、把 `_migrate` 上浮到共享根；**默认部署的行为逐字不变**（树仍在共享），介质翻转留到 Task 3 改两个值。

**这不是从零开始。** 本计划的输入是这三份东西（都不在版本库里，先把它们落成文档）：

- 测量：`tmp/local-disk-eval-measurements.md`（本地 vs NAS 的元数据与拷贝、节点间带宽、两条硬约束）
- 盘点：`tmp/sandbox-tree-inventory.md`（每个命名空间谁读、是否跨节点、证据行）
- 本计划 §Global Constraints：用户在这一轮做的裁定，**逐条照录**

## Global Constraints

- **用户裁定 1**：节点**必有** agent（否则特权操作无从执行）⇒ `command-logs.jsonl` 的远程形态一律是"代理到该节点 worker"，直读只属于 `local://`；因此它可以本地。
- **用户裁定 2**：迁移**可以把 tar 放共享存储**，"上传和恢复从本地到共享的传输"可接受，**其他需要跨节点的操作都可以这么做**（即：跨节点 = 经共享中转，不要求 p2p）。
- **用户裁定 3**：运行时记录（`_runtime/<id>/sandbox.json`）**继续留共享** —— uid 记账的全舰队口径不动。
- **用户裁定 4（设计原则）**：**默认写本地；只有"有已知跨节点消费者"的对象才在写的时候落共享；"只是可能被跨节点读"的一律按需促升/按需取。**
- **用户裁定 5**：**快照在生成时就打成 tar 落共享**（不是"爆炸式 `fs/` 目录落 NAS"）。
- **用户裁定 6**：**沙箱不是持久对象** —— `/workspace` 随沙箱所在节点一起消失是可接受的；**需要持久化的用户文件走额外挂载的卷**（卷切片在共享卷上，按 `volume_node_id` 钉住节点，`migrate` 遇到卷钉在源节点时一律 409）。⇒ 节点掉线丢树不需要代码兜底，但**排水顺序"先迁走、再下线"是操作纪律**；持久面是快照与卷。
- **不变（已逐条查实）**：卷数据与其切片（调度器按 `volume_node_id` 把沙箱钉在卷所属节点）、`_volumes/_meta`、`_templates`、`_builds`（都是**控制面副本间**的共享，worker 从不读）、`_oci/*.oci.tar`（唯一的真·节点间需求，且已是"共享放分发产物"的正确形状）。`_cow` 是空的保留名，保留名单不删。
- **硬约束（实测，2026-10-02 复核）**：每节点只剩 **75 G** 可用盘，且与 4 GiB 的镜像解包缓存**同盘**；`E2B_NODE_DISK_MB=8192` + 每沙箱默认 `E2B_DEFAULT_DISK_MB=1024` ⇒ **8 沙箱/节点**的调度上限（本地化后这个数才第一次与"节点盘"同源；今天心跳报的 `usedDiskMB` 是 NAS 的 566,766 MB）。容器限额：worker / 控制面各 **2 GiB**、agent 的 face A **256 MiB**、face B `maint` **512 MiB** —— 实测一次 500 MB 的本地写把 `maint` 打 OOMKilled 过一次。`agent ↔ agent` 被 NetworkPolicy 挡住（只有 CP→agent）。
- ⚠ **作废的旧读数**：本计划早先引用的"节点 ESSD 写 186 / 冷读 125 MB/s，慢于 NAS 的 381 / 165"**不再作为判据**。2026-10-02 在 `e2b-worker-0` 重测：本地 **1027 MB/s** vs NAS 435 MB/s（`docs/create-local-first-layout.md` §3.1）。Task 1 Step 1 重测之前，任何以旧数为前提的推理都算未验证。
- 断言精确匹配、禁 SKIP/xfail；编辑一律 `apply_patch`；每条"能失败"的钉子必须证明它会红。

## Review Focus

1. **tar 解包是路径逃逸的现场**：`..`、绝对路径链接、半棵树 —— §4.3.1 四条硬要求在解包路径上**逐条重新钉**，不能靠"只是换了个容器"。→ Task 2
2. **按需促升不能丢语义**：树本地之后，迁移/fork/快照/孤儿回收/磁盘扫描必须都能拿到数据；"取不到"要具名失败而不是静默降级。→ Task 3
3. **共享记录的全舰队口径**：`sandbox.json` 留共享之后，`uid_pool._recorded_uids`（靠枚举**树目录名**当索引）会失去视野 —— 索引要改成共享的记录目录，不许降级 fallback。→ Task 4
4. **快照完整性**：单 tar 意味着"写坏一半"看起来像个完整文件；沿用临时名 + fsync + rename + `.complete`。→ Task 2
5. **本地盘的容量与页缓存**：树进 75 G 的节点盘（与 4 GiB 镜像缓存同盘）、`E2B_NODE_DISK_MB=8192` 只够 8 个沙箱、页缓存计入 worker 的 2 GiB / `maint` 的 512 MiB（已 OOM 过一次）。→ Task 1（前置测量）+ Task 3 的淘汰策略
6. **介质归属必须显式，不能从路径推**：一根 `E2B_WORKSPACE_BASE` 同时决定树的介质、平台命名空间的位置和迁移判据，是"树搬本地"卡住的根因。→ Task 0
7. **合并不能静默取一个**：`_snapshots` 两处合一，两边同名文件必须具名拒绝。→ Task 0 Step 6
8. **一条从没跑过的分支要变成主路径**：`shared=False` 的迁移分支在本部署一次都没执行过，而且它的在途是"整棵树进控制面内存 + 固定 120 s 超时"。→ Task 3
9. **"树在哪"与"判据取什么"必须分开命名**：一个 `shared` 同时决定导出/建树/留源，翻错一个值就是静默建空树。→ Task 0 + Task 3

---

## 文件结构

| 文件 | 责任 |
|---|---|
| `docs/create-local-first-design.md`（新） | 把两份 tmp 报告 + 五条裁定落成正式设计（本计划的权威） |
| `envd_service/agent.py`（改） | 快照生成改 tar；树本地化后的迁移/恢复入口；state 分家 |
| `control_plane/api/sandboxes.py`（改） | `_export/_import_sandbox_archive` 的中转落点；迁移路径 |
| `c3_agent/materialize.py`（改） | `copy_from` 从"合并目录"改成"取 tar + 本地解包"（含硬化） |
| `control_plane/file_ops.py`（改） | `derive_materialize` 的 `copy_from` 形状（指向 tar） |
| `gateway_common/paths.py`（改） | 节点本地 state 的路径 helper |
| `control_plane/config.py`、`c3_agent/config.py`、`envd_service/config.py`（改） | 节点 state base、树开关、淘汰上限 |
| `deploy/k8s/{worker,c3-agent,control-plane}.yaml`（改） | 节点本地卷（hostPath/local-path）+ 新的 env |
| `deploy/scripts/acceptance/snapshot_create_probe.py`（改） | 1/40/202 三档的前后对照 |
| `docs/create-local-first-layout.md`（新，Task 0 Step 1） | 重切后的目录地图：路径 · 介质 · 写者 · 读者 · 跨节点（Task 1 往里补读数） |
| `c3_agent/priv/priv_common.c`（改） | `priv_root_paths` 的五根顺序，与新根 `E2B_NODE_STATE_BASE`（Task 0） |
| `deploy/scripts/migrate-state-base.sh`（改） | N58 阶段：`_snapshots` 合并 + `_migrate` 上浮（同 PVC 逐条 `rename(2)`，含回退） |

---

### Task 0: 根重切（目录与介质归属，**默认行为不变**）

**为什么必须在最前**：这是把"介质归属"从路径推导里解开的那一步。今天 `<shared>/workspaces/<id>` 是沙箱树，`<shared>/workspaces/_snapshots/<id>/fs` 是 agent 写的快照载荷（`envd_service/agent.py:4357`），`<shared>/workspaces/_migrate` 是迁移中转（`envd_service/agent.py:4199`、`control_plane/api/sandboxes.py:2570`）—— 三个东西共享一根，而其中只有第一个该搬去节点本地盘。Task 0 只做**归属与命名**，不搬介质。

**Files:** Create `docs/create-local-first-layout.md`（Step 1 的产品）；Modify `gateway_common/paths.py`、`control_plane/file_ops.py`、`control_plane/config.py`、`c3_agent/config.py`、`envd_service/config.py`、`c3_agent/priv/priv_common.c`、`envd_service/agent.py`、`control_plane/api/sandboxes.py`、`control_plane/app.py`、`deploy/k8s/{worker,c3-agent,control-plane}.yaml`、`deploy/k8s-k0s/*.patch.yaml`、`deploy/{stack,compose}/*.yml`、`deploy/scripts/migrate-state-base.sh`；Test `tests/unit/test_root_reslice.py`（新）、`tests/unit/test_migrate_state_base_script.py`（改）。

**Interfaces:** Produces 五个具名根 + 一个具名判据：

| 变量 | 今天 | Task 0 之后 | 介质 | 装什么 |
|---|---|---|---|---|
| `E2B_WORKSPACE_BASE` | `<shared>/workspaces` | **不变**（Task 3 才改指 `/var/lib/e2b/workspaces`） | 共享（Task 3 起：节点本地） | `<id>` 沙箱树 —— 沙箱的 `/workspace` **与** `/home/user`（`envd_service/executors/sandlock.py:2226`） |
| `E2B_NODE_STATE_BASE`（**新**） | — | `/var/lib/e2b/state` | 节点本地 hostPath | Task 4 才往里写（`.creating` / disk-stats / `.route-b` / uid 池本地件）；Task 0 只立变量、白名单与 config 字段 |
| `E2B_STATE_BASE` | `<shared>/state` | 不变 | 共享 | `_runtime/<id>/sandbox.json`（裁定 3）、`.checkpoints/**` |
| `E2B_SHARED_VOLUME_ROOT` | `<shared>` | 不变 | 共享 | `_snapshots/`、`_migrate/`（本任务搬进来）、`_volumes/`、`_templates/`、`_builds/`、`_images/`、`_secrets/` |
| `E2B_IMAGE_CACHE_DIR` | `/var/lib/e2b-images` | 不变 | 节点本地 hostPath | rootfs 解包缓存（已经是本地） |
| `E2B_TREES_SHARED`（**新**，判据） | 隐含在 `bool(shared_workspace_root)` | 显式 `1`/`0` | — | 迁移走"原地共享、只切路由"（`1`，今天的形状）还是"导出 tar → `_migrate` → 目标节点导入"（`0`，Task 3 翻） |

**两条搬家**（同一 PVC 内逐条 `rename(2)`，与 N27 同款；不是整树拷贝）：

1. `<workspaces>/_snapshots/<id>/*` **合并进** `<shared>/_snapshots/<id>/` —— 这是"`_snapshots` 是两个命名空间"的裁定：**合成一处**。今天的形状是同一 id 的**记录**在 `<shared>/_snapshots/<id>/snapshot.json`（控制面写，`control_plane/app.py:554` 的 `platform_root`）而**载荷**在 `<workspaces>/_snapshots/<id>/fs`（agent 写），`control_plane/registry/snapshots.py:340-361` 的注释却说两者同处同一对象。合并后一个 id 一个目录：`snapshot.json` + `fs/`（Task 2 起换成 `fs.tar` + `.complete`）。
2. `<workspaces>/_migrate` → `<shared>/_migrate`（裁定 2：跨节点经共享中转 ⇒ 中转面必须在共享根）。读者是**另一个节点**的目标 agent，今天这个目录挂在树根命名空间下，目标节点看不到它。
3. **不动**：`_images`、`_templates`、`_builds`、`_volumes`、`_secrets`、`state/`（已经在共享根），以及 `_cow`（保留名）。

- [x] **Step 1: 先量后写 —— 两处 `_snapshots` 的逐条实录** ✅ `docs/create-local-first-layout.md`（列：路径 · 介质 · 写者 · 读者 · 跨节点 · 今天/重切后；**这份实录是 Step 6 合并脚本的输入**）。
- [x] **Step 2: 写失败用例** ✅ `tests/unit/test_root_reslice.py` 17 条。
- [x] **Step 3: 先红** ✅ 16 红 1 绿（那条绿的是"今天的根列表逐字不变"）。
- [x] **Step 4: 实现** ✅ `308b543` —— `gateway_common/paths.py` 加 `platform_namespace_root` / `snapshot_payload_dir` / `migrate_staging_dir` + `SNAPSHOT_STORE_DIR_NAME` / `MIGRATE_STAGING_DIR_NAME`；`envd_service/agent.py` 的 5 处（建箱恢复、导出、导入、快照生成、快照删除）与 `control_plane/api/sandboxes.py` 的 `_migrate` 全走 helper；`ControlPaths.node_state_base` 第五根（顺序 workspace → node state → state → shared → cache，去重规则逐字不变）＋ `priv_common.c` 镜像同一个循环、`PRIV_MAX_ROOTS 4 → 5`；`E2B_TREES_SHARED`（`_trees_shared_from_env`，空 = 未命名 = 沿用 `bool(E2B_SHARED_WORKSPACE_ROOT)`）与 `Settings.__post_init__` 的落定；三个 config 的 `node_state_base` 字段；`c3_agent/fileops.py` 的子进程 env 只在命名时写这一根。
- [x] **Step 5: 清单** ✅ —— 实际改到的是**两处**，不是四份清单：
  - `control-plane.yaml`：`_migrate` 的 subPath 从 `workspaces/_migrate` 改成 export 根的 `_migrate`；显式加 `E2B_TREES_SHARED: "1"`（唯一读者就是控制面；Task 3 翻转就改这一个字符）。
  - `c3-agent.yaml` 的 `workspace-root-init`：`$base/_migrate` → `$shared/_migrate`、`$base/_snapshots` → `$shared/_snapshots`（含 chown/verify 判据与 1777 白名单）。
  - `worker.yaml` **不动**：它已经挂整卷，`_snapshots`/`_migrate` 都在同一挂载下，而 `E2B_TREES_SHARED` 不是它的输入。
  - **compose 三份车道不动**：它们本来就把树根与平台命名空间根放在同一个目录（`E2B_WORKSPACE_BASE` = `E2B_SHARED_*_ROOT`），`OWNED_DIRS` 早就是重切后的形状 —— 改它反而会把属主交接指向不存在的路径。
  - k0s overlay 的 patch **不动**：差异层里没有路径字面量，基线改了它自动跟上。
  Review 手段：`tests/unit/test_root_reslice.py` 加 4 条（两条渲染 kustomize 的对象级钉子 + compose 三份的 `OWNED_DIRS` 逐条），`tests/unit/test_worker_manifest_permissions.py` 的两条 N27 钉子改成 N58 形状（先红后绿）。
- [x] **Step 6: 迁移脚本 N58 阶段** ✅ `deploy/scripts/migrate-state-base.sh` 现在一次跑 N27+N58：
  - `scan_uplift()` 逐条算出「`_migrate` 上浮」+「`_snapshots` 合一」。目标 id 目录不存在 ⇒ 整条 `rename`；存在（有记录没载荷的 id）⇒ 逐条搬里面的条目；**两边同名 ⇒ 具名拒绝**（`合并不是覆盖：… 两边同名（先人工决定留哪一份，再重跑）`）。
  - 搬空之后用 `rmdir` 拆壳（**不递归**），journal 里记 `rmdir <rel> <mode>`，回退按原权限位重建。
  - 判据放宽了一条：`<export>/workspaces` 除了「有标记/是空的」以外，还接受「兄弟层 `state/` 已是新布局」——否则合一之后再想看一眼会被误判成"一棵碰巧叫 workspaces 的树"。
  - 彩排（本机 `--root`，`deploy/scripts/acceptance/migrate_state_base_rehearsal.py`）+ 集群只读计划都跑过：见下。
- [x] **Step 7: 集群验收** ✅ 上线窗口已跑（2026-10-02，版本 `0.1.0-887-g7ef319b-20261002-100406`，记录见 `docs/deploy-clusters.md` §7.29）：
  - ✅ 集群身份自检（2 节点 / arm64 / `v1.36.4+k0s` / `sandlock` 9 pod）
  - ✅ `GET /sandboxes` = `[]`
  - ✅ `DRY_RUN=1 apply.sh | kubectl diff -f -`：只有**两个**对象变（c3-agent 的 init 脚本、control-plane 的 env + subPath），worker 与其余对象零差异；渲染用的版本与线上相同，所以没有镜像 tag 行
  - ✅ 迁移的**集群只读计划**（在控制面 pod 里跑 `--mode plan`，不写一个字节）：`moves=9 dirs=3 todo=14` —— 1 条上浮 + 2 条整条 id 合一 + 3 条逐条合一（6 个文件）+ 3 个空壳 rmdir；2 个只有记录的 id 原地不动；**没有一条"两边同名"**
  - ✅ 迁移执行：`apply moves=9 dirs=3 todo=14 done=14`、逐条 `same_inode=yes`、12 条抽样 `sha_same=yes`、journal 0600；卷上 `<export>/_snapshots` 8 个 id、`<export>/_migrate` 1777、`<export>/workspaces` 空
  - ✅ 建箱 p50 前后各量一次：**124 → 135/136 ms**（逐段 `prepare`/`finalize`/`prime`/`record` 与 §7.28 逐条相同 ⇒ 无结构性回归）
  - ✅ 上线当场抓到并前滚修掉**一次回归**：`control_plane/file_ops.py::derive_materialize` 的 `copy_from` 还指向树根 ⇒"从快照建箱"全 502（`7ef319b`；钉子 `test_a_snapshot_create_carries_copy_from` 先红后绿）。复验：新快照往返 p50 208 ms、**被迁移过的 4 个老快照 4/4 恢复成功**
- [x] **Step 8: 提交** ✅ `4ac410b`（清单 + 迁移）+ `7ef319b`（`copy_from` 修复），已上线。

### Task 1: 前置测量与设计文档

> **进度落账（2026-10-02，本批收尾时补记）**：Task 1–6 的代码/文档/上线都由 SDD 批次执行完毕，
> 逐任务提交见 `.superpowers/sdd/2026-10-02-local-first-create/progress.md`，上线记录见
> `docs/deploy-clusters.md` §7.29–§7.32。本项目里**ledger 是权威**，这一段勾选是回溯补的。

（与 Task 0 并行；它决定 Task 3 的淘汰上限）

**Files:** Create `docs/create-local-first-design.md`；Modify `deploy/scripts/acceptance/`（只在需要新探针时）。

- [x] **Step 1: 三笔账 + 一笔复核**（集群实测，n≥10，写进设计文档）：⓪ **复核作废的吞吐读数** —— "本地 186 / NAS 381 MB/s" 已被 `docs/create-local-first-layout.md` §3.1 的实测推翻（本地 **1027** / NAS 435），用同款脚本按三种块大小重测并写死结论；① **沙箱里写大文件**：1 GB 顺序写在"树在本地"与"树在 NAS"两种形状下各多少 MB/s、多少秒（这决定树本地是不是对所有负载都划算）；② **容量账**：快照仓/树的日增量与保留窗口 → 每节点需要多少 G（当前 75 G，且与 4 GiB 镜像缓存同盘）；③ **页缓存账**：一次建箱/快照的峰值页缓存 vs worker 的 2 GiB 与 agent `maint` 的 512 MiB 限额，给出上限配置（`E2B_IMAGE_*` 那种上限的同款做法）。
- [x] **Step 2: `_snapshots` 两命名空间的**复核**（Task 0 已合并，这里只验）**：Task 0 Step 1 的实录 + Step 6 的迁移之后，`<shared>/_snapshots/<id>/` 应当同时有控制面的 `snapshot.json` 与 agent 的载荷（Task 2 之后是 `fs.tar` + `.complete`）。这里复核"合并无遗漏、重复 id 没有静默取一个"，并把结论写进设计文档。**这一步没复核过，Task 2 的路径推导就建在流沙上。**
- [x] **Step 3: 落文档**：把两份 tmp 报告（测量 + 盘点）的核心表与五条裁定搬进 `docs/create-local-first-design.md`，并指向 `tmp/` 里的原始读数。
- [x] **Step 4: 提交**（`docs(...)`）。

### Task 2: 快照打成 tar 落共享（收益最大、与树无关）

**Files:** Modify `envd_service/agent.py`（快照生成）、`c3_agent/materialize.py` + `control_plane/file_ops.py`（`copy_from` 形状与解包）、`deploy/scripts/acceptance/snapshot_create_probe.py`；Test `tests/unit/test_snapshot_tar.py`（新）、`tests/unit/test_agent_materialize.py`（改）。

**Interfaces:** Produces: 快照载荷为 `<...>/_snapshots/<id>/fs.tar`（+ 既有 `.complete`）；`derive_materialize` 的 `copy_from` 指向该 tar；`materialize_tree` 新增"取 tar + 就地解包进树根"的路径。

- [x] **Step 1: 先量基线**：跑 `snapshot_create_probe.py` 的 1/40/202 三档，把今天的 `26 ms/条目` 复现并记下（这是前后对照的基准）。
- [x] **Step 2: 写失败用例**：`test_a_snapshot_is_one_tar`（生成后目录里只有一个 `fs.tar` + `.complete`，没有爆炸式 `fs/`）；`test_a_restore_unpacks_the_tar_at_the_tree_root`（生产形状 `fs/workspace/kept.txt` ⇒ 树根下 `workspace/kept.txt`）；**§4.3.1 四条**在解包路径上各一条：`test_a_symlink_in_the_tar_is_recreated_not_followed`、`test_a_destination_symlink_segment_is_refused_named`、`test_a_partial_unpack_is_reported_as_failure`、`test_a_migrated_tree_keeps_its_files`；`test_a_truncated_tar_is_refused`（完整性）；`test_an_absolute_link_member_is_dropped`（对齐既有 `_extract_sandbox_archive` 的成员过滤）。
- [x] **Step 3: 先红**。
- [x] **Step 4: 实现**：快照侧 `tarfile` 写到**临时名** → `fsync` → `rename` → 最后写 `.complete`（与 `.oci.tar` 同款纪律）；恢复侧在 agent 里解包（**复用** `_extract_sandbox_archive` 的成员过滤与 `dest` 包含检查，不许自己再写一份）。
- [x] **Step 5: 后量对照**：同一三档再跑一遍，把"按条目"换成"按字节"的口径一起报。
- [x] **Step 6: 提交**。

### Task 3: 沙箱树本地化 + 迁移经共享中转

**为什么做（理由换过了）**：主笔是**运行时 I/O** —— 沙箱自己的 `/workspace` 就是 bind mount 到那条 NAS 路径，写一个 64 B 文件实测 **13.06 ms**（本地 0.028 ms，466×），`npm install` 量级的负载是分钟级 vs 秒级。建箱只是次笔，而且**必须与 Task 4 一起到位**：地板是 `max(materialize, prepare)`，只本地化树 ⇒ 地板仍是 `prepare ≈73 ms`；只做 Task 4 ⇒ 地板仍是 `materialize ≈71 ms`。完整评估与跨节点逐条分析见 `docs/create-local-first-layout.md` §3。

**Files:** Modify `deploy/k8s/{worker,c3-agent}.yaml`（节点本地卷 + 开关）、`control_plane/api/sandboxes.py`（`_export/_import_sandbox_archive` 的中转落点与在途上限）、`envd_service/agent.py`；Test `tests/unit/test_tree_local_migration.py`（新）；文档 `docs/create-local-first-layout.md`（排水顺序的裁定）。

**Interfaces:** Consumes: Task 0 的 `E2B_TREES_SHARED` 判据与 Task 2 的 tar 通道。判据为 `0` 时走 `_export_sandbox_archive` / `_import_sandbox_archive` —— `control_plane/api/sandboxes.py:2752` / `:2774` 的 `if not shared:` 两个分支就是完整实现，只是今天判据恒为 `1`（`E2B_SHARED_WORKSPACE_ROOT` 在清单里设着），那两条分支**在本部署里从没跑过一次**。

**跟着必须一起做的三件事**（否则 Task 3 会引入静默故障，不是"稍后再补"）：

1. **判据必须换名字**：`shared` 一个变量今天同时决定"要不要导 tar / 源节点的树留不留"（`_destroy_on_node(..., keep_files=shared)`）。只把树搬本地而不换判据 ⇒ 迁移会**不导出 → 改记录 → 在目标建空树 → 源节点的树留下**，而源节点那棵树因为 id 还在记录里，GC 只会判它 `protected`、永不回收。→ Task 0 的 `E2B_TREES_SHARED`。
2. **在途不能整棵树进内存**：现在两端都是 `resp.content` / `tar_path.read_bytes()`，控制面限额 **2 GiB**，两端各 `timeout=120`（按 13 ms/文件算约 4600 个文件就到顶）。改成流式 + 按字节的具名上限与超限拒绝。
3. **"源节点不可达"要具名**：导出端点在**源节点**上，所以本地化后源节点掉线 = 树不可达且无法事后迁出。把它写成一个具名错误（不是 502 泛化），并在文档里定下排水顺序：**先迁走、再下线**。

- [x] **Step 1: 写失败用例**：`test_a_local_tree_is_not_visible_from_the_shared_volume`；`test_migration_moves_the_tree_through_the_shared_store`（源节点导出 tar → 中转暂存 → 目标节点导入，断言目标节点树的内容与源一致，**且源节点的那棵树被删掉**）；`test_the_migration_judge_is_not_the_shared_root`（`E2B_SHARED_WORKSPACE_ROOT` 设着 + `E2B_TREES_SHARED=0` ⇒ 必须走导出/导入）；`test_a_failed_transfer_leaves_neither_a_half_tree_nor_a_record`；`test_a_migration_from_an_unreachable_source_is_refused_by_name`；`test_the_orphan_sweep_still_sees_a_local_tree`。
- [x] **Step 2: 先红**。
- [x] **Step 3: 实现**：换判据；迁移经控制面/共享的 `_migrate` 中转（复用既有 tar 代码，落点参数化）；上面第 2、3 条的上限与具名拒绝；目标节点导入失败不留半棵树；淘汰策略按 Task 1 的容量账落地。
- [x] **Step 4: 裁定进文档**："沙箱不是持久对象、**持久面是快照**"（否则本地化必须给节点盘做冗余）；排水顺序"先迁走、再下线"；以及"记录指向的节点上没有树"这个状态的具名修复动作。
- [x] **Step 5: 集群验收**：`MULTI-NODE`/`DEPLOYMENT` 冒烟；**两个方向各一次** —— ① 节点健在时跨节点迁移**保文件**；② 停掉源节点 worker 后发起的迁移**具名拒绝**而不是静默建空树；`GET /sandboxes` 无残留、两节点磁盘占用可解释。
- [x] **Step 6: 提交**。

### Task 4: 本节点 state 分家（拿回 `prepare` 的 ~73 ms）

**Files:** Modify `gateway_common/paths.py`、三个 config、`deploy/k8s/*.yaml`、`envd_service/agent.py`、`control_plane/file_ops.py`；Test `tests/unit/test_node_state_split.py`（新）。

**Interfaces:** Produces: `E2B_NODE_STATE_BASE`（节点本地）持有 `.creating`、disk-stats、`.route-b/**`、uid 池本地件；`E2B_STATE_BASE`（共享）继续持有 `_runtime/<id>/sandbox.json` 与 `.checkpoints/**`。且 `uid_pool._recorded_uids` 的索引从"枚举**树目录名**"改成"枚举**共享记录目录**"（Review Focus 3）——否则树本地化之后它连自己的节点都数不全。

- [x] **Step 1: 写失败用例**：`test_the_create_marker_lives_on_the_node_local_base`；`test_the_record_stays_on_the_shared_base`；`test_the_uid_ledger_sees_every_nodes_records_from_the_shared_index`（这条是 Review Focus 3 的钉子，必须先红）。
- [x] **Step 2: 先红** → **实现** → **绿**。
- [x] **Step 3: 量**：建箱 p50 与 `prepare` 段（期望 `prepare` 73 → ~10，长杆换回 `materialize`，而后者已经本地化）。
- [x] **Step 4: 提交**。

### Task 5: `open_dir_chain` 修复（与存储正交，可随时插）

**Files:** Modify `c3_agent/materialize.py`；Test `tests/unit/test_agent_materialize.py`。

- [x] **Step 1: 失败用例**：`test_the_dir_chain_does_not_walk_from_the_root_every_time`（断言对同一棵树的开销与"从 `/` 逐段打开"不同 —— 具体判据由实现定：缓存父 fd 或从已知根起走）。
- [x] **Step 2: 先红 → 实现 → 绿**；量：NAS 上每棵树两次 ≈ 17.4 ms。
- [x] **Step 3: 提交。** 只在树仍走共享的形状（开关关着、`local://`）上有收益 —— 它是 Task 3 的**补救**而不是替代。

### Task 6: 上线与文档

**Files:** Modify `deploy/stack/.version`、`docs/deploy-clusters.md`（§7.29）、`docs/open-issues.md`（N57）、`README.md`。

- [ ] **Step 1: 重建上线**（`screen` 里跑 `build-and-push.sh`；`DRY_RUN | kubectl diff` 只应有镜像 tag）。
- [ ] **Step 2: 量三件事并分形状报**：plain 建箱 p50（目标 ~60 ms）、**快照建箱每条目/每字节**（目标：2000 文件 52 s → 亚秒）、迁移保文件。
- [ ] **Step 3: 冒烟与残留**：`MULTI-NODE`/`DEPLOYMENT`、`GET /sandboxes` = `[]`、两节点无残树、pod 全 Running、`DRY_RUN` diff 0 行。
- [ ] **Step 4: 写 §7.29 / N57 / README**，把每条判据与读数对上，并写明**两条硬约束**（容量、页缓存）各自的上限配置。
- [ ] **Step 5: 提交**。

---

## Self-Review

**覆盖**：裁定 1 → Task 4（`command-logs` 留在原处即可，因为它的远程形态本来就是代理）；裁定 2 → Task 3（`_migrate` 上浮到共享根是它的前置，在 Task 0）；裁定 3 → Task 4（记录不搬）；裁定 4 → 全程，**出口是 Task 0 的根表**（介质归属显式化，不再从一根推），Task 2/3/4 各自只把"有已知跨节点消费者"的对象落共享；裁定 5 → Task 2。盘点里"不变"的那批 → 本计划不动它们（写进 Task 1 的文档）。

**依赖与顺序**：Task 0 是一切的前置（目录与判据先定型，默认行为不变）；Task 1 的三笔账（复核吞吐 / 容量 / 页缓存）与 Task 0 并行；Task 2 独立且收益最大，可在 Task 0 之后先上；Task 3 依赖 Task 0 的判据 + Task 2 的 tar 通道 + Task 1 的容量账（淘汰上限）；Task 4 什么都不依赖，但与 Task 3 **一起上线才拿得到建箱那 60 ms**（`max(materialize, prepare)`）；Task 5 与全部并行，只在"树仍走共享"的形状（Task 0 之后、Task 3 之前）有收益；Task 6 最后。

**风险最高的五处**：Task 0 Step 6（合并 `_snapshots` 时同名文件被静默取一个 —— 那是**数据丢失**，不是格式问题）、Task 2 Step 4（解包的硬化与完整性）、Task 3 Step 3 的判据与在途上限（翻错判据 = 静默建空树 + 源节点留一份永不回收的副本；在途是整棵树进控制面 2 GiB 内存）、Task 3 的"源节点不可达"（本地化后的头号失效面，靠排水顺序而不是代码兜底）、Task 4 Step 1 的第三条（uid 索引改口径）。五处都先红后绿。

**刻意不做**（记在文档里）：卷数据、`_volumes/_meta`、`_templates`、`_builds`、`_oci.tar` 一律不动；`_cow` 保留名不删；checkpoint 本轮仍留共享（它的"本地化"取决于是否禁止迁移 paused 沙箱，那条要单独立项）；**不做**"树本地 + 跨节点冗余"（节点掉线丢树这件事由 Task 3 Step 4 的裁定承担，不靠代码兜底）。
