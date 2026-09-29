# Task 2 报告：agent 的通道与三步校验（含 N49）

- 工作树：`/Users/polus/project/ai/sandlock-e2b/tmp/wt-c3`（分支 `feat/c3-consolidation`）
- BASE：`f3f93f2`（Task 1）
- 本次提交：`1f328ab feat(c3): CP→agent 指令通道 + internal API 三步校验（Task 2，含 N49）`
- 真机 kubectl 一步 **未执行**（指令要求不碰集群），命令见文末 §7，等控制器协调。

---

## 1. 实现了什么

### 1.1 N49：internal API 的身份从凭据推导（`control_plane/api/internal.py`）

把 `/internal/**` 的 handler 明确分成两类（模块 docstring 里逐条列出，代码里用两个命名函数区分）：

| 类别 | 端点 | 做法 |
|---|---|---|
| **节点作用域** | `POST /internal/nodes/register`、`POST /internal/nodes/{id}/heartbeat`、`GET /internal/nodes/{id}/sandboxes`、`POST /internal/nodes/{id}/reconcile` | 走三步校验 + 源 IP 第二因子（`_require_node_identity`） |
| **舰队/运维作用域** | `GET /internal/routes/{sandbox_id}`、`GET /internal/nodes`、`GET /internal/fleet/metrics`、`POST /internal/nodes/{id}/drain|undrain`、`GET /internal/tenants` | 显式命名豁免（`_require_fleet_key`），模块 docstring 说明"调用方不是节点，没有可绑定的身份" |

三步校验（`_require_node_identity`）：

1. **凭据 → 身份**：`control_plane/auth.py::node_id_for_key`（key → node，配置来自 `E2B_INTERNAL_NODE_KEYS`，**只来自配置，绝不来自请求**）。
2. **自称 == 凭据推出**：不一致 → `403 X-Internal-Key is bound to node <derived>; request claims node <claimed>`。
3. **对象用 CP 自己的记录校验**：`recover_node` / `list_by_node` 本来就按（现在可信的）node_id 过滤 CP 记录；worker 上报的 `sandboxIDs` 只是"本地事实"，不成为候选对象。

第二因子（`_enforce_source_ip`）：期望 IP 来自 **resolver**（见 §1.2），绝不是请求里的 `address`，也绝不是"观察到的源 IP"。不一致 → `403 request for node <id> came from <observed>, expected <expected>`。

**fail-closed 与显式降级**：

- 节点绑定的 key：resolver 取不到期望地址 → `503 cannot determine the expected address for node <id>`，并打一条点名的 WARNING。
- 舰队 key（未绑定节点，包括今天的共享 `E2B_INTERNAL_API_KEY`）：显式且点名——每条这样的 key 只打一次 WARNING（`fleet key with no node binding ... the node-identity claim check is inactive`），行为保持 C3 之前（这样 local/合体/未迁移的 lane 不被打断）。**这不是静默豁免**：它是"配置项没打开"的可见事实。
- `register` 的 `address`：resolver 有答案时**一律用 resolver 的**（body 里不一致只记 WARNING）；resolver 没答案且是舰队 key 时才退回 body（点名降级）。
- resolver 模式 **显式**配置（`E2B_NODE_ADDRESS_MODE=k8s|hostname`）时，舰队 key 也吃源 IP 这一层（"偷 key 换网络位置"仍被拒）——见 `_address_enforcement_configured`。

### 1.2 D4：地址/期望 IP 解析器（`control_plane/node_address.py`，新）

两个显式模式 + 一个自动选择：

- `k8s`：`node_id` 就是 StatefulSet pod 名，用挂载的 ServiceAccount 查 `/api/v1/namespaces/<ns>/pods/<name>`，取 `status.podIP`；地址仍是 `http://<podIP>:<port>`（pod 名稳定、IP 会变 ⇒ 绑定自动跟随重启，符合 §11.1 第 9 项前提 (b)）。
- `hostname`：`node_id` 就是 compose 服务名，`socket.getaddrinfo` 解析出 IP，地址保持名字（`http://worker-1:49983`）。
- `auto`（默认）：挂了 ServiceAccount 走 k8s，否则 hostname。显式值优先；非法值在启动时 `ValueError` 点名拒绝。

解析不出来一律返回 `None`（不是猜测、不是回环、不是观察到的源 IP）；`None` 的**用法**是调用方的责任（节点绑定 → 503；舰队 key → 点名降级）。

注入点：`control_plane/app.py::create_app(node_address_resolver=...)`（测试用 `StaticAddressResolver`），否则从 `settings` 构建（`app.state.node_address_resolver`）。

新增配置（`control_plane/config.py`）：`internal_node_keys`（`E2B_INTERNAL_NODE_KEYS`，JSON `{key: node_id}`，并计入 `all_internal_api_keys`）、`node_address_mode/port/namespace`（`E2B_NODE_ADDRESS_MODE/PORT/NAMESPACE`）。

### 1.3 D3：agent 的 CP→agent 指令服务（`deploy/c3_agent/`，新）

- 包结构与 `deploy/quota_agent/` 同形：`__init__.py` / `config.py` / `app.py` / `__main__.py`。
- `POST /internal/nodes/{node_id}/agent/{op}`，第一步只实现 `grant-slot`（body `{sandbox_id, uid, pid}`，返回 `{op, sandboxID, uid, pid, asUid}`）。
- **无状态**：没有授权表、没有 TTL、没有"先推后发"；uid 由 CP 的指令直接给。
- agent 唯一的本地判断：`node_id == 自己的 node_id`（`E2B_C3_AGENT_NODE_ID`，回落 `E2B_NODE_ID`），否则 `403 request is addressed to node X, but this agent is node Y`（点名，且在 op 词汇之前）。
- 认证：`X-Internal-Key` == `E2B_C3_AGENT_TOKEN`（constant-time），未配置 token → `500`，不答；缺/错 token → `401`。agent→CP 方向继续用既有 internal-key 模式（本 task 不起这条出站调用）。
- `grant-slot` 执行 = `as_uid --uid X --pid N`，**只接受** exit 0 + stdout 恰为 `C3-ASUID-OK pid=N uid=X\n` + stderr 为空；其余一律 `502` + 点名（非零退出带 stderr、stdout 不符、stderr 非空、起不了进程）。**未实现**容器 pid → 宿主 pid 反查（注释里写明归 Task 3：NSpid 链 + 目标 worker pod 的 cgroup 匹配，Task 3 传宿主 pid）。
- `deploy/docker/Dockerfile.agent`：把 `sleep infinity` 占位换成 `CMD ["python3","-m","deploy.c3_agent"]`；镜像里只多装 web 栈（fastapi/uvicorn/pydantic，版本与 `requirements.txt` 对齐）+ `deploy/c3_agent` + `gateway_common`，**仍不含** envd_service / sandlock wheel / egress 库；`USER 65534:65534` 不变（face A）。

### 1.4 钉子（`tests/unit/test_c3_internal_api_shape.py`，新）

