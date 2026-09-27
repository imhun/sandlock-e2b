# COW 路线可行性探针（route-B 形态实测）

> 任务：把「COW 的 `max_disk` 到底能不能用」用实测钉死，而不是停在推理。
> 约束：未改任何产品代码、未提交、未推送、未动线上；全部在本机一次性容器内跑完并清理
> （`cowprobe-worker:local` 镜像 + `cowprobe-ws` 卷已删除，`docker ps -a`/`volume ls`/`images` 无残留）。

## 结论（先行）

1. **现在还不能用** —— 在 route-B 生产形态下，policy 里的 `max_disk` **不产生任何强制**：
   沙箱在 `max_disk = 8M` 下**一次 open 写入 256 MiB 成功**，写完后目录里也没有任何 COW
   upper（`/tmp/sandlock-cow-10001` 不存在），因为**分支根本没被创建**。
2. **缺口是一个字段，不是一条路径**：serve 路径**确实**会走「建 `SeccompCowBranch`」的代码，
   但那段代码的前置条件是 `workdir` 存在（`crates/sandlock-core/src/sandbox.rs:2024`），
   而 COW 特性开关本身就是 `cow: sandbox.workdir.is_some()`（`crates/sandlock-core/src/resolved.rs:83`）。
   E2B 的 policy ceiling **从来不设 `workdir`（也不设 `fs_storage`）**
   ——`envd_service/executors/sandlock.py:1261 _policy_ceiling()`，`max_disk` 在 `:1346` 照发。
   我把 `workdir` 注入同一份 ceiling 后，**同一条 serve 路径立刻建分支并开始返回 ENOSPC**（对照臂）。
3. **能用在哪一层**：`max_disk` 的强制点是**写 open**（`open/openat` 带写标志时校准并判定），
   不是内核分配路径。所以它**做不到「最多占 X」**，只能做到「下一次 open 之前还能再写多少」；
   它是一份**按代次（per-generation）重置的增量预算**，不是存量配额。
4. **当配额用的旁路（实测不拦）**：单次 open 之后的 `write`/`pwrite`/`ftruncate`/`mmap` 写回/
   `fallocate`/`O_DIRECT`/稀疏文件/**任何子进程**/**静态 C 与静态 Go 程序**全部照写不误
   （实测 64 MiB，最高单 open 256 MiB），只有**下一个 open** 才拿到 ENOSPC；
   另外 `rename`/`link` 不记账，**挂进来的共享卷完全不在账内**（§7 的卷排除，实测 64 MiB 直接写穿且 gate 仍开）。
5. **混合路线（COW 管事务、quota 管配额）现在不值得做**：COW 的账本在内存、按代次归零、
   默认落点在**节点本地 `/tmp`**（跨 worker 看不见、丢数据），
   而要让它在生产形态下真的生效，得先改 `workdir`/`fs_storage` 语义并解决 chroot 路径映射
   ——成本远高于已经生效的 XFS project quota，收益只是「事务性」这一项非刚需。

一句话：**COW `max_disk` 今天在生产形态下是死参数（写 256 MiB 都不拦）；即使接上，它也只能做「序列化后的写前预算」，做配额会被单次 open、共享卷和跨代重置三条路直接绕过 —— 混合路线不建议做。**

---

## 0. 探针环境（生产形态，非 root 糊不过去）

全部步骤都用**生产 worker 镜像**（`deploy/docker/Dockerfile.envd` + `wheels/fork/` 当前 tip 轮子）跑：

| 项 | 实测值 | 证据 |
|---|---|---|
| 镜像内 sandlock | 0.9.0-beta（cp314）× 轮子 sha256 `7c17fa1f…` | `cowprobe-02-image-verify.log` |
| 镜像内 `sandlock-supervise` | sha256 `a2e469ff…` == 宿主 `SHA256SUMS.supervise`（HEAD `a063daf`） | 同上 |
| broker | `e2b-slot-spawn cap_setgid,cap_setuid=ep`、`e2b-maint cap_chown,cap_dac_override=ep` | 同上 |
| 容器身份 | `uid/euid 65534`、`CapEff=0000000000000000`、**`CapBnd=00000000000000c3`**、`NoNewPrivs=0` | `cowprobe-01-smoke.log` |
| 槽位身份 | `sandlock-supervise --uid 10001`，`Uid: 10001 10001 10001 10001`；沙箱内 `uid=0`（userns self-map） | `cowprobe-03-activation.log` |

