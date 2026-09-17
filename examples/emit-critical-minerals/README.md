# emit-critical-minerals: the product, as designed

**Status: design target. It does not run yet.** This manifest is the full EMIT Critical Minerals
L3 product written down as configuration, and it is the example spec 09 is built around. Keep it
as the statement of intent; run [`../emit-cmr-nevada/`](../emit-cmr-nevada/README.md), which is
this manifest reduced to what the framework builds today, and
[`../emit-cmr-cuprite/`](../emit-cmr-cuprite/README.md), which is that same reduction over a
multi-tile AOI. The four zones below cover 11 one-degree tiles (1, 2, 2 and 6), and
`south-central-az` is itself a 2 x 3 set — exactly the shape the Cuprite example exercises at half
a degree, so the fan-out this product needs is the one that already runs.

## What it declares

| Block | Intent |
|---|---|
| `grid` | One arcsecond, one-degree tiles, `block_size` 720 |
| `aoi` | Four pilot zones by name from `../zones.yaml` |
| `time` | August 2022 to July 2026, monthly votes, one product per year |
| `inputs` | CMR as the source; roles for geometry, mineral id, band depth, uncertainty, the L2A mask and soil fraction |
| `aux` | A slope raster and a daily snow raster. Aux itself is built; **these two URIs are invented** and their fetch modes are not built |
| `granule_filter` | Cloud fraction, solar zenith, an August to November seasonal window |
| `pixel_mask` | Detector-edge trim, the L2A cloud/cirrus/water/spacecraft flags, a soil-fraction floor |
| `scorer` | `cleanest_nadir`: nadir-preferring, penalised on steep slopes, rejecting snow |
| `snapshot` | A curated lumping file (`classes/cm-v1.yaml`), inverse-variance band depth conditional on the class, the uncertainty carried, view zenith carried |
| `outputs` | COG and NetCDF to an S3 bucket, STAC, agreement-faded rendering |
| `plugins` | A `stratum_emit` wheel fetched at start, so a scorer change ships without a new image. **Not built** — refused at validation |
| `budget` | 64 tiles, 12,000 granules, 400 vCPU-hours, human approval on exceedance |

## What stands between it and a run

`pixi run stratum validate -m examples/emit-critical-minerals/manifest.yaml` reports exactly the
gap, and will keep doing so as it closes. As of 2026-09-16 it prints five:

| Problem `validate` reports | What it needs |
|---|---|
| `aux.slope`, `aux.snow`: `s3://` scheme | Aux stages through the asset store, which opens `https://` and `file://`. S3 direct access is in-region only (spec 12 §4) |
| `aux.snow`: `temporal: nearest` | Resolving `{date}` means finding the nearest date that *exists*, which needs a listing — and a run never queries a catalogue (spec 02 §6) |
| `formats: [netcdf]` | A NetCDF writer beside the COG writer (spec 07 §2) |
| `plugins.wheel` | The iteration delivery path: fetch a wheel to `/tmp` at cold start (spec 04 §7). Refused rather than ignored, because the field is read by nothing and a run would silently use whatever is installed |

**The ancillary-data path itself is no longer on this list.** Aux was built on 2026-09-14 and runs
in [`../emit-cmr-cuprite/manifest-bare-earth.yaml`](../emit-cmr-cuprite/), which stages four real
ESA WorldCover tiles over anonymous HTTPS and warps them onto every block. What blocks
`cleanest_nadir` is not aux but `snow`'s date-keyed fetch, above.

Beyond what `validate` can see:

- **`classes/cm-v1.yaml` does not resolve against real granules.** Its members were written as
  illustrations before a delivered class table was inspected: the hematite member names group 1,
  which the granule carries only in group 2, and the pyrite member's record 2568 is Jarosite in
  the granule's table. Which `(library, record, group)` entries define goethite, hematite and
  pyrite is a science decision to make with the mineral team, not a code fix. A test pins the
  current zero-match error so the file cannot be mistaken for working configuration.
- **The zone boxes in `../zones.yaml` are approximate**, placed around the named districts, and
  need confirming against the project's zone definitions before a delivered run.
- **Several values are invented, and no tool can tell you so.** `index_location`,
  `outputs.bucket`, `plugins.wheel` and both `aux` URIs name nothing that exists — the wheel
  claims `stratum_emit-0.4.2` where the real distribution is `0.0.1`, installed in-image. The
  manifest marks each one `# NOTIONAL` at its line, and distinguishes them in its header from the
  features that are merely *unbuilt*: an unbuilt feature is refused by name and cannot rot
  silently, an invented value can.
- **`on_exceed: require_approval` is unbuilt and `validate` does not say so**, because it only
  bites at run time — the executor refuses rather than parking the run until `stratum approve`
  (spec 08 §3).
- **It is a campaign, not a laptop run.** At the measured 150 MB per granule, the budget line of
  12,000 granules is well over a terabyte of downloads and belongs on the cloud executor.

## How the pieces relate

```
emit-critical-minerals/manifest.yaml     the product as designed        <- this directory
        |  drop the lumping file, netcdf, S3, the wheel and the approval gate;
        |  one tile, one year
        v
emit-cmr-nevada/manifest.yaml            runnable today, one tile, no aux
        |  half-degree tiles and a bbox AOI; same science
        v
emit-cmr-cuprite/  three manifests, all runnable today:
        manifest.yaml              2 x 3 tiles, 8 months of 2026. The plain baseline
        manifest-bare-earth.yaml   3 x 6 tiles, all of 2025. + an ortho-native role
                                     and real WorldCover aux
        manifest-joint.yaml        the same 3 x 6 and year, + a Reducer plugin
                                     across EMIT's two mineral groups
   (each has a .cloud.yaml sibling: the same manifest pointed at S3 for the cloud executor)
```

When a block above moves from the second table to "built", it is meant to move back into the
runnable example without changing anything else in this file.
