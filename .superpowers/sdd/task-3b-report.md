# Task 3 / slice B 报告：per-node agent 的部署面（k8s / compose / 构建 / 凭据）

- 工作树：`/Users/polus/project/ai/sandlock-e2b/tmp/wt-c3`（分支 `feat/c3-consolidation`）
- BASE：`a0e3f89`（slice A 三个修复提交之后的 HEAD）
- 本片提交：`e7e1dbd`（25 files changed, 1319 insertions(+), 77 deletions(-)）
- 裁定执行：**D12**（agent 身份 = 宿主机名）、**D13**（一个 DaemonSet、两容器、pod 级 `hostPID`）、
  **D14**（两个分离 compose 栈各一个 agent 服务）、**D15**（构建接线）、**D16**（并发旋钮出厂值）
- 本片**未碰任何集群**，未 push、未 merge、未 deploy；`local://` 形态未动。

---

## 1. 实现了什么

### 1.1 k8s 形态（`deploy/k8s/c3-agent.yaml`，新）

一个 `DaemonSet` `e2b-c3-agent`（`app: c3-agent`）+ 一个 `NetworkPolicy`，同文件两段：

| 面 | 容器 | uid | capabilities | 挂载 | 身份 |
|---|---|---|---|---|---|
| A（身份授予） | `agent` | `runAsUser: 65534` | `add: [SETUID, SETGID]`（BND 声明，非 root 进程 `CapEff` 仍为 0） | **无** | `E2B_C3_AGENT_NODE_ID` ← `spec.nodeName`；`E2B_C3_AGENT_TOKEN` ← Secret |
| B（文件操作） | `maint` | `runAsUser: 0` | `drop: [ALL]` + `add: [CHOWN, DAC_OVERRIDE, FOWNER]` | `sandbox-shared` PVC + `/var/lib/e2b-images` hostPath | 四个白名单根 env（`priv_common.c` 纪律） |

- pod 级 `hostPID: true`（face A 的反查要读宿主 `/proc`；face B 因此也拿到它 —— `plan §2.0`
  点名接受的增量，写进清单注释）。
- face B 的载荷（`e2b-maint` verb 服务）**归 Task 4**；现在它只**持有已评审的能力集与挂载**
  （`command: sleep infinity`，不听端口）—— 一个"root + 三条能力却什么都不做"的容器比
  "已能删树但还没接客户端"的守护进程更容易在评审里看清。
- 禁项逐条成立：无 `SYS_ADMIN`/`SYS_PTRACE`/`NET_RAW`/`privileged`/`hostNetwork`；**且整个文件
  里不出现 `allowPrivilegeEscalation`/`no-new-privileges`**（设为 `false` 会让内核静默忽略 file
  capabilities，face A 会"装好了却永远授不出身份"）。
- 探针用 `exec`（容器内自连 `127.0.0.1:49985`）：kubelet 的 `httpGet` 从节点发起，会被
  下面那条"只许 CP 进"的 NetworkPolicy 挡在半路。
- `NetworkPolicy`：`policyTypes: [Ingress]`，`from` 只有 `podSelector: {app: control-plane}`，
  端口只有 `49985` —— "只有两条通道"的**连接层**那一半（`worker ↔ agent` 在连接层就不存在）。

`deploy/k8s/kustomization.yaml`：加入基线（与 `priv-broker` 同理由 —— 它的 PVC claim 与 hostPath
都是基线已有的，与发行版/存储类型无关；k0s overlay 无需改动即继承）。

`deploy/k8s/control-plane.yaml`：

- RBAC `control-plane-pod-reader` 的 verbs 由 `["get"]` 扩到 `["get", "list"]`（D13：寻址要按
  label 列**本节点**的 agent pod；`get` 只能答 worker pod 的 `nodeName`）。范围不变
  （本命名空间的 `pods`，仍是 namespaced Role）。
- 新增 env：`E2B_C3_AGENT_LABEL=app=c3-agent`、`E2B_C3_AGENT_NAMESPACE=sandlock`、
  `E2B_C3_AGENT_TOKEN`（`secretKeyRef`）、`E2B_C3_AGENT_MAX_CONCURRENCY=64`（D16）。

