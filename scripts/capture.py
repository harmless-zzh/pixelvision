#!/usr/bin/env python3
"""Render a web page to screenshot tiles that a vision model can actually read.

Design goal: one command in, a manifest + a stack of readable JPEG tiles out. The
tiles are sized so a vision model does *not* downscale them (long edge <= 1568 px),
which is the difference between "the model sees a page" and "the model sees grey mush".

Manifest contract
-----------------
`complete` is computed from what was actually written, never assumed. It is true
only when `scope == "page"` and `captured_height` covers `page_height`.

    scope            page | viewport | selector | pdf-viewer
    page_height      height of the whole document, in CSS px
    captured_height  height actually written to page.png, in CSS px
    complete         true only for a full-page capture that was not capped

A capture that is partial for any reason (selector, --no-full-page, PDF viewer,
--max-height cap) is `complete: false` with an explicit warning. That lets an
agent tell "the whole page" from "one element" without guessing.

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

from _console import force_utf8_stdio

VISION_MAX_EDGE = 1568      # vision models downscale long edges above this
DEFAULT_TILE_HEIGHT = VISION_MAX_EDGE
DEFAULT_VIEWPORT_WIDTH = 1280
DEFAULT_MAX_HEIGHT = 30000   # real cap: the capture is cropped, ~19 tiles max
DEFAULT_QUALITY = 85
MIN_SLIVER = 24              # shorter tail bands are rebalanced, never emitted alone
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
    print(" ".join(str(a) for a in args), file=sys.stderr)


def expand(path: str) -> str:
    return os.path.expandvars(os.path.expanduser(path))


LOCAL_FILE_SUFFIXES = {
    ".html", ".htm", ".xhtml", ".mhtml", ".pdf", ".png", ".jpg", ".jpeg",
    ".webp", ".gif", ".svg", ".txt", ".md",
}

_HOSTLIKE = re.compile(r"^[\w-]+(?:\.[\w-]+)+(?::\d+)?(?:[/?#].*)?$")

# Loopback is a dev server far more often than it is a TLS endpoint; https against
# `python -m http.server` or a Vite dev port fails with ERR_SSL_PROTOCOL_ERROR.
_LOOPBACK = re.compile(r"^(?:localhost|127(?:\.\d{1,3}){3}|\[::1\]|::1)(?::\d+)?(?:[/?#].*)?$", re.I)


def _error_result(url: str, error: str) -> dict:
    """Same shape as a successful manifest, so a caller can iterate without special cases."""
    return {"url": url, "error": error, "complete": False}


def normalize_target(raw: str) -> str:
    """Turn whatever the caller typed into a URL or a file:// URI.

    "example.com" has to become https://example.com. Treating it as a local path fails
    with ERR_FILE_NOT_FOUND only after a browser has launched, and a bare host is the
    most natural thing for a person or a model to write.
    """
    text = raw.strip()
    if not text:
        raise FileNotFoundError("empty URL or path")
    low = text.lower()
    if low.startswith(("http://", "https://", "file://")):
        return text
    if _LOOPBACK.match(text):
        return "http://" + text

    path = Path(expand(text))
    if path.is_dir():
        raise FileNotFoundError(
            f"{path} is a directory; pass the HTML file inside it, or a URL"
        )
    if path.suffix.lower() in LOCAL_FILE_SUFFIXES:
        if not path.is_file():
            raise FileNotFoundError(
                f"no such file: {path} (if you meant a website, write https://{text})"
            )
        return path.resolve().as_uri()
    if path.is_file():
        return path.resolve().as_uri()
    if "\\" not in text and _HOSTLIKE.match(text):
        return "https://" + text
    raise FileNotFoundError(
        f"no such file: {path} (if you meant a website, write https://{text})"
    )


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


def _bounded_int(flag: str, low: int, high: int):
    """argparse type that rejects values outside [low, high] with a readable error."""

    def convert(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{flag} must be an integer, got {text!r}")
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(
                f"{flag} must be between {low} and {high}, got {value}"
            )
        return value

    return convert


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

    Band boundaries fall on fixed pixel rows, so a line of text can straddle two
    tiles; read adjacent tiles together instead of treating either as complete.
    A tail shorter than 24 px is rebalanced with the previous band, which keeps
    every tile within the tile height instead of emitting an unreadable strip or
    an oversized merge.
    """
    if not 1 <= int(tile_height) <= VISION_MAX_EDGE:
        raise ValueError(f"tile_height must be 1..{VISION_MAX_EDGE}, got {tile_height}")
    if not 1 <= int(quality) <= 100:
        raise ValueError(f"quality must be 1..100, got {quality}")

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
        if len(bands) > 1 and bands[-1][1] - bands[-1][0] < MIN_SLIVER:
            top = bands[-2][0]
            total = height - top
            split = top + total // 2
            if split > top and split < height:
                bands[-2] = (top, split)
                bands[-1] = (split, height)
        for index, (top, bottom) in enumerate(bands):
            name = f"{stem}_{index:04d}.jpg"
            img.crop((0, top, width, bottom)).save(
                out_dir / name, "JPEG", quality=quality, optimize=True
            )
            names.append(name)
    return names


