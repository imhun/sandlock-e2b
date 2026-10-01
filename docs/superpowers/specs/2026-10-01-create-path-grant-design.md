# 建箱临时授权（per-op create grant）设计

**状态：** 待评审（评审通过后交 writing-plans 出实施计划，再动代码）
**触发：** 用户 2026-10-01 问"建箱时间还能继续优化吗"；§7.26 量出剩下 191 ms 里
`fileop:chown-workspace` 占 **71 ms**（worker→CP→agent→CP→worker 一整圈）、`record` 占 **47 ms**。
用户裁定：①worker 拿一次性授权直连本节点 agent；②把"建树+改属主"合成一次 agent 操作；
③记录带"建箱成功"含义的标记、拆除等建箱完成（防残留）；范围**只覆盖建箱期的临时操作**，
即建树/拷贝/改属主与卷切片这一类**材料化**操作（授权里就是一条 `materialize-tree`，
里面逐条列出这次要做的事）；④**凭据只要够维持到 worker 向 agent 发起请求**，
不需要等到建箱成功；⑤**拷贝也由 agent 做**（"让 agent 自己拷贝最直接"，2026-10-01，
它的代价与硬要求见 §4.3.1）。

> **④ 是这一版改掉前一版的地方**：前一版把授权"随建箱 payload 一次下发"（载体 A），
> 那样它的过期时间必须覆盖 **mint → 最后一次使用**之间的整段空档；而这段空档在建箱里
> **不固定**——不带快照的箱是毫秒级，带快照的箱要先 `copytree`（实测 2000 个文件 17.4 s）
> 才轮到改属主。于是载体 A 只能取"覆盖最慢建箱"的 120 s。要让授权真正短命，
> 就得把**铸权挪到"用之前"**：改成 **载体 B（worker 用时向控制面领一张）**。

---

## 1. 现状与实测（2026-10-01，`0.1.0-841-g0d7dc76-20261001-131547`）

| 段 | 耗时 | 说明 |
|---|---|---|
| 建箱总计（控制面 pod 内发起，n=10） | **191 ms** | 控制面自己的份额只有 ~1–3 ms |
| `provision`（worker 那一次 `POST /agent/sandboxes`） | 173 ms | 幂等重放单独量：p50 198 ms |
| ├ `fileop:chown-workspace` | **71 ms** | worker→CP→agent→CP→worker；其中约 27–30 ms 是**这一圈的固定开销**（同一条通道上"什么都不做的 walk"要 27 ms） |
| ├ `record`（`_runtime/<id>/sandbox.json` 原子写） | **47 ms** | 一次 mkdir + 写 + fsync + rename |
| ├ 其余（两次 NFS mkdir、记录查找、uid 认领、响应） | ~55 ms | |
| └ `prime`（运行时上下文） | 17 ms | |

参考值：两节点之间的**时钟差实测 ~16 ms**（同一次建箱的 worker 与 CP 时间戳之差）。

## 2. 目标与非目标

**目标**：建箱期的**材料化**（建树、快照拷贝、改属主、卷切片）不再由控制面**转发并等待执行**
——控制面只签发一张单次授权，worker 拿它直连本节点 agent，由 agent **一次做完**；
并把记录写移出响应路径而**不**引入残留。

**非目标（明说）**：

- **拆箱/巡检不走这条**：`rm`/`walk`/`remove-workspace`/`remove-runtime` 仍逐次经控制面。
- **不做通用"护照"，也不做"随 payload 预发的长期授权"**：授权是**一次操作一张**、
  用时现铸（见 §5 为什么这么选）。
- 不改 worker 的能力面：worker 仍然**没有** `E2B_C3_AGENT_TOKEN`，拿不到就签不出授权。

## 3. 要反转的既有规则，以及为什么可以

`docs/c3-privilege-relocation.md` §14.1 的链路表第四行写着
「**worker ──▶ agent：禁止（worker 不得发起）**」，
`docs/superpowers/plans/2026-09-28-c3-privilege-consolidation.md` 也把它列为已定形态
（"`worker ↔ agent` **无通道**"）。本设计**只**在这两种建箱期操作上反转它。

