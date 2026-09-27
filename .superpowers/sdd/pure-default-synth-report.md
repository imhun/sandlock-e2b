# pure 形态的默认根翻到合成根（2026-09-27）

用户裁定"都做了"：把 **pure 形态的默认根从 `identity` 翻到 `synth`**（N16 的合成骨架 + 真根），
消掉 N27 在**默认档**上剩下的那条残差。本单落地了默认值、成对耦合的 `E2B_REAL_ROOT`、配置守卫的
新语义、逐处影响面，以及两档形态门禁与两态 lane 的复跑。

**一句话结论**：`E2B_PURE_ROOTFS` 默认 `synth`；`E2B_REAL_ROOT` 未显式设置时**跟着合成根走**
（选 B：成对耦合）；显式 `E2B_REAL_ROOT=0` + `synth` **仍然**被 `check_pure_rootfs_pairing` 当场拒绝；
**退回杆是一句话：`E2B_PURE_ROOTFS=off`**。image-rootfs 形态（两套生产清单）**逐字节不变**。

## 0. 结论摘要

| # | 问题 | 答案 |
|---|---|---|
| 1 | 默认值改成什么了？ | `pure_rootfs` 默认 `off → synth`（空值读作"未设"= 默认）。`real_root` 变成**三态**：`1`/`0` 显式优先，**未设 ⇒ 有合成根就装真根**（`resolve_real_root`）。 |
| 2 | 为什么选 B（成对耦合）不选 A（两个默认一起翻）？ | A 会让**没写这个键的 image 形态栈**（`deploy/stack/docker-compose.prod.yml`、`deploy/compose/*.yml`）从模拟根换到 `pivot_root`——超出"pure 的默认根"这次裁定的范围，且与"生产车队不受影响"矛盾。B 之下 image 形态**逐字节不变**，且退回杆仍是**一个**键。 |
| 3 | 显式矛盾还拒吗？ | **拒**。`synth` + 显式 `E2B_REAL_ROOT=0` ⇒ `RuntimeError`、exit 1，句子里带着退路（`PURE_ROOTFS_WITHOUT_REAL_ROOT_ERROR`）。守卫只把 `None`（未设）当"成对"。 |
| 4 | 影响面有没有"必须显式加键"的地方？ | 有，且只有一处形状必需：`deploy/compose/docker-compose.yml` 的 `envd`（不带 base image + 跑 Docker 默认 seccomp 档）⇒ 已加 `E2B_PURE_ROOTFS: ${E2B_PURE_ROOTFS:-off}`。另外 **6 个形态 lane** 显式写了 `E2B_REAL_ROOT=0`，必须同时点名 `off`（否则被守卫拒绝）⇒ 已逐处加。 |
| 5 | 门禁有没有回归？ | gate A（image 形态，全量）`2119 passed, 10 skipped, 3 xfailed`，**0 failed**；gate B（pure identity，全量）`2112 passed, 17 skipped, 3 xfailed`，**0 failed**；pure 两态 `tests/contract`：`synth+真根` 380/5、`identity` 379/6，**0 failed**。`tests/unit` 本机 `14 failed, 1605 passed`，**failed 名单 = 已知 14 条逐条同名**。 |
| 6 | 判据能红吗？ | RED（`ImportError: cannot import name 'resolve_real_root'`）→ GREEN（24 passed）；**三个变异各红一次**：默认改回 `off` ⇒ 6 条红；守卫放过显式矛盾 ⇒ 1 条红；拆掉耦合 ⇒ 5 条红。日志 `tmp/puresynth/mutation-{1,2,3}-*.log`。 |

## 1. 默认选择与理由（B：成对耦合）

**两套生产清单都设 `E2B_BASE_IMAGE`**（`deploy/k8s/worker.yaml:507`、`deploy/stack/docker-compose.prod.yml:145`），
所以"pure 的默认根"名义上碰不到它们；但 `E2B_REAL_ROOT` 的**默认值**是 deployment-wide 的，把它一起
翻成 `on`（方案 A）就会让**没有显式写这个键的 image 形态栈**也换形态。逐处清点：

