#!/usr/bin/env python3
"""Render a web page to screenshot tiles that a vision model can actually read.

Design goal: one command in, a manifest + a stack of readable JPEG tiles out. The
tiles are sized so a vision model does *not* downscale them (long edge <= 1568 px),
which is the difference between "the model sees a page" and "the model sees grey mush".

Backends
--------
playwright  Self-contained renderer (default). Drives Chrome / Edge / Chromium through
            Playwright. Handles lazy loading, network-idle waits and page-height races.
pixelshot   Delegates to the `pixelshot` CLI from PixelRAG (github.com/StarTrail-org/PixelRAG).
            Use it when it is already installed and you want PixelRAG's exact tiling.

Usage
-----
    python capture.py https://example.com -o ./shots
    python capture.py https://example.com -o ./shots --tile-height 1568 --viewport-width 1280
    python capture.py https://a.com https://b.com -o ./shots --backend pixelshot --workers 4

Output layout (same shape as `pixelshot`, plus a richer manifest):

    shots/example.com.png.tiles/
        page.png          full-page capture (kept for re-cropping; optional)
        tiles.json        manifest: tiles in reading order, page height, warnings
        tile_0000.jpg     top of the page
        tile_0001.jpg     next slice down
        ...

Exit codes: 0 ok, 1 render failed, 2 bad usage / missing dependency.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_TILE_HEIGHT = 1568   # vision models downscale images whose long edge exceeds this
DEFAULT_VIEWPORT_WIDTH = 1280
DEFAULT_MAX_HEIGHT = 30000   # hard stop: ~19 tiles, enough for a long article
DEFAULT_QUALITY = 85
MANIFEST_NAME = "tiles.json"

WINDOWS_BROWSER_CANDIDATES = [
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
    r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe",
]

POSIX_BROWSER_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/microsoft-edge",
    "/snap/bin/chromium",
]


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #

def slugify(url: str) -> str:
    """Turn a URL into a filesystem-safe stem, matching pixelshot's convention."""
    if url.startswith("file://"):
        from urllib.parse import unquote, urlparse

        name = Path(unquote(urlparse(url).path)).name or "page"
        return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._-")[:120] or "page"
    text = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", url)
    text = text.rstrip("/")
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", text)
    text = text.strip("._-")
    return (text or "page")[:120]


def now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def eprint(*args) -> None:
    print(*args, file=sys.stderr)


def expand(path: str) -> str:
    return os.path.expandvars(os.path.expanduser(path))


def find_browser() -> tuple[str | None, str | None]:
    """Return (channel, executable_path) for a Chromium-family browser.

    `channel` is what Playwright prefers ("chrome" / "msedge"); `executable_path`
    is the fallback for builds Playwright does not ship a channel name for.
    Returns (None, None) when nothing is found and Playwright's own browsers should
    be tried.
    """
    explicit = os.environ.get("CHROME_PATH") or os.environ.get("PIXELVISION_BROWSER")
    if explicit:
        path = expand(explicit)
        if Path(path).is_file():
            channel = "chrome"
            low = path.lower()
            if "msedge" in low or "edge" in low:
                channel = "msedge"
            return channel, path
        eprint(f"[warn] CHROME_PATH points at a missing file: {path}")

    candidates = WINDOWS_BROWSER_CANDIDATES if os.name == "nt" else POSIX_BROWSER_CANDIDATES
    for raw in candidates:
        path = expand(raw)
        if Path(path).is_file():
            low = path.lower()
            if "msedge" in low or "edge" in low:
                return "msedge", path
            return "chrome", path
    return None, None


def find_pixelshot(extra: str | None = None) -> str | None:
    """Locate the pixelshot CLI: explicit flag, PATH, or the venv next to this script."""
    if extra:
        return extra if Path(extra).is_file() else None
    for name in ("pixelshot", "pixelshot.exe"):
        found = shutil.which(name)
        if found:
            return found
    here = Path(__file__).resolve().parent.parent
    for rel in ("Scripts/pixelshot.exe", "bin/pixelshot"):
        candidate = here / ".venv" / rel
        if candidate.is_file():
            return str(candidate)
    return None


def load_pillow():
    try:
        from PIL import Image  # noqa: F401
    except ImportError:  # pragma: no cover - exercised only on a bare interpreter
        raise SystemExit(
            "pillow is required (tiling and cropping).\n"
            "  python -m pip install pillow\n"
            "or run scripts/bootstrap.ps1 / scripts/bootstrap.sh from the repo root."
        )
    from PIL import Image as _Image
    return _Image


# --------------------------------------------------------------------------- #
# tiling
# --------------------------------------------------------------------------- #

