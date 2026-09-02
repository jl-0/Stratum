# emit-critical-minerals: the product, as designed

**Status: design target. It does not run yet.** This manifest is the full EMIT Critical Minerals
L3 product written down as configuration, and it is the example spec 09 is built around. Keep it
as the statement of intent; run [`../emit-cmr-nevada/`](../emit-cmr-nevada/README.md), which is
this manifest reduced to what the framework builds today.

## What it declares

| Block | Intent |
|---|---|
| `grid` | One arcsecond, one-degree tiles, `block_size` 720 |
| `aoi` | Four pilot zones by name from `../zones.yaml` |
| `time` | August 2022 to July 2026, monthly votes, one product per year |
| `inputs` | CMR as the source; roles for geometry, mineral id, band depth, uncertainty, the L2A mask and soil fraction |
| `aux` | A slope raster and a daily snow raster, both on S3 |
| `granule_filter` | Cloud fraction, solar zenith, an August to November seasonal window |
| `pixel_mask` | Detector-edge trim, the L2A cloud/cirrus/water/spacecraft flags, a soil-fraction floor |
| `scorer` | `cleanest_nadir`: nadir-preferring, penalised on steep slopes, rejecting snow |
| `snapshot` | A curated lumping file (`classes/cm-v1.yaml`), inverse-variance band depth conditional on the class, the uncertainty carried, view zenith carried |
| `outputs` | COG and NetCDF to an S3 bucket, STAC, agreement-faded rendering |
| `plugins` | A `stratum_emit` wheel fetched at start, so a scorer change ships without a new image |
| `budget` | 64 tiles, 12,000 granules, 400 vCPU-hours, human approval on exceedance |

## What stands between it and a run

`pixi run stratum validate -m examples/emit-critical-minerals/manifest.yaml` reports exactly the
gap, and will keep doing so as it closes. As of 2026-09-02:

| Problem `validate` reports | What it needs |
|---|---|
| `aux` is declared, and `cleanest_nadir` requires `slope` and `snow` | The ancillary-data path: staged rasters warped once per tile onto the block grid (spec 05) |
| `formats: [netcdf]` | A NetCDF writer beside the COG writer (spec 07) |

Beyond what `validate` can see:

- **`classes/cm-v1.yaml` does not resolve against real granules.** Its members were written as
  illustrations before a delivered class table was inspected: the hematite member names group 1,
  which the granule carries only in group 2, and the pyrite member's record 2568 is Jarosite in
  the granule's table. Which `(library, record, group)` entries define goethite, hematite and
  pyrite is a science decision to make with the mineral team, not a code fix. A test pins the
  current zero-match error so the file cannot be mistaken for working configuration.
- **The zone boxes in `../zones.yaml` are approximate**, placed around the named districts, and
  need confirming against the project's zone definitions before a delivered run.
- **`index_location`, `outputs.bucket` and `plugins.wheel` are placeholders** on S3. S3 access,
  the plugin-wheel fetch and the `require_approval` gate are all specified and unbuilt; locally the
  gate refuses rather than waits.
- **It is a campaign, not a laptop run.** At the measured 150 MB per granule, the budget line of
  12,000 granules is well over a terabyte of downloads and belongs on the cloud executor.

## How the pieces relate

```
emit-critical-minerals/manifest.yaml     the product as designed        <- this directory
        |  drop aux, masks, lumping, netcdf, S3, approval; one tile, one year
        v
emit-cmr-nevada/manifest.yaml            the same shape, runnable today
```

When a block above moves from the second table to "built", it is meant to move back into the
runnable example without changing anything else in this file.
