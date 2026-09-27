# 债务报告：N45 —— 池的 worker env 缺 `E2B_PID_NS`（池里的沙箱与 worker 共 pid ns）

日期：2026-09-27 ｜ 工作目录：`/Users/polus/project/ai/sandlock-e2b`
开工 HEAD：`7a11122`（执行期间并行 lane 仍在改 `envd_service/*`、`gateway_common/keepalive.py`
等文件；本单只碰下面 §6 列出的文件，提交用 pathspec）
登记：`docs/open-issues.md` 的 **N45**（本单收口）；顺带只改 **N38 行尾那一句过期的话**

---

## 0. 结论（先给结论）

1. **`E2B_PID_NS: "true"` 已补进 5 个文件 8 处**：池的两处声明（`deploy/compose/docker-compose.autoscale.yml`
   的 `E2B_AS_WORKER_ENV`、`autoscaler/backends/local.py` 的 `self._env`），以及车队形态的其它
   compose 栈 —— `deploy/compose/docker-compose.prod.yml`（worker-1 锚点，worker-2/3 经
   `<<: *worker` 继承）、`docker-compose.multinode.yml`（三处 worker）、单机示例 `docker-compose.yml`
   （`envd`）、测试跑器 `docker-compose.test.yml`（`test-runner`）。
2. **语义已按纪律读代码核实（不是转述）**：`pid_ns` = 沙箱成为**自己 pid 命名空间的 pid 1**；
   唯一前提是**非特权 user namespace 可用**（与 per-sandbox uid / netns 同一个前提），失败
   **fail-closed**；它**不依赖** `E2B_ENABLE_NET_ISOLATION`/`E2B_FD_INJECT_CONNECT` 那条配对链
   （没有配对守卫，单开是合法形态）。详见 §1。
3. **键集合逐处对照**（`deploy/k8s/worker.yaml` 为参照，40 键）：每个键归入**唯一**命名类，
   7 个栈的 missing/extra 与白名单**精确相等**；"有意保留"的差异逐条点名（k8s-only 的磁盘执行键、
   N27 树根下沉、真根/checkpoint；compose 示例的轮换窗口/broker/模板；池的接线键；跑器形态）。
   详见 §2。
4. **同一审计发现并对齐的第二个同形缺口**：`local.py` 的字典缺 4 个 `E2B_IMAGE_CACHE_*`
   （缺 `E2B_IMAGE_CACHE_DIR` ⇒ worker 用**容器内相对默认** `tmp/sandboxes/_images`；缺
   `E2B_IMAGE_CACHE_MAX_BYTES` ⇒ 默认 `0`＝**不回收**）。已补齐为车队值。
5. **钉子**：新增 `tests/unit/test_worker_env_key_sets.py`（5 条）。RED：修前 6/7 栈红（唯一
   绿的是车队自己的 stack，它的白名单本来就精确）；GREEN：7/7 绿。**6 个变异各自红一次**。
6. **动态复验（本机 Docker，worker 镜像从本树构建）**：池按出厂默认 ⇒ 同一支 N39 探针
   `getpid=7 / kill1=ok / kill2=ok`（连续 2 次）；反证臂把该键单独改成 `false` ⇒
   `getpid=42`、`getpid=68`、`kill1=EPERM`（连续 2 次）；第三臂只用字典 spawn ⇒ 该键与 4 个
   cache 键全部 PRESENT。

---

## 1. 语义核实：`E2B_PID_NS` 到底做什么、前提是什么

**读的地方（链路）**：`envd_service/config.py:246`（`E2B_PID_NS` → `Settings.pid_ns`，默认
**false**）→ `envd_service/executors/factory.py:222`（`pid_ns=settings.pid_ns`）→
`envd_service/executors/sandlock.py:806/837`（`SandlockExecutor._pid_ns`）→ 建箱时
`:2422`/`:2669` 把 `pid_ns=True` 放进策略 → `envd_service/route_b.py:956` 的 wire 字段表（本来就有）。