`deploy/k8s/worker.yaml`：新增 `E2B_SLOT_IDENTITY=agent-grant`（回退 = 改回 `spawn`）；容器
**不带** `hostPID`、**不带** agent token/URL（含注释里也点明为什么不能给 `hostPID`）。

`deploy/k8s-k0s/apply.sh`：rollout 闸门由 `broker → worker` 改成 **`broker → agent → worker`**
（agent 是 worker 的新上游，`agent-grant` 下 fail-closed 没有回落路径）。

### 1.2 凭据（`deploy/k8s-k0s/secrets.sh`）

新增托管键 `E2B_C3_AGENT_TOKEN`（`KEYS=(...)` 里第 5 个）：**只出现在 control-plane 与 agent 两处**，
worker 清单/镜像里一个字都没有。agent 服务缺它拒启（`deploy/c3_agent/__main__.py`）。

### 1.3 compose 形态（`deploy/compose/docker-compose.{prod,multinode}.yml`）

每个栈加两个服务（compose 没有 pod，两个面拆成两个服务）：

- `c3-agent`：`image: ${AGENT_IMAGE:-e2b-sandlock-agent:0.1.0}`、`pid: host`（= k8s 的
  `hostPID`）、`user: 65534:65534`、`cap_drop:[ALL]` + `cap_add:[SETUID,SETGID]`、
  `E2B_C3_AGENT_NODE_ID=c3-agent`、`E2B_C3_AGENT_TOKEN`。
- `c3-agent-maint`：`user: 0:0`、`cap_drop:[ALL]` + `cap_add:[CHOWN,DAC_OVERRIDE,FOWNER]`、
  四个白名单根 env、挂 worker 数据卷（`worker-data`/`sandbox-shared`）。

control-plane 服务加 `E2B_C3_AGENT_URL=${E2B_C3_AGENT_URL:-http://c3-agent:49985}`、
`E2B_C3_AGENT_TOKEN`、`E2B_C3_AGENT_MAX_CONCURRENCY=${E2B_C3_AGENT_MAX_CONCURRENCY:-64}`。
三个 worker 各加 `E2B_SLOT_IDENTITY: agent-grant`，**不带**任何 agent 地址/token。

`deploy/compose/.env.example` 增 `AGENT_IMAGE` 与 `E2B_C3_AGENT_TOKEN`（含注释）。`local://`
形态（`deploy/compose/docker-compose.yml`、`docker-compose.autoscale.yml`）**未动**。

### 1.4 构建接线（D15）

`deploy/scripts/build-images.sh` 新增 `e2b-sandlock-agent` 的 buildx 段（`Dockerfile.agent`）
与结束语一行；`deploy/scripts/build-and-push.sh` 的命名约定行加入 `agent`（该脚本复用
`build-images.sh`）。命名沿用 `$REGISTRY/e2b-sandlock-<service>:$VERSION`。

### 1.5 并发旋钮出厂值（D16）

`control_plane/config.py::c3_agent_max_concurrency` 默认由 `0`（不限）改为 **`64`**，理由写在
代码与两份清单的注释里（双侧夹逼，不是拍脑袋）：

- **下界**：≥ 单个控制面可能同时挂起的槽位启动数 —— autoscaler 上限 16 个 worker 副本，
  create 准入上限 100（`E2B_CREATE_QUEUE_MAX`）⇒ 信号量不能是第一个让建箱等待的东西。
- **上界**：agent 是**同步** FastAPI 服务，handler 跑在 anyio 线程池（默认 40）——
  超过 40 在 agent 侧买不到吞吐，低于 40 会把 agent 本可并行服务的授予排起队。
- 64 落在 agent 的 40 之上、CP 的 100 之下。切片 B2 用 N-并发建箱对照这个值（并把它压到 1
  复现排队），要改就是这个数。

---

## 2. D12 的寻址改动（本片唯一的语义改动）

**改动前**（slice A）：agent 的自我身份与寻址都用 **worker pod 名**（`node_id`）。

**改动后**（D12）：**agent 的身份 = 它所在的宿主机名**，因为 agent 是**每节点** DaemonSet，
而一个节点上可以有多个 worker（k8s 2 副本；multinode compose 三个 worker 同机）。用
worker pod 名当身份会错误或歧义。

两条身份在**两个地方**分别承载：

