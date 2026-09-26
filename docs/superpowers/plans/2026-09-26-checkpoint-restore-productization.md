# checkpoint/restore 产品化实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把已经能用、集群验收全绿的 checkpoint/restore 变成**有对外语义、有可观测性、有回收路径、有守卫**的产品功能——`pause` 写下的进程镜像能在 worker 重启后回来、能继续 `exec`、能被用户看见、不会永远占着平台账。

**Architecture:** 不改传送机制，只补齐四件缺的东西：①用**生产形态的集群验收**回答"今天到底能不能用"（Task E1，第一道题）；②把 fork 侧两条**没被任何用例覆盖的边界**钉住（会话恢复 × 纯真根 / 恢复后还能 exec，Task F1）与一条**可见性缺口**补上（`exe`/`argv`，Task F2）；③E2B 侧补**只读查询端点**、**孤儿图回收**、**paused 的过期策略**、**计数指标**（Task E2–E7）；④把文档、清单注释与守卫用例的定位收口（Task E8）。fork 侧的改动必须先重建 wheel，E2B 才会用上（见"执行顺序"）。

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
- **判断"线上跑的是哪一版"只认两处**：`deploy/stack/.version`（当前 `0.1.0-535-g2f38991-20260926-090454`）与集群里 `control-plane` / `autoscaler` / `e2b-worker` 三个工作负载的实际镜像；`docs/deploy-clusters.md` 里写死的版本号只是历史记录。
- **动集群前先认集群**：`deploy/scripts/open-cluster-tunnel.sh`（自检 2 节点 / arm64 / 含 `+k0s`），然后 `export KUBECONFIG="$PWD/tmp/k0s/kubeconfig"`；不带 `KUBECONFIG` 的 `kubectl` 会安静地指向另一套阿里云 ACK 集群。
- **`pause` / `resume` 推送的 204 契约不许动**：控制面把 worker 的**非 204/404** 一律当 502 回滚（`control_plane/api/sandboxes.py:317-340`）；checkpoint/restore 的一切"没有图/没有会话/账满/引擎拒绝"都必须是**正常答案**（带 reason），不得改状态码。
- **承重顺序不许动**：`pause` 必须在**冻结之前**捕获（引擎的捕获自己会 `SIGSTOP`→`SIGCONT`，先冻再捕获等于把 pause 撤销）；`resume` 必须在**发布状态之前**恢复。

---

## 执行顺序与编号

- **fork 侧任务**：`Task F1`–`Task F4`（改动落在 `third_party/sandlock`，单独提交，跑 fork 门禁）。
- **E2B 侧任务**：`Task E1`–`Task E8`（改动落在主仓，跑 pytest + 容器 lane）。
- **顺序（硬要求）**：
  1. **`Task E1` 必须先做**——它是"生产形态今天能不能用"的判据，也是后面所有任务的基线；它不依赖任何代码改动。
  2. 再 `Task F1` → `Task F2`（fork 侧），**每个 fork 任务结束后跑一次 `deploy/scripts/build-sandlock-wheels.sh`**，否则 E2B 侧看到的还是旧引擎。
  3. `Task F3` / `Task F4` 是**条件任务**（各自的第一个 step 是决定门），不满足条件就不做。
  4. 然后 `Task E2` → `Task E8`（E2 依赖 F2 的 wheel，E7 的端到端验收依赖 E2/E3/E4 全部落地）。
- **依赖图**（谁需要谁先落地）：

