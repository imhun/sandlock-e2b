"""Z-F7: the image cache lives on the shared volume, so *several worker
processes* (and the control plane) share one cache directory.

These tests pin the properties the in-process ``threading.Lock`` alone cannot
give, and they use real processes, not threads -- the previous shape passed a
thread-only test while two workers were free to extract the same entry at the
same time, publish ``.complete`` onto a half-written rootfs, and delete each
other's entry on failure.

* two processes racing one image: both end up with a complete rootfs and the
  entry is extracted exactly once;
* a failed extraction cleans up only the staging tree it created and never the
  entry another process completed;
* a fresh process (a restarted worker) reuses the shared cache instead of
  re-extracting;
* the cache directory inside the workspace base is not a sandbox tree;
* the cache is bounded (an unbounded shared-volume cache can fill the disk).
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from envd_service.runtime import image_resolver
from envd_service.runtime.image_resolver import resolve_image_rootfs
from tests.unit.test_local_oci_images import (
    _layer,
    _oci_layout_tar,
    _write_local_oci,
)
from tests.unit.test_oci_registry import (
    FakeRegistry,
    _manifest_json,
    _sha256,
)

PROJECT_ROOT = Path(image_resolver.__file__).resolve().parents[2]


@pytest.fixture()
def registry():  # noqa: ANN201 - test-local fake OCI registry
    reg = FakeRegistry()
    yield reg
    reg.stop()

#: Run by ``sys.executable`` in a *separate process*, i.e. a second worker.
#: It records every real layer extraction in a shared file (so the parent can
#: count how many processes actually extracted), optionally poisons
#: ``extract_layer`` to fail like a truncated layer would, and waits on a
#: barrier file so two children can be released at the same instant.
_CHILD = r"""
import contextlib
import json
import os
import time
from pathlib import Path

from envd_service.runtime import image_resolver as ir

delay = float(os.environ.get("ZF7_EXTRACT_DELAY") or 0.0)
poison = os.environ.get("ZF7_MODE") == "poison"
record = Path(os.environ["ZF7_RECORD"])
real_extract_layer = ir.extract_layer

# ``ZF7_NOLOCK=1`` models storage whose file locks are not honoured across
# clients (an NFS mount with ``nolock``): correctness must then come from the
# atomic publish alone.
if os.environ.get("ZF7_NOLOCK") == "1":
    ir._locked_cache_entry = lambda cache, image: contextlib.nullcontext()


def recording_extract_layer(blob, rootfs):
    with open(record, "a", encoding="utf-8") as fh:
        fh.write("extract %d\n" % os.getpid())
    if poison:
        time.sleep(delay)
        raise ir.ImageResolutionError("poisoned layer (zf7)")
    if delay:
        time.sleep(delay)
    return real_extract_layer(blob, rootfs)


ir.extract_layer = recording_extract_layer

barrier = os.environ.get("ZF7_BARRIER") or ""
if barrier:
    deadline = time.monotonic() + 30.0
    while not Path(barrier).exists():
        if time.monotonic() > deadline:
            raise SystemExit("zf7: barrier timeout")
        time.sleep(0.002)

try:
    rootfs = ir.resolve_image_rootfs(
        os.environ["ZF7_IMAGE"], os.environ["ZF7_CACHE"]
    )
except Exception as exc:  # noqa: BLE001 - the parent asserts on the shape
    print(json.dumps({"pid": os.getpid(), "error": type(exc).__name__,
                      "message": str(exc)}))
else:
    # ``entry_ino`` distinguishes "reused the published entry" from "replaced
    # it": a publisher that deletes and re-creates the entry hands the reader a
    # different inode for the same path, which is the hole this pins shut.
    print(json.dumps({"pid": os.getpid(), "rootfs": str(rootfs),
                      "entry_ino": os.stat(Path(rootfs).parent).st_ino}))
