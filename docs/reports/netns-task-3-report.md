# Task 3 报告：① `deploy/compose/docker-compose.prod.yml` 切车队形态

日期：2026-09-26 ｜ 工作目录：`/Users/polus/project/ai/sandlock-e2b`
简报：`.superpowers/sdd/netns-task-3-brief.md`（= `docs/superpowers/plans/2026-09-26-netns-shape-unification.md` 的 Task 3）
HEAD（本次改动前）：`f03c462`（跑到一半时另一 agent 落了 `fd81f26`）
本次提交：**`28c7f70`** = `feat(compose): the prod example runs the fleet netns shape`（精确两个文件）

---

## 0. 结论

简报 **Step 1–3 逐字落地**，Step 4 里"单测 + 渲染"两段**全绿且与期望值逐个相符**：

- 新测 RED 的点名与简报一致（`assert '\n    sysctls:\n' not in COMPOSE_PROD`）；
- GREEN：本模块 `27 passed`、`docker compose config` 渲染里**没有 `sysctls`**、两个开关各 **3 次**；
- 容器事实：三个 worker `sysctls=null`、`CapAdd=null`、`user=65534:65534`、`CapEff=0`；日志无
  `NET_ISOLATION_PAIRING_ERROR`、有 `seccomp self-check: filter mode active, user namespaces allowed`。

**Step 4 的"业务冒烟 + 沙箱只见 `lo`"两条拿不到**，原因**不是**本次改动，而是 ① 这一格带着与
Task 2 同类的**既有缺陷（N39 的机制，N39 当时只记了池）**：

> `deploy/compose/docker-compose.prod.yml` 的 worker env **没有** `E2B_ROUTE_B_TMP_ROOT`。
> 按本文件头部第 7 行自己的用法 `up -d --build`（= 从工作树构建 worker 镜像，镜像内**有** F1 的
> file-capability brokers）起栈时，三个 worker 一律 `Restarting (1)`，日志是
> `envd_service.priv_helpers.PrivHelperError: route-B scratch root /tmp/sandlock-route-b is outside the
> privileged helper roots (/var/lib/e2b-sandboxes)`。改前形状（无成对开关、未删窗口）直接 `docker run`
> 同一镜像得到**同一份 traceback** ⇒ 与本次改动无关（§4.2 原始输出）。

用 **tmp-only** override 补上车队那个值（`deploy/stack/docker-compose.prod.yml:239`）后实测：
三个 worker `Up`、无配对错误、SDK 探针 `IFACES=["lo"]`、`MATH=2` ⇒ **`PROD EXAMPLE NETNS SHAPE OK`**。
也就是说**只差这一行**（§4.3）。这行属于 N39 的补丁面，简报 3a–3c 没写，**我没有擅自加**，停下问（§8.1）。

---

## 1. 改了什么

| 文件 | 动作 |
|---|---|
| `deploy/compose/docker-compose.prod.yml` | +23 / −22：legacy 注释改写 + 成对开关插入（3a）；A6 注释改写（3b）；`sysctls:` 整块删除（3c，16 行 = 1 行声明 + 14 行解释 + 1 行值） |
| `tests/unit/test_worker_manifest_permissions.py` | +31 / −0：常量 `COMPOSE_PROD`（紧随 `STACK_COMPOSE`）+ 文件末尾新增 `test_compose_prod_example_runs_the_fleet_netns_shape()` |

落地位置（按**内容**定位，不照简报行号）：`worker-1: &worker` 在 `:130`、`worker-2:` 在 `:230`，两份锚点都在，
切片技巧与 stack 断言同源。**没有** executor 门控问题：这是 compose 的 worker service，直接给 env，
`enable_net_isolation` 的读取点在 `envd_service/executors/sandlock.py`，env 直给即生效（§4.3 实测确认）。

新测的断言覆盖三种半迁移：①窗口回来（`sysctls:` / 那条值不得出现）、②只开一个开关（成对两行必须都在 anchor 里）、
③退回 `seccomp=unconfined`（必须是 shipped profile）。全部**精确匹配**，无 `in` 之外的模糊比较、无 skip。

---

## 2. Step 2：RED 原始输出

`tmp/testenv/bin/python -m pytest "tests/unit/test_worker_manifest_permissions.py::test_compose_prod_example_runs_the_fleet_netns_shape" -q -p no:cacheprovider`
（完整留证 `tmp/netns-task3-red.log`）