依据（已实测）：

1. 今天这条是**能力上**禁的：线上 worker 的环境里只有 `E2B_INTERNAL_API_KEY`，
   没有 `E2B_C3_AGENT_TOKEN`，所以它连不上 agent 的鉴权面。
2. worker **今天已经能**对"记录写着属于它这个节点"的任意沙箱，向控制面申请文件操作
   （`node_file_op` 只校验 `record.node_id == node_id`，否则 403）。本方案里，
   worker 的新入口**用同一套身份校验、同一个 owner 校验、同一段路径推导**，
   区别只是：控制面签一张授权让 worker 自己把指令送到 agent，而不是由控制面转发。
   也就是说**授权仍是逐操作、实时发生的**，能力面**严格窄于** worker 今天已能拿到的
   （它今天就能申请 chown，只是要等控制面转发完）。
3. 代价如实记下：agent 从此多一类调用方，它的路径纪律必须和新入口一起严（§4.2）。

## 4. 设计

### 4.1 授权 = 一次"建箱材料化"计划，用时现铸

worker 要材料化这次建箱的树与卷时，先向控制面**领一张计划**，再拿它直连 agent：

```
worker ──① 领权 POST /internal/nodes/{node}/file-grant ─────────▶ CP
worker ◀──② {agent_url, grant} ─────────────────────────────────── CP
worker ──③ POST {agent_url}/internal/grants/file-op {grant} ▶ 面 B ──▶ e2b-maint
```

- **①的 body 只有 `{op: "materialize-tree", sandbox_id, snapshot_id?}`** —— 路径、uid
  一律由控制面从自己的记录推导（§14.4 硬规则二不破）。
- **②的授权载荷**（一条 op，里面逐条列出这次要做的每一件事）：

```json
{
  "v": 1,
  "host": "<agent 自己的身份 = 该节点名>",
  "sandbox_id": "sbx_…",
  "op": "materialize-tree",
  "tree": {
    "path": "<ws>/sbx_…", "subdir": "workspace", "mode": "0770",
    "uid": 10000, "gid": 65534,
    "copy_from": "<ws>/_snapshots/<snap>/fs"
  },
  "slices": [{"volume": "data", "path": "<控制面推导>", "uid": 10000, "gid": 65534}],
  "jti": "<16 hex>", "iat": 1790832000, "exp": 1790832010
}
```

  线格式：`base64url(payload) + "." + base64url(HMAC-SHA256(E2B_C3_AGENT_TOKEN, payload))`。
  （`copy_from` 只在快照建箱时出现；`slices` 由记录的卷挂载列表推导。）
- **③的 agent 地址由控制面解析**（与今天 `node_file_op` 里做的是同一次解析），
  worker 不自己寻址，也不缓存。

### 4.2 agent 侧：独立入口 + 六步校验

面 B（`maint` 那个端口）新增**独立路由** `POST /internal/grants/file-op`，
body 只有 `{grant}` —— 路径、uid、op 一律从授权里读，**agent 不接受请求方另报的路径**。

校验顺序，全部 fail-closed：

1. **签名**：HMAC 对上 `E2B_C3_AGENT_TOKEN`；
2. **host**：`payload.host` == 本 agent 自己的节点身份（D12）；
3. **过期**（含上限）：`iat <= now <= exp`，且 `exp - iat <= 60 s`（§5）；
4. **单次消费**：`jti` 用过即废，再拿来具名拒绝（`grant already used`）；
5. **操作在允许集合内**：只有 `materialize-tree` 一个 verb（建树/拷贝/改属主/卷切片都在它里面，
   逐条按授权里签好的条目做，多一条都不认）；
6. **路径复核**：照旧 `realpath` + agent 自己的四根白名单 —— 与 CP 中转那条路径
   **同一段代码**，两道校验不互相替代。

通过后复用现有 `run_file_op` / `maint` 执行器。

