# Task W4 报告：控制面/清单侧三条 Minor 收口

**状态：DONE**（本地提交，未推送；线上未动）

| 项 | 值 |
|---|---|
| 基线 | `de555f8`（`fix(paths): tell a sandbox tree from infrastructure by shape, not by name`） |
| 提交 | **`94975a8`** `fix(control-plane): follow the envd quota switch and widen volume roots`（本地，未推送，未 amend；10 文件） |
| 工作区 | `/Users/polus/project/ai/sandlock-e2b`（作业期间另有 W1/W2/W3 三个代理在**同一分支**改 `envd_service/**` 与四个测试文件，我的门禁全程用冻结快照对照，见 §3.1；它们随后把这些改动提交为本分支的 `5c73ad1`/`3f2a854`，我的提交落在其上） |
| 我改的文件 | `control_plane/api/sandboxes.py`、`control_plane/app.py`、`control_plane/config.py`、`control_plane/registry/volumes.py`、`deploy/k8s/worker.yaml`、`docs/production-deployment-requirements.md`、`docs/HANDOFF.md`、`docs/task-backlog.md`、`tests/unit/test_controlplane_local_node_quota.py`（新）、`tests/unit/test_controlplane_volume_root_traversal.py`（新） |
| 未碰 | `envd_service/**`、`gateway_common/**`、`deploy/stack/**`、`deploy/docker/**`（其中 `envd_service/**` 的三处改动是并行代理的，见下） |
| 交付物哈希 | 我的补丁 `tmp/w4-my.patch` = `sha256:6c9b615d…`；对照用 WIP 快照 `tmp/w4-wip-snapshot2.patch` = `sha256:e824d443…` |
| 远程推送 | 无（本地提交） |

---

## 0. 判定摘要（先给判定，再给证据）

| # | 条目 | 判定 | 动作 |
|---|---|---|---|
| 1 | 合体节点配额硬编码 `via_agent=False` | **是缺陷**（不是「合体节点必须直连」） | 两处改为跟随 envd 的开关；并在合并进程启动时接上 agent hooks |
| 2 | 控制面卷根缺祖先穿透位 | **是真缺口**（合体节点自建卷根、不走 worker 挂载半边） | 建卷根时复用 A5 的 `_ensure_traversable` 补一遍 |
| 3 | k8s 无 quota-agent 清单 | **是产品口径（降级），不是缺清单** | 口径与后果写进 §2.4.4；**不**新增未验证的特权清单 |

---

## 1. 逐条判定与理由

### 1.1 合体节点配额硬编码 `via_agent=False` ⇒ 判定为缺陷

**事实**（改前 `control_plane/api/sandboxes.py:1223`、`:1488`）：

- `E2B_ENABLE_LOCAL_NODE` 默认 **true**：控制面在自己的进程里建沙箱，**卷配额**
  （`build_volume_mounts` / `cleanup_volume_projects`）由控制面直接调用；
- 同一进程里 envd 的那半（worker 侧 workspace 配额、`QuotaMonitor`）读的是
  `Settings.quota_via_agent`（= `E2B_QUOTA_AGENT_URL` 存在 ⇒ true，否则
  `E2B_QUOTA_VIA_AGENT`，默认 false）；
- 合并镜像（`control_plane.combined_main`）**不跑** `envd_service.app.create_app`，所以它也是
  agent hooks 唯一没被接线的地方。

**为什么「必须直连」不成立**：合并镜像按 `deploy/docker/Dockerfile.envd` 的非 root 形态跑，
既没有 `xfs_quota` 也没有 `CAP_SYS_ADMIN`（§2.4.1 的两条实测）；卷还可能落在 NFS 上，配额在
服务端。一个部署把 `E2B_QUOTA_AGENT_URL` 指向 agent（文档口径里的唯一开关）时，envd 那半走
agent、控制面这半却仍去直连 —— 结果是**静默降级**（或对 NFS 问错对象），而不是「必须直连」的
任何好处。

**改法**：

- `control_plane/config.py::local_node_quota_via_agent()`：直接问
  `envd_service.config._quota_via_agent_from_env`（**就是** `Settings.quota_via_agent` 的
  default_factory，同一份规则，不复制）；envd 不存在（分离控制面镜像）⇒ `False`。
