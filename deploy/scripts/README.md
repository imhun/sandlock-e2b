# E2B-Sandlock 部署 / 升级脚本

> ⛔ **这条线已经停用（2026-09-18）**：目标机 `.140` 上的 compose 栈已经
> `docker compose down`（**卷保留**：`sandlock_sandbox-shared` 等 4 个），
> 后续**以 k8s 为主**（自建 k0s 集群，见 [`deploy/k8s-k0s/README.md`](../k8s-k0s/README.md)
> 与 [`docs/k8s-deployment.md`](../../docs/k8s-deployment.md)）。除非明确必要，
> **不要**再跑下面的 `bootstrap-target.sh` / `upgrade.sh`。
>
> 还在这条线里用到的只有 `build-and-push.sh`：它给**所有**组件打同一个版本号并写
> `deploy/stack/.version`，而 k8s 的 `deploy/k8s-k0s/apply.sh` 正是按那个版本号 pin 镜像。
> （`upgrade.sh`、`smoke.sh`、`bootstrap-target.sh` 保留作参考与应急，不再作为常态流程。）

> **k8s 形态不在本文范围内**：`deploy/k8s/` 那套清单的部署顺序、与 compose 的差异表和开关
> 切换方法见 [`docs/k8s-deployment.md`](../../docs/k8s-deployment.md)。本文只管目标机
> compose 这条线（堡垒机 → 目标机 → 应用用户 `deploy`）。
> 自建 k0s 集群（清单 overlay + 集群怎么起 + 已知未完成项）见
> [`deploy/k8s-k0s/README.md`](../k8s-k0s/README.md)。

## 集群身份闸门（写侧脚本通用）

写侧的 k8s 脚本在碰集群之前必须过 [`lib/cluster-guard.sh`](lib/cluster-guard.sh) 的
`require_target_cluster`（`deploy/k8s-k0s/apply.sh` 连 **DRY_RUN 都算**）：`KUBECONFIG` 必须
**显式设置**（且文件存在）、server `gitVersion` 必须含 `+k0s`、节点必须是 `E2B_TARGET_NODES`
（默认 2）× `arm64` × `kubeletVersion` 含 `+k0s`；不符即 `exit 2` 并点名 context / server
版本 / 每台节点的架构与版本。原因：本机 `kubectl` 的默认 context 指向**另一套阿里云 ACK
集群**，不加 `KUBECONFIG` 的写操作会**安静地**打到那边 —— 2026-10-02 真的发生过一次，
见 [`docs/deploy-clusters.md`](../../docs/deploy-clusters.md) §7.34.1。

```bash
cd <仓库根>
export KUBECONFIG="$PWD/tmp/k0s/kubeconfig"   # 没有就先跑 open-cluster-tunnel.sh
```

连接拓扑：本机 → 堡垒机 `root@172.18.74.236`（key `~/.ssh/id_pub` + 口令）→
目标机 `root@172.18.80.140`（堡垒机免密）→ 应用用户 `deploy`（docker 组）。

服务拓扑：**control-plane 与 gateway 合并为一个服务**（合并镜像
`deploy/docker/Dockerfile.control-plane-gateway`，单容器单端口 `:3000`，API 与 gateway
路由同端口，`E2B_API_URL == E2B_SANDBOX_URL`）+ 2 个 worker + redis，
共 4 个容器。

## 准备（一次性）

```bash
# 连接信息与口令（不入库）
cp deploy/scripts/bastion.env.example deploy/scripts/bastion.env   # 填 BASTION/TARGET/SSH_PASSPHRASE
cp deploy/scripts/acr.env.example   deploy/scripts/acr.env         # 填 ACR_USERNAME/ACR_PASSWORD
chmod 600 deploy/scripts/bastion.env deploy/scripts/acr.env        # cp 出来是 644 ⇒ 不 chmod 的话，lib/helpers.sh 会 refuse 并退出码 1
```

初始化目标机（装 Docker、建 deploy 用户、ACR 登录、镜像加速）：

```bash
./deploy/scripts/bootstrap-target.sh
```

## 升级流程

```bash
# 1) 本地构建多架构镜像并推送 ACR（版本号取自 git describe，可 VERSION=1.2.3 覆盖）
./deploy/scripts/build-and-push.sh

# 2) 升级目标机：上传配置 → pull → compose up → 节点检查 → 冒烟
./deploy/scripts/upgrade.sh          # 复用已推送的滚动 tag
# 或一条龙：
./deploy/scripts/upgrade.sh --build

# 3) 仅跑冒烟
./deploy/scripts/smoke.sh
```

> 从 fix round 1 之前的版本升级到 c1 权限模型时，先按下一节把既有工作区切一次
> （本轮从未发布过 ⇒ 只有开发/测试环境需要）。

## 一次性迁移：把既有沙箱工作区切到 c1 权限模型（`0770 <沙箱 uid>:<worker gid>`）

F1/fix round 1（裁定 c1）之后，worker 通过**属组**访问沙箱工作区
（`0770 owner=<沙箱 uid> group=<worker effective gid>`，见
`docs/production-deployment-requirements.md` §2.4）。**本轮从未发布过**，所以只有
开发/测试环境里存在 fix round 1 之前/之中建出的 `0700 owner=X` 工作区；滚动升级到 c1
镜像前把它们切一次即可（worker 自己改不动别人的模式：broker 刻意不带 `CAP_FOWNER`）。

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

