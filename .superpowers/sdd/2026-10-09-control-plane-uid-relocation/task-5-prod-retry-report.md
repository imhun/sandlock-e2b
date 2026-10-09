# Task 5（生产上线）第二次执行报告 —— **Status: DONE**

人类已放行（"发布吧"），`main` = `5401017`，目标 = 自建 k0s，namespace `sandlock`。
第一次尝试在属主迁移那一步撞上集群内 Job 的加载路径缺陷（`task-5-prod-report.md`），
`5401017` 修好并过了评审。本轮从**干净现场**重跑完整顺序 **1–9**，含回退杆演练。

**结论：车队现在停在目标形状上、线上绿** —— 控制面 65533 且 `CapEff=0`、`_images` 等归 65533、
`state` 留 65534、`_snapshots` 是 `65534:65533 2775` 的两身份共写目录、`_migrate` 归 65533；
八条判据 7/7、两条冒烟全绿；回退杆演练过一次（旧形状起得来、冒烟全绿）再前滚复验。
发版记录落在 `docs/deploy-clusters.md` §7.60。

全程 `export KUBECONFIG="$PWD/tmp/k0s/kubeconfig"`；原始日志在 `tmp/cpuid-prod-2/`（逐文件列在 §7）。

---

## 1. 镜像：**复用上次推的那一批，没有重建**（判定依据）

```console
$ git diff --stat ab98313..5401017
 .superpowers/.../task-fix-job-lib-report.md        | 204 +++++++++++++++++++++
 deploy/k8s-k0s/README.md                           |  10 +-
 deploy/scripts/migrate-state-base.sh               |  18 +-
 deploy/scripts/migrate-state-owner.sh              |  22 ++-
 docs/k8s-deployment.md                             |  12 +-
 tests/unit/test_migrate_scripts_in_cluster_loadable.py | 193 ++++++++++++++
 tests/unit/test_migrate_state_owner_uid.py         |  12 +-
 7 files changed, 459 insertions(+), 12 deletions(-)
```

七条全是 `deploy/scripts/**`（运维手打的那份脚本）、`docs/**`、`tests/**`、`.superpowers/**` ——
**没有一行进镜像**（`deploy/docker/Dockerfile.*` 只 COPY `gateway_common/ control_plane/
envd_service/ autoscaler/ ...`，不含 `deploy/scripts/`）。`5401017` 修的是集群内 Job 的加载路径，
它随 **ConfigMap** 下发（Job 用的是运维本地那一份脚本），所以：

* **镜像按 `ab98313` 的代码复用**，tag 仍 `0.1.0-1183-gab98313-20261009-175937`；
  本次没有跑 `build-and-push.sh`，没有新的 manifest list digest。
* `deploy/stack/.version` = `0.1.0-1183-gab98313-20261009-175937`（就是三处工作负载实测的 tag）。
* 这一句在 §7.60 里明说了：**tag 记的是 `ab98313` 的代码，Job 的修复走 ConfigMap**。

---

## 2. 逐步命令与原文读数

### Step 1（+ Step 2 前置）清理现场 / 隧道

清理必须走 kubectl，所以先把隧道建起来（`open-cluster-tunnel.sh` 复用已有 ControlMaster 与转发）：

```console
$ deploy/scripts/open-cluster-tunnel.sh
==> ① 跳板机可复用连接（root@172.18.74.236）
   已存在，复用
==> ② 控制面隧道 16443 -> 172.18.80.94:6443
   端口已占用，复用
==> ④ 自检：连到的是不是本项目的集群（require_target_cluster）
✓ target cluster: context=Default server=v1.36.4+k0s nodes=2 (izuf697v12g31dyz4uvsjlz izuf6d1usviqv6x9qk1hpcz)
   sandlock namespace: 10 个 pod
通道就绪。
```

`10 个 pod` = 9 个基线 + 上次留下的 `state-owner-migrate-7z5tr`（`0/1 Error`）。按 `task-5-prod-report.md` §4 清掉：

```console
$ kubectl -n sandlock delete job/state-owner-migrate configmap/state-owner-migrate
job.batch "state-owner-migrate" deleted
configmap "state-owner-migrate" deleted
--- after: jobs/cm
(none)
--- after: pods
(none)
```

