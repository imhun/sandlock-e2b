# C1 特权外置（Privilege Externalization）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 k8s worker 容器不再以 root 运行、pod 内不再有 root 容器，把 `chown`/`rm`/`walk` 三个特权动作外置到每节点一个最小 root broker（unix socket），而 `e2b-slot-spawn` 仍留在 worker pod（它必须在 worker 的命名空间里起进程）。

**Architecture:** 复用现有 `e2b-maint`（`deploy/priv/maint.c`）的 verb 实现与路径白名单，新增 `serve` 模式：root DaemonSet 监听 unix socket，校验对端凭据（`SO_PEERCRED`）后 **fork/exec 自己**、原样转发 argv 与 stdio。worker 侧 `priv_helpers` 新增 `socket` transport，调用点（`chown`/`chown_worker`/`remove`/`walk`）一行不改，wire 协议 = `{"args":[...]}` → `{"exit":N,"stdout":...,"stderr":...}`。

**Tech Stack:** C99（`deploy/priv/`，Linux-only，`cc -O2 -Wall -Wextra`）、Python 3.14（`envd_service/priv_helpers.py`）、pytest、k8s（k0s overlay + kustomize）。

## 执行状态（2026-09-27 收尾）

| 任务 | 状态 |
|---|---|
| Task 1（C 侧 `serve`/`ping` + 白名单四根） | ✅ 已合并（wave 1，`526f581`） |
| Task 2（Python socket transport + hello 自检） | ✅ 已合并（wave 1） |
| Task 3（secret 属主三分支） | ✅ 已合并（wave 1） |
| Task 4（root broker DaemonSet，**基线**） | ✅ 已合并（wave 2，`8163f9b` 之前） |
| Task 5（worker 去 root + `wait-for-broker`） | ✅ 已合并（wave 2） |
| Task 6（平台态属主迁移工具 + Job） | ✅ 已合并（wave 2） |
| Task 7（文档与 pin 收口） | ✅ 已合并（wave 2） |
| Task 9（终审 Minor 收口） | ✅ 已合并（wave 2 + `2ef457e`） |
| **Task 8（真机 rollout 与验收）** | ✅ **已执行（2026-09-27，用户授权）**：版本 `0.1.0-698-g55e5e79-20260927-195247`；worker 缩 0 → 迁移（8 条目标、`chowned=8`、files/dirs 计数前后一致）→ `apply.sh`（broker 先滚、worker 后滚）→ 验收全绿。完整记录与证据见 `docs/deploy-clusters.md` §7.1。 |

**Task 8 真机预检发现（2026-09-27，只读）**：**worker 的快照 payload 根** `<workspaces>/_snapshots`（`envd_service/agent.py` 硬编码 `<workspace_base>/_snapshots`；控制面的记录根是另一条 `<export>/_snapshots`）坐在树根下、属主 `root:0755` —— C1 之后 65534 的 worker 写不进去（"上线后第一次 create snapshot 才炸"的静默类型）。已由本次修复覆盖：`deploy/k8s/priv-broker.yaml` 的 `workspace-root-init` 把它交给 65534（`mkdir -p` + `chown 65534:65534` + 校验），`deploy/scripts/migrate-state-owner.sh` 的树根白名单放行它（`workspaces/_migrate`、`workspaces/_snapshots` 两条）。见 `docs/k8s-deployment.md` §24。

