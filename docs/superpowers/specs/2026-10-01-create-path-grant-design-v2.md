# 建箱材料化：控制面直送（materialize instruction）设计 v2

**状态：** 待评审。**本文件取代** `2026-10-01-create-path-grant-design.md`（载体 B：worker 领授权直连 agent）。

**一句话差异：** 建箱的材料化仍然**由 agent 做、只做一次**；改的是**谁把这条指令送到 agent** —— 从"worker 拿一次性授权直连"改成"控制面在它本来就要拨 worker 的那一步之前，自己送一次"。

**为什么换掉 v1：** 独立审查（`df3f06d..804f525`）在载体 B 上打穿了三处，其中两处是**这条新通道本身**的属性，不是实现细节：

* 控制面用 worker 身份去查"按宿主身份"的解析器（`resolve_agent` → `resolve_host`），出厂形态下每次建箱 503（已复现）；
* k8s NetworkPolicy 的 ingress 只有 `app: control-plane`，compose 的 `agent-plane` 也不含 worker ⇒ 这条通道**物理上没开**；
* 带快照的建箱把内容多下沉一层（`fs/` 是**树根**的副本，v1 却拷进 `<root>/workspace`）。

前两处只为"一个不可信携带者"而存在：授权载荷、HMAC、单次消费、TTL、worker 侧客户端、重试与降级，全都是为了让**不该持权的第三方**搬运一张票。控制面自己送，这些连同那两处 Critical 一起消失（代价见 §4.6）。

---

## 1. 现状与实测（沿用 v1 §1，未变）

| 段 | 耗时 | 说明 |
|---|---|---|
| 建箱总计（控制面 pod 内发起，n=10） | **191 ms** | 控制面自己的份额只有 ~1–3 ms |
| `provision`（worker 那一次 `POST /agent/sandboxes`） | 173 ms | 幂等重放单独量：p50 198 ms |
| ├ `fileop:chown-workspace` | **71 ms** | worker→CP→agent→CP→worker；其中约 27–30 ms 是**这一圈的固定开销**（同一条通道上"什么都不做的 walk"要 27 ms） |
| ├ `record`（`_runtime/<id>/sandbox.json` 原子写） | **47 ms** | 一次 mkdir + 写 + fsync + rename |
| ├ 其余（两次 NFS mkdir、记录查找、uid 认领、响应） | ~55 ms | |
| └ `prime`（运行时上下文） | 17 ms | |

参考值：两节点之间的**时钟差实测 ~16 ms**；集群内一次控制面往返 **1.29 ms**；NFS 元数据往返 **10–40 ms**。建箱的每一次"少一次落盘/少一跳"都值十几毫秒，方向只有这一个。

v2 新增的三条事实（本次核查得出，全文依赖它们）：

1. **控制面今天就已经在整段建箱上等待** —— `_provision_remote` 是一次 `await client.post(<worker>/agent/sandboxes, ...)`，worker 的 `copytree` 就在这个 await 里面。拆成"先 agent、后 worker"两次调用，**总等待不变**。
2. **这个等待本来就有上限** —— `app.state.remote_http = httpx.AsyncClient(timeout=60)`。超过 60 s 的建箱今天就已经失败（CP 侧先放弃并 `registry.delete`）。v2 不引入新的天花板。
3. **控制面在建箱路径上已经攥着这条指令的全部输入** —— 调度已定下 `node`；`host_uid` 由 `registry.allocate_host_uid` 分配并写进记录；`volume_mounts` 与每个卷的 path / `per_sandbox_quota_mb` 就在 `_provision_remote` 的 payload 里。**不需要 worker 告诉它任何东西**。

## 2. 目标与非目标

**目标：** 建箱期的材料化（建树、快照拷贝、改属主、卷切片）不再由 worker 自己做、改属主也不再经控制面**转发并等待**——控制面把这次要做的每一件事写成**一条**指令，直接送给该节点的 agent；worker 拿到的是一棵已经就绪的树。

**非目标（明说）：**

