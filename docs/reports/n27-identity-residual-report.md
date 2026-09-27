# N27 identity 残差：核实与收口（2026-09-27）

本单要回答的是登记里那句"由 N16 收敛、不单开 ⇒ **下次跑 identity lane 时顺手核一遍是否已消**"，
外加一句限定语与探针自检。**结论：默认的 `identity` 档没被 N16 改到 —— 残差仍在；`synth` 档确实
已经消掉。** 默认形态要不要也跟着消，需要用户裁定（唯一一步是把 `E2B_PURE_ROOTFS` 的默认值从
`off` 切到 `synth`，代价见 §5）；**本轮没有改任何默认值。**

## 0. 结论摘要

| # | 问题 | 实测答案 |
|---|---|---|
| 1 | `synth` 档是否已消掉"能列出名字"？ | **是**。`chain=PASS`，祖先链只有 3 层，全是沙箱自己的合成根 ⇒ `<export>` 那一层**根本不在链上**（不是"列了但没命中"）。 |
| 2 | `identity` 档是否仍列名字？errno 是 `EACCES` 还是 `ENOENT`？ | **仍列**：`<export>` 一层 `LEAK ["_secrets", "state"]`；四次 `stat` 全是 **`EACCES`**（不是 `ENOENT`）⇒ "读不到内容"这半边成立，"名字看不见"这半边不成立。 |
| 3 | `--layout legacy` 反例档是否 `exit 1`？ | **是**，而且是真 FAIL 不是 VACUOUS：`stat=FAIL`（状态目录**本身**可 `stat`）+ `chain=FAIL`。 |
| 4 | 探针自身有没有"看着在守、其实没守"？ | **两处**，都已修并留 RED→GREEN（§4）：② 的祖先链半边只认四条硬编码名字（换基名即失明）；lane 在 checker 跑起来之前崩掉也走 `exit 1`（与"反例成立"同码）。 |
| 5 | 默认形态要不要也切？ | **留给用户**：切=`E2B_PURE_ROOTFS` 默认值 `off`→`synth`，代价三条见 §5；本轮未动默认值。 |

## 1. 怎么跑的（按现成走法，没有新造一套）

* 探针：`tmp/k0s/probe_state_base_visibility.py`（`lane` 模式；它按 worker 的建箱方式调
  `tests/security/conftest.py::route_b_sandbox`，形状由 `LANE_SHAPES` 决定）。
* 容器入口：**现成的** `tmp/k0s/n27-t7-lane.sh`（N27 Task 7 留下的那一个）—— repo 挂 `/src`、
  `E2B_BASE_IMAGE=` 空、shipped seccomp 档 `deploy/seccomp/sandlock-worker.json`、cap 集与
  `deploy/scripts/arm-lane/x86-security.sh` 同形。
* 命令形状（三档只差 `--shape/--layout`；`--scratch` 指到本单自己的目录，避开别的 lane 的夹具）：

  ```
  sh tmp/k0s/n27-t7-lane.sh python3 -u tmp/k0s/probe_state_base_visibility.py \
      lane --shape <identity|synth-realroot> --layout <n27|legacy> \
      --scratch "$PWD/tmp/k0s/scratch/n27resid"
  ```

* 镜像 `e2b-sandlock-test:latest`（`75753e3fc0c3`，烘焙于约 13 小时前）；registry mirror
  `127.0.0.1:5080`（`sandlock-local-registry`，**不是本单创建的**）。
* 共享纪律：跑前 `docker ps` 确认没有别的 agent 的 lane 容器在跑（当时只有 registry /
  buildkit / mcp-gateway keepalive），本单用 `docker run --rm`（匿名容器、不 join 任何 compose
  project），**没有**碰别人的容器或卷，也没有 `compose down -v`。
* 没有碰集群：本单只读集群即可，而三档 lane 不需要它（`cluster` 模式没有跑）。

## 2. 三档原始输出

