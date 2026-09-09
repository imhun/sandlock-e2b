# envd route-B 接线设计（2026-09-09；backlog #5 剩余项）

> 状态：设计定稿 + **W1 槽位管理器已落地（本计划 Task 1）**；executor 全面接线为
> 后续 Task（本文件给出接口与分步）。fork 侧 F16 语言客户端已完成（`6571c36`），
> T5 xfail 的摘除只差 envd 侧走 route-B。

## 背景与目标

T5：镜像 rootfs（chroot）形态下，共享卷写入经 fs_denied 激活的路径中介执行。进程内
root worker 的中介身份 = root ⇒ 文件宿主属主 root、沙箱 `chmod` EPERM、1777+sticky
跨 uid 保护不成立（`tests/contract/test_uid_permissions.py:99` strict xfail）。
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
- `async acquire(sandbox_id, policy_json, program_json) -> SlotHandle`：
  挑空闲 uid（LRU，空闲 = 无在世进程），spawn
  `sandlock-supervise --policy <file> --uid <uid> --serve-path <name> --token <tok>
  --peer-uid <worker_uid> --program <file>`（spawner 可注入：root worker 直接
  setuid；生产 launcher 以文件方式或外部池替换），等待 registered socket 出现；
- `async release(sandbox_id)`：SuperviseChannel `shutdown` + 等进程退出 → uid 空闲
  （W1 原地重启）；
- `acquired_uid(sandbox_id)` / `live_slots()` 供 stats/对账。

命名：slot name = `rb-<sandbox_id>`；token 每次 acquire 重新生成；socket =
`/tmp/sandlock-ctl-<uid>-registry/<fnv1a16(name)>.d/control.sock`（registry 按 uid
隔离，见 fork `control.rs`）。

## 后续接线 Task（未实现，逐项需 executor 内核对齐）

1. `SandlockExecutor` 在 `E2B_ROUTE_B_SLOTS>0` 或 base-image+per-uid 形态下改走
   supervise：`_policy_ceiling()` 的 kwargs → supervise `--policy` 全字段 JSON
   （含 `chroot`/`fs_mount`/`net_isolation`/`port_mappings`/uid/资源上限）；
2. 长命实例的 M0：supervise 需要 `--program`（launch-first）——envd 实例没有主程序
   语义，用停车程序 `["/bin/sh","-c","read x < /dev/zero"]`（纯 shell 内建 + /dev/zero，
   零 CPU），沙箱生命周期 = 槽位生命周期；
3. `start()`（exec + stdio）、PTY、`update_network`、pause/resume（kill_child
   SIGSTOP/SIGCONT 或保持 envd 侧 ProcessManager 语义）、删除/驱逐收口都从
   in-process `SandboxInstance` 迁到槽位 verb 面；`_CommandGate`/并发上限语义不变；
4. 摘 `tests/contract/test_uid_permissions.py:99` strict xfail + 删
   `_mediation_run_as()` 的 supervisor 降级档（`sandlock.py:755-761`）与
   `mediation_downgrades` 计数/WARN；
5. 门禁：route-B 契约（本计划 Task 1 的跨 uid 用例）+ gate A/B/macOS 三档全量。

## 测试（Task 1）

`tests/contract/test_route_b_slot_pool.py`（root + Linux 门控）：W1SlotPool 起两个
uid 槽位，SuperviseChannel exec 各自 workload：X 建文件宿主属主 == X 且自 chmod
生效；Y 对 X 文件 rm/chmod EPERM（1777+sticky）——即 T5 需要的 envd 侧证据。
