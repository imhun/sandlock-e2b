# 判据入口（acceptance）

这个目录是**判据入口**：`docs/**` 里说"怎么再量一遍"的那些 **lane / 探针 / 验收脚本**都住在这里，
文档里的引用一律指这里，不再指 `tmp/`。

## 入库政策

**文档里让人照着跑的东西必须在这里；`tmp/` 只放一次性日志与被搬走前的原件。**

脚本进这个目录是一次 `git mv` —— 文件名一字不改，只是路径从 `tmp/...` 变成
`deploy/scripts/acceptance/...`，这样"清 `tmp/` / 换台机器 `git checkout`"就不再是
复现判据的前提。往回漂会被钉子挡住：`tests/unit/test_docs_only_point_at_repo_artifacts.py`
里，活文档（`docs/**`，冻结归档 `docs/reports/**` 除外）再引用一个**已有仓库副本**的
`tmp/**.py|sh` 路径**直接判红**，错误信息会写出该改指的新路径。

## 与 `tmp/` 的关系

* 从 `tmp/` 搬进来的脚本（上一轮 67 个 + 本轮 10 个），**`tmp/` 下的同名原件已不存在**。
  本轮搬入的 10 个是：`gateA-full.sh`、`gateB-full.sh`、`gateB-pure-rootfs.sh`、
  `n27-t7-lane.sh`、`phase2.sh`、`probe_state_base_visibility.py`、
  `probe-pure-restore-synthroot.sh`、`probe-pure-synth-root-plaindir.py`、
  `probe-pure-synth-root.sh`、`probe-pure-workload-census.py`。
* **回滚副本在 git 历史里**：`git log --follow -- deploy/scripts/acceptance/<名字>` 能找到搬迁提交，
  `git show <搬迁提交>^:<旧 tmp 路径>` 能取回逐字节原件。
* 一处例外，是个**删除**而不是搬迁：`tmp/k0s/checkpoint_acceptance.py` 是仓库版
  `deploy/scripts/checkpoint_acceptance.py` 的**旧副本**（437 行 vs 632 行）——
  两份不一致会把人带沟里，所以删掉。它当时**没有被 git 跟踪**（整个 `tmp/` 在 `.gitignore` 里），
  因此**不在** git 历史里；删除前留了一份字节副本在
  `.superpowers/sdd/artifact-promotion-round2-removed-tmp-k0s-checkpoint_acceptance.py`
  （sha256 `494e464e3f93aa604e5bb5b610444adccc6241ad8b77b85b8f164354c47f43af`）。

## 索引

"怎么跑"一列照抄脚本自己的头部注释（`--help` / 头部都没有的写 `—`），所以它可能落后于脚本实际参数 ——
以脚本本身为准。少数上一轮搬入的脚本头部还留着搬迁前的 `tmp/...` 拼写（那轮没有"引用也必须改"的纪律，
本轮的钉子只管 `docs/**` 与 `tests/`），照抄时未改写。

