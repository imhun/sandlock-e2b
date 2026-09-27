# netns 形态统一实施计划

> **执行状态（2026-09-27 更新）**：**已落地** —— ①②④ 与车队对齐（成对开 `E2B_ENABLE_NET_ISOLATION`+`E2B_FD_INJECT_CONNECT`、删低端口窗口），③ arm lane 保留；随后 **N38** 把池的 `E2B_EXECUTOR` 改成 `auto`、**N42** 补 `E2B_ENABLE_NETWORK="true"`、④ 的 seccomp 按出厂要求换成真档。
> **仍有效的决定**：回滚到共享 netns 时**需把低端口窗口加回来**（接受这个回滚代价，不承诺"不编辑文件即可干净回滚"）。**已更正的假设**：正文"删窗口这个动作本身会 EACCES"的**触发条件不准** —— 真正会让通配 `allowOut` 静默失效的是**缺 `E2B_ENABLE_NETWORK`**（N42），实测见 `docs/open-issues.md` N36。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把仓库里三处"共享 netns + 非 root + 低端口窗口"的示例/工具（① `deploy/compose/docker-compose.prod.yml`、② `autoscaler/backends/local.py`、④ `deploy/compose/docker-compose.multinode.yml`）与出厂车队对齐——成对打开 `E2B_ENABLE_NET_ISOLATION` + `E2B_FD_INJECT_CONNECT`、删除 `net.ipv4.ip_unprivileged_port_start=0`，只保留 ③ arm lane 的 `deploy/scripts/arm-lane/guest-prep.sh` 不动。

**Architecture:** 车队形态（`deploy/stack/docker-compose.prod.yml` + `deploy/k8s/worker.yaml`）让每个沙箱在自己的 user namespace 里 `unshare(CLONE_NEWNET)`，得到 loopback-only 的 netns；出网由 supervisor 在宿主 netns 建连后用 `SECCOMP_IOCTL_NOTIF_ADDFD` 注入已连接的 fd，入站由 50005+ 端口映射（MCP 网关自动占 61001–65535）。因此 wildcard-DNS 网关的 `127.0.1.x:53` bind 落在沙箱自己的 netns 内，由 userns 内的 root 用 `CAP_NET_BIND_SERVICE` 覆盖，容器级/宿主级低端口 sysctl 不再有任何用户。①②④ 只是接上同一对开关并删掉窗口，不改 envd、gateway 或 fork 的任何代码。

**Tech Stack:** Docker Compose（本地示例与池）、Python 3.14（`autoscaler/backends/local.py`、pytest 文本断言）、envd_service 的 `Settings`/`check_net_isolation_pairing`、sandlock fork 的 `net_isolation` + `fd_inject_connect`（wheel，不改）。

## Global Constraints

- 三处改动的**受众各不同**，行为差异要按受众写：① `deploy/compose/docker-compose.prod.yml` = 本地/单机生产示例的运维与 SDK 用户（`user: "65534:65534"`，per-sandbox host uid 会被自动关掉）；② `autoscaler/backends/local.py` = 本地池/开发机（`seccomp=unconfined` + `E2B_REQUIRE_SECCOMP_FILTER=0`，多 worker × 多沙箱叠加 userns）；④ `deploy/compose/docker-compose.multinode.yml` = 本地多节点拓扑验证（65534 + `seccomp=unconfined`，现状是**坏的**：用通配 `allowOut` 必 EACCES）。
- ③ `deploy/scripts/arm-lane/guest-prep.sh:39-64` **保留不动**：arm lane 的 Rust 套件默认共享 netns 且以 uid 501 跑，实测 stock 1024 下 `bind 127.0.1.9:53` = `EACCES`（`bind DNS gateway: Permission denied (os error 13)`），窗口打开才 551/0；`CAP_NET_BIND_SERVICE` 替代会死在 `pidfd_getfd: Operation not permitted (os error 1)`（file-cap exec 让进程 non-dumpable）。用户 2026-09-26 已拍板保留，调用方 `deploy/scripts/fork-gate.sh:46,77` 与 `deploy/scripts/arm-lane/lima-vm.sh:171` 不动。
- `E2B_ENABLE_NET_ISOLATION` 与 `E2B_FD_INJECT_CONNECT` **必须成对**：单开 `net_isolation` 是 loopback-only（出网在用户态只表现为超时），代码强制 fail-fast —— `envd_service/config.py:471-479` 的 `NET_ISOLATION_PAIRING_ERROR`，由 `config.py:666-686` 的 `check_net_isolation_pairing` 抛出，`envd_service/app.py:225` 在 `create_app` 第一步调用。因此"成对回滚"是设计而非缺陷。
- `E2B_ENABLE_NETNS` 是 **legacy no-op**，不是形态开关：fork 删掉了 per-sandbox veth/netns，`envd_service/executors/sandlock.py:664-669` 明写 "this legacy flag is a no-op"，唯一残留用途是 `envd_service/app.py:324-327` → `envd_service/netns.py:26-44` 的启动 plumbing（写 `net.ipv4.ip_forward=1` + iptables MASQUERADE），而出厂镜像里 `sysctl`/`iptables` 都不存在。①②④ 的注释统一写成"legacy，保留但忽略"（与 `deploy/stack/docker-compose.prod.yml:198` 同口径）。
- **不改**：`envd_service/**`、`gateway_common/**`、`third_party/sandlock/**`、`deploy/stack/docker-compose.prod.yml` 的形态、`deploy/k8s/**` 的形态（Task 5 只修 `deploy/k8s/worker.yaml` 里两处**已经写错**的注释）。不推送远程，不做 ACR 推送，不做线上变更。
- 临时文件一律放**项目内 `tmp/`**（不用系统 `/tmp`、不用 `$TMPDIR`）；证据日志首行带 ENV-HEADER（commit / 命令 / 时间）。
- 断言必须**精确匹配**，禁用 `toContain` / `includes` / 部分匹配；不新增 `skip`、不用 `--ignore` 掩盖失败。清单类改动沿用本仓既有做法——**文本断言而非 YAML 解析**（`tests/unit/test_worker_manifest_permissions.py:21-22` 写明 "the repo does not depend on PyYAML"）。
- 本机测试命令的事实（已核实）：`tmp/testenv/bin/python -m pytest …`（Python 3.14.7、pytest 9.1.1、fakeredis 2.37.1 齐备）；`.venv/bin/python` 里 **`fakeredis` 缺失**（`ModuleNotFoundError: No module named 'fakeredis'`），所以带 Redis 的用例只能用 `tmp/testenv`。
- 改完必须同步更新：`tests/unit/test_worker_manifest_permissions.py`（形态钉子）、`docs/production-deployment-requirements.md` §2.4.3/§2.4.7、`docs/SCALING.md` §7.1、`README.md`、`docs/k8s-deployment.md` §5、`docs/HANDOFF.md`、`docs/cross-platform-lanes.md`、`docs/security-audit/findings.md`、`docs/task-backlog.md` N36、`docs/open-issues.md` N36。
- Task 3、Task 4、Task 5、Task 6 都改 `tests/unit/test_worker_manifest_permissions.py` ⇒ **按序串行执行**（或各自 rebase 后再合），不要并行改同一文件。
- 决策已定（不改判就不再问）：② 的本地池**不加** userns 启动探针 —— `seccomp=unconfined` 时 `check_seccomp_filter` 在 `envd_service/config.py:634-641` 提前 return，`_userns_probe`（`config.py:539`）根本不跑；宿主禁 unprivileged userns 时表现为**每个 create 失败**而不是启动失败。本地池本来就是宽松档，接受这个语义（与 `E2B_REQUIRE_SECCOMP_FILTER=0` 同源）。
- 本计划**不**单独量化性能：netns 的量化代价已在 `docs/production-deployment-requirements.md` §2.4.6 记过（建连 p50/p95 `0.034/0.082 ms` → `0.291/0.560 ms`，~8.5×，只影响建连）；如需 A/B 复测用现成脚本 `tmp/netns-node-compare.py` 与 `tmp/mcp-3way.py`，那是独立动作，不阻塞本计划。

---

## 事实基线（本次勘察逐条核实，`文件:行` 为 2026-09-26 的 HEAD `cf183a4`）

### ① `deploy/compose/docker-compose.prod.yml`（258 行）

| 事实 | 位置 |
|---|---|
| `worker-1: &worker` / `worker-2` / `worker-3` 三个服务，共用 env anchor | `:130` / `:230` / `:237`；anchor `environment: &worker-env` 在 `:155` |
| 非 root：`user: "65534:65534"`，且 `:137-146` 说明 per-sandbox uid 会被自动关掉 | `:147` |
| 只有 legacy 开关，且注释已与实现脱节 | `:164-166`（注释）/`:167`（`E2B_ENABLE_NETNS: ${E2B_ENABLE_NETNS:-false}`） |
| **没有** `E2B_ENABLE_NET_ISOLATION` / `E2B_FD_INJECT_CONNECT`（全文件无） | — |
| 需要删掉的服务级窗口 | `sysctls:` `:198`，解释性注释 `:199-212`，值 `- net.ipv4.ip_unprivileged_port_start=0` `:213` |
| A6 注释里 "low-port window declared below" 这句会随窗口一起失效 | `:194-197` |
| 保留：shipped seccomp profile（非 unconfined） | `:214-227`（`- seccomp=${E2B_SECCOMP_PROFILE:-../seccomp/sandlock-worker.json}` 在 `:227`） |

### ② `autoscaler/backends/local.py`（129 行）

| 事实 | 位置 |
|---|---|
| 基础 env 字典（`**dict(worker_env or {})` 在末尾，故覆盖方向 = worker_env 赢） | `:48-54`（`**dict(worker_env or {})` 在 `:53`） |
| spawn 参数 `--network <net>`、卷挂载 | `:76-79` |
| A6 注释 "A6: no --cap-add SYS_ADMIN … the low-port window below" | `:80-83` |
| `--security-opt seccomp=unconfined` | `:84-85` |
| 需要删掉的两行 | `"--sysctl"` `:86`、`"net.ipv4.ip_unprivileged_port_start=0",` `:87` |
| `-e E2B_REQUIRE_SECCOMP_FILTER=0`（自检降级为 WARNING） | `:88-91` |
| 池 worker 的 env 来源 `E2B_AS_WORKER_ENV`（JSON），当前 JSON **不含**任何 netns 键 | `autoscaler/config.py:76-78`；`deploy/compose/docker-compose.autoscale.yml` 的 `E2B_AS_WORKER_ENV` 行（本计划不再引行号：它会漂，键名不会） |
| 池 worker 无 `--cap-add`、无 `--user` 覆盖 → 吃镜像的 `USER 65534` | `:66-107`（整个 `cmd`） |
| `E2B_AS_WORKER_ENV` 的 `-e` 在 spawn 的 `cmd` 里**后写**，故字典里的默认值可被运维覆盖 | `:106-107`（`for key, value in self._env.items()`） |

### ④ `deploy/compose/docker-compose.multinode.yml`（203 行）

| 事实 | 位置 |
|---|---|
| 三个 worker 各自完整 env（**无 anchor**），均无成对开关、也无 `E2B_ENABLE_NETNS` | 服务头 `:94` / `:131` / `:166`；env 块 `:104-117` / `:139-152` / `:174-187` |
| `user: "65534:65534"` | `:103` / `:138` / `:173` |
| 三处注释明写"本文件不声明窗口"（= 承认通配规则会 EACCES） | `:120-127` / `:155-162` / `:190-197`（核心句 `:123-126` / `:158-161` / `:193-196`） |
| `security_opt: seccomp=unconfined`（仅此一项） | `:128-129` / `:163-164` / `:198-199` |
| 无 `sysctls`、无 netns ⇒ 通配 `allowOut` 时 wildcard DNS 网关 bind `127.0.1.x:53` 必然 EACCES（仓库自己标注"未实测"） | `docs/open-issues.md:22`（N36 行） |

