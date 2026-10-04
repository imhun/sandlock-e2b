# SEC-R3-01 修复方案（设计，未实施）

对应发现：`docs/security-audit/findings-k0s-2026-10-04.md` §2

**状态（2026-10-04）：Phase 1 已实施并本地验证。** Phase 2 / Phase 3 **未实施**（仍是设计）。
同批落地的还有 STATIC-5 与 STATIC-2 两处修复，见 §8。所有代码位置为 2026-10-04 的实测行号。

---

## 0. 要修的到底是什么（一句话）

`secure` 是**客户端可控的开关**，它把 envd 的访问令牌变成空串；
而 envd 的守卫写成了 `if runtime.access_token and token != runtime.access_token`
—— **空令牌让这个条件恒真**，于是 `E2b-Sandbox-Id`（同样客户端可控）就是全部凭证。
再叠加 gateway 自身零认证、`/internal/routes/{id}` 不检查 `allowPublicTraffic`，
最终形成**对外零凭据 RCE**。

四个环节**每一个单独修都能大幅降级，但只有全部修才闭合**：

| # | 环节 | 位置 | 单独修的效果 |
|---|---|---|---|
| ① | 令牌守卫 fail-open | `http/auth.py:68`、`connect/router.py:67` | **直接消灭 RCE** |
| ② | `secure` 由客户端决定 | `api/sandboxes.py:1103` → `registry/manager.py:1630,1657` | 消除"制造空令牌"这个动作 |
| ③ | 路由端点不设防 | `api/internal.py:1112-1124` | 外部无法指名任意沙箱 |
| ④ | gateway 零认证 | `envd_service/gateway.py:277+` | 外部无法到达 envd |

---

## 1. 前置事实（已实测，方案建立在这些之上）

- **仓库内零处依赖 `secure=False`**：`rg 'secure\s*=\s*False|"secure":\s*false'` 无命中，
  测试与 SDK 封装都没用。⇒ 收紧它**不破坏任何现有行为**。
- `traffic_access_token`（`registry/manager.py:171`）**是从未被赋值的死字段**：
  只有数据模型（写入 `:357`/`:405`、读取 `:459`），没有生产者。
  ⇒ 它正好是"公开流量专用令牌"该落的位置。
- **加载路径会重新打开这个洞**：`registry/manager.py:459`
  `envd_access_token=data.get("envd_access_token", "")`。
  一条被污染的 Redis 记录（空/缺字段）就能让守卫再次恒真。
  ⇒ **只改守卫不够，加载侧也必须 fail-closed。**
- **生产无存量空令牌沙箱**（2026-10-04 清查，见 §4 步骤 1）⇒ Phase 1 无迁移成本。
- `docs/HANDOFF.md:1659-1660` 仍写着旧行为
  （"allowPublicTraffic 时跳过 token 校验"）。**照这份文档改会把洞改回去**，
  必须同批修正（见 §5）。

---

## 2. 分期方案

### Phase 1 —— 止血（3 处改动，零兼容性成本）

**P1-a. 守卫改为 fail-closed**（这是真正杀死漏洞的那一处）

`envd_service/http/auth.py:68` 与 `envd_service/connect/router.py:67` 同一处改法：

```python
# 现状（fail-open：空令牌 => 恒真 => 放行）
if runtime.access_token and token != runtime.access_token:
    raise ...

# 改为（fail-closed：令牌必须存在且匹配）
if not runtime.access_token or token != runtime.access_token:
    raise HttpAuthError(401, "Invalid access token")
```

要点：
- 两处**必须同时改**，否则 HTTP 半边与 Connect 半边行为不一致，攻击者换一条路即可。
- 措辞不要暴露"这个沙箱没有令牌"（区分 `not runtime.access_token` 与
  `token != ...` 两种失败，统一回同一句话，避免变成沙箱状态预言机）。
- 保留 `SEC-K0S-005` 那段注释，并**补一句**：空令牌不再等于放行。

**P1-b. 加载侧 fail-closed**（堵住持久化污染）

`control_plane/registry/manager.py` 的 `SandboxRecord.from_dict`：
`envd_access_token` 缺失或为空时**不要**默默填 `""`，而是让该记录无法提供服务
（见 P1-c 的落点），并打一条 audit。

