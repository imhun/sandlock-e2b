# 会话交接记录（2026-08-29）

> **历史基线（2026-08-29；已被下方「M4 收口」块取代，仅存档）**：
> 当时基线：Linux 容器（privileged + host 网络）
> `247 passed, 1 skipped`；macOS `226 passed, 18 skipped`
> （unit + contract + sdk/python + sdk/js + security 跳过项）。

## ⚡ F15（2026-09-08）：控制帧按声明归属描述符（`FRAME_VERSION` 1 → 2，终态 fork `3020ea0` / wheel `3020ea0` 产物）

init 控制通道是 `SOCK_STREAM`：一次 `recvmsg` 可并入多帧，而内核交回的 SCM_RIGHTS
描述符是**一条拼接列表**。旧实现把「本读单元的全部 fd」当成「本帧的 fd」
（`fdrecv::recv(ctl, 3)` + `received.fds[0..3]`），前一帧带 fd 时后一帧的 stdio 整体位移——
两个 `RunExec` 合并进一次读时，exec #2 的 stdout 写进 exec #1 的管道并整条丢失。
修法 = 帧头新增 1 字节 `n_fds`（`FRAME_HEADER_LEN` 10 → 11，`FRAME_VERSION` 1 → 2），
发送侧按实际随 `sendmsg` 交出的 fd 数声明（只有 `RunExec` = 3），接收侧用纯函数
`take_frame_fds` 按声明从读单元队列切分；声明与队列不符 ⇒ 整读单元拒绝；
`MSG_CTRUNC` / `MSG_TRUNC` fail-closed（不再把被内核截断的描述符当完整帧用）。

- **fork 提交**：`c50f407`（F15 RED：两帧一次写出六端，红档证明 exec #2 拿不到自己的三端）、
  `8640223`（fix：`proto.rs` / `fdrecv.rs` / `init/mod.rs` / `executor.rs` / oci 两侧 +
  夹具 + baseline 同 commit）、`3020ea0`（docs：CHANGELOG F15 条目 + e2b-integration §7
  wire 升级约束：`sandlock-supervise` 与 `_sandlock*.so` 必须同批替换，混装 fail-closed 点名版本）。
- **门禁**：fork 11 档全绿 —— core_lib `841`（+4 fd_assignment）/ core_integ `534` /
  ffi `100` / cli `100` / supervise `42` / supervise_cost `3` / cli_build `0` / python `454`，
  root oci `150`（+2 init.rs 头校验，lib+bin 双编译；+1 integration RED 转绿）/
  supervise_root `4` / mediation_2uid `9`（`third_party/sandlock/tmp/sdd/f15-gate-*.log`）。
- **wheel**：cp314 x86_64 + aarch64 重建 @ `3020ea0`，verify 全绿（符号 156=156、RECORD 精确、
  supervise 三方指纹一致、mode 755、`--uid` 拒绝冒烟）；`wheels/fork/` 磁盘产物 sha256：
  aarch64 `9db6ca85…` / x86_64 `48969f2d…`。镜像 `e2b-sandlock-test:latest`
  （`6d208d63`）内 supervise sha256 = manifest x86_64 行 + mode 0755。
- **E2B 三档门禁无漂移**：低 fd 表探针 N=0/1/2/8 全部 `FAILURES: []` + 多探针 4 marker 全绿
  （`tmp/f15-fdcount-*.log` / `tmp/f15-multi.log`）；网关+boxed 契约 2 轮 `2 passed`
  （`tmp/f15-contract-run{1,2}.log`）；**gate A 982 passed / 2 skipped / 1 xfailed(T5) /
  0 failed**（`tmp/f15-e2b-gate-a.log`）、**gate B 982 / 3 skipped / 0**
  （`tmp/f15-e2b-gate-b.log`）、**macOS 916 / 65 skipped / 0**（`tmp/f15-macos.log`）。
- **环境注记**：本机 docker 容器 pid-1（bash）不再及时回收孤儿进程 ⇒ fork 门禁的
  `pgid_entry_survives_leader_exit_with_live_member` 类用例在无 init 容器里必红；本轮 fork
  门禁统一加 `--init`（tini 作 pid1）后稳定绿（红档 `f15-gate-nonroot-r1.log` 另含一条
  cow 并行偶发，单测/串行/两次并行复跑全绿，与 F15 无因果）。
- **T5 前置更新**：本条修复与 T5/F16 无关（F16 仍是 route-B worker 侧 Python 接入面，
  见本计划 Task 9）；T5 xfail 保持。

## ⚡ F16（2026-09-08）：route-B worker 侧语言客户端（fork `6571c36` / wheel `6571c36` 产物）

registered-path 槽位（`--serve-path NAME --token T [--peer-uid UID]...`）的 worker 面
此前只有 Rust（`channel_request_with_fds` 不带 fd 的 `connect_and_request` 之上没有
语言绑定），envd（E2B）当不了 route-B worker。F16 新增：

- **C ABI**：`sandlock_supervise_connect(path, token, err, err_msg)` /
  `sandlock_supervise_request(h, verb, args_json, fds, n_fds, err, err_msg)`（返回
  `ControlResponse` JSON 原文；`exec` 三端 stdio 随帧 SCM_RIGHTS 交付）/
  `sandlock_supervise_free(h)`；错误沿用既有 `err`/`err_msg` 约定。FFI 动态符号
  156 → 159（wheel verify 双向相等随之更新）。
- **Python**：`sandlock.supervise.SuperviseChannel(path, token).request(verb, args,
  fds=()) -> data`；`exec`/`wait_child`/`kill_child`/`update_network`/`shutdown`
  语义全由服务端 Generation 定义；非 ok 响应抛 `SandboxError`，transport 错误抛
  `SandlockError`。
- **T5 所需 Python 可达证据已钉在 fork 侧**：`mediation_2uid` 新增
  `test_python_client_execs_distinct_uids_on_shared_sticky_dir`（10 passed）——
  两个不同 uid 的 registered slot 由 Python 客户端 exec + wait_child + shutdown
  驱动：X 的 exec 建文件宿主属主 == X、自 chmod 生效；Y 的 exec 对该文件
  rm/chmod 均 EPERM（1777+sticky 真语义）。python 档 +1（455，
  `test_supervise_channel.py` 同 uid exec-with-fds 往返）。剩余 T5 动作 =
  envd 接线 + route-B supervise 部署（选 W1/W2）+ 摘 xfail（main backlog #5）。
- **门禁/产物**：fork 11 档全绿（core_lib 841 / core_integ 534 / ffi 101 / cli 100 /
  supervise 42 / supervise_cost 3 / cli_build 0 / python 455；oci 150 /
  supervise_root 4 / mediation_2uid 10；`third_party/sandlock/tmp/sdd/f16-gate-*.log`）；
  wheel cp314 双架构重建 @ 6571c36 + verify 全绿（159=159、RECORD、三方指纹、0755、
  `--uid` 冒烟；`tmp/sdd/f16-wheel-{build,verify}.log`；产物 sha256 aarch64
  `83cfad16…` / x86_64 `db7e0720…`）；E2B 三档无漂移：gate A 982/2/1xfail(T5)/0、
  gate B 982/3/0、macOS 916/65/0（`tmp/f16-e2b-gate-{a,b}.log` / `tmp/f16-macos.log`）。
- **两条部署约束**（见 fork `docs/supervise-identity-handoff.md` §10）：`sun_path`
  108 字节上限（E2B registry 根路径长度进部署检查表）；一 uid = 一个 supervise =
  一代沙箱（复用只能靠重启）。

## ⚡ E2B 侧剩余代码项收口（2026-09-08）：backlog #20 / #13 / #14 / #11

- **#20 OCI 坏镜像源防御**（发布前建议修项，已落地）：① `blob()` 校验层 digest，
  不匹配按可重试错误走下一个 endpoint（坏层不进 rootfs）；② blob 独立 600 s 超时
  （`RegistryClient(blob_timeout=...)`）+ buildkitd mirror 跟随
  `E2B_REGISTRY_MIRRORS`（docker.io 桶；无配置回落 daocloud）；③ challenge 后
  Authorization=None 不再写入（匿名 + Basic-only mirror 不再 TypeError 打断整次
  拉取）。单测 2 条（`tests/unit/test_oci_registry.py`）。
- **#13 本地 snapshot fork × per-sandbox uid 缺口**（已关闭）：snapshots 本地 fork
  分支删重复内联 provision，直接复用 `_provision_local`（create 同路径）——uid 池档
  从此 acquire/apply/commit `host_uid`，register 带 `host_uid`/volume_projects/mcp/
  network/iam，I3 失败 release 保留。单测 `tests/unit/test_provision_local_uid.py`。
- **#14 快照剪枝启发边界**（已加固）：`_holds_snapshots` 有界递归（深度 3），更深层
  marker 完好的嵌入存储整容器剪除；marker 缺失/改名余量保留为文档化边界。单测
  `test_nested_store_markers_are_pruned_within_bounded_depth`。
- **#11 bisect 日志头纪律**（约定已登记，无代码）：复现/bisect 日志带 ENV-HEADER
  （commit/env/镜像/loop/时间）；本轮全部门禁日志已按此执行。
- **最终完整测试（2026-09-08，E2B 修复后三档）**：macOS `921 passed / 65 skipped /
  0 failed`（`tmp/final-macos.log`；916→921 = 新增 5 条单测）、容器 gate A
  `987 passed / 2 skipped / 1 xfailed(T5) / 0 failed`（`tmp/final-e2b-gate-a.log`）、
  gate B `987 passed / 3 skipped / 0 failed`（`tmp/final-e2b-gate-b.log`）——含
  oci_registry/snapshot/provision 新单测与既有快照 fork/模板构建契约回归。
- **仍未做（需用户决策/环境）**：#4（网关启动失败 SDK 可见性 = 产品决策）、
  #5 剩余（envd route-B 接线 = 先选 W1/W2 槽位模型）、T1/O1–O3（真实 XFS/部署窗口）、
  fork Task 10/11（推送/PR/ACR 需授权）。

## ⚡ E3.2 成为部署默认（2026-09-09 晚，per-sandbox host uid 默认开）

`E2B_PER_SANDBOX_UID` 默认 **false → true**。意义：有特权的 worker 从此自动给每个沙箱
一个独立 host uid，于是 chroot（镜像 rootfs）形态的 **route-B 槽位也自动生效**（`auto`
档四条件里最后一条前置补齐）；共享卷的跨租户保护靠真 DAC 成立。

- **不动现网行为**：非 root worker（现网 compose `user: "65534:65534"`、k8s 无
  CAP_SETUID）本就映射不了 uid 也 chown 不动 ⇒ 自动关闭 uid 池、保持 E5.1
  固定身份 + Landlock，只在启动时多一条 WARNING（`envd_service/app.py:PER_UID_NONROOT_WARNING`，
  单测把「非 root 不建池 + 说清楚」钉住）。要真拿到 per-sandbox uid 需给 worker
  root 或 CAP_SETUID/SETGID/CHOWN。
- **容量/成本进文档**：`docs/production-deployment-requirements.md` 新增 §2.4
  （`E2B_UID_POOL_SIZE` = 并发沙箱上限、多 worker 必须不重叠段、每沙箱多一棵
  **不计入** `max_memory`/`max_disk` 的 supervise 进程树、uid 只在支持属主的文件系统上有意义、
  显式 `false` 才回到共享 uid 1000 形态），compose 里同段注释。
- **翻默认翻出来的三处，全部改掉**：
  1. 5 条启动日志单测（quota 系列）原本钉死「非 root worker 的 WARNING 列表」，
     现在多一条 E3.2 披露 ⇒ 改成 `_uid_disclosure()`（root 档 []、非 root 档恰好那条），
     两档都精确；
  2. `test_sandbox_lifecycle_rebuild` 手工 register（没有 host_uid）与「有 per-sandbox
     uid 却没分配」的新默认冲突 ⇒ 显式钉 `per_sandbox_uid=False`（它测的是 exec 失败语义），
     并补 route-B 版对照契约 `test_missing_binary_exits_127_through_the_slot`（实测槽位
     同样 exit 127 且无输出，与进程内一致）；
  3. `test_pure_shape_workspace_ownership` 原本硬编码属主 1000 ⇒ 改成形状无关但同样精确：
     目录与沙箱写出的文件同属一个身份、非 root、0700（chown 而非放开权限），
     且该 uid 必须是 worker 记录里的 host_uid 或旧共享档的 1000。
- **PTY 契约顺手变严**：`test_pty_sandlock` 旧断言钉「四种交错顺序之一」，既没证明
  resize 到达子进程，也会被合法的另一交错绊倒（route-B 下 shell 的「无控制终端」banner
  与首个提示符 `# `/`$ ` 位置不同）。改成「每一片恰好出现一次」+ `stty size`→`40 120`
  的到达证明，两档（进程内 / 槽位）都过。
- **⚠️ 实测出一个语义差异，未擅自改（要用户拍）**：route-B 沙箱**内**不再是 root
  （in-process 是「ns 内 root、宿主为 X」；槽位本来就是 X，core 因此不建 userns、
  不映射 `0 → X`）。文件属主/T5 两侧一致，差别在客体内 `apt-get`/`chown`/bind :80
  这类用法。要在 route B 复原 in-guest root，fork 侧让槽位自 `unshare(CLONE_NEWUSER)`
  + 写 `0 X 1` 即可（可行性已实测：`tmp/unprivileged_userns_probe.py` 以 uid 21850
  成功映射，`in-ns euid: 0`）。见计划文档「实现期的修正」#7。
- **门禁（终态，默认开之后）**：gate A `1057 passed / 3 skipped / 0 failed`
  （`tmp/e32-default-gate-a.log`）、gate B `1056 / 4 / 0`
  （`tmp/e32-default-gate-b.log`）、macOS `979 / 77 / 0`（`tmp/rb-e32-macos.log`）。
  中途 r3/r4 的红（7 条、1 条）就是上面 1–3 与 PTY 那条，全部按上述方式收口。

## ⚡ route-B transport 1：token 从 argv 消失（2026-09-09，SL-10 闭口 / fork F17）

上一块留的「要彻底闭口需 fork 提供 token-by-fd/env」按**给语言面补 transport 1（fd
handoff）**实现：route-B 槽位的凭证现在是**一条继承来的 unix 描述符**，argv 里没有
`--token`，`/tmp` 里也没有注册 socket（`sun_path` 108 字节约束随之消失）。

- **fork F17**（`e290059` + `f20d034` + `c0f7bf5`，未推送）：
  - C ABI 新增 `sandlock_supervise_connect_fd(fd, token, err, err_msg)`（取 fd 的私有
    dup 作**持久会话**；`token` 可为 NULL）、`sandlock_supervise_check_fd(fd)`（交付前
    预检 open/`SOCK_STREAM`/`AF_UNIX`）、`sandlock_supervise_set_timeout(h, ms, ...)`
    （`0`=一直等）；`sandlock_supervise_request` 签名不变、按 handle 形状分派 ⇒ Python
    面只有一个类：`SuperviseChannel(fd=..., token="", timeout_ms=...)` +
    `check_control_fd(fd)`。FFI 动态符号 **159 → 162**。
  - 持久单流两条纪律在 Rust 侧：handle 内互斥锁串行所有 verb；任一 verb 失败即
    **退役会话**（后续调用点名 `frame alignment`）⇒ 新会话默认仍是 fail-fast 的
    `CHANNEL_REQUEST_TIMEOUT`（提为常量 + `channel_request_with_fds_timeout`），
    要 park 必须显式 `set_timeout(0)`。
  - **修 SL-9**：`_take_err_msg` 对 `ctypes.byref(...)` 取 `.contents` ⇒ 每个
    transport 失败都抛 `AttributeError` 并吞掉服务端文本；改为按地址读+释放。
    E2B 侧同时加免疫（非服务端 `SandboxError` 的通道失败一律归成 `SlotDeadError`）。
  - **附带硬化**：`serve_control_fd` 在启动实例前把 `FD_CLOEXEC` 置回（否则主管自己的
    控制端可能被 `sandlock-init` 及以下继承 = SL-4 同族）。**实测本树未观察到泄漏**
    （fix 前后 `/proc/<stats.pid>/fd` 比对结果相同），所以这是护栏不是 bug 复现，
    已钉成 fork 用例。
- **envd 侧**：`W1SlotPool` 默认 `transport="fd"`（`socketpair()` + `pass_fds` 同号交付
  `--control-fd N --serve`）；新开关 `E2B_ROUTE_B_TRANSPORT=fd|path`、
  `E2B_ROUTE_B_VERB_TIMEOUT_S`（默认 15 s，动词超时即退役会话 ⇒ 按死箱重启一次）；
  池缓存键含 transport；`--peer-uid` 只在 `path` 形态相关。
  **白得的收口保证**：worker 崩溃 ⇒ 通道 EOF ⇒ 槽位按 `finish()` 异常收口自杀
  （registered 形态下 socket 比 worker 活得久）。
- **两条被实测纠正的判断（都写进文档与注释）**：
  ① 先前记「跨 uid 读 `/proc/<pid>/cmdline` 需要 ptrace 权限」是**错的** —— cmdline 0444
  且不受该门约束（`environ` 才 0400），这正是 SL-10 值得修的理由；
  ② 「槽位随 worker 死掉即消失」不能用 `/proc/<pid>` 存在性判定 —— 容器 pid 1 未必及时回收
  孤儿，退出的槽位会以 **zombie** 形式保留 `/proc` 条目（fork 门禁同一条环境注记）。
  契约改成 `_proc_state()`：running / zombie / gone 三态，断言「不再 running」+ 整棵树。
- **踩到并修掉的构建链陷阱**：`deploy/scripts/build-sandlock-wheels.sh` 跑的是
  `third_party/sandlock-wheel-builder/Dockerfile` —— 那是 **F2b.5 之前**的旧配方，
  产出的 wheel **不含 `sandlock/bin/sandlock-supervise`**（本次实测 2.2 MB vs 正确 7.4 MB），
  而且**退出码 0**：装上后 route-B 只会静默退回进程内后端。现该脚本改为委托
  fork 的 `python/build-wheels.sh`（它同批 cross-build supervise、注入 wheel、
  写 HEAD 钉住的 `SHA256SUMS.supervise`，缺任何一件**就地报错**），旧配方文件头标
  SUPERSEDED。wheel 复验：`162 == 162` 双向符号相等、supervise 指纹与 manifest
  一致、mode 755、`--uid` 拒绝冒烟过。
- **门禁（终态）**：fork 非 root 8 档 core_lib 841 / core_integ 534 / ffi 101 / cli 100 /
  supervise 42 / supervise_cost 3 / cli_build 0 / **python 461**；root 三档 oci 150 /
  supervise_root 4 / mediation_2uid 10（全部「matches baseline」）。
  E2B（同一棵树 + 同一批 wheel）：gate A `1054 passed / 2 skipped / 0 failed`
  （`tmp/rb-f17r2-gate-a.log`）、gate B `1053 / 3 / 0`（`tmp/rb-f17r2-gate-b.log`）、
  route-B 专题切片（槽位池 + executor 契约 + T5 + 两份单测）**`70 passed`**
  （`tmp/rb-f17-focused.log`）、macOS `977 passed / 75 skipped / 0 failed`
  （`tmp/rb-f17-macos2.log`）。r1 一轮（加 fd-client 守卫之前）在
  `tmp/rb-f17-gate-a.log` / `tmp/rb-f17-gate-b.log`，同样 0 failed。

