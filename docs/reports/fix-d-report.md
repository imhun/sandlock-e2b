# fix-d 报告：C1 剩余「文档 / 旧说法类」记账项

- 分支：`feat/c1-fix-d`（从 `ff04e76` 起）
- 写集：`docs/**`、`README.md`、`deploy/k8s-k0s/README.md`、四个测试文件的 docstring
- 纪律：不碰集群；只改文档与 docstring；不新增 skip/xfail、不动断言
- commit：`5b6eb9545b1898161bd105aae809203e255a063c` — "docs(c1): close the documentation/old-wording bookkeeping items"

## 判据（改前 / 改后，实际输出）

```
/Users/polus/project/ai/sandlock-e2b/tmp/testenv/bin/python -m pytest \
  tests/unit/test_uid_pool.py tests/unit/test_quota_maintenance.py \
  tests/contract/test_orphan_tree_gc.py tests/unit/test_state_base_call_sites.py \
  -q -p no:cacheprovider
```

- 改前：`133 passed, 4 skipped`（4 skip 全是 `chown requires root` / `root worker shape`，本机非 root 的正常跳过；无 failed）
- 改后：`133 passed, 4 skipped`（**失败集合两次都为空**，行数一致）
- 注意：**路径修正** —— 任务里写的 `tests/unit/test_orphan_tree_gc.py` 实际在
  `tests/contract/test_orphan_tree_gc.py`（`fd`/`rg` 确认 `tests/unit/` 下没有该文件）。用的是真实路径。
- 额外回归（文档类，绿）：`tests/unit/test_docs_only_point_at_repo_artifacts.py`（9 passed）、
  `tests/unit/test_seccomp_selfcheck.py` + `test_prod_shaped_lane_netns_passthrough.py` +
  `test_worker_manifest_permissions.py`（66 passed）。改动全是 markdown 散文与 docstring，不动行为。

## 逐条改动

### 1. `auto` 模式遇残留 socket 会硬拒启动 —— 写进文档

先读代码确认语义（不是猜）：

- `envd_service/priv_helpers.py::resolve_priv_helpers`：`transport == "socket" or (transport == "auto"
  and socket_path.exists())` ⇒ 走 `_resolve_socket_shape`。即 **`auto` 按 socket 文件是否存在**选形态，
  与 daemon 是否活着无关。
- `_resolve_socket_shape` → `_require_broker_agreement` 做 `hello` 握手；daemon 已死 / uid 池或根白名单不一致
  ⇒ 抛 `PrivHelperError` ⇒ worker 启动拒服，**没有回落 exec 的路径**（注释明确写 "There is deliberately no
  fallback."）。

落点（一处，不重复抄）：

- `README.md` 的 `E2B_PRIV_HELPER_TRANSPORT` 行：补 `auto` 同样 fail closed + 按文件存在选形态 + 撤
  DaemonSet 必须清 socket 文件；
- `docs/k8s-deployment.md` §2「镜像与升级」：在既有「先 broker 再 worker / socket fail closed」那段后
  补一句 ⚠「反过来撤 broker DaemonSet 时 socket 文件不会自己消失」（节点 hostPath，两节点各一个
  `/run/e2b-broker/broker.sock`；撤前/后清掉，或把 worker 切回 `exec`）。

### 2. `docs/security-hardening.md` §8.3 过期

- 标题加 `—— ✅ 已收口（C1，2026-09-27）`；
- 原文（"`Dockerfile.envd` 无 `USER`——worker 容器以 root 跑"）**原样保留**，前面加一句"这段是原始风险
  记录，保留原文"；
- 之后加一条 ✅ 说明：镜像现在是 `USER 65534:65534`，C1 后 k8s 基线 worker 不写 `runAsUser`（特权在
  `e2b-priv-broker` DaemonSet），实测指向 `docs/deploy-clusters.md` §7.1。

### 3. 四处 docstring 的「SnapshotRegistry 的 base 就是 workspace base」

真实口径（已在 `gateway_common/paths.py` 注释与 `docs/k8s-deployment.md` §24 写对）：