"""


def _child_env(
    *,
    image: str,
    cache: Path,
    record: Path,
    mode: str = "ok",
    delay: float = 0.0,
    barrier: Path | None = None,
    nolock: bool = False,
) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "PYTHONPATH": str(PROJECT_ROOT),
            "ZF7_IMAGE": image,
            "ZF7_CACHE": str(cache),
            "ZF7_RECORD": str(record),
            "ZF7_MODE": mode,
            "ZF7_EXTRACT_DELAY": str(delay),
            "ZF7_BARRIER": str(barrier) if barrier is not None else "",
            # The node default must never shadow the cache under test.
            "E2B_IMAGE_CACHE_DIR": "",
            "E2B_IMAGE_CACHE_MAX_BYTES": "",
            "E2B_IMAGE_CACHE_EVICT_MIN_AGE_S": "",
            "E2B_IMAGE_CACHE_LOCK_TIMEOUT_S": "",
            "E2B_IMAGE_CACHE_OWNER_UID": "",
            "E2B_IMAGE_CACHE_OWNER_GID": "",
            "E2B_IMAGE_CACHE_STAGING_STALE_S": "",
            "ZF7_NOLOCK": "1" if nolock else "",
        }
    )
    return env


def _spawn(env: dict[str, str]) -> subprocess.Popen:
    return _spawn_script(env, _CHILD)


def _spawn_script(env: dict[str, str], script: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", script],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _collect(proc: subprocess.Popen) -> dict[str, object]:
    out, err = proc.communicate(timeout=180)
    assert proc.returncode == 0, f"child failed rc={proc.returncode}\n{err}"
    lines = [line for line in out.splitlines() if line.strip()]
    assert len(lines) == 1, out
    payload = json.loads(lines[0])
    assert isinstance(payload, dict)
    return payload


def _extraction_pids(record: Path) -> list[int]:
    if not record.exists():
        return []
    pids = []
    for line in record.read_text(encoding="utf-8").splitlines():
        kind, _, pid = line.partition(" ")
        assert kind == "extract", line
        pids.append(int(pid))
    return pids


def _entry_names(cache: Path) -> list[str]:
    return sorted(p.name for p in cache.iterdir())


def _write_entry(cache: Path, name: str, payload: bytes, mtime: float) -> Path:
    """A completed cache entry as the resolver publishes it."""
    rootfs = cache / name / "rootfs"
    (rootfs / "bin").mkdir(parents=True)
    (rootfs / "bin" / "sh").write_bytes(b"#!/bin/sh\n")
    (rootfs / "payload").write_bytes(payload)
    (rootfs / ".complete").write_text("ok", encoding="utf-8")
    os.utime(rootfs / ".complete", (mtime, mtime))
    return rootfs


def test_two_processes_racing_one_image_both_get_a_complete_rootfs(
    tmp_path: Path,
) -> None:
    """The shared-volume shape: two workers, one cache, one image."""
    cache = tmp_path / "shared" / "_images"
    cache.mkdir(parents=True)
    image = "e2b-local/zf7_race"
    payload, digest = _oci_layout_tar(
        [_layer({"bin/sh": b"#!/bin/sh\n", "etc/zf7": b"race\n"})]
    )
    _write_local_oci(cache, image, payload)
    record = tmp_path / "extractions.txt"
    barrier = tmp_path / "go"
    # A wide enough extract window that a resolver which does NOT serialize
    # across processes is still inside extraction when the other publishes.
    env = _child_env(
        image=image, cache=cache, record=record, delay=0.4, barrier=barrier
    )

    children = [_spawn(env), _spawn(env)]
    time.sleep(0.3)  # let both children reach the barrier
    barrier.write_text("go", encoding="utf-8")
    results = [_collect(child) for child in children]

    entry = f"e2b-local_zf7_race-{digest[7:47]}"
    rootfs = cache / entry / "rootfs"
    for result in results:
        assert "error" not in result, result
        assert result["rootfs"] == str(rootfs)

    assert (rootfs / ".complete").read_text(encoding="utf-8") == "ok"
    assert (rootfs / "etc" / "zf7").read_bytes() == b"race\n"
    assert (rootfs / "bin" / "sh").read_bytes() == b"#!/bin/sh\n"
    # The published entry keeps the mode ``mkdir`` used to give it: the sandbox
    # uid (not the worker's) has to traverse <entry>/rootfs to chroot into it.
    assert stat.S_IMODE((cache / entry).stat().st_mode) == 0o755
    assert stat.S_IMODE(rootfs.stat().st_mode) == 0o755

    pids = _extraction_pids(record)
    assert len(pids) == 1
    assert pids[0] in (results[0]["pid"], results[1]["pid"])
    # Exactly one entry, no staging leftovers, and the per-image lock file
    # lives inside the cache namespace.
    assert _entry_names(cache) == sorted(["e2b-local_zf7_race.lock", "_oci", entry])


def test_failed_extraction_leaves_another_completed_entry_intact(
    tmp_path: Path,
) -> None:
    """Failure cleanup may only remove what this process staged."""
    cache = tmp_path / "shared" / "_images"
    cache.mkdir(parents=True)
    good = "e2b-local/zf7_good"
    bad = "e2b-local/zf7_bad"
    good_payload, good_digest = _oci_layout_tar(
        [_layer({"bin/sh": b"#!/bin/sh\n", "etc/zf7": b"good\n"})]
    )
    bad_payload, bad_digest = _oci_layout_tar(
        [_layer({"bin/sh": b"#!/bin/sh\n", "etc/zf7": b"bad\n"})]
    )
    _write_local_oci(cache, good, good_payload)
    _write_local_oci(cache, bad, bad_payload)

    good_rootfs = resolve_image_rootfs(good, cache)
    good_entry = f"e2b-local_zf7_good-{good_digest[7:47]}"
    bad_entry = f"e2b-local_zf7_bad-{bad_digest[7:47]}"
    assert good_rootfs == cache / good_entry / "rootfs"
    marker_mtime = (good_rootfs / ".complete").stat().st_mtime_ns

    record = tmp_path / "extractions.txt"
    result = _collect(
        _spawn(
            _child_env(
                image=bad, cache=cache, record=record, mode="poison", delay=0.1
            )
        )
    )

    assert result["error"] == "ImageResolutionError"
    assert result["message"] == "poisoned layer (zf7)"
    # The completed entry is untouched: same bytes, same marker, same mtime.
    assert (good_rootfs / "etc" / "zf7").read_bytes() == b"good\n"
    assert (good_rootfs / ".complete").read_text(encoding="utf-8") == "ok"
    assert (good_rootfs / ".complete").stat().st_mtime_ns == marker_mtime
    # The failed image left no entry and no staging tree behind.
    assert not (cache / bad_entry).exists()
    assert _entry_names(cache) == sorted(
        [
            "_oci",
            "e2b-local_zf7_bad.lock",
            "e2b-local_zf7_good.lock",
            good_entry,
        ]
    )


def test_a_concurrent_failure_cannot_delete_the_winner_entry(tmp_path: Path) -> None:
    """The damage the old cleanup did: a failing extractor removed the whole
    entry -- including the copy another process had just completed and
    published.

    Deterministic shape: the failing process starts while the healthy one is
    already inside extraction, so *with* the fix it can only wait on the lock,
    find the finished entry and reuse it; *without* it, both write the same
    entry and the loser's ``rmtree`` takes the winner's copy away.
    """
    cache = tmp_path / "shared" / "_images"
    cache.mkdir(parents=True)
    image = "e2b-local/zf7_shared_entry"
    payload, digest = _oci_layout_tar(
        [_layer({"bin/sh": b"#!/bin/sh\n", "etc/zf7": b"winner\n"})]
    )
    _write_local_oci(cache, image, payload)
    record = tmp_path / "extractions.txt"

    healthy = _spawn(_child_env(image=image, cache=cache, record=record, delay=0.6))
    time.sleep(0.25)  # the healthy child is inside extraction and holds the lock
    poisoned = _spawn(
        _child_env(
            image=image,
            cache=cache,
            record=record,
            mode="poison",
            delay=0.1,
        )
    )
    healthy_result = _collect(healthy)
    poisoned_result = _collect(poisoned)

    entry = f"e2b-local_zf7_shared_entry-{digest[7:47]}"
    rootfs = cache / entry / "rootfs"
    assert healthy_result["rootfs"] == str(rootfs)
    # The failing process never processed the layers: it waited and reused.
    assert "error" not in poisoned_result, poisoned_result
    assert poisoned_result["rootfs"] == str(rootfs)
    assert (rootfs / ".complete").read_text(encoding="utf-8") == "ok"
    assert (rootfs / "etc" / "zf7").read_bytes() == b"winner\n"
    assert (rootfs / "bin" / "sh").read_bytes() == b"#!/bin/sh\n"
    assert _entry_names(cache) == sorted(
        ["_oci", "e2b-local_zf7_shared_entry.lock", entry]
    )
    assert _extraction_pids(record) == [healthy_result["pid"]]


def test_a_second_publisher_discards_its_staging_without_touching_the_entry(
    tmp_path: Path,
) -> None:
    """Storage whose locks are not honoured across clients (NFS ``nolock``) must
    still never publish a partial rootfs nor leave a hole where a finished entry
    used to be: the atomic publish decides, and the loser's staging tree is all
    that is dropped."""
    cache = tmp_path / "shared" / "_images"
    cache.mkdir(parents=True)
    image = "e2b-local/zf7_nolock"
    payload, digest = _oci_layout_tar(
        [_layer({"bin/sh": b"#!/bin/sh\n", "etc/zf7": b"nolock\n"})]
    )
    _write_local_oci(cache, image, payload)
    record = tmp_path / "extractions.txt"
    barrier = tmp_path / "go"
    env = _child_env(
        image=image, cache=cache, record=record, delay=0.4, barrier=barrier, nolock=True
    )

    children = [_spawn(env), _spawn(env)]
    time.sleep(0.3)
    barrier.write_text("go", encoding="utf-8")
    results = [_collect(child) for child in children]

    entry = f"e2b-local_zf7_nolock-{digest[7:47]}"
    rootfs = cache / entry / "rootfs"
    for result in results:
        assert "error" not in result, result
        assert result["rootfs"] == str(rootfs)
    assert (rootfs / ".complete").read_text(encoding="utf-8") == "ok"
    assert (rootfs / "etc" / "zf7").read_bytes() == b"nolock\n"
    assert (rootfs / "bin" / "sh").read_bytes() == b"#!/bin/sh\n"
    # Nothing serialized the two processes, so one or both processed the
    # layers; what has to hold is that a single, complete entry is published.
    pids = _extraction_pids(record)
    assert 1 <= len(pids) <= 2
    assert set(pids) <= {results[0]["pid"], results[1]["pid"]}
    # The loser discarded its own staging tree: both callers see the SAME
    # published entry (same inode), it was never deleted and re-created.
    assert results[0]["entry_ino"] == results[1]["entry_ino"]
    assert (cache / entry).stat().st_ino == results[0]["entry_ino"]
    assert _entry_names(cache) == sorted(["_oci", entry])


def test_a_fresh_process_reuses_the_shared_cache(registry: FakeRegistry, tmp_path: Path) -> None:
    """A restarted worker must find the completed entry and not re-download or
    re-extract it."""
    config_digest = _sha256(b"{}")
    layer = registry.add_layer(
        {"bin/sh": b"#!/bin/sh\n", "etc/zf7": b"persisted\n"}
    )
    manifest = _manifest_json(
        [
            {
                "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                "size": 1,
                "digest": layer,
            }
        ],
        config_digest,
    )
    registry.add_manifest("latest", manifest)
    image = f"{registry.host}/test/py:latest"
    cache = tmp_path / "shared" / "_images"
    cache.mkdir(parents=True)
    record = tmp_path / "extractions.txt"

    first = _collect(_spawn(_child_env(image=image, cache=cache, record=record)))
    assert "error" not in first
    rootfs = Path(str(first["rootfs"]))
    assert (rootfs / "etc" / "zf7").read_bytes() == b"persisted\n"
    blobs_after_first = registry.blob_requests
    manifest_requests_after_first = registry.manifest_requests
    marker_mtime = (rootfs / ".complete").stat().st_mtime_ns

    second = _collect(_spawn(_child_env(image=image, cache=cache, record=record)))

    assert second["rootfs"] == first["rootfs"]
    # The manifest is re-resolved (that is how a moved tag self-invalidates the
    # cache); the layer blobs and the extraction are not.
    assert registry.manifest_requests > manifest_requests_after_first
    assert registry.blob_requests == blobs_after_first
    assert _extraction_pids(record) == [first["pid"]]
    assert (rootfs / ".complete").stat().st_mtime_ns == marker_mtime


def test_the_shared_cache_dir_is_not_a_sandbox_tree(tmp_path: Path) -> None:
    """The production cache dir sits inside the workspace base; the sandbox
    scans must keep excluding it (and nothing the resolver writes may land
    beside it)."""
    from gateway_common.paths import (
        is_reserved_platform_namespace,
        is_sandbox_workspace_dir,
    )

    workspace_base = tmp_path / "e2b-sandboxes"
    cache = workspace_base / "_images"
    cache.mkdir(parents=True)
    image = "e2b-local/zf7_scan"
    payload, _digest = _oci_layout_tar(
        [_layer({"bin/sh": b"#!/bin/sh\n", "etc/zf7": b"scan\n"})]
    )
    _write_local_oci(cache, image, payload)
    rootfs = resolve_image_rootfs(image, cache)
    assert (rootfs / ".complete").is_file()

    assert is_reserved_platform_namespace("_images") is True
    assert is_sandbox_workspace_dir(cache) is False
    # The lock file and the staging trees stay inside ``_images``: the
    # workspace base gains no new top-level name for a scan to trip over.
    assert sorted(p.name for p in workspace_base.iterdir()) == ["_images"]


def test_prune_evicts_the_oldest_completed_entries(tmp_path: Path) -> None:
    """The cache is bounded: oldest completed entry first, incomplete entries
    never, and the survivors are exactly the newest ones."""
    from envd_service.runtime.image_resolver import prune_image_cache

    cache = tmp_path / "cache"
    cache.mkdir()
    for name, mtime in (("a-1111", 1000.0), ("b-2222", 2000.0), ("c-3333", 3000.0)):
        _write_entry(cache, name, b"p" * (3 * 1024 * 1024), mtime)
    # Incomplete and staging shapes: never candidates.
    (cache / "d-4444" / "rootfs").mkdir(parents=True)
    (cache / ".e-5555.tmp-4242").mkdir()
    before = image_resolver._tree_bytes(cache / "c-3333") + image_resolver._tree_bytes(
        cache / "b-2222"
    ) + image_resolver._tree_bytes(cache / "a-1111")

    stats = prune_image_cache(cache, max_bytes=4 * 1024 * 1024, min_age_s=0.0)

    assert stats["entries"] == 3
    assert stats["evicted"] == 2
    assert stats["skipped_fresh"] == 0
    assert stats["kept_bytes"] == image_resolver._tree_bytes(cache / "c-3333")
    assert stats["freed_bytes"] == before - stats["kept_bytes"]
    assert sorted(p.name for p in cache.iterdir()) == [
        ".e-5555.tmp-4242",
        "c-3333",
        "d-4444",
    ]


def test_a_cold_resolve_enforces_the_cache_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bound is applied by the resolver itself (no separate operator
    cron), and it evicts only completed entries."""
    cache = tmp_path / "shared" / "_images"
    cache.mkdir(parents=True)
    _write_entry(cache, "old-1111", b"o" * (3 * 1024 * 1024), 1000.0)
    image = "e2b-local/zf7_capped"
    payload, digest = _oci_layout_tar(
        [_layer({"bin/sh": b"#!/bin/sh\n", "etc/zf7": b"capped\n"})]
    )
    _write_local_oci(cache, image, payload)
    # The cap is below the 3 MiB older entry, so eviction has to happen.
    monkeypatch.setenv("E2B_IMAGE_CACHE_MAX_BYTES", str(2 * 1024 * 1024))
    monkeypatch.setenv("E2B_IMAGE_CACHE_EVICT_MIN_AGE_S", "0")
    monkeypatch.setattr(image_resolver, "_last_prune_monotonic", 0.0)

    rootfs = resolve_image_rootfs(image, cache)

    entry = f"e2b-local_zf7_capped-{digest[7:47]}"
    assert rootfs == cache / entry / "rootfs"
    assert (rootfs / ".complete").is_file()
    assert (rootfs / "etc" / "zf7").read_bytes() == b"capped\n"
    assert _entry_names(cache) == sorted(
        ["_oci", "e2b-local_zf7_capped.lock", entry]
    )


