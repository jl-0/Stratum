# nevada-cmr: one tile from the archive

A Stratum run over one degree of northern Nevada (tile `-118, 41`), January to August 2026,
with the granules found in NASA's CMR catalogue and downloaded from LP DAAC as they are needed.
Nothing has to be staged by hand. `manifest.yaml` is annotated line by line; this page is what
happens when you run it, and what it costs.

All commands run from the repository root through pixi.

## 1. Credentials

EMIT data sits behind Earthdata Login. You need a free account at
<https://urs.earthdata.nasa.gov>, then one of:

- a `~/.netrc` line, permissions `600`:

  ```
  machine urs.earthdata.nasa.gov login <username> password <password>
  ```

  ```bash
  chmod 600 ~/.netrc
  ```

- the environment variables `EARTHDATA_USERNAME` and `EARTHDATA_PASSWORD`;
- or let `earthaccess` write the `.netrc` for you, interactively:

  ```bash
  pixi run python -c "import earthaccess; earthaccess.login(strategy='interactive', persist=True)"
  ```

Stratum tries `.netrc` first, then the environment. Check that the login works before
spending bandwidth:

```bash
pixi run python -c "import earthaccess; a = earthaccess.login(strategy='netrc'); print(a.authenticated)"
```

Stratum never stores or logs the credential. It is read by `earthaccess` when the first file is
downloaded; it is not written to the run directory, the plan, the provenance record or any
report, and an authentication failure names the strategies it tried, never the values.
Building the index (step 1 below) needs no login at all: a CMR search is anonymous.

## 2. What happens

1. **Index build** queries CMR for the two collections named in `inputs.source.patterns`
   (`EMITL2BMIN` and `EMITL1BRAD`), restricted to the tile's box and the manifest's time
   range, and writes `index/granules.parquet`: one row per granule per collection with its
   footprint, acquisition time, cloud cover, build version, the HTTPS URL of each matching
   file and its SHA-512 checksum. Files that match no pattern (the 1.85 GB radiance file, the
   browse PNGs) are not indexed. No pixel is downloaded.
2. **Plan** reads the index, selects the granules that meet the tile and the time range,
   applies the cloud filter (`max_cloud_fraction: 0.8`), and freezes the survivors into
   `out/runs/<run_id>/index.parquet` so the run can be reproduced. It then opens real granules
   through the asset store to validate the manifest against the data: one OBS file to check
   the geometry role and band aliases, and every surviving MIN file to read the embedded class
   table and check that all granules carry the same one (the vintage check). Those files are
   downloaded now, verified against their checksums, and kept in `out/assets/`, where the run
   finds them again. Plan writes the work lists and `report.md`; nothing else is fetched.
3. **Regrid** downloads each granule's OBS file on first touch into `out/assets/` (verified
   against its checksum), builds one geometry lookup table per granule for the tile, and caches
   it under `out/cache/glt/`.
4. **Resolve** opens the MIN files the same way (cache hits after the plan), trims 7 detector
   columns at each edge, scores every observation by its view zenith, and writes one snapshot
   per month per block under `out/cache/snapshot/`.
5. **Reduce** votes across the months for each block (`min_count: 2`) and takes the median
   depth of the months that agreed, under `out/cache/product/`.
6. **Publish** stitches the blocks and writes the rasters, the RGBA render, the legend, the
   class table and the STAC item under `out/products/<run_id>/-118_41/20260101_20260901/`,
   plus `provenance.json` in the run directory.

## 3. Commands

```bash
# 1. metadata only, a few seconds
pixi run stratum index build -m examples/nevada-cmr/manifest.yaml

# 2. select, validate against real granules, write the work lists
pixi run stratum plan -m examples/nevada-cmr/manifest.yaml
cat examples/nevada-cmr/out/runs/nevada-cmr-*/report.md      # read this before running

# 3. the run (re-plans first; the plan's downloads are cache hits)
pixi run stratum run -m examples/nevada-cmr/manifest.yaml

# progress and the report, at any time
pixi run stratum status --run nevada-cmr-<hash> --root examples/nevada-cmr/out
pixi run stratum report --run nevada-cmr-<hash> --root examples/nevada-cmr/out

# run it again: everything is a cache hit
pixi run stratum run -m examples/nevada-cmr/manifest.yaml
```

`stratum run` builds the index itself when `index/granules.parquet` is absent, so step 1 is
optional; it is worth running on its own the first time to see what the catalogue holds.
`report.md` says how many granules survived each filter, which software builds they carry,
the class-table fingerprint, how many assets the plan staged and how much work the run is.
Reading it costs nothing more than the plan already spent.

