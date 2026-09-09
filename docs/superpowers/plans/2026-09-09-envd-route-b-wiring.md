# envd route-B 接线设计（2026-09-09；backlog #5 剩余项）

> 状态：**设计定稿 + Task 1（W1 槽位管理器）+ Task 2–4（executor 全面走 supervise、
> 摘 T5 xfail）已落地（2026-09-09）**，Task 5 的三档门禁复跑全绿。
> fork 侧 F16 语言客户端见 `6571c36`。
>
> 落地时推翻/新增了 6 条本文件原先没写的事实，见文末「实现期的修正」——
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
   ⬜ **删 `mediation_run_as='supervisor'` 降级档** 还没做：它是「route-B 不可用」时
   chroot 形态唯一的逃生门（现网默认 `E2B_PER_SANDBOX_UID=false` ⇒ 没有独立 host uid
   ⇒ 租不到槽位 ⇒ 直接下发 caller 会被 fork F6.1 C 档在建箱前拒绝）。删档的前置是
   per-sandbox uid 成为部署默认（或让共享 uid 也能槽位化），属独立决策。
5. ✅ **门禁（终态树复跑，含 transport 1 之后）**：gate A（chroot）`1054 passed / 2 skipped / 0 failed`、
   gate B（pure）`1046 passed / 3 skipped / 0 failed`、route-B 专题切片容器
   `64 passed`；日志 `tmp/rb-gate-a2.log` / `tmp/rb-gate-b.log` /
   `tmp/rb-macos2.log`（macOS `972 passed / 74 skipped / 0 failed`）/
   `tmp/rb-focused.log`。
   性能：租槽位只落在每沙箱**第一条命令**（in-process 10.63 ms → route-B 57.11 ms，
   +46 ms），稳态 exec 无差异（warm p50 4.29 → 4.35 ms）——`tmp/perf/route-b-first-exec.txt`。
6. ⬜ **生产 spawner 形态**（launcher / 外部槽位池）仍未接：现网 compose（worker
   `user: 65534`）与 k8s（无 CAP_SETUID）两套部署都不满足特权前置 ⇒ auto 档在现网
   仍走进程内后端。这一步是部署决策，不是代码缺口。

## 部署检查表（route-B 上线前逐条确认）

1. **worker 特权**：默认 spawner 用 util-linux `setpriv` 把槽位起在沙箱 host uid 上
   ⇒ worker 必须 root（或注入 launcher `spawner=`）。非 root + `E2B_ROUTE_B=on` ⇒
   建箱即报错（不静默退 route-A）；非 root + `auto` ⇒ 走进程内后端并保留
   `mediation_run_as='supervisor'` 语义（T5 症状仍在）。
   **现网两套部署都不满足**：`docker-compose.prod.yml` 的 worker 是
   `user: "65534:65534"`，`deploy/k8s/worker.yaml` 只加
   `SYS_ADMIN`/`NET_BIND_SERVICE`（镜像默认 USER 也非 root）⇒ 两处 auto 档都保持
   进程内后端。要在生产开 route-B：worker 改 root（+ `CAP_SETUID`/`CAP_SETGID`
   在Capability里显式列出），或按 fork §2/§8 的边界由**外部 launcher/槽位池**起
   槽位（`W1SlotPool(spawner=...)` 注入），worker 自己永不装特权。
2. **`E2B_PER_SANDBOX_UID=true`**：槽位按沙箱自己的 host uid 定向租用；共享 uid
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

## 实现期的修正（原设计没预见的约束）

| # | 事实 | 后果 |
|---|---|---|
| 1 | exec 会话主程序（M0）的 stdio 被 core 固定为 `/dev/null`（`instance.rs::launch_exec_inner`） | 原方案 `read x < /dev/zero` 永远等不到换行 ⇒ dash/bash 各以不同方式为每个 route-B 沙箱跑满一核。停车程序改 `while :; do kill -STOP $$; done`（纯内建、稳态零 CPU、被 SIGCONT 后立刻再停自己）；契约 `test_parked_main_program_costs_nothing` 钉「1 s 墙钟整棵槽位树 ≤2 tick」 |
| 2 | registered 槽位的 serve loop 是**单线程串行 accept**，一次只处理一个 verb | 子进程还活着时发 `wait_child` 会占住整代 ⇒ `RouteBExecProcess.wait()` 先轮询宿主 pid 消失（上限 `CHILD_POLL_CAP_S`）再发 verb；契约 `test_concurrent_commands_share_one_slot_without_stalling` 钉「长命子进程不堵后续命令」 |
| 3 | `--serve-path` / `--control-fd` 都是**先 bind/accept、后 launch** instance | 通道活着 ≠ 能 exec ⇒ `acquire` 用 `stats(launched:true)` 做就绪门 |
| 4 | policy/program 由 worker（root）写、由槽位（uid X）读 | scratch 根与各级目录不能 0700（pytest `tmp_path`、umask 077 都会踩）；文档也不能 0644（里面有 egress 代理口令/secret 路径）⇒ 目录 0755/0711 + 文档 `0440 root:<uid>`，由池强制 |
| 5 | fork 里 `SandboxError` 是 `SandlockError` 的**子类** | 服务端「拒绝」不能被当成「槽位死了」⇒ `request()` 必须先原样抛 `SandboxError`；否则一个策略错误会触发一次无谓的槽位重启 |
| 6 | registered 形态的 channel token 只能走 **argv**（fork F16 只有 transport 2 的语言面）（`/proc/<pid>/cmdline` 0444，**不受** ptrace 门约束） | envd 默认改用 **transport 1（fd handoff）**：fork F17 为 `--control-fd` 通道补了 C ABI（`sandlock_supervise_connect_fd` / `sandlock_supervise_set_timeout` / `sandlock_supervise_check_fd`）与 Python 面，池用 `socketpair()` + `pass_fds` 交付 worker 端 ⇒ 无 socket 路径、无 argv 秘密（SL-10 关闭），并附带「worker 崩溃 → 通道 EOF → 槽位自收口」。持久单流 ⇒ 动词在客户端侧也串行化；verb 超时（`E2B_ROUTE_B_VERB_TIMEOUT_S`，默认 15 s）即退役会话并按死箱处理 |

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
- 单测 `tests/unit/test_route_b_wiring.py` / `tests/unit/test_sandlock_executor_route_b.py`
  （FakePool + 注入 channel，macOS 可跑）：wire 字段表与 fork 对齐、按 uid 定向
  租用与 W1 二次拒绝、就绪门、退役语义、交付的描述符可用性、verb 参数面。
- fork 侧（F17）：`python/tests/test_supervise_channel.py` 同一真槽位跑 fd handoff
  （持久会话、无 token 也能服务、belt 不匹配仍拒、`set_timeout` 允许 park、
  失败动词退役会话、`check_control_fd` 点名坏描述符）。
