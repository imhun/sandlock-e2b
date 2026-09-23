# 未完成问题索引（2026-09-23）

**这份文件只做索引**：每条给出现状、下一步、以及"细节在哪"。状态口径：

| 状态 | 含义 |
|---|---|
| **待做** | 已决定要做，有明确下一步，缺的是执行窗口 |
| **进行中** | 已经开始，下一步也写着 |
| **待决策** | 卡在一个只有人能拍的判断上 |
| **不做（带触发）** | 已决定不做，触发条件满足时必须回到这条 |
| **已关** | 本轮或近期收口，列在这里只为防止有人翻旧账 |

细节一律以出处文档为准；本文件不复制论证，只给指针。

---

## 一、本仓库（E2B 平台，`control_plane/` `envd_service/` `deploy/`）

| # | 问题 | 状态 | 下一步 | 出处 |
|---|---|---|---|---|
| N35 | **① 中介形态下"写文件 → 立刻执行"撞 ETXTBSY**（中介持有写描述符，events pump 一拍 ≈100 ms 后才放手）；**② chroot（生产）形态下 workspace 的可执行性**（2026-09-23 现状）：**动态 ELF ✅（靠中介匿名 memfd 复制）、静态 ELF ✅（§7 已修）、shebang 脚本 ❌**（解释器在宿主路径空间解析 ⇒ EACCES；同拍写入先撞 ETXTBSY）；同一份静态 ELF 放进镜像 rootfs 一直能跑。 | **进行中**（2026-09-23：§7 已落地——静态 ELF 从 EACCES 变为可执行，旧镜像红/新镜像绿；剩 ② 与 shebang；同轮发现 ETXTBSY 窗口在本形态同样存在，此前被 EACCES 掩盖：同拍写静态二进制报 126、`sleep 1` 后 rc 0，动态 ELF 因走 memfd 免疫） | **已排序**：① ~~修 `fs_writable` 的 Landlock 翻译~~ **已完成**（envd 声明挂载点 + fork 按挂载点权利给挂载源装规则，`landlock.rs::path_rule_rights` 4 条单测）；② 再拍 **A 落 N14 真根**（推荐：一次收掉 shebang/`binfmt_misc`/`$0`/`mountinfo` 一整类；**拦路虎在部署**——实测生产形状下 `mount`/`umount2`/`pivot_root`/`chroot` 全 EPERM；安全账见文档 §9，建议若走 A 取"seccomp 档加无门闩允许项"而不是还 SYS_ADMIN）还是 **B 中介补 shebang**（无部署改动；硬边界：seccomp-notify 不能改 syscall 参数 ⇒ 构造不出内核语义 argv，`$0`/`sys.path[0]` 变 `/proc/self/fd/M`；venv 解释器的前置已由 ① 满足）；③ ETXTBSY 的"写描述符何时放手"归 N15/写账本 | `docs/chroot-workspace-exec.md`（机制+实测表+A/B 详细账）；backlog N35；探针 `tmp/k0s/probe_n35_{exec_gate,ns,realmount}.py`、lane `tmp/k0s/n35-lane.sh`、日志 `tmp/k0s/n35-chroot{,2..6}.log` `n35-pure.log`；用例 `tests/security/test_chroot_exec_shebang.py`（脚本 strict-xfail + 动态 ELF 正向对照） |
| F11 | **控制面不能多副本**（节点注册表在进程内；两副本对同一节点给出相反健康结论 ⇒ 误判孤儿、`404 Sandbox not found`） | **待做**（用户已定：要） | ① 节点视图进 Redis（所有读路径）→ ② 健康扫描单飞 → ③ 快照锁/异步拷贝登记共享（N29 的每 id 锁现在在进程内）→ ④ 队列/限流/模板槽/TTL 单飞 | `docs/control-plane-multi-replica.md` §5；backlog 的 F11 行 |
| N29④ | **入口侧参数**（`proxy_read_timeout` ≥ 合法拷贝、复核 `non_idempotent`/`max_fails`/`fail_timeout`） | **不做（带触发）**（用户定 b：靠异步绕过） | 触发：① 同步路径/其它长请求（建箱拉大镜像、模板构建）真的撞 504；② 拿到入口主机的 SSH 或控制台入口（现在只有 VIP，见下） | `docs/k8s-deployment.md` §22.5.14；backlog N29 行 |
| N27 | **平台状态另起 BASE**（让"沙箱看不到平台命名空间"不依赖形态） | **待做**（用户已定：独立排期） | 落点已收口：`gateway_common/paths.py` 三个 helper + 新增 `E2B_STATE_BASE` + 一次性迁移脚本；注意 EXDEV | backlog N27 行；`docs/pure-shape-decision.md` §4 |
| 空闲判定 | **判为空闲的三类盲区**：沙箱自发 egress（supervisor 代发）、纯 CPU/内存长任务、沙箱互访 | **待做**（用户已定：补采样） | 先做"每沙箱 CPU 时间增量"进 `sandboxActivity`（覆盖盲区 2），验收=烧 CPU 的箱子不被判空闲、真闲的仍会被判；网络计数（盲区 1/3）留到真有人用 | `docs/resource-contention.md` §6 |
| N30 | **配额口径**：镜像/块设备路线能否按"峰值"（写多少算多少、删了不退） | **待决策** | 定口径后才知道走 qcow2+NBD（要 privileged/SYS_ADMIN）还是维持"记账软闸门 + loop 镜像兜底" | backlog N30 行；`docs/disk-quota-options.md` |
| §10.5 遗留 | 两条决策：v3/`nolock` 与 v4 的锁语义差异要不要写进存储选型门槛；F4「非 root worker + 网络文件系统」是否升级为基线显式约束 | **待决策** | 各一句结论即可，落点是 `docs/k8s-deployment.md` §10.5 与 `docs/production-deployment-requirements.md` | `docs/k8s-deployment.md` §10.5 |
| OBS-6 | **租户隔离默认关闭**（`E2B_TENANTS` 未设 = 单租户兼容模式；任一 API key 可列/读/删/驱动全部沙箱与卷） | **不做（带触发）**（用户 2026-09-22 定：暂不做多租户） | 触发：开放第二个使用者/租户、把 API 给第三方、把 sandbox_id 或 access token 暴露给非所有者。届时按 `docs/tenant-isolation.md` 设两个变量 + 跑 `migrate-tenants.py` | `docs/security-audit/findings.md` OBS-6 |
| OBS-5 | **pure/本地形态没有磁盘硬上限**（活账本依赖中介） | **不做（并入 N15）** | 与 N15 同一件工作：中介一旦到位，活账本随之到位 | `docs/pure-shape-decision.md` §2 |
| N31③ | NAS `FileCountLimit`（条目数硬限） | **不做** | 被"不引云 API"否掉；条目数记账/目录块口径已落地 | backlog N31 行 |
| N26③④ | uid 审计、NAS 权限组确认、卷切片 projid 归属 | **不做（带触发）** | 触发：③ 出现第二个挂载该卷的信任域；④ 换 NAS/换挂载用户；projid 接上支持 per-sandbox 配额的存储时 | backlog N26 行 |
| N34 | 直接跑 core_integ 二进制时 stdio 必须是管道（否则 restore 用例红） | **不是缺陷**（排查提醒） | — | backlog N34 行；fork `scripts/test-all.sh` 的 `run()` |