### Step 2 基线（只读，改动前的实形）

```console
POD pod/control-plane-65c8b5cb79-crqz9
uid=65534(nobody) gid=65534(nogroup) groups=65534(nogroup),1000
CapEff:	0000000000000000
POD pod/control-plane-65c8b5cb79-hcx7p   （同上）
```

`probe_platform_paths_shape.py --root /var/lib/e2b-sandboxes --json`（CP 容器内）：
八条**全 `65534:65534`**（`state`/`_migrate` `0o1777`，其余 `0o755`）；`_snapshots` 盘上 8 个 `<id>`
子目录（`drwxr-xr-x 65534 65534`）；`GET /sandboxes` = `[]`。与 `task-5-prod-report.md` §1.1 逐字相同。

### Step 3 停写：**先关自动扩缩器**，再把 worker 缩到 0

```console
$ kubectl -n sandlock get deploy/control-plane ...   # 之前
E2B_AS_ENABLED=true
$ kubectl -n sandlock set env deploy/control-plane E2B_AS_ENABLED=false
deployment.apps/control-plane env updated
$ kubectl -n sandlock rollout status deploy/control-plane --timeout=180s
deployment "control-plane" successfully rolled out
$ kubectl -n sandlock scale statefulset/e2b-worker --replicas=0
statefulset.apps/e2b-worker scaled
$ kubectl -n sandlock wait --for=delete pod -l app=e2b-worker --timeout=300s
pod/e2b-worker-0 condition met
pod/e2b-worker-1 condition met
--- 8 s 后复核
0
No resources found in sandlock namespace.
```

（这条就是第一次尝试缺的那一步：`E2B_AS_MIN_REPLICAS=2` 会在 ~1 s 内把 worker 拉回来。
`E2B_AS_ENABLED` 由后面的 `apply.sh` 按清单写回 `true`。）

### Step 4 属主迁移 —— **这次真的 chown 了**

4a 只读计划（经控制面 pod）：

```console
$ deploy/scripts/migrate-state-owner.sh --uid 65533
TARGET rel=state owner=65534:65534
TARGET rel=workspaces/_migrate owner=65534:65534
TARGET rel=workspaces/_snapshots owner=65534:65534
TARGET rel=_images owner=65533:65533
TARGET rel=_secrets owner=65533:65533
TARGET rel=_snapshots owner=65534:65533
TARGET rel=_templates owner=65533:65533
TARGET rel=_builds owner=65533:65533
STAT rel=state uid=65534 gid=65534 mode=01777 files=3032 dirs=1534
MISSING rel=workspaces/_migrate（不在盘上：跳过 —— 这条对第二次跑就是幂等的）
MISSING rel=workspaces/_snapshots（不在盘上：跳过 —— 这条对第二次跑就是幂等的）
STAT rel=_images ... files=23594 dirs=3360 / _secrets 0/0 / _snapshots files=4055 dirs=22
STAT rel=_templates files=125 dirs=125 / _builds files=96 dirs=192
SUMMARY mode=plan targets=8 chowned=0 missing=2
EXIT=0
```

4b `--uid 65533 --apply`（原文，`tmp/cpuid-prod-2/migrate-apply.log`）：

```console
$ deploy/scripts/migrate-state-owner.sh --uid 65533 --apply
configmap/state-owner-migrate created
job.batch/state-owner-migrate created
== state-owner-migration(C1 wave 2) ==
mode=apply root=/var/lib/e2b-sandboxes dry_run=0 uid=65533 gid=65533
direction=forward control=65533:65533
STAT/AFTER rel=state      65534:65534（**不动**）mode=01777 files=3032 dirs=1534
MISSING rel=workspaces/_migrate / workspaces/_snapshots
STAT/AFTER rel=_images    65534:65534 → 65533:65533  mode=0755 files=23597 dirs=3360
STAT/AFTER rel=_secrets   65534:65534 → 65533:65533  files=0 dirs=0
STAT/AFTER rel=_snapshots 65534:65534 → 65534:65533  mode=02755 files=4055 dirs=22
STAT/AFTER rel=_templates 65534:65534 → 65533:65533  files=131 dirs=131
STAT/AFTER rel=_builds    65534:65534 → 65533:65533  files=99 dirs=198
CHOWN ×6
SUMMARY mode=apply targets=8 chowned=6 missing=2
job.batch "state-owner-migrate" deleted
configmap "state-owner-migrate" deleted
EXIT=0
```

