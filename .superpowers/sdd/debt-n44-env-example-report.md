# 债务报告：N44 的下游实例（示例 env 照文档 `cp` 会把栈钉回 MCP 503）

日期：2026-09-27 · 工作目录 `/Users/polus/project/ai/sandlock-e2b`
入口：N44（`858d5d8`）修完 8 处清单默认值后，`docs/open-issues.md` N44 行留下的
「残余（新查出、未改）」。

---

## 1. 结论

N44 的**下游实例已清**：`deploy/compose/.env.example` 的基镜像换成车队那条
MCP-capable digest（**取值解析 `deploy/k8s/worker.yaml` + `control-plane.yaml`**，
不是抄第三遍字面量）。`deploy/stack/.env.example` 判定为**已对齐、不动**
（车队的 `python-mcp:3.14`，只有 digest 是 E6.2 的占位符）。
两处**刻意保留的非 MCP 默认**（`deploy/docker/Dockerfile.test-runner`、
`deploy/scripts/smoke-prod-worker.sh`）已在 N44 行点明"用它建的沙箱没有 MCP"。

新钉子：`tests/unit/test_compose_base_image_shape.py` 里两条 —— 逐份示例 env 解析
`E2B_BASE_IMAGE=` 声明（处数钉死），再与**两份 k8s 清单解析出来的车队值**比对。
4/4 内存变异各自红一次。

---

## 2. 逐处核清

题面命令（`deploy/compose/` 下只有这一份示例 env；`deploy/scripts/*.env.example`
两条经 rg 确认**不含** `E2B_BASE_IMAGE`）：

```
$ rg -n "E2B_BASE_IMAGE" deploy/compose/.env.example deploy/stack/.env.example \
      deploy/scripts/acr.env.example deploy/scripts/bastion.env.example
deploy/compose/.env.example:50:E2B_BASE_IMAGE=python:3.11-slim@sha256:d1e9ca7c4e78d1e8ecadb5d44bfc8e956e7a65b659a9950f569f243d72b326d0
deploy/stack/.env.example:82:# __E2B_BASE_IMAGE_DIGEST__ below; a tag change requires updating the digest
deploy/stack/.env.example:85:E2B_BASE_IMAGE=registry.cn-shanghai.aliyuncs.com/byteplan/python-mcp:3.14@sha256:__E2B_BASE_IMAGE_DIGEST__
```

| 处 | 原值 | 判定 | 依据 |
|---|---|---|---|
| `deploy/compose/.env.example:50`（改后 54） | `python:3.11-slim@sha256:d1e9ca7c…` | **对齐（已改）** | 这是唯一"照文档操作会重新引入 bug"的入口：`README.md:189`、`docker-compose.prod.yml:6`、`docker-compose.autoscale.yml:8` 都教 `cp deploy/compose/.env.example deploy/compose/.env`，而那份 `.env` 的值**顶掉** `docker-compose.prod.yml`（两处）/`docker-compose.autoscale.yml`（两处）里的 `${E2B_BASE_IMAGE:-…}` 默认值 ⇒ 拷完就回到 `python:3.11-slim`，沙箱从它建 rootfs ⇒ `/mcp` 503（`can't open file '/usr/bin/mcp-gateway'`）。|
| `deploy/stack/.env.example:85` | `…/python-mcp:3.14@sha256:__E2B_BASE_IMAGE_DIGEST__` | **不动（已对齐）** | 它**本来就是**车队那条 MCP-capable 镜像，只有 digest 是 `build-and-push.sh` 后由运维按 E6.2 替换的占位符（`upgrade.sh` 会拒绝未解析占位符，这是刻意的落地流程）。没有"错的默认值"可改；加真 digest 反而让两份模板互相漂。|
| `deploy/scripts/acr.env.example` / `bastion.env.example` | 无 `E2B_BASE_IMAGE` | **不适用** | rg 无匹配。|