**fork 侧实现**（子模块 `third_party/sandlock`，tip `c4d18c0`）：

- `crates/sandlock-core/src/sandbox.rs:2489+` 的**中间进程**：`pid_ns` 时子进程先
  `unshare(CLONE_NEWUSER)`（`pid_ns` 要求先有自己的 userns，才能非特权 `CLONE_NEWPID`），
  按与 `confine_child` 同一套三选一写 uid/gid map（特权 remap / route-B self-map /
  自身身份），**再** `unshare(CLONE_NEWPID)` + `fork()` 出最终的 leader；中间进程留作 leader 的
  父进程，只负责把 leader 的**宿主 pid** 经专用管道报给 supervisor、等它退出并转发退出码。
- `crates/sandlock-core/src/context.rs:705` 起：`if !pid_ns { ...整个 userns 块... }` ——
  pid_ns 时 `confine_child` **跳过**自己那次 `unshare(CLONE_NEWUSER)`（因为中间进程已经建好了）；
  `context.rs:391-397` 的字段注释同句："the user namespace (and any uid/gid mapping) was
  already created by the intermediate process before the final fork"。

**当前 tip 的用例（判据，不是转述）**：`crates/sandlock-core/tests/integration/test_pid_ns.rs`
（9 条）——`pid_ns_kill_host_pid_is_esrch`（屋内 kill 宿主 pid ⇒ **ESRCH**，kill 自己的 child/leader ⇒ 0）、
`pid_ns_procfs_view_is_renumbered`（`/proc` 只剩 `[1, 2]`）、`pid_ns_cross_sandbox_signal_isolation`、
`pid_ns_pause_resume_checkpoint`、`pid_ns_self_map_restores_guest_root`（route-B 非 root 相位：
客人 `id -u`=0、宿主侧落盘仍属槽位 uid —— 这条就是 N1 修好后的那半）、以及
`pid_ns_default_off_keeps_host_pid_view`（关着时 `kill(host, 0)` 必须是 0/EPERM、**永不 ESRCH**）。

**前提与失败模式**：

- **唯一前提**：内核/LSM 允许创建**非特权 user namespace**（部署形态里 worker 是 uid 65534、
  无 cap）。这与 per-sandbox uid（`E2B_PER_SANDBOX_UID`，默认开）和 per-sandbox netns 是
  **同一个前提** —— `deploy/k8s/worker.yaml:395-404` 的注释原文如此。
- **失败是 fail-closed**：`unshare(CLONE_NEWUSER)` 被拒时中间进程 `_exit(127)`
  （`sandbox.rs` 里两处 `sandbox child: unshare(...)` 报错分支）⇒ 建箱报错，**不会静默退回共享
  pid ns**。对比 `E2B_ENABLE_NET_ISOLATION`+`E2B_FD_INJECT_CONNECT` 那对：那一对由 `create_app`
  的**配对守卫**拒绝单开（`envd_service/config.py:471-479`），pid_ns 没有配对守卫，也不会把
  沙箱弄成离线（`docs/production-deployment-requirements.md` §2.4.10.3 的灰度记录同句）。
- **不需要额外 cap**：路上没有 `CAP_SYS_ADMIN` 依赖（`pid_ns` 走的是"先 userns 再 NEWPID"的
  非特权路线）；worker 侧 `cap_add` 仍只有 Track F 那四个 BND 项。netns 那条链
  （`E2B_ENABLE_NET_ISOLATION` / `E2B_FD_INJECT_CONNECT`）与它**正交**：本单只把 `pid_ns`
  统一切，netns 对是否声明仍按各栈原样（见 §2 的裁定表）。
- **route-B 的旧"阻断项"已不存在**：`pid_ns` 曾经让客人丢 root 身份（`id -u` = 槽位 uid），
  fork `5b16855`（2026-09-16）修的，当前 tip 有回归用例 `pid_ns_self_map_restores_guest_root`。
  `envd_service/config.py:239-245` 那段 docstring 仍写着"今天打开会丢掉 guest root"——
  **那句是过期的**，与 fork 用例/`docs/production-deployment-requirements.md` §2.4.10.1 相反；
  本单没有改它（不属于本单范围，且同一文件正被并行 lane 改动）。⚠️ 见 §7 残留 ④。

