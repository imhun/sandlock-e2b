# C2 所有权前移（Ownership Front-loading）设计 —— **未实施**

> **状态：设计定稿（2026-09-27 会话裁定，2026-09-28 落文档），一行代码都没动。**
> 集群今天跑的是 **C1（特权外置）**：每节点一个 `e2b-priv-broker` DaemonSet（`CapEff=0x0b` =
> `CHOWN`+`DAC_OVERRIDE`+`FOWNER`）+ 非 root worker —— 见
> `docs/superpowers/plans/2026-09-27-priv-broker-externalization.md` 与
> `docs/deploy-clusters.md` §7.1–§7.3。本文记的是**另一条路线**的完整设计、它能解决什么、
> 4 条硬限制；那两条 NAS 事实已在 §7 量过（2026-09-28）。
>
> **这篇不是待办。** §7 的两条 NAS 事实已于 2026-09-28 用探针量过（结论 `zero-regression`，
> 并推翻了一条旧记录、量出一条新的硬约束）；要捡起 C2，先读 §6（硬限制）、§5 第 2 条与 §4 末的
> 两个**必须裁定的设计点** —— 它们没定之前，本文仍然只是"设计备选"，不是实施计划。

## 1. 两条 C 路线怎么分岔

| | **C1 特权外置**（已上线） | **C2 所有权前移**（本文） |
|---|---|---|
| 做法 | `e2b-maint` 从"file-capability 二进程"变成"一个最小的 root broker 服务"，动作与路径白名单一个字不改 | 建箱流程改成"每个属于沙箱的路径都由池 uid 亲手创建"，worker 不再 chown 任何东西 |
| worker 容器 | 非 root（65534）；pod 里不再有 root 容器 | 非 root；**全链路**（含节点级 DaemonSet）都不再有 root |
| 隔离模型 | 完全不变（per-sandbox uid + route B + `0770` 组位） | 完全不变 |
| 改动面 | 1 个 broker 服务模式 + 1 个 transport + 1 个 DaemonSet | 1 个新 broker 原语（`e2b-as-uid`）+ 8 个位点改写 |
| NFS 语义风险 | 零（还是 root 在 chown，已实测过的那条路） | **每个位点都要在 NAS 上重新验一遍**（粘滞位/组位/`0600` 都会咬人） |
| 相对规模 | 1 周 | 3–5 周（含真机验收） |

**这两条路线的目标不同，不是同一个目标的强弱版**：

- C1 减的是**能力面**：worker 容器零特权，特权动作收敛到一个只有 3 个 verb、有路径白名单、
  每次调用都留日志的组件。集群里**仍然存在一个 uid 0 进程**（每节点一个 broker）。
- C2 减的是**root 进程本身**：数据面里不存在 uid 0。但代价是它把 `CAP_CHOWN` 换成
  **`CAP_SETUID`/`CAP_SETGID`**（更宽——能 `setuid` 就等于能以那个 uid 做任何事），
  所以 C2 **不是"更小的权限面"，是"另一种权限"**（见 §6 H1）。如果动机是"缩小能力集"，
  C1 反而更优；如果动机是"这套数据面里根本不允许有 root 组件"，C2 才是那个答案。

## 2. 现状：C2 的起点不是 2026-09-27 那份分析时的样子了

那份分析里列的 10 个位点，有两条在 C1 落地后已经变了，落地前要按今天重算：

| 原文条目 | 今天的状态 |
|---|---|
| 第 9 条：平台态属主一次性迁移（`_runtime/**`、`.uid_pool.lock`、`_images`、`_secrets` 从 root 交还 65534，带停机窗口） | ✅ **已完成（2026-09-27，C1 wave 2）**：`deploy/scripts/migrate-state-owner.sh`，8 条目标、集群实测 `chowned=8`、files/dirs 计数前后一致（`docs/k8s-deployment.md` §24）。C1 与 C2 共用这一条前置，**已经付过**。 |
| 第 6 条：沙箱 secret 注入在非 root 下静默跳过（只有 root 才 chown） | ✅ **已改（2026-09-27，C1 wave 3 的 fix-b）**：现在是"先全部解析、再统一落地" + `os.open(..., 0o600)` + 经 broker 通道交属主 + reclaim 前显式校验父目录属主/非 sticky（`envd_service/executors/sandlock.py`）。**C2 若做，要把它从"broker chown"换成"以 X 写入"**（§5 第 6 条）。 |
| 其余 8 条 | 与原文一致，见 §5。 |

