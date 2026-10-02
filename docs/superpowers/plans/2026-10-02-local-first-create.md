# 建箱存储本地优先（local-first）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按"**默认本地写、只有需要共享时才落共享**"改造建箱与快照的存储布局，把建箱从 **127 ms** 拉向 **~60 ms**、把快照从 **26 ms/条目** 拉向**按字节付费**。

**Architecture:** 三个对象换位置 —— ① 快照落成 **tar** 放共享（恢复任何节点都可做，不需要 p2p）；② 沙箱树搬**节点本地**，跨节点（迁移/fork）用既有 tar 通道经共享中转；③ `.creating`/disk-stats/route-B/uid 池本地件搬到**节点本地的 state**。共享侧只保留：运行时记录 `sandbox.json`、checkpoint、快照 tar 与记录、卷与 `_meta`、`_templates`/`_builds`/`_oci.tar`。

**Tech Stack:** Python（`control_plane/`、`c3_agent/`、`envd_service/`、`gateway_common/`）、k8s（k0s、arm64）、NFS（阿里云 NAS）+ 节点 ESSD、tar。

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
- **不变（已逐条查实）**：卷数据与其切片（调度器按 `volume_node_id` 把沙箱钉在卷所属节点）、`_volumes/_meta`、`_templates`、`_builds`（都是**控制面副本间**的共享，worker 从不读）、`_oci/*.oci.tar`（唯一的真·节点间需求，且已是"共享放分发产物"的正确形状）。`_cow` 是空的保留名，保留名单不删。
- **硬约束（实测）**：节点盘是阿里云 ESSD（**写 186 / 冷读 125 MB/s**，比 NAS 的 381 / 165 慢），每节点只剩 **75 G**；容器内存 **512 MiB**（agent 的 maint **256 MiB**），页缓存计入 cgroup —— 测 500 MB 本地写已经 OOM 过一次。`agent ↔ agent` 被 NetworkPolicy 挡住（只有 CP→agent）。
- 断言精确匹配、禁 SKIP/xfail；编辑一律 `apply_patch`；每条"能失败"的钉子必须证明它会红。

## Review Focus

1. **tar 解包是路径逃逸的现场**：`..`、绝对路径链接、半棵树 —— §4.3.1 四条硬要求在解包路径上**逐条重新钉**，不能靠"只是换了个容器"。→ Task 2
2. **按需促升不能丢语义**：树本地之后，迁移/fork/快照/孤儿回收/磁盘扫描必须都能拿到数据；"取不到"要具名失败而不是静默降级。→ Task 3
3. **共享记录的全舰队口径**：`sandbox.json` 留共享之后，`uid_pool._recorded_uids`（靠枚举**树目录名**当索引）会失去视野 —— 索引要改成共享的记录目录，不许降级 fallback。→ Task 4
4. **快照完整性**：单 tar 意味着"写坏一半"看起来像个完整文件；沿用临时名 + fsync + rename + `.complete`。→ Task 2
5. **本地盘的容量与页缓存**：树进 75 G 的节点盘、大文件写入更慢（186 vs 381）、页缓存计入 512 MiB。→ Task 1（前置测量）+ Task 3 的淘汰策略

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

---

### Task 1: 前置测量与设计文档（**先做，它决定后面两处形状**）

**Files:** Create `docs/create-local-first-design.md`；Modify `deploy/scripts/acceptance/`（只在需要新探针时）。

- [ ] **Step 1: 三笔账**（集群实测，n≥10，写进设计文档）：① **沙箱里写大文件**：1 GB 顺序写在"树在本地"与"树在 NAS"两种形状下各多少 MB/s、多少秒（这决定树本地是不是对所有负载都划算）；② **容量账**：快照仓/树的日增量与保留窗口 → 每节点需要多少 G（当前 75 G）；③ **页缓存账**：一次建箱/快照的峰值页缓存 vs 512 MiB（agent 256 MiB）限额，给出上限配置（`E2B_IMAGE_*` 那种上限的同款做法）。
- [ ] **Step 2: `_snapshots` 是两个命名空间的裁定**：实测 CP 用导出根（`<export>/_snapshots/<id>/snapshot.json`）、agent 用 `E2B_WORKSPACE_BASE`（`<workspaces>/_snapshots/<id>/fs`），而 `registry/snapshots.py:340-361` 的注释说它们是同一对象同一处。二选一并写死：**要么修成一处**，要么在文档与代码注释里明确"两个命名空间"并各自说明读者。**这一条不解决，Task 2 的路径推导就会建在流沙上。**
- [ ] **Step 3: 落文档**：把两份 tmp 报告（测量 + 盘点）的核心表与五条裁定搬进 `docs/create-local-first-design.md`，并指向 `tmp/` 里的原始读数。
- [ ] **Step 4: 提交**（`docs(...)`）。

