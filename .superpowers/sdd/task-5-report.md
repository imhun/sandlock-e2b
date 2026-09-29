# Task 5 报告：CP 收敛到无 root（裁定 **D24**）

- 工作树：`/Users/polus/project/ai/sandlock-e2b/tmp/wt-c3`（分支 `feat/c3-consolidation`）
- 提交：**`2c67fcc`**（一个功能提交）。**未 push、未 merge、未部署、未碰集群**（真机由 controller 协调）。
- 依据：task-5-brief、plan §3（§3.1 现状 / §3.2 三步）、`docs/c3-privilege-relocation.md` §13（尤其
  §13.6/§13.7/§13.8）、Task 4a/4b 报告，以及 **D24 裁定**（本任务中途取得）。

---

## 1. 一句话形状

```
CP pod（uid 65534，pod 内无 root 容器）
  · 平台自己的目录都是 65534 的 → 读写是属主操作
  · _volumes 由 agent 的 storage-init 一次性、非递归地交给 65534 → 之后 CP 的 mkdir/chmod 也是属主操作
  · 具名失败：属主不对时 VolumeRootNotOwnedError 带出那条 chown 命令（不再裸 EACCES）

agent DaemonSet e2b-c3-agent（每节点）
  · initContainer storage-init（root，与面 B 同一理由：NAS AUTH_SYS 只认 uid 0）
      ├─ 接过原 CP pod 的 image-cache-init（_images 建/验）
      └─ 交棒 _volumes 与 _volumes/_meta（非递归、幂等、有名有姓、最后校验）
```

## 2. 三步的落地（`§3.2`）+ 那条独立缺陷

| 步 | 计划原话 | 本任务怎么做 | 判据 |
|---|---|---|---|
| **1** | CP 主容器 `runAsUser: 65534` | `deploy/k8s/control-plane.yaml` 主容器加 `securityContext.runAsUser/runAsGroup: 65534`（取值理由：平台目录已 65534、`state/.uid_pool.lock` `65534:65534 0600` 且启动期就碰、镜像缓存 owner 65534） | 单测（新文件）+ 真机程序 §13.6.1 |
| **2** | 把 CP 剩下的 A 类交给 agent：`_volumes` 卷根 `mkdir`+`chmod 1777`、`_runtime/<id>` 删除 | **改走计划的备选（D24）**：`_volumes`（含 `_meta`）一次性、非递归交给 65534，CP 保留自己的 `mkdir`/`chmod`；`_runtime/<id>` 无需委派（CP 就是它的属主） | 单测 + 真机程序 §13.6.1 |
| **3** | `image-cache-init`（root initContainer）移给 agent | CP pod 的 `initContainers` 整块删除 → agent DaemonSet 新增 initContainer `storage-init`（root），同一脚本同一校验 | 单测（pod 内无 root 容器 + init 已搬走） |

**那条"独立缺陷"（`_remove_local_tree_confirming` 配对的 `_runtime/<id>` 裸 `shutil.rmtree(ignore_errors=True)`）**：
Task 4 片 A **已经修掉**（新增 `_remove_local_runtime_confirming`，两半走同一条确认路径，存活即 `False`；
`tests/unit/test_c3_a5_local_rmtree.py`）。本任务**没有重做**，只做了两点复核：

1. 它是 **`local://` 车道**（唯一调用者 `_destroy_local`），两个生产栈 `E2B_ENABLE_LOCAL_NODE=false`
   ⇒ 生产不可达（§13.7 的更正）。
2. CP 变 65534 之后**它不需要委派给 agent**：`_runtime/<id>` 是 `0700 65534`，CP 就是属主 ——
   §13.7 的探针早就量到 `A-owner: returned=True survived=False -> ok (same uid means no privilege needed)`。
   ⇒ brief 里"`_runtime` 删除改走 agent"由 D24 同一条理由取代（判据是**结果**："`_runtime` 删得掉"）。

## 3. A 类清单：每条现在归谁（k8s 生产形态）