`<export>` 在下面写作 `<export>`，实际值是
`/Users/polus/project/ai/sandlock-e2b/tmp/k0s/scratch/n27resid/<shape>-<layout>`。
每一档的完整 stdout（含祖先链每一层的完整 listing）落在 `tmp/k0s/n27resid-*.log`；
这里逐字给出**每一条承载判据的行**（`CHECKER-LAYER` 只摘承载结论的那些层）。

**夹具不事后留存**：`lane` 跑完，夹具树连同**空的上层目录**一起被清掉（实测：用一个全新的
`--scratch` 根跑一档后，那个新建的 scratch 根整棵不存在）。所以判据只能读日志，复跑就是重跑
lane；下面引用的日志是本单实测留下的原始输出。

### ① `identity`（`E2B_PURE_ROOTFS=off`，默认形态）— `tmp/k0s/n27resid-identity-n27.log`

```
== date: 2026-09-27T01:10:30Z
LANE shape=identity (pure, N15 identity translation, emulated root) layout=n27
LANE state-base=<export>/state workspace=<export>/workspaces/sbx_probe
LANE route_b_active=True decline=None has_root=False chroot=/
CHECKER-PWD <export>/workspaces/sbx_probe
CHECKER-STATE-BASE <export>/state
CHECKER-WATCHED [".route-b", "_runtime", "_secrets", "state"]
CHECKER-CONTROL stat-canary OK size=4
CHECKER-CONTROL listdir-canary OK entries=2
CHECKER-STAT <export>/state DENIED errno=EACCES
CHECKER-STAT <export>/state/_runtime DENIED errno=EACCES
CHECKER-STAT <export>/state/.route-b DENIED errno=EACCES
CHECKER-STAT <export>/state/_runtime/.checkpoints DENIED errno=EACCES
CHECKER-LAYER <export>/workspaces/sbx_probe LISTED [".n27-probe-canary", "n27-checker.py"]
CHECKER-LAYER <export>/workspaces/sbx_probe CANARY-PRESENT
CHECKER-LAYER <export>/workspaces LISTED ["_migrate", "sbx_probe"]
CHECKER-LAYER <export> LISTED ["_builds", "_images", "_secrets", "_snapshots", "_templates", "_volumes", "state", "workspaces"]
CHECKER-LAYER <export> LEAK ["_secrets", "state"]
CHECKER-CHAIN layers=13 listed=13 reached-root=yes
CHECKER-VERDICT stat=PASS chain=FAIL
CHECKER-EXIT 1
lane: CHECKER-VERDICT stat=PASS chain=FAIL (exit 1)
```

值得单独指出：那四条 `EACCES` **不是权限位能解释的** —— 同一次沙箱运行里、同一个进程，把
`<export>/workspaces` **列出来了**（`LISTED ["_migrate", "sbx_probe"]`），而它与 `state` 是同一段
`mkdir(parents=True)` 造出来的兄弟目录（探针代码里只有 `_runtime` / `.route-b` 会在 **legacy** 档被
`chmod 0700`；state 基目录自己从不被 chmod/chown 到沙箱 uid）。一个列得出来、另一个连 `stat` 都
`EACCES` ⇒ 拒绝来自中介的策略，与 §11.2 那句"pure 形态是中介的策略拒绝，不是 ENOENT"逐字对上。

### ② `synth` + 真根（`E2B_PURE_ROOTFS=synth` + `E2B_REAL_ROOT=1`）— `tmp/k0s/n27resid-synth-realroot-n27.log`

