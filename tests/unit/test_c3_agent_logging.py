"""The agent entry point must let ``deploy.c3_agent.*`` INFO reach the log.

The worker (``envd_service.__main__._configure_logging``) and the control plane
(``control_plane.config.configure_logging``) both raise the root logger before
serving, for the same reason: ``uvicorn.run(log_level=...)`` only configures the
``uvicorn*`` loggers, so every module logger inherits the root logger's default
WARNING level. The agent's entry point was the one that did not, and the line
that made it visible is Task 6's: the sweep's per-round
``c3-agent inventory: node=… scanned=… protected=… orphans=… removed=…`` is INFO
while its refusals are WARNING -- so "is the self-heal alive?" had no greppable
answer in exactly the (healthy) case the operator asks about, and
``E2B_LOG_LEVEL=DEBUG`` was a no-op.

Measured on the live multinode stack 2026-09-30: with the shared record store in
place the round reached the control plane (200 on
``POST /internal/nodes/c3-agent/agent/inventory``) but the agent printed nothing
about it.
"""

from __future__ import annotations

import logging

import pytest

from deploy.c3_agent.__main__ import _configure_logging
from deploy.c3_agent.config import Settings as AgentSettings

SCAN_LOGGER = "deploy.c3_agent.scan"
ROUND_LINE = (
    "c3-agent inventory: node=%s scanned=%d protected=%d orphans=%d removed=%d "
    "failed=%d deferred=-"
)


@pytest.fixture()
def restore_root_logging():
    """Undo the global root level/handlers the entry point installs."""
    root = logging.getLogger()
    level = root.level
    handlers = list(root.handlers)
    yield
    root.setLevel(level)
    root.handlers[:] = handlers


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def test_the_self_heal_round_line_is_emitted(restore_root_logging) -> None:
    capture = _Capture()
    logging.getLogger().addHandler(capture)

    _configure_logging(AgentSettings(log_level="INFO"))
    logging.getLogger(SCAN_LOGGER).info(
        ROUND_LINE, "c3-agent", 4, 4, 0, 0, 0
    )

    assert capture.messages == [
        "c3-agent inventory: node=c3-agent scanned=4 protected=4 orphans=0 "
        "removed=0 failed=0 deferred=-"
    ]


def test_the_default_level_is_info_and_drops_debug(restore_root_logging) -> None:
    assert _configure_logging(AgentSettings()) == logging.INFO
    assert logging.getLogger().level == logging.INFO
    logger = logging.getLogger(SCAN_LOGGER)
    assert logger.isEnabledFor(logging.INFO) is True
    assert logger.isEnabledFor(logging.DEBUG) is False


def test_env_log_level_debug_reaches_the_agents_loggers(
    restore_root_logging, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("E2B_LOG_LEVEL", "DEBUG")
    assert AgentSettings().log_level == "DEBUG"

    assert _configure_logging(AgentSettings(log_level="DEBUG")) == logging.DEBUG
    assert logging.getLogger(SCAN_LOGGER).isEnabledFor(logging.DEBUG) is True


def test_unknown_level_name_falls_back_to_info(restore_root_logging) -> None:
    assert _configure_logging(AgentSettings(log_level="not-a-level")) == logging.INFO
    assert logging.getLogger().level == logging.INFO