| # | 动作（§13.1） | 现在归谁 | 说明 |
|---|---|---|---|
| A1 | 删沙箱树 `<workspaces>/<id>` | **worker（已是外包）** | CP 的 RW 面**不含** `workspaces/`（§13.8），k8s 上不是 CP 的行为 |
| A2 | 建箱把树 `chown` 给池 uid | **worker → agent（Task 4）** | 同上，`local://` 才走 CP |
| **A3** | **卷根 `mkdir` + `chmod 1777`** | **CP 自己，但不再是特权操作** | **D24**：`_volumes`（含 `_meta`）交给 65534 之后是属主操作 |
| A4 | 迁移导入的 `rmtree` + `mkdir` | **worker → agent（Task 4）** | `local://` 车道，C3 不覆盖 |
| A5 | 删 `_runtime/<id>` | **CP 自己（属主）** | Task 4 修掉静默失败；CP=65534 后它与该目录同 uid |
| — | `_volumes/_meta/<id>.json` 记录写入 | **CP 自己（属主）** | **本任务新发现（④）**：它同在 `0:0 755` 的 `_volumes` 根里，是 D24 的第二个动因 |
| — | （本轮顺带发现）`registry/manager.py::cleanup_workspace` 的 `rmtree(ignore_errors=True)` | **不在本次范围** | 与 A5 同形（静默半删）且只在 `local://`（`record.workspace_dir` 只由 `_provision_local` 写），记在 §8 顾虑 |

⇒ **k8s 生产里 CP 侧 A 类清零**：A3 是唯一一条，且现在只是"属主操作"；其余在只读半边或 `local://`。

## 4. D24 裁定（本任务的核心决定，偏离 §3.2 **字面**）

**为什么要问**：brief 说"要么给 agent 加一条具名 verb（maint.c 风格），要么复用既有 verb；都不行就停下来问"。
复核发现：

1. `maint.c` 只有 `chown`/`rm`/`walk`（`main()` 的动词分派），**没有** mkdir/chmod → 复用不可能。
2. **`_volumes` 不是 CP 在那里唯一的写**：`VolumeRegistry._write_record` 写
   `_volumes/_meta/<volume_id>.json`，`_meta` 由 `write_text_atomically` 的 `mkdir` 惰性建出（"谁第一次
   写谁当属主" = root CP）⇒ **两条路都必须先交棒**，§3.2 的"不需要磁盘迁移"不完整。
3. 交棒既然不可省，agent 路线的剩余增量是：**给 root 的 `e2b-maint` 加一条 `mkdir` 动词** + 给
   共享存储上的 op **定一条节点寻址规则**（卷记录的 `node_id` 是 `local`，而 CP 按节点寻址 agent）。
   而新动词跑的正是 **root 文件面** —— 扩的恰是 C3 要收的那张面。

**裁定（D24）= 走 §13.6① 的第一条备选**：`_volumes`（含 `_meta`）**一次性、非递归**交给 65534，
CP 保留自己的 `mkdir` + `chmod 1777`。依据是计划自己的备选：§13.6①"把 `_volumes` 迁给 CP 的 uid 后
由 CP 自己做"、§13.2"B 类只需要一个稳定的非 root uid"、§13.5"A3 与 A1/A4 一起交给 agent，**或
一次性迁属主**"。已写进记录：`docs/c3-privilege-relocation.md` §13.6（含 ④ 与偏离说明）、
plan §3.2 的"★ D24 修订"。

**交棒放在哪、谁跑**（ruling 里点名的那一问）：

- **agent DaemonSet 的 initContainer `storage-init`**（root），**每个节点一次**。理由：它是本任务
  已经在动的那个 pod（步骤 3 把 `image-cache-init` 搬进来），face B 本来就是 root + `CHOWN`，所以
  **没有新增特权面**；而且**不需要任何寻址** —— 每个节点对同一份共享挂载做一遍，第二个节点自然
  落到"already belongs"（这正是 ruling 举的第一种形状）。**没有**任何请求体里的地址（硬规则 3）。
- 替代方案（"agent 作为某个 op 的一部分顺手做"）**不成立**：需要它的那一刻是 **CP 建卷**
  （`POST /volumes`），而 CP 建卷发生在任何挂载之前 —— 那时没有任何 worker 在跑，也就没有自然的
  op 与节点上下文。这是把它做成**节点级幂等 init**而不是 op 的真正原因，不是省事。