```
== date: 2026-09-27T01:10:39Z
LANE shape=synth-realroot (pure, N16 synthesized root, real root) layout=n27
LANE state-base=<export>/state workspace=<export>/workspaces/sbx_probe
LANE route_b_active=True decline=None has_root=True chroot=/tmp/tmpmxsdu0ak-pure-rootfs/sbx_slot_0
CHECKER-PWD /home/user
CHECKER-STATE-BASE <export>/state
CHECKER-WATCHED [".route-b", "_runtime", "_secrets", "state"]
CHECKER-CONTROL stat-canary OK size=4
CHECKER-CONTROL listdir-canary OK entries=2
CHECKER-STAT <export>/state DENIED errno=ENOENT
CHECKER-STAT <export>/state/_runtime DENIED errno=ENOENT
CHECKER-STAT <export>/state/.route-b DENIED errno=ENOENT
CHECKER-STAT <export>/state/_runtime/.checkpoints DENIED errno=ENOENT
CHECKER-LAYER /home/user LISTED [".n27-probe-canary", "n27-checker.py"]
CHECKER-LAYER /home/user CANARY-PRESENT
CHECKER-LAYER /home LISTED ["user"]
CHECKER-LAYER / LISTED ["bin", "dev", "etc", "home", "lib", "lib64", "media", "mnt", "opt", "proc", "root", "run", "sbin", "srv", "tmp", "usr", "var", "workspace"]
CHECKER-CHAIN layers=3 listed=3 reached-root=yes
CHECKER-VERDICT stat=PASS chain=PASS
CHECKER-EXIT 0
lane: CHECKER-VERDICT stat=PASS chain=PASS (exit 0)
```

`<export>` 不在链上 —— 那三层的 `/` 是**沙箱自己的合成根**（`/src` 看不见、`/workspace` 是
`/home/user` 那份 workspace）。所以这一档不是"名字碰巧没命中"，而是"那一层走不到"。

### ③ 反例档：`identity` + 迁移前布局（`--layout legacy`）— `tmp/k0s/n27resid-identity-legacy.log`

```
== date: 2026-09-27T01:10:44Z
LANE shape=identity (pure, N15 identity translation, emulated root) layout=legacy
LANE state-base=<export> workspace=<export>/sbx_probe
LANE route_b_active=True decline=None has_root=False chroot=/
CHECKER-PWD <export>/sbx_probe
CHECKER-STATE-BASE <export>
CHECKER-WATCHED [".route-b", "_runtime", "_secrets", "identity-legacy", "state"]
CHECKER-CONTROL stat-canary OK size=4
CHECKER-CONTROL listdir-canary OK entries=2
CHECKER-STAT <export> OK mode=0o40755 ino=53114582
CHECKER-STAT <export>/_runtime DENIED errno=EACCES
CHECKER-STAT <export>/.route-b DENIED errno=EACCES
CHECKER-STAT <export>/_runtime/.checkpoints DENIED errno=ENOENT
CHECKER-LAYER <export>/sbx_probe LISTED [".n27-probe-canary", "n27-checker.py"]
CHECKER-LAYER <export>/sbx_probe CANARY-PRESENT
CHECKER-LAYER <export> LISTED [".route-b", "_builds", "_images", "_runtime", "_secrets", "_snapshots", "_templates", "_volumes", "sbx_probe"]
CHECKER-LAYER <export> LEAK [".route-b", "_runtime", "_secrets"]
CHECKER-LAYER <export-parent> LEAK ["identity-legacy"]
CHECKER-CHAIN layers=12 listed=12 reached-root=yes
CHECKER-VERDICT stat=FAIL chain=FAIL
CHECKER-EXIT 1
lane: CHECKER-VERDICT stat=FAIL chain=FAIL (exit 1)
```

`<export-parent> LEAK ["identity-legacy"]` 是 §4.1 那处修复带来的**新增但正确**的命中：legacy
夹具里 state base **就是**树根，它自己的名字当然在上一层列得出来（真实 pre-N27 部署同理）。
它的存在不改变任何一档的 verdict（这档本来就 FAIL）。

### ④ 附带：`synth` 配模拟根 —— 形态不可服务（`tmp/k0s/n27resid-GREEN-synth-emulated-n27.log`）

