# Task 6 报告：自愈改走 agent（**孤儿回收 = agent 巡检 → CP 决策 → agent 执行**）

- 工作树：`/Users/polus/project/ai/sandlock-e2b/tmp/wt-c3`（分支 `feat/c3-consolidation`），BASE：`c31aa09`（Task 5 评审）
- 依据：task-6-brief、plan §5.2 / §11.1 第 5 项 / §14.3–§14.5 / §11.2.1、Task 4a/4b/5 报告，以及 **T6 裁定**（1–4 通过 + 两条加注）
- **未 push、未 merge、未部署、未碰集群**（真机由 controller 协调）

---

## 1. 一句话形状

```
agent（面 B，唯一挂共享工作区的容器）        CP（唯一有权威记录的人）                agent（面 B 执行）
  周期扫 <workspaces>/*  ──{"sandboxes":[id]}──▶  三档门：记录共享？全部可读？条数对得上？
  （"眼睛"：只看、只报 id）                          └─ 否 ⇒ 整轮具名推迟（什么都不删）
                                                    └─ 是 ⇒ 记录认领 = protected（原 protected_elsewhere）
                                                            无记录认领 = 孤儿
                                          ◀──{"rm", path=<CP 推导>}──  同节点的 agent 执行
  （Agent 不决定、不自己动手、无授权表）
```

- `worker` **不在这条链上**（判据：worker 崩了且不重启，盘上照样收敛）——这也正是 (e) 相对 (b1) 的增益。
- worker 自己的 `_startup_uid_reconcile` / `_startup_reconcile_once` 在 agent 形状下**仍然不跑**
  （Task 4 的具名告警逐字保留，本任务没有把它打开）；非 agent 形状里那条既有清扫**一字未动**。

---

## 2. 交付物（按文件）

### 2.1 agent（`deploy/c3_agent/`）

| 文件 | 内容 |
|---|---|
| `scan.py`（新） | `InventoryScanner`：`scan_once()`（`os.scandir(<workspace base>)` + 共享谓词 `is_sandbox_workspace_dir` + `is_reserved_platform_namespace` 过滤）、`round()`（扫 + 报 + 记）、`run(stop)`（周期循环）；`ScanSchedule`（首扫 30s / 周期 120s / 退避封顶 600s）；`HttpInventoryReporter`（POST `{"sandboxes":[…]}` + `X-Internal-Key`）；`scanner_for(settings)` 的三条惰性理由（没开 / 没 CP URL / 周期非正）**逐条具名** |
| `config.py` | `control_plane_url`（复用 `E2B_CONTROL_PLANE_URL`）、`scan_enabled`（`E2B_C3_AGENT_SCAN`）、`scan_initial_delay_s`(30)、`scan_interval_s`(120)、`scan_backoff_max_s`(600)、`report_timeout_s`(10) |
| `app.py` | lifespan 起/停巡检任务（`create_app(..., inventory=…)` 可注入；`None` = 本容器不扫）；`FileOpBody.worker` 变成**可选**（见 §3.3） |
| `fileops.py` | `maint_env` 只在**给了 worker 身份**时写 `E2B_BROKER_WORKER_UID/GID`；`chown` 缺身份 = `FileOpShapeRefusal`（400 具名）；`rm`/`walk` 不再要求它 |

### 2.2 控制面（`control_plane/`）