**没有 `--target _migrate`**：那条可选件由 Step 5 的 agent `storage-init` 交接（`--target` 一旦用，
回退要重复同一条；不用它就少一处不对称）。`_migrate` 当时是空目录。

### Step 5 整套清单 apply（agent DaemonSet 在内）

先 `DRY_RUN=1 apply.sh | kubectl diff -f -`（原文摘要）：**只 5 个对象变** ——
`apps/v1.Deployment sandlock.control-plane`、`StatefulSet sandlock.e2b-worker`（多了
`securityContext.supplementalGroups: [65533]`）、`DaemonSet sandlock.e2b-c3-agent`、
`DaemonSet sandlock.seccomp-installer`（tag），加 `NetworkPolicy sandlock-default-deny` 的
`generation` 噪声（36→37）。worker 的 tag：`0.1.0-1164…` → `0.1.0-1183-gab98313-20261009-175937`。

```console
$ deploy/k8s-k0s/apply.sh
✓ target cluster: context=Default server=v1.36.4+k0s nodes=2 (...)
版本 0.1.0-1183-gab98313-20261009-175937：7 个镜像引用已 pin
deployment.apps/control-plane configured
statefulset.apps/e2b-worker configured
daemonset.apps/e2b-c3-agent configured
daemonset.apps/seccomp-installer configured
等待 C3 agent DaemonSet 滚动完成
daemon set "e2b-c3-agent" successfully rolled out
等待 worker 滚动完成
statefulset rolling update complete 2 pods at revision e2b-worker-5998488f5b...
预热 e2b-worker-0：… peek {"cached": true, …}   （两台都 cached=true）
base image 已在每个 worker 就绪（peek cached=true）
EXIT=0
```

### Step 6 起回来 + 健康

`apply.sh` 把清单里的 `E2B_AS_ENABLED=true` 写回（容器级：`control-plane` → `true`，
sidecar `buildkit` → `false`；pod 内实测 `E2B_AS_ENABLED=true`、`E2B_AS_MIN_REPLICAS=2`），
worker 由清单的 `replicas` 起回 2。收敛后：

```console
deployment "control-plane" successfully rolled out
statefulset rolling update complete 2 pods at revision e2b-worker-5998488f5b...
control-plane-59f8f6f945-*   2/2 Running 0
e2b-c3-agent-*               2/2 Running 0
e2b-worker-0 / e2b-worker-1  1/1 Running 0
redis-* 1/1   seccomp-installer-* 1/1
```

---

## 3. Step 7 复核（全部原文）

### 3.1 身份与探针

```console
POD pod/control-plane-59f8f6f945-fmkkz
uid=65533 gid=65533 groups=65533,1000
Uid:	65533	65533	65533	65533
Gid:	65533	65533	65533	65533
CapPrm:	0000000000000000
CapEff:	0000000000000000
CapBnd:	00000000a80425fb
POD pod/control-plane-59f8f6f945-tcrhf   （逐字相同）
```

```json
[
  {"path": "_builds",    "mode": "0o755",  "uid": 65533, "gid": 65533},
  {"path": "_images",    "mode": "0o755",  "uid": 65533, "gid": 65533},
  {"path": "_secrets",   "mode": "0o755",  "uid": 65533, "gid": 65533},
  {"path": "_snapshots", "mode": "0o2775", "uid": 65534, "gid": 65533},
  {"path": "_templates", "mode": "0o755",  "uid": 65533, "gid": 65533},
  {"path": "_volumes",   "mode": "0o755",  "uid": 65533, "gid": 65533},
  {"path": "state",      "mode": "0o1777", "uid": 65534, "gid": 65534},
  {"path": "_migrate",   "mode": "0o2775", "uid": 65533, "gid": 65533}
]
```

