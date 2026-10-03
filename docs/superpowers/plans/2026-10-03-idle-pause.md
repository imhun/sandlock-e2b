# 空闲即暂挂（idle → pause）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 闲置 5 分钟的沙箱自动暂挂（冻结现场、归还准入配额），暂挂 30 分钟后回收；有活跃 exec 流或 CPU 在跑的沙箱不算闲置。

**Architecture:** 三件事。① worker 侧让"开着的流"持续算活动 —— 复用既有 activity 上报通道（worker 心跳里的 `sandboxActivity`），不新增协议字段；② 控制面新增一个 15 s 一轮、跨副本单飞的 `IdlePauseSweeper`，动作复用 `POST /sandboxes/{id}/pause` 那条链（把 `api/sandboxes.py` 里那几个 request 依赖的 helper 抽成 request-free，好让后台任务调用）；③ 部署侧只做策略取值（`E2B_IDLE_PAUSE_AFTER_S=300` / `E2B_PAUSED_TTL_S=1800` / `E2B_ORPHAN_RECORD_TTL=300`），代码默认全部 `0 = 关`，合并本身不改变任何现有部署的行为。

**Tech Stack:** Python 3.11、FastAPI、asyncio、Redis（单飞 claim）、k0s + kustomize、pytest。

**Spec:** 无独立设计文档；本计划的规格就是下面 Global Constraints 里逐字记录的 2026-10-03 用户裁定。

## 当前进度（2026-10-03：**Task 1–5 + N77 全部完成并上线 `0.1.0-965-gb5f194a-20261003-193743`；两条现场验收已过**）

| Task | 状态 | 提交 |
|---|---|---|
| 计划 | ✅ | `6d2786d`（本文档） |
| 1 开着的流持续算活动 | ✅ | `3e24702`（`envd_service/connect/router.py` + `tests/unit/test_stream_activity_keepalive.py`，2 条） |
| 2 pause 链抽成 request-free | ✅ | `1684523`（`control_plane/api/sandboxes.py` + `tests/unit/test_platform_pause_action.py`，3 条） |
| 3 `IdlePauseSweeper` | ✅ | `67ebd1c`（`control_plane/registry/idle_pause.py` + `config.py` + `app.py` + `tests/unit/test_idle_pause_sweeper.py`，12 条） |
| 4 部署取值 + 文档 | ✅ | `8b4ebd0`（`deploy/k8s-k0s/control-plane-nfs.patch.yaml` + `docs/env-vars.md`；`kubectl kustomize` 渲染出三个 env，值 `300`/`1800`/`300`） |
| 5 无戳孤儿回落 | ✅ | `06ded8b`（`manager.py::_ttl_reapable` + `tests/unit/test_sandbox_registry.py` 3 条，贴在既有 N22 钉子旁） |
| 验收探针 | ✅ | `d73398a`（`deploy/scripts/acceptance/probe_idle_pause.py` + `tests/unit/test_probe_idle_pause_script.py`，4 条） |
| 回归 | ✅ | `tests/unit` **2419 passed / 12 skipped / 3 failed**（3 条是既有的 macOS-only：`test_real_root_gate` 与 2 条 `test_xfs_quotactl_backend`，需要 Linux `libc.so.6`） |
| 上线 | ✅ | `0.1.0-963`（本批）→ 现场抓到 N77 → `0.1.0-965` 重上；发版记录见 `docs/deploy-clusters.md` §7.36 |
| 现场验收 1：闲置 → 暂挂 → 恢复 | ✅ | `t+302.4 running → t+317.5 paused → connect 200 → running`；控制面日志 `idle pause: sandbox … idle 309s (>= 300s); paused` |
| 现场验收 2：静默 hold 的流不算闲置 | ✅ | 150 s hold 期间始终 `running`、`lastActiveAt` 动 7 次；脚本已固化为 `deploy/scripts/acceptance/probe_stream_keepalive.py` |
| **N77（本轮现场抓到的 bug）** | ✅ 已修 + 已上线 | 判死读本副本缓存而非共享行 ⇒ 误判活节点、把活沙箱标孤儿（被本批的 orphan TTL 放大成 5 分钟后真拆）。修 `8c5b4ec`，先红钉子 `test_redis_multireplica.py::test_the_health_sweep_reads_the_shared_view_not_the_local_cache`；登记在 `docs/open-issues.md` N77 |

