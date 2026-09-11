# sandlock 问题索引（内容已并入 sandlock 项目）

**唯一事实源已迁移到 sandlock 仓库内**：
[`third_party/sandlock/docs/e2b-integration.md`](../third_party/sandlock/docs/e2b-integration.md)
（fork 分支 `upstream-pr/netns-free-clean`，文档提交 `afe4921`；代码基线 `be387c7`）。

那份文档同时覆盖：已落地方案（R*/S*/E*/M*）、待实施修改方案（P1–P8）、未解决问题
（SL-1 等）、E2B 侧现有缓解、验证矩阵与同步约定。本文只保留编号映射，避免两处描述漂移。

| 编号 | 主题 | 严重度 | 去处 |
|---|---|---|---|
| SL-1 | 路径中介（`fs_denied` / chroot / COW 触发的 USER_NOTIF）以 **supervisor 身份**代执行 `openat/unlinkat/renameat2/fchmodat/fchownat/…` ⇒ 沙箱写的文件属主变 root、`chmod` 失效、共享目录 1777+sticky 的 per-uid 保护不成立（**非** Landlock 逃逸）。**已由硬删关闭（B3，2026-09-11）**：P2 的 `mediation_run_as` 降级档被整体删除（枚举/字段/builder/FFI 导出/CLI flag/`--policy` wire 键/Python 取值/`stats` 计数），特权中介 + 路径中介形态一律在建箱前 fail-closed 拒绝且只给 route B 一条修法；身份由构造保证（route-B 槽位 euid == 沙箱 host uid，A/B 档硬证据保留） | High（**已闭**） | `e2b-integration.md` §3.1，修法 P1/P5；B3 见 fork `docs/CHANGELOG.md` 顶部与 `docs/production-deployment-requirements.md` §2.4「删档的后果」 |
| SL-2 | `notify_rate_limit` 假告警：字段已生效（`_sdk.py:1217`），但 `_HANDLED_FIELDS`（`_sdk.py:1138` 起）漏登记 | Low | §3.2，修法 P3（一行） |
| **SL-4** | `extra_fds` 落位用 `dup2` ⇒ 清掉 `FD_CLOEXEC`，宿主↔in-sandbox init 的控制 socket（fd 3）被**每一个用户进程继承** ⇒ 沙箱内代码**已实测证实**：伪造 `Exited` ⇒ 宿主 `exec` 返回 0 而目标进程仍在跑；假 `Exited{未知 pid}` ⇒ `early_exits` 无上限（60k 帧 +5.3 MB RSS）。~~发 `Shutdown` 打死整箱~~ 已否证、抢读宿主请求未复现 | High | `e2b-integration.md` §3.9，详见 `sandbox-exec-security.md` §4.1；**exec 接线前必修** |
| **SL-5** | `sandlock-init` 在解析失败/EOF/`RunMain` 分支不关闭收到的 fd，且不校验请求来源、帧边界按字节流猜 | Medium | 同上 §3.9 / §4.2 |
| **SL-7** | 控制协议无鉴权（`SO_PEERCRED` 只告警不拒绝 + 帧无租户绑定）。**实测**：bind `/dev` 后沙箱内可枚举其他沙箱控制目录并对**别人的** control.sock 成功 `config`（返回对端策略）⇒ `exec` verb 的硬阻塞项 | High | 同上 §3.9 / §4.10 |
| **SL-11** | （F17 附带硬化；**非实测缺陷**）fd handoff 的描述符要被 launcher 清掉 `FD_CLOEXEC` 才能跨 `exec` 存活，若 supervise 不重新置位，理论上会把主管自己的控制端留给被 confine 的 `sandlock-init` 及其子进程（SL-4 同族）。实测（2026-09-09，比对 `/proc/<stats.pid>/fd` 与本端 socket inode）：core 目前只按显式 fd 集合交给 init，**未见泄漏**；仍在 `serve_control_fd` 启动实例前无条件 `fcntl(F_SETFD, FD_CLOEXEC)`，并把该不变量钉成 fork 用例（护栏，不是 bug 复现） | Low（防御） | fork `crates/sandlock-supervise/src/serve.rs` |
| **SL-8** | `proc_count` 无退出兜底（唯一归还点是阻塞 `wait4`）⇒ 孤儿永久占配额。**实测**可累加：`1/7→2/8→2/5`、`2/5→3/6→3/5` | High | 同上 §3.9 / §4.7 |
| **SL-6** | `sandlock-init` 无 `waitpid(-1)` 兜底 ⇒ 被收养的孤儿无人回收（实测 oci **无 pid ns** ⇒ 孤儿归外层 PID 1；开 `pid_ns` 后才归 init）⇒ `<defunct>` 堆积 + `proc_count` 记账永不归还 | Medium-High | 同上 §3.9 / §4.13 |
| T4 | `net_isolation` + 镜像 rootfs(chroot) 下 MCP 入站端口映射起不来（纯 sandlock 形态 3/3 通过） | Medium | §3.3，修法 P4 |
| T5 | SL-1 在 chroot 形态的后果面（该形态仍必须整树挂 `/dev`，故 `fs_denied` 不可省） | 随 SL-1 | §3.1 + §4；**2026-09-09 已随 route-B 接线关闭**（chroot 沙箱跑在 euid==host uid 的 supervise 槽位上，T5 strict xfail 已摘） |
| **SL-9** ✅已修（fork F17 `e290059`）| F16 Python 客户端 `sandlock/supervise.py::_take_err_msg` 错误路径必坏：调用点传 `ctypes.byref(err_msg)`（`CArgObject`，无 `.contents`），于是**任何 connect/transport 失败都抛 `AttributeError: '_ctypes.CArgObject' object has no attribute 'contents'`** 而不是 `SandlockError`，服务端错误文本全丢。实测：`21501` 带正确 token 连别人槽位 → `AttributeError`；白名单内 token 错 → 正常 `SandboxError`（只有失败分支坏）。修法：helper 收 `c_char_p` 本体（`cast(byref(m), POINTER(c_void_p))` 取值/释放）+ 两个调用点去掉 `byref` + 一条「refused connect 必须抛 SandlockError 且带服务端文本」回归用例（≈10 行）。E2B 侧已免疫：`RouteBInstance.request` 把 `SandlockError`/`OSError`/`AttributeError` 统一归成槽位失效（`SlotDeadError`），只放行服务端 `SandboxError` | Medium（可诊断性 + 错误分类） | Medium（可诊断性 + 错误分类）→ **已闭（F17 修 + E2B 归类双保险）** |
| **SL-10** ✅已闭（fork F17 + envd 默认 `transport=fd`）| registered 槽位的 channel token 只能经 `--token` **argv** 提供 ⇒ `/proc/<pid>/cmdline`(0444) 本机任意 uid 可读（**实测**：foreign uid 21501 读到 21500 槽位的 `--token <64hex>`；`environ` 0400 才真的读不到）。当前不构成攻击面仅因鉴权顺序 ①`SO_PEERCRED` ∈ `--peer-uid` ②token：实测非白名单 uid 即使**持有正确 token** 也被静默关连接。风险在配置漂移（白名单一旦放进租户可达的 uid 即成真漏洞）与 `ps`/coredump/日志留痕。候选修法（择一，fork 侧）：① `--token-fd N` 由 launcher 经 pipe 送 token（复用已有 `read_policy_fd` 的超时+尺寸闸，最贴合「描述符即凭证」既有语义）；② `--token-env VAR` 从 0400 的 `/proc/self/environ` 面读（envd 已能安全地把 env 传给 setpriv 子进程）；③ token 文件 `<registry>/<name>.d/token` 0400（与实例控制目录 `token` 文件 0600 同形，fork 已有 `token_path`/`read_token`）；④ 给语言面补 transport 1（fd-handoff，token 只作 belt）——F16 明确没做，改动最大但语义最干净 | Low（当前）/ Medium（配置漂移时） | Low（当前）/ Medium（配置漂移时）→ **已闭（改用 fd handoff：argv 与 /tmp 里都没有秘密）** |
| **SL-12** ✅已闭（fork B1 `656bb31` + fix round 1 `f5e1edd`；E2B fix round 2）| 语言面的 create/launch 失败**不带原因**：`sandlock_create` / `sandlock_instance_launch` 只返回空句柄，Python SDK 一律译成 `RuntimeError("sandlock_instance_launch failed")`（`sandbox.py` 全文件十余处同形），Rust 侧那条精心写就的 fail-closed 文本（`mediation_run_as=caller refused: ... route B ...`）在 FFI 边界被丢掉。删掉 `mediation_run_as='supervisor'` 降级档之后，这是运维唯一能看到「为什么建箱被拒」的地方 ⇒ 只剩 `sandlock_create failed`。**已修**：两个入口各有增量符号 `sandlock_create_with_err` / `sandlock_instance_launch_with_err`（复用 supervise 侧 `err`/`err_msg` 约定，成功 `err=0` 且清空槽），Python 面译成 `RuntimeError("<what> failed: <core Display>")`；评审后补：缺符号不再静默（点名 RuntimeError，绝不回落到无原因的旧符号）、closed/dead 改按类型/错误码判定、E2B 侧对「装了但坏」**fail closed**（`auto` 与 `sandlock` 都抛带原因的 `RuntimeError`，只有「包不存在」才允许 auto 回落 LocalExecutor；`local` 不探测）。E2B 建箱前的 disclosure 仍保留作第二道说明 | Medium（可诊断性；fail-closed 默认值失去解释） | ✅ **已闭（F17 式 out 参 + SDK 译码）**，见 fork `docs/CHANGELOG.md`「create/launch 失败带原因」与「B1 fix round 1」 |
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
| [`sandbox-exec-security.md`](../third_party/sandlock/docs/sandbox-exec-security.md) | 每沙箱一实例：`exec` 机制、12 条新增攻击面、会话生命周期状态机与放行门槛 | 📐 分析（M0′ 门槛未清零前不接线） |
| [`upstream-pr-netns-free.md`](../third_party/sandlock/docs/upstream-pr-netns-free.md) | 无特权上游 PR 范围/分支/推送状态 | ⏸ 推送受 token 权限阻塞 |
