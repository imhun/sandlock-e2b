# Task 3 / slice A 报告：槽位身份由 CP 下发、由 agent 授予（route B 启动路径）

- 工作树：`/Users/polus/project/ai/sandlock-e2b/tmp/wt-c3`（分支 `feat/c3-consolidation`）
- BASE：`2f849b3`（Task 2 三轮修复）
- 裁定：D9.1（worker 只报 `{sandbox_id, pid}`）、D9.2（CP 转发）、**D9.3 = (B) pidns inode +
  k8s cgroup 叠加证明**、D9.4（CP→agent 寻址）、D9.5（fail-closed 细节 + 并发旋钮）
- **未做**（slice B）：`deploy/k8s/c3-agent.yaml`、compose agent 服务、token/NetworkPolicy、
  CP SA 的 RBAC（D8）、并发上限的出厂取值、判据 13/16 的真机验收、`docs/deploy-clusters.md` 现状节。
  本片**未碰任何集群**，未 push、未 merge。

---

## 1. 实现了什么

### 1.1 agent 侧：容器 pid → 宿主 pid 反查（`deploy/c3_agent/lookup.py`，新）

纯函数式的一片，只吃一棵 `/proc`（生产是宿主的那棵，因为面 A `hostPID`），因此可以在宿主 lane
上用合成 `/proc` 驱动、在容器 lane 上用真内核驱动，slice B 的 DaemonSet 一行不用改。

```python
class WorkerIdentity: node_id: str; pid_namespace: str; pod_uid: str | None
class ProcLookup:
    def host_pid(container_pid, identity, *, sandbox_id) -> int   # 或 LookupRefusal
    def present(host_pid) -> bool
```

候选必须**同时**满足：

1. `NSpid` 链长度 > 1 且**最后一项** == 上报的容器 pid（长度 1 的链是从未离开自己 pid ns 的进程
   —— agent 自己的 `/proc` 里全是这种）；
2. `readlink /proc/<pid>/ns/pid` **逐字等于** worker 记录的 pidns 身份；
3. k8s lane 额外要求宿主侧 `cgroup` 路径含 `pod<worker pod UID>`（UID 由 CP 从 API 取，不从
   worker 来）。

四条点名拒绝：身份不可用、pid 已不在（`沙箱 S 的槽位 pid 已不在`，brief 原文）、不在目标 worker
的 pid ns、k8s cgroup 不符、多命中（ambiguous）。**没有** "取第一个 NSpid 尾号匹配" 这种回退。

### 1.2 agent 服务：指令带身份、写前反查、写失败点名（`deploy/c3_agent/app.py` 改）

- `grant-slot` 的 body 变成 `{sandbox_id, uid, pid, worker{node_id, pid_namespace, pod_uid?}}`；
  `pid` 是**容器** pid。缺 `worker` 由 schema 拒（422）；`worker.node_id` 与 URL 节点不一致 → 400 点名。
- 流程：`lookup.host_pid(...)` → `as_uid --uid X --pid <host>`；lookup 的拒绝 → 502 点名。
- `as_uid` 失败**且**该 host pid 已从进程表消失 → 502 `沙箱 S 的槽位 pid 已不在`（D9.5 那一条），
  不再把原语的 "cannot read uid_map" 当最终口径转发。
- 响应新增 `hostPid` / `pidNamespace`，保留原字段（`pid` 仍是容器 pid）。

### 1.3 CP 侧：转发 + 寻址 + 客户端（`control_plane/`）

- 新端点 `POST /internal/nodes/{node_id}/slot-identity`（body `{sandbox_id, pid}`）：先走
  `_require_node_identity`（①凭据→节点 ②自称==凭据 ③对象是 CP 自己记录且属于该节点 + 源 IP 第二
  因子），再取 CP 记录里的 `host_uid` 与节点记录里的 pidns；**body 里出现 `uid` 一律 400 点名**
  （worker 不能命名身份）。命名失败：未知沙箱 404、属他节点 403、无 host uid 503、节点无 pidns 503、
  无 agent 客户端 503。
- 节点记录新增 `pid_namespace`（`control_plane/registry/nodes.py`，进 `to_storage_dict` 共享视图）；
  `register` 与 `heartbeat` 都收 `pidNamespace`（形状不合 → 400 点名）；心跳刷新是必需的——worker
  容器重启后 node id 不变而 inode 变了，只写一次会把节点钉死。心跳不带该字段时**不覆盖**已存值
  （滚动升级期间老 worker 不会抹掉记录）。
