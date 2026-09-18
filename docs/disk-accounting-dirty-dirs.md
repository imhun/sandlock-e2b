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
（正好等于 NFS 的成本单位），每轮只重扫脏目录；**低频整树对账保留**，但它兜的是**牢外写者与
带外写**（§3），不是"另一个节点在写"——运行中的沙箱，写只发生在它的宿主节点上。

## 1. 为什么是 Rust 侧，而不是 inotify

代码事实（`third_party/sandlock/crates/sandlock-core/src/sys/path_surface.rs`）：

* `:67 MEDIATED_PATH_SYSCALLS` 已含 `openat/openat2/open/truncate/rename*/renameat*/unlink*/
  unlinkat/rmdir/mkdir*/mkdirat/link*/symlink*` —— **"谁被以写方式打开"本来就要经过 mediator**
  （它必须解析路径才能做路径校验），插一个集合是 O(1)；
* `:368 NON_PATH_SYSCALLS` 含 `write/pwrite64/pwritev/ftruncate/fallocate/mmap/copy_file_range/
  sendfile` —— 所以**不拦写字节**，也就没有"给最热 syscall 加往返"的问题。

对比 inotify（§5.3 实测）：两者都随写者，但**inotify 的覆盖面在"牢外的同机写者"上更宽**
（§3 第 1 条：SDK 上传、命令日志、provision 物化 —— mediator 看不见，inotify 看得见），
代价是要多付 **2315 µs/目录**的 watch 建立与维护、`max_user_watches=58688`（per-uid）/
`max_user_instances=128` 的上限、以及 `max_queued_events=16384` 的丢事件风险。
**结论：愿意动 Rust 就选 mediator 脏集合（必须同时做 §3 的两个事件驱动点）；**
**inotify 在"坚决不动 Rust"时可用，而且它对牢外写者反而更全。**

## 2. 口径与不变式

* 口径仍是**存量**（当前占用），不是"写过的累计字节"——与 `diskMB`、与 `du` 可对账；
* 不变式：**本机写永远不会漏标**（标记点在已被拦的路径 syscall 上，粒度是父目录，重扫整目录
  ⇒ 不会多算），但**牢外写者与带外写**会让它偏，且**危险的方向是偏小**：
  - 牢外写者漏标（worker 代写：SDK 上传 / 命令日志）或带外增长 ⇒ `ledger < 真实值`
    ⇒ 该暂停没暂停（前者应当在**写点标脏**，后者靠对账纠）；
  - 带外删除/缩小 ⇒ 本机没标记 ⇒ `ledger` 停在旧值 ⇒ 偏大 ⇒ 可能**误暂停**（对账后自愈）；
  所以只标记**改变文件大小**的操作（`chmod/chown/utimensat/setxattr` 一律不标），并在属性测试里
  钉住"重扫结果 == 全树 walk"；
* 触发暂停的门槛与语义**完全复用已上线的 L2b**（实测 > `disk_size_mb` → pause，状态保留、
  预留释放，见 `control_plane/registry/manager.py::enforce_disk_budget`）。本文只换"怎么算量"。

## 3. 覆盖面与已知盲区（必须写进测试，不是注释）