def tile_image(
    image_path: Path,
    out_dir: Path,
    tile_height: int = DEFAULT_TILE_HEIGHT,
    quality: int = DEFAULT_QUALITY,
    stem: str = "tile",
) -> list[str]:
    """Slice a tall screenshot into JPEG tiles of at most `tile_height` pixels.

    Tiles never span a tile boundary, so no line of text is cut in half and
    repeated across two images. A final sliver shorter than 24 px is merged into
    the previous tile instead of being emitted as its own unreadable strip.
    """
    Image = load_pillow()
    out_dir.mkdir(parents=True, exist_ok=True)
    names: list[str] = []
    with Image.open(image_path) as img:
        img = img.convert("RGB")
        width, height = img.size
        bands: list[tuple[int, int]] = []
        y = 0
        while y < height:
            bottom = min(y + tile_height, height)
            bands.append((y, bottom))
            y = bottom
        if len(bands) > 1 and bands[-1][1] - bands[-1][0] < 24:
            bands.pop()
            prev_start, _ = bands[-1]
            bands[-1] = (prev_start, height)
        for index, (top, bottom) in enumerate(bands):
            name = f"{stem}_{index:04d}.jpg"
            img.crop((0, top, width, bottom)).save(
                out_dir / name, "JPEG", quality=quality, optimize=True
            )
            names.append(name)
    return names


# --------------------------------------------------------------------------- #
# playwright backend
# --------------------------------------------------------------------------- #

def _launch(playwright, browser_hint: tuple[str | None, str | None], headless: bool = True):
    """Launch a Chromium-family browser, preferring channels over raw paths."""
    channel, executable = browser_hint
    attempts: list[dict] = []
    if channel and os.name == "nt" and executable and not os.environ.get("PIXELVISION_FORCE_PATH"):
        # Channels pick the right build/args; keep the path as a fallback.
        attempts.append({"channel": channel, "headless": headless})
    if executable:
        attempts.append({"executable_path": executable, "headless": headless})
    attempts.append({"channel": "chrome", "headless": headless})
    attempts.append({"channel": "msedge", "headless": headless})
    attempts.append({"headless": headless})  # Playwright's bundled chromium

    errors: list[str] = []
    for kwargs in attempts:
        try:
            return playwright.chromium.launch(**kwargs)
        except Exception as exc:  # noqa: BLE001 - we want the last real reason
            errors.append(f"{kwargs}: {exc}".replace("\n", " ")[:300])
    raise RuntimeError(
        "could not launch any browser. Tried:\n  - " + "\n  - ".join(errors)
        + "\nInstall Chrome/Edge, run `playwright install chromium`, or set CHROME_PATH."
    )


def _settle(page, wait_network_idle: bool, extra_wait_ms: int, scroll: bool) -> list[str]:
    """Let client-rendered content arrive, then report anything odd."""
    warnings: list[str] = []
    try:
        page.wait_for_load_state("load", timeout=30000)
    except Exception:  # noqa: BLE001
        warnings.append("load event did not fire within 30s")
    if wait_network_idle:
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:  # noqa: BLE001
            warnings.append("network never went idle within 8s (page may still be rendering)")

    if scroll:
        # Trigger IntersectionObserver / lazy <img> loading down the page.
        try:
            page.evaluate(
                """async () => {
                    const step = Math.max(300, Math.floor(window.innerHeight * 0.9));
                    const cap = 40000;
                    for (let y = 0; y < Math.min(document.body.scrollHeight, cap); y += step) {
                        window.scrollTo(0, y);
                        await new Promise(r => setTimeout(r, 120));
                    }
                    window.scrollTo(0, 0);
                    await new Promise(r => setTimeout(r, 150));
                }"""
            )
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"scroll pass failed: {exc}".replace("\n", " ")[:200])

    if extra_wait_ms > 0:
        page.wait_for_timeout(extra_wait_ms)
    return warnings