```
ENV-HEADER commit=f03c462 date=2026-09-26T12:10:54+08:00 cmd=pytest-task3-red
F                                                                        [100%]
=================================== FAILURES ===================================
_____________ test_compose_prod_example_runs_the_fleet_netns_shape _____________

    def test_compose_prod_example_runs_the_fleet_netns_shape() -> None:
        """N36: the single-host example follows the fleet, window and all.
    ...
        worker = COMPOSE_PROD.split("\n  worker-1: &worker", 1)[1].split(
            "\n  worker-2:", 1
        )[0]
        # The directive, not the prose: the comment above the line names the old
        # value on purpose.
>       assert "\n    sysctls:\n" not in COMPOSE_PROD
E       AssertionError: assert '\n    sysctls:\n' not in '# Productio...stry-data:\n'
E
E         '\n    sysctls:\n' is contained here:
E           p working.
E               sysctls:
E         ? ----------
E                 # The wildcard DNS gateway binds 127.0.1.x:53; resolv.conf cannot carry
E                 # a port, so allow unprivileged low-port binding (namespaced per...
E
E         ...Full output truncated (58 lines hidden), use '-vv' to show

tests/unit/test_worker_manifest_permissions.py:797: AssertionError
=========================== short test summary info ============================
FAILED tests/unit/test_worker_manifest_permissions.py::test_compose_prod_example_runs_the_fleet_netns_shape
1 failed in 0.15s
PYTEST-EXIT=1
```

失败点与简报 Step 2 的"逐条期望"逐字一致（第一条就点名 `sysctls:`）。

---

## 3. Step 4（前半）：GREEN 原始输出

### 3.1 新测 + 整个模块（提交后复跑，`28c7f70`）

```
ENV-HEADER commit=28c7f70 date=2026-09-26T12:17:08+08:00 cmd=pytest-task3-green-post-commit
...........................                                              [100%]
27 passed in 0.43s
PYTEST-EXIT=0
--- the new test alone ---
.                                                                        [100%]
1 passed in 0.05s
PYTEST-EXIT=0
```

（`tmp/netns-task3-green-unit.log`、`tmp/netns-task3-green-post-commit.log`。简报写"原 12 条 + 新 1 条"，
本模块现已 27 条 —— 期间其它任务加过用例。）

### 3.2 渲染（不需要起栈）

```
$ WORKER_IMAGE=e2b-local/e2b-sandlock-worker:netns-task3 docker compose \
    -f deploy/compose/docker-compose.prod.yml config | rg -n "sysctls|NET_ISOLATION|FD_INJECT"
132:      E2B_ENABLE_NET_ISOLATION: "true"
135:      E2B_FD_INJECT_CONNECT: "true"
182:      E2B_ENABLE_NET_ISOLATION: "true"
185:      E2B_FD_INJECT_CONNECT: "true"
232:      E2B_ENABLE_NET_ISOLATION: "true"
235:      E2B_FD_INJECT_CONNECT: "true"
RG-EXIT=0
--- counts (expect: 3 / 3 / no sysctls line) ---
3
3
sysctls count=0
```

⇒ **没有 `sysctls` 行；两个键各 3 次**（worker-1/2/3 各自继承 anchor），与简报 Step 4 的期望逐个相符。
留证 `tmp/netns-task3-compose-config.log`。（`deploy/compose/.env` 不存在，所以这次渲染就是出厂默认。）

### 3.3 全量单测（回归，不是本任务的门）

```
tmp/testenv/bin/python -m pytest tests/unit -q -p no:cacheprovider
... 14 failed, 1313 passed, 11 skipped, 2 warnings in 56.72s
```

14 条红是**已知的 Linux-only 名单**，与 `pure-task-4-report.md:83` 记的口径逐条同名：
`test_priv_helpers.py` 11 条 + `test_real_root_gate.py` 1 条 + `test_xfs_quotactl_backend.py` 2 条。
passed 数 1313 > 派单里写的 1288（那份基线已过期，`progress.md:2067` 自己更正过），且这次运行还捎带了
工作区里**他人未提交**的改动（`envd_service/executors/sandlock.py`、`test_pause_quota.py`、
`test_pure_rootfs_shape.py`、`test_tenant_quota.py`、新增 `tests/unit/conftest.py`）。
留证：`tmp/netns-task3-unit-full.log`（本轮 `pytest ... tests/unit` 的输出，含 14 条 FAILED 名单）。

---

## 4. Step 4（后半）：实跑（本机 Docker/OrbStack）

### 4.1 简报命令的产出：容器事实对、但 worker 起不来

```
$ WORKER_IMAGE=e2b-local/e2b-sandlock-worker:netns-task3 CONTROL_PLANE_PORT=3900 \
    docker compose -f deploy/compose/docker-compose.prod.yml up -d --build
...
 Container compose-worker-1-1 Started
 Container compose-worker-2-1 Started
 Container compose-worker-3-1 Started
COMPOSE-EXIT=0

$ for s in worker-1 worker-2 worker-3; do docker inspect "$(... ps -q $s)" \
    --format '{{.Name}} sysctls={{json .HostConfig.Sysctls}}'; done
/compose-worker-1-1 sysctls=null
/compose-worker-2-1 sysctls=null
/compose-worker-3-1 sysctls=null

$ ... docker compose ps
compose-worker-1-1  ...  Restarting (1) 5 seconds ago
compose-worker-2-1  ...  Restarting (1) 5 seconds ago
compose-worker-3-1  ...  Restarting (1) 5 seconds ago
```