---

## 2. 键集合对照表（`文件:行号`）与逐键裁定

参照 = `deploy/k8s/worker.yaml` 的 worker 容器 env（**40 键**）。口径（写进钉子）：

1. 40 键**每个**必须归入**唯一**命名类（`KEY_CLASSES`），所以新加一个 k8s 键不会"默认被允许缺席"；
2. 每个栈的 `missing`（车队有、栈没有）与 `extra`（栈有、车队没有）必须**精确等于**它的白名单
   （`ALLOWED_MISSING` / `ALLOWED_EXTRA`），白名单的每一项又必须来自命名类；
3. `E2B_PID_NS` **不在任何栈的白名单里** ⇒ 它对每个栈都是必需键，且取值必须 = 车队值（`true`）。

命名类（`tests/unit/test_worker_env_key_sets.py` 里逐条带理由）：
`k8s_state_layout`（3）、`k8s_disk_enforcement`（13）、`k8s_real_root_and_checkpoint`（2）、
`rotation_window`（1）、`priv_helpers`（1）、`template_images`（1）、`worker_wiring`（4）、
`image_cache`（4）、`node_capacity`（4）、`base_image`（1）、`workspace_base`（1）、
`pid_namespace`（1）、`netns_pair`（2）、`egress_switch`（1）、`route_b_root`（1）。

> 行号：k8s 侧是 `deploy/k8s/worker.yaml` 的行号（简写 `k8s:N`）；栈侧是本文件的行号。
> 下表是**修完之后**的状态（红线量：修之前有 6 个栈各自缺 `E2B_PID_NS`，字典还缺 4 个 cache 键）。

### `deploy/stack/docker-compose.prod.yml`（worker-1）— 38 键

- 车队有、本处没有（18）：`E2B_DISK_APPEND_MIN_INTERVAL_S`（k8s:648）; `E2B_DISK_APPEND_TRIGGER_MB`（k8s:646）; `E2B_DISK_DIRTY_GRACE_S`（k8s:578）; `E2B_DISK_ENFORCE_DIRTY`（k8s:563）; `E2B_DISK_ENFORCE_INTERVAL_S`（k8s:544）; `E2B_DISK_EXEC_LIMIT`（k8s:608）; `E2B_DISK_MAX_ENTRIES`（k8s:629）; `E2B_DISK_OVERRUN_ACTION`（k8s:660）; `E2B_DISK_OVERRUN_DENY_S`（k8s:662）; `E2B_DISK_RECONCILE_INTERVAL_S`（k8s:580）; `E2B_DISK_TIGHTEN_INTERVAL_S`（k8s:617）; `E2B_DISK_TIGHTEN_STEP_MB`（k8s:615）; `E2B_IMAGE_OCI_DIR`（k8s:362）; `E2B_PAUSE_CHECKPOINT`（k8s:483）; `E2B_PLATFORM_DISK_MB`（k8s:485）; `E2B_REAL_ROOT`（k8s:457）; `E2B_SHARED_VOLUME_ROOT`（k8s:340）; `E2B_STATE_BASE`（k8s:323）
- 本处有、车队没有（16）：`E2B_DEFAULT_CPU_PERCENT`（:267）; `E2B_DEFAULT_DISK_MB`（:268）; `E2B_DEFAULT_MAX_PROCESSES`（:269）; `E2B_DEFAULT_MEMORY_MB`（:266）; `E2B_ENABLE_NETNS`（:198）; `E2B_IMAGE_REGISTRY`（:247）; `E2B_IMAGE_REGISTRY_PASSWORD`（:249）; `E2B_IMAGE_REGISTRY_USERNAME`（:248）; `E2B_NETWORK_DENY_CIDRS`（:197）; `E2B_PER_SANDBOX_UID`（:230）; `E2B_QUOTA_AGENT_TIMEOUT_S`（:281）; `E2B_QUOTA_AGENT_TOKEN`（:280）; `E2B_QUOTA_AGENT_URL`（:279）; `E2B_QUOTA_VIA_AGENT`（:278）; `E2B_UID_POOL_SIZE`（:232）; `E2B_UID_POOL_START`（:231）

