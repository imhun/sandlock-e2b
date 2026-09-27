# checkpoint/restore 产品化实施计划

> **执行状态（2026-09-27 更新）**：**Task 1 / 2 / F2 / E2 / E3 / E4 / E8 已完成并上线**（`E2B_PAUSE_CHECKPOINT=1` 写在 `deploy/k8s/worker.yaml`，集群端到端验收全绿）；条件任务 **F3/F4 的决定门都落在"不做"**（复核 0 命中）。
> **仍有效的决定**：恢复**进会话**（保留 `exec`）；平台账是**软账**（允许并发短超）。
> **已作废的假设**：正文早期"恢复后不能 exec""restore stub 与 chroot 根不兼容"两条 —— 均已被 fork 取代，`2026-09-26-decisions.md`《D9 已关闭》已两次更正。**仍未做/未定**：`E2B_PAUSED_TTL_S`（paused 过期策略）**待拍板**、今天无实现。证据：`docs/checkpoint-restore-e2b-half.md` §6(k) 与 `docs/reports/checkpoint-e5-e8-audit-report.md`。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把已经能用、集群验收全绿（2026-09-25 的 `0.1.0-525` / `0.1.0-527` 两轮）的 checkpoint/restore 变成**有对外语义、有可观测性、有回收路径、有守卫**的产品功能——`pause` 写下的进程镜像能在 worker 重启后回来、能继续 `exec`、能被用户看见、不会永远占着平台账。

**Architecture:** 不改传送机制，只补齐四件缺的东西：①把"生产形态到底能不能用"这个第一道题落到**引擎侧的交接点**上——restore stub 与「根形态 × 会话恢复」的兼容，用本机用例钉死（Task 1，fork 侧排在最前面的改动）；②用**生产形态的集群验收**回答"今天到底能不能用"，并把那条判据从 `tmp/` 转正进仓库（Task 2，E2B 侧，不依赖任何代码改动）；③补齐可见性（`exe`/`argv`，Task F2）与 E2B 侧的**只读查询端点**、**孤儿图回收**、**paused 的过期策略**（Task E2–E7）；④把文档、清单注释与守卫用例的定位收口（Task E8）。**"恢复后能不能 exec"不是待决项**：D9 已在 2026-09-25 关闭（裁定见 `2026-09-26-decisions.md`《D9 已关闭》）——E2B 走的 `restore` verb 把镜像恢复**进会话**，会话继续服务 `exec`；按名拒绝 exec 的只有 OCI / `--restore-from` 那条 E2B 不消费的路。fork 侧的改动必须先重建 wheel，E2B 才会用上（见"执行顺序"）。

**Tech Stack:** Python 3.12（FastAPI：`envd_service` worker + `control_plane` 控制面）、Rust（fork `third_party/sandlock`：`sandlock-core` / `sandlock-supervise`）、Redis（多副本控制面状态）、k0s（2 节点 arm64）、pytest（单测/契约/SDK）、cargo test（fork 相位门禁）、E2B Python SDK（集群验收）。

## Global Constraints

- **fork 是 git submodule**：`third_party/sandlock` 的改动要在 fork 仓**单独提交**（`git -C third_party/sandlock commit`），主仓只更新 submodule 指针；fork 改动**必须先重建 wheel**（`deploy/scripts/build-sandlock-wheels.sh`）才能被 E2B 用上。
- **fork 门禁的入口**：`third_party/sandlock/scripts/test-all.sh`（规范镜像 `sandlock-dev:latest`，以 uid 65534 跑）；本机的等价入口是 `IMAGE=sandlock-dev-f17:latest deploy/scripts/fork-gate.sh`，单族复跑用 `deploy/scripts/fork-gate.sh --one 'test_restore::'`。
- **改了 fork 的用例条数，必须同步 `third_party/sandlock/docs/test-baseline.md` 的计数**（`test-all.sh` 对"套件悄悄少跑/多跑"判红）；当前值：`core_lib = 913`、`core_integ = 560`、`supervise = 55`、`oci = 157`、`ffi = 104`、`cli = 98`、`python = 465`。
- **`test-all.sh` 的 `run()` 用匿名管道跑每条套件**（N34）：checkpoint/restore 的用例在**直接跑二进制且 stdio 是普通文件**时会确定性红（`restore skipped fds` 只列 `fd 0`，恢复出的进程立刻以 `Code(10)` 退出）；复跑一律走门禁入口，不要 `cargo test > file 2>&1`。
- **本机 pytest lane**：`tmp/testenv/bin/python -m pytest`（`.venv` 缺 `fakeredis`，contract 相位会红）；**集群验收脚本**反过来用 `.venv/bin/python`（`e2b` SDK 只在 `.venv` 里，`testenv` 没有）。
- **容器 lane**：`tmp/k0s/gateA-full.sh <log>`（镜像形态）/ `tmp/k0s/gateB-full.sh <log>`（pure 形态）；本机对照基线 gate A **1772 passed / 6 skipped / 3 xfailed / 0 failed**、gate B **1765 passed / 13 skipped / 3 xfailed / 0 failed**。
- **临时文件一律放项目内 `tmp/`**（不用系统 `/tmp`、不用 `$TMPDIR`）；本计划里出现的探针脚本、日志都在 `tmp/` 下。
- **断言必须精确匹配**：新增/改动的断言禁用 `toContain` / `includes` / `assertIn` / 子串判据；比较用 `==`（既有用例里那种"整句日志文本相等"是本仓的写法）。
- **`tests/unit/test_checkpoint_restore_unused.py` 当前钉着"envd 不碰这套 API"**（`FORBIDDEN = (".checkpoint(", "restore_interactive", ".restore_skipped(")`，扫 `envd_service/**/*.py`）——它是**形状守卫**，不是"做不了"的声明；本计划新增的行为级验收会与它并存，Task E8 必须把它的 docstring 改到与事实一致。
- **`exec` 与恢复：引擎有两条路，E2B 走的是能 exec 的那条（D9 已关闭，2026-09-25）**——OCI / `--restore-from`（`crates/sandlock-oci/src/supervisor.rs:1382-1390`、`crates/sandlock-supervise/src/serve.rs:1473-1487`）按名拒绝 `exec`，因为 exec 靠 `sandlock-init` 转发，而"从镜像起一个 generation"没有 init；**恢复进会话**（`crates/sandlock-core/src/instance.rs:1229`，fork `1f41f1a`）保留 `exec` / `wait_child` / `kill_child` 与孩子表，**E2B 走这条**（`envd_service/route_b.py::restore_checkpoint` 的 docstring 自己写着 "resume the image in `dir` into its own session… a **session** is what serves `exec`"）。裁定记录是 `2026-09-26-decisions.md` 的《D9 已关闭》一节，**比本计划原文权威**：凡是把"恢复后不能 exec"当拦路虎/待拍板/第一道题的写法都是过期内容。
- **`restore` 的 stub 是宿主构建产物，靠描述符投递、靠启动时的 Landlock grant**：这是 Task 1 的全部内容。fd 投递由 fork `a6f6b04` 落地（`crates/sandlock-core/src/sandbox.rs:1443-1463`），它**取代了** `43cc62a` 的前置拒绝（"任何 chroot 根都拒绝恢复"），后者的历史留在 `sandbox.rs:1419-1431` 的注释里；会话那条路的 grant 在**会话启动时**装一次（`instance.rs:577-584`），因为 Landlock 域安装后不能再加规则。
- **需求已确认（用户裁定）：checkpoint/restore 是真实需求，按产品功能推进**；恢复后**必须**支持 `exec`，生产形态**必须**支持。裁定记录是 `2026-09-26-decisions.md` 的裁定表第 6 行（`:13`）——"**要支持**，生产形态**必须**支持。**且这不是待做项——已经支持了**"。据此，`docs/checkpoint-restore-e2b-half.md` §0 里那句"仓库里没有任何'用户要这个'的记录 / 需求仍未确认"是**过期**的（同节的 exec 结论已在 2026-09-26 就地更正，需求这一句没有）。
- **判断"线上跑的是哪一版"只认两处**：`deploy/stack/.version`（当前 `0.1.0-535-g2f38991-20260926-090454`）与集群里 `control-plane` / `autoscaler` / `e2b-worker` 三个工作负载的实际镜像；`docs/deploy-clusters.md` 里写死的版本号只是历史记录。
- **动集群前先认集群**：`deploy/scripts/open-cluster-tunnel.sh`（自检 2 节点 / arm64 / 含 `+k0s`），然后 `export KUBECONFIG="$PWD/tmp/k0s/kubeconfig"`；不带 `KUBECONFIG` 的 `kubectl` 会安静地指向另一套阿里云 ACK 集群。
- **`pause` / `resume` 推送的 204 契约不许动**：控制面把 worker 的**非 204/404** 一律当 502 回滚（`control_plane/api/sandboxes.py:333-338`，状态映射见 `:250-262`）；checkpoint/restore 的一切"没有图/没有会话/账满/引擎拒绝"都必须是**正常答案**（带 reason），不得改状态码。
- **承重顺序不许动**：`pause` 必须在**冻结之前**捕获（引擎的捕获自己会 `SIGSTOP`→`SIGCONT`，先冻再捕获等于把 pause 撤销）；`resume` 必须在**发布状态之前**恢复。

---

## 执行顺序与编号

- **`Task 1` / `Task 2` 是本轮的头两个任务**，它们合起来回答"D9 关闭之后，生产形态到底还差什么"：
  - `Task 1`（fork 侧）：restore stub 与「根形态 × 会话恢复」这个交接点，用本机用例钉死；**fork 侧排在最前面的改动**。
  - `Task 2`（E2B 侧）：把生产形态的集群验收转正进仓库并跑出判据；**不依赖任何代码改动**。
- **fork 侧任务**：`Task 1`、`Task F2`–`Task F4`（改动落在 `third_party/sandlock`，单独提交，跑 fork 门禁）。
- **E2B 侧任务**：`Task 2`、`Task E2`–`Task E8`（改动落在主仓，跑 pytest + 容器 lane）。
- **顺序**：
  1. **`Task 1` 先做**：它是生产形态在**引擎侧**的那一半判据（stub × 真根/模拟根 × 会话恢复 × 恢复后 exec），也是后面所有 fork 改动的基线。它不改 Python，不依赖部署。
  2. **`Task 2` 紧随其后**：它跑的是**当前部署那一版**（今天 `deploy/stack/.version` = `0.1.0-535-g2f38991-20260926-090454`），所以它**在 Task 1 之前跑也是合法的**——两者互不阻塞，只是都排在前面：Task 1 决定"引擎这条交接点还成不成立"，Task 2 决定"今天线上到底能不能用"。若 Task 2 是在 Task 1 的 wheel 被部署上去之后复跑，它就同时覆盖新 wheel。
  3. `Task F2` 是 fork 侧的下一个改动（`exe`/`argv` 透传）；**每个 fork 任务结束后跑一次 `deploy/scripts/build-sandlock-wheels.sh`**，否则 E2B 侧看到的还是旧引擎。
  4. `Task F3` / `Task F4` 是**条件任务**（各自的第一个 step 是决定门），不满足条件就不做。
  5. 然后 `Task E2` → `Task E8`（E2 依赖 F2 的 wheel，E7 的端到端验收依赖 E2/E3/E4 全部落地）。
- **依赖图**（谁需要谁先落地）：

| 任务 | 依赖 | 被谁依赖 |
|---|---|---|
| Task 1 | 无 | Task 2 红在 `resumed` / `exec_after_resume` 时的定因输入；所有 fork 改动的基线 |
| Task 2 | 无（跑当前部署那一版；无需新 wheel） | 所有 E2B 任务的基线 |
| F2 | 无 | E2（`exe`/`argv` 要透传到 worker） |
| F3 / F4 | 决定门 | 无（可选增强） |
| E2 | F2 的 wheel | E7 的只读查询指标 |
| E3 | 无 | E7 |
| E4 | 无 | E8（文档要写"孤儿会回收"） |
| E5 | 无 | E8 |
| E6 | 拍板 | E8 |
| E7 | E2 + E3 | E8 |
| E8 | 全部 | — |

---

## 验收矩阵（改了什么 → 用哪条验收回答）

### 引擎侧（fork，`third_party/sandlock`）

| 档位 | 命令 | 期望 |
|---|---|---|
| Task 1 的交接点（**stub × 根形态 × 会话 × exec**） | `deploy/scripts/fork-gate.sh --one 'test_restore::'`、`--one 'test_a_dynamic_workload_resumes_into_a_session_under_a_real_root'`、`--one 'test_a_static_workload_resumes_into_a_session_under_an_emulated_chroot'`、`--one 'test_a_session_launched_without_the_stub_grant_refuses_a_restore_by_name'` | 每条 `0 failed`；`test_restore::` = 5 条（实测），后三条各 1 条 |
| `core_integ`（会话恢复两态 + 恢复后 exec） | `deploy/scripts/fork-gate.sh --one 'test_instance'` | `0 failed`；整档基线 `core_integ = 560`（2026-09-26 本机实测），Task 1 之后 `562` |
| `supervise`（`checkpoint` / `restore` verb） | `deploy/scripts/fork-gate.sh --one 'test_supervise_checkpoint'` / `--one 'test_supervise_restore'` | 4 条相关用例 `0 failed`（`supervise = 55` 不失配） |
| 全档（提交前） | `IMAGE=sandlock-dev-f17:latest deploy/scripts/fork-gate.sh` | 每个相位 `passed -- baseline says N`；无 `suite FAILED` |
| 计数守卫 | `grep -n '^core_integ = ' third_party/sandlock/docs/test-baseline.md` | 加了用例就等于新值（Task 1：`560` → `562`，+2），否则门禁判红 |

### E2B 侧（主仓）

| 档位 | 命令 | 期望 |
|---|---|---|
| 单测（checkpoint 家族） | `tmp/testenv/bin/python -m pytest tests/unit/test_checkpoint_store.py tests/unit/test_agent_checkpoint_restore.py tests/unit/test_sandlock_executor_route_b.py -q` | 全绿（现基线：18 + 11 + 37 条，2026-09-26 实测） |
| 契约（生命周期） | `tmp/testenv/bin/python -m pytest tests/contract/test_pause_write_gating.py tests/contract/test_pause_resume_sandlock_multinode.py -q` | 全绿 |
| 新增契约（只读查询） | `tmp/testenv/bin/python -m pytest tests/contract/test_checkpoint_status_api.py -q` | 全绿（E3 新建） |
| 容器 lane（镜像形态） | `tmp/k0s/gateA-full.sh tmp/k0s/e-plan-gateA.log` | **1772 passed / 6 skipped / 3 xfailed / 0 failed**（只加测试时同步上浮） |
| 容器 lane（pure 形态） | `tmp/k0s/gateB-full.sh tmp/k0s/e-plan-gateB.log` | **1765 passed / 13 skipped / 3 xfailed / 0 failed** |

### 集群侧（生产形态：image-rootfs + `E2B_REAL_ROOT=1`）