**已知延后（非阻断，均已记账）**
1. ~~`drop: [ALL]`~~ ✅ **已做并在真机验证（2026-09-27）**：broker 的 `CapEff=0xcb` = 恰好 `CHOWN`+`DAC_OVERRIDE`+`FOWNER`+`SETUID`+`SETGID`（不再是运行时默认的满 root 集）；`chown`/`rm`/`walk` 三个 verb 都在这个集合下现场验过（新建沙箱树 `770 10000:65534`、kill 后树消失、worker 记账的 `walk` 计数在走）。其中 `SETUID`/`SETGID` 是给**探针**保留的：探针必须以对端身份连 socket 才过 peer 门，用 `setpriv` 降权需要这两条。
2. ~~Python 侧单行读取上限 ≈3 GiB~~ ✅ **已收（2026-09-27）**：`walk` 有自己的 `BROKER_MAX_WALK_RESPONSE_BYTES = 512 MiB`（推导写实：单树 ≤ `E2B_DISK_MAX_ENTRIES`=500000 条目 × ~80 B ≈ 40 MB 未转义 × 6 转义 ≈ 229 MiB 线路 ⇒ 512 MiB ≈ 2.2×，且 < 容器 2Gi；80 B 明确标为估计而非上界）；**`runtime/platform_disk.measure_platform_disk_bytes` 从"整棵 `_runtime` 一次 walk"改成逐子项求和**（否则单树前提不成立、合法答案会被拒），数值与旧口径相等并有等式用例；`raw += chunk` 改 `chunks+join`；两条 pin（`-mindepth 1 -maxdepth 2 -type d` 与 manifest 侧 `limits.memory == 2Gi` / `E2B_DISK_MAX_ENTRIES == 500000`）。"测不到 → 0" 现在会打一条 WARNING（不再静默 fail-open）。
3. ~~`image-cache-init` 对 `secrets/` 的 chown 是静默 best-effort~~ ✅ **已收**：两条 `|| true` 改成可见的 `|| echo "image-cache-init: chown refused …"`（仍 non-fatal），注释写明 `-mindepth 1 -maxdepth 2` 就是契约，pin 同步。
4. ~~`deploy/scripts/migrate-state-base.sh` 的 bash 3.2 隐患~~ ✅ **已收**：4 处 `$VAR（` 改成 `${VAR}`，`usage()` 的 sed 上界改准（`2,39p`），并加了静态扫描用例（该脚本里 `$VAR` 紧邻非 ASCII 必须 0 次）。
5. 环境侧既有噪声：CP 有两个副本而构建状态是进程内的 ⇒ `deployment_smoke` 的模板构建轮询偶发 404（重跑即绿）。与"控制面只能 1 副本"同源，不属 C1。
6. **上线时真机才暴露、已修的缺陷**：DaemonSet 第一版的 liveness/readiness 以容器 root 跑 `e2b-maint ping` → 被 peer 门拒（`peer uid 0 does not match`），daemon 正常但 pod 停在 `Running 0/1`、反复重启、rollout 超时。修法：探针用 `setpriv --reuid/--regid 65534 --clear-groups` 降到对端身份；pin 见 `tests/unit/test_worker_manifest_permissions.py::test_the_broker_probes_connect_as_the_peer_identity`。

## Global Constraints

- 沟通中文；最小改动，保持既有风格；临时文件一律放仓库 `tmp/`（不是 `/tmp`、不是 `$TMPDIR`）。
- 测试：先写失败测试再实现（TDD）；断言必须**精确匹配**，禁用 `toContain`/`includes`/部分匹配；禁止新增 skip/xfail；日志驱动排查，不靠猜。
- 失败必须 fail closed：半安装/握手失败/白名单漂移 → worker 拒绝启动并点名，绝不静默降级。
- **协议冻结（跨任务接口，不得改名）**：
  - socket 路径默认 `/run/e2b-broker/broker.sock`，环境变量 `E2B_PRIV_HELPER_SOCKET`。
  - transport 开关 `E2B_PRIV_HELPER_TRANSPORT=auto|exec|socket`，默认 `auto`（socket 存在则用 socket，否则 exec = 今天行为）。
  - 请求（一行 JSON + `\n`）：`{"v":1,"args":["chown","--uid","10001","--gid","65534","--recursive","--path","/..."] ,"timeout_s":300}`
    - **`args` 不含 argv[0]**；daemon 只 exec 自己（`/var/lib/e2b-priv/e2b-maint`），不是通用 launcher。
    - 探活/握手：`{"v":1,"hello":true}` → `{"v":1,"ok":true,"peer_uid":65534,"peer_gid":65534,"uid_pool":[10000,1000],"roots":["..."]}`
      （`peer_uid`/`peer_gid` = daemon 会当作 worker 的那个身份；Python 侧启动自检必须断言它们 == `os.geteuid()/os.getegid()`，见 Global Constraints 最后一条。）
  - 响应：`{"v":1,"ok":true,"exit":0,"stdout":"...","stderr":"..."}`；请求从未执行 → `{"v":1,"ok":false,"error":"..."}`。
  - verb 集合只允许 `chown` / `rm` / `walk`（第三个在 daemon 侧再校验一次，纵深防御）。
  - 默认超时：`chown`/`rm` 300s、`walk` 120s；上限 3600s；请求体上限 64 KiB；输出上限 256 MiB。
  - 对端凭据：uid 必须 == `E2B_BROKER_PEER_UID`（默认 65534），gid 必须 == `E2B_BROKER_PEER_GID`（默认 65534）。
