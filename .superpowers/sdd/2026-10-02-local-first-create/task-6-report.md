# Task 6 报告：发版记录与跨切面冒烟（建箱存储本地优先）

**日期**：2026-10-02
**版本**：`0.1.0-908-gd652148-20261002-184859`（= `deploy/stack/.version`；worker /
control-plane-gateway / agent 三个镜像同 tag；9 个 pod Running）
**工作 HEAD**：`59ce556`（本任务开始时是 `d652148`；期间上游加了一个 docs-only 提交
`59ce556`「补记 Task 1–5 的勾选」，只动计划文件的复选框，不影响本次读数与代码）

## Status

**Task 6 的两件交付都做了，但本批没有闭合 —— 跨切面冒烟是红灯。**
两条冒烟（`MULTI-NODE` / `DEPLOYMENT`）都因为一个**新发现的真 bug**失败：迁移的
「源节点不可达」具名拒绝路径**泄漏目标节点的配额**（已登记 **N59**）。清那条台账要写
Redis（集群写），不在本任务的授权范围内，所以**残留没有被清理**，如实记在下面。
介质翻转的泄漏修复（`39f28a9` + `d652148`）**已按判据复核通过**。

## 1. 泄漏修复的现场复核（Task 3 的 `stale-tree-on-former-source`）

判据（brief 原话）："after a *successful* migration the source node's tree is **gone**
and no `stale-tree-on-former-source` note appears on the record." 结果是**成立**。

`tree_local_migration_probe.py --directions up --files 200`（公开 API 建/杀沙箱）：

```
METRIC up status=200 from=e2b-worker-0 to=e2b-worker-1 ms=889
METRIC up kept='kept\n' last='199\n' files=201
METRIC residue=[]
```

探针自己会在 `finally` 里杀掉沙箱，来不及看源节点的树，所以另跑了一条**在检查前不杀**
的流（`tmp/t6-leak-check.py`）—— 建 200 文件、确认树在 `e2b-worker-0`、迁到
`e2b-worker-1`，然后：

```
METRIC source-tree-before id_present=True trees=1
METRIC migrate status=200 ms=895
METRIC source-tree-after id_present=False trees=0 target_has_tree=True
METRIC record-note source_tree_retained=False
METRIC cp-log pod=control-plane-…-54cgs retention_error_lines=0
METRIC cp-log pod=control-plane-…-qmr9m retention_error_lines=0
METRIC after-kill fleet={} roots src=[] tgt=[]
LEAK CHECK OK
```

即：**源节点 tree root 上该 id 已消失**（`trees=0`）、目标节点有它、记录日志里
没有 `source tree retained`、控制面两个副本日志里没有 `was retained` 的 ERROR。
`0.1.0-908` 上新修复生效 —— 泄漏**关掉了**。

down 腿（brief 授权的 `kubectl scale`，跑完复原）：

```
METRIC stop-source pod=e2b-worker-1 gone after 1.1s
METRIC down status=502 target=e2b-worker-0
  body={"code":502,"message":"source-node-unreachable: node e2b-worker-1 is not answering,
        so the sandbox tree it holds cannot be reached or moved (it did not acknowledge the runtime stop)"}
METRIC restore workers={'e2b-worker-0': 'Running', 'e2b-worker-1': 'Running'}
METRIC residue=[]
```

复原核过：`statefulset/e2b-worker` `replicas=2 ready=2`、两个 worker Running、9 pod。

## 2. Task 6 Step 2 的三条读数（分形状，附命令）

### 2.1 plain 建箱 p50（**本轮实测**）

```bash
export KUBECONFIG="$PWD/tmp/k0s/kubeconfig"
export E2B_API_KEY=$(kubectl -n sandlock get secret e2b-secrets \
    -o jsonpath='{.data.E2B_API_KEYS}' | base64 -d | cut -d, -f1)          # 不打印

# 客户端边界（本机 → 入口），两轮 n=10
env -u http_proxy -u https_proxy -u all_proxy tmp/venv/bin/python \
    deploy/scripts/acceptance/create_latency_probe.py \
    --base http://172.18.78.49:3000 --key "$E2B_API_KEY" --n 10

# 平台侧（控制面 pod 内回环），n=10
CP=$(kubectl -n sandlock get pod -l app=control-plane -o jsonpath='{.items[0].metadata.name}')
kubectl -n sandlock exec -i "$CP" -c control-plane -- \
    python3 - --base http://127.0.0.1:3000 --key "$E2B_API_KEY" --n 10 \
    < deploy/scripts/acceptance/create_latency_probe.py
```

