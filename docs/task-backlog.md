# 任务总清单（路线图）

汇总 2026-08-31 ~ 09-02 分析产生的待办。**状态最后更新：2026-09-12（线上 Track C
上线：非 root worker 形态 + uid 段拆分已在目标机生效，四条线上测试全绿；同轮修复并
上线两条产品缺陷 —— 卷/沙箱记录共用 `e2b:record:` 命名空间（见 #26）与非 root 卷根
chmod 顺序（见 #27）。此前 2026-09-11 Track F / F1 收口、2026-09-08 F15 收口、
FUP-01/07/09/10/15/17 台账关闭，见 fork `docs/fork-plan-followups.md`）**
（细粒度执行记录见 `.superpowers/sdd/progress.md`，两阶段总路线见
`docs/superpowers/plans/2026-09-01-sandlock-e2b-completion-roadmap.md`）。

约束提醒：本期"不推送远程" = 不做目标机远程部署（`upgrade.sh` / 远程复测暂缓），
ACR 镜像推送照常，git 远程推送暂缓。

构建/测试/部署前先看 [docs/build-test-deploy-pitfalls.md](build-test-deploy-pitfalls.md)
（已踩过的坑 + 对应做法，防复发）。

## 归属方说明

| 归属 | 代码库 | 改动/验证方式 |
|---|---|---|
| **sandlock** | `third_party/sandlock` 子模块（Rust fork） | 改 fork → 构建 wheel（`build-sandlock-wheels.sh`）→ 子模块提交 |
| **E2B 服务端** | 本仓库 `control_plane/` `envd_service/` `gateway_common/` `deploy/` | 直接改 + 单测 + 部署 |
| **运维** | 部署环境（目标机/代理） | 配置变更 + 维护窗口 |

---

## sandlock fork 侧任务（阶段一：S0–S3 全部完成）

| # | 任务 | 状态 |
|---|---|---|
| S0.1 | 基线回归（lib / integration / python 三套） | ✅ 完成（lib 771 / integration 432 / python 430） |
| S0.2 | wheel 重建 + `notify_rate_limit` SDK 冒烟 | ✅ 完成 |
| S1.1 | PID namespace（`CLONE_NEWPID`、procfs 视图、跨沙箱 PID 探测） | ✅ 完成（lib 773 / integration 441；遗留：CLI `--pid-ns` 未接线到运行时 builder，已随 S3 收尾处理） |
| S1.2 | 独立 uid 的 userns 单 entry 约束验证 | ✅ 完成（结论：非 root supervisor 无法映射任意 host uid，每沙箱独立 uid 需 root/CAP_SETUID → E3.2/E5.1 输入）。**2026-09-12 更正**：无 helper/subuid 时如上；给定发行版 `uidmap` + 覆盖池的委托段时**不需要 root、也不需要 `CAP_SYS_ADMIN`**，只要 BND ⊇ SETUID/SETGID（`task-usernsprobe-report.md`、计划「Track U」） |
| S1.3 | 无特权运行固化（uid 65534 全绿） | ✅ 完成（基线写入 HANDOFF） |
| S2.1 | connect handler 宿主建连 + ADDFD 注入 | ✅ 完成（已知限制：getsockname/getpeername 显示宿主地址、非阻塞 connect 无 EINPROGRESS） |
| S2.2 | spawn 路径 `CLONE_NEWNET` + `lo up` | ✅ 完成（netns 沙箱 netlink 为合成 loopback-only 视图） |
| S2.3 | DNS 网关进沙箱 netns + 通配域名回归 | ✅ 完成 |
| S2.4 | UDP（connected 注入 / datagram on-behalf） | ✅ 完成（datagram 单向代发） |
| S2.5 | 入站端口映射（MCP 场景） | ✅ 完成（host 映射端口强制 50005+；poll/epoll 可读性合成随 E7.1 完成） |
| S2.6 | 全特性矩阵回归 + wheel 重建 | ✅ 完成（lib 788 / integration 465 / python 430） |
| S3.1 | cp314 双架构 wheel + 冒烟 | ✅ 完成（cp310-313 扩列本期不做） |
| S3.2 | 私有源/镜像安装切换 | ✅ 完成（Dockerfile 按 ABI+ARCH 从 `wheels/fork` 安装） |
| S3.3 | 上游 PR 分支整理 | 🟡 分支已整理，**推送未做**：fork 分支领先 origin 166 个提交（含本轮 pid_ns 两个提交与 `fe492be`/`752b5db`），远程约束已解禁 → 见 N11 |

| E10 | 每沙箱一实例（fork §8，M0–M4）：三层拆分 + E2B 接线。含 Q10 风险：`max_processes` 从『每命令 64』变『整箱 64』，落地时必须同步上调默认值并写变更说明；另含 Q6 网络策略语义、Q7 控制目录身份、Q8 泄漏回收。**安全前置（fork §7 M0′）未清零前不得在 envd 开 `exec`**；M4 第一步只做「网关+命令」半合并 | ✅ 完成（fork 侧 M0–M4/F0–F10 在子模块 b955ae9；E2B 接线 5d38537（Task 0.5 supervisor 档）→ 4f34e55/e0f5507/6de41db/05f349f/cb36b7a/f64d7ab/19dc1f5/f337724/4149a1f/4d617b9/3bf5d0e/883d38d/f67a6b9 → Task 11 收口；全量门禁见 HANDOFF。剩余 follow-ups 见下方「M4 收口后的 open follow-ups」） | `5d38537`…`f67a6b9`（+Task 11） |
| SL-1 | 路径中介（USER_NOTIF）以 supervisor 身份执行 `openat/unlinkat/fchmodat/...`：沙箱文件属主变 root、`chmod` 失效、共享目录 per-uid 保护不成立 | ✅ **已关闭（构造消除 + fail-closed）**：route B（supervise 进程 euid == 沙箱 host uid）使原修法不再需要，特权进程内中介 + 路径中介建箱前拒绝且**没有降级档**（B3 硬删 `mediation_run_as`）；E2B 侧见 T5 收口（`596f843`…）。**残余**：fork `docs/e2b-integration.md` §3.1 的"fail-closed 现状"段待重写（#25 ②）+ 上游 issue 未开（`gh` 不可用，见 N11） | `b62e201` `dd5a7e8` `7f81314` `75bbe0b`
镜像内 wheel 与 fork 提交一致后才能推 ACR。

## E2B 服务端侧任务（阶段二）

| # | 任务 | 状态 | 关键提交 |
|---|---|---|---|
| E1.1 | 构建 + 推送 ACR 双架构镜像 | ✅ 完成 | `0.1.0-9-g9ed0f00-20260902-013319` |
| E1.2 | 部署验证 | ✅ 完成（2026-09-16：远程部署解禁后真做了目标机验证 —— 两轮 `upgrade.sh`（净 netns 全量 + pid_ns 灰度/全量）每次自带多节点冒烟与部署级冒烟全过，另有按节点的契约探针；日志 `tmp/upgrade-*.log` / `tmp/pidns-*.log`） | `917395b` `3b8f8af` |
| E1.3 | 内网隔离复测 | ✅ 完成（tests/security 26/0/1skip；顺带修 rootfs 绝对符号链接） | `b8cb817` |
| E1.4 | 控制面 TLS | ✅ 完成 | `8b63da9` `9395c88` `01b6723` |
| E2.1–E2.6 | XFS project quota（检测/管理/串行锁/孤儿清理/volume 配额/quota-agent） | ✅ 完成 | `a819426`…`e985efc` |
| E3.1 | 租户隔离（归属 + per-tenant 配额/限流 + 迁移脚本） | ✅ 完成 | `cfd7e95`…`ebef5b6` |
| E3.2 | 每沙箱独立 host uid + volume 权限模型 | ✅ 完成 | `596f843`…`700ef34` |
| E3.3–E3.6 | volume token TTL/吊销、upload token 失效、构建限流、internal key 轮换 | ✅ 完成 | `a97f7fb`…`81ee806` |
| E4.1 | 命令输出缓存上限（capped buffer + 截断标记） | ✅ 完成 | `bc98fea` |
| E4.2 | 文件写入流式落盘 + 大小限制 + 413 预检 | ✅ 完成 | `73c3843` `e04f9db` |
| E5.1–E5.4 | 非 root worker、依赖锁定、大小限制、secret 加密持久化 | ✅ 完成 | `35fefdd`…`843b9e3` |
| E6.1–E6.4 | 失联沙箱对账、镜像 digest 固定、MCP 端口复用、NFS 实测 | ✅ 完成 | `b995ea4`…`41ccd0c` |
| E7.1–E7.3 | 网络隔离联动（MCP 路径、配置透传、Linux 集成） | ✅ 完成 | `2e2df65`…`bc597a8` |
| E9.1 | 空闲检测（`last_active_at` + worker 活动上报 + 阈值配置） | ✅ 完成 | `07b3555` |
| E9.2 | pause 释放配额 / resume 重新准入 | ✅ 完成 | `8448d64` |
| E9.3 | 驱逐选择器 + kill/pause + 通知（默认开启） | ✅ 完成 | `bcee688` |
| E9.4 | 创建排队 / 超时 / 队列上限 | ✅ 完成 | `59c64d9` |
| E8.1 | 部署后远程 smoke 回归 | ✅ 完成（2026-09-16：解禁后每轮部署都跑 —— `multinode_smoke.py`（命令/文件/健康经网关、stdin、kill 后预留归零）+ `deployment_smoke.py`（迁移保留文件、网络配置、卷挂载、模板构建→ACR→worker 拉取、箱内 MCP 经代理），见 `tmp/upgrade-pidns.log` / `tmp/upgrade-pidns-full.log`） | — |
| E8.2 | 本地测试基线确认 + HANDOFF/backlog 更新 | ✅ 完成（Linux 容器全量 28 failed / 804 passed / 17 skipped / 6 errors，219.63s；macOS unit+contract 11 failed / 689 passed / 23 skipped / 33 errors，36.91s；详见 HANDOFF「验证命令与基线」） | — |
| E8.3 | 测试环境失败清零（把 E8.2 的"环境类失败"逐条定根因） | ✅ 完成（Linux 容器全量 **0 failed / 0 error**，843 passed / 18 skipped；macOS 全量（含 sdk python/js + security）**0 failed**，804 passed / 53 skipped；顺带修掉 2 个产品缺陷（模板镜像切换未落盘、无 registry 构建产物无法解析） | `ac59152` `d8b7f41` `87874a0` `0a235b4` `22e5acc` |
| E8.4 | 公共镜像不直连 Docker Hub：`E2B_REGISTRY_MIRRORS` + 凭据按 host 作用域 + harness 存储改容器原生盘 + 清掉一条假 skip | ✅ 完成（默认形态 851 passed / 18 skipped；image-rootfs 形态 73 failed+28 errors → 853 passed / 16 skipped；两形态 0 failed。18 条 skip 的分组与跑法见 HANDOFF「容器全量剩下的 skip」） | `a6f74e4` `3d7cc79` `08a21dc` |
| E8.5 | 把"能跑却在跳"的用例真正跑起来：镜像自带 XFS prjquota/npm/netns/双形态 + `E2B_TEST_STRICT_SKIPS` 能力型 skip 直接判失败 | ✅ 完成（全开跑见下；顺带修掉 fs_denied 废掉 per-uid 隔离、lsattr 缺失导致孤儿只报不清） | 本次提交 |
| T4 | net_isolation + 镜像 rootfs(chroot) 形态下 MCP 入站端口映射起不来（纯 sandlock 形态 3/3 通过） | ✅ 已关闭（Task 10，FUP-E1）：根因 = envd 侧 base-image 组成（slim rootfs 无 mcp-gateway，ENOENT exit 2），非 fork；改用 MCP-capable 基镜像 `python-mcp:3.14`（deploy/docker/Dockerfile.mcp-base）后 chroot+netns MCP 契约两形态 3/3 绿；xfail 已摘 | `883d38d` `f67a6b9` |
| T5 | chroot 形态共享卷写入经 supervisor 归属（fs_denied 代打开路径），per-uid 卷保护无法还原 | ✅ 关闭（2026-09-10）：三步全落。① fork F16/F17（worker 侧 C ABI + Python `SuperviseChannel`，含 fd 交接）；② E2B envd 全面接线 route B（W1 槽位模型，chroot 形态默认档）；③ strict xfail 已摘 + **`mediation_run_as='supervisor'` 降级档已删**（那条组合现在由 fork 拒绝建箱，E2B 不再请求）。证据：契约 `test_uid_permissions`（两 uid 槽位 20000/20001 各自属主/自 chmod/跨 uid EPERM）、`test_route_b_slot_pool` 的 token 非暴露面证明、`test_template_isolation` 两条真槽位 chroot + 一条钉住拒绝。fork §3.1 的「fail-closed 现状」段仍待重写（#25 ②），与本项无关 | — |
| T1 | 真实 XFS/ext4 目标机上复测沙箱文件属主：① 沙箱能否 `chmod` 自己写的文件（本机 EPERM）；② 共享卷 1777+sticky 的跨 uid 保护是否真生效（本机 A 写的文件宿主属主是 uid 0，而沙箱 host_uid 是 20000） | ✅ **完成（2026-09-16，随 O1，目标机实测 `tmp/quota-t1-probe4.log`）**：① 箱内 `touch+chmod 600` 成功、宿主属主 = 池内槽位 uid（`600 11002`）——本机那条 EPERM 是 overlayfs 产物；② 卷目录宿主模式 **`1777`**（owner=池内 uid, group=worker gid），A（worker-2, 11002）写 0600 后，B（worker-1, 另一槽位 uid）读 `EACCES`、`rm` **`EPERM`**（sticky）、`chmod` **`EPERM`**，A 的文件事后仍是 `600 11002` | — |
| T6 | 内存/CPU/进程配额按实例而非按沙箱 ⇒ 超卖（默认 K=2 实测 1.76x），放大为节点超卖 | ✅ 已定方案：改为**每沙箱一个 sandlock 实例**（fork 文档 §8，取代 P10 共享资源组） | — |
| T2 | `third_party/sandlock`：`_HANDLED_FIELDS` 登记 `notify_rate_limit`，消掉假告警 | ✅ 完成（fork P3：`17ee48d` fix + `fad056a` doc，子模块 b955ae9 内） | `17ee48d` |
| T3 | 复现并修 `SnapshotRegistry.expand_to` 快照自嵌套（`snap_X/fs/snap_X/fs/...`） | ✅ 已落地（G2，2026-09-06）：`create_from_sandbox`/`expand_to` 复制前拒绝"目标落在源之内"（`ValueError`），并用 ignore 回调剪掉工作区里嵌入的快照存储根（只剪最外层，普通同名目录保留）；用例 `tests/unit/test_snapshot_registry.py` 3 条 + snapshot 契约回归全绿；设计见 `docs/superpowers/plans/2026-09-04-sandlock-remaining-goals.md` Task 0.2 | 见 git log（fix(snapshots) commit） |
| **F1** | **非 root worker 也能有 per-sandbox host uid + route-B 槽位**（file capabilities 路线；2 个专用 broker + 一份共享校验模块） | ✅ **已关闭**（2026-09-11，两阶段提交 `3c88872` + 本条提交）。① broker + 镜像 + 校验单测：`deploy/priv/{priv_common,slot_spawn,maint}.{c,h}`、`Dockerfile.envd`/`Dockerfile.test-runner` 多阶段编译 + **最终阶段** `setcap`（`COPY --from` 不保留 xattr；构建期要 `libcap2-bin`/`SETFCAP`、运行期不需要）、`envd_service/priv_helpers.py`（uid 池/根白名单/`realpath` 逃逸/program 钉死的 Python 半边 + 启动自检）、`tests/unit/test_priv_helpers.py`（RED→GREEN，26 条）；② envd 接线 + 端到端：`E2B_PRIV_HELPERS=auto\|off`、`RouteBConfig.spawner` → `e2b-slot-spawn`、workspace chown / 孤儿对账 / 删除 / `/metrics` 扫描 → `e2b-maint`、`tests/contract/test_nonroot_route_b.py`（已加进 `test-prod-shaped.sh` 的 unprivileged phase）。证据：`tmp/f1/f1-{red,green-unit,build-worker,stage1,e2e,lane}.log`（含镜像内 `getcap` 两条、uid 10001 exec 被拒、池内/越界/`..`/符号链接/非 supervise 程序/缺 cap 各条拒绝、槽位 `uid 10007 CapEff=0`、非 root 端到端五条断言原始输出）；文档落 `docs/production-deployment-requirements.md` §2.4/§2.4.1（含 BND 四条、构建期/运行期、威胁模型）+ `README.md` + `deploy/stack/.env.example`；`deploy/stack/docker-compose.prod.yml`/`deploy/k8s/worker.yaml` 补 BND 四条 + `E2B_PRIV_HELPERS`/`E2B_ROUTE_B_TMP_ROOT`。**偏离见报告**：broker 目录用 root:worker-gid `0710`（root 0700 会让 uid 65534 自己 exec 不了，实测），`libcap2-bin` 保留在 worker 运行镜像仅用于 `getcap` 验证。**Fix round 1 / 裁定 c1（2026-09-12）**：权限模型改为 `0770 owner=<sandbox uid> group=<worker effective gid>`（chmod 先于 chown；broker 只保留 chown + `rm/walk` 兜底），worker 数据面（`sbx.files.*`、`snapshot.create`、命令日志）全部恢复；新增硬护栏"uid/gid 池不得覆盖 worker 自身身份"（启动 fail closed）；Z2 两条冒烟在非 root 形态通过（`tmp/f1/f1-c1-z2-deploy-smoke.log` / `-multinode-smoke.log`）。详见 `.superpowers/sdd/task-F1-report.md` 的「Fix round 1」节 | — |
| **F9** | `deploy/scripts/smoke-prod-worker.sh` 形态过时：按 root + Docker 默认 cap 跑、且断言旧「共享 uid（宿主 1000 → ns 内 0）」语义，出厂形态 3 errors、部署身份下 1 failed | ✅ **已关闭**（2026-09-12，fix round 2）：脚本改为**部署形态**（`--user 65534:65534 --cap-drop ALL --cap-add SETUID,SETGID,CHOWN,DAC_OVERRIDE` + `seccomp=unconfined` + 可写的 `E2B_TEST_TMP_ROOT`），并配 `E2B_BASE_IMAGE`；`test_sandbox_child_runs_unprivileged` 按已拍板口径改写为 **route-B 身份**（宿主侧 uid = 池内 uid、ns 内 = uid 0，并断言写出的文件宿主属主就是池内 uid）。实测：非 root 形态 `2 passed, 1 skipped, EXIT=0`（skip 是 buildkit/docker-daemon 依赖，uid 65534 用不了挂进来的 socket），root runner 单测 `1 passed`。证据 `tmp/f1/f1-c2-smoke-final.log` / `f1-c2-identity{,-root}.log` | — |

## 运维侧任务

| # | 任务 | 状态 |
|---|---|---|
| O1 | 目标机启用 XFS `prjquota`（fstab + 在线 remount，维护窗口） | ✅ **完成（2026-09-16，用户侧开启并已复验）**：根盘 `/dev/nvme0n1p2` 以 `prjquota` 挂载、`/etc/fstab` 同项；部署侧配额链路随即转活 —— agent `/detect` = `{"fs_type":"xfs","prjquota":true,"backend":"quotactl"}`，`/report` 每个沙箱项目都有 `hard_blocks=1048576`（= 默认 1 GiB 盘上限），实测记账与强制见 N12 同段证据（`tmp/quota-t1-probe4.log`） |
| O2 | TLS 证书/代理层配置（代码侧 E1.4 已完成） | 未开始（需部署窗口） |
| O3 | 凭据管理（ACR/API key/redis/SSH 上密钥管理） | 未开始（E5.4 已提供 master key 轮换能力） |

## 待办登记（2026-09-16：netns 全量 / bind 注入 / seccomp 自检 / pid_ns 评估）

按归属方整理；每条都写明"下一步动作"与"验收判据"，避免下次重新推导。

