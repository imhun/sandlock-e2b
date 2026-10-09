# 控制面 uid 迁移：本地车道差分读数（Task 5 Step 1–3）

> **本文件是历史读数**（`docs/reports/**` 是冻结归档的一部分）：它是 2026-10-09 在
> 本机 Docker VM（linux/amd64，cgroup v2）上跑出来的原文，**不是**给读者照抄的
> 操作手册。要再量一遍，跑
> `deploy/scripts/acceptance/cp_uid_lane_diff.py`（一条命令，见 §1）；那条命令会
> 自己把 gitignored 的 `tmp/task5-cpuid/*.yml` override 生成出来。
>
> 计划：`docs/superpowers/plans/2026-10-09-control-plane-uid-relocation.md` Task 5
> （Step 1–3 = 本地车道差分 → 存读数；Step 4–6 是生产步骤，被人为 gated，本文件不碰）。
> 背景：`docs/security-audit/control-plane-uid-2026-10-09.md`（控制面 65533 ⇄ worker 65534）。

**结论先行**：控制面以 **65533** 起来、`CapEff` 恒为 0，两个 uid 不再相等；它自己
`chown`/创建/读写的一切（`_builds`/`_images`/`_secrets`/`_snapshots`/`_templates`/
`_migrate`/`_volumes`/卷根）都正常，worker 与 agent 面 A 仍是 65534。
`create / 首条命令 / GET /logs / pause+resume / snapshot+restore / delete` 六项判据
在本车道全绿，`multinode_smoke.py` 全绿。两条判据在本车道**证明不了**，
按实测如实记在 §6/§7：

* **模板构建的 `_oci` tar 那条路**：基表给控制面写死了占位 registry
  （`registry.example.com/e2b`）且**整个栈没有 buildkitd**，`Template.build` 走 push 分支、
  在 `dial unix /run/buildkit/buildkitd.sock: no such file or directory` 处断掉。
  §5 用一个**最小 override**（起 buildkit + 清空 registry + 共享 `_oci` + worker 侧
  `E2B_IMAGE_OCI_DIR`）把这条路真的跑通了（tar 65533 写、worker 65534 解包、`.link`
  65534 写在自己的缓存里）；
* **卷挂载读写**：本车道（两个卷）里沙箱里的 `mnt/data` 是指向平台根绝对路径的断链；
  §6 有沙箱内 `os.stat` ENOENT 的原文，以及**对照车道**（`docker-compose.prod.yml`，
  控制面与 worker 共用一个卷）同一判据**绿**的原文 ⇒ 这是本车道的形状事实，
  不是 Task 3 的回归，也不是 k8s 的结论。

外部判据 `deployment_smoke.py` 在本车道**不可能全绿**（它自带卷挂载与模板两段，
用法头写的是 `docker-compose.prod.yml`）；原文与逐段原因见 §7。

## 1. 起车道：命令、镜像、override

```bash
# 起（并跑全部判据）——这一条命令就是判据入口：
tmp/venv/bin/python deploy/scripts/acceptance/cp_uid_lane_diff.py --phase lane  --up
tmp/venv/bin/python deploy/scripts/acceptance/cp_uid_lane_diff.py --phase shared --up
# 收尾：
tmp/venv/bin/python deploy/scripts/acceptance/cp_uid_lane_diff.py --phase shared --down
```

* 工程名 `-p cpuid`（宿主端口 **3110**，不碰用户自己的 `compose` 栈在 3100）；
* 基表：`deploy/compose/docker-compose.multinode.yml`（3 worker + 控制面 + redis +
  agent 两面 + root 一次性 `image-cache-init`；worker 65534、控制面 65533）；
* `--phase lane` = 基表 + `tmp/task5-cpuid/override-lane.yml`（容量与镜像，见下）；
* `--phase shared` = 再加 `tmp/task5-cpuid/override-shared-namespaces.yml`
  （最小形状：buildkit + 无 registry + 共享 `_oci` + 共享卷存储，见 §5/§6）。
* 镜像（本树构建，**没有**推任何仓库）：

```bash
$ REGISTRY=local/e2b VERSION=cpuid PLATFORMS=linux/amd64 sh deploy/scripts/build-images.sh
  ...
done:
  worker:        local/e2b/e2b-sandlock-worker:cpuid
  agent:         local/e2b/e2b-sandlock-agent:cpuid
  quota-agent:   local/e2b/e2b-sandlock-quota-agent:cpuid
  (control-plane-gateway is built by build-and-push.sh)

$ docker compose -p cpuid -f deploy/compose/docker-compose.multinode.yml \
      -f tmp/task5-cpuid/override-lane.yml build control-plane
  #19 naming to docker.io/library/cpuid-control-plane 0.0.0s done
  #20 DONE 0.4s
 Image cpuid-control-plane Built
```

