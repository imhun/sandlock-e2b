# 共享卷去 SYS_ADMIN（fork 确定性 cwd 根治）+ 剩余任务收口 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: 用 superpowers:subagent-driven-development
> （推荐）或 superpowers:executing-plans 按任务逐条执行。步骤用 `- [ ]` 勾选跟踪；
> 每个 Task 自带验证，不通过不进入下一个 Task。fork 侧进度账本
> `third_party/sandlock/.superpowers/sdd/progress.md`，main 侧 `.superpowers/sdd/progress.md`。

**Goal:** 让共享卷（`volumeMounts`）在**不需要 `CAP_SYS_ADMIN`** 的前提下恢复完整的
跨 uid 读写语义——在 fork 侧把"沙箱虚拟 cwd"变成由请求决定的确定量，而不是由宿主路径
反查决定；随后把 `SYS_ADMIN` 从 worker 的部署 capset 里摘掉。同一份计划顺带收口
backlog 上其余未完成项（fork 安全项 / 上线运维 / 产品决策 / 清理授权）。

**Architecture:** 共享卷内容通过 sandlock 的 `fs_mount` 覆盖层（无特权的路径中介）暴露给
沙箱，不再用 `mount --bind`。缺口的根因已由探针定性（见「设计与证据」）：`/workspace` 与
`/home/user` 指向同一宿主目录时 `host_to_virtual` 的反查结果不确定（Rust `max_by_key`
取最后一个平局项 ⇒ `/home/user`），于是由 cwd 推导的相对路径绕过了 `/workspace/<rel>`
这个卷子挂载点。fork 侧修三处（chdir 记录请求的虚拟路径、为 exec 子进程播种初始 cwd、
反查确定性化）；E2B 侧把卷视图在两个别名下都注册，并删除 bind 材料化整段代码。

**Tech Stack:** Rust（sandlock-core / sandlock-supervise）、manylinux cp314 wheel 管线、
E2B（FastAPI envd + pytest 契约/单测）、Docker 门禁（`sandlock-dev:latest`、
`e2b-sandlock-test:latest`）、XFS prjquota。

## Global Constraints

- 不推送远程：fork/main 均为**本地提交**；ACR 推送与线上变更必须等用户点头（C1–C3）。
- 断言精确（禁 `toContain` / `includes` / 部分匹配）；禁止新增 skip 或用 `--ignore` 掩盖失败；
  `E2B_TEST_STRICT_SKIPS=1` 保持开启。
- 临时产物放各自仓库 `tmp/`；证据日志首行带 ENV-HEADER（commit / env / 镜像 / 时间）。
- 门禁计数按 `docs/test-baseline.md`（fork）与 HANDOFF 基线表登记增量，漂移即视为失败。
- 安全口径：沙箱侧最小 cap = `SETUID`+`SETGID`+`CHOWN`；`DAC_OVERRIDE` 仅管理面需要；
  **`SYS_ADMIN` 与 `SYS_PTRACE` 都不是 route-B/chroot 形态的前置**
  （`docs/production-deployment-requirements.md` §2.4.1）。

## 已确认的决定（2026-09-10 用户拍板，执行时按此，不再询问）

| # | 决定 | 落到哪个 Task |
|---|---|---|
| 1 | 规范别名取 **`/home/user/<rel>`**；`_view_cwd` 返回 `/home/user`，`pwd` 与现状保持一致 | A4 Step 3 |
| 2 | chdir 记录**请求的虚拟路径**（逻辑路径语义，接受 `..` 按逻辑解析） | A2 Step 1（原样执行） |
| 3 | `xfs_quota` 走 **quota-agent**（不放弃配额能力） | A6 Step 2 |
| 4 | SDK **要能看到**网关启动失败 ⇒ 落地错误上抛 | D1 |
| 5 | **线上暂不升级**：等所有问题闭环 + 本地模拟测试全绿后再上 | C1–C3 挂起，见其入口条件 |
| 7 | OCI 限流按**最快且测试正常**的口径：多源回落 + 测试用本地 registry 预置镜像为默认，digest 侧车仅在实测仍抖动时再加 | D2 |
| 6 | SL-1 走 **C-硬删**：删掉 `mediation_run_as=supervisor` 档，"中介必须是沙箱身份"成为唯一形态；不考虑推上游，安全性优先 | B3（已改写为硬删任务） |
| 8 | 不做上游 PR：B4（fork push + PR 回复）取消 | — |
| 9 | 全部计划执行完后跑**本地部署测试** | 新增 Track Z |

## 设计与证据（先读这段再动代码）

探针 `tmp/vol_fs_mount_probe.py`（无 SYS_ADMIN 容器 + 真 fork wheel + route-B 槽位）实测：

| 工作区卷条目 | 绝对路径 `cat /workspace/mnt/data/x` | 相对 `cat mnt/data/x` | 相对写 | `/proc/self/cwd/...` |
|---|---|---|---|---|
| 控制面现状（绝对符号链接） | ✅ 0 | ❌ EACCES | ❌ EACCES | ❌ EACCES |
| 真实空目录占位 | ✅ 0 | ❌ ENOENT | ❌ EACCES | ❌ ENOENT |
| **符号链接 + `/home/user` 别名挂载** | ✅ 0 | **✅ 0** | **✅ 0** | **✅ 0** |
| 宿主卷路径祖先 0700 | ❌ EACCES | ❌ | ❌ | ❌ |

根因链（代码位置）：

1. `third_party/sandlock/crates/sandlock-core/src/chroot/resolve.rs::host_to_virtual` 用宿主源
   最长前缀反查虚拟路径；`/workspace` 与 `/home/user` 的宿主源是同一个目录，长度打平后
   `max_by_key` 取**最后一个** ⇒ 返回 `/home/user`。
2. `crates/sandlock-core/src/chroot/dispatch.rs::handle_chroot_chdir`（约 2079–2091 行）记录的是
   `host_to_virtual(readlink(fd))`，于是**任何** chdir 都把虚拟 cwd 记成 `/home/user`；
   未跟踪时的 `virtual_cwd_of` 回落到 `/proc/<pid>/cwd` 后同样如此。
3. `build_virtual_path` 用该 cwd 拼相对路径 ⇒ `/home/user/mnt/data/x`，命中 `/home/user`
   挂载（宿主 workspace 目录），**`/workspace/mnt/data` 这个卷子挂载点永远不会被匹配**。
4. 解析失败在 `handle_chroot_open`（约 619–622 行）统一返回 `EACCES`，症状因此伪装成
   "权限不足"；`mount --bind` 之所以"修好"它，是因为 bind 把卷内容**物理搬到** workspace
   子路径下，让第 3 步的回落能捡到东西。

推论（决定了本计划范围）：

- 修复需要**两件独立的事**：①fork 侧让 cwd 确定且由请求决定；②宿主卷路径对租户 uid
  可穿过（表格最后一行证明它是独立的第二前提）。
- 只改权限（backlog 候选①）**不充分**；让容器运行时做 bind（候选②）**不可行**——
  per-sandbox 切片是运行期产物，仍需要在 worker 挂载命名空间里的一次 `mount(2)`。

### Track A 通用门禁命令（下面 Task 复用，先 export 一次）

`deploy/scripts/test-prod-shaped.sh` 本轮会加 `PROD_DROP_CAPS`（见 A7 Step 1）；在它落地前，
直接用下面这个函数跑"无 SYS_ADMIN 的生产形 lane"，参数与脚本 phase 1 逐项一致：

```sh
# 放进当前 shell 即可：lane <pytest 参数...>
lane() {
  docker run --rm --init --network host \
    --cap-drop ALL \
    --cap-add NET_BIND_SERVICE --cap-add NET_RAW --cap-add SYS_CHROOT --cap-add CHOWN \
    --cap-add DAC_OVERRIDE --cap-add FOWNER --cap-add FSETID --cap-add KILL \
    --cap-add SETGID --cap-add SETUID --cap-add SETPCAP --cap-add AUDIT_WRITE \
    --cap-add SETFCAP --cap-add NET_ADMIN \
    --security-opt seccomp=unconfined --security-opt apparmor=unconfined \
    -e E2B_HOST_PROJECT="$PWD" -e E2B_TEST_STRICT_SKIPS=1 \
    -e E2B_BASE_IMAGE="${E2B_BASE_IMAGE:-python-mcp:3.14}" \
    -e E2B_MAX_CONCURRENT_COMMANDS_PER_SANDBOX=2 \
    -v "$HOME/.orbstack/run/docker.sock:/var/run/docker.sock" \
    -v "$PWD:/workspace" -w /workspace \
    e2b-sandlock-test:latest "$@"
}
```

注意这里**没有** `SYS_ADMIN`、**没有** `SYS_PTRACE`——这正是本计划要证明可用的形态。

---

## Track A — 共享卷去 SYS_ADMIN（fork 确定性 cwd 根治）

### Task A0：前置核对与基线（30 分钟）

**Files:** 无代码改动；产出 `tmp/a0-baseline.log`、`tmp/a0-probe.log`。

- [x] **Step 1: 记录三处 HEAD**

```bash
cd /Users/polus/project/ai/sandlock-e2b
git rev-parse HEAD | tee tmp/a0-baseline.log
git -C third_party/sandlock rev-parse HEAD | tee -a tmp/a0-baseline.log
python - <<'PY' | tee -a tmp/a0-baseline.log
import json, pathlib
m = json.loads(pathlib.Path("wheels/fork/manifest.json").read_text())
print("wheel manifest HEAD:", m.get("head") or m)
PY
```

Expected: 三个值一致；不一致先按 HANDOFF「wheel 与 tip 一致性」流程对齐，不要开始 A1。

- [x] **Step 2: 复跑探针，确认本机仍复现**

```bash
docker run --rm --network host \
  --cap-drop ALL --cap-add NET_BIND_SERVICE --cap-add NET_RAW --cap-add SYS_CHROOT \
  --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER --cap-add FSETID --cap-add KILL \
  --cap-add SETGID --cap-add SETUID --cap-add SETPCAP --cap-add AUDIT_WRITE --cap-add SETFCAP \
  --security-opt seccomp=unconfined --security-opt apparmor=unconfined \
  -v "$PWD:/workspace" -v "$HOME/.orbstack/run/docker.sock:/var/run/docker.sock" \
  -w /workspace e2b-sandlock-test:latest python tmp/vol_fs_mount_probe.py 2>&1 | tee tmp/a0-probe.log
```

Expected: `chroot+fs_mount/symlink` 的 `rel-read=1`、`rel-write=2`；
`chroot+fs_mount/symlink+home-alias` 的 `rel-read=0,rel-write=0`。

### Task A1：fork RED —— 别名 + 子挂载下的相对路径（半天）

**Files:**
- Modify: `third_party/sandlock/crates/sandlock-core/tests/integration/test_instance_chroot.rs`
- Test: 同上（新增 2 个 `#[tokio::test]`）