| # | 归属 | 任务 | 状态 / 下一步 |
|---|---|---|---|
| N1 | fork | **pid_ns 自映射修复**：pid-ns 中间进程复刻 `confine_child` 的第三分支（`userns_self_map && !remap && user.is_some() && real_uid != 0` ⇒ `write_id_maps(real_uid, real_gid, 0, 0)`）。现在中间进程只有"特权 remap"和"real_uid→real_uid"两种 | ✅ **完成（2026-09-16，fork `5b16855`）**：中间进程按同一套三选一挑映射；新增 fork 回归 `test_pid_ns::pid_ns_self_map_restores_guest_root`（修前 RED `id -u`=65534，修后 GREEN）；wheel 同 tip 重建。验收测试 `test_route_b_restores_guest_root_with_and_without_pid_ns` 在 `E2B_PID_NS=1` 下 **转绿**（`id -u` 21000 → 0，属主仍为槽位 uid），并用 `kill()` 探针确认真的开了 pid ns。详见 §2.4.10.1 |
| N2 | 测量 | **pid_ns 的 syscall 代价**：pid_ns 会拦 `newfstatat/statx/faccessat/faccessat2/readlinkat`（+旧 ABI 四条），全是热路径，且 seccomp 无法按路径过滤 ⇒ 每次调用都进 supervisor | ✅ **完成（2026-09-16）**：部署（route-B/chroot 中介）形态里增量测不到（≤2 µs/次，与对照组抖动同量级）——这几个 syscall 本来就已经被 chroot 中介拦着；裸形态（无中介）才见 +80~90 µs/次。netns 的 390 ms/请求不适用。详见 §2.4.10.2 与 `tmp/pidns-cost-*.log` |
| N3 | E2B/运维 | **pid_ns 灰度与默认值**：N1+N2 通过后才能谈 | ✅ **完成（2026-09-16）**：worker-2 灰度（§2.4.10.3，canary 契约 C1–C6 全过）→ 共享锚点改 `true` 全量，两节点复核 `id -u`=0 / 池内 uid 各异 / `kill(1,0)=ok` / 外来 pid `ESRCH` / 线程与 files API 正常，`/mcp` 持平 ~8 ms、`RestartCount=0`（§2.4.10.4）。**代码默认值仍是 `false`**（部署清单决定形态）；单节点回滚用 `E2B_PID_NS_WORKER2=false` |
| N10 | E2B/k8s | ~~k8s 是否开 pid_ns~~ | ✅ **已切（2026-09-17）**：`E2B_PID_NS=true` 进清单，同 N5 一并落地。节点仍需允许非特权 userns（该依赖本来就存在：per-sandbox uid 已要求 userns），失败模式是**建箱 fail closed**。**待真实集群按 §6 复验** |
| N4 | 运维/k8s | **目标集群安装 seccomp profile**：`deploy/k8s/seccomp-installer.yaml`（ConfigMap + DaemonSet）已就位但**尚未在真实集群验证** | 未开始，**步骤已写进 `docs/k8s-deployment.md` §2**（顺序：`kubectl apply -f deploy/k8s/seccomp-installer.yaml` → 等 DaemonSet 每节点 Ready → 再滚 worker；缺文件的节点 fail closed）。**已在本集群验证（2026-09-22）**：`deploy/k8s-k0s/apply.sh` 之后 `ds/seccomp-installer` 为 **2/2**，worker 容器带 `seccompProfile: Localhost/sandlock-worker.json`，进 pod 读 `/proc/self/status` 是 **`Seccomp: 2`（filter mode，1 个 filter）** —— 即线上确实跑在过滤器下，不是"文件放了但没接上" |
| N5 | E2B/k8s | ~~k8s 是否切 per-sandbox netns~~ | ✅ **已切（2026-09-17）**：`worker.yaml` 加 `E2B_ENABLE_NET_ISOLATION` + `E2B_FD_INJECT_CONNECT`（成对），删除 pod 级 `ip_unprivileged_port_start=0`（顺带关掉「pod 内任何进程可绑低端口」这个与沙箱无关的口子）；`tests/unit/test_worker_manifest_permissions.py` 已改为钉住新形态。**待真实集群按 §6 复验**（本地无集群） |
| N6 | E2B | **MCP 网关未就绪时的状态码**：`/mcp` 代理对 `httpx.ConnectError` 未捕获，客户端看到 500（应为 502/503） | ✅ **完成并已上线（2026-09-16，`0.1.0-316-g10bedf6-20260916-175558`）**：`httpx.ConnectError` ⇒ **503 + `Retry-After: 1`**（"还没 listen"是可重试状态）；D1 那条"网关已死"的 503 保持**无** `Retry-After`（重试是假话），两种 503 被测试分开钉住。`HttpAuthError` 新增可选 `headers`。RED（修前）`assert 500 == 503`（`tmp/n6-red.log`）→ GREEN 61 passed。**目标机实测**：create 后立刻打 `/mcp` 的竞态探针 `status counts: {503: 26, 200: 1}`、`retry-after: {'1': 26}`（`tmp/n6-live-race.log`），worker-2 日志 **30×503 / 0×500 / 0 traceback**（升级前同一探针是 500 + traceback） |
| N7 | 测试 | **`test_nonroot_route_b.py::test_nonroot_worker_runs_route_b_with_pooled_uids` 冷 lane 抖动**：没带 `X-Sandbox-Id`，冷缓存下直接 428 | ✅ **完成（2026-09-16）**：两次 create 各带独立 `X-Sandbox-Id`（它就是沙箱 id，幂等短路）。为了真验冷形态，lane 新增 `E2B_IMAGE_CACHE_DIR` 透传（否则缓存落在仓库 `tmp/sandboxes/_images` 永远是暖的）：冷跑 GREEN（`tmp/n7-cold-green.log`，8.18s），去掉 header 的冷跑 RED = `assert (428, 428) == (201, 201)`（`tmp/n7-cold-red.log`），两相位常规跑也绿（`tmp/n7-green.log`） |
| N8 | 运维 | **netns 观察清单剩余项**（§2.4.7）：按节点分组的超时率与 MCP 端口带水位 | ✅ **完成并已上线（2026-09-16，同 N6 版本）**：端口带水位变成可读的**一条全局值** —— `McpPortPool.stats()`（`capacity/in_use/highest/free`）→ worker 心跳带 `mcpPortsInUse`/`mcpPortsCapacity` → 节点视图 `GET /nodes` 可见（读法与判据写进 §2.9）。单测覆盖池子算术、心跳 payload（含 provider 崩掉不牵连）、节点记录与端点往返。**目标机实测**：`before {worker-1:(0,4535), worker-2:(0,4535)}` → 建 3 个 MCP 沙箱 `{worker-1:(1,4535), worker-2:(2,4535)}` → kill 后 `{0,0}`（`tmp/n8-verify.log`）。超时率那一半本就是客户端侧观测，形态统一后不再需要分节点对比 |
| N9 | 仓库卫生 | 未跟踪产物：`.graphifyignore`、`graphify-out/`、`target` | **已决（2026-09-22 复核）**：`.graphifyignore` **入库**（已 tracked），`graphify-out/` 与 `target`（指向 `/src/target-linux` 的容器产物符号链接）**走 `.gitignore`** —— 三者现在都不是"未跟踪且未忽略"，工作区干净 |
| N11 | 仓库/上游 | **fork 分支推送 + SL-1 上游 issue**：分支领先 `origin` 166 个提交（含本轮 `5b16855`/`752b5db`）；SL-1 的 issue 一直没开（`gh` 不可用 + token 只读） | 待你决定：① `git -C third_party/sandlock push origin <branch>`（需网络+权限）；② 是否要开上游 issue（要一个可写的 token/凭据） |
| ~~N12~~ | E2B/运维 | ~~**沙箱删除后 project 行不立即消失**：`project -C` 只清目录态、限额留着，行（0 用量 + 非零 `hard_blocks`）要等 reconcile 把限额复位才被 XFS 丢掉~~ | ✅ **2026-09-18 已修（`docs/k8s-deployment.md` §17）**。根因：删除路径只做 `project -C` + 删目录，没人复位**限额**，而 XFS 只在用量与限额同时为 0 时才丢记录（实测一次潮留下 40 行，reconcile 清 36）。**修法**：新增 `clear_project_limits`（限额复位），在**树确实删掉之后**调用（先复位会给活沙箱留下无限的磁盘用量）；agent 形态补 `POST /project_limits` op；卷切片同理。顺带修两处：① `_reclaim_quota_rows` 原来把「本趟没报 cleaned」当「没收回」⇒ 每次删除多一条假警告，现在改为**问配额表**（延迟记账那类行才是这趟仍要服务的场景）；② `xfs_quotactl.available()` 在无 `libc.so.6` 的主机会抛 OSError，而后端选择每次都走它 —— 一条带旧 projid 的记录就能让普通删除炸掉，现在 `_use_quotactl` 把这种情况归到 subprocess 回退。**验证**：本地特权 XFS lane（真 XFS+prjquota）里两条 N12 断言通过；该 lane 改动前后同为 5 failed（干净 HEAD worktree 对比确认），那 5 个的成因另登记为 N23 |
| ~~N13~~ | 运维/k8s | ~~**k8s 多副本形态整体未验证**。「共享一份 RWX 时 uid 池重叠」~~ ← **2026-09-17 更正**：两个 pod 共用同一 `E2B_WORKSPACE_BASE` 时它们**共用同一个分配器** —— `uid_pool.acquire` 先 flock `<base>/.uid_pool.lock`，再按**全部** `sandbox.json`（`host_uid`）与预约标记重算空闲集，所以副本之间**不会**发出同一个 uid；「段相同」本身不是缺口。**真正的未知**是同一 base 上各副本的 **reconcile/GC 权限**（一个 worker 会不会动到另一个 worker 的活树） | ✅ **2026-09-17 已收口（`docs/k8s-deployment.md` §13）**。判据脚本 `deploy/scripts/multiworker_interference.py` 在真集群（k0s + Calico VXLAN + NAS NFSv4.0）跑通：① 4 个沙箱 **2+2 跨两个 worker**；② 从**磁盘**读出每棵树的宿主 uid **互不相同**（`[10000, 10001, 10002, 10006]`）且都不是 worker 自己的身份；③ 重启一个 worker（换 node id ⇒ 它看共享 base 上 4 棵活树**全是无主**）后断言 **4 棵树都还在**、幸存 worker 的沙箱**仍能读写自己的文件**、起手 reconcile 摘要 **`deleted=0`**（4 棵全判 `protected_elsewhere`）、两侧预留归零。整轮 3 分 23 秒。<br>**清单解除 pin**：`worker.yaml` `replicas: 2`、`autoscaler.yaml` MIN=2/MAX=16；`strategy.maxSurge: 0` 保留但理由换成**容量**（Deployment 无 `requests` ⇒ Kubernetes 把 2 CPU limit 复制成 request，surge 在 4 核节点排不下）。单测改名/改断言后 **22 passed**。<br>**前置（写进清单注释）**：共享存储的锁必须跨节点 —— uid 池的 flock 要真互斥，阿里云 NAS 上只有 **NFSv4.0** 成立（v3+`nolock` 只是本地锁）。<br>**路上另修**：控制面把**已死节点**当健康节点继续派活 ⇒ 建箱 502；根因是 300 秒的孤儿窗口被复用在**放置**上，现另立 `PLACEMENT_MAX_HEARTBEAT_AGE_S=15`（只影响放置，不动孤儿判定）。<br>**衍生**：N20（重启后 node id 变、路由不到）与 N21（reconcile 扫描期间不响应文件 API）。 |
| N14 | fork | **用真根替换"虚拟根"：评估 `mount ns + pivot_root` 形态**（不是 `chroot(2)` 本身）。现状：chroot（镜像 rootfs）形态**没有内核级根** —— 子进程内核根仍是宿主 `/`，rootfs 视图完全由 supervisor 拦截路径 syscall + `chroot_root` 翻译制造（`chroot/dispatch.rs`），`landlock.rs` 的 fail-closed 兜底因此依赖"拦截清单完整"（2026-09-17 审计 OBS-1/OBS-2 已两次证伪该前提：`chroot` 可 chroot 到宿主目录、`inotify_add_watch` 可监听宿主目录并收到宿主文件名） | **未开始（评估项，只出决策文档不动代码）**。收益：路径空间变成内核不变量，`chroot_path_syscalls()` 的完整性不再承担安全职责；`/workspace`+`/home/user` 同一宿主目录的别名由 bind mount 天然表达（可删掉 chdir 记录机制）；exec 的 `PT_INTERP` 补丁 + memfd 那套可删（内核按新根解析解释器）。代价：`fs_mount`（workspace/卷/`minimal_dev` 六节点）全在 rootfs 之外 ⇒ **必须引入 mount namespace**，而 core 现在完全不建 mount ns（`CLONE_NEWNS` 只出现在拒绝 clone 命名空间的过滤器里）；需先做宿主兼容性矩阵（节点是否允许非特权 userns+mount ns，k8s 已见 fail-closed 形态）+ 全量回归成本。**建议判据**：若宿主矩阵允许，这是"加固 + 简化"双赢；若不稳妥，则以 N14 的替代方案收口 —— 继续用路径面账本（`sys/path_surface.rs`）把 fallthrough 集合钉成 CI 不变量。；**2026-09-17 可行性实测（N14 追加）**：本机 OrbStack（kernel 7.0.14）上，`--cap-drop ALL` 只带 worker 实际拥有的四个 cap（无 SYS_ADMIN）时，`unshare(CLONE_NEWUSER)`→self-map→`unshare(CLONE_NEWNS)`→`bind /usr`→`mount tmpfs`→self-bind→**`pivot_root` 全部成功**（seccomp=unconfined 的对照组）—— 说明**不需要任何新的宿主特权**，cap 来自 fork 已经为每个沙箱建的 userns，与既有 netns 同一机制。**但部署形态下有两个硬阻塞，都出在清单而不是权限**：① `deploy/seccomp/sandlock-worker.json` 里 `mount`/`move_mount`/`open_tree`/`mount_setattr`/`fsopen`/`fsmount`/`fsconfig`/`fspick`/`umount2` 只在容器带 CAP_SYS_ADMIN 时才 allow，而 A6 已删 SYS_ADMIN ⇒ 实测同一序列 `mount` 直接 EPERM；② `pivot_root` **根本不在 profile 的 allow 组**（defaultAction=ERRNO）无条件被拒，`chroot` 则要 CAP_SYS_CHROOT（也不在 worker 的 bounding set）。所以要做的是把 mount 家族与 `pivot_root` 移到**无条件 allow**（允许 syscall ≠ 授予 SYS_ADMIN：内核仍要求拥有该 mount ns 的 userns 里的 CAP_SYS_ADMIN，容器 CapEff 可以保持 0）；AppArmor 侧 stock `docker-default` 有 `deny mount`，本机 OrbStack 未拦（实测默认 profile 下成功）⇒ 属宿主相关，必须在目标机复验；**2026-09-17 暴露面实测（N14 追加二）**：① **沙箱侧零增量** —— seccomp 过滤器叠加，把外层 profile 改成允许 `mount`/`pivot_root` 后，沙箱内 `mount`/`pivot_root`/`umount2`/`unshare(NEWNS)` 仍全部 EPERM（内层 `DEFAULT_BLOCKLIST_SYSCALLS` 说了算）；② **无法做参数收窄** —— Docker profile JSON 的 `SCMP_CMP_MASKED_EQ` 语义是 `(arg & value) == 0`（其 `clone` 规则即靠此拒命名空间位），只能表达「MS_BIND **未**置位时允许」，所以「只允许 bind mount」表达不出来（实测：value=4096 时 tmpfs 放行、bind 被拒，正好相反）；因此本项是**二选一**：整族放行，或不放行。若做，务必只放 `mount`+`pivot_root`，不要连带 `fsopen`/`fsconfig`/`fsmount`/`move_mount`/`open_tree`/`mount_setattr`/`umount2`。背景与实测见 `docs/security-audit/findings.md` 的 OBS-1/OBS-2 与 `docs/security-audit/attack-surface.md`「chroot 形态的正确读法」 |
| N15 | fork | **pure（无 chroot）形态的统一闸门**：该形态没有 rootfs、没有路径中介，只有 Landlock 一道网，而 Landlock 访问位是闭集 ⇒ "带路径但不在闭集里"的 syscall 全部无人拦。2026-09-17 实测同一宿主文件：`openat` 被 EACCES（落在 `READ_FILE`），而 `getxattr` **读回宿主 xattr**、`open_tree` **返回 fd**、`inotify_add_watch` **投递宿主事件**（详见 `docs/security-audit/findings.md` OBS-7） | **未开始（真实缺口，中）**。两条路择一：① 给 `NotifPolicy` 补非 chroot 的 readable/writable 集合 + 一个"仅 Landlock 档位"标志，对"Landlock 覆盖不到的带路径 syscall"注册统一的策略判定 handler（`dup_fd_from_pid` 代执行，同 `handle_chroot_inotify_add_watch`）；② 评估 `N14 扩展形态`——pure 形态也合成一个真根（bind mount `/usr`,`/lib`,`/bin`,`/opt`,workspace,`minimal_dev` 后 pivot），一次关掉整类，但要接受 mount ns 前置条件与 pure 语义变更。**前置已完成（2026-09-17，fork `733fbb0`）**：账本已按形态分类（`PURE_LANDLOCK_GATED` 25 / `PURE_GATED_ELSEWHERE` 14 / `PURE_UNGATED` 35，用例钉住三张清单恰好划分路径面）；算出来的未闸门集合是 **35 条**而不是 1 条 —— 元数据族（stat/lstat/newfstatat/statx/statfs）、存在性探测（access/faccessat/faccessat2）、readlink(at)、chdir/fchdir/getcwd、chmod/fchmodat、时间戳族、**全部 8 条 xattr**、inotify_add_watch、open_tree，外加 5 条新内核才活的 at 风格调用。即 pure 形态泄露的是宿主**元数据**（存在性/大小/时间戳/inode/链接目标/xattr），剩余工作就是 ① 或 ② 本身。**实现捷径（2026-09-17 设计勘查）**：chroot dispatch 的 handler 全部按 `ChrootCtx{root, mounts, readable, writable, denied}` 解析——把 pure 形态的 `chroot_root` 设为 `Some("/")`（虚拟路径 == 宿主路径，`landlock.rs` 的规则前缀也随之退化成今天的形式，语义不变），就**可以直接复用现成的 stat/statx/readlink/getdents/statfs/xattr 等 handler**，不必为 pure 形态另写一套代执行；只需：(a) 给 plan 加一个独立 feature 标志，让 pure 形态只拦截 `PURE_UNGATED` 那批而不是整份 `chroot_path_syscalls()`（避免把已由 Landlock 正确覆盖的 open/exec 也拖进中介，性能与风险都不划算）；(b) `can_read`/`can_write` 的集合用 pure 形态的 `fs_readable`/`fs_writable`。代价：pure 形态从"内核 Landlock 单闸"变成"中介 + Landlock"，需要跑 gate B 那一档回归 |
| N16 | 运维/k8s | **自建集群**：新节点 172.18.80.94 + 172.18.80.140，用于验证**沙箱数据面与多副本**（ACK 内核 5.10 无 Landlock / 无 userns，物理上不可验；§6 B/C 至今没在真集群跑过） | **2026-09-17 已建成并跑起来**：k0s `v1.36.4+k0s.0` 双节点（.94 controller+worker、.140 worker），三道闸门全过（Landlock ABI 6 / userns 30519 / 无 AppArmor），清单经 `deploy/k8s-k0s/` overlay 部署成功，**§6 A ✅、§6 C ✅（单节点）**；`.140` 与 compose 生产栈共存已验（6 容器照跑、gateway 仍应答、KUBE-* 与 DOCKER-* 链共存）。抓出 8 条真集群问题、修掉其中 6 条，见 `docs/k8s-deployment.md` §10.3（seccomp 根随 kubelet `--root-dir`、headless per-pod DNS 对 Deployment 不存在、非 root worker + 网络文件系统做不了 chown、NAS 的 v3/v4 锁语义、卷根权限、容量默认值、Docker Hub 不通、node id 抖动）。**§6 B 与 N13 被 N17 挡** |
| ~~N17~~ | 运维/k8s | ~~跨节点 pod 流量被云网络拦掉~~ | ✅ **2026-09-17 已解（走路线 B）**：k0s 的 CNI 换成 **Calico VXLAN**（`mode: vxlan` + `overlay: Always` + `mtu: 1450`），节点间只出现已放行的 `172.18.x`。**跨节点 pod 流量已通**，`deployment_smoke.py` 的多节点阶段全过（跨节点分布 / 经 gateway 的命令与文件 / **跨节点迁移且共享 workspace 文件保留** / 网络配置 / 远端卷隔离 / 预留归零）。过程记录：先手工建 VXLAN 与 IP-in-IP 隧道验证这套 VPC 不拦封装（双向 0% 丢包）；k0s **拒绝给已有集群换 CNI**（`cannot change CNI provider from kuberouter to calico`）⇒ 按 Calico 重建集群；calico 三个镜像都在 quay.io（连通），无需镜像到 ACR。详见 `docs/k8s-deployment.md` §11 |
| ~~N18~~ | E2B/运维 | ~~模板沙箱的首条命令在共享存储上会「卡住」，并连锁到节点被判不健康~~ | ✅ **2026-09-17 已解（做了 (c) + (a)）**。<br>**诊断（三层，全部实测）**：① 慢——同一份 python-slim rootfs（2111 文件）解到共享 NAS **61.4 秒** vs 节点本地 overlay **0.26 秒**（240 倍），而 compose 生产栈的等价物在本地 XFS 上（0.3 秒）所以从未暴露；② 阻塞——`SandboxRuntimeContext`（含 `create_executor` → `resolve_image_rootfs`）由**首个 RPC** 惰性创建、跑在事件循环上，于是解析多久 worker 就多久发不出心跳（实测 3 分 46 秒空档）⇒ 控制面 15 秒判 unhealthy ⇒ `reap_unhealthy` 把活沙箱当孤儿、释放其 route-B 槽位；③ 预热协议要求 `X-Sandbox-Id`，而 e2b SDK 2.46.0 不发也不处理 428。<br>**(c)**：新设置 `E2B_IMAGE_OCI_DIR` 把「OCI tar 目录」（共享卷，控制面写、各节点读）与「解出的 rootfs 目录」（worker 的 `hostPath` 节点本地）拆开；worker 解析时命中共享 tar 就解到本地并写本地 link，tar 目录由 prune 兜住不再无界。**(a)**：`POST /agent/sandboxes` 在建箱后用 `asyncio.to_thread` 预建 context（`_prime_runtime_context`），首个命令直接命中；预建失败不影响建箱契约（warning + 首个命令照旧报错）。<br>**结果**：`deployment_smoke.py` **20.6 秒全绿**（含模板构建→worker 拉取→镜像 rootfs、箱内 MCP 经代理）、`multinode_smoke.py` **8.7 秒全绿**（4 箱 2+2 跨节点）；两个 worker 的 `reconcile summary` 干净（`deleted=0 protected_elsewhere=0`）。兜底 `E2B_NODE_HEARTBEAT_TIMEOUT`（默认 15 秒不变、k0s overlay 300 秒 → **2026-09-18 收到 30 秒**，推导见 `docs/k8s-deployment.md` §14）保留为安全网。<br>**未做**：③ 那条协议不一致仍在（现在代价被 (c) 压到亚秒，不再拦路）——要不要让冷镜像预热对不带头的客户端透明，需单独设计确认；另 N13 收口仍需专门的交叉干扰用例 —— **2026-09-17 已补：`deploy/scripts/multiworker_interference.py`，见 N13 行与 `docs/k8s-deployment.md` §13** |
| ~~N19~~ | E2B/运维 | ~~**失败的 `Template.build` 留下可解析的残骸**：记录文件 `_templates/<tpl_id>/template.json` 与一个 **0 字节**的 `_images/_oci/e2b-local_<tpl_id>.oci.tar` 都会落盘；之后再构建同名模板时名字解析可能取到残骸，worker 解 rootfs 时报 `Code.INTERNAL: file could not be opened successfully: … empty file`~~ | ✅ **2026-09-18 已修（`docs/k8s-deployment.md` §16）**。① 每条失败路径都调 `_discard_failed_build`：删掉 buildctl 半写的 tar/link，并 `discard` 记录（解绑名字 + 删盘上记录 + `list()` 不展示；**内存保留**，因为 SDK 靠 `…/builds/{id}/status` 拿失败原因）；② 名字解析从「扫盘最后一条」改成 **`created_at` 最新者优先** —— 这条治**存量**残骸，等价于 backlog 里原来记的「绑定最近一次成功构建」。**实测**：失败构建后共享卷零残留、同名重建 1.3 秒成功、按名字建箱跑命令 OK；7 条同名 `smoke-template` 时，重启控制面后解析取到最新那条（`newest-wins` 标记可读）。`tmp/k0s/reset-smoke-template.sh` 那个绕法不再需要 |
| ~~N20~~ | E2B/运维 | ~~**worker 重启后 node id 变了（node id == pod 名），它承载的沙箱记录仍指向旧 id ⇒ 控制面路由不到**。表现：`Node unavailable: All connection attempts failed`，要等记录的 TTL~~ | ✅ **2026-09-18 已修（`docs/k8s-deployment.md` §15）**。**根因**：Deployment 的 pod 名随机，而 node id 就是 pod 名 ⇒ 每次重启都成了「永久丢节点」，E6.1 那条**本来就设计好的分区恢复**路径（控制面把断开期的记录标 orphaned 但不删 workspace，worker 用同一 id 回来再 un-orphan）被彻底绕过。**修法**：worker 换成 **StatefulSet**（`e2b-worker-0/1`，跨重启稳定），`serviceName` 指现成的 headless Service，`podManagementPolicy: Parallel`；autoscaler 新增 `E2B_AS_K8S_KIND`（默认 deployment，兼容老集群）+ RBAC 补 `statefulsets{,/scale}`；k0s overlay 的两个 worker patch 的 `target.kind` 跟着改（**漏改会让 worker 以 uid 65534 跑、建箱全部失败在 `.uid_pool.lock`**，已实测）。**验证**：删掉 `e2b-worker-0`，同名回来，126 秒后文件 API 恢复、路由未变、记录仍在（日志 `reconcile: restored sandboxes …`）；N13 脚本里原来那条 `lost their route` 的 NOTE 随之消失。**关键判据**：不能用 worker 上报的运行时列表做认领（含共享 base 上所有树 ⇒ 会偷别人的沙箱），要让「是不是同一个 worker」有确定答案 —— 这正是 pod 名稳定与否在承担的角色；compose 侧一直是稳定 id（`E2B_NODE_ID: worker-1/2`） |
| ~~N21~~ | E2B/运维 | ~~**reconcile 压在事件循环上**：`_scan_workspace_runtimes` 是同步调用（`iterdir` + 每棵树读一次 `sandbox.json`），而且**心跳与 reconcile 在同一个协程**（`NodeAgent._loop`）⇒ 空档 = 5 秒 + 一整轮，轮次时长随 base 树数增长、没有上界~~ | ✅ **2026-09-18 已修**。**修法**：① 轮次独立成 task（`_reconcile_round`，单飞：一轮在跑时不消费触发条件，留到下次心跳，既不丢也不并发），心跳循环只剩「注册/心跳 → 可能起一轮 → sleep(5)」；② `_scan_workspace_runtimes` 与 `_verified_teardown_plan` 走 `asyncio.to_thread`；③ `stop()` 把在跑的轮次一起取消；④ 轮次用自己的 HTTP client（心跳那个每 pulse 就关）。**附带**：启动时现在只跑一轮恢复 reconcile（以前注册分支跑一轮 + `_reconcile_pending` 未消费导致 5 秒后白跑第二轮）。<br>**真集群验证**（base 灌到 3002 棵树，一轮实测 7.8 秒）：重启 worker 与重启控制面两条路径下，心跳仍是 **5.00 / 5.01 秒**（修前应为 5+7.8 ≈ 12.8 秒）；同一 worker 上沙箱的文件 API 346 次探测**最差 0.30 秒、超 1 秒 0 次**。契约测试 `test_orphan_tree_gc.py::test_heartbeats_keep_their_cadence_while_a_round_scans_the_base` 把扫描故意卡住后断言心跳照发（老形态 RED 已实测）。<br>**注意数字别认错**：24 秒 / 43 秒那组是**镜像缓存 prune 的 walk**（383k 文件，`image_resolver.py::_schedule_cache_prune`），那条早已挪到线程上，与本条无关。<br>**结果**：`E2B_NODE_HEARTBEAT_TIMEOUT` 随之从 60 秒收到 **30 秒**（见 `docs/k8s-deployment.md` §14） |
| ~~N23~~ | E2B/运维 | ~~**两条配额后端语义不一致：fd 后端把 soft 限额也设成 hard**~~ | ✅ **2026-09-18 已修（`docs/k8s-deployment.md` §18）**。`xfs_quotactl.set_limit` 现在只置 `_FS_DQ_BHARD` 并把 `d_blk_softlimit` 显式写 0（保留 `_FS_DQ_BSOFT` 位是为了把 0 写下去而不是继承旧 dquot 的值），与 subprocess 后端（`limit -p bhard=<mb>M`）和 XFS lane 的契约一致。实测：生产机上那行变成 `soft=0 hard=1048576`。**追寻这一条时带出了 N24**，两条一起改、一起验：XFS 契约 lane 由 5 failed/5 passed 变成 10 passed/1 skipped |
| ~~N24~~ | E2B/运维 | ~~**fd 后端的项目打标/去标只作用于一个 inode，而 `project -s/-C` 是递归的 ⇒ 每沙箱磁盘限额在生产上完全失效**~~：建箱顺序是「建 `<id>` → 建 `<id>/workspace` → 才 provision」，`PROJINHERIT` 只影响*之后*创建的条目，于是 `workspace/` 停在 project 0，沙箱写的文件全部记进默认项目 | ✅ **2026-09-18 已修（§18）**。实测症状：写 50 MiB，**沙箱自己那行 4 KiB→8 KiB，而 project 0 涨了 50 MiB**；`lsattr -p -d` 显示 `<tree>` = projid+P、`<tree>/workspace` = 0。**修法**：新增 `assign_projid_tree` / `clear_projid_tree`（深度优先覆盖**已存在**的每个目录与文件，`O_NOFOLLOW` 且跳过软链接），fd 分支的 provision 与孤儿清理改用它；`release_project` 保持单目录（调用方紧接删树）。**生产验证**：升级后写 50 MiB → 沙箱那行 **+50.0 MiB**；`dd` 写超 1 GiB 被 `No space left on device` 挡住（限额终于生效）。⚠ **运维影响**：这一版起默认 `E2B_DEFAULT_DISK_MB`（这台 1 GiB）会真的拦住超限写入，老工作负载若写超需要调大 |
| N25 | 运维/k8s | **k8s 主线上没有每沙箱磁盘硬限**：共享卷是阿里云**托管 NAS（NFS）**，而 NFS 上的每沙箱配额只能由 NFS 服务端执行（agent 得跑在那台我们碰不到的存储服务器上）。compose 停用（2026-09-18）后这是 k8s 唯一缺的实能力：跑飞的沙箱可以把 50 GiB 共享卷写满，连带影响同一 base 上所有 worker。剩下的软信号只有 worker 心跳的 `usedDiskMB`、控制面的 disk-warn/error 计数、以及节点层 `E2B_NODE_DISK_MB` 记账（`docs/k8s-deployment.md` §3「配额」行与 §20） | **已评估（2026-09-18，`docs/disk-quota-options.md`）**，结论比原来那条"未选型"清楚得多：**① 我们的存储其实支持**——阿里云 NAS **目录配额**（通用型 NFS，`SetDirQuota` `QuotaType=Enforcement` 是**硬限**，服务端执行，worker 零成本；另有 `FileCountLimit` 管 inode），**代价是每文件系统 500 个目录配额上限、`SizeLimit` 单位是 GiB 整数、需要 RAM 凭据**（这台 ECS 目前没挂角色）。**② 不受存储类型限制的方案里，"硬+便宜"不存在**：写路径记账要把 `write`/`pwrite64`/`ftruncate`/`fallocate` 拉进中介集合（这些现在明确在 `NON_PATH_SYSCALLS` 里，即**完全没拦**），等于给最高频 I/O 加 supervisor 往返；每沙箱 loop 镜像要 `CAP_SYS_ADMIN`+`/dev/loop` 且 NFS 上不稳；cgroup 没有空间配额（排除）；稀疏占位在 NFS 上不保留（排除）。**③ 无论选哪条，都要补"卷级水位闸门"**：共享存储下磁盘准入应是**卷级台账**而不是每节点一份声明预算，而且 `diskWarnCount/diskErrorCount` 现在只上报、没有任何消费者。**待你定两件事**：这台 NAS 是否通用型；超限语义要 ENOSPC（目录配额）还是停箱（B/C）。<br>**追加（同日实测）**：把 **loop 镜像**那条路也量了（`.94`，同一个 NAS export）——机制是「定长镜像文件 → `losetup` → 内层 ext4 → 沙箱工作区就是挂载点」，**配额就是内层 FS 的容量**，32 MiB 镜像里写 64 MiB 得到 `dd: error writing …: No space left on device`（exit=1），语义最正、完全不依赖存储。但实测代价明确：① 镜像在这台 NAS 上**不是瘦的**（`truncate -s 256M` 后 `du` 直接 256 MiB）⇒ 每个空沙箱也占满额度、50 GiB 卷只能放 **50 个 1 GiB 沙箱**（比目录配额的 500 还紧）；② 吞吐 +35%（128 MiB+fsync：0.50 s vs 0.37 s）；③ 要 `mount` + `/dev/loop-control`（A6 已删 SYS_ADMIN，N14 说过要改 profile）；④ 树变成不透明镜像 ⇒ 扫树/GC/模板 copytree/迁移全要重做，崩溃还会留残留挂载。结论：**兜底方案，不首选**。<br>**再追加：卷级闸门的成本与信号（`docs/disk-quota-options.md` §5.1）**——`statvfs` 在 NAS 上 **2.2 ms**（本地 XFS 1.3 µs），而心跳**每 5 秒本来就在调**它 ⇒ 闸门增加的是"一次比较"不是一次 syscall；新鲜度**立刻可见**（写 256 MiB 后下一次 statfs 就是 −256.0 MiB，无缓存）。真正的风险是 PV 是 `hard timeo=600`：statfs 在请求路径上遇 NAS 不可达会**无限重试**，所以闸门要消费心跳已采的值，不要在建箱路径上同步读。**更要紧的是测出前提不成立**：pod 里 `df` 是 **10 PB（整个 NAS 文件系统）**，我们的 PV 50 GiB 只是标称 ⇒ `used/total`=0.0053%，`diskWarn/diskError` **永不触发**。所以水位闸门必须建立在**我们自己的台账**上，而不是 statfs 比例。<br>**方案已定（`docs/disk-quota-options.md` §7）**：**L1 现在就能做**（纯记账、不依赖任何未知）—— 新增 `E2B_WORKSPACE_CAPACITY_MB`，CP 侧建**卷级台账**（`sold = Σ 活沙箱 disk_mb`），`>0.95×capacity` 停准入（503）、`>0.85×` WARN，**同时**退休/改造那两个永远绿的 `diskWarn/diskError`；**L2 二选一**（拿 NAS 规格后）：通用型 → 目录配额 `SetDirQuota(Enforcement)` + 删箱 `CancelDirQuota` + 给 `/sandlock` 设 50 GiB 真边界（约束 500 目录、GiB 粒度、RAM 凭据）；非通用型 → 增量测量 + 超限**暂停**并把语义写进 API 文档；**L3 兜底** loop 镜像。**不做**：写路径记账、cgroup、稀疏占位、本地 XFS 工作区。<br>**L1 已落地并复验（2026-09-18）**：`E2B_MAX_TOTAL_DISK_MB=10240` 显式进 k0s overlay（更正：台账机制本来就有、**代码默认值就是 10240**、compose 也设过同一个数 ⇒ 两个栈一直同口径；先前"k8s 从来没设"是**错的**）；拒绝时给专门文案 `shared workspace disk budget exhausted: N MiB reserved of M MiB`（非磁盘维度保持 "No resources available"）；`/internal/fleet/metrics` 暴露 `workspaceDisk{reservedMB,limitMB,warn,saturated}`；`global_reserved()` 可读两种后端。**上线后复验发现文案没透出**，根因两处并已收口：① `create()` 的 **Redis 分支**硬编码了旧文案（线上走的就是这支，只修内存分支等于没修）；② **节点闸门**（`select_and_reserve` 返回 None）同样只说"没有资源"，而单节点形态与测试 harness 下它先触发 ⇒ 现在按 `blocking_dimension` 分类，只在**所有可放置节点同因失败**时才点名 `disk`（混合原因保持中性），两条闸门的措辞由 `workspace_disk_refusal()` 单点产出。**L2a（目录配额）已否决**（运维：不想引入云 API 依赖）⇒ **L2b 已落地（2026-09-18，`0.1.0-362-…`）**：**没有 inotify** —— 实测推翻了这个前提（整树 walk 只要 10.9 ms/2000 文件、35.7 ms/10000 文件，单文件亚线性、贵在目录 ≈2.5 ms/目录；而写文件是 16 ms/个），所以是「worker 周期性整树 walk（`E2B_DISK_ENFORCE_INTERVAL_S`，默认 30 s、0 关闭，1 s/轮预算 + 游标轮转）→ 心跳报 `sandboxDiskUsage` → CP `enforce_disk_budget()` 走 E9.2 pause（只动 running 且实测 > `diskMB`，已暂停的跳过；冻结推送失败**不回滚**，下个心跳重推）」；实测值 `None`（DAC 够不到且无 broker）时不报，`unknown ≠ 0`。**顺带核出**：`_provision_remote` 结尾显式 `record.workspace_dir = None`（远端树归 worker 管，设计使然）⇒ `GET /sandboxes/{id}/metrics` 的 `diskUsed` 在 k8s 上**恒为 0**，这个数**还没接到 API**，是 L2b 剩下的收口（落库最后一次实测值并暴露，兼作 resume 时的解释文案）。**L2c 已落地（2026-09-19，`0.1.0-394-g0f2ae02`，`docs/k8s-deployment.md` §22.2）**：worker 不再整树 walk，而是问**中介**哪些目录变了（`dirty.rs`：写意图路径的父目录集合，上限 4096、超了置 overflow；打点在两个 chroot 写 handler 内部，**不是** handler 链上的新 builtin —— 那会被 `ReturnValue` 短路掉），只重扫那些目录；`E2B_DISK_ENFORCE_DIRTY=1` 选中这条路径。实测：400 目录/2000 文件的树 **1043.9 ms → 2.34 ms（446x）**；验收探针在**沙箱内**独立量出的字节数与平台上报**逐字节相等**（6144=6144；追加写 12144=12144），worker 自证日志 `ledger=13 rebuilt=0 walk=0`。**设计里两处被修正**：① 打点位置如上一句（原方案写的"链上新增 builtin"不成立）；② **pure 形态没有 openat 通知**（`chroot_path_syscalls()` 只在 chroot 形态加入计划表）⇒ 该形态永久回落整树 walk。**第一轮集群实测抓到一个真缺口**：中介只在路径 syscall 上打点，"开文件→等 12 s→追加"在 `open` 之后没有任何路径 syscall，平台只报 7144/12144；补了两条边界 —— **grace**（`E2B_DISK_DIRTY_GRACE_S=120`，写过的目录持续复检）与 **reconcile**（`E2B_DISK_RECONCILE_INTERVAL_S=900`，定期从真实 walk 重建，兜中介结构上看不见的写）。<br>**L2c 之上的 C 已收口（2026-09-19，`0.1.0-396-gbea497c`，§22.3）**：单文件硬限从"整棵树预算"改成**剩余额度**，且**每次 exec 现算**（L2c 让刷新只要毫秒级；否则连续写会各自读到旧值）—— fork 加 `ExecParams.max_file_size`（per-exec **收紧**，越界/0 按 `PolicyToWide` 拒；在 `sandlock-init` fork 的子进程里、execve 前应用，因为限额是进程级的）、worker 每个 exec 前按 `diskMB − 账本值` 取 `max(剩余, 下限 1 MiB)`（卷额度只放宽）。**集群实测**：树已用 700/1024 时，单个 1024 MiB 文件落地 **324.0 MiB**；连续三次 900 MiB（各自独立 exec）落地 **324 / 1 / 1 MiB**，树收敛在 **1026 MiB**（做之前同样三次共 **2701 MiB**）⇒ **串行（每次独立 exec）能拦住**。**仍拦不住的**：一条命令里的循环（一个进程一个限额）—— 那格由 5 s 暂停闸门兜，要更硬需要"周期 `prlimit` 下压运行中进程软限"（未做）。<br>**冻结延迟 3.7~4.0 s 的根因已定位并修（2026-09-19，`docs/k8s-deployment.md` §22.4）**：延迟与扫描间隔无关（0.25/1/3 s 三档完全一致），因为里面有一段与间隔无关的阻塞 —— NFS 的 `nfs_getattr` 在请求 ctime/mtime 时会对普通文件做 `filemap_write_and_wait`，也就是 **`stat` 一个"自己还有脏页"的文件会先把脏页写完再回答**（写入节点实测 **1405 ms**，另一台 worker 同一个文件 **0.03 ms**；线程 wall 1722 ms vs CPU 2.2 ms、`nr_throttled=0`、内核态 `D` + `wchan=folio_wait_bit_common/rpc_wait_bit_killable`）。修法是只请求 `STATX_SIZE` 的 `statx`（实测 **0.01 ms**，返回同一个数）：新模块 `envd_service/runtime/brief_stat.py`，被 `dir_ledger.scan_subtree` 与 `priv_helpers.dir_size` 共用（"账本 == 整树 walk"的逐字节契约因此不变），无 `statx` 的平台回退到 `os.stat`。此前几条假设（树太大 / CPU 限流 / 整个挂载点慢 / RPC 排队）都各自被反例打掉，逐条记录在 §22.4。**上线实测**：写入节点 `stat` 正在写的文件 1395 ms → `entry_size` **0.08 ms**；整树 `dir_size` **4.9 ms**；worker 一轮 **1.0/1.6/1.8 s → 0.010/0.011 s**；冻结延迟 **3.7~4.0 s → 1.9~3.7 s**（多数档在 3.1~3.4 s；三处返回值逐字节相同）。**剩下的 ~3 s 不是这条链路**，而是"沙箱写下去的字节什么时候对平台可见"：不带 flush 的 `stat` 只看到**已提交**状态（快速写呈 `0 → 900 MiB → 1800 MiB` 台阶，偶尔抓到 `1151 MiB` 那次延迟就是 1.9 s），带 flush 的 `stat` 要等到这次写结束 —— 两侧一样（沙箱自己的 `os.stat` 同样被卡 0.85 s）。**要再往下压，得换"数从哪来"**：从中介侧按 write 字节记账 / 事件上报，而不是继续调扫描频率。<br>**已做（2026-09-19，`docs/k8s-deployment.md` §22.5）**：① **fork 侧 push 上报** —— 中介在 `openat` 时（ADDFD 回包之后）登记 `(pid, 子进程 fd, 路径, 打开瞬间大小)`，独立线程每 100 ms 读 `/proc/<pid>/fdinfo/<fd>` 的 `pos:`，把增量按 **NDJSON** 推到**单独的 `--events-fd`**（不塞进 request/response 的控制通道）；worker 侧累计并让一轮上报 `walk + 自那轮以来的追加量`（**下界**：可能早、不会晚）。② **运行中进程 prlimit** —— 新 verb `update_file_size_limit`，按**进程组**逐个 `prlimit(RLIMIT_FSIZE)`，只取 `min(当前, bytes)`（永不放大）、一代之内单调、没有 `max_file_size` 天花板就拒（那个天花板才是装 `SIGXFSZ=SIG_IGN` 的地方，否则越界是"进程被杀"）；worker 只在实质下降（默认 4 MiB 步长、最短 0.5 s 间隔）时下发。**边界**：pure 形态没有 `openat` 通知 ⇒ 该通道只在 chroot（route-B）形态存在；打开→写完→关闭都落在同一采样间隔内的 fd 会被漏掉（那部分仍由 walk 稍后记到）。**集群实测（`0.1.0-404-g01fa514-20260919-134431`）**：① 冻结时的超支 **1800 MiB（超 776）→ 1025/1148 MiB（超 1~124）**，"跨过预算→暂停"只剩 **20~60 ms**（剩下的 2.8~3.9 s 是写者自己写到 1024 MiB 需要的时间）；② 一条命令里死写一个文件：**`dd: … File too large`，rc=1，沙箱仍 running**（不再靠暂停兜）；③ 合法大文件（900 MiB / 1024 预算）**正常写完** —— 这是修了一个自造缺陷：按"剩余"直接施加会让文件吃自己的尾巴，现在施加的是 `下发值 + 该进程组已增长量`；④ 无回归：per-exec 上限 324/1/1、账本逐字节相等、两个 smoke OK、core 879 / supervise 27 集成 / worker 1170+321 全绿。<br>**push 在集群上第一次真正跑通（2026-09-19，`0.1.0-405-g8ca0551-20260919-192955`，§22.5.7/§22.5.8）**：上线后 `watch state` 从「1515 个 tick 全是 `watching: 0`」变成 `watching: 1` + `disk append +1 MiB (wakeup=True)`。通道一通，立刻暴露三个都在它后面的缺陷，各自都补了测试：**① 同一字节被算两次** —— 一轮原本报 `walk + pushed`，回写落地后同一批字节同时在两者里；实测 287 MiB 的文件被报成 **647 MiB**，于是「剩余」只有 377 MiB，`剩余 + 自身已写 = 664` 把一个**合法的 900 MiB 文件在 696 MiB 处 EFBIG 切断** —— 改成 `max(walk, 上次上报 + 本次 push)`；**② 沙箱会挪走注入的 fd** —— `exec 9>>file` dup 到 9 并关掉原 fd，watch 记的 `(pid, fd)` 几毫秒内失效、条目第一 tick 就被丢 —— 精确 fd 读不到时按路径在该进程 fd 表里回退匹配；**③ pid 命名空间表可能是空的** —— `PidNsMap::new` 在 leader 正在进自己 userns 时读 `/proc/<leader>/ns/pid` 会被拒、`.ok()` 吞掉后留下空表（沙箱 `ls /proc` 一个数字都没有、watch 也翻不出 pid）—— 现在保留 leader pid 重试 inode、仍读不到则按进程树判定、空表必打 stderr。**实测**：A 超支 124/246/124 MiB（验收线 1/4 预算）**PASS**、B 单命令失控写者内核 EFBIG 拦住且沙箱仍在跑 **PASS**、合法 900 MiB 文件 rc=0 且 `ls -l` = 943718400；per-exec 324/1/1、ledger 6144=6144 & 12144=12144、单机+多机 smoke OK、worker 单元 1186 通过、fork 881+27。**残余是采样粒度不是记账**：这类写者 250 ms 写满一个 124 MiB 文件，而 mediator 每 100 ms 采一次，整文件可能落在一个采样里，两个文件就能各继承一次「上次收紧时的剩余」（实测阶梯 900 MiB→limit 124 MiB→1148 MiB 冻结）。<br>**两条路都做了（2026-09-20，`0.1.0-408-g98052a0-20260920-120350`，§22.5.9）**：① **open 时共享分配** —— `WriteFds` 记「已花计数 + worker 最后一次给的额度」，`openat` 按 `baseline + (额度 − 自那以后已花)` 发放（下限 1 MiB，且不低于该进程现有描述符的最大授予）；② **中介自持 fd + 20 ms 采样** —— 内核注入是把 listener 的 fd dup 进沙箱（同一 open file description、同一偏移），所以直接读自己的 `/proc/self/fdinfo/<fd>`，不需要 pid 翻译、不受 `exec 9>>` 搬 fd 影响。**实测**：A 超支 **1 MiB**（776 → 124 → 246 → 1）、合法 900 MiB 文件 rc=0、单文件（700 已用）落 **324.0 MiB**（正好剩余）、紧接着第二个文件落 **1.0 MiB**（池已花光的下限）、串行两次 324.0/1.0 MiB 且树收敛 **1025 MiB**、ledger 逐字节相等、单机/多机 smoke OK、fork 885+28、worker 单元 1187。过程中又修掉三个坑（各带测试）：授额必须是可回升的**值**而不是单向棘轮（`>/dev/null` 那次发放把后续所有 fork 压到 1 MiB）；push 与 walk 是同一个量的两个估计不能相加（700 MiB 被报成 777 MiB）；收紧时的「文件已写量」必须现读而不是用上次采样（113 vs 54 MiB 少给 59 MiB）。**已定并落地（2026-09-20，`0.1.0-412-gd359196`）**：**超上限 = 不能写，不冻结**。写侧:`RLIMIT_FSIZE=0`（EFBIG)+中介对 `O_CREAT`/`mkdir`/`symlink`/`link` 返回 ENOSPC（建空条目也能撑大树,限额管不到);其余全部照常:读、exec、删除、rename、stat,腾出空间后下一条命令立刻能再写。控制面只记录不再暂停,下限 knob 一并删掉。**实测**:A 超支 **0 MiB**（三次 run 全部 1024 MiB 整)、超预算时新建文件/目录 ENOSPC、超预算时删除可用、腾空间后可再写、沙箱全程不被冻结;单文件 324.0 MiB、串行 324.0/0 MiB、ledger 逐字节相等、单机+多机 smoke OK。同轮还修掉一个自己引入的坑:A' 的中介自持 fd 会让 NFS 把被删文件留成 `.nfsXXXX` 占着空间(删除永不释放 ⇒ 永远无法恢复写入),现在 watch 会问沙箱自己还有没有描述符指向该文件,没有就记下最后偏移并放手。<br>**天花板归平台、软限可回升、unlink 立刻归还（2026-09-20，`0.1.0-414-g68b9196-20260920-154249`，§22.5.10）**：内核里**没有**"实例级文件空间限制"——`RLIMIT_FSIZE` 是进程级、只管单文件大小，"实例上限"一直是 launch 的**硬限** + **中介的池**；而内核规则里任何进程都能把 `rlim_cur` 抬到 `rlim_max` ⇒ 之前"可动的那一半"放在硬限上（单向、不可回升）时，workload 只要抬自己的软限就拿到中介没打算发的额度，释放空间后又永远回不去（这正是"为什么 workload 能自己调限制"的答案）。现在：硬限 = 实例上限（平台不动、guest 也动不了）、平台只动软限、seccomp 闸门让 guest 只能**下调** `RLIMIT_FSIZE`（抬高 → EPERM；实测在沙箱里用 ctypes 直打 `setrlimit`/`prlimit64`，抬高 4 种形态全 EPERM、下调返回 0、之后正常 `dd`）、`unlink`/`rmdir` 成功即 credit（按删除前的大小）并**当场**把额度交还调用者（同一条命令 `rm -f fill.bin; dd of=after.bin 1MiB` → `rc=0`、`after.bin=1048576`）。同一轮还抓到被那条棘轮掩盖的漏洞：**决策点的账必须是当下的**——watch 的 tick 与 worker 的账本两侧都滞后时，700 MiB 已用 + 324 MiB 文件后**紧接着**的第二个文件落了 **48 MiB**；现在 `is_exhausted`（建条目闸门）/ open 发放 / unlink 归还三处都先 `WriteFds::flush_held()`（中介自持 fd 现读）再算，同一形状 **0/8** 越界。实测：A 超支 **0 MiB**、超预算时新建文件/目录 ENOSPC、删除可用且可恢复、单文件 324.0 MiB、第二个文件 0 MiB、串行 324.0/0 且树收敛 1024 MiB 整、ledger 逐字节相等、两个 smoke OK、fork core 887 + supervise 28（1 个预存在失败）/ 主仓单测 1190（13 个既有失败）。代价：单文件上限仍 = 实例上限，"总量"靠中介的发放入口 + 池。另记一条运维事实：worker 滚动重启可能打断 base image 预热（缓存目录只剩 `.lock`）⇒ `Sandbox.create()` 回 `428 warm_required`，预热端点是 **POST** `/agent/images/<urlencoded-ref>/warm`（GET 只查询）。<br>**收口（2026-09-21，§22.5.13）**：两条部署路径现在 rollout 后**自动预热**（`deploy/scripts/warm_base_image.py` 一份 HTTP 契约，在容器内跑：`apply.sh` 走 `kubectl exec -i`、`upgrade.sh` 走 base64 + `docker compose exec -T`；`SKIP_WARM=1` / `--skip-warm` 关掉，失败非零退出）。集群实测：热节点两 worker 都 `peek cached=true`（无需手动 POST）；把 rootfs 挪走造冷节点后同一命令 `peek cached=false` → POST → `cached=true`，**18.8 s**。<br>**另收口一条 fork 账（E，同日）**：`sandlock-supervise` 那条"预存在失败"（`test_supervise_path_serve_…` 报 `timed out waiting for registered socket to appear`）**不是 supervise 的 bug，是 AF_UNIX `sun_path` 108 字节上限**：夹具把 registry socket 建在 `<repo>/tmp/supervise-ctl-<pid>-registry/<fnv>.d/control.sock`，而 `CARGO_MANIFEST_DIR` 保留 `/crates/sandlock-supervise/../../` 这一段 ⇒ `/src` 挂载下 **100 字节**（距上限只剩 8），宿主长路径下 **153 字节**，`bind` ENAMETOOLONG、进程退出、夹具超时。修复在 fork 里：override 改成 `/tmp/sandlock-ctl-test-<pid>`（68 字节，镜像生产的 `/tmp/sandlock-ctl-<uid>` 形态），长路径下同一条用例由红转绿，`/src` 全量 30 通过、core 888 通过。**顺带记一条**：fork 的 `docs/test-baseline.md` 已过期（`core_lib` 写 848，实测 888），所以 `scripts/test-all.sh` 的基线断言现在必然红一次——要么按本轮数刷新基线，要么用定向套件（`cargo test -p sandlock-core --lib` / `-p sandlock-supervise --test supervise`）当证据