| 任务 | 依赖 | 被谁依赖 |
|---|---|---|
| E1 | 无 | 所有任务的基线 |
| F1 | 无 | E1 红时的定因输入 |
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
| `core_integ`（会话恢复两态 + 恢复后 exec） | `deploy/scripts/fork-gate.sh --one 'test_restore::'` 与 `--one 'test_instance'` | `0 failed`；`test_restore` 5 条、`test_instance*` 50 条（基线 §`core_integ`） |
| `supervise`（`checkpoint` / `restore` verb） | `deploy/scripts/fork-gate.sh --one 'test_supervise_checkpoint'` / `--one 'test_supervise_restore'` | 4 条相关用例 `0 failed`（`supervise = 55` 不失配） |
| 全档（提交前） | `IMAGE=sandlock-dev-f17:latest deploy/scripts/fork-gate.sh` | 每个相位 `passed -- baseline says N`；无 `suite FAILED` |
| 计数守卫 | `grep -n '^core_integ = ' third_party/sandlock/docs/test-baseline.md` | 加了用例就等于新值（F1：`560` → `561`），否则门禁判红 |

### E2B 侧（主仓）

| 档位 | 命令 | 期望 |
|---|---|---|
| 单测（checkpoint 家族） | `tmp/testenv/bin/python -m pytest tests/unit/test_checkpoint_store.py tests/unit/test_agent_checkpoint_restore.py tests/unit/test_sandlock_executor_route_b.py -q` | 全绿（现基线：18 + 11 + 25 条） |
| 契约（生命周期） | `tmp/testenv/bin/python -m pytest tests/contract/test_pause_write_gating.py tests/contract/test_pause_resume_sandlock_multinode.py -q` | 全绿 |
| 新增契约（只读查询） | `tmp/testenv/bin/python -m pytest tests/contract/test_checkpoint_status_api.py -q` | 全绿（E3 新建） |
| 容器 lane（镜像形态） | `tmp/k0s/gateA-full.sh tmp/k0s/e-plan-gateA.log` | **1772 passed / 6 skipped / 3 xfailed / 0 failed**（只加测试时同步上浮） |
| 容器 lane（pure 形态） | `tmp/k0s/gateB-full.sh tmp/k0s/e-plan-gateB.log` | **1765 passed / 13 skipped / 3 xfailed / 0 failed** |

### 集群侧（生产形态：image-rootfs + `E2B_REAL_ROOT=1`）

| 档位 | 命令 | 期望 |
|---|---|---|
| 通道与身份 | `deploy/scripts/open-cluster-tunnel.sh --check` | `✓ 2 节点 / arm64 / 含 +k0s` |
| 版本对齐 | `KUBECONFIG=$PWD/tmp/k0s/kubeconfig kubectl -n sandlock get deploy,sts -o jsonpath='{..image}' \| tr ' ' '\n' \| sort -u` | 只有 `…:$(cat deploy/stack/.version)` 一族 |
| 生产形状验收 | `KUBECONFIG=$PWD/tmp/k0s/kubeconfig .venv/bin/python deploy/scripts/checkpoint_acceptance.py` | 逐步 JSON，末行 `{"step": "OK"}`，退出码 `0` |
| 其中四条硬判据 | 同上输出里的这四行 | `{"step":"image"…}` 含 `meta.json`；`{"step":"resumed","before":N,"after":M}` 且 `M-N < 30`；`{"step":"exec_after_resume","stdout":"EXEC_OK\n"}`；`{"step":"image_consumed"…}` 含 `No such file or directory` |

---

## 必须先拍板的决策点（每一行都要人给答案）