容器起法与生产 compose 逐项对齐：`--user 65534:65534`、`--cap-drop ALL` +
`SETUID,SETGID,CHOWN,DAC_OVERRIDE`、`--security-opt seccomp=unconfined`、
`E2B_WORKSPACE_BASE=/var/lib/e2b-sandboxes`、`E2B_ROUTE_B_TMP_ROOT=/var/lib/e2b-sandboxes/.route-b`、
`E2B_PRIV_HELPERS=auto`、`E2B_PER_SANDBOX_UID=true`、uid 池 10001+、`E2B_ROUTE_B=on`。
驱动代码走的是**产品自己的路径**：`Settings` → `priv_helpers.configure_priv_helpers` → `W1SlotPool`
（`spawner=helpers.slot_spawner`，即 file-cap broker）→ `SandlockExecutor`（`_policy_ceiling()` 出 policy）→
`supervise_policy_document` → 槽位。

**唯一的人工差异**（对照臂，probe_lib 里显式标注）：`workdir` / `fs_storage` 两个字段由探针注入；
生产 ceiling 从不包含它们（这就是本报告要测的那个缺口）。

沙箱形态说明：探针跑的是**纯形态**（`base_image=None`，无 chroot），因为 COW 的开关
（`workdir.is_some()`）与 chroot 无关，而 chroot 形态需要额外的镜像 rootfs 准备；
chroot 下的路径映射**未实测**，作为开放项列在 §7。

---

## 1. 能不能激活（最高优先）——**不能**

### 1.1 生产 ceiling 里 `max_disk` 发了，`workdir` 没发（槽位自己收到的文档为准）

```
slot 的 policy.json（W1SlotPool 写盘、槽位实际读的那份）：
{ "fs_writable": ["/var/lib/e2b-sandboxes/cowprobe-a/workspace"],
  "fs_readable": ["/usr","/lib","/bin","/opt"], "fs_denied": [],
  "net_allow": [], "net_deny": [], "http_allow": [], "http_inject": [],
  "max_memory": "512M", "max_processes": 64, "max_open_files": 1024,
  "max_cpu": 100, "max_disk": "8M", "uid": 10001, "gid": 10001 }     <- 没有 workdir / fs_storage
```

`max_disk` 确实**落到了 builder**（`builder max_disk (landed): 8M`），而且这一步有硬校验：
`sandlock-supervise/src/policy.rs:513-516` 应用字段、`:1136-1145` 若没落地就按名拒绝启动。
启动门是活的（负向对照，`cowprobe-09-verify.log`）：

```
policy {"max_disk":"8M","no_such_field":1}  -> rc=1  policy contains unknown field(s): `no_such_field`
policy {"max_disk":"not-a-size"}            -> rc=1  policy field `max_disk`: invalid byte size: not-a-size
policy {"max_disk":"8M"}                    -> rc=0
```

⇒ 槽位能起来，就证明 `max_disk` 一路落到了 `Sandbox.max_disk`。**问题不在 wire，也不在 landing。**

### 1.2 分支没被创建：8 MiB 配额下写 256 MiB 成功

arm A（**生产 ceiling 原样**：`max_disk=8M`、无 `workdir`）：

```
--- 64 MiB single-open write (python) under max_disk=8M: exit=0
  stdout: wrote 67108864
--- next write-open after that write: exit=0
  stdout: OPEN_OK
COW storage bases that exist: []
workspace entries after close: ['big.bin', 'probe_after.bin']      # 直接落在共享 workspace 里
```

再看 step 04 的 `huge_write`（同一形态、COW 开关打开但配额为 0 时同样不拦；
`cow8` 臂下 256 MiB 也是 `RESULT=ok`，只有 gate 变 ENOSPC）：

