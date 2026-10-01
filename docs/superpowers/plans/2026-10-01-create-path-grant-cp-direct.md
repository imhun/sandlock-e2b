# 建箱材料化：控制面直送（materialize instruction）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把建箱期的材料化（建树 / 快照拷贝 / 改属主 / 卷切片）改成"控制面在拨 worker 之前，往既有的 CP→agent 通道送**一条** `materialize` 指令，由 agent 一次做完"，并把已经落在 `main` 上的载体 B 残留（授权票据、面 B 新路由、worker 侧客户端）删干净。

**Architecture:** 三角分工不变（CP 决策 / agent 执行 / worker 跑沙箱），**不新增任何通道**：控制面本来就攥着这次建箱的全部输入（节点、host_uid、卷挂载），也本来就要拨 worker，它只是先拨 agent 一次。worker 得到一个"树已就绪"的标志，其余照旧。难写的部分（硬化递归拷贝、建箱标记、延迟落盘）从本轮成果里原样继承。

**Tech Stack:** Python 3.14（`control_plane/`、`c3_agent/`、`envd_service/`）、C（`c3_agent/priv/maint.c`，本计划不改它）、k8s（k0s，arm64）、NFS（阿里云 NAS）。

**Spec:** `docs/superpowers/specs/2026-10-01-create-path-grant-design-v2.md`（载体 C）。前作 `2026-10-01-create-path-grant-design.md`（载体 B）**已被取代**，其 §1 实测与 §4.3.1 四条硬要求由 v2 继承。

**本计划实施前 `main` 上已有的 8 个提交**（`6f4e35b..804f525`）**不是从零开始**：Task 1/2/4 在改造它们，Task 3 在删它们。执行前先读 `git log --oneline 6f4e35b..804f525`。

## Global Constraints

- **不新增 `worker → agent` 通道**，`docs/c3-privilege-relocation.md` §14.1 的链路表一个字不改；worker 仍然没有 `E2B_C3_AGENT_TOKEN`。
- 路径由**控制面**命名（§14.4 硬规则二允许且要求），agent 仍独立复核一次（realpath + 自己那四根），两道不互相替代。
- 快照载荷格式**不变**：`<ws>/_snapshots/<snap>/fs` 是**树根**的副本，落点必须是 `<root>`（v1 的落点是错的）。
- §4.3.1 四条硬要求：源侧不跟随符号链接（用 `symlink()` 重建）；目标侧逐段 `O_NOFOLLOW`，遇符号链接**具名拒绝**；半棵树不得报成功；跨节点迁移保文件不许丢。
- materialize **不排进** `E2B_C3_AGENT_MAX_CONCURRENCY`（默认 64）那个信号量；agent 侧有自己的上限，且**不得**把 anyio 线程池吃光（面 A 的 `grant-slot` 还在同一个池子里）。
- 回退是**一个 payload 字段**：不送 `materialized`，worker 就自己建树（今天的行为）。没有降级分支、没有异常类型。
- 拆箱/巡检（`rm`/`walk`/`remove-workspace`/`remove-runtime`）仍逐次经控制面，形状不变。
- `local://` 形态与 `_provision_local` **不动**。
- 只在**建箱**路径上使用这条指令。

## Review Focus

1. **快照落点**：`fs/` 是树根副本 ⇒ 快路与降级路必须产出**同一棵树**（`workspace/kept.txt` 在，不是 `workspace/workspace/kept.txt`）。→ Task 1
2. **两侧的符号链接**：源侧必须被重建为链接、目标树里预置的链接必须**具名拒绝**且不留半棵树。→ Task 2
3. **agent 饱和**：materialize 超上限时必须**具名**答复，且面 A 的 `grant-slot` 不被拖住。→ Task 2 / Task 6
4. **滚动升级两端**：新 CP + 旧 worker、旧 CP + 新 worker，建箱都必须成功。→ Task 4
5. **CP 侧窗口**：材料化完成、worker 尚未被拨到之间发 DELETE，不得留下"记录在、树没了"或"记录没了、树还在"。→ Task 5

---