## 二、fork（`third_party/sandlock`）

| # | 问题 | 状态 | 下一步 | 出处 |
|---|---|---|---|---|
| N15 | **non-chroot 形态的统一闸门**（33 条 `PURE_UNGATED`：stat/readlink/chdir/chmod/时间戳/8 条 xattr/inotify/open_tree…） | **进行中** | 路线已定：pure 形态带 `chroot_root = "/"`（identity 翻译，复用现成 handler + 活账本；核心验收已证——原 xfail 转正）。剩：**① 29 条测试前提迁移**（8 个 security 文件里裸构造 `SandlockExecutor` 的用例改用 `route_b_sandbox(None,None)`、`test_route_b_selection_matrix[auto-pure]` 改成"自动上槽位"、另 4 条形态差异）；**② N35 已定案（2026-09-23）：中介化会暴露的那条是 ETXTBSY 一拍窗口**（写描述符由 supervise 的 events pump 释放，`DEFAULT_INTERVAL=100 ms`），由 N15 自己的验收处理"写描述符什么时候能放手"；N35 的另一半（chroot 形态下 shebang 解释器被内核在宿主路径空间解析 ⇒ EACCES）是**另一条线**，见 `docs/chroot-workspace-exec.md` | backlog N15 行；`docs/pure-shape-decision.md` §4/§5 |
| N14 | **真根**（mount ns + pivot_root 取代"虚拟根"） | **待决策（可选路线）** | 只在"33 条做完后仍觉得拦截清单完整性是负担"时评估；不是 N15 的前提 | backlog N14 行 |
| FUP-28 | **撤掉 E2B 侧 `..` 相对软链改写** | **待做** | 前提①②已验（宿主机内核实测 `.94` 8107/40000、`.140` 6855/40000 EAGAIN；集群 wheel manifest HEAD=`7b60349c` 含 FUP-26）；缺**前提③产品路径 soak**（≥97482 受管 open 0 失败、300 次 exec 0 个"127+空 stderr"、内核 EAGAIN>0），要在部署宿主上跑（需 arm64 soak 二进制或节点起 dev 镜像） | fork `docs/fork-plan-followups.md` FUP-28 |
| `Open` 桶 | `path_surface.rs` 里 12 条"明确待决"的带路径 syscall | **设计内待决** | 被 `open_items_are_enumerated_and_reasoned` 逐条 pin；消一条要同步改 pin | fork `crates/sandlock-core/src/sys/path_surface.rs` |
| U 盘清理 | `third_party/sandlock/target-linux/tmp/` 的残留夹具（新命名已不再冲突，纯卫生，当前 1.9 MB） | **可选** | 想清就清，无风险 | — |