**P1-c. 拒绝 `secure=false`**，在 API 边界显式拒绝，而不是静默忽略：

`control_plane/api/sandboxes.py:1103` 附近。推荐**显式 400 + 明确文案**，
不要静默改成 `true` —— 静默会让调用方以为自己拿到了无认证沙箱。

> **Phase 1 之后 `secure=false` 不可用。** 这是有意的：
> `secure` 在 SDK 里是公开参数（`e2b/sandbox_sync/main.py:171`，
> docstring 写 "Envd is secured with access token and cannot be used without it"），
> 但**本仓库从未实现过"不用令牌"这个语义** —— 之前的实现方式是
> 把令牌留空，那不是"不用令牌"，那是"任何人都能用"。
> 如果产品确实需要公开沙箱，走 Phase 3。

### Phase 2 —— 入口设防（纵深防御，Phase 1 之外的独立价值）

**P2-a. `/internal/routes/{id}` 真正检查 `allowPublicTraffic`**

`control_plane/api/internal.py:1112-1124`。当前只检查三件事：沙箱存在 / 节点存在 / 节点 healthy。

建议：非 `allowPublicTraffic` 的沙箱，只有**持有该沙箱 access token**的调用方才可解析路由；
公开沙箱才允许被 gateway 这类内部组件免凭据解析。

**P2-b. gateway 边缘加认证**

`envd_service/gateway.py` 的 `proxy()`。当前只要求 `E2b-Sandbox-Id` 头。
建议二选一：
- **默认**：要求有效平台 API key（与 `POST /sandboxes` 同源）；
- **公开流量**：走一条独立的、显式标注的路径，并强制携带该沙箱的公开令牌。

P2 的价值在于：**Phase 1 万一被回退或漏改某处，Phase 2 仍然挡着。**
两层独立才是纵深防御；单层只是单点。

### Phase 3 —— 若产品确需"公开沙箱"（用死字段实现，不要复活空令牌）

**不要**用空令牌表达"公开"。用 `traffic_access_token`，它已经在数据模型里：

1. 创建时 `allowPublicTraffic=true` ⇒ 控制面**生成**一个 `traffic_access_token`，
   走**平台侧**渠道下发（admin 接口 / 签名 URL），**不回显给创建者**
   （回显给创建者就等于没有隔离 —— 创建者本来就能用 API key）。
2. envd 守卫改为接受**两个**令牌之一：`access_token`（平台/SDK）或
   `traffic_access_token`（公开流量），且后者只能访问**显式允许的方法**
   （建议先只放读，或干脆第一版只放 `/health` + `/mcp`，不放 `process.*`）。
3. `process.Process/Start` **永不**接受 `traffic_access_token`。
   公开一个可以执行命令的沙箱，等于公开一个 root shell —— 这不是权限配置问题。

> 这一条是**产品决策**，不是技术选型。请先回答：
> "公开沙箱"要公开的是**读**还是**执行**？现状下 SDK 的全部能力（含执行）
> 挂在一个令牌后面，所以"公开"目前只能二选一：公开读，或者不公开。

---

## 3. 回归测试要求（必须新增，当前一个都没有）

现状：`rg 'secure\s*=\s*False'` 零命中 ⇒ **没有任何测试覆盖这个开关**，所以它一直没被发现。

| # | 测试 | 断言 |
|---|---|---|
| T1 | 参数化：`secure` ∈ {`true`, `false`, 缺失} × 两种 transport（HTTP + Connect）× 令牌 ∈ {无, 空串, 错误, 正确} | 只有"正确令牌 + `secure=true`"返回 2xx；**其余全部 401** |
| T2 | 若保留 `secure=false` 的显式拒绝 | 返回 400，且错误文案不含"令牌为空"之类可区分信息 |
| T3 | 加载侧污染：往 Redis 写一条 `envd_access_token=""` 的记录后取用 | **拒绝服务**，而不是让它变成无认证沙箱 |
| T4 | 路由端点：非公开沙箱 + 免凭据 | `GET /internal/routes/{id}` 不得泄露 worker 内网地址 |
| T5 | gateway：免凭据 + 任意 sandbox_id | 不得代理到 envd |
| T6 | 不变量（借鉴 fork 的 `every_arch_syscall_is_classified` 风格）：遍历 `SandboxRecord` 的所有构造点 | **不存在 `envd_access_token == ""` 的可达状态** |