## ⚡ executor 全面走 supervise（2026-09-09，route-B 接线收口 / backlog #5）

chroot（image-rootfs）形态的沙箱现在跑在**每沙箱一只 `sandlock-supervise` 槽位**上
（euid == 该沙箱 host uid），路径中介不再是 root worker 进程 ⇒ T5（代打开文件属主
变 root、1777+sticky per-uid 卷保护失效）在 E2B 侧构造性消失。
`tests/contract/test_uid_permissions.py` 的 **strict xfail 已摘**。

- **开关**：`E2B_ROUTE_B=auto|on|off` + `E2B_ROUTE_B_SLOTS`（>0 亦为强开信号）+
  `E2B_ROUTE_B_TMP_ROOT`。`auto` 只在「root worker + `E2B_PER_SANDBOX_UID` + 已分配
  host_uid + chroot 形态 + wheel 带 supervise」成立时启用；显式 `on`/`SLOTS>0` 而前置
  不满足 ⇒ 建箱直接报错（route A/B 是部署决策，绝不静默降级）。
- **实现**：`_build_instance_policy()` 拆出 `_policy_ceiling()`（kwargs）→
  `route_b.supervise_policy_document()`（`fs_mount` 转 `VIRT:HOST`、丢 `None` 与
  `mediation_run_as`、未知字段按名拒绝；字段表由单测与 fork
  `policy.rs::POLICY_FIELDS` 逐名钉住，53 项）；`route_b.RouteBInstance` /
  `RouteBExecProcess` 复刻 `SandboxInstance`/`ExecProcess` 面
  （exec/wait_child/kill_child/update_network/shutdown），executor **只剩一条代码路径**；
  建槽/收槽走 `asyncio.to_thread`（不阻塞事件循环），`_CommandGate` 语义不变。
- **实现期推翻的两条设计**（详见计划文档「实现期的修正」表）：
  ① 停车程序不能用 `read x < /dev/zero` —— exec 会话 M0 的 stdio 被 core 固定为
  `/dev/null`，dash/bash 都会为它跑满一核；改成 `while :; do kill -STOP $$; done`
  （契约 `test_parked_main_program_costs_nothing` 钉「1 s 墙钟整棵槽位树 ≤2 tick」）。
  ② 槽位必须**按沙箱自己的 host uid 定向租用**（workspace 已按该 uid chown 0700，
  换 uid 的槽位连自己沙箱目录都进不去）⇒ route-B 天然要求 per-sandbox uid；
  uid 台账加锁、进程未确认退出前不归还 uid。
- **另外三条硬约束**（都进了代码注释 + 部署检查表）：registered 槽位是**单线程串行
  accept** ⇒ `wait()` 先轮询宿主 pid 消失再发 `wait_child`（`CHILD_POLL_CAP_S` 兜底）；
  `--serve-path` 先 bind 后 launch ⇒ `acquire` 用 `stats(launched:true)` 做就绪门；
  fork 里 `SandboxError ⊂ SandlockError` ⇒ 服务端「拒绝」必须先原样抛，否则策略错误
  会被误判成「槽位死了」并触发无谓重启。
- **踩到的环境坑（已在池里根治）**：scratch 根若为 0700（pytest `tmp_path`、
  umask 077 都会）则槽位读不到自己的 policy（`Permission denied`），而 policy 里有
  egress 代理口令/secret 路径 ⇒ 池现在强制 目录 0755/0711 + 文档 `0440 root:<uid>`。
- **route-B 反而更强的一点**：`kill_child` 带信号号 ⇒ route-B 子进程
  `supports_signal_pause=True`（SIGSTOP 真停，进程内后端仍 False/SIGKILL-only）。
- **口径更正（重要，实测推翻本块上一版的一句结论）**：本块初版写「token 在 argv 里，
  但跨 uid 读 `/proc/<pid>/cmdline` 需要 ptrace 权限 ⇒ 租户读不到」—— **错的**。
  特权容器实测（`tmp/rb_token_probe.py`）：`cmdline` 是 0444 且**不走** ptrace 门
  （只有 `environ` 0400 被挡），foreign uid 21501 直接读出 21500 槽位的
  `--token <64hex>`。真正挡住攻击的是 registered 路径的鉴权顺序
  ①`SO_PEERCRED` ∈ `--peer-uid`（不在名单里静默关连接）②token —— 实测非白名单 uid
  **即使带正确 token** 也拿不到任何 verb，连沙箱自己的 uid 都被拒。
  所以结论从「不是暴露面」改成「**是暴露面，但被 peer 白名单兜住；配置漂移即成真漏洞**」，
  登记为 fork 侧 SL-10（`--token-fd` / `--token-env` / token 文件三选一）。
- **顺带抓到 fork 一个真 bug（SL-9）**：F16 Python 客户端 `_take_err_msg` 的错误分支
  必坏（调用点传 `ctypes.byref(...)`，helper 却取 `.contents`）⇒ 任何 connect/transport
  失败抛 `AttributeError` 而不是 `SandlockError`，服务端错误文本全丢。
  envd 侧已免疫：`RouteBInstance.request` 把 `SandlockError`/`OSError`/`AttributeError`
  一并归类成 `SlotDeadError`（只放行服务端 `SandboxError`），单测
  `test_a_client_side_channel_failure_is_not_a_policy_refusal` 钉住。
  fork 侧修法（≈10 行 + 1 条回归用例）与影响见 `docs/sandlock-upstream-issues.md` SL-9。
- **仍未删**：`mediation_run_as='supervisor'` 降级档 —— 它现在是「route-B 不可用」
  （现网默认无 per-sandbox uid；prod compose worker `user: 65534`、k8s worker 无
  CAP_SETUID）时 chroot 形态唯一逃生门；删档要先让 per-sandbox uid 成为部署默认，
  属独立决策（backlog #5 已记）。
- **新增测试**：单测 `tests/unit/test_route_b_wiring.py`（28）+
  `tests/unit/test_sandlock_executor_route_b.py`（23，FakePool/FakeChannel 注入，
  macOS 可跑）；契约 `tests/contract/test_route_b_executor.py`（7，root+Linux 实跑：
  子进程 uid、文件属主+自 chmod、停车零 CPU、PTY 尺寸回读、SIGSTOP/信号退出码 -1、
  close 后 uid 干净可复用、**单槽位并发命令不互堵**）。
- **终态门禁（同一棵树复跑）**：gate A（chroot，base=python-mcp:3.14，concurrency=2、
  strict skips、netns 开）`1048 passed / 2 skipped / 0 failed`（`tmp/rb-gate-a2.log`；
  本轮早先一次 `1047/2/0` 见 `tmp/rb-gate-a.log`，差额 = 中途新增的那条「单槽位多命令
  不互堵」契约）；gate B（pure）`1046 passed / 3 skipped / 0 failed`
  （`tmp/rb-gate-b.log`）；macOS `972 passed / 74 skipped / 0 failed`
  （`tmp/rb-macos2.log`）；route-B 专题切片（槽位池 2 + executor 契约 7 + T5 4 +
  两份 route-B 单测 51）容器实跑 `64 passed`（`tmp/rb-focused.log`，同一终态树）。
  **T5 从此在两份门禁日志里都不再出现 xfail**。
  口径说明：gate A2/B 起跑后本树只发生过 **注释与一个未用 import 的删除**
  （`from dataclasses import …, field`），不动行为；macOS 全量与
  `tmp/rb-focused.log` 切片则直接跑在终态字节上 ⇒ 三份数字对终态 tip 都成立。
- **性能（必录）**：租槽位只发生在每沙箱第一条命令 ——
  in-process first-exec `10.63 ms` → route-B `57.11 ms`（+46 ms：spawn supervise +
  launch 一代 + 等 registered channel），稳态 exec 无差异
  （warm p50 `4.29 → 4.35 ms`，n=20）；证据 `tmp/perf/route-b-first-exec.txt`。
  另注意 route-B 的 `max_lifetime=None` + 常驻 M0 ⇒ core 的 15 min idle reclaim
  不再触发，沙箱回收完全由 envd 生命周期（TTL/evict/`ctx.shutdown`→`close()`）驱动。

## ⚡ envd route-B 接线起步（2026-09-09）：W1 槽位管理器 + envd 侧 T5 证据

- **W1 已定**（沿用 2026-09-04 决策；窗口 = 同时在世槽数 N；W2 为可选升级）。
- `envd_service/route_b.py`：`W1SlotPool` —— 固定不重叠 uid 段；`acquire` 挑空闲
  uid 并 spawn `sandlock-supervise --serve-path --token --peer-uid <worker> --program`
  （setpriv 包装，同 fork root 档形态；registry 按 uid 隔离），等 socket 出现；
  `release` 发 shutdown + 等退出 → uid 回池（W1 原地重启）；可注入 spawner 供生产
  launcher。
- 契约 `tests/contract/test_route_b_slot_pool.py`（root + Linux 门控）**2 passed**
  （容器实跑）：两个不同 uid 槽位经 `SuperviseChannel` exec —— X 建文件宿主属主 X、
  自 chmod 生效；Y 对该文件 rm/chmod 均 EPERM（1777+sticky 真语义）；W1 uid 复用/
  耗尽语义。这是 T5 在 envd 侧的硬证据（fork `mediation_2uid` B档 的 Python 复刻）。
- **最终门禁（2026-09-09，route-B 基础落地后）**：macOS `921 passed / 67 skipped /
  0 failed`（`tmp/final2-macos.log`）、gate A `989 passed / 2 skipped /
  1 xfailed(T5) / 0 failed`（`tmp/final2-e2b-gate-a.log`；r1–r3 红 =
  `worker_nonroot` rootfs `/bin/echo` 缺失，VM 磁盘水位 ~91% 下的 docker export
  抖动——单跑/route_b+worker 配对/contract+perf+sdk 切片均绿，释放 14GB 后复跑全绿，
  与本次改动无因果）、gate B `989 passed / 3 skipped / 0 failed`
  （`tmp/final2-e2b-gate-b.log`）。
- 剩余 = executor 全面接线（设计 + 分步清单见
  `docs/superpowers/plans/2026-09-09-envd-route-b-wiring.md`「后续接线 Task」）：
  policy→supervise JSON、M0 停车程序、exec/PTY/update_network/pause/收口 verb 面迁移、
  摘 xfail、删 supervisor 降级档。等用户决策/资源的主要是生产 spawner 形态与完整
  executor 迁移的验收窗口。
  **（本块的「剩余」已在上方「executor 全面走 supervise」块完成 —— 除删 supervisor
  降级档与生产 spawner 两项，见该块与 backlog #5。）**

## ⚡ 修复回合 2（2026-09-08）：FUP-23 根因闭环 + 修复，FUP-14 重新上线（终态 fork `e045881` / 代码 `880a1ec` / wheel `d5cab47` 产物）

上一块的「回退 FUP-14 缓解」已被**真修复**取代。成对探针加上六个快照点
（父端送出 → init `fdrecv::recv` 返回 → 装配前 → `dup3` 前 → `fork` 前 →
`fork` 后 init 自身 → 子进程第一条指令）用 `(O_ACCMODE, st_dev, st_ino)` 取身份、
由子进程经**自己的 stdout 端**回送，彻底堵住「两端采到不同次 exec」的测量漏洞。
结果：**前五个点全对，只有子进程第一条指令处第 3 端已是另一条管道的读端**（其写端在
子进程 fd 8；marker 管道实验证明父子 fd 表不共享）⇒ SCM_RIGHTS 收发链路清白，
是**外部方在 `fork()` 与子进程第一条指令之间往新生儿低号位装描述符**（E2B 宿主形态特有）。
「客户端多开 1 个 fd 就恢复」被证伪为假象：损坏每次都在，只是从 stdout 槽挪到 stderr 槽。

- **修法（fork `880a1ec`，双保险）**：① init 在 `fork()` **之前**把三端 `dup3` 到保留号段
  `EXEC_STDIO_BASE = 64`（三个保留号必须全空才搬，否则整体退回原号 —— `dup3` 会静默覆盖
  占用号，毁掉别人还持有的描述符比本竞态更糟；`RLIMIT_NOFILE` 低于该号段时同样退回 ⇒
  不可能劣于修复前）；② 子进程 dup2 前逐槽校验身份，被换端 ⇒ **拒绝装配**，消息写到仍完好的流
  并以退出码 **124** 结束该 exec（新增语义，与 125 chdir / 126 setpgid / 127 execvp 并列）；
  ③ 父进程 fork 后关闭保留副本、子进程关闭保留号与原始接收号（不漏描述符、不破坏宿主侧 EOF）。
- **FUP-14 重新上线**：signalfd 事件化 reap 的回退（`bb1cb42`）撤销，exec 往返 p50
  **101.75 → 5.35 ms** 收益回来；本轮全部验证都在「FUP-14 + FUP-23 修复」同一棵树上做。
- **取证与复现（E2B 侧）**：`tmp/f11_fdcount_probe.py N` 的 **N=0 / 1 / 2 / 8 全部
  `FAILURES: []`**（此前 N=0 必红）；`tmp/f23_multi_probe.py 0 4` 四条不同 marker 连发各得
  自己那条 stdout；gateway 探针 4/4、thread 探针 GREEN（`tmp/perf/f23c-*.log`）。
  fork 夹具：core_lib +4（搬迁与身份 / 占号退让 / 三端精确不串流 / 换端拒装配）、
  root 档 oci +1（真 `run_init` 控制环 40 轮 exec：逐轮输出精确 + init fd 表逐轮回基线）。
- **终态门禁（fork tip `e045881`，代码 tip `880a1ec`，非 root 8 档 + root 三档 + wheel + E2B 三档）**：
  fork `837 / 534 / 100 / 100 / 42 / 3 / 0 / 454` + `145 / 4 / 9`
  （`third_party/sandlock/tmp/sdd/f23b-gate-{nonroot,root}-final.log`）；
  wheel 双架构 verify 全绿（符号 156=156、RECORD 精确、supervise 三方指纹一致、mode 755、
  `--uid` 拒绝冒烟；`tmp/sdd/f23c-wheel-build.log`）；镜像 `959c9e8d383f` 内 supervise
  sha256 = manifest x86_64 行 + mode 0755 + `landlock_abi 8`；
  **gate A 982 passed / 2 skipped / 1 xfailed(T5) / 0 failed**（`tmp/f23c-e2b-gate-a.log`）、
  **gate B 982 / 3 skipped / 0**（`tmp/f23c-e2b-gate-b.log`）、**macOS 916 / 65 skipped / 0**
  （`tmp/f23c-macos.log`）。不变量：wheel = 代码 tip `880a1ec`（`880a1ec..e045881` 为 docs-only，
  `git diff --stat 880a1ec..HEAD -- crates Cargo.toml Cargo.lock` 为空 ⇒ 按既定约定不重钉），
  子模块指针 = fork HEAD `e045881`。
- **两条新的环境教训（已写进 fork `scripts/test-all.sh` 头与 §5）**：
  ① **门禁必须从短路径跑** —— 嵌套 git worktree 里 `repo_tmp_dir()` 变长，supervise 注册套接字
  路径超过 108 字节 `sun_path` 上限 ⇒ `test_supervise_path_serve_...` 假红（15 s 超时），
  在 fork 根 `/src` 跑即绿（0.4 s）；② **gate B 档必须带 `E2B_MAX_CONCURRENT_COMMANDS_PER_SANDBOX=2`**
  —— 漏传会让兄弟命令串行化、holder 30 s 后释放配额，`test_boxed_memory_quota_denies_sibling_overcommit`
  以「DID NOT RAISE」假红（首档红档留 `tmp/f23c-e2b-gate-b-r1.log`，补 env 复跑即 982/3skip/0）；
  ③ 批量复跑前 `losetup -D`（本轮起跑前仍有 18 个泄漏 loop）。
- **仍未做（单独排期）**：init 控制通道 SOCK_STREAM 多帧合并时「本读单元全部 fd 当本帧 fd」
  的独立协议缺陷（候选补丁存档 `tmp/fup23-candidate-frame-fd-count.patch`，需 bump
  `FRAME_VERSION`）；本因与它无因果（实测排除）。

## ⚡ 修复回合 1（已被上面的修复取代）：回退 FUP-14 消除 FUP-23 用户可见故障（2026-09-08，wheel `0770e59`）

上一块收口后，网关+命令探针从「4/4 全绿」翻红（命令 stdout 整条丢失，CPython
`exit 120`）。两步取证钉死触发方：① 判别变量 = 承载沙箱的进程 fd 表是否只剩
0/1/2（多开 1 个 fd 即恢复正常）；② 同一测试镜像只热替换 debug `.so` 做 A/B ⇒
本波之前的 `4d5f385` 绿、FUP-14 `7671240` 红。⇒ **本波的 signalfd 事件化让
fork 里一处潜伏的 stdio fd 号敏感变成可达**（记为 FUP-23 / 本文 #22）。

- **处置**：fork 以 `bb1cb42` **回退 FUP-14**（≈19× 的 exec 往返收益一并撤回，
  `supervise_cost` 预算回到 200/300/2000 ms），`d9b379c`/`0770e59` 记录取证、缓解、根因与 §5 终态行；
  FUP-14 重新 open。回退后同一探针 N=0 场景 `FAILURES: []`，五项签名逐字回归：
  `list_tools == ['echo']`、网关后命令 exit 0 / `post-gateway-ok\n`、450M 超卖
  exit 137 / stdout `''`、50M 控制命令 exit 0 / `got 50\n`、`memoryMB == 1024`。
- **终态门禁**（wheel = fork tip `d9b379c`，全部 ENV-HEADER 留档）：
  fork 非 root `core_lib 833 / core_integ 534 / ffi 100 / cli 100 / supervise 42 /
  supervise_cost 3 / cli_build 0 / python 454` + root `oci 144 / supervise_root 4 /
  mediation_2uid 9`（`third_party/sandlock/tmp/sdd/f23-gates.log`）；
  wheel verify 全绿（`f23-wheel-verify.log`）；E2B **gate A 982 passed / 2 skipped /
  1 xfailed(T5) / 0 failed**（`tmp/f23-e2b-gate-a.log`）、**gate B 982 / 3 skipped /
  0 failed**（`tmp/f23-e2b-gate-b-rerun.log`）、**macOS 916 / 65 skipped / 0 failed**
  （`tmp/f23-e2b-macos.log`）、thread 探针 GREEN、gateway 探针 **4/4 GREEN**
  （`tmp/perf/f23-gateway-probe-run{1..4}.log`）；镜像 `caaed60847f3` 内 supervise
  = manifest x86_64 行、mode **0755**（`tmp/f23-e2b-image.log`）。
  不变量保持：fork HEAD == wheel manifest HEAD == 子模块指针 == `0770e59`
  （`d9b379c`→`0770e59` 只是 §5 文档行的增量，重建产物四份 sha256 逐个相同 ⇒ 门禁/探针证据继续成立）。