**Interfaces:**
- Consumes: `Sandbox::builder().chroot(rootfs).fs_mount(virtual, host).fs_write(virtual).cwd(...)`、
  `SandboxInstance::launch_exec_only(policy)`、`instance.exec(&["rootfs-helper", "cat", ...], ExecStdio::Piped)`
  （同文件既有用例 `test_instance_exec_only_chroot_supervisor_same_uid_launch_and_exec` 用的就是这套）。
- Produces: `test_relative_open_from_second_workspace_alias_resolves_the_submount`、
  `test_getcwd_reports_the_alias_the_policy_declared`。

- [x] **Step 1: 写第一条失败用例（子挂载在 `/workspace` 下，cwd 在 `/home/user`）**

```rust
/// A host directory mounted at two virtual paths must not let the second
/// alias hide a sub-mount declared under the first one. Regression for the
/// E2B shared-volume shape: cwd-derived relative opens resolved against
/// `/home/user` (the alias `host_to_virtual` happened to pick) and missed
/// `/workspace/mnt/data` entirely -- EACCES on `cat mnt/data/x`.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn test_relative_open_from_second_workspace_alias_resolves_the_submount() {
    let base = temp_dir("alias-mount");
    let rootfs = build_test_rootfs("rootfs");
    let ws = base.join("workspace");
    let vol = base.join("vol");
    std::fs::create_dir_all(&ws).expect("create workspace host dir");
    std::fs::create_dir_all(&vol).expect("create volume host dir");
    std::fs::write(vol.join("data.txt"), "hello\n").expect("seed volume file");

    let policy = Sandbox::builder()
        .chroot(&rootfs)
        .fs_read("/")
        .fs_read("/usr")
        .fs_read("/bin")
        .fs_read("/proc")
        .fs_mount("/workspace", &ws)
        .fs_mount("/workspace/mnt/data", &vol)
        .fs_mount("/home/user", &ws)
        .fs_write("/workspace")
        .fs_write("/workspace/mnt/data")
        .fs_write("/home/user")
        .cwd("/home/user")
        .build()
        .expect("alias + submount policy builds");

    let mut inst = SandboxInstance::launch_exec_only(policy)
        .await
        .expect("alias + submount instance must launch");
    let h = inst
        .exec(&["rootfs-helper", "cat", "mnt/data/data.txt"], ExecStdio::Piped)
        .await
        .expect("exec must succeed");
    let status = inst.wait_child(h.child_id).await.expect("wait child");
    let stdout = h.stdout.expect("piped stdout");
    let mut out = Vec::new();
    std::fs::File::from(stdout).read_to_end(&mut out).expect("read stdout");
    assert_eq!(status, ExitStatus::Code(0), "relative cat must exit 0");
    assert_eq!(String::from_utf8_lossy(&out), "hello\n", "exact volume bytes");

    let w = inst
        .exec(&["rootfs-helper", "write", "mnt/data/new.txt", "bye"], ExecStdio::Piped)
        .await
        .expect("write exec must succeed");
    let wstatus = inst.wait_child(w.child_id).await.expect("wait write child");
    assert_eq!(wstatus, ExitStatus::Code(0), "relative write must exit 0");
    assert_eq!(
        std::fs::read_to_string(vol.join("new.txt")).expect("volume must hold the write"),
        "bye"
    );
    assert!(
        !ws.join("mnt/data/new.txt").exists(),
        "relative write must not land in the workspace copy"
    );

    inst.shutdown().await.expect("shutdown");
    cleanup(&rootfs);
    cleanup(&base);
}
```

- [x] **Step 2: 跑它，确认是红的**

```bash
chmod -R a+rwX third_party/sandlock/tmp
docker run --privileged --rm -v "$PWD/third_party/sandlock":/src -w /src \
  sandlock-dev:latest \
  sh -c 'cargo test -p sandlock-core --test integration \
    test_relative_open_from_second_workspace_alias_resolves_the_submount -- --nocapture'
```

Expected: FAIL——`relative cat must exit 0`（stderr `cat: mnt/data/data.txt: Permission denied`）。
输出存 `tmp/a1-red.log`。若它直接通过，说明 fork 已在别处修过——立刻停下同步，不要继续 A2。

> **控制器更正（2026-09-10，A1 实测，已批准）**
>
> 1. **门禁命令必须带 `-e CARGO_HOME=/src/tmp/cargo-home`**（镜像默认 `/opt/cargo` 不可写，
>    否则 `cargo test` 直接 `EXIT=101`）。本计划所有 `sandlock-dev:latest` 的
>    `cargo test` 命令都按这条补。`scripts/test-all.sh` 自带该设置，不受影响。
> 2. **Step 3/4 的形状达不到挂载平局**：`SandboxInstance::exec()` 传
>    `ExecParams::default()`（`cwd: None`），子进程继承的是启动 cwd，而启动 cwd 由
>    `context.rs` 的真实 `chdir` 落到 `<rootfs>/<cwd>`（chroot 根规则管辖，与挂载无关）
>    ⇒ 该形状恒绿，**不能当 RED**。它保留为"修复后语义不回退"的守护用例。
> 3. **真正的 cwd RED** 必须经过 `handle_chroot_chdir`，用 helper 的 `chdir` 子命令：
>
>    ```rust
>    // 策略：/workspace 与 /home/user 同指一个宿主 workspace，两个都 fs_write
>    let h = inst
>        .exec(&["rootfs-helper", "chdir", "/workspace"], ExecStdio::Piped)
>        .await
>        .expect("chdir exec");
>    let status = inst.wait_child(h.child_id).await.expect("wait");
>    let stdout = h.stdout.expect("piped stdout");
>    let mut out = Vec::new();
>    std::fs::File::from(stdout).read_to_end(&mut out).expect("read");
>    assert_eq!(status, ExitStatus::Code(0));
>    assert_eq!(String::from_utf8_lossy(&out), "OK /workspace\n", "exact chdir report");
>    ```
>
>    修复前实测 `OK /home/user\n`（`handle_chroot_chdir` 的反查平局取最后一个），修复后应为
>    `OK /workspace\n`。
> 4. **夹具加固（`test_instance_chroot.rs` 的 `temp_dir()`）**：除 `remove_dir_all` 外，目录名
>    还要加进程内单调序号 `-{pid}-{seq}`——同一进程里 4 条用例并发，只加 `remove_dir_all`
>    会让它们互删 `rootfs`，把 git-ignored 的 `tests/rootfs-helper` 清零进而全文件 `exit 127`。

- [x] **Step 3: 写第二条用例（cwd 身份）**

```rust
/// The sandbox's cwd identity is what the request asked for, not an artifact
/// of a host->virtual reverse lookup. `getcwd` must report the requested alias.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn test_getcwd_reports_the_alias_the_policy_declared() {
    let base = temp_dir("alias-cwd");
    let rootfs = build_test_rootfs("rootfs");
    let ws = base.join("workspace");
    std::fs::create_dir_all(&ws).expect("create workspace host dir");

    let policy = Sandbox::builder()
        .chroot(&rootfs)
        .fs_read("/")
        .fs_read("/usr")
        .fs_read("/bin")
        .fs_mount("/workspace", &ws)
        .fs_mount("/home/user", &ws)
        .fs_write("/workspace")
        .fs_write("/home/user")
        .cwd("/home/user")
        .build()
        .expect("alias policy builds");

    let mut inst = SandboxInstance::launch_exec_only(policy).await.expect("launch");
    let h = inst
        .exec(&["rootfs-helper", "pwd"], ExecStdio::Piped)
        .await
        .expect("pwd exec");
    let status = inst.wait_child(h.child_id).await.expect("wait");
    let stdout = h.stdout.expect("piped stdout");
    let mut out = Vec::new();
    std::fs::File::from(stdout).read_to_end(&mut out).expect("read");
    assert_eq!(status, ExitStatus::Code(0));
    assert_eq!(String::from_utf8_lossy(&out), "/home/user\n", "exact cwd string");

    inst.shutdown().await.expect("shutdown");
    cleanup(&rootfs);
    cleanup(&base);
}
```

- [x] **Step 4: 跑它，确认是红的**

Run: 同 Step 2 的容器命令，替换测试名（日志存 `tmp/a1-red-cwd.log`）。
Expected: FAIL，实际输出 `/workspace\n`——两条红指向**同一个**反查不确定性问题，修完必须同时转绿。

- [x] **Step 5: 提交 RED**

```bash
git -C third_party/sandlock add crates/sandlock-core/tests/integration/test_instance_chroot.rs
git -C third_party/sandlock commit -m "test(chroot): pin alias + sub-mount relative resolution and cwd identity (RED)"
```

### Task A2：fork 修复（1 天）

**Files:**
- Modify: `third_party/sandlock/crates/sandlock-core/src/chroot/dispatch.rs`（`handle_chroot_chdir` ~2079–2091）
- Modify: `third_party/sandlock/crates/sandlock-core/src/chroot/resolve.rs`（`host_to_virtual` ~31–55 + `mod tests`）
- Modify: `third_party/sandlock/crates/sandlock-core/src/instance.rs`（`exec_with_fds_inner` 拿到 `pid` 之后 ~1077；以及 `RunMain`/popen 的同类 announce 点）
- Modify: `third_party/sandlock/crates/sandlock-core/src/seccomp/state.rs`（`set_virtual_cwd` 文档：说明播种来源）

**Interfaces:**
- Consumes: `ProcessIndex::set_virtual_cwd(pid: i32, cwd: PathBuf)`、
  `SandboxInstance.supervisor_processes: Option<Arc<ProcessIndex>>`（`instance.rs:313`）、
  `ExecParams.cwd: Option<String>`。
- Produces: `handle_chroot_chdir` 记录**请求的**虚拟路径；exec announce 后播种初始 cwd；
  `host_to_virtual` 平局按**声明顺序取第一个**（含文档注释）。

- [x] **Step 1: chdir 记录请求的虚拟路径**

`dispatch.rs::handle_chroot_chdir` 末尾替换为：

```rust
    // Record the path the caller asked for, not a host->virtual reverse
    // lookup: when one host directory is mounted at several virtual paths
    // (E2B's /workspace and /home/user are the same directory) the reverse
    // lookup is ambiguous, and recording the wrong alias makes every later
    // relative open miss sub-mounts declared under the requested one.
    // `resolved` stays in use as a *liveness* proof only -- it shows the
    // kernel would have landed on a real directory inside the root.
    let _ = resolved;
    set_virtual_cwd(notif, ctx, confined);
    NotifAction::ReturnValue(0)
```

- [x] **Step 2: 为新 exec 子进程播种初始 cwd**

`instance.rs::exec_with_fds_inner`，紧跟 `let pid = self.translate_announced_pid(pid)?;`：

```rust
        // Seed the tracked cwd from the request. The child already chdir'd
        // to it, but that chdir is serviced by *recording* (see
        // handle_chroot_chdir), and a path the child never re-derives must
        // not depend on a host->virtual reverse lookup.
        if let (Some(procs), Some(cwd)) =
            (self.supervisor_processes.as_ref(), params.cwd.as_ref())
        {
            procs.set_virtual_cwd(pid, cwd.clone().into());
        }
```

`RunMain`/popen 的 announce 点做同样一件事（用其对应的 `cwd` 字符串）。

