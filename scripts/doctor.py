#!/usr/bin/env python3
"""Check whether this machine can actually render pages and read them back.

Run this first when a capture fails, or before installing. It answers one
question per line and exits non-zero only when a hard requirement is missing.

    python scripts/doctor.py
    python scripts/doctor.py --json
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def check_python() -> dict:
    ok = sys.version_info >= (3, 10)
    return {
        "name": "python",
        "ok": ok,
        "detail": platform.python_version() + ("" if ok else " (need >= 3.10)"),
        "hint": "" if ok else "install Python 3.10 or newer",
    }


def check_pillow() -> dict:
    try:
        from importlib.metadata import version

        return {"name": "pillow", "ok": True, "detail": version("pillow"), "hint": ""}
    except Exception:  # noqa: BLE001
        return {
            "name": "pillow",
            "ok": False,
            "detail": "not installed",
            "hint": f"{sys.executable} -m pip install pillow",
        }


def check_playwright() -> dict:
    try:
        from importlib.metadata import version

        dist_version = version("playwright")
    except Exception:  # noqa: BLE001
        return {
            "name": "playwright",
            "ok": False,
            "detail": "not installed",
            "hint": f"{sys.executable} -m pip install playwright",
        }
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            exe = p.chromium.executable_path
            bundled = bool(exe and Path(exe).is_file())
        detail = f"{dist_version}, bundled chromium {'present' if bundled else 'absent'}"
        return {"name": "playwright", "ok": True, "detail": detail, "hint": ""}
    except Exception as exc:  # noqa: BLE001
        return {"name": "playwright", "ok": True, "detail": f"{dist_version} ({exc})", "hint": ""}


def check_browser() -> dict:
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        from capture import find_browser  # type: ignore
    except Exception as exc:  # noqa: BLE001
        return {"name": "browser", "ok": False, "detail": f"cannot import capture.py: {exc}",
                "hint": "run this from the repo checkout"}

    channel, path = find_browser()
    if path:
        return {"name": "browser", "ok": True, "detail": f"{channel}: {path}", "hint": ""}
    return {
        "name": "browser",
        "ok": False,
        "detail": "no Chrome/Edge/Chromium executable found",
        "hint": "install Chrome or Edge, run `playwright install chromium`, or set CHROME_PATH",
    }


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
        return {"name": "pixelshot (optional)", "ok": True, "detail": exe, "hint": ""}
    return {
        "name": "pixelshot (optional)",
        "ok": True,
        "detail": "not installed",
        "hint": "optional: `uv tool install pixelrag` for the PixelRAG backend",
    }


def check_network() -> dict:
    import socket

    targets = [("developer.mozilla.org", 443), ("example.com", 443)]
    for host, port in targets:
        try:
            with socket.create_connection((host, port), timeout=5):
                return {"name": "network", "ok": True, "detail": f"{host}:{port} reachable", "hint": ""}
        except Exception:  # noqa: BLE001
            continue
    return {
        "name": "network",
        "ok": False,
        "detail": "no test host reachable",
        "hint": "check proxy / offline; local file:// captures still work",
    }


def check_skill_install() -> dict:
    home = Path.home()
    candidates = [home / ".agents" / "skills" / "pixelvision", home / ".dsh" / "skills" / "pixelvision"]
    found = [str(p) for p in candidates if (p / "SKILL.md").is_file()]
    if found:
        return {"name": "skill installed", "ok": True, "detail": ", ".join(found), "hint": ""}
    return {
        "name": "skill installed",
        "ok": True,
        "detail": "not installed globally (the repo still works in place)",
        "hint": "run scripts/install.ps1 or scripts/install.sh to expose it to your agents",
    }


CHECKS = [check_python, check_pillow, check_playwright, check_browser, check_pixelshot,
          check_network, check_skill_install]


def main() -> int:
    results = [check() for check in CHECKS]
    hard_failures = [r for r in results if not r["ok"]]

    if "--json" in sys.argv:
        print(json.dumps({"ok": not hard_failures, "checks": results}, ensure_ascii=False, indent=2))
    else:
        print(f"pixelvision doctor  ({platform.system()} {platform.release()})\n")
        for r in results:
            mark = "ok  " if r["ok"] else "FAIL"
            print(f"  [{mark}] {r['name']:<22} {r['detail']}")
            if r["hint"]:
                print(f"         -> {r['hint']}")
        print()
        if hard_failures:
            print(f"{len(hard_failures)} check(s) failed. Fix the FAIL lines above, then retry.")
        else:
            print("Ready. Try: python scripts/capture.py https://example.com -o ./shots")
    return 1 if hard_failures else 0


if __name__ == "__main__":
    sys.exit(main())