def test_cap_of_zero_keeps_the_shared_cache_unbounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``0`` disables eviction: the resolver must not touch an existing entry."""
    cache = tmp_path / "shared" / "_images"
    cache.mkdir(parents=True)
    old_rootfs = _write_entry(cache, "old-1111", b"o" * (3 * 1024 * 1024), 1000.0)
    image = "e2b-local/zf7_unbounded"
    payload, digest = _oci_layout_tar(
        [_layer({"bin/sh": b"#!/bin/sh\n", "etc/zf7": b"unbounded\n"})]
    )
    _write_local_oci(cache, image, payload)
    monkeypatch.setenv("E2B_IMAGE_CACHE_MAX_BYTES", "0")
    monkeypatch.setattr(image_resolver, "_last_prune_monotonic", 0.0)

    rootfs = resolve_image_rootfs(image, cache)

    assert (old_rootfs / ".complete").is_file()
    entry = f"e2b-local_zf7_unbounded-{digest[7:47]}"
    assert _entry_names(cache) == sorted(
        ["_oci", "e2b-local_zf7_unbounded.lock", "old-1111", entry]
    )
    assert resolve_image_rootfs(image, cache) == rootfs


# --- Z-F7 rework: C1..C5 -----------------------------------------------------
#
# The first cut of the shared cache was reviewed (`.superpowers/sdd/
# task-zf7-review.md`) and five properties were missing: two uids sharing one
# cache lock each other out (C1), eviction can take the rootfs out from under a
# running sandbox (C2) while not even accounting for what fills the disk (C3),
# the publish path can delete an entry another process just completed (C4), and
# the cross-process lock has no timeout while a floor of ``0`` lets a process
# evict its own fresh entry (C5). The tests below pin each of them.

_MIB = 1024 * 1024


def _write_sandbox_record(
    base: Path, sandbox_id: str, image: str, *, digest: str | None = None
) -> Path:
    """A ``sandbox.json`` as the registry persists it inside the sandbox tree."""
    record = base / sandbox_id / "sandbox.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "sandbox_id": sandbox_id,
        "access_token": "zf7-token",
        "workspace_dir": str(base / sandbox_id),
        "base_image": image,
    }
    if digest is not None:
        payload["base_image_digest"] = digest
    record.write_text(json.dumps(payload), encoding="utf-8")
    return record


def test_the_shared_cache_is_owner_writable_only(tmp_path: Path) -> None:
    """C1: the cache is shared by the control plane (root) and the worker
    (65534) and stays *read-only* for everyone else -- a sandbox uid that can
    write a cache entry can poison every other sandbox's rootfs, which is why
    "just chmod 777 the cache" is not an option."""
    cache = tmp_path / "shared" / "_images"
    cache.mkdir(parents=True)
    image = "e2b-local/zf7_modes"
    payload, digest = _oci_layout_tar(
        [_layer({"bin/sh": b"#!/bin/sh\n", "etc/zf7": b"modes\n"})]
    )
    _write_local_oci(cache, image, payload)
    rootfs = resolve_image_rootfs(image, cache)
    entry = cache / f"e2b-local_zf7_modes-{digest[7:47]}"
    lock = cache / "e2b-local_zf7_modes.lock"
    link = cache / "_oci" / "e2b-local_zf7_modes.link"

    assert stat.S_IMODE(cache.stat().st_mode) == 0o755
    assert stat.S_IMODE((cache / "_oci").stat().st_mode) == 0o755
    assert stat.S_IMODE(entry.stat().st_mode) == 0o755
    assert stat.S_IMODE(rootfs.stat().st_mode) == 0o755
    assert stat.S_IMODE(lock.stat().st_mode) == 0o644
    assert stat.S_IMODE(link.stat().st_mode) == 0o644
    for path in (cache, cache / "_oci", entry, rootfs, lock, link):
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode & stat.S_IWGRP == 0, path
        assert mode & stat.S_IWOTH == 0, path


def test_a_world_writable_cache_directory_is_tightened(tmp_path: Path) -> None:
    """C1: a directory a previous deployment left 0777 is re-tightened, not
    inherited (the resolver re-asserts the shared-cache contract every time)."""
    cache = tmp_path / "_images"
    cache.mkdir()
    os.chmod(cache, 0o777)

    image_resolver._ensure_shared_dir(cache)

    assert stat.S_IMODE(cache.stat().st_mode) == 0o755


def test_the_cache_owner_comes_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C1: the manifests name the uid the cache belongs to, so a root-run
    process hands every artifact it creates to the worker."""
    monkeypatch.setenv("E2B_IMAGE_CACHE_OWNER_UID", "65534")
    monkeypatch.delenv("E2B_IMAGE_CACHE_OWNER_GID", raising=False)
    assert image_resolver._cache_owner_ids(tmp_path) == (65534, 65534)

    monkeypatch.setenv("E2B_IMAGE_CACHE_OWNER_GID", "65535")
    assert image_resolver._cache_owner_ids(tmp_path) == (65534, 65535)


