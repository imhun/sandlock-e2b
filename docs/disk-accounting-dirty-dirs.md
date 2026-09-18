# 沙箱磁盘记账：mediator 脏目录方案（已记录，待实施）

> 状态：**设计已记录，未实施**。前置事实见 `docs/disk-quota-options.md` §5.1（statfs）、
> §5.2（整树 walk 成本）、§5.3（inotify / COW / mmap 实测）。
> 现行上线形态是"周期性整树 walk"（`docs/k8s-deployment.md` §21.1）。
> 本文只回答一件事：**要把"每轮整树 walk"降级为"只重扫脏目录"，该动哪里、代价是什么、
> 哪些盲区必须留着对账兜。**

## 0. 一句话

脏信号从**已经被拦的**路径 syscall 上顺手取（零新增陷阱、零 Python 回调），粒度取**父目录**
（正好等于 NFS 的成本单位），每轮只重扫脏目录；**低频整树对账保留**，因为跨节点写看不见。

## 1. 为什么是 Rust 侧，而不是 inotify

代码事实（`third_party/sandlock/crates/sandlock-core/src/sys/path_surface.rs`）：

* `:67 MEDIATED_PATH_SYSCALLS` 已含 `openat/openat2/open/truncate/rename*/renameat*/unlink*/
  unlinkat/rmdir/mkdir*/mkdirat/link*/symlink*` —— **"谁被以写方式打开"本来就要经过 mediator**
  （它必须解析路径才能做路径校验），插一个集合是 O(1)；
* `:368 NON_PATH_SYSCALLS` 含 `write/pwrite64/pwritev/ftruncate/fallocate/mmap/copy_file_range/
  sendfile` —— 所以**不拦写字节**，也就没有"给最热 syscall 加往返"的问题。

对比 inotify（§5.3 实测）：覆盖面**一样只到本机**（跨节点写 0 事件），但要多付
**2315 µs/目录**的 watch 建立与维护、`max_user_watches=58688`（per-uid）/
`max_user_instances=128` 的上限、以及 `max_queued_events=16384` 的丢事件风险。
**结论：愿意动 Rust 就选 mediator 脏集合；inotify 只在"坚决不动 Rust"时才考虑（而且只能当提示）。**

## 2. 口径与不变式

* 口径仍是**存量**（当前占用），不是"写过的累计字节"——与 `diskMB`、与 `du` 可对账；
* 不变式：**脏集合只可能漏标，不会多标** ⇒ `ledger ≤ 真实值`，漏的部分靠对账补；
  反过来"多标"会导致**误暂停**，所以只标记**改变文件大小**的操作（`chmod/chown/utimensat/
  setxattr` 一律不标）；
* 触发暂停的门槛与语义**完全复用已上线的 L2b**（实测 > `disk_size_mb` → pause，状态保留、
  预留释放，见 `control_plane/registry/manager.py::enforce_disk_budget`）。本文只换"怎么算量"。

## 3. 覆盖面与已知盲区（必须写进测试，不是注释）

1. **跨节点写看不见**（实测：worker-0 在 `.94` 写、watcher 在 `.140`，事件数 = 0，而文件确实
   存在）⇒ 迁移、快照展开、邻居 worker、控制面直接写这棵树，脏集合**一律不知道**。
   ⇒ **低频整树对账是必需项，不是优化项**；
2. **纯形状**（无 chroot/路径中介、只有 Landlock）根本没有路径中介 ⇒ 该形态必须回落整树 walk
   （`sandbox.rs:283-286` 的 `unsupported` 列表也说明这类开关在该形态下不可用）；
3. **同机非 mediator 写者**（worker/CP/GC/快照展开）不在脏集合里 ⇒ 对账兜；
4. `ftruncate/fallocate/copy_file_range/sendfile/splice/mmap` 自身不被拦，但它们**都要求一个
   以写方式打开的 fd**（`ftruncate(2)` 对只读 fd 返回 EINVAL）⇒ 那次 `open` 已经把父目录标脏。
   **唯一例外是"fd 由外部注入"**（`fd_inject_*` 一类）：要么在注入点显式标脏，要么对注入过 fd
   的沙箱强制走对账；
