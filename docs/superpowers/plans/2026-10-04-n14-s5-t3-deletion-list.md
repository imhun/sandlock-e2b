# S5 / Task 3 第一步：fork 翻译路径的删除清单（供评审，未动代码）

> 判据来自 `docs/n14-retire-the-emulation.md` §4.2：一段代码**删除**当且仅当
> ① 它只在 `!child_is_pivoted` 时执行，**且** ② 它的产物是"宿主路径 ↔ 沙箱路径的换算"。
> 反例（只在模拟根下跑但产物不是翻译）一律保留；本清单逐 handler 过一遍，行号取自子模块
> `third_party/sandlock`（`da90921`）。

## 0. 一个必须先钉死的前提：S5 之后还会有非 pivoted 的中介子进程吗

| 形态 | 有 chroot root？ | 起中介？ | pivoted？ |
|---|---|---|---|
| image-rootfs（`E2B_BASE_IMAGE`，两份生产清单） | 有（解压出的 rootfs） | 有 | **是** |
| pure + 合成根（`E2B_PURE_ROOTFS=synth`，2026-09-27 起的默认） | 有（`<base>/_pure_rootfs/<id>`） | 有 | **是** |
| pure 无根（N15 identity） | 无 | `auto` 直接把它留在 in-process，"mediates nothing" | 不适用（**没有中介**） |

第三条正是 S5 退掉的那条（`E2B_PURE_ROOTFS=off` 启动期具名拒绝），所以**S5 之后走到
`chroot/dispatch.rs` 任何 handler 的子进程都是 pivoted**。这是本清单成立的唯一前提，也是
T3 落地时要写进代码注释的那句话。

一处必须一起改的语义：`child_is_pivoted()`（`:341`）在 `metadata()` 失败时 `return false`——
那是"读不到就当作模拟根"的**静默降级**。非 pivoted 分支删掉之后，这个 `false` 就没有可执行
的兜底了，必须改成"记一条 trace/日志并按拒绝处理"（fail closed），**不能**保留成静默 false。

## 1. 逐个 handler

宿主侧工作 = 策略判定（`can_read`/`can_write`/`fs_denied`）、COW 视图（`cow_resolve`）、
活账本（`mark_dirty`/`write_fds`/`raise_caller_file_size_limit`）、fd 注入与结果写回。