- [x] **Step 3: 反查确定性化**

`resolve.rs::host_to_virtual` 的迭代器链改为（其余逻辑不变）：

```rust
    std::iter::once((Path::new("/"), chroot_root))
        .chain(mounts.iter().map(|(v, h)| (v.as_path(), h.as_path())))
        .filter(|(_, source)| host_path.starts_with(source))
        .enumerate()
        // Longest host source wins; ties fall back to *declaration order*
        // (first mount in the policy wins). The old `max_by_key` picked the
        // last one, so a caller that declared /workspace before /home/user
        // still got /home/user back for the shared workspace directory.
        .max_by_key(|(idx, (_, source))| (source.as_os_str().len(), std::cmp::Reverse(*idx)))
        .map(|(_, (virtual_base, source))| {
            let rest = host_path.strip_prefix(source).expect("prefix matched");
            if rest.as_os_str().is_empty() {
                virtual_base.to_path_buf()
            } else {
                virtual_base.join(rest)
            }
        })
```

- [x] **Step 4: 纯函数单测钉住平局规则**

`resolve.rs` 的 `mod tests` 加：

```rust
    #[test]
    fn host_to_virtual_tie_breaks_on_declaration_order() {
        let host = PathBuf::from("/srv/ws");
        let mounts = vec![
            (PathBuf::from("/workspace"), host.clone()),
            (PathBuf::from("/home/user"), host.clone()),
        ];
        assert_eq!(
            host_to_virtual(Path::new("/rootfs"), &mounts, &host.join("a.txt")),
            Some(PathBuf::from("/workspace/a.txt")),
            "first-declared alias must win the tie"
        );
    }
```

- [x] **Step 5: 跑 RED 两条用例 + 单测，确认转绿**

```bash
docker run --privileged --rm -v "$PWD/third_party/sandlock":/src -w /src sandlock-dev:latest \
  -e CARGO_HOME=/src/tmp/cargo-home \
  sh -c 'cargo test -p sandlock-core --test integration test_relative_open_from_second_workspace_alias_resolves_the_submount -- --nocapture && \
         cargo test -p sandlock-core --test integration test_getcwd_reports_the_alias_the_policy_declared -- --nocapture && \
         cargo test -p sandlock-core --test integration test_getcwd_reports_the_requested_alias_not_the_best_match -- --nocapture && \
         cargo test -p sandlock-core --lib chroot::resolve'
```

Expected: 两条 PASS + `host_to_virtual_tie_breaks_on_declaration_order` PASS。

- [x] **Step 6: chroot/cwd 回归（防语义漂移）**

```bash
docker run --privileged --rm -v "$PWD/third_party/sandlock":/src -w /src sandlock-dev:latest \
  -e CARGO_HOME=/src/tmp/cargo-home \
  sh -c 'cargo test -p sandlock-core --test integration chroot -- --nocapture'
```

Expected: 全绿。若既有用例断言"`cd` 走符号链接后 `..` 用物理父目录"，按确认点 #2 取舍，
并在 `CHANGELOG.md` 明确记录这条语义变更。

- [x] **Step 7: 提交**

```bash
git -C third_party/sandlock add crates/sandlock-core/src
git -C third_party/sandlock commit -m "fix(chroot): make the virtual cwd request-derived and the host->virtual tie-break deterministic"
```

### Task A3：fork 全门禁 + wheel 重建（半天）

**Files:**
- Modify: `third_party/sandlock/CHANGELOG.md`、`third_party/sandlock/docs/test-baseline.md`
- Produce: `wheels/fork/*.whl` + manifest（脚本生成）

- [x] **Step 1: 非 root 全档**

```bash
cd third_party/sandlock && chmod -R a+rwX tmp
docker run --privileged --rm -v "$PWD":/src -w /src sandlock-dev:latest sh scripts/test-all.sh 2>&1 | tee tmp/a3-gate-nonroot.log
```

Expected: 各档与 baseline 一致，仅 core_lib / core_integ 因新用例递增。

- [x] **Step 2: 三个 root 档**

```bash
docker run --privileged --rm -v "$PWD":/src -w /src --entrypoint bash sandlock-dev:latest -c 'sh scripts/test-all.sh --oci-root' 2>&1 | tee tmp/a3-gate-oci.log
docker run --privileged --rm -v "$PWD":/src -w /src --entrypoint bash sandlock-dev:latest -c 'sh scripts/test-all.sh --supervise-root' 2>&1 | tee tmp/a3-gate-supervise-root.log
docker run --privileged --rm -v "$PWD":/src -w /src --entrypoint bash sandlock-dev:latest -c 'sh scripts/test-all.sh --mediation-2uid' 2>&1 | tee tmp/a3-gate-mediation.log
```

Expected: 三档全绿（本机容器需 `--init`，见 HANDOFF「pid-1 不回收孤儿」注记）。

- [x] **Step 3: wheel 双架构重建 + verify + 同步**

```bash
cd third_party/sandlock && sh python/build-wheels.sh 2>&1 | tee tmp/a3-wheel-build.log
sh python/verify-wheel.sh 2>&1 | tee tmp/a3-wheel-verify.log
cp wheels/*.whl /Users/polus/project/ai/sandlock-e2b/wheels/fork/
```

Expected: verify 全绿（FFI 符号双向相等、RECORD 精确、supervise 三方指纹、mode 755、
`--uid` 拒绝冒烟）；manifest HEAD == fork HEAD。

### Task A4：E2B 接线（别名完备 + 删除 bind）（1 天）

**Files:**
- Modify: `envd_service/runtime/context.py`（删 `_bind_mount` / `_unmount` /
  `_materialize_chroot_volume_mounts` / `_volume_bind_mounts` 及其 shutdown 段；
  `fs_mounts` 改双别名）
- Modify: `envd_service/executors/sandlock.py`（`_view_cwd` 返回 `/home/user`——决定 ①）
- Delete: `tests/unit/test_runtime_context_volumes.py`
- Modify: `tests/unit/test_executor_policy.py`、`tests/unit/test_policy_mapping.py`
- Create: `tests/contract/test_shared_volume_relative_cwd.py`

**Interfaces:**
- Consumes: `RuntimeSandbox.volume_mounts = [{"path": rel, "hostPath": host}]`；
  `SandboxExecutor(fs_mounts: dict[str, str])`。
- Produces: 每个卷视图同时出现在两个别名下——`fs_mounts` 键集合 =
  `{f"/workspace/{rel}", f"/home/user/{rel}"}`（对每个 `rel`）。

- [x] **Step 0: 重建测试镜像（前置，否则跑的还是旧 fork）**

`e2b-sandlock-test:latest` 在**构建期**把 `wheels/fork/*.whl` pip 装进镜像
（`deploy/docker/Dockerfile.test-runner`），A3 换了 wheel 之后镜像必须重建，否则 E2B
门禁跑的是 A2 之前的代码。

```bash
docker build -f deploy/docker/Dockerfile.test-runner -t e2b-sandlock-test:latest . 2>&1 | tee tmp/a4-image-build.log
```

重建后**必须核对镜像内的 `.so` 与 wheel 内的 `.so` 同源**（sha256 相等），并把两个值写进证据：

```bash
python3 - <<'PY'
import hashlib, zipfile, pathlib
whl = next(pathlib.Path("wheels/fork").glob("*x86_64*.whl"))
with zipfile.ZipFile(whl) as z:
    n = next(n for n in z.namelist() if n.endswith(".so"))
    print("wheel", whl.name, hashlib.sha256(z.read(n)).hexdigest())
PY
docker run --rm --entrypoint sh e2b-sandlock-test:latest -c \
  'sha256sum /usr/local/lib/python3.14/site-packages/sandlock/libsandlock_ffi*.so'
```

Expected: 两个 sha256 相等（2026-09-10 现状：镜像内 `0989bb55…` ≠ 新 wheel `efdd3264…`，
即**确实需要重建**）。

- [x] **Step 1: 先写契约测试（红）**

`tests/contract/test_shared_volume_relative_cwd.py`：

```python
"""Shared volume views must resolve from BOTH workspace aliases.

Regression for backlog #25: a cwd-derived relative open (`cat mnt/data/x`)
bypassed the /workspace/<rel> sub-mount, so chroot sandboxes saw EACCES (or
ENOENT) for every relative volume path once the bind workaround was removed.
"""
from __future__ import annotations

import uuid

import httpx
import pytest

from tests.contract.test_uid_permissions import (
    _envd_settings,
    _result,
    _run_cmd,
)


@pytest.mark.asyncio
async def test_volume_visible_from_both_workspace_aliases(make_apps, workspace):
    control, envd = make_apps(envd_settings=_envd_settings(workspace))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://test"
    ) as client:
        vol = await client.post(
            "/volumes",
            headers={"X-API-Key": "local-key"},
            json={"name": f"alias-{uuid.uuid4().hex[:8]}"},
        )
        assert vol.status_code == 201
        vid = vol.json()["volumeID"]
        sbx = await client.post(
            "/sandboxes",
            headers={"X-API-Key": "local-key"},
            json={
                "templateID": "base",
                "timeout": 300,
                "volumeMounts": [{"name": vid, "path": "mnt/data"}],
            },
        )
        assert sbx.status_code == 201
        payload = sbx.json()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=envd), base_url="http://test"
    ) as client:
        code, stdout, stderr = _result(
            await _run_cmd(
                client,
                payload,
                "echo hello > mnt/data/a.txt && cat /workspace/mnt/data/a.txt "
                "&& cd /home/user && cat mnt/data/a.txt && cd /workspace "
                "&& cat ./mnt/data/a.txt",
            )
        )
        assert code == 0
        assert stdout == b"hello\nhello\nhello\n"
        assert stderr == b""
```

- [x] **Step 2: 跑它，确认红**

```bash
lane pytest tests/contract/test_shared_volume_relative_cwd.py -q --tb=short 2>&1 | tee tmp/a4-red.log
```

Expected: FAIL（`cat mnt/data/a.txt` Permission denied）。

- [x] **Step 3: 双别名注册 + 删除 bind 段落**

`envd_service/runtime/context.py` 的 `create_executor(...)` 参数改为：

```python
            fs_mounts={
                # Every volume view must be registered under BOTH workspace
                # aliases (they are the same host directory). A cwd-derived
                # relative open resolves against whichever alias the sandbox
                # sits in, and only that alias's sub-mount can serve it --
                # evidence: tmp/vol_fs_mount_probe.py.
                **{
                    alias: m["hostPath"]
                    for m in record.volume_mounts
                    for alias in (
                        f"/workspace/{m['path']}",
                        f"/home/user/{m['path']}",
                    )
                },
            },
```

并删除同文件里的 `_bind_mount`、`_unmount`、`_materialize_chroot_volume_mounts`、
`self._volume_bind_mounts` 初始化与 `shutdown()` 中的 unmount 循环（以及
`__init__` 里 `if record.volume_mounts: self._materialize_chroot_volume_mounts()`）。

- [x] **Step 3b: `_view_cwd` 改为 `/home/user`（决定 ①）**

