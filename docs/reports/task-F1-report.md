# Task F1 报告：两个 file-capability broker + envd 接线（非 root worker 的 route-B）

**状态：DONE（F1 范围内的六步全绿）；另有 1 条相邻缺口 NEEDS_CONTEXT（见 §7）**

| 项 | 值 |
|---|---|
| 主仓库 | `main`，两阶段提交 `3c88872`（stage 1）+ `4f03dee`（stage 2），显式 pathspec、未 amend；基线 HEAD `05dd4b8`（期间并行 agent 又提交了 `26db7c1`，不在我的 pathspec 内） |
| fork 子模块 | **只读、一行未改**（tip 仍是 `a063daf`；`third_party/sandlock` 无改动，`lib.rs` 的「无 setuid/setfsuid/CAP_SETUID」不变量保持） |
| 用户拍板形态 | 2 个专用 broker（`e2b-slot-spawn` / `e2b-maint`）+ 一份共享校验模块（`deploy/priv/priv_common.c`）；不做 4 个 stock 副本 |
| 证据日志 | `tmp/f1/f1-*.log`（每个首行 `ENV-HEADER`、末行 `EXIT=`）：red / green-unit / build-worker / build-test / stage1 / unit-suite / unit-manifest / contract-root / contract-nonroot / e2e / worker-nonroot / lane / lane-nosa / z1-build / z1-build-cp / z1-compose / z1-routeb / z2-deploy-smoke / smoke-prod-worker{,-pref1} / flake-check |
| 测试镜像 | 本地重建 `e2b-sandlock-test:f1`（= 同一 Dockerfile.test-runner，含两个 broker）；worker 镜像 `f1-worker:stage1`（stage 1 验证）与 `e2b-local/e2b-sandlock-worker:f1`（Track Z） |

## 0. 结论（先看这三行）

1. **非 root worker 现在真的能起 route-B 槽位、也能把工作区交给池内任意 uid**：uid 65534 +
   `--cap-drop ALL --cap-add SETUID,SETGID,CHOWN,DAC_OVERRIDE` 下，两个 broker 完成
   `setgroups([])→setgid(X)→setuid(X)→execve(supervise)` 与 chown/rm/walk；槽位 exec 后
   `CapEff=0000000000000000`。端到端五条断言（①worker 日志、②槽位 argv、③两个不同 workspace 属主、
   ④跨 uid 写/删 EPERM、⑤`pwd=/home/user`）在**两种形态**（root 的 setpriv、非 root 的 broker）
   都通过，非 root 形态另在真实 compose 栈（Track Z 复跑）里复核。
2. **两处必须偏离任务书原文，都有实测依据**（见 §5）：broker 目录不能是「root 0700」（那样
   uid 65534 自己 `Permission denied`，整条路线死掉），改用 root:worker-gid `0710` / 文件 `0750`；
   非 root 形态的 `E2B_ROUTE_B_TMP_ROOT` 必须在 broker 白名单根之下（清单已设
   `/var/lib/e2b-sandboxes/.route-b`），否则槽位 policy 文档会退化成 world-readable 0444。
3. **相邻缺口（不在 F1 验收表内，但 Track Z 跑出来了）**：非 root + broker 形态下
   **worker 进程自身**对租户 `0700` 工作区的 I/O（`sbx.files.*`、`snapshot.create`、命令日志）仍被
   DAC 拒绝；route-B 槽位/命令路径不受影响。修它要动 broker 接口或工作区权限口径 ⇒
   **按任务书约束停下报 NEEDS_CONTEXT，未自行放宽白名单/0700**（§7）。

## 1. RED → GREEN（逐字证据）

### 1.1 RED（先写用例、先跑成红）

`tmp/f1/f1-red.log`（`tests/unit/test_priv_helpers.py`，模块尚不存在）：

```
ImportError while importing test module '/workspace/tests/unit/test_priv_helpers.py'.
tests/unit/test_priv_helpers.py:31: in <module>
    from envd_service import priv_helpers as ph
E   ImportError: cannot import name 'priv_helpers' from 'envd_service' (/workspace/envd_service/__init__.py)
!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
1 error in 0.25s
EXIT=2
```

### 1.2 GREEN

`tmp/f1/f1-green-unit.log`：`26 passed in 0.18s` / `EXIT=0`。逐条覆盖任务书点名的六类：

| 用例 | 精确断言（节选） |
|---|---|
| uid 不在池内 | `uid 9999 is outside the privileged helper uid pool 10000..10999`（11000/0 同形） |
| 路径越出根 | `path /etc/passwd is outside the privileged helper roots (<ws>, <shared>)` |
| `..` 逃逸 | 同上，`path <ws>/../escaped ...` |
| 符号链接逃逸 | 同上，`path <ws>/loop/secret ...`（`loop -> outside/`） |
| `argv[0]` 非 supervise 绝对路径 | `the spawned program must be the absolute path <…/sandlock-supervise> (got '/bin/sh'): e2b-slot-spawn is not a general run-as-uid-X launcher` |
| 缺 helper | 两个都缺 ⇒ 返回 `None` + `helpers_unavailable_reason()` = `E2B_PRIV_HELPERS=auto on a non-root worker, but <dir>/e2b-slot-spawn is missing: …`；**只缺一个 ⇒ fail closed**：`<dir>/e2b-maint is missing while <dir>/e2b-slot-spawn is present: a partial broker install must not be guessed at` |
| helper 无 cap | `<dir>/e2b-slot-spawn is missing the file capabilities ['CAP_SETGID', 'CAP_SETUID'] (found []): run \`setcap cap_setuid,cap_setgid+ep\` in the final image stage (\`COPY --from\` does not preserve the xattr)` |

