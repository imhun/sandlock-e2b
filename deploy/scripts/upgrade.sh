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
# Usage: ./deploy/scripts/upgrade.sh [--build] [--version <v>] [--env-file <path>] [--skip-smoke] [--force-env] [--keep-image-tags] [--allow-tag-base-image]
#                                     [--with-quota-agent] [--without-quota-agent]
#                                     [--rotate-internal-key] [--finalize-internal-key-rotation <old-key>]
#                                     [--rotate-secret-master-key] [--finalize-secret-master-key-rotation <old-key>]

set -euo pipefail
. "$(cd "$(dirname "$0")" && pwd)/lib/helpers.sh"

BUILD=0
SKIP_SMOKE=0
FORCE_ENV=0
KEEP_IMAGE_TAGS=0
ALLOW_TAG_BASE_IMAGE=0
ROTATE_INTERNAL=0
FINALIZE_INTERNAL=""
ROTATE_SECRET_MASTER=0
FINALIZE_SECRET_MASTER=""
WITH_QUOTA_AGENT=0
WITHOUT_QUOTA_AGENT=0
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
        --allow-tag-base-image) ALLOW_TAG_BASE_IMAGE=1 ;;
        --with-quota-agent) WITH_QUOTA_AGENT=1 ;;
        --without-quota-agent) WITHOUT_QUOTA_AGENT=1 ;;
        --rotate-internal-key) ROTATE_INTERNAL=1 ;;
        --finalize-internal-key-rotation) FINALIZE_INTERNAL="${2:-}"; shift ;;
        --rotate-secret-master-key) ROTATE_SECRET_MASTER=1 ;;
        --finalize-secret-master-key-rotation) FINALIZE_SECRET_MASTER="${2:-}"; shift ;;
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
        REDIS_PASSWORD="$(openssl rand -hex 24)"
        SECRET_MASTER_KEY="$(openssl rand -hex 32)"
        sed -e "s|__E2B_API_KEYS__|$API_KEY|" \
            -e "s|__E2B_INTERNAL_API_KEY__|$INTERNAL_KEY|" \
            -e "s|__E2B_REDIS_PASSWORD__|$REDIS_PASSWORD|" \
            -e "s|__E2B_SECRET_MASTER_KEY__|$SECRET_MASTER_KEY|" \
            -e "s|__ACR_USERNAME__|$ACR_USERNAME|" \
            -e "s|__ACR_PASSWORD__|$ACR_PASSWORD|" \
            "$STACK_DIR/.env.example" > "$STACK_DIR/.env"
        chmod 600 "$STACK_DIR/.env"
        ENV_FILE="$STACK_DIR/.env"
    fi
fi

# --- 保留远端密钥（默认）---
if [ -n "$ENV_FILE" ] && [ "$FORCE_ENV" != "1" ]; then
    for key in E2B_API_KEYS E2B_INTERNAL_API_KEY E2B_INTERNAL_API_KEYS E2B_IMAGE_REGISTRY_PASSWORD E2B_REDIS_PASSWORD E2B_SECRET_MASTER_KEY E2B_SECRET_MASTER_KEYS; do
        if grep -qE "^$key=(__.*__)?$" "$ENV_FILE"; then
            remote_val="$(remote_env_value "$key" || true)"
            if [ -n "$remote_val" ]; then
                sed "s|^$key=.*|$key=$remote_val|" "$ENV_FILE" > "$ENV_FILE.tmp" && mv "$ENV_FILE.tmp" "$ENV_FILE"
                say "已从远端保留 $key"
            fi
        fi
    done
fi

