# S5：真根成为唯一形态（退役模拟）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让**真根**（mount ns + `pivot_root`）成为唯一形态：删掉两条显式退路（`E2B_PURE_ROOTFS=off`、`E2B_REAL_ROOT=0`）、删掉只为模拟根存在的翻译路径，安全套件从"两态"收敛成"一态"。

**Architecture:** 三块，按依赖排序。① **E2B 侧先动**（本仓库、可独立验收）：两个开关变成闭集合 + 退役取值**具名拒绝**，pure 形态的合成根（N16 已实现）成为无条件默认；② **fork 侧再动**（submodule `third_party/sandlock`）：删掉"模拟根才走"的那半 —— 翻译路径与 identity 根的管道，**保留**策略判定、COW 视图与 `/proc` 合成（见 §Review Focus 第 1 条）；③ **收尾**：arm-lane 的 0 臂、wheel 重建与 submodule 指针、清单与集群验收。

**Tech Stack:** Python（envd/control_plane）、Rust（fork: sandlock-core）、submodule + wheel、k0s 清单、pytest 与 fork 的 `core_integ`/cargo 测试。

**Spec:** `docs/n14-retire-the-emulation.md`（§4.2 的 handler 判据表、§5 的 N16 合成根、§6 的 S1–S5、§7 的"不做会怎样"）；本计划是它的 S5 落地版，不重复它的论证。

## Global Constraints

- **这是代码卫生，不是欠一道防线**（源文档 §5/§7 的原话）：真根的收益由 S1 交付；本计划不得声称"修了安全问题"。
- **两处不能跟着退役**：① `/proc` 是中介**合成**的（真根下内核 `/proc` 为空、真 procfs 挂不上，三形态实测全 EPERM）⇒ `crates/sandlock-core/src/procfs.rs`（2155 行）**保留**；② 策略判定与 COW 视图（"沙箱写过的东西必须报副本元数据"）在真根下**仍然需要**（§4.2 表：`open`/`write` 带 COW + 写监控；`stat`/`statx`/`readlink`/`xattr`/`utimensat` 带 COW 视图；`inotify_add_watch`/`statfs` 带策略）⇒ **这些 handler 的 body 保留**，只删"为了模拟根而翻译"的那半。
- **退役取值一律具名拒绝**（仓库既有口径，见 N52/N16 的"闭列表"做法）：`E2B_REAL_ROOT=0` 与 `E2B_PURE_ROOTFS=off` 不许静默回落，启动时按名拒绝并指出替代。
- **磁盘活账本已解绑**：S4（2026-10-04 实测，见 `docs/open-issues.md` N14）已证"周期全扫足够"（4 棵树 × 20000 文件：walk 与快路同为 0.03 s 级、逐字节一致），所以本计划**不依赖**中介的 dirty 集；`E2B_DISK_ENFORCE_DIRTY` 的去留是 S5 的一部分（建议保留开关、默认仍 `1`，除非同时接受大树的 walk 成本）。
- 断言精确匹配、禁 SKIP/xfail；编辑一律 `apply_patch`；每条"能失败"的钉子必须先证明它会红。
- fork 改动**必须**走 submodule：提交在 `third_party/sandlock`，父仓 `git add third_party/sandlock` 更新指针，wheel 由 `deploy/scripts/build-sandlock-wheels.sh` 重建（`wheels/fork/` 是 gitignored，不提交产物）。

## Review Focus

1. **别把"策略/COW/账本"当成翻译一起删掉**：§4.2 的判据表是唯一权威，逐 handler 过；删除的判据是"这一段只在 `!child_is_pivoted` 时执行**且**它的产物是翻译（宿主路径 ↔ 沙箱路径的换算）"。反例：`legacy_chown`（207 行、11 处策略判定）不是壳，`legacy_*` 的 12 个是参数重排壳但不是翻译。
2. **`/proc` 与 `/dev`**：合成根要在真根下继续提供，且 `E2B_PURE_ROOTFS=synth` 的骨架内容必须与今天**逐项一致**（N16 的白名单：`/usr`/`/lib`/`/bin`/`/opt` + workspace，整棵 `/dev`）。
3. **纯净形态的残留**：N27 的"`<export>` 一层能列出 `state`/`_secrets` 名字"只在 identity 档存在，identity 档退役后这条残差随之消失 —— 但**不要在 plan 里把它算成本计划的收益**（它已由 N16 的默认切换交付）。
4. **两态测试的收敛方式**：不是删测试，而是让它们只跑一态；`deploy/scripts/arm-lane/x86-security.sh <0|1>` 的 0 臂要按名拒绝地退场（脚本层面），而不是留着一个永远不被调用的参数。
5. **退路的可发现性**：旧部署若还写着这两个值，必须**启动即拒**并打印替代写法（`E2B_PURE_ROOTFS=synth` 已是默认，直接删掉那行即可），而不是"值变了但没人知道"。

