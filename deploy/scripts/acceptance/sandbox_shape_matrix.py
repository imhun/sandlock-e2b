#!/usr/bin/env python3
"""Which sandbox shapes can a deployment's worker pod actually build?

Runs inside a worker pod (or any container with the sandlock wheel) as the
worker's own uid and tries each shape once, printing the core's own refusal
text when one fails. Cheap enough to run before flipping a switch like
`E2B_ENABLE_NET_ISOLATION` -- the fleet's worker seccomp profile decides this
answer, and the answer is not visible from the manifests.

Usage:
    export KUBECONFIG="$PWD/tmp/k0s/kubeconfig"
    kubectl -n sandlock exec -i e2b-worker-0 -c worker -- python3 - \
        < deploy/scripts/acceptance/sandbox_shape_matrix.py

Measured on a fleet worker pod (2026-10-08, arm64 / Rocky 6.12,
`sandlock-worker.json` with `defaultAction: SCMP_ACT_ERRNO` and 404 allowed
syscall names):

    plain                      OK
    pid_ns                     OK
    net_isolation              FAIL  (unshare is not in the profile's allowlist)
    net_isolation+pid_ns       OK    (the netns rides in the one clone3 call)
    net_isolation+map          FAIL  (same unshare refusal)
    net_isolation+pid_ns+map   OK

So under this profile `net_isolation` needs `pid_ns` as well (the direct check
`unshare(CLONE_NEWUSER|NEWNS|NEWNET)` answers EPERM). The fleet itself is
consistent -- its worker env sets `E2B_ENABLE_NET_ISOLATION=true` **and**
`E2B_PID_NS=true` -- but any lane that narrows to net_isolation alone in this
image fails every create with `sandlock_create failed`, whose text carries no
further reason.
"""

import os
import sys

from sandlock import Sandbox

BASE = dict(
    fs_readable=["/usr", "/lib", "/lib64", "/bin", "/etc", "/proc", "/dev"],
    fs_writable=["/tmp"],
)
PROBE = ["python3", "-c", "print('probe-ok')"]

CASES = {
    "plain": {},
    "pid_ns": dict(pid_ns=True),
    "net_isolation": dict(net_isolation=True),
    "net_isolation+pid_ns": dict(net_isolation=True, pid_ns=True),
    "net_isolation+map": dict(
        net_isolation=True,
        port_mappings={50021: 8080},
        net_allow_bind=[8080],
    ),
    "net_isolation+pid_ns+map": dict(
        net_isolation=True,
        pid_ns=True,
        port_mappings={50021: 8080},
        net_allow_bind=[8080],
    ),
}


def main():
    print(f"uid={os.getuid()} python={sys.version.split()[0]}", flush=True)
    for name, extra in CASES.items():
        sb = Sandbox(**BASE, **extra)
        try:
            result = sb.run(PROBE, timeout=25)
            out = (getattr(result, "stdout", b"") or b"").decode("utf-8", "replace")
            err = (getattr(result, "stderr", b"") or b"").decode("utf-8", "replace")
            detail = f"out={out.strip()!r}"
            if err.strip():
                detail += f" err={err.strip()[-300:]!r}"
            if getattr(result, "error", None):
                detail += f" error={result.error!r}"
            print(
                f"{name:26s} success={result.success} exit={getattr(result, 'exit_code', '?')} {detail}",
                flush=True,
            )
        except Exception as exc:  # noqa: BLE001 - the core's text is the point
            print(f"{name:26s} FAIL  {type(exc).__name__}: {exc}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
