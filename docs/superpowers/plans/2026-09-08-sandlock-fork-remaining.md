# sandlock fork 收尾计划（F15 帧-描述符归属修复 + 台账 + 门禁/wheel + 推送发布）

> **For agentic workers:** REQUIRED SUB-SKILL: 用 `superpowers:subagent-driven-development`（推荐）或
> `superpowers:executing-plans` 逐任务实施。步骤用 `- [ ]` 勾选跟踪，一个 Task 一个提交。

**Goal:** 清掉 sandlock fork 唯一剩余的**代码**缺陷 —— init 控制通道把「本读单元的全部
SCM_RIGHTS 描述符」当成「本帧的描述符」（记为 **F15**，原登记见 `docs/task-backlog.md` #22
末段与 fork `docs/e2b-integration.md:443`），并把 fork 侧的台账、门禁、wheel、E2B 复验、推送
与发布门收口到可交付状态。

**Architecture:** 帧头新增 1 字节 `n_fds`，`FRAME_VERSION` 1→2（**wire 不兼容**，同批发版）；
`fdrecv::recv` 的控制缓冲按 `MAX_FDS_PER_READ = 16` 分配，并对 `MSG_CTRUNC` / `MSG_TRUNC`
fail-closed；`run_init` 读循环用纯函数 `take_frame_fds` 按「帧自己声明的数量」切分本读单元的
描述符队列，声明与队列不符 ⇒ 整读单元拒绝、不投毒任何一帧。发送侧（`executor.rs` /
`supervisor.rs` / `oci/init.rs`）在 `encode_frame` 处声明每帧 fd 数（只有 `RunExec` 是 3）。

**Tech Stack:** Rust（`sandlock-core` / `sandlock-oci`）、`libc` `recvmsg` + SCM_RIGHTS、
`scripts/test-all.sh` 基线漂移门禁（非 root 8 档 + root 3 档）、
`deploy/scripts/build-sandlock-wheels.sh`（cp314 双架构 + verify）、E2B test-runner 镜像
`e2b-sandlock-test:latest`、pytest。

## 范围（先说清不做什么）

- **做**：F15 代码修复（Task 1–4）、E2B 侧接线复验（Task 5）、runner 残留两条（Task 6）、
  fork 台账收口（Task 7）、设计候补一次评估文档（Task 8）、**F16 —— route-B worker 侧客户端面
  （Task 9，T5 的真前置）**、推送与上游 PR（Task 10，需授权）、发布门（Task 11）。

### fork「小项」逐条对账（决定它们是任务还是一行台账）

| 项 | 代码/测试是否已落地 | 证据 | 归属 |
|---|---|---|---|
| FUP-01 `--pid-ns` 接线 | ✅ 全关 | `262c0cf`；`main.rs:478` + 单测 `main.rs:1214` + `cli_test.rs` 端到端 | Task 7 关台账 |
| FUP-07 no-clobber + I1 谓词 | ✅ 全关（**两半都有独立回归**） | `48968a5`；`profile.rs:60`、`profile_integration.rs:40`、`mediation_2uid.rs:1030`、I1 半 `sandbox/tests.rs:648 mediation_active_covers_policy_fn_deny_capability`（注释点名「the I1 shape」） | Task 7 关台账 |
| FUP-10 逃逸会话比较 + `dead_groups` 去重 | ✅ 全关 | `d5bbdd8`；`init/mod.rs:540-566`（`getsid` 比较 + `unique_signal_pgids`）+ 单测 `:970`/`:981`；FUP-13 亦记「重复投送面由 FUP-10 关闭」 | Task 7 关台账 |
| FUP-15 `panic=abort` + `strip` | ✅ 全关 | `b1e2e32`；`Cargo.toml:24-26` | Task 7 关台账 |
| FUP-09 flake 证据留存 | ⚠️ **半**：约定写进 `test-all.sh:33-38`，但「脚本化留存」未做（`run` 直接 `tee`，重跑即覆盖） | `scripts/test-all.sh:93-121` | **Task 6 Step 2** |
| FUP-17 runner 硬化 | ⚠️ **半**：无参/`--wheels` 拒 root 已实现（`:130-141` + 三 root 档正向守卫）；**root/65534 增量缓存隔离未做**（`rg CARGO_INCREMENTAL scripts/test-all.sh` 无命中） | 同上 | **Task 6 Step 1** |
| FUP-02 / FUP-08 | 按决策关闭的长期项，无代码动作 | 条目内「为什么留」即决策 | Task 7 关台账 |

⇒ **不要再实现 FUP-01/07/10/15**；真正还剩的代码动作只有 Task 6 那两条（合计约 10 行 shell）。

### fork「候补」FUP-19/20/21：不进实施队列

- 三条仍是**触发式候补**：per-child 正向 fs/bind 强制（FUP-19）、credential per-child 归因
  （FUP-20）、port-aware `update_network`（FUP-21）。
- F12/F13/F14 都**没有改变它们的触发面**：F12 修的是 TGID 建模（不是 child 归因），F13/F14 是
  挂载保护与能力 gate。destination 级泄漏已由 connect verdict 关闭，凭据/端口级归因属下一设计层。
- 成本不对称：任一实现都要扩 wire/verdict 结构 + 逐消费点复核，而 E2B/产品当前无需求。
- Task 8 只输出一份评估（是否把 FUP-19/20 合并成单个 pid-passthrough 设计 + 明确触发条件），
  **不写代码**。

- **不做（属 E2B/运维侧）**：route-B supervise 的**部署与 envd 接线**、T5 strict xfail 摘除与
  `mediation_run_as='supervisor'` 降级档清理（main backlog #5；两项均已 2026-09-10 完成）、
  T1 真实 XFS 复测、O1/O2/O3、
  E1.2/E8.1 目标机部署。其中**「fork 侧要能提供 Python 可达的接入面」这一部分已挪进本计划
  = Task 9（F16）**，剩下的才是纯 E2B/运维动作。

## Global Constraints

- 不推送远程：Task 0–9 全程本地提交；`git push` 只允许出现在 Task 10 且用户明确授权之后。
- 提交粒度：一个 Task 一个 fork 提交（`fix(...)` / `test(...)` / `docs(...)`）；主仓库的
  子模块指针与文档各自单独提交。
- **诊断代码不得入库**：候选补丁里 `SANDBOX_FUP23_MODES`（`init/mod.rs` 的 `_exit(64+…)`）、
  `SANDBOX_FUP23_QUEUE`（`init/mod.rs` 的 `_exit(100+…)`）与 `instance.rs` 的 `eprintln!` 三段
  必须整段丢弃（作者本意即「不留在仓库」）。
- 断言精度：新用例一律整串/整字节精确比较（`assert_eq!(out, b"01O".to_vec())` 形态），
  禁止 `contains` / `starts_with` / 子串式断言。
- RED 先行：Task 1 必须在 tip `e045881` 上跑出可复现红档并留日志，才允许进 Task 2。
- 门禁路径纪律：`scripts/test-all.sh` 必须从短路径 `/src` 跑（嵌套 worktree 会把 supervise
  注册套接字路径顶过 108 字节 `sun_path` 上限 ⇒ `test_supervise_path_serve_...` 假红）。
- 基线漂移即失败：`docs/test-baseline.md` 的计数更新必须与新增用例在**同一提交**里；
  红档按 FUP-09 归档为 `<label>-r1.log` + `<label>-final.log`，报告里写明首轮红的是什么。
- 不变量：`wheel 产物 = 代码 tip`；`wheels/fork/SHA256SUMS.supervise` 的 HEAD 行 ==
  子模块指针 == fork HEAD。docs-only 增量按既定约定不重钉，但必须重建并比对 sha256 逐字节一致
  才算证据成立。
