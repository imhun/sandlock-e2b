# 债务报告：N44 其余 compose 的默认基镜像（MCP 503 的同一根因）

日期：2026-09-27 ｜ 工作目录：`/Users/polus/project/ai/sandlock-e2b`
HEAD（改动前）：`ebee2f1` ｜ 本次代码提交：**`858d5d8`** = `fix(compose): every stack builds sandboxes from the fleet's MCP-capable base image`（精确 5 个文件）
登记：`docs/open-issues.md` N44（本报告与那一行随文档提交）

> ⚠️ 同一工作树里有并行 agent（`envd_service/runtime/image_resolver.py`、`oci_registry.py`、
> `tests/unit/test_oci_registry.py` 在改、`tests/unit/test_image_rootfs_links.py` 被它暂存删除）。
> 本次两次用 pathspec 提交，第一次（`8b58aa4`）误把对方的**暂存删除**一起带走，已用 `git reset --soft HEAD^`
> 撤回并重做成 `858d5d8`（只含我的 5 个文件）；对方的暂存状态已逐字节还原（见 §6）。

---

## 0. 结论

**改成哪个基镜像**：车队那条 MCP-capable digest —— **逐处解析** `deploy/k8s/worker.yaml`
的 `E2B_BASE_IMAGE` 取值得到，没有在代码里抄第三遍字面量：

```
registry.cn-shanghai.aliyuncs.com/byteplan/python-mcp:3.14@sha256:3675662d0f545e255c707ca67ee1b6fae556b6db9306f6c5fbfb83b51920c8f6
```

**8 处默认值/取值全部换掉**（每处只改值、保留原有形态）：

| 文件 | 处数 | 形态 |
|---|---|---|
| `deploy/compose/docker-compose.prod.yml` | 2 | `${E2B_BASE_IMAGE:-<车队值>}`（可覆盖，与池同形） |
| `deploy/compose/docker-compose.multinode.yml` | 4 | `E2B_BASE_IMAGE: <车队值>` |
| `deploy/compose/docker-compose.yml` | 1 | `E2B_BASE_IMAGE: <车队值>` |
| `deploy/compose/docker-compose.test.yml` | 1 | `E2B_BASE_IMAGE: <车队值>` |

**`deploy/stack/docker-compose.prod.yml` 两处判定为「不动」**（依据见 §1.2）。

**实测**：本机 Docker 起改过的 `docker-compose.prod.yml`（control-plane + worker-1），两臂只差
`E2B_BASE_IMAGE` 一个变量 —— 旧值 30/30 次 503（`can't open file '/usr/bin/mcp-gateway'`），
新默认第 4 次 `200` + 真 JSON-RPC `initialize`（`serverInfo.name=e2b-mcp-gateway`）。
**不是** 503 了。

---

## 1. 改了什么

### 1.1 逐处改前改后

改动由 `tmp/n44/apply-n44-base-image.py` 施加：它**解析** `deploy/k8s/worker.yaml` 得到车队值，
对每个文件做**计数断言**的替换（数量不符就不写盘），并打印 diff（原文 `tmp/n44/n44-apply-diff.txt`）。

| # | 文件:改前行 | 改前 | 改后 |
|---|---|---|---|
| 1 | `deploy/compose/docker-compose.prod.yml:114` | `${E2B_BASE_IMAGE:-python:3.14-slim}` | 同形，默认值=车队 digest |
| 2 | `deploy/compose/docker-compose.prod.yml:184` | `${E2B_BASE_IMAGE:-python:3.14-slim}` | 同形，默认值=车队 digest |
| 3 | `deploy/compose/docker-compose.multinode.yml:75` | `E2B_BASE_IMAGE: python:3.14-slim` | `E2B_BASE_IMAGE: <车队 digest>` |
| 4 | `deploy/compose/docker-compose.multinode.yml:108` | 同上 | 同上 |
| 5 | `deploy/compose/docker-compose.multinode.yml:188` | 同上 | 同上 |
| 6 | `deploy/compose/docker-compose.multinode.yml:268` | 同上 | 同上 |
| 7 | `deploy/compose/docker-compose.yml:62` | `E2B_BASE_IMAGE: python:3.14-slim` | `E2B_BASE_IMAGE: <车队 digest>` |
| 8 | `deploy/compose/docker-compose.test.yml:27` | `E2B_BASE_IMAGE: python:3.14-slim` | `E2B_BASE_IMAGE: <车队 digest>` |

