"""F4: the worker entry point must let ``envd_service.*`` INFO reach the log.

``uvicorn.run(log_level=...)`` only configures the ``uvicorn*`` loggers, so
before this the worker's own loggers inherited the root logger's default
WARNING level: ``own-identity instance ready ...`` (and every other INFO line on
the worker start path) was dropped, and ``E2B_LOG_LEVEL=DEBUG`` changed
nothing. ``envd_service.__main__._configure_logging`` is what the real worker
process (``python -m envd_service``) runs before serving; these tests pin its
level contract and that the own-identity message is actually emitted.
"""

from __future__ import annotations

import logging

import pytest

from envd_service.__main__ import _configure_logging
from envd_service.config import Settings as EnvdSettings

OWN_IDENTITY_LOGGER = "envd_service.executors.sandlock"


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


def test_own_identity_ready_info_line_is_emitted(restore_root_logging) -> None:
    capture = _Capture()
    logging.getLogger().addHandler(capture)

    _configure_logging(EnvdSettings(log_level="INFO"))
    logging.getLogger(OWN_IDENTITY_LOGGER).info(
        "own-identity instance ready sandbox_id=%s instance_name=%s uid=%s "
        "slot=%s channel=%s guest-uid=%s",
        "sbx_f4",
        "sbx_f4-1",
        10000,
        "slot-10000",
        "fd-handoff(pid 42)",
        "uid-0-in-userns",
    )

    assert capture.messages == [
        "own-identity instance ready sandbox_id=sbx_f4 instance_name=sbx_f4-1 "
        "uid=10000 slot=slot-10000 channel=fd-handoff(pid 42) "
        "guest-uid=uid-0-in-userns"
    ]


def test_default_level_is_info_and_drops_debug(restore_root_logging) -> None:
    level = _configure_logging(EnvdSettings(log_level="INFO"))

    assert level == logging.INFO
    assert logging.getLogger().level == logging.INFO
    logger = logging.getLogger(OWN_IDENTITY_LOGGER)
    assert logger.isEnabledFor(logging.INFO) is True
    assert logger.isEnabledFor(logging.DEBUG) is False


def test_env_log_level_debug_reaches_envd_loggers(restore_root_logging) -> None:
    level = _configure_logging(EnvdSettings(log_level="DEBUG"))

    assert level == logging.DEBUG
    assert logging.getLogger().level == logging.DEBUG
    assert logging.getLogger(OWN_IDENTITY_LOGGER).isEnabledFor(logging.DEBUG) is True


def test_unknown_level_name_falls_back_to_info(restore_root_logging) -> None:
    level = _configure_logging(EnvdSettings(log_level="not-a-level"))

    assert level == logging.INFO
    assert logging.getLogger().level == logging.INFO
