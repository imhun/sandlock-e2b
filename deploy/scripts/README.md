# E2B-Sandlock 部署 / 升级脚本

连接拓扑：本机 → 堡垒机 `root@172.18.74.236`（key `~/.ssh/id_pub` + 口令）→
目标机 `root@172.18.80.140`（堡垒机免密）→ 应用用户 `deploy`（docker 组）。

服务拓扑：**control-plane 与 gateway 合并为一个服务**（合并镜像
`Dockerfile.control-plane-gateway`，单容器单端口 `:3000`，API 与 gateway
路由同端口，`E2B_API_URL == E2B_SANDBOX_URL`）+ 2 个 worker + redis，
共 4 个容器。

## 准备（一次性）

```bash
# 连接信息与口令（不入库）
cp deploy/scripts/bastion.env.example deploy/scripts/bastion.env   # 填 BASTION/TARGET/SSH_PASSPHRASE
cp deploy/scripts/acr.env.example   deploy/scripts/acr.env         # 填 ACR_USERNAME/ACR_PASSWORD
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

## 常用参数

| 脚本 | 参数 / 环境变量 |
|---|---|
| build-and-push.sh | `VERSION`、`PLATFORMS`（默认 amd64+arm64）、`BASE_IMAGE`、`MIRROR_BASE_IMAGE=0` 跳过基础镜像 |
| upgrade.sh | `--build` 先构建；`--version <v>` 固定镜像版本（默认 git describe）；`--env-file <path>` 指定 env；`--force-env` 不保留远端密钥；`--keep-image-tags` 不重写镜像 tag；`--skip-smoke` |
| smoke.sh | 无 |
| 全局 | `BASTION_HOST`、`TARGET_HOST`、`DEPLOY_USER`、`SSH_KEY`、`SSH_PASSPHRASE`、`TASK_TIMEOUT` 可覆盖 |

## 版本与回滚

- 镜像命名：**名称区分服务、tag 区分版本**：
  `e2b-sandlock-{control-plane-gateway,worker,autoscaler}:<VERSION>`
  （redis 用 `redis:8-alpine`、基础镜像用 `python:3.14-slim`，本身即版本号）。
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
