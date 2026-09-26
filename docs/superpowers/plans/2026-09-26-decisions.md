# 2026-09-26 用户裁定（四条工作流的开工决策）

用户对八个计划里"需人拍板"点的一次性裁定。**实现者开工前先读本文件**；
引用计划时以本文件的裁定为准（计划正文写的是建议，这里是决定）。

| # | 计划 | 要定的 | **裁定** |
|---|---|---|---|
| 1 | `2026-09-26-n27-state-base.md` | 独立挂载（要拷 GiB 级 checkpoint 镜像、逐文件校验）还是**同挂载 + 树根下沉** | **同挂载 + 树根下沉**：`<export>/workspaces/<id>` + `<export>/state/`。理由：`rename(2)` 的边界本来就是**挂载点**，任何第二个挂载都必然 EXDEV；下沉一级让迁移变成秒级 rename、可原路回退。**独立挂载那版（含 `move_entry` 的 EXDEV 兜底）不写** |
| 2 | 同上 | 迁移窗口：worker 缩到 0（现有沙箱全部消失、数据保留） | **接受**。排到维护窗口执行 |
| 3 | `2026-09-26-n30-disk-accounting.md` | 确认存量口径；条目（inode）维度是否升格为对外可配的配额项 | 见下节《N30 口径确认》 |
| 4 | `2026-09-26-o2-ingress-tls.md` | 是否翻 N29④ 的"不改入口"；443 vs 3000 | **暂时不做** —— O2 整条**本轮搁置**，计划保留不执行。触发：入口侧真的出现 504/性能问题，或拿到入口主机控制台 |
| 5 | `2026-09-26-o3-credential-rotation.md` | redis 口令：ACL 双用户热轮换，还是接受 10–30 s 中断 | **接受中断**（不做 ACL 双用户）。计划里"ACL 双用户"那版降级为备选，主路径按"接受中断"写；轮换窗口要把这 10–30 s 写进操作步骤与影响面 |
| 6 | `2026-09-26-checkpoint-restore-productization.md` | 恢复后要不要支持 `exec`；生产形态是否必须支持 | **要支持**，生产形态**必须**支持。**且这不是待做项——已经支持了**，见下节《D9 已关闭》 |
| 7 | `2026-09-26-pure-shape-synthetic-rootfs.md` | 骨架落点：共享骨架 vs 每沙箱一份 | **每沙箱一份**（`<base>/_pure_rootfs/<id>`，含拆箱清理）。理由：共享骨架会让卷的虚拟路径 stub **跨沙箱累积**，等于给别的租户一个存在性 oracle——那正是 N15 关掉的那类 |
| 8 | `2026-09-26-netns-shape-unification.md` | 回滚语义：三处成对设 `false` 回共享 netns 时，wildcard `allowOut` **需要把低端口窗口加回来** | **接受**这个回滚代价，并在文档里写实（不承诺"不编辑文件即可干净回滚"） |

## N30 口径确认（第 3 条问的就是这个）

**要你确认的那句话是**：`diskMB` 采用**存量（位置边界）口径** ——
"这棵沙箱树**当前**占用的字节（文件按 `st_size`、目录按分配块 `st_blocks×512`）"，
**删除即归还**；上限由三处一起保证：中介在 `open` 时按剩余额度发放单文件上限、
`unlink`/删除**当场归还**、`O_CREAT`/`mkdir`/`symlink`/`link` 超预算返回 `ENOSPC`，
另有 per-exec `RLIMIT_FSIZE` 作内核级兜底。

**同时否决**"峰值口径"（写多少算多少、**删了不退**）：它唯一的卖点是"内核级 ENOSPC、
不经过中介"，而代价不可逆 —— 本集群 NFS **不支持打洞**（`fallocate -p` unsupported，
`fstrim` 报 973 MiB 而 `du` 不降），于是镜像占用 = **高水位**，"写满一次"会把沙箱
**永久**降级成只读，唯一恢复路径是销毁重建；另外还要付 privileged/`SYS_ADMIN`、
`/dev/loop` 或 NBD、宿主 `modprobe nbd`、每节点守护进程、以及"工作区不再是目录"
（GC/快照/模板/迁移/CP 读树全部跟着改）。

**因此 qcow2-over-NBD（L3）不作为计划项。** 触发条件（满足任一即回到这条）：
① 出现真实使用者要求**写路径上**的字节级 ENOSPC；② 存储换成支持打洞或支持目录配额的文件系统。

