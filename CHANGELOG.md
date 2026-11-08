# Changelog

All notable changes to PixelVision are documented here.

## [Unreleased]

### Added

- `schema_version: 1` in every manifest, so a reader can tell which contract it holds.
- `tile_offsets` and `tile_heights`: the y offset and pixel height of each tile, measured
  from the files on disk. Tile heights are not uniform (`_quiet_cut`, tail rebalance), so
  `tile_offsets[i] + y_in_tile` is the only reliable page-coordinate mapping. `tiles` keeps
  its order and shape; older readers keep working.
- `--allow-file` and `--allow-private`: local targets are now opt-in. `file://`,
  loopback/private/link-local addresses, and `*.local` / `*.internal` names are refused
  before a browser launches, and the resolved URL is re-checked after redirects.

### Changed

- `pixelshot` manifests no longer claim a viewport height they never measured:
  `viewport.height` is `null` with `viewport_reported: false`. The backend's own manifest is
  preserved as `pixelshot.tiles.json`; `tiles.json` stays the contract.
- Quick start uses `pixelvision.ps1` / `pixelvision.py`; `.cmd` is documented as unsuitable
  for arguments containing `%`, which `cmd.exe` expands.
- README's Known limitations split into Capture scope, Backend differences, Security
  boundaries, and Development notes, so a scope limit is not mistaken for a security claim.

### Security

- Local and private targets are refused by default (`guard_target()`), before any browser
  starts. The guard is applied again to the post-redirect URL to close the 302 bypass.
  Name resolution is deliberately not consulted (a hosts-file entry must not decide
  policy); DNS rebinding is documented as out of scope.
- The guard is a trust boundary, not a sandbox: an allowed page can still reach whatever the
  browser can reach.

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