| 若选 A（`E2B_REAL_ROOT` 全局默认 on）会翻形态 | 它现在是什么 |
|---|---|
| `deploy/stack/docker-compose.prod.yml`（`worker-1`/`worker-2`：`:145` 设 base image，**无** real_root 键） | 模拟根（`E2B_REAL_ROOT` 未设） |
| `deploy/compose/docker-compose.prod.yml:114`、`docker-compose.multinode.yml:75/108/195/282` | 同上 |
| 池 worker（`deploy/compose/docker-compose.autoscale.yml:166`、`autoscaler/backends/local.py:48`） | 同上（池的 JSON 也没有这个键） |
| 各 lane（`deploy/scripts/test-prod-shaped.sh:207/240`、`run-f31.sh`、`final-verify.sh`、`smoke-prod-worker.sh`、`phase1-probe2.sh`） | 同上 |

也就是说 A 的影响面是"整个模拟根形态的默认面"，而这次裁定的对象只有 pure。B 用一条解析规则
（`envd_service/config.py::resolve_real_root`）把两者绑在一起：**有合成根的箱装真根，其余保持原样**。
副产品是退回杆更干净——`E2B_PURE_ROOTFS=off` 之后"没有合成根可跟随"，`real_root` 的默认随之回 `off`，
所以**回到旧形态是这一个键，不是两个**（这是简报里"退回杆必须是一句话"的硬要求，A 做不到）。

## 2. 代码改动（最小面）

| 文件 | 改了什么 |
|---|---|
| `envd_service/config.py:80` | 新增 `_real_root_from_env()`：把 `E2B_REAL_ROOT` 解析成**三态**（`None` = 未设 ≠ `off`）。 |
| `envd_service/config.py:212` | `pure_rootfs` 默认 `"synth"`；空值/空白读作未设（本仓库其他 env 助手的惯例）。字段 docstring 里写明**代价三条**与**退回杆一句话**。 |
| `envd_service/config.py:298` | `real_root: bool \| None`（三态），docstring 说明"未设 = 跟着合成根"。 |
| `envd_service/config.py:605` | `PURE_ROOTFS_WITHOUT_REAL_ROOT_ERROR` 的文本改掉"unset 就是 identity"这句已失效的话，换成两条出路（去掉显式 `=0`，或 `E2B_PURE_ROOTFS=off`）。 |
| `envd_service/config.py:824` | 新增 `resolve_real_root(settings, *, pure_shape)`：显式值优先；未设 ⇒ `pure_shape and pure_rootfs == "synth"`。 |
| `envd_service/config.py:869` | `check_pure_rootfs_pairing` 只在 `real_root is False`（**显式** off）时拒绝；`None`/`True` 都是"成对"。 |
| `envd_service/executors/factory.py:228` | `real_root=resolve_real_root(settings, pure_shape=base_image is None)` —— 形态判定与 `pure_rootfs_dir` 同一个地方决定。 |
| `tests/security/conftest.py:225-239` | `route_b_sandbox` 不再自带一份开关规则，改为**走产品自己的 `Settings` + `resolve_real_root`**（否则 lane 会静默测另一个形状）。 |
| `tests/security/test_uid_isolation.py:52`、`tests/security/test_pure_root_errno_contract.py:62`、`tests/contract/test_shared_volume_relative_cwd.py:48` | 三处"镜像默认值"跟着改成 `synth`。 |
| `tests/unit/test_pure_rootfs_config.py` | 重写：默认值/退回杆/三态/耦合/守卫，11 → 24 条。 |
| `tests/unit/test_worker_env_key_sets.py:250` | 新增 `EXTRA_CLASSES["demo_pure_rootfs_lever"]` 并进 `COMPOSE_DEMO` 白名单（N45 那套"每个 worker 栈的键集必须被命名"的纪律）。 |
| `deploy/compose/docker-compose.yml:103` | `envd` 加 `E2B_PURE_ROOTFS: ${E2B_PURE_ROOTFS:-off}`（见 §4 第 3 行）。 |
| 6 个形态 lane（见 §4 第 4 行）+ `n27-t7-lane.sh:36` | 显式 `E2B_PURE_ROOTFS=off` / 转发。同 4 个文件（3 个 gate + n27 lane）还各改了一行 `cd`/`REPO` 深度（见 §7.1）。 |

## 3. TDD：RED → GREEN，以及三个变异

**RED（先红）**：新用例先落库，运行得到
`ImportError: cannot import name 'resolve_real_root' from 'envd_service.config'`（收集期就红，
因为默认值与解析函数都还不存在）。