另含：`chown --uid 0` 拒绝、`rm/chown` 不接受管理根本身、`--worker` 归还语义、
`E2B_PRIV_HELPERS` 与 `E2B_PER_SANDBOX_UID`/`E2B_ROUTE_B` 的一致性、xattr rev2 编解码、
沙箱可达目录（0710 之外）fail closed。

## 2. 镜像内 `getcap` 与 stage-1 容器实测

`tmp/f1/f1-build-worker.log`（**最终阶段** `setcap`，构建输出即含 `getcap`）：

```
#23 0.182 /var/lib/e2b-priv/e2b-slot-spawn cap_setgid,cap_setuid=ep
#23 0.182 /var/lib/e2b-priv/e2b-maint cap_chown,cap_dac_override=ep
```

`tmp/f1/f1-stage1.log`（非 root 容器：uid 65534 + 四条 BND cap，`seccomp=unconfined`）：

```
### 1. image: getcap on the two brokers
/var/lib/e2b-priv/e2b-slot-spawn cap_setgid,cap_setuid=ep
/var/lib/e2b-priv/e2b-maint cap_chown,cap_dac_override=ep
drwx--x--- 1 root nogroup 46 … /var/lib/e2b-priv
-rwxr-x--- 1 root nogroup … e2b-slot-spawn / e2b-maint
### 2a. non-root container identity
uid=65534(nobody) gid=65534(nogroup) groups=65534(nogroup)
CapEff=0000000000000000
### 2a2. a sandbox-shaped uid (pool uid 10001) cannot reach the brokers
rc=126  sh: 1: /var/lib/e2b-priv/e2b-slot-spawn: Permission denied
### 2b. e2b-slot-spawn: refusals
e2b-slot-spawn: refused: uid 9999 is outside the privileged helper uid pool 10000..10999        rc=77
e2b-slot-spawn: refused: e2b-slot-spawn starts one host identity: uid 10001 and gid 10002 must match   rc=77
e2b-slot-spawn: refused: the spawned program must be the absolute path …/sandlock-supervise (got '/bin/sh')  rc=77
e2b-slot-spawn: refusing to start: euid 10001 does not match --uid 10002   rc≠0   # ← 身份真的被 broker 改成 10001
### 2d. e2b-maint: whitelist + escapes
10002:10002 755 / 10002:10002 644            # chown --uid 10002 --recursive 生效
d 10002 10002 755 8 …/probe  f 10002 10002 644 0 …/probe/file   # walk 输出
probe removed                                  # rm 生效
e2b-maint: refused: path /etc/passwd is outside the privileged helper roots (/var/lib/e2b-sandboxes)  rc=77
e2b-maint: refused: cannot resolve path …/../escape: No such file or directory                        rc=77
e2b-maint: refused: path …/loop/passwd is outside the privileged helper roots (…)                     rc=77
e2b-maint: refused: path /var/lib/e2b-sandboxes is outside the privileged helper roots (…)             rc=77   # 管理根本身
e2b-maint: usage: --uid: uid/gid must be positive (got 0)                                             rc=2
### 3a. a broker-started slot is uid 10007 with CapEff=0 (root container reads /proc)
slot ['Uid:\t10007\t10007\t10007\t10007', 'Gid:\t10007…', 'CapEff:\t0000000000000000']   EXIT=0
### 3b. the same pool lease works for the uid-65534 worker itself
euid 65534 slot pid 6 … slot ['Uid:\t10007…', 'CapEff:\t0000000000000000']                EXIT=0
```

> 2b 的「身份真的被改成 10001」用的是 fork 自己的自检做对照（把 `--uid 10002` 传给 supervise，
> 它按 `euid != --uid` 拒绝）——比对 `/proc` 更直接，也证明没有转手 exec 别的 setuid 工具。

## 3. 端到端五条断言（非 root 形态，逐字）

`tmp/f1/f1-e2e.log`（容器：`--user 65534:65534 --cap-drop ALL --cap-add SETUID,SETGID,CHOWN,DAC_OVERRIDE`）：

```
worker euid=65534 egid=65534 CapEff=0000000000000000
   A write: rc=0 stdout=b'' stderr=b'' host-owner=0644 21000
① route-B instance ready sandbox_id=sbx_2234… instance_name=sbx_2234… uid=21000 slot=sbx_2234… channel=fd-handoff(pid 12) guest-uid=uid-0-in-userns
   route-B instance ready sandbox_id=sbx_1a92… instance_name=sbx_1a92… uid=21001 slot=sbx_1a92… channel=fd-handoff(pid 33) guest-uid=uid-0-in-userns
② uid 21000: /usr/local/lib/python3.14/site-packages/sandlock/bin/sandlock-supervise --policy /tmp/f1-e2e-…/.route-b/21000/sbx_2234…/policy.json --uid 21000 --control-fd 7 --serve --program …/program.json
   uid 21001: … --policy /tmp/f1-e2e-…/.route-b/21001/sbx_1a92…/policy.json --uid 21001 --control-fd 8 --serve --program …
③ workspace owners: 21000:0700 and 21001:0700
④ cross-uid rm: rc=1 stderr=b"rm: cannot remove 'mnt/data/a.txt': Operation not permitted\n" (file still there: True)
   cross-uid touch: rc=1 stderr=b"touch: cannot touch 'mnt/data/a.txt': Permission denied\n"
   file content unchanged: 'owned-by-a\n'
⑤ pwd: rc=0 stdout=b'/home/user\n/home/user\n' stderr=b''
   volume root: mode 1777 owner 65534
EXIT=0
```