### 2.1 刻意保留为非 MCP 形态的两处（不是任何栈的 `.env`）

| 处 | 值 | 为什么不改 |
|---|---|---|
| `deploy/docker/Dockerfile.test-runner:95` | `ENV E2B_BASE_IMAGE=python:3.11-slim` | 这是**测试跑器**的默认（不是部署栈），作用是让"image-rootfs 形态"的用例在本地可跑。它必须能在 uid 65534、无 ACR 网络的本机直接拉取；`deploy/scripts/test-prod-shaped.sh:240-255` 明写第二相位刻意不继承 `python-mcp:3.14`（"a locally built image that the registry mirrors refuse (403 not in the allowlist)"）。需要 MCP 的 lane 都显式传镜像：`tmp/k0s/gateA-full.sh:31 -e E2B_BASE_IMAGE=python-mcp:3.14`，gate B 显式传空。**用它建的沙箱没有 MCP。**|
| `deploy/scripts/smoke-prod-worker.sh:27` | `${E2B_BASE_IMAGE:-python:3.14-slim}` | 同类的**冒烟跑器**默认：它跑的是 `test_fork_network_features` / `test_egress_proxy`（不需要 MCP），同样面向"uid 65534 能直接拉"的本机形态。**用它建的沙箱没有 MCP。**|

这两条已在 `docs/open-issues.md` 的 N44 行里点明（见 §6），并作为 nail 的"为什么
不把车队的名字钉到全仓"的边界。

---

## 3. 改前改后

`deploy/compose/.env.example`（值由 `tmp/n44-env/apply-n44-env-example.py` 解析清单后写入，
脚本输出见下）：

```diff
 # Base image for the `base` template. Digest-pinned (E6.2): tag changes
-# require explicitly updating the digest too.
-E2B_BASE_IMAGE=python:3.11-slim@sha256:d1e9ca7c4e78d1e8ecadb5d44bfc8e956e7a65b659a9950f569f243d72b326d0
+# require explicitly updating the digest too. Kept equal to the fleet's own
+# pin (deploy/k8s/worker.yaml / control-plane.yaml): a sandbox's `/mcp` route
+# execs `/usr/bin/mcp-gateway`, which is baked into *this* image
+# (deploy/docker/Dockerfile.mcp-base) -- a plain python base here would 503 the
+# moment an operator runs the documented `cp .env.example .env` (N44).
+E2B_BASE_IMAGE=registry.cn-shanghai.aliyuncs.com/byteplan/python-mcp:3.14@sha256:3675662d0f545e255c707ca67ee1b6fae556b6db9306f6c5fbfb83b51920c8f6
```

取值脚本（`tmp/n44-env/apply-n44-env-example.py`）的原始输出
（`tmp/n44-env/apply.log`）：

```
fleet base image (parsed from deploy/k8s/worker.yaml): registry.cn-shanghai.aliyuncs.com/byteplan/python-mcp:3.14@sha256:3675662d0f545e255c707ca67ee1b6fae556b6db9306f6c5fbfb83b51920c8f6
patched deploy/compose/.env.example (1 site)
--- deploy/compose/.env.example (before)
+++ deploy/compose/.env.example (after)
@@ -47,7 +47,7 @@
 
 # Base image for the `base` template. Digest-pinned (E6.2): tag changes
 # require explicitly updating the digest too.
-E2B_BASE_IMAGE=python:3.11-slim@sha256:d1e9ca7c4e78d1e8ecadb5d44bfc8e956e7a65b659a9950f569f243d72b326d0
+E2B_BASE_IMAGE=registry.cn-shanghai.aliyuncs.com/byteplan/python-mcp:3.14@sha256:3675662d0f545e255c707ca67ee1b6fae556b6db9306f6c5fbfb83b51920c8f6
```

---

## 4. 钉子（RED → GREEN，先红后绿）