裁定：18 个缺席**是有意的形态差异**（k8s 的磁盘执行/记账开关、N27 树根下沉、N35/N14 真根与
checkpoint —— 上线记录见 `docs/deploy-clusters.md` §7/§9）；16 个"多出来的"是车队 compose 自己的
形态（quota-agent、registry 拉取凭据、deny CIDR 默认值、per-sandbox uid 池、示例的 per-sandbox
默认值、legacy `E2B_ENABLE_NETNS`）。

### `deploy/compose/docker-compose.autoscale.yml`（`E2B_AS_WORKER_ENV`）— 15 键

- 车队有、本处没有（26）：`E2B_CONTROL_PLANE_URL`（k8s:290）; 13 个 `E2B_DISK_*`+`E2B_PLATFORM_DISK_MB`（k8s:544-662）; `E2B_IMAGE_OCI_DIR`（k8s:362）; `E2B_INTERNAL_API_KEY`（k8s:292）; `E2B_INTERNAL_API_KEYS`（k8s:304）; `E2B_NODE_ADDRESS`（k8s:288）; `E2B_NODE_ID`（k8s:268）; `E2B_PAUSE_CHECKPOINT`（k8s:483）; `E2B_PRIV_HELPERS`（k8s:390）; `E2B_REAL_ROOT`（k8s:457）; `E2B_SHARED_VOLUME_ROOT`（k8s:340）; `E2B_STATE_BASE`（k8s:323）; `E2B_TEMPLATE_IMAGES`（k8s:511）; `E2B_WORKSPACE_BASE`（k8s:310）
- 本处有、车队没有（1）：`E2B_EXECUTOR`（:166）

裁定：接线四键（`E2B_NODE_ID`/`E2B_NODE_ADDRESS`/`E2B_CONTROL_PLANE_URL`/`E2B_INTERNAL_API_KEY`）与
`E2B_WORKSPACE_BASE` 是 autoscaler 自己的 `-e` 参数（`local.py:145-156`），不该进这份 JSON；
`E2B_EXECUTOR` 是池自己的执行器选择（N38 的回退杆）；其余与上一行的"k8s-only 类"同因。

### `autoscaler/backends/local.py`（`self._env`）— 13 键

- 车队有、本处没有（27）：上一行的 26 个 + `E2B_BASE_IMAGE`（k8s:507）
- 本处有、车队没有（0）

裁定：字典是**手搓后端**的兜底声明，`E2B_BASE_IMAGE` 走构造参数（`local.py:35/157-158`，
`autoscaler/__main__.py:31` 从 JSON 里取），因此它缺 `E2B_BASE_IMAGE` 是有意的；其余同因。
**本单在此处做的两个对齐**：`E2B_PID_NS`（N45 本体）与 4 个 `E2B_IMAGE_CACHE_*`
（同一审计发现的同形缺口，见 §0 第 4 条）。

### `deploy/compose/docker-compose.prod.yml`（worker-1）— 28 键 / `docker-compose.multinode.yml`（worker-1）— 19 键

- 车队有、本处没有（各 21）：`E2B_DISK_*`（12）+`E2B_PLATFORM_DISK_MB`、`E2B_IMAGE_OCI_DIR`、
  `E2B_STATE_BASE`、`E2B_SHARED_VOLUME_ROOT`、`E2B_REAL_ROOT`、`E2B_PAUSE_CHECKPOINT`、
  `E2B_INTERNAL_API_KEYS`、`E2B_PRIV_HELPERS`、`E2B_TEMPLATE_IMAGES`
- 本处有、车队没有：prod 9（quota-agent 四键、registry 三键、deny CIDR、legacy netns）/ multinode 0