`git diff --stat` 恰好 `8 insertions(+), 8 deletions(-)`：没有别的字动过。

**"只改值"的口径（一处判断，说明依据）**：题面写「只改默认值/取值，保留可覆盖形式，与池那一处
形状一致」。我按**最小改动**读：`prod.yml` 那两处本来就是 `${E2B_BASE_IMAGE:-…}`，保持可覆盖、
只换默认值（形状与池一致）；`multinode/yml/test.yml` 那 6 处本来就是**写死的字面量**，只换值、
**没有**顺手把它们改成 `${E2B_BASE_IMAGE:-…}`（那会给这三个栈新引入一个此前不存在的 env 影响面，
超出"只改默认值/取值"）。若评审要的是"6 处也变成可覆盖插值"，那是 6 行的一步后续，钉子不受影响
（钉子比的是**回退值**，不是形式）。

### 1.2 `deploy/stack/docker-compose.prod.yml`：判定**不动**，依据

那两处（`145`、`221`）是**裸** `${E2B_BASE_IMAGE}` —— 仓库里**没有默认值**可改，真值在节点本地、
未跟踪的 `deploy/stack/.env`。实测那份 `.env`（`deploy/stack/.env:22`）**逐字节等于**两份 k8s 清单
里的那条 digest；仓库内模板 `deploy/stack/.env.example:85` 钉的也是
`python-mcp:3.14@sha256:__E2B_BASE_IMAGE_DIGEST__`（`upgrade.sh` 还会拒绝 tag-only，E6.2）。
所以：

1. 这里**没有"钉在不含 mcp-gateway 的基镜像上"这个缺陷**（N44 的根因）—— 不需要"修"；
2. 给它加仓库内默认值反而**有害**：① 多出第三处字面量（正是 E6.2/车队清单特意收敛掉的那种漂移源）；
   ② 在运维故意留空 `E2B_BASE_IMAGE`（pure 形态，见 `docs/pure-shape-decision.md`）的主机上，
   会悄悄把形态翻成 image-rootfs。

钉子把这一判断钉住（`test_the_fleet_stack_keeps_the_base_image_as_a_bare_env_override`），
以后谁要"顺手也修这里"，会先红、且必须先反驳上面两条理由。

---

## 2. RED / GREEN 原始输出

### 2.1 RED（先写钉子，清单还没改）— `tmp/n44/n44-red.log`

```
PYTEST-EXIT=1
F.                                                                       [100%]
=================================== FAILURES ===================================
________ test_every_stack_defaults_to_the_fleets_mcp_capable_base_image ________
...
>               assert (value if default is None else default) == fleet_base, (
                    relative,
                    value,
                )
E               AssertionError: ('deploy/compose/docker-compose.prod.yml', '${E2B_BASE_IMAGE:-python:3.14-slim}')
E               assert 'python:3.14-slim' == 'registry.cn-...b83b51920c8f6'
E                 - registry.cn-shanghai.aliyuncs.com/byteplan/python-mcp:3.14@sha256:3675662d0f545e255c707ca67ee1b6fae556b6db9306f6c5fbfb83b51920c8f6
E                 + python:3.14-slim
tests/unit/test_compose_base_image_shape.py:118: AssertionError
=========================== short test summary info ============================
FAILED tests/unit/test_compose_base_image_shape.py::test_every_stack_defaults_to_the_fleets_mcp_capable_base_image
1 failed, 1 passed in 0.10s
```

（`.F` 的那个点是 stack 那条：它本来就该绿 —— 那两处确实没有仓库内默认值。
测试在第一个文件的第一个坏值就断言失败，所以一次只显示一处；四个文件的**处数**另行核对过，
全部相符：

