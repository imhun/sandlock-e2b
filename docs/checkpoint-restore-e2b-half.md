# E2B 侧的 checkpoint/restore：设计

守卫用例（`tests/unit/test_checkpoint_restore_unused.py`）挡着的就是这一份：
**引擎能用了，但 E2B 这一半没设计。** 本文把它设计出来，并把方案里
"看起来能做、其实做不了"的三处先钉住（§1）。

> **状态（2026-09-27 更新）**：这句开头是 **2026-09-25 写设计时的现况，已过期** ——
> 设计已落地、已上线并已集群验收（S2/S3/S4 见 §6，E5–E8 收口审计见 §6(k)）；需求也**已确认**
> （用户裁定"有需求、恢复后必须支持 exec"，见 §0 的 ✅ 段与 §6(k)）。本文现在的读法：
> **`E2B_PAUSE_CHECKPOINT` 默认关**是**代码**默认；**生产清单把它设成 `"1"`**（`deploy/k8s/worker.yaml`），
> 所以线上 `pause` 已经变成"先写整份进程内存"。**E5–E8 的尾巴已全部收口**（2026-09-27，见 §6(l)）：
> `E2B_PAUSED_TTL_S` 已实现（默认 `0` = 不启用）、平台账告警已实现（默认阈值 `0.8`）、`E5` 判为
> **无独立交付（被 E4/E8 吸收）**。两条开关都**默认关/默认静默**，打开是部署的决定。
>
> **引用约定（2026-09-27 更新）**：正文里的 `tmp/**` 与 `.superpowers/sdd/**` 都在 `.gitignore` 里
> （**不是仓库路径**）。可重跑脚本已迁到 [`deploy/scripts/acceptance/`](../deploy/scripts/acceptance/)
> （原名不变），报告迁到 [`docs/reports/`](reports/)（原名不变）；`tmp/**.log` 一律是**原始日志**
> （会被清、可重跑，脚本见 `deploy/scripts/acceptance/`）。

---

## 0. 这个能力在产品上是什么

今天 `pause` 的实现是**冻结进程组**（`ProcessManager.pause_all` → SIGSTOP，
worker 侧 `_agent_set_paused`）。所以：

* 进程活在 **worker 的内存里**，只在它的 worker 还活着时存在；
* worker 滚动重启 / 节点掉线 ⇒ 沙箱的进程没了（文件还在，`_runtime` 与树都在共享 NFS 上）；
* `/sandboxes` 里的沙箱能"跨 worker 迁移"（`deployment_smoke` 验的就是这条），
  但那迁移的是**文件**，不是**正在跑的进程**。

checkpoint/restore 补的正是这一段：**把一个正在跑的沙箱写进磁盘、之后再恢复**——
包括它的 worker 已经不在了的时候。因为 workspace base 是共享 NFS，
"之后"可以是同一个 worker、也可以是另一个节点。

**所以这个能力的产品形状是：`pause` 变得能在 worker 重启后存活。**
（不是新造一个用户可见的动词；E2B SDK 的 `pause()`/`connect()` 已经有位置放它。）

**而"存活"的形状由"恢复进哪条路"决定（§1(d)）**：恢复出来的沙箱里**原有那个进程**回来了；
能不能**继续 exec** 取决于是把镜像恢复**进会话**（E2B 走这条 ⇒ 能，fork `1f41f1a`）
还是走 OCI 的 `--restore-from`（按名拒绝 ⇒ 不能）。E2B 用的是前者，
所以"pause 活过 worker 重启"换来的是一个**进程还在、且还能继续往里敲命令**的沙箱。
（本段 2026-09-26 更正：原文按"不能 exec"写，那只描述 OCI 那条 E2B 不使用的路。）

> ✅ **需求已确认（2026-09-26，用户裁定）**：checkpoint/restore 是真实需求，按产品功能推进，
> 恢复后**必须**支持 `exec`、生产形态**必须**支持（裁定表第 6 行，见
> `docs/superpowers/plans/2026-09-26-decisions.md`）。原文那句"仓库里没有任何'用户要这个'的
> 记录 / 需求仍未确认"**已过期**，这里就地更正。
>
> **实现已完成（2026-09-25，S2/S3/S4 见 §6）**：能力在 `E2B_PAUSE_CHECKPOINT`
> 后面，**默认关** —— 打开它才改变 `pause` 的成本与 `resume` 的行为，
> 这是"能力默认不改变今天的行为"这件事在代码里的形状（生产清单把它设成 `"1"`，
> 见 `deploy/k8s/worker.yaml`）。

---

## 1. 三个把方案钉死的事实

### (a) `Sandbox` 活在 slot 进程里 ⇒ **必须先加一个 slot verb**

生产是 own identity：真正的 `Sandbox` 句柄在 `sandlock-supervise` 那个独立进程里，
worker 的 python 只是通过 socket 上的 verb 跟它说话
（`envd_service/own_identity.py`，verb 有 `run`/`exec`/`wait_child`/`kill_child`/
`update_network`/`shutdown`，分发在 fork 的 `crates/sandlock-supervise/src/serve.rs:922`）。

所以"E2B 自己那一半"这个说法**不完整**：`checkpoint()` 是 `Sandbox` 上的方法，
worker 调不到它，得先有一条 verb。好消息是加 verb 是安全的——未知 verb 会被干净拒绝，
worker 早就把"这个 slot 不认识这个 verb"（旧二进制）当成一种正常情况处理
（`own_identity.py` 里那两处 "an older binary" 注释）。

**这不是纯 E2B 改动，和 `update_network` 当年一样是 fork+E2B 一起动。**

**而且 verb 不够**（2026-09-25 集群实测才发现："2 live children"）：`checkpoint()` 要求
会话里**恰好一个活着的子进程**，而 own-identity 的会话**一定**有一个 M0 ——
`sandlock-init` 只在主子进程活着时服务 `exec`，而 envd 实例在启动时没有自己的负载，
所以 slot 用 `PARKING_PROGRAM`（一个自我 SIGSTOP 的 shell）当 M0。于是"用户跑过东西的沙箱"
永远是 **2 个活子进程**（park + 负载），引擎（正确地）拒绝对它捕获。
解法是让**只有部署能知道的那句话**说出来：`exclude_main`（见 §6(i) ②）。

### (b) blob 的天然位置**不在**磁盘账本里，而"计进账"这句话得说清记到谁头上

进程内存的落点不能是 workspace——那是 guest 可读的。天然的、也是唯一干净的位置是
`gateway_common/paths.py` 的 `sandbox_runtime_dir()`：`<base>/_runtime/<id>`，
注释写着 *"the sandbox has no access at all"*。

**但它不在账本里。** 磁盘账本量的是 `record.workspace_dir`（`<base>/<id>`），
`_runtime/<id>` 是它的**兄弟目录**（`registry.py::disk_usage_snapshot` 逐 `workspace_dir` 走）。
今天那里有多大，是量过的（2026-09-25，两台 worker）：**每个沙箱 4096 字节**——正好一个块，
内容只有 `command-logs.jsonl`（样例 59 字节）。对照 1 GiB 的 `diskMB`，那是 **0.0004%**。
所以"把现有的 `_runtime` 纳入账本"这件事本身几乎不花钱（见 §2 D3 的实测）。

**真正不一样的只有 blob**：它是**整个进程的内存**，量级差三四个数量级。
难点不是"要不要算"，而是**记到谁的账上** —— 因为按用户的 `diskMB` 去算会撞上一个很坏的形状：

`pause` 是**释放**资源的动作，而不是消耗资源的动作。如果 pause 写的 checkpoint 记在被暂停的那个
沙箱自己的配额里，那么"暂停"会把它推过预算，于是它**从此不能写**（worker 侧实测口径：
超预算 ⇒ `RLIMIT_FSIZE=0` 让每个写失败在 `EFBIG`，`O_CREAT`/`mkdir`/`symlink`/`link` 回 `ENOSPC`，
见 `control_plane/registry/manager.py::enforce_disk_budget` 的说明），而它的文件**一个字节都没变**。
用户想腾空间只能删自己的文件，而 blob 不会因此变小。

（一条我先前的猜测在这里被代码否掉了，记下来免得别人重走：磁盘超限**不会**触发 pause ——
`enforce_disk_budget` 的注释写着 *"Over budget is not a pause"*，这是刻意的产品语义
（冻结会连"删东西把自己弄回预算内"一起拿走）。所以不存在"pause → 更大 → 更多 pause"的正反馈；
上面那个陷阱与它无关，它只来自"记到谁头上"。）

### (c) `restore_skipped` 是一条**语义选择**，不是缺陷

