# Task 4 / slice A 报告：文件操作面归 agent（面 B 载荷 + 调用点改道 + A5）

- 工作树：`/Users/polus/project/ai/sandlock-e2b/tmp/wt-c3`（分支 `feat/c3-consolidation`）
- BASE：`e7e1dbd`（Task 3 片 B）
- 裁定：D18.1（不可服务即 fail closed 点名，**绝不**回退本地特权调用）、D18.2（动词白名单，未知动词点名拒）、
  D18.3（复用既有 verb，不新写一套）、D12（agent 自身身份 = 主机名）、硬规则 1/3/5（身份/路径来自 CP 记录；
  只有 `worker↔CP` 与 `CP↔agent` 两条通道）
- **未做**（slice B）：删 worker 镜像里的两个特权二进制 + BND、compose/stack 的 agent 服务、k8s DaemonSet
  face B 载荷、真机验收、文档/pin 清扫。本片**未碰任何集群**，未 push、未 merge、未部署。

---

## 1. 一句话形状

```
worker（零特权）  --{sandbox_id, op}-->  CP（唯一知道路径/uid 的地方）  --{verb, path, uid, gid}-->  agent 面 B
                                                                        └─ exec `e2b-maint`（复用 priv_common.c 的 realpath + 四根）
```

- worker 的请求里**没有 path、没有 uid**（硬规则 1/3、§14.4）：`POST /internal/nodes/{node_id}/file-op` 只收
  `{sandbox_id, op}` 与 op 自己的参数；body 里出现 `path`/`uid`/`gid`/`target`/`worker` 一律 400 点名。
- CP 从**自己的记录与设置**推导目标（`record.host_uid`、`<workspace base>/<id>`、volume registry、
  `<image cache>/secrets/<id>/<name>.secret`、route-B 根），再做一次四根包含性检查后才下发。
- agent 面 B 只做两件事：形状检查 + `execv` `e2b-maint`，与 worker 的 broker **同一个二进制、同一套纪律**
  （不新写白名单）。判定沿用 as_uid 的严格口径：exit 0 才算成功；`chown`/`rm` 有 stdout 即拒；`walk` 的每一行
  必须是文档形状；子进程环境里写入 `E2B_BROKER_WORKER_UID/GID`（否则直接 exec 时 `--worker` 会把树交给 **root**）。

## 2. 交付物

### 2.1 agent 面 B（`deploy/c3_agent/`）

| 文件 | 内容 |
|---|---|
| `fileops.py`（新） | `chown` / `rm` / `walk` 的 **argv 构造 + 严格判定**；`maint_env()`（四根 + uid 池 + `E2B_BROKER_WORKER_UID/GID`）；`FileOpInstruction`；拒绝分两类：`FileOpShapeRefusal`（400：相对路径、chown 目标缺失/冲突）与 `AgentFileOpRefusal`（502：exit≠0 / 多余 stdout / walk 行不合形状） |
| `errors.py`（新） | `AgentRefusal` 移到独立模块（两个面都用；`deploy.c3_agent.app.AgentRefusal` 仍可导入） |
| `app.py`（改） | op 分发：`grant-slot`（逐字不动）+ 三个文件动词（白名单，D18.2）；未知 op 404 点名；D12 自身寻址检查在**解释 body 之前**；每个 op 独立 body 模型，不合形状 → 422 点名 |
| `config.py`（改） | `E2B_C3_AGENT_MAINT`（默认 `/var/lib/e2b-priv/e2b-maint`）、`E2B_C3_AGENT_MAINT_TIMEOUT_S`（300）、`E2B_WORKSPACE_BASE`/`E2B_STATE_BASE`/`E2B_SHARED_VOLUME_ROOT`/`E2B_IMAGE_CACHE_DIR`、`E2B_UID_POOL_START/SIZE` —— 全是 `priv_common.c` 的输入，默认值与 C/Python 侧一致 |

### 2.2 CP：op 表 + 面向 worker 的端点（`control_plane/`）

| 文件 | 内容 |
|---|---|
| `file_ops.py`（新） | **op 白名单表**（12 条）：op → verb + 目标推导；`FORBIDDEN_KEYS`；路径参数把关（volume 走 registry、secret 名走形状、slot 文档走闭集）；四根包含性检查（第二道，不替代 `maint.c` 的第一道） |
| `api/internal.py`（改） | `POST /internal/nodes/{node_id}/file-op`：三步校验（①②+源 IP、③对象属于该节点）+ `host_uid`（记录）+ `worker_uid/gid`（节点记录）+ agent 跳转的 typed 失败（504/502/503） |
| `c3_agent_client.py`（改） | 新增 `chown` / `rm` / `walk`；共用寻址/凭据/并发旋钮与拒绝文案（`refused the <verb>`）；**face B 有独立超时**（`file_op_timeout_s`，默认 600s）——rm/walk 由树的大小界定，5s 的槽位授权超时会把合法拆箱打断 |
| `config.py`（改） | `c3_agent_file_op_timeout_s`（`E2B_C3_AGENT_FILE_OP_TIMEOUT_S`，600）、`route_b_tmp_root`（`E2B_ROUTE_B_TMP_ROOT`，默认空 = 拒绝派生） |
| `registry/nodes.py` + `api/internal.py`（改） | 节点记录新增 `worker_uid` / `worker_gid`：register 与 heartbeat 都收（`workerUID`/`workerGID`），半个身份 → 400，心跳刷新，老 worker 不覆盖 |

**op 表（12 条，全部落到既有 verb）**

| op | verb | 目标（CP 推导） |
|---|---|---|
| `chown-workspace` | chown | `<workspace base>/<id>`（uid=记录 host_uid，gid=节点 worker gid） |
| `remove-workspace` | rm | 同上 |
| `walk-workspace` | walk | 同上 |
| `remove-runtime` | rm | `<state base>/_runtime/<id>` |
| `chown-checkpoint` | chown | `<state base>/_runtime/.checkpoints/<id>` |
| `remove-checkpoint` | rm | 同上 |
| `walk-checkpoint` | walk | 同上 |
| `chown-volume-slice` | chown | volume registry 的 path + `/<id>` |
| `chown-volume-root` | chown | volume registry 的 path |
| `remove-volume-slice` | rm | volume registry 的 path + `/<id>` |
| `chown-secret` | chown | `<image cache>/secrets/<id>/<name>.secret` |
| `scope-slot-document` | chown（`--worker --gid`） | `<route-B root>/<host_uid>/rb-<id>/{policy,program}.json` |

### 2.3 worker：客户端 + 调用点改道（`envd_service/`）

| 文件 | 内容 |
|---|---|
| `agent_fileops.py`（新） | worker 侧客户端：`{op, sandbox_id, ...}` → CP；op 白名单本地也有一份；失败一律点名（不可达 / 被拒 / 非 JSON / 非对象 / walk 无 entry 文本）；`configure/enabled/active` 单例（形状开关 = `E2B_PRIV_HELPER_TRANSPORT=agent`，与 `priv_helpers` 读**同一个**变量） |
| `priv_helpers.py`（改） | `TRANSPORTS` 加 `agent`；`configure_priv_helpers` 在 agent 形状下**不**构造 PrivHelpers（argv/二进制不在这个形状里）、改为配置 agent 客户端；新增 `file_steps_available()`（调用点的门）；`helpers_unavailable_reason` 在 agent 形状下返回 None（否则会误报"退回 E5.1 形状"） |
| `app.py`（改） | per-sandbox uid 的门改用 `file_steps_available()`；`E2B_PRIV_HELPERS=off` + `agent` 同时配置 → 启动即点名拒绝；**孤儿回收（`_startup_uid_reconcile`）在 agent 形状下不再运行**（见 §4 残留） |

## 3. 全量清单：worker 还需要哪些文件操作，各自去了哪里

（landmine 1 要求的清单。全部映射到 `maint.c` 既有动词，**没有**一项需要新写实现。）