# --- internal key 轮换（E3.6）---
# 轮换 = 生成新主 key 并把它追加到 E2B_INTERNAL_API_KEYS（旧 key 保留，
# 轮换窗口内新旧 key 均可用）；finalize = 从列表移除旧 key（立即失效）。
if [ "$ROTATE_INTERNAL" = "1" ]; then
    [ -n "$ENV_FILE" ] || { echo "--rotate-internal-key 需要 .env（首次部署会自动生成）" >&2; exit 1; }
    NEW_INTERNAL_KEY="$(openssl rand -hex 24)"
    CURRENT_KEYS="$(sed -n 's/^E2B_INTERNAL_API_KEYS=//p' "$ENV_FILE" | tail -1)"
    if [ -z "$CURRENT_KEYS" ] || [ "$CURRENT_KEYS" = "__E2B_INTERNAL_API_KEYS__" ]; then
        CURRENT_KEYS="$(sed -n 's/^E2B_INTERNAL_API_KEY=//p' "$ENV_FILE" | tail -1)"
    fi
    [ -n "$CURRENT_KEYS" ] || { echo "无法确定当前 internal key（.env 缺少 E2B_INTERNAL_API_KEY）" >&2; exit 1; }
    NEW_LIST="$(INTERNAL_NEW_KEY="$NEW_INTERNAL_KEY" INTERNAL_CURRENT="$CURRENT_KEYS" python3 -c '
import os
keys = [k for k in os.environ["INTERNAL_CURRENT"].split(",") if k]
keys = list(dict.fromkeys(keys + [os.environ["INTERNAL_NEW_KEY"]]))
print(",".join(keys))
')"
    if grep -q '^E2B_INTERNAL_API_KEYS=' "$ENV_FILE"; then
        sed "s|^E2B_INTERNAL_API_KEYS=.*|E2B_INTERNAL_API_KEYS=$NEW_LIST|" "$ENV_FILE" > "$ENV_FILE.tmp" && mv "$ENV_FILE.tmp" "$ENV_FILE"
    else
        printf '\nE2B_INTERNAL_API_KEYS=%s\n' "$NEW_LIST" >> "$ENV_FILE"
    fi
    sed "s|^E2B_INTERNAL_API_KEY=.*|E2B_INTERNAL_API_KEY=$NEW_INTERNAL_KEY|" "$ENV_FILE" > "$ENV_FILE.tmp" && mv "$ENV_FILE.tmp" "$ENV_FILE"
    say "已轮换 internal key：新主 key 写入 E2B_INTERNAL_API_KEY；旧 key 保留在 E2B_INTERNAL_API_KEYS（轮换窗口内仍可用）"
    say "所有节点滚动到新 key 后，执行 upgrade.sh --finalize-internal-key-rotation <旧key> 移除旧 key"
fi

if [ -n "$FINALIZE_INTERNAL" ]; then
    [ -n "$ENV_FILE" ] || { echo "--finalize-internal-key-rotation 需要 .env" >&2; exit 1; }
    PRIMARY_KEY="$(sed -n 's/^E2B_INTERNAL_API_KEY=//p' "$ENV_FILE" | tail -1)"
    if [ "$FINALIZE_INTERNAL" = "$PRIMARY_KEY" ]; then
        echo "不能移除当前主 key；请先 --rotate-internal-key 生成新主 key" >&2
        exit 1
    fi
    CURRENT_KEYS="$(sed -n 's/^E2B_INTERNAL_API_KEYS=//p' "$ENV_FILE" | tail -1)"
    [ -n "$CURRENT_KEYS" ] || { echo "E2B_INTERNAL_API_KEYS 为空：旧 key 已不在生效列表" >&2; exit 1; }
    REMAINING="$(INTERNAL_OLD_KEY="$FINALIZE_INTERNAL" INTERNAL_CURRENT="$CURRENT_KEYS" python3 -c '
import os
old = os.environ["INTERNAL_OLD_KEY"]
keys = [k for k in os.environ["INTERNAL_CURRENT"].split(",") if k and k != old]
print(",".join(keys))
')"
    if [ "$REMAINING" = "$CURRENT_KEYS" ]; then
        echo "E2B_INTERNAL_API_KEYS 中未找到 $FINALIZE_INTERNAL" >&2
        exit 1
    fi
    sed "s|^E2B_INTERNAL_API_KEYS=.*|E2B_INTERNAL_API_KEYS=$REMAINING|" "$ENV_FILE" > "$ENV_FILE.tmp" && mv "$ENV_FILE.tmp" "$ENV_FILE"
    say "已从 E2B_INTERNAL_API_KEYS 移除 $FINALIZE_INTERNAL：该 key 立即失效"