> **⚠️ 2026-09-26 实测更正（Task 4）：本文件里"通配 `allowOut` 必 EACCES"的说法不成立**
> —— Global Constraints 的 ④ 一条、本节的两行事实、Task 4 正文的"净效果"与"回滚"两条都按这句读。
> ④ 文件**原样**时通配**并不会** EACCES：容器 `ip_unprivileged_port_start` 本来就是 0，沙箱
> `IFACES=['lo','eth0']`、`RESOLV=127.0.0.2`、`DNS=10.250.0.2`、`EGRESS=CONNECT-OK 104.20.23.154`
> （`tmp/netns-unify-wildcard-before-b2.log`）；**把窗口强行关到 1024 才逐字复现**
> `bind DNS gateway: Permission denied (os error 13)`
> （`tmp/netns-unify-wildcard-forced-window-shut.log`），而切到 netns 后即使窗口仍是 1024 也照常解析+出网
> （`tmp/netns-unify-wildcard-after-window-shut.log`）—— 机制真实、原表述的触发条件不准。④ 真正的缺口是缺
> `E2B_ENABLE_NETWORK`（N42）。账本与正文的更正见 `docs/open-issues.md` N36、
> `docs/task-backlog.md` N36、`docs/production-deployment-requirements.md` §2.4.3。

### ③ `deploy/scripts/arm-lane/guest-prep.sh`（保留，不动）

`:39-64` 一次性打开窗口：有 `sysctl` 用 `sysctl -qw`（`:61`），没有则写 `/proc/sys/net/ipv4/ip_unprivileged_port_start`（`:63`）；理由与实测写在 `:46-57`。调用方 `deploy/scripts/fork-gate.sh:46,77`、`deploy/scripts/arm-lane/lima-vm.sh:171`。

### 权威参考（车队形态）

`deploy/stack/docker-compose.prod.yml`：anchor `environment: &worker-env` `:190`；`E2B_ENABLE_NETNS: "false"` `:198`；成对开关 `:209-210`（`${E2B_ENABLE_NET_ISOLATION:-true}` / `${E2B_FD_INJECT_CONNECT:-true}`）；worker-2 的单节点回滚 lever `:351-352`（`*_WORKER2`）；为什么容器级 `sysctls` 被撤 `:305-320`；seccomp `:328`。

`deploy/k8s/worker.yaml`：pod 级 `securityContext.sysctls` 已于 2026-09-17（N5）删除及理由 `:64-72`；三条 env `:278-283`。

钉子：`tests/unit/test_worker_manifest_permissions.py:57-104`（stack 无 `sysctls:`、成对开关在、`E2B_PID_NS` 在）、`:250-261`（`test_no_low_port_window_survives_anywhere`，**只扫 stack 与 k8s 两个文件**，不管 ①②④）、`:264-278`（k8s 与 stack 同形态）。

### 开关的默认值与机制

| 开关 | 读取点 | 代码默认 | 清单默认 | 语义 |
|---|---|---|---|---|
| `E2B_ENABLE_NETNS`（legacy） | `envd_service/config.py:133-135` | `false` | stack `:198` `"false"` | **no-op**（见 Global Constraints） |
| `E2B_ENABLE_NET_ISOLATION` | `envd_service/config.py:141-143` | `false` | stack `:209` `${…:-true}` | 沙箱在自己的 userns 之后 `unshare(CLONE_NEWNET)`，loopback-only |
| `E2B_FD_INJECT_CONNECT` | `envd_service/config.py:149-151` | `false` | stack `:210` `${…:-true}` | **配对项**：supervisor 在宿主 netns 建连 + `SECCOMP_IOCTL_NOTIF_ADDFD` 注入已连接 fd |

切形态**不需要**手配 `E2B_PORT_MAPPINGS`：MCP 入站端口在 netns 形态下自动映射（`envd_service/config.py:208-213` 的注释与 `port_mappings` 字段），池带 `61000-65535`（发放 61001 起）见 `envd_service/runtime/context.py:31-49` 与 `docs/production-deployment-requirements.md:1516-1545` §2.9。

**切形态后的可观测差异（三段受众共用的部分）**，出处 §2.4.6（`:501-528`）与 §2.4.7 的全量实测表（`:672-690`）：

- 沙箱内 `ip addr` 只见 `lo`（全量表记 `IFACES=lo`）；不能从宿主机直连沙箱端口，入站必须走 `/mcp` 代理或 50005+（池 61001+）映射端口。
- 出网全部经 supervisor 注入：建连 p50/p95 `0.034/0.082 ms` → `0.291/0.560 ms`（~8.5×，只影响建连；连接池/长连接无感）。
- 非阻塞 `connect_ex()` 从 `115 EINPROGRESS` 变 **`0 OK`**（唯一实证的语义差异，方向是"更友好"）。
- `getsockname()`（非 loopback 目标）两形态都是 worker 地址 —— 不是 netns 引入的。
- `docker inspect` 的 `HostConfig.Sysctls` 从 `{"net.ipv4.ip_unprivileged_port_start":"0"}` 变 `null`；容器 `CapEff` 仍为 0（不变）。
- 顺手消掉一个口子：容器级窗口对容器内**任何**进程生效，撤掉后 worker 容器里再没有无关进程能绑低端口（与 k8s N5 的理由相同）。
- 回滚语义：成对 `false` 即回到共享 netns，但**此时 wildcard `allowOut` 需要把低端口窗口加回来**（车队今天也是这个性质）。如果目的只是让沙箱能连宿主机端口，不要用回滚，用 50005+ 映射。

## 验证环境对照（哪些只能在本机 Docker/OrbStack 跑，哪些必须在集群/arm lane 跑）

| 验证 | 环境 | 说明 |
|---|---|---|
| 全部 pytest 文本断言（Tasks 1–6 的 Step 1/2/4） | **本机原生**（macOS） | `tmp/testenv/bin/python -m pytest tests/unit/...`，不碰 Docker |
| `deploy/scripts/test-prod-shaped.sh`（Task 1 的形态通道） | **本机 Docker/OrbStack** | 需要 Linux 容器 + `unshare(CLONE_NEWUSER)`；不能在 macOS 原生跑。脚本挂 `$HOME/.orbstack/run/docker.sock`，本机 OrbStack 已验证可用（§2.4.5 的 `tmp/prod-shaped-netns-on.log` 就是这条 channel 的产物） |
| ①②④ 的 `docker compose config` / `up` / smoke | **本机 Docker/OrbStack** | 三者都是**本地示例**，不属于集群部署形态；**不要**在 k0s 集群节点上跑这套 compose |
| ④ 的通配规则探针（切前 EACCES / 切后成功） | **本机 Docker/OrbStack** | 只读 worker 日志 + 一个 SDK 调用 |
| ③ arm lane（`deploy/scripts/fork-gate.sh` / `lima-vm.sh prep`） | **arm64 lane（Lima VM）** | 本计划不改 ③；Task 6 只改文档措辞 + 加钉子。可选回归：跑一次确认窗口仍在、套件仍 551/0 |
| k0s 集群 | **本计划不需要** | k8s 形态（`deploy/k8s/worker.yaml`）不涉及；如需只读核对必须按 `AGENTS.md`：`deploy/scripts/open-cluster-tunnel.sh` + `export KUBECONFIG="$PWD/tmp/k0s/kubeconfig"`。**禁止**写操作 |

---

### Task 1: 给 prod-shaped lane 加 netns 形态通道（后续三处的验证前提）