新增两条（`tests/unit/test_compose_base_image_shape.py`，复用 N44 的
`_fleet_base_image()`——它读 `deploy/k8s/worker.yaml` +
`deploy/k8s/control-plane.yaml`）：

* `test_the_compose_example_env_defaults_to_the_fleets_mcp_capable_base_image`
  —— `deploy/compose/.env.example` 的 `E2B_BASE_IMAGE=` **精确等于**车队值；
* `test_the_stack_example_env_keeps_the_fleets_mcp_capable_image`
  —— `deploy/stack/.env.example` **精确等于**"车队 repo:tag + `@sha256:__E2B_BASE_IMAGE_DIGEST__`"；
  辅助 `_example_env_base_image()` 把每份文件里 `E2B_BASE_IMAGE=` 的**处数钉成 1**
  （注释行不算声明）。

### 4.1 RED（改 `.env.example` 之前，原始输出 `tmp/n44-env/red-pin.log`）

```
..F.                                                                     [100%]
=================================== FAILURES ===================================
__ test_the_compose_example_env_defaults_to_the_fleets_mcp_capable_base_image __

    def test_the_compose_example_env_defaults_to_the_fleets_mcp_capable_base_image() -> None:
        """The documented ``cp`` must not re-pin the stacks to a base without MCP.
    ...
        """
>       assert (
            _example_env_base_image(REPO / "deploy/compose/.env.example")
            == _fleet_base_image()
        )
E       AssertionError: assert 'python:3.11-...f243d72b326d0' == 'registry.cn-...b83b51920c8f6'
E
E         - registry.cn-shanghai.aliyuncs.com/byteplan/python-mcp:3.14@sha256:3675662d0f545e255c707ca67ee1b6fae556b6db9306f6c5fbfb83b51920c8f6
E         + python:3.11-slim@sha256:d1e9ca7c4e78d1e8ecadb5d44bfc8e956e7a65b659a9950f569f243d72b326d0

tests/unit/test_compose_base_image_shape.py:183: AssertionError
=========================== short test summary info ============================
FAILED tests/unit/test_compose_base_image_shape.py::test_the_compose_example_env_defaults_to_the_fleets_mcp_capable_base_image
1 failed, 3 passed in 0.14s
```

（另一条 stack 的钉子在改前就是绿的 —— 它要钉的是"**别把这一份也改坏**"。）

### 4.2 GREEN（改完之后，`tmp/n44-env/green-pin.log`）

```
$ tmp/testenv/bin/python -m pytest tests/unit/test_compose_base_image_shape.py \
      tests/unit/test_autoscaler_local_backend_shape.py -q -p no:cacheprovider
...............                                                          [100%]
15 passed in 0.09s
```

### 4.3 变异证据（`tmp/n44-env/probe-pin-mutations.sh` → `mutation-probe.log`）

把钉子读的那几个文件复制到 `tmp/n44-env/mutated/`（**复制**不是软链：测试模块用
`__file__` 解析仓库根，软链会指回真仓库），每次只改一个文件：

```
== baseline (unmutated copy)
4 passed in 0.06s
--- mutation 1: compose example env drifts (python:3.14-slim)
== compose-example-drift: RED
   FAILED ...::test_the_compose_example_env_defaults_to_the_fleets_mcp_capable_base_image
   1 failed, 3 passed in 0.04s
--- mutation 2: compose example env gains a second declaration
== compose-example-double: RED
   FAILED ...::test_the_compose_example_env_defaults_to_the_fleets_mcp_capable_base_image
   1 failed, 3 passed in 0.05s
--- mutation 3: the fleet bumps its digest pin alone
== fleet-worker-drift: RED
   FAILED ...::test_every_stack_defaults_to_the_fleets_mcp_capable_base_image
   FAILED ...::test_the_compose_example_env_defaults_to_the_fleets_mcp_capable_base_image
   FAILED ...::test_the_stack_example_env_keeps_the_fleets_mcp_capable_image
   3 failed, 1 passed in 0.06s
--- mutation 4: the stack example env drifts (python:3.11-slim)
== stack-example-drift: RED
   FAILED ...::test_the_stack_example_env_keeps_the_fleets_mcp_capable_image
   1 failed, 3 passed in 0.04s
--- baseline again (all restored)
4 passed in 0.02s
```