| # | 位点（改前） | 现在的去向 | 备注 |
|---|---|---|---|
| 1 | `uid_pool.apply_sandbox_ownership`（建箱属主交棒） | `chown-workspace` → `chown --uid X --gid <worker gid> --recursive` | chmod 仍在 worker（它还是属主），chown 归 agent |
| 2 | `agent.py::_delete_sandbox_runtime` 删树 | `remove-workspace` → `rm` | 仍是"磁盘自己回答"：之后复查 `exists()` |
| 3 | 同上的配对 `_runtime/<id>`（**裸 `rmtree(ignore_errors=True)`**） | `remove-runtime` → `rm` + `exists()` 确认，存活即 `SandboxTreeNotRemoved` | envd 侧的 A5 同型缺陷，顺手修 |
| 4 | `agent.py::agent_import_sandbox`（迁移导入前清空） | `remove-workspace` → `rm` | 解包本身是 worker 自己的 DAC，不改 |
| 5 | `checkpoint_store._hand_to_sandbox` | `chown-checkpoint` → `chown` | |
| 6 | `checkpoint_store._remove_image` | `remove-checkpoint` → `rm` | |
| 7 | `checkpoint_store.image_bytes` | `walk-checkpoint` → `walk` | 用同一份 `WalkEntry.parse` 求和 |
| 8 | `volumes._ensure_shared_volume_root` → `_chown_path` | `chown-volume-root` → `chown` | |
| 9 | `volumes.provision_sandbox_volume_mount` 卷切片 | `chown-volume-slice` → `chown --recursive` | mode 先设（仍在 worker 手里），再 chown |
| 10 | `volumes.cleanup_volume_projects` 删切片 | `remove-volume-slice` → `rm` | 记录里没有卷名 → 点名拒绝（绝不猜） |
| 11 | `executors/sandlock.py` secret 交棒 | `chown-secret` → `chown` | 文件仍由 worker 写（0600），只把属主交出去 |
| 12 | `route_b.W1SlotPool._scope_slot_document`（**landmine 1**：`chown -1:<uid>`） | `scope-slot-document` → `chown --worker --gid <slot uid>` | 同函数里的 **0444 兜底在 agent 形状下不再执行**：拒绝即抛出（D18.1），不再把带 egress-proxy 凭据的 policy 变成世界可读 |
| 13 | `runtime/registry.py` 用量扫描 | `walk-workspace` → `walk` | |
| 14 | `http/health.py` metrics 用量 | `walk-workspace` → `walk` | |
| 15 | `uid_pool._chown_tree` 孤儿回收（`chown --worker`） | **不路由**：agent 形状下该扫描不跑 | 见 §4；这是 §14.3 探针证明的那条越权（任意 worker 可把任意树 chown 给自己），Task 6 用"agent 巡检 → CP 决策"取代 |
| 16 | `runtime/platform_disk.measure_platform_disk_bytes`（走 `<state>/_runtime` 的每个子目录） | **不路由**（仍走本地/既有 broker 回退） | 见 §4 |

CP 侧（`control_plane/`）的特权文件操作：

| 位点 | 现状 |
|---|---|
| A5：`_remove_local_tree_confirming` 的配对 `_runtime/<id>` | **修**（本片）：两半走同一条确认路径；存活即 `False`（见 §5） |
| A3：`registry/volumes.py` 的 `_volumes` 根 `mkdir` + `chmod 1777` | Task 5 的范围（本片未动）；**`maint.c` 没有 mkdir/chmod 动词** → 见 §4 D18.3 |
| `_provision_local` / `_import_sandbox_archive` 的 local 分支 | `local://` 形态不在 C3 覆盖内（§11.1 第 4 项）→ 逐字不动 |

## 4. D18.3 的答复（哪些操作今天没有 verb）

1. **本片改道的每一条都落在既有三个动词内**（`chown` / `rm` / `walk`）——没有新增 verb，也没有第二套路径白名单：
   目标推导在 CP（记录 + 设置），路径纪律在 `priv_common.c`，判定在 `deploy/c3_agent/fileops.py`。
2. **没有 verb、因此按 D18.3 只报告不自己写**的两条：
   - **在调用者自己建不了的目录里建目录/改模式**：CP 的 `_volumes` 卷根 `mkdir` + `chmod 1777`（A3，Task 5）。
     `e2b-maint` 只有 chown/rm/walk，没有 mkdir/chmod。可选路：给 agent 加一条**具名** verb（要评审），
     或按计划 §3.2 第 2 步把 `_volumes` 根迁到 CP 自己的 uid 后由 CP 直接建（不需要新 verb）。
   - **枚举平台态目录**：`runtime/platform_disk.measure_platform_disk_bytes` 走 `<state>/_runtime` 的每个子目录
     （其中 `.checkpoints/<id>` 是沙箱 uid 的 0700）。没有任何 verb 能"列目录"；要收敛只能由 CP 用**自己的记录**
     枚举沙箱 id 后逐条 `walk-checkpoint`（Task 5/6 的形状决定），不在本片猜。
3. **孤儿回收**（`uid_pool._chown_tree` 的 `chown --worker`）：verb 存在，但**故意不给这条 op**——它正是 C3 §14.3
   实测的越权面（"任何 worker 都能把任意树 chown 给自己"）。agent 形状下 worker 的扫描被关掉（点名日志），
   决策上移是 Task 6 的交付。

## 5. A5（配对 `_runtime` 的静默失败）

- 定位按 2026-09-28 更正：在 `_destroy_local`（local lane）里，两个生产栈 `E2B_ENABLE_LOCAL_NODE=false`
  ⇒ **生产不可达**。按"正确性顺手修"处理。
- 修法：新增 `_remove_local_runtime_confirming()`，与沙箱树**同一条**确认路径
  （`priv_helpers.remove_tree(..., on_error="raise")` + 磁盘复查）；存活 → `False`（调用方保留记录并 502）。
  两条分支（树还在、树已不在）都走它。
- **实测验**（本机容器，root + 生产 fixture 形状；`--root` 放在容器自己的文件系统上，否则 chown 是 no-op）：

```
$ docker run --rm --user 0:0 -v "$PWD":/repo -w /repo -e PYTHONPATH=/repo python:3.12-slim \
    python3 deploy/scripts/acceptance/probe_c3_a5_silent_rmtree.py --root /probe-root/c3-a5
N-fixture-shape: ok (dir=0o700 65534:65534, parent=0o711 65534, ...)
A-root:    returned=True  survived=False -> ok (invisible today)
A-as-uid:  returned=False survived=True  child_error=None -> no silent failure
A-strict:  raised="PermissionError: [Errno 13] ..." survived=True -> ok (the failure is real, only masked)
A-owner:   returned=True  survived=False -> ok (same uid means no privilege needed)
C3-A5-VERDICT=not-reproduced

# 同一容器里把 control_plane/api/sandboxes.py 换回 HEAD 的那份
--- PRE-FIX (HEAD) ---
A-as-uid:  returned=True survived=True child_error=None -> SILENT FAILURE
C3-A5-VERDICT=reproduced
```

⇒ 判据从 `reproduced` 变为 **`not-reproduced`**。探针本身需要 **root + 非属主 uid + 能 chown 的文件系统**
（集群里是 CP pod；本机是容器）。宿主 lane 另行精确钉住行为（`tests/unit/test_c3_a5_local_rmtree.py`，5 条）。

## 6. 测试（RED → GREEN）

| 新用例 | RED | GREEN |
|---|---|---|
| `tests/unit/test_c3_agent_fileops.py`（agent 面 B，14 条） | `ModuleNotFoundError: deploy.c3_agent.fileops` | 14 passed |
| `tests/unit/test_c3_fileops_forwarding.py`（CP 端点 + op 表，30 条） | op 表的 wire/路径断言全红（先有测试，后有 `control_plane/file_ops.py` 与端点） | 30 passed |
| `tests/unit/test_c3_fileops_worker.py`（worker 客户端 + 调用点，28 条） | 同上（客户端模块先缺） | 28 passed |
| `tests/unit/test_c3_a5_local_rmtree.py`（A5，5 条） | 3 failed（`True is not False`） | 5 passed |
| 既有文件新增面：`test_c3_agent_client.py`（+5 条） | 新动词断言先失败 | 28 passed（整支） |

最终：

```
# 宿主（本机 venv；tests/unit 全量在这里停在既有的 fakeredis 收集错误，故按文件跑）
$ .venv/bin/python -m pytest <本片新增 6 文件 + test_c3_agent_client.py + test_c3_agent_service.py \
      + test_c3_slot_identity_forwarding.py + test_cp_state_base.py + test_shared_volume_traversal.py> -q
162 passed

# 容器 lane（Linux，真门禁）
$ docker run --rm --security-opt seccomp=unconfined -e E2B_REQUIRE_SECCOMP_FILTER=0 \
    -v "$PWD":/workspace -w /workspace e2b-sandlock-test:latest python3 -m pytest tests/unit -q
22 failed, 1932 passed, 17 skipped
```