| 口径 | 读数 |
|---|---|
| 客户端边界（两轮） | **70 / 71 ms**（p95 75 / 86；mean 71 / 74；预热那一发 611 ms） |
| 平台侧（控制面 pod 内） | **40 ms**（p95 49；mean 43） |
| 客户端→入口那一段 | 约 **30 ms**（70 − 40） |

**对 ~60 ms 目标：平台侧 40 ms 在目标内；客户端可见的 70 ms 比目标高约 10 ms。**
计划的目标写的是平台侧口径（改前 §7.31 的 116–117 ms 也是控制面 pod 内量的），所以
**平台侧达标、客户端边界未达标**，两个口径都要报。

**worker trace 之外有多少**：worker 的逐段 trace **本轮没测**（要 `E2B_CREATE_TRACE=1`，
那会改 StatefulSet env → 一次滚动，本任务未获授权）。复用 §7.31 在 `0.1.0-900` 上的
逐段：`prepare` 7.4 + `finalize` 8.4 + `prime` 3.3 ≈ **19 ms** ⇒ 平台侧 40 ms 里
**≈ 21 ms 在 worker trace 之外**（控制面自己的活 + 与 `prepare` 并发的 agent `materialize`
那一跳）。对照 §7.31 改前 ≈ 97 ms（116 − 19）：**翻转主要动的就是这一段**。这是
「实测 + 复用 + 相减」的推断，不是本轮重量的 split。

### 2.2 快照建箱每条目 / 每字节（**翻转后本轮实测**；翻转前的数复用 §7.30）

```bash
export E2B_API_URL=http://172.18.78.49:3000 E2B_SANDBOX_URL=http://172.18.78.49:3000
env -u http_proxy -u https_proxy -u all_proxy tmp/venv/bin/python \
    deploy/scripts/acceptance/snapshot_create_probe.py --files 1,40,202 --n 3
env -u http_proxy -u https_proxy -u all_proxy tmp/venv/bin/python \
    deploy/scripts/acceptance/snapshot_create_probe.py --files 2000 --n 3 --timeout 600
```

| 形状 | 捕获 | 从快照建箱 p50 | 每条目 |
|---|---|---|---|
| **翻转前**（202 档，`0.1.0-895`，树仍在共享）—— **复用 §7.30** | 1703 ms | 6162 ms | 30.354 ms |
| 翻转后 1 / 40 / 202（`0.1.0-908`） | 141 / 134 / 152 ms | **87 / 123 / 154 ms** | 43.682 / 2.997 / **0.756 ms** |
| 翻转后 **2000**（计划点名的那一档） | **419 ms** | **532 ms**（p95 1097，mean 719） | **0.266 ms** |

**目标「2000 文件 52 s → 亚秒」：达成（p50）** —— 同档 p50 **532 ms**（3 个样本里
有一个 1097 ms 越过 1 s，p95 因此 > 1 s）。对照旧形状 28.2 ms/条目 × 2000 ≈ 56 s，
与计划记的 52 s 同一量级。三档都带生产形状守卫：`kept='kept\n'`、
`workspace/workspace/…` = `FileNotFoundException`。

### 2.3 迁移保文件（**复用 §7.32 / 设计 §8.5，本轮复跑**）

```bash
env -u http_proxy -u https_proxy -u all_proxy tmp/venv/bin/python \
    deploy/scripts/acceptance/tree_local_migration_probe.py --directions up --files 200
```

| 来源 | 读数 |
|---|---|
| 复用（§7.32，`0.1.0-905`） | `e2b-worker-0 → e2b-worker-1`，**1047 ms**，201 文件 + 1 目录，逐字读回 |
| 本轮复跑（`0.1.0-908`） | **889 ms**（probe）/ **895 ms**（独立流），文件读回一致 |

## 3. 冒烟与残留（Task 6 Step 3）—— **红灯**

```bash
env -u http_proxy -u https_proxy -u all_proxy tmp/venv/bin/python deploy/scripts/multinode_smoke.py
env -u http_proxy -u https_proxy -u all_proxy tmp/venv/bin/python deploy/scripts/deployment_smoke.py
```

两条冒烟都从 env 取凭据（`E2B_API_URL` / `E2B_SANDBOX_URL` / `E2B_API_KEY` /
`E2B_INTERNAL_API_KEY`）。