- 批量复跑前后各查一次 loop：起跑前 `losetup -D`（上一波曾累积 293 个 loop ⇒ gate B 出现
  10 个 XFS 门禁 error）。
- 语言风格：文档中文、代码注释英文，与 fork `docs/` 现有条目一致。

## 文件结构（谁负责什么，本计划动谁）

| 文件 | 职责 | 动作 |
|---|---|---|
| `third_party/sandlock/crates/sandlock-core/src/init/proto.rs` | 帧编解码、`FRAME_VERSION`、头长 | Task 2 改（n_fds / Header / TooManyFds） |
| `.../sandlock-core/src/init/fdrecv.rs` | 一次 `recvmsg` 收字节 + fd | Task 2 改（CTRUNC/TRUNC fail-closed、缓冲按 16） |
| `.../sandlock-core/src/init/mod.rs` | `run_init` 控制环 + `ExecStdioPlan` 装配（FUP-23 已修） | Task 2 改（`take_frame_fds` + 按帧切片 + `handed` 累计） |
| `.../sandlock-core/src/init/executor.rs` | 发送侧 4 处 `encode_frame`（`:159 :201 :307 :480`） | Task 2 改（声明 3 / 0） |
| `.../sandlock-oci/src/init.rs`、`.../sandlock-oci/src/supervisor.rs` | oci 侧帧头测试与 `decode_header` 消费者 | Task 2 改（签名适配） |
| `.../sandlock-oci/tests/integration.rs` | 真 `run_init` 控制环夹具（F1.6 / FUP-23 harness） | Task 1 加 RED；Task 2 改 harness 头（`SLK_HEADER_LEN` 10→11） |
| `.../sandlock-core/src/instance.rs` | exec 下发三端 | **不改**（候选补丁在此只有诊断段，丢弃） |
| `.../docs/test-baseline.md`、`docs/CHANGELOG.md`、`docs/e2b-integration.md`、`docs/fork-plan-followups.md` | 基线计数 / 变更日志 / 唯一事实源 / follow-ups 台账 | Task 2/3/4/7 更新 |
| `deploy/scripts/build-sandlock-wheels.sh`（主仓库） | wheel 双架构构建 + verify | Task 4 执行 |
| `.../scripts/test-all.sh` | 门禁 runner（root 守卫、日志、基线比对） | Task 6 改（增量缓存隔离 + 日志轮换） |
| `.../sandlock-core/src/control.rs`、`.../sandlock-ffi/src/`、`python/src/sandlock/` | route-B worker 侧客户端 / C ABI / Python 包装 | **Task 9（F16）新增接入面** |
| `docs/task-backlog.md`、`docs/HANDOFF.md`（主仓库） | 路线图与交接 | Task 5/7/9 更新 |

---

## Task 0: 前置核对（只读，不改仓库）

**Files:**
- Create: `third_party/sandlock/tmp/sdd/f15-ledger-audit.md`（取证报告，`tmp/` 已被 gitignore）

- [x] **Step 1：确认三方一致的起点**

```bash
cd /Users/polus/project/ai/sandlock-e2b
git -C third_party/sandlock status --short           # 期望：空
git -C third_party/sandlock rev-parse --short HEAD   # 期望：e045881
git -C third_party/sandlock log --oneline -1 880a1ec  # 期望：880a1ec fix(core): move exec stdio …（代码 tip）
head -3 wheels/fork/SHA256SUMS.supervise             # 期望：HEAD 行 = e045881
git submodule status                                # 期望：e0458811fd2… 无 +/- 前缀
```

任一期外 ⇒ 停下报告，不要在脏树上开工。

- [x] **Step 2：证明候选补丁不能直接 apply**

Run: `cd third_party/sandlock && git apply --check tmp/sdd/fup23-wip-frame-fd-count.patch`
Expected: `error: patch failed: crates/sandlock-core/src/init/mod.rs:166` / `patch does not apply`

结论写进报告：**补丁写在 FUP-23 修复之前的树上（`87003de`），Task 2 走「按补丁重写」而不是
`git apply`**；它仍是唯一的实现蓝本（用 `git show 880a1ec -- crates/sandlock-core/src/init/mod.rs`
对照新的冲突面）。

- [x] **Step 3：台账与代码核对（驱动 Task 6 与 Task 7）**

```bash
cd third_party/sandlock
rg -n "pb.pid_ns|builder.pid_ns" crates/sandlock-cli/src/main.rs | head -3   # FUP-01 已接线（:472-479, 单测 :1214）
rg -n "FUP-07" crates/sandlock-core/src/profile.rs crates/sandlock-cli/tests/profile_integration.rs | head
rg -n "FUP-10" crates/sandlock-core/src/init/mod.rs | head                    # 会话比较 + pgid 去重（:540-566）
sed -n '24,27p' Cargo.toml                                                     # FUP-15 panic=abort + strip
rg -n "FUP-09|refuses to run as root" scripts/test-all.sh | head               # FUP-09/17 纪律（:33-39）
rg -n "FUP-(01|07|10|15|17)" docs/CHANGELOG.md | head                         # 关闭证据 hash
rg -n "CARGO_INCREMENTAL" scripts/test-all.sh || echo "FUP-17 后半未做 → Task 6"
sed -n '93,121p' scripts/test-all.sh                                          # run() 是否覆盖同名日志 → Task 6
```

把「代码已落地 / 台账未关闭」各行连同 hash 与文件行号写进（FUP-01/07/10/15 全关；FUP-09/17 各记「已落地的那半 + 剩余的那半」）
`tmp/sdd/f15-ledger-audit.md`，Task 6 直接引用。

- [x] **Step 4：本 Task 不产生仓库提交**（报告在 `tmp/`，被 ignore）。

---

## Task 1: F15 RED —— 一个读单元两帧时的 stdio 归属

**Files:**
- Modify: `third_party/sandlock/crates/sandlock-oci/tests/integration.rs`（紧跟
  `exec_frames_deliver_their_own_output_and_leave_no_descriptor_behind`（`:1259`）追加）
- Test: 同文件（root 档 `--oci-root` 套件）

**Interfaces:**
- Consumes: `spawn_run_init_probe()`（`:881`）、`RunInitProbeGuard`、`wait_ready_byte`、
  `FrameReader::next_payload`（`:1178`）、`read_bytes_from`、`reply_tag`、`reply_pid`、
  `frame_bytes(SLK_TYPE_REQ, payload)`（`:865`）、
  `sandlock_oci::fdpass::send_with_fds(&UnixStream, &[u8], &[RawFd])`
- Produces: 无（纯测试；Task 2 的 GREEN 以本用例转绿为准）

- [x] **Step 1：写失败测试**（两帧一次写出 + 六个描述符）