def render_with_playwright(
    url: str,
    out_root: Path,
    *,
    tile_height: int = DEFAULT_TILE_HEIGHT,
    viewport_width: int = DEFAULT_VIEWPORT_WIDTH,
    quality: int = DEFAULT_QUALITY,
    wait_network_idle: bool = True,
    extra_wait_ms: int = 0,
    max_height: int = DEFAULT_MAX_HEIGHT,
    full_page: bool = True,
    scroll: bool = True,
    scale: int = 1,
    selector: str | None = None,
    timeout_ms: int = 60000,
) -> dict:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit(
            "playwright is not installed.\n"
            "  python -m pip install playwright\n"
            "or run scripts/bootstrap.ps1 / scripts/bootstrap.sh from the repo root."
        )

    slug = slugify(url)
    tile_dir = out_root / f"{slug}.png.tiles"
    tile_dir.mkdir(parents=True, exist_ok=True)
    for stale in tile_dir.glob("tile_*.jpg"):
        stale.unlink()

    hint = find_browser()
    warnings: list[str] = []
    started = time.time()
    css_tile_height = max(200, tile_height // max(1, scale))
    if viewport_width * scale > 1600:
        warnings.append(
            f"tiles are {viewport_width * scale}px wide after --scale {scale}; "
            "vision models downscale above ~1568px, so use a narrower --viewport-width"
        )

    with sync_playwright() as p:
        browser = _launch(p, hint)
        try:
            context = browser.new_context(
                viewport={"width": viewport_width, "height": css_tile_height},
                device_scale_factor=scale,
                reduced_motion="reduce",
                ignore_https_errors=False,
            )
            page = context.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(f"navigation failed for {url}: {exc}".replace("\n", " ")[:400])

            warnings.extend(_settle(page, wait_network_idle, extra_wait_ms, scroll))

            title = ""
            final_url = url
            try:
                title = page.title()
                final_url = page.url
            except Exception:  # noqa: BLE001
                pass

            def measure() -> int:
                return int(
                    page.evaluate(
                        """() => {
                            const d = document.documentElement, b = document.body || {};
                            return Math.max(
                                d ? d.scrollHeight : 0,
                                b.scrollHeight || 0,
                                d ? d.clientHeight : 0
                            );
                        }"""
                    )
                )

            # Client-rendered pages can grow after the first measurement; take the
            # max over a couple of samples so we do not truncate the page.
            height = measure()
            for _ in range(2):
                page.wait_for_timeout(400)
                height = max(height, measure())
            complete = height > 0
            if max_height and height > max_height:
                warnings.append(
                    f"page height {height}px exceeds --max-height {max_height}px; capture may be truncated"
                )

            # page.png is the full-page capture: it is what crop.py crops from, so it
            # stays on disk even when only the tiles are read.
            page_path = tile_dir / "page.png"
            if selector:
                try:
                    locator = page.locator(selector).first
                    locator.wait_for(state="visible", timeout=timeout_ms)
                    locator.screenshot(path=str(page_path), timeout=timeout_ms)
                    complete = True
                except Exception as exc:  # noqa: BLE001
                    raise RuntimeError(
                        f"selector {selector!r} could not be captured: {exc}".replace("\n", " ")[:300]
                    )
            elif full_page:
                page.screenshot(path=str(page_path), full_page=True, timeout=timeout_ms)
            else:
                page.screenshot(path=str(page_path), full_page=False, timeout=timeout_ms)
                complete = True
            context.close()
        finally:
            browser.close()

    tiles = tile_image(page_path, tile_dir, tile_height, quality)
    if not tiles:
        raise RuntimeError("tiling produced no images")

    if len(tiles) > 25:
        warnings.append(f"{len(tiles)} tiles captured; read them in batches and crop when text is small")

    manifest = {
        "url": url,
        "final_url": final_url,
        "title": title,
        "page_height": height,
        "viewport": {"width": viewport_width, "height": css_tile_height},
        "tile_height": tile_height,
        "scale": scale,
        "selector": selector,
        "tiles": tiles,
        "complete": complete,
        "backend": f"playwright:{hint[0] or 'bundled'}",
        "captured_at": now_iso(),
        "elapsed_s": round(time.time() - started, 2),
        "warnings": warnings,
        "manifest_path": str(tile_dir / MANIFEST_NAME),
    }
    (tile_dir / MANIFEST_NAME).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


# --------------------------------------------------------------------------- #
# pixelshot backend (PixelRAG)
# --------------------------------------------------------------------------- #

def render_with_pixelshot(
    urls: list[str],
    out_root: Path,
    *,
    tile_height: int = DEFAULT_TILE_HEIGHT,
    viewport_width: int = DEFAULT_VIEWPORT_WIDTH,
    quality: int = DEFAULT_QUALITY,
    wait_network_idle: bool = True,
    workers: int = 4,
    pixelshot_bin: str | None = None,
) -> dict:
    exe = find_pixelshot(pixelshot_bin)
    if not exe:
        raise SystemExit(
            "pixelshot not found. Install PixelRAG's renderer:\n"
            "  uv tool install pixelrag      # or: pipx install pixelrag\n"
            "or pass --pixelshot /path/to/pixelshot, or use --backend playwright."
        )

    out_root.mkdir(parents=True, exist_ok=True)
    cmd = [exe, *urls, "-o", str(out_root), "--tile-height", str(tile_height),
           "--viewport-width", str(viewport_width), "--quality", str(quality), "--workers", str(workers)]
    if wait_network_idle:
        cmd.append("--wait-network-idle")

    env = dict(os.environ)
    channel, executable = find_browser()
    if executable and not env.get("CHROME_PATH"):
        env["CHROME_PATH"] = executable

    started = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=900)
    if proc.returncode != 0:
        raise RuntimeError(
            "pixelshot failed:\n" + (proc.stderr or proc.stdout or "")[-2000:]
        )

    manifests = []
    for url in urls:
        tile_dir = out_root / f"{slugify(url)}.png.tiles"
        mpath = tile_dir / MANIFEST_NAME
        if not mpath.is_file():
            manifests.append({"url": url, "error": f"no manifest at {mpath}", "complete": False})
            continue
        data = json.loads(mpath.read_text(encoding="utf-8"))
        data.setdefault("backend", "pixelshot")
        data.setdefault("complete", True)
        data.setdefault("warnings", [])
        data["manifest_path"] = str(mpath)
        data["elapsed_s"] = round(time.time() - started, 2)
        mpath.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        manifests.append(data)

    return manifests[0] if len(manifests) == 1 else {"results": manifests}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="capture.py",
        description="Render web pages to vision-readable screenshot tiles.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("urls", nargs="+", help="http(s) URLs, local HTML files, PDFs or images")
    p.add_argument("-o", "--output", default="./shots", help="output directory")
    p.add_argument("--backend", choices=["playwright", "pixelshot", "auto"], default="auto")
    p.add_argument("--pixelshot", help="path to the pixelshot executable")
    p.add_argument("--tile-height", type=int, default=DEFAULT_TILE_HEIGHT,
                   help="max tile height in px; keep <= 1568 so vision models do not downscale")
    p.add_argument("--viewport-width", type=int, default=DEFAULT_VIEWPORT_WIDTH)
    p.add_argument("--quality", type=int, default=DEFAULT_QUALITY, help="JPEG quality 1-100")
    p.add_argument("--workers", type=int, default=4, help="parallel browsers (pixelshot backend)")
    p.add_argument("--max-height", type=int, default=DEFAULT_MAX_HEIGHT,
                   help="warn when the page is taller than this")
    p.add_argument("--scale", type=int, default=1,
                   help="device pixel ratio; 2 doubles text sharpness but halves content per tile")
    p.add_argument("--selector", help="capture only this CSS selector instead of the whole page")
    p.add_argument("--wait-ms", type=int, default=0, help="extra settle time before capture")
    p.add_argument("--no-network-idle", action="store_true", help="skip the network-quiet wait")
    p.add_argument("--no-scroll", action="store_true", help="skip the lazy-load scroll pass")
    p.add_argument("--no-full-page", action="store_true", help="capture only the first viewport")
    p.add_argument("--json", action="store_true", help="print the manifest as JSON and nothing else")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    out_root = Path(args.output).expanduser().resolve()

    backend = args.backend
    if backend == "auto":
        backend = "playwright"
    try:
        if backend == "pixelshot":
            result = render_with_pixelshot(
                args.urls, out_root,
                tile_height=args.tile_height,
                viewport_width=args.viewport_width,
                quality=args.quality,
                wait_network_idle=not args.no_network_idle,
                workers=args.workers,
                pixelshot_bin=args.pixelshot,
            )
            results = result.get("results", [result])
        else:
            results = []
            for url in args.urls:
                if not url.lower().startswith(("http://", "https://")):
                    eprint(f"[info] {url} is a local file; it will be opened as a file:// URL")
                target = url if url.lower().startswith(("http://", "https://")) else str(Path(url).expanduser().resolve().as_uri())
                results.append(
                    render_with_playwright(
                        target, out_root,
                        tile_height=args.tile_height,
                        viewport_width=args.viewport_width,
                        quality=args.quality,
                        wait_network_idle=not args.no_network_idle,
                        extra_wait_ms=args.wait_ms,
                        max_height=args.max_height,
                        full_page=not args.no_full_page,
                        scroll=not args.no_scroll,
                        scale=max(1, args.scale),
                        selector=args.selector,
                    )
                )
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        eprint(f"[error] {exc}")
        return 1

    failures = [r for r in results if r.get("error")]
    if failures and len(failures) == len(results):
        for r in failures:
            eprint(f"[error] {r['url']}: {r['error']}")
        return 1

    if args.json:
        print(json.dumps(results[0] if len(results) == 1 else {"results": results}, ensure_ascii=False, indent=2))
    else:
        for r in results:
            if r.get("error"):
                eprint(f"[error] {r['url']}: {r['error']}")
                continue
            print(f"{r['url']}")
            print(f"  title      {r.get('title', '')}")
            print(f"  manifest   {r['manifest_path']}")
            print(f"  tiles      {len(r['tiles'])} x {r['tile_height']}px  ({r['viewport']['width']}px wide)")
            print(f"  page       {r.get('page_height', '?')}px  complete={r.get('complete')}")
            for w in r.get("warnings") or []:
                print(f"  warning    {w}")
        print("\nNext: read the manifest, then read every tile it lists in order.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