**Files:**
- Modify: `deploy/scripts/test-prod-shaped.sh:155-180`（在 `PIDNS_ENV` 块后插 `NETNS_ENV` 块，并在两处 `docker run` 的 `-e` 列表里各加一行 `$NETNS_ENV \`，与所在列表的续行同缩进）
- Create: `tests/unit/test_prod_shaped_lane_netns_passthrough.py`

**Interfaces:**
- Consumes: `deploy/scripts/test-prod-shaped.sh` 现有的 `MIRRORS_ENV`(`:124-133`)、`MEMORY_ENV`(`:150-153`)、`PIDNS_ENV`(`:158-161`)、`CACHE_ENV`(`:169-172`) 透传风格。
- Produces: `NETNS_ENV`（一个可空字符串，形如 `-e E2B_ENABLE_NET_ISOLATION=true -e E2B_FD_INJECT_CONNECT=true -e E2B_TEST_NET_ISOLATION=1`），Task 2–4 的形态验证靠它跑；三条 pytest 文本断言供 Task 6 引用为证据。

为什么排第一：`deploy/scripts/test-prod-shaped.sh` 只透传 `MIRRORS/MEMORY/PIDNS/CACHE`（`:124-172`），**没有任何 netns 变量**，于是 §2.4.5 记的那次"netns 形态本机全量"（`tmp/prod-shaped-netns-on.log`，`1439 passed, 3 skipped, 369.20s`）**用脚本复现不出来**。

**2026-09-26 更正（Task 1 实测）**：原因**不是**"contract 三条会被 skip"——那是错的。① runner 镜像自 `407a59c`（2026-09-03）起就有 `ENV E2B_TEST_NET_ISOLATION=1`（`deploy/docker/Dockerfile.test-runner:93-96`）；② `tests/contract/test_mcp_netns.py` 的 `_netns_servers()` 自己起 worker（`envd_settings_extra={"enable_net_isolation": True, "fd_inject_connect": True}`，`:68-70`）⇒ 该契约**自给自足**，lane 传不传那两个开关它都跑：09-16 那份日志里该模块有 45 条告警（= 执行过），它的 3 skipped 是 pure-shape / template_isolation / uid_pool。真正的原因是**另一件事**：lane 跑的是**整档**套件，而 in-process 控制面/worker 的默认形态**确实**读 `E2B_ENABLE_NET_ISOLATION` / `E2B_FD_INJECT_CONNECT`（`envd_service/config.py:142`/`:150`，`tests/unit/test_net_isolation_config.py` 钉住）⇒ "用脚本跑出**部署形态的整档**"在此之前做不到，**那**才是 §2.4.5 日志的复现前提。

- [ ] **Step 1: 写会失败的测试**

```python
"""The prod-shaped lane must be able to run the *whole* suite in the deployed shape.

What this pins -- and, just as important, what it is *not* about:

* **Not the netns contract.** ``tests/contract/test_mcp_netns.py`` is
  self-sufficient and has been all along: the runner image bakes
  ``E2B_TEST_NET_ISOLATION=1`` (``deploy/docker/Dockerfile.test-runner:93-96``,
  since ``407a59c`` 2026-09-03), so it was never skipped, and its
  ``_netns_servers()`` starts its own worker with
  ``envd_settings_extra={"enable_net_isolation": True, "fd_inject_connect": True}``.
  Neither of the lane's switches gates or shapes that module.
* **The whole suite's deployment shape.** The in-process control plane / worker
  take their *deployment default* from ``E2B_ENABLE_NET_ISOLATION`` and
  ``E2B_FD_INJECT_CONNECT`` (``envd_service/config.py:142``/``:150``, pinned by
  ``tests/unit/test_net_isolation_config.py``), and ``docker-compose.prod.yml``
  ships both as ``true``. ``deploy/scripts/test-prod-shaped.sh`` forwarded only
  MIRRORS/MEMORY/PIDNS/CACHE, so "run the suite the way the shipped stack runs"
  could not be selected from the script at all -- which is the premise of the
  documented netns-shaped full run (``tmp/prod-shaped-netns-on.log``,
  docs/production-deployment-requirements.md §2.4.5).

The two switches are a pair in the code (``create_app`` refuses the unpaired
shape), so the lane forwards them together and refuses half a pair. This pins
the passthrough in *both* phases (phase 1 root worker, phase 2 uid 65534 worker).
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
LANE = (REPO / "deploy" / "scripts" / "test-prod-shaped.sh").read_text(
    encoding="utf-8"
)


def _indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def test_lane_forwards_the_net_isolation_pair_when_set() -> None:
    # Unset stays unset: the code default is the shared-netns shape.
    assert 'NETNS_ENV=""\n' in LANE
    assert (
        'NETNS_ENV="-e E2B_ENABLE_NET_ISOLATION=${E2B_ENABLE_NET_ISOLATION} '
        "-e E2B_FD_INJECT_CONNECT=${E2B_FD_INJECT_CONNECT} "
        '-e E2B_TEST_NET_ISOLATION=${E2B_TEST_NET_ISOLATION:-1}"\n' in LANE
    )


def test_half_a_pair_is_refused_instead_of_silently_shared() -> None:
    # `create_app` refuses the unpaired shape, so a lane that quietly ran the
    # shared-netns shape while the operator asked for netns would be a lie.
    assert (
        '    echo "!! E2B_ENABLE_NET_ISOLATION and E2B_FD_INJECT_CONNECT must '
        'be set together (create_app refuses the unpaired shape)" >&2\n' in LANE
    )
    assert "    exit 2\n" in LANE


def test_both_phases_carry_the_net_isolation_pair() -> None:
    # Two `docker run` invocations: phase 1 (root worker) and phase 2 (uid
    # 65534). Each carries the line in the same argument list as the other
    # forwarded shapes -- right after `$PIDNS_ENV \`, at that list's own
    # indentation. Asserting the structure (which run owns the line, what it
    # sits behind, how it is indented) keeps the pin without hardcoding a
    # column count that only phase 1 happens to satisfy.
    lines = LANE.splitlines()
    runs = [i for i, line in enumerate(lines) if line.strip().startswith("docker run ")]
    assert len(runs) == 2

    forwarded = [i for i, line in enumerate(lines) if line.strip() == "$NETNS_ENV \\"]
    assert len(forwarded) == 2

    for index in forwarded:
        anchor = lines[index - 1]
        assert anchor.strip() == "$PIDNS_ENV \\"
        assert _indent(lines[index]) == _indent(anchor)

    owners = {max(run for run in runs if run < index) for index in forwarded}
    assert owners == set(runs)
```

- [ ] **Step 2: 跑它，确认失败**

Run: `tmp/testenv/bin/python -m pytest tests/unit/test_prod_shaped_lane_netns_passthrough.py -q -p no:cacheprovider`

Expected: FAIL —— 三条都红：第一条报 `assert 'NETNS_ENV=""\n' in LANE`（脚本里没有这个符号），第三条报 `assert 0 == 2`。（若脚本里已经存在一版缩进不对的 `$NETNS_ENV \`，第三条改为报缩进不符 —— 2026-09-26 第二轮实测：`assert '    ' == '        '`。）

- [ ] **Step 3: 最小改动**

在 `deploy/scripts/test-prod-shaped.sh` 的 `PIDNS_ENV` 块之后（`:161` 与 `:163` 的 `CACHE_ENV` 注释之间）插入：

```sh
# The per-sandbox network namespace is a *deployment* shape too (E7.2): the
# shipped stack (`docker-compose.prod.yml`) and the k8s manifest run
# `E2B_ENABLE_NET_ISOLATION=true` + `E2B_FD_INJECT_CONNECT=true`, and this lane
# exists to reproduce the deployed shape -- of the *whole* suite, because the
# in-process control plane and worker read these two variables as their
# deployment default (envd_service/config.py, pinned by
# tests/unit/test_net_isolation_config.py). Forward them when both are set;
# without that the lane could only ever run the code default (shared netns)
# shape, which is why the documented netns-shaped full run (§2.4.5) was not
# reproducible from the script.
# `tests/contract/test_mcp_netns.py` is *not* the reason: it never needed this
# passthrough (the runner image bakes `E2B_TEST_NET_ISOLATION=1`, and the
# contract's own harness sets the worker's pair), so it runs the same way
# whether the pair arrives here or not. `E2B_TEST_NET_ISOLATION` is forwarded
# only so the shape stays explicit end to end.
# The two are a pair -- `create_app` refuses the single-switch shape by name (it
# would leave every sandbox loopback-only, i.e. "the network is down" with no
# error anywhere) -- so forward them together, and only when both are set.
# Unset stays unset: the code default is the shared-netns shape.
NETNS_ENV=""
if [ -n "${E2B_ENABLE_NET_ISOLATION:-}" ] && [ -n "${E2B_FD_INJECT_CONNECT:-}" ]; then
    NETNS_ENV="-e E2B_ENABLE_NET_ISOLATION=${E2B_ENABLE_NET_ISOLATION} -e E2B_FD_INJECT_CONNECT=${E2B_FD_INJECT_CONNECT} -e E2B_TEST_NET_ISOLATION=${E2B_TEST_NET_ISOLATION:-1}"
elif [ -n "${E2B_ENABLE_NET_ISOLATION:-}${E2B_FD_INJECT_CONNECT:-}" ]; then
    echo "!! E2B_ENABLE_NET_ISOLATION and E2B_FD_INJECT_CONNECT must be set together (create_app refuses the unpaired shape)" >&2
    exit 2
fi
```

然后在**两处** `docker run` 的参数列表里，紧跟 `$PIDNS_ENV \` 之后各加一行（phase 1 在 `:212` 之后、4 空格续行；phase 2 在 `:244` 之后、8 空格续行 —— **与该列表的邻居同缩进**，测试是按结构钉的，不硬编码列数）：

```sh
    $NETNS_ENV \
```

保留既有的 `# shellcheck disable=SC2086` 写法（未加引号的展开是刻意的，与 `$MIRRORS_ENV` 等一致）。

- [ ] **Step 4: 跑测试确认通过，并用它跑一次真形态**

Run: `tmp/testenv/bin/python -m pytest tests/unit/test_prod_shaped_lane_netns_passthrough.py -q -p no:cacheprovider`

Expected: `3 passed`

Run（**本机 Docker/OrbStack**，实测约 6–12 分钟 —— 本计划唯一的慢步骤；`tmp/` 下留证）：

```bash
E2B_REGISTRY_MIRRORS=registry-1.docker.io=127.0.0.1:5080 \
E2B_ENABLE_NET_ISOLATION=true E2B_FD_INJECT_CONNECT=true \
  ./deploy/scripts/test-prod-shaped.sh 2>&1 | tee tmp/netns-unify-lane.log
```

Expected:

1. phase 1 的汇总行是 `… passed, … skipped, 0 failed`，且 `tests/contract/test_mcp_netns.py` 的三条**不是 skip**（`rg -c "test_mcp_netns.py" tmp/netns-unify-lane.log` 不应出现 SKIPPED 行）。**这一条只是回归检查**：该契约本来就自给自足（见 Task 1 的 2026-09-26 更正），通道的作用是让**整档**跑在部署形态的默认值上，不是"解掉它的 skip"。条数应与**同日的共享 netns 档**逐条一致；§2.4.5 记的 `1439 passed / 3 skipped` 是 2026-09-16 的快照，此后测试数已增长，所以以"`0 failed` + 与同日共享 netns 档一致"为准，具体条数记进日志。
2. phase 2（uid 65534）的行尾是 `0 failed`；条数与共享 netns 档一致（`docs/open-issues.md` OBS-5 行记 phase 2 = 57 passed 可作参照）。
3. 这两条是**本计划最大的未知**：车队证据（§2.4.6/§2.4.7）来自"root/cap_SETUID worker + per-sandbox uid 开"，而 ①②④ 是 `65534 + per-sandbox uid 自动关`。**phase 2 不绿就不要往下做 Tasks 2–4**，把日志贴回来重新评估（届时 ①②④ 的形态选择要重新拍板）。
   > **⚠️ 2026-09-26 更正（Task 1 实测）**：这道门只覆盖"成对开关作为 worker 默认值、无特权相位整档仍全绿"，**不覆盖 netns 契约** —— phase 2 的选择集是 5 个 sandlock/route-B 文件，不含 `tests/contract/test_mcp_netns.py`（Task 1 报告 §7.3 实测写明）。"65534 这一格的形态"另有实测通道，见 Task 3 的同日更正。

若 phase 2 因 `unshare(CLONE_NEWUSER)` EPERM 而红，先在宿主核对 §2.4.6 提到的两条内核开关（`kernel.apparmor_restrict_unprivileged_userns`、`user.max_user_namespaces`），并把 `sysctl -a | rg "user.max_user_namespaces|apparmor_restrict"` 的输出一起写进日志。

- [ ] **Step 5: 提交**

```bash
git add deploy/scripts/test-prod-shaped.sh tests/unit/test_prod_shaped_lane_netns_passthrough.py
git commit -m "test(lane): let the prod-shaped lane reproduce the netns shape"
```

---

### Task 2: ② 本地池 `autoscaler/backends/local.py` 切车队形态

**Files:**
- Modify: `autoscaler/backends/local.py`（基础 env 字典 `self._env`、`cmd` 里的注释与要删的两行）
  —— 基础字典另立一条（2026-09-26 N38 第 4 项，控制器裁定）：`E2B_ENABLE_NETWORK` 与
  `E2B_ROUTE_B_TMP_ROOT` 也进基础字典，取值同车队，好让不经 `E2B_AS_WORKER_ENV` 直接构造的
  `DockerPoolBackend()` 自身自足（`E2B_AS_WORKER_ENV` 仍是覆盖入口，后写者赢）
- Modify: `deploy/compose/docker-compose.autoscale.yml` 的 `E2B_AS_WORKER_ENV` 行（JSON：先补成对的
  `E2B_ENABLE_NET_ISOLATION`/`E2B_FD_INJECT_CONNECT`；2026-09-26 的 N38 追加裁定再补
  `E2B_ENABLE_NETWORK` 与 `E2B_ROUTE_B_TMP_ROOT`，取值逐字取自
  `deploy/stack/docker-compose.prod.yml:196`/`:239`）
- Create: `tests/unit/test_autoscaler_local_backend_shape.py`

**Interfaces:**
- Consumes: `autoscaler/backends/local.py:106-107` 的 `for key, value in self._env.items(): cmd += ["-e", f"{key}={value}"]`（后写者赢 ⇒ 字典默认值可被 `E2B_AS_WORKER_ENV` 覆盖）。
- Produces: `LocalBackend._env` 里 `"E2B_ENABLE_NET_ISOLATION": "true"` 与 `"E2B_FD_INJECT_CONNECT": "true"` 两个键；三处文本断言供 Task 6 引用。

为什么放**基础字典**而不是 `cmd`：`cmd` 里的 `-e KEY=V` 是硬编码且写在字典的 `-e` 之前（`:88-107`），放进去就把运维的最后一条退路封了（必须改代码才能关 netns）；放字典里，`E2B_AS_WORKER_ENV` 仍能覆盖成 `"false"`。

- [ ] **Step 1: 写会失败的测试**

```python
"""The local Docker pool spawns workers in the fleet's net-isolation shape.