| 身份 | 值 | 承载位置 | 谁决定 |
|---|---|---|---|
| **agent（本机）** | `spec.nodeName`（k8s）/ 服务名（compose） | 指令 URL 的 `{node_id}` 段 | CP 的解析器（从 API/清单来，从不从请求体来） |
| **worker（上报 pid 的那个）** | worker 的 node_id（== pod 名）+ pod UID + 它记的 pidns | 指令**体**的 `worker` 字段 | CP 的节点记录（register/heartbeat 自报 + API 取 pod UID） |

代码落点：

- `control_plane/c3_agent_client.py`：`AgentTarget` 新增 `node_identity` 字段；
  `K8sAgentAddressResolver.resolve` 填 `node_identity = node_name`（`spec.nodeName`）；
  `ComposeAgentAddressResolver.resolve` 填 `node_identity = urlsplit(url).hostname`；
  `_post` 用 `target.node_identity` 拼 URL；`grant_slot` 前加一道 `validate_node_id(node_identity)`
  （否则会打到 `/internal/nodes//agent/...`）。指令体里的 `worker.node_id` 仍是 worker 名。
- `deploy/c3_agent/app.py`：**删掉**原来的 `body.worker.node_id != node_id → 400` 比对
  （两者本就命名不同的事物）；改为 `validate_node_id(body.worker.node_id)` 的形状检查
  （hostile 串不进日志）。URL 段仍与 `settings.node_id`（= 本机身份）比对 —— agent 仍只应答
  发给自己的指令。
- `deploy/c3_agent/config.py`：`node_id` 的注释改成"这是**主机**名（D12），不是 worker pod 名"。
- `gateway_common` 的 `validate_node_id` 直接复用（k8s 节点名与 pod 名同形状，`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`）。

**"worker ↔ agent 不存在"这条性质照旧成立**：agent 仍无 worker 面向的凭据路径；NetworkPolicy
是它的连接层执行点。

---

## 3. RED / GREEN（逐条 pin）

全部命令在 `tmp/wt-c3` 下；宿主 lane 用 `/Users/polus/project/ai/sandlock-e2b/.venv/bin/python -m pytest`。

### 3.1 新 pin 文件：`tests/unit/test_c3_agent_manifest.py`

**RED**（清单与构建脚本都还没动）：

```console
$ ... -m pytest tests/unit/test_c3_agent_manifest.py -q -p no:randomly
FAILED ...::test_the_agent_is_one_daemonset_with_two_containers_and_a_pod_scoped_host_pid
FAILED ...::test_face_a_is_the_unprivileged_identity_giver
FAILED ...::test_face_b_is_the_file_face_with_c1s_capability_set
FAILED ...::test_face_a_carries_no_mounts_and_the_pod_ships_only_the_two_it_needs
FAILED ...::test_the_agent_manifest_never_names_a_forbidden_privilege
FAILED ...::test_the_networkpolicy_names_the_control_plane_as_the_only_ingress
FAILED ...::test_the_agent_is_in_the_baseline_kustomization
FAILED ...::test_the_control_plane_role_may_read_and_list_pods
FAILED ...::test_the_control_plane_is_given_the_agent_channel_and_a_sized_limit
FAILED ...::test_the_worker_is_on_the_agent_grant_path_and_carries_no_agent_secret
FAILED ...::test_no_pod_other_than_the_agent_claims_the_agent_label
FAILED ...::test_each_compose_stack_runs_exactly_one_agent_facing_the_control_plane
FAILED ...::test_the_compose_control_plane_dials_the_agent_by_service_name
FAILED ...::test_no_compose_worker_receives_the_agent_token_or_the_agent_identity
FAILED ...::test_the_agent_image_is_built_by_the_repos_own_scripts
FAILED ...::test_the_build_and_push_script_names_the_agent_image
16 failed, 2 passed in 0.63s
```

**GREEN**（清单 + 构建脚本 + CP env/RBAC 落地后）：

```console
$ ... -m pytest tests/unit/test_c3_agent_manifest.py -q -p no:randomly
....................                                                     [100%]
20 passed in 0.47s
```

### 3.2 D12 改动的 RED / GREEN

把 pin（client / agent 服务）先改成 D12 期望的形状（URL 段=主机名、体里=worker 名），实现还没改：

