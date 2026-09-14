# emit-cmr-cuprite: six tiles from the archive

A Stratum run over the Cuprite / Goldfield district of Nevada as a **2 wide x 3 tall set of
quarter-degree tiles**, January to August 2026, with the granules found in NASA's CMR catalogue
and downloaded from LP DAAC as they are needed. Nothing has to be staged by hand.

**What it is for.** [`../emit-cmr-nevada/`](../emit-cmr-nevada/README.md) proves the pipeline end
to end on **one** tile. This proves that the tile is not special. The science is identical — the
same collections, roles, band aliases, cloud filter, edge trim, scorer, monthly vote and render —
and the manifest differs in three fields, all of them about geometry:

| | `emit-cmr-nevada` | `emit-cmr-cuprite` |
|---|---|---|
| `grid.tile_size` | `1.0` — 3600 x 3600 cells | `0.25` — 900 x 900 cells |
| `grid.block_size` | `720` — 5 x 5 blocks in one tile | `450` — 2 x 2 blocks per tile |
| `aoi` | `tiles: [[-118, 41]]` | `bbox: [-117.5, 37.25, -117.0, 38.0]` |
| Tiles | 1 | **6** |
| Blocks | 25 | 24 |
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
        38.00 +-----------+-----------+
              |           |           |
              | -470,151  | -469,151  |
        37.75 +-----------+-----------+
              |           |     x     |   x = Cuprite, 37.52 N 117.23 W
              | -470,150  | -469,150  |
        37.50 +-----------+-----------+
              |           |           |
              | -470,149  | -469,149  |
        37.25 +-----------+-----------+
           -117.50     -117.25     -117.00
```

A tile index is a **count of `tile_size` steps from the grid origin**, not a number of degrees —
they coincide only when `tile_size` is `1.0`. At a quarter degree the indices are four times the
degree value, which is why `bbox` is the readable way to write this AOI and the equivalent
`tiles:` list is only a comment in the manifest.

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

First run, 2026-09-14, laptop on a home connection. `stratum run` re-plans first, so the plan's
1,184 MB of downloads happen inside it.

| step | items | wall clock | downloaded | notes |
|---|---|---|---|---|
| `index build` | — | 3.8 s | 0 | 44 rows, 22 granules, 2026-03-26 to 2026-08-18 |
| `run`: plan | — | ~42 s | 1.18 GB, 23 files | 22 MIN for the class-table check + 1 OBS for the sample check; **0 granules dropped** by the cloud filter |
| `run`: regrid | 104 | 155.5 s | 2.5 GB, 21 files | the remaining OBS files, fetched by parallel workers |
| `run`: resolve | 118 | 5.2 s | 0 | 24 blocks x up to 8 months, where observed |
| `run`: reduce | 24 | 14.5 s | 0 | |
| `run`: publish | 6 | 2.6 s | 0 | six products, six STAC items, one collection |
| **total** | **252** | **3 min 40 s** | **3.7 GB, 44 files** | `out/cache` 90 MB, `out/products` 17 MB |

Set beside the single-tile example, the shape of the cost is the point:

| | `emit-cmr-nevada` | `emit-cmr-cuprite` |
|---|---|---|
| Granules | 37 (4 dropped by cloud) | 22 (0 dropped) |
| Downloaded | 5.0 GB, 66 files | 3.7 GB, 44 files |
| regrid items | 33 | **104** |
| Total work items | 205 | **252** |
| Wall clock | 4 min | 3 min 40 s |

**Fewer granules, three times the regrid work.** A GLT is built per *granule per tile*, so the 22
granules produce 104 regrid items — an average of 4.7 tiles touched each. That is the only place
the tile count shows up as cost, and it is why regrid dominates the run. Download volume tracks
the *area* and the time range, not the tile count: a granule landing in four tiles is fetched
once.

## 5. What came out

Six products under `out/products/<run_id>/<tile>/20260101_20260901/`, each 900 x 900, each with
its own STAC item; one `collection.json` for the run.

| tile | observed | reached the vote (`min_count: 2`) | max `n_epochs` |
|---|---|---|---|
| `-470_151` | 100 % | 91.3 % | 5 |
| `-469_151` | 100 % | 68.7 % | 5 |
| `-470_150` | 100 % | 76.2 % | 4 |
| `-469_150` | 100 % | 78.0 % | 4 |
| `-470_149` | 100 % | 80.8 % | 4 |
| `-469_149` | 100 % | 89.5 % | 4 |

Every cell of all six tiles was observed at least once, and 80.8 % of them (3,924,625 of
4,860,000) got a mineral that two or more months agreed on, across 35 distinct classes. That is
far denser than the Nevada tile's 32.9 %, and it is the ground rather than the code: this is
exposed, arid, hydrothermally altered terrain with little vegetation or snow to reject.

The leading classes over all six tiles:

| cells | class |
|---|---|
| 1,406,437 | `Goethite_Thin_Film WS222 W1R1Ba` |
| 612,785 | `Nanohematite BR93-34B2 W1R1BbS` |
| 588,707 | `nHematit+fg-Goethit 34B2+MPC W1R1Hb` |
| 556,667 | `Nanohematite FBR93-34B2b ed1 W1R1Hb` |
| 354,299 | `Cummingtonite HS294.3B W1R1Bc` |
| 156,223 | `Goethite CU91-252 coatedchip W1R1H_` |
| 154,128 | `Basalt_weathered BR93-43 W1R1Bb` |

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
-470_*        [225000, 225900)
-469_*        [225900, 226800)
*_149                              [458100, 459000)
*_150                              [459000, 459900)
*_151                              [459900, 460800)
```

