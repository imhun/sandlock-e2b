# envd route-B 接线设计（2026-09-09；backlog #5 剩余项）

> 状态：**设计定稿 + Task 1（W1 槽位管理器）+ Task 2–4（executor 全面走 supervise、
> 摘 T5 xfail）已落地（2026-09-09）**，Task 5 的三档门禁复跑全绿。
> fork 侧 F16 语言客户端见 `6571c36`。
>
> 落地时推翻/新增了 5 条本文件原先没写的事实，见文末「实现期的修正」——
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

`W1SlotPool`：

- `__init__(uid_start, size, worker_uid=None, supervise_bin=None)`；
- `acquire(sandbox_id, policy_json, program_json, *, uid=None, name=None)`：
  默认挑空闲 uid（LRU，空闲 = 无在世进程）；executor 传 `uid=<沙箱 host uid>`
  定向租用（段外 uid 报错、在世 uid 拒绝二次租用），spawn
  `sandlock-supervise --policy <file> --uid <uid> --serve-path <name> --token <tok>
  --peer-uid <worker_uid> --program <file>`（spawner 可注入：root worker 直接
  setuid；生产 launcher 以文件方式或外部池替换），等 socket 出现后再用
  `stats(launched:true)` 过就绪门（槽位是**先 bind 后 launch**，见修正表 #3）；
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
5. ✅ **门禁（终态树复跑）**：gate A（chroot）`1048 passed / 2 skipped / 0 failed`、
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
4. **`sun_path` 108 字节**：registered socket 固定在
   `/tmp/sandlock-ctl-<uid>-registry/<fnv1a16(name)>.d/control.sock`（不受
   `E2B_ROUTE_B_TMP_ROOT` 影响），该路径长度进部署检查。
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
   **但这仍是必须记录的暴露面**：任何一次 `ps`/coredump/审计日志都会把 token 落到别人眼前，
   且 `--peer-uid` 一旦配宽（比如把某个共享 uid 放进白名单）就立刻变成真漏洞。
   闭口方案见 `docs/sandlock-upstream-issues.md` SL-10（fork 侧需要动，envd 侧已把
   「不是服务端拒绝」统一归类成槽位失效）。

## 测试（Task 1）

`tests/contract/test_route_b_slot_pool.py`（root + Linux 门控）：W1SlotPool 起两个
uid 槽位，SuperviseChannel exec 各自 workload：X 建文件宿主属主 == X 且自 chmod
生效；Y 对 X 文件 rm/chmod EPERM（1777+sticky）——即 T5 需要的 envd 侧证据。
