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

- [ ] **Step 1: 记录三处 HEAD**

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

- [ ] **Step 2: 复跑探针，确认本机仍复现**

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

- [ ] **Step 1: 写第一条失败用例（子挂载在 `/workspace` 下，cwd 在 `/home/user`）**

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

- [ ] **Step 2: 跑它，确认是红的**

```bash
chmod -R a+rwX third_party/sandlock/tmp
docker run --privileged --rm -v "$PWD/third_party/sandlock":/src -w /src \
  sandlock-dev:latest \
  sh -c 'cargo test -p sandlock-core --test integration \
    test_relative_open_from_second_workspace_alias_resolves_the_submount -- --nocapture'
```

Expected: FAIL——`relative cat must exit 0`（stderr `cat: mnt/data/data.txt: Permission denied`）。
输出存 `tmp/a1-red.log`。若它直接通过，说明 fork 已在别处修过——立刻停下同步，不要继续 A2。

- [ ] **Step 3: 写第二条用例（cwd 身份）**

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

- [ ] **Step 4: 跑它，确认是红的**

Run: 同 Step 2 的容器命令，替换测试名（日志存 `tmp/a1-red-cwd.log`）。
Expected: FAIL，实际输出 `/workspace\n`——两条红指向**同一个**反查不确定性问题，修完必须同时转绿。

- [ ] **Step 5: 提交 RED**

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

- [ ] **Step 1: chdir 记录请求的虚拟路径**

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

- [ ] **Step 2: 为新 exec 子进程播种初始 cwd**

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

- [ ] **Step 3: 反查确定性化**

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

- [ ] **Step 4: 纯函数单测钉住平局规则**

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

- [ ] **Step 5: 跑 RED 两条用例 + 单测，确认转绿**

```bash
docker run --privileged --rm -v "$PWD/third_party/sandlock":/src -w /src sandlock-dev:latest \
  sh -c 'cargo test -p sandlock-core --test integration test_relative_open_from_second_workspace_alias_resolves_the_submount -- --nocapture && \
         cargo test -p sandlock-core --test integration test_getcwd_reports_the_alias_the_policy_declared -- --nocapture && \
         cargo test -p sandlock-core --lib chroot::resolve'
```

Expected: 两条 PASS + `host_to_virtual_tie_breaks_on_declaration_order` PASS。

- [ ] **Step 6: chroot/cwd 回归（防语义漂移）**

```bash
docker run --privileged --rm -v "$PWD/third_party/sandlock":/src -w /src sandlock-dev:latest \
  sh -c 'cargo test -p sandlock-core --test integration chroot -- --nocapture'
```

Expected: 全绿。若既有用例断言"`cd` 走符号链接后 `..` 用物理父目录"，按确认点 #2 取舍，
并在 `CHANGELOG.md` 明确记录这条语义变更。

- [ ] **Step 7: 提交**

```bash
git -C third_party/sandlock add crates/sandlock-core/src
git -C third_party/sandlock commit -m "fix(chroot): make the virtual cwd request-derived and the host->virtual tie-break deterministic"
```

### Task A3：fork 全门禁 + wheel 重建（半天）

**Files:**
- Modify: `third_party/sandlock/CHANGELOG.md`、`third_party/sandlock/docs/test-baseline.md`
- Produce: `wheels/fork/*.whl` + manifest（脚本生成）

- [ ] **Step 1: 非 root 全档**

```bash
cd third_party/sandlock && chmod -R a+rwX tmp
docker run --privileged --rm -v "$PWD":/src -w /src sandlock-dev:latest sh scripts/test-all.sh 2>&1 | tee tmp/a3-gate-nonroot.log
```

Expected: 各档与 baseline 一致，仅 core_lib / core_integ 因新用例递增。

- [ ] **Step 2: 三个 root 档**

