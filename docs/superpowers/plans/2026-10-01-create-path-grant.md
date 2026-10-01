# 建箱临时授权（materialize-tree grant）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把建箱期的材料化（建树 / 快照拷贝 / 改属主 / 卷切片）从"worker 自己做 + 控制面逐次转发 chown"改成"控制面签一张 10 s 单次授权、worker 直连本节点 agent、agent 一次做完"，并让建箱有一个"建箱中"标记供拆除等待。

**Architecture:** 三角分工不变（CP 决策 / agent 执行 / worker 跑沙箱）。新增一条**用时现铸**的授权通道：worker 向 CP 领一张 HMAC 签名的 `materialize-tree` 计划，拿它直连面 B；面 B 校验六步后用硬化过的 Python 实现 + 已审计的 `e2b-maint` 完成材料化。拆除侧加一个 `.creating` 标记，使"记录写移出响应路径"安全。

**Tech Stack:** Python 3.14（`control_plane/`、`c3_agent/`、`envd_service/`、`gateway_common/`）、C（`c3_agent/priv/maint.c`，本计划不改它）、k8s（k0s，arm64）、NFS（阿里云 NAS）。

**Spec:** `docs/superpowers/specs/2026-10-01-create-path-grant-design.md`（提交 `5019b69`）。

## Global Constraints

- 授权 TTL：`E2B_CREATE_GRANT_TTL_S` 默认 **10**；agent 拒绝 `exp - iat > 60` 或 `exp < iat`。
- 授权**单次消费**（jti）；重复使用要回答具名 `grant already used`，worker 的规则是**重新领一张再试一次**。
- worker **绝不**在授权里或请求里报路径：`materialize-tree` 的每条路径都由控制面从自己的记录/设置推导（§14.4 硬规则二）。
- agent 侧**独立复核**每条路径落在自己的四根白名单内（realpath），与 CP 的推导不互相替代。
- §4.3.1 的四条硬要求：源侧不跟随符号链接（用 `symlink()` 重建）；目标侧逐段 `O_NOFOLLOW`，遇符号链接**具名拒绝**；半棵树不得报成功；跨节点迁移保文件不许丢。
- 拆箱/巡检（`rm`/`walk`/`remove-workspace`/`remove-runtime`）**仍逐次经控制面**，不进这条通道。
- 回退必须是**具名降级**：面 B 没有新路由或授权通道不可用时，记 WARNING 并走老路（worker 自己建树/拷贝 + 控制面中转），步骤照样执行，绝不静默跳过。
- 只在**建箱**路径上使用新通道；`local://` 形态与 `_provision_local` 不动。

## Review Focus

1. **快照里的符号链接**：源侧必须被**重建为链接**，不得解引用（否则 root 级逃逸）。→ Task 4
2. **目标树里预置的符号链接**：`dirs_exist_ok` 的合并（迁移/重建保文件）会写进一个沙箱能改过的树；任何一段是符号链接都必须**具名拒绝**。→ Task 4
3. **授权被重放 / 过期 / 跨节点使用**：单次消费 + host 绑定 + 10 s 过期 + 60 s 上限。→ Task 1 / Task 3
4. **建箱中途死掉**：`.creating` 标记必须让拆除**有界等待**后按"未完成的建箱"回收，不能挂死、也不能把半棵树当活沙箱。→ Task 7
5. **滚动升级**（新 worker + 旧 agent / 旧 worker + 新 agent）：两条方向都必须仍能建箱成功。→ Task 5

---

## 文件结构