* **拆箱/巡检不走这条**：`rm`/`walk`/`remove-workspace`/`remove-runtime` 仍逐次经控制面（本来就是控制面在送，形状不变）。
* **不新增 `worker → agent` 通道**，也不改 worker 的能力面：worker 仍然没有 `E2B_C3_AGENT_TOKEN`。
* **不动 `local://` 形态**：`_provision_local` 在控制面进程内自己完成材料化，一行不改。
* **不改快照载荷格式**：`_snapshots/<snap>/fs` 是**树根**的副本，v2 沿用（见 §4.3.1）。

## 3. 不需要反转任何既有规则

v1 §3 要反转 `docs/c3-privilege-relocation.md` §14.1 第四行「**worker ──▶ agent：禁止**」。**v2 不碰它**：控制面本来就是 face B 的唯一调用方，v2 只是在那条通道上多一个 verb。于是：

* 清单不用改（CP→agent 本来就通，端口 49985/49986 已经对 `app: control-plane` 开放）；
* 身份解析用**既有**那条（`C3AgentClient._target` → `resolver.resolve(node_id)`，worker 键 → 宿主身份），不是 v1 那条按宿主名查的 `resolve_host`；
* §14.1 的表**一个字都不用改**，只需要在 op 词表里多一行。

## 4. 设计

### 4.1 形状

```
client ─▶ CP POST /sandboxes
   CP: 准入 → 选节点 → 建记录 → 分配 host_uid → 推导材料化计划
   CP ─▶ 该节点 agent：materialize（树 / 拷贝 / 切片 / 改属主，一条指令）
   CP ─▶ 该节点 worker：POST /agent/sandboxes {…, "materialized": true}
   worker: 认 uid、卷配额 + 挂载视图、写记录、盘上统计、预热上下文 → 201
   CP: append log、save、返回给 client
```

时序上材料化在"CP 拨 worker"**之前**完成，所以 worker 不需要知道 agent 存在，也不需要向任何人领权。两跳都是控制面本来就有的：一跳是对 agent（多出来的唯一一跳），一跳是对 worker（今天就有）。

### 4.2 agent 侧：既有通道上加一个 verb

路由沿用 `POST /internal/nodes/{node_id}/agent/{op}`（`X-Internal-Key: E2B_C3_AGENT_TOKEN`，与 `chown`/`rm`/`walk` 同一条），新增 `op = "materialize"`。body：

```json
{
  "sandbox_id": "sbx_…",
  "worker": {"uid": 65534, "gid": 65534},
  "tree": {"path": "<ws>/<id>", "subdir": "workspace", "mode": "0770",
           "uid": 10000, "gid": 65534,
           "copy_from": "<ws>/_snapshots/<snap>/fs"},
  "slices": [{"volume": "vol_…", "path": "<vol>/<id>", "uid": 10000, "gid": 65534}]
}
```

* `worker` 与 `FileOpBody.worker` **同一个模型**（uid/gid + 可选的 compose anchor），因为材料化最后那一次 `chown --uid --gid` 要过 `maint.c` 的 `--gid` 门，而那道门比的是 worker 自己的 gid（`E2B_BROKER_WORKER_GID`，见 `c3_agent/fileops.py::maint_env`）。
* `copy_from` 只在快照建箱时出现；`slices` 由记录的卷挂载列表推导（与 `build_volume_mounts` 同源）。
* **路径由控制面给**，这在这条通道上是**正常且必须的**（§14.4 硬规则二：只有控制面可以命名路径）；agent 仍然照旧独立复核一次（realpath + 自己那四根）。
* agent 侧的校验就一条，且是既有的：`node_id == settings.node_id`（"这条指令是发给我的吗"）+ 路径复核。**没有签名/时效/单次消费**——凭据就是那条 `X-Internal-Key`，与 `chown` 完全同形。

### 4.3 合成操作 `materialize-tree`（沿用 v1 §4.3）

一次调用把四件事做完：① `mkdir -p <root>/<subdir>`（mode 0770，group = worker gid）；② 有 `copy_from` 就递归拷贝；③ 每条 `--slice` 建好；④ 一次 `e2b-maint chown --uid U --gid G --recursive` 收口（树一棵，每条切片一棵）。

`c3_agent/materialize.py` 里那套实现（本分支 `8fa9b58` 的成果）**原样保留**，只改调用方与落点（§4.3.1）。

### 4.3.1 特权拷贝的硬要求（沿用 v1 §4.3.1）+ **一处必须修的落点**

