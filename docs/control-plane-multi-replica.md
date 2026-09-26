# 控制面多副本：账单、两条路，以及"要不要"

**2026-09-22。决策文档，不含代码改动。** 对应 backlog 的 **F11**（节点注册表在进程内 ⇒
控制面必须单副本）与 **F5**（共享存储的锁必须跨节点）。注意区分：**多 worker 早就成立**
（N13，2026-09-17 集群实测：4 个沙箱 2+2 跨两节点、宿主 uid 互不相同、重启一个 worker 不会
动到另一边的活树），这里说的是**控制面自己开多副本**。

## 1. 今天为什么是 1 副本（不是保守，是有证据）

F11 的实测：两个副本对**同一个节点**同时给出相反的结论 —— 副本 A 连续 12 次报
`fxf2j: unhealthy`，副本 B 同时报 `healthy`。因为心跳被 Service 轮询，而节点视图在内存里：
每个副本只知道自己收到的那几次心跳。后果不是"视图不新鲜"这么轻：

* `/internal/routes` 在过时副本上回 **502 Node ... unavailable**；
* **放置**只在它以为有容量的节点上发生；
* **E6.1 的孤儿判定**会把"它以为不健康"的节点上的**活沙箱**标成 orphaned（冒烟里那条
  `404 Sandbox ... not found` 就是这么来的）。

所以 1 副本是一条**结论**：`control-plane` 回到 1 副本、与 compose 生产栈一致，并有用例钉住。

## 2. 要开多副本，账单上有什么

按"状态放在哪"分三类（`control_plane/app.py` 里能一次看全）：

| 状态 | 今天在哪 | 多副本下的后果 | 需要的动作 |
|---|---|---|---|
| 沙箱记录、配额台账、uid 账本 | **Redis**（`RedisRecordStore`/`RedisQuotaStore`/`RedisUidLedger`） | 已共享 | 无 |
| **节点视图**（健康、地址、预留） | `NodeRegistry._nodes`（内存 dict） | F11 的全部症状 | **必须共享**：心跳/注册写 Redis（含 TTL），`/internal/routes`、放置、健康扫描都从它读；健康扫描改成"分布式单飞"（一个副本扫，或带锁扫） |
| 快照记录 + payload | 文件（共享 base 的 `_snapshots/`，控制面 RW `subPath` 挂回）+ 进程内缓存 | 缓存各自一份（可接受）；**记录写在共享目录**所以要防同时写 | 记录写已是"写文件 + 内存缓存"；多副本要加**文件锁或 Redis 记录** |
| 每 id 的拷贝锁、异步拷贝任务 | `_SNAPSHOT_LOCKS` + `_PENDING_CAPTURES`（进程内，N29 异步形态引入） | 同键并发落在两个副本 ⇒ **可能拷两份** | 换 Redis 锁；或把"谁在拷"写进记录并在启动/轮询时对齐（async 的 `creating` 记录天然可做这个） |
| 建箱等待队列 `CreateQueue` | 进程内事件 | 容量释放的通知只到本副本 ⇒ 别的副本上的等待者超时 | 换 Redis 发布/订阅（或轮询 Redis 台账） |
| 7 个 `SlidingWindowRateLimiter` + `recent_failures` 计数器 | 进程内 | 限流被**放大到 N 倍**（每个副本各自计数） | 换共享计数器（Redis INCR + 窗口），或接受"限流按副本数放大"并写进文档 |
| `TTLSweeper`、节点健康扫描 | 每个副本各跑一份 | 并发回收同一批（多数幂等，但会有重复的 502/日志；健康扫描会各自孤儿判定） | 单飞/加锁；**健康扫描那条正是 F11 的核心** |
| `template_build_slots` + 其锁 | 进程内 | 模板构建并发上限被放大 | 换 Redis 槽位 |

**存储侧的前置（F5）**：uid 池的互斥靠共享 base 上的 `flock`，而阿里云 NAS 上**只有
`vers=4.0` 跨节点真互斥**（v3+服务端锁直接 `ESTALE`；v3+`nolock` 只是本地锁）。这条与
worker 多副本共用 —— 也就是说它是**已经满足**的前置（PV 用 4.0），但它同时是"任何依赖
共享文件锁的协调"的天花板：跨节点锁成立，跨节点**没有**别的原子原语（没有 compare-and-swap），
所以"分布式单飞"要靠锁 + 记录，而不是内存里的 flag。

## 3. 两条路

**路 A：把节点视图搬进 Redis（+ 上面那张表里的其余项）**
最小可用集是**节点视图 + 健康扫描单飞**（这两条一解决，F11 的三个后果就没了）；其余项
（限流放大、队列跨副本、快照锁）是"多副本下语义变差"但不会破坏正确性的项，可以分两步。
*代价*：一次真实的共享状态改造（含契约测试）；收益是控制面能做无中断滚动（今天的
`maxUnavailable: 25%` 在 1 副本下等于**先停后起**）。

**路 B：维持 1 副本，把容量问题留在 worker 侧**
worker 已经能多副本（2/2，autoscaler MIN=2/MAX=16），控制面是薄层：它做的是记账、放置、
限流与转发，实测 CPU 很低。1 副本的**真实代价**只有两个：滚动升级有中断窗口、
单副本挂了控制面就没了（数据在 Redis/共享盘上，所以重启即可恢复）。
*代价*：接受这两个代价，并把"为什么是 1"写进清单注释（现在只在 F11 里）。

## 4. 建议

