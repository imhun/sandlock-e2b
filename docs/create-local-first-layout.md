# 沙箱盘上材料的介质归属（根重切后的目录地图）

> **Task 0 Step 1 的产品**，2026-10-02 在集群实测（自建 k0s，2 节点全 arm64，
> `sandlock` namespace）。判定标准只有一条：**这份数据被"另一个节点上的读者"
> 需要吗** —— 需要就必须共享，否则可以本地。
>
> 原始的逐条清单在 `tmp/sandbox-tree-inventory.md`（gitignored），本文件是它的
> 结论面 + 根重切之后的目标形状。

## 0. 为什么要有这份地图

今天一根 `E2B_WORKSPACE_BASE` 同时决定三件事：

1. 沙箱树在哪（`<shared>/workspaces/<id>`）；
2. 平台自己的共享命名空间挂在它下面的哪（`_snapshots` 载荷、`_migrate`）；
3. "树是不是共享"这个迁移判据取什么值
   （`control_plane/api/sandboxes.py:2687` 的 `shared = bool(settings.shared_workspace_root)`）。

三件事共用一根，所以"把树搬去节点本地盘"不是改一个值：改了它，平台命名空间会一起
被拖到本地盘上，跨节点的读者就再也看不到它们了。根重切要做的就是**把这根拆开**。

---

## 1. 五个根（根重切之后）

| 变量 | 重切前的值 | 重切后的值 | 介质 | 谁写 | 谁读（跨节点？） |
|---|---|---|---|---|---|
| `E2B_WORKSPACE_BASE` | `<shared>/workspaces` | 同左（Task 3 才改指 `/var/lib/e2b/workspaces`） | 共享 → **Task 3 起节点本地** | worker / agent 建树；**沙箱自己**在其上读写 | ① 沙箱（同节点，bind mount）② worker files API（同节点）③ **迁移目标节点**（经裁定 2 的 tar 中转）④ agent 拍快照（同节点） |
| `E2B_NODE_STATE_BASE`（新） | — | `/var/lib/e2b/state` | **节点本地** hostPath | Task 4 起：`.creating` / disk-stats / `.route-b/**` / uid 池本地件 | 只有本节点（同节点） |
| `E2B_STATE_BASE` | `<shared>/state` | 同左 | 共享 | worker（`_runtime` 记录、命令日志）、沙箱 slot 进程（checkpoint） | ① 本节点 worker ② **别的节点的 worker**（`uid_pool._recorded_uids`）③ CP 只推路径，不直读（远程形态是代理） |
| `E2B_SHARED_VOLUME_ROOT` | `<shared>` | 同左（`_snapshots`、`_migrate` 搬进来） | 共享 | CP（`_builds`/`_templates`/`_volumes/_meta`/`_snapshots` 记录）、agent（`_snapshots` 载荷） | CP 两个副本之间；`_oci.tar` 是唯一的真·节点间交付面 |
| `E2B_IMAGE_CACHE_DIR` | `/var/lib/e2b-images` | 同左 | **节点本地** hostPath | 本节点 worker（解包 rootfs） | 只有本节点 |

`<shared>` = `/var/lib/e2b-sandboxes`，共享 PVC（阿里云 NAS）。

**要点**：重切**不动介质**，只把"谁在哪"写清楚。介质翻转是 Task 3 的事，那时只改
`E2B_WORKSPACE_BASE` 的指向与 `E2B_TREES_SHARED` 这一个判据。

---

## 2. 两条搬家

### 2.1 `_snapshots`：两个命名空间 → 一个（**实测矩阵**）

`control_plane/registry/snapshots.py:340-361` 的注释说记录与载荷是同处同一对象，
但实测是两个目录、同一批 id：

| id | `<shared>/_snapshots/<id>/`（记录，控制面写） | `<workspaces>/_snapshots/<id>/`（载荷，agent 写） | 时间 |
|---|---|---|---|
| `snap_015f907ace11c1ea` | `snapshot.json` | — | 09-27 |
| `snap_1ca5ab3332906e32` | `snapshot.json` | — | 09-27 |
| `snap_46dc467759dbbfb7` | `snapshot.json` | `.complete` + `fs/` | 10-01 |
| `snap_4bf1225dfbf54e0c` | `snapshot.json` | `.complete` + `fs/` | 10-01 |
| `snap_962c14802d6cbd50` | `.complete` + `fs/` + `snapshot.json` | — | **09-26** |
| `snap_ce90ef9852fc6809` | `snapshot.json` | `.complete` + `fs/` | 09-27 |
| `snap_2bb1fe18f81d4d77` | — | `.complete` + `fs/` | 10-01 |
| `snap_a6e1470cca1c2c09` | — | `.complete` + `fs/` | 10-01 |

三类，各有各的处置：

* **两边都有**（`46dc…`、`4bf1…`、`ce90…`）⇒ 把载荷目录**移到记录旁边**，一个 id
  一个目录。这是"合一"的正面情形，`rename(2)` 即可。
* **只有记录**（`015f…`、`1ca5…`）⇒ 有记录没载荷，恢复会失败。合并脚本**不许**替它
  编一个载荷，也不许把记录删掉；原样留着并**具名报告**（这是数据问题，不是格式问题）。
* **只有载荷**（`2bb1…`、`a6e1…`）⇒ 有载荷没记录。同样原样留着 + 具名报告：
  记录是控制面在成功之后写的，所以"载荷在、记录不在"意味着那次快照没成功收尾。

**时间线给的因果**：`snap_962c14802d6cbd50`（09-26）三样都在共享根，是**下沉前的
布局**；从 09-27 起载荷开始落在 `<workspaces>/_snapshots/`。所以这多半是 N27
"树根下沉"顺手把载荷根与记录根拆开了 —— 根重切把它合回来，正好和
`registry/snapshots.py` 的注释对上。

**合并后的形状**：

```
<shared>/_snapshots/<id>/snapshot.json      ← 控制面（记录）
<shared>/_snapshots/<id>/fs/ + .complete    ← agent（载荷；Task 2 换成 fs.tar）
```

### 2.2 `_migrate` 上浮到共享根

今天：`<workspaces>/_migrate`（`envd_service/agent.py:4199`、`:4260`、
`control_plane/api/sandboxes.py:2570`），是**空**的，只在
`if not shared:` 的迁移分支里用 —— 而那两条分支今天**从没跑过**
（`E2B_SHARED_WORKSPACE_ROOT` 在清单里设着，判据恒为 `1`）。

按裁定 2，跨节点一律经共享中转，所以这个中转面必须在**共享根**
（`<shared>/_migrate`）：它的读者是**另一个节点**的目标 agent，挂在树根命名空间下
那边根本看不到。

---

## 3. 刻意不动

已经在共享根、且其读者确实跨节点或跨 CP 副本的：`_images`（`_oci/*.oci.tar` 是
唯一的真·节点间交付面）、`_templates`、`_builds`、`_volumes`、`_secrets`、`state/`。

`_cow` 是空的保留名（`gateway_common/paths.py` 的 `RESERVED_PLATFORM_NAMESPACES`），
名单不删。

卷数据（`_volumes/<id>/` 切片）按 `volume_node_id` 钉在节点上，本轮不动 —— 它的
"可以本地"取决于调度器是否愿意把卷也放本地，那要单独立项。
