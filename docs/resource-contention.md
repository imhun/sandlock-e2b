# 资源争用与沙箱驱逐机制（设计方案）

## 1. 背景与现状

场景：活跃沙箱已占满资源，但存在空闲沙箱——需要"驱逐空闲的、给新沙箱
腾资源"，避免新创建直接失败。

当前机制（代码事实）：

| 机制 | 现状 |
|---|---|
| 准入 | 预留配额制：创建时校验全局+节点配额，不足 → `503 No resources available`（`manager.py:495`） |
| 回收 | 仅 TTL 到期：`TTLSweeper` 定期 `remove_expired` 清理到期沙箱并释放配额（`manager.py:655`）——时间驱动，非资源驱动 |
| pause/resume | 只改 state，**不释放资源配额**（`manager.py:110`） |
| 创建失败 | 驱逐（E9.3）后仍无容量时排队等释放（E9.4），超时才 503 |
| 优先级/驱逐 | 无 |

**结论**：没有"资源紧张时驱逐空闲沙箱"的路径；空闲沙箱占着配额直到
TTL 到期或被显式 kill。

## 2. 目标

资源争用时：

1. 新沙箱创建不再直接 503，而是有机会通过"驱逐空闲沙箱"获得资源；
2. 被驱逐的沙箱用户收到明确通知（不静默消失）；
3. 可配置策略（优先级 / 空闲阈值 / 是否允许驱逐）。

## 3. 设计方案

### 3.1 空闲检测

E9.1 已实现。`SandboxRecord` 增加 `last_active_at`（tz-aware，**只前进不回退**）
与 `priority`（0–10，默认 5），随记录持久化；空闲定义
`now - last_active_at > E2B_SANDBOX_IDLE_THRESHOLD_S`（默认 300s，`0` = 永不空闲）。

**算活动的来源**（两处，缺一不可）：

| 来源 | 采集点 | 送达控制面的路径 |
|---|---|---|
| worker 侧：任何通过 envd 鉴权的请求（Connect 进程/文件 RPC、`/files`、`/envs` 等 HTTP、`/mcp` 代理） | `envd_service/http/auth.py::require_http_sandbox`、`envd_service/connect/router.py::_find_sandbox`、`envd_service/http/mcp.py`（该路由自带鉴权，故单独打点） | `RuntimeRegistry.mark_active`（10s 合并）→ 心跳 `sandboxActivity` → `POST /internal/nodes/{id}/heartbeat` → `apply_activity_report`；组合部署（控制面+worker 同进程）走 `add_activity_callback` |
| 控制面侧：生命周期/变更类调用 | `connect` / `timeout` / `pause` / `resume` / `PUT network` | `SandboxRegistry.mark_active`（直改记录） |

**故意不算活动**：只读轮询（`GET /sandboxes/{id}`、`/metrics`、`/logs`）与内部端点
（reconcile、节点沙箱清单）——否则一个监控轮询循环或控制面自身的对账就能让空闲沙箱
永远逃过驱逐。落库按 `E2B_ACTIVITY_PERSIST_INTERVAL_S`（默认 30s）节流，
因为空闲阈值本身是分钟级精度。

### 3.2 分层资源池（可选更优雅）

- **预留池**：保证已创建沙箱的资源（现状逻辑）；
- **超额池**：允许新沙箱在"存在可驱逐的空闲沙箱"时短暂超卖进入；
- 创建流程：先试预留池 → 不足时评估超额池（可驱逐目标存在则允许，
  驱逐后再确认资源）。

### 3.3 驱逐策略（资源紧张时）

最终实现（E9.3；用户决策 2026-09-01：驱逐**默认开启**）：

创建沙箱在容量准入失败（`select_node` 无节点，或全局/租户配额不足）时，
控制面按以下顺序驱逐**空闲**（`running` 且超过 `E2B_SANDBOX_IDLE_THRESHOLD_S`
未活跃）沙箱后重试：

1. **优先级**：沙箱创建时带 `priority`（如 0-10，默认 5）；先踢低优先级；
2. **空闲最久**：同优先级内踢 `last_active_at` 最旧的；
3. **租户权重**：配置了 `tenant_limits[tenant].max_sandboxes` 的租户先踢，
   配额小者更先；未配限租户/无租户记录排最后；
