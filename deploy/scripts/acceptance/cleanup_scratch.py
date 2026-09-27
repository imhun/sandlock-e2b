"""One-shot scratch cleanup: delete regenerable test scratch under tmp/, keep
anything the docs cite as evidence. Dry run by default; --go to delete."""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = ROOT / "tmp"
IMAGES = TMP / "sandboxes" / "_images"
KEEP_FOREVER = {  # not pullable from the mirrors any more, or documented
    "stale-20260902",       # G2 forensic dir (docs say: delete only after confirm)
    "testenv", "review-venv",  # documented local venvs
    "perf", "wheel-context", "f30-all.done", "final-verify.sh", "cleanup_scratch.py",
}


def cited_names() -> set[str]:
    names: set[str] = set()
    docs = list(ROOT.glob("docs/**/*.md")) + [ROOT / "README.md", ROOT / "spec.md"]
    pattern = re.compile(r"tmp/([A-Za-z0-9_.\-]+)")
    for doc in docs:
        try:
            text = doc.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        names.update(pattern.findall(text))
    return names


def size(paths) -> int:
    total = 0
    for p in paths:
        out = subprocess.run(["du", "-sk", str(p)], capture_output=True, text=True)
        if out.stdout.strip():
            total += int(out.stdout.split()[0])
    return total


def main(go: bool) -> None:
    keep = cited_names() | KEEP_FOREVER
    groups: dict[str, list[Path]] = {}

    # Tier 1: per-run template image caches (rootfs dirs + their OCI tars).
    groups["images/tpl rootfs"] = sorted(
        p for p in IMAGES.glob("*_tpl_*") if p.is_dir()
    )
    groups["images/tpl oci"] = sorted((IMAGES / "_oci").glob("*_tpl_*"))
    # Tier 2: scratch directories, skipped when a doc cites the name.
    dirs = [
        p for p in TMP.iterdir()
        if p.is_dir() and p.name not in keep and p.name not in KEEP_FOREVER
        and re.match(
            r"^(__pycache__|test-runtime|cold-cache-|buildkit-test-|diag-net-|"
            r"e5-nonroot-|worker-nonroot-|wt-|chk|chown-|f11-|f14-|f15-|f23g|f23-|"
            r"dev-e7|e6-nfs|pre-e9|quarantine-|multinode|registry|tmp|.*\.stale-)",
            p.name,
        )
    ]
    groups["scratch dirs"] = sorted(dirs)
    # Tier 3: test-runtime contents live inside the dir the conftest expects.
    tr = TMP / "test-runtime"
    groups["test-runtime children"] = sorted(tr.iterdir()) if tr.is_dir() else []
    # Tier 4: this session's throwaway helpers (never doc-cited).
    junk = [
        p for p in TMP.iterdir()
        if p.is_file() and p.name not in keep
        and re.match(
            r"^(write_tpl_iso.*|write_design\.py|probe_launch_err.*|p1\.patch|"
            r"uv\.lock\..*|tpliso-.*|rb-f19-.*|rb-f20-.*|rb-f21-.*|rb-f22-macos\.log|"
            r"sec-f19.*|prod-shaped-f2\d\.done|e2b-gates-f19\.done|"
            r"prod-lane-inner.*|run-prod-lane-f20\.sh|run-e2b-gates-f19\.sh|"
            r"run-e2b-gates-f20\.sh|gate-a-only\.sh|gate-b-only\.sh|skips-.*\.txt|"
            r"final-verify\.out|tmp.*\.py|unprivileged_userns_probe\.py)$",
            p.name,
        )
    ]
    groups["session files"] = sorted(junk)

    total_kb = 0
    for label, paths in groups.items():
        if not paths:
            continue
        kb = size(paths)
        total_kb += kb
        print(f"{label:24s} {len(paths):5d} 项  {kb / 1024 / 1024:7.2f} GB")
    print(f"{'合计可回收':24s} {sum(len(v) for v in groups.values()):5d} 项  "
          f"{total_kb / 1024 / 1024:7.2f} GB")
    if not go:
        print("\nDRY RUN（加 --go 才删）；保留：文档引用的 tmp/*、stale-20260902 取证目录、"
              "base 镜像缓存（python-mcp:3.14 已不在镜像站白名单里）、perf/、wheel-context/")
        return
    for paths in groups.values():
        for p in paths:
            subprocess.run(["rm", "-rf", "--", str(p)], check=False)
    print("\n已删除。")


if __name__ == "__main__":
    main("--go" in sys.argv)