def test_the_cache_owner_falls_back_to_the_volume_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C1: with no explicit uid the cache inherits the owner of the volume --
    the worker uid that owns ``/var/lib/e2b-sandboxes`` in production."""
    monkeypatch.delenv("E2B_IMAGE_CACHE_OWNER_UID", raising=False)
    monkeypatch.delenv("E2B_IMAGE_CACHE_OWNER_GID", raising=False)
    expected = None
    for ancestor in (tmp_path, *tmp_path.parents):
        info = ancestor.stat()
        if info.st_uid != 0:
            expected = (info.st_uid, info.st_gid)
            break
    assert image_resolver._cache_owner_ids(tmp_path / "deeper" / "still") == expected


def test_a_lock_file_the_other_uid_owns_still_locks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C1: a lock file that can only be read (an older resolver created it
    ``0600`` for one uid) must not lock the other uid out of the image --
    ``flock`` works on a read-only descriptor, so the open is retried."""
    cache = tmp_path / "_images"
    cache.mkdir()
    lock = cache / "img.lock"
    lock.write_bytes(b"")
    real_open = os.open
    calls: list[tuple[str, int]] = []

    def fake_open(path, flags, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        calls.append((str(path), flags))
        if str(path) == str(lock) and flags & os.O_RDWR:
            raise PermissionError(13, "Permission denied", str(path))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(image_resolver.os, "open", fake_open)
    with image_resolver._locked_cache_entry(cache, "img"):
        pass

    assert calls == [
        (str(lock), os.O_RDWR | os.O_CREAT | os.O_CLOEXEC),
        (str(lock), os.O_RDONLY | os.O_CLOEXEC),
    ]


def test_eviction_never_takes_an_entry_a_sandbox_references(tmp_path: Path) -> None:
    """C2: eviction must leave the rootfs of every image a ``sandbox.json``
    references alone -- taking it breaks (silently) every new command in every
    sandbox using that image, which the production ``MIN_AGE=300`` does not
    prevent."""
    base = tmp_path / "e2b-sandboxes"
    cache = base / "_images"
    cache.mkdir(parents=True)
    referenced = "python/3.11-slim"
    _write_sandbox_record(base, "sbx_in_use", referenced)
    pinned_entry = f"{image_resolver._image_cache_name(referenced)}-a1b2c3d4"
    pinned_rootfs = _write_entry(cache, pinned_entry, b"r" * (1 * _MIB), 1000.0)
    spare_entry = "node_22-slim-d4c3b2a1"
    _write_entry(cache, spare_entry, b"u" * (3 * _MIB), 2000.0)

    stats = image_resolver.prune_image_cache(cache, max_bytes=1, min_age_s=0.0)

    # The referenced entry is the *oldest* one and survives anyway; only the
    # image no record references is evicted.
    assert stats["evicted"] == 1
    assert stats["skipped_pinned"] == 1
    assert stats["skipped_fresh"] == 0
    assert stats["kept_bytes"] == image_resolver._tree_bytes(cache / pinned_entry)
    assert (pinned_rootfs / ".complete").is_file()
    assert not (cache / spare_entry).exists()
    assert sorted(p.name for p in cache.iterdir()) == [pinned_entry]


def test_a_recorded_digest_pins_exactly_one_entry(tmp_path: Path) -> None:
    """C2: when the record carries the resolved digest, only that entry is
    pinned -- other digests of the same image stay evictable."""
    base = tmp_path / "e2b-sandboxes"
    cache = base / "_images"
    cache.mkdir(parents=True)
    image = "python/3.12-slim"
    digest = "sha256:" + "5f4dcc3b5aa765d61d8327deb882cf99" + "0" * 32
    _write_sandbox_record(base, "sbx_digest", image, digest=digest)
    pinned_entry = image_resolver._entry_name(image, digest)
    _write_entry(cache, pinned_entry, b"p" * (1 * _MIB), 1000.0)
    other_entry = f"{image_resolver._image_cache_name(image)}-{'1' * 40}"
    _write_entry(cache, other_entry, b"o" * (3 * _MIB), 2000.0)

    stats = image_resolver.prune_image_cache(cache, max_bytes=1, min_age_s=0.0)

    assert stats["evicted"] == 1
    assert stats["skipped_pinned"] == 1
    assert sorted(p.name for p in cache.iterdir()) == [pinned_entry]


def test_the_default_bound_evicts_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C2: with no bound configured the resolver does not evict at all --
    eviction is an explicit operational decision (``0``/unset = unbounded)."""
    monkeypatch.delenv("E2B_IMAGE_CACHE_MAX_BYTES", raising=False)
    assert image_resolver._cache_max_bytes() == 0
    base = tmp_path / "e2b-sandboxes"
    cache = base / "_images"
    cache.mkdir(parents=True)
    for name, mtime in (("old-1111", 1000.0), ("new-2222", 2000.0)):
        _write_entry(cache, name, b"x" * (3 * _MIB), mtime)

    stats = image_resolver.prune_image_cache(cache, min_age_s=0.0)

    assert stats["max_bytes"] == 0
    assert stats["evicted"] == 0
    assert stats["freed_bytes"] == 0
    assert stats["total_bytes"] == image_resolver._tree_bytes(cache)
    assert sorted(p.name for p in cache.iterdir()) == ["new-2222", "old-1111"]


def test_a_cold_resolve_keeps_the_entry_it_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C5: ``E2B_IMAGE_CACHE_EVICT_MIN_AGE_S=0`` used to let the GC pass that
    follows a publish evict that very entry and return a path that no longer
    exists; the entry this call published is exempt and the floor is positive.
    """
    cache = tmp_path / "shared" / "_images"
    cache.mkdir(parents=True)
    image = "e2b-local/zf7_keep"
    payload, digest = _oci_layout_tar(
        [_layer({"bin/sh": b"#!/bin/sh\n", "payload": b"z" * (2 * _MIB)})]
    )
    _write_local_oci(cache, image, payload)
    monkeypatch.setenv("E2B_IMAGE_CACHE_MAX_BYTES", "1")
    monkeypatch.setenv("E2B_IMAGE_CACHE_EVICT_MIN_AGE_S", "0")
    monkeypatch.setattr(image_resolver, "_last_prune_monotonic", 0.0)

    rootfs = resolve_image_rootfs(image, cache)

    entry = cache / f"e2b-local_zf7_keep-{digest[7:47]}"
    assert rootfs == entry / "rootfs"
    assert (rootfs / ".complete").is_file()
    assert rootfs.exists()
    # A second call is a cache hit: nothing was evicted and re-extracted.
    assert resolve_image_rootfs(image, cache) == rootfs
    assert sorted(p.name for p in cache.iterdir() if not p.name.startswith(".")) == sorted(
        ["_oci", "e2b-local_zf7_keep.lock", entry.name]
    )


def test_the_freshness_floor_has_a_positive_lower_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C5: ``MIN_AGE=0`` in the environment is raised to the floor, so no
    configuration lets an eviction pass run right behind a publish."""
    monkeypatch.setenv("E2B_IMAGE_CACHE_EVICT_MIN_AGE_S", "0")
    assert image_resolver._cache_evict_min_age_s() == 60.0
    monkeypatch.setenv("E2B_IMAGE_CACHE_EVICT_MIN_AGE_S", "300")
    assert image_resolver._cache_evict_min_age_s() == 300.0


def test_prune_accounts_for_every_byte_and_reclaims_stale_leftovers(
    tmp_path: Path,
) -> None:
    """C3: the reported total is the cache's real disk usage -- staging trees
    (including what a ``SIGKILL`` leaves), incomplete entries, ``_oci`` tars and
    lock files included -- and only leftovers that are provably junk are
    reclaimed: a tree carrying this process's pid (and not being worked in), or
    one older than the staleness window. A fresh tree that belongs to another
    process is left alone for the whole staleness window."""
    cache = tmp_path / "cache"
    cache.mkdir()
    _write_entry(cache, "complete-1111", b"c" * (1 * _MIB), 1000.0)
    (cache / "_oci").mkdir()
    (cache / "_oci" / "tpl.oci.tar").write_bytes(b"t" * (2 * _MIB))
    own = cache / f".complete-1111.tmp-{os.getpid()}-own"
    (own / "rootfs").mkdir(parents=True)
    (own / "rootfs" / "big").write_bytes(b"f" * (1 * _MIB))
    stale = cache / ".complete-1111.tmp-4242-stale"
    (stale / "rootfs").mkdir(parents=True)
    (stale / "rootfs" / "big").write_bytes(b"s" * (3 * _MIB))
    os.utime(stale, (1000.0, 1000.0))
    foreign = cache / ".complete-1111.tmp-999999-foreign"
    (foreign / "rootfs").mkdir(parents=True)
    (foreign / "rootfs" / "big").write_bytes(b"n" * (1 * _MIB))
    incomplete = cache / "incomplete-2222" / "rootfs"
    incomplete.mkdir(parents=True)
    (incomplete / "big").write_bytes(b"i" * (2 * _MIB))
    lock = cache / "complete-1111.lock"
    lock.write_bytes(b"")
    stale_bytes = image_resolver._tree_bytes(stale) + image_resolver._tree_bytes(own)

    stats = image_resolver.prune_image_cache(cache, max_bytes=1, min_age_s=0.0)

    # Our own leftover and the stale one are reclaimed, the other process's
    # fresh tree stays, and the completed entry is evicted for the cap.
    assert sorted(p.name for p in cache.iterdir()) == sorted(
        [foreign.name, "incomplete-2222", "_oci", "complete-1111.lock"]
    )
    assert stats["evicted"] == 1
    assert stats["stale_removed"] == 2
    assert stats["stale_freed_bytes"] == stale_bytes
    assert stats["staging_bytes"] == image_resolver._tree_bytes(foreign)
    assert stats["incomplete_bytes"] == image_resolver._tree_bytes(
        cache / "incomplete-2222"
    )
    assert stats["oci_bytes"] == image_resolver._tree_bytes(cache / "_oci")
    assert stats["loose_bytes"] == image_resolver._own_bytes(lock)
    assert stats["kept_bytes"] == 0
    assert stats["total_bytes"] == image_resolver._tree_bytes(cache)


def test_an_active_staging_tree_is_never_reclaimed(tmp_path: Path) -> None:
    """C3: a staging tree this process is extracting into (another thread of
    the same worker) carries our pid but must survive its own GC pass."""
    cache = tmp_path / "cache"
    cache.mkdir()
    staging = image_resolver._stage_entry(cache, "entry-1111")
    (staging / "rootfs").mkdir()
    (staging / "rootfs" / "big").write_bytes(b"a" * (2 * _MIB))
    try:
        stats = image_resolver.prune_image_cache(cache, max_bytes=1, min_age_s=0.0)
        assert stats["stale_removed"] == 0
        assert staging.is_dir()
        assert stats["staging_bytes"] == image_resolver._tree_bytes(staging)
        assert stats["total_bytes"] == image_resolver._tree_bytes(cache)
    finally:
        image_resolver._release_staging(staging)


def test_the_accounted_total_matches_the_disk_when_the_cap_cannot_be_met(
    tmp_path: Path,
) -> None:
    """C3: with nothing evictable the cache still reports its real size, so
    "over the cap" is visible instead of silent (the old accounting stopped at
    ``kept + oci`` and read 15.8 MiB while the disk held 31.5 MiB)."""
    cache = tmp_path / "cache"
    cache.mkdir()
    staging = cache / ".x-1111.tmp-4242-big"
    (staging / "rootfs").mkdir(parents=True)
    (staging / "rootfs" / "big").write_bytes(b"s" * (4 * _MIB))
    os.utime(staging, (1000.0, 1000.0))
    incomplete = cache / "incomplete-2222" / "rootfs"
    incomplete.mkdir(parents=True)
    (incomplete / "big").write_bytes(b"i" * (2 * _MIB))

    stats = image_resolver.prune_image_cache(cache, max_bytes=1, min_age_s=0.0)

    assert stats["entries"] == 0
    assert stats["evicted"] == 0
    assert stats["stale_removed"] == 1
    assert stats["kept_bytes"] == 0
    assert stats["oci_bytes"] == 0
    assert stats["staging_bytes"] == 0
    assert stats["incomplete_bytes"] == image_resolver._tree_bytes(
        cache / "incomplete-2222"
    )
    assert stats["total_bytes"] == image_resolver._tree_bytes(cache)
    assert stats["total_bytes"] > stats["max_bytes"]


def test_another_processes_fresh_staging_tree_is_left_alone(tmp_path: Path) -> None:
    """C3: a staging tree that is not ours and is not over-age may belong to a
    live extraction in another worker, so GC never touches it."""
    cache = tmp_path / "cache"
    cache.mkdir()
    foreign = cache / ".x-1111.tmp-999999-fresh"
    (foreign / "rootfs").mkdir(parents=True)
    (foreign / "rootfs" / "big").write_bytes(b"f" * (2 * _MIB))

    stats = image_resolver.prune_image_cache(cache, max_bytes=0, min_age_s=0.0)

    assert stats["stale_removed"] == 0
    assert foreign.is_dir()
    assert stats["staging_bytes"] == image_resolver._tree_bytes(foreign)
    assert stats["total_bytes"] == image_resolver._tree_bytes(cache)


#: Run by ``sys.executable`` in a *separate process*: one publisher of the
#: un-honoured-lock shape. ``_remove_path`` is wrapped so the window between the
#: completeness check and the removal (the window the old code removed the
#: final entry in) is widened to a barrier both children wait on, i.e. two
#: publishers reach it together deterministically instead of by microsecond
#: luck, and every call that targets the final entry is recorded.
_PUBLISH_RACE_CHILD = r"""
import contextlib
import json
import os
import time
from pathlib import Path

from envd_service.runtime import image_resolver as ir

entry_name = os.environ["ZF7_ENTRY_NAME"]
reached = Path(os.environ["ZF7_REACHED"])
barrier = Path(os.environ["ZF7_BARRIER"])
events = Path(os.environ["ZF7_EVENTS"])
real_remove_path = ir._remove_path


def watched_remove_path(path):
    named = Path(path).name
    if named == entry_name:
        reached.write_text(str(os.getpid()), encoding="utf-8")
        deadline = time.monotonic() + 30.0
        while not barrier.exists() and time.monotonic() < deadline:
            time.sleep(0.005)
    existed = Path(path).exists()
    was_complete = (Path(path) / "rootfs" / ".complete").is_file()
    result = real_remove_path(path)
    if named == entry_name:
        with open(events, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"pid": os.getpid(), "target": named,
                                 "existed": existed,
                                 "was_complete": was_complete}) + "\n")
    return result


