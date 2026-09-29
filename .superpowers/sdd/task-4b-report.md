# Task 4 / slice B 报告：部署面（worker 去特权 + agent 面 B 载荷 + D22 双端点）

- 工作树：`/Users/polus/project/ai/sandlock-e2b/tmp/wt-c3`（分支 `feat/c3-consolidation`）
- BASE：`d2bfd84`（slice A 第五轮 mini-fix）
- 裁定：**D22**（面 B 独立端点；两条进程、两个监听）、D1（worker 镜像无特权二进制 + 无 BND）、
  D17（`deploy/stack/docker-compose.prod.yml` 在 C3 覆盖内）、D21（选项 1 的部署前提：k8s pin 身份；
  compose 加 `pid: host`）、Global Constraints（禁项、只有两条通道、`local://` 不动）
- **未做**：真机验收（判据 1/2/3/4/7/13/15/16 的机器臂）、部署、`local://` 形态、Task 5/6/7 的范围

---

## 0. 一句话

```
worker（零特权二进制、BND 空集）--{sandbox_id, op}--> CP --{verb, path, uid,gid}--> agent 面 B（49986）
                                                        └--{sandbox_id, uid, pid}--> agent 面 A（49985）
```

两个面**必须是两个进程**（面 A 要 euid 65534 才写得进 `uid_map`——owner 规则；面 B 要 uid 0 才能在
NFS AUTH_SYS 上 `chown`，且只有它挂了四个白名单根），所以是**两个监听**：k8s 同一个 pod IP 的两个端口
（49985/49986），compose 两个服务（`c3-agent`/`c3-agent-maint`）。CP 侧**一个 client**，按 op 选目标。

---

## 1. D22：面 B 的独立端点

### 代码（`control_plane/`）

| 文件 | 改动 |
|---|---|
| `config.py` | 新增 `c3_agent_maint_url`（`E2B_C3_AGENT_MAINT_URL`）、`c3_agent_maint_port`（`E2B_C3_AGENT_MAINT_PORT`，默认 **49986**） |
| `c3_agent_client.py` | `AgentTarget` 增 `maint_url`（face B）；`ComposeAgentAddressResolver(url, maint_url)`；`K8sAgentAddressResolver(..., maint_port=)`: `maint_url = {scheme}://{同一个 pod IP}:{maint_port}`；`build_agent_address_resolver` 透传；`_instruct/_post` 改为**收显式 url**；`_file_op` 用 `target.maint_url`，**为 None 时具名 503**（`cannot determine the file-operation agent address for node … (E2B_C3_AGENT_MAINT_URL / E2B_C3_AGENT_MAINT_PORT)`），**绝不回落面 A** |

- 仍然**一个** client 模块、一套寻址/凭据/并发旋钮/拒绝文案；没有第二份实现。
- face B 的地址来自**与 face A 同一次可信查找**：k8s 是同一个 agent pod（同 netns）的第二个端口；
  compose 是**部署命名**的第二个服务名（`validate_node_id` 校验其 host，坏 host 直接丢弃 → 文件 op 503）。
  **任何**路径都不取请求体里的地址。

### 清单

| 文件 | 改动 |
|---|---|
| `deploy/k8s/control-plane.yaml` | 加 `E2B_C3_AGENT_MAINT_PORT: "49986"` |
| `deploy/k8s/c3-agent.yaml` | 面 B：`ports: [49986]`、`E2B_C3_AGENT_PORT=49986`、`E2B_C3_AGENT_NODE_ID`(spec.nodeName)、`E2B_C3_AGENT_TOKEN`、四个根 + `E2B_UID_POOL_START/SIZE`(10000/1000)、liveness/readiness 探针自连 49986 |
| 同文件 NetworkPolicy | ingress **一条规则的端口列表两项**（49985+49986），来源仍是**只有** `app=control-plane` 一个 podSelector |
| `deploy/compose/docker-compose.{prod,multinode}.yml`、`deploy/stack/docker-compose.prod.yml` | 面 B 服务加 `E2B_C3_AGENT_PORT=49986`/`NODE_ID`/`TOKEN`；CP 加 `E2B_C3_AGENT_MAINT_URL`（默认 `http://c3-agent-maint:49986`） |
| `deploy/compose/.env.example` | `E2B_C3_AGENT_MAINT_URL` 注释行 |

---

## 2. 项 1/2：worker 去特权二进制与 BND、同批切 agent 形态

| 文件 | 改动 |
|---|---|
| `deploy/docker/Dockerfile.envd` | **删掉** `/var/lib/e2b-priv` 整块：builder 里两个二进制的编译、`COPY --from=builder`、`setcap`/`getcap`、以及只为它们装的 `libcap2-bin`（注释保留，写明为什么没了 + 判据 2/15 的可断言性来自 `Dockerfile.agent`） |
| `deploy/k8s/worker.yaml` | 删 `securityContext.capabilities`（BND **空集**）；`E2B_PRIV_HELPER_TRANSPORT: socket → agent`；**显式 pin `runAsUser: 65534` + `runAsGroup: 65534`**；注释同步 |
| 三个 compose worker（anchor/单服务） | 加 `E2B_PRIV_HELPER_TRANSPORT: agent`（stack 另加 `E2B_SLOT_IDENTITY: agent-grant`）；stack worker 的 `cap_add`（四条 file-cap caps）**删除**，`cap_drop: [ALL]` 保留 |
| `deploy/k8s/priv-broker.yaml` | 4 处 image 由 **worker → agent 镜像**（见 §6 concerns 第 1 条） |