```rust
/// F15: the control channel is a `SOCK_STREAM`, so one `recvmsg` can return
/// several frames while the kernel hands back **one concatenated fd list**.
/// Two `RunExec` frames written once with six descriptors must give each exec
/// its own three — the positional guess ("three arrived, so they are mine") is
/// what made exec #2 write into exec #1's pipe and throw its own output away.
#[test]
fn two_exec_frames_in_one_read_unit_get_their_own_stdio() {
    if !cfg!(target_os = "linux") {
        eprintln!("skipping: /proc-based fd checks and exec probes are Linux-only");
        return;
    }
    let (pid, ctl, ready_r) = spawn_run_init_probe();
    let guard = RunInitProbeGuard { pid, ready_r };
    assert_eq!(
        wait_ready_byte(ready_r, Instant::now() + Duration::from_secs(5)),
        Some(b'r'),
        "probe child never entered run_init"
    );

    let mut bytes = Vec::new();
    let mut child_ends: Vec<RawFd> = Vec::new();
    let mut stdout_read: Vec<RawFd> = Vec::new();
    let mut stderr_read: Vec<RawFd> = Vec::new();
    for tag in ["A", "B"] {
        let mut p = [[0i32; 2]; 3];
        for pair in p.iter_mut() {
            assert_eq!(unsafe { libc::pipe2(pair.as_mut_ptr(), 0) }, 0, "pipe2");
        }
        let payload = format!(
            r#"{{"req":"runexec","argv":["/bin/sh","-c","printf '{tag}'"],
                "env":[],"cwd":null,"detach":false}}"#
        );
        bytes.extend_from_slice(&frame_bytes(SLK_TYPE_REQ, payload.as_bytes()));
        // The three ends init must install as 0/1/2: stdin read, stdout write,
        // stderr write. Everything else stays ours.
        child_ends.extend_from_slice(&[p[0][0], p[1][1], p[2][1]]);
        stdout_read.push(p[1][0]);
        stderr_read.push(p[2][0]);
        // Nobody writes this exec's stdin; our copy of the write end goes now.
        unsafe { libc::close(p[0][1]) };
    }
    // ONE write: both frames plus all six descriptors in a single `sendmsg`,
    // which is what forces them into one read unit on the init side.
    sandlock_oci::fdpass::send_with_fds(&ctl, &bytes, &child_ends)
        .expect("send two RunExec frames with six fds");
    // Our copies of the handed-over ends must go: while we hold a write end,
    // our own reader can never see EOF for that exec.
    for fd in &child_ends {
        unsafe { libc::close(*fd) };
    }

    let mut reader = FrameReader::new(&ctl);
    let first = reader
        .next_payload(Instant::now() + Duration::from_secs(10))
        .expect("init must answer the first RunExec");
    let second = reader
        .next_payload(Instant::now() + Duration::from_secs(10))
        .expect("init must answer the second RunExec");
    assert_eq!(reply_tag(&first).as_deref(), Some("started"));
    assert_eq!(reply_tag(&second).as_deref(), Some("started"));
    let pa = reply_pid(&first).expect("Started carries the child pid");
    let pb = reply_pid(&second).expect("Started carries the child pid");
    assert_ne!(pa, pb, "two exec frames are two distinct children");

    let out_a = read_bytes_from(stdout_read[0], 1, Instant::now() + Duration::from_secs(10));
    let out_b = read_bytes_from(stdout_read[1], 1, Instant::now() + Duration::from_secs(10));
    assert_eq!(out_a, b"A", "exec #1 stdout must be exactly its own byte");
    assert_eq!(out_b, b"B", "exec #2 stdout must be exactly its own byte");
    for fd in stderr_read.iter().chain(stdout_read.iter()) {
        unsafe { libc::close(*fd) };
    }
    drop(guard);
}
```

> 关键约束只有三条：**一次 `send_with_fds` 写两帧六端**（否则两帧不进同一读单元，RED
> 无效）、**断言逐字节精确**、**不依赖 sleep（全部走 deadline）**。
> `frame_bytes` 现在是两参（v1 头）；Task 2 Step 5 把头扩到 11 字节后，这个用例里的调用同步
> 改成 `frame_bytes(SLK_TYPE_REQ, 3, payload.as_bytes())`（每个 `RunExec` 声明 3 端）。

- [x] **Step 2：跑测试确认失败**

```bash
cd /Users/polus/project/ai/sandlock-e2b
losetup -D 2>/dev/null || true
docker run --privileged --rm -v "$PWD/third_party/sandlock":/src -w /src \
  sandlock-dev:latest bash -c '
    cd /src && CARGO_TARGET_DIR=/src/target-linux \
    cargo test -p sandlock-oci --offline --test integration \
      two_exec_frames_in_one_read_unit -- --test-threads=1'
```

Expected: **FAILED**。可接受的红形两种（留档确认是哪一种）：
① 第二帧 `assert_eq!(out_b, b"B")` 失败且 `left: []`（内核按 3 端截断且今天不看
`MSG_CTRUNC` ⇒ 第二帧要么拿不到三端、要么复用第一帧的三端）；② `out_a` 拿到两帧共写的
`b"AB"`（`left: [65, 66]` 形态）。若**意外通过** ⇒ 说明两帧没进同一个读单元，回到
`send_with_fds` 检查是否拆成两次 `sendmsg`，不要继续往下做。

留档：`third_party/sandlock/tmp/sdd/f15-red-r1.log`。

- [x] **Step 3：把红档摘要（失败断言 + left/right 原文）写进提交信息并提交**

```bash
cd third_party/sandlock
git add crates/sandlock-oci/tests/integration.rs
git commit -m "test(oci): pin per-frame stdio ownership for coalesced exec frames (F15 RED)"
```

---

## Task 2: F15 GREEN —— 帧头声明 fd 数 + 截断 fail-closed

**Files:**
- Modify: `crates/sandlock-core/src/init/proto.rs`（`:42` `FRAME_VERSION`、`:60` `FRAME_HEADER_LEN`、`Frame`、`FrameError`、`encode_frame`、`decode_header`、`decode_frame`、`mod tests`）
- Modify: `crates/sandlock-core/src/init/fdrecv.rs:14`（`pub fn recv`）
- Modify: `crates/sandlock-core/src/init/mod.rs`（`:95` Resp 编码、`:793` `fdrecv::recv(ctl, 3)`、`:885-903` RunExec 分支与 `received.handed`）
- Modify: `crates/sandlock-core/src/init/executor.rs:159,201,307,480`
- Modify: `crates/sandlock-oci/src/init.rs:29-90`、`crates/sandlock-oci/src/supervisor.rs:183,273,322,337,1535,1546`
- Modify: `crates/sandlock-oci/tests/integration.rs:856-868,979-982,1178-1183`
- Test: `crates/sandlock-core/src/init/mod.rs` 新 `mod fd_assignment_tests`（4 条）+ `oci/src/init.rs` 头校验 2 条

**Interfaces:**
- Consumes: Task 1 的 RED 用例
- Produces: `proto::FRAME_VERSION = 2`、`proto::FRAME_HEADER_LEN = 11`、
  `proto::MAX_FDS_PER_FRAME: u8 = 8`、`proto::MAX_FDS_PER_READ: usize = 16`、
  `proto::Header { kind, n_fds, payload_len }`、`FrameError::TooManyFds(u8)`、
  `encode_frame(kind: FrameKind, payload: &[u8], n_fds: u8) -> io::Result<Vec<u8>>`、
  `Frame<'a> { kind, payload, n_fds, consumed }`、
  `init::take_frame_fds(cursor: &mut usize, declared: u8, available: usize) -> Option<Range<usize>>`

- [x] **Step 1：proto.rs —— 头多一字节，版本进 2**

```rust
pub const FRAME_VERSION: u8 = 2;
/// Fixed header size: magic (4) + version (1) + type (1) + fd count (1) + length (4).
pub const FRAME_HEADER_LEN: usize = 11;
/// Most descriptors one frame may declare it owns. Only `RunExec` uses any
/// (exactly 3); the ceiling exists to reject an absurd declaration before it can
/// starve the reader's control buffer.
pub const MAX_FDS_PER_FRAME: u8 = 8;
/// Most descriptors one read unit may carry (`fdrecv`'s control buffer).
pub const MAX_FDS_PER_READ: usize = 16;

pub struct Frame<'a> {
    pub kind: FrameKind,
    pub payload: &'a [u8],
    /// Descriptors this frame owns, taken from the front of the read unit's fd
    /// queue (F15). `0` for every verb but `RunExec`.
    pub n_fds: u8,
    pub consumed: usize,
}

pub enum FrameError {
    /* …existing… */
    /// Declared descriptor count exceeds [`MAX_FDS_PER_FRAME`]: nothing may be
    /// assigned from the fd queue on its behalf.
    TooManyFds(u8),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Header {
    pub kind: FrameKind,
    pub n_fds: u8,
    pub payload_len: usize,
}
```