v1 的四条硬要求不变，每条都要有用例：

1. **源侧**：不跟随符号链接（先判 `is_symlink`，用 `symlink()` 重建）；
2. **目标侧**：合并写入时逐段 `O_NOFOLLOW`；目标是符号链接就**具名拒绝**；
3. **失败具名**：拷到一半失败不得报成功；
4. **不许丢既有性质**：跨节点迁移"文件还在"不能被破坏。

**并且必须修一处落点错误：** `copy_from`（`<ws>/_snapshots/<snap>/fs`）是**树根**的副本 —— 快照侧 `agent_create_snapshot` 的 `src = workspace_base / sandbox_id`，控制面本地形态的 `expand_to(snapshot, workspace_dir)` 同样如此，今天 worker 的降级路 `copytree(snapshot_fs, workspace_dir)` 也回到树根。v1 把它拷进了 `<root>/<subdir>`，于是 `workspace/workspace/…`，比降级路多下沉一层（已复现）。**落点必须是 `<root>`**；`subdir` 只用于"没有快照时建出 `<root>/workspace`"。

### 4.4 worker 侧：一个 `materialized` 标志

`_provision_remote` 的 payload 多一个 `"materialized": true`。worker 侧：

* **是** ⇒ 跳过建树/拷贝段，也**跳过** `apply_sandbox_ownership`（agent 那次 `chown --recursive` 已经办完），`build_volume_mounts(..., slices_materialized=True)` 只做配额与挂载视图；
* **否/缺** ⇒ 与今天**逐字相同**（自己 mkdir/copytree + 经控制面中继 chown）。

这一条把滚动升级的矩阵塌成两端都安全，且**没有任何降级分支要写**：

* 新 CP + 旧 worker：旧 worker 不认这个键 ⇒ 自己建树（幂等，今天的行为）；
* 旧 CP + 新 worker：没有这个键 ⇒ 自己建树（今天的行为）。

v1 为这个矩阵造了 `AgentMaterializeUnsupported`、404/503 分类、一次重试与一条 WARNING，v2 全部不需要。

### 4.5 记录与标记

沿用 v1 §4.5，但把窗口说清：材料化现在发生在 worker 参与**之前**，所以：

* worker 侧的建箱标记（`<state>/_runtime/<id>/.creating`）仍然覆盖"记录写尚未落盘"的那段（`804f525` 的延迟落盘需要它）；
* 新的窗口是**"CP 已材料化、worker 还没开始"**。它的处置在 Task 5：DELETE 落在 CP 侧在建窗口内时，必须与 worker 标记同一纪律（有界等待，或具名拒绝），**不得**留下"CP 已删记录、树上却有半棵树"的组合。

### 4.6 并发：materialize 必须有自己的预算

这是 v2 唯一的实质代价，点名两条：

1. **控制面侧**：`C3AgentClient` 有一个 `E2B_C3_AGENT_MAX_CONCURRENCY`（默认 64）信号量，它的注释自己写着"它必须不低于最大的在途 slot 启动数；create 准入上限是 100，所以信号量不该是 create 第一个排队的地方"。materialize 在 create 路径上、可能占 17 s 级，**不能**排进那个 64；它要自己的预算（或明确豁免），否则它就会变成 create 的第一个排队点。
2. **agent 侧**：agent 是同步 FastAPI，handler 跑 anyio 线程池（默认 40），同时还要回答面 A 的 `grant-slot`（沙箱第一条命令要用）。v2 之后**快照拷贝也进 agent**（今天只有 chown 在），所以 materialize 必须有自己的并发上限，**且**不能把 40 个线程吃光 —— 超限时给具名的"忙"答复，而不是静默排队。

默认值由 Task 7 的实测来定，代码里先给一个保守值并留开关。

## 5. 不需要授权寿命（v1 §5 作废）

v1 花了一节论证 TTL 取 10 s（"只要够 worker 把请求发到 agent"）。v2 没有票：凭据是那条长期存在的 `X-Internal-Key`，与 `chown`/`rm`/`walk` 同形；"逐操作、实时、不可重放"由**控制面在网络位置上**保证，而不是由票据的寿命保证。

