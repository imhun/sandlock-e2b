# 剩余工作索引（2026-10-09 清理后）

**这份文件只列"还没做"的事。**

2026-10-09 按明确要求清理过一次：把**已收口**的条目（N1–N92、FUP-*、各阶段 E*/S*，以及原
"已关（防翻旧账）"两节）从正文移除，只留下面的未作项。被移除部分的全文快照在
[`docs/reports/open-issues-archive-2026-10-09.md`](reports/open-issues-archive-2026-10-09.md)
（git 历史里另有一份）—— 要按编号翻旧账、或要查某条当时怎么收口的，去那里，不要在这里重述历史。
旧的路线图 [`docs/task-backlog.md`](task-backlog.md)（口径停在 2026-09-12）**已归档** ——
全文在 `docs/reports/task-backlog-archive-2026-10-09.md`，原位只留一个指针。新登记的项一律进
本文件。细节以各行的"出处"为准；本文件不复制论证。

状态口径：

| 状态 | 含义 |
|---|---|
| **待做** | 已决定要做，有明确下一步，缺的是执行窗口 |
| **待决策** | 卡在一个只有人能拍的判断上 |
| **不做（带触发）** | 已决定不做，触发条件满足时必须回到这条 |
| **有意保留** | 明确决定维持现状，不是漏项（退役要另立条目） |

**一句话读法**：下面共 25 行，其中 23 行不是"现在就该动手"的 —— 待拍的 4 条、带触发条件的 7 条、
换环境时复核的 2 条、有意保留的 6 条、可选卫生 4 条；建议立刻做的是 **N93**（2026-10-09）与
**N97**（2026-10-11，PTY master 泄漏）。
（N95 于 2026-10-10 收口：fork 已修、本地多节点车道已验，见"清理记录"末。）

---

## 一、新登记（建议立刻做）

| # | 事项 | 现状 | 下一步 |
|---|---|---|---|
| N93 | **R3 / CVE-2026-53362 的沙箱侧闸门**：拦掉"pipe → **数据报** socket 的 `splice(2)`"，切断这条 KEV 的触发链 | **未做**。`splice` 既不在 blocklist、也不在代执行覆盖内（只出现在 `path_surface.rs` 的 `NON_PATH_SYSCALLS` 分类表里）。今天挡住它的是"节点没有可路由的全局 IPv6 + `::1/128` 在 deny 清单" ⇒ **配置漂移就会打开**；arm64 上"不可利用"是架构运气，x86_64 上即为实打实可逃逸 | 进**代执行**（seccomp 拿不到 fd 类型）。判据**不能**写成"fd_out 是 socket" —— Go 的 `socket→pipe→socket` 转发会一起被打断；代价按同类量过的是 +80~90 µs/次，计数探针显示常见负载（node/python3/git/curl/cp/tar）对 `splice` **零调用**。出处：`docs/security-architecture.md` §8.1、`docs/security-audit/security-framework.md` |
| N97 | **PTY 会话结束后 worker 不释放 pty master**：每个会话泄漏 1 个 fd + 1 个线程 + 1 个 `/dev/pts` 节点 | **未修**（2026-10-11 查清；本轮只把可见性与机理钉住）。可见证据：全量车道里 `tests/contract/test_pty_sandlock.py` 与 `tests/sdk/python/test_pty.py`（两条都显式调过 `pty.kill()`）跑完后，容器内 `/dev/pts/0`、`/dev/pts/1` 一直留到 pytest 进程结束（逐秒采样 `tmp/devpts-sample.log`），同一批残留节点也正是 `test_ioctl_inventory` 那条预条件用例**在全量车道里变红**的原因。机理：`RunningProcess.close()` 没有任何调用点（`ProcessManager._drive` 收尾只把 proc 从 `_processes` 摘掉），`OwnIdentityExecProcess.wait()` 不关 master，PTY 模式下 `close_stdin()` 提前 return（注释写"master 由 wait() 关"，这句在 own-identity 路径上没有实现）⇒ stdin writer 线程永远阻塞在 `_stdin_queue.get()`，把 master（和它的 pts 节点）一起钉住 | 修法：`_drive` 收尾（或 PTY 的 `_mark_eof` 之后）调 `running.close()`，并给 PTY 的 writer 线程一个 None 哨兵让它退出；改完在本地车道跑 `tests/contract/test_pty_sandlock.py`、`tests/sdk/python/test_pty.py`、`tests/sdk/python/test_stdin.py`、`tests/security/test_ioctl_inventory.py`。出处 `envd_service/executors/sandlock.py:594-800`、`envd_service/own_identity.py:1486-1640`、`envd_service/process/manager.py:455-505` |

