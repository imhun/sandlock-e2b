# 生产部署要求（XFS project quota 磁盘配额）

<!-- 本文档同时收录 E6.4 NFS 共享存储形态部署要求与实测结论（§5）。 -->

## 1. 前置条件（已确认目标机满足）

| 要求 | 目标机现状 | 达标 |
|---|---|---|
| 文件系统为 **XFS** | `/dev/nvme0n1p2` = xfs | ✅ |
| XFS 支持 project quota（`projid32bit=1`） | `projid32bit=1` | ✅ |
| 内核 ≥ 4.5 | 6.12（EL 10.2） | ✅ |
| `xfs_quota` 工具 | quota 4.09 | ✅ |
| `lsattr`（e2fsprogs）| 未装则孤儿 project 只报不清 | ⚠️ **需安装**（`_scan_project_dirs` 靠它把 projid 映射回沙箱目录）|
| 挂载启用 `prjquota` | 当前 `noquota` | ⚠️ **需启用** |

## 2. 启用步骤（需维护窗口）

### 2.1 修改 /etc/fstab

根分区条目加 `prjquota` 挂载选项：

```fstab
/dev/nvme0n1p2 / xfs defaults,prjquota 0 0
```

### 2.2 在线启用（无需重启，首选）

```bash
mount -o remount,prjquota /
```

### 2.3 验证

```bash
grep " / " /proc/mounts          # 应看到 prjquota，不再是 noquota
xfs_quota -x -c "state" /        # 应显示 Project quota enabled
```

> 回退：`mount -o remount,noquota /` + 还原 fstab。XFS 的 quota 挂载选项
> 支持在线切换，无持久副作用。

## 2.4 每沙箱 host uid（E3.2）与 route-B 槽位（2026-09-09 起为默认开）

`E2B_PER_SANDBOX_UID` 现在默认 **true**：每个沙箱从池里拿一个独立 host uid，
workspace 的属主 = 该沙箱 uid、**属组 = worker 的 effective gid**、模式 **`0770`**
（fix round 1 / 裁定 c1，2026-09-12；此前是 `0700 owner=X`）。这不是可选项式的
「加强安全」，而是另外两件事的地基：

- 共享卷的跨租户保护靠真实 DAC（1777+sticky），需要写者身份互不相同；
- chroot（镜像 rootfs）形态的 route-B `sandlock-supervise` 槽位就以该 uid 运行
  （`E2B_ROUTE_B` 默认 `auto`），路径中介的代打开由此落在沙箱自己身上（T5）。

**2026-09-11（Track F / Task F1）：非 root worker 是目标形态。** 出厂清单的
`user: "65534:65534"`（compose）与镜像自带的 `USER 65534`（k8s pod 不覆盖
`runAsUser`）现在同样能拿到 per-sandbox host uid 与 route-B 槽位。机制不再是
worker 自己 `setuid`——uid 65534 的进程 `CapEff=0`，`setuid(X)` 必 EPERM——而是 exec
镜像里两个带 **file capabilities** 的**编译型** broker（file caps 对 `#!` 脚本不生效）：

> **userns 路线的事实更正（2026-09-12，`.superpowers/sdd/task-usernsprobe-report.md`）**：
F1 探针当时判"userns 需要 `CAP_SYS_ADMIN`"，那是在**本机 OrbStack 内核**上测的——该环境
对 `uid_map` 写入一律 EPERM，**与 BND 无关**，属环境假象而非机制限制。在目标机同款内核
（aarch64 / 6.12）上、用**线上同款 BND `0xc3`**，发行版 `newuidmap`/`newgidmap` 成功写入
`0 100000 1`；把 BND 清空后同一 helper **连 exec 都被拒**（rc=126）。⇒ 映射非自身 uid 的
充分条件是 **BND ⊇ SETUID（`newgidmap` 还需 SETGID）+ 发行版 helper + `/etc/subuid` 委托段**，
**与 `CAP_SYS_ADMIN` 无关，也不需要 euid 0**。**不选 userns 是语义代价，不是机制不可用**：
它要改 fork 的 `--uid` 身份自检并新增一套启动路径，而 file caps 是"identity by construction"。
触发条件与前置清单见 `docs/superpowers/plans/2026-09-10-shared-volume-cwd-and-backlog-closeout.md`
「Track U」。

> **二次更正（2026-09-15，本机实测，撤回上面的"本机环境假象"归因）**：那批探针把失败归给
> "本机 OrbStack 内核对 `uid_map` 写入一律 EPERM" **不成立**。同一台机器上，只要容器的
> seccomp 档放行 `unshare`（`unconfined`，或现在上线的
> `deploy/seccomp/sandlock-worker.json`）：
> - 能力矩阵四档全部通过：`root + 默认 caps`、`root + 仅 SYS_ADMIN`、
>   `root + 仅 SETUID,SETGID`、`uid 65534 + SETUID,SETGID` ——
>   `unshare(CLONE_NEWUSER)` 成功且 `uid_map`/`gid_map` 写入成功；
> - 本地 lane 跑 `tests/contract/test_route_b_executor.py` = `14 passed`，槽位日志
>   `guest-uid=uid-0-in-userns`（含 SYS_ADMIN 与 `PROD_DROP_CAPS=SYS_ADMIN` 两种形态一致）。
>
> 真正的门槛在**容器运行时**：Docker 默认 seccomp 档把 `unshare` 放在
> `includes.caps: [CAP_SYS_ADMIN]` 组里（实测默认档 `unshare(NEWUSER)`=EPERM、`unconfined`=ok），
> 所以当初"某档 BND 失败、加 `SYS_ADMIN` 就成功"的整张矩阵，是 **seccomp 的能力门控**，
> 与内核无关；`newuidmap`+subuid 那条路另有自己的前提（helper 特权、`/etc/subuid` 按
> **调用者用户名**配段、挂载非 `nosuid`、无 NNP）。
> ⇒ **本地完全能做 userns（以及 `net_isolation`）的验证**；"只能上目标机验"的说法撤回，
> `.superpowers/sdd/task-usernsprobe-report.md` §6 的那条建议随之失效。

| broker（`/var/lib/e2b-priv/`） | file caps | 调用形态 |
|---|---|---|
| `e2b-slot-spawn` | `cap_setuid,cap_setgid+ep` | `spawn --uid X --gid X -- <sandlock-supervise 绝对路径> <args…>`；内部 `setgroups([])`→`setgid(X)`→`setuid(X)`→`execve`。`argv[0]` 钉死为 supervise 绝对路径、X 必须在已配 uid 池内，所以它不是「以任意 uid 跑任意程序」的通用工具；**不 shell、也不转手 exec 别的 setuid 工具**（实测那样 caps 会在 exec 时丢失：`setresuid failed: EPERM`）。槽位 exec 后自动零 cap（uid 变更清空 permitted/effective，supervise 自身无 file caps）。 |
| `e2b-maint` | `cap_chown,cap_dac_override+ep` | `chown --uid X [--gid G] [--recursive] --path P`、`chown --worker …`、`rm --path P`、`walk --path P`。P 必须经 `realpath` 落在 `<workspace_base>/` 或 `<shared_volume_root>/` 之下，`..`/符号链接逃逸一律拒绝；`rm`/`chown` 还必须**严格在**根之下（不接受根本身）。 |

两者共用一份校验模块（`deploy/priv/priv_common.c`）：uid 池范围、根白名单、参数
形状各只有一处实现，避免「其中一份忘了检查」。**broker 的职责分工（c1 之后）**：
route-B 的 `RouteBConfig.spawner` 指向 `e2b-slot-spawn`（唯一的"以池内 uid 起进程"原语）；
`chown` 由 `e2b-maint` 保留（worker 是 `0770` 的**属组**而不是属主，自己 chown 不了）；
`rm`/`walk` 只在 worker 自己的组访问够不到时兜底 —— 沙箱自建的 `0700` 子目录、
`1777` 卷根、以及升级前遗留的 root 属主目录；日常数据面（files API、watcher、
命令日志、快照、删除租户树）都是 worker 进程内的普通 I/O，不再经过 broker
（`envd_service/priv_helpers.py`：`remove_tree`/`dir_size` 都是「先自己来、EACCES 才找 broker」）；
`E2B_PRIV_HELPERS=auto|off` 是开关，启动自检验证「存在 + cap 正确 + 沙箱不可达 +
路径正确」，半安装的 broker 对一律 fail closed 并点名。非 root 形态的
`E2B_ROUTE_B_TMP_ROOT` 必须在白名单根之下（清单已设 `/var/lib/e2b-sandboxes/.route-b`）：
槽位 policy/program 文档靠 `e2b-maint` 归到该槽位 uid（`0440`，
owner=worker 以便 W1 重启重写），否则会退化成 world-readable（策略文档带 egress
proxy 凭据），自检会按名字拒绝。**root worker 形态保持现状**（root 自己有这些
能力，route-B 继续用 `setpriv` 起槽位）。

**为什么 worker 需要访问工作区（数据面所有者）**：files API、watcher、命令日志写入、
快照（`copytree`）、导出/迁移、生命周期回收**全部在 worker 进程里**跑，而且必须对
「已 pause / 已冻结 / 进程已退出的沙箱」同样成立——也就是说这条访问需求是**固有的**，
不是某一次特权操作。root 形态靠 root + `DAC_OVERRIDE` 满足它；非 root 形态用
**组成员**满足：树的属组是 worker 的 `os.getegid()`（**不硬编码 65534**，k8s 可以
`runAsGroup`），worker 因此以自己的身份读写，而不需要 broker 代读每一个字节。

**跨沙箱隔离由什么保证**：沙箱以 `uid X / gid X` 运行且**清空 supplementary groups**
（route-B 槽位由 broker `setgroups([])`，进程内 userns 路径是 `setgroups=deny`），
而 `0770` 的"other"位是 0、属组是 worker 的 gid≠X ⇒ 另一个沙箱既进不去目录也读不到
文件：这仍然是**一条普通的内核 DAC 判定**（契约里既有真沙箱（uid Y）的 list/write/rm
被拒，也有 `setpriv --reuid Y --regid Y --clear-groups` 的裸内核复核，外加"带上 worker
的 gid 就能读"的正向对照）。**硬护栏**：uid 池不得覆盖 worker 自己的 uid/gid
（`E2B_UID_POOL_START/SIZE` 必须避开 `os.geteuid()/os.getegid()`），否则某个沙箱会拿到
worker 的身份、组隔离失效——启动自检 fail closed 并点名（`priv_helpers.check_worker_identity_outside_pool`，
root worker 同样检查）。

**顺序**：`chmod` 必须**先于** `chown`（chown 之后 worker 不再属主，再 chmod 会 EPERM；
broker 刻意不带 `CAP_FOWNER`，root 形态看不出这个顺序，非 root 必踩）。

**接受的边界（fix round 2 判定，2026-09-12；W6 定读数语义，2026-09-13）**：worker 是用
**属组权限**访问托管目录的，所以**沙箱自己把条目收紧到 `0600`/`0700`** 时，platforms 侧
读不到它（`sbx.files.read` 会 EACCES；root 形态没有这个限制——它靠 `DAC_OVERRIDE`）。
实测（非 root 栈，`tmp/f1/f1-c1-locked-file-probe.log`）：沙箱内
`chmod 600 /home/user/locked.txt` 后，沙箱自己 `cat` 得到内容、`sbx.files.list` 正常、
`/metrics` 目录扫描正常、`sbx.files.remove` 成功（删除只需要目录写权限），
只有 worker 侧**读文件**被拒。判定依据：仓库里**没有任何用例**依赖"worker 读沙箱自建的
更严格权限文件"这条路径（`tests/sdk/python/*`、`tests/contract/test_filesystem_rpc.py`、
`tests/security/*` 全量 grep `chmod|0600|permission` 只有"沙箱给自己文件加可执行位"
与宿主侧 marker 两类）⇒ **接受该边界**，不为此给 broker 加通用 `read/write` 动词。
删/扫由 worker 自己（属组）或 `e2b-maint rm/walk` 兜底。

**W6 读数语义（不是"塌成空体"，也不是 500）**：该边界是**部署形态的永久属性**（非 root
worker 永远拿不到别人的 `0600`），所以它必须作为**明确的客户端可见错误**表达，而不是伪装成
平台故障。实现（`envd_service/http/files.py`）：

| 形状 | W6 之前（实测，非 root 栈） | W6 之后 |
|---|---|---|
| `0600` 文件（worker 读被拒） | `500` + 裸 errno：SDK 抛 `SandboxException("500: [Errno 13] Permission denied: '/var/lib/…/locked.txt'")` —— 看起来像平台坏了，调用方无从下手 | `403`，`message = "Path <p> is not readable: <原因> ([Errno 13] …)"` |
| 文件在沙箱自建的 `0700` 父目录下（`stat` 被拒） | 同上（`500` + 裸 errno；`pathlib` 只吞 `ENOENT/ENOTDIR/ELOOP`，`EACCES` 照抛） | 同上 `403` |
| 路径确实不存在 | `404 {"message": "Path <p> not found"}`（不变） | 不变 |
| 路径存在但不是普通文件（目录等） | `404`（不变） | 不变（仍 `404`；读目录不在本项范围内） |

原因文案点名**沙箱自建的私有权限**（`0600`/`0700` / `0700` 父目录）、说明 worker 用属组身份
读，并给出两条出路：**在沙箱内部读**（`cat` 等），或**放宽该条目权限**。`403` 是 Connect
协议里 `permission_denied` 的 HTTP 映射（`gateway_common/errors.py`），SDK 侧表现为
`SandboxException("403: <message>")`：状态与文案都指向"条目权限"，不再指向"平台 500"。
回归钉在 `tests/contract/test_files_private_entries.py`（RED：HEAD 两形状都是 `500 ≠ 403`；
GREEN：两者都是 `403` + 逐字文案）。

follow-up（一句，仍未做）：若将来出现"worker 必须**读成功**沙箱自建 `0600` 文件"的需求，走
**该沙箱自己的 route-B 槽位**读回（槽位就是 uid X、本来就能读自己的文件），槽位不可用时仍
按上表返回明确的"不可读"（`403`），不得退化成空体或 500。

要求与影响（逐条对照）：

| 项 | 说明 |
|---|---|
| 权限 | 分三层，**照抄会多给特权**（2026-09-10 实测，逐项见 §2.4.1）：**沙箱侧最小集 = `CAP_SETUID`+`CAP_SETGID`+`CAP_CHOWN`**；`CAP_DAC_OVERRIDE` 是**管理面**兜底需要（升级前遗留的 root 属主树、`1777` 卷根；c1 之后租户树是 `0770`、worker 走属组，日常数据面不再需要它）；`CAP_SYS_ADMIN` **在出厂镜像与清单形态下 worker 已不需要**（共享卷 bind 由 A4 删除、配额改由 quota-agent 提供、低端口 sysctl 由容器 spec 声明；代码里仍有两条非部署默认的路径需要它，见 §2.4.1 的限定），也**不是 E3.2 / route B 的前置**；`CAP_SYS_PTRACE` 只在走进程内 `RunAs` 时才需要。非 root worker 装了 F1 的两个 file-capability broker（出厂镜像都装）就同样建 uid 池并走 route B —— 非 root 现在是**目标形态**；只有**没有** broker 时才自动关闭 uid 池并保持「固定身份 + Landlock」（E5.1）、启动打一条 WARNING（但线上实际是 root，见下面审计）。 |
| 容量 | 并发沙箱数受 `E2B_UID_POOL_SIZE` 约束（默认 1000，起始 `E2B_UID_POOL_START=10000`）；池满即建箱失败。多 worker 共用同一 workspace 时必须配**互不重叠**的段。 |
| 进程/内存 | chroot 形态每沙箱多一棵 supervise 进程树（supervise + sandlock-init + 停车 M0）。它在沙箱 cgroup **之外**，不计入 `max_memory`/`max_disk`，并在 `max_processes` 里占 1；容量表按「N 沙箱 = N 额外进程」重算。 |
| 回收 | route-B 代次的结束由 envd 生命周期（TTL/idle eviction/删除 → `executor.close()`）决定，不再依赖 core 的 15 min idle；槽位进程退出前该 uid 不会被再次租出（W1）。 |
| 文件系统 | uid 只对**支持属主的存储**有意义：repo 的 virtiofs bind 挂载上 chown 是 no-op，生产请用容器原生 / XFS（本项目门禁把 workspace 放 `/var/lib/e2b-sandboxes` 的 XFS+prjquota 上）。 |
| 关掉它 | 显式 `E2B_PER_SANDBOX_UID=false` 回到旧的共享 uid（1000）形态：**pure（无 base image）形态照常跑**；**chroot 形态在 root worker 上会被 fork 直接拒绝建箱**（沙箱 host uid 1000 ≠ 中介 euid 0 ⇒ `in-process path mediation refused: … Run sandlock-supervise as uid 1000 (route B)`，见下条）。非 root worker 不受影响（沙箱就用 worker 自己的 euid，中介身份与沙箱身份同一个）。降级逃生门已不存在：E2B 2026-09-10 不再请求它，fork B3 2026-09-11 把字段连 API 一起删除（详见下条）。 |
| 删档的后果（终态，B3 2026-09-11） | **「特权进程内中介 + 路径中介 + 非 0 host uid」只剩 route B 一条路**。fork 把 `mediation_run_as` 档位整体删除（枚举/字段/builder/FFI 导出/CLI flag/`--policy` wire 键/Python 取值/`stats` 计数），拒绝文本如今只给一个修法：`in-process path mediation refused: … Run sandlock-supervise as uid <N> (route B)`。因此 **root worker + chroot（image-rootfs）形态要能建箱，以下四条硬前置必须同时成立**：① 安装的 wheel 带 `sandlock/bin/sandlock-supervise`（fork F17/B3 之后的 wheel；`_route_b_decline` 会点名缺哪条）；② `E2B_ROUTE_B` 不为 `off`（`auto`/`on`，`on` 时失败是硬报错、`auto` 时回落进程内）；③ `E2B_PER_SANDBOX_UID=true`（否则根本没有 per-sandbox host uid 可租）；④ worker 的 uid 段（`E2B_UID_POOL_START`/`SIZE`）与其他共用同一 workspace 的 worker **不重叠**，且池未满。任一条不成立 ⇒ 该 chroot 建箱失败，并打一条 ERROR 说明「为什么没有槽位、怎么修」（容器实测钉在 `tests/security/test_template_isolation.py::test_in_process_chroot_is_refused_without_a_slot`）。**没有兜底**：既不会退回进程内代打开（那会把文件属主写成 worker = T5），也没有降级档可开。非 root worker（E5.1，中介就是它自己的 euid）与 **pure 形态（无 chroot/COW/fs_deny/policy_fn 的 per-uid RunAs）不受影响** —— 拒绝是条件触发的，这两条组合不构成拒绝，也不会打这条 ERROR。 |
| **CAP_SYS_PTRACE**（进程内 RunAs 才有） | 内核要求「给子进程写 `uid_map`」除 `CAP_SETUID` 外还要对该子进程的 **ptrace 访问权**。实测 `--cap-drop ALL`：只补 `SYS_ADMIN` ⇒ 每个建箱都挂在 `sandlock_create failed`；只再补 `SYS_PTRACE` 即通。**route B 不需要它**（槽位自己就是那个 uid，自映射 `0 -> euid` 无需特权）。root worker + 非 chroot 形态开 E3.2 时，worker 启动会打 WARNING（`PER_UID_NO_PTRACE_WARNING`）说明缺哪条 cap、怎么修。 |