`autoscaler/backends/local.py` ran every pooled worker behind
`--sysctl net.ipv4.ip_unprivileged_port_start=0` while
`E2B_ENABLE_NET_ISOLATION` stayed off -- the shared-netns + non-root shape the
fleet left on 2026-09-16 (`deploy/stack/docker-compose.prod.yml:209-210`). The
k8s backend only scales replicas of the StatefulSet (`autoscaler/backends/k8s.py:94-101`),
so this file is the *only* pool that needs the pair. `E2B_AS_WORKER_ENV` is
carried in the compose file too, so an operator sees the shape without reading
Python.

Text assertions rather than importing the backend: running `docker` is the
only thing this module does, and the repo pins manifests by their text
(`tests/unit/test_worker_manifest_permissions.py`).
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
LOCAL_BACKEND = (REPO / "autoscaler" / "backends" / "local.py").read_text(
    encoding="utf-8"
)
AUTOSCALE_COMPOSE = (
    REPO / "deploy" / "compose" / "docker-compose.autoscale.yml"
).read_text(encoding="utf-8")


def test_local_pool_no_longer_declares_a_low_port_window() -> None:
    assert "net.ipv4.ip_unprivileged_port_start" not in LOCAL_BACKEND
    assert '"--sysctl"' not in LOCAL_BACKEND


def test_local_pool_spawns_the_paired_net_isolation_switches() -> None:
    assert '\n            "E2B_ENABLE_NET_ISOLATION": "true",\n' in LOCAL_BACKEND
    assert '\n            "E2B_FD_INJECT_CONNECT": "true",\n' in LOCAL_BACKEND
    # Still `seccomp=unconfined` + E2B_REQUIRE_SECCOMP_FILTER=0: the pool is the
    # permissive local shape on purpose, and the pair above is what makes the
    # sandbox (not the worker) the one that binds :53.
    assert '\n                "seccomp=unconfined",\n' in LOCAL_BACKEND
    assert '\n                "E2B_REQUIRE_SECCOMP_FILTER=0",\n' in LOCAL_BACKEND


def test_autoscale_compose_carries_the_same_pair() -> None:
    assert '"E2B_ENABLE_NET_ISOLATION": "true"' in AUTOSCALE_COMPOSE
    assert '"E2B_FD_INJECT_CONNECT": "true"' in AUTOSCALE_COMPOSE
```

- [ ] **Step 2: 跑它，确认失败**

Run: `tmp/testenv/bin/python -m pytest tests/unit/test_autoscaler_local_backend_shape.py -q -p no:cacheprovider`

Expected: FAIL —— 第一条报 `assert 'net.ipv4.ip_unprivileged_port_start' not in LOCAL_BACKEND`（`:87` 有它）；第二条报 `assert '\n            "E2B_ENABLE_NET_ISOLATION": "true",\n' in LOCAL_BACKEND`；第三条报 `assert '"E2B_ENABLE_NET_ISOLATION": "true"' in AUTOSCALE_COMPOSE`。

- [ ] **Step 3: 最小改动**

3a. `autoscaler/backends/local.py` —— 删掉 `:86-87` 两行：

```python
                "--sysctl",
                "net.ipv4.ip_unprivileged_port_start=0",
```

3b. 把 `:80-83` 的注释从"还有窗口"改成"窗口已撤、理由同车队"：

```python
                # A6: no --cap-add SYS_ADMIN. The shared-volume bind was
                # deleted in A4 and quota goes through quota-agent
                # (E2B_QUOTA_AGENT_URL); the container-level low-port window is
                # gone too (2026-09-26) -- the pooled workers run the fleet's
                # per-sandbox netns below, where the wildcard-DNS `:53` bind
                # happens inside the sandbox's own netns as root-in-userns.
```

3c. 在基础 env 字典（`:48-54`）里，`**dict(worker_env or {})` **之前**插入两个键，保持"运维赢"的覆盖方向：

```python
        self._env = {
            "E2B_NODE_MEMORY_MB": node_memory_mb,
            "E2B_NODE_CPU_PERCENT": node_cpu_percent,
            "E2B_NODE_DISK_MB": node_disk_mb,
            "E2B_NODE_PROCESSES": node_processes,
            # Fleet shape (deploy/stack/docker-compose.prod.yml:209-210): each
            # sandbox gets its own loopback-only netns and egress is mediated by
            # the supervisor's connect fd injection. BOTH are required --
            # `create_app` refuses the unpaired shape by name
            # (envd_service/config.py:471-479), which crash-loops a worker
            # rather than silently cutting every sandbox's network. Set here
            # (not in `cmd`) so E2B_AS_WORKER_ENV can still override the pair
            # to "false": the dictionary expands first, worker_env last.
            "E2B_ENABLE_NET_ISOLATION": "true",
            "E2B_FD_INJECT_CONNECT": "true",
            **dict(worker_env or {}),
        }
```

3d. `deploy/compose/docker-compose.autoscale.yml` 的 `E2B_AS_WORKER_ENV` 行 —— 在它的单引号 JSON 里，紧跟 `"E2B_EXECUTOR": "${E2B_EXECUTOR:-local}", ` 之后插入 `"E2B_ENABLE_NET_ISOLATION": "true", "E2B_FD_INJECT_CONNECT": "true", `。改完那一行是：

   > **⚠️ 本步已执行，且实际值与本步文字不同（2026-09-26 更正）**：落地时 `E2B_EXECUTOR` 的默认被**一并改成 `auto`**（用户裁定，见《追加裁定：N38》）—— 因为按 `local` 默认，那两个开关是**空转**的（`local` 执行器不做 Sandlock 隔离，`enable_net_isolation` 只在 `envd_service/executors/factory.py:210` 传给 sandlock 执行器）。下面那行 `:-local` 是**改动前**的样子，照它写会得到"改了等于没改"。**实际值**：`"E2B_EXECUTOR": "${E2B_EXECUTOR:-auto}"`，且另补了 `E2B_ENABLE_NETWORK` 与 `E2B_ROUTE_B_TMP_ROOT`（见 Task 2 的收口记录）。

```yaml
      E2B_AS_WORKER_ENV: '{"E2B_BASE_IMAGE": "${E2B_BASE_IMAGE:-python:3.14-slim}", "E2B_EXECUTOR": "${E2B_EXECUTOR:-local}", "E2B_ENABLE_NET_ISOLATION": "true", "E2B_FD_INJECT_CONNECT": "true", "E2B_IMAGE_CACHE_DIR": "/var/lib/e2b-sandboxes/_images", "E2B_IMAGE_CACHE_MAX_BYTES": "4294967296", "E2B_IMAGE_CACHE_EVICT_MIN_AGE_S": "300", "E2B_IMAGE_CACHE_OWNER_UID": "65534", "E2B_NODE_MEMORY_MB": "2048", "E2B_NODE_CPU_PERCENT": "200", "E2B_NODE_DISK_MB": "4096", "E2B_NODE_PROCESSES": "256"}'
```

**行为差异（受众＝本地池/开发机）**

- 池里每个 worker 的每个沙箱都进自己的 userns+netns（此前是 `seccomp=unconfined` 下的共享 netns）⇒ 受 `user.max_user_namespaces` / `kernel.apparmor_restrict_unprivileged_userns` 门控；本机 OrbStack 已被 §2.4.5 证明可用，但**池是唯一会"多 worker × 多沙箱"叠加 userns 水位的地方**。
- 沙箱内 `ip addr` 只见 `lo`；`docker exec` 进 **worker** 容器仍能 ping 外网（supervisor 在宿主 netns 建连），但沙箱内部看不到 worker 的网卡 —— 本地调试最容易困惑的一点。
- 出网全部经 supervisor 注入（建连 +0.25 ms 量级；非阻塞 `connect_ex()` 立即返回 0）。
- 入站只能走池的 61001+ 映射（`envd_service/runtime/context.py:31-49`）。
- 没有 fail-fast：`Seccomp: 0`（`seccomp=unconfined`）+ `E2B_REQUIRE_SECCOMP_FILTER=0` 让 `check_seccomp_filter` 在 `envd_service/config.py:634-641` 提前 return，`_userns_probe`（`config.py:539`）不跑 ⇒ 宿主禁 userns 时表现为**每个 create 失败**。这是 Global Constraints 里已接受的决策。
- k8s 池不受影响：`autoscaler/backends/k8s.py:94-101` 只 scale StatefulSet/Deployment 的副本数，形态来自 `deploy/k8s/worker.yaml:278-283` 的 pod template（已带成对开关）。

- [ ] **Step 4: 跑测试确认通过，再跑池的形态验证（本机 Docker/OrbStack）**

Run: `tmp/testenv/bin/python -m pytest tests/unit/test_autoscaler_local_backend_shape.py -q -p no:cacheprovider`

Expected: `3 passed`

Run:

```bash
docker compose -f deploy/compose/docker-compose.autoscale.yml up -d --build
# 等 autoscaler 至少孵出一个 worker 后：
docker ps -q -f label=e2b.role=worker | head -1 | xargs -I{} docker inspect {} \
  --format 'sysctls={{json .HostConfig.Sysctls}}{{"\n"}}{{range .Config.Env}}{{println .}}{{end}}' \
  | tee tmp/netns-unify-pool.txt
```

Expected: `sysctls=null`（**不是** `{"net.ipv4.ip_unprivileged_port_start":"0"}`），Env 里同时出现 `E2B_ENABLE_NET_ISOLATION=true` 与 `E2B_FD_INJECT_CONNECT=true`。

Run（形态 + 业务，两个断言都是精确匹配）：

```bash
docker compose -f deploy/compose/docker-compose.autoscale.yml logs --since 5m autoscaler | rg "NET_ISOLATION_PAIRING_ERROR"   # 期望：无输出
python - <<'PY'
import os
os.environ.setdefault("E2B_API_KEY", "local-key")
from e2b import Sandbox
sbx = Sandbox.create(api_key=os.environ["E2B_API_KEY"])
try:
    out = sbx.commands.run("ip -o addr | awk '{print $2}'").stdout
    assert out.strip() == "lo:", out          # 沙箱自己的 netns：只有 lo
    assert sbx.commands.run("python3 -c 'print(1+1)'").stdout == "2\n"
finally:
    sbx.kill()