| 项 | 结果 |
|---|---|
| `MULTI-NODE` | **❌ 失败**：建第 4 个沙箱 `503: No resources available`；`finally` 里 `assert` 预约归零也失败 |
| `DEPLOYMENT` | **❌ 失败**：3 个建箱成功、`NODE DISTRIBUTION` 两节点都在、命令/文件段过；**迁移**那步 `503 {"code":503,"message":"Node e2b-worker-0 has no capacity or is unavailable"}` |
| `GET /sandboxes` | `[]` |
| `GET /internal/fleet/sandboxes` | `{}` |
| 两节点树根 / 共享旧树根 | 都是 **0 项** |
| pod | 9 个全 Running（`e2b-worker` 2/2） |
| `DRY_RUN=1 deploy/k8s-k0s/apply.sh \| kubectl diff -f -` | **0 行** |
| **节点配额台账（唯一脏残留）** | worker-0：Redis `e2b:node:quota:e2b-worker-0` = memory **3072** / cpu 300 / disk 3072；同一刻 `/internal/nodes.reservedMemoryMB` = **1024**；worker-1 两边都是 0。**`GET /sandboxes` 是 `[]`** |

### 3.1 冒烟为什么红（新真 bug，登记 N59）

代码路径（`control_plane/api/sandboxes.py::migrate_sandbox`）：

1. 先 `nodes.reserve_node(target_node_id, **dims)` —— 同时加**内存**台账与 **Redis** 台账；
2. **之后**才 `_stop_source_runtime`；源节点不回答 → `raise source_node_unreachable(...)`；
3. 这走外层 `except Exception` 回滚 —— 只把记录指回源节点、尽力重建源的运行时，
   **从不 `release_quota(target)`**。

⇒ **每一次具名拒绝都泄漏目标节点的一份配额。** 本任务的 down 腿（source=worker-1,
target=worker-0）就泄漏了 1024：down 腿跑完当场 `/internal/nodes` 报 worker-0 = 1024
（此前 0）。上一次 `0.1.0-905` 的验收也跑过 2/2 同样的拒绝，所以 Redis 上 worker-0 累计
3072 = 3 × 1024（份数是推演，路径与回滚是读代码确认的）。

**它为什么让冒烟失败**：worker-0 的 Redis 台账满时，`select_and_reserve` 选中它、
`_quota_store.reserve` 返回 False、函数**不换下一个候选**直接 `return None` ⇒
`503: No resources available`，即便 worker-1 还有空位。我直接复现了这条序列（建 3 个：
w1、w0、w1；第 4 个 503），并在 `/internal/nodes` 与 Redis 两边同时读到台账。

**为什么残留清不掉**：清 Redis 是一次集群写，不在本任务授权内（只授权了公开 API 沙箱与
down 腿的 `kubectl scale`）。另外控制面**没有**按记录重建 Redis 台账的路径 ——
`_rebuild_node_reservations` 只在节点注册时重建**内存**那一半，Redis 那一半只增不减，
所以两个数会长期漂移。

## 4. 文档改动（Task 6 Step 4）

* **`docs/deploy-clusters.md`**
  * 新 **§7.33 发版：建箱存储本地优先（Task 0–5）** —— 把 §7.29–§7.32 串成一次上线：
    一条版本线（887 → 892/895 → 900 → 905 → **908**）、上线后的开关取值表、§7.33.1 的
    **验收表**（每条标"本轮实测"还是"复用"）、§7.33.2 **两条硬约束**（容量 / 页缓存）与
    各自的上限配置、§7.33.3 计划的**刻意不做**清单、§7.33.4 **冒烟与残留（红灯）**、
    §7.33.5 回退路。
  * §7 的"当前部署状态"摘要行改指 §7.33；把 §7.28–§7.31 里含义会被误读的"当前版本"
    改成"该节版本"（避免在 908 之后还留着旧版本号自称"当前"）。
  * 修掉 §7.32 标题上挂错的 **N57**（Task 3 不是 N57；N57 是 Task 4 那条 uid 池残余）。
* **`docs/open-issues.md`**
  * **N57** 行：补上「本批发版记录见 §7.33；Task 6 复核后仍只登记未修」。**没有**新开
    第二个 N57。
  * 新 **N59** 行：迁移被具名拒绝时泄漏目标节点配额 —— 触发条件、两条台账为什么会漂、
    实测后果（两条冒烟红）、补救（`reserve_node` 挪到确认源可停之后 / 拒绝路径补
    `release_quota(target)` / 台账按记录对账）、以及怎么证伪（拒绝路径要有能红的钉子）。