```
== date: 2026-09-27T01:10:08Z
LANE shape=synth-emulated (pure, N16 synthesized root, emulated root) layout=n27
LANE route_b_active=True decline=None has_root=True chroot=/tmp/tmp18600npe-pure-rootfs/sbx_slot_0
LANE cwd-retry=/home/user (host path refused: SlotRefusal: instance exec failed: process error: instance is closed ...)
LANE VACUOUS: the checker never ran (SlotRefusal: instance exec failed: ...)
exit=2
```

## 3. 判据的可靠性（"能红"的证据）

| 判据 | 拿什么反证它 | 结果 |
|---|---|---|
| ① 四个 `stat` 必须失败 | legacy 档：状态目录**本身**可 `stat`（`OK mode=0o40755 ino=53114582`） | 红（`stat=FAIL`）⇒ 非恒真 |
| ② 祖先链不能出现那些名字 | legacy 档：`<export>` 列 `_runtime`/`.route-b`/`_secrets`；identity 档：`<export>` 列 `state`/`_secrets` | 两档都红 ⇒ 非恒真 |
| ② 的"看不见 vs 列不出来" | identity 档每一层都 `LISTED`（13/13 层列得出来）⇒ 这一档不是靠"到处 EACCES"过关的 | 成立 |
| 正对照（防"到处都拒=干净"） | canary 每档都 `stat` 到（`size=4`）且 workspace 层都列出它（`CANARY-PRESENT`） | 三档都成立 |
| 形态开关本身是活的 | 同一份夹具、同一份 fixture 代码，只换 `--shape` ⇒ `exit 0`（synth） vs `exit 1`（identity） | 成立 |

## 4. 探针自身两处假闸：RED→GREEN

### 4.1 (a) ② 的祖先链半边只认四条硬编码名字

**问题**：`stat` 半边跟着 `--state-base` 走，chain 半边却拿 `STATE_NAMES = ("state", "_runtime",
".route-b", "_secrets")` 这个**常量**去比对。只要部署把 `E2B_STATE_BASE` 指到一个基名不在那四条
里的目录，`<export>` 照旧列着那个名字，探针却报 `chain=PASS` —— 看着在守那条残差，其实守的是
四个字面量。

**RED**（同一夹具、只改基名；夹具在 `tmp/k0s/scratch/n27resid/holes2.*/`，
日志 `tmp/k0s/n27resid-RED-hardcoded-names-{state,platform}.log`）：

```
[state]    exit=1  CHECKER-STAT .../exp/state DENIED errno=ENOENT  ×4
                   CHECKER-LAYER .../exp LEAK ["state"]        CHECKER-VERDICT stat=PASS chain=FAIL
[platform] exit=0  CHECKER-STAT .../exp/platform DENIED errno=ENOENT ×4
                   （没有任何 LEAK 行，虽然 <export> 列出 ["platform", "workspaces"]）
                   CHECKER-VERDICT stat=PASS chain=PASS
```

`<export>` 明明列着平台状态目录的名字，探针答 `PASS/PASS`、`exit 0`。

**GREEN**（同一夹具重跑；`tmp/k0s/n27resid-GREEN-hardcoded-names-{state,platform}.log`）：

```
[state]    exit=1  CHECKER-WATCHED [".route-b", "_runtime", "_secrets", "state"]
                   CHECKER-LAYER .../exp LEAK ["state"]                  CHECKER-VERDICT stat=PASS chain=FAIL
[platform] exit=1  CHECKER-WATCHED [".route-b", "_runtime", "_secrets", "platform", "state"]
                   CHECKER-LAYER .../exp LEAK ["platform"]               CHECKER-VERDICT stat=PASS chain=FAIL
```

**改法**：`watched_names(state_base)` = 四条字面量 ∪ `basename(state_base)`，chain 半边用它比对，
并把这一轮真正在守的名字打成 `CHECKER-WATCHED`（证据行，也是"判据跟着 `--state-base` 走"的凭证）。
只放宽**不让它失明**，不放宽任何真实命中：三档 lane 的 verdict 改前改后逐字相同
（`tmp/k0s/n27resid-prefix-*.log` 是改前那一代）。