print("POOL NETNS SHAPE OK")
PY
```

Expected: 最后一行打印 `POOL NETNS SHAPE OK`（收尾 `docker compose -f deploy/compose/docker-compose.autoscale.yml down` 由执行者按需决定；`tmp/netns-unify-pool.txt` 留证）。

**形态验证的前提与池的新出网语义（2026-09-26 补记，N38 追加裁定后实测）**

- **池沙箱的出网语义当场变了（用户可感知，别只当成"修一个让 worker 起不来的键"）**：
  `E2B_ENABLE_NETWORK` 一补上，"静默无网"就变成**按沙箱规则集出网** —— 请求里不带 `network`
  的沙箱只能到固定域名集（pypi/npm/github，`envd_service/executors/sandlock.py` 的固定规则集），
  **与车队一致，但对"池只是本地调试"的人是新行为**。
- **必须显式传 `WORKER_IMAGE=`。** compose 的 `E2B_AS_DOCKER_IMAGE` 默认
  `.../e2b-sandlock-worker:0.1.0` 是 2026-08-30 的快照，该镜像里 `envd_service/config.py`
  **没有** `E2B_ENABLE_NET_ISOLATION` 这个字段（镜像内 `grep -c` = 0）⇒ 在那个 tag 上
  `E2B_EXECUTOR=auto`（N38）**等于没切**：实测同一条探针仍是 `lo,eth0`
  （`tmp/task2-review/run-c-shape-probe-default-image.log`）。池没有"跟着仓库构建 worker"的
  步骤（`build:` 只给 control-plane/autoscaler），所以这里的一串命令要
  `WORKER_IMAGE=<用本工作树构建的 tag> docker compose ... up -d`；**默认 tag 要不要换是另一个
  决定**（未定，登记在 `docs/open-issues.md` N40）。
- 池的 worker env 与车队**还有若干既有漂移**未修，最直接的一条是 MCP 基镜像：
  `/usr/bin/mcp-gateway` 是 `COPY` 进 **worker 镜像**的
  （`deploy/docker/Dockerfile.envd:69`），而池把 `E2B_BASE_IMAGE` 钉在 `python:3.14-slim`
  ⇒ 池里建 MCP 沙箱必然 503（`mcp gateway failed to start ... can't open file
  '/usr/bin/mcp-gateway'`）。**既有漂移，见 N40**，不在本计划的改动范围内。

- [ ] **Step 5: 提交**

```bash
git add autoscaler/backends/local.py deploy/compose/docker-compose.autoscale.yml tests/unit/test_autoscaler_local_backend_shape.py
git commit -m "feat(autoscaler): the local pool spawns the fleet netns shape"
```

---

### Task 3: ① `deploy/compose/docker-compose.prod.yml` 切车队形态

**Files:**
- Modify: `deploy/compose/docker-compose.prod.yml:164-167`（legacy 注释 + 插入成对开关）、`:194-213`（改写注释、删整块 `sysctls:`）
- Modify: `tests/unit/test_worker_manifest_permissions.py:36-47`（新增 `COMPOSE_PROD` 常量）与文件末尾（新增一条测试）

**Interfaces:**
- Consumes: `tests/unit/test_worker_manifest_permissions.py:57-104` 的切片技巧 —— `STACK_COMPOSE.split("\n  worker-1: &worker", 1)[1].split("\n  worker-2:", 1)[0]`；prod 示例有**同样的两个锚点**（`  worker-1: &worker` 在 `:130`、`  worker-2:` 在 `:230`），可直接复用。
- Produces: 模块级常量 `COMPOSE_PROD`（读 `deploy/compose/docker-compose.prod.yml`）与 `test_compose_prod_example_runs_the_fleet_netns_shape()`；Task 6 引用这套钉子作为证据。

- [ ] **Step 1: 写会失败的测试**

在 `tests/unit/test_worker_manifest_permissions.py` 的常量区（`:36-47`，紧随 `STACK_COMPOSE` 之后）加：

```python
COMPOSE_PROD = (
    REPO / "deploy" / "compose" / "docker-compose.prod.yml"
).read_text(encoding="utf-8")
```

在文件末尾加：

```python
def test_compose_prod_example_runs_the_fleet_netns_shape() -> None:
    """N36: the single-host example follows the fleet, window and all.

    `deploy/compose/docker-compose.prod.yml` ran the shared-netns shape (uid
    65534) and paid for it with a container-level
    `net.ipv4.ip_unprivileged_port_start=0` window. It now carries the same
    paired switches the stack anchor does, so the wildcard-DNS `:53` bind
    happens inside each sandbox's own netns instead. A half-migration -- window
    back, or one switch without the other -- is what these assertions catch.

    Pinned here rather than in a new file because this module already owns the
    "the manifests must not ask the worker to bind a low port" family, and the
    slice trick below is the same one the stack assertions use.
    """
    worker = COMPOSE_PROD.split("\n  worker-1: &worker", 1)[1].split(
        "\n  worker-2:", 1
    )[0]
    # The directive, not the prose: the comment above the line names the old
    # value on purpose.
    assert "\n    sysctls:\n" not in COMPOSE_PROD
    assert "\n      - net.ipv4.ip_unprivileged_port_start=0\n" not in COMPOSE_PROD
    # The anchor every worker inherits (worker-2/worker-3 use `<<: *worker-env`).
    assert "\n      E2B_ENABLE_NET_ISOLATION: ${E2B_ENABLE_NET_ISOLATION:-true}\n" in worker
    assert "\n      E2B_FD_INJECT_CONNECT: ${E2B_FD_INJECT_CONNECT:-true}\n" in worker
    # The worker still runs the shipped seccomp profile, not `unconfined`.
    assert "\n      - seccomp=${E2B_SECCOMP_PROFILE:-../seccomp/sandlock-worker.json}\n" in worker
```

- [ ] **Step 2: 跑它，确认失败**

Run: `tmp/testenv/bin/python -m pytest "tests/unit/test_worker_manifest_permissions.py::test_compose_prod_example_runs_the_fleet_netns_shape" -q -p no:cacheprovider`

Expected: FAIL with `assert '\n    sysctls:\n' not in COMPOSE_PROD`（`:198` 就是它）。

- [ ] **Step 3: 最小改动**

3a. `:164-167` 的注释与 env 改成（`E2B_ENABLE_NETNS` 这一行的位置不动，只在它下面插两条）：

```yaml
      # Legacy, compatibility only: the fork dropped per-sandbox veth/netns,
      # so this flag is accepted but ignored
      # (envd_service/executors/sandlock.py:664-669). The shape switch is the
      # pair below, same as the fleet.
      E2B_ENABLE_NETNS: ${E2B_ENABLE_NETNS:-false}
      # Fleet shape (deploy/stack/docker-compose.prod.yml:209-210): each
      # sandbox gets its own loopback-only netns, and outgoing connects are
      # mediated by the supervisor's connect-fd injection. BOTH are required --
      # `create_app` refuses the unpaired shape by name
      # (envd_service/config.py:471-479; the single switch leaves every sandbox
      # loopback-only, i.e. "the network is down" with nothing in the logs), so
      # a typo here crash-loops the worker instead of silently breaking
      # sandboxes. This is why no container-level `sysctls:` window is declared
      # any more: the wildcard-DNS `:53` bind now happens inside the sandbox's
      # own netns, covered by its root-in-userns CAP_NET_BIND_SERVICE.
      # Rollback (stack precedent: deploy/stack/docker-compose.prod.yml:349-352)
      # is setting both to false in deploy/compose/.env and recreating -- and
      # then wildcard allowOut needs the low-port window back.
      E2B_ENABLE_NET_ISOLATION: ${E2B_ENABLE_NET_ISOLATION:-true}
      E2B_FD_INJECT_CONNECT: ${E2B_FD_INJECT_CONNECT:-true}
```

3b. `:194-197` 的 A6 注释改成：

```yaml
    # A6: the worker needs no SYS_ADMIN (shared-volume bind deleted in A4,
    # quota served by the quota-agent, and since 2026-09-26 no low-port window
    # either -- see the net-isolation pair above). Enabling the agent form is
    # setting E2B_QUOTA_AGENT_URL; without it quota degrades with warnings and
    # the sandbox/volume paths keep working.
```

3c. 删掉整块 `sysctls:`（`:198` 到 `:213`，含 12 行解释性注释与那一条 `- net.ipv4.ip_unprivileged_port_start=0`）。删完后 `volumes: &worker-volumes` 的下一个键就是 `security_opt:`。

> **⚠️ 追加裁定（2026-09-26，同批执行）**：3a–3c 之外**还要**补一行 `E2B_ROUTE_B_TMP_ROOT: /var/lib/e2b-sandboxes/.route-b`（值照抄车队：`deploy/stack/docker-compose.prod.yml:239`，`deploy/k8s/worker.yaml:258-259` 同值）。本示例的 worker env 从来没有这个键，而镜像里有 F1 的 file-capability brokers ⇒ `configure_priv_helpers` 拿默认 `/tmp/sandlock-route-b`（在 broker 白名单外）按名字拒绝 ⇒ `up -d --build` 出来的 worker **启动即 exit，三个都 `Restarting (1)`** —— 这是 N39 在 ① 的同一根因（N39 原先只记了池那一处）。补上后实测三 worker `Up` + `PROD EXAMPLE NETNS SHAPE OK`；钉子照池那一处的形状写（解析车队清单取值再 `==`，不是把字面量抄两遍），见 `test_compose_prod_worker_env_carries_the_fleets_route_b_root`。

**行为差异（受众＝本地/单机生产示例的运维与 SDK 用户）**

- 形态从"单 uid userns + 共享 netns"变成"单 uid userns + 每沙箱 netns"：沙箱内 `ip addr` 从能看到 worker 的 `eth0` 变成只见 `lo`；**不能再从宿主机直连沙箱端口**（走网关 `/mcp` 或 50005+ 映射）。
- 出网全部经 supervisor 注入（建连 p50 +0.25 ms 量级；非阻塞 `connect_ex()` 从 `EINPROGRESS` 变 `0 OK`）。
- `docker inspect` 的 `HostConfig.Sysctls` 变 `null`；容器 `CapEff` 仍为 0。
- **注意这一格在仓库里证据最薄**：车队证据是 root/cap_SETUID worker + per-sandbox uid 开，而这里 65534 且 `:137-146` 明说 per-sandbox uid 会被自动关掉。**⚠️ 2026-09-26 更正（原句是错的）**：这里原写"Task 1 的 phase 2（uid 65534）就是为这一格准备的实测；phase 2 不绿就别合这个 Task" —— Task 1 报告 §7.3 实测写明 phase 2 的选择集（5 个 sandlock/route-B 文件）**不含** netns 契约，"phase 2 绿"只等于"把成对开关作为 worker 默认值塞进去、无特权相位仍全绿"。本格真正的两条实测通道（本任务都已跑）：① Task 1 建立的 `NETNS_ENV` 透传（`deploy/scripts/test-prod-shaped.sh` 把成对开关交给两个相位，整档跑在部署形态的默认值上，`1795 passed / 0 failed` + phase 2 `57 / 1 skipped / 0 failed`）；② **本格的容器级实测** —— `docker compose -f deploy/compose/docker-compose.prod.yml up -d --build` 三 worker `Up`、`docker inspect` 三份 `sysctls=null`、SDK 探针 `IFACES=["lo"]` ⇒ `PROD EXAMPLE NETNS SHAPE OK`，wildcard `allowOut` 下沙箱 `/etc/resolv.conf` 是 `nameserver 127.0.0.2`（网关的 `:53` 绑在沙箱自己的 netns 里）、DNS 解析与 `CONNECT-OK` 都成立。合并门槛随之改为**这两条**（无特权相位仍全绿 + 本格容器级实测绿）。
- 回滚代价（Global Constraints 里已记）：成对设 `false` 即回到共享 netns，此时 wildcard `allowOut` 要把窗口加回来。`deploy/compose/.env.example` 里**没有**这两个键（已核实），所以本地 `.env` 不会意外覆盖 `:-true`。

- [ ] **Step 4: 跑测试确认通过，再跑渲染 + 容器事实 + 业务冒烟（本机 Docker/OrbStack）**

Run: `tmp/testenv/bin/python -m pytest tests/unit/test_worker_manifest_permissions.py -q -p no:cacheprovider`

Expected: 全部 passed（原 12 条 + 新 1 条），`0 failed`

Run（渲染，不需要起栈）：

```bash
docker compose -f deploy/compose/docker-compose.prod.yml config | rg -n "sysctls|NET_ISOLATION|FD_INJECT"
```

Expected: **没有** `sysctls` 行；`E2B_ENABLE_NET_ISOLATION: "true"` 与 `E2B_FD_INJECT_CONNECT: "true"` 各出现 **3 次**（worker-1/2/3 各自继承 anchor）。

Run（起栈 + 容器事实）：

```bash
docker compose -f deploy/compose/docker-compose.prod.yml up -d --build
for s in worker-1 worker-2 worker-3; do
  docker inspect "$(docker compose -f deploy/compose/docker-compose.prod.yml ps -q $s)" \
    --format '{{.Name}} sysctls={{json .HostConfig.Sysctls}}'