---

## 文件结构

| 文件 | 责任 |
|---|---|
| `envd_service/config.py`（改） | 两个开关的闭集合解析 + 退役值具名拒绝；`resolve_real_root` 去掉三态 |
| `envd_service/executors/sandlock.py`（改） | 删掉 `real_root=False` 才走的分支与 `_real_root_capability()` |
| `envd_service/executors/factory.py`、`envd_service/app.py`、`envd_service/agent.py`、`gateway_common/paths.py`（改） | 跟随上面的签名/判据变化 |
| `tests/unit/test_pure_rootfs_config.py`（改） | 42 处引用收敛到新契约（红→绿） |
| `tests/unit/test_real_root_gate.py`、`tests/security/conftest.py`、`tests/security/test_pure_root_errno_contract.py` 等（改） | 单臂化 |
| `deploy/scripts/arm-lane/x86-security.sh`（改） | 0 臂退场（按名拒绝） |
| `third_party/sandlock/crates/sandlock-core/src/chroot/dispatch.rs`、`resolve.rs`（改，**fork**） | 删翻译路径；保留策略/COW/账本 |
| `deploy/k8s/worker.yaml`（改） | 删掉 `E2B_REAL_ROOT`（若显式写着）；seccomp profile 要求保留 |

---

### Task 1: 两个退路变成具名拒绝（E2B 侧，可独立验收）

**Files:**
- Modify: `envd_service/config.py`（`_real_root_from_env`、`_pure_rootfs_from_env`、`resolve_real_root`、`check_pure_rootfs_pairing`）
- Test: `tests/unit/test_pure_rootfs_config.py`

**Interfaces:**
- Produces: `RealRootRetiredError` / 既有 `PURE_ROOTFS_WITHOUT_REAL_ROOT_ERROR` 被**取代**为"退役即拒"；`resolve_real_root(...) -> bool` 不再有 `None` 分支
- Consumes: 既有 `Settings` 字段（`pure_rootfs`、`real_root`）

- [x] **Step 1: Write the failing tests**（每条一个钉子）

```python
def test_real_root_zero_is_refused_by_name():
    # E2B_REAL_ROOT=0 -> RuntimeError，消息里含 "E2B_REAL_ROOT=0" 与 "retired"

def test_pure_rootfs_off_is_refused_by_name():
    # E2B_PURE_ROOTFS=off -> RuntimeError，消息里含 "E2B_PURE_ROOTFS=off"

def test_unset_and_on_are_the_only_accepted_values():
    # 不设 / =1 / =true -> 通过，且 resolve_real_root() is True
```

- [x] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_pure_rootfs_config.py -q`
Expected: 三条新钉子红（今天 `=0` 是**合法**的退路，`=off` 亦然）

- [x] **Step 3: Implement**（闭集合 + 具名拒绝；删掉 identity 档相关的配对分支）

- [x] **Step 4: Run**

Run: `.venv/bin/python -m pytest tests/unit/test_pure_rootfs_config.py tests/unit/test_worker_env_key_sets.py -q`
Expected: PASS

- [x] **Step 5: Commit**（`b8d9d72`）

```bash
git add envd_service/config.py tests/unit/test_pure_rootfs_config.py
git commit -m "feat(n14-S5): E2B_REAL_ROOT=0 / E2B_PURE_ROOTFS=off are retired and refused by name"
```

---

### Task 2: 删掉"只有模拟根才走"的 E2B 侧分支

**Files:**
- Modify: `envd_service/executors/sandlock.py`（`_real_root_capability()`、`self._real_root` 的 6 处分支）、`envd_service/executors/factory.py`、`envd_service/app.py`、`envd_service/agent.py`、`gateway_common/paths.py`
- Test: `tests/unit/test_real_root_gate.py`（4 处引用收敛）

**Interfaces:**
- Produces: `SandlockExecutor` 不再接受 `real_root=False`（签名去掉该参数或在构造时断言）
- Consumes: Task 1 的新契约

- [x] **Step 1: 先写会红的钉子**

```python
def test_a_pure_sandbox_always_gets_the_synthesized_root():
    # 建一个 pure sandbox：<base>/_pure_rootfs/<id> 必须存在（今天 identity 档下不存在）