`build-images.sh` 用仓库自己的脚本（buildx 多架构 builder、单平台 `--load`）建 worker /
agent / quota-agent 三个镜像；控制面由基表的 `build:` 建（`cpuid-control-plane`）。两个
override 的**逐字内容**（`cp_uid_lane_diff.py` 里 `_LANE_OVERRIDE` / `_SHARED_OVERRIDE`
的同一份文本）：

```yaml
# tmp/task5-cpuid/override-lane.yml（capacity + images）
services:
  control-plane:
    ports: !override ["3110:3000"]
  worker-1:
    build: !reset null
    image: local/e2b/e2b-sandlock-worker:cpuid
    environment: &worker-capacity
      E2B_NODE_DISK_MB: "400000"      # worker 报的 diskUsedMB 是整个 VM 的用量（131.5G）
      E2B_NODE_PROCESSES: "1024"      # 基表 256 = 一个箱，multinode_smoke 要 4 个箱
      E2B_EXECUTOR: sandlock
      E2B_PER_SANDBOX_UID: "true"
  worker-2: {build: !reset null, image: local/e2b/e2b-sandlock-worker:cpuid, environment: *worker-capacity}
  worker-3: {build: !reset null, image: local/e2b/e2b-sandlock-worker:cpuid, environment: *worker-capacity}
  image-cache-init: {image: local/e2b/e2b-sandlock-worker:cpuid}
  c3-agent:        {image: local/e2b/e2b-sandlock-agent:cpuid}
  c3-agent-maint:  {image: local/e2b/e2b-sandlock-agent:cpuid}
```

```yaml
# tmp/task5-cpuid/override-shared-namespaces.yml（最小形状，逐字）
services:
  buildkit:
    image: moby/buildkit:rootless
    security_opt: [seccomp=unconfined, label=disable]
    volumes:
      - buildkit-data:/home/user/.local/share/buildkit
      - ../stack/buildkitd.toml:/home/user/.config/buildkit/buildkitd.toml:ro
    restart: "no"
  image-cache-init:
    volumes:
      - oci-shared:/cache/control/_images/_oci
  c3-agent-maint:
    volumes:
      - control-data:/var/lib/e2b-sandboxes/_volumes
  control-plane:
    group_add: ["1000"]                # rootless buildkit socket 是 660 1000:1000（k8s 用 fsGroup）
    environment:
      E2B_IMAGE_REGISTRY: ""
      E2B_IMAGE_REGISTRY_USERNAME: ""
      E2B_IMAGE_REGISTRY_PASSWORD: ""
      E2B_BUILDKIT_ADDR: unix:///run/buildkit/buildkitd.sock
      E2B_SHARED_VOLUME_ROOT: /var/lib/e2b-sandboxes
      E2B_TREES_SHARED: "1"            # 树在**一个**共享卷上（基表三个 worker 共用 worker-data）
    volumes:
      - buildkit-data:/run/buildkit:ro
      - oci-shared:/var/lib/e2b-sandboxes/_images/_oci
  worker-1:
    environment:
      E2B_SHARED_VOLUME_ROOT: /var/lib/e2b-sandboxes
      E2B_IMAGE_OCI_DIR: /var/lib/e2b-sandboxes/_shared-images
    volumes:
      - oci-shared:/var/lib/e2b-sandboxes/_shared-images/_oci
      - control-data:/var/lib/e2b-sandboxes/_volumes
  worker-2: <同 worker-1>
  worker-3: <同 worker-1>
volumes:
  buildkit-data:
  oci-shared:
```

（上面为了行宽把 `worker-2/3` 折了一行；生成器里的文本是展开的三段，见脚本。）

## 2. 形态读数：控制面 65533（CapEff 0），worker / 面 A 65534

