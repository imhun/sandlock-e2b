"""Where the loop's marks live: outside it, so replicas and restarts agree.

The loop has exactly three pieces of memory -- when it last scaled up, when it
last scaled down, and which node it is currently draining -- and all three are
statements about the *fleet*: "someone grew the pool 10 s ago", "node n1 is on
its way out". Since 2026-09-30 the loop runs inside the control plane (the k8s
path), which makes two things routine that used to be impossible:

* **two replicas** (``deploy/k8s/control-plane.yaml`` runs two), each with its
  own loop object, taking turns on the tick claim; and
* **restarts on every rollout** (``maxUnavailable: 1``), which discard anything
  the loop was holding in memory.

Kept on the instance, that memory would be wrong in both directions: the
replica that has never ticked would see no cooldown and scale a second time,
and a restarted loop would forget the drain it started and begin draining a
second node while the first still holds live sandboxes. So the marks are read
and written around each tick through this store.

Without Redis there is one process and :class:`InMemoryLoopState` is the whole
fleet -- the same "one process is the winner" posture ``try_claim`` takes. The
two are deliberately independent: the claim decides *who acts this interval*,
the marks decide *what the fleet has already done*.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

logger = logging.getLogger(__name__)

#: The Redis hash the marks live in. One key, three fields: the loop is
#: single-flight per interval, so the read-modify-write of a tick has one
#: writer and last-writer-wins per field is enough.
STATE_KEY = "e2b:autoscaler:state"


@dataclass
class LoopMarks:
    """What the loop has to remember between ticks (and between replicas)."""

    #: Epoch seconds, never a monotonic reading: the marks are compared by
    #: *other processes*, and a monotonic clock has no common origin.
    last_scale_up: float = float("-inf")
    last_scale_down: float = float("-inf")
    draining_node_id: str | None = None


class LoopState(Protocol):
    def read(self) -> LoopMarks: ...

    def write(self, marks: LoopMarks) -> None: ...


@dataclass
class InMemoryLoopState:
    """The one-process shape (no shared store, or tests)."""

    _marks: LoopMarks = field(default_factory=LoopMarks)

    def read(self) -> LoopMarks:
        return replace(self._marks)

    def write(self, marks: LoopMarks) -> None:
        self._marks = replace(marks)


class RedisLoopState:
    """The merged shape's store: two replicas, one set of marks.

    An unreachable store degrades instead of failing the tick (the posture
    ``try_claim`` already takes): a read answers "no marks", a write is
    reported and dropped. That is a real loss -- the fleet can scale twice
    inside one cooldown while the store is down -- but the alternative, a loop
    that stops reconciling during a Redis outage, loses capacity instead.
    """

    def __init__(self, client: Any, key: str = STATE_KEY) -> None:
        self._client = client
        self._key = key

    def read(self) -> LoopMarks:
        try:
            raw = self._client.hgetall(self._key) or {}
        except Exception:
            logger.warning(
                "autoscaler state read failed; ticking with no marks "
                "(a cooldown may be forgotten this round)",
                exc_info=True,
            )
            return LoopMarks()
        return LoopMarks(
            last_scale_up=_epoch(raw.get("last_scale_up")),
            last_scale_down=_epoch(raw.get("last_scale_down")),
            draining_node_id=_text(raw.get("draining_node_id")) or None,
        )

    def write(self, marks: LoopMarks) -> None:
        try:
            self._client.hset(
                self._key,
                mapping={
                    "last_scale_up": repr(marks.last_scale_up),
                    "last_scale_down": repr(marks.last_scale_down),
                    "draining_node_id": marks.draining_node_id or "",
                },
            )
        except Exception:
            logger.warning(
                "autoscaler state write failed; the next replica will not see "
                "this tick's marks",
                exc_info=True,
            )


def _text(value: Any) -> str:
    """Redis answers ``bytes`` unless the client decodes; both are text here."""

    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _epoch(value: Any) -> float:
    text = _text(value)
    if not text:
        return float("-inf")
    try:
        return float(text)
    except ValueError:
        # A hand-edited or half-written field is not a reason to stop scaling.
        logger.warning("autoscaler state: %r is not a timestamp; ignoring", text)
        return float("-inf")