- **根因仍 open**：为什么 init 多占一个低位 fd 就能让子进程 fd 1 不可写，还没有
  fork 侧可控 RED（cargo/pytest 进程都持有几十个 fd，落不进危险号段 ⇒ 门禁看不见）。
  RED 姿势与候选修法（搬迁下界避开保留 fd 号段、`dup2`/`close` 失败点名退出码）记在
  fork `docs/fork-plan-followups.md` FUP-23；重做 FUP-14 时必须与它一起验证。
- **顺带发现并已排除的另一个缺陷**：init 控制通道是 SOCK_STREAM，一次 `recvmsg`
  可并入多帧，而 SCM_RIGHTS 描述符是一条拼接列表，现有代码把「本读单元全部 fd」
  当成「本帧的 fd」，且不看 `MSG_CTRUNC` ⇒ 前帧带 fd 时后帧 stdio 整体位移。它与本次
  故障无因果（实测排除），候选补丁（帧头声明 fd 数 + 按声明分配 + CTRUNC
  fail-closed + 4 条纯函数单测）存档 `tmp/fup23-candidate-frame-fd-count.patch`
  （fork 同步一份 `tmp/sdd/fup23-wip-frame-fd-count.patch`）；因要 bump
  `FRAME_VERSION`，单独排期验证，未随本回合上车。
- **两条环境教训**（已记 #20）：公共镜像源当日不可用（见下块）；**本轮还发现
  loop 设备泄漏** —— 反复跑 test-runner 后 VM 内积累 293 个 loop，导致一次 gate B
  出现 10 个 `XFS 门禁` error（strict skips 把「环境不满足」如实判错，不是掩盖）；
  `losetup -D` 释放后复跑即 982/3skip/0。以后批量复跑前后各查一次 `losetup -a`。

## ⚡ A/B cleanup 剩余任务收口（2026-09-07，fork FUP-11 硬化 + wheel 重钉 + E2B 三档复跑；**wheel 已被上面的回退版本取代**）

计划 `docs/superpowers/plans/2026-09-07-ab-cleanup-remaining.md`（Task 0–5 全部走完，
未推送）。fork 侧提交链（`upstream-pr/netns-free-clean`，本地）：`1bd3b82`
registered slot 日志节流 → `8e65476` 错误面精确断言 + remap 常量/validate-exit 覆盖
→ `eadd383` foreign-uid slot 节流与预算契约 → `d054c11` docs 关闭 FUP-11 →
`6b76e71` **wheel verify 口径修正** → `603b546`/`ee66234` docs（§5 两行 +
FUP-23 登记）。

- **FUP-11 六项全关**（fork 最后一个 open 代码项）：1a supervise 的 13 处 error-path
  `contains` 断言清零转整行/整串（唯一留白 = OS 分配 fd 号与 elapsed 计数）；1b
  `FORBIDDEN_RUNTIME_MEDIATOR_REMAP` 获得测试引用（常量原文 + CLI 无任何运行期 remap
  flag + registered path 也钉 `unknown verb: map-uid`）；1c registered slot 异常连接
  日志改 `AbnormalEndLog`（首条点名 + 每 256 条一条带累计数；root 档实测 300 条被拒
  连接 ⇒ 恰好 2 行）；1d 120 s connect 重试 vs 30 s verb I/O 提为命名常量 + 取舍注释
  + 契约单测；1e 非 root registered path settle 补 `proc_count_vs_live == 0`；1f
  validate-and-exit × `--program` 审计＝模式仍在并补 3 例。计数 supervise 36→42、
  supervise_root 3→4。fork 门禁：非 root 8 档 + root 三档全绿
  （`third_party/sandlock/tmp/sdd/f11-gate-nonroot-final.log` /
  `f11-gate-root-final.log`；首轮 cli `learn` 外部 HTTPS flake 按 FUP-09 留红档另跑）。
- **wheel**：最终 tip 重建 cp314 双架构 + supervise 注入，verify 全绿（FFI 156=156
  双向、RECORD 精确、指纹三方一致、wheel 内 mode 755、`--uid` 冒烟点名双 uid）。
  体积 10.4/9.5 MB → 8.3/7.4 MB（FUP-15 `panic=abort`+`strip`）。**本波抓到并修掉
  verify 自身的假失败**：`sandlock-dev` 无 `unzip` ⇒ 回退 `python3 -m zipfile -e`
  不还原 unix mode，任何正确 wheel 都会被判 0644 红；改为以 wheel 中央目录记录的
  mode 为权威（= pip 安装依据）。文档提交后重钉 manifest HEAD，重跑构建产物
  **逐字节一致**（四份 sha256 全等，仅 HEAD 行变化）⇒ fork HEAD == wheel manifest
  HEAD == 子模块指针 == `ee66234`；三次重建（`d054c11`/`603b546`/`ee66234`）双 wheel 与双
  supervise 的 sha256 **逐个相同** ⇒ 文档提交不动产物，门禁/探针证据对终态 tip 仍成立。
- **E2B 复跑**（main `8ae1a40`/`8c5b50f`，镜像 `39ed2a82b08b`）：pip 真机落
  `-rwxr-xr-x`（0755）且镜像内 supervise sha256 = manifest x86_64 行（FUP-16 遗留
  的「pip 真机 0755 未直接验证」闭环）；thread 探针 GREEN（exec B exit 0 /
  `b-ok\n`，`tmp/perf/f11-thread-probe.log`）；**full gate A 982 passed / 2 skipped /
  1 xfailed(T5) / 0 failed**、**gate B 982 passed / 3 skipped / 0 failed**、
  **macOS 916 passed / 65 skipped / 0 failed** —— 三档与 F12–F14 收口档逐项相同，
  无漂移（`tmp/f11-e2b-gate-a.log`＋前四档 `-r1..r5`、`tmp/f11-e2b-gate-b.log`＋
  `-r1-with-scratch-test.log`、`tmp/f11-e2b-macos-r2.log`）。
- **⚠ 本波暴露的真实回归（FUP-23 / 本文 #22，未修，升级前必读）**：网关+命令探针
  （`tmp/f11_fup3_probe.py`）从上一波「4/4 全绿」翻为本轮「任何写 stdout 的命令都
  `exit=120`/`stdout=''`」。两步二分定性：①只在**承载 harness 的客户端进程 fd 表只剩
  0/1/2**（下一个可用 fd=3）时必现，预先多开 1 个 fd 即全绿；②同一镜像只热替换 debug
  `libsandlock_ffi.so` 做 A/B ⇒ 本波之前 tip `4d5f385` 绿、FUP-14 `7671240` 红
  ⇒ **本波使潜伏缺陷可达**（非 FUP-11 引入）。子进程侧 `/proc/self/fd/1` 存在但
  `write` 失败 ⇒ exec stdio 装配的「搬到 ≥3」下界与桩/控制通道固定低位号
  （`CONTROL_FD = 3` + READY/GO）可重叠，被 dup2/dup3 覆盖后 fd 1 不可写；FUP-14 新增
  signalfd 使低位 fd 分配位移而暴露。**三档门禁与入库契约全绿不能反证它不存在**——
  pytest 进程天然持有几十个 fd，落不进危险号段。生产 envd 启动后即持有监听 socket ⇒
  不在触发条件内，但「以近乎空的 fd 表嵌入沙箱」的形态会踩到。修法与取证见 fork
  `docs/fork-plan-followups.md` FUP-23 + fork `docs/CHANGELOG.md` 升级警示 +
  本文 #22；`tmp/f11_fdcount_probe.py`（N=0 vs N≥1）是现成回归门。
- **环境教训（新增 open 项 #19/#20/#21）**：本轮公共 Docker Hub 源整体劣化
  ——daocloud 拉 29.8 MB 层实测 37.2 s > 客户端 30 s 请求预算（必然 ReadTimeout）、
  1ms.run TLS EOF/Cloudflare 403、dockerproxy.net 曾交付**损坏层**却因
  `RegistryClient.blob()` 不校验 digest 而被静默解出（表现为 rootfs 里
  `execvp '/bin/echo': No such file or directory`）。gate A 前四档红全部源于此；
  最终用本地 `registry:2` 镜像源（amd64 `library/python` 3.11/3.12/3.14-slim +
  `library/node:22-slim`）跑绿；`tests/conftest.py:193` 给 buildkitd 的 mirror 是
  硬编码 daocloud，故 `test_template_build_and_create_sandbox` 也变同源抖动
  （单跑 282 s 过、全量档内偶发 `buildkit build exited with code 1`）。
  FUP-E3 gateway+命令变体的正式验证由入库契约
  `test_memory_quota_gateway_command.py`（pure + image-rootfs 两形态全绿，含在
  gate A/B 档内）承担；探针脚本形态另见上一条。
- 报告：fork `third_party/sandlock/tmp/sdd/ab-cleanup-report.md`、
  E2B `tmp/sdd/ab-e2b-report.md`。

## ⚡ Fork F12–F14 全部完成（2026-09-07，fork 侧收口 + E2B wheel/探针/全量复跑）

`third_party/sandlock`（fork 子模块，分支 `upstream-pr/netns-free-clean`）本地
提交链：`68e7e84`+`194ffed`（F12 ProcessIndex 一 TGID 一 entry）、`4576615`+
`6a8cec1`（F13 fs 写家族挂载保护收尾）、`ba6963e`+`4d5f385`（F14 capability-
aware 特权 remap gate），均未推送。三份计划文档状态 → ✅（f12/f13/f14）。

- **F12**：线程通知一律路由/注册到 TGID leader（删除 `PIDFD_THREAD` 独立 tid key
  路径），`key_for`/`entry_for`/`contains`/`addr_space_state`/cwd 对未登记 tid
  leader 解析，freeze TGID 归一化保留为防御；core_lib 823→827（+4 unit），
  core_integ 533 不变；pidfd leader watcher 组退出语义 C 探针实证。用户可见：
  `stats().live_watchers` 按进程组计数、6.9+ 虚拟化 /proc 不再单列被中介线程 tid。
- **F13**：目录挂载点 rmdir 与真实 bind-mount 一致 EBUSY（宿主目录不再可被沙箱
  视图删除；文件/chardev leaf 回落 ENOTDIR；挂载点内普通目录不受影响）+ link
  直击 pin + 断言精度；ffi 98→100。
- **F14**：`privileged_userns`/C 档 gate 从 `euid==0` 升级为 effective-caps 判定
  （CapEff 含 `CAP_SETUID|CAP_SETGID`）；file-cap launcher（euid 非 0 + caps）
  以点名能力的新消息建箱前 fail-closed，不再落到暗示无 caps 的晚拒；root/无
  caps/同 uid/route-B 不受影响；core_lib 827→828、mediation_2uid 8→9。
- **fork 终态门禁（各任务逐轮全绿，最后一次 = 4d5f385 树）**：non-root
  core_lib 828 / core_integ 533 / ffi 100 / cli 98 / supervise 36 /
  supervise_cost 3 / cli_build 0 / python 454；root oci 144 / supervise_root 2 /
  mediation_2uid 9（fork `tmp/sdd/f1[234]-gate-*.log`）。
- **wheel（F12–F14 最终 tip 统一重建，4d5f385）**：fork `python/build-wheels.sh`
  cp314 x86_64+aarch64 双 wheel + supervise 双架构 + HEAD 钉住
  `SHA256SUMS.supervise`（`tmp/sdd/f14-wheel-build.log`）；`verify-wheel.sh` 全绿
  （FFI 156=156 双架构双向相等、supervise 三方指纹一致、x86_64 `--uid` 拒绝冒烟，
  `tmp/sdd/f14-wheel-verify.log`）；已同步 `wheels/fork/` 并重建
  `e2b-sandlock-test:latest`。
- **E2B 真栈 thread 探针 GREEN**（新 wheel + 重建镜像，
  `tmp/perf/f14-thread-probe.log`）：线程化 python A 存活时后续 exec B exit 0 /
  stdout `b-ok\n`（F11/F12 argv-safety × 多线程回归在 E2B 栈上保持绿）。
- **E2B 下一波收口（2026-09-07，wheel = 4d5f385）**：FUP-E3 gateway+命令变体
  pure 形态探针 4/4 GREEN（`list_tools == ['echo']`、网关后命令 exit 0 /
  stdout `post-gateway-ok\n`、450M 超卖命令拒绝 exit 137 / stdout `''` /
  stderr ∈ {"", "Killed\n"}、50M 控制命令 exit 0、record `memoryMB == 1024`；
  `tmp/perf/f14-gateway-probe-450-450-50{,-run2,-run3,-run4}.log` +
  `tmp/perf/f14-gateway-evidence.txt`）；契约 `test_memory_quota_gateway_command.py`
  与 `test_memory_quota_boxed.py` pure 各 2 轮全绿（`tmp/f14-e2b-contract-gw{1,2}.log`
  / `-boxed{1,2}.log`）；full gate A（image-rootfs python-mcp:3.14）
  **982 passed / 2 skipped / 1 xfailed(T5) / 0 failed**（`tmp/f14-e2b-gate-a.log`）、
  full gate B（pure sandlock）**982 passed / 3 skipped / 0 failed**
  （`tmp/f14-e2b-gate-b.log`）、macOS 全量 **916 passed / 65 skipped / 0 failed**
  （`tmp/f14-e2b-macos.log`）。task-backlog #15/#17/#18 的 E2B 复跑随之关闭
  （fork 侧早于 `bc54b26` 完成；E2B 侧收口见下方提交与 `.superpowers/sdd/progress.md`）。
  指针 bump = 4d5f385（`bc54b26`）+ 本波 fork docs commit（e2b-integration §5/§8、
  CHANGELOG、followups、f12 计划）。
- **gate A 复跑说明（参数纪律）**：full gate A/B 需要
  `E2B_MAX_CONCURRENT_COMMANDS_PER_SANDBOX=2`（boxed 两并发命令共享槽位）；
  首轮漏传该参数时 `test_boxed_memory_quota_denies_sibling_overcommit` 以
  "command queue timed out after 30s" 失败，补参后同一用例 gate A 形态
  33.81s 通过（`tmp/f14-e2b-boxed-gateA-focused.log`），full gate A 复跑绿——
  非 fork 回归，属门禁运行参数。

## ⚡ G3 final wave（FUP #12 egress 证据 + G1/G2 台账收口，2026-09-06）

主仓库两个提交（fork 未动，子模块指针仍 `bc6c892`）：
`0e15572`（`test(sandlock): pin worker-side egress after rejected network widen
(FUP12)`）+ docs commit（本块所在提交，见 git log）。报告
`tmp/sdd/g3-final-wave-report.md`；门禁日志 `tmp/g3-*.log`。

- **FUP #12 行为证据**：`tests/security/test_network_enforcement.py` 新增
  `test_rejected_rewiden_leaves_worker_runtime_copy_narrowed`——IP-literal allowOut
  建箱（198.18.0.99 loopback 别名 + 本地 RecordingOrigin，NET_ADMIN 门控与
  `test_fork_network_features` 一致）→ launch → 放行探针 exit 0 → 收窄 `[]` 204
  （探针拒绝）→ 放宽 409 且 record 与收窄态相等 → 新命令再探仍拒绝：worker 运行时
  副本未被 409 改回。断言全精确，无 substring。
- **Ledger close-out**（`docs/task-backlog.md`）：FUP #1（`7a98755`+`aa844b7`）、
  #7（`8ac02a7`）、#8（`84e807f`）、#9（`710ddd2`）、#10（`beff30f`）、#12
  （`0e15572`）→ ✅；#4（产品决策）、#5（route-B 前置）、#11（约定）⬜ open 并显式
  标注；新增两条 G2 评审登记（#13 本地 snapshot fork × per-sandbox-uid uid 分配
  缺口、#14 快照剪枝启发边界风险）；FUP #6 / T3 ✅ 原样保留。快照剪枝边界同时在
  `control_plane/registry/snapshots.py::_prune_store` 留下 boundary note。
- **Open-FUP 最终态**：仍 open 仅 #4（SDK 可见性，产品决策）、#5（T5 route-B 后摘
  xfail）、#11（bisect 日志头纪律，约定）、#13/#14（G2 评审登记）；下方各历史块中
  的 FUP 编号列表以本块与 task-backlog 为准。**新增（2026-09-06 用户指示列入主要
  计划）**：fork **F12 — ProcessIndex 一 TGID 一 entry**（线程 tid 懒登记建模收口，
  ⬜ 计划中）：fork 详细计划 `third_party/sandlock/docs/fork-plan-2026-09-f12.md`，
  main 登记 task-backlog #15，fork followups A 节 F12；收口流程 = fork 实现 + 门禁
  + wheel → E2B 指针 bump + thread/gateway 探针 + full gate A/B 复跑。
  **再增（2026-09-06，C 类评估后按建议排入计划）**：fork **F13 — fs 写家族挂载
  保护收尾**（FUP-04 link 直击 + FUP-05 目录挂载点 rmdir + 断言精度，中优先，
  ⬜ 计划中）与 **F14 — capability-aware 特权 remap gate**（FUP-22，route-B ③
  部署前完成，⬜ 计划中）：fork 计划
  `third_party/sandlock/docs/fork-plan-2026-09-f13.md` /
  `third_party/sandlock/docs/fork-plan-2026-09-f14.md`；main 登记 task-backlog
  #17/#18。C 类评估全文 `third_party/sandlock/docs/fork-c-class-design-assessment.md`。

门禁摘要（容器 strict：image 默认 `E2B_BASE_IMAGE=python:3.11-slim` + privileged
host-net，`E2B_TEST_STRICT_SKIPS=1`；macOS host 见下）：
- 容器：新 egress 测试独立跑 1 passed（`tmp/g3-egress-r1.log`）；network
  enforcement 文件全量 3 passed / 0 failed（`tmp/g3-network-file.log`，新测试第二遍
  在内）。两条均 0 skip 0 error。
- macOS：security 文件（含新测试）off-Linux 干净 skip 3/3
  （`tmp/g3-macos-security.log`），无额外依赖或重 fixture 启动。

## ⚡ F11 E2B 集成收口（2026-09-06）

fork F11（argv-safety exec freeze × 多线程进程树，fork 本地 `edd8c76` fix +
`927d015` 收口）的 E2B 侧收口：wheel 按 F11 tip 重建同步 `wheels/fork/` 并重建
`e2b-sandlock-test:latest`；FUP-E3 gateway+命令变体在 1 GiB 默认箱复跑全绿——MCP
gateway + 450M stdio server 可达（`list_tools == ['echo']`）、网关后普通命令恢复
（exit 0，stdout `post-gateway-ok\n`）、第二 450M 命令精确拒绝（exit 137 /
stdout `''` / stderr ∈ {"", "Killed\n"} / error None）、450+50 控制命令 exit 0 且网关
持续服务、record `memoryMB == 1024`。契约：`tests/contract/test_memory_quota_gateway_command.py`
（pure 2/2 绿 + gate A 内通过）；探针日志 `tmp/perf/f11-gateway-probe-450-450-50*.log`，
门禁日志 `tmp/f11-e2b-*.log`，报告 `tmp/sdd/f11-e2b-integration-report.md`。