```bash
$ docker compose -p cpuid -f deploy/compose/docker-compose.multinode.yml -f tmp/task5-cpuid/override-lane.yml ps
NAME                     IMAGE                                 COMMAND                  SERVICE          STATUS                    PORTS
cpuid-c3-agent-1         local/e2b/e2b-sandlock-agent:cpuid    "python3 -m c3_agent"    c3-agent         Up
cpuid-c3-agent-maint-1   local/e2b/e2b-sandlock-agent:cpuid    "python3 -m c3_agent"    c3-agent-maint   Up
cpuid-control-plane-1    cpuid-control-plane                   "python -m control_p…"   control-plane    Up                        0.0.0.0:3110->3000/tcp
cpuid-redis-1            redis:8-alpine                        "docker-entrypoint.s…"   redis            Up (healthy)
cpuid-worker-1-1         local/e2b/e2b-sandlock-worker:cpuid   "python -m envd_serv…"   worker-1         Up
cpuid-worker-2-1         local/e2b/e2b-sandlock-worker:cpuid   "python -m envd_serv…"   worker-2         Up
cpuid-worker-3-1         local/e2b/e2b-sandlock-worker:cpuid   "python -m envd_serv…"   worker-3         Up

$ docker inspect cpuid-control-plane-1 --format '{{.Config.User}}'   # 65533:65533
$ docker exec cpuid-control-plane-1 id
uid=65533 gid=65533 groups=65533
$ docker exec cpuid-control-plane-1 sh -c 'grep -E "^(CapEff|CapBnd)" /proc/self/status'
CapEff:	0000000000000000
CapBnd:	00000000a80425fb

$ docker exec cpuid-worker-1-1 id
uid=65534(nobody) gid=65534(nogroup) groups=65534(nogroup)
$ docker exec cpuid-c3-agent-1 id
uid=65534(nobody) gid=65534(nogroup) groups=65534(nogroup)
$ docker exec cpuid-c3-agent-maint-1 id
uid=0(root) gid=0(root) groups=0(root)

# 控制面自己写的文件仍归它自己（根挂载点 65533，`_volumes` 非递归交棒 65533）：
$ docker exec cpuid-control-plane-1 sh -c 'touch /var/lib/e2b-sandboxes/.cpuid-owner-probe &&
      stat -c "%n %a %u:%g" /var/lib/e2b-sandboxes/.cpuid-owner-probe && rm -f …'
/var/lib/e2b-sandboxes/.cpuid-owner-probe 644 65533:65533
```

`image-cache-init`（root 一次性）的**按卷分流**交接原文 —— 这是 Task 3 把
`_images/_oci` 的回收收归控制面之后，控制面得以换 uid 的那条前提：

```bash
$ docker logs cpuid-image-cache-init-1
image-cache-init: /cache/control/_images is owned by uid 65533
image-cache-init: /cache/worker/_images is owned by uid 65534
image-cache-init: /cache/control/_builds is owned by uid 65533
image-cache-init: /cache/control/_secrets is owned by uid 65533
image-cache-init: /cache/control/_snapshots is owned by uid 65533
image-cache-init: /cache/control/_templates is owned by uid 65533
image-cache-init: /cache/control/_migrate is owned by uid 65533
image-cache-init: /cache/worker/_snapshots is owned by uid 65534
image-cache-init: /cache/worker/_migrate is owned by uid 65534
image-cache-init: /cache/control is writable by uid 65533 (owner=65533 mode=755)
image-cache-init: /cache/worker is writable by uid 65534 (owner=65534 mode=755)
image-cache-init: created /cache/control/_volumes (the platform's volume store)
image-cache-init: /cache/control/_volumes -> uid 65533 mode 755 (a non-recursive hand-over; the volume data directories below it keep their pooled uids)
image-cache-init: /cache/control/_volumes/_meta does not exist -- nothing to hand over (the control plane creates it under its own uid when it first needs it)
image-cache-init: /cache/control/_volumes is owned by uid 65533 (the volume data directories below it are left alone)
```

## 3. 判据读数（`--phase lane`，车道原样）

入口命令与退出码（run-lane.log 全文在 `tmp/task5-cpuid/run-lane.log`，可重跑）：

```bash
$ tmp/venv/bin/python deploy/scripts/acceptance/cp_uid_lane_diff.py --phase lane --up
RUNNER_EXIT=1
```