- 三条硬性质（ruling 要求，都有钉子）：
  - **非递归**：`chown 65534:65534 "$target"`，脚本里**没有**任何 `chown -R` 命中 `_volumes`
    （`_volumes/<id>/` 与每沙箱切片属于池 uid，`chown -R` 会在每次 agent 滚动时把它们抢回来）；
  - **幂等**：`already belongs to uid 65534 (mode …)` 分支；
  - **可见**：`handed over` / `already belongs` / `chown refused …` 三种文案 + **最后校验**
    `_volumes` 根的属主（不是 65534 就 `exit 1` 并打出那条一次性命令）。
  - **缺目录时由它创建**（写脚本时的实测抓到并修掉的，见 §6.1 case A）：`_volumes` 不存在时先
    `mkdir -p` 再交棒 —— 控制面 pod 以 **subPath** 挂它（源目录不存在会卡在
    `ContainerCreating`），而原创建者是 **Task 7 要退役**的 broker `workspace-root-init`；
    否则"agent 先于控制面起来"的 fresh install 会撞上一次裸 `stat` 失败。

**判据改写**（brief 第三条）："`_volumes` 的 `mkdir` 不在 CP 代码路径里" ⇒
**"CP 拥有 `_volumes`，所以它的 `mkdir`/`chmod` 不需要特权"**。钉子三处：
清单（`storage-init` 的存在 + 非递归 + 幂等文案 + 校验门）、动词白名单（`FILE_OP_VERBS == ("chown","rm","walk")`
且 `maint.c` 里没有 mkdir/chmod 分派）、代码（`VolumeRegistry` 不引 agent/file-op/subprocess +
具名失败 `VolumeRootNotOwnedError`）。

## 5. RED / GREEN（逐钉）

行：`.venv/bin/python -m pytest tests/unit/test_c3_cp_rootless.py -q`

| 臂 | 状态 | 结果 |
|---|---|---|
| **RED（全）** | 只有新测试文件（清单与代码都在 HEAD） | **7 failed, 3 passed**（3 条本来就成立的：buildkit 保留项、动词白名单、属主正确时的本地建卷） |
| **RED-A（清单钉）** | 代码已落、清单仍在 HEAD | **5 failed, 5 passed**（5 条清单/交棒钉红：`KeyError: 'storage-init'` 等） |
| **RED-B（代码钉）** | 清单已落、代码仍在 HEAD | **2 failed, 8 passed**（`ImportError: cannot import name 'VolumeRootNotOwnedError'`） |
| **GREEN** | 全部落地 | **10 passed** |

RED 复现方式（可重跑；注意本提交就是 `HEAD`，要退到 **`HEAD~1`**）：

```bash
P=/Users/polus/project/ai/sandlock-e2b/.venv/bin/python
git checkout HEAD~1 -- deploy/k8s/control-plane.yaml deploy/k8s/c3-agent.yaml control_plane/registry/volumes.py
$P -m pytest tests/unit/test_c3_cp_rootless.py -q      # 7 failed, 3 passed
git checkout HEAD -- control_plane/registry/volumes.py
$P -m pytest tests/unit/test_c3_cp_rootless.py -q      # 5 failed, 5 passed   (armA：清单钉)
git checkout HEAD -- deploy/k8s/control-plane.yaml deploy/k8s/c3-agent.yaml
git checkout HEAD~1 -- control_plane/registry/volumes.py
$P -m pytest tests/unit/test_c3_cp_rootless.py -q      # 2 failed, 8 passed   (armB：代码钉)
git checkout HEAD -- deploy/k8s/control-plane.yaml deploy/k8s/c3-agent.yaml control_plane/registry/volumes.py
$P -m pytest tests/unit/test_c3_cp_rootless.py -q      # 10 passed
```

## 6. 跑了什么（命令 + 输出）