socket / pipe / memfd 恢复不了（引擎固有边界，与架构无关）。恢复出来的进程
**不会有它原来的连接**。这必须被明确表达给调用方，而不是让沙箱静默地"看起来恢复正常、
然后第一次 read/write 才炸"。引擎已经把它列出来了（`restore_skipped` 的 fd 表，
`test_restore.rs` 断言"只有 stdio"），所以 E2B 侧要做的是**把它作为恢复结果的一部分**
返回/记录，并在文档里说清"恢复的沙箱没有原有的网络连接"。

### (d) 恢复出来的沙箱能不能 exec —— **分两条路，E2B 走的是能 exec 的那条**

> **2026-09-26 更正**：本节原文按"不能 exec"写（只说 OCI 那条路），下面补上另一条路。
> 结论以本节末的更正为准；§0 的对应句与 §2 的 D9 行也已同步。

OCI 的恢复路径（`crates/sandlock-oci/src/supervisor.rs:1386` 的 `serve_one_running`）
对 `Exec` 的回答确实是：

    exec is not supported on a restored container

理由是：**exec 靠 `sandlock-init` 转发**，而 `restore_interactive` 起的是一个"被还原的进程"，
不是 `sandlock-init`（对照 create 路径：它 `spawn` 出 init，再由 init 服务 exec）。
`crates/sandlock-supervise/src/serve.rs:1481` 对"从镜像起一个 generation"
（`--restore-from`）也是同一句拒绝。

**但引擎另有一条路，而且 E2B 用的就是它 —— 恢复\*进会话\*。**
`crates/sandlock-core/src/instance.rs:1205-1211` 写得直白：
*"exec is served by init, so a resumed process that hangs off the supervisor can never be
exec'd into again (OCI's restore path refuses exactly that case)…

**Restoring \*into\* a session keeps `exec`**, `wait_child`, `kill_child` and the child table
working, which is what a long-lived sandbox needs after a resume."*

E2B 侧正是这个形状：`envd_service/own_identity.py::restore_checkpoint` 的 docstring ——
"Ask the slot to resume the image in ``dir`` **into its own session**. …
It matters because a **session** is what serves ``exec`` —— a resume that produced the
supervisor's own child could never be exec'd into again"。也就是"worker 租一个槽位，
**再**告诉它要恢复什么"，而不是"每个镜像起一个槽位"。

| | 今天（SIGSTOP 冻结） | 恢复之后（**进会话**，E2B 走这条） | 恢复之后（OCI `--restore-from`） |
|---|---|---|---|
| 原来那个进程 | 活着 | **活着**（内存状态回来了） | 活着 |
| 能不能 exec 新命令 | 能 | **能**（fork `1f41f1a` 起） | **不能**（引擎按名拒绝） |

验收（两处都过，不是推演）：fork 用例
`test_a_child_restored_into_a_session_keeps_the_session_executable` 三条断言
（进程在跑、**恢复后仍能 exec**、`children_live` 算上它），`core_lib` 911/0；
集群侧见 §6(g) —— **重启 worker 的 pod → resume → 进程状态还在、且还能 exec**。

所以 §2 的 D9（"恢复后 exec 不可用"）**不是待决的产品问题**：它在 2026-09-25 就由
fork `1f41f1a` 关闭了。OCI 那句拒绝描述的是**另一条 E2B 不使用的路**。


### (e) **根因（已定位并已修，2026-09-25）**：加载器写进"只读页"的运行时值，在恢复时丢了

一开始的症状是"真实程序恢复不了"（slot 自恢复时 `/bin/sh`、`python3` 立刻 SIGSEGV，
而静态 helper、`/bin/sleep` 正常）。**前面两版框架都是错的，被实验推翻**：

* ~~"静态能、动态不能"~~ —— 错：动态链接但不用 libc 机制的程序**能**恢复；
* ~~"libc 分配器是问题"~~ —— 错：`malloc`/`free` **能**恢复。

真正区分开的是：**这个程序有没有碰"动态加载器在启动时写进只读页"的值**。
捕获只 dump **可写**（或不可重开）的映射，于是 `PT_GNU_RELRO`
——加载器存放**重定位后的指针**与 **vDSO 函数缓存**的地方——被留作"从文件重读"，
而文件里那些位置是 **0**。第一个解引用它的 libc 调用就崩：实测
`segfault at 300 ... in libc.so.6`，加载到的指针是 NULL，位置在 `clock_gettime` 的 vDSO 路径里。

**一个机制解释两个症状，而且是构造出来的证据、不是论证**：把 RELRO 段在捕获前改成可写
（这样捕获就会 dump 它）之后，**vDSO 程序能恢复、stdio 程序也能恢复**；同样的两个程序不做这个处理就是僵尸。
其余机制被逐条钉成"正常"：裸 syscall、普通 libc 调用、`malloc`、`open`/`close`。

| 变体（同一个 harness / policy / 代码路径） | 结果 |
|---|---|
| 静态 freestanding helper | 恢复（对照） |
| `malloc`/`free` | 恢复 |
| `clock_gettime`（**走 vDSO**） | **僵尸** |
| 同一个调用走**裸 syscall** | 恢复 |
| `open`/`close`（libc 包装） | 恢复 |
| `fopen`/`fclose` | **僵尸** |
| **上面两个 + 捕获前把 RELRO 改成可写** | **都恢复** |

**为什么这一格一直没人踩到**：引擎里每条 restore 用例用的都是静态 freestanding helper
（core 四处），FFI 那条也自己编了 `-static -nostdlib -no-pie` —— 它们**都不碰 RELRO**。
**已修（2026-09-25，fork `e9b8b6c`）**：捕获现在把 RELRO 段一并 dump（`checkpoint::capture::is_relro_map`），恢复侧零改动。原来那条"故意断言缺口存在"的用例已按它自己的提示**转成回归用例**`test_libc_workloads_resume_after_restore`（四类形状全部断言能恢复）。

**沿途被排除的解释**（免得后来人重走）：动态链接、fork 子进程、fd 被 skip、
"恢复本身坏了"、slot 路径、noexec 路线（那条在 supervisor 多线程下本来就已知会崩，**不作为反证**）、
以及**断点（brk）**——它确实没被恢复（实测：活着的恢复进程没有 `[heap]` 标签；内核只把该标签给 break 范围；
且内核**拒绝**把 break 移到初始断点之下，所以 stub 路线补不了），但把 malloc 完全赶出 brk 的程序**照样崩**，
所以它是这一片里的另一个已证缺陷，而**不是**这些崩溃的原因。

### (g) (b) 的实施计划：让恢复出来的会话可 exec

**已经确定的机制**（侦察过，不是猜）：

* `SandboxInstance`（可 exec 的会话）= 一个 `sandlock-init` + 子进程表；`exec` 是请 init 去 fork 一个孩子。
* 恢复（`Sandbox::restore_interactive`）走的是 **`RestoreLaunch::Exec`**：把 **restore stub 当孩子 exec 进去**
  （stub 由描述符投递，`execveat(AT_EMPTY_PATH)`），supervisor 侧用 `process_vm_writev` 把内存写进去，
  靠 stub 的 READY/GO 握手。
* **两者现在接不上**，原因是投递面：`ExecParams` 只带 cwd/env/extra_writable/bind_ports/max_file_size，
  **没有"额外 fd"这一项**——而 restore 需要把 stub fd 和三个控制 fd（CTRL/READY/GO）交给那个孩子。
  今天这些 fd 是通过 `Sandbox::extra_fds` 交给"会话自己创建的那个孩子"的，而会话创建的孩子永远是 init。

**因此 (b) 的形状是**：让**init 生出来的孩子**也能带上 restore 的那组 fd。

1. **扩投递面**：给 exec 路加一种新请求（或给 `Req::RunExec` 加一组可选 fd），语义是"用这些 fd + 这个程序
   起一个孩子"——也就是把 `RestoreLaunch::Exec` 今天做的事挪进会话；
2. **写入端**：孩子由 init fork，slot 进程是它的**祖父**。supervisor 侧要 `process_vm_writev` 进去，
   于是第一步要验证的未知量是：**同 uid、同一 userns 映射下，slot 能不能 ptrace 到 init 的孩子**。
   能（预期），写入端才能照旧；不能，就得让 init 自己写（或换投递形状）。
3. **会话记账**：这个孩子要跟普通 exec 孩子一样进子进程表（wait/kill/进程组），否则
   `stats.children_live`、`kill`、`shutdown` 的语义都会漏掉它。

**下一步（按顺序）**：

