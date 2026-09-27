# envd route-B 接线设计（2026-09-09；backlog #5 剩余项）

> **执行状态（2026-09-27 更新）**：**已落地并上线** —— route B（supervise 槽位模型）已是生产形态，`envd_service/route_b.py` 长期在线；T5 xfail 已摘。
> **仍有效的决定**：W1 槽位模型——槽位按**沙箱自己的 host uid** 定向租用；停车程序不能用 `/dev/zero`（文末"实现期的修正"8 条仍有效）。
> **已作废的假设**：无（设计期被推翻的 8 条已就地写在文末）。证据：`docs/open-issues.md` T5 行。

> 状态：**设计定稿 + Task 1（W1 槽位管理器）+ Task 2–4（executor 全面走 supervise、
> 摘 T5 xfail）已落地（2026-09-09）**，Task 5 的三档门禁复跑全绿。
> fork 侧 F16 语言客户端见 `6571c36`。
>
> 落地时推翻/新增了 8 条本文件原先没写的事实，见文末「实现期的修正」——
> 尤其：**停车程序不能用 `/dev/zero`**，**槽位按沙箱自己的 host uid 定向租用**。

## 背景与目标

T5：镜像 rootfs（chroot）形态下，共享卷写入经 fs_denied 激活的路径中介执行。进程内
root worker 的中介身份 = root ⇒ 文件宿主属主 root、沙箱 `chmod` EPERM、1777+sticky
跨 uid 保护不成立（曾是 `tests/contract/test_uid_permissions.py` 的 strict xfail，
2026-09-09 route-B 接线后已摘）。
fork 侧结论（2026-09-04 用户拍板 + F2b/F16）：route B —— 每沙箱一个
`sandlock-supervise` 进程，**euid == 沙箱 host uid**；envd（worker）只当控制面客户端，
经 `sandlock.supervise.SuperviseChannel` 发 verb（exec/wait_child/kill_child/
update_network/shutdown）。

## 槽位模型：W1（默认，沿用 2026-09-04 确认）

- 固定、不重叠的 uid 段（`uid_pool_start..+size`，复用 E2B `uid_pool` 的段语义）；
- 一个 uid = 一个 supervise 进程 = 一代沙箱；代次结束 = 进程重启（同 uid 原地重启）；
- uid 复用窗口 = 同时在世槽数 N；
- W2（换 uid 重启、窗口 = 段大小 M）需要特权 restarter，静态 compose 做不到，列为
  可选升级，不阻塞本次接线。

## 组件：`envd_service/route_b.py`（本计划 Task 1，已实现）

`W1SlotPool`（默认 **transport 1 = fd handoff**，见「实现期的修正」#6）：

- `__init__(uid_start, size, worker_uid=None, supervise_bin=None)`；
- `acquire(sandbox_id, policy_json, program_json, *, uid=None, name=None)`：
  默认挑空闲 uid（LRU，空闲 = 无在世进程）；executor 传 `uid=<沙箱 host uid>`
  定向租用（段外 uid 报错、在世 uid 拒绝二次租用），spawn
  `sandlock-supervise --policy <file> --uid <uid> --control-fd <N> --serve
  --program <file>`（描述符由池的 `socketpair()` 经 `pass_fds` 继承、同号交付），
  再用 `stats(launched:true)` 过就绪门（槽位**先 bind 后 launch**，见修正 #3）；
  `transport="path"` 才走 `--serve-path/--token/--peer-uid` 的 registered 形态
  （接外部槽位池时用）；
- `release(sandbox_id)` / `retire(handle)`：SuperviseChannel `shutdown` + 等进程退出
  → uid 回池（W1 原地重启）；uid 台账加锁，进程未确认退出前不释放该 uid；
- `acquired_uid(sandbox_id)` / `live_slots()` 供 stats/对账。