`envd_service/executors/sandlock.py::_view_cwd`：chroot 模式下把宿主 workspace cwd（或空
默认值）映射为 **`/home/user`**，并把同文件两处 `mount_map` 的字面量顺序改为
`"/home/user"` 在前、`"/workspace"` 在后（与 A2 Step 3 的"声明顺序决定平局"一致，
让 `getcwd`/`pwd` 稳定报告 `/home/user`）：

```python
            mount_map = {
                "/home/user": self._workspace_dir,
                "/workspace": self._workspace_dir,
            }
```

同步更新 `tests/unit/test_executor_policy.py` 里对 `sb.cwd` / `sb.fs_mount` 键的断言。

- [x] **Step 4: 跑契约 + 单测，确认绿**

```bash
lane pytest tests/contract/test_shared_volume_relative_cwd.py \
  tests/contract/test_uid_permissions.py tests/contract/test_volumes.py \
  tests/unit/test_executor_policy.py tests/unit/test_policy_mapping.py -q --tb=short
```

Expected: 全绿（bind 相关单测随文件删除；`--ignore` 为空）。

- [x] **Step 5: 迁移/多节点契约回归**

```bash
lane pytest tests/contract/test_migration.py tests/sdk/python/test_shared_volumes.py -q --tb=short
```

Expected: 全绿——这正是 `tmp/nosa-full.log` 剩下的 4 条红。

- [x] **Step 6: 提交**

```bash
git add envd_service tests
git commit -m "fix(volumes): expose volume views under both workspace aliases and drop the bind workaround"
```

### Task A5：宿主卷路径可穿越性（半天）

**Files:**
- Modify: `envd_service/volumes.py`（`_ensure_shared_volume_root`）
- Modify: `docs/production-deployment-requirements.md`（新增 §2.4.2）
- Create: `tests/unit/test_shared_volume_traversal.py`

**Interfaces:**
- Consumes: `provision_sandbox_volume_mount(..., host_uid: int | None)`。
- Produces: `_ensure_traversable(path: Path) -> None`；卷根与中间目录对 other 可穿过
  （`0711`/`0755`），卷切片保持 `0700` 且属主为该沙箱 host uid；无 per-sandbox 配额时
  卷根保持 `1777`。

- [x] **Step 1: 写失败单测**

构造 `<tmp>/root/_volumes/vol_a`，把 `root` 与 `_volumes` 造成 `0700`；调用
`provision_sandbox_volume_mount(..., host_uid=21700)` 后断言 `root`、`_volumes`、
`vol_a` 的 **mode 位**满足 `mode & 0o011 == 0o011`（用 mode 断言而非 `os.access`，
避免测试进程的 root 特权让断言空过）。

- [x] **Step 2: 实现**

```python
def _ensure_traversable(path: Path) -> None:
    """Give tenant uids a way *through* every ancestor of a volume view.

    The mediator opens the volume host path as the sandbox's own uid (route-B
    slot / RunAs), so DAC needs o+x on each ancestor. Traverse-only (0111)
    where the tenant must not list, otherwise keep what is there.
    """
    for candidate in [path, *path.resolve().parents]:
        if candidate == Path("/"):
            break
        try:
            mode = stat.S_IMODE(candidate.stat().st_mode)
        except OSError:
            continue
        wanted = mode | 0o011
        if wanted != mode:
            try:
                os.chmod(candidate, wanted)
            except OSError as exc:
                logger.warning(
                    "cannot make %s traversable for tenant uids: %s", candidate, exc
                )
```

在 `_ensure_shared_volume_root` 内对 `volume_root` 调用它（覆盖 `volume_root.parent` 链）。

- [x] **Step 3: 启动自检 + 文档**

worker 启动时用池内第一个 uid 探测 `settings.shared_volume_root` 的可穿透性，失败打一条
`WARNING`（点名目录、实际 mode、修法）；`docs/production-deployment-requirements.md`
新增 §2.4.2：**共享卷根及其祖先必须对租户 uid 可穿过（0711/0755）**。

> **控制器更正（2026-09-11，A5 评审）**：本步骤原写"否则只有绝对路径可用、相对路径会
> EACCES"——**实测不成立**。`tmp/vol_fs_mount_probe.py` 的 `symlink-tight-ancestor` 场景
> （祖先 `0700`）里，绝对路径 `cat /workspace/mnt/data/data.txt` **同样** `EACCES`
> （`tmp/a0-probe.log`）。原因是中介以沙箱自己的 uid 打开同一宿主路径，绝对/相对都要过
> 同一 DAC 判定。文档应写成"祖先不可穿过 ⇒ 卷视图整体不可用"，并注明证据来自 A0/A3 探针
> （A5 沿用，未重跑宿主探针；现场复现归 Track Z）。

- [x] **Step 4: 验证**

```bash
lane pytest tests/unit/test_shared_volume_traversal.py -q --tb=short
lane pytest tests/contract/test_uid_permissions.py -q --tb=short
```

Expected: 全绿；把祖先改回 0700 的那条变体必须红（证明断言有效）。

### Task A6：SYS_ADMIN 另外两处用途迁出 worker（1 天，可与 A5 并行）

**Files:**
- Modify: `deploy/stack/docker-compose.prod.yml`、`deploy/k8s/worker.yaml`
- Modify: `envd_service/xfs_quota.py`（`via_agent` 形态不再 exec `xfs_quota`）
- Modify: `docs/production-deployment-requirements.md` §2.4.1 表格

- [x] **Step 1: namespaced sysctl 改由容器 spec 提供**

把 `net.ipv4.ip_unprivileged_port_start` 的运行时写入改成部署清单声明
（Docker `--sysctl` / k8s `securityContext.sysctls`），验证 MCP 端口（50005+）与既有
端口映射行为不变。

- [x] **Step 2: xfs_quota 全量走 quota-agent（决定 ③）**

确保三条路径都经 agent 而不是 worker 侧 exec `xfs_quota`：`E2B_QUOTA_AGENT_URL` 配置项
（存在时 `via_agent=True`）、`envd_service/xfs_quota.py` 的 `provision_project` /
`release_project` / 扫描入口、以及 `deploy/stack` 的 agent 服务定义。
`docs/production-deployment-requirements.md` §2.4.1 的 `SYS_ADMIN` 行删掉
"直接执行 `xfs_quota -x`"这一条，并注明"配额能力由 quota-agent 提供，
**不再需要 worker 持 `SYS_ADMIN`**"。

降级路径保留但不再依赖 SYS_ADMIN：agent 不可达时仍按既有 `ProjectQuotaError` 降级
（挂卷成功、无 per-sandbox 限额）并打 WARNING。

- [x] **Step 3: 验证**

```bash
lane pytest tests/contract/test_volume_quota.py tests/unit/test_volume_quota.py -q --tb=short
```

Expected: agent 形态全绿；降级形态需 `E2B_TEST_STRICT_SKIPS=1` 显式暴露，不漏跑。

### Task A7：无 SYS_ADMIN 门禁固化 + backlog #25 收口（半天）

**Files:**
- Modify: `deploy/scripts/test-prod-shaped.sh`（capset 可配置）
- Modify: `docs/task-backlog.md`（#25 收口）、`docs/HANDOFF.md`（新增 ⚡ 段）

- [x] **Step 1: 让 capset 可配置**

在 `CAPS` 拼装之后加入 `PROD_DROP_CAPS`（逗号分隔）支持。

> **控制器更正（2026-09-11，A7 实测）**：**不能**用"事后追加 `--cap-drop`"来实现——本机引擎
> `--cap-add` 压过 `--cap-drop`（与两者顺序无关，A7 两种顺序都实测过），于是会产出
> "自称无 SYS_ADMIN、实际仍带着它"的**假证据**。正确做法是**在 `--cap-add` 循环里按
> `PROD_DROP_CAPS` 跳过要摘的 cap**，`--cap-drop` 只作兜底。

> **控制器更正（2026-09-11，A7 实测）**：上面这段**单独用不够** —— 本机 Docker 引擎
> （29.4.0）里 `--cap-add` 压过 `--cap-drop`，与参数顺序无关：`--cap-drop ALL
> --cap-add SYS_ADMIN --cap-drop SYS_ADMIN` 的 `CapEff` 仍是 `0xa02c35fb`（含
> SYS_ADMIN 位 0x200000），而没有 `--cap-add` 的 `--cap-drop SYS_ADMIN` 才是
> `0xa00c35fb`。所以实际实现是**先把被掉的 cap 从 `--cap-add` 循环里摘掉**，再保留这段
> `--cap-drop` 作为兜底（`deploy/scripts/test-prod-shaped.sh`）。

- [x] **Step 1b: 收窄 `XFS_DESELECTS`（A6 反馈）**

现有 deselect 表把 4 个**不需要 XFS** 的 unit 文件也排除了（A5/A6 的新用例因此进不了默认门禁）。
核对每个被 deselect 的文件是否真的硬依赖 XFS prjquota：只保留真正需要的，其余从列表移除；
移出的文件必须在**无 SYS_ADMIN 且无 XFS** 的 lane 下跑绿（或暴露出的红是真缺陷）。

- [x] **Step 1c: 更正 strict-skips 口径（A5 实测）**

本计划与部分文档曾写"`E2B_TEST_STRICT_SKIPS=1` 会把能力型 skip 判失败"。实测口径更窄：
它只升级 `tests/conftest.py:81-88` 的 **6 个 runner 能力标记**，普通 `pytest.mark.skipif`
在 strict 下仍是 skip。收口时按实测口径更正相关文档，避免误以为新增 `skipif` 会被拦住。

- [x] **Step 2: 跑无 SYS_ADMIN 全量**

```bash
PROD_DROP_CAPS=SYS_ADMIN UNPRIVILEGED_PHASE=0 ./deploy/scripts/test-prod-shaped.sh 2>&1 | tee tmp/a7-nosa.log
```

Expected: **0 failed / 0 error**（本计划前基线 `tmp/nosa-full.log` = 4 failed）。

实测（2026-09-11）：`1075 passed, 3 skipped, 0 failed`（301.19s，`tmp/a7-nosa.log`，
首行 ENV-HEADER；cap 探针 `CapEff 0xa02c35fb → 0xa00c35fb`）。

- [x] **Step 3: 常规三档门禁无漂移**

```bash
bash tmp/run-f31.sh   # gate A / gate B / 生产形 phase1/phase2（macOS 另跑）
```

Expected: 与上一基线逐项一致（±本次新增用例数）。

实测（2026-09-11，A7 本体）：生产形默认 lane（cap 不削）phase 1 `1075 passed, 3 skipped,
0 failed`、phase 2 `48 passed, 1 skipped, 0 failed`（`tmp/a7-default-lane.log`）。

实测（2026-09-11，**fix round 1 补跑全三档**；运行器 `tmp/a7-fix1-run.sh`）：

| 相 | 本次 | 基线（`569a70a`，早于 A4） | 日志 |
|---|---|---|---|
| gate A（chroot） | `1104 / 4 skip / 0 failed`（`EXIT=0`） | `1069 / 4 / 0` | `tmp/fix1-gate-a.log` |
| gate B（pure） | ⚠️ `1102 / 5 skip / **1 failed**`（`EXIT=1`） | `1068 / 5 / 0` | `tmp/fix1-gate-b.log` |
| macOS | ⚠️ `1023 / 80 skip / **1 failed**`（`EXIT=1`） | `989 / 84 / 0` | `tmp/fix1-macos.log` |