> **单次消费带来的一个重试细节**：agent 已经执行、但响应丢失时，worker 重放同一张授权会
> 拿到 `grant already used`。规则写死：**worker 重新领一张再试一次**（领权就是把 ① 再走一遍），
> 绝不重放旧授权。两种操作都是幂等的，所以"重试一次"是安全的。

### 4.3 合成操作 `materialize-tree`：agent 建树 + 拷贝 + 改属主

**用户裁定：拷贝也由 agent 做**（协议上最直接 —— 一条 op 做完，中间没有握手，
worker 不再自己建树/拷贝/建卷切片）。`c3_agent/priv/maint.c` 新增一个 verb：

```
e2b-maint materialize-tree --uid U --gid G --path <root> [--subdir workspace] [--mode 0770]
                           [--copy-from <snapshot fs>] [--slice <path>]...
```

一次调用把四件事做完：① `mkdir -p <root>/<subdir>`（mode 0770，group = worker gid）；
② 有 `--copy-from` 就递归拷贝（**不跟随符号链接**，源侧沿用本文件既有的 `FTS_PHYSICAL` 纪律）；
③ 每条 `--slice` 建好；④ 一次 FTS 遍历把上述所有树 `lchown` 成 U:G。
它**不做任何路径推导**（路径是授权里签好的），失败按 `maint.c` 既有纪律：非零退出、带 stderr 具名失败。

于是 worker 的建箱路径不再碰树：领权 → 一条 op → 记录 → 响应。

#### 4.3.1 这是本设计最需要小心的一处（威胁模型变了）

把递归拷贝从**无特权的 worker**（65534）搬进**root + `CHOWN,DAC_OVERRIDE,FOWNER` 的面 B**，
搬的不是新特权（面 B 本来就是 uid 0 的 Python 服务，`maint` 容器实测
`runAsUser=0` + 那三条 cap），而是**一条新的、吃租户输入的特权代码路径**：

- 今天 `shutil.copytree(symlinks=True, dirs_exist_ok=True)` 跑在 worker 里，
  就算被快照里的符号链接骗到，它能写的地方也就限于自己那些树；
- 同一个洞在面 B 里就是 **root 级逃逸**；
- 而且拷贝比 chown/rm **多一侧**：**目标侧**。`dirs_exist_ok` 的合并语义是有用的
  （迁移/重建同一个 id 要保留既有文件 —— §6 的"跨节点迁移保文件"就是它），
  但目标树里可能有**上一个化身留下的、沙箱自己能改的**东西：一个指向 `/etc` 的符号链接，
  就足以把写操作引到根目录之外。

所以这个 op 的硬要求（每条都要有用例，见 §6）：

1. **源侧**：不跟随符号链接（`FTS_PHYSICAL` + 用 `symlink()` 重建，不是解引用后拷贝）；
2. **目标侧**：合并写入时逐段 `O_NOFOLLOW`（或 `openat2(RESOLVE_BENEATH)`）打开；
   目标是符号链接就**具名拒绝**，绝不顺着写；
3. **失败具名**：拷到一半失败不得报成功，半棵树必须能被识别并交给孤儿路径；
4. **不许丢既有性质**：跨节点迁移"文件还在"这条不能被这次改动破坏。

### 4.4 worker 侧收口：`AgentFileOps` 上多一个 `materialize()`

worker 的特权文件操作已经收口在 `envd_service/agent_fileops.py`。新增一个方法：

```python
client.materialize(sandbox_id, snapshot_id=None)   # 领权 → 直连面 B → 返回材料化结果
```

1. 向控制面**领权**（`{op:"materialize-tree", sandbox_id, snapshot_id}`）；
2. 拿 `{agent_url, grant}` 直连面 B；
3. 若面 B 说 `grant already used` ⇒ 重新领一张、重试一次；
4. 若面 B **没有这条路由**（旧 agent，滚动升级中）⇒ **具名告警 + 退回今天的老路**
   （worker 自己 mkdir/copytree + 经控制面中转 chown）：控制面那条路是实时授权、
   更严不会更松，且**不是静默跳过** —— 步骤照样执行，只是慢。