相应地，**C2 要额外做一件原文没写的事**：撤掉 C1 的每节点 root broker（`deploy/k8s/priv-broker.yaml`）
与它的 `socket` transport，把 `SETUID`/`SETGID` 重新变成数据面唯一的特权通道（worker 容器 BND
里今天本来就有这两条，给 `e2b-slot-spawn` 的 file caps 开闸；`e2b-as-uid` 走同一条路）。三处同源
（worker uid/gid == `E2B_BROKER_PEER_UID/GID` == socket 属组）随之作废，`E2B_PRIV_HELPER_TRANSPORT`
的 socket 分支也要退役。镜像里那个 `e2b-maint`（file caps `cap_chown,cap_dac_override`）同样要到
那时重新定位 —— 它原来承担的 `rm`/`walk` 要么搬到 `e2b-as-uid` 那一族（见 §4 末的设计点），要么被
"以 X 运行"的路径整体取代；这一步是退役动作的一部分，**不是**本文已经定好的设计。

## 3. C2 的核心不变量

> **一个属于沙箱的 inode，从创建的第一次 syscall 起就必须属于池 uid；worker 永不改变任何 inode 的属主。**

它成立的前提是 NFS 客户端把**调用进程的 fsuid/fsgid** 放进 AUTH_SYS 凭据：

- "**以 X 的身份**执行 `mkdir`/`write`" ⇒ 服务端看到的凭据就是 X ⇒ 建出来的 inode 属主 = X ✔
- "以 X 的身份执行 `chown`" ⇒ **不成立**：服务端拒绝非属主改属主（这正是今天必须 euid 0 的原因）

所以 C2 的全部改动都是"把前者换掉后者"。这条链路已经在生产上被验证过：**route-B 槽位以沙箱 uid
运行，沙箱自己写的文件宿主属主就是池 uid**（`docs/production-deployment-requirements.md` §5.4(b)
的 T1 实测，`docs/reports/o1-t1-fleet-report.md`）。缺的不是能力，是"把这个形状推广到建箱的每一个位点"。

`CAP_CHOWN` 不过网这条背景（2026-09-17 实测：uid 65534 做 `chown 10000:65534` EPERM；同一挂载上
root 做同样的事成功）记在 `deploy/k8s/priv-broker.yaml` 文件头。

## 4. 新原语：`e2b-as-uid`

新增第三个 broker（file caps `cap_setuid,cap_setgid+ep`，与今天的 `e2b-slot-spawn` 同形；装在
`/var/lib/e2b-priv`，`0710 root:<worker-gid>`，与现有两个二进制同级）：

```
e2b-as-uid mkdir   --uid X --gid G --mode 0770 --path P     # 只建最后一级
e2b-as-uid write   --uid X --gid G --mode M    --path P     # stdin → P（小文件：策略文档、secret）
e2b-as-uid extract --uid X --gid G             --path D     # stdin = tar 流，解到 D
```

- `mkdir`/`write` 用 C 直接实现；`extract` 只 exec 一个**写死的** argv
  （`python3 -I -m envd_service.priv_materialize --dest <已校验路径>`）——**不是**通用 launcher。
- 路径校验复用现有 `priv_common.c` 的 `realpath` + 白名单；**uid 必须落在池内**；失败一律 fail closed
  并点名（与 `e2b-maint` 的现有纪律相同）。
- 内容传输统一走 **stdin 管道**：worker 以自己的身份打开源（快照树、模板 tar、secret 值、策略
  JSON），池 uid 的子进程只负责"写"。这是唯一能同时满足"源只有 worker 能读"与"目标必须由 X 创建"
  的形状。

> **落地前要先定的设计点（本文不臆断取舍）**：上面三个 verb 都是**创建型**。但 §6 H4 要求
> **删除与记账**（`rm` / `walk`）也必须"以 X 身份"做 —— 要么补 4/5 个 verb（`rm`/`walk` 以 X 运行），
> 要么复用"已经以 X 在跑的 route-B 槽位"来代劳。两条路的失败模式与审计面不同，要在写实施计划时
> 单列一节定死。
>
> **第二个设计点（2026-09-28 量出来的）**：池 uid **不能**把自己的目录 `chgrp` 到 worker 的组
> （探针 cell `A6` = `EPERM`，§7）。也就是说 §5 第 2 条要断言的"目录 `gid = <worker gid>`、`0770`"
> **不是 X 自己能产生的**。三选一：保留一次极小的交棒（谁来做、算不算 "worker 永不 chown" 的
> 例外）、放弃组位模型（worker 也改成"以 X 为唯一通道"）、或者换存储。这同样必须在写计划之前定死。

### 4.1 裁定一：`rm` / `walk`（删除与记账）怎么"以 X 身份"做

