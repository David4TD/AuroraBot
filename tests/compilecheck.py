"""Compile every source file, one at a time, and say so honestly.

compileall(quiet=2) once printed OK for a file with an unterminated string
literal, so this uses py_compile with doraise per file instead.
"""
from __future__ import annotations

import py_compile
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "src"

broken = 0
for path in sorted(ROOT.rglob("*.py")):
    if "__pycache__" in path.parts:
        continue
    try:
        py_compile.compile(str(path), doraise=True, quiet=1)
    except py_compile.PyCompileError as exc:
        broken += 1
        print(f"BROKEN {path.relative_to(ROOT.parent)}: {exc.exc_type_name}: "
              f"{exc.exc_value}")

print(f"{broken} FILE(S) BROKEN" if broken else "COMPILE OK")
sys.exit(1 if broken else 0)