主提交：`36fe28d`（子模块 bump——指针 = fork core F11 tip `927d015` + fork docs commit
`bc6c892`，均本地未推送）、`7685126`（gateway+command 契约，FUP-E3/F11）、docs commit
（本块所在提交，见 git log）。fork 侧 docs 编辑（e2b-integration §5/§8）在子模块内提交
并折入 bump。

门禁摘要：
- pure 契约（`E2B_BASE_IMAGE=`，concurrency=2）：gateway+command 文件 2/2 绿 +
  boxed sibling 2/2 绿（`tmp/f11-e2b-contract-gw{2,3}.log`、`-boxed{1,2}.log`）；
- 容器 gate A（image-rootfs `python-mcp:3.14` + netns + XFS + npm + strict，
  concurrency=2）：`933 passed / 1 skipped / 1 xfailed (T5) / 0 failed / 0 error`
  （`tmp/f11-e2b-gate-a.log`；skip = volume_quota:274 互斥分支，历史一致）；
- macOS full（unit+contract+sdk python/js+security）：`871 passed / 60 skipped /
  0 failed / 0 error`（`tmp/f11-e2b-macos.log`；60 条 skip 全为平台能力，其中含本波
  新增 gateway+command 契约文件的 sandlock 平台 skip 1 条，无掩盖）。

Open-FUP 列表据此更新（最终态见顶部 ⚡ G3 块）：② fork F11、③ 网关 ledger headroom
已关闭（thread-tid-keying fork 内部残余随 ② 登记，见 `docs/task-backlog.md`
row 2）；① 远程 pause/resume 投递、⑥ pure-shape workspace 属主对齐（gate B trio）、
⑦–⑩、⑫ 已分别由 G1a/G2/G3 关闭（见 ⚡ G2 与顶部 ⚡ G3 块）；仍 open：④ 网关启动
失败 SDK 可见性（产品决策）、⑤ T5 xfail route-B 后摘除、⑪ bisect 日志头纪律
（约定），外加 G2 评审登记 #13/#14（task-backlog 同号条目）。

## ⚡ G2（FUP #6 pure-shape workspace 属主对齐 + T3 快照自嵌套守卫，2026-09-06）

主仓库两个提交（见 git log，fork 未动）：`fix(sandlock): align pure-shape workspace
ownership with run-as identity (FUP6)`、`fix(snapshots): refuse self-nesting copies and
prune embedded store roots (T3)`。报告 `tmp/sdd/g2-ownership-snapshot-report.md`。

- **FUP #6 根因与修法**：pure-sandlock（无 base image）沙箱命令以 host RunAs 身份直写
  workspace（无 chroot ⇒ 无 supervisor 中介），而 root worker provision 出的 workspace
  是 root:root 0755（gate-B migration trio 首个命令 EACCES；`tmp/m4-bisect-t1-pure.log`
  与 M4 基线证据同前）。修法 = `envd_service/uid_pool.py` 新增 `align_shared_uid_workspace`：
  worker 为 root 且 workspace 属主仍为 root 时，整树 chown 到 legacy 共享 RunAs uid
  `1000` 并收紧 0700（复用 `apply_sandbox_ownership` 语义；**不** blanket-chmod 0777，
  不触碰共享卷 slice——per-uid 隔离模型不变）。接入四处 provision 缝：agent create、
  agent import（tar `data` filter 会丢 uid/gid，展开后需重对齐）、control-plane 本地
  provision、本地 snapshot fork。per-sandbox uid（`host_uid`）路径原样保留；非 root
  worker 无操作（创建者身份 == RunAs 身份）。新增 pure-shape 回归契约
  `tests/contract/test_pure_shape_workspace_ownership.py`（shell 写 workspace 根 +
  migration 跨 worker 保留 + 目标导出 tar 属主 == 1000）与单测
  `tests/unit/test_workspace_ownership.py`（决策部分全平台 + chown 部分 root）。
- **T3 根因与守卫**：`SnapshotRegistry` base 默认 = workspace_base
  （`control_plane/app.py`），快照目录与沙箱工作区同级；`expand_to`/`create_from_sandbox`
  若把存储复制进快照自身会指数嵌套到 `ENAMETOOLONG`（证据 `tmp/stale-20260902/`）。守卫 =
  复制前拒绝"目标落在源之内"（显式 `ValueError`），并用 ignore 回调剪掉工作区里嵌入的
  快照存储根（只剪最外层：目录本身是快照根、或直接装着快照根；普通同名目录原样保留）。
  用例 `tests/unit/test_snapshot_registry.py` 3 条（先 RED 后 GREEN），snapshot 契约回归全绿。

门禁摘要（日志 `tmp/g2-*.log`）：
- macOS full（unit+contract+sdk python/js+security）：`916 passed / 64 skipped /
  0 failed / 0 error`（`tmp/g2-macos-full.log`；新单测决策部分全平台跑，chown 部分 root
  标记跳过，skip 全为平台能力）。
- 容器 pure（`E2B_BASE_IMAGE=` + netns + strict）：migration 全套 + pure-shape 属主契约 +
  snapshot 契约/单测 `22 passed / 0 skipped / 0 failed`（`tmp/g2-pure-container.log`）；
  full gate B `981 passed / 3 skipped / 0 failed / 0 error`（`tmp/g2-full-gate-b.log`，
  基线 921/3/3 —— gate-B migration trio 转绿）。
- 容器 image-rootfs（`E2B_BASE_IMAGE=python-mcp:3.14` + netns + strict）：migration 全套
  `7 passed / 0 failed`（`tmp/g2-gateA-migration-container.log`）；full gate A
  `981 passed / 2 skipped / 1 xfailed (T5) / 0 failed / 0 error`
  （`tmp/g2-full-gate-a.log`）。
- M4 narrow（canonical 10-file list，image-rootfs `python:3.14-slim` strict）：
  `80 passed / 0 skipped / 0 failed`（`tmp/g2-m4narrow-container.log`）。

## ⚡ M4 收口（2026-09-06，Task 0/0.5/0.6/1–11 全部完成）

> 主线目标已达成：E2B 侧 M4 接线（fork §8 / fork-plan-followups FUP-E2）+ FUP-E1（T4 复测）+
> FUP-E3（超卖断言化）+ 发布前置；E2B 全量门禁通过。
> **执行计划（唯一入口）**：`docs/superpowers/plans/2026-09-06-e2b-m4-wiring.md`
> （Task 0 基线归因 / 0.5 mediation 下发 / 0.6 fork F10 / 1–11 M4 主线）。
> **进度账本**：`.superpowers/sdd/progress.md`（本仓库，git-ignored）。
> **Task 11 报告**：`tmp/sdd/task-11-report.md`（提交 hash、门禁摘要行、xfail 清单、FUP 列表）。

任务状态与提交（main；fork 子模块 docs commit `fdbc170`（docs-only，位于 docs commit
`48ec096`/`8c5f020` 与 fork core F10 tip `b955ae9` 之上），本地未推送）：

- Task 0/0.5/0.6（fork F10 前置）：fork 修复 `9dd134e..b955ae9`（评审 clean，门禁非 root
  822/532/98/98/36/3/0/454 + root oci 144/supervise_root 2/mediation_2uid 8）；
  E2B 提交 `5d38537`（Task 0.5：显式 `mediation_run_as='supervisor'`）+ 子模块 bump `614224f`；
  基线 24 failed 归零（Task 0.5 全量 878 passed / 0 failed / 1 skipped / 2 xfailed）。
- Task 1–3：`4f34e55` / `e0f5507` / `6de41db` / `05f349f`（实例持有、per-exec、
  MCP 端口 ceiling、生命周期与 127 契约）+ 容器门禁收敛 `cb36b7a`。
- Task 4（含 Task 9 验收）：`4149a1f` + review fix `4d617b9`（update_network S2，D4=A：
  放宽/模型翻转 HTTP 409 不落库；local apply 原子）。
- Task 5：`f64d7ab`（pause/resume 按 child 进程组 SIGSTOP/SIGCONT，语义保持；网关不暂停）。
- Task 6：`19dc1f5`（chroot 用 `minimal_dev` 六键；T5 strict xfail 保持）。
- Task 7：`f337724`（`max_processes` 整箱默认 64→256 全链 + 容量口径 2048/256=8）。
- Task 8：`3bf5d0e`（FUP-E3 sibling-exec 超卖断言；网关+命令变体 fork-blocked → F11）。
- Task 10：`883d38d` + `f67a6b9`（T4 摘标：根因 = envd base-image 组成——slim rootfs 无
  mcp-gateway；改用 MCP-capable `python-mcp:3.14` 后 chroot+netns 契约两形态 3/3 绿）。
- Task 11（本任务）：D10 可观测性日志点 + caplog pin、deploy/compose 与 migrate-tenants
  默认对账 64→256、五处文档一致性 + 计划 checklist、三轮全量门禁、FUP 登记；提交与
  门禁摘要见 `tmp/sdd/task-11-report.md`。

全量门禁基线（日志 `tmp/m4-full-gate-a.log` / `tmp/m4-full-gate-b.log` /
`tmp/m4-full-gate-macos.log`；精确 summary 行与 xfail 清单见 `tmp/sdd/task-11-report.md`）：

- 容器 full gate A：image-rootfs + netns + XFS + npm + strict（`E2B_BASE_IMAGE=python-mcp:3.14`，
  `E2B_MAX_CONCURRENT_COMMANDS_PER_SANDBOX=2`）→ 925 passed / 1 skipped / 1 xfailed /
  0 failed / 0 error（282.69s）；唯一 xfail = T5（route-B 前置）；唯一 skip =
  `test_volume_quota.py:274`（XFS-degradation 互斥分支，历史每轮一致）。
- 容器 full gate B：pure sandlock + netns + strict（`E2B_BASE_IMAGE=` 空）→
  921 passed / 3 skipped / **3 failed（pre-existing pure-shape trio，见下）** /
  0 error（314.38s）；3 条 skip = `test_volume_quota.py:274` 互斥分支 +
  `test_fork_network_features.py:281` / `test_template_isolation.py:17`
  （pure 形态无 `E2B_BASE_IMAGE` 的声明性 gate skip）。
- macOS 宿主 full（unit + contract + sdk python/js + security）→
  865 passed / 58 skipped / 0 failed / 0 error（136.97s）。

**Gate B pre-existing trio（确未修 inventory，非 M4 回归）**：
`test_migrate_sandbox_files_between_workers` / `test_migrate_failure_restores_source_runtime` /
`test_migrate_shared_workspace_skips_transfer` 在 pure-sandlock（无 base image）形态下，
沙箱 shell 无法写入自己的 workspace 根目录（`/bin/sh: cannot create …: Permission denied`；
workspace 根 root:root 0755，共享 uid 沙箱按 host uid 1000 直写）。同 3 条在 M4 前置
commit `4f34e55` 的同一 pure 形态下以相同方式失败（`tmp/m4-bisect-t1-pure.log`），且
pre-M4 基线 `tmp/e2b-base-20260906.log` 已含同族 migration 失败（chroot 形态；mediation
修复后 gate A 同批已全绿）→ 判定为既有 pure 形态缺陷，登记下方 FUP #6。

**Open follow-ups（详情与指针见 `docs/task-backlog.md`「M4 收口后的 open
follow-ups」；最终态见顶部 ⚡ G3 块）**：① 远程 pause/resume 投递、② fork F11、
③ 网关 ledger headroom（E2B 侧已关闭，FUP #3）、⑥ pure-shape workspace 属主对齐
（gate B trio）、⑦–⑩、⑫ —— 均 ✅ 已关闭（G1a/G2/G3，提交 hash 见 task-backlog
同号条目；门禁 `tmp/g1-*`/`tmp/g2-*`/`tmp/g3-*.log`）；仍 open：④ 网关启动失败 SDK
可见性（日志已落地，产品决策待定）、⑤ T5 xfail route-B 后摘除、⑪ bisect 日志头
纪律（约定），外加 G2 评审登记 #13/#14（task-backlog 同号条目）。

Release note / 变更段（M4；fork 侧行为变化引用
`third_party/sandlock/docs/CHANGELOG.md` F0–F10 段）：

- `max_processes` 默认按整箱 64→256（D6）：单沙箱实例内命令与 MCP 网关共享整箱预算；
  节点进程维度容量 = `total_processes / 256`（默认 2048 → 8 个标准沙箱），compose 与
  migrate-tenants 回退已同步。
- chroot 形态用 `minimal_dev` 六节点（ptmx/pts/null/urandom/zero/tty）替换整树宿主 `/dev`；
  `/dev/shm`、`/dev/mqueue` 在沙箱视图中构造上不存在，无需 carve-out（D7）。
- 每沙箱一只 exec-only `SandboxInstance`（M4 D1–D3）：exec 是 envd 唯一命令路径（无
  `popen` 残留）；实例惰性创建、delete/kill/TTL/evict/migrate/worker 停全部幂等收敛到
  `close()`，closed/dead 后重建一次并重试，仍败才向 SDK 报错。
- 网络 update 语义（D4=A）：live-expressible = IP-literal `allowOut` 收窄（只影响新 exec，
  在跑 child 保旧策略，staleness 回报日志）；`denyOut`/default-allow 实例 live-immutable；
  放宽/模型翻转在 persist 前 HTTP 409；local apply 原子；remote push-then-persist 带既有
  transport-loss caveat。
- pause 冻结 `ProcessManager` 命令组（网关不在命令组、不暂停）；T5 xfail 在 route-B
  supervise 部署前保持（属预期）。
- per-sandbox 默认内存 512→1024 MiB（`E2B_DEFAULT_MEMORY_MB`）：网关 ledger headroom
  FUP 在 E2B 侧关闭——1 GiB 箱给网关 allocator reservations 与 450M MCP server 目标留出
  空间；fork 逻辑未改动，fork F11 保持 open。
- 远程 pause/resume 投递（FUP #1，`7a98755` + review `aa844b7`）：control plane 对
  非 `local://` 节点 push pause/resume（agent 路由；显式拒绝回滚 + 502、transport
  loss best-effort），SDK pause/connect 在 multinode 下真正冻结/恢复 worker 运行时
  （multinode pause 契约）。
- 网络 remote push fail-closed（FUP #7，`8ac02a7`）：worker 显式 ≥400 的拒绝不再
  静默落库 204——409 保持 409，其余显式错误映射 502 且 record 不变；仅
  transport-loss/missing-node 保留既有 best-effort caveat。
- 属主对齐（FUP #6，`b3bfe2d`）：pure-sandlock root worker 的 root-owned workspace
  在 provision/import/fork 缝整树 chown 到共享 RunAs uid 1000 + 0700；per-sandbox
  uid 路径不变（gate-B migration trio 转绿）。
- 快照守卫（T3，`c5867be`）：`create_from_sandbox`/`expand_to` 拒绝"目标落在源之
  内"（显式 `ValueError`），复制剪掉工作区里嵌入的快照存储根（只剪最外层、直接含
  `snapshot.json` 根的目录；普通同名目录保留）。

## ⚡ sandlock fork 交接总览（新会话从这里开始）

> 📄 **集成事实源已下沉到 fork 仓库**：`third_party/sandlock/docs/e2b-integration.md`
> （已落地方案 R*/S*/E*/M*、待实施 P1–P8、未解决 SL-1/T5（T4 已随 FUP-E1 关闭，
> 见顶部「M4 收口」块）、缓解与验证矩阵）。
> E2B 为 sandlock 写的方案文档也已迁入该仓库 `docs/`（原文件名不变）：
> `sandlock-network-wildcard.md`（R1–R14 总纲，已落地）、
> `netns-isolation-fd-injection.md`（loopback netns + fd 注入，落地为 S1.1/S2.x）、
> `sandbox-level-cow.md`（**已否决**，最终走 XFS prjquota）、
> `upstream-pr-netns-free.md`（无特权上游 PR 范围与推送状态）。
> 本文件保留 E2B 侧上下文与命令，条目细节以上述文档为准；
> E2B 仓库的 `docs/sandlock-upstream-issues.md` 只剩编号映射。

**位置与分支**：`third_party/sandlock`（imhun/sandlock fork 子模块，版本 0.9.0-beta；
origin=fork，upstream=multikernel）。**运行时基线：`upstream-pr/netns-free-clean`
（无 netns/veth 的无特权版本，全程无 root）**；`feature/network-socks5` 是
含 per-sandbox netns 的旧主线，仅作参考，不再用于运行时 wheel。

**已完成（Block A 全部）**：

- R1 通配解析（`NetTarget::HostWildcard`，deny 拒绝域名）、R2 合成映射
  （`SyntheticDns`，10.250.0.0/16、LRU 4096）、R3/R4 连接判定 + SSRF 护栏。
- **默认路径（无特权）**：每沙箱 loopback DNS 网关（`127.0.0.x:53`）+
  resolv.conf memfd + connect/send 豁免；netlink 合成视图含虚拟 eth0
  （192.0.2.1/24 + 2001:db8::1/64）修复 glibc AI_ADDRCONFIG。
- UDP 通配（send 路径）、HTTP ACL 代理经网关重定向、wheel 可构建。
- **netns 已从运行时基线移除**：`upstream-pr/netns-free-clean` 删掉了
  per-sandbox netns/veth（`network/netns.rs`、test_netns、`netns` flag/
  FFI/Python），wildcard 全程走无特权共享路径；executor 不再传 `netns`
  参数，`E2B_ENABLE_NETNS` 仅兼容保留（默认 false）。

**sandlock 本身状态**：

- **Block B（R8–R11）已完成**（`feature/network-inject`）；**Block C
  （R12–R14）已完成**（`feature/network-socks5`：SOCKS5 on-behalf 替代
  LD_PRELOAD，fail closed，ATYP=domain/IPv4/IPv6，RFC 1929）。
- **上游 PR 已备好**：`upstream-pr/netns-free-clean`（`d3a28cc` +
  `55709f2`（Block C）+ `b6ef050`（非 root 测试入口），基 `f6a3e39`，
  无 netns/veth；**同时是项目运行时 wheel 的构建基线**）；**未推送**——
  当前 `GITHUB_TOKEN` 只读（push/API 写均 403），需换写权限 token 或
  手动推送，见 `third_party/sandlock/docs/upstream-pr-netns-free.md`。
- **M6 cp314 双架构完成 + 运行时统一 3.14**：`wheels/fork/` 现有 cp314
  x86_64 + aarch64 wheel（版本 0.9.0-beta，`manylinux_2_34` 标签；
  `deploy/scripts/build-sandlock-wheels.sh`，一个 amd64 manylinux builder 内用
  **zig 交叉编译**两个架构，zig glibc pin 2.34 + auditwheel 修复，无需
  QEMU 编译；镜像源：apt=清华、rustup/crates=rsproxy、pip=清华）；worker/
  control-plane/test-runner 与 `E2B_BASE_IMAGE` 默认模板统一切到
  `python:3.14-slim`，Dockerfile 按镜像内 CPython ABI 选 wheel
  （cp 矩阵混放不会选错）；fork 侧 build.rs 加 `-mcmodel=large`
  （manylinux gcc-toolset-14 下 restore-stub 的 32 位绝对重定位溢出）；
  cp310/312–313 未做。