- **白名单新增第 4 根 `E2B_IMAGE_CACHE_DIR`，且*仅在该变量显式配置时*纳入**（2026-09-27 裁定，覆盖本节早先"默认 `/var/lib/e2b-images`"的说法）：沙箱 secret 文件今天落在 `<E2B_IMAGE_CACHE_DIR>/secrets/<sandbox_id>/`（`envd_service/executors/factory.py:236`），非 root worker 要把它交给池 uid 就必须在白名单内。**未设置该变量时 Python 侧的默认是 cwd 相对的 `tmp/sandboxes/_images`（`envd_service/config.py:25-39`），把它放进特权 daemon 的白名单既是错的也危险**，所以未配置就两侧都不加、`Sandbox.create()` 到 secret 那步 fail closed 点名。C 侧与 Python 侧的这条规则必须逐字相同（同样的"配置才加 + 去重 + 顺序"），否则 `hello` 握手会因 roots 不一致而拒服（这是刻意的）。真实部署（k8s、compose）都显式设了该变量。
- 既有三种形态必须继续绿：root worker、本地盘非 root（`exec` transport）、legacy 共享 uid。`exec` 路径行为逐字不变。
- 本次（wave 1）只执行 Task 1–3；Task 4–8 为 wave 2，需另开执行轮次。
- **对端身份必须显式传给 broker 子进程（2026-09-27 终审裁定，Critical）**：exec 形态下"进程身份 = worker"，`serve` 把同一份 argv 交给 root 之后这个前提就不成立。因此 daemon 在通过 `SO_PEERCRED` 之后，必须把**已鉴权的对端 uid/gid** 通过环境变量（`E2B_BROKER_WORKER_UID` / `E2B_BROKER_WORKER_GID`，由 daemon 无条件覆盖、请求无法伪造）交给它 exec 出来的子进程；broker 里所有"worker 自己的身份"（`chown --worker` 的目标、`priv_gid_allowed` 的 own gid）都必须取这个值，直接 exec 时该变量不存在 → 仍用 `getuid()/getgid()`。`hello` 必须同时回 `peer_uid` 与 `peer_gid`，Python 侧启动自检断言它们 == `os.geteuid()/os.getegid()`，不一致即拒服并点名（把"两侧单测各自绿、合起来不成立"变成启动期失败）。**wave 2 的 DaemonSet 必须把 `E2B_BROKER_PEER_UID/GID` 设成 worker pod 的 uid/gid，且与 socket 目录/文件的组一致。**

## File Structure

| 文件 | 责任 |
|---|---|
| `deploy/priv/maint.c` | 新增 `serve` / `ping` 两个 verb；verb 实现保持不变 |
| `deploy/priv/priv_common.c` / `.h` | 白名单加第 4 根；新增 `priv_peer_allowed()`、`priv_roots_json()`（供 hello 回包） |
| `envd_service/priv_helpers.py` | transport 抽象（`exec`/`socket`）、`hello` 自检、per-verb 超时、第 4 根 |
| `envd_service/executors/sandlock.py` | secret 文件属主：非 root worker 走 broker（现存缺陷） |
| `tests/unit/test_priv_broker_protocol.py` | 协议/凭据/漂移/超时单测（Python 假 daemon，跨平台可跑） |
| `tests/contract/test_broker_socket_c.py` | 真 C broker 的 socket 往返（Linux 容器内跑） |
| `deploy/k8s/priv-broker.yaml` | （wave 2）root DaemonSet + socket 目录 + 接管两个属主 init。**放基线、不放 overlay**（2026-09-27 裁定，见 Task 4 的改动说明）：它与存储类型无关，而 socket 开关在共享基线里，放 overlay 会让"不经 overlay 的非 root 部署"起不来 |
| `deploy/k8s-k0s/state-owner-migrate.yaml` | （wave 2）平台态属主一次性迁移 Job |

---

### Task 1: C 侧 `e2b-maint serve` / `ping` + 白名单第 4 根

**Files:**
- Modify: `deploy/priv/maint.c`（新增 `serve`、`ping`；usage 文本补两行）
- Modify: `deploy/priv/priv_common.h` / `deploy/priv/priv_common.c`（`priv_peer_allowed`、`priv_roots_json`、根列表加 `E2B_IMAGE_CACHE_DIR`）
- Test: `tests/contract/test_broker_socket_c.py`（新建）

**Interfaces:**
- Produces（Task 2/3 依赖，逐字）：socket 协议见 Global Constraints；`e2b-maint serve --socket P` / `e2b-maint ping --socket P`。
- Consumes：既有 `priv_resolve_allowed_path` / `priv_validate_uid` / `priv_gid_allowed`。

- [ ] **Step 1: 写失败测试** `tests/contract/test_broker_socket_c.py`：
  - `test_serve_round_trips_a_chown`：起 `e2b-maint serve --socket <tmp>`，连上去发 `{"v":1,"args":["chown","--uid","<池内 uid>","--path","<tmp 下的树>"]}`，断言 `ok is True`、`exit == 0`、且 `stat` 的属主 == 该 uid（测试进程以 root 跑，uid 用 `os.geteuid()`… 用池段内一个确定值如 21000）。
  - `test_serve_refuses_a_path_outside_the_roots`：`--path /etc/hosts` → `exit == 77`、`stderr` 含 `outside the privileged helper roots`。
  - `test_serve_rejects_a_non_pool_uid`：`--uid 0` → non-zero 且点名池段。
  - `test_serve_rejects_unknown_verb`：`args[0] == "sh"` → `ok is False`。
  - `test_ping_answers_hello_with_pool_and_roots`：断言 `uid_pool == [start, size]`、`roots` 含 `E2B_IMAGE_CACHE_DIR`。
  - `test_hello_rejects_a_peer_uid_mismatch`：以 `E2B_BROKER_PEER_UID=<不是自己的 uid>` 起 daemon，连接 → `ok is False` 且 stderr 点名。
