# Task 7 报告：退役 C1 的节点 broker + 清 N48

- 工作树：`/Users/polus/project/ai/sandlock-e2b/tmp/wt-c3`（分支 `feat/c3-consolidation`），BASE `b5b66e0`
- 依据：task-7-brief、计划 `## Task 7` / `## 回退` / 验收矩阵第 10（+5/11/12）、`docs/c3-privilege-relocation.md`
  §14.3/§14.4、`docs/open-issues.md` N47/N48/N49、C1 计划与 `deploy/k8s/priv-broker.yaml` 文件头（原文，
  删除前已读）、以及 task-4b/5/6 报告里"Task 7 照清单退役"的表述
- commit：**`8776c67`**（第二个 commit 记 §1 的实测与文档回填；**未 push**）
- **本机 kubeconfig 显式指定；所有 kubectl 都先过 `open-cluster-tunnel.sh --check` 的集群身份自检。**

---

## 1. N48：5 棵属主 0 的老树

### 1.1 结论先说

**实测与原文不符：Task 7 动手前逐棵 `stat` 时，`workspaces/` 下已经没有属主 0 的条目。**
所以按"删前留证、删后复验"的字面要求：**留了证，没有可删的对象，Task 7 没有删任何一棵树**。

### 1.2 做的事（哪条路、为什么）

- **走 agent pod（`e2b-c3-agent` 的 `maint` 容器，uid 0）**，不上节点：它挂的就是那份共享 NAS
  （PV `sandlock-shared-nas` = `nfs://347d748090-ihu74.cn-shanghai.nas.aliyuncs.com/sandlock`），
  而任务的判据也在同一个挂载面上；上节点反而要经跳板机两跳（`deploy/scripts/lib/run-target.exp`）且
  看到的是**同一份 NFS**，没有额外信息。`rm` 动词那条路因此没有用上。
- 取证命令（两个 agent pod 各一次，`-c maint`）：
  ```
  ls -lan /var/lib/e2b-sandboxes/workspaces
  for e in .../workspaces/* .../workspaces/.[!.]*; do stat -c "%n mode=%a owner=%u:%g type=%F mtime=%y" "$e"; done
  find /var/lib/e2b-sandboxes/workspaces -mindepth 1 -uid 0 -printf "%m %u:%g %p\n"
  find /var/lib/e2b-sandboxes/workspaces -mindepth 1 -uid 0 | wc -l
  ```
  原始输出：`tmp/t7-cluster-state.txt`（§N48 AFTER 段）与 `tmp/n48-evidence.txt`。

### 1.3 before（2026-09-29T13:16Z，pod `e2b-c3-agent-bgg2m`）

```
## 1. ls -lan /var/lib/e2b-sandboxes/workspaces
total 2
drwxrwxrwt  4     0 65534 4096 Sep 29 13:07 .
drwxrwxrwt 11     0     0 4096 Sep 28 00:07 ..
drwxrwxrwt  2 65534 65534 4096 Sep 26 10:00 _migrate
drwxr-xr-x  3 65534 65534 4096 Sep 27 07:13 _snapshots

## 2. 每个顶层条目的完整 stat
/var/lib/e2b-sandboxes/workspaces/_migrate   mode=1777 owner=65534:65534 type=directory mtime=2026-09-26 10:00:11
/var/lib/e2b-sandboxes/workspaces/_snapshots mode=755  owner=65534:65534 type=directory mtime=2026-09-27 07:13:52

## 3. owner==0 扫描（全深度）
owner=0 count: 0
```

### 1.4 after（部署 + 删 DaemonSet 之后，2026-09-29T13:55Z，两个 agent pod）

```
-- e2b-c3-agent-5dmbz
total 2
drwxrwxrwt  4     0 65534 4096 Sep 29 13:07 .
drwxrwxrwt 11     0     0 4096 Sep 28 00:07 ..
drwxrwxrwt  2 65534 65534 4096 Sep 26 10:00 _migrate
drwxr-xr-x  3 65534 65534 4096 Sep 27 07:13 _snapshots
owner=0 count: 0
-- e2b-c3-agent-h8958
（逐字相同）
owner=0 count: 0
```

⇒ **验收判据 10（`workspaces/` 下无 `owner=0` 残留）成立**。注意 `workspaces/` **目录本身**仍是
`0:65534 mode=1777`（粘滞位、world-writable）——那是卷根约定（README / C2 §4.2 都这么记），不是残留。

### 1.5 那 5 棵树去哪了（按代码给出机理，不是猜）