### 2.4.1 特权最小集（2026-09-10 实测：非特权容器 + 真 fork wheel + 真槽位；`SYS_ADMIN` 行 = 终态，A6 迁出 + A7 门禁固化，2026-09-11）

| capability | 作用在哪 | 摘掉的实测后果 |
|---|---|---|
| `SETUID` + `SETGID` | 把 route-B 槽位起在该沙箱的 host uid 上（`setpriv --reuid X --regid X --clear-groups`） | 租不到槽位 ⇒ chroot 形态被 fork 拒绝建箱（见上「删档的后果」行） |
| `CHOWN` | workspace/slice 交给该沙箱 uid，属组 = worker 的 gid、模式 `0770`（fix round 1 / c1）；uid 回收时再 chown 回来 | E3.2 的属主前提不成立（非 root 形态由 `e2b-maint` 代做） |
| `DAC_OVERRIDE` | **兜底**：worker 的属组访问够不到的树 —— 沙箱自建 `0700` 子目录、`1777` 卷根、升级前遗留的 root 属主目录（`e2b-maint rm/walk`）。c1 之后 files API / watcher / 命令日志 / 快照 / 删除租户树都走 worker 自己的属组权限，不经 broker | 旧 `0700` 模型下：`PermissionError: …/sbx_a/workspace`、对账与共享卷持久化用例 4 failed / 4 error；新模型下日常路径已不需要它 |
| `SETUID`+`SETGID`+`CHOWN`+`DAC_OVERRIDE`（**BND**，非 root worker） | 非 root worker 的那四条 cap 只出现在容器 **bounding set** 里，不落到 worker 进程：`capabilities.add` 对非 root 不产生 `CapEff`（实测 `--user 65534 --cap-add SETUID` 仍 `CapEff=0`），它们唯一的作用是给 broker 的 file caps「开闸」——**file caps 必须是 BND 的子集，否则连 exec 都 EPERM**（实测 rc=126）。worker 侧的四步特权动作全部由两个专用 broker 完成（见 §2.4 的 F1 段） | 缺任一条 ⇒ broker exec 被内核拒绝（`Operation not permitted`），`e2b-slot-spawn`/`e2b-maint` 全废：非 root worker 退回进程内 E5.1 形态（无 per-sandbox uid、无槽位；chroot 形态建箱被 fork 拒绝）。`E2B_PRIV_HELPERS` 自检会点名缺哪条并让 worker 拒绝启动。**另：`--cap-drop ALL` 必须跟 `--cap-add`**（单独 drop ALL ⇒ BND=0 ⇒ 同样死），**绝不能加 no-new-privileges**（实测 NNP=1 时 file caps 被忽略：`setgroups/setgid/setuid: Operation not permitted`） |
| `SYS_ADMIN` | **出厂镜像与清单形态下，worker 侧不需要它**（A6 迁出，A7 固化）。全库只剩一处用途，且不在 worker 上：`deploy/stack/docker-compose.prod.yml` 的 **quota-agent** 服务（`profiles: ["quota"]`）执行 `xfs_quota -x` —— 内核按 effective `CAP_SYS_ADMIN` 门控（§2.4.3）。worker 侧原来那三处：① 共享卷 `mount --bind` 进 workspace —— **A4 删 bind**（卷视图 = 请求路径决定的符号链接）+ **A5 补祖先穿透位**；② 直接执行 `xfs_quota -x` —— **A6** 改由 quota-agent 提供（worker 只发 HTTP，`E2B_QUOTA_AGENT_URL` 即开关）；③ 写 namespaced sysctl（`ip_unprivileged_port_start`）—— **A6** 改由容器 spec 声明（compose `sysctls:` / `docker --sysctl`；k8s 是 **pod 级** `spec.template.spec.securityContext.sysctls`；`NET_BIND_SERVICE` 对非 root pod **不足以**覆盖 `:53`，实测见 §2.4.3）。⚠️ **限定**：代码里仍有两条非部署默认的路径需要它 —— 合体节点（`E2B_ENABLE_LOCAL_NODE` 默认 **true**：控制面在进程内自建卷配额；W4 起它的 `via_agent` **跟随** envd 的开关 —— `E2B_QUOTA_AGENT_URL` 存在就走 agent（§2.4.3/§2.4.4），所以只有**没配 agent** 的合体节点才在控制面进程里本地直连、才需要它）与 legacy `E2B_ENABLE_NETNS=true`（运行时写 `net.ipv4.ip_forward` + iptables）；这两条在**出厂镜像**里也跑不起来（无 `xfs_quota`/`sysctl`/`iptables`，实测镜像 `command -v` 全 MISSING），所以「不需要」只在镜像 + 清单形态下成立 | 摘掉它的后果**只剩配额降级**：quota-agent 未配置/不可达 ⇒ 建箱与挂卷照常、无 per-sandbox 磁盘硬限 + 一条 WARNING。共享卷不再是理由 —— A4/A5 的契约（`tests/contract/test_shared_volume_relative_cwd.py` 等 36 条）+ 13 条穿透单测在**无 `SYS_ADMIN`** lane 三连绿（`tmp/a4-final-step4-run{1,2,3}.log`）；A7 起整份套件也在**无 `SYS_ADMIN`** 下全绿：`PROD_DROP_CAPS=SYS_ADMIN UNPRIVILEGED_PHASE=0 ./deploy/scripts/test-prod-shaped.sh` = `1075 passed, 3 skipped, 0 failed`（`tmp/a7-nosa.log`，cap 探针 `CapEff 0xa02c35fb → 0xa00c35fb`）。A6 的配额 lane（同形状，无 `SYS_ADMIN`）：agent 形态 `tmp/a6-agent.log` = `107 passed`；降级形态 `tmp/a6-degrade.log` = `29 passed, 5 errors`（5 个 error 是 XFS prjquota 门用例被 `E2B_TEST_STRICT_SKIPS=1` 显式暴露；A7 起 `tests/unit/test_xfs_project_quota_agent.py` 不再被 deselect，见 §2.5） |
| `SYS_PTRACE` | 只服务**进程内 RunAs**（父进程给子进程写 `uid_map` 需要对该子进程的 ptrace 访问权） | 非 route-B 的 per-uid 沙箱每个建箱挂在 `sandlock_create failed`；route B 完全不需要 |

**构建期 vs 运行期（F1 实测，都是踩过的坑）**：`setcap` 需要 `libcap2-bin`，而执行
`setcap` 的构建容器自身要有 `CAP_SETFCAP`；**打 cap 必须发生在最终镜像阶段**——
`COPY --from` **不保留** `security.capability` xattr，在 builder 阶段打的 cap 会静默丢失
（镜像里 `getcap` 读回空）。运行期不需要 `SETFCAP`：worker 自身没有任何有效 cap，
能力只在 exec broker 的那一刻由文件 xattr 授予，槽位 exec 后又是零 cap（`/proc/<pid>/status`
实测 `Uid 10007 … CapEff 0000000000000000`）。worker 镜像基于 `python:3.14-slim`（无编译器），
所以 broker 走多阶段编译（builder 装 `gcc`/`libc6-dev`），最终阶段只 `COPY` 二进制 + `setcap`。

**威胁模型（file capabilities）**：file cap 的语义就是「**任何能 exec 该文件的进程获得该
cap**」。因此 broker 必须落在沙箱不可达的路径，且路径本身的 DAC 要比 Landlock 更可靠：

- `/var/lib/e2b-priv`：root 所有、**组 = worker 的 gid（65534）**、mode `0710`，二进制 `0750`。
  沙箱的 uid 是池内 uid（10000+），不在该组，既不能穿目录也不能 exec（lane 实测
  `uid 10001` exec 返回 `Permission denied`）；而 worker（uid/gid 65534）能 exec。
  **注意不能照抄成「root 所有 0700」**：那样 worker 自己也 `Permission denied`（F1 实测），
  整条 file-cap 路线直接死掉。
- 也**不要**放 `/usr/local` 或 `/opt`：纯形态（无 chroot）的 Landlock 规则覆盖这两个前缀，
  broker 会变成沙箱可达；这也是「沙箱不可达」不能只靠 Landlock 论证的原因。
- 面已经被收窄到最小：只授予**单个专用二进制**（不是通用 `setpriv`/`chown` 副本）、
  uid 必须在池内（**永不接受 uid 0**）、`spawn` 的 program 钉死为 `sandlock-supervise`
  绝对路径、`maint` 的路径必须 `realpath` 落在两个白名单根之下。
  沙箱若真能 exec broker 就等于拿到 `cap_setuid`——这是这条路线**接受**的风险，
  用上述四条把它压到「需要先突破 DAC + Landlock」的前提里。

三条实测口径（`e2b-sandlock-test` 容器，`--cap-drop ALL` + 指定 capset）：

- 无 `SYS_ADMIN`、无 `SYS_PTRACE` 下跑 route-B + chroot 沙箱：
  `created=true, guest_uid="0", mknod_rc="mknod-rc=1", blk_node_left=false, file_owner=21710`
  —— 建箱成功、客体内仍是 uid 0、**造不出也不会残留块设备节点**、写出文件属主就是该沙箱 host uid。
- worker 持 `SYS_ADMIN` 时读 route-B 槽位的 `/proc/<pid>/status`：`uid 21710, CapEff=0`
  —— **一个 cap 都没有**：`setpriv` 降 uid 会清空 effective/permitted 集，E2B 也不用
  `--ambient-caps` 往下传。所以「worker 有 SYS_ADMIN」不会顺着中介进程进入租户路径；
  它的作用域是 worker→宿主，不是沙箱→宿主。
- 同一探针里进程内后端（`E2B_ROUTE_B=off` 的 chroot、以及 pure 形态的 per-uid 沙箱）在无
  `SYS_PTRACE` 时挂在 `sandlock_instance_launch failed`，补上 ptrace 即通 —— 与 F18 的
  `PER_UID_NO_PTRACE_WARNING` 同源。
- **F1（2026-09-11）非 root 形态端到端**：uid 65534 容器 +
  `--cap-drop ALL --cap-add SETUID,SETGID,CHOWN,DAC_OVERRIDE`、真镜像（含两个 broker）：
  worker 起来后建两个 chroot 沙箱，宿主侧 workspace 是 `0770 21000:<worker gid>`
  / `0770 21001:<worker gid>`、两个槽位进程都是 `sandlock-supervise --policy <ws>/.route-b/<uid>/… --uid <uid> …`、
  跨 uid 删除被拒（`rm: cannot remove … Operation not permitted`）、跨 uid 改写被拒
  （`touch: cannot touch … Permission denied`）、沙箱内 `pwd`/`pwd -P` 都是 `/home/user`、
  槽位 `/proc/<pid>/status` 是 `CapEff=0`。逐条原始输出见 `tmp/f1/f1-e2e.log` +
  `tests/contract/test_nonroot_route_b.py`（该文件同时加进 `test-prod-shaped.sh` 的
  unprivileged phase）。

> ✅ **backlog #25 已收口（A4/A5/A6）**：上面那条「没有 `SYS_ADMIN` ⇒ 共享卷退化成不可用的
> 符号链接」的缺口不再成立 —— A4 删掉 `mount --bind`（卷视图由请求路径决定）、A5 补齐卷根
> 及祖先对租户 uid 的穿透位（§2.4.2）、A6 把配额与低端口 sysctl 也迁出 worker。现在
> **无 `SYS_ADMIN` 的部署要用共享卷不需要任何保留条件**；唯一代价是配额需要 quota-agent
> （§2.4.3），否则降级为「无 per-sandbox 硬限 + WARNING」。

**线上审计（2026-09-10，只读探测：跳板→目标机，无写入 / 不重启 / 无副作用 exec）**：仓库清单写的
`user: "65534:65534"` **不等于已部署状态** —— 远端 `/opt/sandlock/docker-compose.prod.yml`
没有 `user:` 行、worker 镜像 `0.1.0-20260830-191728` 也没有 `USER` 声明 ⇒ **线上 worker 实际
以 root 跑**，`CapEff=0xa82425fb`（Docker 默认集 + `SYS_ADMIN`，**无 `SYS_PTRACE`**）。逐项：

| 检查项 | 实测 | 判定 |
|---|---|---|
| §2.4.1 的沙箱侧最小集 | root，`SETUID`/`SETGID`/`CHOWN`/`DAC_OVERRIDE` 都在默认集里 | 满足 |
| wheel 具备 route-B 语言面 | `sandlock_supervise_connect_fd` = **False**、`sandlock/bin/sandlock-supervise` **不存在**（`setpriv` 在、Landlock ABI 6） | **不满足** |
| 多 worker uid 段不重叠 | `worker-1`/`worker-2` 共用同一 `sandbox-shared` 卷，两边都没设 `E2B_UID_POOL_START` ⇒ 都从 10000 起 | **不满足** |
| 存储支持属主 / 配额 | 卷是 xfs ✓，但挂载选项是 `noquota`，且镜像内 `xfs_quota: not found` | 部分（配额仍降级） |
| 非特权 userns（F18 自映射前置） | kernel 6.12，`user.max_user_namespaces=30519` | 满足 |
| 在线沙箱 | worker 内只有 2 个进程（python + sh）；`sbx_*` 目录多为无记录残留（属主 `0:0 755`） | 空载，适合升级窗口 |

⚠️ 现网既是 **root worker** 又配了 `E2B_BASE_IMAGE`（chroot 形态在用）还**拿不到槽位**，
正好落在上面「删档的后果」那一格里 ⇒ **先用带新 wheel 的镜像 `build-and-push`，再升代码**，
顺序反了就会出现「镜像 rootfs 沙箱全部建不出来」；uid 段也要在同一次变更里拆开。
升级顺序、待授权事项与取证脚本见 `docs/HANDOFF.md`「特权最小集实测 + 线上就绪审计」。

### 2.4.2 共享卷路径的可穿越性（证据来自 A0/A3 探针实测，A5 沿用）

沙箱的路径中介以**沙箱自己的 host uid** 打开卷的宿主路径（route-B 槽位 / RunAs），
所以从 `/` 到卷视图的**每一级目录**都必须对该 uid 有 **o+x（可穿过）**；卷根自己的
`1777` 不够。**祖先不可穿过 ⇒ 卷视图整体不可用（绝对与相对同命）**：中介把
`/workspace/<rel>` 解析回宿主卷路径后逐级打开，缺哪级就死在哪级，沙箱里只看到一句
没有上下文的 `Permission denied`。

下表来自探针 `tmp/vol_fs_mount_probe.py` 的场景 `symlink-tight-ancestor`
（**A0/A3 轮次实测，日志 `tmp/a0-probe.log`**；A5 本轮沿用该结论，**未重跑**宿主探针，
现场复现归 Track Z 的本地部署测试）：

| 宿主卷路径祖先链 | 绝对路径 `cat /workspace/mnt/data/x` | 相对 `cat mnt/data/x` |
|---|---|---|
| 逐级 `0711`/`0755` | ✅ exit 0 | ✅ exit 0 |
| 某一级 `0700` | ❌ EACCES | ❌ EACCES |

（探针原始字段：`perms="0700"`、`abs-read=1`、`rel-read=1`；控制组 `perms="0755"` 时
`abs-read=0`、`rel-read=0`。绝对路径同命的原因是两条路径最终都由中介以同一个 uid
打开同一个宿主对象，DAC 判定完全相同。）

要求与实现：

- **卷根** `1777`（sticky，跨 uid 共享）+ 属主 = 第一个挂载该卷的沙箱 host uid；
  **每沙箱切片** `0770` + 属主 = 该沙箱 host uid、属组 = worker 的 gid（fix round 1 / c1：owner 语义不变，穿透补位**不**下放到切片）；
  **卷根及其全部祖先**补到 `0711` —— 原本就是 `0755`/`1777` 的更宽目录保持原样，
  只补缺的 x 位。
- E2B 行为：`provision_sandbox_volume_mount(..., host_uid=...)` 在应用卷根权限时调用
  `_ensure_traversable()` 沿 `resolve()` 祖先链补 o+x（`resolve()` 抛 `OSError`
  ——符号链接环等——时退回字面父链并只打一条 WARNING；`envd_service/volumes.py`）；
  worker 启动时用 uid 池的第一个 uid 做一次可穿透性自检，失败打一条 WARNING
  （点名目录、实际 mode、修法）。
- **部署要求**：`E2B_SHARED_VOLUME_ROOT` 的每一级祖先（例如 `/var/lib/e2b-volumes`、
  `/var/lib`）都要 `0711`/`0755`；只把卷根设成 `1777`，在缺祖先 x 位时整卷不可达。

> 历史成因：删掉 `mount --bind` 材料化之前，bind 把卷内容物理复制进 workspace
> 子路径，缺 o+x 的祖先链被绕过，症状因此被 backlog #25 **记成**「只有相对路径
> EACCES」——探针（上表）表明缺祖先 x 位时绝对路径同样不可用。A4 删 bind（虚拟路径
> 由请求决定）、A5 补穿透位之后，无 `CAP_SYS_ADMIN` 的共享卷形态才在「绝对 + 相对」
> 两个方向上都完整。