```text
===== 1. create + 2. first command (through the gateway) =====
  created sbx_f34b4dd9fc0fccf4
$ curl -sS http://127.0.0.1:3110/internal/routes/sbx_f34b4dd9fc0fccf4 -H X-Internal-Key: internal-key
  {"nodeID":"worker-1","address":"http://worker-1:49983"}
  [exit 0]
  route: {"nodeID":"worker-1","address":"http://worker-1:49983"}
  stdout='cpuid-first-command-ok\n' stderr='' exit=0

===== 3. GET /sandboxes/{id}/logs (+ kill the create criterion's sandbox) =====
  HTTP 200
  [
  {
    "timestamp": "2026-10-09T08:43:42.474Z",
    "line": "> /bin/bash -l -c echo cpuid-first-command-ok"
  },
  {
    "timestamp": "2026-10-09T08:43:42.496Z",
    "line": "cpuid-first-command-ok"
  },
  {
    "timestamp": "2026-10-09T08:43:42.497Z",
    "line": "exit: 0"
  }
]
  kill(sbx_f34b4dd9fc0fccf4) -> True

===== 4. pause + resume =====
  created sbx_f4895893547c3f1b
  pause() -> True
  get_info().state after pause -> paused
  state after connect -> running
  stdout='cpuid-after-resume\n' exit=0

===== 5. snapshot + restore =====
  snapshot_id=snap_32fb7f13777a95d0 names=['cpuid-lane']
  restored file -> 'cpuid-snapshot-data'
  delete_snapshot -> True

===== 6. template build (buildkit -> _oci tar -> worker unpack) [lane] =====
  build log: debug: Build started
  build log: info: Requesting build for template: cpuid-lane-template
  build log: info: Template created with ID: tpl_c896c8027cfca63a, Build ID: bld_44a96366ae8d85fb
  build log: info: All file uploads completed
  build log: info: Starting building...
  build log: info: Waiting for logs...
  build log: info: building template (buildkit: unix:///run/buildkit/buildkitd.sock)
  build log: info: error: listing workers for Build: failed to list workers: Unavailable: connection error: desc = "transport: Error while dialing: dial unix /run/buildkit/buildkitd.sock: connect: no such file or directory"
  build log: info: buildkit build exited with code 1
  build log: debug: Build finished
  Template.build raised BuildException: buildkit build exited with code 1

===== 7. volume mount read-write =====
  sandbox read raised CommandExitException: Command exited with code 1 and error:
cat: mnt/data/host.txt: No such file or directory

  [sandbox] $ readlink mnt/data
  [sandbox] Command exited with code 1 and error:
  [sandbox] $ python3 workspace/volume_probe.py
  [sandbox] /home/user/mnt/data ERR [Errno 2] No such file or directory: '/home/user/mnt/data'
  [sandbox] /var/lib/e2b-sandboxes ERR [Errno 2] No such file or directory: '/var/lib/e2b-sandboxes'
  [sandbox] /var/lib/e2b-sandboxes/_volumes ERR [Errno 2] No such file or directory: '/var/lib/e2b-sandboxes/_volumes'
$ docker exec cpuid-control-plane-1 sh -c ls -ld /var/lib/e2b-sandboxes/_volumes /var/lib/e2b-sandboxes/_volumes/*/
  drwxr-xr-x 1 65533 65533   50 Oct  9 08:43 /var/lib/e2b-sandboxes/_volumes
  drwxr-xr-x 1 65533 65533 1114 Oct  9 08:43 /var/lib/e2b-sandboxes/_volumes/_meta/
  drwxrwxrwt 1 65533 65533   16 Oct  9 08:43 /var/lib/e2b-sandboxes/_volumes/vol_20e1ea99985be776/
  [exit 0]
$ docker exec cpuid-worker-1-1 sh -c ls -ld /var/lib/e2b-sandboxes/_volumes 2>&1 || true
  drwxr-xr-x 1 root root 0 Oct  9 08:23 /var/lib/e2b-sandboxes/_volumes
  [exit 0]

===== 8. delete =====
  created sbx_f6dcb52c2b0fddad
  kill() -> True
  GET /sandboxes/sbx_f6dcb52c2b0fddad -> HTTP 404
  after-delete reservations: {'worker-1': 0, 'worker-2': 0, 'worker-3': 0}
```

两个 smoke 的原文（同一次运行的最后一段）：

```text
--- multinode_smoke.py (lane) ---
$ tmp/venv/bin/python deploy/scripts/multinode_smoke.py
  NODE DISTRIBUTION: {'http://worker-1:49983': 2, 'http://worker-2:49983': 1, 'http://worker-3:49983': 1}
  ALL sandboxes: commands + files + health through gateway OK
  stdin through gateway OK
  after kill reservations: [('worker-1', 0), ('worker-2', 0), ('worker-3', 0)]
  MULTI-NODE SMOKE OK
  [exit 0]

--- deployment_smoke.py (lane) ---
$ tmp/venv/bin/python deploy/scripts/deployment_smoke.py
  NODE DISTRIBUTION: {'http://worker-3:49983', 'http://worker-1:49983', 'http://worker-2:49983'}
  OK: commands + files through gateway
  after kill reservations: {'worker-1': 0, 'worker-2': 0, 'worker-3': 0}
  Traceback (most recent call last):
    File ".../deployment_smoke.py", line 269, in <module>
      sys.exit(main())
               ^^^^^^
    File ".../deployment_smoke.py", line 92, in main
      assert target.files.read("workspace/deploy-0.txt") == "data-0"
  e2b.exceptions.FileNotFoundException: Path workspace/deploy-0.txt not found
  [exit 1]
```

汇总（`--phase lane`，19 条）：**16 PASS / 3 FAIL**