- worker 的 k8s StatefulSet：`capabilities.add` **恰为** `["SETGID","SETUID"]`，且不含 `SYS_ADMIN/SYS_PTRACE/NET_RAW`。
- 两套 compose 的 worker 服务（结构解析）：`cap_add` 不含三种危险能力、无 `privileged`、无 `network_mode: host`；autoscaler 的 docker backend 源码不含 `NET_RAW/SYS_PTRACE`。
- internal API 前没有代理：k8s 无 `Ingress`、无 mesh 注入注解、无 `istio-proxy/linkerd-proxy/envoy/nginx` sidecar 容器；`control-plane` Service 是 `ClusterIP` + 直选 `app: control-plane` + `port 3000`；k8s worker 与全部 compose worker 的 `E2B_CONTROL_PLANE_URL` 默认值都是 `http://control-plane:3000`（直连，无代理跳）。

---

## 2. TDD：RED → GREEN 逐用例

### RED（实现之前）

```
$ .venv/bin/python -m pytest tests/unit/test_node_address.py tests/unit/test_c3_agent_service.py \
    tests/unit/test_c3_internal_api_shape.py tests/contract/test_internal_identity.py -q
ERROR collecting tests/unit/test_node_address.py ... ModuleNotFoundError: No module named 'control_plane.node_address'
ERROR collecting tests/unit/test_c3_agent_service.py ... ModuleNotFoundError: No module named 'deploy.c3_agent'
ERROR collecting tests/contract/test_internal_identity.py ... ModuleNotFoundError: No module named 'control_plane.node_address'
3 errors in 0.36s
```

（钉子文件单独跑，因为它只读现有清单：`$ pytest tests/unit/test_c3_internal_api_shape.py -q` → `7 passed in 0.51s`，是"守卫已就位"，不是红。）

### GREEN（实现之后，host）

| 用例组 | 命令 | 结果 |
|---|---|---|
| 新用例 4 个文件 | `pytest tests/unit/test_node_address.py tests/unit/test_c3_agent_service.py tests/unit/test_c3_internal_api_shape.py tests/contract/test_internal_identity.py -q` | `36 passed in 2.68s` |
| 必跑契约 + 新用例 | §3 的 host 命令 | `66 passed in 4.57s` |

逐条对应（`tests/contract/test_internal_identity.py`）：

| 判据 | 用例 | 断言 |
|---|---|---|
| ① 凭据层 | `test_node_as_credential_cannot_heartbeat_for_node_b` / `..._cannot_read_node_bs_sandboxes` / `test_registration_cannot_bind_node_as_credential_to_node_b` | `403` + 逐字 `{"code":403,"message":"X-Internal-Key is bound to node node_a; request claims node node_b"}`；且 `nodes.get("node_b") is None`（拒绝发生在触达记录之前） |
| ② 源 IP 层 | `test_node_bs_key_from_node_as_network_position_is_rejected` | 同一请求：A 的 IP → `403 {"message":"request for node node_b came from 10.0.0.1, expected 10.0.0.2"}`；B 的 IP → `204`（两臂都断言，防"全拒"假通过） |
| ③ 非恒真 | `test_the_two_nodes_expected_source_ips_differ` | `10.0.0.1 != 10.0.0.2` **且**同一请求在 B IP 得 `204`、在 A IP 得 `403` |
| 第 3 步（对象归属） | `test_object_validation_uses_the_control_planes_own_records` | A 的凭据根本触不到 B 的记录：`reconcile` 返回 `{"recovered":[],"removed":["sbx_a"],"kept":[]}`，`registry.get("sbx_b")` 对象与状态不变 |
| 地址不采信 body | `test_registration_derives_the_address_from_the_resolver` | body 里 `http://evil.example:1` → 记录地址为 `http://10.0.0.1:49983` |
| fail closed + 点名 | `test_unresolvable_node_bound_request_is_refused_named` / `..._for_heartbeats` | `503 {"message":"cannot determine the expected address for node node_a"}` |
| 舰队 key 显式降级 | `test_a_fleet_key_is_an_explicit_named_degradation` | `200` + 记录地址取 body + WARNING 文本含 `fleet key with no node binding` |
| 显式模式给舰队 key 也加④ | `test_an_explicit_address_mode_gives_a_fleet_key_the_second_factor` | `hostname` 模式：注册地址取 resolver；换 IP 重发 → `403 ... came from 10.0.0.1, expected 10.0.0.2` |

agent 服务（`tests/unit/test_c3_agent_service.py`）：happy path / 错节点 403 / 缺·错 token 401 / token 未配置 500 / 未知 op 404 / runner 拒绝 502，全部**逐字**断言响应体；`SubprocessAsUidRunner` 用真 shell 脚本钉住"恰好一行 stdout + 空 stderr"的接受规则与四种拒绝。

resolver（`tests/unit/test_node_address.py`）：k8s 用 `httpx.MockTransport` 断言请求路径与 header（`/api/v1/namespaces/sandlock/pods/<name>` + `Bearer sa-token`）；hostname 用 monkeypatch 的 `getaddrinfo`；两种模式的 `None` 都断言到；`auto` 用 monkeypatch 切换。

---

## 3. 跑了哪些既有契约文件（必跑清单）+ 结果

### host（`/Users/polus/project/ai/sandlock-e2b/.venv/bin/python -m pytest ...`）

```
$ .venv/bin/python -m pytest tests/contract/test_control_plane.py \
    tests/contract/test_internal_identity.py tests/contract/test_internal_key_rotation.py \
    tests/contract/test_internal_tenants.py tests/contract/test_node_partition_reconcile.py \
    tests/unit/test_c3_agent_service.py tests/unit/test_node_address.py \
    tests/unit/test_c3_internal_api_shape.py -q -p no:cacheprovider
66 passed in 4.57s

$ .venv/bin/python -m pytest tests/contract/test_multinode.py -q -p no:cacheprovider
5 passed in 1.98s
```

必跑清单四条（`test_control_plane.py` / `test_internal_*` / `test_node_partition_reconcile.py` / `test_multinode.py`）**全绿**。

### 容器门禁（真正门禁，OrbStack，Landlock ABI 8）

```
$ docker run --rm --security-opt seccomp=unconfined --cap-add NET_ADMIN --network host \
    -e E2B_REQUIRE_SECCOMP_FILTER=0 -e E2B_BASE_IMAGE=python:3.14-slim -e E2B_HOST_PROJECT="$PWD" \
    -v /var/run/docker.sock:/var/run/docker.sock -v "$PWD:/workspace" -w /workspace \
    e2b-sandlock-test:latest \
    pytest tests/contract/test_control_plane.py tests/contract/test_internal_identity.py \
      tests/contract/test_internal_key_rotation.py tests/contract/test_internal_tenants.py \
      tests/contract/test_node_partition_reconcile.py tests/contract/test_multinode.py \
      tests/unit/test_c3_agent_service.py tests/unit/test_node_address.py \
      tests/unit/test_c3_internal_api_shape.py tests/security/test_agent_image_privilege.py \
      -q -p no:cacheprovider
80 passed, 112 warnings in 39.34s
```

（含 Task 1 的 agent 镜像 lane，证明换 CMD 没破坏镜像 pin。）