T6 值得强调：这个仓库已经有一个"全量分类测试"的先例
（`sys/path_surface.rs:680`），而且它**正因为覆盖了所有构造点才让一个 0.9.0-beta
的 fork 在升级内核时暴露问题**。同一手法应当用在"令牌非空"这个不变量上。

**T1 的 transport 维度必须参数化** —— 上一轮 SEC-K0S-005 就是只改了 HTTP 半边、
（或反过来）留下的缺口，两条半边都要测。

---

## 4. 上线顺序与回滚

1. **先上 Phase 1**（守卫 + 加载侧 + 拒绝 `secure=false`）。单副本滚动，观察 15 分钟。
   风险：若有外部调用方在用 `secure=false`，会开始收到 400。

   **存量清查已于 2026-10-04 做完，结论：可以直接上线，无需迁移、无需租户通知。**
   ```bash
   # 在 redis pod 内（密码从它自己的 env 读，不要写进命令行）
   kubectl exec -n sandlock redis-5bcf9567dc-5vpft -- \
     sh -c 'redis-cli -a "$REDIS_PASSWORD" --no-auth-warning KEYS "e2b:record:sbx_*"'
   ```
   现场读数：`DBSIZE 373`，键分布为 `e2b:record:vol_*`（volume 记录，300 条）、
   `e2b:quota:released:sbx_*`（配额释放标记）、以及若干 `e2b:node:*` / `e2b:*:sweep`。
   **`e2b:record:sbx_*` 一条都不存在** —— 当前没有存活的沙箱记录，
   因此不存在"已带空令牌的沙箱"，也不存在"上线后突然失联"的存量。

   > 扫描逻辑（逐条比对 `envd_access_token` 是否为空/缺失）就是上面这段 `redis-cli`：
   > 取到值后判 `envd_access_token` 是否为空字符串、以及该字段是否**整个缺失**。
   > 两个都要查 —— `registry/manager.py:459` 的 `data.get("envd_access_token", "")`
   > 对缺失字段同样给出空串，而空串就是守卫恒真的那个输入。

   仍建议上线后再跑一次同样的 `KEYS`，作为 T3（加载侧污染）的例行检查。
2. **再上 Phase 2**（路由端点 + gateway）。这一层会改变 gateway 的入站契约，
   要与 SDK 联调；建议先在 `--security-opt` 等价的预发形态验证。
3. Phase 3 只在产品决策明确后做。
4. **回滚**：Phase 1 的每一处都是"把放行改成拒绝"，回滚等于把洞放回来 ——
   所以 Phase 1 不设自动回滚，靠 T1 拦住再发布。

---

## 5. 必须同批做的非代码动作

1. **修正 `docs/HANDOFF.md:1659-1660`**。它现在写着
   "allowPublicTraffic：envd HTTP/Connect 鉴权在 `runtime.allow_public_traffic` 时跳过 token 校验" ——
   这是 **SEC-K0S-005 修复前的行为**。留着它，下一个照文档办事的人会把洞改回来。
   仓库自己的注释（`auth.py:60-66`）已经明确禁止重犯，文档应与之一致。
2. **SDK 侧同步**：`e2b/sandbox_sync/main.py:171` 的 `secure` 参数要么废弃、
   要么改语义并改 docstring。留着"关掉它就没有认证"的文档，等于对外承诺一个洞。
3. **通知**：如果 `secure=false` 曾被对外文档/集成示例推荐过，需要公告。

---

## 6. 验证方法（复用本轮探针，改完直接跑）