```console
$ ... -m pytest tests/unit/test_c3_agent_manifest.py tests/unit/test_c3_agent_client.py \
    tests/unit/test_c3_agent_service.py tests/unit/test_c3_slot_identity_forwarding.py -q -p no:randomly
FAILED .../test_c3_agent_service.py::test_an_instruction_whose_worker_disagrees_with_its_node_is_refused
   （改成 403 主机比对 / 200 接受异名 worker 后 RED）
FAILED .../test_c3_agent_client.py::test_the_agent_is_found_through_the_workers_own_node  （AgentTarget 缺 node_identity）
FAILED .../test_c3_agent_client.py::test_the_compose_agent_is_the_configured_service_name
FAILED .../test_c3_agent_client.py::test_the_instruction_addresses_the_host_and_names_the_worker_in_the_body
FAILED .../test_c3_agent_client.py::test_the_real_agent_service_accepts_the_clients_instruction
10 failed, 61 passed in 3.29s
```

实现落地后（GREEN）：

```console
$ ... -m pytest tests/unit/test_c3_agent_client.py tests/unit/test_c3_agent_service.py \
    tests/unit/test_c3_slot_identity_forwarding.py -q -p no:randomly
55 passed in 2.30s
```

`test_c3_agent_service.py` 的两条改动是**接口变了**（D12）的必然后果：
`test_an_instruction_for_another_host_is_refused_named`（URL 段=主机名时 403）、
`test_a_worker_that_is_not_this_host_is_accepted`（异名 worker 现在**必须**被接受）+ 新增
`test_a_worker_identity_that_is_not_a_node_id_is_refused_named`（形状检查）。

### 3.3 既有 pin 的同步更新（都是"行为变了、pin 跟着变"，不是放宽）

| pin | 旧 | 新 | 为什么 |
|---|---|---|---|
| `test_c3_internal_api_shape.py::test_the_k8s_control_plane_can_read_pods_and_nothing_else` | `verbs: ["get"]` | `verbs: ["get","list"]` | D13：寻址要按 label 列 agent pod |
| `test_c3_internal_api_shape.py::test_no_worker_shape_carries_the_agent_token` | 两个 compose 栈整体扫文本 | 逐服务扫（worker 服务的 env） | 这两个栈里 CP 与 worker 同文件，token 合法地属于 CP；worker 服务仍逐字无 token |
| `test_k0s_secrets_script.py` | `MANAGED_KEYS` 4 个 | 5 个（+`E2B_C3_AGENT_TOKEN`） | 脚本新增托管键；指纹/保留行随之更新 |
| `test_worker_env_key_sets.py` | `E2B_SLOT_IDENTITY` 未分类 | 新类 `slot_identity`；两个 C3 compose 栈**声明**它，其余栈继承默认 `spawn` | N45 的"形状键必须显式分类"契约 |
| `test_worker_manifest_permissions.py` | 表 3 两行 | 表 3 三行（+`E2B_C3_AGENT_TOKEN` 行） | 与 `docs/k8s-deployment.md` 表 3 逐字一致 |

### 3.4 捆绑测试全档（GREEN）

```console
$ ... -m pytest tests/unit/test_c3_agent_manifest.py tests/unit/test_c3_internal_api_shape.py \
    tests/unit/test_worker_manifest_permissions.py tests/unit/test_k0s_secrets_script.py \
    tests/unit/test_worker_env_key_sets.py tests/unit/test_c3_agent_client.py \
    tests/unit/test_c3_agent_service.py tests/unit/test_c3_slot_identity_forwarding.py \
    tests/unit/test_c3_slot_identity_lookup.py tests/unit/test_compose_base_image_shape.py \
    tests/unit/test_deploy_env_examples_are_ignored.py tests/unit/test_route_b_slot_identity.py \
    tests/unit/test_sandlock_executor_route_b.py tests/unit/test_route_b_wiring.py \
    tests/unit/test_node_address.py tests/unit/test_autoscaler_local_backend_shape.py -q -p no:randomly
309 passed, 1 skipped in 24.07s
# skip = 既有 fork submodule 门（test_route_b_wiring.py:243）
```

---

## 4. 命令与输出

### 4.1 两个 kustomize 渲染（本片要求的 render 检查）