`encode_frame` 增形参并加校验：

```rust
    if n_fds > MAX_FDS_PER_FRAME {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            format!("cannot declare {n_fds} fds on one frame: cap is {MAX_FDS_PER_FRAME}"),
        ));
    }
    out.push(kind.wire_byte());
    out.push(n_fds);
    out.extend_from_slice(&(payload.len() as u32).to_le_bytes());
```

`decode_header` 返回 `Header`：`let n_fds = bytes[6];` → `TooManyFds` 检查 → 长度读
`bytes[7..11]`；`decode_frame` 用 `header.payload_len` 算 `total` 并填 `n_fds`。
`Display for FrameError` 增加 `TooManyFds` 分支（消息点名声明值与上限）。

- [x] **Step 2：fdrecv.rs —— 截断即失败**

```rust
    let n = unsafe { libc::recvmsg(fd, &mut msg, 0) };
    if n < 0 {
        return Err(std::io::Error::last_os_error());
    }
    if msg.msg_flags & libc::MSG_CTRUNC != 0 {
        // The kernel discarded descriptors that did not fit. Nothing in this
        // read unit can be trusted, so the caller loses the channel instead of
        // handing a workload somebody else's stdio end.
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "control channel: SCM_RIGHTS truncated (descriptors discarded by the kernel)",
        ));
    }
    if msg.msg_flags & libc::MSG_TRUNC != 0 {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "control channel: frame payload truncated (lost frame boundary)",
        ));
    }
```

并把函数 doc 里「one frame per sendmsg，fds bound to that sendmsg」的旧承诺改写成
「一个读单元可含多帧，fd 是一条拼接列表，归属由帧头 `n_fds` 决定」。

- [x] **Step 3：init/mod.rs —— 按声明切队列的纯函数 + 读循环改造**

```rust
/// Assign one frame its own slice of a read unit's descriptor list, by the count
/// the frame declared (F15). `None` means the read unit cannot satisfy the
/// declaration, and the caller fails the whole unit closed rather than hand a
/// frame somebody else's descriptor.
fn take_frame_fds(
    cursor: &mut usize,
    declared: u8,
    available: usize,
) -> Option<std::ops::Range<usize>> {
    let end = cursor.checked_add(declared as usize)?;
    if end > available {
        return None;
    }
    let range = *cursor..end;
    *cursor = end;
    Some(range)
}
```

读循环改动（当前形态：`fdrecv::recv(ctl, 3)`；RunExec 用 `received.fds[0..3]`；
`received.handed = 3`）：

```rust
        let (bytes, fds) = match fdrecv::recv(ctl, proto::MAX_FDS_PER_READ) {
        // …
        let mut fd_cursor = 0usize;
        let mut handed = 0usize;
        while off < bytes.len() {
            // decode_frame 成功后，先按声明给这一帧切描述符：
            let frame_fds = match take_frame_fds(&mut fd_cursor, frame.n_fds, received.fds.len()) {
                Some(range) => range,
                None => {
                    replies.push(Resp::Err {
                        msg: "control frame declares more descriptors than the read unit carries"
                            .into(),
                    });
                    break;
                }
            };
            off += frame.consumed;
            // …Req::RunExec 分支：
            let frame_fds = &received.fds[frame_fds];
            if frame_fds.len() != 3 {
                replies.push(Resp::Err { msg: "exec needs 3 fds".into() });
                continue;
            }
            let stdio = [
                frame_fds[0].as_raw_fd(),
                frame_fds[1].as_raw_fd(),
                frame_fds[2].as_raw_fd(),
            ];
            // …spawn 成功后：
            handed += 3;
        }
        received.handed = handed; // 只有真交给子进程的才免泄漏计数
```

`mod.rs:95` 的 `proto::encode_frame(proto::FrameKind::Resp, &payload)` 补第三参 `0`。
**注意与 FUP-23 的 `ExecStdioPlan` 共存**：搬迁/身份校验逻辑（`:141` `EXEC_STDIO_BASE`、
`:198`、`:209`、`spawn` 签名 `:294`）一行都不要动，F15 只改「哪三个 fd 进 `stdio`」。

- [x] **Step 4：发送侧声明（其余 encode 站点）**

```bash
cd third_party/sandlock
rg -n "encode_frame\(|decode_header\(" crates/sandlock-core/src/init/executor.rs \
   crates/sandlock-oci/src/init.rs crates/sandlock-oci/src/supervisor.rs
```

规则：`RunExec` 三处（`executor.rs:159,201,307`）传 `3`；其余 Req（`supervisor.rs:183,273`）
与全部 Resp（`executor.rs:480`、`oci/src/init.rs:47`、`supervisor.rs:1535`）传 `0`；
`supervisor.rs:337`、`:1546` 的 `decode_header` 结果改用 `header.kind`。

- [x] **Step 5：测试夹具跟随头长**

- `crates/sandlock-oci/tests/integration.rs`：`:858` `SLK_VERSION: u8 = 1` ⇒ `2`；
  `:863` `SLK_HEADER_LEN: usize = 10` ⇒ `11`；`frame_bytes(kind, payload)` ⇒
  `frame_bytes(kind, n_fds, payload)`（长度字段偏移 `6..10` ⇒ `7..11`，见 `:981`、`:1180`）；
  既有调用点按帧类型补 `0` / `3`（含 Task 1 新增用例里的那一处 ⇒ 补 `3`）；
- `crates/sandlock-oci/src/init.rs:33` 的 `header(kind, len)` ⇒ `header(kind, n_fds, len)`，
  并新增两条单测：`n_fds = MAX_FDS_PER_FRAME + 1` ⇒ `Err(FrameError::TooManyFds(9))`；
  构造一个 10 字节 v1 头 ⇒ `Err(FrameError::Version(1))`（消息含期望版本 2）。

- [x] **Step 6：新增 4 条纯函数单测（与实现同提交）**

```rust
#[cfg(test)]
mod fd_assignment_tests {
    use super::take_frame_fds;

    /// F15: descriptors belong to the frame that *declared* them. A
    /// zero-descriptor frame ahead of an exec must not shift that exec's stdio
    /// onto the wrong pipe ends.
    #[test]
    fn a_zero_fd_frame_does_not_steal_the_next_frames_descriptors() {
        let mut cursor = 0usize;
        assert_eq!(take_frame_fds(&mut cursor, 0, 3), Some(0..0));
        assert_eq!(take_frame_fds(&mut cursor, 3, 3), Some(0..3));
        assert_eq!(cursor, 3, "the exec frame consumed exactly its own three");
    }

    #[test]
    fn two_exec_frames_split_one_queue_in_order() {
        let mut cursor = 0usize;
        assert_eq!(take_frame_fds(&mut cursor, 3, 6), Some(0..3));
        assert_eq!(take_frame_fds(&mut cursor, 3, 6), Some(3..6));
        assert_eq!(cursor, 6);
    }

    #[test]
    fn a_short_queue_fails_closed_without_consuming() {
        let mut cursor = 0usize;
        assert_eq!(take_frame_fds(&mut cursor, 3, 2), None);
        assert_eq!(cursor, 0, "a rejected declaration must not advance the queue");
    }

    #[test]
    fn zero_descriptor_frames_always_succeed() {
        let mut cursor = 0usize;
        for _ in 0..16 {
            assert_eq!(take_frame_fds(&mut cursor, 0, 0), Some(0..0));
        }
        assert_eq!(cursor, 0);
    }
}
```