`uid_pool.reconcile` 确实永远够不到它们（`envd_service/uid_pool.py::_reconcile_locked` 的
`if st.st_uid not in pool or st.st_uid in referenced: continue`，`0` 不在池里）——原文这半是对的。
**但树还有第二条回收路径**：`envd_service/agent.py::_reconcile_with_control_plane` 的
`disk_candidates = set(scanned) - known - concurrent_creates`，它的入口谓词
`is_sandbox_workspace_dir` **只看名字与有没有 `sandbox.json`，不看属主**；删树走
`priv_helpers.remove_tree()`，而那条函数对"worker 自己的 DAC 够不到"的树显式回落到
broker/agent 的 `rm`（`e2b-maint rm` 带 `cap_dac_override`，删 root 属主的树没问题）。
Tasks 3–6 期间每一次 socket 形态 worker 滚动都会跑一轮这个 GC ⇒ **"0 不在池里 ⇒ 永远回收不掉"
只对 uid 池回收成立，对按名字判定的孤儿树 GC 不成立**。已在 `docs/open-issues.md` 的 N48 行就地更正，
并把"孤儿回收要不要加显式分支"记为**已经存在**（就是上面这条）。

---

## 2. 退役覆盖了什么 / 还找到什么

### 2.1 删掉的（清单 + 代码）

| 面 | 动作 |
|---|---|
| 清单 | **删 `deploy/k8s/priv-broker.yaml`**（602 行，含 `socket-dir-init`/`image-cache-init`/`workspace-root-init` 与 `broker` 容器）；`deploy/k8s/kustomization.yaml` 去掉该资源；`deploy/k8s-k0s/kustomization.yaml` 的注释改成"曾在这里" |
| apply 闸门 | `deploy/k8s-k0s/apply.sh` 删 `rollout status ds/e2b-priv-broker`，闸门只剩 **agent → worker** |
| worker 清单 | 删 `wait-for-broker` initContainer（worker pod 现在**没有 init**）、`E2B_PRIV_HELPER_SOCKET` env、`broker-socket` hostPath 与它的挂载；注释改成"无 socket 形态" |
| Python | `envd_service/priv_helpers.py`：删 `DEFAULT_BROKER_SOCKET`/`BROKER_SOCKET_ENV`/`BROKER_PROTOCOL_VERSION`/`BROKER_TIMEOUT_*`/`BROKER_READ_CHUNK`/`BROKER_MAX_*`、`_broker_socket_path`/`_broker_timeout`/`_broker_response_limit`/`_read_broker_line`/`_broker_stream`、`PrivHelpers.transport`/`broker_socket`、`_run_socket`/`_broker_request`/`hello`、`_resolve_socket_shape`/`_require_broker_agreement`、`_realpath`（只被它用）；`TRANSPORTS` = `("auto","exec","agent")`；`import json`/`import socket` 一并删。**净 -574 行** |
| **静默回归的补位** | broker 的两个属主 init **逐字搬进 `deploy/k8s/c3-agent.yaml`**：新增 init `workspace-root-init`（建平台自己的 `_builds _images _secrets _templates _snapshots _volumes`/`<workspaces>`/`<state>`/`_migrate`/`_snapshots`/`state/_runtime(.checkpoints)`，并逐条 chown+校验），并把 `storage-init` 的 `CACHE_DIRS` 补回 `/var/lib/e2b-images`、恢复 C1 审过的"顶层非递归 + `_oci` 递归 + `secrets/` 只交目录、`*.secret` 一个字不碰"、挂上 `image-cache` hostPath。**不搬 = 删掉唯一会建卷根/交缓存属主的东西**（详见 §2.3） |
| 用例 | 删 `tests/unit/test_priv_broker_protocol.py`（1123 行，整份是 Python socket 客户端）、`tests/contract/test_broker_socket_identity.py`（482 行，Python 客户端过真 socket）；`test_worker_manifest_permissions.py` 里 6 个 broker 专用用例改写/删除；`test_priv_helpers.py` 补一条"未知 transport（含 `socket`）被具名拒绝" |
| 文档指针 | `probe_broker_authorization_surface.py` 顶部标 `RUNNABLE=no`（三个 phase 都要进 broker pod）；`probe_c2_ownership_p0.py`、`state-*-migrate.yaml`、`c3-agent.yaml`、`k8s-deployment.md`、`test_migrate_state_base_script.py`、`test_state_owner_migrate.py` 里指向已删文件/已删步骤的引用改为指向现役文档 |