ir._remove_path = watched_remove_path
# Storage whose locks are not honoured across clients (an NFS ``nolock`` mount).
ir._locked_cache_entry = lambda cache, image, **kwargs: contextlib.nullcontext()

try:
    rootfs = ir.resolve_image_rootfs(os.environ["ZF7_IMAGE"], os.environ["ZF7_CACHE"])
except Exception as exc:  # noqa: BLE001 - the parent asserts on the shape
    print(json.dumps({"pid": os.getpid(), "error": type(exc).__name__,
                      "message": str(exc)}))
else:
    print(json.dumps({"pid": os.getpid(), "rootfs": str(rootfs),
                      "entry_ino": os.stat(Path(rootfs).parent).st_ino}))
"""


def test_a_publisher_never_removes_a_published_entry(tmp_path: Path) -> None:
    """C4: with the lock not honoured and an incomplete leftover of the same
    name, both publishers used to take the "remove the leftover" branch -- and
    the loser deleted the entry the winner had just published
    (``was_complete=True``), leaving readers with a half-eaten rootfs."""
    cache = tmp_path / "shared" / "_images"
    cache.mkdir(parents=True)
    image = "e2b-local/zf7_claim"
    payload, digest = _oci_layout_tar(
        [_layer({"bin/sh": b"#!/bin/sh\n", "etc/zf7": b"claim\n"})]
    )
    _write_local_oci(cache, image, payload)
    entry = f"e2b-local_zf7_claim-{digest[7:47]}"
    # The shape the new GC can leave behind too (a half-removed entry): the
    # name exists, the marker does not.
    (cache / entry / "rootfs").mkdir(parents=True)
    (cache / entry / "rootfs" / "partial.bin").write_bytes(b"garbage\n")

    reached = tmp_path / "reached"
    barrier = tmp_path / "barrier"
    events = tmp_path / "events.jsonl"
    env = _child_env(
        image=image,
        cache=cache,
        record=tmp_path / "extractions.txt",
        delay=0.2,
        nolock=True,
    )
    env.update(
        {
            "ZF7_ENTRY_NAME": entry,
            "ZF7_REACHED": str(reached),
            "ZF7_BARRIER": str(barrier),
            "ZF7_EVENTS": str(events),
        }
    )
    children = [
        _spawn_script(env, _PUBLISH_RACE_CHILD),
        _spawn_script(env, _PUBLISH_RACE_CHILD),
    ]
    time.sleep(1.0)  # both children are at the removal window if there is one
    barrier.write_text("go", encoding="utf-8")
    results = [_collect(child) for child in children]

    # Nothing ever removed the final entry name: the leftover is claimed with an
    # atomic rename and re-verified first, so a published entry is untouchable.
    assert reached.exists() is False
    assert events.exists() is False
    for result in results:
        assert "error" not in result, result
        assert result["rootfs"] == str(cache / entry / "rootfs")
    assert results[0]["entry_ino"] == results[1]["entry_ino"]
    rootfs = cache / entry / "rootfs"
    assert (rootfs / ".complete").read_text(encoding="utf-8") == "ok"
    assert (rootfs / "etc" / "zf7").read_bytes() == b"claim\n"
    assert (rootfs / "bin" / "sh").read_bytes() == b"#!/bin/sh\n"
    assert _entry_names(cache) == sorted(["_oci", entry])


#: Run by ``sys.executable``: resolves one image and reports a timeout of the
#: cross-process lock as data instead of a traceback.
_LOCK_TIMEOUT_CHILD = r"""
import json
import os
import time
from pathlib import Path