- `control_plane/c3_agent_client.py`（新）：`AgentTarget(url, pod_uid)` +
  `K8sAgentAddressResolver`（worker pod → `spec.nodeName` + `metadata.uid`，再按 label 在该 node 上找
  agent pod 取 `podIP`；缺/多 → `None`）+ `ComposeAgentAddressResolver`（`E2B_C3_AGENT_URL` 服务名）
  + `C3AgentClient`（typed 超时 → 504、不可达 → 502、agent 拒绝 → 502 原文转发；`E2B_C3_AGENT_TOKEN`
  未配置 → 503；**`E2B_C3_AGENT_MAX_CONCURRENCY` 并发旋钮**，0 = 不限）。
- 寻址模式跟随 `E2B_NODE_ADDRESS_MODE`（不再开第二个会漂移的开关）；`create_app` 可注入
  `c3_agent_client`（测试/嵌入）。

### 1.4 worker 侧：零特权启动路径（`envd_service/`）

- `E2B_SLOT_IDENTITY=spawn|agent-grant`，**默认 `spawn`**（回退保留到 Task 4/7）。非法值在
  `RouteBConfig.from_settings` 点名拒绝。
- `envd_service/slot_identity.py`（新）＝子进程那一半：`unshare(CLONE_NEWUSER)` → 轮询
  `setresuid(X)`（`E2B_SLOT_IDENTITY_WAIT_TIMEOUT_S`，默认 30s）→ `execv sandlock-supervise`。
  `_spawn_slot_identity` 用 `child_argv(uid=..., supervise_argv=...)` 起它，supervise 的 argv 与
  `setpriv` 那条**同一个 builder**（`_supervise_argv`），`--control-fd/--events-fd` 原样继承。
- `W1SlotPool(slot_identity=..., identity_reporter=...)`：`agent-grant` 时默认 starter 换成上面那个，
  且**不再使用 broker spawner**（`RouteBConfig.from_settings` 在 agent-grant 下把 `spawner` 置 None）；
  起完子进程后、**在 readiness 等待之前**调用 reporter `(sandbox_id, process.pid)`；reporter 失败 →
  kill 子进程 + 原样抛出（uid 归还，不泄漏）。`agent-grant` 无 reporter → 构造即拒绝。
- `RouteBConfig.privileged_starter`：`agent-grant` 下不再要求 root/broker，只要求 reporter；
  executor 的 decline 文案因此多一条分支（老的文案逐字不动，`test_sandlock_executor_route_b.py` 的
  既有正则仍成立）。
- `envd_service/priv_helpers.py::request_identity(pid, sandbox_id, *, ...)`：**签名与报文体都没有
  uid**；POST 到 CP 的 `slot-identity`；拒绝/不可达 → `PrivHelperError` 点名。
- `envd_service/worker_identity.py`（新）：`worker_pid_namespace()`（`readlink /proc/self/ns/pid`，非
  Linux 或无该链 → None）+ `build_identity_reporter(settings, ...)`（缺 CP URL 或 node id → None）。
- register/heartbeat 载荷新增 `pidNamespace`（heartbeat 里作为显式参数传入，因此既有的
  `_heartbeat_usage_payload` 精确断言不受影响）。

### 1.5 共享形状校验（`gateway_common/worker_identity.py`，新）

`validate_pid_namespace`（`pid:[N]`）、`validate_pod_uid`（UUID）、`pod_cgroup_token`（`pod<uid>`）。
两个部署面共用（与 `gateway_common.paths` 同性质）：进 `/proc` 查找与 cgroup 子串比较之前先卡形状。

---

## 2. D9.3 设计（载体、为什么安全、失败模式）

**载体 = worker 自己的 pid namespace 身份（`pid:[N]`）**，由 worker 在 register/heartbeat 自报、CP
存进它自己的节点记录、随指令下发；k8s lane **叠加** pod UID（CP 从 API 取）。

为什么是它（实测）：compose worker 的 cgroup namespace 是私有的，容器内 `/proc/self/cgroup` 是
`0::/`、mountinfo 的 cgroup2 root 也是 `/`（OrbStack 实测 `185c54a76f4f` 只能从 hostname 拿到短
容器 id），**读不到自己的容器 id**；但 `readlink /proc/self/ns/pid` 在每个 lane、每个方向都在
（worker 读自己、agent 从宿主侧读候选，两者逐字相同——本片容器 lane 实测）。

为什么自报安全（控制器裁定里要求写进代码/报告的那条论证）：

1. **worker 在自己的 pid ns 里看不到宿主的 pid**，所以它**观测不到**同伴容器的 nsfs inode，也就无法
   用它替换自己的值；猜测一个 inode 只会让**自己**的建箱失败（fail closed），不会指向别人的进程。