```bash
docker run --privileged --rm -v "$PWD":/src -w /src --entrypoint bash sandlock-dev:latest -c 'sh scripts/test-all.sh --oci-root' 2>&1 | tee tmp/a3-gate-oci.log
docker run --privileged --rm -v "$PWD":/src -w /src --entrypoint bash sandlock-dev:latest -c 'sh scripts/test-all.sh --supervise-root' 2>&1 | tee tmp/a3-gate-supervise-root.log
docker run --privileged --rm -v "$PWD":/src -w /src --entrypoint bash sandlock-dev:latest -c 'sh scripts/test-all.sh --mediation-2uid' 2>&1 | tee tmp/a3-gate-mediation.log
```

Expected: 三档全绿（本机容器需 `--init`，见 HANDOFF「pid-1 不回收孤儿」注记）。

- [ ] **Step 3: wheel 双架构重建 + verify + 同步**

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
- Modify: `envd_service/executors/sandlock.py`（确认点 #1 选 `/home/user` 时改 `_view_cwd`）
- Delete: `tests/unit/test_runtime_context_volumes.py`
- Modify: `tests/unit/test_executor_policy.py`、`tests/unit/test_policy_mapping.py`
- Create: `tests/contract/test_shared_volume_relative_cwd.py`

**Interfaces:**
- Consumes: `RuntimeSandbox.volume_mounts = [{"path": rel, "hostPath": host}]`；
  `SandboxExecutor(fs_mounts: dict[str, str])`。
- Produces: 每个卷视图同时出现在两个别名下——`fs_mounts` 键集合 =
  `{f"/workspace/{rel}", f"/home/user/{rel}"}`（对每个 `rel`）。

- [ ] **Step 1: 先写契约测试（红）**

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

- [ ] **Step 2: 跑它，确认红**

```bash
lane pytest tests/contract/test_shared_volume_relative_cwd.py -q --tb=short 2>&1 | tee tmp/a4-red.log
```

Expected: FAIL（`cat mnt/data/a.txt` Permission denied）。

- [ ] **Step 3: 双别名注册 + 删除 bind 段落**

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

- [ ] **Step 4: 跑契约 + 单测，确认绿**

```bash
lane pytest tests/contract/test_shared_volume_relative_cwd.py \
  tests/contract/test_uid_permissions.py tests/contract/test_volumes.py \
  tests/unit/test_executor_policy.py tests/unit/test_policy_mapping.py -q --tb=short
```

Expected: 全绿（bind 相关单测随文件删除；`--ignore` 为空）。

- [ ] **Step 5: 迁移/多节点契约回归**

```bash
lane pytest tests/contract/test_migration.py tests/sdk/python/test_shared_volumes.py -q --tb=short
```

Expected: 全绿——这正是 `tmp/nosa-full.log` 剩下的 4 条红。

- [ ] **Step 6: 提交**

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

- [ ] **Step 1: 写失败单测**

构造 `<tmp>/root/_volumes/vol_a`，把 `root` 与 `_volumes` 造成 `0700`；调用
`provision_sandbox_volume_mount(..., host_uid=21700)` 后断言 `root`、`_volumes`、
`vol_a` 的 **mode 位**满足 `mode & 0o011 == 0o011`（用 mode 断言而非 `os.access`，
避免测试进程的 root 特权让断言空过）。

- [ ] **Step 2: 实现**

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

- [ ] **Step 3: 启动自检 + 文档**

worker 启动时用池内第一个 uid 探测 `settings.shared_volume_root` 的可穿透性，失败打一条
`WARNING`（点名目录、实际 mode、修法）；`docs/production-deployment-requirements.md`
新增 §2.4.2：**共享卷根及其祖先必须对租户 uid 可穿过（0711/0755），否则只有绝对路径
可用、相对路径会 EACCES**。

- [ ] **Step 4: 验证**

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

- [ ] **Step 1: namespaced sysctl 改由容器 spec 提供**

把 `net.ipv4.ip_unprivileged_port_start` 的运行时写入改成部署清单声明
（Docker `--sysctl` / k8s `securityContext.sysctls`），验证 MCP 端口（50005+）与既有
端口映射行为不变。

