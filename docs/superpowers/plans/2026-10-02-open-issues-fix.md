# 未完成问题修复批（2026-10-02）：N57 / N60 / N61 / N62 / N63

**来源**：`docs/open-issues.md` 的五条未完成项，逐条读完代码后给出的修复计划；控制者与用户已确认的裁定：

1. **N60**：动调度策略（逐候选重试 + 具名 WARNING）。
2. **N57**：走 A（把池自己的锁与预约标记挪回共享 state base），不走"worker→agent→控制面台账"。
3. **N63**：接受"先测后定、默认 1.5 M 可调"的取值政策。
4. **N62**：本轮做（它已经产出过"两个副本列表不一致"的可见错误）。

**批次目标**：把上述五条从"登记"推进到"已修 + 已钉 + 已上线"，并让 N61 从"观测"推进到"定因 + 修"。

**非目标（本批不做，仍留在 open-issues）**：

- N14 的"退役模拟"那半（代码卫生，不欠防线）。
- `path_surface.rs` 的 Open 桶 7 条（设计内待决）。
- 入口代理 / O2 / OBS-6（带触发或需人类动作）。

## Global Constraints

- **先红后绿**：每条"能失败"的钉子必须先证明它会红，把 RED 的**原始输出**贴进报告。整支评审会查这一条。
- **断言精确匹配**：禁用 `toContain` / `includes` / `assertContains` 之类的部分匹配；日期、数字、字符串一律全等比较。日志断言用 `record.getMessage()` 与逐字期望串**相等**，不许 `in`。
- **禁止** SKIP / xfail / 过滤错误输出。
- **临时文件**放**项目内** `tmp/`，不用系统 `/tmp`、`$TMPDIR`。
- **编辑一律 `apply_patch`**；最小改动，保持既有风格。
- **不动已部署行为之外的东西**：本批不碰 `E2B_WORKSPACE_BASE`、不删数据、不需要停机窗口（§7.29 那套流程不适用）。
- **写集隔离**：每个任务只许改它自己列出的文件。**并行**执行（同一 worktree），撞 `.git/index.lock` 时等 2 s 重试，**不要删锁文件**。
- **提交**：一个任务 1–2 个提交，中文提交信息照既有风格（`fix(...)` / `feat(...)` / `docs(...)`）。
- Python 3.12、`pytest`；单测入口 `.venv/bin/python -m pytest tests/unit/...`。
- 报告文件：`.superpowers/sdd/2026-10-02-open-issues-fix/task-N-report.md`（brief 里给绝对路径）。

## Review Focus（整支评审会逐条查）

1. **N57 的隔离墙**：修完之后"两个节点各自 `acquire` 拿到同一个 uid"必须**不可能**；控制档（共用 node_state_base）不得回归。
2. **N60 不许把 503 换成"静默放到漂移节点"**：每次跳过都要有具名 WARNING；`reserve_node`（点名目标节点）不得重试。
3. **N62 的两半都要**：删掉的记录在另一副本不得再出现；新建的记录在另一副本必须看得见；缓存不得变成"记录已从盘上消失但仍被服务"。
4. **N63 是读侧护栏**：成员数上限必须落在解包路径上并具名拒绝；上限取值必须有实测依据（索引内存），不是拍的。
5. **N61 只做"可见 + 不阻塞"**：本批不猜根因、不改 TTL 语义；但埋点必须能在下一次复现时一眼看出哪一步没走。
6. **不许把既有语义改掉**：字节上限、`_guard_member` 的路径逃逸防线、孤儿/暂停豁免、配额释放的唯一释放点，全部原样。

---

## 文件结构