2. 「我是节点 N」这件事本身已由身份层的**源 IP 第二因子**钉住（`_require_node_identity`），
   载体只是"哪一个进程"，不是"我是谁"。
3. 反向查的候选必须与该 inode **完全相等**，因此两个 worker 各有一个容器 pid 42 时不可能混。

失败模式（全部点名、全部 fail-closed）：

| 情形 | 结果 |
|---|---|
| 节点记录没有 pidns（老 worker / 非 Linux） | CP 503 `node N has reported no pid namespace identity...` |
| 指令缺 `worker` | agent 422（schema） |
| `worker.node_id` 与 URL 节点不符 | agent 400 点名 |
| pidns 形状不合法（register / 心跳） | CP 400 `pidNamespace must be a pid namespace identity...` |
| 容器 pid 已不在 | `沙箱 S 的槽位 pid 已不在` |
| 有 NSpid 命中但都不在目标 pid ns | `container pid N is not in worker W's pid namespace (...): refusing`（命中数只进日志，不进名字——同一台机器上邻居进程数会让名字漂移） |
| k8s lane 有命中但 cgroup 不含 `pod<uid>` | `... is in a process of worker W but not in pod U's cgroup: refusing` |
| 同 ns 同 cgroup 命中 > 1 | `... matches more than one process of worker W: refusing (ambiguous)` |
| k8s 取不到 pod UID / agent pod 缺失或两个 | resolver 返回 None → CP 503 `cannot determine the agent address...` |
| agent 卡住 / 不可达 / 拒绝 | 504 / 502 / 502 原文转发；worker 侧 `PrivHelperError` 点名 |
| worker 报 pid 后 CP 不可达 | 建箱失败并 kill 子进程（不留一个永远轮询的孩子） |

### 2.1 每个 lane 能拿到什么证据

| 证据 | k8s | compose（含 multinode） |
|---|---|---|
| worker 的 pidns inode | ✅ worker 自报（register/heartbeat） | ✅ 同 |
| worker pod/容器身份（更强证明） | ✅ **API**：pod `spec.nodeName` → 该 node 上的 agent pod，顺带取 worker pod `metadata.uid` | ❌ 无 API 来源（判定 (A) 与 (C) 都被裁定排除） |
| agent 侧能读到候选的 `ns/pid` / `cgroup` | ✅ hostPID + 同 uid 65534（本片容器 lane 实测可读） | ✅ 同（本片容器 lane 实测） |
| 判别值会不会"撞" | 不会：pidns 精确相等 + `pod<uid>` 双重 | 不会：pidns 精确相等（两个 worker 的 inode 不同） |

compose 那条绑定因此**只**立在"注册时自报 + 源 IP 第二因子"上——这是裁定明确接受的形状，理由写在
上面 1/2/3。

---

## 3. RED/GREEN（逐条）

全部命令在 `tmp/wt-c3` 下，宿主 lane 用
`/Users/polus/project/ai/sandlock-e2b/.venv/bin/python -m pytest`。

| 行为 | RED（实现前实测） | GREEN |
|---|---|---|
| 反查：两 worker 同容器 pid 不混 / pid 消失点名 / k8s cgroup 证明 / 身份不可用 | `tests/unit/test_c3_slot_identity_lookup.py` 收集即 `ModuleNotFoundError: No module named 'deploy.c3_agent.lookup'` | 10 passed |
| agent 服务：反查 → 写宿主 pid、写失败点名、缺身份 422、worker/node 不一致 400 | `tests/unit/test_c3_agent_service.py`：6 failed, 11 passed（`TypeError: create_app() got an unexpected keyword argument 'lookup'`） | 18 passed |
| CP 客户端/寻址：k8s pod→node→agent、并发旋钮、typed 超时、agent 原文转发 | `tests/unit/test_c3_agent_client.py`：`ModuleNotFoundError: No module named 'control_plane.c3_agent_client'` | 20 passed |
| CP 转发：记录里的 uid、`uid` 字段拒绝、身份层、对象校验、各跳点名 | `git stash push -u control_plane/` 后：`ModuleNotFoundError: No module named 'control_plane.c3_agent_client'` | 12 passed |
| worker：`E2B_SLOT_IDENTITY`、报告无 uid、无 agent 地址/token、池在 readiness 前报告、报告失败 kill、agent-grant 不用 broker | `git stash push -u envd_service/` 后：`ModuleNotFoundError: No module named 'envd_service.slot_identity'` | 17 passed |
| 容器 lane：同机两 worker 同容器 pid、真内核授予 ①②③ | 首跑真实失败：`assert '6' == '2'`（容器内子进程 pid 不是 2）+ 一处把容器 pid 当宿主 pid 用（`0::/../../init.scope` vs `0::/../6e7ed85b…`）——数字全部改为从实测读，不再由测试假设 | 4 passed |

