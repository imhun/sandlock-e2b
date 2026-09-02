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

# --- E6.2 image digest pinning ---------------------------------------------

# parse_image_ref <ref> — split an image reference into "repo|tag|digest".
# digest includes the leading @sha256: prefix (empty when absent); tag is
# empty when the reference carries no explicit tag.
parse_image_ref() {
    local ref="$1" digest="" tag="" last_seg
    if [[ "$ref" == *"@"* ]]; then
        digest="${ref##*@}"
        ref="${ref%@*}"
        digest="@$digest"
    fi
    last_seg="${ref##*/}"
    if [[ "$last_seg" == *":"* ]]; then
        tag="${last_seg##*:}"
        ref="${ref%:$tag}"
    fi
    printf '%s|%s|%s\n' "$ref" "$tag" "$digest"
}

# validate_env_file_base_image <env_file> — fail closed unless
# E2B_BASE_IMAGE is pinned with a well-formed @sha256: digest (E6.2).
# Tag-only refs are refused by default; set ALLOW_TAG_BASE_IMAGE=1 to
# accept them explicitly (non-production). A tag change therefore requires
# the operator to explicitly update the digest in the same edit.
validate_env_file_base_image() {
    local env_file="$1"
    local ref digest hex
    ref="$(sed -n 's/^E2B_BASE_IMAGE=//p' "$env_file" | tail -1 | tr -d '\r')"
    if [ -z "$ref" ]; then
        echo "E2B_BASE_IMAGE 未在 $env_file 中配置" >&2
        return 1
    fi
    digest="$(parse_image_ref "$ref" | cut -d'|' -f3)"
    if [[ "$digest" != @sha256:* ]]; then
        if [ "${ALLOW_TAG_BASE_IMAGE:-0}" = "1" ]; then
            say "警告：E2B_BASE_IMAGE 未固定 digest（--allow-tag-base-image，仅限非生产）"
            return 0
        fi
        echo "E2B_BASE_IMAGE 必须固定 @sha256: digest（供应链要求 E6.2）：$ref" >&2
        echo "解析方法：docker buildx imagetools inspect <镜像:tag> --format '{{.Manifest.Digest}}'" >&2
        echo "然后显式更新为 <镜像:tag>@sha256:<digest>；tag 变更必须同步更新 digest。" >&2
        return 1
    fi
    hex="${digest#@sha256:}"
    if [[ "$hex" == *"__"* ]]; then
        echo "E2B_BASE_IMAGE 的 digest 是未解析占位符：$ref" >&2
        echo "按上方方法解析真实 digest 并替换后再部署。" >&2
        return 1
    fi
    if ! [[ "$hex" =~ ^[0-9a-f]{64}$ ]]; then
        echo "E2B_BASE_IMAGE 的 digest 格式非法（需 @sha256: 后接 64 位十六进制）：$ref" >&2
        return 1
    fi
    return 0
}