裁定：与"compose 示例"同因。**multinode 的 `extra = 0`**，所以它的 19 键是车队 40 键的真子集
（除上面 21 个 k8s-only/示例键之外）；三处 worker 的键集合必须一致（有专门的钉子）。

### `deploy/compose/docker-compose.yml`（`envd`）— 6 键

- 车队有、本处没有（34）：上面 21 个 + `E2B_BASE_IMAGE`、`E2B_CONTROL_PLANE_URL`、
  `E2B_INTERNAL_API_KEY`、6 个 `E2B_NODE_*`、`E2B_ENABLE_NETWORK`、`E2B_ENABLE_NET_ISOLATION`、
  `E2B_FD_INJECT_CONNECT`、`E2B_ROUTE_B_TMP_ROOT`（k8s 行号见 §2 生成日志）
- 本处有、车队没有（0）

裁定：**单机本地构建示例**，本来就只声明 cache 与 workspace；本单只把 `E2B_PID_NS`（= 车队形状里
唯一"单开也合法、且不依赖配对"的形状键）补上，其余形状开关维持原样（N36/N42 也没有动过它）。
代价与退回杆见 §7 残留 ①。

### `deploy/compose/docker-compose.test.yml`（`test-runner`）— 7 键

- 车队有、本处没有（37）：上面 34 个 + 4 个 `E2B_IMAGE_CACHE_*` - 它自己有的 `E2B_BASE_IMAGE`/`E2B_WORKSPACE_BASE`
- 本处有、车队没有（4）：`E2B_API_KEY`（:32）、`E2B_API_URL`（:33）、`E2B_REQUIRE_SECCOMP_FILTER`（:23）、`E2B_SANDBOX_URL`（:34）

裁定：**测试跑器形态** —— 它的 netns 对与镜像形态由 runner 镜像
（`deploy/docker/Dockerfile.test-runner:93-96` 的 `E2B_BASE_IMAGE`/`E2B_TEST_NET_ISOLATION`）与套件
fixture（`tests/contract/test_mcp_netns.py` 自起 worker）选，四个 client 键是它自己的；本单只补
`E2B_PID_NS`，让**本地门禁跑的就是车队的 pid 形状**（此前只有显式传 `E2B_PID_NS=1` 才覆盖 fork 的
pid-ns 路径）。§2 生成脚本：`tmp/n45/key-table.py` → `tmp/n45/key-table.md`。

---

## 3. 钉子用例：RED → GREEN

新增 `tests/unit/test_worker_env_key_sets.py`（风格照 `test_compose_base_image_shape.py` /
`test_worker_manifest_permissions.py`：文本解析、无 PyYAML、断言精确相等）。5 条：

1. `test_every_k8s_worker_key_is_classified` —— 40 键归入唯一命名类（新键必须被点名）；
2. `test_every_worker_stack_declares_the_fleets_keys_except_a_named_whitelist` —— 逐栈
   `missing == 白名单` 且 `extra == 白名单`（两向精确；白名单项只能是 k8s 真有的键）；
3. `test_every_worker_stack_turns_on_the_per_sandbox_pid_namespace` —— `E2B_PID_NS` 存在、取值
   （含 `${E2B_PID_NS:-true}` 的默认值）**等于车队值**；
4. `test_the_pools_two_declarations_agree` —— 字典 ⊆ compose JSON 且字面值逐个相等；
5. `test_the_three_multinode_workers_declare_the_same_env_keys` —— 三处 worker 键集相同。

**RED（补键之前，真跑）**：

* pytest 层（`tmp/n45/red-1.log`）：`.FFF.` —— 3 条红，第一条就是
  `AssertionError: ('deploy/compose/docker-compose.autoscale.yml', 'missing != whitelist', ['E2B_PID_NS'], [])`；
  集合相等断言在第一个栈就失败，所以 pytest 只报第一个栈。