from envd_service.runtime import image_resolver as ir

hanging = os.environ.get("ZF7_HANG") == "1"
started = Path(os.environ["ZF7_STARTED"])
real_extract_layer = ir.extract_layer


def slow_extract_layer(blob, rootfs):
    started.write_text(str(os.getpid()), encoding="utf-8")
    if hanging:
        time.sleep(600)
    return real_extract_layer(blob, rootfs)


ir.extract_layer = slow_extract_layer

try:
    rootfs = ir.resolve_image_rootfs(os.environ["ZF7_IMAGE"], os.environ["ZF7_CACHE"])
except ir.CacheLockTimeout as exc:
    print(json.dumps({"pid": os.getpid(), "error": type(exc).__name__,
                      "message": str(exc), "timeout": exc.timeout,
                      "lock_path": str(exc.lock_path), "image": exc.image}))
except Exception as exc:  # noqa: BLE001 - the parent asserts on the shape
    print(json.dumps({"pid": os.getpid(), "error": type(exc).__name__,
                      "message": str(exc)}))
else:
    print(json.dumps({"pid": os.getpid(), "rootfs": str(rootfs)}))
"""


def test_the_cache_lock_times_out_instead_of_blocking_forever(tmp_path: Path) -> None:
    """C5: a worker wedged inside an extraction blocks every other resolver of
    that image (the old in-process lock could not do that across processes).
    The waiter gives up at the deadline with a clear error instead."""
    cache = tmp_path / "shared" / "_images"
    cache.mkdir(parents=True)
    image = "e2b-local/zf7_locktimeout"
    payload, _digest = _oci_layout_tar(
        [_layer({"bin/sh": b"#!/bin/sh\n", "etc/zf7": b"lock\n"})]
    )
    _write_local_oci(cache, image, payload)
    record = tmp_path / "extractions.txt"
    holder_env = _child_env(image=image, cache=cache, record=record)
    holder_env.update(
        {"ZF7_HANG": "1", "ZF7_STARTED": str(tmp_path / "holder-started")}
    )
    holder = _spawn_script(holder_env, _LOCK_TIMEOUT_CHILD)
    deadline = time.monotonic() + 30.0
    while (
        not (tmp_path / "holder-started").exists() and time.monotonic() < deadline
    ):
        time.sleep(0.02)
    assert (tmp_path / "holder-started").exists()

    waiter_env = _child_env(image=image, cache=cache, record=record)
    waiter_env.update(
        {
            "ZF7_HANG": "0",
            "ZF7_STARTED": str(tmp_path / "waiter-started"),
            "E2B_IMAGE_CACHE_LOCK_TIMEOUT_S": "0.6",
        }
    )
    started = time.monotonic()
    result = _collect(_spawn_script(waiter_env, _LOCK_TIMEOUT_CHILD))
    elapsed = time.monotonic() - started
    holder.kill()
    holder.communicate(timeout=30)

    lock = cache / "e2b-local_zf7_locktimeout.lock"
    assert result["error"] == "CacheLockTimeout"
    assert result["timeout"] == 0.6
    assert result["lock_path"] == str(lock)
    assert result["image"] == image
    assert result["message"] == (
        f"timed out after 0.6s waiting for another process to finish resolving "
        f"image {image} (lock {lock}; raise E2B_IMAGE_CACHE_LOCK_TIMEOUT_S, or "
        f"set it to 0 to wait forever)"
    )
    assert elapsed < 10.0


#: Run by ``sys.executable``: publishes the entry from *inside* the lock and
#: then keeps holding the lock (the shape a slow publisher has). It publishes
#: only once the waiter is *at* the lock, so the waiter's pre-lock cache check
#: has already missed and the timeout path is the one under test.
_LOCK_PUBLISH_HOLD_CHILD = r"""
import json
import os
import time
from pathlib import Path

