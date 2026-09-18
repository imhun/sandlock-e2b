# 沙箱磁盘记账：mediator 脏目录方案（已记录，待实施）

> 状态：**设计已记录，未实施**。前置事实见 `docs/disk-quota-options.md` §5.1（statfs）、
> §5.2（整树 walk 成本）、§5.3（inotify / COW / mmap 实测）。
> 现行上线形态是"周期性整树 walk"（`docs/k8s-deployment.md` §21.1）。
> 本文只回答一件事：**要把"每轮整树 walk"降级为"只重扫脏目录"，该动哪里、代价是什么、
> 哪些盲区必须留着对账兜。**
> §10 是**探索**：fork 侧另外两条更便宜的原语（A：`RLIMIT_FSIZE` 零成本硬限单文件；
> C：worker 侧纯 `/proc` 采样打开的写 fd，实测 86 µs/次），以及它们与本文主体的关系。

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
* 不变式：**本机写永远不会漏标**（标记点在已被拦的路径 syscall 上，粒度是父目录，重扫整目录
  ⇒ 不会多算），但跨节点写两个方向都会偏，且**危险的方向是偏小**：
  - 跨节点**增长** ⇒ 脏集合不知道 ⇒ `ledger < 真实值` ⇒ 该暂停没暂停（靠对账纠）；
  - 跨节点**删除/缩小** ⇒ 本机没标记 ⇒ `ledger` 停在旧值 ⇒ 偏大 ⇒ 可能**误暂停**（对账后自愈）；
  所以只标记**改变文件大小**的操作（`chmod/chown/utimensat/setxattr` 一律不标），并在属性测试里
  钉住"重扫结果 == 全树 walk"；
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

* 跨节点增长（漏标）⇒ 账偏小 ⇒ 只有对账能纠 ⇒ 对账间隔要写进用户可见文案
  （"最迟 N 分钟被暂停"）；
* 跨节点删除/缩小 ⇒ 账停在旧值 ⇒ 可能误暂停（对账后自愈，且暂停可恢复、不丢现场）；
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

## 10. 探索：fork 侧还有没有更便宜的记账原语

结论先行：**有两条比"改 dispatch 链"更便宜的路（A 零成本、C 零 fork 改动），但都不替代树级
对账**。四种原语按"每轮成本"排：

| 原语 | 挂在哪 | 每轮成本 | 覆盖 | 强制力 |
|---|---|---|---|---|
| **A. `RLIMIT_FSIZE = diskMB`** | `context.rs` 的 child setup（与 `RLIMIT_CORE:585`、`RLIMIT_NOFILE:1017` 同处） | **0**（一次 `setrlimit`，之后内核执行） | **单个文件**不可能超过整树预算 ⇒ 实测那种 `dd bs=1M count=1200` **当场 EFBIG** | **硬**（内核，非"30 s 后暂停"） |
| **B. 脏目录集合**（本文主体） | `seccomp/dispatch.rs` 链上新增 builtin | 0 新增陷阱 | 本机全部写路径的**目录** | 软（暂停） |
| **C. 采样"打开的写 fd"** | **worker 侧即可，零 fork 改动** | **86 µs/次采样**（实测，含 Python 开销） | **此刻正在增长的文件** | 软 |
| **D. 每次 write 记账** | 把 `write/pwrite*` 与 `MEDIATED_PATH_SYSCALLS` 同级 | per-write 往返 | 完整 | 硬（可返回 ENOSPC） |

### 10.1 为什么 fork 的"按标量记账"范式不能直接套到磁盘上

fork 已有的 `ResourceState`（`seccomp/state.rs:12`）是**低成本记账的教科书范式**：内存只在
`mmap`/`brk` 上按**标量长度参数**记账，进程数只在 `clone`/`wait` 上记 —— 从不在数据通路上计数。
磁盘要找同类"标量尺寸参数"的 syscall，结果是：

| syscall | 尺寸参数 | 今天是否被拦 |
|---|---|---|
| `truncate(path, len)` | `len` 标量 | ✅ 已拦 |
| `ftruncate(fd, len)` | `len` 标量 | ❌ 未拦 |
| `fallocate(fd, mode, off, len)` | `len` 标量 | ❌ 未拦 |
| `write(fd, buf, count)` | `count` 标量 | ❌ 未拦（且最热） |

⇒ 范式**不免费迁移**：唯一"标量尺寸且已被拦"的是路径版 `truncate`。这正是 A/C 这两条
"不碰数据通路"的路值得单独记录的原因。