| handler | 入口 | pivoted 分支 | 模拟根专属的代码 | 裁定 |
|---|---|---|---|---|
| `handle_chroot_open` | `:791` | 无 | 无（同一段代码两种形态共用：注入 fd 是**中介 open 的实现**，不是翻译） | **保留**（§4.2 ❌：COW + 写监控 + fd 注入在这里） |
| `handle_chroot_exec` | `:1583` | `:1666` | **`:1674-1809`**：`openat2(RESOLVE_IN_ROOT)` 打开目标 → `read_pt_interp` 读 PT_INTERP → `memfd_with_patched_interp` 造补丁副本 → `ADDFD` 注入 → `rewrite_exec_path_to_fd` 把 `path_ptr` 改写成 `/proc/self/fd/N` | **删 `:1674-1809`**；`:1655` 的 `settle_closed_writes` 与 `:1667-1671` 的 `chroot_exe` 记账**保留**（§4.2 ❌：真根下仍要记账与写虚拟 exe） |
| `read_pt_interp` | `:1457-1514` | — | 唯一调用点在 `:1704`（上面那段） | **随 exec 一起删** |
| `memfd_with_patched_interp` | `:1519-1581` | — | 唯一调用点在 `:1749` | **随 exec 一起删** |
| `handle_chroot_write` | `:1816` | 无 | 无（unlinkat/mkdirat/renameat2/symlinkat/linkat/fchmodat/fchownat/truncate 全部是策略 + 账本 + `exec_on_host`） | **保留**（§4.2 ❌：账本与 COW 都在这里） |
| `handle_chroot_stat` | `:2295` | 无 | 无 | **保留**（§4.2 ❌：COW 视图 —— 沙箱写过的必须报副本元数据） |
| `handle_chroot_statx` | `:2343` | 无 | 无 | **保留**（同上） |
| `handle_chroot_readlink` | `:2404` | 无 | 无（`/proc/<pid>` 的 per-PID 门 + COW 视图都在这里） | **保留**（§4.2 ❌） |
| `handle_chroot_xattr` | `:2639` | 无 | 无 | **保留**（§4.2 ❌：COW 视图） |
| `handle_chroot_getdents` | `:2746` | **无条件 `Continue`** | 无 | **保留**（已经是单分支） |
| `handle_chroot_chdir` | `:2765` | `:2829` | `:2832` 的 `NotifAction::ReturnValue(0)` —— "答应成功但不让内核真的 chdir"，这正是模拟根的**模拟动作** | **删 `:2822-2832`**（`:2821` 的 `set_virtual_cwd` **保留**，见 §2） |
| `handle_chroot_fchdir` | `:2844` | 无 | 无（`reported_to_virtual` 对 fd 的映射对 pivoted 子进程**同样需要**，N43 已量） | **保留** |
| `handle_chroot_getcwd` | `:2867` | `:2886`（S3 已放行） | `:2890-2907`：读缓冲区、拼虚拟 cwd、`ERANGE` 长度检查、`write_child_mem` 写回 | **删 `:2884-2907`**；函数只剩一个 `Continue`（`NotifAction` 都不需要返回值） |
| `handle_chroot_statfs` | `:2914` | 无 | 无（策略 + 在宿主侧执行 + 写回结果） | **保留**（§4.2 ❌：策略） |
| `handle_chroot_inotify_add_watch` | `:2984` | 无 | 无（在根的解析对象上注册 watch，是策略面） | **保留**（§4.2 ❌） |
| `handle_chroot_utimensat` | `:3038` | 无 | 无 | **保留**（§4.2 ❌：COW 视图） |
| `legacy_*` ×13 | `:3118-3351` | 无 | 无：它们只把非 `*at` 拼写**换成 `*at` 拼写**再委托（`notif_with_args` + `synth.data.nr`），产物是参数布局规范化，**不是路径翻译** | **保留全部 13 个**（§4.2 原话："大多是薄转发，但它们是否触达账本要读代码定" —— 读完的结论是：账本在被委托的 handler 里，壳自己没有） |

## 2. 明确**不删**、且为什么（评审重点）

1. **`/proc` 合成**（`crates/sandlock-core/src/procfs.rs`）：真根下内核的 `/proc` 是空的、真 procfs
   挂不上（三形态实测全 EPERM）。`procfs.rs:1427` 读 `processes.virtual_cwd(pid)`，所以
   `set_virtual_cwd`（`dispatch.rs:489`）与 `handle_chroot_fchdir` 的跟踪**保留**。
2. **策略判定与 COW 视图**：§4.2 表里所有 ❌ 的 handler 的 body 全部保留 —— 真根下
   "沙箱写过的东西必须报副本元数据"这条没有消失。
3. **活账本**：`mark_dirty` / `write_fds` / `raise_caller_file_size_limit` / `settle_closed_writes`
   一行不动（S4 已证全树 walk 够用，但那是"要不要保留快路"的问题，不是"翻译"的问题）。
4. **`reported_to_virtual` 的映射回调**（`:376-382`）：它对 pivoted 子进程**仍然**要先尝试
   `host_to_virtual` —— N43 量过，中介注入的 fd 在 pivoted 子进程里照样报宿主路径。删掉它会把
   N43 重新引回来。（删的只是 `child_is_pivoted` 为 false 时"整体走映射"这条分支的前提。）
5. **`build_virtual_path` 的 dirfd 分支**（`:604-633`）：N43 的修法就在这里，两种形态共用。

## 3. 需要评审点头的三处判断

1. **`enforce_resolve_flags`（`:509-550`）**：它拒绝 `RESOLVE_BENEATH/IN_ROOT/NO_MAGICLINKS` 等
   中介无法在"宿主侧代做"时兑现的标志。真根下内核本可以自己兑现这些标志，但**中介仍然自己解析
   路径**（策略判定要求），所以它看起来仍需要。T3 落地前需要一个人对着 `openat2(2)` 复核：
   哪些标志在"中介解析 + 宿主侧 open"这条路上真的兑现不了。
2. **`read_symlink_in_root`/`resolve_self_fd_magic`/`magic_self_fd`（`:1389-1455`）**：它们是
   `open_in_namespace`（`:1330`）的组成部分，服务于"`/proc/self/fd/N` 这类 fd 引用"的 open。
   本清单判**保留**（open 本身保留），但它们是否需要"按 pivoted 简化"没有细读。