fi

# --- secret master key 轮换（E5.4）---
# 轮换 = 生成新主 key 并保留旧 key 在 E2B_SECRET_MASTER_KEYS（轮换窗口内
# 旧密文仍可解密）；控制面滚动后旧密文会被主 key 重新加密，再 finalize
# 从列表移除旧 key。
if [ "$ROTATE_SECRET_MASTER" = "1" ]; then
    [ -n "$ENV_FILE" ] || { echo "--rotate-secret-master-key 需要 .env（首次部署会自动生成）" >&2; exit 1; }
    NEW_MASTER="$(openssl rand -hex 32)"
    CURRENT_MASTER="$(sed -n 's/^E2B_SECRET_MASTER_KEY=//p' "$ENV_FILE" | tail -1)"
    if [ -z "$CURRENT_MASTER" ] || [ "$CURRENT_MASTER" = "__E2B_SECRET_MASTER_KEY__" ]; then
        echo "无法确定当前 secret master key（.env 缺少 E2B_SECRET_MASTER_KEY）" >&2
        exit 1
    fi
    CURRENT_LEGACY="$(sed -n 's/^E2B_SECRET_MASTER_KEYS=//p' "$ENV_FILE" | tail -1)"
    NEW_LIST="$(MASTER_OLD="$CURRENT_MASTER" MASTER_LEGACY="$CURRENT_LEGACY" python3 -c '
import os
keys = [k for k in os.environ["MASTER_LEGACY"].split(",") if k]
keys = list(dict.fromkeys(keys + [os.environ["MASTER_OLD"]]))
print(",".join(keys))
')"
    if grep -q '^E2B_SECRET_MASTER_KEYS=' "$ENV_FILE"; then
        sed "s|^E2B_SECRET_MASTER_KEYS=.*|E2B_SECRET_MASTER_KEYS=$NEW_LIST|" "$ENV_FILE" > "$ENV_FILE.tmp" && mv "$ENV_FILE.tmp" "$ENV_FILE"
    else
        printf '\nE2B_SECRET_MASTER_KEYS=%s\n' "$NEW_LIST" >> "$ENV_FILE"
    fi
    sed "s|^E2B_SECRET_MASTER_KEY=.*|E2B_SECRET_MASTER_KEY=$NEW_MASTER|" "$ENV_FILE" > "$ENV_FILE.tmp" && mv "$ENV_FILE.tmp" "$ENV_FILE"
    say "已轮换 secret master key：新主 key 写入 E2B_SECRET_MASTER_KEY；旧 key 保留在 E2B_SECRET_MASTER_KEYS（轮换窗口内旧密文仍可解密）"
    say "所有控制面滚动并重新加密后，执行 upgrade.sh --finalize-secret-master-key-rotation <旧key> 移除旧 key"
fi

if [ -n "$FINALIZE_SECRET_MASTER" ]; then
    [ -n "$ENV_FILE" ] || { echo "--finalize-secret-master-key-rotation 需要 .env" >&2; exit 1; }
    PRIMARY_MASTER="$(sed -n 's/^E2B_SECRET_MASTER_KEY=//p' "$ENV_FILE" | tail -1)"
    if [ "$FINALIZE_SECRET_MASTER" = "$PRIMARY_MASTER" ]; then
        echo "不能移除当前主 key；请先 --rotate-secret-master-key 生成新主 key" >&2
        exit 1
    fi
    CURRENT_LEGACY="$(sed -n 's/^E2B_SECRET_MASTER_KEYS=//p' "$ENV_FILE" | tail -1)"
    [ -n "$CURRENT_LEGACY" ] || { echo "E2B_SECRET_MASTER_KEYS 为空：旧 key 已不在生效列表" >&2; exit 1; }
    REMAINING="$(MASTER_OLD="$FINALIZE_SECRET_MASTER" MASTER_LEGACY="$CURRENT_LEGACY" python3 -c '