那 22 条**全部与本片无关**，逐类核过（并且在"把本片改动文件换回 HEAD"的对照容器里同样失败）：
容器内看不到 git（`test_deploy_env_examples_are_ignored` 5 条、`test_docs_only_point_at_repo_artifacts` 1 条）、
镜像未预热导致的 428（`test_control_plane_network_local` 3 条、`test_migration_volume_quota` 2 条）、
bash 探针渲染（`test_c2_p0_probe` 2 条、`test_migrate_state_base_script` 3 条）、
root 无 `CAP_SYS_PTRACE` 时多出的告警（`test_xfs_project_quota_agent` 5 条、`test_quota_agent_client` 1 条；
加 `--cap-add SYS_PTRACE` 后全绿）。macOS 上另有 chown/AF_UNIX/ctypes 环境失败——同属既有。

被本片**有意改动**并同步更新的三个既有 pin：

- `tests/unit/test_cp_state_base.py`：`sandboxes.py` 里 `sandbox_runtime_dir` 的调用数 2 → 1（A5 让两半共用一个已解析路径）。
- `tests/unit/test_shared_volume_traversal.py`、`tests/unit/test_checkpoint_store.py`：桩函数接受新增的 `sandbox_id` 关键字。

## 7. 文件清单

新增：`deploy/c3_agent/fileops.py`、`deploy/c3_agent/errors.py`、`control_plane/file_ops.py`、
`envd_service/agent_fileops.py`、`tests/unit/test_c3_agent_fileops.py`、`tests/unit/test_c3_fileops_forwarding.py`、
`tests/unit/test_c3_fileops_worker.py`、`tests/unit/test_c3_a5_local_rmtree.py`。

改动：`control_plane/{api/internal.py,api/sandboxes.py,app.py,c3_agent_client.py,config.py,registry/nodes.py}`、
`deploy/c3_agent/{app.py,config.py}`、
`envd_service/{agent.py,app.py,executors/sandlock.py,http/health.py,priv_helpers.py,route_b.py,runtime/checkpoint_store.py,runtime/registry.py,uid_pool.py,volumes.py,worker_identity.py}`、
`tests/unit/{test_c3_agent_client.py,test_checkpoint_store.py,test_cp_state_base.py,test_shared_volume_traversal.py}`。

## 8. 自评（本片）

- **请求面**：worker 侧只在三处带 sandbox 之外的参数（volume 名、secret 名、slot 文档名），三者都在 CP 侧被
  关进 namespace（registry 查表 / 正则 / 闭集）。`path`/`uid` 一旦出现即 400 点名（有专门用例）。
- **两道白名单**：CP 推导后先做四根包含性检查（`ControlPaths.contains()`），agent 再交给 `maint.c` 独立 realpath
  + 四根；`deploy/c3_agent/fileops.py` **不做**路径解析，故没有第二套实现。
- **没有本地回退**：agent 形状下每个位点都是 `if client is not None: 走 agent; else: 旧路径`。形状是**启动期**
  决定的（`E2B_PRIV_HELPER_TRANSPORT`），不是逐调用决定；形状要不到客户端时启动即拒绝。
- **失败面**：agent 不可达 502 / 卡住 504 / 拒绝 502 原文 / 无客户端 503 / 无 host_uid 503 / 无 worker 身份 503，
  全部点名；`walk` 的坏行、`chown|rm` 的多余 stdout、exit≠0 都是拒绝（半做的动作绝不当成功）。
- **没有扩面**：没有新增 `SYS_ADMIN`/`SYS_PTRACE`/特权字段；agent 面 B 的能力集与 C1 broker 逐条相同，
  本片只加"客户换成 CP"的那一半代码。**没有** worker↔agent 通道：worker 只连 `E2B_CONTROL_PLANE_URL`，
  agent 地址与 token 不出现在 worker 侧。

## 9. 顾虑 / slice B 必须收尾的事

1. **上线顺序**：CP 的 `file-op` 需要节点记录里的 `workerUID/GID`，而它由 worker 上报。⇒ 先滚 worker，再滚 CP；
   否则所有 chown 会以 503 点名拒绝（可见，但会挡建箱）。
2. **CP 需要 `E2B_ROUTE_B_TMP_ROOT`**：`scope-slot-document` 的路径由 CP 推导，而 CP 清单里今天没有这个变量。
   slice B 要给 CP 加（k8s：`/var/lib/e2b-sandboxes/state/.route-b`；compose prod/multinode/stack：
   `/var/lib/e2b-sandboxes/.route-b`，与 worker 现值逐字一致）。没设时该 op 503 点名，route-B 槽位起不来。
3. **agent 面 B 的载荷**：DaemonSet 里那半个容器现在还是 `sleep infinity` → 换成 `python -m deploy.c3_agent`
   （同一进程服务面 A 与面 B），并给足 `E2B_UID_POOL_START/SIZE`（否则 `chown --uid` 被池门拒）与四个根；
   ⚠ 目前 DaemonSet 的 `E2B_IMAGE_CACHE_DIR=/var/lib/e2b-images` 与 CP 的 `.../_images` 不同名，而
   `chown-secret` 的路径由 CP 推导 ⇒ 两边必须是同一个目录（挂载/取值对齐，否则 agent 以"不在四根内"拒）。
4. **worker 侧开关**：`E2B_PRIV_HELPER_TRANSPORT=agent` + `E2B_SLOT_IDENTITY=agent-grant`（Task 3）要一起设；
   删二进制/BND（D1 的 pin）之后 `auto` 会退化成"无 broker 的 E5.1 形状"，那是**形状回退**，必须在同一提交里切开关。
5. **语义变化（要写进 slice B 的说明）**：`_runtime/<id>` 与 `_runtime/.checkpoints/<id>` 的删除现在会因 agent 拒绝
   而让拆箱失败（500/502），这是有意的 fail-closed；孤儿回收在 agent 形状下不跑（Task 6 接手）；
   `platform_disk` 的整片 `<state>/_runtime` 只能本地走（§4 第 2 条），需要 Task 5/6 决定是否加 CP 侧枚举。
6. **未验收**：判据 4（真机 `CapEff=0x0b`）、判据 7 的真机版、判据 14/15 的清单解析验收都在 slice B
   （本片只在容器里跑了 A5 探针与单元/契约 lane）。

---

# 附录：片 A 评审修复轮（2026-09-29，Needs fixes → 3 Important + 4 minors）

评审的三条 Important 都是"我的新用例看不见"的真缺陷——它们全是**接线**问题：op 表、门、（失败的）
幂等性都在我的用例覆盖之外。逐条修 + 逐条 RED/GREEN。

## 修了什么