- [x] **Step 7：跑测试确认通过**

```bash
docker run --privileged --rm -v "$PWD/third_party/sandlock":/src -w /src \
  sandlock-dev:latest bash -c '
    cd /src && CARGO_TARGET_DIR=/src/target-linux \
    cargo test -p sandlock-core --offline --lib &&
    cargo test -p sandlock-oci --offline --test integration -- --test-threads=1'
```

Expected: `two_exec_frames_in_one_read_unit_get_their_own_stdio` **PASS**；
`sandlock-core --lib` 从 `837` 涨 4（`fd_assignment_tests`）+ 实际新增的 proto 条数；
`sandlock-oci` 从 `145` 涨 1（RED 用例）+ `oci/src/init.rs` 新增头校验条数。
**以实测数为准并同步 `docs/test-baseline.md`（注释写明加了哪些用例）**，不要照抄估算。

- [x] **Step 8：确认诊断段没混进来**

```bash
cd third_party/sandlock
git diff --cached -U0 | rg "SANDBOX_FUP23|_exit\(64|_exit\(100|FUP23 parent child_id"; echo "rc=$?"
```

Expected: 无匹配（`rc=1`）。

- [x] **Step 9：提交**

```bash
git add crates/sandlock-core/src/init/proto.rs crates/sandlock-core/src/init/fdrecv.rs \
        crates/sandlock-core/src/init/mod.rs crates/sandlock-core/src/init/executor.rs \
        crates/sandlock-oci/src/init.rs crates/sandlock-oci/src/supervisor.rs \
        crates/sandlock-oci/tests/integration.rs docs/test-baseline.md
git commit -m "fix(core): let each control frame claim its own descriptors (F15)"
```

---

## Task 3: wire 不兼容面 + 升级约束文档

**Files:** Modify `crates/sandlock-core/src/init/proto.rs`（版本错误消息）、
`docs/e2b-integration.md`、`docs/CHANGELOG.md`

- [x] **Step 1：确认版本不匹配消息点名两侧** —— `FrameError::Version(found)` 的 `Display`
  必须含「expected FRAME_VERSION=2, got 1」；`run_init` 遇解码错误保持**关通道**
  （现有 `Err(_) => break`），不得退化为「跳过这一帧继续读」。

- [x] **Step 2：写升级约束（进 `docs/e2b-integration.md` 协议小节）**

> 帧头 `n_fds`（`FRAME_VERSION = 2`）与 v1 **不兼容**：`sandlock-supervise` 二进制与
> `_sandlock*.so` 必须来自同一 fork tip（同一份 `SHA256SUMS.supervise` manifest）。混装不会
> 静默错输出，而是在建箱/首次 exec 时 fail-closed（消息点名版本），因此 E2B 侧升级必须
> **同批**替换 wheel 与 supervise，不做半升级。

- [x] **Step 3：CHANGELOG 新增 F15 行为条目**（用户可见语义：合并读单元内每帧只拿自己声明的
  描述符；`MSG_CTRUNC`/`MSG_TRUNC` ⇒ 控制通道失败而非错位；`RunExec` 需**恰好** 3 个描述符，
  多了/少了都回 `exec needs 3 fds`）。

- [x] **Step 4：聚焦复验**
  Run: `cargo test -p sandlock-core --offline --lib fd_assignment` 和
  `cargo test -p sandlock-oci --offline --test integration two_exec_frames`
  Expected: 全 PASS，计数与 Task 2 记录一致。

- [x] **Step 5：提交** `docs(f15): record the frame-version incompatibility and upgrade constraint`

---

## Task 4: fork 全量门禁 + wheel 双架构重建

**Files:** 无源码改动；产物 `wheels/fork/*`、`third_party/sandlock/tmp/sdd/f15-gate-*.log`

- [x] **Step 1：环境前置** —— `losetup -D; losetup -a | wc -l`（期望 0 或仅本机常驻项）。

- [x] **Step 2：非 root 8 档 + root 3 档（必须从 /src 短路径）**

```bash
cd /Users/polus/project/ai/sandlock-e2b
chmod -R a+rwX third_party/sandlock/tmp
docker run --privileged --rm -v "$PWD/third_party/sandlock":/src -w /src \
  sandlock-dev:latest sh scripts/test-all.sh            # → tmp/sdd/f15-gate-nonroot-final.log
for m in --oci-root --supervise-root --mediation-2uid; do
  docker run --privileged --rm -v "$PWD/third_party/sandlock":/src -w /src \
    --entrypoint bash sandlock-dev:latest -c "sh scripts/test-all.sh $m"
done                                                     # → tmp/sdd/f15-gate-root-final.log
```

Expected（基线 `core_lib 837 / core_integ 534 / ffi 100 / cli 100 / supervise 42 /
supervise_cost 3 / cli_build 0 / python 454` + `oci 145 / supervise_root 4 / mediation_2uid 9`）：
只允许 `core_lib` 与 `oci` 按 Task 2 实测数增长，其余逐项相等；任何「少跑」即门禁失败。
首轮红 ⇒ 归档 `-r1.log` 再跑 `-final.log`，两档都留。

- [x] **Step 3：wheel 双架构 + verify**

```bash
./deploy/scripts/build-sandlock-wheels.sh
shasum -a 256 wheels/fork/*.whl && cat wheels/fork/SHA256SUMS.supervise
```

Expected: verify 全绿（FFI 符号 156=156 双向、RECORD 精确、`supervise` 三方指纹一致、
mode 0755、`--uid` 拒绝冒烟）。docs 提交后若需重钉：重建产物 sha256 必须与重钉前逐个相同。

- [x] **Step 4：提交（fork docs 与主仓库 wheel/指针分开）**

```bash
git -C third_party/sandlock add docs/test-baseline.md && \
git -C third_party/sandlock commit -m "docs(baseline): record the F15 suite counts"
git add wheels/fork third_party/sandlock && \
git commit -m "chore: rebuild fork wheels for F15 and bump the sandlock pointer"
```

---

## Task 5: E2B 侧接线复验（wire 变更必做）

**Files:** 无产品代码改动；证据 `tmp/f15-*.log`、`tmp/perf/f15-*.log`；文档
`docs/HANDOFF.md`、`docs/task-backlog.md`

- [x] **Step 1：重建测试镜像并核对 wheel 落盘**

```bash
docker build -f deploy/docker/Dockerfile.test-runner -t e2b-sandlock-test:latest .
docker run --rm e2b-sandlock-test:latest sh -c '
  SO=$(python3 -c "import sandlock,os;print(os.path.dirname(sandlock.__file__))");
  ls -l "$SO/bin/sandlock-supervise"; sha256sum "$SO/bin/sandlock-supervise"'
```

Expected: sha256 == `wheels/fork/SHA256SUMS.supervise` 对应架构行；mode `0755`。

- [x] **Step 2：低 fd 表探针（FUP-23 + F15 的联合回归门）**

```bash
for n in 0 1 2 8; do tmp/venv/bin/python tmp/f11_fdcount_probe.py $n > "tmp/f15-fdcount-$n.log" 2>&1; done
tmp/venv/bin/python tmp/f23_multi_probe.py 0 4 > tmp/f15-multi.log 2>&1
rg -c "FAILURES: \[\]" tmp/f15-fdcount-*.log     # 期望 4 个文件各命中一次
```

- [x] **Step 3：网关 + 命令 / boxed 契约两形态各 2 轮**