| 文件 | 责任 |
|---|---|
| `gateway_common/create_grant.py`（新） | 授权的载荷 / 规范化 / HMAC 签名与校验（CP 签、agent 验，同一份代码） |
| `gateway_common/paths.py`（改） | 新增建箱标记路径 helper |
| `control_plane/file_ops.py`（改） | 词表新增 `materialize-tree`；`derive_materialize()` 推导计划（树 + 卷切片） |
| `control_plane/api/internal.py`（改） | `POST /internal/nodes/{node_id}/file-grant`：身份门 + 记录/属主校验 + 推导 + 铸权 |
| `control_plane/config.py`（改） | `create_grant_ttl_s`（`E2B_CREATE_GRANT_TTL_S`，默认 10） |
| `c3_agent/materialize.py`（新） | 硬化过的材料化实现（mkdir / 递归拷贝 / 卷切片）+ 交给 runner 做 chown |
| `c3_agent/app.py`（改） | `POST /internal/grants/file-op`：六步校验 + 单次消费 + 调 `materialize` |
| `envd_service/agent_fileops.py`（改） | `materialize()`：领权 → 直连面 B；失败分类（未支持 ⇒ 具名降级） |
| `envd_service/agent.py`（改） | `_agent_create_sandbox` 改用 `materialize()`；写/摘建箱标记；拆除等待 |
| `envd_service/runtime/registry.py`（改） | `register(..., persist=False)` + `persist(sandbox_id)` |
| `envd_service/volumes.py`（改） | 拆出"只算挂载计划"的纯函数，供计划推导与 worker 记录复用 |

---

### Task 1: 授权的载荷与签名（共享模块）

**Files:**
- Create: `gateway_common/create_grant.py`
- Test: `tests/unit/test_create_grant.py`

**Interfaces:**
- Produces: `OP = "materialize-tree"`；`MAX_TTL_S = 60`；`class GrantRefusal(Exception): reason: str`；`mint(payload: dict, *, secret: str) -> str`；`verify(token: str, *, secret: str, host: str, now: float | None = None) -> dict`。
- 载荷字段（spec §4.1，逐字）：`v`、`host`、`sandbox_id`、`op`、`tree`、`slices`、`jti`、`iat`、`exp`。

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_create_grant.py`：固定 `secret="sekret"`、`host="node-a"`、`iat=1000`、`exp=1010` 造一张合法授权，断言：
`test_round_trip_returns_the_payload`（`verify` 回来的 dict 逐字段相等）；
`test_a_tampered_payload_is_refused`（改 payload 一个字符 ⇒ `GrantRefusal.reason == "bad-signature"`）；
`test_a_malformed_token_is_refused`（`"no-dot"`、`"a.b.c"`、非 base64 ⇒ `"bad-format"`）；
`test_another_hosts_grant_is_refused`（`host="node-b"` ⇒ `"wrong-host"`）；
`test_an_expired_grant_is_refused`（`now=1011` ⇒ `"expired"`）；
`test_a_grant_from_the_future_is_refused`（`now=990` ⇒ `"not-yet-valid"`，留 5 s 时钟余量）；
`test_a_long_lived_grant_is_refused`（`exp=iat+61` ⇒ `"ttl-too-long"`）；
`test_another_op_is_refused`（`op="chown-workspace"` ⇒ `"op-not-allowed"`）；
`test_the_signature_is_over_the_canonical_form`（同一 payload 不同字典插入顺序 ⇒ 同一 token）。

- [ ] **Step 2: Run tests to verify they fail**

Run: `env -u all_proxy -u http_proxy -u https_proxy tmp/venv/bin/python -m pytest tests/unit/test_create_grant.py -q`
Expected: FAIL（`ModuleNotFoundError: gateway_common.create_grant`）

- [ ] **Step 3: Implement `gateway_common/create_grant.py`**

```python
def _canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()

def mint(payload: dict, *, secret: str) -> str:
    raw = _canonical(payload)
    sig = hmac.new(secret.encode(), raw, hashlib.sha256).digest()
    return f"{_b64(raw)}.{_b64(sig)}"
