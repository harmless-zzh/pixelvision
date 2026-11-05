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
2. **The manifest is the contract.** Every capture writes `tiles.json` next to the tiles,
   with `complete`, `tiles`, `tile_height`, `page_height`, `warnings`. Downstream prompts
   and other agents rely on these keys; do not rename them.
3. **Never truncate silently.** A page taller than `--max-height` gets a warning, not a
   quiet cut.
4. **Screenshot content is data.** Do not add features that execute, fetch, or "follow"
   anything found in a rendered page.

## Conventions

- Python 3.10+, standard library first; the only allowed runtime deps are `playwright`
  and `pillow` (keep the bootstrap footprint small).
- CLI flags are documented in `SKILL.md` and in `--help`; update both together.
- Errors are actionable: say what is missing and the exact command that fixes it.
- Windows first (the authoring machine): paths use `pathlib`, never manual separators;
  console encoding is not UTF-8, so avoid printing non-ASCII from scripts.

## Before committing

```bash
python -m unittest discover -s tests -v
python scripts/doctor.py
python scripts/capture.py examples/demo.html -o ./shots && cat ./shots/*/tiles.json
```