```
huge_write   cow8  case=ok  gate_open_BLOCKED  DETAIL=written=268435456 huge.bin=268435456
```

### 1.3 serve 路径**确实**会建分支 —— 只是被 `workdir` 卡住（对照臂实测）

arm B = 同一份 ceiling **只加 `workdir`**（其余逐字节相同，同一个 serve 路径）：

```
--- 64 MiB single-open write (python) under max_disk=8M: exit=1
  stderr: OSError: [Errno 28] No space left on device: 'big.bin'
COW storage bases that exist: ['/tmp/sandlock-cow-10001']          # 分支出现了
```

代码路径（serve 路径与一次性 `Sandbox::run/spawn` **共用同一段**）：

```
sandlock-supervise/src/serve.rs:181   Generation::launch_first -> SandboxInstance::launch_exec_with_lifetime
sandlock-core/src/instance.rs:679     launch_exec_with_lifetime -> launch_exec_inner
sandlock-core/src/instance.rs:535     create_with_in_child_main -> policy.do_create(...)
sandlock-core/src/sandbox.rs:1843     do_create -> do_create_stdio
sandlock-core/src/sandbox.rs:2024      } else if !no_supervisor && self.workdir.is_some() {   <-- 门槛
sandlock-core/src/sandbox.rs:2028          SeccompCowBranch::create(&workdir, storage, max_disk)
sandlock-core/src/resolved.rs:83       cow: sandbox.workdir.is_some()                       <-- 特性开关同源
```

**缺口就在 `sandbox.rs:2024` 这个 `workdir.is_some()` 条件**，而它上游的 E2B 侧缺口是
`envd_service/executors/sandlock.py:1261 _policy_ceiling()` 从不输出 `workdir`/`fs_storage`
（route-B wire 本身是收这两个字段的：`envd_service/route_b.py:813`、`policy.rs:609-614`）。
也就是说：**没有任何 "只有 run/spawn 才建" 的隐藏分支**；缺的是那个字段。

---

## 2. 拦得住哪些写路径（`max_disk = 8M`，对照臂 COW 开 + 限额 0）

每格 = 同一 case 在三个臂下的结果：`plain`=生产 ceiling（无 COW）、`cow0`=COW 开 + 无限额、
`cow8`=COW 开 + 8 MiB。`gate` = 该 case 之后**再开一个写 open** 的结果（ENOSPC 说明账已经超了）。
每个 case 都是**全新沙箱 + 全新分支**。日志：`cowprobe-04-writematrix.log`（`### case=…` 段落有原始输出）。

| 写路径 | plain | cow0 | cow8（目标写入） | gate（下一次 open） | 判定 |
|---|---|---|---|---|---|
| `write` 64 MiB（单 open） | ok | ok | **成功 67108864** | **ENOSPC** | 只在下次 open 拦 |
| 单 open 写 256 MiB | ok | ok | **成功 268435456** | **ENOSPC** | 每次可越额一整段 |
| `O_APPEND` 追加 64 MiB | ok | ok | **成功 68157440** | **ENOSPC** | 同上 |
| 路径 `truncate` 放大到 64 MiB | ok | ok | **ENOSPC（文件仍 1 B）** | ok | **拦** |
| `ftruncate` 放大到 64 MiB | ok | ok | **成功 67108864** | **ENOSPC** | 不拦 |
| `pwrite` 逐块写 64 MiB | ok | ok | **成功 67108864** | **ENOSPC** | 不拦 |
| `mmap` 写回 64 MiB | ok | ok | **成功 67108864** | **ENOSPC** | 不拦 |
| `fallocate` 64 MiB | ok | ok | **成功 67108864** | **ENOSPC** | 不拦 |
| `copy_file_range` 64 MiB | ok | ok | 源文件 64 MiB ok，**目的 open 直接 ENOSPC** | **ENOSPC** | 拦在第二个 open |
| `sendfile` 64 MiB | ok | ok | 源文件 ok，**目的 open ENOSPC** | **ENOSPC** | 拦在第二个 open |
| `rename` 进 upper（源已在 upper） | ok | ok | 成功 | ok | 不记账、不查配额 |
| `link`（源已在 upper） | ok | ok | 成功 | ok | 不记账、不查配额 |
| `mkdir` | ok | ok | **第 2049 个目录 ENOSPC**（2048×4096 = 8 MiB 整） | ok | 拦（每目录记 4096） |
| 稀疏文件（表观 64 MiB / 实占 4 KiB） | ok | ok | **成功 67108864** | **ENOSPC** | 不拦，且按**表观**字节记账 |
| `O_DIRECT` 64 MiB | ok | ok | **成功 67108864** | **ENOSPC** | 不拦 |
| 经子进程 `sh -c 'cat > f'` | ok | ok | **成功 67108864** | **ENOSPC** | 不拦 |
| 静态 C 程序（`gcc -static`）写 64 MiB | ok | ok | **成功 67108864** | **ENOSPC** | 不拦 |
| 静态 Go 程序写 64 MiB | ok | ok | **成功 67108864** | **ENOSPC** | 不拦 |
| **挂进来的共享卷**写 64 MiB | ok | ok | **成功 64 MiB，upper 只记了 workdir 的 4 MiB** | **ok（账号没动）** | **完全不进账（卷旁路）** |