- [ ] **Step 2: 跑测试确认红**：`docker run --rm -v "$PWD:/w" -w /w e2b-sandlock-test:latest sh -c 'cc -O2 -Wall -Wextra -o /tmp/maint deploy/priv/maint.c deploy/priv/priv_common.c && python3 -m pytest tests/contract/test_broker_socket_c.py -q'` → 期望 `serve` 报 usage/未实现而失败。
- [ ] **Step 3: 实现**：
  - `priv_common.c`：`priv_roots_json(char *buf, size_t n)` 输出 `["<ws>","<state>","<shared>","<image cache>"]`（state/shared 与 ws 相同则去重，与 `PrivHelpers._root_paths()` 逐字同序）；`priv_peer_allowed(uid,gid,char *err,size_t)` 用 `E2B_BROKER_PEER_UID/GID` 比对；根列表里加 image-cache 根，**当且仅当 `E2B_IMAGE_CACHE_DIR` 已设置且非空**（不设默认值，与 Python 侧逐字同规则；见 Global Constraints 的裁定）。
  - `maint.c` `serve`：`socket(AF_UNIX)` → `bind`（先 `unlink` 陈旧 socket）→ `listen` → `accept` 循环；每个连接 `fork()` 一个 handler；handler 做 `SO_PEERCRED` 校验 → 读一行（上限 64 KiB）→ 极简 JSON 解析取 `args`（字符串数组）→ 校验 `args[0] ∈ {chown,rm,walk}` → `fork()` 孙子并 `dup2` 两根管道 → 孙子 `execv("/var/lib/e2b-priv/e2b-maint", args)`（该路径必须等于 `/proc/self/exe`，否则拒绝启动）→ 父用 `poll()` 收两条管道、按 `timeout_s` 超时 `kill(SIGKILL)` → 打印响应 JSON → 退出。
  - `ping`：连 socket，发 `{"v":1,"hello":true}`，把响应原样打到 stdout，退出码 = `ok ? 0 : 77`（给探活用）。
- [ ] **Step 4: 跑测试确认绿**：同 Step 2 命令，另加 `deploy/priv` 编译零 warning。
- [ ] **Step 5: 提交**：`git add deploy/priv tests/contract/test_broker_socket_c.py && git commit -m "feat(priv): e2b-maint serve/ping over a unix socket"`

---

### Task 2: Python 侧 socket transport + hello 自检

**Files:**
- Modify: `envd_service/priv_helpers.py`
- Test: `tests/unit/test_priv_broker_protocol.py`（新建）

**Interfaces:**
- Consumes：Task 1 的协议（逐字）。
- Produces：`PrivHelpers.broker_socket: Path | None`、`PrivHelpers.transport: str`；`priv_helpers.broker_chown(uid, path, *, recursive=False, gid=None)` 行为不变（Task 3 依赖）。

- [ ] **Step 1: 写失败测试**（用线程内假 daemon，纯 Python，跨平台）：
  - `test_socket_transport_sends_args_without_argv0`：断言假 daemon 收到的 `args[0] == "chown"` 且不含 `/var/lib/e2b-priv/e2b-maint`。
  - `test_socket_transport_maps_refusal_to_privhelpererror`：假 daemon 回 `{"ok":true,"exit":77,"stderr":"e2b-maint: refused"}` → `pytest.raises(PrivHelperError)` 且消息含 `refused by e2b-maint (exit 77)`（与今天的措辞逐字一致）。
  - `test_socket_transport_raises_on_ok_false`：`{"ok":false,"error":"..."}` → `PrivHelperError` 含该 error。
  - `test_hello_mismatch_refuses_to_start`：假 daemon 回 roots 少了 image cache → `resolve_priv_helpers()` 抛 `PrivHelperError`，消息点名 `roots`。
  - `test_missing_socket_refuses_to_start_when_transport_is_socket`：`E2B_PRIV_HELPER_TRANSPORT=socket` 且 socket 不存在 → 抛错，不回落。
  - `test_transport_auto_falls_back_to_exec_when_no_socket`：无 socket + 有 exec 二进制 → `transport == "exec"`。
  - `test_root_paths_include_the_image_cache`：`PrivHelpers._root_paths()` 含 `E2B_IMAGE_CACHE_DIR`。
