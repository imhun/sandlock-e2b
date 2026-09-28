"""Configuration for the C3 per-node agent (``deploy/c3_agent``).

Same shape as ``deploy/quota_agent/config.py``: a tiny dataclass reading env,
refusing to run without its token (``app``/``__main__`` enforce that), and with
no state beyond what the process was configured with -- the agent is
**stateless by construction** (C3 §1: no authorization table, no TTL, no
"push before send" ordering; every parameter, the uid included, arrives in the
control plane's instruction).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from gateway_common.env import _env_int


@dataclass
class Settings:
    #: The CP→agent credential (``X-Internal-Key`` on this service). The
    #: agent→CP direction keeps the existing internal-key pattern; this is the
    #: other half of "双向认证" -- both directions authenticated, no new PKI.
    token: str = field(default_factory=lambda: os.getenv("E2B_C3_AGENT_TOKEN", ""))
    #: The node this agent is (the only local decision it makes: "addressed to
    #: me?"). Prefers the C3-specific name, then the worker's own E2B_NODE_ID
    #: (the agent runs beside the worker's pod, so the identities coincide).
    node_id: str = field(
        default_factory=lambda: os.getenv("E2B_C3_AGENT_NODE_ID")
        or os.getenv("E2B_NODE_ID", "")
    )
    #: Listen address. ``0.0.0.0`` by default because the control plane dials
    #: this service from another pod; what must bound the reach is a
    #: NetworkPolicy allowing only CP→agent (Task 3's DaemonSet), not a loopback
    #: bind that would just break the channel. Kept configurable so a shape with
    #: a local sidecar can bind deliberately.
    host: str = field(
        default_factory=lambda: os.getenv("E2B_C3_AGENT_HOST", "0.0.0.0")
    )
    port: int = field(
        default_factory=lambda: _env_int("E2B_C3_AGENT_PORT", 49985)
    )
    #: The Task 1 primitive. Face A is the only shipped payload of the agent
    #: image besides ``e2b-maint`` (face B, wired by a later task).
    as_uid_path: str = field(
        default_factory=lambda: os.getenv(
            "E2B_C3_AGENT_AS_UID", "/var/lib/e2b-priv/as_uid"
        )
    )
    #: How long one ``as_uid`` invocation may take. The primitive is a single
    #: ``write(2)`` into ``/proc/<pid>/uid_map``, so this only bounds a hung
    #: target; it is a timeout, not a retry budget (a retry is safe -- the
    #: primitive refuses an already-written map -- but Task 3 owns delivery).
    as_uid_timeout_s: float = field(
        default_factory=lambda: float(os.getenv("E2B_C3_AGENT_AS_UID_TIMEOUT_S", "5"))
    )