**GREEN**：实现之后 `tests/unit/test_pure_rootfs_config.py` → **24 passed**。
另跑相邻面 `test_real_root_gate.py / test_executor_factory_sandlock_health.py / test_net_isolation_config.py /
test_pure_rootfs_shape.py / test_platform_disk.py` → 72 passed / 1 failed，唯一失败是
**本机既有红**（`test_the_probe_asks_for_the_pivot_root_this_architecture_has`：macOS 没有 `libc.so.6`）。

**变异（三条，各真跑一次，日志留在 `tmp/puresynth/`）**：

| # | 变异 | 结果（逐字） | 日志 |
|---|---|---|---|
| M1 | 默认改回 `off`（`or "synth"` → `or "off"`） | `6 failed, 18 passed`：`test_the_switch_defaults_to_synth`、`test_an_empty_value_is_the_default_not_the_lever`、`test_the_coupled_default_follows_the_synthesized_root`、`test_the_factory_puts_the_pure_shape_on_its_own_root_by_default`、`test_the_coupled_default_leaves_the_image_shape_where_it_was`、`test_the_security_helper_defaults_to_the_synthesized_root` | `mutation-1-default-back-to-off.log` |
| M2 | 守卫放过显式矛盾（`if real_root is not False: return` 换成裸 `return`） | `1 failed, 23 passed`：`test_the_explicit_contradiction_is_refused_by_name`（`DID NOT RAISE RuntimeError`） | `mutation-2-guard-lets-contradiction-through.log` |
| M3 | 拆掉耦合成对（`resolve_real_root` 未设时返回 `False`） | `5 failed, 19 passed`：`test_the_coupled_default_follows_the_synthesized_root`、`test_the_factory_puts_the_pure_shape_on_its_own_root_by_default`、`test_the_factory_hands_over_the_root_once_the_switch_is_synth`、`test_the_security_helper_mirrors_the_shape_switch`、`test_the_security_helper_defaults_to_the_synthesized_root` | `mutation-3-coupling-broken.log` |

每次变异之后都用 `.orig` 副本还原并复核（`diff -q` 相同 + `24 passed`），所以 RED 不是"实现没回来"。

## 4. 影响面（逐处 `文件:行号` 与结论）

**判据**：`pure_rootfs=synth` 只在"这个箱没有 base image"时生效（`sandlock.py::_synthetic_rootfs`
对 image 箱直接返回 `None`）；`resolve_real_root` 也按同一个 `pure_shape` 解析。

| # | 面（文件:行号） | 翻默认后 | 结论 |
|---|---|---|---|
| 1 | `deploy/k8s/worker.yaml:507`（`E2B_BASE_IMAGE`）+ `:457`（`E2B_REAL_ROOT=1`）、`deploy/stack/docker-compose.prod.yml:145`、`deploy/compose/docker-compose.prod.yml:114`、`docker-compose.multinode.yml:75/108/195/282`、`docker-compose.autoscale.yml:96` | **不变**。image 箱既不吃 `pure_rootfs`（`_synthetic_rootfs` 返回 `None`），`E2B_REAL_ROOT` 也没被显式设置 ⇒ 仍是模拟根；k8s 显式 `=1` ⇒ 仍走真根 | 无需加键。**生产车队不受影响**（两套清单都设 base image + 仓库 seccomp 档） |
| 2 | 池：`deploy/compose/docker-compose.autoscale.yml:166`（`E2B_AS_WORKER_ENV` 里带 `E2B_BASE_IMAGE`）、`autoscaler/backends/local.py:48`（`_env` 默认字典；容器 `seccomp=unconfined` 在 `:141`） | 池的默认 worker **带 base image ⇒ 不变**；**手搭的、不带 base image 的池**会走 `synth`，靠非特权 userns 建根 | 无需改键（`autoscaler/**` 不在本单可改范围）。**退回杆 = 在 `E2B_AS_WORKER_ENV` / `worker_env=` 里加 `"E2B_PURE_ROOTFS": "off"`**；宿主禁非特权 userns 时建箱会**按名字**拒绝，不会静默降级 |
| 3 | **不带 base image 的 compose 栈**：`deploy/compose/docker-compose.yml:82`（`envd` 服务；无 `E2B_BASE_IMAGE`，也**没有** `seccomp=` 覆盖 ⇒ 跑 Docker 自己的默认档） | **会走 `synth`** | **必须加键**（已加 `:103`）：实测同一个镜像里 `_real_root_capability()` 在 Docker 默认档下答 `unshare(CLONE_NEWUSER): Operation not permitted`、在仓库档下答 `''`(ok) ⇒ 不加键的话这个示例栈的每个建箱都会拒绝。要跑新默认就把 `deploy/seccomp/sandlock-worker.json` 装到该服务上并删掉这个键 |
| 4 | 形态 lane 里**显式**写 `E2B_REAL_ROOT=0` 的：`deploy/scripts/acceptance/gateA-full.sh:40`、`gateB-full.sh`、`gateB-pure-rootfs.sh`（state 0）、`x86-security-one.sh:19`、`x86-run-py.sh:19`、`deploy/scripts/arm-lane/x86-security.sh:31` | 这些命令在翻默认后**会被守卫拒绝**（显式 `=0` + 默认 `synth`） | **必须加键**（已逐处加 `E2B_PURE_ROOTFS=off`）。注：`gateA`/`x86-*` 是 image 形态，`pure_rootfs` 对它们本来无意义，但守卫是 **shape-blind** 的（它只看得到 deployment-wide 设置，看不到每个 create 的 `baseImage`）——见 §7 的担忧 |
| 5 | N27 探针 lane：`deploy/scripts/acceptance/n27-t7-lane.sh:35`（`E2B_BASE_IMAGE=` 空 ⇒ pure） | 原来靠"默认即 identity"拿到 identity 档 | 改成**转发** `E2B_PURE_ROOTFS`（`:36`），由调用者与探针自己的 `--shape` 对齐；未设 ⇒ 新默认 |
| 6 | `deploy/scripts/test-prod-shaped.sh:207/240`、`run-f31.sh`、`final-verify.sh`、`smoke-prod-worker.sh:45`、`phase1-probe2.sh:12` | image 形态 + 没设 `real_root` ⇒ **不变**（B 的功劳） | 无需加键。（`test-prod-shaped.sh` 因为 `${VAR:-default}` 的写法**表达不了**空 base image，要跑 pure 得用 `gateB-full.sh`） |