| 档位 | 命令 | 期望 |
|---|---|---|
| 通道与身份 | `deploy/scripts/open-cluster-tunnel.sh --check` | `✓ 2 节点 / arm64 / 含 +k0s` |
| 版本对齐 | `KUBECONFIG=$PWD/tmp/k0s/kubeconfig kubectl -n sandlock get deploy,sts -o jsonpath='{..image}' \| tr ' ' '\n' \| sort -u` | 只有 `…:$(cat deploy/stack/.version)` 一族 |
| 生产形状验收（Task 2 转正后） | `KUBECONFIG=$PWD/tmp/k0s/kubeconfig .venv/bin/python deploy/scripts/checkpoint_acceptance.py` | 逐步 JSON，末行 `{"step": "OK"}`，退出码 `0` |
| 其中四条硬判据 | 同上输出里的这四行 | `{"step":"image"…}` 含 `meta.json`；`{"step":"resumed","before":N,"after":M}` 且 `M-N < 30`；`{"step":"exec_after_resume","stdout":"EXEC_OK\n"}`；`{"step":"image_consumed"…}` 含 `No such file or directory` |

---

## 必须先拍板的决策点（每一行都要人给答案）

| # | 决策 | 计划里的默认（不拍板就按这个走） | 落在哪 |
|---|---|---|---|
| 1 | ~~D9 是否按"恢复进会话"封板~~ **已关闭**（2026-09-25）——剩下的同类问题是：OCI / `--restore-from` 那条**另一条路**要不要也支持 exec | 不投人：E2B 从不走 `--restore-from`（`route_b.py` 只发 `checkpoint` / `restore` 两个 verb），等出现真实消费者再评估 | Task F4（决定门，不命中就不做）/ Task 2 Step 3 ③（判据里写明两条路的差别） |
| 2 | **恢复后进程的 stdout 是否接到平台日志**（今天进 `/dev/null`） | 不接，只**写进对外语义**（"恢复的沙箱日志消失"） | Task E8 |
| 3 | **平台账接受"软账 + 并发可超"，还是做跨节点硬账** | 接受软账：保持"整个 `_runtime`"口径 + 把 `used/budget` 暴露 + 告警 | Task E7（暴露数字）/ Task E8（写清口径） |
| 4 | **paused 是否要有 TTL 以及多久**（会摧毁用户状态） | `E2B_PAUSED_TTL_S` 默认 **0 = 不启用**；只在拍板后打开 | Task E6 |
| 5 | 是否新增**公开端点** `GET /sandboxes/{id}/checkpoint`（API 契约扩张，要和 SDK 对齐） | 新增（只读、不碰 204 契约） | Task E3 |
| 6 | 是否把 `tmp/k0s/checkpoint_acceptance.py` **转正进仓库** | 转正（它现在是唯一的生产形态验收，却躺在被 `.gitignore` 忽略的 `tmp/` 里） | Task 2 |
| 7 | `E2B_PAUSE_CHECKPOINT` 是否长期默认开（它让 `pause` 变成"写整个进程内存"的动作） | 保持清单里 `"1"`，并把代价写进文档 | Task E8 |

---

### Task 1: 生产形态的第一道题 —— restore stub 与「根形态 × 会话恢复」这个交接点（fork）

**为什么它是第一道题**：生产形态是 image-rootfs + `E2B_REAL_ROOT=1`，而 restore stub 是
**平台在宿主上的构建产物**、不在 rootfs 里；它能不能被 exec，决定这条能力在生产上是否可用。
这里**历史上确实撞过墙**：fork `43cc62a`（2026-09-23）在 `Sandbox::restore_interactive` 里
**前置拒绝**了任何 chroot 根，理由是 stub 按**宿主路径** exec、chroot 根解析不到它
（实测 `execvp '<stub>': No such file or directory`，随后 10 s READY 超时）。
**这条已经在引擎里解掉了**，但解它的那次改动只在"一次性恢复"那条路上被用例钉住，
而 **E2B 走的是会话那条**。所以本任务不是"修一个拒绝"，而是**把这条交接点在会话路径上钉死**
（+ 把唯一一处静默失败说清楚），让它一旦回归，本机就红，不必等到集群。

**已核实的事实（2026-09-26，本机 lane；证据 `tmp/k0s/plan-rewrite-*.log`）**：

- stub 不再按宿主路径 exec：`Sandbox::restore_interactive` 由 fork `a6f6b04` 改成
  "按描述符投递 + `execveat(AT_EMPTY_PATH)`"，`43cc62a` 的拒绝被同一次改动取代
  （`crates/sandlock-core/src/sandbox.rs:1443-1463`；那段历史留在 `:1419-1431` 的注释里）。
- 实测 `IMAGE=sandlock-dev-f17:latest deploy/scripts/fork-gate.sh --one 'test_restore::'`
  = **5 passed / 0 failed**，其中 `test_restore_resumes_inside_a_chroot_root`
  （`crates/sandlock-core/tests/integration/test_restore.rs:192`）把**模拟根与真根两态**都跑过。
- 会话路径（`SandboxInstance::restore_into_session`，`crates/sandlock-core/src/instance.rs:1229`）
  从 fork `1f41f1a` 起就是同一个 fd 投递（`:1283-1315`），grant 在**会话启动时**装一次
  （`:577-584`）。实测 `--one 'test_a_dynamic_workload_resumes_into_a_session_under_a_real_root'`
  = **1 passed / 0 failed**。
- ⇒ **今天没有任何引擎代码拒绝"stub × image-rootfs × `E2B_REAL_ROOT=1`"**，生产形态的复跑
  由 Task 2 回答。本任务处理的是下面的三条缺口。

**还缺的三件（本任务的三个交付物）**：

1. **会话恢复 × 模拟根**（`real_root(false)`）**没有任何用例**：生产形态的根由
   `E2B_REAL_ROOT` 决定，"真根绿"推不到"模拟根绿"（一次性路径那条用例偏偏两态都跑，
   这两条路的差别正是 chroot 那一族）。
2. **会话恢复 × 真根**有用例，但**只断言"计数器继续"，不断言恢复后还能 exec**
   （`crates/sandlock-core/tests/integration/test_instance_exec.rs:1147-1274`，fork `6367c26`）
   —— 而"恢复后继续服务 exec"是这个功能的产品语义（D9 的正题，见 Global Constraints）。
   另外那条无根用例（同文件 `:599`）是有 exec 断言的，但用的是 `base_policy()` ⇒ 与生产形态无关。
3. **stub 的 grant 缺失是静默的**：`instance.rs:577-584` 只在 `restore_stub.exists()` 时装
   grant，没装也不留痕；那样的会话在恢复时会以**沙箱内的 Landlock 拒绝**出现，而不是一句
   点名的话。grant 装不上是结构性的：域在启动时安装一次、之后不能加规则（同处注释）。

**候选修法与推荐**（第 3 件是本任务唯一的行为改动，先给候选；前两件只是补用例）：

| 候选 | 落点 | 代价 / 风险 | 推荐 |
|---|---|---|---|
| ① **fd 投递 + 启动时把 stub 那一个文件授予 grant**（引擎今天就是这条） | `crates/sandlock-core/src/sandbox.rs:1443-1463`、`crates/sandlock-core/src/instance.rs:1283-1315`、`:577-584` | 已付。代价是 grant 只能启动时装 ⇒ 会话与 stub 必须"同龄" | **就是答案**：本任务不改这条路，只把它在两态上钉死 |
| ② 把 stub 用一条 policy mount 带进 rootfs（`43cc62a` 的消息里写的"出路一"） | 部署侧的 `fs_readonly_host` / `fs_mount` + 每个部署跟着 wheel 版本维护 | 平台构建产物进**租户可读**的树（等于把 stub 的存在暴露给沙箱），且多一处版本耦合 | **不做**：①已经不需要它 |
| ③ 让 stub 落在**不含构建哈希**的固定路径（`SANDLOCK_RESTORE_STUB` 指到 wheel 里的 `sandlock/bin/restore-stub`，fork `2d5f2e9` 已经这么做） | `crates/sandlock-core/src/checkpoint/resume.rs:86-104` + 部署 env | 让"会话比 stub 活得久"（跨 wheel 升级）也能恢复 | 本轮**不做**：生产是"租槽位、再恢复"，会话与 stub 同龄；等出现真实投诉再说 |
| ④ **把"启动时没装上 grant"从静默变成一句点名的话** | `crates/sandlock-core/src/instance.rs:577-584`（记下装上的那个路径）与 `:1255-1258`（恢复前比对、不匹配就拒绝） | 小；只影响"stub 在会话启动之后才出现/换了路径"的形状 | **推荐**：resume 失败总要说得出原因，这是唯一会把原因藏起来的地方 |

**Files（fork 仓，`third_party/sandlock`）:**
- Modify: `crates/sandlock-core/tests/integration/test_instance_exec.rs:1147-1274`（扩展现有真根用例：加 `children_live` + exec-after-restore 断言）
- Modify: `crates/sandlock-core/tests/integration/test_instance_exec.rs`（同一个文件里新增两条用例，跟在真根用例之后）：
  `test_a_static_workload_resumes_into_a_session_under_an_emulated_chroot`（交付物 1）与
  `test_a_session_launched_without_the_stub_grant_refuses_a_restore_by_name`（交付物 3）
- Modify: `crates/sandlock-core/src/instance.rs`（交付物 3 / 候选④：`InstanceRuntime` 上记下装上的 stub 路径，`restore_into_session` 入口比对；落点见 Step 4）
- Modify: `crates/sandlock-core/src/checkpoint/resume.rs:86-104`（**只在 Step 4 分派到"stub 路径与启动时不同"时**才动，默认不动）
- Modify: `docs/test-baseline.md`（`core_integ = 560` → `562`，fork 仓）

**Interfaces:**
- Consumes: `SandboxInstance::launch_exec(policy, argv)`、`SandboxInstance::exec(argv, ExecStdio::Piped)`、`SandboxInstance::checkpoint_excluding_main()`、`SandboxInstance::restore_into_session(&cp)`、`SandboxInstance::kill_child(id, SIGKILL)`、`SandboxInstance::stats().children_live`、`SandboxInstance::wait_child`（全部来自 `crates/sandlock-core/src/instance.rs`）；`Sandbox::builder().chroot()/real_root()/user()/fs_mount()/fs_write()/cwd()`；环境变量 `SANDLOCK_RESTORE_STUB`（`crates/sandlock-core/src/checkpoint/resume.rs:86-104` 的第一优先级）；测试夹具 `tests/rootfs-helper`（静态、`build.rs` 编好，子命令 `clock-loop` / `echo`）
- Produces: 三条能在引擎层面判红的用例——生产形态的会话恢复（真根/模拟根）与"stub 的 grant 没装上"一旦回归，它们先红，而不是等到集群；`core_integ` 的新基线 `562`

- [ ] **Step 1: 先跑现状，把"今天到底哪条绿"留成证据（决定门）**

```bash
cd /Users/polus/project/ai/sandlock-e2b
IMAGE=sandlock-dev-f17:latest deploy/scripts/fork-gate.sh --one 'test_restore::' \
  | tee tmp/k0s/plan-task1-restore.log          # 期望：5 passed; 0 failed
IMAGE=sandlock-dev-f17:latest deploy/scripts/fork-gate.sh \
  --one 'test_a_dynamic_workload_resumes_into_a_session_under_a_real_root' \
  | tee tmp/k0s/plan-task1-realroot.log         # 期望：1 passed; 0 failed
rg -n 'emulated_chroot' third_party/sandlock/crates    # 期望：无输出（模拟根那一态没有用例）
```

Expected: 前两条绿 ⇒ **候选①是答案**，本任务不去改 fd 投递那条路；第三条无输出 ⇒ 交付物 1 成立。
三行输出贴进提交信息（本机 lane 的证据留在 `tmp/k0s/`）。

- [ ] **Step 2: 写会失败的测试（三条：真根补 exec 断言 / 模拟根新用例 / grant 缺失按名拒绝）**

（1）在 `test_a_dynamic_workload_resumes_into_a_session_under_a_real_root` 里，
`restore_into_session` 成功、并且计数器已经推进之后，插入下面这段（**用该文件已有的 API**，
见 `test_a_sessions_workload_is_captured_with_the_park_left_out:843-853` 的同款写法）：

```rust
    // 会话恢复之后：不是"进程活着"就够，而是**这个会话仍然服务 exec** —— 这就是这个功能的
    // 产品语义（D9 在 2026-09-25 由 fork `1f41f1a` 关闭：恢复进会话 ⇒ exec 继续由 init 服务）。
    // 恢复出来的孩子是 init 生的，所以 exec 必须照常；OCI / `--restore-from` 那条 E2B 不消费的
    // 路在这里才是按名拒绝的，两条路的差别写进 Task 2 的分层表 ③。
    assert!(
        dst.stats().await.children_live >= 2,
        "the restored child must be registered in the session's child table \
         (park + restored workload), got {}",
        dst.stats().await.children_live
    );
    let echoed = dst
        .exec(&["/bin/echo", "EXEC_OK"], ExecStdio::Piped)
        .await
        .expect("the restored session must still serve exec");
    let out = read_exact_bytes(echoed.stdout.expect("piped stdout"), "EXEC_OK\n".len());
    assert_eq!(String::from_utf8_lossy(&out), "EXEC_OK\n");
    assert_eq!(
        dst.wait_child(echoed.child_id).await.expect("wait the exec"),
        ExitStatus::Code(0)
    );
```

（2）新增模拟根用例（静态 helper，跟随真根用例）—— 交付物 1 的判据：