- [ ] **Step 2: 跑测试确认红**：`tmp/testenv/bin/python -m pytest tests/unit/test_priv_broker_protocol.py -q`（无 `tmp/testenv` 时用 `.venv/bin/python`）。
- [ ] **Step 3: 实现**：`_root_paths()` 加第 4 根；`PrivHelpers` 增 `transport`/`broker_socket`；`_run()` 分派到 `_run_exec`（今天的实现）与 `_run_socket`（`socket.socket(AF_UNIX)` + `settimeout(timeout_s + 5)` + 一行 JSON 往返；`ok:false`/超时/连接失败 → `PrivHelperError`；非零 exit → 与今天同措辞）；`resolve_priv_helpers()` 按 `E2B_PRIV_HELPER_TRANSPORT` 分支，socket 模式必须握手成功且 roots/uid_pool 与本地配置逐字相同，否则抛错；`subprocess_env()` 只在 exec 模式调用。
- [ ] **Step 4: 跑测试确认绿** + `tmp/testenv/bin/python -m pytest tests/unit/test_priv_helpers.py tests/unit/test_shared_volume_traversal.py -q` 不得回归。
- [ ] **Step 5: 提交**：`git add envd_service/priv_helpers.py tests/unit/test_priv_broker_protocol.py && git commit -m "feat(priv): socket transport for the maintenance broker"`

---

### Task 3: secret 文件属主在非 root worker 上走 broker

**Files:**
- Modify: `envd_service/executors/sandlock.py:1965-1976`
- Test: `tests/unit/test_sandbox_secret_ownership.py`（新建）

**Interfaces:**
- Consumes：`priv_helpers.broker_chown(uid, path, recursive=False)`、`priv_helpers.active_helpers()`、`priv_helpers.helpers_cover(path)`（Task 2 之后在两种 transport 下都可用）。
- Produces：无新接口；修复"非 root worker + 每沙箱 uid"下 `Sandbox.create()` 必然失败（supervise 打不开 0600 的 secret 文件）。

- [ ] **Step 1: 写失败测试**（假 `active_helpers`，断言调用形状；不需要真 broker）：
  - `test_nonroot_worker_hands_the_secret_to_the_sandbox_uid`：`os.geteuid` 打桩为非 0、`_host_uid=21001`、`active_helpers()` 返回记录型 stub → 断言调用 `chown(uid=21001, path=<...>/x.secret, recursive=False)`，且 `os.chmod(path, 0o600)` 仍被调用。
  - `test_root_worker_keeps_the_direct_chown`：`euid=0` 时**不**调用 broker，走 `os.chown(path, 21001, -1)`。
  - `test_helpers_not_covering_the_path_fails_loudly`：`helpers_cover() is False` → 抛 `PrivHelperError` 并点名该路径（不许静默留成 worker 属主）。
- [ ] **Step 2: 跑测试确认红**：`tmp/testenv/bin/python -m pytest tests/unit/test_sandbox_secret_ownership.py -q` → 今天非 root 分支什么都不做，第 1 条会红。
- [ ] **Step 3: 实现**：把 `if os.geteuid() == 0 and identity:` 改成三分支（root → 直 chown；非 root 且 `helpers_cover` → `broker_chown(identity, path)`；非 root 且不覆盖 → `PrivHelperError`），`priv_helpers` 用局部 import（与 `checkpoint_store._hand_to_sandbox` 同形）。
- [ ] **Step 4: 跑测试确认绿** + `tests/unit/test_executor_*.py` 不回归。
- [ ] **Step 5: 提交**：`git add envd_service/executors/sandlock.py tests/unit/test_sandbox_secret_ownership.py && git commit -m "fix(priv): hand sandbox secret files to the pooled uid on a non-root worker"`

---

### Task 4（wave 2）: root broker DaemonSet + socket 目录 + 接管属主 init

**Files:** Create `deploy/k8s/priv-broker.yaml`（基线，**不是** overlay）；Modify `deploy/k8s/kustomization.yaml`（加进 resources）；Modify `deploy/k8s-k0s/kustomization.yaml`（overlay 只保留 NAS PV、seccomp 根、NodePort 这些发行版差异）。

⚠️ **2026-09-27 裁定（Task 4/5 评审的 Important 回归）**：最初把 DaemonSet 放在 `deploy/k8s-k0s/`，但 Task 5 把 `E2B_PRIV_HELPER_TRANSPORT=socket` 写进了**共享基线** `deploy/k8s/worker.yaml` ⇒ 不经 overlay 的部署（`docs/k8s-deployment.md` 把 `deploy/k8s/` 当可部署清单集）会因缺 socket 而 `Init:Error` 且属主 init 无人接管。修法就是把 DaemonSet 提到基线：基线本来就是"单个 RWX PVC + 2 副本"的共享卷形态，DaemonSet 挂同一 PVC 是自洽的；overlay 只留发行版差异。并补一条一致性 pin：**渲染基线时若 worker 声明 socket transport，同一渲染里必须存在 broker DaemonSet**。

