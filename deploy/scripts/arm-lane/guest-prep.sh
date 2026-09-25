#!/bin/sh
# The root prep the test container's entrypoint does before dropping to an
# unprivileged uid. `net_fixture.rs` has two modes and this is what selects the
# unprivileged one: the fixture *reads* a pre-seeded /etc/hosts mapping
# instead of adding a 198.18.0.x address to lo itself, so the whole network
# family runs without CAP_NET_ADMIN. Without it the family fails with
# "no free 198.18.0.x address ... run the test container entrypoint (root
# prep) so the unprivileged fixtures exist" -- measured on the aarch64 lane
# (2026-09-24), 42 of core_integ's failures were exactly that.
set -eu
mirror="${ARM_LANE_GUEST_MIRROR:-/Users/polus/project/ai/sandlock-e2b}"
# ^ the guest's own copy of the repo (this file runs *inside* the guest,
#   streamed over stdin, so the host cannot hand it anything).

# Two callers, two privilege shapes: the lane runs this as the unprivileged
# guest user (hence the `sudo` below), and `deploy/scripts/fork-gate.sh` runs it
# as root inside the test container, where `sudo` is not even installed. A
# pass-through keeps one definition of the prep instead of two that drift.
if [ "$(id -u)" = 0 ]; then
    sudo() { "$@"; }
fi

# `test_policy_fn::test_instance_exec_after_threaded_peer_succeeds` execs
# `/usr/local/bin/python3` -- the layout of the lane image (python:3.11-slim
# keeps its interpreter there). Ubuntu's is /usr/bin/python3, so without this
# the helper exits 127 and the test dies on "threaded helper never reported
# ready" (measured 2026-09-24).
#
# **Only when the image needs it.** The local gate image already has
# `/usr/local/bin/python3`, and there `/usr/bin/python3` is a *link to it* -- so
# pointing one at the other creates a cycle (`python3 -> python3`), and two
# unrelated suites then fail with `Too many levels of symbolic links` (measured
# 2026-09-25 on the local gate run). Resolving to a working interpreter is the
# condition, not the mere existence of a path.
if [ ! -x /usr/local/bin/python3 ] && [ -x /usr/bin/python3 ]; then
    sudo ln -sfn /usr/bin/python3 /usr/local/bin/python3
fi

# The per-sandbox DNS gateway binds 127.0.1.x:53: resolv.conf cannot express a
# port, so the wildcard gateway must use the low port 53. The suites' default is
# the *shared-netns* shape (`net_isolation` defaults false), and this lane runs
# them as the unprivileged guest user, so the one-time window below is what
# makes every shared-netns wildcard case reachable here. The deployed E2B shape
# sets E2B_ENABLE_NET_ISOLATION=true, where the gateway binds inside the
# sandbox's own netns and no sysctl is involved -- hence no window in the
# manifests. Measured on this lane as uid 501 (2026-09-24):
#
#   - with the stock `ip_unprivileged_port_start=1024`:
#       bind 127.0.1.9:53                       -> EACCES
#       integration ... test_wildcard_shared    -> `bind DNS gateway: Permission
#                                                  denied (os error 13)` (both cases)
#   - with the window open: the suite is 551/0 (s5-arm-core-integ-final.log).
#   - the CAP_NET_BIND_SERVICE alternative (`setcap cap_net_bind_service+ep` on
#     the test binary) is NOT usable: the low-port error goes away, but the suite
#     then dies at `pidfd_getfd: Operation not permitted (os error 1)`, because a
#     file-capability exec makes the process non-dumpable and it can no longer
#     ptrace the sandbox child it just forked.
# `sysctl` is not in every test image (the local gate image has none), so fall
# back to the file it writes: same knob, same effect, one less dependency.
if command -v sysctl >/dev/null 2>&1; then
    sudo sysctl -qw net.ipv4.ip_unprivileged_port_start=0
else
    echo 0 | sudo tee /proc/sys/net/ipv4/ip_unprivileged_port_start >/dev/null
fi

# 198.18.0.0/24 is the benchmarking range the SSRF guard deliberately allows.
for a in 99 100 101 102 103; do
    sudo ip addr add "198.18.0.$a/32" dev lo 2>/dev/null || true
done
for spec in "198.18.0.99 conn.example.com" "198.18.0.100 api.egress.test"; do
    addr=${spec%% *}
    host=${spec##* }
    grep -q "$host" /etc/hosts 2>/dev/null || echo "$addr $host" | sudo tee -a /etc/hosts >/dev/null
done

echo "prep: $(ip -4 addr show dev lo | grep -c 198.18) loopback fixture addresses, /etc/hosts:"
grep -E "conn\.example\.com|api\.egress\.test" /etc/hosts || true