`⑤`（报文不带 uid）＝ fake CP 精确断言 body 只有 `{sandbox_id, pid}`；`⑥/⑦`（不存在
worker↔agent 通道）＝ 配置/会话级：`Settings()` 无任何 `c3_agent*` 项、`envd_service/**` 不含
`E2B_C3_AGENT_URL`/`E2B_C3_AGENT_TOKEN`、身份客户端唯一目的地是 CP URL（连接层拒绝的真机臂在 slice B
的 NetworkPolicy）。

---

## 4. 命令与输出

```console
$ /Users/polus/project/ai/sandlock-e2b/.venv/bin/python -m pytest \
    tests/unit/test_c3_slot_identity_lookup.py tests/unit/test_c3_agent_client.py \
    tests/unit/test_c3_agent_service.py tests/unit/test_c3_slot_identity_forwarding.py \
    tests/unit/test_route_b_slot_identity.py tests/unit/test_sandlock_executor_route_b.py \
    tests/unit/test_route_b_wiring.py tests/unit/test_c3_internal_api_shape.py \
    tests/unit/test_node_address.py tests/contract/test_internal_identity.py -q -p no:randomly
188 passed, 1 skipped in 5.42s          # skip 是既有 fork submodule 门

$ docker run --rm -v /var/run/docker.sock:/var/run/docker.sock -v "$PWD:/workspace" -w /workspace \
    e2b-sandlock-test:latest python3 -m pytest \
    tests/unit/test_c3_slot_identity_lookup.py tests/unit/test_c3_agent_client.py \
    tests/unit/test_c3_agent_service.py tests/unit/test_c3_slot_identity_forwarding.py \
    tests/unit/test_route_b_slot_identity.py tests/contract/test_c3_slot_identity_grant.py -q
81 passed in 7.48s                     # 容器 lane：真内核 + 真 docker 拓扑

$ docker run --rm -v /var/run/docker.sock:/var/run/docker.sock -v "$PWD:/workspace" -w /workspace \
    e2b-sandlock-test:latest python3 -m pytest tests/contract/test_c3_slot_identity_grant.py -q
4 passed in 4.87s

$ /Users/polus/project/ai/sandlock-e2b/.venv/bin/python -m pytest tests/security -q -p no:randomly
2 failed, 17 passed, 38 skipped in 144.20s
  # 两条失败 = tests/security/escape/test_path_surface_inotify.py，baseline 同样两条失败（已用
  #   git stash 复验：`2 failed in 48.05s`），与本片无关

$ /Users/polus/project/ai/sandlock-e2b/.venv/bin/python -m pytest tests/unit -q \
    --ignore=tests/unit/test_pause_quota.py -p no:randomly
49 failed, 1651 passed, 30 skipped in 106.75s
  # 49 = 既有环境失败：28 test_priv_broker_protocol(AF_UNIX path too long) + 11 test_priv_helpers
  #   (chmod EPERM, 非 root macOS) + 3 test_migrate_state_base_script + 2 test_xfs_quotactl_backend
  #   + 2 test_gateway + 2 test_c2_p0_probe + 1 test_real_root_gate（stash 后逐文件复验合计 10 条，
  #   与 broker 的 39 条相加＝49；零回归）
  # 另：tests/unit/test_pause_quota.py 与 test_migrate_tenants.py 在宿主 lane 因缺 redis/fakeredis
  #   收集失败（既有环境问题，容器 lane 才有这两个依赖）
```

容器 lane 关键输出（`tests/contract/test_c3_slot_identity_grant.py`）：
两个 worker 子进程各自报 `C3-CHILD container_pid=6`（同一台机器上同号，真实撞号），各自
`readlink /proc/self/ns/pid` 不同；agent（`--pid=host`）用生产 lookup 解析出**两个不同的宿主 pid**，
每个宿主 cgroup 里含**自己那个 worker 的容器 id**；`as_uid` 授予后宿主侧 `stat -c %u` = `10009`、
槽位 `cgroup` 与 worker **逐字相同**、worker `CapEff: 0000000000000000`。

---

## 5. 文件

新增：