| N26 | 安全/存储 | **共享卷是单一信任域：集群内任意 root 可写任意沙箱树**（2026-09-18 实测，超本轮攻击者模型）：`: /sandlock` 被控制面与两个 worker **都以 root 挂载**，两节点实测都能以自身身份写卷根（`WRITE OK as 0:0` / `0:65534`），卷根还是 `1777` ⇒ 机器级没有边界，只有进程级（host uid + Landlock）。对"沙箱内攻击者"**不是洞**（C 层干净），但有一条具体链：OBS-4 的修法让 fleet 级 uid 分配以**卷内** `sandbox.json` 的 `host_uid` 为权威（`uid_pool.py:_recorded_uids`），而它 `0:65534` 可被 root 改写 ⇒ 能让两个沙箱共用 host uid、拆掉第二道墙；**卷上 root squash 与每沙箱 host uid 互斥**（E3.2 需要 worker 对卷 chown）。建议：① `host_uid`/卷切片归属的权威搬到 `SandboxRecord`/Redis + 一致性校验；② 控制面最小权限挂载（`: /sandlock` RO + 仅 `_builds` RW）；③ 周期扫描里加 uid 审计（非本沙箱 uid/非 worker gid 即告警）；④ NAS 控制台确认权限组与是否有别的信任域挂同一路径。详见 `docs/security-audit/findings.md` OBS-9 与 `docs/disk-accounting-dirty-dirs.md` §11。**①（uid 权威搬到 `SandboxRecord`/Redis）与 ②（控制面最小权限挂载）已于 2026-09-18 落地并集群验证**：控制面写沙箱树现在 EROFS（六个 RW 子路径 `subPath` 挂回；顺带把快照记录从根层 `snap_*` 挪进 `_snapshots/`，与 worker 的 `fs/` 同目录）；uid 由 `RedisUidLedger` 分配、随 `hostUID` 下发、worker `claim()` 采用，实测"伪造 `sandbox.json` 抢占下一个 uid"无效（C 仍拿到 10002）且释放回池（D 拿回 10000）。**未做**：③ uid 审计、④ NAS 权限组确认、卷切片归属的权威（仍在卷内）。<br>**追加（2026-09-18，`0.1.0-368-g65b1747`）：平台文件与用户 workspace 已分离**（`docs/disk-accounting-dirty-dirs.md` §12）——`sandbox.json` 与 `command-logs.jsonl` 搬到 `<base>/_runtime/<id>/`（`0700`、属主 = worker），沙箱对它是 **Permission denied**（读写都不行），用户树保持 `0770` sandbox uid；实测沙箱 `rm sandbox.json` 再伪造的成功路径已被切断。顺带：配额口径与快照 payload 都只含用户数据；建箱侧拒绝保留命名空间 id；删除时 `_runtime/<id>` 与树成对收尾。**卷切片 projid 归属是这族里唯一剩下的**（等接上支持 per-sandbox 配额的存储/agent 时再做，届时它落在 `_runtime/` 即天然不可被沙箱改写）<br>**结论（2026-09-21，本轮 C 收口）：接受，不再改造**。判据：这条的前提已经是「在集群里拿到 root + 挂上卷」——那是机器级权限（同一信任域里的 root 本来就能 kill 进程、改 kubelet 配置、读别人的内存），比本轮的攻击者模型（**沙箱内**进程）高一层，所以对"沙箱内攻击者"它不是洞。**①（uid 权威搬到 `SandboxRecord`/Redis）与 ②（控制面 RO 挂载 + 六个 RW `subPath`）已落地并集群复验**，N26 正文点名的那条链（伪造卷内 `sandbox.json` 抢占下一个 host uid）实测无效且释放回池。**剩下的 ③ uid 审计 / ④ NAS 权限组 / 卷切片 projid 归属都不做**，各自带触发条件：③ —— 一旦出现**第二个挂载该卷的信任域**（NAS 上出现我们不知道的挂载方或写入方），或开始把 open API 卖给第三方；④ —— 换 NAS 实例或换挂载用户时随部署复核一次；卷切片 projid —— 接上支持 per-sandbox 配额的存储/agent 时。 |
| N27 | 安全/架构 | **平台状态另起 BASE：让"沙箱看不到平台命名空间"不再依赖形态**（2026-09-18 记录，**优先级低**，触发条件见末）。依据：实测 chroot/image-rootfs 形态下 `_runtime` 对沙箱是 **ENOENT**（沙箱的根是它自己的 rootfs，只有 `/home/user` 与 `/workspace` 被 bind 进来，整颗 `<base>` 不在其命名空间；`/proc/mountinfo` 的 source 也被渲染成 `sandlock`，不泄漏宿主路径），但 **pure 形态（无 chroot）下 `/home/user` 就是 `<base>/<id>` 的真实路径** ⇒ `..` 会到 `<base>`，`_runtime` 变成"看得见但打不开"（DAC `0700`/Landlock → EACCES）。见 `docs/disk-accounting-dirty-dirs.md` §12.1。<br>**目标**：平台状态位于沙箱**永远不经过**的路径上 ⇒ 不可见性不随形态漂移。<br>**范围**：① 第一期只搬 `_runtime`（record + 命令日志）——它只被 worker/CP 读写、沙箱从不读，是最安全的一刀；`_images`/`_snapshots`/`_volumes`/`_builds`/`_templates`/`_secrets`/`_migrate`/`.route-b`/`.uid_reservations` 是后续（`_volumes` 的真实数据要被 bind 进沙箱、`_snapshots` 的 `fs/` 是沙箱树拷贝 ⇒ 搬它们牵动卷挂载与迁移语义，必须单独评估）。② 新增独立设置（如 `E2B_STATE_BASE`）+ 部署清单目录/挂载 + 一次性迁移脚本。③ 落点已收口在 `gateway_common/paths.py` 的三个 helper（`sandbox_runtime_dir`/`sandbox_record_path`/`sandbox_command_log_path`），改 base 即改这一处。<br>**坑（先记账）**：新 base 若与 `<base>` **不同文件系统**，`adopt_legacy_records` 与 park 用的 `os.replace` 会 EXDEV ⇒ 要么让新 base 同卷不同目录，要么改成 copy+unlink（并逐条确认 N12/N24 的成对收尾语义）。<br>**验收**：pure 形态下 `os.stat(<新 base>)` 从沙箱内为 ENOENT 且新 base 不在其祖先路径上；`_runtime` 功能不变（record/日志/采纳/成对收尾）；`deployment_smoke` + `multinode_smoke` 全绿；`_runtime` 迁移后旧树仍可读（回退窗口）。<br>**触发条件**：切到 pure 形态、或去掉 chroot/image-rootfs 形态时**必须**做；若长期只跑 chroot 形态，本条只是"防止形态漂移"的账，保持低优先级即可。<br>**结论（2026-09-21，本轮 D 收口）：不做，保持低优先级**。判据：① 能看见 `_runtime` 的形态只有 **pure**（生产现在只跑 chroot/image-rootfs，沙箱对它 ENOENT，`/proc/mountinfo` 也不泄漏宿主路径），搬 base 因此是纯"防形态漂移"；② 搬迁要动 `_images`/`_snapshots`/`_volumes` 的 bind 语义与 `os.replace` 的 EXDEV 账（正文已列），收益与代价不成比例。**触发条件（任一即做，且落点已收口在 `gateway_common/paths.py` 的三个 helper：改这一处 + 新增 `E2B_STATE_BASE` + 一次性迁移脚本）**：切到 pure 形态（`E2B_CHROOT=0` / 去掉 image-rootfs），或平台状态首次暴露给非本租户读者（第三方审计、跨租户共享 worker）。<br>**2026-09-22 复核（用户决定"暂不做多租户"）**：第二条触发条件（跨租户/非本租户读者）因此维持在"未到"，本条的**唯一活触发条件就是形态切换**——它与 N15（pure 形态只有 Landlock）、OBS-5（pure/本地形态没有磁盘硬上限）是同一个"要不要上 pure 形态"的决策包，建议一起定。 |
| N28 | 安全/语义 | **统一写者身份：文件写入走沙箱，且"暂停"必须真的不可写**（2026-09-18 用户决定）。**实测现状（矛盾点）**：暂停状态下 `files.write`（新建/覆盖）、`files.make_dir`、`files.remove` **全部成功**，连 `commands.run` 都能执行——而 `pause` 自己的文档写着 "it does not gate future execs"，即今天"暂停"= **释放准入预留给别人、自己却仍可继续干活**（与 E9.2 的账本语义相互矛盾：预留释放了，实际消耗没停）。决定：**统一写者身份**——所有进入工作区树的写都经沙箱自身（sandlock 中介 + 沙箱身份），平台不再有牢外写者。<br>**要一并改的（缺一不可）**：① **文件 API（write/mkdir/remove/move/upload）改由沙箱内写者执行**（route-B slot 天然是 "euid = 沙箱 host uid 的 supervise 进程"）；**但没有现成的"把文件 fd/数据注入沙箱"原语**（`fd_inject_connect` 只做网络），要新增一条注入通道（SCM_RIGHTS/unix socket 或复用 slot 控制通道）——属于 fork 侧新功能；② **暂停要门控新工作**（exec 与写都拒绝，并给出可读的 409/503 文案），否则"写走沙箱"在冻结期与解冻后都不成立；③ 上传字节路径变长（先进沙箱再落盘），大文件/高并发吞吐与内存要评估。<br>**收益**：属主/权限统一成沙箱身份（今天上传是 `root:65534`、沙箱自己写的是 `10000`——现成的一致性瑕疵）；路径语义统一（可顺带修掉"SDK 绝对路径按树根解析"的偏差）；配额账本来源单一（`docs/disk-accounting-dirty-dirs.md` §13）；**`RLIMIT_FSIZE` 由此对上传也生效**（免费硬边界）。<br>**API 语义变更（需产品确认）**：暂停的沙箱将**收不到上传、也不能执行**（今天两者都行）。<br>**验收**：暂停 → `files.write`/`mkdir`/`remove`/`commands.run` 全部被拒且文案可读；上传文件属主 = 沙箱 host uid；`deployment_smoke` + `multinode_smoke` 绿；文档与 SDK 语义说明同步。<br>**✅ 2026-09-19 已落地（A+B+C+D）**：明细与实测见 `docs/k8s-deployment.md` §22。三处判断纠正 —— ① "没有现成的 fd/数据注入原语 ⇒ 要新增 SCM_RIGHTS 通道"**不成立**：复用命令通道的 **stdin** 即可（`sh -c <脚本> e2b-write <路径…>`，路径走 argv 不做拼接），fork 侧零改动；② "顺带修掉 SDK 绝对路径按树根解析的偏差"**没做**（独立 API 语义变更，另记）；③ 新发现并已修的缺口：`/agent/sandboxes/{id}/pause|resume` 以前**从不回写 worker 自己的运行时记录**（远端记录永远 `running`）⇒ 门控在分离部署下等于不存在，现在会 `set_state` 并带上原因。A/B/C/D 各自实现、契约与集群验收见 §22。<br>**集群验收（2026-09-19，`0.1.0-388-ge76d38e-20260919-010638`）**：`tmp/k0s/probe_n28_acceptance.py` 全绿 —— 上传身份 = 沙箱命令身份（`10000:10000`）、写不排命令闸门（0.14 s）、暂停 409 / `failed_precondition` / 读照常 / resume 恢复、`dd count=1200` 停在 1024.0 MiB 且沙箱存活、`diskUsed` 169 B → 1200.0 MiB、拒绝文案与 logs 都带原因；`deployment_smoke` + `multinode_smoke` 通过。**验收过程中又抓出并修掉两个真问题**：① 暂停落在"写已过闸门"之后时，沙箱内 helper 被 `SIGSTOP` 冻住 ⇒ 调用方挂到入口 nginx **60 s 504**、临时文件留在树上；现在 `pause_all` 对**内部 helper 中止**（用户命令仍冻结/解冻），写者把"被信号杀掉 + 记录非 running"翻成同一个 409。② 暂停原因原先只写进 `record.logs`，而 Redis 后端的记录**从不持久化 logs**（每次 `get` 都重建）⇒ 线上 `GET /sandboxes/{id}/logs` 永远看不到它；原因是"关于当前状态"的事实，改持久化 `pause_reason`/`paused_at`，并由 logs 端点派生那一行。
| N29 | 运维/API | **长任务（快照）不该是同步 HTTP**：实测 2000 文件 / 1 MiB 的树 `create_snapshot()` **超过入口代理 60 s 超时**——客户端收到 nginx **504**，而**服务端把拷贝跑完并落了记录**（重试会得 409 "already exists"，客户端无法区分"失败"与"其实成功"）；期间入口还会**短暂 502**（nginx 在一次 504 后把 upstream 摘掉一段时间），而 worker 心跳全程 healthy。**已修的同族 bug（本轮，`0.1.0-369`）**：`/agent/snapshots` 与 `/agent/sandboxes` 都在**事件循环上**跑 `shutil.copytree`（同文件其它地方都用 `asyncio.to_thread`）⇒ 拷贝期间心跳与所有请求停摆，节点看起来像挂了；现已改到线程池，并有"拷贝不得阻塞循环 + 失败不得留半份 payload"两条单测，集群验证两节点在拷贝期间保持 healthy。**待做**：① 快照改**异步任务语义**（202 + 轮询/回调，或至少把入口 `proxy_read_timeout` 调大并写进部署文档）；② **幂等重试**：同 id 的重复请求返回既有快照而不是 409；③ 明确"客户端超时但服务端成功"的语义并写进 API 文档；④ 复核入口 nginx 的 upstream 摘除参数（`max_fails`/`fail_timeout`/`proxy_next_upstream`），避免一次长请求把整个入口打成 502<br>**已收口（2026-09-22，§22.5.14）**：先把客户端实际看到的形状量清（`tmp/k0s/probe_n29_sync.py`）——超时后**入口会把 upstream 摘掉约 30 s**（`/sandboxes` 连续 502），所以重试先撞的是入口自身的 502，而不是原以为的 409；服务端确实拷完并落了记录。据此：**② 已做**——`POST /sandboxes/{id}/snapshots` 认 `Idempotency-Key`（或 body `snapshotID`），同键续到的重试拿回**同一份**记录（200 + `alreadyExists`，控制面内按 id 加锁，拷贝进行中就等它跑完）；worker 的 `/agent/snapshots` 对**已完成**载荷（`.complete` 标记）回 **200 alreadyExists** 而不是 409，只有"半份"才是 409（并发保护不变），控制面把 409 如实透成 409 而不是 502。**③ 已做**——文档明确"客户端超时 ≠ 失败"，并写明没带键的重试（e2b SDK 就只发 `name`）会**再建一个新快照**，要可重试语义就带键或先 `GET /snapshots`。**④ 已给出要配的值与理由**（`proxy_read_timeout` ≥ 合法拷贝时间；复核 `non_idempotent`/`max_fails`/`fail_timeout`，它决定超时重试会不会打到第二个上游）。**① 仍未做**：异步（202 + 轮询/回调）保持为后续项，现在的形状是"同步但可幂等重试"。验证：`tests/contract/test_snapshots.py` 八条（新增同键重试、无键重试、worker 完成/半份三条） |
| N30 | 运维/语义 | **镜像/块设备路线的唯一挡路问题：配额口径能不能改成"峰值"**。Spike 已完成（`docs/disk-accounting-dirty-dirs.md` §13.4，2026-09-18 在 `.94` 真 NAS 上实测）：qcow2-over-NBD **薄**（空 1 GiB = 196 KB；写 100 MiB → 101.6 MiB）、**崩溃可恢复**（kill -9 服务端后写请求 EIO，umount/detach/reattach 正常，ext4 journal 回放成功，未 fsync 尾部丢失）、**生命周期毫秒级**（attach 113 / mount 12 / detach 3 ms）、吞吐 **+15–20%**（`--cache=none` 只比 writeback 慢 5%；上一版 raw loop +35% 未复现）。**但**：这台 NFS **不支持打洞**（`fallocate -p` → "keep size mode is unsupported"，`fstrim` 报 973 MiB 而 `du` 不降）⇒ **镜像占用 = 高水位，删文件不退还空间**，与 `diskMB` 的**存量**语义冲突。**待决策**：① 若接受峰值口径（写多少算多少、删了不退）⇒ 走 qcow2+NBD，并要付：privileged/SYS_ADMIN + 设备白名单 + 宿主 `modprobe nbd`（worker 容器 CapEff 无 SYS_ADMIN，实测）、"工作区不再是目录"的整套改造（GC/L2b/快照/模板/CP 读树）、守护进程（每沙箱或每节点）；② 若不接受 ⇒ 维持 `RLIMIT_FSIZE` + 记账软闸门，或重新评估 NAS 目录配额。**遗留物**：ACR 上有一个 spike 镜像标签 `e2b-spike-nbd:0.1`（Dockerfile 在 `tmp/k0s/spike/`），节点上的 `nbd` 模块已卸载、临时 pod 与 `/nas/_spike*` 已清理 |
| N31 | 存储/配额 | **字节账本看不见"条目"：空文件/空目录 0 字节，目录本身的块也不计**（2026-09-20 集群实测，`0.1.0-414-g68b9196`，N25 的收尾缺口）。在沙箱里建 2000 个空文件：平台 `diskUsed` 全程 **0 字节（delta 0）**，而文件系统上 `os.stat(dir).st_size` = **16384**（NFS 每目录至少 16 KiB 的目录块也**完全不进账**，因为 `dir_size`/`dir_ledger` 只累加非目录条目的大小）。唯一能挡住"建条目"的闸门是"池**恰好**为 0"，而建空条目永远不消耗字节 ⇒ **数量无上限**：真实风险不是容量而是 **inode/元数据耗尽**（`docs/disk-quota-options.md` §35 已写明"inode 耗尽同样是整卷故障，这条我们现在连 XFS 形态都没做"），以及整树 walk/GC/快照的成本随条目数线性上升。顺带量到的第二个事实：建条目本身很贵——**200 个空文件 ≈ 4.2 s（≈21 ms/个）**，且这个速率在 200→2000 个之间**不随目录变大而改善**（10 批各 4.1~4.5 s）；`rm -rf` 2000 个文件 **20.8 s**；一次命令里建 20000 个文件会把命令的响应流打断（客户端拿到 `unexpected EOF`，worker 日志只有 slot 关闭）。**已落地（2026-09-20，`0.1.0-423-g01769b3`，§22.5.12）**：修法 ① **做了**——`dir_ledger` 在走字节的同时数名字（文件 + 目录），worker 每轮把计数与上限下发给中介（`update_entry_limit`），中介对 `openat(O_CREAT|O_TMPFILE)`/`mkdirat`/`symlinkat`/`linkat` 在计数到顶时返回 **ENOSPC**，并给自己看到的创建/删除累加（`rm -rf cache && mkdir cache` 因此照常可用）；knob `E2B_DISK_MAX_ENTRIES`（默认 0 = 关，k8s overlay 取 500000 作失控兜底）。集群实测（临时把 knob 调到 200）：建 **198** 个后第 199 个条目 ENOSPC、总数 199 ≤ 200，删 20 再建 20 全成功；第一版让 **213** 个通过（锚点滞后），用与字节同一套 stamping 修掉后复测 198/199。修法 ②（把目录自身的 `st_size` 计入）与 ③（NAS `FileCountLimit`，被"不引云 API"否掉）**没做**。**可选修法**：① 把"条目数"也纳入账本（`dir_ledger` 已按目录保存文件集合，加一个计数列即可）并给每沙箱一个 `max_entries`，`mkdir`/`O_CREAT` 在超限时返回 ENOSPC；② 把**目录自身的 `st_size`** 计入（NFS 上就是真金白银的 16 KiB/目录）；③ 用 NAS 目录配额的 `FileCountLimit`（被"不引云 API"否掉，除非改口径）。未定优先级，暂无实现<br>**修法 ② 已落地（2026-09-21，`0.1.0-425-…-20260921-172405`，§22.5.12）——但口径被实测改了**：在这台 NAS 上**目录的 `st_size` 不是它占的空间**（空目录 4096、2000 条目 16384，而 `st_blocks×512` 与 `du -s` 全程 512，五档实测），按 `st_size` 计费反而**离 `du` 更远**，所以落成 **`st_blocks × 512`**（`brief_stat.directory_cost`；文件侧口径不变）。三处求和（`dir_ledger.scan_subtree` / `priv_helpers.dir_size` 两实现 / `health.py` 兜底）同一口径，`e2b-maint walk` 对目录输出分配大小并且**每目录只输出一次**（`FTS_D`+`FTS_DP` 原本打印两行）。**集群验收**：平台数 = 沙箱内独立测量 = `du -s -B1` = **33792**（含 40 个目录 + 4096 B 文件；做之前是 4096 vs 33792），逐字节相等且与 `du` diff 0。顺带修掉一个被这条改动照出来的真缺口：`note_local_write` 只标"写入路径的父目录"，而 `mkdir(parents=True)` 给每一层都加了名字 ⇒ 上层自身尺寸的变化没人重扫（实测少报 6 字节），现在从写入点一路标到账本根。修法 ③ 仍不做 |
| N32 | 运维/存储 | **✅ 已修（2026-09-22，同一轮做完定向测量）：重活期间的心跳断档 + 30 s 节点健康窗口 ⇒ 沙箱被判 orphaned（随后的快照 409）**（2026-09-22 实测，N29 验收时顺带抓到）。现象：反复做大快照（2000 文件的树、单次拷贝 76 s）期间，worker 心跳出现 **76–83 s 断档**（`deploy/scripts/heartbeat_gaps.py --since 40m`：82.72 / 82.15 / 78.43 / 77.07 s，节点 p99 也都在 76 s 以上），而 k8s overlay 的节点健康窗口是 **30 s** ⇒ 控制面日志出现 `node health sweep: orphaned sandboxes on e2b-worker-0/1`，该沙箱记录变成 **orphaned**，**紧接着的下一次快照直接 409「Sandbox … is not running」**（直连控制面的探针 `tmp/k0s/n29-idem-direct2.log`；同一轮里共享 base 只有 8 棵树、2.6 GB，所以不是"树多把 reconcile 轮次拖长"）。**未定位**：断档量级与拷贝耗时几乎相同，但阻塞路径有三种可能——拷贝把 NAS 打满导致心跳的 HTTP/账号侧变慢、reconcile 的磁盘轮次（§14.4 说已挪出心跳路径）、或冻结推送；**下一步是一次定向测量**：同一 base 上分别"只做拷贝、不做拷贝"各跑一段 `heartbeat_gaps.py`，把断档归因到具体路径；定论之后再决定是给窗口留余量（30 s → 按实测 3x 余量）还是把阻塞搬出心跳循环。**触发条件**：任何一次真实的长快照/大拷贝之后，控制面日志里出现 `node health sweep: orphaned` 就该做这条。<br>**定向测量与修法（2026-09-22）**：把三种猜想一次判掉 —— 打 `SANLOCK_EVENT_TRACE=1` 复做大快照，同时抓**控制面访问日志的任意行间隔**（不只是心跳行）：一次 2000 文件的快照让控制面日志**整段空白 76.1 s**（连 kubelet 的 `HEAD /` 都没有），而 `sandlock: wait_main: pidfd reported exit` 正常打印 ⇒ 既不是"拷贝把 NAS 打满让心跳变慢"，也不是 reconcile 轮次（§14.4 早就挪出去了），而是**控制面自己的事件循环被堵**：`POST /sandboxes/{id}/snapshots` 在 `async def` 里用了**同步** `httpx.post(..., timeout=120)` 等 worker 拷完。循环一恢复，`now − 最后一次心跳` = 一个拷贝时长 ⇒ `node health sweep: orphaned sandboxes on e2b-worker-1` ⇒ 该沙箱之后全是 **409 "Sandbox … is not running"**（N29 验收时看到的那批 409 就是这条，当时被记成"部署项"）。<br>**同一形状共 4 处**（都在共享 NAS 上做大树操作）：控制面 `POST /sandboxes/{id}/snapshots`（同步 httpx，76.1 s）、控制面 `DELETE /templates/{id}`（`shutil.rmtree` 共享 `_snapshots` payload，每个 **17.1 s**）、**worker** `DELETE /agent/sandboxes/{id}`（`_delete_sandbox_runtime` 同步删树，响应 17.4 s 后才出现、期间无心跳 ⇒ 21.6 s 空档）、**worker** `DELETE /agent/snapshots/{id}`（rmtree）。**修法两层**：① 一律 `await asyncio.to_thread(...)`（捕获/删除、`_delete_sandbox_runtime`、rmtree、volume 删除与 `volumecontent` 删除）；TTL 回调的节点删除从同步 `httpx.delete`（上限 30 s，与窗口同量级）改成 `AsyncClient`，`TTLSweeper` 会 await 它。② **判据侧加守卫**：`_node_health_loop` 记录自己一轮的起点，本轮**自己**落后 ≥ 心跳窗口就跳过这一轮的孤儿判决（那一刻队列里的心跳还没被处理，判"失联"必然误伤；下一轮照常判）。<br>**集群验收**（`0.1.0-437-g2e91607-20260922-171258`，N29 直连探针：2000 文件树 + 两次同键 + 一个新键 + 三次删除）：心跳最大空档 **77.2/78.3 s → 19.6/21.6 s（只修捕获）→ 5.03/5.05 s（全修）**；控制面日志最大行间隔 **76.1 s → 17 s → 0.94 s（>5 s 空档 0 次）**；`orphaned` 行 **1 → 0**，探针期间沙箱保持 `running`，同键重试 0.06 s 拿回同一 id、并发两条折叠成一个快照（N29 语义完好）。`heartbeat_gaps.py` 判词从 "headroom is thin（21.6 s = 窗口 72%）" 回到 **"15s covers this sample with 3x headroom"**。测试：`tests/unit/test_node_health_sweep.py`、`tests/unit/test_ttl.py::test_ttl_sweeper_awaits_an_async_callback`、`tests/contract/test_snapshots.py` 三条"慢 X 不堵循环"（控制面捕获 / 控制面删除 / worker 两个删除；均 RED→GREEN）。lanes：phase 1 **1694 passed**（基线 1688 + 新增 6）/ 6 skipped / 1 xfailed、phase 2 51 passed。文档：`docs/k8s-deployment.md` §14.5、`docs/build-test-deploy-pitfalls.md` D7 |
| N33 | fork/门禁 | **✅ 已修（2026-09-22，同一轮）：`test_chroot::test_chroot_magic_fd_symlink_resolves_to_child_fd` 在 tip 上确定性挂住，`core_integ` 因此跑不完**。证据：① 该用例单跑、空载、helper 健康、夹具干净（新命名）**每次都挂**，30–45 s 被手动 kill（`tmp/k0s/sigstop-logs/onetest-r*.log`）；② 同一份 tip 的**干净 worktree**（`/tmp/sl-tip`，不带本轮任何改动）上同样挂 ⇒ **不是本轮改动**；③ 挂住时进程表里**只有 `timeout` 与测试二进制**——15 s 内采样 227 次从未出现子进程或 supervise（`tmp/k0s/sigstop-logs/hang-sample-procs.log`），线程视图是主线程 `futex_wait` + 测试线程 `do_epoll_wait`；④ `SANLOCK_EVENT_TRACE=1` 的最后一行是 `sandlock-supervise: watch pid picked: notif=29 host=29 ns_map=false map_is_empty=true`（`crates/sandlock-core/src/chroot/dispatch.rs:905`，写 fd 监控入口），此后无任何输出。判读：这条用例是**唯一**用 magic link（`/tmp/errlog → /dev/stderr → /proc/self/fd/2`，且 `/dev` 走 `fs_mount`）触发写 fd 监控的形状；兄弟用例（`test_chroot_write_file` / `ls_root` / `lstat_describes_the_symlink`）同容器同镜像下都是 **0.02 s 绿**。所以故障在"写 fd 监控 → 完成通知"这条链上：子进程/supervise 消失，而父侧等待没有被唤醒（没有超时、也没有 EOF 路径把它叫醒）。**下一步**：① 先在 supervise 侧给写 fd 监控的失败路径留至少一条 stderr 证据，定"谁先死"；② 再决定是修监控路径，还是给父侧等待一个有界出口。**触发条件**：要跑完整 core_integ 门禁时（现在跑不完）。**根因（trace 定出来的）**：挂点**不在**等子进程 —— `SANLOCK_EVENT_TRACE=1` 的 trace 里 `sandlock: wait_main: pidfd reported exit` 已经打印（pidfd 正常报告子进程退出），挂的是**其后的收尾**。字节监控（`chroot/dispatch.rs::inject_watched`）把子进程**自己的 stderr 管道**（`/tmp/errlog → /dev/stderr → /proc/self/fd/2`）当成"有大小的文件"登记，并 `held` 了一份自己的 dup；capture 管道因此在该子进程退出后仍不 EOF，父侧的 drain 永远等不到，而那份 dup **只在 sandbox drop 时释放**——drop 又在那次 `wait()` 之后 ⇒ 自锁。库形态里没有 E2B worker 那种 100 ms tick 来把它丢掉，所以只有这条路会挂。<br>**修法**：`inject_watched` 只监控**有大小**的 open（新增 `holds_a_file_size`：`fstat` 判 `S_IFREG`；管道/socket/FIFO/字符设备一律走普通 `InjectFdSend`，但 `created_entry` 的**条目数记账保留**，N31 语义不变）。副作用修正：`/dev/null` 这类重定向不再进字节监控——它正是先前"空池瞬间给后续 fork 留下 1 MiB 地板"的来源。<br>**验收**：RED = 把分类变异成恒真，新增的有界用例在 **30 s 超时断言**上红（`tmp/k0s/sigstop-logs/mutant-bounded.log`，`Elapsed(())`）；GREEN = 原用例 + 新用例都 0.0x s 过，**整档 `core_integ` 545 passed / 0 failed / 85.84s**（`tmp/k0s/sigstop-logs/core_integ-green.log`）。新增用例 `a_magic_link_write_returns_instead_of_pinning_the_capture_pipe` 存在的意义就是让这条回归**红成断言而不是挂**。<br>**顺带（同一轮挖出的第二个挡门问题）**：core_integ 通过后，门禁继续走到 `cli_build`，撞出 N25/C 漏下的编译错误 —— `Req::RunExec` 的新字段 `max_file_size` 在 `sandlock-oci` 里 1 处生产 + 4 处测试字面量没跟上（`cli_build` 与 `--oci-root` 是唯一编译那个 crate 的地方 ⇒ N25 之后没人跑过这道门）。补 `None` 并让 `req_roundtrip` 断言其值。修完 fork 门禁**四相位全绿**（core_lib 891 / core_integ 545 / ffi 104 / cli 97 / supervise 51 / supervise_cost 3 / cli_build 0 / python 465、oci 157、supervise_root 4、mediation_2uid 9），基线表按绿跑刷新（`supervise` 43→51 是 N25 的既存漂移，一并补账）。**零字节 helper 那一层**（残留夹具 + `fs::copy` 截断同 inode 的共享 helper）另记 `docs/build-test-deploy-pitfalls.md` B12。<br>**E2B 侧验证（同一轮）**：fork 修的是**产品代码**，所以按约定重建了 wheel（`./deploy/scripts/build-sandlock-wheels.sh`，manifest HEAD=**`7b60349c`** = 该提交）与测试镜像（`deploy/scripts/build-test-image.sh`），再跑四条 lane：**phase 1 `1688 passed / 6 skipped / 1 xfailed`（415.50s）、phase 2（非 root worker）`51 passed / 1 skipped`（15.06s）**，rc=0，日志 `tmp/k0s/n33-lanes.log`（6 个 skip 与 1 个 xfail 都是清单里写明的形态要求/已知残余）。**未上线 → 已于同一晚上线**：这轮先停在 wheel + lane；随后做 N32 时按 tip 重新 `build-and-push.sh` + `apply.sh`，集群现在跑的 `0.1.0-437-g2e91607-20260922-171258` 的 worker 镜像就带着这条修复（manifest HEAD=`7b60349c` 的那份 wheel，即 N33 的提交）。 |
| N34 | fork/门禁 | **排查时必须知道：`test_restore::test_restore_glibc_vdso_program_resumes` 只在"测试二进制 stdio 是管道"时通过**（2026-09-22 复核）。这**不是新缺陷** —— `scripts/test-all.sh` 的 `run()` 早就为此把每条套件 `tee` 过匿名管道并写明理由（"checkpoint/restore reopens a checkpointed child's stdio by path … the restored process then cannot reopen it … and dies in the restore stub"）。本条记的是**绕过门禁直接跑**时的坑：输出重定向到**普通文件**（`> /logs/x.log 2>&1`，排查时最顺手的写法）会确定性红——`restore skipped fds` 只列 `fd 0`，断言报 `last seen 0, restored exit Ok(Code(10))`；换成**管道**（`\| tail -20`）同一条立刻**绿**（0.48–0.54 s，`restore skipped fds` 列出 `fd 0/1/2`）。**不是本轮改动**：把本轮四个文件拷进干净 worktree 重跑仍绿。含义：当 fd1/fd2 由**普通文件**背书时，恢复出来的进程不再推进计数器文件，而是立刻以 `Code(10)` 退出——要么产品在"该路径不可 reopen"时应退化成"跳过该 fd"（或挂 `/dev/null`）而不是让恢复进程早退，要么用例自己把 stdio 固定成可恢复的形状。**结论：`test-all.sh` 已经把这条处理掉了，直接跑二进制（尤其带 `--nocapture` + 文件重定向）的人才需要记住它 —— 撞到时不要当成产品回归去查**。另：把本轮四个文件拷进干净 worktree 后重跑仍是绿，所以也**不是本轮改动** |
| ~~N22~~ | E2B/运维 | ~~**一个再也不会回来的节点会永久留下记录**：`mark_orphaned`（E6.1）把断开节点的沙箱记录标成 `orphaned`，而 `_ttl_reapable` **故意**让 `orphaned`/`paused` 跳过 TTL 过期（worker 可能还握着那些 inode）⇒ 节点行、沙箱记录、以及共享 base 上对应的树都**没有回收路径**~~ | ✅ **2026-09-18 已修（`docs/k8s-deployment.md` §19）**，分两半。<br>**① 孤儿记录宽限（可选，默认关）**：新设置 `E2B_ORPHAN_RECORD_TTL`（秒，0 = 永不，即原行为）；记录新增持久化的 `orphaned_at`，**在状态翻转时**打戳（不是每次扫描刷新，否则宽限永不流逝），恢复时清戳（下一次断开重新计时）。默认关是刻意的：这是有数据损失的取舍（worker 在宽限后回来会发现沙箱已删），要开就取**远大于心跳窗口**的值（如 `86400`）。<br>**② 空节点行清理（无条件）**：unhealthy + 预留全 0 + 名下无记录的节点行，安静超过 10 个心跳窗口后从舰队视图里移除 —— 它不参与放置、不影响记账、也不影响 worker 的舰队枚举，纯属让运维多跳一行；这条不丢任何东西，也正是它收掉 pod-名时代残留的行。<br>单测覆盖「到期才回收 / 默认永不 / 恢复清戳 / 清理的三个保留条件」，全量 1325 passed；已随 `0.1.0-350-g212850d-20260918-152008` 上两套栈 |
**本轮已完成（2026-09-16，供追溯）**：seccomp 收敛到"默认档 + 2 条补白"并上线；并发容量
512MB/8 并发（`366e5dc`）；契约按 `E2B_DEFAULT_MEMORY_MB` 取值 + lane 透传（`d6b7270`）；
MCP 网关 `MALLOC_ARENA_MAX=1`（`d5114a2`，箱内 MCP server 上限 110 → 300 MiB）；
netns 灰度暴露 MCP 入站每请求 +390 ms，根因是 readiness 合成，fork 侧 `net_bind_inject`
（`fe492be`）+ E2B 接线（`15d4726`）修掉，实测 375.9 → 29.1 ms；netns 全量 + 撤 compose
低端口 sysctl（`97ad404`）；k8s seccomp 安装器（`d818896`）；worker seccomp 启动自检
（`f559014`）；pid_ns 评估与开关（`3461e36`）。