| 文件 | 内容 |
|---|---|
| `self_heal.py`（新） | 决策面：三档门 → `protected` / `orphans` → 逐条推导路径 → `client.rm(target=…)` → `removed` / `failed`；`SweepOutcome.as_response()` 是 agent 那行日志读的 wire 形状；推导走 `asyncio.to_thread`（与 file-op 端点同一条纪律：别在事件循环里碰共享存储） |
| `fleet_view.py`（新） | `active_sandbox_count(state)`：`/internal/fleet/metrics` 的 `activeSandboxes` 的**唯一定义**，供三档门第 3 条使用（有一条 pin 断言两边一致） |
| `registry/manager.py` | `FleetIdSnapshot(ids, unreadable, shared)` + `SandboxRegistry.fleet_id_snapshot()`；`_store_sandbox_ids()` **数出** 旧 `_iter_stored_records` 静默跳过的条目（墓碑 = "已删"，不算 unreadable）；构造器新增 `record_store=` 注入缝（同一形状的共享记录面） |
| `file_ops.py` | op 表新增 `remove-orphan-workspace`（verb `rm`，`callers={"self-heal"}`）与 `callers` 维度；`spec_for(op, caller=…)` 对越权调用**具名 400**（unknown op 的文案改成"这个 caller 的词汇表"，worker 面文案逐字不变）；`derive()` 里这条 op 不需要 `host_uid`，且**再拒**平台保留名（两道） |
| `c3_agent_client.py` | `AgentTarget.source_ips`（源 IP 第二因子的期望值）；`resolve_host()`（**主机键**：k8s = agent pod 的 label + `fieldSelector spec.nodeName=`，compose = 配置的宿主名 → DNS）；`C3AgentClient.resolve_agent()`（三处具名 503）；`rm(..., worker_uid/gid 可选, target=…)`。**worker 键的 `resolve()` 一字未动** |
| `api/internal.py` | 新端点 `POST /internal/nodes/{node_id}/agent/inventory`；`_require_agent_identity()`（凭据 → 主机声明 → 源 IP）；body **只允许** `{"sandboxes":[…]}`，多一个键具名 400，非法/保留 id 具名 400 |
| `auth.py` | `verify_agent_key()`：`E2B_C3_AGENT_TOKEN` **只**在 agent 自己的面上被接受；**没有**把它加进 `all_internal_api_keys`（否则一把 agent token = 舰队内部凭据） |

### 2.3 部署面

| 文件 | 内容 |
|---|---|
| `deploy/k8s/c3-agent.yaml` | 面 B 加 `E2B_C3_AGENT_SCAN=on` + `E2B_CONTROL_PLANE_URL=http://control-plane:3000` + 三个时间旋钮（注释写明"agent 是眼睛"与 2–3 分钟的口径）；NetworkPolicy `policyTypes: [Ingress, Egress]`，**出口只到 `app: control-plane` 的 3000**；面 A 不扫（它没挂工作区） |
| `deploy/compose/docker-compose.prod.yml`、`deploy/stack/docker-compose.prod.yml` | 这两个栈（**有 Redis**）的**面 B** 加同一组 env（`E2B_CONTROL_PLANE_URL` 用各文件自己的写法），面 A 不加 |
| `deploy/compose/docker-compose.multinode.yml` | **故意不加**：这个栈没有 Redis ⇒ 没有共享记录 ⇒ 门 a 每轮都会推迟，开了只是每 120s 一行的假象。清单里把**触发条件**写下来了（给了共享记录再开），pin 双向钉住（见 §8 minor 1） |

> **控制面侧没有 NetworkPolicy**（它本来就接收 worker/gateway 的 `/internal/**`），所以"CP 增加来自
> agent 的入口"在清单上**无需**改动；这一条差别写在 §11.1 第 5 项与 `deploy-clusters.md` §7.8，
> 免得下一位读者以为漏了一半。

---

## 3. 三档门（本任务的安全核心）与它们各自的"删掉即红"

`control_plane/self_heal.py`，三档**依次**判定，任一条不成立 ⇒ `deferred=<具名原因>`、
`protected/orphans/removed/failed` **全空**（半答案不许被下游当答案用）：

| 档 | 规则 | 具名原因（wire 原文） | 独立 RED 臂（把这一条临时短路） |
|---|---|---|---|
| **a** | 权威面必须是**共享**记录（`_record_store` 存在） | `this control plane's records are process-local (no shared record store): a process-local set cannot certify that no record anywhere claims a tree -- deferring the sweep` | 短路后 `test_a_restart_that_cannot_see_the_records_defers_the_whole_sweep` 与退避用例 **2 failed**，日志里那棵 `sbx_orphan` 当场被删（进程内 CP 的活树同样会被删——这正是要挡的形状） |
| **b** | `unreadable == 0`（旧 `_iter_stored_records` 静默跳过的条目要**数出来**） | `<n> record(s) in the shared store could not be read: the fleet view is incomplete -- deferring the sweep` | 短路后 `test_an_unreadable_record_defers_the_whole_sweep` **1 failed**，`assert … rem… == …[]…`（日志：`c3 self-heal: node k0s-node-1 removed the orphan tree sbx_live (…)` —— 一条读不出的记录让它名下的**活树**被删） |
| **c** | 枚举条数 == `/internal/fleet/metrics` 的 `activeSandboxes`（Task 4 评审钉过的纪律） | `fleet sandbox enumeration is incomplete (<n> of <m> records accounted for) -- deferring the sweep` | 短路后 `test_the_id_count_compared_against_the_fleet_metrics_defers` **1 failed** |