```rust
/// 生产形态的**另一态**：模拟根（`real_root(false)`）。
///
/// 真根那一态与一次性恢复都已有用例，而 E2B 走的是**会话**这条；chroot 根与 restore stub
/// 的不兼容（fork `43cc62a` 拒绝 → `a6f6b04` 改 fd 投递）当年正是这一态撞出来的，所以会话
/// 恢复在模拟根下必须有一条自己的用例，而不是靠"一次性那条绿"外推。
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn test_a_static_workload_resumes_into_a_session_under_an_emulated_chroot() {
    let helper = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/rootfs-helper")
        .canonicalize()
        .expect("rootfs-helper — build.rs should have compiled it");
    let tmp = std::env::temp_dir().join(format!(
        "sandlock-emulated-session-restore-{}",
        std::process::id()
    ));
    let _ = std::fs::remove_dir_all(&tmp);
    let rootfs = tmp.join("rootfs");
    let work = tmp.join("work");
    std::fs::create_dir_all(rootfs.join("usr/bin")).unwrap();
    std::fs::create_dir_all(rootfs.join("work")).unwrap();
    std::fs::create_dir_all(&work).unwrap();
    std::fs::copy(&helper, rootfs.join("usr/bin/rootfs-helper")).unwrap();
    let counter = work.join("clock.cnt");
    let park_counter = work.join("park.cnt");
    let counter_s = counter.to_str().unwrap().to_string();
    let park_s = park_counter.to_str().unwrap().to_string();
    let read_counter = || {
        std::fs::read_to_string(&counter)
            .ok()
            .and_then(|s| s.trim().parse::<u64>().ok())
    };
    let euid = unsafe { libc::geteuid() };
    let egid = unsafe { libc::getegid() };
    let mut builder = sandlock_core::Sandbox::builder()
        .chroot(&rootfs)
        .real_root(false)
        .user(euid, egid)
        .fs_read("/usr")
        .fs_mount("/work", &work)
        .fs_write("/work")
        .cwd("/work");
    builder.userns_self_map = true;
    let policy = builder.build().expect("emulated-chroot policy builds");

    // 主子进程是一个**不 fork** 的长驻进程，作用与 route B 的 park 相同：让 init（因而让
    // `exec`）活着，同时在 `checkpoint_excluding_main` 的语义里不是"那个工作负载"。
    let mut src = SandboxInstance::launch_exec(
        policy.clone().with_name("nochroot-src"),
        &["/usr/bin/rootfs-helper", "clock-loop", &park_s],
    )
    .await
    .expect("launch the parked emulated-chroot session");
    let work_handle = src
        .exec(
            &["/usr/bin/rootfs-helper", "clock-loop", counter_s.as_str()],
            ExecStdio::Piped,
        )
        .await
        .expect("exec the workload");
    let deadline = Instant::now() + Duration::from_secs(20);
    while !read_counter().is_some_and(|v| v >= 3) {
        assert!(Instant::now() < deadline, "the workload must run first");
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    let cp = src
        .checkpoint_excluding_main()
        .await
        .expect("capture the workload beside the park");
    assert_eq!(cp.process_state.pid, work_handle.pid);
    src.shutdown().await.expect("tear the source session down");
    std::fs::write(&counter, b"0\n").unwrap();

    let mut dst = SandboxInstance::launch_exec(
        policy.with_name("nochroot-dst"),
        &["/usr/bin/rootfs-helper", "clock-loop", &park_s],
    )
    .await
    .expect("launch the destination session");
    let resumed = dst
        .restore_into_session(&cp)
        .await
        .expect("restore into an emulated-chroot session");
    let deadline = Instant::now() + Duration::from_secs(20);
    while !read_counter().is_some_and(|v| v > 0) {
        assert!(
            Instant::now() < deadline,
            "the restored workload must keep running under an emulated chroot \
             (counter {:?}, restored child {} pid {})",
            read_counter(),
            resumed.child_id,
            resumed.pid
        );
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    let echoed = dst
        .exec(
            &["/usr/bin/rootfs-helper", "echo", "EXEC_OK"],
            ExecStdio::Piped,
        )
        .await
        .expect("the restored session must still serve exec");
    let out = read_exact_bytes(echoed.stdout.expect("piped stdout"), "EXEC_OK\n".len());
    assert_eq!(String::from_utf8_lossy(&out), "EXEC_OK\n");
    let _ = dst.kill_child(resumed.child_id, libc::SIGKILL);
    let _ = dst.kill_child(0, libc::SIGKILL);
    let _ = dst.wait_child(0).await;
    let _ = dst.shutdown().await;
    let _ = std::fs::remove_dir_all(&tmp);
}
```

（3）新增"grant 缺失"用例 —— 交付物 3 的判据。它构造的是 `instance.rs:577-584` 里那条
`if restore_stub.exists()` 的**另一支**：会话起来时 stub 不在，于是 domain 里没有那条 grant；
之后 stub 出现，恢复必须**按名拒绝**，而不是让沙箱内的拒绝去当答案。

```rust
/// 会话启动时装不上 stub 的 grant ⇒ 恢复要**按名拒绝**。
///
/// grant 是启动时装一次（`instance.rs:577-584`，Landlock 域安装后加不了规则），所以
/// "stub 与会话不同龄"是唯一一个会把失败原因藏起来的位置：今天它会以沙箱内的拒绝出现，
/// 读起来像"恢复坏了"。`SANDLOCK_RESTORE_STUB` 是 `stub_path()` 的第一优先级
/// （`crates/sandlock-core/src/checkpoint/resume.rs:86-104`），用它构造这一态。
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn test_a_session_launched_without_the_stub_grant_refuses_a_restore_by_name() {
    let helper = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/rootfs-helper")
        .canonicalize()
        .expect("rootfs-helper — build.rs should have compiled it");
    let helper_s = helper.to_str().unwrap().to_string();
    let tmp = std::env::temp_dir().join(format!("sandlock-stub-grant-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&tmp);
    std::fs::create_dir_all(&tmp).unwrap();
    let absent = tmp.join("restore-stub");
    let counter = tmp.join("clock.cnt");
    let counter_s = counter.to_str().unwrap().to_string();
    // 与 route B 同一个 park：让 `sandlock-init`（因而让 `exec`）活着，且不 fork。
    const PARK: &str =
        "trap '' TERM HUP INT QUIT USR1 USR2 PIPE; while :; do kill -STOP $$; done";
    let read_counter = || {
        std::fs::read_to_string(&counter)
            .ok()
            .and_then(|s| s.trim().parse::<u64>().ok())
    };
    let euid = unsafe { libc::geteuid() };
    let egid = unsafe { libc::getegid() };
    // 与 `test_a_sessions_workload_is_captured_with_the_park_left_out` 同一套形状：
    // 宿主路径 + `fs_read`/`fs_write`，不挂 chroot —— 这一条要钉的只是 stub 的 grant。
    let mut builder = sandlock_core::Sandbox::builder()
        .user(euid, egid)
        .fs_read(helper.parent().unwrap())
        .fs_read(&tmp)
        .fs_write(&tmp)
        .cwd(&tmp);
    builder.userns_self_map = true;
    let policy = builder.build().expect("stub-free policy builds");

    // 这一版引擎的会话不要求 chroot；要点只是"启动时 stub 不在这条路径上"。
    let previous = std::env::var_os("SANDLOCK_RESTORE_STUB");
    // SAFETY: this suite runs with `--test-threads=1` and the variable is restored below.
    unsafe { std::env::set_var("SANDLOCK_RESTORE_STUB", &absent) };

    let mut src = SandboxInstance::launch_exec(
        policy.clone().with_name("nogrant-src"),
        &["sh", "-c", PARK],
    )
    .await
    .expect("launch the parked session");
    let work_handle = src
        .exec(
            &[helper_s.as_str(), "clock-loop", counter_s.as_str()],
            ExecStdio::Piped,
        )
        .await
        .expect("exec the workload");
    let deadline = Instant::now() + Duration::from_secs(20);
    while !read_counter().is_some_and(|v| v >= 3) {
        assert!(Instant::now() < deadline, "the workload must run first");
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    let cp = src
        .checkpoint_excluding_main()
        .await
        .expect("capture the workload beside the park");
    let _ = src.kill_child(work_handle.child_id, libc::SIGKILL);
    let _ = src.wait_child(work_handle.child_id).await;
    src.shutdown().await.expect("tear the source session down");

    // 目标会话仍在 stub 不存在的状态下起来 ⇒ 它的 domain 里没有那条 grant。
    let mut dst = SandboxInstance::launch_exec(
        policy.with_name("nogrant-dst"),
        &["sh", "-c", PARK],
    )
    .await
    .expect("launch the destination session");
    // **起来之后** stub 才出现：`restore_into_session` 的 `stub.exists()` 这一关因此过得去，
    // 而 domain 里的 grant 还是空的 —— 这正是我们要的那一态。
    std::fs::write(&absent, b"not a real stub").unwrap();
    // 引擎报的是它自己解析出来的那条路径（`restore_into_session` 会 canonicalize），这里跟着算一遍。
    let named = absent.canonicalize().unwrap_or_else(|_| absent.clone());
    let err = dst
        .restore_into_session(&cp)
        .await
        .expect_err("a session without the stub's grant must refuse, not fail inside the sandbox");
    assert_eq!(
        err.to_string(),
        format!(
            "restore: this session was launched without a grant for the restore stub ({}): the \
             sandbox's Landlock domain is installed once, at launch, so a stub that appeared \
             afterwards cannot be exec'd here; lease a fresh slot and resume into that",
            named.display()
        )
    );

    let _ = dst.kill_child(0, libc::SIGKILL);
    let _ = dst.wait_child(0).await;
    let _ = dst.shutdown().await;
    let _ = std::fs::remove_dir_all(&tmp);
    // SAFETY: as above.
    unsafe {
        match previous {
            Some(value) => std::env::set_var("SANDLOCK_RESTORE_STUB", value),
            None => std::env::remove_var("SANDLOCK_RESTORE_STUB"),
        }
    }
}
```

同时改基线计数（`test-all.sh` 会因为"套件多跑了两条"判红；`560` 是 2026-09-26 本机实测值）：

```text
core_integ = 562 # 2026-09-26: 560 -> 562, +2:
                 #  (1) 会话恢复在**模拟根**（real_root(false)）下也要能继续 exec
                 #      （test_a_static_workload_resumes_into_a_session_under_an_emulated_chroot）
                 #  (2) 会话启动时没装上 stub 的 grant ⇒ 恢复按名拒绝
                 #      （test_a_session_launched_without_the_stub_grant_refuses_a_restore_by_name）
                 # —— E2B 走的就是会话这条恢复路径，而它此前只有"真根（不带 exec 断言）"
                 #    与"无根（带 exec 断言）"两态被钉住。
```

- [ ] **Step 3: 跑它，确认结果（**前两条是这个计划里唯一无法预先写出"红色原文"的用例**：那两条边界今天可能已经成立，也可能不成立，两种结果都必须写进提交信息；第三条按 Step 4 的实现判红/判绿）**

```bash
cd /Users/polus/project/ai/sandlock-e2b/third_party/sandlock
IMAGE=sandlock-dev-f17:latest /Users/polus/project/ai/sandlock-e2b/deploy/scripts/fork-gate.sh \
  --one 'test_a_dynamic_workload_resumes_into_a_session_under_a_real_root'
IMAGE=sandlock-dev-f17:latest /Users/polus/project/ai/sandlock-e2b/deploy/scripts/fork-gate.sh \
  --one 'test_a_static_workload_resumes_into_a_session_under_an_emulated_chroot'
IMAGE=sandlock-dev-f17:latest /Users/polus/project/ai/sandlock-e2b/deploy/scripts/fork-gate.sh \
  --one 'test_a_session_launched_without_the_stub_grant_refuses_a_restore_by_name'
```

Expected: 前两条各 `1 passed; 0 failed`（真根这条在补断言之前就已经绿，补的是 exec 断言）；
第三条在动手改之前应当 **红**，红的样子就是"失败发生在沙箱里"而不是被点名（`expect_err` 拿到的
文本不是 Step 4 里那句，或者干脆是 `restore: …` 的其它原话）——那句原文进 Step 4。

- [ ] **Step 4: 最小实现（交付物 3 的候选④；前两条红了才按下面的分派做）**

```text
红的原文形如 "restore: …" ⇒ 修复落在 crates/sandlock-core/src/instance.rs:1229-1290
  （restore_into_session 的 chroot_root / mounts / plan 三段，与 Sandbox::restore_interactive
  在 crates/sandlock-core/src/sandbox.rs:1420-1470 做的是同一件事；两边必须同参，抄过去）
红的原文形如 "restore-stub was not built" ⇒ 是构建面：
  crates/sandlock-core/src/checkpoint/resume.rs:86 stub_path() + build.rs；
  本机容器 lane 里有 stub（build.rs 编译进 target/），所以这条只会在 wheel/集群上出现。
```

候选④的实现（三处，都是小改动）：

```rust
// 1) InstanceRuntime 上记下"启动时到底装上了哪个 stub 的 grant"（与 policy_image 并列，
//    crates/sandlock-core/src/instance.rs:382 那一带）。`None` = 启动时 stub 不在，
//    domain 里因此没有这条 grant。
    /// F/R: the restore stub's host path when its Landlock grant was armed at
    /// launch. Landlock domains are installed once and cannot be widened
    /// later, so a session can only ever resume into *this* stub path.
    pub(crate) restore_stub_grant: Option<std::path::PathBuf>,

// 2) launch_exec_inner 里装 grant 的那一段（crates/sandlock-core/src/instance.rs:577-584）：
//    装上就记下来。缺失时不再静默 —— 留一条面包屑（`resume::note` 与
//    `SANLOCK_RESTORE_TRACE=1` 同一条通道，E2B 侧会把它打进 worker 日志）。
        let restore_stub = crate::checkpoint::resume::stub_path();
        if restore_stub.exists() {
            let stub_path = restore_stub.canonicalize().unwrap_or(restore_stub);
            if !policy.fs_readable_host.contains(&stub_path) {
                policy.fs_readable_host.push(stub_path.clone());
            }
            rt.restore_stub_grant = Some(stub_path);
        } else {
            crate::checkpoint::resume::note(&format!(
                "no restore-stub at launch ({}): this session cannot resume into a stub \
                 that appears later",
                restore_stub.display()
            ));
        }

// 3) restore_into_session 的入口（crates/sandlock-core/src/instance.rs:1255-1258，
//    在 `let stub = crate::checkpoint::resume::stub_path();` 之后、任何 plan/channel 工作之前）：
//    路径与会话启动时那一个不一致 ⇒ 按名拒绝（这句文本就是 Step 2 用例断言的那一句）。
        let granted = self.rt.restore_stub_grant.as_deref();
        if granted != Some(stub_path.as_path()) {
            return Err(SandboxRuntimeError::Child(format!(
                "restore: this session was launched without a grant for the restore stub ({}): \
                 the sandbox's Landlock domain is installed once, at launch, so a stub that \
                 appeared afterwards cannot be exec'd here; lease a fresh slot and resume into \
                 that",
                stub_path.display()
            ))
            .into());
        }
```

（若前两条中的任何一条在 Step 3 红了，按上面的分派先修那一条；`crates/sandlock-core/src/checkpoint/resume.rs:86-104` 只在"会话与 stub 不同龄"变成真实投诉时才动——那是候选③，不在本轮。）

- [ ] **Step 5: 跑测试，确认通过**

```bash
cd /Users/polus/project/ai/sandlock-e2b/third_party/sandlock
IMAGE=sandlock-dev-f17:latest /Users/polus/project/ai/sandlock-e2b/deploy/scripts/fork-gate.sh \
  --one 'test_a_static_workload_resumes_into_a_session_under_an_emulated_chroot'
# Expected: test result: ok. 1 passed; 0 failed
IMAGE=sandlock-dev-f17:latest /Users/polus/project/ai/sandlock-e2b/deploy/scripts/fork-gate.sh \
  --one 'test_a_session_launched_without_the_stub_grant_refuses_a_restore_by_name'
# Expected: test result: ok. 1 passed; 0 failed（拒绝的那句与 Step 2 的断言逐字相同）
IMAGE=sandlock-dev-f17:latest /Users/polus/project/ai/sandlock-e2b/deploy/scripts/fork-gate.sh \
  --one 'test_a_child_restored_into_a_session_keeps_the_session_executable'
# Expected: test result: ok. 1 passed; 0 failed（无根那一态不许被这次改动带红）
IMAGE=sandlock-dev-f17:latest /Users/polus/project/ai/sandlock-e2b/deploy/scripts/fork-gate.sh --one 'test_restore::'
# Expected: test result: ok. 5 passed; 0 failed      （一次性恢复那两态不许被这次改动带红）
IMAGE=sandlock-dev-f17:latest /Users/polus/project/ai/sandlock-e2b/deploy/scripts/fork-gate.sh --one 'test_instance_exec::'
# Expected: 0 failed；这一族的总数比改动前多 2（新用例），基线的 core_integ 因此从 560 到 562 ——
#           门禁自己会拿 docs/test-baseline.md 比对，数字不对它直接判红，这就是那条计数的验收
```

- [ ] **Step 6: 提交（fork 仓单独提交 + 重建 wheel）**