## 剩余工作

1. 上线前：`wheels/fork` 重建 + 镜像重建推 ACR —— ✅ **全部完成**（2026-09-16：wheel 按
   fork `5b16855` 重建、镜像推 ACR 两轮 —— netns 全量 + pid_ns 灰度/全量，见
   `tmp/build-push-pidns.log`）
2. 远程约束**已解禁并已用起来**（2026-09-16）：E1.2/E8.1 目标机部署与远程复测 ✅ 完成；
   仍剩需要维护/部署窗口的 O1（prjquota）、O2（TLS 代理层）、O3（凭据管理），以及随 O1
   一起做的 T1（真实 XFS/ext4 上复测沙箱文件属主，去掉那条带证据的 skip）；
3. 不需要环境就能做的：~~T3（快照展开自嵌套守卫）~~ ✅ 已完成（G2，2026-09-06，见上表行）；
   T2 已随 fork P3 完成；~~#22 候选补丁（协议缺陷）~~ ✅ 已完成（F15，见 #23）。

## M4 收口后的 open follow-ups（Task 11 登记，2026-09-06；G3 收口 2026-09-06）

1. **FUP 远程 pause/resume 投递**（Task 5 登记）: ✅ 已关闭（G1a + G1a review，
   2026-09-06；commit `7a98755` + `aa844b7`）：control plane 对非 `local://` 节点
   push pause/resume（`_push_pause_state` / `_push_evicted_pause`，agent 新增
   pause/resume 路由），显式非 404 拒绝回滚状态并 502、transport loss 保持既有
   best-effort caveat；SDK pause/connect 在 multinode 下真正投到 worker。
   契约 `tests/contract/test_pause_resume_sandlock_multinode.py` + 单测
   `tests/unit/test_remote_pause_delivery.py`；证据
   `tmp/sdd/g1-remote-pause-report.md`。
