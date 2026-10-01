# 建箱材料化（载体 C）收尾计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development（本计划按"每任务一个 implementer + 一个 reviewer"派发）。Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 v2 载体 C 剩下的四件事做完并上线：C2 的真钉子、(b) 两跳并发、M11、集群验收读数。

**Architecture:** 不改三角分工与信任模型（控制面决策 / agent 执行 / worker 跑沙箱；`materialized` 是一个 payload 布尔）。本计划只做三件事：补证据、把两跳从串行改成并发、把既有差异收口。

**Spec:** `docs/superpowers/specs/2026-10-01-create-path-grant-design-v2.md`（§4.5 窗口、§4.6 并发预算、§6 验收矩阵）。

**Status of the branch this plan continues**（`57f7ca2`）：C1/I3/I4/I5/I6 已修；M7/M9/M10 已修；C2 代码已落地但**只有 unit 级证据**；(b) 未做；集群仍跑未修版本 `0.1.0-864-g3377ffc`。

## Global Constraints

- 不新增 `worker → agent` 通道；worker 没有 `E2B_C3_AGENT_TOKEN`（`docs/c3-privilege-relocation.md` §14.1 一个字不改）。
- 路径由控制面命名、agent 独立复核（§14.4 两道不互相替代）。
- §4.3.1 四条硬要求不变（源侧不解引用 / 目标侧具名拒绝 / 半棵树不报成功 / 迁移保文件）。
- (b) 之后**建箱仍必须是同步契约**：材料化失败 ⇒ 建箱失败，不许变成"201 之后第一条命令才炸"。
- 每条断言精确匹配；不许 SKIP；不许留"在 stub 下也绿"的假钉子（本轮已出现过两次）。
- 集群写操作前先跑 `deploy/scripts/open-cluster-tunnel.sh` 自检（2 节点 / arm64 / `+k0s`；见 `docs/deploy-clusters.md` §2）。

## Review Focus

1. **两跳并发后的失败面**：CP 并发发出两条指令，材料化失败时 worker 那一半必须被收干净（不许留下半棵树或一个活运行时）。→ Task C
2. **worker 等待的有界性**：等"树就绪"必须有上限，且超限是**具名失败**而不是挂住。→ Task C
3. **降级路不受影响**：不带 `materialized` 的建箱（旧 CP、迁移、fork）行为必须逐字不变。→ Task C
4. **C2 的钉子必须能失败**：stub 掉被测检查时用例必须变红。→ Task A
5. **既有树上的目录权限**：快路与老路对"每个目录 0770"这点必须一致，否则迁移/重建出来的树，worker 可能写不进去。→ Task D

---

## 文件结构

| 文件 | 责任 | 本计划改它 |
|---|---|---|
| `tests/unit/test_cp_create_delete_window.py` | 建箱窗口（同副本 / 跨副本 / 放弃 / 完成） | Task A |
| `control_plane/api/sandboxes.py` | 建箱与拆除路径、在飞声明、共享检查 | Task A（若需）、Task C |
| `envd_service/agent.py` | worker 建箱（本计划把它拆成 prepare / finalize 两相） | Task C |
| `control_plane/c3_agent_client.py` | CP→agent 指令 | Task C（可能加一个收口调用） |
| `c3_agent/materialize.py` | 硬化材料化 | Task C（完成信号）、Task D（目录权限） |
| `deploy/stack/.version` + 集群 | 发布与读数 | Task E |

---

### Task A: C2 的集成钉子（先找，再钉）

**Files:** Modify `tests/unit/test_cp_create_delete_window.py`；必要时 `control_plane/api/sandboxes.py`。

**Interfaces:** Produces: 一条**在 `_record_is_still_ours` 被 stub 成 `True` 时会红**的跨副本用例。

已知事实（省得重查）：两个 app 共用一个注入 `record_store=` 的 `SandboxRegistry` 时，把该检查 stub 成 `True` 后用例**照样通过** ⇒ 这个形状里另有东西先回了 409，用例测不到该检查。

