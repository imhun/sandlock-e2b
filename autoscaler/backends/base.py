"""Scale backend contract."""

from __future__ import annotations

from typing import Protocol


class ScaleBackend(Protocol):
    def current(self) -> int:
        """Number of worker instances currently running."""
        ...

    def has_node(self, node_id: str) -> bool:
        """Whether a specific worker instance still exists."""
        ...

    def retire_victim(self, candidates: list[str]) -> str | None:
        """Which of these idle nodes a scale-down would *actually* retire.

        The decision belongs here because it is the workload that decides it,
        not the policy: shrinking a Deployment can be aimed at a chosen pod
        (the ReplicaSet controller honours pod-deletion-cost), while shrinking
        a StatefulSet always takes the highest ordinal -- so with that kind any
        other pod would die, which is not the pod the loop drained (and may be
        running live sandboxes).

        ``None`` means "not this tick": none of the candidates is what this
        backend can retire right now, so shrinking would take a node the loop
        did not choose. The loop then waits rather than gambling (N51).
        """
        ...

    def scale_to(self, replicas: int) -> None:
        """Grow the pool to ``replicas`` instances (never shrinks)."""
        ...

    def remove_node(self, node_id: str) -> None:
        """Terminate one specific (already drained) worker instance."""
        ...
