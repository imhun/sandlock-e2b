# N27 Task 7 报告：集群验收（形态无关性 + 不回归）

**Status: 完成。** commit `bbd3313`（pathspec 限定：`docs/deploy-clusters.md` +
`tmp/k0s/probe_state_base_visibility.py`（`tmp/` 被 gitignore ⇒ `git add -f`）+
`tmp/k0s/n27-t7-lane.sh`；提交前 `git diff --cached --name-only` 只有这三个，
`.superpowers/sdd/progress.md` 是并行 agent 的改动，未 stage）。

**验收结论有一条边界需要人拍板**（§3 / §7）：形态无关性在**有根**的形态下成立
（生产 image-rootfs、pure+合成根+真根），在**无根的 pure identity 形态**下只成立一半 ——
`stat` 四次全 `EACCES`（读不到 ✔），但祖先链在 `<export>` 一层能列出 `state` 与 `_secrets` 的**名字**。

---

## 0. 结论速览

| 项 | 结果 |
|---|---|
| 集群探针（生产 image-rootfs，真 `Sandbox`） | `stat=PASS chain=PASS`、`CHECKER-EXIT 0`、`ENOENT` ×4 |
| 两条冒烟（`multinode_smoke.py` / `deployment_smoke.py`） | 都 `OK`，退出码 0 |
| pure + 合成根 + 真根（lane） | `stat=PASS chain=PASS`、exit 0（`ENOENT` ×4） |
| pure + identity（lane，N15 无根默认） | `stat=PASS chain=FAIL`、exit 1（`EACCES` ×4；`<export>` 列出 `["_secrets","state"]`） |
| 反例：迁移前布局（lane legacy） | `stat=FAIL chain=FAIL`、exit 1（判据可证伪） |
| 卷上现状 | `state/` + `workspaces/` 就位，顶层无 `_runtime`/`.route-b`，journal `0600`，sts `2/2`，无 Job 残留 |

## 1. 范围、既有产物与不可重做的部分

* 简报：`.superpowers/sdd/n27-task-7-brief.md`（74 行，5 步）。前置事实：N27 Task 1–6 已落地，
  **迁移已于 2026-09-26 执行并上线**（`done=12 unknown=0`、`same_inode=yes`、journal 0600；
  `.superpowers/sdd/progress.md` 的「N27 迁移已执行 + 已上线」段），本任务**只读 + 冒烟**。
* 前一个 agent 留下的探针 `tmp/k0s/probe_state_base_visibility.py`（21 KB）**只读不改**，直接使用；
  它的判据我逐行读过：① 四个平台状态路径的 `stat` 必须全失败且 errno ∈ {`ENOENT`,`EACCES`}；
  ② `cwd`→`/` 每一层要么列不出来、要么不含 `state`/`_runtime`/`.route-b`/`_secrets`；
  另有**正对照**（自写 canary 必须可 `stat`、workspace 那层必须列出它）⇒ 判据可证伪、且"到处都拒"会报 VACUOUS(2)。
* **Step 1 的"迁移前对照"不可能重做**（窗口已经过去）。替代做法用的是探针自带的
  `lane --layout legacy`：它按迁移前布局合成夹具（平台命名空间就在树根上），要求用真实沙箱拿到
  `exit 1`。这次实测它确实报 `1`（§2.4），所以"判据能失败"这一条是有证据的，不是口头声明。

## 2. 跑过什么（逐条，含退出码）

连接：`deploy/scripts/open-cluster-tunnel.sh` ⇒ 自检 `✓ 2 节点 / arm64 / 含 +k0s`、`sandlock` 8 个 pod。
凭据一律 `kubectl -n sandlock get secret e2b-secrets` 就地取出、**任何输出都不打印**。

### 2.1 集群探针（`probe_state_base_visibility.py cluster`）

Run: `tmp/testenv/bin/python tmp/k0s/probe_state_base_visibility.py cluster`（`KUBECONFIG` 指向本集群）
Log: `tmp/k0s/n27-t7-cluster-probe.log`（exit=0）

