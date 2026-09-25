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

> **空闲的对外口径（2026-09-25 起）**：**没有经过平台的请求**（见下表）**且没在烧 CPU**。
> CPU 这一半是 §6 的选项 i 补上的（worker 每 5 s 做一次 `/proc` 走查、按**属主 uid** 汇总
> `utime+stime`，超过**一个核的 5%** 即算活动）；沙箱自发 egress 与沙箱互访**仍不算**
> （那是选项 ii，见 §6）。另有一条要知道的滞后：`last_active_at` 落进共享 store 最多要等
> `E2B_ACTIVITY_PERSIST_INTERVAL_S`（默认 30 s），而**驱逐候选正是从这份 store 视图读的** ——
> 调小空闲阈值时要把它一并算进去。

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

冻结作用于 `ProcessManager` 里各命令子进程组（exec child 自带组，F1.7），
**MCP 网关进程本身不在暂停范围**（它是长驻进程，冻结它会把 SDK 的 MCP 通路
一起掐掉）。paused 态下的可达性在 2026-09-18（N28/A）收窄为：**读可达、写与
新 exec 一律被拒**——`process.Process/Start` 与 `MakeDir`/`Move`/`Remove` 回
`failed_precondition`，HTTP 写端点回 409，`Stat`/`ListDir`/`GET /files` 正常。
理由与实现见 `docs/disk-accounting-dirty-dirs.md` §13。

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
4. **空闲判定可靠性（E9.1 后的剩余边界，2026-09-25 更新）**：活动信号 =
   「经过 envd/控制面鉴权层的请求」（见 §3.1 来源表）**+ worker 的 CPU 采样**
   （§6 选项 i，已上线并集群验收）。因此**仍然**会被判为空闲的只剩两类：
   沙箱内部进程自己的**出站**流量（egress on-behalf 由 supervisor 代发，不经 envd）、
   以及沙箱内服务之间的互访。**纯 CPU 型长任务已经不算了**；但纯内存型（既不发请求、
   也没什么 CPU）仍会被判空闲 —— 它不在选项 i 的覆盖里，要连网络一起才算，那是选项 ii。
   兜底手段仍是把这类沙箱建成高 `priority`（或调大 `E2B_SANDBOX_IDLE_THRESHOLD_S`、
   对关键租户关闭驱逐）。TTL 仍是独立的时间兜底，与空闲判定互不影响。
5. **pause 语义**：pause 释放配额后现场保留的实现（进程冻结 vs 停止+
   恢复），与 sandlock 能力相关。

---

## 6. 2026-09-22 复核：空闲判定的边界、现在的旋钮、以及要不要补采样

上面 §3.1 那条"剩余边界"在实现落地之后（E9.1–E9.4）仍然成立，这里是复核过的现状与账。

**活动信号今天有两个来源**（都在代码里可查）：

* **控制面侧**：只有生命周期/变更类端点会 `_mark_active`（connect、timeout、pause、resume、
  改网络），**只读轮询（info/metrics/logs）与内部端点（reconcile、列沙箱）刻意不算** ——
  否则"有人在看"就等于"有人在用"（`control_plane/api/sandboxes.py::_mark_active` 的注释），
  一个被监控轮询的空沙箱将永远踢不掉；
* **worker 侧**：每个沙箱**经过 envd 鉴权层**的请求计数，随心跳的 `sandboxActivity` 上报
  （`envd_service/agent.py` 的 `_activity_provider` → `internal.py` 的
  `apply_activity_report`）。

**因此这三类仍然会被判成空闲**（会走驱逐：pause + 释放预留）：

1. 沙箱内部进程**自己的出站流量**（egress on-behalf：supervisor 代发，从不经过 envd）；
2. **纯 CPU/内存型长任务**（没有请求，也没有 egress）；
3. **沙箱之间的互访**（不经过 envd）。

**现有旋钮与它们的真实强度**（别把它们当"免疫"）：

| 旋钮 | 真实语义 |
|---|---|
| `E2B_SANDBOX_IDLE_THRESHOLD_S`（默认 300） | 大于多少秒没活动就算空闲；**设 0 等于关掉整条判定** |
| `E2B_EVICTION_ENABLED` | 总开关（关掉 = 容量不足直接 503/排队，不驱逐） |
| `priority` | 只决定**顺序**（低优先先被踢）——它不是豁免：候选不够时高优先也会被踢 |
| 租户维度 | 默认只踢请求者**自己租户**的空闲沙箱（`eviction_cross_tenant` 才跨租户，防止"建箱即踢别人"的 DoS） |

**要不要补采样（这是需要定的那条）**：

