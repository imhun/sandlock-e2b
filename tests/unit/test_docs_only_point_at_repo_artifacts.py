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
import subprocess
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
#: 84 entries: the 67 from the first promotion round, the 10 fixed tools of the
#: second round, `checkpoint_acceptance.py` (whose old 437-line copy was a
#: duplicate of the 632-line `deploy/scripts/checkpoint_acceptance.py`), and the
#: six probes a third pass caught -- they were cited by *bare filename*, which
#: the path-shaped scan above cannot see (see `BARE_NAME_ALLOWANCES`).
PROMOTED_ENTRIES = (
    ("tmp/arm-vm/run.sh", "deploy/scripts/acceptance/run.sh"),
    ("tmp/capacity_check.py", "deploy/scripts/acceptance/capacity_check.py"),
    ("tmp/cleanup_scratch.py", "deploy/scripts/acceptance/cleanup_scratch.py"),
    ("tmp/e5e8/guard_probe.py", "deploy/scripts/acceptance/guard_probe.py"),
    ("tmp/e5e8/guard_probe2.py", "deploy/scripts/acceptance/guard_probe2.py"),
    ("tmp/f11_direct_exec_probe.py", "deploy/scripts/acceptance/f11_direct_exec_probe.py"),
    ("tmp/plan-2026-09-26/f11_snapshot_restart_acceptance.py", "deploy/scripts/acceptance/f11_snapshot_restart_acceptance.py"),
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
    ("tmp/k0s/probe_delete_then_write.py", "deploy/scripts/acceptance/probe_delete_then_write.py"),
    ("tmp/k0s/probe_freeze_latency_cp.py", "deploy/scripts/acceptance/probe_freeze_latency_cp.py"),
    ("tmp/k0s/probe_guest_raise_ctypes.py", "deploy/scripts/acceptance/probe_guest_raise_ctypes.py"),
    ("tmp/k0s/probe_second_file_race.py", "deploy/scripts/acceptance/probe_second_file_race.py"),
    ("tmp/k0s/probe_mmap_growth.py", "deploy/scripts/acceptance/probe_mmap_growth.py"),
    ("tmp/k0s/probe_n28_acceptance.py", "deploy/scripts/acceptance/probe_n28_acceptance.py"),
    ("tmp/k0s/probe_n29_sync.py", "deploy/scripts/acceptance/probe_n29_sync.py"),
    ("tmp/k0s/probe_n35_exec_gate.py", "deploy/scripts/acceptance/probe_n35_exec_gate.py"),
    ("tmp/k0s/probe_n35_mount_perms.py", "deploy/scripts/acceptance/probe_n35_mount_perms.py"),
    ("tmp/k0s/probe_n35_mount_variants.py", "deploy/scripts/acceptance/probe_n35_mount_variants.py"),
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
    (
        "tmp/n58-rehearsal/rehearse.py",
        "deploy/scripts/acceptance/migrate_state_base_rehearsal.py",
    ),
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
    # The k0s white-box audit probes (2026-09-30 / 2026-10-01 rounds). They are
    # not reproducible from a checkout: every one of them needs the live cluster
    # plus a control-plane API key, and the shape they were written against is
    # the deployment of that day. Their conclusions are in
    # `docs/security-audit/findings-k0s-*.md` (and, where they became
    # contracts, in `tests/`).
    "tmp/audit/probe_i1_replay.py": "一次性线上审计探针（需集群 + API key），结论见 docs/security-audit/findings-k0s-2026-09-30.md",
    "tmp/audit2/p21_cross_rce.py": "一次性线上审计探针（需集群 + API key），结论见 docs/security-audit/findings-k0s-2026-10-01.md",
    # The k0s white-box audit probes, third round (2026-10-04): the SEC-R3-01
    # unauthenticated-RCE proof and the STATIC-2/3/5 reproductions. Same reason as
    # the rounds above -- each needs the live cluster plus a control-plane API key,
    # and the shape they were written against is the deployment of that day. What
    # became a contract lives in `tests/`: the envd fail-closed guard in
    # `tests/security/test_envd_token_fail_closed.py`, the `e2b-maint --worker`
    # gate in `tests/unit/test_priv_maint_worker_gate.py`, and the fork-side
    # syscall classification in `third_party/sandlock`'s own `sys::path_surface`
    # tests. Conclusions: `docs/security-audit/findings-k0s-2026-10-04.md`.
    "tmp/audit3/repro_c3_maint.sh": "一次性线上审计驱动（本地编译 c3_agent 特权二进制复现 STATIC-5）；结论已转成 tests/unit/test_priv_maint_worker_gate.py",
    "tmp/audit3/run_layer_probe.sh": "差分探针 runner（同一车道跑两遍：带 worker "
        "profile 与 seccomp=unconfined，归因出只靠外层 profile 挡住的 syscall）；"
        "结论见 docs/security-audit/layer-attribution-2026-10-04.md",
    "tmp/audit3/extract_syscall_table.py": "从 syscalls crate 的 per-arch 源文件导出编号表"
        "（差分探针的编号-名字映射来源，非手抄常量）；见 layer-attribution-2026-10-04.md",
    "tmp/audit3/s1_notify_fd.py": "一次性线上审计探针（需集群 + API key），结论见 findings-k0s-2026-10-04.md §3.1",
    "tmp/audit3/s2_reach.py": "一次性线上审计探针（需集群 + API key），结论见 findings-k0s-2026-10-04.md §3.2",
    "tmp/audit3/s3_reach_allowout.py": "一次性线上审计探针（需集群 + API key），结论见 findings-k0s-2026-10-04.md §3.2",
    "tmp/audit3/s4_secure_flag.py": "一次性线上审计探针（需集群 + API key），`secure` 字段取证；结论已转成 tests/security/test_envd_token_fail_closed.py",
    "tmp/audit3/s5_rce_proof.py": "一次性线上审计探针（SEC-R3-01 决定性证据，需集群 + API key）；结论已转成 tests/security/test_envd_token_fail_closed.py",
    "tmp/audit3/s5_postfix_matrix.py": "修复后一次性线上矩阵驱动（需集群 + API key）：原 s5_rce_proof.py 修复后建箱即被 400 拒而不可复用；结论已转成 tests/security/test_envd_token_fail_closed.py 与 docs/deploy-clusters.md §7.37",
    "tmp/audit3/in_pod_rce.py": "s5 的集群内半边（Connect 信封协议），随 s5 一起记录",
    "tmp/audit3/s6_escalation.py": "一次性线上审计探针（需集群 + API key），结论见 findings-k0s-2026-10-04.md §2",
    "tmp/audit3/s7_cross_tenant_reach.py": "一次性线上审计探针（需集群 + API key），结论见 findings-k0s-2026-10-04.md §3.2",
    "tmp/audit3/s8_static_repro.py": "一次性线上审计探针（需集群 + API key），STATIC-2/3/4 复现；结论见 findings-k0s-2026-10-04.md §5",
    "tmp/audit3/s9_caps_clone.py": "一次性线上审计探针（已废弃：裸调 clone3 且 stack=0 会递归 fork，见 findings §5.3 的教训）",
    "tmp/audit3/s10_caps_only.py": "一次性线上审计探针（需集群 + API key），能力普查；结论见 findings-k0s-2026-10-04.md §4",
    "tmp/audit3/s11_clone3_safe.py": "一次性线上审计探针（需集群 + API key），clone3 层级归因；结论见 findings-k0s-2026-10-04.md §5.3",
    "tmp/probe/probe.py": "一次性线上审计探针（需集群 + API key），结论见 docs/security-audit/findings.md",
    "tmp/signal-probe/cross_sandbox_signal.py": "一次性线上审计探针（需集群 + API key），结论见 docs/security-audit/findings.md",
    "tmp/signal-probe/inside_sandbox_blast.py": "一次性线上审计探针（需集群 + API key），结论见 docs/security-audit/findings.md",
    "tmp/syscall-probe/probe.py": "一次性线上审计探针（需集群 + API key），结论见 docs/security-audit/findings.md",
    # N79 的卡顿取证组（2026-10-05）：stat 族进 seccomp 通知后，撞限流会"一秒睡满 0.86 s"。
    # 三支都需要当时那套部署 —— stat_stall_hunt 走控制面 API 建箱，另外两支要 kubectl exec
    # 进 hostPID 的 c3-agent pod 读全宿主 /proc，脱离当时的集群形状不可复现。
    # 结论与口径见 docs/benchmarks.md §③、docs/open-issues.md 的 N79 行。
    "tmp/stat_stall_hunt.py": "一次性线上卡顿取证（需集群 + API key）：前台 stat 循环标出 >20 ms 的调用；结论见 docs/benchmarks.md §③",
    "tmp/host_sampler.py": "一次性线上卡顿取证（需 kubectl exec 进 hostPID 的 c3-agent pod）：宿主侧 200 ms 采样，与 stat_stall_hunt 同源时间戳对齐",
    "tmp/stall_snapshot.py": "一次性线上卡顿取证（需 kubectl exec 进 hostPID 的 c3-agent pod）：卡顿瞬间的 /proc 现场快照（谁烧 CPU、supervisor wchan）",
    # N80 的进程合并取证（2026-10-05）：unshare(CLONE_NEWUSER) 对多线程调用方 EINVAL，
    # 而 clone3 一次带 CLONE_NEWUSER+CLONE_NEWPID 两臂都成功 —— 三条都得在 worker 容器里跑
    # （agent pod 的 unshare 直接 EPERM，到不了线程检查），脱离当时那套部署不可复现。
    # 结论与代码出处见 docs/open-issues.md 的 N80 行、docs/isolation-boundaries.md §4。
    "tmp/userns_thread_probe.py": "一次性内核实测（需 kubectl exec 进 e2b-worker-0）：unshare(CLONE_NEWUSER) 单/多线程对照，EINVAL 那一臂是 N80 的硬约束",
    "tmp/clone3_probe.py": "一次性内核实测（需 kubectl exec 进 e2b-worker-0）：clone3 一次带 CLONE_NEWUSER+CLONE_NEWPID 的单/多线程对照",
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


#: The same promise, one level up. A doc often cites a criterion by *bare
#: filename* -- "实测 `probe_delete_then_write.py`" -- and that form has no
#: directory for the path-shaped scan above to bite on. It is not hypothetical:
#: five N35/disk probes plus `probe_n35_mount_variants.py` were cited this way and
#: stayed in `tmp/` through both promotion rounds, so a reader following the doc
#: on a fresh checkout would find nothing. A bare name is now required to name a
#: file this repo tracks (by basename) or to be explained in the manifest below --
#: which is where the fork's own files go, since a submodule's contents are a
#: gitlink to the parent and never appear in `git ls-files`.
BARE_SCRIPT = re.compile(r"`([A-Za-z0-9_.-]+\.(?:py|sh))`")

BARE_NAME_ALLOWANCES = {
    "_sdk.py": "fork 子模块里的文件（third_party/sandlock/python/src/sandlock/_sdk.py）；父仓 git ls-files 看不到子模块内容",
    "build-wheels.sh": "fork 子模块里的构建脚本（third_party/sandlock/python/build-wheels.sh），同上",
    "exceptions.py": "fork 子模块里的异常类型模块（third_party/sandlock/python/src/sandlock/exceptions.py），同上",
    "sandbox.py": "fork 子模块里的 SDK 模块（third_party/sandlock/python/src/sandlock/sandbox.py），同上",
    "test-all.sh": "fork 子模块里的套件入口（third_party/sandlock/scripts/test-all.sh），同上",
    "test_supervise_channel.py": "fork 子模块里的用例（third_party/sandlock/python/tests/），同上",
    "verify-wheel.sh": "fork 子模块里的校验脚本（third_party/sandlock/python/verify-wheel.sh），同上",
    "connection_config.py": "上游 e2b SDK 的内部模块（只在 SCALING.md 里做来源说明，本仓与其子模块都没有这个文件）",
    "test_runtime_context_volumes.py": "A4 已删掉的历史测试（HANDOFF 在叙述那次改动，不是让人去跑）",
    "test_agent_grant_route.py": (
        "载体 B（2026-10-01）的 agent 授权路由用例，随该方向被否而改名成 "
        "tests/unit/test_agent_materialize.py：两个建箱计划都在叙述这次改名，"
        "不是在让人去跑它（`git rm` 那条命令执行过后就不再存在）"
    ),
    "test_file_grant_endpoint.py": (
        "载体 B（2026-10-01）的 /file-grant 端点用例，随该端点一起被删"
        "（载体 C 由控制面直接送指令，不再有票据端点）：两份建箱计划都在叙述"
        "这次删除，不是在让人去跑它"
    ),
    # route B -> own_identity 改名（2026-10-07）：三份**冻结目录**（docs/
    # superpowers/plans、docs/reports 之外的历史）与发版记录
    # docs/deploy-clusters.md 里引用的旧文件名。这些目录记的是当时的事实
    # （见 docs/superpowers/plans/2026-10-06-route-b-rename-to-own-identity.md
    # 的「历史文档不改」），所以按名引用的是**改名之前**的那个文件，而不是
    # 让人今天去跑一个不存在的脚本。
    "route_b.py": "改名前的模块（现 own_identity.py）；docs/superpowers、docs/security-audit 里的历史计划引它",
    "slot_identity.py": "改名前的模块（现 identity_grant.py）；docs/deploy-clusters.md 的发版记录引它",
    "test_nonroot_route_b.py": "改名前的用例（现 test_nonroot_own_identity.py）；历史审计/计划在引它",
    "test_route_b_slot_pool.py": "改名前的用例（现 test_own_identity_slot_pool.py）；改名计划在叙述这次改名",
    "test_sandlock_executor_route_b.py": "改名前的用例（现 test_sandlock_executor_own_identity.py）；历史审计/计划在引它",
}

#: The k0s white-box audit docs list their one-off probes as an *index* (a table
#: of "probe name -> what it proved"), and an index entry is not an instruction
#: to run it: every one of those probes needs the live cluster plus a
#: control-plane API key, and the shape is the deployment of that day. They stay
#: out of the repo on purpose (`docs/security-audit/findings-k0s-*.md` and
#: `findings.md` hold the conclusions); the entries below are the audit-round
#: filenames that index cites.
for _audit_probe in (
    "p01_recon.py",
    "p02_hostinfo.py",
    "p03_sidechannel.py",
    "p11_public_and_cidr.py",
    "p15_cp_paths.py",
    "p16_syscalls.py",
    "p17_clone3.py",
    "p18_final.py",
    "p19_pubtraffic_auth.py",
    "p20_unauth_rce.py",
    "p21_cross_rce.py",
    "probe_a2_l1.py",
    "probe_a9_scan.py",
    "probe_b1_verify8.py",
    "probe_c1_l2.py",
    "probe_d1_l3.py",
    "probe_d3_fileapi.py",
    "probe_d_net.py",
    "probe_f1_cp.py",
    "probe_g1_ratelimit.py",
    "probe_h1_l4.py",
    "probe_h2_output.py",
    "probe_i1_replay.py",
    "probe_j1_blocklist_arm64.py",
    "probe_k1_memory_scope.py",
    "verify_numbers.py",
    "verify_openapi_fix.py",
):
    BARE_NAME_ALLOWANCES[_audit_probe] = (
        "线上 k0s 审计探针（2026-09/10 三轮），需集群 + API key；"
        "审计报告只把它当索引引用，结论在 docs/security-audit/"
    )


def _bare_names() -> set[str]:
    found: set[str] = set()
    for path in _live_docs():
        found.update(BARE_SCRIPT.findall(path.read_text(encoding="utf-8")))
    return found


def _repo_basenames() -> set[str]:
    listed = subprocess.run(
        ["git", "-C", str(REPO), "ls-files"], capture_output=True, text=True, check=True
    )
    return {Path(line).name for line in listed.stdout.splitlines()}


def test_no_live_doc_names_a_script_that_this_repo_does_not_have() -> None:
    """A bare filename in a live doc must resolve, or be a recorded exception."""
    unresolved = sorted(_bare_names() - _repo_basenames() - set(BARE_NAME_ALLOWANCES))
    assert unresolved == [], (
        "a live doc tells the reader to use a script by name, and no file of "
        f"that name is in this repo: {unresolved}"
    )


def test_every_bare_name_allowance_is_still_cited_and_explained() -> None:
    cited = _bare_names()
    stale = sorted(set(BARE_NAME_ALLOWANCES) - cited)
    assert stale == [], f"these bare-name allowances no longer excuse anything: {stale}"
    bad = sorted(
        name
        for name, reason in BARE_NAME_ALLOWANCES.items()
        if not reason.strip() or "\n" in reason
    )
    assert bad == [], f"allowances need a one-line reason each: {bad}"
