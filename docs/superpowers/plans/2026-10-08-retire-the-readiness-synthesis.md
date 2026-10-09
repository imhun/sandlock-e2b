# 退役 readiness 合成（B）实施计划（2026-10-08 登记，未开工）

**Goal**：删掉 `network/readiness.rs`（`poll`/`ppoll`/`epoll_wait`/`epoll_pwait` 的就绪合成）与它的
全部状态，让这四条系统调用**永久离开通知表**。之后：映射端口的**事件循环型**服务只在 bind 注入
形状下成立；非注入形状保留**阻塞/线程式** `accept()`（那条路是 `handle_accept_impl` 的 defer 等
`conns`，与合成无关）。

## 为什么现在可以退（读数都在仓里）

- **生产形状根本不走它**：`E2B_NET_BIND_INJECT` 默认 `true`，而 A（fork `dda8dd7`）之后注入形状连
  trap 都没有了 —— 集群实测 `epoll_wait(0)` **16.33 → 0.49 µs/次**（`docs/deploy-clusters.md`
  §7.56、§7.55「补读数」、`docs/production-deployment-requirements.md` §2.4.7）。
- **唯一走它的形状没有实例**：只有 `E2B_NET_BIND_INJECT=0` 的沙箱会走合成；仓库里没有任何车道设它，
  E2B 的 MCP 路径按沙箱自动加映射且默认注入。
- **安全面是净减少**（逐条对账见 §2.4.7 与 N88/A 的提交信息）：它是唯一一条"按事件循环迭代"去
  `pidfd_getfd` 复制子进程 fd、把 events **写进子进程内存**（目标指针由子进程给）、并 unbounded
  defer 的路；而这些动作**不做任何 allow/deny**。入站真正的门是 Landlock 的 `BIND_TCP` 白名单
  （内核侧）、映射配置本身、以及注入路径的 fail-closed —— 都不在这条路上。

## 代价（要写进文档，不能悄悄丢）

非注入形状 + **事件循环型**服务器 = 宿主侧连接照常进队列，但沙箱永远不会调 `accept()` ⇒
**服务静默不响应**（uvicorn/asyncio/Node 那类）。所以必须配 fail-closed 守卫，见 T3。

## 任务

- [x] **T1 先写两枚钉子（取 RED）**（fork `8396264`）
  1. **跨表钉子**："凡被 trap 的 nr 必有 handler chain"。今天**没有任何测试**兜这条 ——
     `build_dispatch_table` 只被 `seccomp/notif.rs` 用，dispatch 里那句注释自己写着"trapped 但没有
     chain 是 planning bug（N81 那次）"。钉子的形状：拿 `notif_syscalls` 的结果去问 dispatch 表，
     逐个核 chain 存在。**先证明它抓得到**（故意留一个无 handler 的 nr → 红）。
  2. **"poll 族在任何形状都不在表里"**。今天在注入关的形状下它是**红**的（那四条还在表里）。
- [x] **T2 删代码，钉子转绿**（fork `ff0b8e7`）：`network/readiness.rs` 整个删；`seccomp_plan` 的
  `INBOUND_READINESS_SYSCALLS` 与 `push_optional(poll/epoll_wait)` 删；`dispatch.rs` 里那四条注册删；
  readiness 的 4 条集成测试（`test_net_isolate::..._{epoll,poll}_event_loop_serves_external`，
  plain/chroot 两套）与 readiness 单测删。**N88 ② 的"复用 fd 号不被合成接管"钉子改写成表成员钉子**
  （没有 trap 就不存在陈旧注册 —— 表述更强，不是丢覆盖）。映射本身的四条阻塞式测试（mcp roundtrip /
  internal loopback / lifecycle / 与 fd 注入共存）**保留**。
- [x] **T3 E2B 侧 fail-closed**（同上批发版）："**这个沙箱会带映射**（`port_mappings` 非空，或 MCP 路径隐式加的
  那条）而 `E2B_NET_BIND_INJECT=0`" ⇒ 启动/建箱**拒绝并点名**。守卫要判"会不会带映射"，不能只看
  全局开关（MCP 的映射是按沙箱加的）。配单测；文档口径统一改成"非注入映射 = 只支持阻塞接受"。
- [x] **T4 门禁十一档 + 基线**（fork `67da4eb`）：core_lib 945 − readiness 单测、
  core_integ 577 − 4，按实测写。