from envd_service.runtime import image_resolver as ir

holding = Path(os.environ["ZF7_HOLDING"])
waiting = Path(os.environ["ZF7_WAITING"])
published = Path(os.environ["ZF7_PUBLISHED"])
record = Path(os.environ["ZF7_RECORD"])
hold = float(os.environ["ZF7_HOLD"])
real_publish_staged_entry = ir._publish_staged_entry
real_extract_layer = ir.extract_layer


def recording_extract_layer(blob, rootfs):
    with open(record, "a", encoding="utf-8") as fh:
        fh.write("extract %d\n" % os.getpid())
    return real_extract_layer(blob, rootfs)


def slow_publish_staged_entry(staging, entry):
    # Inside the lock now: the caller may start the waiter.
    holding.write_text(str(os.getpid()), encoding="utf-8")
    deadline = time.monotonic() + 30.0
    while not waiting.exists() and time.monotonic() < deadline:
        time.sleep(0.005)
    real_publish_staged_entry(staging, entry)
    published.write_text(str(os.getpid()), encoding="utf-8")
    time.sleep(hold)


ir.extract_layer = recording_extract_layer
ir._publish_staged_entry = slow_publish_staged_entry
rootfs = ir.resolve_image_rootfs(os.environ["ZF7_IMAGE"], os.environ["ZF7_CACHE"])
print(json.dumps({"pid": os.getpid(), "rootfs": str(rootfs)}))
"""

#: Run by ``sys.executable``: waits for the lock and reports whether it expired
#: while waiting; the entry published in that window has to be used.
_LOCK_WAIT_CHILD = r"""
import contextlib
import json
import os
from pathlib import Path