* **S1b-spike**：✅ **已做（2026-09-25）——答案是"能"**。让会话的 init 起一个孩子
  （`test_instance_exec` 的 `exec` 路），从会话父进程对这个**孙进程**尝试两条注入路径：
  `process_vm_writev` **写进了 8 字节（正好是 payload）**、`PTRACE_ATTACH` **返回 0**
  （随后的 `PTRACE_DETACH` 也成功）。原因就是同 host uid + 同一 userns 映射。
  已钉成用例 `test_the_session_parent_can_write_into_an_init_spawned_child`
  （断言而非打印）——**这是 (b) 的地基**：它若被内核/策略改动打破，这里先红，
  而不是后来在恢复路径里变成一个说不清的现象。
* **S1b-impl**：spike 通过就按 1+3 落地（新增请求 + 会话记账），验收沿用现有的恢复用例
  （`test_restore_*`）+ 一条"恢复之后还能 exec"的会话用例。**分成两步走**：
  * **① ✅ 已完成（2026-09-25，fork `7f94561`）：摆放规划器**。`init::plan_fd_placements` /
    `wire_fds` 把 `plan_exec_stdio` 那套（FUP-23 的"先挪到保留区、再在子进程 dup 到目标号"）
    从"固定 3 个 stdio"泛化成"调用方指定的任意号码集合"，并保留它的两条性质：
    保留区不可用就退回原号（绝不变差）、身份校验覆盖整组后才动任何 fd。
    拒绝而不是猜的四条：目标号被另一个描述符占着（子进程的 dup2 会clobber，两个顺序都不安全）、
    目标号重复/落在保留区、两个数组长度不一致、空集合。单测 5 条，`core_lib` 909/0。
    **故意是纯增量**：stdio 路径一格未动；两套计划用不同保留区（64 / 80），
    因为普通 exec 可能与恢复请求同时在飞。
  * **② ✅ 已完成**：两块机制 + 投递面全部落地 ——
    * ✅ **按 fd 执行**（fork `bf60b5d`）：`init::exec_at_fd` = `execveat(fd, "", argv, envp,
      AT_EMPTY_PATH)`，让会话的孩子能跑一个**只以描述符存在**的程序（stub 是宿主产物、
      不在镜像的路径空间里，所以只能这样投递）。失败纪律照抄 execvp 那条：**errno 只读一次**、
      只有非 ENOENT 才在 fd 2 上留一行（e2b 契约钉着"缺程序 ⇒ 127 且无输出"）；
      环境用 `vars_os` 而非 `vars`（非 UTF-8 会让 `vars` panic，而那是在 fork 后的子进程里
      —— 会变成沙箱内的 abort 而不是错误）。
    * ⏳ **还差**：把"摆放计划"接进 `spawn`（给 `spawn` 一个携带 placements + exec_fd 的规格；
      三个调用点：RunMain / RunExec / launch-first）、加 `Req` 臂，并让孩子进子进程表。
    ✅ **已做**：`SpawnSpec`（by_path / placed 两种构造）、`Req::RunPlacedExec`、
    `spawn` 里按计划摆放 + 按 fd 执行、孩子以 `ChildKind::ExecAttach` 进会话表。
  * **③ ✅ 已完成**：`SandboxInstance::restore_into_session(cp)` —— 同一套 plan / StubChannel /
    `finish_restore`，只把孩子交给会话的 init 生（`Req::RunPlacedExec`），再按会话子进程登记。
    验收＝**在会话里恢复一个 child，然后这个会话还能 exec、且 stats 把它算进去**（用例见 D9 行）。
    **上线时会撞到的两个点，都写在这里省得重踩**：
    * **stub 的授权只能在创建时装**：Landlock 域是启动时一次性的，而 `fs_readable_host` 是
      `serde(skip)`（不能动 wire/镜像布局）⇒ 会话没法在恢复时给自己补授权。现在
      `launch_exec_inner` **给每个会话都装**那一条（与一次性恢复给自己装的是同一条、同一个
      平台自带静态 stub、guest 也叫不出它的名字）。代价一条：stub 路径带构建哈希，
      重建之前创建的会话不能恢复进新 stub。
    * **目标会话必须有一个长驻的第一个孩子**：M0 退出＝会话结束（文档化语义），
      用 `true` 之类去起目标会话，它会在恢复之前就自己收摊（这个坑在用例注释里也留了）。
* 之后才回到 **S2**（worker 侧存储与平台账）——S2 的接线与 (a)/(b) 无关，但 (b) 改的是会话形状，
  先落地能避免 S2 按旧形状写一遍。

### (f) 修法：把"进程改过的私有文件页"纳入捕获（F1 已给出落点）

| # | 方案 | 代价 | 判断 |
|---|---|---|---|
| **F1** | ✅ **已完成**：根因定位（见 §1(e)） | — | 结论：不是结构性墙，而是"捕获了哪些页"的一个具体漏洞 |
| **F2a** | ✅ **已做**（fork `e9b8b6c`）：**只把 RELRO 段纳入 dump**：按每个已加载对象的 `PT_GNU_RELRO`（从 `/proc/<pid>/maps` 找到 r--p 文件段，或读 ELF program headers）把这些页记成"带字节的匿名区"，恢复侧已具备能力（`SRC_ANON` + 步骤 6 的 `mprotect` 收窄） | 实际改动很小：`capture.rs` 的 `is_relro_map` + `capture_memory` 一处判据，**恢复侧零改动**（这类区域变成"带字节的匿名区"，现有步骤 6 再收窄到记录的权限） | **已完成并验证**：原来失败的四类形状（`malloc`、vDSO `clock_gettime`、`fopen`、静态对照）现在**全部恢复**；`test_restore` 5 passed / 0 failed；`core_lib` 904 passed / 0 failed |
| **F2b** | **通用的"软脏页"**：`/proc/pid/clear_refs` + pagemap 找出启动后被写过的私有文件页（CRIU 的做法） | 中：更通用（也覆盖"程序自己 mprotect 后写 .text"这类形状），但要处理 pagemap/软脏的权限与可用性 | 若 F2a 之后仍有别的形状崩，再上这条 |
| **F3** | **接受限制**：只有不碰加载器运行时状态的程序能恢复 | 零 | 今天等于不交付（python/node/sh 全在这条线上），**F2a 出来前不要选它** |

**对计划的影响**：这个缺口现在是**小而具体**的引擎修复（不是重构），所以顺序是
**F2a → 复跑本用例（缺口两个 case 应变红）→ 再回 S2（worker 侧存储/平台账）**。

---

## 2. 决策点与建议