3. **模块头注释**（`:18-40`）：第 4 条（Path-rewrite-then-Continue）随 exec 一起删；第 2 条
   （on-behalf result writes）要从名单里去掉 `getcwd`；`landlock.rs:497` 提到 PT_INTERP 补丁的
   注释也要改。这些是纯文档改动，但漏改会让下一个读者以为补丁还在。

## 4. T3 落地时的验收口径（计划里的 Step 3）

- fork 侧：`core_integ` 559 + `test_chroot` 51 + `test_instance_exec` 28 + `test_cow` 26 +
  `test_restore` 5 + `test_procfs`，全绿；`scripts/test-all.sh` 的四条相位逐条比对
  `docs/test-baseline.md`。
- E2B 侧：`tests/security` 在 x86_64 生产形态容器与 arm64 lane 上跑一遍（N35 三条 shebang 用例
  是这次改动最直接的判据：删掉注入路径之后它们**仍然**要绿 —— 真根下内核自己解析解释器）。
- 每个被删的分支先写一条会红的用例（例：真根下 `exec` 一个"写进去立刻执行"的脚本，判据是 rc=0；
  删代码前它应当由注入路径满足，删后由内核满足）。

## 5. 本清单**不含**

- 任何代码改动（fork 有自己的 git 历史与 CI；本文件是 Step 1 的产物，评审通过后才动）。
- `/proc` 合成、策略/COW、活账本的任何改动。
- S5 的 E2B 侧（T1/T2/T4/T5 已完成，见 `docs/open-issues.md` N14 行与提交 `b8d9d72`/`d8bf694`/`1478a3c`）。

## 6. 开工落地时的实测（2026-10-05：第一步做完后暂停）

按批准开工后先在 fork 里落了"创建即拒"（`Sandbox::do_create_stdio` 里 `chroot.is_some() && !real_root`
→ 具名 `SandboxRuntimeError::Child`），再用门禁量爆炸半径。**结论：这比"删 200 行"大得多**，
三件事必须先处理，所以暂停在这里、把半成品留在子模块 `stash@{0}`
（`N14 S5 T3 wip: create-time refusal + test_chroot real-root conversion (incomplete)`），
父仓工作树保持干净。

### 6.1 模拟根在 fork 自己的测试里是**承重**的（28 处）

`chroot(...)` 的构造点里 **28 处没设 `real_root`**（`test_chroot.rs` 13、`test_instance_chroot.rs` 8、
`test_mediation_identity.rs` / `test_net_isolate.rs` / `test_procfs.rs` / `test_sandbox.rs` /
`test_seccomp_enforce.rs` / `test_transaction.rs` 各 1 …）。创建即拒一开它们全红，这是预期的；
下面那条不是。

### 6.2 `test_chroot.rs` 里 **39 个用例在"静默跳过"**（本次最值钱的发现）

它们都写成 `match policy.run(...) { Ok(r) => {…}, Err(e) => eprintln!("Chroot test skipped: {}", e) }`
—— 构造失败只打一行字，用例照样算**通过**。也就是说这份"chroot 家族全绿"从来没证明它跑过。
WIP 里已把这 39 处改成 `panic!`，而这正是让 42 条红现形的动作（正式落地必须保留）。
这条与仓库《规范-测试规范》"禁止 SKIP、禁吞错误"直接冲突。

### 6.3 把一条 chroot 用例改成真根要三件事，不止 `.real_root(true)`

1. `.real_root(true)`；
2. **`.user(euid, egid)` + `builder.userns_self_map = true`** —— 非 root 中介没有 userns 时，
   真根子进程的 `unshare(CLONE_NEWNS)` 得 EPERM（与 E2B 侧 2026-10-04 的发现同源，见
   `docs/open-issues.md` N14 行 T4 段）；`userns_self_map` 的生效条件还要求 `user.is_some()`
   （`context.rs:706-722`），所以 `user` 不能省；
3. **夹具要预建挂载点**：`realroot::build` 要求每个 `fs_mount` 目标"在 rootfs 里已存在"
   （否则 `mount point … does not exist inside the rootfs`），而模拟根从不要求 ——
   `build_test_rootfs` 的骨架要按各用例的挂载表补 `mkdir`。