⚠ 为什么 transport/slot 与删二进制**必须同批**：`auto` 在"镜像里没有 broker"时会**静默**退到进程内 E5.1
（无 per-sandbox uid、无 route-B，只有一条 WARNING），那是最难发现的一种降级。三个 C3 compose 栈 +
k8s 现在都显式 `agent`。

---

## 3. 项 3/4：三个 compose 栈的 agent 服务 + face B 载荷

- `deploy/compose/docker-compose.prod.yml` / `docker-compose.multinode.yml`：Task 3 已有两个服务，
  本片**装载荷**（去掉 `sleep infinity`，改跑镜像默认 CMD = `python3 -m deploy.c3_agent`）、加
  `E2B_C3_AGENT_NODE_ID`/`_TOKEN`/`_PORT`、四个根、uid 池，并**加 `pid: host`**（项 5 的 compose 半边）。
- **`deploy/stack/docker-compose.prod.yml`（D17）**：Task 3 没给它 agent —— 本片**新建**
  `c3-agent`（`pid: host`、`user 65534:65534`、`cap_add [SETUID,SETGID]`）与 `c3-agent-maint`
  （`user 0:0`、`pid: host`、`cap_drop[ALL]` + `cap_add [CHOWN,DAC_OVERRIDE,FOWNER]`、
  `E2B_C3_AGENT_PORT=49986`、四根、uid 池），CP 侧加 `E2B_C3_AGENT_URL`/`_MAINT_URL`/`_TOKEN`/
  `_MAX_CONCURRENCY`，worker 侧加 `E2B_SLOT_IDENTITY`/`E2B_PRIV_HELPER_TRANSPORT`。
- **uid 池对齐**：stack 的 worker-1 池是 `10000..10999`、worker-2 是 `11000..11999`，而它们**共用一个
  agent** ⇒ 该 agent 的池取**并集** `E2B_UID_POOL_START=10000` + `E2B_UID_POOL_SIZE=2000`（`priv_common.c`
  按 `START..+SIZE` 校验 `--uid`，池窄了会把 worker-2 的每一步变成 agent 的具名拒绝）。prod/multinode
  两个栈的 worker 走码默认 10000/1000，agent 同值。
- 构建/发布接线：`deploy/stack/.env.example` 加 `AGENT_IMAGE` + `E2B_C3_AGENT_TOKEN`（占位符），
  `deploy/scripts/upgrade.sh` 首部署生成该 token 并把 `AGENT_IMAGE` 纳入版本 pin 循环，
  `deploy/scripts/lib/helpers.sh` 把它加进 `PRESERVED_REMOTE_SECRET_KEYS`（否则一次普通重部署会把
  已在跑的 token 清空 ⇒ 每个文件 op 与每个槽位 401）。

---

## 4. 项 5/6：身份来源与 CP 配置

- **k8s（项 5 上半）**：`worker.yaml` 的 worker 容器 pin `runAsUser: 65534` / `runAsGroup: 65534`
  （pod 级**不**覆盖）。CP 的可信来源读的就是 pod spec 的 `securityContext` —— 只靠镜像 `USER` 会读成
  "未知" ⇒ 不记身份 ⇒ 每个需要身份的 op 具名 503。
- **compose（项 5 下半）**：三个栈的 face B 都加 `pid: host`（D21 选项 2 的**部署前提**）。
  ⚠ **选项 2 本身需要 agent 今天没有的代码**（按 worker pid 读 `/proc/<pid>` 属主）：agent 目前只把 CP
  指令里的 `worker.uid/gid` 写进子进程环境。所以 compose 车道今天仍走**选项 1 的 fail-closed 一侧**
  （`NoWorkerIdentitySource` ⇒ 具名 503），`pid: host` 是为那一步预留。见
  `docs/c3-privilege-relocation.md` §11.2.1 第 9 条（已改写为"片 B 已落 `pid: host`，选项 2 待代码"）。
- **项 6**：CP（k8s + 三个 compose 栈）加 `E2B_ROUTE_B_TMP_ROOT`（k8s `/var/lib/e2b-sandboxes/state/.route-b`，
  compose `/var/lib/e2b-sandboxes/.route-b`，**与 worker 逐字一致**）；k8s CP 的
  `E2B_IMAGE_CACHE_DIR` 由 `_images` 改为 worker 的节点本地 `/var/lib/e2b-images`（`chown-secret` 的路径
  = worker 写 secret 的目录），并**显式** `E2B_IMAGE_OCI_DIR=/var/lib/e2b-sandboxes/_images`（模板 OCI tar
  必须留在共享卷）。两个 compose 栈本来三处同值，不动。

---

## 5. RED → GREEN（每条新 pin 都先看过红）