原始摘录（`cowprobe-04-writematrix.log`）：

```
small_step  cow8  case=ok     gate_open_BLOCKED  DETAIL=written=10485760 step.bin=10485760
truncate_path cow8 case=ERROR gate_open_ok      DETAIL=tr.bin=1        (ENOSPC/28)
copy_file_range cow8 case=ERROR gate_open_BLOCKED  OSError 28 at os.open('cfr.bin')
mkdir       cow8  case=ERROR gate_open_ok       DETAIL=made=2048 entries=2051
gobinary    cow8  case=ok     gate_open_BLOCKED  DETAIL=gowriter_rc=0 … staticgo.bin=67108864
```

卷旁路（`cowprobe-04c-volume.log`）：

```
ceiling fs_writable: ['…/cowprobe-volume/workspace', '…/cowprobe-volume-volume']
write 4 MiB into the workdir          -> ok
write 64 MiB into the attached volume -> ok
gate: next write-open in the workdir  -> GATE=ok
upper (COW) tree while live: upper_bytes=4194304         # 只记了 workdir 的 4 MiB
volume bytes on disk: 67108864
```

**旁路清单（不拦 → 当配额用时的漏洞）**：单次 open 之后的任何写（`write`/`O_APPEND`/`pwrite`/
`mmap` 写回/`ftruncate`/`fallocate`/`O_DIRECT`/稀疏放大）、`rename`/`link` 的字节、
以及**挂进来的共享卷**。真正被拦的只有：写 open 本身、路径 `truncate`、`mkdir`、以及
**需要新 open 的那些操作**（因为下一个 open 会被拒）。

补一条边界（代码 `seccomp.rs:1372` / `:1460`）：`rename`/`link` 的**源若只在 lower**，
会先走 `copy_up_tree`/`ensure_cow_copy`，那一步按**整个文件大小**记账（因此可能直接被拒）；
本次矩阵里源都是沙箱自己刚写的（已在 upper），所以表现为「不记账」。

两个附带实测（与 COW 无关，但探针里撞到了，值得记一笔）：

* 静态 Go 二进制在**生产 `max_memory=512M` 下被 SIGKILL**（三个臂都一样）；
  调到 4096M 就正常写 64 MiB。原因是 sandlock 把 **匿名 mmap 预留** 计入内存预算
  (`crates/sandlock-core/src/resource.rs:762-785` 对超限任务直接 SIGKILL)，而 Go runtime 启动就预留大 arena。
  证据：`cowprobe-04d-go-memory.log`（`Killed` / rc=137，`GOMAXPROCS=1` 也一样）
  与 `cowprobe-04e-go-memory-4g.log`（`max_memory=4096M` → 正常写入）。