要点：

* **取 worker gid 要用容器里的 `id -g`**（compose 是 `user: "65534:65534"`；k8s 若设了
  `runAsGroup` 就以 pod 的值为准）——不要硬编码 65534。
* `chgrp -R` 只改属组（文件模式不动，沙箱还是自己文件的属主）；目录统一 `0770`。
* 只迁移 `sbx_*` 顶层目录：卷根保持 `1777`，`_volumes`/`_snapshots`/`_migrate`
  不动（它们本来就归 worker）。
* 迁移后 worker 侧数据面（files API / watcher / 命令日志 / 快照）才可用；不迁移时
  这些路径会 EACCES，route-B 槽位与命令执行不受影响。

## 常用参数

| 脚本 | 参数 / 环境变量 |
|---|---|
| build-and-push.sh | `VERSION`、`PLATFORMS`（默认 amd64+arm64）、`BASE_IMAGE`、`MIRROR_BASE_IMAGE=0` 跳过基础镜像 |
| upgrade.sh | `--build` 先构建；`--version <v>` 固定镜像版本（默认 git describe）；`--env-file <path>` 指定 env；`--force-env` 不保留远端密钥；`--keep-image-tags` 不重写镜像 tag（**不能**与 `--with-quota-agent` 合用：会留下空的 `QUOTA_AGENT_IMAGE` ⇒ 回落 `:latest`，脚本 fail fast 并点名该组合）；`--skip-smoke`；`--with-quota-agent` 开栈内 agent（写 `QUOTA_AGENT_PROFILE=1`、固定 `QUOTA_AGENT_IMAGE`、带上 `--profile quota`，缺 `E2B_QUOTA_AGENT_TOKEN` 时 fail fast）；`--without-quota-agent` 关：写回 `QUOTA_AGENT_PROFILE=0`、清掉栈内 `E2B_QUOTA_AGENT_URL`，并在目标机**显式** `docker compose --profile quota rm -sf quota-agent`（不依赖 `--remove-orphans`；外置 agent 的 URL 保留） |
| smoke.sh | 无 |
| 全局 | `BASTION_HOST`、`TARGET_HOST`、`DEPLOY_USER`、`SSH_KEY`、`SSH_PASSPHRASE`、`TASK_TIMEOUT` 可覆盖 |

## 版本与回滚

- 镜像命名：**名称区分服务、tag 区分版本**：
  `e2b-sandlock-{control-plane-gateway,worker,agent,quota-agent}:<VERSION>`
  （redis 用 `redis:8-alpine`、基础镜像用 `python:3.14-slim`，本身即版本号）。
- **每沙箱磁盘配额（可选）**：`upgrade.sh --with-quota-agent` 把
  `QUOTA_AGENT_IMAGE` 固定成 `<registry>/<ns>/e2b-sandlock-quota-agent:<VERSION>`
  （`build-images.sh`/`build-and-push.sh` 会构建并推送它，与 worker 同一个流程），
  写 `QUOTA_AGENT_PROFILE=1` 并让 worker 指向栈内 agent；后续升级自动带
  `--profile quota`。`--without-quota-agent` 是它的逆操作：写回 0、清掉栈内 URL，
  **并显式 `rm -sf` 掉那个容器**（compose 不会因为服务离开 active profile 就停它，
  实测 compose 5.1.2）；外置/NFS 服务器形态的 agent 不要开这个开关（开着 off 也无妨，
  外置 URL 不会被清）。
- **开发构建默认带时间戳**：`build-and-push.sh` 未指定 `VERSION` 时使用
  `<git describe>-<时间戳>`（如 `0.1.0-20260830-153045`），每次构建都是新
  tag，不覆盖旧镜像；本次构建版本记录在 `deploy/stack/.version`。
- `upgrade.sh` 默认部署 **最近一次构建** 的版本（读 `deploy/stack/.version`，
  `--version` / `VERSION=` 可覆盖），因此 **构建即部署**：
  `upgrade.sh --build` 一条命令完成带时间戳版本的构建、推送与升级。
- 发布固定版本：`VERSION=1.0.0 ./deploy/scripts/build-and-push.sh`（不带
  时间戳）。
- 回滚：`VERSION=<旧版本> ./deploy/scripts/upgrade.sh`（旧镜像需已存在于
  ACR），或编辑 `.env` 后加 `--keep-image-tags` 再升级。

## 注意事项

- 目标机 Docker Hub 不通：基础镜像已镜像到 ACR（`byteplan/python:3.14-slim`），
  新增基础镜像需先 `build-and-push.sh` 镜像或手动推到 ACR。
- 本机构建依赖 `multiarch` buildx builder（脚本自动创建，docker.io 走
  daocloud 加速）；Docker Hub 不可达时会失败。
- `Template.build`（控制面模板构建）当前不可用：控制面镜像未装 docker CLI，
  目标机也不通 Docker Hub；仅支持预置 base 镜像的单机模板行为。
- 冒烟脚本依赖目标机能访问 `pypi.tuna.tsinghua.edu.cn`（e2b SDK 安装）。
- 所有远端操作以 base64 传输、目标机 `/tmp/sandlock-task.sh` 执行；
  `TASK_TIMEOUT` 默认 600s，慢网络可调大。
