# C3 真实集群缺陷修复报告（F1 / F2）

工作目录：`/Users/polus/project/ai/sandlock-e2b/tmp/wt-c3`，分支 `feat/c3-consolidation`。
本次改动的镜像版本：`0.1.0-762-gce6f3d6-20260929-205938`（k0s 集群已滚动到该版本）。

---

## 裁决：F1 用「迁移前把记录指向目标节点」这一条

两个候选里选的是**第二条**——迁移的显式步骤里，先由控制面把 sandbox 记录改到目标节点，再让目标节点 provision。

理由：

* 文件操作作用域**一行都不用改**。它依旧是「记录里的节点 == 调用方节点」，
  `control_plane/api/internal.py` 的 `node_file_op` / `node_slot_identity` 三处
  `owner != node_id` 检查保持原样，因此普通 create/delete 的严格度**逐字节不变**；
  `multinode_smoke` 依赖的正是这条路。选择「给迁移开一个作用域例外」则要在文件操作
  路径上引入第二套判定（目标节点从哪来、什么时候生效、失败窗口怎么收），风险面更大。
* 改节点这件事**只能由控制面做**：记录是控制面自己的，worker 请求体里既没有节点也
  没有路径（硬规则 1/3），所以「记录被指向目标」这个事实本身才是授权的来源，不带任何
  请求可控量。
* 时序上它放在 `_stop_source_runtime` **之后**、`_import/_provision` **之前**：
  源节点的 stop/清理仍然按源节点作用域走；目标节点 provision（worker create →
  `apply_sandbox_ownership` → `POST .../file-op`）时记录已经是目标节点。
* 回滚路径同步调整：先恢复 `record.node_id = old_node_id`，**再**重新 provision 源节点。
  否则失败迁移在恢复源节点时，源节点自己的 ownership 步骤又会被同一套作用域以
  「belongs to node <target>, not <source>」拒掉（原代码因为 re-point 在 provision
  之后，没有这个二次问题；时序改了必须一起改）。

代码位置：[control_plane/api/sandboxes.py:2241](../../control_plane/api/sandboxes.py)（re-point）与
[control_plane/api/sandboxes.py:2320](../../control_plane/api/sandboxes.py)（回滚先恢复记录）。

## F1 第二半：空的 502 从哪里丢的

链路：`_provision_remote` 打目标节点 agent 的 `POST /agent/sandboxes`，agent 侧
`envd_service/agent.py::agent_create_sandbox` 负责 create。里面的 `apply_sandbox_ownership`
经 `agent_fileops.chown_workspace` 拿到 CP 的 403 → 抛 `AgentFileOpsError`（`RuntimeError` 子类）。

`agent_create_sandbox` 当时只对 `PermissionError` 单独返回带正文的 500；`AgentFileOpsError`
落到最后那个

```python
except Exception:
    logger.exception("agent create sandbox failed")
    return Response(status_code=500)          # ← 正文为空
```

于是 CP 侧 `f"Node {node.node_id} failed to provision: {resp.text}"` 渲染成
`failed to provision: `（正文空）。**丢消息的位置就是这个 «空正文的兜底 500»。**

修复（[envd_service/agent.py:3024](../../envd_service/agent.py)）：

* 新增 `except AgentFileOpsError` 分支，返回带正文的 500，把模块已经命名好的拒绝
  （例如 `the control plane refused chown-workspace ... (HTTP 403): ...`）原样带上去；
* 兜底的 `except Exception` 也改成带正文（`str(e) or type(e).__name__`），落实「named,
  never silent」——这条路由由 internal key 认证，正文是控制面自己的诊断，不是泄露。

## F2：container-id 警告只留给真正需要它的形态

`envd_service/worker_identity.worker_container_id()` 用 `socket.gethostname()` 当锚点。
compose 形态下 hostname 默认就是 container id；**k8s 形态下 hostname 恒等于 pod 名**
（`e2b-worker-1`），永远不可能是 container id，而 k8s 的身份根本走 pod spec 的
`runAsUser`/`runAsGroup`（`K8sWorkerIdentitySource`，`kernel_verified=False`），
锚点在这个形态里**从不使用**。所以那条警告在 k8s 上 100% 是假消息。

修复（[envd_service/worker_identity.py:38](../../envd_service/worker_identity.py)）：

