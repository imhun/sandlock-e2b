# 建箱临时授权（create plan grant）设计

**状态：** 待评审（评审通过后交 writing-plans 出实施计划，再动代码）
**触发：** 用户 2026-10-01 问"建箱时间还能继续优化吗"；§7.26 量出剩下 191 ms 里
`fileop:chown-workspace` 占 **71 ms**（worker→CP→agent→CP→worker 一整圈）、`record` 占 **47 ms**。
用户裁定：①worker 拿一次性授权直连本节点 agent；②把"建树+改属主"合成一次 agent 操作；
③记录加"建箱成功"标记、拆除等建箱完成（防残留）；范围**只覆盖建箱期的临时操作**，
即 `create-tree` 与 `chown-volume-slice` 两种；并问"临时授权的凭据应该多久过期"。

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

## 2. 目标与非目标

**目标**：把建箱期这两类特权文件操作从"每操作一次控制面往返"改成"建箱时授权一次、
worker 直连本节点 agent 执行"，并把"建树+改属主"合成一次 agent 操作；
同时把记录写移出响应路径而**不**引入残留。

**非目标（明说）**：

- **拆箱/巡检不走这条**：`rm`/`walk`/`remove-workspace`/`remove-runtime` 仍必须逐次经控制面
  （那是"控制面是唯一裁决者"承担的地方，N53 的修复正建立在它之上）。
- **不做通用"护照"**：授权不是"这个 worker 可以对我节点上的沙箱做特权操作"，
  而是**一次建箱的、逐条列出的操作计划**。
- 不改 worker 的能力面：worker 仍然**没有** `E2B_C3_AGENT_TOKEN`。

## 3. 要反转的既有规则，以及为什么可以

`docs/c3-privilege-relocation.md` §14.1 的链路表第四行写着
「**worker ──▶ agent：禁止（worker 不得发起）**」，
`docs/superpowers/plans/2026-09-28-c3-privilege-consolidation.md` 也把它列为已定形态
（"`worker ↔ agent` **无通道**"）。本设计**只**在这两种建箱期操作上反转它。

依据（已实测）：

1. 今天这条是**能力上**禁的：线上 worker 的环境里只有 `E2B_INTERNAL_API_KEY`，
   没有 `E2B_C3_AGENT_TOKEN`，所以它连不上 agent 的鉴权面。
2. 但 worker **今天已经能**对"记录写着属于它这个节点"的任意沙箱，向控制面申请文件操作
   （`node_file_op` 只校验 `record.node_id == node_id`，否则 403）。
   也就是说：一张**限定单个沙箱、限定两种建箱操作、带过期**的授权，
   能力面**严格窄于** worker 今天已经能拿到的。
3. 代价如实记下：裁决点从"每次操作在 CP 现算"变成"建箱时铸一次"；
   agent 从此多一类调用方，它的路径纪律必须和新入口一起严（§4.2）。

## 4. 设计

### 4.1 授权 = 一张签名的"建箱计划"（create plan）

控制面在**建箱时**（`_provision_remote` 之前）铸一张计划，把这次建箱要用到的
特权文件操作**逐条列全、连路径与 uid 一起**签进去：

```json
{
  "v": 1,
  "host": "<agent 自己的身份 = 该节点名>",
  "sandbox_id": "sbx_…",
  "jti": "<16 hex>",
  "iat": 1790832000, "exp": 1790832120,
  "ops": [
    {"op": "create-tree",          "path": "<ws>/sbx_…",              "subdir": "workspace", "uid": 10000, "gid": 65534},
    {"op": "chown-volume-slice",   "path": "<vol>/data/sbx_…",        "uid": 10000, "gid": 65534, "recursive": true}
  ]
}
```

线格式：`base64url(payload) + "." + base64url(HMAC-SHA256(E2B_C3_AGENT_TOKEN, payload))`。

**为什么"逐条列路径"而不是"只给沙箱 id、让 agent 自己推"**：§14.4 硬规则二说
worker 只报「哪个沙箱、什么动作」，**路径由控制面从自己的记录推导**，
agent 再独立做一次 realpath + 白名单。把推导结果签进计划，两边都不破：
控制面仍是唯一推导者，agent 仍独立复核；worker 既不能指定路径，也不能新增计划外的操作。

### 4.2 agent 侧：一个独立入口 + 五步校验

面 B（`maint` 那个端口）新增一条**独立路由** `POST /internal/grants/file-op`，
不复用"CP token"那条（让审计上"谁在调"不混）。校验顺序，全部 fail-closed：

1. **签名**：HMAC 对上 `E2B_C3_AGENT_TOKEN`；
2. **host**：`payload.host` == 本 agent 自己的节点身份（D12：地址里的主机名就是它的身份）；
3. **过期**：`now <= exp`；
4. **在计划里**：请求的 `{op, path}` 必须**逐字命中**计划中的某一条 —— 不在计划里就具名拒绝；
5. **路径复核**：照旧 `realpath` + agent 自己的四根白名单（与 CP 中转那条路径**同一段代码**）。