- `control_plane/api/sandboxes.py` 两处 `via_agent=local_node_quota_via_agent()`
  （provision **和** destroy/GC release —— 释放必须问同一侧，否则 release 打给没建过行的那边）。
- `control_plane/app.py::_wire_local_node_quota_agent()`：`E2B_ENABLE_LOCAL_NODE` 且开关开时，
  启动即 `configure_quota_agent_client(...)` 接上 hooks（`app.state.quota_agent_client`，
  lifespan 关闭时 close）。没有这一步，「配好了 agent」的合体节点仍会被报成
  `quota-agent not configured (E2.6)` 并继续静默降级。

**范围控制（不扩大耦合）**：接线**不**构造 `envd_service.config.Settings`（那会让控制面在启动时
校验端口映射/模板镜像等一整批 worker 配置，一个无关的坏值就能把控制面拉不起来）；
只按 envd 自己的三个键读 url/token/timeout，且有一条测试把这三个键/默认值**钉在
`EnvdSettings()` 的取值上**（同名 env 进、同坐标出）。

### 1.2 控制面卷根祖先穿透 ⇒ 判定为真缺口，补到控制面

`VolumeRegistry.create()` 建的是 `<volume root>` 本身（`mkdir` + `chmod 1777`），而沙箱是**以
自己的 uid** 打开这个宿主路径的（E3.2 / route-B 槽位）：DAC 要求 `/` 到卷视图**每一级**都有
`o+x`，只有卷根的 `1777` 不够（A5 的结论，§2.4.2；缺祖先 x 位时**绝对路径也 EACCES**）。

- stack 形态：worker 侧的挂载路径（`envd_service.volumes._ensure_shared_volume_root`）会补；
- 合体节点：卷根由控制面自己建，`_provision_local` 走的是 envd 的**挂载半边**，但建卷发生在
  挂载之前、且卷可以在没有任何挂载时被建出来 ⇒ 建出来那一刻链上是 0700 的形态没人补。

**改法**：`control_plane/registry/volumes.py::_widen_ancestors_for_tenant_uids()` —— 直接复用
`envd_service.volumes._ensure_traversable`（**同一份实现**，不复制第二套规则；envd 不存在 ⇒
no-op），在 `chmod 1777` **之前**跑一次。语义与 worker 侧一致：只**加** x 位、best-effort、
不碰卷内切片（向上走）。

### 1.3 k8s 无 quota-agent 清单 ⇒ 判定为口径（降级），写文档、不新增清单

`deploy/k8s/` 只有 autoscaler/control-plane/gateway/namespace/pvc/redis/worker 七份清单，没有
quota-agent。两条候选路线：

1. **补一份等价清单**：agent 需要 `SYS_ADMIN` + 宿主机上真实的 XFS 设备/挂载点。compose 形态能
   直接给它 `cap_add: SYS_ADMIN` + 同一份宿主卷；k8s 的等价物是「特权 pod + PVC/PV 后端语义 +
   `E2B_QUOTA_AGENT_PATH_MAP` + 节点亲和」，而这套**本仓没有任何环境验证过**（k8s 的共享卷是
   RWX PVC：NFS 形态配额本来就在服务端，由集群外的 agent 提供；CephFS 后端根本没有 XFS project
   quota 这个概念）。发一份没人跑通过的清单，等于把「配额可用」这个错误结论写进部署产物。
2. **写清降级口径**（采纳）：k8s 形态默认**没有** per-sandbox 硬限 + 一条 WARNING，建箱/挂卷/
   命令/快照照常；需要硬限时指向**自备** agent（集群内/外皆可，前提是它对 worker 看到的那份存储
   执行 `xfs_quota -x`）。

**后果（已写进文档，必须让部署方看到）**：① 单沙箱可写满共享卷、影响同卷其他租户；
② 降级形态下**不要**手工 `xfs_quota -x` 建行 —— 对账/释放链（`release_project`、
`reconcile_orphan_projects`）也需要一个配额来源，手工行会变成没人回收的孤儿；③ 验收结论不能写
「有硬限」。落点：`docs/production-deployment-requirements.md` **§2.4.4（新）**，
`deploy/k8s/worker.yaml` 的 A6 注释块指向该节；§2.4.1 的 `SYS_ADMIN` 行与
`docs/HANDOFF.md` 的 ⚡ 块同步改成「W4 起跟随开关」的新口径。