```text
  [PASS] control-plane runs as the manifest's uid  (uid=65533 gid=65533 groups=65533)
  [PASS] worker-1/2/3 runs as the manifest's uid  (uid=65534(nobody) …)
  [PASS] c3-agent runs as the manifest's uid  (uid=65534(nobody) …)
  [PASS] control plane CapEff is 0  (CapEff:	0000000000000000 CapBnd:	00000000a80425fb)
  [PASS] hand-over gave every control-data target 65533  (6 targets: _builds,_images,_migrate,_secrets,_snapshots,_templates)
  [PASS] hand-over gave every worker-data target 65534  (3 targets: _images,_migrate,_snapshots)
  [PASS] the volume store went to the control plane's uid  (non-recursive hand-over of _volumes)
  [PASS] both writable roots are their volume's uid  (control 65533 / worker 65534)
  [PASS] create lands a sandbox that answers its first command
  [PASS] GET /logs returns the command log for the run above
  [PASS] pause freezes and resume restores the sandbox  (paused=paused resumed=running)
  [PASS] a snapshot restores into a new sandbox with its files
  [FAIL] template build (this phase)  (BuildException: buildkit build exited with code 1)
  [FAIL] a mounted volume reads and writes in both directions  (cat: mnt/data/host.txt: No such file or directory)
  [PASS] delete removes the record and releases the node's reservation  (kill=True http=404 reserved={'worker-1': 0, 'worker-2': 0, 'worker-3': 0})
  [PASS] multinode_smoke.py  (exit 0)
  [FAIL] deployment_smoke.py  (exit 1)
  16/19 criteria PASS
```

## 4. 判据读数（`--phase shared`，最小形状）

```bash
$ tmp/venv/bin/python deploy/scripts/acceptance/cp_uid_lane_diff.py --phase shared --up
SHARED_EXIT=1
```

这一段只贴与 lane 相位**不同**的判据（形态那 10 条与 §2/§3 逐字同形，只多一条
`groups=65533,1000` —— buildkit socket 的组位）：

```text
===== 6. template build (buildkit -> _oci tar -> worker unpack) [shared] =====
  template_id=tpl_280c6f46917ac203 image=None
$ docker exec cpuid-control-plane-1 sh -c ls -la /var/lib/e2b-sandboxes/_images/_oci/ && stat -c "%n %F %a %u:%g" /var/lib/e2b-sandboxes/_images/_oci/*
  total 222720
  drwxr-xr-x 1 65533 65533      380 Oct  9 08:44 .
  drwxr-xr-x 1 65533 65533        8 Oct  9 08:12 ..
  -rw-rw-rw- 1 65533 65533 45613056 Oct  9 08:36 e2b-local_tpl_18da93d06d4747c4.oci.tar
  -rw-rw-rw- 1 65533 65533 45613056 Oct  9 08:44 e2b-local_tpl_280c6f46917ac203.oci.tar
  -rw-rw-rw- 1 65533 65533 45613056 Oct  9 08:29 e2b-local_tpl_60afc7d213ce4aa1.oci.tar
  -rw-rw-rw- 1 65533 65533 45613056 Oct  9 08:23 e2b-local_tpl_a79f0a091860a4bb.oci.tar
  -rw-rw-rw- 1 65533 65533 45613056 Oct  9 08:33 e2b-local_tpl_c4edb1ae981f767a.oci.tar
  /var/lib/e2b-sandboxes/_images/_oci/e2b-local_tpl_18da93d06d4747c4.oci.tar regular file 666 65533:65533
  /var/lib/e2b-sandboxes/_images/_oci/e2b-local_tpl_280c6f46917ac203.oci.tar regular file 666 65533:65533
  [exit 0]
  stdout='cpuid-template-marker\n' exit=0
$ curl -sS http://127.0.0.1:3110/internal/routes/sbx_1aaee04efb2b6ad0 -H X-Internal-Key: internal-key
  {"nodeID":"worker-1","address":"http://worker-1:49983"}
  route: {"nodeID":"worker-1","address":"http://worker-1:49983"}
$ docker exec cpuid-worker-1-1 sh -c ls -la … | grep -E '(e2b-local_tpl_280c6f46917ac203)'
  drwxr-xr-x 1 nobody nogroup   12 Oct  9 08:44 e2b-local_tpl_280c6f46917ac203-08f96fc5f21fa3022feefdda2b2f79af598447f4
  -rw-r--r-- 1 nobody nogroup    0 Oct  9 08:44 e2b-local_tpl_280c6f46917ac203.lock
  # worker-2 / worker-3 的输出逐字同形（同一个 worker-data 卷；命令只换了容器名）
$ docker exec cpuid-worker-1-1 sh -c stat -c "%n %F %a %u:%g" /var/lib/e2b-sandboxes/_images/_oci/e2b-local_tpl_280c6f46917ac203.link
  /var/lib/e2b-sandboxes/_images/_oci/e2b-local_tpl_280c6f46917ac203.link regular file 644 65534:65534
  [exit 0]

===== 9. the two shipped smokes =====
--- multinode_smoke.py (shared) ---   [exit 0]
--- deployment_smoke.py (shared) ---
  NODE DISTRIBUTION: {'http://worker-3:49983', 'http://worker-2:49983', 'http://worker-1:49983'}
  OK: commands + files through gateway
  OK: migrated worker-1 -> worker-2, files kept
  OK: network config echo + atomic update
  after kill reservations: {'worker-1': 0, 'worker-2': 0, 'worker-3': 0}
  Traceback (most recent call last):
    File ".../deployment_smoke.py", line 145, in main
      assert mounted.commands.run("cat mnt/data/own.txt").stdout == "volume-data"
  e2b.sandbox.commands.command_handle.CommandExitException: Command exited with code 1 and error:
  cat: mnt/data/own.txt: No such file or directory
  [exit 1]
```