done | tee tmp/netns-unify-compose-prod.txt
```

Expected: 三行都是 `sysctls=null`。

Run（启动自检）：

```bash
docker compose -f deploy/compose/docker-compose.prod.yml logs worker-1 \
  | rg "NET_ISOLATION_PAIRING_ERROR|seccomp self-check"
```

Expected: 无 `NET_ISOLATION_PAIRING_ERROR`；有 `seccomp self-check: filter mode active, user namespaces allowed`。

Run（业务冒烟，脚本自带断言；按 `deploy/compose/.env.example:60-70` 的提示把 `E2B_NODE_*` 调大，否则会 `503 No resources available`）：

```bash
E2B_API_URL=http://127.0.0.1:3000 E2B_SANDBOX_URL=http://127.0.0.1:3000 E2B_API_KEY=local-key \
  python deploy/scripts/deployment_smoke.py
```

Expected: 逐段 `OK:` 输出，退出码 0（无 assert 失败）。

> **⚠️ 2026-09-26 实测（Task 3）**：按上面把 `E2B_NODE_*` 调大（`4096 / 400 / 8192 / 1024`，`.env.example:60-70` 的处方；默认 `E2B_NODE_PROCESSES=256` 时一个 worker 只放得下一个沙箱，第 2 段的 migrate 会 `503 Node worker-1 has no capacity`）后，第 1–4 段全绿（`OK: commands + files through gateway` / `migrated worker-1 -> worker-3, files kept` / `OK: network config echo + atomic update` / `OK: volume mounted remotely + sibling volume isolated`）。**第 5 段（template 构建）在本示例里必然失败**，原因与形态无关：`deploy/compose/docker-compose.prod.yml` 不起 buildkitd，控制面于是报 `dial unix /run/buildkit/buildkitd.sock: connect: no such file or directory` ⇒ 这条 Expected 应改成"第 1–4 段逐段 `OK:`；第 5 段需要额外起 buildkit（本示例没有）"。证据：`.superpowers/sdd/netns-task-3-report.md`、`tmp/netns-task3-deployment-smoke*.log`、`tmp/netns-task3-template-build-status.log`。

Run（形态证据：沙箱只见 lo）：

```bash
E2B_SANDBOX_URL=http://127.0.0.1:3000 E2B_API_KEY=local-key python - <<'PY'
import os
from e2b import Sandbox
sbx = Sandbox.create(sandbox_url=os.environ["E2B_SANDBOX_URL"], api_key=os.environ["E2B_API_KEY"])
try:
    out = sbx.commands.run("ip -o addr | awk '{print $2}'").stdout
    assert out.strip() == "lo:", out
finally:
    sbx.kill()
print("PROD EXAMPLE NETNS SHAPE OK")
PY
```

Expected: `PROD EXAMPLE NETNS SHAPE OK`。

- [ ] **Step 5: 提交**

```bash
git add deploy/compose/docker-compose.prod.yml tests/unit/test_worker_manifest_permissions.py
git commit -m "feat(compose): the prod example runs the fleet netns shape"
```

---

### Task 4: ④ `deploy/compose/docker-compose.multinode.yml` 切车队形态（这一处是修 bug）

**Files:**
- Modify: `deploy/compose/docker-compose.multinode.yml:104-117` / `:139-152` / `:174-187`（三份 env 各加两行）、`:120-127` / `:155-162` / `:190-197`（三处注释重写）
- Modify: `tests/unit/test_worker_manifest_permissions.py`（常量区加 `COMPOSE_MULTINODE`，文件末尾加一条测试）

**Interfaces:**
- Consumes: Task 3 引入的 `COMPOSE_PROD` 常量所在的同一常量区。
- Produces: 常量 `COMPOSE_MULTINODE` 与 `test_multinode_example_runs_the_fleet_netns_shape()`；一条可复现的"切前 EACCES / 切后成功"日志（`tmp/netns-unify-wildcard-*.log`）。

注意：这个文件**没有 anchor**（三份 env 是复制粘贴的），所以测试要按服务名切块，不能照抄 stack 的 `&worker` 切片。

- [ ] **Step 1: 写会失败的测试**

常量区加：

```python
COMPOSE_MULTINODE = (
    REPO / "deploy" / "compose" / "docker-compose.multinode.yml"
).read_text(encoding="utf-8")
```

文件末尾加：

```python
def _multinode_worker_block(name: str) -> str:
    tail = COMPOSE_MULTINODE.split(f"\n  {name}:", 1)[1]
    for marker in ("\n  worker-1:", "\n  worker-2:", "\n  worker-3:", "\nvolumes:"):
        if marker in tail:
            tail = tail.split(marker, 1)[0]
    return tail


def test_multinode_example_runs_the_fleet_netns_shape() -> None:
    """N36's fourth site: the file that was broken the other way round.

    `deploy/compose/docker-compose.multinode.yml` ran the shared-netns shape
    *and* declared no low-port window, so its own comment admitted wildcard
    `allowOut` rules would fail with `bind DNS gateway: Permission denied
    (os error 13)` (docs/open-issues.md N36). Aligning it with the fleet both
    removes the window question and fixes the wildcard path. There is no
    anchor in this file: each of the three workers repeats its whole env block,
    so all three are asserted separately.
    """
    assert "\n    sysctls:\n" not in COMPOSE_MULTINODE
    assert "\n      - net.ipv4.ip_unprivileged_port_start=0\n" not in COMPOSE_MULTINODE
    for name in ("worker-1", "worker-2", "worker-3"):
        block = _multinode_worker_block(name)
        assert '\n      E2B_ENABLE_NET_ISOLATION: "true"\n' in block, name
        assert '\n      E2B_FD_INJECT_CONNECT: "true"\n' in block, name
        assert f"\n      E2B_NODE_ID: {name}\n" in block, name
```

- [ ] **Step 2: 跑它，确认失败**

Run: `tmp/testenv/bin/python -m pytest "tests/unit/test_worker_manifest_permissions.py::test_multinode_example_runs_the_fleet_netns_shape" -q -p no:cacheprovider`

Expected: FAIL with `assert '\n      E2B_ENABLE_NET_ISOLATION: "true"\n' in block`（第一个 `worker-1` 就红；`:104-117` 的 env 里没有这两条）。

- [ ] **Step 3: 最小改动**

3a. 三份 env 块各加两行（保持 6 空格缩进）—— 插在每份 env 的最后一条 `E2B_NODE_PROCESSES: 256` 之后：

```yaml
      # N36 (2026-09-26): the fleet shape, so wildcard allowOut works here.
      # Each sandbox runs its own loopback-only netns and egress goes through
      # the supervisor's connect-fd injection; the wildcard-DNS `:53` bind then
      # happens inside the sandbox's own netns (root-in-userns covers port 53),
      # which is why this file declares no low-port window. BOTH switches are
      # required: `create_app` refuses the unpaired shape by name
      # (envd_service/config.py:471-479).
      E2B_ENABLE_NET_ISOLATION: "true"
      E2B_FD_INJECT_CONNECT: "true"
```

三处插入点：`worker-1` 在 `:117` 之后、`worker-2` 在 `:152` 之后、`worker-3` 在 `:187` 之后。

3b. 三处注释（`:120-127` / `:155-162` / `:190-197`）整段替换为（各自保持 4 空格缩进）：

```yaml
    # A6: no SYS_ADMIN (shared-volume bind deleted in A4; quota via
    # E2B_QUOTA_AGENT_URL + quota-agent). The file runs the fleet shape:
    # per-sandbox netns, so wildcard allowOut rules resolve through the
    # sandbox's own DNS gateway binding 127.0.1.x:53 inside its own netns --
    # no low-port window is declared, and none is needed (as in
    # deploy/stack/docker-compose.prod.yml and deploy/compose/docker-compose.prod.yml).
```

**行为差异（受众＝本地多节点拓扑验证）**

- 沙箱内只见 `lo`、egress 经 supervisor 注入、入站走 50005+（与 Task 3 相同）。
- **净效果是把"用通配 `allowOut` 必 EACCES"变成"能用"** —— 现状是坏的，所以这一处是修 bug 而不是引入回归；这是四处里唯一"修 bug"性质的一处。
- 回滚没有意义：成对 `false` 就是回到今天这个文件的状态（无 netns、无窗口、通配规则 EACCES）。若真要在 ④ 上跑共享 netns，必须同时把 Task 3 删掉的那段 `sysctls:` 抄回来。

- [ ] **Step 4: 跑测试确认通过，再跑渲染 + 多节点冒烟 + 通配规则探针（本机 Docker/OrbStack）**

Run: `tmp/testenv/bin/python -m pytest tests/unit/test_worker_manifest_permissions.py -q -p no:cacheprovider`

Expected: 全部 passed（Task 3 后 13 条 + 新 1 条 = 14 条），`0 failed`

Run:

```bash
docker compose -f deploy/compose/docker-compose.multinode.yml config | rg -n "NET_ISOLATION|FD_INJECT|sysctls"
```

Expected: 每个 worker 各两条 = `"true"`（共 6 行），无 `sysctls`。

Run（多节点冒烟；按 `deploy/compose/.env.example:60-70` 调大 `E2B_NODE_*`，本脚本创建 4 个沙箱并要求跨 ≥2 个节点）：

```bash
docker compose -f deploy/compose/docker-compose.multinode.yml up -d
E2B_API_URL=http://127.0.0.1:3100 E2B_SANDBOX_URL=http://127.0.0.1:3100 E2B_API_KEY=local-key \
  python deploy/scripts/multinode_smoke.py
```

Expected: `NODE DISTRIBUTION:` 里至少 2 个地址（多节点冒烟脚本 `deploy/scripts/multinode_smoke.py:47`，同理 `deployment_smoke.py:62`），最后一行 `MULTI-NODE SMOKE OK`（`:78`）。

Run（通配规则探针 —— ④ 的真正卖点；**切前切后各跑一次**。拿基线时先 `git stash push deploy/compose/docker-compose.multinode.yml` 或 `git stash` 本 Task 的改动）：

```bash
docker compose -f deploy/compose/docker-compose.multinode.yml logs --since 10m worker-1 worker-2 worker-3 \
  | rg "bind DNS gateway" > tmp/netns-unify-wildcard-before.log || true
E2B_SANDBOX_URL=http://127.0.0.1:3100 E2B_API_KEY=local-key python - <<'PY'
import os
from e2b import Sandbox
opts = {"sandbox_url": os.environ["E2B_SANDBOX_URL"], "api_key": os.environ["E2B_API_KEY"]}
sbx = Sandbox.create(network={"allow_out": ["*.example.com"]}, **opts)
try:
    r = sbx.commands.run(
        "python3 -c \"import socket;print(socket.getaddrinfo('sub.example.com',443)[0][4][0])\""
    )
    print("WILDCARD RESOLVED:", r.stdout.strip(), "rc=", r.exit_code)
finally:
    sbx.kill()
