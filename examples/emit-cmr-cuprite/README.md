# emit-cmr-cuprite: six tiles from the archive

A Stratum run over the Cuprite / Goldfield district of Nevada as a **2 wide x 3 tall set of
half-degree tiles**, January to August 2026, with the granules found in NASA's CMR catalogue
and downloaded from LP DAAC as they are needed. Nothing has to be staged by hand.

**What it is for.** [`../emit-cmr-nevada/`](../emit-cmr-nevada/README.md) proves the pipeline end
to end on **one** tile. This proves that the tile is not special. The science is identical — the
same collections, roles, band aliases, cloud filter, edge trim, scorer, monthly vote and render —
and the manifest differs in three fields, all of them about geometry:

| | `emit-cmr-nevada` | `emit-cmr-cuprite` |
|---|---|---|
| `grid.tile_size` | `1.0` — 3600 x 3600 cells | `0.5` — 1800 x 1800 cells |
| `grid.block_size` | `720` — 5 x 5 blocks in one tile | `450` — 4 x 4 blocks per tile |
| `aoi` | `tiles: [[-118, 41]]` | `bbox: [-118.0, 37.0, -117.0, 38.5]` |
| Tiles | 1 | **6** |
| Blocks | 25 | **96** |
| Products | 1 | **6** |

Everything else is copied. If a result differs, it is because the ground differs.

Cuprite is the reference target of imaging spectroscopy — a long-standing airborne calibration and
validation site, and the scene most mineral-mapping algorithms have been checked against — so it is
a useful place to look at the output and have some idea what should be there.

All commands run from the repository root through pixi.

## 1. Credentials