* **选项 i（便宜）**：worker 已经在跑周期任务（disk scan / reconcile），顺手采**每沙箱的 CPU
  时间增量**（cgroup v2 `cpu.stat` 的 `usage_usec` 差，或该沙箱进程组的 `/proc/<pid>/stat`
  utime+stime 差），把"这段时间确实在烧 CPU"并进 `sandboxActivity` 上报。
  覆盖盲点 2；代价小，不需要新通道。
* **选项 ii（完整）**：再加上**每沙箱的网络计数**（route-B 槽位的 netns 里
  `/sys/class/net/*/statistics`，或按 pid 聚合），覆盖盲点 1 与 3。代价是要给每个沙箱定位网络命名
  空间（route-B 槽位天然有；route-A/in-process 形态没有独立 netns，只能按 pid 聚合，精度差）。
* **选项 iii（不补）**：接受现状，把上面这张表写进对外文档（"空闲 = 没有经过平台的请求"），
  并建议长任务型沙箱调高阈值或提高 priority。

**决定（2026-09-23，用户）：要补采样。** 按 **i → ii** 的顺序做，**iii 的文档部分照写**：

1. ✅ **已做 i（每沙箱 CPU 时间增量，2026-09-25）**：worker 的周期任务里顺手采（cgroup v2 `cpu.stat` 的
   `usage_usec` 差，或该沙箱进程组的 `/proc/<pid>/stat` utime+stime 差），把"这段时间确实在
  烧 CPU"并进 `sandboxActivity` 上报；控制面把"CPU 增量 > 阈值"也算活动。
  验收：一个**不发任何请求、只在循环里烧 CPU** 的沙箱在阈值窗口内**不被判空闲**（今天的
   行为是会被 pause + 释放预留），而一个真正什么都没做的沙箱**仍然**会被判空闲
   （不能把判定变成"永不空闲"）。
   **实现（2026-09-25）**：`envd_service/runtime/cpu_activity.py` + `agent.py` 的一个独立
   周期任务（与磁盘轮同形，不挂在心跳上）：

   * **一次 `/proc` 走查**，按进程的**属主 uid** 汇总 `utime+stime`（E3.2 之下 slot 就是沙箱
     的进程树、跑在它的池 uid 上，所以不需要按沙箱走树）；没有池 uid 的形态
     （非 root worker / 共享 uid）**故意不采**——那里的 CPU 分不出是沙箱的还是 worker 自己的，
     猜一个数就等于让驱逐去信一个假信号；
   * **阈值是"一个核的百分比"**（`E2B_CPU_ACTIVITY_PERCENT`，默认 5，按采样窗口平均；
     `E2B_CPU_ACTIVITY_INTERVAL_S` 默认 5 s，0 = 关掉）。"有增量就算活动"是错的：一分钟醒一次
     的进程也有增量，那样**没有沙箱可被驱逐**；
   * 超过阈值的沙箱调**同一个** `mark_active`（worker 侧 registry），于是走的是**同一条**
     `sandboxActivity` 心跳与 `apply_activity_report` 合并路径 —— 不加新字段、控制面不加第二个
     阈值。`E2B_CPU_TRACE=1` 打一行每轮的采样摘要（排障用）。

   验收（单测，`tests/unit/test_cpu_activity.py` + `test_eviction_selector.py`）：
   `/proc` 汇总按 uid 精确；阈值边界（0.5 s/5 s = 10% 算活动、无增量不算、uid 回绕不算）；
   烧 CPU 的沙箱被 mark、**闲的 / 没有池 uid 的 / paused 的都不会**；
   并且把这条 mark 走完 `apply_activity_report` 之后，它**不在** `eviction_candidates` 里，
   而什么都没做的那个**还在** —— 正是这条决策的验收。

   **集群验收（2026-09-25，自建 k0s 两节点，`0.1.0-527-g946daa9`，脚本
   `tmp/k0s/cpu_activity_acceptance.py`）**：两个沙箱 —— A 只烧 CPU（一条 `commands.run` 之后
   彻底静默），B 建完就不碰；观测值取自 `GET /sandboxes`（**读不算活动**，故意选的）。
   因为 `lastActiveAt` 读的是**共享 store 里的值**，而活动在内存里只前进、最多每
   `E2B_ACTIVITY_PERSIST_INTERVAL_S`（默认 30 s，本集群未设）才穿透一次，所以窗口必须长于它，
   并且**第一段窗口只用来取基线**（A 自己那条 `commands.run` 也是活动，会推动它）：

   | 沙箱 | 建箱 | 基线（静默 45 s 后） | 再静默 45 s 后 |
   |---|---|---|---|
   | A：`exec python3 -c 'while True: pass'` | 14:05:23.041 | 14:05:47.699 | **14:06:47.715（又前进）** |
   | B：建完不动 | 14:05:23.675 | 14:05:23.675 | **14:05:23.675（不动）** |

   第二段窗口里脚本只做了读，所以 A 的这次前进只可能来自 CPU 采样 —— 这正是"CPU 型长任务
   不被判空闲"的验收；B 不动则是另一半（不能把判定变成"永不空闲"）。
   采样器本身也在真机上单独量过：worker 的 `/proc` 里沙箱进程属于池 uid（`10001`），两次采样
   相隔 6 s ⇒ `percents={"10001": 100.2}`、`busy=[10001]`。

   ⚠️ **可观察语义**：`GET /sandboxes` 的 `lastActiveAt` 最坏落后真实活动一个
   `E2B_ACTIVITY_PERSIST_INTERVAL_S`；`eviction_candidates` 也是从同一份 store 视图读的
   （`control_plane/registry/manager.py::eviction_candidates` → `list()`），所以驱逐看到的
   活动时间同样最坏滞后这么多。默认空闲阈值 300 s 之下这是 10%，可接受；**调小空闲阈值时
   要一并考虑它**。
   **已知缺口（有意）**：这套采样挂在 NodeAgent 的循环上，所以只有"分离形态"（worker 有自己的
   heartbeat 循环）会采；combined 形态（控制面与 worker 同进程）今天不采 —— 它本来也不是
  产能形态，且它的空闲判定与驱逐在同一个进程里，等真有人用再补。