| 文件 | 内容 |
|---|---|
| `deploy/c3_agent/lookup.py` | D9.3 反查（纯 `/proc` 函数 + `WorkerIdentity` + 点名拒绝） |
| `control_plane/c3_agent_client.py` | D9.4 寻址（k8s/compose）+ D9.5 客户端（typed 超时 + 并发旋钮） |
| `envd_service/slot_identity.py` | 子进程那一半：unshare → 轮询 setresuid → exec |
| `envd_service/worker_identity.py` | worker 自报身份 + 报 CP 的 reporter 工厂 |
| `gateway_common/worker_identity.py` | pidns / pod UID 形状校验（两面共用） |
| `tests/unit/test_c3_slot_identity_lookup.py` | 反查 host lane（合成 /proc） |
| `tests/unit/test_c3_agent_client.py` | 寻址 + 客户端 + 与真 agent 服务的线格 |
| `tests/unit/test_c3_slot_identity_forwarding.py` | CP 转发端点与注册/心跳 plumbing |
| `tests/unit/test_route_b_slot_identity.py` | worker 侧：模式、报告、池接线、无 agent 地址/token |
| `tests/contract/test_c3_slot_identity_grant.py` | 容器 lane：真内核、两 worker 同容器 pid、①②③ |

修改：`control_plane/api/internal.py`（新端点 + register/heartbeat 的 `pidNamespace` + 模块 docstring）、
`control_plane/registry/nodes.py`（`pid_namespace`）、`control_plane/config.py`（7 个 `E2B_C3_AGENT_*`）、
`control_plane/app.py`（构建/注入客户端）、`deploy/c3_agent/app.py`（身份、反查、点名）、
`envd_service/route_b.py`（模式、starter、报告）、`envd_service/priv_helpers.py`（`request_identity`）、
`envd_service/agent.py`（`pidNamespace` 上报）、`envd_service/config.py`（2 个设置）、
`envd_service/executors/sandlock.py`（decline 分支）、
`tests/unit/test_c3_agent_service.py`（+4 条、更新既有 body 形状）、
`tests/unit/test_sandlock_executor_route_b.py`（+1 条 agent-grant 选择用例）。

### 5.1 新增的旋钮（slice B 要写进清单/文档）

| 变量 | 谁 | 默认 | 说明 |
|---|---|---|---|
| `E2B_SLOT_IDENTITY` | worker | `spawn` | `agent-grant` 开 C3 路径 |
| `E2B_SLOT_IDENTITY_REPORT_TIMEOUT_S` | worker | 10.0 | 一次上报的截止（CP→agent 的截止在它里面） |
| `E2B_SLOT_IDENTITY_WAIT_TIMEOUT_S` | 子进程 | 30.0 | 轮询 `setresuid` 的兜底 |
| `E2B_C3_AGENT_URL` / `_NAMESPACE` / `_LABEL` / `_PORT` | CP | 无 / sandlock / `app=c3-agent` / 49985 | compose 服务名；k8s 用 label + ns |
| `E2B_C3_AGENT_TOKEN` | CP | 空 | 空 = 拒绝指令（点名 503） |
| `E2B_C3_AGENT_TIMEOUT_S` | CP | 5.0 | 一次 CP→agent 指令 |
| `E2B_C3_AGENT_MAX_CONCURRENCY` | CP | 0（不限） | 判据 16 的旋钮；反面臂压到 1 必须复现排队 |

---

## 6. 自审

- **两条通道**：worker 侧没有 agent 地址/token（配置级 + 源码级 pin），身份报告唯一目的地是 CP；
  CP→agent 的寻址来自 API/清单，绝不来自请求体。`worker↔agent` 一行都没有新增。
- **硬规则 1/4/5**：槽位由 worker fork（cgroup 留在 worker，容器 lane 逐字验证）；uid 只从 CP 记录
  来（body 带 `uid` → 400）；身份只给沙箱自己的进程（`as_uid` 只写子进程的 map）。
- **禁项**：没有引入 `SYS_ADMIN`/`SYS_PTRACE`/`NET_RAW`/`privileged`/`hostNetwork`/
  `allowPrivilegeEscalation`；本片未改任何 manifest。
- **精确断言**：所有新断言都是 `==` / 逐字字符串；唯一两处"形状"断言用 `re.fullmatch` 卡数字
  （宿主 pid、`C3-RESOLVED host=\d+`），因为数字由内核给，测试不假设。
- **不做静默降级**：身份/寻址/超时/拒绝每条都有名字；pid 消失有自己的中文名。
- **没有削弱既有期望**：`test_c3_agent_service.py` 的改动是给 `_body()` 补 `worker`（接口由本 task
  改变）并新增用例，断言本身仍逐字；`test_sandlock_executor_route_b.py` 只增不改；老 decline 文案
  逐字保留（新增一条 agent-grant 专用分支）。