退避：被推迟/报不出去的轮次按倍率 120→240→480→600（封顶）重试，**且每轮都有一行具名日志**
（`test_a_deferred_sweep_backs_off_and_logs_one_named_line` 逐字钉住）。成功一轮把退避清零。

### 3.3 `rm` 不带 worker 身份（为什么、以及没有削弱 Task 4）

Task 4 第四轮评审的 m4 要求"每类指令都带 worker 身份"，理由是 `--worker` **就是**那个身份、且
`--gid` 的门拿 worker 自己的 gid 比。**但 `rm` 不读这两个环境变量**（`maint.c` 的 `rm` 分支只用
`--path`），而这台巡检删的是**没有记录的树**——一个崩了且不重启的 worker **没有身份可命名**。
所以收窄成：

- **CP 面向 worker 的端点语义不变**（每个 op 仍要节点记录里的身份，缺了 503 点名 —— Task 4 的 pin 原样通过）；
- **agent 侧**：`chown` 缺 worker 身份 = 具名 400（`FileOpShapeRefusal`，因为 `--worker` 会退化成
  **root**、`--gid` 会被 `priv_common.c` 拒）；`rm`/`walk` 允许不带（`maint_env` 就不写那两个变量）。
  用例：`test_the_face_that_executes_a_removal_needs_no_worker_identity`。

---

## 4. 三个必需用例（RED → GREEN）

命令：`.venv/bin/python -m pytest tests/unit/test_c3_self_heal_sweep.py -q -p no:cacheprovider`（宿主 venv）。

| 用例 | 红（怎么红的） | 绿 |
|---|---|---|
| ① worker 崩溃且**不重启**时盘上仍在 N 分钟内收敛 | 首轮 **`ModuleNotFoundError: deploy.c3_agent.scan`**（整个模块还不存在）；再把删除循环短路 ⇒ `test_a_worker_that_never_restarts_still_converges` 等 **3 failed** | `test_a_worker_that_never_restarts_still_converges`：CP 里**一个 worker 节点都没有**（只有 in-process 的 `local` 行），agent 报 2 个 id ⇒ 无记录的 `sbx_orphan` 被删（真 `shutil.rmtree` 在 `tmp/` 的真树上）、有记录的 `sbx_live` 原样留下；agent 收到的 argv **逐字** = `[<maint>, "rm", "--path", <workspace base>/sbx_orphan]`，环境里**没有** `E2B_BROKER_WORKER_*` |
| ② CP 滚动重启期间不误删活沙箱 | 同上（端点/模块不存在）；门 a 短路时该形状被红出来 | `test_a_rolling_control_plane_restart_does_not_delete_a_live_sandbox`：共享 store 起第二个 CP 进程（新 `SandboxRegistry`/新 app），重启窗口里新建的沙箱也保住 ⇒ `protected` 两名、`removed=[]`、两棵树都在；`test_a_restart_that_cannot_see_the_records_defers_the_whole_sweep` 是它的反面臂（进程内记录 ⇒ 具名推迟 + 两棵树都在） |
| ③ CP 记录过期 ⇒ 整轮推迟 | 门 b 短路 = **1 failed**（活树被删，见 §3 表格）；门 c 短路 = 1 failed | `test_an_unreadable_record_defers_the_whole_sweep`、`test_the_id_count_compared_against_the_fleet_metrics_defers`、`test_a_tombstoned_record_is_not_an_unreadable_one`（墓碑不是"读不出来"，巡检照跑） |

其余用例（同一文件）：

- **眼睛**：`test_the_scan_reads_sandbox_shaped_trees_only`（`_images`/`state`/`.hidden`/非目录都不报）、
  `test_a_missing_workspace_base_is_an_empty_scan_not_a_crash`（具名 warning）、
  `test_the_report_body_is_the_ids_and_nothing_else`（body **逐字** `{"sandboxes":[id]}` + 头 + URL 路径）、
  `test_the_scan_does_not_report_and_the_control_plane_does_not_delete_a_reserved_name`（`state` 两道都拒）。
- **每一跳的具名失败**：CP 不可达 / CP 拒绝（原文转发）/ 未知键 / 源 IP 不符 / agent 定位不到（503）/
  未配 agent 凭据（503）；`test_the_agent_credential_is_not_a_general_internal_key` 双向（agent token 打不开
  worker 端点；fleet key 打不开巡检端点）。
