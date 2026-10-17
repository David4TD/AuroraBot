"""Run every suite in this directory and report the totals.

    python tests/run.py            # everything
    python tests/run.py reminder   # only suites whose name contains "reminder"

Each suite is a standalone script that prints one line per check and exits
non-zero if any failed, so they need no test framework and can be run one at a
time while working on the thing they cover. This runner only collects them.

Why not pytest: these assert on rendered Discord embeds and on a real SQLite
file, so almost every check needs its own fixture built in order. Written as
plain scripts they stay readable as a description of the behaviour, which is
what makes a failure tell you what broke rather than which assertion tripped.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Rendered cards are full of emoji and box-drawing characters, and the Windows
# console defaults to cp1252 — without this a passing suite dies on its own
# output rather than on a failed check.
ENV_ENCODING = {"PYTHONIOENCODING": "utf-8"}


def suites(pattern: str | None) -> list[Path]:
    found = sorted(p for p in HERE.glob("test_*.py"))
    if pattern:
        found = [p for p in found if pattern.lower() in p.name.lower()]
    return found


def main() -> int:
    import os

    pattern = sys.argv[1] if len(sys.argv) > 1 else None
    env = {**os.environ, **ENV_ENCODING}

    # The compile check first: a syntax error makes every suite's failure a
    # misleading import error.
    compile_check = HERE / "compilecheck.py"
    if compile_check.exists() and not pattern:
        done = subprocess.run([sys.executable, "-u", str(compile_check)],
                              env=env, capture_output=True, text=True)
        print(done.stdout.strip() or done.stderr.strip())
        if done.returncode != 0:
            return 1

    found = suites(pattern)
    if not found:
        print(f"No suites match {pattern!r}")
        return 1

    passed = failed = 0
    broken: list[str] = []
    for path in found:
        done = subprocess.run([sys.executable, "-u", str(path)],
                              env=env, capture_output=True, text=True)
        out = done.stdout
        n_pass = out.count("  PASS  ")
        n_fail = out.count("  FAIL  ")
        passed += n_pass
        failed += n_fail
        # A suite can also die before it finishes, which is neither a pass nor
        # a fail and must not be reported as a clean run.
        crashed = done.returncode != 0 and n_fail == 0
        status = "crashed" if crashed else ("FAILED" if n_fail else "ok")
        print(f"{path.name:28} {n_pass:4} passed  {n_fail:3} failed  {status}")
        if n_fail or crashed:
            broken.append(path.name)
            for line in out.splitlines():
                if "  FAIL  " in line:
                    print(f"    {line.strip()}")
            if crashed:
                tail = (done.stderr or out).strip().splitlines()[-6:]
                for line in tail:
                    print(f"    {line}")

    print("-" * 70)
    print(f"{passed} checks passed, {failed} failed, across {len(found)} suite(s)")
    if broken:
        print("broken: " + ", ".join(broken))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