- [x] **T5 发版（要单独拍板）**（2026-10-09，版本 `0.1.0-1164-g5fee77f-20261009-101349`，发版记录
  §7.59）：注入档 MCP 往返应仍 ~0.2 ms；非注入形状的"事件循环型服务"从
  "81 ms 能用"变成"配置被拒"（这就是这次改动的对外差别）。照 §7.55/§7.56 的两批 apply + 预热 +
  `cgroup_acceptance.py` 复验。

## 风险与回退

- 风险只有可用性那一条（静默不响应）；T3 的 fail-closed 把"静默"变成"启动即拒"。
- **回退杆**：`git revert` 这次改动 + 重建 wheel + 重滚镜像 tag。**不要**手工只把 poll 族塞回表
  而不恢复 handler —— 那会变成"被 trap 但没有 handler"（N81 型 planning bug：白付一跳、吃通知预算、
  什么都不决策），而 T1 的跨表钉子正是为了让这种状态再也进不了树。
- 不动的东西：N88 ②/A 之后 `close`/`epoll_ctl` 的状态；`inbound.rs` 的宿主 listener + eager worker +
  阻塞 `accept` 路径。

## 登记时的判据（做完要能回答）

1. 注入档 MCP 往返不变（集群 pod 内 `probe_inbound_readiness.py`，~0.2 ms）；
2. 非注入 + 带映射的建箱被 T3 具名拒绝（不是挂死）；
3. 跨表钉子绿，且它在"故意留一个无 handler 的 nr"下会红；
4. 十一档门禁与基线一致。

---

## 执行记录（2026-10-08，T1–T4 完成；T5 未开工）

**T1–T4 + 评审修复批的提交**：fork `8396264`（T1 两枚钉子 + 钉子第一次跑抓到的既有缺陷，见下）→
fork `ff0b8e7`（T2 删 1422 行/加 116 行）→ fork `67da4eb`（T4 基线）→ 父仓 `6f3d169`（T3 守卫 +
docs + submodule 指针）→ 评审修复批（见文件末"评审与修复"，含第三枚钉子
`the_cross_table_check_catches_a_missing_chain` 与 `create_app` 的真正启动闸门）。

**判据读数（四条，逐条对上面「登记时的判据」）**：

1. 注入档 MCP 往返：**未在集群复验**（T5 未开工）。本地证据：注入形状的映射链路在门禁与
   `test_net_isolate` 的 5 条阻塞式往返里全绿；合成那一侧已不存在。
2. 非注入 + 带映射的建箱**被具名拒绝**（不是挂死）：
   `tests/unit/test_net_isolation_config.py::test_a_mapped_sandbox_without_injection_is_refused_by_name`
   （消息点名端口 + `E2B_NET_BIND_INJECT` + 为什么），以及
   `…::test_injection_off_with_worker_wide_mappings_fails_at_startup`（确定性那一半）。
3. 跨表钉子绿，且**它能抓到**：人工变异（往 filter 里加 `SYS_syncfs`）当场红并报 `[306]`；
   常驻自测 `the_cross_table_check_catches_a_missing_chain` 一直在树上。
   **它第一次跑就抓到一条既有的真问题** → 见 N92（`fchmodat2` 被 trap 没有 chain）。
4. 十一档门禁与基线一致（**全部实测**）：core_lib 944 / core_integ 573 / ffi 104 / cli 100 /
   supervise 57 / supervise_cost 3 / cli_build 0 / python 465 / oci 157 / supervise_root 4 /
   mediation_2uid 9。两格基线按"实测 − 删掉/新增"解释清楚后刷新（946 − 5 条 readiness 单测 +
   3 枚钉子 = 944；578 − 5 条集成测试 = 573）。

**T2 一处比计划多做（已记账）**：`InboundListener.pending` 是只有合成器读的状态，随合成一起删。

**本机环境的两条限制（复现时先读）**：

* `deploy/scripts/fork-gate.sh` 的默认相位可用；**三个 root 相位不能走它**（脚本固定降权到
  65534，而 `test-all.sh --oci-root` 拒绝非 root 运行）。root 相位按 fork `test-all.sh` 头部的
  规范形态跑：`--entrypoint sh` + prep + `CARGO_HOME/CARGO_TARGET_DIR` 指到 fork 内 + 直接
  `sh scripts/test-all.sh --<phase>`。
* 本轮 E2B 侧 `tests/unit` 的基线是本机 4 条固有红（`test_real_root_gate` 的 pivot_root、
  `test_xfs_quotactl_backend` ×2 —— 这两族在 macOS 上必红），另有本轮修掉的一处
  `test_executor_policy::test_mcp_gateway_netns_identity_mapping`（它编码的是 N89 之前、
  注入关掉也允许映射的旧形状）。