契约测试把这个流程固化为 `tests/contract/test_nonroot_route_b.py`（2 条：端到端五条 + broker 租约），
root 形态 `tmp/f1/f1-contract-root.log` = `2 passed`，非 root 形态 `tmp/f1/f1-contract-nonroot.log` = `2 passed`。

## 4. lane 与 Track Z 复跑计数

| lane | 命令 | 计数 |
|---|---|---|
| 全量（root 形态，phase 1） | `IMAGE=e2b-sandlock-test:f1 ./deploy/scripts/test-prod-shaped.sh` | `1166 passed, 3 skipped, 0 failed`（335–340s）；旧基线 `1138 passed / 3 skipped / 0 failed`，**+28 = 新增测试**（26 unit + 2 contract），skip 数不变 |
| 非 root（phase 2，同一次 lane 内） | 同上（脚本自带） | `50 passed, 1 skipped`（62s）；旧为 48，**+2 = 新契约** |
| 无 `SYS_ADMIN` | `IMAGE=… PROD_DROP_CAPS=SYS_ADMIN UNPRIVILEGED_PHASE=0 …` | `1166 passed, 3 skipped, 0 failed`（318s），无回归 |
| 单测重跑 | 首次 lane 出现的 `test_pause_stops_exec_child_group_until_connect_resumes` 失败 | `tmp/f1/f1-flake-check.log` = `1 passed in 3.06s` ⇒ 计时型 flake（首跑时工作区磁盘水位 94.2%），非回归 |

Track Z（**非 root 形态**：出厂清单 `user: "65534:65534"` + `E2B_PRIV_HELPERS=auto`）：

| 项 | 命令 | 结果 |
|---|---|---|
| 镜像 | `REGISTRY=e2b-local PLATFORMS=linux/amd64 VERSION=f1 ./deploy/scripts/build-images.sh` + control-plane-gateway 本地构建 | `tmp/f1/f1-z1-build.log` / `f1-z1-build-cp.log` = `EXIT=0` |
| Z1 起栈 | `docker compose -p f1stack --env-file tmp/f1/stack.env -f deploy/stack/docker-compose.prod.yml up -d`（五个容器 Up，host 端口 3910；用独立 project 以免动 Z agent 留下的卷） | `tmp/f1/f1-z1-compose.log` = `EXIT=0` |
| Z1 route-B 证据 | `tmp/f1/z1-routeb-evidence.sh` | `tmp/f1/f1-z1-routeb.log`：两 worker `uid=65534 CapEff=0000000000000000 CapBnd=00000000000000c3`（= 四条 cap）、`/var/lib/e2b-priv` `drwx--x--- 0:65534`、`getcap` 两条；`PER_UID_NONROOT_WARNING count: 0`、helpers 警告 0；四个沙箱 `pwd=/home/user`、`id -u`=0；槽位进程 `uid=10000/10001/10002/10003` 与 `.route-b/<uid>/<sbx>/policy.json` 一一对应；policy/program 文档 `-r--r----- 1 65534 <uid>`（0440，owner=worker、group=槽位 uid，**没有** world-readable）；sandbox 记录 owner=`10000/10001/10002/10003 mode=700`；两 worker 日志各有 `route-B instance ready … uid=<池内 uid> … guest-uid=uid-0-in-userns` |
| Z2 `deployment_smoke.py` | `tmp/z-venv/bin/python deploy/scripts/deployment_smoke.py`（127.0.0.1:3910） | **未过**：`sb.files.write(...)` → `500: [Errno 13] Permission denied: '/var/lib/e2b-sandboxes/sbx_…/workspace'`（跨节点分布、建箱、命令、kill 后配额归零均已正常） |
| Z2 `multinode_smoke.py` | 同上 | **未跑**（同因：它也用 files.write；`f1-z2-deploy-smoke.log` 已先暴露根因，避免重复证据） |
| SDK 手工复核 | 同一 venv + 官方 SDK：`commands.run` / `files.*` / `create_snapshot` | `commands.run` **OK**（`echo hi > /home/user/a.txt` 回显 `hi`）；`files.write`(新/覆盖) `500 EACCES`、`files.read/list` `FileNotFound`、`create_snapshot` `502` — 见 §7 |
| `smoke-prod-worker.sh` | `./deploy/scripts/smoke-prod-worker.sh e2b-sandlock-test:f1` | `14 warnings, 3 errors`（全部 `sandlock_create failed`）；对**改前**镜像 `e2b-sandlock-test:latest` 复跑同样 `3 errors`（`tmp/f1/f1-smoke-prod-worker-pref1.log`）⇒ 这是 task-Z 报告登记的 **F9**（脚本假设 root+默认 cap、无 SYS_PTRACE；改它要动「沙箱内 uid 口径」），与 F1 无关，仍待用户拍板 |

## 5. 两处偏离（都有实测依据）

1. **broker 目录权限：`root:worker-gid 0710`（文件 `0750`），不是 `root 0700`。**
   0700 下 uid 65534 连目录都穿不过去，`e2b-slot-spawn` 直接 `Permission denied`
   （第一次 stage-1 lane 就是这个输出）。改为「root 所有 + worker 的 gid + 0710」后：
   worker（gid 65534）可 exec，沙箱 uid（池内 10000+，不属于该组）不可 exec
   —— 上面 stage-1 实测 `uid 10001 → rc=126 Permission denied`。自检按
   「owner=0 + mode=0710/0750 + `os.access(X_OK)`」判定，所以 0700 与 0755 都会被点名拒绝。