```bash
export E2B_API_URL=http://172.18.78.49:3000 E2B_SANDBOX_URL=http://172.18.78.49:3000
export E2B_API_KEY=$(cat tmp/audit/api_key)

# 1) secure=false 现在应当被显式拒绝（而不是静默变成 true）
curl -sS -X POST "$E2B_API_URL/sandboxes" -H "X-API-Key: $E2B_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"templateID":"base","timeout":60,"secure":false}'      # 期望 400 + 明确文案

# 2) secure=true 的沙箱，无令牌经对外入口 -> 必须 401
python3 tmp/audit3/s5_rce_proof.py                            # A/B/E 三个用例仍须 401

# 3) 集群内直连 envd（两条半边都要覆盖）
.venv/bin/python tmp/audit3/s5_rce_proof.py
```

判定标准：**§2.3 的那张 5 用例矩阵里，A/B/E 保持 401，C 从"执行成功"变成 401。**
若 C 仍是 200，说明只改了 transport 之一或加载侧没生效。

---

## 7. 与本次审计其他结论的关系

本方案只覆盖 SEC-R3-01。同一轮审计里另外两条**建议同期处理**（都很小）：

- **STATIC-5**（`c3_agent/priv/maint.c:287-300`）：`--worker` 分支补 `priv_validate_uid()`，
  或在 `run_file_op` 里拒绝 `worker_owned && recursive`。已本地实证该分支无门禁。
- **STATIC-2**：把 7 个 `*at` syscall **显式列入** `DEFAULT_BLOCKLIST_SYSCALLS`，
  而不是靠 `defaultAction` 兜底。已实测 Docker/OCI 默认档放行 `fchmodat2`。

这两条与 SEC-R3-01 无依赖关系，可并行。

---

## 8. 实施记录（2026-10-04）

### 8.1 已落地：Phase 1（SEC-R3-01）

| 环节 | 文件 | 改动 |
|---|---|---|
| ① 守卫 fail-closed | `envd_service/http/auth.py` | `if not runtime.access_token or token != runtime.access_token` |
| ① 守卫 fail-closed | `envd_service/connect/router.py` | 同上（**两条半边必须同改**，否则另一条就是旁路） |
| ② 不让客户端造空令牌 | `control_plane/api/sandboxes.py` | `secure=false` → **400 显式拒绝**（不是静默改成 true） |
| ② 加载/启动边界 | `envd_service/agent.py` | `_agent_finalize_sandbox` 入口检查 `accessToken`，缺失/空即拒（**在建树之前**，一个 dict 查找的成本） |
| 文档 | `docs/HANDOFF.md` | 删掉那段"allowPublicTraffic 跳过 token 校验"的过期描述 |

**为什么 ② 的两处都必要**：`secure=false` 被拒后，仍存在"Redis 里一条 `envd_access_token`
为空/缺失的持久化记录"这条路（`registry/manager.py:459` 的 `data.get(..., "")`），
它会让守卫再次恒真。agent 侧的前置拒绝把这条路径也变成一次显式失败。

**验证**（`tests/security/test_envd_token_fail_closed.py`，**11 passed**）：

- `secure=false` → 400，且错误文案精确匹配（不是"看起来对"）。
- HTTP 半边 / Connect 半边 × 令牌 ∈ {无、空串、错误} 全部 401 / `unauthenticated`。
- 两条半边各有一个"带正确令牌 ⇒ 被服务"的对照，断言成功形态（start 事件 + `exited: true`），
  而不是断言"没有报错"——后者在 handler 什么都没产出时也会通过。
- 空令牌经**注册表**注入（而不是经 `secure=false`），因为 API 已拒绝该字段，
  注册表是守卫真正要守的最后一道。

**连带修正的 6 个既有测试文件**：它们构造 worker create payload 时**没有** `accessToken`。
生产路径一直带（`control_plane/api/sandboxes.py:1974`），`tests/contract/*` 也一直带 ——
所以是这 6 个文件漏了字段，不是新检查过严。改动是在各自的共享 helper 里注入
`"accessToken": "tok"`，因此每个调用点都继承，diff 保持在每文件 1–2 处。

### 8.2 已落地：STATIC-5（c3-agent `--worker` 越过 pool 门禁）

| 文件 | 改动 |
|---|---|
| `c3_agent/priv/priv_common.{c,h}` | 新增 `priv_uid_in_pool()`：只判池成员关系，**不带 uid-0 规则** |
| `c3_agent/priv/maint.c` | `--worker` 分支：① 身份落在池内则拒；② 与 `--recursive` 同用则拒（usage） |
| `c3_agent/fileops.py` | 形状门禁镜像 ②（错误更清楚，且不必 fork） |

