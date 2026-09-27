"""E5–E7 收口：每条判据的变异反证（跑一遍，变异体必须变红）。

用法（任选一个 python，脚本自己用 `sys.executable` 起 pytest）：

```bash
tmp/testenv/bin/python deploy/scripts/acceptance/e567_mutations.py
```

每条记录是 `(label, 文件, 原文, 变体, pytest 目标)`：把原文换成变体（一次只换一处），
跑那一条用例，再把文件**无条件**恢复。变异体必须让用例**变红**——变绿就是"这条用例
没在守它声称守的东西"，脚本以非零退出并把幸存者列出来。

被判据覆盖的两条尾巴：

* **E6**（`control_plane/registry/paused_ttl.py` + `control_plane/app.py` 的接线）：
  开关默认关、只删"超期且 paused"、跨副本单飞、走 delete 同一条 teardown、删记录。
* **E7**（`control_plane/registry/ledger_alert.py` + 接线）：越限只打一条、`budget=0` 不算满、
  单飞、接线真的启动扫描。

脚本只改 `control_plane/**` 的三个文件，且改前先备份到 `tmp/e567/`、`finally` 里恢复；
它不动 git 状态，也不碰 `envd_service/**`（那一半是并行的另一单）。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

# repo root: this file lives at deploy/scripts/acceptance/e567_mutations.py
REPO = Path(__file__).resolve().parents[3]
SCRATCH = REPO / "tmp" / "e567"
SCRATCH.mkdir(parents=True, exist_ok=True)

PAUSED = "control_plane/registry/paused_ttl.py"
ALERT = "control_plane/registry/ledger_alert.py"
APP = "control_plane/app.py"

E6_UNIT = "tests/unit/test_paused_ttl_sweep.py"
E7_UNIT = "tests/unit/test_platform_ledger_alert.py"

MUTANTS = [
    (
        "E6/no-off-guard",
        PAUSED,
        "    if ttl_s <= 0:\n        return []\n",
        "    if False:\n        return []\n",
        f"{E6_UNIT}::test_ttl_zero_selects_nothing_however_old_the_sandbox_is",
    ),
    (
        "E6/no-paused-filter",
        PAUSED,
        '    if getattr(record, "state", None) != "paused":\n        return None\n',
        "    if False:\n        return None\n",
        f"{E6_UNIT}::test_the_selection_skips_a_running_record_by_its_state",
    ),
    (
        "E6/no-claim-check",
        PAUSED,
        "                if self._claim is not None and not self._claim():\n"
        "                    await asyncio.sleep(self._interval)\n"
        "                    continue\n",
        "",
        f"{E6_UNIT}::test_a_lost_claim_skips_the_round",
    ),
    (
        "E6/no-teardown",
        PAUSED,
        "    removed = list(await _maybe_await(teardown(record)) or [])\n",
        "    removed = []\n",
        f"{E6_UNIT}::test_the_app_wires_the_sweep_into_the_same_teardown_delete_uses",
    ),
    (
        "E6/no-record-delete",
        PAUSED,
        "        registry.delete(record.sandbox_id)\n",
        "        pass\n",
        f"{E6_UNIT}::test_the_reap_gives_a_still_held_reservation_back",
    ),
    (
        "E6/switch-never-wired",
        APP,
        "        app.state.paused_sweeper = paused_sweeper\n"
        "        paused_sweeper.start(registry)\n",
        "        app.state.paused_sweeper = paused_sweeper\n",
        f"{E6_UNIT}::test_the_app_wires_the_sweep_into_the_same_teardown_delete_uses",
    ),
    (
        "E7/never-remember",
        ALERT,
        "        self._over = set(over)\n",
        "        self._over = set()\n",
        f"{E7_UNIT}::test_a_node_over_the_ratio_does_not_repeat_every_round",
    ),
    (
        "E7/no-claim-check",
        ALERT,
        "                if self._claim is not None and not self._claim():\n"
        "                    await asyncio.sleep(self._interval)\n"
        "                    continue\n",
        "",
        f"{E7_UNIT}::test_the_alert_round_is_single_flight",
    ),
    (
        "E7/budget-zero-means-full",
        ALERT,
        "    if budget <= 0:\n        return None\n",
        "    if budget < 0:\n        return None\n",
        f"{E7_UNIT}::test_a_budget_of_zero_is_unlimited_and_never_warns",
    ),
    (
        "E7/warn-every-round",
        ALERT,
        "        for node_id in sorted(over.keys() - self._over):\n",
        "        for node_id in sorted(over):\n",
        f"{E7_UNIT}::test_a_node_over_the_ratio_does_not_repeat_every_round",
    ),
    (
        "E7/alert-never-wired",
        APP,
        "        app.state.ledger_alerter = ledger_alerter\n",
        "        app.state.ledger_alerter = ledger_alerter\n"
        "        if True:\n            return\n",
        f"{E7_UNIT}::test_the_app_starts_the_alert_scan",
    ),
]


def main() -> int:
    survivors: list[str] = []
    for label, rel, old, new, target in MUTANTS:
        path = REPO / rel
        original = path.read_text()
        if old not in original:
            print(f"{label}: ANCHOR MISSING -- fix the mutant, it proved nothing")
            survivors.append(label)
            continue
        backup = SCRATCH / (path.name + ".orig")
        shutil.copyfile(path, backup)
        path.write_text(original.replace(old, new, 1))
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", target, "-q"],
                cwd=REPO,
                capture_output=True,
                text=True,
            )
            tail = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
            verdict = (
                "KILLED (red as required)" if proc.returncode != 0 else "SURVIVED"
            )
            print(f"{label}: {verdict} -- {tail}")
            if proc.returncode == 0:
                survivors.append(label)
        finally:
            shutil.copyfile(backup, path)
            backup.unlink()
    print("---")
    if survivors:
        print("SURVIVORS:", ", ".join(survivors))
        return 1
    print(f"all {len(MUTANTS)} mutants killed; every source file restored")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