```
deploy/compose/docker-compose.prod.yml: expected=2 got=2   ${E2B_BASE_IMAGE:-python:3.14-slim} ×2
deploy/compose/docker-compose.multinode.yml: expected=4 got=4   python:3.14-slim ×4
deploy/compose/docker-compose.yml: expected=1 got=1   python:3.14-slim
deploy/compose/docker-compose.test.yml: expected=1 got=1   python:3.14-slim
deploy/stack/docker-compose.prod.yml: expected=2 got=2   ${E2B_BASE_IMAGE} ×2
```

）

### 2.2 GREEN（改清单后，同一份测试代码）— `tmp/n44/n44-green-pin.log`

```
PIN-EXIT=0
..                                                                       [100%]
2 passed in 0.06s
```

### 2.3 相关组（池/车队形态 + autoscaler + 清单钉子 + .env 校验 + 解锁位）— `tmp/n44/n44-green-group.log`

```
GROUP-EXIT=0
........................................................................ [ 68%]
.................................                                        [100%]
105 passed in 4.15s
```

---

## 3. 钉子钉的是什么，以及"任一侧漂就红"的证据

`tests/unit/test_compose_base_image_shape.py`（新文件，143 行）：

* `_fleet_base_image()` —— **解析**两份 k8s 清单（借 `test_autoscaler_local_backend_shape.py`
  的 `_fleet_base_images()`，与池的钉子同一个"车队值"来源；两份清单互不一致就红）；
* `_declared_base_images()` —— 逐行扫 `E2B_BASE_IMAGE:` 声明（跳过注释；`E2B_AS_WORKER_ENV`
  那种 JSON 值不在行首，抓不到），**逐文件钉处数**（`DEFAULTED_STACKS` = 2/4/1/1）；
* 两条用例：每个声明的**回退值**（裸字面量本身，或 `${…:-<默认>}` 的默认段）必须等于车队值；
  fleet 栈两处必须保持裸覆盖。

变异探针（`tmp/n44/probe-pin-mutations.py`；**磁盘没碰**，在项目内 `tmp/n44/mutated/` 的副本 +
内存里的清单文本上跑）— `tmp/n44/n44-pin-mutations.log`：

```
MUTATION-PROBE-EXIT=0
OK   baseline (worktree copies): both green
OK   fleet one-sided drift (control-plane.yaml digest): red
OK   stack one-sided drift (multinode worker-3): red
OK   prod.yml interpolation default back to python:3.14-slim: red
OK   third declaration (count pin): red
OK   fleet stack gains an in-tree default: red
OK   baseline again (nothing mutated on disk): both green
```

**没有**在测试里写第三遍字面量：断言读的是车队清单解析出来的值。

---

## 4. MCP 实测：同一个改过的栈，两臂只差 `E2B_BASE_IMAGE`

栈：`deploy/compose/docker-compose.prod.yml`（**本次改过的**），本机 Docker/OrbStack；
`-p n44`，只起 `redis / image-cache-init / control-plane / worker-1`；worker 镜像用**本工作树**
的 `e2b-local/e2b-sandlock-worker:n40`（N40 那轮构建，早于并行 agent 的未提交改动）；
`CONTROL_PLANE_PORT=3940`。为了不碰这台机器上的**裸** `sandbox-shared` 卷，加了一份 tmp-only
覆盖 `tmp/n44/stack-workspace-volume.yml`，把这三个服务的挂载改到项目内卷
`n44-sandbox-shared`（`docker compose config` 解析确认，`tmp/n44/n44-compose-config.log`）。

### 4.0 起栈与解析出来的基镜像

`tmp/n44/n44-stack-up.log`（`UP-EXIT=0`）＋ `docker compose config` 里的四处：

```
E2B_BASE_IMAGE: registry.cn-shanghai.aliyuncs.com/byteplan/python-mcp:3.14@sha256:3675662d0f545e255c707ca67ee1b6fae556b6db9306f6c5fbfb83b51920c8f6
(×4: control-plane / worker-1 / worker-2 / worker-3)
```