| 判据 | 退回方式（临时改回，跑完立刻恢复） | RED | GREEN |
|---|---|---|---|
| D22 路由：grant→面 A、file op→面 B | `_file_op` 的 `url=target.maint_url` → `url=target.url` | `assert ['http://c3-agent:49985/.../walk'] == ['http://c3-agent-maint:49986/.../walk']`（`test_the_two_faces_are_two_endpoints_and_each_op_uses_its_own` failed） | 30 passed（`test_c3_agent_client.py`） |
| 无面 B 端点时文件 op 具名 503 | 删 `_file_op` 的 `if not target.maint_url` 分支 | 该用例拿到 200（dialled face A）而不是 503 | 同上（同一支 30 条里） |
| k8s CP 的 face B 端口 | 删 `control-plane.yaml` 的 `E2B_C3_AGENT_MAINT_PORT` | `KeyError: 'E2B_C3_AGENT_MAINT_PORT'`（`test_the_control_plane_is_given_the_agent_channel_and_a_sized_limit`） | 21 passed（`test_c3_agent_manifest.py`） |
| worker BND 空集 | 给 `worker.yaml` 的 worker 容器加回 `capabilities.add: [SETUID]` | `assert 'capabilities:' not in <worker.yaml>` + `assert 'capabilities' not in {... 'capabilities': {'add': ['SETUID']} ...}`（两条各自红） | 53 passed + 26 passed（见下） |
| 判据 2/15：worker 镜像无特权二进制 | 本片之前 pin 的是"**镜像里还有**两个二进制"（`test_the_worker_image_still_carries_both_binaries_at_this_point`）——删掉镜像里那块之后该 pin 当场红（首次跑 `test_c3_agent_manifest.py`：`FAILED …still_carries_both_binaries_at_this_point`），本片把它改写成反向 pin（worker 镜像无、agent 镜像有，**逐字**断言 caps） | `assert 'e2b-slot-spawn' not in worker_text` 的反向臂：把旧 COPY 行放回即红 | 21 passed |
| NetworkPolicy 两端口 | 把 `c3-agent.yaml` ingress 的 `ports` 收回只留 49985 | `assert [...] == [ {...49985}, {...49986} ]` 红 | 21 passed |
| compose face B `pid: host` / 端口 / 池 | 去掉 stack 的 `pid: host` 或 `E2B_C3_AGENT_PORT` | `KeyError`/`assert 'host' == None` | 21 passed |
| stack worker 的 route-b 读法（旧 helper 用整文件唯一行） | 不再唯一（CP 也有同名键）⇒ `expected exactly one 'E2B_ROUTE_B_TMP_ROOT' line …: [两行]` | 改成读 **worker anchor** | 53 passed |

**首轮（改完代码/清单、pin 还没更新时）的红**（16 failed，`test_c3_agent_manifest.py` + `test_worker_manifest_permissions.py`
+ `test_worker_env_key_sets.py` + `test_c3_agent_client.py`）就是这一片的 RED 证据：
`test_the_worker_image_still_carries_both_binaries_at_this_point`、`test_the_networkpolicy_…`、
`test_stack_worker_has_no_cap_add_…`、`test_k8s_worker_drops_sys_admin_…`、
`test_k0s_overlay_moves_the_seccomp_root_…`（worker 现在 pin `runAsUser`）、
`test_socket_transport_and_its_broker_are_inseparable`（transport 已变 `agent`）、
`test_compose_prod_worker_env_carries_the_fleets_route_b_root`、`test_every_worker_stack_declares_…`。
它们逐条改成新形状（不是放宽）后全绿。

**另外三条是本片引入的真回归**（在整支 `tests/unit` 的差集里发现，不在上面那 16 条里）：
`test_c3_agent_fileops.py::test_the_real_agent_service_accepts_the_clients_instruction`（`AgentTarget` 缺
`maint_url` ⇒ 文件 op 503）、`test_c3_internal_api_shape.py` 的两条（`worker_pod_manifest_carries_no_net_raw`
的 caps 集合应从 `{SETGID,SETUID}` 变**空**；`no_worker_shape_carries_the_agent_token` 里
`deploy/stack/docker-compose.prod.yml` 从"纯 worker 文件"变成"CP+agent+worker 同文件"，要改成按服务扫 +
反向断言两面**确实**带 token）。

---

## 6. 命令与输出（照抄）