- **不许 worker 要这条 op**：`test_a_worker_may_not_ask_for_the_sweeps_removal`（400，报文逐字）+
  `FILE_OPS[...].callers == {"self-heal"}`。
- **计数面一致**：`test_the_count_surface_is_the_one_the_fleet_metrics_endpoint_reports`（真打
  `/internal/fleet/metrics` 对比）。
- **接线**：`test_the_agent_app_runs_the_scan_loop_only_when_it_is_configured`（lifespan 起/停；
  `inventory=None` 就不起）。
- **清单**（`tests/unit/test_c3_agent_manifest.py`）：NetworkPolicy pin 更新为
  `["Ingress","Egress"]` + 新臂 `test_the_agents_new_egress_is_one_narrow_named_rule`（出口**只有**
  一条：`app: control-plane` 的 3000，无 IP/DNS）+ `test_only_face_b_scans_the_workspaces`（面 A 不得带
  `E2B_C3_AGENT_SCAN`/`E2B_CONTROL_PLANE_URL`）+ compose 三栈的两面差异。

---

## 5. 跑了什么（命令 + 输出）

```bash
# 宿主（本机 venv；tests/unit 全量在这里停在既有的 fakeredis 收集错误，故按文件跑）
$ .venv/bin/python -m pytest tests/unit/test_c3_self_heal_sweep.py tests/unit/test_c3_agent_manifest.py \
    tests/unit/test_c3_agent_fileops.py tests/unit/test_c3_agent_service.py tests/unit/test_c3_agent_client.py \
    tests/unit/test_c3_fileops_forwarding.py tests/unit/test_c3_fileops_worker.py \
    tests/unit/test_c3_slot_identity_forwarding.py tests/unit/test_c3_slot_identity_lookup.py \
    tests/unit/test_c3_internal_api_shape.py tests/unit/test_c3_cp_rootless.py tests/unit/test_c3_a5_local_rmtree.py \
    tests/unit/test_c3_fileop_degradation.py tests/unit/test_c3_slot_document_naming.py \
    tests/contract/test_orphan_tree_gc.py -q
313 passed

# 容器 lane（Linux，真门禁）
$ docker run --rm --security-opt seccomp=unconfined -e E2B_REQUIRE_SECCOMP_FILTER=0 \
    -v "$PWD":/workspace -w /workspace e2b-sandlock-test:latest python3 -m pytest \
    tests/unit/test_c3_self_heal_sweep.py tests/unit/test_c3_agent_manifest.py tests/unit/test_c3_agent_fileops.py \
    tests/unit/test_c3_agent_service.py tests/unit/test_c3_agent_client.py tests/unit/test_c3_fileops_forwarding.py \
    tests/unit/test_c3_fileops_worker.py tests/unit/test_c3_internal_api_shape.py \
    tests/contract/test_orphan_tree_gc.py -q
255 passed

# 全量（宿主，--continue-on-collection-errors 绕过既有的 redis/fakeredis 收集错误）
$ .venv/bin/python -m pytest tests/unit tests/contract -q --continue-on-collection-errors --tb=no
50 failed, 2212 passed, 95 skipped, 3 errors
# 对照：HEAD（c31aa09）单开一个 worktree 跑同一条命令
$ git worktree add --detach tmp/baseline-wt HEAD   # 跑完已 remove
$ ... 50 failed, 2181 passed, 95 skipped, 3 errors
$ diff before-names.txt after-names.txt   # 空
IDENTICAL FAILURE SETS（50 == 50，逐条同名）⇒ 零回归；本任务净增 31 条通过的用例
# （那些失败全是既有的 macOS/环境项：priv_broker_protocol/priv_helpers 的 socket lane、
#   real_root_gate、xfs_quotactl_backend、migrate_state_base 的 bash 渲染、gateway 的 redis 等）

# 渲染（清单钉之外的第二次确认）
$ kubectl kustomize deploy/k8s > tmp/k8s-render.yaml        # exit 0（1558 行）
$ kubectl kustomize deploy/k8s-k0s > tmp/k8s-k0s-render.yaml # exit 0（1606 行）
$ python3 - <<'PY'   # 从渲染结果里读回形态
# DaemonSet e2b-c3-agent：containers -> agent(scan=None, cp=None) / maint(scan='on',
#   cp='http://control-plane:3000')；NetworkPolicy policyTypes=['Ingress','Egress']；
#   egress=[{'to':[{'podSelector':{'matchLabels':{'app':'control-plane'}}}],'ports':[{'TCP',3000}]}]
PY
```