---

## 2. RED → GREEN

两条测试都写在**新文件**里（没有改任何既有测试的断言）：

| 阶段 | 日志 | 结果 |
|---|---|---|
| RED（`de555f8` + WIP 快照，**不打**我的补丁） | `tmp/w4-red.log`（终版测试文件）／`tmp/w4-red2.log` | **12 failed / 3 passed**（`EXIT=1`）；3 条「不打补丁本来就该成立」的用例保持绿（开关关闭时仍直连、卷记录/权限不变等） |
| GREEN（同一快照 + 我的补丁） | `tmp/w4-green2.log`（main 树） | **15 passed**（`EXIT=0`） |
| GREEN（邻域用例） | `tmp/w4-green-main.log` | `15 新 + test_provision_local_uid + test_migration_volume_quota + test_shared_volume_traversal + test_secret_registry + security/test_template_isolation` = **46 passed / 4 skipped** |

RED 名单里逐条过的就是三件事：① 开关读取与两个调用点的 `via_agent`；② 合并进程的接线（含
「开关关/分离控制面都不接线」与「不读无关 worker 配置」）；③ 建卷时把整条祖先链补到可穿透
（含「只向上、不下探切片」与一个把补位步骤中和掉的变异对照，证明断言有牙）。

---

## 3. 门禁（失败集合逐条 diff）

### 3.1 对照方法（重要）

本工作区**不是**干净的：W1/W2/W3 三个代理正在并行改
`envd_service/{agent,runtime/registry,uid_pool,xfs_quota}.py` 与
`tests/{contract,unit}/**` 四个文件，它们的半成品会让 `tests/unit` 出现与 W4 无关的失败
（实测：同一份代码，快照 `e56872c1` 下 26 failed、快照 `e824d443` 下 22 failed）。
所以门禁用**冻结对照**：把并行代理的改动固化成一个 patch
（`tmp/w4-wip-snapshot2.patch`，`sha256:e824d443…`）应用到 `de555f8` 的独立 worktree
（`tmp/w4-baseline-wt`，gitignored），**同一棵树**上先跑基线、再打我的补丁跑对照 ——
两次唯一的差别就是 W4 本身。

另有两条**更贴近字面要求**的对照（`de555f8` 的纯树 vs `de555f8` + 仅 W4 补丁，两个都不含
并行代理的改动）在下两节的最上面；作业末期并行代理把这批改动提交为本分支的
`5c73ad1`/`3f2a854`，所以另外补了一行「当前分支 HEAD」的实测。

### 3.2 `tests/unit`

命令：`tmp/w4-venv/bin/python -m pytest tests/unit -q -rf --tb=line`（macOS，Python 3.12.13）

**直接对 HEAD 的比较（无 WIP 干扰，最干净的一对）**：

| 跑法 | 日志 | 结果 |
|---|---|---|
| 基线 = 纯 `de555f8`（独立 worktree，无并行代理改动） | `tmp/w4-unit-baseline.log` | 22 failed / 866 passed / 11 skipped |
| `de555f8` + 仅 W4 补丁（同 worktree，无并行代理改动） | `tmp/w4-unit-head-plus-w4.log` | 22 failed / **881 passed** / 11 skipped |
| **失败集合逐条 diff** | `tmp/w4-unit-head.ids` vs `tmp/w4-unit-head-plus-w4.ids` | **空**（22 vs 22，`diff` 无输出）；`+15 passed` 全是新增的 W4 用例 |

**带并行代理 WIP 的比较（本仓真实脏树形态）**：

