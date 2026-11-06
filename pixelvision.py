#!/usr/bin/env python3
r"""Cross-shell launcher for pixelvision.

Unlike the .cmd wrapper, this process receives its argv from Python (not cmd.exe),
so arguments containing `%` are passed through literally. Run it with the
interpreter that has playwright/pillow installed, e.g. the repo venv:

    .\.venv\Scripts\python.exe pixelvision.py https://example.com/a%20b
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path


def main() -> int:
    target = Path(__file__).resolve().parent / "scripts" / "capture.py"
    sys.path.insert(0, str(target.parent))
    sys.argv = [str(target), *sys.argv[1:]]
    runpy.run_path(str(target), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