### 全量单元 / 全量契约（容器，对比 BASE 基线）

基线材料用 `git archive HEAD | tar -x -C tmp/c3-baseline` 生成（只读导出，未碰主树），两棵树各自挂进同一条容器命令。

```
$ docker run ... -v "$PWD:/workspace" -w /workspace e2b-sandlock-test:latest pytest tests/unit -q
17 failed, 1748 passed, 17 skipped in 78.64s     # 本树
$ docker run ... -v "$PWD/tmp/c3-baseline:/workspace" ... pytest tests/unit -q
22 failed, ...                                   # 基线
$ comm -13 tmp/c3-baseline-unit-failures.txt tmp/c3-wt-unit-failures.txt   # 本树独有
(空)

$ docker run ... -v "$PWD:/workspace" ... pytest tests/contract -q
27 failed ...                                    # 本树
$ docker run ... -v "$PWD/tmp/c3-baseline:/workspace" ... pytest tests/contract -q
36 failed ...                                    # 基线
```

- 单元：本树失败集是基线失败集的**子集**（没有新增失败）。
- 契约：本树比基线**少** 9 条失败；`comm` 里冒出的 3 条（`test_network_api::test_network_rejects_unsupported_parts`、`test_nonroot_route_b::...` 两条）单独重跑 → `3 passed in 6.41s`，是两趟全量连跑时的端口/容器竞争抖动，与本 task 面无关。
- 既有失败都是环境既有（`fakeredis`/`redis` 缺失、XFS `/dev/loop-control`、`.env.example` gitignore、冷镜像缓存 428 等），两份清单里都在。

**清理说明**：`tmp/` 里有本次留下的 scratch（`c3-baseline/`、`c3-agent-build-ctx-*/`、`c3-*-failures.txt`），以及被 `mv` 到 `tmp/_stale-unit-route-b-{rootfs,slots}` 的两份**先前就已存在**的陈旧 scratch（19:25，早于本会话；它们会让 `test_sandlock_executor_route_b` 报 `FileExistsError`）。全部在 gitignored 的 `tmp/` 里，未删除。

---

## 4. resolver / 地址模式设计（要点）

```
create_app(node_address_resolver=None)
        └─ app.state.node_address_resolver = 注入的 or build_node_address_resolver(settings)
                                                 ├─ mode=k8s      → K8sPodAddressResolver(SA token + CA, GET pods/<node_id>)
                                                 ├─ mode=hostname → HostnameAddressResolver(getaddrinfo(node_id))
                                                 └─ mode=auto     → 有 SA 走 k8s，否则 hostname
```

- 返回 `NodeEndpoint{address, ip}`：`address` 是 CP 回拨用（写进 NodeRecord），`ip` 是源 IP 层的期望值。两者来自**同一个可信查询**，"解析到了"是两者共同的唯一前置条件。
- 期望值**不来自请求**：注册 body 的 `address` 只在舰队降级路径里被使用（点名告警）。
- 观测值是 `request.client.host`（k8s 里 kube-proxy 保留源地址 ⇒ 就是 worker pod IP）。判据③ 依据在此：两节点 pod IP 不同 ⇒ 该层非恒真。防"这层变死代码"的配置钉子在 `tests/unit/test_c3_internal_api_shape.py`。
- 与 hard rule 的关系：没有 `worker↔agent` 通道；`/internal/**` 仍是 CP 面，agent 是另一条 `CP→agent` 通道（服务端在 `deploy/c3_agent`）。

---

## 5. 改了哪些文件

新增：

- `control_plane/node_address.py`（resolver）
- `deploy/c3_agent/__init__.py`、`config.py`、`app.py`、`__main__.py`（agent 服务）
- `tests/contract/test_internal_identity.py`（①②③ + 第 3 步 + register 地址 + fail-closed + 舰队降级）
- `tests/unit/test_c3_agent_service.py`、`tests/unit/test_node_address.py`、`tests/unit/test_c3_internal_api_shape.py`

修改：

- `control_plane/api/internal.py`（分类 + `_require_node_identity` / `_require_fleet_key` / `_enforce_source_ip`；register 地址取 resolver）
- `control_plane/auth.py`（`node_id_for_key`）
- `control_plane/config.py`（`E2B_INTERNAL_NODE_KEYS` + 地址模式三项；`all_internal_api_keys` 并入 per-node key）
- `control_plane/app.py`（`node_address_resolver=` 注入点 + `app.state.node_address_resolver`）
- `deploy/docker/Dockerfile.agent`（CMD → agent 服务；装 web 栈 + COPY 包）
- `tests/security/test_agent_image_privilege.py`（最小构建上下文随镜像新内容扩展：`deploy/__init__.py`、`deploy/c3_agent/`、`gateway_common/`）

**未改动**（按约束）：`deploy/k8s/worker.yaml` 能力块、`deploy/docker/Dockerfile.envd`、任何部署清单（`deploy/k8s/*`、`deploy/compose/*`、`deploy/stack/*`）、`docs/**`、主树。

---

## 6. 自审发现（已处理 / 已知）

1. **节点绑定的 key 必须先是一个有效 internal key**：`all_internal_api_keys` 原来不含 per-node key，会让节点绑定的 worker 在身份层之前就 401。已把 `internal_node_keys` 的 key 并入该属性，并有契约用例覆盖（否则 ①② 会变成 401 而不是 403）。
2. **`_require_fleet_key` 的注解先写错了**（原稿说"未来 per-node 凭据不能在这层被接受"，但实现上是接受的）。已改成诚实描述：任何有效 internal 凭据都可，因为该面不为任何节点行事。
3. **k8s resolver 的 `resp.json()` 可能抛**：已加 `except ValueError → None`（不是 JSON 也是"取不到"，绝不能回退到请求）。
4. **舰队 key + `auto` 模式不做解析**：避免在"名字其实解析不了"的形态里每 5s 心跳做一次 DNS，也避免通配 DNS 搜索域把心跳误判成 403。显式模式才启用④（`_address_enforcement_configured`），这条两种行为都有用例。
5. **`deploy/c3_agent` 让 agent 镜像开始吃仓库源码**：Task 1 的最小构建上下文（只 `deploy/priv/`）会 build 失败，已同步扩到"镜像真正 COPY 的那几个路径"，并在该 lane 的 docstring 里写明理由（仍不整仓 COPY）。
6. **容器端到端实测**：镜像重新 build（`docker build` 成功），并以 65534 起容器打真接口：happy path 返回 `{"op":"grant-slot",...,"asUid":"C3-ASUID-OK pid=4242 uid=10007"}`；错节点 `403 {"error":"request is addressed to node node_b, but this agent is node node_a"}`；错 token `401 {"error":"unauthorized"}`；无 token / 无 node id 启动直接点名退出。

---

## 7. 真机（未执行）——请控制器按此协调

目标：复验两节点的 worker 请求在 CP 侧**源 IP 不同**（判据③ 的真机臂），并确认 `internal API 前没有代理` 这条在真集群上也成立。