⇒ `sysctls=null`（**不是** `{"net.ipv4.ip_unprivileged_port_start":"0"}`）✅，但三个 worker 都在重启。
留证 `tmp/netns-task3-compose-prod-sysctls.txt`、`tmp/netns-task3-compose-up.log`。

worker-1 日志（原文节选，`tmp/netns-task3-prod-worker1-logs.log`）：

```
INFO:envd_service.config:seccomp self-check: filter mode active, user namespaces allowed
Traceback (most recent call last):
  File "/app/envd_service/__main__.py", line 43, in main
    app = create_app(settings=settings)
  File "/app/envd_service/app.py", line 262, in create_app
    priv_helpers.configure_priv_helpers(settings)
  File "/app/envd_service/priv_helpers.py", line 906, in resolve_priv_helpers
    _require_route_b_scratch_root(helpers, settings)
  File "/app/envd_service/priv_helpers.py", line 1035, in _require_route_b_scratch_root
    raise PrivHelperError(
envd_service.priv_helpers.PrivHelperError: route-B scratch root /tmp/sandlock-route-b is outside the
privileged helper roots (/var/lib/e2b-sandboxes): the slot documents are group-scoped to the slot uid
through e2b-maint, so point E2B_ROUTE_B_TMP_ROOT at the workspace base
```

简报点名要查的两条：**无 `NET_ISOLATION_PAIRING_ERROR`** ✅、**有 `seccomp self-check: ...`** ✅
（也说明成对开关被 `create_app` 接受 —— 带配对错误的话第一步就炸在 `check_net_isolation_pairing`）。

容器能力事实（`tmp/netns-task3-capfacts.txt`）：

```
== worker-1
/compose-worker-1-1 sysctls=null capadd=null user=65534:65534
CapEff:	0000000000000000
== worker-2  （同）
== worker-3  （同）
```

⇒ 与简报"行为差异"清单里的 `Sysctls` 变 `null`、`CapEff` 仍为 0 一致。

### 4.2 改前对照：这不是本次改动引入的

改代码**之前**、用同一镜像、按当时文件自己的 env（无成对开关、未删窗口）直接跑：

```
$ docker run --rm -u 65534:65534 -e E2B_ENABLE_NETWORK=true -e E2B_ENABLE_NETNS=false \
    -e E2B_WORKSPACE_BASE=/var/lib/e2b-sandboxes -e E2B_BASE_IMAGE=python:3.14-slim \
    --security-opt seccomp=$PWD/deploy/seccomp/sandlock-worker.json \
    e2b-local/e2b-sandlock-worker:netns-task2c
INFO:envd_service.config:seccomp self-check: filter mode active, user namespaces allowed
envd_service.priv_helpers.PrivHelperError: route-B scratch root /tmp/sandlock-route-b is outside the
privileged helper roots (/var/lib/e2b-sandboxes): ...
```

留证 `tmp/netns-task3-probe-norouteb.log`（`ENV-HEADER commit=f03c462`，改动前的工作树）。
机制：镜像里有 F1 的 brokers ⇒ `resolve_priv_helpers` 走 broker 形状 ⇒
`_require_route_b_scratch_root` 用默认 `/tmp/sandlock-route-b` 对白名单（`/var/lib/e2b-sandboxes`）判负 ⇒
启动即拒。**与 netns 开关无关**（该次运行没开 netns）。

### 4.3 tmp-only override 后的形态实测：只差那一行

`tmp/netns-task3-compose-override.yml`（文件头写明 TMP-ONLY、不入库）给三个 worker 补
`E2B_ROUTE_B_TMP_ROOT: /var/lib/e2b-sandboxes/.route-b`（车队值，`deploy/stack/docker-compose.prod.yml:239`）：

```
compose-control-plane-1   Up 15 seconds
compose-redis-1           Up (healthy)
compose-worker-1-1        Up 15 seconds   49983/tcp
compose-worker-2-1        Up 15 seconds   49983/tcp
compose-worker-3-1        Up 15 seconds   49983/tcp

$ ... logs worker-1 | rg "NET_ISOLATION_PAIRING_ERROR|seccomp self-check|priv"
INFO:envd_service.config:seccomp self-check: filter mode active, user namespaces allowed
INFO:envd_service.app:startup uid reconciliation: referenced=[] reclaimed=[] cleaned=[] skipped=[]
INFO:envd_service.agent:registered node worker-1 at http://worker-1:49983
INFO:envd_service.runtime.image_resolver:resolved base image python:3.14-slim to rootfs ...
INFO:envd_service.app:worker image warmed: python:3.14-slim
```

留证 `tmp/netns-task3-compose-up-override.log`、`tmp/netns-task3-prod-worker1-logs-override.log`。

形态探针（`tmp/netns-task3-shape-probe.py`，`tmp/netns-task3-shape-probe.log`）：