| # | 决策 | 建议 | 理由 |
|---|---|---|---|
| D1 | blob 放哪 | ✅ **改为** `<base>/_runtime/.checkpoints/<id>/latest`（`gateway_common.paths.sandbox_checkpoint_dir`）：平台状态下，但**与 `_runtime/<id>` 并列**而不是嵌在里面 | 原设计写成 `_runtime/<id>/checkpoint/`，集群实测（§6(i)）撞死：那个目录是 worker 的 `0700`，而**写图的是沙箱自己的 slot**，它连穿过都做不到。并列之后 store 自己 `0711`（可穿不可列），每个 `<id>` 是那个沙箱的 `0700` |
| D2 | 谁拥有 | ⚠️ **修正**：目录交给**沙箱自己的池 uid**（0700），不是 worker uid | 原因是同一件事：捕获是**沙箱自己的进程树**做的（own identity 下 slot 就是那个 uid），所以"worker uid 拥有、沙箱永远读不到"在捕获路径上不可实现。仍然保住的：别的沙箱读不到（不同 uid + 0700）、不在用户的配额树里、平台仍能量能删。**没保住**：沙箱能读/伪造自己的图（边界分析见 `runtime/checkpoint_store.py` 的模块头）；要彻底关掉得让 slot 把 blob 交给 worker（协议改动，未做） |
| D3 | 配额怎么算 | ✅ **已做**：记到平台账上，不记进用户的 `diskMB`（`E2B_PLATFORM_DISK_MB`，0=不限；`runtime/platform_disk.py`）；不够就**拒绝这次 checkpoint 并退回今天的 SIGSTOP**，而不是悄悄吃掉用户的空间 | §1(b)。两边的理由都硬：不计 = checkpoint 变成绕过配额的口子；按用户配额计 = "暂停"把沙箱推过预算，它从此只能读不能写（`EFBIG`/`ENOSPC`），而用户自己的文件一个字节没变。§6(d) 是两次检查的形状 |
| D4 | 何时 checkpoint | ✅ **已做**：`pause` 时（`E2B_PAUSE_CHECKPOINT=1`，默认关），且**在冻结之前** | 复用已有的、用户可见的生命周期动词；默认关 = 不改变今天的行为。顺序不是形式：引擎的捕获自己会 SIGSTOP→SIGCONT 目标子进程，先冻再捕获会把 `pause` 撤销（§6(b)） |
| D5 | 何时 restore | ✅ **已做**：`resume` 时，**若进程已不在**（worker 重启过）才走恢复；还在就直接解冻，并删掉那张已经过时的图 | 恢复是慢路径、且丢连接，不该在正常路径上付这个代价 |
| D6 | `restore_skipped` 对外 | ✅ **已做**：恢复结果里带 fd 表（`unrecoveredFds` / `unrecoveredFdCount`），日志逐条列出，文档（本节 + §6(e)）明说"连接不回来"；**不**假装成功 | §1(c) |
| D7 | 跨节点 | 允许（blob 在共享 NFS 上），但**同内核**是硬前提 | 引擎前提，与架构无关 |
| D8 | 清理 | ✅ **已做，但是两条调用**：`_delete_sandbox_runtime` 删 `_runtime/<id>`，**再加** `checkpoint_store.remove_checkpoint_images` 删 `.checkpoints/<id>`（D1 改成并列之后，"删 `_runtime/<id>` 就够"不再成立）；隔离区（`_park_refused_tree`）同样把图搬进隔离区 | 图是平台为一个沙箱持有的最大东西，漏掉它就是把账留给一个没有人认领的目录 |
| D9 | **恢复后 exec 不可用** | ✅ **已做（2026-09-25，fork `1f41f1a`）**：走 (b) —— 恢复**进会话**，会话继续服务 exec/wait/kill/记账。验收用例 `test_a_child_restored_into_a_session_keeps_the_session_executable`（三条断言：进程在跑、**恢复后仍能 exec**、`children_live` 算上它），`core_lib` 911/0、`test_restore::` 5/0、`test_instance*` 50/0。**OCI / `--restore-from` 那条 E2B 不消费的路按 Task F4 的决定门本轮不做**（2026-09-27 复核：`rg "restore-from\|restore_from" envd_service control_plane deploy tests` = **0 命中** ⇒ 无消费者） | 见 §(g)、§6(k) ⑤ |
| D10 | **碰加载器只读页的程序恢复后即崩** | ✅ **已修（2026-09-25，fork `e9b8b6c`）**：捕获把 RELRO 段一并 dump；四类形状（`malloc`、vDSO、`fopen`、静态对照）全部恢复，`test_restore` 5/0、`core_lib` 904/0 | §1(e)/(f)。**不再是阻塞项**：这个能力对"真实程序"（python/node/sh）现在成立 |

---

## 3. 阶段（每阶段独立验收）

| 阶段 | 做什么 | 验收 |
|---|---|---|
| **S0** | ✅ 修掉守卫用例里过时的架构说法（它仍写着"引擎只支持 x86_64/riscv64、aarch64 要先移植"，而 aarch64 的 S0–S5 2026-09-24 已落地） | 用例文本与代码一致 |
| **S1a** | ✅ fork：slot 加 `checkpoint` verb（写 blob 到调用方指定的路径）—— fork `e76cb2f`，主仓 pin `82a26df` | fork 的 supervise 相位 **31 passed / 0 failed**，新用例钉住"镜像是引擎格式"与"捕获不是 kill" |
| **S1b** | ✅ fork：**从镜像起一个 slot**（`Checkpoint::load` → 用镜像里的 policy 起沙箱 → `restore_interactive`），服务 `config`/`stats`/`shutdown`、**按名拒绝 exec**（照 OCI 的既有语义）。不是 verb，是启动模式 | fork `58264eb`，supervise 相位 **32 passed / 0 failed**。用例钉住：恢复出的进程**真的在跑**（计数器继续前进）、`stats.restored` 可辨、`exec` 得到引擎原话、`shutdown` 干净退出、**进程死后报 `Exited` 而不是 `Live`**（僵尸那个 bug 就是这一步量出来的）。**警告**：workload 必须是 §1(e) 那格里"能恢复"的类型 |
| **S2** | ✅ **已完成**：worker：agent 端点（`/checkpoint`、`/restore`）+ D1/D2/D8 的落地 + **D3 的平台账与拒绝路径**（`runtime/checkpoint_store.py`、`runtime/platform_disk.py`、`own_identity.OwnIdentityInstance` 的两个 verb 客户端、`executors/sandlock.py` 的两个能力入口） | 单测：blob 落在 `_runtime`、目录 0700 且属主是**沙箱自己的池 uid**（D2 的修正；无池 uid 时才留在 worker，`test_checkpoint_store.py:150-160`/`:193-204`）、沙箱树一个字节不动、**平台账计入且用户的 `diskMB` 不变**、账满时**先拒**（一条 verb 都不发）、写超了**删掉再拒**、refusal 带原因、teardown 删净 —— `tests/unit/test_checkpoint_store.py`（16 条）+ `tests/unit/test_agent_checkpoint_restore.py`（11 条）+ `tests/unit/test_sandlock_executor_own_identity.py` 的 6 条 verb 用例 |
| **S3** | ✅ **完成**：`pause` 先捕获再冻结、`resume` 先解冻/恢复再改状态，`E2B_PAUSE_CHECKPOINT` 默认关 | 单测把两条顺序钉成事实（事件序列 `["executor.capture_checkpoint", "ctx.pause"]` / `["executor.restore_checkpoint", "ctx.resume"]`，`test_agent_checkpoint_restore.py`）。**集群验收见 §6(g)：全绿** —— 起一个跑着的沙箱 → 重启它的 worker → resume → **进程状态还在、还能 exec**（中途那段"恢复了但进程不见"是验收脚本自己的命令形状，见 §6(g) 第三轮） |
| **S4** | ✅ **已完成**：`restore_skipped` 的对外语义（D6） | `unrecoveredFds` 随 `/restore` 与 `resume` 的结果返回、逐条进日志（用例断言的是**整句**日志文本，不是子串），文档在这一节与 §6(e) 里明说"恢复的沙箱没有原有的网络连接" |

**S1 之前的任何 E2B 侧改动都没有意义**：没有 verb，worker 拿不到 `Sandbox`。

---

## 4. 明确不做

* **不做跨内核恢复**：同内核是引擎前提（捕获的是这台内核的地址空间布局）。跨内核要重做引擎，不在这个能力的范围里。
* **不做"自动迁移正在跑的沙箱"**：blob 在共享 NFS 上让这条路技术上可行，但那是调度器的活，
  且要先把 §2 全部落地。留给以后按需评估。
* **不改 `pause` 今天的行为**（旗标默认关）：S3 之前，pause 仍只是 SIGSTOP。
  **（2026-09-27 更新：这句只描述"代码默认关"）** —— S3 已完成，且**生产清单把 `E2B_PAUSE_CHECKPOINT` 设成 `"1"`**
  （`deploy/k8s/worker.yaml`），所以线上 `pause` 已经会先捕获再冻结（见 §6(k)④）。回滚就是把这个 env 改回 `0`。

---

## 5. 出处

* 引擎与两种根形态：`docs/chroot-workspace-exec.md` §9.7.9、§11（A 方案 `a6f6b04`）
* 当前守卫：`tests/unit/test_checkpoint_restore_unused.py`
* slot 协议：`envd_service/own_identity.py`、fork `crates/sandlock-supervise/src/serve.rs`
* 平台状态目录：`gateway_common/paths.py`（`sandbox_runtime_dir`）
* 账本口径：`envd_service/runtime/registry.py::disk_usage_snapshot`、`envd_service/runtime/dir_ledger.py`

---

## 6. 实施记录（2026-09-25，S2/S3/S4）

主仓提交 `9ddebc5`；引擎侧在此之前已入库并 pin（fork `57f610c` 及以下）。

### (a) 分层：谁决定什么

| 层 | 落点 | 它决定 |
|---|---|---|
| 引擎 | fork `sandlock-supervise` 的 `checkpoint`/`restore` verb | 捕获那个进程、把它恢复成会话的孩子 |
| 传输 | `own_identity.OwnIdentityInstance.capture_checkpoint/restore_checkpoint` | verb 的线上形状；**refusal 原样抛**，由上层判断它是"没有这个能力"还是"这次不行" |
| 能力 | `executors/sandlock.py::capture_checkpoint/restore_checkpoint` | 把三类"做不了"翻译成**带原因的结果**（`{"captured": false, "reason": ...}`）：中介形态、本机没有活会话、slot 拒绝（含旧 wheel 的 `unknown verb`）。**捕获从不租 slot，恢复一定租** —— 后者正是 (b) 的形状 |
| 存储/账 | `runtime/checkpoint_store.py` + `runtime/platform_disk.py` | 图放哪、谁付钱、什么时候删、平台账满时怎么拒 |
| 对外 | `agent.py` 的 `/agent/sandboxes/{id}/checkpoint`、`/restore`，以及 `pause`/`resume` 的接线 | 200 + 数字（无活会话/超账/refusal 都是**正常答案**）、401/404 的投递契约 |