* 新增 `_identity_shape()`：读 `E2B_NODE_ADDRESS_MODE`（显式 `k8s`/`hostname`），
  未设时按「挂载了 ServiceAccount token」判定 in-cluster，与 CP 的
  `control_plane/node_address.py` 同规则（这里是镜像实现，因为 `envd_service` 不依赖
  `control_plane`）。
* `container_identity_anchor_expected()` / `reported_container_id()`：只有非 k8s 形态才
  probe 并上报 `containerID`；k8s 形态直接返回 `None`，不 probe、不发警告。
* `worker_container_id()` 本身保持原样（诚实探针 + 警告），只是改为被上面这层闸门调用；
  CP 侧「pod spec 没有 pin runAsUser/runAsGroup」那条**真实**警告在
  `control_plane/api/internal.py::_verified_worker_identity`，本次未改，保持完整。
* [envd_service/agent.py:292](../../envd_service/agent.py) 的注册与
  [envd_service/agent.py:1925](../../envd_service/agent.py) 的心跳两处调用点改为
  `reported_container_id()`。

---

## 测试与 RED 证据

新增/扩展：

* `tests/unit/test_c3_migration_fileop_scoping.py`（新）
  * `test_cross_node_migration_is_not_refused_by_the_file_op_scoping`：两个真节点、
    真 `POST /sandboxes/{id}/migrate`，`_provision_remote` 被替换成「以目标节点身份发一次
    真实 `chown-workspace` 文件操作」；断言迁移 200、文件操作 200、记录落到目标节点。
  * `test_ordinary_file_op_still_refuses_a_cross_node_request`：记录在 A、B 调文件操作
    → 精确 403 `Sandbox sbx_migrate belongs to node node_a, not node_b`，且 agent 一次没被指令。
* `tests/unit/test_c3_worker_container_identity_shape.py`（新）：k8s 形态不 probe、
  不产生该警告（显式 `k8s` 和 auto+ServiceAccount 两种）；compose 形态仍 probe 且仍警告，
  并对合法 container-id hostname 正常上报。
* `tests/unit/test_agent_create_sandbox_auth.py`（扩展）：`AgentFileOpsError` → 500 且
  `resp.text == 原因`；任意兜底异常也带原因。

RED（把三处源码 `git stash` 回退到 HEAD，仅保留测试）：

```
FAILED tests/unit/test_c3_migration_fileop_scoping.py::test_cross_node_migration_is_not_refused_by_the_file_op_scoping
FAILED tests/unit/test_c3_worker_container_identity_shape.py::test_the_k8s_shape_does_not_probe_for_a_container_id
FAILED tests/unit/test_c3_worker_container_identity_shape.py::test_the_k8s_shape_does_not_emit_the_container_id_warning
FAILED tests/unit/test_c3_worker_container_identity_shape.py::test_auto_under_a_service_account_is_the_k8s_shape
FAILED tests/unit/test_c3_worker_container_identity_shape.py::test_the_compose_shape_keeps_probing_and_its_warning
FAILED tests/unit/test_c3_worker_container_identity_shape.py::test_the_compose_shape_reports_the_kernel_hostname
FAILED tests/unit/test_agent_create_sandbox_auth.py::test_create_file_op_failure_is_500_with_reason
FAILED tests/unit/test_agent_create_sandbox_auth.py::test_create_unexpected_fault_is_500_with_reason
8 failed, 7 passed
```

（迁移用例在旧代码上的失败信息正是线上那条 403：
`502 {"message":"Node node_b failed to provision: {\"code\":403,\"message\":\"Sandbox sbx_migrate belongs to node node_a, not node_b\"}"}`。
`test_ordinary_file_op_still_refuses_a_cross_node_request` 在旧代码上通过——它 pin 的是
「作用域保持严格」这份不变量，本来就不该随修复变红。）

GREEN：新用例 15 passed。相关旧套件回归无新增失败：
`tests/unit/test_c3_*.py` + `test_agent_create_sandbox_auth.py` +
`test_migration_volume_quota.py` + `test_worker_env_key_sets.py` +
`test_worker_manifest_permissions.py` 等 373 passed。
全量 `tests/unit`（去掉缺 `redis`/`fakeredis` 的用例）1908 passed / 44 failed，
44 例全部是 macOS 上 `os.chown(...,0,..)` EPERM 之类环境失败，`git stash` 回退到 HEAD
后**同一个 44**，与本次改动无关。