```
$ E2B_API_URL=http://127.0.0.1:3900 E2B_SANDBOX_URL=http://127.0.0.1:3900 E2B_API_KEY=local-key \
    tmp/testenv/bin/python tmp/netns-task3-shape-probe.py
ENV-HEADER commit=fd81f26 date=2026-09-26T12:13:41+08:00 cmd=prod-example-netns-shape-probe
IFACES= ["lo"]
MATH= 2
PROD EXAMPLE NETNS SHAPE OK
PROBE-EXIT=0
```

⇒ 简报 Step 4 最后一条（"沙箱只见 `lo`"）**在这一行补上后成立**。
探针换掉了简报的 `ip -o addr`：基线镜像 `python:3.14-slim` **没有 `ip`**（Task 2 已实测），
改用 `socket.if_nameindex()`（车队脚本与 §2.4.7 的 `IFACES=` 探针同源），并把"看到什么"原样打印。

收尾：`docker compose ... down` 已执行，`docker ps` 回到改动前的容器集合（`tmp/netns-task3-teardown.log`）；
卷 `compose_sandbox-shared` / `compose_redis-data` 保留（未 `-v`）。

### 4.4 没跑的东西（不编）

`deploy/scripts/deployment_smoke.py` **没跑**，两条理由：

1. 坏态下三个 worker 都起不来（§4.1），smoke 只会 503；
2. 即便补上 §4.3 的那一行，smoke 的第 5 段（template 构建 → registry 推送 → worker OCI 拉取）要求
   本地 registry（`--profile registry`）与 `E2B_IMAGE_REGISTRY`，而本示例的出厂值是**空字符串**
   （注释写明"留空 = 保持单机行为，镜像留在本机"），简报给的起栈命令也没带 profile ⇒ 按字面必在第 5 段失败，
   与形态无关。

---

## 5. 与简报的差异

1. **行号引用（两处照实际改）**：`envd_service/executors/sandlock.py:664-669` → 现文件是 **`:744-748`**
   （`e8a36f5` 把那段落注释下移了约 80 行；Task 1 报告 §5 记过同类漂移）；
   `deploy/stack/docker-compose.prod.yml:349-352` → worker-2 的成对回滚 lever 现在在 **`:351-352`**。
   其余引用逐字核实无误、未改：`deploy/stack/docker-compose.prod.yml:209-210`（成对开关）、
   `envd_service/config.py:471-479`（`NET_ISOLATION_PAIRING_ERROR`）。
   改这两处的理由：简报要求"按内容定位、不要照行号盲改"，而这两行注释是给人看的**指路信息**，
   留着旧行号等于写进一条错误线索。
2. **前提不符（最重的一条）**：简报把 ① 当作"现状可用、只差切形态"一格（Step 4 直接 `up -d --build` +
   smoke + 形态探针）。实测现状**不可用** —— 缺 `E2B_ROUTE_B_TMP_ROOT`，`--build` 的 worker 起不来（§4.1/§4.2）。
   影响：Step 4 的"smoke 全绿""沙箱只见 `lo`"在本任务范围内**产不出**。我按派单规矩
   （"如实记录并停下问，不要自己扩大范围"）**没有**加那一行。
3. **简报引用的 Phase 2 门槛语义**：简报写"Task 1 的 phase 2（uid 65534）就是为这一格准备的实测；
   phase 2 不绿就别合"。Task 1 报告 **§7.3 自己写明 phase 2 的选择集**（5 个 sandlock/route-B 文件）
   **不含 netns 契约**，"phase 2 的绿不覆盖 netns 契约"。⇒ 那道门**字面上过**
   （`57 passed / 1 skipped / 0 failed`），但它**不是本格的形态证据**；本格真正拿到的形态证据是 §4.3。
4. 简报的其他核实项：`deploy/compose/.env.example` 里确实**没有**这两个键（不会覆盖 `:-true`）✅；
   顺带发现 `deploy/stack/.env.example:199-200` 把这两个键写成 `false`（与 stack 清单的 `:-true` 相反），
   不在本任务范围，登记备查。
5. 除注释措辞与上述行号外，3a/3b/3c 的 YAML 与 Step 1 的测试代码**逐字照抄**（未改断言、未加断言）。

---

## 6. 文件清单

入库：

```
deploy/compose/docker-compose.prod.yml                 | 45 +++++++++-------------
tests/unit/test_worker_manifest_permissions.py         | 31 ++++++++++++++++++
```

只在 `tmp/`（`.gitignore:5` 忽略）：`netns-task3-compose-override.yml`（TMP-ONLY override）、
`netns-task3-shape-probe.py`、`netns-task3-capfacts.sh`、
日志 `netns-task3-{red,green-unit,green-post-commit,compose-config,compose-up,compose-up-override,
compose-prod-sysctls,prod-worker1-logs,prod-worker1-logs-override,probe-norouteb,capfacts,teardown,unit-full}.log`。

---

## 7. 自审发现