### 4.2 (b) lane 崩在 checker 之前也走 `exit 1`

**问题**：`lane_main` 的两条 retry 路径（`cwd=ws` 失败 → `cwd=/home/user`；以及 `code==125`）
在**第二次**也失败时让异常穿透到顶层 ⇒ traceback + 解释器自己的 `exit 1`。而本探针对外最要紧的
一句契约是"`--layout legacy` 是反例、它回来 `1`"。于是**崩溃与"反例成立"同一个退出码** —— 只读
退出码的人（或脚本）会把崩溃读成反例，正是本单第 ③ 条要防的那类"恒真"。

**RED**（`tmp/k0s/n27resid-RED-synth-emulated-n27.log`）：`exit=1`，`grep -c CHECKER-VERDICT` = **0**
（一条判据行都没有，只有 traceback）。

**GREEN**（`tmp/k0s/n27resid-GREEN-synth-emulated-n27.log`）：`LANE VACUOUS: the checker never
ran (SlotRefusal: ...)`、`exit=2`、traceback 行数 = 0。**改法**：两条 retry 的兜底都走
`_vacuous_unreachable()` → `VACUOUS(2)`，与"缺凭据/测不成"的既有口径一致。

### 4.3 看过但没有改的

* `CHECKER-CHAIN ... reached-root=yes` 这一句是无条件打印的，而 `CHECKER-CHAIN BROKEN` 那个分支
  对绝对路径的 `cwd` 恒不可达（`os.path.dirname("/") == "/"`，而循环在 `layer == "/"` 就先
  `break`）。也就是说这句是"恒真的总结行"而不是判据本身（判据在 `chain_vacuous`）。**没改**：
  改了也无法用夹具变红，属于添加不可证伪的代码；记在这里，免得下一个人把它当证据用。
* 正对照只覆盖"能不能 `stat`/`listdir`"，不覆盖"ancestor 层是否**应该**可列" —— 后者是形态本身
  的性质（identity 就该列得出来），不是探针能判的。

## 5. 要让**默认**形态也消掉：唯一一步 + 代价/风险

**唯一一步**：把 `E2B_PURE_ROOTFS` 的默认值从 `off` 切到 `synth`
（`envd_service/config.py` 的 `Settings.pure_rootfs` 默认 `"off"`；两份 k8s 清单今天不设它）。

代价/风险（至少三条，逐条都能在本单的输出里对上）：

1. **pure 形态每沙箱一份骨架目录**：`<workspace base>/_pure_rootfs/<id>`（`gateway_common.paths.
   PURE_ROOTFS_DIR_NAME`）—— 普通目录 + bind 系统目录 + 整棵 `/dev` + `pivot_root`。即每个 pure
   沙箱多一棵目录树和一次 `pivot_root`；本单 ② 档的 `chroot=/tmp/tmp…-pure-rootfs/sbx_slot_0`
   就是它。
2. **依赖 `E2B_REAL_ROOT=1`**：`synth` 配 `REAL_ROOT=0` **结构性不可服务**（骨架是空的，`/bin/sh`
   不在里面）。本单 ④ 档实测 `SlotRefusal: instance is closed`；N16 还加了成对守卫，
   这种配置会在 worker 启动时被 loud 拒。即切默认值必须同时保证真根开着。
3. **依赖 worker seccomp 档已应用**：`E2B_REAL_ROOT` 那条路要 `mount/umount2/pivot_root`，档里
   必须有无门闩的这三条（N35）；漏了会被 worker 的启动自检当场拒（不是静默降级）。
4. 影响面：生产两条清单都设 `E2B_BASE_IMAGE` ⇒ 生产走 **image-rootfs**（有根形态，本来就干净），
   **不受**这次切换影响；受影响的只有 pure 形态部署（无基镜像）。见 N16 / N35。