```bash
cd /Users/polus/project/ai/sandlock-e2b/third_party/sandlock
git add crates/sandlock-core/tests/integration/test_instance_exec.rs crates/sandlock-core/src/instance.rs \
        docs/test-baseline.md
git commit -m "fix(restore): pin the stub's launch-time grant, and refuse a stale session by name"
cd /Users/polus/project/ai/sandlock-e2b
deploy/scripts/build-sandlock-wheels.sh            # ~4 分钟；E2B 侧不重建 wheel 就看不到这条改动
git add third_party/sandlock
git commit -m "chore(fork): the restore stub's launch-time grant is pinned on both root shapes"
```

---

### Task 2: 生产形态到底能不能用 —— 把集群验收搬进仓库并跑出判据（E2B）

**为什么它是头两个任务之一**：S2–S4 落地后**只跑过两次**生产形态验收（`0.1.0-525` / `0.1.0-527`，见
`docs/deploy-clusters.md:209-291`），而仓库现在已经是 `0.1.0-535`（含 N15 的 `_chroot_root` 改动、
F11 的多副本、两次 fork 侧修复）。那两次验收的脚本本身也躺在 `tmp/k0s/`（被 `.gitignore`
忽略 ⇒ **不在仓库里**），所以"今天还能不能用"和"这条能力有没有验收"这两件事现在都没有答案。
这一步先把脚本转正，再拿它当判据——它绿，后面所有 E2B 任务才有基线；它红，红在哪一条断言就决定
接下来改哪一层（本任务 Step 3 的分层表）。

**它不回答"引擎这条交接点还成不成立"**：那一条由 Task 1 在本机判红/判绿（stub × 根形态 × 会话），
本任务只回答"**当前部署的这一版**在生产形态下跑不跑得通"，因此**不需要新 wheel、不依赖任何代码改动**，
也可以排在 Task 1 之前跑。

**Files:**
- Create: `deploy/scripts/checkpoint_acceptance.py`（源：`tmp/k0s/checkpoint_acceptance.py`，实测 437 行，E2B 仓）
- Modify: `docs/deploy-clusters.md:265-280`（§9 的"怎么再跑一遍"，:`274` 那行指 `tmp/k0s/checkpoint_acceptance.py` ⇒ 改成仓库内的脚本）
- Test: 脚本自身（每步 `assert`，末行 `{"step": "OK"}`）

**Interfaces:**
- Consumes: `deploy/scripts/open-cluster-tunnel.sh`；`deploy/k8s-k0s/apply.sh`；`deploy/stack/.version`；集群 secret `sandlock/e2b-secrets` 的 `E2B_API_KEYS` / `E2B_INTERNAL_API_KEY`；worker 清单里的 `E2B_REAL_ROOT=1`、`E2B_PAUSE_CHECKPOINT=1`、`E2B_PLATFORM_DISK_MB=8192`（`deploy/k8s/worker.yaml:314/340/342`）；`.venv` 里的 `e2b` SDK（`tmp/testenv` 没有 `e2b`，脚本必须用 `.venv/bin/python` 跑）
- Produces: `deploy/scripts/checkpoint_acceptance.py`——后续每个 E2B 任务的端到端验收都调它；`tmp/k0s/checkpoint-task2.log`（这一步的判据证据）

- [ ] **Step 1: 把脚本搬进仓库，并加上"worker 开关必须是开的"这条前置断言**

```bash
cd /Users/polus/project/ai/sandlock-e2b
mkdir -p deploy/scripts
cp tmp/k0s/checkpoint_acceptance.py deploy/scripts/checkpoint_acceptance.py
```

改文件头第二行（说明它已经是仓库的一部分）：

```python
"""Cluster acceptance for S2/S3/S4: a pause that survives its worker.

仓库版（2026-09-26 从 ``tmp/k0s/checkpoint_acceptance.py`` 搬入）。它回答的是**生产形态**
（image-rootfs + ``E2B_REAL_ROOT=1``）下这条能力到底能不能用，所以它不只是回归测试，
也是"这一版能不能对外声明"的判据。用法见 ``docs/deploy-clusters.md`` §9。
"""
```

在 `main()` 里 `probe = kubectl("get", "nodes", ...)` 那段**后面**加一条开关断言——否则
worker 上的 `E2B_PAUSE_CHECKPOINT` 被关掉时，这条验收会以另一种方式红（`resume` 恢复不出
任何东西），把"开关没开"误判成"引擎坏了"：

```python
    # 开关必须真的在这版清单里（`deploy/k8s/worker.yaml`），且三件一起才构成"生产形态"：
    # 真根、pause 抓图、平台账。哪一个没开，下面那条验收的失败原因都不是引擎。
    def worker_env(name: str) -> str:
        out = kubectl(
            "get",
            "sts",
            "e2b-worker",
            "-o",
            "jsonpath={.spec.template.spec.containers[0].env[?(@.name=='"
            + name
            + "')].value}",
        )
        return out.stdout.strip()

    assert worker_env("E2B_PAUSE_CHECKPOINT") == "1", (
        "worker 清单里 E2B_PAUSE_CHECKPOINT 不是 1；这一版 pause 不会写图，"
        "下面的验收测的不是这个功能"
    )
    assert worker_env("E2B_REAL_ROOT") == "1", "worker 清单里 E2B_REAL_ROOT 不是 1"
    assert worker_env("E2B_PLATFORM_DISK_MB") == "8192", (
        "worker 清单里的平台账预算不是 8192 MiB；图会以 0=不限 的形态落盘"
    )
```

同时把 `docs/deploy-clusters.md` §9 末尾"怎么再跑一遍"的 `tmp/k0s/checkpoint_acceptance.py`
改成 `deploy/scripts/checkpoint_acceptance.py`：

```bash
sed -n '268,282p' docs/deploy-clusters.md   # 改前：.venv/bin/python tmp/k0s/checkpoint_acceptance.py
```

- [ ] **Step 2: 跑它，把结论记下来（这一步的输出就是判据）**

```bash
cd /Users/polus/project/ai/sandlock-e2b
deploy/scripts/open-cluster-tunnel.sh --check      # 期望最后一行形如：✓ 2 节点 / arm64 / 含 +k0s
export KUBECONFIG="$PWD/tmp/k0s/kubeconfig"
kubectl get nodes -o wide                          # 期望：2 个节点，arm64，v1.36.4+k0s，172.18.80.94 / .140
kubectl -n sandlock get pods                       # 期望：control-plane-*、autoscaler-*、e2b-worker-0/1、redis-*、seccomp-installer-*

export E2B_API_URL=http://172.18.78.49:3000
export E2B_API_KEY=$(kubectl -n sandlock get secret e2b-secrets -o jsonpath='{.data.E2B_API_KEYS}' | base64 -d)
export E2B_INTERNAL_API_KEY=$(kubectl -n sandlock get secret e2b-secrets -o jsonpath='{.data.E2B_INTERNAL_API_KEY}' | base64 -d)
.venv/bin/python deploy/scripts/checkpoint_acceptance.py | tee tmp/k0s/checkpoint-task2.log
```

Expected（全部满足才算绿，缺一条就是红，红的原文进 Step 3）:

```text
{"step": "created", ...}
{"step": "running", "counter": N}                     # N >= 3
{"step": "paused", "counter": N, "frozen_after_4s": N}
{"step": "image", "listing": "... meta.json ..."}      # 含 meta.json 或 policy.dat
{"step": "platform_account", "nodes": {...}}           # 每节点 platformDiskUsedMB / platformDiskBudgetMB
{"step": "thawed_kept_ticking", "counter": M}          # M > N
{"step": "exec_after_thaw", "stdout": "THAWED_OK\n"}
{"step": "resumed", "before": X, "after": Y}           # Y - X < 30
{"step": "exec_after_resume", "stdout": "EXEC_OK\n"}
{"step": "image_consumed", "listing": "... No such file or directory ..."}
{"step": "worker_log", "line": "... resumed ... into the session (child 1, pid N); K fd(s) could not come back ..."}
{"step": "OK"}
```

`echo $?` = `0`。

- [ ] **Step 3: 若红，按这一层定位（红在哪一条，命令就打哪一层）**

```bash
# ① 红在 image / platform_account ⇒ 图根本没写：先确认这一版清单真的生效了
kubectl -n sandlock get sts e2b-worker -o jsonpath='{.spec.template.spec.containers[0].env}' | tr ',' '\n' | grep -E 'E2B_PAUSE_CHECKPOINT|E2B_REAL_ROOT|E2B_PLATFORM_DISK_MB'
kubectl -n sandlock logs e2b-worker-0 | grep -E 'checkpoint|holds no checkpoint' | tail -20
# 期望至少一行：pause of sandbox sbx_... holds no checkpoint: <引擎原话> 或 wrote checkpoint ... (N MiB, pid P)

# ② 红在 resumed（恢复了但计数不动）⇒ 先分清引擎 / 部署：本机 lane 跑同形状（Task 1 的那三条）
cd third_party/sandlock
IMAGE=sandlock-dev-f17:latest /Users/polus/project/ai/sandlock-e2b/deploy/scripts/fork-gate.sh \
  --one 'test_a_dynamic_workload_resumes_into_a_session_under_a_real_root'
# 期望：test result: ok. 1 passed; 0 failed
#   绿 ⇒ 引擎在"动态 + 真根 + 会话 + restore"这个形状上是对的，红的是部署侧（Task E2 的输入）
#   红 ⇒ 引擎回归，把它当 P0，按引擎侧门禁的纪律留红的日志再单跑

# ③ 红在 exec_after_resume / worker_log ⇒ 先按"哪条恢复路"分层，再按"stub 在不在"分层
kubectl -n sandlock logs e2b-worker-0 | grep -E 'resumed .* into the session|restore refused|restore-stub was not built' | tail -5
# 期望至少一行 `resumed … into the session (child N, pid M)`：E2B 走的是**恢复进会话**，
#   那一条**保留 exec**（fork 1f41f1a；OCI / `--restore-from` 那条才按名拒绝，E2B 不消费它）。
#   没有任何 `resumed …` ⇒ 恢复根本没跑到（看 ①②），不是"exec 不被支持"。
#   出现 `restore-stub was not built` ⇒ wheel 没带 stub（fork 2d5f2e9 之后不该出现）。
#   出现沙箱内的 EACCES / 引擎的 "landlock" 类拒绝 ⇒ 这个会话起来时 stub 的 grant 没装上
#   （Task 1 的交付物 3 / 候选④就是把它变成一句点名的话）。

# ④ 红在 kubectl 的报错上（`kubectl exec` 打不出图、`kubectl delete pod` 超时）⇒ 通道，不是功能
deploy/scripts/open-cluster-tunnel.sh --check
```

- [ ] **Step 4: 把绿的日志留成证据，并把结论写进文档**

```bash
cd /Users/polus/project/ai/sandlock-e2b
grep -c '^{"step"' tmp/k0s/checkpoint-task2.log      # 期望 >= 10（脚本每一步都打一行）
tail -1 tmp/k0s/checkpoint-task2.log                 # 期望：{"step": "OK"}
```

在 `docs/deploy-clusters.md` §9 顶部的"**版本**"行改为当前版本，并追加一行判据：

```markdown
> **2026-09-26 复核**：`deploy/scripts/checkpoint_acceptance.py` 在 `0.1.0-535-…` 上全绿
> （日志 `tmp/k0s/checkpoint-task2.log`，末行 `{"step": "OK"}`）。这条能力在生产形态
> （image-rootfs + `E2B_REAL_ROOT=1`）下**可用**。**"恢复后不能 exec"不是拦路虎**：D9 已在
> 2026-09-25 由 fork `1f41f1a` 关闭 —— E2B 走 `restore` verb 把镜像恢复**进会话**，
> `exec` 继续由 init 服务（判据就是下面那条 `exec_after_resume`：`EXEC_OK\n`）。
> restore stub 的交付也不再走宿主路径：fork `a6f6b04` 改成按描述符投递，模拟根与真根两态
> 都有用例（`test_restore_resumes_inside_a_chroot_root`）。剩下的都是**语义与运维**问题，
> 以及 Task 1 在会话路径上补的那两条用例（见
> `docs/superpowers/plans/2026-09-26-checkpoint-restore-productization.md`）。
```

- [ ] **Step 5: 提交**

```bash
git add deploy/scripts/checkpoint_acceptance.py docs/deploy-clusters.md
git commit -m "test(checkpoint): the production-shape acceptance lives in the repo and passes"
```

---

### Task F2: `pause` 捕获的"是谁"要说得出名字（fork + E2B 两半）

**要钉的是什么**：`checkpoint` verb 的回复今天只有 `{dir, name, pid, fds}`（`serve.rs:765-770`）。
FUP-30 那次事故里，唯一的证据是"图里有 19 个映射、填充 388 KiB"——**那是 dash 的大小**，
不是 python 的。用户真正需要知道的是一句直白的话："这次 pause 抓到的是 `dash`"。
本任务把 `/proc/<pid>/exe`（realpath）与 `/proc/<pid>/cmdline`（NUL 分隔）加进 verb 回复；
E2B 侧的透传在 Task E2（要等本任务的 wheel）。

**Files:**
- Modify: `crates/sandlock-supervise/src/serve.rs:746-772`（`handle_checkpoint` 的回复；fork 仓）
- Create: `crates/sandlock-supervise/tests/supervise.rs` 新增
  `test_the_checkpoint_reply_names_the_captured_program`（fork 仓）
- Modify: `third_party/sandlock/docs/test-baseline.md`（`supervise = 55` → `56`，fork 仓）

**Interfaces:**
- Consumes: `Generation::handle_checkpoint`（`serve.rs:726`）、已就位的 `cp.process_state.pid`（`serve.rs:751`）
- Produces: verb 回复新增两个键 —— `exe: string`（`readlink /proc/<pid>/exe`，读不到就是 `""`）、`argv: string[]`（`/proc/<pid>/cmdline` 按 NUL 拆、丢空段；读不到就是 `[]`）。Task E2 依赖这两个键名，**不许改名**。

- [ ] **Step 1: 写会失败的测试**