**先看它们今天在哪、以谁的身边跑**（C1 之后都经 broker 的 socket；`grep priv_helpers`）：

| 调用点 | 干什么 | C2 下"以 X"是否必须 |
|---|---|---|
| `uid_pool` 的孤儿回收（把 stale 树 chown/删掉） | 删整棵无主树 | **必须**：树根在粘滞的 `<workspaces>` 里，worker 删不掉（`D2`/`F4`），而且没有活着的槽位可借 |
| `agent.py` 建箱失败/拆箱（`remove_tree(workspace_dir)`）、迁移源清理（`remove_tree(workspace)`） | 删整棵树 | **必须**，同上 |
| `volumes.py` 的卷切片删除（`remove_tree(sandbox_dir)`） | 删切片 | **必须**（卷根 `1777` 也是粘滞的） |
| `checkpoint_store` 的镜像目录回收（`remove_tree(image)`） | 删 worker 自己的目录 | 不需要："gate" `_runtime/.checkpoints` 是 **worker 自己**的（`0711 owner=65534`，init 保证），worker 直接删 |
| `dir_size` / `brief_stat` / 磁盘账本 walk | 遍历+sizes | **不需要**（见下：组位够用，`F3` 实测） |
| `sandlock.py` 的 secret 交属主、`checkpoint_store._hand_to_sandbox`、`volumes` 的卷根/切片交属主 | `chown`/`chgrp` | 见 §4.2：`chgrp` 那半由 setgid 顶掉；`chown` 那半只有在"整棵树要换 uid"时才需要（迁移/接管） |

**选项**：

1. **给 `e2b-as-uid` 补一个窄的 `rm --recursive` verb**（推荐）。它只服务上表里那 4 个"必须"的调用点，
   路径过 `priv_common.c` 的 `realpath` + 白名单、uid 必须落在池内、**且必须与树/记录的属主一致**
   （否则一个池 uid 就能删另一个沙箱的树 —— 这条要在实现时写死并有用例）。**实测支持**：`A2`
   （X 拆自己的树）= `OK`、`A4`（X 在粘滞父目录里删自己的条目）= `OK`。
2. 借已经以 X 在跑的 **route-B 槽位**代劳。**不成立**：槽位跑在沙箱自己的 mount ns 里（`pivot_root`
   之后宿主路径不可达），而且要删的树大多属于**已经死掉**的沙箱 —— 没有槽位可借。除非改 fork 暴露一条
   "宿主视角、以 X 身份"的通路，那是比 1 更大的改动。
3. **去掉粘滞位**（`<workspaces>` 从 `1777` 变 `2777`），让 worker 用组位直接删树根。**实测支持**：
   `F4` 显示在没有粘滞位时 worker 删得掉 X 的子树。**代价**：任何沙箱 uid 都能 rename/删掉别人的树根
   （抢名/DoS），今天 `1777` 的粘滞位正是防这个的。**不建议**。
4. 留一个只做 `rm` 的 root broker。等于保留 uid 0 —— 与 C2 的目标直接冲突，只适合当过渡（C1.5）。

### 4.2 裁定二：`gid = <worker gid>` 这一位谁来产生

**约束（实测）**：池 uid **不能**把自己的目录 `chgrp` 到 worker 的组（`A6` = `EPERM`；POSIX 规则，
本地盘一样）。所以要得到"整棵树 `owner=X group=<worker gid>`"，只有下面几条路：

1. **共享根打 setgid（`3777` = setgid + sticky + rwx），沙箱用 `umask 007`**（推荐，**已实测**）。
   `<workspaces>` 与每个卷根一次性由平台（init/CP，root）改成 `gid=<worker gid>` + `3777`，
   之后"以 X 创建"的每一个 inode 都自动带上 `group=<worker gid>`，**且不再需要任何 chgrp**。
   2026-09-28 在真 NAS 上的 6 个 cell（fixture `setgid-parent`，`0o3777`，gid 65534）：

   | cell | 结果 | 含义 |
   |---|---|---|
   | `F1-as-X-child-inherits-worker-group` | **OK** | X 在 setgid 父目录里建的目录，组 = 65534 |
   | `F1b-child-keeps-the-setgid-bit` | **OK** | 服务端**保留**了子目录的 setgid 位（否则下一层就断） |
   | `F2-as-X-grandchild-still-worker-group` | **OK** | 两层之后组仍是 65534 ⇒ 整棵树都是 |
   | `F3-worker-deletes-inside-inherited-tree` | **OK** | worker 通过组位（`2770` 的 group-w）能删里面的文件 ⇒ **记账/删除不必"以 X"** |
   | `F4-worker-removes-X-subtree` | **OK** | 树根仍在粘滞位保护下（worker 删不掉别人的整棵树） |
   | `P0C-setgid-inheritance` | **yes** | |
   | `P0C-worker-deletes-via-group` / `P0C-sticky-preserved` | **yes / yes** | |

   **两个必须一起动的旋钮**：① 共享根 `gid=65534` + `3777`（一次性；`<workspaces>` 今天是
   `1777 0:65534`，卷根是 `1777`）；② 沙箱进程的 **umask = 007**（今天默认 `022`：`0755`/`0644`
   的树里 worker 没有组写位，`F3` 会变成 `EACCES` —— 本机彩排第一次跑就撞上了）。
   umask 要设在**被 spawn 的子进程**上（slot/supervise），不能动 worker 自己的 umask。
