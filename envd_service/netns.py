"""Worker-side plumbing for per-sandbox network namespaces.

Each sandbox's veth subnet lives in ``10.200.0.0/16`` (see the sandlock
fork's pool allocator). For the sandbox's traffic to reach the internet the
worker must (a) forward between its own netns and the veth links and (b)
source-NAT the veth pool, because the container's own MASQUERADE rule only
covers the docker bridge subnet.

Everything here is best-effort and idempotent: it runs once at worker
startup when ``E2B_ENABLE_NETNS`` is on. Failures log a warning — a worker
that cannot set up plumbing still serves sandboxes, they just cannot egress.
"""

from __future__ import annotations

import logging
import shutil
import subprocess

logger = logging.getLogger(__name__)

# The veth pool must match the sandlock fork's allocator (10.200.0.0/16).
VETH_POOL = "10.200.0.0/16"


def ensure_worker_netns_plumbing() -> None:
    """Enable ip_forward and NAT the veth pool inside the worker netns."""
    sysctl = shutil.which("sysctl")
    iptables = shutil.which("iptables") or shutil.which("iptables-legacy")
    if sysctl is None:
        logger.warning("netns: sysctl not found; cannot enable ip_forward")
    else:
        _run([sysctl, "-w", "net.ipv4.ip_forward=1"])
    if iptables is None:
        logger.warning(
            "netns: iptables not found; veth pool %s will not be NATed "
            "(sandboxes can resolve DNS but cannot reach the internet)",
            VETH_POOL,
        )
        return
    # Idempotent: check first, add only if absent.
    check = _run([iptables, "-t", "nat", "-C", "POSTROUTING", "-s", VETH_POOL, "-j", "MASQUERADE"])
    if check is False:
        _run([iptables, "-t", "nat", "-A", "POSTROUTING", "-s", VETH_POOL, "-j", "MASQUERADE"])


def _run(argv: list[str]) -> bool:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.warning("netns: %s failed: %s", " ".join(argv), e)
        return False
    if proc.returncode != 0:
        logger.warning(
            "netns: %s exited %d: %s",
            " ".join(argv),
            proc.returncode,
            proc.stderr.strip(),
        )
        return False
    return True