```bash
docker run --privileged --rm --network host -v "$PWD":/src -w /src \
  -e E2B_MAX_CONCURRENT_COMMANDS_PER_SANDBOX=2 -e E2B_TEST_STRICT_SKIPS=1 \
  e2b-sandlock-test:latest bash -c \
  'python -m pytest tests/contract/test_memory_quota_gateway_command.py \
                    tests/contract/test_memory_quota_boxed.py -q -p no:cacheprovider' \
  > tmp/f15-contract-run1.log 2>&1     # 同命令再跑 run2
```

Expected: `list_tools == ['echo']`、网关后命令 exit 0 / `post-gateway-ok\n`、第二 450M
命令 exit 137 / stdout `''`、50M 控制命令 `got 50\n`、record `memoryMB == 1024`；0 failed。

- [x] **Step 4：三档全量门禁**

| 档 | 形态 | 期望 |
|---|---|---|
| gate A | image-rootfs（`E2B_BASE_IMAGE=python-mcp:3.14`）+ netns + XFS + npm + strict | `982 passed / 2 skipped / 1 xfailed(T5) / 0 failed` |
| gate B | pure sandlock（`E2B_BASE_IMAGE=` 空）+ netns + strict | `982 passed / 3 skipped / 0 failed` |
| macOS | 本机全量（unit+contract+sdk python/js+security） | `916 passed / 65 skipped / 0 failed` |

两档容器都必须带 `E2B_MAX_CONCURRENT_COMMANDS_PER_SANDBOX=2`（漏传 ⇒ boxed 用例
`DID NOT RAISE` 假红）。日志 `tmp/f15-e2b-gate-{a,b}.log`、`tmp/f15-macos.log`。

- [x] **Step 5：文档收口并提交（主仓库）** —— `docs/HANDOFF.md` 顶部新增
  `## ⚡ F15（2026-09-08）…` 块（修法、终态 tip、逐项门禁数）；`docs/task-backlog.md`
  新增 #23 行（✅，引用 fork 提交 hash）。

```bash
git add docs/HANDOFF.md docs/task-backlog.md
git commit -m "docs(f15): close the frame-fd assignment fix on the E2B ledger"
```

---

## Task 6: runner 残留两条（FUP-17 增量缓存隔离 + FUP-09 证据留存脚本化）

**Files:**
- Modify: `third_party/sandlock/scripts/test-all.sh`（`mode` 归一化之后；`run()` 的日志选择处）
- Test: 门禁本身 —— runner 改动**不得改变任何套件计数**，计数漂移即失败

**Interfaces:**
- Consumes: `mode="${1:-}"` + `case` 归一化（`:123-127`）、root 守卫（`:130-141`）、
  三个 root 档各自的正向守卫、`run <label> <cmd…>`（`:93-121`：`tee "$log"` + 与
  `docs/test-baseline.md` 比对计数）
- Produces: root 三档 `CARGO_INCREMENTAL=0`；`run` 自动把同名旧日志轮换成 `tmp/<label>-rN.log`
  （FUP-09 的 `-r1`/`-final` 从此是机制而不是记忆）

- [x] **Step 1：FUP-17 剩余那半 —— root 阶段不吃共享增量缓存**

前半（无参/`--wheels` 拒 root）已在 `:130-141` 落地。后半（root 与 uid 65534 共用
`target-linux` 的增量产物）今天 `rg -n CARGO_INCREMENTAL scripts/test-all.sh` **无命中** ⇒ 未做。
在 `mode` 归一化之后、任何 `run` 之前插入：

```sh
# FUP-17: the root phases share target-linux with the uid-65534 phase. Cargo's
# incremental artifacts are owned by the uid that produced them, so a root run
# either cannot rewrite them or reads an index the other uid wrote -- a stale-cache
# false red that looks exactly like a code regression. Dropping incremental
# compilation for the root phases costs only our own crates' re-codegen (deps
# still come from the shared cache) and removes that whole class.
case "$mode" in
    --oci-root|--supervise-root|--mediation-2uid) export CARGO_INCREMENTAL=0 ;;
esac
```

- [x] **Step 2：FUP-09 剩余那半 —— 把「留住红档」变成脚本行为**

现状：`run` 直接 `tee "$log"`，重跑同名即覆盖 ⇒ 「首轮红留 `-r1.log`、终局绿留 `-final.log`」
只活在 `:33-38` 的注释里。在 `run()` 里 `$log` 确定之后、`tee` 之前插入轮换（不给出任何绕过开关：
门禁纪律要求红档可见）：

```sh
    n=1
    while [ -e "tmp/$label-r$n.log" ]; do n=$((n + 1)); done
    if [ -e "$log" ]; then
        mv "$log" "tmp/$label-r$n.log"
        printf '    previous %s log archived as tmp/%s-r%s.log\n' "$label" "$label" "$n"
    fi
```

- [x] **Step 3：验证 —— 计数零漂移 + 归档生效**

```bash
cd /Users/polus/project/ai/sandlock-e2b
losetup -D 2>/dev/null || true
docker run --privileged --rm -v "$PWD/third_party/sandlock":/src -w /src \
  sandlock-dev:latest sh scripts/test-all.sh
docker run --privileged --rm -v "$PWD/third_party/sandlock":/src -w /src \
  --entrypoint bash sandlock-dev:latest -c 'sh scripts/test-all.sh --mediation-2uid'
# 再跑一次同一 root 档，确认归档提示与 -r1.log 出现
ls third_party/sandlock/tmp/mediation_2uid*.log
```

Expected: 8 档 + root 档计数与 `docs/test-baseline.md` 逐项相等（本 Task 不该动任何计数）；
第二次跑打印 `previous mediation_2uid log archived as tmp/mediation_2uid-r1.log` 且该文件存在。

- [x] **Step 4：语法自检 + 提交**

```bash
cd third_party/sandlock && sh -n scripts/test-all.sh && \
git add scripts/test-all.sh && \
git commit -m "test(scripts): isolate the root-phase incremental cache and archive prior gate logs (FUP-17/FUP-09)"
```

---

## Task 7: fork 台账收口（docs-only）

**Files:** Modify `third_party/sandlock/docs/fork-plan-followups.md`、`docs/e2b-integration.md`

- [x] **Step 1：关闭陈旧行**（证据取 Task 0 Step 3 的审计表；**FUP-09/17 只在 Task 6 完成后关，
  并在行内写明「已落地的那半 + 本次补的那半」**）。统一格式，例：

```markdown
  **已关闭（2026-09-07，A/B cleanup wave）**：`--pid-ns` 在 flatten 后转发给运行时 builder
  （`262c0cf`；单测 `main.rs:1214 test_pid_ns_flag_reaches_runtime_policy` + `cli_test.rs`
  端到端）。台账行漏写关闭结论，本次补记（无代码改动）。
```

同样处理 FUP-07（`48968a5`，两半：no-clobber + I1 谓词 `sandbox/tests.rs:648`）、FUP-10（`d5bbdd8`）、
FUP-15（`b1e2e32`，`Cargo.toml:24-26`）；FUP-09/FUP-17 记 Task 6 的提交 hash 后再关；
FUP-02/FUP-08 记「按决策关闭的长期项，无代码动作」。

- [x] **Step 2：把帧缺陷从「候选补丁存档」改成「已修」** —— FUP-23 条目末尾与
  `docs/e2b-integration.md:443` 那行：把「候选补丁存档
  `tmp/sdd/fup23-wip-frame-fd-count.patch`，单独排期」改写为「F15 已落地（commit；
  `FRAME_VERSION` 1→2，帧头 `n_fds` + `MSG_CTRUNC`/`MSG_TRUNC` fail-closed；门禁与 wheel 见
  §5 终态行）」。取证残留（`tmp/sdd/fup23-wip-frame-fd-count.patch`、
  `tmp/fup23-candidate-frame-fd-count.patch`、`third_party/sandlock/tmp/wt-*`）先 `du -sh`
  再向用户确认后删除。