# --------------------------------------------------------------------------- #
# manifest
# --------------------------------------------------------------------------- #

def build_manifest(
    *,
    url: str,
    final_url: str,
    title: str,
    page_height: int,
    captured_height: int,
    tile_height: int,
    viewport: dict,
    scale: int,
    selector: str | None,
    tiles: list[str],
    scope: str,
    backend: str,
    warnings: list[str],
    started: float,
) -> dict:
    """Assemble the manifest and derive `complete` from what was captured."""
    warnings = list(warnings)
    if scope == "page" and page_height > 0 and captured_height < page_height - 1:
        warnings.append(
            f"captured {captured_height}px of a {page_height}px page; the rest was not "
            "rendered (raise --max-height, or scroll the page shorter, to capture it)"
        )
    complete = (
        scope == "page"
        and page_height > 0
        and captured_height >= page_height - 1
    )
    return {
        "url": url,
        "final_url": final_url,
        "title": title,
        "scope": scope,
        "page_height": page_height,
        "captured_height": captured_height,
        "viewport": viewport,
        "tile_height": tile_height,
        "scale": scale,
        "selector": selector,
        "tiles": tiles,
        "complete": complete,
        "backend": backend,
        "captured_at": now_iso(),
        "elapsed_s": round(time.time() - started, 2),
        "warnings": warnings,
    }


def write_manifest(tile_dir: Path, manifest: dict) -> Path:
    """Write tiles.json atomically so a failure never leaves a half-written manifest."""
    target = tile_dir / MANIFEST_NAME
    # Record the on-disk location in the file itself: a reader that finds tiles.json by
    # globbing should not have to re-derive where it was told to look.
    manifest["manifest_path"] = str(target)
    tmp = tile_dir / (MANIFEST_NAME + ".tmp")
    tmp.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(tmp, target)
    return target


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


def is_pdf_url(url: str) -> bool:
    """True for URLs that end in .pdf (query string and fragment ignored)."""
    clean = url.split("#", 1)[0].split("?", 1)[0].lower()
    return clean.endswith(".pdf")


def _image_height(path: Path) -> int:
    Image = load_pillow()
    with Image.open(path) as img:
        return img.height


def _verify_tiles(tile_dir: Path, tiles: list[str], warnings: list[str]) -> None:
    """A manifest that lists tiles which are not on disk would send the reader nowhere."""
    missing = [name for name in tiles if not (tile_dir / name).is_file()]
    if missing:
        warnings.append(
            f"{len(missing)} of {len(tiles)} tiles are missing on disk "
            f"(first: {missing[0]}); re-run the capture"
        )