```console
$ kubectl kustomize deploy/k8s > tmp/k8s-render.yaml ; echo rc=$?
rc=0
# 24 个 kind；其中新增：
#   DaemonSet e2b-c3-agent
#   NetworkPolicy e2b-c3-agent

$ kubectl kustomize deploy/k8s-k0s > tmp/k0s-render.yaml ; echo rc=$?
rc=0
# 26 个 kind；c3-agent 的 DaemonSet/NetworkPolicy 原样继承（overlay 无额外改动）
```

渲染出的 agent 对象（用 yaml 解析核对，不是 grep）：

```console
hostPID True
agent  registry.cn-shanghai.aliyuncs.com/byteplan/e2b-sandlock-agent:0.1.0
       {'capabilities': {'add': ['SETUID', 'SETGID']}, 'runAsUser': 65534}  mounts []
maint  registry.cn-shanghai.aliyuncs.com/byteplan/e2b-sandlock-agent:0.1.0
       {'capabilities': {'add': ['CHOWN','DAC_OVERRIDE','FOWNER'], 'drop': ['ALL']}, 'runAsUser': 0}
       mounts ['shared', 'image-cache']
netpol podSelector {app: c3-agent}; policyTypes [Ingress];
       ingress[0].from = [{podSelector:{matchLabels:{app: control-plane}}}]; port 49985 TCP
CP C3 env {'E2B_C3_AGENT_LABEL':'app=c3-agent','E2B_C3_AGENT_NAMESPACE':'sandlock',
           'E2B_C3_AGENT_TOKEN': <secretKeyRef e2b-secrets/E2B_C3_AGENT_TOKEN>,
           'E2B_C3_AGENT_MAX_CONCURRENCY':'64'}
Role rules [{'apiGroups':[''], 'resources':['pods'], 'verbs':['get','list']}]
```

### 4.2 服务端 dry-run（基线渲染喂给 kubectl）

```console
$ kubectl apply --dry-run=client -f tmp/k8s-render.yaml 2>&1 | tail
...
daemonset.apps/e2b-c3-agent created (dry run)
daemonset.apps/e2b-priv-broker created (dry run)
daemonset.apps/seccomp-installer created (dry run)
networkpolicy.networking.k8s.io/e2b-c3-agent created (dry run)
# 无 error/schema 报错；rc=0
```

### 4.3 compose 渲染（两个分离栈）

```console
$ (cd deploy/compose && docker compose --env-file .env.example -f docker-compose.prod.yml config) > tmp/compose-prod-render.yaml
rc=0
services: ['c3-agent','c3-agent-maint','control-plane','image-cache-init','redis','worker-1','worker-2','worker-3']
c3-agent       image=…/e2b-sandlock-agent:0.1.0 user=65534:65534 cap_drop=['ALL'] cap_add=['SETUID','SETGID'] pid=host
c3-agent-maint image=…/e2b-sandlock-agent:0.1.0 user=0:0       cap_drop=['ALL'] cap_add=['CHOWN','DAC_OVERRIDE','FOWNER']
CP agent env   {'E2B_C3_AGENT_URL':'http://c3-agent:49985','E2B_C3_AGENT_TOKEN':'c3-agent-token','E2B_C3_AGENT_MAX_CONCURRENCY':'64'}

$ (cd deploy/compose && docker compose --env-file .env.example -f docker-compose.multinode.yml config) > tmp/compose-mn-render.yaml
rc=0
services: 同上（含 c3-agent / c3-agent-maint）
worker-1/2/3 E2B_SLOT_IDENTITY=agent-grant ；token? False（三个 worker 都没有 token）
c3-agent-maint mounts: worker-data -> /var/lib/e2b-sandboxes
```

`deploy/scripts/build-images.sh` 新增 `e2b-sandlock-agent` 段（未在宿主 lane 真跑 buildx：
多平台构建要 registry，且本片不部署；接线由 4.1/3.1 的 pin 断言）。

### 4.4 全量 unit 回归（对照既有 49 红基线）

```console
$ ... -m pytest tests/unit -q --ignore=tests/unit/test_pause_quota.py -p no:randomly
49 failed, 1684 passed, 30 skipped in 110.39s
# 49 = 既有环境失败，逐文件与 slice A 报告一致：
#   28 test_priv_broker_protocol（AF_UNIX path too long）+ 11 test_priv_helpers（chmod EPERM，非 root macOS）
#   + 3 test_migrate_state_base_script + 2 test_xfs_quotactl_backend + 2 test_gateway + 2 test_c2_p0_probe
#   + 1 test_real_root_gate  = 49；零回归
```

