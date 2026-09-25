#!/bin/sh
# Cross-build sandlock-core binaries for aarch64 with the repo's zig builder
# image. The image's cargo config points BOTH targets at zigcc, and zigcc reads
# one global ZIG_TARGET, so host units (build scripts / proc macros) would be
# linked as aarch64. Pin the host linker back to the system cc and leave zigcc
# for the aarch64 target only.
#
# Why the target root is /var/tmp/aarch64-target and NOT /tmp: cargo bakes
# `CARGO_TARGET_TMPDIR` (= <target dir>/<triple>[/<profile>]/tmp) into the test
# binaries as a compile-time path, and the sandlock integration policy grants
# `fs_write("/tmp")` while the *real* `/tmp` is virtualized. A test that keeps a
# fixture outside those grants -- the named-unix-socket gate family picks
# exactly this spot, "a real host mount visible in the sandbox, unlike the
# virtualized /tmp" -- then lands *inside* the write grant instead, and the gate
# is bypassed: measured 2026-09-24, five test_landlock named-unix denials came
# back "CONNECTED"/"SENT" on the aarch64 lane while the container lane (target
# root = <repo>/target-linux) is green. /var/tmp is 1777 like /tmp, so the
# container's dropped user can still write there.
set -eu
# The repo root and the *guest* mirror path. The scripts live in
# deploy/scripts/arm-lane/, so the root is three levels up; the guest mirror
# keeps the host's absolute path by default purely so the two sides read the
# same strings in logs and `cd` args (override with ARM_LANE_GUEST_MIRROR).
repo="$(cd "$(dirname "$0")/../../.." && pwd)"
mirror="${ARM_LANE_GUEST_MIRROR:-$repo}"
exec docker run --rm \
  -v "$repo/third_party/sandlock":/src \
  -v "$repo/tmp/arm-lane/target":/var/tmp/aarch64-target \
  -w /src \
  -e CARGO_TARGET_DIR=/var/tmp/aarch64-target \
  -e ZIG_TARGET=aarch64-linux-gnu.2.34 \
  -e CC_aarch64_unknown_linux_gnu=zigcc \
  -e CC_x86_64_unknown_linux_gnu=cc \
  -e CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_LINKER=cc \
  sandlock-zig-builder:local "$@"
