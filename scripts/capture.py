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

__version__ = "0.0.2"

import argparse
import datetime as _dt
import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from _console import force_utf8_stdio

VISION_MAX_EDGE = 1568      # vision models downscale long edges above this
DEFAULT_TILE_HEIGHT = VISION_MAX_EDGE
DEFAULT_VIEWPORT_WIDTH = 1280
DEFAULT_VIEWPORT_HEIGHT = 900
DEFAULT_MAX_HEIGHT = 30000   # real cap: the capture is clipped, ~19 tiles max
DEFAULT_QUALITY = 85
DEFAULT_SELECTOR_TIMEOUT_MS = 15000
MIN_SLIVER = 24              # shorter tail bands are rebalanced, never emitted alone
MANIFEST_NAME = "tiles.json"
MANIFEST_SCHEMA_VERSION = 1  # bump only for a breaking manifest change
MAX_CAPTURE_WIDTH = 4096     # safety bound for horizontal page overflow
SCROLL_LIMIT = 40000         # lazy-load pre-scroll stops here (performance guard)
MAX_TITLE_CHARS = 300
# Chromium's screenshot raster wraps at 16384 device px on Windows (no exception).
# Capture in segments below that limit, then stitch; 8192 leaves headroom for
# drivers that off-by-one the texture bound.
MAX_RASTER_HEIGHT = 8192
# Refuse captures that would allocate an unreasonable page.png (≈500 MB RGB).
MAX_RASTER_PIXELS = 180_000_000

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


_SENSITIVE_QUERY = re.compile(
    r"(?i)^(?:access_?token|api_?key|auth|code|key|password|passwd|secret|session|"
    r"sig|signature|token)$"
)


def _redact_query(query: str) -> str:
    """Replace values of credential-bearing query parameters with `***`."""
    if not query:
        return query
    from urllib.parse import parse_qsl, urlencode

    pairs = parse_qsl(query, keep_blank_values=True)
    changed = False
    redacted = []
    for name, value in pairs:
        if _SENSITIVE_QUERY.match(name):
            redacted.append((name, "***"))
            changed = True
        else:
            redacted.append((name, value))
    return urlencode(redacted, doseq=True, safe="*") if changed else query