- **日志驱动**：容器 lane 首跑的两个失败都是靠读它打出的真实数字定位的（`container_pid=6`、
  `init.scope`），不是猜。

---

## 7. 顾虑 / 留给 slice B

1. **真机验收**：判据 1/2/3/7 的 k8s 臂、13/16 的 compose multinode 臂、以及 ⑥ 的连接层拒绝
   （NetworkPolicy）都要在 slice B 跑；本片的容器 lane 是它们的"本地同形"预演。
2. **判据 16 的并发上限**：`E2B_C3_AGENT_MAX_CONCURRENCY` 本片默认 0（不限），出厂值需按该形态最大
   并发建箱数定；反面臂（压到 1 必须复现排队）在 slice B。
3. **RBAC（D8）**：k8s 寻址需要 CP 的 SA 有 `get pods` + `list pods`（Task 2 已加了 worker pod 的
   `get pods`；本片新增了"按 label 列 agent pod"，slice B 核对权限是否够）。
4. **`E2B_CONTROL_PLANE_URL` / `E2B_NODE_ID`**：`agent-grant` 的 reporter 从这两个（settings 优先，
   环境兜底）解析；清单里它们已存在，slice B 加 `E2B_SLOT_IDENTITY` 时确认二者仍在。
5. **agent 与 worker 都必须"天生 65534"**：容器 lane 用 `--user 65534:65534` 复现；一旦有人改成
   "root 启动再降权"，反查仍能命中但 `as_uid` 的写会变成 EACCES（`docs/c3-privilege-relocation.md`
   §14.2.7 第 1 条），这条要靠清单 + 既有 pin 守住。
6. **`local://` 形态**：本片未触碰（不在 C3 覆盖范围）；compiled compose 的 `docker-compose.yml`
   未加 agent 服务（slice B）。
7. **小项**：`slot_pool_for` 的缓存键含 `slot_identity` 但不含 reporter 实例（同一 worker 内 reporter
   由 settings 决定，实际不会变）；agent 的 `worker.node_id` 与 URL 节点一致性检查是本片新加的
   纵深防御（CP 两侧同源，永不触发）。
8. **文档**：`docs/deploy-clusters.md` 现状节与新旋钮表归 slice B（本片未改文档）。

---

## 8. 修复轮（评审 Needs fixes → 2 Important + 6 minors）

### 8.1 IMPORTANT 1：上报与子进程 `unshare` 的竞态（裁定 D11）

**问题（评审复现）**：`route_b` 在 `Popen` 返回后立刻上报，而 `Popen` 只保证 `execve`；
子进程的 `unshare(CLONE_NEWUSER)` 发生在解释器起来之后。授予先到时 `as_uid` 读到初始命名空间的
全量 map（`as_uid.c` 的拒绝 3）→ agent 502 → 池杀子进程 → 建箱间歇性失败。

**改法（在源头修）**：子进程带一条**握手管道**，worker 只在该字节到达后才上报。

| 位置 | 改动 |
|---|---|
| `envd_service/slot_identity.py::child_argv(..., unshared_fd=)` | 新增 `--unshared-fd N`，fd 由 `pass_fds` 带过解释器启动 |
| `slot_identity.main()` | `unshare` 成功 → `signal_unshared(fd)`（写 1 字节 + **关闭**，绝不留到 supervise 的 fd 表）→ 才开始轮询 `setresuid` |
| `slot_identity.await_unshared(read_fd, pid=, timeout_s=)` | 有界等待；超时 = `the slot child (pid N) did not report its user namespace within Xs: refusing (the identity grant would race the unshare)`；EOF = `... exited before reporting its user namespace: refusing` |
| `slot_identity.spawn_child(...)` | worker 侧唯一入口：建管道 → `Popen(pass_fds=(...， write_fd))` → 父进程关写端 → `await_unshared` → 失败即 **kill 子进程** 再抛 |
| `route_b._spawn_slot_identity` | 改为调用 `spawn_child`；池的上报仍在 spawner 返回之后 → **语义上就是"unshare 之后才上报"** |
| 旋钮 | `E2B_SLOT_IDENTITY_UNSHARED_TIMEOUT_S`（默认 10s，命名超时） |

**没有**加无界重试；agent 侧一行都没为这个竞态改动（D11 的"不许用重试糊过去"）。

**顺序的可证伪证据（两条 lane 都跑了 RED）**：