命名：slot name 默认 `rb-<sandbox_id>`，executor 传自己的 `instance_name`
（= sandbox_id 或其 sha256 短哈希）以便与日志/进程内后端对齐；token 每次
acquire 重新生成；socket =
`/tmp/sandlock-ctl-<uid>-registry/<fnv1a16(name)>.d/control.sock`（registry 按 uid
隔离，见 fork `control.rs`）。

## 接线 Task（2026-09-09 实现状态）

1. ✅ **ceiling → `--policy` 全字段 JSON**：`_build_instance_policy()` 拆成
   `_policy_ceiling()`（kwargs）+ 组装，`route_b.supervise_policy_document()` 做单向
   翻译：`fs_mount` dict → `VIRTUAL:HOST` 规格串、丢 `None`（wire 上 `null` 不是
   「未提供」）、丢 `mediation_run_as`（route-B 不需要降级档）、未知字段按名拒绝。
   字段表 `SUPERVISE_POLICY_FIELDS` 由单测与 fork `policy.rs::POLICY_FIELDS` 逐名对齐
   （53 项）。开关：`E2B_ROUTE_B=auto|on|off` + `E2B_ROUTE_B_SLOTS>0`（任一即强开）；
   `auto` 只在「root worker + per-sandbox uid + 已分配 host_uid + chroot 形态 +
   wheel 带 supervise」成立时启用；强开而前置不满足 ⇒ 建箱即报错（不静默退 route-A）。
2. ✅ **M0 停车程序改为自 stop**：`while :; do kill -STOP $$; done`（`PARKING_PROGRAM`）。
   原方案的 `read x < /dev/zero` 会把每个 route-B 沙箱跑满一核：exec 会话主程序的 stdio
   被 core 固定为 `/dev/null`（`instance.rs::launch_exec_inner`），`read` 永远等不到换行
   ⇒ dash 每字节一次 `read(2)`、bash 丢 NUL 死循环。契约
   `test_parked_main_program_costs_nothing` 钉住「1 s 墙钟内整棵槽位进程树 ≤2 tick」。
3. ✅ **verb 面迁移**：`route_b.RouteBInstance` / `RouteBExecProcess` 完整复刻
   `SandboxInstance`/`ExecProcess` 面（`exec`/`wait_child`/`kill_child`/
   `update_network`/`close`→`shutdown`），executor 只剩一条代码路径；
   `_CommandGate`/并发上限在 ProcessManager 层不受影响。建槽/收槽走
   `asyncio.to_thread`（`_ensure_instance_async`/`_reopen_instance_after`），
   事件循环不再被进程级操作堵住；`update_network` 命中「槽位已死」时**拒绝本次更新**
   而不是在请求路径里重启槽位（记录与运行时不会分叉，下一条 exec 再重建）。
   PTY：worker 侧 `os.openpty()`，child 拿 slave 三端、`resize` 直接打在 master 上
   （Linux 契约用 `stty size` 回读 44x132 验证）。pause/resume：槽位 `kill_child`
   带信号号 ⇒ route-B 子进程 `supports_signal_pause=True`（进程内后端仍 False）。