* 全栈视图（`tmp/n45/red-prefix-whitelist-diff.log`：把**冻结的白名单**拿到开工 commit
  （scratch worktree `tmp/n45/prefix` = `7a11122`）上算）：**6/7 栈 RED** —— 池 JSON、字典、
  compose/prod、multinode、单机示例、跑器各缺 `E2B_PID_NS`（字典还多缺 4 个 cache 键）；
  唯一 GREEN 的是车队自己的 `deploy/stack/docker-compose.prod.yml`（它的白名单本来就精确）。

**GREEN（补键之后）**：`tmp/n45/green-1.log` = `5 passed`；同一支冻结白名单脚本对修后树
（`tmp/n45/green-whitelist-diff.log`）= `0/7 栈 RED`；相邻钉子一并复跑
（`test_worker_env_key_sets + test_autoscaler_local_backend_shape + test_compose_base_image_shape +
test_worker_manifest_permissions + test_net_isolation_config + test_seccomp_selfcheck`）= `81 passed`。

**基线噪声**：`tests/unit` 全量 = `1542 passed / 14 failed / 11 skipped`，那 14 条在**未改动的
scratch worktree（HEAD）上逐条同名复现**（`tmp/n45/head-baseline-same-14-failures.log`：darwin 宿主
`os.chown`/`pivot_root`/XFS 探针类），与本单无关。

---

## 4. 变异（6 个，各自红一次；日志在 `tmp/n45/`）

| # | 变异 | 期望（实际） | 日志 |
|---|---|---|---|
| M1 | 删掉**池的** `E2B_PID_NS`（compose JSON） | 3 红：`missing != whitelist ['E2B_PID_NS']` + 取值条 + 池两处一致条 | `mut1-pool-pidns-dropped.log` |
| M2 | 把 **k8s 侧**键名改一个字（`E2B_PID_NS` → `E2B_PID_NX`） | 3 红：分类条 `['E2B_PID_NS','E2B_PID_NX']` + 逐栈白名单 + 取值条 | `mut2-k8s-key-typo.log` |
| M3 | 往白名单里塞一个**不该有**的键（compose/prod 的 `ALLOWED_MISSING` 加 `E2B_PID_NS`） | 1 红：`missing != whitelist [], ['E2B_PID_NS']`（"白名单替它已经有的键背书"也被拒） | `mut3-bogus-whitelist-entry.log` |
| M4 | 删掉 **multinode 三处**的 `E2B_PID_NS` | 2 红：逐栈白名单 + 取值条 | `mut4-multinode-pidns-dropped.log` |
| M5 | 只删 **multinode worker-2** 的那一行 | 1 红：`test_the_three_multinode_workers_declare_the_same_env_keys`（**逐栈条仍绿** ⇒ 这就是那条专测存在的理由） | `mut5-multinode-worker2-only.log` |
| M6 | 把**池的**取值翻成 `"false"` | 2 红：取值条 `('...autoscale.yml', 'false')`（**键集合条仍绿** ⇒ 取值钉不是空转） | `mut6-pool-value-false.log` |

6/6 各自红一次，且红的原因与设计意图一致（不是"随便哪里炸了"）。跑完每个变异都按原样还原，
每次还原后 `5 passed`。

---

## 5. 动态复验（本机 Docker）

守则：独立 project（`n45` / `n45b`）、独立端口（`3920` / `3921`）、tmp-only override 把卷换成
`n45-*`（**没有** `down -v`，共享 `sandbox-shared` 一次没动）；worker 镜像从本树构建
（`e2b-local/e2b-sandlock-worker:n45`，与 n39 那次 image id 逐字节相同 ⇒ 本单没碰镜像内容）。

### 5.1 主臂：池按出厂默认（`n45`，端口 3920）

* worker 容器：`state=running / exitcode=0`、`user=65534:65534`、`capadd=null`、
  `privileged=false`、`security_opt=["seccomp=unconfined"]`、`sysctls=null`、
  `mounts=[n45-pool-shared -> /var/lib/e2b-sandboxes]`（`tmp/n45/n45-pool-worker-facts.log`）。