## 二、等人拍板

| # | 事项 | 现状 | 下一步 |
|---|---|---|---|
| E2B_PAUSED_TTL_S | paused 沙箱的过期策略 | 已实现（`4396915`），默认 **0 = 不启用**；打开后周期任务只删"超期且 paused"的沙箱 | 拍板是否启用、阈值多少。出处 `docs/checkpoint-restore-e2b-half.md` §6(l) |
| 空闲判定 ② | 沙箱互访 / 纯 egress 型长任务判不出空闲（① CPU 采样已上线并集群验收全绿） | ② **未做** | 等真有人用这类负载再做（每沙箱网络计数）。出处 `docs/resource-contention.md` §6 |
| 控制面移出 uid 65534 | **已评估（2026-10-09）⇒ 可行**：控制面与 worker / agent 面 A 同 uid ⇒ 被攻破的 agent 可 ptrace 读控制面内存（实测）。评估订正了三条过期结论（`.uid_pool.lock` / `0700 记录目录` 今天都不约束控制面；舰队里"解包缓存"在节点本地，控制面不碰） | **仓库侧已落地（2026-10-09）**：清单（k8s 控制面 65533、worker/agent 面 A 仍 65534、uid 池不动）、按身份拆开的 hand-over（agent 的 `storage-init` 交：`<export>/_snapshots` 是 `65534:65533 2775`、`<export>/_migrate` 是 `65533:65533 2775`、`_volumes` 非递归 chown 到 65533，并顺带扫掉旧的 `_snapshots/<id>` 子目录；worker 经 `securityContext.supplementalGroups` 入组 65533）、`migrate-state-owner.sh` 的 `--uid`/`--rollback`、本地多节点 lane 的读数，都在库里。**舰队尚未滚动** —— 上线是另一步、等人拍板，不是已迁（`hostPID` 拿不掉仍成立，见 §五 R1）。仍开着的是：两份单卷 compose-prod 形状（N94），以及只有集群能证的 k8s 形状事实（NAS AUTH_SYS 认不认补充组、fresh volume 的权限位） | 先拍板上线：worker 缩到 0 → `migrate-state-owner.sh --uid 65533 --apply` → apply **整套**清单（agent DaemonSet 也在内 —— 只 apply `control-plane.yaml` 会让 `_volumes` 留在 65534，第一个建卷就撞具名 EACCES；`storage-init` 才把 `<export>/_migrate`/`_volumes` 交给 65533、把 `<export>/_snapshots` 交给 `65534:65533`；控制面 pod 滚上去到 `storage-init` 跑完之间是窗口期，要挑安静的时候做）→ 起 worker → 跑 Task 1 探针；上线时顺手在集群上补那两条形状事实。N94 单独决定要不要一起收口 —— 不做的代价是那两份形状里"被攻破的 agent/worker 可 ptrace 控制面"继续成立。方案与代价见 `docs/security-audit/control-plane-uid-2026-10-09.md`；为什么值得做见 `docs/security-audit/c3-agent-hostpid-2026-10-09.md` §5 |
| N94 | **两份 compose-prod 形状的控制面仍是 65534**（与 k8s 形状的 65533 不同） | Task 3（2026-10-09，`c3392d4`）把**仓库里的 k8s 清单**（`deploy/k8s/control-plane.yaml`）改成 uid 65533 —— **舰队滚动是另一步、等人拍板，尚未执行**（清单已改 ≠ 已迁）。`deploy/compose/docker-compose.prod.yml` 与 `deploy/stack/docker-compose.prod.yml` 是**单卷**形状：控制面与 worker 的 `E2B_IMAGE_CACHE_DIR` 都是 `/var/lib/e2b-sandboxes/_images`（两份都没设 `E2B_IMAGE_OCI_DIR`，于是 OCI 目录就是它），在同一个 `sandbox-shared` 卷上**两边都写它** ⇒ 属主拆不开，两份清单的 CP 保持 65534（就地注释写明原因）。**第四份 compose 形状 `deploy/compose/docker-compose.yml` 不在这类里**：它没给控制面设 `user:`，跑的是镜像默认的 uid 0（root），所以"控制面与 worker 拆 uid"这个问题在那里不成立 —— 枚举 compose 形状时不要把它算进 N94 | 要不要一起收口：与 k8s 形状同一个问题（选共享组 `group_add` + 目录 0770/2770，或把 worker 的 rootfs 缓存挪到节点本地）——不做的代价是这两份形状里"被攻破的 agent/worker 可 ptrace 控制面"继续成立。出处 `deploy/compose/docker-compose.prod.yml:334,593`、`deploy/stack/docker-compose.prod.yml:366,639`；背景 `docs/security-audit/control-plane-uid-2026-10-09.md` |