## 5. 形态门禁与两态 lane：数字与逐条归因

跑法（本单用的命令；日志在 `tmp/puresynth/`，`tmp/` 是 gitignored）：

```
sh deploy/scripts/acceptance/gateA-full.sh                  tmp/puresynth/gateA.log
sh deploy/scripts/acceptance/gateB-full.sh                  tmp/puresynth/gateB-off.log
sh deploy/scripts/acceptance/gateB-pure-rootfs.sh 1 tmp/puresynth/pure1-contract.log tests/contract --ignore=…（两个 XFS 文件）
sh deploy/scripts/acceptance/gateB-pure-rootfs.sh 0 tmp/puresynth/pure0-contract.log tests/contract --ignore=…（同上）
E2B_TEST_TMP_ROOT=$PWD/tmp/puresynth/test-runtime tmp/testenv/bin/python -m pytest tests/unit -q -rf
```

| 档 | 结果（末行逐字） | 基线 | 与基线的差 / 归因 |
|---|---|---|---|
| **gate A**（image 形态，全量） | `2119 passed, 10 skipped, 3 xfailed, 13514 warnings in 516.26s (0:08:36)`，**0 failed** | §7 表：`2022 passed, 10 skipped, 3 xfailed`（revision `1374e87`，收集 2035） | skip/xfail **逐条同名**；收集数 +97 = **+84**（`1374e87 → 4396915` 之间落库的其他任务用例，N27/N45/N46/checkpoint）+ **+13**（本单 `test_pure_rootfs_config.py`：11 → 24）。0 failed 不变 |
| **gate B off**（pure `identity` + `E2B_PURE_ROOTFS=off`，全量） | `2112 passed, 17 skipped, 3 xfailed, 12900 warnings in 480.59s (0:08:00)`，**0 failed** | §7 表：`2015 passed, 17 skipped, 3 xfailed`（同一 revision） | skip/xfail **逐条同名**；收集数同样 +97（同一个 2132）⇒ 两个门禁的收集面一致，纯差异只在形状 |
| **pure `=1`（新默认形状：合成根 + 真根）** | `380 passed, 5 skipped, 2689 warnings in 139.88s (0:02:19)`，**0 failed / 0 error** | §7 里同态的全量是 `2019 passed, 16 skipped`（不同目标集，不能直接比）；本行是 `tests/contract` | 与 `=0` 档的唯一差是 1 条（见下） |
| **pure `=0`（identity，退回档）** | `379 passed, 6 skipped, 2612 warnings in 138.51s (0:02:18)`，**0 failed / 0 error** | 同上 | 差的那 1 条是 `tests/contract/test_shared_volume_relative_cwd.py`：identity 档 skip（"两个别名要有一个根才可解析"），合成根档**跑并且过** ⇒ 两态自洽，不是回归 |
| **pure「默认档」**（**不设** `E2B_PURE_ROOTFS` / `E2B_REAL_ROOT`，只设 `E2B_BASE_IMAGE=`）：`tests/security/test_pure_root_errno_contract.py tests/security/test_uid_isolation.py tests/contract/test_shared_volume_relative_cwd.py` | `6 passed in 0.53s`（`tmp/puresynth/pure-default-contracts.log`） | 这 6 条在改动前会按 identity 跑（其中 2 条 `test_shared_volume_relative_cwd` 会 skip） | 默认档真的落到合成根上：`test_pure_root_errno_contract` 里那句 `assert executor._has_sandbox_root is synth`（`synth` 现在 = 未设 ⇒ True）**过**，且它前面已经用 `require_sandlock` 建过箱并跑过命令 —— 也就是"没有这两个键、只有 `E2B_BASE_IMAGE=`"时，沙箱是**合成根 + 真根**建起来的 |
| **`tests/unit`（本机 macOS，py3.14）** | `14 failed, 1605 passed, 11 skipped, 2 warnings` | 同机、同 `E2B_TEST_TMP_ROOT` 约定下、动手前的 HEAD（`4396915`）worktree：`17 failed, 1588 passed, 12 skipped`（`tmp/puresynth/baseline-unit-head.log`） | **失败面 -3**：3 条 `test_migrate_state_base_script.py` 只在 worktree 里红（渲染出的 Job 清单第 50 行 `yaml.scanner.ScannerError`，主树里绿 —— worktree 环境差异，本单没碰那个脚本/清单，见 §7.7）；**+17 passed** = +13 本单新用例 + 3 条上面那 3 个 + 1 条由 skip 转 pass（`test_route_b_wiring.py:243` 的 "fork submodule not checked out"：HEAD 是 gitlink，worktree 里 `third_party/sandlock` 是空目录，主树是完整 checkout ⇒ skip 12→11）。**最终 14 条逐条同名**：`test_priv_helpers.py` × 11、`test_real_root_gate.py::test_the_probe_asks_for_the_pivot_root_this_architecture_has` × 1、`test_xfs_quotactl_backend.py` × 2。本单第一版曾多红 1 条 `test_worker_env_key_sets.py::test_every_worker_stack_declares_the_fleets_keys_except_a_named_whitelist`——那是 §4 第 3 行的 compose 加键触发的**键集审计**，已按该文件的纪律加 `EXTRA_CLASSES["demo_pure_rootfs_lever"]` 转绿 |