4. ✅ **摘 xfail**：`tests/contract/test_uid_permissions.py` 的 strict xfail 已删，
   chroot 形态容器实跑 4 passed，日志里两个沙箱分别租到 uid 20000/20001 的槽位。
   ✅ **删 `mediation_run_as='supervisor'` 降级档（2026-09-10）**：前置已满足
   （`E2B_PER_SANDBOX_UID` 已成部署默认）。`_mediation_run_as()` 与两处 ceiling 里的
   该键一并删除，`_route_b_selected()` 改成 `_route_b_decline_reason()`（一个决策点，
   disclosure 原文引用它）。删档后 chroot 形态在「拿不到槽位」时被 fork 拒绝建箱
   （不再静默产出 supervisor 属主的文件 = T5），executor 在建箱前打一条 ERROR 说明
   原因与修法；非 root worker（中介即沙箱自己的 euid）不构成拒绝，故不打。
   容器实测：`tests/security/test_template_isolation.py` 三条（两条走真槽位、
   一条钉住拒绝 + 对照组）。
   删档后终态门禁（`tmp/final-verify.sh` / `tmp/run-f31.sh` 顺序单容器逐相跑，
   互不并发；清理前后各一遍，六相数字逐条相同）：
   gate A `1069/4/0`、gate B `1068/5/0`、mediated-chroot 切片 `100/1/0`、
   生产形 lane phase 1（root + 部署 capset）`966/3/0`、**phase 2
   （`--user 65534:65534`，无池无槽位）`47/1/0`**、macOS 全量 `989/84/0`；
   日志 `tmp/f31-gate-a.log` / `tmp/f31-gate-b.log` / `tmp/f31-focused.log` /
   `tmp/f31-prod1.log` / `tmp/f31-prod2.log` / `tmp/f31-macos.log`。
   两个坑记录在案：① 门禁容器必须 `--network host`，否则 5 条 sdk 模板构建用例
   会因为 buildctl 连不上宿主随机端口而假红（与代码无关）；② 生产形 lane 从「只削
   cap 但仍是 root」变成两相，phase 2 才是 compose 清单真正跑的形态。
5. ✅ **门禁（终态树复跑，含 transport 1 之后）**：gate A（chroot）`1054 passed / 2 skipped / 0 failed`、
   gate B（pure）`1046 passed / 3 skipped / 0 failed`、route-B 专题切片容器
   `64 passed`；日志 `tmp/rb-gate-a2.log` / `tmp/rb-gate-b.log` /
   `tmp/rb-macos2.log`（macOS `972 passed / 74 skipped / 0 failed`）/
   `tmp/rb-focused.log`。
   性能：租槽位只落在每沙箱**第一条命令**（in-process 10.63 ms → route-B 57.11 ms，
   +46 ms），稳态 exec 无差异（warm p50 4.29 → 4.35 ms）——`tmp/perf/route-b-first-exec.txt`。
6. ⬜ **生产 spawner 形态**（launcher / 外部槽位池）仍未接 —— 但**它的定位要改**：
   审计（2026-09-10）发现现网 worker 实际是 root + Docker 默认 cap 集（含
   `SETUID`/`SETGID`/`CHOWN`）+ `SYS_ADMIN`，**特权前置已经满足**；`k8s/worker.yaml` 的
   `capabilities.add` 也是"在默认集上追加"，不是"只给这两条"。所以挡住 auto 档落地的
   不是特权，而是**镜像里的 wheel 没有 route-B 语言面**与**两个 worker 的 uid 段没拆开**。
   launcher / 外部槽位池这条路，因此从"上线前置"降级为"当部署必须非 root 时才需要"。
   见 `docs/production-deployment-requirements.md` §2.4 线上审计块与 HANDOFF 同名块。
   > **审计更正（2026-09-10）**：这句的前提按**仓库清单**成立、按**线上实态**不成立 ——
   > 线上 worker 实际是 root（远端清单无 `user:` 行、旧镜像无 `USER`）。所以现网 auto 档
   > 并不是"因为没特权而缩退"，而是"因为 **wheel 不带 route-B 语言面**而缩退"
   > （`sandlock_supervise_connect_fd` = False、缺 `sandlock-supervise`）⇒ 先重建镜像就能让
   > route B 生效，launcher 那条路只在"worker 必须非 root"的部署里才是硬前置。
   > 同时必须拆开两个 worker 共用的 uid 段（现在都为空 ⇒ 都从 10000 起）。逐条见
   > `docs/production-deployment-requirements.md` §2.4 线上审计块与 HANDOFF 同名块。
   > 补（2026-09-10，删档后）：该形态现在**有测试了** —— `test-prod-shaped.sh` 的
   > phase 2 就按 `--user 65534:65534 --cap-drop ALL` 跑 mediated-chroot，实测
   > `47 passed / 1 skipped / 0 failed`（`tests/security/test_template_isolation.py::
   > test_unprivileged_worker_still_mediates_the_chroot` 钉住「进程内中介就是自己的
   > euid ⇒ fork 不拒绝、chroot 仍然限制路径空间」）。删档没有动到现网形态。