### 2.4.3 配额能力的来源：quota-agent（A6）

worker 的**本地直连** `xfs_quota -x` 需要 effective `CAP_SYS_ADMIN`（内核按 cap 门控，不看
euid），而部署形态的 worker 是 uid 65534（Docker 对非 root 清零 CapEff），本地直连在
生产里本来就不可用。A6 把链路定成**一条**：配额由服务端 **quota-agent** 提供，worker 只发
HTTP，`SYS_ADMIN` 只留在 agent 上。

- **开关是 URL**：`E2B_QUOTA_AGENT_URL` 存在即启用 agent 形态（优先于 `E2B_QUOTA_VIA_AGENT`，
  后者默认 false）。同时配 `E2B_QUOTA_AGENT_TOKEN`（与 agent 同值，`X-Internal-Key`）。
  `E2B_QUOTA_VIA_AGENT=true` 而 URL 为空仍是受支持的误配：启动打一条 WARNING、配额降级。
- **合体节点（W4）**：`E2B_ENABLE_LOCAL_NODE=true` 时控制面在**自己的进程里**建卷配额
  （`control_plane/api/sandboxes.py`），它的 `via_agent` **跟随同一个开关**（不再硬编码
  `false`），并且合并镜像不跑 `envd_service.app.create_app` ⇒ agent hooks 由控制面启动时
  接上。缺了接线这一步，一个**配好了** `E2B_QUOTA_AGENT_URL` 的合体节点仍会被报成
  「quota-agent not configured」并静默降级。非 root 的合并镜像既没有 `xfs_quota` 也没有
  `CAP_SYS_ADMIN`，本地直连本来就不可用（§2.4.1 的 `SYS_ADMIN` 行）。
- **清单 + 发布流程（A6 fix-1）**：`deploy/stack/docker-compose.prod.yml` 的 `quota-agent`
  服务（`profiles: ["quota"]`、`cap_add: SYS_ADMIN`、与 worker 挂同一份 `sandbox-shared`）。
  一条命令开启：`./deploy/scripts/upgrade.sh --with-quota-agent` —— 它把
  `QUOTA_AGENT_PROFILE=1` 写进 `.env`（粘性，后续 upgrade 自动带 `--profile quota`）、
  worker 没配 URL 时指向 `http://quota-agent:49984`、并把 `QUOTA_AGENT_IMAGE` 固定成
  `<registry>/<ns>/e2b-sandlock-quota-agent:<VERSION>`。该镜像与 worker 走同一个发布流程：
  `deploy/scripts/build-images.sh`（`build-and-push.sh` 调用）多平台构建并推送它。手动形态：
  自己设 `E2B_QUOTA_AGENT_URL/TOKEN` + `docker compose --profile quota up -d`。
  关闭用 `--without-quota-agent`：写回 `QUOTA_AGENT_PROFILE=0`、清掉栈内
  `E2B_QUOTA_AGENT_URL`（worker 转本地直连 ⇒ 非 root 降级 + WARNING），并在目标机
  **显式** `docker compose --profile quota rm -sf quota-agent` —— compose **不会**因为服务
  离开 active profile 就停掉容器（实测 compose 5.1.2：不带 profile 的
  `up -d --remove-orphans` 仍让 agent 运行），所以这里不依赖 `--remove-orphans`；外置 agent
  的 URL 会保留（配额仍由它提供，特权在它那边）。注意 `--keep-image-tags` 与
  `--with-quota-agent` **不能**合用：前者跳过 tag 固定 ⇒ `QUOTA_AGENT_IMAGE` 为空 ⇒ 目标机
  回落到拉不到的 `e2b-sandlock-quota-agent:latest`，脚本会 fail fast 并点名该组合。
  NFS 服务器形态见 `deploy/compose/docker-compose.quota-agent.yml` +
  `E2B_QUOTA_AGENT_PATH_MAP`（外置 agent 时不要开 `QUOTA_AGENT_PROFILE`）。
  `deploy/k8s/worker.yaml` 已不再声明 `SYS_ADMIN`（把 `E2B_QUOTA_AGENT_URL` 指向集群内或
  外部的 agent；k8s 的共享卷是 RWX PVC，走 NFS 时本地直连本来就不可能）。
- **启动竞态（W6）**：worker 与 quota-agent 同一次 rollout，但**不同容器**，worker 完全可能
  先起。启动探针（`QuotaMonitor` 的首次扫描 + 启动期 orphan 对账）现在先做一次**有界**
  就绪等待（`quota_agent.wait_for_startup_readiness`：8 次 × 2.5s ≈ 最多 17.5s，两处共用一个
  判定），三种结论**分开表达**：URL 为空 ⇒ 不等待、保留原来的显式 WARNING
  （`quota-agent not configured`）；只是还没起来 ⇒ 重试，成功后在 **INFO** 里说明最终结果
  （`quota-agent answered on startup attempt N/8`）；窗口耗尽仍不可达 ⇒ INFO 记最终结果 +
  调用点照常打原来的 WARNING ⇒ **真降级不会被掩盖**。等待跑在 worker 线程上（监控扫描线程 /
  启动对账线程），**不阻塞事件循环与心跳循环**；建箱路径的探针**不做**这种等待（它不能为了
  等 agent 而拖住请求，单个沙箱拿不到限额本身就是要暴露的真降级）。回归钉在
  `tests/unit/test_quota_agent_startup_race.py`。
- **降级语义（不变）**：agent 不可达/401/协议错误 ⇒ `ProjectQuotaError` ⇒ **建箱与挂卷成功、
  无 per-sandbox 限额 + WARNING**。agent 形态不会在 worker 上执行任何本地配额命令，回归钉在
  `tests/unit/test_xfs_project_quota_agent.py::test_agent_form_never_shells_out_to_a_local_quota_tool`。
- **低端口 sysctl（第 ③ 处用途）**：wildcard allowOut 的 DNS 网关绑 `<127.0.1.x>:53`
  （`resolv.conf` 带不了端口，且**默认的共享 netns 形态就是由 worker 父进程绑它**）。两个
  部署形态都由容器 spec 声明
  `sysctls: net.ipv4.ip_unprivileged_port_start=0` —— 运行时应用，worker 不写 sysctl、不持
  `SYS_ADMIN`：compose 的 `sysctls:`，k8s 的**pod 级**
  `spec.template.spec.securityContext.sysctls`（`deploy/k8s/worker.yaml`）。
  **2026-09-16 起：compose 侧不再声明这条 sysctl**——两个 worker 都跑 per-sandbox netns，
  wildcard-DNS 的 `:53` bind 落在沙箱自己的 netns 内，沙箱在自身 userns 里是 root
  （`CAP_NET_BIND_SERVICE` 覆盖 53），host 侧 sysctl 无关（fork: `context.rs`）。k8s 清单仍是
  共享 netns 形态，继续保留 pod 级声明。
  ⚠️ **不能用 `NET_BIND_SERVICE` 代替**：worker 镜像（`deploy/docker/Dockerfile.envd`）
  以 `USER 65534:65534` 构建、pod 也没有 `runAsUser: 0` ⇒ containerd 对非 root 清空
  effective 集（实测 `CapEff=0`，没有 ambient caps），内核默认
  `ip_unprivileged_port_start=1024` 下 bind `:53` = **EACCES**（本仓实测：同一 uid 把该值
  声明为 0 即 OK；root + `NET_BIND_SERVICE` 才在 1024 下也 OK —— 早期推送的镜像仍是 root，
  但清单以 Dockerfile 的 `USER` 为准，声明这条 sysctl 对两种形态都安全）。该 sysctl 自 k8s 1.22 起属
  **safe sysctl**（无需 kubelet `--allowed-unsafe-sysctls`）；`hostNetwork: true` 下 `net.*`
  会被拒。MCP 入站端口是 50005+，从来不需要低端口窗口。

### 2.4.4 k8s 清单形态的配额口径：降级（2026-09-13 裁定，W4）

`deploy/k8s/` 发布 worker / control-plane / gateway / autoscaler / redis / PVC / namespace
七份清单，**不含 quota-agent**，所以 k8s 形态的默认口径就是**配额降级**（不是缺陷，是口径）：

- 建箱、挂卷、命令、快照全部照常；**没有** per-sandbox 磁盘硬限 —— `xfs_project_supported`
  找不到可用配额来源，挂卷回落到卷根，worker 按 §3.1 的既有披露打一条 WARNING。
- 后果（部署/验收前必须知道）：
  1. 单沙箱写满共享卷会影响同卷的其他租户（没有 `bhard` 兜底），容量只能靠
     `E2B_NODE_DISK_MB` 一类的节点级预算和监控；
  2. 对账链上的 `release_project` / `reconcile_orphan_projects` 也需要一个配额来源；
     降级形态下**没有**项目行可释放（从来没建过），所以**不要**在降级形态下手动
     `xfs_quota -x` 建行 —— 那会造出没人回收的孤儿行，正是 §2.5 那批 follow-up 的形状；
  3. 升级说明与验收结论里不能写「有硬限」。
- 需要硬限时：把 `E2B_QUOTA_AGENT_URL`/`E2B_QUOTA_AGENT_TOKEN` 指向**自己部署**的 agent
  （集群内、集群外都行），前提是它对 worker 看到的那份存储执行 `xfs_quota -x`。k8s 的共享卷
  是 RWX PVC（NFS/CephFS），XFS project quota 只在该 PVC 后端**真的是 XFS**、且 agent 能拿到
  设备/挂载点时成立；NFS 形态要配 `E2B_QUOTA_AGENT_PATH_MAP`（见
  `deploy/compose/docker-compose.quota-agent.yml` 与 §5.2）。
- **为什么不随清单发 agent**：agent 需要 `SYS_ADMIN` + 宿主机上真实的 XFS 设备/挂载点；
  compose 形态能直接给它 `cap_add: SYS_ADMIN` + 同一份宿主卷，k8s 的等价物是「特权 pod +
  PVC/PV 后端语义 + `PATH_MAP` + 节点亲和」——**本仓没有验证过的部署面**。与其发一份没人跑通过
  的清单，不如把口径写死在这里；等有真实 k8s + XFS 环境再补清单，并把本节改成「已提供」。
  `deploy/k8s/worker.yaml` 的 A6 注释块指向本节。

### 2.4.5 worker seccomp 面：从 `unconfined` 收敛到「默认档 + 2 条」（2026-09-15）

**结论**：worker 容器不再用 `seccomp=unconfined`，改用
`deploy/seccomp/sandlock-worker.json` —— **Docker 默认 profile 加恰好两条 syscall**
（`pidfd_getfd`、`unshare`；做法是把这两个名字从各自的 capability 门控组移到无条件白名单，
其余条目一律不动）。理由、证据与重新生成方式见 `deploy/seccomp/README.md`。

**为什么是这两条**（2026-09-15 实测，x86_64 容器 `CapEff=0xa80425fb`，无
`SYS_ADMIN`／`SYS_PTRACE`；同容器只换 seccomp 档，按 errno 判定）：

| syscall | 默认档 | unconfined | 判定 |
|---|---|---|---|
| `pidfd_getfd` | `EPERM` | `EBADF` | **profile 拦**：sandlock 用它取子进程的 seccomp-notif fd（`sandbox.rs::dup_child_fd`），缺它沙箱建不起来 |
| `unshare(CLONE_NEWUSER)` | `EPERM` | `ok` | **profile 拦**：per-sandbox host uid（E3.2）、route-B F18 自映射、`net_isolation`、`pid_ns` 都靠它 |
| `unshare(NEWNET/NEWPID/NEWNS)` | `EPERM` | `EPERM` | **内核 capability 拒的，不是 seccomp**（容器无 `SYS_ADMIN`） |
| `pidfd_open`、`process_vm_readv/writev`、`seccomp(SET_MODE_FILTER)`、`setgroups`、`ptrace` | 放行 | 放行 | 默认档本来就够，无需补白 |
| `mount`、`keyctl`、`bpf`、`clone3` | `EPERM`/`EPERM`/`EPERM`/`ENOSYS` | `ENOENT`/`EINVAL`/`EINVAL`/`EINVAL` | 保持拒绝不动（不需要） |

端到端（真实建箱 + 执行命令）：默认档 ❌ 建箱失败；默认档 + **仅** `pidfd_getfd` ✅；
默认档 + **仅** `unshare` ❌；两条齐（或 unconfined）✅。⇒ `pidfd_getfd` 是基线必需，
`unshare` 是命名空间类路径必需；当前 `E2B_PER_SANDBOX_UID` 默认开且 route-B 槽位在用，
**所以两条都要**。

**安全边界（该收敛的关键前提）**：沙箱**不继承**这份放宽 —— 沙箱内
`unshare(CLONE_NEWUSER)` 在本 profile 与 unconfined 下**都是 `EPERM`**（sandlock 自己的
过滤器挡的，实测）。放宽面只到 worker/supervisor。

**落地要求**：

- **compose**（`deploy/compose/docker-compose.prod.yml`、`deploy/stack/docker-compose.prod.yml`）：
  `seccomp=../seccomp/sandlock-worker.json`。Compose 读取该文件并随请求下发，相对路径按
  compose 文件所在目录解析（已实测：Compose 渲染出的容器里 `unshare(NEWUSER)` 成功）。
  ⚠️ **只拷单个 compose 文件的部署（线上 `/opt/sandlock/` 那种）没有 `../seccomp/`**
  ⇒ 必须把 profile 一起放上去并用 `E2B_SECCOMP_PROFILE=<绝对路径>` 指过去；用
  `docker compose config` 检查渲染值，别等到 `up` 才失败。
- **k8s**（`deploy/k8s/worker.yaml`）：`seccompProfile: {type: Localhost,
  localhostProfile: sandlock-worker.json}`。Localhost profile 是**节点本地状态**：kubelet
  在节点的文件系统上按 seccomp 根目录（默认 `/var/lib/kubelet/seccomp`）解析它 —— 所以
  **ConfigMap 挂在 worker pod 里是没用的**，内容必须落到节点上。
  `deploy/k8s/seccomp-installer.yaml`（ConfigMap + DaemonSet，2026-09-16 起）就是这个落地
  组件：ConfigMap 逐字携带 `deploy/seccomp/sandlock-worker.json`，DaemonSet 每节点把它原子
  写入 `/var/lib/kubelet/seccomp/sandlock-worker.json`（`DirectoryOrCreate`，覆盖含 tainted
  control-plane 在内的所有节点）。
  - **顺序**：先 `kubectl apply -f deploy/k8s/seccomp-installer.yaml`，等 DaemonSet 在每个
    节点 Ready，再滚 worker Deployment —— 缺文件的节点会直接起不来（fail closed，符合预期）。
  - **改 profile**：改 `deploy/seccomp/sandlock-worker.json` → 跑
    `tests/unit/test_worker_manifest_permissions.py`（它逐字比对嵌入副本、并用 sha256 钉住
    `checksum/profile` 注解）→ `kubectl apply`。注解在 pod template 上，内容一变就触发
    DaemonSet 滚动；容器另有 5 分钟兜底重查，漏滚也会收敛（kubelet 每次建容器读文件，不需要
    重启 kubelet）。
  - **权限**：DaemonSet 只需要 `runAsUser: 0` 写那一个目录，`capabilities.drop: [ALL]`、
    `allowPrivilegeEscalation: false`、`readOnlyRootFilesystem: true`，无 `privileged`/
    `hostNetwork`/`hostPID`；SELinux enforcing 的节点若报 `Permission denied`，按清单注释加
    `seLinuxOptions: {type: spc_t}`。
  - 若集群改过 kubelet 的 `--seccomp-default-root`，hostPath 要指向同一个目录；手工拷文件仍
    可作单节点应急，但可复现路径是 DaemonSet。
  顺带：`Localhost` 是 Pod Security `baseline` 的允许值，而 `Unconfined` 不是。
- **启动自检（2026-09-16，A7 follow-up）**：worker 起服务前会验证自己**确实**跑在
  profile 下（`envd_service/config.py::check_seccomp_filter`，在 `create_app` 第一步调用，与
  net-isolation 配对守卫并排）。两层，各自抓一种**静默**失效：
  1. `/proc/self/status` 的 `Seccomp:` 必须为 `2`；`0` = 完全没过滤（profile 被丢、写成
     `seccomp=unconfined`、或运行时忽略了未知 profile）⇒ 抛 `SECCOMP_FILTER_MISSING`。
     这才是 A7 要消除的形态：沙箱会继承 worker 的整个系统调用面，而日志里什么都没有。
  2. `Seccomp: 2` 之后再做一次**主动探针**：子进程里 `unshare(CLONE_NEWUSER)`。shipped
     profile 无条件放行这条；而**运行时默认档**把它按 `CAP_SYS_ADMIN` 门控（worker 没有该
     cap）⇒ 探针 EPERM 即抛 `SECCOMP_PROFILE_NOT_APPLIED`。这正是 **k8s 节点缺 Localhost
     profile 文件时的退化形态**：kubelet 会静默跳过缺失文件、pod 照起（kubernetes#124944，
     1.28/1.29），只有建箱时才失败。探针结果按进程缓存，不会每次建 app 都起子进程。
     若 EPERM 其实来自宿主限制（`kernel.apparmor_restrict_unprivileged_userns=1` 或
     `user.max_user_namespaces=0`），自检**点名宿主原因**并降级为 WARNING，而不是归咎 profile。
  - 逃生口：`E2B_REQUIRE_SECCOMP_FILTER=0` 把两层都降为 WARNING —— 测试 runner
    （`deploy/compose/docker-compose.test.yml` 故意 `seccomp=unconfined`）与 autoscaler 的
    local backend（`autoscaler/backends/local.py` 拉起 worker 时同样 unconfined）已各自声明。
  - 真实容器三态验证（2026-09-16，本机 Docker）：`seccomp=unconfined` ⇒ 抛
    `SECCOMP_FILTER_MISSING`（`Seccomp: 0`）；`seccomp=deploy/seccomp/sandlock-worker.json`
    ⇒ 返回 `2` 不抛；**不加任何 `--security-opt`**（Docker 默认档，即 k8s 静默跳过的退化态）
    ⇒ 抛 `SECCOMP_PROFILE_NOT_APPLIED`。回归：`tests/unit/test_seccomp_selfcheck.py`（11 条）。