**为什么不直接用 `priv_validate_uid()`**：合法形态要求身份**在池外** —— k8s worker 是 65534、
compose/test 的 root worker 是 0，两者按构造都在池外。要求池成员身份会把规则搞反。
真正要紧的不变量更窄：**调用方只能把特权树交给自己能扮演的身份**，而它能扮演的
只有沙箱 uid 池。所以精确拒掉池内 uid 就关掉了跨租户那一半（属某个池内 uid 的树，
就是对应沙箱可读可写的树），同时不误伤任何合法部署。

**验证**（`tests/unit/test_priv_maint_worker_gate.py`，**13 passed**）：编译**生产翻译单元**
（`maint.c` + `priv_common.c`，非重实现），所有用例都在任何特权 syscall **之前**判定，
因此整套测试以非特权用户运行 —— 而"拒绝"正是纯决策，"拒绝需要 root 才能观察"会是更差的测试。

- 池内 uid（含边界 `10000` 与 `10999`）逐个精确断言完整 stderr 文本。
- 非池内身份（65534 / 0 / 1）断言**到达了 `lchown`**（用锚定 `re.fullmatch` 匹配
  `e2b-maint: refused: chown .* to \d+:\d+ failed: .+`），即门禁放行 —— 以非特权用户无法
  观察成功，所以断言的是"失败来自 syscall 而非门禁"这个可判定的事实。
- 对照组：`--uid` 的 pool 门禁未受影响（池外拒、池内放行）。
- 回归护栏：穿越与符号链接逃逸各一条。断言**精确复现 C 的两级 `snprintf(512)` 截断**
  （`PRIV_ERR_LEN`），而不是把正则放宽去容忍截断 —— 放宽会连"门禁打印了另一个路径"也一起容忍。

**一处刻意的检查顺序**：递归拒绝放在两个身份检查**之后**。"不知道 worker 是谁"是更根本的拒绝，
调换顺序会改变既有调用方看到的理由（`test_a_worker_chown_without_a_worker_uid_is_refused`
正是钉这个的）。

### 8.3 已落地：STATIC-2（7 个 `*at` syscall 只靠外层 profile 挡）

改动在 **`third_party/sandlock` 子模块**里（见 §8.5 提交注意事项）。

分成两类，理由是升级预演给出的实测结论：

| syscall | 处置 | 理由 |
|---|---|---|
| `fchmodat2` (452) | **中介**（不是 blocklist） | glibc 用它实现 `chmod`/`fchmodat` 的 `AT_SYMLINK_NOFOLLOW` 语义，而 seccomp 拒绝是 `EPERM`，glibc **不会**回退 —— blocklist 会直接搞坏沙箱里的 `chmod` |
| `setxattrat` `getxattrat` `listxattrat` `removexattrat` `file_getattr` `file_setattr` | **blocklist** | 没有工作负载需要，glibc 也还没调用它们 |

fork 侧 5 个文件：

- `sys/arch.rs`：新增 `SYS_FCHMODAT2`（走 `Sysno::fchmodat2`，不用 `libc::SYS_*`，因为该
  syscall 比某些受支持的 `libc` 版本新），并在两个 arch 的 `tests` 里钉住 **452**。
- `seccomp_plan.rs`：两处 chroot 路径集合各加 `arch::SYS_FCHMODAT2`。
- `chroot/dispatch.rs`：`fchmodat` 分支同时接住 `fchmodat2`，**共用同一个门禁**。
- `sys/structs.rs`：6 个名字进 `DEFAULT_BLOCKLIST_SYSCALLS`。
- `sys/path_surface.rs`：`fchmodat2` 移出 `UNMEDIATED_PATH_TAKING` 进 `MEDIATED_PATH_SYSCALLS`；
  6 个改成 `Disposition::Blocked`；**`Open` 集合钉成空**。