**要点（不可变）**：`runAsUser: 0` + `capabilities.add: [CHOWN, DAC_OVERRIDE, FOWNER]`；`command: ["/var/lib/e2b-priv/e2b-maint","serve","--socket","/run/e2b-broker/broker.sock"]`；env 带 `E2B_UID_POOL_START/SIZE`、`E2B_WORKSPACE_BASE`、`E2B_STATE_BASE`、`E2B_SHARED_VOLUME_ROOT`、`E2B_IMAGE_CACHE_DIR`、`E2B_ROUTE_B_TMP_ROOT=<state>/.route-b`、`E2B_BROKER_PEER_UID=65534`/`E2B_BROKER_PEER_GID=65534`；挂同一个 RWX PVC（同路径）+ hostPath `/run/e2b-broker`（DirectoryOrCreate，initContainer 里 `chown 0:65534 && chmod 0710`）；把 `worker.yaml` 的两个属主 initContainer（`image-cache-init`、`workspace-root-init`）原样搬进来；livenessProbe = `e2b-maint ping --socket ...`。

⚠️ 终审要求这三处**同源**：worker pod 的 `runAsUser/runAsGroup` == `E2B_BROKER_PEER_UID/GID` == `/run/e2b-broker` 目录与 socket 文件的组。前两者已由 Python 启动断言强制（不一致即拒服并点名），yaml 里请写成同一个变量/注释说明，别各写一遍 `65534`。

**验收**：`kubectl kustomize deploy/k8s-k0s | grep -A3 'name: e2b-priv-broker'`；真机 `kubectl -n sandlock exec ds/e2b-priv-broker -- /var/lib/e2b-priv/e2b-maint ping --socket /run/e2b-broker/broker.sock` 返回 `ok:true`；`ls -l /run/e2b-broker` 为 `srw-rw---- root:65534`。

---

### Task 5（wave 2）: worker pod 去 root

**Files:** Modify `deploy/k8s/worker.yaml`（securityContext + initContainers + env），Delete `deploy/k8s-k0s/worker-root.patch.yaml`，Modify `deploy/k8s-k0s/kustomization.yaml`，Modify `tests/unit/test_worker_manifest_permissions.py:655-666`。

**要点**：worker 容器不再有 `runAsUser`（回落镜像 `USER 65534:65534`）；`capabilities.add` 收到 `[NET_BIND_SERVICE? 不需要 → 只留 SETUID, SETGID]`；新增 env `E2B_PRIV_HELPER_TRANSPORT=socket`、`E2B_PRIV_HELPER_SOCKET=/run/e2b-broker/broker.sock`、`E2B_IMAGE_CACHE_DIR=/var/lib/e2b-images`；挂 hostPath `/run/e2b-broker`；initContainers 换成非 root 的"等 socket 就绪"（30 次 × 2s，失败即退出）；单测断言改成 `runAsUser is None` + `transport == socket` + caps 恰为 `{SETUID,SETGID}`。

**验收**：`DRY_RUN=1 deploy/k8s-k0s/apply.sh | kubectl apply --dry-run=server -f -`；渲染单测绿。

---

### Task 6（wave 2）: 平台态属主一次性迁移

**Files:** Create `deploy/k8s-k0s/state-owner-migrate.yaml`（仿 `state-base-migrate.yaml`：`runAsUser: 0`、`backoffLimit: 0`、占位符 fail-closed）；Create `deploy/scripts/migrate-state-owner.sh`。

**要点**：worker 缩 0 → 对 `<export>/state/**`、`<export>/workspaces/_migrate`、`<export>/workspaces/_snapshots`、`<export>/_images`、`<export>/_secrets`、`<export>/_snapshots`、`<export>/_templates`、`<export>/_builds` 递归 `chown 65534:65534`（**8 条**），**树根下恰放行 `workspaces/_migrate` 与 `workspaces/_snapshots` 这两条确切条目，其余 `workspaces/**`（含 `workspaces` 本身、兄弟、两条下面的东西、`..` 与符号链接变体）一律拒绝**（那是池 uid 的树，脚本里用精确白名单 + 断言拒绝）。`workspaces/_snapshots` 是 **worker 的快照 payload 根**（`envd_service/agent.py` 把 copy/export/delete 硬编码在 `<workspace_base>/_snapshots`），控制面的快照**记录**根是另一条 `<export>/_snapshots`（`SnapshotRegistry` 建在共享 export 根上）——两条都在计划里。跑完 `stat` 留证。