def redact_url(url: str) -> str:
    """Hide embedded credentials and query tokens before a URL is written or printed."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return _clean_text(url, limit=2000)
    query = _redact_query(parts.query)
    userinfo = ""
    if parts.username is not None or parts.password is not None:
        host = parts.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        try:
            if parts.port:
                host = f"{host}:{parts.port}"
        except ValueError:
            pass
        userinfo = f"***@{host}"
    if not userinfo and query == parts.query:
        return url
    netloc = userinfo or parts.netloc
    return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))


def _clean_text(text: object, limit: int = MAX_TITLE_CHARS) -> str:
    """Make page-derived text safe for JSON and terminal display.

    Titles can contain lone surrogates (pixelshot manifests) or control characters
    (non-Chromium sources). Neither belongs in a manifest a model will read.
    """
    value = str(text or "").encode("utf-8", "replace").decode("utf-8")
    value = "".join(
        ch if unicodedata.category(ch) not in ("Cc", "Cf") else " " for ch in value
    )
    value = re.sub(r"(?i)\b(https?://)[^/\s@]+@", r"\1***@", value)
    value = re.sub(
        r"(?i)([?&](?:access_?token|api_?key|auth|code|key|password|passwd|secret|"
        r"session|sig|signature|token)=)[^&\s#]+",
        r"\1***",
        value,
    )
    return " ".join(value.split())[:limit]


def _tile_dir_for(out_root: Path, url: str) -> Path:
    """Choose a per-target directory that cannot silently overwrite another target.

    slugify() is lossy on purpose (it matches pixelshot's naming), so two distinct
    targets can map to the same stem. Reuse the directory only when its existing
    manifest belongs to the same target; otherwise append a short URL hash.
    """
    sanitized = redact_url(url)
    base = slugify(sanitized)
    if sanitized != url:
        # Redaction makes distinct token/userinfo URLs look identical; keep a
        # hash of the real target so two secrets cannot collide in one directory.
        digest = hashlib.sha1(url.encode("utf-8", "replace")).hexdigest()[:8]
        base = f"{base}-{digest}"
    candidate = out_root / f"{base}.png.tiles"
    if not candidate.exists():
        return candidate
    if not candidate.is_dir():
        digest = hashlib.sha1(url.encode("utf-8", "replace")).hexdigest()[:8]
        return out_root / f"{base}-{digest}.png.tiles"
    manifest_path = candidate / MANIFEST_NAME
    if manifest_path.is_file():
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            existing = {}
        if _url_key(existing.get("url", "")) == _url_key(redact_url(url)):
            return candidate
    else:
        # A directory without a manifest is an interrupted capture; reuse it.
        return candidate
    digest = hashlib.sha1(url.encode("utf-8", "replace")).hexdigest()[:8]
    return out_root / f"{base}-{digest}.png.tiles"


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
    return {"url": redact_url(url), "error": _clean_text(error, limit=1000), "complete": False}


_LOCAL_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}
_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa")


def _host_is_local(host: str) -> bool:
    """True for loopback, private, link-local, reserved, and local-only names.

    Literal addresses and well-known local names are checked here. A public hostname
    that resolves to a private address is *not* caught by this function (no DNS
    lookup): the post-navigation check still sees the final URL, so a 302 to an IP
    address is blocked, but DNS rebinding is out of scope.
    """
    name = (host or "").strip().strip("[]").lower().rstrip(".")
    if not name:
        return False
    if name in _LOCAL_NAMES or name.endswith(_LOCAL_SUFFIXES):
        return True
    candidate = name.split("%", 1)[0]
    try:
        ip = ipaddress.ip_address(candidate)
    except ValueError:
        # Browsers still accept legacy IPv4 spellings such as 127.1 or 2130706433;
        # inet_aton parses those without doing a DNS lookup.
        try:
            ip = ipaddress.ip_address(socket.inet_aton(candidate))
        except OSError:
            return False
    return any(
        (
            ip.is_private,
            ip.is_loopback,
            ip.is_link_local,
            ip.is_reserved,
            ip.is_multicast,
            ip.is_unspecified,
        )
    )


def guard_target(url: str, *, allow_private: bool = False, allow_file: bool = False) -> None:
    """Refuse targets that reach the local machine unless the caller opted in.

    This is a trust boundary for an agent-driven tool: the caller is often handed a URL
    it cannot vouch for, and rendering `file://` or an intranet address can expose local
    files, admin panels, or cloud metadata to whatever the page renders.
    """
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    if scheme == "file":
        if not allow_file:
            raise ValueError(
                f"refusing to render {redact_url(url)}: file:// targets are off by default "
                "(pass --allow-file to render a local file)"
            )
        return
    if scheme in ("http", "https") and not allow_private:
        host = parts.hostname or ""
        if _host_is_local(host):
            shown = host if host else url
            raise ValueError(
                f"refusing to render {redact_url(url)}: {shown} is a loopback or private "
                "address (pass --allow-private to capture a local server)"
            )


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

def _quiet_cut(img, width: int, start: int, target: int, reach: int = 64) -> int:
    """Move a band boundary up to `reach` px to a visually quiet row.

    A cut through a line of text is unreadable on both tiles. Searching upward
    for two adjacent near-uniform rows keeps the previous tile at or below the
    target height and usually lands in the gap between lines. If no quiet row is
    found, return the fixed target; the renderer still guarantees coverage.
    """
    if target >= img.height:
        return target
    floor = max(start + 1, target - reach)
    for y in range(target, floor, -1):
        if y <= 0 or y >= img.height:
            continue
        try:
            top = img.crop((0, y - 1, width, y)).convert("L")
            bottom = img.crop((0, y, width, y + 1)).convert("L")
        except Exception:  # noqa: BLE001 - never fail a capture over a heuristic
            return target
        for row in (top, bottom):
            low, high = row.getextrema()
            if high - low > 10:
                break
        else:
            return y
    return target


def tile_image(
    image_path: Path,
    out_dir: Path,
    tile_height: int = DEFAULT_TILE_HEIGHT,
    quality: int = DEFAULT_QUALITY,
    stem: str = "tile",
    allow_wide: bool = False,
) -> list[str]:
    """Slice a tall screenshot into JPEG tiles of at most `tile_height` pixels.

    Every tile is also at most VISION_MAX_EDGE px wide. `allow_wide=True` is only
    for the explicit --viewport-width / --scale exception; a page whose content
    merely overflows is downscaled by the renderer before this function runs.

    Band boundaries are nudged upward to a quiet (near-uniform) row, so a cut is
    less likely to slice through a line of text; when no quiet row exists nearby,
    the boundary stays at the fixed row and adjacent tiles must still be read
    together. A tail shorter than 24 px is rebalanced with the previous band,
    which keeps every tile within the tile height instead of emitting an
    unreadable strip or an oversized merge.
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
        if width > VISION_MAX_EDGE and not allow_wide:
            raise ValueError(
                f"image is {width}px wide, above the {VISION_MAX_EDGE}px vision bound; "
                "downscale the screenshot or pass allow_wide=True for an explicit "
                "--viewport-width/--scale layout"
            )
        bands: list[tuple[int, int]] = []
        y = 0
        while y < height:
            target = min(y + tile_height, height)
            bottom = _quiet_cut(img, width, y, target)
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

def _tile_geometry(tile_dir: Path, tiles: list[str]) -> tuple[list[int], list[int]]:
    """Return each tile's y offset and height in page pixels.

    Tiles are cut by a quiet-row heuristic and a tail rebalance, so heights are not
    guaranteed to be uniform; readers that need to map a crop coordinate back to the
    page must use these offsets instead of assuming a fixed tile_height stride.
    """
    offsets: list[int] = []
    heights: list[int] = []
    top = 0
    for name in tiles:
        try:
            height = _image_height(tile_dir / name)
        except Exception:  # noqa: BLE001 - a missing tile is reported by _verify_tiles
            return [], []
        offsets.append(top)
        heights.append(height)
        top += height
    return offsets, heights


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
    width_truncated: bool = False,
    wrap_suspected: bool = False,
    lazy_truncated: bool = False,
    tile_offsets: list[int] | None = None,
    tile_heights: list[int] | None = None,
    viewport_reported: bool = True,
) -> dict:
    """Assemble the manifest and derive `complete` from what was captured."""
    warnings = [_clean_text(w, limit=500) for w in warnings]
    if scope == "page" and page_height > 0 and captured_height < page_height - 1:
        warnings.append(
            f"captured the top {captured_height}px of a {page_height}px page; the rest "
            "was not included (raise --max-height to capture it)"
        )
    if width_truncated:
        warnings.append(
            "horizontal overflow was truncated at the capture width; the page is wider "
            "than what page.png contains"
        )
    if wrap_suspected:
        warnings.append(
            "page.png's bottom band repeats its top band; this looks like a renderer "
            "wrap, so the capture is treated as incomplete"
        )
    if lazy_truncated:
        warnings.append(
            f"page is {page_height}px tall, beyond the {SCROLL_LIMIT}px lazy-load pre-scroll "
            "limit; content below that point may not have loaded, so the capture is incomplete"
        )
    complete = (
        scope == "page"
        and page_height > 0
        and captured_height > 0
        and captured_height >= page_height - 1
        and not width_truncated
        and not wrap_suspected
        and not lazy_truncated
    )
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "url": redact_url(url),
        "final_url": redact_url(final_url),
        "title": _clean_text(title),
        "scope": scope,
        "page_height": page_height,
        "captured_height": captured_height,
        "viewport": viewport,
        "viewport_reported": viewport_reported,
        "tile_height": tile_height,
        "tile_offsets": tile_offsets or [],
        "tile_heights": tile_heights or [],
        "scale": scale,
        "selector": _clean_text(selector, limit=200) if selector else None,
        "tiles": tiles,
        "complete": complete,
        "width_truncated": width_truncated,
        "wrap_suspected": wrap_suspected,
        "lazy_truncated": lazy_truncated,
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

