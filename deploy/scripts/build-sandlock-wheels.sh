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
# It used to run its own buildx recipe (`third_party/sandlock-wheel-builder/`),
# which predates F2b.5 and produces wheels *without* the supervise binary -- and
# did so with exit code 0. Measured 2026-09-09: such a wheel installs fine but
# `sandlock/bin/sandlock-supervise` is missing, so route B quietly refuses to
# start a slot and the worker keeps the in-process mediator (i.e. silently loses
# the T5 ownership fix). Anything that can half-build the release must not be a
# second code path; the fork's script also fails loudly if supervise is absent.
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

mkdir -p wheels/fork tmp
OUT_DIR="$PWD/wheels/fork" \
CONTEXT_DIR="${CONTEXT_DIR:-$PWD/tmp/wheel-context}" \
    sh "$FORK/python/build-wheels.sh"

echo "==> E2B wheel inputs refreshed in wheels/fork/:"
ls -lh wheels/fork/*.whl
ls -l  wheels/fork/supervise/*/sandlock-supervise
if [ ! -f wheels/fork/supervise/x86_64/sandlock-supervise ] \
   || [ ! -f wheels/fork/supervise/aarch64/sandlock-supervise ]; then
    echo "build-sandlock-wheels: supervise binaries missing from wheels/fork/" >&2
    exit 1
fi