## 部署检查表（route-B 上线前逐条确认）

   > 更新（2026-09-09 晚）：`E2B_PER_SANDBOX_UID` 已成默认开 ⇒ 下面第 2 条的前置
   > 在「worker 有特权」时自动满足；非 root worker 仍旧自动缩退（E5.1）+ 一条 WARNING。
1. **worker 特权**：默认 spawner 用 util-linux `setpriv` 把槽位起在沙箱 host uid 上
   ⇒ worker 必须 root（或注入 launcher `spawner=`）。非 root + `E2B_ROUTE_B=on` ⇒
   建箱即报错（不静默退 route-A）；非 root + `auto` ⇒ 走进程内后端。
   **删档后（2026-09-10）这句话只对外半句成立**：降级档已删，但非 root worker 的进程内
   中介就是 worker 自己的 euid、也正是沙箱的 host uid ⇒ fork 不拒绝，没有 T5 症状
   （跨租户隔离仍弱，是 E5.1 的既有取舍）。会被拒的是另一半形态 ——
   **root worker + chroot + 拿不到槽位**（`E2B_ROUTE_B=off` / wheel 不带 supervise /
   `E2B_PER_SANDBOX_UID=false`）⇒ 建箱失败 + 一条 ERROR 说明为什么没有槽位、怎么修。
   **现网两套部署都不满足**：`docker-compose.prod.yml` 的 worker 是
   `user: "65534:65534"`，`deploy/k8s/worker.yaml` 只加
   `SYS_ADMIN`/`NET_BIND_SERVICE`（镜像默认 USER 也非 root）⇒ 两处 auto 档都保持
   进程内后端。要在生产开 route-B：worker 改 root（+ `CAP_SETUID`/`CAP_SETGID`
   在Capability里显式列出），或按 fork §2/§8 的边界由**外部 launcher/槽位池**起
   槽位（`W1SlotPool(spawner=...)` 注入），worker 自己永不装特权。
   > **审计更正（2026-09-10，实测线上 172.18.80.140）**：这句话描述的是**仓库清单**，
   > 不是**已部署状态**。远端 `/opt/sandlock/docker-compose.prod.yml` 里根本没有 `user:`
   > 行（那行是 09 月才加进仓库的），worker 镜像 `0.1.0-20260830-191728` 也没 `USER`
   > 声明 ⇒ **线上 worker 实际是 root**，`CapEff=0xa82425fb`（默认集 + `SYS_ADMIN`，
   > **无 `SYS_PTRACE`**）。所以"现网不满足特权前置"这个结论对线上不成立；线上真正缺的是
   > **wheel 的 route-B 语言面**（`sandlock_supervise_connect_fd` = False、
   > `sandlock/bin/sandlock-supervise` 不存在）与**不重叠的 uid 段**（两个 worker 共用
   > 同一 `sandbox-shared` 卷却都没设 `E2B_UID_POOL_START` ⇒ 都会从 10000 起）。
   > 逐条数据见 HANDOFF「特权最小集实测 + 线上就绪审计」。
2. **`E2B_PER_SANDBOX_UID=true`（2026-09-09 起为代码默认）**：槽位按沙箱自己的 host uid 定向租用；共享 uid
   形态租不到槽位（route-B 的整个身份论证依赖「一个 uid 一代沙箱」）。
3. **wheel 带 supervise**：`<site-packages>/sandlock/bin/sandlock-supervise`
   存在且 0755；`sandlock-supervise` 与 `_sandlock*.so` **必须同批替换**
   （F15 起 frame 版本不兼容会 fail-closed 点名版本）。