## 三、带触发条件（不触发就不做）

| # | 事项 | 触发条件 | 出处 |
|---|---|---|---|
| OBS-6 | 租户隔离默认关闭（`E2B_TENANTS` 未设 ⇒ 任一 API key 可列/读/删/驱动全部沙箱与卷） | 开放第二个使用者/租户、把 API 给第三方、把 `sandbox_id` 或 access token 暴露给非所有者 | `docs/tenant-isolation.md`；`docs/security-audit/findings.md` OBS-6 |
| N29④ | 入口侧参数（`proxy_read_timeout` ≥ 合法拷贝、复核 `non_idempotent`/`max_fails`/`fail_timeout`） | ① 同步路径真撞 504（建箱拉大镜像、模板构建）；② 拿到入口主机的 SSH 或控制台 | `docs/k8s-deployment.md` §22.5.14 |
| 入口代理 | `172.18.78.49:3000` 是云上 nginx/VIP（2000 文件快照固定 60.1 s → 504，两次尝试都真拷了） | 同上 —— 要 SSH 或控制台才能改 | `docs/k8s-deployment.md` §22.5.14 |
| O2 | 入口侧 TLS 代理层（仓库内 NodePort 入口不带 TLS ⇒ API key 与沙箱数据明文过网） | 入口侧真的出现 504/性能问题，或拿到入口主机控制台 | `docs/superpowers/plans/2026-09-26-decisions.md` 第 4 条 |
| 信号面 | kill 族零中介（`kill`/`tgkill`/`rt_sigqueueinfo`/`pidfd_send_signal` 既不在 blocklist 也不在通知表） | ① 同一沙箱内出现多个互不信任主体；② 有人同时关掉 `E2B_PID_NS` 与 `E2B_PER_SANDBOX_UID`（首选是恢复其中之一，不是加信号策略）；③ 需要的只是可观测量而非 gate | `docs/security-audit/findings.md`《2026-10-01 补测：kill 一族的实际边界》 |
| N31③ / N30-L3 | NAS `FileCountLimit`（条目数硬限）；镜像/块设备按"峰值"记账 | 换存储：NAS → 支持项目配额的 XFS/CephFS，或接入支持 per-sandbox 配额的存储 | `docs/disk-quota-options.md` |
| N26③④ | uid 审计、NAS 权限组确认、卷切片 projid 归属 | ③ 出现第二个挂载该卷的信任域；④ 换 NAS 或换挂载用户 | `docs/k8s-deployment.md` |

## 四、换环境时回来做（不是代码任务）

| # | 事项 | 现状 | 何时回来 |
|---|---|---|---|
| O1 | 目标机 prjquota ⇒ **quota-agent 至今没落地**（出厂是"无 per-sandbox 磁盘硬限 + 一条 WARNING"的降级形态）；真实生产 NFS 上的 per-sandbox uid × no_root_squash 组合也未复测 | 已复核（2026-09-27 只读实测）：共享卷是阿里云 NAS（`nfs4`），XFS 项目配额**结构上不可得** ⇒ 不是配置漏项 | 存储换成支持项目配额的 XFS/CephFS 时：落 quota-agent + 跑 XFS 契约 lane + 在生产 NFS 上重跑探针 |
| F5 | uid 池的 `flock` 互斥**只有 NFSv4.0 跨节点成立**，跨节点没有 CAS ⇒ 分布式单飞只能靠"锁 + 记录" | 约束已满足 | 换 NAS / 换挂载参数时随部署复核一次 |

## 五、有意保留的残余（都还没做，但**不是**漏项）

