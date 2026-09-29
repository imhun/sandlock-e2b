## Task 3: 槽位身份由 **CP 下发**、由 agent 授予（route B 启动路径）

**Deliverable:** worker fork 子进程、让它 unshare，**把 `{pid, sandbox_id}` 报给 CP**
（**不是报给 agent**，fire-and-forget）；CP 校验后**带着 uid 指令 agent**；agent 反查宿主 pid
并写 map；**子进程自己轮询 `setresuid`** 成功后 exec `sandlock-supervise`。
**worker 侧零特权，且不能指定 uid；agent 无状态。**

> **⚠ 验收环境（不看这条会"假通过"）**：本 task 的判据 **13**（`NSpid` + cgroup 双命中）与
> **16**（并发建箱）**都只在"同一节点上跑多个 worker"时才暴露**，而 **k8s 现在是 1 节点 1 worker** ——
> 在 k8s 上跑这两条会**全绿但什么都没测到**。⇒ **13 与 16 必须在
> `deploy/compose/docker-compose.multinode.yml`（3 个 worker 同机）上验收**；
> k8s 只跑与 worker 数无关的那几条（1 / 2 / 3 / 7）。

- [ ] 建 `deploy/k8s/c3-agent.yaml`（**一个 DaemonSet、两个容器**，pod 级 `hostPID: true`）：
  面 A 用**独立镜像**、`runAsUser: 65534`、`capabilities.add: [SETUID, SETGID]`（BND 声明）；
  面 B `runAsUser: 0` + `drop:[ALL]` + `add:[CHOWN,DAC_OVERRIDE,FOWNER]`。

- [ ] 写契约用例（`tests/contract/test_route_b_executor.py` 同形）：断言
  ① 槽位进程的**宿主 uid == X**（宿主机侧 `stat`，不是 ns 内视角）；
  ② 槽位进程的 `/proc/<pid>/cgroup` **与 worker 自己的逐字相同**；
  ③ worker 进程 `CapEff == 0`；④ worker 镜像里**没有** `e2b-slot-spawn`；
  ⑤ **worker 上报的消息里不含 uid**（用一个只接受 `{pid, sandbox_id}` 的假 **CP** 断言：
     多带一个 `uid` 字段即被拒）；
  ⑥ **worker 直接向 agent 发身份请求必须被拒**（"通道不存在" ⇒ **连接层就拒**，
     而不是靠 agent 判）—— 这是"只有两条通道"的第二个检查点；
  ⑦ **worker 的会话列表里没有指向 agent 的连接**（断言"只有两条通道"这条规则没被绕过 ——
     这是本 task 唯一一条"因为省一跳而被违反"的检查点）。
- [ ] 跑确认**红**。
- [ ] 改 `envd_service/route_b.py`：新增"identity-grant"启动路径（`E2B_SLOT_IDENTITY=agent-grant`，
  默认 `spawn` 以保回退）；`envd_service/priv_helpers.py` 加
  `request_identity(pid, sandbox_id)` —— **签名里没有 uid**。
- [ ] CP 侧新增**转发**：worker → `POST /internal/nodes/{node_id}/slot-identity`
  （body `{sandbox_id, pid}`，**没有 uid**）；CP 走硬规则 6 的三步校验，**再带着 uid 指令 agent**
  （`grant-slot`）；**worker 不直接调 agent**。
- [ ] 跑确认**绿**；再跑 `tests/security` 全档，确认没有回归。
- [ ] 实现反查时 **`NSpid` 命中 + 目标 worker pod 的 cgroup 命中**（**不能只比 `NSpid`**）；
  ④ 的指令里因此要带 **worker 的 pod 身份**。写一条用例：**同节点造两个 worker、各起一个
  容器 pid 相同的子进程**，断言 agent 只认对的那个。
  （这条在 k8s 上 1 节点 1 worker 时不暴露，**compose multinode 的 3 worker 同机会暴露**。）
- [ ] 测唯一那个细节：**pid 在 worker 上报与 agent 写入之间消失**（子进程崩了）⇒
  agent 的写失败必须 **fail closed 并点名**（"沙箱 S 的槽位 pid 已不在"），不许静默继续。
- [ ] **并发建箱（判据 16）**：N 个 worker **同时**各建 1 个沙箱（N = 该形态的 worker 数；
  compose multinode = 3、k8s = 2），重复 k 轮；断言 ① **全部成功、零 `E2B_CREATE_QUEUE_TIMEOUT_S`
  命中**；② 总耗时**不出现超线性退化**（对比"逐个建"的串行基线）；③ agent 侧日志**不出现
  "同一时刻只有一个 grant 在执行"**那种排队证据。
  **反面臂（判据非恒真）**：把 **CP→agent 客户端的连接池压到 1**，同一个用例**必须复现**
  排队或超时。**不带反面臂的这条判据等于没测。**
  ⇒ 并据此定下 **CP→agent 的并发上限配置**（≥ 该形态最大并发建箱数），写进 manifest。
- [ ] **真机**：用 `deploy/scripts/acceptance/probe_c3_userns_map_handoff.py` 的
  `--role forker/agent --pids-file` 两臂在集群上复验（判据：`C3-MAPHANDOFF-VERDICT=agent-can-map`
  **且** `C3-CGROUP=worker`）。
- [ ] Commit。