**验收**：迁移后 `ls -ld <export>/state` 与 `ls -ld <export>/workspaces/_snapshots` 属主 65534；树的属主仍是池 uid（`stat` 前后对比）。

---

### Task 7（wave 2）: 文档与 pin 收尾

**Files:** `docs/production-deployment-requirements.md`（§2.4 能力表、§5.4(b) 判据改写、:173 的"线上实际是 root"审计句）、`deploy/k8s-k0s/README.md`（overlay 差异表：`worker-root.patch.yaml` 那行**删掉**并说明特权动作已移到**基线**的 broker——注意 broker **不是** overlay 差异，别作为新行加进那张表；:176 的"worker 以 root 跑"段落重写）、`docs/deploy-clusters.md` §7、`deploy/seccomp/README.md` 的 worker 形态描述（若无实质变化就写明"无变化"）、`tests/unit/test_worker_manifest_permissions.py`（若 Task 4/5 已收口则只做核对）、**`README.md`（第 247 行 `E2B_PRIV_HELPERS` 行的白名单口径：补齐 state base、第 4 根 image cache、以及 `E2B_PRIV_HELPER_TRANSPORT`/`E2B_PRIV_HELPER_SOCKET` 两个新旋钮）**，以及 **`docs/k8s-deployment.md`**（终审 Task 4/5 评审点名：§1 清单表补 `priv-broker.yaml` 一行、§2 部署顺序把 apply broker 放在 worker **之前**、升级段的 `kubectl set image` 补 `ds/e2b-priv-broker` 并把一致性自查的 `get deploy,sts` 扩成含 `ds`——broker 与 worker 镜像是一个契约、必须同版本滚）。

**验收（Task 7）**：`rg -n 'runAsUser: 0' deploy/k8s deploy/k8s-k0s` 只剩 broker DaemonSet（容器 + 3 个 init）、seccomp installer、control-plane 与两个一次性 Job，且每处都有注释说明为什么；全仓不再有指向已删除 `deploy/k8s-k0s/worker-root.patch.yaml` 的活引用（历史计划文档除外）；`docs/k8s-deployment.md` 的 apply 顺序与升级命令与新形态一致；`README.md` 的白名单口径含四根；渲染与单测仍全绿。

**验收**：`rg -n 'runAsUser: 0' deploy/k8s deploy/k8s-k0s` 只剩 seccomp installer / migration Job / broker DaemonSet 三处，且每处都有注释说明为什么。

---

### Task 8（wave 2）: 真机验收（需 KUBECONFIG 与授权）

```bash
deploy/scripts/open-cluster-tunnel.sh
export KUBECONFIG="$PWD/tmp/k0s/kubeconfig"
export E2B_API_URL=http://172.18.78.49:3000 E2B_SANDBOX_URL=http://172.18.78.49:3000
tmp/testenv/bin/python deploy/scripts/multinode_smoke.py
tmp/testenv/bin/python deploy/scripts/deployment_smoke.py
kubectl -n sandlock exec e2b-worker-0 -- sh -c 'stat -c "%a %u %g %n" /var/lib/e2b-sandboxes/workspaces/*/ | head -5'
```

判据：两条 smoke 全绿；树的 `770 <池 uid> 65534` 不变；worker pod `securityContext` 无 `runAsUser`；§5.4.1 第 3 步判据按"非网络文件系统才允许非 0"改写后的版本通过。

### Task 9（wave 2）: 终审遗留收口（Minor 记账，逐条有归属）