| 文件 | 责任 | 任务 |
|---|---|---|
| `envd_service/uid_pool.py`（改） | 池自己的锁与预约标记回到共享 state base | 1 |
| `tests/unit/test_uid_pool.py`（改） | 两条钉子 + 既有用例不回归 | 1 |
| `control_plane/scheduler.py`（改） | 新增 `rank_candidates()`，`pick_best()` 行为逐字不变 | 2 |
| `control_plane/registry/nodes.py`（改） | `select_and_reserve` 逐候选重试 + 具名 WARNING | 2 |
| `tests/unit/test_placement_candidate_fallback.py`（新） | N60 的先红钉子 | 2 |
| `control_plane/registry/snapshots.py`（改） | `list()` 走共享目录、`get()` 校验记录文件 | 3 |
| `tests/unit/test_snapshot_record_visibility.py`（新） | 跨副本可见性钉子（两半） | 3 |
| `gateway_common/archive.py`（改） | 解包侧成员数上限 + 具名拒绝 + env 开关 | 4 |
| `tests/unit/test_snapshot_tar.py`（改） | 成员数上限的先红钉子 | 4 |
| `deploy/scripts/acceptance/archive_member_index_memory.py`（新） | 索引内存实测（取值依据） | 4 |
| `control_plane/registry/ttl.py`（改） | 埋点：每轮候选数、claim 饥饿看门狗、不阻塞事件循环 | 5 |
| `control_plane/app.py`（改） | claim TTL 改为不短于轮次预算 + 传参 | 5 |
| `tests/unit/test_ttl_sweeper_stall.py`（新） | 不阻塞 + 饥饿 WARNING 的钉子 | 5 |
| `deploy/scripts/acceptance/probe_ttl_sweep_reap.py`（新） | 只读定因探针 | 5 |
| `docs/open-issues.md`、`docs/deploy-clusters.md`、`docs/env-vars.md`（改） | 收尾登记 | 6 |

---

## Task 1: N57 —— uid 池回落分配器的跨节点互斥

**根因（已实证）**：Task 4 把池自己的 `.uid_pool.lock` 与 `.uid_reservations/` 挪到**节点本地** `node_state_base`，而 `acquire`（回落分配器）的临界区"读共享记录索引 → 挑空闲 uid → 写预约标记"因此只在**同一节点内**串行。两个节点可以同时（甚至先后）挑中同一个 uid —— 共享对象（卷切片、`_snapshots` 载荷、镜像缓存 secrets）互相可读，E3.2 的隔离墙没了。

**先红证据（控制者已跑，2026-10-02；探针原在 tmp/ 下，Task 6a 已提升为仓库路径）**：`deploy/scripts/acceptance/uid_pool_cross_node_probe.py`

```
pool_a -> 10000
pool_b -> 10000        ← COLLISION
reservation markers: node-0-state/.uid_reservations/sbx_a, node-1-state/.uid_reservations/sbx_b
control: 10000 / 10001 -> no collision   ← 控制档（共用 node_state_base）
```

**改法（已裁定为 A）**：池自己的文件回到**共享** state base。四处：

- `UidPool.lock_path` ⇒ 用 `self._state_base`
- `UidPool._marker_path()` ⇒ 用 `self._state_base`
- `UidPool._clear_reservations()` ⇒ 用 `self._state_base`
- `UidPool._reserved_uids()` ⇒ 用 `self._state_base`

并改掉 `__init__` 里那段把 `_local_base` 说成"不需要跨节点可见"的注释（那句话就是这条 bug 的书面版本）：新注释要写明"`acquire` 的临界区必须跨节点互斥；`claim`（出厂常态路径）不碰这两个文件，所以把它们放回共享盘不付 `prepare` 的代价"。若 `_local_base` 与 `resolve_node_state_base` 因此成为死代码，一并删掉；构造参数 `node_state_base` **保留**（调用点兼容），docstring 说明它不再影响池自己的文件。

**钉子（`tests/unit/test_uid_pool.py`）**：

1. `test_two_nodes_with_distinct_node_state_base_do_not_share_a_uid`：两个 `UidPool`，**同一个** `state_base`、**不同** `node_state_base`；顺序 `acquire("sbx_a")` / `acquire("sbx_b")` ⇒ 断言 `(POOL_START, POOL_START + 1)`（先红：今天两条都是 `POOL_START`）。
2. `test_the_pool_lock_and_markers_live_on_the_shared_state_base`：断言 `pool.lock_path == state_base / ".uid_pool.lock"`，且 `acquire` 之后预约标记出现在 `state_base / ".uid_reservations"` 下（先红）。
3. 既有 `test_acquire_across_pool_instances_does_not_collide` 与 `test_acquire_concurrent_across_pool_instances_no_collision` 必须继续绿（控制档）。

