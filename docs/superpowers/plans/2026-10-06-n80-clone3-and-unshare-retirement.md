# N80：clone3 建箱与 unshare 退役 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建箱路径去掉中间进程 B（A 用一次 `clone3` 直接把 leader 生进新 userns+pidns），随后让 sandlock 自己不再调用 `unshare`，并据此收紧 worker 容器 profile。

**Architecture:** `clone3` 的 `CLONE_NEWUSER|CLONE_NEWPID` 由多线程的 A 直接调用（内核按“子进程进新 ns”处理，不受 `unshare` 的“调用者不能多线程”限制）。C 出生即 pidns 的 PID 1、宿主侧的父就是 A，B 及其 leader-pid 回传管道、退出码转发一并删除。Task 2 把 `CLONE_NEWNS`/`CLONE_NEWNET` 也放进同一次 `clone3`，于是 `confine_child` 里不再有 `unshare`。

**Tech Stack:** Rust（`sandlock-core` / `sandlock-supervise`）、Python/pytest（E2B 侧 profile 钉子）、Docker（`sandlock-dev:latest` 跑 fork 门禁）、k0s（现场验收）。

**Spec:** `docs/open-issues.md` 的 N80 行；`docs/isolation-boundaries.md` §4；本文件「设计决策」一节（Task 2/3 的判据也在那里）。

## 设计决策

- **D1** `clone3` 一次带 ns 位替代「`unshare` + `fork`」；C 直接是 pidns 的 PID 1。
- **D2** 身份映射写边保持不变：**C 自写** `/proc/self/uid_map`（与今天的 B 等价）。搬给 A 写是 Task 3，本轮不做。
- **D3** 父死检测改用 `PR_SET_PDEATHSIG` + 既有 ready/gate 管道的 EOF：C 在 pidns 内 `getppid()` 恒为 0，今天那条 `getppid() != parent_pid` 在 C 侧是 vacuous（`sandbox.rs:2768` 的注释自己写明），race 兜底改由管道承担。
- **D4** profile 里 `clone3` 的显式化是**放宽**（当前那条 `ERRNO 38` 实测未触发，属“名义拒绝”）；它必须与 `unshare` 收窄**同一批**落地，净安全面不降。
- **D5** Task 3（统一到 A 写）本轮只列不做。

## Global Constraints

- 只改 `pid_ns` 形态；非 pid_ns（库使用者）路径与 `E5.1` 形态保持今天的形状。
- 沙箱内策略一律不动；A 是宿主侧进程，不受沙箱自己的 seccomp 过滤约束，**不得为此在沙箱内开口子**。
- 测试规范：精确断言（禁用 `contains` 类部分匹配）、禁止 SKIP、每个改动 RED→GREEN。
- 临时文件只放本仓 `tmp/`；可重跑脚本放 `deploy/scripts/acceptance/`。
- 集群操作前先 `deploy/scripts/open-cluster-tunnel.sh`，且每条 `kubectl` 都带 `KUBECONFIG="$PWD/tmp/k0s/kubeconfig"`。
- fork 门禁基线（`sandlock-dev:latest`，八相位）：core_lib **932** / core_integ **570** / ffi **104** / cli **98** / supervise **57** / supervise_cost **3** / cli_build **0** / python **466**；E2B 侧 `tests/unit` 有 3 条 macOS 固有红。
- 不动 N79 的 stat 预算与 N81 的 stat 放行面。

## Review Focus

1. **C 看不见父**：任何仍然依赖 `getppid()` 判父死的代码在 pidns 下都是 vacuous；必须有一条“Kill A ⇒ C 死”的用例。
2. **`stack=0` 的 fork 语义**：`clone3` 未设 `CLONE_VM`，`stack` 必须为 0 且子进程从调用点继续；子进程分支必须 `_exit` 或进入 `confine_child`，绝不能 `return`。
3. **4 个 ns 位的创建顺序**：`NEWNS`/`NEWNET` 必须归新 userns（内核先建 userns），否则 C 在里面没有 `CAP_SYS_ADMIN`，真根与 netns 当场 EPERM。
4. **profile 无参数级过滤**：`clone3` 的 flags 在结构体里，外层只能整条 allow/deny；“显式允许 clone3” 与 “禁掉 unshare” 若不共振，等于换了入口没减能力。
5. **退出语义不漂移**：`exit 7` 仍是 7，被信号杀死仍是 `128+sig`（今天那 5 行 relay 的语义由 A 的直接 `waitpid` 承接）。