```rust
/// FUP-30 的真因是"抓到的是包装用的那个 shell，而不是负载"—— 而当时唯一的证据是映射数量
/// 与填充字节（19 / 388 KiB 是 dash）。这条用例要求 verb 的回复**直接给出**被捕获进程的身份：
/// `exe` 是 `/proc/<pid>/exe` 的 realpath，`argv` 是 `/proc/<pid>/cmdline`。
///
/// 用 `sleep 30` 而不是 `sh -c '… && exec …'`：后者的 pid 会在证据文件写完之后**变成** sleep，
/// 于是断言与 exec 赛跑（FUP-30 的调试经验：会读到随机一侧）。`sleep 30` 的 argv 从 execve
/// 那一刻起就固定，`exe` 也固定。
#[test]
fn test_the_checkpoint_reply_names_the_captured_program() {
    let ctl_root = isolate_ctl_root();
    let workdir = repo_tmp_dir().join(format!("supervise-cp-names-{}", std::process::id()));
    std::fs::create_dir_all(&workdir).expect("create workdir");
    let image = workdir.join("image");
    let policy = write_policy(
        "names-instance",
        &instance_policy(workdir.to_str().expect("workdir utf8")),
    );
    let program = write_policy(
        "names-program",
        &serde_json::json!({ "argv": ["/bin/sleep", "30"] }).to_string(),
    );
    let name = format!("supervise-cp-names-{}", std::process::id());
    let token = "cp-names-token-0123456789abcdef";
    let (child, sock_path) = spawn_serving_slot(&ctl_root, &policy, &name, token, Some(&program));

    // 让 slot 真的把程序跑起来（launch-first：程序在 slot 开始服务之前就起了）。
    wait_until(
        Instant::now() + Duration::from_secs(15),
        "the sleep workload to be running",
        || {
            let resp = registered_verb_args(&sock_path, token, "stats", serde_json::json!({}));
            resp["data"]["children_live"].as_u64().unwrap_or(0) >= 1
        },
    );

    let resp = registered_verb_args(
        &sock_path,
        token,
        "checkpoint",
        serde_json::json!({ "dir": image.to_str().unwrap(), "exclude_main": false }),
    );
    assert_eq!(resp["ok"], serde_json::Value::Bool(true), "checkpoint: {resp:?}");

    // 精确相等，不做子串判据：`/bin` 在多数发行版上是 `/usr/bin` 的符号链接，所以期望值
    // 现场用 canonicalize 取（这也是"哪个 inode 被捕获了"的正确问法）。
    let expected_exe = std::fs::canonicalize("/bin/sleep").expect("/bin/sleep exists");
    assert_eq!(
        resp["data"]["exe"],
        serde_json::json!(expected_exe.to_str().unwrap()),
        "the reply must name the captured program by its real path: {resp:?}"
    );
    assert_eq!(
        resp["data"]["argv"],
        serde_json::json!(["/bin/sleep", "30"]),
        "the reply must carry the captured program's argv: {resp:?}"
    );

    let _ = child.kill();
    let _ = child.wait();
    let _ = std::fs::remove_dir_all(&workdir);
}
```

```text
supervise = 56 # 2026-09-26: 55 -> 56, +1: the checkpoint reply names the captured program
               # (`test_the_checkpoint_reply_names_the_captured_program`) —— FUP-30 的教训是
               # "抓到了谁"必须由引擎说出来，而不是靠映射数量猜。
```

- [ ] **Step 2: 跑它，确认失败**

```bash
cd /Users/polus/project/ai/sandlock-e2b/third_party/sandlock
IMAGE=sandlock-dev-f17:latest /Users/polus/project/ai/sandlock-e2b/deploy/scripts/fork-gate.sh \
  --one 'test_the_checkpoint_reply_names_the_captured_program'
```

Expected: `1 failed`，断言原文是
``assertion `left == right` failed ... left: Null ... right: String("/usr/bin/sleep")``
（回复里根本没有这个键）。

- [ ] **Step 3: 最小实现**

```rust
        // ... `cp.save(...)` 之后、构造回复之前：把被捕获进程的身份读出来。放在 save 之后是
        // 因为捕获会 SIGSTOP→SIGCONT 目标进程，我们要读的正是"那个还在跑的进程"；读不到
        // （进程在捕获窗口里死了）就给空值 —— 空值是真实答案，不要为它编一个猜测。
        let exe = std::fs::read_link(format!("/proc/{pid}/exe"))
            .map(|p| p.to_string_lossy().into_owned())
            .unwrap_or_default();
        let argv: Vec<String> = std::fs::read(format!("/proc/{pid}/cmdline"))
            .map(|raw| {
                raw.split(|b| *b == 0)
                    .filter(|s| !s.is_empty())
                    .map(|s| String::from_utf8_lossy(s).into_owned())
                    .collect()
            })
            .unwrap_or_default();
        Ok(serde_json::json!({
            "dir": dir,
            "name": cp.name,
            "pid": pid,
            "fds": fds,
            // FUP-30：让调用方能写出一句"这次 pause 抓到的是 dash"，
            // 而不是让使用者自己去比映射数量。
            "exe": exe,
            "argv": argv,
        }))
```

- [ ] **Step 4: 跑测试，确认通过**

```bash
cd /Users/polus/project/ai/sandlock-e2b/third_party/sandlock
IMAGE=sandlock-dev-f17:latest /Users/polus/project/ai/sandlock-e2b/deploy/scripts/fork-gate.sh \
  --one 'test_the_checkpoint_reply_names_the_captured_program'
# Expected: test result: ok. 1 passed; 0 failed
IMAGE=sandlock-dev-f17:latest /Users/polus/project/ai/sandlock-e2b/deploy/scripts/fork-gate.sh --one 'test_supervise_checkpoint'
# Expected: 2 passed; 0 failed（既有两条不许被新键带红 —— 它们比的是具体键，不是整份回复）
```

- [ ] **Step 5: 提交（fork + 重建 wheel）**

```bash
cd /Users/polus/project/ai/sandlock-e2b/third_party/sandlock
git add crates/sandlock-supervise/src/serve.rs crates/sandlock-supervise/tests/supervise.rs docs/test-baseline.md
git commit -m "feat(checkpoint): the reply names the captured program (exe/argv)"
cd /Users/polus/project/ai/sandlock-e2b
deploy/scripts/build-sandlock-wheels.sh
git add third_party/sandlock
git commit -m "chore(fork): pin exe/argv in the checkpoint reply"
```

---

### Task F3: 【条件任务】fd 按路径重开时的身份校验（fork）

**决定门（先跑这一步，命中就不做）**：只有当使用者确实会把**被捕获进程持有的文件**在
pause 与 resume 之间 rename / replace 时才做。今天 `FdInfo` 只有 `fd/path/flags/offset`
（`crates/sandlock-core/src/checkpoint/mod.rs:86-91`），恢复时按路径 `openat` 重开
（`restore-stub.c:576-582`）——**同一个路径、不同的 inode** 就会把写入落到别人的文件上。

```bash
cd /Users/polus/project/ai/sandlock-e2b
rg -n "os.replace|mv |rename" docs/checkpoint-restore-e2b-half.md docs/k8s-deployment.md | head
# 期望：只有拿验收脚本自己的计时器文件（temp+os.replace）那一段；出现"业务文件会被 replace"
# 的用法 ⇒ 做；只有上面那一段 ⇒ 不做（记一行到 Task E8 的文档里，写明这是已知边界）
```

**Files:**
- Modify: `crates/sandlock-core/src/checkpoint/mod.rs:86-91`（`FdInfo` 加 `st_dev`/`st_ino`；fork 仓）
- Modify: `crates/sandlock-core/src/checkpoint/capture.rs:503-528`（填这两个值；fork 仓）
- Modify: `crates/sandlock-core/src/checkpoint/restore_blob.rs:268-284`（重开前先验身份，不一致按 `SkippedFd` 处理；fork 仓）
- Modify: `crates/sandlock-core/src/checkpoint/image.rs:33`（`IMAGE_VERSION` `3` → `4`，并改 `image_version_covers_the_thread_pointer` 的期望值；fork 仓）

**Interfaces:**
- Consumes: `build_fd_plan(&cp.fd_table)`（`restore_blob.rs:685`，`plan()` 内），它的返回值直接进 `RestorePlan.skipped` → `Sandbox::restore_skipped()` / `Instance::restore_skipped()` → verb 回复的 `restore_skipped` → E2B 的 `unrecoveredFds`（`checkpoint_store.py:384-413`）
- Produces: `FdInfo.st_dev: u64` / `FdInfo.st_ino: u64`（`#[serde(default)]`），以及 `build_fd_plan_with(fds, intact)`（可注入的判据，便于单测）

- [ ] **Step 1: 写会失败的测试**

```rust
    /// 同一个路径、换了 inode：恢复必须**跳过**这个 fd，而不是把写入落到新文件上。
    /// 这条用例不用真沙箱：判据是可注入的，路径用真的临时文件（因为默认判据 stat 的是宿主路径）。
    #[test]
    fn a_reopened_path_that_is_a_different_inode_is_skipped() {
        let dir = std::env::temp_dir().join(format!("sandlock-fd-ident-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("victim.log");
        std::fs::write(&path, b"original\n").unwrap();

        // 捕获时记下的身份
        use std::os::unix::fs::MetadataExt;
        let meta = std::fs::metadata(&path).unwrap();
        let recorded = FdInfo {
            fd: 3,
            path: path.to_str().unwrap().to_string(),
            flags: 0,
            offset: 0,
            st_dev: meta.dev(),
            st_ino: meta.ino(),
        };

        // 路径被 replace：同路径、同大小、不同 inode
        std::fs::remove_file(&path).unwrap();
        std::fs::write(&path, b"replaced\n").unwrap();

        let (restorable, skipped) = build_fd_plan(&[recorded.clone()]);
        assert_eq!(restorable.len(), 0, "a replaced inode must not be reopened");
        assert_eq!(
            skipped,
            vec![SkippedFd { fd: 3, path: recorded.path.clone() }],
            "the replaced fd is reported as skipped, not silently reopened"
        );

        // 阴性对照：没被动过的文件照旧进 restorable
        let intact = FdInfo { fd: 4, ..recorded.clone() };
        std::fs::write(&path, b"original\n").unwrap();
        use std::os::unix::fs::MetadataExt as _;
        let meta = std::fs::metadata(&path).unwrap();
        let intact = FdInfo { st_dev: meta.dev(), st_ino: meta.ino(), ..intact };
        let (restorable, skipped) = build_fd_plan(&[intact]);
        assert_eq!(restorable.len(), 1);
        assert_eq!(skipped.len(), 0);

        let _ = std::fs::remove_dir_all(&dir);
    }
```

同时把 `restore_blob.rs:855/874` 两条既有单测里的假路径改成 `build_fd_plan_with(&fds, &|_| true)`
（它们测的是路径分类，不是身份），否则它们会因为"路径不存在"一起红——**这不是放宽断言**，
是把"分类"与"身份"两件事分开测。

- [ ] **Step 2: 跑它，确认失败**

```bash
cd /Users/polus/project/ai/sandlock-e2b/third_party/sandlock
IMAGE=sandlock-dev-f17:latest /Users/polus/project/ai/sandlock-e2b/deploy/scripts/fork-gate.sh \
  --one 'a_reopened_path_that_is_a_different_inode_is_skipped'
```

Expected: 编译失败 —— `FdInfo` 没有 `st_dev` / `st_ino` 字段
（``error[E0560]: struct `FdInfo` has no field named `st_dev` ``）。

- [ ] **Step 3: 最小实现**

```rust
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FdInfo {
    pub fd: i32,
    pub path: String,
    pub flags: i32,
    pub offset: u64,
    /// 捕获那一刻这个 fd 指向的对象身份（`fstat` 的 `st_dev`/`st_ino`）。恢复按**路径**
    /// 重开，而路径可以被 rename/replace 指向另一个 inode；有这两个值就能在写内存之前
    /// 判出来，把它算成 skip（`restore_skipped`），而不是把数据写进别人的文件。
    /// `serde(default)` 只为让同一版本内的旧结构可读；跨版本的兼容由 `IMAGE_VERSION` 管。
    #[serde(default)]
    pub st_dev: u64,
    #[serde(default)]
    pub st_ino: u64,
}
```

```rust
// capture.rs::capture_fd_table，紧跟 parse_fdinfo 之后
        // `/proc/<pid>/fd/<n>` 的元数据 stat 的是**这个 fd 指向的对象**（不是路径），
        // 这正是恢复时要比对的东西。
        use std::os::unix::fs::MetadataExt;
        let (st_dev, st_ino) = match std::fs::metadata(format!("/proc/{pid}/fd/{fd}")) {
            Ok(m) => (m.dev(), m.ino()),
            Err(_) => (0, 0),
        };
        fds.push(FdInfo { fd, path, flags, offset, st_dev, st_ino });
```

```rust
// restore_blob.rs::build_fd_plan —— 分类与身份两步分开，判据可注入（单测用）
pub(crate) fn build_fd_plan(fds: &[FdInfo]) -> (Vec<FdInfo>, Vec<SkippedFd>) {
    build_fd_plan_with(fds, &identity_intact)
}

/// 记录的身份与现在路径上的对象是否同一个。路径不存在时**不**在这里判 skip：
/// "文件没了"是另一条语义（今天由 stub 的 `die(10)` 处理，见 N34），这次只关
/// "同路径不同 inode"这一个口子。
fn identity_intact(f: &FdInfo) -> bool {
    if f.st_dev == 0 && f.st_ino == 0 {
        return true; // 旧图（无身份）保持今天的重开行为
    }
    use std::os::unix::fs::MetadataExt;
    match std::fs::metadata(&f.path) {
        Ok(m) => m.dev() == f.st_dev && m.ino() == f.st_ino,
        Err(_) => true,
    }
}

pub(crate) fn build_fd_plan_with(
    fds: &[FdInfo],
    intact: &dyn Fn(&FdInfo) -> bool,
) -> (Vec<FdInfo>, Vec<SkippedFd>) { /* 原 build_fd_plan 的循环，条件改为两个 */ }
```

```rust
// image.rs
const IMAGE_VERSION: u32 = 4; // 2026-09-26: 3 -> 4 —— FdInfo 多了 st_dev/st_ino，
                              // bincode 布局变了，旧图必须在 meta 层就被拒。
// 并把 image_version_covers_the_thread_pointer 里的 assert_eq!(…, 3) 改成 4
```

- [ ] **Step 4: 跑测试，确认通过**

```bash
cd /Users/polus/project/ai/sandlock-e2b/third_party/sandlock
IMAGE=sandlock-dev-f17:latest /Users/polus/project/ai/sandlock-e2b/deploy/scripts/fork-gate.sh --one 'restore_blob'
# Expected: 0 failed（含新那条与两条改过的分类用例）
IMAGE=sandlock-dev-f17:latest /Users/polus/project/ai/sandlock-e2b/deploy/scripts/fork-gate.sh --one 'test_restore::'
# Expected: test result: ok. 5 passed; 0 failed（版本 bump 之后的图自己写得出来也读得回去）
IMAGE=sandlock-dev-f17:latest /Users/polus/project/ai/sandlock-e2b/deploy/scripts/fork-gate.sh --one 'image_version'
# Expected: 1 passed（新期望值 4）
```

- [ ] **Step 5: 提交**

```bash
cd /Users/polus/project/ai/sandlock-e2b/third_party/sandlock
git add crates/sandlock-core/src/checkpoint/mod.rs crates/sandlock-core/src/checkpoint/capture.rs \
        crates/sandlock-core/src/checkpoint/restore_blob.rs crates/sandlock-core/src/checkpoint/image.rs
git commit -m "fix(restore): an fd whose path was replaced is skipped, not reopened onto a new inode"
cd /Users/polus/project/ai/sandlock-e2b
deploy/scripts/build-sandlock-wheels.sh
git add third_party/sandlock
git commit -m "chore(fork): pin fd identity checks across a restore"
```

---

### Task F4: 【条件任务】让 `--restore-from` 那条模式也能 exec（默认不做）

**事实先摆正（这是本任务存在的全部理由）**：引擎有**两条**恢复路径，而它们对 `exec` 的态度不同——
**E2B 走的那条已经支持 exec**（`restore` verb → `SandboxInstance::restore_into_session`，
`crates/sandlock-core/src/instance.rs:1229`、`crates/sandlock-supervise/src/serve.rs:789-819`；
恢复出的进程是会话 init 的孩子，所以 `exec` / `wait_child` / `kill_child` 照常，
fork `1f41f1a`；本机用例 `test_instance_exec.rs:599`，集群验收拿到 `EXEC_OK`）。
仍按名拒绝 exec 的只有 `--restore-from` / OCI 那条**独立启动模式**
（`crates/sandlock-supervise/src/serve.rs:1473-1487`、
`crates/sandlock-oci/src/supervisor.rs:1382-1390`），而 E2B 从不走它（`route_b.py` 只发
`checkpoint` / `restore` 两个 verb）。