2. **每个树根交一次棒**（root 只 `chgrp` 一个 inode，其余靠 setgid 继承）。比 1 多一次 root 介入，
   但可以在"共享根不方便改"时用；`setgid` 位还是要有人打（同一条规则）。
3. **放弃组位模型**：worker 不再直接读写树，所有操作都"以 X"。这样 primitive 要扩成
   `mkdir/write/extract/rm/walk/read…`（一个"以 X 执行"的通用服务），审计面变大、且每次 walk 都要
   跨进程；但换来"worker 对树零直连"。**与 §6 H3 的取舍正好相反**，除非合规要求"worker 连读都不许"。
4. 槽位以 `gid=65534` 运行（fork/spawn 改 `--regid`）。结果与 1 类似，但**改变了沙箱自己的组身份**
   （ns 内 gid 映射、`setgroups([])` 语义、以及若干断言"宿主 uid/gid == 池 uid"的测试都要重核），
   风险比 1 大，收益不多。

**推荐组合**：**4.2 选 1**（`3777` + `umask 007`）⇒ 树天生就是 `owner=X group=65534`；**4.1 选 1**
（`e2b-as-uid` 只补一个带属主校验的 `rm --recursive`）⇒ 只有"整棵树要消失"的那几个调用点走"以 X"，
记账/遍历/删内部文件都还在组位上（`F3`）。这也是这次实测把两个裁定一起收窄的结果。

**尚未核清、写计划前要做的**：① 卷根今天由**控制面 API**建（`volumes.py` 的注释说 CP 建 `0o1777`），
改成 `3777` 要碰 CP 那一侧；② setgid 树在**沙箱内**看到的组是未映射的 65534（沙箱的 gid_map 只有
`0→X`）—— 要用一个真沙箱确认 `ls -l`/`os.stat` 的组不影响租户（不会因为"组不是自己"而拒绝访问，
因为属主位仍在）；③ 盘上现在有几棵**属主 0** 的老树（`755 0:65534`，`state/_runtime` 里有 58 条
Sep-19 的记录、这些树没有记录）—— `uid_pool` 的孤儿回收只回收**池内** uid，`0` 不在池里，所以它们
永远回收不掉。C2 落地前要先把这一批清掉（root 一次性 `rm`），否则"树必须出生即正确"的不变量从第一天
就带着例外。

## 5. 需要改的位点（全部）

