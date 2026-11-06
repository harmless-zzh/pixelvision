# Changelog

All notable changes to PixelVision are documented here.

## [v0.0.1] — 2026-11-06

First public release.

### Added

- A self-contained Playwright renderer that captures a page, splits it into
  vision-readable tiles, and writes a `tiles.json` manifest next to them.
- The 1568px tile-height convention: tiles stay inside the native resolution of
  vision models, so small text is not silently downscaled.
- An honest capture contract: `scope`, `page_height`, `captured_height`,
  `complete`, `width_truncated`, `wrap_suspected`, and `lazy_truncated` describe
  exactly what was rendered and what was not.
- Segmented full-page capture above 8192 device pixels to avoid Chromium's
  16384px screenshot wrap, with a wrap detector that forces `complete: false`
  when the bottom band repeats the top.
- Selector, viewport, full-page, and logged-in captures; `--profile` and
  `--storage-state` for authenticated pages.
- `--hide` / `--hide-fixed` to remove overlays before capturing, and
  `--viewport-height` so `100vh` layouts no longer inherit the tile height.
- Seam-aware tiling that moves a cut up to 64px to a low-contrast gap.
- `crop.py` for crop/zoom with PNG, JPEG, and WebP output; `doctor.py` for
  environment checks with real browser-launch probes.
- A `pixelshot` backend with strict manifest matching, tile-coverage checks, and
  explicit warnings for flags it does not forward.
- English documentation (`README.md`, `SKILL.md`, `AGENTS.md`) and a
  browser regression test that is skipped by default.

### Security

- Screenshot content and every page-derived manifest field (`title`, `warnings`,
  `url`, `final_url`) are treated as untrusted data, never as instructions.
- URL userinfo and sensitive query parameters are redacted from manifests,
  warnings, error messages, and output directory names.
- Tile names in manifests are validated before use; `crop.py` never builds a
  shell command from user input.

### Known limitations

- Multi-page PDFs are not paginated; only the page visible in the browser viewer
  is captured.
- `pixelshot` metadata (title, slug, viewport height) is approximate, and the
  backend ignores selector/max-height/viewport options with warnings.
- Localhost, intranet, and `file://` targets are allowed; this is documented as a
  trust boundary, not blocked in code.
- The manifest has no `schema_version` and no per-tile y offset.