* 探针里 `/usr/local/bin/python3`（动态）与静态 C 程序都不受影响。

---

## 3. 记账口径实测（delete / truncate / 与 `du` 的偏差）

日志：`cowprobe-05-accounting.log`（COW 开、`max_disk=8M`、`fs_storage` 钉在共享卷上以便用 broker 读 upper）。

| 步骤 | upper 实际字节 | 下一个写 open |
|---|---|---|
| 写 `a.bin` 5 MiB | 5 242 880 | **ok** |
| 再写 `b.bin` 5 MiB（累计 10 MiB） | 10 485 760 | **ENOSPC** |
| `unlink b.bin`（upper 文件被真删掉） | 5 242 880 | **ok**（账跟着回落） |
| `ftruncate a.bin 0`（**不拦**的那条路） | 0 | **ok**（下次 open 的 recalc 看到 0） |
| 写 `c.bin` 4 MiB | 4 194 304 | ok |
| 稀疏文件（表观 64 MiB / 实占 4 KiB） | `dir_size`=71 303 168 | 与 `du` 差 **67 104 768 B** |

* `disk_used` 落回：**会回落**，但机制不是「删除时精确减账」，而是
  **每次写 open 都用 `recalc_disk_used()` 重走 upper 树**（`handle_open` `seccomp.rs:1085`、
  `prepare_open` `seccomp.rs:1161`：都是「先 recalc，再 `check_quota(0)`」），
  另有 `unlink` 后一次 recalc（`:1286`）和 copy_up 回滚（`:1002`）。
  所以「计数」实际上是 **upper 树的现算值**，`saturating_sub` 只在两次 open 之间起作用。
* 与 `du upper` 的偏差量级：**普通文件完全一致**（实测差 0）；
  **稀疏文件按表观长度计**（`dir_size` 用 `st_size`，`seccomp.rs:82-95`），
  一个 64 MiB 表观 / 4 KiB 实占的文件就把账多算 **64 MiB（16000×）**。
  也就是说这个账既不是 `du`（存量），也不是「真实占用」，而是**表观增量**。
* 频率：**每个写 open 一次全树 walk**；upper 里 200 / 1200 / 5200 个文件时，
  每次 open 的成本实测 0.594 / 1.824 / **8.008 ms**（§5）。

---

## 4. storage 默认落点与跨节点含义（混合路线的前提）

### 4.1 默认落点：节点本地 `/tmp`，不在共享卷上，worker 自己都看不见

`cowprobe-06-storage.log`（phase A，生产 ceiling 原样 + `workdir` 注入，**不设 `fs_storage`**）：

```
slot env XDG_RUNTIME_DIR: '<unset>'   TMPDIR: '<unset>'      # 读 /proc/<slot pid>/environ
/tmp/sandlock-cow-10001: exists=True mode=0o700 uid:gid=10001:10001 dev=0x35
  worker listdir: EACCES/EPERM (Permission denied)
  broker walk refused: PrivHelperError: path /tmp/sandlock-cow-10001 is outside the
                       privileged helper roots (/var/lib/e2b-sandboxes)
workspace dev=0x25   /tmp dev=0x35   workspace_base dev=0x25
workspace on the same filesystem as /tmp: False
findmnt(/tmp):      "/ overlay overlay rw,…,upperdir=/var/lib/docker/overlay2/…"
findmnt(workspace): "/var/lib/e2b-sandboxes /dev/vdb1[/docker/volumes/…/_data] btrfs rw,…"
```

代码对应 `crates/sandlock-core/src/cow/seccomp.rs:391-429`（`preferred_storage_base` /
`resolve_default_storage_base`）：`uid == euid` 且有
`$XDG_RUNTIME_DIR` 才用它，否则 `$TMPDIR/sandlock-cow-<uid>`（`tmp_storage_base`）。
槽位环境里两个变量都没有 ⇒ 落在 **`/tmp/sandlock-cow-<uid>`**，
而 `/tmp` 是**容器 overlay（= 节点本地）**，与共享卷（btrfs）**不同设备**。

三个直接后果：