- 三条断言真的能抓住三种半迁移（§1）；切片锚点在两处都在、切片非空（渲染出的三份 worker service 交叉印证）。
- 新增测试的注释里"the comment above the line names the old value on purpose"是**照简报逐字抄**的，
  对本文件**已略失真**：那条旧值只被删除，新散文不再逐字点出 `net.ipv4.ip_unprivileged_port_start=0`。
  断言本身针对的是 directive，仍然正确，故按"照抄"处理，只在此登记。
- Task 6 的收口钉子 `assert "ip_unprivileged_port_start" not in COMPOSE_PROD` 现在**成立**
  （本文件对该字符串 0 次命中，`rg -c` 已核）。
- 删掉窗口后本示例的出网**完全**依赖 supervisor 注入，MCP 入站走 50005+ 映射 —— 注释已写明；
  回滚（成对 `false`）时通配 `allowOut` 需要把窗口加回，注释也写了，且 `.env.example` 不含这两个键，
  所以本地 `.env` 不会意外把默认值覆盖成 `false`。
- 工作区是共享的：改动期间 HEAD 从 `f03c462` → `fd81f26`（另一 agent 落了池的 env 补齐），
  本次提交落在 `28c7f70`；§3.3 的全量单测把他人未提交改动一起跑了，故只把"14 条红名单逐条同名"当判据。

---

## 8. 担忧与需要拍板的岔路

1. **（要你点头）① 要不要一起补 `E2B_ROUTE_B_TMP_ROOT`？** 本文件头部的用法就是 `up -d --build`，
   现状下这一步必然 crash-loop；补上 `E2B_ROUTE_B_TMP_ROOT: /var/lib/e2b-sandboxes/.route-b`
   （车队值，`deploy/stack/docker-compose.prod.yml:239`）后 §4.3 实测起得来且形态成立。
   这属于 N39 的补丁面（池那边 `fd81f26` 已经在做同一件事），简报 3a–3c 没写 ⇒ 我停在这里问。
2. 这个缺陷**先于本任务 13 天**（F1 把 brokers 装进镜像那天起）：只有**不 build、用旧镜像**
   （`registry...worker:0.1.0`，08-30，无 brokers）才起得来 ⇒ "① 这个示例能跑"一直只在这种跑法下成立。
3. 若 ① 不补那一行，运维看到的效果是"从共享 netns + 窗口（旧镜像能用）换成同样起不来"，
   对"让部署形态可复现"这个目标**价值为负**；上一行是本节第 1 条建议的依据。
4. Task 1 §7.3 的门槛口径（phase 2 不覆盖 netns 契约）与本简报冲突；若 Task 6 拿"phase 2 绿"
   当本格的证据引用，会把证据说强。建议 Task 6 引用本报告的 §4.3 与 `tmp/netns-task3-shape-probe.log`。

---

## 9. 提交

```bash
git add deploy/compose/docker-compose.prod.yml tests/unit/test_worker_manifest_permissions.py
git commit -m "feat(compose): the prod example runs the fleet netns shape"
# => 28c7f70 feat(compose): the prod example runs the fleet netns shape
```

---

# 追加裁定落地（2026-09-26 12:19–12:26）

控制器裁了 §8.1 与 §8.4 两条。本节是**追加**内容，上面的 §0–§9 保持原样（除 §4 里"没跑 smoke"的
结论已被本节取代，见 §12）。

## 10. 裁定 1：`E2B_ROUTE_B_TMP_ROOT` 补进 ①（含对齐钉子）

### 10.1 改了什么

| 文件 | 动作 |
|---|---|
| `deploy/compose/docker-compose.prod.yml` | +12：worker anchor 里 `E2B_WORKSPACE_BASE` 之后插 `E2B_ROUTE_B_TMP_ROOT: /var/lib/e2b-sandboxes/.route-b` + 9 行解释注释 |
| `tests/unit/test_worker_manifest_permissions.py` | +76：`_value_after_key` / `_k8s_env_value` / `_fleet_route_b_roots` / `_compose_prod_worker_route_b_root` 四个 helper + `test_compose_prod_worker_env_carries_the_fleets_route_b_root()` |

值**照抄车队**（`deploy/stack/docker-compose.prod.yml:239`，`deploy/k8s/worker.yaml:258-259` 同值），
没有自己发明：钉子把两份车队清单都**解析**出值再与 ① 的值 `==` 比较（池那一处的形状：
`tests/unit/test_autoscaler_local_backend_shape.py`），不是把字面量抄两遍；断言锚在
`worker-1: &worker`→`worker-2:` 的切片上，而不是"全文件某处出现过"。

### 10.2 RED 原始输出

`tmp/testenv/bin/python -m pytest "tests/unit/test_worker_manifest_permissions.py::test_compose_prod_worker_env_carries_the_fleets_route_b_root" -q -p no:cacheprovider`
（完整留证 `tmp/netns-task3-routeb-align-red.log`）