| 文件 | 服务于哪个问题（取自脚本头部） | 怎么跑（取自脚本头部 / `--help`） |
| --- | --- | --- |
| `c2-p0-probe.sh` | C2 P0 探针的 runner：把 `probe_c2_ownership_p0.py` 送进集群、在那份 RWX claim 上跑一次、收日志、删 Job（Job 清单 `deploy/k8s-k0s/c2-p0-probe.yaml`）。 | `deploy/scripts/acceptance/c2-p0-probe.sh --print-plan\|--render-job\|--root DIR\|--apply`（`--help`） |
| `capacity_check.py` | Capacity + per-sandbox memory check. | —（头部无 Usage 行；见脚本 `--help`） |
| `cleanup_scratch.py` | One-shot scratch cleanup: delete regenerable test scratch under tmp/, keep | —（头部无 Usage 行；见脚本 `--help`） |
| `cluster_keepalive_probe.py` | N37 probes: is the cut an *idle* one, and does output hold the stream open? | —（头部无 Usage 行；见脚本 `--help`） |
| `cluster_run.py` | N37 cluster run: one command writing N files, on the deployed fleet. | —（头部无 Usage 行；见脚本 `--help`） |
| `cpu_activity_acceptance.py` | Cluster acceptance for E9.1 blind spot 2: a CPU-only sandbox is not idle. | —（头部无 Usage 行；见脚本 `--help`） |
| `create_latency_probe.py` | 建箱延迟（API 边界，纯标准库）：本机跑量"平台 + 网络"，灌进控制面 pod 跑量"只有平台"；预热那一行就是解析/解包基础镜像的代价（N54 用它验落盘 digest 缓存）。 | `python deploy/scripts/acceptance/create_latency_probe.py --base http://<入口>:3000 --key "$E2B_API_KEY" --n 10`（in-cluster 见脚本头部） |
| `dir_chain_cost_probe.py` | Task 5：一次建箱要为一棵树走几次"从 `/` 逐段打开"，走法（老）与链缓存（新）各花多少 ms —— 对着一个真沙箱在生产路径上的树，跑在 c3-agent pod 的 `maint` 容器里（NAS 与 `E2B_WORKSPACE_BASE` 都在那儿）。只读：全是 `open`/`fstat`/`close`。 | `env -u http_proxy -u https_proxy -u all_proxy tmp/venv/bin/python deploy/scripts/acceptance/dir_chain_cost_probe.py --n 20`（要 `KUBECONFIG` + `E2B_*`；建箱/杀箱走公开 API） |
| `worker_provision_cost.py` | 建箱里 worker 那一跳单独多贵（幂等重放，灌进控制面 pod 跑）：先真建一个沙箱拿到授权，再把同一份 payload 直打 worker 的 agent 口计时 —— 用来切分"控制面 vs worker"，再配合 `E2B_CREATE_TRACE=1` 拆 worker 内部。 | `kubectl -n sandlock exec -i <control-plane-pod> -c control-plane -- python3 - "$E2B_API_KEY" "$E2B_INTERNAL_API_KEY" < deploy/scripts/acceptance/worker_provision_cost.py` |
| `f11_direct_exec_probe.py` | Control experiment: pure-shape exec over the direct executor, low fd table. | —（头部无 Usage 行；见脚本 `--help`） |
| `f11_fdcount_probe.py` | Does the standalone probe's stdout loss depend on the client's fd layout? | —（头部无 Usage 行；见脚本 `--help`） |
| `f11_fup3_probe.py` | F11 E2B rerun probe: gateway+command boxed quota on the F11 tip wheel. | —（头部无 Usage 行；见脚本 `--help`） |
| `f11_snapshot_restart_acceptance.py` | F11 acceptance: a rolling restart must not kill a live peer's copy. | `TREE_FILES=8000 .venv/bin/python deploy/scripts/acceptance/f11_snapshot_restart_acceptance.py`（**先读头部**：本部署当前 `重启 134 s > 拷贝超时 120 s`，窗口是空集 ⇒ 只会 INCONCLUSIVE，见 `docs/open-issues.md` N46） |
| `f23_multi_probe.py` | FUP-23 paired send/recv probe: several marker execs on one pure instance. | —（头部无 Usage 行；见脚本 `--help`） |
| `final-verify.sh` | Sequential final verification of the tier-removal tree. One phase at a time, | —（头部无 Usage 行；见脚本头部） |
| `gateA-full.sh` | The whole suite in the production-shaped root worker with `E2B_BASE_IMAGE=python-mcp:3.14`. | `sh deploy/scripts/acceptance/gateA-full.sh <log>` |
| `gateB-full.sh` | The whole suite in the production-shaped root worker with `E2B_BASE_IMAGE=`（pure 形态）. | `sh deploy/scripts/acceptance/gateB-full.sh <log>` |
| `gateB-pure-rootfs.sh` | gate B's twin: the *pure* shape (`E2B_BASE_IMAGE=""`) in both root states. | `Usage: gateB-pure-rootfs.sh <state 0\|1> <log> [pytest target...]` |
| `guard_probe.py` | Probe the shape guard: does it actually fire, and is it non-vacuous? | —（头部无 Usage 行；见脚本 `--help`） |
| `guard_probe2.py` | Post-fix probe: the vacuity hole is now an assertion, not a silent green. | —（头部无 Usage 行；见脚本 `--help`） |
| `ledger-arena-test.py` | Is the 72 MiB/thread cost glibc's per-thread malloc arena? | —（头部无 Usage 行；见脚本 `--help`） |
| `ledger-thread-cost.py` | What does each kind of thing cost in the sandlock ledger, in a plain box? | —（头部无 Usage 行；见脚本 `--help`） |
| `local_first_capacity_account.py` | Task 1 Step 1 ②：共享根逐命名空间的字节/文件数、节点本地盘 headroom、快照记录与日期（每节点容量账的输入）。 | `kubectl -n sandlock exec -i <agent-pod> -c maint -- python3 - < deploy/scripts/acceptance/local_first_capacity_account.py` |
| `local_first_pagecache_acceptance.py` | Task 1 Step 1 ③：用公开 API 驱动建箱/快照/从快照建箱，同时在 worker 与 `maint` 容器内采样页缓存峰值（n≥10）。恢复那一行会顶着 `maint` 的 512 MiB 限跑，**实测 OOM 过一次**：create 失败与重试都记进输出（`create_attempts` / `failures`），快照在 `finally` 里删。 | `tmp/venv/bin/python deploy/scripts/acceptance/local_first_pagecache_acceptance.py --repeat 10 --snapshot-repeat 10 --sampler-seconds 18 --restore-seconds 10 --max-failures 2 --create-retries 6`（要 `KUBECONFIG` + `E2B_*`） |
| `local_first_pagecache_probe.py` | 采样**本容器** cgroup 的 `memory.stat:file` / `memory.current`（要跑在被测容器里，两边一共挂同一个 hostPath）。 | `kubectl -n sandlock exec -i e2b-worker-0 -c worker -- python3 - --seconds 45 --label worker-snapshot < deploy/scripts/acceptance/local_first_pagecache_probe.py` |
| `local_first_sequential_write_probe.py` | Task 1 Step 1 ①：在**沙箱里**写 `--seq-mb` MiB（默认 900，1024 MiB 配额下的最大值），附一次 1024 MiB `dd` 的配额拒绝读数。 | `tmp/venv/bin/python deploy/scripts/acceptance/local_first_sequential_write_probe.py --seq-mb 900 --repeat 10`（要 `E2B_*`） |
| `local_first_snapshot_verify.py` | Task 1 Step 2：只读复核合一后的 `<export>/_snapshots/<id>`（记录/载荷四分类、旧载荷根是否还在、迁移 journal 有无同名冲突）。 | `kubectl -n sandlock exec -i <cp-pod> -c control-plane -- python3 - < deploy/scripts/acceptance/local_first_snapshot_verify.py` |
| `local_first_storage_probe.py` | Task 1 Step 1 ⓪/①：同一份负载在共享 NAS 与节点本地盘上的小文件 + 顺序写（三种块大小、n≥10、可选逐窗口速率）。 | `kubectl -n sandlock exec -i e2b-worker-0 -- python3 - --root nas:/var/lib/e2b-sandboxes/workspaces --root local:/var/lib/e2b-images --seq-mb 64,256,1024 --repeat 10 --chunk-log < deploy/scripts/acceptance/local_first_storage_probe.py` |
| `mcp-3way.py` | Split an MCP call into forward path / server work / return path. | —（头部无 Usage 行；见脚本 `--help`） |
| `mcp-512-size.py` | In a 512MB box: wait for the gateway, then find the largest stdio server. | —（头部无 Usage 行；见脚本 `--help`） |
| `mem512-limit.py` | Live: is the 512MB box a hard limit, and what does an MCP gateway cost? | —（头部无 Usage 行；见脚本 `--help`） |
| `mmap-probe.py` | Does a MAP_SHARED store past EOF grow the file on *this* filesystem? | `Run as: mmap-probe.py <dir> <label>` |
| `n27-t7-lane.sh` | N27 Task 7: run a command inside the prod-shaped lane container. | `Usage: sh deploy/scripts/acceptance/n27-t7-lane.sh python3 -u deploy/scripts/acceptance/probe_state_base_visibility.py lane --shape identity --layout n27` |
| `n35-lane.sh` | Run one command in the prod-shaped lane (the same container shape as | `sh deploy/scripts/acceptance/n35-lane.sh python3 -u deploy/scripts/acceptance/probe_n35_exec_gate.py chroot all` |
| `n39-pool-pidns-probe2.py` | N39 follow-up, take 2: raw `/proc` view inside a *pooled* sandbox. | —（头部无 Usage 行；见脚本 `--help`） |
| `n46-copy-lease-probe.py` | N46 on the fleet: does an *unnamed* async copy hold the fleet-wide lease? | `TREE_FILES=2000 .venv/bin/python deploy/scripts/acceptance/n46-copy-lease-probe.py`（要 `E2B_API_KEY` + `KUBECONFIG`；树别超过 worker 调用 120 s 超时） |
| `n42-egress-probe.py` | N42 acceptance on the live fleet: does `allow_internet_access=True` reach out? | —（头部无 Usage 行；见脚本 `--help`） |
| `netns-node-compare.py` | Per-node comparison: SDK command RTT, MCP /mcp RTT, wildcard DNS. | —（头部无 Usage 行；见脚本 `--help`） |
| `node-mmap-storage.sh` | Same kernel, four storages: does a mapped store past EOF extend the file? | —（头部无 Usage 行；见脚本头部） |
| `overlay-probe.sh` | 在不改云网络的前提下，验证封装型 overlay 能不能跨这两个节点工作。 | —（头部无 Usage 行；见脚本头部） |
| `phase1-probe2.sh` | —（头部没写） | —（头部无 Usage 行；见脚本头部） |
| `phase2.sh` | test-prod-shaped.sh's phase 2 (the unprivileged worker), runnable on its own. | `sh deploy/scripts/acceptance/phase2.sh <log>` |
| `pidns-cost-probe.py` | Probe: what does `pid_ns` cost on the syscalls it traps? | `Run in the prod-shaped lane, both shapes, same image:` |
| `pidns-shape-probe.py` | Probe: which shape did a route-B sandbox actually get? | 头部示例（旧拼写）`./deploy/scripts/test-prod-shaped.sh tmp/pidns-shape-probe.py -k pidns_shape_probe` |
| `probe-pure-realroot.py` | pure 能不能走真根？把 pivot_root 的两种用法实测一遍。 | —（头部无 Usage 行；见脚本头部） |
| `probe-pure-restore-synthroot.sh` | pure + 合成根下的 pause/resume：restore stub 从"根内"变成"根外"。 | `用法：probe-pure-restore-synthroot.sh <log>` |
| `probe-pure-synth-root-plaindir.py` | 合成根在生产 cap 形状下能不能 pivot？以及 /dev、/proc 该怎么装。 | —（头部无 Usage 行；见脚本头部；由 `probe-pure-synth-root.sh <part>` 驱动） |
| `probe-pure-synth-root.sh` | 合成根探针的 runner：**生产 cap 形状**（worker 那五个 cap，无 SYS_ADMIN）+ 出厂 seccomp 档。 | 头部：`用法：probe-pure-synth-root.sh <part> <log>`（part ∈ b2/tmpfs/symlinks/proc/dev/devdiff） |
| `probe-pure-workload-census.py` | 同一组命令在三种形态下的 (rc, stdout, stderr)，逐字节 diff。 | —（头部无 Usage 行；见脚本头部） |
| `probe_127_errno.py` | Why does a *missing* path answer EACCES(13) instead of ENOENT(2)? | 头部示例（旧拼写）`sh tmp/k0s/phase1-probe2.sh /workspace/tmp/k0s/probe_127_errno.py` |
| `probe_brief_stat_live.py` | A/B of the fix on the live cluster (N25): os.stat vs entry_size. | —（头部无 Usage 行；见脚本 `--help`） |
| `probe_broker_authorization_surface.py` | **RUNNABLE=no（2026-09-29 起）**：C1 broker 的**授权面**，确认它对"哪一个沙箱"没有任何概念（`docs/c3-privilege-relocation.md` §14.3）。目标 DaemonSet 已随 C3 Task 7 退役，脚本本身也留着作 N47 的现场证据、不可再跑；想复现要在 C1 的镜像/清单上跑（git 历史里的 `deploy/k8s/priv-broker.yaml`）。 | 三 phase 分别在不同容器：`--phase setup / cleanup` 在 **broker pod**（root，PVC 可写），`--phase attempt` 在 **worker pod**（65534，需 `PYTHONPATH=/app`）；判据 `C1-AUTHZ-VERDICT=no-sandbox-authorization` |
| `probe_c3_userns_map_handoff.py` | C3 §14.2.7：**非父进程、跨 pod、跨 pid namespace** 能不能替 worker 的已 unshare 子进程写 uid/gid 映射（决定 worker 能否既保进程树又零特权）。 | 三个 role：`--role forker` 跑在 `e2b-worker-0`（65534）、`--role agent --agent-keep-caps-uid 65534` 跑在 `deploy/k8s-k0s/c3map-probe-agent.yaml` 那个 `hostPID` pod 里、`--role matrix` 做写者/目标身份的对照矩阵（单 pod 内）；判据 `C3-MAPHANDOFF-VERDICT=agent-can-map` |
| `probe_c2_ownership_p0.py` | C2 P0：uid 0 对别人 `0600`/`0700` 的语义（读/遍历/删/改）+ 粘滞位与组位（`docs/c2-ownership-frontload.md` §7）。 | 由 `c2-p0-probe.sh` 在 Job 里跑（root + NFS）；本机彩排 `sh deploy/scripts/acceptance/c2-p0-probe.sh --root DIR`；退出码 0/1/2/3/4/5 |
| `probe_c3_a5_silent_rmtree.py` | C3 §13.7/A5：CP 的"配对删除"在非属主身份下**静默失败**（调用真实函数 `_remove_local_tree_confirming`，4 个 cell）。 | 在**挂了共享 PVC 的 root 容器**里：`env PYTHONPATH=/app python3 probe_c3_a5_silent_rmtree.py --root <那 8 条 RW 子挂载之一下的目录>`（CP 根挂载只读，见 `docs/c3-privilege-relocation.md` §13.8）；退出码 0/2 |
| `probe_ceiling_completeness.py` | Can anything still grow the tree once the ceiling is exactly zero? | —（头部无 Usage 行；见脚本 `--help`） |
| `probe_copy_range_zero.py` | `copy_file_range` reported 300 MiB moved while the file stayed 4096 bytes. | —（头部无 Usage 行；见脚本 `--help`） |
| `probe_dir_cost.py` | What a directory really costs on this NAS: st_size vs st_blocks, by entry count. | —（头部无 Usage 行；见脚本 `--help`） |
| `probe_dir_ledger.py` | N25/L2c acceptance: the incremental ledger must equal a measurement made | —（头部无 Usage 行；见脚本 `--help`） |
| `probe_dir_stsize.py` | N31 fix 2 acceptance: the platform number charges what a tree allocates. | —（头部无 Usage 行；见脚本 `--help`） |
| `probe_disk_metric_agreement.py` | N30 T6 acceptance: the platform's `diskUsed` equals a measurement taken | —（头部无 Usage 行；见脚本 `--help`） |
| `probe_etxtbsy_shape.py` | Is the write-then-exec ETXTBSY window real in the *chroot* (production) shape? | `Usage (inside the lane container):`, `python3 tmp/k0s/probe_etxtbsy_shape.py [chroot\|pure] [iterations]`（旧拼写） |
| `probe_exec_limit.py` | N25/C acceptance: the per-exec ceiling is "what is left", refreshed per exec. | —（头部无 Usage 行；见脚本 `--help`） |
| `probe_kernel_copy.py` | Do kernel-side copies respect RLIMIT_FSIZE? | —（头部无 Usage 行；见脚本 `--help`） |
| `probe_landlock_execveat.py` | Can a Landlock-confined process exec a binary it only holds *by fd*? | `Usage (inside the lane container):`, `python3 /src/tmp/k0s/probe_landlock_execveat.py`（旧拼写） |
| `probe_mmap_growth.py` | Exactly where does a MAP_SHARED store stop growing the file? | —（头部无 Usage 行；见脚本 `--help`） |
| `probe_n28_acceptance.py` | N28 acceptance on the live fleet: A (pause gates), B (one writer), C, D. | —（头部无 Usage 行；见脚本头部） |
| `probe_n29_sync.py` | N29: what a client actually sees when a snapshot outlives the entry proxy. | `Run with E2B_API_URL/E2B_SANDBOX_URL/E2B_API_KEY set, from the deploy host.` |
| `probe_n35_exec_gate.py` | N35 (directed follow-up): who refuses to exec a file the sandbox owns? | `Usage (inside the lane container)`，`python3 -u tmp/k0s/probe_n35_exec_gate.py [chroot\|pure] [grant] [leg\|all\|list]`（旧拼写） |
| `probe_n35_ns.py` | Which namespaces and caps does a chroot-shaped sandbox actually have today? | `Usage (inside the lane container):`, `python3 -u tmp/k0s/probe_n35_ns.py [chroot\|pure]`（旧拼写） |
| `probe_n35_realmount.py` | Can a real root (mount ns + pivot_root) be built in the deployed shape? | `Usage (inside the lane container, root)`，`PROD_DROP_CAPS=SYS_ADMIN sh tmp/k0s/n35-lane.sh python3 -u tmp/k0s/probe_n35_realmount.py`（旧拼写） |
| `probe_openat2_eagain.py` | Does *this* kernel answer EAGAIN for openat2(RESOLVE_IN_ROOT) through a `..`? | `Usage: python3 probe_openat2_eagain.py [iterations] [racer_on\|racer_off]` |
| `probe_push_and_tighten.py` | Cluster evidence for N25's push reporting and the live tightening. | —（头部无 Usage 行；见脚本 `--help`） |
| `probe_restore_state.py` | Why does a *restored* sandbox not tick? (2026-09-25, cluster probe) | `Run with KUBECONFIG + E2B_API_URL + E2B_API_KEY set; it prints the sandbox id` |
| `probe_state_base_visibility.py` | N27 Task 7 acceptance probe: is the platform's state base visible from a sandbox? | 头部 Modes：`cluster`（默认，走 API）/ `lane`（lane 容器内）/ `in-sandbox`（checker 本体）；退出码 0/1/2 |
| `probe_write_paths.py` | Which ways of making a file bigger does RLIMIT_FSIZE actually stop? | —（头部无 Usage 行；见脚本 `--help`） |
| `rb_token_probe.py` | What actually leaks when the channel token travels in supervise's argv. | —（头部无 Usage 行；见脚本 `--help`） |
| `red-routeb-stderr-drain.py` | RED/GREEN check for the slot-stderr drain (N35 side quest). | `sh tmp/k0s/n35-lane.sh python3 -u tmp/k0s/red-routeb-stderr-drain.py`（旧拼写） |
| `relay_probe.py` | N37 end-to-end: a 60 s idle cut in front of the local stack. | `Run it twice: once with the keepalive removed (RED), once as shipped (GREEN).` |
| `routeb_cap_probe.py` | 机制级探针：在给定 capset 的容器里，逐项问 route B / 进程内后端「还活着吗」。 | —（头部无 Usage 行；见脚本 `--help`） |
| `run-f31.sh` | Sequential re-verification (f31) on the final bytes, one container per phase. | —（头部无 Usage 行；见脚本头部） |
| `run.sh` | The local aarch64 lane's kernel: qemu-system-aarch64 under TCG (this host is | —（头部无 Usage 行；见脚本头部） |
| `sdkflake-cacheprobe.py` | Measure the image-cache maintenance walk that sits inside the first-command path. | `Usage: python tmp/sdkflake-cacheprobe.py [cache-dir]`（旧拼写） |
| `sec-run-probe.sh` | Reusable runner: the prod-shaped capability set the sandlock create path needs. | —（头部无 Usage 行；见脚本头部） |
| `slot_cap_probe.py` | 量一件事：route-B 槽位（=路径中介进程）与被 confine 的子进程各自持有哪些 cap。 | —（头部无 Usage 行；见脚本 `--help`） |
| `snapshot_tar_roundtrip_probe.py` | Task 2 上线验收的"形状"那一腿：新快照卷上是 `fs.tar`、建箱回到树根、链接仍是链接；`--modes` 报恢复后的模式（`data` filter 的夹取读数）；`--fifo --expect-fifo-refusal` 报"捕获成功 / 恢复具名拒绝"。 | `tmp/venv/bin/python deploy/scripts/acceptance/snapshot_tar_roundtrip_probe.py --modes`（要 `E2B_API_URL`/`E2B_SANDBOX_URL`/`E2B_API_KEY`；`--keep` 留下快照） |
| `sync-seccomp-installer.py` | Re-embed `deploy/seccomp/sandlock-worker.json` into the ConfigMap installer. | `python3 tmp/k0s/sync-seccomp-installer.py`（dry run）/ `… --write`（旧拼写） |
| `t1-ownership-probe.py` | O1/T1 re-measurement on the live fleet: who owns a file the sandbox writes? | —（头部无 Usage 行；见脚本 `--help`） |
| `task8_fup3_probe.py` | M4 Task 8 (FUP-E3) Step 1 probe: record the exact rejection shape. | —（头部无 Usage 行；见脚本 `--help`） |
| `unprivileged_userns_probe.py` | Can a route-B slot (euid == the sandbox host uid) map 0 -> X in its own | —（头部无 Usage 行；见脚本 `--help`） |
| `verify-arena-live.py` | Post-deploy: the MCP stdio server's ceiling with the pinned arena. | —（头部无 Usage 行；见脚本 `--help`） |
| `vol_fs_mount_probe.py` | Mechanism probe: why does the chroot volume view fail without SYS_ADMIN? | —（头部无 Usage 行；见脚本 `--help`） |
| `x86-run-py.sh` | Run one python file in the production-shaped root worker, x86_64. | 头部：`Run one python file in the production-shaped root worker, x86_64.` |
| `x86-security-one.sh` | One security case (or a -k filter) in the production-shaped root worker, x86_64. | `Usage: x86-security-one.sh <E2B_BASE_IMAGE> <log> <pytest args...>` |