### 2.2 钉子（新增/更新）

- `tests/unit/test_c3_agent_manifest.py::test_the_manifest_set_ships_no_priv_broker_any_more`：
  `deploy/k8s/priv-broker.yaml` **不存在**、kustomization 不再引用它、**两份渲染（baseline + k0s overlay）
  里都没有名为 `e2b-priv-broker` 的对象**（按解析后的对象名，不是 grep）。
- `…::test_the_worker_and_face_a_stay_outside_the_forbidden_set`：§2.3 禁项
  （`SYS_ADMIN`/`SYS_PTRACE`/`NET_RAW`/`privileged`/`hostNetwork`）在 **worker 与 agent 面 A** 都不出现，
  且 **`allowPrivilegeEscalation` 不在 `securityContext` 里**（判据 5 + 11 的现行版本；原来的
  "不得设为 false" 语义因此保住了：字段整个不存在）。
- `…::test_the_retired_socket_rollback_lever_is_gone`：渲染出的 worker 容器里没有
  `E2B_PRIV_HELPER_SOCKET`，transport 是 `agent`。
- `…::test_the_k0s_apply_gate_converges_the_agent_before_the_worker`：`apply.sh` 里**没有**
  `rollout status ds/e2b-priv-broker`，且 `apply` 行 `<-` agent `<-` worker。
- `tests/unit/test_worker_manifest_permissions.py::test_the_retired_brokers_owner_inits_moved_into_the_agent`：
  渲染出的 agent pod 有两个 init（都 `runAsUser: 0`）、`workspace-root-init` 的三个 env 与挂载、
  `storage-init` 的 `CACHE_DIRS`（两个缓存）与两个挂载、以及没有 broker DaemonSet。
- 判据 12（面 A 的容器 BND 含 `SETUID`/`SETGID`）沿用 Task 4 片 B 的钉子，未改动；本轮在集群上复核了
  面 A 的 `CapBnd=00000000a80425fb` 与 `as_uid` 的 file caps。

### 2.3 为什么"搬 init"是必须的（不搬就是静默回归）

`e2b-priv-broker` 是退役前**唯一**做这两件事的东西：

1. 建平台自己的根：`<workspaces>` 与 `<state>` 是**两个 pod 的 subPath 源**（worker / control plane）——
   源不存在 ⇒ 那个 pod 永远 `ContainerCreating`；`<state>` 还是 `uid_pool.acquire` 打开
   `<state>/.uid_pool.lock` 的目录（缺它 ⇒ 第一次 `Sandbox.create()` ENOENT）；
   `state/_runtime/.checkpoints` 的 `0711` 门是 checkpoint 能写的前提；`<workspaces>/_snapshots` 要归 65534。
2. 交两个镜像缓存的属主（`/var/lib/e2b-images` 是 kubelet 用 `DirectoryOrCreate` 建的 **root:0755** hostPath；
   `_images` 是共享的）——含"`secrets/` 只交目录、`*.secret` 留给沙箱"这条 C1 终审修过的纪律。

Task 5 只把**控制面那份** `image-cache-init` 搬进了 agent（`storage-init`），broker 那两份仍在
DaemonSet 里；Task 7 删 DaemonSet 的同时把它们搬过去（逐字），这是"退役"而不是"删掉一个还能跑的东西"。

### 2.4 grep 的结果（还剩什么）

`rg -n "e2b-priv-broker|E2B_PRIV_HELPER_SOCKET|E2B_BROKER_PEER|priv-broker|wait-for-broker|broker.sock"`
（排除 `tmp/`、`third_party/`）剩下的命中分三类：

| 类别 | 命中 | 处置 |
|---|---|---|
| **注释/历史** | `priv_helpers.py` 的"已退役"说明、`worker.yaml`/`kustomization`/`apply.sh`/`state-*-migrate.yaml`/`c3-agent.yaml` 的历史注、测试里"必须没有"的断言 | 有意保留（一眼能看出它为什么没了） |
| **证据/探针** | `deploy/scripts/acceptance/probe_broker_authorization_surface.py` | 保留 + 标 `RUNNABLE=no`（N47 的现场证据） |
| **源码残留（点名）** | `deploy/priv/priv_common.c`（`priv_peer_allowed`/`priv_env_peer_id`/`priv_roots_json`）、`deploy/priv/maint.c`（`serve`/`ping`/健康 socket/peer 门）、`tests/contract/test_broker_socket_c.py`（那份 C 契约用例） | **按计划范围保留**：Task 7 的文件清单点的是清单 + Python 客户端 + 三处同源断言，`deploy/priv/*` 不在内（task-4b 报告 §3 与 task-5/6 报告的同类表述都明说"`deploy/priv/*`（没有给 e2b-maint 加动词）"）。它们**没有任何出厂清单会启动、也没有任何客户端会连**：`e2b-maint` 只在 agent 镜像里、只被面 B 以 `chown/rm/walk` 直接 exec；那份契约用例只在一次性容器车道里跑（本机契约车道里它是基线就有的收集期 ERROR）。**已记进 N47 的"源码卫生残留"**，是"删死代码"级别的独立小任务 |