- [x] **Step 3：§E 三条（FUP-E1/E2/E3）标注 E2B 侧已关闭**，引用主仓库证据（T4 关闭于
  `883d38d`/`f67a6b9`；§8 M4 接线于 `5d38537`…`f67a6b9`；§3.8 超卖复验见 backlog FUP #3 行），
  并注明「fork 无权执行，条目保留作追溯」。

- [x] **Step 4：主仓库台账同步** —— `docs/task-backlog.md` 顶部「状态最后更新」改 2026-09-08；
  「剩余工作」第 1 条（`wheels/fork` 重建）标已完成并指向 F15 产物；#20/#22 状态与 F15 对齐；
  ⬜ 行只剩真需要动作的（#4 产品决策、#5 T5 route-B、#11 日志头纪律、#13、#14、#20、SL-1/T1 环境项）。

- [x] **Step 5：提交并核对「文档不动产物」**

```bash
git -C third_party/sandlock commit -am "docs(followups): close the stale FUP rows and record F15"
git -C third_party/sandlock diff --stat 880a1ec..HEAD -- crates Cargo.toml Cargo.lock   # 期望空
```

非空 ⇒ 回 Task 4 重跑 wheel 与门禁（说明这次「docs」提交其实动了代码）。

---

## Task 8（可选）: FUP-19/20/21 合并评估（docs-only）

**Files:** Modify `third_party/sandlock/docs/fork-c-class-design-assessment.md`

- [x] **Step 1** 追加「F12 后重估（2026-09-08）」小节：FUP-19（per-child fs/bind 强制）与
  FUP-20（credential per-child 归因）是否合并为**一个 pid-passthrough 设计**（两者同源于
  「通知只带 destination，不带 child 归因」）；FUP-21（port-aware payload）单列。
- [x] **Step 2** 明确三条的**触发条件**（跨 uid 文件可见性、per-child 凭据、端口级收窄任一被
  E2B/产品提出）与不触发时不做的理由，避免下波无据重开。
- [x] **Step 3** 提交 `docs(f19-f21): reassess the per-child design candidates after F12`。

---

## Task 9: F16 —— route-B worker 侧客户端面（T5 的真前置）

**为什么在 fork 计划里**：T5 想要的 per-uid 卷保护**语义**在 fork 已闭环（§3.1 标
「已修（构造消除 + fail-closed）」，B 档 `test_two_supervisors_distinct_uids_isolate_files`
已是内核级硬证据），**但 route-B 的 worker 侧接入只有 Rust**：`control.rs:1634
connect_and_request` 仅被 `supervise*.rs` 测试使用；FFI 的 142 个 `sandlock_*` 导出不含任何
connect/attach；Python SDK 里的 `registry` 全是 handler registry ⇒ envd（Python）今天**当不了**
route-B 的 worker。所以 main backlog #5 **不是纯部署项**。

**Files:**
- Modify: `crates/sandlock-core/src/control.rs`（把 `connect_and_request` 的「一次请求」扩成可复用
  的 client 入口；现有 `channel_request(&mut stream, &token, verb, args)` 是事实面）
- Modify: `crates/sandlock-ffi/src/lib.rs`（新增导出）、`include/sandlock.h`（cbindgen 再生）
- Modify: `python/src/sandlock/`（薄包装 + 文档串）、`python/tests/`（跨进程用例）
- Test: `crates/sandlock-supervise/tests/mediation_2uid.rs`（Rust 侧客户端复用）、Python 侧同族用例
- Modify: `docs/supervise-identity-handoff.md`（新增「语言客户端接入面」小节）、`docs/CHANGELOG.md`

**Interfaces:**
- Consumes: 服务端 `serve_registered_once`（`control.rs:1277`）、`sandlock-supervise
  --serve-path/--token/--peer-uid`（`main.rs:106-123`）、verb 面 `exec`（**带 fds**，
  `serve.rs:597`）/`wait_child`/`kill_child`/`update_network`/`shutdown`
- Produces（C ABI，命名沿用现有 `sandlock_*` 纪律）：
  `sandlock_supervise_connect(const char *path, const char *token, uint32_t **err) -> void*`、
  `sandlock_supervise_request(void *h, const char *verb, const char *args_json,
  const int *fds, size_t n_fds, uint32_t **err) -> char *`、
  `sandlock_supervise_free(void *h)`；Python：`SuperviseChannel(path=..., token=...)`
  + `.request(verb, **args, fds=[]) -> dict`

- [x] **Step 1：先锁范围（设计决策，写成 docs 再动码）**

必须是「request + fds」而不是「request 一条 JSON 就够」：`serve.rs:597 "exec" => 
self.handle_exec(&req.args, fds)` 说明 exec 的三端 stdio 就是从这条通道的 SCM_RIGHTS 进来的，
砍掉 fd 传递就等于砍掉 envd 真正需要的能力。同时**明确不做**：不在 F16 里加实例语义（
`exec`/`wait_child` 的 verb 语义由服务端定，客户端只做「发一请求、收一响应、附上要交的 fd」）。
把这段决策与不做的边界写进 `docs/supervise-identity-handoff.md` 新小节。

- [x] **Step 2：RED —— 头文件先指不到符号**

在现有 C 冒烟档（`docs/test-baseline.md` 记的「C smoke target compiles the regenerated
`sandlock.h` against the cdylib」）里加一条：

```c
/* F16: the route-B worker-side client must be reachable from C, not just Rust. */
void *h = sandlock_supervise_connect(path, token, &err);
if (!h) { fprintf(stderr, "connect failed: %u\n", err); return 1; }
char *resp = sandlock_supervise_request(h, "shutdown", "{}", NULL, 0, &err);
```

Run: `cargo test -p sandlock-ffi --offline`（或 `scripts/test-all.sh` 的对应档）
Expected: **编译失败**（`sandlock_supervise_connect` 未声明）——即 RED，留档
`tmp/sdd/f16-red-r1.log`。

- [x] **Step 3：GREEN —— Rust 入口 + FFI 导出 + Python 包装**

`control.rs` 里给客户端一个具名入口（保留现有 `connect_and_request` 不动，新函数带 fd 附加）：

```rust
/// Worker-side client for route B: connect to a registered `sandlock-supervise`
/// slot, attach the channel token, and issue one verb — optionally handing over
/// descriptors (`exec` needs exactly three). Response bytes come back as-is so
/// the caller keeps the existing JSON contract.
pub fn registered_request(
    sock_path: &Path,
    token: &str,
    verb: &str,
    args: serde_json::Value,
    fds: &[RawFd],
) -> Result<ControlResponse, String> {
    let stream = UnixStream::connect(sock_path).map_err(|e| format!("connect {sock_path:?}: {e}"))?;
    channel_request_with_fds(&stream, token, verb, args, fds)
}
```

FFI 三导出 + `include/sandlock.h` 由 cbindgen 再生（**头文件漂移是已知坑**：F3 那条就写过
「header regeneration also picks up pre-existing drift」⇒ 本次一并核对本轮新增声明是否齐）。
Python 包装只做一个类，错误路径把 `err` 码翻成既有 `exceptions.py` 的异常类型。

- [x] **Step 4：跨进程 B 档用例（把 T5 想要的证据钉在 fork 侧）**

在 `--mediation-2uid` 档加一条（root 容器，setpriv 起真实 uid）：以 uid X 起
`sandlock-supervise --serve-path <slot> --token <t> --peer-uid X`，**用新 Python 包装**连上去
`create`+`exec`，断言三件事精确成立：

