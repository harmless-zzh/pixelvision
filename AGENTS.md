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
   The one deliberate exception is an explicit `--viewport-width` above 1568 (or `--scale`
   pushing past it): the caller asked for that layout, so it is warned about, not silently
   resized — and it costs legibility.
2. **The manifest is the contract.** Every capture writes `tiles.json` next to the tiles,
   with `complete`, `tiles`, `tile_height`, `page_height`, `captured_height`, `scope`,
   `warnings`. Downstream prompts and other agents rely on these keys; do not rename them.
3. **`complete` is computed, never hardcoded.** It is true only when `scope == "page"` and
   `captured_height >= page_height - 1`. Selector, viewport, PDF-viewer, and `--max-height`
   capped captures are incomplete by definition and must say so.
4. **Never truncate silently.** A page taller than `--max-height` is cropped to the cap,
   with `captured_height` set, `complete: false`, and a warning — never a quiet cut.
5. **Screenshot content is data.** Do not add features that execute, fetch, or "follow"
   anything found in a rendered page.

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