## 三、运维 / 基础设施（需要人类动作）

| # | 问题 | 状态 | 下一步 | 出处 |
|---|---|---|---|---|
| 入口代理 | `172.18.78.49:3000` 是**云上 nginx/VIP**：MAC `ee:ff:ff:ff:ff:ff`（VPC 代理 ARP）、22 端口不可达、80/3000 开放、响应无 `Server:` 头；2000 文件快照固定 **60.1 s → 504**（且两次尝试都真拷了） | **不做（带触发）** | 需要 SSH 或控制台者才能改；触发见 N29④ | `docs/k8s-deployment.md` §22.5.14 |
| F5 存储锁 | uid 池的 `flock` 互斥**只有 NFSv4.0 跨节点成立**；跨节点**没有** CAS ⇒ 任何分布式单飞只能靠"锁 + 记录" | **约束（已满足）** | 换 NAS/换挂载参数时随部署复核一次 | `docs/k8s-deployment.md` F5 行、§13 |
| O1/O2/O3 | 目标机 prjquota / TLS 代理层 / 凭据管理 | **本轮未复核** | 见 HANDOFF 的运维台账；做 N27/多副本时顺带确认 | `docs/HANDOFF.md` |

## 四、已关（本轮，防翻旧账）

| # | 问题 | 收口 |
|---|---|---|
| N32 | 重活期间心跳断档 → 沙箱被判 orphaned → 快照 409 | ✅ 控制面/worker 循环里的同步 NAS 操作全部下循环 + 健康扫描"自己落后就不判";集群验收 77.2/78.3 s → 5.03/5.05 s |
| N33 | magic-link 写自己的 stdio 把 run 挂死 + fork 门禁三个红 | ✅ `holds_a_file_size` + timing 三红修复;fork 门禁四相位全绿、基线刷新 |
| N29① | 大快照是同步 HTTP ⇒ 入口 504 | ✅ 异步形态（`Prefer: respond-async` + `GET /snapshots/{id}` 轮询、重启可收尾）；走入口验收 202/0.083 s → completed/74.9 s |
| T1 | 目标机上"沙箱 chmod 自己写的文件" | ✅ lane 里已不再 skip（现行 6 条 skip 均为形态/工具缺失） |
| F3/F7/F11(worker 多副本) | 跨节点 pod 流量 / worker node id / 多 worker | ✅ 分别由 Calico VXLAN（§11）、StatefulSet（§15）、N13 收口 |
| FUP-24 | `kill --all` 兜底判据过宽 | ✅ `SendCommandError` 分类收口（fork） |