**收集面归因（容器内 `pytest tests --collect-only`，同一镜像同一组 `--ignore`）**：
`4396915`（本单动手前的 HEAD）收集 **2119**；本单工作树收集 **2132**（= 2119 + 13）。
两份清单 `diff` 之后**唯一的差异全部落在 `tests/unit/test_pure_rootfs_config.py`**（旧 11 条里 3 条改名/替换、
新增 13 条），也就是"没有别的用例被我加进来或掉出去"。§7 基线的 2035 与 2119 之间的 +84 是那之后
其他任务的用例（不经本单）。

**默认档的配置解析（容器内实测，同一个镜像 + `E2B_BASE_IMAGE=`）**：

| 环境 | `Settings().pure_rootfs` / `real_root` | `resolve_real_root(pure)` / `(image)` | `create_app` |
|---|---|---|---|
| 两个形状键都不设 | `synth` / `None` | `True` / `False` | ok |
| `E2B_PURE_ROOTFS=off`（+`E2B_REAL_ROOT=0`） | `off` / `False` | `False` / `False` | ok（退回杆可用） |
| `E2B_PURE_ROOTFS=synth` + `E2B_REAL_ROOT=0` | `synth` / `False` | `False` / `False` | **拒绝**（逐字：`E2B_PURE_ROOTFS=synth without E2B_REAL_ROOT=1: … Drop the explicit E2B_REAL_ROOT=0 so the pair travels together (that is the default), or set E2B_PURE_ROOTFS=off to keep the pure shape on N15's identity root.`） |

