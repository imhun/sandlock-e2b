"""Per-create stage timing, off by default.

``POST /agent/sandboxes`` on a worker is a *chain of round trips* -- two NFS
mkdirs, one control-plane round trip for the ownership hand-over, one atomic
record write, a reservation-marker drop -- and each of those costs a few to
tens of milliseconds on the deployment's NAS. Which one dominates moves with
the NAS's mood, so the only useful answer is a measurement taken on the live
fleet rather than an estimate from the code.

This is that measurement's switch, and it follows the shape the worker already
uses for the disk scanner (``NodeAgent._disk_trace``): **one env var turns one
INFO line per stage**, so an operator can watch a single create without turning
the worker's log level to DEBUG (which would drown it in per-request httpx
lines).

    kubectl -n sandlock set env deploy/e2b-worker E2B_CREATE_TRACE=1
    kubectl -n sandlock logs e2b-worker-0 --since=2m | grep "create trace:"
    kubectl -n sandlock set env deploy/e2b-worker E2B_CREATE_TRACE-

Stages are named after the *cost centre*, not the function: ``record`` (the
``_runtime/<id>/sandbox.json`` write), ``commit`` (dropping the pool's
reservation marker), ``fileop:chown-workspace`` (the control-plane round trip
that hands the tree to the sandbox's uid), ``prime`` and ``total``. A create
whose ``total`` is much larger than the named stages is telling you the
remainder is in the unnamed file work (the two ``mkdir``s and the tree walk).
"""

from __future__ import annotations

import logging
import os
import time

logger = logging.getLogger(__name__)

#: Turns the per-stage lines on. Read per call (not cached at import) so a
#: deployment can flip it with ``kubectl set env`` and see the very next
#: create without a code change.
TRACE_ENV = "E2B_CREATE_TRACE"


def enabled() -> bool:
    """Whether this process was asked to trace creates."""
    value = os.getenv(TRACE_ENV, "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def stage(name: str, sandbox_id: str | None, started: float) -> None:
    """Log one stage's duration, if tracing is on.

    ``started`` is a ``time.monotonic()`` reading taken *before* the work, so
    the caller can wrap any block without calling into this module first --
    and so a disabled trace costs one ``os.getenv`` and nothing else.
    """
    if not enabled():
        return
    logger.info(
        "create trace: stage=%s sandbox=%s ms=%.1f",
        name,
        sandbox_id or "-",
        (time.monotonic() - started) * 1000.0,
    )