- **生产形 lane**：`deploy/scripts/test-prod-shaped.sh` 与
  `deploy/scripts/smoke-prod-worker.sh` 改用同一文件（`SECCOMP_PROFILE=` 可覆盖）
  ⇒ 门禁跑的就是上线形态，而不是比它更宽的形态。

**仍然 `unconfined` 的两处（有意保留）**：`docker-compose.prod.yml`/`docker-compose.stack` 里的
`moby/buildkit:rootless`（构建器需要完整 syscall 面，与沙箱能力无关）；
`docker-compose.test.yml` 的测试镜像（要跑 loop/XFS/mount 依赖的那批门禁）。

**本地可验证（2026-09-15 更正）**：先前记的"本机内核（OrbStack）写 `uid_map` 一律 EPERM"
**是错的**。本地 lane 用上线同款形态跑 `tests/contract/test_route_b_executor.py` 实测
`14 passed`，槽位日志报 `guest-uid=uid-0-in-userns`（含 `SYS_ADMIN` 与
`PROD_DROP_CAPS=SYS_ADMIN` 两种形态都一样）⇒ **本机就能建 userns 并完成 F18 自映射**。
卡住早期探针的是**容器的 seccomp 档**（Docker 默认档把 `unshare` 挂在 `CAP_SYS_ADMIN`
门控组上），不是内核；机制细节见 §2.4 的"二次更正"。
推论：`net_isolation` 的端到端**本机就能验**；`pid_ns` 缺的是 envd 开关与中间进程的自映射兼容
（代码问题），与环境无关。`test-prod-shaped.sh` 全量的 1439 passed 也正是在本机取到的。

**netns 形态本机也验过了（2026-09-16）**：`E2B_TEST_NET_ISOLATION=1
E2B_ENABLE_NET_ISOLATION=true E2B_FD_INJECT_CONNECT=true` 跑全量 =
`1439 passed / 3 skipped / 0 failed`（`tmp/prod-shaped-netns-on.log`），与共享 netns 那次
逐项相同；netns 契约三条（MCP 全链路经代理、CPython `connect()` 走 fd 注入、通配域名）本地
`3 passed`。⇒ **"能不能跑"这一层已经没有悬念**；开与不开的取舍落在语义与容量上。

### 2.4.6 netns 的代价：逐条实测（2026-09-16，本机）

开了 `net_isolation` 与没开，**在同一条 lane、同一份 profile 下**各跑一遍同类探针
（`127.0.0.1` 与**非 loopback** 目标各一次；`connect` / 非阻塞 `connect` / `bind+listen`
三种 socket 用法），逐项对比：

| 观测项 | 共享 netns（线上现状） | netns（fd 注入） | 判定 |
|---|---|---|---|
| `getsockname()`（loopback 目标） | `127.0.0.1:<源端口>` | `127.0.0.1:<源端口>` | **无差异** |
| `getsockname()`（**非 loopback** 目标） | `192.168.139.2:<源端口>`（**worker 的地址**） | `192.168.139.2:<源端口>` | **无差异** ⇒ 文档里"显示宿主地址"那条**不是 netns 引入的**，共享 netns 已如此（沙箱本来就在 worker 的网络命名空间里） |
| `getpeername()` | 真实目标 | 真实目标 | 无差异 |
| `bind()+listen()` 后 `getsockname()` | 正确 | 正确（`inbound.rs` 就在自己 netns 内 bind） | 无差异 |
| **非阻塞 `connect_ex()`** | `115 EINPROGRESS`，此时 `getpeername()` 不可用 | **`0 OK`**，`getpeername()` 可用 | **唯一实证的语义差异**（方向还是"更友好"），但依赖 EINPROGRESS + 可写事件等待的代码路径会走不同分支 |
| 连接建立 p50 / p95（200 次短连接） | **0.034 / 0.082 ms** | **0.291 / 0.560 ms** | **~8.5×**；绝对值 0.3 ms/连接（≈3400 conn/s 单线程），长连接/连接池无感 |
| 入站可达性 | 同 netns ⇒ 外部可直连沙箱端口 | 必须有 `net_bind_map` | **差异，但不构成功能缺口**：全库没有 `get_host`/端口暴露语义（`rg get_host` 无命中），唯一入站消费者是内部 MCP 网关，它由 §2.9 的端口池自动映射 |
| 端口带容量 | n/a | `61000–65535` = 4536 个 | **不是瓶颈**：节点上限 ≤100 沙箱（`E2B_MAX_SANDBOXES` 默认 100）⇒ 45× 余量 |

**结论：没有发现阻塞项。** 需要管理的只有两件：
① 两个开关必须同时开 —— 只开 `enable_net_isolation` 而不开 `fd_inject_connect` 时，
沙箱会变 loopback-only 且"网络全断"表现为超时而非报错。**已改为启动自检 fail-fast
（2026-09-16）**：`create_app` 第一步调用 `config.check_net_isolation_pairing`，命中即抛
`NET_ISOLATION_PAIRING_ERROR`（点名两个变量 + 症状 + 两条出路），worker 拒绝启动而不是带着
一张"网是坏的"的沙箱跑起来。确实想要"不能出网"的沙箱时显式声明
`E2B_NET_ISOLATION_ALLOW_LOOPBACK_ONLY=1`（启动时另打一条 WARNING）。
回归：`tests/unit/test_net_isolation_config.py`（配对的四种组合 + `create_app` 拒绝 + 开关解析）；
② 连接建立速率（~8.5×）是唯一的量化代价，短连接密集的负载需按此评估。
未测项剩下 UDP/ICMP 边缘（无 connect 的 datagram、组播/广播）与真实 SDK 负载下的行为。

### 2.4.7 netns 全量（2026-09-16：灰度 → 全量）

**结果**：灰度期间量到 MCP 入站路径每请求 +390 ms（§2.4.7 下方实测 + 根因 + fork 侧
`net_bind_inject` 修复），修复上线后 worker-2 的 `/mcp` p50 回到 29.1–29.7 ms、与共享形态持平
⇒ **2026-09-16 全量**：两个 worker 都在 `&worker-env` 上带
`E2B_ENABLE_NET_ISOLATION=${E2B_ENABLE_NET_ISOLATION:-true}` +
`E2B_FD_INJECT_CONNECT=${E2B_FD_INJECT_CONNECT:-true}`（worker-2 保留自己的
`*_WORKER2` 覆盖以便单节点回滚），**容器级 `ip_unprivileged_port_start=0` 已从 compose 撤掉**
（它的唯一用户是 wildcard-DNS 的 `:53`，netns 形态下该 bind 发生在沙箱自己的 netns，
root-in-userns 自带 `CAP_NET_BIND_SERVICE`；k8s 清单仍是共享 netns，继续保留 pod 级那份）。

以下为灰度期的记录，保留作追溯：**形态**：`deploy/stack/docker-compose.prod.yml` 里**只有 worker-2** 带
`E2B_ENABLE_NET_ISOLATION=true` + `E2B_FD_INJECT_CONNECT=true`（默认即 true，可用
`E2B_ENABLE_NET_ISOLATION_WORKER2`/`E2B_FD_INJECT_CONNECT_WORKER2` 覆盖），worker-1 保持共享
netns ⇒ 一套栈上两种形态并存，用真实流量判断。

**为什么可以并存**：沙箱的节点归属在创建时就定死（由控制面调度），单个沙箱不会在两种形态间迁移；
按 §2.4.6 的实测，两端对外只有"连接建立 ~8.5×"与"非阻塞 connect 立即成功"两处差异。

**回滚（一条命令）**：把 `E2B_ENABLE_NET_ISOLATION_WORKER2` / `E2B_FD_INJECT_CONNECT_WORKER2`
设为 `false`（或删掉那两行）再跑 `upgrade.sh` —— 容器重建回共享 netns，不涉及镜像与数据回滚。
若只开了一个开关，worker-2 会**拒绝启动并打印原因**（上面的配对守卫），这是预期行为。

**观察清单（开灰度后 24h）**：

1. **连接建立延迟**：worker-2 的首命令/建连指标 vs worker-1（预期 +0.3 ms 量级，只影响建连，
   稳态数据面不变）。
2. **超时率**：SDK 侧 `timeout` / 连接失败类错误**按节点分组**对比 —— netns 下的失败会表现为
   超时而不是"connection refused"。
3. **MCP 网关**：netns 沙箱的 `/mcp` 代理可用性 + 端口带水位（`61000–65535`，§2.9）。
4. **DNS / 通配域名**：解析是否与共享形态一致（沙箱内 `ip addr` 只见 `lo` 是预期）。
5. **worker 日志**：不应出现 `net_isolation enabled without fd_inject_connect`（配对守卫会先拒绝
   启动）；容器非 0 退出 = 配置被拒，读错误原文即可定位。
6. **内核资源**：`user.max_user_namespaces` 水位（每沙箱 +1 个 netns；目标机实测 30519）、
   `ip netns list | wc -l` 应 ≈ 该节点并发沙箱数。

**退出到全量的判据（已按实测判据执行，未等 24h）**：原判据是 24h 内 1–4 无回归、无客户可感知
差异；但车队没有真实流量，"24h" 事实上不会产生证据，所以改用**按节点分组的定向实测**替代：
命令 RTT、wildcard DNS、MCP `/mcp` p50/p95 两种形态逐项对齐（见下方修复后表格），再切 worker-1
并撤掉容器级 `ip_unprivileged_port_start=0`。

**2026-09-16 灰度实测：暂缓全量。** 按节点分组的合成负载（官方 MCP 客户端、每请求新建连接，
即 envd 代理的真实行为）量到一条 §2.4.6 没覆盖的回归：

| 指标 | worker-1（共享 netns） | worker-2（netns） |
|---|---|---|
| MCP `/mcp` p50 | **19.3 ms**（18.7–42.9） | **375.9 ms**（375.1–398.9） |
| 同连接复用（官方客户端 pooled） | 6.9–9.9 ms | 394–498 ms |
| 命令 RTT p50 | 34.3 ms | 33.9 ms |
| wildcard DNS | ok（10.250.0.2） | ok（10.250.0.2） |

三段拆分（`tmp/mcp-3way.py`：测试自己的 MCP server 在工具处理里打时间戳，客户端在 worker 侧
打时间戳，同一宿主墙钟）定位到**传输路径**而不是网关逻辑：

| 阶段 | worker-1 | worker-2 |
|---|---|---|
| 去程（client 发出 → server 收到） | 5.3 ms | **170–183 ms** |
| server 自身处理 | 0.2 ms | 0.2 ms |
| 回程（server 回复 → client 收到） | 2.3 ms | **226 ms** |

排除项：裸 TCP `connect()` 两种形态都是 0.1–0.2 ms（不是建连）；池化连接同样付费（不是每连接）；
`/mcp` 的 401（网关鉴权中间件产生的一行响应）在 netns 侧也要 ~75 ms（不是 MCP 协议层）；
netns 箱内 DNS 是"快速失败"（0.4 ms，不是解析）。⇒ 每请求 ~390 ms 落在 netns 独有的
**入站映射 + fd 注入 socket 的数据面**（`network/inbound.rs`：host listener → eager-accept
队列 → seccomp 注入的 `accept()`），fork 侧路径。

**机制（代码级定位，2026-09-16）**：`network/readiness.rs` 为"事件循环型服务器"合成就绪
——host 侧排队的连接不会让沙箱自己的 listener 变可读，所以 fork 拦截
`ppoll`/`epoll_pwait`（`seccomp_plan.rs::INBOUND_MAPPING_SYSCALLS`，**仅在
`features.inbound_port_map` 打开时**注册），supervisor 复制被监视的 fd、按
`POLL_SLICE_MS = 20` 切片轮询，再把组合好的 events 写回子进程。关键触发条件在
`handle_poll_impl`：**被 poll 的 fd 集合里只要有"被映射的 listener"，整次调用就走合成路径**
（`any_mapped` 为真即 `Defer`，否则 `Continue` 交还内核）。MCP 网关进程的 epoll 集合里永远
有它自己的 listener（否则它没法 accept），而箱内 uvicorn 跑在 uvloop 上（`uvloop True`，
纯 epoll 驱动）⇒ **该进程每一次事件循环等待都要过一次 supervisor**，一个请求在去程/回程各
经历若干次事件循环迭代，累计成测到的 170 / 226 ms。

这也解释了为什么只有 MCP 路径慢：命令 RTT（34 ms，两形态一致）与 stdio server 自身
（0.2 ms）都不带被映射的 listener，不触发拦截；401 只走少数几次迭代（~75 ms）。

修复方向（fork 侧，任选其一或组合）：
① 快路径先行——进入切片循环前先以 timeout=0 试探一次复制 fd 与 `pending`，就绪即立刻返回，
不再 `Defer`；
② 去掉每次调用的 `dup_fd_from_pid` + `socket_ino` + 网络锁（epoll 路径已有 `epoll_ctl`
注册表可缓存 inode；`ppoll` 可按 (pid, fd) 做小缓存）；
③ 从设计上消掉这条映射——让网关改用 supervisor 交付的 socketpair 而不是在沙箱 netns 里
bind+listen，则 `inbound_port_map` 关掉、拦截整体消失（改动最大，收益也最彻底）。

修完要重跑同一条 A/B（`tmp/netns-node-compare.py`）确认收敛，才谈 worker-1 全量与撤
`ip_unprivileged_port_start`。

**修复已实现（方案 ③ 的落地形态，fork `fe492be`）**：`net_bind_inject` —— 映射端口的
`bind()` 不再让沙箱在自己 netns 里绑，而是由 supervisor 在 **worker netns 的
127.0.0.1:<host_port>** 建好 socket，再用 `SECCOMP_ADDFD_FLAG_SETFD` **替换沙箱的 socket fd**
（`fd_inject_connect` 已在用的机制）。此后沙箱的 `listen()`/`accept()` 全是内核在
宿主 netns socket 上的普通调用：listen 处理器与就绪合成都走 `Continue`（该 listener 不在
`NetworkState::inbound` 里），**supervisor 彻底离开数据面与就绪面**，主机监听器 / eager-accept
队列 / `poll`·`epoll_wait` 拦截全部不再参与。

边界与保证（同一次改动里写死）：

- **只对已映射的 TCP 端口生效**：其它 family / 临时端口 / 未映射端口一律 `Continue`，原有
  netlink cookie、`port_remap`、bind denylist 链路不变；
- **只绑 loopback**（127.0.0.1/::1，绝不 0.0.0.0），所以沙箱内 `getsockname()` 报的是
  loopback 地址而不是它请求的 `0.0.0.0`——与主机监听器方案对外暴露的地址一致；
- **fail closed**：宿主 socket 建不出来或绑不上（端口被占、无 loopback）就让沙箱的 `bind()`
  带着该 errno 失败，而不是退回"在自己 netns 里绑上了但外面够不着"；
- 构建期校验：`net_bind_inject` 必须同时有 `net_isolation` 与 `net_port_map`（否则报错拒跑）。

E2B 侧开关：`E2B_NET_BIND_INJECT`（默认 `true`，`envd_service/config.py`），经
`factory.py` → `SandlockExecutor(bind_inject=...)` → 策略里出现 `net_bind_inject`（
`route_b.py` 的 wire 字段白名单同步加了这个键）。回滚只需把该变量设为 `false` 并重建 worker
镜像/重启，策略立刻回到主机监听器映射。

**上线实测（2026-09-16，随 `0.1.0-297-g15d4726` 发布，worker-2 仍是唯一 netns 节点）**：
同一条按节点 A/B（`tmp/netns-node-compare.py`）在修复前后对比：

| 指标 | worker-1（共享） | worker-2 修复前 | worker-2 修复后 |
|---|---|---|---|
| MCP `/mcp` p50 | 19–34 ms | **375.9 ms** | **29.1–29.7 ms** |
| MCP p95 | 49–62 ms | ~399 ms | 37.8–48.7 ms |
| 命令 RTT p50 | 33.7 ms | 33.9 ms | 33.2 ms |
| wildcard DNS | ok | ok | ok |

三段拆分（`tmp/mcp-3way.py`）同步收敛：worker-2 的去程 170–183 ms → **5.3 ms**、回程
226 ms → **2.5 ms**（服务端自身仍是 0.2 ms），与 worker-1 的 5.3/2.3 ms 持平。

形状不变量（worker 容器内 `/proc/net/tcp`，决定性证据）：共享形态的监听是
`00000000:61001`（沙箱自己在 worker netns 里绑 0.0.0.0），注入形态是 `0100007F:61001`
——即 supervisor 建的 **127.0.0.1** host-loopback socket 被注入了沙箱 fd，与「只绑 loopback」
的设计一致。

门禁：fork `sandlock-supervise` 库测试 20/20；E2B prod-shaped lane 在 netns 形态整轮
EXIT=0、0 failed（`tmp/gate-inject-full2.log`）；`mediation_2uid` 的 6 个失败在干净 HEAD 上
同样存在（环境所致，与本次改动无关）。升级后两个冒烟（多节点 + 部署级，含 MCP 网关过代理）
全部通过。

**全量上线后的实测（`0.1.0-299-g97ad404`，两个 worker 同形态）**：

| 检查 | worker-1 | worker-2 |
|---|---|---|
| 容器开关 | `NET_ISOLATION=true` + `FD_INJECT_CONNECT=true` | 同 |
| 容器 `sysctls` | **`null`**（低端口窗口已撤） | **`null`** |
| 沙箱内接口 | `IFACES=lo`（自有 netns） | `IFACES=lo` |
| MCP `/mcp` p50 | 29.7–39.0 ms | 29.4 ms |
| 命令 RTT p50 | 33.1 ms | 32.9 ms |
| wildcard DNS | ok（10.250.0.2） | ok（10.250.0.2） |

wildcard DNS 在**没有容器级低端口 sysctl** 的情况下仍然可用，正是"`:53` 现在绑在沙箱自己的
netns、由 userns root 覆盖"的直接证据；`IFACES=lo` 则确认车队里不再存在共享 netns 的沙箱。

**因此：worker-1 保持共享 netns，`ip_unprivileged_port_start=0` 不撤**，等 fork 侧把这条
每请求代价定位并修掉后再走全量。复测脚本：`tmp/netns-node-compare.py`（按节点）、
`tmp/mcp-3way.py`（三段拆分）。

### 2.4.8 并发容量口径（2026-09-16 调整）