2. **FUP fork F11：argv-safety freeze × 多线程进程树**（Task 8/FUP-E3 登记）: ✅ 已关闭
   （2026-09-06；fork 本地提交 `edd8c76` fix + `927d015` docs/门禁收口，未推送；wheel 已按
   927d015 重建并同步 `wheels/fork/`）。修法 = exec 冻结前把 ProcessIndex keys 归一化为
   唯一 TGID，每线程组只冻结一次；**线程 tid 懒登记本身保留**（fork F11 报告 concern #1）：
   属 fork 内部建模细节，本次在冻结侧归一化、行为面最小；若后续要彻底消除"一个 TGID 多
   key"，另做 fork FUP（改登记策略，需逐消费点复核）——残余随本行登记。E2B 侧复跑与契约
   见 `tmp/sdd/f11-e2b-integration-report.md`（探针日志 `tmp/perf/f11-gateway-probe-450-450-50*.log`；
   契约 commit `7685126`，`tests/contract/test_memory_quota_gateway_command.py`）。
3. **FUP 网关 ledger headroom**（Task 8）: ✅ 已关闭（FUP #3，2026-09-06）：per-sandbox
   默认内存 512→1024 MiB（`E2B_DEFAULT_MEMORY_MB`）给网关 ledger（~250–330M）与
   450M MCP server 目标留出空间；fork F11（row 2）落地后，FUP-E3 gateway+命令变体在
   1 GiB 箱复跑全绿：gateway+450M MCP server 可达（`list_tools == ['echo']`）、网关后
   命令 exit 0（stdout `post-gateway-ok\n`）、第二 450M 命令精确拒绝（exit 137 /
   stdout `''` / stderr ∈ {"", "Killed\n"} / error None）、450+50 控制命令 exit 0 且网关
   持续服务、record `memoryMB == 1024`。thread-tid-keying 残余登记随 row 2。