1. **不在沙箱 mediator 里的写者**（同机，但**例行**）—— 这才是主盲区。

   运行中的沙箱，它**自己的**写都发生在宿主节点上（进程、NFS 客户端都在那台），所以"跨节点"
   不是主风险。真正的缺口是"**谁在写**"而不是"**哪台机器在写**"：worker 会**代沙箱写树**，
   而这些写**根本不经过沙箱的 seccomp 牢**，mediator 看不见：

   | 牢外写者 | 频率 | 依据（**实测判据**：看宿主 uid —— 沙箱进程写的文件是 `10000:…`，worker 代写的是 `0:65534`） |
   |---|---|---|
   | **SDK 文件上传**（`sb.files.write(...)`） | 每次上传 | 实测上传落到 `<tree>/home/user/upload.bin` 且属主 `0:65534`（= worker），沙箱内 `echo > cmd.txt` 是 `10000:10000`。代码：`envd_service/http/files.py:177-182`，worker 自己的进程 `stream_body_to_file` + `os.replace`；`files.py:30` 的 W6 注释明说 worker 是"用自己的 group 身份去够这些条目" |
   | **命令日志** `command-logs.jsonl` | **每条命令** | 实测属主 `0:65534`（worker），而沙箱自己写的文件是 `10000:10000`。代码：`envd_service/process/logs.py:36`，由 worker 的 `runtime/context.py:316` 实例化并 append |

   这两条都在**运行期**，所以必须在写点标脏；它们**不是**"沙箱自己没写"的推论，而是 uid 实测。
   （`sandbox.json` 同样是 `0:65534`，即 worker 写的。）

   处置**便宜且精确**：这些写点**全是我们自己的代码** ⇒ 在写点直接标脏（零成本、无需推断），
   比"靠对账兜"既准又快。**不要**把它们留给对账。

2. **账本基线的失效点**（同机，但不是"别人在写"）—— 迁移冷启动（`migrate_sandbox`，
   `sandboxes.py:2009`：共享卷下只切记录、目标重新 provision、进程不迁移）、worker 重启、
   分区后在另一节点重 provision：写者换了，**新宿主的 mediator/脏集合没有旧基线**。
   处置是**事件驱动**的：**在 provision 完成时做一次全树 walk 建基线**（窗口 = 0），而不是靠
   低频对账慢慢追。
   **注意这不是"漏记的写"**：建箱/迁移期的物化（快照 `copytree`、卷挂载点 symlink）发生在
   provision 之内、在基线之前，所以它不构成盲区——把它列进"牢外写者"是上一版的错误。
   真正需要记账的是**运行期**的牢外写者（§3 第 1 条那两条）。

3. **带外写**（末位，也是唯一可能真跨机的）—— 运维在别的 pod 里改数据、平台 GC。
   实测：worker-0 在 `.94` 写、watcher 在 `.140` 时 **0 事件**（文件确实存在）。
   这一类只能靠低频对账兜，但它罕见。

   **明确不是跨节点写的**（避免过度设计）：沙箱内进程的写（含 `exec`/命令，全部经由本机
   mediator）；快照 copytree 跑在源沙箱所在节点的 worker（`agent.py:2668`）、展开跑在新沙箱
   所在节点的 worker（`agent.py:2017-2021`），写的是 `_snapshots/`（不在树预算内）；删除/
   teardown 按 `node.address == "local://"` 分派，远端走 node agent（`sandboxes.py:1500-1502`）
   ⇒ 树由属主删；卷的数据在共享根下的 `_volumes/…`，**不在树预算内**，且天生被不同节点的多个
   沙箱同时挂载 ⇒ 它是另一条配额线。

   ⇒ 对账的定位因此**降级**为"兜带外写 + 防漂移"，间隔可以比 §4.2 建议的更松；真正必须做的是
   上面两个**事件驱动**的点（provision 建基线 + 写点标脏）。
4. **纯形状**（无 chroot/路径中介、只有 Landlock）根本没有路径中介 ⇒ 该形态必须回落整树 walk
   （`sandbox.rs:283-286` 的 `unsupported` 列表也说明这类开关在该形态下不可用）；
5. `ftruncate/fallocate/copy_file_range/sendfile/splice/mmap` 自身不被拦，但它们**都要求一个
   以写方式打开的 fd**（`ftruncate(2)` 对只读 fd 返回 EINVAL）⇒ 那次 `open` 已经把父目录标脏。
   **唯一例外是"fd 由外部注入"**（`fd_inject_*` 一类）：要么在注入点显式标脏，要么对注入过 fd
   的沙箱强制走对账；