def _crop_top(path: Path, keep_px: int) -> None:
    """Keep only the top `keep_px` rows of page.png, in place."""
    Image = load_pillow()
    tmp = path.with_name(path.stem + ".crop.png")
    with Image.open(path) as img:
        img.crop((0, 0, img.width, min(keep_px, img.height))).save(tmp, "PNG")
    os.replace(tmp, path)


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
    storage_state: str | None = None,
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
    # Remove the previous result first: a render failure must leave no manifest,
    # so "no tiles.json = failed capture" stays true across re-captures.
    for stale in tile_dir.glob("tile_*.jpg"):
        stale.unlink()
    manifest_path = tile_dir / MANIFEST_NAME
    manifest_path.unlink(missing_ok=True)
    (tile_dir / (MANIFEST_NAME + ".tmp")).unlink(missing_ok=True)

    hint = find_browser()
    warnings: list[str] = []
    started = time.time()
    css_tile_height = max(200, tile_height // max(1, scale))
    if viewport_width * scale > VISION_MAX_EDGE:
        warnings.append(
            f"tiles are {viewport_width * scale}px wide after --scale {scale}; "
            f"vision models downscale above ~{VISION_MAX_EDGE}px, so use a narrower --viewport-width"
        )

    title = ""
    final_url = url
    page_height = 0
    captured_height = 0
    scope = "page"

    with sync_playwright() as p:
        browser = _launch(p, hint)
        try:
            context_kwargs = {
                "viewport": {"width": viewport_width, "height": css_tile_height},
                "device_scale_factor": scale,
                "reduced_motion": "reduce",
                "ignore_https_errors": False,
            }
            if storage_state:
                context_kwargs["storage_state"] = storage_state
            context = browser.new_context(**context_kwargs)
            page = context.new_page()
            served_pdf = False
            try:
                response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                # A PDF can be served from a path with no .pdf suffix (arxiv.org/pdf/…),
                # so trust the response over the URL before claiming the capture is whole.
                if response is not None:
                    ctype = (response.headers or {}).get("content-type", "")
                    served_pdf = "application/pdf" in ctype.lower()
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(f"navigation failed for {url}: {exc}".replace("\n", " ")[:400])

            warnings.extend(_settle(page, wait_network_idle, extra_wait_ms, scroll))

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
            page_height = height

            if selector:
                scope = "selector"
                warnings.append(
                    "selector capture: page.png contains only the matched element; "
                    "page_height describes the whole document"
                )
                if max_height and page_height > max_height:
                    warnings.append("--max-height is ignored when --selector is used")
            elif not full_page:
                scope = "viewport"
                warnings.append(
                    "viewport capture: page.png contains only the first screen; "
                    "omit --no-full-page to capture the whole page"
                )
                if max_height and page_height > max_height:
                    warnings.append("--max-height is ignored when --no-full-page is used")
            elif served_pdf or is_pdf_url(final_url or url):
                scope = "pdf-viewer"
                warnings.append(
                    "PDF input uses the browser viewer: only the visible page is captured; "
                    "convert multi-page PDFs to images first"
                )

            # page.png is the full-page capture: it is what crop.py crops from, so it
            # stays on disk even when only the tiles are read.
            page_path = tile_dir / "page.png"
            if selector:
                try:
                    locator = page.locator(selector).first
                    locator.wait_for(state="visible", timeout=timeout_ms)
                    locator.screenshot(path=str(page_path), timeout=timeout_ms)
                except Exception as exc:  # noqa: BLE001
                    raise RuntimeError(
                        f"selector {selector!r} could not be captured: {exc}".replace("\n", " ")[:300]
                    )
            elif full_page:
                page.screenshot(path=str(page_path), full_page=True, timeout=timeout_ms)
            else:
                page.screenshot(path=str(page_path), full_page=False, timeout=timeout_ms)
            context.close()
        finally:
            browser.close()

    image_height = _image_height(page_path)

    # --max-height is a real cap now: crop the screenshot before tiling, warn, and
    # build_manifest appends the single warning for a capped page.
    if scope == "page" and max_height and page_height > max_height:
        keep_px = max_height * scale
        if image_height > keep_px:
            _crop_top(page_path, keep_px)
            image_height = keep_px

    captured_height = int(round(image_height / max(1, scale)))
    if scope == "page" and page_height:
        captured_height = min(captured_height, page_height)

    tiles = tile_image(page_path, tile_dir, tile_height, quality)
    if not tiles:
        raise RuntimeError("tiling produced no images")
    _verify_tiles(tile_dir, tiles, warnings)

    if len(tiles) > 20:
        warnings.append(f"{len(tiles)} tiles captured; read them in batches and crop when text is small")

    manifest = build_manifest(
        url=url,
        final_url=final_url,
        title=title,
        page_height=page_height,
        captured_height=captured_height,
        tile_height=tile_height,
        viewport={"width": viewport_width, "height": css_tile_height},
        scale=scale,
        selector=selector,
        tiles=tiles,
        scope=scope,
        backend=f"playwright:{hint[0] or 'bundled'}",
        warnings=warnings,
        started=started,
    )
    write_manifest(tile_dir, manifest)
    return manifest


# --------------------------------------------------------------------------- #
# pixelshot backend (PixelRAG)
# --------------------------------------------------------------------------- #

def _url_key(url: str) -> str:
    """Compare URLs across percent-encoding and separator differences."""
    from urllib.parse import unquote, urlparse

    parsed = urlparse((url or "").strip())
    if parsed.scheme == "file":
        path = str(Path(unquote(parsed.path)).resolve()).replace("\\", "/")
        return "file:" + path.lower()
    return (url or "").strip().rstrip("/").lower()


def _pixelshot_outputs(out_root: Path, started: float) -> list[tuple[Path, dict]]:
    """Manifests pixelshot wrote during this run, newest first.

    The directory name is not predictable: pixelshot flattens a local path
    (`examples/demo.html` -> `examples_demo.html.png.tiles`) where slugify() keeps the
    basename, so guessing it made every file capture look like a failed render.
    """
    fresh: list[tuple[Path, dict]] = []
    everything: list[tuple[Path, dict]] = []
    for mpath in out_root.glob("*.png.tiles/" + MANIFEST_NAME):
        try:
            item = (mpath, json.loads(mpath.read_text(encoding="utf-8")))
            stamp = mpath.stat().st_mtime
        except (OSError, ValueError):
            continue
        everything.append(item)
        if stamp >= started - 2:
            fresh.append(item)
    # If nothing was touched this run, pixelshot reused what was already on disk.
    chosen = fresh or everything
    chosen.sort(key=lambda item: item[0].stat().st_mtime, reverse=True)
    return chosen


def _match_pixelshot_output(url: str, produced: list[tuple[Path, dict]]):
    """Pick the manifest pixelshot wrote for this URL."""
    key = _url_key(url)
    for item in produced:
        if _url_key(item[1].get("url", "")) == key:
            return item
    # One URL, one fresh manifest: pixelshot may have rewritten the URL (redirect,
    # normalized file path), and the only candidate left is the answer.
    if len(produced) == 1:
        return produced[0]
    return None


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

    produced = _pixelshot_outputs(out_root, started)
    manifests = []
    unclaimed = list(produced)
    for url in urls:
        match = _match_pixelshot_output(url, unclaimed)
        if match is None:
            manifests.append(
                {"url": url,
                 "error": f"pixelshot reported success but wrote no manifest for {url} under {out_root}",
                 "complete": False}
            )
            continue
        unclaimed.remove(match)
        mpath, raw = match
        tile_dir = mpath.parent
        page_height = int(raw.get("page_height") or 0)
        tiles = list(raw.get("tiles") or [])
        upstream_complete = bool(raw.get("complete"))
        warnings = list(raw.get("warnings") or [])
        _verify_tiles(tile_dir, tiles, warnings)
        warnings.append(
            "rendered by pixelshot; its own manifest is kept as pixelshot.tiles.json and "
            "`complete` here is derived from what it claimed"
        )
        # Our manifest is the contract every reader follows, but pixelshot's file is not
        # ours to overwrite: copy it aside first, byte for byte.
        original = tile_dir / "pixelshot.tiles.json"
        if not original.exists():
            try:
                shutil.copyfile(mpath, original)
            except OSError as exc:  # noqa: BLE001
                warnings.append(f"could not keep a copy of pixelshot's manifest: {exc}")
        captured_height = page_height if (upstream_complete and page_height) else 0
        manifest = {
            "url": raw.get("url", url),
            "final_url": raw.get("url", url),
            "title": raw.get("title", ""),
            "scope": "page",
            "page_height": page_height,
            "captured_height": captured_height,
            "viewport": {"width": viewport_width, "height": tile_height},
            "tile_height": tile_height,
            "scale": 1,
            "selector": None,
            "tiles": tiles,
            # Derived, never copied: an older pixelshot manifest without `complete` is
            # reported as incomplete, which is the honest answer.
            "complete": bool(page_height and upstream_complete and captured_height >= page_height - 1),
            "backend": "pixelshot",
            "captured_at": now_iso(),
            "elapsed_s": round(time.time() - started, 2),
            "warnings": warnings,
        }
        written = write_manifest(tile_dir, manifest)
        manifest["manifest_path"] = str(written)
        manifests.append(manifest)

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
    p.add_argument(
        "urls",
        nargs="+",
        help="http(s) URLs, local HTML files, or images; PDFs open in the browser viewer "
             "(only the visible page is captured)",
    )
    p.add_argument("-o", "--output", default="./shots", help="output directory")
    p.add_argument("--backend", choices=["playwright", "pixelshot", "auto"], default="auto",
                   help="playwright, pixelshot, or auto to pick whichever can run")
    p.add_argument("--pixelshot", help="path to the pixelshot executable (pixelshot backend only)")
    p.add_argument("--tile-height", type=_bounded_int("--tile-height", 1, VISION_MAX_EDGE),
                   default=DEFAULT_TILE_HEIGHT,
                   help="max tile height in px; capped at 1568 so vision models do not downscale")
    p.add_argument("--viewport-width", type=_bounded_int("--viewport-width", 320, 3840),
                   default=DEFAULT_VIEWPORT_WIDTH,
                   help="CSS px viewport width; keep the rendered width <= 1568")
    p.add_argument("--quality", type=_bounded_int("--quality", 1, 100),
                   default=DEFAULT_QUALITY, help="JPEG quality 1-100")
    p.add_argument("--workers", type=_bounded_int("--workers", 1, 16),
                   default=4, help="parallel browsers (pixelshot backend)")
    p.add_argument("--max-height", type=_bounded_int("--max-height", 0, 200000),
                   default=DEFAULT_MAX_HEIGHT,
                   help="stop capture after this many CSS px (0 = no cap); capped captures are complete=false")
    p.add_argument("--scale", type=_bounded_int("--scale", 1, 4), default=1,
                   help="device pixel ratio; 2 doubles text sharpness but halves content per tile")
    p.add_argument("--selector", help="capture only this CSS selector instead of the whole page")
    p.add_argument("--storage-state",
                   help="Playwright storage_state JSON (cookies + localStorage) for logged-in pages")
    p.add_argument("--wait-ms", type=_bounded_int("--wait-ms", 0, 120000),
                   default=0, help="extra settle time before capture")
    p.add_argument("--no-network-idle", action="store_true", help="skip the network-quiet wait")
    p.add_argument("--no-scroll", action="store_true", help="skip the lazy-load scroll pass")
    p.add_argument("--no-full-page", action="store_true", help="capture only the first viewport")
    p.add_argument("--json", action="store_true", help="print the manifest as JSON and nothing else")
    return p


def _playwright_can_run() -> bool:
    """True when playwright is importable and some Chromium build is available."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    if find_browser()[1]:
        return True
    try:
        with sync_playwright() as p:
            exe = p.chromium.executable_path
            return bool(exe and Path(exe).is_file())
    except Exception:  # noqa: BLE001
        return False


def main(argv: list[str] | None = None) -> int:
    force_utf8_stdio()
    args = build_parser().parse_args(argv)
    out_root = Path(args.output).expanduser().resolve()

    storage_state = None
    if args.storage_state:
        state_path = Path(args.storage_state).expanduser().resolve()
        if not state_path.is_file():
            eprint(f"[error] --storage-state file not found: {state_path}")
            return 2
        storage_state = str(state_path)

    backend = args.backend
    if backend == "auto":
        if _playwright_can_run():
            backend = "playwright"
        elif find_pixelshot(args.pixelshot):
            backend = "pixelshot"
        else:
            backend = "playwright"
    if backend == "pixelshot" and storage_state:
        eprint("[warn] --storage-state is ignored by the pixelshot backend")

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
            for raw in args.urls:
                try:
                    target = normalize_target(raw)
                except FileNotFoundError as exc:
                    results.append(_error_result(raw, str(exc)))
                    continue
                if target.startswith("file://"):
                    # Not an error: on Windows a shell treats stderr output as failure, so
                    # keep it on stdout unless stdout is reserved for the JSON payload.
                    note = f"[info] {raw} is a local file; rendering the file as it exists on disk"
                    (eprint if args.json else print)(note)
                try:
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
                            scale=args.scale,
                            selector=args.selector,
                            storage_state=storage_state,
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    # One bad URL must not cost the caller every other page in the list;
                    # the collected error is reported once, with the other results.
                    results.append(_error_result(raw, str(exc)))
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        eprint(f"[error] {exc}")
        return 1

    failures = [r for r in results if r.get("error")]
    all_failed = bool(failures) and len(failures) == len(results)

    if args.json:
        # A scripting caller needs the error text in the payload, not only on stderr.
        print(json.dumps(results[0] if len(results) == 1 else {"results": results}, ensure_ascii=False, indent=2))
        return 1 if all_failed else 0

    if all_failed:
        for r in failures:
            eprint(f"[error] {r['url']}: {r['error']}")
        return 1

    for r in results:
        if r.get("error"):
            eprint(f"[error] {r['url']}: {r['error']}")
            continue
        print(r["url"])
        print(f"  title      {r.get('title', '')}")
        print(f"  manifest   {r['manifest_path']}")
        print(f"  tiles      {len(r['tiles'])} x {r['tile_height']}px  ({r['viewport']['width']}px wide)")
        print(
            f"  page       {r.get('captured_height', '?')}/{r.get('page_height', '?')}px"
            f"  scope={r.get('scope')}  complete={r.get('complete')}"
        )
        for w in r.get("warnings") or []:
            print(f"  warning    {w}")
    print("\nNext: read the manifest, then read every tile it lists in order.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