---

### Task 0: 能力探针（只读，Phase 0）

**Files:**
- Create: `deploy/scripts/acceptance/probe_clone3_shape.py`
- 产物：`tmp/n80/probe-clone3-shape.log`

**Interfaces:**
- Produces: 四条 arm 的实测结论，决定 Task 1（必须）与 Task 2（可选）是否继续。

- [ ] **Step 1: 写探针脚本**

四条 arm，各起一个独立 python 进程（`clone` 不可逆），父进程只做 `waitpid` 与读 `/proc/<child>/stat`：

| arm | 调用 | 线程形态 |
|---|---|---|
| A1 | `clone3(flags=NEWUSER\|NEWPID, exit_signal=SIGCHLD, stack=0)` | 单线程 |
| A2 | 同 A1 | 多线程（先起一个活线程） |
| A3 | `clone(flags=NEWUSER\|NEWPID, stack=0)`（老 syscall） | 多线程 |
| A4 | `clone3(flags=NEWUSER\|NEWPID\|NEWNS\|NEWNET, …)` | 多线程 |

每条打印：返回值、`errno`、子进程 `NSpid:`（读 `/proc/<pid>/status`）、宿主视角 `PPid`（读 `/proc/<pid>/stat`）。

- [ ] **Step 2: 在 worker 容器里跑，落日志**

Run:
```bash
deploy/scripts/open-cluster-tunnel.sh
export KUBECONFIG="$PWD/tmp/k0s/kubeconfig"
mkdir -p tmp/n80
kubectl -n sandlock exec e2b-worker-0 -- python3 - < deploy/scripts/acceptance/probe_clone3_shape.py \
  | tee tmp/n80/probe-clone3-shape.log
```
Expected: A1/A2 成功；A3 记录成功或失败（clone3 的备选）；A4 成功或给出明确 errno。

- [ ] **Step 3: 判读并汇报**

A2 失败 ⇒ 停下汇报，N80 维持“挂 backlog”。A2 成功 ⇒ 进 Task 1。A4 结果单独记录，供 Task 2 使用。

- [ ] **Step 4: commit**

```bash
git add deploy/scripts/acceptance/probe_clone3_shape.py
git commit -m "probe(n80): can clone3 hand the leader both namespaces at once?"
```

### Task 1: clone3 建箱，删掉中间进程 B

**Files:**
- Modify: `third_party/sandlock/crates/sandlock-core/src/sandbox.rs:2380-2720`（spawn 的 child/parent 两半）、`:2810-2890`（管道与 leader pid）
- Modify: `third_party/sandlock/crates/sandlock-core/src/context.rs:692-800`（pid_ns 分支注释与 `ChildSpawnArgs`）
- Modify: `third_party/sandlock/crates/sandlock-core/src/procfs.rs:44-70`（leader 来源注释）
- Modify: `third_party/sandlock/crates/sandlock-supervise/src/serve.rs:1130-1170`（`probe_userns_self_map` 改用 clone3 形态）
- Test: `third_party/sandlock/crates/sandlock-core/tests/integration/test_pid_ns.rs`

**Interfaces:**
- Produces: `Sandbox::pid()` 与内部 `child_pid` 同值（不再有“直接子进程 ≠ leader”）；`runtime.leader_pid` 由 `clone3` 返回值直接填充。
- Consumes: Task 0 的 A2 结论。

- [ ] **Step 1: 写 RED 用例（必须多线程 runtime）**

在 `test_pid_ns.rs` 新增（断言精确相等，不用 `contains`）：

```rust
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn pid_ns_leader_is_the_direct_child_of_the_spawner() {
    let dir = /* scratch dir, 0777 */;
    let euid = unsafe { libc::geteuid() };
    let egid = unsafe { libc::getegid() };
    let mut sb = Sandbox::builder()
        .pid_ns(true).user(euid, egid).fs_write(&dir)
        .build().unwrap();
    sb.userns_self_map = true;
    let _h = sb.run(&["sh", "-c", "sleep 30"]).await.unwrap();
    let leader = sb.pid().expect("leader pid");
    assert_eq!(proc_ppid(leader), std::process::id() as i32);
}
```

`proc_ppid(pid: i32) -> i32` 是本文件新增的小 helper：读 `/proc/<pid>/stat`，从最后一个 `)` 之后取第 2 个字段（state 之后的 ppid）。

- [ ] **Step 2: 跑，确认失败**