### 4.5 `tests/security` 全档

```console
$ ... -m pytest tests/security -q -p no:randomly
2 failed, 10 passed, 40 skipped, 5 errors in 65.15s
# 2 failed = 既有 baseline：tests/security/escape/test_path_surface_inotify.py（两条），
#   与 slice A 报告记录的一致，与本片无关。
# 5 errors = 本 sandbox 本次运行**禁止进程 bind 端口**（conftest.py:256 `sock.bind` →
#   PermissionError [Errno 1]），属环境限制（本片未碰这些文件）；在更宽松的 shell 里
#   这些用例通过（slice A 报告记录过 tests/security 为 2 failed / 17 passed）。
```

---

## 5. 文件

**新增**

| 文件 | 内容 |
|---|---|
| `deploy/k8s/c3-agent.yaml` | DaemonSet `e2b-c3-agent`（两容器，pod 级 `hostPID`）+ NetworkPolicy |
| `tests/unit/test_c3_agent_manifest.py` | 20 条 pin：DaemonSet/NetworkPolicy 形状、RBAC、worker 侧约束、compose 形态、构建接线、D16 默认值、`local://` 未动 |

**修改**

| 文件 | 改动 |
|---|---|
| `control_plane/c3_agent_client.py` | D12：`AgentTarget.node_identity`（host）、URL 用 agent 身份、加 `validate_node_id` 检查、模块 docstring |
| `control_plane/config.py` | D16：`c3_agent_max_concurrency` 默认 `0`→`64`（含理由） |
| `deploy/c3_agent/app.py` | D12：删 worker/node 名字比对、改为形状检查；docstring/`WorkerInstruction` 注释 |
| `deploy/c3_agent/config.py` | `node_id` 注释：这是**主机**名 |
| `deploy/k8s/kustomization.yaml` | 加入 `c3-agent.yaml` |
| `deploy/k8s/control-plane.yaml` | Role `get,list`；新增 4 个 `E2B_C3_AGENT_*` env |
| `deploy/k8s/worker.yaml` | `E2B_SLOT_IDENTITY: agent-grant` + 注释（含"不许 hostPID"） |
| `deploy/k8s-k0s/apply.sh` | 闸门 `broker → agent → worker` |
| `deploy/k8s-k0s/secrets.sh` | 托管键 +`E2B_C3_AGENT_TOKEN` |
| `deploy/k8s-k0s/README.md` | 一句：c3-agent 与 broker 同处境（不在 overlay 表里） |
| `deploy/compose/docker-compose.prod.yml` | 加 `c3-agent`/`c3-agent-maint`；CP 加 3 键；worker 加 `E2B_SLOT_IDENTITY` |
| `deploy/compose/docker-compose.multinode.yml` | 同上 |
| `deploy/compose/.env.example` | `AGENT_IMAGE`/`E2B_C3_AGENT_TOKEN` |
| `deploy/scripts/build-images.sh` | 构建 `e2b-sandlock-agent` |
| `deploy/scripts/build-and-push.sh` | 命名约定行 +`agent` |
| `docs/deploy-clusters.md` | §7.6：C3 Task 3 现状/上线动作/待部署窗口 + 判据 13/16 的验收环境提醒 |
| `docs/k8s-deployment.md` | §1 拓扑表 +`c3-agent.yaml`；§2 密钥命令 +agent token；§4 表 +行；§4.5 表 3 +agent token 行 |
| `tests/unit/test_c3_agent_client.py` | D12：`AgentTarget`/URL/体断言；新增"无身份地址被拒"用例 |
| `tests/unit/test_c3_agent_service.py` | D12：URL=主机名、异名 worker 接受、形状检查用例 |
| `tests/unit/test_c3_internal_api_shape.py` | Role `get,list`；token pin 逐服务化 |
| `tests/unit/test_k0s_secrets_script.py` | 第 5 个托管键 |
| `tests/unit/test_worker_env_key_sets.py` | `slot_identity` 分类 + 两个 compose 栈的 whitelist |
| `tests/unit/test_worker_manifest_permissions.py` | 表 3 增 agent token 行 |

---

## 6. 自审

