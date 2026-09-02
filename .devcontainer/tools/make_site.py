"""Render one published product directory as a small static page.

Usage: python make_site.py <product_dir> <site_dir>

Reads the delivered rasters (the mineral map's RGBA rendering, the agreement band
and the epoch count), writes them as PNGs, and writes an index.html with the
legend and the class tally. Only the demo uses this; the products themselves are
the deliverable and this page is a convenience for looking at them in a browser.
"""
from __future__ import annotations

import html
import json
import sys
from pathlib import Path

import numpy as np
import rasterio


def write_png(path: Path, rgba: np.ndarray) -> None:
    h, w, _ = rgba.shape
    with rasterio.open(path, "w", driver="PNG", width=w, height=h, count=4, dtype="uint8") as dst:
        dst.write(np.moveaxis(rgba, -1, 0))


def ramp(values: np.ndarray, lo: float, hi: float, nodata_mask: np.ndarray) -> np.ndarray:
    """Grey ramp to RGBA; nodata transparent."""
    t = np.clip((values.astype("float64") - lo) / (hi - lo), 0, 1)
    g = (255 * t).astype("uint8")
    rgba = np.dstack([g, g, g, np.full_like(g, 255)])
    rgba[nodata_mask] = 0
    return rgba


def downsample(a: np.ndarray, target: int = 900) -> np.ndarray:
    step = max(1, a.shape[0] // target)
    return a[::step, ::step] if a.ndim == 2 else a[::step, ::step, :]


def main(prod: Path, site: Path) -> None:
    site.mkdir(parents=True, exist_ok=True)
    # The rendered mineral map, as published.
    with rasterio.open(prod / "mineral_1_rgba.tif") as src:
        rgba = np.moveaxis(src.read(), 0, -1)
    write_png(site / "mineral_1.png", downsample(rgba))

    with rasterio.open(prod / "mineral_1.tif") as src:
        cls = src.read(1)
        cls_nodata = src.nodata if src.nodata is not None else 65535
    valid = cls != cls_nodata

    with rasterio.open(prod / "mineral_1_agreement.tif") as src:
        agr = src.read(1)
    write_png(site / "agreement.png", downsample(ramp(np.nan_to_num(agr), 0.0, 1.0, np.isnan(agr))))

    with rasterio.open(prod / "n_epochs.tif") as src:
        n = src.read(1)
    write_png(site / "n_epochs.png", downsample(ramp(n, 0, max(1, int(n.max())), n == 0)))

    legend = json.loads((prod / "mineral_1_legend.json").read_text())
    names = {e["id"]: e for e in legend["entries"]}
    ids, counts = np.unique(cls[valid], return_counts=True)
    order = np.argsort(counts)[::-1][:15]
    total = int(valid.sum())
    rows = []
    for i in order:
        e = names.get(int(ids[i]), {"name": f"class {ids[i]}", "color": [128, 128, 128]})
        r, g, b = e["color"][:3]
        rows.append(
            f"<tr><td><span class='sw' style='background:rgb({r},{g},{b})'></span></td>"
            f"<td>{html.escape(e['name'])}</td><td class='n'>{counts[i]:,}</td>"
            f"<td class='n'>{100 * counts[i] / total:.1f}%</td></tr>"
        )
    item = json.loads((prod / "item.json").read_text())
    props = item.get("properties", {})
    stats = {
        "cells with a mineral": f"{total:,} of {cls.size:,} ({100 * total / cls.size:.1f}%)",
        "epochs observed per cell": f"{int(n[n > 0].min()) if (n > 0).any() else 0} to {int(n.max())}",
        "mean agreement where valid": f"{float(np.nanmean(agr[valid])):.2f}" if total else "n/a",
        "period": f"{props.get('start_datetime', '?')[:10]} to {props.get('end_datetime', '?')[:10]}",
    }
    stat_rows = "".join(f"<tr><th>{html.escape(k)}</th><td>{html.escape(v)}</td></tr>" for k, v in stats.items())

    page = f"""<!doctype html><meta charset="utf-8"><title>Stratum demo results</title>
<style>
body{{font:15px/1.45 system-ui,sans-serif;margin:2rem auto;max-width:1100px;padding:0 1rem;color:#1b1b1b;background:#fafafa}}
h1{{font-weight:600}} h2{{font-weight:600;margin-top:2rem}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:1.5rem}}
figure{{margin:0}} img{{width:100%;image-rendering:pixelated;background:#fff;border:1px solid #ddd}}
figcaption{{font-size:.9rem;color:#555;margin-top:.4rem}}
table{{border-collapse:collapse}} td,th{{padding:.25rem .75rem;text-align:left;border-bottom:1px solid #e5e5e5}}
td.n{{text-align:right;font-variant-numeric:tabular-nums}} .sw{{display:inline-block;width:1em;height:1em;border:1px solid #999;vertical-align:middle}}
code{{background:#eee;padding:.1em .3em;border-radius:3px}}
</style>
<h1>Stratum demo — tile (−118, 41), EMIT 2026</h1>
<p>Products from <code>{html.escape(str(prod))}</code>. Each image is one degree of northern
Nevada at one arcsecond, downsampled for the page.</p>
<table>{stat_rows}</table>
<div class="grid">
<figure><img src="mineral_1.png" alt="mineral map"><figcaption><b>Mineral map</b> — the modal
group-1 mineral across the monthly epochs; faded where agreement is low, transparent where no
mineral was identified.</figcaption></figure>
<figure><img src="agreement.png" alt="agreement"><figcaption><b>Agreement</b> — the winning
class's share of the epochs that observed the cell (white = 1.0).</figcaption></figure>
<figure><img src="n_epochs.png" alt="epochs"><figcaption><b>Epochs observed</b> — how many
monthly snapshots had an observation of the cell (white = most).</figcaption></figure>
</div>
<h2>Classes present</h2>
<p>Top 15 by cell count; names are the granules' own class table (<code>classes.json</code>).</p>
<table><tr><th></th><th>class</th><th>cells</th><th>share</th></tr>{''.join(rows)}</table>
<h2>Files</h2>
<p>The delivered rasters, legend, class table and STAC item are in the product directory above;
the run report and provenance record are under <code>out/runs/</code>.</p>
"""
    (site / "index.html").write_text(page)
    print(f"[site] wrote {site}/index.html and three PNGs")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(Path(sys.argv[1]), Path(sys.argv[2]))