## 文件结构

| 文件 | 责任 |
|---|---|
| `c3_agent/materialize.py`（改） | 硬化材料化实现；**本任务组只改拷贝落点**（`copy_tree(source, root)`） |
| `c3_agent/app.py`（改） | 既有 `/internal/nodes/{node}/agent/{op}` 上加 `materialize` verb：body 模型、worker 身份解析复用、自己的并发预算；**删掉** `/internal/grants/file-op` 与 jti 表 |
| `c3_agent/config.py`（改） | `materialize_max_concurrency`（`E2B_C3_AGENT_MATERIALIZE_MAX`，默认 4）、`materialize_busy_timeout_s`（默认 5.0） |
| `control_plane/c3_agent_client.py`（改） | `materialize(...)`：走既有 `_target(node_id)`（worker 键 → 宿主身份），**不进**共享信号量 |
| `control_plane/api/sandboxes.py`（改） | 建箱路径在 `_provision_remote` **之前**送一次指令；失败即走既有回滚；成功则 payload 带 `materialized` |
| `control_plane/api/internal.py`（改） | **删** `/internal/nodes/{node}/file-grant` |
| `control_plane/config.py`（改） | **删** `create_grant_ttl_s` |
| `gateway_common/create_grant.py`（删） | 载体 B 的票据；载体 C 不需要 |
| `envd_service/agent.py`（改） | `_agent_create_sandbox` 认 `materialized`：跳过建树/拷贝与属主段；**删** `_materialize_or_degrade` |
| `envd_service/agent_fileops.py`（改） | **删** `materialize()` / `_mint_create_grant` / `AgentMaterializeUnsupported` / `MATERIALIZE_OP` |
| `envd_service/volumes.py`（不动） | `slices_materialized` 已就位（`1e847a1`），由 worker 按标志传入 |

---

### Task 1: 快照落点修回树根

**Files:**
- Modify: `c3_agent/materialize.py`
- Test: `tests/unit/test_agent_materialize.py`（**新建**，取代 `tests/unit/test_agent_grant_route.py`；本任务先把该文件整体改名并把路由指向旧路由，Task 2 再把它指向新路由）

**Interfaces:**
- Produces: `materialize_tree(plan, *, settings, runner) -> dict`（签名不变）；`copy_tree(src, dst, *, dir_mode=0o770) -> int`（签名不变）。落点语义：`tree.copy_from` 合并进 **`tree.path`（树根）**。

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_agent_materialize.py`（沿用 `test_agent_grant_route.py` 的夹具：`_Agent` 造 app、`_grant` 造载荷、`_snapshot(agent, entries)` 造快照）：

`test_a_snapshot_lands_at_the_tree_root`：快照里放**生产形状**的 `{"workspace": {"kept.txt": "kept\n"}}`（即 `fs/workspace/kept.txt`），建箱后断言 `(tree / "workspace" / "kept.txt").read_text() == "kept\n"`，且 `(tree / "workspace" / "workspace").exists() is False`。

`test_the_fast_path_and_the_fallback_produce_the_same_tree`：同一份快照，一次走材料化、一次走 worker 的 `copytree` 降级路（用 `_Agent` + 一个 `materialize` 抛 `AgentMaterializeUnsupported` 的替身），断言两棵树的相对文件列表**逐项相等**。

- [ ] **Step 2: Run tests to verify they fail**

Run: `env -u all_proxy -u http_proxy -u https_proxy tmp/venv/bin/python -m pytest tests/unit/test_agent_materialize.py -q -k "snapshot_lands or same_tree"`
Expected: FAIL（`workspace/workspace/kept.txt`）

- [ ] **Step 3: Implement**

`c3_agent/materialize.py::materialize_tree`：把 `copy_tree(str(source), str(target), dir_mode=mode)` 改成 `copy_tree(str(source), str(root), dir_mode=mode)`，并加一行注释说明为什么（`fs/` 是树根副本；`subdir` 只用于"没有快照时建出 `<root>/workspace`"）。

- [ ] **Step 4: Run tests to verify they pass**

Run: 同 Step 2。Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add c3_agent/materialize.py tests/unit/test_agent_materialize.py
git rm tests/unit/test_agent_grant_route.py
git commit -m "fix(agent): 快照合并回树根 —— fs/ 是树根副本，不是 <root>/workspace 的副本"
```