```bash
# 宿主（本机 venv；tests/unit 全量在宿主停在既有的 redis/fakeredis 收集错误，故按文件跑）
$ /Users/polus/project/ai/sandlock-e2b/.venv/bin/python -m pytest \
    tests/unit/test_c3_cp_rootless.py tests/unit/test_c3_agent_manifest.py \
    tests/unit/test_worker_manifest_permissions.py tests/unit/test_c3_internal_api_shape.py \
    tests/unit/test_compose_base_image_shape.py tests/unit/test_k0s_secrets_script.py \
    tests/unit/test_cp_state_base.py tests/unit/test_shared_volume_traversal.py \
    tests/unit/test_worker_env_key_sets.py tests/unit/test_docs_only_point_at_repo_artifacts.py \
    tests/unit/test_autoscaler_local_backend_shape.py tests/unit/test_autoscaler_k8s_backend.py \
    tests/contract/test_volumes.py -q
190 passed, 2 skipped          # skip = fakeredis 未安装（既有环境项）

# 宿主全量（unit+contract，--continue-on-collection-errors 绕过既有 redis 收集错误）
$ .venv/bin/python -m pytest tests/unit tests/contract -q --continue-on-collection-errors
50 failed, 2177 passed, 95 skipped, 3 errors

# 对照：把 HEAD（a4fc553）单开一个 worktree 跑同一批失败文件
$ git worktree add --detach tmp/baseline-wt HEAD && cd tmp/baseline-wt && python -m pytest <同一批文件> -q
50 failed, 113 passed
$ diff tmp/before-names.txt tmp/after-names.txt
（失败集合**逐条相同**；多出来的 3 条是"全量收集"才出现的既有 collection error：
 redis/fakeredis 缺失 ×2 + broker socket lane 需要 Linux）
⇒ **本任务零回归**

# 容器 lane（Linux，真门禁）
$ docker run --rm --security-opt seccomp=unconfined -e E2B_REQUIRE_SECCOMP_FILTER=0 \
    -v "$PWD":/workspace -w /workspace e2b-sandlock-test:latest python3 -m pytest \
    tests/unit/test_c3_cp_rootless.py tests/unit/test_c3_agent_manifest.py \
    tests/unit/test_worker_manifest_permissions.py tests/unit/test_c3_internal_api_shape.py \
    tests/unit/test_compose_base_image_shape.py tests/unit/test_k0s_secrets_script.py \
    tests/unit/test_cp_state_base.py tests/contract/test_volumes.py \
    tests/unit/test_shared_volume_traversal.py -q
149 passed, 14 skipped         # skip = 容器里没有 kubectl（那些是渲染钉，在宿主已跑绿）

# 渲染检查（brief 点名）
$ kubectl kustomize deploy/k8s > tmp/k8s-render.yaml      # exit 0（1506 行）
$ kubectl kustomize deploy/k8s-k0s > tmp/k8s-k0s-render.yaml  # exit 0（1554 行）
$ python3 - <<'PY'   # 从渲染结果里读回形态
# control-plane Deployment: initContainers=[] ; control-plane 容器 {'runAsGroup':65534,'runAsUser':65534}
#                            buildkit 容器 {'seccompProfile': {'type': 'Unconfined'}}
# DaemonSet e2b-c3-agent:    agent inits=['storage-init']
PY
```

### 6.1 `storage-init` 的脚本级实测（把它从清单里抠出来，用 `sh` 跑四格）

清单里的那段 shell 是**会被执行**的东西，所以单独在容器里（`sh`）对着假的 `SHARED_ROOT` 跑了
四格（脚本原文抠自清单，落成 `tmp/storage-init.sh`）：

| 形态 | 期望 | 实测 |
|---|---|---|
| A：`_images`/`_volumes` 都不存在（fresh install、agent 先起） | 建 `_volumes` → 交棒 65534 → exit 0 | ✔ `created … volume store`；`_volumes 65534:65534 755` |
| B：`_volumes`+`_meta` 是 `0:0`，且 `_volumes/vol_abc` 是 `10000:10000` | 交棒**只动那两个目录**、`vol_abc` 不动、exit 0 | ✔ `vol_abc` 仍是 `10000:10000 755` —— **非递归的现场证据** |
| C：紧接着再跑一遍（幂等） | 两条都报 already belongs、exit 0 | ✔ |
| D：`chown` 被拒（PATH 里放一个必失败的 `chown`） | 先 WARNING，最后 **FATAL + 一次性命令 + exit 1** | ✔ `FATAL: … is owned by uid 65533 …`、`exit=1` |