| # | 事项 | 现状 | 说明 |
|---|---|---|---|
| N14-S5 ① | 退役 `E2B_DISK_ENFORCE_DIRTY`（中介 dirty 集快路） | 线上仍 `1` | S4 已证周期全扫在本部署规模下与快路同样新鲜（含 4 棵 × 20000 文件的密集树，逐字节一致），但"大树"代价没到饱和区（饱和点约 28 万文件/棵）⇒ 要退役得**另立一条**并带大树复测，且与"删两条退路"同批 |
| N14-S5 ② | 清单里 `E2B_BASE_IMAGE` 仍钉旧 digest | 与当前 digest 内容相同 | 有意不动：避免两台节点重拉 2 GB base image |
| N14-S5 ③ | `/proc` 合成、策略挂载、COW、磁盘活账本 | 真根下仍需要 | 真 procfs 在本平台三种形态实测全 EPERM，活账本从拦截点采样（9 处 `mark_dirty`）⇒ 不随"退役模拟"一起退 |
| fork `Open` 桶 | `path_surface.rs` 里 7 条"明确待决"的带路径 syscall：`fchmodat2` + `getxattrat`/`setxattrat`/`listxattrat`/`removexattrat` + `file_getattr`/`file_setattr` | 设计内待决，被 `open_items_are_enumerated_and_reasoned` 逐条 pin | 消一条要同步改 pin；`fchmodat2` 是 glibc 已在用的（账本判"要中介不要拒绝"），`file_*` 两条要等内核 6.13+ 签名核实 |
| N84 残留 | 创建请求里的 `diskSizeMB` 仍被**静默忽略**（`cpuCount`/`memoryMB` 已真的解析并对非法值 400） | 未做 | 对外契约不一致：要么解析它，要么按非法字段回 400（今天既不生效也不报错） |
| R1 `hostPID` | agent 的 `hostPID: true`（pod 级，面 A/B 共用） | **已评估（2026-10-09）：不能拿掉** —— 它是面 A 的 pid 反查与面 B 两处反查的承重件；可达面的增量实测为"同 uid ⇒ 等效 ptrace"，收窄杠杆在控制面的 uid（见上一节新登记那条） | 结论与逐条否决的替代路径见 `docs/security-audit/c3-agent-hostpid-2026-10-09.md`；探针 `deploy/scripts/acceptance/probe_c3_agent_hostpid_reach.sh` |

## 六、可选 / 卫生

| # | 事项 | 说明 |
|---|---|---|
| U 盘清理 | `third_party/sandlock/target-linux/` 下的残留夹具（新命名已不冲突，纯卫生，当前约 1.9 MB） | 想清就清，无风险 |
| N11 | fork 分支推送 + SL-1 上游 issue | 待你决定：① `git -C third_party/sandlock push origin <branch>`（需网络+权限；fork 主线领先 `origin/main` 323 个提交）；② 开上游 issue 需要可写凭据。上游 PR 分支 `origin/upstream-pr/netns-free-clean` 已于 2026-10-04 推送 |
| N96 | 多节点 in-process harness 没有每节点的 C3 agent | `tests/conftest.py::_start_multinode` 在一个进程里跑控制面 + N 个 envd + 网关，没有 agent 服务，于是 CP→agent 那条通道**整体未被车道覆盖**：`test_migrate_sandbox_files_between_workers` 在 2026-10-11 之前一直红在"源节点树没被释放"（CP 按自己的 `workspace_base` 推导路径、agent 不在）。当前收法：该用例装一个**与真实 agent 同语义**的替身（按被寻址节点自己的 workspace base 解析、只删 `sbx_*` 树），agent 服务本身由真实 agent 镜像的 `tests/contract/test_c3_slot_identity_grant.py` 与 `tests/unit/test_c3_agent_fileops.py` 覆盖。要加强的话：harness 起每节点 agent app（真 `c3_agent` 二进制在 runner 镜像里没有，需要 stub runner，且会改变所有 create 的形状 —— 所以是独立任务，不是顺手改） |
| N98 | **`tests/security` 里偶发一条 ~31 s 的建箱用例**（不同次跑落在不同文件） | **未定位**（2026-10-11 判定为"复用容器 + 满负载下的建箱慢点"，不是用例本身）。证据：干净全量跑里 4 次观测分别落在 `test_real_root_mounts::test_creating_and_destroying_sandboxes_leaves_no_mounts_behind`（31.37 s call）、`test_sandlock_isolation::test_install_to_system_path_denied`（30.73 s setup）、`test_template_isolation::test_in_process_chroot_is_refused_without_a_slot`（30.91 s call）、`test_pure_root_errno_contract::test_the_errno_for_a_path_outside_every_grant`（30.96 s call）；同一条单独跑是 0.1~0.4 s（`tests/security` 单目录也不复现），相位 1 整体 7:26~7:56 仍全绿。可用的两个 deadline 常量：`slot_verb_timeout_s=15`、`slot_ready_timeout_s=30`（`envd_service/own_identity.py`）；复用容器里跑一小时后 pytest 有 ~100 线程 / 240 fd，`<repo>/tmp/sandboxes` 会涨到 14 GB 量级 | 下一步：给 slot spawn 的握手（`spawn_child` → 子进程 exec → `sandbox-supervise` accept → 首个 `stats`）**加时间戳打点**再判，而不是继续猜"是不是 GIL/页缓存"（后者已被证伪，见 2026-10-11 那条收口记录）。出处：本行 + 车道日志的 slowest 列表（`tmp/lane-final2-1030.log`） |