4. **`sun_path` 108 字节**：只对 registered 形态成立
   （`/tmp/sandlock-ctl-<uid>-registry/<fnv1a16(name)>.d/control.sock`）。envd 默认的
   fd handoff 不创建任何 socket 路径 ⇒ 该约束对默认路径消失；
   `E2B_ROUTE_B_TRANSPORT=path` 时仍要检查。
5. **scratch 根可被外部 uid 遍历**：`E2B_ROUTE_B_TMP_ROOT`（默认
   `/tmp/sandlock-route-b`）各级目录不能是 0700 —— 池会把 root 档设成 0755、
   uid/slot 档设成 0711，policy/program 文件 `0440 root:<uid>`（里面有 egress
   代理口令与 secret 路径，不能 0644 全机可读）。祖先目录若被别的策略压成
   0700，槽位连自己策略都读不到（`read policy file ...: Permission denied`）。
6. **每沙箱多一个常驻进程**：supervise 进程树（supervise + sandlock-init +
   停车 M0）不计入沙箱 `max_memory`/`max_disk` cgroup；`max_processes` 预算里
   占 1 个（停车 M0）。容量表要按「N 个沙箱 = N 个额外进程」重算。
7. **回收改由 envd 驱动**：槽位 instance 的 `max_lifetime=None`（部署拥有生命周期），
   且因为 M0 常驻，core 的 15 min idle reclaim 不会再触发；沙箱 TTL/idle eviction
   由 worker 现有机制调 `executor.close()` → `shutdown` verb 收口。
8. **一 uid 一代沙箱（W1）**：`release` 后同一 uid 可再租（重启进程，clean slate）；
   槽位进程活着时**拒绝**第二个沙箱同 uid 租用。
9. **channel token 在 supervise 的 argv 里 ⇒ 本机任何 uid 都能看到**（实测 2026-09-09，
   见下「token 暴露面实测」）：`/proc/<pid>/cmdline` 是 0444、**不受 ptrace 门约束**
   （`environ` 才是 0400 被挡），foreign uid 直接读到 `--token <64hex>` 与 socket 路径。
   它之所以**今天还不构成攻击面**：registered 路径的鉴权顺序是
   ①`SO_PEERCRED` ∈ `--peer-uid` 白名单（不在名单里直接静默关连接、不发响应、不记日志）
   ②才是 token。实测槽位（白名单只有 worker uid 0）：
   | 客户端身份 | token | 结果 |
   |---|---|---|
   | uid 0（在白名单） | 正确 | 正常服务（`instance_state=Live`） |
   | uid 0 | 错误 | `SandboxError: requires a valid channel token` |
   | uid 21501（别的租户） | **正确** | 连接被静默关闭（拿不到任何 verb） |
   | uid 21500（沙箱自己，token 就在它的可见 argv 里） | 正确 | 同上，被拒 |
   ⇒ 只有「能以白名单 uid 起进程」才用得上偷到的 token，而那种身份本来就有 worker 权限。
   **这仍是必须记录的暴露面**：任何一次 `ps`/coredump/审计日志都会把 token 落到
   别人眼前，且 `--peer-uid` 一放宽就立刻变成真漏洞。
   ⇒ **已闭口（SL-10）**：envd 默认改用 transport 1（fd handoff，fork F17 补的语言面），
   槽位 argv 里既没有 `--token` 也没有 `--serve-path`，凭证就是那条继承的描述符；
   顺带获得「worker 崩溃 ⇒ 通道 EOF ⇒ 槽位按异常收口杀掉自己这一代」的保证。
   还要复查 `--peer-uid` 的场景只剩 `E2B_ROUTE_B_TRANSPORT=path`（外部槽位池）。
   另加一条护栏：wheel 的 FFI 若没有 `sandlock_supervise_connect_fd`（fork F17 之前），
   `auto` 档宁可退回进程内后端也不静默改用「token 进 argv」的 registered 形态；
   强开则报错点名要重建 wheel（`fd_client_available()`）。
