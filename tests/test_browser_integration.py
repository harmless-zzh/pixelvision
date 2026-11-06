"""Opt-in browser regression for the 16384 px screenshot wrap.

The normal suite stays browser-free (AGENTS.md). Set PIXELVISION_BROWSER_TESTS=1
to run this against the local Playwright install; it is the only test that proves
a 20000 px page is stitched rather than silently wrapped by Chromium.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from PIL import Image, ImageChops  # noqa: E402

import capture  # noqa: E402


@unittest.skipUnless(
    os.environ.get("PIXELVISION_BROWSER_TESTS") == "1",
    "set PIXELVISION_BROWSER_TESTS=1 to run the real-browser regression",
)
class BrowserWrapRegressionTests(unittest.TestCase):
    def test_20000px_page_is_stitched_without_wrap(self) -> None:
        rows = "\n".join(
            f'<div class="row">ROW {i:03d}</div>' for i in range(200)
        )
        html = (
            "<!doctype html><html><head><meta charset='utf-8'><style>"
            "body{margin:0;font:16px monospace}"
            ".row{height:100px;box-sizing:border-box;border-bottom:1px solid #ddd;"
            "padding:8px}</style></head><body>"
            f"{rows}</body></html>"
        )
        with tempfile.TemporaryDirectory(prefix="pixelvision-browser-") as tmp:
            root = Path(tmp)
            page = root / "mid.html"
            page.write_text(html, encoding="utf-8")
            manifest = capture.render_with_playwright(
                str(page),
                root / "out",
                max_height=0,
                wait_network_idle=False,
                extra_wait_ms=0,
                timeout_ms=30000,
            )
            self.assertEqual(manifest["scope"], "page")
            self.assertEqual(manifest["page_height"], 20000)
            self.assertEqual(manifest["captured_height"], 20000)
            self.assertTrue(manifest["complete"])
            self.assertFalse(manifest["wrap_suspected"])
            self.assertFalse(manifest["lazy_truncated"])
            self.assertFalse(manifest["width_truncated"])

            # _tile_dir_for may hash the stem; find the single manifest.
            manifests = list((root / "out").glob("*.png.tiles/tiles.json"))
            self.assertEqual(len(manifests), 1)
            tile_dir = manifests[0].parent
            with Image.open(tile_dir / "page.png") as img:
                self.assertEqual((img.width, img.height), (1280, 20000))
                top = img.convert("RGB").crop((0, 0, img.width, 3616))
                bottom = img.convert("RGB").crop(
                    (0, img.height - 3616, img.width, img.height)
                )
                self.assertIsNotNone(ImageChops.difference(top, bottom).getbbox())

            with open(tile_dir / "tiles.json", encoding="utf-8-sig") as fh:
                written = json.load(fh)
            self.assertEqual(written["complete"], True)


if __name__ == "__main__":
    unittest.main()