前置：本 task 没把仓库清单切到 per-node key / 显式地址模式（见 §8 缺口）。真机复验需要临时给 CP 打上解析器与 per-node key（rollout 后回滚），或先由 Task 3 落地 RBAC + agent DaemonSet 一起做。命令（**编辑器/控制器执行时逐条确认**）：

```bash
# 0. 先认集群，再敲任何命令（不加 KUBECONFIG 会安静地打到别的 ACK 集群）
cd /Users/polus/project/ai/sandlock-e2b/tmp/wt-c3
deploy/scripts/open-cluster-tunnel.sh
export KUBECONFIG="$PWD/tmp/k0s/kubeconfig"
kubectl -n sandlock get nodes -o wide        # 期望 2 节点、arm64、172.18.80.94/.140
kubectl -n sandlock get pods -o wide         # 期望看到 e2b-worker-0/1 及各自 podIP

# 1. 两节点的 pod IP（判据③ 的"两个位置"）
kubectl -n sandlock get pods -l app=e2b-worker \
  -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.podIP}{"\n"}{end}'

# 2. 确认 internal API 前面没有代理（Service 是直连 ClusterIP，无 Ingress）
kubectl -n sandlock get svc control-plane -o yaml | sed -n '/^spec:/,/^status:/p'
kubectl -n sandlock get ingress -A

# 3. 让 CP 用 k8s 解析器 + 一对临时 per-node key（rollout 后立刻回滚）
#    （k8s 模式需要 SA 有 pods get 权限 —— 见 §8 缺口，Task 3 一起补）
kubectl -n sandlock set env deployment/control-plane \
  E2B_NODE_ADDRESS_MODE=k8s E2B_NODE_ADDRESS_NAMESPACE=sandlock

# 4. 从每个 worker pod 里用"另一个节点"的 key 发一次节点作用域请求，
#    CP 的 403 日志会分别打印它看到的两个源 IP
for p in e2b-worker-0 e2b-worker-1; do
  kubectl -n sandlock exec "$p" -- python3 -c \
    "import os,urllib.request as u; h={'X-Internal-Key':os.environ['E2B_INTERNAL_API_KEY'],'Content-Type':'application/json'}; \
     r=u.Request('http://control-plane:3000/internal/nodes/e2b-worker-0/heartbeat',data=b'{}',headers=h); \
     u.urlopen(r)" || true
done
kubectl -n sandlock logs deployment/control-plane --tail=200 | grep -E "came from|expected"
#   判据：两行日志的 "came from" 必须是两个**不同**的 pod IP；
#         若相同 ⇒ 中间有代理/sidecar，这一层是死代码（回到 §6 的钉子排查）

# 5. 回滚
kubectl -n sandlock set env deployment/control-plane E2B_NODE_ADDRESS_MODE-
```

（这一步只写进本报告；`docs/deploy-clusters.md` §现状 与 `docs/open-issues.md` N49 状态行**未改**，等真机结果出来由控制器落笔。）

---

## 8. 顾虑与点名缺口

1. **仓库清单仍是一个舰队共享 key（`E2B_INTERNAL_API_KEY`）+ `auto` 地址模式** ⇒ 判据① 与④ 在**已发布形态**里默认**未激活**；本 task 交付的是机制 + 测试 + 注入点，激活需要部署改动（本 task 明文不碰清单）。要关 N49 需要两步，建议与 Task 3 一起做：
   - Secret 里加 `E2B_INTERNAL_NODE_KEYS={"<per-node key>":"<node_id>"}`，并把 worker 的 `E2B_INTERNAL_API_KEY` 换成各自的 key（compose：`worker-1..3`；k8s：StatefulSet pod 名 `e2b-worker-0/1`，名字稳定可预置）。
   - CP 显式 `E2B_NODE_ADDRESS_MODE=k8s|hostname`；k8s 另需给 CP 的 SA 一个 `pods get` 的 Role/RoleBinding（**当前仓库没有** CP 的 SA/RBAC）。角色就绪前 k8s 模式会 503（节点绑定）或点名降级（舰队 key），不会静默放行。
2. **`E2B_C3_AGENT_TOKEN` 与 agent 服务尚未进任何清单/Secret**（Task 3 的 `deploy/k8s/c3-agent.yaml` + compose agent 服务该做）；本 task 只交付服务端与镜像。
3. **CP→agent 的客户端（转发）没有实现**：按计划那是 Task 3 的"CP 侧新增转发"；本 task 交付的是**被调用面**（agent 服务）与 CP 侧的"只能指令凭据对应节点"的机制。agent→CP 出站也留给 Task 3。
4. **判据③ 的真机臂未做**（§7），N49 行不宜现在就标"已关闭"。
5. **`node_address_port` 默认 49983**，与清单一致；若某形态改了 `E2B_ENVD_PORT`，需要同时设 `E2B_NODE_ADDRESS_PORT`（否则解析到的回拨地址端口不对）。
6. **k8s resolver 的 RBAC 未落**：auto 模式下挂了 SA 就选 k8s，但没有 `pods get` 权限时会解析失败 → 舰队 key 点名降级、节点绑定 key 503。这是显式、可见的失败，不静默；但要在真机上用 k8s 模式必须先补 RBAC（Task 3 的 agent DaemonSet 也需要同名权限，建议合并）。
7. **测试纪律提醒**：host 的 `tests/unit` 全量因缺 `redis`/`fakeredis` 停在同一处收集错误（环境既有），所以 host 只跑具体文件；全量单元/契约的结论来自容器 + BASE 基线对比（§3）。

---

# 评审修复（Needs fixes → 已修，commit `679d2f2`）

## F1. 评审发现（findings）

| # | 级别 | 发现 |
|---|---|---|
| C1 | **Critical** | N49 在**出厂形态仍开着**：`_require_node_identity` 对未绑定的舰队 key 保留 C3 之前的行为（返回**请求自陈**的 node_id、只打一次 WARNING），而出厂清单既没设 `E2B_INTERNAL_NODE_KEYS` 也没设 `E2B_NODE_ADDRESS_MODE`。⇒ 拿共享 key 的 worker 仍可 `register {"nodeID":"e2b-worker-1","address":"http://attacker:1"}` 冒充别的节点并让 CP 回拨攻击者。我原来的 `test_a_fleet_key_is_an_explicit_named_degradation` 把这个洞当"期望行为"钉住了 —— 已删除。 |
| I1 | Important | agent 服务 `0.0.0.0` + 单一共享 token + 无调用方检查；缺 NetworkPolicy / token 不进 worker 的要求（Task 3 的活，本任务只记录并加钉子）。 |
| I2 | Important | 真机判据③与文档同步未做（本任务只能写命令 + 标注 pending）。 |
| M1 | Minor | 部分匹配断言：`test_internal_identity.py` 的 `"…" in record.getMessage()`、`test_c3_agent_service.py` 的 `pytest.raises(match=…)`。 |
| M2 | Minor | agent FastAPI 暴露 `/docs`/`/redoc`/`/openapi.json`。 |
| M3 | Minor | agent 不校验 `sandbox_id`（敌意 id 原样进日志）。 |
| M4 | Minor | 非 ASCII 的 `X-Internal-Key` → `secrets.compare_digest` 抛 `TypeError` → 500（agent 与 CP 两侧）。 |
| M5 | Minor | `node_address.py` 里 `status` 非 dict 时 `.get` → `AttributeError` → 500。 |
| M6 | Minor | 点名/记录：fleet-scope 端点保持豁免，需写进 N49 文档行。 |