import os
old = os.environ["MASTER_OLD"]
keys = [k for k in os.environ["MASTER_LEGACY"].split(",") if k and k != old]
print(",".join(keys))
')"
    if [ "$REMAINING" = "$CURRENT_LEGACY" ]; then
        echo "E2B_SECRET_MASTER_KEYS 中未找到 $FINALIZE_SECRET_MASTER" >&2
        exit 1
    fi
    sed "s|^E2B_SECRET_MASTER_KEYS=.*|E2B_SECRET_MASTER_KEYS=$REMAINING|" "$ENV_FILE" > "$ENV_FILE.tmp" && mv "$ENV_FILE.tmp" "$ENV_FILE"
    say "已从 E2B_SECRET_MASTER_KEYS 移除 $FINALIZE_SECRET_MASTER：旧 key 立即失效"
fi

# --- 基础镜像 digest 固定（E6.2）---
# 生产部署要求 E2B_BASE_IMAGE 以 @sha256: digest 形式固定；tag-only 或
# 未解析占位符会被拒绝（--allow-tag-base-image 显式放行非生产场景）。
# tag 变更必须同时显式更新 digest，禁止“改了 tag 静默部署”。
if [ -n "$ENV_FILE" ]; then
    validate_env_file_base_image "$ENV_FILE" || exit 1
fi

# --- 镜像 tag 固定为当前版本（除非 --keep-image-tags）---
if [ "$KEEP_IMAGE_TAGS" != "1" ] && [ -n "$ENV_FILE" ]; then
    REGISTRY_URL="$ACR_REGISTRY/$ACR_NAMESPACE"
    # A6: QUOTA_AGENT_IMAGE is pinned too (and added when the .env predates it)
    # so the quota-agent image the worker points at is pullable on the target.
    for entry in "CONTROL_PLANE_IMAGE:e2b-sandlock-control-plane-gateway" "WORKER_IMAGE:e2b-sandlock-worker" "QUOTA_AGENT_IMAGE:e2b-sandlock-quota-agent"; do
        key="${entry%%:*}"
        suffix="${entry#*:}"
        set_env_file_value "$ENV_FILE" "$key" "$REGISTRY_URL/$suffix:$VERSION"
    done
    say "镜像 tag 固定为版本 $VERSION"
fi

# --- quota-agent（A6）：栈内 agent 形态 -------------------------------------
# --with-quota-agent 把 QUOTA_AGENT_PROFILE=1 写进 .env（粘性：之后的普通
# upgrade 也带 --profile quota 起本地 agent），并在 worker 还没配 URL 时指向
# 栈内服务；--without-quota-agent 只是把开关写回 0（配额随即降级 + WARNING）。
if [ "$WITH_QUOTA_AGENT" = "1" ] && [ "$WITHOUT_QUOTA_AGENT" = "1" ]; then
    echo "--with-quota-agent 与 --without-quota-agent 互斥" >&2
    exit 1
fi
if [ "$WITH_QUOTA_AGENT" = "1" ] || [ "$WITHOUT_QUOTA_AGENT" = "1" ]; then
    [ -n "$ENV_FILE" ] || { echo "quota-agent 开关需要 .env（--env-file 或 deploy/stack/.env）" >&2; exit 1; }
    if [ "$WITH_QUOTA_AGENT" = "1" ]; then
        enable_quota_agent_profile "$ENV_FILE"
        say "quota-agent 形态已开启（QUOTA_AGENT_PROFILE=1，URL=$(env_file_value "$ENV_FILE" E2B_QUOTA_AGENT_URL)）"
        say "记住：agent 需要 E2B_QUOTA_AGENT_TOKEN（与 worker 同值），否则本次部署会 fail fast"
    else
        set_env_file_value "$ENV_FILE" QUOTA_AGENT_PROFILE 0
        say "quota-agent 形态已关闭（QUOTA_AGENT_PROFILE=0）；worker 仍指向 agent 时配额会降级 + WARNING"
    fi
