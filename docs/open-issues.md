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

**一句话读法**：下面共 19 行，其中 18 行不是"现在就该动手"的 —— 待拍的 2 条、带触发条件的 7 条、
换环境时复核的 2 条、有意保留的 5 条、可选卫生 2 条；唯一建议立刻做的是 2026-10-09 新登记的
**N93**（第 19 行）。

---

## 一、新登记（2026-10-09，唯一一条建议立刻做）

| # | 事项 | 现状 | 下一步 |
|---|---|---|---|
| N93 | **R3 / CVE-2026-53362 的沙箱侧闸门**：拦掉"pipe → **数据报** socket 的 `splice(2)`"，切断这条 KEV 的触发链 | **未做**。`splice` 既不在 blocklist、也不在代执行覆盖内（只出现在 `path_surface.rs` 的 `NON_PATH_SYSCALLS` 分类表里）。今天挡住它的是"节点没有可路由的全局 IPv6 + `::1/128` 在 deny 清单" ⇒ **配置漂移就会打开**；arm64 上"不可利用"是架构运气，x86_64 上即为实打实可逃逸 | 进**代执行**（seccomp 拿不到 fd 类型）。判据**不能**写成"fd_out 是 socket" —— Go 的 `socket→pipe→socket` 转发会一起被打断；代价按同类量过的是 +80~90 µs/次，计数探针显示常见负载（node/python3/git/curl/cp/tar）对 `splice` **零调用**。出处：`docs/security-architecture.md` §8.1、`docs/security-audit/security-framework.md` |

## 二、等人拍板

| # | 事项 | 现状 | 下一步 |
|---|---|---|---|
| E2B_PAUSED_TTL_S | paused 沙箱的过期策略 | 已实现（`4396915`），默认 **0 = 不启用**；打开后周期任务只删"超期且 paused"的沙箱 | 拍板是否启用、阈值多少。出处 `docs/checkpoint-restore-e2b-half.md` §6(l) |
| 空闲判定 ② | 沙箱互访 / 纯 egress 型长任务判不出空闲（① CPU 采样已上线并集群验收全绿） | ② **未做** | 等真有人用这类负载再做（每沙箱网络计数）。出处 `docs/resource-contention.md` §6 |

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

## 六、可选 / 卫生

| # | 事项 | 说明 |
|---|---|---|
| U 盘清理 | `third_party/sandlock/target-linux/` 下的残留夹具（新命名已不冲突，纯卫生，当前约 1.9 MB） | 想清就清，无风险 |
| N11 | fork 分支推送 + SL-1 上游 issue | 待你决定：① `git -C third_party/sandlock push origin <branch>`（需网络+权限；fork 主线领先 `origin/main` 323 个提交）；② 开上游 issue 需要可写凭据。上游 PR 分支 `origin/upstream-pr/netns-free-clean` 已于 2026-10-04 推送 |

---

## 清理记录（2026-10-09）

- **移除**：原索引一/二/三/四/五节里所有**已收口**的行（N1–N92 中的完成项、FUP-*、E-*/S-*、
  "已关（防翻旧账）"两节，以及 §五 的"N41 残余 / N27 identity 残差"两条 —— 后者在 S5 T1 把
  `E2B_PURE_ROOTFS=off` 变成启动期具名拒绝之后已不可达）。
- **保留并压缩**：上面 19 行（原文是超长单元格，这里改成"现状 + 触发/下一步 + 出处"的短行）。
- **新增**：**N93**（编号在 markdown 里此前未占用）。
- **全文快照**：`docs/reports/open-issues-archive-2026-10-09.md`；`docs/**`、`tests/**` 里按编号
  引用被移除行的注释仍可在快照里检索到。