5. **mmap 越 EOF 扩容在本存储上不可能**（NFS 上直接 `SIGBUS`，实测）⇒ 常被引用的"事件漏 mmap"
   在这里不成立。**但换存储/换挂载选项后必须重新验证**，这条是环境事实不是普适事实。

## 4. 设计

### 4.1 Rust（`sandlock-core`）

* **落点**：`seccomp/dispatch.rs` 的 handler 链上新增一个 builtin（排在 `cow` 之后），在路径
  解析完成处挂；**不改 `seccomp_plan.rs`**（计划表不变 ⇒ 零新增陷阱，安全姿态不变）；
* **状态**：每 branch 一份 `DirtyDirs { dirs: HashSet<DirKey>, overflow: bool }`；
  `DirKey` 用**已经解析好的**父目录（相对根），不额外发 syscall；
* **标记点**：write-intent `openat/openat2/open`（`WRITE_FLAGS = O_WRONLY|O_RDWR|O_CREAT|
  O_TRUNC|O_APPEND`，该常量在 `cow/seccomp.rs:24` 已有先例）、路径 `truncate`、
  `rename*/renameat*`（**两侧**父目录）、`unlink*/unlinkat/rmdir`（父目录）、
  `mkdir*/mkdirat`（父目录）、`link*/symlink*`（父目录）；
* **上限**：`dirs.len() > MAX_DIRTY`（建议 4096）⇒ `overflow = true` 并清空集合，让上层对该沙箱
  做一次全树 walk；避免内存无界与"一次 `cp -r` 10 万文件"的风暴；
* **导出**：`sandlock-ffi/src/lib.rs` 增加 drain 语义的 C ABI（读走即清零），
  Python wrapper 加 `Sandbox.drain_dirty_dirs()`；
* **兼容**：老 mediator 没有该符号 ⇒ Python 侧**能力探测**后回落整树 walk；
  行为开关 `E2B_DISK_ENFORCE_DIRTY`（**默认关**，先观察）。

### 4.2 Python（worker / `envd_service`）

* `DirLedger`：`{目录相对路径: bytes}` + `total`；`baseline()` 建一次（一次整树 walk，
  或直接复用现有 `priv_helpers.dir_size` 口径）；
* 每轮（沿用 `E2B_DISK_ENFORCE_INTERVAL_S`，默认 30 s）：`drain_dirty_dirs()` → 只重扫脏目录
  （`os.scandir`，一个目录一次 readdirplus ≈ **2.4 ms**）→ 更新 ledger → 交给已上线的
  `enforce_disk_budget()`；
* **对账**：`E2B_DISK_RECONCILE_INTERVAL_S`（默认 300 s）、`overflow`、以及进程重启后各做一次
  全树 walk —— 复用今天已上线的 `RuntimeRegistry.disk_usage_snapshot` 逻辑，只是调度频率下降；
* 内存：524 目录 × ~80 B ≈ **42 KB/沙箱**，100 沙箱 ≈ 4 MB，可忽略。

### 4.3 备选：不动 Rust（用现成 Python handler 钩子）

`third_party/sandlock/docs/python-handlers.md` 是**已支持的**扩展点（`Handler.handle(ctx)`、
`COMMON_PATH_SYSCALLS`，链在 builtins 之后，`NotifAction::Continue` 即穿透）。
可以在 Python 里维护同一份脏集合，**零 Rust 改动**，代价是每个写 open 一次 Python 回调
（GIL/通道开销），且必须严格保证穿透 builtin 的语义不变。
用途：先验证"脏目录记账"的收益，再决定要不要下沉到 Rust。

## 5. 预期收益（基于 §5.2 实测的 venv 形状：524 目录 / 3 446 文件）