worker 注册 + 预热成功（`tmp/n44/n44-worker-logs.log`）：

```
POST http://control-plane:3000/internal/nodes/register -> 200
resolved base image registry.cn-shanghai.aliyuncs.com/byteplan/python-mcp:3.14@sha256:3675662d… to rootfs …/_images/…
image registry.cn-shanghai.aliyuncs.com/byteplan/python-mcp:3.14@sha256:3675662d…: rewrote 197 relative symlink(s) …
worker image warmed: registry.cn-shanghai.aliyuncs.com/byteplan/python-mcp:3.14@sha256:3675662d…
```

### 4.1 修复臂（出厂默认 = 车队那条）⇒ `/mcp` **200 + 真 JSON-RPC**

`tmp/n44/n44-mcp-probe.log`（重试探针，`tmp/task2-review/probe-mcp-inbound.py`）：

```
attempt 1: mcp /mcp -> 503 {"message":"MCP gateway for sandbox sbx_e0c172bf5d75ba44 is not listening on port 61001 yet"}
attempt 2: mcp /mcp -> 503 {"message":"MCP gateway for sandbox sbx_e0c172bf5d75ba44 is not listening on port 61001 yet"}
attempt 3: mcp /mcp -> 503 {"message":"MCP gateway for sandbox sbx_e0c172bf5d75ba44 is not listening on port 61001 yet"}
attempt 4: mcp /mcp -> 200 event: message
data: {"jsonrpc":"2.0","id":1,"result":{"capabilities":{"experimental":{},"tools":{"listChanged":false}}
```

`tmp/n44/n44-mcp-body.log`（完整 body）：

```
attempt 4: mcp /mcp -> 200
  body: event: message
data: {"jsonrpc":"2.0","id":1,"result":{"capabilities":{"experimental":{},"tools":{"listChanged":false}},"protocolVersion":"2025-06-18","serverInfo":{"name":"e2b-mcp-gateway","version":""}}}
```

控制面日志同一沙箱的轨迹：`POST /sandboxes 201` → `POST /mcp 503,503,503` → `POST /mcp 200 OK`。

**注意两臂 503 的语义不同**（别把这里前 3 次当成同一个故障）：修复臂的 503 是
`MCP gateway for sandbox … is not listening on port 61001 yet`（网关刚启动的竞态，第 4 次就 200）；
下面控制臂的 503 是 `mcp gateway failed to start … exit_code=2`（进程根本起不来，30/30 都是）。

### 4.2 对照臂（`E2B_BASE_IMAGE=python:3.14-slim`，其余完全相同）⇒ 必然 503

`tmp/n44/n44-control-arm-body.log`（完整 body）：

```
attempt 1: mcp /mcp -> 503
  body: {"message":"mcp gateway failed to start sandbox_id=sbx_156f048ad55cf3cb port=61001 exit_code=2 stderr=\"/usr/local/bin/python3: can't open file '/usr/bin/mcp-gateway': [Errno 2] No such file or directory\\n\""}
attempt 2: mcp /mcp -> 503
  body: …同一条…
attempt 3: mcp /mcp -> 503
  body: …同一条…
attempt 4: mcp /mcp -> 503
  body: …同一条…
attempt 5: mcp /mcp -> 503
  body: …同一条…
```

`tmp/n44/n44-control-arm-probe.log`（30 次重试）末三行：

```
attempt 28: mcp /mcp -> 503 {"message":"mcp gateway failed to start sandbox_id=sbx_ad14675ab725ad7e port=61001 exit_code=2 stderr=\"/usr/local/bin/p
attempt 29: mcp /mcp -> 503 …
attempt 30: mcp /mcp -> 503 …
```

⇒ 与 N44 登记的现象**逐字同一句**（`can't open file '/usr/bin/mcp-gateway'`），也顺带证明
`E2B_BASE_IMAGE=` 覆盖入口仍然有效（对照臂就是靠它把默认值顶掉的）。

### 4.3 沙箱**自己的 rootfs** 里有什么（直接证据）