三处与原计划不同的做法（都记在上面各自 Task 里）：Task 2 用 `_state_of(ctx)` 一个访问器替掉 16 处调用点改动；Task 5 的钉子放进了 `tests/unit/test_sandbox_registry.py`，没有另起文件；探针在**现场**被改了两回（先读错载荷：`GET /sandboxes/{id}` 不含 `state`；再把"返回字符串的 fetch"套进按 payload 解析的 `state_of` —— 两次都补了钉子）。

## Global Constraints

- 闲置阈值 `E2B_IDLE_PAUSE_AFTER_S=300`（5 分钟，避免反复暂挂）；扫描间隔 `15 s`；claim key `e2b:idle-pause:sweep`，TTL = 间隔（沿用 `F11` 的 `SET NX EX` 协议，无释放路径）。
- 代码默认 `0 = 关`（off 是 inert：不建 task、不取 claim），与 `PausedTTLSweeper` 同一条既有约定；k0s 部署显式取值。
- 暂停超过 `E2B_PAUSED_TTL_S=1800`（30 分钟）由既有 `PausedTTLSweeper` 回收（不再默认关闭）。
- `E2B_ORPHAN_RECORD_TTL=300` + 无 `orphaned_at` 戳的旧孤儿回落 `end_at`（见 Task 5）。
- 只对 `state == "running"` 生效；`paused` / `orphaned` / 其它状态一律不是候选。
- 跳过两种记录：已过期（TTL 扫描的事）、`end_at - now < 60 s`（不值得为不到一分钟去暂停）。
- "活跃 exec / 打开的流" 算活动（worker 侧保证，Task 1）；CPU ≥ `E2B_CPU_ACTIVITY_PERCENT`（默认 5）已是活动，不改。
- 豁免开关：`metadata["e2b_pause_on_idle"]` 取值 `{0,false,no,off}`（去空白、大小写不敏感）时不暂挂。
- 第一期不做跨节点恢复、不做 resume 抢占。恢复失败的对外契约**不变**：仍是 `503 {"code":503,"message":"No resources available"}`（`tests/contract/test_pause_resume_quota.py:94` 钉着这句），只在控制面日志里具名"沙箱钉在哪个节点、该节点哪个维度满"。
- 一条记录失败不许中断整轮（N61/N72 的教训：`expired_candidates()` 在逐条 try 之外，一条坏数据能让全舰队停摆）。可见性沿用 TTL sweeper 的形状：候选清单 INFO、单轮超时（> 3 × 间隔）WARNING、每 ~30 轮汇总 INFO。
- 断言精确匹配、禁 SKIP/xfail；编辑一律 `apply_patch`；每条"能失败"的钉子必须先证明它会红。

## Review Focus

1. **长时间静默的流**（`sleep 300`、等外部 API 的 exec）：CPU≈0、无新流量，但客户端正握着流 —— 必须**不**暂挂（Task 1 的钉子）。
2. **恢复被峰值打回**：resume 要重新占配额，配额被别人拿走 —— 必须答 503 且沙箱留在 `paused`（不是半个状态），且不得泄漏准入（Task 2 的钉子）。
3. **暂停的磁盘账是谎话**：`_park_capacity` 把 disk 维度一起还了，但树还在盘上 —— 由 `E2B_PAUSED_TTL_S=1800` 兜住，`docs/env-vars.md` 写明这个代价（Task 4）。
4. **候选集里的一条坏记录**：只跳过它，整轮其余照做（Task 3 的钉子）。
5. **暂挂与 TTL 打架**：`end_at` 快到了还被暂挂 = 纯 churn —— 60 s 的 `min_remaining_s` 让给 TTL（Task 3 的钉子）。

---

## 文件结构

| 文件 | 责任 |
|---|---|
| `envd_service/connect/router.py`（改） | 流式 RPC 开着期间持续 `mark_active`（Task 1） |
| `control_plane/api/sandboxes.py`（改） | 把 pause/resume 那条链抽成 request-free，新增 `pause_record_for_platform`（Task 2） |
| `control_plane/registry/idle_pause.py`（新） | `IdlePauseSweeper` + 候选选择 + 开关读取（Task 3） |
| `control_plane/config.py`（改） | `Settings.idle_pause_after_s`（Task 3） |
| `control_plane/app.py`（改） | 起停 idle sweeper、wiring claim 与动作（Task 3） |
| `control_plane/registry/manager.py`（改） | `_ttl_reapable` 的无戳孤儿回落（Task 5） |
| `deploy/k8s-k0s/control-plane-nfs.patch.yaml`（改） | 三个策略 env 的取值（Task 4） |
| `docs/env-vars.md`（改） | 三个 env 的语义与代价（Task 4） |
| `tests/unit/test_stream_activity_keepalive.py`（新） | Task 1 |
| `tests/unit/test_platform_pause_action.py`（新） | Task 2 |
| `tests/unit/test_idle_pause_sweeper.py`（新） | Task 3 |
| `tests/unit/test_sandbox_registry.py`（改，贴在既有 N22 孤儿钉子旁） | Task 5 |