```

`verify` 的顺序：拆两段（否则 `bad-format`）→ 重算签名并 `hmac.compare_digest`（否则 `bad-signature`）
→ 载荷必须是 dict 且有 `v == 1`（否则 `bad-format`）→ `op == OP`（否则 `op-not-allowed`）
→ `host` 相等（否则 `wrong-host`）→ `iat <= now + 5`（否则 `not-yet-valid`）→ `now <= exp`（否则 `expired`）
→ `0 < exp - iat <= MAX_TTL_S`（否则 `ttl-too-long`）。`now` 缺省用 `time.time()`。

- [ ] **Step 4: Run tests to verify they pass**

Run: 同 Step 2。Expected: PASS（9 passed）。

- [ ] **Step 5: Commit**

```bash
git add gateway_common/create_grant.py tests/unit/test_create_grant.py
git commit -m "feat(grant): 建箱授权的载荷与 HMAC 签名（签在 CP、验在面 B）"
```

---

### Task 2: 控制面铸权端点

**Files:**
- Modify: `control_plane/file_ops.py`（词表 + `derive_materialize`）
- Modify: `control_plane/config.py`（`create_grant_ttl_s`）
- Modify: `control_plane/api/internal.py`（新端点）
- Test: `tests/unit/test_file_grant_endpoint.py`

**Interfaces:**
- Consumes: `gateway_common.create_grant.{OP, MAX_TTL_S, mint}`；`control_plane.file_ops.control_paths`。
- Produces: `file_ops.derive_materialize(state, record, settings, *, snapshot_id: str | None) -> dict`，返回 `{"tree": {"path", "subdir", "mode", "uid", "gid", "copy_from"?}, "slices": [{"volume", "path", "uid", "gid"}]}`；端点 `POST /internal/nodes/{node_id}/file-grant` 返回 `{"agentURL": str, "grant": str, "expiresAt": int}`。

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_file_grant_endpoint.py`（沿用 `tests/unit/test_provision_remote_client.py` 的 `SimpleNamespace` 夹具风格）：
`test_the_grant_names_the_agent_host_and_verifies`（用同一 secret + `host=<节点身份>` 能验过；`tree.path == <workspace base>/<id>`；`tree.uid == record.host_uid`）；
`test_another_nodes_sandbox_is_refused`（403）；
`test_an_unknown_sandbox_is_refused`（404）；
`test_an_op_outside_the_vocabulary_is_refused`（400）；
`test_the_ttl_comes_from_settings_and_is_capped`（`create_grant_ttl_s=600` ⇒ 签出来的 `exp - iat == MAX_TTL_S`）；
`test_a_snapshot_create_carries_copy_from`。

- [ ] **Step 2: Run tests to verify they fail**

Run: `env -u all_proxy -u http_proxy -u https_proxy tmp/venv/bin/python -m pytest tests/unit/test_file_grant_endpoint.py -q`
Expected: FAIL（404 / `derive_materialize` 不存在）

- [ ] **Step 3: Implement**

1. `config.py`：`create_grant_ttl_s: int = field(default_factory=lambda: int(os.getenv("E2B_CREATE_GRANT_TTL_S", "10")))`。
2. `file_ops.py`：词表增一行（保持"这张表就是平台能要求节点做什么"的完整性），并写 `derive_materialize()`：树 = `<workspace base>/<id>`、`subdir="workspace"`、`mode="0770"`、`uid=record.host_uid`、`gid=node.worker_gid`、`copy_from=<workspace base>/_snapshots/<snapshot_id>/fs`（仅当给了 snapshot_id 且该目录属于记录）；切片按记录里的卷列表去重。每条路径都过 `file_ops` 的根检查。
3. `internal.py` 新端点：`_require_node_identity` → 解析 body `{op, sandbox_id, snapshot_id?}` → `registry.get` + 属主校验（与 `node_file_op` 同一段逻辑抽成小函数，避免两份）→ `derive_materialize` → `c3_agent_client.resolve_agent(node_id)` 取 URL 与 agent 身份 → `mint(...)`，`exp = iat + min(settings.create_grant_ttl_s, MAX_TTL_S)`。

- [ ] **Step 4: Run tests to verify they pass**