汇总（`--phase shared`，19 条）：**17 PASS / 2 FAIL**（模板那条由 §5 的最小形状转绿；
卷挂载与 `deployment_smoke` 仍受 §6 的形状限制）。

```text
  …（形态 10 条 + create/logs/pause/snapshot/delete 全 PASS，同 §3）…
  [PASS] template build -> _oci tar -> worker unpack -> image rootfs  ({"nodeID":"worker-1", …})
  [FAIL] a mounted volume reads and writes in both directions  (cat: mnt/data/host.txt: No such file or directory)
  [PASS] multinode_smoke.py  (exit 0)
  [FAIL] deployment_smoke.py  (exit 1)
  17/19 criteria PASS
```

## 5. `_oci` 模板那条路：这条车道走哪条，以及最小形状里跑通了什么

从文件读出来的**分支事实**（不是猜）：

* `control_plane/api/templates.py::_run_build` 的判据是
  `registry = settings.image_registry.rstrip("/")`：**注册表非空** ⇒
  `type=image,name=<registry>/<tpl>,push=true`（**不写任何 tar**）；**空** ⇒
  `type=oci,dest=<image_oci_dir or image_cache_dir>/_oci/<slug>.oci.tar`；
* `deploy/compose/docker-compose.multinode.yml:339` 给控制面写死
  `E2B_IMAGE_REGISTRY: registry.example.com/e2b` ⇒ 这条车道走 **push 分支**；
* 全树与 k8s 车队（`deploy/k8s/control-plane.yaml`、`worker.yaml`）都**不设**
  `E2B_IMAGE_REGISTRY` ⇒ 车队走 **tar 分支**，并且 `E2B_IMAGE_OCI_DIR` 指的是
  **共享卷**上的 `_images`、worker 的 rootfs 缓存是节点本地
  （`deploy/k8s/worker.yaml:284-304`）；
* 这条车道还**没有 buildkitd**（基表与 `docker-compose.prod.yml` 都不起它；`stack/` 才带
  那一层）。所以 `--phase lane` 里 `Template.build` 是在
  `dial unix /run/buildkit/buildkitd.sock: no such file or directory` 处断的 ——
  连"推给谁"都还没轮到。

⇒ **基表这条车道根本量不到"buildkit → `_oci` → worker 解包"**。§1 的最小形状把三件事
补上（起 rootless buildkit + `E2B_IMAGE_REGISTRY=""` + 控制面写 tar 的车队路径
`<platform_root>/_images/_oci` 挂到共享卷、worker 用 `E2B_IMAGE_OCI_DIR` 指到同一个卷的
另一个路径），于是 §4 的读数成立：**tar 45.6 MB、65533:65533 写在共享 `_oci`**，
worker（65534）**读**它、解包进自己的 `_images`，并把 `.link` 边车
（644、**65534:65534**）写在自己的缓存里 —— 写共享 tar 目录会 EACCES，这正是
Task 2/3 的 `_images` 只读契约在起作用（第一次尝试把共享卷直接挂到 worker 的
`_images/_oci` 时就撞上了这条：原文见下）。

```text
# 反例原文（把共享卷挂在 worker 自己的 `_images/_oci` 上，缺 E2B_IMAGE_OCI_DIR 那半边）
  criterion raised SandboxException: Code.INTERNAL: shared image cache directory /var/lib/e2b-sandboxes/_images/_oci is not writable by uid 65534: [Errno 13] Permission denied: '/var/lib/e2b-sandboxes/_images/_oci/.e2b-local_tpl_a79f0a091860a4bb.link.tmp-1-d7b829ab' (the sidecar e2b-local_tpl_a79f0a091860a4bb.link cannot be written next to the OCI layout tar; fix the ownership once with `chown -R 65534:65534 /var/lib/e2b-sandboxes/_images/_oci` as root)
```