### 5.1 `multiworker_interference.py`：**跑不了**（brief 同意接受的缺口）

它需要**活集群**（`kubectl` 重启 worker 读日志）**和** `e2b` SDK 的 `<API_URL>`/`<API_KEY>`，而本任务
的约束是"不碰任何集群"。**没有**去伪造一次集群运行；改跑的是**进程内**的等价车道：

1. 真 CP app（`control_plane.app.create_app`）+ 真 agent app（`deploy.c3_agent.app.create_app`）+ 真
   `tmp/` 下的树，跨 ASGI 真发 HTTP（两个方向都是真 `httpx`），断言跨进程的 wire 形状与磁盘结果；
2. 既有孤儿契约 `tests/contract/test_orphan_tree_gc.py`（worker 那条清扫的舰队枚举/推迟/退避纪律，
   在非 agent 形状下原样通过 ⇒ 本任务没有动它）；
3. §3 表格里那三档门的"删掉即红"臂（正是 `multiworker_interference` 想覆盖的"共享盘上谁也不许动别人的树"）。

---

## 6. 文件清单

**新增**：`deploy/c3_agent/scan.py`、`control_plane/self_heal.py`、`control_plane/fleet_view.py`、
`tests/unit/test_c3_self_heal_sweep.py`。

**改动**：`deploy/c3_agent/{app.py,config.py,fileops.py}`、
`control_plane/{api/internal.py,auth.py,c3_agent_client.py,file_ops.py,registry/manager.py}`、
`deploy/k8s/c3-agent.yaml`、`deploy/compose/docker-compose.{prod,multinode}.yml`、
`deploy/stack/docker-compose.prod.yml`、`tests/unit/test_c3_agent_manifest.py`、
`docs/c3-privilege-relocation.md`、`docs/deploy-clusters.md`、
`docs/superpowers/plans/2026-09-28-c3-privilege-consolidation.md`。

**没有改**：`envd_service/**`（worker 那条清扫与其告警一字未动）、`deploy/priv/**`（**没有**给 `e2b-maint`
加动词）、`deploy/k8s/priv-broker.yaml`（Task 7 退役）、`deploy/k8s/control-plane.yaml`（不需要新入口规则）。

---

## 7. 留给部署窗口 / 顾虑

**留给部署窗口**（程序写在 `docs/deploy-clusters.md` §7.8，本任务不执行）：先滚 agent（新 env +
NetworkPolicy），看 `c3-agent inventory:` 周期行；造一棵无记录的 `sbx_*` 树，等 **2–3 分钟**断言消失；
反向臂留一棵有记录的树断言出现在 `protected` 且不被删，再滚一次 CP 复做；最后回填 §7.8 与
`docs/c3-privilege-relocation.md` §11.1 第 5 项。

**顾虑 / 明确记账**：

1. **共享挂载上每个 agent 都看得见全盘** ⇒ 同一棵孤儿树可能被两个 agent 各报一次，第一个删掉后第二个
   的 `rm` 会拿到 `e2b-maint` 的拒绝（"不存在"）。这条**不当成功**（fail-closed，进 `failed`），下一轮
   两个 agent 都看不见它 ⇒ 收敛不受影响；代价是每个孤儿最多多一行具名失败/日志。没有为它做 cross-agent
   单飞（那需要 CP 侧一个 TTL/占位表，正是本任务要避免的"授权表"味道）。
2. **源 IP 那层在本机单机部署里恒真**（两个前提见 §11.1 第 9 项）：真机上若中间有代理/sidecar，所有
   agent 会呈现同一个 IP —— 那时它是**死代码**。真机复验时按 §7.8 第 1 步的两个 grep 确认（拒绝行会
   把"observed vs expected"打出来）。
3. **compose 车道的 CP 仍是 root、且没有 NetworkPolicy**：agent 的出口在 compose 里不受限（compose 没
   有 k8s 的策略层），所以"出口只到 CP:3000"这条**只在 k8s 成立**。要收 compose 得另做（与 Task 5 记账的
   "compose CP 还不是 65534"同一批）。