---

### Task 1: 开着的流持续算活动（worker 侧）

**为什么先做**：不先有这条，5 分钟的暂挂会咬到正在跑长命令的客户端（`sleep 300` 的 CPU 占用≈0、没有新流量）。

**Files:**
- Modify: `envd_service/connect/router.py`（`handle_stream` 与新增的模块级 helper）
- Test: `tests/unit/test_stream_activity_keepalive.py`

**Interfaces:**
- Produces: `envd_service.connect.router.ACTIVITY_KEEPALIVE_S: float`（= `RuntimeRegistry.ACTIVITY_COALESCE_S`）、`async def _keep_active_while_streaming(registry, sandbox_id: str) -> None`
- Consumes: `RuntimeRegistry.mark_active(sandbox_id: str) -> None`（已存在，内部 10 s 合并）

- [ ] **Step 1: Write the failing test**

```python
class _CountingRegistry:
    def __init__(self):
        self.calls: list[str] = []

    def mark_active(self, sandbox_id: str) -> None:
        self.calls.append(sandbox_id)


def test_a_silent_stream_keeps_the_sandbox_active(monkeypatch):
    monkeypatch.setattr(router, "ACTIVITY_KEEPALIVE_S", 0.01)
    registry = _CountingRegistry()

    async def run():
        task = asyncio.create_task(
            router._keep_active_while_streaming(registry, "sbx_a")
        )
        await asyncio.sleep(0.035)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert registry.calls[0] == "sbx_a"
    assert len(registry.calls) >= 3


def test_the_keepalive_stops_when_the_stream_closes(monkeypatch):
    # 用 asyncio.run 驱动一个「立即结束」的流：body() 消费完之后 keepalive 必须已被取消
    # （断言：流结束后再等 5 x ACTIVITY_KEEPALIVE_S，mark_active 调用数不再增加）
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_stream_activity_keepalive.py -v`
Expected: FAIL —— `AttributeError: module 'envd_service.connect.router' has no attribute '_keep_active_while_streaming'`

- [ ] **Step 3: Implement in `envd_service/connect/router.py`**

`ACTIVITY_KEEPALIVE_S = RuntimeRegistry.ACTIVITY_COALESCE_S`（从 `envd_service.runtime.registry` 导入常量，不写字面量，防止两处漂移）；`_keep_active_while_streaming` 是 `while True: registry.mark_active(sid); await asyncio.sleep(ACTIVITY_KEEPALIVE_S)`，`CancelledError` 直接冒出。`handle_stream` 的 `body()` 里：`sandbox_id = getattr(sandbox, "sandbox_id", None)`，非空时 `asyncio.create_task(...)`，`finally` 里 `cancel()`（`require_sandbox=False` 的路径没有 sandbox，跳过）。

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_stream_activity_keepalive.py tests/unit/test_connect_envelope.py tests/unit/test_sandbox_activity.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add envd_service/connect/router.py tests/unit/test_stream_activity_keepalive.py
git commit -m "feat(envd): an open stream keeps the sandbox active (E9.1)"
```

---

### Task 2: 把 pause 那条链抽成 request-free（控制面）

**为什么**：后台 sweeper 没有 `Request` 对象，而 `_park_capacity` / `_push_pause_state` / `_rollback_pause` / `_resume_with_capacity` 全都吃 `request`。抽成吃 `state`（`app.state`）是唯一能让两条路径共用一套语义的改法 —— 绝不另写一条"并行 pause"。

**Files:**
- Modify: `control_plane/api/sandboxes.py:166`（`_release_node_quota`）、`:208`（`_park_capacity`）、`:222`（`_resume_with_capacity`）、`:272`（`_push_pause_state`）、`:363`（`_rollback_pause`）、`:405`（`_rollback_resume`）与它们的调用点（同文件约 10 处，全部 `request` → `request.app.state`）
- Test: `tests/unit/test_platform_pause_action.py`

**Interfaces:**
- Produces: `async def pause_record_for_platform(state, record, *, reason: str) -> str` —— 成功返回 `"paused"`；worker 具名拒绝时**先回滚再抛** `OfficialError`（与端点一致），由调用方（sweeper）逐条 catch 并具名
- Consumes: `SandboxRegistry.pause(record, reason) -> SandboxRecord`（释放 global/tenant 配额）、`NodeRegistry.release_quota(...)`、`RuntimeRegistry.set_state(...)`

- [ ] **Step 1: Write the failing test**

```python
def test_pausing_returns_the_node_reservation(...):
    # 建一个 running 记录 -> await pause_record_for_platform(state, record, reason="idle")
    # 断言：state == "paused"、record.pause_reason == "idle"、record.paused_at is not None
    # 断言：node.release_quota 被调用一次，四维与 _record_quota_dims(record) 逐字相等