gate A 的 `+35` 全是新增用例（`git diff --numstat 569a70a HEAD -- tests/`：38 个新 `def test_`
− 3 个删除），既有断言只改了 `/workspace` → `/home/user` 与双别名 `fs_mounts`。两条红是**同一条**
A4 契约用例 `test_shared_volume_relative_cwd.py::test_volume_visible_from_both_workspace_aliases`
（pure 形态没有 `/home/user`、macOS 没有 Landlock ⇒ 这两相永远不可能通过），**不是 A4/A5 的
产品回归**（pure 形态自己的工作区相对路径契约仍成立，探针 `tmp/fix1-alias-probe.log`）。

**fix round 2（控制器裁定方案 ①：形状限定）**：该用例按 chroot 形态门控 —— 在
`tests/contract/test_shared_volume_relative_cwd.py` 加 `_IMAGE_ROOTFS_ONLY =
pytest.mark.skipif(not os.environ.get("E2B_BASE_IMAGE"), …)` 并装饰它，逐字对标同目录
`tests/contract/test_pure_shape_workspace_ownership.py:75-80` 的 `_NO_BASE_IMAGE`（方向相反）；
断言一字未动、无 `--ignore`。重跑：

| 相 | fix round 2 | fix round 1 | 基线 `569a70a` |
|---|---|---|---|
| gate A（chroot） | `1104 / 4 skip / 0 failed`（`EXIT=0`，该用例真跑） | `1104 / 4 / 0` | `1069 / 4 / 0` |
| gate B（pure） | `1102 / **6** skip / 0 failed`（`EXIT=0`） | `1102 / 5 / 1 failed` | `1068 / 5 / 0` |
| macOS | `1023 / **81** skip / 0 failed`（`EXIT=0`） | `1023 / 80 / 1 failed` | `989 / 84 / 0` |

skip 增量逐条核对：gate B `+1`、macOS `+1`，都只有这一条（`…:44 image-rootfs contract
requires a non-empty E2B_BASE_IMAGE (chroot shape)…`）。形状无关的那半仍由
`tests/unit/test_policy_mapping.py::test_volume_views_map_under_both_workspace_aliases`（精确
键集合断言）与同文件内的 `test_runtime_context_registers_both_volume_aliases` 守着，macOS 上
`2 passed`。日志 `tmp/fix2-{gate-a,gate-b,macos}.log`；逐条账见 `docs/HANDOFF.md` §4b 与
`.superpowers/sdd/task-A7-report.md` §F2。

- [x] **Step 4: 台账收口**

`docs/task-backlog.md`：#25 标 ✅（写清"无 SYS_ADMIN 可用 + 两处改动 + 证据日志名"），
并从 §2.4.1 的 `SYS_ADMIN` 行删掉"共享卷 bind"这一用途；`docs/HANDOFF.md` 新增
`## ⚡ 共享卷去 SYS_ADMIN` 段（探针、RED/GREEN、门禁数字、wheel 指纹）。

---

## Track B — fork 侧遗留代码项

### Task B1：SL-12 —— create/launch 失败必须带原因（1 天）

**Files:** `third_party/sandlock/crates/sandlock-ffi/src/lib.rs`、
`third_party/sandlock/python/src/sandlock/sandbox.py`、
`third_party/sandlock/crates/sandlock-core/src/sandbox.rs`（错误文本出口）

- [ ] **Step 1: RED**：新增用例断言 fail-closed 拒绝（如 `mediation_run_as=caller` + 非 0
  host uid）时，Python 面异常文本**逐字包含** Rust 侧拒绝原因（当前只有
  `RuntimeError("sandlock_instance_launch failed")`）。
- [ ] **Step 2: 实现**：两个入口按 supervise 侧既有 `err_msg` out 参把消息带出
  （或提供 `sandlock_last_error()`），SDK 侧译码。
- [ ] **Step 3: 验证**：`cargo test -p sandlock-ffi` + fork 非 root 全档 + wheel verify。
- [ ] **Step 4: CHANGELOG 记一条并提交。**

### Task B2：SL-11 —— handed-over 控制 fd 的 FD_CLOEXEC 护栏（2 小时）

**Files:** `third_party/sandlock/crates/sandlock-supervise/src/serve.rs`、
`third_party/sandlock/crates/sandlock-supervise/tests/supervise.rs`

- [ ] **Step 1: `serve_control_fd` 启动实例前无条件 `fcntl(F_SETFD, FD_CLOEXEC)`**（已有则补注释）。
- [ ] **Step 2: 用例**：比对槽位 `/proc/<pid>/fd` 与本端 socket inode，断言被 confine 的
  子进程看不到主管控制端（护栏，非 bug 复现；实测当前无泄漏）。
- [ ] **Step 3: `sh scripts/test-all.sh --supervise-root` 全绿；提交。**

### Task B3：SL-1 硬删 —— 取消 `mediation_run_as` 降级档（2–3 天）

**目标（决定 ⑥，用户拍板 2026-09-10）**：删掉 `MediationRunAs::Supervisor` 这一档，
让"**需要路径中介时，中介身份必须就是沙箱身份**"成为唯一形态；不再保留任何降级逃生门，
安全优先。不做墓碑档。

**Files（fork 侧）:**
- Modify: `crates/sandlock-core/src/sandbox.rs`（删 enum 变体与文档；拒绝分支去 `match`）
- Modify: `crates/sandlock-core/src/sandbox/builder.rs`（删 builder 方法）
- Modify: `crates/sandlock-core/src/sandbox/tests.rs`（删/改档位断言）
- Modify: `crates/sandlock-core/src/profile.rs`（删 TOML 键的解析/回写与 3 条用例）
- Modify: `crates/sandlock-core/src/instance.rs`（`mediation_run_as` 的消费点）
- Modify: `crates/sandlock-ffi/src/lib.rs`（删导出函数）+ `include/sandlock.h`（cbindgen 头）
- Delete: `crates/sandlock-ffi/tests/mediation_run_as.rs`（改为"该符号不存在"的断言或整删）
- Modify: `crates/sandlock-cli/src/main.rs`（删 `--mediation-run-as`）+ `cli_test.rs` / `profile_integration.rs`
- Modify: `crates/sandlock-supervise/src/policy.rs`（wire 字段表去掉该字段）
- Modify: `crates/sandlock-supervise/tests/mediation_2uid.rs`（C 档验收 **反转**：从"断言降级发生"改为"断言必须被拒"）
- Modify: `crates/sandlock-core/tests/integration/test_mediation_identity.rs`、`test_instance_chroot.rs`
- Modify: `python/src/sandlock/_sdk.py`、`python/src/sandlock/sandbox.py`、`python/tests/test_sandbox_config.py`
- Modify: `docs/e2b-integration.md` §3.1/§2(P1/P2)、`docs/CHANGELOG.md`、`docs/test-baseline.md`、
  `docs/fork-plan-followups.md`、`docs/supervise-identity-handoff.md`、`docs/upstream-pr-netns-free.md`

**Files（E2B 侧）:**
- Modify: `envd_service/route_b.py`（`supervise_policy_document` 的 drop-guard 保留或简化为"该字段已不存在"）
- Modify: `envd_service/executors/sandlock.py:708` 附近注释
- Modify: `tests/unit/test_route_b_wiring.py`、`tests/unit/test_sandlock_executor_route_b.py`
- Modify: `docs/production-deployment-requirements.md` §2.4（"删档的后果"段）、§2.4.1

**Interfaces:**
- Consumes: `mediation_remap_is_refused(mediator_euid, host_uid, mediation_active, privileged_remap_caps)`（保留不动）
- Produces: `mediation_run_as` 字段在 builder/Policy/profile/FFI/CLI/Python/supervise wire 全线消失；
  拒绝路径只剩一条（无 `match`），错误文本里不再出现 `supervisor` 这个出路。

- [ ] **Step 1: RED —— 钉住"必须被拒"（先把两条测试写出来，确认红）**

(a) `crates/sandlock-core/tests/integration/test_mediation_identity.rs`：

```rust
/// The privileged-mediator downgrade no longer exists. A root process that
/// would mediate on behalf of a different host uid must be refused before
/// fork, with the route-B remedy in the message and no `supervisor` escape.
#[test]
fn privileged_in_process_mediation_is_refused_with_route_b_remedy() {
    let err = Sandbox::builder()
        .chroot("/tmp")
        .fs_read("/")
        .user(21700, 21700)
        .build()
        .expect_err("root mediator + host uid 21700 + path mediation must be refused");
    let msg = err.to_string();
    assert!(
        msg.contains("Run sandlock-supervise as uid 21700 (route B)"),
        "refusal must name the route-B remedy verbatim, got: {msg}"
    );
    assert!(
        !msg.contains("mediation_run_as=supervisor"),
        "the removed downgrade tier must not be offered as a remedy, got: {msg}"
    );
}
```

(b) `crates/sandlock-core/tests/integration/test_instance_chroot.rs`：反向用例，
**证明没有误伤**（`mediation_active` 前置条件必须保留）：

```rust
/// Once the identity rule is enforced, a *non-mediated* per-uid shape must
/// still build: `mediation_active` is what gates the refusal, not RunAs.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn pure_per_uid_run_as_is_still_accepted() {
    let base = temp_dir("pure-per-uid");
    let ws = base.join("workspace");
    std::fs::create_dir_all(&ws).expect("create workspace");
    let policy = Sandbox::builder()
        .fs_write(&ws)
        .user(21700, 21700)
        .build()
        .expect("no chroot/COW/policy-fn => no mediation => RunAs stays legal");
    let mut inst = SandboxInstance::launch_exec_only(policy)
        .await
        .expect("pure per-uid instance must still launch");
    inst.shutdown().await.expect("shutdown");
    cleanup(&base);
}
```

- [ ] **Step 2: 删档（fork 代码面）**

1. `sandbox.rs`：`enum MediationRunAs` 整体删除（连同 `Display`/`FromStr`/serde），
   `Sandbox.mediation_run_as` 字段、builder 字段、`mediation_run_as()` 方法与
   `mediation_remap_is_refused` 分支里的 `match` 一并删除——保留 `Caller` 那一支的逻辑
   （即"需要中介且身份不匹配 ⇒ 拒绝"），错误文本删掉
   `or pass mediation_run_as=supervisor to explicitly accept the downgrade` 这半句。
2. `builder.rs`/`instance.rs`/`profile.rs`：删字段与其序列化（`profile.rs:560` 那行三元判断整删）。
3. `sandlock-ffi`：删 `sandlock_sandbox_builder_mediation_run_as` 导出函数；重跑 cbindgen 更新
   `include/sandlock.h`；`ffi/tests/mediation_run_as.rs` 整文件删除并在 wheel verify 的符号基线里
   登记 -1。