| # | 评审发现 | 修法 | 位置 |
|---|---|---|---|
| **I1** | 三个卷 op 永远解析不到：`volume_paths` 按**显示名**建索引，而 worker 送的是 volume **id**（mount payload 的 `name` 就是 id，CP 自己也用 `volumes.get(name)` 解析）⇒ `_volume_root` 一律 404；`remove-volume-slice` 还被删箱路径无门调用 ⇒ 带卷的沙箱删不掉、切片留着 | 索引改 `record.volume_id`；错误文案改成"is not a volume **id**"；用例改用 id（哨兵 `<volume-id>` 由用例替换），并新增"**显示名不是键**"的用例把键空间钉死 | `control_plane/file_ops.py`、`tests/unit/test_c3_fileops_forwarding.py` |
| **I2** | 属主交棒在 agent 形状下**不可达**：`configure_priv_helpers` 在该形状不装 `PrivHelpers`，而两个门还在问 `active_helpers()`——`envd_service/agent.py` 的建箱门（⇒ `host_uid` 永远 None ⇒ 交棒与所有面 B `chown` 都不发生且**无日志**）与 `envd_service/volumes.py::_can_manage_sandbox_uid`（⇒ 卷所有权模型被跳过） | 两处门都改走 `priv_helpers.file_steps_available(settings)`（与我在 `app.py` 已改的那两处同一个谓词）；新增**端到端**用例驱动**真** `_agent_create_sandbox` / 拆箱端点 / `provision_sandbox_volume_mount`，断言 `host_uid` 已设**且** op 以精确参数发到 agent | `envd_service/agent.py`、`envd_service/volumes.py`、`tests/unit/test_c3_fileops_worker.py` |
| **I3** | 拆箱丢了幂等性：`e2b-maint rm` 对**不存在**的路径硬拒（`realpath` → NULL），而旧代码把"已经没了"当成功；重试删除 / 从未落盘的沙箱因此失败。且 `remove_runtime` 在命名 try/except 之外 ⇒ 裸 500 | **D19**：新增 `_remove_agent_half()`——"路径不存在"= **成功 + 具名日志**（前置检查 + 拒绝后再查一次，覆盖竞态），其它失败（权限/IO，路径仍在）一律 `SandboxTreeNotRemoved`（500 带原因）；树/`_runtime`/迁移导入三处统一走它，树半的 `except` 补 `except SandboxTreeNotRemoved: raise` 避免二次包裹 | `envd_service/agent.py` |
| m4 | `int(node.worker_uid)` 无条件 ⇒ 只有 chown 有 503 门，`remove-*`/`walk-*` 在滚动窗口里 `TypeError` → 裸 500 | 身份门提到所有 op 之前（每类指令都带 worker 身份：`--worker` 就是它，树的组也是它的 gid），缺身份一律 503 点名 | `control_plane/api/internal.py` |
| m5 | agent 形状关掉孤儿回收是**静默跳过**（只有注释） | 启动时一行具名 `logger.warning`（`sweep_wanted and agent_fileops.enabled()` 才发，说明"开关被形状否决、Task 6 接手"）；用例驱动真 lifespan 钉住这一行 | `envd_service/app.py` |
| m6 | `remove-checkpoint` 删**整个 store**，而调用方传 `<store>/latest` | **在代码里显式记录这处漂移**（评审二选一里的"记录"）：`_remove_image` 的 docstring 说明为什么是精确而非近似（每沙箱一张镜像、"消费即走"、teardown 钩子本就要删 store；收窄回 `latest` 会留下空 store，而清理它的正是 agent 形状关掉的那次扫描），op 表同处标注，用例继续钉"派生路径 = store" | `envd_service/runtime/checkpoint_store.py`、`control_plane/file_ops.py` |
| m7 | `assert ... startswith(` 违反精确断言 | 改成整条精确等于（含 `entry!r`），并显式构造该 entry | `tests/unit/test_c3_fileops_worker.py` |

> **评审 Minor 7（记录用，本轮不改代码）**：`control_plane/file_ops.py::_inside()` 是 CP 侧**手写的**第二道包含性
> 规则（`Path.resolve()` + `is_relative_to`），与 `priv_common.c` 的 `realpath` 纪律**不共享实现**。两者可能分歧的角落：
> ① 符号链接——CP 在推导时 `resolve()`（可穿透链接）而 `maint.c` 是 `realpath` 后再比，语义相同但**对不存在路径**的
> 处理不同（CP 的 `resolve(strict=False)` 成功，`realpath` 返回 NULL ⇒ agent 拒）；② 路径不存在/不可解析——CP 侧
> `_inside` 里 `OSError` 直接 `False`（拒），`priv_common` 是 NULL → 拒，方向一致但**文案不同**；③ 末尾斜杠与 `..`
> 归一化两边都用系统调用式归一，行为一致。最终判定：CP 那道是"别把明显越界的目标发出去"的**发送前**检查，
> `maint.c` 那道是**执行前**的权威检查，两者不互为替代（§14.4）；若最终评审要收成一处，方向应是把 CP 的检查
> 换成 `deploy/priv/` 的同一实现（而不是删掉它）。

## RED → GREEN（每条 Important 都先"退回去看它红"）

命令统一为 `.venv/bin/python -m pytest <file>::<test> -q -p no:cacheprovider`（宿主 venv）。

| 判据 | 退回方式（临时把修复改回原样，跑完立刻改回） | RED 输出 | GREEN |
|---|---|---|---|
| I1 | `volume_paths[record.volume_id]` → `[record.name]` | `assert 404 == 200` ×3；`3 failed, 9 passed`（三条卷 op 行全红） | 31 passed（整支 `test_c3_fileops_forwarding.py`） |
| I2 | 两个门 → `priv_helpers.active_helpers() is not None` | `assert None == 10007`（`record.host_uid`）；`assert False is True`（`_can_manage_sandbox_uid()`）；`2 failed` | 两用例各自通过（35 passed 整支） |
| I3 | `_remove_agent_half` 去掉"不存在即成功"的前置/后置检查 | `assert 500 == 204` + `agent delete sbx_twice failed: ... e2b-maint: refused: ... does not exist` | 同一用例 204 + 两行具名日志（`already absent; nothing to remove`） |
| m4 | 身份门退回"只在 chown 分支" | `TypeError: int() argument must be ... not 'NoneType'`（`internal.py:660`）→ 裸 500 | `503` + `node node_a has reported no worker identity (workerUID/workerGID): refusing to instruct the agent` |

最终跑（GREEN）：

```
# 宿主
$ .venv/bin/python -m pytest <本片 6 个新增文件 + 相关既有 9 个文件> -q
307 passed

# 容器 lane（Linux，真门禁）
$ docker run --rm --security-opt seccomp=unconfined -e E2B_REQUIRE_SECCOMP_FILTER=0 \
    -v "$PWD":/workspace -w /workspace e2b-sandlock-test:latest python3 -m pytest tests/unit -q
22 failed, 1942 passed, 17 skipped
```

容器里那 22 条与上一轮**逐条相同**（git 不可见 / 428 预热门 / bash 渲染 / root 无 `CAP_SYS_PTRACE` 的额外告警），
且都在"文件换回 HEAD"的对照里同样失败；本片新增/修改的用例在这一轮**零失败**（上一轮 3 条与我相关，已按下面两条
修成环境无关）。

## 用例自身的两处环境无关化（不是放宽断言）

- 卷用例：容器 lane 以 root 跑时 fixture 卷根是 root 属主，会**额外**触发"root 属主的卷根才交棒"这条**既有**策略，
  于是同样的断言在两条 lane 上不同。修法：root 时把 fixture 卷根 `chown` 给池 uid，让"被测的那一半（切片）"
  在两条 lane 上完全一致；卷根那条 op 由自己的用例（`chown` 直接调用）精确钉住。
- 日志列表：容器 lane 会多出 `SECCOMP_FILTER_MISSING` 与（root 时）`E2B_PER_SANDBOX_UID ... CAP_SYS_PTRACE` 两行。
  修法沿用本仓库既有做法（`test_xfs_project_quota_agent._warnings`）：**按 logger 选中**（`envd_service.agent`）
  再整条精确比较；孤儿回收那支把 `has_effective_cap` 打桩为真（该告警是从 `create_app` 发的，桩要打在建 app 之前），
  断言仍是整条精确。两条 lane 都可复现同一答案。

---

# 附录 B：第二轮评审修复（2026-09-29，Needs fixes → N1 + N2/N3/N4 + N5/N6 记录）

第一轮的 I1/I2/I3 与 minors 4–7 已被复审确认修好。这一轮是同**一类缺陷**（"两处各写一份规则/接线"）在
`scope-slot-document` 上的残留，加上四处行为回归。

## N1（必修）：slot 文档目录名两份规则，CP 一律指错目录

**根因**：worker 侧 `uid_dir/<slot_name>` 的 `slot_name` 在生产**永远是** `instance_name`
（`executors/sandlock.py` 传 `name=self.instance_name`，`= _instance_name_for()` = 裸 sandbox id，或 >64 字节时的
`sbx_<sha256[:16]>`），而 CP 猜的是 `rb-<id>` ⇒ 在 agent 形状下 `scope-slot-document` 指向一个**不存在**的目录，
`maint.c` 的 realpath 拒掉 ⇒ `AgentFileOpsError` 逃出 `route_b._write_slot_documents`（那个 `except` 只认
`PermissionError`/`PrivHelperError`）⇒ route-B 槽位起不来——**正好是带 egress-proxy 凭据的那份文档**。
两个新用例都没抓到，因为它们各自钉住自己那一侧的假设（CP 侧钉 `rb-{id}`，worker 侧手搓 `rb-` 字符串）。