```
CLUSTER deployment-declared shape: {'E2B_BASE_IMAGE': '…python-mcp:3.14@sha256:3675662d…', 'E2B_REAL_ROOT': '1',
                                    'E2B_PURE_ROOTFS': '<unset>', 'E2B_STATE_BASE': '/var/lib/e2b-sandboxes/state'}
CHECKER-PWD /home/user
CHECKER-CONTROL stat-canary OK size=4 / listdir-canary OK entries=3
CHECKER-STAT /var/lib/e2b-sandboxes/state                        DENIED errno=ENOENT
CHECKER-STAT /var/lib/e2b-sandboxes/state/_runtime               DENIED errno=ENOENT
CHECKER-STAT /var/lib/e2b-sandboxes/state/.route-b               DENIED errno=ENOENT
CHECKER-STAT /var/lib/e2b-sandboxes/state/_runtime/.checkpoints  DENIED errno=ENOENT
CHECKER-LAYER /home/user LISTED [".n27-probe-canary", "n27-probe", "workspace"]  CANARY-PRESENT
CHECKER-LAYER /home     LISTED ["user"]
CHECKER-LAYER /         LISTED [".complete","bin","boot","dev","etc","home","lib","media","mnt","opt","proc","root","run","sbin","srv","sys","tmp","usr","var","workspace"]
CHECKER-CHAIN layers=3 listed=3 reached-root=yes
CHECKER-VERDICT stat=PASS chain=PASS
CHECKER-EXIT 0
```

⇒ 生产形态：平台状态**不在祖先链上**（链是沙箱自己的 `/home/user → /home → /`）也**读不到**（`ENOENT` ×4）。

### 2.2 卷上现状（只读复核）

Logs: `n27-t7-cluster-state.log`、`n27-t7-cluster-volume.log`、`n27-t7-cluster-layout.log`

* 版本：`deploy/stack/.version` = `0.1.0-597-g3701a53-20260926-163057`，`autoscaler` / `control-plane` /
  `e2b-worker` 三个工作负载的镜像都是这一版；`rollout status` = `complete`、`readyReplicas/replicas = 2/2`。
* 卷：`/var/lib/e2b-sandboxes`（1777）= `_builds _images _secrets _snapshots _templates _volumes state workspaces`
  —— **顶层没有 `_runtime` / `.route-b`**（同挂载 `rename(2)` ⇒ 旧路径不存在）；
  `state/`（1777）= `.route-b .state-base-migration.journal .uid_pool.lock _runtime`；
  `workspaces/`（1777）= `_migrate` + 7 棵 `<id>` 树；`state/_runtime/.checkpoints` 当前为空（没有在途 checkpoint）。
* `state/.state-base-migration.journal` = **`600 585`**（迁移脚本承诺的硬项）；无 migrate Job / ConfigMap 残留。
* 两个 control-plane 副本启动都打印 `workspace base = /var/lib/e2b-sandboxes/workspaces` +
  `platform state base = /var/lib/e2b-sandboxes/state`。
* 简报 Step 3 期望"worker 启动日志里能看到 `platform state base = …`"：**实测这行在控制面**
  （`control_plane/app.py::startup` 的 startup pair），worker 日志里 `rg 'state base'` 零命中（见 §5.2）。
  简报 Step 3 的另一半（没有 "cannot resolve path" 类 broker 拒绝）实测成立：两台 worker 的**全量**日志里
  都是 0 命中（`e2b-worker-0` 1084 行 / `e2b-worker-1` 754 行）。

### 2.3 两条冒烟（不回归）

Logs: `tmp/k0s/n27-t7-smoke-multinode.log`（exit 0）、`tmp/k0s/n27-t7-smoke-deployment.log`（exit 0）