**所以本任务不是"D9 的候选修法"**（D9 已于 2026-09-25 关闭，见 `2026-09-26-decisions.md`），
而是"**另一条路的 exec 要不要跟**"：除非真有部署在发 `--restore-from`，否则不投人。

**候选修法与推荐**（这一步是决策，不是实现）：

| 候选 | 落点 | 代价 | 推荐 |
|---|---|---|---|
| ① 恢复进会话（**E2B 已在用**） | `crates/sandlock-core/src/instance.rs:1229` + `serve.rs:789` | 已付 | **就是答案** |
| ② 给 `--restore-from` 补 init：`RestoredGeneration::new` 不再 `Sandbox::restore_interactive`，而是先起一个带 park 的会话再 `restore_into_session` | `crates/sandlock-supervise/src/serve.rs:1321-1440`、`crates/sandlock-cli` 的 `--restore-from` 入口 | 0.5–1.5 人日，另有 `stats.restored` 与 M0 长驻的语义变化 | **只在出现真实消费者时做** |
| ③ 让 restore stub 承接 exec（在 stub 里实现 init 协议） | `crates/sandlock-core/src/checkpoint/restore-stub.c` + `resume.rs` 的 `StubChannel` | 3–6 人日 | 不做：stub 跑在被恢复的地址空间里，任何分配/锁都是地雷 |
| ④ 在恢复出的进程里"重建 init" | — | — | 不可行：无法把一个陌生地址空间变成 init |

**Files（只有决定门命中才动）:**
- Modify: `crates/sandlock-supervise/src/serve.rs:1321-1440`（`RestoredGeneration::new`；fork 仓）
- Modify: `crates/sandlock-supervise/tests/supervise.rs:3569`（把"按名拒绝 exec"的断言改成"能 exec"；fork 仓）

**Interfaces:**
- Consumes: `SandboxInstance::launch_exec` + `restore_into_session`（会话臂）、`serve.rs` 里 `Generation` 的 `handle_exec`
- Produces: `RestoredGeneration` 的 `exec` 臂不再返回 `Refusal`，而是转发给会话（`stats.restored` 保持为 `true`）

- [ ] **Step 1: 决定门（命中就停在这里，并在 Task E8 的文档里写清结论）**

```bash
cd /Users/polus/project/ai/sandlock-e2b
rg -n "restore-from|restore_from" --glob '!third_party/sandlock/target*' envd_service control_plane deploy tests | head
# Expected: 无输出（0 命中 —— 2026-09-26 已核实）⇒ 生产没有消费者 ⇒ 本任务**不做**，
#           把上面那张候选表补进 docs/checkpoint-restore-e2b-half.md §2 的 D9 行（那一行已更正为
#           "✅ 已做，fork 1f41f1a"）旁边，写明"`--restore-from` 是**另一条** E2B 不消费的路，
#           无消费者、本轮不做"。
#           若出现命中（真的有部署在发 --restore-from）⇒ 继续 Step 2。
```

- [ ] **Step 2: 写会失败的测试（把今天的"按名拒绝"改成"能执行"）**

```rust
// crates/sandlock-supervise/tests/supervise.rs:3569 附近，原来断言的是拒绝原文
//     .contains("exec is not supported on a restored container")
// 改成：从镜像起的 slot 也能 exec
    let echo = registered_verb_args(
        &sock_path,
        token,
        "exec",
        serde_json::json!({ "argv": ["/bin/echo", "RESTORED_EXEC"], "stdio": "piped" }),
    );
    assert_eq!(echo["ok"], serde_json::Value::Bool(true), "exec on a restored slot: {echo:?}");
    let out = read_control_response(&mut stream); // 沿用本文件既有的读帧助手
    assert_eq!(out, serde_json::json!("RESTORED_EXEC\n"));
```

- [ ] **Step 3: 最小实现（会话承载 `--restore-from`）**

```rust
// RestoredGeneration::new：不再直接 restore_interactive，而是"先起一个 park 会话，再把图恢复进去"。
// park 与 route B 用的是同一句（envd_service/route_b.py::PARKING_SCRIPT），M0 退出＝会话结束。
const PARK: &str = "trap '' TERM HUP INT QUIT USR1 USR2 PIPE; while :; do kill -STOP $$; done";

let mut instance = SandboxInstance::launch_exec(policy, &["/bin/sh", "-c", PARK]).await?;
let handle = instance.restore_into_session(&cp).await?;
// stats.restored = true（对外语义不变）、exec 臂转发 handle/child 表（新增）
```

- [ ] **Step 4: 跑测试，确认通过**

```bash
cd /Users/polus/project/ai/sandlock-e2b/third_party/sandlock
IMAGE=sandlock-dev-f17:latest /Users/polus/project/ai/sandlock-e2b/deploy/scripts/fork-gate.sh --one 'test_supervise_restore_from_an_image_resumes_a_serving_slot'
# Expected: test result: ok. 1 passed; 0 failed
IMAGE=sandlock-dev-f17:latest /Users/polus/project/ai/sandlock-e2b/deploy/scripts/fork-gate.sh --one 'test_supervise_restore'
# Expected: 2 passed; 0 failed
```

- [ ] **Step 5: 提交**

```bash
cd /Users/polus/project/ai/sandlock-e2b/third_party/sandlock
git add crates/sandlock-supervise/src/serve.rs crates/sandlock-supervise/tests/supervise.rs
git commit -m "feat(restore-from): the image-started slot serves exec through its session"
cd /Users/polus/project/ai/sandlock-e2b
deploy/scripts/build-sandlock-wheels.sh
git add third_party/sandlock
git commit -m "chore(fork): --restore-from slots keep serving exec"
```

---

### Task E2: 把"抓到了谁"透传到 worker 的日志与回复（E2B）

**依赖**：Task F2 的 wheel（verb 回复里已有 `exe`/`argv`）。

**要钉的是什么**：`capture_checkpoint` 的三层都用了**键白名单**，新键会被安静地丢掉：
`route_b.RouteBInstance.capture_checkpoint` 原样返回（不丢），但
`executors/sandlock.py:1136` 的 `for key in ("dir", "name", "pid", "fds")` 与
`checkpoint_store.py:291-293` 的 `reply["pid"] / reply["fds"]` 都会丢——于是 `pause` 的日志里
永远不会出现"抓到了谁"。本任务打通这两层，并让 `pause` 的日志行直接点名。

**Files:**
- Modify: `envd_service/executors/sandlock.py:1135-1138`（白名单加 `exe`/`argv`；E2B 仓）
- Modify: `envd_service/runtime/checkpoint_store.py:248-258`（成功日志）与 `:271-294`（`_capture_reply`；E2B 仓）
- Modify: `envd_service/agent.py:2864-2872`（`pause` 的那条 info 日志，`_checkpoint_before_pause` 的尾巴；E2B 仓）
- Test: `tests/unit/test_agent_checkpoint_restore.py:161-186`（整份回复相等的那条，必须一起改）、`tests/unit/test_checkpoint_store.py`（新增一条日志/回复断言）

**Interfaces:**
- Consumes: verb 回复的 `exe` / `argv`（Task F2）
- Produces: `/agent/sandboxes/{id}/checkpoint` 的 200 回复新增 `exe: str`、`argv: list[str]`；`pause` 的日志行形如
  `pause of sandbox sbx_x wrote checkpoint <path> (N MiB, pid P, captured /usr/bin/python3 ["python3","-u","/home/user/tick.py"])`

- [ ] **Step 1: 写会失败的测试**

```python
# tests/unit/test_agent_checkpoint_restore.py::_RecordingExecutor.capture_checkpoint
# 把假执行器的回复补上引擎会带的两个键：
        return {
            "captured": True,
            "reason": "",
            "dir": dir,
            "name": name,
            "pid": 4242,
            "fds": 3,
            "exe": "/usr/bin/python3",
            "argv": ["python3", "-u", "/home/user/tick.py"],
        }

# 同文件 test_checkpoint_returns_the_image_and_the_platform_account 的期望整份相等：
    assert resp.json() == {
        "sandbox_id": "sbx_ckpt_ok",
        "captured": True,
        "reason": "",
        "image": str(expected),
        "imageMB": 0,
        "platformDiskUsedMB": 0,
        "platformDiskBudgetMB": 0,
        "pid": 4242,
        "fds": 3,
        # FUP-30：pause 抓到的是 dash 还是 python，必须能从回复里看出来
        "exe": "/usr/bin/python3",
        "argv": ["python3", "-u", "/home/user/tick.py"],
    }
```

再加一条"日志点名"的用例到 `tests/unit/test_checkpoint_store.py`（用 `caplog` 精确比整行）：

```python
def test_the_capture_log_names_the_program_it_captured(tmp_path: Path, caplog) -> None:
    """FUP-30 的教训落在日志上：抓到了谁要写在那一行里，而不是让读者去比内存大小。"""
    base = tmp_path / "sandboxes"
    _sandbox_tree(base, "sbx_named")
    # 假执行器按给定的回复作答 ⇒ 它不写盘，`image_bytes` 因此是 0（日志里的两个 MiB 数
    # 就是 0）；这一条要钉的是"名字"，不是尺寸。
    executor = _FakeExecutor(
        capture_reply={
            "captured": True,
            "reason": "",
            "dir": str(checkpoint_image_dir(base, "sbx_named")),
            "pid": 4242,
            "fds": 3,
            "exe": "/usr/bin/dash",
            "argv": ["/bin/sh", "-c", "sh -c 'exec python3 -c pass'"],
        }
    )
    with caplog.at_level("INFO", logger="envd_service.runtime.checkpoint_store"):
        reply = capture_checkpoint_image(base, _ctx(executor), "sbx_named")

    assert reply["exe"] == "/usr/bin/dash"
    assert reply["argv"] == ["/bin/sh", "-c", "sh -c 'exec python3 -c pass'"]
    # 整行相等（不做子串判据）：日志里点名 dash，读者一眼就知道"抓错对象"了
    assert caplog.messages[-1] == (
        "sandbox sbx_named: checkpoint image written to "
        f"{checkpoint_image_dir(base, 'sbx_named')} (0 MiB, pid 4242, 3 fd(s), "
        "captured /usr/bin/dash ['/bin/sh', '-c', \"sh -c 'exec python3 -c pass'\"]); "
        "the platform account now holds 0 MiB of an unlimited budget"
    )
```

- [ ] **Step 2: 跑它，确认失败**

```bash
cd /Users/polus/project/ai/sandlock-e2b
tmp/testenv/bin/python -m pytest \
  tests/unit/test_agent_checkpoint_restore.py::test_checkpoint_returns_the_image_and_the_platform_account \
  tests/unit/test_checkpoint_store.py::test_the_capture_log_names_the_program_it_captured -q
```

Expected: `2 failed`。第一条是整个 dict 相等，右侧多出 `exe`/`argv` 两项
（`Right contains 2 more items`）；第二条是 `KeyError: 'exe'`。

- [ ] **Step 3: 最小实现（三处白名单 + 一条日志）**

```python
# envd_service/executors/sandlock.py::capture_checkpoint
        outcome: dict = {"captured": True, "reason": ""}
        for key in ("dir", "name", "pid", "fds", "exe", "argv"):
            if key in reply:
                outcome[key] = reply[key]
        return outcome
```

```python
# envd_service/runtime/checkpoint_store.py::_capture_reply
    if capture:
        reply["pid"] = capture.get("pid")
        reply["fds"] = capture.get("fds")
        # FUP-30: 谁被捕获了。空串 / 空表是真实答案（进程在捕获窗口里死了），
        # 不是一个"没接线"的信号。
        reply["exe"] = str(capture.get("exe") or "")
        reply["argv"] = list(capture.get("argv") or [])
    return reply
```

```python
# envd_service/runtime/checkpoint_store.py::capture_checkpoint_image 的成功日志
    logger.info(
        "sandbox %s: checkpoint image written to %s (%s MiB, pid %s, %s fd(s), "
        "captured %s %s); the platform account now holds %d MiB of %s",
        sandbox_id,
        image,
        written // _MIB,
        outcome.get("pid"),
        outcome.get("fds"),
        outcome.get("exe") or "<unknown>",
        list(outcome.get("argv") or []),
        (used_before + written) // _MIB,
        "an unlimited budget" if limit <= 0 else f"{limit // _MIB} MiB",
    )
```

```python
# envd_service/agent.py::_checkpoint_before_pause
        logger.info(
            "pause of sandbox %s wrote checkpoint %s (%s MiB, pid %s, captured %s %s)",
            sandbox_id,
            reply.get("image"),
            reply.get("imageMB"),
            reply.get("pid"),
            reply.get("exe") or "<unknown>",
            reply.get("argv") or [],
        )
```

- [ ] **Step 4: 跑测试，确认通过**

```bash
cd /Users/polus/project/ai/sandlock-e2b
tmp/testenv/bin/python -m pytest tests/unit/test_checkpoint_store.py tests/unit/test_agent_checkpoint_restore.py -q
# Expected: 30 passed（`test_checkpoint_store.py` 18 → 19，加的是上面那条日志用例；
#           `test_agent_checkpoint_restore.py` 仍是 11 —— 它只改了期望的 dict，没加用例）
tmp/testenv/bin/python -m pytest tests/unit/test_sandlock_executor_route_b.py -q
# Expected: 37 passed（2026-09-26 实测的基线；verb 白名单改动不许碰 route-b 的其它用例）
```

- [ ] **Step 5: 提交**

```bash
git add envd_service/executors/sandlock.py envd_service/runtime/checkpoint_store.py envd_service/agent.py \
        tests/unit/test_checkpoint_store.py tests/unit/test_agent_checkpoint_restore.py
git commit -m "feat(checkpoint): the pause log names the process it captured"
```

---

### Task E3: 公开的只读查询 `GET /sandboxes/{id}/checkpoint`（E2B）

**要钉的是什么**：今天对外**零可见度**——`pause`/`resume` 只有 204，
`GET /sandboxes` / `{id}` / `/metrics` 里没有任何 checkpoint 字段（`manager.py:293-341`），
`unrecoveredFds` 只出现在内部端点与 worker 日志里（`agent.py:2905-2917`）。使用者因此无法回答
"我的沙箱有没有图、图多大、上次恢复丢了几个 fd"。

**设计取舍（写进 docstring，免得后来人重开）**：不往控制面的沙箱记录里加字段。记录的每一次
schema 变更都要过 Redis 兼容与 `to_storage_dict`/`from` 两条路，而"最近一次恢复"是**某个
worker 做过的事**——放记录里就成了第二个真相来源。所以：worker 把结果落在**它自己的运行时
目录**（`_runtime/<id>/last-restore.json`，worker 0700，与 `sandbox.json` 并列），控制面
**只代理**（照 `_command_logs` 的写法，`sandboxes.py:674-690`）。