---

## 清理记录（2026-10-09）

- **移除**：原索引一/二/三/四/五节里所有**已收口**的行（N1–N92 中的完成项、FUP-*、E-*/S-*、
  "已关（防翻旧账）"两节，以及 §五 的"N41 残余 / N27 identity 残差"两条 —— 后者在 S5 T1 把
  `E2B_PURE_ROOTFS=off` 变成启动期具名拒绝之后已不可达）。
- **保留并压缩**：清理时保留 19 行（原文是超长单元格，这里改成"现状 + 触发/下一步 + 出处"的短行）；
  当天稍后又追加 2 行（`hostPID` 评估的结论、以及随之新登记的控制面 uid 待决策）⇒ 当时 21 行。
- **追加**：**N94**（N93 之后的下一个空号）—— compose-prod 两份单卷形状的同一个控制面 uid 问题，
  见 §二 ⇒ 现在 22 行。
- **新增**：**N93**（编号在 markdown 里此前未占用）。
- **全文快照**：`docs/reports/open-issues-archive-2026-10-09.md`；`docs/**`、`tests/**` 里按编号
  引用被移除行的注释仍可在快照里检索到。

## 收口记录

- **2026-10-10 / N95（写路径的 errno 塌缩）**：fork 侧修掉 —— 三个解析函数从 `Option` 换成
  `Result<_, i32>`，把 `openat2` 的 errno 一路带到写处理器，于是"父目录还不存在"答 **ENOENT**、
  策略拒绝仍答 EACCES。本地多节点车道实测：判据探针四条腿转 ENOENT、`/opt` 仍 EACCES；
  `byteplan-python-template` 沙箱里 `npm install -g @larksuite/cli` **一条命令 rc=0**
  （`lark-cli version 1.0.97`）；fork 四条相位全绿（core_lib 945 / core_integ 574 / oci 157 /
  supervise_root 4 / mediation_2uid 9，基线已按 +1 登记）。正文、读数与**未做的那一步
  （舰队滚动，等人拍板）**在 [`docs/n95-write-path-errno.md`](n95-write-path-errno.md) ——
  本条不需要另存快照，它的正文本身就是入库文档。行已从 §一 移除 ⇒ 本文件 22 行。
- **2026-10-11 / 本地全量车道扫红（N96 由此登记）**：prod-shaped 车道（`deploy/scripts/test-prod-shaped.sh`，
  root 相位 + uid 65534 相位）两周前基线 135 failed / 52 error，本轮按根因逐簇收：车道缺"扮演 agent 的
  身份上报者"（`tests/lane_identity.py::with_lane_identity_reporter`，接线到 `make_apps` 与两处手工
  settings）、纯形态缺合成根（`pure_rootfs_dir`）、seccomp 档缺被 mask 的 `unshare`、C3 驱动迁到
  `clone3`、N73 卷布局、以及一批**期望语义**（N95 把"缺父目录"的 errno 从 EACCES 改成 ENOENT 之后，
  纯根/真根几条逐字节读数要重钉）。两处**环境/形状**而非代码的读数也一并记下：车道用 `--network host`
  跑在虚拟机 netns（`ip_unprivileged_port_start=1024`），而部署里的 worker 跑在容器/pod netns（该值
  为 0，共享 netns 形态的 `127.0.1.x:53` DNS 网关绑得上 ⇒ 车道脚本开跑前把窗口按容器语义打开）；
  `IN_PROCESS_NODE_DEFAULT_TOTALS`（8192 MiB / 400 CPU / 2048 进程）只容 4 个并发沙箱，而 perf 套件要
  30~50 个 ⇒ `live_servers` 现在**显式声明**自己的池（准入控制另有 fixture 覆盖）。车道里唯一不再由
  真实组件承担的是 CP→agent 的释放通道 ⇒ **N96**。