**先按路 B 记录，把路 A 的触发条件写死**：控制面 CPU 成为瓶颈、或需要无中断滚动、
或单副本可用性不再够（对外 SLA）。触发时按第 2 节表里的**最小可用集**做（节点视图 +
健康扫描单飞 + 快照锁），其余项同批或紧随其后 —— 因为任何一项留下，都会以"多副本下
语义变差"的形式重新出现在同一个 F11 的形状里。

## 5. 决定（2026-09-23，用户）：**需要多副本**，走路 A

实施顺序（每一步都能独立验收，前两步做完 F11 的三个后果就消失）：

1. **节点视图进 Redis**：注册/心跳写 Redis（含 TTL），`/internal/routes`、放置、
   健康扫描、`/internal/nodes` 全从它读；内存 dict 退化成缓存。
   验收：同一个读路径分别问两个副本，**不可能**同时得到 `healthy` / `unhealthy`。
2. **健康扫描单飞**：一个副本扫（抢共享锁），或每个副本扫描但只在"节点在**所有**副本的
   视图里都过期"时才判（前者简单，选前者）。验收：两副本舰队里 4 沙箱的冒烟中
   **不出现** `404 Sandbox ... not found` 与 `Node ... unavailable`（F11 当年的判据）。
3. **快照锁 + 异步拷贝登记共享**：N29 的每 id 锁与 `_PENDING_CAPTURES` 现在是进程内的，
   同键并发落在两个副本会拷两份。做法：把"谁在拷"写进**记录**（异步形态的 `creating`
   状态天然是这个），锁换 Redis；同步形态判断"记录存在且 creating ⇒ 等/轮询"。
4. **同批或紧随其后**（多副本下语义变差但不破坏正确性）：`CreateQueue` 换 Redis 通知、
   7 个限流器换共享计数、`template_build_slots` 换 Redis 槽位、`TTLSweeper` 单飞。

前置（F5，已满足但要知道它的天花板）：共享存储的锁**只有 NFSv4.0 跨节点真互斥**
（v3+`nolock` 只是本地锁），而跨节点**没有** compare-and-swap —— 所以 1/2/3 步都要靠
"锁 + 记录"来做单飞，不能靠内存 flag。

## 6. 实施记录（2026-09-26）：第 1–3 步已落地

**第 1 步：节点视图进 Redis**（`control_plane/registry/redis_backend.py::RedisNodeStore` +
`nodes.py`）。注册、心跳、排空、预留、用量全部写进共享视图，`get`/`list`、
`_placeable_candidates_locked`（放置）、`reap_unhealthy`（健康扫描）、`/internal/routes`、
`/internal/nodes` 一律从它读；进程内 dict 退化成缓存（没有 Redis 时才当权威）。两个要点：

* **健康是"算"出来的，不是"存"出来的**：`_status_of` 用共享的 `heartbeat_at` 与同一超时，
  在每个读路径现算 ⇒ 两个副本对同一节点不可能给出不同答案（这正是 F11 的验收句）。
  视图 TTL = `4 × heartbeat_timeout`：worker 真没了，行会在**同一时刻**从所有副本消失。
* **`local://` 故意不进共享视图**：它是**本副本**内嵌的 worker，发布出去只会让别的副本把活
  派到一个它够不着的 worker，而且两个副本都会写 `local` 这一行。

**第 2 步：健康扫描单飞**（`NodeRegistry.try_acquire_sweep` + `_node_health_loop`）：每轮一个
`SETNX + EX`（TTL = 扫描间隔），抢到的那个副本扫；抢不到就跳过。视图共享之后重复扫描是重复
**劳动**（以及重复的 `orphaned sandboxes on …` 告警），不是额外覆盖。没有 Redis 时单进程即
sweeper。

**第 3 步：快照拷贝登记共享**（`SnapshotRegistry.try_acquire_copy` / `release_copy` +
`api/snapshots.py`）：

* **记录是持久的那一半**：`creating` 状态写在共享卷上的 `snapshot.json` 里，每个副本都读得到；
* **Redis 认领是补窗口的那一半**：记录挡不住"两个副本都还没写完记录"的那一小段，
  `try_acquire_copy` 用 `SET NX EX`（TTL 600 s，远大于任何一次拷贝）把这段关掉；输的一方
  等赢家的**记录**（轮询 2 s）并照 N29 的老语义回 `alreadyExists`（还在拷 = 202），
  等不到才 409 说明是谁在拷；
* **`get()` 不再对 `creating` 使用缓存**：`creating` 是唯一会被**另一个副本**改掉的状态，
  按旧实现轮询打到"错"副本会永远看不到完成；完成的记录照旧走缓存（热路径不变）。

**验收（在容器里跑，`fakeredis` 2.37.1）**：`tests/unit/test_redis_multireplica.py` 新增 11 条
（视图共享、心跳落地、两副本同判健康、TTL、`local` 不外发、用量随视图、单飞扫描、
快照认领/过期/无 Redis、`creating` 重读），加上 `test_snapshot_registry.py`、
`test_registry_dirty_snapshot.py`、`test_node_partition_reconcile.py`、`tests/contract/test_snapshots.py`
全绿；本机 `tests/unit` 仍是 16 条既有 macOS 红 / 1164 passed，`tests/contract` 321 passed。

**第 4 步仍未做**（明确保持"多副本语义变差但不破坏正确性"的排序）：`CreateQueue` 的跨副本
唤醒、7 个限流器换共享计数、`template_build_slots` 换 Redis 槽位、`TTLSweeper` 单飞。
四者的共同形态与第 2 步相同（TTL'd claim 或共享计数器），`RedisNodeStore`/`try_acquire_sweep`
是现成的模板。
