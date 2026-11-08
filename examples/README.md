# Examples

## demo.html — the page you cannot read as text

A static operations dashboard: KPI cards, a canvas bar+line chart, and a service table.
There is no data in the HTML for the chart — the numbers live in a `<script>` array and are
painted onto a canvas. A text extractor sees an empty `<canvas>` element and a table.
A reader sees the trend and the orange failover week.

```bash
python scripts/capture.py examples/demo.html -o ./shots --viewport-width 1100 --allow-file
# then read shots/demo.html.png.tiles/tiles.json and tile_0000.jpg
```

Local files need `--allow-file`: `file://` targets are refused by default (see the trust
boundary section of the main README).

The image at `docs/demo.png` in the repository root is this page captured and cropped.

## What to look for when you read it

1. Four KPI values and their deltas (1,284 / 842 ms / 37 / $4.18).
2. Week 37 is the orange bar — the only week whose throughput dropped.
3. `render-worker` is degraded and `scheduler` is in incident; error rates differ by 30×.
4. The footnote names the incident id (OPS-2291).

All four are visible only in the pixels. That is the point of the skill.