### Task 2: 快照打成 tar 落共享（收益最大、与树无关）

**Files:** Modify `envd_service/agent.py`（快照生成）、`c3_agent/materialize.py` + `control_plane/file_ops.py`（`copy_from` 形状与解包）、`deploy/scripts/acceptance/snapshot_create_probe.py`；Test `tests/unit/test_snapshot_tar.py`（新）、`tests/unit/test_agent_materialize.py`（改）。

**Interfaces:** Produces: 快照载荷为 `<...>/_snapshots/<id>/fs.tar`（+ 既有 `.complete`）；`derive_materialize` 的 `copy_from` 指向该 tar；`materialize_tree` 新增"取 tar + 就地解包进树根"的路径。

- [ ] **Step 1: 先量基线**：跑 `snapshot_create_probe.py` 的 1/40/202 三档，把今天的 `26 ms/条目` 复现并记下（这是前后对照的基准）。
- [ ] **Step 2: 写失败用例**：`test_a_snapshot_is_one_tar`（生成后目录里只有一个 `fs.tar` + `.complete`，没有爆炸式 `fs/`）；`test_a_restore_unpacks_the_tar_at_the_tree_root`（生产形状 `fs/workspace/kept.txt` ⇒ 树根下 `workspace/kept.txt`）；**§4.3.1 四条**在解包路径上各一条：`test_a_symlink_in_the_tar_is_recreated_not_followed`、`test_a_destination_symlink_segment_is_refused_named`、`test_a_partial_unpack_is_reported_as_failure`、`test_a_migrated_tree_keeps_its_files`；`test_a_truncated_tar_is_refused`（完整性）；`test_an_absolute_link_member_is_dropped`（对齐既有 `_extract_sandbox_archive` 的成员过滤）。
- [ ] **Step 3: 先红**。
- [ ] **Step 4: 实现**：快照侧 `tarfile` 写到**临时名** → `fsync` → `rename` → 最后写 `.complete`（与 `.oci.tar` 同款纪律）；恢复侧在 agent 里解包（**复用** `_extract_sandbox_archive` 的成员过滤与 `dest` 包含检查，不许自己再写一份）。
- [ ] **Step 5: 后量对照**：同一三档再跑一遍，把"按条目"换成"按字节"的口径一起报。
- [ ] **Step 6: 提交**。

### Task 3: 沙箱树本地化 + 迁移经共享中转

**Files:** Modify `deploy/k8s/{worker,c3-agent}.yaml`（节点本地卷 + 开关）、`control_plane/api/sandboxes.py`（`_export/_import_sandbox_archive` 的中转落点）、`envd_service/agent.py`；Test `tests/unit/test_tree_local_migration.py`（新）。

**Interfaces:** Consumes: Task 2 的 tar 通道；`E2B_SHARED_WORKSPACE_ROOT` 这个既有开关（今天关着，`if not shared:` 两个分支就是完整实现）。

- [ ] **Step 1: 写失败用例**：`test_a_local_tree_is_not_visible_from_the_shared_volume`；`test_migration_moves_the_tree_through_the_shared_store`（源节点导出 tar → 共享暂存 → 目标节点导入，断言目标节点树的内容与源一致）；`test_a_failed_transfer_leaves_neither_a_half_tree_nor_a_record`；`test_the_orphan_sweep_still_sees_a_local_tree`。
- [ ] **Step 2: 先红**。
- [ ] **Step 3: 实现**：翻开关 + 迁移/fork 照裁定 2 经共享中转（复用既有 tar 代码，落点参数化）；淘汰策略按 Task 1 的容量账落地（至少要有上限与具名拒绝）。
- [ ] **Step 4: 集群验收**：`MULTI-NODE`/`DEPLOYMENT` 冒烟（**必含跨节点迁移保文件**）、`GET /sandboxes` 无残留、两节点磁盘占用可解释。
- [ ] **Step 5: 提交**。

### Task 4: 本节点 state 分家（拿回 `prepare` 的 ~73 ms）