**未完成（项目侧 sandlock 落地）**：

- 3.14 + netns-free wheel 下全量回归（unit/contract/security/sdk 已跑，
  见下）；worker/测试镜像已切 fork wheel（0.9.0b0 manylinux_2_34）。
- 上游 PR 推送（换写权限 token）+ 上游合入后回切官方 wheel 的流程。

**e2b 对接覆盖（补齐 5 个缺口，`tests/security/test_fork_network_features.py`）**：

- 通配 `allowOut` 走无特权 DNS 网关 + 合成 IP + supervisor 代连（198.18.0.99
  loopback 别名 fixture，SSRF 护栏放行段；test-runner 已装 iproute2）；
- HTTP 注入 + `maskRequestHost` 在 origin 侧断言 wire 头（`Host` 改写、
  字面量 secret 与 `${e2b.identity.tokens.*}` env token 各一条；fork 注入
  是 first-match-wins，一个 matcher 一条 header）；
- 镜像 rootfs 模式下 HTTPS MITM CA splice（`.e2b-ca` + `SSL_CERT_FILE`）；
- 沙箱子进程恒为 uid/gid 1000（无 root 断言）。

**fork 改动：多 header 注入**（`transparent_proxy/service.rs`）：注入循环去掉
`break`，同一 matcher 的多条 credential 规则全部应用（此前 first-match-wins，
`transform.headers` 多 header 只有第一个生效）；同 header 名多条规则按序
后者覆盖，AddOnly 语义不变；新增 hermetic 用例
`http_injects_multiple_credentials_per_request`（Linux 容器跑通）。wheel 已
重建（0.9.0b0 manylinux_2_34 双架构），e2b 测试改为单规则双 header 组合断言。

**生产形态验证（seccomp）**：`unshare(CLONE_NEWUSER)` 会被 Docker 默认
seccomp profile 以 EPERM 拦截（capability 无法绕过），所以 worker 容器必须
`security_opt: [seccomp=unconfined]`（compose 已有）。已新增
`deploy/scripts/smoke-prod-worker.sh`：非 privileged + seccomp=unconfined 形态下跑
沙箱创建（无 root）、rootfs chroot（CA splice）、SOCKS5 出口三个用例，
实测 3 passed。**全量测试套件已切非特权形态**：`--security-opt
seccomp=unconfined --cap-add NET_ADMIN --network host`（无 `--privileged`），
257 passed；`--network host` 仅 registry/Redis 测试基础设施需要（Docker
daemon 只对 localhost 默认放行 HTTP registry），`NET_ADMIN` 仅通配域名
本地 origin fixture 需要。顺带修了 authenticated_registry fixture 的
htpasswd 路径 bug：容器内写文件必须走 `/workspace` 挂载视图（daemon 在
宿主解析 `-v` 源路径，写宿主绝对路径会落进容器自身文件系统、daemon 在
宿主建目录导致 registry 登录 400）。

**iam（SDK 工作负载身份）已实现**：控制面 create 接受 `iam.tokens`
（兼容 wire 的 camelCase `tokenType` 与 snake_case），存到沙箱记录并透传
worker；executor 在注入时把 `${e2b.identity.tokens.<name>}` 占位符（含
`Bearer ${...}` 内嵌形式）替换为 HS256 JWT-SVID（aud=audience，
`E2B_IAM_SIGNING_KEY` 签名，默认本地开发密钥），env `E2B_IDENTITY_TOKEN_*`
作为回退；新增契约测试（接受/非法 name/token 拒绝）、单元 JWT 签发测试、
SDK iam 端到端（origin 收到 `Authorization: Bearer <jwt>` 且 aud 正确）。
另发现并规避：同步 e2b SDK 会阻塞测试事件循环，harness 用例里的本地
origin 需跑在后台线程。

**验证基线（fork，Linux 容器，全程非 root uid=65534，2026-09-01 更新）**：
lib `773 passed, 0 failed`；integration `445 passed, 0 failed`（netns
用例在无 CAP_NET_ADMIN 时按能力跳过）；Python `430 passed, 0 skipped`
（`deploy/docker/Dockerfile.test-runner` 已补 `/usr/bin/python3 -> /usr/local/bin/python3`
符号链接）。

**内核级隔离（S1.1/S1.2 已落地，`upstream-pr/netns-free-clean`）**：

- **PID namespace（`pid_ns=true` 开关，默认 false）**：`CLONE_NEWPID` 两级
  fork，沙箱内 pid 1 = 首进程；`kill(host_pid,0)` 返回 ESRCH（不再可枚举）；
  procfs 按 ns pid 重编号；on-behalf `/proc` open 只读元数据白名单
  （root/mem/fd 等 EACCES）；freeze/thaw/checkpoint/throttle/tty/stat 家族/
  线程 tid 全覆盖测试。
- **独立 uid（userns 单 entry，`RunAs` 任意 host uid）**：root supervisor
  下不同沙箱不同 host uid → 同路径文件（0700）与 unix socket 真隔离（内核
  DAC，非仅 Landlock）。**约束：非 root supervisor 无法映射任意 host uid，
  请求不同 uid 的 RunAs 会 fail-closed 拒绝——每沙箱独立 uid 需要
  root/CAP_SETUID 或等价机制**（E3.2/E5.1 架构输入）。

**环境注意事项**：

- 容器 `sandlock-dev:latest`（e2b-sandlock-test + rustup/rsproxy +
  iproute2 + **入口脚本**（root 一次：`ip_unprivileged_port_start=0` +
  预置 198.18.0.99–103 回环地址与 /etc/hosts fixture，chmod 共享
  target，然后 `setpriv` 降为 nobody 再执行命令）。宿主
  `~/.cargo/registry` 挂载到 `/opt/cargo/registry` 离线构建。
- **:53 低端口设置的固化**：`net.ipv4.ip_unprivileged_port_start` 是内核
  设置，写不进镜像文件，但可以固化到容器运行时清单——`docker run
  --sysctl net.ipv4.ip_unprivileged_port_start=0`、compose `sysctls:`、
  K8s `securityContext.sysctls`（实测无需 privileged、按容器隔离，容器可
  全程非 root，连入口 root 都不需要）。`--cap-add=NET_BIND_SERVICE` 对非
  root 进程无效（Docker 不注入 ambient caps），`setcap` 文件能力也被
  sandlock 的 no_new_privs 禁用——所以 sysctl 声明是唯一干净的方式。
  **注意**：`--network host` 的容器 Docker 拒绝应用 net sysctl（宿主
  netns 不允许），所以测试容器（host 网络）必须靠入口脚本 root 写一次；
  生产 worker 用桥接网络，compose `sysctls` 生效（deploy/compose/docker-compose.prod.yml
  已加）。
- **构建以 root 跑一次**（`--user root --entrypoint bash`，见下），**测试
  全程非 root**——这是 sandlock 无 root 原则的落地；整个套件不再有
  "root 环境性失败"。需要 root 的操作显式
  `--user root --entrypoint bash`。
- netns 集成测试（fork 专属）仍需 `--privileged --network host` +
  `CAP_NET_ADMIN`，非特权环境下自动跳过（`net_admin_available()`）。
- 跑前清 VM 残留（`ip addr del 198.18.0.9x` + 删非 master 的 veth）仅
  root 会话需要。
- 本环境外部 DNS 被透明代理改写为 198.18.x，SSRF 护栏已放行该段；
  e2e 测试用本地 fixture（worker /etc/hosts → 198.18.0.9x）不依赖外网。

**下一步**：① 换写权限 token 推送 `upstream-pr/netns-free-clean` 并开上游
PR；② cp310/312–313 wheel 矩阵（沿用 zig 交叉编译流程）；③ 3.14 全量双架构
回归 + 生产镜像重建验证（worker/测试镜像已切 fork wheel）。

## 本会话已完成（Block B — header 注入 / maskRequestHost / HTTP 通配）

fork 分支 `feature/network-inject`（基于 feature/network-netns）：

1. **R11 HTTP 通配 matcher**：`HttpRule::matches` 的 host 位置支持
   `*.suffix`（只匹配子域、不匹配裸域、大小写不敏感，与 `net_allow`
   通配语义一致）；`parse` 校验非法形态（`**`/`*.`/`*.*`/内嵌 `*` 拒绝）；
   `extend_net_allow_for_http` 对 `*.suffix` HTTP 规则映射到
   `NetTarget::HostWildcard`（不再当字面 hostname 解析），走 DNS 合成 +
   SSRF 护栏。
2. **R8 credential injection 暴露**：FFI 新增
   `sandlock_sandbox_builder_credential(name, source)` /
   `sandlock_sandbox_builder_http_auth(rule)`；Python 新增
   `Sandbox.http_inject`（list[dict]：matcher/auth/secret/name/on_existing，
   校验 + 序列化）；CLI 沿用既有 `--credential`/`--http-auth`。secret 仍
   只存 supervisor（`SecretString` 零化、env: 变量从子进程剥离）。
3. **R10 host_mask**：`Sandbox`/builder/CLI（`--host-mask`）/FFI/Python
   （`host_mask`）/TOML profile 新增；`transparent_proxy/service.rs` 转发前
   只改写 wire `Host` 头（`${PORT}` 替换为真实目标端口），URI authority
   保持真实（驱动上游连接，hyper-util 保留显式 Host 头）；非法掩码 502
   fail closed。
4. **R9 HTTPS MITM 复用**：注入/掩码在明文与 MITM 共用同一 handler，TLS
   终止路径既有测试覆盖。
5. **验证**：lib 763→771（2 个既有 cow/seccomp root 环境性失败）；integration
   http_acl 16/16（新增 host-mask e2e）；hermetic 代理测试（本地上游断言
   注入头 + 掩码 Host）；Python 全量 412 passed；wheel 可构建且含新符号；
   `Sandbox(http_allow=[...], http_inject=[...], host_mask=...)` 原生构建通过。
   注意：本环境 `target/` 已改为指向 `target-linux` 的符号链接（Python
   `_find_lib` 需要），旧 `target/` 残留已清理。

## 本会话已完成（② 项目切源 + 5B.4 映射 + e2e；③ Block C；④ 上游 PR）

1. **Block C（fork `feature/network-socks5`，e84d65b）**：`network/egress.rs`
   —— SOCKS5 客户端（RFC 1928/1929、poll 驱动、10s 超时、fail closed）；
   `connect_on_behalf` 在 allow/deny 过滤后对所有 TCP 走隧道（通配目标
   ATYP=domain 远程 DNS，字面目标 IPv4/IPv6；UDP/ICMP 直出；loopback remap
   与 DNS 网关豁免）；代理端点由 supervisor 代拨且不进 net_allow（沙箱无法
   直连绕过）；Sandbox/builder/CLI/FFI/Python/profile 暴露 `egress_proxy`
   （含 RFC 1929 凭据，不序列化）。验证：7 单测 + 3 hermetic 集成（隧道/
   fail closed/ATYP=domain）；lib 778、integration 436、Python 414。
2. **② 项目侧**：`gateway_common/network.py` 接受 `maskRequestHost`
   （create-only）与 `rules[].transform.headers`，映射到 `http_inject` /
   `host_mask`；executor 把字面 header 值写入 supervisor-only 0600 文件
   （`E2B_IMAGE_CACHE_DIR/secrets/<sbx>/`），`${e2b.identity.tokens.*}` 映射
   `E2B_IDENTITY_TOKEN_*` env（缺失则创建失败）；修复 chroot 模式下
   `http_inject_ca` 传宿主路径导致 popen 失败的既有 bug（改为沙箱视图路径
   `/home/user/.e2b-ca/...`）；LD_PRELOAD egress 库退役（R14），egressProxy
   统一走 sandlock on-behalf。镜像切 fork wheel（`wheels/fork/`，TARGETARCH
   选择）；requirements-test 不再锁 PyPI 0.8.6。验证：全量 257 passed +
   JS skip；修复过程发现并解决了 SDK 无 `mask_request_host` 字段、
   `api.example.com` 在测试容器不可解析等环境问题。
3. **④ 上游 PR**：`upstream-pr/netns-free-clean` = fork 特性树去掉
   netns/veth（删除 `network/netns.rs`、`netlink/ops.rs`、test_netns、context
   netns pipe、sandbox veth 阶段、`netns` flag/FFI/Python、VethView、
   CLONE_NEWNET），保留无特权 loopback DNS gateway + 虚拟 eth0 +
   wildcard/UDP + Block B + Block C；lib 761、integration 428、Python 412。
   已推送 `origin/upstream-pr/netns-free-clean`（tip `53a8ee2`）；PR 文案见
   `third_party/sandlock/docs/upstream-pr-netns-free.md`。

## 本会话已完成（Block A 第一阶段 — sandlock fork：通配域名规则）

1. **fork 基线（M0）**：`third_party/sandlock`（imhun/sandlock 子模块，0.8.6 起步，
   origin=fork / upstream=multikernel）。构建链：
   `sandlock-dev:latest`（e2b-sandlock-test + rustup/rsproxy）；容器内
   `cargo build --workspace --offline`（宿主 `~/.cargo/registry` 挂载做
   缓存）通过；`sandlock-0.8.6-cp311-cp311-linux_x86_64.whl` 可构建。
   macOS 无法编译 sandlock-core（seccomp/Landlock 仅 Linux），stub
   编译加了非 Linux 宿主宽容（build.rs，Linux 上仍致命）。
2. **R1 通配解析**：`NetTarget::HostWildcard` + 校验（`**`/`*.`/`*.com`/
   `*.*.x` 拒绝）+ `ResolvedNetAllow.wildcard_domains`（不 DNS）；
   `format_net_rule` 往返。
3. **R2 映射表**：`network/dns_synth.rs` —— `SyntheticDns`
   （127.0.0.2/8、双向映射、LRU 4096、耗尽 fail closed）+
   `wildcard_suffix_matches`（子域匹配/裸域不匹配/大小写不敏感）。
4. **R3/R4 连接判定**：`destination_verdict_with_host`；
   `connect_on_behalf` 合成段反查（无映射拒连）→ 实时 DNS 解析改写
   sockaddr 代连 → 解析结果二次 IP 校验。`NetworkPolicy::AllowList` 增
   `wildcard_domains`；`NetworkState` 增 `synthetic_dns`。
5. **测试**：fork 新增 20 用例；`cargo test -p sandlock-core --lib`
   `745 passed`（2 个 cow/seccomp 容器 root 环境性失败，基线一致）。
   fork 改动在 `feature/network-wildcard` 分支。

### 下一步（Block A 未完）

- **M3 项目接入已接线（2026-08-29）**：`gateway_common/network.py` 在
  `E2B_ENABLE_NETNS` 时放开通配 allowOut（否则维持 400）；
  `SandlockExecutor` 增加 `enable_netns`（kwargs `netns`，默认 False 兼容
  旧 wheel）；`deploy/compose/docker-compose.prod.yml` worker 加 `NET_ADMIN` +
  `net.ipv4.ip_forward=1` + `E2B_ENABLE_NETNS`；`envd_service/netns.py`
  在 worker 启动时配 ip_forward + veth 网段 MASQUERADE；`deploy/docker/Dockerfile.envd`/
  `test-runner` 加 iptables；fork wheel（含 `netns` FFI/Python 绑定）已可
  构建。**待办**：把 worker/测试镜像的 sandlock 来源切到 fork wheel
  （M6 wheel 矩阵/私有源），跑 security 通配 e2e + 全量回归；HTTP ACL +
  netns 组合用例。

## 本会话已完成（Block A 第二阶段 — per-sandbox netns 完整落地）

fork 分支 `feature/network-netns`（基于 feature/network-wildcard）：

1. **netns/veth**：子进程在 userns 之前 `unshare(CLONE_NEWNET)`；父进程
   从全局池（10.200.0.0/16 → /30，沙箱=+2 网关=+1）分配、`IFLA_NET_NS_FD`
   建 veth（修过 VETH_INFO_PEER 嵌套与 `IFLA_NET_NS_FD=28` 常量）、配
   网关端、两端口 UP；子进程经 pipe 收地址后配置自己一端（地址 + 默认
   路由）并回传 ifindex。失败路径删 host 端 veth，teardown 显式删。
2. **DNS 网关**：`gateway:53` UDP listener，通配 A 查询 → `SyntheticDns`
   合成 IP（TTL=0、RA/RD），其余转发 worker 上游；`/etc/resolv.conf`
   memfd 虚拟化；connect 与 send 路径豁免网关端点（glibc res_send 先
   connect UDP socket——connect 不豁免则 res_query 直接 -1，排查最久的坑）。
3. **netlink 视图**：`NetlinkState` 增 `VethView`（子进程回传 ifindex），
   GETLINK/GETADDR dump 加入 veth（否则 glibc AI_ADDRCONFIG 只见回环，
   getaddrinfo 不发 DNS 直接 -3）。
4. **HTTP 代理**：`spawn_transparent_proxy` 增 `bind_ip`，netns 模式绑
   网关地址（代理创建移到 veth 建立之后）。
5. **SSRF 护栏**：通配解析后的真实 IP 拒绝私网/回环/链路本地/CGNAT/ULA/
   组播（放行 198.18/15 与 TEST-NET，防透明代理/拦截 DNS 误伤）；二次
   校验带 hostname 上下文（AllowList 下真实 IP 才能通过通配规则）。
6. **测试**：lib `762 passed`（2 个既有 cow::seccomp root 环境性失败）；
   integration `428 passed`（1 个事务合并 root 环境性失败）。新增
   `test_netns.rs` 三用例全绿：loopback 隔离 / 通配 DNS 合成 IP /
   通配 connect 到真实目标。
7. **补充（fork abe7bf8）**：UDP 通配落地——sendto/sendmsg/sendmmsg 对
   合成 IP 反查 hostname、通配判定、实时解析 + SSRF 护栏、改写 sockaddr
   后代连（QUIC 等 UDP 通配可用）；`check_ip_destination` 被新的
   `resolve_send_destination` 取代。`test_netns.rs` 扩为 5 用例全绿：
   loopback 隔离 / 通配 DNS 合成 IP / 通配 TCP connect（本地 fixture，
   不依赖外部 DNS）/ 通配 UDP 到达真实目标 / HTTP ACL 经网关代理重定向。
   全部改 multi_thread runtime（current-thread 会饿死 supervisor/DNS 任务
   导致 create 挂起）+ 30s 快速失败超时。全量：lib 762 passed（2 既有
   root 环境性失败）、integration 430 passed（1 既有 root 环境性失败）、
   wheel 可构建、Python `Sandbox(netns=True, net_allow=["*.example.com:443"])`
   可用。