from envd_service.runtime import image_resolver as ir

armed = Path(os.environ["ZF7_ARMED"])
waiting = Path(os.environ["ZF7_WAITING"])
timed_out = Path(os.environ["ZF7_TIMED_OUT"])
real_locked_cache_entry = ir._locked_cache_entry


@contextlib.contextmanager
def watched_locked_cache_entry(cache, image, **kwargs):
    waiting.write_text(str(os.getpid()), encoding="utf-8")
    try:
        with real_locked_cache_entry(cache, image, **kwargs):
            yield
    except ir.CacheLockTimeout:
        timed_out.write_text("1", encoding="utf-8")
        raise


ir._locked_cache_entry = watched_locked_cache_entry
armed.write_text(str(os.getpid()), encoding="utf-8")

try:
    rootfs = ir.resolve_image_rootfs(os.environ["ZF7_IMAGE"], os.environ["ZF7_CACHE"])
except Exception as exc:  # noqa: BLE001 - the parent asserts on the shape
    print(json.dumps({"pid": os.getpid(), "error": type(exc).__name__,
                      "message": str(exc)}))
else:
    print(json.dumps({"pid": os.getpid(), "rootfs": str(rootfs),
                      "timed_out_while_waiting": timed_out.exists()}))
"""


def test_a_lock_timeout_uses_the_entry_published_while_waiting(tmp_path: Path) -> None:
    """C5: the deadline is not a failure when the holder published the entry in
    the meantime -- the resolver re-checks the marker and returns the cache hit
    instead of erroring (or handing back a path nobody finished)."""
    cache = tmp_path / "shared" / "_images"
    cache.mkdir(parents=True)
    image = "e2b-local/zf7_late_publish"
    payload, digest = _oci_layout_tar(
        [_layer({"bin/sh": b"#!/bin/sh\n", "etc/zf7": b"late\n"})]
    )
    _write_local_oci(cache, image, payload)
    record = tmp_path / "extractions.txt"
    armed = tmp_path / "armed"
    waiting = tmp_path / "waiting"
    holding = tmp_path / "holding"
    holder_env = _child_env(image=image, cache=cache, record=record)
    holder_env.update(
        {
            "ZF7_HOLDING": str(holding),
            "ZF7_WAITING": str(waiting),
            "ZF7_PUBLISHED": str(tmp_path / "published"),
            "ZF7_HOLD": "4",
        }
    )
    holder = _spawn_script(holder_env, _LOCK_PUBLISH_HOLD_CHILD)
    deadline = time.monotonic() + 30.0
    while not holding.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert holding.exists()

    waiter_env = _child_env(image=image, cache=cache, record=record)
    waiter_env.update(
        {
            "ZF7_ARMED": str(armed),
            "ZF7_WAITING": str(waiting),
            "ZF7_TIMED_OUT": str(tmp_path / "timed_out"),
            "E2B_IMAGE_CACHE_LOCK_TIMEOUT_S": "1.5",
        }
    )
    waiter = _spawn_script(waiter_env, _LOCK_WAIT_CHILD)
    holder_result = _collect(holder)
    waiter_result = _collect(waiter)

    entry = cache / f"e2b-local_zf7_late_publish-{digest[7:47]}"
    assert waiter_result["timed_out_while_waiting"] is True
    assert (tmp_path / "timed_out").exists()
    assert waiter_result["rootfs"] == str(entry / "rootfs")
    assert waiter_result["rootfs"] == holder_result["rootfs"]
    assert (entry / "rootfs" / ".complete").read_text(encoding="utf-8") == "ok"
    assert (entry / "rootfs" / "etc" / "zf7").read_bytes() == b"late\n"
    # One extraction, two callers: the waiter reused what the holder published.
    assert _extraction_pids(record) == [holder_result["pid"]]


def test_a_configured_shared_cache_is_prepared_at_settings_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C1: the control plane exports template layout tars into
    ``_images/_oci/`` *without* going through the resolver, so a cache the
    operator configured explicitly is created (with its ``_oci``
    subdirectory) as soon as the settings are built -- before either uid
    writes, and with the shared-cache contract."""
    from envd_service.config import Settings

    cache = tmp_path / "e2b-sandboxes" / "_images"
    monkeypatch.setenv("E2B_IMAGE_CACHE_DIR", str(cache))

    settings = Settings()

    assert settings.image_cache_dir == cache.resolve()
    assert stat.S_IMODE(cache.stat().st_mode) == 0o755
    assert stat.S_IMODE((cache / "_oci").stat().st_mode) == 0o755


def test_the_default_cache_path_is_not_created_by_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C1: with no explicit configuration nothing is created or changed --
    local development stays all-one-uid, as before."""
    from envd_service.config import Settings

    monkeypatch.delenv("E2B_IMAGE_CACHE_DIR", raising=False)
    monkeypatch.chdir(tmp_path)

    settings = Settings()

    assert settings.image_cache_dir == (tmp_path / "tmp" / "sandboxes" / "_images").resolve()
    assert not (tmp_path / "tmp").exists()