**本轮没有改默认值**（任务要求），改动只落在文档的限定语与探针自身。

## 6. 文件清单与提交

提交 **`84e21c8`**（`N27(9/9): identity 残差按 lane 三档复核，并收口探针自身两处假闸`）：

| 文件 | 改了什么 |
|---|---|
| `tmp/k0s/probe_state_base_visibility.py` | §4 的两处修复 + 模块 docstring 两段（`CHECKER-WATCHED`、崩溃=2） |
| `docs/deploy-clusters.md` §11.2 | 判据 ② 补"以及 `<state base>` 的 basename"；新增 2026-09-27 复核段（三档实测 + 默认形态的唯一一步与代价）；`synth-emulated` 那行退出码从错位的表头对齐成 `2（LANE VACUOUS）` |
| `docs/open-issues.md` | N27 行补 2026-09-27 复核结论与限定语；文末"N27 identity 残差"那行把"下次核一遍"改成已核 + 待裁定 |

原始输出（未提交，`tmp/` 按 `.gitignore:5` 忽略；留在盘上）：

* 三档：`tmp/k0s/n27resid-{identity-n27,synth-realroot-n27,identity-legacy}.log`
* 改前那一代（用于证明"修复不改变任何 verdict"）：`tmp/k0s/n27resid-prefix-*.log`
* 假闸 RED/GREEN：`tmp/k0s/n27resid-{RED,GREEN}-synth-emulated-n27.log`、
  `tmp/k0s/n27resid-{RED,GREEN}-hardcoded-names-{state,platform}.log`
* 夹具：手工造的两组假闸夹具还在（`tmp/k0s/scratch/n27resid/{holes2.*,green.*}/`，它们没交给沙箱）；
  三档 lane 的夹具按 §2 的说明在跑完时被一起清掉，所以那三档的证据只在日志里
  （`tmp/k0s/n27resid2-identity-n27.log` 就是"新 scratch 根跑完整棵消失"那次观测）。

**提交范围**：走**索引提交**而不是 `git commit -- <paths>`。原因：同一个
`docs/open-issues.md` 的工作树里当时另有并行 agent 未提交的 O1 行改动，而 pathspec/`--only`
模式取的是**工作树内容**，会把它一并带进本提交。核对：`git diff --cached --name-only` = 上表
三个文件；`git show 84e21c8 -- docs/open-issues.md` 的 hunk 数 = 2，两条都是 N27 行。
其它 agent 的改动（`control_plane/*`、`docs/HANDOFF.md`、`tests/unit/test_pause_quota.py`、
`docs/open-issues.md` 的 O1 行）一律未进本提交，也没有被 revert。

## 7. 担忧 / 未做

* **没跑单元/契约套件**：本提交没碰 `envd_service/`、`control_plane/`、`tests/` 或任何部署清单，
  验收就是 §2/§4 那五条 lane 输出。若审查要求，`sh tmp/k0s/n27-t7-lane.sh` 那三行就是复跑命令。
* **`cluster` 模式没有复核**：本单只读集群即可，且 §2 的三档已覆盖判据；§11.2 表里的
  `image-rootfs` 那一行仍是 2026-09-26 的集群记录，不是本轮复测。
* **默认形态仍是"半干净"**：`<export>` 一层能列出 `state` / `_secrets` **名字**这件事今天依然
  成立，且**默认**形态就是它。这不是 N27 引入的（迁移前同一形态在 `..` 就列 `_runtime`），
  但只要默认值不切，这条残差就得继续挂着 —— 建议由用户就 §5 那一步拍板。
* **生产不受影响**：两条生产清单都有 `E2B_BASE_IMAGE` ⇒ image-rootfs（有根），实测/登记都干净；
  §5 的取舍是 pure（无基镜像）部署的事。