**按 D20 的做法**：

- 规则收敛到 **`gateway_common/paths.py::route_b_instance_name(sandbox_id)`**（+ `ROUTE_B_INSTANCE_NAME_MAX_BYTES = 64`），
  控制面与 envd 共用的那个模块；
- worker 的 `SandlockExecutor._instance_name_for()` 改为调用它（`instance_name` 仍是它传给池的值）；
- CP 的 `_slot_document()` 改为 `route_b_tmp_root / <uid> / route_b_instance_name(sandbox_id) / <name>`；
- `rb-` 兜底**保留**给"不命名实例"的调用者（嵌入方/测试），但**生产永不走它**，且这一点被写成检查：
  `_scope_slot_document()` 在 agent 形状下把"目录叶 != 共享规则的答案"**点名拒掉**（否则就会把 CP 指到一个
  没人创建的目录、或把别的文档交出去）；`route_b.acquire_sync` 与 `_scope_slot_document` 的 docstring 都写明了这条。

**对等的（peer-pinned）用例**：`tests/unit/test_c3_slot_document_naming.py` 驱动**真**执行器的 route-B acquire
（真 `W1SlotPool` + 假 spawner/channel，生产那句 `name=self.instance_name` 原样执行），然后拿**磁盘上真出现的目录**
与 `control_plane.file_ops.derive()` 的推导逐字比较——普通 id 与 **>64 字节 id** 各一例；另加"`rb-` 兜底被点名拒"
一例。原来那两个"自证"用例改成引用共享规则（并说明为什么）。

## N2/N3/N4：这一轮引入的行为回归

| # | 发现 | 处理 |
|---|---|---|
| **N2** | `/metrics` 是 async，却 inline 调 `_dir_size → agent_fileops → httpx.Client(timeout=660s)`：CP 黑洞时把 worker 事件循环（心跳 + 所有沙箱 API）卡住最多 ~11 分钟；形状失败还变成裸 500 | 两处都修：① `used_bytes = await asyncio.to_thread(_dir_size, …)`（离开事件循环）；② `agent_fileops` 给 **connect 阶段**单独的短超时（`DEFAULT_CONNECT_TIMEOUT_S = 5.0`，"读"仍用 file-op 预算，因为大树本来就慢）；③ `_dir_size` 在 agent 失败时**降级**（具名 warning + 回落到 worker 自己的 walk）——预 C3 形状本来就是这个答案，metrics 不该因为监控不到而 500 |
| **N3** | `_ensure_shared_volume_root` 只 catch `OSError`，而 agent 分支抛 `AgentFileOpsError`（`RuntimeError`）⇒ 一个**文档写明 best-effort** 的步骤会中止 `build_volume_mounts` ⇒ 建箱失败 | **刻意选 best-effort**（另一条路 fail-closed 也说得通，这里选一致性）：新增 `except AgentFileOpsError` + 具名 warning，并在代码里写明为什么安全——只有**root 属主**的卷根会被交棒（遗留布局迁移），而沙箱自己的挂载视图是它的 `0770` 切片，那条照旧 fail closed（`chown-volume-slice` 不在 best-effort 里）。同时抽了 `_volume_root_needs_handover(st)` 这个谓词做测试缝，避免用例依赖"谁拥有 fixture" |
| **N4** | `checkpoint_status` 在"nothing to report"契约之外调 `image_bytes`；`registry` 的用量扫描同形 ⇒ 一次瞬时 CP 故障把**诊断**变成 500 | 两处都按各自的既有"unknown"约定降级：`checkpoint_status` → `imageMB = 0` + 具名 warning（`image_bytes` 本来就对问不到的 broker 返回 0）；`disk_usage_snapshot` → `size = None` ⇒ 该沙箱作为**缺席**上报（"unknown 不能被读成 empty"是它们自己的 docstring）+ 具名 warning |

## N5/N6：按评审要求"记录"（不改代码）

- **N5**：`_remove_agent_half` 的"已缺席"分支在拿到拒绝后会吞掉**任意** `Exception`，而"缺席"是拿 **worker 的**路径判的、
  删除是 agent 在 **CP 推导的**路径上做的 ⇒ 两侧 base 配不一致的部署可能"报成功而树还在"。已在 `_remove_agent_half`
  的 docstring 里记明（非安全问题：真正执行（或不执行）的仍是 CP 推导的那条路径；且这类配置漂移在别处已经可见：
  磁盘报告看不到这棵树、沙箱文件从所有 API 消失），并注明最终评审若要收紧，方向是在注册时做一次配置一致性检查，
  而不是每次拆箱多加一次往返。
- **N6**：`_remove_image` 的 agent 分支没有"已缺席"处理（`e2b-maint rm` 对不存在路径硬拒），所以"调用方 `is_dir()`
  与 op 之间镜像消失"会拒而不是当成"没有可消费的东西"。已在 `_remove_image` 的 docstring 里记明：所有调用方都先检查，
  窗口只有一个 syscall 宽，且结果是**具名拒绝**而非静默跳过；留待最终评审决定是否并入 `_remove_agent_half`。

## RED → GREEN（N1 与 N2/N3/N4）

| 判据 | 退回方式（改回原样跑一次，随后立刻改回） | RED | GREEN |
|---|---|---|---|
| N1 | `_slot_document` 的叶改回 `f"rb-{sandbox_id}"` | `test_the_slot_directory_is_what_the_control_plane_derives[normal]` 与 `[over-64-bytes]` 双双失败：`assert '<…>/sbx_docs/policy.json' == '<…>/rb-sbx_docs/policy.json'`（`2 failed`） | `test_c3_slot_document_naming.py` 3 passed |
| N2 | ① `used_bytes` 改回 inline `_dir_size`；② 去掉 `_dir_size` 的降级 | `assert <_MainThread(...)> is not <_MainThread(...)>`（在事件循环线程上跑）+ `AgentFileOpsError: ... walk-workspace ...` | 5 passed（`test_c3_fileop_degradation.py`） |
| N3 | 去掉 `except AgentFileOpsError` 分支 | `envd_service.agent_fileops.AgentFileOpsError: ... chown-volume-root ...` 逃出 `_ensure_shared_volume_root` | 同上（该用例断言那条具名 warning） |
| N4 | 去掉 `checkpoint_status` 与 `registry` 的两处 try | 两处 `AgentFileOpsError: ... walk-checkpoint / walk-workspace ...` | 同上 |

最终跑（GREEN）：

```
# 宿主（含新加的 test_c3_slot_document_naming.py / test_c3_fileop_degradation.py + 相关既有 19 个文件）
$ .venv/bin/python -m pytest <21 个文件> -q
420 passed, 7 skipped          # skip 全是既有的环境/依赖项（fakeredis、chown 需 root）

# 容器 lane（Linux，真门禁）
$ docker run --rm --security-opt seccomp=unconfined -e E2B_REQUIRE_SECCOMP_FILTER=0 \
    -v "$PWD":/workspace -w /workspace e2b-sandlock-test:latest python3 -m pytest tests/unit -q
22 failed, 1950 passed, 17 skipped     # 22 条与对照 HEAD 逐条相同（git 不可见 / 428 预热门 / bash 渲染 / root 无 CAP_SYS_PTRACE）
```

## 复审确认项（本片同意其结论，无额外工作）

- I1/I2/I3 与 minors 4–7 已修；`test_the_volume_ownership_path_runs_under_the_agent_transport` 在两条 lane 上都是
  "退回谓词即红"的敏感钉子；D19 的命名一路到 HTTP 边界（成功 204、已缺席 204 + 具名日志、其它失败 500 带原因）。
- 本轮新增的回归全部落在"接线/契约"这一层，且都由**对等**用例（peer-pinned）而不是自证用例来钉——这正是第一轮
  漏掉 N1 的原因，报告 §6 的那段教训（"自证用例不算证据"）在本轮被写成了一条具体的测试。

---

# 附录 C：第三轮评审修复（2026-09-29，Needs fixes → I-1/I-2/I-3 + 5 minors）

复审确认 N1 已真正关闭、N3/N4 形状正确；本轮的 3 条 Important 与 5 条 minor 同样**全部藏在既有用例之外**。

## I-1：属主交棒跳过了 mode pass（agent 形状下树对 worker 只读）