Run: 同 Step 2。Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add control_plane/file_ops.py control_plane/config.py control_plane/api/internal.py tests/unit/test_file_grant_endpoint.py
git commit -m "feat(cp): /internal/nodes/{node}/file-grant —— 按记录推导材料化计划并签 10 s 单次授权"
```

---

### Task 3: 面 B 的授权入口 + 建树/改属主（先不做拷贝）

**Files:**
- Create: `c3_agent/materialize.py`
- Modify: `c3_agent/app.py`
- Test: `tests/unit/test_agent_grant_route.py`

**Interfaces:**
- Consumes: `gateway_common.create_grant.verify`；`c3_agent.fileops.SubprocessMaintRunner`；`c3_agent.config.Settings`。
- Produces: `POST /internal/grants/file-op`（body `{grant}`，成功 200 `{"materialized": {...}}`）；`materialize.materialize_tree(plan, *, settings, runner) -> dict`；`class MaterializeRefusal(Exception): reason: str`，reason ∈ `path-outside-roots` / `destination-is-a-symlink` / `partial-copy` / `already-exists-as-a-file`。

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_agent_grant_route.py`（`httpx.ASGITransport` 直接打 `create_app`，runner 用记录 argv 的假实现）：
`test_a_valid_grant_creates_and_chowns_the_tree`（树与 `workspace/` 都在；runner 收到的 argv 是 `chown --uid U --gid G --recursive --path <root>`）；
`test_the_mode_and_group_come_from_the_plan`（`0770` + plan 里的 gid）；
`test_a_path_outside_the_roots_is_refused_named`（plan 里塞 `<workspace base>/../../etc` ⇒ 400 `path-outside-roots`，且**没有**调用 runner）；
`test_a_grant_is_single_use`（第二次同一 grant ⇒ 409 `grant already used`）；
`test_a_bad_signature_is_refused`（401）；
`test_an_unknown_op_in_the_grant_is_refused`（400）；
`test_two_sandboxes_grants_do_not_interfere`（两张 grant 各自建自己的树）。

- [ ] **Step 2: Run tests to verify they fail**

Run: `env -u all_proxy -u http_proxy -u https_proxy tmp/venv/bin/python -m pytest tests/unit/test_agent_grant_route.py -q`
Expected: FAIL（405/404：路由不存在）

- [ ] **Step 3: Implement**

1. `c3_agent/app.py`：新路由，**不复用** `_require_key`（那条是 CP token 的）：读 `{grant}` → `verify(secret=E2B_C3_AGENT_TOKEN, host=本 agent 身份)` → 单次消费表（`dict[str, float]` + TTL 清理，键 `jti`）→ `materialize_tree(...)`。
2. `c3_agent/materialize.py`：`_resolve_inside(path, roots)`（realpath + 必须落在四根内）；建树 = `os.makedirs(<root>/<subdir>, exist_ok=True)` + `os.chmod(0o770)`；**改属主交给 runner**（exec 已审计的 `e2b-maint chown --uid --gid --recursive --path`），不自己 `os.chown`。

- [ ] **Step 4: Run tests to verify they pass**

