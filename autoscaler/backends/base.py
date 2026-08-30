"""Scale backend contract."""

from __future__ import annotations

from typing import Protocol


class ScaleBackend(Protocol):
    def current(self) -> int:
        """Number of worker instances currently running."""
        ...

    def scale_to(self, replicas: int) -> None:
        """Grow the pool to ``replicas`` instances (never shrinks)."""
        ...

    def remove_node(self, node_id: str) -> None:
        """Terminate one specific (already drained) worker instance."""
        ...