**验收**：`.venv/bin/python -m pytest tests/unit/test_uid_pool.py -q` 全绿；`tests/unit` 全档只允许基线那 3 条 macOS-only 红。

**写集**：`envd_service/uid_pool.py`、`tests/unit/test_uid_pool.py`（仅此两个文件）。

---

## Task 2: N60 —— 一个节点满不再让整支舰队 503

**根因**：`select_and_reserve` 只试 `pick_best` 选中的那一个候选；`_quota_store.reserve` 返回 False 就直接 `return None`，调用方折成 `503 No resources available`，哪怕另一个候选 `can_fit`。

**改法**：

- `control_plane/scheduler.py`：新增 `rank_candidates(candidates, *, base_image, volume_node_id, memory_mb, cpu_percent, disk_mb, processes) -> list[NodeRecord]`，返回**按现有打分排序**的候选列表（卷钉住的节点排最前，其余按 `(_image_affinity, _remaining_ratio, -labels)` 降序）；`pick_best` 改成 `rank_candidates(...)` 取第一个（**行为逐字不变**，`select_node` 不受影响）。
- `control_plane/registry/nodes.py::select_and_reserve`：在 `self._lock` 下取 `ranked = rank_candidates(candidates, ...)`，按顺序逐个 `_quota_store.reserve`；被拒时打**具名 WARNING** 并继续下一个；**候选耗尽**才 `return None`。内存侧 `node.reserve(...)` + `self._persist_locked(node)` 仍然只在最后一个成功者身上发生（不许对失败的候选做任何内存 reservation）。
- `reserve_node`（调用方点名目标节点）**不变**：不重试、不换候选。

WARNING 的逐字文本（钉子按这个串全等断言，两处占位按实际填）：

```
quota store refused node %s for memory=%s cpu=%s disk=%s processes=%s; trying the next candidate (%s left)
```

最后候选都被拒时，除上述逐条 WARNING 外再打一条点名"候选已耗尽"的 WARNING：

```
quota store refused every candidate for memory=%s cpu=%s disk=%s processes=%s; this placement answers 503
```

**钉子（新文件 `tests/unit/test_placement_candidate_fallback.py`）**：

1. `test_a_refused_candidate_hands_the_placement_to_the_next_one`：两个健康节点都能 `can_fit`；注入一个只在 `node_a` 上 `reserve` 返回 False 的 Duck-typed store（同时实现 `get`/`reconcile`/`release` 的 no-op）⇒ 断言返回的是 `node_b`（先红：今天返回 `None`）。
2. `test_the_skip_names_the_node_and_the_dimensions`：`caplog` 里存在与上面模板逐字相等的 WARNING（用 `record.getMessage()` 全等比较），且 `node.reserved_memory_mb` 在 `node_a` 上**未变**。
3. `test_every_candidate_refused_still_answers_with_capacity_left_elsewhere`：所有候选都被拒 ⇒ 返回 `None`，且"候选已耗尽"那条 WARNING 在。
4. `test_reserve_node_does_not_retry_a_named_target`：`reserve_node("node_a", ...)` 在 store 拒绝时仍是 `None`，且**没有** WARNING 说去试别的候选。

**验收**：`.venv/bin/python -m pytest tests/unit/test_placement_candidate_fallback.py tests/unit/test_node_registry.py tests/unit/test_node_quota_reconcile.py tests/unit/test_redis_multireplica.py -q` 全绿。

**写集**：`control_plane/scheduler.py`、`control_plane/registry/nodes.py`、`tests/unit/test_placement_candidate_fallback.py`。

---

## Task 3: N62 —— 快照记录的跨副本可见性

**根因**：记录文件在共享卷（`<export>/_snapshots/<id>/snapshot.json`），但 `SnapshotRegistry.list()` **只读本进程内存字典**，`get()` 对已完成记录**直接吃缓存**，`delete()` 只 `pop` 本副本 ⇒ 被删的记录在另一副本永久可见（实测一个副本 15 条、另一个 12 条，成员不同）。