def test_a_refused_worker_push_rolls_the_record_back_and_keeps_no_quota(...):
    # 让 worker 推送返回显式 500 -> 期望 OfficialError
    # 断言：state == "running"（回滚）、节点与全局配额都不留（resume 重新占回后被 release）


def test_a_worker_404_is_treated_as_paused(...):
    # 404 = worker 上没有活运行时，暂停仍然算成功（沿用端点既有语义）


def test_a_full_node_is_named_in_the_log_not_the_response(...):
    # 节点配额拒绝时：响应文本仍是 "No resources available"（契约），
    # 但 caplog 里必须出现节点 id 与拒绝维度（memory/cpu/disk/processes 之一）
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_platform_pause_action.py -v`
Expected: FAIL —— `ImportError: cannot import name 'pause_record_for_platform'`

- [ ] **Step 3: Implement the refactor + `pause_record_for_platform`**

**实现时改了一个做法（已落地，记录在此）**：六个 helper 的第一个参数改名为 `ctx`，函数体里的 `request.app.state.X` 换成 `_state_of(ctx).X` —— 新增的 `_state_of` 一个访问器同时吃 `Request`（端点）与 `app.state`（后台任务），于是**同文件那 16 处调用点一个都不用动**（比原计划"逐处传 `request.app.state`"的 diff 小得多，也少一类"两处写法漂移"）。`pause_record_for_platform` 只做端点里除 `registry.get` / 归属校验 / `record.touch()` 之外的三步：`registry.pause(record, reason)` → `_park_capacity(state, record)` → 推送失败时 `_rollback_pause(state, registry, record.sandbox_id)`；推送管道丢失（transport）仍是 best-effort WARNING（端点既有语义）。`_resume_with_capacity` 的节点拒绝分支补一条 WARNING（日志，**不动**响应文本）：节点 id + `node.blocking_dimension(*dims)` 的结果，让"恢复不了"能一眼看出是钉在哪个节点、哪个维度满。

- [ ] **Step 4: Run the new test plus every existing pause/resume test**

Run: `.venv/bin/python -m pytest tests/unit/test_platform_pause_action.py tests/unit/test_pause_quota.py tests/unit/test_remote_pause_delivery.py tests/unit/test_agent_pause_resume.py tests/unit/test_process_pause_fallback.py tests/unit/test_paused_ttl_sweep.py tests/unit/test_eviction_execution.py tests/contract/test_pause_resume_quota.py -v`
Expected: PASS（既有 503 文本契约不变）

- [ ] **Step 5: Commit**

```bash
git add control_plane/api/sandboxes.py tests/unit/test_platform_pause_action.py
git commit -m "refactor(cp): the pause chain takes app.state, not a Request"
```

---

### Task 3: IdlePauseSweeper（控制面）

**Files:**
- Create: `control_plane/registry/idle_pause.py`
- Modify: `control_plane/config.py`（`idle_pause_after_s`，默认 `_env_float("E2B_IDLE_PAUSE_AFTER_S", 0.0)`）、`control_plane/app.py`（`_IDLE_PAUSE_INTERVAL_S = 15.0`、起停 sweeper、claim、动作）
- Test: `tests/unit/test_idle_pause_sweeper.py`

**Interfaces:**
- Produces: `IDLE_PAUSE_ENV = "E2B_IDLE_PAUSE_AFTER_S"`、`idle_pause_after_seconds(settings=None) -> float`、`idle_pause_exempt(record) -> bool`、`idle_candidates(records, *, after_s, now, min_remaining_s=60.0) -> list[tuple[record, float]]`、`class IdlePauseSweeper`（`enabled` / `due(registry)` / `start(registry)` / `await stop()`）
- Consumes: Task 2 的 `pause_record_for_platform(state, record, *, reason)`

构造签名钉死：`IdlePauseSweeper(*, after_s: float, on_idle: Callable[[object, float], Any], interval_seconds: float = 15.0, claim: Callable[[], bool] | None = None, now: Callable[[], datetime] = utcnow)`。

- [ ] **Step 1: Write the failing tests**（每条一个钉子，断言精确值）

```python
def test_an_idle_running_sandbox_is_paused():
    # last_active_at = now - 301s, end_at = now + 600s
    # due(registry) == [(record, 301.0)] 且 on_idle 收到 ("sbx_a", 301.0)