## F2. 我改了什么（D5）

1. **代码层关掉（D5.1/D5.5）** —— `control_plane/api/internal.py::_require_node_identity` 重写：
   - 节点绑定的 key：不变（自称不一致 403）。
   - **未绑定的舰队 key：不再免检**。自称必须**解析到一个期望地址**，且请求必须**从该地址发出**（源 IP 第四层）；`claim is None`（register 不带 `nodeID`）或**解析不到**→ 一律拒绝，点名（`503 cannot determine the expected address for node <id>` / `403 a node-scoped request with a fleet key must declare the node it acts for (register: body.nodeID)`）。
   - **删除**了"取不到就退回 `body["address"]`"这条分支与 `_address_enforcement_configured`（模式不再是"要不要这层"的开关）。register 的 `address` 永远是解析器的答案；body 不一致只记 WARNING。
   - 舰队 key 的 WARNING 文案改成描述真实规则（凭据无法说明是谁在调用 ⇒ 只认"自称解析到的地址"）。
2. **出厂清单显式（D5.2/D5.3/D5.4）**：
   - `deploy/k8s/control-plane.yaml`：`E2B_NODE_ADDRESS_MODE: k8s` + `E2B_NODE_ADDRESS_NAMESPACE: sandlock`；新增**同名 ServiceAccount** + **namespaced Role（只 `get pods`）** + RoleBinding，并在 pod spec 上 `serviceAccountName: control-plane`（无 ClusterRole/ClusterRoleBinding）。
   - `deploy/compose/docker-compose.{prod,multinode,autoscale}.yml` 与 `deploy/stack/docker-compose.prod.yml`：`E2B_NODE_ADDRESS_MODE: hostname`（硬编码，不写成 `${…:-}`，免得以后一次 env 覆盖就把洞又打开；要改只能改清单，而清单有钉子）。
   - **可解析性已核对**：prod/multinode/stack 里 worker 是 compose 服务 `worker-1…3` 且 `E2B_NODE_ID` 同名，控制面在同一 project network ⇒ docker 内嵌 DNS 解析得到；autoscale 的 CP 在 `networks.default.name: sandlock` 上，而池子建 worker 用的是 `E2B_AS_DOCKER_NETWORK: sandlock`（同一个网络）且容器名 = `E2B_NODE_ID` ⇒ 也解析得到。**无需改名**。
   - 不需要新的 Secret 键 ⇒ `deploy/k8s-k0s/secrets.sh` 未改；k0s overlay 直接继承 `../k8s` 的 RBAC（`kubectl kustomize deploy/k8s` 与 `deploy/k8s-k0s` 都渲染通过）。
3. **文档（D5.5）**：`docs/open-issues.md` N49 行状态改成「**部分关闭（C3 Task 2）**」，逐条写清**关了哪些**（四个 node-scoped 端点 / 显式地址模式 / 源 IP 层 / 地址不采信 body）与**没关哪些**（仍是共享 key ⇒ "同节点其它东西"盲区仍在；per-node key = 近期加固、mTLS = 目标；fleet-scope 端点不覆盖；真机③待部署窗口）。`README.md` 环境变量表加 3 行（internal key / `E2B_NODE_ADDRESS_MODE` / `E2B_INTERNAL_NODE_KEYS`）；`docs/production-deployment-requirements.md` 新增 §2.10（按形态给出必须设置 + 未关闭项）；`docs/deploy-clusters.md` 新增 §7.5（**仓库已落、集群未上线** + 真机命令指向 + **待窗口回填**）；`deploy/k8s-k0s/README.md` 说明新 SA/RBAC 随基线 apply、不需要新 Secret 键。
4. **Minors**：断言改成整串精确比较（agent 的 4 条拒绝 + 缺失二进制；CP 的舰队 WARNING 用整份 messages 列表比较）；agent `FastAPI(docs_url=None, redoc_url=None, openapi_url=None)`；agent 用 `gateway_common.paths.validate_sandbox_id` 校验 `sandbox_id`（400，回显里不带该 id）；agent 与 CP 的 key/token 比较都吞 `TypeError` → 401（`_keys_match` / `_token_matches`）；`K8sPodAddressResolver` 对非 dict 的 `status` 返回 None；`SubprocessAsUidRunner` 的启动失败消息改用 `strerror`（确定性）。
5. **I1 记录 + 钉子**：`tests/unit/test_c3_internal_api_shape.py::test_no_worker_shape_carries_the_agent_token` 断言 8 个 worker 侧来源（k8s worker/autoscaler、四个 compose、`Dockerfile.envd`、`autoscaler/backends/local.py`）**都不含** `E2B_C3_AGENT_TOKEN`；agent 的 listen host 本来就可配（`E2B_C3_AGENT_HOST`，默认 `0.0.0.0`，注释说明 Task 3 要用 NetworkPolicy 兜住）。**NetworkPolicy 本身不在本任务**。
6. **测试接线（D4 的"注入 resolver"落到实处）**：新增 `tests/_c3_resolver.py`（`loopback_resolver(*ids)` 精确表 / `AnyNodeLoopbackResolver` 任意 id→回环）；`tests/conftest.py` 的 `apps`/`make_apps`/`_start_multinode` 注入解析器（multinode 另外给每个 worker 稳定的 `E2B_NODE_ID=worker-N`，为此给 `create_envd_app`/`NodeAgent` 加了可选 `node_id=` 透传，`_register_payload` 用它、生产仍读环境变量）；`test_node_partition_reconcile`/`test_orphan_tree_gc`/`test_delete_trusted_targets`/`test_quota_maintenance` 各自注入。这些是新的 fail-closed 行为要求 lane 显式声明"这些节点的期望端点是什么"。

## F3. RED → GREEN（新关掉的行为）

**RED**（把**新版**测试放进 `1f328ab` 的代码树 `tmp/c3-red` 跑，host）：

```
$ cd tmp/c3-red && pytest tests/contract/test_internal_identity.py \
    tests/unit/test_c3_agent_service.py tests/unit/test_c3_internal_api_shape.py -q
FAILED tests/contract/test_internal_identity.py::test_a_fleet_key_with_an_unresolvable_claim_is_refused_named
FAILED tests/contract/test_internal_identity.py::test_a_fleet_key_register_without_a_claim_is_refused_named
FAILED tests/contract/test_internal_identity.py::test_a_fleet_key_is_accepted_only_from_the_claims_resolved_address
FAILED tests/contract/test_internal_identity.py::test_a_non_ascii_internal_key_is_a_401_not_a_500
FAILED tests/unit/test_c3_agent_service.py::test_subprocess_runner_refuses_a_missing_binary
FAILED tests/unit/test_c3_agent_service.py::test_the_service_exposes_no_interactive_surface
FAILED tests/unit/test_c3_agent_service.py::test_a_hostile_sandbox_id_is_refused_before_the_runner
FAILED tests/unit/test_c3_agent_service.py::test_a_non_ascii_token_is_a_401_not_a_500
FAILED tests/unit/test_c3_internal_api_shape.py::test_the_k8s_control_plane_pins_the_k8s_address_mode
FAILED tests/unit/test_c3_internal_api_shape.py::test_the_k8s_control_plane_can_read_pods_and_nothing_else
FAILED tests/unit/test_c3_internal_api_shape.py::test_every_production_compose_control_plane_pins_the_hostname_mode
11 failed, 27 passed in 4.06s
```