def _launch_attempts(
    browser_hint: tuple[str | None, str | None], headless: bool
) -> list[dict]:
    """Browser launch arguments to try, most specific first."""
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
    return attempts


def _launch(playwright, browser_hint: tuple[str | None, str | None], headless: bool = True):
    """Launch a Chromium-family browser, preferring channels over raw paths."""
    errors: list[str] = []
    for kwargs in _launch_attempts(browser_hint, headless):
        try:
            return playwright.chromium.launch(**kwargs)
        except Exception as exc:  # noqa: BLE001 - we want the last real reason
            errors.append(f"{kwargs}: {exc}".replace("\n", " ")[:300])
    raise RuntimeError(
        "could not launch any browser. Tried:\n  - " + "\n  - ".join(errors)
        + "\nInstall Chrome/Edge, run `playwright install chromium`, or set CHROME_PATH."
    )


def _launch_profile(playwright, browser_hint, user_data_dir: Path, headless: bool, context_kwargs: dict):
    """Open a persistent profile, so a session survives between captures.

    This is how a logged-in page stays readable: log in once with `--show`, and every
    later capture reuses the cookies and localStorage in that directory.
    """
    errors: list[str] = []
    for kwargs in _launch_attempts(browser_hint, headless):
        try:
            return playwright.chromium.launch_persistent_context(
                str(user_data_dir), **kwargs, **context_kwargs
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{kwargs}: {exc}".replace("\n", " ")[:300])
    raise RuntimeError(
        f"could not open the profile at {user_data_dir}. Tried:\n  - "
        + "\n  - ".join(errors)
        + "\nIf a browser is already using that directory, close it first, or point "
        "--profile at a dedicated directory (recommended)."
    )


def _wait_for_login(page, seconds: int, warnings: list[str]) -> bool:
    """Block until the password field disappears, i.e. a human finished signing in."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            state = page.evaluate(
                """() => {
                    const ready = document.readyState === 'complete';
                    const asking = !!document.querySelector('input[type=password]');
                    return {ready, asking};
                }"""
            )
            still_asking = (not state["ready"]) or bool(state["asking"])
        except Exception:  # noqa: BLE001 - navigation in flight
            still_asking = True
        if not still_asking:
            warnings.append("signed-in page detected; captured after the login form went away")
            return True
        page.wait_for_timeout(2000)
    warnings.append(
        f"no login detected within {seconds}s; captured whatever the page showed "
        "(raise --wait-login if you needed longer)"
    )
    return False


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
                """async (cap) => {
                    const step = Math.max(300, Math.floor(window.innerHeight * 0.9));
                    for (let y = 0; y < Math.min(document.body.scrollHeight, cap); y += step) {
                        window.scrollTo(0, y);
                        await new Promise(r => setTimeout(r, 120));
                    }
                    window.scrollTo(0, 0);
                    await new Promise(r => setTimeout(r, 150));
                }""",
                SCROLL_LIMIT,
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


def _image_size(path: Path) -> tuple[int, int]:
    Image = load_pillow()
    with Image.open(path) as img:
        return img.width, img.height


def _image_height(path: Path) -> int:
    return _image_size(path)[1]


def _capture_region(
    page,
    path: Path,
    *,
    x: int,
    y: int,
    width: int,
    height: int,
    scale: int,
    timeout_ms: int,
) -> None:
    """Screenshot a page region, splitting heights Chromium cannot rasterize.

    Chromium/Edge silently wrap full-page screenshots taller than 16384 device
    pixels: the bottom becomes a copy of the top, with no error. A single
    screenshot is fine below that bound; above it we take consecutive clips and
    stitch them, so every row is real page content.
    """
    height = max(1, int(height))
    width = max(1, int(width))
    raster_pixels = width * scale * height * scale
    if raster_pixels > MAX_RASTER_PIXELS:
        raise RuntimeError(
            f"capture would be {width}x{height} CSS px at scale {scale} "
            f"({raster_pixels:,} raster pixels); lower --max-height, --scale, "
            "or capture a narrower selector"
        )
    if height * scale <= MAX_RASTER_HEIGHT:
        page.screenshot(
            path=str(path),
            full_page=True,  # without this, clip is limited to the viewport
            clip={"x": x, "y": y, "width": width, "height": height},
            timeout=timeout_ms,
        )
        return

    Image = load_pillow()
    segment = max(1, MAX_RASTER_HEIGHT // max(1, scale))
    parts: list[Path] = []
    try:
        for index, offset in enumerate(range(0, height, segment)):
            part = path.with_name(f"{path.stem}.seg{index:04d}.png")
            part_height = min(segment, height - offset)
            page.screenshot(
                path=str(part),
                full_page=True,
                clip={
                    "x": x,
                    "y": y + offset,
                    "width": width,
                    "height": part_height,
                },
                timeout=timeout_ms,
            )
            parts.append(part)

        sizes = [_image_size(part) for part in parts]
        widths = {size[0] for size in sizes}
        if len(widths) != 1:
            raise RuntimeError(f"segmented screenshot widths differ: {sorted(widths)}")
        total_height = sum(size[1] for size in sizes)
        with Image.open(parts[0]) as first:
            canvas = Image.new("RGB", (first.width, total_height))
        cursor = 0
        for part in parts:
            with Image.open(part) as img_part:
                img_part = img_part.convert("RGB")
                canvas.paste(img_part, (0, cursor))
                cursor += img_part.height
        canvas.save(path, "PNG")
    finally:
        for part in parts:
            part.unlink(missing_ok=True)


def _wrapped_bottom(path: Path, band: int = 512) -> bool:
    """Heuristic guard for a renderer that wrapped the bottom to the top.

    A genuine page can repeat a band, but an exact pixel-identical copy of the
    first `band` rows at the bottom means the raster is suspect. This should
    never fire after segmented capture; it is a last-resort honesty check.
    """
    Image = load_pillow()
    from PIL import ImageChops

    with Image.open(path) as img:
        img = img.convert("RGB")
        if img.height < band * 4:
            return False
        top = img.crop((0, 0, img.width, band))
        bottom = img.crop((0, img.height - band, img.width, img.height))
        # An all-white/blank band is identical at both ends on many legitimate
        # short pages; only a *featured* top band wrapping to the bottom is
        # evidence of the Chromium 16384px bug.
        low, high = top.convert("L").getextrema()
        if high - low <= 10:
            return False
        return ImageChops.difference(top, bottom).getbbox() is None


def _downscale_width(path: Path, target_width: int) -> tuple[int, int]:
    """Resize page.png to `target_width`, preserving aspect ratio, in place."""
    Image = load_pillow()
    with Image.open(path) as img:
        img = img.convert("RGB")
        ratio = target_width / img.width
        new_size = (target_width, max(1, int(round(img.height * ratio))))
        resized = img.resize(new_size, Image.LANCZOS)
        tmp = path.with_name(path.stem + ".width.png")
        resized.save(tmp, "PNG")
    os.replace(tmp, path)
    return new_size


def _verify_tiles(
    tile_dir: Path,
    tiles: list[str],
    warnings: list[str],
    *,
    strict: bool = False,
) -> None:
    """Ensure a manifest's tile list names real, local files.

    With `strict=True` a broken list aborts the capture; otherwise it is reported as
    a warning (pixelshot owns its output and may be repaired by the caller).
    """
    if not tiles:
        message = "manifest lists no tiles"
        if strict:
            raise RuntimeError(message)
        warnings.append(message)
        return
    unsafe = []
    for name in tiles:
        if not isinstance(name, str) or not name or Path(name).name != name:
            unsafe.append(str(name))
    if unsafe:
        message = f"manifest contains unsafe tile names (first: {unsafe[0]!r})"
        if strict:
            raise RuntimeError(message)
        warnings.append(message)
        return
    missing = [name for name in tiles if not (tile_dir / name).is_file()]
    if missing:
        message = (
            f"{len(missing)} of {len(tiles)} tiles are missing on disk "
            f"(first: {missing[0]}); re-run the capture"
        )
        if strict:
            raise RuntimeError(message)
        warnings.append(message)


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
    viewport_height: int = DEFAULT_VIEWPORT_HEIGHT,
    quality: int = DEFAULT_QUALITY,
    wait_network_idle: bool = True,
    extra_wait_ms: int = 0,
    max_height: int = DEFAULT_MAX_HEIGHT,
    full_page: bool = True,
    scroll: bool = True,
    scale: int = 1,
    selector: str | None = None,
    hide_selectors: list[str] | None = None,
    hide_fixed: bool = False,
    timeout_ms: int = 60000,
    selector_timeout_ms: int = DEFAULT_SELECTOR_TIMEOUT_MS,
    storage_state: str | None = None,
    profile: Path | None = None,
    headful: bool = False,
    wait_login: int = 0,
    allow_private: bool = False,
    allow_file: bool = False,
) -> dict:
    guard_target(url, allow_private=allow_private, allow_file=allow_file)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit(
            "playwright is not installed.\n"
            "  python -m pip install playwright\n"
            "or run scripts/bootstrap.ps1 / scripts/bootstrap.sh from the repo root."
        )

    tile_dir = _tile_dir_for(out_root, url)
    tile_dir.mkdir(parents=True, exist_ok=True)
    # Remove the previous result first: a render failure must leave no manifest,
    # so "no tiles.json = failed capture" stays true across re-captures. page.png is
    # part of that result: leaving it behind lets crop.py read an older page.
    for stale in tile_dir.glob("tile_*.jpg"):
        stale.unlink()
    for stale in tile_dir.glob("*.crop.png"):
        stale.unlink()
    for stale in tile_dir.glob("*.width.png"):
        stale.unlink()
    for stale in tile_dir.glob("*.seg*.png"):
        stale.unlink()
    page_path = tile_dir / "page.png"
    page_path.unlink(missing_ok=True)
    manifest_path = tile_dir / MANIFEST_NAME
    manifest_path.unlink(missing_ok=True)
    (tile_dir / (MANIFEST_NAME + ".tmp")).unlink(missing_ok=True)
    (tile_dir / "pixelshot.tiles.json").unlink(missing_ok=True)

    hint = find_browser()
    warnings: list[str] = []
    started = time.time()
    explicit_wide = viewport_width * scale > VISION_MAX_EDGE
    if explicit_wide:
        warnings.append(
            f"tiles are {viewport_width * scale}px wide after --scale {scale}; "
            f"vision models downscale above ~{VISION_MAX_EDGE}px, so use a narrower --viewport-width"
        )

    title = ""
    final_url = url
    page_height = 0
    captured_height = 0
    page_width = viewport_width
    width_truncated = False
    wrap_suspected = False
    lazy_truncated = False
    scope = "page"

    with sync_playwright() as p:
        browser = None
        try:
            context_kwargs = {
                "viewport": {"width": viewport_width, "height": viewport_height},
                "device_scale_factor": scale,
                "reduced_motion": "reduce",
                "ignore_https_errors": False,
            }
            if storage_state:
                context_kwargs["storage_state"] = storage_state
            if profile:
                # Persistent context: the profile directory *is* the session, so cookies
                # set by a human logging in once are still there on the next run.
                context = _launch_profile(p, hint, profile, not headful, context_kwargs)
                page = context.pages[0] if context.pages else context.new_page()
                if headful:
                    warnings.append(
                        "opened a visible browser from --profile; log in there, then let "
                        "this capture finish (or rerun without --show)"
                    )
            else:
                browser = _launch(p, hint)
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
                # A 302 to 127.0.0.1 or to an intranet host would slip past the pre-flight
                # check; re-check what the browser actually landed on.
                landed = page.url or url
                if landed != url:
                    try:
                        guard_target(
                            landed, allow_private=allow_private, allow_file=allow_file
                        )
                    except ValueError as exc:
                        raise RuntimeError(
                            f"navigation redirected to a blocked target: {exc}"
                        )
            except ValueError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    f"navigation failed for {redact_url(url)}: {exc}".replace("\n", " ")[:400]
                )

            warnings.extend(_settle(page, wait_network_idle, extra_wait_ms, scroll))

            if headful:
                # Give the human a chance to sign in. Interactive terminals wait for Enter;
                # a piped run (agent) either polls for the login form or waits a fixed minute.
                try:
                    if wait_login > 0:
                        # Explicit polling beats the prompt: it also works when this runs
                        # under an agent whose stdin is already at EOF.
                        _wait_for_login(page, wait_login, warnings)
                        warnings.extend(_settle(page, wait_network_idle, 0, scroll))
                    elif sys.stdin is not None and sys.stdin.isatty():
                        input("Log in in the opened window, then press Enter to capture... ")
                    else:
                        page.wait_for_timeout(60000)
                except (EOFError, KeyboardInterrupt):
                    pass

            try:
                title = page.title()
                final_url = page.url
            except Exception:  # noqa: BLE001
                pass

            hidden = 0
            for sel in hide_selectors or []:
                try:
                    count = page.locator(sel).count()
                    page.locator(sel).evaluate_all(
                        "els => els.forEach(e => e.style.setProperty('display', 'none', 'important'))"
                    )
                    hidden += count
                except Exception as exc:  # noqa: BLE001
                    warnings.append(f"--hide {sel!r} failed: {_clean_text(exc, limit=200)}")
            if hide_fixed:
                try:
                    hidden_fixed = int(
                        page.evaluate(
                            """() => {
                                let n = 0;
                                for (const e of document.querySelectorAll('body *')) {
                                    const s = getComputedStyle(e);
                                    if (s.position === 'fixed' || s.position === 'sticky') {
                                        e.style.setProperty('visibility', 'hidden', 'important');
                                        n++;
                                    }
                                }
                                return n;
                            }"""
                        )
                    )
                    hidden += hidden_fixed
                except Exception as exc:  # noqa: BLE001
                    warnings.append(f"--hide-fixed failed: {_clean_text(exc, limit=200)}")
            if hidden:
                warnings.append(
                    f"hid {hidden} page element(s) before capture (--hide/--hide-fixed); "
                    "the screenshot no longer shows what a person would see"
                )

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

            def measure_width() -> int:
                return int(
                    page.evaluate(
                        """() => {
                            const d = document.documentElement, b = document.body || {};
                            return Math.max(
                                d ? d.scrollWidth : 0,
                                b.scrollWidth || 0,
                                window.innerWidth || 0
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
            page_width = max(viewport_width, measure_width())

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
            if selector:
                try:
                    locator = page.locator(selector).first
                    locator.wait_for(state="visible", timeout=selector_timeout_ms)
                    box = locator.bounding_box()
                    if box and box["width"] > MAX_CAPTURE_WIDTH:
                        raise RuntimeError(
                            f"element is {int(box['width'])}px wide, above the "
                            f"{MAX_CAPTURE_WIDTH}px safety limit; capture the page and crop it instead"
                        )
                    if box and box["height"] * scale > MAX_RASTER_HEIGHT:
                        _capture_region(
                            page,
                            page_path,
                            x=int(box["x"]),
                            y=int(box["y"]),
                            width=int(box["width"]),
                            height=int(box["height"]),
                            scale=scale,
                            timeout_ms=timeout_ms,
                        )
                    else:
                        locator.screenshot(path=str(page_path), timeout=selector_timeout_ms)
                except RuntimeError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    raise RuntimeError(
                        f"selector {selector!r} could not be captured: {exc}".replace("\n", " ")[:300]
                    )
            elif full_page:
                if page_height > SCROLL_LIMIT:
                    lazy_truncated = True
                capture_width = min(page_width, MAX_CAPTURE_WIDTH)
                if page_width > MAX_CAPTURE_WIDTH:
                    width_truncated = True
                    warnings.append(
                        f"page is {page_width}px wide; only the left {MAX_CAPTURE_WIDTH}px "
                        "were captured (horizontal overflow)"
                    )
                clip_height = page_height
                if max_height and page_height > max_height:
                    # Clip the screenshot itself instead of rasterizing the whole page
                    # and cropping afterwards: the cap must bound capture work too.
                    clip_height = max_height
                clip_height = max(1, clip_height)
                _capture_region(
                    page,
                    page_path,
                    x=0,
                    y=0,
                    width=max(1, capture_width),
                    height=clip_height,
                    scale=scale,
                    timeout_ms=timeout_ms,
                )
            else:
                page.screenshot(path=str(page_path), full_page=False, timeout=timeout_ms)
            context.close()
        finally:
            if browser is not None:
                browser.close()

    image_width, image_height = _image_size(page_path)
    if scope == "page" and _wrapped_bottom(page_path):
        wrap_suspected = True
        warnings.append(
            "the rendered page repeats its top band at the bottom; the screenshot "
            "backend may have wrapped content instead of failing"
        )

    # Usually the clip already capped the height; keep a guard for engines that
    # return a taller raster than requested.
    if scope == "page" and max_height and page_height > max_height:
        keep_px = max_height * scale
        if image_height > keep_px:
            _crop_top(page_path, keep_px)
            image_height = keep_px

    # Height is measured before any width downscale, so `captured_height` keeps
    # describing CSS pixels of the page rather than pixels of the resized raster.
    raw_captured_height = image_height

    # The width bound is about the *rendered* image, not only the viewport: a page
    # with a 3000px-wide element under a 1280px viewport would otherwise produce
    # 3000px tiles that vision models silently downscale. An explicit wide viewport
    # raises the limit to the width the caller asked for, but page content wider than
    # that is still reduced so the manifest's viewport is not a lie.
    width_limit = max(VISION_MAX_EDGE, viewport_width * scale) if explicit_wide else VISION_MAX_EDGE
    if image_width > width_limit:
        new_size = _downscale_width(page_path, width_limit)
        warnings.append(
            f"rendered page was {image_width}px wide; downscaled to {width_limit}px "
            f"({new_size[0]}x{new_size[1]}) so tiles stay legible; text is smaller and "
            "crop coordinates refer to the downscaled page.png"
        )
        image_width, image_height = new_size

    captured_height = int(round(raw_captured_height / max(1, scale)))
    if scope == "page" and page_height:
        captured_height = min(captured_height, page_height)

    tiles = tile_image(
        page_path, tile_dir, tile_height, quality, allow_wide=explicit_wide
    )
    if not tiles:
        raise RuntimeError("tiling produced no images")
    _verify_tiles(tile_dir, tiles, warnings, strict=True)

    if len(tiles) > 20:
        warnings.append(f"{len(tiles)} tiles captured; read them in batches and crop when text is small")

    tile_offsets, tile_heights = _tile_geometry(tile_dir, tiles)
    if not tile_offsets:
        warnings.append("could not measure tile heights; tile_offsets is empty")

    manifest = build_manifest(
        url=url,
        final_url=final_url,
        title=title,
        page_height=page_height,
        captured_height=captured_height,
        tile_height=tile_height,
        viewport={"width": viewport_width, "height": viewport_height},
        viewport_reported=True,
        scale=scale,
        selector=selector,
        tiles=tiles,
        tile_offsets=tile_offsets,
        tile_heights=tile_heights,
        scope=scope,
        backend=f"playwright:{hint[0] or 'bundled'}",
        warnings=warnings,
        started=started,
        width_truncated=width_truncated,
        wrap_suspected=wrap_suspected,
        lazy_truncated=lazy_truncated,
    )
    write_manifest(tile_dir, manifest)
    return manifest


# --------------------------------------------------------------------------- #
# pixelshot backend (PixelRAG)
# --------------------------------------------------------------------------- #

def _url_key(url: str) -> str:
    """Compare URLs across percent-encoding and separator differences."""
    from urllib.parse import unquote, urlparse

    text = (url or "").strip()
    parsed = urlparse(text)
    if parsed.scheme == "file":
        path = str(Path(unquote(parsed.path)).resolve()).replace("\\", "/")
        return "file:" + path.lower()
    if not parsed.scheme and text:
        # pixelshot sometimes records the path it was given, not a file:// URI.
        candidate = Path(text).expanduser()
        if candidate.is_absolute() or candidate.is_file():
            try:
                path = str(candidate.resolve()).replace("\\", "/")
                return "file:" + path.lower()
            except OSError:
                pass
    if parsed.scheme in ("http", "https"):
        # Percent-encoding differences are common between the CLI argument and the
        # URL the renderer records; compare the decoded path instead.
        return urlunsplit(
            (parsed.scheme, parsed.netloc, unquote(parsed.path), parsed.query, parsed.fragment)
        ).rstrip("/").lower()
    return text.rstrip("/").lower()


def _pixelshot_outputs(out_root: Path, started: float) -> list[tuple[Path, dict]]:
    """Manifests pixelshot wrote during this run, newest first.

    Only manifests touched by this run count. Falling back to older files is how a
    failed pixelshot run used to be reported as a successful capture of the wrong page.
    """
    fresh: list[tuple[Path, dict]] = []
    for mpath in out_root.glob("*.png.tiles/" + MANIFEST_NAME):
        try:
            stamp = mpath.stat().st_mtime
            if stamp < started - 2:
                continue
            item = (mpath, json.loads(mpath.read_text(encoding="utf-8-sig")))
        except (OSError, ValueError):
            continue
        fresh.append(item)
    fresh.sort(key=lambda item: item[0].stat().st_mtime, reverse=True)
    return fresh


def _match_pixelshot_output(url: str, produced: list[tuple[Path, dict]]):
    """Pick the fresh manifest whose URL matches this target; never guess."""
    keys = {_url_key(url), _url_key(redact_url(url))}
    try:
        keys.add(_url_key(normalize_target(url)))
        keys.add(_url_key(redact_url(normalize_target(url))))
    except (FileNotFoundError, OSError):
        pass
    for item in produced:
        if _url_key(str(item[1].get("url", ""))) in keys:
            return item
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
    max_height: int | None = None,
    pixelshot_bin: str | None = None,
    ignored_flags: list[str] | None = None,
    allow_private: bool = False,
    allow_file: bool = False,
) -> dict:
    # pixelshot is a separate process we cannot inspect, so the only place to refuse a
    # local target is here, before handing it over. Normalize first: a bare
    # "localhost:5173" or a local file path has no URL scheme for guard_target to see.
    for url in urls:
        try:
            guarded = normalize_target(url)
        except FileNotFoundError:
            guarded = url
        guard_target(guarded, allow_private=allow_private, allow_file=allow_file)
    exe = find_pixelshot(pixelshot_bin)
    if not exe:
        raise SystemExit(
            "pixelshot not found. Install PixelRAG's renderer:\n"
            "  uv tool install pixelrag      # or: pipx install pixelrag\n"
            "or pass --pixelshot /path/to/pixelshot, or use --backend playwright."
        )
    for url in urls:
        if str(url).startswith("-"):
            raise ValueError(
                f"refusing a target that starts with '-': {url!r} (it could be read as a pixelshot option)"
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
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=900,
    )
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
                _error_result(
                    url,
                    f"pixelshot reported success but wrote no fresh manifest for {url} "
                    f"under {out_root}; refusing to reuse an older capture",
                )
            )
            continue
        unclaimed.remove(match)
        mpath, raw = match
        try:
            tile_dir = mpath.parent
            page_height = int(raw.get("page_height") or 0)
            tiles = list(raw.get("tiles") or [])
            upstream_complete = bool(raw.get("complete"))
            warnings = [_clean_text(w, limit=500) for w in (raw.get("warnings") or [])]
            for flag in ignored_flags or []:
                warnings.append(f"{flag} is not forwarded to the pixelshot backend")
            # The ignored_flags loop above already warns when the caller actually
            # passes a non-default --max-height; do not warn about a default the
            # caller never asked for.
            _verify_tiles(tile_dir, tiles, warnings, strict=True)
            if page_height <= 0:
                raise ValueError("pixelshot manifest has no usable page_height")
            try:
                first_width = _image_size(tile_dir / tiles[0])[0]
                width_limit = max(VISION_MAX_EDGE, viewport_width)
                if first_width > width_limit:
                    raise ValueError(
                        f"pixelshot produced {first_width}px-wide tiles, above the "
                        f"{width_limit}px bound; use --backend playwright (which downscales "
                        "page overflow) or a narrower page layout"
                    )
                if first_width > VISION_MAX_EDGE:
                    warnings.append(
                        f"pixelshot tiles are {first_width}px wide after an explicit "
                        f"--viewport-width {viewport_width}; vision models downscale them"
                    )
            except ValueError:
                raise
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"could not measure pixelshot tiles: {exc}")

            if max_height and page_height > max_height:
                warnings.append(
                    f"pixelshot captured the whole {page_height}px page; --max-height "
                    f"{max_height} is not applied by this backend"
                )
            # Check the tiles cover the page instead of trusting pixelshot's claim.
            tile_coverage = 0
            try:
                tile_coverage = sum(_image_height(tile_dir / name) for name in tiles)
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"could not measure pixelshot tiles: {exc}")
            if upstream_complete and tile_coverage < page_height - 1:
                warnings.append(
                    f"pixelshot claimed complete, but its {len(tiles)} tiles cover only "
                    f"{tile_coverage}px of {page_height}px"
                )
                upstream_complete = False
            # Never infer coverage from a failed measurement: unmeasurable tiles
            # mean we cannot certify that the capture is complete.
            captured_height = min(tile_coverage, page_height) if page_height else 0

            warnings.append(
                "rendered by pixelshot; its own manifest is kept as pixelshot.tiles.json "
                "and this manifest only reports what its tiles cover"
            )
            warnings = [_clean_text(w, limit=500) for w in warnings]
            # Our manifest is the contract every reader follows, but pixelshot's file is
            # not ours to overwrite: copy it aside first, byte for byte.
            original = tile_dir / "pixelshot.tiles.json"
            try:
                shutil.copyfile(mpath, original)
            except OSError as exc:  # noqa: BLE001
                warnings.append(f"could not keep a copy of pixelshot's manifest: {exc}")
            tile_offsets, tile_heights = _tile_geometry(tile_dir, tiles)
            manifest = {
                "schema_version": MANIFEST_SCHEMA_VERSION,
                "url": redact_url(str(raw.get("url", url))),
                "final_url": redact_url(str(raw.get("url", url))),
                "title": _clean_text(raw.get("title", "")),
                "scope": "page",
                "page_height": page_height,
                "captured_height": captured_height,
                # pixelshot does not record the viewport it used, and filling in our own
                # numbers would make the field look measured when it is guessed.
                "viewport": {"width": viewport_width, "height": None},
                "viewport_reported": False,
                "tile_height": tile_height,
                "tile_offsets": tile_offsets,
                "tile_heights": tile_heights,
                "scale": 1,
                "selector": None,
                "tiles": tiles,
                "complete": bool(
                    page_height
                    and upstream_complete
                    and captured_height > 0
                    and captured_height >= page_height - 1
                ),
                "width_truncated": False,
                "wrap_suspected": False,
                "lazy_truncated": False,
                "backend": "pixelshot",
                "captured_at": now_iso(),
                "elapsed_s": round(time.time() - started, 2),
                "warnings": warnings,
            }
            written = write_manifest(tile_dir, manifest)
            manifest["manifest_path"] = str(written)
            manifests.append(manifest)
        except Exception as exc:  # noqa: BLE001
            manifests.append(_error_result(url, f"pixelshot manifest rejected: {exc}"))

    return manifests[0] if len(manifests) == 1 else {"results": manifests}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _cli_error(args, message: str, code: int) -> int:
    """Report an argv-level failure in the selected output format."""
    if args.json:
        print(json.dumps(_error_result(args.urls[0], message), ensure_ascii=False, indent=2))
    else:
        eprint(f"[error] {message}")
    return code


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
    p.add_argument("--version", action="version", version=f"PixelVision {__version__}")
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
    p.add_argument("--viewport-height", type=_bounded_int("--viewport-height", 320, 2160),
                   default=DEFAULT_VIEWPORT_HEIGHT,
                   help="CSS px viewport height for 100vh/fixed-element layout; "
                        "independent of --tile-height")
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
    p.add_argument("--hide", action="append", metavar="SELECTOR",
                   help="hide elements matching this CSS selector before capture (repeatable)")
    p.add_argument("--hide-fixed", action="store_true",
                   help="hide position:fixed/sticky elements (cookie banners, floating headers)")
    p.add_argument("--selector-timeout", type=_bounded_int("--selector-timeout", 100, 600000),
                   default=DEFAULT_SELECTOR_TIMEOUT_MS,
                   help="ms to wait for --selector to become visible (avoid a 60s stall)")
    p.add_argument("--storage-state",
                   help="Playwright storage_state JSON (cookies + localStorage) for logged-in pages")
    p.add_argument("--profile",
                   help="persistent browser profile directory; log in once with --show, "
                        "then reuse the session headless")
    p.add_argument("--show", action="store_true",
                   help="open a visible browser (requires --profile); use it to log in")
    p.add_argument("--wait-login", type=_bounded_int("--wait-login", 0, 1800), default=0,
                   help="with --show: poll this many seconds for a human to finish logging in")
    p.add_argument("--allow-private", action="store_true",
                   help="allow loopback/private addresses (localhost, 127.0.0.1, 10.x, "
                        "192.168.x, .local); refused by default")
    p.add_argument("--allow-file", action="store_true",
                   help="allow file:// targets (local HTML/images); refused by default")
    p.add_argument("--wait-ms", type=_bounded_int("--wait-ms", 0, 120000),
                   default=0, help="extra settle time before capture")
    p.add_argument("--no-network-idle", action="store_true", help="skip the network-quiet wait")
    p.add_argument("--no-scroll", action="store_true", help="skip the lazy-load scroll pass")
    p.add_argument("--no-full-page", action="store_true", help="capture only the first viewport")
    p.add_argument("--json", action="store_true", help="print the manifest as JSON and nothing else")
    p.add_argument("--allow-partial", action="store_true",
                   help="exit 0 when at least one target succeeded (default: any failure exits 1)")
    return p