4. `sandlock-cli`：删 `--mediation-run-as` 与其用例。
5. `sandlock-supervise/src/policy.rs`：去掉该字段（wire 不再接受它；旧客户端发来会按
   "unknown field refused by name" 拒绝）。
6. Python：`_sdk.py` 拆掉 `_b_mediation_run_as` 绑定与 policy 字段、`sandbox.py` 删
   `mediation_run_as: str = "caller"` 与其取值校验。

- [ ] **Step 3: 反转 C 档验收（supervise 侧）**

`crates/sandlock-supervise/tests/mediation_2uid.rs`：删掉"root 特权中介 + 显式 supervisor 档
可用（属主错位、跨 uid 删除）"那组用例，替换为"**特权中介 + 需要中介 ⇒ 建箱前拒绝**"，
断言逐字包含 route-B 修法且不含 `supervisor`。**保留 B 档**（两个不同 uid 的真槽位，
内核级隔离证据）不动。

- [ ] **Step 4: 全文面清场核验**

```bash
cd third_party/sandlock
grep -rn 'mediation_run_as\|MediationRunAs\|mediation-run-as' crates/ python/ --include='*.rs' --include='*.py' --include='*.h' | grep -v '^target'
```

Expected: **无输出**（`tmp/`、`docs/`、`.superpowers/` 下的历史记录不算）。

- [ ] **Step 5: 跑红/绿与全档**

```bash
chmod -R a+rwX third_party/sandlock/tmp
docker run --privileged --rm -v "$PWD/third_party/sandlock":/src -w /src sandlock-dev:latest \
  sh -c 'cargo test -p sandlock-core --test integration mediation_identity pure_per_uid -- --nocapture'
docker run --privileged --rm -v "$PWD/third_party/sandlock":/src -w /src sandlock-dev:latest sh scripts/test-all.sh
docker run --privileged --rm -v "$PWD/third_party/sandlock":/src -w /src --entrypoint bash sandlock-dev:latest -c 'sh scripts/test-all.sh --mediation-2uid'
docker run --privileged --rm -v "$PWD/third_party/sandlock":/src -w /src --entrypoint bash sandlock-dev:latest -c 'sh scripts/test-all.sh --oci-root'
docker run --privileged --rm -v "$PWD/third_party/sandlock":/src -w /src --entrypoint bash sandlock-dev:latest -c 'sh scripts/test-all.sh --supervise-root'
```

Expected: 全绿；`docs/test-baseline.md` 计数按删除的用例数下调并登记。

- [ ] **Step 6: wheel 重建 + verify（ABI 变更）**

```bash
cd third_party/sandlock && sh python/build-wheels.sh && sh python/verify-wheel.sh
cp wheels/*.whl /Users/polus/project/ai/sandlock-e2b/wheels/fork/
```

Expected: verify 全绿，且**导出符号计数比上一基线少 1**（`sandlock_sandbox_builder_mediation_run_as`）；
manifest HEAD == fork HEAD。

- [ ] **Step 7: E2B 侧清理与文档**

- `envd_service/route_b.py`：`supervise_policy_document` 里对 `mediation_run_as` 的 drop
  分支简化为注释说明"该字段已从 fork 删除；保留这一行是为了让旧 ceiling 不会把它带进 wire"，
  或直接删除并同步两条单测。
- `tests/unit/test_route_b_wiring.py` / `test_sandlock_executor_route_b.py`：把"字段被丢弃"
  的断言改成"字段在 fork/ceiling 中都不存在"。
- `docs/production-deployment-requirements.md` §2.4「删档的后果」段改写为终态：
  **root worker + chroot ⇒ route B 是唯一路径**，并列出四条硬前置（wheel 带 supervise、
  `E2B_ROUTE_B≠off`、`E2B_PER_SANDBOX_UID=true`、uid 段不重叠）。
- `docs/sandlock-upstream-issues.md`：SL-1 标注为"**已由硬删关闭**（降级档不存在，
  特权中介形态被 fail-closed 拒绝）"。

- [ ] **Step 8: E2B 侧回归**

```bash
lane pytest tests/security/test_template_isolation.py tests/contract/test_route_b_executor.py \
  tests/contract/test_route_b_slot_pool.py tests/unit/test_route_b_wiring.py \
  tests/unit/test_sandlock_executor_route_b.py -q --tb=short
```

Expected: 全绿（含 `test_in_process_chroot_is_refused_without_a_slot`）。

- [ ] **Step 9: 提交（fork + main 各一条）**

```bash
git -C third_party/sandlock add -A crates python docs
git -C third_party/sandlock commit -m "feat(breaking)!: remove the privileged-mediator downgrade tier (SL-1)"
git add third_party/sandlock envd_service tests docs
git commit -m "chore(sandlock): bump to the no-downgrade wheel and drop the supervisor tier references"
```

---

## Track C — 上线（**2026-09-11 用户解禁：按非 root 形态上线，并要求线上测试全绿**）

> **新目标（用户 2026-09-11）**：所有任务完成后，**按非 root worker 形态**部署线上环境，
> 并确保线上测试全部通过。原"决定 ⑤ 暂不升级"作废。**Track F（F1 非 root + file caps）
> 是本次上线的硬前置**。
>
> **入口条件（全部满足才动线上）**：
> 1. Track F / Task F1 闭环：两个 broker 在非 root 形态下端到端五条断言全绿 + Track Z 非 root 复跑全绿；
> 2. `PROD_DROP_CAPS=SYS_ADMIN UNPRIVILEGED_PHASE=0 ./deploy/scripts/test-prod-shaped.sh` 与
>    `bash tmp/run-f31.sh` 六相在本地**连续两轮全绿**；
> 3. 线上**回滚点已记录**（现网镜像 tag/digest、`docker-compose.prod.yml`、远端 `.env` 三份快照落 `tmp/rollback-<ts>/`）；
> 4. 用户确认维护窗口（现网审计为空载，适合窗口）。
>
> **顺序（不可颠倒）**：① 镜像（worker/control-plane/gateway/quota-agent）构建并推 ACR →
> ② 远端 `.env` 与 compose 同步（含 uid 段拆分、`E2B_PRIV_HELPERS=auto`、配额 agent URL）→
> ③ 起/重建容器 → ④ 等 warm 完成（F5/F7 窗口 20–120s）→ ⑤ 跑线上测试。
> **B3 是 breaking**：wheel/`.so`/worker 镜像必须同批；旧 `--mediation-run-as` CLI/profile 会按名拒绝。

### Task C0：线上回滚点与前置核对（新增）

- [ ] 记录现网三份快照（镜像 tag/digest、compose、远端 `.env`）到 `tmp/rollback-<ts>/`
- [ ] 核对两颗 worker 的 uid 段：现网审计发现两边都从 10000 起 ⇒ **必须拆成互不重叠**
      （建议 worker-1 `10000..10999`、worker-2 `11000..11999`，可按你的偏好改）
- [ ] 核对 quota-agent：摘 `SYS_ADMIN` 前必须先把 `E2B_QUOTA_AGENT_URL` 配好并有 agent 在跑

### Task C0.5：线上测试清单（新增，C3 之后执行）

- [ ] `deploy/scripts/smoke-prod-worker.sh`（worker 自检：非 root 身份、route-B 槽位、uid 隔离）
- [ ] `deploy/scripts/deployment_smoke.py` 与 `deploy/scripts/multinode_smoke.py`（跨真实两节点）
- [ ] 官方 SDK 手工复核：`pwd == /home/user`、写读回显、**两个沙箱 workspace 属主是两个不同 uid**、
      跨 uid 访问被拒（1777+sticky）
- [ ] 线上日志核对：worker 出现 `route-B instance ready … host-uid=<池内 uid>`，
      无 `PER_UID_NONROOT_WARNING`，**无**缺 `SYS_ADMIN` 类告警
- [ ] 结果落 `tmp/prod-verify-<ts>/*.log`，首行 ENV-HEADER；任一红即按回滚点回退并报告

### Task C1：镜像重建 + ACR 推送

- [ ] **Step 1: 用 A3 的 wheel 重建 worker/测试镜像**（`deploy/scripts/build-and-push.sh`）。
- [ ] **Step 2: 推送 ACR（双架构）**，记录 digest。
- [ ] **Step 3: 镜像内自检**：`sandlock_supervise_connect_fd` 存在、
  `sandlock/bin/sandlock-supervise` 存在且 sha256 == wheel manifest 对应行。

### Task C2：uid 段拆分 + 线上升级顺序

- [ ] **Step 1: 两个 worker 配互不重叠的 `E2B_UID_POOL_START/SIZE`**（现网两边都从 10000 起）。
- [ ] **Step 2: 按"先前面的镜像、后代码"的顺序升级**（顺序反了会出现"镜像 rootfs 沙箱
  全部建不出来"，见 HANDOFF「删档的后果」）。
- [ ] **Step 3: 升级后自检**：`./deploy/scripts/smoke-prod-worker.sh`；worker 日志出现
  `route-B instance ready … guest-uid=uid-0-in-userns|host-uid=<uid>`。

### Task C3：E1.2 部署验证 + E8.1 远程 smoke

- [ ] **Step 1: `deploy/scripts/multinode_smoke.py` 跑通**（多节点创建/命令/文件/迁移）。
- [ ] **Step 2: `deploy/scripts/deployment_smoke.py` 跑通**，证据落 `tmp/`。

### Task C4：O1 —— 目标机 XFS prjquota（维护窗口）

- [ ] **Step 1: fstab 加 `prjquota` + 在线 remount**；`xfs_quota -x -c state` 显示开启。
- [ ] **Step 2: 配额用例在目标机形态下全绿**（不再是降级态）。

### Task C5：O2 —— TLS 证书/代理层（代码侧 E1.4 已完成）

- [ ] **Step 1: 证书签发（`deploy/scripts/gen-tls-cert.sh`）+ 代理层配置。**
- [ ] **Step 2: 控制面 TLS 端到端复验。**

### Task C6：O3 —— 凭据管理（ACR / API key / redis / SSH）

- [ ] **Step 1: 用 E5.4 的 master key 轮换能力接入密钥管理**，并做一次轮换演练。

### Task C7：T1 + P2 —— 真实存储复测

- [ ] **Step 1: T1**：真实 XFS/ext4 上验证"沙箱能否 chmod 自己写的文件"与"共享卷
  1777+sticky 跨 uid 保护"，去掉带证据的 skip。
- [ ] **Step 2: P2**：真实生产 NFS 上重跑 `deploy/scripts/nfs_quota_probe.sh`，核对
  per-sandbox uid × `no_root_squash` 组合。

---

## Track D — 产品决策与清理

### Task D1：FUP #4 —— 网关启动失败必须对 SDK 可见（决定 ④）

**Files:** `envd_service/runtime/context.py`（watcher）、
`tests/contract/test_mcp_netns.py` 或新增 `tests/contract/test_mcp_gateway_failure.py`

- [ ] **Step 1: 定义可见面**：网关在 `start_mcp_gateway` 后早期退出（watchdog 已捕获
  `sandbox_id/port/stderr/exit code`）时，不能只打 ERROR——SDK 侧要能拿到失败。
  选定的口径：**首次 `commands.run`（或 MCP 调用）返回非 0 退出码 + stderr 里带
  `mcp gateway failed to start` 原文**，而不是先回 exit-0 让用户看到空结果。
