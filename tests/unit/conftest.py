"""Fixtures the unit lane shares.

``registry`` and ``make_record`` live here rather than in the two files that
use them because the N30 ledger cases in ``test_pause_quota.py`` and
``test_tenant_quota.py`` assert the *same* property of the same object -- the
reservation rows equal the live records' own numbers -- and a per-file copy is
the copy that drifts the first time one of them grows a dimension.

The ``registry`` fixture is deliberately the *unbounded* shape: no pool can
refuse, so nothing it provides can decide an assertion. A case that needs a
refusal to be possible builds its own bounded registry and says so.

``publish_spy`` earns its place the same way: the record files in
``control_plane/registry/`` and ``envd_service/runtime/registry.py`` are read by
a *different* process (the other replica, the other worker) while they are
written, so every one of those writers owes the same property -- the update
becomes visible in one step. The cases that pin it live in four files; the spy
is what lets each of them stand inside the write window instead of racing for
it.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from control_plane.config import Settings
from control_plane.registry.manager import SandboxRecord, SandboxRegistry


class _PublishSpy:
    """Stands in for the one step that makes a record visible to a reader.

    ``os.replace`` is that step: the new bytes are written to a file beside
    the target and then renamed over it, so a reader -- in this process or in
    another one -- sees the previous complete document or the new complete
    one, never half of either. Watching for it is also how a case can *be* in
    the write window deterministically instead of racing for it:
    :meth:`hold_next_publish` freezes the writer with the new bytes staged and
    the published path untouched.

    ``calls`` doubles as the defect's witness. A writer that truncates the
    target in place (``Path.write_text``) never reaches this step at all, so a
    case that asks for the window and waits for it fails on the defect rather
    than on a timing accident -- which is what makes these cases red before
    the fix and green after it.
    """

    def __init__(self, real: Callable[..., Any]) -> None:
        self._real = real
        #: ``(staged, published)`` for every publish this process performed.
        self.calls: list[tuple[Path, Path]] = []
        self._window: tuple[threading.Event, threading.Event] | None = None

    def __call__(self, src, dst, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        self.calls.append((Path(src), Path(dst)))
        window = self._window
        if window is not None:
            entered, release = window
            entered.set()
            release.wait(timeout=30)
        return self._real(src, dst, *args, **kwargs)

    def reset(self) -> None:
        """Forget everything published so far (a case's own setup writes)."""
        self.calls.clear()

    @contextmanager
    def hold_next_publish(self) -> Iterator[threading.Event]:
        """Allow the next publish to be reached, then hold it there.

        Yields the event that the writer sets once it is inside the window:
        the staged file holds the new bytes, the published path still holds
        the previous document. The writer is released when the ``with`` block
        exits (including on an assertion failure, so a red case does not leave
        a thread parked behind it).
        """
        window = (threading.Event(), threading.Event())
        self._window = window
        try:
            yield window[0]
        finally:
            self._window = None
            window[1].set()

    def await_publish(self, entered: threading.Event, subject: str) -> None:
        """Assert the writer reached the single-step publish of ``subject``."""
        assert entered.wait(timeout=5), (
            f"{subject} has to be published in one step: another process reads "
            "the published path while this writer is holding it, so an "
            "in-place truncating write hands that reader half a document "
            "(this write never reached a rename at all)"
        )


@pytest.fixture()
def publish_spy(monkeypatch: pytest.MonkeyPatch) -> _PublishSpy:
    """Watch (and, on request, hold) how the code under test publishes files."""
    spy = _PublishSpy(os.replace)
    monkeypatch.setattr(os, "replace", spy)
    return spy


@pytest.fixture(autouse=True)
def _the_worker_can_build_a_sandbox_root(request):
    """The N35 gate asks the *node* whether it can build a sandbox root.

    Since N14 S5 the real root is the shape, so every `SandlockExecutor` with a
    root consults the mount-family probe at construction. The probe needs a
    Linux worker (``libc.so.6``, ``pivot_root``, the mount admission in the
    seccomp profile), so on this dev host it answers "no" and the create is
    refused before a case can look at what it is about -- a statement about the
    laptop, not about the code under test.

    `test_real_root_gate.py` is the one file that pins the gate itself (and the
    probe's own answers), so it opts out of this stub.
    """
    if request.module.__name__.rsplit(".", 1)[-1] == "test_real_root_gate":
        yield
        return
    import envd_service.executors.sandlock as sandlock_mod

    # Plain setattr rather than `monkeypatch`: asking for that fixture here
    # would reorder its teardown in the modules on the lane, and
    # `test_real_root_gate`'s cache-clearing fixture needs to run while the
    # file's own `monkeypatch` is still in place.
    original = sandlock_mod._real_root_capability
    sandlock_mod._real_root_capability = lambda: ""
    try:
        yield
    finally:
        sandlock_mod._real_root_capability = original


def ledger_settings(**overrides) -> Settings:
    """Settings for a registry whose only subject is the ledger.

    Realistic per-record defaults (a create still books memory, cpu and
    processes), every *pool* unbounded -- ``0`` is "no ceiling" throughout the
    manager -- so the disk rows are the only thing a case can be surprised by.
    """
    defaults = dict(
        api_keys=("local-key",),
        max_sandboxes=0,
        default_memory_mb=512,
        default_cpu_percent=100,
        default_disk_mb=0,
        default_max_processes=64,
        max_total_memory_mb=0,
        max_total_cpu_percent=0,
        max_total_disk_mb=0,
        max_total_processes=0,
    )
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.fixture()
def registry() -> SandboxRegistry:
    return SandboxRegistry(ledger_settings())


@pytest.fixture()
def make_record() -> Callable[..., SandboxRecord]:
    """Factory for a live record holding exactly ``disk_size_mb`` of disk.

    ``SandboxRegistry.create`` sells whatever ``settings.default_disk_mb``
    says, so a case that needs two different budgets in one ledger has to write
    the sale itself. The write goes through the same release/hold pair the
    pause/resume path uses -- never a poke at the counters -- so the helper
    cannot leave a ledger that admission could not have produced, and the
    property under test (release gives the row back, hold takes it again) stays
    the only thing the cases assert.
    """

    def _make(
        registry: SandboxRegistry,
        *,
        disk_size_mb: int,
        sandbox_id: str | None = None,
        **overrides,
    ) -> SandboxRecord:
        kwargs = dict(
            template_id="base",
            timeout=300,
            metadata={},
            env_vars={},
            secure=True,
            allow_internet_access=False,
            base_image=None,
            sandbox_id=sandbox_id,
        )
        kwargs.update(overrides)
        record = registry.create(**kwargs)
        if record.disk_size_mb == disk_size_mb:
            return record
        assert registry.release_quota(record) is True, (
            "the sale helper re-books through release/hold, so a fresh record "
            f"must be holding a reservation ({record.sandbox_id})"
        )
        record.disk_size_mb = disk_size_mb
        assert registry.hold_quota(record) is True, (
            f"an unbounded-pool registry must be able to hold "
            f"{disk_size_mb} MiB for {record.sandbox_id}"
        )
        return registry.save(record)

    return _make