2. **`E2B_ROUTE_B_TMP_ROOT` 必须在白名单根之下**（清单已设 `/var/lib/e2b-sandboxes/.route-b`）。
   槽位 policy/program 文档要在非 root worker 上归到槽位 uid，只能经 `e2b-maint`；而 broker
   只碰 `<workspace_base>/`、`<shared_volume_root>/`。放在默认 `/tmp/sandlock-route-b` 时：
   要么 word-readable 0444（凭据泄漏，Z1 第一次跑就是这个警告），要么自检 fail closed。
   我选了后者：**不放宽白名单**，改为按名字拒绝并给修法，清单同时给出正确取值。
   现在文档落成 `-r--r----- 1 65534 <uid>`（0440，owner=worker 以便 W1 重启重写）。

其他实现细节（非偏离，供复核）：
* `chmod` 不进 broker（broker 刻意不带 `CAP_FOWNER`）——改成「worker 还拥有时先 chmod，再 broker chown」，
  见 `uid_pool.apply_sandbox_ownership` / `volumes.provision_sandbox_volume_mount`；
* 孤儿回收用 `chown --worker`（owner 只能是 broker 自己的 uid，永不接受 0）；槽位文档用
  `chown --worker --gid <池内 uid>`；
* `libcap2-bin` 留在 worker **运行镜像**里，只为 `getcap` 验证（任务书 Step 2 要求）；
  运行期没有任何代码路径需要它，也不需要 `SETFCAP`。若要最小化可在 `setcap` 后 purge。
* `tests/security/test_worker_nonroot.py` 的「最小构建上下文」补了 `deploy/priv/`
  （Dockerfile 现在消费它），否则该用例的 `docker build` 会因缺文件失败——这是我改 Dockerfile 的必然配套。

## 6. 威胁模型（file capabilities）

* file cap 的语义是「**任何能 exec 该文件的进程拿到该 cap**」。所以 broker 必须放在沙箱不可达处，
  且要**用 DAC 而不是只靠 Landlock** 论证：纯形态的 Landlock 覆盖 `/usr/local`、`/opt`
  （所以不放那里），`/var/lib/e2b-priv` 为 root 所有 + worker 组 + `0710`，
  沙箱 uid（池内 uid，永不等于 65534 且不在该组）既不穿目录也不能 exec（实测 `rc=126`）。
* 面收窄到最小：只授**单个专用二进制**（不是通用 `setpriv`/`chown` 副本）、uid 必须在池内
  （**永不接受 0**）、`spawn` 的 program 钉死为 `sandlock-supervise` 绝对路径、
  `maint` 的路径必须 `realpath` 落在两个白名单根下且 `rm/chown` 不接受根本身。
* 槽位仍是零 cap：uid 变更清空 permitted/effective，supervise 自身无 file caps
  （实测 `/proc/<pid>/status` = `CapEff 0000000000000000`）；worker 自身 `CapEff=0`，
  四条 cap 只在容器 **BND**（Z1 实测 `CapBnd=0xc3`）。
* 绝不加 no-new-privileges：NNP=1 时 file caps 被内核忽略（探针实测）。清单里已用注释与
  单测（`tests/unit/test_worker_manifest_permissions.py`）把这条钉住。
* 已知残余风险：沙箱若**真能** exec broker 就等于拿到 `cap_setuid` —— 这是本路线接受的风险，
  用「不可达 + 专用二进制 + 池内 uid + program 钉死」把它压到需要先突破 DAC + Landlock 的前提里。

## 7. 遗留 / NEEDS_CONTEXT（相邻缺口，未自行决定）

**非 root + broker 形态下，worker 进程自身对租户 `0700` 工作区的 I/O 被 DAC 拒绝。**

* 实测（§4 SDK 手工复核）：`sbx.files.write` `500 EACCES`（写 `<ws>/.<name>.tmp` 时），
  `files.read`/`files.list` `FileNotFoundException`，`sbx.create_snapshot()` `502`
  （`shutil.copytree` 读不动租户工作区）；`deployment_smoke.py` 也停在这里。
* 根因：`envd_service/filesystem/ops.py` 是**进程内** os 调用，`CommandLogWriter`、
  `snapshot` 的 copytree 同理；而 F1 之后工作区归租户 uid `0700`（这正是③/④成立的前提）。
  旧的非 root 形态（工作区属 worker 自己）没有这个问题，但它也没有 per-sandbox uid/route-B。
* **为什么停下**：任务书约束写明「若发现需要新的产品口径决策（例如某条路径要不要放行），
  停下报 NEEDS_CONTEXT，不要自行放宽白名单」。修它必须动接口/口径，三个候选方向：
  1. **broker 增加 `dac`/fd 原语**（例如 `e2b-maint open --path P [--create|--truncate]` 通过
     `SCM_RIGHTS` 把 fd 交给 worker，I/O 仍在 worker 里做）——面小但仍是新协议面；
  2. **files API 挂到 route-B 槽位**（fork 侧有按策略代打开/写入的机制，但需要新增
     fs 动词与 E2B 侧改造，工作量最大、语义最"正确"）；
  3. **调整工作区权限模型**（如目录 `X:65534 0770`）——最小改动，但改变「0700 per-uid」这条
     已被 §2.4/多个契约钉住的口径，且要重新论证跨租户隔离。