### 3.2 老快照目录被扫成（8/8）

```console
$ ls -lan /var/lib/e2b-sandboxes/_snapshots        # CP 容器内
drwxrwsr-x 10 65534 65533 4096 Oct  2 11:03 .
drwxrwsr-x  2 65534 65533 4096 Sep 27 05:17 snap_015f907ace11c1ea
drwxrwsr-x  2 65534 65533 4096 Sep 27 05:12 snap_1ca5ab3332906e32
drwxrwsr-x  3 65534 65533 4096 Oct  1 15:32 snap_2bb1fe18f81d4d77
drwxrwsr-x  3 65534 65533 4096 Oct  2 01:52 snap_46dc467759dbbfb7
drwxrwsr-x  3 65534 65533 4096 Oct  2 01:52 snap_4bf1225dfbf54e0c
drwxrwsr-x  3 65534 65533 4096 Sep 26 01:43 snap_962c14802d6cbd50
drwxrwsr-x  3 65534 65533 4096 Oct  1 15:33 snap_a6e1470cca1c2c09
drwxrwsr-x  3 65534 65533 4096 Oct  2 01:52 snap_ce90ef9852fc6809
$ ls -land /var/lib/e2b-sandboxes/_migrate
drwxrwsr-x 2 65533 65533 4096 Oct  9 10:53 /var/lib/e2b-sandboxes/_migrate
$ ls -land .../_volumes .../_volumes/_meta
drwxr-xr-x 3 65533 65533 4096 Oct  9 10:54 /var/lib/e2b-sandboxes/_volumes
drwxr-xr-x 2 65533 65533 4096 Oct  9 10:54 /var/lib/e2b-sandboxes/_volumes/_meta
```

### 3.3 只线上能证的两条

**(a) NAS 认真补组 65533 —— 快照捕获**（`snap_9ef9f6f26ffd13c6`，CP 容器里读盘）：

```console
$ ls -lan /var/lib/e2b-sandboxes/_snapshots/ | grep snap_9ef9f6f26ffd13c6
drwxrwsr-x  2 65534 65533 4096 Oct  9 11:05 snap_9ef9f6f26ffd13c6
$ ls -lan /var/lib/e2b-sandboxes/_snapshots/snap_9ef9f6f26ffd13c6/
-rw-r--r--  1 65534 65533     9 Oct  9 11:05 .complete
-rw-------  1 65534 65533 10240 Oct  9 11:05 fs.tar        # worker(65534) 写的载荷
-rw-r--r--  1 65533 65533   457 Oct  9 11:05 snapshot.json # CP(65533) 写的记录
```

**一个 `<id>/`，两个身份都写进去了** ⇒ 组位这条机制在阿里云 NAS 上成立（"worker 写载荷"那条
失败形状是 500，本轮一次都没出现）。

**(b) worker 写 `_migrate/<id>.tar.gz`** —— 在 `pod/e2b-worker-0` 里用慢读者把导出流拉住，
中途 `ls -lan`（`tmp/cpuid-prod-2/fact-b2.log`）：

```console
== during (mid-stream) ==
drwxrwsr-x 2 65533 65533 4096 Oct  9 11:03 .
-rw------- 1 65534 65533 25173935 Oct  9 11:03 sbx_fe75dae99da60fea.tar.gz
stat: /var/lib/e2b-sandboxes/_migrate/sbx_fe75dae99da60fea.tar.gz 600 65534:65533 25173935 bytes
== after（服务端自己 unlink）==
drwxrwsr-x 2 65533 65533 …（只剩目录本身）
```

worker（65534）真的写进了 65533:65533 / 2775 的目录。

### 3.4 八条判据（fleet）

用 `tmp/cpuid-prod-2/fleet_criteria.py`（临时 runner，从本地车道的
`deploy/scripts/acceptance/cp_uid_lane_diff.py` 改的，kubectl 读盘那几条指到车队）：