4. **FUP 网关启动失败 SDK 可见性**（Task 10/11 → Task D1）: ✅ 已关闭（2026-09-11，
   决定 ④"SDK 要能看到"）—— watcher 把早期非 0 退出记成 typed `McpGatewayFailure`
   （文本 `mcp gateway failed to start sandbox_id=… port=… exit_code=… stderr=…` 逐字，
   `envd_service/runtime/context.py`），命令路径在首次 exec **之前** replay 该记录：
   `commands.run` 的整串 stderr = 记录原文、退出码 = 网关自己的非 0 码；`/mcp` 以同一
   文本 503（`envd_service/http/mcp.py`）。判定只认这个 typed 记录，不做子串/宽 except
   分类。契约 `tests/contract/test_mcp_gateway_failure.py`（建箱 → 调用 → exit_code != 0
   + stderr 与记录逐字相等 + MCP 503 同文），单测 `tests/unit/test_mcp_gateway.py`
   两条（watcher 记录、命令路径 replay）。证据：`tmp/d1-red-contract.log`（RED，SDK 侧
   `exit_code=0 stderr=''`）、`tmp/d1-green-mcp.log`（41 passed，含 netns 契约）、
   `tmp/d1-green-netns.log`、`tmp/d1-template-isolation.log`、
   `tmp/d1-prod-shaped-full.log`（`PROD_DROP_CAPS=SYS_ADMIN` 全量 1124 passed / 3 known
   skips / 0 failed）。
5. **FUP T5 xfail 摘除 + reason 清理**: 🟡 executor 接线完成、T5 已摘（2026-09-09：
   **executor 已全面走
   supervise** ⇒ `tests/contract/test_uid_permissions.py` 的 strict xfail **已摘**，
   chroot 形态容器实跑 4 passed，日志里两个沙箱分别租到 uid 20000/20001 的
   `sandlock-supervise` 槽位）。新增开关 `E2B_ROUTE_B=auto|on|off` +
   `E2B_ROUTE_B_SLOTS`（auto 档只在 root worker + per-sandbox uid + chroot 形态 +
   wheel 带 supervise 时启用；强开而前置不满足 ⇒ 建箱报错，不静默退 route-A）。
   执行面证据三份契约：`tests/contract/test_route_b_executor.py`（executor↔槽位
   端到端：子进程 uid、文件属主 + 自 chmod、**停车 M0 零 CPU**、PTY 尺寸、
   SIGSTOP 真停 + 信号退出码 -1、close 后 uid 干净可复用）、
   `test_route_b_slot_pool.py`（跨 uid 卷保护）、`test_uid_permissions.py`（T5 本体）。
   实现期推翻的两条设计：停车程序**不能**用 `read x < /dev/zero`（exec 会话 M0
   stdio 被 core 固定为 /dev/null ⇒ 死循环跑满一核），改成自 `kill -STOP $$`；
   槽位**必须按沙箱自己的 host uid 定向租用**（换 uid 就连自己 workspace 都进不去）。
   其余 5 条约束（串行 accept、先 bind 后 launch、scratch 目录可遍历性 +
   文档 0440 root:uid、`SandboxError ⊂ SandlockError` 的捕获顺序、token 走 argv）
   见 `docs/superpowers/plans/2026-09-09-envd-route-b-wiring.md` 文末表。
   ⚠️ 其中「token 走 argv」一条本块初版的判断被实测推翻，已更正：
   `/proc/<pid>/cmdline` 是 0444 且**不受 ptrace 门约束**（只有 `environ` 0400 被挡），
   实测 foreign uid 21501 直接读出 21500 槽位的 `--token <64hex>` ⇒ **是暴露面**；
   今天不构成攻击面只因为 registered 路径先查 `SO_PEERCRED` ∈ `--peer-uid` 再查 token
   （实测非白名单 uid **带正确 token** 也被静默关连接，连沙箱自己的 uid 都被拒）。
   登记 fork 侧 **SL-10**；同一轮实测抓到 **SL-9**（F16 Python 客户端错误分支抛
   `AttributeError`，`_take_err_msg` 收到 `ctypes.byref(...)` 却取 `.contents`）。
   ✅ **两条都已闭（2026-09-09，fork F17 `c0f7bf5`）**：选了「给语言面补 transport 1
   （fd handoff）」这条路 —— C ABI 加 `sandlock_supervise_connect_fd` /
   `sandlock_supervise_check_fd` / `sandlock_supervise_set_timeout`（request 签名不变、
   按 handle 分派），Python `SuperviseChannel(fd=...)`；持久单流在 Rust 侧串行 + 失败退役会话；
   FFI 动态符号 159→162。envd 默认 `E2B_ROUTE_B_TRANSPORT=fd` ⇒ **槽位 argv 里再无 token、
   `/tmp` 里再无注册 socket**，并白得「worker 崩溃 ⇒ 槽位按 EOF 自收口」的生命周期保证
   （契约 `test_worker_death_ends_the_generation`）。老 wheel 没有该符号时 `auto` 退回进程内、
   强开则报错点名重建 wheel —— 不静默改用 token 进 argv 的 registered 形态。
   顺带修掉一处**发布链陷阱**：`deploy/scripts/build-sandlock-wheels.sh` 跑的是
   F2b.5 之前的旧配方，产出的 wheel **不含 `sandlock/bin/sandlock-supervise`** 且退出码 0
   （实测 2.2 MB vs 7.4 MB），route-B 会因此静默失效；现在该脚本委托 fork 的
   `python/build-wheels.sh`（同批 cross-build supervise + 注入 + HEAD 指纹 manifest +
   缺件即错），旧 Dockerfile 标 SUPERSEDED。
   ✅ 三档门禁已在终态树上复跑全绿：gate A（chroot，base=python-mcp:3.14，
   concurrency=2）`1048 passed / 2 skipped / 0 failed`（`tmp/rb-gate-a2.log`；
   本轮早先一次 1047/2/0 见 `tmp/rb-gate-a.log`，差额就是新增的那条单槽位并发契约）、
   gate B（pure）`1046 passed / 3 skipped / 0 failed`（`tmp/rb-gate-b.log`）、
   macOS `972 passed / 74 skipped / 0 failed`（`tmp/rb-macos2.log`）；route-B 专题切片
   （槽位池 + executor 契约 + T5 + 两份单测）容器实跑 `64 passed`（`tmp/rb-focused.log`）。
   首次命令延迟代价已实测：租槽位只发生在**每沙箱第一条命令**上
   （in-process 10.63 ms → route-B 57.11 ms，+46 ms；warm p50 4.29 → 4.35 ms 无差），
   证据 `tmp/perf/route-b-first-exec.txt`。
   ✅ **`E2B_PER_SANDBOX_UID` 已改为部署默认（2026-09-09 晚）**：有特权的 worker 从此
   自动拿到 per-sandbox host uid ⇒ chroot 形态的 route-B 槽位也自动生效；非 root worker
   仍自动缩退到 E5.1 固定身份 + 一条 WARNING（现网 compose/k8s worker 行为因此不变）。
   部署要求见 `docs/production-deployment-requirements.md` §2.4（池容量 = 并发沙箱上限、
   每沙箱多一棵不计入内存上限的 supervise 树）+ compose 注释。
   ⚠️ 实测暴露**第 7 条语义差异待决策**：route-B 沙箱**内**不再是 root（in-process 是
   ns 内 root，宿主侧同为一个 X）；要保持就得让 fork 侧槽位自 `unshare` + 写 `0 X 1`。
   ✅ **guest root 对齐（fork F18）+ 非特权测试 lane（2026-09-10）**：route-B 槽位现在自映射
   `0 -> euid`，客体内恢复 uid 0（与进程内后端一致；探不到非特权 userns 就不建 ns，权限只少
   不多，形态经 `stats.guest_uid` 回报），in-ns `CAP_MKNOD` 换来的设备节点由 seccomp 按
   `S_IFBLK`/`S_IFCHR` 类型位拒（`mkfifo` 仍可用）。
   ⚠️ 顺带挖出一条被 `--privileged` 掩盖的生产要求：**进程内 `RunAs` 需要 `CAP_SYS_PTRACE`**
   （写别人进程的 `uid_map` 除 `CAP_SETUID` 外还要 ptrace 访问权；实测只补该 cap 即通），
   而 **route B 一条 cap 都不需要** —— 这是选它的第二个理由。worker 启动会探测并 WARNING
   （`PER_UID_NO_PTRACE_WARNING`）；k8s/compose 若要 root + E3.2 + 非 chroot 形态必须声明它。
   新增 `deploy/scripts/test-prod-shaped.sh`：`--cap-drop ALL` + 部署等价 capset、无
   `--privileged`，**958 passed / 2 skipped / 0 failed**（只有 XFS prjquota 的 7 个文件因容器内
   造不出 loop 而显式 ignore，strict skips 仍开）。终态门禁：gate A 1061/3/0、gate B 1060/4/0、
   macOS 982/78/0，fork 11 档全 matches baseline（python 455→461、core_lib 841→842）。
   ✅ **删 `mediation_run_as='supervisor'` 降级档（2026-09-10）**：前置（per-sandbox uid
   成部署默认）已满足，`_mediation_run_as()` 与两处 ceiling 里的该键删除，
   `_route_b_selected()` ⇒ `_route_b_decline_reason()`（单一决策点，日志原文引用）。
   后果：root worker + chroot 形态拿不到槽位 ⇒ fork 拒绝建箱（不再静默留 T5），
   executor 在建箱前打一条 ERROR 说清原因与修法；非 root worker 的中介即沙箱自己的
   euid，不构成拒绝（也不会误报）。容器实测
   `tests/security/test_template_isolation.py`（两条真槽位 + 一条钉住拒绝与对照组）。
   进度留痕（2026-09-09 早）：fork F16
   ✅、**W1 槽位管理器 ✅（`envd_service/route_b.py` + 契约
   `tests/contract/test_route_b_slot_pool.py`：两个不同 uid 槽位经 SuperviseChannel
   exec，X 建文件属主 X + 自 chmod、Y rm/chmod EPERM —— envd 侧 T5 证据已立）**；剩余
   = executor 全面接线（设计见 `docs/superpowers/plans/2026-09-09-envd-route-b-wiring.md`
   「接线 Task」）——
   fork 侧前置 **F16 已完成（2026-09-08）**：`sandlock_supervise_connect/request/free`
   C ABI + Python `SuperviseChannel`（含 exec 的 SCM_RIGHTS stdio 交接；fork `1159525`/
   `6571c36`、主仓 `f8c4020`，见 #24）。路线：envd 接线与 route-B supervise 部署
   （supervise 进程 euid == 沙箱 host uid，**W1 已按 2026-09-04 决策采用**；注意
   `sun_path` 108 字节与「一 uid = 一代沙箱，复用需重启」两条约束），最后摘除
   `tests/contract/test_uid_permissions.py` 的 strict xfail 并回归 T5；
   `mediation_run_as='supervisor'` 降级档与 WARN/`mediation_downgrades` 计数随之移除
   （两者均已于 2026-09-10 完成，见本条开头与 #5 ②）。W1 已定（2026-09-04 决策；窗口 =
   同时在世槽数 N），W2（换 uid 重启、窗口 = 段大小）为可选升级，不阻塞。