**Files:**
- Modify: `envd_service/runtime/checkpoint_store.py`（新增 `restore_outcome_path` / `record_restore_outcome` / `checkpoint_status`；E2B 仓）
- Modify: `envd_service/agent.py`（新增 `GET /agent/sandboxes/{id}/checkpoint`；在 `_resume_process_tree` 与 `POST …/restore` 的出口调 `record_restore_outcome`；E2B 仓）
- Modify: `control_plane/api/sandboxes.py`（新增公开只读 `GET /sandboxes/{id}/checkpoint`，代理到 worker；E2B 仓）
- Test: `tests/unit/test_checkpoint_store.py`（3 条：无图 / 有图 / 最近一次恢复）、Create `tests/contract/test_checkpoint_status_api.py`

**Interfaces:**
- Consumes: `checkpoint_image_dir` / `image_bytes`（`checkpoint_store.py:72/136`）、`_registry_workspace_base`（`agent.py`）、`_require_internal_key`（worker）、`require_api_key` + `_require_owned` + `request.app.state.nodes`（控制面，照 `_command_logs`）
- Produces: `checkpoint_status(workspace_base, sandbox_id) -> {"sandboxID", "hasImage", "imageMB", "capturedAt", "lastRestore"}`，其中 `lastRestore` 是 `{restored, reason, pid, unrecoveredFdCount, at}` 或 `None`；worker 端点 `GET /agent/sandboxes/{id}/checkpoint`（200/401/404）；控制面端点 `GET /sandboxes/{id}/checkpoint`（200/404）——**只读，不碰 pause/resume 的 204 契约**

- [ ] **Step 1: 写会失败的测试**

```python
# tests/unit/test_checkpoint_store.py
from envd_service.runtime.checkpoint_store import (
    checkpoint_status,
    record_restore_outcome,
)


def test_checkpoint_status_says_there_is_no_image(tmp_path: Path) -> None:
    base = tmp_path / "sandboxes"
    _sandbox_tree(base, "sbx_status")
    assert checkpoint_status(base, "sbx_status") == {
        "sandboxID": "sbx_status",
        "hasImage": False,
        "imageMB": 0,
        "capturedAt": None,
        "lastRestore": None,
    }


def test_checkpoint_status_reports_the_image_and_the_last_restore(tmp_path: Path) -> None:
    base = tmp_path / "sandboxes"
    _sandbox_tree(base, "sbx_status")
    image = checkpoint_image_dir(base, "sbx_status")
    image.mkdir(parents=True)
    (image / "meta.json").write_bytes(b"x" * (2 * MIB))
    record_restore_outcome(
        base,
        "sbx_status",
        {
            "restored": True,
            "reason": "",
            "pid": 31337,
            "unrecoveredFdCount": 2,
        },
    )

    status = checkpoint_status(base, "sbx_status")
    assert status["sandboxID"] == "sbx_status"
    assert status["hasImage"] is True
    assert status["imageMB"] == 2
    assert isinstance(status["capturedAt"], int)
    assert status["lastRestore"] == {
        "restored": True,
        "reason": "",
        "pid": 31337,
        "unrecoveredFdCount": 2,
        "at": status["lastRestore"]["at"],   # 时间戳由实现打，形态精确到秒的 ISO 字符串
    }
```

```python
# tests/contract/test_checkpoint_status_api.py（新建）
"""只读查询：`GET /sandboxes/{id}/checkpoint`（E2B 侧唯一新增的公开面）。

它必须**只读**：pause/resume 的 204 契约一个字都不许动（控制面把 worker 的非 204/404 一律当
502 回滚，`control_plane/api/sandboxes.py:333-338`），所以这条端点是 GET、无副作用、
未接线时给"不知道"而不是报错。
"""

from __future__ import annotations

from gateway_common.paths import sandbox_checkpoint_dir


async def _create_sandbox(control_client) -> dict:
    response = await control_client.post(
        "/sandboxes",
        headers={"X-API-Key": "local-key"},
        json={"templateID": "base", "timeout": 300},
    )
    assert response.status_code == 201
    return response.json()


async def test_a_sandbox_without_an_image_says_so(control_client, workspace) -> None:
    sandbox = await _create_sandbox(control_client)
    sid = sandbox["sandboxID"]
    resp = await control_client.get(
        f"/sandboxes/{sid}/checkpoint", headers={"X-API-Key": "local-key"}
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "sandboxID": sid,
        "hasImage": False,
        "imageMB": 0,
        "capturedAt": None,
        "lastRestore": None,
    }


async def test_an_image_and_a_restore_are_visible(control_client, workspace) -> None:
    sandbox = await _create_sandbox(control_client)
    sid = sandbox["sandboxID"]
    # 造一张图与一次恢复的结果，形状与 worker 真写的一致（读端点只认这两个位置）
    image = sandbox_checkpoint_dir(workspace, sid) / "latest"
    image.mkdir(parents=True)
    (image / "meta.json").write_text("{}", encoding="utf-8")
    runtime_dir = workspace / "_runtime" / sid
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "last-restore.json").write_text(
        '{"restored": true, "reason": "", "pid": 31337, "unrecoveredFdCount": 2, '
        '"at": "2026-09-26T00:00:00+00:00"}',
        encoding="utf-8",
    )

    resp = await control_client.get(
        f"/sandboxes/{sid}/checkpoint", headers={"X-API-Key": "local-key"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["hasImage"] is True
    assert body["lastRestore"]["unrecoveredFdCount"] == 2
    assert body["lastRestore"]["pid"] == 31337


async def test_an_unknown_sandbox_is_404(control_client) -> None:
    resp = await control_client.get(
        "/sandboxes/sbx_does_not_exist/checkpoint", headers={"X-API-Key": "local-key"}
    )
    assert resp.status_code == 404
```

- [ ] **Step 2: 跑它，确认失败**

```bash
cd /Users/polus/project/ai/sandlock-e2b
tmp/testenv/bin/python -m pytest tests/unit/test_checkpoint_store.py -q -k status
# Expected: 2 failed — `ImportError: cannot import name 'checkpoint_status'`
tmp/testenv/bin/python -m pytest tests/contract/test_checkpoint_status_api.py -q
# Expected: 3 failed — 全部 404（`<sid>/checkpoint` 这条路由还不存在；GET 落到
#           `/sandboxes/{id}` 的其它路径上会得到 404）
```

- [ ] **Step 3: 最小实现（存储 → worker 端点 → 控制面代理）**

```python
# envd_service/runtime/checkpoint_store.py
import json
from datetime import datetime, timezone

#: 最近一次恢复的结果，落在 worker 自己的运行时目录里（与 `sandbox.json` 并列）。
LAST_RESTORE_NAME = "last-restore.json"


def restore_outcome_path(workspace_base, sandbox_id: str) -> Path:
    from gateway_common.paths import sandbox_runtime_dir

    return sandbox_runtime_dir(workspace_base, sandbox_id) / LAST_RESTORE_NAME


def record_restore_outcome(workspace_base, sandbox_id: str, outcome: dict) -> None:
    """记住这次恢复的结果（D5/D6 的对外一半：丢掉的 fd 要能被看见）。

    写盘而不是只放内存：`resume` 之后 worker 可能再重启一次，而"上一次恢复丢了几个 fd"
    恰恰是排查时才要读的东西。同一目录内 `os.replace`，所以读到的永远是完整的一份。
    """
    record = restore_outcome_path(workspace_base, sandbox_id)
    try:
        record.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "restored": bool(outcome.get("restored")),
            "reason": str(outcome.get("reason") or ""),
            "pid": outcome.get("pid"),
            "unrecoveredFdCount": int(outcome.get("unrecoveredFdCount") or 0),
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        tmp = record.with_name(record.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, record)
    except OSError:  # pragma: no cover - 一个读数不能把 resume 弄失败
        logger.warning(
            "sandbox %s: could not record the restore outcome", sandbox_id, exc_info=True
        )


def checkpoint_status(workspace_base, sandbox_id: str) -> dict:
    """这个沙箱的图与最近一次恢复，按只读查询的形状回答。"""
    image = checkpoint_image_dir(workspace_base, sandbox_id)
    has_image = image.is_dir()
    captured_at: int | None = None
    if has_image:
        try:
            captured_at = int(image.stat().st_mtime)
        except OSError:
            captured_at = None
    last: dict | None = None
    try:
        last = json.loads(
            restore_outcome_path(workspace_base, sandbox_id).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        last = None
    return {
        "sandboxID": sandbox_id,
        "hasImage": has_image,
        "imageMB": (image_bytes(image) // _MIB) if has_image else 0,
        "capturedAt": captured_at,
        "lastRestore": last,
    }
```

```python
# envd_service/agent.py —— GET 版（与 POST 版同一套投递契约：401 / 404）
@router.get("/agent/sandboxes/{sandbox_id}/checkpoint")
async def agent_checkpoint_status(sandbox_id: str, request: Request) -> Response:
    """只读：这个 worker 手上关于这张图的事实（图在不在、多大、上次恢复丢了几个 fd）。"""
    settings = request.app.state.settings
    try:
        _require_internal_key(request, settings)
    except PermissionError:
        return Response(status_code=401)
    runtime = request.app.state.runtime_registry.get(sandbox_id)
    if runtime is None:
        return Response(status_code=404)
    reply = await asyncio.to_thread(
        checkpoint_status,
        _registry_workspace_base(request.app.state.runtime_registry, settings),
        sandbox_id,
    )
    return JSONResponse(reply)


# 在 `_resume_process_tree` 成功/失败两条出口都记一笔（失败也要记：reason 才是使用者要的）
        await asyncio.to_thread(
            record_restore_outcome,
            _registry_workspace_base(request.app.state.runtime_registry, settings),
            sandbox_id,
            reply,
        )
```