---

### Task 2: agent 侧 `materialize` verb（并删掉面 B 的授权路由）

**Files:**
- Modify: `c3_agent/app.py`、`c3_agent/config.py`
- Test: `tests/unit/test_agent_materialize.py`（改指向新路由）、`tests/unit/test_c3_agent_fileops.py`（并发用例）

**Interfaces:**
- Consumes: `c3_agent.materialize.materialize_tree`（Task 1）。
- Produces: `MATERIALIZE_OP = "materialize"`；`POST /internal/nodes/{node_id}/agent/materialize`，body 见 spec §4.2；成功 200 `{"op": "materialize", "sandboxID", "tree": {...}, "slices": [...]}`；agent 配置项 `materialize_max_concurrency` / `materialize_busy_timeout_s`。

- [ ] **Step 1: Write the failing tests**

把 `test_agent_materialize.py` 的 helper 从 `POST /internal/grants/file-op` 改成 `POST /internal/nodes/{host}/agent/materialize` + `X-Internal-Key`，body 由 `{"grant": token}` 改成 spec §4.2 的明文计划。新增/保留的断言：

`test_a_valid_instruction_creates_and_chowns_the_tree`（树与 `workspace/` 都在；runner 收到的 argv 恰好是 `chown --uid U --gid G --recursive --path <root>`）；
`test_the_worker_identity_reaches_the_child_environment`（`E2B_BROKER_WORKER_GID == worker.gid`，且 `E2B_BROKER_WORKER_UID` 不出现）；
`test_a_path_outside_the_roots_is_refused_named`（`path-outside-roots`，且**没有**调用 runner）；
`test_an_instruction_for_another_host_is_refused_named`（403）；
`test_an_unknown_op_is_still_refused_named`（`chown` 之外的新 op 名不存在的仍是 404）；
`test_a_symlink_in_the_snapshot_is_recreated_not_followed`、`test_a_symlinked_destination_segment_is_refused_named`、`test_an_ordinary_merge_keeps_existing_files`、`test_a_file_where_a_directory_belongs_is_refused_named`、`test_a_partial_copy_is_reported_as_failure`（**原样保留**，只改调用方式）；
`test_a_busy_agent_answers_busy_without_touching_the_tree`：把 `materialize_max_concurrency` 设成 1、让第一个请求的 runner 阻塞，第二个请求断言 503 且 body 含 `materialize is busy`，且第二个请求**没有**调用 runner。

`tests/unit/test_c3_agent_fileops.py` 加 `test_a_materialize_saturation_does_not_block_a_slot_grant`：materialize 占满自己的上限时，`/agent/grant-slot` 仍然在 1 s 内答复（面 A 不被拖住）。

- [ ] **Step 2: Run tests to verify they fail**

Run: `env -u all_proxy -u http_proxy -u https_proxy tmp/venv/bin/python -m pytest tests/unit/test_agent_materialize.py tests/unit/test_c3_agent_fileops.py -q`
Expected: FAIL（404：`materialize` 不在 op 白名单）

- [ ] **Step 3: Implement**

1. `c3_agent/config.py`：加 `materialize_max_concurrency`（`E2B_C3_AGENT_MATERIALIZE_MAX`，默认 4）与 `materialize_busy_timeout_s`（`E2B_C3_AGENT_MATERIALIZE_BUSY_TIMEOUT_S`，默认 5.0），两行注释写明 §4.6 的两条理由（不排进共享 64；不把 anyio 的 40 个线程吃光）。
2. `c3_agent/app.py`：`MaterializeBody`/`TreePlan`/`SlicePlan` 三个 pydantic 模型（字段与 spec §4.2 逐字相同，`mode` 默认 `"0770"`、`subdir` 默认 `"workspace"`、`copy_from` 默认 `None`）；`_worker_identity(op, body)` 改成 `_worker_identity(op, worker: WorkerCredentials | None)` 以便两个 body 共用；新增 `_materialize(body)`，把 body 转成 `materialize_tree` 认的 dict 后 `to_thread` 执行；`app.state.materialize_slots = asyncio.Semaphore(settings.materialize_max_concurrency)`，取不到就在 `materialize_busy_timeout_s` 后回 503 `{"error": "materialize is busy: ..."}`（**不排队**，具名）。
3. **删掉**面 B 的 `/internal/grants/file-op` 路由、`spent_grants`、`_grant_status`、`_materialize_status` 与 `GrantRefusal/verify` 的 import。