通过后执行复用现有 `run_file_op` / `maint` 执行器。**不做单次消费**：两种操作都是幂等的
（`mkdir -p` + 递归 chown），而建箱路径本来就有重试；靠"短过期 + 计划封闭 + host 绑定"约束，
比引入"用一次就废"更容易说清，也不会把重试变成失败。

### 4.3 合成操作 `create-tree`

`c3_agent/priv/maint.c` 新增一个 verb：

```
e2b-maint create-tree --uid U --gid G --path <root> [--subdir workspace] [--mode 0770]
```

语义：`mkdir -p <root>/<subdir>`（父目录按需创建），然后**一次 FTS 遍历**把整棵树 `lchown`
成 U:G。它**不做任何路径推导**（路径是计划里签好的），失败按 `maint.c` 既有纪律：非零退出、
带 stderr 具名失败。

收益：省掉 worker 的两次 NFS mkdir，以及"先建后改"的第二遍遍历。

### 4.4 worker 侧收口：`AgentFileOps` 上加"建箱作用域"

worker 的所有特权文件操作已经收口在 `envd_service/agent_fileops.py`。加一个**线程内**的
建箱作用域：

```python
with agent_fileops.create_scope(plan, agent_url):   # 只有本次建箱的线程看得见
    ...   # 建树、装卷、改属主 —— 这些调用沿用现有函数签名
```

- 计划里**命中**的操作 → 直连 agent 的 `/internal/grants/file-op`；
- 计划里**没有**的操作、或没装作用域时 → **照旧走控制面**（今天的路径一字不变）。
  这是有意的回退：控制面那条路是**实时授权**，更严不会更松；同时让"新 agent + 旧 worker"
  或"旧 agent + 新 worker"的滚动升级都能工作（新入口不可用 ⇒ 具名告警 + 走老路，不是静默跳过）。

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
- **记录在 ⇒ 该次建箱已经成功落盘**（这正是"记录是否建箱成功的标记"，
  只是用"记录存在与否"表达，而不是在同一份记录上写两次 —— 后者要多付一次 47 ms 的原子写，
  详见 §5 的账）。

重启语义：进程崩在建箱中途 ⇒ 磁盘上留下标记 + 半棵树、没有记录 ⇒ 走**既有**的孤儿路径回收
（`_scan_workspace_runtimes` / uid reconcile 那条），并顺手清掉过期标记；
**绝不允许**把带标记的记录当成活沙箱。

## 5. 过期时间：120 s（可配）

`E2B_CREATE_GRANT_TTL_S`，默认 **120**。理由：

1. **必须超过控制面自己的耐心**：控制面对 worker 的建箱 POST 超时是 **60 s**
   （`app.state.remote_http` 的 `timeout=60`）。超过 60 s，控制面已经按失败处理并删了记录，
   授权再长也只是给一个"控制面已经不认"的沙箱续命。2× 是留给 worker 收尾与重试的余量。
2. **不取 660 s**（agent file-op 预算）：那意味着泄漏出去的授权在 11 分钟内一直可用，
   而没有任何"控制面仍在跟踪"的建箱会跑那么久。
3. **失败的形状是好的**：TTL 内没做完 ⇒ 特权步骤被**具名拒绝**（"grant expired"）⇒
   建箱失败得清清楚楚。对比今天：一个跑了 130 s 的大快照建箱，控制面 60 s 就放弃了，
   worker 却可能把它建完 —— 结果是一个**控制面不知道的活沙箱**。TTL 反而把这个洞收小了。
4. **计划封闭 + host 绑定 + 只覆盖两种建箱操作**：泄漏一张授权的价值，
   等于"在我这台节点上、对这个沙箱、再做一次幂等的建树/改属主"。

## 6. 验收矩阵

| 判据 | 怎么测 | 期望 |
|---|---|---|
| 建箱延迟 | `deploy/scripts/acceptance/create_latency_probe.py`（控制面内，n=10） | 191 ms → **P1 后 ~150 ms** → **P2 后 ~110 ms** |
| 逐段 | `E2B_CREATE_TRACE=1` + `worker_provision_cost.py` | `fileop:*` 段显著变小；`record` 段离开响应路径 |
| 授权负面 | 新用例：错签名 / 过期 / 换 host / 计划外 op / 计划外 path / 换 sandbox | **全部具名拒绝**，且不触达 `maint` |
| 竞态（P2） | 建箱中途发 DELETE：树与记录都不残留；带标记的记录**永不**被当活沙箱 | 0 残留 |
| 回退（滚动升级） | 计划存在但 agent 无新路由 ⇒ 具名告警 + 走控制面老路 | 建箱仍成功 |
| 未回归 | `MULTI-NODE`/`DEPLOYMENT` 冒烟、`DRY_RUN` diff 0 行 | 全绿 |

## 7. 回退

- 一个开关（`E2B_CREATE_GRANT_URL`/`E2B_CREATE_GRANT_ENABLED`）即可让 worker 回到
  "全部经控制面"的老路；agent 的新路由可以留着不用。
- `maint.c` 的新 verb 是**增量**：老 instruction 一条都不改，删掉它不影响任何现有调用。
- 建箱标记是**纯增量**文件：不写它、或删掉它，行为回到今天。
