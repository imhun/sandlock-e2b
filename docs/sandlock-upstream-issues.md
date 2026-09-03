# sandlock 问题索引（内容已并入 sandlock 项目）

**唯一事实源已迁移到 sandlock 仓库内**：
[`third_party/sandlock/docs/e2b-integration.md`](../third_party/sandlock/docs/e2b-integration.md)
（fork 分支 `upstream-pr/netns-free-clean`，文档提交 `afe4921`；代码基线 `be387c7`）。

那份文档同时覆盖：已落地方案（R*/S*/E*/M*）、待实施修改方案（P1–P8）、未解决问题
（SL-1 等）、E2B 侧现有缓解、验证矩阵与同步约定。本文只保留编号映射，避免两处描述漂移。

| 编号 | 主题 | 严重度 | 去处 |
|---|---|---|---|
| SL-1 | 路径中介（`fs_denied` / chroot / COW 触发的 USER_NOTIF）以 **supervisor 身份**代执行 `openat/unlinkat/renameat2/fchmodat/fchownat/…` ⇒ 沙箱写的文件属主变 root、`chmod` 失效、共享目录 1777+sticky 的 per-uid 保护不成立（**非** Landlock 逃逸） | High | `e2b-integration.md` §3.1，修法 P1/P2/P5 |
| SL-2 | `notify_rate_limit` 假告警：字段已生效（`_sdk.py:1217`），但 `_HANDLED_FIELDS`（`_sdk.py:1138` 起）漏登记 | Low | §3.2，修法 P3（一行） |
| T4 | `net_isolation` + 镜像 rootfs(chroot) 下 MCP 入站端口映射起不来（纯 sandlock 形态 3/3 通过） | Medium | §3.3，修法 P4 |
| T5 | SL-1 在 chroot 形态的后果面（该形态仍必须整树挂 `/dev`，故 `fs_denied` 不可省） | 随 SL-1 | §3.1 + §4 |
| — | 非 root supervisor 无法映射任意 host uid（单 entry userns 结构性限制） | 结构性 | §3.5 |
| — | wheel 与 tip 一致性只能靠重跑自证（符号已核对存在） | 发布流程 | §3.4 |

E2B 侧对应的跟踪条目在 [`task-backlog.md`](task-backlog.md)（`SL-1`、`T1`、`T4`、`T5`）与
[`HANDOFF.md`](HANDOFF.md)「09-03（续）」小节；修好后由 `xfail(strict=True)` 的 XPASS
提醒摘除标记（`tests/contract/test_uid_permissions.py`、`tests/contract/test_mcp_netns.py`）。

## 已迁出的 sandlock 方案文档（现在 fork 仓库 `docs/`）

| 文档 | 内容 | 状态 |
|---|---|---|
| [`sandlock-network-wildcard.md`](../third_party/sandlock/docs/sandlock-network-wildcard.md) | E2B Network API 能力对齐总纲（R1–R14） | ✅ 已落地 |
| [`netns-isolation-fd-injection.md`](../third_party/sandlock/docs/netns-isolation-fd-injection.md) | loopback netns + supervisor fd 注入（ADDFD 无特权 PoC） | ✅ 落地为 S1.1/S2.x；netns 已移出运行时基线 |
| [`sandbox-level-cow.md`](../third_party/sandlock/docs/sandbox-level-cow.md) | 沙箱级 COW（常驻 supervisor）路线评估 | ❌ 已否决，改用 [`sandbox-disk-quota.md`](sandbox-disk-quota.md) |
| [`upstream-pr-netns-free.md`](../third_party/sandlock/docs/upstream-pr-netns-free.md) | 无特权上游 PR 范围/分支/推送状态 | ⏸ 推送受 token 权限阻塞 |