4. `sandbox_id` 兜底保证确定性。

候选过滤：

- 只有 `state == "running"` 且 `is_idle(...)` 的记录可被选；`paused`（已无
  配额、现场保留）与 `orphaned`（E6.1 节点失联）永不参与。
- **跨租户保护**：默认（`E2B_EVICTION_CROSS_TENANT=false`）候选只限请求者
  自己的租户；admin key 或显式开启开关才允许踢别人的沙箱——否则任何租户都
  能用“创建沙箱”把别的租户空闲沙箱全踢掉（DoS）。
- 防风暴：单次创建最多驱逐 `E2B_EVICTION_MAX_PER_CREATE`（默认 3）个受害者；
  驱逐轮次间隔不得小于 `E2B_EVICTION_MIN_INTERVAL_S`（默认 1 秒，控制面进程内
  节流）。**多副本不共享节流**（已知限制，见 §8）。

驱逐动作：

- **kill**（默认）：删除记录前写
  `append_log("sandbox evicted (reason=evicted-idle)")` 并存驱逐通知
  （`sandbox_id -> {reason, at, actor}`；Redis 可用时存 Redis，TTL =
  `E2B_EVICTION_NOTICE_TTL_S` 默认 3600；否则进程内字典 + 惰性过期 + 容量
  上限）。之后 `GET /sandboxes/{id}` 返回 404，文案为
  `Sandbox <id> not found (evicted: evicted-idle)`，响应头带
  `x-e2b-eviction-reason: evicted-idle`；普通不存在的沙箱文案/响应头不变。
- **pause**（`E2B_EVICTION_PREFER_PAUSE=true`）：先 `registry.pause(victim)`
  （E9.2：释放全局/租户配额、现场保留）+ API 归还节点配额 + 冻结运行时；
  pause 仍拿不到配额才 kill 已 pause 的候选，再继续。
- 每次驱逐打
  `logger.warning("evicted sandbox %s (reason=evicted-idle, tenant=%s, priority=%s, idle=%.0fs)", ...)`，
  不静默消失。

### 3.4 pause 释放配额改造（关键前置）

E9.2 已完成：`pause` = 释放配额 + 冻结现场（状态保留）；`resume` =
重新分配资源（配额不足则失败）。“把空闲沙箱休眠腾资源”因此成立，E9.3 的
prefer-pause 直接复用它。

原文（改造前的目标描述）：

- `pause` = 释放配额 + 持久化现场（进程停止、状态保留）；
- `resume` = 重新分配资源（配额不足则失败/排队）；
- 这样"把空闲沙箱休眠腾资源"才成立（否则 pause 无意义）。

### 3.5 创建排队（E9.4 已实现）

顺序固定为：尝试准入 →（E9.3）驱逐空闲沙箱 → 仍不足 →（E9.4）排队等待
容量释放 → 超时才 503。队列按需启用（`E2B_CREATE_QUEUE_TIMEOUT_S`，默认
30s；`0` = 保持驱逐后的直接 503）：

- **等待语义**：准入失败（无节点 / 全局/租户配额不足，或驱逐后仍不足）时，
  请求进入 `CreateQueue` 等待，期间**既不占节点配额也不占 pending marker**
  （attempt 失败前已回滚）——同 id 的客户端重试不会被自己的 marker 卡住，
  且仍可命中 `registry.get()` 短路返回既有沙箱；
- **唤醒**：`SandboxRegistry.release_quota` 真正归还配额后触发
  `add_on_quota_released` 钩子 → `CreateQueue.notify_capacity()`（线程安全，
  `loop.call_soon_threadsafe`），等待者被唤醒后重跑完整准入（每次都是原子
  准入，因此不会超卖）；另有一个上限 `min(1s, 剩余超时)` 的兜底 tick，
  防止唤醒信号丢失导致白等整个超时窗口；