| 场景 | 今天（整树 walk） | 脏目录方案 |
|---|---|---|
| 稳态（这一轮改了几个文件） | 1.27 s / 轮 | **2.4 ms / 轮** |
| `cp -r` 3 446 个文件（约 100 个脏目录） | 1.27 s / 轮 | 240 ms / 轮 |
| 随树增长 | **线性** | 不随树增长（只随"改了哪些目录"） |

这也是它相对"风险排序"（`docs/disk-quota-options.md` §7 L2b 备选）的本质区别：
风险排序只是把有风险的树排前面，**单棵重树的成本不变**。

## 6. 测试计划

1. **Rust 单测**：每个标记点各一条；`rename` 两侧都标；`mkdir` 标父目录；
   `chmod/chown/utimensat` **不**标；`MAX_DIRTY` 溢出置 `overflow`；
2. **属性测试**：随机 syscall 序列（`write/ftruncate/rename/unlink/mkdir`）后，
   "脏集合重扫"得到的总量必须**逐字节等于**全树 walk（0 容差）；
3. **Python 集成**：脏重扫 == 全扫；`overflow` 后触发对账；重启后重建 baseline；
4. **集群验收**（沿用上一轮脚本）：单沙箱写 1200 MiB → ≤30 s 内 `paused`；
   **新增跨节点写用例**，断言"脏集合看不见、只有对账能发现"——把盲区变成一条测试；
5. **回归**：`deployment_smoke.py` + `multinode_smoke.py`；
   `E2B_DISK_ENFORCE_DIRTY=0` 时必须与今天行为一致（回落整树 walk）。

## 7. 风险与回退

* 漏标 ⇒ 账偏小 ⇒ 只有对账能纠 ⇒ 对账间隔要写进用户可见文案（"最迟 N 分钟被暂停"）；
* 误标 ⇒ 多算 ⇒ 误暂停 ⇒ 用"只标改变大小的操作"约束，并在属性测试里钉住；
* 回退：关开关即回到今天已上线的整树 walk，**无数据迁移**。

## 8. 涉及文件（实施时的改动面）

| 层 | 文件 |
|---|---|
| Rust 核心 | `third_party/sandlock/crates/sandlock-core/src/seccomp/dispatch.rs`、`.../sys/path_surface.rs`（分类常量与测试同步） |
| FFI/Python 包 | `third_party/sandlock/crates/sandlock-ffi/src/lib.rs`、`third_party/sandlock/python/`（drain API） |
| worker | `envd_service/executors/sandlock.py`（开关 + 能力探测）、`envd_service/runtime/registry.py`（`DirLedger` 接进扫描调度） |
| 控制面 | 无需改动（`enforce_disk_budget` 已上线，输入从"实测字节"变为"ledger 字节"） |
| 测试 | Rust `cargo test`；`tests/unit/test_sandbox_disk_enforcement.py`（扩）、`tests/contract/` |
| 文档 | 本文、`docs/disk-quota-options.md`（§5.3 证据 / §7 指针）、`docs/task-backlog.md` N25 |

## 9. 与其它路线的关系

* **硬 ENOSPC** 仍只有存储侧硬边界能提供（NAS 目录配额 / 每沙箱 loop 镜像），见
  `docs/disk-quota-options.md` §3 / §4.1；本文解决的是**软闸门的可伸缩性**，不改变
  "超限暂停而不是 ENOSPC"的语义；
* **COW**（`docs/sandbox-disk-quota.md` §1.1.1）不能替代它：COW 的账本本身就是
  "每次写 open 整树 `recalc_disk_used()`"（`cow/seccomp.rs:1085`/`:1161`），是把 walk 搬到
  最热路径上，比现在的 30 s 一次更贵；而且它在 E2B 形态下根本没被激活 —— 门槛是
  `!no_supervisor && workdir.is_some()`（`sandbox.rs:2078`），我们发 `max_disk` 但从不发
  `workdir` —— 强制点也在 open 而不在字节。
