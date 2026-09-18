"""Quota-agent HTTP client + deployment wiring (E2.6; agent form promoted in A6).

**The agent form is the deployment's quota source, and
``E2B_QUOTA_AGENT_URL`` (non-blank) is what selects it** -- the worker's
``quota_via_agent`` setting derives from that URL, and the flag
``E2B_QUOTA_VIA_AGENT`` only matters when no URL is configured (failing with a
warning when it is set without one). Leaving the URL unset keeps the dev/legacy
direct path, where the worker runs ``xfs_quota`` against its own XFS mount and
therefore needs root/effective CAP_SYS_ADMIN.

The historical reason for this module is the NFS form: the worker only sees an
NFS client mount, so the real filesystem lives on the server; this module talks
to the server-side quota-agent (``deploy/quota_agent``), which executes
``xfs_quota`` locally and owns the privilege.

Wire the module hooks (``xfs_quota.agent_query`` + ``xfs_quota.agent_ops``)
with :func:`configure_quota_agent_client`; :func:`envd_service.app.create_app`
does that automatically whenever the agent form is on -- i.e. whenever
``E2B_QUOTA_AGENT_URL`` is set (or ``E2B_QUOTA_VIA_AGENT=true`` without one,
which wires nothing and degrades with a warning).

HTTP contract with the agent (every response is a JSON object):

- ``GET /detect?mount=...`` -> facts dict (``fs_type`` / ``projid32bit`` /
  ``prjquota`` / ``xfs_quota``) or ``{"error": reason}`` when the server
  cannot answer.
- ``POST /project_create`` ``{"projid", "path", "limit_mb", "mount"}``
  -> ``{"projid": int}``.
- ``POST /project_delete`` ``{"projid", "path", "mount"}``
  -> ``{"deleted": int}``.
- ``GET /report?mount=...`` -> ``{"projects": {projid: {"used_blocks",
  "soft_blocks", "hard_blocks"}}}``.
- ``POST /reconcile`` ``{"workspace_base", "mount"}`` -> ``{"cleaned":
  [projid], "skipped": [{"projid", "reason"}]}``.

Auth: ``X-Internal-Key`` header carrying the configured agent token
(``E2B_QUOTA_AGENT_TOKEN``), matching the worker's internal-key style.

Every failure — network error, non-2xx (401 included), non-JSON body,
non-dict payload or missing fields — raises :class:`ProjectQuotaError`. The
existing quota call sites already catch that and degrade with a warning, so
an unreachable or rejecting agent never blocks sandbox create/delete or
volume mounts.

W6 adds a **bounded startup readiness wait** around the first quota probes
(:func:`wait_for_startup_readiness`): the worker and quota-agent are separate
containers in one rollout, so the worker's startup probes can run before the
agent serves. That ordering used to be recorded as a degrade — and the
``QuotaMonitor`` caches the failed verdict for the process lifetime — so the
wait tells "not up yet" (retried, reported at INFO) apart from "not
configured" (``E2B_QUOTA_AGENT_URL`` empty, which keeps its explicit
WARNING). An agent that is still absent when the window runs out keeps the
ordinary WARNING too: a real degrade is never hidden.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from envd_service import xfs_quota
from envd_service.xfs_quota import (
    ProjectQuotaError,
    _probe_free_projid,
    configure_agent_ops,
    configure_agent_query,
)

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 5.0
_INTERNAL_KEY_HEADER = "X-Internal-Key"

#: W6: size of the bounded startup readiness window — ``attempts`` probes
#: spaced ``delay_s`` apart (8 x 2.5s = at most 17.5s of waiting).
STARTUP_READY_ATTEMPTS = 8
STARTUP_READY_DELAY_S = 2.5

#: Test seam for the retry's sleep: the window can be made instant without
#: touching the policy under test.
_sleep = time.sleep

#: Verdict cache keyed on the *wired hook object* (``xfs_quota.agent_query``).
#: The two startup probes of one worker (the quota monitor's first scan and
#: the startup reconcile) therefore share one bounded wait instead of each
#: burning its own, while a freshly configured client — a new app in tests, a
#: restarted worker in production — re-arms the window.
_startup_gate_lock = threading.Lock()
_startup_gate: tuple[object, tuple[str, str | None]] | None = None


def _response_error(response: httpx.Response) -> str:
    """Best-effort extract of the agent's ``{"error": ...}`` detail."""
    try:
        payload = response.json()
    except ValueError:
        return response.text.strip() or f"HTTP {response.status_code}"
    if isinstance(payload, dict) and "error" in payload:
        return str(payload["error"])
    return f"HTTP {response.status_code}"


