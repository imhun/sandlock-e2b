#!/bin/sh
# Task 13: is `test_a_refused_tree_is_parked_...` load/timing flaky, or does it
# only fail in the synthesized-root shape?
#
# It failed once in the whole-suite synth+real-root lane
# (`tmp/k0s/n16-gateB-synth-realroot.log`) with an extra WARNING in the caplog:
# the test replaces `agent_mod.time` with a `SimpleNamespace(time=...)`, so the
# node agent's heartbeat tick raises `AttributeError: ... no attribute
# 'monotonic'` and logs `node agent heartbeat failed` inside the strict
# `_agent_lines(caplog) == [...]` window. Five repetitions per state, same
# invocation shape as the gate lanes.
set -eu
cd /Users/polus/project/ai/sandlock-e2b
outdir="tmp/k0s/task13/noisy-teardown"
mkdir -p "$outdir"
TARGET="tests/contract/test_teardown_failure_semantics.py -k refused_tree_is_parked"
for state in 0 1; do
    for i in 1 2 3 4 5; do
        log="$outdir/state$state-run$i.log"
        E2B_TEST_IMAGE=e2b-sandlock-test:task12cur \
            sh tmp/k0s/gateB-pure-rootfs.sh "$state" "$log" $TARGET || true
        printf 'state=%s run=%s  %s\n' "$state" "$i" "$(tail -1 "$log")"
    done
done