**这是 k8s 形状事实**（车队走 tar 分支、`_oci` 与 worker 缓存分属两处），所以：
车队上"模板构建 → worker 解包"要在 **gated 的生产步骤**复核（本车道的最小形状证明的是
代码路径与 uid 契约，不是 k8s 的挂载形状）。

## 6. 卷挂载读写：本车道证明不了，对照车道可以

本车道（三个 worker 共用 `worker-data`，控制面的 `control-data` 上放着 `_volumes`）
里，`mnt/data` 是**指向平台根绝对路径**的符号链接
（`/var/lib/e2b-sandboxes/_volumes/vol_…`），而沙箱自己的文件系统视图里
**没有 `/var/lib/e2b-sandboxes`**（§3 的 `os.stat` 原文：三条 ENOENT）。把控制面的
`_volumes` 挂进 worker 与面 B、把 `E2B_SHARED_VOLUME_ROOT` 补到 worker 之后，
`cat mnt/data/host.txt` **仍然** ENOENT（§4 的原文）。

对照：`docker-compose.prod.yml`（控制面与 worker **共用一个** `sandbox-shared` 卷）上
同一条判据**绿**：

```bash
# 起：docker compose -p cpuidprod -f deploy/compose/docker-compose.prod.yml \
#        -f tmp/task5-cpuid/prod-lane.yml up -d     （WORKER_IMAGE/AGENT_IMAGE/CONTROL_PLANE_IMAGE=local/e2b/*:cpuid）
$ tmp/venv/bin/python tmp/task5-cpuid/volume-view-probe.py 3120     # prod 车道
[3120] create 201
[3120] $ ls -la mnt
[3120]   RAISED CommandExitException: Command exited with code 1 and error:
ls: cannot read symbolic link 'mnt/data': No such file or directory
[3120] $ cat mnt/data/host.txt
[3120]   from-the-host            # ← 通过路径读**成功**
[3120] $ cat /var/lib/e2b-sandboxes/_volumes/vol_fb1332423fd3b195/host.txt
[3120]   RAISED … Permission denied

$ tmp/venv/bin/python tmp/task5-cpuid/volume-view-probe.py 3110     # multinode 车道（shared 相位）
[3110] $ cat mnt/data/host.txt
[3110]   RAISED CommandExitException: Command exited with code 1 and error:
cat: mnt/data/host.txt: No such file or directory
```

**⇒ 这是车道的形状事实**（同一份镜像、同一份 SDK 探针、同一个宿主；差别在两个卷的
切法），**不是** Task 3 的回归：这条判据的路上没有控制面的 uid（对照车道跑的还是旧姿态的
控制面 65534，见 `docker-compose.prod.yml` 的注释）。本轮**没有**去改车道文件；
留作问题（§8 Q3）。

## 7. 两个 smoke：`multinode_smoke.py` 全绿；`deployment_smoke.py` 在本车道不可能全绿

* `multinode_smoke.py`：**两个相位都 exit 0**（原文见 §3/§4）。
* `deployment_smoke.py`：用法头写的就是
  `docker compose -f deploy/compose/docker-compose.prod.yml up -d --no-build`。在本车道的
  两段失败都**不是** uid 相关：
  1. `--phase lane` 第 2 段（迁移保留文件）：基表的控制面没设 `E2B_TREES_SHARED`、
     也没设 `E2B_SHARED_WORKSPACE_ROOT` ⇒ `settings.trees_shared = False` ⇒ 迁移走
     "树在节点本地"那条路：**export → import → 释放源树**；而这条车道三个 worker 共用
     **同一个** `worker-data`（源与目标是同一个目录），那条释放把刚 import 的树一起删了。
     实测：迁移后两个 worker 上都不再有 `sbx_<id>`，且随后 `exec` 报
     `init announced child pid 3, which is not a live process of this sandbox's pid namespace`。
     最小形状里补上 `E2B_TREES_SHARED: "1"`（= 这个车道**实际**的存储形态：树在一个共享卷上）
     之后这一段转绿：`OK: migrated worker-1 -> worker-2, files kept`（§4）。
  2. 第 4 段（卷挂载）：见 §6 的车道形状事实。

## 8. 这条车道能证明 / 不能证明什么（含留给 gated 生产步骤的问题）