| # | 条目 | 出处 | 处置 |
|---|---|---|---|
| 1 | `ok` 必须是 bool 的守卫没有测试（改 `if False:` 套件仍全绿） | 终审二评 Minor 1 | ✅ 已覆盖：`tests/unit/test_priv_broker_protocol.py::test_socket_transport_rejects_an_ok_that_is_not_a_boolean`（`{"v":1,"ok":"yes","exit":0}` → `PrivHelperError`） |
| 2 | `timeout_s` 的杀进程路径无用例（唯一兜住 runaway `walk` 的边界，而 `walk` 是配额记账常驻路径） | 终审 Important 4 的剩余 | ✅ 已覆盖（2026-09-27 本轮补齐）：`tests/contract/test_broker_socket_c.py::test_a_walk_that_outlives_the_budget_is_killed_and_the_daemon_survives` —— 硬链接农场 60 万条目 / 200 字节名字，实测（arm64 dev 机、`e2b-sandlock-test` 容器、overlayfs）造树 ~19s、完整 walk ~3.3s、被杀的请求 1s（预算 1s，余量 >3×）；断言 `ok:false` + `timed out … SIGKILL`、daemon 存活、随后 hello 成功。单条用例 **22.8s**（lane 上限 30s），规模与成本写在 docstring 里 |
| 3 | `read_request` 无读截止（现由 `PRIV_MAX_HANDLERS=32` 兜着） | T1 复审 Minor | ✅ 已覆盖：`maint.c` 的 `request_read_ms`（`E2B_BROKER_REQUEST_READ_MS`）+ `test_a_silent_peer_is_refused_and_the_broker_keeps_serving` / `test_serve_refuses_an_unusable_request_read_deadline` |
| 4 | 拒绝路径 50ms/连接的 accept 节流 | T1 复审 Minor | 既有实现，**本轮未改**：拒绝路径先 `poll(PRIV_REFUSAL_WAIT_MS = 50ms)` 等**第一个字节**（等不到就放弃），之后才用 `recv(MSG_DONTWAIT)` 把已在途的请求排空 —— 每个被拒连接最多花 50ms，accept 循环不会被一个挂着的对端拖住；由 `test_serve_refuses_connections_over_the_handler_cap` / `test_a_peer_that_hangs_up_cannot_take_the_broker_down` 压住 |
| 5 | `_read_broker_line` 无长度上限 + 转义 6× 放大（256 MiB 算的是未转义字节） | T2 终审 Minor | ✅ 已覆盖：worker 侧读取设了上限，转义后的字节也计入（`envd_service/priv_helpers.py::_read_broker_line`）+ `test_socket_transport_refuses_an_answer_over_the_read_limit` |
| 6 | `_build_helpers` 对 socket 形态不要求"二进制存在"，而 `e2b-slot-spawn` 两种 transport 都要本地 | T2 复审 Minor | ✅ 已覆盖：`resolve_priv_helpers` 的 socket 分支要求本地 `e2b-slot-spawn` 存在，缺了就点名拒绝 + `test_socket_transport_still_needs_the_local_slot_spawn` |
| 7 | 契约测试 fixture 覆盖镜像内 `/var/lib/e2b-priv/e2b-maint`（硬杀不还原；Linux root 开发机会写到宿主） | T1 复审 Minor | ✅ 已覆盖：`_require_disposable_container()` 在 `euid != 0` 或不在一次性容器里时显式 `RuntimeError`（**不是** skip） |
| 8 | `E2B_BROKER_WORKER_UID/GID` 在**直接 exec** 形态下可被调用方环境污染（可达者本就能 exec 带 cap 的 broker） | 终审残留 | ✅ 已覆盖：`deploy/k8s/priv-broker.yaml` 的 `E2B_BROKER_PEER_UID/GID` 注释写明 "worker pod 侧**不得**设置 `E2B_BROKER_WORKER_UID/GID`"（它们是每次连接由 `SO_PEERCRED` 得出的结论，不是可配输入） |
| 9 | secret 侧：多条目失败不回滚 / `open()`→`chmod()` umask 窗口（**预先存在**）/ reclaim 依赖 worker 建的 `<secrets>/<id>` 非 sticky | T3 三评 Minor | 永久记录（本轮不动代码）：真机验收（Task 8）时确认 `<secrets>/<sandbox_id>` 由 worker 创建且非 sticky |
| 10 | SIGPIPE 回归测试含时序成分；harness 逐字节断言只对纯 ASCII 载荷成立；`SIG_IGN` 让直连 verb 的断管退出码 141→77 | T1 三评 Minor | 永久记录（本轮不动代码）：`141 → 77` 只影响"直连 verb 的调用方，在对端挂断时看到的退出码"，协议两侧都不读它（socket 形态由 daemon 自己收尾）；测试的时序成分已被 `test_a_peer_that_hangs_up_cannot_take_the_broker_down` 的轮询收口 |

**上线顺序（终审要求写进 Task 4/5 文档）**：先 apply **DaemonSet（新 C）**，再上**新 worker 镜像**——新 Python + 旧 daemon 会在握手期因缺 `peer_gid` fail closed（设计如此），而旧 Python + 新 daemon 向前兼容。

---

## Self-Review

- **Spec coverage**：C1 的四个动作（chown/rm/walk 外置、slot-spawn 留在 pod、init 搬运、平台态迁移）+ 现存 secret 缺陷，分别落在 Task 1/2、Task 4/5、Task 6、Task 3。
- **Placeholder scan**：无 TODO/TBD；wave 2 的每个任务都有文件、要点与验收命令。
- **Type consistency**：协议字段（`v`/`args`/`timeout_s`/`ok`/`exit`/`stdout`/`stderr`/`hello`/`roots`/`uid_pool`/`peer_uid`/`peer_gid`）在 Task 1 与 Task 2 中逐字一致；`broker_chown(uid, path, recursive=)` 在 Task 2/3 同形。
- **已知风险**：C 侧 `SO_PEERCRED`/`fts.h` 为 Linux-only → 测试必须在 `e2b-sandlock-test` 镜像内编译运行（本机 macOS 只跑 Python 侧单测）。
