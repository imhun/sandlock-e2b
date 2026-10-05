# N79：stat 族 seccomp 通知单列预算 实施计划

**Goal:** 让 `stat` 族（`newfstatat`/`statx`/`faccessat`/`faccessat2`/`readlinkat` + 旧 ABI 的
`stat`/`lstat`/`access`/`readlink`）在 seccomp 通知限流里吃**自己的**一秒预算，不再和通用
5000/s 抢额度 —— 普通元数据负载（`find`/`git status`/包管理器）不再每秒被卡 0.86 s，
而一场真的 stat 洪泛仍然受限。

**Architecture:** 限流器从"一个窗口一个计数"变成"**每类一个窗口**"。分类键是 **syscall 号**，
类成员直接取 `seccomp_plan::stat_family_syscalls()`（与 `pid_ns_procfs_stat_syscalls()` 同源，
不会各自漂移）。新策略字段 `notify_rate_limit_stat`：

- **不设（`None`）⇒ stat 并入通用预算**，与今天逐字节相同（fork 的其他使用者、以及回退都走这条）；
- **设成 ≥1 ⇒ stat 单列预算**，通用预算只数非 stat 的通知；
- **设成 0 ⇒ 同上"不设"**（不是"无限"—— 不能靠一个 0 就把防洪泛闸关掉）。

envd 侧新变量 `E2B_SANDBOX_STAT_NOTIFY_RATE_LIMIT`，默认 **20000/s**（0 = 退回并入通用预算）。

**Tech Stack:** Rust（`crates/sandlock-core`、`crates/sandlock-ffi`、`crates/sandlock-supervise`）、
Python（`python/src/sandlock`）、pytest（`envd_service` 侧单测）、Docker（`sandlock-dev:latest`
跑 fork 套件）、k0s（现场验收）。

**Spec / 依据:** `docs/open-issues.md` 的 N79 行（选项 ①）、`docs/benchmarks.md` §③ 的实测
（p50 26 µs、紧循环每 1.0007 s 卡 852–864 ms）、`docs/security-hardening.md` P1-5 的原始意图。

## Global Constraints

- **不设该字段时，行为必须与今天逐字节相同** —— fork 还有别的使用者，而且回退要能一键退回去。
- **类成员与 notify 表同源**：`stat_family_syscalls()` 是唯一名单，`pid_ns_procfs_stat_syscalls()`
  直接复用；分类函数不许自己抄一份常量表。
- **不许把 stat 做成无预算**（N79 的裁定写明"别做成完全豁免"）。单列预算仍然是一个真实上界：
  超了就睡满该窗口剩余时间（机制不变，只换预算归属）。
- **窗口机制本身不动**：不改"睡满窗口"这个形状。换形状（平滑 pacing / token bucket）会改变洪泛
  上界的含义，且影响所有类 —— 留给独立一条（见文末）。
- 只碰：`third_party/sandlock`（fork 子模块）、`envd_service/`、`deploy/k8s/*.yaml`（env）、
  `docs/`。不碰 seccomp profile、不碰 `/proc` 合成路径。
- 基线：`tests/unit` 本机 `3 failed / 2467 passed / 12 skipped`，3 条是 macOS 固有
  （`test_real_root_gate` ×1 dlopen + `test_xfs_quotactl_backend` ×2）。

## Review Focus

按"最可能咬人"排序：

1. **默认值选错**：选小了等于没修（照旧在 5000/s 附近撞墙），选大了等于把闸门拆了。判据写在
   Task 3 的注释里：20000/s ≈ 0.52 核（26 µs/次），是 worker pod 2 核的四分之一，也是今天
   5000/s 的四倍；普通突发（≤20000/s 的窗口内）**完全不被限流**，只有持续洪泛才吃背压。
2. **`0` 的语义被读成"无限"**：通用字段的 `0` 是"关掉限流"，新字段必须是"关掉**独立预算** ⇒
   并回通用预算"。两种"0"含义不同，代码和文档都要写死，单测钉住。
3. **分类漏项/串类**：`pid_ns` 关掉时这些 syscall 仍在 chroot 表里（`chroot_path_syscalls()` 也含
   stat 族），所以分类**按 syscall 号、与形态无关**，不能只在 `pid_ns` 打开时生效。