`uid_pool.apply_sandbox_ownership` 在 agent 分支里**提前 return**，而 0o770 的 mode pass 在 return **之后**（注释却写
"上面"）。`maint.c` 的 chown 不改 mode，交棒后 worker 只是组内成员、不再是属主，于是它必须写的树（files API /
快照 / 生命周期）最差只剩 `r-x`。旁边的卷切片路径（`volumes.provision_sandbox_volume_mount`）正是"先 chmod 后 chown"，
本次把工作区这条改成同一形状：**mode pass 在两种形状里都先跑**（worker 仍是属主时才 chmod 得动）。

**用例**：`test_the_ownership_handover_sets_the_tree_modes_before_it_leaves` —— 在 agent 形状下建树、调用交棒，
断言**磁盘上的 mode**（树与子目录 `0o770`，文件保持 `0o644`）且 op 照发。退回"先 chown 后 return"即红：
`AssertionError: assert 493 == 504`（0o755 vs 0o770）。

## I-2：N2 的修复漏了 import 端点（同一类：async handler 里同步阻塞）

`agent_import_sandbox` 是 `async def`，却在树已存在（文档写明的重试场景）时**内联**调 `_remove_agent_half`，那是一次
同步 `httpx.Client` POST（connect 5s / read 660s）。已改为 `await asyncio.to_thread(...)`（非 agent 分支的
`priv_helpers.remove_tree` 一并挪进去——它同样是整棵树的重活）。

**用例**：`test_the_import_removal_runs_off_the_event_loop` —— 驱动真 `/agent/sandboxes/{id}/import`（真 tar.gz），
用桩客户端记录**调用线程**，断言 `threads[0] is not loop_thread` 且端点 204、内容已恢复。退回内联即红：
`assert <_MainThread(...)> is not <_MainThread(...)>`。

**同模式清扫（本轮逐条看过，结论写在这里）**：

| async 位点 | 是否内联阻塞 | 处理 |
|---|---|---|
| `agent_import_sandbox` 的树清理 | **是**（本轮唯一） | 已改 `to_thread` |
| `agent_create_sandbox` / `_agent_create_sandbox` | 否（`to_thread`） | 不动 |
| `agent_delete_sandbox` / `_delete_sandbox_runtime` | 否（`to_thread`，N32 的注释在案） | 不动 |
| `agent_checkpoint_sandbox` / `agent_checkpoint_status` / `agent_restore_sandbox` | 否（`to_thread`） | 不动 |
| `agent_create_snapshot`（copytree/marker/清理） | 否（`to_thread`） | 不动 |
| `/metrics`（`envd_service/http/health.py`） | 曾是（N2） | 上一轮已改 `to_thread` |
| `agent_pause_sandbox` / `agent_resume_sandbox` / `agent_sandbox_logs` / `agent_list_untrusted` / `agent_park_untrusted` / `agent_update_sandbox_network` / `agent_image_warm_*` / `agent_delete_snapshot` / `agent_health` | 否：不触达 agent 层（`pause/resume` 走 executor 的同步 verb，不是本片引入的路径） | 不动 |
| `agent_export_sandbox`（`tar.add(workspace, recursive=True)`）与 import 的 `write_bytes` + `_extract_sandbox_archive` | **是（内联文件层重活），但不是本片引入、也不触达 agent 层** | **不动**，但第四轮评审指出本表不能读成"这一类已经清干净"：这两处与 I-2/`/metrics` 同性质，已记入 `docs/c3-privilege-relocation.md` §11.2.1 第 7 条作为 slice B/Task 7 的清扫项 |
| `NodeAgent` 的后台轮询协程（`_loop`/`_pulse`/`_scan_disk_round`/`_reconcile_round`…） | 否（各自已 `to_thread` 调用阻塞段） | 不动 |

## I-3：平台磁盘账把"测不到"读成 0

`.checkpoints` 那一支是沙箱 uid 的 `0700`，agent 形状没有可问的 broker ⇒ `dir_size` 返回 `None` ⇒ 被折成 **0 且无日志**，
而 0 对**准入**和**心跳账**都是"平台什么也没存"。按评审给的两条路里选"**显式具名 unknown**"（把 `.checkpoints` 真正接回
agent 需要 CP 侧对"记录已不在的孤儿 store"也允许 walk，属设计决定，记为 slice B 后续项）：

- `measure_platform_disk_bytes()` 现在返回 `int | None`：**目录不存在 = 0（真的是空）**，**测不到 = None**，并在
  无法测量的那个子目录上打一条具名 warning（此前 `None`→0 这一步完全没有日志）；
- `checkpoint_admission(used_bytes=None, …)` 与 `checkpoint_no_room_reason(used_bytes=None, …)` 都**拒绝**并给出
  同一句 `UNKNOWN_ACCOUNT_REASON`（"未知 ≠ 空，无法记账时不落盘"）——与"账满"同一个出口：沙箱原地冻结；
- 心跳侧 `_measure_platform_account` 在未知时**省略** `platformDiskUsedMB`（CP 的 `update_usage` 把缺字段当"不更新"，
  会保留上一次的数），只照发预算；回复体 `_capture_reply` 里该字段同样可以是 `null`；
- **CP 侧仍有的局限要说清**：`platform_disk_used_mb` 是 int 字段，节点**从未**报过 usage 时它读作 0 ——
  worker 侧"静默的 0"已经消失（有日志、有 None、准入拒绝），CP 侧的"没有数"与"0"仍不可分，这一条留给
  接回 agent 的那次改动一起处理。

**用例**：`test_a_child_the_worker_cannot_read_makes_the_account_unknown_not_zero`（含"空目录仍是 0"）、
`test_admission_and_the_pre_capture_check_refuse_an_unknown_account`、
`test_the_heartbeat_omits_an_unmeasurable_platform_usage`、`test_a_measurable_account_still_reports_its_usage`，
以及既有 pin 改写：`test_an_unlistable_runtime_dir_is_unknown_and_says_so`（原为"……before_reporting_zero"）。
退回 `0 if size is None else int(size)` 即红：`assert 0 is None`（两处）。

## minors

| # | 处理 |
|---|---|
| m-1 | **CP 侧**：`workerUID`/`workerGID` 的 400 文案点名真实条件（"a worker may not run as root (uid 0) …"），并加用例钉住 uid 0 时的整条消息。**worker 侧**（顺带修掉一条会被契约 lane 抓到的连带缺陷）：`worker_identity_fields()` 在 uid/gid ≤ 0 时**不报**这对字段，并打一条具名 warning —— 注册与心跳不是文件操作，报 0 只会让整个节点 join 不上（容器 lane 里 `test_orphan_tree_gc` / `test_checkpoint_status_api` 正是这样红的）；省略后节点照常加入，而需要身份的每一个 op 仍由 CP 以具名 503 拒（本轮新增两个用例：root 报告为空 + 一行日志；65534 照常上报） |
| m-2 | route-B 文档检查同时比 `<uid>` 组件（`path.parent.parent.name == str(uid)`），并新增"同一名字但属于别的 uid 目录的陈旧副本"用例；退回只比叶即红 |
| m-3 | 删掉 `envd_service/http/health.py` 与 `envd_service/runtime/registry.py` 里各重复一遍的注释段落 |
| m-4 | `control_plane/api/sandboxes.py:1307`（`_provision_local` 仍在问 `active_helpers()`）写进 `docs/c3-privilege-relocation.md` 新增的 **§11.2.1**，与其它 `local://` 例外并列 |
| m-5 | N5（拆箱"已缺席"判据与执行不同路径）与 N6（`remove-checkpoint` 无缺席分支）从"只有 docstring/报告"提升到设计文档 §11.2.1 的已知缺口清单（连同 I-3 的接回后续项） |

## 契约 lane 的两处连带修复（本轮新跑出来的）

1. **root worker 不再报 0**（见 m-1 worker 侧）：否则容器 lane 里任何"worker 以 root 加入"的契约用例都会红
   （实测：`test_orphan_tree_gc` 4 条、`test_checkpoint_status_api` 2 条，且"把本片 34 个文件换回 `e7e1dbd`"的对照
   全绿 ⇒ 确为本片引入）。
2. **`test_teardown_failure_semantics` 的"helper 调用只有一次"pin 与 A5 冲突**：A5 之后配对目录也走同一条确认路径，
   所以成功的 local 拆箱会有**两次** `remove_tree` 调用。该用例改为精确断言两次调用（树 + `sandbox_runtime_dir(...)`），
   并在注释里指向 §13.7 与探针；拒绝那一条仍是单次（树先抛，配对分支到不了）。