`E2B_MAX_TOTAL_*` 是**车队级**预算，`E2B_NODE_*` 是**每节点**容量，两者都以「每沙箱预留」
为单位计价 —— 而每沙箱预留来自 `E2B_DEFAULT_MEMORY_MB` / `_CPU_PERCENT` / `_DISK_MB` /
`_MAX_PROCESSES`（`control_plane/config.py`，SDK 的 stock create 不带这些维度）。所以
**并发上限 = 各维度预算 ÷ 每沙箱预留，取最小值**；清单现在把这四个 `E2B_DEFAULT_*` 也透传给
control-plane 与 worker（此前只在代码里有默认值 1024MB/100%/1024MB/256）。

当前口径（2026-09-16 起）：

| 维度 | 每沙箱 | 车队预算 | 车队并发 | 每节点预算 | 每节点并发 |
|---|---|---|---|---|---|
| 内存 | **512MB** | 8192 | 16 | 4096 | 8 |
| CPU | 100% | **800** | **8** | 400 | 4 |
| 进程 | 256 | **2048** | **8** | 1024 | 4 |
| 磁盘 | 1024MB | 10240 | 10 | 8192 | 8 |
| 沙箱数 | — | 100 | 100 | — | — |

⇒ **车队上限 8（CPU/进程维度绑定），每节点 4**。实测：一次 8 个 create 全部成功、4+4 分摊到
两个 worker；沙箱记录 `memoryMB=512`（`tmp/capacity_check.py` 的验证输出）。

两个必须知道的后果：
① **沙箱内存上限减半（1024→512MB）是用户可见变更** —— 之前用满 1GB 的负载现在会被
`max_memory` 拦（表现为分配失败/被杀）。要回到 1GB 就把 `E2B_DEFAULT_MEMORY_MB` 设回 1024
（那会让车队内存维度降到 8、与 CPU/进程一致，并发上限仍是 8）。
② 之前"最多 4 个并发、第 5 个起 `503 No resources available`"的根因就是这张表：
`E2B_MAX_TOTAL_CPU_PERCENT=400` 与 `E2B_MAX_TOTAL_PROCESSES=1024` 各折算 4 个。

线上实测（2026-09-16，`tmp/mem512-limit.py` / `tmp/mcp-512-size.py`）：

- **上限是真硬约束**：箱内 `MemTotal=524288 kB`；同箱内 400 MiB 分配 exit 0，700 MiB 分配
  被 SIGKILL（SDK 侧 exit 137 / stderr `Killed`）。
- **箱内 MCP 网关的 stdio server 上限 ≈110 MiB**（这是 512MB 口径下比"用户负载减半"更容易
  被忽略的一条）：server 在 import 期持有 110 MiB 时 `tools/list` 与 `tools/call` 都正常；
  120 MiB 起 server 起不来，网关 `session.initialize()` 拿到 `MCPError: Connection closed`
  后 exit 1（worker 日志 `MCP gateway exited ... exit_code=1`），`/mcp` 按 FUP #4 以 503 带
  记录原文作答。FUP #3 当年把 per-sandbox 默认从 512 抬到 1024 正是为了 450 MiB 的 MCP
  server 目标 —— **改回 512 就等于把那个目标降到 ~110 MiB**，跑大 MCP server 的部署要么把
  `E2B_DEFAULT_MEMORY_MB` 设回 1024，要么接受这个上限。
- **`Sandbox.create(mcp=...)` 返回 ≠ 网关已就绪**（与本次容量调整无关的既有语义）：envd 拦截
  SDK 的 `mcp-gateway --config` 命令后立刻回 exit 0，网关真正 bind 要 2–5s（并发创建更多箱
  时更久），这期间 `/mcp` 从 worker 代理出去是 `httpx.ConnectError` → 客户端看到 **500**
  （不是 503）。用 MCP 的客户端要把"刚 create 就连"当作可重试窗口，与
  `tests/contract/test_mcp_netns.py` 里那段等 20s 的逻辑同源。

门禁口径（2026-09-16 修正）：此前这两个契约把上限写成常量 1024（分配量 800/400/50 也按
1 GiB 箱折算），而 lane 不把 `E2B_DEFAULT_MEMORY_MB` 透传进去 —— 门禁跑的是**代码默认
1024 形态**，不是线上 512 形态。现在：

- `deploy/scripts/test-prod-shaped.sh` 会透传宿主的 `E2B_DEFAULT_MEMORY_MB`（未设则保持
  不设，退回代码默认），两个阶段都带上；
- `tests/_memory_budget.py` 是尺寸口径的唯一来源：`boxed_sizes()` 按上限取比例
  （512 → 358/204/25），`gateway_sizes()` 的 `denied` 直接要整箱（与网关自身占用无关，
  这样在生产与 lane 两种网关记账下都成立），`server_hold`/`control` 之和压在**实测**的
  512 箱余量 110 MiB 之内；
- `tests/unit/test_memory_quota_contract_sizes.py` 把这三条性质钉住，将来改比例不会静默
  让断言变空转；
- 同一批被 512 形态暴露出来的写死值（`/metrics` 的 `memTotal`、`/internal/tenants` 的
  usage/unowned、migration 的 target 预留）一并改成按同一常量取。

跑线上形态的门禁：

```sh
E2B_REGISTRY_MIRRORS=registry-1.docker.io=127.0.0.1:5080 \
  PROD_DROP_CAPS=SYS_ADMIN UNPRIVILEGED_PHASE=0 E2B_DEFAULT_MEMORY_MB=512 \
  ./deploy/scripts/test-prod-shaped.sh
```

2026-09-16 两种形态各跑一次：`502`/`1024` 下改动涉及的 22 条全绿，512 形态整轮 100%
无 F/E。

### 2.4.9 为什么 MCP 箱里"只剩 110 MiB"：记账按预留，不按实际触碰

上面的 110 MiB 不是"箱子被谁占了 400 MiB"，而是**两个 Python 进程的地址空间预留**在这套
记账下被全额计入。机制先说清：

- sandlock 是**无 cgroup** 运行时（`crates/sandlock-oci/README.md`：no namespaces / no
  cgroups），内存上限由 supervisor 在 seccomp 通知里执行
  （`crates/sandlock-core/src/resource.rs::handle_memory`，拦 `mmap/brk/mremap/munmap`）；
- 账本记的是**匿名映射的请求大小**，不是驻留也不是触碰量 —— 预留 64 MiB、只用 1 MiB
  也按 64 MiB 计；
- 箱内 `/proc/meminfo` 就是这本账：`MemFree = max_memory − mem_used`
  （`crates/sandlock-core/src/procfs.rs::generate_meminfo`），所以可以直接在箱内读数。

2026-09-16 在线上 512MB 箱里逐项量的**单位成本**（每次都是独立前台进程，读同一个账本；
`tmp/ledger-thread-cost.py`、`tmp/ledger-arena-test.py`）：

| 项 | 记账 | 说明 |
|---|---|---|
| 空箱基线 | ~28 MiB | 无 MCP、无命令时的箱底 |
| 触碰 50 MiB | **+50 MiB** | 真实数据 1:1 |
| 多 1 个线程 | **+72 MiB** | glibc per-thread malloc arena（64 MiB 预留）+ 8 MiB 栈 |
| 3 个线程 | +216 MiB | 线性叠加（3×72） |
| `import mcp` | **+73 MiB** | mcp 依赖树的映射 |
| `import uvicorn` | +25 MiB | |
| 3 线程 + `MALLOC_ARENA_MAX=1` | **+24 MiB** | arena 预留消失，只剩栈 |

⇒ MCP 箱的账：网关进程（python + mcp + uvicorn + 自己的线程/arena）+ stdio server
（python + `import mcp` + 它的线程/arena）在 server **还没分配任何业务内存**之前就吃掉
约 320 MiB（实测：MCP 箱 347.7 MiB vs 同形空箱 27.7 MiB），剩下约 110 MiB 才是 server 自己
的 payload。同一只箱里一条**普通命令**仍能分配 160 MiB（账本 507.6 MiB）——箱没满，
是 server 自己先付了 import/线程的钱。

**已落地的杠杆（2026-09-16，`d5114a2`，随 `0.1.0-293-gd5114a2` 上线）**：
`MALLOC_ARENA_MAX=1` 钉在 envd 起的 MCP 网关上
（`envd_service/runtime/context.py::_MCP_GATEWAY_MALLOC_ARENA_MAX`）。
两个进程都要覆盖，而只有一个能靠继承：

- 网关自己拿 env（`ExecConfig.env`）；
- **stdio server 继承不到** —— SDK 的 stdio client 只转发
  `DEFAULT_INHERITED_ENV_VARS`（HOME/LOGNAME/PATH/SHELL/TERM/USER），所以 envd 把它注入
  mcp config 的 `envs`（调用方自带的值优先）。

线上复测（同一只 512MB 箱，`tmp/verify-arena-live.py`）：server 持 **110 / 200 / 300 MiB
都能 serve**（`tools/list` + `echo` 往返），340 MiB 仍失败；server 侧 `echo` 回读环境变量确认
拿到 `MALLOC_ARENA_MAX=1`。普通箱 400 MiB 分配不受影响——这个变量**只**加在网关/server
这条链上，不进用户命令的默认环境，因为多线程分配密集的负载会吃到 arena 竞争。


## 2.5 门禁容器的两种形态（别把测试特权当成生产需要）

- **`deploy/scripts/test-prod-shaped.sh`（生产形，默认推荐）**：容器不带 `--privileged`，
  `--cap-drop ALL` 后给一组部署等价 cap（Docker 默认集 + `SYS_PTRACE` `NET_ADMIN`，
  `seccomp=<deploy/seccomp/sandlock-worker.json>`，2026-09-15 起与上线清单同一份 profile，
  `SECCOMP_PROFILE=` 可覆盖）。⚠️ A6 之后清单**已不再声明 `SYS_ADMIN`**，而脚本默认仍加着它
  ⇒ 不带参数的 lane 是清单的**超集**；**无 `SYS_ADMIN` 的形态由 A7 的参数固化**：
  `PROD_DROP_CAPS=SYS_ADMIN UNPRIVILEGED_PHASE=0 ./deploy/scripts/test-prod-shaped.sh`
  （实测 `1075 passed, 3 skipped, 0 failed`，`tmp/a7-nosa.log`）。
  注意 `PROD_DROP_CAPS` 是把 cap 从 `--cap-add` 列表里**摘掉**，不只是追加 `--cap-drop`：
  本机引擎（29.4.0）`--cap-add` 压过 `--cap-drop`，与参数顺序无关
  （`--cap-drop ALL --cap-add SYS_ADMIN --cap-drop SYS_ADMIN` 的 `CapEff` 仍含 SYS_ADMIN 位；
  探针对照 `0xa02c35fb → 0xa00c35fb`）。
  ⚠️ **必须带 `E2B_REGISTRY_MIRRORS=registry-1.docker.io=127.0.0.1:5080`**（§2.6.1 的本地
  预置源）：不带就退回公共镜像链，而本地构建的 `python-mcp:3.14` 不在那些镜像站的白名单里，
  实测 **205 条失败/错误**（`this image is not in the allowlist` 与它引发的 428
  `warm_required` 级联）；带上之后同一棵树是 **1437 passed / 3 skipped / 0 failed**
  （2026-09-15，`tmp/prod-shaped-seccomp-final2.log`，用的就是上线那份
  `deploy/seccomp/sandlock-worker.json`）。脚本在未设置时会打一条显式告警，不再静默跑出
  一片红。
  **磁盘水位也是这条 lane 的前置**：宿主 `/System/Volumes/Data` 用到 97%（873G/932G）时，
  `tests/unit/test_image_cache_sharing.py` 的 `wait_for_image_cache_maintenance(timeout=30)`
  会等不到收敛而超时（测试日志里有 `workspace disk watermark warning: 96.9% used`）；
  该文件单独跑（两种 seccomp 档都试过）是 `41 passed`。清出 75G（873G→798G）后复跑即全绿；
  另注意**清盘会把镜像缓存变成冷源**，紧接着的一次全量会出现 `428 warm_required` 竞态
  （实测 20 条），热一轮即消失，不是回归。
  实测（2026-09-10）Landlock（ABI 8）与非特权 userns 都不需要任何特权；**唯一造不出来的是
  XFS prjquota 暂存盘**（容器内 loop 设备不可用，即使 `--cap-add SYS_ADMIN` +
  `--device /dev/loop-control` 也 `failed to setup loop device`）⇒ 只有**真挂 XFS prjquota**
  的 2 个契约文件显式 `--ignore`（`tests/contract/test_volume_quota.py`、
  `tests/contract/test_xfs_project_quota.py`）；A7 把首个过宽的名单收窄，4 个配额单测
  （`test_volume_quota` / `test_xfs_project_quota_agent` / `test_quota_agent_client` /
  `test_quota_maintenance`，靠 monkeypatch 不碰真文件系统）与一个**已不存在**的
  `tests/security/test_quota_enforcement.py` 都从名单移出，A5/A6 的新用例因此回到默认门禁。
  口径更正（A5 实测、A7 记录）：`E2B_TEST_STRICT_SKIPS=1` **只**升级
  `tests/conftest.py::_STRICT_SKIP_FORBIDDEN` 的 6 个 runner 能力标记，
  普通 `pytest.mark.skipif` 在 strict 下仍是 skip；上面两个契约文件恰好用第一条标记
  （"XFS quota integration requires"），所以漏列会变 error 而不是静默少跑。
  `NET_ADMIN` 是给**夹具**用的（往 lo 上放 198.18.0.99 作为可 allow/deny 的真实源地址），
  worker 自身不需要。
- **phase 2：`--user 65534:65534` 的无特权 worker 跑法**（`UNPRIVILEGED_PHASE=0` 可跳）：
  上面那条「生产形」lane 仍是 **root**（只是把 cap 削到部署等价集），而
  `docker-compose.prod.yml` 真正写的是 `user: "65534:65534"` —— 那是**第三种形态**：
  没有 `CAP_SETUID` ⇒ uid 池自动关、租不到 route-B 槽位、沙箱就用 worker 自己的 euid
  （E5.1），路径中介留在进程内。删掉 `mediation_run_as` 降级档之后，这一形态
  必须自己站出来跑一遍：`tests/security/test_template_isolation.py` +
  `tests/security/test_sandlock_isolation.py` + 两份 route-B/policy 单测，
  实测 `47 passed / 1 skipped / 0 failed`（唯一的 skip 是那条「euid 0 才谈得上被拒」
  的钉桩）。phase 2 用 `python:3.11-slim` 作基镜像（`PHASE2_BASE_IMAGE` 可改）：
  本地构建的 `python-mcp:3.14` 镜像在 registry 镜像站的白名单外，且它的 rootfs
  缓存落在 phase 1 那套 harness 目录里，65534 写不进去。
- **原有特权跑法**：继续承担 XFS 配额全量与 loop 相关用例。
  两条 lane 的差集只应当是「配额/loop」这一类，任何别处的差集都是生产可用性缺陷。

## 2.6 公共镜像源与 OCI 限流回落（D2 口径，2026-09-11）

公共 registry 对匿名拉取限流（Docker Hub 会在一个节点的建箱量之前就回 `429
TOOMANYREQUESTS`），而各镜像站的抖动互相独立 ⇒ 默认形态是**多源回落**，不是单源，也不是直连：

- `E2B_REGISTRY_MIRRORS=host=mirrorA|mirrorB,...`：`|` 分隔的候选源按**顺序尝试**，
  每个源的 token challenge 独立重跑；origin host（如 `registry-1.docker.io`）**始终追加为最后
  一个端点**，全部失败才报错（`envd_service/runtime/oci_registry.py`）。
- **未设置**该变量 ⇒ 用内置默认链
  `registry-1.docker.io=docker.m.daocloud.io|docker.1ms.run`
  （`DEFAULT_REGISTRY_MIRRORS`，与 `deploy/compose/.env.example`、
  `deploy/docker/Dockerfile.test-runner` 同值）——出厂不配置也有多源，不是单源兜底。
- **显式置空**（`E2B_REGISTRY_MIRRORS=`，如 `.env.example` 的「Leave empty to pull directly」）
  ⇒ 有意直连 origin，不插任何源；这是保留的逃生门，语义与未设置不同。
- **404 语义**：404 是「镜像不存在」的答案，不是端点故障 ⇒ **不重试、不换下一个源**
  （`RegistryError.retryable` 仅对 408/429/5xx/连接错误/层 digest 不匹配为真）。推论：
  任何被用作源的本地/私有 registry **必须镜像全集**，缺一个 tag 就是硬失败。
- 限流兜底还有两层：`E2B_IMAGE_MANIFEST_TTL_S`（默认 60s，按镜像+凭据缓存 manifest digest）
  与 blob 独立超时（600s），慢源不占用交互请求预算。
- **buildkitd（模板构建）与 resolver 读同一个 `E2B_REGISTRY_MIRRORS`**：测试容器的
  `buildkitd.toml` 由 `tests/conftest.py` 调 resolver 自己的解析函数生成，
  `deploy/stack/buildkitd.toml` / `deploy/scripts/lib/buildkitd.toml` 内置同一多源链，
  避免「resolver 换了源、构建还钉在单源」的同源抖动。

### 2.6.1 测试侧：本地 registry 预置镜像（默认跑法）

测试不依赖公共源：`registry:2` 起在 `127.0.0.1:5080`，预置**全集**镜像，再把同一个 env 指到它
（同一做法已登记在 `docs/task-backlog.md` #20 的规避记录里，此处固化为默认跑法）：

```bash
docker run -d --rm -p 127.0.0.1:5080:5000 registry:2
# 预置（amd64）全集：缺任何一个 tag 都会因「404 不重试」硬失败，不会回落公共源
for t in 3.11-slim 3.12-slim 3.14-slim; do
  docker pull --platform linux/amd64 python:$t
  docker tag python:$t 127.0.0.1:5080/library/python:$t
  docker push 127.0.0.1:5080/library/python:$t
done
docker pull --platform linux/amd64 node:22-slim   # 需要 JS/模板用例时
docker tag node:22-slim 127.0.0.1:5080/library/node:22-slim
docker push 127.0.0.1:5080/library/node:22-slim

E2B_REGISTRY_MIRRORS=registry-1.docker.io=127.0.0.1:5080 \
E2B_BASE_IMAGE=python:3.11-slim \
  ./deploy/scripts/test-prod-shaped.sh            # OCI 形态
```