### 6.4 门禁在这个 pin 上**本来就是红的**

`core_lib: baseline says 913 passed, run produced 922`（子模块干净 pin `da90921` 上原样复现，
先 stash 掉本次改动验过）。`scripts/test-all.sh` 在**第一个**算错的相位就退出，所以自那次 pin
之后没人跑过门禁的另一半。T3 落地时必须：① 找出这 9 条差在哪（多半是先前会话加了用例而没刷新
`docs/test-baseline.md`）；② 刷新基线与 `core_integ` 那一档；③ 四个相位（默认 + `--oci-root` +
`--supervise-root` + `--mediation-2uid`）全部复跑。

### 6.5 建议的两次提交（而不是一次大改）

1. **fork 提交 A（行为变更）**：创建即拒 `chroot && !real_root` + 28 处构造点与夹具迁到真根 +
   6.2 的 39 处静默跳转换成 panic + 刷新 `docs/test-baseline.md`。此时模拟根已不可达，门禁
   仍全绿（一态）。
2. **fork 提交 B（纯删除）**：删 §1 表里那几段（exec 注入半段 + `read_pt_interp` +
   `memfd_with_patched_interp` + chdir 尾巴 + getcwd 改写半段）与模块头注释，行为不变。
   再重建 wheel、更新父仓指针、走一次 T5 式的上线与现场验收（`probe_real_root_shape.py`）。

拆成两次的价值：A 之后"要删的东西确实不可达"是被门禁证明过的，B 就只是删死代码 —— 评审与
回滚都简单得多。

## 7. 落地结果（2026-10-05，A、B 两次提交都已落在子模块）

* **A = fork `939bb90`**（行为变更）：`chroot && !real_root` 创建即拒；`SandboxBuilder::chroot()`
  默认 `real_root = true`；真根在 `confine_child` 里**主动要一个 userns**（且是必需的：拿不到就
  建箱失败，不再半途而废）；`reported_path_virtual` 把 N43 的规则用到 **cwd** 上（fchdir 穿过
  中介注入的 fd 会让 cwd 报宿主路径，旧代码把它当虚拟拼写 ⇒ 之后每个相对路径都 EACCES，实测）。
  测试侧：夹具迁真根（挂载点要在 rootfs 里存在；父路径已被绑定时要在**源侧**存在）、identity
  形态的用例收敛、`test_chroot.rs` 的 45 处"静默跳过"改成 panic、删掉只覆盖模拟根的 session 用例。
  **这批测试此前一直在"空转"**：45 个用例的构造失败只打一行 "Chroot test skipped" 就算通过。
* **B = fork `64746d3`**（纯删除）：exec 的注入半段（-136 行）、`read_pt_interp` +
  `memfd_with_patched_interp`（-120 行）、chdir 的 `ReturnValue(0)` 尾巴、getcwd 的改写半段、
  模块头与 `landlock.rs` 的注释。保留：`/proc` 合成、策略/COV/账本、13 个 `legacy_*`、
  `reported_path_virtual`。
* **验收（本机镜像 `sandlock-dev-f17`，两次提交后都跑过）**：`core_lib` 922/0、`test_chroot`
  50/0、`test_instance_chroot` 10/0、`test_instance_exec` 17/0、`test_restore` 5/0、`test_procfs`
  14/0、`test_net_isolate` 25/0、`test_mediation_identity` 3/0、`ffi` 104/0、`supervise` 57/0、
  `supervise_cost` 3/0；`python` 从 pin 上的 446 passed / 19 failed 变成 **457 / 8**（回来的 11 条
  是 `test_fs_mount`；剩下 8 条与 pin 上逐条相同，是环境：`sandlock-supervise` 不在 PATH）。
* **两处环境事实（不是本次引入）**：① 门禁在这个 pin 上**本来就是红的**（`core_lib: baseline
  says 913, run produced 922`，stash 掉本次改动后原样复现）——`docs/test-baseline.md` 已把
  `core_lib`/`supervise` 两格按实测刷新并写明还有哪几格要等规范镜像；② 本机镜像跑不了
  `test_control`（缺 `sandlock` CLI）与 7 条 `test_supervise_channel`，所以 `core_integ` /
  `cli` / `python` 的整档绿要等规范镜像或 root 相位。