Run（容器内树在 `/src`；以 `third_party/sandlock/docs/test-baseline.md` 的规范跑法为准）:
```bash
cd third_party/sandlock
docker run --rm -v "$PWD:/src" -w /src sandlock-dev:latest \
  cargo test -p sandlock-core --test integration pid_ns_leader_is_the_direct_child_of_the_spawner
```
Expected: FAIL —— 今天的 `PPid` 是中间进程 B 的 pid。

- [ ] **Step 3: 实现 clone3 的 A 侧调用**

在 `sandbox.rs` 加一个只做一件事的函数：
```rust
/// Create the sandbox leader directly inside fresh namespaces.
unsafe fn clone3_new_namespaces(flags: u64, exit_signal: u64) -> std::io::Result<libc::pid_t>
```
`clone_args { flags, exit_signal, stack: 0, ..Default::default() }`，走 `libc::syscall(libc::SYS_clone3, …)`，`ENOSYS` 时返回具名错误（不静默回退）。`pid_ns` 为真时用它替换 `fork()`；`real_uid`/`real_gid` 在调用前捕获。

- [ ] **Step 4: C 侧就位（顺序固定）**

1. `prctl(PR_SET_PDEATHSIG, SIGKILL)`；
2. 三选一写 map（照抄今天 B 的 `write_id_maps` 分支：remap / self_map / plain），`real_uid` 来自 clone3 前的捕获；
3. 进入 `confine_child`。

父死 race 的兜底不再用 `getppid()`：C 在 `confine_child` 读到 ready 管道 EOF 时必须 `fail!`（A 已死）。

- [ ] **Step 5: 删 B 的残留**

- 删中间进程整段（`unshare(NEWUSER)`、`unshare(NEWPID)`、`fork`、`waitpid`、`_exit(code)`）；
- 删 `pipes.leader_pid_r/leader_pid_w` 及其创建/关闭/drop；
- `runtime.leader_pid = Some(clone3 返回值)`；`child_parent_pid` 的注释按新形态重写；
- `procfs.rs` 与 `context.rs` 的“中间进程转述”注释逐条更正。
- `serve.rs` 的 `probe_userns_self_map()` 从“fork + `unshare(NEWUSER)` + 子进程写 map”改成 clone3 形态（`clone3(NEWUSER)` + 子进程写 map）：探测形态必须与运行形态一致，否则就是 N79 那类“前提没了、文档还在说”。

- [ ] **Step 6: 跑 GREEN 与本套件**

Run: 同 Step 2 的命令 + `cargo test -p sandlock-core --test integration pid_ns`
Expected: 新用例与 `test_pid_ns` 全绿；`pid_ns_self_map_restores_guest_root` 仍绿。

- [ ] **Step 7: 跑 fork 门禁八相位**

Run:
```bash
cd third_party/sandlock
docker run --rm -v "$PWD:/src" -w /src sandlock-dev:latest sh scripts/test-all.sh
```
Expected: 八相位 ≥ 基线，0 failed，skip/xfail 逐条不变。

- [ ] **Step 8: 两条专项用例**

1. Review Focus 1：对同一形态（pidns + 多线程 spawner）断言杀掉 spawner 后 leader 在超时内消失；
2. Review Focus 5：`sh -c 'exit 7'` 的退出码精确等于 7；`sh -c 'kill -9 $$'` 精确等于 137（`128+9`）—— relay 删除后语义不得漂移。

- [ ] **Step 9: commit（fork）**

```bash
cd third_party/sandlock
git add crates/sandlock-core/src/sandbox.rs crates/sandlock-core/src/context.rs \
        crates/sandlock-core/src/procfs.rs crates/sandlock-core/tests/integration/test_pid_ns.rs
git commit -m "feat(n80): clone3 hands the leader both namespaces; the middle process retires"
```

### Task 2: 零 unshare + profile 收紧

**Files:**
- Modify: `third_party/sandlock/crates/sandlock-core/src/sandbox.rs`（clone3 flags 加位）
- Modify: `third_party/sandlock/crates/sandlock-core/src/context.rs:826-845`（删 `unshare(CLONE_NEWNET)`）
- Modify: `third_party/sandlock/crates/sandlock-core/src/realroot.rs:221-240`（删 `unshare(CLONE_NEWNS)`）
- Modify: `deploy/seccomp/sandlock-worker.json`
- Test: `tests/unit/test_docs_only_point_at_repo_artifacts.py` 邻近的 profile 钉子（或 `tests/unit/test_worker_manifest_permissions.py`）