Run: 同 Step 2。Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add c3_agent/materialize.py c3_agent/app.py tests/unit/test_agent_grant_route.py
git commit -m "feat(agent): /internal/grants/file-op —— 六步校验 + 单次消费 + 建树/改属主"
```

---

### Task 4: 硬化过的递归拷贝（本计划最危险的一块）

**Files:**
- Modify: `c3_agent/materialize.py`
- Test: `tests/unit/test_agent_grant_route.py`（同文件新增）

**Interfaces:**
- Produces: `materialize.copy_tree(src: str, dst: str) -> int`（返回拷贝的条目数）；失败一律 `MaterializeRefusal`。

- [ ] **Step 1: Write the failing tests**

`test_a_symlink_in_the_snapshot_is_recreated_not_followed`（源里有 `link -> /etc/passwd` 与一个指向树外的目录链接；断言目标是**符号链接**、`os.readlink` 与原值相同，且外部文件内容/mtime 不变）；
`test_a_symlinked_destination_segment_is_refused_named`（目标树里预置 `sub -> <树外目录>`，源里有 `sub/file` ⇒ `destination-is-a-symlink`，且树外目录下**没有**新文件）；
`test_an_ordinary_merge_keeps_existing_files`（目标里上一个化身留下的普通文件仍在，源里的同名文件被覆盖）；
`test_a_file_where_a_directory_belongs_is_refused_named`（`already-exists-as-a-file`）；
`test_a_partial_copy_is_reported_as_failure`（monkeypatch 拷贝循环抛一次 `OSError` ⇒ 抛错、reason `partial-copy`，不返回成功）。

- [ ] **Step 2: Run tests to verify they fail**

Run: `env -u all_proxy -u http_proxy -u https_proxy tmp/venv/bin/python -m pytest tests/unit/test_agent_grant_route.py -q -k "symlink or merge or partial"`
Expected: FAIL（`copy_tree` 不存在）

- [ ] **Step 3: Implement `copy_tree`**

算法（签名与测试之外的部分由这里定死，逐条照做）：
1. **源侧**：`os.scandir()` 递归；**先判 `entry.is_symlink()`**（再判 `entry.is_dir(follow_symlinks=False)`）：符号链接 ⇒ `os.symlink(os.readlink(src), dst)`，**绝不**解引用；目录 ⇒ 递归；普通文件 ⇒ `os.open(src, O_RDONLY | O_NOFOLLOW)` 打开后拷贝内容。
2. **目标侧**：每一段都用上一段的 fd 打开 —— `os.open(seg, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)`；命中符号链接（`ELOOP`）⇒ `MaterializeRefusal("destination-is-a-symlink")`。**禁止**用 `os.path.join` 拼字符串写目标。
3. 目标已存在且类型冲突（文件 vs 目录）⇒ `MaterializeRefusal("already-exists-as-a-file")`。
4. 拷贝途中任何 `OSError` ⇒ 包成 `MaterializeRefusal("partial-copy", ...)` 抛出（半棵树不许报成功）。
5. 权限/属主不在这里管：拷完由 runner 的 `chown --recursive` 一次性收口（Task 3 已接好）。

- [ ] **Step 4: Run tests to verify they pass**

Run: `env -u all_proxy -u http_proxy -u https_proxy tmp/venv/bin/python -m pytest tests/unit/test_agent_grant_route.py -q`
Expected: PASS（含 Step 1 的全部用例）。

- [ ] **Step 5: Commit**

```bash
git add c3_agent/materialize.py tests/unit/test_agent_grant_route.py
git commit -m "feat(agent): 硬化递归拷贝 —— 源侧不解引用、目标侧逐段 O_NOFOLLOW、半棵树不报成功"
```

---

### Task 5: worker 侧领权 + 直连 + 具名降级

**Files:**
- Modify: `envd_service/agent_fileops.py`
- Modify: `envd_service/agent.py`（`_agent_create_sandbox` 的建树/拷贝段）
- Test: `tests/unit/test_c3_fileops_worker.py`（新增）、`tests/unit/test_create_materialize_switch.py`（新增）

**Interfaces:**
- Produces: `AgentFileOps.materialize(sandbox_id: str, snapshot_id: str | None = None) -> dict`；新异常 `AgentMaterializeUnsupported(AgentFileOpsError)`。

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_c3_fileops_worker.py`：
`test_materialize_mints_then_calls_the_agent`（两次请求：先 `POST /internal/nodes/<node>/file-grant`，再 `POST <agent>/internal/grants/file-op`，后者 body 是 `{"grant": ...}`）；
`test_an_already_used_grant_is_re_minted_once`（agent 先回 409 `grant already used` ⇒ 再领一张重试；共 4 次请求，且第二次领到的 grant 与第一次不同）；
`test_an_agent_without_the_route_falls_back_named`（agent 回 404 ⇒ 抛 `AgentMaterializeUnsupported`）。

`tests/unit/test_create_materialize_switch.py`：
`test_a_supported_agent_materializes_once_and_skips_the_workers_own_copy`（`shutil.copytree` **没有被调用**）；
`test_an_unsupported_agent_falls_back_to_the_old_path_and_warns`（`copytree` 调用一次 + 一条 WARNING 含 sandbox id）。

- [ ] **Step 2: Run tests to verify they fail**

Run: `env -u all_proxy -u http_proxy -u https_proxy tmp/venv/bin/python -m pytest tests/unit/test_c3_fileops_worker.py tests/unit/test_create_materialize_switch.py -q -k materialize`
Expected: FAIL（`materialize` 不存在）

- [ ] **Step 3: Implement**