- 宿主 lane：把 `await_unshared` 改成立刻返回（= 旧的"`Popen` 后立即上报"），4 条顺序用例全红：

```console
$ ... -m pytest tests/unit/test_route_b_slot_identity.py -q -p no:randomly -k "handshake or signals or never_signals or dies_before"
E           assert (2565516.8786252 - 2565516.871148876) >= 0.4     # 报告早于子进程的信号
4 failed, 17 deselected in 0.20s
```

- 容器 lane：同一次"拆掉握手"的状态下，慢子进程那条臂红：

```console
$ docker run ... e2b-sandlock-test:latest pytest tests/contract/test_c3_slot_identity_grant.py -k "slow_childs_unshare or precedes_the_unshare"
>       assert elapsed >= delay
E       assert 0.5531067647971213 >= 1.0        # 上报出现在子进程（延迟 1s）unshare 之前
1 failed, 1 passed, 9 deselected in 2.62s
```

- 恢复握手后：宿主 21 passed / 容器 11 passed（同一批命令，逐条列出见 §8.4）。

**为什么容器那条臂要"延迟 1 秒"**：lane 自己的上报路径里有一次日志轮询 + `docker exec`
（≥0.25s），比 CPython 启动（几十 ms）还慢，所以**真实子进程**在"拆掉握手"的状态下**也可能**
侥幸通过 —— 那不是可靠的 RED。延迟臂把子进程的 `unshare` 推到 1s 之后（包裹脚本只延迟、不改
握手：fd 跨两次 exec 存活，字节仍由生产模块在真 unshare 之后发出），于是"上报早于 unshare"
变成**确定性**失败。真实子进程不靠运气，由 §8.2 的 5 次迭代覆盖。

### 8.2 IMPORTANT 2：生产子进程路径第一次被真正执行（裁定 D11.2）

新增 `tests/contract/c3_slot_child_driver.py`（lane 的 worker 侧驱动，跑在 65534、零能力、
挂载本仓库的容器里）与 3 条用例：

| 用例 | 覆盖 |
|---|---|
| `test_the_production_child_path_grants_then_execs`（**参数化 5 次**） | 真 `_spawn_slot_identity` → 真 `child_argv` → `python -m envd_service.slot_identity` → argv 解析、握手、`unshare`、轮询 `setresuid`、fd 跨解释器启动存活 → 真 `as_uid` 授予 → `execv`（打印 `C3-SLOT-EXEC-OK uid=X pidns=<worker 的 pidns>`）→ 宿主侧 `stat` 出池 uid |
| `test_the_report_waits_for_a_slow_childs_unshare` | 延迟臂：上报时刻 ≥ 子进程延迟（RED 见 §8.1） |
| `test_a_grant_that_precedes_the_unshare_is_refused_by_name` | **反面臂**：旧的"`Popen` 后立即上报"顺序下，`as_uid` 逐字拒绝 `as_uid: refused: uid_map for pid N is the initial namespace's full range: this pid has not unshared a user namespace, so there is no new identity to grant`（exit 77） |

驱动用生产模块（`envd_service.route_b._spawn_slot_identity` 与 `slot_identity.child_argv` /
`await_unshared`），自己只做"当 worker、把 pid 报出来"这一件事；仓库在 lane 里通过
`docker inspect` 找到**宿主机上的**路径再挂进 worker 容器（容器内的 `/workspace` 不能被嵌套
`docker run` 直接使用）。

### 8.3 minors（6 条）

1. **容器 lane 的 5 处 `in` 断言 → 精确**：槽位 cgroup 现在与**该 worker 自己的 cgroup 逐字相等**、
   两个 worker 的 cgroup **互不相等**，并用 `_cgroup_owner()` 把叶节点（`docker-<id>.scope` /
   `<id>` / `crio-` / `cri-containerd-`）规范后与 `docker inspect` 的容器 id **精确相等**。
2. **CP→agent 客户端的 2xx 非对象应答**：不再包成 `{"answer": ...}` 当成功，改为点名
   `502 the agent for node N answered with a list, not an instruction answer`（非 JSON 也点名）；
   两条新用例覆盖。
3. **agent 服务测试的 stub 文案**：改成与真 lookup **同样的拼写**，并新增
   `test_the_real_lookup_names_its_refusals_through_the_service` —— 用合成 `/proc` 驱动**真**
   `ProcLookup` 穿过服务，文案漂移会在这条上现形。
4. **worker 的"没有 agent 地址/token"用例**：在源码扫描里加上 **agent 端口 49985**（比只扫变量名更
   强，硬编码 URL 也会被抓），并在 docstring 里点明它**不覆盖**什么（真机连接层拒绝 / manifest，
   归 slice B）。