> A 格就是**先跑脚本**抓到的那条缺陷：第一版在 `_volumes` 缺失时会被最后那行 `stat` 在 `set -e`
> 下打断（`stat: cannot statx … No such file or directory`，exit 1）—— 之后才有"缺目录就创建"。

### 6.2 `deployment_smoke` / `multinode_smoke`：**只能部分跑**（说清为什么）

它们打的是 **compose 车道**（`docker-compose.prod.yml` / `docker-compose.multinode.yml`），而本任务的
清单改动全在 **k8s**（compose 的 control-plane 仍是 root、`image-cache-init` 仍是独立服务 —— §13.3
判定"形态已经是对的"）。仍然试着把它拉起来跑（因为 brief 点名），结论与证据：

| 尝试 | 结果 |
|---|---|
| `docker compose ... up -d --no-build`（原样，用本机已有镜像） | worker 起不来：`SECCOMP_PROFILE_NOT_APPLIED`（这台 Docker/OrbStack 上装的不是仓库那份 profile）；`E2B_REQUIRE_SECCOMP_FILTER=0` 又没被 compose 透传给 worker |
| 用 `tmp/` 里的 override 打开 `E2B_REQUIRE_SECCOMP_FILTER=0` | 换成 `E2B_PRIV_HELPER_TRANSPORT must be 'auto','exec' or 'socket' (got 'agent')` —— **本机 worker 镜像是 Task 4 之前的**（`0.1.0-698-g55e5e79`），不认识 compose 现写的 `agent` 传输。要跑起来得**重建 worker 镜像**（带 sandlock wheel），不在本任务预算内 |
| override 把它退回经典形态（`transport=auto` + `slot_identity=spawn`） | 3 个 worker 注册并 healthy ✔，但 `commands.run` 报 `E2B_EXECUTOR=auto cannot run: this kernel has no usable Landlock (ABI -1)`；实测**同一个内核在 test-runner 镜像里 ABI=8**，是**那份老 worker 镜像的探测**读不到 |
| 再显式 `E2B_EXECUTOR=local`（README 说的"macOS 用 Local 执行器跑通协议"） | `deployment_smoke` 跑到 **`NODE DISTRIBUTION: {worker-1,worker-2,worker-3}` + `OK: commands + files through gateway`**，然后在**迁移**那一步 503：`Node worker-1 has no capacity or is unavailable`（`sandboxes.py:2206` 的 `nodes.reserve_node(...) is None`，此刻三节点都 healthy、`reservedMemoryMB=0`、`totalMemoryMB=2048`）。这条是**容量预留**，与本任务的改动无关（本任务没碰 node/quota；CP 侧只有 `VolumeRegistry.create` 的错误映射） |

**替代证据（更贴本任务）**：把 **CP 镜像从本树重建**（`e2b-sandlock-control-plane-gateway:c3task5`）后，
用真镜像跑本任务改到的那条路径：

```bash
$ curl -sS -X POST -H 'X-API-Key: local-key' -H 'Content-Type: application/json' \
    -d '{"name":"c3task5-smoke-2"}' http://127.0.0.1:3010/volumes
{"volumeID":"vol_779983e1d1588ccc", ...}                  # 201 语义
$ docker exec c3task5-control-plane-1 stat -c "%n %u:%g %a" \
    /var/lib/e2b-sandboxes/_volumes/vol_779983e1d1588ccc /var/lib/e2b-sandboxes/_volumes/_meta
/var/lib/e2b-sandboxes/_volumes/vol_779983e1d1588ccc 0:0 1777
/var/lib/e2b-sandboxes/_volumes/_meta                 0:0 755
$ docker exec c3task5-control-plane-1 id
uid=0(root) gid=0(root) groups=0(root)                    # ← compose 的 CP 仍是 root（本任务没改它）
```

⇒ **本任务改动的代码路径在真镜像里跑通了**（建卷 + 落 `_meta` 记录）；`0:0` 属主正是 compose 车道的
现状（k8s 那条由 D24 的交棒修掉）。栈已 `down -v` 拆掉，无残留容器。

`multinode_smoke` 未单独跑：它需要 `docker-compose.multinode.yml`，卡在同一处（老 worker 镜像 +
Landlock 探测），且它覆盖的是**同机多 worker** 的通道/并发判据（计划明确说那是判据 13/16 的场地），
与本任务改的 CP uid / 卷交棒无关。