* **`README.md`**
  * §8 新增 **8.1 当前部署形态：建箱存储本地优先** —— 三条用户会关心的读数（沙箱内小文件
    58–66×、2000 文件从快照建箱 532 ms、建箱 p50 平台 40 ms / 客户端 70 ms）、两条操作
    语义（节点掉线丢树、先迁走再下线；迁移经 `_migrate` 中转）、以及**冒烟红灯的警告**。
  * §10"路由与迁移"那条旧边界（"共享存储模式下只重建挂载与路由"）改成默认本地布局的
    真实语义。
  * `deploy/stack/.version` **没动**：它已经是 `0.1.0-908-gd652148-20261002-184859`，
    与线上三个镜像逐字一致（该文件 gitignored，不入库）。

写进 §7.33.2 的**两条硬约束与上限配置**（照录计划 Global Constraints + Task 1 实测）：

1. **容量**：每节点约 75 G 可用、与 4 GiB 镜像缓存同盘；`E2B_NODE_DISK_MB=8192` +
   `E2B_DEFAULT_DISK_MB=1024` ⇒ 8 沙箱/节点。上限配置：`E2B_NODE_DISK_MB`、
   `E2B_IMAGE_CACHE_MAX_BYTES=4294967296`。
2. **页缓存**：worker 4 GiB / 控制面 2 GiB / agent face A 256 MiB / face B `maint` 2 GiB
   （Task 3 从 512 MiB 抬上来，抬之前真 OOMKilled 过一次）。上限配置：
   `E2B_TREE_COPY_MAX_BYTES=1342177280`、`E2B_TREE_COPY_WINDOW_BYTES=67108864`。

计划的**刻意不做**清单也照录进 §7.33.3：卷数据 / `_volumes/_meta` / `_templates` /
`_builds` / `_oci.tar` 一律不动；`_cow` 保留名不删；checkpoint 本轮仍留共享；
**不做**"树本地 + 跨节点冗余"（节点掉线丢树由产品语义承担）。

## 5. 测试

```
tmp/venv/bin/python -m pytest tests/unit -q -p no:cacheprovider
→ 3 failed, 2274 passed, 12 skipped in 172.01s
```

3 条失败 = 基线 macOS-only（`test_real_root_gate.py` 1 条、`test_xfs_quotactl_backend.py` 2 条），
与 brief 给的基线逐条一致。`tests/unit/test_docs_only_point_at_repo_artifacts.py` 9 passed
（新增的 README / deploy-clusters 引用没有指向 `tmp/**`）。本任务**只改文档**，没有代码改动。

## 6. Concerns（按重要性）

1. **N59 是发布阻断项，而本批的验收逻辑把它带进来的**：迁移拒绝路径泄漏目标节点配额，
   生产后果是节点有效容量缩水、`503 No resources available` 会在调度选中"台账满"的节点时
   出现（即便另一节点有空位）。**建议在宣告本批闭合之前先修 N59**（把 `reserve_node` 挪到
   确认源可停之后，或在拒绝/回滚路径补 `release_quota(target)`），并补一条会红的钉子。
2. **被污染的配额台账目前没有授权内的清法**：worker-0 的 Redis 台账虚高 3072（内存台账
   虚高 1024），清它要写 Redis。**残留原样留在那里**，并已记进 §7.33.4/N59；在清掉之前
   两条冒烟都不会绿。
3. **建箱 ~60 ms 目标按口径分读**：平台侧 40 ms 达标，客户端边界 70 ms 未达标（约 30 ms
   是客户端→入口网络）。计划里的 60 ms 是平台侧口径，两条都写进了发版记录。
4. **worker trace 未在本轮重量**：没有开 `E2B_CREATE_TRACE`（一次 worker 滚动，未授权），
   "worker trace 之外 ≈21 ms" 是复用 §7.31 的逐段再相减；要坐实得给控制面侧也加逐段 trace。
5. **两处陈旧注释（本轮未改，留给后续）**：`deploy/scripts/deployment_smoke.py` 的步骤 2
   注释仍写 "shared workspace keeps the file (no archive transfer)"，翻转后不成立
   （断言仍然对：文件确实保住了）；`deploy/scripts/multinode_smoke.py` 的 docstring 仍写
   "against a live compose deployment"（它现在也跑 k0s）。

## 附：本轮用到的原始日志（gitignored，`tmp/`）

`tmp/t6-up-leg.log`、`tmp/t6-down-leg.log`、`tmp/t6-leak-check.log`、
`tmp/t6-create-outside-1.log`、`tmp/t6-create-outside-2.log`、`tmp/t6-create-inside.log`、
`tmp/t6-snapshot-probe.log`、`tmp/t6-snapshot-2000.log`、`tmp/t6-smoke-multinode*.log`、
`tmp/t6-smoke-deployment.log`、`tmp/t6-dry-run-diff.txt`、`tmp/t6-unit-tests.log`、
脚本 `tmp/t6-leak-check.py`。