这么分是因为三件事的**归属不同**：捕获属于 slot（进程树在那儿），路径与账属于部署，
而"pause 该不该因此失败"属于产品 —— 后者的答案是**不**，所以 refusal 一路只带原因，不改状态码。

### (b) 两条顺序，都是承重的

1. **捕获在冻结之前**（`pause`）。引擎的 `SandboxInstance::checkpoint` 自己会
   `SIGSTOP` 目标子进程、捕获、再 `SIGCONT`（`instance.rs`），所以对一个**已经**被
   `ProcessManager.pause_all` 停住的沙箱做捕获，收尾的 `SIGCONT` 会把暂停**撤销**：
   那会变成"pause 了但还在跑"，而且没有任何日志说它发生了。
2. **恢复在发布状态之前**（`resume`）。反过来做的话，记录已经写 `running`、进程还没回来，
   期间到达的命令会先落进一个**空会话**。两条顺序都被用例钉成事件序列（不是注释）。

### (c) 一张图，活着的时候只有一张，出门即焚

* 名字固定为 `checkpoint/latest`（不带点：引擎的 `Checkpoint::save` 是
  `<dir>.tmp` + rename，带扩展名会让临时目录变成别的东西的兄弟）；
* `pause` 覆盖写（rename 是原子的，捕获到一半不会留下半张图）；
* **两条 resume 路径都删图**：会话还在 ⇒ 图已经过时（删掉，否则下一次 resume 会把
  沙箱**倒回**一个更早的进程）；会话不在 ⇒ 图被消费掉。

于是"图存在"这句话的准确含义是：**有一个暂停着的沙箱，它的进程不在任何 worker 上**。

### (d) 平台账：两次检查，中间那一下写是必须付的

图的**大小只能靠写出来才知道**，所以拒绝分两道：

1. **写之前**（`checkpoint_no_room_reason`）：平台账已经满 ⇒ 直接拒，一条 verb 都不发
   —— 捕获是这个 worker 会写出的最大东西，没地方放就不该先花掉它；
2. **写之后**（`checkpoint_admission`）：按**实际写出的字节**判定；超了就把图**删掉**再拒
   （否则字节已经花了，只是没人记账）。

两个方向的错都被排除了：既不会"计到用户头上让暂停变成只读"，也不会"不计而成为绕过配额的口子"。
唯一的代价是第 2 道之下有一次**短暂的**超账（写完到删掉之间），这是"尺寸不可预知"的必然，
不是选择。

### (e) `restore_skipped`：说清楚，而不是等第一次 read 炸

socket / pipe / memfd 恢复不了，所以恢复出来的进程**没有它原来的连接**。worker 把引擎的
这张表原样带出来（`unrecoveredFds` + `unrecoveredFdCount`），并逐条写进日志：

```
sandbox sbx_x: resumed …/checkpoint/latest into the session (child 7, pid 31337);
2 fd(s) could not come back (sockets/pipes/memfds):
[{'fd': 5, 'path': 'socket:[12345]'}, {'fd': 6, 'path': 'pipe:[6789]'}]
```

**对外一句话**：恢复回来的沙箱**没有原有的网络连接**；长期服务要重新连接，靠旧连接的
协议会话不会接上。空列表也是一个真实答案（这个进程只持有 stdio）。

### (f) 平台账上报

`_runtime` 的字节数随心跳上到节点视图（`platformDiskUsedMB` / `platformDiskBudgetMB`，
0 = 不限），测量就在**已有的**那次离循环磁盘轮里做（同一块卷、同一节奏、同一个单飞），
并把平台账**放在 per-sandbox 走树之前**：后者会失败或超预算，而管着图的这条账不该跟着哑掉。
worker 上把 per-sandbox 扫描整个关掉（`E2B_DISK_ENFORCE_INTERVAL_S=0`）时它没有账可报 ——
那种形态本来就没有任何磁盘上报，两个端点的**回复**里仍然带着这两个数。

### (g) 集群验收（`E2B_PAUSE_CHECKPOINT=1`）

验收形状只有一条，正是今天做不到的那条：**启一个跑着的沙箱 → 重启它的 worker → resume →
进程状态还在，且还能 exec**。除了"进程回来"，第二条断言来自 D9/(b)：恢复的是一个**会话的
孩子**，所以 `exec` 继续被服务（不是 OCI 那句按名拒绝）。部署记录与实测输出见
`docs/deploy-clusters.md`。

**2026-09-25 实测（第一、二轮）：看起来"一半绿、一半红"**。脚本
`deploy/scripts/checkpoint_acceptance.py`（每步都断言，不是打印）走到：

* ✅ `pause` 写了图，落在**平台的**目录里、**属主是那个沙箱的 uid**（`_runtime/.checkpoints/
  <id>/latest`，`meta.json` + `policy.dat` + `process/`，422 KiB），沙箱自己的树一个字节没动；
* ✅ 心跳把平台账报到了节点视图（`platformDiskUsedMB` / `platformDiskBudgetMB` = 0/8192）；
* ✅ 删掉宿主 worker 的 pod、等它重建并重新注册 → `resume` **成功把镜像恢复进一个新会话**：
  worker 日志逐字 `resumed … into the session (child 1, pid 30); 4 fd(s) could not come back
  (sockets/pipes/memfds): [fd 0 pipe, fd 1 pipe, fd 2 pipe, fd 3 pipe]`（D6/S4 的对外语义也在这里
  得到了真实数据：回来的是 stdio + 一个 pipe，其余都在）；
* ❌ **被恢复的那个 python 进程几秒后不在 `/proc` 里了**（节点上只剩 worker、slot 的 3 个
  `sandlock-supervise` 与 park），计数器文件停在 pause 时的值 —— 也就是"恢复了、但没活下来"。

当时读成"引擎在动态程序 + 恢复进会话这个组合上出了问题"，于是按 fork 的纪律登记为 **FUP-30**
（本机同形 probe 也卡住，另外登记为 FUP-29）。**两件的真因后来都查清了、都不是引擎**：
FUP-29 是 fork 侧测试 runtime 单线程（本机那半），FUP-30 是**上面这条验收脚本自己的命令形状**
（下面第三轮）。也就是说：E2B 侧接线、引擎的捕获/恢复、存储与平台账**在集群上从一开始就是对的**，
被误判成引擎缺陷的那张图其实是一个 shell。

**2026-09-25 第二轮（同一晚，集群版本 `0.1.0-523-g38fbc30-20260925-204255`）**：

* ✅ **thaw 路径（D5 的常规路径）在集群上完全正确**：`pause` 之后 `Sandbox.connect` 解冻，
  **同一个进程继续计数**（3 → 4），并且**在同一个会话里 exec 一条命令能拿到输出**
  （`echo THAWED_OK` → `THAWED_OK\n`）。这正是 FUP-29 追的那条产品形状——它在**线上**是好的；
  夹具里的 1/3 卡死已被证明是 fork 侧测试用单线程 runtime 的自伤（fork `docs/fork-plan-followups.md`
  FUP-29 已闭，本机那条断言 5/5 绿）。
* ❌ **restore 路径仍然不活**，但这次拿到了最硬的证据：让第二段负载（python）**开机第一句**
  就把自己的 pid 写进 `/home/user/boot2.txt`，并把 stdout/stderr 重定向到文件（stdio 是
  pipe，恢复时会被 skip，traceback 会丢）。resume 之后：`boot2.txt` **仍是捕获前那个 pid**、
  `err2.txt`/`out2.txt` 都是空的 ⇒ **被恢复的进程连一行 Python 都没跑到**，死在恢复本身，
  而不是"跑起来之后被拒"。本轮日志里 skip 的 fd 是 5 个（0/1/2 stdio + 两个 pipe）。
  本机把这些轴一个个复现都过：**动态**（python）、**真根**（`real_root(true)` + `/usr`/`/bin`/`/lib`/`/etc`
  挂载）、**pid_ns**、**net_isolation + fd_inject_connect** —— 所以剩下的差别在 own-identity/部署侧
  （image rootfs、worker 交给子进程的额外 pipe fd、slot 向 init 孩子写内存那一段在线上 uid/userns
  下的真实行为）。下一步：给 restore 路径做一条**落盘 trace**（与引擎已有的
  `SANLOCK_REALROOT_TRACE` 同形），在集群上跑一次就能定位到"没写进去/没跳转/跳转即崩"哪一段。
