"""Console helpers shared by the CLIs in this directory.

Windows consoles default to a legacy code page (GBK on the authoring machine), so a
Chinese path printed from Python either raises UnicodeEncodeError or arrives as mojibake.
Escaping the text to ASCII "works" but destroys the one thing the caller needs: a path
it can paste into the next command. Re-encoding the stream is the cheap correct fix.
"""

from __future__ import annotations

import sys


def force_utf8_stdio() -> None:
    """Make stdout/stderr emit UTF-8, so non-ASCII paths and titles round-trip."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, OSError, ValueError):
            # Piped through an object without reconfigure (StringIO in tests, IDEs).
            pass