6. **FUP pure-shape workspace 属主对齐**（Task 11 gate B 首跑暴露，确未修）: ✅ 已关闭
   （G2，2026-09-06）：无 base image
   的 pure-sandlock 沙箱（root worker + 共享 uid）无法 shell 写入 workspace 根目录
   （root:root 0755 vs sandbox host uid 1000），migration 三用例在其首个命令即红
   （`test_migrate_*_between_workers` / `failure_restores_source_runtime` /
   `shared_workspace_skips_transfer`）。同 3 条在 M4 前置 commit `4f34e55` 同一 pure
   形态同样失败（`tmp/m4-bisect-t1-pure.log`），pre-M4 基线 `tmp/e2b-base-20260906.log`
   亦含同族 migration 失败 ⇒ 非 M4 回归。修法 = `envd_service/uid_pool.py` 新增
   `align_shared_uid_workspace`（root worker + 属主为 root 时整树 chown 到 legacy 共享
   RunAs uid 1000 + 0700，复用 `apply_sandbox_ownership` 语义；不 blanket-chmod 0777、
   不动共享卷 slice），接入 agent create / agent import / 本地 provision / 本地 fork 四处
   provision 缝；per-sandbox uid 路径（`host_uid`）原样保留。回归：pure-shape 契约
   `tests/contract/test_pure_shape_workspace_ownership.py`（shell 写 workspace 根 +
   migration 跨 worker 保留 + 导出 tar 属主 == 1000）+ gate B migration trio +
   macOS 单测；门禁日志 `tmp/g2-*.log`，报告 `tmp/sdd/g2-ownership-snapshot-report.md`。
7. **FUP 远程 network update 显式拒绝判定**（Ohm review 登记）: ✅ 已关闭（G1b，
   2026-09-06；commit `8ac02a7`）：`_remote_network_decision` 三态化——worker 显式
   ≥400（404/409/500…）均为"拒绝"并在 persist 前抛错（409→409，其余→502，record
   不变）；仅 transport loss / missing node / 非决策 3xx 保留 best-effort
   persist + WARNING caveat。单测 `tests/unit/test_control_plane_network_remote.py`。
8. **FUP pause/resume killpg-fallback 语义**（Ohm review 登记）: ✅ 已关闭（G1b，
   2026-09-06；commit `84e807f`）：`RunningProcess.supports_signal_pause` 标记——
   sandlock 后端（`SandlockRunningProcess`）为 False，killpg 失败时 fail-loud-skip
   （WARNING 点名 pid + 命令），不再走会实际 SIGKILL 的 direct-signal 兜底。
   单测 `tests/unit/test_process_pause_fallback.py`。
9. **FUP rpc drift 告警节流**（Ohm review 登记）: ✅ 已关闭（G1b，2026-09-06；
   commit `710ddd2`）：worker 侧 drift WARNING 按沙箱 60s 窗口节流
   （`DRIFT_WARN_THROTTLE_SECONDS` + per-sandbox `_drift_warned_at`）。单测
   `tests/unit/test_rpc_drift_throttle.py`。
10. **FUP `_instance_network_snapshot` 死状态**（Ohm review 登记）: ✅ 已关闭（G1b，
    2026-09-06；commit `beff30f`）：删除 executor 只写不读的
    `_instance_network_snapshot` 状态与赋值/清理；D4=A ratchet 只读
    `_applied_state()`。
11. **FUP bisect 证据日志头纪律**（Ohm review 登记）: ✅ 已登记约定（2026-09-08，
    无代码修复计划）——容器/宿主复现与 bisect 日志必须带环境头（commit、env、
    镜像/loop、时间）便于跨会话归因；本轮 F15/F16 所有门禁/探针日志已按此执行
    （ENV-HEADER 首行）。
12. **FUP worker 侧 409 无 egress 探针断言**（Task 4 Minor 登记，随 final review
    入 backlog 可见）: ✅ 已关闭（G3 final wave，2026-09-06；test commit
    `0e15572`）：`tests/security/test_network_enforcement.py` 新增
    `test_rejected_rewiden_leaves_worker_runtime_copy_narrowed`——IP-literal
    allowOut 建箱 → launch → 放行探针 exit 0 → 收窄 [] 204（探针拒绝）→ 放宽 409
    且 record 与收窄态相等 → 新命令再探仍拒绝，证明 worker 运行时副本未被 409 改回；
    目的地址 = 198.18.0.99 loopback 别名 + 本地 RecordingOrigin（NET_ADMIN 门控
    与 `test_fork_network_features` 一致），断言全精确、无 substring。
13. **G2 评审登记：本地 snapshot fork × per-sandbox-uid uid 分配缺口**（2026-09-06）:
    ✅ 已关闭（2026-09-08）：`control_plane/api/snapshots.py` 本地 fork 分支删掉
    重复内联 provision，改为直接调用 `_provision_local`（create 同路径）——per-sandbox
    uid 档从此 acquire/apply/commit `host_uid`、register 带 `host_uid`/volume_projects/
    mcp/network/iam（I3 失败 release 保留）；legacy 无池档保持共享 uid 对齐。单测
    `tests/unit/test_provision_local_uid.py`（acquire→ownership→register(host_uid)→commit
    + 失败 release 两条）。
14. **G2 评审登记：快照剪枝启发边界风险**（2026-09-06）: ✅ 已关闭（2026-09-08）——
    `_holds_snapshots` 改有界递归（深度 3），更深层嵌入
    且 marker 完好的存储（`cache/mirror/snap_X/...`）整容器剪除；marker 缺失/改名与
    权限/竞态余量保留为文档化边界（不自身再造指数链，仍可能携带存储字节）。单测
    `tests/unit/test_snapshot_registry.py::test_nested_store_markers_are_pruned_within_bounded_depth`
    + 既有同名普通目录正例保持。
15. **F12（fork，2026-09-06 已列入主要计划）: ProcessIndex 一 TGID 一 entry** —
    ✅ fork 侧完成（2026-09-07；fork 本地提交 `68e7e84` fix + `194ffed` docs，
    未推送；报告 `third_party/sandlock/tmp/sdd/f12-report.md`）。来源：F11 report concern #1（row #2 的
    thread-tid-keying 残余）升级为独立 fork 计划。问题：`register_pid_if_new`
    对发出被中介 syscall 的非 leader 线程以 tid 懒登记独立 entry，一个 TGID 可
    多 key；F11 只在 freeze 侧归一化。完整修法 = 线程通知一律路由到 TGID leader
    entry、删除 per-tid 登记、逐消费点复核（freeze/内存记账/cwd/exit/枚举），
    RED 先行。fork 详细计划与审计清单：
    `third_party/sandlock/docs/fork-plan-2026-09-f12.md`；
    followups 登记 `third_party/sandlock/docs/fork-plan-followups.md`（A 节 F12）。
    收口流程：fork 实现 + 门禁 + wheel → E2B 指针 bump + thread/gateway 探针 +
    full gate A/B 复跑（沿用 F11 的 E2B 接线模式）。**F12 侧结果**：核心登记
    归一化到 TGID leader + 查询面 leader 解析 + freeze 归一化保留为防御；
    core_lib 823→827（+4 unit）、core_integ 533 不变（F11 argv-safety 回归保持
    绿）；pidfd leader watcher 的组退出语义 C 探针实证。fork 全门禁绿 +
    wheel（4d5f385）双架构重建 verify 全绿。E2B 真栈 thread 探针（新 wheel +
    重建 e2b-sandlock-test）GREEN：线程化 python 存活时后续 exec exit 0 /
    `b-ok\n`（`tmp/perf/f14-thread-probe.log`）。**E2B 下一波完成
    （2026-09-07）**：FUP-E3 gateway+命令变体 pure 探针 4/4 GREEN
    （`tmp/perf/f14-gateway-probe-450-450-50{,-run2,-run3,-run4}.log`）、
    gateway+命令与 boxed 契约 pure 各 2 轮全绿（`tmp/f14-e2b-contract-*.log`）、
    full gate A 982/2skip/1xfail(T5)/0（`tmp/f14-e2b-gate-a.log`）、full gate B
    982/3skip/0（`tmp/f14-e2b-gate-b.log`）、macOS 916/65skip/0
    （`tmp/f14-e2b-macos.log`）；gate A/B 需
    `E2B_MAX_CONCURRENT_COMMANDS_PER_SANDBOX=2`（漏参首轮 boxed 队列超时，非回归；
    聚焦复跑 `tmp/f14-e2b-boxed-gateA-focused.log`）。
16. **fork C 类设计项评估（2026-09-06）**: 已评估（fork docs
    `third_party/sandlock/docs/fork-c-class-design-assessment.md`，fork 提交
    `9d60058`）。结论：FUP-22（non-root-but-CAP_SETUID launcher gate）→ 立项，
    随 route-B ③ launcher 部署前完成（高优先，安全 gate）；FUP-05（目录挂载点
    rmdir 语义）+ FUP-04（link 直击）→ fs 收尾小批立项（中优先）；FUP-19
    （per-child fs/bind 强制）、FUP-20（credential per-child 归因）、FUP-21
    （port-aware update_network）→ 候补（触发式，E2B/产品当前无需求；F12 完成
    后再评估与 pid 穿透设计合并）。
17. **fork F13（已排入计划，2026-09-06）: fs 写家族挂载保护收尾** —
    ✅ fork 侧完成（2026-09-07；fork 本地提交 `4576615` fix + `6a8cec1` docs；
    报告 `third_party/sandlock/tmp/sdd/f13-report.md`）。
    范围：FUP-04 `link()` 于 rw 挂载点直击 + 断言精度收敛；FUP-05 目录挂载点
    `rmdir` 语义（与真实 bind-mount 一致返回 EBUSY，禁止删除活动挂载点）；
    文档 §3.1 注记随实现闭环。fork 计划：
    `third_party/sandlock/docs/fork-plan-2026-09-f13.md`。**F13 侧结果**：
    chroot dispatch 对 `unlinkat(AT_REMOVEDIR)` 命中目录 mount leaf 返回 EBUSY
    （宿主目录不再可被沙箱 rmdir 删除；文件/chardev leaf 回落 ENOTDIR）；ffi
    98→100（+2 测试：link 直击 pin + 目录 rmdir EBUSY）；RED 先证「沙箱 rmdir
    删除空宿主目录」（`tmp/sdd/f13-red-rmdir.log`）；fork 全门禁绿；wheel 随
    F14 最终 tip（4d5f385）统一重建 verify 全绿；E2B 复跑 2026-09-07 完成
    （见 #15）。
18. **fork F14（已排入计划，2026-09-06）: capability-aware 特权 remap gate** —
    ✅ fork 侧完成（2026-09-07；fork 本地提交 `ba6963e` fix + `4d5f385` docs；
    报告 `third_party/sandlock/tmp/sdd/f14-report.md`；route-B ③ 部署前必须
    完成的 gate 已就位）。范围：
    把 C 档 gate 从 `euid==0` 升级为 effective-capability 判定（CAP_SETUID/
    SETGID 即使 euid 非 0 也 fail-closed 拒绝），RED 用 capability 夹具模拟，
    不引入真实 file-cap 部署；supervise B 档交接不受影响。fork 计划：
    `third_party/sandlock/docs/fork-plan-2026-09-f14.md`。**F14 侧结果**：
    `privileged_userns` 分类与 C 档 gate 升级为 capability-aware（CapEff 含
    CAP_SETUID|CAP_SETGID 即特权跨 uid remap）；默认 caller 档对 euid 非 0 +
    caps 的调用方（file-cap launcher 形态）以点名能力的新消息建箱前拒绝，不再
    落到暗示无 caps 的晚拒；root 行为/消息不变。core_lib 827→828（+1 纯决策
    单测）、mediation_2uid 8→9（setcap eip + setpriv 65533 真执行夹具）；
    fork 全门禁绿；wheel（4d5f385）双架构重建 verify 全绿；E2B 复跑
    2026-09-07 完成（见 #15）。
19. **A/B cleanup 剩余任务收口（2026-09-07）**: ✅ 完成。计划
    `docs/superpowers/plans/2026-09-07-ab-cleanup-remaining.md`（Task 0–5 全走完，
    未推送）。fork 侧最后一个 open 代码项 FUP-11 六子项全关（1a supervise 全部
    error-path `contains` 断言转整行/整串；1b `FORBIDDEN_RUNTIME_MEDIATOR_REMAP`
    获得常量原文 + CLI flag 面测试引用；1c registered slot 异常连接日志改
    `AbnormalEndLog` 节流＝首条点名 + 每 256 条一条带累计数；1d harness
    120 s connect 与 30 s verb I/O 不对称提为命名常量 + 取舍注释 + 契约单测；
    1e 非 root registered path stats settle 补 `proc_count_vs_live == 0`；1f
    validate-and-exit × `--program` 审计＝模式仍在并补 3 例）。fork 计数
    supervise 36→42 / supervise_root 3→4，其余档不变；fork 非 root 8 档 + root
    三档全绿。wheel：最终 tip 重建 + verify 全绿（FFI 156=156 双向、RECORD 精确、
    mode 755、三方指纹、`--uid` 冒烟），体积 10.4/9.5 → 8.3/7.4 MB（FUP-15
    `panic=abort`+`strip`）；**本波修掉 verify 自身假失败**（无 `unzip` 时
    `python3 -m zipfile -e` 不还原 mode，正确 wheel 也被判 0644 红）⇒ 改以 wheel
    中央目录记录的 mode 为权威。docs 提交后重钉 manifest，重跑构建产物逐字节一致
    ⇒ fork HEAD == manifest HEAD == 子模块指针 == `ee66234`（三次重建的 wheel/supervise
    sha256 逐个相同 ⇒ 文档提交不动产物）。E2B：镜像重建后
    pip 真机落 0755 + 镜像内 supervise sha256 = manifest（FUP-16 遗留项闭环）；
    thread 探针 GREEN；**gate A 982/2skip/1xfail/0failed、gate B 982/3skip/0、
    macOS 916/65skip/0**（与上一波逐项一致，无漂移）。CHANGELOG 补记前波漏写的
    A/B 行为条目（FUP-01/03/07/10/11c/14/15/16/17）。
19b. **修复回合（2026-09-08）**: ✅ 完成（**并被「修复回合 2」取代**：FUP-23 已按根因
    修复、FUP-14 回退撤销，终态 fork `e045881` / wheel `d5cab47` 产物，见本文顶部 ⚡ 块与 #22）
    —— 上一行的「探针不可复现」曾定性为**真实回归**（#22 = fork FUP-23），并以 fork
    `bb1cb42` 回退 FUP-14 作为缓解：
    回退后同一探针 N=0 场景 `FAILURES: []`（五项签名逐字回归）。终态
    fork HEAD == wheel manifest HEAD == 子模块指针 == `0770e59`（`d9b379c`→`0770e59`
    仅文档增量，重建产物四份 sha256 相同 ⇒ 证据成立）。复跑：fork 非 root 8 档 +
    root 三档全绿、wheel verify 全绿、**gate A 982/2skip/1xfail(T5)/0 failed**、
    **gate B 982/3skip/0**、**macOS 916/65skip/0**、thread 探针 GREEN、
    gateway 探针 4/4（日志 `tmp/f23-*`、`tmp/perf/f23-gateway-probe-run*.log`、
    fork `tmp/sdd/f23-*`）。FUP-14 的 ≈19× exec 往返收益随回退撤回，
    `supervise_cost` 预算回 200/300/2000 ms。
20. **OCI 镜像解析对「坏镜像源」无防御（2026-09-07）**: ✅ 已修复（2026-09-08，
    E2B 侧）——三处全落地：
    ① `oci_registry.py` `blob()` 下载后按请求 digest 校验 sha256，不匹配 ⇒
    `RegistryError(retryable=True)` 走下一个 endpoint，坏层绝不进 rootfs；
    ② blob 拉取用独立超时（`blob_timeout=600s`，`RegistryClient` 新参数），
    30 s 请求预算只管 manifest/交互往返；`tests/conftest.py` buildkitd mirror
    改从 `E2B_REGISTRY_MIRRORS` 的 docker.io 桶取值（无配置才回落 daocloud），
    不再硬编码单源；
    ③ challenge 后仅在 `_authorization()` 非 None 时才写 Authorization 头，
    匿名 + Basic-only mirror 不再因 `TypeError` 打断整次拉取（走既有
    fall-through）。
    单测：`tests/unit/test_oci_registry.py`（blob digest mismatch retryable +
    anonymous Basic challenge 不写 None 头，18 passed）。
    原始描述（2026-09-07 观测）：
   本轮公共 Docker Hub 源整体劣化，暴露三处：
    ① `envd_service/runtime/oci_registry.py:352` `blob()` 直接返回 `.content`，
    **不校验层 digest** ⇒ 镜像源交付截断/错误层时被静默解进 rootfs，症状漂移到
    远端（gate A r3 出现 `sandlock child: execvp '/bin/echo': No such file or
    directory`；同一用例在换源后 0.09 s 内即 exit=1/120、沙箱内连
    `/tmp` 都写不出来）。建议：下载后按 manifest 里的 digest 校验 + 不匹配即
    按可重试错误走下一个 endpoint。
    ② 客户端请求预算 30 s（`RegistryClient(timeout=30.0)`）对慢源必输：实测
    daocloud 拉 29.8 MB 层 37.2 s（0.8 MB/s）⇒ 必然 `ReadTimeout`；而
    `tests/conftest.py:193` 给 buildkitd 的 mirror 硬编码 daocloud，于是
    `test_template_build_and_create_sandbox` 也变成同源抖动（单跑 282 s 过、
    全量档内偶发 `buildkit build exited with code 1`）。建议：blob 拉取用独立
    （更大或按体积伸缩的）超时，且 buildkitd mirror 跟随 `E2B_REGISTRY_MIRRORS`。
    ③ `_request_one` 在 challenge 之后无条件
    `headers["Authorization"] = self._authorization()`（`oci_registry.py:262`），
    匿名拉取遇到只给 Basic challenge 的 mirror 时该值为 `None` ⇒ httpx 抛
    `TypeError: Header value must be str or bytes, not NoneType`，而
    `_request` 的镜像源 fall-through 只捕 `RegistryError` ⇒ 一个坏源直接打断
    整次拉取而不是换下一个源（实测两个候选源触发）。
    本轮规避：本地 `registry:2` 作镜像源（`127.0.0.1:5080`，预置 amd64
    `library/python` 3.11/3.12/3.14-slim + `library/node:22-slim`）后三档全绿；
    注：404 按既有语义不重试，故本地源必须**镜像全集**（缺 `python:3.12-slim`
    时 `test_create_with_template_image` 直接 503）。
21. **`tmp/f11_fup3_probe.py` 独立脚本形态不可复现（2026-09-07）**: ✅ **已结案
    （2026-09-08，根因与修复见 #22；本条保留现象与已排除项作追溯）**。同一镜像、同一 pure 形态下，
    该脚本自建的 harness 里
    **任何写 stdout 的命令**都拿 `exit=120`/`stdout=''`（`/bin/echo x` → 1、
    `echo hi > /tmp/o.txt` → 2、沙箱内文件写不出来），而入库契约
    `tests/contract/test_memory_quota_gateway_command.py`（断言同样五项：
    `['echo']`、网关后命令 exit 0 + `post-gateway-ok\n`、450M 超卖 137、
    50M 控制命令 0、`memoryMB==1024`）在同一 wheel 下 pure 与 image-rootfs
    两形态均绿、并包含在 gate A/B 全档内。已排除：镜像源/rootfs 缓存（隔离
    `tmp/sandboxes/_images/python_3.11-slim-*` 与改用干净 `E2B_IMAGE_CACHE_DIR`
    均不变）、harness 根目录所在文件系统、worker 数、`E2B_MAX_CONCURRENT_COMMANDS_PER_SANDBOX`、
    `PYTEST_*` 环境变量、`e2b` 模块解析路径、SDK 调用形态（`commands.run(cmd,
    timeout=60)` 逐字一致）、以及把脚本 `main()` 塞进 pytest 里跑（同样红）。
    **决定性对照（`tmp/f11_fdcount_probe.py`，同一脚本同一环境，只在客户端进程
    里多开 N 个 `/dev/null` fd）**：`N=0` ⇒ `FAILURES: [trivial exit=120, control
    exit=120]`；`N=8 / 24 / 64` ⇒ `FAILURES: []` 且 trivial exit 0 /
    `post-gateway-ok\n`。⇒ 不是脚本"接线"问题，而是**客户端 fd 表越空、沙箱
    stdio 管道落到的 fd 号越低 ⇒ exec 装配越容易接错**（见 #22）。结论不变：
    FUP-E3 gateway+命令变体的验证由入库契约 + 两形态门禁承担（探针 4 轮均红已如实留档
    `tmp/perf/f11-gateway-probe-450-450-50-run{1..4}.log`）。
    **2026-09-08 结案**：本条现象 = #22 的回归，非「脚本接线问题」；回退 FUP-14 后
    同一脚本 `FAILURES: []`（`tmp/perf/f23-gateway-probe-run{1..4}.log`），本条关闭。
