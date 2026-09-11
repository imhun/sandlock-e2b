# 任务总清单（路线图）

汇总 2026-08-31 ~ 09-02 分析产生的待办。**状态最后更新：2026-09-08（F15 收口；
FUP-01/07/09/10/15/17 台账关闭，见 fork `docs/fork-plan-followups.md`）**
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

| E10 | 每沙箱一实例（fork §8，M0–M4）：三层拆分 + E2B 接线。含 Q10 风险：`max_processes` 从『每命令 64』变『整箱 64』，落地时必须同步上调默认值并写变更说明；另含 Q6 网络策略语义、Q7 控制目录身份、Q8 泄漏回收。**安全前置（fork §7 M0′）未清零前不得在 envd 开 `exec`**；M4 第一步只做「网关+命令」半合并 | ✅ 完成（fork 侧 M0–M4/F0–F10 在子模块 b955ae9；E2B 接线 5d38537（Task 0.5 supervisor 档）→ 4f34e55/e0f5507/6de41db/05f349f/cb36b7a/f64d7ab/19dc1f5/f337724/4149a1f/4d617b9/3bf5d0e/883d38d/f67a6b9 → Task 11 收口；全量门禁见 HANDOFF。剩余 follow-ups 见下方「M4 收口后的 open follow-ups」） | `5d38537`…`f67a6b9`（+Task 11） |
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
| T4 | net_isolation + 镜像 rootfs(chroot) 形态下 MCP 入站端口映射起不来（纯 sandlock 形态 3/3 通过） | ✅ 已关闭（Task 10，FUP-E1）：根因 = envd 侧 base-image 组成（slim rootfs 无 mcp-gateway，ENOENT exit 2），非 fork；改用 MCP-capable 基镜像 `python-mcp:3.14`（deploy/docker/Dockerfile.mcp-base）后 chroot+netns MCP 契约两形态 3/3 绿；xfail 已摘 | `883d38d` `f67a6b9` |
| T5 | chroot 形态共享卷写入经 supervisor 归属（fs_denied 代打开路径），per-uid 卷保护无法还原 | ✅ 关闭（2026-09-10）：三步全落。① fork F16/F17（worker 侧 C ABI + Python `SuperviseChannel`，含 fd 交接）；② E2B envd 全面接线 route B（W1 槽位模型，chroot 形态默认档）；③ strict xfail 已摘 + **`mediation_run_as='supervisor'` 降级档已删**（那条组合现在由 fork 拒绝建箱，E2B 不再请求）。证据：契约 `test_uid_permissions`（两 uid 槽位 20000/20001 各自属主/自 chmod/跨 uid EPERM）、`test_route_b_slot_pool` 的 token 非暴露面证明、`test_template_isolation` 两条真槽位 chroot + 一条钉住拒绝。fork §3.1 的「fail-closed 现状」段仍待重写（#25 ②），与本项无关 | — |
| T1 | 真实 XFS/ext4 目标机上复测沙箱文件属主：① 沙箱能否 `chmod` 自己写的文件（本机 EPERM）；② 共享卷 1777+sticky 的跨 uid 保护是否真生效（本机 A 写的文件宿主属主是 uid 0，而沙箱 host_uid 是 20000） | ⬜ 待环境（两条用例已改为带证据跳过，不再靠巧合通过） | — |
| T6 | 内存/CPU/进程配额按实例而非按沙箱 ⇒ 超卖（默认 K=2 实测 1.76x），放大为节点超卖 | ✅ 已定方案：改为**每沙箱一个 sandlock 实例**（fork 文档 §8，取代 P10 共享资源组） | — |
| T2 | `third_party/sandlock`：`_HANDLED_FIELDS` 登记 `notify_rate_limit`，消掉假告警 | ✅ 完成（fork P3：`17ee48d` fix + `fad056a` doc，子模块 b955ae9 内） | `17ee48d` |
| T3 | 复现并修 `SnapshotRegistry.expand_to` 快照自嵌套（`snap_X/fs/snap_X/fs/...`） | ✅ 已落地（G2，2026-09-06）：`create_from_sandbox`/`expand_to` 复制前拒绝"目标落在源之内"（`ValueError`），并用 ignore 回调剪掉工作区里嵌入的快照存储根（只剪最外层，普通同名目录保留）；用例 `tests/unit/test_snapshot_registry.py` 3 条 + snapshot 契约回归全绿；设计见 `docs/superpowers/plans/2026-09-04-sandlock-remaining-goals.md` Task 0.2 | 见 git log（fix(snapshots) commit） |

## 运维侧任务

| # | 任务 | 状态 |
|---|---|---|
| O1 | 目标机启用 XFS `prjquota`（fstab + 在线 remount，维护窗口） | 未开始（E2 生产验证前置；本地已用 losetup/XFS 实测） |
| O2 | TLS 证书/代理层配置（代码侧 E1.4 已完成） | 未开始（需部署窗口） |
| O3 | 凭据管理（ACR/API key/redis/SSH 上密钥管理） | 未开始（E5.4 已提供 master key 轮换能力） |

## 剩余工作

1. 上线前：`wheels/fork` 重建（E7 最终 tip）+ 镜像重建推 ACR —— ✅ 代码/产物侧已完成
   （F15 终态 fork `3020ea0` / wheel `3020ea0` 产物，见 #23）；**仍剩 ACR 推送**（受
   "不推送远程"约束暂缓，Task 11）；
2. 用户解除"不做远程部署"约束后：O1（prjquota）、E1.2/E8.1 目标机部署与远程复测、O2/O3；
   T1（真实 XFS/ext4 上复测沙箱文件属主，去掉那条带证据的 skip）随 O1 一起做；
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
4. **FUP 网关启动失败 SDK 可见性**（Task 10/11）: ⬜ open（产品决策，未定）——
   Task 11 已落地 ERROR 日志（sandbox_id/port/stderr/exit text，
   `envd_service/runtime/context.py` watcher），SDK 仍按契约先收 exit-0；SDK 可见的
   错误上抛是未来产品决策，未定。
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
21. **`tmp/f11_fup3_probe.py` 独立脚本形态不可复现（2026-09-07）**: ⬜ open
    （**已定性，见 #22**；本条保留现象与已排除项）。同一镜像、同一 pure 形态下，
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
    `wheels/fork/` 已换成 B3 构建（三方 supervise 指纹一致，HEAD `4b4012b`＝
    `SHA256SUMS.supervise` 的 `# HEAD=4b4012bc85a8ec…`），
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