def test_a_busy_sandbox_is_not_paused():
    # last_active_at = now - 299s -> due == []

def test_an_expired_record_is_left_to_the_ttl_sweep():
    # end_at = now - 1s -> due == []（即使闲置 1 小时）

def test_a_record_that_dies_within_a_minute_is_left_alone():
    # end_at = now + 59s, 闲置 400s -> due == []

def test_only_running_records_are_candidates():
    # registry.list(state_filter=["running"]) 被调用；paused/orphaned 记录不在 due 里

def test_a_metadata_opt_out_is_skipped():
    # metadata={"e2b_pause_on_idle": "OFF"} -> due == []

def test_the_switch_off_starts_no_task_and_takes_no_claim():
    # after_s=0 -> enabled is False, start() 之后 _task is None 且 claim 未被调用

def test_one_failing_record_does_not_stop_the_round():
    # 三个候选，第一个 on_idle 抛异常 -> 后两个仍被调用（每轮报 1 条 WARNING）

def test_the_round_is_single_flight():
    # claim() -> False -> on_idle 一次都没调用
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_idle_pause_sweeper.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'control_plane.registry.idle_pause'`

- [ ] **Step 3: Implement `idle_pause.py` + config + wiring**

模块 docstring 照 `paused_ttl.py` 的口径写清三件事：`0 = 关` 是 inert；动作复用 `POST /pause` 那条链（本模块不自己拆）；单飞 claim 的协议。`due()` 用 `registry.list(state_filter=["running"])`；`_loop` 里把 `due()` 放到 `asyncio.to_thread`（共享 store 扫描，N61 的教训）、逐条 `try/except` 并打具名 WARNING。`app.py` 的 wiring：

```python
idle_sweeper = IdlePauseSweeper(
    after_s=idle_pause_after_seconds(settings),
    on_idle=lambda record, idle_s: pause_record_for_platform(
        app.state, record, reason=f"idle {idle_s:.0f}s"
    ),
    interval_seconds=_IDLE_PAUSE_INTERVAL_S,
    claim=lambda: try_claim(
        redis_client, "e2b:idle-pause:sweep", ttl_s=int(_IDLE_PAUSE_INTERVAL_S)
    ),
)
app.state.idle_sweeper = idle_sweeper
idle_sweeper.start(registry)
```

同处按既有 sweeper 的收尾方式在 lifespan 的清理段 `await idle_sweeper.stop()`（与 TTL / paused sweeper 并列）。

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_idle_pause_sweeper.py tests/unit/test_ttl_sweeper_stall.py tests/unit/test_ttl.py tests/unit/test_paused_ttl_sweep.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add control_plane/registry/idle_pause.py control_plane/config.py control_plane/app.py tests/unit/test_idle_pause_sweeper.py
git commit -m "feat(cp): pause idle sandboxes after E2B_IDLE_PAUSE_AFTER_S"
```

---

### Task 4: 部署策略取值与文档

**Files:**
- Modify: `deploy/k8s-k0s/control-plane-nfs.patch.yaml`（CP 的 env）、`docs/env-vars.md`

- [ ] **Step 1: 加三个 env（注释写清取值理由与代价）**

`E2B_IDLE_PAUSE_AFTER_S: "300"`、`E2B_PAUSED_TTL_S: "1800"`、`E2B_ORPHAN_RECORD_TTL: "300"`。注释必须逐条写明：5 分钟是对着 `E2B_ACTIVITY_PERSIST_INTERVAL_S=30` 与心跳 5 s 定的（远大于两者，避免误判成闲置）；暂挂仍占盘、30 分钟是硬上限；孤儿 300 s 的误判窗口对着 `E2B_NODE_HEARTBEAT_TIMEOUT=30` 与 N32 实测的 76 s 停顿。

- [ ] **Step 2: `docs/env-vars.md` 三个新行**

每行写：变量名、默认值（代码 0 = 关）、本部署取值、语义、代价（尤其"暂停归还 disk 台账但树还在盘上"与"恢复要重新占配额、失败仍答 503"）。

