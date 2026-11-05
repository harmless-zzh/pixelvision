"""Tests for the capture/crop helpers. Run: python -m unittest discover -s tests -v"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import capture  # noqa: E402
import crop  # noqa: E402

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None


class SlugTests(unittest.TestCase):
    def test_strips_scheme_and_sanitises(self):
        self.assertEqual(capture.slugify("https://example.com"), "example.com")
        self.assertEqual(capture.slugify("https://example.com/"), "example.com")
        self.assertEqual(capture.slugify("https://news.ycombinator.com/item?id=1"),
                         "news.ycombinator.com_item_id_1")

    def test_keeps_it_filesystem_safe(self):
        slug = capture.slugify("https://例子.com/路径?q=1&x=2")
        self.assertNotIn("/", slug)
        self.assertNotIn("?", slug)
        self.assertTrue(slug)
        self.assertLessEqual(len(slug), 120)

    def test_empty_url_falls_back(self):
        self.assertEqual(capture.slugify(""), "page")

    def test_local_file_uses_basename(self):
        self.assertEqual(capture.slugify("file:///D:/work/examples/demo.html"), "demo.html")
        self.assertEqual(capture.slugify("file:///tmp/My%20Page.html"), "My_Page.html")


@unittest.skipIf(Image is None, "pillow not installed")
class TileTests(unittest.TestCase):
    def _tall_image(self, width: int, height: int) -> Path:
        tmp = Path(tempfile.mkdtemp())
        path = tmp / "page.png"
        Image.new("RGB", (width, height), "white").save(path)
        return path

    @staticmethod
    def _size(path: Path) -> tuple[int, int]:
        with Image.open(path) as img:
            return img.size

    def test_splits_into_expected_bands(self):
        page = self._tall_image(1280, 5000)
        out = page.parent / "tiles"
        names = capture.tile_image(page, out, tile_height=1568, quality=80)
        self.assertEqual(names, ["tile_0000.jpg", "tile_0001.jpg", "tile_0002.jpg", "tile_0003.jpg"])
        heights = [self._size(out / n)[1] for n in names]
        self.assertEqual(heights[:3], [1568, 1568, 1568])
        self.assertEqual(heights[3], 5000 - 3 * 1568)
        self.assertTrue(all(h <= 1568 for h in heights))

    def test_short_page_is_one_tile(self):
        page = self._tall_image(900, 400)
        names = capture.tile_image(page, page.parent / "tiles", tile_height=1568)
        self.assertEqual(names, ["tile_0000.jpg"])
        self.assertEqual(self._size(page.parent / "tiles" / names[0])[1], 400)

    def test_sliver_is_merged_not_emitted(self):
        page = self._tall_image(800, 3 * 1568 + 10)
        out = page.parent / "tiles"
        names = capture.tile_image(page, out, tile_height=1568)
        self.assertEqual(len(names), 3, "a 10px sliver must not become its own unreadable tile")
        self.assertLess(self._size(out / names[-1])[1], 1600)

    def test_manifest_roundtrip_is_json(self):
        payload = {"tiles": ["tile_0000.jpg"], "complete": True}
        path = Path(tempfile.mkdtemp()) / "tiles.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["tiles"], ["tile_0000.jpg"])


class CropParsingTests(unittest.TestCase):
    def test_box(self):
        self.assertEqual(crop.parse_box("1,2,3,4"), (1, 2, 3, 4))
        self.assertEqual(crop.parse_box(" 0, 0 , 10,20 "), (0, 0, 10, 20))

    def test_box_rejects_bad_input(self):
        with self.assertRaises(SystemExit):
            crop.parse_box("1,2,3")

    def test_percent(self):
        self.assertEqual(crop.parse_percent("0,0,100,50", (1000, 800)), (0, 0, 1000, 400))

    def test_oversize_hint_only_above_bound(self):
        self.assertIsNone(crop.oversize_hint((1280, 1568)))
        hint = crop.oversize_hint((1680, 120))
        self.assertIsNotNone(hint)
        self.assertIn("1568", hint)


class BrowserDiscoveryTests(unittest.TestCase):
    def test_explicit_chrome_path_wins(self):
        import os
        saved = os.environ.get("CHROME_PATH")
        fake = Path(tempfile.mkdtemp()) / "msedge.exe"
        fake.write_bytes(b"")
        try:
            os.environ["CHROME_PATH"] = str(fake)
            channel, path = capture.find_browser()
            self.assertEqual(channel, "msedge")
            self.assertEqual(Path(path), fake)
        finally:
            os.environ.pop("CHROME_PATH", None)
            if saved is not None:
                os.environ["CHROME_PATH"] = saved

    def test_missing_path_is_ignored(self):
        import os
        saved = os.environ.get("CHROME_PATH")
        try:
            os.environ["CHROME_PATH"] = str(Path(tempfile.mkdtemp()) / "nope.exe")
            channel, path = capture.find_browser()
            self.assertTrue(path is None or Path(path).is_file())
        finally:
            os.environ.pop("CHROME_PATH", None)
            if saved is not None:
                os.environ["CHROME_PATH"] = saved


if __name__ == "__main__":
    unittest.main()