```console
===== 1. create + 2. first command =====
  created sbx_e09a40a55102b2b8
  route: {"nodeID":"e2b-worker-0","address":"http://10.244.140.55:49983"}
  stdout='cpuid-prod-first-command-ok\n' exit=0                                     [PASS]
===== 3. GET /sandboxes/{id}/logs =====
  HTTP 200 lines=['> /bin/bash -l -c echo cpuid-prod-first-command-ok',
                  'cpuid-prod-first-command-ok', 'exit: 0']                          [PASS]
===== 4. pause + resume =====
  pause() -> True；state after pause -> paused；after connect -> running             [PASS]
===== 5. snapshot + restore =====
  snapshot_id=snap_9ef9f6f26ffd13c6；restored file -> 'cpuid-prod-snapshot-data'      [PASS]
===== 6. template build (buildkit -> _oci tar -> worker unpack) =====
  template_id=tpl_2f106485948f6559
  …/_images/_oci/e2b-local_tpl_2f106485948f6559.oci.tar regular file 644 65533:65533
  stdout='cpuid-prod-marker\n' exit=0                                                [PASS]
===== 7. volume mount read-write =====
  sandbox read 'written-from-the-host'；write ok；host read back 'written-from-the-sandbox'  [PASS]
===== 8. delete =====
  kill() -> True；GET /sandboxes/<id> -> HTTP 404；reservations {'e2b-worker-0': 0, 'e2b-worker-1': 0} [PASS]
CRITERIA 7/7 PASS
```

两条细节读数：

* **`_oci` tar 现在是 `644 65533:65533`**（老 tar 是历史遗留的 `666`；worker 只读它）。
  本地车道量到 buildctl 写的是 `0666` ⇒ 这条路上控制面把 tar 收成了 `0644`（R16 的注释口径）。
* worker 的解包落在**节点本地**：`/var/lib/e2b-images/e2b-local_tpl_2f106485948f6559-<sha>`
  `755 65534:65534` + `.lock 644 65534:65534` ⇒ "tar 共享、rootfs 本地"没变。

### 3.5 两条冒烟 + 收尾

```console
$ E2B_API_URL=http://172.18.78.49:3000 … python deploy/scripts/multinode_smoke.py
NODE DISTRIBUTION: {'http://10.244.140.55:49983': 2, 'http://10.244.192.203:49983': 2}
ALL sandboxes: commands + files + health through gateway OK
stdin through gateway OK
after kill reservations: [('e2b-worker-0', 0), ('e2b-worker-1', 0)]
MULTI-NODE SMOKE OK                                                       EXIT=0
$ … python deploy/scripts/deployment_smoke.py
OK: commands + files through gateway
OK: migrated e2b-worker-0 -> e2b-worker-1, files kept        ← worker 的 export 路由写 _migrate
OK: network config echo + atomic update
OK: volume mounted remotely + sibling volume isolated
OK: template built -> registry push -> worker pull -> image rootfs
OK: MCP gateway inside sandbox + streamable HTTP through proxy
after kill reservations: {'e2b-worker-0': 0, 'e2b-worker-1': 0}
DEPLOYMENT SMOKE OK                                                       EXIT=0
```

```console
--- 收尾（11:07）
GET /sandboxes -> []
GET /snapshots -> 6 条（与改动前同一批：snap_4bf1225dfbf54e0c / snap_46dc467759dbbfb7 /
                  snap_ce90ef9852fc6809 / snap_015f907ace11c1ea / snap_1ca5ab3332906e32 /
                  snap_962c14802d6cbd50）
GET /volumes   -> 0
/internal/nodes -> e2b-worker-0 reserved 0 / e2b-worker-1 reserved 0
pods: 9/9 Running，**0 重启**
deploy/stack/.version = 0.1.0-1183-gab98313-20261009-175937（= 三处工作负载实测 tag）
```

---

## 4. 回退杆（演练过一次，随后再前滚）

顺序与脚本 `--help` 一致（`tmp/cpuid-prod-2/rollback-rod.log`）：

1. `E2B_AS_ENABLED=false` + rollout → `scale statefulset/e2b-worker --replicas=0` + `wait --for=delete`
   （EXIT=0，无 worker pod）；