4. **`E2B_C3_AGENT_SCAN` 默认关**：这是刻意的（face A 没挂工作区、别的车道不该扫），代价是漏配就是
   "巡检不跑"。护栏是清单 pin（面 B 必须 `on`）+ `scanner_for` 的三条具名原因，但**运维改 env 时没有任何
   东西会拦住**"把 `on` 删掉"这件事 —— 真机复验的第 1 步就是那条启动日志。
5. **CP 推迟时的磁盘不收敛**：记录面坏掉（Redis 挂/有记录读不出）期间，孤儿树会一直堆着（这是"推迟"
   的代价，plan §14.5 已承认"CP 不可用 ⇒ 不能回收"，只能靠日志与指标看见）。三档门都会打出精确原因，
   退避封顶 10 分钟，所以不会静默也不会打爆。
6. **`remove-checkpoint`/§11.2.1 第 3 条（平台账读成 unknown）** 不在本任务范围：那条要走
   "CP 用自己记录枚举 + 逐条 `walk-checkpoint`"，是另一条 op 设计（Task 7 候选清单里的项），本任务只把
   **workspace 树**的孤儿回收接回来了。
7. **`_remove_agent_half`（Task 4 的 N5）** 与本任务无关，未动：它保护的是拆箱路径，不是巡检路径；
   巡检的"已缺席"分支不存在（agent 报的是它当下看见的树）。

---

# 8. 评审 round 1 修复（2026-09-29）：2 Important + 3 minors

## Important 1：出口策略把 DNS 一起挡了（**确认属实，已修**）

我加的出口只有 `app: control-plane` 的 3000，而 agent 拨的是 **Service 名** `http://control-plane:3000`，
pod 里没有 `hostAliases`/`dnsConfig`、也不在宿主网络里 —— **k8s 出口隔离是"列出即放行、其余全丢"**，
所以解析这一步先被自己的策略掐掉：上报会以 `the control plane … is unreachable` 永远重试，特性在 k8s 上静默死亡。

**修法（二选一里选前者，理由是 ClusterIP 是分配出来的、钉字面地址会在每次重建后漂）**：加**窄 DNS 出口** ——
`namespaceSelector: kubernetes.io/metadata.name=kube-system` **AND** `podSelector: k8s-app: kube-dns`，
端口 53 **UDP + TCP**（解析走 UDP，截断后的重试走 TCP；两个 selector 写在同一 `to` 条目里是 AND，
既不是"kube-system 里随便谁"也不是"任何叫 kube-dns 的 pod"）。出口仍是两条、无通配、无 IP。

- **pin**：`test_the_agents_new_egress_is_one_narrow_named_rule` 改成**逐字**断言两条规则（含 DNS 的
  namespace+pod AND 与 53/udp+tcp）；删掉 DNS 那一条即红（RED 臂实测：**1 failed**，报文
  `assert [...] == [{…control-plane…}]`）。`test_the_agent_manifest_never_names_a_forbidden_privilege`
  的同一条文本 pin 还抓到我注释里写出的 `hostNetwork` 字样（该 pin 连注释一起禁）—— 注释改成
  "不在宿主网络命名空间里"，引脚保持原样。
- **§7.8 第 1 步**补了"三种失败分开读"：**解析失败**（`Name or service not known` /
  `Temporary failure in name resolution` ⇒ DNS 被挡，改集群 DNS 的 selector，**不要**放通全部出口）、
  **超时**（⇒ 策略/Service）、**403**（⇒ 源 IP，另有 401 = token、503 = CP 侧缺凭据/查不到 agent）。
- 渲染复验：`kubectl kustomize deploy/k8s` exit 0，读回 `egress` 两条规则（选择器与端口逐字如上）。

## Important 2：生产寻址器没有覆盖（**确认属实，已补**）

`K8sAgentAddressResolver.resolve_host`、`ComposeAgentAddressResolver.resolve_host`、
`C3AgentClient.resolve_agent` 此前只有进程内 stub 覆盖。补在 `tests/unit/test_c3_agent_client.py`
（复用该文件既有的假 k8s API / 假 DNS 手法）：