- **控制面**：`control_plane/app.py:497/525` `platform_root = settings.shared_workspace_root`（export 根）
  ⇒ `SnapshotRegistry(platform_root)` ⇒ `<export>/_snapshots/snap_<hex>`；
- **worker**：payload 硬编码在 `<workspace_base>/_snapshots/<id>`（`envd_service/agent.py:2581/3616/3683`）。

四处都只改 docstring（不动断言/行为）：

1. `tests/unit/test_uid_pool.py:43`（`_snapshot_store`）
2. `tests/unit/test_uid_pool.py:311`（`test_reconcile_spares_the_store_the_copy_and_the_infrastructure_namespace`）
3. `tests/unit/test_quota_maintenance.py:246`（`test_scan_project_dirs_separates_the_store_from_a_prefixed_tree`）
4. `tests/contract/test_orphan_tree_gc.py:957`（`test_snapshot_store_directory_is_spared_by_its_shape`）

改法统一：把"store 的 base *就是* workspace base"改成"这是**共享 export 根**（`<export>/_snapshots`），
本测试把同一个根同时喂给两边，就是"两者重合"的那种形状（也是 OBS-9 之前的老形状）"，因此 store 仍会
落在与 `sbx_*` 同级的扫描面里 —— 原 docstring 要表达的那个事实（前缀不能当分隔符）不变。

**`tests/unit/test_state_base_call_sites.py`：没有这类陈旧句子。** `grep -in "snapshot" 该文件` 无命中；
它出现 "workspace base" 的三处（`:107` / `:165` / `:188`）讲的都是 `RuntimeRegistry` / 命令日志 /
`_recorded_projids`，且**都仍然正确**（`Path(record.workspace_dir).parent` 就是树根=workspace base）。
所以该文件未改（任务把它列进了 grep 范围，但句不在那里）。

### 4. `docs/task-backlog.md:122`（N14）的「四个 cap」

- 原文保留，仅在 `只带 worker 实际拥有的四个 cap（无 SYS_ADMIN）` 后补：
  `；C1 之后 k8s 基线只剩 \`SETUID\`/\`SETGID\` 两条，见 \`docs/deploy-clusters.md\` §7.1`。
  （依据：`docs/deploy-clusters.md` §7.1 实测 worker `capabilities.add:["SETUID","SETGID"]`，无
  `runAsUser`；broker 的另一侧在 DaemonSet 上。）

### 5. 容器里单跑测试必须带 seccomp 档 —— 写进文档

- 落点：`docs/production-deployment-requirements.md` §2.5「门禁容器的两种形态」末尾（写集允许的
  `docs/**`；`deploy/scripts/test-prod-shaped.sh` 与 `deploy/seccomp/README.md` **不在**写集，故未动）。
- 内容：裸 `docker run --rm … pytest …`（不带 `--security-opt seccomp=…`）跑 `tests/unit/test_priv_helpers.py`
  会**假红** `test_create_app_refuses_a_pool_that_contains_the_worker_identity` —— 机制已核对：
  `envd_service/app.py:251` 的 `check_seccomp_filter(settings)` 在 uid 池身份守卫**之前**执行，无过滤器时先抛
  seccomp 缺失错误（`envd_service/config.py:733`）。带上 `--security-opt seccomp=deploy/seccomp/sandlock-worker.json`
  即绿；给了正确命令，并注 `E2B_REQUIRE_SECCOMP_FILTER=0` 只是测试形状、不是线上口径。

### 6. 直接 exec 形态的退出码变化（一句）

- 落点：`docs/production-deployment-requirements.md` §2.4.1 能力表之后。
- 核对 `deploy/priv/maint.c:1728-1735`：`main` 里 `signal(SIGPIPE, SIG_IGN)`（socket 形态需要：拒答从
  父进程写出，信号会打死整节点 broker）。写清：**直接 exec** 的 verb 被断管时，从被 SIGPIPE 打死（141）
  变成写失败 `EPIPE` → `priv_fail`（退出码 **77**，`priv_common.c:37` `exit(PRIV_EXIT_REFUSED)`）；
  仓库内无消费者读 141，外部脚本认 `77 = 拒绝`。

## 7.（调查）pure 形态的 `_pure_rootfs` —— **无缺口**