* worker env 实测含 **`E2B_PID_NS=true`**（连同 `E2B_EXECUTOR=auto`、netns 对、`E2B_ENABLE_NETWORK`、
  `E2B_ROUTE_B_TMP_ROOT`、4 个 cache 键）；worker 日志 `Uvicorn running on http://0.0.0.0:49983`
  + `registered node e2b-worker-…` ⇒ 起得来、注册得上。
* 同一支 N39 探针（`tmp/n39/n39-pool-pidns-probe2.py`）**连续 2 次**：

```
RAW-STDOUT-START
getpid=7
kill1=ok
kill2=ok
kill999999=ESRCH
nproc=0
RAW-STDOUT-END
EXIT-CODE=0
```

（`tmp/n45/n45-pool-pidns-run1.log` / `run2.log`；对照：N39 出厂默认那次是 `getpid=81`、
`kill1=EPERM` ⇒ 本单把那条 RED 变成 GREEN。）

### 5.2 反证臂：只把该键改成 `false`（`n45b`，端口 3921）

worker env 实测 `E2B_PID_NS=false`（其余同主臂），同一支探针**连续 2 次**：

```
getpid=42   kill1=EPERM   kill2=ESRCH        # run 1
getpid=68   kill1=EPERM   kill2=ESRCH        # run 2
```

（`tmp/n45/n45b-pool-pidns-off-run{1,2}.log`）⇒ 主臂的绿**不是恒真**，就是这一个变量决定的。
（踩坑记录：`DockerPoolBackend.current()` 数的是**整个 daemon** 上 `e2b.role=worker` 的容器，
所以两次起池必须先把上一臂 retire，否则第二个 autoscaler 认为"已经够了"不 spawn ——
`tmp/n45/n45-teardown-for-control.log`。）

### 5.3 第三臂：只用 `DockerPoolBackend` 自带字典 spawn（不传 `E2B_AS_WORKER_ENV`）

`tmp/n45/backend-arm.py`（tmp-only）直接构造后端、`scale_to(current+1)`，读新容器 env：

```
E2B_PID_NS=true: PRESENT
E2B_IMAGE_CACHE_DIR=/var/lib/e2b-sandboxes/_images: PRESENT
E2B_IMAGE_CACHE_MAX_BYTES=4294967296: PRESENT
E2B_IMAGE_CACHE_EVICT_MIN_AGE_S=300: PRESENT
E2B_IMAGE_CACHE_OWNER_UID=65534: PRESENT
```

（`tmp/n45/n45-backend-dict-arm.log`）⇒ 池的第二处声明也自足（N38 那句"手搓的
`DockerPoolBackend()` 也得自足"现在对这两组键成立）。

**收尾**：`tmp/n45/n45-teardown.log` / `n45-teardown2.log` —— 容器全清、本单自建的
`n45*` 卷删除，`sandbox-shared`/`compose_sandbox-shared`/`stack_sandbox-shared` 一个没动。

---

## 6. 文件清单与提交

**提交**：`fix(deploy): every worker stack turns on E2B_PID_NS (N45)`（本单唯一提交；hash 取
`git log -1`，本报告随该提交一起纳入，所以不写死自身 hash）
（9 文件，`git diff --cached --name-only` 逐条核对过：只有下面这 9 个；并行 lane 的
`envd_service/*`、`gateway_common/keepalive.py`、`tests/unit/test_gateway.py` 等仍在工作区里
**未纳入**。`.superpowers/` 被 gitignore，报告按仓库既有惯例用 `git add -f` 纳入。）