| # | 决策 | 计划里的默认（不拍板就按这个走） | 落在哪 |
|---|---|---|---|
| 1 | **D9 是否按"恢复进会话"封板**（接受"能继续 exec，但原连接/stdout 不回来"） | 按"封板"走：现有形状即答案，不再为 `--restore-from` 那条无消费者的模式投人 | Task F4（不满足条件就不做）/ Task E8 文档 |
| 2 | **恢复后进程的 stdout 是否接到平台日志**（今天进 `/dev/null`） | 不接，只**写进对外语义**（"恢复的沙箱日志消失"） | Task E8 |
| 3 | **平台账接受"软账 + 并发可超"，还是做跨节点硬账** | 接受软账：保持"整个 `_runtime`"口径 + 把 `used/budget` 暴露 + 告警 | Task E7（暴露数字）/ Task E8（写清口径） |
| 4 | **paused 是否要有 TTL 以及多久**（会摧毁用户状态） | `E2B_PAUSED_TTL_S` 默认 **0 = 不启用**；只在拍板后打开 | Task E6 |
| 5 | 是否新增**公开端点** `GET /sandboxes/{id}/checkpoint`（API 契约扩张，要和 SDK 对齐） | 新增（只读、不碰 204 契约） | Task E3 |
| 6 | 是否把 `tmp/k0s/checkpoint_acceptance.py` **转正进仓库** | 转正（它现在是唯一的生产形态验收，却躺在被 `.gitignore` 忽略的 `tmp/` 里） | Task E1 |
| 7 | `E2B_PAUSE_CHECKPOINT` 是否长期默认开（它让 `pause` 变成"写整个进程内存"的动作） | 保持清单里 `"1"`，并把代价写进文档 | Task E8 |

---

### Task E1: 生产形态到底能不能用 —— 把集群验收搬进仓库并跑出判据

**为什么是第一道题**：S2–S4 落地后**只跑过两次**生产形态验收（`0.1.0-525` / `0.1.0-527`，见
`docs/deploy-clusters.md:209-291`），而仓库现在已经是 `0.1.0-535`（含 N15 的 `_chroot_root` 改动、
F11 的多副本、两次 fork 侧修复）。那两次验收的脚本本身也躺在 `tmp/k0s/`（被 `.gitignore`
忽略 ⇒ **不在仓库里**），所以"今天还能不能用"和"这条能力有没有验收"这两件事现在都没有答案。
这一步先把脚本转正，再拿它当判据——它绿，后面所有任务才有基线；它红，红在哪一条断言就决定
接下来改哪一层（本任务 Step 3 的分层表）。

**Files:**
- Create: `deploy/scripts/checkpoint_acceptance.py`（源：`tmp/k0s/checkpoint_acceptance.py`，437 行，E2B 仓）
- Modify: `docs/deploy-clusters.md:270-280`（§9 的"怎么再跑一遍"改成指仓库内的脚本）
- Test: 脚本自身（每步 `assert`，末行 `{"step": "OK"}`）

**Interfaces:**
- Consumes: `deploy/scripts/open-cluster-tunnel.sh`；`deploy/k8s-k0s/apply.sh`；`deploy/stack/.version`；集群 secret `sandlock/e2b-secrets` 的 `E2B_API_KEYS` / `E2B_INTERNAL_API_KEY`；worker 清单里的 `E2B_REAL_ROOT=1`、`E2B_PAUSE_CHECKPOINT=1`、`E2B_PLATFORM_DISK_MB=8192`（`deploy/k8s/worker.yaml:314/340/342`）；`.venv` 里的 `e2b` SDK（`tmp/testenv` 没有 `e2b`，脚本必须用 `.venv/bin/python` 跑）
- Produces: `deploy/scripts/checkpoint_acceptance.py`——后续每个 E2B 任务的端到端验收都调它；`tmp/k0s/checkpoint-e1.log`（这一步的判据证据）

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
.venv/bin/python deploy/scripts/checkpoint_acceptance.py | tee tmp/k0s/checkpoint-e1.log
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

# ② 红在 resumed（恢复了但计数不动）⇒ 先分清引擎 / 部署：本机容器 lane 跑同形状
cd third_party/sandlock
IMAGE=sandlock-dev-f17:latest /Users/polus/project/ai/sandlock-e2b/deploy/scripts/fork-gate.sh \
  --one 'test_a_dynamic_workload_resumes_into_a_session_under_a_real_root'
# 期望：test result: ok. 1 passed; 0 failed
#   绿 ⇒ 引擎在"动态 + 真根 + 会话 + restore"这个形状上是对的，红的是部署侧（Task E2 的输入）
#   红 ⇒ 引擎回归，把它当 P0，按引擎侧门禁的纪律留红的日志再单跑