- 沙箱内新建文件的宿主属主 `== X`（不是 0）；
- 该文件由 X `chmod` 自己成功（`0o600` 回读一致）；
- 同机另一 uid Y 起的第二个沙箱对它 `rm`/`chmod` 均 `EPERM`（1777+sticky 语义真实生效）。

Expected: `mediation_2uid 9 → 10`（或 Python 档 +1，按实际落点记），并在
`docs/test-baseline.md` 注释里写明「= T5 所缺的 Python 可达证据」。

- [x] **Step 5：两条部署约束进文档（会咬人，必须写）**

- **`sun_path` 108 字节上限**：注册套接字路径过长会假失败（fork 门禁自己踩过，
  `scripts/test-all.sh:12-20`）⇒ E2B 侧 registry 根路径长度要进部署检查表。
- **一 uid = 一个 supervise = 一代沙箱**：槽位复用只能靠**重启进程**
  （`docs/supervise-identity-handoff.md:185-200`）；uid 复用窗口 = 同时在世槽数 N，
  要换成 W2（换 uid 重启）需要特权 restarter。E2B 的 per-sandbox uid 池（`envd_service/uid_pool.py`）
  必须先选 W1/W2 之一，再接线。

- [x] **Step 6：全量门禁 + wheel + E2B 复验**

沿用 Task 4 / Task 5 的纪律与期望值（F16 不改 init 的 SLKF 帧协议 ⇒ 与 F15 无耦合；
`connect_and_request` 走的是 control registry 协议）。E2B 侧只需复验三档门禁无漂移，
**不摘 T5 xfail**（摘除属 envd 接线完成后的动作，见 main backlog #5）。

- [x] **Step 7：提交（分两笔，便于回退）**

```bash
git -C third_party/sandlock add crates/sandlock-core crates/sandlock-ffi include crates/sandlock-supervise
git -C third_party/sandlock commit -m "feat(ffi): expose the route-B worker-side client to C and Python (F16)"
git -C third_party/sandlock add python docs
git -C third_party/sandlock commit -m "docs(route-b): record the language client surface and the two deployment constraints"
```

---

## Task 10: 推送与上游 PR（**需用户授权，硬门**）

**Files:** 无代码改动。

- [ ] **Step 1：预推送审计（只读）**

```bash
cd third_party/sandlock
git rev-list --count origin/upstream-pr/netns-free-clean..HEAD    # 期望 129
git log --pretty=format: --name-only origin/upstream-pr/netns-free-clean..HEAD \
  | sort -u | rg '(^|/)(\.env$|.*\.key$|id_rsa|secret|credential)' || echo "no secret-looking paths"
git log --pretty=format: --name-only origin/upstream-pr/netns-free-clean..HEAD \
  | sort -u | rg '^tmp/' || echo "no tmp/ artifacts committed"
git diff --stat origin/upstream-pr/netns-free-clean..HEAD | tail -1
```

- [ ] **Step 2：向用户要三件事**（缺一即停，不要自行假设权限）：① 有写权限的 GitHub token
  （当前 token 只读、`gh` 不可用）；② 目标分支确认（`upstream-pr/netns-free-clean`）；
  ③ 主仓库远端地址 —— **现在根本没配 remote，201 个提交纯本地**，这是当前最大的丢失风险。

- [ ] **Step 3：推送 fork 分支** —— `git -C third_party/sandlock push origin upstream-pr/netns-free-clean`

- [ ] **Step 4：上游 PR + issue** —— 按 `docs/upstream-pr-netns-free.md` 的范围整理面向
  `multikernel/sandlock` 的 PR（netns 部分留 fork 分支），并为 SL-1（路径中介以 supervisor
  身份执行 `openat/unlinkat/fchmodat`）开上游 issue；工具/权限不可用时如实报告，不静默跳过。

- [ ] **Step 5：主仓库推送**（用户提供远端后）—— `git remote add origin <url> && git push -u origin main`

---

## Task 11: 发布门（受「不做远程部署」约束的剩余项）

- [ ] **Step 1** 用 F15 终态 wheel 重建 worker/控制面镜像并推 ACR
  （`deploy/scripts/build-and-push.sh`，tag 规则 `0.1.0-<n>-g<sha>-<yyyymmdd>-<seq>`）；
  ACR 现存 `0.1.0-9-g9ed0f00-20260902-013319` 已过期。
- [ ] **Step 2** 发布前修 backlog #20（E2B 侧 OCI 三处：`oci_registry.py:352` blob 不校验
  digest、`RegistryClient(timeout=30)` + `tests/conftest.py:193` buildkitd mirror 硬编码、
  `oci_registry.py:262` 匿名 `Authorization=None` 触发 `TypeError` 打断整次拉取）。
  —— 属 E2B 侧，列此仅为发布门可见。
- [ ] **Step 3** 目标机窗口：O1（XFS prjquota）→ E1.2/E8.1 部署与远程 smoke → T1 复测
  （摘掉那条带证据的 skip）→ O2/O3。

---

## Definition of Done

1. Task 1 红档与 Task 2 绿档都有日志入库引用，且红档证明的正是「第二帧拿到别人的描述符」。
2. `FRAME_VERSION = 2`；`take_frame_fds` 4 条 + proto 头校验 2 条单测入库；`MSG_CTRUNC` /
   `MSG_TRUNC` 不再被忽略。
3. fork 11 档门禁绿且计数与 `docs/test-baseline.md` 逐项一致；wheel 双架构 verify 全绿；
   `fork HEAD == manifest HEAD == 子模块指针`。
4. E2B 三档门禁无漂移（`982/2/1xfail/0`、`982/3/0`、`916/65/0`），低 fd 表探针
   N=0/1/2/8 全 `FAILURES: []`。
5. `docs/fork-plan-followups.md` 不再存在「代码已落地但台账仍开」的行；主仓库 ⬜ 行全部
   确实需要动作。
6. Task 6 两项 runner 残留落地且**零计数漂移**；`docs/fork-plan-followups.md` 里
   FUP-01/07/09/10/15/17 六行全部关闭（09/17 记 Task 6 hash）。
7. F16 合入：`sandlock_supervise_connect/request/free` 三导出进 `sandlock.h`、Python 有
   `SuperviseChannel`，且 `--mediation-2uid` 档新增用例以「Python 客户端」身份复现
   per-uid 卷保护 ⇒ main backlog #5 从此只剩 envd 接线 + 摘 xfail。
8. Task 10/11 的约束状态对用户可见：要么已获授权推送，要么明确记为「等人」。

## 风险与回退

- **wire bump 的混装窗口**：半升级 ⇒ 建箱期 fail-closed（可观测、不会错输出）。回退 =
  revert F15 代码提交 + 用 `d5cab47` 那批 sha256 重钉 wheel/manifest，并重跑 E2B 三档。
- **合并帧在生产罕见**（请求由链路写锁串行化）：RED 仍保留 —— 「按声明归属」是正确性前提，
  不该依赖竞态才复现。
- **`MAX_FDS_PER_READ = 16` 抬高控制缓冲**：over-declaration 与超长队列由 `TooManyFds` +
  `take_frame_fds` 的 `None` 分支覆盖，均有单测。
- **F16 扩大 wire 面**：新客户端复用既有 registry 协议，不改帧版本；若未来要给 registry 也加
  版本门，另开计划，**不要**夹在 F15/F16 里做。
- **已知门禁假红两类**：路径过长的 supervise 套接字超时（必须 `/src` 短路径）、gate B 漏传
  `E2B_MAX_CONCURRENT_COMMANDS_PER_SANDBOX=2` ⇒ 先按纪律排除环境因，再谈回归。
