# pure（无 chroot）形态：要修、要换真根，还是让它显式选择

**2026-09-22。这是一份决策文档，不含代码改动** —— 它要把 backlog 里四条挂着的东西
合成一个决定：**N15**（pure 形态只有 Landlock 一道网）、**OBS-5**（该形态没有磁盘硬闸门）、
**N27**（平台状态可见性依赖形态）、**N14**（用 mount ns + pivot_root 换掉"虚拟根"）。
它们不是四个问题，是**同一个缺失**的四个面：pure 形态没有 rootfs，也就没有路径中介。

> **引用约定（2026-09-27 更新）**：本文正文里的 `tmp/**`（`.log`、`tmp/k0s/task*/` 等）与
> `.superpowers/sdd/**` 都在仓库 `.gitignore` 里，**不是仓库路径**。可重跑脚本已迁到
> [`deploy/scripts/acceptance/`](../deploy/scripts/acceptance/)（原名不变），报告迁到
> [`docs/reports/`](reports/)（原名不变）；仍写成 `tmp/**.log` 的都是**原始日志**（会被清，
> 可重跑，脚本见 `deploy/scripts/acceptance/`）——判据不要只挂在日志上。

## 1. 今天这个形态是什么、怎么落进去的

不是"可选特性"，是**没有镜像 rootfs 时自动落到**的那条路：

```
mediation_shape = bool(settings.base_image and image_rootfs is not None)
```

（`envd_service/executors/sandlock.py`）。生产清单设了 `E2B_BASE_IMAGE=python-mcp:3.14`
⇒ 走 chroot/image-rootfs，路径中介在；把 base image 留空（或解析不到 rootfs）⇒ **沙箱
直接落在宿主根下，只有 Landlock**。平台自己的 lane 里就有这样一个 gate（
`test_pure_shape_workspace_ownership` 的 skip 理由写着 "requires an empty E2B_BASE_IMAGE"），
所以这不是纸上的形态。

## 2. 缺什么（实测，不是推断）