- loopback 源自动走 `http://`（resolver 与 buildkitd 同一条规则），`registry:2` 无需 TLS；
  `tests/conftest.py` 同时给 buildkitd 写 `[registry."127.0.0.1"] http = true`。注意该规则是
  **loopback 之外一律 https**：写 `http://mirror.internal` 会被规范化成 `https://`（resolver 与
  buildkitd 一致，避免同源漂移），纯 HTTP 的远端镜像源需要 TLS 或走 loopback；
- 本地源**只对测试**成立（它没有上游回源能力），所以「镜像全集」是硬要求：缺
  `python:3.12-slim` 时 `test_create_with_template_image` 直接 503。
- 本轮 `127.0.0.1:5080` 的实际内容（`GET /v2/_catalog` + tags）：`library/python` =
  3.11-slim / 3.12-slim / 3.14-slim，`library/node` = 22-slim。

### 2.6.2 digest 固定的 `tag@sha256:...`：resolver 曾把 tag 留在路径里（2026-09-15 修）

E6.2 要求生产把 `E2B_BASE_IMAGE` 钉成 `<repo>:<tag>@sha256:<digest>`（`upgrade.sh` 拒绝
tag-only，除非显式 `--allow-tag-base-image`）。这个形态**当时跑不通**，实测（目标机
Rocky/aarch64，2026-09-15，`docs/HANDOFF.md` 同名块）：

```
GET /v2/byteplan/python-mcp:3.14/manifests/sha256:3675662d…  ->  404
ImageResolutionError: failed to resolve image registry…/python-mcp:3.14@sha256:3675662d…
→ 建箱 428 warm_required（该节点上所有建箱都失败）
```

根因：`parse_image_ref`（`envd_service/runtime/oci_registry.py`）在参考串含 `@` 时把 `@`
之前**整段**当成 repository，于是 tag 留在路径里；而 registry API 要的是
`/v2/<repo>/manifests/<digest>`。修法：只在**最后一个路径段**上剥 tag（带端口的 registry
主机名 `localhost:5000/ns/img:1` 因此不受影响），`reference` 仍是 digest。

回归：`tests/unit/test_oci_registry.py::test_parse_image_ref_tag_plus_digest_keeps_the_repository_clean`
（解析层）与 `::test_resolve_rootfs_from_a_digest_pinned_ref`（用假 registry 端到端解析一个
digest 固定的引用；修复前这两条一红一 error）。

✅ **已落地（2026-09-15 同日）**：`build-and-push.sh` 出新版本
`0.1.0-281-gbd88421-20260915-185036`（重建后 `python-mcp:3.14` 的 manifest list digest 仍是
`sha256:3675662d…`，base 内容未变），`.env` 把 `E2B_BASE_IMAGE` 改成 `tag@digest` 后
`upgrade.sh` 升级完成。线上实测：worker 日志
`resolved base image …@sha256:3675662d… to rootfs …` 与 `worker image warmed: …@sha256:3675662d…`，
多节点 + 部署级冒烟全绿（`tmp/build-push-resolver-fix.log`、`tmp/upgrade-resolver-fix.log`）。

两条运维注意：① **换 digest 就是换缓存条目** —— 升级后第一次建箱要等预热，抢在预热完成前创建
会拿到 428 `warm_required`（`upgrade.sh` 内置的那次冒烟就撞上过，补跑 `smoke.sh` 即绿）；
② 重建 base 镜像（`build-and-push.sh` 的 `MIRROR_BASE_IMAGE=1`）会换 digest，必须同步更新
`.env` 的钉值 —— tag 变更不更新 digest 会被 `upgrade.sh` 按 E6.2 拒绝，这正是它的意图。

### 2.6.3 已知退路：`<image>.digest` 侧车（本轮不做）

若「多源回落 + 本地源预置」落地后**实测仍抖动**（例如候选源同时 429/超时），再加
`<image>.digest` 侧车：解析失败时回落「上次成功的 digest」，代价是限流期间感知不到 tag 更新
（HANDOFF「OCI 形态」一节的出路 (b)）。本轮按用户拍板（决定 ⑦：最快且测试正常）不做。

## 2.7 镜像 rootfs 缓存：落共享卷 + 跨进程安全 + 有界（Track Z / Z-F7，2026-09-13）

`E2B_IMAGE_CACHE_DIR` 的**默认值仍是相对路径** `tmp/sandboxes/_images`（相对进程 CWD，
本地开发形态不变、兼容）；生产形态必须显式把它指到共享卷，否则缓存落在 **worker 容器内**：
`docker compose up -d` 即丢、每个 worker 各存一份、且完全不受配额约束（既不在沙箱 projid 里，
也不在共享卷上）。原始缺口登记在 `docs/sandbox-disk-quota.md` §1.1 的 Z-F7 条目。

### 2.7.1 落点（谁必须设、设成什么）

```yaml
E2B_IMAGE_CACHE_DIR: /var/lib/e2b-sandboxes/_images            # compose: worker-1/worker-2 + control-plane
E2B_IMAGE_CACHE_MAX_BYTES: "4294967296"                        # 4 GiB；0 = 不限
E2B_IMAGE_CACHE_EVICT_MIN_AGE_S: "300"                         # 逐出新鲜度下限（秒）
E2B_IMAGE_CACHE_OWNER_UID: "65534"                             # 缓存归 worker uid（见下）
```

- **compose（`deploy/stack/docker-compose.prod.yml`）**：`worker-1`/`worker-2`（同一
  `&worker-env` 锚点）与 `control-plane` 三处同值。控制面也要设，是因为**不配
  `E2B_IMAGE_REGISTRY` 的单机形态**下它把本地构建的模板导出成 OCI layout tar 写进
  `_images/_oci/`（`control_plane/api/templates.py`），worker 再从那份 tar 解析 rootfs；
  控制面不设 ⇒ tar 落在控制面容器里，worker 永远看不见（"cannot resolve image"）。
- **k8s（`deploy/k8s/worker.yaml` + `control-plane.yaml`）**：同一份 **RWX PVC**
  （`sandbox-shared`，§2.4.4 W4）挂在同一个路径 `/var/lib/e2b-sandboxes`，所以路径语义与
  compose **没有差异**，同源设置即可，不引入新的卷/PVC。差异只在后面两条：
  1. worker Deployment 默认 `replicas: 1`（可扩到 N），control-plane 是 `replicas: 2`
     ——**多个副本共享同一缓存目录**，正是下面 2.7.2 的并发形状；
  2. 这套清单**不发 quota-agent**（§2.4.4），所以在这个形态下 `_images` 没有任何
     XFS project 兜底，**`E2B_IMAGE_CACHE_MAX_BYTES` 就是它唯一的容量上界，必须设**。
  （PVC 本身仍是 50Gi、无目录级限额，本项不要求改 `pvc.yaml`。）
- **命名空间**：`_images` 在 `gateway_common/paths.py` 的 `RESERVED_PLATFORM_NAMESPACES`
  里，`is_sandbox_workspace_dir()` 按"有没有顶层 `sandbox.json`"的既有判据把它排除 ⇒
  放进 workspace base **不会**被当成沙箱树（不会被 GC 扫成孤儿、不会进配额扫描）。
  解析器自己写的东西（`<image-slug>.lock`、`.…tmp-*` 暂存树）都落在 `_images/` **里面**，
  workspace base 顶层不新增任何名字（有单测钉住，见 2.7.5）。
- **两个 uid 共享同一个 `_images`（必须设 `E2B_IMAGE_CACHE_OWNER_UID`）**：两套清单里
  **控制面跑 root、worker 跑 65534**（k8s 的 worker pod 继承镜像的 `USER 65534:65534`，
  控制面 pod 覆盖镜像的 root），而两者指向同一个目录。第一版就死在这里：root 先跑
  （不配 registry 时控制面导出模板 tar，**必然** root 先建 `_images`）⇒ `_images` 是
  `root:root 0755`，worker 连 `<image>.lock` 与暂存树都建不出来（`PermissionError` ⇒
  该镜像解析全量失败）；反向同理（哪一方先建了 `0600` 的锁文件，另一方**永远**打不开，
  而锁文件按设计从不删除）。
  规则是**所有权**，不是把目录放宽：

  | 目录/文件 | owner | mode | 谁能写 |
  |---|---|---|---|
  | `_images` / `_images/_oci` / `<entry>` / `<entry>/rootfs` | 缓存 owner（= worker uid，生产 65534） | `0755` | owner（worker）+ root（`CAP_DAC_OVERRIDE`） |
  | `<image-slug>.lock` / `.link` 侧车 | 同上 | `0644` | 同上（`flock` 只需要可读 fd，读不到才报错） |
  | 沙箱 uid（池内 10000+） | — | 只有 `r-x` | **一个字节都写不了** |

  解析器每次创建目录/文件都执行这条契约：`_ensure_shared_dir()` 建目录后强制 `0755`，
  并以 root 身份把它 `chown` 给**缓存 owner**（也就是 worker uid，和
  `deploy/scripts/upgrade.sh` 对卷根做的那次 `chown -R 65534` 同一个身份；
  `E2B_IMAGE_CACHE_OWNER_UID` 未设时取最近的**非 root 祖先**的 owner，即卷根
  `/var/lib/e2b-sandboxes`）。root 进程发布条目时，整棵 rootfs 也会被交给该 uid
  （`lchown`，不跟随镜像里的符号链接）——否则 worker 之后写不进
  `<rootfs>/workspace` 与 MITM CA 文件（`sandlock.py` 的既有路径），那也是一种"被锁死"。

  **谁先碰到缓存、以及谁有权改它（2026-09-13 实测，别把它当成"控制面会自愈"）**：

  | 路径 | 会不会把 `_images` 修回 worker uid |
  |---|---|
  | 控制面导出模板 tar（`control_plane/api/templates.py`，`parent.mkdir(parents=True, exist_ok=True)` + `buildctl` 写文件） | **不会**。它不经过解析器，也不做 chown ⇒ 在空卷上**必然**留下 `root:root 0755` 的 `_images`（探针 `tmp/zf7tail-hints.log` 的 prep 就是照这条路径造的） |
  | 控制面的 Settings（`control_plane/config.py`） | **不会**。它只是 `Path(os.getenv(...)).resolve()`，没有 `ensure_shared_cache_dir()` 这道准备逻辑（只有 `envd_service/config.py` 里那一处调用它，那是 worker 侧） |
  | root 身份跑解析器（控制面本地解析 registry 镜像） | **会**。`_ensure_shared_dir()` 在 `euid==0` 时把目录 `chown` 给缓存 owner；探针实测：root 解析一次后 `_images`、`_oci`、条目、`<rootfs>`、`.link` 全部变成 `65534`，随后 worker 复用同一 Inode 并在里面建挂载点（`tmp/zf7tail-hints.log` h2/h3） |
  | worker（65534）跑解析器 | **不能**。非 root 没有 `CAP_CHOWN`，`mkdir(exist_ok=True)` 对已存在的目录静默成功 ⇒ 碰到别人拥有的 `_images` 只能报错 |

  所以"root 侧下一次解析会自愈"只在**那条解析路径**上成立；单机无 registry 形态下恰恰是
  不经过解析器的那条（tar 导出）先建目录。修不了的一方必须拿到**能照抄的命令**：本轮把
  解析器所有"缓存不归我"的失败点（锁文件、暂存树、`_oci` 侧车）统一换成带
  `chown -R 65534:65534 <cache>` 的 `ImageResolutionError`，实测报文形如
  `shared image cache directory /var/lib/e2b-sandboxes/_images is not writable by uid 65534:
  [Errno 13] Permission denied: '<cache>/<image>.lock' (the cache must belong to the worker uid;
  fix it once with \`chown -R 65534:65534 /var/lib/e2b-sandboxes/_images\` as root)`
  （`tmp/zf7tail-hints.log` h1；同一形状在 `d0b7e5c` 上只报 `... is not writable by uid 65534: …`，
  没有修复命令）。

  **两套清单的一次性 `image-cache-init`：以 root 建 `_images`（含 `_oci`）→ `chown 65534` →
  `chmod 0755` → 最后按 `stat -c %u` 校验目录确实归 worker uid**，不合格就 `exit 1` 并把
  上面那条 `chown -R 65534:65534 <cache>` 打到 stderr。校验这一步是"能自愈就自愈、不能就
  明确报错"的分界（实测 5 种形态，见 `tmp/zf7tail-init-behaviour.log`）：

  | 形态 | 结果 |
  |---|---|
  | 空卷 + root（正常形态） | init `rc=0`，`_images`/`_oci` = `65534 0755` |
  | NFS `root_squash` 把客户端 root 映射成 **65534**（卷根也归 65534） | `mkdir` 本身就落在 65534 上，`chown 65534:65534` 是**同 owner 的合法 no-op** ⇒ init `rc=0`、目录仍是 `65534 0755`（**自愈**，不是降级） |
  | NFS `root_squash` 的 anon uid **不是 65534**（例如 1000） | `mkdir` 成功、目录归 1000，`chown` 被拒 ⇒ init **`rc=1`** + `FATAL … is owned by uid 1000, not the worker uid 65534` + 修复命令。compose 形态因 `depends_on: service_completed_successfully` 整栈不启动；k8s 形态 Pod 停在 `Init:Error`（**不再**是"只告警然后 Pod 起来全量解析失败"；`d0b7e5c` 的 k8s init 在这种形态下实测 `rc=0`、只打印 `chown refused`） |
  | 旧卷：`_images` 已存在且归 1000，root 无 `CAP_CHOWN`（`--cap-drop CHOWN`） | 同上：`rc=1` + 修复命令（这一格就是"init 报成功、worker 之后全量失败"的老形态） |
  | 外来的 1000 目录留着不动，worker（65534）去解析 | worker 的报错里带同一条修复命令（`tmp/zf7tail-init-behaviour.log` E 行） |

  **"worker 可自建缓存"这句话不成立**：init 只要跑过就已经 `mkdir` 出目录，worker 之后
  `mkdir(exist_ok=True)` 不会改变它的所有权；只有在 init **完全没跑**、且卷上连 `_images`
  都没有时，worker 才会以 65534 建出归自己的目录（那时确实可用，但两套生产清单都不提供这个
  前提）。上一轮报告 §6.2 的"此时由 worker 自己建缓存也可用"就是被 init 自己的 `mkdir`
  推翻的那句，本节按实测改写。

  **已有共享卷升级时的一次性动作**：`deploy/scripts/upgrade.sh` 只覆盖 `stack` 形态，它已经
  在部署时对整个卷做一次 `chown -R <worker-uid>:<worker-uid> /var/lib/e2b-sandboxes`
  （E5.1 的既有逻辑，`_images` 也在其中）；其余形态（`deploy/compose/*.yml`，尤其
  `multinode`）没有这条自动化，升级后如果 init 报 `FATAL … owned by uid …`，就在宿主上
  照抄它给出的那条命令执行一次：`chown -R 65534:65534 /var/lib/e2b-sandboxes/_images`
  （k8s/NFS 形态在存储服务端执行等价动作）。

  **本轮补齐的清单覆盖**（`tmp/zf7tail-manifests.log` 的 coverage 表逐条列出证据）：
  `compose/docker-compose.yml`、`compose/docker-compose.prod.yml`、`compose/docker-compose.autoscale.yml`、
  `stack/docker-compose.prod.yml`、`k8s/worker.yaml`、`k8s/control-plane.yaml` 六处跑**同一段**
  init 脚本（compose 里按 compose 语法把 `$` 写成 `$$`，逐字节同源由探针断言）；
  `compose/docker-compose.multinode.yml` **本轮新增** init（这个形态有两个卷：控制面的
  `control-data` 与 worker 的 `worker-data`，`CACHE_DIRS` 两个路径都 chown）；
  另外把 `compose.prod` 的 worker、`stack.prod` 的 worker-1、`autoscale` 的 control-plane
  从"仅启动顺序"的 `depends_on` 改成 `condition: service_completed_successfully`
  （实测 `docker compose config` 把纯列表渲染成 `service_started`，即 worker 可以先于 init
  启动并撞上 root-owned 目录）；`compose/docker-compose.test.yml`、
  `compose/docker-compose.quota-agent.yml`、`k8s/gateway.yaml`、`k8s/autoscaler.yaml`、
  `k8s/redis.yaml`、`k8s/pvc.yaml`、`k8s/namespace.yaml` 都不碰 `_images`，登记为不需要 init
  （探针逐条断言"不含 `E2B_IMAGE_CACHE_DIR`、不出现 `_images`"）。
  **明确不做的事**：不把 `_images` 改成 `0777`。共享卷上跑着租户进程，rootfs 缓存可写 =
  任一沙箱可以改写别的沙箱将要 chroot 进去的镜像 = 跨租户投毒；解析器反过来还会把
  遗留的 `0777` 收紧成 `0755`。升级说明：本次修复之前从未上线，`_images` 是新建的；
  若曾在某台机器上手工跑过旧版并在卷上留下 `root:root` 的 `_images`/`0600` 锁文件，
  一次 `chown -R 65534:65534 /var/lib/e2b-sandboxes/_images` 即可——**别指望解析器
  自己修**：只有以 root 身份跑的那条解析路径会把目录 chown 回缓存 owner，worker
  （65534）碰到这种残留只能报错（报文里带上面那条命令），init 则会直接失败并打印它。

### 2.7.2 跨进程安全（为什么"同目录"以前是坏的）

修复前只有**进程内** `threading.Lock`：两个 worker 共享同一目录时，双方都没看到
`rootfs/.complete` ⇒ 同时解压**同一个最终目录**，先完成的先写 `.complete`，后写者继续往同一棵树
里写（读者可能拿到**残缺 rootfs**）；任一方失败还会 `rmtree(rootfs.parent)` 把**另一方正在用/
刚完成的条目整条删掉**。现在（`envd_service/runtime/image_resolver.py`）：

1. **跨进程互斥**：`flock(2)` 独占锁包住「检查 `.complete` → 拉层 → 解压 → 发布」整段，
   锁文件是 `<cache>/<image-slug>.lock`（`0644`、owner = 缓存 owner，所以两个 uid 都打得开；
   **从不 unlink**：删了会让两个进程锁到同名的不同 inode 上，等于没锁）。进程内
   `threading.Lock` 保留在 `flock` 外层：同一进程的第二个 `open` 会在自己的文件描述上阻塞，
   线程走廉价的进程内锁排队更合适，两者一起覆盖两种形状。