（C1 的正例：新用例 `test_a_fleet_key_with_an_unresolvable_claim_is_refused_named` 的输入与旧用例
`test_a_fleet_key_is_an_explicit_named_degradation` **完全同形**，旧代码下判 200、新代码下判 503 —— 这就是被关掉的洞。）

**GREEN**（本树）：

```
$ .venv/bin/python -m pytest tests/contract/test_internal_identity.py tests/unit/test_node_address.py \
    tests/unit/test_c3_agent_service.py tests/unit/test_c3_internal_api_shape.py \
    tests/unit/test_priv_as_uid.py tests/security/test_agent_image_privilege.py \
    tests/contract/test_control_plane.py tests/contract/test_internal_tenants.py \
    tests/contract/test_internal_key_rotation.py tests/contract/test_node_partition_reconcile.py \
    tests/contract/test_multinode.py -q
99 passed in 14.69s            # host
```

```
$ docker run --rm --security-opt seccomp=unconfined --cap-add NET_ADMIN --network host \
    -e E2B_REQUIRE_SECCOMP_FILTER=0 -e E2B_BASE_IMAGE=python:3.14-slim -e E2B_HOST_PROJECT="$PWD" \
    -v /var/run/docker.sock:/var/run/docker.sock -v "$PWD:/workspace" -w /workspace \
    e2b-sandlock-test:latest \
    pytest tests/contract/test_internal_identity.py tests/unit/test_node_address.py \
      tests/unit/test_c3_agent_service.py tests/unit/test_c3_internal_api_shape.py \
      tests/unit/test_priv_as_uid.py tests/security/test_agent_image_privilege.py \
      tests/contract/test_control_plane.py tests/contract/test_internal_tenants.py \
      tests/contract/test_internal_key_rotation.py tests/contract/test_node_partition_reconcile.py \
      tests/contract/test_multinode.py tests/unit/test_worker_manifest_permissions.py \
      tests/unit/test_k0s_secrets_script.py tests/unit/test_autoscaler_local_backend_shape.py \
      -q -p no:cacheprovider
174 passed, 14 skipped in 16.09s
```

（14 条 skip 是 `test_worker_manifest_permissions.py` 里"容器内没有 kubectl，渲染不了 kustomize overlay"的既有能力 skip；同一文件在 **host 上 113 passed**，另外我手工跑了 `kubectl kustomize deploy/k8s` 与 `kubectl kustomize deploy/k8s-k0s`，两份都渲染成功且能 grep 到新增的 SA/Role/RoleBinding 与 `E2B_NODE_ADDRESS_MODE`。）

**全量回归对比（容器，对比 `1f328ab` 基线 `git archive`）**：

```
$ pytest tests/unit  →  17 failed / 1748 passed      （基线 22 failed；comm -13 新失败 = 空）
$ pytest tests/contract → 23 failed                  （基线 36 failed；comm -13 只剩日志噪声 + 1 条）
```

唯一那条 `test_teardown_failure_semantics.py::test_a_refused_tree_is_parked_and_the_row_it_pinned_is_released[quotactl]` 单独重跑 → 本树与基线都 `2 passed`（`lsattr`/`quotactl` 两个 backend 参数在连跑时按前序 tmp 状态择一失败，环境既有）。

**新增/改动的覆盖清单**（`tests/contract/test_internal_identity.py`）：舰队 key 不可解析 ⇒ 503 点名；舰队 key 不带 `nodeID` 的 register ⇒ 403 点名；舰队 key 只在"自称解析到的地址"上被接受（同请求换 IP ⇒ 403）；节点绑定 key 的 ①②③ 与对象归属不变；非 ASCII key ⇒ 401。

## F4. 仍未做 / 需要 Task 3 或部署窗口

- **per-node key（`E2B_INTERNAL_NODE_KEYS`）没有接线**：出厂仍是共享舰队 key，所以"同一节点上的其它东西同时有该节点 IP 与 key"这一档盲区仍在（已写进 N49 行）。机制与测试都在，接线是一次 Secret + worker env 改动。
- **agent 的 NetworkPolicy 与 `E2B_C3_AGENT_TOKEN` 分发**：Task 3 的 DaemonSet 一起做（本任务只加了"worker 侧不得出现该 token"的钉子）。
- **真机判据③**：`docs/deploy-clusters.md` §7.5 标了 ⏳，命令在 §7（本文档）——部署窗口执行并回填。
- **CP→agent 的转发客户端**：仍是 Task 3（本任务交付被调用面 + CP 侧"只允许按凭据对应节点"的机制）。

---

# 第二轮评审修复（commit `6ba3e88`）

## G1. 发现（findings）

| # | 级别 | 发现 |
|---|---|---|
| I1 | **Important（第一轮的修复引入的新耦合）** | `envd_service/agent.py::_fleet_sandbox_ids` 用 `GET /internal/nodes` + **逐节点** `GET /internal/nodes/{id}/sandboxes` 枚举全舰队记录。第一轮之后这些逐节点端点受身份校验，于是**"已注册但解析不到"的死节点**（`reap_unhealthy` 在沙箱 TTL 前一直保留它的行）会 503，枚举返回 `None`，**全舰队所有 live worker 的孤儿树/孤儿图回收集体停摆** —— 恰好在 Task 6 判据 9 的场景里。第一轮留下的唯一痕迹是我在 `tests/unit/test_quota_maintenance.py` 里加的那句"把 `local` 节点从注册表删掉"。 |
| M4 | Minor | `test_node_address.py` 缺"200 但 body 非 dict"的用例（`node_address.py` 的 `isinstance` 守卫可被静默回退）。 |
| M5 | Minor | 503 的 fail-closed WARNING **每请求**一条（每个 worker 每 5 s、外加每轮枚举），舰队级故障会刷屏。 |
| M6 | Minor | `node_id` 在插进 k8s API 路径 / 交给 `getaddrinfo` 之前没有形状校验。 |
| M7 | Minor | `HostnameAddressResolver` 只取 `getaddrinfo` 的**第一条**（可能是 AAAA），而观测到的客户端可能是 IPv4。 |
| M8 | Minor | `_pulse` 对非 2xx 的注册/心跳**完全静默**（没有 `E2B_NODE_ID` 的分离 worker 永远不加入，节点上却没有任何线索）。 |

## G2. 我改了什么（D6 + minors）

1. **D6：新增加 fleet 作用域枚举端点**（`control_plane/api/internal.py`）：
   `GET /internal/fleet/sandboxes` → `{"sandboxIDs": [...]}`（与逐节点端点同形），走 `_require_fleet_key`，并写进模块 docstring 的 fleet-scope 列表。