- [ ] **Step 2: xfs_quota 走 agent 或接受降级**

按确认点 #3 二选一：(a) 配 `E2B_QUOTA_AGENT_URL` 走 agent（推荐）；(b) 明确接受配额降级
（线上当前就是 `noquota` + 镜像内无 `xfs_quota`），并把"无 SYS_ADMIN ⇒ 无直接 xfs_quota"
写进文档，配额用例按既有 `ProjectQuotaError` 降级路径断言。

- [ ] **Step 3: 验证**

```bash
lane pytest tests/contract/test_volume_quota.py tests/unit/test_volume_quota.py -q --tb=short
```

Expected: agent 形态全绿；降级形态需 `E2B_TEST_STRICT_SKIPS=1` 显式暴露，不漏跑。

### Task A7：无 SYS_ADMIN 门禁固化 + backlog #25 收口（半天）

**Files:**
- Modify: `deploy/scripts/test-prod-shaped.sh`（capset 可配置）
- Modify: `docs/task-backlog.md`（#25 收口）、`docs/HANDOFF.md`（新增 ⚡ 段）

- [ ] **Step 1: 让 capset 可配置**

在 `CAPS` 拼装之后加入：

```sh
# Drop additional caps without editing the list above, e.g.
#   PROD_DROP_CAPS=SYS_ADMIN,SYS_PTRACE ./deploy/scripts/test-prod-shaped.sh
for drop in $(printf '%s' "${PROD_DROP_CAPS:-}" | tr ',' ' '); do
    CAPS="$CAPS --cap-drop $drop"
done
```

- [ ] **Step 2: 跑无 SYS_ADMIN 全量**

```bash
PROD_DROP_CAPS=SYS_ADMIN UNPRIVILEGED_PHASE=0 ./deploy/scripts/test-prod-shaped.sh 2>&1 | tee tmp/a7-nosa.log
```

Expected: **0 failed / 0 error**（本计划前基线 `tmp/nosa-full.log` = 4 failed）。

- [ ] **Step 3: 常规三档门禁无漂移**

```bash
bash tmp/run-f31.sh   # gate A / gate B / 生产形 phase1/phase2（macOS 另跑）
```

Expected: 与上一基线逐项一致（±本次新增用例数）。

- [ ] **Step 4: 台账收口**

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

### Task B3：SL-1 / e2b-integration §3.1 口径收口（半天，纯文档）

**Files:** `third_party/sandlock/docs/e2b-integration.md` §3.1、`docs/sandlock-upstream-issues.md`

- [ ] **Step 1: 按现状重写 §3.1**：route-B 槽位的路径中介 euid == 沙箱 host uid（属主不再
  是 root）；进程内 + chroot + 非 0 uid 组合在 E2B 已被 fork 拒绝建箱（不再是可达面）。
- [ ] **Step 2: 在 `docs/sandlock-upstream-issues.md` 标注 SL-1"E2B 可达面已关闭、fork 内部
  语义保留"，避免两处口径漂移。**
- [ ] **Step 3: 按确认点 #6 拍板后，若决定关票则同步 `CHANGELOG.md`。**

### Task B4：fork 本地提交推送 + PR 回复（30 分钟，**需授权**）

- [ ] **Step 1: 待授权后推 `upstream-pr/netns-free-clean`**

```bash
git -C third_party/sandlock log --oneline origin/upstream-pr/netns-free-clean..HEAD
git -C third_party/sandlock push origin upstream-pr/netns-free-clean
```

Expected: 6 个既有提交（F17 `e290059`/`f20d034`/`c0f7bf5`、F18 `03cd36b`/`9995e28`/`fb2e106`）
+ 本计划新增提交全部推上。

- [ ] **Step 2: 回复 PR #34 / #35**（SL-10 闭口 + 客体内 root 的安全论证 + mknod 围栏 +
  ptrace 前置）。
- [ ] **Step 3: 更新 `docs/sandlock-upstream-issues.md` 的推送状态列。**

