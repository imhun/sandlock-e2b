"""Deterministic answers for the project-id read the destructive paths trust.

``_verified_project_id`` / ``directory_project_id`` read a directory's
project id *from the filesystem*, never from ``sandbox.json``: the record
lives inside the sandbox-owned tree, so the sandbox can rewrite it (review
W1 / M4). A contract for the delete endpoint or for the orphan sweep
therefore has to answer that read itself.

Faking the mount-level probe is not enough. W2 (``5c73ad1``) made the read
two-stage: the containing mount is asked first (``_use_quotactl_read``) and a
mount-level "no" is deliberately *not* final, because the directory itself
gets the last word (``can_read_projid`` -- ``FS_IOC_FSGETXATTR`` on the
directory's own fd, so a workspace base under a mount root this worker may
not open still reads; follow-up 2). A fake that only patches
``_use_quotactl_read`` therefore decides the read on macOS -- there is no
``libc.so.6``, so the directory cannot answer, so the ``lsattr`` fallback the
fake *did* patch is reached -- and decides nothing at all on the deployment's
own shape: on Linux the directory answers with its real project id (0 on a
tree that was never tagged), the read silences to "no project id",
``release_project`` is never called, and every assertion built on the
explicit table is skipped while the file stays green. Measured in the gate
container before the fix: ``tmp/probe_diskread.py`` printed an empty
``lsattr argv`` list, ``released: []`` and ``status: 204`` for a record whose
tree the disk was supposed to say belonged to another tenant.

Both fakes below patch the *composition* gate
(:func:`envd_service.xfs_quota._use_quotactl_for_read`) -- the single point
that decides between the two read backends -- and answer the read in the form
that gate selects, so the table decides the read on every host:

``lsattr``
    the historical fallback. The fake returns ``lsattr -p -d``'s stdout, so
    the real argv and the real parser still run.
``quotactl``
    the deployment's own shape. The fake stands in for
    :func:`envd_service.xfs_quotactl.projid_of`, mirrored down to its
    ``open``: a directory that is not there fails there, exactly as the real
    one does, and ``_read_failure_class`` classifies it the same way in both
    forms. The ioctl itself is exercised for real on a kernel that has one by
    ``tests/unit/test_xfs_quotactl_backend.py``.

``backend`` is a test parameter (see the ``disk_read_backend`` fixture in
``tests/conftest.py``), not an environment fact: the same assertions run
against both read paths on every host, so a contract that only ever
exercised the one form its host happens to have cannot come back.

The *administration* gate (``_use_quotactl``) is pinned to the subprocess form
in both, because it answers a different question -- "can this mount administer
project quotas" -- from a different probe (``xfs_quotactl.available``), and
``xfs_quota`` uses it for the orphan scan's own read
(``_read_top_level_project_ids``) as well as for every local quota operation.
The same pin for the same reason is in
``test_quota_maintenance.py::test_scan_project_dirs_keeps_sandbox_trees_only``
("the fd backend probe is Linux-only"): a host that answers it yes -- or, on a
dev box without ``libc.so.6``, raises ``OSError`` out of ``ctypes.CDLL`` --
must not decide these contracts. No contract in this family drives a local
quota operation without its own fake (``agent_ops`` / ``release_project``),
and the scan therefore speaks ``lsattr`` in both forms.

Both read implementations are installed whichever form is under test, so the
only thing that decides which one the product used is the gate above -- and
the ``calls`` assertion pins that, rather than the fake silently covering for
a wrong fallback.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import envd_service.xfs_quota as xfs_quota
import envd_service.xfs_quotactl as xfs_quotactl

#: The two ways ``directory_project_id`` can be answered, in the order the
#: product picks them (W2's two-stage read). ``lsattr`` stays first so the
#: default is the fallback the older contracts were written against.
DISK_READ_BACKENDS = ("lsattr", "quotactl")


def read_calls(backend: str, *paths: Path) -> list[list[str]]:
    """The exact product read calls for ``paths`` in ``backend``'s form.

    Handed to an assertion against :attr:`DiskProjids.calls` so the contract
    keeps pinning *which* directory was read and in what shape, on either
    backend, instead of degrading to "the table was consulted".
    """
    lead = ["lsattr", "-p", "-d"] if backend == "lsattr" else ["projid_of"]
    return [lead + [str(path)] for path in paths]


class DiskProjids:
    """One explicit project-id table, answering whichever read is installed.

    ``mapping`` is the whole disk: a directory absent from it carries no
    project id, which is what ``lsattr -p -d``'s empty output and
    ``projid_of()`` returning 0 both mean. ``calls`` records what the product
    asked for, in the installed backend's own form.
    """

    def __init__(
        self,
        backend: str,
        mapping: dict[Path, int],
        failure: str | None = None,
    ) -> None:
        if backend not in DISK_READ_BACKENDS:
            raise ValueError(
                f"unknown read backend {backend!r}; "
                f"expected one of {DISK_READ_BACKENDS}"
            )
        self.backend = backend
        self.mapping = {
            Path(path): int(projid) for path, projid in mapping.items()
        }
        #: When set, the read backend itself fails with this detail instead of
        #: answering -- the "this host cannot ask the disk" shape, expressed
        #: in each form's own mechanism (a non-zero ``lsattr``, a
        #: ``QuotactlError`` from the fd backend).
        self.failure = failure
        self.calls: list[list[str]] = []

    def install(self, monkeypatch) -> "DiskProjids":
        """Pin the read gate to :attr:`backend` and install that backend."""
        monkeypatch.setattr(
            xfs_quota,
            "_use_quotactl_for_read",
            lambda directory: self.backend == "quotactl",
        )
        monkeypatch.setattr(xfs_quota, "_use_quotactl", lambda mount_point: False)
        self._install_lsattr(monkeypatch)
        if self.backend == "quotactl":
            self._install_quotactl(monkeypatch)
        return self

    def _install_lsattr(self, monkeypatch) -> None:
        real_run = subprocess.run

        def fake_run(argv, *args, **kwargs):
            if isinstance(argv, (list, tuple)) and argv and argv[0] == "lsattr":
                path = Path(argv[-1])
                self.calls.append(list(argv))
                if self.failure is not None:
                    return subprocess.CompletedProcess(
                        list(argv), 1, "", self.failure
                    )
                projid = self.mapping.get(path)
                stdout = (
                    ""
                    if projid is None
                    else f"{projid:>8} ---------------- {path}\n"
                )
                return subprocess.CompletedProcess(list(argv), 0, stdout, "")
            return real_run(argv, *args, **kwargs)

        # The argv and the parser on the other side of it are the product's
        # own: only the process that would have printed the project id is
        # replaced.
        monkeypatch.setattr(xfs_quota.subprocess, "run", fake_run)

    def _install_quotactl(self, monkeypatch) -> None:
        def fake_projid_of(path):
            self.calls.append(["projid_of", str(path)])
            if self.failure is not None:
                raise xfs_quotactl.QuotactlError(self.failure)
            # The real ``projid_of`` opens the directory before it asks the
            # kernel anything, so a directory that is not there fails here
            # rather than answering from the table -- which is what makes
            # ``ProjectDirectoryGone`` come out of the fd form too.
            try:
                fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
            except OSError as exc:
                raise xfs_quotactl.QuotactlError(
                    f"cannot open {path}: {exc}"
                ) from exc
            os.close(fd)
            return self.mapping.get(Path(path), 0)

        monkeypatch.setattr(xfs_quotactl, "projid_of", fake_projid_of)


def install_disk_projids(
    monkeypatch,
    mapping: dict[Path, int],
    *,
    backend: str = "lsattr",
    failure: str | None = None,
) -> DiskProjids:
    """Answer the project-id read from ``mapping`` through ``backend``.

    ``backend`` defaults to the fallback so a one-shot caller (a probe, or a
    contract that is explicitly about that form) can omit it; every contract
    here passes the ``disk_read_backend`` fixture, which is what makes the
    same assertions run against both forms.
    """
    return DiskProjids(backend, mapping, failure=failure).install(monkeypatch)