- **两条通道**：worker 清单/镜像里没有 agent 地址、token（k8s 与 compose 两侧都有 pin）；
  NetworkPolicy 把入口收到只有 control-plane；CP 只从 API/清单拿 agent 地址。
- **红线**：agent 清单不含任何禁项，且**不出现** `allowPrivilegeEscalation`/`no-new-privileges`
  （判据 11）；面 A 的 BND 只声明 `SETUID`/`SETGID`（判据 12）。
- **不扩面**：面 B 与 C1 broker 的能力集逐条相同（`drop:[ALL]` + 三条）。
- **精确断言**：新 pin 全部 `==` / 逐字；唯一"形状"处是 `urlsplit(...).hostname` 比对与
  `re.fullmatch` 之外的等价判断，无 `in`/部分匹配。
- **D16 不说空话**：不是拍一个数，是双侧夹逼并写进三处（config.py + 两份清单注释）。
- **没有削弱既有期望**：既有 pin 的每次改动都在 §3.3 列出并说明"行为变了"。
- **文档同步**：Global Constraints 要求"凡改 manifest 的 task 同步 `docs/deploy-clusters.md`
  现状节与对应 pin"—— 已做（§7.6 与 §4.5 表 3 均带 pin 引用）。
- **未在清单里内联任何凭据**：agent token 只经 `secretKeyRef`/`${VAR}`。

---

## 7. 留给 slice B2（不做的、要说清的）

1. **真机验收**：判据 1/2/3/7 的 k8s 臂（`probe_c3_userns_map_handoff.py --role forker/agent`）
   与 `E2B_SLOT_IDENTITY` 真切换；判据 13/16 的 **compose multinode** 臂（3 worker 同机）；
   判据 ⑥ 的连接层拒绝（真 NetworkPolicy）。
2. **并发上限定稿**：对 64 做 N-并发建箱测量，并把连接池压到 1 复现排队（反面臂）；据此或改数。
3. **上线窗口**：跑 `secrets.sh`（生成 agent token）→ `apply.sh`（新闸门）→ 观察 agent
   DaemonSet 收敛 → 切 `E2B_SLOT_IDENTITY`。
4. **判据 4**（面 B `CapEff=0x0b` 真机 `grep`）：face B 载荷属 Task 4，真机核在这之后。

---

## 8. 顾虑

1. **`deploy/stack/docker-compose.prod.yml` 未加 agent 服务**（D14 只点名了
   `deploy/compose/` 下的两个栈）。该文件是"沙箱目标主机（Rocky Linux/aarch64）的生产栈"，
   也是**分离 compose 栈**，今天沿用 file-capability 的 `exec` 形态、未声明 `E2B_SLOT_IDENTITY`
   （继承代码默认 `spawn`，仍可工作）。⇒ **请控制器裁一次**：C3 的 compose 覆盖范围是否含
   `deploy/stack/`？若要含，做法就是把本片的两个服务块与 worker 的 `E2B_SLOT_IDENTITY` 照搬
   （但该栈的 `image-cache-init`/卷布局与 compose 栈不同，需单独核对 `c3-agent-maint` 的挂载）。
   本片**按 D14 字面执行，不擅自扩面**。
2. **agent token 无列表式双窗**：轮换窗口内 CP 与 agent 各持一半 ⇒ 该窗口"建不了新箱"
   （在跑沙箱不受影响，worker 不必滚）。已按 O3 的"无双窗凭据"模板写进 `docs/k8s-deployment.md`
   表 3，并 pin 在 `test_worker_manifest_permissions.py`。要不要升级成双窗是控制器的事。
3. **面 B 现在 `sleep infinity`**：这是**有意**的空转形态（只持有已评审的能力集与挂载），
   不是把 Task 4 的活提前做；但真机上它会作为一个 root + 三条能力的常驻容器存在 ——
   B2 上线时值得在 `kubectl get pods` 里点名确认它就是这一形态。
4. **`tests/security` 的 5 个 error 来自本 sandbox 的 bind 限制**（conftest.py:256），非本片
   改动；更宽松 shell 下这些用例通过。真门禁是容器 lane。
5. **`hostPID` 在 face B 上的增量**：`plan §2.0` 已点名接受（face B 本就 root + `DAC_OVERRIDE`），
   本片把它写进清单注释而非默默发生；评审若要复核，这是唯一一处"一个 pod 字段跨两个面"的点。