def test_the_executor_refuses_real_root_off():
    # SandlockExecutor(..., real_root=False) -> RuntimeError（今天它是合法入参）
```

- [x] **Step 2: Run to verify they fail**（`test_the_executor_has_no_real_root_knob` 红：`real_root` 当时还在签名里）

Run: `.venv/bin/python -m pytest tests/unit/test_real_root_gate.py -q`
Expected: 两条红

- [x] **Step 3: 实现**：`self._real_root` 与构造参数删除、两处 `if self._real_root:` 塌成"有根即真根"、`factory.py` 不再传；**偏离一处**：`_real_root_capability()` 保留，改为**无条件**的构造期门（有根就问一次）——它挡的是"节点 seccomp 没放行 mount 族"这条现场故障，与开关无关，删掉只会退回"每次 create 都 instance is closed 且无原因"

- [x] **Step 4: Run**：`tests/unit` **2455 passed / 12 skipped / 3 failed**（3 条为既有 macOS-only：pivot_root 探针、两条 `test_xfs_quotactl_backend`）；`tests/security` 采集 85 条不破。macOS 侧新增 `tests/unit/conftest.py` 的 autouse 桩（探针是"节点属性"，本机不是 Linux worker），`test_real_root_gate.py` 退出该桩，它钉门本身

Run: `.venv/bin/python -m pytest tests/unit/test_real_root_gate.py tests/unit/test_pure_rootfs_config.py tests/unit/test_worker_env_key_sets.py -q`
Expected: PASS

- [x] **Step 5: Commit**

```bash
git add envd_service tests/unit/test_real_root_gate.py gateway_common/paths.py
git commit -m "refactor(n14-S5): the executor has no real-root-off branch (the root is the shape)"
```

---

### Task 3: fork 侧删翻译路径（**跨仓库，独立提交**）

**Files:**
- Modify（fork）: `third_party/sandlock/crates/sandlock-core/src/chroot/dispatch.rs`、`chroot/resolve.rs`
- Test（fork）: `crates/sandlock-core/tests/integration/test_chroot.rs`、`test_instance_exec.rs`、`test_cow.rs`、`test_restore.rs`、`test_procfs.rs`

**Interfaces:**
- Consumes: Task 1/2 之后"真根恒真"
- Produces: `resolve.rs` 的翻译表只保留策略/COW 需要的那部分；`dispatch.rs` 的每 handler 双分支塌成单分支

**这是本计划唯一不能在本仓库里完成的任务**（fork 有自己的 git 历史与 CI）。

- [ ] **Step 1: 先产出删除清单**（不猜）：按 §4.2 的判据表逐 handler 过一遍，写成 `删除 / 保留 / 理由` 三列，**review 通过后才动代码**
- [ ] **Step 2: 每个 handler 一条测试**（fork 侧）：`child_is_pivoted` 恒真后，原先走翻译的输入必须仍被策略/COW 正确处理
- [ ] **Step 3: 删代码 + `cargo test` 全绿**（口径：`core_integ` 559 + `test_chroot` 51 + `test_instance_exec` 28 + `test_cow` 26 + `test_restore` 5 + `test_procfs`）
- [ ] **Step 4: fork 提交 → 父仓 `git add third_party/sandlock` → `./deploy/scripts/build-sandlock-wheels.sh`**
- [ ] **Step 5: 父仓提交**（submodule 指针 + 说明）

---

### Task 4: 两态测试收敛成一态（E2B 侧）

**Files:**
- Modify: `tests/security/conftest.py`（`E2B_REAL_ROOT` 的解析）、`tests/security/test_real_root_denials.py`、`test_pure_root_errno_contract.py`、`test_uid_isolation.py`、`test_socket_families.py`、`test_ioctl_inventory.py`、`test_chroot_exec_shebang.py`、`worker_nonroot_probe.py`；`deploy/scripts/arm-lane/x86-security.sh`、`e2b-sync.sh`；`deploy/scripts/acceptance/{gateA-full,gateB-full,gateB-pure-rootfs,x86-run-py,x86-security-one}.sh`、`deploy/compose/docker-compose.yml`
- Add: `tests/unit/test_arm_lane_one_shape.py`（钉子；计划里预留的那个文件名最终换成了这个名字）

- [x] **Step 1: 先写会红的钉子**（`tests/unit/test_arm_lane_one_shape.py`）

```python
def test_the_arm_lane_refuses_the_retired_zero_arm():
    # deploy/scripts/arm-lane/x86-security.sh 0 <log> -> 非零退出 + 具名消息

