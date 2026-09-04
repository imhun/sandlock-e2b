# 任务总清单（路线图）

汇总 2026-08-31 ~ 09-02 分析产生的待办。**状态最后更新：2026-09-02（E8.3 测试环境清零）**
（细粒度执行记录见 `.superpowers/sdd/progress.md`，两阶段总路线见
`docs/superpowers/plans/2026-09-01-sandlock-e2b-completion-roadmap.md`）。

约束提醒：本期"不推送远程" = 不做目标机远程部署（`upgrade.sh` / 远程复测暂缓），
ACR 镜像推送照常，git 远程推送暂缓。

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
| S1.2 | 独立 uid 的 userns 单 entry 约束验证 | ✅ 完成（结论：非 root supervisor 无法映射任意 host uid，每沙箱独立 uid 需 root/CAP_SETUID → E3.2/E5.1 输入） |
| S1.3 | 无特权运行固化（uid 65534 全绿） | ✅ 完成（基线写入 HANDOFF） |
| S2.1 | connect handler 宿主建连 + ADDFD 注入 | ✅ 完成（已知限制：getsockname/getpeername 显示宿主地址、非阻塞 connect 无 EINPROGRESS） |
| S2.2 | spawn 路径 `CLONE_NEWNET` + `lo up` | ✅ 完成（netns 沙箱 netlink 为合成 loopback-only 视图） |
| S2.3 | DNS 网关进沙箱 netns + 通配域名回归 | ✅ 完成 |
| S2.4 | UDP（connected 注入 / datagram on-behalf） | ✅ 完成（datagram 单向代发） |
| S2.5 | 入站端口映射（MCP 场景） | ✅ 完成（host 映射端口强制 50005+；poll/epoll 可读性合成随 E7.1 完成） |
| S2.6 | 全特性矩阵回归 + wheel 重建 | ✅ 完成（lib 788 / integration 465 / python 430） |
| S3.1 | cp314 双架构 wheel + 冒烟 | ✅ 完成（cp310-313 扩列本期不做） |
| S3.2 | 私有源/镜像安装切换 | ✅ 完成（Dockerfile 按 ABI+ARCH 从 `wheels/fork` 安装） |
| S3.3 | 上游 PR 分支整理 | ✅ 完成（推送按约束暂缓） |

| E10 | 每沙箱一实例（fork §8，M0–M4）：三层拆分 + E2B 接线。含 Q10 风险：`max_processes` 从『每命令 64』变『整箱 64』，落地时必须同步上调默认值并写变更说明；另含 Q6 网络策略语义、Q7 控制目录身份、Q8 泄漏回收。**安全前置（fork §7 M0′：SL-4/SL-5 +组级 kill/freeze 拆 per-child + `proc_count` 对账）未清零前不得在 envd 开 `exec`**；M4 第一步只做「网关+命令」半合并 | ⬜ 未开始（等 fork M0 落地） | — |
| SL-1 | 路径中介（USER_NOTIF）以 supervisor 身份执行 `openat/unlinkat/fchmodat/...`：沙箱文件属主变 root、`chmod` 失效、共享目录 per-uid 保护不成立 | ⬜ 待修（正文+复现+修法在 `third_party/sandlock/docs/e2b-integration.md` §3.1/§2 P1；`gh` 不可用 + token 只读，暂无法直接开上游 issue） |：`wheels/fork` 需按 E7 最终 sandlock tip 重建（`scripts/build-sandlock-wheels.sh`），
镜像内 wheel 与 fork 提交一致后才能推 ACR。

## E2B 服务端侧任务（阶段二）