The asset cache defaults to `out/assets/` under the manifest's `outputs.bucket`; override it
with `--asset-cache` on `stratum run` or the `STRATUM_ASSET_CACHE` environment variable when
several runs should share one download directory.

## 4. Sizes and times measured

First run, 2026-09-02, laptop on a home connection:

| step | wall clock | downloaded | notes |
|---|---|---|---|
| `index build` | 4 s | 0 | 74 rows, 37 granules, Feb 10 to Aug 19 2026 |
| `plan` | 93 s | 1.55 GB, 34 files | 1 OBS + 33 MIN; 4 granules dropped by the cloud filter (89 to 98 % cloud) |
| `run`: plan again | 3 s | 0 | cache hits |
| `run`: regrid | 99 s | 3.5 GB, 32 files | the remaining OBS files, fetched by parallel workers; 33 GLTs |
| `run`: resolve | 6 s | 0 | 146 snapshots (25 blocks x up to 8 months) |
| `run`: reduce | 21 s | 0 | 25 product blocks |
| `run`: publish | 4 s | 0 | |
| **total** | **about 4 min** | **5.0 GB, 66 files** | `du -sh out/assets`; `out/cache` 241 MB, `out/products` 19 MB |

Second `stratum run` with nothing changed: **14 s**, every regrid, resolve and reduce item a
cache hit; publish always rewrites its 19 MB.

The product: a 3600 x 3600 grid in which every cell was observed in 3 to 6 of the 8 months and
32.9 % of cells received a mineral vote that met `min_count: 2` (the rest are `none` or a
one-month singleton); the mean agreement over those cells is 0.60. The most common classes are
cummingtonite, butlerite, weathered basalt and the nano-hematite group, the same leaders as the
local June-only trial in `examples/trial-nevada/`. Of that trial's 14 June granules, CMR
returns 12: the other two touch the tile only with their bounding box, not with their footprint
polygon, which is what CMR searches on.

## 5. Narrowing it

- **One month.** Set `time.start: 2026-06-01`, `time.end: 2026-07-01`, `deliver: P1M`: up to 12
  granules before the cloud filter, about 1.8 GB. Votes are cast per month, so a one-month run has one vote per cell
  and `min_count: 2` can never be met: lower it to 1.
- **A different tile.** Replace `aoi.tiles` with `bbox: [w, s, e, n]` or with
  `zones: [name]` plus `registry: ../zones.yaml` (both are commented in the manifest). Keep
  `budget.max_tiles` in step with the number of one-degree tiles the box covers.
- **After a manifest change.** The run id is the label plus a hash of the manifest, so a
  change gets a new `out/runs/` and `out/products/` directory. The index records the AOI box,
  time range and collections it was built for; it is reused while the manifest stays inside
  that scope, and a wider `time`, `aoi` or a new collection makes `stratum plan` refuse until
  you rebuild it (`stratum index build`, or delete `index/`). `stratum index build --since
  <datetime>` refreshes an index in place: rows CMR revised since then replace their earlier
  versions, everything else is kept.
  Downloaded assets are reused whatever changes; the GLTs are reused unless the grid changes;
  snapshots are reused unless the scorer, masks, class handling or time epochs change. Changing
  only the vote (`min_count`, `tie_break`) or the render re-runs reduce and publish, about half
  a minute here.

## 6. Where things are

```
examples/nevada-cmr/
  manifest.yaml                 the run definition
  index/granules.parquet        the CMR index: URLs, checksums, footprints, cloud cover
  out/assets/                   downloaded granules, named by checksum; delete to re-download
  out/cache/{glt,snapshot,product}/   content-addressed intermediates, shared by every run
  out/runs/<run_id>/            frozen index, plan.json, work lists, report.md, provenance.json
  out/products/<run_id>/-118_41/20260101_20260901/
                                mineral_1.tif, mineral_1_agreement.tif, mineral_1_runner_up.tif,
                                depth_1.tif, depth_1_n.tif, depth_1_spread.tif, n_epochs.tif,
                                mineral_1_rgba.tif, mineral_1_legend.json, classes.json, item.json
```

The 1.85 GB radiance files are never downloaded: `patterns` lists only the OBS file of each
`EMITL1BRAD` record, and a file that matches no pattern is neither indexed nor fetched.
`index/`, `out/` and `assets/` are git-ignored.