`_agent_create_sandbox` 里那三段（`workspace_dir.mkdir` / 快照 `copytree` /
`build_volume_mounts` 的切片创建）随之消失，改成这一次调用；其余（uid 认领、
`runtime_registry.register`、记录、建箱标记）**不动**。

### 4.5 记录先落"建箱中"标记，拆除等它

今天已经存在一个**建箱/拆除竞态**（不是本轮引入）：DELETE 落在建箱中途时，
worker 先把树拆了，建箱随后继续写记录 / 写盘统计 ⇒ 留下残留（N53 清理过的那一类）。
本设计要把记录写移出响应路径，这个窗口会从 ~0 变成 ~100 ms，所以必须先补上它：

1. 建箱**开始**时先落一个**建箱标记**（`<state>/_runtime/<id>/.creating`，小文件、不 fsync）；
2. 建树 / 改属主 / 收尾（含记录写）全部完成后**摘掉**这个标记；
3. `DELETE /agent/sandboxes/{id}` 看到标记 ⇒ 先**有界等待**这次建箱结束再拆；
   等不到（比如建箱进程已经崩了）⇒ 按"未完成的建箱"回收：拆树 + 丢弃记录；
4. 记录（`sandbox.json`，`state=running`）改在**响应之后**写。

于是有两条可读的不变量：

- **标记在 ⇒ 有建箱在飞**（拆除要等）；
- **记录在 ⇒ 该次建箱已经成功落盘**（这就是"记录是否建箱成功的标记"，用"记录存在与否"
  表达，而不是在同一份记录上写两次 —— 后者要多付一次 47 ms 的原子写）。

重启语义：进程崩在建箱中途 ⇒ 磁盘上留标记 + 半棵树、没有记录 ⇒ 走**既有**的孤儿路径回收，
并顺手清掉过期标记；**绝不允许**把带标记的记录当成活沙箱。

## 5. 过期时间：**10 s**（可配，`E2B_CREATE_GRANT_TTL_S`）

1. **它只需要覆盖"铸权 → worker 把请求发到 agent"**：两者相邻（中间只有一次 HTTP），
   在生产里就是几毫秒。
2. **留足时钟余量**：两节点实测钟差 ~16 ms，10 s 是它的 **600×**。
3. **单次消费**：泄漏出去的授权在 10 s 内也只能用一次，且只能用于"这台节点上，
   这个沙箱的这一个操作"。
4. **为什么不取 120 s**：那正是前一版（授权随 payload 预发）被迫取的数——因为要在
   "铸权"与"最后一次使用"之间跨过整个快照 `copytree`（实测 2000 文件 17.4 s，上不封顶）。
   本方案把铸权挪到用时，那段空档**不存在了**，所以可以取到 10 s。
5. **上限**：agent 拒绝 `exp - iat > 60 s` 的授权（防铸权端写错），并拒绝 `exp < iat`。

## 6. 验收矩阵

| 判据 | 怎么测 | 期望 |
|---|---|---|
| 建箱延迟 | `deploy/scripts/acceptance/create_latency_probe.py`（控制面内，n=10） | 191 ms → **P1 后 ~145–150 ms** → **P2 后 ~110–115 ms** |
| 逐段 | `E2B_CREATE_TRACE=1` + `worker_provision_cost.py` | `fileop:*` 段整段消失（worker 不再建树/拷贝/改属主），只剩一次 `materialize`；`record` 段离开响应路径 |
| **符号链接不得逃逸**（新） | 源侧：快照里放一个指向外部的符号链接 ⇒ 必须被**重建为符号链接**，不得解引用；目标侧：目标树里预置一个指向 `/etc` 的符号链接 ⇒ 必须**具名拒绝**，不得写穿 | 两条用例都绿，且拒绝时不留半棵树 |
| **迁移保文件**（既有性质） | `deployment_smoke.py` 的"跨节点迁移保文件" | 仍绿（合并语义没被新 op 破坏） |
| 授权负面 | 新用例：错签名 / 过期 / 超上限 / 换 host / 计划外 op / 重放 / 换 sandbox | **全部具名拒绝**，且不触达 `maint` |
| 重试 | agent 已执行但响应丢失（注入） | worker 重新领权重试一次后成功；旧授权再拿来被拒 |
| 竞态（P2） | 建箱中途发 DELETE：树与记录都不残留；带标记的记录**永不**被当活沙箱 | 0 残留 |
| 回退（滚动升级） | agent 无新路由 ⇒ 具名告警 + 走控制面老路 | 建箱仍成功 |
| 未回归 | `MULTI-NODE`/`DEPLOYMENT` 冒烟、`DRY_RUN` diff 0 行 | 全绿 |