| 面 | chroot 形态 | pure 形态 |
|---|---|---|
| 路径可见性 | 路径 syscall 全被中介，`chroot_root` 翻译 | **Landlock 一道网**；Landlock 访问位是闭集，"带路径但不在闭集里"的调用无人拦 |
| 具体暴露 | — | 同一宿主文件上实测：`openat` EACCES，而 `getxattr` **读回宿主 xattr**、`open_tree` **返回 fd**、`inotify_add_watch` **投递宿主事件与宿主文件名**（OBS-7）；`path_surface.rs::PURE_UNGATED` 把这一类**逐条 pin 成 33 条**（stat/readlink/chdir/chmod/utimensat/*xattr/inotify_add_watch + 5 条 at 风格，其中 5 条在当前内核 ENOSYS 或被 worker seccomp 档拒） |
| 磁盘闸门 | 中介的**活账本**：`openat` 按剩余额度发上限、超预算建条目 ENOSPC、unlink 即时归还、N31 的条目计数闸门 | 只有 init 在 fork 里施加的 **per-exec `RLIMIT_FSIZE` 硬上限**（单文件、shape 无关，仍生效）；**没有活账本**：跑飞的写者可以一直写到自己那条 exec 的额度，`diskMB` 那层语义在这个形态下不成立（OBS-5） |
| 平台状态可见性 | `_runtime` 对沙箱 ENOENT（沙箱的根是它自己的 rootfs） | `/home/user` 就是 `<base>/<id>` 的真实路径 ⇒ `..` 到 `<base>`，`_runtime` "看得见但打不开"（DAC `0700` → EACCES）。**不是洞，但是形态漂移**（N27）。**N27 上线后（2026-09-26）**：树根下沉一级，`/home/user` = `<export>/workspaces/<id>`、平台状态在 `<export>/state/` ⇒ **有根形态**（生产 image-rootfs、pure+合成根+真根）**既不在祖先链上、也读不到**（`ENOENT`）；但**无根 pure identity**（`E2B_PURE_ROOTFS=off`；2026-09-27 起它只是**退回杆**，默认已翻到合成根）**只成立一半** —— 四次 `stat` 全 `EACCES`（读不到 ✔），而 `../..`（= `<export>`）能列出 `state` / `_secrets` 的**名字** ⇒ 形态漂移**未完全消除**。这条残差**不是 N27 引入的**（迁移前同形态在 `..` 一层就列出 `_runtime`），是"没有根"这件事本身，**N16（合成根）才是消掉它的那条路**。实测（四种形态对照）见 `docs/deploy-clusters.md` §11.2 |

## 3. 三条路

**A. 给 33 条补统一闸门（N15 ①）**
给 `NotifPolicy` 加非 chroot 的 readable/writable 集合 + 一个"仅 Landlock 档位"标志，
对"Landlock 覆盖不到的带路径 syscall"注册统一策略判定（`dup_fd_from_pid` 代执行，同
`handle_chroot_inotify_add_watch`）。worklist 现成、被单测 pin 住。
*代价*：33 条里每条都要决定语义（`chmod` 是 DAC 门控的、`readlink` 要落在 root 内……），
而且**新增内核 syscall 会继续进这个桶**（有单测兜，但每加一条就要处理一次）；
它关掉的是"元数据泄漏"，**关不掉 OBS-5 与 N27**（那两件是"没有中介"本身）。

**B. 换真根：mount ns + pivot_root（N14）**
pure 形态也合成一个真根（bind `/usr`、`/lib`、`/bin`、`/opt`、workspace、`minimal_dev` 后 pivot）。
*一次关掉整类*：路径空间变成内核不变量，`chroot_path_syscalls()` 的完整性不再承担安全职责；
`/workspace` 与 `/home/user` 同一宿主目录的别名由 bind mount 天然表达（可删掉 chdir 记录机制）；
exec 的 `PT_INTERP` 补丁 + memfd 那套可删（内核按新根解析解释器）；OBS-5 与 N27 也随之落地
（状态在真根之外 / 绑定视图之内）。
*代价*：core 现在**完全不建 mount ns**（`CLONE_NEWNS` 只出现在"拒绝 clone 命名空间"的过滤器里），
要先做宿主兼容性矩阵；`fs_mount`（workspace/卷/`minimal_dev` 六节点）全在 rootfs 之外 ⇒
必须引入 mount namespace 与整套 bind 装配；这是三条里唯一动**形态语义**的。

**C. 让这个形态变成显式选择（fail closed 默认）**
不修能力，先修"默认"：没有 rootfs 时**拒绝建箱**（给出可读原因），除非运维显式设置
（例如 `E2B_ALLOW_PURE_SHAPE=1`）—— 平台自己的 gate B 改成显式设置它。
这与本仓库既有的做法一致（SL-1 的路径中介"建箱前拒绝且没有降级档"、own-identity "强开而前置
不满足 ⇒ 建箱报错，不静默退 in-process"）。
*代价*：几乎没有（一个开关 + 一段文档 + lane 里那一处显式设置）；*收益*：把"默认部署下
一不小心就落到无中介形态"这件事**变成一次显式决定**。它**不修** pure 形态本身的三个缺口。

## 4. 决定（2026-09-23，用户）

**不要把这四条捆在"要不要支持 pure 形态"的决定后面 —— 前三条各自独立，可以现在就做。**
本文档原先把它们打成一个包，是错的：它们只是**共享一个触发条件**，不是共享一个前提。

新的分工：

| 项 | 是什么 | 独立性 | 代价 |
|---|---|---|---|
| **N15 + OBS-5** | **一件工作**：给"没有 rootfs 的形态"补路径中介 —— 33 条 `PURE_UNGATED`（元数据泄漏）与活账本（`diskMB`/条目闸门）是**同一个缺失**的两个面（今天那个形态的 mediator 什么都不中介："auto keeps the pure (no-chroot) shape in-process: it mediates nothing"） | **不依赖任何 pure 决定**：它是"只要沙箱没有 rootfs 就生效"的代码 | 中：策略面（非 chroot 的 readable/writable + "仅 Landlock"档位）+ 33 条逐条定语义（清单已被单测 pin 住）+ 写路径记账 |
| **N27** | E2B 侧把平台状态搬到沙箱永远不经过的 base（`E2B_STATE_BASE` + 挂载 + 一次性迁移） | **已落地（2026-09-26）** —— 原本"完全独立、任何时候都能做"；实际选的是**同挂载 + 树根下沉**（用户裁定见 `docs/superpowers/plans/2026-09-26-decisions.md` 第 1/2 条） | 低：同挂载 `rename(2)`（秒级、可原路回退），原列的 EXDEV 账**不发生**；上线状态与验收口径见 §5 第 2 条 |
| **N14** | 用 mount ns + pivot_root 换"虚拟根" | **可选替代路线**，不阻塞上面两件 | 大：core 引入 mount ns + 宿主兼容矩阵 + exec 路径简化 |

**做完 N15 + OBS-5 之后，"要不要支持 pure"这道题基本消失**：那个形态不再是"只有 Landlock
一道网"，只剩"没有内核级根"，风险从"泄漏宿主元数据"降到"覆盖度"。也就是说——
**不需要先做产品决定，做工作本身就把问题变小了**。

## 5. 于是现在的路线（不再需要你拍这一条）

1. **N15 + OBS-5 作为一个工作包开工**，路线已用实验选定：**不给 33 条另写一套代执行，
   而是把 pure 形态的根设为 `"/"`**（虚拟路径 == 宿主路径，identity 翻译），直接复用
   现成的 chroot handler 与活账本。实验（2026-09-23，改动已回退以保持树绿）证明这条路
   的核心验收一次通过：原先 `xfail(strict=True)` 的
   `tests/security/escape/test_path_surface_inotify.py::test_pure_shape_inotify_still_reaches_the_host_root`
   变成正向断言通过（宿主目录被拒 + 无事件泄漏，沙箱自己的 workspace 仍可 watch）。
   代价与前置也随之确定：**pure 形态从此需要 own identity 槽位**（否则中介以 euid 0 跑被
   SL-1 守卫拒 → fail closed，与既有纪律一致），因此**第一步是把 29 条假定"纯形态不中介"
   的测试迁移过来**（清单与证据见 backlog N15 行：8 个 security 文件里直接构造
   `SandlockExecutor` 的用例改用 `own_identity_sandbox(None, None)`、选择矩阵那条改成"自动上
   槽位"、另有 4 条形态差异），迁移后跑 **gate B（`E2B_BASE_IMAGE=""`）+ 默认档**双复验。
   验收线照 chroot 形态的现有契约等强 —— 宿主文件的存在性/大小/时间戳/inode/链接目标/
   xattr/事件都不可见，且 `diskMB` 那层账本在该形态下也有等价物。
2. **N27 已落地（2026-09-26，E2B 侧）**：落点全部兑现 —— `gateway_common/paths.py` 的 helper 认
   `E2B_STATE_BASE`（未设 = 树根 ⇒ 逐字节零变化）+ 清单把树根下沉一级（树 `<export>/workspaces/<id>`、
   平台状态 `<export>/state/`，**同一个挂载**）+ 一次性迁移脚本（`deploy/scripts/migrate-state-base.sh`，
   同挂载 `rename(2)`；集群实测 `done=12 unknown=0`、逐条 `same_inode=yes` ⇒ 原先记的 EXDEV 账不发生）。
   **形态无关性（按 2026-09-26 集群实测限定）**：**有根形态**（生产 image-rootfs、pure+合成根+真根）
   平台状态**既不在祖先链上、也读不到**（`ENOENT`）；**无根 pure identity**（`E2B_PURE_ROOTFS=off`）
   **只成立一半**（**2026-09-27 前是默认档**；现在默认已翻到合成根，它只是退回杆）—— 四次 `stat` 全 `EACCES`（由中介按策略拒绝 ⇒ **读不到** ✔），但 `../..`（= `<export>`）
   能列出 `state` / `_secrets` 的**名字** ⇒ **不写回"完全形态无关"**。这条残差**不是 N27 引入的**
   （迁移前同形态在 `..` 一层就列出 `_runtime`），是"没有根"这件事本身，**N16（合成根）才是消掉它的
   那条路**。两种形态都**不是"能读"** —— 这半边仍成立 —— 所以验收**不再**是"pure 形态下从沙箱内
   `stat(<新 base>)` 为 ENOENT"这条单形态判据（该判据随 N15 的中介化作废）。证据：`docs/deploy-clusters.md`
   §11.2 的形态对照表 + 探针 `deploy/scripts/acceptance/probe_state_base_visibility.py`。**回退窗口**：旧
   `<export>/_runtime` 不需要保留副本 —— `--rollback` 是同一张映射表的反向 `mv`
   （`state/_runtime` → `<export>/_runtime`，inode 保留、不拷数据），依据是留在盘上的
   `state/.state-base-migration.journal`（0600），一个发布周期内不删它即可原路退回。
   证据：`docs/deploy-clusters.md` 的 N27 上线记录节 + 探针 `deploy/scripts/acceptance/probe_state_base_visibility.py`。
   **（2026-09-27 更新：同一支探针按 lane 三档重跑复核，结论逐字未变）** —— `synth` + `E2B_REAL_ROOT=1`
   档**已消掉**"能列出名字"（`chain=PASS`，`<export>` 根本不在祖先链上）；**当时的默认档（`identity`）仍有残差**
   （`<export>` 一层 `LEAK ["_secrets", "state"]`、四次 `stat` 仍 `EACCES`）；legacy 反例档 `exit 1`
   ⇒ 判据非恒真。要让**默认**形态也消掉，唯一一步是把 `E2B_PURE_ROOTFS` 的默认值从 `off` 切到 `synth`
   （代价见 §7）。**（2026-09-27 后半：已拍板切并落地 —— 默认值现在是 `synth`；上面那几句读作
   「切换之前」的实测，切换记录见 §7 的「默认已切（2026-09-27）」一节。）** 原始输出
   `tmp/k0s/n27resid-*.log`，逐档表见
   `docs/deploy-clusters.md` §11.2、报告 `docs/reports/n27-identity-residual-report.md`。
3. **N14 挂在 N15+OBS-5 之后评估**：如果 33 条做完之后仍觉得"拦截清单完整性"这层负担不值，
   再走真根；那时它是个优化，不是前提。
4. **顺带**（与上面不冲突，且很小）：今天的默认仍是"base image 忘配 ⇒ 静默降级到无中介形态"。
   在 N15+OBS-5 落地前，我建议把那一段改成**显式选择**（fail closed 默认、lane 里显式打开），
   否则"忘了配"就等于"悄悄少一道墙"。

## 6. 实施记录（2026-09-25/26）：pure 形态已中介化

**产品改动**（`envd_service/executors/sandlock.py`）：pure 形态（无 base image）与镜像形态走
**同一条**中介路径 —— `_chroot_root` 返回宿主根 `/`（identity 翻译），`fs_mount` 把 workspace
挂在 `/home/user` 与 `/workspace`，`fs_readable/fs_writable` **保持原样的白名单**（不因为
有了中介就放开 `/`：那是镜像形态"整颗 rootfs"的写法，在宿主根上等于放开整个文件系统）。
配套三处，都是"root 不再是 jail"的直接后果：

* **cwd 必须是宿主路径**：fork 的启动 cwd 是 `chroot_root.join(cwd)` 的真实 `chdir`；镜像形态给
  虚拟 `/home/user`（rootfs 里有这个目录），pure 给同样的值就落到宿主的 `/home/user` → ENOENT。
  现在 pure 给宿主 workspace 路径，中介再经挂载表映射回 `/home/user`（`pwd` 仍是它）。
* **ceiling 要写回 `kwargs`**：`_policy_ceiling` 的 dict 在形状分支**之前**就建好了（镜像形态靠
  `fs_readable` 含 `/` 恰好绕开这条检查），只改局部变量是死代码 ⇒ per-exec cwd 被 fork 拒
  （`exec params exceed the instance policy ceiling`）。
* **凭据文件跟读者走**：own-identity 槽位以**沙箱的 uid**运行，而 E2B 原先把 http-auth 的 secret 写成
  `0600 root` ⇒ 槽位读不到，策略校验直接失败（`invalid sandbox: credential file … Permission
  denied`）。现在 chown 给 `host_uid`（文件仍 `0600`）；这不是暴露 —— 它落在沙箱所有 fs 授权
  之外（镜像形态在 rootfs 之外，pure 形态在 `can_read` 白名单之外）。

路由选择：`_own_identity_decline_reason` 的 `mediation_shape` 现在对**所有**形状为真（`auto` 到处上
槽位，因为中介必须以沙箱自己的 uid 跑，T5）；`_in_process_mediation_is_refused` 同理不再对 pure
短路。拿不到槽位且中介只能以 root 跑时**照样 fail closed**（SL-1），不再有"共享 uid 的 root
worker + pure"这条能跑但不中介的路。

**fork 侧两处"root 不是 jail"**（同一个 bug 的两个面，都带回归用例）：

* `compose_virtual_etc_hosts` 在 `root="/"` 时读的是**宿主** `/etc/hosts` ⇒ 宿主能解析的名字以
  **字面 IP** 进沙箱，`allowOut` 规则再也拦不住（实测：测试往宿主 hosts 加
  `127.0.0.1 api.egress.test` 后，沙箱直连 loopback，egress 代理根本没看到连接，两条 wildcard
  用例 ECONNREFUSED）。现在 `root="/"` 与"没有镜像"等价。
* 凭据暴露警告把 chroot 根当成一条 grant ⇒ `root="/"` 时**任何**宿主路径都"在授权内"，警告变噪音。
  该形状的可达集合是策略白名单（`can_read`），警告只看它。

**顺带修掉的真 bug（与 N15 无关，是它把测试推到了能发现的位置）**：两个同时活着的 wildcard
沙箱会撞 DNS 网关地址 —— `dns_synth::allocate_gateway_addr` 是**进程内**计数器，而路由 B 下
每个沙箱一个 supervisor 进程、每个都从 `127.0.1.1` 开始 ⇒ 第二个槽位启动即
`bind DNS gateway: Address already in use`。现在改成**探测式**取地址（fork `8f9c8d2`，含
`test_a_held_gateway_address_is_skipped_not_fatal`）。

**验收（两档全量）**：

| 档 | 命令 | 结果 |
|---|---|---|
| gate A（镜像形态，默认） | `deploy/scripts/acceptance/gateA-full.sh`（= phase 1 的形状 + `E2B_BASE_IMAGE=python-mcp:3.14`） | **1772 passed / 6 skipped / 3 xfailed / 0 failed**（581 s） |
| gate B（pure） | `deploy/scripts/acceptance/gateB-full.sh`（同形状 + `E2B_BASE_IMAGE=`） | **1765 passed / 13 skipped / 3 xfailed / 0 failed**（436 s） |
| phase 2（非 root worker） | `deploy/scripts/acceptance/phase2.sh` | **57 passed / 1 skipped / 0 failed** |
| security 两态（`E2B_REAL_ROOT=0/1`） | `deploy/scripts/arm-lane/x86-security.sh` | 默认档 44 passed / 1 skipped / 3 xfailed；pure 42 passed / 3 skipped / 3 xfailed |
| `tests/unit`（macOS 本机） | `.venv/bin/python -m pytest tests/unit` | 16 failed / 1164 passed（**基线未变**：gateway/priv_helpers/real_root_gate/xfs_quotactl） |

> **`test-prod-shaped.sh` 不能跑 gate B**：它的 `-e E2B_BASE_IMAGE="${E2B_BASE_IMAGE:-…}"` 会把
> **空值**变回默认镜像（`:-` 对"已设但为空"同样取默认）。`deploy/scripts/acceptance/gateB-full.sh` 就是它 phase 1 的
> 复制品，只把这一处写成真正的空。

**N15②（ETXTBSY 一拍窗口）已随本条一起被覆盖**：`test_user_cli_install_within_workspace_persists`
（"写脚本 → chmod +x → 立刻执行"，即 N35 那条同拍窗口）现在跑在 pure+中介形态上并**通过** ——
fork 侧 `handle_chroot_exec` 的 `settle_closed_writes` 在每次 exec 前结算并释放持有中的写描述符，
两条形状共用同一段代码。

**顺带在验收里修掉的 9 条既有红**（都不是 N15 引入，用"把 E2B 改动静音、同一个镜像再跑一遍"
归因出来的）：6 条是 gate B 既有红（`mcp_gateway_keepalive` ×2 需要 base image、`checkpoint_store`
×2 与 `disk_scan_offload` ×1 在容器 root 下的机制/调度假设、`pure_shape_workspace_ownership` 读的
是平台状态分离**之前**的 record 路径），3 条是 N15 引起（missing-binary 契约：授权外的路径现在按
**拒绝**答 EACCES 而不是"不存在"，这正是 N15 关掉的存在性 oracle —— 契约用例改用白名单内的路径，
并把"授权外 = EACCES + 一行诊断"按逐字节断言钉住）。

## 7. N16（2026-09-26）：pure 的第二种根 —— 合成骨架

**先更正 §6 里一句与实测冲突的现况描述**（Task 11 报过）：§6 的三处配套里写着"现在 pure 给宿主
workspace 路径，中介再经挂载表映射回 `/home/user`（`pwd` 仍是它）"。实测（
`tmp/k0s/task11/probe-boundary.log`、Task 10 §2.4）**identity 态的 `pwd` 报的是宿主 workspace 路径**，
`cd /home/user` 直接 `can't cd to /home/user`（rc=2）——**`/home/user` 这个别名只在有根形态存在**。
挂载表映射管的是 exec 的 cwd 参数，不是 shell 的 `pwd`；把那句读成"identity 下别名可解析"是错的。

**N16 是什么**：`E2B_PURE_ROOTFS=synth`（**2026-09-27 起是默认**，`off` 是退回杆）时，pure 沙箱拿到**每沙箱一份的合成骨架**
（`<base>/_pure_rootfs/<id>`，普通目录 + bind 系统目录 + 整棵 `/dev`），fork 在沙箱自己的 mount ns
里 `pivot_root` ⇒ pure 也有内核根，`E2B_REAL_ROOT=1` 于是在 pure 里也开得起来。产品侧只动
`kwargs["chroot"]` **一个值**：`_chroot_root` 从 `"/"` 变成骨架目录，中介、策略、COW、活账本一行
不退。机制、`/dev` 与 `/etc` 的取法、两处 fork 特例在新形态下的身份（以及"合成根绝不绑宿主
`/etc` 或凭据目录"这条前提）写在 `docs/n14-retire-the-emulation.md` §5.3。

**"合成根 + 模拟根"结构性不成立 ⇒ 配置守卫**（2026-09-26 追加裁定，选 A）：bind 只在真根那条
路径上发生，`=0` 的模拟形态把虚拟路径翻译进**空骨架** ⇒ 生成期 `execvp("/bin/sh")` errno 13 ⇒
container 崩塌 ⇒ 之后每个 verb 都答 `InstanceClosed`（security 两态实测：`=0` 1 failed / 14 passed /
32 errors，`=1` 1 failed / 44 passed）。因此 `E2B_PURE_ROOTFS=synth` 且 `E2B_REAL_ROOT=0` 时
**`create_app` 当场 loud 拒绝**（`RuntimeError`、exit 1，照 SL-1 与 `real_root` 无根那条的既有风格
写明出路），配一条 E2B 侧红用例 + 这句文档。

**两态的新读法**（同一裁定）：纯形态的"两态"是 **`=0` ⇔ N15 identity（不设根）**、
**`=1` ⇔ 合成根 + 真根**。两态仍然都在（`gateB-pure-rootfs.sh 0|1`），但**默认档从 identity 换成了
合成根**（下一节）。

**默认已切（2026-09-27）：`E2B_PURE_ROOTFS=synth` + 成对耦合的 `E2B_REAL_ROOT`**

用户裁定（本条）：**默认切成 `synth`** —— 残差的成因是"没有根"本身（§5 第 2 条），所以消掉它只能
靠默认档换根。切换前 lane 三档的复核（`synth` 档 `chain=PASS`、默认 identity 档 `LEAK
["_secrets","state"]`、legacy 反例 `exit 1`）证据见 §5 第 2 条与 `docs/deploy-clusters.md` §11.2。
**切换后（2026-09-28）在 lane 上复核过默认档本身**：不设任何键 = `own_identity_active=True has_root=True`、
`stat=PASS`/`chain=PASS`/`exit 0`，而 `E2B_PURE_ROOTFS=off` 仍 `LEAK ["_secrets","state"]`/`exit 1`
（日志 `tmp/n27-default-synth-lane.log` / `tmp/n27-off-identity-lane.log`）；同一轮修掉探针在浅路径上
（沙箱里那份 `/home/user/n27-checker.py`）算 `parents[3]` 的 `IndexError`，pin 见
`tests/unit/test_n27_probe_cli.py`。

**两个默认怎么一起动（选 B：成对耦合）**。`pure_rootfs` 默认 `synth`；`E2B_REAL_ROOT` **未被显式
设置**时按"跟着合成根走"处理（`envd_service/config.py::resolve_real_root`）：**有合成根的箱**
（pure 形态）装上真根，**其余形态**（有 base image 的 image-rootfs 箱）保持今天的模拟根。
不选 A（把 `E2B_REAL_ROOT` 的全局默认一起翻成 `on`）的理由是影响面：A 会让**两套生产清单里没显式
写这个键的那些栈**（`deploy/stack/docker-compose.prod.yml`、`deploy/compose/*.yml`）在无人声明的
情况下从模拟根换到 pivot_root，而这次裁定的范围只是"pure 的默认根"；B 之下 image 形态**逐字节
不变**，生产车队（两套清单都设 `E2B_BASE_IMAGE`）不受影响（逐处行号见本节末的影响面表）。

**退回杆是一句话**：`E2B_PURE_ROOTFS=off`。它把 pure 形态放回 N15 的 identity 根，同时因为
"没有合成根可跟随"，耦合出来的真根默认也随之回到 `off` —— **旧形态是这一个键，不是两个**。
`E2B_REAL_ROOT=1/0` 仍然显式优先（`=0` + `synth` 就是配置守卫拒绝的那对）。

**代价（三条，都写进 `config.py` 的字段 docstring）**：① 每沙箱一份骨架目录
`<base>/_pure_rootfs/<id>`（拆箱时收掉）；② pure 形态从此**依赖真根**，也就是依赖节点上的
`deploy/seccomp/sandlock-worker.json`——**实测**：同一个镜像、只差 seccomp 档，探针在 Docker 默认档
下答 `unshare(CLONE_NEWUSER): Operation not permitted`、在仓库档下答 `ok`
（`envd_service/executors/sandlock.py::_real_root_capability`；没有档的宿主上，建箱会**按名字**
拒绝，不会静默降级）；③ 空值仍读作"未设"（本仓库 env 助手的
惯例），所以"选 identity"要写 `off`，写 `E2B_PURE_ROOTFS=` 是取默认。

**影响面（逐处实测/逐处给行号）**：

| 面 | 例子（文件:行） | 翻默认后 |
|---|---|---|
| image-rootfs 车队（设 `E2B_BASE_IMAGE`） | `deploy/k8s/worker.yaml:507`（`k8s` 另设 `E2B_REAL_ROOT=1`，`:457`）、`deploy/stack/docker-compose.prod.yml:145`、`deploy/compose/docker-compose.prod.yml:114` | **不变**（B 的耦合按形态解析；`E2B_REAL_ROOT` 未设 ⇒ image 箱仍是模拟根） |
| 池（`E2B_AS_WORKER_ENV`）/ autoscale 栈 | `deploy/compose/docker-compose.autoscale.yml:166`、`autoscaler/backends/local.py:48`（`_env` 字典；`seccomp=unconfined` 在 `:141`） | 池默认带 base image ⇒ **不变**；**手搭的不带 base image 的池**会走 `synth`，宿主不允许非特权 userns 时建箱按名字拒绝，退回杆 = `E2B_AS_WORKER_ENV` 里加 `"E2B_PURE_ROOTFS": "off"` |
| 不带 base image 的 compose 栈 | `deploy/compose/docker-compose.yml:82`（`envd`，无 `E2B_BASE_IMAGE`、也没换 seccomp 档） | **会走 `synth` 并在 Docker 默认档下起不来** ⇒ 已显式加 `E2B_PURE_ROOTFS: ${E2B_PURE_ROOTFS:-off}`（`:103`；要跑新默认就装上仓库档并删掉这个键） |
| 形态 lane | `deploy/scripts/acceptance/gateA-full.sh`、`gateB-full.sh`、`gateB-pure-rootfs.sh`（state 0）、`x86-security-one.sh`、`x86-run-py.sh`、`deploy/scripts/arm-lane/x86-security.sh` | 它们显式写 `E2B_REAL_ROOT=0`，翻默认后**必须同时点名 `E2B_PURE_ROOTFS=off`**（否则被守卫拒绝）⇒ 已逐处加上 |
| N27 探针 lane | `deploy/scripts/acceptance/n27-t7-lane.sh:35`（`E2B_BASE_IMAGE=` 空 ⇒ pure） | 原来靠"默认即 identity"；现在**转发** `E2B_PURE_ROOTFS`（`:36`），由调用者与探针自己的 `--shape` 对齐 |

**验收（2026-09-26，Task 13 权威落点）**：四档全量 lane 的逐档数字、基线与差逐字如下（全部在
revision `1374e87`、同一份树指纹上跑，镜像 `e2b-sandlock-test:task12cur`）：

| 档 | 命令（`E2B_TEST_IMAGE=e2b-sandlock-test:task12cur` 前缀省略） | 结果（末行逐字） | 基线 | 与基线的差 / 判定 |
|---|---|---|---|---|
| **gate A**（镜像形态，全量） | `sh deploy/scripts/acceptance/gateA-full.sh tmp/k0s/n16-gateA.log` | `2022 passed, 10 skipped, 3 xfailed` | Task 11 的 `2003 passed / 0 failed`（revision `47998de`） | **+19 passed、0 failed**；+19 全部是 `47998de` 之后落库的新用例（collect：`47998de` = 2016、现在 = 2035）⇒ **相等或更好** |
| **gate B off**（= 新读法下的 `=0` identity） | `sh deploy/scripts/acceptance/gateB-full.sh tmp/k0s/n16-gateB-off.log` | `2015 passed, 17 skipped, 3 xfailed` | 简报的 `1765 passed / 13 skipped / 3 xfailed`（N15 当天，过期）；同形的 Task 11 identity security `43/3/3` | `0 failed`；与基线数字不同只因基线 revision 少 19 条用例 + 7 条形态 skip ⇒ **相等或更好** |
| **`=1` 合成根 + 真根** | `sh deploy/scripts/acceptance/gateB-pure-rootfs.sh 1 tmp/k0s/n16-gateB-synth-realroot.log tests --perf --ignore=tests/contract/test_volume_quota.py --ignore=tests/contract/test_xfs_project_quota.py` | `2019 passed, 16 skipped` | 简报"两态都 0 failed、passed ≥ 1765"；同态的 security `46 passed / 3 skipped / 0 failed`（Task 11） | **0 failed / 0 error**，比 identity 多 4：3 条 N35 `xfail` 转 pass + 1 条别名用例不再 skip ⇒ **相等或更好** |
| **`=0` 那一态**（`gateB-pure-rootfs.sh 0`） | `sh deploy/scripts/acceptance/gateB-pure-rootfs.sh 0 tmp/k0s/n16-gateB-identity.log tests --perf --ignore=…同上` | `2015 passed, 17 skipped, 3 xfailed` | —（新读法下它就是 gate B off） | 与 gate B off **逐字相同**（同 2015/17/3）⇒ 两态 = 第 2 档 + 第 3 档，**自洽** |
| **第三组合（`synth` + `REAL_ROOT=0`）** | `docker run … -e E2B_PURE_ROOTFS=synth -e E2B_REAL_ROOT=0 … python3 -c 'from envd_service.app import create_app; create_app()'` | `RuntimeError: E2B_PURE_ROOTFS=synth without E2B_REAL_ROOT=1: …`，**exit 1** | 追加裁定：该组合结构性不成立，守卫当场拒绝 | **没有"这一档"的结果，只有拒绝证据** |
| **phase 2**（非 root worker） | `sh deploy/scripts/acceptance/phase2.sh tmp/k0s/n16-phase2.log` | `57 passed, 1 skipped` | 简报 `57 passed, 1 skipped` | **逐字相等** |

**四档全 `0 failed`**；简报与 §6 里 N15 当天那组 `1772/1765` 已被这组取代。security 两态（Task 11，
`tests/security` 自 `47998de` 起零改动）identity `43 passed / 3 skipped / 3 xfailed`、合成根
`46 passed / 3 skipped / 0 failed`。数字出处是本地 lane 日志 `tmp/k0s/n16-gateA.log`、
`tmp/k0s/n16-gateB-off.log`、`tmp/k0s/n16-gateB-identity.log`、`tmp/k0s/n16-gateB-synth-realroot.log`、
`tmp/k0s/n16-phase2.log` 与被拒证据 `tmp/k0s/task13/n16-guard-synth-realroot0.log`（`tmp/` 是 gitignored）；
**日志若被清，以本文档的数字为准**。
两条注记：① `E2B_PAUSE_CHECKPOINT` **默认关**，所以合成根下 pause/resume 的第一次探针是**空洞的**
（pause 不写图、`resume` 面对活会话走 thaw 分支 ⇒ restore verb 一次都不调）；真跑恢复链的是"用
worker 自己的两个入口 + 把 restore stub 指到树外"那一版（`tmp/k0s/task12/`），两态两轮都
`restored: true`、恢复后仍能 exec，且合成根下只能靠**投递 fd** 把树外的 stub 交给引擎
（`docs/chroot-workspace-exec.md` §11.6.1）。② 合成根下 `stat` 家族对"父链缺一环"的路径答
**ENOENT**（identity 答 EACCES）—— 比 EACCES **更不泄漏存在性**，不是漏拦；契约在
`tests/security/test_pure_root_errno_contract.py`。

**其他"pure 根 = `/`"的命中怎么处置**（Task 14 Step 1 的全仓扫描，逐条都在这里落定）：产品
docstring（`envd_service/executors/sandlock.py` 的 `_chroot_root`/`_view_cwd`）与
`envd_service/config.py` 的字段注释都已经按两形态写；`tests/security/conftest.py`、
`tests/security/escape/test_path_surface_inotify.py`、`tests/contract/test_own_identity_executor.py`
里的注释按产品默认写（`E2B_PURE_ROOTFS` 未设 = `synth`；`tests/security/conftest.py` 已改成照
`Settings` 解析，不再自带一份形状规则）；`docs/HANDOFF.md` 顶部那段与
`docs/superpowers/plans/2026-09-10-*` 是**带日期的留档**，按"不改写历史记录"的纪律不动。
**唯一需要条件标注的现况句就是上面 §6 的 `pwd` 那句**，已更正。
