#!/bin/sh
# E2B-side entry point for the sandlock fork wheels that the service images
# install (`wheels/fork/`, git-ignored build output).
#
# The wheel pipeline itself lives in the fork -- `python/build-wheels.sh` plus
# `python/wheel-builder/` -- because a release wheel has to carry more than the
# FFI extension: F2b.5 cross-builds the `sandlock-supervise` release binary in
# the same builder, injects it as `sandlock/bin/sandlock-supervise` after
# auditwheel repair (RECORD rewritten, exec bit restored), and writes the
# HEAD-pinned `SHA256SUMS.supervise` manifest that `python/verify-wheel.sh`
# checks against both copies. So this script only delegates, with the artifacts
# landing here.
#
# It used to run its own buildx recipe. That recipe was a near-copy of the
# fork's (`third_party/sandlock-wheel-builder/`, deleted 2026-09-30 -- its
# `cargo-config.toml`/`zigcc` were byte-identical duplicates, so the only thing
# it could still do was send the next reader to the wrong file), and its output
# was *worse*: it predates F2b.5 and produces wheels *without* the supervise
# binary -- with exit code 0. Measured 2026-09-09: such a wheel installs fine but
# `sandlock/bin/sandlock-supervise` is missing, so route B quietly refuses to
# start a slot and the worker keeps the in-process mediator (i.e. silently loses
# the T5 ownership fix). Anything that can half-build the release must not be a
# second code path; the fork's script also fails loudly if supervise is absent.
# `tests/unit/test_one_sandlock_wheel_recipe.py` pins that there is exactly one.
#
# Usage:
#   ./deploy/scripts/build-sandlock-wheels.sh
#
# Environment (passed through to the fork's script):
#   PLATFORM / BUILDER / BASE_IMAGE   see third_party/sandlock/python/build-wheels.sh
set -eu
cd "$(dirname "$0")/../.."                       # repo root
FORK="third_party/sandlock"

if [ ! -f "$FORK/python/build-wheels.sh" ]; then
    echo "build-sandlock-wheels: $FORK is not populated." >&2
    echo "  run: git submodule update --init --recursive" >&2
    exit 1
fi

# Build output stays inside the fork (`third_party/sandlock/`): the staging
# context used to be `<repo>/tmp/wheel-context`, which is exactly the
# "root directory" shape we do not want -- the fork's `.gitignore` owns
# `tmp/`, the E2B repo root does not (`docs/build-test-deploy-pitfalls.md` A7).
mkdir -p wheels/fork
OUT_DIR="$PWD/wheels/fork" \
CONTEXT_DIR="${CONTEXT_DIR:-$PWD/third_party/sandlock/tmp/wheel-context}" \
    sh "$FORK/python/build-wheels.sh"

echo "==> E2B wheel inputs refreshed in wheels/fork/:"
ls -lh wheels/fork/*.whl
ls -l  wheels/fork/supervise/*/sandlock-supervise
ls -l  wheels/fork/restore-stub/*/restore-stub
if [ ! -f wheels/fork/supervise/x86_64/sandlock-supervise ] \
   || [ ! -f wheels/fork/supervise/aarch64/sandlock-supervise ]; then
    echo "build-sandlock-wheels: supervise binaries missing from wheels/fork/" >&2
    exit 1
fi
# F2b.5b: the restore stub has to travel with the wheel too. Without it the
# engine refuses every restore with "restore-stub was not built" -- measured on
# the deployment 2026-09-25, where the stub existed only in the build
# container's target dir. A wheel build that loses it fails here, not three
# steps later in a resume.
if [ ! -f wheels/fork/restore-stub/x86_64/restore-stub ] \
   || [ ! -f wheels/fork/restore-stub/aarch64/restore-stub ]; then
    echo "build-sandlock-wheels: restore-stub missing from wheels/fork/" >&2
    exit 1
fi