22. **exec stdio 装配存在低 fd 号依赖（2026-09-07，P1，fork 侧）**: ✅ **已修复
    （2026-09-08，fork `880a1ec`；wheel = `d5cab47` 产物、指针 = fork HEAD `e045881`）**
    —— 根因：fork 后、子进程第一条指令前，**外部方**把一条无关管道装进新生子进程的低号位，
    顶掉了 init 刚收到的某个 stdio 端；SCM_RIGHTS 收发链路经六点身份快照（`(O_ACCMODE,
    st_dev, st_ino)`）证明清白，「多开 1 个 fd 即恢复」只是把损坏槽位从 stdout 挪到 stderr。
    修法 = init fork 前把三端搬到保留号段 64+（保留号不全空则整体退回，绝不覆盖他人描述符）
     + 子进程装配前逐槽校验身份、被换端即以退出码 **124** 明确失败（不再静默丢输出）。
    验证：`tmp/f11_fdcount_probe.py` N=0/1/2/8 全部 `FAILURES: []`、gateway 探针 4/4、
    thread 探针 GREEN、gate A/B（982/2/1xfail/0、982/3/0）、macOS 916/65/0、
    fork 非 root 8 档 + root 三档全绿。FUP-14（signalfd 事件化 reap，exec 往返
    p50 101.75 → 5.35 ms）的回退同时撤销。细节见 fork
    `docs/fork-plan-followups.md`「FUP-23 根因闭环与修复」与本文顶部 ⚡ 修复回合 2。
    历史（缓解阶段，保留备查）：触发方（fork FUP-14 `7671240` 的 signalfd）一度以
    `bb1cb42` 回退，当时 wheel `0770e59` 不带该用户可见故障。现象（#21 的对照实验）：pure 形态下 SDK 命令
    的 stdout 在「客户端 fd 表很空」时丢失/写错（python 因 stdout flush 失败退出
    120、`/bin/echo` 退 1、`> /tmp/x` 退 2），多开 8 个 fd 即消失。
    代码面：`third_party/sandlock/crates/sandlock-core/src/init/mod.rs:167-178`
    的子进程 stdio 装配是裸循环

    ```rust
    for (i, &fd) in fds.iter().enumerate() { libc::dup2(fd, i as i32); }
    for &fd in &fds { if fd > 2 { libc::close(fd); } }
    ```

    既不排除「某个源 fd 恰号等于另一路的 dup2 目标（0/1/2）」，也不检查 `dup2`
    返回值；而同一仓库的进程内 spawn 路径早已为此写过防护
    （`sandbox.rs:2496-2514` + `:3376` 注释：「先把每一端搬到一个 ≥3、与 0/1/2
    不相交的 fd，再 dup2 下来」）。SCM_RIGHTS 收到的描述符号 = 内核挑的空隙号，
    低 fd 表下正好落进危险区，故症状随客户端 fd 数漂移。
    观察与机制（同一次 N 扫描）：子进程确实起来了（python 跑到 flush 才失败 ⇒
    exit 120），且 `/bin/echo x` 以「写错误」退 1 ⇒ 子进程的 **fd 1 被接到了管道
    的读端**（`write(1)` ⇒ EBADF），即上面那条循环把兄弟流的另一端 dup2 覆盖了；
    `stderr` 通路在同一布局下也受影响（`echo hi >&2` 退 1）。N=0（下一个可用
    fd=3）必现，N≥1（≥4）全部正常 ⇒ 只要客户端 fd 表把三端挤到 0/1/2 邻域就翻车。
    **收窄对照（`tmp/f11_direct_exec_probe.py`，同一镜像同一 wheel，N=0）**：不经
    in-process 控制面/网关/SDK，直接 `SandlockExecutor.start()` 跑
    `python3 -c "print('direct-ok')"` ⇒ `{"exit": 0, "stdout": "direct-ok\n"}`
    ——**低 fd 布局下直接执行器路线是好的**。所以缺陷面收窄为
    「in-process harness（uvicorn 线程 + 控制面 + SDK 流式回传）+ 网关 + 低 fd 表」
    的组合路径，而不是裸 exec stdio 装配单独出错；但 `.so` A/B 又确实证明
    fork 侧改动是触发方（旧 tip 绿 / `7671240` 红）⇒ 修复会话要从
    **「FUP-14 让 init 多占一个低位 fd（signalfd）后，谁在依赖 fd 号假设」**
    入手（fork `docs/fork-plan-followups.md` FUP-23 待补这条对照数据）。
    为何本轮才暴露：FUP-14 给 init 增加了 signalfd，改变了低段 fd 的占用情况，
    使「三端正好落在会被覆盖的位置」成为可能（潜伏缺陷被编号位移触发，
    非 FUP-11 引入）。
    待办：① fork 侧 RED（可控 fd 表把 stdio 三端钉到 0/1/2 邻域，断言子进程
    stdout 精确到达且三端互不串流），②按 `sandbox.rs` 同款「先搬到与 0/1/2
    不相交的高 fd，再 dup2 下来」修 init 路径，并把 `dup2`/`close` 的返回值
    处理一并收紧（异步信号安全前提下失败即 `_exit` 点名），③修完重跑 fork 门禁 +
    wheel + E2B 三档，并把 #21 的 N=0 对照并入回归门。
    本轮未修：不在 A/B 计划范围内，且需要完整重跑一轮 fork + wheel + E2B 门禁。
23. **F15（fork，2026-09-08，已排入计划后完成）: 控制帧按声明归属描述符
    （`FRAME_VERSION` 1 → 2）** — ✅ fork 侧完成（fork 提交 `c50f407` RED + `8640223` fix +
    `3020ea0` docs；主仓指针 `018afdd`；wheel `3020ea0` 产物 = 子模块指针 = manifest HEAD）。
    来源：#22 末段「协议缺陷（候选补丁存档）」升级为独立计划（`docs/superpowers/plans/
    2026-09-08-sandlock-fork-remaining.md`）。问题：init 控制通道是 SOCK_STREAM，一次
    `recvmsg` 可并入多帧，而 SCM_RIGHTS 描述符是**一条拼接列表**；旧实现把「本读单元
    全部 fd」当「本帧的 fd」，前一帧带 fd 时后一帧 stdio 整体位移（exec #2 输出进 exec #1
    的管道并整条丢失）；且不检查 `MSG_CTRUNC`/`MSG_TRUNC`。修法 = 帧头 1 字节 `n_fds` +
    纯函数 `take_frame_fds` 按声明切队列（不符 ⇒ 整读单元拒绝）+ 截断 fail-closed。
    **F15 侧结果**：core_lib 837→841（+4 fd_assignment）、root 档 oci 145→150
    （+2 init.rs 头校验 lib+bin 双编译 +1 两帧一次写出六端各归其主 integration）；fork
    11 档门禁全绿、wheel 双架构重建 verify 全绿（156=156 / RECORD / 三方指纹 / 0755 /
    `--uid` 冒烟）；E2B 复跑 2026-09-08 完成：低 fd 表探针 N=0/1/2/8 全 `FAILURES: []` +
    多探针全绿、网关+boxed 契约 2 轮全绿、**gate A 982/2skip/1xfail(T5)/0**、**gate B
    982/3skip/0**、**macOS 916/65skip/0**。wire 升级约束（同批替换 supervise 与 wheel，
    混装 fail-closed 点名版本）见 fork `docs/e2b-integration.md` §7。环境注记：本机
    容器 pid-1 不再回收孤儿 ⇒ 本轮 fork 门禁统一加 `--init` 跑（红档 `f15-gate-nonroot-r1.log`）。
24. **F16（fork，2026-09-08，已排入计划后完成）: route-B worker 侧语言客户端
    （T5 的真前置）** — ✅ fork 侧完成（fork `1159525` feat + `6571c36` docs/python；
    主仓指针 `f8c4020`；wheel `6571c36` 产物）。新增 C ABI
    `sandlock_supervise_connect/request/free`（`exec` 三端 stdio 随帧 SCM_RIGHTS）
    + Python `sandlock.supervise.SuperviseChannel`；FFI 符号 156→159。证据：
    `mediation_2uid` 9→10（Python 客户端驱动两个不同 uid 的 registered slot，
    per-uid 卷保护三断言精确成立 = T5 所缺的 Python 可达证据）、python 档 454→455
    （同 uid exec-with-fds 往返）、ffi 100→101（C smoke 编译+链接+失败路径契约）。
    fork 11 档门禁全绿 + wheel 双架构 verify 全绿（159=159）+ E2B 三档无漂移
    （gate A 982/2/1xfail(T5)/0、gate B 982/3/0、macOS 916/65/0）。**剩余 = E2B
    route-B supervise **部署**（W1/W2 槽位模型；`sun_path` 108 与「一 uid = 一代沙箱」
    两条约束）**。其余三项（envd 接线、摘 T5 xfail、删 `mediation_run_as='supervisor'`
    降级档）已于 2026-09-10 全部完成 ⇒ 见 #5 与 #25：部署侧还差 wheel 与 uid 段两件事，
    线上已实测核过。**2026-09-11 B3（SL-1 硬删）**：fork 侧把该档连字段一并删除
    （含 FFI 导出与 `stats()` 计数，导出符号 164→163），拒绝文本变为
    `in-process path mediation refused: … Run sandlock-supervise as uid <N> (route B)`；
    `wheels/fork/` 当时换成 B3 构建（HEAD `4b4012b`）；2026-09-11 终态收口 `b51fd0d`
    已把 wheel 重钉到 fork tip `a063daf`（三方 supervise 指纹一致，当前
    `SHA256SUMS.supervise` 的 `# HEAD=a063dafe6835d4cf3cfdd259d4c1b1156f54df30`），
    四条硬前置见 `docs/production-deployment-requirements.md` §2.4。

25. **route-B 特权最小集 + 共享卷 bind 的退化缺口（2026-09-10 实测；2026-09-11 A7 收口）**:
   ✅ **已收口：无 `SYS_ADMIN` 可用** —— worker 侧不再需要它，共享卷也不再是保留它的理由。
   特权最小集实测并写进 `docs/production-deployment-requirements.md` §2.4.1 与计划
   `2026-09-09-envd-route-b-wiring.md` 检查表第 10 条：沙箱侧
   `SETUID`+`SETGID`+`CHOWN`；`DAC_OVERRIDE` 属管理面（对账 `os.walk`、删除 `rmtree`、
   配额扫描要穿租户 0700 目录，摘掉即 `EACCES`）；**`SYS_ADMIN` 不是 route-B 前置**，
   worker 侧三处用途已全部迁出（见下）；`SYS_PTRACE` 只服务进程内 `RunAs`。对照实验与
   「槽位进程 `CapEff=0` ⇒ worker 的 cap 不会顺着中介漏进租户路径」的取证在 HANDOFF 同名块。
   ✅ **三处改动（A4 / A5 / A6）**：① 共享卷 `mount --bind` 进 workspace：**A4** 删掉 bind
   （卷视图 = 请求路径决定的符号链接，双别名 `/workspace/<rel>` + `/home/user/<rel>`），
   **A5** 补齐卷根及祖先对租户 uid 的 `o+x` 穿透位（§2.4.2）⇒ 原来「无 `SYS_ADMIN` 就
   EACCES」的跨 uid 读写缺口不再成立；② 直接执行 `xfs_quota -x`：**A6** 改由服务端
   **quota-agent** 提供（worker 只发 HTTP，`E2B_QUOTA_AGENT_URL` 即开关，`SYS_ADMIN` 只留在
   `profiles: ["quota"]` 的 agent 服务上）；③ 写 namespaced sysctl
   （`ip_unprivileged_port_start`）：**A6** 改由容器 spec 声明（compose `sysctls:`）。
   ✅ **证据日志**：`tmp/a4-final-step4-run{1,2,3}.log`（A4/A5 契约 36 条 ×3，**无
   `SYS_ADMIN`** 三连绿）、`tmp/a6-agent.log`（agent 形态 `107 passed`）、
   `tmp/a6-full-gate.log`、以及 A7 的**无 `SYS_ADMIN` 全量门禁**
   `tmp/a7-nosa.log` = `1075 passed, 3 skipped, 0 failed`（对照改造前基线
   `tmp/nosa-full.log` = `4 failed, 962 passed, 3 skipped`；那 4 条正是 A4/A5 修掉的共享卷
   用例）。门禁固化入口：`PROD_DROP_CAPS=SYS_ADMIN ./deploy/scripts/test-prod-shaped.sh`
   （cap 探针 `CapEff 0xa02c35fb → 0xa00c35fb`，SYS_ADMIN 位已清）。
   ⬜ **线上部署前置（审计结论，非代码缺口）**：现网 worker 实际是 root（远端清单没有
   `user:` 行、旧镜像也没有 `USER`），但**镜像里的 wheel 没有 route-B 语言面**
   （`sandlock_supervise_connect_fd` = False、缺 `sandlock-supervise`），且两个 worker
   共用同一 `sandbox-shared` 卷却**都没设不重叠的 uid 段** ⇒ 升级前必须先用新 wheel
   `build-and-push`，并配 `E2B_UID_POOL_START/SIZE`。逐条数据见 HANDOFF
   「特权最小集实测 + 线上就绪审计」。

26. **卷记录与沙箱记录共用 `e2b:record:` 命名空间（2026-09-12 线上实测；当日修复上线）**:
   ✅ **已修复**（commit `1b990f5`；修复镜像 tag `0.1.0-230-g8b01839-20260912-153947`）。
   **根因**：`RedisRecordStore` 把记录写成 `e2b:record:<id>`，而沙箱注册表
   （`registry/manager.py`）与卷注册表（`registry/volumes.py`）**用同一个 namespace**
   （都是 `"e2b"`）。沙箱侧的 `list()` / `tenant_usage()` / `remove_expired()` 都遍历
   `_record_store.keys()` 并把每个 payload 当沙箱记录解析，卷记录没有 `template_id`
   ⇒ `SandboxRecord.from_storage_dict` 抛 `KeyError`，只捕 `UnknownSandboxError` 的
   调用方全部穿透。
   **影响面（线上实测，非纸面）**：只要线上存在**任意一个卷**（非 tombstone），
   ① `GET /sandboxes`、`GET /v2/sandboxes`、`GET /internal/tenants` 返回 **500**；
   ② TTL 回收线程（`registry/ttl.py`，1s 间隔）每次扫描都抛
   `KeyError: 'template_id'` ⇒ **过期沙箱永不被自动回收**（容量/配额静默泄漏），
   显式 kill 不受影响。旧镜像（`0.1.0-20260830-191728`）里是同一段代码
   （已直读旧镜像文件确认）⇒ 不是本次升级引入，只是旧部署没有卷记录所以从未触发；
   回滚并不能修复它。
   **修法**：**保留 key 形态、读取侧按类型过滤 + 容忍缺字段**（不需要清空 Redis、
   不需要迁移）。沙箱/卷记录各自写 `kind` 标签；`manager._is_sandbox_record_payload`
   对**没有标签的旧记录**退回用 `sandbox_id`/`volume_id` 形状判定，
   `get()`/枚举路径对不可解析的记录记名跳过而不是抛出。
   **证据日志**：缺陷复现 `tmp/c2c3-33-defect1-repro.log`、因果实验（建卷→500+计数增长、
   销毁→200+冻结）`tmp/c2c3-35-defect1-verify.log`、本地 RED→GREEN
   `tests/unit/test_record_namespace_isolation.py`（列表/租户用量/TTL 三条路径 ×
   只有卷/只有沙箱/两者都有/旧格式 四种组合 + 两个列表端点 + sweeper），
   线上复验 `tmp/c2c3-46-c21-regress.log`（有卷时三端点 200、短 TTL 沙箱
   ~6s 被回收、sweeper 0 失败 / 0 KeyError）。

27. **非 root worker 上卷根 `chmod 1777` 必 EPERM（2026-09-12 线上实测；当日修复上线）**:
   ✅ **已修复**（commit `8b01839`；同一 tag `0.1.0-230-g8b01839-20260912-153947`）。
   **根因**：`envd_service/volumes.py::_ensure_shared_volume_root` 先
   `_chown_path`（经 `e2b-maint` broker 把卷根交给首个挂载沙箱的 uid）**再**
   `os.chmod(root, 0o1777)`；chown 之后 worker 既不是属主也没有 `CAP_FOWNER`，
   chmod 必然 EPERM——与 c1 工作区修复里「chmod 必须先于 chown」是同一条纪律。
   **影响面**：每个卷 × 每个 worker 一条
   `cannot apply shared perms to volume root …: [Errno 1] Operation not permitted`
   WARNING（日志噪声）；**实测无功能影响**——卷根由控制面以 root 创建时就已经是
   `1777`（`registry/volumes.py:191`），两个不同 uid 的沙箱仍都能写入同一卷。
   **修法**：chmod 提到 chown 之前、且仅在 mode ≠ 1777 时才尝试；失败降级为 DEBUG，
   只有**最终** mode 仍不对才 WARNING（best-effort 语义不变，噪声消失、
   真故障仍会点名）。
   **证据日志**：线上 WARNING 取证 `tmp/c2c3-36-defect2-verify.log`、
   权限结果实测（两个 uid 都能写、卷根 1777）`tmp/c2c3-37-defect2-uid.log`、
   本地 RED→GREEN `tests/unit/test_shared_volume_traversal.py`
   （模拟非 root worker：chown 之后的 chmod 是 EPERM，钉住顺序与"已 1777 不吭声"），
   上线后复验 `tmp/c2c3-53-volume-perms-online.log` + `tmp/c2c3-54-volume-warn-recheck.log`
   （挂载后 WARNING 计数 0）。

28. **控制面/清单侧三条 Minor 收口（W4，2026-09-13）**:
   ✅ 三条都已落地（本地提交，未推送；判定与证据见
   `.superpowers/sdd/task-w4-controlplane-report.md`）：
   ① **合体节点配额硬编码 `via_agent=False`** ⇒ **判定为缺陷**：`control_plane/api/sandboxes.py`
   两处（provision + destroy/GC release）改为跟随 envd 的开关 —— `E2B_QUOTA_AGENT_URL` 存在即
   agent 形态，否则 `E2B_QUOTA_VIA_AGENT`（默认 false）；开关本身直接问
   `envd_service.config.Settings.quota_via_agent`（`control_plane.config.local_node_quota_via_agent()`），
   不复制规则。合并镜像不跑 `envd_service.app.create_app`，所以合成节点在启动时
   （`E2B_ENABLE_LOCAL_NODE` 且开关开）用 `configure_quota_agent_client` 把 hooks 接上 ——
   否则「配好了 agent」的合体节点仍会被报成 quota-agent not configured 并静默降级。
   ② **控制面卷根缺祖先穿透位** ⇒ 新增
   `control_plane/registry/volumes.py::_widen_ancestors_for_tenant_uids`，复用 A5 的
   `envd_service.volumes._ensure_traversable`（同一份实现、不复制），在 `VolumeRegistry.create`
   建卷根、`chmod 1777` **之前**跑一次：合体节点自己建卷根、不走 worker 的挂载半边，
   0700 祖先会让租户 uid 连绝对卷路径都 EACCES（§2.4.2）。
   ③ **k8s 无 quota-agent 清单** ⇒ **判定为口径（降级），不新增清单**：k8s 形态默认无
   per-sandbox 硬限 + 一条 WARNING（建箱/挂卷照常），需要硬限时把 `E2B_QUOTA_AGENT_URL`
   指向集群内/外的自备 agent；后果清单与「为什么不随清单发特权 agent（未验证的新部署面）」
   写进 `docs/production-deployment-requirements.md` §2.4.4，`deploy/k8s/worker.yaml` 的 A6
   注释块指向该节。
   **证据**：RED `tmp/w4-red.log`（10 failed）、GREEN `tmp/w4-green.log`（13 passed）、
   `tests/unit` 失败集合逐条 diff 为空（`tmp/w4-unit-baseline-wip.log` vs
   `tmp/w4-unit-final.log`）、`tests/contract` 31 failed / 187 passed / 41 skipped 且集合一致
   （`tmp/w4-contract-baseline-r2.log`、`tmp/w4-contract-final.log`）。