def _playwright_can_run() -> bool:
    """True only when playwright can really launch a browser, not just find one."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as p:
            browser = _launch(p, find_browser())
            browser.close()
        return True
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
            return _cli_error(args, f"--storage-state file not found: {state_path}", 2)
        storage_state = str(state_path)

    if args.show and not args.profile:
        return _cli_error(args, "--show needs --profile DIR: the login must be saved in a profile", 2)
    profile = None
    if args.profile:
        profile = Path(args.profile).expanduser().resolve()
        try:
            profile.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return _cli_error(args, f"cannot create --profile directory {profile}: {exc}", 2)

    backend = args.backend
    if backend == "auto":
        # A persistent profile is a Playwright-only feature, so honour it over pixelshot.
        if args.profile or _playwright_can_run():
            backend = "playwright"
        elif find_pixelshot(args.pixelshot):
            backend = "pixelshot"
        else:
            backend = "playwright"
    ignored_flags: list[str] = []
    if backend == "pixelshot":
        if storage_state:
            eprint("[warn] --storage-state is ignored by the pixelshot backend")
            ignored_flags.append("--storage-state")
        if profile:
            eprint("[warn] --profile is ignored by the pixelshot backend; use --backend playwright")
            ignored_flags.append("--profile")
        if args.selector:
            ignored_flags.append("--selector")
        if args.hide:
            ignored_flags.append("--hide")
        if args.hide_fixed:
            ignored_flags.append("--hide-fixed")
        if args.no_full_page:
            ignored_flags.append("--no-full-page")
        if args.viewport_height != DEFAULT_VIEWPORT_HEIGHT:
            ignored_flags.append("--viewport-height")
        if args.scale != 1:
            ignored_flags.append("--scale")
        if args.wait_ms:
            ignored_flags.append("--wait-ms")
        if args.no_scroll:
            ignored_flags.append("--no-scroll")
        if args.no_network_idle:
            ignored_flags.append("--no-network-idle")
        if args.selector_timeout != DEFAULT_SELECTOR_TIMEOUT_MS:
            ignored_flags.append("--selector-timeout")
        if args.wait_login:
            ignored_flags.append("--wait-login")

    try:
        if backend == "pixelshot":
            try:
                result = render_with_pixelshot(
                    args.urls, out_root,
                    tile_height=args.tile_height,
                    viewport_width=args.viewport_width,
                    quality=args.quality,
                    wait_network_idle=not args.no_network_idle,
                    workers=args.workers,
                    max_height=args.max_height,
                    pixelshot_bin=args.pixelshot,
                    ignored_flags=ignored_flags,
                    allow_private=args.allow_private,
                    allow_file=args.allow_file,
                )
            except Exception as exc:  # noqa: BLE001
                # A renderer crash should still produce one error result per target, so
                # --json stays parseable and the caller learns which URL failed.
                results = [_error_result(raw, str(exc)) for raw in args.urls]
            else:
                results = result.get("results", [result])
        else:
            results = []
            for raw in args.urls:
                try:
                    target = normalize_target(raw)
                    # Refuse local targets before any browser work (and before the
                    # "rendering a local file" note, which would be a lie otherwise).
                    guard_target(
                        target,
                        allow_private=args.allow_private,
                        allow_file=args.allow_file,
                    )
                except (FileNotFoundError, ValueError) as exc:
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
                            viewport_height=args.viewport_height,
                            quality=args.quality,
                            wait_network_idle=not args.no_network_idle,
                            extra_wait_ms=args.wait_ms,
                            max_height=args.max_height,
                            full_page=not args.no_full_page,
                            scroll=not args.no_scroll,
                            scale=args.scale,
                            selector=args.selector,
                            hide_selectors=args.hide,
                            hide_fixed=args.hide_fixed,
                            selector_timeout_ms=args.selector_timeout,
                            storage_state=storage_state,
                            profile=profile,
                            headful=args.show,
                            wait_login=args.wait_login,
                            allow_private=args.allow_private,
                            allow_file=args.allow_file,
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    # One bad URL must not cost the caller every other page in the list;
                    # the collected error is reported once, with the other results.
                    results.append(_error_result(raw, str(exc)))
    except SystemExit as exc:
        # Missing dependencies raise SystemExit("how to fix it"); the documented
        # exit code is 2, not the 1 an uncaught SystemExit would produce.
        if isinstance(exc.code, str):
            if args.json:
                print(json.dumps(_error_result(args.urls[0], exc.code),
                                 ensure_ascii=False, indent=2))
            else:
                eprint(f"[error] {exc.code}")
            return 2
        raise
    except Exception as exc:  # noqa: BLE001
        message = _clean_text(exc, limit=2000)
        if args.json:
            print(json.dumps(_error_result(args.urls[0], message),
                             ensure_ascii=False, indent=2))
        else:
            eprint(f"[error] {message}")
        return 1

    failures = [r for r in results if r.get("error")]
    all_failed = bool(failures) and len(failures) == len(results)

    if args.json:
        # A scripting caller needs the error text in the payload, not only on stderr.
        print(json.dumps(results[0] if len(results) == 1 else {"results": results}, ensure_ascii=False, indent=2))
        if not failures:
            return 0
        return 0 if args.allow_partial and not all_failed else 1

    if all_failed:
        for r in failures:
            eprint(f"[error] {_clean_text(r['url'])}: {r['error']}")
        return 1

    for r in results:
        if r.get("error"):
            eprint(f"[error] {_clean_text(r['url'])}: {r['error']}")
            continue
        print(_clean_text(r["url"]))
        print(f"  title      {_clean_text(r.get('title', ''))}")
        print(f"  manifest   {r['manifest_path']}")
        tile_width = r['viewport']['width']
        if r.get("tiles") and r.get("manifest_path"):
            try:
                tile_width = _image_size(Path(r["manifest_path"]).parent / r["tiles"][0])[0]
            except Exception:  # noqa: BLE001 - the summary is not the contract
                pass
        print(f"  tiles      {len(r['tiles'])} x {r['tile_height']}px  ({tile_width}px wide)")
        print(
            f"  page       {r.get('captured_height', '?')}/{r.get('page_height', '?')}px"
            f"  scope={r.get('scope')}  complete={r.get('complete')}"
        )
        for w in r.get("warnings") or []:
            print(f"  warning    {w}")
    print("\nNext: read the manifest, then read every tile it lists in order.")
    if failures and not (args.allow_partial and not all_failed):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