class QuotaAgentClient:
    """HTTP client for the server-side quota-agent."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        url = base_url.rstrip("/")
        if "://" not in url:
            url = f"http://{url}"
        self._base_url = url
        headers = {}
        if token:
            headers[_INTERNAL_KEY_HEADER] = token
        self._client = httpx.Client(
            base_url=url,
            headers=headers,
            timeout=timeout_s,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def _request(self, op: str, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self._client.request(method, url, **kwargs)
        except (httpx.HTTPError, OSError) as exc:
            raise ProjectQuotaError(f"quota-agent unreachable: {exc}") from exc
        if response.status_code == 401:
            raise ProjectQuotaError("quota-agent auth failed: HTTP 401")
        if response.status_code >= 400:
            raise ProjectQuotaError(
                f"quota-agent {op} failed: {_response_error(response)}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProjectQuotaError(
                f"quota-agent {op} returned non-JSON response"
            ) from exc
        if not isinstance(payload, dict):
            raise ProjectQuotaError(
                f"quota-agent {op} returned non-dict response: {payload!r}"
            )
        return payload

    def detect(self, mount_point: str) -> dict[str, Any]:
        """Agent-side detection facts for ``mount_point`` (E2.1 contract)."""
        return self._request(
            "detect", "GET", "/detect", params={"mount": str(mount_point)}
        )

    def report(self, mount_point: str) -> dict[str, Any]:
        """Server-side project quota table (E2.4 monitoring contract)."""
        payload = self._request(
            "report", "GET", "/report", params={"mount": str(mount_point)}
        )
        if "projects" not in payload or not isinstance(payload["projects"], dict):
            raise ProjectQuotaError(
                f"quota-agent report response missing 'projects': {payload!r}"
            )
        return payload

    def provision(
        self,
        *,
        sandbox_id: str,
        project_dir: str,
        disk_mb: int,
        mount_point: str,
        project_id: int | None = None,
    ) -> int:
        """Create a server-side project + hard limit; return the projid."""
        if project_id is None:
            in_use = {int(projid) for projid in self.report(mount_point)["projects"]}
            projid = _probe_free_projid(sandbox_id, in_use)
        else:
            projid = int(project_id)
        payload = self._request(
            "project_create",
            "POST",
            "/project_create",
            json={
                "projid": projid,
                "path": str(project_dir),
                "limit_mb": int(disk_mb),
                "mount": str(mount_point),
            },
        )
        returned = payload.get("projid")
        if not isinstance(returned, int):
            raise ProjectQuotaError(
                f"quota-agent project_create response missing int 'projid': "
                f"{payload!r}"
            )
        return returned

    def release(
        self,
        *,
        project_dir: str,
        mount_point: str,
        projid: int,
    ) -> None:
        """Clear the server-side project state on ``project_dir``."""
        payload = self._request(
            "project_delete",
            "POST",
            "/project_delete",
            json={
                "projid": int(projid),
                "path": str(project_dir),
                "mount": str(mount_point),
            },
        )
        deleted = payload.get("deleted")
        if not isinstance(deleted, int):
            raise ProjectQuotaError(
                f"quota-agent project_delete response missing int 'deleted': "
                f"{payload!r}"
            )

    def clear_limits(
        self,
        *,
        mount_point: str,
        projid: int,
    ) -> None:
        """Reset the project's block limits so XFS drops the row (N12).

        The delete path's second half: ``release`` clears the directory's project
        state before the tree is removed, this resets the limits after it. Only
        both together make the quota row go away at delete time instead of at the
        next reconciliation.
        """
        payload = self._request(
            "project_limits",
            "POST",
            "/project_limits",
            json={"projid": int(projid), "mount": str(mount_point)},
        )
        cleared = payload.get("cleared")
        if not isinstance(cleared, int):
            raise ProjectQuotaError(
                f"quota-agent project_limits response missing int 'cleared': "
                f"{payload!r}"
            )

    def reconcile(
        self,
        *,
        workspace_base: str,
        mount_point: str,
    ) -> dict[str, Any]:
        """Server-side orphan project reconciliation (E2.4 contract)."""
        payload = self._request(
            "reconcile",
            "POST",
            "/reconcile",
            json={
                "workspace_base": str(workspace_base),
                "mount": str(mount_point),
            },
        )
        cleaned = payload.get("cleaned")
        skipped = payload.get("skipped")
        if not isinstance(cleaned, list) or not isinstance(skipped, list):
            raise ProjectQuotaError(
                f"quota-agent reconcile response missing 'cleaned'/'skipped' "
                f"lists: {payload!r}"
            )
        return payload

    def agent_ops(self) -> dict[str, Callable[..., Any]]:
        """The worker hook map expected by ``xfs_quota.agent_ops``."""
        return {
            "provision": self.provision,
            "release": self.release,
            "clear_limits": self.clear_limits,
            "report": self.report,
            "reconcile": self.reconcile,
        }


def configure_quota_agent_client(
    *,
    url: str | None,
    token: str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    transport: httpx.BaseTransport | None = None,
) -> QuotaAgentClient | None:
    """Wire ``xfs_quota.agent_query`` / ``agent_ops`` from deployment config.

    Returns the configured client (for app lifecycle cleanup) or ``None``.
    With ``url`` unset the hooks are reset to ``None`` and a warning is
    logged: the worker degrades (skips quota with a warning) exactly like an
    unreachable agent. That is the only case left in which the flag matters --
    a non-blank ``E2B_QUOTA_AGENT_URL`` selects the agent form on its own
    (``Settings.quota_via_agent`` derives from it), and setting
    ``E2B_QUOTA_VIA_AGENT=true`` without a URL never blocks sandboxes.
    """
    if not url:
        configure_agent_query(None)
        configure_agent_ops(None)
        logger.warning(
            "E2B_QUOTA_VIA_AGENT is enabled but E2B_QUOTA_AGENT_URL is not "
            "set; quota-agent hooks unconfigured, quota operations will "
            "degrade with warnings"
        )
        return None
    if not token:
        logger.warning(
            "E2B_QUOTA_AGENT_TOKEN is not set; quota-agent requests carry no "
            "X-Internal-Key and will be rejected (401) until configured"
        )
    client = QuotaAgentClient(
        url,
        token=token,
        timeout_s=timeout_s,
        transport=transport,
    )
    configure_agent_query(client.detect)
    configure_agent_ops(client.agent_ops())
    logger.info("quota-agent configured at %s", client._base_url)
    return client


def wait_for_startup_readiness(
    mount_point: str | Path,
    *,
    attempts: int = STARTUP_READY_ATTEMPTS,
    delay_s: float = STARTUP_READY_DELAY_S,
) -> tuple[str, str | None]:
    """Wait (bounded) for quota-agent before a startup quota probe runs.

    Three conclusions, told apart on purpose so a startup race cannot be
    mistaken for a missing deployment (W6):

    * ``("unconfigured", None)`` — ``E2B_QUOTA_AGENT_URL`` is empty, so no
      hook is wired. Nothing to wait for, and the unconfigured form keeps its
      existing explicit WARNING ("quota-agent not configured").
    * ``("ready", None)`` — the agent answered (logged at INFO when it took
      more than one attempt, i.e. when a race was actually absorbed).
    * ``("unreachable", reason)`` — still nothing after the whole window. The
      outcome is reported at INFO with the reason and the caller's own probe
      then logs the ordinary WARNING, so a real degrade stays visible.

    Bounded by construction — at most ``attempts`` probes spaced ``delay_s``
    apart — and never raises; callers run it on a worker thread so it cannot
    block the event loop or the heartbeat loop.
    """
    hook = xfs_quota.agent_query
    if hook is None:
        return ("unconfigured", None)
    attempts = max(1, int(attempts))
    global _startup_gate
    with _startup_gate_lock:
        if _startup_gate is not None and _startup_gate[0] is hook:
            return _startup_gate[1]
        verdict = _run_startup_window(
            hook, mount_point, attempts=attempts, delay_s=delay_s
        )
        _startup_gate = (hook, verdict)
        return verdict


def _run_startup_window(
    hook: Callable[[str], dict[str, Any]],
    mount_point: str | Path,
    *,
    attempts: int,
    delay_s: float,
) -> tuple[str, str | None]:
    reason = ""
    for attempt in range(1, attempts + 1):
        try:
            hook(str(mount_point))
        except Exception as exc:
            reason = str(exc) or exc.__class__.__name__
            if attempt < attempts:
                _sleep(delay_s)
        else:
            if attempt > 1:
                logger.info(
                    "quota-agent answered on startup attempt %d/%d: the "
                    "startup probe is no longer racing the agent",
                    attempt,
                    attempts,
                )
            return ("ready", None)
    logger.info(
        "quota-agent unreachable after %d startup attempt(s) over %.1fs: %s; "
        "the startup probe now records its own WARNING",
        attempts,
        (attempts - 1) * delay_s,
        reason,
    )
    return ("unreachable", reason)