## 7. 文件清单

**新增**：`tests/unit/test_c3_cp_rootless.py`

**改动**：

| 文件 | 改了什么 |
|---|---|
| `deploy/k8s/control-plane.yaml` | 删 `initContainers`（`image-cache-init`）、主容器加 `runAsUser/runAsGroup: 65534` + 取值理由与保留项注释 |
| `deploy/k8s/c3-agent.yaml` | 新增 initContainer `storage-init`（root）：接过 `_images` 的建/验 + **D24** 的 `_volumes`/`_meta` 非递归交棒（幂等、有名有姓、校验） |
| `control_plane/registry/volumes.py` | 新增 `VolumeRootNotOwnedError`；`create()` 的 `mkdir` 在 EACCES/EPERM/EROFS 时抛它（消息带那条 chown 命令） |
| `docs/c3-privilege-relocation.md` | §13.6：① 收口到 D24 + 新增 ④（`_meta`）+ D24 裁定/理由/硬性质；**新增 §13.6.1 部署窗口复验程序（未执行）** |
| `docs/deploy-clusters.md` | 新增 **§7.7**：CP 无 root 的现状（集群未上线）/ 仓库现状 / 上线顺序 / 待回填 |
| `docs/k8s-deployment.md` | 清单表两行（`control-plane.yaml`、`c3-agent.yaml`）同步新形态 |
| `docs/production-deployment-requirements.md` | §2.7.1 的"两个 uid 共享 `_images`"与清单检查项：k8s 侧 init 已搬到 agent pod，compose 不变 |
| `docs/superpowers/plans/2026-09-28-c3-privilege-consolidation.md` | §3.2 后加"★ D24 修订"（判据不变、实现改走备选、判据改写） |

**没有改**：`deploy/compose/*` 与 `deploy/stack/*`（compose 的 CP 仍 root —— 见 §8 顾虑 1）、
`deploy/priv/*`（**没有**给 `e2b-maint` 加动词）、`deploy/k8s/priv-broker.yaml`（Task 7 退役）。

## 8. 留给部署窗口 / 顾虑

**留给部署窗口（真机，本任务不执行）** —— 程序写在 `docs/c3-privilege-relocation.md` §13.6.1（六步）：

1. 认集群自检 → 2. 读回清单形态（CP 65534、无 initContainer、buildkit 保留项）→ 3. 读 `storage-init`
   日志 → 4. **§13.6 那张表复量**（含 `_volumes` 与 `_volumes/_meta` 必须 65534、而
   `_volumes/<vol_id>/` 必须仍是池 uid）→ 5. `.uid_pool.lock` 可开 → 6. 建卷 + `deployment_smoke`
   （顺带 `Template.build`）→ 回填 §7 与 §13.6。
2. **顺序**：先把 agent 滚起来（`storage-init` 完成交棒），再滚 control-plane。反了也不会坏
   （交棒幂等，CP 只在用户建卷时才需要它），但会撞上具名 `VolumeRootNotOwnedError`。

**顾虑 / 明确记账**：

1. **compose 两条栈的 CP 仍是 root**（本任务只做 k8s 的 pod 判据）。它们的 `image-cache-init`
   本来就是独立服务（§13.3"形态已经是对的"），所以不是"pod 里有 root 容器"；但"CP 收敛到无 root"
   在 compose 上还差同一套（`user: "65534:65534"` + 同一份交棒）。已写进 `deploy-clusters.md` §7.7
   与 §2.7.1 的注记。
2. **`_volumes/_meta` 在真集群上的属主本任务无法量**（不能碰集群）。§13.6 那次只 stat 了
   `_volumes` 根。§13.6.1 第 4 步把它列成**必须**为 65534 —— 若那里是别的值，交棒脚本会当场报出来。
3. **交棒是"每节点一次"**：k8s 上两个节点都会跑 `storage-init`，第二个落到"already belongs"。若将来
   引入**只跑一次**的语义（Job），注意共享挂载上"谁先谁后"没有保证 → 保持幂等是必须的。