**Files:** Modify `gateway_common/paths.py`、三个 config、`deploy/k8s/*.yaml`、`envd_service/agent.py`、`control_plane/file_ops.py`；Test `tests/unit/test_node_state_split.py`（新）。

**Interfaces:** Produces: `E2B_NODE_STATE_BASE`（节点本地）持有 `.creating`、disk-stats、`.route-b/**`、uid 池本地件；`E2B_STATE_BASE`（共享）继续持有 `_runtime/<id>/sandbox.json` 与 `.checkpoints/**`。且 `uid_pool._recorded_uids` 的索引从"枚举**树目录名**"改成"枚举**共享记录目录**"（Review Focus 3）——否则树本地化之后它连自己的节点都数不全。

- [ ] **Step 1: 写失败用例**：`test_the_create_marker_lives_on_the_node_local_base`；`test_the_record_stays_on_the_shared_base`；`test_the_uid_ledger_sees_every_nodes_records_from_the_shared_index`（这条是 Review Focus 3 的钉子，必须先红）。
- [ ] **Step 2: 先红** → **实现** → **绿**。
- [ ] **Step 3: 量**：建箱 p50 与 `prepare` 段（期望 `prepare` 73 → ~10，长杆换回 `materialize`，而后者已经本地化）。
- [ ] **Step 4: 提交**。

### Task 5: `open_dir_chain` 修复（与存储正交，可随时插）

**Files:** Modify `c3_agent/materialize.py`；Test `tests/unit/test_agent_materialize.py`。

- [ ] **Step 1: 失败用例**：`test_the_dir_chain_does_not_walk_from_the_root_every_time`（断言对同一棵树的开销与"从 `/` 逐段打开"不同 —— 具体判据由实现定：缓存父 fd 或从已知根起走）。
- [ ] **Step 2: 先红 → 实现 → 绿**；量：NAS 上每棵树两次 ≈ 17.4 ms。
- [ ] **Step 3: 提交。** 只在树仍走共享的形状（开关关着、`local://`）上有收益 —— 它是 Task 3 的**补救**而不是替代。

### Task 6: 上线与文档

**Files:** Modify `deploy/stack/.version`、`docs/deploy-clusters.md`（§7.29）、`docs/open-issues.md`（N57）、`README.md`。

- [ ] **Step 1: 重建上线**（`screen` 里跑 `build-and-push.sh`；`DRY_RUN | kubectl diff` 只应有镜像 tag）。
- [ ] **Step 2: 量三件事并分形状报**：plain 建箱 p50（目标 ~60 ms）、**快照建箱每条目/每字节**（目标：2000 文件 52 s → 亚秒）、迁移保文件。
- [ ] **Step 3: 冒烟与残留**：`MULTI-NODE`/`DEPLOYMENT`、`GET /sandboxes` = `[]`、两节点无残树、pod 全 Running、`DRY_RUN` diff 0 行。
- [ ] **Step 4: 写 §7.29 / N57 / README**，把每条判据与读数对上，并写明**两条硬约束**（容量、页缓存）各自的上限配置。
- [ ] **Step 5: 提交**。

---

## Self-Review

**覆盖**：裁定 1 → Task 4（`command-logs` 留在原处即可，因为它的远程形态本来就是代理）；裁定 2 → Task 3；裁定 3 → Task 4（记录不搬）；裁定 4 → 全程（Task 2/3/4 各自只把"有已知跨节点消费者"的对象落共享）；裁定 5 → Task 2。盘点里"不变"的那批 → 本计划不动它们（写进 Task 1 的文档）。

**依赖与顺序**：Task 1 是所有事情的前置（三笔账 + `_snapshots` 裁定）；Task 2 独立且收益最大，可先上；Task 3 依赖 Task 2 的 tar 通道；Task 4 依赖 Task 3（树本地之后 `prepare` 才是长杆）；Task 5 与全部并行；Task 6 最后。

**风险最高的三处**：Task 2 Step 4（解包的硬化与完整性）、Task 3 Step 3（促升/按需取的失败面）、Task 4 Step 1 的第三条（uid 索引改口径）。三处都先红后绿，Review Focus 各占一条。

**刻意不做**（记在文档里）：卷数据、`_volumes/_meta`、`_templates`、`_builds`、`_oci.tar` 一律不动；`_cow` 保留名不删；checkpoint 本轮仍留共享（它的"本地化"取决于是否禁止迁移 paused 沙箱，那条要单独立项）。