- [ ] **Step 3: 验证清单渲染**

Run: `KUBECONFIG="$PWD/tmp/k0s/kubeconfig" DRY_RUN=1 deploy/k8s-k0s/apply.sh | grep -E "E2B_IDLE_PAUSE_AFTER_S|E2B_PAUSED_TTL_S|E2B_ORPHAN_RECORD_TTL" -A 1`
Expected: 三个变量各出现一次，值分别是 `"300"` / `"1800"` / `"300"`

- [ ] **Step 4: Commit**

```bash
git add deploy/k8s-k0s/control-plane-nfs.patch.yaml docs/env-vars.md
git commit -m "deploy(k0s): idle-pause 300s, paused TTL 1800s, orphan TTL 300s"
```

---

### Task 5: 无戳孤儿不再永久占槽位（N61 那一类收口）

**为什么**：`E2B_ORPHAN_RECORD_TTL > 0` 现在对 `orphaned_at is None` 的记录**完全无效**（`_ttl_reapable` 见无戳直接 `False`），所以"设个值就能自愈"这句话是假的，得先补回落判据。

**Files:**
- Modify: `control_plane/registry/manager.py::_ttl_reapable`（orphaned 分支回落 `end_at`）
- Test: `tests/unit/test_sandbox_registry.py`（**实现时改到既有 N22 孤儿钉子旁边**，而不是原计划的独立文件：同一个机制、同一组 `_settings` / `_create` / `workspace` 夹具，分开放只会让读者多跑一处）

**Interfaces:**
- Produces: `orphaned` + `orphan_record_ttl_s > 0` + `orphaned_at is None` 时，按 `end_at + ttl` 判可回收；`orphan_record_ttl_s <= 0` 时行为逐字不变（永不回收）

- [ ] **Step 1: Write the failing tests**

```python
def test_a_stampless_orphan_is_collected_once_the_ttl_is_set():
    # state="orphaned", orphaned_at=None, end_at = now - 301s, ttl=300 -> _ttl_reapable is True

def test_a_stampless_orphan_survives_with_the_switch_off():
    # 同上但 ttl=0 -> False（今天的默认行为逐字不变）

def test_a_stamped_orphan_still_ages_from_its_own_stamp():
    # orphaned_at = now - 100s, end_at = now - 1h, ttl=300 -> False（不被 end_at 提前收走）
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_sandbox_registry.py -k orphan -v`
Expected: FAIL —— 第一条 `assert False is True`（无戳回落还没有）

- [ ] **Step 3: Implement the fallback**（`orphaned_at or end_at` 作为计时起点，其余不动）

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_sandbox_registry.py tests/unit/test_ttl.py tests/unit/test_ttl_sweeper_stall.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add control_plane/registry/manager.py tests/unit/test_sandbox_registry.py
git commit -m "fix(cp): a stampless orphan ages from end_at when the orphan TTL is set (N61)"
```

---

## 上线后验收（控制者执行，不是实现任务）

1. 构建 + `apply.sh`（两个新 env 随 CP 滚动生效；`DRY_RUN=1 ... | kubectl diff -f -` 先看 0 行）。
2. `deploy/scripts/acceptance/probe_idle_pause.py`（本期新建，只读+一次建箱/删箱）：建一个 `timeout=900` 的沙箱 → 不发任何请求 → 6 分钟内 `GET /sandboxes/<id>` 必须看到 `state: paused`、`pausedAt` 非空；再 `POST /sandboxes/<id>/connect` → `state: running`；最后 `DELETE`，两节点预约回到 `0/0`。
3. 反向对照：`deploy/scripts/acceptance/probe_stream_keepalive.py`（**本轮已把它从 tmp 固化进仓库**）——150 s 静默 hold 期间状态必须始终 `running` 且 `lastActiveAt` 至少动 3 次（`0.1.0-965` 现场：动 7 次）。
4. 三条读数回填 `docs/deploy-clusters.md` 新 §7.36 与 `docs/open-issues.md`（N61 行的"下一步"改写成本版的实测结论）。

## 本期明确不做

- 跨节点恢复（语义上是"从快照重建"，丢内存态，单独立项）；resume 抢占同节点闲置沙箱（第一期只答具名 503）。
- 暂停期间"保留恢复余地"的配额策略（暂停就是把配额还回去，这是 E9.2 的既有语义）。
- 暂停态的内存/进程搬迁（CRIU 类能力）。
