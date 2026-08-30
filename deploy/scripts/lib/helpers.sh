#!/usr/bin/env bash
# Shared helpers for the E2B-Sandlock deploy scripts.
# Sourced by bootstrap-target.sh / build-and-push.sh / upgrade.sh / smoke.sh.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIB_DIR="$SCRIPT_DIR/lib"
STACK_DIR="$(cd "$SCRIPT_DIR/.." && pwd)/stack"
VERSION_FILE="$STACK_DIR/.version"

# --- connection defaults (overridable via env or bastion.env) ---
BASTION_HOST="${BASTION_HOST:-172.18.74.236}"
BASTION_USER="${BASTION_USER:-root}"
TARGET_HOST="${TARGET_HOST:-172.18.80.140}"
TARGET_SSH_USER="${TARGET_SSH_USER:-root}"
DEPLOY_USER="${DEPLOY_USER:-deploy}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_pub}"
SSH_PASSPHRASE="${SSH_PASSPHRASE:-}"
TASK_TIMEOUT="${TASK_TIMEOUT:-600}"
REMOTE_DIR="${REMOTE_DIR:-/opt/sandlock}"

# --- ACR credentials (overridable via env or acr.env) ---
ACR_REGISTRY="${ACR_REGISTRY:-registry.cn-shanghai.aliyuncs.com}"
ACR_NAMESPACE="${ACR_NAMESPACE:-byteplan}"
ACR_USERNAME="${ACR_USERNAME:-}"
ACR_PASSWORD="${ACR_PASSWORD:-}"

if [ -f "$SCRIPT_DIR/bastion.env" ]; then
    # shellcheck disable=SC1091
    . "$SCRIPT_DIR/bastion.env"
fi
if [ -f "$SCRIPT_DIR/acr.env" ]; then
    # shellcheck disable=SC1091
    . "$SCRIPT_DIR/acr.env"
fi

export BASTION_HOST BASTION_USER TARGET_HOST TARGET_SSH_USER DEPLOY_USER \
    SSH_KEY SSH_PASSPHRASE TASK_TIMEOUT REMOTE_DIR

say() { printf '\n==> %s\n' "$*"; }

require_expect() {
    command -v expect >/dev/null 2>&1 || {
        echo "缺少 expect（macOS 自带，Linux 请安装 tcl-expect）" >&2
        exit 1
    }
}

require_acr_creds() {
    if [ -z "$ACR_USERNAME" ] || [ -z "$ACR_PASSWORD" ]; then
        echo "缺少 ACR 凭据：设置 ACR_USERNAME/ACR_PASSWORD 或创建 deploy/scripts/acr.env" >&2
        exit 1
    fi
}

# run_target <command> [user]  — run a shell command on the target (as root by default)
run_target() {
    require_expect
    local cmd="$1"
    local user="${2:-$TARGET_SSH_USER}"
    local b64
    b64="$(printf '%s' "$cmd" | base64 | tr -d '\n')"
    expect -f "$LIB_DIR/run-target.exp" "$b64" "$user"
}

# run_as_deploy <command> — run a shell command on the target as the deploy user
run_as_deploy() { run_target "$1" "$DEPLOY_USER"; }

# upload_file <local> <remote> [user] — copy a local file to the target (base64 transport)
upload_file() {
    local src="$1"
    local dst="$2"
    local user="${3:-$DEPLOY_USER}"
    local b64
    b64="$(base64 < "$src" | tr -d '\n')"
    run_target "echo '$b64' | base64 -d > '$dst' && chown $user:$user '$dst' && chmod 600 '$dst' && echo uploaded: '$dst'"
}

# remote_env_value <key> — read one KEY=value from the remote .env (root can read deploy's 600 file)
remote_env_value() {
    run_target "grep -E '^$1=' '$REMOTE_DIR/.env' | tail -1 | cut -d= -f2-" \
        | tr -d '\r' | grep -vE '^[[:space:]]*$' | tail -1
}
