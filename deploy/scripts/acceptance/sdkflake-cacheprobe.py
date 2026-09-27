"""Measure the image-cache maintenance walk that sits inside the first-command path.

Times ``_cache_usage`` (the whole-cache ``os.walk``) and a full
``prune_image_cache`` on the lane's warm cache, plus the raw file count, so the
claim "the stall is O(cache files) work" is a measurement, not a reading.

Usage: python tmp/sdkflake-cacheprobe.py [cache-dir]
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from envd_service.runtime import image_resolver  # noqa: E402


def main() -> int:
    cache = Path(sys.argv[1] if len(sys.argv) > 1 else "tmp/sandboxes/_images")
    print(f"cache={cache} exists={cache.is_dir()}")

    files = dirs = 0
    started = time.monotonic()
    for _root, subdirs, names in os.walk(cache):
        dirs += len(subdirs)
        files += len(names)
    walk_s = time.monotonic() - started
    print(f"raw os.walk: files={files} dirs={dirs} secs={walk_s:.3f}")

    started = time.monotonic()
    usage = image_resolver._cache_usage(cache)
    usage_s = time.monotonic() - started
    print(
        f"_cache_usage: secs={usage_s:.3f} complete={len(usage['complete'])} "
        f"staging={len(usage['staging'])} incomplete={len(usage['incomplete'])} "
        f"total={image_resolver._usage_total(usage)}"
    )

    started = time.monotonic()
    stats = image_resolver.prune_image_cache(cache, max_bytes=0, min_age_s=0.0)
    prune_s = time.monotonic() - started
    print(
        f"prune_image_cache: secs={prune_s:.3f} total_bytes={stats['total_bytes']} "
        f"evicted={stats['evicted']} stale_removed={stats['stale_removed']} "
        f"refused={stats['eviction_refused']!r}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