---

## 真实集群验证

镜像：`PLATFORMS=linux/arm64` 构建（单平台分支只 `--load`，按提示手动
`docker push` 了 worker/agent/autoscaler/quota-agent 四个本地 tag；CP 由脚本内建 `--push`）。
推送前用 `docker manifest inspect` 逐个确认存在，并在容器内 `grep` 确认新代码确实进了
worker 与 CP 镜像。随后 `KUBECONFIG=... ./deploy/k8s-k0s/apply.sh` 滚动
broker → agent → worker，并等 CP Deployment rollout 完成，base image 预热 cached=true。

worker digest `sha256:28c2d01f…`，CP digest `sha256:4f8f708e…`。

### `multinode_smoke.py`（全绿）

```
NODE DISTRIBUTION: {'http://10.244.192.193:49983': 2, 'http://10.244.140.36:49983': 2}
ALL sandboxes: commands + files + health through gateway OK
stdin through gateway OK
after kill reservations: [('e2b-worker-0', 0), ('e2b-worker-1', 0)]
MULTI-NODE SMOKE OK
```

### `deployment_smoke.py`

F1 关心的迁移段**已通过**（之前正是这一步 502）：

```
NODE DISTRIBUTION: {'http://10.244.192.193:49983', 'http://10.244.140.36:49983'}
OK: commands + files through gateway
OK: migrated e2b-worker-1 -> e2b-worker-0, files kept
OK: network config echo + atomic update
OK: volume mounted remotely + sibling volume isolated
after kill reservations: {'e2b-worker-0': 0, 'e2b-worker-1': 0}
```

CP 日志同一窗口两次 `INFO ... migrated sandbox sbx_… from e2b-worker-1 to e2b-worker-0`，
无任何 `belongs to node` 403；worker 日志无 `HTTP 403`。

smoke 在第 5 段 `Template.build` 处退出（**环境问题，非本次改动**）：

```
e2b.exceptions.BuildException: buildkit build exited with code 1
```

buildkit sidecar 日志：

```
resolving docker.io/library/python:3.11-slim … dial tcp 202.160.128.205:443: i/o timeout
… rpc error: code = Unknown desc = mkdir /nonexistent: permission denied
```

即 k8s 集群侧到 Docker Hub 与其两个 mirror（`docker.m.daocloud.io` / `docker.1ms.run`，
`configmap/buildkitd-config` 里配的）当前都不可达，`python:3.11-slim` 拉不到。
`E2B_TEMPLATE_IMAGES` 只把别名 `py311` 指到 ACR，而 smoke 的 Dockerfile 写的是
`FROM python:3.11-slim`。这与 C3 迁移/身份改动无关（改的是 `sandboxes.py` 的迁移编排、
`agent.py` 的 create 错误回传、`worker_identity.py` 的上报闸门，均不涉及模板构建）。
注：滚动 CP 会重建 buildkit 的 emptyDir 缓存，因此这一步以前若靠热缓存绕过、现在冷缓存
才暴露；根因是出网不可达。

### F2 现场核对

滚动后两 worker 日志里 `is not a container id` 计数均为 **0**（滚动前每几秒一条）。

---

## 顾虑 / 未决

1. **模板构建**：`deployment_smoke` 走不完全程不是本次改动导致，但会持续挡冒烟。
   需要把 smoke 的 `FROM python:3.11-slim` 改成走 `E2B_TEMPLATE_IMAGES` 的别名，或给集群
   配一个可达的 docker.io mirror（超出本次 C3 范围）。
2. **`E2B_NODE_ADDRESS_MODE` 双读**：worker 侧 `_identity_shape()` 与 CP 的
   `node_address.py` 是两份实现（有意分层，不 import）。若将来新增形态（例如新的
   identity source），两处需同步；建议后续把该判据下沉到 `gateway_common`。
3. **迁移窗口语义**：F1 让记录在目标 provision 期间就指向目标。此时源已停、网关路由
   尚未失效（迁移末尾才 invalidate），窗口内请求仍可能被路由到已停的源并失败——这与
   改前一致（改前记录虽在源、源也已停），没有引入新的双活，但值得记一笔。
4. 我未改 `deploy/k8s*` 清单；F2 通过运行时探测（ServiceAccount token）区分形态，
   无需给 worker 注入 `E2B_NODE_ADDRESS_MODE`。