5. **`present()` 的 pid 复用漏洞**：`host_pid()` 现在返回 `SlotProcess(host_pid, start_time,
   pid_namespace)`（`stat` 第 22 字段 = 该 pid 实例的出生时刻），新增
   `still_alive(slot)`：pid 没了、**或**同一个号被复用（start time 变了）、**或**换了 ns → 一律
   False，于是"沙箱 S 的槽位 pid 已不在"这个名字与事实一致。容器 lane 的
   `_resolved_pid()` 断言 `C3-RESOLVED host=<pid> start=<start>` 形状，顺带钉住真内核上的解析。
6. **`build_identity_reporter` 的取值来源**：代码与文档都改为**环境**
   （`E2B_CONTROL_PLANE_URL` / `E2B_NODE_ID`，与 node agent 注册用的是同两个）；`envd_service`
   的 `Settings` 本来就没有这两个字段，之前的 `getattr(settings, ...)` 是死代码。新用例按环境驱动。
   **订正 §1.4 的措辞**：那里写的"settings 优先，环境兜底"不成立，实际只有"显式参数（测试/嵌入）
   优先，否则读环境"。

### 8.4 复验命令与输出（修复轮）

```console
$ /Users/polus/project/ai/sandlock-e2b/.venv/bin/python -m pytest \
    tests/unit/test_c3_slot_identity_lookup.py tests/unit/test_c3_agent_client.py \
    tests/unit/test_c3_agent_service.py tests/unit/test_c3_slot_identity_forwarding.py \
    tests/unit/test_route_b_slot_identity.py tests/unit/test_sandlock_executor_route_b.py \
    tests/unit/test_route_b_wiring.py tests/unit/test_c3_internal_api_shape.py \
    tests/unit/test_node_address.py tests/contract/test_internal_identity.py \
    tests/contract/test_multinode.py tests/contract/test_control_plane.py -q -p no:randomly
218 passed, 1 skipped in 9.63s

$ docker run --rm -v /var/run/docker.sock:/var/run/docker.sock -v "$PWD:/workspace" -w /workspace \
    e2b-sandlock-test:latest python3 -m pytest \
    tests/contract/test_c3_slot_identity_grant.py tests/unit/test_c3_slot_identity_lookup.py \
    tests/unit/test_c3_agent_client.py tests/unit/test_c3_agent_service.py \
    tests/unit/test_c3_slot_identity_forwarding.py tests/unit/test_route_b_slot_identity.py -q
96 passed in 24.64s

$ docker run ... e2b-sandlock-test:latest pytest tests/contract/test_c3_slot_identity_grant.py -q
11 passed in 18.80s

$ /Users/polus/project/ai/sandlock-e2b/.venv/bin/python -m pytest tests/unit -q \
    --ignore=tests/unit/test_pause_quota.py -p no:randomly
49 failed, 1662 passed, 30 skipped in 118.05s    # 与修复前完全同一组 49 条（28+11+3+2+2+2+1），零回归

$ /Users/polus/project/ai/sandlock-e2b/.venv/bin/python -m pytest tests/security -q -p no:randomly
2 failed, 17 passed, 38 skipped    # 同样是 baseline 那 2 条 inotify，与本片无关
```

### 8.5 新增/改动的旋钮与文件（本轮）

- 新旋钮：`E2B_SLOT_IDENTITY_UNSHARED_TIMEOUT_S`（worker，默认 10s）= 握手等待上界。
- 新文件：`tests/contract/c3_slot_child_driver.py`（lane 的 worker 侧驱动）。
- 改动：`envd_service/slot_identity.py`（握手 + `spawn_child`）、`envd_service/route_b.py`
  （starter 走 `spawn_child`）、`deploy/c3_agent/lookup.py`（`SlotProcess` / `still_alive`）、
  `deploy/c3_agent/app.py`（用 `SlotProcess`）、`envd_service/worker_identity.py`（取值来源）、
  `control_plane/c3_agent_client.py`（非对象应答点名拒绝），以及四个测试文件。

### 8.6 仍留给 slice B（不变）

manifest / NetworkPolicy / RBAC / 真机验收 / `docs/deploy-clusters.md` / 并发上限出厂值；
另外 §8.1 的容器 lane 需要**延迟臂**才能确定性证伪这件事本身，也是 slice B 在真机上可以用真实
CP→agent 时延再压一次的点（真机 hop 比 lane 的 `docker exec` 快，天然更接近竞态窗口）。