---

## 3. 集群上线（真机）

**版本 `0.1.0-764-g8776c67-20260929-215208`**（`deploy/stack/.version`）。
镜像构建：`PLATFORMS=linux/arm64 ./deploy/scripts/build-and-push.sh`（**单平台分支只 `--load` 不推**）
→ 手动 `docker push` worker/agent/autoscaler/quota-agent + `docker manifest inspect` 五个镜像逐个复核
（`tmp/build/t7-manifest-*.json`）。镜像内容抽查：worker 镜像里 `TRANSPORTS == ('auto','exec','agent')`、
没有 `_run_socket`、没有 `/var/lib/e2b-priv/`；agent 镜像里 `e2b-maint` 仍在（usage 含 `serve|ping`，见 §2.4）。

**动作**：`./deploy/k8s-k0s/apply.sh`（8 个引用 pin 到该 tag；闸门：`ds/e2b-c3-agent` → `sts/e2b-worker`
→ 预热 base image）→ **`kubectl -n sandlock delete daemonset e2b-priv-broker`**。
⚠ `apply.sh` 不 prune：清单里删文件不会让集群上那个 DaemonSet 消失，必须显式删（删前的 spec 存证在
`tmp/t7-broker-ds-before.json`：`e2b-maint serve --socket /run/e2b-broker/broker.sock
--health-socket /run/e2b-broker-health.sock`、`caps drop:[ALL] + CHOWN/DAC_OVERRIDE/FOWNER`）。

**pod（实测）**：`control-plane` 2×`2/2`、`autoscaler 1/1`、`e2b-worker-0/1 1/1`、`e2b-c3-agent` 2×`2/2`、
`seccomp-installer 2/2`、`redis 1/1`，**`grep -c broker` = 0**（没有任何 broker pod）。
工作负载镜像：worker/agent/seccomp-installer/autoscaler/control-plane-gateway 全部 = 新 tag。

**形状（pod spec 与 `/proc` 的读数，全表见 `docs/deploy-clusters.md` §7.9）**：

```
worker-0            CapEff=0000000000000000  CapBnd=00000000a80425fb   initContainers=[]  transport=agent  (无 E2B_PRIV_HELPER_SOCKET / 无 broker-socket 挂载)
agent 面 A          runAsUser=65534 CapEff=0            getcap as_uid = cap_setgid,cap_setuid=ep
agent 面 B          runAsUser=0     CapEff=000000000000000b  getcap e2b-maint = cap_chown,cap_dac_override=ep
禁项（两 pod）      SYS_ADMIN/SYS_PTRACE/NET_RAW/privileged/hostNetwork/allowPrivilegeEscalation/no-new-privileges 全部 False
```

`agent pod 的 storage-init` 与 `workspace-root-init` 各跑一轮并逐条打印归属（`_snapshots owner=65534 mode=755`、
`state/_runtime(.checkpoints) owner=65534 mode=711`、两个缓存 `owned by uid 65534`、`_volumes` 两处
`already belongs to uid 65534`）—— 这就是"两个属主 init 搬家后仍在干活"的现场证据。

**残留（无害）**：节点上 `/run/e2b-broker/`（hostPath `DirectoryOrCreate` 建的）目录还在，但没有组件挂它、
也没有人读（worker 的挂载已删）。清不清都不影响任何组件，已记进 §7.9。

---

## 4. 冒烟

- **`multinode_smoke.py` = `MULTI-NODE SMOKE OK`**：4 箱 2+2（`10.244.140.45` / `10.244.192.207`）、
  命令/文件/健康/stdin 过网关、kill 后两节点预留均归零。
- **`deployment_smoke.py`：C3 段全绿，卡在 Track Z 的模板构建**：
  `OK: commands + files through gateway`、`OK: migrated e2b-worker-0 -> e2b-worker-1, files kept`
  （**这条说明 Task 6/7 期间的 F1 修复仍然成立**）、`OK: network config echo + atomic update`、
  `OK: volume mounted remotely + sibling volume isolated`、预留归零；
  随后 `Template.build` 抛 `e2b.exceptions.BuildException: buildkit build exited with code 1`。