8. **并行安全（fork 8709846）**：`WorkerLocalHost` 改为每实例独立
   `198.18.0.x` 地址（首个空闲，进程级互斥锁保护地址与 `/etc/hosts`
   读写，Drop 只删自己的行/地址），三个本地服务器用例绑定实例地址。
   netns 套件在**默认并行**下 5/5 通过（0.2s），不再需要
   `--test-threads=1`。注意：既有 `test_control` 族在并行下随机互踩
   （每次失败成员不同、单独跑都过），与 netns 无关；并行全量基线
   429+2（control 随机 + txn root 环境性），串行基线 430+1。
9. **无特权默认路径（fork 89b31d9）**：通配运行时改回共享 netns +
   loopback DNS 网关（每沙箱绑 `127.0.0.x:53`，resolv.conf 不支持端口故
   每沙箱独立 loopback 地址）；合成 IP 段迁到 `10.250.0.0/16`（与网关
   段彻底分开，connect 豁免优先于合成反查）；netlink 合成视图新增固定
   文档地址虚拟 `eth0`（192.0.2.1/24 + 2001:db8::1/64）让 glibc
   `__check_pf`/AI_ADDRCONFIG 看到非 loopback 族（顺带修复无特权实时
   DNS 基线问题）。**默认路径完全无特权**；per-sandbox netns（veth +
   loopback 隔离）保留为 `netns(true)` / `E2B_ENABLE_NETNS` 可选增强。
   项目侧 wildcard allowOut 默认放行（不再依赖 egressProxy 或
   E2B_ENABLE_NETNS）。验证（历史基线）：lib 763+2、integration 432+1
   （root 环境性，后已改为全程非 root 全绿，见"验证基线"）。
   Block B/C 在同一无特权 seccomp/loopback 模型上实现。

环境注意：`sandlock-dev:latest` 已加 iproute2；集成测试需
`--privileged --network host`；e2e 连接用例临时改容器 resolv.conf 为
8.8.8.8 并配 ip_forward + MASQUERADE（本环境 DNS 被透明代理改写为
198.18.x，护栏已放行）。

## 本会话已完成（Network API 阶段 B1 — egressProxy）

1. **LD_PRELOAD SOCKS5 隧道库** `envd_service/egress/libegress_proxy.c`：
   hook `getaddrinfo`（域名→合成 127.0.0.2/8 + hostname 映射，沙箱内不发
   DNS）与 `connect`（恢复 hostname/直连 IP → 库内 allowOut/denyOut 过滤 →
   SOCKS5 RFC1928/1929 握手，域名走 ATYP=domain 远程 DNS；非阻塞 fd 同步
   等待连接完成；代理不可达/握手失败 → ECONNREFUSED，fail closed）。
2. **执行器集成**：`egressProxy` 模式下 net_allow 只放行代理端点，库经
   LD_PRELOAD 注入（chroot 模式复制进 workspace/.egress 并以
   /home/user/.egress 路径加载），EGRESS_PROXY/ALLOW/DENY/USER/PASS 走
   env；库源码由 worker 首次使用时 `cc` 构建并缓存到
   `E2B_IMAGE_CACHE_DIR/egress/`。动态更新复用 `update_network`。
3. **控制面校验**：`egressProxy.address` 必须解析到公网 IPv4（拒绝
   私网/loopback/link-local，防 SSRF），username/password ≤255；update 中
   `egressProxy: null` 显式清除。create/update/detail 全链路。
4. **测试**：安全用例 2 个（隧道 + ATYP=domain 断言、deny 拦截），单元
   校验用例；测试镜像加 `gcc`/`libc6-dev`。
5. **限制**：仅 IPv4 代理；仅动态链接应用（python/node）；过滤在沙箱内
   库做（LD_PRELOAD 方案固有妥协）；rules/maskRequestHost 仍 400。

## 本会话已完成（最终容器镜像 + 分离部署）

1. **镜像分离**：`deploy/docker/Dockerfile.control-plane` 只含 `gateway_common` +
   `control_plane`；`deploy/docker/Dockerfile.envd` 只含 `gateway_common` + `envd_service`，
   且 multi-stage 预编译 `libegress_proxy.so` 到 `/opt/egress/`（最终镜像
   不带 gcc）。代码层解耦：env 工具函数移到 `gateway_common/env.py`；
   控制面 `create_app` 对 `RuntimeRegistry` 懒导入，分离模式用 no-op
   哨兵（pause/resume/snapshots/kill 等调用安全）。
2. **构建脚本** `deploy/scripts/build-images.sh`：buildx 多架构
   （`linux/amd64,linux/arm64`），多平台需 `PUSH=1`。
3. **部署示例** `deploy/compose/docker-compose.prod.yml` + `deploy/compose/.env.example`：控制面 +
   gateway + worker-1/2/3（YAML anchor）+ Redis（共享状态）+ 可选本地
   registry（profile）；`deploy/compose/docker-compose.yml` 单机示例控制面改为
   `E2B_ENABLE_LOCAL_NODE=false`。
4. **验证**：两镜像构建成功（镜像内容分离确认）；`compose config` 有效；
   macOS 起栈（`--no-build` 强制用分离镜像）三 worker 验证全绿：
   `multinode_smoke.py`（跨节点分布覆盖 3 worker/命令/文件/stdin/配额释放）
   + `deploy/scripts/deployment_smoke.py`（追加迁移 worker-2→worker-1 共享
   workspace 文件保留、network 回显/原子更新）。踩坑记录：宿主 3000 端口
   被占用需换端口；本机 docker daemon 里 `python:3.11-slim` 曾被 arm64
   spike 覆盖导致沙箱 qemu-arm64——拉回 amd64 后正常（顺带验证了 rootfs
   digest 缓存失效）。

## 本会话已完成（Network API，阶段 A + C）

1. **network 配置全链路**：`POST /sandboxes` 的 `network` 字段与
   `PUT /sandboxes/{id}/network`（官方 `update_network`，原子替换、省略字段
   清空、`allowPublicTraffic` 仅创建时可设）。wire 格式为 camelCase
   （`allowOut`/`denyOut`/`allowPublicTraffic`/`rules`；更新体 `allow_internet_access`
   兼容 SDK 的 snake_case 拼写）。`SandboxRecord`/`RuntimeSandbox` 新增
   `network` 字段并持久化（Redis 可见），`as_detail` 回显。
2. **运行时映射（阶段 A）**：`allowOut`/`denyOut`/`allowInternetAccess` →
   Sandlock `net_allow`/`net_deny`（互斥：两者都在时 allowlist 模型胜出、
   deny CIDR 覆盖的 allow 条目被剔除）；`rules` 域名 → `http_allow`
   （80/443 透明 MITM ACL，镜像 rootfs 模式把临时 CA 拼进每沙箱信任副本并
   注入 `SSL_CERT_FILE`/`CURL_CA_BUNDLE`）。`LocalExecutor` no-op。
3. **动态更新**：控制面保存后 `_push_network_config` 推送到节点 agent
   （`POST /agent/sandboxes/{id}/network`），agent 更新 `RuntimeSandbox` 并
   调用 `SandboxRuntimeContext.update_network`；RPC `_context` 增加网络配置
   drift 检测，下条命令用新策略。
4. **allowPublicTraffic**：envd HTTP/Connect 鉴权在
   `runtime.allow_public_traffic` 时跳过 token 校验（仍校验 sandbox id）。
5. **显式拒绝（no fake success）**：`egressProxy`、`maskRequestHost`、
   `rules.transform`（header 改写）返回 400，标注依赖阶段 B 代理层。
6. **测试**：单元（校验/映射/序列化/executor kwargs）14 个；契约 5 个
   （回显/原子更新/404/拒绝/allowPublicTraffic/SDK 往返）；Sandlock 强制
   1 个（deny 后 update_network 恢复 egress，`example.com:443` 实测 200）。
   注意：多节点 harness worker 现设 `enable_network=True`（默认 false 时
   网络策略不生效）。

## 本会话已完成

1. **P0 并发迁移锁**：`SandboxRegistry.try_acquire_migration/release_migration`
   —— Redis 多副本用 `SETNX` 标记 + TTL（WATCH 对比删除，兼容 fakeredis），
   单进程用等价内存锁；并发 migrate 第二个请求返回 409；失败路径
   `finally` 释放标记。单元测试覆盖跨副本互斥、过期释放、错误 token 不能
   释放；契约测试覆盖持锁 409 + 释放后可迁移。
2. **P1 双活窗口关闭**：迁移先调源节点 `DELETE ?keepFiles=true`（停
   runtime、杀进程树、保留文件）再导出/导入/切路由；失败时自动在源节点
   重新 provision（`_provision_local` 现支持幂等替换过期挂载符号链接），
   并回滚已持久化的 node_id。契约测试：目标 provision 失败（指向控制面
   自身地址 → 快速 404）后沙箱命令仍可用。
3. **P1 rootfs 缓存失效**：`resolve_image_rootfs` 缓存目录名加入镜像
   digest（`docker image inspect` 的 RepoDigests/Id），tag 更新自动换新
   rootfs；先 pull + login 再算 digest，避免首次解析重复解包。README 补充
   说明与手动清理方式。
4. **P2 JS SDK 测试在宿主跑通**：`pytest tests/sdk/js`（npm 在宿主机），
   vitest 全量通过；README 测试表更新为 macOS / Linux 均可。

## 本会话验证结果

```text
macOS: 200 passed, 6 skipped（tests/unit + tests/contract + tests/sdk/python + tests/sdk/js）
Linux: 225 passed, 1 skipped（全量含 Sandlock/registry/真实 Redis/模板隔离）
```

## 本会话已完成

1. **Template COPY 文件上下文**：`GET /templates/{id}/files/{hash}`（201）返回
   带 token 的上传 URL，`PUT .../upload` 校验 token 并存储归档；构建时解包进
   `ctx/` 作为 docker build context；COPY 步骤生成 Dockerfile
   （`--chown/--chmod`）；构建先 push 成功才置 ready。
2. **真实 Redis 多副本**：`SandboxRegistry.save()` 持久化 node_id/TTL/暂停等
   变更；Redis 模式下 `get/list` 总读共享存储；`E2B_REDIS_URL` 生效；
   `tests/contract/test_redis_multireplica_e2e.py` 用真实 redis（容器内
   `redis-server` 进程，macOS 回退 docker redis:7）。
3. **节点故障迁移**：`POST /sandboxes/{id}/migrate`（可选 `nodeID`）——
   agent `export/import`（tar.gz）、配额转移、源清理、gateway 路由失效
   （`E2B_GATEWAY_URL`）；非共享卷按卷亲和约束。
4. **命令输出日志**：`command-logs.jsonl` 落盘（stdout/stderr/PTY、ANSI
   剥离、1MB/命令 + 16MB/文件截断），`GET /sandboxes/{id}/logs` 合并返回；
   远程经 `GET /agent/sandboxes/{id}/logs` 拉取。
5. **共享 workspace 模式**：`E2B_SHARED_WORKSPACE_ROOT` 时迁移只切路由
   （跳过 export/import），源目录不删（`DELETE /agent/sandboxes/{id}
   ?keepFiles=true` 仅释放 runtime）。
6. **镜像仓库分发**：`E2B_IMAGE_REGISTRY` 构建后 tag+push，模板镜像名切到
   `{registry}/{templateID}`；worker resolver 本地无镜像先 `docker pull`。
7. **镜像仓库认证**：`E2B_IMAGE_REGISTRY_USERNAME/PASSWORD`（控制面+worker），
   `docker login --password-stdin`（密码不进 argv）。

## 本会话已完成（E9.1–E9.4 资源争用闭环）

1. **E9.1 活动/空闲检测**：`SandboxRecord` 增 `last_active_at`（tz-aware、只前进）与
   `priority`（0–10，默认 5，越界/脏值钳制）；worker 心跳携带每沙箱
   `sandboxActivity`（`envd_service/agent.py`），控制面 `apply_activity_report`
   合并进共享 registry，落库按 `E2B_ACTIVITY_PERSIST_INTERVAL_S` 写节流；
   `E2B_SANDBOX_IDLE_THRESHOLD_S` 判定空闲（≤0 = 永不空闲）。
   活动来源 = 经 envd/Connect 鉴权的请求（**含 `/mcp` 代理**：该路由自带鉴权，
   单独打点）+ 控制面生命周期调用；只读轮询与内部端点故意不算活动
   （否则监控轮询循环就能让空闲沙箱永远逃过驱逐），清单见
   `docs/resource-contention.md` §3.1。
2. **E9.2 pause 释放配额 / resume 重新准入**：pause 置 `paused` 并幂等归还
   全局/租户/节点配额（现场保留）；resume 先重新准入（不足 → 503，记录保持
   paused）再翻状态；`paused`/`orphaned` 不被 TTL 回收；彻底删除在回调之后才
   归还配额，不二次释放。创建可带 `priority`，非法值 400。
3. **E9.3 驱逐（默认开启）**：容量准入失败时按「低 `priority` → 空闲最久 →
   租户配额权重 → `sandbox_id`」顺序驱逐 `running`+idle 受害者后重试；跨租户
   默认关闭（admin key / `E2B_EVICTION_CROSS_TENANT` 才放行）；动作默认 kill，
   `E2B_EVICTION_PREFER_PAUSE=true` 先 pause（仍不够才 kill 已 pause 候选）；
   被驱逐沙箱 `GET` 404 文案含 `(evicted: evicted-idle)`，响应头
   `x-e2b-eviction-reason: evicted-idle`；防风暴 = 单次创建最多
   `E2B_EVICTION_MAX_PER_CREATE` 个 + 轮次最小间隔 `E2B_EVICTION_MIN_INTERVAL_S`
   （进程内节流，多副本不共享）；驱逐通知可查窗口 `E2B_EVICTION_NOTICE_TTL_S`。
4. **E9.4 创建排队（默认 30s / 100）**：驱逐后仍无容量时进 `CreateQueue`
   （`control_plane/queue.py`）等 registry 真正归还配额（
   `add_on_quota_released` 广播唤醒）或 ≤1s 兜底 tick，超时才回原 503；队列满
   → 429 `Sandbox create queue is full` + `retry-after: 1`，**且计入
   `recent_failures`**（扩缩容信号）；排队不占配额/pending marker，同 id 并发
   重试幂等 201，不超卖；无 FIFO/公平性承诺（多副本各自排队）。

配置项与默认值：

```text
E2B_SANDBOX_IDLE_THRESHOLD_S     300   # 空闲阈值秒；0 = 永不空闲
E2B_ACTIVITY_PERSIST_INTERVAL_S  30    # 活动时间戳落库节流秒；0 = 每次更新都写
E2B_EVICTION_ENABLED             true  # 驱逐总开关（用户决策：默认开启）
E2B_EVICTION_PREFER_PAUSE        false # true = 先 pause 保留现场再 kill
E2B_EVICTION_MAX_PER_CREATE      3     # 单次创建最多驱逐数（防风暴）
E2B_EVICTION_MIN_INTERVAL_S      1     # 驱逐轮次最小间隔秒（进程内节流）
E2B_EVICTION_NOTICE_TTL_S        3600  # 驱逐通知可查窗口秒
E2B_EVICTION_CROSS_TENANT        false # 跨租户驱逐开关（安全默认关；admin 放行）
E2B_CREATE_QUEUE_TIMEOUT_S       30    # 创建排队超时秒；0 = 关闭排队（驱逐后直接 503）
E2B_CREATE_QUEUE_MAX             100   # 并发排队上限；满 → 429 + retry-after: 1
```

注意事项（上线前必读，细节见 `docs/resource-contention.md` §3.1/§5/§8）：

- **默认值会改变客户端可观察行为**：`E2B_EVICTION_ENABLED=true` 会踢掉空闲沙箱
  （默认阈值 300s）；`E2B_CREATE_QUEUE_TIMEOUT_S=30` 意味着满池时 `POST /sandboxes`
  最长挂 30s 才拿 503 —— 客户端/网关读超时更短的部署必须把它调到读超时以下或设 0。
- **跨租户驱逐默认关闭**（`E2B_EVICTION_CROSS_TENANT=false`）：租户只能踢自己
  租户的空闲沙箱，否则"创建沙箱"就成了打别人空闲沙箱的武器；admin key 放行。
- **节流与排队都是控制面进程内状态**：`E2B_EVICTION_MIN_INTERVAL_S` 与
  `CreateQueue` 深度不跨副本共享（不超卖由共享配额 ledger 保证），需要全局
  节流/全局队列得把状态迁到 Redis。
- **活动来源有边界**：只有"经过 envd/Connect 鉴权的请求 + 控制面生命周期调用"
  算活动（`/mcp` 代理已单独打点）；沙箱自身**出站**流量、纯 CPU 长任务不算，
  这类沙箱要用高 `priority` 或调大阈值保护。
- **fork 侧一条假告警（不影响功能，未在本仓库修）**：容器测试里每个沙箱都会打
  `UserWarning: Policy field 'notify_rate_limit' is set but not wired through FFI`。
  实际 `sandlock._sdk._build_from_policy` 确实调用了
  `sandlock_sandbox_builder_notify_rate_limit`，只是同文件里的守卫清单
  `_NativePolicy._HANDLED_FIELDS` 漏登记了该字段名（tip `be387c7` 仍如此）。
  属 `third_party/sandlock` 的一行修复（往集合里加名字），我们的
  `E2B_SANDBOX_NOTIFY_RATE_LIMIT` 是生效的；记录以免下次误判成"配额没起作用"。
- **`wheels/fork` 与子模块 tip 的一致性无法从产物本身判定**：wheel 时间
  （09-02 11:11）早于 E7 的两个 sandlock 提交（11:12 `2eb3e7f`、`be387c7`），
  所以发布前**照例重跑** `scripts/build-sandlock-wheels.sh` + 重建镜像最稳妥；
  已验证的是：当前 wheel 下 E7 门控套件
  `E2B_TEST_NET_ISOLATION=1 pytest tests/contract/test_mcp_netns.py` 3/3 通过。

## 未完成 / 待办（按优先级）

**E9 已完成**（E9.1–E9.4，见上）。**测试环境也已清零**（2026-09-02 晚，见
「2026-09-02（测试环境专项）」一节：Linux 容器全量 0 failed / 0 error）。剩余：
**E8.1 部署后远程 smoke**（受"不做远程部署"约束暂缓）、运维 **O1/O2/O3**
（目标机 XFS prjquota / TLS 代理层 / 凭据管理），以及**上线前必须**：重建
`wheels/fork`（E7 最终 sandlock tip）→ 重建 worker/测试镜像 → 推 ACR。

新增待办（本轮定位、需要环境或上游动作）：

- **T1** 在真实 XFS/ext4 目标机上验证"沙箱 chmod 自己创建的文件"（overlayfs 上
  EPERM，用例目前带证据跳过）；顺带核对 `E2B_PER_SANDBOX_UID=true` 的组合。
- **T2** `third_party/sandlock`：把 `notify_rate_limit` 登记进
  `_NativePolicy._HANDLED_FIELDS`（一行，消掉每次建沙箱的假告警）。