`tmp/n44/n44-sandbox-rootfs.log`（`tmp/n40/probe-base-image-in-sandbox.py`）：

```
exit_code: 0
stdout: -rwxr-xr-x 1 nobody nogroup 5526 Sep 14 08:40 /usr/bin/mcp-gateway
#!/usr/local/bin/python3
mcp+ mcp uvicorn
stderr:
```

---

## 5. 回归（本机单测）

```
# 基线（改动前，HEAD=ebee2f1）tmp/n44/unit-baseline.log
14 failed, 1518 passed, 11 skipped, 2 warnings in 108.42s

# 改动后（本提交）tmp/n44/n44-green-unit-full.log
14 failed, 1520 passed, 11 skipped, 2 warnings in 116.64s
```

失败名单 `diff` **逐条同名**（`tmp/n44/n44-failed-before.txt` vs `…-after.txt`）：

```
SAME-14-KNOWN-LINUX-ONLY-FAILURES
```

（`test_priv_helpers` 11 / `test_real_root_gate` 1 / `test_xfs_quotactl_backend` 2 —— 全是 Linux-only。）

**passed 1518 → 1520 的 +2 就是我这次新增的两条用例**，没有别的增减：
基线在 `test_image_rootfs_links.py` 被并行 agent 暂存删除之后收集到 1543 条，本次 1543+2=1545。
（同一棵树第一次跑时它还没删，是 1549 条 —— 那 6 条的差就是并行 agent 的在途改动，不是本改动。）

---

## 6. 收尾：动了哪些容器 / 卷（**没有**用 `down -v`）

收尾命令与原文：`tmp/n44/n44-teardown-down.log`、`tmp/n44/n44-volume-rm.log`。

```bash
# 只 down（不带 -v），然后按名字显式删我自己的两个项目卷
docker compose -p n44 -f deploy/compose/docker-compose.prod.yml -f tmp/n44/stack-workspace-volume.yml down
docker volume rm n44_n44-sandbox-shared n44_redis-data
```

| 我创建并**已删除** | 说明 |
|---|---|
| 容器 `n44-redis-1`、`n44-image-cache-init-1`、`n44-control-plane-1`、`n44-worker-1-1` | `down` 停并删（含各自起停输出） |
| 网络 `n44_default` | `down` 删 |
| 卷 `n44_n44-sandbox-shared`、`n44_redis-data` | `down` 保留 → **按名字显式 `docker volume rm`** |

**我明确没碰**：裸 `sandbox-shared`（这次从一开始就用 tmp-only 覆盖把它绕开；实测**前后都不存在**）、
`stack_*`、`compose_*`、`f1stack_*`、`zf7*`、所有 buildkit 卷、以及 k0s 集群（本轮**全程没有执行任何 kubectl**）。

收尾后与开工前的快照对账（`tmp/n44-vols-before.txt` / `tmp/n44-vols-after.txt`）：

* 卷集合差异 = 6 个**匿名**卷（`com.docker.volume.anonymous`，创建时刻 08:11–08:15）+ 4 个容器
  （`registry-auth-e3c7adb6`、`buildkit-test-43a8d476`、`awesome_villani`、`zen_elion`）；
  这些是**跑 `pytest tests/unit` 时的 fixture**（registry/buildkit 那几条用例用 `docker run` 临时起的），
  我这轮按要求跑了两次全量、并行 agent 也在跑同样的用例 —— 与本 compose 栈无关，**没有一个 `n44-*` 残留**。
* 这些 fixture 残留里的 `registry-auth-e9062ae4` / `buildkit-test-*` 在我开工前**本来就有**（上一轮遗留），
  我没有清理它们（不是我的，且并行 agent 可能正在用）。

---

## 7. 文件清单

入库（`858d5d8`，pathspec 提交，`git show --stat` 恰好这 5 个）：

```
deploy/compose/docker-compose.multinode.yml     |  8 ++++----
deploy/compose/docker-compose.prod.yml          |  4 ++--
deploy/compose/docker-compose.test.yml          |  2 +-
deploy/compose/docker-compose.yml               |  2 +-
tests/unit/test_compose_base_image_shape.py     | 143 ++++++++++++++++++++++++++++
```