- **2026-10-11 / "容器里第一次 chroot 建箱 30~184 秒（偶发永久挂死）"的根因已查清并修掉**：
  现象是每个 *pytest 进程*第一个要沙箱的用例吃满若干个 15 s verb 超时（31 s / 92 s / 184 s，或
  `stats` 卡在 Rust 侧的互斥锁上永不返回），security 每个文件的首个夹具都要建箱 ⇒ 30 s 就绪时限
  撞上时**整个文件**报错（一次全量 11 个文件）。查证手段：fork 侧 `SANLOCK_REALROOT_TRACE` 分阶段
  打点（证明真根那串步骤都发生在最后，时间花在子进程起来之前）、每 250 ms 采 `/proc`（证明"子进程"
  一直在——它的 `comm` 是从调用线程继承来的 `asyncio_0`，我一度当成线程）、以及给卡住的子进程发
  `SIGABRT` 取它自己的 faulthandler 栈。**根因**：`envd_service/identity_grant.py::spawn_child` 用
  `ctypes.CDLL` 直调 `clone3`，而 `CDLL` 在调用期间**释放 GIL**——若那一刻父进程别的线程（事件循环、
  导入、镜像缓存清理线程等）持有 GIL，子进程会**永远**卡在 ctypes 的 `PyEval_RestoreThread`
  （`pthread_cond_timedwait`）里，一行子进程代码都不执行、永不 exec、永不应答 `stats`；而
  `SlotProcess.poll()` 正确地说"它活着"。这也解释了"只在首用、只偶发、随机落在一个进程的第一个
  沙箱用例上"和"负载高时更容易复现"。**修法**：① `ctypes.PyDLL`（调用期间不释放 GIL）让子进程
  带着自己唯一线程持有的 GIL 开始；② 子进程 exec 前不做任何会分配/取锁的活（fd 清单改在**父进程**
  预计算，`_fds_to_close`；`_close_fds` 只做整数循环）；③ CPython 的 `PyOS_AfterFork_Child()`
  **不能**用在这里——手搓 fork 非主线程时会 SIGABRT（实测 -6），所以走"GIL 持有 + 零分配"这条路；
  ④ 可诊断性：子进程的异常/原因写进 slot stderr（`_child_says`），池子报错带上退出码并具名
  （3=身份没落地，4=子进程抛异常，9=execvpe 返回）。**读数**：修后连续 4 个全新容器首次 chroot
  启动 = 0.070/0.095/0.075/0.078 s（修前同形状 31 s / 92 s / 184 s / 挂死）；`tests/security`
  单目录 8:39 → 2:10（149 passed / 1 skipped）。车道里那个为了遮它而设的
  `E2B_SLOT_READY_TIMEOUT_S=180` 已撤掉，恢复部署默认 30 s。