**改法**：

- `list()`：改成从共享目录构建（`sorted(self._snapshots_root.glob("*/snapshot.json"))` → `self.get(id)`，`UnknownSnapshotError` 跳过），再套用现有的 tenant/name/limit 过滤与排序；内存字典降级为"暖启动缓存"，不再是 `list()` 的真相来源。
- `get()`：命中缓存且 `status != "creating"` 时，**先** `self._record_path(snapshot_id)[0].is_file()` 确认记录还在；不在就丢缓存并抛 `UnknownSnapshotError`。未命中或 `creating` 的路径保持现状（读盘 + 回填缓存）。
- `delete()` 保持"先 `get()` 再 `rmtree` 再 `pop`"，但因为它现在会校验文件，语义自然变成"删了就两边都看不见"。

**钉子（新文件 `tests/unit/test_snapshot_record_visibility.py`）**：

1. `test_a_record_deleted_by_one_replica_disappears_from_the_other`：两个 `SnapshotRegistry` 指向**同一个** `base_dir`；A 建一条记录（用现有 helper 直接写 `snapshot.json` + `fs/` 的形状）、两边都 `list()` 到它；B `delete(id)` ⇒ 断言 A 的 `list()` **不含**该 id，且 A `get(id)` 抛 `UnknownSnapshotError`（先红：今天 A 仍列出它）。
2. `test_a_record_written_by_another_replica_is_visible_to_a_warm_cache`：A 先 `list()`（把缓存暖起来），B 再写一条新记录 ⇒ A 的 `list()` 必须包含新 id（先红）。
3. `test_a_record_whose_payload_vanished_is_not_served_from_cache`：A 缓存了记录，直接把记录文件删掉（模拟另一副本的 rmtree）⇒ A `get()` 抛 `UnknownSnapshotError`（先红）。
4. `test_listing_keeps_the_existing_filters`：tenant / `name` / `limit` 过滤在新实现下逐字不变（既有 `test_snapshot_registry.py` 之类不许回归）。

**验收**：`.venv/bin/python -m pytest tests/unit -k "snapshot" -q` 全绿 + 上面新文件全绿。

**写集**：`control_plane/registry/snapshots.py`、`tests/unit/test_snapshot_record_visibility.py`。

---

## Task 4: N63 —— tar 成员数上限（读侧护栏）

**根因**：解包时**成员数据是流式的**，但 CPython 的 `TarFile.next()` 无条件 `members.append` ⇒ 成员**索引**不流式（实测 ~430 B/成员）。2 M 个极小成员 ≈ 0.9 GB 索引，而 `maint` 的 limit 是 2 GiB（`deploy/k8s/c3-agent.yaml` face B）。字节上限 1.25 GiB（`E2B_TREE_COPY_MAX_BYTES`）**bound 但不消除**它：2 M 个空成员 ≈ 1 GiB 的 512 B 头，正好在字节上限内。

**改法**：

- `gateway_common/archive.py`：照现有 reason 常量风格新增 `TOO_MANY_MEMBERS = "archive-too-many-members"`（放在 `PARTIAL_UNPACK` 旁边，带一行 `#:` 说明），以及 `DEFAULT_ARCHIVE_MAX_MEMBERS = 1_500_000` 与 `resolve_member_max()`（读 `E2B_ARCHIVE_MAX_MEMBERS`，空/非法回落默认值，`0` = 关闭上限）。
- `extract_sandbox_archive(archive_path, dest, *, max_members: int | None = None)`：`None` 时用 `resolve_member_max()`；在遍历成员的循环里计数，**超过上限的那一个成员在被写出之前**抛 `ArchiveRefusal(TOO_MANY_MEMBERS, detail)`（detail 里带上限、已见成员数、归档路径；不要再加子类，保持既有"一个 refusal + 一个 reason"的形状）。
- 读侧默认生效 ⇒ 三处解包（快照恢复、迁移导入、materialize）**零调用点改动**就受保护；`0` 关闭上限的口子留着（与字节上限同款纪律）。
- 模块 docstring 里那句"member-count cap belongs beside Task 3's byte cap（→ N63）"改成"已经在这里，取值依据见下"，并把实测数字写进去。