```
NODE DISTRIBUTION: {'http://10.244.140.50:49983': 2, 'http://10.244.192.230:49983': 2}
ALL sandboxes: commands + files + health through gateway OK
stdin through gateway OK
after kill reservations: [('e2b-worker-0', 0), ('e2b-worker-1', 0)]
MULTI-NODE SMOKE OK

OK: commands + files through gateway
OK: migrated e2b-worker-0 -> e2b-worker-1, files kept
OK: network config echo + atomic update
OK: volume mounted remotely + sibling volume isolated
OK: template built -> registry push -> worker pull -> image rootfs
OK: MCP gateway inside sandbox + streamable HTTP through proxy
DEPLOYMENT SMOKE OK
```

（`deployment_smoke.py` 覆盖了 template build → 推 ACR → worker 拉 → image rootfs 这条链，
等于同时验证"树根下沉 + 新 state base"没有影响模板/镜像路径。）

### 2.4 lane 形态对照（探针 `lane` 模式）

跑法：`sh tmp/k0s/n27-t7-lane.sh python3 -u tmp/k0s/probe_state_base_visibility.py lane --shape <形状> --layout <布局>`
（新增的 runner `tmp/k0s/n27-t7-lane.sh`：`e2b-sandlock-test:latest`、caps/seccomp 档同
`deploy/scripts/arm-lane/x86-security.sh`，两处刻意的差别 —— `E2B_BASE_IMAGE` **显式传空**
（`${VAR:-default}` 会把它换成默认值）、仓库挂在 **`/src`** 而不是 `/workspace`
（identity 形态拒 `/workspace`，挂那儿连夹具都进不去））。

| 形状 / 布局 | 日志 | 关键原始行 | 退出 |
|---|---|---|---|
| `identity` / `n27` | `n27-t7-lane-identity-n27.log` | `CHECKER-STAT …/state DENIED errno=EACCES` ×4；`CHECKER-LAYER …/workspaces LISTED ["_migrate","sbx_probe"]`；`CHECKER-LAYER …/identity-n27 LISTED […,"state","workspaces"] LEAK ["_secrets","state"]`；`VERDICT stat=PASS chain=FAIL` | **1** |
| `identity` / `legacy`（反例） | `n27-t7-lane-identity-legacy.log` | `CHECKER-STAT …/identity-legacy OK mode=0o40755`（状态目录本身可 stat）、`…/_runtime DENIED errno=EACCES`；`LEAK [".route-b","_runtime","_secrets"]`；`VERDICT stat=FAIL chain=FAIL` | **1** |
| `synth-realroot` / `n27` | `n27-t7-lane-synth-realroot-n27.log` | `ENOENT` ×4；链 3 层（合成根）；`VERDICT stat=PASS chain=PASS` | **0** |
| `synth-emulated` / `n27` | `n27-t7-lane-synth-emulated-n27.log` | 起不来：`LANE cwd-retry=/home/user (host path refused: SlotRefusal: instance exec failed: … instance is closed …)`；回溯到 `envd_service/route_b.py::exec` | **1** |

机制旁证 `n27-t7-lane-mount.log`（用前一个 agent 的 `probe_n27_repo_mount.py` 复跑）：同一条 identity 形态里
workspace 的**父目录**可列（`ls -a /tmp/n27-mount` → `_runtime tmp-tree`，`rc=0`），而**不是祖先**的
`/workspace/...` 一律 `Permission denied` ⇒ identity 形态的可见面是"workspace 的祖先链"，不是"随便一个目录"。

> 注：前一个 agent 在同一批形状上留下过 `tmp/k0s/n27-lane-*.log`（18:08–18:17）；我这次的四个结果与它逐条一致
> （identity/n27 `chain=FAIL`、identity/legacy `1`、synth-realroot `0`、synth-emulated 起不来），
> 所以这不是一次性现象。

## 3. 形态无关性：结论与边界（本任务的核心）

**成立的部分**：`stat` 那一半是形态无关的 —— 生产 image-rootfs 给 `ENOENT` ×4，pure 形态给中介的
策略拒绝 `EACCES` ×4，两者都"读不到"。这条正是简报 Step 1 要防的坑（只认 ENOENT 的探针只在一种形态上通过）。