**结论：commit 形状下没有缺口**；`<workspaces>/_pure_rootfs` 不进 C1 属主迁移白名单是对的（没东西要迁），
§24 已补一句说明。（因此**不需要改 `deploy/**`**。）

依据（只读查证，全部 file:line）：

1. **谁创建**：worker 自己。`envd_service/executors/sandlock.py:517` `_materialize_synthetic_rootfs` →
   `:539` `_mkdir_traversable(root)`，root = `<pure_rootfs_dir>/<sandbox_id>`；`_mkdir_traversable`
   （`:324`）会**逐级创建缺失父目录**（`<workspaces>/_pure_rootfs` 因此被建），并显式 `chmod 0755`；
   `:548` 的 `_heal_traversable(base, base)` 把该层也 heal 回 `0755`。不是 fork/slot、也不是 broker。
2. **属主 / 模式**：worker 的身份（k8s 基线 = uid 65534）+ `0755`；沙箱的池 uid 只**穿行**不拥有。
3. **为什么非 root worker 也建得动**：树根 `<workspaces>` 由 **broker** 的 `workspace-root-init` 保证
   对 65534 可写 —— `deploy/k8s/priv-broker.yaml:325-345` 的 writability gate（属主 65534 或 `1777`，
  否则 FATAL）。所以 worker 在 `<workspaces>` 下 `mkdir` 一个 `_pure_rootfs` 无需 root，无需 broker。
4. **拆箱**：worker 自己 `shutil.rmtree(pure_rootfs_dir/<id>, ignore_errors=True)`
   （`envd_service/agent.py:1407`）；骨架各层都归 worker 所有，root 不参与。即便将来需要在其下 `rm`/`chown`，
   `e2b-maint` 的四根白名单本就含 `E2B_WORKSPACE_BASE`（`deploy/priv/priv_common.c:211` /
   `envd_service/priv_helpers.py:685`），覆盖得到。
5. **为什么迁移盘上没有它**：`_pure_rootfs` 只对**无基镜像的 pure 沙箱**落盘 ——
   `_synthetic_rootfs`（`sandlock.py:2131`）对有基镜像的沙箱返回 None，而 `executors/factory.py:202-205`
   只在该沙箱是 pure 时把 `pure_rootfs_dir` 交给 executor；线上基线所有沙箱都带 `E2B_BASE_IMAGE`。
   加上 `E2B_PURE_ROOTFS` 的旧默认是 `off`（合成根只有 N16/synth 之后才启用；见 `envd_service/config.py`
   注释里"开关只可在 teardown 落地后打开"），root worker 时代没在集群上留下 `_pure_rootfs`。
   因此 C1 属主迁移（白名单只放 `workspaces/_migrate` 与 `workspaces/_snapshots`，
   `deploy/scripts/migrate-state-owner.sh:8-13`）**没有 root 属主要迁**，加进去反而会把一条
   "worker 自有的目录"混进"root 平台态"的清单。

**理论残留（不是当前缺口，记录备查）**：若**某次 root worker 部署确实开过 `E2B_PURE_ROOTFS=synth` 且跑过
无基镜像沙箱**，盘上会留下由 root 建的 `<workspaces>/_pure_rootfs`（`root:0755`），C1 后 65534 的 worker
在它下面建新 `<id>` 会 EACCES。此时**需要**对 `<workspaces>/_pure_rootfs` 补一次
`chown 65534:65534`（或让 `workspace-root-init` 把这一层也纳入 writability gate）。本次实测的这台集群
**没有**这个前提（基线沙箱都带基镜像 + 旧默认 off），所以判为无缺口、无需改 `deploy/**`。

## 未碰 / 顺带发现（供参考，未改）

- `deploy/k8s-k0s/README.md` 在写集里，但本次没有必须改的点，未改。
- `docs/deploy-clusters.md` §7.2「形态开关」表下有一句"`E2B_PURE_ROOTFS` **未设** ⇒ 走代码默认 `off`"，
  与当前代码默认 `synth`（`envd_service/config.py:211-213`，2026-09-27 裁定）**已不符**。不在本
  任务清单内，未改；建议另开一条记账。
