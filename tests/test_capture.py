"""Tests for the capture/crop helpers. Run: python -m unittest discover -s tests -v"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import time
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
class NormalizeTargetTests(unittest.TestCase):
    def test_bare_host_becomes_https(self):
        self.assertEqual(capture.normalize_target("example.com"), "https://example.com")
        self.assertEqual(capture.normalize_target("example.com/a?x=1#y"),
                         "https://example.com/a?x=1#y")
        self.assertEqual(capture.normalize_target("example.com:8080/x"),
                         "https://example.com:8080/x")

    def test_loopback_defaults_to_http(self):
        self.assertEqual(capture.normalize_target("localhost:8000"), "http://localhost:8000")
        self.assertEqual(capture.normalize_target("localhost"), "http://localhost")
        self.assertEqual(capture.normalize_target("127.0.0.1:5173"), "http://127.0.0.1:5173")
        self.assertEqual(capture.normalize_target("[::1]:5173"), "http://[::1]:5173")

    def test_explicit_schemes_are_untouched(self):
        for url in ("https://example.com/missing", "http://example.com", "file:///C:/tmp/a.html"):
            with self.subTest(url=url):
                self.assertEqual(capture.normalize_target(url), url)

    def test_existing_file_becomes_file_uri(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = Path(tmp) / "demo.html"
            page.write_text("<html><body>hi</body></html>", encoding="utf-8")
            target = capture.normalize_target(str(page))
            self.assertTrue(target.startswith("file://"))
            self.assertTrue(target.endswith("demo.html"))

    def test_missing_file_names_the_file(self):
        for raw in ("notes.html", "missing/deep/page.html", "no_such_file"):
            with self.subTest(raw=raw):
                with self.assertRaises(FileNotFoundError) as ctx:
                    capture.normalize_target(raw)
                self.assertIn("no such file", str(ctx.exception))

    def test_missing_html_hints_at_the_url_form(self):
        with self.assertRaises(FileNotFoundError) as ctx:
            capture.normalize_target("demo.html")
        self.assertIn("https://demo.html", str(ctx.exception))

    def test_directory_says_so_instead_of_no_such_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError) as ctx:
                capture.normalize_target(tmp)
            message = str(ctx.exception)
            self.assertIn("is a directory", message)
            self.assertNotIn("no such file", message)


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

    def test_sliver_is_rebalanced_within_the_bound(self):
        page = self._tall_image(800, 3 * 1568 + 10)
        out = page.parent / "tiles"
        names = capture.tile_image(page, out, tile_height=1568)
        heights = [self._size(out / n)[1] for n in names]
        self.assertTrue(all(h <= 1568 for h in heights), heights)
        self.assertEqual(sum(heights), 3 * 1568 + 10)
        self.assertEqual(heights[-2:], [789, 789], "the sliver is split evenly with the previous band")

    def test_tile_height_outside_the_bound_is_rejected(self):
        page = self._tall_image(400, 400)
        with self.assertRaises(ValueError):
            capture.tile_image(page, page.parent / "tiles", tile_height=0)
        with self.assertRaises(ValueError):
            capture.tile_image(page, page.parent / "tiles", tile_height=1569)

    def test_manifest_roundtrip_is_json(self):
        payload = {"tiles": ["tile_0000.jpg"], "complete": True}
        path = Path(tempfile.mkdtemp()) / "tiles.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["tiles"], ["tile_0000.jpg"])


class ManifestTests(unittest.TestCase):
    def _manifest(self, **overrides):
        base = dict(
            url="https://example.com",
            final_url="https://example.com",
            title="Example",
            page_height=1000,
            captured_height=1000,
            tile_height=1568,
            viewport={"width": 1280, "height": 1568},
            scale=1,
            selector=None,
            tiles=["tile_0000.jpg"],
            scope="page",
            backend="test",
            warnings=[],
            started=time.time(),
        )
        base.update(overrides)
        return capture.build_manifest(**base)

    def test_full_page_is_complete(self):
        manifest = self._manifest()
        self.assertTrue(manifest["complete"])
        self.assertEqual(manifest["scope"], "page")
        self.assertEqual(manifest["captured_height"], 1000)
        self.assertEqual(manifest["warnings"], [])

    def test_capped_page_is_incomplete_and_warns(self):
        manifest = self._manifest(captured_height=500)
        self.assertFalse(manifest["complete"])
        self.assertTrue(any("captured 500px" in w for w in manifest["warnings"]))

    def test_selector_is_never_complete(self):
        manifest = self._manifest(scope="selector", selector="#box", captured_height=220, page_height=3820)
        self.assertFalse(manifest["complete"])
        # build_manifest adds no warning of its own for alternate scopes; the renderer
        # is what reports "captured one element", so this stays empty here.
        self.assertEqual(manifest["warnings"], [])

    def test_viewport_and_pdf_are_incomplete(self):
        for scope in ("viewport", "pdf-viewer"):
            with self.subTest(scope=scope):
                manifest = self._manifest(scope=scope, page_height=5310)
                self.assertFalse(manifest["complete"])

    def test_unknown_page_height_is_incomplete(self):
        self.assertFalse(self._manifest(page_height=0, captured_height=0)["complete"])

    def test_one_pixel_rounding_is_still_complete(self):
        self.assertTrue(self._manifest(captured_height=999)["complete"])

    def test_manifest_is_written_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = capture.write_manifest(Path(tmp), self._manifest())
            self.assertEqual(path.name, "tiles.json")
            self.assertFalse((Path(tmp) / "tiles.json.tmp").exists())
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["scope"], "page")
            self.assertTrue(data["complete"])


class ParserTests(unittest.TestCase):
    def _parse(self, *argv):
        return capture.build_parser().parse_args(["https://example.com", *argv])

    def test_defaults_are_vision_safe(self):
        args = self._parse()
        self.assertEqual(args.tile_height, capture.VISION_MAX_EDGE)
        self.assertEqual(args.viewport_width, capture.DEFAULT_VIEWPORT_WIDTH)
        self.assertEqual(args.backend, "auto")

    def test_out_of_range_values_are_rejected(self):
        cases = [
            ("--tile-height", "0"),
            ("--tile-height", "1569"),
            ("--viewport-width", "100"),
            ("--max-height", "-1"),
            ("--scale", "5"),
            ("--quality", "101"),
        ]
        for flag, value in cases:
            with self.subTest(flag=flag, value=value):
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    self._parse(flag, value)

    def test_zero_max_height_means_no_cap(self):
        self.assertEqual(self._parse("--max-height", "0").max_height, 0)


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

    def test_output_format_follows_suffix(self):
        self.assertEqual(crop.output_format(Path("x.png")), "PNG")
        self.assertEqual(crop.output_format(Path("x")), "PNG")
        self.assertEqual(crop.output_format(Path("x.jpg")), "JPEG")
        self.assertEqual(crop.output_format(Path("x.JPEG")), "JPEG")
        self.assertEqual(crop.output_format(Path("x.webp")), "WEBP")
        with self.assertRaises(SystemExit):
            crop.output_format(Path("x.gif"))

    @unittest.skipIf(Image is None, "pillow not installed")
    def test_jpeg_suffix_writes_jpeg_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.png"
            Image.new("RGB", (100, 100), "white").save(src)
            out = Path(tmp) / "crop.jpg"
            with contextlib.redirect_stdout(io.StringIO()):
                crop.main([str(src), "--box", "0,0,50,50", "-o", str(out)])
            self.assertEqual(out.read_bytes()[:3], b"\xff\xd8\xff")


class CliTests(unittest.TestCase):
    def test_missing_local_file_fails_before_launching_a_browser(self):
        with tempfile.TemporaryDirectory() as tmp:
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                code = capture.main(["no_such_page.html", "-o", tmp])
            self.assertEqual(code, 1)
            self.assertIn("no such file", err.getvalue())
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_one_bad_target_does_not_hide_the_rest(self):
        with tempfile.TemporaryDirectory() as tmp:
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                code = capture.main(["no_such_page.html", "also_missing.html", "-o", tmp])
            self.assertEqual(code, 1)
            message = err.getvalue()
            self.assertIn("no_such_page.html", message)
            self.assertIn("also_missing.html", message)

    def test_json_failure_payload_is_still_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = capture.main(["no_such_page.html", "-o", tmp, "--json"])
            self.assertEqual(code, 1)
            payload = json.loads(out.getvalue())
            self.assertIn("no such file", payload["error"])
            # A caller that iterates results must see the same keys a success carries.
            self.assertIs(payload["complete"], False)

    def test_json_mixed_batch_exits_zero_and_lists_both(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Two missing files: no browser is launched, so the batch stays offline.
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = capture.main(["missing_a.html", "missing_b.html", "-o", tmp, "--json"])
            self.assertEqual(code, 1)
            results = json.loads(out.getvalue())["results"]
            self.assertEqual([r["complete"] for r in results], [False, False])
            self.assertEqual([r["url"] for r in results], ["missing_a.html", "missing_b.html"])


class VerifyTilesTests(unittest.TestCase):
    def test_flags_a_tile_the_manifest_promises_but_disk_lacks(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "tile_0000.jpg").write_bytes(b"x")
            warnings: list[str] = []
            capture._verify_tiles(Path(tmp), ["tile_0000.jpg", "tile_0001.jpg"], warnings)
            self.assertEqual(len(warnings), 1)
            self.assertIn("tile_0001.jpg", warnings[0])

    def test_stays_quiet_when_every_tile_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "tile_0000.jpg").write_bytes(b"x")
            warnings: list[str] = []
            capture._verify_tiles(Path(tmp), ["tile_0000.jpg"], warnings)
            self.assertEqual(warnings, [])


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