---

## Track C — 上线与运维（**需窗口与授权，按顺序执行**）

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

### Task D1：FUP #4 —— 网关启动失败对 SDK 的可见性

- [ ] **Step 1: 决策**（确认点 #4）：维持"先收 exit-0"契约，还是让 SDK 上抛错误。
- [ ] **Step 2: 若上抛**：改 `envd_service/runtime/context.py` 的 watcher + SDK 契约，
  新增契约测试断言错误文本逐字匹配；否则把 backlog #4 标记为"维持现状"并关闭。

### Task D2：OCI 限流回落策略

- [ ] **Step 1: 决策**（确认点 #5）：给可认证 registry（ACR，现成路径），还是加
  `<image>.digest` 侧车回落（限流期感知不到 tag 更新）。
- [ ] **Step 2: 落地后**把 `E2B_REGISTRY_MIRRORS` 的默认形态写进部署文档，复跑 OCI 形态门禁。

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

## 需要你确认的地方（阻塞项）

1. **规范别名（canonical alias）选哪个？** 影响 `cwd`/`pwd`/卷挂载点：
   - **(a) `/home/user/<rel>`（推荐）** —— 与 `spec.md:170`（"挂载到 rootfs 内
     `/home/user/<path>`"）和官方 API 示例 `"cwd": "/home/user"` 一致；需要把
     `_view_cwd` 改为返回 `/home/user`，沙箱内 `pwd` 相对**现状**保持不变。
   - **(b) `/workspace/<rel>`** —— 与 E2B 现有 `_view_cwd` 注释一致，但 `pwd` 会从
     `/home/user` 变成 `/workspace`（可见行为变更，需过一遍 SDK 兼容性）。
   - 无论选哪个，A4 都会**两个别名都注册**；差别只在谁是"请求来源"。
2. **A2 的语义变更是否接受**：chdir 改为记录"请求的虚拟路径"后，`cd <符号链接>` 之后的
   `..` 按逻辑路径解析（更接近 shell 的 logical 模式），与内核"物理父目录"语义不同。
   若要求保持物理语义，则只做 A2 Step 3（反查确定性化），并把 A1 第二条用例的期望值
   改成 `/workspace`。
3. **无 SYS_ADMIN 后 `xfs_quota` 怎么办**：走 quota-agent（推荐，需要 agent 常驻与
   `E2B_QUOTA_AGENT_URL` 配置），还是接受配额降级（线上当前已是 `noquota`）。
4. **FUP #4 的产品口径**：SDK 是否需要看见网关启动失败（现在只有 worker ERROR 日志，
   SDK 先收 exit-0）。
5. **B4 与 C1–C3 的授权**：是否允许推 fork 提交 / 回 PR #34#35 / 推 ACR / 拆 uid 段并
   升级线上（现网空载，是升级窗口）。
6. **B3 收口口径**：SL-1 按"E2B 可达面已关闭、fork 内部语义保留"办理，还是要求 fork 侧
   把 `fs_denied` 中介身份彻底改掉（会牵动 `mediation_run_as` 语义）。
7. **D2 的 OCI 策略**：可认证 registry，还是 digest 侧车回落。

## Self-Review

- **Spec coverage**：backlog #25（A1–A7）、#5 剩余部署（C2）、#4（D1）、SL-11（B2）、
  SL-12（B1）、SL-1 文档（B3）、fork 推送（B4）、ACR/升级（C1–C3）、O1–O3（C4–C6）、
  T1（C7）、P2（C7）、OCI 限流（D2）、清理（D3）、候补（E）均有对应 Task。
- **Placeholder scan**：无 TBD / "稍后补充"；代码步骤均给出可编译片段或精确断言。
- **Type consistency**：`set_virtual_cwd(pid: i32, cwd: PathBuf)`、
  `supervisor_processes: Option<Arc<ProcessIndex>>`、`fs_mounts: dict[str, str]`、
  `_ensure_traversable(path: Path) -> None` 在全文命名一致。