2. **原子落盘**：解压写进**同文件系统的私有暂存树** `_images/.<entry>.tmp-<pid>-<rand>/`，
   `.complete` **先写在暂存树里**，最后 `rename(暂存, 最终)` 一步发布 ⇒ 读者看到这个路径时
   它已经是完整的；即使锁完全没生效（见下条），也**不可能**看到半成品。发布用的是
   `os.rename`（非空目标会失败）而不是覆盖式 `os.replace`：既有名字**永不**被静默顶掉。
3. **失败只清自己**：`except` 只 `rmtree` 本次创建的暂存树。
4. **"清垃圾"不再能误伤已发布条目**：最终目录若被**不完整**的残留占着（旧版 worker 崩溃
   留下的形状），先 `rename` **原子地把它抢到**本进程的私有隔离名
   `_images/.<entry>.garbage-<pid>-<rand>`，再核对抢到的是不是刚才检查的那一个 inode、
   并且**仍然没有 `.complete`**；两项都对才 `rmtree`。中途被别人发布的条目会被原样放回，
   发布者则退避重试直到看见那条完成条目。旧代码的窗口（检查 `.complete` → 未再确认就
   `rmtree(最终目录)`）在锁不被跨客户端遵守时会把**刚发布的完整条目**整棵删掉
   （评审 C4 实测 `was_complete=True`，两个调用者拿到不同 inode），现在这条路径上根本
   不存在"删除最终条目路径"这个动作。
5. **锁有超时**：`E2B_IMAGE_CACHE_LOCK_TIMEOUT_S`（默认 300s，`0` = 永远等）同时约束
   进程内锁与 `flock`。超时后：若条目已经被发布就用它（`_materialize_entry` 会重查
   `.complete`），否则抛 `CacheLockTimeout`（`ImageResolutionError` 的子类，报文里带
   锁路径与超时值）。没有超时的话，一个卡在解压里的 worker 会**无限期**阻塞其它 worker
   解析同一镜像（评审 C5 实测 waiter 20s 仍阻塞）。
6. **本地构建（OCI tar）路径同一套**：`_resolve_local_oci()` 也走 `_materialize_entry()`，
   同锁、同暂存、同发布语义；`.link` 侧车用"写临时文件 + rename"的方式更新，所以另一
   uid 写的旧侧车不会挡住自己。

**存储不支持跨客户端锁时的口径**（NFS 形态务必知道）：§5.1 允许 NFSv3 `nolock` 挂载，
那 `flock` 退化为客户端本地锁 ⇒ **跨客户端不再互斥**，两个节点会各解压一份。此时正确性由
第 2/3 条独立保证：两份内容同源（同一 digest），只会有一个条目被发布，另一份暂存树被丢弃，
读者拿到的始终是完整 rootfs —— 代价是**多一次解压**（不是数据错误）。这也解释了为什么两条
机制都要，而不是二选一：文件锁买的是"只解压一次"，原子发布买的是"永远不会残缺"。

### 2.7.3 有界策略（`_images` 在无限额 project 里）

被采纳的是**解析器自带的按量 GC**（不是文档化 TODO、也不是"只做观测"）：
`envd_service/runtime/image_resolver.py::prune_image_cache()`。

- **触发点**：唯一让缓存增长的地方就是"冷解析成功后发布"（`_resolve_local_oci()` /
  `resolve_image_rootfs()` 共用 `_materialize_entry()`），所以发布后顺手执行一次；
  **每个进程最多每 60s 走一次目录**（`_PRUNE_INTERVAL_S`），不需要运维加 cron，也不给
  热路径（`.complete` 命中，直接 return）增加任何开销。外部想主动跑一次也可以直接调
  `prune_image_cache(cache_dir)`。
- **这一趟跑在自己的线程上，不在发布者的线程上（2026-09-14，sdk lane flake）**：发布路径
  就是**沙箱的第一条命令**——worker 在第一个 RPC 上才建运行时上下文（`SandboxRuntimeContext`
  → `create_executor()` → `resolve_image_rootfs()`），而 SDK 对这条请求的预算是
  `request_timeout`/`timeout`（默认 60s）。实测（`tmp/sdkflake-diag2.log`、`-diag3.log`、
  `tmp/sdkflake-cacheprobe.py`）：在 13.98 GiB / 383k 文件的暖缓存上，**一次 GC 里的量取
  走一遍就要 24.3s（空载）、43.3s（6 核压载），而 `prune_image_cache()` 走两遍**（先判再报），
  整趟 97.9s ⇒ 一次冷创建的"首条命令"≈105s，SDK 到点把请求杀掉，测试看到的是
  `process.Process/Start` 客户端超时。因此 `_materialize_entry()` 现在只**排队**
  （`_schedule_cache_prune()`：进程内单飞 + 沿用 60s 节流），GC 在 `image-cache-maintenance`
  守护线程上跑。发布本身仍是原子 `os.replace`，调用方要的是路径而不是 GC 的数字，且新鲜度
  下限（≥60s）本来就把"刚发布的条目"挡在逐出集合之外（`protect` 继续原样传递）。GC 失败只
  打 ERROR（缓存维持现状到下一趟），不再让一条**已经落盘**的发布失败。
  要观测一趟是否跑过，单元测试用 `wait_for_image_cache_maintenance()` 等它结束。
**默认不限（`E2B_IMAGE_CACHE_MAX_BYTES` 未设 = `0` = 不逐出）**，因为逐出的失败模式是
"静默打断活沙箱"：上界是**显式**的运维动作（两套清单都设 4 GiB），不是默认打开的行为。
`E2B_IMAGE_CACHE_MAX_BYTES=0` 下仍会回收残留（下一条），但**上界本身只约束 rootfs 条目**
（`_oci` 见下面单独那条），所以"不限"确实可以增长到"本地构建模板的 tar 总量"——要把它
也管住就得显式设 `E2B_IMAGE_CACHE_OCI_MAX_BYTES`。

- **候选**：只有带 `rootfs/.complete` 的**已完成条目**。暂存树（`.` 前缀）与未完成目录
  **永不**是逐出候选 ⇒ 不会踩到别人正在解压的条目。
- **上限是"总占用"的判据，能回收的只有 rootfs 条目**：`total_bytes`（下一条）**含**
  `_oci/*.oci.tar`、暂存残留、未完成条目与锁文件，判定 `> 上限` 时也按这个总数算；
  但逐出只动已完成条目，`_oci` 由它自己的上界管（见下）。