1. **worker / 控制面看不见它**：目录 0700 属沙箱 uid，worker 自己 EACCES；
   `e2b-maint` 的路径白名单只有 `/var/lib/e2b-sandboxes`（有意为之），broker 也拒绝。
   ⇒ 这份「配额」的任何统计都进不了 worker 的 `/metrics`、孤儿扫描或回收流程。
2. **沙箱自己也看不见**：沙箱内 `listdir('/tmp')` = EACCES（COW 只把 upper 本身加进可读前缀，
   没加父目录）。
3. **把 project quota 打在 upper 上无从谈起**：upper 不在共享卷，
   `xfs_quota` 的 project 只能打在共享卷的目录树上；要打就得先用 `fs_storage`
  把 upper 钉到卷上（E2B 从不设），而钉上去以后又多出一个跨 worker 共享同一 upper 的正确性问题。

分支树的形状（`cowprobe-06c-branch-tree.log`，`fs_storage` 钉在卷上时用 broker 读原始行）：

```
'd 10001 65534 770 /var/lib/e2b-sandboxes/.cow/<sid>'
'd 10001 10001 755 /var/lib/e2b-sandboxes/.cow/<sid>/<uuid>'
'd 10001 10001 755 /var/lib/e2b-sandboxes/.cow/<sid>/<uuid>/upper'
'f 10001 10001 644 4194304 …/<uuid>/upper/a.bin'
'f 10001 10001 644 0       …/<uuid>/deleted.log'
```

即：一个分支 = 一个 uuid 目录（`upper/` + `deleted.log`），沙箱自己看不见父目录
（`listdir(storage)` = EACCES），只有 `upper/` 本身被加进了沙箱的可读前缀。

### 4.2 跨 worker：A 节点写的未提交内容在 B 节点就是「没有」

`cowprobe-06-storage.log` phase B（A、B 两个容器**共用同一个 workspace 卷**，各自有独立 `/tmp`；
A 在世代存活时被 `docker rm -f`＝节点猝死）：

```
[worker A] 写入 4 MiB（uncommitted.bin）
  workdir [A, live generation]: <>                    # 共享 workspace 里什么都没有
  local COW bases while alive: {"/tmp/sandlock-cow-10001": true}
--- SIGKILL worker A 的容器（节点死亡）---
[worker B] workdir entries on arrival: <>
  uncommitted.bin present: False                      # 4 MiB 没了
  worker B 自己的 12 MiB 单 open 写入: 成功            # 新分支 -> 配额从 0 起
  worker B 下一个 open: GATE=ENOSPC
--- 共享卷上剩下什么 ---
  只有 worker B 自己写的 workerb.bin（12 MiB）
```

⇒ 迁到另一个 worker 后：**旧节点的 upper 不会跟着走**（要么随容器消失，
要么留在旧节点的 `/tmp` 里成为没人认领的碎片），沙箱在新节点看到的是
「上一次 commit 落进 workdir 的内容」——这正是默认落点决定的语义。

---

## 5. 成本（数字）

日志：`cowprobe-07-cost.log`；两臂除 COW 外逐字段相同，`fs_storage` 钉在同一个共享卷上
（`upper` 与 workdir 同设备，避免把后端文件系统差异算到中介头上）。

| 工作量 | 无 COW | 有 COW | 结论 |
|---|---|---|---|
| 256 MiB 顺序写（单 open，3 次取最好） | 0.123 s（2081 MiB/s） | 0.119 s（2143 MiB/s） | **写路径本身没有额外开销**（`write` 不被 trap） |
| 同上，`close()`（commit/merge 尾巴） | 0.004 s | 0.269 / 0.270 / 0.320 s | 结束时要多花一次 merge（≈1 GiB/s） |
| 3000 ×（建 4 KiB 文件） | 0.456 s（6573 files/s） | 0.491 s（6107 files/s，−7%） | 建文件本身只贵 7% |
| 同上 `close()` | 0.004 s | **6.03 / 6.13 s** | **merge 是主要成本：≈2 ms/文件** |
| 200 次写 open，upper 200 个文件 | — | 0.594 ms/open | recalc 走全树 |
| 200 次写 open，upper 1200 个文件 | — | 1.824 ms/open | 线性 |
| 200 次写 open，upper 5200 个文件 | — | **8.008 ms/open** | **O(upper 条目)** |
| 改已有 64 MiB 文件 1 页（copy-up） | 0.1 ms | **53.6 ms** | 首写要整文件复制 |
| 同时刻占用 | 1.00× | **2.00×**（lower 64 MiB + upper 64 MiB） | merge 后回到 1.00× |

