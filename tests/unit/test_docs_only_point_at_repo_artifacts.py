"""Docs must not send a reader to a `tmp/` script that only exists on one machine.

Most of this repo's acceptance evidence used to live in `tmp/` (gitignored) and
`.superpowers/sdd/` (gitignored) while `docs/**` told the reader to go run it *by
name*. `tmp/` gets cleaned, and a fresh checkout has neither file, so the sentence
"how to run this again" was the first thing to rot -- one round of it produced an
acceptance table that only existed inside a gitignored report. The scripts have
now been promoted to `deploy/scripts/acceptance/` (reports: `docs/reports/`), and
this file pins that mapping so the next tmp-only criterion cannot be introduced
by quietly adding a line to a doc.

What is asserted: every `tmp/**.py|sh` path a *live* doc cites as the way to
re-run a criterion is named either in `PROMOTED` (it is in the repo now) or in
`ALLOWED_TMP_REFERENCES` (it is not, and the reason says why). The comparison is
set equality against an explicit manifest -- not a substring or prefix match --
so a new reference, or the removal of an allowance, changes the answer.

`docs/reports/**` is deliberately *not* part of the pin: those files are
byte-exact copies of historical work notes, so their `tmp/` mentions are the past
narrated, not instructions. They are still scanned (see
`test_the_frozen_archive_is_not_live_docs`) so that this is a decision on the
record rather than a glob accident.

Falsifiability -- both run against the tree of the commit that added this file,
red output pasted into `docs/reports/README.md`:

* appending `see tmp/does-not-exist-probe.py` to a live doc: the unpinned path is
  named and the set comparison fails;
* deleting a still-cited entry from `ALLOWED_TMP_REFERENCES` (the run used
  `tmp/k0s/gateA-full.sh`): the same assertion names that path.
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

#: source (gitignored) -> where the byte-exact copy lives now. 67 entries.
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
    ("tmp/k0s/cpu_activity_acceptance.py", "deploy/scripts/acceptance/cpu_activity_acceptance.py"),
    ("tmp/k0s/mmap-probe.py", "deploy/scripts/acceptance/mmap-probe.py"),
    ("tmp/k0s/n35-lane.sh", "deploy/scripts/acceptance/n35-lane.sh"),
    ("tmp/k0s/n42-egress-probe.py", "deploy/scripts/acceptance/n42-egress-probe.py"),
    ("tmp/k0s/node-mmap-storage.sh", "deploy/scripts/acceptance/node-mmap-storage.sh"),
    ("tmp/k0s/overlay-probe.sh", "deploy/scripts/acceptance/overlay-probe.sh"),
    ("tmp/k0s/phase1-probe2.sh", "deploy/scripts/acceptance/phase1-probe2.sh"),
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

#: References a live doc may keep pointing at `tmp/`, one reason per line. Every
#: entry is either already tracked by git at that very path (so a cleaned `tmp/`
#: or another machine costs a `git checkout`, not an artifact), or one-off /
#: historical work that cannot be reproduced from today's tree -- the promotion
#: criterion is "which file does a reader need to re-run this conclusion", and
#: for these the honest answer is "not this one".
ALLOWED_TMP_REFERENCES = {
    "tmp/a7-fix1-run.sh": "一次性复跑 runner（A7 fix round 1），依赖同轮临时文件 tmp/a7-run.sh，属一次性验证",
    "tmp/instance_probe.py": "一次性诊断（F6.1 期 wheel）：monkeypatch 当时的 executor，结论已转成 tests/contract 断言",
    "tmp/k0s/checkpoint_acceptance.py": "已有仓库副本 deploy/scripts/checkpoint_acceptance.py（docs 应改指它）；tmp 那份是旧副本，不搬也不覆盖",
    "tmp/k0s/gateA-full.sh": "已在版本库：git 已跟踪这个 tmp 路径本身（清 tmp / 换机后 git checkout 可还原），不搬",
    "tmp/k0s/gateB-full.sh": "已在版本库：git 已跟踪这个 tmp 路径本身（清 tmp / 换机后 git checkout 可还原），不搬",
    "tmp/k0s/gateB-pure-rootfs.sh": "已在版本库：git 已跟踪这个 tmp 路径本身（清 tmp / 换机后 git checkout 可还原），不搬",
    "tmp/k0s/n27-t7-lane.sh": "已在版本库：git 已跟踪这个 tmp 路径本身（清 tmp / 换机后 git checkout 可还原），不搬",
    "tmp/k0s/open-tunnels.sh": "历史/坏的那一版：已被仓库内 deploy/scripts/open-cluster-tunnel.sh 取代",
    "tmp/k0s/phase2.sh": "已在版本库：git 已跟踪这个 tmp 路径本身（清 tmp / 换机后 git checkout 可还原），不搬",
    "tmp/k0s/probe-pure-restore-synthroot.sh": "已在版本库：git 已跟踪这个 tmp 路径本身（清 tmp / 换机后 git checkout 可还原），不搬",
    "tmp/k0s/probe-pure-synth-root-plaindir.py": "已在版本库：git 已跟踪这个 tmp 路径本身（清 tmp / 换机后 git checkout 可还原），不搬",
    "tmp/k0s/probe-pure-synth-root.sh": "已在版本库：git 已跟踪这个 tmp 路径本身（清 tmp / 换机后 git checkout 可还原），不搬",
    "tmp/k0s/probe-pure-workload-census.py": "已在版本库：git 已跟踪这个 tmp 路径本身（清 tmp / 换机后 git checkout 可还原），不搬",
    "tmp/k0s/probe_state_base_visibility.py": "已在版本库：git 已跟踪这个 tmp 路径本身（清 tmp / 换机后 git checkout 可还原），不搬",
    "tmp/k0s/reset-smoke-template.sh": "一次性冒烟清理：读本机未跟踪的 tmp/k0s/secrets.env 取 key、curl 固定本机端口，只在那次环境成立",
    "tmp/k0s/tools.sh": "跳板机 wrapper：依赖未在文档引用清单内、因此未搬的 tmp/k0s/lib/*.exp，单搬 wrapper 不能复现",
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


def _references(files: tuple[Path, ...]) -> set[str]:
    found: set[str] = set()
    for path in files:
        found.update(TMP_SCRIPT.findall(path.read_text(encoding="utf-8")))
    return found


def test_the_frozen_archive_is_not_live_docs() -> None:
    """The exclusion is a decision, not a glob that happened to miss files."""
    frozen = tuple(p for p in _doc_files() if FROZEN_ARCHIVE in p.parents)
    assert frozen, "docs/reports/ is where the frozen work notes were copied"
    assert len(frozen) == len(_doc_files()) - len(_live_docs())
    assert _references(frozen), "the frozen archive is expected to cite tmp/ paths"


def test_every_live_doc_reference_is_pinned_by_name() -> None:
    unpinned = _references(_live_docs()) - set(PROMOTED) - set(ALLOWED_TMP_REFERENCES)
    assert unpinned == set(), (
        "a live doc sends the reader to a tmp/ script that is neither promoted "
        f"into the repo nor allowed with a reason: {sorted(unpinned)}"
    )


def test_no_unpinned_reference_still_exists_in_tmp() -> None:
    """The literal rule: outside the allowance, a cited tmp path is not there."""
    present = sorted(
        ref
        for ref in _references(_live_docs())
        if ref not in ALLOWED_TMP_REFERENCES and (REPO / ref).exists()
    )
    assert present == [], (
        "these tmp scripts are cited by a live doc and still on disk, so the doc "
        f"is pointing at a scratch file: {present}"
    )


def test_promoted_scripts_are_in_the_repo_and_gone_from_tmp() -> None:
    missing, leftovers = [], []
    for source, target in PROMOTED_ENTRIES:
        if not (REPO / target).is_file():
            missing.append(target)
        if (REPO / source).exists():
            leftovers.append(source)
    assert missing == [], f"promoted targets are not in the repo: {missing}"
    assert leftovers == [], f"promoted sources are still in tmp/: {leftovers}"


def test_a_reference_is_never_both_promoted_and_allowed() -> None:
    overlap = sorted(set(PROMOTED) & set(ALLOWED_TMP_REFERENCES))
    assert overlap == [], f"each path has exactly one disposition: {overlap}"


def test_every_allowed_reference_carries_a_one_line_reason() -> None:
    bad = sorted(
        path
        for path, reason in ALLOWED_TMP_REFERENCES.items()
        if not reason.strip() or "\n" in reason
    )
    assert bad == [], f"allowances need a one-line reason each: {bad}"
