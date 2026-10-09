# 控制面移出 uid 65534（含共享 `_oci/` 回收归属）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让控制面主容器以自己的专用 uid 运行（不再与 worker / agent 面 A 共用 65534），
从而关掉"被攻破的 agent 可 ptrace 读控制面内存"那条路，同时把共享 `_images/_oci/` 的回收
归属收敛到控制面一处。

**Architecture:** C3 已经把特权文件动作（`chown`/`rm`/`walk`/`materialize`）交给每节点的 agent，
控制面自己只剩"写它自己的平台目录"。评估（`docs/security-audit/control-plane-uid-2026-10-09.md`）
把约束追到只剩一个**有条件的共写点**：`_images/_oci/` 由控制面创建、worker 曾经修剪 ——
而那两个 OCI 回收旋钮出厂没设，所以舰队里 worker 对它只读。于是本计划分两步：先把回收收归
控制面（Task 2），再做纯属主迁移（Task 4），中间用一条只读探针（Task 1）把生产真实 mode/owner
取回来当作迁移的输入。

**Tech Stack:** Kubernetes manifest（`deploy/k8s/*.yaml`）、docker compose（本地车道）、
pytest（`tests/unit`）、`deploy/scripts/`（迁移与验收脚本）。

**Spec:** `docs/security-audit/control-plane-uid-2026-10-09.md`（§2 清点、§4.1 共写点、§5 方案、
§6 待测三条、§7 关闭判据）；背景与收益见 `docs/security-audit/c3-agent-hostpid-2026-10-09.md` §5。

## Global Constraints

- 新 uid / gid **65533**（worker 与 agent 面 A 保持 65534，槽位池 uid 段不动）。
- 控制面**不得获得任何 capability**（`CapEff` 必须恒为 0）；"能写别人的目录"不许用 capability 换。
- `_images` 对 worker 是**只读+遍历**（目录 `0755`、文件 `0644`）：`E2B_IMAGE_CACHE_OCI_MAX_BYTES`
  与 `E2B_IMAGE_CACHE_OCI_STALE_S` **保持不设**（`0`）；谁要打开它们，必须同批给 worker 写位。
- 节点本地的 rootfs 缓存（`/var/lib/e2b-images`、`E2B_IMAGE_CACHE_MAX_BYTES=4294967296`、
  `E2B_IMAGE_CACHE_EVICT_MIN_AGE_S=300`）**留在 worker**，本计划不碰。
- k8s 与 compose 两份清单同批改；测试断言精确匹配（禁 `toContain`/`in` 部分匹配），
  RED→GREEN 原文留档；本地车道先绿再考虑上线。

## Review Focus

1. **迁移中途的窗口**：属主已改、控制面未滚（或反之）时，两个身份都必须仍能工作 —— 期望是
   "迁移先、滚动后"，且期间只读接口不报错。
2. **`_images` 上的并发写**：控制面重建同一模板时清旧 tar（`api/templates.py:285`）与 worker
   读 tar 同时发生 —— 期望是 worker 要么读到完整 tar，要么得到具名错误，不出现半个文件。
3. **回收判据的边界**：仅当模板被重建或删除时才回收 tar；一个仍被模板记录引用的 tar 绝不能删。
4. **回退**：反向 chown 必须真的能跑通并让旧 uid 的控制面起来（不只写在文档里）。
5. **只看模式的假设**：控制面写的文件若落到 `0600` 且读者非 root，迁移后才会暴露 —— 期望是
   Task 1 的探针把这类文件列出来，而不是等车道红。

---

### Task 1: 只读探针 —— 取回生产的真实 mode/owner（迁移的输入）

**Files:**
- Create: `probe_platform_paths_shape`（放 `deploy/scripts/acceptance/`，本计划的钉子只认已存在的脚本）
- Test: `tests/unit/test_probe_platform_paths_shape.py`

**Interfaces:**
- Produces: `platform_path_shape(root: Path) -> list[dict[str, str | int]]` —— 对给定 export 根
  返回 8 条平台路径的 `{"path","mode","uid","gid"}`（`mode` 为八进制字符串，如 `"0o755"`）。
- Produces: `mode_is_world_readable(entry) -> bool` —— 供 Task 4 的迁移判据复用。

- [ ] **Step 1: 写失败用例**

```python
def test_shape_reports_the_eight_platform_paths(tmp_path):
    (tmp_path / "state").mkdir()
    entries = platform_path_shape(tmp_path)
    assert [e["path"] for e in entries] == [
        "_builds", "_images", "_secrets", "_snapshots", "_templates", "_volumes",
        "state", "workspaces/_migrate",
    ]
    assert entries[6]["mode"] == "0o755"
    assert entries[6]["uid"] == os.getuid()
```

- [ ] **Step 2: 跑，确认红** — `pytest tests/unit/test_probe_platform_paths_shape.py -q` ⇒ ImportError
- [ ] **Step 3: 实现** —— 只 `os.lstat`，不写盘；缺的路径报 `mode="missing"` 而不是抛
- [ ] **Step 4: 跑，确认绿**；再在集群上跑一次（`KUBECONFIG` + 隧道自检），把输出贴进
  `docs/security-audit/control-plane-uid-2026-10-09.md` §6 作为实测替换掉"待测"