10. **cap 的最小集（2026-09-10 实测，别照抄 §2.4 老口径）**：沙箱侧只要
   `SETUID`+`SETGID`+`CHOWN`；`DAC_OVERRIDE` 是**管理面**（对账/删除/配额扫描要穿租户
   0700 目录）需要；`SYS_ADMIN` 只服务共享卷 bind mount 与直接 `xfs_quota`，
   **不是 route-B 前置**；`SYS_PTRACE` 只在走进程内 `RunAs` 时才需要。
   细节与实测三对照见 `docs/production-deployment-requirements.md` §2.4.1。


## 实现期的修正（原设计没预见的约束）

| # | 事实 | 后果 |
|---|---|---|
| 1 | exec 会话主程序（M0）的 stdio 被 core 固定为 `/dev/null`（`instance.rs::launch_exec_inner`） | 原方案 `read x < /dev/zero` 永远等不到换行 ⇒ dash/bash 各以不同方式为每个 route-B 沙箱跑满一核。停车程序改 `while :; do kill -STOP $$; done`（纯内建、稳态零 CPU、被 SIGCONT 后立刻再停自己）；契约 `test_parked_main_program_costs_nothing` 钉「1 s 墙钟整棵槽位树 ≤2 tick」 |
| 2 | registered 槽位的 serve loop 是**单线程串行 accept**，一次只处理一个 verb | 子进程还活着时发 `wait_child` 会占住整代 ⇒ `RouteBExecProcess.wait()` 先轮询宿主 pid 消失（上限 `CHILD_POLL_CAP_S`）再发 verb；契约 `test_concurrent_commands_share_one_slot_without_stalling` 钉「长命子进程不堵后续命令」 |
| 3 | `--serve-path` / `--control-fd` 都是**先 bind/accept、后 launch** instance | 通道活着 ≠ 能 exec ⇒ `acquire` 用 `stats(launched:true)` 做就绪门 |
| 4 | policy/program 由 worker（root）写、由槽位（uid X）读 | scratch 根与各级目录不能 0700（pytest `tmp_path`、umask 077 都会踩）；文档也不能 0644（里面有 egress 代理口令/secret 路径）⇒ 目录 0755/0711 + 文档 `0440 root:<uid>`，由池强制 |
| 5 | fork 里 `SandboxError` 是 `SandlockError` 的**子类** | 服务端「拒绝」不能被当成「槽位死了」⇒ `request()` 必须先原样抛 `SandboxError`；否则一个策略错误会触发一次无谓的槽位重启 |
| 6 | registered 形态的 channel token 只能走 **argv**（fork F16 只有 transport 2 的语言面）（`/proc/<pid>/cmdline` 0444，**不受** ptrace 门约束） | envd 默认改用 **transport 1（fd handoff）**：fork F17 为 `--control-fd` 通道补了 C ABI（`sandlock_supervise_connect_fd` / `sandlock_supervise_set_timeout` / `sandlock_supervise_check_fd`）与 Python 面，池用 `socketpair()` + `pass_fds` 交付 worker 端 ⇒ 无 socket 路径、无 argv 秘密（SL-10 关闭），并附带「worker 崩溃 → 通道 EOF → 槽位自收口」。持久单流 ⇒ 动词在客户端侧也串行化；verb 超时（`E2B_ROUTE_B_VERB_TIMEOUT_S`，默认 15 s）即退役会话并按死箱处理 |