PY
```

Expected:

- 切前：`tmp/netns-unify-wildcard-before.log` 命中 `bind DNS gateway: Permission denied (os error 13)`（或在沙箱内看到 `EAI_AGAIN` 解析失败）—— 这就是 N36 记的"未实测"，**第一次把它实测出来**。
- 切后：同一条命令在 worker 日志里**无** `bind DNS gateway` 命中，且打印 `WILDCARD RESOLVED: <合成 IP> rc= 0`（README 记：通配子域被解析成合成 IP，connect 由 supervisor 代连并二次校验）。

- [ ] **Step 5: 提交**

```bash
git add deploy/compose/docker-compose.multinode.yml tests/unit/test_worker_manifest_permissions.py
git commit -m "fix(compose): the multinode example can use wildcard allowOut"
```

---

### Task 5: 残留漂移（两处**已经写错**的注释 + 两份会误导人的 docstring/env 注释）

**Files:**
- Modify: `deploy/k8s/worker.yaml:553-556`、`:568-571`
- Modify: `deploy/stack/.env.example:193-200`
- Modify: `tests/security/test_worker_nonroot.py:7-9`
- Modify: `tests/unit/test_network_config.py:436-450`、`:541-550`（docstring 措辞）
- Modify: `tests/unit/test_worker_manifest_permissions.py`（两条新钉子 + 一个 `STACK_ENV_EXAMPLE` 常量）

**Interfaces:**
- Consumes: Task 3 的 `COMPOSE_PROD` 常量（本 Task 不再需要新常量，只加 `STACK_ENV_EXAMPLE`）。
- Produces: `STACK_ENV_EXAMPLE` 常量、`test_no_comment_still_claims_a_pod_level_sysctl_exists()`、`test_stack_env_example_describes_the_rollback_lever_not_a_canary()`。

为什么独立成 Task：这三处**今天就与代码矛盾** —— `deploy/k8s/worker.yaml:64-72` 与 `tests/unit/test_worker_manifest_permissions.py:250-261` 都钉着"pod 级 sysctl 已删"，而同文件 `:555` 还写着 "the pod-level sysctl above"、`:570` 写着 "which uses the sysctl above"。与形态改动无关，所以单独提交、单独 review。

- [ ] **Step 1: 写会失败的测试**

在 `tests/unit/test_worker_manifest_permissions.py` 文件末尾加：

```python
STACK_ENV_EXAMPLE = (REPO / "deploy" / "stack" / ".env.example").read_text(
    encoding="utf-8"
)


def test_no_comment_still_claims_a_pod_level_sysctl_exists() -> None:
    """The window was deleted on 2026-09-17 (N5); two comments missed it.

    `deploy/k8s/worker.yaml` explained the `:53` wildcard-DNS gateway and the
    `NET_BIND_SERVICE` cap with "the pod-level sysctl above" and "the sysctl
    above" -- but `:64-72` and `test_no_low_port_window_survives_anywhere`
    both say that block is gone. Pinned so the stale sentence cannot return.
    """
    assert "pod-level sysctl above" not in K8S_WORKER
    assert "which uses the sysctl above" not in K8S_WORKER


def test_stack_env_example_describes_the_rollback_lever_not_a_canary() -> None:
    """`worker-2` is a per-node rollback lever, not a canary (docs §2.4.7).

    The anchor turns the pair on for every worker since 2026-09-16; the
    `*_WORKER2` overrides exist so one node can be taken back alone. The env
    example still said "the canary turns it on for worker-2 only" and shipped
    `false`, which reads as "the fleet default is off" -- the opposite of the
    manifests.
    """
    assert "the canary turns it on for worker-2 only" not in STACK_ENV_EXAMPLE
    assert "E2B_ENABLE_NET_ISOLATION=false" not in STACK_ENV_EXAMPLE
    assert "E2B_FD_INJECT_CONNECT=false" not in STACK_ENV_EXAMPLE
```

- [ ] **Step 2: 跑它，确认失败**

Run: `tmp/testenv/bin/python -m pytest "tests/unit/test_worker_manifest_permissions.py::test_no_comment_still_claims_a_pod_level_sysctl_exists" "tests/unit/test_worker_manifest_permissions.py::test_stack_env_example_describes_the_rollback_lever_not_a_canary" -q -p no:cacheprovider`

Expected: FAIL —— 前者报 `assert 'pod-level sysctl above' not in K8S_WORKER`（`deploy/k8s/worker.yaml:555`）；后者报 `assert 'the canary turns it on for worker-2 only' not in STACK_ENV_EXAMPLE`（`deploy/stack/.env.example:195-196`）。

- [ ] **Step 3: 最小改动**

3a. `deploy/k8s/worker.yaml:553-556` —— 把

```yaml
          # docs/production-deployment-requirements.md §2.4.4 (W4); ③ the :53
          # wildcard-DNS gateway is enabled by the pod-level sysctl above
          # (NET_BIND_SERVICE alone does not cover it for this non-root pod --
          # see the comment there).
```

改成：

```yaml
          # docs/production-deployment-requirements.md §2.4.4 (W4); ③ the :53
          # wildcard-DNS gateway binds inside each sandbox's own netns
          # (`E2B_ENABLE_NET_ISOLATION` is on for this pod), where the guest's
          # root-in-userns CAP_NET_BIND_SERVICE covers port 53 -- the pod-level
          # window was deleted in N5 (2026-09-17) and is not needed.
```

3b. `deploy/k8s/worker.yaml:568-571` —— 把

```yaml
              # NET_BIND_SERVICE stays for a root override (runAsUser: 0); it is
              # inert for the default non-root pod, which uses the sysctl above.
```

改成：

```yaml
              # NET_BIND_SERVICE stays for a root override (runAsUser: 0); it is
              # inert for the default non-root pod (CapEff stays 0), and nothing
              # in this pod binds a low port any more -- the sandbox's own netns
              # covers :53.
```

3c. `deploy/stack/.env.example:193-200` —— 整块替换为：

```dotenv
# Per-sandbox network namespace (net_isolation, S2.2). The manifests default
# BOTH to true (`${E2B_ENABLE_NET_ISOLATION:-true}`,
# deploy/stack/docker-compose.prod.yml:209-210); these two lines are the
# rollback lever, not a canary -- worker-2 additionally carries its own
# `*_WORKER2` overrides so one node can be taken back alone
# (docs/production-deployment-requirements.md §2.4.7). BOTH switches must be
# true: `net_isolation` without `fd_inject_connect` leaves every sandbox
# loopback-only (outbound connects fail inside the kernel and user code only
# sees timeouts), so create_app refuses that pairing by name at startup.
# Setting both to false returns the sandboxes to the shared netns -- and then
# wildcard allowOut needs a low-port window back, which no manifest declares.
E2B_ENABLE_NET_ISOLATION=true
E2B_FD_INJECT_CONNECT=true
```

3d. `tests/security/test_worker_nonroot.py:7-9` —— 把

```python
* with the production security shape (seccomp unconfined, NET_ADMIN, host
  network, low-port sysctl) a non-root supervisor can still create plain,
  network-enabled and image-rootfs (chroot) sandboxes.
```

改成：

```python
* with this probe's privilege shape (seccomp unconfined, NET_ADMIN, host
  network, root-then-setpriv) a non-root supervisor can still create plain,
  network-enabled and image-rootfs (chroot) sandboxes. The probe's
  `docker create` declares no `--sysctl` (`:228-260`): a `--network host`
  container cannot take a net.* sysctl at all, which is why the low-port
  window belongs to a bridge-networked worker instead
  (docs/production-deployment-requirements.md §2.4.3).
```

3e. `tests/unit/test_network_config.py:436-450`（`test_netns_flag_accepted_but_not_passed_through`）与 `:541-550`（`test_wildcard_allowout_accepted_without_netns_flag`）的两处 docstring —— 只改措辞：把 "the default unprivileged shared-netns DNS gateway serves it" 改成 "the fork's unprivileged DNS gateway serves it（部署默认已是 per-sandbox netns；共享 netns 只剩 arm lane）"。断言一行不动（它们不依赖窗口，不会假红）。

- [ ] **Step 4: 跑测试，确认通过**

Run: `tmp/testenv/bin/python -m pytest tests/unit/test_worker_manifest_permissions.py tests/unit/test_network_config.py -q -p no:cacheprovider`

Expected: 全部 passed，`0 failed`

Run（既有钉子必须仍然绿）: `tmp/testenv/bin/python -m pytest "tests/unit/test_worker_manifest_permissions.py::test_no_low_port_window_survives_anywhere" "tests/unit/test_worker_manifest_permissions.py::test_k8s_runs_the_same_namespace_shape_as_the_stack" -q -p no:cacheprovider`

Expected: `2 passed`

`tests/security/test_worker_nonroot.py` 只改 docstring，但它要建镜像：**不要**在本机为它单独跑，交给 Task 6 的容器内全量。

- [ ] **Step 5: 提交**

```bash
git add deploy/k8s/worker.yaml deploy/stack/.env.example tests/security/test_worker_nonroot.py tests/unit/test_network_config.py tests/unit/test_worker_manifest_permissions.py
git commit -m "docs(shape): drop the stale low-port-window comments"
```

---

### Task 6: 文档与账本同步（含 ④ 的"未实测"结案）

**Files:**
- Modify: `docs/production-deployment-requirements.md:184`（§2.4.1 引用句）、`:366`（§2.4.3 追加）、`:534-538`（§2.4.7 顶部追加）、`:575`、`:687`（历史记录加注）—— **注：§2.4.5 已在 Task 1 补了 2026-09-26 更正（新增 `:501-513`，14 行），`:534` 之后的引用行号请按当时文件重新核对**
- Modify: `docs/SCALING.md:301-306`
- Modify: `README.md:374-382`
- Modify: `docs/k8s-deployment.md:166-176`
- Modify: `docs/HANDOFF.md:125`、`:1399-1401`
- Modify: `docs/cross-platform-lanes.md:244`
- Modify: `docs/security-audit/findings.md:66`
- Modify: `docs/task-backlog.md:104`、`docs/open-issues.md:22`
- Modify: `tests/unit/test_worker_manifest_permissions.py`（收口钉子）

**Interfaces:**
- Consumes: Task 1–5 的提交与 `tmp/` 证据（`tmp/netns-unify-lane.log`、`tmp/netns-unify-pool.txt`、`tmp/netns-unify-compose-prod.txt`、`tmp/netns-unify-wildcard-before.log`）。
- Produces: `test_only_the_arm_lane_keeps_a_low_port_window()`（形态是否真的统一的单条判据）、N36 在两份账本里的"已完成（2026-09-26）"状态，以及"仓库里还剩哪些共享 netns 形态"的唯一答案。

- [ ] **Step 1: 写会失败的测试（收口钉子）**

在 `tests/unit/test_worker_manifest_permissions.py` 末尾加：

```python
def test_only_the_arm_lane_keeps_a_low_port_window() -> None:
    """N36 closed 2026-09-26: the window survives in exactly one place.

    The three deployment-ish sites (compose prod example, local pool, compose
    multinode example) were aligned with the fleet; the aarch64 lane's
    `guest-prep.sh` keeps its one-shot window because the Rust suites still run
    the shared-netns shape as uid 501 and cannot drop it (measured; see that
    file's comment and docs/open-issues.md N36).
    """
    lane = (REPO / "deploy" / "scripts" / "arm-lane" / "guest-prep.sh").read_text(
        encoding="utf-8"
    )
    assert "net.ipv4.ip_unprivileged_port_start=0" in lane
    assert "ip_unprivileged_port_start" not in COMPOSE_PROD
    assert "ip_unprivileged_port_start" not in COMPOSE_MULTINODE
    assert "ip_unprivileged_port_start" not in (
        REPO / "autoscaler" / "backends" / "local.py"
    ).read_text(encoding="utf-8")
    # The k8s pod-level window is gone (N5) and must stay gone.
    assert POD_SYSCTL not in K8S_WORKER