- **OCI 形态（`E2B_BASE_IMAGE=python:3.11-slim`）在本机仍不能全绿**：本轮实测
  `73 failed / 744 passed / 28 errors in 788s`（日志 `tmp/final-oci-linux.log`），
  主因是**每次建沙箱都要向 Docker Hub 取一次 manifest**（缓存目录名带 digest，
  用于 tag 更新自动失效），匿名配额耗尽后就是成片 401/429 与建沙箱失败后的
  `KeyError: 'sandboxID'` 连锁；也发现一例 `token exchange failed: 401`
  （携带了凭据去换 Docker Hub 的匿名 token，属 fixture 环境变量污染，待清）。
  两类出路，需要产品决策：(a) 按文档要求给可认证 registry（ACR，现成路径）;
  (b) 让解析器在 registry 不可用/限流时回落到“上次成功的 digest”（写一个
  `<image>.digest` 侧车），代价是限流期间感知不到 tag 更新。本轮没有改这个策略。
- **已决策：每沙箱一个 sandlock 实例**（fork 文档 §8，取代共享资源组 P10），用来根除 §3.8 的配额
  超卖。改造实质是 Policy/Instance/Child 三层拆分（不是删一个 guard）：现状每次 create 都新建
  Sandbox+runtime、运行时状态是单槽（`child_pid`/`leader_pid`/三个 stdio 端）、
  `Process<'a>` 借 `&mut Sandbox` ⇒ 结构上只能一个活子进程、`ResourceState` 在 `do_create_stdio`
  里 new、控制目录靠 `kill(pid,0)` 判活。问题清单 Q1–Q15、里程碑 M0–M4 与验收标准见
  `third_party/sandlock/docs/e2b-integration.md` §8；E2B 侧接线记 backlog **E10**。
  **回归风险最大的是 Q10**：`max_processes` 从"每命令 64"变成"整箱 64"，必须同步调默认值。
- **T3** ✅ 已修（G2，2026-09-06，见本文件顶部 ⚡ G2 块）：`SnapshotRegistry` 的
  `create_from_sandbox`/`expand_to` 复制前拒绝"目标落在源之内"（`ValueError`），并以
  ignore 回调剪掉工作区里嵌入的快照存储根——不再出现
  `snapshots/snap_X/fs/snapshots/snap_X/fs/...` 自嵌套；普通同名目录不受影响
  （`tests/unit/test_snapshot_registry.py` 3 条 + snapshot 契约回归全绿）。事故证据目录
  `tmp/stale-20260902/`（4.9G，确认无用即可单独删除）。

### P2 — 真实 NFS 部署未验证

共享 workspace/volume 目前只在同一主机共享目录模拟；NFS/CSI 上的
root_squash、uid=1000 映射、命令 IO 延迟未实测。部署验证时注意
`E2B_SHARED_VOLUME_ROOT` / `E2B_SHARED_WORKSPACE_ROOT` 各节点路径语义一致。

**E6.4 进展（2026-09-02）**：已在容器内内核 nfsd + XFS prjquota 导出 +
双 NFS 客户端上实测：路径语义一致、迁移保留文件、projid 继承、sync 挂载
超限即时 ENOSPC、async 挂载 fsync/close 延迟报错（建议 sync）、多 worker
独立限额、root_squash 影响。探针 `deploy/scripts/nfs_quota_probe.sh` 与
结论已写入 `docs/production-deployment-requirements.md §5`。**仍待办**：
在真实生产 NFS（Linux 目标机）上重跑探针并核对 per-sandbox uid ×
no_root_squash 组合（OrbStack 宿主 NFS 代理使容器化自动探针不稳定）。

### P3 — 遗留优化 / 后续 Block（sandlock fork）

- 迁移导出 tar 仍含卷挂载符号链接空条目（功能等价，可显式排除）；
- 未配置 `E2B_GATEWAY_URL` 时迁移后路由依赖 gateway 30s 缓存 TTL（文档已知）；
- ~~**Block B — header 改写（rules.transform / maskRequestHost）**~~：fork
  `feature/network-inject` 已完成（R8–R11，见上）；剩项目侧 5B.4 映射
  （依赖 fork wheel 切源后生效）。
- **Block C — SOCKS5 on-behalf**：`ConnectPlan::Socks5Upstream` 替代
  LD_PRELOAD egress 库（R12–R14），纯 TCP 握手无特权可实现。
- **M6 — wheel 矩阵**：cp310–314 × x86_64/aarch64 + 私有 index / git 安装
  切换（worker 与测试镜像当前仍装 PyPI 0.8.6）。
- **M7 — 上游 PR**：把无特权部分整理成面向 `multikernel/sandlock` 的 PR；
  netns 留 fork 分支（上游是无特权项目，netns 特权要求难被接受）。
- **LD_PRELOAD 隧道已知限制**：静态/Go 应用不受影响（可后续用 sandlock
  on-behalf connect 的 SOCKS5 分支替代，语义更完整）；IPv6 目标/代理未
  隧道（直接 real connect）。
- spec.md 其余官方 API 面（iam/lifecycle 等）仍未支持，入口处
  `UNSUPPORTED_FIELDS`/`UNSUPPORTED_ENDPOINTS` 明确拒绝。

## 2026-09-02（测试环境专项）：Linux 容器与 macOS 全量清零

上一轮记为"抖动用例/环境类失败"的东西几乎都有确定根因。本轮之后：
**Linux 容器全量 `843 passed / 18 skipped / 0 failed / 0 error`**（此前基线
`28 failed / 804 passed / 6 errors`），**macOS 全量（unit+contract+sdk python+
sdk js+security）`803 passed / 53 skipped / 0 failed`**（此前 unit+contract
记为 `2 failed / 732 passed`，并写着"单独重跑都会通过"——实际是稳定复现的）。

| 症状 | 真根因 | 处理 |
|------|--------|------|
| macOS `test_tls::test_plain_http_against_tls_port_fails` 稳定失败、`test_command_logs::test_remote_command_output_in_logs` 抖动 | httpx 的 `trust_env` 在 macOS 会回落到**系统代理**（本机 127.0.0.1:7897），发往测试临时端口的请求被代理截走：明文打到 TLS 端口拿到的是代理自己的 `502`（不是 TLS 握手失败），日志读取也多一跳 | `tests/conftest.py` 导入时把 loopback 固定进 `NO_PROXY`（`ac59152`） |
| `tests/unit/test_mcp_gateway.py` 3 例 registry 401 | 单测里 `base_image="python-mcp:3.14"` 是项目自建镜像（Docker Hub 无此 repo），`create_executor` 却真的去做 registry 解析，而下一行就把 executor 换成 fake | autouse fixture 打桩 `resolve_image_rootfs`（`87874a0`） |
| SDK fixtures 429（`test_stdin`/`test_snapshots` 6 ERROR、`test_metadata_filter_via_query`） | 全套件一分钟创建量超过生产默认的 create 限流 120/min | 真起服务的 fixtures 显式 `create_rate_limit_per_min=0`（限流本身有自己的用例） |
| 9 例 `buildctl is not available in this image` | test-runner 镜像里没有 buildctl（只有 `Dockerfile.control-plane-gateway` COPY 了） | `Dockerfile.test-runner` 同法 `COPY --from=moby/buildkit`（`22e5acc`） |
| 有 buildctl 之后 9 例仍 `buildkit build exited with code 1` | buildkitd fixture 把配置文件写在**容器本地路径**再 `-v` 出去；daemon 在宿主解析源路径，找不到就挂成空目录 → buildkitd 直接退出（`read .../buildkitd.toml: is a directory`） | 改走 `/workspace` 写入 + 宿主路径挂载（与 htpasswd 同一条已记录规则），not-ready 时把容器日志带进 skip 原因 |
| 4 例 `st_uid == 0` / `assert 0 != 0` 类 uid 断言 | `/workspace` 是 virtiofs，**chown 是 no-op**，per-sandbox uid 断言在这块盘上没有意义 | 镜像内 `ENV E2B_TEST_TMP_ROOT=/var/lib/e2b-test-runtime`（容器原生存储；此前 conftest 注释已建议但没人设过） |
| 7 例 `sandlock_create failed` / `sandlock_popen failed`（egress 3、fork network 2、rootfs/uid 2） | 测试把 0700、runner 所有的 `mkdtemp()`/`tmp_path` 交给以 uid 1000 运行的沙箱，沙箱进不去自己的工作目录/走不到 chroot；更糟的是 `exit_code != 0` 的"拒绝"断言因此**空过** | `tests/security/conftest.py` 统一补齐沙箱可见性 + 把工作目录属主给沙箱 uid（模拟 `apply_sandbox_ownership`），并加沙箱能力探针（`0a235b4`） |
| `test_create_with_template_image` 428、`test_volume_mount_paths...` 428 | 单机 harness 没像 multinode 那样预热模板镜像：冷缓存 + 官方 SDK 不带 `X-Sandbox-Id` → 按契约快速失败 428 | fixtures 预热本节点会用到的镜像；顺带修掉跨会话残留（harness 目录复用导致模板记录里带着**上一轮已消失的 registry 端口**） |
| `test_mcp_gateway_tools` "mcp-gateway did not start listening" | 用例还连已退役的固定 50005 端口；现在每沙箱一个 `MCP_PORT`，只能经 `/mcp` 代理 + `E2b-Sandbox-Id` 路由 | 本地/远端统一走代理路由 |

顺带修掉的**产品缺陷**（不是测试问题，`d8b7f41`）：

1. **构建产物切镜像名没落盘**：`Template.build` 配了 `E2B_IMAGE_REGISTRY` 后把记录改成
   `{registry}/{templateID}`，但只改内存，而 `TemplateRegistry.get_by_name` 每次都从磁盘
   重读 → 下一次 create 又去解析 `e2b-local/...`（Docker Hub 401）。现在 save。
2. **无 registry（单机形态）的构建产物谁都解析不了**：`type=image,name=...` 的输出只留在
   buildkit 内部，而 worker 侧只会走 OCI distribution API（去 docker socket 那步在
   `27c6c62` 删了）→ 本地建的模板沙箱根本起不来（README 却写着可用）。现在导出
   **OCI layout tar** 到 `E2B_IMAGE_CACHE_DIR/_oci/`，resolver/peek 命中本地 tar；跨节点
   仍需 registry，这成了两种形态的明确分界。
3. **registry 连接失败不带地址**：`[Errno 111] Connection refused` 从解析器深处冒出来，
   看不出在连谁。现在 `RegistryError` 带上 URL（这次定位就靠它）。

### 仍未解决（已定位，需要环境/上游动作）

- **公共镜像不再直连 Docker Hub**（09-03 处理）：09-02 的复跑把匿名配额打满，
  `3 failed / 789 passed / 54 errors` 全是 `registry-1.docker.io 429
  TOOMANYREQUESTS`（含 harness 预热 fixture 的连锁）。两层处理：查询频率降为
  "每进程每 tag 60s 一次"（`E2B_IMAGE_MANIFEST_TTL_S`），并且解析器现在支持
  `E2B_REGISTRY_MIRRORS`（`host=mirrorA|mirrorB,...`，按端点依次尝试、origin
  兜底、token 每端点重新换发；404 不做无谓重试）。测试镜像默认走
  `docker.m.daocloud.io|docker.1ms.run`（与 buildkitd 用的同一个源，实测两个
  镜像源的 digest 与 Docker Hub 一致）。生产要彻底摆脱公共仓库配额，仍建议把
  基础镜像镜像到 ACR（`deploy/scripts/build-and-push.sh`）并把
  `E2B_BASE_IMAGE` 指过去。
- **顺带修掉的凭据外泄**：`E2B_IMAGE_REGISTRY_USERNAME/PASSWORD` 是一对按部署
  配的凭据，过去解析**任意**镜像都会带着它去换 token —— 解析公共镜像时既被源站
  拒（`token exchange failed: 401 incorrect username or password`，09-02 OCI
  复跑里出现过），也把凭据发给了第三方 host。现在只有 `E2B_IMAGE_REGISTRY`
  的 host 与镜像 host 一致时才带（`registry_credential_host()`）。
- **overlayfs 上"沙箱自己写的文件"归属不对**（T1，现在有两种实测表现，都要在真实
  XFS/ext4 目标机上复测）：
  1. 沙箱内对自己刚写的文件 `chmod`/`touch` 返回 EPERM（宿主侧看该文件属主是 root，
     而沙箱 host uid 是池内 uid）⇒ `pip install` 这类流程在本机不可用；受影响用例
     `test_user_cli_install_within_workspace_persists` 带证据跳过。
  2. 共享卷的 **1777 + sticky"他人不可删"保护在本机测不出来**：
     `test_volume_shared_rw_across_distinct_uids` 现在会先量一下 A 写入文件的真实宿主
     属主（实测 uid 0，而 A 的 host_uid 是 20000），不匹配就带原因跳过——原先这条
     断言"通过"其实是在一个未被施加的身份上碰巧成立。要点：这不是测试能修的问题，
     要么目标机复测确认，要么确认 fork 的 chroot/on-behalf 写路径是否以 supervisor
     身份落盘（若是，属 fork 侧隔离语义问题）。
- **fork 侧假告警**：`Policy field 'notify_rate_limit' is set but not wired through FFI`
  仍在（`_NativePolicy._HANDLED_FIELDS` 漏登记，一行修复，属 `third_party/sandlock`）。
  顺带核实：当前 `wheels/fork` 的 `.so` **确实导出**了
  `egress_proxy/http_auth/credential/host_mask/notify_rate_limit/pid_ns/net_isolation/fd_inject_connect`
  全部符号（此前只按时间戳存疑）；发布前重跑构建脚本仍是硬性步骤。
  子模块 `upstream-pr/netns-free-clean` 现多一个**纯文档**提交 `afe4921`
  （`docs/e2b-integration.md`），代码基线仍是 `be387c7` ⇒ 不需要因此重建 wheel。
- **已修（G2，2026-09-06）**：`tmp/stale-20260902/test-runtime/**/snapshots/snap_X/fs/snapshots/snap_X/fs/...`
  的同一快照自嵌套（路径长到 `ENAMETOOLONG`，根因 = `SnapshotRegistry` base 默认 =
  workspace_base，展开会把存储复制进快照自身）。守卫与用例见本文件顶部 ⚡ G2 块；
  证据目录 `tmp/stale-20260902/`（4.9G，确认无用即可删）。

### 09-03：公共镜像改走国内镜像源，两种形态全量复跑

- 解析器新增 `E2B_REGISTRY_MIRRORS`（`host=mirrorA|mirrorB,...`）：镜像**身份**仍是
  `registry-1.docker.io/library/python`，但按端点依次拨号（mirror 优先、origin 最后
  兜底），token 每个端点重新换发（各 mirror 有自己的 realm），404 视为答案而非端点
  故障（不重复重试）。实测 `docker.m.daocloud.io` 与 `docker.1ms.run` 对
  `python:3.11-slim` 返回的 platform digest 与 Docker Hub 完全一致
  （`sha256:d10533…`），测试镜像默认带上这两个源。
- 凭据作用域：`E2B_IMAGE_REGISTRY_USERNAME/PASSWORD` 之前解析**任意**镜像都会带上，
  既被公共源拒（`401 incorrect username or password` / `403 DENIED`），也把部署凭据
  发给无关 host。现在按调用方 Settings 的 `image_registry_host` 精确匹配；worker 的
  `EnvdSettings` 和生产 compose 的 worker 环境变量都补齐了 `E2B_IMAGE_REGISTRY`
  （否则 worker 无从知道凭据属于哪个 host）。
- 复跑（镜像源生效后，配额不再是变量）：
  - 默认形态（不带 `E2B_BASE_IMAGE`）：`851 passed / 18 skipped / 0 failed / 0 error`
    （`tmp/session-scratch/fin-D-default.log`）；
  - OCI rootfs 形态（`E2B_BASE_IMAGE=python:3.11-slim`）：从 `73 failed / 28 errors`
    变成 `853 passed / 16 skipped / 0 failed / 0 error`
    （`tmp/session-scratch/fin-E-oci.log`）。
- 顺手清掉一条**假 skip**：`test_fork_network_features` 的 wildcard 本地 origin
  fixture 只看 `ip addr add 198.18.0.99/32 dev lo` 的返回码，共享 VM netns 里这个
  地址常是上一轮残留（`ipv4: Address already assigned`），于是被误报成"缺
  NET_ADMIN"，把 fork 的无特权通配 DNS 路径整条跳过；现在把"已存在"当成功（只有
  自己加的才回收），其它失败把 iproute2 原文写进 skip 原因（`08a21dc`）。

### 09-03（续）：把"能跑却在跳"的用例真正跑起来，并禁止再次跳

测试镜像现在自己把环境补齐（`deploy/docker/entrypoint.test-runner.sh` + Dockerfile）：

- `xfsprogs` + `e2fsprogs` + `nodejs`/`npm`（npm 走 `registry.npmmirror.com`）；
- 启动时 losetup + `mkfs.xfs` + `mount -o prjquota` 挂到 `/var/lib/e2b-sandboxes`，
  并把 `E2B_TEST_TMP_ROOT` 挪到这块 XFS 上（容器内 loop 设备与宿主共享、节点可能缺失，
  脚本会 `mknod` 显式分配并在退出时只解绑自己那个）；成功后导出
  `E2B_XFS_QUOTA_INTEGRATION=1`；
- 默认就跑两种形态：`E2B_BASE_IMAGE=python:3.11-slim`（镜像 rootfs）与
  `E2B_TEST_NET_ISOLATION=1`（netns + fd 注入 connect）；
- `E2B_TEST_STRICT_SKIPS=1`：**运行器能力型** skip（XFS prjquota、`lsattr`/npm、
  NET_ADMIN、沙箱文件属主测量）一律判为失败，杜绝"环境没配好 → 覆盖率悄悄掉"。
  部署形态开关（`E2B_BASE_IMAGE` / `E2B_TEST_NET_ISOLATION`）不在禁用清单里：
  主动关它们是合法的窄矩阵，不是丢覆盖率。

跑起来之后当场暴露三个真问题（前两个已修，第三个转为跟踪项）：

1. **`fs_denied` 会废掉 per-sandbox host uid 隔离（已修，重要）**。非 chroot 形态下
   `/proc/kcore`、`/sys`、`/dev/shm` 本来就不在 Landlock 可读白名单里，denial 是冗余的；
   但一旦下发，fork 就走"代打开"路径，**沙箱自己创建的文件属主变成 supervisor（host uid 0）**
   ⇒ 沙箱内 `chmod`/`touch` 自己文件 EPERM，共享卷 1777+sticky 的跨 uid 保护也失效。
   现在 denial 只在镜像 rootfs 形态下发；实测（真 XFS）纯 sandlock 形态下
   `uid=1000/4242/4243` 归属正确、`chmod` 正常、跨 uid sticky 保护真的生效
   —— 以前这条用例在 virtiofs/overlay 上的"通过"是没有意义的。
2. **`lsattr` 缺失让孤儿 project 只报不清（已修）**。`_scan_project_dirs` 依赖
   `lsattr -p -d`，镜像里没这个二进制时 `reconcile_orphan_projects` 静默返回
   `skipped: 用了 block 但找不到 project 目录`；补 `e2fsprogs` 并让缺失时打 WARN，
   生产节点要求也写进 `docs/production-deployment-requirements.md`。