| # | 位点（当前树的锚点） | 现状 | C2 之后 |
|---|---|---|---|
| 1 | 建树：`envd_service/agent.py`（`workspace_dir.mkdir` / `shutil.copytree` 快照 / `<ws>/workspace` 三级）与 `control_plane/api/sandboxes.py` 同形处 | worker `mkdir` + `copytree` 快照 | `mkdir` 原语建树；快照内容走 `extract` 流 |
| 2 | 属主交棒：`envd_service/uid_pool.py::apply_sandbox_ownership` | 递归 `chown` 给池 uid | 改成**校验**：walk 断言每个 entry 属主=X、目录 gid=worker gid、mode `0770`，不符即 fail closed 点名 —— ⚠ **这里的 "gid=worker gid" 是 2026-09-28 量出来的设计问题**：池 uid 自己 `chgrp` 到 worker 的组会 `EPERM`（§4 末、§7），所以这一位要么保留一次显式交棒，要么放弃组位模型 |
| 3 | 卷切片：`envd_service/volumes.py`（卷根 `_chown_path(volume_root, host_uid)` 与 slice 建立） | worker 建 slice 再 chown；**卷根属主 = 当前挂载的那个沙箱** | `mkdir` 原语建 slice；卷根的 chown 直接删掉（`1777` 足够）⇒ **"卷根属主=首个挂载沙箱"这条语义废弃**，`tests/unit/test_volume_quota.py` 里那条 `root_st.st_uid` 断言要跟着改 |
| 4 | 检查点镜像目录：`envd_service/runtime/checkpoint_store.py`（`_hand_to_sandbox`；`.checkpoints` gate 今天是 `os.chmod(root, 0o711)`） | worker 建 parent 再交属主 | `mkdir` 原语建；**`.checkpoints` 的 `0711` 对池 uid 不可写，要单独定策**（见 §8 风险 2） |
| 5 | route-B 策略文档：`envd_service/route_b.py`（lease 文档由 worker 写） | worker 写 + `chgrp` 到 slot gid（NFS 上给非属组 `chgrp` 必被拒） | 用 `write` 原语写进 slot 自己的目录 |
| 6 | 沙箱 secret 文件：`envd_service/executors/sandlock.py` 的注入路径 | C1 wave 3 起走 broker chown（已修静默跳过） | 用 `write` 原语（"以 X 写"代替"写完再 chown"） |
| 7 | 孤儿回收：`envd_service/uid_pool.py` 的 reconcile（把 stale 树 `_chown_tree` 回 worker） | 把 stale 树 chown 回 worker，uid 可复用 | 改成**删除**（池 uid 回收 + `rmtree`，以 X 身份 ⇒ §4.1 的 `rm --recursive` verb）；"chown 回 worker"在 NFS 上做不到。⚠ 今天的回收谓词是"属主 ∈ 池 且 无记录"，**属主 0 的老树永远进不来**（§4.2 末）|
| 8 | 迁移导入：`envd_service/agent.py` 的 tar 解包（`tar.extractall`）与 `align_shared_uid_workspace` | worker 解 tar | `extract` 原语；`align_shared_uid_workspace` 按形态保留 |
| 9 | 平台态属主 | 今天 root worker 把 `_runtime/**`、`.uid_pool.lock`、`_images`、`_secrets` 写成 root | ✅ **C1 已完成**（`migrate-state-owner.sh`，见 §2）|
| 10 | 树根可写 | init 只在 owner≠65534 时 `chmod 1777` | 必须**无条件** `3777`（setgid 带组 + 粘滞位防互删；见 §4.2）——卷根同理 |

## 6. 硬限制（产品语义层面，绕不过去）

先把结论说清楚：**物理上没有"做不到"这回事**（NFS 上"以池 uid 身份创建"是可用的，route-B 槽位
今天就在这么干），但有 4 条硬约束。

**H1：必须"以 X 身份创建"，所以 C2 需要"能成为 X"的能力。**
只有三条实现：新加一个 file-capability broker（`cap_setuid,cap_setgid`，就是今天
`e2b-slot-spawn` 的模式）、把这一步塞进**已经以 X 运行的 route-B 槽位**（fork 改动，不新增任何
能力）、或者换掉存储。**必须说破的事实**：C2 不是"减小权限面"，是"换一种权限"——它把
`CAP_CHOWN` 换成 `CAP_SETUID`（更宽）。它真正换来的是**数据面里不存在 uid 0 进程**。

**H2：属主写错就不可修复，一切"回收/接管"退化为删除。**
今天所有"chown 回来"的路径在 C2 下都没有等价物（§5 的第 2/3/4/7 条）。后果是硬的：

- 树必须"出生即正确"，任何 crash 留下的半成品树只能删，不能救；
- 接管既有树的前提是**同一个 uid**（靠 `record.host_uid` 的迁移/重启没问题），但跨集群恢复、
  备份导入、手工挪树这类场景只有一条路：删了重建；
- 卷根属主 = "首个挂载沙箱"这条语义一起废掉（§5 第 3 条）。

**H3："worker 要能写"与"只有 X 能读"在同一挂载上不可兼得。**
沙箱是 `setgroups([])`，没有共享组可用；能力又不过网。所以给一个 inode 定权限时只有两个选项：
X 私有（worker 完全碰不到，要改只能通过"以 X 身份"的原语整体重写），或者放开组位/其它位
（那就等于放开给所有读者）。这条直接顶死一个现有需求：route-B 策略文档的注释写着"owner 必须留在
worker，因为它要在 W1 重启后改写 lease"（`envd_service/route_b.py`）。所以这里必须**二选一**：走
"X 私有"就得每次通过原语以 X 重写（策略文档、secret 都是这一类）；走"放开组位"就是 §4.2 选的
setgid 方案 —— worker 靠组位直接读写（`F3` 实测），代价是 worker 作为可信中介能读树里的一切
（这也是今天的姿态：树里 `0755`/`0644` 的条目本来就近乎对所有读者开放）。**§4.2 选的是后者**；
secret 注入那一类"只有 X 该读"的东西仍走"以 X 写"（`F` 系列之外，见 §5 第 6 条）。