* 现状不会"静默坏"：`E2B_PRIV_HELPERS=auto` 是显式形态选择，`off` 可回到今天的
  in-process 行为；文档 §2.4 已写明该缺口与三个方向。
* 另两条仍开的、与 F1 无关的条目：**F9**（`smoke-prod-worker.sh` 的 root+默认 cap 形态，task-Z 已登记；
  本轮未复跑，改前/改后行为一致）、**F5/F6**（冷节点 428、provisioning EPERM 被当 401）——都不在 F1 pathspec 内。
* 上面的缺口在 **Fix round 1（裁定 c1）** 里关闭了 —— 见下一节。

---

# Fix round 1（裁定 c1：把 worker 的访问需求表达成**权限**而不是能力）

**状态：DONE（本轮四组验收全绿，新增 1 个提交 `b407e53`，未 amend）**

## R1.1 结论

* **数据面恢复**：`sbx.files.write/read/list`、`snapshot.create`、命令日志写入在非 root + broker
  形态下全部转绿（真实 compose 栈 + 官方 SDK；`deployment_smoke.py` 的
  "commands + files through gateway" 段也回来了）。
* **隔离仍然成立，并且被更直接地钉住**：真沙箱（uid Y/gid Y、`setgroups([])`）与
  `setpriv` 裸内核两种形态都证明"读/写/删 A 的 `0770` 树"被拒，同时"同一个 uid 但带上 worker
  的 gid"可以访问——后者正是机制本身。
* **新增硬护栏**：uid 池不得覆盖 worker 自己的 uid/gid，启动自检按名字 fail closed（root worker
  一样检查）。

## R1.2 权限模型与两处"顺序/取值"细节

| 项 | 值 | 理由 |
|---|---|---|
| 目录模式 | `0770`（`priv_helpers.WORKSPACE_MODE`，`uid_pool.WORKSPACE_MODE` 是同一个对象的再导出） | owner=沙箱 uid（沙箱是属主，本体不动）；group=worker → worker 以自己的身份读写 |
| 组 | **`os.getegid()`**（从不硬编码 65534） | k8s 可以 `runAsGroup`；非 root 部署的 egid 就是容器里 worker 的 gid |
| 顺序 | **先 `chmod`、后 `chown`** | chown 之后 worker 不再是属主，chmod 会 EPERM（broker 刻意不带 `CAP_FOWNER`）。root 形态看不出这个顺序，非 root 必踩。两处都按此写：`uid_pool.apply_sandbox_ownership`、`volumes.provision_sandbox_volume_mount` |
| 覆盖范围 | **整棵树的目录**，不只是沙箱根 | files API 写的是 `<workspace>/workspace/`（worker 自己 `mkdir` 出来的 0755），只把根设成 0770 会让 worker 在下一层没有 w（本轮第一次跑 Z2 就是这么红的：`.deploy-0.txt.<uuid>.tmp: Permission denied`）。文件保持各自模式；覆盖采用"临时文件 + rename"，只需要目录写权限 |
| 重复建箱 | 只在"树仍属 worker"时改模式（`st_uid == os.geteuid()`）；已交给沙箱的树跳过（避免每次 resume 打一堆 EPERM 日志） | 非属主改模式本来就会失败 |
| 卷 | 卷根保持 `1777`；**每沙箱切片** `0770 <沙箱 uid>:<worker gid>` | 与 workspace 同一模型 |
| 槽位文档 | 不变（`0440 owner=worker group=<槽位 uid>`） | c1 下 worker 就是属主，W1 重启可重写；沙箱经属组读 |

## R1.3 broker 的新分工（动词面收缩）

* `e2b-slot-spawn`：唯一保留的"以池内 uid 起进程"原语（route B）。
* `e2b-maint chown`：保留 —— worker 是树的**属组**而不是属主，自己 chown 不了
  （把树交给池内 uid、孤儿回收 `--worker`、槽位文档 `--gid`）。
  本轮给 C 侧补上 `--gid` 允许"broker 自己的 gid"这一档（`priv_gid_allowed`），
  uid 侧的门不变（池内、永不 0）；顺手修掉一个把判定写反的 bug（`!priv_gid_allowed(...)`，
  症状是合法 gid 被拒、非法 gid 通过；新单测覆盖三种取值）。
* `e2b-maint rm` / `walk`：**只做兜底** —— 沙箱自建的 `0700` 子目录、`1777` 卷根、
  升级前遗留的 root 属主目录。`priv_helpers.remove_tree` / `dir_size` 改成
  "先自己来（属组权限），EACCES 才找 broker"。

## R1.4 新增硬护栏

`priv_helpers.check_worker_identity_outside_pool(uid, gid, start, size)`：
`E2B_UID_POOL_START/SIZE` 覆盖 `os.geteuid()` 或 `os.getegid()` ⇒ 启动即 fail closed 并点名
（`the sandbox uid pool 10000..10999 contains the worker's own uid (10005): …`）。
接线两处：`create_app` 建 uid 池之前（**root worker 一样**）与 `resolve_priv_helpers`。
理由：`0770` 的组隔离前提是"沙箱 gid ≠ worker gid"，否则该沙箱就在 worker 的信任边界内。

## R1.5 验收证据（全部 `tmp/f1/*`，首行 `ENV-HEADER`、末行 `EXIT=`）

