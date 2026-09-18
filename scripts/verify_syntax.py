#!/usr/bin/env python3
"""Fail fast if any .py file has syntax errors. Run before building the Windows exe."""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP = {".git", "__pycache__", "build", "dist", ".venv", "venv"}


def main() -> int:
    errors: list[str] = []
    for path in sorted(ROOT.rglob("*.py")):
        if any(part in SKIP for part in path.parts):
            continue
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            errors.append(f"{path.relative_to(ROOT)}:{exc.lineno}: {exc.msg}")
    if errors:
        print("Syntax errors found:", file=sys.stderr)
        for line in errors:
            print(f"  {line}", file=sys.stderr)
        return 1
    print(f"OK - all Python files under {ROOT.name}/ parse cleanly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