# ③ 红在 exec_after_resume / worker_log ⇒ 恢复没进会话（exec 会被按名拒绝）
kubectl -n sandlock logs e2b-worker-0 | grep -E 'restore refused|restore-stub was not built' | tail -5
# 期望无输出；出现 `restore-stub was not built` ⇒ wheel 没带 stub（fork 2d5f2e9 之后不该出现）

# ④ 红在 kubectl 的报错上（`kubectl exec` 打不出图、`kubectl delete pod` 超时）⇒ 通道，不是功能
deploy/scripts/open-cluster-tunnel.sh --check
```

- [ ] **Step 4: 把绿的日志留成证据，并把结论写进文档**

```bash
cd /Users/polus/project/ai/sandlock-e2b
grep -c '^{"step"' tmp/k0s/checkpoint-e1.log      # 期望 >= 10（脚本每一步都打一行）
tail -1 tmp/k0s/checkpoint-e1.log                 # 期望：{"step": "OK"}
```

在 `docs/deploy-clusters.md` §9 顶部的"**版本**"行改为当前版本，并追加一行判据：

```markdown
> **2026-09-26 复核**：`deploy/scripts/checkpoint_acceptance.py` 在 `0.1.0-535-…` 上全绿
> （日志 `tmp/k0s/checkpoint-e1.log`，末行 `{"step": "OK"}`）。这条能力在生产形态
> （image-rootfs + `E2B_REAL_ROOT=1`）下**可用**；三大拦路虎里 D9（恢复后不能 exec）与 chroot/真根
> 与 restore stub 的不兼容都已在引擎侧解掉，剩下的都是**语义与运维**问题（见
> `docs/superpowers/plans/2026-09-26-checkpoint-restore-productization.md`）。
```

- [ ] **Step 5: 提交**

```bash
git add deploy/scripts/checkpoint_acceptance.py docs/deploy-clusters.md
git commit -m "test(checkpoint): the production-shape acceptance lives in the repo and passes"
```

---

### Task F1: 会话恢复 × 两种根形态 × 恢复后仍然能 exec（fork）

**要钉的是什么**：`restore_into_session` 是 E2B **唯一**走的恢复路径（route B 只发
`checkpoint` / `restore` 两个 verb，`envd_service/route_b.py:1489/1509`），而它在 fork 里的
覆盖是一半的：

- `test_a_child_restored_into_a_session_keeps_the_session_executable`
  （`crates/sandlock-core/tests/integration/test_instance_exec.rs:599`）用的是 `base_policy()` ⇒
  **没有 chroot 根**；
- `test_a_dynamic_workload_resumes_into_a_session_under_a_real_root`（同文件 `:1147`，fork
  `6367c26`）覆盖了**真根**，但只断言"恢复出的进程继续计数"，**没有断言这个会话还能 exec**，
  也没有模拟根那一态。
- 一次性恢复路径（`test_restore.rs:192`）倒是两态都跑——**生产形态的关键那条（会话）不是**。

所以生产形态今天之所以"看起来没问题"，靠的是集群脚本（E1）；本地没有任何一条用例能在
引擎层面把它钉住。

**Files:**
- Modify: `crates/sandlock-core/tests/integration/test_instance_exec.rs:1147-1274`（扩展现有真根用例：加 exec-after-restore 断言；fork 仓）
- Create: `crates/sandlock-core/tests/integration/test_instance_exec.rs` 里新增
  `test_a_static_workload_resumes_into_a_session_under_an_emulated_chroot`（同一个文件，跟在真根用例之后；fork 仓）
- Modify: `third_party/sandlock/docs/test-baseline.md`（`core_integ = 560` → `561`，fork 仓）

**Interfaces:**
- Consumes: `SandboxInstance::launch_exec(policy, argv)`、`SandboxInstance::exec(argv, ExecStdio::Piped)`、`SandboxInstance::checkpoint_excluding_main()`、`SandboxInstance::restore_into_session(&cp)`、`SandboxInstance::kill_child(id, SIGKILL)`、`SandboxInstance::stats().children_live`（全部来自 `crates/sandlock-core/src/instance.rs`）；测试夹具 `tests/rootfs-helper`（静态、`build.rs` 编好，子命令 `clock-loop` / `echo`）
- Produces: 一条能在引擎层面判红的用例——生产形态的会话恢复一旦回归，它先红，而不是等到集群

- [ ] **Step 1: 写会失败的测试（扩真根用例 + 新增模拟根用例）**

在 `test_a_dynamic_workload_resumes_into_a_session_under_a_real_root` 里，`restore_into_session`
成功、并且计数器已经推进之后，插入这三段（**用该文件已有的 API**，见
`test_a_sessions_workload_is_captured_with_the_park_left_out:824-841` 的同款写法）：

```rust
    // 会话恢复之后：不是"进程活着"就够，而是**这个会话仍然服务 exec**（D9/(b) 的全部意义）。
    // 恢复出来的孩子是 init 生的，所以 exec 必须照常；OCI 那条一次性恢复路径在这里是按名
    // 拒绝的 —— 这两条路径的差别就是 E2B 能不能用这个功能。
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

