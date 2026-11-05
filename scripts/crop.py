#!/usr/bin/env python3
"""Crop and zoom a screenshot so small text becomes readable.

Reading a tile is cheap; reading a *whole page* at maximum detail is not. The
usual loop is therefore: read the tiles, spot the region that matters, crop it,
read the crop. A crop of ~800x800 pixels at 2x is dramatically easier for a
vision model than the same content at 1x inside a 1280x1568 tile.

Coordinates are pixels from the top-left of the source image. They are values you
choose *after* looking at a page you did not write, so this script never builds a
shell string from them: it takes argv only (no `eval`, no f-strings into a shell).

Usage:
    python crop.py page.png --box 0,1200,1280,1900 -o crop.png
    python crop.py tile_0002.jpg --box 100,40,700,300 --zoom 2 -o crop.png
    python crop.py page.png --box 0,0,1280,1568 --mode downscale -o thumb.png
    python crop.py tile_0000.jpg --bbox-percent 0,0,100,50 -o top-half.png
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from _console import force_utf8_stdio


def parse_box(text: str) -> tuple[int, int, int, int]:
    parts = [p.strip() for p in text.replace(";", ",").split(",") if p.strip()]
    if len(parts) != 4:
        raise SystemExit("--box needs 4 comma-separated integers: x1,y1,x2,y2")
    try:
        x1, y1, x2, y2 = (int(float(p)) for p in parts)
    except ValueError:
        raise SystemExit(f"--box values must be numbers, got: {text!r}")
    return x1, y1, x2, y2


def parse_percent(text: str, size: tuple[int, int]) -> tuple[int, int, int, int]:
    parts = [p.strip() for p in text.replace(";", ",").split(",") if p.strip()]
    if len(parts) != 4:
        raise SystemExit("--bbox-percent needs 4 comma-separated numbers: left,top,right,bottom")
    left, top, right, bottom = (float(p) for p in parts)
    width, height = size
    return (
        int(width * left / 100),
        int(height * top / 100),
        int(width * right / 100),
        int(height * bottom / 100),
    )


def output_format(path: Path) -> str:
    """Pick the encoder from the output suffix instead of always writing PNG."""
    suffix = path.suffix.lower()
    if suffix in ("", ".png"):
        return "PNG"
    if suffix in (".jpg", ".jpeg"):
        return "JPEG"
    if suffix == ".webp":
        return "WEBP"
    raise SystemExit(
        f"unsupported output extension {suffix!r}; use .png, .jpg/.jpeg, or .webp"
    )


def oversize_hint(size: tuple[int, int]) -> str | None:
    """Warn when a crop would be downscaled by a vision model anyway."""
    long_edge = max(size)
    if long_edge <= 1568:
        return None
    return (
        f"output is {long_edge}px on the long edge; vision models downscale above 1568px, "
        f"so the zoom may not buy you extra legibility. Use --mode downscale, a smaller "
        f"--box, or a lower --zoom."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Crop and zoom a screenshot tile for closer visual reading.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("image", help="source image (page.png or tile_NNNN.jpg)")
    parser.add_argument("--box", help="crop box in pixels: x1,y1,x2,y2")
    parser.add_argument("--bbox-percent", dest="bbox_percent",
                        help="crop box in percent: left,top,right,bottom (0-100)")
    parser.add_argument("--zoom", type=float, default=0.0,
                        help="upscale factor for the crop (2.0 doubles its size)")
    parser.add_argument("--mode", choices=["clip", "downscale"], default="clip",
                        help="clip: sharp crop; downscale: fit under 1568px long edge")
    parser.add_argument("-o", "--out", help="output path (default: <image>_crop.png)")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    args = parser.parse_args(argv)
    force_utf8_stdio()

    if args.zoom < 0:
        raise SystemExit("--zoom must be >= 0 (0 means no zoom)")

    try:
        from PIL import Image
    except ImportError:
        raise SystemExit("pillow is required: python -m pip install pillow")

    src = Path(args.image).expanduser().resolve()
    if not src.is_file():
        raise SystemExit(f"no such image: {src}")

    # Validate the output suffix before spending time on the crop itself.
    out = Path(args.out).expanduser() if args.out else src.with_name(src.stem + "_crop.png")
    fmt = output_format(out)

    with Image.open(src) as img:
        img = img.convert("RGB")
        width, height = img.size

        if args.bbox_percent:
            box = parse_percent(args.bbox_percent, (width, height))
        elif args.box:
            box = parse_box(args.box)
        else:
            box = (0, 0, width, height)

        x1, y1, x2, y2 = box
        x1, x2 = sorted((max(0, min(x1, width)), max(0, min(x2, width))))
        y1, y2 = sorted((max(0, min(y1, height)), max(0, min(y2, height))))
        if x2 - x1 < 2 or y2 - y1 < 2:
            raise SystemExit(f"crop box is empty after clamping: {(x1, y1, x2, y2)} in {width}x{height}")

        crop = img.crop((x1, y1, x2, y2))

        if args.zoom and args.zoom != 1.0:
            new_size = (max(1, int(crop.width * args.zoom)), max(1, int(crop.height * args.zoom)))
            crop = crop.resize(new_size, Image.LANCZOS)
        if args.mode == "downscale" and max(crop.size) > 1568:
            ratio = 1568 / max(crop.size)
            crop = crop.resize((max(1, int(crop.width * ratio)), max(1, int(crop.height * ratio))), Image.LANCZOS)

        out.parent.mkdir(parents=True, exist_ok=True)
        if fmt == "JPEG":
            crop.save(out, "JPEG", quality=92, optimize=True)
        elif fmt == "WEBP":
            crop.save(out, "WEBP", quality=92, method=6)
        else:
            crop.save(out, "PNG", optimize=True)

    info = {
        "source": str(src),
        "source_size": [width, height],
        "box": [x1, y1, x2, y2],
        "out": str(out),
        "out_size": [crop.width, crop.height],
        "format": fmt,
    }
    hint = oversize_hint(crop.size)
    if hint:
        info["hint"] = hint
    if args.json:
        import json
        print(json.dumps(info, ensure_ascii=False))
    else:
        print(f"{out}  {crop.width}x{crop.height}  (from {width}x{height} at {x1},{y1})")
        if hint:
            print(hint, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
