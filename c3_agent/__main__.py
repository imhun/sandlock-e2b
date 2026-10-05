"""Run the C3 per-node agent: ``python -m c3_agent`` (Task 2)."""

from __future__ import annotations

import logging
import sys

import uvicorn

from c3_agent.app import create_app
from c3_agent.config import Settings

logger = logging.getLogger(__name__)

#: Where the two fields this service decides on live. Read here rather than
#: imported from ``envd_service.config``: the agent image ships ``c3_agent`` and
#: ``gateway_common`` and deliberately nothing else
#: (``deploy/docker/Dockerfile.agent``), so the worker's self-check is not
#: importable from this side.
_STATUS_PATH = "/proc/self/status"


def _status_field(text: str, name: str) -> int | None:
    """The integer on the ``name:`` line of a ``/proc/self/status`` blob."""
    for line in text.splitlines():
        field, _, value = line.partition(":")
        if field.strip() == name:
            try:
                return int(value.strip())
            except ValueError:
                return None
    return None


def _filter_error(settings: Settings, status_text: str | None) -> str | None:
    """The named reason this container is not in the shape it was hardened with.

    Two ways, and neither is visible from the outside until the first slot
    start (measured: ``docs/security-audit/c3-agent-syscall-filter-2026-10-05.md``):

    * ``Seccomp: 0`` -- no filter at all. A dropped profile, a stray
      ``seccomp=unconfined``, or a runtime that ignored an unknown profile.
    * ``NoNewPrivs: 1`` -- a filter *is* loaded, but the kernel now ignores
      ``as_uid``'s file capabilities, so every grant fails with
      ``Operation not permitted``. That is what adding
      ``allowPrivilegeEscalation: false`` to face A would do.

    A profile itself does **not** set ``NoNewPrivs`` -- that distinction is the
    whole reason face A can be filtered at all.
    """
    text = _read_status() if status_text is None else status_text
    if text is None:
        # No /proc (a developer's macOS box): nothing here to assert.
        logger.debug("agent filter self-check skipped: no /proc/self/status")
        return None

    mode = _status_field(text, "Seccomp")
    if mode != 2:
        reason = (
            "no seccomp filter is loaded (Seccomp: {}): this container is not "
            "running under the profile it was hardened with "
            "(E2B_C3_AGENT_REQUIRE_FILTER=0 runs unfiltered on purpose)"
        ).format("?" if mode is None else mode)
    elif _status_field(text, "NoNewPrivs") == 1:
        reason = (
            "NoNewPrivs is set: the kernel will ignore as_uid's file "
            "capabilities, so every grant would fail silently "
            "(E2B_C3_AGENT_REQUIRE_FILTER=0 runs unfiltered on purpose)"
        )
    else:
        return None

    if not settings.require_filter:
        logger.warning("%s (E2B_C3_AGENT_REQUIRE_FILTER=0: continuing)", reason)
        return None
    return reason


def _read_status() -> str | None:
    try:
        with open(_STATUS_PATH, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return None


def _configure_logging(settings: Settings) -> int:
    """Make the agent's own INFO logging visible.

    The same arrangement the worker's and the control plane's entry points have
    (``envd_service.__main__._configure_logging``,
    ``control_plane.config.configure_logging``) and for the same reason:
    ``uvicorn.run`` only configures the ``uvicorn*`` loggers, while
    ``c3_agent.*`` inherits a root logger left at WARNING -- so the
    self-heal round's one greppable line ("is the sweep alive?") never appeared,
    and ``E2B_LOG_LEVEL=DEBUG`` was a no-op. ``logging.basicConfig`` is a no-op
    when the root logger already has handlers (e.g. under pytest), so the level
    is pinned explicitly as well. Returns the numeric level applied.
    """
    # An unknown name falls back to INFO (getattr's default) instead of raising
    # the way basicConfig(level="BOGUS") would.
    level = getattr(logging, str(settings.log_level).upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(levelname)s:%(name)s:%(message)s")
    logging.getLogger().setLevel(level)
    return level


def _startup_error(settings: Settings, *, status_text: str | None = None) -> str | None:
    """The one named reason this service must not start, or ``None``.

    The first two are hard, not warnings: without a token an exposed port is
    unguarded, and without its own node id the agent cannot make its only local
    decision ("addressed to me?") and would either refuse everything or (worse)
    accept instructions for any node. The third is :func:`_filter_error`, and
    it is here rather than in ``create_app`` so a refusal happens before the
    port is bound.
    """
    if not settings.token:
        return "E2B_C3_AGENT_TOKEN is required; refusing to start without auth"
    if not settings.node_id:
        return (
            "E2B_C3_AGENT_NODE_ID (or E2B_NODE_ID) is required; the agent "
            "must know which node it is"
        )
    return _filter_error(settings, status_text)


def main() -> None:
    settings = Settings()
    _configure_logging(settings)
    error = _startup_error(settings)
    if error is not None:
        sys.exit(error)
    uvicorn.run(
        create_app(settings=settings),
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
