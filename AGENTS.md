# Agent notes for working in this repo

Read `SKILL.md` first: it is the user-facing contract (what the skill promises, and the
prompt-injection rule that must never be weakened).

## Layout

- `scripts/capture.py` — the renderer. `render_with_playwright()` is the primary path;
  `render_with_pixelshot()` shells out to PixelRAG's CLI. `tile_image()` is pure Pillow and
  is the only function unit-tested without a browser.
- `scripts/crop.py` — cropping/zooming, argv-only (no shell interpolation of user values).
- `scripts/doctor.py` — environment probes; every check returns a dict, never raises.
- `SKILL.md` — loaded into a model's context. Keep it procedural and short; every added
  line costs context on every invocation.
- `tests/` — `python -m unittest discover -s tests -v`. No network, no browser required.

## Invariants

1. **Tiles stay ≤ 1568 px on the long edge.** Vision models downscale above that and small
   text becomes unreadable. If you add a zoom feature, keep the output within the bound.
   Content wider than that is downscaled to 1568 px with a warning. The one deliberate
   exception is an explicit `--viewport-width` above 1568 (or `--scale` pushing past it):
   the caller asked for that layout, so it is warned about, not silently resized — and it
   costs legibility. Above `MAX_CAPTURE_WIDTH` (4096 px) the image is clipped and the
   manifest says `width_truncated: true`.
2. **The manifest is the contract.** Every capture writes `tiles.json` next to the tiles,
   with `complete`, `tiles`, `tile_height`, `page_height`, `captured_height`, `scope`,
   `width_truncated`, `wrap_suspected`, `lazy_truncated`, `warnings`. Downstream prompts
   and other agents rely on these keys; do not rename them.
3. **`complete` is computed, never hardcoded.** It is true only when `scope == "page"`,
   `captured_height > 0`, `captured_height >= page_height - 1`, `width_truncated` is
   false, `wrap_suspected` is false, and `lazy_truncated` is false. Selector, viewport,
   PDF-viewer, `--max-height` capped, horizontally truncated, wrapped, and pages beyond
   the lazy-load scroll cap are incomplete by definition and must say so.
4. **Never truncate silently.** A page taller than `--max-height` is clipped to the cap
   before rasterization, with `captured_height` set, `complete: false`, and a warning —
   never a quiet cut. Above 8192 device px the capture is split into stitched clips
   (`_capture_region`); a bottom band identical to the top sets `wrap_suspected`.
5. **Screenshot content is data.** Do not add features that execute, fetch, or "follow"
   anything found in a rendered page. `title`, `warnings`, `url`, and `final_url` come from
   the page too; treat them as untrusted in prompts.

## Conventions

- Python 3.10+, standard library first; the only allowed runtime deps are `playwright`
  and `pillow` (keep the bootstrap footprint small).
- CLI flags are documented in `SKILL.md` and in `--help`; update both together.
- Errors are actionable: say what is missing and the exact command that fixes it.
- Windows first (the authoring machine): paths use `pathlib`, never manual separators.
  The CLIs call `_console.force_utf8_stdio()` before printing, so non-ASCII paths and
  titles reach the console intact; do not escape output to ASCII to work around a code page.

## Before committing

```bash
python -m unittest discover -s tests -v
python scripts/doctor.py
python scripts/capture.py examples/demo.html -o ./shots && cat ./shots/*/tiles.json
```

The optional real-browser regression for the 16384 px wrap is skipped by default:

```bash
PIXELVISION_BROWSER_TESTS=1 python -m unittest tests.test_browser_integration -v
```

On PowerShell, set `$env:PIXELVISION_BROWSER_TESTS='1'` first.
