#!/usr/bin/env bash
# Deploy / upgrade the stack on the target server (runs as the deploy user).
#   * upload deploy/stack/docker-compose.prod.yml
#   * upload .env (local deploy/stack/.env or --env-file; first deploy generates one)
#     - existing remote secrets are preserved unless --force-env
#     - image tags are pinned to VERSION (default: git describe; override
#       with --version or VERSION=) unless --keep-image-tags
#   * docker compose pull + up -d --no-build on the target
#   * wait, then verify containers and worker registration
#   * run smoke tests (skip with --skip-smoke)
#
# Usage: ./deploy/scripts/upgrade.sh [--build] [--version <v>] [--env-file <path>] [--skip-smoke] [--force-env] [--keep-image-tags]

set -euo pipefail
. "$(cd "$(dirname "$0")" && pwd)/lib/helpers.sh"

BUILD=0
SKIP_SMOKE=0
FORCE_ENV=0
KEEP_IMAGE_TAGS=0
ENV_FILE=""
VERSION_ARG=""
while [ $# -gt 0 ]; do
    case "$1" in
        --build) BUILD=1 ;;
        --version) VERSION_ARG="$2"; shift ;;
        --env-file) ENV_FILE="$2"; shift ;;
        --skip-smoke) SKIP_SMOKE=1 ;;
        --force-env) FORCE_ENV=1 ;;
        --keep-image-tags) KEEP_IMAGE_TAGS=1 ;;
        *) echo "未知参数: $1" >&2; exit 1 ;;
    esac
    shift
done

REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
if [ -n "$VERSION_ARG" ]; then
    VERSION="$VERSION_ARG"
elif [ -z "${VERSION:-}" ] && [ -f "$VERSION_FILE" ]; then
    VERSION="$(cat "$VERSION_FILE")"
fi
VERSION="${VERSION:-$(git -C "$REPO_DIR" describe --tags --always 2>/dev/null || echo 0.1.0)}"

if [ "$BUILD" = "1" ]; then
    say "先构建并推送镜像"
    "$SCRIPT_DIR/build-and-push.sh"
fi

COMPOSE="$STACK_DIR/docker-compose.prod.yml"
[ -f "$COMPOSE" ] || { echo "缺少 $COMPOSE" >&2; exit 1; }

# --- .env 解析：--env-file > deploy/stack/.env > 复用远端 ---
if [ -z "$ENV_FILE" ] && [ -f "$STACK_DIR/.env" ]; then
    ENV_FILE="$STACK_DIR/.env"
fi
if [ -z "$ENV_FILE" ]; then
    if run_target "test -f '$REMOTE_DIR/.env'" >/dev/null 2>&1; then
        say "拉取目标机现有 .env 到 $STACK_DIR/.env"
        run_target "cat '$REMOTE_DIR/.env'" > "$STACK_DIR/.env"
        chmod 600 "$STACK_DIR/.env"
        ENV_FILE="$STACK_DIR/.env"
    else
        say "首次部署：生成密钥并写入 $STACK_DIR/.env"
        require_acr_creds
        API_KEY="$(openssl rand -hex 24)"
        INTERNAL_KEY="$(openssl rand -hex 24)"
        sed -e "s|__E2B_API_KEYS__|$API_KEY|" \
            -e "s|__E2B_INTERNAL_API_KEY__|$INTERNAL_KEY|" \
            -e "s|__ACR_USERNAME__|$ACR_USERNAME|" \
            -e "s|__ACR_PASSWORD__|$ACR_PASSWORD|" \
            "$STACK_DIR/.env.example" > "$STACK_DIR/.env"
        chmod 600 "$STACK_DIR/.env"
        ENV_FILE="$STACK_DIR/.env"
    fi
fi

# --- 保留远端密钥（默认）---
if [ -n "$ENV_FILE" ] && [ "$FORCE_ENV" != "1" ]; then
    for key in E2B_API_KEYS E2B_INTERNAL_API_KEY E2B_IMAGE_REGISTRY_PASSWORD; do
        if grep -qE "^$key=(__.*__)?$" "$ENV_FILE"; then
            remote_val="$(remote_env_value "$key" || true)"
            if [ -n "$remote_val" ]; then
                sed "s|^$key=.*|$key=$remote_val|" "$ENV_FILE" > "$ENV_FILE.tmp" && mv "$ENV_FILE.tmp" "$ENV_FILE"
                say "已从远端保留 $key"
            fi
        fi
    done
fi

# --- 镜像 tag 固定为当前版本（除非 --keep-image-tags）---
if [ "$KEEP_IMAGE_TAGS" != "1" ] && [ -n "$ENV_FILE" ]; then
    REGISTRY_URL="$ACR_REGISTRY/$ACR_NAMESPACE"
    for entry in "CONTROL_PLANE_IMAGE:e2b-sandlock-control-plane-gateway" "WORKER_IMAGE:e2b-sandlock-worker"; do
        key="${entry%%:*}"
        suffix="${entry#*:}"
        sed "s|^$key=.*|$key=$REGISTRY_URL/$suffix:$VERSION|" \
            "$ENV_FILE" > "$ENV_FILE.tmp" && mv "$ENV_FILE.tmp" "$ENV_FILE"
    done
    say "镜像 tag 固定为版本 $VERSION"
fi

say "上传部署文件到 $REMOTE_DIR"
upload_file "$COMPOSE" "$REMOTE_DIR/docker-compose.prod.yml" "$DEPLOY_USER"
upload_file "$STACK_DIR/buildkitd.toml" "$REMOTE_DIR/buildkitd.toml" "$DEPLOY_USER"
if [ -n "$ENV_FILE" ]; then
    upload_file "$ENV_FILE" "$REMOTE_DIR/.env" "$DEPLOY_USER"
fi

say "拉取镜像并重建（deploy 用户）"
run_as_deploy "cd '$REMOTE_DIR' && docker compose -f docker-compose.prod.yml pull --quiet && docker compose -f docker-compose.prod.yml up -d --no-build --remove-orphans"

say "等待就绪并检查容器"
run_as_deploy "sleep 8 && cd '$REMOTE_DIR' && docker compose -f docker-compose.prod.yml ps --format 'table {{.Name}}\t{{.Status}}'"

say "校验 control-plane 网络挂载（异常时重跑 up 自愈）"
run_as_deploy "cd '$REMOTE_DIR' && docker inspect sandlock-control-plane-1 --format '{{range \$k, \$v := .NetworkSettings.Networks}}{{\$k}} {{end}}' | grep -q 'sandlock_default' || docker compose -f docker-compose.prod.yml up -d --no-build --remove-orphans"

say "检查 worker 注册"
INTERNAL_KEY="$(remote_env_value E2B_INTERNAL_API_KEY)"
run_target "set -o pipefail; for i in 1 2 3 4 5 6; do OUT=\$(curl -s -m 5 -H 'X-Internal-Key: $INTERNAL_KEY' http://127.0.0.1:3000/internal/nodes) && [ -n \"\$OUT\" ] && break; sleep 5; done; printf '%s' \"\$OUT\" | python3 -c 'import sys,json; d=json.load(sys.stdin); [print(\"{:12} {:8} mem={}/{}\".format(n[\"nodeID\"], n[\"status\"], n[\"reservedMemoryMB\"], n[\"totalMemoryMB\"])) for n in d]'"

if [ "$SKIP_SMOKE" != "1" ]; then
    say "运行冒烟验证"
    "$SCRIPT_DIR/smoke.sh"
fi

say "升级完成"