## RED → GREEN（第三轮）

| 判据 | 退回方式 | RED | GREEN |
|---|---|---|---|
| I-1 | agent 分支移回 mode pass 之前 | `AssertionError: assert 493 == 504` | 36 passed（`test_c3_fileops_worker.py`） |
| I-2 | import 的清理改回内联 | `assert <_MainThread(...)> is not <_MainThread(...)>` | 10 passed（`test_c3_fileop_degradation.py`） |
| I-3 | `return 0 if size is None …` | `assert 0 is None`（测量）/ 既有 pin 同样红 | 同上 + `test_platform_disk.py` 全绿 |

最终跑：

```
# 宿主（本片 6 个用例文件 + 相关既有 17 个 unit/contract 文件）
$ .venv/bin/python -m pytest <24 个文件> -q
492 passed, 7 skipped        # skip 均为既有环境项（chown 需 root / fakeredis）

# 容器 lane（Linux，真门禁）
$ docker run --rm --security-opt seccomp=unconfined -e E2B_REQUIRE_SECCOMP_FILTER=0 \
    -v "$PWD":/workspace -w /workspace e2b-sandlock-test:latest \
    python3 -m pytest tests/unit tests/contract/test_teardown_failure_semantics.py \
      tests/contract/test_orphan_tree_gc.py tests/contract/test_checkpoint_status_api.py \
      tests/contract/test_volumes.py tests/contract/test_uid_permissions.py -q
17 failed, 2066 passed, 17 skipped
```

容器里这 17 条与既有基线同类（容器内看不到 git → 2 个 deploy/docs 用例族；bash 渲染 → `test_c2_p0_probe`、
`test_migrate_state_base_script`；root 无 `CAP_SYS_PTRACE` 导致的多余告警 → `test_xfs_project_quota_agent`、
`test_quota_agent_client`），**本片用例零失败**，被点名的 5 个契约文件全绿。

---

# 附录 D：第四轮评审修复（2026-09-29，Need fixes → ①/② + 6 minors）

## ① agent 把特权 exec 跑在了自己的事件循环上（一行回归）

`agent_op` 为了 `await request.json()` 从 `def` 变成 `async def`，于是**同步**的 `as_uid`/`e2b-maint`
子进程调用（5 s / 最长 300 s）从 anyio 线程池**搬到了 uvicorn 的唯一事件循环**上：一次拆树或 walk 期间
agent 不再接受连接，并发的槽位授权会撞上 CP 的 5 s 期限变成 504，而 `E2B_C3_AGENT_MAX_CONCURRENCY`
的取值理由（`control_plane/config.py`："agent 是同步 FastAPI 服务，handler 跑在线程池"）也随之失真。

**修法**：两个 op 都在 `await asyncio.to_thread(...)` 里执行（`grant-slot` 一并覆盖）。CP 侧那段
推理因此重新成立。

**用例**：`test_two_instructions_do_not_serialize_on_the_event_loop` —— 两个并发指令（`rm` + `walk`）
配一个"慢"的 maint runner（0.2 s），断言两次 exec 的**时间区间重叠**（后一个在前一个结束前开始）。
退回内联即红：`assert 2614122.940656807 < 2614122.938546193`。

## ② `--worker` 身份此前是 worker 自报（D21 选型：**选项 1**）

### 结论与理由

选了 **D21 的选项 1**（用可信来源校验上报值；没有可信来源的形状就**不记身份**、由 op 点名拒），理由：

1. 它是裁定的首选形状，且是控制面**单侧**的改动（不新增 agent 面、不改 `maint.c` 语义）；
2. 可信值来自**部署事实**（k8s 里 worker pod 自己的 `securityContext.runAsUser/runAsGroup`，CP 本来就有
   `get,list pods` 的 RBAC 与那个 pod 的读取路径），而不是从内核反推——"agent 自己 exec 的 `--worker`
   到底指谁"这件事，语义上就该由部署定；
3. C1 里这个值来自 `SO_PEERCRED`，本片要守住的红线就是"**worker 不能命名它自己特权动作的身份**"——
   选项 1 把这条红线恢复成"上报只是声明，权威在部署"。

### 实现

- 新模块 `control_plane/worker_identity_source.py`：
  `WorkerIdentitySource` 协议 + `NoWorkerIdentitySource`（无可信来源的形状）/ `StaticWorkerIdentitySource`
  （测试、嵌入方）/ `K8sWorkerIdentitySource`（读 worker pod 的 pod 级 `securityContext`，退化到唯一容器的
  那一份；**没有 pin / 读不到 / 读坏了都返回 `None`**，绝不退回上报值）；`build_worker_identity_source(settings)`
  跟随 `E2B_NODE_ADDRESS_MODE`（与 node/agent 寻址同一个开关）。
- `create_app(..., worker_identity_source=…)` 注入（测试/嵌入方），默认按 settings 构建。
- `control_plane/api/internal.py`：register 与 heartbeat 都对上报值走
  `_verified_worker_identity(request, node_id, claimed)`——**校验通过才存**；不匹配或无来源一律
  **存空**并打具名 warning（节点照常 join，需要身份的 op 由各自的具名 503 拒）。
- `deploy/c3_agent/fileops.py` 的模块注释改正：不再声称"请求无法伪造"，改为写明"这里的值是 CP 用可信来源
  校验过的部署事实，或该 op 根本到不了这里"。

### ⚠ 对 slice B 的两条硬约束（必须一起做，否则 agent 形状的文件操作全是 503）

1. **k8s**：可信来源是 worker pod 的 `securityContext`，而**现在 `deploy/k8s/worker.yaml` 没有 pin**
   （一直靠镜像 `USER 65534:65534`）。所以 slice B 必须在 worker 容器上显式写
   `securityContext.runAsUser: 65534` + `runAsGroup: 65534`（以及 pod 级，若要覆盖 init 容器），
   否则 CP 记不到身份、文件操作按设计 fail closed。**这是"开关打开前必须先落地"的一项。**
2. **compose（含 `deploy/stack/docker-compose.prod.yml`，D17 在范围内）**：compose 文件 CP 看不到，
   今天**没有**可信来源 ⇒ 该车道同样 fail closed。slice B 要么给 face B 加 `pid: host`
   （`c3-agent` 已有；`c3-agent-maint` 目前**没有**），让 agent 走"选项 2：从内核读 worker 进程的
   uid/gid"——推荐，因为它同时覆盖 k8s 与 compose；要么显式接受 compose 车道没有文件操作。

### 用例（RED → GREEN）

- `test_a_worker_claiming_another_identity_gets_nothing_stored`：部署 pin `65534:65534`，worker 先正常
  注册、再在**心跳里**改报 `10007:10007` ⇒ 记录仍是 65534；另起一个 app 直接以 `10007` 注册 ⇒
  记录为空，`chown-workspace` 503 点名且 **agent 从未被拨号**（`agent2.calls == []`）。
- `test_a_shape_without_a_trusted_source_records_no_identity`：`NoWorkerIdentitySource` ⇒ 注册成功但无身份，
  `remove-workspace` 503 点名、agent 未被拨号。
- `test_the_k8s_source_reads_the_pods_own_security_context`、`…_reads_a_container_level_pin_too`、
  `test_a_pod_that_pins_no_identity_has_no_trusted_answer`（含"只有半条 pin"）、
  `test_an_api_that_cannot_answer_is_not_a_trusted_answer`（404 / 连不上 / 非 JSON）、
  `test_a_hostile_node_id_never_reaches_the_api`。
- **RED**：把校验退回"信任上报值"⇒ 上面两条主用例红：
  `AssertionError: the forged claim must never replace a verified identity`、
  `assert (65534, 65534) == (None, None)`。

## minors

