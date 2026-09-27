# O1 + T1 在出厂集群上的复核（2026-09-27，只读 / 一个探针）

控制器（非子代理）亲手跑的一轮，因为它是**运维口径题**而不是代码题：
`docs/open-issues.md` 的 O1（目标机 prjquota）与 HANDOFF 里挂在它名下的
T1 复测（"沙箱写的文件宿主属主是谁"）。

## 1. 通道与身份（先认集群）

```
$ bash deploy/scripts/open-cluster-tunnel.sh
   izuf697v12g31dyz4uvsjlz  arm64  v1.36.4+k0s
   izuf6d1usviqv6x9qk1hpcz  arm64  v1.36.4+k0s
✓ 2 节点 / arm64 / 含 +k0s
   sandlock namespace: 8 个 pod
```

任何 kubectl 都带 `KUBECONFIG=$PWD/tmp/k0s/kubeconfig`。

## 2. O1 的第一半：prjquota 在出厂集群上结构上不可得

```
$ kubectl -n sandlock get pvc sandbox-shared
persistentvolumeclaim/sandbox-shared   Bound   sandlock-shared-nas   50Gi   RWX   9d

$ kubectl -n sandlock get pv sandlock-shared-nas -o jsonpath='{.spec.mountOptions}'
["vers=4.0","proto=tcp","hard","timeo=600","retrans=2","noresvport","rsize=1048576","wsize=1048576"]

$ kubectl -n sandlock exec e2b-worker-0 -- df -T /var/lib/e2b-sandboxes
347d748090-ihu74.cn-shanghai.nas.aliyuncs.com:/sandlock  nfs4  10995116277760  …  /var/lib/e2b-sandboxes

$ kubectl -n sandlock get sts e2b-worker -o jsonpath='{…env[*]…}' | grep -i quota
（无输出：worker 的 env 里没有 E2B_QUOTA_AGENT_URL）
```

结论：共享卷是**阿里云 NAS（nfs4）**，不是 XFS ⇒ XFS 项目配额在这套部署里
**没有落点**（NAS 的配额要么在服务端、要么换存储，`docs/k8s-deployment.md` §20）。
worker 没有 `E2B_QUOTA_AGENT_URL`，正是文档里那个**降级形态**（无 per-sandbox
磁盘硬限 + 一条 WARNING），不是配置漏项。O1 因此写成"已复核 + 触发条件
（换到支持项目配额的 XFS/CephFS 时回来）"，而不是一个悬着的待办。

同一份 env 里同时确认了 N27 上线后的三项：
`E2B_STATE_BASE=/var/lib/e2b-sandboxes/state`、
`E2B_ROUTE_B_TMP_ROOT=/var/lib/e2b-sandboxes/state/.route-b`、`E2B_REAL_ROOT=1`。

## 3. O1 的第二半（= T1）：宿主属主是谁

探针 `tmp/k0s/t1-ownership-probe.py`：建箱 → 箱内 `pwd`/`id` → 写一个文件 →
箱内 `stat` → **箱内 `chmod 600`** → 然后从 worker pod 这一侧对同一个文件
`stat`（这是"宿主属主"的口径）。

单箱：

```
SANDBOX sbx_1dcb9e533b1b9d88
PWD /home/user
ID 0:0
INSIDE owner=10000:10000 mode=644 size=3
CHMOD rc=0
AFTER owner=10000:10000 mode=600
HOST owner=10000:10000 mode=600 /var/lib/e2b-sandboxes/workspaces/sbx_1dcb9e533b1b9d88/t1-ownership.txt
```

两箱同时在位（`--count 2`）：

```
SANDBOX sbx_3deaf5287ad8a770  … INSIDE owner=10000:10000 … CHMOD rc=0 … HOST owner=10000:10000
SANDBOX sbx_afb735ea68ebdad4  … INSIDE owner=10001:10001 … CHMOD rc=0 … HOST owner=10001:10001
```

读法：

* 箱内 `ID 0:0` 是"沙箱在自己的 userns 里是 root"，与宿主属主无关 —— 别拿它当证据；
* **宿主属主 = 沙箱自己的 uid**（10000 / 10001，各自子树一致），**不是 uid 0**，
  也没有被 NAS squash 成 nobody；
* `chmod 600` 自己写的文件 **rc=0** ⇒ overlayfs 时代那条 T1 失效模式
  （宿主属主 0 ⇒ EPERM）**在 fleet 上不成立**。

## 4. 还没闭合的那一小块（写清楚，别当成已做）

T1 的两条里第二条（`test_uid_permissions::test_volume_shared_rw...`：共享卷
1777+sticky 的**跨 uid** 保护）需要"两个沙箱看得见同一个目录"这个前提，而出厂
集群是**每箱一棵 NAS 子树**（N27 的结论：互相看不见）⇒ 那条要在 **volumes 形态**
上才有定义，不是本次能量到的东西。本轮只把"宿主属主 + chmod"这半边量死了。

## 5. 文件

* `tmp/k0s/t1-ownership-probe.py`（探针，gitignored）
* `docs/open-issues.md` 的 O1 行（口径 + 证据 + 触发）
* `docs/HANDOFF.md` 的 T1 skip 行（"真实目标机复测"改成"已复测 + 结论"）