代价如实记：v1 的"泄漏出去的票据只有 10 s 且只能用一次"这条**附加**防线没有了。它换掉的是"一条新的特权通道 + 一类新的调用方"，而那条通道要求清单、身份与并发三处同时正确。

## 6. 验收矩阵

| 判据 | 怎么测 | 期望 |
|---|---|---|
| 建箱延迟 | `deploy/scripts/acceptance/create_latency_probe.py`（控制面内，n=10） | 191 ms → **P1 后 ~145–150 ms** → **P2 后 ~110–115 ms** |
| 逐段 | `E2B_CREATE_TRACE=1` + `worker_provision_cost.py` | `fileop:*` 段整段消失（worker 不再建树/拷贝/改属主），只剩控制面侧一次 `materialize`；`record` 段离开响应路径 |
| **符号链接不得逃逸** | 源侧：快照里放一个指向外部的符号链接 ⇒ 必须被**重建为符号链接**；目标侧：目标树里预置一个指向 `/etc` 的符号链接 ⇒ 必须**具名拒绝** | 两条用例都绿，且拒绝时不留半棵树 |
| **快照落点** | 生产形状的快照（`fs/workspace/kept.txt`）建箱 ⇒ 沙箱里 `workspace/kept.txt` 在 | 快路与降级路产出**同一棵树**（这条是 v1 漏掉的） |
| **迁移保文件** | `deployment_smoke.py` 的"跨节点迁移保文件" | 仍绿（合并语义没被破坏） |
| 指令负面 | 换 host 的 `node_id`、路径越界、`slices` 多一条计划外的路径 | **全部具名拒绝**，且不触达 `maint` |
| 并发饱和 | agent 侧 materialize 超上限 | 具名"忙"，且**面 A 的 `grant-slot` 不被拖住**（有界延迟） |
| 滚动升级 | 新 CP + 旧 worker、旧 CP + 新 worker | 两条方向建箱都成功；旧 worker 那条自己建树 |
| CP 侧窗口 | 材料化完成、worker 尚未被拨到之间发 DELETE | 0 残留（没有"记录在、树没了"或"记录没了、树还在"的组合） |
| 未回归 | `MULTI-NODE`/`DEPLOYMENT` 冒烟、`APPLY DRY_RUN` diff 0 行 | 全绿 |

## 7. 记录在案的取舍

### 7.1 载体 B（worker 领授权直连）—— 已否决

否决依据是三处实测/核查（见文件开头）：出厂形态下 503（身份查反）、清单不放行、快照落点错。前两处是**这条通道的属性**：它给面 B 添了一类调用方，于是要求清单、身份解析、并发三处同时正确；而它为延迟省下的只是一个同节点跳（亚毫秒）加一次领权往返（1.29 ms 量级）—— 与 v2 的跨节点一跳大致打平。为这点收益背一条新的特权通道，不值。

**保留自 v1 的部分：** §4.3/§4.3.1 的合成 op 与那四条硬要求、`c3_agent/materialize.py` 的硬化实现、§4.5 的标记与延迟落盘。那部分是真正难写的部分，且与"谁送指令"无关。

### 7.2 载体 A（授权随 payload 预发）—— 仍然否决

理由同 v1 §7.1：TTL 必须覆盖整段建箱（含无上限的快照拷贝），且"授权在使用之前就存在"这条叙事更弱。

### 7.3 "worker 一开头就发 op、agent 等就绪信号" —— 仍然否决

实测理由同 v1 §7.2：就绪信号是一次 NFS 写（12 ms）+ 一次删（9 ms），而集群内一次控制面往返只要 1.29 ms。

### 7.4 让指标不掉的另一条：把材料化整段挪进 `local://` —— 不做

控制面进程内做材料化只对"控制面就是 worker"的形态成立，宿主形态下控制面没有那个挂载的写入权，且会把特权面挪进控制面容器。记在这里，不做。

## 8. 回退

* 一个 payload 字段（`materialized`）即可回到今天：不送这个键，worker 自己建树。控制面侧那一跳删掉不影响任何现有调用。
* agent 侧的 `materialize` verb 是**增量**：老 instruction 一条都不改，删掉它不影响 `chown`/`rm`/`walk`。
* 建箱标记是**纯增量**文件：不写它、或删掉它，行为回到今天。