**条目（inode）维度**：按**已有旋钮** `E2B_DISK_MAX_ENTRIES`（代码默认 0，k8s 取 500000）
写进口径即可，**本期不新增机制、不升格为对外可配项**。
（计划原文建议新增 `E2B_MAX_ENTRIES_PER_SANDBOX`，作废——那个旋钮已经存在。）

## D9 已关闭（第 6 条的依据）

用户问"fork 应该已经支持了吧"——**对，已经支持了**，D9 在 2026-09-25 就关闭了。
分工在于引擎有**两条**恢复路径：

- **OCI / `--restore-from`**（`crates/sandlock-oci/src/supervisor.rs:1386`、
  `crates/sandlock-supervise/src/serve.rs:1481`）：按名拒绝 `exec`，
  因为 exec 靠 `sandlock-init` 转发，而"从镜像起一个 generation"没有 init。
- **恢复进会话**（`crates/sandlock-core/src/instance.rs:1205-1211`）：
  *"Restoring **into** a session keeps `exec`, `wait_child`, `kill_child` and the child
  table working, which is what a long-lived sandbox needs after a resume."*

**E2B 走的是后者**：`envd_service/route_b.py::restore_checkpoint` 的 docstring 写明
"resume the image in `dir` **into its own session**… a **session** is what serves `exec`"。
验收两处都过：fork `test_a_child_restored_into_a_session_keeps_the_session_executable`
（进程在跑 / **恢复后仍能 exec** / `children_live` 算上它，`core_lib` 911/0），
集群 §6(g)（重启 worker pod → resume → **进程状态还在、还能 exec**）。

⇒ 所以 checkpoint/restore 的产品化**没有"恢复后不能 exec"这道拦路虎**。
`docs/checkpoint-restore-e2b-half.md` §0/§(d) 的原文按"不能 exec"写、误导过一轮排查，
已就地更正。

> **⚠️ 同日第二次更正（写本节时我又犯了同一个错，一并记下）**：本节初稿还写了
> "restore stub 与 chroot/真根不兼容 ⇒ 这道才是生产能不能用的第一道题"。**那句也是过期的。**
> `43cc62a` 的"立即拒绝"只在那时成立；`a6f6b04`（*feat(restore): deliver the stub by
> descriptor, so a chroot root can restore*）已把它取代：stub 由**描述符**投递
> （`execveat(AT_EMPTY_PATH)`），规则集给这一个宿主文件 Landlock 真正判定的那个权利，
> 于是"两种 chroot 形态都能恢复"，由 `test_restore_resumes_inside_a_chroot_root` 钉住
> （模拟根与真根都跑）。依据是 fork `crates/sandlock-core/src/sandbox.rs:1419-1463` ——
> 那段注释本身就是被更正过的历史（"and for a while the call was refused up front instead
> (43cc62a), because that was true. **It is not true any more**"）。
>
> **两次错的是同一个东西**：我拿 E2B 侧文档（`open-issues.md` / `checkpoint-restore-e2b-half.md`）
> 的叙述当引擎事实，而那份叙述滞后于 fork。**纪律**：凡"引擎能不能做某事"的判断，
> 一律读 **fork 代码 + 当前 tip 的用例**，不读 E2B 侧的转述。
>
> 结论：生产形态**今天没有**已知的引擎侧拦路虎（会话恢复保留 exec、两种根形态都能恢复 stub）。
> 计划（重写后的 `2026-09-26-checkpoint-restore-productization.md`）的 Task 1 因此改成
> **覆盖缺口**而不是"解一道禁令"：模拟根缺会话恢复用例、真根缺"恢复后仍能 exec"断言、
> "会话启动时没装上 stub 的 grant"是**静默失败**（这三条才是要补的）。

## 追加裁定（2026-09-26，同日）：N38 —— 本地池的 `E2B_EXECUTOR` 也切成 `auto`

**背景**：实现 N36-② 时实测发现，把 `E2B_ENABLE_NET_ISOLATION` + `E2B_FD_INJECT_CONNECT`
成对打开后，**池里跑的仍是共享 netns** —— 因为 `deploy/compose/docker-compose.autoscale.yml:116`
的 `E2B_EXECUTOR` 默认是 `local`，而 `envd_service/executors/local.py` 明写
"No Sandlock isolation"，`enable_net_isolation` 只在
`envd_service/executors/factory.py:210` 传给 **sandlock** 执行器。
也就是说：**按原裁定做完，② 的目标（池与车队对齐）并未达成**，而低端口窗口一删，
共享 netns + 通配规则就会 EACCES。

