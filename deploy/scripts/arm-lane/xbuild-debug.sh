#!/bin/sh
set -eu
# The repo root and the *guest* mirror path. The scripts live in
# deploy/scripts/arm-lane/, so the root is three levels up; the guest mirror
# keeps the host's absolute path by default purely so the two sides read the
# same strings in logs and `cd` args (override with ARM_LANE_GUEST_MIRROR).
repo="$(cd "$(dirname "$0")/../../.." && pwd)"
mirror="${ARM_LANE_GUEST_MIRROR:-$repo}"
exec docker run --rm \
  -v "$repo/third_party/sandlock":/src \
  -v "$repo/tmp/arm-lane/target2":/tmp/target-aarch64 \
  -w /src \
  -e CARGO_TARGET_DIR=/tmp/target-aarch64 \
  -e ZIG_TARGET=aarch64-linux-gnu.2.34 \
  -e CC_aarch64_unknown_linux_gnu=zigcc \
  -e CC_x86_64_unknown_linux_gnu=zigcc \
  sandlock-zig-builder:local "$@"
