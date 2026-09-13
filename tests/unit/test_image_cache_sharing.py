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
            "ZF7_NOLOCK": "1" if nolock else "",
        }
    )
    return env


def _spawn(env: dict[str, str]) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", _CHILD],
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