**用户裁定（选 A）**：**把池的 `E2B_EXECUTOR` 也改成 `auto`**，让池真吃 per-sandbox netns。

**随之要做的（已实测的连带项，按顺序解）**：
1. `E2B_ENABLE_NETWORK` 在池的 worker env 里也缺 —— 补上；
2. 补上后实测仍 `ECONNREFUSED`、且沙箱内 `127.0.1.1:53` **不监听** ⇒ 这一串要单独查（连接口 50005+
   的入站映射与通配 DNS 网关在池形态下的行为）；
3. `E2B_ROUTE_B_TMP_ROOT` 在 `E2B_AS_WORKER_ENV` 里缺失 ⇒ 当前镜像在池里 **exit 1**（N39），
   要先于形态验证修掉；
4. 池默认 worker 镜像 tag 是 `0.1.0`（08-30 那版，零 netns 代码）⇒ 形态验证必须显式指镜像。

**登记**：`docs/open-issues.md` 的 N38 / N39。

## 追加裁定（2026-09-26）：N42 网络全开 + ④ 的 seccomp 按出厂要求

**背景一（N42，实测）**：线上集群的沙箱**没有出网** —— 一个 `allowInternetAccess=True` 的沙箱
`1.1.1.1:443` → `PermissionError [Errno 13]`、`pypi.org:443` → `gaierror [Errno -3]`。
根因：`E2B_ENABLE_NETWORK` 只出现在两套 compose 里，**`deploy/k8s/worker.yaml` 缺**
（`envd_service/config.py:131` 默认 `false`）⇒ `sandlock.py:2307` 的
`self._allow_internet_access and self._enable_network` 恒假 ⇒ **SDK 的 `allowInternetAccess` 在线上静默无效**。

**用户裁定（2026-09-26）：「网络先全开吧」。**
⇒ 缺 `E2B_ENABLE_NETWORK` 的清单都补 `"true"`：`deploy/k8s/worker.yaml`、
`deploy/compose/docker-compose.multinode.yml`，以及 `deploy/k8s/control-plane.yaml`（先查用途再定，
依据写报告）。已有的那几处（stack / prod example / autoscale / `local.py`）**不动**。
语义：沙箱**可按请求**出网；**不带 `network` 的请求仍只到固定域名集**（pypi/npm/github）。

**背景二**：④ `docker-compose.multinode.yml` 三处用 `seccomp=unconfined`，而**出厂要求**
（`deploy/stack/docker-compose.prod.yml:328`）是 `seccomp=${E2B_SECCOMP_PROFILE:-../seccomp/sandlock-worker.json}`
—— 即挂真档；`seccomp=unconfined` 是**旧的**做法（stack `:323-326` 的注释写明它被换掉的原因见
`deploy/seccomp/README.md`）。按树里镜像，④ 的 worker 因为 `E2B_REQUIRE_SECCOMP_FILTER` 会**一个都起不来**。

**用户裁定（2026-09-26）：「另一条按出厂要求修」。**
⇒ ④ 的三处改成与 stack **同形**的真档，并把出厂要求的 `E2B_REQUIRE_SECCOMP_FILTER` 一并对齐。

**部署侧（控制器负责，不在清单任务内）**：k8s 改完要 `apply.sh` 上集群，并用"`allowInternetAccess=True`
的沙箱能否连出去"复测 —— 那是 N42 的验收判据。

## 追加裁定（2026-09-26，O3 第二轮）

- **api/internal key：改做「双窗轮换」**（新 key 与旧 key 并存 → 滚动 → finalize 移除旧的），
  即计划 **Task 3** 的形态。**redis 口令仍按原裁定"接受 10–30 s 中断"**（Task 4 不变）——
  两条凭据的取舍不同，别混。
- **清理既有明文**：`<workspace_base>/_secrets/**` 上**已经落盘**的明文要清理。
  ⚠️ 计划里**没有这条**：Task 1 的"开 `E2B_SECRET_MASTER_KEY`"只影响**之后的写入**，
  既有明文不会被自动加密或删除。**新开任务**：写迁移/清理工具（把既有 secret 经 registry 重写为
  加密态 + 清理残留明文 + 校验），单测覆盖，然后由**部署侧**在集群上执行。
