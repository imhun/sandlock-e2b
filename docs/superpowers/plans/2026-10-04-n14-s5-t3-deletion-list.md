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
- S5 的 E2B 侧（T1/T2/T4 已完成，见 `docs/open-issues.md` N14 行与提交 `b8d9d72`/`d8bf694`/`1478a3c`）。
