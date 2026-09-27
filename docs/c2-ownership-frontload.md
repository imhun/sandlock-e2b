# C2 所有权前移（Ownership Front-loading）设计 —— **未实施**

> **状态：设计定稿（2026-09-27 会话裁定，2026-09-28 落文档），一行代码都没动。**
> 集群今天跑的是 **C1（特权外置）**：每节点一个 `e2b-priv-broker` DaemonSet（`CapEff=0x0b` =
> `CHOWN`+`DAC_OVERRIDE`+`FOWNER`）+ 非 root worker —— 见
> `docs/superpowers/plans/2026-09-27-priv-broker-externalization.md` 与
> `docs/deploy-clusters.md` §7.1–§7.3。本文记的是**另一条路线**的完整设计、它能解决什么、
> 4 条硬限制，以及落地前必须先量的两件事。
>
> **这篇不是待办。** 要捡起 C2，先读 §6（硬限制）与 §7（先量的事实）；那两条事实没量之前，
> 本文只能当"设计备选"，不能当实施计划。

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

## 5. 需要改的位点（全部）

| # | 位点（当前树的锚点） | 现状 | C2 之后 |
|---|---|---|---|
| 1 | 建树：`envd_service/agent.py`（`workspace_dir.mkdir` / `shutil.copytree` 快照 / `<ws>/workspace` 三级）与 `control_plane/api/sandboxes.py` 同形处 | worker `mkdir` + `copytree` 快照 | `mkdir` 原语建树；快照内容走 `extract` 流 |
| 2 | 属主交棒：`envd_service/uid_pool.py::apply_sandbox_ownership` | 递归 `chown` 给池 uid | 改成**校验**：walk 断言每个 entry 属主=X、目录 gid=worker gid、mode `0770`，不符即 fail closed 点名 |
| 3 | 卷切片：`envd_service/volumes.py`（卷根 `_chown_path(volume_root, host_uid)` 与 slice 建立） | worker 建 slice 再 chown；**卷根属主 = 当前挂载的那个沙箱** | `mkdir` 原语建 slice；卷根的 chown 直接删掉（`1777` 足够）⇒ **"卷根属主=首个挂载沙箱"这条语义废弃**，`tests/unit/test_volume_quota.py` 里那条 `root_st.st_uid` 断言要跟着改 |
| 4 | 检查点镜像目录：`envd_service/runtime/checkpoint_store.py`（`_hand_to_sandbox`；`.checkpoints` gate 今天是 `os.chmod(root, 0o711)`） | worker 建 parent 再交属主 | `mkdir` 原语建；**`.checkpoints` 的 `0711` 对池 uid 不可写，要单独定策**（见 §8 风险 2） |
| 5 | route-B 策略文档：`envd_service/route_b.py`（lease 文档由 worker 写） | worker 写 + `chgrp` 到 slot gid（NFS 上给非属组 `chgrp` 必被拒） | 用 `write` 原语写进 slot 自己的目录 |
| 6 | 沙箱 secret 文件：`envd_service/executors/sandlock.py` 的注入路径 | C1 wave 3 起走 broker chown（已修静默跳过） | 用 `write` 原语（"以 X 写"代替"写完再 chown"） |
| 7 | 孤儿回收：`envd_service/uid_pool.py` 的 reconcile（把 stale 树 `_chown_tree` 回 worker） | 把 stale 树 chown 回 worker，uid 可复用 | 改成**删除**（池 uid 回收 + `rmtree`，以 X 身份）；"chown 回 worker"在 NFS 上做不到 |
| 8 | 迁移导入：`envd_service/agent.py` 的 tar 解包（`tar.extractall`）与 `align_shared_uid_workspace` | worker 解 tar | `extract` 原语；`align_shared_uid_workspace` 按形态保留 |
| 9 | 平台态属主 | 今天 root worker 把 `_runtime/**`、`.uid_pool.lock`、`_images`、`_secrets` 写成 root | ✅ **C1 已完成**（`migrate-state-owner.sh`，见 §2）|
| 10 | 树根可写 | init 只在 owner≠65534 时 `chmod 1777` | 必须**无条件** `1777`（池 uid 要在里面建树，粘滞位防互删）|

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
worker，因为它要在 W1 重启后改写 lease"（`envd_service/route_b.py`）。C2 下做不到"worker 可改写
+ 仅 X 可读"，只能改成"每次通过原语以 X 重写"。secret 注入同理。