| # | 任务 | 状态 | 关键提交 |
|---|---|---|---|
| E1.1 | 构建 + 推送 ACR 双架构镜像 | ✅ 完成 | `0.1.0-9-g9ed0f00-20260902-013319` |
| E1.2 | 部署验证 | ⏸ 改为本地 compose 冒烟（远程部署按约束暂缓） | `917395b` `3b8f8af` |
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
| E8.1 | 部署后远程 smoke 回归 | ⏸ 受"不做远程部署"约束暂缓 | — |
| E8.2 | 本地测试基线确认 + HANDOFF/backlog 更新 | ✅ 完成（Linux 容器全量 28 failed / 804 passed / 17 skipped / 6 errors，219.63s；macOS unit+contract 11 failed / 689 passed / 23 skipped / 33 errors，36.91s；详见 HANDOFF「验证命令与基线」） | — |
| E8.3 | 测试环境失败清零（把 E8.2 的"环境类失败"逐条定根因） | ✅ 完成（Linux 容器全量 **0 failed / 0 error**，843 passed / 18 skipped；macOS 全量（含 sdk python/js + security）**0 failed**，804 passed / 53 skipped；顺带修掉 2 个产品缺陷（模板镜像切换未落盘、无 registry 构建产物无法解析） | `ac59152` `d8b7f41` `87874a0` `0a235b4` `22e5acc` |
| E8.4 | 公共镜像不直连 Docker Hub：`E2B_REGISTRY_MIRRORS` + 凭据按 host 作用域 + harness 存储改容器原生盘 + 清掉一条假 skip | ✅ 完成（默认形态 851 passed / 18 skipped；image-rootfs 形态 73 failed+28 errors → 853 passed / 16 skipped；两形态 0 failed。18 条 skip 的分组与跑法见 HANDOFF「容器全量剩下的 skip」） | `a6f74e4` `3d7cc79` `08a21dc` |
| E8.5 | 把"能跑却在跳"的用例真正跑起来：镜像自带 XFS prjquota/npm/netns/双形态 + `E2B_TEST_STRICT_SKIPS` 能力型 skip 直接判失败 | ✅ 完成（全开跑见下；顺带修掉 fs_denied 废掉 per-uid 隔离、lsattr 缺失导致孤儿只报不清） | 本次提交 |
| T4 | net_isolation + 镜像 rootfs(chroot) 形态下 MCP 入站端口映射起不来（纯 sandlock 形态 3/3 通过） | ⬜ 新发现，已用 strict xfail 跟踪 | — |
| T5 | chroot 形态共享卷写入经 supervisor 归属（fs_denied 代打开路径），per-uid 卷保护无法成立 | ⬜ 即上游 **SL-1**（notif 代执行未切 caller 身份），本仓库 strict xfail 跟踪；正文见 `third_party/sandlock/docs/e2b-integration.md` §3.1 | — |
| T1 | 真实 XFS/ext4 目标机上复测沙箱文件属主：① 沙箱能否 `chmod` 自己写的文件（本机 EPERM）；② 共享卷 1777+sticky 的跨 uid 保护是否真生效（本机 A 写的文件宿主属主是 uid 0，而沙箱 host_uid 是 20000） | ⬜ 待环境（两条用例已改为带证据跳过，不再靠巧合通过） | — |
| T6 | 内存/CPU/进程配额按实例而非按沙箱 ⇒ 超卖（默认 K=2 实测 1.76x），放大为节点超卖 | ✅ 已定方案：改为**每沙箱一个 sandlock 实例**（fork 文档 §8，取代 P10 共享资源组） | — |
| T2 | `third_party/sandlock`：`_HANDLED_FIELDS` 登记 `notify_rate_limit`，消掉假告警 | ⬜ 待做（一行） | — |
| T3 | 复现并修 `SnapshotRegistry.expand_to` 快照自嵌套（`snap_X/fs/snap_X/fs/...`） | ⬜ 新发现，无用例覆盖 | — |

## 运维侧任务

| # | 任务 | 状态 |
|---|---|---|
| O1 | 目标机启用 XFS `prjquota`（fstab + 在线 remount，维护窗口） | 未开始（E2 生产验证前置；本地已用 losetup/XFS 实测） |
| O2 | TLS 证书/代理层配置（代码侧 E1.4 已完成） | 未开始（需部署窗口） |
| O3 | 凭据管理（ACR/API key/redis/SSH 上密钥管理） | 未开始（E5.4 已提供 master key 轮换能力） |

## 剩余工作

1. 上线前：`wheels/fork` 重建（E7 最终 tip）+ 镜像重建推 ACR；
2. 用户解除"不做远程部署"约束后：O1（prjquota）、E1.2/E8.1 目标机部署与远程复测、O2/O3；
   T1（真实 XFS/ext4 上复测沙箱文件属主，去掉那条带证据的 skip）随 O1 一起做；
3. 不需要环境就能做的：T2（fork 里 `_HANDLED_FIELDS` 一行）、T3（快照展开自嵌套，
   先写复现用例）。