```bash
# 渲染检查（kubectl 1.34）
$ kubectl kustomize deploy/k8s > tmp/render-k8s.yaml ; echo $?
0
$ kubectl kustomize deploy/k8s-k0s > tmp/render-k0s.yaml ; echo $?
0

# 渲染后的关键值（解析 tmp/render-k8s.yaml）
cp E2B_ROUTE_B_TMP_ROOT = /var/lib/e2b-sandboxes/state/.route-b
cp E2B_IMAGE_CACHE_DIR  = /var/lib/e2b-images
cp E2B_IMAGE_OCI_DIR    = /var/lib/e2b-sandboxes/_images
cp E2B_C3_AGENT_MAINT_PORT = 49986
worker securityContext: {'runAsUser': 65534, 'runAsGroup': 65534, 'seccompProfile': {...}}
worker transport: agent | socket: /run/e2b-broker/broker.sock | slot: agent-grant
agent face agent ports [{'containerPort': 49985}]
agent face maint ports [{'containerPort': 49986}]

# compose 渲染检查（仓库既有做法：c4-prjquota-window.sh 用 `docker compose … config --quiet`）
$ docker compose -f deploy/compose/docker-compose.prod.yml   --env-file deploy/compose/.env.example config --quiet ; echo $?
0
$ docker compose -f deploy/compose/docker-compose.multinode.yml --env-file deploy/compose/.env.example config --quiet ; echo $?
0
$ docker compose -f deploy/stack/docker-compose.prod.yml     --env-file deploy/stack/.env.example config --quiet ; echo $?
0

# 渲染后的 stack（关键服务）
control-plane   {E2B_C3_AGENT_URL: http://c3-agent:49985, E2B_C3_AGENT_MAINT_URL: http://c3-agent-maint:49986, E2B_ROUTE_B_TMP_ROOT: /var/lib/e2b-sandboxes/.route-b}
c3-agent        pid=host user=65534:65534 caps=[SETUID,SETGID]      port=49985
c3-agent-maint  pid=host user=0:0        caps=[CHOWN,DAC_OVERRIDE,FOWNER] port=49986 pool=10000/2000
worker-1        caps=None                transport=agent slot=agent-grant  pool=10000/1000
worker-2        caps=None                transport=agent slot=agent-grant  pool=11000/1000

# 宿主 lane：单测全档（macOS：fakeredis 收集错误既有，故加 --continue-on-collection-errors）
$ /Users/polus/project/ai/sandlock-e2b/.venv/bin/python -m pytest tests/unit -q --continue-on-collection-errors
49 failed, 1806 passed, 30 skipped, 1 error in 126.98s
# 与"同一 HEAD 的对照 worktree"逐条比对（基线 = d2bfd84）：
$ diff tmp/base-unit.txt tmp/wt-unit2.txt && echo IDENTICAL to baseline (unit)
IDENTICAL to baseline (unit)

$ /Users/polus/project/ai/sandlock-e2b/.venv/bin/python -m pytest tests/contract -q --continue-on-collection-errors
1 failed, 353 passed, 65 skipped, 2 errors in 101.07s
# 与基线对照：唯一差异是基线**多**一条时序 flake
#   test_teardown_failure_semantics.py::test_a_refused_tree_is_parked_and_the_row_it_pinned_is_released[quotactl]
# （两个 error 都是 `RuntimeError: this lane only runs on Linux` 的环境项）

# 本片直接相关/被改动的 13 个文件
$ … -m pytest tests/unit/test_c3_agent_manifest.py tests/unit/test_c3_agent_client.py tests/unit/test_c3_agent_fileops.py \
    tests/unit/test_c3_fileops_forwarding.py tests/unit/test_c3_fileops_worker.py tests/unit/test_c3_internal_api_shape.py \
    tests/unit/test_worker_manifest_permissions.py tests/unit/test_worker_env_key_sets.py tests/unit/test_upgrade_quota_agent_profile.py \
    tests/unit/test_cp_state_base.py tests/unit/test_compose_base_image_shape.py tests/unit/test_k0s_secrets_script.py \
    tests/unit/test_image_rootfs_cache_split.py -q
272 passed in 27.98s
```

基线那 49 条失败**逐条与本片无关**（macOS：`os.chown` 需 root、AF_UNIX、`getcap`/file-caps、`xfs_quota` 探测、
`test_pause_quota.py` 的 fakeredis 收集错误）—— 用 `git worktree add --detach tmp/baseline HEAD` 造了一份
**同一 HEAD 的对照树**，跑同一组命令得到**逐条相同**的失败集合（脚本与对照见上）。

---

## 7. 文件清单

**代码**：`control_plane/config.py`、`control_plane/c3_agent_client.py`。

**清单**：`deploy/docker/Dockerfile.envd`、`deploy/k8s/worker.yaml`、`deploy/k8s/c3-agent.yaml`、
`deploy/k8s/control-plane.yaml`、`deploy/k8s/priv-broker.yaml`、
`deploy/compose/docker-compose.prod.yml`、`deploy/compose/docker-compose.multinode.yml`、
`deploy/stack/docker-compose.prod.yml`、`deploy/stack/.env.example`、`deploy/compose/.env.example`、
`deploy/scripts/upgrade.sh`、`deploy/scripts/lib/helpers.sh`。

**测试**：`tests/unit/test_c3_agent_manifest.py`（+1 新用例、改写 4 条）、`tests/unit/test_c3_agent_client.py`
（+2 新用例、3 处改）、`tests/unit/test_c3_agent_fileops.py`、`tests/unit/test_c3_internal_api_shape.py`、
`tests/unit/test_worker_manifest_permissions.py`、`tests/unit/test_worker_env_key_sets.py`、
`tests/unit/test_upgrade_quota_agent_profile.py`。

**文档**：`docs/deploy-clusters.md`（§7.6 现状节）、`docs/k8s-deployment.md`（清单表 + token 表）、
`docs/production-deployment-requirements.md`（§2.4.1 权限表、§2.7.1 缓存落点）、
`deploy/k8s-k0s/README.md`、`README.md`（env 表）、`docs/c3-privilege-relocation.md`（§11.2.1 第 8/9 条）、
`docs/superpowers/plans/2026-09-28-c3-privilege-consolidation.md`（验收矩阵行 2/15）。

---

## 8. 有意留给验收轮

1. **判据 2/15 的机器臂**（worker `CapEff=0`、镜像里 `getcap /var/lib/e2b-priv/*` 为空 / 目录不存在）——
   单测 pin 已落，真机 grep 属验收轮（需部署窗口）。
2. **判据 4（面 B `CapEff=0x0b`）**：载荷装上后才能真机 grep（`E2B_C3_AGENT_*` 双端点的真机连通也一并）。
3. **判据 1/13/16**：`probe_c3_userns_map_handoff.py` 两臂、`NSpid`+cgroup 双命中、并发建箱——
   13/16 必须在 compose multinode（3 worker 同机）上跑；本片**未碰集群**。
4. **compose 车道的文件 op**：D21 选项 2 待代码（见 §4），今天该车道凡需身份的 op 具名 503 —— 这是
   设计内的 fail closed，不是本片引入的缺陷；`pid: host` 已就位。