- [ ] **Step 4: Run tests to verify they pass**

Run: 同 Step 2。Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add c3_agent/app.py c3_agent/config.py tests/unit/test_agent_materialize.py tests/unit/test_c3_agent_fileops.py
git commit -m "feat(agent): 既有通道上加 materialize verb —— 控制面直送的建箱材料化（含自己的并发预算）"
```

---

### Task 3: 控制面侧：`C3AgentClient.materialize()` + 建箱路径接上

**Files:**
- Modify: `control_plane/c3_agent_client.py`、`control_plane/api/sandboxes.py`
- Delete: `control_plane/api/internal.py` 的 `/file-grant` 端点、`control_plane/config.py` 的 `create_grant_ttl_s`、`gateway_common/create_grant.py`、`tests/unit/test_create_grant.py`、`tests/unit/test_file_grant_endpoint.py`
- Test: `tests/unit/test_cp_materialize_instruction.py`（**新建**，取代 `test_file_grant_endpoint.py`）

**Interfaces:**
- Consumes: `control_plane/file_ops.derive_materialize(record, *, paths, node_id, worker_gid, snapshot_id)`（已在 `5cb853d` 落地，**原样复用**）；`C3AgentClient._target`/`_prepare_target`（私有的既有路径）。
- Produces: `C3AgentClient.materialize(*, node_id: str, sandbox_id: str, tree: dict, slices: list[dict], worker_uid: int, worker_gid: int, worker_container_id: str | None = None) -> dict`；`control_plane/api/sandboxes.py::_materialize_remote(request, record, node, settings, snapshot) -> bool`。

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_cp_materialize_instruction.py`（沿用旧文件的 `_Cp`/`_app` 夹具，但把 `NODE_A = "node_a"` **拆成两个不同的名字** —— `WORKER = "e2b-worker-0"` 与 `HOST = "k0s-worker-0"`，`StaticAgentAddressResolver` 的 target 用 `HOST`，记录与 `_enroll` 用 `WORKER`；这是本任务最重要的一条夹具改动）：

`test_the_instruction_is_addressed_to_the_host_of_the_workers_node`（记录的 `node_id` 是 `e2b-worker-0`，断言 stub 收到的 `node_id` 是 `e2b-worker-0` 而指令发往 `HOST` 的 maint URL —— 即 v1 Critical 1 的回归钉子）；
`test_the_instruction_carries_the_derived_tree`（`tree.path == <ws>/<id>`、`tree.uid == record.host_uid`、`tree.gid == <worker gid>`）；
`test_a_snapshot_create_carries_copy_from`；`test_a_quota_volume_contributes_a_slice`；`test_a_volume_without_a_quota_contributes_nothing`；
`test_a_sandbox_without_a_host_uid_refuses_before_instructing`；
`test_a_refused_instruction_fails_the_create_and_releases_the_record`（stub 抛 `AgentClientError` ⇒ 建箱 5xx，且 `registry.get(id) is None`、uid 已归还）；
`test_a_local_node_needs_no_instruction`（`node.address == "local://"` ⇒ 一次指令都不发）。

- [ ] **Step 2: Run tests to verify they fail**

Run: `env -u all_proxy -u http_proxy -u https_proxy tmp/venv/bin/python -m pytest tests/unit/test_cp_materialize_instruction.py -q`
Expected: FAIL（`C3AgentClient` 没有 `materialize`）

- [ ] **Step 3: Implement**