**取值依据（必须实测，不许拍）**：`deploy/scripts/acceptance/archive_member_index_memory.py` —— 生成 N 个空成员的 tar（N ∈ {200_000, 1_500_000}），用 `tracemalloc` + `resource.getrusage(RUSAGE_SELF).ru_maxrss` 量解包峰值，输出 `N / 索引字节数 / 每成员字节 / 峰值 RSS`。报告里给出实测表与"为什么默认取 1.5 M 能塞进 2 GiB"的算术；若实测显示 1.5 M 下峰值会逼近 2 GiB，**不许自己改容器限额**，把结论写进报告并按 `DONE_WITH_CONCERNS` 上报（控制者裁定）。

**钉子（`tests/unit/test_snapshot_tar.py`）**：

1. `test_an_archive_over_the_member_cap_is_refused_by_name`：3 个成员的 tar + `max_members=2` ⇒ `pytest.raises(ArchiveRefusal)` 且 `exc.value.reason == "archive-too-many-members"`（先红：今天不抛）。
2. `test_a_well_formed_archive_at_the_cap_still_unpacks`：正好等于上限（`max_members=3`）⇒ 返回写出的成员数 `3`，文件内容逐字节对得上。
3. `test_the_cap_can_be_disabled`：`max_members=0` + 10 个成员的 tar ⇒ 全部解出。
4. `test_the_env_knob_is_read_and_zero_disables`：`monkeypatch.setenv("E2B_ARCHIVE_MAX_MEMBERS", "5")` ⇒ `resolve_member_max() == 5`；`"0"` ⇒ `0`；未设 ⇒ `DEFAULT_ARCHIVE_MAX_MEMBERS`（全等断言）。

**验收**：`.venv/bin/python -m pytest tests/unit/test_snapshot_tar.py tests/unit/test_tree_local_migration.py -q` 全绿；`.venv/bin/python deploy/scripts/acceptance/archive_member_index_memory.py` 跑出实测表（贴进报告）。

**写集**：`gateway_common/archive.py`、`tests/unit/test_snapshot_tar.py`、`deploy/scripts/acceptance/archive_member_index_memory.py`。

---

## Task 5: N61 —— TTL sweeper 的定因探针 + 埋点（本批不猜根因）

**现状**：8 条 `state: running`、`endAt` 早约 65 分钟的记录没被 1 s 一轮的 sweeper 回收；机制上 `_ttl_reapable` 对 `running` 是"到点就收"，与观测矛盾。控制者另外找到三个记录里没写的怀疑点：

- claim 的 TTL（`int(_TTL_SWEEP_INTERVAL_S)` = 1 s）**短于一轮的真实时长**（一次 rmtree 实测 17.1 s），且**没有"连续丢 claim"的看门狗**，starvation 不出声；
- `expired_candidates()`（Redis SCAN + NFS 读）与 `registry.cleanup_workspace()`（`shutil.rmtree`）都在**事件循环上同步跑** —— 隔壁的节点健康扫描已经改成 `asyncio.to_thread`（N32 的教训只修了一半）；
- `expired_candidates()` 抛异常就**整轮**跳过，只有一条 `TTL sweep failed`，看不出"哪一条记录、哪一步"。

**改法（只做可见性与不阻塞，不改 TTL 语义）**：

- `control_plane/registry/ttl.py`：
  - `expired = await asyncio.to_thread(registry.expired_candidates)`；
  - 候选非空时打 INFO：`TTL sweep: %d expired candidate(s): %s`（id 列表，超过 10 条截断成前 10 + `…(+N more)`）；
  - `cleanup_workspace` 用 `await asyncio.to_thread(registry.cleanup_workspace, record)`；
  - claim 连续失败计时：`self._starved_since` 首次失败的 monotonic 时刻；连续失败 ≥ 30 s 时打**一次**具名 WARNING（`TTL sweep starved: the fleet-wide claim %s has been held for %.1fs; no expired record can be reaped while this lasts`），claim 成功后清零并打一条 INFO 说明恢复。