| 用例 | 断言（要点） |
|---|---|
| `test_a_node_whose_worker_pod_is_gone_still_resolves_its_agent` | worker pod **404**（= 已不在）时仍解析出该主机的 agent；断言 `fieldSelector == spec.nodeName=<host>`、`labelSelector == app=c3-agent`，并**逐字**断言本次请求**只有一次**、路径是 `/pods`（**没有**读 worker pod） |
| `test_two_agent_pods_on_one_node_are_a_named_refusal` | 两个 agent pod ⇒ `None` + 具名 warning（`host … has 2 agent pods with an address; refusing (fail closed)`），不是掷硬币 |
| `test_the_compose_lane_resolves_its_own_host_and_nothing_else` | 假 DNS（A + AAAA）⇒ `source_ips` 两个都收；**别的宿主名 ⇒ `None`**（这一车道只认一个名字） |
| `test_resolve_agent_is_fail_closed_at_every_way_it_can_fail` | 四条具名 503：查不到 / 答的不是这个宿主 / 没凭据 / agent 没有面 B 地址（后者经由 `rm` 断言"宁可不发也不猜"） |

**RED 臂**：把 `resolve_host` 改回 worker 键（`return self.resolve(node_identity)`）⇒
`test_a_node_whose_worker_pod_is_gone_still_resolves_its_agent` 与
`test_two_agent_pods_on_one_node_are_a_named_refusal` **2 failed**（日志出现
`worker pod k0s-worker-0 carries no usable uid/nodeName; refusing`）——这正是"worker 崩了"的形状被打红。
`resolve`（worker 键）的既有 pin 原样通过 ⇒ 没有削弱那条路径。

## minors

| # | 处理 |
|---|---|
| **1** | **multinode compose 没有 Redis ⇒ 门 a 每轮推迟 ⇒ 那车道的巡检是惰性的**：确认属实。**选择**：不在那个栈开扫描（开了只是每 120s 一行"被推迟"的假象），把**触发条件**写进 `docker-compose.multinode.yml` 的注释（给它一份记录存储 —— 另两个栈的 `redis` 服务 + `E2B_REDIS_URL` —— 再把面 B 四个变量搬过去），并让 pin **双向**成立：`test_each_compose_stack_runs_exactly_one_agent_facing_the_control_plane` 现在对 multinode 断言"控制面没有 `E2B_REDIS_URL`、而面 B **没有**扫描 env"，另两栈断言 `on` + CP URL。RED 臂：给 multinode 面 B 加回 `E2B_C3_AGENT_SCAN` ⇒ **1 failed**。报告 §2.3 的 compose 记账同步改正 |
| **2** | **`_require_agent_identity` 在事件循环上做 k8s list / `getaddrinfo`**：确认属实（与同文件把路径推导挪进线程的纪律矛盾）。改成 `target = await asyncio.to_thread(_require_agent_identity, request, node_id)`（读的 header/app state 是不可变上下文，线程安全），注释写明是同一条 I-2/`/metrics` 的教训 |
| **3** | **墓碑 vs 读不出的区分要有理由**：在 `fleet_id_snapshot` 的 docstring 里补了"为什么这个计数器必须存在、且不许被简化成 `if payload is None: continue`"——"store 列得出但答不出的记录，**对条数面同样不可见**（`list()` 也跳过它），所以只有这个计数器能看见它；而墓碑是一次**正确的删除**，两边本来就不该有它，当成 unreadable 会让任何删过沙箱的部署永久推迟" |

## 复跑（本轮）

```bash
$ .venv/bin/python -m pytest <C3 相关 15 文件 + worker/compose/docs 清单 pin + orphan GC 契约> -q
414 passed

$ docker run --rm --security-opt seccomp=unconfined -e E2B_REQUIRE_SECCOMP_FILTER=0 \
    -v "$PWD":/workspace -w /workspace e2b-sandlock-test:latest python3 -m pytest \
    tests/unit/test_c3_self_heal_sweep.py tests/unit/test_c3_agent_manifest.py tests/unit/test_c3_agent_client.py \
    tests/unit/test_c3_agent_fileops.py tests/unit/test_c3_agent_service.py tests/unit/test_c3_fileops_forwarding.py \
    tests/unit/test_c3_fileops_worker.py tests/unit/test_c3_internal_api_shape.py tests/contract/test_orphan_tree_gc.py -q
259 passed

$ .venv/bin/python -m pytest tests/unit tests/contract -q --continue-on-collection-errors --tb=no
50 failed, 2216 passed, 95 skipped, 3 errors      # 失败集合与 HEAD 基线仍逐条相同（零回归）

$ kubectl kustomize deploy/k8s && kubectl kustomize deploy/k8s-k0s   # 两个 overlay exit 0
```
