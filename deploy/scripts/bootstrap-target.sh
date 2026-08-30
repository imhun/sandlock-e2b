#!/usr/bin/env bash
# One-time target host preparation (run as root via the bastion):
#   * verify kernel Landlock ABI >= 6 (sandlock requirement)
#   * install Docker CE + compose plugin (aliyun mirror, aarch64/x86_64)
#   * configure daemon registry mirrors (Docker Hub is unreachable)
#   * create the deploy user (docker group) + SSH key authorization
#   * log the deploy user into ACR and sanity-pull redis
#
# Usage: ./deploy/scripts/bootstrap-target.sh
# Env:   ACR_USERNAME / ACR_PASSWORD (or deploy/scripts/acr.env)

set -euo pipefail
. "$(cd "$(dirname "$0")" && pwd)/lib/helpers.sh"

say "目标机 Landlock ABI 检查（需 >= 6）"
run_target '
python3 - <<"PY"
import ctypes
libc = ctypes.CDLL(None, use_errno=True)
abi = libc.syscall(444, 0, 0, 1)  # SYS_landlock_create_ruleset, VERSION
print("landlock ABI =", abi)
raise SystemExit(0 if abi >= 6 else 1)
PY
'

say "安装 Docker CE（阿里云源）"
run_target '
set -e
if ! command -v docker >/dev/null 2>&1; then
    curl -fsSL -o /etc/yum.repos.d/docker-ce.repo \
        https://mirrors.aliyun.com/docker-ce/linux/centos/docker-ce.repo || true
    if ! grep -q "mirrors.aliyun.com" /etc/yum.repos.d/docker-ce.repo 2>/dev/null; then
        cat > /etc/yum.repos.d/docker-ce.repo <<"REPO"
[docker-ce-stable]
name=Docker CE Stable - $basearch
baseurl=https://mirrors.aliyun.com/docker-ce/linux/centos/$releasever/$basearch/stable
enabled=1
gpgcheck=1
gpgkey=https://mirrors.aliyun.com/docker-ce/linux/centos/gpg
REPO
    fi
    dnf -y install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    systemctl enable --now docker
fi
docker --version
docker compose version
'

say "配置 daemon 镜像加速并重启"
run_target '
set -e
mkdir -p /etc/docker
cat > /etc/docker/daemon.json <<"JSON"
{
  "registry-mirrors": [
    "https://docker.m.daocloud.io",
    "https://docker.1ms.run",
    "https://dockerpull.cn"
  ]
}
JSON
systemctl restart docker
docker info 2>&1 | grep -A3 "Registry Mirrors" | head -5
'

say "创建 deploy 用户 + docker 组 + SSH 授权"
PUBKEY="$(cat "${SSH_KEY}.pub")"
run_target "
set -e
id $DEPLOY_USER >/dev/null 2>&1 || useradd -m -s /bin/bash $DEPLOY_USER
usermod -aG docker $DEPLOY_USER
mkdir -p /home/$DEPLOY_USER/.ssh
if ! grep -qF '$PUBKEY' /home/$DEPLOY_USER/.ssh/authorized_keys 2>/dev/null; then
    echo '$PUBKEY' >> /home/$DEPLOY_USER/.ssh/authorized_keys
fi
chmod 700 /home/$DEPLOY_USER/.ssh
chmod 600 /home/$DEPLOY_USER/.ssh/authorized_keys
chown -R $DEPLOY_USER:$DEPLOY_USER /home/$DEPLOY_USER/.ssh
mkdir -p $REMOTE_DIR
chown $DEPLOY_USER:$DEPLOY_USER $REMOTE_DIR
id $DEPLOY_USER
"

say "deploy 用户登录 ACR"
require_acr_creds
run_as_deploy "printf '%s' '$ACR_PASSWORD' | docker login $ACR_REGISTRY -u '$ACR_USERNAME' --password-stdin >/dev/null && echo 'ACR login ok'"

say "验证镜像加速（拉取 redis）"
run_as_deploy "docker pull redis:7-alpine >/dev/null && echo 'redis pull ok'"

say "完成。下一步：./deploy/scripts/upgrade.sh --build"
