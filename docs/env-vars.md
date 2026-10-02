# 环境变量参考

本文件是 worker / 控制面 / agent / quota-agent 的**运行期开关速查**，从旧 `README.md`
的「环境变量」一节原样搬来（2026-10-01）。**权威口径**是各组件的 `config.py` 与
[spec.md](../spec.md) §7.2；每个变量的完整语义、默认值来源、以及"改错了会怎样"，
以对应组件的 `config.py` 注释为最终答案。

> ⚠ 这份表里保留了**已退役**的条目（`E2B_PRIV_HELPERS`、`E2B_PRIV_HELPER_SOCKET`），
> 目的是让旧部署的配置有个解释；代码不再读它们，设了也只是惰性残留。

控制面与 envd 的全部配置见 spec §7.2，默认值与之一致。常用：

| 变量 | 默认 | 说明 |
|------|------|------|
| `E2B_API_KEY` / `E2B_API_KEYS` | `local-key` | 控制面 API Key |
| `E2B_WORKSPACE_BASE` | `tmp/sandboxes` | 沙箱工作目录 |
| `E2B_STATE_BASE` / `E2B_NODE_STATE_BASE` | 前者空（= 工作区根）、后者空 | 平台自己的文件放在哪两根上。`E2B_STATE_BASE`（N27）是**共享**那根：`_runtime/<id>/sandbox.json` 记录、命令日志、`.checkpoints/**` —— 记录是**舰队级 uid 账本的索引**（`envd_service/uid_pool.py::_recorded_uids` 枚举它），checkpoint 会被**别的节点**在 resume 时读，所以它必须共享。`E2B_NODE_STATE_BASE`（N57 / Task 4）是**节点本地**那根：建箱 `prepare` 写的 `.creating` 标记与 `disk-stats` 种子、`.route-b/**` 的 slot 文档、uid 池自己的 `.uid_pool.lock` / `.uid_reservations/`。判据只有一条：**这份数据有没有另一个节点上的读者**。k8s 出厂值 = `/var/lib/e2b-sandboxes/state` + `/var/lib/e2b/state`（hostPath，节点盘，与 `E2B_IMAGE_CACHE_DIR` 同一块盘；`c3-agent` 的 `workspace-root-init` 建它并交给 65534）。**两个都不设**时是 Task 4 之前的形状逐字节不变（compose、测试、`local://` 都不设）。为什么值得分：共享卷是 NFS，一次元数据往返 ~13 ms（Task 1 §1.1），`prepare` 原先要为它写的每个小件各付一次（**已部署形状**=标记 + `disk-stats` 种子两笔；`uid_pool.acquire` 那条**回落**路径——payload 不带 `hostUID`——还会多付 `.uid_pool.lock` 与 `.uid_reservations/` 两笔，本机探针量到的 14 就是这条形状）；本地量法见 `deploy/scripts/acceptance/node_state_split_local_probe.py`（writes 14 → 0），集群量法见 `prepare_phase_cost_probe.py`。 |
| `E2B_BASE_IMAGE` | 未配置 | `base` 模板基础镜像；配置后启用镜像 rootfs |
| `E2B_TEMPLATE_IMAGES` | `{}` | 模板 ID → 基础镜像 JSON 映射 |
| `E2B_EXECUTOR` | `auto` | `auto`/`local`/`sandlock`；`auto` 只对**顶层包不存在**（`ModuleNotFoundError: No module named 'sandlock'`）回落 `local`，包在但坏（版本不匹配/缺符号/半升级树少 `sandlock.*` 子模块）一律 fail closed（B1 fix round 2/3） |
| `E2B_PRIV_HELPERS` | **已移除（N52，2026-09-30）** | Track F 的本地 broker 开关：`auto` 让 uid 65534 的 worker 用镜像里 `/var/lib/e2b-priv/` 的两个 file-capability 二进制（`e2b-slot-spawn`、`e2b-maint`）完成"以池内 uid 起 route-B 槽位"和"把工作区/slice 交给沙箱 uid"两步，`off` 则保持进程内 E5.1 形态。**这个形态整体退役了**：二进制、`exec` transport、C1 的 `socket` broker 与这个开关都不再存在，worker 的特权文件步骤全部由每节点 agent 以 `{sandbox_id, op}` 代做（见 `E2B_PRIV_HELPER_TRANSPORT`）。旧的仓库里若还设着这个变量，它现在是**惰性**的（代码不再读它）；真需要那个形态时从 git 取回当时的清单 + 镜像。`e2b-maint` 只存在于 **agent 镜像**（`deploy/docker/Dockerfile.agent` 的 `/var/lib/e2b-priv/`），它的路径白名单是五根：`E2B_WORKSPACE_BASE`、`E2B_NODE_STATE_BASE`（N57 / Task 4）、`E2B_STATE_BASE`（N27）、`E2B_SHARED_VOLUME_ROOT`、以及仅当显式非空时的 `E2B_IMAGE_CACHE_DIR`（沙箱 secret 落在 `<image_cache_dir>/secrets/<id>/`）。容器 **BND**：worker 是**空集**，**绝不要加 no-new-privileges**（NNP=1 会让 agent 面 A 的 `as_uid` 的 file caps 静默失效）。 |
| `E2B_PRIV_HELPER_TRANSPORT` | `auto`（出厂 manifest 显式 `agent`） | 文件操作走哪条路。`agent`（**C3 Task 4 片 B 起的出厂形态**）：worker 一个特权二进制都不 exec，每个文件步骤与槽位身份都以 `{sandbox_id, op}` 交给控制面，由控制面指令本节点的 agent 执行（`E2B_C3_AGENT_MAINT_URL`/`_MAINT_PORT`，见下）。`auto`：与 `agent` 等价（agent 是唯一剩下的特权形态）。**取值是闭列表**：`auto` / `agent`；`exec`（worker 自己 exec 镜像里的 file-capability 二进制）与 `socket`（C1 的每节点 root broker）**都已退役**（2026-09-30，N52；`socket` 更早随 C3 Task 7 的 DaemonSet 一起），现在被这个开关**具名拒绝**，不是静默回落。没有 agent 又不是 root 的 worker 保留进程内 E5.1 形态（无 per-sandbox host uid、无 route-B）并在启动时打一条 WARNING。运维含义：出厂形态是 `agent`；遗留的 `E2B_PRIV_HELPERS` 与 `E2B_PRIV_HELPER_SOCKET` 都是惰性残留。 |
| `E2B_PRIV_HELPER_SOCKET` | **已移除（C3 Task 7）** | C1 的 `socket` transport 连的那个 unix socket（`/run/e2b-broker/broker.sock`，hostPath `/run/e2b-broker`）。它随 `e2b-priv-broker` DaemonSet 一起退役：worker 与节点组件之间不再有这条通道，k8s 清单里也不再有这个 hostPath、这个 env 和 `wait-for-broker` 闸门。**留这一行只为让旧配置有个解释**：现在把 `E2B_PRIV_HELPER_TRANSPORT` 设成 `socket` 会在启动时被点名拒绝。节点上若还留着 `/run/e2b-broker/` 空目录，那是历史残留（hostPath 是 `DirectoryOrCreate`），可随手删。 |
| `E2B_C3_AGENT_URL` | 空（k8s 走 pod API；compose 默认 `http://c3-agent:49985`） | **C3 Task 3** 的 CP→agent 面 A 端点：`grant-slot`（写 `uid_map`）。k8s 不设（按 worker pod → `spec.nodeName` → 本节点 agent pod 的 label 查），compose 指向 agent 服务名，且该 URL 的**主机名就是 agent 自己的身份**（D12）。 |
| `E2B_C3_AGENT_MAINT_URL` / `_MAINT_PORT` | 空 / `49986` | **C3 Task 4 片 B（裁定 D22）** 的**面 B 端点**：`chown`/`rm`/`walk`。两个面必须是两个进程（面 A 要 uid 65534 才写得进 `uid_map`；面 B 要 uid 0 才能在 NFS 上 `chown`，且只有它挂了四个根），因此是两个监听——k8s 用同一个 pod IP 的第二个端口（`_MAINT_PORT`，面 B 绑 49986），compose 用第二个服务名（`_MAINT_URL` → `c3-agent-maint:49986`）。compose 未设该变量时**每个文件 op 具名 503**（不回落面 A：那会变成"每个 chown 都 EPERM"的假权限 bug）。 |
| `E2B_C3_AGENT_TOKEN` | 空（fail closed） | CP↔agent 的唯一凭据（`X-Internal-Key`）。**只**出现在控制面与 agent 两面；worker 清单/镜像里一个字都没有（有就等于把 `worker ↔ agent` 这条不存在的通道造出来）。轮换见 `docs/k8s-deployment.md` §4.5 表 3。 |
| `E2B_SLOT_IDENTITY` | `spawn` | route-B 槽位身份的来源。`spawn`=worker 镜像里的 file-capability `e2b-slot-spawn`（**Task 4 片 B 后该二进制已移出镜像，这条只剩回退镜像时可用**）；`agent-grant`=worker fork+unshare 后把 `{sandbox_id, pid}` 报给 CP，CP 校验后带 uid 指令本节点 agent 写 map（出厂 k8s 与三个 C3 compose 栈都是这个值）。 |
| `E2B_ROUTE_B_TMP_ROOT` | `/tmp/sandlock-route-b` | route-B 槽位的 `policy.json`/`program.json` 所在根。**worker 与 CP 两侧都要设**且逐字一致：worker 写文档，CP 推导"要交给哪个 uid"的那一条路径（`scope-slot-document`）；CP 未设时该 op 具名 503、槽位起不来。出厂值：k8s `/var/lib/e2b/state/.route-b`（N57 / Task 4 把它挪到**节点本地** base —— 写者与读者都只在本节点；CP 那侧只推路径，所以它那里的 `E2B_NODE_STATE_BASE` 是给 `_require_in_roots` 用的根，不挂卷），compose `/var/lib/e2b-sandboxes/.route-b`。 |
| `E2B_INTERNAL_API_KEY` / `E2B_INTERNAL_API_KEYS` | `internal-key` / 空 | worker、网关与 C3 agent 的 `X-Internal-Key`（**舰队共享**；`_KEYS` 是轮换窗口，见 `deploy/k8s-k0s/README.md`）。⚠️ 它证明"你是这类组件之一"，**不**证明"你是 worker-1"——节点的身份由下面两项绑 |
| `E2B_NODE_ADDRESS_MODE` | `auto` | **C3 Task 2（N49）**：node-scoped 内部请求的**期望地址 / 源 IP** 从哪来。`k8s`=按 `node_id`（= StatefulSet pod 名）查 pod API（要控制面 SA 有 `get pods`，出厂 k8s 清单已给）；`hostname`=把 `node_id` 当 compose 服务名解析；`auto`=挂了 ServiceAccount 走 k8s、否则 hostname（**开发/合体默认**）。解析不到即 **503 点名**（fail closed），注册的 `address` **永不取**请求体；出厂 k8s/compose 清单都**显式**设它，不落 `auto`。配套 `E2B_NODE_ADDRESS_PORT`（49983）、`E2B_NODE_ADDRESS_NAMESPACE`（sandlock） |
| `E2B_INTERNAL_NODE_KEYS` | 空 | **C3 Task 2** 的**近期**加固（机制已实现、出厂未接线）：JSON `{"<key>": "<node_id>"}`。列进来的 key 是**节点绑定**凭据——自称与它不一致即 403。未列（含上面的共享 key）只能靠"自称解析到的地址 + 源 IP"背书。没接线这一点写在 `docs/open-issues.md` N49 |
| `E2B_MAX_TOTAL_*` | 见 spec | 宿主总资源上限，`0` 表示关闭该维度 |

## 相关文档

- 生产形态的开关清单与"为什么必须这样设"：[production-deployment-requirements.md](production-deployment-requirements.md)
- k8s 清单逐项说明（含 env 与 capability 的钉子）：[k8s-deployment.md](k8s-deployment.md)
- 目标集群的连接方式与现状：[deploy-clusters.md](deploy-clusters.md)
