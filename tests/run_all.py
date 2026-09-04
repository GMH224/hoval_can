"""Run every Hoval CAN test suite in a fresh interpreter each.

Each suite installs its own Home Assistant module stubs into ``sys.modules``,
so they must not share a process. This runner spawns one subprocess per suite
and aggregates the results.

    python3 tests/run_all.py        # exit code 0 == all suites pass
"""
from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

SUITES = [
    ("test_protocol", "framing, decoder, COP model, watchdog, diagnostics"),
    ("test_health", "health index, Gütegrad, cold-start gating"),
    ("test_thread_safety", "dispatcher job types (HA 2026.9 loop safety)"),
    ("test_config_flow", "config/options flow + 2026.12 lifecycle contracts"),
    ("test_lifecycle", "entry setup/unload/reload, resource cleanup"),
]


def main() -> int:
    results = []
    total_checks = 0
    for name, desc in SUITES:
        proc = subprocess.run(
            [sys.executable, os.path.join(HERE, f"{name}.py")],
            capture_output=True, text=True,
        )
        checks = proc.stdout.count("[OK]")
        total_checks += checks
        ok = proc.returncode == 0
        results.append((name, desc, ok, checks))
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {name:<20} {checks:>4} checks   {desc}")
        if not ok:
            print(proc.stdout[-3000:])
            if proc.stderr:
                print(proc.stderr[-2000:])

    failed = [r for r in results if not r[2]]
    print("-" * 72)
    print(f"{len(results) - len(failed)}/{len(results)} suites passed, "
          f"{total_checks} checks total")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