- [ ] **Step 1: 找出那个先到的 409。** 在跨副本形状里逐步打印/断言：`_create_sandbox_attempt` 的哪条分支给出 409（`grep -n '409' control_plane/api/sandboxes.py` 只有两处：本计划的 abandoned 分支与 `SandboxStateConflictError` 的映射）。把结论写进用例 docstring。
- [ ] **Step 2: 写一条能失败的用例。** 让"记录被另一个副本删掉"这个事实真的到达 `registry.save` 之前的那次检查（必要时把 registry 的 `save` 换成透写替身），先确认它在 `_record_is_still_ours` stub 成 `True` 时**红**。
- [ ] **Step 3: 跑绿**（`tmp/venv/bin/python -m pytest tests/unit/test_cp_create_delete_window.py -q`），并保留 unit 级钉子。
- [ ] **Step 4: 提交**（`git commit -m "test(cp): C2 的跨副本钉子 —— stub 掉检查时必须红"`）。

### Task B: (b) 的形状实验（先量，再定）

**Files:** 只写 `tmp/b-two-phase-measurement.md`（项目内 tmp，gitignored）；不改生产代码。

**Interfaces:** Produces: 一个决定 —— 收口信号用 **(i) CP 补一条空指令** 还是 **(ii) agent 写完成标记 + worker 轮询**，附读数。

- [ ] **Step 1: 在控制面 pod 内量**（与真实调用同一网络位置）：① 直打 agent 维护面的**空指令**往返（已知参考 1.8 ms）；② 在 NAS 上写/读一个 `<ws>/<id>/.materialized` 小文件（已知参考：写 12 ms、stat 负缓存 0.00 ms）；③ 取一个真实沙箱量 `materialize` 全程（已知 70.8 ms）与 worker 那一跳（已知 76–79 ms）。
- [ ] **Step 2: 把三条读数与结论写进 `tmp/b-two-phase-measurement.md`**，并给出"预计建箱 p50 = max(材料化, worker-prepare) + 收口 + 尾段"的算术。
- [ ] **Step 3: 不改代码，不提交**；把文件路径交给主会话（Task C 依赖它）。

### Task C: (b) 两跳并发

**Files:** Modify `envd_service/agent.py`、`control_plane/api/sandboxes.py`、`control_plane/c3_agent_client.py`（若收口走 CP 补指令）、`c3_agent/materialize.py`（若收口走完成标记）；Test `tests/unit/test_create_two_phase.py`（新）、`tests/unit/test_create_materialize_switch.py`（改）。

**Interfaces:**
- Consumes: Task B 的形状决定。
- Produces: CP 建箱路径 = `gather(agent.materialize, worker.prepare)` → 收口 → 返回；worker 的 prepare 只做不需要树的部分（uid 认领、卷配额、xfs project、盘上统计），收口做树依赖的部分（挂载视图）与记录。

- [ ] **Step 1: 写失败用例**：`test_the_worker_starts_while_the_tree_is_still_being_made`（材料化被挂住时，worker 那一半已经开始做 uid/配额 —— 断言它**没有**等材料化完成才动手）；`test_a_failed_materialization_fails_the_create`（材料化失败 ⇒ 建箱失败，且 worker 那半被收干净）；`test_a_slow_materialization_is_bounded_and_named`；`test_a_plain_create_still_takes_the_old_path`（不带标志 ⇒ 逐字不变）。
- [ ] **Step 2: 先红**（`tmp/venv/bin/python -m pytest tests/unit/test_create_two_phase.py -q`）。
- [ ] **Step 3: 实现**（按 Task B 定的形状；worker 侧把 `_agent_create_sandbox` 拆成 prepare/finalize，CP 侧 `asyncio.gather` + 收口）。
- [ ] **Step 4: 跑绿**，并跑相邻车道（`test_create_marker.py`、`test_create_deferred_persist.py`、`test_cp_create_delete_window.py`、`test_create_materialize_switch.py`）。
- [ ] **Step 5: 提交**。