3. **SL-1（上游 sandlock 问题，正文已并入 fork 仓库
   `third_party/sandlock/docs/e2b-integration.md` §3.1）：路径中介以 supervisor 身份执行系统调用**。
   启用路径中介时（实测：`fs_denied` 非空或 chroot；源码另含 COW 一组），fork 通过 `SECCOMP_RET_USER_NOTIF` 把
   `openat/unlinkat/mkdirat/renameat2/fchmodat/fchownat/utimensat/...` 交给 supervisor
   代执行，而 `seccomp/notif.rs` 里没有 `setfsuid/seteuid`——于是沙箱自己创建的文件属主是
   uid 0、请求的 mode 不生效，`unlinkat/renameat2` 也按 root 判定，共享目录上的 per-uid
   保护（1777+sticky）不再成立。Landlock 白名单**没有**被绕过（越界写入仍被拒），所以定级是
   "多租户 DAC 隔离缺陷"而不是逃逸。可复现脚本、源码定位与修法建议见
   [docs/sandlock-upstream-issues.md](sandlock-upstream-issues.md)（SL-1）。
4. **T4/T5：镜像 rootfs(chroot) 形态的两个已测出缺陷（strict xfail 跟踪，不 skip）**
   - T4 `net_isolation` + chroot：MCP 入站端口映射起不来（`/mcp` 代理整段连不上），
     纯 sandlock 形态 3/3 通过 ⇒ `test_mcp_full_path_under_net_isolation` 在该形态
     `xfail(strict=True, run=False)`。**状态更新（2026-09-06，Task 10/FUP-E1）：已关闭**——
     根因是 envd 侧 base-image 组成（slim rootfs 无 mcp-gateway → ENOENT exit 2），非 fork；
     改用 MCP-capable 基镜像 `python-mcp:3.14`（`deploy/docker/Dockerfile.mcp-base`）后
     chroot+netns MCP 契约两形态 3/3 绿，xfail 已摘。详见顶部「M4 收口」块。
   - T5 chroot 形态下共享卷写入仍经 supervisor 归属（`fs_denied` 的代打开路径在
     chroot 里无法回避，见上）⇒ `test_volume_shared_rw_across_distinct_uids` 在该形态
     `xfail(strict=True)`，非 chroot 形态必须真通过（并新增断言
     `written_by == ra.host_uid`，回归即红）。**仍开（唯一 xfail）**：route-B supervise
     部署（euid == 沙箱 host uid）后摘除，见顶部 follow-up 列表。

全开一次（镜像 rootfs + netns + XFS + npm + strict）：

- `867 passed / 1 skipped / 2 xfailed / 0 failed / 0 error`（`tmp/session-scratch/full-final.log`）
- 2 条 xfail = T4/T5（chroot 形态那两个真缺陷，2026-09-03 观测；T4 已于 2026-09-06
  Task 10 关闭，剩余 T5 见顶部「M4 收口」块）；唯一 skip 是
  `test_volume_quota.py:274`（"XFS supported: degradation path not exercised"）——
  它测的是"XFS 不可用时的降级路径"，本机 XFS 可用所以走另一分支，属于互斥分支而非能力缺失。

### （历史）容器全量剩下的 18 条 skip，09-03 续已消除其中 17 条

| 分组 | 数量 | 为什么跳 | 怎么跑起来 |
|---|---|---|---|
| XFS project quota（`test_xfs_project_quota.py` / `test_volume_quota.py`） | 10 | 需要 `E2B_XFS_QUOTA_INTEGRATION=1` **且**工作目录在带 `prjquota` 的真实 XFS 上；容器根是 overlay，镜像里也没有 `xfs_quota`（日志里 `FileNotFoundError: 'xfs_quota'`） | 测试镜像装 `xfsprogs`，容器里 losetup 一个 XFS+prjquota 挂到 `/var/lib/e2b-sandboxes`（`docs/sandbox-disk-quota.md §4` 有步骤），再加 `-e E2B_XFS_QUOTA_INTEGRATION=1`；生产上就是运维项 O1 |
| netns 隔离形态（`test_mcp_netns.py`） | 3 | 显式门控：`E2B_TEST_NET_ISOLATION=1` + worker 侧 `E2B_ENABLE_NET_ISOLATION=true E2B_FD_INJECT_CONNECT=true`（默认关，因为运行时基线是无 netns 的无特权形态） | 按 `third_party/sandlock/docs/netns-isolation-fd-injection.md` 的那套开关跑一遍专用作业 |
| 沙箱文件属主（T1 的两条：`test_sandlock_isolation::test_user_cli_install...`、`test_uid_permissions::test_volume_shared_rw...`） | 2 | 实测本机 overlayfs 上沙箱写的文件宿主属主是 uid 0（沙箱 host_uid 是 20000），于是 ① 沙箱 `chmod` 自己文件 EPERM，② 共享卷 1777+sticky 的跨 uid 保护无法成立。两条都改成"先量再断言"，不匹配带证据跳过 | 真实 XFS/ext4 目标机复测（O1/T1）；若 chroot 形态仍落 root 属主，则是 fork 的写入身份问题 |
| 需要 OCI 形态（`test_fork_network_features`、`test_template_isolation`） | 2 | 只有设了 `E2B_BASE_IMAGE`（镜像 rootfs 沙箱）才有意义 | 已在带 `E2B_BASE_IMAGE=python:3.11-slim` 的那次全量里执行（所以那一轮是 16 skip） |
| JS SDK（`tests/sdk/js`） | 1 | 测试镜像里没有 npm | 本机 macOS 全量里跑（`812 passed`）；或镜像装 `nodejs`/`npm` 后在容器里跑 |
  - macOS 全量（unit+contract+sdk python/js+security）：`812 passed / 53 skipped /
    0 failed`（不依赖 Docker Hub：本机用 local executor，镜像解析用例跳过）。

## 验证命令与基线

```bash
# macOS 全量（含 SDK python/js 与 security；security 里需要 sandlock 的用例会跳过）
tmp/testenv/bin/python -m pytest tests/unit tests/contract tests/sdk/python \
  tests/sdk/js tests/security -q -p no:cacheprovider

# Linux 容器全量基线（E8.2 正式数字：local executor，**不带** E2B_BASE_IMAGE；
# 含真实 Redis / registry 认证 / Sandlock / perf 用例）
docker run --rm --privileged --network host \
  -e E2B_HOST_PROJECT="$(pwd)" \
  -v ~/.orbstack/run/docker.sock:/var/run/docker.sock \
  -v "$(pwd):/workspace" -w /workspace \
  e2b-sandlock-test:latest pytest tests --perf -q -p no:cacheprovider

# OCI rootfs 模式（E2B_BASE_IMAGE=…）需要可认证 registry（ACR）或未被限流的
# 出网，否则 Docker Hub 匿名拉取限流让建沙箱用例拿 428 warm_required
# （2026-09-02 两次全量尝试均如此；环境前提，非代码缺陷）
docker run --rm --privileged --network host \
  -e E2B_BASE_IMAGE=python:3.11-slim \
  -e E2B_HOST_PROJECT="$(pwd)" \
  -v ~/.orbstack/run/docker.sock:/var/run/docker.sock \
  -v "$(pwd):/workspace" -w /workspace \
  e2b-sandlock-test:latest pytest tests --perf -q -p no:cacheprovider
```

- 测试镜像 `e2b-sandlock-test:latest`（deploy/docker/Dockerfile.test-runner，国内源，
  已含 redis-server、**buildctl** 与 `E2B_TEST_TMP_ROOT=/var/lib/e2b-test-runtime`）；
  改依赖后需重建：`docker build -f deploy/docker/Dockerfile.test-runner -t e2b-sandlock-test:latest .`
- `--network host` + `E2B_HOST_PROJECT` 是容器内 docker CLI 访问宿主
  localhost 端口 / 挂载宿主路径的前提（registry/Redis 端口映射、htpasswd
  挂载）。
- 多节点冒烟：`deploy/scripts/multinode_smoke.py` + `deploy/scripts/deployment_smoke.py`
  （后者含迁移/共享 workspace/network 更新）；compose：
  `docker compose -f deploy/compose/docker-compose.multinode.yml up -d`。

### E8.2 基线（2026-09-02 上午确认，日志 `tmp/e82-linux-local.log` / `tmp/e82-macos.log`）——**已被下一节取代，仅作历史**

> 下面这组数字里的 28 failed / 6 errors 全部在当天晚上的专项里定到了根因并修掉（见「2026-09-02（测试环境专项）」一节）；保留原文是为了不丢掉当时的取证。

- **Linux 容器全量**（local executor，无 `E2B_BASE_IMAGE`）：
  `28 failed / 804 passed / 17 skipped / 6 errors in 219.63s`（perf 用例无失败）。
  failed/error 名单与 pre-E9 快照（HEAD bc597a8：28 failed / 709 passed / 19
  skipped / 6 errors in 107.66s，日志 `tmp/e82-linux-base.log`）**逐名比对完全
  相同** → E9 零新增回归；28+6 全是既有环境依赖类：
  registry/buildkit（`python-mcp:3.14` Docker Hub 拉取 401、模板构建
  `buildctl is not available in this image`）、内核/特权（`sandlock_popen
  failed`、uid 归属断言、shm/egress 断言）、SDK 建沙箱 fixture 撞 create 限流
  429（`test_stdin`/`test_snapshots` 各 3 ERROR）、mcp-gateway 未监听。17 skipped
  = XFS 配额集成未开（10）+ net-isolation 形态未开（3）+ JS SDK 需 npm（1）+
  NET_ADMIN / `E2B_BASE_IMAGE` 门控（3）。
- **macOS 本机 venv**（`tmp/testenv/bin/python`，unit + contract）：
  `11 failed / 689 passed / 23 skipped / 33 errors in 36.91s`。11 failed
  （gateway / mcp_gateway / template_build）与 33 errors（oci_registry /
  migration / multinode / network_api / redis_multireplica_e2e / tls /
  command_logs）全是既有环境类：端口绑定 PermissionError、docker/buildkit
  不可用、registry·ACR 凭据 env 污染；23 skipped = XFS 配额集成未开（10）+
  需 root/root worker 的 chown·uid 断言（10）+ net-isolation 形态未开（3）。
  ⚠️ 这组数字**随 runner 权限而变**：同一棵树在"可绑定任意端口 + 可访问
  docker"的终端环境下是 `2 failed / 732 passed / 23 skipped`（原 33 errors 里的
  绝大多数其实只是端口权限受限）。当时把这 2 例记成"单独重跑都会通过的抖动"，
  **这个判断是错的**：`test_tls` 稳定失败（宿主系统代理劫持了测试流量），
  `test_remote_command_output_in_logs` 同因，只是被时序掩盖。两者见下一节。

### sandlock fork 验证（Linux 容器）

```bash
# 1) 一次性 root 构建（FFI/测试二进制；入口脚本会 chmod 共享 target）
docker run --rm --privileged --network host --user root --entrypoint bash \
  -v "$(pwd)/third_party/sandlock:/src" \
  -v ~/.cargo/registry:/opt/cargo/registry \
  -v "$(pwd)/tmp/sandlock-dev/cargo-config.toml:/opt/cargo/config.toml" \
  -w /src sandlock-dev:latest -c '
    cd /src && CARGO_TARGET_DIR=/src/target-linux cargo build -p sandlock-ffi --offline
    chmod -R a+rwX /src/target-linux'

# 2) 全程非 root 测试（入口 root 准备后自动降权 nobody；命令用 bash -c，
#    不要 bash -lc —— login shell 会重置 PATH）
docker run --rm --privileged --network host \
  -v "$(pwd)/third_party/sandlock:/src" \
  -v ~/.cargo/registry:/opt/cargo/registry \
  -v "$(pwd)/tmp/sandlock-dev/cargo-config.toml:/opt/cargo/config.toml" \
  -w /src sandlock-dev:latest bash -c '
    cd /src && CARGO_TARGET_DIR=/src/target-linux \
    cargo test -p sandlock-core --offline --lib
    cargo test -p sandlock-core --offline --test integration -- --test-threads=1
    cd python && PYTHONPATH=/src/python/src python -m pytest tests -q -p no:cacheprovider'

# netns 套件（需要 CAP_NET_ADMIN；非特权环境自动跳过）
cargo test -p sandlock-core --offline --test integration test_netns -- --test-threads=1
```

## 关键文件索引

| 文件 | 内容 |
|------|------|
| `control_plane/api/sandboxes.py` | migrate（per-sandbox 锁 + 先停源 runtime + 失败回滚）、network 创建/`PUT /sandboxes/{id}/network`/`_push_network_config`、logs 合并、keep_files 销毁、create 调度（E9.3 驱逐重试 / E9.4 排队段，429 满队列计入 `recent_failures`）、pause/resume 端点 |
| `control_plane/registry/manager.py` | Redis save/get/list、TTL 回收、`try_acquire_migration`/`release_migration`（SETNX + TTL / 内存锁）、E9 记录字段与配额语义（`last_active_at`/`priority`/`touch`/`is_idle`、`pause`/`resume`、`evict_for_capacity`、`add_on_quota_released`、paused/orphaned 不被 TTL 回收） |
| `control_plane/queue.py` | E9.4 `CreateQueue`（asyncio 排队：容量释放广播唤醒 + ≤1s 兜底 tick、超时/满队列 429、无全局状态） |
| `control_plane/config.py` | E9 配置项（`E2B_SANDBOX_IDLE_THRESHOLD_S`、`E2B_ACTIVITY_PERSIST_INTERVAL_S`、`E2B_EVICTION_*`、`E2B_CREATE_QUEUE_*`，默认值见「本会话已完成（E9）」节） |
| `envd_service/agent.py` | export/import/logs/keepFiles 端点、`POST /agent/sandboxes/{id}/network` 更新端点、心跳携带每沙箱 `sandboxActivity`（E9.1 上报入口） |
| `control_plane/api/templates.py` | COPY 上传链路、registry push/login |
| `control_plane/registry/nodes.py` | `select_and_reserve(exclude_node_id)`、`reserve_node` |
| `envd_service/process/logs.py` | 命令输出 JSONL 采集 |
| `envd_service/runtime/image_resolver.py` | rootfs 解包、pull、registry login、digest 缓存 key |
| `E2B_IMAGE_CACHE_DIR/_oci/` | 无 registry 时本地构建的 OCI layout tar + `.link` 侧车（resolver 优先读它） |
| `third_party/sandlock/docs/e2b-integration.md` | sandlock 侧唯一事实源：已落地方案 / 待实施 P1–P8 / 未解决 SL-1（T5 摘除前置 route-B）/ 验证矩阵；T4 已于 2026-09-06 关闭（fork `upstream-pr/netns-free-clean`，M4 状态随 Task 11 收口） |
| `docs/sandlock-upstream-issues.md` | 编号映射索引（内容以上述 fork 文档为准） |
| `third_party/sandlock/docs/{e2b-integration,sandlock-network-wildcard,netns-isolation-fd-injection,sandbox-level-cow,upstream-pr-netns-free}.md` | sandlock 侧全部方案与问题文档（E2B 撰写的部分已迁入 fork 仓库） |
| `envd_service/gateway.py` | 路由缓存 + `/internal/routes/{id}/invalidate` |
| `gateway_common/network.py` | network 校验/规范化 + sandlock 策略映射 |
| `envd_service/executors/sandlock.py` | network→net_allow/net_deny/http_allow + 每沙箱 CA 注入 |
| `envd_service/egress/libegress_proxy.c` | LD_PRELOAD SOCKS5 隧道库（getaddrinfo/connect hook + 过滤 + ATYP=domain） |
| `envd_service/egress/build.sh` | 库构建脚本（gcc） |
| `envd_service/runtime/context.py` | `update_network` + RPC drift 检测 |
| `tests/contract/test_network_api.py` | network 契约（回显/更新/拒绝/allowPublicTraffic） |
| `tests/security/test_network_enforcement.py` | deny→update→allow 强制用例 |
| `tests/security/test_egress_proxy.py` | SOCKS5 隧道 + 远程 DNS + deny 拦截用例 |
| `tests/unit/test_network_config.py` | network 校验 + sandlock 映射单测 |
| `tests/conftest.py` | live/multinode/registry/redis fixtures（session 级） |
| `tests/security/conftest.py` | 沙箱存储可见性 helper（`make_sandbox_visible`/`sandbox_tmpdir`）与能力探针（`sandbox_owns_files_it_creates`） |
| `tests/contract/test_migration.py` | 迁移 + 共享 workspace + 持锁 409 + 失败回滚用例 |
| `tests/contract/test_redis_multireplica_e2e.py` | 真实 Redis 多副本 |
| `tests/contract/test_command_logs.py` | 命令日志合并（本地 + 远程） |
| `tests/contract/test_template_upload.py` | COPY 上传契约 |
| `tests/sdk/python/test_templates.py` | 构建、COPY、registry push/pull/认证 |
| `tests/unit/test_sandbox_registry.py` / `test_redis_multireplica.py` | 迁移锁单元测试（内存 + fakeredis） |
| `tests/unit/test_sandbox_activity.py` / `test_pause_quota.py` / `test_eviction_execution.py` / `test_eviction_selector.py` / `test_create_queue.py` | E9.1–E9.4 单测（活动上报/空闲、pause 配额、驱逐选择与执行、排队） |
| `tests/contract/test_idle_activity.py` / `test_pause_resume_quota.py` / `test_pause_resume_metrics_logs.py` / `test_eviction_api.py` / `test_create_queue_api.py` | E9.1–E9.4 契约（含驱逐 404 通知 + `x-e2b-eviction-reason`、排队 429/503） |
| `deploy/docker/Dockerfile.control-plane` / `deploy/docker/Dockerfile.envd` | 分离的最终镜像（envd multi-stage 预编译 egress 库，最终镜像无 gcc） |
| `deploy/compose/docker-compose.prod.yml` / `deploy/compose/.env.example` | 生产部署示例（控制面+gateway+worker+Redis+可选 registry） |
| `deploy/scripts/build-images.sh` | buildx 多架构（amd64/arm64）镜像构建脚本 |
| `gateway_common/env.py` | env 工具函数（消除 control_plane↔envd_service 交叉导入） |

## 配置速查（新增项）

```text
E2B_SHARED_WORKSPACE_ROOT      共享工作目录（迁移只切路由）
E2B_IMAGE_REGISTRY             模板镜像 push 目标
E2B_IMAGE_REGISTRY_USERNAME    仓库认证（控制面 push / worker pull）
E2B_IMAGE_REGISTRY_PASSWORD    仓库认证
E2B_GATEWAY_URL                迁移后通知 gateway 失效路由
E2B_IMAGE_MANIFEST_TTL_S       60    # 同一镜像 tag 的 manifest 查询缓存秒数
                                     # （0 = 每次 create 都查；见 image_resolver）
E2B_REGISTRY_MIRRORS                  # 公共仓库镜像源：host=mirrorA|mirrorB,...
                                     # Origin 始终作为最后一个端点兜底；测试镜像
                                     # 已默认走 docker.m.daocloud.io|docker.1ms.run
```
