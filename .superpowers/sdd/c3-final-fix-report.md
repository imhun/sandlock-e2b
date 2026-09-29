# C3 收口：整支评审的三条 must-fix + 若干残余（一次过）

工作目录 `/Users/polus/project/ai/sandlock-e2b/tmp/wt-c3`，分支 `feat/c3-consolidation`。
这一轮是分支的最后一改；未 push，未动主检出。

## 提交

| commit | 内容 |
|---|---|
| `b50be12` | `fix(c3): workspace-root-init 补能力集、worker BND 显式 drop，禁项钉子扩到每个容器` —— must-fix 1 + item 4 的清单与钉子 |
| `17aa2fb` | `docs(c3): 回退面改用镜像口径、§24.1 起回顺序改 agent→worker，收口陈旧表述与残余` —— must-fix 2/3、item 5/6/7/8 |
| `fef8151` | `docs(c3): 回填 §7.10 的 before/after cap 读数与 multinode_smoke，指向最新一发`（`docs/deploy-clusters.md`） |

## 逐条

1. **`workspace-root-init` 的能力集（must-fix 1）**：`deploy/k8s/c3-agent.yaml` 的 init 从
   `runAsUser: 0` + 无 `capabilities:` 块改成 `drop: [ALL]` + `{CHOWN, DAC_OVERRIDE, FOWNER}`
   （与面 B / `storage-init` 逐条相同；脚本实际用到这三个动词：`chown`、幂等重跑时 `mkdir -p`
   进别人拥有的目录、`chmod` 别人拥有的目录）。`tests/unit/test_c3_agent_manifest.py` 的禁项钉子
   从"worker + 面 A"扩到 **agent pod 的全部四个容器**（新增 `_pod_containers`），并对每个
   `runAsUser: 0` 的容器断言能力集恰为该三条。
2. **回退面改口（must-fix 2）**：`E2B_SLOT_IDENTITY=spawn` 不再写成"仍可原地切"——
   `docs/c3-privilege-relocation.md` §14.8 第 1 行、`docs/k8s-deployment.md` §24.2、
   `deploy/k8s/worker.yaml` 的 `E2B_SLOT_IDENTITY` 注释都改成"要 `helpers.slot_spawner`
   （被 Task 4 移出镜像的 `e2b-slot-spawn`），没有它 route B 直接不可用 ⇒ 必须清单 + 镜像同批退"，
   与 `socket` 那把同口径；§24.2 的交叉引用从 §14.6 改成 §14.8。
3. **§24.1 第 4 步（must-fix 3）**：删掉 `kubectl apply -f deploy/k8s/priv-broker.yaml`，改成
   `kubectl apply -f deploy/k8s/c3-agent.yaml` 并把顺序写清（agent → worker）。
4. **"BND 空集"改成真的**：`deploy/k8s/worker.yaml` 加 `capabilities: {drop: [ALL]}`；
   `tests/unit/test_worker_manifest_permissions.py` 与 `test_c3_agent_manifest.py` 的钉子从
   "没有 `capabilities` 块"改成"`drop: [ALL]` 且没有 `add`"。
5. **锚点措辞**：`control_plane/worker_identity_source.py` 说明两个锚点各管什么——文件操作锚点是
   容器 id（D25），槽位授予锚点是 pid namespace。
6. **残余落账**：`docs/c3-privilege-relocation.md` §11.2.1 新增第 11–13 条（worker/CP 各写一份
   形态判定、`manager.py` 的 A5 型静默半删、compose 的"无策略层 / multinode 无 Redis"）。
7. **C1 残留**：`docs/k8s-deployment.md` 的 worker 行与 env 列表、`docs/security-hardening.md` 8.3、
   `deploy/seccomp/README.md` 的"哪个 pod 用默认 profile"。
8. **陈旧横幅/理由**：`docs/c3-privilege-relocation.md` 首行不再写"未实施"；
   `deploy/k8s/control-plane.yaml` 的 `E2B_IMAGE_CACHE_OWNER_UID` 理由改成 CP 已是 65534（值不变）。

## 集群实测（`0.1.0-768-g17aa2fb-20260929-223205`）

构建链：`PLATFORMS=linux/arm64 ./deploy/scripts/build-and-push.sh`（单平台只 `--load`）→ 手动
`docker push` 四个 `e2b-sandlock-*` → 五个 tag 逐个 `docker manifest inspect` OK → `apply.sh`
（agent DaemonSet 2/2 → worker StatefulSet 2/2 → base image warmed）→ 重取读数。

`CapBnd` / `CapEff`（before → after）：

| 容器 | before | after |
|---|---|---|
| worker | `0xa80425fb` / `0` | **`0` / `0`** |
| agent 面 A | `0xa80425fb` / `0` | 同（未变） |
| agent 面 B | `0xb` / `0xb` | 同（未变） |
| `storage-init` | `CHOWN,DAC_OVERRIDE,FOWNER` | 同 |
| `workspace-root-init` | 默认 14 条（含 `CAP_NET_RAW`） | **`CHOWN,DAC_OVERRIDE,FOWNER`** |

读数方法两个坑写在 `docs/deploy-clusters.md` §7.10：agent pod 是 pod 级 `hostPID: true`，
`/proc/1` 是宿主机的 pid 1（要读容器自己得用 `/proc/self/status`）；两个 init 退出得太快，
它们的 cap 取自节点 `k0s ctr -n k8s.io c info <container-id>` 的 OCI `process.capabilities`。
两台节点的 `workspace-root-init` 日志都走完全绿。

冒烟：`multinode_smoke.py` = **`MULTI-NODE SMOKE OK`**（4 箱 2+2、命令/文件/stdin 过网关、
kill 后两个 worker 预约归 0）。

## 测试

* 直接相关的单项：`tests/unit/test_c3_agent_manifest.py` + `tests/unit/test_worker_manifest_permissions.py`
  → **78 passed**；再带上 `test_c3_cp_rootless / test_worker_env_key_sets / test_c3_internal_api_shape /
  test_c3_worker_kernel_identity / test_c3_fileops_forwarding / test_docs_only_point_at_repo_artifacts /
  test_c3_a5_local_rmtree / test_c3_agent_client / test_c3_slot_identity_lookup / test_c3_agent_service`
  → **260 passed**。
* `tests/unit` 全量：**2055 passed, 12 skipped, 14 failed**。14 个失败全部是本机（macOS）跑不了
  Linux-only 的用例（`test_priv_helpers.py` 11 条要 native sandlock / libc、`test_real_root_gate.py`
  1 条、`test_xfs_quotactl_backend.py` 2 条），已用 `git stash` 在 HEAD 上复跑同一组，**同样是这 14 条**
  ⇒ 与本轮改动无关。

## 顾虑

* §7.9 的 worker 行仍写着收口前的 `CapBnd=0xa80425fb`（那是当时的读数），已在 §7.9 末尾加了指向
  §7.10 的提示，没有改历史表本身。
* `deploy/stack/.version` 是 operator 文件（gitignored），本轮已指向新 tag；重跑任何部署都会按它渲染。
* 镜像内容没变（只有清单变），所以这次构建几乎全命中缓存；四个 `e2b-sandlock-*` 的 arm64 tag 是手动
  `docker push` 上去的（单平台分支只 `--load`，`manifest inspect` 已逐个复核）。