| 文件 | 改动 |
|---|---|
| `deploy/compose/docker-compose.autoscale.yml` | `E2B_AS_WORKER_ENV` 加 `"E2B_PID_NS": "true"` + 注释（N45 的语义/前提） |
| `autoscaler/backends/local.py` | `self._env` 加 `"E2B_PID_NS": "true"` 与 4 个 `E2B_IMAGE_CACHE_*`（+理由注释） |
| `deploy/compose/docker-compose.prod.yml` | worker-1 锚点加 `E2B_PID_NS: ${E2B_PID_NS:-true}`（worker-2/3 继承） |
| `deploy/compose/docker-compose.multinode.yml` | 三处 worker 各加 `E2B_PID_NS: "true"` |
| `deploy/compose/docker-compose.yml` | `envd` 加 `E2B_PID_NS: ${E2B_PID_NS:-true}` |
| `deploy/compose/docker-compose.test.yml` | `test-runner` 加 `E2B_PID_NS: ${E2B_PID_NS:-true}` |
| `tests/unit/test_worker_env_key_sets.py` | 新增钉子（5 条） |
| `docs/open-issues.md` | N45 行改成收口态；N38 行尾那句过期的话就地校准（只改那一处） |
| `.superpowers/sdd/n45-pid-ns-report.md` | 本报告 |

未提交（`tmp/`，均为本单证据）：`tmp/n45/` 下 —— `env-key-audit.{py,log}`、`key-table.{py,md}`、
`prefix-diff.py`、`red-1.log`、`red-prefix-whitelist-diff.log`、`green-1.log`、
`green-whitelist-diff.log`、`mut1..mut6-*.log`、`n45-pool-up.log`、`n45-worker-build.log`、
`n45-pool-worker-facts.log`、`n45-pool-pidns-run{1,2}.log`、`n45b-pool-up.log`、
`n45b-worker-facts.log`、`n45b-pool-pidns-off-run{1,2}.log`、`n45-backend-dict-arm.log`、
`backend-arm.py`、`worker-facts.py`、`pool-override.yml`、`pool-pidns-off-override.yml`、
`n45-teardown*.log`、`head-baseline-same-14-failures.log`、`unit-all.log`。

---

## 7. 残留

1. **单机示例与测试跑器多了一条前提**：它们现在也要求宿主允许非特权 user namespace（与车队
   相同的前提）。不满足时建箱 **fail-closed**（报错，不静默降级），退回杆是 `E2B_PID_NS=false`
   （compose 里都是 `${E2B_PID_NS:-true}`，`docker compose run -e E2B_PID_NS=false …` 也覆盖）。
   本机（OrbStack）与两条生产形态都满足；未在"禁止非特权 userns"的宿主上实测过。
2. **同一 worker 上多沙箱互探仍未测**（默认 `E2B_NODE_PROCESSES=256`／一 worker 一沙箱）；
   要测需把容量抬到能容两个沙箱。fork 侧有 `pid_ns_cross_sandbox_signal_isolation`（跨沙箱
   `kill(pid,0)` ⇒ ESRCH），所以机制上有判据，但**部署形态**没量过。
3. **k8s-only 类差异没有统一**（13 个磁盘执行键、N27 树根三键、`E2B_REAL_ROOT`+
   `E2B_PAUSE_CHECKPOINT`）：它们是另一套形态（k8s 上线记录在 `docs/deploy-clusters.md` §7/§9），
   不在"池＝车队"的口径里。本单只把它们**点名 + 钉住**（谁要把它们搬进 compose，钉子会要求
   同步改白名单，即显式裁定）。
4. **`envd_service/config.py:239-245` 的 `pid_ns` docstring 已过期**（写着"今天打开会丢 guest
   root"）：route-B 自映射在 fork `5b16855` 已修，当前 tip 的
   `test_pid_ns::pid_ns_self_map_restores_guest_root` 是反例。本单没改它 —— 同一文件正被并行
   lane 改（N37），且不属于 N45 的文件范围。建议单开一条：把那段 docstring 改成"route-B
   形状由 fork 的自映射分支覆盖（`sandbox.rs` 中间进程）"。
5. **测试跑器的默认形状变了**：本地门禁（`pytest tests/sdk/python` 那条 compose 路径）现在默认
   跑车队 pid 形状；共享 pid 形状仍有入口（`E2B_PID_NS=false`）。依据是
   `docs/production-deployment-requirements.md` §2.4.10.3 的实测（`E2B_PID_NS=1` 全量门禁
   `1470 passed / 0 failed`），但**本单没有重跑整条容器内门禁**（只在宿主跑了 `tests/unit`）。
