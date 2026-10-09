# 控制面移出 uid 65534 的评估（2026-10-09）

**结论：可做，而且比 `c3-privilege-relocation.md` §13.2 / §13.7 当时估的便宜 —— 那两节点名的
"阻塞项"在今天已经不成立（见 §3），§4.1 把第三处（`_images`）也追到底了：舰队里 worker 对它
**只读**（那趟 prune 要回收的东西都在节点本地），所以**这是纯属主迁移**；`_images` 不需要组，
只有两个身份共写的 `_snapshots`/`_migrate` 用组 65533（§5 第 2 条）。**

这是 [`c3-agent-hostpid-2026-10-09.md`](c3-agent-hostpid-2026-10-09.md) §5 那条收敛的前置：
移出 65534 之后，被攻破的 agent 不能再 ptrace 控制面进程（今天 `runAsUser: 65534` 与 agent 面 A
同 uid，实测等效 ptrace 可读）。

## 1. 控制面今天为什么是 65534（以及当时的原话）

`deploy/k8s/control-plane.yaml` 自己写着这笔交易的价格：

> *"The price is real and named: CP and worker are no longer distinguishable at the filesystem layer.
> Buying that back is a dedicated uid plus a second migration package (§13.2 / §13.6's "三条合起来
> 指向同一个结论"), not a side change."*

§13.2 的结论同样直白：`PLATFORM_TARGETS` 那 8 条今天已经是 `65534:65534`（C1 wave 2 的
`deploy/scripts/migrate-state-owner.sh` 迁过），所以"CP 改成非 root 后照样能写"只是因为它也是
65534；要不再同 uid，就得**再迁一次**。

## 2. 重新清点：控制面今天自己碰哪些文件（§13 那两节写于 2026-09-28，CP 还是 root）

| 路径 | 谁创建 / 谁写 | 谁读 | 拆 uid 后会不会撞 |
|---|---|---|---|
| `state/**`（记录、命令日志、uid 池锁、route-b） | **全是 worker**：`envd_service/uid_pool.py`（`.uid_pool.lock`）、`process/logs.py`（`command-logs.jsonl`，`0o600`）、`runtime/registry.py`（记录目录 `0o711`） | worker 自己；控制面在 **k8s 车道上走 worker 的 HTTP 面**（`api/sandboxes.py:937` 的 remote 分支），只有 **local 车道**才直接读文件 | **不会** —— 控制面从不碰这些文件（`sandbox_record_path` 全仓只出现在 `envd_service/`）。它只是**派生路径**（`file_ops.py`） |
| `_images/_oci/*.oci.tar` | **buildkit 写**（rootless，uid **1000**） | **worker 读**（解进自己的缓存） | **不会** —— 这今天就已经是跨 uid 的（1000 → 65534），能工作说明它靠的是文件模式而不是同 uid |
| `/var/lib/e2b-images/<slug>/`（解出的 rootfs 缓存，**节点本地**） | **worker 写** | worker / 沙箱（只读+遍历位） | **不会** —— 控制面根本不碰它（见 §4.1 的订正） |
| `_builds/**`、`_templates/**`、`_secrets/**`、`_snapshots/**`、`_migrate/**`（**活的**迁移暂存在导出根上，N58；`workspaces/_migrate/**` 是 N58 之前的拼写、实测 `missing`） | **控制面写**（`api/templates.py`、`registry/{secrets,snapshots}.py`）；**`_snapshots` 与 `_migrate` 是两个身份共写**（worker 各写一半，见 §5 第 2 条） | agent（root，`materialize`/`chown-secret`）；或按 umask 022 出来的 `0755`/`0644` 被 worker 读 | 需要逐条核模式，机制见 §4.2 |
| `_volumes/**` | 卷内容 API 由控制面写；**切片的最终属主交给 agent**（`chown-volume-slice` / `chown-volume-root`） | 沙箱（池 uid） | 需要在集群上量当前属主（§6） |

**一句话**：C3 把特权动作（`chown`/`rm`/`walk`/`materialize`）搬给 agent 之后，控制面自己的
uid 只剩"**与 worker 在同一批目录里共同写入**"这一条约束。`control_plane/` 里**没有任何
`os.chown`**，`file_ops.py` 派生的 uid 也全部来自 CP 的记录（`host_uid` = 槽位 uid），
与控制面自己的身份无关。

## 3. §13.7 那两条"阻塞项"今天不成立

| §13.7 的结论（2026-09-28） | 今天的代码 |
|---|---|
| `.uid_pool.lock`（`0600`）是 CP 的阻塞项："CP 启动期就 `O_RDWR` 打开并 flock" | **`control_plane/` 里没有任何一处打开它**（只有两处注释提到 worker 的磁盘扫描）。打开/加锁的是 `envd_service/uid_pool.py:579` 的 worker ⇒ 不约束控制面 |
| 记录目录 `0700` 让非属主删不掉（A5 探针的结论） | 已经是 **`0711`**（那次 statfs/账本修复：`envd_service/agent.py:3636`、`runtime/registry.py:1116`），而且那条 `shutil.rmtree` 路径（`_remove_local_tree_confirming`）**只在 local lane 可达**（生产两个栈都 `E2B_ENABLE_LOCAL_NODE: "false"`）⇒ 与 k8s 控制面无关 |

（这两条不是"当时错了"，是**形态变了**：那两节写的是"CP 还是 root、且还要自己删树"的年代。
按今天的代码重算，控制面的 uid 约束面比当时小得多。）

## 4. 真正要处理的两件

### 4.1 `_oci/` 有两个写者（**本文件第一版的错判，这里更正**）

第一版写的是"`_images` 下控制面写 `_oci/`、worker 写解出的 rootfs 缓存 ⇒ 双写目录"。**那句把
compose 车道的形状读成了舰队的形状。** 复核配置后：

| | 舰队（k8s） | compose 车道 |
|---|---|---|
| `E2B_IMAGE_CACHE_DIR`（解出的 rootfs 缓存） | `/var/lib/e2b-images` —— **节点本地 hostPath**，只有 worker 写 | `/var/lib/e2b-sandboxes/_images`（与下面同一个目录） |
| `E2B_IMAGE_OCI_DIR`（OCI layout tar） | `/var/lib/e2b-sandboxes/_images` —— 共享卷 | 同上 |

（`deploy/k8s/worker.yaml:269/279`、`deploy/k8s/control-plane.yaml:464/471` 取值相同；
`_warm_node` / `_image_warm` 只在 `node.address == "local://"` 时才调解析器，舰队走的是 worker 的
`/agent/images/<image>/warm` ⇒ **控制面在舰队里根本不碰节点本地缓存**。）

**但耦合是真的，只是另一个动词** —— `_images/_oci/` 在舰队里就有两个写者：

* **创建**：控制面（`api/templates.py` 建目录、让 buildkit 写 tar、清陈旧 tar 与旁路链接）；
* **修剪**：**worker** —— `image_resolver._local_oci_tar` 从 producer 目录取 tar 之后会
  `_schedule_cache_prune(producer)`（`envd_service/runtime/image_resolver.py:1754`，注释写明
  "or a long-lived deployment grows the shared `_oci/` directory without limit"）。

**但这个写者是有条件的**：`_oci/*.oci.tar` **不计入** `E2B_IMAGE_CACHE_MAX_BYTES` 的淘汰，
它们只有一对**opt-in** 的旋钮 —— `E2B_IMAGE_CACHE_OCI_MAX_BYTES` / `_OCI_STALE_S`，
而**出厂清单一个都没设**（默认 `0` = "keeps every tar" / "disables the `_oci` pass"，
`envd_service/runtime/image_resolver.py:901/917`）。所以舰队里 worker 对 producer 目录的那趟
prune 实际上**只剩一次只读走查**（要回收的 staging 也都在它自己的节点本地缓存里）。

⇒ **结论（第三版，也是最终版）：舰队里 `_images` 对 worker 是只读的** —— 把它 `chown` 给新 uid
并保持 `0755`/`0644`（worker 要读+遍历）就够，**不需要组**。
唯一的前提条件：**那两个 OCI 旋钮保持不设**（谁要打开它们，谁就得同时给 worker 在 `_oci/`
上的写位 —— 那时才回到共享组）。把这句写进清单注释，比留一个隐式的"两个身份共用"假设好。
（compose 车道不受影响：它的两个目录本来就重合，属于"本地开发一个 uid"那一档。）

**另外**：这条也解释了为什么"提取的缓存可写"被当成跨租户投毒向量 —— 那段注释（
`image_resolver.py:170-176`）描述的是**缓存**，与共享卷上的 `_oci/` 是两件事。

### 4.2 逐条核"控制面写的、别人读的"文件模式

控制面写文件基本不设模式（吃 umask 022 ⇒ `0644`/`0755`，世界可读 ⇒ 跨 uid 没问题），
但**有几处显式 `0o600`**，例如 `api/sandboxes.py` 的迁移暂存 tar（`migrate_transfer_path`）——
它由**目标节点的 agent**（root）读，所以安全。要做的是把这批显式模式**列全并逐个核读者**
（清单见 §6），而不是假设"umask 会兜住"。

## 5. 建议的落地形状（未执行，这是方案）

1. **选 uid**：一个新的专用 uid（例如 `65533`），不改 worker / agent / 槽位池的任何 uid。
2. **组：`_images` 不需要，两个共写目录需要**。`_images` 归新 uid、保持 `0755`/`0644`
   （worker 只读+遍历），不需要共享组；前提是把 `_images` 对 worker **只读**这件事写进清单
   注释 —— 它今天的成立条件是两个 OCI 回收旋钮（`E2B_IMAGE_CACHE_OCI_MAX_BYTES` /
   `_OCI_STALE_S`）都没设；谁要打开它们，谁就得同时给 worker 在 `_oci/` 上的写位（那时才需要
   共享组）。**但 `<export>/_snapshots` 与 `<export>/_migrate` 是两个身份共写的**（前者 worker
   写 `<id>/fs.tar` + `.complete`、控制面写 `<id>/snapshot.json`；后者控制面写
   `control-plane/<id>.tar.gz`、worker 写/删 `<id>.tar.gz`），落地的形状是属主各留原主、**组位
   给 65533**，模式 `2775`（setgid），由 agent 的 `storage-init` 摆；worker 经
   `securityContext.supplementalGroups: [65533]` 入组。本节初稿说"不需要组"，那只对 `_images`
   成立。
3. **迁移属主**：`deploy/scripts/migrate-state-owner.sh` 加 `--target` 并把 `CHOWN_UID` 指向新 uid
   （这脚本第二次派上用场；C1 wave 2 那次是 root → 65534）。它自带 dry-run 与路径白名单，
   回退是同一张映射表反向 chown。
4. **两份清单**：`deploy/k8s/control-plane.yaml` 的 `runAsUser/runAsGroup` 与
   `deploy/k8s/worker.yaml` 的 `supplementalGroups`；compose 车道同步（`user:` / `group_add:`）。
5. **滚动顺序**：先迁移属主与组位 → 再滚控制面（**反过来 = 控制面写不了平台态 = 平台停摆**）；
   回退杆 = 反向 chown + 滚回旧 uid。
6. **差分验收（本地 compose 多节点车道先跑）**：create / 首条命令 / `GET /logs` /
   pause+resume / snapshot 与 restore / 模板构建（走 buildkit → `_oci` → worker 解包）/
   卷挂载读写 / delete，外加 `multinode_smoke.py` 与 `deployment_smoke.py` 两条 smoke。
   **这一批就是"CP 与 worker 不再同 uid"之后唯一值得看的证据**。

## 6. 实测读数（2026-10-09 取；本节第一版那三条"待测"已收口）

三条各有读数：第 1、3 条来自集群只读探针（只 `os.lstat` / `stat`，不写盘），第 2 条由本仓库
代码逐处对齐 —— 它本来就不需要集群。三条都已量，没有剩下"必须在集群上量"的。

### 6.1 八条平台路径的真实 mode/owner

`deploy/scripts/acceptance/probe_platform_paths_shape.py` 在 `sandlock/control-plane` 主容器内、
`--root /var/lib/e2b-sandboxes --json` 的原样输出：

```json
[
  {
    "path": "_builds",
    "mode": "0o755",
    "uid": 65534,
    "gid": 65534
  },
  {
    "path": "_images",
    "mode": "0o755",
    "uid": 65534,
    "gid": 65534
  },
  {
    "path": "_secrets",
    "mode": "0o755",
    "uid": 65534,
    "gid": 65534
  },
  {
    "path": "_snapshots",
    "mode": "0o755",
    "uid": 65534,
    "gid": 65534
  },
  {
    "path": "_templates",
    "mode": "0o755",
    "uid": 65534,
    "gid": 65534
  },
  {
    "path": "_volumes",
    "mode": "0o755",
    "uid": 65534,
    "gid": 65534
  },
  {
    "path": "state",
    "mode": "0o1777",
    "uid": 65534,
    "gid": 65534
  },
  {
    "path": "_migrate",
    "mode": "0o1777",
    "uid": 65534,
    "gid": 65534
  }
]
```

| 路径 | 真实 mode | uid:gid |
|---|---|---|
| `_builds` / `_images` / `_secrets` / `_snapshots` / `_templates` / `_volumes` | `0755` | `65534:65534` |
| `state` / `_migrate` | `1777` | `65534:65534` |

* 六条 `0755 65534:65534` ⇒ 迁到新 uid 之后 worker / agent 对它们仍"世界可读 + 可遍历"
  （`0644`/`0755`），§4.1 的"只读就够了"成立。注意这是**迁移前**的读数：`_images` 迁到 65533
  之后仍是 `0755` 目录 + `0644` tar（build 完由 `api/templates.py` 把 tar `chmod 0644`，
  见 §6.2），worker 仍只读；而 `_snapshots` 的落地形状是 `65534:65533` 模式 `2775` —— 它有两个
  写入者，不在"只读就够了"那一组里（§5 第 2 条）。
* `state` 与 `_migrate` **迁移前**都是 **`1777`**（sticky + world-rwx）而不是 `0755`，但迁移后
  两者不同：`state` 是 worker 的账、模式保持不动（C3 的 `workspace-root-init` 对 `$state` 的
  放行判据就是"属主 65534 **或** world-writable"；迁移只改属主，**不要顺手收模式**）；
  `_migrate` 则由 `storage-init` 的 `hand_over_shared` 交给 **`65533:65533` 并把模式无条件设成
  `2775`**（被拒就 FATAL —— 没有 1777 兜底，也让沙箱池 uid 无法在迁移中途往里塞东西），所以
  对它"不要收模式"的老建议不适用。
* 第 8 条读的是 `_migrate`：清单（`deploy/k8s/control-plane.yaml` 的同名 `subPath`）才是"pod 挂了
  什么"的权威；本节初稿的 8 条清单抄的是 §13.8（2026-09-28）的快照，写的还是上浮前的
  `workspaces/_migrate`，而 **N58（§7.29）已把 `_migrate` 上浮到导出根** —— 那处老拼写实测
  `missing`（`workspaces/` 自己 `0o1777 0:65534`），按清单改读 `_migrate` 之后就是上表里的读数。

### 6.2 显式模式的写点与它们的读者（从代码对齐）

`control_plane/` 里的显式模式写入有两处 `os.chmod`（卷根的 `0o1777`、OCI layout tar 的
`0o644`）、一处 `0o600`，另有一条经共享助手落地的 `0o666 & ~umask`（= `0644`）：

| 写点 | 显式模式 | 落地 | 读者 |
|---|---|---|---|
| `api/sandboxes.py:2937`、`:2960`（`_export_sandbox_archive`，local 与 remote 两条） | `0o600` | `_migrate/control-plane/<sandbox_id>.tar.gz`（迁移暂存 tar） | **控制面自己** —— `_import_sandbox_archive` 以 `open(tar_path, "rb")` 读出再经 HTTP 流给目标节点 agent（`POST /agent/sandboxes/<id>/import`）；目标 agent 收到的是字节流，**不读这个文件**。同 uid 读写 ⇒ 迁移后仍成立。 |
| `registry/volumes.py:306`（`os.chmod`） | `0o1777` | `_volumes/<volume_id>/`（卷根） | 沙箱（池 uid）经挂载写/读、控制面卷内容 API 写；world-rwx + sticky 是有意的（单层 userns 无附加组，sticky 防跨 uid 删） |
| `api/templates.py::_run_build`（buildctl 成功后 `os.chmod`） | `0o644` | `_images/_oci/<slug>.oci.tar`（OCI layout tar） | worker（解进自己的节点本地缓存；只读+遍历）+ 控制面自己的回收路径；沙箱够不到 `_images`。2026-10-09 实测 buildctl 原本落 `0666`（`65534` 也能覆写已有 tar 的字节），这条 chmod 把清单注释里"只读 + `0644`"从假设变成事实 |
| `gateway_common/paths.py:798`（`write_text_atomically` 的暂存兄弟文件；`control_plane/registry/{templates,volumes,secrets,snapshots}.py` 经 `write_json_atomically`/`write_text_atomically` 调用） | `0o666 & ~umask` = `0644` | `_meta/*.json`（模板/构建/卷记录）、`_volumes/_meta/<id>.deleted`、`_secrets/**`、`_snapshots/**` | 另一副本的控制面（SDK 轮询可能落在任一副本；`secrets` 的 `_scan_disk` 也在未命中时扫） + 控制面自己；`_snapshots/**` 的读者还有 agent（root，从快照建箱读 `fs.tar`）。注意：**沙箱的 secret 文件不在共享 `_secrets/` 上** —— 它由 worker 写在**节点本地** `<E2B_IMAGE_CACHE_DIR>/secrets/<id>/`（`envd_service/executors/sandlock.py`），`chown-secret` 交的就是这份 |

`open(..., "w")` 形状的（默认 umask ⇒ `0644`，世界可读）：

| 写点 | 落地 | 读者 |
|---|---|---|
| `_write_docker_config`（`api/templates.py`） | `$DOCKER_CONFIG/config.json`（pod 本地） | `buildctl` 子进程（同 pod、同 uid） |
| `_run_build`（写 build context 的 `Dockerfile`） | `_builds/<id>/ctx/Dockerfile` | buildkit 的 dockerfile frontend（`--local dockerfile=…`） |
| `template_file_upload`（`api/templates.py`）→ `gateway_common/upload.py:142`（`open(path, "wb")`） | `_builds/<id>/archives/<hash>.tar.gz` | 控制面自己（`_extract_build_context`） |
| `volume_write_file`（`api/volumes.py`）→ 同上 | `_volumes/<id>/<path>` | 沙箱（池 uid）经卷挂载 |
| `api/snapshots.py:247`、`registry/snapshots.py:427/672`（`shutil.copytree`，继承源模式） | `_snapshots/**` | agent / 控制面 |

不是控制面的文件系统写、列出来只为免含糊：`_steps_to_dockerfile`（`api/templates.py`）里的
`--chmod={mode}` 是**用户** COPY 步骤生成的 Dockerfile 指令；`api/volumes.py::_entry` 的
`0o4000`/`0o7777` 是**读掩码**（上报 mode 用）。`control_plane/` 里没有任何 `os.chown`
（§2 的结论，这轮复核仍成立）。

⇒ 显式 `0600` 那处唯一的读者是控制面自己；`1777` 那处是给沙箱 uid 的卷根；其余都世界可读。
**没有一处显式模式把文件关到"写者之外的读者读不到"**（`0600` 的读者就是写者）。

### 6.3 `_volumes` 的当前属主与切片由谁创建

* `_volumes` 属主：**`0755 65534:65534`**（§6.1）—— §13.8 的 `0:0 755` 是 09-28 的旧读数，
  C1 wave 2 的 `migrate-state-owner.sh` 已把它连同其余 7 条改到 65534。
* 切片目录 = `_volumes/<volume_id>/<sandbox_id>/`（`envd_service/volumes.py` 的 E2.5 形状）。
  **创建**它的是**目标节点的 agent**（root）：控制面在 create 时把 `materialize` 计划
  （含 `slices`，由 `control_plane/file_ops.py::_volume_slices` 派生）发给 agent，
  `c3_agent/materialize.py::_plan_slices` 落 `mkdir`；计划不可表达时回落到
  `envd_service/volumes.py::provision_sandbox_volume_mount` 自己 `mkdir`（worker 或 local 车道的
  控制面）。**交接**（`chmod 0770` 后 `chown` 到槽位 uid）由 agent 的 `chown-volume-slice` 做；
  卷根（`_volumes/<id>` 的 `1777`）由控制面卷仓建。
* 集群现状里 `_volumes` 只有一个 `_meta/`（`0755 65534:65534`），**没有活着的卷目录**，所以没有
  切片可 stat；`_meta` 下有几个旧 `.deleted` 墓碑是 `0:0 0644`（控制面还是 root 时留下的），
  迁移的递归 chown 会一并带上它们。

⇒ `_volumes` 今天已是 65534 的目录，迁到新 uid 是**纯属主变更**；切片归**槽位 uid**（不是控制面、
也不是 worker），所以换控制面的 uid 不碰切片的属主。

## 7. 关闭判据

* §5 第 6 条那套差分全绿 + `migrate-state-owner.sh --rollback` 演练过一次
  （回退必须**真的走过**，不能只写在纸上）；
* 控制面进程的 `CapEff` 仍为 0 —— 移出 65534 不能顺手带回任何 capability（这条必须实测，
  因为"能写别人的目录"的正统做法就是加 capability，而那正是要避免的）。

**本次没有改动任何配置** —— 这是评估，不是实施。