2. `migrate-state-owner.sh --rollback --uid 65533 --apply` → **`chowned=6 missing=2`**、
   `_snapshots` 回到 `65534:65534`（模式停在 `0o2755`），Job/ConfigMap 自清理、EXIT=0；
3. **清单 + 镜像一起退**：`git archive 5fee77f` 取上一版树到 `tmp/cpuid-prod-2/rollback-tree/`，
   `VERSION=0.1.0-1164-g5fee77f-20261009-101349 …/deploy/k8s-k0s/apply.sh` → agent 滚完、
   worker `rolling update complete 2 pods`、base image `cached=true`、EXIT=0；
4. 起回来并取读数：

```console
control-plane-7dd9958fb8-*  user=65534 grp=65534   CapEff: 0000000000000000
三处镜像 tag = registry.cn-shanghai.aliyuncs.com/byteplan/e2b-sandlock-*:0.1.0-1164-g5fee77f-20261009-101349
E2B_AS_ENABLED=true
probe：_builds/_images/_secrets/_templates/_volumes/state 全 65534:65534
       _snapshots 0o2755 65534:65534      ← 模式带着 setgid 的残留
       _migrate   0o3777 65533:65533      ← 旧 workspace-root-init 置的世界可写 + setgid 残留
_volumes / _volumes/_meta 被旧 storage-init 的非递归 hand_over 交回 65534:65534
$ … multinode_smoke.py     -> MULTI-NODE SMOKE OK                     EXIT=0
$ … deployment_smoke.py    -> DEPLOYMENT SMOKE OK（六步全绿）           EXIT=0
```

5. 再前滚：pause AS → worker 0 → `--uid 65533 --apply`（`chowned=6`）→ `apply.sh` →
   起回 2 → **§3 的全部读数重取一遍**（探针、扫成、两条集群事实、八条判据、两条冒烟）。

**回退杆可信**：旧形状起得来、数据面两条冒烟全绿、随后前滚也全绿。两条残留（诚实记下）：

* `_snapshots` 停在 `0o2755`、`_migrate` 停在 `0o3777` —— chown 不动模式位，setgid 是前一轮留下的；
  旧形状里这两个目录的**属主都是 65534**，读写照旧。
* **`_volumes` 不在迁移表里**（它的内容是池 uid 的账，不能递归 chown），回退靠旧 `storage-init` 的
  非递归 `hand_over` 交回 65534 —— 所以 `docs/k8s-deployment.md` §24.2 那句"回退不需要反向 chown"
  **在 65533 这一版之后不再成立**，本轮已把 §24.2 改成"先 `--rollback --apply`，再退清单+镜像"。

---

## 5. 发版记录落在哪里

| 文件 | 改了什么 |
|---|---|
| `docs/deploy-clusters.md` | **新增 §7.60**（版本、改了什么、时间线、迁移原文、探针、两条集群事实、八条判据、回退杆读数 + 两条残留、两个坑）；§7 顶部的"最近一次发版"改成 §7.60；§7.1 的第 3 步加了一条 2026-10-09 注（历史原话保留）；§7.1 那条可复用指令改成 `--uid 65533 --apply` |
| `docs/k8s-deployment.md` | §24.1 的第 2/3 步与第 5 步判据改成 `--uid 65533` 与新的属主口径；**§24.2 加了回退必须先 `--rollback --apply` 的一节（含演练过的命令序列）** |
| `deploy/k8s-k0s/README.md` | 同一段 runbook 的调用改 `--uid 65533`，并说明目标对按身份分流 |
| `docs/security-audit/control-plane-uid-2026-10-09.md` | §7 关闭判据标注"2026-10-09 已收口"，指向 §7.60 |

`deploy/stack/.version` 未改（`0.1.0-1183-gab98313-20261009-175937` 就是当前部署的 tag，truthful）。

---

## 6. 残留 / 观察