`AgentFileOps.materialize()`：用现成的 `_http()` 客户端先领权（控制面 URL 是 `self._url`），再打 agent；`409` 且 body 含 `already used` ⇒ 重新领权一次；`404`/连接类错误 ⇒ `AgentMaterializeUnsupported`（消息里带上原始原因）。`_agent_create_sandbox`：`materialize()` 成功 ⇒ 跳过本地建树/拷贝；`AgentMaterializeUnsupported` ⇒ WARNING + 原来的 `mkdir`/`copytree` 段照旧。

- [ ] **Step 4: Run tests to verify they pass**

Run: 同 Step 2。Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add envd_service/agent_fileops.py envd_service/agent.py tests/unit/test_c3_fileops_worker.py tests/unit/test_create_materialize_switch.py
git commit -m "feat(worker): 建箱改用 agent 材料化；面 B 无此路由时具名降级走老路"
```

---

### Task 6: 卷切片并入同一张授权

**Files:**
- Modify: `control_plane/file_ops.py`（`derive_materialize` 增 `slices`）
- Modify: `c3_agent/materialize.py`（建切片 + 一起 chown）
- Modify: `envd_service/volumes.py`（拆出纯函数 `volume_mount_plan(...)`）
- Modify: `envd_service/agent.py`（授权路径下不再自建切片）
- Test: `tests/unit/test_agent_grant_route.py`、`tests/unit/test_create_materialize_switch.py`

**Interfaces:**
- Produces: `volumes.volume_mount_plan(*, sandbox_id, volume_mounts, shared_volume_root, workspace_dir, fallback_mount_point) -> tuple[list[dict], list[dict]]`（只算，不碰盘；返回 `(mount_paths, volume_projects)`）；授权里的 `slices` 与它一一对应。

- [ ] **Step 1: Write the failing tests**

`test_the_plan_lists_one_slice_per_volume`（两个卷 ⇒ `slices` 两条，路径 = `<卷根>/<id>`）；
`test_the_agent_creates_and_chowns_every_slice`（两条切片目录都在，且 chown 覆盖它们）；
`test_a_slice_path_outside_the_volume_root_is_refused_named`；
`test_a_slice_not_in_the_plan_is_never_created`（授权里没有的切片路径不得被创建）。

- [ ] **Step 2: Run tests to verify they fail**

Run: `env -u all_proxy -u http_proxy -u https_proxy tmp/venv/bin/python -m pytest tests/unit/test_agent_grant_route.py tests/unit/test_create_materialize_switch.py -q -k slice`
Expected: FAIL。

- [ ] **Step 3: Implement**

控制面按记录的卷列表推导 `slices`（路径与 `volume_mount_plan` 同源）；面 B 建切片目录（同样逐段 `O_NOFOLLOW`）并一起交给 runner chown；worker 在授权路径成功时只用 `volume_mount_plan` 的结果写运行时记录，不做文件系统动作。

- [ ] **Step 4: Run tests to verify they pass**

Run: 同 Step 2。Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add control_plane/file_ops.py c3_agent/materialize.py envd_service/volumes.py envd_service/agent.py tests/unit/test_agent_grant_route.py tests/unit/test_create_materialize_switch.py
git commit -m "feat(grant): 卷切片并入 materialize-tree 计划，worker 不再自建切片"
```

---

### Task 7: 建箱标记 + 拆除有界等待（P2a）

**Files:**
- Modify: `gateway_common/paths.py`（`sandbox_creating_marker(...)`）
- Modify: `envd_service/agent.py`（建箱写/摘标记；`agent_delete_sandbox` 等待）
- Test: `tests/unit/test_create_marker.py`

**Interfaces:**
- Produces: `paths.sandbox_creating_marker(workspace_base, sandbox_id, *, state_base=None) -> Path`（`<state base>/_runtime/<id>/.creating`）；`envd_service.agent._await_inflight_create(sandbox_id, *, timeout_s) -> bool`。

- [ ] **Step 1: Write the failing tests**