**lane 镜像与同源**：`e2b-sandlock-test:latest` == `e2b-sandlock-test:task12cur`
（同一 image id `sha256:75753e3fc0c3…`，即 §7 那批数字用的镜像），且容器里安装的
`sandlock/_sdk.py` 与仓库 `wheels/fork/sandlock-0.9.0b0-…x86_64.whl` 里的同名文件 sha256 相同
（`af83dbb7ba4170bd…`）⇒ lane 是"同一根 fork wheel + 本单的 E2B 源码"。

## 6. 退回杆怎么用（一句话）

**`E2B_PURE_ROOTFS=off`** —— pure 形态回到 N15 的 identity 根；因为"没有合成根可跟随"，
`E2B_REAL_ROOT` 的耦合默认也随之回到 `off`。三个场景：

* **某个 pure 宿主没有 `deploy/seccomp/sandlock-worker.json`**（例如跑 Docker 默认档的开发机）：
  在 worker 的 env 里加 `E2B_PURE_ROOTFS=off`（`deploy/compose/docker-compose.yml` 已经这么写了），
  或把仓库档装上再跑新默认。
* **本地池**（手搭、不带 base image）：`backend = DockerPoolBackend(..., worker_env={"E2B_PURE_ROOTFS": "off"})`
  或 `E2B_AS_WORKER_ENV` 里加同名键。
* **只想退回并且明确要求模拟根**（image 形态）：`E2B_REAL_ROOT=0`。注意它**必须**同时点名
  `E2B_PURE_ROOTFS=off` 才能在 pure 宿主上启动 —— 只写前者会被守卫按名字拒（这正是"显式矛盾仍被拒"）。
* 反向 opt-in（在带默认档的示例栈上跑新形状）：`E2B_PURE_ROOTFS=synth`（先装仓库 seccomp 档）。

## 7. 未做 / 担忧（需要 controller 看的地方）

1. **我改了 `deploy/scripts/**`（7 个文件）、`deploy/compose/docker-compose.yml`，以及
   `docs/production-deployment-requirements.md`** —— 简报的"你只碰"清单里只写了
   `deploy/k8s*/**`、`deploy/compose/**`、三个 docs 与 `tests/**`。理由：① 简报第 3 条要求**跑**
   `gateA/gateB-full.sh`，而这两个脚本（连同 `gateB-pure-rootfs.sh`）从 `tmp/` 提升到
   `deploy/scripts/acceptance/` 时 `cd .../../..` 没跟着改成 `../../..`，`sh deploy/scripts/acceptance/
   gateA-full.sh <log>`（README 与 §7 写的调用法）会 cd 到 `deploy/`，既写不出日志、又会把 `deploy/`
   挂成 `/workspace`；不改这三个脚本就没法给出本单要求的两档数字（`n27-t7-lane.sh:22` 的 `REPO=…/../..`
   是同一个 bug，它会把 `deploy/` 挂成 `/src`，也一并修成三级）。② 简报第 2 条要求的"哪些必须显式
   加键"里，那 6 个 lane 与 demo compose 是**必须**加键的对象，否则它们要么被守卫拒绝、要么在缺
   seccomp 档的宿主上建不出箱。改动都很小（一行 env / 三行 cd 注释），**没有**碰其它 agent 的
   `control_plane/**` 与他们的文档。
2. **同一个 cd bug 还有两处没修**：`deploy/scripts/acceptance/phase2.sh:4`、
   `probe-pure-restore-synthroot.sh:13`（后者还在 cd 之后用 `deploy/scripts/acceptance/...` 的相对路径
   调 `gateB-pure-rootfs.sh`，所以是双重失效）。它们不在本单要跑的门禁里，留着给 controller 决定。
   同一族的 `x86-run-py.sh:5` / `x86-security-one.sh:5` 用的是正确深度（`../../..`）。
3. **守卫是 shape-blind 的**（有意为之，但要说清）：`check_pure_rootfs_pairing` 看不到每个 create 的
   `baseImage`，所以"image-only"的 lane/部署只要显式写 `E2B_REAL_ROOT=0`，就必须同时写
   `E2B_PURE_ROOTFS=off`，哪怕合成根对它永远不会生效。反过来，如果按 `settings.base_image` 放行，
   那么"`E2B_BASE_IMAGE` 配了、但某个箱没带 `baseImage`"的 pure 箱就会悄悄落回"空骨架 + 模拟根"，
   正是守卫要拦死的那条路。我选了保守的一侧（更吵、但不漏）。