1. **一个既有小毛病（与本改动无关）**：导出流被客户端**中途掐断**时，worker 的
   `_migrate/<id>.tar.gz` 会被 NFS 改名成 `.nfs…` 并**被 worker 的 pid 1 一直持着 fd**
   ——本轮用一条中止的 `export` 探针撞到过（`ls -l /proc/1/fd` 里就挂着它）。
   处置：`kubectl -n sandlock delete pod e2b-worker-0`（StatefulSet 立刻重建，本轮执行过），
   重建后 `_migrate` 干净。影响有界：一个隐身文件占着那一份字节数，直到该 worker 重启。
   正常完成的导出**不会**留（§3.3(b) 那次读完就自己 unlink 了）。
2. **第一次尝试的现场**已按报告 §4 清掉（job + configmap + pod 都没了）。
3. `tmp/cpuid-prod-2/rollback-tree/`（`5fee77f` 的树）留在 `tmp/` 里，供下次复用；`tmp/` 是 gitignored。
4. 判据 runner `tmp/cpuid-prod-2/fleet_criteria.py` 是**临时件**（车队专用，kubectl 读盘那几条写死了
   `sandlock`），没有进版本库；本地车道的正式 runner 仍是
   `deploy/scripts/acceptance/cp_uid_lane_diff.py`。
5. `seccomp-installer` 与两个 worker/agent 的 pod 在演练与两次滚里各重建过一次
   （`0.1.0-1164` ↔ `0.1.0-1183` 往返），收尾时 9/9 Running、0 重启。

---

## 7. 产物（`tmp/cpuid-prod-2/`，都在 gitignored 的 `tmp/` 下）

```
00-tunnel.log            隧道 + require_target_cluster 自检
rollout.log              清理 → Step 3 → 健康 → 收尾的逐步记录
pre-probe.txt            改动前的基线（CP 身份 + 探针 + 盘上 id 列表 + GET /sandboxes）
migrate-plan.log         Step 4a 只读计划
migrate-apply.log        Step 4b **属主迁移原文**（chowned=6）
dryrun-diff.txt          清单 diff（5 个对象）
apply.log                Step 5 整套清单 apply
forward2.log / forward2-migrate.log / forward2-apply.log / forward2-health.log
                         回退杆之后再前滚的四段
verify-shape.txt         第一次前滚后的形状读数
verify2-shape.txt        最终形状读数（探针 + 扫成 + 卷 + 解包落点）
fleet-criteria.log       八条判据（第一次）
fleet-criteria-final.log 八条判据（最终形状，7/7 PASS）
fact-b.log / fact-b2.log worker 写 `_migrate/<id>.tar.gz` 的中途读数
volume-rerun.log         卷判据（runner 自身的两处修正，见下）
fleet-smokes-final.log   两条冒烟（最终形状）
rollback-rod.log         回退杆全文（R1–R5）
rollback-smoke.log       旧形状上的两条冒烟
final-state.log          收尾状态（sandboxes/snapshots/volumes/预约/pods/version）
rollback-tree/           `git archive 5fee77f` 的清单树（回退用）
fleet_criteria.py        车队判据 runner（临时件）
```

**runner 自身的两处修正**（不是车队的问题，记下来免得下次再撞）：① 卷判据要先
`E2B_VOLUME_API_URL=<入口>` 并把 `api_url/api_key` 传给 `Volume.create/destroy`，否则回
`400 Invalid authorization header`（`deployment_smoke.py` 就是这么写的）；② `volume.read_file()`
在 SDK 这条路上回的是 `str`，不要再 `.decode()`。

---

## 8. 与第一次尝试的对照

| | 第一次（`task-5-prod-report.md`） | 本次 |
|---|---|---|
| 版本 | `0.1.0-1183-gab98313-20261009-175937`（已推，未上线） | 同一批 tag，**复用**（`5401017` 不进镜像） |
| Step 2 属主迁移 | **BLOCKED**：Job 一启动 `/scripts/lib/cluster-guard.sh: No such file or directory` | `chowned=6 missing=2`、EXIT=0 |
| Step 3–7 | 全未做 | 全做完，读数在 §3 |
| 回退杆 | 未做 | 演练过一次 + 再前滚复验（§4） |
| 发版记录 | 未写 | `docs/deploy-clusters.md` §7.60（§5） |
| 车队 | 恢复成改动前的形状 | **停在目标形状上、线上绿** |