* **写验收脚本要注意的部署语义**：worker 的 `max_concurrent_commands_per_sandbox` 默认 **1**，
  所以"后台跑着的进程 + 再 exec 一条命令"会排队 30s 然后 429（`command queue timed out`）。
  这不是 pause 的问题，是部署的并发形状；脚本里要先 `handle.kill()` 再 exec。

**2026-09-25 第三轮（当晚收口）：验收全绿，FUP-30 的真因是验收脚本的命令形状。**

引擎侧加了 restore 面包屑（fork `89e8ab2`：`SANLOCK_RESTORE_TRACE=1` 时逐步打印；
E2B 侧 `65ad183` 在 restore 之后把 slot 的 stderr 尾巴写进 worker 日志——会话里子进程的
stdio 是 `/dev/null`，这是唯一能看到引擎自述的通道）。第一张面包屑就露了馅：

| 同一次验收 | 图里的形态 | 握手 | 50 ms 后 |
|---|---|---|---|
| 旧命令串 `sh -c 'exec python3 …'` | `maps=19`、填充 **397312** 字节 | 全部走完 | **子进程已死** |
| 改成 `exec python3 …` | `maps=32`、填充 **6279168** 字节 | 全部走完 | **还活着** |

`19` 个映射、388 KiB 是 **dash** 的大小，不是 python。**根因**：worker 本来就把命令包成
`/bin/sh -c "<串>"`，而脚本又在串里写了一次 `sh -c '…'` —— 那个 `sh` 会 fork 出第二个 shell，
于是**会话里的活子进程是第二个 shell**，python 成了它的孙子。捕获按设计只抓"会话里那一个
活子进程"⇒ 抓到 shell；恢复出来的也是 shell，它唯一的孩子早没了，`wait4` 拿到 ECHILD
→ 走完脚本 → **立刻退出**（这就是"恢复了但没活下来"）。把 `exec` 放在**命令串的第一个词**
（让 worker 自己的 shell 原地换成 python）之后，一切正常。

**最终一次验收（集群 `0.1.0-525-g65ad183-20260925-212439`）全绿**：

* `pause` → 图 **6162 KiB**（真 python），平台账 `used=5 MiB / 8192`；
* `connect`（thaw）→ 同一进程继续计数（3 → 4），同会话 `exec` 拿到 `THAWED_OK`；
* 第二段 python 负载 → 再 `pause` → **删掉宿主 worker 的 pod** → 重建 → 重新注册 → `resume`；
* `resume` → **被恢复的进程继续计数（4 → 5）**、`exec` 拿到 `EXEC_OK`、图被消费
  （worker 日志：`resumed … into the session (child 1, pid 30); 2 fd(s) could not come back`——
  两个 stdio/pipe），引擎面包屑 `child alive 50ms after the handshake: true`。

**一条必须写给使用者的语义**（本项目就是它的第一个"使用者"）：一次 pause 捕获的是
**会话里那个活子进程**，也就是 worker 自己 exec 的 `/bin/sh -c <命令串>`。单条简单命令会被
dash 原地 `exec`（所以 `python3 …` 这种形状抓到的就是 python），但**会 fork 出子 shell 的形状**
（`sh -c '…'`、管道、`&&` 列表…）抓到的就是那个子 shell——恢复回来的也是一个 shell，
它的孩子不会回来。要"抓住那个服务进程"，命令串就该以 `exec` 开头（或直接给单条命令）。

### (i) 集群上现学到的三件事（都是 E2B 侧，2026-09-25）

1. **slot 是沙箱的 uid，不是 worker 的** —— 所以图的目录必须交给那个 uid（D1/D2 的修正）。
   第一版按设计写成"worker uid + 0700"时，引擎**捕获成功、保存失败**：
   `checkpoint save failed: process error: io error: Permission denied`。
2. **`exclude_main`** —— own identity 的会话一定有 park 当 M0，所以"用户跑过东西的沙箱"永远是
   2 个活子进程，引擎（正确地）拒绝盲捕；见 §1(a) 与 fork `da0faf5`。
3. **restore stub 必须随 wheel 走** —— `build.rs` 把它编译进 *build 容器* 的 `target/`，
   而 `stub_path()` 用的就是那条路径；装到 worker 上的 wheel 里没有它 ⇒ 每次 resume 都
   被引擎按名拒绝（`restore-stub was not built`）。修法见 fork `2d5f2e9`（随 wheel 注入
   `sandlock/bin/restore-stub`，并在 `stub_path()` 里依次认「环境变量 → 构建路径 → 可执行
   文件旁边」），集群上修复后恢复调用链立刻通。

### (j) 复核（2026-09-25 深夜，部署 `0.1.0-527-g946daa9`）

在同一套环境上把这条验收又跑了一遍，**全绿**（脚本 `deploy/scripts/checkpoint_acceptance.py`，
日志 `tmp/k0s/restore-recheck3.log`）：

* pause 写图 → `latest`（6162 KB，含 `meta.json` / `policy.dat` / `process`，属主是沙箱的
  uid `10000`）；
* 平台账按节点各一列（worker-1 `4/8192`、worker-0 `6/8192` MiB）；
* 解冻：**同一进程继续计数**（3 → 4）、同会话 `exec` 拿到 `THAWED_OK`；
* 第二段负载 → 再 pause → **删掉宿主 worker 的 pod** → 重建、重新注册 → `resume` →
  进程继续（3 → 4）、`exec` 拿到 `EXEC_OK`、图被消费（`latest: No such file or directory`）。
  引擎这次的面包屑：`resumed … into the session (child 1, pid 30); 1 fd(s) could not come
  back (sockets/pipes/memfds): [{'fd': 0, 'path': 'pipe:[…]'}]`。

**这一轮先红了两次，两次都不在 restore**，而且都属于"验收脚本自己的毛病"这一类
（与 FUP-29/30 同族）。写下来，免得下次又当成引擎回归：

1. **kubectl 通道死了会伪装成"图没写"**：`image_on_node()` 用 `kubectl exec` 去列节点上的图，
   而它的报错走 stderr、被 helper 丢掉 ⇒ 断言打出 `no checkpoint image on the hosting node:`
   后面跟着一片空白。同一时刻 worker 日志里却明明写着
   `checkpoint image written to … (5 MiB, pid 225, 4 fd(s))`。修法：`image_on_node` 在
   `kubectl` 非零退出时把 stderr 带进 listing；`main()` 开头先 `kubectl get nodes` 做前置
   断言（通道要先用 `deploy/scripts/open-cluster-tunnel.sh` 建好，验收脚本依赖它）。
2. **计时器文件的写法会被 pause 冻在"截断窗口"里**：夹具原本 `open('tick','w')` 再写，而
   `pause()` 会把进程冻在**任意一条指令**上 —— 冻在 truncate 与 write 之间时，文件在**整个
   冻结期都是空的**，读者拿到 `""`，而脚本按自己的约定把"空"读作"还没写"（那是给活着的写者
   留的窗口）⇒ 断言 `the counter vanished at the pause`，看起来像"进程死了"。改成
   **临时文件 + `os.replace`**（同目录内原子）之后，任何时刻观察到的都是一个完整的值
   （旧的或新的），这条红消失。两次跑里红绿各一次，正是它随机的直接证据。

### (k) 对外语义与两条条件任务的结论（2026-09-27，E5–E8 收口审计补写）

这一节补的全是"决定已经拍过、只是没写下来"的话。出处是
`docs/superpowers/plans/2026-09-26-checkpoint-restore-productization.md` 的决策点表与依赖表，
逐条证据见同计划尾部的《E5–E8 收口审计》与
`docs/reports/checkpoint-e5-e8-audit-report.md`（报告已迁入 `docs/reports/`；原 `.superpowers/sdd/` 那份 gitignored）。

① **恢复出来的进程没有可读的 stdout/stderr —— 它进 `/dev/null`**（决策点表第 2 行）。
会话把主程序的 stdio 接到 `/dev/null`（`envd_service/own_identity.py:332`、
`deploy/k8s/worker.yaml:490-492`），所以 `pause` 之前已经落在命令日志里的东西不会重放，
恢复之后那个进程**新写的**东西也不进平台日志；唯一的引擎自述通道是 slot 的 stderr
（`SANLOCK_RESTORE_TRACE`，见 `envd_service/executors/sandlock.py::_log_slot_stderr`）。
对外一句话：**恢复的沙箱日志消失**，要日志就自己写文件。这是"恢复进会话"这条路的形状，
不是缺陷。