- **根因（读日志得到，不是猜）**：buildkit 容器日志 `dial tcp 162.125.34.133:443: i/o timeout`
  （`resolving docker.io/library/python:3.11-slim`），CP pod 内实测
  `registry-1.docker.io` / `auth.docker.io` = **Network is unreachable**，而 ACR 可达。
- **不是我引入的**：改动**之前**那一轮的 `deployment_smoke` 日志在**同一步、同一个异常**失败
  （`tmp/build/deployment_smoke.log`，21:07；`tmp/build/multinode_smoke.log` 当时是 OK）。
  即模板构建那一段本来就依赖 Docker Hub，而这台部署宿主到不了 Docker Hub。

---

## 5. 测试

- **`tests/unit`**：失败集合与基线**逐条相同**（16 条，全部环境性：`test_priv_helpers` 里 11 条
  `os.chown` 需 root、缺 `redis`/`fakeredis`、macOS 平台项）——**零新增**；删掉的三份 socket 用例
  顺带消掉了基线里 28 条与顺序/`monkeypatch` 泄漏相关的红。
- **`tests/contract`**：361 passed / 65 skipped / **1 failed**（`test_template_upload::…concurrent_upload…`，
  基线同红）+ `tests/contract/test_broker_socket_c.py` 的**收集期 ERROR**（要求一次性容器车道，
  基线同错）。`--ignore=tests/unit/test_pause_quota.py` 仅因本机缺 `redis`。

---

## 6. N47 判定

**✅ 已关闭（对"worker 拿路径去 `chown` 任意树"这条链）；残余面点名挪到 N49。** 退役把这条洞的三个
组成件**同时**去掉了：①可达性（DaemonSet + `serve` 协议 + Python socket 客户端 + worker 闸门全没，
`socket` 现在是启动期具名拒绝）；②授权对象（同一批动作改由面 B 做，但请求是 `{sandbox_id, op}`、
**路径与 uid 由 CP 从记录推导** —— §14.4 硬规则二）；③节点边界（CP 的 `sandbox → node` 记录第一次让
"这个节点有没有资格动 X"可判定，即 N49 的 ①②③ 层）。**残余**：谁拿到内部 key 谁还能驱动面 B 的
三个动词 —— 那是 N49 的舰队共享 key 盲区，不是"没有授权对象"。原文的两条修法 (a)/(b) 都作废
（broker 没了 / 已按 (b) 关闭）。完整行文见 `docs/open-issues.md` 的 N47。

---

## 7. 回退面（Task 7 之后的 C3）

`docs/c3-privilege-relocation.md` §14.8 与计划 `## 回退` 都记了同一张表：

| 想退回到 | 怎么退 | 代价 |
|---|---|---|
| `E2B_SLOT_IDENTITY=spawn`（Task 2） | 改一个 env | **仍然可原地切**（agent 还得在） |
| `E2B_PRIV_HELPER_TRANSPORT=exec`（Task 4） | 改 env **+ 回到含 file-capability 二进制的 worker 镜像** | 只改 env = 启动自检具名拒绝（半安装） |
| C1 的 broker（`=socket`） | **不是开关**：整批 revert 清单 + 镜像 | 代码路径与清单都已删 |

盘上数据不受影响（树仍是 `0770 <池 uid>:<worker gid>`，任何 root 进程都能接管）。

---

## 8. 交付物

- 代码/清单：见 §2.1（含 `deploy/k8s/priv-broker.yaml` 删除、`deploy/k8s/c3-agent.yaml` 的两个 init）。
- 文档：`docs/deploy-clusters.md`（§4 + §7 头 + **新增 §7.9**）、`docs/production-deployment-requirements.md`
  §5.4(b) 与 §24 的验证命令、`README.md` 的 env 表、`docs/open-issues.md`（N47 重判 / N48 关闭）、
  `docs/k8s-deployment.md`（清单表、apply 步骤、升级段、§24.2 回退）、`docs/c3-privilege-relocation.md`
  **新增 §14.8**、计划文件 `## 回退` 的补充。
- 证据：`tmp/t7-cluster-state.txt`（本次全部集群读数）、`tmp/t7-broker-ds-before.json`、
  `tmp/n48-evidence.txt`、`tmp/build/t7-*.log`、`tmp/build/t7-manifest-*.json`。