入库（文档提交）：`docs/open-issues.md`（N44 行 → **已修（`858d5d8`）**，并把 §8-2 的残余写进去）、本报告。

只在 `tmp/n44/`（`.gitignore` 忽略）：

| 文件 | 用途 |
|---|---|
| `n44-red.log`、`n44-green-pin.log`、`n44-green-group.log`、`n44-green-unit-full.log`、`unit-baseline.log`、`n44-failed-{before,after}.txt` | RED / GREEN / 回归 / 失败名单 diff |
| `n44-pin-mutations.log`、`probe-pin-mutations.py`、`mutated/` | 变异证据 |
| `apply-n44-base-image.py`、`n44-apply-diff.txt` | 改法（解析清单取值）+ 改前改后 diff |
| `n44-compose-config.log`、`n44-stack-up.log`、`n44-ps.log`、`n44-worker-logs.log`、`n44-cp-logs.log` | 栈解析 / 起栈 / 注册与预热 |
| `n44-mcp-probe.log`、`n44-mcp-body.log`、`n44-control-arm-{up,body,probe}.log`、`n44-sandbox-rootfs.log` | MCP 两臂原始输出 |
| `n44-teardown-down.log`、`n44-volume-rm.log`、`stack-workspace-volume.yml`、`n44-vols-*.txt`、`code-commit-msg.txt` | 收尾 / 卷覆盖 / 提交信息 |

---

## 8. 担忧

1. **`deploy/compose/.env.example:50` 仍是 `E2B_BASE_IMAGE=python:3.11-slim@sha256:d1e9ca7c…`
   （非 MCP-capable）—— 这是本轮新查出来的、同一根因的下一处，我没改（不在题面清单里）。**
   影响是实的：`docker-compose.prod.yml` 顶部就写着 `cp deploy/compose/.env.example deploy/compose/.env`，
   照文档走一遍，`E2B_BASE_IMAGE` 会被这份 `.env` 顶成 `python:3.11-slim`，这批栈**重新**回到 503。
   `deploy/docker/Dockerfile.test-runner:95` 的 `ENV E2B_BASE_IMAGE=python:3.11-slim` 同源
   （那是测试镜像的形态，可能是**故意**不带 MCP，需要人拍）。建议单开一条：要么把这两处也换到
   `python-mcp:3.14@sha256:3675662d…`，要么在文档层写明"这些形态本来就不带 MCP"。
2. **"只改值"是判断，不是题面直给**（§1.1）：我保留了 6 处写死字面量的形式（没有把它们变成
   `${E2B_BASE_IMAGE:-…}`）。若评审要的是"这 6 处也可覆盖"，请回一句，我加 6 行；钉子不受影响。
3. **车队 compose 栈那两处（`deploy/stack`）的一致性仍然没有自动化**：值在未跟踪的 `.env` 里，
   钉子只能拿两份 k8s 清单当"车队值"。今天 `deploy/stack/.env:22` 与它逐字节相同（我核过），
   但如果哪天有人只改 `.env` 不改 k8s 清单，钉子**不会**红（N40 报告第 4 条同款担忧，仍在）。
4. **实测边界**：MCP 证据是本机 OrbStack + 工作树自建的 worker 镜像，不是 ACR 出厂 worker 镜像、
   不是 k0s 集群；且默认基镜像是 ACR digest ⇒ 离线环境下这批栈需要显式
   `E2B_BASE_IMAGE=<本地 python-mcp:3.14>`（覆盖入口照旧）。
5. **同一工作树并行**：我这轮踩到一次"pathspec 提交被折断 ⇒ 连带提交了对方的暂存删除"，
   已用 `git reset --soft HEAD^` 撤回并重做（`8b58aa4` → `858d5d8`），对方的 index/worktree 状态
   已还原成原样（`D  tests/unit/test_image_rootfs_links.py` + 3 个 ` M`）。残留风险：任何按整索引
   `git commit`（不带 pathspec）的人都会带上它。