2. **ii（每沙箱网络计数）留到真有人用纯 egress / 沙箱互访型长任务时**：那时才需要给每个
   沙箱定位网络命名空间（route-B 槽位天然有；route-A/in-process 只能按 pid 聚合，精度差）。
3. **不论做到哪一步，都把语义写进对外文档**：空闲 = "没有经过平台的请求 + 没有在烧 CPU
   （做完 i 之后）"，长任务型沙箱仍建议调高阈值或提高 priority。
   **已写入** §3.1 的"空闲的对外口径"与 §4 第 4 条（2026-09-25）。

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

- **已修（M4 整箱实例化 + D6 默认上调）：配额按沙箱记账，不是按实例**。M4 D1–D3 起每个
  沙箱只持有一个 `SandboxInstance`，命令与 MCP 网关都以 `exec` 进同一实例 ⇒
  `max_memory` / `max_processes` / `max_cpu` 是**整箱预算**
  （`envd_service/executors/sandlock.py` 模块头），同沙箱 K 个并发命令共享一份、不再各拿
  一份；节点台账 `_record_quota_dims` 按沙箱预留与实际核算同维度，"实际 RSS 先于准入
  冲破节点"的超卖路径关闭。修复依据（历史实测）：按实例记账时期网关 + 一条命令各占
  450M 同时成功（900M / 标称 512M ≈ 1.76x）、同实例两 child 各 300M 只有 1 个成功
  （数据与实验编号见 `third_party/sandlock/docs/sandbox-exec-security.md` §10.2 V5）；
  同批的前置缺陷（`proc_count` 只靠阻塞 `wait4` 归还 ⇒ setsid 孤儿永久吃掉进程预算）由
  fork pidfd 权威归还（F1.4）+ init subreaper 收养（F1.5）关闭。
- **进程维度默认 64→256（M4 D6）**：整箱语义下 `max_processes` 就是整箱进程预算上限，
  `E2B_DEFAULT_MAX_PROCESSES` 默认 256，与 fork 出厂整箱默认对齐；节点容量口径见
  `docs/SCALING.md` §3（`total_processes` 默认 2048 ÷ 256 = 每节点 8 个标准沙箱）。

**M4 关闭（2026-09-06，§3.8 / FUP-E3）**：执行边界 = 产品边界——同沙箱 K 个并发命令 +
网关共享一只 exec-only 实例的整箱预算，实例化前"K 份各自配额"的超卖形态已由构造消除；
FUP-E3 sibling-exec 断言（`tests/contract/test_memory_quota_boxed.py`）落成真档。
fork-blocked 边界登记在 `docs/task-backlog.md`「M4 收口后的 open follow-ups」：fork F11
（多线程 MCP gateway/uvicorn 进程进入实例后，后续 exec 触发 argv-safety 冻结
EPERM——网关+命令同实例变体依赖其修复，探针 `tmp/task8_fup3_probe.py`）。网关 ledger
headroom 已关闭在 E2B 侧（FUP #3）：默认箱从 512 MiB 提到 1 GiB
（`E2B_DEFAULT_MEMORY_MB`），fork 逻辑未改动。


## 9. 排期

资源管理层增强，优先级低于安全修复（E1）与磁盘配额（E2）。排期见
`docs/task-backlog.md`（新增项）。