---

## 9. Concerns（本片自己发现的、需要控制面裁定或下一位实施者接手）

1. **C1 broker 的镜像**（本片**新增**的一处改动，不在派发清单的字面范围里）：
   `deploy/k8s/priv-broker.yaml` 的 4 个容器原本跑 **worker 镜像**（`e2b-maint` 只在那儿），而项 1 要求
   worker 镜像删掉它 —— 不处理的话 broker 主容器 exec 失败 ⇒ CrashLoop ⇒ `apply.sh` 的
   `rollout status ds/e2b-priv-broker` 卡死 ⇒ **worker 根本起不来**（worker 的 `wait-for-broker` 闸门还在，
   它也是 `socket` 回退的一部分）。本片的处理：把那 4 处 image 指到 **agent 镜像**（唯一还有
   `e2b-maint` 的镜像），broker 的 caps/命令/peer 门一个字没改，Task 7 照原计划退役整个 DaemonSet。
   钉子：`test_the_workers_upstream_is_the_agent_and_the_broker_stays_consistent`（渲染后断言 broker 的 image）。
   若控制面更希望"现在就退役 broker"，那属于 Task 7，请按 Task 7 的清单做（含 `wait-for-broker` 与
   `E2B_PRIV_HELPER_SOCKET` 的删除）。
2. **autoscaler 的 docker pool 与单机示例会降级**：`deploy/compose/docker-compose.autoscale.yml`（pool）与
   `deploy/compose/docker-compose.yml`（单机示例）的 worker 都靠 worker 镜像里的 file-cap 二进制（`auto`+
   `exec`）来开 per-sandbox uid / route-B；镜像删掉二进制后，它们会**静默**退到进程内 E5.1（有 WARNING），
   即失去 per-sandbox host uid 与 route-B。派发清单只点了三个 C3 compose 栈，所以本片**没动**这两个文件；
   要么给 pool 配一个 agent（并解决"CP 如何寻址 pool 里的 worker 宿主"这一设计点），要么显式裁定这两个
   形态在 C3 覆盖外（`local://` 同等处理）。**这一条请控制面裁定**。
3. **worker 的 `wait-for-broker` init 闸门**保留（它是 `socket` 回退的一部分），因此 k8s 上线时 broker 仍必须
   健康；`E2B_PRIV_HELPER_SOCKET` 同理保留。Task 7 退役 broker 时两者一起删。
4. **stack 的 agent uid 池**取的是两个 worker 池的**并集**（10000/2000），且随
   `E2B_UID_POOL_START`/`E2B_C3_AGENT_UID_POOL_SIZE` 可覆盖。若运维只改 `E2B_UID_POOL_START_WORKER2`
   而不改 agent 的池，worker-2 的文件 op 会在 agent 侧具名拒绝（**可见**，不是静默）；更好的做法是让
   agent 的池从一个显式"本机所有 worker 池的并集"变量派生，属于下一个把"池一致性"做成注册期检查的任务。
5. **CP 的 `E2B_IMAGE_CACHE_DIR` 现在指到节点本地路径**：分离形态下 CP 不读写它（只用来拼 secret 路径），
   所以无害；但 `local://` 形态的 CP 会**真的**用它作缓存目录（`api/sandboxes.py` 的两个 local 分支），
   而 `local://` 不在 C3 覆盖内、其清单也不会设这个值 —— 记为一条"改这个变量时要同时想到 local lane"的提醒。

---

# 附录 F：评审修复轮（2026-09-29，D1/D22/D21-8/9 通过后提出 4 项 + 2 项记录）

工作树/分支不变（`tmp/wt-c3`，`feat/c3-consolidation`）。BASE：`3030722`。未部署、未碰集群、未跑验收探针。

## F.1 逐项

### ① 裁定 D23：两个被排除的形态必须自己声明"没有文件操作能力"

`deploy/compose/docker-compose.autoscale.yml`（pool 的 `E2B_AS_WORKER_ENV` JSON）、
`autoscaler/backends/local.py`（`self._env`，手搓 backend 的那份）与 `deploy/compose/docker-compose.yml`
（单机示例的 `envd`）各加 **`E2B_PRIV_HELPERS: "off"`** —— 这是 `E2B_PRIV_HELPERS` **已有**的
"从不用 broker"取值（`resolve_priv_helpers` 见 `off` 直接返回 `None`，`helpers_unavailable_reason`
对它返回 `None`，也就是**不再有那条 WARNING**：声明取代了"静默降级"）。三处都写了注释说明
"本形态没有特权文件操作能力"+ 为什么（worker 镜像已无二进制）+ 覆盖范围排除。

顺带记录的事实（写进注释与文档）：单机示例在片 B 之前是**拒绝启动**的（默认 route-B 根
`/tmp/sandlock-route-b` 在 broker 白名单外 ⇒ `_require_route_b_scratch_root` 抛错），片 B 之后
`auto` 解析不到 broker 会让它**静默**降级 —— 这正是 C3 要防的形态，所以 `off` 是升级而不是遮掩。