| 7 | **route-B 沙箱内不再是 root**：core 的 userns 只在「请求身份 ≠ 当前 euid」时创建（`context.rs` 的 `userns_needed`），而槽位本来就以沙箱 host uid 运行 ⇒ 不建 ns、不映射 `0 → X` | 实测（root worker + chroot，同一负载两档对照）：in-process `id -u`=**0**（ns 内 root，宿主侧仍是 X），route-B `id -u`=**X**。文件属主 / T5 两侧一致（宿主 X），差别只在**客体内**是否 root：`apt-get`/`chown`/bind :80 这类「容器内 root」用法在 route-B 沙箱不再可用。要复原需 fork 侧让槽位自己 `unshare(CLONE_NEWUSER)` + 写 `0 X 1`（非特权即可，可行性见 `tmp/unprivileged_userns_probe.py`）——**语义决策，未擅自改**。这也解释了一条门禁红：shell 的「无控制终端」banner 与首个提示符（`# ` vs `$ `）的交错位置两档不同，PTY 契约因此改成「每片恰好一次」并补了 resize 到达证明 |

| 8 | 内核写**别人进程**的 `uid_map` 除 `CAP_SETUID` 外还要求对该进程的 ptrace 访问权 | 进程内 `RunAs`（root worker 把沙箱映射到 X）因此需要 **`CAP_SYS_PTRACE`**；此前一直用 `--privileged` 跑门禁，所以没人发现。实测 `--cap-drop ALL`：缺它则每个建箱报 `sandlock_create failed`，只补它即通。**route B 完全不需要**（槽位自映射），这是选它的第二个理由。envd 侧现在会在启动时探测并 WARNING（`PER_UID_NO_PTRACE_WARNING`），非特权跑法进 `deploy/scripts/test-prod-shaped.sh` |

另外两条也钉进了代码：

- **槽位 uid = 沙箱 host uid**（不是池里随便挑空闲 uid）：workspace 已按该 uid
  chown 0700，换 uid 的槽位连自己沙箱目录都进不去 ⇒ route-B 天然要求
  `E2B_PER_SANDBOX_UID=true`；
- **一 uid 一代沙箱的台账**：`_ledger` 跨整个退役过程持锁，进程未确认退出前
  不把 uid 归还，否则下一个 `acquire` 会撞上还活着的槽位。

## 测试（Task 1 / Task 5）

- `tests/contract/test_route_b_slot_pool.py`（root + Linux）：两个 uid 槽位各
  exec 一份 workload —— X 建文件宿主属主 == X 且自 chmod 生效；Y 对 X 文件
  rm/chmod EPERM（1777+sticky）；并断言 transport-1 不变量（无路径、无 token）。
- `tests/contract/test_route_b_executor.py`（root + Linux）：executor 直接驱动
  真槽位 —— 子进程 uid、文件属主 + 自 chmod、停车零 CPU、PTY 窗口尺寸、
  SIGSTOP 真停 + 信号退出码 -1、close 后 uid 干净可复用、单槽位多命令不互堵。
- `tests/contract/test_uid_permissions.py`：T5 本体（chroot 形态共享卷跨 uid）。
- `tests/contract/test_pty_sandlock.py`（改后）：按「每一片恰好出现一次」断言转录，并新增
  `stty size` → `40 120` 的 resize 到达证明（route B 的 pty 主端在 worker 进程里，正是
  该验的地方）；旧断言钉死四种交错顺序之一，既证明不了 resize，也会被合法的另一交错绊倒。
- 单测 `tests/unit/test_route_b_wiring.py` / `tests/unit/test_sandlock_executor_route_b.py`
  （FakePool + 注入 channel，macOS 可跑）：wire 字段表与 fork 对齐、按 uid 定向
  租用与 W1 二次拒绝、就绪门、退役语义、交付的描述符可用性、verb 参数面。
- fork 侧（F17）：`python/tests/test_supervise_channel.py` 同一真槽位跑 fd handoff
  （持久会话、无 token 也能服务、belt 不匹配仍拒、`set_timeout` 允许 park、
  失败动词退役会话、`check_control_fd` 点名坏描述符）。