```
ENV-HEADER commit=28c7f70 date=2026-09-26T12:19:30+08:00 cmd=pytest-task3-routeb-align-red
F                                                                        [100%]
=================================== FAILURES ===================================
_________ test_compose_prod_worker_env_carries_the_fleets_route_b_root _________
        fleet = _fleet_route_b_roots()
        # One value across the fleet, and it is the path under the workspace base
        # the comment in `deploy/stack/docker-compose.prod.yml:239` explains.
        assert set(fleet.values()) == {"/var/lib/e2b-sandboxes/.route-b"}, fleet
>       assert _compose_prod_worker_route_b_root() == {
            "E2B_ROUTE_B_TMP_ROOT": next(iter(fleet.values()))
        }
E       AssertionError: assert {} == {'E2B_ROUTE_B...xes/.route-b'}
E
E         Right contains 1 more item:
E         {'E2B_ROUTE_B_TMP_ROOT': '/var/lib/e2b-sandboxes/.route-b'}
E         Use -v to show more diff

tests/unit/test_worker_manifest_permissions.py:877: AssertionError
=========================== short test summary info ============================
FAILED tests/unit/test_worker_manifest_permissions.py::test_compose_prod_worker_env_carries_the_fleets_route_b_root
1 failed in 0.21s
PYTEST-EXIT=1
```

注意第一条断言**已经通过**（说明车队两份清单解析出来的值就是 `/var/lib/e2b-sandboxes/.route-b`），
红的是 ① 那一侧 `{}` —— 键确实不存在，正是 §4.1 的机制。

### 10.3 GREEN（提交后复跑，`586569c`）

```
ENV-HEADER commit=586569c date=2026-09-26T12:25:53+08:00 cmd=pytest-task3-green-post-commit-v2
............................                                             [100%]
28 passed in 0.49s
PYTEST-EXIT=0
--- render (expect 3 route-B key lines, no sysctls) ---
3
sysctls count=0
```

（`tmp/netns-task3-green-unit-v2.log`、`tmp/netns-task3-green-post-commit-v2.log`。模块从 27 → 28 条。）

## 11. 裁定 1 的复跑：三 worker 起来 + 形态探针

**只用出厂文件**（无 override，`up -d --build`；`tmp/netns-task3-compose-up-v2.log`）：

```
ENV-HEADER commit=28c7f70 date=2026-09-26T12:19:48+08:00 cmd=compose-up-v2-no-override
 Container compose-worker-3-1 Started
 Container compose-worker-1-1 Started
 Container compose-worker-2-1 Started
NAME                      IMAGE                                       COMMAND                  SERVICE         STATUS                        PORTS
compose-control-plane-1   e2b-sandlock-control-plane-gateway:0.1.0    "python -m control_p…"   control-plane   Up 19 seconds                 0.0.0.0:3900->3000/tcp
compose-redis-1           redis:8-alpine                              "docker-entrypoint.s…"   redis           Up 25 seconds (healthy)       6379/tcp
compose-worker-1-1        e2b-local/e2b-sandlock-worker:netns-task3   "python -m envd_serv…"   worker-1        Up 18 seconds                 49983/tcp
compose-worker-2-1        e2b-local/e2b-sandlock-worker:netns-task3   "python -m envd_serv…"   worker-2        Up 18 seconds                 49983/tcp
compose-worker-3-1        e2b-local/e2b-sandlock-worker:netns-task3   "python -m envd_serv…"   worker-3        Up 18 seconds                 49983/tcp
```

⇒ **三个 worker 全是 `Up`，不再是 `Restarting (1)`**（与 §4.1 改动前同一命令的输出逐字对照）。

容器事实与启动自检（`tmp/netns-task3-compose-v2-facts.log`）：

```
/compose-worker-1-1 sysctls=null capadd=null user=65534:65534
/compose-worker-2-1 sysctls=null capadd=null user=65534:65534
/compose-worker-3-1 sysctls=null capadd=null user=65534:65534
--- pairing/privhelper grep over all three (expect ONLY the three seccomp lines) ---
worker-2-1  | INFO:envd_service.config:seccomp self-check: filter mode active, user namespaces allowed
worker-3-1  | INFO:envd_service.config:seccomp self-check: filter mode active, user namespaces allowed
worker-1-1  | INFO:envd_service.config:seccomp self-check: filter mode active, user namespaces allowed
worker-2-1  | INFO:     Application startup complete.
worker-3-1  | INFO:     Application startup complete.
worker-2-1  | INFO:envd_service.agent:registered node worker-2 at http://worker-2:49983
worker-3-1  | INFO:envd_service.agent:registered node worker-3 at http://worker-3:49983
worker-1-1  | INFO:     Application startup complete.
worker-1-1  | INFO:envd_service.agent:registered node worker-1 at http://worker-1:49983
```

⇒ 三行 `sysctls=null`、无 `NET_ISOLATION_PAIRING_ERROR`、无 `PrivHelperError`、三个都
`Application startup complete` + `registered node`。

形态探针（`tmp/netns-task3-shape-probe-v2.log`）：