② **平台账是软账：允许并发短超。** 图记**平台**的账（`E2B_PLATFORM_DISK_MB`，`0` = 不限），
口径是**整个 `_runtime`**（`envd_service/runtime/platform_disk.py::measure_platform_disk_bytes`），
随心跳上报节点视图（`envd_service/agent.py:265-266` →
`control_plane/api/internal.py:106-107` → `control_plane/registry/nodes.py:206-207`），
worker 的 checkpoint 回复里也带（`checkpoint_store.py:473-474`）。图的大小只能写出来才知道，
所以拒绝分两道（写前按剩余额度、写后按实际字节），第 2 道之下那次**短暂超账**是必然
（§6(d)）；多个 worker 并发捕获时各自按自己看到的账判定 ⇒ **总数可以短时超过预算**。
决策点表第 3 行接受这个口径（**不做**跨节点硬账）。**仍未做的是告警**：数字已经上报，
但没有任何消费者拿它报警（`rg 'alert|PrometheusRule' deploy/` 0 命中），已登记
`docs/open-issues.md`。

③ **孤儿图会被回收。** 一张图只有在**任何一处记录都不认领它**时才删；回收走的是 worker
每轮的 reconcile（`envd_service/agent.py:2345` 调
`envd_service/runtime/checkpoint_store.py::remove_orphan_checkpoint_stores`，归属判据见
`docs/superpowers/plans/2026-09-26-checkpoint-restore-productization.md` 的 Task E4），
结果进 summary 的 `checkpointsReclaimed`（`envd_service/agent.py:2423`）。所以"删了记录忘了图"
不会永远占着平台账。

④ **`E2B_PAUSE_CHECKPOINT` 在生产清单里长期默认 `"1"`，代价是 `pause` 变成"写整个进程内存"**
（决策点表第 7 行）——图量级是沙箱内存（512 MiB 生产形态下几十到几百 MiB），装不下就
**拒绝并删图、沙箱原地保持 paused**；代价写在 `deploy/k8s/worker.yaml:455-492` 的注释里。
代码默认仍是关（`envd_service/config.py:378`）：打开是**部署**的决定，不是库的默认。

⑤ **两条条件任务的决定门都落在"不做"，并已复核**：Task F3（fd 按路径重开时的身份校验）
——`rg "os\.replace|mv |rename" docs/checkpoint-restore-e2b-half.md docs/k8s-deployment.md`
今天的命中只有本能力自己的 rename（引擎保存的 `<dir>.tmp`→`latest`、验收脚本的
"临时文件 + `os.replace`"）与磁盘记账的 rename，**没有"业务文件在 pause 与 resume 之间被
replace"的用法** ⇒ 不做。**已知边界**：恢复按**路径**重开 fd，同路径换了 inode **不校验**
（真要支持时按 F3 的候选做）。Task F4（让 `--restore-from` 也能 exec）——决定门 0 命中 ⇒
不做（见 §2 的 D9 行）。

⑥ **`E2B_PAUSED_TTL_S`（paused 的过期策略）——已实现，默认关**（2026-09-27 收口，见 §6(l)）。
本节写这段时的状态是"未拍板、有意不实现"；现在这条尾巴按"默认 **0 = 不启用**"落地：
开关、语义、日志、单飞与验收全在 §6(l)。

---

### (l) E5–E7 三条尾巴的收口（2026-09-27）

计划正文只写到 `Task E4`，依赖表/决策点表却引用了 `E5`–`E8`（审计见
`docs/reports/checkpoint-e5-e8-audit-report.md`）。本轮把 `E5`/`E6`/`E7` 三条收口，**两条默认关**、
**一条判为没有独立交付**。

① **E6：`E2B_PAUSED_TTL_S`（paused 过期策略）——已实现，默认 `0` = 不启用。**

* **语义**：每轮扫出"状态仍是 `paused`、且已停了超过 TTL"的沙箱，走**与 `DELETE
  /sandboxes/{id}` 同一条清理**——先把沙箱在**它所在的节点**上拆掉（worker 的
  `DELETE /agent/sandboxes/{id}`，也就是会删 `_runtime/.checkpoints/<id>` 那张图的那个动作），
  再 `registry.delete()` 掉记录（同一条 `_release` 链：幂等归还配额 + 释放 host uid）。
  **顺序是承重的**，与 delete 端点一致：先拆树再删记录；反过来就留下一棵没有任何记录引用的
  孤儿树（E4 的 reconcile 才是兜底，不是首选）。
* **"paused 多久"的取数**：优先 `record.paused_at`；平台自己发起的暂停（驱逐、resume 回滚）带
  reason、带这个时间戳，**调用方发起的暂停不带**（`SandboxRecord.pause(reason=None)` 把它写成
  `None`），此时回退到 `last_active_at` —— 而 pause 端点在那之前刚 `record.touch()`（E9.1），
  所以回退值就是暂停时刻本身（对更老的记录只是上界）。两个戳都没有 ⇒ **不可计时 ⇒ 跳过**
  （"不知道多老"绝不能被读成"很老"）。
* **一条点名日志**（每删一个沙箱一行，WARNING）：
  `paused TTL: sandbox <id> had been paused <N>s (>= <ttl>s); removed <清单>`。
  清单是**真删掉的东西**：节点侧拆干净 ⇒ `runtime, checkpoint-image`；记录删掉 ⇒ `record`；
  配额**确实还持有**时才追加 `quota`（今天的 `pause` 在暂停那一刻就还了它，见 E9.2 —— 所以这一项
  通常不出现，出现就说明这是一条 E9.2 之前的旧记录，或别的副本读到了过期副本）。
  节点拒绝/不可达时不谎报：那一路的 WARNING 由 teardown 自己打（点名节点与原因，说明留给该
  worker 的下一轮 reconcile），本行只列真正完成的动作。
* **周期与单飞**：与控制面既有的 TTL 扫描同一个节奏（`_TTL_SWEEP_INTERVAL_S = 1.0`），
  单飞用同一套协议（`try_claim` 的 TTL 键，`F11` step 4）——键是
  `e2b:paused-ttl:sweep`，`ttl_s` 等于扫描间隔，所以多副本下**每轮只有一个副本动手**；
  候选集来自**共享**记录，两个副本同扫必然选中同一批沙箱，没有单飞就会拆两遍。
* **怎么开 / 怎么关**：`E2B_PAUSED_TTL_S=<秒>`（控制面进程的环境变量；集群里加到
  `deploy/k8s/control-plane.yaml` 的 control-plane 容器 `env:` 下）。**不设或设 `0` 就是关闭**：
  关闭时**连任务都不建、连 claim 都不取**（`PausedTTLSweeper.start()` 直接返回），
  不是"跑了但没删"。**打开之前请确认代价**：这会**摧毁用户状态**（被回收的是已经写盘的
  进程镜像，恢复不回来）。
* **本地形态的一处差别（已知边界）**：`local://` 节点走的是控制面自己的 `_destroy_local`，
  它删的是 `<workspace_base>/<id>`，**不含** `<state_base>/_runtime/.checkpoints/<id>`，
  所以本地形态这一行只报 `runtime, workspace`，图留给 worker 的 reconcile 按"没有记录认领"
  收走（E4，§6(k)③）。生产形态（远程 worker、`E2B_PAUSE_CHECKPOINT=1` 的那套）走的是
  worker 的 delete，图在同一动作里删掉。

② **E7：平台账告警——已实现，默认阈值 `0.8`。** 周期扫描读的是**同一个节点视图**
（`NodeRecord.platform_disk_used_mb` / `platform_disk_budget_mb`，即 `/internal/nodes` 给的那两个数），
算 `used/budget`，**越限打一条点名 WARNING**：
`platform ledger over budget: node <id> used <used> MiB of <budget> MiB (ratio <r> >= <t>)`。

* **阈值**：`E2B_PLATFORM_LEDGER_ALERT_RATIO`，默认 **0.8**（生产形态上 512 MiB 的沙箱、
  每张图几十到几百 MiB，"还剩五分之一"大致就是"再放得下一张图"），`0` = 关闭告警。
  `budget = 0` 是**不限**（全仓的读法），所以没有预算的节点**永不告警**。