**钉子**（新）：
`tests/unit/test_c3_agent_manifest.py::test_the_shapes_excluded_from_c3_declare_that_they_have_no_file_ops`
—— 逐字断言两处取值 `== "off"`（demo 的 env key、pool 的 JSON 里那一个键），并断言两个文件**不含**
`E2B_SLOT_IDENTITY`/`E2B_PRIV_HELPER_TRANSPORT`/`E2B_C3_AGENT_URL`/`_MAINT_URL`/`_TOKEN`。
`tests/unit/test_worker_env_key_sets.py` 的分类相应拆分：`_POOL_MISSING`/`_DEMO_MISSING` 去掉
`priv_helpers` 白名单（现在**声明**了），`_FLEET_STACK_MISSING` 补了说明"fleet stack 现在同时命名
transport 与 slot identity"的注释。

### ② 部署窗口阻塞项：k8s runbook 还把 broker 指向 worker 镜像

`docs/k8s-deployment.md`：

| 位置 | 改动 |
|---|---|
| `set image` 块 | `ds/e2b-priv-broker broker=` 改成 **agent 镜像**；新增 `ds/e2b-c3-agent agent=… maint=…`（两个容器显式列两次） |
| "同一个镜像仓库、必须同版本滚" | 改成：**broker 与 agent 同镜像（`e2b-sandlock-agent`）、worker 是另一个仓库**；broker 指错镜像 = CrashLoop → worker 的 `wait-for-broker` 闸门把 worker 一起堵死；升级顺序 **broker → agent → worker**；自查命令换成 `custom-columns` 打全部镜像 |
| 清单表（`priv-broker.yaml` 行） | 标注"片 B 起跑 agent 镜像，Task 7 退役" |
| 清单表（`worker.yaml` 行） | 改成"显式 pin `runAsUser/runAsGroup: 65534`、无 cap 声明、`transport=agent` + `slot=agent-grant`" |
| compose↔k8s 差异表 route B 行 | 改成 C3 形态（CP 转发 → agent 面 A 写 map；BND 空集） |
| "worker 侧关键 env" 段 | `E2B_PRIV_HELPERS=auto`/`E2B_PRIV_HELPER_TRANSPORT=socket` 改为 `agent` + `E2B_SLOT_IDENTITY=agent-grant`；补 CP 侧三个新值（`E2B_C3_AGENT_MAINT_PORT`、`E2B_ROUTE_B_TMP_ROOT`、cache/OCI 拆分） |
| 撤 broker 的 ⚠ 块 | 改成"出厂 `agent` 下残留 socket 是惰性的；只有切回 `socket` 才需要清文件；`exec` 已不是可行回退（二进制没了）" |
| §24.2 回退段 | 补一句"回退要连镜像一起退，`exec` 需要旧镜像里的二进制" |

### ③ 部署窗口阻塞项：stack 的升级路径发不出 agent token

`deploy/scripts/lib/helpers.sh` 新增 **`ensure_c3_agent_token <env_file> [fallback]`**（add-if-missing：
缺失/空/仍是 `__C3_AGENT_TOKEN__` 占位符时写入；已有可用值一律不动；`fallback` 为空则
`openssl rand -hex 24`）。`deploy/scripts/upgrade.sh` 在 **carry-over 之后、镜像 tag pin 之前**调用它，
fallback 取 `remote_env_value E2B_C3_AGENT_TOKEN`（目标机已有的值优先，避免"例行升级把舰队凭据换掉"），
并按结果打一条 `已补上 …（沿用目标机已部署的值）` / `已生成 …` 的日志。

**钉子**（新文件）：`tests/unit/test_upgrade_c3_agent_token.py`，6 条 —— 缺失⇒生成（48 hex）、
占位符⇒不算 token、空值⇒填充、真值⇒逐字不动、有远端值时逐字采用、env 文件不存在时不报错不创建；
最后一条用**精确位置**断言调用点在 carry-over 与镜像 pin 之间，且确实读了 `remote_env_value`。

### ④ 文档/注释清扫

| 位置 | 改动 |
|---|---|
| `docs/production-deployment-requirements.md:1955` | §5.4(b) 重写为"euid 0 归 broker **与 agent 面 B**；worker 显式 pin 65534/65534、无 cap、`transport=agent`"；引用的测试名改成 `test_the_workers_upstream_is_the_agent_and_the_broker_stays_consistent` |
| 同文件 §6 验收"第 3 步"（原 `:2038-2041`） | 判据改成：worker `securityContext` = `{runAsUser: 65534, runAsGroup: 65534}` 且**无 capabilities**；worker env 含 `transport=agent`/`slot=agent-grant`；broker `runAsUser: 0` **且镜像是 agent 镜像**；新增 agent 面 A/B 的 `runAsUser`（`65534 0`） |
| 同文件 `:246`（`/var/lib/e2b-priv` 段）与构建期段（`:240-241`） | 标注"这个目录只在 **agent 镜像**里；worker 镜像里没有"（DAC 模型逐字不变） |
| 同文件 `:312-313` | §6 的 C1 修订段改成 C1+C3 双修订 |
| `README.md` 重复行 | 删掉重复的 `E2B_PRIV_HELPER_TRANSPORT（C3 形态）` 行，把 `agent` 形态并入唯一那一行（默认列 `auto` → "`auto`（出厂 manifest 显式 `agent`）"） |
| `deploy/k8s/worker.yaml:223` | `_chown_path` 回退注释里的"its `E2B_PRIV_HELPER_TRANSPORT` is `socket`"改为"C1 的 broker / C3 的 agent，两种形态下都由特权侧的白名单决定 chown 能否发生" |
| `deploy/k8s/priv-broker.yaml:1-56` | 头部改成"**片 B 起它是 socket 回退形态**、跑 agent 镜像"；"三处同源"里补"worker pod 现在显式 pin"；上线顺序段改成 **broker → agent → worker** |
| `deploy/k8s/worker.yaml` wait-for-broker 注释 | 写明它是 `socket` 回退的闸门、已不在出厂路径上，但 broker 起不来仍会在这里响亮失败 |
| `deploy/k8s-k0s/README.md` | worker 段更新为"显式 pin 身份 + 无 cap + `agent` 形态；euid 0 在 agent 面 B；broker 保留到 Task 7"（顺带修掉"worker 不写 `runAsUser`"） |
| `tests/unit/test_worker_env_key_sets.py:334-342` | `_FLEET_STACK_MISSING` 补注释：fleet stack 就是 `deploy/stack/docker-compose.prod.yml`，它现在**同时命名** `E2B_PRIV_HELPER_TRANSPORT` 与 `E2B_SLOT_IDENTITY`（所以两者不在这个"缺"集合里） |
| `deploy/scripts/c4-prjquota-window.sh:636` | `getcap` 改成两行：worker 上"预期 no such file"（输出带说明前缀）、**agent 面 B 容器**（`sandlock-c3-agent-maint-1`）上取两个二进制的 caps；都不参与 fail 判定 |
| broker 镜像的字面量断言 | `test_the_workers_upstream_is_the_agent_and_the_broker_stays_consistent` 改成与**同一份渲染里 agent 面 B 的 image** 逐字相等（broker 主容器与 3 个 init 都是），不再写死 tag |

