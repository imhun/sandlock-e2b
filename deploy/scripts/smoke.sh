#!/usr/bin/env bash
# Run the multi-node and deployment smoke tests against the deployed stack.
# Tests run on the target itself against 127.0.0.1.
#
# Usage: ./deploy/scripts/smoke.sh

set -euo pipefail
. "$(cd "$(dirname "$0")" && pwd)/lib/helpers.sh"

REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

say "上传冒烟脚本"
upload_file "$REPO_DIR/scripts/multinode_smoke.py" "$REMOTE_DIR/multinode_smoke.py" "$DEPLOY_USER"
upload_file "$REPO_DIR/scripts/deployment_smoke.py" "$REMOTE_DIR/deployment_smoke.py" "$DEPLOY_USER"

say "准备 venv（e2b SDK）"
run_as_deploy '
set -e
if [ ! -x /opt/sandlock/venv/bin/python ]; then
    python3 -m venv /opt/sandlock/venv
fi
/opt/sandlock/venv/bin/python -c "import e2b, httpx, mcp, httpx2" 2>/dev/null || \
    /opt/sandlock/venv/bin/pip install -q -i https://pypi.tuna.tsinghua.edu.cn/simple e2b==2.46.0 httpx mcp httpx2
'

API_KEY="$(remote_env_value E2B_API_KEYS)"
INTERNAL_KEY="$(remote_env_value E2B_INTERNAL_API_KEY)"

say "多节点冒烟（multinode_smoke.py）"
run_as_deploy "cd '$REMOTE_DIR' && E2B_API_URL=http://127.0.0.1:3000 E2B_SANDBOX_URL=http://127.0.0.1:3000 E2B_API_KEY='$API_KEY' E2B_INTERNAL_API_KEY='$INTERNAL_KEY' venv/bin/python multinode_smoke.py"

say "部署级冒烟（deployment_smoke.py）"
run_as_deploy "cd '$REMOTE_DIR' && E2B_API_URL=http://127.0.0.1:3000 E2B_SANDBOX_URL=http://127.0.0.1:3000 E2B_API_KEY='$API_KEY' E2B_INTERNAL_API_KEY='$INTERNAL_KEY' venv/bin/python deployment_smoke.py"

say "冒烟全部通过"