- [ ] **Step 5: Commit** — `git commit -m "feat(probe): read the platform paths' real mode and owner"`

### Task 2: 共享 `_oci/` 的回收收归控制面

**Files:**
- Modify: `control_plane/api/templates.py`（重建路径已有 `for stale in (oci_tar, oci_link): stale.unlink()`；新增"模板删除/重命名时回收"）
- Modify: `envd_service/runtime/image_resolver.py:1754`（worker 不再对 `E2B_IMAGE_OCI_DIR` 调度 prune）
- Test: `tests/unit/test_template_oci_reclaim.py`

**Interfaces:**
- Produces: `reclaim_oci_artifacts(platform_root: Path, image: str) -> list[Path]` —— 删除
  `<platform_root>/_images/_oci/<slug>.oci.tar` 与其旁路 link，返回被删路径；对不在模板记录里的
  image **拒绝**（具名 `ValueError`）。

- [ ] **Step 1: 写失败用例**（tar+link 被删；仍被模板引用的 image ⇒ 拒绝且两文件都还在）
- [ ] **Step 2: 跑，确认红**
- [ ] **Step 3: 实现**（判据只认模板记录，不看 mtime；`unlink(missing_ok=True)` 两处）
- [ ] **Step 4: 跑整档** `pytest tests/unit -q`，失败集合必须仍是既有的 3 个平台项
- [ ] **Step 5: 删掉 worker 侧那次 `_schedule_cache_prune(producer)`**，并在该处留一行注释：
  "producer 目录的回收归控制面（Task 2）；worker 只读它"
- [ ] **Step 6: Commit**

### Task 3: 两份清单：控制面换 uid，`_images` 只读给 worker

**Files:**
- Modify: `deploy/k8s/control-plane.yaml:120-121`（`runAsUser`/`runAsGroup` → 65533）
- Modify: `deploy/k8s/worker.yaml`（`_images` 只读的注释 + 两个 OCI 旋钮不得设的说明）
- Modify: `deploy/compose/docker-compose.multinode.yml:225`（`user: "65533:65533"`，agent/worker 不动）
- Test: `tests/unit/test_control_plane_uid.py`

**Interfaces:**
- Produces: 无新函数；产出是清单常量（两条断言钉住）。

- [ ] **Step 1: 写失败用例**（CP 主容器 `runAsUser == 65533`；**且** worker 里不含
  `E2B_IMAGE_CACHE_OCI_MAX_BYTES`/`_STALE_S`；worker 的 `E2B_IMAGE_CACHE_DIR` 仍是节点本地）
- [ ] **Step 2: 跑，确认红** → **Step 3: 改三份文件** → **Step 4: 跑，确认绿**
- [ ] **Step 5: Commit**

### Task 4: 迁移脚本（属主 + 组位）与回退演练

**Files:**
- Modify: `deploy/scripts/migrate-state-owner.sh`（`--uid` 参数化，默认仍是 65534；白名单不变）
- Test: `tests/unit/test_migrate_state_owner_uid.py`

**Interfaces:**
- Consumes: Task 1 的 `platform_path_shape` 判据（迁移后逐条复核 mode/uid）。
- Produces: `deploy/scripts/migrate-state-owner.sh --uid 65533 [--apply]`；`--rollback` 走同一张映射表反向。

- [ ] **Step 1: 写失败用例**（`--uid 65533` 时渲染出的 Job 里 `chown` 目标 uid 是 65533；
  不带 `--uid` 时逐字等于今天 65534）
- [ ] **Step 2: 跑，确认红** → **Step 3: 实现参数** → **Step 4: 跑，确认绿**
- [ ] **Step 5: 演练回退**：`--apply` 后立刻 `--rollback --apply`，两次都贴出探针读数
- [ ] **Step 6: Commit**

### Task 5: 本地车道差分 → 上线 → 现场复核

**Files:**
- Create: `cp_uid_lane_diff`（放 `deploy/scripts/acceptance/`）（一条命令跑完全部判据）
- Test: 无单测；产出是读数，落 `docs/reports/`

**Interfaces:**
- Consumes: Task 3 的清单取值、Task 4 的迁移脚本。

- [ ] **Step 1: 起车道** `docker compose -f deploy/compose/docker-compose.multinode.yml up -d`
- [ ] **Step 2: 跑判据**（全绿才算过）：create / 首条命令 / `GET /logs` / pause+resume /
  snapshot+restore / **模板构建**（buildkit → `_oci` → worker 解包）/ 卷挂载读写 / delete，
  外加 `deploy/scripts/multinode_smoke.py` 与 `deployment_smoke.py`
- [ ] **Step 3: 存读数**（`docs/reports/` + 本计划追加"执行记录"一节）
- [ ] **Step 4: 上线**：先迁移属主（Task 4）→ 再滚控制面（Task 3 的清单）→
  复核控制面 `CapEff` 为 0、`_images` 属主为 65533、worker 仍能解包
- [ ] **Step 5: 回退杆演练**（一次）：`migrate-state-owner.sh --rollback --apply` + 滚回旧 uid
- [ ] **Step 6: Commit**（含发版记录；`docs/deploy-clusters.md` 新增一节）
