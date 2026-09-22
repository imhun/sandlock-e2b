#!/usr/bin/env bash
# Rebuild the LOCAL test-runner image (`e2b-sandlock-test:latest`).
#
# Why this is a script and not a line in a doc: the image bakes in
# `wheels/fork/*.whl`, and nothing rebuilds it when those wheels change. The
# 2026-09-21 cost of that: the image was four days older than the wheels, so
# every test that builds worker settings died on
#   TypeError: Sandbox.__init__() got an unexpected keyword argument 'max_file_size'
# -- 31 failures in `tests/unit` alone, and they looked like code regressions
# (`docs/build-test-deploy-pitfalls.md` §B7).
#
# The image is local-only (never pushed to ACR: it is a test lane, not a
# deployment artifact), so this is deliberately *not* part of
# build-and-push.sh -- which points here instead.
#
# Usage: ./deploy/scripts/build-test-image.sh            # rebuild + summary
#        IMAGE=my-test:dev ./deploy/scripts/build-test-image.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
IMAGE="${IMAGE:-e2b-sandlock-test:latest}"
WHEELS="$REPO_ROOT/wheels/fork"

command -v docker >/dev/null || { echo "缺少 docker" >&2; exit 1; }

if ! ls "$WHEELS"/*.whl >/dev/null 2>&1; then
    echo "缺少 $WHEELS/*.whl：先跑 ./deploy/scripts/build-sandlock-wheels.sh" >&2
    exit 1
fi

echo "==> 用当前 wheels 重建 $IMAGE"
echo "    wheels: $(ls -1 "$WHEELS"/*.whl | wc -l | tr -d ' ') 个，最新的是 $(ls -t "$WHEELS"/*.whl | head -1 | xargs basename)"

docker build \
    -f "$REPO_ROOT/deploy/docker/Dockerfile.test-runner" \
    -t "$IMAGE" \
    "$REPO_ROOT"

# The whole point is that the wheel inside matches the one on disk: compare the
# installed version against the wheel's filename rather than trusting the build.
version="$(cd "$WHEELS" && ls -1 *.whl | head -1 | sed -E 's/^sandlock-([^-]+)-.*/\1/')"
installed="$(docker run --rm --entrypoint python3 "$IMAGE" -c 'import importlib.metadata as m; print(m.version("sandlock"))' 2>/dev/null || echo unknown)"
echo "==> 镜像内 sandlock=$installed（wheel=$version）"
if [ "$installed" != "$version" ]; then
    echo "警告：镜像里的版本与 wheels/ 不一致，检查 wheel 是否重建过" >&2
fi
echo "==> 完成：$IMAGE"
echo "    跑套件：./deploy/scripts/test-prod-shaped.sh（两阶段；见 pitfalls §B5-B9 的环境要求）"