Identical to the Nevada example: an Earthdata Login account and either a `~/.netrc` line or
`EARTHDATA_USERNAME` / `EARTHDATA_PASSWORD`. The walk-through, including how to check the login
before spending bandwidth, is
[`../emit-cmr-nevada/README.md` §1](../emit-cmr-nevada/README.md#1-credentials). Building the index
needs no login at all — a CMR search is anonymous.

## 2. The tiling

`aoi.bbox` is resolved to **every tile whose nominal bounds intersect the box**, ordered south to
north then west to east. The box sits exactly on tile edges, so it produces a clean 2 x 3 set and
pulls in no seventh tile:

```
        38.50 +-----------+-----------+
              |           |           |
              | -236,76   | -235,76   |
        38.00 +-----------+-----------+
              |     x     |           |   x = Cuprite, 37.52 N 117.23 W
              | -236,75   | -235,75   |
        37.50 +-----------+-----------+
              |           |           |
              | -236,74   | -235,74   |
        37.00 +-----------+-----------+
           -118.00     -117.50     -117.00
```

**The folder names are tile indices, not degrees, and this is the one thing about this example
that reliably surprises people.** A tile index is a *count of `tile_size` steps from the grid
origin*; it equals the degree value only when `tile_size` is `1.0`. At a half degree the indices
are doubled, so the directory `-236_75` holds lon [-118.0, -117.5), lat [37.5, 38.0) — not
anything at -236. Nevada's `-118_41` only looks like coordinates because its tiles are one degree.
That is why `bbox` is the readable way to write this AOI and the equivalent `tiles:` list is left
as a comment. `item.json` in every product directory carries the real bbox.

Two properties matter and both are worth checking in the output:

- **Tiles are cut on the cell lattice, not on a lattice of their own.** `resolution` and `origin`
  are unchanged from the Nevada example, so changing `tile_size` re-cuts the *same* cells into
  different rectangles. No pixel moves, no tile straddles a cell, and neighbouring tiles neither
  overlap nor gap.
- **A granule contributes to every tile it touches.** The six tiles share one asset cache and one
  GLT cache, so a granule is downloaded once no matter how many tiles it lands in — but it is
  regridded once *per tile*, which is why the regrid item count is much higher here than the
  granule count.

## 3. Commands

```bash
# 1. metadata only, a few seconds, no login
pixi run stratum index build -m examples/emit-cmr-cuprite/manifest.yaml

# 2. select, validate against real granules, write the work lists
pixi run stratum plan -m examples/emit-cmr-cuprite/manifest.yaml
cat examples/emit-cmr-cuprite/out/runs/emit-cmr-cuprite-*/report.md

# 3. the run (re-plans first; the plan's downloads are cache hits)
pixi run stratum run -m examples/emit-cmr-cuprite/manifest.yaml

# progress and the report, at any time
pixi run stratum status --run emit-cmr-cuprite-<hash> --root examples/emit-cmr-cuprite/out
pixi run stratum report --run emit-cmr-cuprite-<hash> --root examples/emit-cmr-cuprite/out
```

## 4. Sizes and times measured

Run 2026-09-14, laptop on a home connection. `stratum run` re-plans first, so the plan's downloads
happen inside it. **3.7 GB of the granules were already staged from an earlier run over a smaller
box**, so the wall clock below is not a cold start; a first run downloads about 6.4 GB and takes
proportionally longer in regrid.

| step | items | wall clock | notes |
|---|---|---|---|
| `index build` | — | 4 s | 90 rows, 45 granules, no login, nothing downloaded |
| `run`: plan | — | ~65 s | 42 assets, 2,115 MB staged; **4 granules dropped** by the cloud filter, 41 survive |
| `run`: regrid | 146 | 97.8 s | one GLT per granule per tile |
| `run`: resolve | 471 | 9.5 s | 96 blocks x up to 8 months, where observed |
| `run`: reduce | 96 | 35.9 s | |
| `run`: publish | 6 | 4.2 s | six products, six STAC items, one collection |
| **total** | **719** | **3 min 33 s** | `out/assets` 6.4 GB / 82 files, `out/cache` 345 MB, `out/products` 47 MB |

Set beside the single-tile example:

| | `emit-cmr-nevada` | `emit-cmr-cuprite` |
|---|---|---|
| Tiles | 1 x 3600² | **6 x 1800²** |
| Blocks | 25 | **96** |
| Granules | 37, 4 dropped by cloud | 45, 4 dropped by cloud |
| Downloaded | 5.0 GB, 66 files | 6.4 GB, 82 files |
| regrid items | 33 | **146** |
| Total work items | 205 | **719** |

**Similar granule count, four and a half times the regrid work.** A GLT is built per *granule per
tile*, so 41 surviving granules produce 146 regrid items — 3.6 tiles touched each. That is the
only place the tile count shows up as cost. Download volume tracks the *area* and the time range,
not the tile count: a granule landing in four tiles is fetched once.

Why not one-degree tiles, to match the delivered product? Because a 2 x 3 set of them is a
2 x 3 degree box, which CMR answers with **89 granules** and roughly 15 GB — measured, not
guessed. Half-degree tiles keep the fan-out and a fifth of the bytes.

## 5. What came out

Six products under `out/products/<run_id>/<tile>/20260101_20260901/`, each 1800 x 1800, each with
its own STAC item; one `collection.json` for the run.

| tile | observed | reached the vote (`min_count: 2`) | max `n_epochs` |
|---|---|---|---|
| `-236_76` | 100 % | 61.9 % | 4 |
| `-235_76` | 100 % | 70.3 % | 5 |
| `-236_75` | 100 % | 60.1 % | 5 |
| `-235_75` | 100 % | 78.5 % | 5 |
| `-236_74` | 100 % | **31.1 %** | 5 |
| `-235_74` | 100 % | 72.4 % | 4 |

Every cell of all six was observed at least once, and 62.4 % (12,131,396 of 19,440,000) got a
mineral two or more months agreed on, across 49 classes — against 32.9 % on the Nevada tile. The
spread between tiles is the useful part: `-236_74` reaches only 31.1 % while `-235_75`, one tile
east and one north, reaches 78.5 %. That is ground, not machinery, and it is exactly the kind of
thing a single-tile example cannot show you.

The leading classes over all six tiles:

| cells | class |
|---|---|
| 4,013,583 | `Goethite_Thin_Film WS222 W1R1Ba` |
| 2,389,703 | `Cummingtonite HS294.3B W1R1Bc` |
| 1,335,981 | `Nanohematite BR93-34B2 W1R1BbS` |
| 1,334,149 | `nHematit+fg-Goethit 34B2+MPC W1R1Hb` |
| 1,331,182 | `Nanohematite FBR93-34B2b ed1 W1R1Hb` |
| 987,390 | `Basalt_weathered BR93-43 W1R1Bb` |

> **Iron, not alunite.** If you know Cuprite from the AVIRIS literature you are expecting alunite,
> kaolinite and buddingtonite. They are not here, and nothing is wrong: this manifest reads
> `group_1_mineral_id`, and in the granules' own class table every Alunite (20 entries), Kaolinite
> (2) and Buddingtonite (1) sits in **group 2** — a separate variable this manifest does not read.
> Group 1 holds 95 entries, and what it finds at Cuprite is iron oxides and oxyhydroxides, which
> is the expected answer for the band it covers. Reading group 2 as well means adding a role and a
> layer; nothing else changes.

### The tiles line up

Checked on the cell lattice rather than on floating-point bounds, which is the only check that
means anything:

```
tile          columns              rows
-236_*        [223200, 225000)
-235_*        [225000, 226800)
*_74                               [457200, 459000)
*_75                               [459000, 460800)
*_76                               [460800, 462600)
```

Contiguous, half-open, no overlap and no gap, every tile exactly 1800 x 1800 at one arcsecond.
Comparing the written GeoTIFFs' `bounds` instead can show a seam disagreeing by ~1e-14 degrees,
because `bounds` is reconstructed as `top + height * -resolution` in one file and as
`origin + n * resolution` in the other. That is float accumulation in the comparison, not a gap in
the data.

## 6. Looking at the output

`outputs.formats: [gtiff]` is set so the products open with whatever is already on a reviewer's
machine, and every band does — all 48 files, in Preview, QGIS, GDAL and rasterio alike.

That took a fix, and the reason is worth knowing because it is easy to misdiagnose. **A plain
GeoTIFF and an internally tiled one are not opposites**: "COG" means header-first plus an overview
pyramid, but a GeoTIFF can be cut into internal tiles without being a COG at all. Stratum used to
write `gtiff` that way, and macOS ImageIO refuses *some* internally tiled TIFFs —
data-dependently, so `mineral_1` would open and `n_epochs` beside it would not, at every size
tried — 900 x 900, 1800 x 1800 and 3600 x 3600 alike. It looked like the files were broken or the products were COGs; they were
neither. `gtiff` now writes **strips**, which ImageIO reads in every case, for about 3 % more
bytes.

`cog` is unaffected and still tiled — a range-reading tile server is the entire point of that
format, and `formats: [cog]` is what a delivered product uses.

| | `gtiff` (this example) | `cog` (delivered products) |
|---|---|---|
| Internal layout | strips | 256- or 400-cell tiles |
| Overviews | none | built, decimated for categorical bands |
| Opens in Preview | yes | no (paletted bands) |
| STAC media type | `image/tiff; application=geotiff` | `…; profile=cloud-optimized` |

Use QGIS, `gdalinfo` or rasterio for anything quantitative — Preview will show you the picture,
not the values.

## 7. Narrowing it

- **Fewer tiles.** Shrink `aoi.bbox` to a half-degree multiple — `[-118.0, 37.5, -117.5, 38.0]`
  is the single tile Cuprite itself sits in — and lower `budget.max_tiles` to match.
- **One month.** `time.start: 2026-06-01`, `time.end: 2026-07-01`, `deliver: P1M`. Votes are cast
  per month, so `min_count: 2` can never be met by a single month: lower it to 1.
- **The delivered tiling.** Set `tile_size: 1.0` and `block_size: 720` to match
  `../emit-critical-minerals/`. The tile folders then read as degrees (`-118_37`), which is worth
  something; the same 2 x 3 set becomes a 2 x 3 degree box, which CMR answers with 89 granules and
  about 15 GB. Raise `budget.max_granules` from 100 if you do.

Caching behaves as it does in the Nevada example, with one addition: `tile_size` is part of the
grid id, so changing it invalidates every GLT, snapshot and product block. Changing only the
`bbox` does not — tiles the previous run already built are hits.

## 8. The second manifest: bare, well-lit ground wins

`manifest-bare-earth.yaml` runs the same grid over **three times the ground** &mdash; a 3 x 6 set,
18 tiles &mdash; and changes how a cell picks its winner. It exists to exercise the two input paths
the baseline does not use, and it shares this directory's `index/` and `out/`, so every GLT and
every downloaded granule is reused.

| | baseline `manifest.yaml` | `manifest-bare-earth.yaml` |
|---|---|---|
| area | 2 x 3 tiles | **3 x 6 tiles** (the baseline is its bottom-left corner) |
| scorer | `min_view_zenith` &mdash; closest to nadir | `prefer_bare_earth` &mdash; most bare soil, then best lit |
| extra input | &mdash; | `frcov`, an **ortho-native role** (L2B FRCOV GeoTIFFs) |
| extra input | &mdash; | `landcover`, an **aux source** (ESA WorldCover, four tiles mosaicked) |
| masks | `edge_trim` | `edge_trim` + `landcover` |

**The two mechanisms, one sentence each.** An *ortho-native role* is a product already on a map
grid: no sensor space, no GLT, so the framework warps it straight onto the block
([12 §2](../../docs/specs/12-data-access.md)). *Aux* is data that is not an observation at all:
declared by URI, addressed by alias, staged and warped at plan time
([05](../../docs/specs/05-ancillary-data.md)). See
[Authoring a manifest](../../docs/guide/manifests.html) for how the pieces connect.

```bash
pixi run stratum index build -m examples/emit-cmr-cuprite/manifest-bare-earth.yaml  # rebuild: +FRCOV, wider box
pixi run stratum run         -m examples/emit-cmr-cuprite/manifest-bare-earth.yaml
```

The index rebuild is not optional and the planner says so: adding a collection or widening the box
widens the index's scope, and a run refuses an index built for less.

```
wrote index/granules.parquet: 197 row(s), 69 granule(s)
  EMITL1BRAD     69 row(s)   EMITL2BFRCOV  59 row(s)   EMITL2BMIN  69 row(s)
```

**197 rows, 69 granules** &mdash; that gap is the id merge working. The FRCOV records carry the same
granule ids as the MIN and OBS records, so they fold into one record each rather than becoming
separate granules.

### The aux source is four files, not one

ESA WorldCover ships as 3&deg; x 3&deg; tiles. The baseline's 2 x 3 box fitted inside one of them;
this one does not, so the manifest names four and they are composited later-over-earlier, each
contributing only where it has data. A tile that does not reach the AOI is skipped without being
read, so naming a spare costs nothing. **The order is part of the source's identity** &mdash;
reorder them and it is a different artifact under a different key.

### What it cost

| step | wall clock | notes |
|---|---|---|
| `index build` | 6 s | 197 rows, 69 granules |
| `plan` | ~50 s | stages four WorldCover tiles, digests them, warps all 18 |
| `run` | **3 min 33 s** | 1,817 work items, **713 of them cache hits** |

```
stage    items  hits  seconds
regrid   327    146   117.15     <- the 2x3 run's GLTs, reused
resolve  1184   471    63.58     <- and its snapshots
reduce   288     96    26.57
publish   18      0     5.78
total   1817    713   213.08
```

Tripling the area cost only the new tiles: **every artifact from the 2 x 3 run was reused**, because
a GLT is keyed on its granule and grid and a snapshot on its own block's window &mdash; neither
mentions the AOI. Downloads grew far less than area, 41 to 59 usable granules, because an EMIT
footprint is about 0.7&deg; and already spans several 0.5&deg; tiles.

A later run that changed **only the aux source** (one WorldCover tile to four) took 80 s: all 327
GLTs and every FRCOV warp were hits, and only the snapshots downstream of the landcover raster
were rebuilt.

Disk after both runs: `assets` 10 GB, `cache` 2.4 GB, `products` 194 MB.

### What came out &mdash; and a finding worth carrying

| | observed in &ge;1 month | received a vote | classes |
|---|---|---|---|
| baseline `min_view_zenith`, 2 x 3 | 100.0 % | **62.4 %** | 49 |
| bare earth, the same 2 x 3 tiles | 43.2 % | **19.7 %** | 35 |
| bare earth, all 3 x 6 | 29.1 % | **8.9 %** | 36 |

The middle row is measured over the six tiles the two runs share, so it is the like-for-like
comparison. It is also **bit-identical to the standalone 2 x 3 run** &mdash; widening the AOI and
switching the aux source to a four-tile mosaic changed nothing on ground both covered, which is
tile independence and correct compositing in one check.

Coverage falls by three times at the recommended thresholds, and **the loss is at the observation
level, not the vote**: with `hard_floor: 0.65` only 43 % of cells have *any* admissible
observation, averaging 0.89 months against the baseline's 3.60. `min_count: 2` then takes what is
left from 43 % to 30 %. The northern tiles are worse again &mdash; bare-soil fraction falls from a
median of about 0.55 at Cuprite to 0.22 at 39.5&deg; N &mdash; which is what drags the 18-tile
figure down to 8.9 %.

Measured over the warped FRCOV for the original AOI:

```
bare-soil fraction, 1.5 M covered cells
  p10 0.216   p25 0.368   p50 0.531   p75 0.678   p90 0.786
  >= 0.65:  29.6 %        >= 0.80:   8.5 %
```

**The median bare-soil fraction over Cuprite is 0.53** &mdash; at one of the most exposed, least
vegetated mineral sites on Earth. That is the caveat from the Mines tag-up showing up in numbers:
the current FRCOV is "NPV false-positive happy" and assigns genuinely bare desert to the
non-photosynthetic-vegetation endmember, so a 0.65 floor on the *bare* fraction alone discards two
thirds of the scene. At `hard_floor: 0.35` / `min_soil: 0.55` the 2 x 3 set recovers to 45.6 %.

The manifest ships the recommended 0.65 / 0.80 rather than the values that make the picture look
better, because those are what the science team recommended and tuning them here would hide the
finding. **Worth putting to Phil and Thomas before any delivered run:** whether the cutoff should
be lower for this product, or whether it belongs on bare + NPV together rather than on bare alone.

The thresholds are `scorer.params`, so changing them re-runs resolve, reduce and publish and
touches neither the GLTs nor the warps.

### What the cloud filter does, and does not

`granule_filter: [{max_cloud_fraction: 0.8}]` drops a granule whose **scene-level** cloud cover
exceeds 80 %, at plan time, before a byte is read. It is cost control, not quality control: an
85 %-cloudy scene may still hold the only clear look at some cell.

Here it happens to cost nothing. The 10 granules it drops have cloud fractions 0.83 to 0.97, and
they are *exactly* the 10 for which no FRCOV was published &mdash; the upstream producer gives up on
the same scenes. That is luck rather than design, and it is not true of the baseline, where those
10 do carry mineral data.

**Nothing here masks cloud per pixel.** The mask for that (`l2a_standard`, reading the L2A MASK
product's cloud, cirrus, water and spacecraft flags) is built but not used by either manifest;
adding it means a fourth collection, about 3 GB, and a cross-version join, since the mask is
published at v002 while the mineral product is v001. [observed] It matters more than it looks:
sampled over the 59 staged granules, the mineral product writes **no fill at all**, and even at
60-80 % cloud still returns a mineral identification for 39 % of pixels. Unmasked cloud does not
produce absence, it produces plausible-looking identifications.

## 9. The same run in the cloud

Identical to [`../emit-cmr-nevada/README.md` §6](../emit-cmr-nevada/README.md#6-the-same-run-in-the-cloud):
point `outputs.bucket` at the deployment's S3 root, set `$STRATUM_LAMBDA_FUNCTION`, and run with
`--executor aws`. Six tiles make it a considerably more interesting test — **719 invocations**
rather than 205, and six workers writing six products into one bucket — but nothing about the
manifest or the code changes. It is also the first case where fan-out concurrency matters: 719
items at the default `--workers 32` is the scale at which the open question about exhausting
account Lambda concurrency starts to bite. [Deploying to AWS](../../docs/guide/deploying.html) is the runbook.

## 10. Where things are

```
examples/emit-cmr-cuprite/
  manifest.yaml                 the run definition
  index/granules.parquet        the CMR index: URLs, checksums, footprints, cloud cover
  out/assets/                   downloaded granules, named by checksum (6.4 GB after a run)
  out/cache/{glt,snapshot,product}/   content-addressed intermediates, shared by every run
  out/runs/<run_id>/            frozen index, plan.json, work lists, report.md, provenance.json
  out/products/<run_id>/
    collection.json             one STAC collection for the run
    -236_74/20260101_20260901/  ... and five more tiles, each with eight rasters,
    -235_74/...                     mineral_1_legend.json, classes.json and item.json
```

`index/`, `out/` and `assets/` are git-ignored. The 1.85 GB radiance files are never downloaded:
`patterns` lists only the OBS file of each `EMITL1BRAD` record.