即：示例 env 单侧漂 / 多出第二处声明 / 车队单侧漂 / fleet 栈那份模板漂，四个方向
各自红一次；还原后仍绿。

---

## 5. 本机单测（判据：失败名单与那 14 条已知 Linux-only 红逐条同名）

```
$ tmp/testenv/bin/python -m pytest tests/unit -q -p no:cacheprovider
14 failed, 1522 passed, 11 skipped, 2 warnings in 118.43s (0:01:58)
PYTEST-EXIT=1
```

失败名单与 `tmp/n40/n40-green-unit-full.log`（N40 轮留的同款基线）里的 14 条**逐条同名**：

```
$ rg "^FAILED" tmp/n44-env/unit-full.log | sort > tmp/n44-env/run-14.txt
$ rg "^FAILED" tmp/n40/n40-green-unit-full.log | sort > tmp/n44-env/known-14.txt
$ diff tmp/n44-env/known-14.txt tmp/n44-env/run-14.txt
IDENTICAL FAILED SET
      14 tmp/n44-env/known-14.txt
      14 tmp/n44-env/run-14.txt
```

（11 条 `test_priv_helpers` + `test_real_root_gate` 1 + `test_xfs_quotactl_backend` 2。）
通过数 1522（基线 1524）的差值来自**并行 agent**：同一工作树里 `tests/unit/test_image_rootfs_links.py`
被其删掉（`D` 在索引里），与本次改动无关；本次只跑本机 mac 档，**没有碰 docker lane**。

---

## 6. `docs/open-issues.md` N44 行补的那句

把原来那段「**残余（新查出、未改）**：… 要么把它们也换到 python-mcp，要么在文档层写明这些形态本来就不带 MCP」
替换为（原行 `<br>` 段，末尾引用处加了本报告）：

> **下游实例已清（2026-09-27，本行补注）**：`deploy/compose/.env.example:50` 的旧值
> `E2B_BASE_IMAGE=python:3.11-slim@sha256:d1e9ca7c…` 已换成车队那条 digest —— 这是照文档
> `cp deploy/compose/.env.example deploy/compose/.env` 会把这批栈**重新**钉回 503 的入口
> （拷贝出来的 `.env` 会顶掉 `docker-compose.prod.yml` / `docker-compose.autoscale.yml` 里两处
> `${E2B_BASE_IMAGE:-…}` 的默认值），取值同样是解析 `deploy/k8s/worker.yaml` +
> `deploy/k8s/control-plane.yaml`、没有抄第三遍字面量。`deploy/stack/.env.example:85` 判定为
> **已对齐、不动**：车队的 `python-mcp:3.14`，只有 digest 是 `__E2B_BASE_IMAGE_DIGEST__` 占位符
> （运维按 E6.2 从 `build-and-push.sh` 填），没有错值可改。<br>**有意保留为非 MCP 形态的两处**
> （都不是任何栈的 `.env`，用它建的沙箱没有 MCP，别当缺陷改）：`deploy/docker/Dockerfile.test-runner:95`
> 的 `ENV E2B_BASE_IMAGE=python:3.11-slim` 与 `deploy/scripts/smoke-prod-worker.sh:27` 的
> `${E2B_BASE_IMAGE:-python:3.14-slim}` —— 它们是**测试/冒烟跑器**的默认，要能在 uid 65534、
> 无 ACR 网络的本机直接拉取（`deploy/scripts/test-prod-shaped.sh` 第二相位明写 `python-mcp:3.14`
> 是本地构建镜像、registry 镜像拒绝 403 ⇒ 那条脚本刻意不继承它）；需要 MCP 的两档 lane 都显式传
> 镜像（`tmp/k0s/gateA-full.sh -e E2B_BASE_IMAGE=python-mcp:3.14`；gate B 显式传空）。<br>**钉子（下游那半）**：
> `tests/unit/test_compose_base_image_shape.py::test_the_compose_example_env_defaults_to_the_fleets_mcp_capable_base_image`
> 与 `::test_the_stack_example_env_keeps_the_fleets_mcp_capable_image` —— 解析两份 k8s 清单 + 逐份示例 env
> 的 `E2B_BASE_IMAGE=` 声明（处数钉死），4/4 变异各自红一次