- **超时 / 上限**：超过 `E2B_CREATE_QUEUE_TIMEOUT_S` → 原 503
  `No resources available`（`recent_failures.record()` 语义不变）；
  并发排队数达到 `E2B_CREATE_QUEUE_MAX` → 立即 429
  `Sandbox create queue is full` + `retry-after: 1`（429 同样计入
  `recent_failures`：能走到队列满说明池子确实饱和，autoscaler 需要这个信号）；

> 运维提示：默认 30s 意味着“满池”时 `POST /sandboxes` 最长挂起 30s 才拿
> 到 503。若客户端/网关的读超时更短，会把“排队”变成客户端超时——那种
> 部署应显式设 `E2B_CREATE_QUEUE_TIMEOUT_S=0`（回到驱逐后立即 503）或把
> 该值调到客户端超时之下。

- **不保证顺序公平性**：多副本各自排队（已知限制，见 §8），单副本内也是
  release 广播唤醒 + 竞速准入，没有 FIFO 承诺。

## 4. 与现有机制集成

| 机制 | 关系 |
|---|---|
| TTL | 驱逐是"资源驱动的提前回收"，TTL 是"时间驱动的兜底"；驱逐不影响 TTL 到期逻辑 |
| pause/resume | E9.2 已完成配额释放语义；E9.3 prefer-pause 复用（§3.4） |
| 配额（XFS project quota / 全局池） | 驱逐释放的是准入配额；磁盘配额由 XFS 独立生效 |
| 租户隔离 | 驱逐可感知租户（优先级/权重），但基础机制与租户无关 |
| `recent_failures` | 创建排队后仍保留计数（超时失败计入） |

## 5. 配置项

```env
E2B_SANDBOX_IDLE_THRESHOLD_S=300     # 空闲判定阈值
E2B_EVICTION_ENABLED=true            # 驱逐总开关（用户决策：默认开启）
E2B_EVICTION_PREFER_PAUSE=false      # true = 先 pause（保留现场）再 kill
E2B_EVICTION_MAX_PER_CREATE=3        # 单次创建最多驱逐受害者数（防风暴）
E2B_EVICTION_MIN_INTERVAL_S=1        # 驱逐轮次最小间隔（进程内节流）
E2B_EVICTION_NOTICE_TTL_S=3600       # 驱逐通知可查窗口（Redis TTL/惰性过期）
E2B_EVICTION_CROSS_TENANT=false      # 跨租户驱逐开关（安全默认关；admin 放行）
E2B_CREATE_QUEUE_TIMEOUT_S=30        # 创建排队超时（0 = 不排队，驱逐后直接 503）
E2B_CREATE_QUEUE_MAX=100             # 排队上限（满 → 429 + retry-after）
```

## 6. 实施步骤

1. `SandboxRecord` 增加 `last_active_at`、`priority`，命令/API 更新活跃时间；
2. pause/resume 配额释放改造（§3.4，前置）；
3. 驱逐选择器（优先级 + 空闲最久 + 租户权重）+ kill/pause 动作 + 通知；
4. 创建流程集成（分层池或排队）；
5. 配置项 + 单测。

## 7. 测试矩阵

| 用例 | 预期 |
|---|---|
| 资源满 + 存在空闲低优先级沙箱 | 新沙箱创建成功，空闲沙箱被驱逐（带通知） |
| 资源满 + 全部活跃 | 新沙箱 503（或排队超时） |
| 优先级：高优先级活跃 vs 低优先级空闲 | 踢低优先级 |
| 同优先级多空闲 | 踢最久未活跃 |
| pause 释放配额后 resume | 配额重新分配，不足时失败/排队 |
| 驱逐通知 | 被驱逐沙箱用户收到明确事件 |
| 排队创建 | 资源释放后自动补建，超时失败 |

## 8. 风险与开放问题

1. **驱逐的数据丢失**：kill 空闲沙箱丢现场——默认 kill 前是否有
   "快照/保存"策略（可选：驱逐前自动快照）；
2. **驱逐风暴**：批量创建时连续驱逐多个空闲沙箱——需节流（每次驱逐
   间隔/每轮上限）；
3. **与用户预期冲突**：用户可能不期望沙箱被自动踢——驱逐需默认关闭或
   配置显式开启；**用户决策 2026-09-01：改为默认开启**
   （`E2B_EVICTION_ENABLED` 默认 `true`），用空闲阈值 + 优先级 + 通知兜底；
