"""Docs must not send a reader to a `tmp/` script.

Most of this repo's acceptance evidence used to live in `tmp/` (gitignored) and
`.superpowers/sdd/` (gitignored) while `docs/**` told the reader to go run it *by
name*. `tmp/` gets cleaned, and a fresh checkout has neither file, so the sentence
"how to run this again" was the first thing to rot -- one round of it produced an
acceptance table that only existed inside a gitignored report. The fixed tools now
live in `deploy/scripts/acceptance/` (reports: `docs/reports/`) and the reports in
`docs/reports/`.

The first version of this pin only asserted *membership*: a live doc citing a
`tmp/` path was fine as long as the path was either `PROMOTED` (a repo copy
existed) or explicitly allowed. That was too weak in a specific way -- a live doc
could keep pointing at `tmp/` forever and stay green, because "a repo copy exists
somewhere" is not the same as "the doc points at it". The rule is now:

* `PROMOTED` is a *deny list*. A live doc citing one of those old `tmp/` paths is
  red, and the failure text names the promoted path to point it at instead. The
  point of the promotion was to change the reference, not to bless the old one.
* `ALLOWED_TMP_REFERENCES` keeps only the paths that genuinely cannot be
  reproduced from today's tree -- one-off diagnostics, wrappers whose payloads
  were never in the reference set, or files that no longer exist at all. Each one
  carries a one-line reason, and an allowance that no live doc cites any more is
  itself an error (a stale excuse is how the weak version would come back).

What is asserted:

* no live doc cites a `PROMOTED` old `tmp/` path (the deny list);
* every `tmp/**.py|sh` path a live doc cites is in `ALLOWED_TMP_REFERENCES`;
* every allowance is still cited by a live doc, and carries a one-line reason;
* every promoted *source* is gone from `tmp/` and every promoted *target* is a
  file in the repo -- "promoted" means moved, not copied;
* every `deploy/scripts/acceptance/*.py|sh` path a live doc cites exists on disk,
  so the replacement reference is checked the same way the old one is.

`docs/reports/**` is deliberately *not* part of the pin: those files are byte-exact
copies of historical work notes, so their `tmp/` mentions are the past narrated,
not instructions. They are still scanned (see
`test_the_frozen_archive_is_not_live_docs`) so that this is a decision on the
record rather than a glob accident.

Falsifiability -- four mutations, each run against the tree of the commit that
wrote this file, red output pasted into
`.superpowers/sdd/artifact-promotion-round2-report.md`:

* appending `see tmp/foo-probe.py` to a live doc: the uncited, unpinned path is
  named and the allowance comparison fails;
* writing a promoted old path back into a live doc (the run used
  `tmp/k0s/gateB-full.sh`): the deny list names it and the promoted path;
* deleting a still-cited allowance (`tmp/k0s/tools.sh`): the allowance comparison
  fails and names that path;
* renaming a cited `deploy/scripts/acceptance/` script (`capacity_check.py`): the
  existence check for the repo-side reference fails.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
DOCS = REPO / "docs"
FROZEN_ARCHIVE = DOCS / "reports"

#: Anchored so a repo path is never mistaken for a tmp one: `deploy/scripts/
#: acceptance/x.py` and `docs/reports/x.md` contain no `tmp/` segment, and the
#: lookbehind keeps `foo/tmp/x.py` (a path *outside* this repo) out of the set.
TMP_SCRIPT = re.compile(r"(?<![A-Za-z0-9_])tmp/[A-Za-z0-9._/-]+\.(?:py|sh)")

#: A reference to the promoted tree. Anchored the same way, so `tmp/…` cannot
#: satisfy it and a bare `capacity_check.py` (no directory) is not a reference.
ACCEPTANCE_SCRIPT = re.compile(
    r"(?<![A-Za-z0-9_])deploy/scripts/acceptance/[A-Za-z0-9._/-]+\.(?:py|sh)"
)

#: deny list: old (gitignored) path -> where the byte-exact copy lives now. A
#: live doc citing the left-hand side is red; the failure says to cite the right.
#: 78 entries: the 67 from the first promotion round, the 10 fixed tools of the
#: second round, and `checkpoint_acceptance.py` (whose old 437-line copy was a
#: duplicate of the 632-line `deploy/scripts/checkpoint_acceptance.py`).
PROMOTED_ENTRIES = (
    ("tmp/arm-vm/run.sh", "deploy/scripts/acceptance/run.sh"),
    ("tmp/capacity_check.py", "deploy/scripts/acceptance/capacity_check.py"),
    ("tmp/cleanup_scratch.py", "deploy/scripts/acceptance/cleanup_scratch.py"),
    ("tmp/e5e8/guard_probe.py", "deploy/scripts/acceptance/guard_probe.py"),
    ("tmp/e5e8/guard_probe2.py", "deploy/scripts/acceptance/guard_probe2.py"),
    ("tmp/f11_direct_exec_probe.py", "deploy/scripts/acceptance/f11_direct_exec_probe.py"),
    ("tmp/f11_fdcount_probe.py", "deploy/scripts/acceptance/f11_fdcount_probe.py"),
    ("tmp/f11_fup3_probe.py", "deploy/scripts/acceptance/f11_fup3_probe.py"),
    ("tmp/f23_multi_probe.py", "deploy/scripts/acceptance/f23_multi_probe.py"),
    ("tmp/final-verify.sh", "deploy/scripts/acceptance/final-verify.sh"),
    ("tmp/k0s/checkpoint_acceptance.py", "deploy/scripts/checkpoint_acceptance.py"),
    ("tmp/k0s/cpu_activity_acceptance.py", "deploy/scripts/acceptance/cpu_activity_acceptance.py"),
    ("tmp/k0s/gateA-full.sh", "deploy/scripts/acceptance/gateA-full.sh"),
    ("tmp/k0s/gateB-full.sh", "deploy/scripts/acceptance/gateB-full.sh"),
    ("tmp/k0s/gateB-pure-rootfs.sh", "deploy/scripts/acceptance/gateB-pure-rootfs.sh"),
    ("tmp/k0s/mmap-probe.py", "deploy/scripts/acceptance/mmap-probe.py"),
    ("tmp/k0s/n27-t7-lane.sh", "deploy/scripts/acceptance/n27-t7-lane.sh"),
    ("tmp/k0s/n35-lane.sh", "deploy/scripts/acceptance/n35-lane.sh"),
    ("tmp/k0s/n42-egress-probe.py", "deploy/scripts/acceptance/n42-egress-probe.py"),
    ("tmp/k0s/node-mmap-storage.sh", "deploy/scripts/acceptance/node-mmap-storage.sh"),
    ("tmp/k0s/overlay-probe.sh", "deploy/scripts/acceptance/overlay-probe.sh"),
    ("tmp/k0s/phase1-probe2.sh", "deploy/scripts/acceptance/phase1-probe2.sh"),
    ("tmp/k0s/phase2.sh", "deploy/scripts/acceptance/phase2.sh"),
    ("tmp/k0s/probe_127_errno.py", "deploy/scripts/acceptance/probe_127_errno.py"),
    ("tmp/k0s/probe_brief_stat_live.py", "deploy/scripts/acceptance/probe_brief_stat_live.py"),
    ("tmp/k0s/probe_ceiling_completeness.py", "deploy/scripts/acceptance/probe_ceiling_completeness.py"),
    ("tmp/k0s/probe_copy_range_zero.py", "deploy/scripts/acceptance/probe_copy_range_zero.py"),
    ("tmp/k0s/probe_dir_cost.py", "deploy/scripts/acceptance/probe_dir_cost.py"),
    ("tmp/k0s/probe_dir_ledger.py", "deploy/scripts/acceptance/probe_dir_ledger.py"),
    ("tmp/k0s/probe_dir_stsize.py", "deploy/scripts/acceptance/probe_dir_stsize.py"),
    ("tmp/k0s/probe_disk_metric_agreement.py", "deploy/scripts/acceptance/probe_disk_metric_agreement.py"),
    ("tmp/k0s/probe_etxtbsy_shape.py", "deploy/scripts/acceptance/probe_etxtbsy_shape.py"),
    ("tmp/k0s/probe_exec_limit.py", "deploy/scripts/acceptance/probe_exec_limit.py"),
    ("tmp/k0s/probe_kernel_copy.py", "deploy/scripts/acceptance/probe_kernel_copy.py"),
    ("tmp/k0s/probe_landlock_execveat.py", "deploy/scripts/acceptance/probe_landlock_execveat.py"),
    ("tmp/k0s/probe_mmap_growth.py", "deploy/scripts/acceptance/probe_mmap_growth.py"),
    ("tmp/k0s/probe_n28_acceptance.py", "deploy/scripts/acceptance/probe_n28_acceptance.py"),
    ("tmp/k0s/probe_n29_sync.py", "deploy/scripts/acceptance/probe_n29_sync.py"),
    ("tmp/k0s/probe_n35_exec_gate.py", "deploy/scripts/acceptance/probe_n35_exec_gate.py"),
    ("tmp/k0s/probe_n35_ns.py", "deploy/scripts/acceptance/probe_n35_ns.py"),
    ("tmp/k0s/probe_n35_realmount.py", "deploy/scripts/acceptance/probe_n35_realmount.py"),
    ("tmp/k0s/probe_openat2_eagain.py", "deploy/scripts/acceptance/probe_openat2_eagain.py"),
    ("tmp/k0s/probe_push_and_tighten.py", "deploy/scripts/acceptance/probe_push_and_tighten.py"),
    ("tmp/k0s/probe_restore_state.py", "deploy/scripts/acceptance/probe_restore_state.py"),
    ("tmp/k0s/probe_write_paths.py", "deploy/scripts/acceptance/probe_write_paths.py"),
    ("tmp/k0s/probe-pure-realroot.py", "deploy/scripts/acceptance/probe-pure-realroot.py"),
    ("tmp/k0s/probe-pure-restore-synthroot.sh", "deploy/scripts/acceptance/probe-pure-restore-synthroot.sh"),
    ("tmp/k0s/probe-pure-synth-root-plaindir.py", "deploy/scripts/acceptance/probe-pure-synth-root-plaindir.py"),
    ("tmp/k0s/probe-pure-synth-root.sh", "deploy/scripts/acceptance/probe-pure-synth-root.sh"),
    ("tmp/k0s/probe-pure-workload-census.py", "deploy/scripts/acceptance/probe-pure-workload-census.py"),
    ("tmp/k0s/probe_state_base_visibility.py", "deploy/scripts/acceptance/probe_state_base_visibility.py"),
    ("tmp/k0s/red-routeb-stderr-drain.py", "deploy/scripts/acceptance/red-routeb-stderr-drain.py"),
    ("tmp/k0s/sync-seccomp-installer.py", "deploy/scripts/acceptance/sync-seccomp-installer.py"),
    ("tmp/k0s/t1-ownership-probe.py", "deploy/scripts/acceptance/t1-ownership-probe.py"),
    ("tmp/k0s/x86-run-py.sh", "deploy/scripts/acceptance/x86-run-py.sh"),
    ("tmp/k0s/x86-security-one.sh", "deploy/scripts/acceptance/x86-security-one.sh"),
    ("tmp/ledger-arena-test.py", "deploy/scripts/acceptance/ledger-arena-test.py"),
    ("tmp/ledger-thread-cost.py", "deploy/scripts/acceptance/ledger-thread-cost.py"),
    ("tmp/mcp-3way.py", "deploy/scripts/acceptance/mcp-3way.py"),
    ("tmp/mcp-512-size.py", "deploy/scripts/acceptance/mcp-512-size.py"),
    ("tmp/mem512-limit.py", "deploy/scripts/acceptance/mem512-limit.py"),
    ("tmp/n37/cluster_keepalive_probe.py", "deploy/scripts/acceptance/cluster_keepalive_probe.py"),
    ("tmp/n37/cluster_run.py", "deploy/scripts/acceptance/cluster_run.py"),
    ("tmp/n37/relay_probe.py", "deploy/scripts/acceptance/relay_probe.py"),
    ("tmp/n39/n39-pool-pidns-probe2.py", "deploy/scripts/acceptance/n39-pool-pidns-probe2.py"),
    ("tmp/netns-node-compare.py", "deploy/scripts/acceptance/netns-node-compare.py"),
    ("tmp/pidns-cost-probe.py", "deploy/scripts/acceptance/pidns-cost-probe.py"),
    ("tmp/pidns-shape-probe.py", "deploy/scripts/acceptance/pidns-shape-probe.py"),
    ("tmp/rb_token_probe.py", "deploy/scripts/acceptance/rb_token_probe.py"),
    ("tmp/routeb_cap_probe.py", "deploy/scripts/acceptance/routeb_cap_probe.py"),
    ("tmp/run-f31.sh", "deploy/scripts/acceptance/run-f31.sh"),
    ("tmp/sdkflake-cacheprobe.py", "deploy/scripts/acceptance/sdkflake-cacheprobe.py"),
    ("tmp/sec-run-probe.sh", "deploy/scripts/acceptance/sec-run-probe.sh"),
    ("tmp/slot_cap_probe.py", "deploy/scripts/acceptance/slot_cap_probe.py"),
    ("tmp/task8_fup3_probe.py", "deploy/scripts/acceptance/task8_fup3_probe.py"),
    ("tmp/unprivileged_userns_probe.py", "deploy/scripts/acceptance/unprivileged_userns_probe.py"),
    ("tmp/verify-arena-live.py", "deploy/scripts/acceptance/verify-arena-live.py"),
    ("tmp/vol_fs_mount_probe.py", "deploy/scripts/acceptance/vol_fs_mount_probe.py"),
)

PROMOTED = dict(PROMOTED_ENTRIES)

#: References a live doc may *still* point at `tmp/`, one reason per line. These
#: are the ones with no repo copy and no honest way to make one: one-off
#: diagnostics whose conclusion is now a test, wrappers whose payloads were never
#: in the reference set, or files that are simply gone. Everything reproducible
#: is promoted and therefore denied above; an entry here is a statement that a
#: fresh checkout *cannot* run this, not a statement that it is fine to try.
ALLOWED_TMP_REFERENCES = {
    "tmp/a7-fix1-run.sh": "一次性复跑 runner（A7 fix round 1），依赖同轮临时文件 tmp/a7-run.sh，属一次性验证",
    "tmp/instance_probe.py": "一次性诊断（F6.1 期 wheel）：monkeypatch 当时的 executor，结论已转成 tests/contract 断言",
    "tmp/k0s/open-tunnels.sh": "历史/坏的那一版：已被仓库内 deploy/scripts/open-cluster-tunnel.sh 取代",
    "tmp/k0s/tools.sh": "跳板机 wrapper：依赖未在文档引用清单内、因此未搬的 tmp/k0s/lib/*.exp，单搬 wrapper 不能复现",
    "tmp/k0s/reset-smoke-template.sh": "一次性冒烟清理：读本机未跟踪的 tmp/k0s/secrets.env 取 key、curl 固定本机端口，只在那次环境成立",
    "tmp/mediation_probe.py": "一次性诊断（F6.1 期 wheel）：monkeypatch 当时实现，结论已转成 tests/contract 断言",
    "tmp/mem_overcommit_probe.py": "口径只在当时的真 sandlock wheel 上成立（unit 档不可复现）；结论已钉进 tests/contract",
    "tmp/pidns-canary-contracts.sh": "wrapper：payload 是临时上传到目标机上跑的那个 .py，不在文档引用清单内，无法单搬复现",
    "tmp/pidns-canary-health.sh": "在目标机上跑的一次性健康扫描（读 /opt/sandlock 容器状态与日志），依赖当时那份部署",
    "tmp/pidns-canary.sh": "wrapper：payload 是临时上传到目标机上跑的那个 .py，不在文档引用清单内，无法单搬复现",
    "tmp/prod-run.sh": "只读审计 wrapper：payload tmp/prod-audit{,2,3}.sh 不在文档引用清单内，单搬 wrapper 不成事",
    "tmp/security-probe2.py": "文件已不存在（历史），无法复现",
    "tmp/security-probe3.py": "文件已不存在（历史），无法复现",
}


def _doc_files() -> tuple[Path, ...]:
    """Every markdown file under `docs/`, frozen archive included."""
    return tuple(sorted(DOCS.rglob("*.md")))


def _live_docs() -> tuple[Path, ...]:
    return tuple(p for p in _doc_files() if FROZEN_ARCHIVE not in p.parents)


def _references(files: tuple[Path, ...], pattern: re.Pattern[str] = TMP_SCRIPT) -> set[str]:
    found: set[str] = set()
    for path in files:
        found.update(pattern.findall(path.read_text(encoding="utf-8")))
    return found


def test_the_frozen_archive_is_not_live_docs() -> None:
    """The exclusion is a decision, not a glob that happened to miss files."""
    frozen = tuple(p for p in _doc_files() if FROZEN_ARCHIVE in p.parents)
    assert frozen, "docs/reports/ is where the frozen work notes were copied"
    assert len(frozen) == len(_doc_files()) - len(_live_docs())
    assert _references(frozen), "the frozen archive is expected to cite tmp/ paths"


def test_no_live_doc_points_at_a_promoted_tmp_path() -> None:
    """The deny list: a promoted path is a *moved* path, not a blessed one."""
    offenders = sorted(_references(_live_docs()) & set(PROMOTED))
    assert offenders == [], (
        "a live doc still sends the reader to tmp/ after the file was promoted "
        "into the repo (point it at the repo path instead): "
        + "; ".join(f"{old} -> {PROMOTED[old]}" for old in offenders)
    )


def test_every_live_doc_tmp_reference_is_allowed_with_a_reason() -> None:
    unpinned = sorted(_references(_live_docs()) - set(PROMOTED) - set(ALLOWED_TMP_REFERENCES))
    assert unpinned == [], (
        "a live doc sends the reader to a tmp/ script that has no repo copy and "
        f"no recorded reason: {unpinned}"
    )


def test_every_allowance_is_still_cited_and_carries_a_one_line_reason() -> None:
    cited = _references(_live_docs())
    stale = sorted(set(ALLOWED_TMP_REFERENCES) - cited)
    assert stale == [], (
        "these allowances no longer excuse anything, so they are only a way for "
        f"the next tmp/ reference to slip back in: {stale}"
    )
    bad = sorted(
        path
        for path, reason in ALLOWED_TMP_REFERENCES.items()
        if not reason.strip() or "\n" in reason
    )
    assert bad == [], f"allowances need a one-line reason each: {bad}"


def test_a_reference_is_never_both_promoted_and_allowed() -> None:
    overlap = sorted(set(PROMOTED) & set(ALLOWED_TMP_REFERENCES))
    assert overlap == [], f"each path has exactly one disposition: {overlap}"


def test_promoted_targets_are_in_the_repo_and_gone_from_tmp() -> None:
    """Promotion means moved: the repo copy exists and the tmp original is gone."""
    missing, leftovers = [], []
    for source, target in PROMOTED_ENTRIES:
        if not (REPO / target).is_file():
            missing.append(target)
        if (REPO / source).exists():
            leftovers.append(source)
    assert missing == [], f"promoted targets are not in the repo: {missing}"
    assert leftovers == [], f"promoted sources are still in tmp/: {leftovers}"


def test_every_cited_acceptance_script_exists() -> None:
    """The replacement reference is checked too: no dangling repo-side pointer."""
    missing = sorted(
        ref for ref in _references(_live_docs(), ACCEPTANCE_SCRIPT) if not (REPO / ref).is_file()
    )
    assert missing == [], (
        "a live doc points at an acceptance script that is not in the repo: "
        f"{missing}"
    )