4. **空值语义**：`E2B_PURE_ROOTFS=`（设为空）现在读作"未设"= `synth`（本仓库 `env_int`/`env_json`
   等助手的惯例）。要 identity 必须写 `off`。这写进了字段 docstring、`pure-shape-decision.md` §7 和
   §2.4.11。风险：任何用 `E2B_PURE_ROOTFS=${E2B_PURE_ROOTFS:-}` 这类写法的地方会从"空 = identity"
   变成"空 = synth"；本单扫过 `deploy/**`，没有这种写法。
5. **pure 形态现在依赖真根 ⇒ 依赖 seccomp 档**。这是这次翻默认的**最大代价**（简报里那句"缺 seccomp
   档的宿主上起不来"就是这个）。实测两档见 §4 第 3 行。缺档时的失败是**建箱期按名字拒绝**（不是
   静默降级），这条路径由 `tests/unit/test_real_root_gate.py` 的既有用例钉住。
6. **本地池的非特权 userns 依赖**：池容器是 `seccomp=unconfined`（`local.py:141`），但真根还要宿主
   允许非特权 userns；池默认带 base image，所以只有"不带 base image 的池"会碰到，退回杆见 §6。
   `autoscaler/**` 不在本单可改范围，所以只写进了文档而没有加键。
7. **基线环境的两处偏差（不是回归，已归因）**：本机度量用的 `HEAD` worktree（`4396915`）里
   ① `tests/unit/test_migrate_state_base_script.py` 的 3 条会红（跑 operator path 渲染出的 Job 清单在
   worktree 里触发 `yaml.scanner.ScannerError`，第 50 行那个 image 值），主树里**绿**；
   ② `tests/unit/test_route_b_wiring.py:243` 多一条 skip（"fork submodule not checked out"：
   `third_party/sandlock` 在 HEAD 里是 gitlink `160000`，`git worktree add` 不会 checkout 它，
   主树那棵是完整的）。两处都只与 worktree 这一环境有关，本单没有碰这些文件。权威口径 = 主树上的
   14 条（与"已知 14"逐条同名）。
8. **`tests/unit` 的已知 14 条与文档里那句"16 failed"不一致**：本轮用 `tmp/testenv`（py3.14）量到
   14（`priv_helpers` 11 + `real_root_gate` 1 + `xfs_quotactl` 2），与简报的"已知 14 条"逐条同名；
   `docs/pure-shape-decision.md` §6 那句 16 是另一个 venv 下的数字（多两条 gateway），本轮没有复现到 16。

## 8. 文件清单与提交

改动的文件（提交用 pathspec，staged 名单核对过只含这些）：

```
envd_service/config.py
envd_service/executors/factory.py
deploy/compose/docker-compose.yml
deploy/scripts/acceptance/gateA-full.sh
deploy/scripts/acceptance/gateB-full.sh
deploy/scripts/acceptance/gateB-pure-rootfs.sh
deploy/scripts/acceptance/n27-t7-lane.sh
deploy/scripts/acceptance/x86-run-py.sh
deploy/scripts/acceptance/x86-security-one.sh
deploy/scripts/arm-lane/x86-security.sh
docs/pure-shape-decision.md
docs/production-deployment-requirements.md   # §2.4.11 新增（简报说的 docs/deployment-requirements.md 不存在，同一内容的实际载体是这份）
docs/n14-retire-the-emulation.md
tests/unit/test_pure_rootfs_config.py
tests/unit/test_worker_env_key_sets.py
tests/security/conftest.py
tests/security/test_pure_root_errno_contract.py
tests/security/test_uid_isolation.py
tests/contract/test_shared_volume_relative_cwd.py
.superpowers/sdd/pure-default-synth-report.md
```

原始日志（`tmp/` 是 gitignored，会被清；数字以本文件为准）：
`tmp/puresynth/gateA.log`、`gateB-off.log`、`pure1-contract.log`、`pure0-contract.log`、
`baseline-unit-head.log` + `baseline-unit-head-rs.log` + `skips-head.txt`（动手前 HEAD 的 worktree 基线）、
`unit-after-rs.log` + `skips-after.txt`（本单主树）、`mutation-1|2|3-*.log`、
`collect-head2.txt` / `collect-after.txt`（容器内 collect-only 两份清单）。