fi
QUOTA_PROFILE_ARGS="$(quota_agent_profile_args "${ENV_FILE:-}")" || exit 1
if [ -n "$QUOTA_PROFILE_ARGS" ]; then
    say "本次 upgrade 带 --profile quota（栈内 quota-agent）"
fi

say "上传部署文件到 $REMOTE_DIR"
upload_file "$COMPOSE" "$REMOTE_DIR/docker-compose.prod.yml" "$DEPLOY_USER"
upload_file "$STACK_DIR/buildkitd.toml" "$REMOTE_DIR/buildkitd.toml" "$DEPLOY_USER"
if [ -n "$ENV_FILE" ]; then
    upload_file "$ENV_FILE" "$REMOTE_DIR/.env" "$DEPLOY_USER"
fi

say "拉取镜像（deploy 用户）"
run_as_deploy "cd '$REMOTE_DIR' && docker compose -f docker-compose.prod.yml $QUOTA_PROFILE_ARGS pull --quiet"

# E5.1: one-time shared-volume ownership migration for the non-root worker.
# The worker image now runs as uid 65534, but a volume created by an older
# (root) image is root-owned and would make the worker crash-loop. When the
# deployed worker image is non-root, chown the volume once to the image's
# uid (the control plane keeps running as root and is unaffected).
run_as_deploy "cd '$REMOTE_DIR' && WORKER_UID=\$(docker compose -f docker-compose.prod.yml run --rm -T --no-deps --entrypoint id worker-1 -u 2>/dev/null || echo 0) && if [ \"\$WORKER_UID\" != 0 ]; then OWNER=\$(docker compose -f docker-compose.prod.yml run --rm -T --no-deps --user root --entrypoint python worker-1 -c 'import os;print(os.stat(\"/var/lib/e2b-sandboxes\").st_uid)' 2>/dev/null || echo 0); if [ \"\$OWNER\" != \"\$WORKER_UID\" ]; then echo \"migrating shared volume ownership to uid \$WORKER_UID\"; docker compose -f docker-compose.prod.yml run --rm -T --no-deps --user root worker-1 chown -R \"\$WORKER_UID\":\"\$WORKER_UID\" /var/lib/e2b-sandboxes; fi; fi"

say "重建容器（deploy 用户）"
run_as_deploy "cd '$REMOTE_DIR' && docker compose -f docker-compose.prod.yml $QUOTA_PROFILE_ARGS up -d --no-build --remove-orphans"

say "等待就绪并检查容器"
run_as_deploy "sleep 8 && cd '$REMOTE_DIR' && docker compose -f docker-compose.prod.yml $QUOTA_PROFILE_ARGS ps --format 'table {{.Name}}\t{{.Status}}'"

say "校验 control-plane 网络挂载（异常时重跑 up 自愈）"
run_as_deploy "cd '$REMOTE_DIR' && docker inspect sandlock-control-plane-1 --format '{{range \$k, \$v := .NetworkSettings.Networks}}{{\$k}} {{end}}' | grep -q 'sandlock_default' || docker compose -f docker-compose.prod.yml $QUOTA_PROFILE_ARGS up -d --no-build --remove-orphans"

say "检查 worker 注册"
INTERNAL_KEY="$(remote_env_value E2B_INTERNAL_API_KEY)"
run_target "set -o pipefail; for i in 1 2 3 4 5 6; do OUT=\$(curl -s -m 5 -H 'X-Internal-Key: $INTERNAL_KEY' http://127.0.0.1:3000/internal/nodes) && [ -n \"\$OUT\" ] && break; sleep 5; done; printf '%s' \"\$OUT\" | python3 -c 'import sys,json; d=json.load(sys.stdin); [print(\"{:12} {:8} mem={}/{}\".format(n[\"nodeID\"], n[\"status\"], n[\"reservedMemoryMB\"], n[\"totalMemoryMB\"])) for n in d]'"

if [ "$SKIP_SMOKE" != "1" ]; then
    say "运行冒烟验证"
    "$SCRIPT_DIR/smoke.sh"
fi

say "升级完成"