**Interfaces:**
- Consumes: Task 0 的 A4 结论、Task 1 的 `clone3_new_namespaces`。
- Produces: `pid_ns && real_root` ⇒ flags 含 `CLONE_NEWNS`；`net_isolation` ⇒ flags 含 `CLONE_NEWNET`；sandlock 运行时代码再无 `unshare` 调用（探针与非 pid_ns 形态除外）。

- [ ] **Step 1: 写 RED 钉子**

两条：
1. profile 钉子：`deploy/seccomp/sandlock-worker.json` 里 `clone3` 不得出现在 `SCMP_ACT_ERRNO` 条目里（今天有，见 `:689`）；
2. 行为钉子：netns 建箱后沙箱的 `ifaces` 仍是 `lo`、真根下 shebang 脚本仍 `rc=0`（沿用既有探针断言，不新造判据）。

- [ ] **Step 2: 跑，确认失败**

Run: `python3 -m pytest tests/unit/test_worker_manifest_permissions.py -k clone3 -v`
Expected: FAIL（profile 里仍有 ERRNO 条目）。

- [ ] **Step 3: clone3 加 NEWNS/NEWNET，删两处 unshare**

- flags 组装：`NEWUSER|NEWPID` +（有真根时 `NEWNS`）+（`net_isolation` 时 `NEWNET`）；
- 删 `context.rs:836` 的 `unshare(CLONE_NEWNET)` 与 `realroot.rs:225` 的 `unshare(CLONE_NEWNS)`，保留其后的 `lo up`、DNS bind、策略挂载与 `pivot_root`；
- `context.rs` 的“5b”注释改为“netns 由 clone3 创建”。

- [ ] **Step 4: 改 profile（两处同批）**

- 删掉 `clone3 → SCMP_ACT_ERRNO` 条目，并在 allow 组里显式保留 `clone3`；
- 收窄 `unshare`：把条件规则改成**整条拒绝**（建箱已不需要它），或退一步只允许 `CLONE_NEWCGROUP|CLONE_NEWUTS|CLONE_NEWIPC`；
- 同一提交内更新两个文件的指纹/内嵌副本（`seccomp-installer.yaml` 的 ConfigMap）。

- [ ] **Step 5: 跑 GREEN + 门禁八相位**

Run: Step 2 的命令；再跑 `sh scripts/test-all.sh`
Expected: 钉子绿、八相位 ≥ 基线。

- [ ] **Step 6: 集群现场验收**

先 `kubectl apply` 新 profile（顺序：profile 先于镜像，否则真根的 `NEWNS` 会被旧档拒），再建箱实测：
`ifaces=lo`、shebang `rc=0`、`/proc` 形状不变、两条冒烟（`multinode_smoke.py` / `deployment_smoke.py`）全绿。

- [ ] **Step 7: 重建 wheel + 父仓指针 + 上线**

`./deploy/scripts/build-sandlock-wheels.sh` → 父仓 `git add third_party/sandlock` → 构建推送镜像 → `apply.sh` → 现场复核 `kubectl diff` 0 行。

- [ ] **Step 8: commit（含文档）**

更新 `docs/isolation-boundaries.md` §4（三个角色 → 两个）、`docs/open-issues.md` 的 N80 行（改“已完成”并写明 profile 两处同批）、`docs/deploy-clusters.md` 新发版节。

### Task 3（本轮不做）: 身份映射统一到 A 写

**Files:** `sandbox.rs`（map ready/done 管道扩到非特权路径）、`context.rs:255-340`（`write_id_maps` 与 `write_privileged_id_maps` 合并）、`serve.rs:1142`（探测改 clone3 形态）。

**为什么先列不做:** 它与 Task 1/2 动同一条身份路径；绑在一起会让“建箱失败”无法二分。它的两个真实收益（错误归因直接返回、C 出生即完整个体）都不是 N80 的目标。

- [ ] Step 1: 写 RED：非特权形态下 map 由 A 写、C 只等 gate；
- [ ] Step 2: 验证 A 写 `/proc/<C>/setgroups` = deny 与 gid_map 在目标机内核上的权限；
- [ ] Step 3: 合并两个写函数，删 `context.rs` 的三选一；
- [ ] Step 4: 三条身份形态与 route-B 全套回归；
- [ ] Step 5: 独立发版，不与 Task 1/2 同批。