### ⑤ 记录 compose 车道的真实后果（比"身份类 op 503"更重）

`docs/c3-privilege-relocation.md` §11.2.1 第 9 条补一段：属主交棒是**建箱路径上的第一个特权步骤**
（`envd_service/agent.py:2869-2870` → `uid_pool.apply_sandbox_ownership` → `agent_fileops.chown_workspace`），
它要的 `chown-workspace` 在 CP 侧先要可信的 `worker_uid/gid` —— compose 拿到 `null` ⇒ 具名 503 ⇒
**`Sandbox.create()` 直接失败**（不是"建好了但身份操作不可用"）。⇒ **compose 的部署窗口必须等
D21 选项 2 的 agent 代码**（或把 transport 留在带特权二进制的旧形状上）；k8s 不受影响。

### ⑥ 记录 uid 池耦合

同文件新增 §11.2.1 第 10 条：stack 的 face B 用 `E2B_UID_POOL_START`（默认 10000）+
`E2B_C3_AGENT_UID_POOL_SIZE`（默认 **2000** = worker-1 `10000..10999` ∪ worker-2 `11000..11999`），
而两个 worker 的池由各自独立的旋钮控制 ⇒ 把 worker-2 的池挪出并集、又不同步抬 agent 的池，
会让 worker-2 的每一步在 agent 侧具名拒绝（安全、可见）；更好的形状（显式并集变量或注册期一致性检查）
记进 Task 5/6 候选。`deploy/stack/.env.example` 补上 `E2B_C3_AGENT_UID_POOL_SIZE=2000`（带推导注释）。

## F.2 RED → GREEN（本轮新增/改动的 pin 都先看过红）

| 判据 | 退回方式（临时改回，跑完立刻恢复） | RED | GREEN |
|---|---|---|---|
| D23：排除形态声明 | 删 `docker-compose.yml` 的 `E2B_PRIV_HELPERS: "off"` | `KeyError: 'E2B_PRIV_HELPERS'`（`test_the_shapes_excluded_from_c3_declare_that_they_have_no_file_ops`） | 该文件 22 passed |
| D23 的 pool 一半 | 删 JSON 里的键（同一用例会红，pool 断言在后） | 同上（`json.loads(…)[…]` KeyError） | 同上 |
| stack 补 token（生成） | 把 `ensure_c3_agent_token` 的写入那段换成 `return 0` | `KeyError: 'E2B_C3_AGENT_TOKEN'`、`AssertionError: __C3_AGENT_TOKEN__`、两处 `assert None = re.match(...)`（4 条同时红） | `tests/unit/test_upgrade_c3_agent_token.py` 6 passed |
| stack 补 token（顺序） | 把调用挪到 carry-over 之前（或删除） | `assert carry_over < backfill` 分支红 / `ValueError: substring not found` | 同上 |

## F.3 命令与输出（照抄）