2. **worker 改用它**（`envd_service/agent.py::_fleet_sandbox_ids`）：一次 GET + 与 `/internal/fleet/metrics` 的计数比对（M1 纪律保留，只是"短"的成因从"节点行缺失"变成"读完列表后又有 create 落库"）。**逐节点端点保持严格**，没有为它放宽身份层。删掉了不再使用的 `quote` 导入。
3. **M4**：`tests/unit/test_node_address.py` 增加"200 + 非 dict body"（`[]`/`["pods"]`/`"boom"`/`7`/`None`/`{"status":"Pending"}`/`{"status":[]}`）全部 ⇒ `None`。
4. **M5**：新增 `_unresolvable_nodes_reported`（模块级、按 node id 一次），503 的**拒绝与报文不变**，只把 WARNING 收敛为每节点一条；`test_an_unresolvable_node_warns_once_but_refuses_every_time` 断言 3 次请求 = 3 个 503 + **恰好一条**日志。
5. **M6**：`gateway_common/paths.py` 增加 `validate_node_id`（与 `_SANDBOX_ID_RE` 同风格；用 `\Z` 而不是 `$`，因为这里要挡住尾部换行这种"另一个名字"），两个 resolver 在**插值/解析之前**先用它，非法 id ⇒ `None`（fail closed，不抛）。
6. **M7**：`NodeEndpoint` 增加 `ips`（`source_ips` 属性给出完整集合）；`HostnameAddressResolver` 收集**全部**解析结果（去重、保序），`_enforce_source_ip` 命中**集合中任意一个**即可，拒绝时点名整集（`expected one of …`；单地址时仍打印单个 IP，旧断言不变）。
7. **M8**：`_pulse` 在注册/心跳非 2xx（心跳仍把 404 当"重注册"）时打点名 WARNING；新增 `tests/unit/test_worker_register_diagnostics.py`（stub 控制面 503/403）断言这两行。**没有**加启动硬失败。
8. **撤销第一轮的 fixture 调整**：`tests/unit/test_quota_maintenance.py` 里那句 `control.state.nodes.remove("local")` 已删——D6 之后它不再是必要的，也不该继续掩盖问题。
9. **M1 机制测试的触发方式**：新增 `tests/_fleet_view.py::ShortFleetView`（ASGI 包装：只把 `/internal/fleet/sandboxes` 换成"短清单"，其余全走真 app），把 4 条 M1 用例的触发从"节点行缺失"改成"清单短"（`test_orphan_tree_gc.py` 三条 + `test_quota_maintenance.py` 一条），机制断言（跳过/点名/退避 schedule）原样保留。
10. **文档**：`docs/production-deployment-requirements.md` §2.10 与 `docs/open-issues.md` N49 行补"**分离形态的 worker 必须声明 `E2B_NODE_ID`**"这一前置 + 新端点存在的理由（死节点不再让全舰队跳过回收）。

## G3. RED → GREEN（新关掉的回归）

**RED**（把新代码树 rsync 到 `tmp/c3-red2`，**只把 worker 改回逐节点路径**，那条新用例必须失败）：

```
$ cd tmp/c3-red2 && pytest \
    "tests/contract/test_orphan_tree_gc.py::test_fleet_enumeration_survives_a_registered_but_unresolvable_node" -q
httpx.HTTPStatusError: Server error '503 Service Unavailable' for url
  'http://control/internal/nodes/node_ghost/sandboxes'
WARNING envd_service.agent: reconcile: leaving 1 orphan tree(s) on disk alone
  this round (fleet record enumeration unavailable): sbx_unowned_dead_node
WARNING envd_service.agent: reconcile: disk sweep deferred by an incomplete
  fleet enumeration; retrying in 1 heartbeat interval(s) (attempt 1)
FAILED tests/contract/test_orphan_tree_gc.py::test_fleet_enumeration_survives_a_registered_but_unresolvable_node
1 failed in 0.30s
```

这正是评审描述的链路：逐节点路径下 `node_ghost` 503 ⇒ 枚举不可信 ⇒ 全舰队跳过回收。新端点 + 一次 GET 之后该用例通过。

## G4. 复跑（本树）

```
$ .venv/bin/python -m pytest tests/contract/test_internal_identity.py tests/unit/test_node_address.py \
    tests/unit/test_c3_agent_service.py tests/unit/test_c3_internal_api_shape.py \
    tests/unit/test_priv_as_uid.py tests/security/test_agent_image_privilege.py \
    tests/contract/test_control_plane.py tests/contract/test_internal_tenants.py \
    tests/contract/test_internal_key_rotation.py tests/contract/test_node_partition_reconcile.py \
    tests/contract/test_multinode.py tests/contract/test_orphan_tree_gc.py \
    tests/unit/test_quota_maintenance.py tests/unit/test_worker_register_diagnostics.py -q
209 passed in 26.63s            # host
```

```
$ docker run ... e2b-sandlock-test:latest pytest <同上一份清单 + test_worker_manifest_permissions.py
    + test_k0s_secrets_script.py> -q -p no:cacheprovider
273 passed, 14 skipped in 29.01s
```

（14 条 skip 仍是容器内没有 kubectl 的 kustomize 渲染能力 skip；该文件 host 上 113 passed，两份 overlay 也手工 `kubectl kustomize` 渲染过。）

**全量回归对比（容器，对比 `1f328ab` 基线）**：`tests/unit` = 17 failed / 1748 passed（新失败 `comm` = 空）；`tests/contract` = 26 failed（基线 36 failed），多出的 2 条是 `test_nonroot_route_b.py` 的全量连跑抖动 —— **单独重跑该文件 `3 passed`**（与本任务面无关）。

## G5. 仍未做（与第一轮相同，未变）

- per-node key（`E2B_INTERNAL_NODE_KEYS`）接线；agent 的 NetworkPolicy 与 token 分发（Task 3）；真机判据③（部署窗口，`docs/deploy-clusters.md` §7.5 标 ⏳）；CP→agent 转发客户端（Task 3）。

---

# 第三轮评审修复（commit `2f849b3`）

## H1. 发现（findings）

| # | 级别 | 发现 |
|---|---|---|
| I1 | **Important** | `deploy/scripts/checkpoint_acceptance.py` 在**集群外**用共享 key 读**按节点**名单（`GET /internal/nodes/<id>/sandboxes`）。第一轮之后该端点要求调用方就是那个节点（凭据 + 源 IP），所以无论现场是哪种边缘形态，这个请求都会 403；脚本的 `except Exception` 便把"读不到名单"归因成"通道/控制面坏了"并退出 2 —— 它的安全闸从"真的核对过名单"退化成"永远拒绝"，而部署窗口里的人正会去找 `--force`。 |
| M5 | Minor | `docs/production-deployment-requirements.md` 的 fleet 作用域枚举漏了新端点。 |
| M6 | Minor | `tests/contract/test_orphan_tree_gc.py` 里两条已经失效的前置（ghost 记录 + `== ["node_a"]` 断言）读起来像"节点行缺失仍会让名单变短"。 |
| M7 | Minor | N49 行的"未关闭"清单没写：共享 key 现在能经舰队枚举端点读到**全舰队**的 sandbox id（跨租户）。 |