4. **空闲判定可靠性（E9.1 后的剩余边界）**：活动信号来自「经过 envd/控制面
   鉴权层的请求」（见 §3.1 来源表），因此这些仍然会被判为空闲：
   沙箱内部进程自己的**出站**流量（egress on-behalf 由 supervisor 代发，不经
   envd）、纯 CPU/内存型长任务、以及沙箱内服务之间的互访。
   现阶段的兜底手段是把这类沙箱建成高 `priority`（或调大
   `E2B_SANDBOX_IDLE_THRESHOLD_S`、对关键租户关闭驱逐）；若要彻底解决，需
   worker 侧采到进程/连接级活跃（cgroup 或 `/proc/<pid>` 采样）再随心跳上报，
   本期未做。TTL 仍是独立的时间兜底，与空闲判定互不影响。
5. **pause 语义**：pause 释放配额后现场保留的实现（进程冻结 vs 停止+
   恢复），与 sandlock 能力相关。

**E9.3 已实现机制对应的已知限制（与代码注释口径一致）：**

- **驱逐节流是进程内状态**：`E2B_EVICTION_MIN_INTERVAL_S` 在每个控制面副本
  内独立计数，多副本**不共享**节流；同一瞬间多个副本可能各自发起驱逐轮次
  （单副本仍受每轮上限约束）。若需全局节流，可把节流时间戳迁到 Redis
  （带 TTL 的原子 set）。

**E9.4 创建排队对应的已知限制：**

- **多副本各自排队**：`CreateQueue` 是每个控制面进程内的状态，多个副本之间
  不共享"哪些请求在等、排到第几个"；副本 A 释放的容量可能被刚好到达副本 B
  的新请求抢走（唤醒的是 A 的等待者，准入结果仍由共享配额/节点 ledger 保证
  不超卖）。如需全局队列，需把排队状态迁到 Redis。
- **无顺序公平性**：release 广播唤醒所有等待者后各自竞速准入，不保证 FIFO；
  对客户端可观察的影响仅是"谁抢到容量"不确定，不丢请求、不超卖。
- **驱逐通知表**：Redis 形态靠 key TTL 自然过期；无 Redis 的进程内字典用
  惰性过期 + 容量上限（10 000 条，超限丢最旧），不做主动扫描。TTL 过期后
  `GET /sandboxes/{id}` 回到普通 404。
- **创建期驱逐只处理“容量不足”失败**：镜像预热失败 / 428 / 其他非容量错误
  不会触发驱逐，行为与接入驱逐前一致。

- **配额按 sandlock 实例记账，不是按沙箱 ⇒ 并发命令可超卖**：`max_memory` / `max_processes` /
  `max_cpu` 由 sandlock 在每个实例的运行时状态里核算（`brk`/`mmap` 的 USER_NOTIF 记账），而 E2B
  是"每条命令一个实例"，于是同一沙箱 K 个并发命令各拿一份配额。实测 `max_memory=512M` 时
  单实例申请 600M 被拒，但 3 个并发实例各占 200M 全部成功（峰值 600M）。K≥2 是常态：
  `Sandbox.create(mcp=...)` 的网关是长驻实例，`background=True` 的命令也各持实例。
  本文件的节点台账仍按沙箱预留一次 `memory_mb`（`_record_quota_dims`），所以这会直接变成
  **节点超卖**：实际 RSS 早于准入判定冲破节点，OOM 由驱逐/扩缩信号之外的路径发生。
  磁盘不受影响（XFS project id 按沙箱目录设置、被所有实例共享，限额是真加总）。
  修法二选一：fork 提供跨实例共享资源组（`e2b-integration.md` §3.8 / 提案 P10），或
  E2B 侧给每个沙箱建一个 cgroup v2 并把每次命令的子进程放进去（需要 worker 有 cgroup 写权限）。


## 9. 排期

资源管理层增强，优先级低于安全修复（E1）与磁盘配额（E2）。排期见
`docs/task-backlog.md`（新增项）。