4. **`VolumeRootNotOwnedError` 目前经通用 500 出口**（`api/volumes.py` 只捕 `ValueError`）：
   消息在 CP 日志里（具名 + 命令），但 HTTP 响应体不带它。要更友好可以映射成 503 + 原文；
   不在本任务判据内，先记在这里。
5. **`registry/manager.py::cleanup_workspace` 还有一处 `rmtree(ignore_errors=True)`**（本轮读代码
   顺带发现，§13.1 没列）：与 A5 同形（静默半删），但只作用于 `record.workspace_dir`，而它只由
   `_provision_local` 写 ⇒ 同样是 `local://` 车道。**记录，不在本次范围**（与 §11.2.1 第 6 条同类）。
6. **buildkit 的 socket 读路径**：CP 从 root 变 65534 后，读 rootless buildkitd 的 unix socket
   靠的是 pod 级 `fsGroup: 1000` 的**组位**（原来靠 `CAP_DAC_OVERRIDE`）。理论上成立
   （socket 由 uid 1000 建在那个 emptyDir 里，目录被 fsGroup chgrp 且 setgid），但**没有真机证据**，
   已写进 §13.6.1 第 5 步（`Template.build` 各跑一次）。
7. **compose 车道的 `image-cache-init` 用 `chown -R` 打 `_images`**：在 compose 形态里
   `E2B_IMAGE_CACHE_DIR` 就是共享的 `_images`，而 `<sandbox_id>/<name>.secret` 是**在跑沙箱自己的
   0600 文件** —— 每次 `up -d` 都递归 chown 有抢属主之嫌（k8s 侧 C1 的 broker init 早已为同一原因
   改成"只动目录"，我这里**逐字照搬的是 CP pod 那份旧脚本**）。本任务没改 compose，但把它记下来
   （Task 7 清扫或 compose 收敛时一起处理）。

---

## 10. 评审 round 1（2026-09-29）：1 Important + 4 minors —— 全部已修

提交：**`c31aa09`** `fix(c3): 交棒两个目标各自设门 + storage-init 收紧到面 B 的能力集（Task 5 评审）`

### Important：`_meta` 半场没有门、且"报成功"

**确认属实**，按评审给的形状修：

- `hand_over` 自己**复核**：chown 之后重新 `stat`，不是 65534 就打 `FATAL` + **`return 1`**；
  只有通过校验才打印 `-> uid … mode …` —— 成功行现在在门**之后**（`stderr` 的 FATAL 与
  `stdout` 的成功行不可能同时出现）。
- 两个调用点各自设门：`hand_over "$volume_store" || exit 1`、
  `hand_over "$volume_store/_meta" || exit 1`。
- 原来那段独立的 `store_owner` 门被并进 helper（否则同一句话会出现两遍）；最后那行
  `... is owned by uid <n>` 现在只可能在**两条门都通过之后**打印，所以它不能撒谎。

**证据（把清单里的脚本抠出来，真跑 `sh`）**：`tests/unit/test_c3_cp_rootless.py` 里新增了行为臂 ——
`stat`（macOS 的 `stat` 没有 `-c`）与 `chown` 用两个 shim 固定，`PROBE_REFUSED_UID` 控制"谁被拒"：

| 臂 | 期望 | 实测 |
|---|---|---|
| 全部交棒（控制臂） | exit 0 + **两条**成功行（`_volumes`、`_meta`） | ✔ `test_a_record_directory_that_hands_over_reports_success` |
| **只有 `_meta` 被拒** | exit 1；`stdout` **没有** `_meta` 的成功行；`stderr` 是指名 `_meta` 的 FATAL + 一次性命令 | ✔ `test_a_refused_record_directory_cannot_read_as_a_hand_over`（逐行 `==` 断言） |
| 文本臂 | 两个 `|| exit 1` 都在；helper 里"校验 → FATAL → 成功行"的次序 | ✔ `test_both_hand_overs_are_gated_in_the_script_text` |

**RED**（改坏再跑）：

- 抽掉 helper 的校验（成功行又变成无条件）→ **3 failed**（两条行为臂 + 文本臂）；
- 只抽掉 `_meta` 调用点的 `|| exit 1` → **2 failed**（两条文本臂；行为臂仍绿，因为 `set -e` 对
  helper 的非零返回照样中止 —— 这也是为什么文本臂需要独立存在）。