---

## 7. 文件清单（本次改动）

| 文件 | 改动 |
|---|---|
| `deploy/compose/.env.example` | `E2B_BASE_IMAGE` 换成车队 digest + 注释说明为什么必须是 MCP-capable |
| `tests/unit/test_compose_base_image_shape.py` | 新增 `_example_env_base_image()` + 两条钉子 |
| `docs/open-issues.md` | N44 行「残余」→「下游实例已清」+ 两处刻意保留的点明 + 新钉子引用 |
| `.superpowers/sdd/debt-n44-env-example-report.md` | 本报告 |

未跟踪/临时物（`tmp/n44-env/`，不入库）：`apply-n44-env-example.py`、`apply.log`、
`red-pin.log`、`green-pin.log`、`probe-pin-mutations.sh`、`mutation-probe.log`、
`mutation-*.log`、`mutated/`、`unit-full.log`、`known-14.txt`、`run-14.txt`。

---

## 8. 担忧

1. **`deploy/docker/Dockerfile.test-runner` 的裸跑档与 MCP 契约用例不一致（需要人拍，未改）**：
   该 Dockerfile 把 `E2B_BASE_IMAGE` 默认成 `python:3.11-slim`，而
   `tests/contract/test_mcp_gateway_keepalive.py::_base_image()` 是"env 有值就用它、
   空才 skip、未设才落到 `python-mcp:3.14`" ⇒ **裸跑**（`docker run e2b-sandlock-test` 不带 `-e`）
   会拿非 MCP 镜像去跑 MCP 契约。本轮的判读是"lane 的默认形态刻意是 OCI-rootfs 非 MCP，MCP
   由 `-e` 显式给"（N44 行已按此点明），但这条**没有实测**（并行 lane 占用，我没抢 lane），
   若要收口，正确形状可能是"契约用例 require 一个 MCP-capable 镜像、否则按形状 skip/fail"，
   属另一条 lane 的题面。
2. **车队 compose 栈（`deploy/stack/.env`）那条一致性仍无自动化**：真值在未跟踪文件里，
   钉子只能拿两份 k8s 清单当"车队值"（N44 报告第 3 条同款）。今天一致；哪天只改 `.env`
   不改 k8s 清单，钉子不红。
3. **`deploy/scripts/smoke-prod-worker.sh` 的默认 `python:3.14-slim` 我按"刻意非 MCP"处理
   而未改**：若评审想要"冒烟也走 MCP-capable"，那要连同 uid 65534 的可拉取性一起考虑
   （ACR 在本机不可达时会红），建议单开一条。
4. **实测边界**：本轮只跑 mac 本机单测 + 钉子；**没有**起 docker 栈、**没有**跑
   `docker compose down -v`、**没有**碰 k0s 集群（题面禁止）。因此"MCP 200"那类端到端证据
   沿用 N44 报 (`debt-n44-compose-base-image-report.md` §4)，本轮只证"值已对齐 + 钉子能红"。
5. **同一工作树并行**：索引里另有并行 agent 的 `D tests/unit/test_image_rootfs_links.py` +
   3 个 `M`（`envd_service/runtime/image_resolver.py`、`oci_registry.py`、
   `tests/unit/test_oci_registry.py`）。本次提交按 pathspec 只带自己的文件，
   提交前会 `git diff --cached --name-only` 核对。