1. `control_plane/c3_agent_client.py`：加 `async def materialize(...)`，形状与 `chown` 同构 —— 用 `self._target(node_id)`（**worker 键**，不是 `resolve_agent`）、`maint_url`、`_worker_body(...)`、`_instruct(...)` 的 `refusal`/`timeout_tail`。**不进 `self._semaphore`**（spec §4.6 第 1 条：它会让信号量变成 create 的第一个排队点），并在方法 docstring 里写明这一点与理由。
2. `control_plane/api/sandboxes.py`：加 `_materialize_remote(...)`，在 `create_sandbox` 的 `else:` 分支里**先**调它、**再**调 `_provision_remote(..., materialized=True)`；任一失败都走既有的 `registry.delete(record.sandbox_id)` 回滚。`local://` 与"没有 c3_agent_client / 节点没报 worker 身份"的形态不发指令，`materialized=False`。
   `_provision_remote` 的 payload 加 `"materialized": bool`。
3. 删掉 `/file-grant` 端点、`create_grant_ttl_s`、`gateway_common/create_grant.py` 与两个旧用例文件（此时它们已无消费者）。

- [ ] **Step 4: Run tests to verify they pass**

Run: 同 Step 2，再跑 `tests/unit/test_c3_fileops_forwarding.py tests/unit/test_c3_self_heal_sweep.py tests/unit/test_provision_remote_client.py`。Expected: 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add control_plane/c3_agent_client.py control_plane/api/sandboxes.py control_plane/api/internal.py control_plane/config.py tests/unit/test_cp_materialize_instruction.py
git rm gateway_common/create_grant.py tests/unit/test_create_grant.py tests/unit/test_file_grant_endpoint.py
git commit -m "feat(cp): 建箱前直送一条 materialize 指令（worker 键解析宿主身份）；删掉载体 B 的票据与端点"
```

---

### Task 4: worker 认 `materialized` 标志（并删掉 worker 侧的直连客户端）

**Files:**
- Modify: `envd_service/agent.py`、`envd_service/agent_fileops.py`
- Test: `tests/unit/test_create_materialize_switch.py`（改）、`tests/unit/test_c3_fileops_worker.py`（删领权用例、把 `_AgentStub.materialize` 删掉）

**Interfaces:**
- Consumes: payload 的 `materialized: bool`（Task 3）。
- Produces: `_agent_create_sandbox` 在 `materialized is True` 时跳过建树/拷贝段与属主段，并 `build_volume_mounts(..., slices_materialized=True)`；`False`/缺省时行为与今天逐字相同。

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_create_materialize_switch.py` 重写为三条：
`test_a_materialized_create_skips_the_tree_and_the_handover`（payload 带 `materialized: True` ⇒ `shutil.copytree` 没被调用、`apply_sandbox_ownership` 没被调用、`build_volume_mounts` 收到 `slices_materialized=True`）；
`test_a_plain_create_still_builds_the_tree_and_hands_it_over`（不带该键 ⇒ `copytree` 调用一次、`chown_workspace` 调用一次、`slices_materialized=False`）；
`test_an_unknown_flag_value_is_treated_as_absent`（`materialized: None` / 缺键 ⇒ 老路）。

- [ ] **Step 2: Run tests to verify they fail**

Run: `env -u all_proxy -u http_proxy -u https_proxy tmp/venv/bin/python -m pytest tests/unit/test_create_materialize_switch.py -q`
Expected: FAIL（worker 仍在调 `client.materialize`）

- [ ] **Step 3: Implement**

`envd_service/agent.py::_agent_create_sandbox`：`materialized = payload.get("materialized") is True`；删掉 `_materialize_or_degrade` 及其调用；建树/拷贝段与属主段都由 `if not materialized:` 包住（属主段的 `elif not settings.per_sandbox_uid` 分支保持原样，只在未材料化时生效）；`build_volume_mounts(..., slices_materialized=materialized)`。
`envd_service/agent_fileops.py`：删 `materialize()`、`_mint_create_grant()`、`AgentMaterializeUnsupported`、`MATERIALIZE_OP` 与其 import。
`tests/unit/test_c3_fileops_worker.py`：删掉 6 条领权/降级用例与 `_AgentStub.materialize`，保留中继路用例（它们回到"worker 自己建树 + 中继 chown"的形态，即 `_AgentStub` 最初的样子）。