6. **mmap 越 EOF 扩容在本存储上不可能**（NFS 上直接 `SIGBUS`，实测）⇒ 常被引用的"事件漏 mmap"
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
* **基线重建是事件驱动的**（§3 第 2 条）：`provision` 完成时、以及每次接管一棵已有树时
  （迁移冷启动、worker 重启后 reconcile 到本地 runtime）各建一次 —— 这样"换宿主"的窗口是 0，
  不需要靠对账去追；
* **牢外写者由写点自己标脏**（§3 第 1 条）：SDK 上传（`http/files.py`）与命令日志
  （`process/logs.py`）都调同一个 `mark_dir_dirty()`；这些是**我们自己的代码**，标脏精确且
  零成本，别留给对账。**provision 期的物化不需要标脏**——它在基线之前，由 §3 第 2 条的重建
  覆盖；
* 每轮（沿用 `E2B_DISK_ENFORCE_INTERVAL_S`，默认 30 s）：`drain_dirty_dirs()` → 只重扫脏目录
  （`os.scandir`，一个目录一次 readdirplus ≈ **2.4 ms**）→ 更新 ledger → 交给已上线的
  `enforce_disk_budget()`；
* **对账**（兜带外写 + 防漂移，§3 第 3 条）：`E2B_DISK_RECONCILE_INTERVAL_S`、以及溢出时各做一次
  全树 walk —— 复用今天已上线的 `RuntimeRegistry.disk_usage_snapshot` 逻辑。因为"跨节点"已被
  降级为"罕见的带外写"（§3 第 3 条），这个间隔**不必像 300 s 那么紧**（15–30 min 足够），先按
  保守值上线，再用 `overflow`/漂移计数观察；
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
   **新增三条按 §3 排布的用例**：① 牢外写者（SDK `files.write` 上传一个大文件、跑一条产生大量日志的
   命令）必须被记进账；② 迁移换宿主/worker 重启后**基线被重建**（不出现"漏算到下次对账"）；
   ③ 带外写（从另一个 pod 直接写树）断言"脏集合看不见、只有对账能发现"——把盲区变成测试；
5. **回归**：`deployment_smoke.py` + `multinode_smoke.py`；
   `E2B_DISK_ENFORCE_DIRTY=0` 时必须与今天行为一致（回落整树 walk）。

## 7. 风险与回退

* 牢外写者漏标（改成"写点不该标脏"）⇒ 账偏小 ⇒ 该暂停没暂停 ⇒ 靠写点标脏 + 对账纠；
* 带外增长 ⇒ 账偏小 ⇒ 只有对账能纠 ⇒ 对账间隔要写进用户可见文案（"最迟 N 分钟被暂停"）；
* 带外删除/缩小 ⇒ 账停在旧值 ⇒ 可能误暂停（对账后自愈，且暂停可恢复、不丢现场）；
* **顺手记一个与本方案无关、但同一轮测出来的既有行为**：SDK 的绝对路径是**相对树根**解析的
  （`gateway_common/paths.py:11`"Absolute user paths are treated as relative to the root"），
  所以 `sb.files.write("/home/user/x")` 落到 `<tree>/home/user/x`（沙箱里看到
  `/home/user/home/user/x`）。这与上游 E2B 的语义不同，值得单独排查（不影响配额账，但会让人
  以为"文件没写进去"）；
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
* 带外写（§3 第 3 条）看不见 ⇒ 对账仍是必需。

**但它在 §3 第 1 条那一类上有额外优势**：inotify 看的是**这台机器上的 VFS**，所以
**worker 代沙箱写**（SDK 上传、命令日志、provision 物化）它**看得见**，而 mediator 脏集合
看不见（那些写不在牢里）。这一点把 inotify 从"更贵的同覆盖面"抬成"覆盖面更全的备选"——
代价仍是 §5.3 的 watch 建立成本/上限/丢事件。

### 10.4 建议的组合与顺序

1. **A（fork 侧，几行）**：立刻拿到"单文件不可能超预算"的**硬**边界，专治最常见的跑飞；先把
   `max_disk` 从"死参数"变成有含义，代价是 EFBIG/SIGXFSZ 语义与卷/tmp 口径要写清；