`test_the_marker_exists_while_a_create_is_in_flight`（用慢的假 materialize 挂住建箱，期间标记存在）；
`test_the_marker_is_gone_once_the_create_succeeded`；
`test_a_delete_waits_for_the_in_flight_create_and_removes_nothing_twice`（拆除只发生一次；之后 `_runtime/<id>` 与 `<ws>/<id>` 都不存在）；
`test_a_stale_marker_is_reclaimed_instead_of_hanging`（标记 mtime 早于 `E2B_CREATE_WAIT_S` ⇒ 立即按未完成回收，204，不留残树）；
`test_the_marker_alone_does_not_make_a_record_look_live`（只有标记、没有 `sandbox.json` ⇒ `runtime_registry.get(id) is None`）。

- [ ] **Step 2: Run tests to verify they fail**

Run: `env -u all_proxy -u http_proxy -u https_proxy tmp/venv/bin/python -m pytest tests/unit/test_create_marker.py -q`
Expected: FAIL。

- [ ] **Step 3: Implement**

建箱：`_agent_create_sandbox` 在材料化**之前**写标记（建 runtime 目录 + 写一个字节，不 fsync），收尾之后 `unlink(missing_ok=True)`；删除：`agent_delete_sandbox` 先 `_await_inflight_create(...)`（轮询 ≤50 ms，上限 `E2B_CREATE_WAIT_S` 默认 60），超时或标记过期 ⇒ 走既有 `_delete_sandbox_runtime`（无记录时它本来就按约定路径拆）；`_scan_workspace_runtimes` 与 uid reconcile 把**新鲜的**标记视作"有建箱在飞"，不回收。

- [ ] **Step 4: Run tests to verify they pass**