- [ ] **Step 4: Run tests to verify they pass**

Run: `env -u all_proxy -u http_proxy -u https_proxy tmp/venv/bin/python -m pytest tests/unit/test_create_materialize_switch.py tests/unit/test_c3_fileops_worker.py tests/unit/test_c3_fileop_degradation.py tests/unit/test_create_marker.py tests/unit/test_create_deferred_persist.py -q`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add envd_service/agent.py envd_service/agent_fileops.py tests/unit/test_create_materialize_switch.py tests/unit/test_c3_fileops_worker.py
git commit -m "feat(worker): 认 materialized 标志 —— 树已由 agent 就绪；删掉 worker 侧的直连客户端与降级分支"
```

---

### Task 5: DELETE 与"控制面已材料化、worker 尚未开始"的窗口

**Files:**
- Modify: `control_plane/api/sandboxes.py`（删除路径）、`control_plane/registry/manager.py`（若需要"在建"标记的读接口）
- Test: `tests/unit/test_cp_create_delete_window.py`（新建）

**Interfaces:**
- Produces: 删除一个**正在建箱（控制面侧材料化已开始、尚未返回）**的沙箱时，删除路径**有界等待**该建箱结束（上限取 `settings.remote_http` 的 60 s 量级，或一个新增的 `E2B_CREATE_WINDOW_WAIT_S`），等不到则按"未完成的建箱"回收；无论哪条路径，**都不留**"记录在、树没了"或"记录没了、树还在"的组合。

- [ ] **Step 1: Write the failing tests**

`test_a_delete_during_materialization_waits_for_the_create_then_removes_both`（用慢的假 materialize 挂住建箱，并发发 DELETE：断言 DELETE 在材料化释放前不返回，之后记录与树都不在）；
`test_a_delete_that_gives_up_leaves_no_record_and_no_tree`（材料化超过等待上限：断言最终**没有**记录、**没有**树 —— 半棵树交给既有 orphan 回收，绝不出现"记录在、树没了"）；
`test_a_completed_create_is_never_removed_by_a_late_delete`（正常建箱后 DELETE ⇒ 一次干净的拆除）。

- [ ] **Step 2: Run tests to verify they fail**

Run: `env -u all_proxy -u http_proxy -u https_proxy tmp/venv/bin/python -m pytest tests/unit/test_cp_create_delete_window.py -q`
Expected: FAIL（当前 DELETE 直接删记录，与在飞的建箱赛跑）

- [ ] **Step 3: Implement**

按 spec §4.5：把"这次建箱正在进行"的事实放在控制面自己的簿记里（`registry` 的在建标记，与准入用同一套），删除路径先有界等待它；放弃等待时**先作废该标记**，让在飞的建箱在写记录前发现自己已不被认（拒绝落盘并按未完成回收），而不是让它把记录写回去。

- [ ] **Step 4: Run tests to verify they pass**

Run: 同 Step 2，再跑 `tests/unit/test_create_marker.py tests/unit/test_create_deferred_persist.py`。Expected: 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add control_plane/api/sandboxes.py control_plane/registry/manager.py tests/unit/test_cp_create_delete_window.py
git commit -m "fix(cp): 建箱窗口内的 DELETE 有界等待 —— 不留'记录在、树没了'"
```

---

### Task 6: 集群验收 + 文档

**Files:**
- Modify: `docs/deploy-clusters.md`（§7.27）、`docs/open-issues.md`（N56）、`README.md`（① 的数字）、`docs/c3-privilege-relocation.md`（§14.1 那条不改，只在 §14.2 的 op 词表加 `materialize`）

**Interfaces:**
- Consumes: `deploy/scripts/acceptance/create_latency_probe.py`、`worker_provision_cost.py`、`E2B_CREATE_TRACE=1`。