`upper+lower 双份`是结构性的：COW 分支期间，被改动的 lower 文件在 upper 里有一份完整拷贝
（含 `mmap`、`fallocate` 造成的表观增长），只在 commit 落地后才消除。

---

## 6. 重启 / 崩溃后的记账

日志：`cowprobe-08-restart.log`（`max_disk=8M`，`fs_storage` 钉在共享卷上）。

### 6.1 优雅重启：换代即归零（工作目录里已有 10 MiB，仍能再写 10 MiB）

```
gen1: 写 10 MiB（新文件）                     -> 成功；分支 upper=10 485 760
gen1: gate                                    -> GATE=ENOSPC          # 配额确实满过
gen1: close() -> commit                        -> workdir = 10 485 760 字节，分支消失
gen2: 同一 workdir（已有 10 MiB）再写 10 MiB   -> 成功（新分支，新计数）
gen2: gate                                    -> GATE=ENOSPC
gen2: close()                                 -> workdir = 20 971 520 字节（20 MiB）
```

**`max_disk = 8M` 之下最终存量 20 MiB。** 计数是 `SeccompCowBranch` 的内存字段
（`seccomp.rs:629`，`create()` 里初始化为 0，`:687`），磁盘上只有 upper 与 `deleted.log`，
**没有任何持久化的计数器**；重建分支 = 计数归零。所谓「重建窗口」就是**整个世代**
（不是启动后的一小段）。

### 6.2 崩溃：账归零 + 未提交内容既不合并也不记账

```
[worker A] 写 10 MiB（upper=10 485 760），世代存活时整容器被 SIGKILL
[worker B] 同一卷上仍留着 A 的分支目录: upper_bytes=10 485 760, preserved_marker=false
  workdir entries inherited from A: ten-gen1.bin,ten-gen2.bin      # 没有 A 的 10 MiB
  worker B 自己再写 10 MiB -> 成功（配额又从 0 起）；gate -> ENOSPC
  orphan bytes still on the shared volume: 10 485 760
```

* 崩溃后 `disk_used` **无法恢复**（不在磁盘上）；
* 旧分支没有 `PRESERVED` marker ⇒ `list_preserved()` **扫不到它**
  （`seccomp.rs:344` 的 `list_preserved` 只认带 marker 的目录），既不会被合并也不会被清理
  —— 在「upper 钉在共享卷」这一形态下就是**永久泄漏**；
* 在**默认落点**（节点本地 `/tmp`）形态下则更干脆：随节点消失，数据丢失（§4.2）。

---

## 7. 开放项 / 未实测

1. **chroot 形态下的路径映射**：探针跑的是纯形态。COW 的匹配用孩子在子命名空间里看到的路径
   （`cow/dispatch.rs:110-140` 直接读 child 内存的路径），而 chroot handler 在同一条 handler 链上
   **先注册先执行**（`seccomp/dispatch.rs:236-243` 的链式语义、`:606` 之后才注册 COW）。
   所以「在 chroot 形态里接上 `workdir` 就能用」**未验证**，需要单独一探。
2. **`workdir` 与 `fs_mount` 的交互**：生产 chroot 把 `/workspace` 绑到宿主沙箱目录；
   COW 的 workdir 是宿主路径。两者是同一个目录，但 guest 看到的是 `/workspace`，
   这与开放项 1 是同一个问题的两面。
3. **并发**：本次没有测「同一沙箱两条命令并发写」时 `disk_used` 的竞态
   （route-B 单槽位内命令是否真串行、以及 `recalc` 与写入的交错）。