* **刷屏策略：进入/退出各一条**，停在阈值之上不再重复。跨过去是**事件**，"还在上面"不是新信息；
  真的要每 N 分钟报一次的话，那是指标管道的事，而部署里没有指标管道
  （`rg 'alert|PrometheusRule' deploy/` 0 命中）——所以这里只做"一条点名日志"。退出时打一条
  INFO（`platform ledger back under budget: …`）；预算被撤掉（`budget` 变 `0`）时打的是
  `platform ledger is no longer budgeted: node <id> had used <used> MiB`，**不谎称"回落"**。
  节点整个从视图里消失（worker 退役、视图条目过期）不打恢复行——那不是"降下来了"。
* **周期与单飞**：30 s 一轮（`_LEDGER_ALERT_INTERVAL_S`；数字只在心跳/捕获时动，告警是跨越事件
  而不是实时仪表，也没必要把共享节点视图按秒读一遍），单飞键 `e2b:ledger-alert:sweep`。
* **怎么开 / 怎么关**：同上，`E2B_PLATFORM_LEDGER_ALERT_RATIO` 放进控制面进程环境；
  默认值即"开，阈值 0.8"，设 `0` 关闭。
* **口径**仍然是 §6(k)② 那句：**软账、允许并发短超**——所以这条告警**不是**准入判据，
  它只是"你该看一眼了"，准入依旧是捕获时那两道账检查。

③ **E5：判定为"无独立交付"，被 E4/E8 吸收**（不是"看起来没人用"）：

* 计划里 `E5` **只出现在三处**：依赖表的一行 `| E5 | 无 | E8 |`（`E8` 依赖它）与执行顺序里
  `Task E2 → Task E8` 这一段范围（`:42`、`:48` 的"E2B 侧任务/顺序"行）——**正文里没有任何一节
  定义它**（`rg -n 'E5' docs/superpowers/plans/2026-09-26-checkpoint-restore-productization.md`
  的命中只有这些引用与审计段落）。
* 依赖表那一行要的是"`E8` 的文档里补一句 `E5` 的交付"。而 `E8` 继承的文档义务已经被
  **E4**（"孤儿图会回收"，§6(k)③）、**E6**（"paused 会过期"，§6(l)①）、**E7**（"账口径 + 告警"，
  §6(l)②）与决策点表第 2 行（恢复后 stdout 语义，**直接归 E8**，§6(k)①）各自承担；
  架构行（`:11`）枚举 E2B 侧要补的东西时是"`exe`/`argv` 透传（E2）、只读查询端点（E3）、
  孤儿图回收（E4）、paused 过期策略（E6）+ 账（E7）"，**E5 没有任何一件**。
* 结论：**`E5` = 无独立交付，见 E4/E8**（计划依赖表已就地标注）。判定证据与"为什么不能只说
  '没人用'"写在 `.superpowers/sdd/checkpoint-e567-report.md`，审计期结论见
  `docs/reports/checkpoint-e5-e8-audit-report.md` §5.1。

④ **验收**：`tests/unit/test_paused_ttl_sweep.py`（11 条：开关默认值、`0` 的反证、只选"超期且
paused"、配额归零（含 E9.2 之前仍持有的旧记录）、**不重复释放**、点名日志、丢 claim 跳过、
共享 claim 只有一个副本动手、`create_app` 的**接线**（真发 worker DELETE + 删记录 + 一条日志）、
关闭时不建任务）；`tests/unit/test_platform_ledger_alert.py`（13 条：阈值默认/关闭、越限恰一条、
不重复、未越限不打、恰好等于阈值算越限、`budget=0` 不告警、退出再进入再打、节点消失不谎报恢复、
预算撤销不谎报回落、单飞、周期循环里每次跨越只打一条、视图不可列举时不炸、`create_app` 会启动
扫描）。11 个变异体（去关闭守卫、去 paused 过滤、
去单飞、去 teardown、不删记录、不接线、不记状态、`budget=0` 当满等）**逐个真跑并被杀**，
命令与输出在报告 §4。

---

## 7. 未命名异步快照的拷贝租约（N46，2026-09-27）

**这条缺陷与 checkpoint 无关，但落在这份文档要守的同一段代码上**（快照拷贝的
`control_plane/api/snapshots.py` + `control_plane/registry/snapshots.py`），运维口径写在这里；
机制与证据见 `docs/open-issues.md` 的 N46 行（控制器维护）与 `.superpowers/sdd/n46-fix-report.md`。

### 缺陷一句话

异步快照的记录先落 `creating`（共享卷上，谁都能读），而"有人在拷"的标记过去只有**带名**
（`Idempotency-Key`/`snapshotID`）的请求才持有。**未命名**异步拷贝因此没有活性标记：对端
`reconcile_pending_snapshots` 读到一条没人认领的 `creating` 记录，只能理解成"owner 崩了"
（孤儿），于是**重驱动**一条还在跑着的拷贝 —— 第二份整树拷贝，以及 worker 对半写 payload 回
409 后把记录 `mark_failed`。

### 修法：拷贝租约（lease）

* **未命名异步拷贝也持有认领**（`e2b:snapshot:copy:<id>`），值是一个 **token**（不是 `1`），
  由**owner 续租**（`SnapshotRegistry.refresh_copy`，条件续租：不是自己的 lease 不续）。
* 认领在**记录之前**取：`_create_snapshot_async` 先 `try_acquire_copy(token=...)` 再
  `reserve_from_sandbox`，所以"记录 `creating`"与"别人看得出有人在拷"之间没有窗口。
* 拷贝任务 `_run_reserved_capture` 全程跑 `_refresh_copy_lease`，结束时释放；
  **带名 id 的老路径逐字不变**（认领 + 输家等记录、等不到回 409
  `snapshot <id> is being copied by another control-plane replica`）——租约加在它下面，不替换它。
* **启动扫描改为周期性**（`snapshot_reconcile_loop`）：只有启动跑一次的话，owner 在启动**之后**
  崩掉的记录要等到下一次重启才被 settle —— 那比现状更糟。每轮用 `try_acquire_reconcile`
  单飞（与 `try_acquire_sweep` / `try_claim` 同形），重启时的那一次仍由
  `reconcile_pending_snapshots` 的启动任务负责。

### 运维口径（数字与常量）

| 量 | 值 | 常量 / 位置 |
|---|---|---|
| 拷贝租约 TTL（无续租即过期） | **30 s** | `COPY_LEASE_TTL_S`（`control_plane/registry/snapshots.py`） |
| owner 续租间隔 | **10 s** | `COPY_LEASE_REFRESH_S`（同上） |
| 孤儿扫描节奏（周期） | **10 s** | `SNAPSHOT_RECONCILE_INTERVAL_S`（`control_plane/api/snapshots.py`） |
| 带名**同步**拷贝的认领 TTL（无续租，老形状） | **600 s** | `COPY_CLAIM_TTL_S`（registry） |
| **孤儿 settle 时延上界** | owner 最后一次续租 + **≤ ~40 s**（TTL 30 s + 一轮扫描 10 s）之后进入"可被 settle"；settle 本身再花一次重驱动的时间（worker 拷贝 RPC 自带 `timeout=120`） | 上面三个常量 |
| 活拷贝被保护的条件 | 续租在跑（即控制面事件循环没被卡过 TTL） | 同上 |

两条由此推出的运维事实：

1. **带名异步拷贝的恢复也变快了**：它的认领过去是"一挂 600 s 不刷新"，现在是同一把租约
   （30 s + 续租）。owner 崩掉后这个 id 约 30 s 就能被重试/接管，而不是等 10 分钟。
2. **被保护的前提是续租线程活着**：控制面事件循环若被卡住超过 `COPY_LEASE_TTL_S`，
   本副本的续租也一起停，此时对端的扫描**可能**把这条还在拷的记录当孤儿 settle（日志里
   有 `this replica's copy lease is no longer held ...` 一行）。这是"由 owner 定期刷新"
   这一形状的固有代价；把 TTL 调大能缓解，代价是孤儿 settle 变慢。
   周期扫描自己也有一次同样的刹车：上一轮之后若发现本循环落后 ≥ `COPY_LEASE_TTL_S`，
   这一轮**跳过**（沿用 `_node_health_loop` 的"落后就别拿旧证据下破坏性结论"）。

### 接线（挂点，留给控制器）

`control_plane/api/snapshots.py` 提供好了循环，`control_plane/app.py` 里需要在 lifespan 中
加一行（与启动那次 `reconcile_pending_snapshots(app)` 并列、同样在 shutdown 时取消）：

```python
reconcile_task = asyncio.create_task(snapshot_reconcile_loop(app))
```

**本节提交前该行尚未接线**（`control_plane/app.py` 由另一单持有），所以今天的实际行为是
"启动扫描一次 + 租约已生效"；接上线之后才是"周期性 settle 孤儿"。