## 7. 记录在案的取舍与已否决的变体

### 7.1 载体 A（授权随 payload 预发）—— 为了短 TTL 否掉

载体 A（授权随建箱 payload 一次下发、worker 全程不回控制面）比本方案**再**省一次领权往返，
代价是授权必须在 worker 内存里活过整段建箱（因此 TTL 得取 120 s 那一档），
而且"授权在用时之前就已经存在"这条叙事更弱。本设计选择 B：
**逐操作、实时签发、10 s、单次消费**。要回头走 A，需要先有实测说明那点收益值这个价。

### 7.2 "worker 一开头就发 op、agent 等文件就绪再改" —— 实测更慢，否掉

这个变体的形状是：worker 在建箱**一开始**就把这条 op 发给 agent，agent
建好空树后**挂着等**一个"就绪"信号（然后一次 FTS 改属主），worker 在这期间完成
`copytree` / 装卷，再写就绪信号，最后 join。直觉上它应该更快：尾段没有往返了。

**2026-10-01 在生产 worker 里实测**（同一个 NAS、同一台节点，p50，n=8）：

| 动作 | 实测 |
|---|---|
| 写一个"就绪"小文件（open+write，不 fsync） | **11.97 ms**（max 16.0） |
| 写 + 删一对（agent 侧还要清掉它） | **20.77 ms** |
| `stat` 一个已存在的 marker | 0.00 ms（客户端缓存） |
| `stat` 一个**不存在**的文件（轮询未命中） | 0.00 ms（负缓存；max 4.8） |
| **集群内一次控制面往返**（worker→control-plane Service，`GET /healthz`） | **1.29 ms**（max 11.8） |

结论：这个变体的"就绪信号"是**一次 NFS 写（约 12 ms）+ agent 侧一次删（约 9 ms）+ 轮询抖动**，
而它想省掉的那次**领权往返只要 1.29 ms**（控制面就在集群里）。
**它是本方案 4–5 倍贵**，所以否掉。

> 这条实测也是整轮优化的注脚：**NFS 元数据往返 10–40 ms，集群内 HTTP 1–2 ms**。
> 建箱的每一次"少一次落盘/多一次直连"都值十几毫秒，方向只有这一个。

### 7.3 让 agent 自己拷贝 —— **已选定**（原为备选，用户 2026-10-01 裁定）

选定后 §4.3/§4.4 按它改写：worker 不再建树/拷贝/建卷切片，一条 op 做完，
**中间没有任何握手**，也就没有 §7.2 那笔 NFS 开销。代价与硬要求见 §4.3.1。

剩下的唯一"再快一点"的变体（**本轮不做**）：把领权那一次控制面往返
（1.29 ms + 控制面自身 1–5 ms）也省掉 —— 把就绪/参数放进那条已有的 HTTP 连接
（分块请求体，EOF = 参数结束）。收益只有几毫秒，且要在两侧引入流式语义，
不值得，留作记录。

## 8. 回退

- 一个开关即可让 `AgentFileOps` 回到"全部经控制面"的老路；agent 的新路由留着不用。
- `maint.c` 的新 verb 是**增量**：老 instruction 一条都不改，删掉它不影响任何现有调用。
- 建箱标记是**纯增量**文件：不写它、或删掉它，行为回到今天。