- **2026-10-11 / 本地全量车道收口（第二轮：红清零、慢点成组收、镜像源加护栏）**：干净跑
  `deploy/scripts/test-prod-shaped.sh`（无人干扰，**窄 profile + 默认 env**）相位 1 =
  **3381 passed / 24 skipped / 0 failed / 9:15**，相位 2 = **60 passed / 1 skipped / 0 failed /
  1.0 s**，两个相位全绿。当天更早的四次读数（7:26 / 7:56 / 8:15 / 9:10，其中 8:15 那次两个相位
  全绿）是带着**下一条那个后来回退掉的 `unshare` 补丁**跑的；回退后重跑就是上面这一次，⑥ 那条
  偶发 pty 契约在最后两次读数里也都没出现。逐条：
  ① **相位 2 的 4 条红**：共享 rootfs 缓存（`E2B_TEST_ROOTFS_DIR=/var/lib/lane/rootfs`）由相位 1 的
  root 进程按它的 umask 建出来，uid 65534 在 `resolve_test_rootfs` 上撞 `PermissionError:
  /var/lib/lane`（另外 56 条照常过）。收法：相位 2 开跑前由 root 侧 `mkdir -p` + `chmod 0777`，
  实现放在**容器 lib**（`lane_container_prepare_rootfs`）—— 放脚本里会多出第三个 `docker exec`，
  而"脚本里只有两次跑套件的 exec"被 `tests/unit/test_prod_shaped_lane_netns_passthrough.py` 逐行
  pin 住（那条 pin 现在也把新函数钉住）；mkdir/chmod 不需要能力，`--cap-drop ALL` 没被放宽。
  ② 相位 1 那条 PTY 预条件用例（`tests/security/test_ioctl_inventory.py`）：它原来断言
  "`/dev/pts` 只有 ptmx"，而真形状是 **worker 的 devpts 原样绑入**（全量车道里 pty 用例会留下 `0`/`1`
  两个节点，这就是它变红的原因）。判据改成"DAC 不可达"（`test_foreign_ptys_are_unreachable`：
  别家的节点**按名字可见**，但必须打不开；2026-10-11 在车道容器里挂住一个 pty 复现，读到 EACCES），
  `docs/security-hardening.md` 的 PTY 行同步改写；顺带登记 **N97**（PTY master 不释放）。
  ③ 慢点（都精确到用例）：`test_quota_admission` 三条手工 settings 没设 `create_queue_timeout_s`
  ⇒ "池子满"的 503 先排队 30 s（30.2/30.1 s → 0.12/0.06 s）；`test_quota_agent_startup_race` 的
  `_agent_records` 认了全部 `envd_service.*`，被多节点 harness 的后台心跳/TTL 日志灌进同一个 capture
  （那次 21 条噪声）⇒ 只认本该发言的 4 个 logger，并给该用例装上 `_sleep` 记录器（省 5 s）；
  `test_memory_quota_boxed` 的 holder `hold=30 → 12`（31.2 → 13.3 s；实测其后半程只有 ~1.2 s）；
  `test_template_upload` 的并发上传 `join(timeout=10)` 与自己的 barrier 10 s 窗口撞车（输家只能在
  barrier 破掉之后才 append 409）⇒ 放宽到 30 s（负载下实测 `[204] == [204, 409]`）；
  `test_provision_remote_client` 的"每次调用自己拨一条连接"用进程级 patch 计数 ⇒ 后台心跳在窗口里
  建一个 client 就把它记成 2（实测 `assert 2 == 1`）⇒ 改成**按调用栈归属**（只算
  `_provision_remote` 帧下建的 client）。
  ④ **车道前置的护栏（这一轮踩到的新坑）**：一次全量跑回来 `30 failed / 2 errors`，横跨 20 多个
  毫不相干的文件（`warm_required` 15 条、`registry endpoint http://127.0.0.1:5080/v2 unusable`
  32 条），根因是那个预置镜像的 registry 容器**不在了**（文档 §2.6.1 原来给的配方是
  `docker run --rm`，停一次就连容器一起没）。两处收口：车道开跑前探一次 mirror 的 `/v2/`，不是
  200/401 就带具名信息 `exit 2`（`LANE_SKIP_MIRROR_CHECK=1` 可跳过）——写这条护栏时又踩了一次
  `printf '%s' | while read` 的老坑（**单个元素的值没有结尾换行时循环体根本不执行**，护栏会静默
  通过，实测过一次）；registry 改成具名 + `--restart unless-stopped` + 具名卷，§2.6.1 的配方同步改。
  ⑤ 还没定位的速率项 ⇒ **N98**（`tests/security` 里偶发一条 ~31 s 的建箱用例，逐次落在不同文件，
  单独跑都是 0.1~0.4 s）。
  ⑥ **偶发（四次读数里命中一次）**：
  `tests/contract/test_own_identity_executor.py::test_pty_stdio_and_window_size_reach_the_child`
  读到 `assert b'0 0\r\npty-out' == b'44 132\r\npty-out'` —— 子进程的 `stty size` 是 0x0，说明
  **创建时的 resize 与用例自己的 resize 都没落到那个 slave 上**，而日志里**没有** `sandlock pty
  resize failed` 的告警（`SandlockRunningProcess.resize` 只在抛错时才打），即 ioctl 要么打在错的
  fd 上、要么子进程与 worker 手里的 master 不是一对。同一文件、同一条用例单独跑 6/6 通过
  （0.2~0.6 s），`tests/security`/`tests/contract` 单目录也过 ⇒ 需要的是"全量车道里才会出现"的
  上下文。两个候选：`OwnIdentityExecProcess._pty_master_fd` 是**创建时记下的 fd 号**（进程里同时
  有泄漏的 pty master，fd 复用会让 ioctl 打偏），以及子进程的 slave 与 worker 的 master 来自
  不同一次 `openpty`（fd 交接在负载下出岔）。下一步：在 `resize` 前后把 `stderrno`/`ttyname`/
  `TIOCGPTN` 一起记进 `envd_service` 的日志再跑全量。**未修**。