## H2. 我改了什么（D7）

1. **舰队视图带归属**（`control_plane/api/internal.py::fleet_sandboxes`）：
   `GET /internal/fleet/sandboxes` → `{"sandboxes": {"<nodeID>": ["<id>", …]}}`（每节点内**排序**，便于人读与 diff）。
   仍在命名 fleet 作用域集合里（`_require_fleet_key`），仍是**完整视图**（无节点身份要求）。无 node 的记录
   （进程内 `local` worker）归到 `"local"` —— 与 CP 别处对这个节点的称呼一致，于是调用方总能解释**每一条**记录。
   理由（写进 docstring）：集群外的调用方持有共享 key、**冒充不了**某个节点；带归属的舰队视图让它能问
   "node X 拥有哪些 id"而无需冒充。
2. **worker 侧扁平化**（`envd_service/agent.py::_fleet_sandbox_ids`）：读 `payload["sandboxes"]` 并按节点展开成 id 集合；
   形状不对（缺 `sandboxes` / 不是对象）仍是"枚举不可信"。**M1 的完整性规则一字未改**：仍与
   `/internal/fleet/metrics` 的记录数逐条比对，短了就跳过本轮 + 记退避（四条 M1 用例照旧，删掉该规则仍会红）。
3. **验收脚本改读舰队视图**（`deploy/scripts/checkpoint_acceptance.py::node_sandbox_ids`，仍是可导入的小函数）：
   一次 `GET /internal/fleet/sandboxes`，取本节点那一格；形状不对 ⇒ 抛（调用方照样拒删 pod、退出 2、不碰 kubectl）。
   节点不在归属里 ⇒ 空列表 ⇒ 由"名单连自己都不认"那一关拒掉（更贴切的措辞）。
   提示文案改成真实成因："读不到控制面的**舰队沙箱归属名单**（GET /internal/fleet/sandboxes）… 确认内部 key 能读它
   （舰队作用域、不需要是那个节点）"，不再把人往"通道坏了"上引。
4. **测试**：
   - `tests/contract/test_internal_identity.py::test_the_fleet_sandbox_view_is_attributed_and_fleet_scope`：逐字断言归属形状，
     **并且**同一把 key、同一个非节点来源（`10.9.9.9`）读舰队视图 = 200、读**按节点**端点 = 403（对比即要点）。
   - `tests/unit/test_checkpoint_acceptance_pod_politeness.py`：新增两条聚焦用例 —— 查表只发一次舰队视图请求且 URL 不含
     `/internal/nodes/`、按节点取切片/缺席节点返回空；以及畸形载荷（`[]` / 只有 `sandboxIDs` / `sandboxes` 非对象 /
     节点值不是 list）逐字报错。既有的拒删/`--force` 用例只改了"读不到名单"那条的期望文案。
   - `tests/_fleet_view.py::ShortFleetView` 改成归属形状；四条 M1 用例的触发随之改为"归属里短了一条"。
5. **文档**：§2.10 的 fleet 作用域清单补上 `/internal/fleet/sandboxes`（并点名它是 worker 回收与验收脚本读的那个）；
   N49 的"未关闭"清单加一句：持有共享 key 即可经它读到**全舰队**每个 sandbox id 及其归属节点（跨租户只读清单，
   不含内容），与 `/internal/nodes` / `/internal/routes` 同级信任。
6. **M6**：删掉 `test_incomplete_fleet_enumeration_aborts_the_disk_sweep` 里那两条失效前置（节点行断言），
   只保留"记录存在（让 metrics 计数包含它）"，并注明缺口由 `ShortFleetView` 直接制造。

## H3. RED → GREEN（新形状）

**RED**（`tmp/c3-red3` 里**只把舰队视图改回扁平** `{"sandboxIDs": [...]}`，其余不动）：

```
$ cd tmp/c3-red3 && pytest \
    "tests/contract/test_internal_identity.py::test_the_fleet_sandbox_view_is_attributed_and_fleet_scope" \
    "tests/contract/test_orphan_tree_gc.py::test_fleet_enumeration_survives_a_registered_but_unresolvable_node" -q
ValueError: fleet sandbox attribution is not an object      # worker 侧：形状不对 ⇒ 枚举不可信
WARNING envd_service.agent: reconcile: leaving 1 orphan tree(s) on disk alone this round …
WARNING envd_service.agent: reconcile: disk sweep deferred by an incomplete fleet enumeration; retrying in 1 …
FAILED tests/contract/test_internal_identity.py::test_the_fleet_sandbox_view_is_attributed_and_fleet_scope
FAILED tests/contract/test_orphan_tree_gc.py::test_fleet_enumeration_survives_a_registered_but_unresolvable_node
2 failed in 0.39s
```

一次回退同时红了"形状"与"worker 扁平化"两件事：契约用例抓形状，worker 用例抓"读了扁平视图 ⇒ 枚举不可信 ⇒ 回收停摆"。

## H4. 复跑（本树）

```
$ .venv/bin/python -m pytest tests/contract/test_internal_identity.py tests/unit/test_node_address.py \
    tests/unit/test_c3_agent_service.py tests/unit/test_c3_internal_api_shape.py \
    tests/unit/test_priv_as_uid.py tests/security/test_agent_image_privilege.py \
    tests/contract/test_control_plane.py tests/contract/test_internal_tenants.py \
    tests/contract/test_internal_key_rotation.py tests/contract/test_node_partition_reconcile.py \
    tests/contract/test_multinode.py tests/contract/test_orphan_tree_gc.py \
    tests/unit/test_quota_maintenance.py tests/unit/test_checkpoint_acceptance_pod_politeness.py \
    tests/unit/test_worker_register_diagnostics.py -q
217 passed in 25.97s            # host
```

```
$ docker run ... e2b-sandlock-test:latest pytest <同上一份清单 + test_worker_manifest_permissions.py> -q
256 passed, 14 skipped in 24.74s
```

**全量回归（容器，对比 `1f328ab` 基线）**：`tests/unit` = 17 failed / 1748 passed（新失败 `comm` = 空）；
`tests/contract` = 22 failed（基线 36 failed），**"不在基线里的 FAILED" = 空**（本轮没有再出现上一轮那两条
`test_nonroot_route_b` 的连跑抖动）。

## H5. 仍未做（与第二/三轮相同，未变）

- per-node key（`E2B_INTERNAL_NODE_KEYS`）接线；agent 的 NetworkPolicy 与 token 分发（Task 3）；
  真机判据③（部署窗口，`docs/deploy-clusters.md` §7.5 标 ⏳）；CP→agent 转发客户端（Task 3）；
  fleet 枚举端点的跨租户只读可见性（已写进 N49 的"未关闭"）。
- 验收脚本的舰队查询**已能单测**（`node_sandbox_ids` 是可导入函数，两条聚焦用例覆盖正常/畸形视图），
  所以没有"无法测试"的遗留项。