- [ ] **Step 1: 从干净 worktree 构建并上线**

```bash
deploy/scripts/open-cluster-tunnel.sh          # 先自检连对集群（见 docs/deploy-clusters.md §2）
git worktree add --detach tmp/build-wt <本计划末次提交>
cp -R wheels tmp/build-wt/wheels && cp deploy/scripts/acr.env tmp/build-wt/deploy/scripts/acr.env
screen -dmS e2bbuild sh -c "cd $(pwd)/tmp/build-wt && ./deploy/scripts/build-and-push.sh > $(pwd)/tmp/k0s/release-materialize.log 2>&1; echo EXIT=\$? >> $(pwd)/tmp/k0s/release-materialize.log"
```
`EXIT=0` 后写 `deploy/stack/.version`，再 `export KUBECONFIG="$PWD/tmp/k0s/kubeconfig"` + `./deploy/k8s-k0s/apply.sh`。

- [ ] **Step 2: 量延迟**

同原计划：控制面 pod 内 `create_latency_probe.py`（n=10），期望 p50 **145–150 ms**（改前 191 ms）；开 `E2B_CREATE_TRACE=1` 后 `fileop:*` 段消失，控制面侧出现一次 `materialize`。

- [ ] **Step 3: 冒烟与残留**

`MULTI-NODE SMOKE OK` + `DEPLOYMENT SMOKE OK`（**含跨节点迁移保文件**，并**新增一条带快照的建箱**：快照里放 `workspace/kept.txt`，沙箱内读到它在树根下而不是 `workspace/workspace/`）；`GET /sandboxes` = 0、两 worker `_runtime`/`workspaces` = 0、`DRY_RUN=1 apply.sh | kubectl diff -f -` 0 行。

- [ ] **Step 4: 写文档**

§7.27（读数表 + 复跑命令 + §4.3.1 四条硬要求各自的用例名 + 快照落点那条）；N56 行（记下**载体 B 被否的三条依据**与 v2 的取舍，免得下一轮有人重走）；README ① 的数字改成三段式。

- [ ] **Step 5: Commit**

```bash
git add docs/deploy-clusters.md docs/open-issues.md README.md docs/c3-privilege-relocation.md
git commit -m "docs(n56): 建箱材料化改走控制面直送（延迟读数 + 硬要求用例 + 载体 B 的否决依据）"
```

---

## Self-Review

**Spec 覆盖**：§2 目标 → Task 3/4；§3 不反转规则 → 全程（Task 2/3 只在既有通道上加 verb）；§4.1 形状 → Task 3；§4.2 线格式 → Task 2；§4.3 合成 op → 沿用 + Task 1；§4.3.1 四条 → Task 2（源/目标/半棵树）+ Task 6 Step 3（迁移保文件与快照落点）；§4.4 标志 → Task 4；§4.5 标记与窗口 → Task 5（worker 侧的标记沿用 `d7a0e04`/`804f525`）；§4.6 并发 → Task 2 + Task 6 Step 2；§5 无票据 → Task 3 的删除项；§6 验收 → Task 6；§8 回退 → Task 4 的三条用例。

**类型一致性**：`materialize`（op 名）在 agent 路由 / CP 客户端方法 / 用例里同一拼写；`materialize_tree(plan, *, settings, runner)` 与 `copy_tree(src, dst, *, dir_mode)` 全程不改签名；`slices_materialized`（volumes）与 payload 的 `materialized` 是两个不同的东西，名字刻意不同；`AgentMaterializeUnsupported` 在 Task 4 删除后不再被任何文件 import（Task 4 Step 4 的相邻车道会抓到）。

**比例**：6 个任务；代码块只出现在"必须定死的算法"与命令上，其余是签名 + 用例名 + 断言。

**风险最高的三步**：Task 1 Step 3（落点，改错会让快照建箱悄悄多一层）、Task 2 Step 3（并发预算：取不到要具名回绝而不是排队）、Task 5 Step 3（放弃等待时谁作废谁的标记）。三处都先红后绿，Review Focus 各占一条。