- [ ] **Step 2: 写失败用例**：用一个必然启动失败的 MCP 配置（例如把 gateway 入口指向
  不存在的模块）建箱 → 调用 → 断言 `exit_code != 0` 且 stderr **逐字包含** watcher 记下的
  失败原因文本。先跑成红（当前是 exit 0）。
- [ ] **Step 3: 实现**：watcher 把失败原因记到 sandbox 运行时状态；命令路径在首次 exec
  前检查该状态并直接失败（或把原因挂到该次命令的 stderr 尾部）。
- [ ] **Step 4: 验证**：新用例绿 + `tests/contract/test_mcp_netns.py` 全绿 +
  `tests/security/test_template_isolation.py`（MCP 基镜像形态）无漂移。
- [ ] **Step 5: backlog #4 关闭并写证据日志名。**

### Task D2：OCI 限流回落策略

- [ ] **Step 1: 固化"最快且测试正常"的默认形态（决定 ⑦）**：把 `E2B_REGISTRY_MIRRORS`
  的多源回落（`|` 分隔、按顺序尝试）作为默认路径写进
  `docs/production-deployment-requirements.md`，与 `tests/conftest.py` 里 buildkitd
  mirror 的取值方式对齐（同一 env，不再硬编码单源）。
- [ ] **Step 2: 测试侧用本地 registry 预置镜像**（`registry:2` + `127.0.0.1:5080`，
  镜像全集必须齐，404 按既有语义不重试），把这条写进测试文档。
- [ ] **Step 3: 只有当 Step 1+2 落地后实测仍抖动**，才加 `<image>.digest` 侧车回落；
  本轮不做。
- [ ] **Step 4: 验证**：`lane pytest tests -q --perf`（OCI 形态 `E2B_BASE_IMAGE=python:3.11-slim`）
  无 registry 相关失败；日志落 `tmp/d2-oci.log`。

### Task D3：清理授权

- [ ] **Step 1: 删除 `tmp/stale-20260902`**（4.9G，G2 取证目录，文档已写明可删）。
- [ ] **Step 2: docker 侧回收**（images 18G / volumes 39.7G / build cache 8.7G）。
  **卷里混着别的项目数据：只按名字精确删除本项目对象，不跑 `prune`。**

---

## Track E — 候补（触发式，本计划不排期，仅登记）

| 项 | 触发条件 |
|---|---|
| FUP-19 per-child fs/bind 强制 | 出现"子进程需要比实例 ceiling 更窄"的真实需求 |
| FUP-20 credential per-child 归因 | 多租户共享实例 + 凭据隔离需求 |
| FUP-21 port-aware `update_network` | MCP/端口映射需要按命令粒度变更 |
| Block C（SOCKS5 on-behalf） | 静态/Go 应用隧道成为客户需求 |
| M6 wheel 矩阵（cp310–313） | 出现非 cp314 的目标运行时 |
| M7 上游 PR（无特权部分） | 上游接受 netns-free 分支的评审节奏确定 |

---

## Track Z — 本地部署测试（全部计划完成后执行，决定 ⑨）

> 入口条件：Track A / B / D 全部闭环，且 Track A 的无 SYS_ADMIN 门禁与常规三档已绿。
> 目标是用**部署形态**（而非测试形态）验证结局：本地 compose 起控制面 + 网关 + worker，
> 跑一遍真实 SDK 流程。

### Task Z1：本地 compose 起栈（E1.2 的本地形态）

**Files:** 无代码改动；产出 `tmp/z1-compose.log`、`tmp/z1-smoke.log`。

> **执行结果（2026-09-11）：见 `.superpowers/sdd/task-Z-report.md`。** Z1/Z2 全绿，但有三处
> 偏离需要记录：① 出厂清单 worker 是 `user: "65534:65534"`，envd 因此自动关闭 per-sandbox
> uid ⇒ **route-B 默认不成立**（与 §2.4 及线上 root worker 冲突，报告 F1，待拍板）；本轮
> 因此跑了两种形态——出厂形态 + root worker（不加任何 cap）形态，两形态冒烟均 `EXIT=0`，
> route-B 证据取自槽位进程表（`route-B instance ready` 行在部署日志里被 root logger=WARNING
> 吞掉，报告 F4）。② 冷启动有两个部署缺口（共享卷属主、worker 缺 `E2B_IMAGE_REGISTRY`），
> 已修并提交（`2cb85fb` + 口径更正 `5df7367`）。③ `smoke-prod-worker.sh` 的容器形态早于
> E5.1/A7，出厂形态 3 errors、部署身份下 1 条用例断言旧共享 uid 语义（报告 F9，未改）。
> 另注：本机 `python` 不在 PATH，冒烟用项目内临时 venv（`tmp/z-venv/bin/python`）。

> **F4 收口修复（2026-09-11，本行以上保留为当轮记录）**：`envd_service/__main__.py` 现在在
> `uvicorn.run` 之前配置日志（root level = `E2B_LOG_LEVEL`，默认 INFO），因此上面那条验收
> 口径（Step 2「worker 日志出现 `route-B instance ready …`」）重新可用。实跑：用本轮工作树
> 重建的 worker 镜像 + 同一套 stack compose（root-worker override）跑
> `deploy/scripts/multinode_smoke.py`（`EXIT=0`，2+2 分布），`worker-1/worker-2` 容器日志里
> 有 **4 条** `route-B instance ready sandbox_id=… uid=<host uid> … guest-uid=uid-0-in-userns`
> 与 `worker image warmed: …`（默认 `E2B_LOG_LEVEL=INFO`，无需 DEBUG）。证据：
> `tmp/f4-real-stack.log`、`tmp/f4-real-stack-worker-logs.log`、`tmp/f4-logging-probe.log`。
> 注意：该行仍只在 **root worker**（route B 成立）形态出现，出厂 `user: "65534"` 形态取决于
> F1 的口径（未改）。

- [x] **Step 1: 构建镜像（本地 tag，不推 ACR）**

```bash
cd /Users/polus/project/ai/sandlock-e2b
./deploy/scripts/build-images.sh 2>&1 | tee tmp/z1-build.log
```

- [x] **Step 2: 起本地栈**

```bash
docker compose -f deploy/stack/docker-compose.prod.yml up -d 2>&1 | tee tmp/z1-compose.log
docker compose -f deploy/stack/docker-compose.prod.yml ps
```

Expected: control-plane / gateway / worker 三个服务 healthy；worker 日志出现
`route-B instance ready … host-uid=`，且**没有** `SYS_ADMIN` 相关 WARNING。

- [x] **Step 3: worker 自检**（脚本形态过时 ⇒ 记录 F9；worker 侧改用运行栈 + route-B 进程证据）

```bash
./deploy/scripts/smoke-prod-worker.sh 2>&1 | tee tmp/z1-worker-smoke.log
```

### Task Z2：本地部署冒烟（SDK 端到端）

- [x] **Step 1: 跑部署冒烟脚本**

```bash
python deploy/scripts/deployment_smoke.py 2>&1 | tee tmp/z2-deploy-smoke.log
python deploy/scripts/multinode_smoke.py 2>&1 | tee tmp/z2-multinode-smoke.log
```

Expected: 全绿。两条脚本使用真实 HTTP + 真实镜像 rootfs + 真实 route-B 槽位。
实际：出厂形态与 root（无 cap）形态各一遍全绿（root 形态首跑撞 ACR token 偶发 TLS EOF，重试绿）。

- [x] **Step 2: 关键路径手工复核（无 SYS_ADMIN 域）**

用官方 SDK 跑一遍本地栈，逐项记录：

```bash
python - <<'PY' | tee tmp/z2-sdk-manual.log
import os
from e2b import Sandbox
sbx = Sandbox.create(api_key="local-key", api_url=os.environ["E2B_API_URL"],
                     template="base", timeout=300)
print("cwd:", sbx.commands.run("pwd").stdout.strip())           # 期望 /home/user
print("abs:", sbx.commands.run("echo hi > /home/user/a.txt && cat /home/user/a.txt").stdout.strip())
vol = sbx.volumes.create("z2vol") if hasattr(sbx, "volumes") else None   # 无 API 时跳过
sbx.kill()
PY
```

Expected: `pwd` 输出 `/home/user`；写读回显 `hi`。
实际：两形态均满足（出厂形态沙箱内 `uid=65534`，route-B 形态 `uid=0` 自映射）。

- [x] **Step 3: 收栈与清理**

```bash
docker compose -f deploy/stack/docker-compose.prod.yml down 2>&1 | tee -a tmp/z1-compose.log
```

---

## 确认状态

已拍板（2026-09-10，见文首「已确认的决定」）：#1 规范别名 `/home/user`；#2 逻辑路径

---

## Track F — F1：非 root worker 下让 route B 可用（2026-09-11 用户拍板路线 3）

> 决策：**worker 长期目标是非 root（uid 65534）**，但 route-B 必须仍然可用（它需要
> "以任意池内 uid 起槽位"）。探针结论（`.superpowers/sdd/task-f1probe-report.md`、
> `tmp/f1probe-*.log`）：
> - **file capabilities 路线可行且已实测**：非 root 容器里带 `setcap` 的 helper 真拿到 cap；
>   同一进程自己 `setgroups([])→setgid→setuid→exec` 成功切到 10001；`chown` 到别的 uid 成功；
>   它 exec 无 caps 的二进制后 `CapEff` 自动归零（槽位仍是零 cap）。
> - **userns 路线不可行**：零 cap 只能自映射（=现状，无 per-sandbox uid）；映射到别的 host uid
>   需要父 userns 的 `CAP_SETUID` 或 `newuidmap`+subuid（镜像里没有 `uidmap`），探针里只有给
>   `CAP_SYS_ADMIN` 才跑通，且会被 fork 的 `--uid` 自检拒（`crates/sandlock-supervise/src/main.rs:153-161`）。
> - 形态选择：**2 个专用 broker + 一份共享校验模块**（用户 2026-09-11 拍板），不用 4 个 stock 副本。

### Task F1：两个 file-cap broker + envd 接线（2 天，分两阶段提交）

**Files（E2B 主仓库；fork 只读、不改）:**
- Create: `deploy/priv/priv_common.h`、`deploy/priv/priv_common.c`（共享校验：uid 池范围、
  根路径白名单 + `realpath` 逃逸防护、argv 形状）
- Create: `deploy/priv/slot_spawn.c`（`cap_setuid,cap_setgid+ep`）
- Create: `deploy/priv/maint.c`（`cap_chown,cap_dac_override+ep`）
- Modify: `deploy/docker/Dockerfile.envd`（多阶段：编译 → **最终阶段** `setcap`）
- Modify: `envd_service/route_b.py`（spawner 指向 `e2b-slot-spawn`）
- Modify: `envd_service/volumes.py`、`envd_service/app.py`、`envd_service/agent.py`（chown / walk / rmtree 改走 `e2b-maint`）
- Modify: `envd_service/config.py`（`E2B_PRIV_HELPERS=auto|off` + 自检）
- Modify: `deploy/stack/docker-compose.prod.yml`、`deploy/k8s/worker.yaml`（BND 补四条）
- Modify: `deploy/scripts/test-prod-shaped.sh`（unprivileged phase 的 cap 集）
- Create: `tests/unit/test_priv_helpers.py`（校验规则）、`tests/contract/test_nonroot_route_b.py`（端到端）
- Modify: `docs/production-deployment-requirements.md` §2.4/§2.4.1、`README.md`

