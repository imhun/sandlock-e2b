"""Periodic quota + disk watermark monitoring (E2.4).

The worker owns the XFS mount, so it runs the periodic ``report -p`` scan:
projects that have reached their hard limit are reported as over-limit, and
projects above the warning ratio are reported as near-limit. Filesystem
usage of ``workspace_base`` is checked against watermark ratios. Every scan
updates counters that the worker heartbeat carries to the control plane,
where they are stored on the node record and exposed through the nodes API.

NFS deployments run the same loop against quota-agent (E2.6) via
``via_agent``; when the agent path is unconfigured the quota part degrades
to disk-usage-only monitoring.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Any

from envd_service.xfs_quota import ProjectQuotaError, project_quota_table, xfs_project_supported

logger = logging.getLogger(__name__)


class QuotaMonitor:
    """Periodic over-limit + disk watermark inspection for one worker."""

    def __init__(
        self,
        *,
        workspace_base: str | Path,
        mount_point: str | Path,
        via_agent: bool = False,
        interval_s: float = 60.0,
        quota_warn_ratio: float = 0.9,
        disk_warn_ratio: float = 0.9,
        disk_error_ratio: float = 0.98,
    ) -> None:
        if not 0 < quota_warn_ratio <= 1:
            raise ValueError("quota_warn_ratio must be in (0, 1]")
        if not 0 < disk_warn_ratio < disk_error_ratio <= 1:
            raise ValueError("disk ratios must satisfy 0 < warn < error <= 1")
        self._workspace_base = Path(workspace_base)
        self._mount_point = Path(mount_point)
        self._via_agent = via_agent
        self._interval_s = interval_s
        self._quota_warn_ratio = quota_warn_ratio
        self._disk_warn_ratio = disk_warn_ratio
        self._disk_error_ratio = disk_error_ratio
        self._quota_supported: bool | None = None
        self._task: asyncio.Task | None = None
        self.over_limit: list[int] = []
        self.near_limit: list[int] = []
        self.over_limit_count = 0
        self.near_limit_count = 0
        self.disk_warn_count = 0
        self.disk_error_count = 0
        self.last_scan: dict[str, Any] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.inspect_once)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("quota monitor scan failed", exc_info=True)
            await asyncio.sleep(self._interval_s)

    def metrics(self) -> dict[str, Any]:
        """Snapshot of alert state for the worker heartbeat / control plane."""
        return {
            "quotaOverLimit": list(self.over_limit),
            "quotaNearLimit": list(self.near_limit),
            "quotaOverLimitCount": self.over_limit_count,
            "quotaNearLimitCount": self.near_limit_count,
            "diskWarnCount": self.disk_warn_count,
            "diskErrorCount": self.disk_error_count,
        }

    def inspect_once(self) -> dict[str, Any]:
        """Run one scan; returns a summary and logs any alerts."""
        summary: dict[str, Any] = {
            "over_limit": [],
            "near_limit": [],
            "disk_used_ratio": None,
        }
        if self._quota_supported is None:
            self._quota_supported = xfs_project_supported(
                self._mount_point, via_agent=self._via_agent
            )[0]
        if self._quota_supported:
            try:
                table = project_quota_table(
                    self._mount_point, via_agent=self._via_agent
                )
            except ProjectQuotaError as exc:
                logger.warning("quota report failed: %s", exc)
                table = {}
            over_limit = [
                usage.projid
                for usage in table.values()
                if usage.hard_blocks > 0 and usage.used_blocks >= usage.hard_blocks
            ]
            near_limit = [
                usage.projid
                for usage in table.values()
                if usage.hard_blocks > 0
                and usage.used_blocks > 0
                and usage.used_blocks < usage.hard_blocks
                and usage.used_blocks / usage.hard_blocks >= self._quota_warn_ratio
            ]
            for projid in over_limit:
                usage = table[projid]
                logger.error(
                    "sandbox quota over limit: projid %s used %s blocks hard %s",
                    projid,
                    usage.used_blocks,
                    usage.hard_blocks,
                )
            for projid in near_limit:
                usage = table[projid]
                logger.warning(
                    "sandbox quota near limit: projid %s used %s blocks hard %s",
                    projid,
                    usage.used_blocks,
                    usage.hard_blocks,
                )
            self.over_limit = over_limit
            self.near_limit = near_limit
            self.over_limit_count += len(over_limit)
            self.near_limit_count += len(near_limit)
            summary["over_limit"] = over_limit
            summary["near_limit"] = near_limit
        try:
            usage = shutil.disk_usage(self._workspace_base)
        except OSError as exc:
            logger.warning(
                "disk usage check failed for %s: %s", self._workspace_base, exc
            )
            self.last_scan = summary
            return summary
        ratio = usage.used / usage.total
        summary["disk_used_ratio"] = round(ratio, 4)
        summary["disk_used_bytes"] = usage.used
        summary["disk_total_bytes"] = usage.total
        if ratio >= self._disk_error_ratio:
            self.disk_error_count += 1
            logger.error(
                "workspace disk watermark critical: %.1f%% used (%d/%d bytes)",
                ratio * 100,
                usage.used,
                usage.total,
            )
        elif ratio >= self._disk_warn_ratio:
            self.disk_warn_count += 1
            logger.warning(
                "workspace disk watermark warning: %.1f%% used (%d/%d bytes)",
                ratio * 100,
                usage.used,
                usage.total,
            )
        self.last_scan = summary
        return summary