- **2026-10-11 / `unshare` 那条 profile 规则被误加回去，已回退**：N80（`4e3a7db`，2026-10-06）把槽位
  握手移到 `clone3(CLONE_NEWUSER)` 之后，`sandlock-worker.json` 里最后一条 `unshare` 规则是**专门删掉
  的**（fleet 三种开关全开，每个沙箱的命名空间都来自那一次 `clone3`），worker 自检探针也同步换成了
  `clone3`（`envd_service/config.py::_userns_probe`）。本轮为了让"`net_isolation` 不带 `pid_ns`"的车道
  （`tests/contract/test_mcp_netns.py` 的 harness 只开 net 对，pid ns 落代码默认）跑绿，把那条规则按
  "缺项"加回了 profile —— 而 `deploy/k8s/seccomp-installer.yaml` 是**逐字携带**这份文件的（带
  `checksum/profile` pin），于是集群的 profile 也被一并放宽。**这是错的**：那个组合本来就不是部署形状
  （`docs/deploy-clusters.md` §7.55 的形状矩阵：`net_isolation` 单开在这个 profile 下
  `sandlock_create failed`，2026-10-08 集群实测）。收法：profile 与 installer 各自**逐字节回退到
  HEAD**（`git diff` 干净、profile sha256 回到 `d39657fd…`，由
  `deploy/scripts/acceptance/sync-seccomp-installer.py --write` 重新嵌入）；窄 profile 下
  `tests/contract/test_mcp_netns.py` 3/3 通过（harness 现在自己钉 `pid_ns=True`，即生产那一对开关）；
  §2.4.5、`deploy/seccomp/README.md`、`worker.yaml` 与三份 compose 里"profile 带 `unshare`"的旧描述
  一并改成"默认档 + `pidfd_getfd`"，并留下"这条是被删掉的、什么时候才会需要"的记录。
- **2026-10-11 / 发版 `0.1.0-1185-g39d3460-20261011-225938`（会话 DNS 网关常开 + 本轮 worker 修复）**：
  按纪律走完"重建 wheel → 本地全车道 → build-and-push → apply → 探针"。**上线内容**：① fork
  `sandbox.rs` 的 `SESSION_DNS_GATEWAY_ALWAYS_ON = true`（会话 DNS 网关常开：每个沙箱的
  `/etc/resolv.conf` 一律虚拟成 `<gateway>:53` 回环，上游 = worker 的第一个 nameserver = CoreDNS）；
  ② 本轮 worker 侧修复（`identity_grant` 的 `PyDLL`/零分配子进程路径 = 首用 30~184 s 的根因）。
  **读数**：本地车道（生产开关形状 `E2B_PID_NS=1` + net 对）3380 passed / 24 skipped / 1 error
  （`test_egress_proxy` 的 61 s fixture 起步，单独跑 3/3 过 = N98 那族）；集群 `apply.sh` 顺序
  agent → worker → 逐节点预热（两节点 `peek cached=true`），9 个 pod Running，tag
  `…-1185-g39d3460-20261011-225938`；线上探针 N95 errno `VERDICT collapsed-errno legs: []`、
  `mkdir -p` 两个形态 ok；常开实测：线上沙箱 `cat /etc/resolv.conf` = `nameserver 127.0.1.x`，
  `getent hosts` 对 ACR 与公网名字都解析成功。**回退杆**：把 `deploy/stack/.version` 写回
  `0.1.0-1185-g39d3460-20261011-065341` 再 `apply.sh`。
- **2026-10-11 / 窄 profile 暴露的形状陷阱（本地车道会红，线上不受影响）**：回退 `unshare` 之后，
  **裸跑** `deploy/scripts/test-prod-shaped.sh`（不带 `E2B_PID_NS`）会有 34 条红，全部是
  `SlotDeadError … read notif fd from child: pipe closed before 4 bytes read`；子进程自己的 trace
  （`SANLOCK_REALROOT_TRACE=<path>`，`fail!()` 会写进去）点名原因：
  `sandlock child: unshare(CLONE_NEWUSER): Operation not permitted` —— 即"real-root 沙箱 + pid_ns 关"
  这条路径要自建 userns，而窄 profile 按 N80 的裁定拒绝它（线上每个 worker 都带 `E2B_PID_NS=true`，
  命名空间全来自那一次 `clone3`，所以 fleet 走不到这条路径）。收法（都不碰 shipped profile）：
  ① 车道的**手搓 fixture**（`tests/security/conftest.py::own_identity_sandbox`、
  `tests/security/test_fork_network_features.py`、`tests/contract/test_own_identity_executor.py`）
  显式 `pid_ns=True`（= 部署的形状；它们原来落 `SandlockExecutor` 的默认 `False`，那个默认不读 env）；
  ② 跑车道时带上生产开关 `E2B_PID_NS=1`（+ 需要 netns 时那对）。**仍待拍板**：`Settings.pid_ns`
  的**代码默认仍是 `false`**（2026-09-27 起就挂着"未拍板切换"）—— 只要某个 worker 没声明
  `E2B_PID_NS`，它建的 real-root 沙箱在这份 profile 下就建不出来，且报错只有一句
  `pipe closed before 4 bytes read`。要么把代码默认切成 `true`（与所有 shipped 栈一致），要么在
  worker 启动自检里把"pid_ns 关 + 这份 profile"变成具名拒绝。
