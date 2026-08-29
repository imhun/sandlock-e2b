#!/bin/sh
# Build the LD_PRELOAD egress proxy library (Linux ELF, glibc).
set -eu
DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
OUT="${1:-$DIR/libegress_proxy.so}"
cc -shared -fPIC -O2 -Wall -Wextra -o "$OUT" "$DIR/libegress_proxy.c" -ldl
echo "built $OUT"