4. **策略线协议漂移**：`notify_rate_limit_stat` 要进 `KNOWN_KEYS`、`--policy` 读回、FFI 符号、
   Python `_HANDLED_FIELDS`、envd 的 route-B 转发键集。漏一处就是"设了没生效"，且 Python 侧会
   打"字段没接线"的告警（**这正是 SL-2 的教训**）。
5. **只在 PY 层设、清单里看不见**：这一条最后的落法是**有意不写进 worker 清单** —— 既有的
   `E2B_SANDBOX_NOTIFY_RATE_LIMIT` 也不在清单里（走代码默认），新值跟着它的样子走，省掉一处
   平白的 `kubectl diff` 面；默认值改在 `envd_service/config.py` 的注释与
   `docs/security-hardening.md` P1-5 里写清楚。

---

### Task 1: fork —— 把限流窗口拆成可测的类预算

**Files:**
- Modify: `third_party/sandlock/crates/sandlock-core/src/seccomp_plan.rs`（新增
  `stat_family_syscalls()`；`pid_ns_procfs_stat_syscalls()` 复用它）
- Modify: `third_party/sandlock/crates/sandlock-core/src/seccomp/notif.rs`（窗口从单个拆成两个）
- Test: `third_party/sandlock/crates/sandlock-core/src/seccomp/notif.rs` 的 `#[cfg(test)]`（新增）

**Interfaces:**
- Produces: `pub(crate) fn stat_family_syscalls() -> Vec<i64>`（唯一名单）、
  `struct WindowBudget`（`new(limit)` / `admit(now) -> Duration`）、
  `supervisor(..., notify_rate_limit: Option<u32>, notify_rate_limit_stat: Option<u32>)`。
- Consumes: 无。

- [ ] **Step 1: 写失败用例**

在 `notif.rs` 的测试模块里加（沿用文件已有的测试体例）：

```rust
/// 单列预算：stat 的通知**不消耗**通用窗口。
#[test]
fn a_stat_burst_does_not_spend_the_general_budget() { ... }

/// 单列预算仍然是一个真实上界：超了照样睡满窗口。
#[test]
fn the_stat_window_still_throttles_a_flood() { ... }

/// 不设该字段 ⇒ 两类并回一个窗口（今天的行为）。
#[test]
fn without_a_stat_budget_every_notification_shares_one_window() { ... }

/// 名单不许漂移：分类用的表就是 pid_ns 表用的表。
#[test]
fn the_stat_family_is_the_pid_ns_table() { ... }
```

判据用**精确相等**（`assert_eq!` 到具体 `Duration` / 计数），不用"大约"或 `contains`。

- [ ] **Step 2: 跑，确认失败**

```bash
docker run --rm -v "$PWD/third_party/sandlock":/src -w /src sandlock-dev:latest \
  cargo test -p sandlock-core --lib notif
```

Expected: 编译失败（`stat_family_syscalls` / `WindowBudget` / 新参数还不存在）。

- [ ] **Step 3: 实现**

`seccomp_plan.rs`：抽 `stat_family_syscalls()`，`pid_ns_procfs_stat_syscalls()` 改成
`stat_family_syscalls()` 的转发（名字与注释保留，因为调用点在别处）。

`notif.rs`：把现在的 `rate_window_start`/`rate_window_count` 换成
`WindowBudget { limit, start, count }` + `admit(now) -> Duration`；`supervisor()` 里按
`stat_class.contains(&notif.data.nr)` 选窗口；`None` 时两个类共用一个窗口。

- [ ] **Step 4: 跑整档**

```bash
docker run --rm -v "$PWD/third_party/sandlock":/src -w /src sandlock-dev:latest \
  cargo test -p sandlock-core --lib
```

Expected: 新用例绿，其余零回退。

- [ ] **Step 5: commit（fork 仓）**

```bash
git -C third_party/sandlock add crates/sandlock-core/src/seccomp_plan.rs crates/sandlock-core/src/seccomp/notif.rs
git -C third_party/sandlock commit -m "feat(seccomp): a stat family with its own notification budget"
```

---

### Task 2: fork —— 把 `notify_rate_limit_stat` 接到策略线协议上