4. **`E2B_QUOTA_VIA_AGENT` 路径**：本探针只碰 COW，没有覆盖 quota-agent 侧。

---

## 8. 复现命令（每条证据都可复核）

```bash
cd /Users/polus/project/ai/sandlock-e2b

# 0/1 建镜像 + 造两个沙箱侧程序（静态 C / 静态 Go）
./tmp/cowprobe/run.sh 00-build-programs ./tmp/cowprobe/00-build-programs.sh
./tmp/cowprobe/build.sh                       # 生产 worker 镜像 -> cowprobe-worker:local
./tmp/cowprobe/run.sh 02-image-verify ./tmp/cowprobe/02-image-verify.sh
./tmp/cowprobe/run.sh 01-smoke        ./tmp/cowprobe/01-smoke.sh

# 逐条实测
./tmp/cowprobe/run.sh 03-activation   ./tmp/cowprobe/03-activation.sh     # 第 1 条（能否激活）
./tmp/cowprobe/run.sh 04-writematrix  ./tmp/cowprobe/04-writematrix.sh    # 第 2 条（写路径矩阵）
./tmp/cowprobe/run.sh 04c-volume      ./tmp/cowprobe/04c-volume.sh        # 第 2 条补充（卷旁路）
./tmp/cowprobe/run.sh 04d-go-memory   ./tmp/cowprobe/04d-go-memory.sh    # 附注：Go vs max_memory
./tmp/cowprobe/run.sh 04e-go-memory-4g ./tmp/cowprobe/04e-go-memory-4g.sh
./tmp/cowprobe/run.sh 05-accounting   ./tmp/cowprobe/05-accounting.sh     # 第 3 条（记账口径）
./tmp/cowprobe/run.sh 06-storage      ./tmp/cowprobe/06-storage.sh        # 第 4 条（落点/跨节点）
./tmp/cowprobe/run.sh 06c-branch-tree ./tmp/cowprobe/06c-branch-tree.sh    # 附注：分支树形状
./tmp/cowprobe/run.sh 07-cost         ./tmp/cowprobe/07-cost.sh           # 第 5 条（成本）
./tmp/cowprobe/run.sh 08-restart      ./tmp/cowprobe/08-restart.sh       # 第 6 条（重启/崩溃）
./tmp/cowprobe/run.sh 09-verify       ./tmp/cowprobe/09-verify.sh        # 启动门 + 清理
```

每个日志首行是 `ENV-HEADER task=COWPROBE step=… time_utc=… kernel=… docker_server=… cmd=…`，
末行是 `EXIT=<rc>`；日志全部落在 `tmp/cowprobe/cowprobe-*.log`。
**09 必须最后跑**：它做启动门负向对照后删掉探针镜像与命名卷（要再复测就先重跑 `build.sh`）。

## 9. 清理核对（跑完）

```
=== 探针资源 ===
docker ps -a --filter name=cowprobe   -> （空）
docker volume ls | grep cowprobe      -> （空，cowprobe-ws 已删）
docker images | grep cowprobe         -> （空，cowprobe-worker:local 已 rmi）
=== 宿主仓库 ===
git status --short                    -> 只有原有 '?? target'（本次未改产品代码/未提交）
tmp/sandboxes/                        -> 未新增（探针只用命名卷 cowprobe-ws，已删）
```

## 10. 一句话建议（混合路线）

**不建议做**：COW 今天在生产形态下是死参数，接上也只能当「写前预算」，
会被单次 open、共享卷、跨代重置三条路绕过，还要额外背上 merge 的 6 s/3000 文件关闭延迟、
2× 存储放大和「账在内存、崩溃归零」的语义——同样的工程投入放在已生效的
XFS project quota 上（存量、内核强制、跨命令累计、与 `du` 可解释）更划算；
只有当真出现「沙箱内容必须事务性提交/回滚」的刚需时，才值得重新评估 COW，
而且那时也应该把它当**事务层**用（`on_exit/on_error` + 显式 commit/abort），**不要再指望它当配额**。