关于 `fchmodat2` 的 `flags` 参数**故意不当作 follow/no-follow 开关处理**：用
`read_and_resolve` 解析，意味着策略判决算在**已解析**的路径上、随后 `libc::chmod` 作用于
**同一个**已解析路径 —— 判定与动作指向同一个 inode。把它拆开（判定走 no-follow、
动作让 `chmod` 自己去 follow）正是把竞态变成策略旁路的写法；上面的 `fchownat` 之所以安全，
是因为它的 no-follow 解析配的是 `lchown`。可见后果是
`fchmodat2(..., AT_SYMLINK_NOFOLLOW)` 作用在符号链接上时这里会 chmod 目标，而内核返回
EOPNOTSUPP —— Linux 没有 `lchmod`，该标志的唯一效果就是那个拒绝，且目标已经过
`can_write` 检查，所以没有放宽任何东西。

**验证**（子模块自带测试，在 Linux 容器里跑，rust 1.90）：

```
cargo test -p sandlock-core --lib   →  915 passed, 2 failed
```

那 2 个失败（`procfs::tests::pid_ns_kill_host_pid_is_esrch`、
`seccomp::notif::tests::dup_fd_from_pid_handles_worker_thread_fd`）**在原始 HEAD 上同样失败**
—— 已用 `git archive HEAD` 的干净副本复现确认。它们需要新鲜 PID namespace 与跨线程
ptrace 权限，是容器环境的既有失败，与本次改动无关。定向的 63 个
（`path_surface` / `arch` / `seccomp_plan` / `chroot`）**全部通过**，其中包括 7 条不变量测试：
全量分类、`Open` 集合现为空、`Blocked` 项确实被拒、`mediated` 与台账一致。

> fork 的测试**在 macOS 上编不过**（该 crate 是 Linux 定向的，且 `build.rs` 需要可用的
> C 编译器编 restore stub；`rustc 1.86` 又低于某个依赖要求的 1.88）。本次用
> `docker run rust:1.90-bookworm` 挂载子模块来编译与测试。

### 8.4 全量回归

```
tests/unit      2453 passed, 3 failed, 12 skipped
tests/security  （离线可跑部分全绿；12 个 live-lane 文件需要真实沙箱机队，未运行）
```

那 3 个失败是**纯 macOS 平台问题**，与改动无关：
`test_real_root_gate` 试图 `dlopen("libc.so.6")`；
`test_xfs_quotactl_backend` 两条要跑 Linux 的 `quotactl`。
它们与仓库已有的 12 个 SKIP（`"chown requires root"`、`"Linux-only"`）同类。

### 8.5 提交注意：fork 在子模块里

`third_party/sandlock` 是 git submodule（`.gitmodules`，父仓记录为 mode `160000`，
当前 HEAD `e8c6730`）。§8.3 的 5 个文件改动**只存在于子模块工作区，未提交**。
落地需要：

1. 在子模块内提交（改动落在上游 fork 仓库，本仓只是 pin）
2. 父仓提交子模块指针前移

`cargo` 缓存与基线副本放在了 `tmp/audit3/`（gitignored），不入库。

### 8.6 未实施（仍是设计）

- **Phase 2**：`/internal/routes/{id}` 检查 `allowPublicTraffic`；gateway 边缘认证。
  独立于 Phase 1 的第二层，**Phase 1 万一被回退时它才挡得住** —— 但它会改变 gateway 的
  入站契约，需要与 SDK 联调，建议预发验证后再上。
- **Phase 3**：若产品确需"公开沙箱"，用 `traffic_access_token`（数据模型里已有、
  从未被赋值），且 `process.Process/Start` **永不**接受它。这是产品决策。
- **STATIC-3**（`CLONE_NEWNET` 漏出 `CLONE_NS_FLAGS`）：降级为 Low，外层 profile 的
  clone 掩码（`0x7E020000`，含 bit30）比内层更完整，且 `capget` 实测 `CAP_SYS_ADMIN`
  已被清除。两层之外还有内核兜底，故本轮**未改**。
- **STATIC-6**（c3-agent pod 卫生：两个容器都无 `seccompProfile`、`hostPID: true`、
  无 `readOnlyRootFilesystem`、无 SA-token opt-out、face A 无 `runAsGroup`）。
- **部署**：以上全部只改工作区。**尚未构建镜像、未上集群**，所以线上行为未变 ——
  复测要走 §6 的流程。