### 10.2 A：`RLIMIT_FSIZE`（免费 + 内核强制，今天完全没用上）

* 事实：fork 只设了 `RLIMIT_CORE` 与 `RLIMIT_NOFILE`，**从未设 `RLIMIT_FSIZE`**；policy 里的
  `max_disk` 目前只被 COW 消费，而 COW 在本形态下未激活（§9）⇒ **`max_disk` 在今天的生产形态里
  没有任何强制力**。A 等于给它一个内核级含义；
* 值取 **= `diskMB`（树预算）**，不能更小：它只禁止"单个文件大于整棵树的预算"，而这种文件本身
  就必然超预算 ⇒ **对树口径永不误伤**；
* 代价 1：**它是 per-process 而不是 per-tree**。同一个上限也作用在**沙箱写的任何文件**上
  （挂进来的卷、沙箱内 `/tmp`、rootfs overlay）—— 若某卷的 per-sandbox 配额大于 `diskMB`，
  就会出现"合法写被 EFBIG"。处置：`RLIMIT_FSIZE = max(diskMB, 卷配额)`，或对挂了卷的沙箱不开 A；
* 代价 2：触发时内核发 `SIGXFSZ`，**默认动作是杀进程**；要贴近 ENOSPC 的体验需在沙箱内把
  `SIGXFSZ` 置为忽略（此时 `write` 返回 `EFBIG`）。两种语义都要写进 API 文档；
* 只能在**沙箱子进程**上设：worker/envd 自身要能写模板 copytree / 快照展开 / 镜像缓存（远超
  `diskMB`），一旦被这个 rlimit 限制会直接坏掉。

### 10.3 C：采样"打开的写 fd"（零 fork 改动，直接命中跑飞形态）

跑飞的特征是"**一个打开的写 fd 一直在长**"（实测的 `dd` 正是这样）。而"谁被以写方式打开"这件事
fork 本来就知道，且它**已经有按 child fd 键控的 per-process 状态**：
`PerProcessState { cow_dir_cache: HashMap<u32, …>, procfs_dir_cache: HashMap<(u32, String), …> }`
（`seccomp/state.rs:142`，自带 fd 复用失效语义）；`pidfd_getfd` 也已是常用工具
（`context.rs` / `network/connect.rs` / `port_remap.rs`）。

但有更省的做法 —— **worker 侧纯 `/proc`，一行 fork 代码都不用改**：

```
对沙箱的每个进程：
  /proc/<pid>/fdinfo/<fd> 里 flags 含 O_WRONLY|O_RDWR   → 这是写 fd
  os.stat("/proc/<pid>/fd/<fd>").st_size                → 它现在的尺寸
```

实测成本：**86 µs/次采样**（Python，含枚举与解析；21.6 µs/fd，Rust 侧是 µs 级）。
1 Hz 采样对单核是 0.01%。它天然覆盖 `ftruncate`/`fallocate`/`copy_file_range`/`sendfile`
的结果 —— 因为无论哪个 syscall 改的，都体现为这些 fd 的 size。

* 只能看见"**此刻打开的**"：写完就关的文件要靠 B 或对账；
* 需要按路径记住"上次采样时的尺寸"才能算增量（只保留当前/近期打开的少量条目，不需要全树
  per-file 表）；
* 跨节点写照样看不见 ⇒ 对账仍是必需。

### 10.4 建议的组合与顺序

1. **A（fork 侧，几行）**：立刻拿到"单文件不可能超预算"的**硬**边界，专治最常见的跑飞；先把
   `max_disk` 从"死参数"变成有含义，代价是 EFBIG/SIGXFSZ 语义与卷/tmp 口径要写清；
2. **C（worker 侧，Python）**：拿到"正在增长的文件"的实时数字，成本 0.01% 单核，不动 Rust；
3. **B（fork 侧，本文主体）**：拿到"树级准确 + 已关闭文件"的账，才真正把每轮成本从 O(目录数)
   降到 O(脏目录数)；
4. **对账（不变）**：A/B/C 都看不见跨节点写 ⇒ 低频整树 walk 始终保留。

口径提醒：A 是**硬**（内核 EFBIG，逐次写生效），B/C 是**软**（发现后暂停）。若产品口径要
"配额 = 写不进去"（ENOSPC 类），只有 A（单文件维度）与 L3 的存储侧硬边界（目录配额 / loop
镜像）能给；B/C 给的是"软闸门 + 可解释的暂停"。