### Task D: M11 + 清理

**Files:** Modify `c3_agent/materialize.py`、`tests/unit/test_agent_materialize.py`；删 `tmp/build-wt`（git worktree）。

**Interfaces:** Produces: 快路与老路对"树里每个目录都是 0770"这件事**一致**，并有用例；`git worktree list` 只剩主工作区。

- [ ] **Step 1: 写失败用例**：预先在目标树上放一个 **0755 的目录**（"上一次失败留下的残树"形状），走材料化，断言它变成 0770（老路 `apply_sandbox_ownership` 的行为）。
- [ ] **Step 2: 先红**。
- [ ] **Step 3: 实现**（在硬化遍历里对既有目录补 `fchmod`，只在快照合并那条路上即可）。
- [ ] **Step 4: 跑绿**；`git worktree remove --force tmp/build-wt && git worktree prune`。
- [ ] **Step 5: 提交**（代码与清理分开两个提交）。

### Task E: 重建、上线、读数、文档

**Files:** Modify `deploy/stack/.version`、`docs/deploy-clusters.md`（§7.28）、`docs/open-issues.md`（N56 状态）、`README.md`（① 的四段式）。

**Interfaces:** Consumes: `deploy/scripts/acceptance/create_latency_probe.py`、`E2B_CREATE_TRACE=1`、`deploy/scripts/open-cluster-tunnel.sh`。

- [ ] **Step 1: 先自检集群**（2 节点 / arm64 / `+k0s`），再 `git worktree add --detach tmp/build-wt <HEAD>` + wheels + `acr.env` → `build-and-push.sh`（**用 `screen`**，`nohup &` 会被会话连坐杀掉）→ `EXIT=0` 后写 `.version` → `DRY_RUN` 确认 diff 只有镜像 tag → `apply.sh`。
- [ ] **Step 2: 量**：控制面 pod 内 `create_latency_probe.py --n 10`（期望 **(b) 之后 ~90–100 ms**，改前 191/193）；`E2B_CREATE_TRACE=1` 确认建箱路径上 `fileop:*` 为 0 行、出现 `materialize`；worker 那一跳的逐段。
- [ ] **Step 3: 冒烟**：`MULTI-NODE`、`DEPLOYMENT`（含跨节点迁移保文件）；**外加一条生产形状的快照建箱**（`workspace/kept.txt` 读到、`workspace/workspace/` 不在）；`GET /sandboxes` = 0、两 worker 无残留、`DRY_RUN | kubectl diff` 0 行、pod 全 Running。
- [ ] **Step 4: 写 §7.28 / N56 / README**，把每条判据与读数对上；C2 那条钉子与 (b) 的形状各自指明用例名。
- [ ] **Step 5: 提交**。

---

## Self-Review

**覆盖**：C2 真钉子 → A；(b) 形状 → B → C；M11 与清理 → D；发布与读数 → E。N56 里登记的其余项（假绿、`_release` 守卫）已在 `38b2998`/`57f7ca2` 落地，本计划不重复。

**依赖**：A/D 与 B 互不依赖（可并行）；C 依赖 B；E 依赖 C。文件不重叠：A 动 `tests/unit/test_cp_create_delete_window.py`（必要时 `api/sandboxes.py`），B 只写 `tmp/`，D 动 `c3_agent/materialize.py` + 其用例 + worktree，C 动 `envd_service/agent.py` + `api/sandboxes.py` + 新用例 —— **A 与 C 都可能碰 `api/sandboxes.py`，所以 A 必须在 C 之前完成或至少先提交**。

**比例**：5 个任务；两处必须定死的形状（A 的钉子形状、B 的收口信号）各有一个任务专门解决，其余是"先红后绿 + 读数"。
