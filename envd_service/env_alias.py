"""Read the new environment name, falling back to the old one for one version.

A rename that moves an environment variable cannot land in the manifests and in
the running processes at the same instant, so for one version the reader
accepts both spellings and resolves the ambiguity in a fixed, one-way
direction:

* the **new** name wins whenever it holds a non-empty value -- even when the
  old name is also set and disagrees. The migration flows one way; a stale line
  an operator forgot to delete must never drag a deployment back;
* the old name is honoured when the new one is absent or empty, and its use is
  **warned exactly once per process** (keyed by the legacy name, so two
  different legacy names each get their one line). Once, not once-per-read: a
  hot path must not flood the log. A warning, not silence: the deprecation has
  to be visible or nobody removes the line.

Both branches name *both* spellings -- the warning's job is to say which line
to change.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

#: Legacy names already warned about, process-wide. Keyed by the *legacy* name
#: so an aliased pair warns once and a second pair warns once of its own.
_warned: set[str] = set()


def reset_warnings() -> None:
    """Forget which legacy names were warned (tests; a re-read of settings)."""
    _warned.clear()


def _warn_once(legacy: str, name: str, detail: str) -> None:
    if legacy in _warned:
        return
    _warned.add(legacy)
    logger.warning(
        "%s is deprecated in favour of %s: %s",
        legacy,
        name,
        detail,
    )


def read(name: str, *, legacy: str, default: str = "") -> str:
    """The value of ``name``, or its ``legacy`` spelling, or ``default``.

    ``name`` wins whenever it is set to a non-empty value. When both are set
    that precedence is warned about (naming both). The legacy name is used --
    and warned about, once -- only when the new one is absent or empty.
    """
    value = os.getenv(name)
    legacy_value = os.getenv(legacy)
    if value is not None and value.strip():
        if legacy_value is not None and legacy_value.strip():
            _warn_once(
                legacy,
                name,
                "both are set; the new name wins (delete the old line)",
            )
        return value
    if legacy_value is not None and legacy_value.strip():
        _warn_once(
            legacy,
            name,
            "the old name is still read, but it will be removed "
            "(rename the line to the new name)",
        )
        return legacy_value
    return default