**H4：沙箱自造的 `0600`/`0700` 对 worker 与 C2 的原语都不可达，"以 X 身份"是唯一通道。**
否则垃圾树清不掉、uid 池被永久占住（池 1000 个，成了真实上限）。这里的"不可达"是**实测量出来的**，
但对谁是量出来的要分清（2026-09-28，§7）：

- **worker（65534）**：`D2`（删 `1777` 里 X 的条目）= `EPERM`、`D4`（拆 X 的 `0700` 树）= `EACCES`
  ⇒ 它确实只能请求"以 X 身份"的代劳。
- **C2 的原语（只有 `SETUID`/`SETGID`、没有 `DAC_OVERRIDE`）**：同理，它只能变成 X。
- **uid 0 那个今天的 broker 例外**：探针量到 **uid 0 能越权**（`P0A-uid0-override=yes`，两条臂
  一致，连"由 65534 创建的 `0600`"这个原记录形状也是 `OK`）—— 所以这个洞**今天并不存在**，
  它是 C2 才会真正遇到的东西（C2 里没有 uid 0 可依赖）。旧记录说反了，见 §7 的"推翻的旧记录"。

## 7. 已量：2026-09-28 的真集群结果（探针两条臂）

**探针**：`deploy/scripts/acceptance/probe_c2_ownership_p0.py`（引擎）+ `c2-p0-probe.sh`（runner，
渲染 `deploy/k8s-k0s/c2-p0-probe.yaml` 的 root Job）。它在集群里挂同一份 PVC
（`/var/lib/e2b-sandboxes`，实测 `P0-MOUNT … vers=4.0,…,sec=sys`），fork 成 5 个身份
（`root` 0:0、`broker` 0:65534、`worker` 65534、`pool` 10000、`other` 10001）跑 29 个 cell，
搭 fixture 的位置只在自己 `_probes/c2-p0-*/` 里，跑完由每个 cell 的 owner 自删。

```bash
export KUBECONFIG="$PWD/tmp/k0s/kubeconfig"      # 见 docs/deploy-clusters.md §0/§2
deploy/scripts/acceptance/c2-p0-probe.sh --apply                      # 臂 1：容器默认 cap
deploy/scripts/acceptance/c2-p0-probe.sh --apply --drop-dac-override  # 臂 2：uid 0 不带 DAC_OVERRIDE
```

**结论（两臂一致）**：

| 判据 | 结果 | 说明 |
|---|---|---|
| `C1-CONTROL` | **ok** | 今天的模型在这台 NAS 上照旧：`broker` 能 list/stat/unlink `0770` 树、`chown` verb 成功；`worker` 靠组位读写 |
| `C2-PREMISE` | **ok** | "以 X 创建"落盘属主/模式**就是**请求的样子（`A1` 读回 `10000:10000` `0700`/`0600`），X 能拆自己的树、能在 `1777` 里建/删自己的条目、能写自己的 `0770` |
| `P0A-uid0-override` | **yes** | uid 0 能读 10000/65534 的 `0600`、进 `0700`、删 `0700`/`0755`/`0770` 里的条目 |
| `P0A-uid0-needs-the-group` | **no** | 同一件事：uid 0 **不带**树属组也做得到 ⇒ 服务端给的是 uid 0 特权 |
| `P0A-uid0-record-check` | **no-longer** | 复刻 09-17 那条形状（由 65534 创建的 `0600`，uid 0 打开）= `OK`；旧记录说 `EACCES` |
| `P0B-sticky` | **enforced** | `other`（10001）删不掉 X 在 `1777` 里的条目（`EPERM`） |
| `P0B-x-can-chgrp` | **no** | X **不能**把自己的目录 `chgrp` 到 worker 的组（`EPERM`） |
| `P0C-setgid-inheritance` | **yes** | 共享根改成 `3777`（gid=65534）+ 沙箱 `umask 007` 时，X 的目录与**孙目录**都自动带 `gid=65534`，且 setgid 位被服务端保留（`F1`/`F1b`/`F2`） |
| `P0C-worker-deletes-via-group` | **yes** | 那种树里 worker 用组位就能删文件（`F3`）⇒ 记账/删内部文件**不必**"以 X" |
| `P0C-sticky-preserved` | **yes** | 同一父目录仍粘滞：worker 删不掉 X 的整棵子树（`F4`）⇒ 树根删除必须"以 X"（§4.1） |
| `C2-P0-VERDICT` | **zero-regression** | 相对今天的 root worker/broker，C2 的三条替换（以 X 创建 / 以 X 拆 / 在 sticky 父目录里自助）在这台 NAS 上都成立 |