| 项 | 日志 | 实际输出 |
|---|---|---|
| lane（含 phase 2） | `f1-c1-lane-final.log` | phase 1 `1177 passed, 3 skipped, 0 failed`（316s）；phase 2 `50 passed, 1 skipped`（42s）；`EXIT=0`。旧基线（round 1）1166/3/0，**+11 = 本轮新增断言**，skip 数不变 |
| 无 SYS_ADMIN lane | `f1-c1-lane-nosa-final.log` | `1177 passed, 3 skipped, 0 failed`（321s），`EXIT=0` |
| 隔离（真沙箱 + 裸内核） | `f1-c1-isolation.log` | `3 passed`：B 的 chdir/stat/cat/**写/删**全被拒（`ls: cannot access … Permission denied`、`/bin/sh: 1: cannot create … Permission denied`、`rm: cannot remove … Permission denied`），A 自己能读；`setpriv --reuid B --regid B --clear-groups` 对 A 树 list/write/rm 全拒，`setpriv … --groups <worker gid> cat` 读到 `A-secret`（正向对照） |
| F1 五条（非 root 容器） | `f1-c1-e2e.log` | ①`route-B instance ready … uid=21000 … guest-uid=uid-0-in-userns` ②`…/sandlock-supervise --policy …/.route-b/21000/… --uid 21000 …` ③ **`21000:65534 0770` 与 `21001:65534 0770`（worker egid=65534）** ④`rm: … Operation not permitted` + `touch: … Permission denied` ⑤`pwd`/`pwd -P` = `/home/user`；`EXIT=0` |
| F1 契约（两条形态） | `f1-c1-contract-nonroot.log` | `2 passed`（含新增的 **worker 数据面**断言：`POST /files` 上传 200 + `GET /files` 回读一致 + `ListDir` 里能看到 `worker-wrote.txt` + 命令日志里有 `owned-by-a` + `POST /agent/snapshots` 201 / DELETE 204） |
| Z1 起栈 + route-B 证据 | `f1-c1-z1-compose.log` / `f1-c1-z1-routeb.log` | 五个容器 Up；两 worker `uid=65534 CapEff=0 CapBnd=0xc3`、broker 目录 `drwx--x--- 0:65534` + 两条 `getcap`；四个沙箱记录 **`owner=10000..10003 group=65534 mode=770`**；槽位进程分别跑在 `uid=10000..10003`，策略文档 `-r--r----- 1 65534 <uid>`；ready 行 `uid=<池内 uid>`；`PER_UID_NONROOT_WARNING`/helpers 警告计数 0 |
| **Z2 冒烟（非 root 形态）** | `f1-c1-z2-deploy-smoke.log` | `OK: commands + files through gateway` / `OK: migrated worker-1 -> worker-2, files kept` / `OK: network config echo + atomic update` / `OK: volume mounted remotely + sibling volume isolated` / `OK: template built -> registry push -> worker pull -> image rootfs` / `OK: MCP gateway inside sandbox + streamable HTTP through proxy` / `after kill reservations: {'worker-1': 0, 'worker-2': 0}` / **`DEPLOYMENT SMOKE OK`**，`EXIT=0` |
| **Z2 冒烟（非 root 形态）** | `f1-c1-z2-multinode-smoke.log` | `NODE DISTRIBUTION: {worker-1: 2, worker-2: 2}` / `ALL sandboxes: commands + files + health through gateway OK` / `stdin through gateway OK` / `after kill reservations: [('worker-1', 0), ('worker-2', 0)]` / **`MULTI-NODE SMOKE OK`**，`EXIT=0` |
| SDK 手工复核 | `f1-c1-z2-sdk.log` | `cwd: /home/user` / `shell-write: hi` / `files.write: WriteInfo(name='worker.txt' …)` / `files.read: from-files-api` / `files.list: ['a.txt', 'command-logs.jsonl', 'sandbox.json', 'worker.txt', 'workspace']` / `snapshot: snap_59850519c533ee03` / `command-log: ['> /bin/bash -l -c pwd', '/home/user', 'exit: 0', …]` / `killed: True`，`EXIT=0` |

对照（round 1 卡点，供复核）：同一 SDK 流程在 round-1 代码上是
`files.write → 500 [Errno 13] … .a.txt.<uuid>.tmp: Permission denied`、
`create_snapshot → 502`、`deployment_smoke` 停在 `sb.files.write`（`tmp/f1/f1-z2-deploy-smoke.log`）。

## R1.6 文档同步

`production-deployment-requirements.md` §2.4/§2.4.1：权限模型改成
`0770 owner=<sandbox uid> group=<worker gid>`，补"为什么 worker 需要访问（数据面所有者）"、
"跨沙箱隔离由什么保证（沙箱 `setgroups([])`、gid=X 不在 worker 组、other 位为 0）"、
chmod/chown 顺序、uid 池硬护栏，以及 broker 的新分工与 `CHOWN`/`DAC_OVERRIDE` 两行的口径；
`security-hardening.md` §1 的"内核权限兜底"改成 c1 表述；README 的 `E2B_PRIV_HELPERS`
行与 `deploy/stack/.env.example` 的说明同步。

## R1.7 遗留（与 c1 无关，仍开）

* **沙箱自建 `0600`/`0700` 条目**：沙箱显式把文件/子目录收紧到 0600/0700 时，worker 的属组
  访问读不到（`rm`/`walk` 兜底能删/扫，`files.read` 会 EACCESS）。这是 c1"用权限而非
  `DAC_OVERRIDE`"的固有边界；root 形态没有这个问题。当前产品路径（SDK/files API/命令）
  产出的文件是常规 umask（0644/0755），不受影响。
* **升级遗留**：round-1 建出的 `0700 owner=X` 工作区在新模型下 worker 仍进不去（broker 没有
  `CAP_FOWNER`，改不动别人的模式）。本轮未发布过，故只需在滚动升级时对既有 workspace 做一次性
  `chmod 0770`（或在 `E2B_PRIV_HELPERS=off` 下重建）；建议写进升级 runbook。
* **F9 / F5 / F6**：见上节，均不在 F1 pathspec。

---

# Fix round 2（收三条尾巴：(1) 0600 边界判定、(2) 升级 runbook、(3) F9 修好并跑绿）

**状态：DONE（新增 1 个提交 `e75b03a`（fix round 2），未 amend；本轮不改 broker 的动词面）**

## R2.1 结论

1. **（1）沙箱自建 `0600`/`0700` 条目 → 按规则"接受边界"**：仓库里**没有任何用例**依赖
   "worker 侧读回沙箱自己收紧权限的文件"；上真机跑了一遍，行为与预期一致（沙箱自己能读、
   worker 侧 `files.read` EACCES、`list`/`/metrics` 扫描/`remove` 都正常）。已在 §2.4.1 与
   README 写明，并登记 follow-up（一句）。
2. **（2）升级遗留 `0700 owner=X` 目录**：写入 runbook（`deploy/scripts/README.md`
   新增「一次性迁移」节，含从容器里取 worker gid 的完整命令），并在升级流程里加了指路。
3. **（3）F9 关闭**：`smoke-prod-worker.sh` 改成部署形态并按已拍板口径改写断言，在非 root
   形态 `2 passed, 1 skipped, EXIT=0`（skip 是 docker-daemon 依赖），root runner 单测
   `1 passed, EXIT=0`。

## R2.2 （1）判定过程与证据

**grep 全量扫（判定"有没有用例依赖"）**：`tests/sdk/python/*`、
`tests/contract/test_filesystem_rpc.py`、`tests/security/*` 里 `chmod|0o600|0600|0o700|0700|
permission` 的命中只有三类：

* `tests/security/test_template_isolation.py`：宿主侧 marker `chmod 0644`（不是沙箱自建条目的读回）；
* `tests/security/test_sandlock_isolation.py`：沙箱给自己文件 `chmod +x bin/tool`（**放宽**可执行位）；
* `tests/security/conftest.py::sandbox_owns_files_it_creates`：`printf x > tool && chmod 700 tool`
  —— **沙箱内**自检（不经过 worker 读）；
* 其余是 workspace/volume 模式断言（`0770`）与 helper 目录模式断言（`0710`）。

⇒ **没有任何用例**要求 worker 读沙箱自建的更严格权限文件。

**上机实跑**（非 root 栈，`tmp/f1/f1-c1-locked-file-probe.log`）：

```
sandbox: sbx_691acd9960e098ad | locked.txt = sandbox-created 0600, owned by the pooled uid
sandbox-side chmod: 600 10000 10000
sandbox reads its own 0600 file: OK -> 'secret'          # 沙箱是属主，读得到
worker files.read (group access): FAIL -> SandboxException: 500: [Errno 13] Permission denied:
    '/var/lib/e2b-sandboxes/sbx_691acd9960e098ad/locked.txt'
worker files.list (directory is 0770): OK -> ['command-logs.jsonl', 'locked.txt', 'sandbox.json', 'workspace']
worker metrics scan (/metrics disk.usedBytes): OK -> 1100      # 扫描不因 0600 失败
worker files.remove (dir write, no file read needed): OK -> None
worker files.list after remove: OK -> ['command-logs.jsonl', 'sandbox.json', 'workspace']
EXIT=0
```

**结论（按控制器给的规则）**：**接受这个边界**。

* §2.4.1 新增「接受的边界（fix round 2 判定）」段：写明"worker 通过组权限访问托管目录；
  沙箱自建的更严格权限条目对 platforms 侧不可读（`files.read` 会 EACCES）；删除/扫描由
  worker 属组或 `e2b-maint rm/walk` 兜底；root 形态无此限制"。
* README 的 `E2B_PRIV_HELPERS` 行同步一句。
* follow-up（一句，写在 §2.4.1 里）：若将来出现"worker 必须读沙箱自建 `0600` 文件"的需求，
  走**该沙箱自己的 route-B 槽位**读回（槽位就是 uid X、本来就能读自己的文件），槽位不可用时
  明确返回"不可读"，**不**给 broker 加通用 `read/write`。
* 为什么没选"最窄兜底"：判定规则的前置条件（"确实有用例依赖"）不成立，所以不做。

## R2.3 （2）runbook 原文

写进 `deploy/scripts/README.md`（升级流程之后新增一节，并在升级流程里加了指路）：

```bash
COMPOSE="docker compose -f deploy/stack/docker-compose.prod.yml"

# 0) 先看清 worker 的 uid/gid 与现状（只读）
$COMPOSE exec -T worker-1 id -u; $COMPOSE exec -T worker-1 id -g
$COMPOSE exec -T worker-1 sh -c 'ls -ln /var/lib/e2b-sandboxes | head'

# 1) 每个 worker 各跑一次：只碰 <workspace_base> 下的 sbx_* 顶层目录
for svc in worker-1 worker-2; do
  WGID="$($COMPOSE exec -T "$svc" id -g | tr -d '\r')"
  $COMPOSE exec -T -u 0 "$svc" sh -c "
    for d in /var/lib/e2b-sandboxes/sbx_*; do
      [ -d \"\$d\" ] || continue
      chgrp -R $WGID \"\$d\"
      find \"\$d\" -type d -exec chmod 0770 {} +
    done
    ls -ln /var/lib/e2b-sandboxes | head"
done
```

要点（同节里写明）：worker gid 必须**在容器里 `id -g` 取**（k8s 若设 `runAsGroup` 以 pod 为准），
不要硬编码 65534；`chgrp -R` 只改属组、文件模式不动；只迁移 `sbx_*` 顶层目录（卷根保持
`1777`，`_volumes`/`_snapshots`/`_migrate` 不动）；**本轮从未发布过 ⇒ 只影响开发/测试环境**。

## R2.4 （3）F9：脚本前后断言与实际输出

**脚本改动（`deploy/scripts/smoke-prod-worker.sh`）**

| | 改前 | 改后 |
|---|---|---|
| 容器身份 | `docker run`（= 镜像默认：root + Docker 默认 cap 集，**没有 SYS_PTRACE**） | `--user 65534:65534 --cap-drop ALL --cap-add SETUID,SETGID,CHOWN,DAC_OVERRIDE`（部署形态 = 非 root + broker 需要的四条 BND cap） |
| 其他 | 仅 `--security-opt seccomp=unconfined` | 另加 `-e HOME=/tmp -e TMPDIR=/tmp -e E2B_TEST_TMP_ROOT=/tmp/e2b-test-runtime`（镜像里的 `E2B_TEST_TMP_ROOT` 是 root 属主，uid 65534 写不进去；换成容器原生可写目录，属主/`0770` 语义不变） |
| 用例断言 | `test_sandbox_child_runs_unprivileged` 断言**旧共享 uid 语义**：宿主 uid 1000 → ns 内 0，且用例自己按"没有 per-sandbox uid"的方式建 executor | 按已拍板口径（§2.4.1 / 决定 #1）改写：**宿主侧 uid = 池内 uid、ns 内 = uid 0**。用例现在走生产路径（`apply_sandbox_ownership` + `RouteBConfig(mode="on", spawner=broker 或 setpriv)`），并断言三件事：①`os.getuid(),os.getgid()` == `0 0`；②`executor._route_b_active is True` 且槽位 uid == 池内 uid；③沙箱写出的文件**宿主属主 = 池内 uid**；④（保留）ns 内 uid 0 仍写不了宿主系统路径 |

**实际输出**

| 场景 | 命令 | 结果 |
|---|---|---|
| 部署形态（非 root + 四条 BND cap） | `IMAGE=e2b-sandlock-test:f1 ./deploy/scripts/smoke-prod-worker.sh "$IMAGE"` | `tmp/f1/f1-c2-smoke-final.log`：`2 passed, 1 skipped in 0.30s`，**EXIT=0**。skip 的行是 `test_https_mitm_ca_spliced_in_image_rootfs`：`cannot start buildkit container: docker: permission denied … /var/run/docker.sock`（uid 65534 用不了挂进来的 socket —— 非 root 形态的既有边界，Z 报告同样记录；该用例由 root lane / phase 2 lane 覆盖） |
| 同脚本改前 | `./deploy/scripts/smoke-prod-worker.sh e2b-sandlock-test:f1`（round 1 版本） | `tmp/f1/f1-smoke-prod-worker.log`：`14 warnings, 3 errors`，全部 `sandlock_create failed`（root+默认 cap 形态建不出箱） |
| 同脚本改前、非 root 变体 | `--user 65534:65534 --cap-drop ALL`（Z 报告 §1.3） | `1 failed, 1 passed, 1 skipped`：`test_sandbox_child_runs_unprivileged` 断言"`0 0`"失败（实测 `65534 65534`）——**F9 的原始症状** |
| 身份用例 · 非 root runner | `--user 65534:65534 --cap-drop ALL --cap-add <四条>` + 该用例 | `tmp/f1/f1-c2-identity.log`：`1 passed`，EXIT=0 |
| 身份用例 · root runner（setpriv 形态） | root lane 同款 cap 集 | `tmp/f1/f1-c2-identity-root.log`：`1 passed`，EXIT=0（两种形态同一断言都过） |

**F9 关闭**：`docs/task-backlog.md` 新增 F9 行并标 ✅ 已关闭（含上述命令与计数）。

## R2.5 回归复跑（fix round 2 后的代码）

| lane | 命令 | 计数 |
|---|---|---|
| 全量（含 phase 2） | `IMAGE=e2b-sandlock-test:f1 ./deploy/scripts/test-prod-shaped.sh` | phase 1 `1177 passed, 3 skipped, 0 failed`（287s）；phase 2 `50 passed, 1 skipped`（35s）；`EXIT=0`（`tmp/f1/f1-c2-lane.log`） |
| 无 `SYS_ADMIN` | `PROD_DROP_CAPS=SYS_ADMIN UNPRIVILEGED_PHASE=0 …` | `1177 passed, 3 skipped, 0 failed`（294s），`EXIT=0`（`tmp/f1/f1-c2-lane-nosa-final.log`） |

> 口径说明：第一次跑无 `SYS_ADMIN` lane 时我**同时开着** f1stack（compose 栈），
> `test_worker_nonroot_sandbox_network_rootfs_all_green` 报
> `rootfs sandbox: execvp '/bin/echo': No such file or directory`（docker daemon 被 compose
> 与 rootfs 导出争用导致的 flake）。停栈后单测复跑 `2 passed`（`tmp/f1/f1-c2-worker-nonroot.log`），
> 整 lane 亦全绿（上表）。两条记录都留在 `tmp/f1/` 便于复核。