新用例（模拟根，静态 helper，跟随真根用例）：

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

同时改基线计数（`test-all.sh` 会因为"套件多跑了一条"判红）：

```text
core_integ = 561 # 2026-09-26: 560 -> 561, +1: 会话恢复在**模拟根**下也要能继续 exec
                 # （test_a_static_workload_resumes_into_a_session_under_an_emulated_chroot）
                 # —— 生产走的就是会话这条恢复路径，而它此前只有真根/无根两态被钉住。
```

- [ ] **Step 2: 跑它，确认结果（**这是本计划唯一一条无法预先写出"红色原文"的用例**：两条边界今天可能已经成立，也可能不成立，两种结果都必须写进提交信息）**

```bash
cd /Users/polus/project/ai/sandlock-e2b/third_party/sandlock
IMAGE=sandlock-dev-f17:latest /Users/polus/project/ai/sandlock-e2b/deploy/scripts/fork-gate.sh \
  --one 'test_a_dynamic_workload_resumes_into_a_session_under_a_real_root'
IMAGE=sandlock-dev-f17:latest /Users/polus/project/ai/sandlock-e2b/deploy/scripts/fork-gate.sh \
  --one 'test_a_static_workload_resumes_into_a_session_under_an_emulated_chroot'
```

Expected: 每一条都 `1 passed; 0 failed`。红了就是引擎的原话（`SandboxRuntimeError` 文本，
例如 `restore: the stub was not granted` / `restore: could not resolve <path>`）——那句原文
进 Step 3。

- [ ] **Step 3: 最小实现（只有 Step 2 红了才做；落点由那句话决定）**

```text
红的原文形如 "restore: …" ⇒ 修复落在 crates/sandlock-core/src/instance.rs:1229-1290
  （restore_into_session 的 chroot_root / mounts / plan 三段，与 Sandbox::restore_interactive
  在 crates/sandlock-core/src/sandbox.rs:1420-1470 做的是同一件事；两边必须同参，抄过去）
红的原文形如 "restore-stub was not built" ⇒ 是构建面：
  crates/sandlock-core/src/checkpoint/resume.rs:86 stub_path() + build.rs；
  本机容器 lane 里有 stub（build.rs 编译进 target/），所以这条只会在 wheel/集群上出现。
```

```rust
// restore_into_session 里，把与一次性路径不一致的那一段改成同一段（示例：chroot 解析）
let chroot_root = crate::chroot::resolve::resolve_chroot_root(policy.chroot.as_deref())?;
let mounts = crate::chroot::resolve::resolve_chroot_mounts(&policy.fs_mount);
let plan = crate::checkpoint::restore_blob::plan(cp, chroot_root.as_deref(), &mounts)
    .map_err(SandboxRuntimeError::Child)?;
```