**H4：沙箱自造的 `0600`/`0700` 对 worker 不可达，记账/删除/快照都得"以 X 身份"做。**
否则垃圾树清不掉、uid 池被永久占住（池 1000 个，成了真实上限）。**注意这条今天可能已经存在**：
`docs/production-deployment-requirements.md` §5.4(b) 记着 2026-09-17 的实测 —— **这台 NAS 对 uid 0
也不给越权读别的 uid 的 `0600` 文件**。也就是说 root worker 今天同样读不到沙箱自造的 `0600`；
C2 不引入这个洞，但 C2 也修不了它。这正是 §7 的 P0-a 要把"读/删/改"三件事分开量的原因。

## 7. 落地前必须先量的事实（没量完，方案不成立）

- **P0-a：这台 NAS 对 uid 0 的越权语义** —— 读/删/改别人 `0600`/`0700` 的能力。**已知一半**：
  2026-09-17 那次实测说"uid 0 读别人的 `0600` 也 EACCES"（与通用 nfsd 行为不同，所以要重测）；
  **删/改那两半没量过**。这一条决定 C2 相对今天的 root worker 是**零回归**还是**回归**。
- **P0-b：NFSv4.0 上的粘滞位/组位行为** —— `1777`+sticky 目录里"拥有父目录但不是条目属主"能否
  `rmdir`；`0770 group=<worker gid>` 目录里 worker 能否 `unlink`。三个 gate 目录
  （`<workspaces>`、`_volumes/<vid>`、`_runtime/.checkpoints`）的做法全押在这上面。

量法：只读 + 临时目录的探针，跑在真集群上，跑完自清理；结论按"零回归 / 有回归 / 不可用"三档写下来，
再决定要不要写实施计划。

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
| P0 | NAS 行为探针（只读+临时目录）：以 X 建目录/写文件落盘属主是否=X；`1777` 粘滞目录下 worker 能否删 X 的条目；worker 用组位读写 `0770`；复现"uid 0 无 DAC 覆盖" | 三条探针输出留档，决定第 3/4/7 条的具体做法 |
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
- **做之前的三件事**：① 量 P0-a/P0-b（§7）；② 定"删除/记账怎么以 X 身份做"（§4 末的设计点）；
  ③ 明确接受 §6 的三条产品语义变更（属主不可修复、无法"worker 可写且仅 X 可读"、回收=删除）。
- **与 C1 的关系**：不是升级关系。C1 减能力面、保留一个 uid 0 组件；C2 消 root 进程、但把
  `CAP_CHOWN` 换成更宽的 `CAP_SETUID`。两条路各有取舍，别把 C2 当成"更安全的 C1"。

## 12. 参考

- **C1（已上线的那条）**：`docs/superpowers/plans/2026-09-27-priv-broker-externalization.md`、
  `docs/deploy-clusters.md` §7.1–§7.4、`docs/production-deployment-requirements.md` §2.4.1 与 §5.4(b)、
  `deploy/k8s/priv-broker.yaml`（文件头记着 NAS/`CAP_CHOWN` 不过网的实测）。
- **同族的选项文档**（同样"评估过、按触发条件决定做不做"的写法）：
  `docs/disk-quota-options.md`、`docs/pure-shape-decision.md`、`docs/n14-retire-the-emulation.md`。
- **源码锚点**（§5 的位点，按函数名而不是行号引用，行号会漂）：
  `envd_service/uid_pool.py::apply_sandbox_ownership`、`envd_service/volumes.py`、
  `envd_service/runtime/checkpoint_store.py::_hand_to_sandbox`、`envd_service/route_b.py`、
  `envd_service/executors/sandlock.py`、`envd_service/agent.py`、`control_plane/api/sandboxes.py`、
  `deploy/priv/priv_common.c`。