两条臂的差别只在 `P0-CAPS`（臂 1 `CapEff=…a80425fb` 含 `DAC_OVERRIDE`；臂 2 `…a80425f9` 不含），
**cell 结果逐格相同** ⇒ 批准 uid 0 的是**服务端**，不是客户端 cap。原始日志：`tmp/c2p0-cluster-run*.log`。

**推翻的旧记录**：`deploy/k8s-k0s/worker-root.patch.yaml`（C1 删除，git 历史里还在）与
`docs/production-deployment-requirements.md` §5.4(b) 里原来写着"这台 NAS 对 uid 0 也不给越权读
别人的 `0600`（一个由 65534 建的 `.uid_pool.lock`，root 打开报 EACCES）"。同一挂载、同一 uid 0
身份下复现不出来；已按探针结果更正（§6 H4 与 §5.2 的对应结论也一并改了）。**"保留 worker 的
gid" 依然成立，但理由要读对**：它服务的是 **worker（65534，没有任何有效 cap）靠组位进树**，
以及 broker 新造出来的东西要带上 worker 的组；不是"uid 0 也进不去"。

**新量出来的硬约束（选项与建议见 §4.1/§4.2）**：`P0B-x-can-chgrp=no` —— 池 uid **不能**把目录的组
改成 worker 的 gid（POSIX 规则，本地盘一样）。但同一轮也量到**它的答案是可行的**：共享根打 setgid
（`3777`，gid=65534）+ 沙箱 `umask 007` ⇒ X 建出来的整棵树自动是 `owner=X group=65534`，而且 worker
在树内用组位就能删（`F1`–`F4`）。于是两个裁定收窄成"选哪条路"而不是"能不能做"：
**§4.2 选 1（setgid + umask 007）+ §4.1 选 1（`e2b-as-uid` 只补一个带属主校验的 `rm --recursive`）**。

**还没量、也不该由这次探针回答的**（都写在 §4.1/§4.2 的选项里）：① 卷根由**控制面 API** 创建
（`0o1777`），改 `3777` 要同时改 CP 那一侧；② setgid 树在**沙箱内**看到的组是未映射的 65534，
要用一个真沙箱确认租户侧观感/工具链没问题；③ 盘上那批**属主 0** 的老树（§4.2 末）先清掉；
④ `rm --recursive` 的"必须与树属主一致"这条守卫怎么写成用例。

## 8. 会咬人但不算硬限制

1. **gate 目录放宽后的抢名 DoS**：`.checkpoints` 若为了让池 uid 可写而放到 `1733`（粘滞），
   就留一个可被"抢名"的面，要显式裁定（或者把镜像目录移进树内）。
2. **每个位点只能在真机验**：本地 lane 一律绿，NFS 的粘滞位/组位/`0600` 只有目标机才现形。
3. **未来回归风险**：树内任何一处新代码 `mkdir`/`write` 都会悄悄造出 65534 的 inode ⇒ 需要一个
   "树内不许 worker 建 inode"的契约测试兜底。好消息是今天树内 worker 侧建目录**只有一处**
   （§5 第 1 条），租户文件写入早就走 `SandboxWriter`（以沙箱身份执行），面很小。
4. **三种历史形态（root worker / 本地盘 / legacy 共享 uid）必须继续绿** —— 测试矩阵会变大。

## 9. 分期（每期都能独立验收）

| 期 | 内容 | 验收 |
|---|---|---|
| P0 | NAS 行为探针（只读+临时目录）：以 X 建目录/写文件落盘属主是否=X；`1777` 粘滞目录下 worker 能否删 X 的条目；worker 用组位读写 `0770`；uid 0 的越权语义（两条臂） | ✅ **已完成（2026-09-28）**：`deploy/scripts/acceptance/{probe_c2_ownership_p0.py,c2-p0-probe.sh}` + `deploy/k8s-k0s/c2-p0-probe.yaml`；29 个 cell，`C2-P0-VERDICT=zero-regression`，另量出 `P0B-x-can-chgrp=no`（§7） |
| P1 | `e2b-as-uid` + `priv_materialize` + 单测（池外 uid/越界路径/符号链接逃逸/半安装 fail closed） | `tests/unit/test_priv_helpers.py` 同形断言 |
| P2 | 第 1/2 条（建树+校验），开关 `E2B_TREE_OWNERSHIP=chown\|create-as` | 真机：新建箱 → `stat` 属主=池 uid、mode `770`；`deployment_smoke` |
| P3 | 第 3/4/5/6 条（卷切片、检查点、策略文档、secret） | 各带一条 NAS 用例；`multinode_smoke` 全绿 |
| P4 | 第 7/8/10 条（孤儿回收改删除、迁移导入、树根 `1777`）—— 原文的第 9 条（属主迁移 Job）**C1 已完成** | `multiworker_interference` + 崩溃点用例 |
| P5 | 收尾：删掉 C1 的每节点 broker 与 socket transport、更新 §5.4(b)、manifest pin 单测、`docs/deploy-clusters.md` | 全套 lane + 真机验收 |