**能证明（本轮已量）**：控制面 65533 且 `CapEff` 0；它在自己的卷上创建/读写全部平台命名
空间与卷存储；root 一次性交接按卷分流（`control-data`→65533、`worker-data`→65534）；worker
与面 A 仍 65534（两个 uid 不再相等）；`create / 首条命令 / GET /logs / pause+resume /
snapshot+restore / delete / multinode_smoke` 全绿；共享卷上 `_oci` tar 的只读契约
（worker 写不了 tar 目录，`.link` 只能写自己的缓存）。

**不能证明（名字 + 为什么要上生产）**：

* **Q1（模板 tar 的 k8s 形状）**：车队的 `_oci`/worker 缓存的挂载形状（`E2B_IMAGE_OCI_DIR`、
  `E2B_IMAGE_CACHE_DIR=/var/lib/e2b-images`）与控制面 `CapEff` 0 之下的 buildkit 侧车；
* **Q2（卷挂载/卷存储的两卷切法）**：本车道的两个卷让卷挂载不可达（§6），车队的单个 PVC
  形状要在现场量同一支探针；
* **Q3（本车道自己的一条形状不一致）**：三个 worker 共用一个卷，控制面却没有
  `E2B_TREES_SHARED`/`E2B_SHARED_WORKSPACE_ROOT`（只有面 B 有
  `E2B_SHARED_VOLUME_ROOT`）⇒ 迁移按"树在节点本地"处理并在提交后释放源树。
  要不要给这条车道的控制面补上具名判据（或把 `deployment_smoke.py` 的用法头/车道约定
  写清楚），**不在本轮范围内**，留给控制面裁定；
* **Q4（`_snapshots`/`_migrate` 的跨 uid 关系）**：本车道两个卷，`_snapshots`/`_migrate`
  在两侧各有一份，量不到"一个共享命名空间、两个 uid 交替读写"的那种关系；车队的单 PVC
  形状属于现场事实；
* **Q5（`_images` 只读契约里的权限细节）**：实测 buildctl 写出来的 tar 是 **0666**
  （`-rw-rw-rw- 65533:65533`），而 `deploy/k8s/worker.yaml:294-296` 的注释写的是
  "0755 directories, 0644 tars"。目录属于 65533 且 0755，所以**替换**一个 tar 仍要求
  平台 uid；但"worker 连**写**这个 tar 文件都做不到"这句话按 0666 不成立。要不要把 tar
  落成 0644（或把注释改成实测值），是一处**独立**的小决定，留给控制面。

## 9. 收尾（车道拆干净）

```bash
$ tmp/venv/bin/python deploy/scripts/acceptance/cp_uid_lane_diff.py --phase shared --down
  Volume cpuid_buildkit-data Removed
  Volume cpuid_control-data Removed
  Volume cpuid_oci-shared Removed
  Volume cpuid_redis-data Removed
  Volume cpuid_worker-data Removed
  Network cpuid_default Removed
$ env … docker compose -p cpuidprod -f deploy/compose/docker-compose.prod.yml -f tmp/task5-cpuid/prod-lane.yml down -v
  Network cpuidprod_default Removed / Volume cpuidprod_sandbox-shared Removed
$ docker ps -a --format '{{.Names}}' | grep -E "cpuid" || echo "(no cpuid containers)"
(no cpuid containers)
$ docker volume ls --format '{{.Name}}' | grep -E "cpuid" || echo "(no cpuid volumes)"
(no cpuid volumes)
```

用户自己的 `compose` 栈（3100，四小时前就在跑）**没被动**：
`docker ps` 仍是 `compose-control-plane-1` + `compose-redis-1`。本机只剩本轮**构建的镜像**
（`local/e2b/e2b-sandlock-{worker,agent,quota-agent}:cpuid`、`cpuid-control-plane:latest`、
`local/e2b/e2b-sandlock-control-plane-gateway:cpuid`）—— 和这台机器上原有的那批 lane 镜像
同类，留着可复用；要清就是 `docker image rm` 那五个 tag。

## 10. 复现入口

```bash
tmp/venv/bin/python deploy/scripts/acceptance/cp_uid_lane_diff.py --phase lane   --up   # 16/19
tmp/venv/bin/python deploy/scripts/acceptance/cp_uid_lane_diff.py --phase shared --up   # 17/19
tmp/venv/bin/python deploy/scripts/acceptance/cp_uid_lane_diff.py --phase shared --down
```

`--only <shape|create|logs|pause|snapshot|template|volume|delete|smokes>` 只跑一条。
脚本自己生成 `tmp/task5-cpuid/override-lane.yml` 与 `override-shared-namespaces.yml`
（gitignored；生成后用 `docker compose config` 校验一次）。完整原始日志（本轮）：
`tmp/task5-cpuid/run-lane.log`、`tmp/task5-cpuid/run-shared.log`、`up-prod.log`、
`down-*.log`（都在 gitignored 的 `tmp/` 下）。