- `control_plane/app.py`：claim 的 `ttl_s` 不再用 `int(_TTL_SWEEP_INTERVAL_S)`，改为模块常量 `_TTL_SWEEP_CLAIM_TTL_S = 60`（并写明理由：claim 的 TTL 是"一轮最多占用多久"的上界，1 s 会让第二轮副本在上一轮还在拆树时插进来）。
- **不改**：`_ttl_reapable` 的豁免语义、teardown 顺序（先拆后删记录）、配额释放路径。

**只读定因探针（新文件 `deploy/scripts/acceptance/probe_ttl_sweep_reap.py`）**：连 `E2B_REDIS_URL`，用**真实** `Settings` + `SandboxRegistry`（`record_store` 路径，只读），输出：

1. 逐条：`sandbox_id / state / end_at / now / is_expired / _ttl_reapable`（`expired_candidates()` 的逐条判定）；
2. `len(expired_candidates())` 与 `list()` 的集合差（有没有"列着但不可回收"的记录）；
3. 对 `e2b:ttl:sweep` 采样 60 次 × 1 s：`EXISTS` / `TTL`（看它是否被长期持有）；
4. 明写"只读，不做任何写操作"，且脚本在缺 `E2B_REDIS_URL` 时**具名拒绝**（退出码 2 + 一行说明），不许静默通过。

**钉子（新文件 `tests/unit/test_ttl_sweeper_stall.py`）**：

1. `test_a_slow_candidate_scan_does_not_block_the_event_loop`：假 registry 的 `expired_candidates` 阻塞 0.2 s（`time.sleep`）；同时跑一个 10 ms tick 的计数器任务，断言一轮结束前 tick ≥ 8（先红：今天 synchronous 调用下 tick 是 1）。
2. `test_a_starved_claim_is_named_after_the_grace_period`：`claim` 恒 False、注入可控时钟/短 grace（用构造参数 `starve_after_s=0.05`）⇒ 断言 `caplog` 里存在与上面模板**逐字相等**的 WARNING，且只出现一次。
3. `test_the_sweep_still_reaps_after_a_candidate_scan_failure`：第一轮 `expired_candidates` 抛异常 ⇒ 断言有一轮 WARNING + 第二轮正常回收（证明"一轮坏不拖死后面"）。
4. `test_cleanup_runs_off_the_loop`：假 registry 的 `cleanup_workspace` 阻塞 0.2 s，同 1 的 tick 断言（先红）。

**验收**：`.venv/bin/python -m pytest tests/unit/test_ttl_sweeper_stall.py tests/unit/test_ttl.py -q` 全绿。

**写集**：`control_plane/registry/ttl.py`、`control_plane/app.py`、`tests/unit/test_ttl_sweeper_stall.py`、`deploy/scripts/acceptance/probe_ttl_sweep_reap.py`。

---

## Task 6: 收尾（控制者执行）

- `docs/open-issues.md`：N57 / N60 / N62 / N63 改成"已修 + 提交号 + 上线版本"；N61 改成"埋点已落 + 探针脚本路径 + 待定因/待修"。
- `docs/deploy-clusters.md`：新增 §7.34 —— 本批的上线版本、四条读数（两条台账、`GET /sandboxes`、`kubectl diff` 0 行）与 N61 探针的结论（若有）。
- `docs/env-vars.md`：补 `E2B_ARCHIVE_MAX_MEMBERS`。
- 构建 + apply + 冒烟（`MULTI-NODE` / `DEPLOYMENT`）+ 收尾核验（三处镜像 tag 一致、9 pod Running、`DRY_RUN=1 apply.sh | kubectl diff` 0 行、工作树干净）。

## Self-Review

- 五个任务的写集两两不相交（已逐对核过：uid_pool / scheduler+nodes / snapshots / archive / ttl+app），可并行。
- Task 1 与 Task 2 都涉及"跨节点/跨副本"语义，但一个在 worker 侧、一个在控制面侧，互不依赖。
- Task 4 的默认值 1.5 M 有实测要求，未实测前不许上线（Task 6 之前必须有数字）。
- Task 5 明确"不猜根因"，因此它的验收里**没有**"8 条记录被回收"这一条 —— 那条属于批 B。