```

- [ ] **Step 2: 跑它，确认它的行为符合预期**

Run: `tmp/testenv/bin/python -m pytest "tests/unit/test_worker_manifest_permissions.py::test_only_the_arm_lane_keeps_a_low_port_window" -q -p no:cacheprovider`

Expected: Tasks 2–4 已合 ⇒ 此条 **PASS**（它是收口钉子）；若在 Tasks 2–4 之前跑 ⇒ FAIL with `assert 'ip_unprivileged_port_start' not in COMPOSE_PROD`。两种结果都说明钉子有效：把它当作"形态是否真的统一"的单条判据。

- [ ] **Step 3: 最小改动（逐文件给出替换/追加文本）**

3a. `docs/production-deployment-requirements.md:184`（A6 的 SYS_ADMIN 表行，第 ③ 处用途的括注）—— 在"compose `sysctls:` / `docker --sysctl`"之后补一句"（2026-09-26 起 `deploy/compose` 的 prod/multinode 示例与本地池已不再声明窗口，见 §2.4.3 末）"。

3b. §2.4.3 末尾（现 `:366` "MCP 入站端口是 50005+，从来不需要低端口窗口。" 之后）追加：

```markdown
  **2026-09-26：仓库里带低端口窗口的位置只剩一处** —— aarch64 lane 的
  `deploy/scripts/arm-lane/guest-prep.sh:39-64`（Rust 套件默认共享 netns、以 uid 501 跑，
  实测删不掉）。同日 `deploy/compose/docker-compose.prod.yml`、
  `deploy/compose/docker-compose.multinode.yml` 与本地池 `autoscaler/backends/local.py`
  已与车队对齐（成对打开 `E2B_ENABLE_NET_ISOLATION` + `E2B_FD_INJECT_CONNECT`、删除窗口），
  落地与验证见 `docs/open-issues.md` N36 行。
```

3c. §2.4.7 顶部结果段（现 `:534-538` 那段 "**2026-09-16 全量**" 之后）追加一句：

```markdown
  2026-09-26：`deploy/compose` 的两个本地示例与 `autoscaler/backends/local.py` 也切到同一
  形态（见 §2.4.3 末尾），仓库里只剩 arm lane 的 Rust 套件还跑共享 netns。
```

3d. `:575` 与 `:687` —— 两句都是已被取代的叙述：`:575`（"再切 worker-1 并撤掉容器级 `ip_unprivileged_port_start=0`"，在"退出到全量的判据"段）在 §2.4.7 顶部全量结果之前；`:687`（"因此：worker-1 保持共享 netns，`ip_unprivileged_port_start=0` 不撤"）在 `:543` 起的"以下为灰度期的记录，保留作追溯"段里。**不要删历史**，就在这两处就地加前缀注记，避免下一个人照它回退：

```markdown
**（历史记录，已被本节顶部的全量结果取代：两个 worker 都跑 per-sandbox netns，容器级
`ip_unprivileged_port_start=0` 已于 2026-09-16 撤掉。）**
```

3e. `docs/SCALING.md:303-306` —— 把

```markdown
- 运行参数与现状一致：`seccomp=unconfined`、
  `--sysctl net.ipv4.ip_unprivileged_port_start=0`（容器 spec 声明，A6 之后
  不再需要 `--cap-add SYS_ADMIN`：共享卷 bind 已删、配额走 quota-agent）、
  共享 workspace 卷挂载；
```

改成：

```markdown
- 运行参数与现状一致：`seccomp=unconfined`、
  `E2B_ENABLE_NET_ISOLATION=true` + `E2B_FD_INJECT_CONNECT=true`（与车队同形态，
  每沙箱自有 netns；A6 之后不再需要 `--cap-add SYS_ADMIN`：共享卷 bind 已删、
  配额走 quota-agent，低端口窗口也一并撤掉了 —— 通配 DNS 的 `:53` 绑在沙箱自己的
  netns 内，由 userns root 覆盖）、共享 workspace 卷挂载；
```

3f. `README.md:374-382` —— 把"共享 netns 形态下一次 `net.ipv4.ip_unprivileged_port_start=0` 即可"改成"仓库里只剩 arm lane 的 Rust 套件还跑共享 netns（`deploy/scripts/arm-lane/guest-prep.sh`），那里的窗口是一次性的、实测必需；部署形态（stack / k8s / `deploy/compose` 的两个示例 / 本地池）都不需要窗口"。

3g. `docs/k8s-deployment.md:166-176` —— §5 里 "netns（N5）" 那段末尾补一句（现文只提 compose stack）：

```markdown
同一形态也是 `deploy/compose/docker-compose.prod.yml`、
`deploy/compose/docker-compose.multinode.yml` 与本地池 `autoscaler/backends/local.py`
的形态（2026-09-26 统一），四处的低端口窗口都因此不再需要。
```

3h. `docs/HANDOFF.md:125`（A6 终态表第 ③ 行）与 `docs/HANDOFF.md:1399-1401` —— 后者原文"生产 worker 用桥接网络，compose `sysctls` 生效（deploy/compose/docker-compose.prod.yml 已加）"要**反过来写**：

```markdown
  生产 worker 用桥接网络，但那套 compose 示例 2026-09-26 起也跑 per-sandbox netns、
  不再声明 `sysctls`（低端口 `:53` 绑在沙箱自己的 netns 内）；`sysctl` 声明这条路现在
  只为 aarch64 lane 的共享 netns 套件保留（`deploy/scripts/arm-lane/guest-prep.sh`）。
```

`:125` 那行把"（compose `sysctls:`；k8s 见本文件顶部 ⚡ 块 fix-1：**pod 级** `securityContext.sysctls`，非 root pod 靠 `NET_BIND_SERVICE` 不够）"改成"（compose/k8s 的**部署形态**已不需要它：2026-09-16 compose 撤、2026-09-17 k8s（N5）撤；仅 arm lane 的共享 netns 套件仍靠 `guest-prep.sh` 写一次）"。

3i. `docs/cross-platform-lanes.md:244` —— 该行的"lane 保留 `ip_unprivileged_port_start=0`"后面加一句："**仅这条 arm lane**，不代表任何部署形态（stack/k8s/compose 示例/本地池都不需要窗口）。"

3j. `docs/security-audit/findings.md:66` —— 那句"netns 形态（`E2B_ENABLE_NET_ISOLATION=true`，compose stack 默认）复跑同一探针"后面补："（2026-09-26 起 `deploy/compose` 的 prod/multinode 示例与本地池 `autoscaler/backends/local.py` 也是同一形态）"。

3k. `docs/open-issues.md:22` —— 状态列 `**待决策**` 改成 `**已完成（2026-09-26）**`，"下一步"列把实测结论写实（条数与文件名按你的实际日志填，下表为模板）：

```markdown
**已完成（2026-09-26）**：①②④ 已与车队对齐（成对打开 `E2B_ENABLE_NET_ISOLATION`+`E2B_FD_INJECT_CONNECT`、删除低端口窗口）；③ arm lane **保留**（lane-only 的共享 netns 覆盖）。验收：`deploy/scripts/test-prod-shaped.sh` 的 netns 形态两相位全绿（`0 failed`；phase 2 的选择集**不含** netns 契约，"65534 这一格"的形态证据是 ① 的容器级实测：三 worker `Up` + `IFACES=["lo"]`）、池与 ① 的 `docker inspect sysctls=null`、`deployment_smoke.py` 第 1–4 段全绿（第 5 段的 template 构建需要额外的 buildkitd，`deploy/compose` 示例不起它）/ `multinode_smoke.py` 全绿、④ 的通配规则从 `bind DNS gateway: Permission denied (os error 13)` 变可解析。证据：`tmp/netns-unify-*.log|txt`、`tmp/netns-task3-*.log|txt`。
```

3l. `docs/task-backlog.md:104` —— 把 `**待决策**：…` 整格替换成同口径的一句："**已完成（2026-09-26）**：①②④ 与车队对齐（成对开关 + 删窗口），③ 保留（lane-only，实测必需）；验收见 `docs/open-issues.md` N36 行。"

- [ ] **Step 4: 跑测试，确认通过 + 全量收尾**

Run: `tmp/testenv/bin/python -m pytest "tests/unit/test_worker_manifest_permissions.py::test_only_the_arm_lane_keeps_a_low_port_window" -q -p no:cacheprovider`

Expected: `1 passed`

Run（本机全量 unit，收尾判据）：`tmp/testenv/bin/python -m pytest tests/unit -q -p no:cacheprovider`

Expected: `0 failed`（macOS 上既有的 16 条红是环境性已知项，见 `docs/open-issues.md` F11 行记的"本机 unit 16 红（既有）"；**不得新增红**）

Run（形态全量，**本机 Docker/OrbStack**，与 Task 1 同一条命令，证明补完文档没改坏形态）：

```bash
E2B_REGISTRY_MIRRORS=registry-1.docker.io=127.0.0.1:5080 \
E2B_ENABLE_NET_ISOLATION=true E2B_FD_INJECT_CONNECT=true \
  ./deploy/scripts/test-prod-shaped.sh 2>&1 | tee tmp/netns-unify-lane-final.log
```

Expected: 两相位都 `0 failed`

Run（arm lane 回归，**可选但推荐**，只读性质；证明 ③ 仍然可用）：

```bash
deploy/scripts/fork-gate.sh
```

Expected: 与既有基线一致（`third_party/sandlock/docs/test-baseline.md` 记的 core_integ 551/0），窗口仍在、`pidfd_getfd` 那条坑不出现。

- [ ] **Step 5: 提交**

```bash
git add docs/production-deployment-requirements.md docs/SCALING.md README.md docs/k8s-deployment.md docs/HANDOFF.md docs/cross-platform-lanes.md docs/security-audit/findings.md docs/task-backlog.md docs/open-issues.md tests/unit/test_worker_manifest_permissions.py
git commit -m "docs(n36): the low-port window survives only in the arm lane"
```

---

## Self-Review（写完后逐条对照过）

1. **用户决定的覆盖面**：① 的删窗口 + 成对开关 = Task 3；② = Task 2；④ = Task 4；③ 保留 = Global Constraints 明写"不动"，Task 6 只改文档措辞，并新增钉子 `test_only_the_arm_lane_keeps_a_low_port_window` 把它钉成"仓库里唯一带窗口的地方"。
2. **零占位符**：每个 Step 都有真代码/真命令/真期望输出；两处带不确定性的期望（Task 1 的条数、Task 4 的通配探针）都给了**判据 + 与基线的对照方式 + 落盘文件名**，没有任何"以后再补"式的句子，也没有跨 Task 的偷懒引用（每个 Task 的代码块都是完整可抄的）。
3. **与草稿的实质性修正**（详见最终报告）：`local.py` 的行号实际是 `:48-54`（草稿写 44–53）；phase 2 **确实**设置 `E2B_BASE_IMAGE`（`PHASE2_BASE_IMAGE:-python:3.11-slim`，`:212`），只是不继承 phase 1 的 `python-mcp:3.14`；`test_no_low_port_window_survives_anywhere` 只扫 stack + k8s 两个文件（不是全仓库）；草稿漏了 `docs/production-deployment-requirements.md:184/:575/:687` 与 `docs/build-test-deploy-pitfalls.md:205` 这几处引用；`deploy/compose/.env.example` 里根本没有 netns 键（回滚靠手写 `.env`，不是改已有键）。
4. **类型/命名一致性**：`NETNS_ENV`（Task 1）被 Tasks 2–4 的验证命令间接使用（都用 `E2B_ENABLE_NET_ISOLATION=true E2B_FD_INJECT_CONNECT=true` 调 lane）；`COMPOSE_PROD`（Task 3）被 Task 6 的收口钉子复用；`COMPOSE_MULTINODE` 同；`STACK_ENV_EXAMPLE`（Task 5）只在 Task 5 用；`_multinode_worker_block`（Task 4）只在本文件用。
5. **串行约束**：Tasks 3/4/5/6 都动 `tests/unit/test_worker_manifest_permissions.py`，Global Constraints 已要求串行（或 rebase）执行。