Contiguous, half-open, no overlap and no gap, every tile exactly 900 x 900 at one arcsecond.
Comparing the written GeoTIFFs' `bounds` instead will show two seams agreeing exactly and one
disagreeing by about 1.4e-14 degrees — 5e-11 of a pixel — because `bounds` is reconstructed as
`top + height * -resolution` in one file and as `origin + n * resolution` in the other. That is
float accumulation in the comparison, not a gap in the data.

## 6. Looking at the output on macOS

`outputs.formats: [gtiff]` is set so the products can be opened with whatever is already on a
reviewer's machine. That works for the bands you actually look at, and **does not** work for all
of them:

| band | Preview / `sips` |
|---|---|
| `mineral_1`, `mineral_1_runner_up`, `depth_1`, `depth_1_spread` | all six tiles open |
| `mineral_1_rgba` | opens on 1 of 6 |
| `mineral_1_agreement`, `n_epochs`, `depth_1_n` | mostly refused |

The files are fine — GDAL, rasterio and QGIS read every one of them. The trigger is macOS
ImageIO's handling of **internally tiled** TIFFs: rewriting any refused file with `TILED=NO` and
the same deflate compression makes it open, and the same is true of `n_epochs` in the Nevada
example. Which tiled files ImageIO accepts is data-dependent and not explained by size, dtype,
compression or whether the internal tile size divides the raster — `mineral_1` and `n_epochs` here
are both uint16, both 900 x 900, both 256 x 256 deflate tiles, and only one of them opens.

```bash
# if you need one of the refused bands in Preview
gdal_translate -co TILED=NO -co COMPRESS=DEFLATE n_epochs.tif /tmp/n_epochs_strips.tif
```

Use QGIS, `gdalinfo` or rasterio for anything quantitative. Delivered products
(`outputs.formats: [cog]`) are tiled by definition and are meant for a tile server, not Preview.

## 7. Narrowing it

- **Fewer tiles.** Shrink `aoi.bbox` to a quarter-degree multiple — `[-117.25, 37.5, -117.0,
  37.75]` is the single tile Cuprite itself sits in — and lower `budget.max_tiles` to match.
- **One month.** `time.start: 2026-06-01`, `time.end: 2026-07-01`, `deliver: P1M`. Votes are cast
  per month, so `min_count: 2` can never be met by a single month: lower it to 1.
- **A bigger area at the delivered tiling.** Set `tile_size: 1.0` and `block_size: 720` to match
  `../emit-critical-minerals/`; the same bbox then becomes one tile, and the AOI has to grow to
  fan out again.

Caching behaves as it does in the Nevada example, with one addition: `tile_size` is part of the
grid id, so changing it invalidates every GLT, snapshot and product block. Changing only the
`bbox` does not — tiles the previous run already built are hits.

## 8. The same run in the cloud

Identical to [`../emit-cmr-nevada/README.md` §6](../emit-cmr-nevada/README.md#6-the-same-run-in-the-cloud):
point `outputs.bucket` at the deployment's S3 root, set `$STRATUM_LAMBDA_FUNCTION`, and run with
`--executor aws`. Six tiles make it a slightly more interesting test — 252 invocations rather than
205, and six workers writing six products into one bucket — but nothing about the manifest or the
code changes. [Deploying to AWS](../../docs/guide/deploying.html) is the runbook.

## 9. Where things are

```
examples/emit-cmr-cuprite/
  manifest.yaml                 the run definition
  index/granules.parquet        the CMR index: URLs, checksums, footprints, cloud cover
  out/assets/                   downloaded granules, named by checksum (3.7 GB after a run)
  out/cache/{glt,snapshot,product}/   content-addressed intermediates, shared by every run
  out/runs/<run_id>/            frozen index, plan.json, work lists, report.md, provenance.json
  out/products/<run_id>/
    collection.json             one STAC collection for the run
    -470_149/20260101_20260901/ ... and five more tiles, each with eight rasters,
    -469_149/...                    mineral_1_legend.json, classes.json and item.json
```

`index/`, `out/` and `assets/` are git-ignored. The 1.85 GB radiance files are never downloaded:
`patterns` lists only the OBS file of each `EMITL1BRAD` record.