| 跑法 | 日志 | 结果 |
|---|---|---|
| 基线（快照2，无 W4 补丁，我的 RED 用例在场） | `tmp/w4-unit-baseline2.log` | 34 failed / 873 passed / 11 skipped（34 = 22 环境 + 12 我的 RED） |
| W4（快照2 + 补丁） | `tmp/w4-unit-final3.log` | 22 failed / **885 passed** / 11 skipped |
| **失败集合逐条 diff**（剔除我新加的两个文件） | `tmp/w4-unit-baseline2-env.ids` vs `tmp/w4-unit-final3-env.ids` | **空**（22 vs 22，行级 `diff` 无输出；基线的另外 12 条正是我的 RED 用例） |
| 当前分支 HEAD（= 我的 `94975a8`，并行代理的提交已落在它下面） | `tmp/w4-unit-current-head.log` | 22 failed / 888 passed / 10 skipped；失败集合与纯 `de555f8` **逐条相同**（`tmp/w4-unit-current-head.ids` vs `tmp/w4-unit-head.ids`，diff 空） |

（更早一轮冻结快照 `e56872c1` 的对照同样为**空 diff**：`tmp/w4-unit-baseline-wip.log`
26 failed/868 passed vs `tmp/w4-unit-final.log` 26 failed/881 passed。）

**这 22 条是本机 lane 的环境性失败（改前改后逐条相同）**，按文件分布：
`test_priv_helpers.py` 11（macOS 非 root `chown` 到 uid 0 ⇒ EPERM）、
`test_xfs_project_quota_agent.py` 4、`test_xfs_quotactl_backend.py` 2
（`ctypes` 找不到 `libc.so.6`）、`test_volume_quota.py` 2、`test_quota_maintenance.py` 2、
`test_quota_agent_client.py` 1（启动告警列表随 lane 形态变化）。它们与 W4 的三条无关，
在容器 lane（`deploy/scripts/test-prod-shaped.sh`）里是另一套结果。

### 3.3 `tests/contract`（计数）

| 跑法 | 日志 | 结果 |
|---|---|---|
| 基线 = 纯 `de555f8`（无并行代理改动） | `tmp/w4-contract-head.log` | 1 failed / 217 passed / 41 skipped |
| `de555f8` + 仅 W4 补丁（同上，无并行代理改动） | `tmp/w4-contract-head-plus-w4.log` | **218 passed / 41 skipped / 0 failed**（`EXIT=0`） |
| 基线（快照2，无 W4 补丁） | `tmp/w4-contract-baseline2.log` | **218 passed / 41 skipped / 0 failed**（`EXIT=0`） |
| W4（快照2 + 补丁） | `tmp/w4-contract-final3.log` | **218 passed / 41 skipped / 0 failed**（`EXIT=0`） |
| 当前分支 HEAD（含并行代理已落的提交） | `tmp/w4-contract-current-head.log` | 2 failed / 224 passed / 41 skipped —— 两条都在并行代理的 `test_orphan_tree_gc.py` 里，隔离重跑各 1 passed（见下） |
| 同一快照 + 补丁（另有 3 次） | `tmp/w4-contract-final3-r1/r2.log`、`tmp/w4-contract-baseline3/4.log` | 2 次 218/41/0，3 次 217/41/1 |
| 更早冻结快照 `e56872c1` | `tmp/w4-contract-baseline-r2.log` / `tmp/w4-contract-final.log` | 两次同为 31 failed / 187 passed / 41 skipped（那批是并行代理半成品状态，集合一致） |

那几次里的唯一失败是**并行代理正在改的** `tests/contract/test_orphan_tree_gc.py`
（`test_deferred_disk_sweep_backs_off_instead_of_polling_every_heartbeat` /
`test_incomplete_fleet_enumeration_is_retried_until_the_fleet_is_complete` /
`test_tree_teardown_does_not_block_the_event_loop`，随负载抖动）：① **不打** W4 补丁也会出现
（纯 HEAD 也中过一次）；② 隔离重跑 2× 全绿；③ 整个文件单独跑 29 passed ×2。W4 未触碰该文件；
「HEAD 218/41/0、HEAD+W4 218/41/0」这一对里的 W4 一方是 `EXIT=0`。

### 3.4 其它约束

- **没有新增 skip/xfail/`--ignore`**：`git diff` 里 grep `skip|xfail|--ignore` 为空；
- `deploy/k8s/*.yaml` 全部仍能被 PyYAML 解析（只动了 `worker.yaml` 的注释块）；
- `control_plane` 改动 `py_compile` 通过，`import control_plane.app` 正常。