def test_the_security_fixture_resolves_one_shape_only():
    # tests/security/conftest.py 解析出的 real_root 恒为 True（不再是环境驱动）
```

- [x] **Step 2: Run to verify they fail**：三条红（0 臂未拒、lane 仍带退役 env、夹具随 `E2B_PURE_ROOTFS=off` 变成无根形态——最后一条用 `git stash` 把旧 conftest 拿回来单独验过）

- [x] **Step 3: 实现**：夹具单形态（不再读两个开关，目录旋钮照读）；`x86-security.sh` 的 `$1` 只接受 `1`、`0` 具名拒绝；`gateB-pure-rootfs.sh` 的 state 0 同样退场；五个 lane/acceptance 脚本删掉退役 env；单机 compose 栈删 `E2B_PURE_ROOTFS=off` 并改带 `deploy/seccomp/sandlock-worker.json`（Docker 默认档不放行 mount 族）；`e2b-sync.sh` 与三处 security 文档串同步
  **现场发现（本步最值钱的一条）**：真根要求**沙箱有自己的 user namespace**。fork 在"沙箱身份 == 中介身份"时跳过 userns，子进程 `unshare(CLONE_NEWNS)` 得 EPERM ⇒ 建箱失败（trace：`deploy/scripts/arm-lane/evidence/s5-nonroot-realroot-needs-userns.log`）。E5.1 非 root worker 探针的 in-process chroot 用例就是这个形态（`test_worker_nonroot` 因此红），而两份生产清单都开 `E2B_PID_NS=true`（其 userns 由中间进程建），所以**生产不受影响**——修法是把探针改成生产形态：`worker_nonroot_probe.py::_executor` 加 `pid_ns=True`

- [x] **Step 4: Run**：x86_64 生产形态容器（镜像 + 线上 cap 集 + 出厂 seccomp 档）里 ① `tests/security/test_worker_nonroot.py` **2 passed**；② 形态相关十文件 **37 passed / 330 s**（含 N35 三条 shebang 用例——T2 拆掉的 xfail 守卫在这里变成真断言）；③ `x86-security.sh 0` **exit 2 + 具名消息**、`1` 臂跑完整套：**62 passed**，其余 17 failed / 5 error 全为环境（本地镜像缓存没有 `python-mcp:3.14`、`127.0.0.1:5080` 镜像仓未起 ⇒ 428 `warm_required` / registry 401）。arm64 lane 与集群侧留 T5 一并做

- [x] **Step 5: Commit**

```bash
git add tests/security deploy/scripts/arm-lane
git commit -m "test(n14-S5): the security suites and the arm lane have one shape"
```

---

### Task 5: 清单与集群验收

**Files:**
- Modify: `deploy/k8s/worker.yaml`（删 `E2B_REAL_ROOT`）、`docs/env-vars.md`、`docs/n14-retire-the-emulation.md`（§6 的 S5 勾选）

- [ ] **Step 1: 清单**：删退路 env；`DRY_RUN=1 apply.sh | kubectl diff -f -` 预期只剩镜像 tag/generation
- [ ] **Step 2: 构建 + 上线**（wheel 已是 Task 3 的产物）
- [ ] **Step 3: 现场验收**：① 两个形态各建一个沙箱（image-rootfs + pure synth），`sandbox.json` 有 root、`chain=PASS`（N16 探针）；② 旧的 `E2B_REAL_ROOT=0` 写进部署会**启动即拒**（用一次受控的 rollout 验证，然后撤回）；③ `GET /sandboxes` / `kubectl diff` 收尾读数
- [ ] **Step 4: 回填** `docs/deploy-clusters.md` 新节 + N14 状态改成"已收口"

## 本计划明确不做

- **不动 `/proc` 合成**（`procfs.rs` 保留）与**策略/COW**（§4.2 表里所有 ❌ 的都是留的理由）。
- **不删 `E2B_DISK_ENFORCE_DIRTY`**：S4 已证 walk 够用，但"大树"的代价没测到饱和区；把它留成开关（默认 `1`），要退役另立一条。
- **不碰 N15 的 33 条闸门**：那是另一条路线（合成根换闸门），不在 S5 内。