```
ENV-HEADER commit=28c7f70 date=2026-09-26T12:21:01+08:00 cmd=prod-example-netns-shape-probe-v2
IFACES= ["lo"]
MATH= 2
PROD EXAMPLE NETNS SHAPE OK
PROBE-EXIT=0
```

### 11.1 额外一条：wildcard `allowOut` 在没有窗口时仍然解析 + 出网

删窗口的全部依据是"通配 DNS 网关的 `:53` 绑定落在沙箱自己的 netns 里"。这条以前在仓库里是**推论**，
现在在 ① 上实测（`tmp/netns-task3-wildcard-dns-probe-v2.log`）：

```
ENV-HEADER commit=43f9085 date=2026-09-26T12:22:41+08:00 cmd=prod-example-wildcard-dns-probe-v2
RESOLV.CONF= nameserver 127.0.0.2 | options ndots:0 timeout:1 attempts:1
DNS-STDOUT= 10.250.0.2 | DNS-STDERR= 
EGRESS-STDOUT= CONNECT-OK 104.20.23.154 | EGRESS-STDERR= 
WILDCARD DNS + EGRESS WITHOUT A LOW-PORT WINDOW OK
PROBE-EXIT=0
```

⇒ 沙箱里的 `/etc/resolv.conf` 就是网关地址（`127.0.0.2`），解析走通，真实连接拿到
`CONNECT-OK`（Cloudflare 的 `104.20.23.154`）。**容器级低端口窗口确实没有用户了。**

中间那版探针只写了 `allow_out: ["*.example.com"]`，然后去连 apex `example.com`，得到
`ConnectionRefusedError: [Errno 111]`：这正是 `docs/open-issues.md` N38 记的**策略拒绝 errno**
（`verdict.rs:16`，且 wildcard 不匹配 apex），不是"没窗口就连不出去"。加上 apex 规则后同一条命令绿。
这一段留给下一个人当反向证据：先看日志再改探针，别把策略拒绝读成形态回退。

### 11.2 业务冒烟 `deployment_smoke.py`：第 1–4 段绿，第 5 段卡在本示例的既有缺口

第一次（出厂 `E2B_NODE_*`）在第 2 段 503，**与简报预告的一致**
（`deploy/compose/.env.example:60-70`：`E2B_NODE_PROCESSES=256` 时一个 worker 只放得下一个沙箱）：

```
NODE DISTRIBUTION: {'http://worker-2:49983', 'http://worker-1:49983', 'http://worker-3:49983'}
OK: commands + files through gateway
AssertionError: {"code":503,"message":"Node worker-1 has no capacity or is unavailable"}
```

按处方把 `E2B_NODE_MEMORY_MB=4096 E2B_NODE_CPU_PERCENT=400 E2B_NODE_DISK_MB=8192 E2B_NODE_PROCESSES=1024`
重建后（`tmp/netns-task3-deployment-smoke-capacity.log`）：

```
NODE DISTRIBUTION: {'http://worker-2:49983', 'http://worker-3:49983', 'http://worker-1:49983'}
OK: commands + files through gateway
OK: migrated worker-1 -> worker-3, files kept
OK: network config echo + atomic update
OK: volume mounted remotely + sibling volume isolated
after kill reservations: {'worker-1': 0, 'worker-2': 0, 'worker-3': 0}
...
e2b.exceptions.BuildException: buildkit build exited with code 1
SMOKE-EXIT=1
```

第 5 段失败的原因**从日志里读出来的**（`tmp/netns-task3-template-build-status.log`）：

```
LOG: building template (buildkit: unix:///run/buildkit/buildkitd.sock)
LOG: error: listing workers for Build: failed to list workers: Unavailable: connection error: desc = "transport: Error while dialing: dial unix /run/buildkit/buildkitd.sock: connect: no such file or directory"
LOG: buildkit build exited with code 1
```

⇒ `deploy/compose/docker-compose.prod.yml` **不起 buildkitd**（stack/k8s 才带那一层），template 构建
在这个示例里按设计就跑不通，与 netns 形态无关。所以简报 Step 4 "逐段 OK 退出码 0" 这条 Expectation
**按字面是不可能达成的**，已按实测改成"第 1–4 段 OK；第 5 段需要额外起 buildkit"，见 §12。

## 12. 裁定 2：计划里那几句错话的更正（改前 → 改后）

共 5 处（都落在 `docs/superpowers/plans/2026-09-26-netns-shape-unification.md`）：

1. **Task 3「行为差异」第 4 条（原句所在，最重的一处）**
   - 改前：`…Task 1 的 phase 2（uid 65534）就是为这一格准备的实测；**Task 1 的 phase 2 不绿就别合这个 Task**。`
   - 改后：保留"这一格证据最薄"的前半句，接 **⚠️ 2026-09-26 更正（原句是错的）**：Task 1 报告 §7.3
     写明 phase 2 的选择集（5 个 sandlock/route-B 文件）**不含** netns 契约；本格真正的两条实测通道
     是 ① Task 1 建立的 `NETNS_ENV` 透传、② **本格的容器级实测**（三 worker `Up` + `sysctls=null` +
     `IFACES=["lo"]` + 沙箱内 `nameserver 127.0.0.2`、DNS 与 `CONNECT-OK`）；合并门槛随之改为这两条。