```bash
# 渲染检查
$ kubectl kustomize deploy/k8s >/dev/null && kubectl kustomize deploy/k8s-k0s >/dev/null && echo "kustomize OK"
kustomize OK
$ for f in deploy/compose/docker-compose.yml deploy/compose/docker-compose.autoscale.yml \
           deploy/compose/docker-compose.prod.yml deploy/compose/docker-compose.multinode.yml \
           deploy/stack/docker-compose.prod.yml; do docker compose -f "$f" --env-file <对应 .env.example> config --quiet && echo "$f OK"; done
deploy/compose/docker-compose.yml: OK
deploy/compose/docker-compose.autoscale.yml: OK
deploy/compose/docker-compose.prod.yml: OK
deploy/compose/docker-compose.multinode.yml: OK
deploy/stack/docker-compose.prod.yml: OK

# 本片相关 10 个文件
$ … -m pytest tests/unit/test_c3_agent_manifest.py tests/unit/test_worker_manifest_permissions.py \
    tests/unit/test_worker_env_key_sets.py tests/unit/test_autoscaler_local_backend_shape.py \
    tests/unit/test_upgrade_c3_agent_token.py tests/unit/test_upgrade_quota_agent_profile.py \
    tests/unit/test_c3_agent_client.py tests/unit/test_c3_internal_api_shape.py \
    tests/unit/test_c3_agent_fileops.py tests/unit/test_c3_fileops_worker.py -q
204 passed in 9.59s

# 宿主 lane 全档 + 与基线逐条比对
$ … -m pytest tests/unit -q --continue-on-collection-errors
49 failed, 1813 passed, 30 skipped, 1 error
$ diff tmp/base-unit.txt tmp/wt-unit3.txt && echo "IDENTICAL to baseline (unit)"
IDENTICAL to baseline (unit)

# 契约 lane（两次；与基线一致，含基线也有的时序 flake）
$ … -m pytest tests/contract -q --continue-on-collection-errors
2 failed, 352 passed, 65 skipped, 2 errors      # 其中 test_teardown_failure_semantics[quotactl] 单独跑 35 passed = 环境 flake
$ … -m pytest tests/contract/test_teardown_failure_semantics.py -q
35 passed in 2.48s
```

## F.4 文件清单（本轮）

**代码/清单/脚本**：`autoscaler/backends/local.py`、`deploy/compose/docker-compose.yml`、
`deploy/compose/docker-compose.autoscale.yml`、`deploy/k8s/worker.yaml`、`deploy/k8s/priv-broker.yaml`、
`deploy/scripts/lib/helpers.sh`、`deploy/scripts/upgrade.sh`、`deploy/scripts/c4-prjquota-window.sh`、
`deploy/stack/.env.example`、`tests/unit/test_c3_agent_manifest.py`、`tests/unit/test_worker_env_key_sets.py`、
`tests/unit/test_worker_manifest_permissions.py`、`tests/unit/test_upgrade_c3_agent_token.py`（新）。

**文档**：`README.md`、`docs/k8s-deployment.md`、`docs/production-deployment-requirements.md`、
`docs/c3-privilege-relocation.md`、`docs/superpowers/plans/2026-09-28-c3-privilege-consolidation.md`、
`deploy/k8s-k0s/README.md`。

## F.5 本轮之后仍留给验收轮 / 后续任务

1. **compose 车道的建箱**（本附录 ⑤）：等 D21 选项 2 的 agent 代码；在此之前**不要把 compose 三栈
   上生产窗口**（k8s 不受影响）。
2. **判据 2/15 的机器臂**（worker 镜像无二进制、`CapEff=0`）、判据 4（面 B `CapEff=0x0b`）、
   判据 1/13/16：单测 pin 已落，真机在验收轮。
3. **pool 若将来需要 per-sandbox uid / route-B**：需要先设计"CP 如何寻址 pooled worker 落在的宿主"
   （D23 的 follow-up，触发条件写进了覆盖范围那段）。
4. **stack 的 uid 池并集**若要变成"派生"而非"两个独立旋钮"：Task 5/6 候选（本附录 ⑥）。

---

## 附录 G：二轮复审的两条余项（2026-09-29，收尾）

复审除这两条外全部通过，且要求"修完即止、不再复审"。**①** `docs/k8s-deployment.md` §2 的逐文件
部署顺序（`namespace → secrets → redis → control-plane → seccomp → broker → worker → autoscaler`）
**从来没有 apply `deploy/k8s/c3-agent.yaml`** —— 照它做全新安装会得到一个已在
`E2B_PRIV_HELPER_TRANSPORT=agent` / `E2B_SLOT_IDENTITY=agent-grant` 上、却没有任何 agent 的 worker
（每个建箱都在 CP 转发那一步具名失败）。现在 agent DaemonSet 插在 broker 与 worker **之间**（新
第 7 步；worker 顺延为 8、autoscaler 为 9），与 `deploy/k8s-k0s/apply.sh:57-67` 的
broker → agent → worker rollout 闸门逐条一致；同一节两处仍读作现状的旧话也一并改掉：Pod Security
前置项（worker 现在**没有** `capabilities` 声明，`no-new-privileges` 的理由改成 agent 面 A 的
file caps 会被 NNP 静默废掉）与第 6 步标题/正文里的"worker 的 socket transport 没有回落路径"
（改成第 6/7 步：broker 是 `socket` 回退的另一半，agent 才是出厂上游），顺带把「镜像与升级」
段的发布清单补上 agent 镜像。**②** pool 的 `E2B_PRIV_HELPERS` 此前只被"两份声明互相一致"
（`test_the_pools_two_declarations_agree`：backend dict ↔ compose JSON）覆盖 —— **两边一起改成
`auto` 仍会绿**；新增
`tests/unit/test_worker_env_key_sets.py::test_the_pool_pins_off_and_does_not_merely_agree_with_itself`，
对两处取值本身各断言 `== "off"`（`autoscaler/backends/local.py` 的 `DockerPoolBackend` 与 pool
compose 的 `E2B_AS_WORKER_ENV` JSON）。RED：把 backend 那一行改成 `"auto"` ⇒
`assert '"auto",  # TEMP-RED' == 'off'`；恢复即绿。验证：相关 8 个文件 154 passed；`tests/unit`
全档 49 failed / 1814 passed（失败集合与同一 HEAD 对照树**逐条相同**；本轮曾有一过性的
`test_mcp_port_pool*`/`test_mcp_gateway` 3 条红，单独跑 37 passed = 端口分配 flake）。
