## Task 2: agent 的通道与三步校验（含 N49）

**Deliverable:** agent 与 CP 之间的**双向**认证通道（CP→agent 指令 / agent→CP 回报）；
CP 侧的身份校验按硬规则 6 落地。**agent 不持有任何授权表**（§4 规则 5 的合并说明）——
它只接受**来自 CP 的、带参数的指令**。

- [ ] 写用例（新 `tests/contract/test_internal_identity.py`）：
  ① 用 node A 的凭据请求 node B 的沙箱 ⇒ **拒**；
  ② 偷到 node B 的凭据、从 node A 的网络位置发出 ⇒ **拒**；
  ③ 同一请求，两个节点的源 IP **必须不同**（防"源 IP 层是恒真的死代码"）。
- [ ] 跑确认**红**。
- [ ] 实现：`control_plane/auth.py` 加 `node_id_for_key(...)`（key → node）；
  `control_plane/api/internal.py` 的每个 handler 走三步校验；节点 IP 从 **k8s API** 取
  （CP 已挂 ServiceAccount），**不采信 `body.get("address")`**。
- [ ] 实现 **CP → agent 的指令面**：`POST /internal/nodes/{node_id}/agent/{op}`
  （第一步只实现 `grant-slot`），**只允许写入凭据所对应的那个节点**（复用三步校验）；
  **agent 侧无状态**，不做本地授权表、不做 TTL、不做"先推后发"的顺序纪律。
- [ ] 跑确认**绿**；并加**钉子**：`tests/unit/` 下断言 worker pod 清单**不含**
  `CAP_NET_RAW`、internal API 前**没有**代理（配置层断言）。
- [ ] 真机：`kubectl` 复验两节点 worker 的请求在 CP 侧源 IP 不同（写进
  `docs/deploy-clusters.md` §现状）。
- [ ] Commit。