- [ ] **Step 4: 跑测试，确认通过**

```bash
cd /Users/polus/project/ai/sandlock-e2b/third_party/sandlock
IMAGE=sandlock-dev-f17:latest /Users/polus/project/ai/sandlock-e2b/deploy/scripts/fork-gate.sh \
  --one 'test_a_static_workload_resumes_into_a_session_under_an_emulated_chroot'
# Expected: test result: ok. 1 passed; 0 failed
IMAGE=sandlock-dev-f17:latest /Users/polus/project/ai/sandlock-e2b/deploy/scripts/fork-gate.sh --one 'test_restore::'
# Expected: test result: ok. 5 passed; 0 failed      （一次性恢复那两态不许被这次改动带红）
IMAGE=sandlock-dev-f17:latest /Users/polus/project/ai/sandlock-e2b/deploy/scripts/fork-gate.sh --one 'test_instance_exec::'
# Expected: 0 failed；这一族的总数比改动前多 1（新用例），基线的 core_integ 因此从 560 到 561 ——
#           门禁自己会拿 docs/test-baseline.md 比对，数字不对它直接判红，这就是那条计数的验收
```

- [ ] **Step 5: 提交（fork 仓单独提交 + 重建 wheel）**

```bash
cd /Users/polus/project/ai/sandlock-e2b/third_party/sandlock
git add crates/sandlock-core/tests/integration/test_instance_exec.rs docs/test-baseline.md
git commit -m "test(restore): a session restored under an emulated chroot keeps serving exec"
cd /Users/polus/project/ai/sandlock-e2b
deploy/scripts/build-sandlock-wheels.sh            # ~4 分钟；E2B 侧不重建 wheel 就看不到这条改动
git add third_party/sandlock
git commit -m "chore(fork): pin the session-restore coverage for both root shapes"
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

### Task F4: 【条件任务】让 `--restore-from` 那条模式也能 exec（D9 候选②，默认不做）

**事实先摆正（这是本任务存在的全部理由）**：D9 的原话是"恢复出来的容器不能再 exec"，
而**生产形态不成立**——E2B 走的是 `restore` verb → `SandboxInstance::restore_into_session`
（`crates/sandlock-core/src/instance.rs:1229`、`serve.rs:789-819`），恢复出的进程是会话 init
的孩子，所以 `exec` 继续被服务（用例 `test_instance_exec.rs:599`，集群验收拿到 `EXEC_OK`）。
仍按名拒绝 exec 的只有 `--restore-from` / OCI 那条**独立启动模式**（`serve.rs:1481`、
`crates/sandlock-oci/src/supervisor.rs:1386`），而 E2B 从不走它（`route_b.py` 只发
`checkpoint` / `restore` 两个 verb）。

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
# Expected: 无输出（0 命中）⇒ 生产没有消费者 ⇒ 本任务**不做**，
#           把上面那张候选表抄进 docs/checkpoint-restore-e2b-half.md §2 的 D9 行，注明"引擎侧已闭、无消费者"。
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
- Modify: `envd_service/agent.py:2857-2864`（`pause` 的那条 info 日志；E2B 仓）
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
# Expected: 25 passed（verb 白名单改动不许碰 route-b 的其它用例）
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
`unrecoveredFds` 只出现在内部端点与 worker 日志里（`agent.py:2905-2926`）。使用者因此无法回答
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
502 回滚，`control_plane/api/sandboxes.py:317-340`），所以这条端点是 GET、无副作用、
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
`deleted` / `untrustedRecords` 同一层级。）

- [ ] **Step 4: 跑测试，确认通过**

```bash
cd /Users/polus/project/ai/sandlock-e2b
tmp/testenv/bin/python -m pytest tests/unit/test_checkpoint_store.py tests/unit/test_quota_maintenance.py -q
# Expected: 74 passed（`test_checkpoint_store.py` 21 + 本任务 3 条 = 24；
#           `test_quota_maintenance.py` 49 + 1 条 = 50）
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