**不成立的部分**：祖先链那一半在 **pure 的 identity 形态**（`E2B_PURE_ROOTFS=off`，N15 的兜底：
宿主根 + identity 翻译，`route_b_active=True has_root=False chroot=/`）不成立：

* 它的 `/home/user` 就是宿主的 `<export>/workspaces/<id>`，所以 `..` 是 `<export>/workspaces`（干净，只有树与 `_migrate`），
  但 `../..` 是 **`<export>`** —— 那里列出 `state`（N27 新家）与 `_secrets`（N27 明确留在原地的平台命名空间）。
  实测 `<export>` 是 `1777`，沙箱列得出来是必然的。
* 这不是 N27 引入的坏化：迁移前同一条形态在 `..`（距离 1）就列出 `_runtime` / `.route-b` / `_secrets`
  （反例那一栏，且 `stat <base>` 本身就成功）。N27 把它推远了一层，但没有消掉 —— 因为那条形态**没有根**，
  "祖先链"就是宿主路径链，中介为了让沙箱走到自己的 workspace 必须放行它的祖先目录。
* 这正是 `docs/superpowers/plans/2026-09-26-pure-shape-synthetic-rootfs.md`（N16 合成根，
  Task 10 明写"`stat <base>/_runtime` 给 EACCES 是 N27 在 pure 上的残留…合成根下消失"）要消掉的那条；
  本次实测给的补充是：**要被消掉的不止 `stat` 那一半，祖先链那一半也一样**（合成根形态实测 `PASS`）。

⇒ 文档口径要收窄：`docs/pure-shape-decision.md` §2、`docs/open-issues.md` N27 行、`docs/task-backlog.md` N27 行
里"pure 形态也不在祖先链上 / 形态漂移已消除"这句只在**有根形态**下成立。Task 8 的自我提醒（"`pure 给中介拒绝`
这句我没有实跑验证"）应验了一半：errno（`EACCES`）对，祖先链那句不对。我**没有改这三处**
（超出本简报的 Files，且涉及别的任务的结论口径），在 `docs/deploy-clusters.md` §11.2 用准确措辞写明并指向本报告，
由控制器决定是否单独收口（见 §7）。

## 4. 没有跑的东西（如需要可补）

* `deploy/scripts/multiworker_interference.py`（简报 Step 4 列了，用户本次只点名两条冒烟）。
* 简报 Step 4 的 pytest 子集（`tests/contract/test_pause_resume_metrics_logs.py`、
  `tests/unit/test_checkpoint_store.py`、`tests/unit/test_dir_ledger.py`、`tests/unit/test_sandbox_disk_enforcement.py`）。
* 迁移回退的**线上**演练（只有 Task 6 的离线彩排）——不进任何"已演练"字样。
* 简报 Step 4 里"回退窗口检查：`<export>/_runtime` 旧目录仍在"这条**已作废**（纯 rename ⇒ 旧路径不存在，
  与 Task 8 的更正一致），我按事实写成"回退窗口 = journal(0600) + `--rollback --apply` 反向改名"。

## 5. 与简报/计划的差异（逐条）

1. **Step 1 的"迁移前"对照不可重做** ⇒ 用 `lane --layout legacy` 当反例，要求它必须报 1；实测报 1（§2.4）。
2. **Step 3 的启动日志位置**：`platform state base = …` 在**控制面**（两个副本都有），不在 worker 日志里
   （worker 那部分实测零命中）。简报写的是 worker 日志。
3. **Step 4 的"旧 `_runtime` 还在"** 已作废（同上）。
4. **Step 4 的两条附加项没跑**（§4），只跑了用户点名的两条冒烟。
5. 探针的 `LANE_SHAPES["synth-emulated"]`（`E2B_PURE_ROOTFS=synth` + `E2B_REAL_ROOT=0`）**已不是可服务形态**：
  N16 守卫要求 `synth` 必须配真根（`envd_service/config.py::check_pure_rootfs_pairing`），实跑是
  `instance is closed`（合成骨架里没有 `/bin/sh`）。可服务的形状是三个：`cluster`(image-rootfs)、
  `identity`、`synth-realroot`。**探针文件我没改**（简报要求"先读它、别重写"），把这一点记在这里。