```python
# control_plane/api/sandboxes.py —— 照 `_command_logs`（:674-690）的代理写法
@router.get("/sandboxes/{sandbox_id}/checkpoint", dependencies=[Depends(require_api_key)])
async def checkpoint_status_sandbox(sandbox_id: str, request: Request) -> dict[str, Any]:
    """只读：这个沙箱的 checkpoint 图与最近一次恢复。

    远端节点上问那个 worker（它才是知道这件事的人），`local://` 直接读本地 store。
    未接线（通道不通、节点不认识）时给"不知道"，**不**报错：这条端点不许改变
    pause/resume 的投递契约，也不该让一个诊断查询变成新的失败点。
    """
    registry = _registry(request)
    try:
        record = registry.get(sandbox_id)
        _require_owned(request, record, resource_id=sandbox_id, label="Sandbox")
    except UnknownSandboxError:
        raise OfficialError(404, f"Sandbox {sandbox_id} not found")
    node = request.app.state.nodes.get(record.node_id or "local")
    if node is not None and node.address != "local://":
        import httpx

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{node.address}/agent/sandboxes/{sandbox_id}/checkpoint",
                    headers={
                        "X-Internal-Key": request.app.state.settings.internal_api_key
                    },
                )
            if resp.status_code == 200:
                payload = resp.json()
                if isinstance(payload, dict) and "hasImage" in payload:
                    return payload
        except (httpx.HTTPError, ValueError):
            pass
        return {
            "sandboxID": sandbox_id,
            "hasImage": False,
            "imageMB": 0,
            "capturedAt": None,
            "lastRestore": None,
            "unreachable": True,
        }
    from envd_service.runtime.checkpoint_store import checkpoint_status

    return checkpoint_status(request.app.state.workspace_base, sandbox_id)
```

- [ ] **Step 4: 跑测试，确认通过**

```bash
cd /Users/polus/project/ai/sandlock-e2b
tmp/testenv/bin/python -m pytest tests/unit/test_checkpoint_store.py tests/unit/test_agent_checkpoint_restore.py -q
# Expected: 32 passed（`test_checkpoint_store.py` 18 + E2 的 1 条日志用例 + 本任务 2 条 status
#           用例 = 21；`test_agent_checkpoint_restore.py` 11）
tmp/testenv/bin/python -m pytest tests/contract/test_checkpoint_status_api.py -q
# Expected: 3 passed
tmp/testenv/bin/python -m pytest tests/contract/test_pause_write_gating.py tests/contract/test_pause_resume_sandlock_multinode.py -q
# Expected: 0 failed（204 契约一条都没动）
```

- [ ] **Step 5: 提交**

```bash
git add envd_service/runtime/checkpoint_store.py envd_service/agent.py control_plane/api/sandboxes.py \
        tests/unit/test_checkpoint_store.py tests/contract/test_checkpoint_status_api.py
git commit -m "feat(checkpoint): a read-only endpoint for the image and the last restore"
```

---

### Task E4: 孤儿图回收 + 拒绝时不留空目录（E2B）

**要钉的两个洞**：
1. `_runtime/.checkpoints/<id>` 属于平台（图是平台持有最大的东西），但它的**候选集只来自内存
   注册表与顶层沙箱树**（`agent.py:2049-2078`、`:1398-1435`）——`.checkpoints/*` 自己没有
   扫描入口。于是"没有记录的孤儿图"（记录被删、图没删；或捕获写完图、记录随后消失）会永远
   占着平台账，直到账满拒新捕获。
2. `_prepare_image_parent`（`checkpoint_store.py:77-112`）先建目录、再判尺寸；捕获被拒或
   记账被拒时，`_remove_image` 只删 `latest`，**留下空的 `<id>` 目录**（捕获被拒那条连
   `latest` 都没写，空目录直接留着）。

**Files:**
- Modify: `envd_service/runtime/checkpoint_store.py`（`list_checkpoint_stores` / `remove_orphan_checkpoint_stores` / `_discard_empty_store`；E2B 仓）
- Modify: `envd_service/agent.py:2128-2136`（reconcile 的循环之后加一次孤儿图清扫；E2B 仓）
- Test: `tests/unit/test_checkpoint_store.py`（3 条）、`tests/unit/test_quota_maintenance.py`（1 条）

**Interfaces:**
- Consumes: `sandbox_checkpoint_dir`（`gateway_common/paths.py:166`）、`priv_helpers.remove_tree`（`:759`）、现成的 teardown 纪律（`_delete_sandbox_runtime` 两步：先 `_runtime/<id>`、再 `remove_checkpoint_images`）
- Produces: `list_checkpoint_stores(workspace_base) -> list[str]`（**只列**，不删）、`remove_orphan_checkpoint_stores(workspace_base, keep) -> list[str]`（返回删掉的 id）、`checkpoint_store_is_empty(image) -> bool`

- [ ] **Step 1: 写会失败的测试**

```python
# tests/unit/test_checkpoint_store.py
def test_an_image_with_no_record_is_reported_as_an_orphan(tmp_path: Path) -> None:
    base = tmp_path / "sandboxes"
    _sandbox_tree(base, "sbx_orphan")
    image = checkpoint_image_dir(base, "sbx_orphan")
    image.mkdir(parents=True)
    (image / "meta.json").write_bytes(b"x")
    assert checkpoint_store.list_checkpoint_stores(base) == ["sbx_orphan"]
    # 双判据：只有 "CP 不认识它" 时才算孤儿 —— keep 里有它，就必须原样留着
    assert checkpoint_store.remove_orphan_checkpoint_stores(base, keep={"sbx_orphan"}) == []
    assert image.is_dir()
    assert checkpoint_store.remove_orphan_checkpoint_stores(base, keep=set()) == ["sbx_orphan"]
    assert not image.is_dir()


def test_a_refused_capture_leaves_no_empty_directory(tmp_path: Path) -> None:
    base = tmp_path / "sandboxes"
    _sandbox_tree(base, "sbx_noroom")
    executor = _FakeExecutor(capture_reply={"captured": False, "reason": "1 live child"})
    reply = capture_checkpoint_image(base, _ctx(executor), "sbx_noroom")
    assert reply == {
        "sandbox_id": "sbx_noroom",
        "captured": False,
        "reason": "1 live child",
        "image": None,
        "imageMB": 0,
        "platformDiskUsedMB": 0,
        "platformDiskBudgetMB": 0,
    }
    # 拒绝不是"半个动作"：目录树必须与调用前逐字节相同
    assert not checkpoint_image_dir(base, "sbx_noroom").parent.exists()


def test_an_image_refused_by_the_account_is_removed_with_its_store(tmp_path: Path, monkeypatch) -> None:
    base = tmp_path / "sandboxes"
    _sandbox_tree(base, "sbx_over")
    monkeypatch.setenv("E2B_PLATFORM_DISK_MB", "1")
    executor = _FakeExecutor(image_bytes=2 * MIB)
    reply = capture_checkpoint_image(base, _ctx(executor), "sbx_over")
    assert reply["captured"] is False
    assert reply["imageMB"] == 0
    assert not checkpoint_image_dir(base, "sbx_over").parent.exists()
```

```python
# tests/unit/test_quota_maintenance.py —— reconcile 把孤儿图算进清扫结果
def test_reconcile_collects_an_image_whose_owner_is_gone(tmp_path: Path, monkeypatch) -> None:
    """图是平台持有最大的东西：没有记录的孤儿图必须被同一个 reconcile 收走。"""
    base = tmp_path / "sandboxes"
    _sandbox_tree(base, "sbx_gone")            # 已回收的沙箱遗留
    image = checkpoint_image_dir(base, "sbx_gone")
    image.mkdir(parents=True)
    (image / "meta.json").write_bytes(b"x")
    summary = _run_reconcile_round(base, known=set(), monkeypatch=monkeypatch)
    assert summary["checkpointsReclaimed"] == ["sbx_gone"]
    assert not image.parent.exists()
```

（`_run_reconcile_round` 是 `test_quota_maintenance.py` 里已有的 reconcile 驱动程序；
若该文件没有这个助手，就用它现有的同族助手并只加这条断言。）

- [ ] **Step 2: 跑它，确认失败**

```bash
cd /Users/polus/project/ai/sandlock-e2b
tmp/testenv/bin/python -m pytest tests/unit/test_checkpoint_store.py -q -k "orphan or empty_directory or account"
# Expected: 3 failed — `AttributeError: module 'envd_service.runtime.checkpoint_store'
#           has no attribute 'list_checkpoint_stores'`，以及两条"空目录还在"的断言失败
```

- [ ] **Step 3: 最小实现**

```python
# envd_service/runtime/checkpoint_store.py
def list_checkpoint_stores(workspace_base) -> list[str]:
    """`_runtime/.checkpoints/` 下有图的沙箱 id（只列，不删）。"""
    from gateway_common.paths import RUNTIME_DIR_NAME

    root = Path(workspace_base) / RUNTIME_DIR_NAME / ".checkpoints"
    if not root.is_dir():
        return []
    try:
        return sorted(
            entry.name for entry in root.iterdir() if entry.is_dir() and entry.name != "latest"
        )
    except OSError:  # pragma: no cover - 读不到就是"没有候选"
        return []


def remove_orphan_checkpoint_stores(workspace_base, *, keep: set[str]) -> list[str]:
    """删掉**不在 keep 里**的图；返回真删掉的 id。

    调用方给的 `keep` 必须同时包含"控制面还认识的 id"与"这个 worker 内存/磁盘上还有记录的
    id"（双判据）。图是平台为一个沙箱持有的最大东西，误删一张就是丢一个用户的状态，
    所以这里的默认是**不删**：只要有任何一处还认领它，就留着。
    """
    removed: list[str] = []
    for sandbox_id in list_checkpoint_stores(workspace_base):
        if sandbox_id in keep:
            continue
        logger.warning("checkpoint store of unknown sandbox %s: reclaiming", sandbox_id)
        remove_checkpoint_images(workspace_base, sandbox_id)
        removed.append(sandbox_id)
    return removed


def _discard_empty_store(image: Path) -> None:
    """把 `_prepare_image_parent` 建出来、但**没能装进一张图**的那层目录收回去。

    拒绝路径今天留下一个空 `<id>/`：账上量得到它、没人认领它，而且下一次捕获会以为
    "目录已经就绪"。只在目录真的是空的时候删，绝不碰有内容的 store。
    """
    store = image.parent
    try:
        store.rmdir()
    except OSError:
        pass
```

```python
# 两处拒绝路径都补一句（顺序：先删图，再收空目录）
    if not outcome.get("captured"):
        reason = str(outcome.get("reason") or "the slot did not capture")
        _discard_empty_store(image)
        return _capture_reply(sandbox_id, False, reason, used=used_before, limit=limit)
...
    if not allowed:
        _remove_image(image)
        _discard_empty_store(image)
        logger.warning(...)
```

```python
# envd_service/agent.py —— reconcile 的 `deleted` 循环之后（`:2128` 一带）
        # 图是平台持有最大的东西，而它的候选集从前只来自内存注册表与顶层树；`.checkpoints/*`
        # 自己没有入口 ⇒ 一个"记录没了、图还在"的孤儿会永远占账。双判据：控制面认识的、
        # 以及这一轮本地还认领的，都不动。
        reclaimed_checkpoints = await asyncio.to_thread(
            remove_orphan_checkpoint_stores,
            self._settings.workspace_base,
            keep=set(known) | set(local) | set(scanned) | concurrent_creates,
        )
        if reclaimed_checkpoints:
            logger.warning(
                "reconcile: reclaimed %d checkpoint store(s) with no owner: %s",
                len(reclaimed_checkpoints),
                ",".join(reclaimed_checkpoints),
            )
```

（`reclaimed_checkpoints` 一并进 reconcile 的回报字段 `checkpointsReclaimed`，与既有的
`deleted` / `untrusted_records`（`envd_service/agent.py:2265` 的 summary 里就是这个拼法）同一层级。）

- [ ] **Step 4: 跑测试，确认通过**

```bash
cd /Users/polus/project/ai/sandlock-e2b
tmp/testenv/bin/python -m pytest tests/unit/test_checkpoint_store.py tests/unit/test_quota_maintenance.py -q
# Expected: 75 passed（`test_checkpoint_store.py` 18 + E2 的 1 条 + E3 的 2 条 + 本任务 3 条 = 24；
#           `test_quota_maintenance.py` 50 + 本任务 1 条 = 51 —— 50 是 2026-09-26 实测的基线）
tmp/testenv/bin/python -m pytest tests/contract/test_orphan_tree_gc.py -q
# Expected: 0 failed（reconcile 的既有语义一条都没松）
```

- [ ] **Step 5: 提交**

```bash
git add envd_service/runtime/checkpoint_store.py envd_service/agent.py \
        tests/unit/test_checkpoint_store.py tests/unit/test_quota_maintenance.py
git commit -m "fix(checkpoint): reclaim ownerless images, and leave no empty store behind"
```

---

## E5–E8 收口审计（2026-09-27）

**这是一节审计，不是新设计。** 本计划正文写到 `Task E4` 为止，`Task E5`/`E6`/`E7`/`E8`
**没有任何一节正文**——它们只出现在上面的**依赖表**（`:53-59`）、**决策点表**（`:96-108`）
与**验收矩阵**里。于是"E5–E8 今天各自到哪一步"在账面上没有答案。这一节把每一处引用逐条
摘出来，到树里取证据（`rg` 具体符号，不凭印象），给处置；**已经拍过决定的补写进文档，
没拍板的登记待决策，绝不为它们发明形状**。逐条原文摘录、判理由与文件清单见
`.superpowers/sdd/checkpoint-e5-e8-audit-report.md`。

### 决策点表（`:96-108`）7 行

| 决策 | 落在哪 | 现状证据（文件:行） | 处置 |
|---|---|---|---|
| 1（余项）OCI / `--restore-from` 要不要也支持 exec | Task F4 决定门 / Task 2 Step 3 ③ | `rg -n "restore-from\|restore_from" envd_service control_plane deploy tests` = **0 命中**（2026-09-27 复核）；结论此前只落在 gitignored 的 `.superpowers/sdd/progress.md:2319` | **随裁定取消（不做）**；结论已补进 `docs/checkpoint-restore-e2b-half.md` §2 D9 行 + §6(k)⑤ |
| 2 恢复后进程 stdout 是否接平台日志（今天 `/dev/null`） | Task E8 | 机制有据（`envd_service/route_b.py:332`、`deploy/k8s/worker.yaml:490-492`、`docs/checkpoint-restore-e2b-half.md` §6(j)），**对外语义全库无** | **已补写** → `docs/checkpoint-restore-e2b-half.md` §6(k)① |
| 3 平台账"软账 + 并发可超"，暴露 `used/budget` + 告警 | Task E7（暴露）/ E8（口径） | 数字已上报节点视图（`envd_service/agent.py:265-266` → `control_plane/api/internal.py:106-107` → `control_plane/registry/nodes.py:206-207`），worker 回复也带（`envd_service/runtime/checkpoint_store.py:473-474`）；**公开端点不带**（`control_plane/api/sandboxes.py:2825-2832` → `checkpoint_store.py:302-308`）；口径句只在计划 `:102`；**无告警**（`rg 'alert\|PrometheusRule' deploy/` 0 命中） | **部分**：数字可见 ✔ / 口径**已补写** §6(k)② / **告警未做 → 登记 `docs/open-issues.md`** |
| 4 paused 是否要有 TTL（`E2B_PAUSED_TTL_S`，默认 0） | Task E6 | `rg 'E2B_PAUSED_TTL_S' .` = **只命中本计划 `:103`**（无实现、无替代语义） | **未做 + 待决策** → 登记 `docs/open-issues.md`（**默认 0 = 不启用**） |
| 5 新增公开只读 `GET /sandboxes/{id}/checkpoint` | Task E3 | `control_plane/api/sandboxes.py:2764-2832`；契约 `tests/contract/test_checkpoint_status_api.py` | **已做 ✔** |
| 6 把 `tmp/k0s/checkpoint_acceptance.py` 转正进仓库 | Task 2 | `deploy/scripts/checkpoint_acceptance.py`（前置断言 `:379-380`） | **已做 ✔** |
| 7 `E2B_PAUSE_CHECKPOINT` 长期默认 `"1"`，代价写进文档 | Task E8 | 清单 `deploy/k8s/worker.yaml:483-484`（`"1"`）+ 代价注释 `:455-492`；**代码默认仍关**（`envd_service/config.py:378`） | **已做 ✔**（清单注释即代价）；"长期默认开"的结论补进 §6(k)④ |

### 依赖表（`:53-59`）里 E5–E8 的引用

| 引用 | 现状证据（文件:行） | 处置 |
|---|---|---|
| `E4 → E8`（"文档要写'孤儿会回收'"） | 代码已做：`envd_service/runtime/checkpoint_store.py:640/681/714`、`envd_service/agent.py:2345`（reconcile 里调）、`:2423`（`checkpointsReclaimed`）；用例 `tests/unit/test_quota_maintenance.py:1686`。**文档无**（`rg '孤儿\|orphan\|回收' docs/checkpoint-restore-e2b-half.md docs/deploy-clusters.md` 0 命中） | 代码 ✔ / 文档**已补写** §6(k)③ |
| `E5 → E8` | **本计划从未定义 `Task E5`**——`rg -n 'E5'` 全计划只有 `:38`/`:44`/`:56`/`:59` 四处*引用*，其中 `:56` 就是依赖表这一行 | **计划缺口 → 登记 `docs/open-issues.md`**（要先有形状才能判"做没做"） |
| `E6 → E8` | 同决策 4 | **未做 + 待决策** |
| `E7 → E8` | 同决策 3（"暴露数字"那半已在 S2 落地，见 §6(f)）；端到端验收已由 Task 2 的 `checkpoint_acceptance.py` 转正 | **部分**（告警未做 → 登记） |
| `E8 → 全部`（文档/守卫收口） | 四件事：① 守卫 `docstring` 与 `FORBIDDEN` **已逐条验与事实一致 ✔**（见下）；② 对外语义未落盘 → 本轮补 §6(k)；③ 守卫**确实会红 ✔**，但空树静默绿 → 本轮加非空断言（RED→GREEN）；④ F3/F4 结论未落盘 → 本轮补 | ①②③④ 本轮收口，详见各行 |

### Task E8 的守卫项：`tests/unit/test_checkpoint_restore_unused.py`

计划 `:21` 与 Global Constraints 说"Task E8 必须把它的 docstring 改到与事实一致"。**验下来
它今天已经一致**（不需要改），逐条：

| docstring 的说法 | 事实（文件:行） |
|---|---|
| stub 靠描述符投递（`execveat(AT_EMPTY_PATH)`）、规则集给那一个宿主文件 `EXECUTE\|READ_FILE` | fork `crates/sandlock-core/src/sandbox.rs:1445-1463` |
| `test_restore_resumes_inside_a_chroot_root` 两种根都跑、断言计数器前进且 fd 表干净 | 用例 `crates/sandlock-core/tests/integration/test_restore.rs:192`（`:199` 循环两态、`:283-294` 断言） |
| 引擎覆盖 x86_64 / aarch64 / riscv64；stub 有 `__aarch64__`；build.rs 对缺失 stub 判 fatal | `sandbox.rs:1392-1398`、`restore-stub.c:2/85`、`crates/sandlock-core/build.rs:47-58` |
| E2B 侧已建（S2/S3/S4） | `docs/checkpoint-restore-e2b-half.md` §3/§6 |

**唯一发现的洞是"看着在守、其实没守"的空转**：`offenders == []` 也是"什么都没扫"的返回值，
所以 `envd_service/` 一旦改名（或不再有 `*.py`），这条守卫会**静默变绿**。已按 TDD 收口：
`_worker_sources`/`_offenders` 抽成可传入根目录的助手，加两条用例——① 真树必须非空
（`test_the_scan_actually_reads_the_worker_tree`）；② 合成树里的 `.checkpoint(` 调用点必须被
判成 offender（`test_the_scan_flags_a_call_site_in_a_synthetic_tree`，`assert _offenders(...) ==
["envd_service/bad.py: .checkpoint("]`，整表相等）。RED = 空树静默通过（探针
`tmp/e5e8/guard_probe.py`）；GREEN = 两条用例在真树上通过（`tmp/e5e8/guard_probe2.py`）。

### 两条条件任务的决定门（`Task F3` / `Task F4`）

| 任务 | 决定门 | 今天的结论 | 处置 |
|---|---|---|---|
| F3 | `rg "os\.replace\|mv \|rename" docs/checkpoint-restore-e2b-half.md docs/k8s-deployment.md` —— 出现"业务文件会被 replace"才做 | 命中只有本能力自己的 rename（引擎保存 `<dir>.tmp`→`latest`、验收脚本"临时文件 + `os.replace`"）与磁盘记账的 rename，**无业务文件被 replace 的用法** | **不做（默认）**；已复核并把"同路径换 inode 不校验"这句**已知边界**补进 §6(k)⑤ |
| F4 | `rg "restore-from\|restore_from" envd_service control_plane deploy tests` —— 有命中才做 | **0 命中**（无消费者） | **不做（默认）**；结论补进 §2 D9 行 + §6(k)⑤ |
