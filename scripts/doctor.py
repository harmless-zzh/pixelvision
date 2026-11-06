#!/usr/bin/env python3
"""Check whether this machine can actually render pages and read them back.

Run this first when a capture fails, or before installing. Each line is one of:
ok (works), warn (may still work, usually offline or optional), FAIL (capture
cannot work until this is fixed). The process exits non-zero only on FAIL.

    python scripts/doctor.py
    python scripts/doctor.py --json
"""

from __future__ import annotations

import json
import platform
import shutil
import sys
from functools import lru_cache
from pathlib import Path

from _console import force_utf8_stdio

ROOT = Path(__file__).resolve().parent.parent


def _result(name: str, level: str, detail: str, hint: str = "") -> dict:
    return {"name": name, "level": level, "ok": level != "fail", "detail": detail, "hint": hint}


def check_python() -> dict:
    ok = sys.version_info >= (3, 10)
    return _result(
        "python",
        "ok" if ok else "fail",
        platform.python_version() + ("" if ok else " (need >= 3.10)"),
        "" if ok else "install Python 3.10 or newer",
    )


def check_pillow() -> dict:
    try:
        from importlib.metadata import version

        return _result("pillow", "ok", version("pillow"))
    except Exception:  # noqa: BLE001
        return _result(
            "pillow",
            "fail",
            "not installed",
            f"{sys.executable} -m pip install pillow",
        )


@lru_cache(maxsize=1)
def _launch_probe() -> tuple[bool, str]:
    """Really launch a browser; a discovered path can still fail to start.

    The driver may be installed while the browser binary is unusable (partial
    `playwright install`, wrong architecture, blocked by policy). Checking only
    for a file path reports a false ok, so doctor opens about:blank once.
    """
    try:
        from playwright.sync_api import sync_playwright

        sys.path.insert(0, str(ROOT / "scripts"))
        from capture import _launch, find_browser  # type: ignore

        with sync_playwright() as p:
            browser = _launch(p, find_browser())
            try:
                page = browser.new_page()
                page.goto("about:blank", timeout=10000)
                page.close()
            finally:
                browser.close()
        return True, "launched a browser and opened about:blank"
    except Exception as exc:  # noqa: BLE001
        return False, " ".join(str(exc).split())[:300]


def check_playwright() -> dict:
    try:
        from importlib.metadata import version

        dist_version = version("playwright")
    except Exception:  # noqa: BLE001
        return _result(
            "playwright",
            "fail",
            "not installed",
            f"{sys.executable} -m pip install playwright",
        )

    launched, detail = _launch_probe()
    if launched:
        return _result("playwright", "ok", f"{dist_version}; {detail}")
    return _result(
        "playwright",
        "warn",
        f"{dist_version}, could not launch a browser: {detail}",
        "install Chrome/Edge, run `playwright install chromium`, or set CHROME_PATH",
    )


def check_browser() -> dict:
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        from capture import find_browser  # type: ignore
    except Exception as exc:  # noqa: BLE001
        return _result("browser", "fail", f"cannot import capture.py: {exc}",
                       "run this from the repo checkout")

    channel, path = find_browser()
    launched, detail = _launch_probe()
    if launched:
        where = f"{channel}: {path}" if path else "playwright bundled chromium"
        return _result("browser", "ok", f"{where}; {detail}")
    return _result(
        "browser",
        "fail",
        f"no browser could be launched ({channel or 'no channel'}: {path or 'no path'}): {detail}",
        "install Chrome or Edge, run `playwright install chromium`, or set CHROME_PATH",
    )


def check_pixelshot() -> dict:
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        from capture import find_pixelshot  # type: ignore
    except Exception:  # noqa: BLE001
        find_pixelshot = None  # type: ignore
    exe = shutil.which("pixelshot") or shutil.which("pixelshot.exe")
    if not exe and find_pixelshot:
        exe = find_pixelshot()
    if exe:
        return _result("pixelshot (optional)", "ok", exe)
    return _result(
        "pixelshot (optional)",
        "ok",
        "not installed",
        "optional: `uv tool install pixelrag` for the PixelRAG backend",
    )


def check_network() -> dict:
    import socket

    targets = [("developer.mozilla.org", 443), ("example.com", 443)]
    for host, port in targets:
        try:
            with socket.create_connection((host, port), timeout=5):
                return _result("network", "ok", f"{host}:{port} reachable")
        except Exception:  # noqa: BLE001
            continue
    return _result(
        "network",
        "warn",
        "no test host reachable",
        "offline is fine for file:// captures; remote URLs will fail",
    )


def check_skill_install() -> dict:
    home = Path.home()
    candidates = [
        home / ".agents" / "skills" / "pixelvision",
        home / ".dsh" / "skills" / "pixelvision",
        home / ".claude" / "skills" / "pixelvision",
    ]
    found = [str(p) for p in candidates if (p / "SKILL.md").is_file()]
    if found:
        return _result("skill installed", "ok", ", ".join(found))
    return _result(
        "skill installed",
        "ok",
        "not installed globally (the repo still works in place)",
        "run scripts/install.ps1 or scripts/install.sh to expose it to your agents",
    )


CHECKS = [check_python, check_pillow, check_playwright, check_browser, check_pixelshot,
          check_network, check_skill_install]


def main() -> int:
    force_utf8_stdio()
    results = [check() for check in CHECKS]
    hard_failures = [r for r in results if r["level"] == "fail"]
    warnings = [r for r in results if r["level"] == "warn"]

    if "--json" in sys.argv:
        print(json.dumps({"ok": not hard_failures, "checks": results}, ensure_ascii=True, indent=2))
    else:
        print(f"pixelvision doctor  ({platform.system()} {platform.release()})\n")
        marks = {"ok": "ok  ", "warn": "warn", "fail": "FAIL"}
        for r in results:
            print(f"  [{marks[r['level']]}] {r['name']:<22} {r['detail']}")
            if r["hint"]:
                print(f"           -> {r['hint']}")
        print()
        if hard_failures:
            print(f"{len(hard_failures)} check(s) failed. Fix the FAIL lines above, then retry.")
        elif warnings:
            print(f"{len(warnings)} warning(s). Captures may still work; see the warn lines above.")
        else:
            print("Ready. Try: python scripts/capture.py https://example.com -o ./shots")
    return 1 if hard_failures else 0


if __name__ == "__main__":
    sys.exit(main())