**文档同步**（评审点名的 `docs/c3-privilege-relocation.md` 一带）：§13.6 的"幂等且有名有姓"那条
改成"**每个目标各自一行状态、各自校验**"，并写明 `_meta` 从前是例外、现在失败会在 init 当场点名；
§13.6.1 **第 2 步的期望与 grep 都改成脚本实际打印的字面量**（四个目标：`owned by uid 65534` /
`already belongs to uid 65534` / `-> uid 65534` / `does not exist -- nothing to hand over`），
并写明 `_volumes` 与 `_meta` 是**两条独立的门**；`deploy-clusters.md` §7.7 同步。§8.2 的那句
"交棒脚本会当场报出来"现在对**两个**目标都成立。

### minors

| # | 处理 |
|---|---|
| 1 | `storage-init` 的能力集改成与面 B **逐条相同**：`drop: [ALL]` + `CHOWN`/`DAC_OVERRIDE`/`FOWNER`。⚠ **`CHOWN`+`FOWNER` 不够**（评审的"只需这两条"我在容器里验过，结论不同）：`--cap-drop ALL --cap-add CHOWN --cap-add FOWNER` 下，`mkdir -p "<已 65534:0755 的目录>/_oci"` 报 `Permission denied`（幂等重跑/半初始化缓存就是这一格），`set -e` 会把健康部署变成 Init:Error。`FOWNER` 覆盖对非属主目录的 `chmod 0755`，`DAC_OVERRIDE` 覆盖那次 `mkdir`；理由写进清单注释与 §13.6 |
| 2 | `VolumeRegistry.__init__` 的 `mkdir` 也套同一个具名失败（compose / `local://` 车道真正走的那层），新增臂 `test_a_store_the_control_plane_cannot_create_at_startup_is_named_too` |
| 3 | 拒绝文案改**形态中立**：说"这个 uid（`os.geteuid()`）+ 一次非递归交棒 + 那条命令"，不再出现 65534/k8s/agent 字样；两条调用点共用一个 `_volume_store_refusal()`，措辞不会漂 |
| 4 | plan §3.2 的 D24 修订、§13.6、§13.6.1、`deploy-clusters.md` §7.7 都按"两个目标各自可见失败"重述了一遍 |

### 复跑（本轮）

```bash
$ /Users/polus/project/ai/sandlock-e2b/.venv/bin/python -m pytest <13 个相关文件> -q
194 passed, 2 skipped                      # skip = fakeredis（既有环境项）

$ docker run --rm --security-opt seccomp=unconfined -e E2B_REQUIRE_SECCOMP_FILTER=0 \
    -v "$PWD":/workspace -w /workspace e2b-sandlock-test:latest python3 -m pytest <9 个文件> -q
153 passed, 14 skipped                     # skip = 容器里没有 kubectl（渲染钉在宿主跑绿）

$ kubectl kustomize deploy/k8s && kubectl kustomize deploy/k8s-k0s     # 两个 overlay exit 0

$ .venv/bin/python -m pytest tests/unit tests/contract -q --continue-on-collection-errors
（失败集合与 a4fc553 基线**逐条相同** ⇒ 仍零回归）

$ pytest tests/unit/test_c3_cp_rootless.py -q      # 14 passed
```

### 本轮新增的顾虑（不改判定）

1. **行为臂用两个 shim 把 `stat`/`chown` 固定住**（否则 macOS 上跑不了：BSD `stat` 没有 `-c`，
   非 root 也真 chown 不了）。它测的是**控制流**（门在不在、成功行在门内还是门外），
   脚本里 `stat`/`chown` 的真实语义仍由盘点清单那条"容器里真跑四格"覆盖（§6.1）。
2. 评审说"只需 `CHOWN`/`FOWNER`"与实测不符（见 minors 1）；我按**实测**取了面 B 的三条。
   如果评审更希望严格只留两条，正确的做法不是删 `DAC_OVERRIDE`，而是把 `_oci` 的 `mkdir`
   改成"先 chown 到 65534 再建"的顺序问题 —— **那要重新量一遍**，不建议在收尾轮改。