6. 计划里 Task 7 的验收条目写"探针在两种形态下都报'平台状态不在祖先链上、读不到'" —— **前半句对 identity 不成立**（§3）。
7. 探针 `cluster` 模式需要 `E2B_API_KEY`（简报的 Run 行只给了两个 URL 环境变量），凭据从 Secret 取、不打印。

## 6. 证据文件（全部在 `tmp/`，gitignored，未入库；探针与 lane runner 已入 `bbd3313`）

| 文件 | 内容 | 退出码 |
|---|---|---|
| `tmp/k0s/n27-t7-cluster-probe.log` | 集群探针（生产 image-rootfs，真 Sandbox） | 0 |
| `tmp/k0s/n27-t7-cluster-state.log` | 版本/镜像/pod 清单 | — |
| `tmp/k0s/n27-t7-cluster-volume.log` | 卷上布局、顶层无 `_runtime`、`rg 'cannot resolve path'` 0 命中 | — |
| `tmp/k0s/n27-t7-cluster-layout.log` | rollout、sts 2/2、journal `600 585`、state/workspaces 列表、CP startup pair | — |
| `tmp/k0s/n27-t7-smoke-multinode.log` | 多节点冒烟 | 0 |
| `tmp/k0s/n27-t7-smoke-deployment.log` | 部署冒烟（含 template→镜像→rootfs） | 0 |
| `tmp/k0s/n27-t7-lane-identity-n27.log` | pure identity + N27 布局（`chain=FAIL`，LEAK `state`/`_secrets`） | 1 |
| `tmp/k0s/n27-t7-lane-identity-legacy.log` | 反例（迁移前布局，`stat=FAIL chain=FAIL`） | 1 |
| `tmp/k0s/n27-t7-lane-synth-realroot-n27.log` | pure 合成根 + 真根（全 PASS） | 0 |
| `tmp/k0s/n27-t7-lane-synth-emulated-n27.log` | 不可服务形态的实录 | 1 |
| `tmp/k0s/n27-t7-lane-mount.log` | 祖先可列 / 非祖先被拒 的机制旁证 | 0 |
| `tmp/k0s/probe_state_base_visibility.py`、`tmp/k0s/n27-t7-lane.sh` | 探针 + lane runner（已入库，`git add -f`） | — |

## 7. 需要人拍板的一条

**pure identity 形态的名字残差怎么处置**：(a) 只在文档里把口径限定成"有根形态"并记这条残差（本轮的做法）；
(b) 让 pure 默认走合成根（`E2B_PURE_ROOTFS=synth` + `E2B_REAL_ROOT=1`），或对"无根建箱"显式拒绝
（fail closed，与 SL-1 / route-B 的既定风格一致）—— 这会改部署默认值/清单，超出本简报"只读 + 冒烟"的授权，
我没有动。建议由控制器在 N16 收尾时一并决定。

## 8. 自审与局限

* 我的 lane 跑在 **amd64** 容器（`e2b-sandlock-test:latest`，caps/seccomp 同
  `deploy/scripts/arm-lane/x86-security.sh`）；集群那一栏是 arm64 的生产形态。identity 形态的可见面由同一份
  中介代码/策略决定，与架构无关，但"只在 x86 lane 上量过"这件事记在这里。
* lane 的 `identity` 一栏走 conftest 的 exec 入口，与 worker 的 `_view_cwd` 同一条 pure 分支
  （cwd 传宿主 workspace 路径），所以链是宿主链 —— 这正是生产 pure 形态会走的那条路。
* 我没有改任何代码、清单、默认值，也没碰集群的写操作（未 scale / 未 apply / 未迁移）。
* `git status` 里剩下的 `.superpowers/sdd/progress.md` 是并行 agent 的改动，我没有 stage、没有改写。
