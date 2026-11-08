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
from unittest import mock

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

    def test_image_wider_than_the_bound_is_rejected_unless_explicit(self):
        page = self._tall_image(2000, 100)
        with self.assertRaises(ValueError):
            capture.tile_image(page, page.parent / "wide-rejected")
        names = capture.tile_image(page, page.parent / "wide-allowed", allow_wide=True)
        self.assertEqual(names, ["tile_0000.jpg"])

    def test_tile_dir_is_not_reused_for_a_different_url_with_the_same_slug(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = capture._tile_dir_for(root, "https://example.com/a/b")
            first.mkdir()
            capture.write_manifest(first, {"url": "https://example.com/a/b", "complete": True})
            same = capture._tile_dir_for(root, "https://example.com/a/b")
            other = capture._tile_dir_for(root, "https://example.com/a_b")
            self.assertEqual(same, first)
            self.assertNotEqual(other, first)
            self.assertRegex(other.name, r"-[0-9a-f]{8}\.png\.tiles$")

    def test_url_key_ignores_percent_encoding_in_the_path(self):
        self.assertEqual(
            capture._url_key("https://example.com/a%20b"),
            capture._url_key("https://example.com/a b"),
        )

    def test_tile_dir_does_not_put_credentials_in_the_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = capture._tile_dir_for(Path(tmp), "http://user:secret@example.com/x")
            self.assertNotIn("secret", str(path))
            self.assertIn("example.com", str(path))

    def test_manifest_roundtrip_is_json(self):
        payload = {"tiles": ["tile_0000.jpg"], "complete": True}
        path = Path(tempfile.mkdtemp()) / "tiles.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["tiles"], ["tile_0000.jpg"])

    def test_query_tokens_are_redacted_from_urls_and_text(self):
        self.assertEqual(
            capture.redact_url("https://example.com/a?token=SECRET&x=1&api_key=K"),
            "https://example.com/a?token=***&x=1&api_key=***",
        )
        text = capture._clean_text("failed https://example.com/a?token=SECRET&x=1")
        self.assertIn("token=***", text)
        self.assertNotIn("SECRET", text)


class WrapTests(unittest.TestCase):
    def _page_with_repeated_top(self, height=3000, band=512):
        tmp = Path(tempfile.mkdtemp())
        path = tmp / "page.png"
        img = Image.new("RGB", (1280, height), "white")
        top = Image.new("RGB", (1280, band), "white")
        for x in range(0, 1280, 40):
            top.paste((20, 40, 60), (x, 10, x + 20, band - 10))
        img.paste(top, (0, 0))
        img.paste(top, (0, height - band))
        img.save(path)
        return path

    def test_exact_featured_top_and_bottom_bands_look_wrapped(self):
        self.assertTrue(capture._wrapped_bottom(self._page_with_repeated_top()))

    def test_blank_page_is_not_called_wrapped(self):
        path = Path(tempfile.mkdtemp()) / "blank.png"
        Image.new("RGB", (1280, 3000), "white").save(path)
        self.assertFalse(capture._wrapped_bottom(path))


class CaptureRegionTests(unittest.TestCase):
    class _Page:
        def __init__(self, scale=1):
            self.scale = scale
            self.calls = []

        def screenshot(self, *, path, full_page, clip, timeout):
            self.calls.append((full_page, dict(clip)))
            width = int(clip["width"] * self.scale)
            height = int(clip["height"] * self.scale)
            # Encode the clip's y in the first pixel so a bad stitch is visible.
            color = (int(clip["y"]) % 256, 0, 0)
            Image.new("RGB", (width, height), color).save(path)

    def test_segments_are_stitched_and_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = self._Page()
            out = Path(tmp) / "page.png"
            capture._capture_region(
                page, out, x=0, y=0, width=100, height=20000, scale=1, timeout_ms=1000
            )
            self.assertEqual(capture._image_size(out), (100, 20000))
            self.assertEqual(len(page.calls), 3)
            self.assertTrue(all(call[0] for call in page.calls))
            self.assertEqual(list(Path(tmp).glob("*.seg*.png")), [])

    def test_scale_two_stitches_device_pixels(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = self._Page(scale=2)
            out = Path(tmp) / "page.png"
            capture._capture_region(
                page, out, x=0, y=0, width=100, height=9000, scale=2, timeout_ms=1000
            )
            self.assertEqual(capture._image_size(out), (200, 18000))
            self.assertGreaterEqual(len(page.calls), 3)


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
        self.assertTrue(any("captured the top 500px" in w for w in manifest["warnings"]))

    def test_horizontal_truncation_is_incomplete_and_warns(self):
        manifest = self._manifest(width_truncated=True)
        self.assertFalse(manifest["complete"])
        self.assertTrue(any("horizontal overflow" in w for w in manifest["warnings"]))

    def test_zero_captured_height_is_incomplete(self):
        self.assertFalse(self._manifest(page_height=1, captured_height=0)["complete"])

    def test_manifest_redacts_credentials_and_sanitises_title(self):
        manifest = self._manifest(
            url="https://user:secret@example.com/x",
            final_url="https://user:secret@example.com/x",
            title="A\x1bB\nC",
        )
        self.assertEqual(manifest["url"], "https://***@example.com/x")
        self.assertNotIn("secret", json.dumps(manifest))
        self.assertEqual(manifest["title"], "A B C")
        # A lone surrogate must not make write_manifest raise.
        capture._clean_text("A\ud800B")

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

    def test_wrap_suspicion_is_incomplete_and_warns(self):
        manifest = self._manifest(wrap_suspected=True)
        self.assertFalse(manifest["complete"])
        self.assertTrue(manifest["wrap_suspected"])
        self.assertTrue(any("bottom band repeats" in w for w in manifest["warnings"]))

    def test_lazy_scroll_cap_is_incomplete_and_warns(self):
        manifest = self._manifest(page_height=50000, captured_height=50000, lazy_truncated=True)
        self.assertFalse(manifest["complete"])
        self.assertTrue(manifest["lazy_truncated"])
        self.assertTrue(any("lazy-load pre-scroll limit" in w for w in manifest["warnings"]))

    def test_one_pixel_rounding_is_still_complete(self):
        self.assertTrue(self._manifest(captured_height=999)["complete"])

    def test_schema_version_is_stamped(self):
        manifest = self._manifest()
        self.assertEqual(manifest["schema_version"], 1)

    def test_tile_geometry_is_passed_through(self):
        manifest = self._manifest(
            tiles=["tile_0000.jpg", "tile_0001.jpg", "tile_0002.jpg"],
            tile_offsets=[0, 1568, 3136],
            tile_heights=[1568, 1568, 812],
        )
        self.assertEqual(manifest["tile_offsets"], [0, 1568, 3136])
        self.assertEqual(manifest["tile_heights"], [1568, 1568, 812])
        self.assertEqual(manifest["tiles"][2], "tile_0002.jpg")

    def test_missing_geometry_is_an_empty_list_not_missing_keys(self):
        manifest = self._manifest()
        self.assertEqual(manifest["tile_offsets"], [])
        self.assertEqual(manifest["tile_heights"], [])

    def test_unreported_viewport_stays_null(self):
        # pixelshot does not record its viewport: saying "1568" would look measured.
        manifest = self._manifest(
            viewport={"width": 1280, "height": None}, viewport_reported=False
        )
        self.assertIsNone(manifest["viewport"]["height"])
        self.assertFalse(manifest["viewport_reported"])
        self.assertTrue(self._manifest()["viewport_reported"])

    def test_manifest_is_written_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = capture.write_manifest(Path(tmp), self._manifest())
            self.assertEqual(path.name, "tiles.json")
            self.assertFalse((Path(tmp) / "tiles.json.tmp").exists())
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["scope"], "page")
            self.assertTrue(data["complete"])
            self.assertEqual(data["schema_version"], 1)


class TileGeometryTests(unittest.TestCase):
    def test_offsets_are_cumulative_and_cover_the_image(self):
        tmp = Path(tempfile.mkdtemp())
        page = tmp / "page.png"
        Image.new("RGB", (1280, 5000), "white").save(page)
        out = tmp / "tiles"
        names = capture.tile_image(page, out, tile_height=1568)
        offsets, heights = capture._tile_geometry(out, names)
        self.assertEqual(len(offsets), len(names))
        self.assertEqual(len(heights), len(names))
        self.assertEqual(offsets[0], 0)
        for index in range(1, len(offsets)):
            self.assertEqual(offsets[index], offsets[index - 1] + heights[index - 1])
        self.assertEqual(offsets[-1] + heights[-1], 5000)

    def test_unequal_heights_are_reported_honestly(self):
        # The sliver rebalance makes the last two bands equal but shorter than 1568.
        tmp = Path(tempfile.mkdtemp())
        page = tmp / "page.png"
        Image.new("RGB", (800, 3 * 1568 + 10), "white").save(page)
        out = tmp / "tiles"
        names = capture.tile_image(page, out, tile_height=1568)
        _, heights = capture._tile_geometry(out, names)
        self.assertEqual(heights[-2:], [789, 789])
        self.assertNotEqual(heights[-1], 1568)

    def test_missing_tile_returns_no_geometry(self):
        tmp = Path(tempfile.mkdtemp())
        self.assertEqual(capture._tile_geometry(tmp, ["tile_0000.jpg"]), ([], []))


class TargetGuardTests(unittest.TestCase):
    def test_loopback_and_private_are_refused_by_default(self):
        for url in (
            "http://localhost:5173/",
            "http://127.0.0.1/",
            "http://[::1]:8000/",
            "http://192.168.1.10/admin",
            "http://10.0.0.5/",
            "http://169.254.169.254/latest/meta-data/",
            "http://127.1/",
            "http://2130706433/",
            "http://0x7f.0.0.1/",
            "http://nas.local/",
            "http://printer.internal/",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    capture.guard_target(url)

    def test_public_targets_and_flag_are_allowed(self):
        capture.guard_target("https://example.com/docs")
        capture.guard_target("http://93.184.216.34/")
        capture.guard_target("http://localhost:5173/", allow_private=True)

    def test_file_targets_are_refused_by_default(self):
        with self.assertRaises(ValueError):
            capture.guard_target("file:///C:/tmp/page.html")
        capture.guard_target("file:///C:/tmp/page.html", allow_file=True)

    def test_guard_runs_before_a_browser_launches(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = Path(tmp) / "page.html"
            page.write_text("<h1>hi</h1>", encoding="utf-8")
            with mock.patch.object(capture, "render_with_playwright") as render:
                code = capture.main([str(page), "-o", tmp])
            self.assertEqual(code, 1)
            render.assert_not_called()

    def test_pixelshot_guard_normalizes_bare_targets(self):
        # A bare host or path has no scheme for guard_target to inspect.
        with tempfile.TemporaryDirectory() as tmp:
            page = Path(tmp) / "page.html"
            page.write_text("<h1>hi</h1>", encoding="utf-8")
            out = Path(tmp) / "shots"
            with self.assertRaises(ValueError):
                capture.render_with_pixelshot(["localhost:5173"], out)
            with self.assertRaises(ValueError):
                capture.render_with_pixelshot([str(page)], out)


class ParserTests(unittest.TestCase):
    def _parse(self, *argv):
        return capture.build_parser().parse_args(["https://example.com", *argv])

    def test_defaults_are_vision_safe(self):
        args = self._parse()
        self.assertEqual(args.tile_height, capture.VISION_MAX_EDGE)
        self.assertEqual(args.viewport_width, capture.DEFAULT_VIEWPORT_WIDTH)
        self.assertEqual(args.viewport_height, capture.DEFAULT_VIEWPORT_HEIGHT)
        self.assertEqual(args.selector_timeout, capture.DEFAULT_SELECTOR_TIMEOUT_MS)
        self.assertFalse(args.allow_partial)
        self.assertEqual(args.backend, "auto")

    def test_out_of_range_values_are_rejected(self):
        cases = [
            ("--tile-height", "0"),
            ("--tile-height", "1569"),
            ("--viewport-width", "100"),
            ("--max-height", "-1"),
            ("--scale", "5"),
            ("--quality", "101"),
            ("--viewport-height", "100"),
            ("--selector-timeout", "99"),
        ]
        for flag, value in cases:
            with self.subTest(flag=flag, value=value):
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    self._parse(flag, value)

    def test_zero_max_height_means_no_cap(self):
        self.assertEqual(self._parse("--max-height", "0").max_height, 0)

    def test_profile_defaults_to_none_and_show_is_off(self):
        args = self._parse()
        self.assertIsNone(args.profile)
        self.assertFalse(args.show)

    def test_profile_and_show_are_accepted_together(self):
        args = self._parse("--profile", "prof", "--show")
        self.assertEqual(args.profile, "prof")
        self.assertTrue(args.show)


class CropParsingTests(unittest.TestCase):
    def test_box(self):
        self.assertEqual(crop.parse_box("1,2,3,4"), (1, 2, 3, 4))
        self.assertEqual(crop.parse_box(" 0, 0 , 10,20 "), (0, 0, 10, 20))

    def test_box_rejects_bad_input(self):
        with self.assertRaises(SystemExit):
            crop.parse_box("1,2,3")
        for bad in ("nan,0,10,10", "inf,0,10,10", "1,2,3,x"):
            with self.subTest(bad=bad):
                with self.assertRaises(SystemExit):
                    crop.parse_box(bad)

    def test_percent_rejects_non_finite_and_out_of_range(self):
        for bad in ("nan,0,100,100", "0,0,101,100", "-1,0,100,100"):
            with self.subTest(bad=bad):
                with self.assertRaises(SystemExit):
                    crop.parse_percent(bad, (100, 100))

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

    def test_zoom_rejects_non_finite_and_excessive_zoom(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.png"
            src.write_bytes(b"not decoded before validation")
            for zoom in ("nan", "inf", "9"):
                with self.subTest(zoom=zoom):
                    with self.assertRaises(SystemExit):
                        crop.main([str(src), "--zoom", zoom])

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

    def test_local_file_needs_allow_file_and_then_renders(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = Path(tmp) / "page.html"
            page.write_text("<h1>hi</h1>", encoding="utf-8")
            fake = {
                "url": page.as_uri(),
                "complete": True,
                "manifest_path": str(Path(tmp) / "tiles.json"),
                "tiles": ["tile_0000.jpg"],
                "viewport": {"width": 1280, "height": 900},
                "tile_height": 1568,
                "page_height": 900,
                "captured_height": 900,
                "scope": "page",
            }
            with mock.patch.object(capture, "render_with_playwright", return_value=fake) as render:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    code = capture.main([str(page), "-o", tmp, "--allow-file"])
            self.assertEqual(code, 0)
            render.assert_called_once()
            self.assertEqual(render.call_args.kwargs["allow_file"], True)

    def test_private_target_needs_allow_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                code = capture.main(["http://127.0.0.1:8080/", "-o", tmp])
            self.assertEqual(code, 1)
            self.assertIn("--allow-private", err.getvalue())

    def test_show_without_profile_is_rejected_before_launching(self):
        with tempfile.TemporaryDirectory() as tmp:
            err = io.StringIO()
            with mock.patch.object(capture, "render_with_playwright") as render:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                    code = capture.main(["https://example.com", "-o", tmp, "--show"])
            self.assertEqual(code, 2)
            self.assertIn("--profile", err.getvalue())
            render.assert_not_called()

    def test_missing_dependency_exits_two_with_actionable_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            err = io.StringIO()
            deeper = SystemExit("playwright is not installed.\n  python -m pip install playwright")
            with mock.patch.object(capture, "render_with_playwright", side_effect=deeper):
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                    code = capture.main(["https://example.com", "-o", tmp])
            self.assertEqual(code, 2)
            self.assertIn("playwright is not installed", err.getvalue())

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

    def test_json_all_failed_batch_lists_both_and_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Two missing files: no browser is launched, so the batch stays offline.
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = capture.main(["missing_a.html", "missing_b.html", "-o", tmp, "--json"])
            self.assertEqual(code, 1)
            results = json.loads(out.getvalue())["results"]
            self.assertEqual([r["complete"] for r in results], [False, False])
            self.assertEqual([r["url"] for r in results], ["missing_a.html", "missing_b.html"])

    def _fake_success(self, out_root: Path) -> dict:
        return {
            "url": "https://good.example/",
            "title": "",
            "manifest_path": str(out_root / "fake.png.tiles" / "tiles.json"),
            "tiles": [],
            "tile_height": 1568,
            "viewport": {"width": 1280},
            "scope": "page",
            "complete": True,
            "captured_height": 1,
            "page_height": 1,
            "warnings": [],
        }

    def test_partial_batch_fails_by_default_and_passes_with_allow_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_root = Path(tmp)
            with mock.patch.object(capture, "render_with_playwright",
                                   return_value=self._fake_success(out_root)):
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    default_code = capture.main(
                        ["no_such_page.html", "https://good.example/", "-o", tmp]
                    )
                    partial_code = capture.main(
                        ["no_such_page.html", "https://good.example/", "-o", tmp, "--allow-partial"]
                    )
            self.assertEqual(default_code, 1)
            self.assertEqual(partial_code, 0)


class LoginWaitTests(unittest.TestCase):
    class _Page:
        def __init__(self, answers):
            self.answers = list(answers)
            self.polls = 0

        def evaluate(self, _script):
            self.polls += 1
            return self.answers.pop(0) if self.answers else {"ready": True, "asking": True}

        def wait_for_timeout(self, _ms):
            pass

    def test_returns_true_once_the_password_field_is_gone(self):
        page = self._Page([
            {"ready": True, "asking": True},
            {"ready": True, "asking": True},
            {"ready": True, "asking": False},
        ])
        warnings: list[str] = []
        self.assertTrue(capture._wait_for_login(page, 30, warnings))
        self.assertEqual(page.polls, 3)
        self.assertIn("signed-in", warnings[0])

    def test_a_page_that_has_not_loaded_is_not_called_signed_in(self):
        page = self._Page([
            {"ready": False, "asking": False},
            {"ready": True, "asking": False},
        ])
        warnings: list[str] = []
        self.assertTrue(capture._wait_for_login(page, 30, warnings))
        self.assertEqual(page.polls, 2)

    def test_gives_up_and_says_so(self):
        warnings: list[str] = []
        self.assertFalse(capture._wait_for_login(self._Page([]), 0, warnings))
        self.assertIn("no login detected", warnings[0])


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

    def test_rejects_path_traversal_and_absolute_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            for names in (["../secret.jpg"], [str(Path(tmp) / "x.jpg")], ["sub/tile.jpg"]):
                with self.subTest(names=names):
                    with self.assertRaises(RuntimeError):
                        capture._verify_tiles(Path(tmp), names, [], strict=True)

    def test_strict_mode_rejects_missing_tiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError):
                capture._verify_tiles(Path(tmp), ["tile_0000.jpg"], [], strict=True)

    def test_empty_tile_list_is_not_a_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError):
                capture._verify_tiles(Path(tmp), [], [], strict=True)


class PixelshotTests(unittest.TestCase):
    def test_match_requires_the_same_url(self):
        produced = [(Path("a.png.tiles"), {"url": "https://example.com/a"})]
        self.assertEqual(
            capture._match_pixelshot_output("https://example.com/a", produced), produced[0]
        )
        self.assertIsNone(capture._match_pixelshot_output("https://example.com/b", produced))

    def test_match_never_adopts_the_only_unrelated_manifest(self):
        produced = [(Path("old.png.tiles"), {"url": "https://example.com/old"})]
        self.assertIsNone(capture._match_pixelshot_output("https://example.com/new", produced))


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