**Files:**
- Modify: `crates/sandlock-core/src/sandbox/builder.rs`、`crates/sandlock-core/src/sandbox.rs`
- Modify: `crates/sandlock-supervise/src/policy.rs`
- Modify: `crates/sandlock-ffi/src/lib.rs`、`crates/sandlock-ffi/include/sandlock.h`
- Modify: `python/src/sandlock/_sdk.py`、`python/src/sandlock/sandbox.py`
- Test: `python/tests/test_sandbox.py`（照 `test_notify_rate_limit_is_declared_handled` 再写一条）

**Interfaces:**
- Consumes: Task 1 的 `supervisor(..., Option<u32>)` 新参数。
- Produces: 一条从 `--policy` JSON / Python `SandboxOptions` / FFI builder 到 supervisor 的
  完整通路，字段名 `notify_rate_limit_stat`。

- [ ] **Step 1..6**：按 `notify_rate_limit` 的既有 6 个落点逐一对齐（field → default → build →
  setter → wire key + read-back → FFI 符号 + 头文件 → Python builder fn → `_HANDLED_FIELDS` →
  `SandboxOptions` 字段），每加一处先让它红。

---

### Task 3: E2B —— env、执行器、清单、单测

**Files:**
- Modify: `envd_service/config.py`（`sandbox_stat_notify_rate_limit`，默认 20000）
- Modify: `envd_service/executors/factory.py`、`envd_service/executors/sandlock.py`
- Modify: `envd_service/route_b.py`（`_FORWARDED_POLICY_KEYS` 集合）
- Modify: `docs/security-hardening.md`（P1-5 的默认值）
- Test: `tests/unit/test_notify_rate_budget.py`（新增）

**Interfaces:**
- Produces: `settings.sandbox_stat_notify_rate_limit`，以及落到沙箱策略里的
  `"notify_rate_limit_stat"` 键。

- [ ] **Step 1: 失败用例**：断言 config 默认 20000、可被 env 调、`0` 时策略里是 `None`
  （并回通用预算）、非 0 时落进 supervise policy 文档。
- [ ] **Step 2..4**：实现 → 跑 `tests/unit` → commit。

---

### Task 4: 现场验收（N79 的口径）

- [ ] **Step 1**: 重建 wheel + 镜像 + 上线（照 `docs/deploy-clusters.md` §8，先认集群）。
- [ ] **Step 2**: 复跑 `deploy/scripts/acceptance/lightweight_metrics_probe.py`：
  `stat` 的 p50 仍在 ~26 µs 量级，且**不再出现"睡满窗口"**。
- [ ] **Step 3**: 复跑 `pidns-cost-probe.py`（§2.4.10.2 同一张表）。
- [ ] **Step 4**: 一条钉子：`stat /proc/<宿主 pid>/…` 仍然 `EACCES`（拦截语义没丢）。

---

### Task 5: 记录

- [ ] `docs/open-issues.md` 的 N79 行：`待决策` → `已修并上线` + 版本号 + 现场读数。
- [ ] `docs/benchmarks.md` §③：把"5000/s 通用预算"改成"通用 5000/s + stat 单列 20000/s"。
- [ ] `docs/security-hardening.md` P1-5：补一句两类预算与默认值。
- [ ] `docs/deploy-clusters.md`：一节发版记录。

---

## 本计划不做（已裁定，理由记进 ledger）

| 项 | 裁定 |
|---|---|
| 改"睡满窗口"为平滑 pacing / token bucket | **本轮不做**。它能让**任何**速率下都不出现一秒级停顿（尾延迟从 860 ms 降到 ~1/limit），代价是洪泛时的 p50 从 26 µs 退到 `26 µs + 1/limit`，且会改变所有类的上界形状（naive pacing 会把首秒上界放大到约 1.8×）。属独立一条，要连带上界重证。 |
| 选项 ②（彻底放行 stat） | **不做（带触发）**，同 N79 行：前置是 `/proc` 的 stat 语义，沙箱自挂 procfs 三形态实测 EPERM；path-aware LSM/BPF 本部署未评估。 |
| `notify_rate_limit_stat` 默认设成"无限" | **不做**。N79 明写"别做成完全豁免"。 |
| 把 stat 预算做成通用预算的倍数 | **不做**。两个独立旋钮更直白；倍数会在改通用值时静默改掉 stat 的上界。 |