## 10. 失败模式与不变量（写实施计划前先钉死）

- **同一沙箱必须复用同一个池 uid**（CP 已经 fleet-wide 分配 + `claim(record.host_uid)`）：换了 uid
  就再也没法把树交过去，只能删树重建。要在 create/migrate 路径把这个条件写成显式 fail-closed 错误。
- **崩在任意点都要能收敛**：acquire 后崩（marker 残留）、树建一半崩（无记录 ⇒ 走 §5 第 7 条删除）、
  记录写了树没建（重新建）。每个点要有用例。
- **回退安全**：新树由池 uid 建在 NFS 上，老代码（root worker / C1 的 broker）仍能 chown/接管，
  所以 C2 是**双向可回退**的 —— 这是它相对某些方案的最大优势。

## 11. 结论

- **今天不做**：C2 的收益（数据面无 uid 0）在合规口径上通常可以被 C1 满足（worker 容器零特权 +
  特权动作收敛到白名单组件）。C2 要付的是 3–5 周、一次产品语义变更、以及每个位点的真机验收。
- **什么时候才值得做**：只要"**这套数据面里不允许存在任何 root 组件**"成为硬要求（例如审计口径
  明确到这一步），或者出现"节点级 root 组件本身不被允许"的约束。
- **P0 已经量完（2026-09-28）**：两条 NAS 事实都站在 C2 这边（`C1-CONTROL=ok`、`C2-PREMISE=ok`、
  `P0B-sticky=enforced`、`C2-P0-VERDICT=zero-regression`），并且顺带推翻了一条旧记录、量出一条
  新约束。**所以现在挡住 C2 的不是"能不能做"，而是两个必须裁定的设计点**。
- **做之前的三件事**：① 定"删除/记账怎么以 X 身份做"（§4 末的设计点）；② 定 `gid = worker gid`
  这一位谁来产生（§4 末的第二个设计点 / §5 第 2 条）；③ 明确接受 §6 的三条产品语义变更
  （属主不可修复、无法"worker 可写且仅 X 可读"、回收=删除）。
- **与 C1 的关系**：不是升级关系。C1 减能力面、保留一个 uid 0 组件；C2 消 root 进程、但把
  `CAP_CHOWN` 换成更宽的 `CAP_SETUID`。两条路各有取舍，别把 C2 当成"更安全的 C1"。

## 12. 参考

- **C1（已上线的那条）**：`docs/superpowers/plans/2026-09-27-priv-broker-externalization.md`、
  `docs/deploy-clusters.md` §7.1–§7.4、`docs/production-deployment-requirements.md` §2.4.1 与 §5.4(b)、
  `deploy/k8s/priv-broker.yaml`（文件头记着 NAS/`CAP_CHOWN` 不过网的实测）。
- **同族的选项文档**（同样"评估过、按触发条件决定做不做"的写法）：
  `docs/disk-quota-options.md`、`docs/pure-shape-decision.md`、`docs/n14-retire-the-emulation.md`。
- **P0 探针（§7 的实测）**：引擎 `deploy/scripts/acceptance/probe_c2_ownership_p0.py`、
  runner `deploy/scripts/acceptance/c2-p0-probe.sh`、Job 清单 `deploy/k8s-k0s/c2-p0-probe.yaml`；
  pin `tests/unit/test_c2_p0_probe.py`；原始日志 `tmp/c2p0-cluster-run*.log`（会随 `tmp/` 清掉，
  要复核就按 §7 的两行命令重跑）。
- **源码锚点**（§5 的位点，按函数名而不是行号引用，行号会漂）：
  `envd_service/uid_pool.py::apply_sandbox_ownership`、`envd_service/volumes.py`、
  `envd_service/runtime/checkpoint_store.py::_hand_to_sandbox`、`envd_service/route_b.py`、
  `envd_service/executors/sandlock.py`、`envd_service/agent.py`、`control_plane/api/sandboxes.py`、
  `deploy/priv/priv_common.c`。
