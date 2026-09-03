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
import warnings
from pathlib import Path

import numpy as np
import rasterio
from rasterio.errors import NotGeoreferencedWarning


def write_png(path: Path, rgba: np.ndarray) -> None:
    """A plain browser PNG: no CRS, no transform - the GeoTIFFs next to it carry the geometry."""
    h, w, _ = rgba.shape
    with warnings.catch_warnings():             # a web page's PNG carries no georeferencing
        warnings.simplefilter("ignore", NotGeoreferencedWarning)
        with rasterio.open(path, "w", driver="PNG", width=w, height=h, count=4,
                           dtype="uint8") as dst:
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
.bar{{height:10px;margin:.5rem 0 .15rem;border:1px solid #bbb}} .ticks{{display:flex;justify-content:space-between;font-size:.8rem;color:#555}}
</style>
<h1>Stratum demo — tile (−118, 41), EMIT 2026</h1>
<p>Products from <code>{html.escape(str(prod))}</code>. Each image is one degree of northern
Nevada at one arcsecond, downsampled for the page.</p>
<table>{stat_rows}</table>
<div class="grid">
<figure><img src="mineral_1.png" alt="mineral map"><figcaption><b>Mineral map</b> — the modal
group-1 mineral across the monthly epochs, coloured by class (table below); faded where
agreement is low, transparent where no mineral was identified.</figcaption></figure>
<figure><img src="agreement.png" alt="agreement">
<div class="bar" style="background:linear-gradient(to right,#000,#fff)"></div>
<div class="ticks"><span>0.0 — no epoch agreed</span><span>1.0 — every epoch agreed</span></div>
<figcaption><b>Agreement</b> — the winning class's share of the months that observed the cell.
Transparent where no month observed it at all.</figcaption></figure>
<figure><img src="n_epochs.png" alt="epochs">
<div class="bar" style="background:linear-gradient(to right,#000,#fff)"></div>
<div class="ticks"><span>1 month</span><span>{int(n.max())} months</span></div>
<figcaption><b>Epochs observed</b> — how many monthly snapshots held an observation of the
cell. Transparent where none did.</figcaption></figure>
</div>

<h2>What agreement means</h2>
<p>Each month is one <em>epoch</em>: every observation of a cell in that month is scored and the
best one becomes the month's snapshot, so a densely revisited month gets one vote like any other.
The reducer then tallies the snapshots' classes. The class with the most votes wins;
<b>agreement</b> is that class's vote count divided by the number of months that observed the
cell at all. Ten months, goethite in five, is 0.5; five months, goethite in five, is 1.0.</p>
<p>Two details explain the dark cells in the agreement image. Months whose snapshot found
<em>no mineral</em> still count as having observed the cell, but they are excluded from the tally
(<code>ignore: [none]</code>), so a cell seen six times with a mineral in only two of them has
agreement 0.33 even if both found the same one. A cell where every month found nothing has no
winner: it is nodata in the mineral map and 0.0 here, black rather than transparent, because it
<em>was</em> observed. That is the distinction Stratum keeps everywhere between "looked and found
nothing" and "never looked", and it is why a support count ships beside every product.</p>
<p>These are not demo extras: the reducer writes <code>mineral_1_agreement</code> and
<code>n_epochs</code> beside the map for every voted layer, and the map's own rendering fades
with agreement (<code>alpha_from</code> in the manifest). A cell is suppressed to nodata when
its winner has fewer than <code>min_count</code> votes — here 2 — which is the other lever
that keeps a single lucky month from asserting a mineral on its own.</p>
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