Run: 同 Step 2。Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add gateway_common/paths.py envd_service/agent.py tests/unit/test_create_marker.py
git commit -m "feat(worker): 建箱标记 + 拆除有界等待 —— 杜绝'拆完又被建箱写回'的残留"
```

---

### Task 8: 记录写移出响应路径（P2b）

**Files:**
- Modify: `envd_service/runtime/registry.py`（`register(..., persist=False)` + `persist()`）
- Modify: `envd_service/agent.py`（建箱：内存登记 → 响应 → 后台落盘 → 摘标记 → `pool.commit`）
- Test: `tests/unit/test_create_deferred_persist.py`

**Interfaces:**
- Produces: `RuntimeRegistry.register(..., persist: bool = True)`；`RuntimeRegistry.persist(sandbox_id: str) -> bool`（失败返回 False，并保留标记）。

- [ ] **Step 1: Write the failing tests**

`test_the_create_response_does_not_wait_for_the_record_write`（monkeypatch `write_json_atomically` 睡 200 ms ⇒ 建箱耗时 < 100 ms，且**最终**记录出现在盘上）；
`test_the_marker_stays_until_the_record_is_durable`（落盘前标记在、落盘后标记消失）；
`test_a_failed_persist_keeps_the_marker_and_is_named`（`write_json_atomically` 抛 `OSError` ⇒ 标记仍在 + 一条具名 WARNING，`persist()` 返回 False）；
`test_the_rpc_path_sees_the_record_before_it_is_durable`（落盘前 `runtime_registry.get(id)` 已返回记录）。

- [ ] **Step 2: Run tests to verify they fail**

Run: `env -u all_proxy -u http_proxy -u https_proxy tmp/venv/bin/python -m pytest tests/unit/test_create_deferred_persist.py -q`
Expected: FAIL。

- [ ] **Step 3: Implement**

`register(persist=False)` 只做内存登记（含 uid 的账）；`persist()` 复用原 `register` 的磁盘段（`write_json_atomically` + legacy unlink），把原来吞掉 `OSError` 的写法改成**返回 False + 具名 WARNING**；`_agent_create_sandbox` 在材料化成功后 `register(persist=False)`，响应之后（`asyncio.create_task` + `to_thread`）调 `persist()`，成功再摘标记、再 `pool.commit()`。

- [ ] **Step 4: Run tests to verify they pass**

Run: 同 Step 2。Expected: PASS。

- [ ] **Step 5: Run the neighbouring lanes**

Run: `env -u all_proxy -u http_proxy -u https_proxy tmp/venv/bin/python -m pytest tests/unit/test_uid_pool.py tests/unit/test_create_marker.py tests/unit/test_c3_fileops_worker.py tests/unit/test_c3_fileop_degradation.py -q`
Expected: PASS（uid 池的 marker/commit 语义不被 defer 破坏）。

- [ ] **Step 6: Commit**

```bash
git add envd_service/runtime/registry.py envd_service/agent.py tests/unit/test_create_deferred_persist.py
git commit -m "feat(worker): 运行时记录写移出响应路径（标记保证拆除不与建箱赛跑）"
```

---

### Task 9: 集群验收 + 文档

**Files:**
- Modify: `docs/deploy-clusters.md`（§7.27）、`docs/open-issues.md`（N56）、`README.md`（① 的数字）、`docs/c3-privilege-relocation.md`（§14.1 第四行加"建箱材料化例外"并指回 spec）

**Interfaces:**
- Consumes: `deploy/scripts/acceptance/create_latency_probe.py`、`worker_provision_cost.py`、`E2B_CREATE_TRACE=1`。

- [ ] **Step 1: 从干净 worktree 构建并上线**

```bash
git worktree add --detach tmp/build-wt <本计划末次提交>
cp -R wheels tmp/build-wt/wheels && cp deploy/scripts/acr.env tmp/build-wt/deploy/scripts/acr.env
screen -dmS e2bbuild sh -c "cd $(pwd)/tmp/build-wt && ./deploy/scripts/build-and-push.sh > $(pwd)/tmp/k0s/release-materialize.log 2>&1; echo EXIT=\$? >> $(pwd)/tmp/k0s/release-materialize.log"
```
`EXIT=0` 后写 `deploy/stack/.version`，再 `./deploy/k8s-k0s/apply.sh`。

- [ ] **Step 2: 量延迟**

```bash
kubectl -n sandlock exec -i <cp-pod> -c control-plane -- python3 - --base http://127.0.0.1:3000 --key "$E2B_API_KEY" --n 10 < deploy/scripts/acceptance/create_latency_probe.py
```
Expected: p50 **145–150 ms**（改前 191 ms）；开 `E2B_CREATE_TRACE=1` 后 `fileop:*` 段消失。

- [ ] **Step 3: 冒烟与残留**

`MULTI-NODE SMOKE OK` + `DEPLOYMENT SMOKE OK`（含跨节点迁移保文件）；`GET /sandboxes` = 0、两 worker `_runtime`/`workspaces` = 0、`DRY_RUN=1 apply.sh | kubectl diff -f -` 0 行。

- [ ] **Step 4: 写文档**

§7.27（读数表 + 复跑命令 + §4.3.1 四条硬要求各自的用例名）；N56 行；README ① 的数字改成三段式；§14.1 那条禁令加例外并指回 spec。

- [ ] **Step 5: Commit**

```bash
git add docs/deploy-clusters.md docs/open-issues.md README.md docs/c3-privilege-relocation.md
git commit -m "docs(n56): 建箱材料化授权上线记录（延迟读数 + 硬要求用例 + 复跑命令）"
```

---

## Self-Review

**Spec 覆盖**：§4.1 → Task 1/2；§4.2 → Task 3；§4.3 → Task 3/4；§4.3.1 四条 → Task 4（源 / 目标 / 半棵树）+ Task 9 Step 3（迁移保文件）；§4.4 → Task 5 + Task 6（老路 / 回退）；§4.5 → Task 7/8；§5 的 TTL 与单次消费 → Task 1（上限）+ Task 2（TTL 来源）+ Task 3（消费）；§6 → Task 9。

**类型一致性**：`materialize-tree` 三处拼写一致（词表 / grant 的 `op` / agent 的允许集合）；`GrantRefusal.reason` 在 Task 1 定义、Task 3/4 引用；`MaterializeRefusal.reason` 在 Task 3 定义、Task 4/6 引用；`AgentFileOps.materialize()` 在 Task 5/6/8 同名。

**比例**：9 个任务；代码块只出现在两处必须定死的算法（Task 1 的签名、Task 4 的拷贝纪律），其余是测试名 + 断言 + 命令。

**风险最高的三步**：Task 4 Step 3（硬化拷贝）、Task 7 Step 3（拆除等待的边界）、Task 8 Step 3（defer 之后 uid 池的 marker 语义）—— 三处都先红后绿，且 Review Focus 各占一条。