2. **Task 1 Step 4 的期望第 3 条（那句错话的源头）** —— 原文不动，紧跟一条缩进的 ⚠️ 更正行，
   说明这道门只覆盖"成对开关作为 worker 默认值、无特权相位仍全绿"，不覆盖 netns 契约。
3. **Task 3 Step 3 之后新增 `⚠️ 追加裁定（2026-09-26，同批执行）`** —— 记录裁定 1：
   为什么必须补 `E2B_ROUTE_B_TMP_ROOT`（`up -d --build` 三个 worker `Restarting (1)`，N39 在 ① 的同一根因）、
   值照抄车队、钉子形状。
4. **Task 3 Step 4 的 smoke `Expected` 之后新增 `⚠️ 2026-09-26 实测（Task 3）`** —— 记两段实测：
   默认 `E2B_NODE_*` 会 `503 Node worker-1 has no capacity`（`.env.example:60-70` 的处方）、
   处方值下第 1–4 段绿、第 5 段因本示例无 buildkitd 必失败（附原始报错）。
5. **Task 6 的 `docs/open-issues.md` N36 模板（3k）** —— 把"两相位全绿（`0 failed`，phase 2 = uid 65534
   这一格此前无实测）"改成"两相位全绿（`0 failed`；phase 2 的选择集**不含** netns 契约，"65534 这一格"的
   形态证据是 ① 的容器级实测）"，并把"`deployment_smoke.py` / `multinode_smoke.py` 全绿"改成
   "`deployment_smoke.py` 第 1–4 段全绿（第 5 段的 template 构建需要额外的 buildkitd，`deploy/compose`
   示例不起它）"，证据指向补上 `tmp/netns-task3-*.log|txt`。

**落点提示**：这 5 处改动**没有**进我的 commit —— 并行 agent 的 `d87834b` 把它们一并提交了
（`git log -S` 对五段新文字逐个命中 `d87834b`），计划文件在 HEAD 已是 clean。我的 commit 只含两个代码文件。

## 13. 回归（判据：14 条红名单逐条同名）

```
tmp/testenv/bin/python -m pytest tests/unit -q -p no:cacheprovider
14 failed, 1329 passed, 11 skipped, 2 warnings in 58.79s
```

14 条与已知 Linux-only 名单**逐条同名**（`test_priv_helpers.py` 11 + `test_real_root_gate.py` 1 +
`test_xfs_quotactl_backend.py` 2）；passed 从上一轮的 1313 涨到 1329（本任务 +1 条钉子，其余是并行
agent 的用例）。留证 `tmp/netns-task3-unit-full-v2.log`。

## 14. 文件清单（本轮追加）

入库（commit `586569c`）：

```
deploy/compose/docker-compose.prod.yml                 | 12 ++++
tests/unit/test_worker_manifest_permissions.py         | 76 ++++++++++++++++++
```

计划文档的 5 处更正已随 `d87834b`（并行 agent）入库；`tmp/` 新增证据：
`netns-task3-{routeb-align-red,green-unit-v2,green-post-commit-v2,compose-up-v2,compose-up-v2-capacity,compose-v2-facts,shape-probe-v2,wildcard-dns-probe,wildcard-dns-probe-v2,deployment-smoke,deployment-smoke-capacity,template-build-status,control-plane-build-log,teardown-v2,unit-full-v2}.log`
与 `netns-task3-wildcard-dns-probe.py`。收尾：`docker compose ... down` 已执行，`docker ps` 只剩改动前的容器
（`tmp/netns-task3-teardown-v2.log`）。

## 15. 追加部分的担忧

1. **计划文档的更正不在我的 commit 里**（被 `d87834b` 捎带）。若上游要求"一处裁定 = 一处 commit"，
   需要重排历史；我没有动别人的提交。
2. **`deployment_smoke.py` 的"全绿"在本示例里做不到**（无 buildkitd，§11.2）。简报 Step 4 与计划的 3k
   模板原来都写"全绿"，我已按实测改口径；Task 6 写入 docs 时请照 §11.2 的说法，别只写"smoke 全绿"。
3. 探针那条"wildcard 不含 apex ⇒ `ECONNREFUSED`"（N38 的策略拒绝 errno）值得进文档：否则运维会把它读成
   "撤掉低端口窗口后出网坏了"，正是 N36/OBS 那类误判。
4. `deploy/compose/.env.example` **没有**登记 `E2B_ROUTE_B_TMP_ROOT`（车队只在 stack 清单里给这个键）。
   ① 现在是字面量、`.env.example` 里没有可覆盖项 —— 与 netns 那两个键同性质。要不要把三个键一起补进
   `.env.example`（让"可回滚/可覆盖"也有入口）不在本任务范围，登记待你决定。