**硬约束（都来自实测，违反即失败）**
1. helper 必须是**编译型二进制**（file caps 对 `#!` 脚本不生效）。
2. **`setcap` 必须在最终镜像阶段执行**——`COPY --from` 不保留 xattr，在构建阶段打过的 caps 会丢；
   构建期需要 `SETFCAP`（`libcap2-bin`），运行期不需要。
3. 运行期 **BND 必须含** `SETUID/SETGID/CHOWN/DAC_OVERRIDE`（`capabilities.add` 对非 root 不产生
   `CapEff`，只撑 BND）；**绝不设 no-new-privs**（实测 NNP=1 ⇒ file caps 全废）。
4. helper 落点必须**沙箱不可达**：放 `/var/lib/e2b-priv/`（root 所有、mode 0700），
   **不要**放 `/usr/local` 或 `/opt`（纯形态 Landlock 覆盖这两个前缀）。
5. `spawn` 的 `argv[0]` 必须**钉死**为 `sandlock-supervise` 的绝对路径，且 uid ∈ 池；
   `maint` 的 path 必须落在 `<workspace_base>/` 或 `<shared_volume_root>/` 之下（`realpath` 后判定）。
6. 槽位仍须是零 cap（exec 时自动丢）——不得为了省事把 caps 留在槽位上。

- [ ] **Step 1: RED（先写校验用例）**：`uid 不在池内`、`路径越出根`、`..`/符号链接逃逸、
  `argv[0]` 非 supervise 绝对路径、缺 helper、helper 无 cap —— 每条都要有精确断言且先跑成红。
- [ ] **Step 2: 阶段一提交**（broker + 镜像构建 + 校验单测）：镜像里 `getcap` 能看到两个二进制的 cap；
  非 root 容器内 `e2b-slot-spawn spawn --uid 10001 -- /…/sandlock-supervise …` 能起来且槽位 `CapEff=0`；
  `e2b-maint chown/rm/walk` 在白名单内可用、越界被拒。
- [ ] **Step 3: 阶段二（envd 接线 + 端到端）**：非 root worker 形态下建 chroot 沙箱 ⇒
  ① worker 日志出现 `route-B instance ready … host-uid=<池内 uid>`（F4 修好后该行可见）；
  ② 槽位进程 `sandlock-supervise --uid <该沙箱 uid>`；③ 两个沙箱的 workspace 属主是**两个不同** uid；
  ④ 跨 uid 写/删被拒（1777+sticky 真语义）；⑤ `pwd == /home/user`。
- [ ] **Step 4: 同步 `test-prod-shaped.sh` 的 unprivileged phase** 的 cap 集（否则该 lane 会证明
  file caps 不可用，门禁自相矛盾）。
- [ ] **Step 5: 文档**：§2.4 改成"非 root worker 是目标形态 + file caps 机制"；§2.4.1 的 cap 表补
  BND 四条与"构建期 SETFCAP / 运行期不需要"；README 与 `.env.example` 写明 `E2B_PRIV_HELPERS`
  与"不要加 no-new-privileges"；威胁模型写明"能 exec helper 即得该 cap（helper 在沙箱不可达路径）"。
- [ ] **Step 6: 重跑 Track Z**（非 root 形态）：Z1 起栈 + `smoke-prod-worker.sh` + Z2 两条冒烟 +
  SDK 手工复核，全绿；并把 F1 在 `docs/task-backlog.md` 标为已关闭。

**非目标（本轮不做）**：userns 路线（登记 follow-up，三触发条件见 `task-f1probe-report.md`）；
`E2B_EXECUTOR=local` 形态；fork 任何改动。

语义；#3 quota-agent；#4 SDK 要看到网关启动失败；#5 线上暂不升级（C1–C3 挂入口条件）；
#6 SL-1 走 **C-硬删**（降级档直接删除，不留逃生门）；#8 **不推上游**（B4 取消）；
#9 全部完成后跑 **Track Z 本地部署测试**。

**仍待确认（授权类，不是设计问题）：** C1–C3 上线窗口（决定 ⑤ 已明确"本地全绿后再谈"）、
D3 的删除授权（`tmp/stale-20260902` + docker 侧回收）。在给出之前这些 Task 保持未开始。

> 附录「SL-1 现状与三种口径」保留作为背景资料；其结论已被决定 #6（C-硬删）取代，
> 实现以 Task B3 为准。

---

## 附录：SL-1 现状与三种口径（待确认 #6）

### SL-1 到底是什么

chroot（镜像 rootfs）形态下，沙箱的路径操作**不是**由内核直接完成，而是由监督进程
（mediator）代执行：它在 seccomp USER_NOTIF 里解析虚拟路径 → 打开真正的宿主路径 →
用 `SECCOMP_IOCTL_NOTIF_ADDFD` 把 fd 塞回子进程（`open_in_namespace`，
`chroot/dispatch.rs`）。写家族（`unlinkat` / `mkdirat` / `renameat2` / `symlinkat` /
`linkat` / `fchmodat` / `fchownat` / `truncate`，共 8 组）同样走代执行。

因此**文件属主 = 代执行进程的身份**。如果 mediator 是 root、而沙箱 host uid 是 X：

| 症状 | 后果 |
|---|---|
| 沙箱新建的文件属主是 root 而不是 X | 沙箱自己 `chmod` 该文件 EPERM；后续迁移/删除要 root |
| `unlinkat`/`renameat2` 以 root 执行 | 1777+sticky 的"只能删自己文件"保护失效 ⇒ **跨 uid 删除** |
| `fchmodat`/`fchownat` 以 root 执行 | 沙箱能改动本不属于它的 inode |

（fork 的 `mediation_2uid` root 档用例就是这套降级的验收证据：root 属主文件 +
跨 uid 删除确实发生过，不是纸面担忧。）

### 为什么在 E2B 上已经不可达

1. **route-B 把 mediator 变成沙箱自己**：槽位进程 `euid == 沙箱 host uid`（`setpriv`
   降 uid、`CapEff=0`），代执行的属主天然正确 —— 这就是 T5 的关闭方式，且自 2026-09-09
   起 chroot 形态默认走这条路。
2. **fork 的 fail-closed 挡住了错配组合**：`mediation_run_as=caller`（默认）要求
   mediator euid == 沙箱 host uid，否则**建箱前拒绝**。E2B 曾下发的
   `mediation_run_as='supervisor'` 降级档已于 2026-09-10 从 envd 删除，
   `route_b.supervise_policy_document()` 还会主动丢弃该字段（防止它被重新带回来）。
3. **触发面本身被缩小**：`minimal_dev()`（P5）让 chroot 形态不再整树挂 `/dev`，
   `/dev/shm`、`/dev/mqueue` 不再需要 `fs_denied` carve-out；E2B 现在只对
   `/proc/kcore`、`/sys` 下发 denial，而这两个是**只读**路径 —— 只读代执行不产生
   属主问题。

结论：**E2B 的部署形态里 SL-1 的属主/删除两条后果都不可达**。剩下的只有：

### 三种口径

| 口径 | 动作 | 代价 | 适用前提 |
|---|---|---|---|
| **A（默认，推荐给 E2B 现状）** | 文档收口：按现状重写 `e2b-integration.md` §3.1 + 在上游问题索引标注"E2B 可达面已关闭、fork 内部语义保留"；并加一条契约测试钉住"错配组合必须被拒"（已有 `test_in_process_chroot_is_refused_without_a_slot`，补一条 `mediation_run_as=supervisor` 被 fork 拒绝的断言） | 0.5 天，纯 fork 文档 + 1 条测试 | 只看 E2B 自己的部署安全 |
| **B（fork 侧根治）** | 让代执行改为**以沙箱身份**执行：`open`/`openat`/`openat2` 与 8 组写家族代执行时用 `setfsuid/setfsgid(沙箱 uid)` 包住（或至少 `O_CREAT` 后 `fchown` 回沙箱 uid + 写家族按调用方复现 DAC 判定）。fork 文件：`chroot/dispatch.rs`（9 处代执行点 + 3 个 `openat2_in_root` 调用点）、`seccomp/notif.rs`、`sandbox.rs`（身份字段传递） | **3–5 天**（fork 改 + RED + `mediation_2uid` 改判 + wheel + 两轮全门禁）；且 `mediation_run_as=supervisor` 从"降级档"变成"安全档"，P2 的语义要重写 | 把 fork 当**通用库/上游**发布；或将来出现"特权 mediator + 非特权沙箱"的第三方用法 |
| **C（删档）** | 直接删掉 `mediation_run_as=supervisor` 这个 tier，只保留 `caller` + fail-closed | 2 天；**破坏性**：已有依赖该逃生门的调用方（P2 的迁移承诺）会断 | 确认没有任何外部使用者（上游未发布过则可行） |

### 我的判断

E2B 自己的部署不需要口径 B/C —— 上线用的是 route-B，二者都不会踩到 SL-1。真正值得做
的是两件不同的事：

1. **必须做（口径 A）**：把"为什么现在安全"写进 fork 文档，并加一条测试钉住
   fail-closed 与降级档的现状，避免将来有人重新打开 `supervisor` 档而没人发现。
2. **建议做（口径 B 的"最小版"）**：只修**属主那半**（`O_CREAT` 代执行后把属主改成
   沙箱 uid），不改写家族的 DAC 复现。理由：属主错误会让用户**看得见**的功能受损
   （chmod 失败、迁移要 root），而跨 uid 删除只在"denied 路径下有可写对象"时才可能，
   当前 E2B 形态里没有这种对象。最小版约 1 天，且能让 `supervisor` 档不再是
   "文件属主错位"的陷阱。是否做取决于你是否打算把 fork 交给上游（M7）——
   如果 M7 会推进，建议做；只自用则 A 足够。

## Self-Review

- **Spec coverage**：backlog #25（A1–A7）、#5 剩余部署（C2）、#4（D1）、SL-11（B2）、
  SL-12（B1）、SL-1 文档（B3）、fork 推送（B4）、ACR/升级（C1–C3）、O1–O3（C4–C6）、
  T1（C7）、P2（C7）、OCI 限流（D2）、清理（D3）、候补（E）均有对应 Task。
- **Placeholder scan**：无 TBD / "稍后补充"；代码步骤均给出可编译片段或精确断言。
- **Type consistency**：`set_virtual_cwd(pid: i32, cwd: PathBuf)`、
  `supervisor_processes: Option<Arc<ProcessIndex>>`、`fs_mounts: dict[str, str]`、
  `_ensure_traversable(path: Path) -> None` 在全文命名一致。