- **`_oci/*.oci.tar` 的独立上界（本轮新增，评审 follow-up F6）**：不配 registry 时那份 tar
  是本地构建镜像在**本节点**的唯一副本，所以既不能"按 cap 顺手删"，也不能假装它不存在。
  口径是**默认不回收 + 显式按龄回收**：
  `E2B_IMAGE_CACHE_OCI_MAX_BYTES`（默认 `0` = 一个 tar 都不删）给出 `_oci` 目录自己的字节上界，
  `E2B_IMAGE_CACHE_OCI_STALE_S`（默认 `86400` = 24h，`0` = 关掉这条路径）给出龄期下限；
  两个条件同时满足才回收，且**每条**还必须满足"卷上没有任何 `sandbox.json` 记录的
  `base_image` 指向这个 slug"（引用钉子同一信号）——拿不准（枚举不完整，见下条）时**一条都不回收**。
  每次回收都打一条 WARNING，带 tar 路径、字节数、实际龄期、以及代价说明
  （"这是无 registry 形态下的唯一副本，模板下一次冷创建前需要重新导出；把
  `E2B_IMAGE_CACHE_OCI_MAX_BYTES` 设为 0 可以保留全部 tar"）。实测
  （`tmp/zf7tail-oci.log`）：默认 3 个 tar 一个不动；设上界后只删"超龄 + 无人引用"的那一个
  （被引用的与新鲜的都留下）；列举不完整时同样一条不删。
  **运维口径**：无 registry 形态的卷容量按 Σ(本地构建模板 tar) + 4 GiB rootfs 规划，
  `_oci` 只设上界不做回收是默认；真要让 `_oci` 落在这个上界内，就要接受"被回收的模板在
  本节点上重新导出后才能冷创建"（实测报错 `failed to resolve image …:
  registry … Connection refused`，即落到 registry 回退并失败）。
  **观测**：`prune_image_cache()` 的返回值/日志里 `oci_bytes`、`oci_max_bytes`、
  `oci_reclaimed`、`oci_freed_bytes` 四个字段就是这项的指标；宿主侧
  `du -s --block-size=1 <cache>/_oci` 应与 `oci_bytes` 相等。
  **明确不做的**：不按"模板是否还在控制面数据库里"判断引用——解析器看不到控制面的模板表，
  能可靠看到的只有"这台机器上活着的沙箱记录"。用不可靠的信号删掉唯一副本比让它多占一点
  磁盘更糟，所以宁可默认不删并把这个口径写死在这里。
- **正在被引用的条目永远不逐出（评审 C2 的钉子）**：`sandbox.json` 是节点自己对"这台机器上
  存在哪些沙箱"的记录（teardown 之前一直在），解析器对每个 workspace base 下的
  `*/sandbox.json` 取 `base_image`（记了 `base_image_digest`/`image_digest` 就**只**钉住
  那一个条目），**这些镜像的条目一律跳过逐出**，无论它们多老、上限差多少。同时
  `_materialize_entry()` 把"刚发布的这一条"作为 `protect` 传给 GC，所以同一次调用绝不会
  逐掉自己刚发布的 rootfs（旧版 `MIN_AGE=0` 时会，`resolve` 返回一个不存在的路径）。
  **拿不准就不删（本轮改成 fail-closed，评审 follow-up F2/F3）**：下面三种"看不见"的形态
  以前都被当成"没有任何引用"、照常逐出（其中两种连日志都没有），现在一律**拒绝这一轮逐出**
  并打一条点名原因的 WARNING（`stats["eviction_refused"]` 里就是那段原因，探针
  `tmp/zf7tail-pins.log` 逐条实测；同形状在 `d0b7e5c` 上实测 `evicted 1`）：
  1. workspace base 存在但**列不出来**（`os.scandir` 被拒，例如平台自己建议过的 `0711`
     且属主不是 worker）——"没有记录"与"看不到记录"必须区分；
  2. 某条 `sandbox.json` **读不出来**（畸形权限/不可穿越的沙箱树）；
  3. 记录读得到但**用不了**（不是 JSON 对象、或没有可用的 `base_image`）。

  拿不到任何 workspace base（缓存不在 `<workspace-base>/_images` 且没设
  `E2B_WORKSPACE_BASE`/`E2B_SHARED_WORKSPACE_ROOT`）同样**拒绝逐出**并告警。
  残留回收（暂存/quarantine 树）**不受影响**：`.` 前缀的树不可能是活沙箱 chroot 进去的
  rootfs，所以 refusal 只挡住"逐出已完成条目"这一步。
  **代价要说清**：拒绝逐出意味着缓存会**停在超上限状态**直到引用集合可枚举；运维的修复
  动作是把 base 的可列权限/记录的可读权限修好（WARNING 里点名了具体路径）。这是刻意的
  取舍——"静默逐掉活沙箱的 rootfs"比"缓存暂时不回收"严重得多。
- **逐出序**：其余候选按 `.complete` 的 mtime **最旧优先**，直到真实占用 ≤ 上限；
  `_oci/*.oci.tar` 计入总量但**不逐出** —— 不配 registry 时那份 tar 是本地构建镜像的
  **唯一副本**，删了是"模板直接不可用"，而不是"重新拉一次"，所以宁可告警也不删。
- **新鲜度下限**：`E2B_IMAGE_CACHE_EVICT_MIN_AGE_S`（默认 300s）内的条目跳过逐出；这个值
  有 **60s 的硬下限**（配 `0` 也会被抬到 60s 并告警），因为逐出恰好发生在一次发布之后。
- **口径覆盖真实占用（评审 C3）**：`prune_image_cache()` 先把缓存**完整**量一遍——已完成
  条目、**有名字但没有 `.complete` 的残留**、**暂存/quarantine 树**（含被 `SIGKILL` 留下的）、
  `_oci/*.oci.tar`、锁文件——`total_bytes` 是**已分配字节**（`st_blocks * 512`）的总和：

  | 对账命令 | 是否等于 `total_bytes` | 说明 |
  |---|---|---|
  | `du -s --block-size=1 <cache>` | **相等**（就是这条口径） | 含目录自身的块、`_oci`、暂存残留、未完成条目、锁文件与 `.link` 侧车 |
  | `du -sb <cache>` | **不等**（apparent size） | 上一轮实测差 884 B（≈0.003%）；`du -sb` 按文件逻辑长度算，缓存里的小文件（锁文件、`.complete`）与稀疏/整块分配都会让两个口径分叉 |

  所以对账用 `du -s --block-size=1`；prune 结束后数字对不上就是 bug（不再出现"磁盘
  31.5 MiB、账面 15.8 MiB"那种差一倍的形态）。
- **崩溃循环的暂存峰值（评审 follow-up F6，属运维口径）**：回收只发生在
  `E2B_IMAGE_CACHE_STAGING_STALE_S`（默认 1h）之后，而 GC 是"冷解压之后每进程最多 60s 一次"，
  所以一次 CrashLoopBackOff 留下的残留峰值 ≈ 崩溃次数 × 单次树大小（用 review 实测的
  `python:3.11-slim` 条目 139 931 004 B 估 ≈ **1.7 GB 量级**）在窗口内**不可回收**，共享卷
  小于这个量级会被填满、>1h 后回落。要收紧就把 `E2B_IMAGE_CACHE_STAGING_STALE_S` 调小
  （代价见下面"已知边界"）或给卷留出余量；这一段是**已知不有界**的部分，不是"cap 兜住了"。
- **残留回收（只清可证明是垃圾的）**：`.{entry}.tmp-<pid>-*` 这类名带 pid；本进程**正在写**
  的暂存树记在进程内的活动集合里，GC 跳过；带**本进程 pid** 且不在活动集合里的、或者
  mtime 超过 `E2B_IMAGE_CACHE_STAGING_STALE_S`（默认 3600s）的，才删。别人的新鲜暂存树
  一律不动（可能是另一台 worker 正在解压）。**已知边界**：一次解压若超过 staleness 窗口，
  别的进程可能把它当残留删掉，那次解压会失败并被重试（默认 1h，远超正常解压耗时）。
- **上限不达标时**：若受引用钉子/新鲜度下限/`_oci` 制约仍超上限，打一条 WARNING 并把
  `kept/oci/oci 上界/staging/incomplete/loose/钉子数/下限` 写进日志（见 2.7.4），不静默；
  因引用集合不可枚举而拒绝逐出时打的是另一条 WARNING（点名原因 + 同一个占用分解）。
- **观测**：每次真的逐出或回收，worker/控制面日志出现一条
  `WARNING envd_service.runtime.image_resolver: image cache <dir> (cap <n> bytes): evicted …; reclaimed …; <total> bytes used (…)`；
  手工核对：`du -s --block-size=1 /var/lib/e2b-sandboxes/_images` 与 `_maybe_prune_cache` 报的
  `total_bytes` 相等（探针 `tmp/zf7fix-prune.log` 给的就是这两个数字）。
- **仍然存在的边界（明说）**：引用钉子基于 `sandbox.json`。`sandbox.json` 是沙箱可写的输入，
  一个能改写自己记录的沙箱理论上能丢掉自己的钉子（记了 digest 的记录不受影响）；另外
  `sandbox.json` 缺失但沙箱树还在的中间态不会被识别成引用。两者都需要宿主机侧的写权限，
  不属于"解析器自动逐出"能单方面造成的事故。

### 2.7.4 环境矩阵与开关

| 变量 | 默认 | 作用 |
|---|---|---|
| `E2B_IMAGE_CACHE_DIR` | `tmp/sandboxes/_images`（相对 CWD） | rootfs 解包缓存位置；生产设为 `/var/lib/e2b-sandboxes/_images` |
| `E2B_IMAGE_CACHE_MAX_BYTES` | `0`（**不限 = 不逐出**） | 缓存上限；compose/k8s 显式设 4 GiB；`0`/负数 = 不限（仍回收残留） |
| `E2B_IMAGE_CACHE_EVICT_MIN_AGE_S` | `300` | 逐出新鲜度下限（秒）；**硬下限 60s**，配更低会被告警并抬到 60 |
| `E2B_IMAGE_CACHE_OWNER_UID` / `_GID` | 未设 = 取最近的**非 root 祖先**的 owner（生产 = 卷根 owner = 65534） | 解析器创建的目录/文件归谁；两套清单显式设 `65534` |
| `E2B_IMAGE_CACHE_LOCK_TIMEOUT_S` | `300` | 等待同镜像的跨进程锁的上限（秒）；`0` = 永远等 |
| `E2B_IMAGE_CACHE_STAGING_STALE_S` | `3600` | 超过这个年龄的暂存/quarantine 树才允许按"超龄"回收；`0` = 只清本进程 pid 的 |
| `E2B_IMAGE_CACHE_OCI_MAX_BYTES` | `0`（**不回收任何 `_oci/*.oci.tar`**） | `_oci` 目录自己的字节上界；>0 时才启用按龄回收（见 §2.7.3） |
| `E2B_IMAGE_CACHE_OCI_STALE_S` | `86400` | `_oci` tar 的龄期下限（秒）；配合上一条使用（`0` = 关掉这条路径） |

生产形态（compose/k8s）与本地开发形态的差别只是"设不设 env"：不设时**落点**与修复前逐字
相同（相对路径），但**行为**有两处刻意的差别 —— 默认上限是"不限"（旧版没有逐出，所以默认
行为与修复前一致；上限是生产清单里的显式 4 GiB），以及 `MIN_AGE` 有 60s 硬下限。
本地 `pytest` / 单机开发不需要任何额外配置。

### 2.7.5 验收清单（本项）

1. 容器内 `docker compose -f deploy/stack/docker-compose.prod.yml config | grep E2B_IMAGE_CACHE_DIR`
   ⇒ 三个服务（control-plane / worker-1 / worker-2）`E2B_IMAGE_CACHE_DIR` /
   `E2B_IMAGE_CACHE_MAX_BYTES` / `E2B_IMAGE_CACHE_EVICT_MIN_AGE_S` /
   `E2B_IMAGE_CACHE_OWNER_UID` **逐字同值**；
2. 宿主上 `ls /var/lib/e2b-sandboxes/_images` 有 `_oci/` 与 `<image>-<digest>/rootfs/.complete`，
   且 `up -d` 重建后**不重新解压**（`grep "resolved base image" tmp/*.log` 不再出现同一条）；
3. **两个 uid 各跑一次解析都成功**：控制面（root）先跑一次、worker（`docker exec -u
   65534:65534`）再跑一次同一镜像，两边都拿到 `rootfs/.complete`，且 worker 能在该条目里
   写（`mkdir <entry>/rootfs/workspace`）；反向顺序同样成立（探针
   `tmp/zf7fix-uid.log`）；
4. **沙箱 uid 写不进缓存**：以池内 uid（如 10001）尝试在 `_images`、`_oci`、条目 rootfs
   里建/改文件必须 `Permission denied`（探针 `tmp/zf7fix-uid.log`）；
5. `du -s --block-size=1 /var/lib/e2b-sandboxes/_images`（**不是** `du -sb`，两个口径不等，
   见 §2.7.3 的对账表）与 GC 报的 `total_bytes` 相等，且 ≤ `E2B_IMAGE_CACHE_MAX_BYTES`
   （或日志里有"仍超上限"WARNING，并列出钉子数/`_oci` 上界）；
6. 两个 worker 与 control-plane 的工作目录/镜像缓存**互不冲突**：并发建箱同一模板不再出现
   `cannot resolve image` / 残缺 rootfs；被 `sandbox.json` 引用的条目在逐出后仍在
   （探针 `tmp/zf7fix-prune.log`）；
7. 单测门禁：`tests/unit/test_image_cache_sharing.py`（跨进程竞争、失败不误伤、重启复用、
   `_images` 不是沙箱树、上限逐出、`nolock` 形态、两个 uid、引用钉子、真实占用口径、
   `nolock`+残留不删已发布条目、锁超时）全绿。
8. `image-cache-init` 在每个形态都报 `image-cache-init: <dir> is owned by uid 65534`（compose
   的 `docker compose logs image-cache-init` / k8s 的 `kubectl logs <pod> -c image-cache-init`）；
   若它报 `FATAL … owned by uid <n>`，按它给出的 `chown -R 65534:65534 <dir>` 执行一次再重建
   （NFS 形态在服务端执行等价动作）——这是"旧卷一次性动作"的判定点；
9. 引用钉子 fail-closed：`0711` 的 workspace base 或不可读记录的形态下，逐出被拒绝且日志
    点名原因（探针 `tmp/zf7tail-pins.log`）；正常可列/可读形态下逐出照常发生。
10. 无 registry 形态的 `_oci`：默认（`E2B_IMAGE_CACHE_OCI_MAX_BYTES` 未设）**不回收任何 tar**；
    设了上界也只回收"超龄 + 无记录引用"的那些，每次回收都能在日志里看到 tar 路径、龄期与
    代价说明（探针 `tmp/zf7tail-oci.log`）。

## 2.8 空闲 HTTP 连接的关闭方向：服务端 keep-alive 必须大于客户端连接池窗口（2026-09-14）

**不变量**：每一条服务端 uvicorn 入口的空闲 keep-alive 必须**严格大于**它的 HTTP
客户端连接池空闲窗口 ⇒ 空闲连接由**客户端**先关。`>=` 不行（两个定时器会互相
竞争），反过来（服务端更短）正是本仓踩过的那一侧。

**本仓取值**（服务端两端都定义在 `gateway_common/keepalive.py`，改一端必须改另一端）：

| 端 | 常量 | 值 | 落在哪 |
|---|---|---|---|
| 服务端 | `SERVER_KEEP_ALIVE_S` | **120s** | 四个宿主入口（`control_plane/__main__.py`、`control_plane/combined_main.py`、`envd_service/__main__.py`、`envd_service/gateway_main.py`）都展开 `**uvicorn_keep_alive_kwargs()`；测试脚手架的 `_ServerThread` 同值 |
| 服务端（沙箱内） | `envd_service/mcp/gateway.py::SERVER_KEEP_ALIVE_S` | **120s** | `mcp-gateway` 是**拷进基础镜像**的独立脚本，镜像里没有 `gateway_common`，该值是内联的（见下） |
| 客户端 | `CLIENT_POOL_IDLE_TIMEOUT_S` | **90s** | 官方 SDK 的 pyqwest `pool_idle_timeout` 默认值，SDK 不覆盖 |

**违反后的症状**：SDK 的 `commands.run` 是 bidi 流（`process.Process/Start`），请求体是
流、**不可重放**（pyqwest 的 `ConnectionRetryTransport` 也重试不了它）。客户端在池里停
90s 后复用它，而服务端若在 uvicorn 默认的 5s 就关，这次 RPC 直接以
`pyqwest.WriteError: ... Connection reset by peer (os error 104)` 结束 —— 这正是整档第 9
轮 SDK 连接重置的签名（`http://127.0.0.1:38735/process.Process/Start`），同轮 MCP 网关上
还有 9 次来自 `envd_service/http/mcp.py` 的 `httpx.ConnectError`（网关起得慢、轮询把它拖
过了窗口）。

**沙箱内那一跳**：`deploy/docker/Dockerfile.mcp-base` 把 `envd_service/mcp/gateway.py`
`COPY` 成 `/usr/bin/mcp-gateway`，它在沙箱里独立运行，**只能内联**这个常量。因此
**改 `envd_service/mcp/gateway.py` 必须重建基础镜像**
（`docker build -f deploy/docker/Dockerfile.mcp-base -t python-mcp:3.14 .`，并按 §2.6.1
把它推回本地源），否则沙箱里跑的还是旧字节。验证手段：

- `tests/contract/test_mcp_gateway_keepalive.py`：源码里的 `timeout_keep_alive` 必须等于
  `gateway_common.keepalive.SERVER_KEEP_ALIVE_S`；**镜像里的 `/usr/bin/mcp-gateway` 必须与
  本树逐字节相同**（没重建就是红，不是静默漂移）；并真起一个镜像里的网关，证明同一条 TCP
  连接空闲 12s（**>** 旧的 5s）后仍能应答 —— 即"客户端先关"在**沙箱内**也成立；
- `Dockerfile.mcp-base` 有一条构建期断言 `grep -q 'timeout_keep_alive' /usr/bin/mcp-gateway`：
  拷贝丢掉这条 pin 时**构建就失败**，不会悄悄发出去；
- 宿主侧 `tests/contract/test_server_keepalive.py` 把两端常量与**安装版** pyqwest 默认值钉
  在一起，并在真 uvicorn 上复跑同一条"空闲后复用"断言。

**反例（不要这么做）**：把客户端窗口调大解决不了 —— 服务端仍是先关的那一侧；让 SDK 重试也
不行（bidi 不可重放）。唯一正解是把服务端的空闲窗口抬到客户端窗口之上（同 nginx
`keepalive_timeout 75s` > 浏览器 60s 的常规配比），代价只是空闲连接最多多留 115s。

## 2.9 worker 上的 MCP 网关端口带：61001–65535（2026-09-14）

每个带 MCP 的沙箱在 worker 网络命名空间里占一个宿主端口（`MCP_PORT`，由
`envd_service/runtime/context.py::McpPortPool` 发放）。该池的端口带是
**`_MCP_PORT_BASE = 61000`（发放 61001 起）到 `_MCP_PORT_MAX = 65535`**。这个区间是被
两条约束挤出来的，只有它能同时满足：

- **下界 50005（net_isolation 的硬约束）**：`net_isolation` 形态下，沙箱在自己的 loopback
  netns 里 `listen()`，由 supervisor 在**宿主 loopback 的同一个端口号**上起监听来服务它的
  `accept()`（S2.5 入站映射）。sandlock 对 `net_bind_map` 的校验要求
  `host_port >= 50005`，否则直接拒绝（`net_bind_map: host port ... is below the reserved
  inbound mapping range (50005+)`，`sandlock-core/src/sandbox/builder.rs`）—— 所以端口带
  **不能**像测试脚手架端口池那样挪到低区间；
- **上界在 `ephemeral` 区间之外**：容器内实测 `net.ipv4.ip_local_port_range = 32768-60999`，
  出向连接**不申请**就从那里取**源端口**；池的基址曾是 `51000`（正在区间内），发出去的
  端口可能已被别人的连接占着，而网关要到沙箱里 bind 才发现。于是 `61000-65535` 是这个
  窗口的**唯一**取值（≥50005 且 > 60999）；
- **与测试脚手架端口池（20000–28000）不相交**；
- **有界**：发满 4535 个即**拒绝分配**（`RuntimeError`）而不是越界借一个共享端口。

`McpPortPool.allocate()` 另外对每个候选**实际做一次 bind 校验**（`0.0.0.0` +
`SO_REUSEADDR`，与网关自己的 bind 同形）：被别的 listener 占着的号直接跳过，不发给网关。

**运维含义**：worker 主机（或 k8s 节点）上 **61001–65535 必须留给 MCP 网关**，不要被别的
服务、端口转发或防火墙预留占用；该区间的监听者会让对应端口被池跳过（日志
`MCP gateway port <n> is already held; skipping it`），占满则建箱在分配端口处失败。
另外**不要**把宿主/worker 的 `net.ipv4.ip_local_port_range` 上界调高到 61000 以上（例如
`1024 65535`）——那会把整个端口带重新拖回出向连接的源端口池里；本仓按默认
`32768-60999` 设计。

## 3. 运维要求

### 3.1 quota 管理（E2B worker 自动执行）

```bash
# 创建沙箱
xfs_quota -x -c "project -s -p /var/lib/e2b-sandboxes/<id> <projid>" /
xfs_quota -x -c "limit -p bhard=<quota>M <projid>" /

# 删除沙箱
xfs_quota -x -c "project -C -p /var/lib/e2b-sandboxes/<id> <projid>" /
rm -rf /var/lib/e2b-sandboxes/<id>
```

- projid 范围：1..2^31-1（32 位 project id），沙箱 id 哈希分配；
- 孤儿清理：worker 启动时对账 `sandbox.json` 与 quota 表，删除无主 project；
- 监控：`xfs_quota -x -c "report -p" /` 定期巡检，超限沙箱告警。

### 3.2 兼容性注意

- **EDQUOT 而非 ENOSPC**：超限写返回 `EDQUOT`，需确认沙箱内工具能正确处理
  （大多数处理一致，个别只处理 ENOSPC 的程序要验证）；
- **内核版本锁定**：开启 project feature 后，旧内核（< 4.5）无法挂载该文件
  系统——升级/回滚内核要评估；
- **挂载选项必须配套**：`prjquota` 挂载选项与 quota feature 配套，部署脚本
  要保证两者一致（漏挂 = quota 不生效但不报错）。

### 3.3 环境矩阵

| 环境 | quota | 说明 |
|---|---|---|
| 生产（目标机 XFS） | ✅ 生效 | 本文档要求 |
| 测试机（OrbStack 容器内 XFS loop） | ✅ 生效 | 需 `--privileged` + losetup 挂载 |
| 开发机（macOS / Docker Desktop / OrbStack 默认 VM） | ⏭️ 降级跳过 | E2B 检测不到 XFS 则跳过 + 警告 |
| CI（Linux 任意 fs） | mock | 单测不依赖真实 quota |

镜像 rootfs 缓存（`_images`）**不在任何沙箱 projid 内**：生产形态靠
`E2B_IMAGE_CACHE_DIR` 把它放到共享卷、`E2B_IMAGE_CACHE_MAX_BYTES` 给它自己的上界
（**这个上界只约束 rootfs 条目**；`_oci/*.oci.tar` 另有 `E2B_IMAGE_CACHE_OCI_MAX_BYTES`
且默认不回收，无 registry 形态的容量要按 Σ(本地模板 tar) 一起规划，§2.7.3；两种形态在这点上**是同一个事实**——compose 的 quota-agent 是 opt-in
`profiles: ["quota"]`，且 `_images` 本来就是保留命名空间、永远不进配额扫描）；
开发形态保持相对路径默认，不受影响。

## 4. 验收清单（部署后）

1. `grep " / " /proc/mounts` 含 `prjquota`；
2. `xfs_quota -x -c "state" /` 显示 project quota enabled；
3. 创建沙箱后 `xfs_quota -x -c "report -p" /` 能看到该沙箱的 project 与限额；
4. 沙箱内写超配额 → 命令报 `EDQUOT`/写入失败；
5. 删除沙箱后 project 记录清理；
6. 迁移沙箱（worker 间）→ 配额保留；
7. 非 XFS 环境创建沙箱 → 正常创建 + 日志警告"project quota unavailable"。
8. 镜像缓存（§2.7）：`E2B_IMAGE_CACHE_DIR` 三个服务同值且落在共享卷上，重建容器后
   `_images/**/rootfs/.complete` 仍在、不再重新解压；root（控制面）与 65534（worker）
   都能解析并写入同一条目；`du -s --block-size=1` 不超（显式设置的）上限且与 GC 报的
   `total_bytes` 相等（**不是** `du -sb`）；`image-cache-init` 报的属主是 `65534`。

## 5. NFS 共享存储形态（E6.4 实测结论与部署要求）

多节点共享 workspace/volume 采用 NFS（或等价 CSI）时，必须满足以下
语义；实测（2026-09-02，容器内核 nfsd + XFS prjquota 导出 + 两个 NFS
客户端挂载）结论如下。

### 5.1 路径语义与迁移

- **路径语义**：同一导出挂在两个 worker 上，`volume/<sandbox_id>/` 与
  workspace 目录完全一致（两挂载点文件列表、md5 相同），worker 侧无需
  区分“本机目录”与“共享目录”。
- **迁移保留文件**：`E2B_SHARED_WORKSPACE_ROOT` / `E2B_SHARED_VOLUME_ROOT`
  下的迁移只切路由、不搬文件（keepFiles），实测 NFS 上文件与 projid
  均保留；跨挂载点读写正常（迁移目标 worker 直接读到源文件）。
- **各节点挂载选项必须一致**：`rw,sync,no_subtree_check`（及 NFSv3
  `nolock` 如无锁服务）；`vers` 与 `sec` 需一致，否则配额与权限行为
  随节点漂移。

### 5.2 配额专项（服务器端 XFS + prjquota）

配额在 NFS **服务器端**执行；客户端通过 NFS 拿到的行为：

1. **projid 继承**：服务器端 `xfs_quota project -s` 设置
   `volume/<sandbox_id>/` 后，NFS 客户端在该目录创建的文件（含子目录
   递归）projid 正确继承（实测 `lsattr -p` = 目标 projid）。
2. **EDQUOT 传播形态**：服务器端超限返回 EDQUOT，NFS 客户端表现为
   **ENOSPC（errno 28）**，不是 EDQUOT——沙箱内工具需同时处理
   ENOSPC（多数只处理 ENOSPC 的程序反而正确）。
3. **sync 挂载**（建议，E2.6 concern）：写入在达到硬限额的写调用上
   立即返回 ENOSPC（探针断言：客户端写入计数 = 限额且 errno 28；服务器端
   `stat -c %s` = 限额字节，文件恰在限额处截断）。
4. **async 挂载**（不推荐）：全部写入进入页缓存后，`close()` 与
   `fsync()` 均返回 ENOSPC（探针断言：客户端写入 `限额+8` MiB 全部成功、
   两个错误路径均 errno 28；服务器端 `stat -c %s` 落盘量**少于客户端
   写入量且不固定**——本轮实测 close.bin 22MiB / fsync.bin 0MiB，异步
   突发下服务器可能已越过硬限额——忽略延迟错误会**静默丢数据**）。生产
   必须 `sync` 挂载，且命令执行器在写路径上显式 `fsync` 并透传错误。
5. **多 worker 独立限额**：两个 worker 同时写各自
   `volume/<id>/`（不同 projid），各自独立达到硬限额，服务器端 report
   分项目记账、互不串扰（探针断言：两客户端各自写入计数 = 限额、errno 28，
   report 显示 projid 1004/1005 各自 16MiB 截断）。
6. **root_squash / uid 映射**：
   - `root_squash` 下客户端 root 映射为 nobody：projid 继承与配额记账
     不受影响（实测文件属 nobody、projid 仍继承、限额仍生效）；
   - 但**沙箱目录权限必须可写**：volume 根建议 `1777` + 每沙箱独立
     子目录（E3.2 模型），否则 squashed 写被 EACCES 拒绝（实测 755
     root 属主目录 → EACCES）；
   - **E5.1 独立 uid 模型与 root_squash 冲突**：sandbox 内 uid 0 经
     NFS 被 squash 后失去独立 uid 语义，文件统一为 nobody 属主，租户间
     NFS 文件隔离退化为“目录权限”而非“uid”。生产若开启 per-sandbox
     uid + NFS 共享卷，建议导出用 `no_root_squash`（内网可信 NFS）或
     改用 `anonuid=<per-sandbox-uid>` 映射（需 NFS 支持 per-export
     配置），并在部署验证中逐项核对。

### 5.3 部署待验证项（环境限制）

本次实测在 OrbStack 容器内内核 nfsd 完成；OrbStack 宿主自带 NFS 代理
占用标准端口（2049/20048）且状态随宿主变化，容器化 nfsd 的线程/导出
表为 VM 内核全局状态，重复自动化运行不稳定（ESTALE / 挂载竞态），因此
**以下项需在真实生产 NFS（Linux 目标机）上复核**：

- `deploy/scripts/nfs_quota_probe.sh` 在目标机 NFS 挂载点重跑全 6 项
  （A projid 继承 / B sync ENOSPC + 服务端截断 / C async close/fsync
  延迟报错 + 服务端落盘量核对（少于客户端写入） / D 多 worker 独立限额 /
  E root_squash / F 共享路径+迁移保留）；
- 生产 NFS 的 `no_root_squash` 与 per-sandbox uid 组合是否保留 uid 隔离；
- NFSv4 与 v3 在目标内核/导出配置下的行为差异（本次用 v3）；
- 配额巡检（`xfs_quota report -p`）在服务器端持续校验，与 worker
  心跳告警对齐。