---

## 4. 行为变化的边界（说清楚，免得被当成白拿）

1. **没配 agent 的合体节点行为不变**（开关 false ⇒ `via_agent=False`、不建 client、不接 hooks）；
2. 配了 agent 的合体节点：启动多一个 httpx client（lifespan 关闭时 close），卷配额的
   provision/release 走 agent；agent 不可达/401 ⇒ 仍然是「建箱挂卷照常 + 无硬限 + WARNING」
   （`ProjectQuotaError` 由既有分支吞掉），与 worker 侧同语义；
3. `VolumeRegistry.create()` 现在会 chmod **卷根的祖先链**（只加 x）。分离控制面（k8s/compose
   prod）也会走这一步 —— 这与 worker 侧对同一路径做的事完全相同（A5 已固化），不是新暴露面，
   但升级说明里要点名（A5 的 Minor 已经要求过这一句）；
4. 接线刻意**不**构造 envd `Settings`，所以 `E2B_QUOTA_AGENT_TIMEOUT_S` 之类的坏值仍是
   「agent 形态开的时候 fail fast（worker 本来就 fail fast）」，而 `E2B_PORT_MAPPINGS` 等
   **无关**键的坏值不会影响控制面（有测试钉住）。

---

## 5. 未做 / 遗留

- k8s 的真 agent 清单**没做**（判定为口径）；将来有真实 k8s + XFS 环境时，本节 §2.4.4 应改成
  「已提供」并附清单与实测；
- 合体节点的 workspace（非卷）配额本来就没有实现（控制面本地路径只做卷配额），
  不在本次三条之内，未扩围；
- 三条上线的镜像/滚动不涉及（本轮只动控制面源码与文档；线上仍跑 `de555f8` 之前的 tag）。

---

## 6. 证据日志清单（`tmp/`，均首行 `ENV-HEADER`、末行 `EXIT=`）

| 文件 | 内容 |
|---|---|
| `tmp/w4-unit-baseline.log` / `tmp/w4-unit-head-plus-w4.log` | **对 HEAD 的 unit 对照**（失败集合 diff 空） |
| `tmp/w4-contract-head.log` / `tmp/w4-contract-head-plus-w4.log` | **对 HEAD 的 contract 对照**（HEAD 1 条 WIP flake，W4 后 218/41/0） |
| `tmp/w4-red.log` / `tmp/w4-red2.log` | RED：`de555f8`+快照2，12 failed / 3 passed |
| `tmp/w4-green2.log` / `tmp/w4-green-main.log` | GREEN：15 passed / 46 passed（邻域） |
| `tmp/w4-unit-baseline2.log` / `tmp/w4-unit-final3.log` | unit 基线 vs W4（失败集合 diff 空） |
| `tmp/w4-unit-baseline-wip.log` / `tmp/w4-unit-final.log` | 更早一轮冻结快照的同一对照 |
| `tmp/w4-contract-final3.log`（218/41/0）、`-r2.log`、`tmp/w4-contract-baseline2/3/4.log` | contract 计数与 flake 取证 |
| `tmp/w4-contract-baseline-r2.log` / `tmp/w4-contract-final.log` | 更早一轮冻结快照的 contract 对照 |
| `tmp/w4-my.patch` / `tmp/w4-wip-snapshot2.patch` | 我的补丁 / 对照用 WIP 快照（含 sha256） |
| `tmp/w4-baseline-wt/`（git worktree，gitignored） | 对照用的独立 worktree（`git worktree add --detach tmp/w4-baseline-wt de555f8`）；里面当前是 `de555f8 + W4 补丁`。复核完可用 `git worktree remove --force tmp/w4-baseline-wt` 清掉 |

> 说明： `tmp/w4-*.log` 全部满足「首行 `ENV-HEADER`、末行 `EXIT=`」。其中 13 份的
> `ENV-HEADER` 行是**事后补写**的（当时那条命令把 header 打到了终端），补写行带
> `backfilled=1`、`time=` 取该文件 mtime，其余字段（commit / patch / 快照哈希 / lane）与运行时
> 实际取值一致；正文引用的行号/计数都能在该文件正文里逐条复读。