2. **C（worker 侧，Python）**：拿到"正在增长的文件"的实时数字，成本 0.01% 单核，不动 Rust；
3. **B（fork 侧，本文主体）**：拿到"树级准确 + 已关闭文件"的账，才真正把每轮成本从 O(目录数)
   降到 O(脏目录数)；落地时必须同时做 §3 的**两个事件驱动点**（provision 建基线 + 牢外写点
   自己标脏），否则盲区会比现在想的更大；
4. **对账（降级为安全网）**：只兜带外写（运维/GC）与漂移 ⇒ 间隔可放到 15–30 min。

口径提醒：A 是**硬**（内核 EFBIG，逐次写生效），B/C 是**软**（发现后暂停）。若产品口径要
"配额 = 写不进去"（ENOSPC 类），只有 A（单文件维度）与 L3 的存储侧硬边界（目录配额 / loop
镜像）能给；B/C 给的是"软闸门 + 可解释的暂停"。

还有一条由此推出的差别：**A 不受 §3 的任何账本盲区影响**（牢外写者、带外写、基线失效都不影响
它）—— 它不看账本，内核在任何节点、任何写者上一致地执行；B/C 的盲区代价是"写者/账本重建后开的
那个窗口"。

## 11. 跨机写不是"罕见场景"，是缺一层边界（安全）

安全侧完整定性见 `docs/security-audit/findings.md` 的 **OBS-9**（并已挂到
`attack-surface.md` 的 C 层）。这里只记与配额账相关的结论：

> **状态（2026-09-18）**：OBS-9 的建议 ①（uid 权威搬到 `SandboxRecord`/Redis）与 ②（控制面
> 最小权限挂载）**已落地并在集群验证**，细节见 findings 的 OBS-9。对本文的影响：那两条链
> （改 `sandbox.json` 拆掉跨 uid 墙、控制面改任意沙箱树）都已关闭；剩下的 ③ uid 审计与
> ④ NAS 权限组确认仍未做，卷切片归属的权威仍在卷内。

**实测现状**（2026-09-18）：`deploy/k8s-k0s/storage-nas.yaml` 的 `: /sandlock` 被**控制面与两个
worker 都以 root 挂载**，两节点分别实测 `WRITE OK as 0:0` / `WRITE OK as 0:65534`，卷根是
`drwxrwxrwt` ⇒ **机器级没有任何边界**，只有进程级（沙箱 host uid + Landlock）。

**定性**：

* 对**本轮攻击者模型**（"已能在沙箱内执行任意代码"）**不是洞** —— C 层已干净（host uid 独立、
  Landlock 白名单、`0770` 只给 worker gid）；
* 对**集群内 root** 是缺纵深防御，而且有一条具体链：OBS-4 的修法让 fleet 级 uid 分配以
  **卷内文件** `sandbox.json` 的 `host_uid` 为权威（`uid_pool.py:_recorded_uids`，
  "on any worker sharing the workspace"），该文件属主 `0:65534`（root 可改）⇒ 可让两个沙箱
  **共用 host uid**，拆掉第二道墙；
* **卷上 root squash 与"每沙箱 host uid"目前互斥**：E3.2 需要 worker 对卷 `chown`
  （`uid_pool.py:apply_sandbox_ownership`），而 squash 是服务端行为，客户端再特权也 EPERM。

**对本文的影响**：

1. 只要不加边界，**低频对账就必须保留**（它不是"兜罕见场景"，而是兜一个确实存在的写者类别）；
2. 若采纳 OBS-9 的建议 ③（在已有周期扫描里加 **uid 审计**：树内出现非本沙箱 uid / 非 worker gid
   的属主即告警），这套扫描同时就是**越界写的检测器** —— 成本几乎为零，因为 walk 已经存在；
3. 若采纳建议 ①（把 `host_uid`/卷切片归属的权威搬到 `SandboxRecord`/Redis），则"脏目录账"
   依赖的树内元数据也不再是信任来源，两条线（配额与安全）在这里合流。