| # | 处理 |
|---|---|
| walk 的 `o` | `WALK_KINDS` 加上 `o`（fifo/socket/设备节点）：`maint.c:walk_kind` 会发它，旧的 `priv_helpers` parser 也接受；只有 `d/f/l` 会让"树里有 socket"的整条 walk 被拒（`/metrics` 与按沙箱记账在 agent 形状下降级）。用例的 walk fixture 现在含一行 `o` |
| `control_paths` 的同步 `volumes.list()` | 接受不了就搬走：file-op 端点里把 `control_paths` + `derive` 放进 `await asyncio.to_thread(...)`（每次文件操作一次 store glob + 逐候选读，属 I/O，不该在循环上） |
| checkpoint / secret 的 gid | **对齐**：`chown-checkpoint` 与 `chown-secret` 现在派生 `<uid>:<uid>`（与 pre-C3 的 `os.chown(path, uid, uid)` / `_hand_to_sandbox` 的 root 分支一致）；只有**工作区树**保留 `gid=<worker gid>`——那个组位才是 worker 作为数据面属主能写树的原因 |
| `platform_disk.py:120-123`/`:150-153` 的残留静默 0 | **记录**（`docs/c3-privilege-relocation.md` §11.2.1 第 5 条）：`directory_cost(runtime_dir)` 失败被吞、非目录子项 `entry_size` 失败 `continue` —— 分量级、同性质，留待后续 |
| `agent.py:1556`/`:1577` 两处 `rmtree(ignore_errors=True)` | **记录**（§11.2.1 第 6 条）：与 A5 同形，但都是 worker 自己的目录、不在特权面，且是既有行为 |
| 清扫表里 `agent_export_sandbox` / import 的 write+extract | **改表**：它们确实做内联文件层重活（`tar.add(workspace)`、`write_bytes` + `_extract_sandbox_archive`），只是不触达 agent 层、也不是本片引入；表里已改成"不动，但不是这一类已清干净的证明"，并记入 §11.2.1 第 7 条 |

## 第四轮 RED → GREEN 汇总

| 判据 | 退回方式 | RED | GREEN |
|---|---|---|---|
| ① | 两个 op 改回内联 | `assert … < …`（两次 exec 不重叠） | 15 passed（`test_c3_agent_fileops.py`） |
| ② | `_verified_worker_identity` 改回"信任上报" | `the forged claim must never replace a verified identity` / `assert (65534, 65534) == (None, None)` | 39 passed（`test_c3_fileops_forwarding.py`） |

最终跑：

```
# 宿主（本片 6 个用例文件 + 相关既有 unit/contract 共 21 个文件）
$ .venv/bin/python -m pytest <21 个文件> -q
442 passed, 3 skipped        # skip 均为既有环境项（fork 子模块 / fakeredis）

# 容器 lane（Linux，真门禁）
$ docker run --rm --security-opt seccomp=unconfined -e E2B_REQUIRE_SECCOMP_FILTER=0 \
    -v "$PWD":/workspace -w /workspace e2b-sandlock-test:latest \
    python3 -m pytest tests/unit tests/contract/{test_teardown_failure_semantics,test_orphan_tree_gc,\
      test_checkpoint_status_api,test_volumes,test_uid_permissions}.py -q
17 failed, 2074 passed, 17 skipped     # 17 条与既有基线同类（无 git / bash 渲染 / root 无 CAP_SYS_PTRACE）
```

## 附录 D 补记：第五轮评审的两条 non-blocking minor（已修）

1. **心跳级告警噪声**：`_verified_worker_identity` 原先每 5 秒每次 register/heartbeat 都打一条 warning，而且
   把"这个 pod 什么都没 pin"写成"本形态没有可信来源"。现在：
   - 复用本文件既有的**once-per-node** 纪律（新增 `_unverified_identity_reported: set[(node, reason)]`，
     与 `_unresolvable_nodes_reported` 同形）；身份一旦**校验通过**就把该节点的两种原因从集合里清掉，
     这样以后真的回归还会再报一次；
   - **两条消息分开**：`no-source`（"this shape has no trusted source … only the k8s lane can verify a report"）
     与 `no-pin`（"its pod spec pins no runAsUser/runAsGroup … pin both in the worker manifest"），
     外加原有的 `mismatch`；为此给 `WorkerIdentitySource` 协议加了 `configured: bool`
     （`NoWorkerIdentitySource.configured = False`，k8s/静态来源为 `True`）。
   - 用例：`test_an_unverifiable_identity_is_reported_once_per_node`（一次注册 + 两次心跳 ⇒ **只**一条，且是
     `no-pin` 文案）、`test_a_shape_with_no_source_at_all_says_that_instead`（同样只一条，`no-source` 文案）、
     `test_a_verified_identity_logs_nothing`。退回"每次都打"即红（列表从 1 条变 3 条）。
2. **§11.2.1 补上 slice B 必须满足的两条身份来源约束**（第 8、9 条）：k8s 的 worker pod 必须显式 pin
   `runAsUser`/`runAsGroup`（现在的 `worker.yaml` 靠镜像 `USER`，pod spec 里没有值 ⇒ CP 记不到身份 ⇒
  每个需要身份的 op 具名 503）；compose（含 `deploy/stack/…`，D17）今天没有可信来源 —— 推荐给
  `c3-agent-maint` 加 `pid: host` 后走 D21 选项 2（从内核读 worker 进程 uid/gid，同时覆盖 k8s），
  或显式裁定该形态不提供文件操作。

## 附录 E 补记：D21 选项 2 落地 —— compose 车道的身份来自内核（Task 4 片 C）

片 A 选了**选项 1**（可信来源校验上报值），并给片 B 留了两条硬约束：k8s pin worker 身份（已落）、
compose 没有可信来源 ⇒ 当时那三个车道**连建箱都完不成**（属主交棒是建箱的第一个特权步骤，
`chown-workspace` 在 CP 侧先要节点记录里的 `worker_uid/gid` ⇒ 具名 503）。片 C 把**选项 2**
接上，本报告记录的是它和片 A 那份身份工作的关系：

1. **CP 侧不再"记不到身份"**：新增 `KernelWorkerIdentitySource`（`hostname` 形状即 compose 走它，
   `configured=True`、`kernel_verified=True`，`identity_for()` 返回 `None` —— 这个形态不在 CP
   侧作答）。`_verified_worker_identity` 认出 `kernel_verified` 后把上报值记为**待内核确认的声明**
   并落库；`node_file_op` 通过 `_worker_identity_anchor` 取**自己记录里的** `pid_namespace` 作为锚点，
   随每条"以 worker 身份执行"的指令下发（`C3AgentClient.chown/rm/walk` 的
   `worker_pid_namespace` → 指令体的 `worker.pid_namespace` + `worker.node_id`）。k8s 车道不带锚点，
   报文与之前逐字相同。
2. **agent 侧按锚点读内核**：`deploy/c3_agent/lookup.py` 新增
   `ProcLookup.worker_uid_gid(identity, *, claimed, reader)`（与槽位反查同一套 `/proc` 纪律与
   `LookupRefusal` 词汇）、`resolve-worker` 子命令入口、以及
   `SubprocessWorkerIdentityResolver`/`ProcWorkerIdentityResolver`。face B 的 `FileOpBody.worker`
   多两个可选字段（`node_id`/`pid_namespace`）；锚点存在时先解析、再 exec `e2b-maint`，声明与内核
   不一致 / 锚点无进程 / 锚点多进程一律**具名 502 且不 exec**。
3. **一个必须记住的内核约束**：`/proc/<pid>/ns/pid` 的 `readlink` 走 `ptrace_may_access`，只有同
   uid（或 `CAP_SYS_PTRACE`）能读。face B 是 root 且**刻意没有** `CAP_SYS_PTRACE`（它与控制面共享
   `pid: host`；给了它就能 ptrace 控制面 —— 那比本条要收的口子更糟），worker 又是 65534 ⇒
   **face B 自己读不到**。所以读取放在 face B 的子进程里、以
   `E2B_C3_AGENT_RESOLVER_UID`/`_GID`（默认 65534:65534，= 三个 compose 栈 worker 的
   `user:`）运行。该旋钮配错 ⇒ 每条 op 具名拒绝，绝不退回"信任上报值"。
4. **残留（已记入 §11.2.1 第 9 条）**：compose 的锚点仍是 worker 自报；同机同 uid 的 worker
   可互读 `ns/pid`，故被攻陷的 worker 理论上能冒充同一 uid 的另一个 worker 的名字空间（k8s 有
   `pod<uid>` 的 cgroup 证）。要收口需补一条 compose 侧的 cgroup/hostname token。

片 C 的报告与 RED→GREEN 见 `.superpowers/sdd/task-4c-report.md`。
