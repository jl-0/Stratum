# Prior art and heritage

**Internal.** Not part of the documentation site. This is the record of what Stratum was built
from, where that code lives, and which observations forced particular design choices — so that a
claim in a spec can be traced back to a file, and so nobody re-derives the same findings.

The user-facing docs deliberately do not carry any of this. They describe how the tool works.

---

## 1. Where the code lives

| What | Location | Relevance |
|---|---|---|
| `SpectralUtil` | Submodule of the EMIT monorepo | The KD-tree regrid and the GLT format we wrap |
| — the selection seam | `spectral_util/mosaic/mosaic.py` | `criteria_band` / `criteria_mode`; the eight lines Stratum generalizes |
| — the readers | `spectral_util/spec_io/spec_io.py` | `load_data` dispatch; no L2B mineral reader; two guards block S3 |
| `emit-sds-l3` (V002) | Submodule of the EMIT monorepo | Reference implementation of the fused-selection mosaic |
| — driver | `run_all_aggregate.py` | Loops −55…55; `--criteria_band 5 --criteria_mode min` |
| — GLT build | `build_cloudy_glts_m4_rev.py` | |
| — granule selection | `subset_from_coverage.py` | Scans `coverage_pub.json`; three sequential `deepcopy` passes |
| `EMIT-AMD` | `github.jpl.nasa.gov/jamesmo/EMIT-AMD` | Deferred-reduction precedent; temporal stacking |
| — pipeline | `pipeline.sh` | One GLT per granule; `steps=(...)` hand-rolled caching; `rm -r` on every run |
| — tiling | `split.sh` | Region → 1° bins → SLURM array |
| — FID resolution | `convert_fids.py` | Hard-codes `b0106_v01` in a module-level dict |
| — post-processing | `gdal.sh` | Coastal mask burns band 4 (alpha); `# Skip counts for now, doesn't work` |
| — supervision | `watch.sh` | Greps stderr for `error|fail|oom`; matches `*Finished*` against a last line that reads `Done` |
| `amd` package | `/store/jamesmo/amd/repo/` (cluster) | The Python package behind `pipeline.sh`. Environment only at `/store/jamesmo/micromamba/envs/amd` — that path is **not** the repo. |
| — config | `configs/config.yml` | Copied verbatim to [`refs/amd-config.yml`](../../refs/amd-config.yml) |
| `tetracorder-lite` | Submodule of the EMIT monorepo | Upstream L2B producer |
| — aggregation | `tetrapy/aggregate.py` | Writes `group_{N}_{band_depth,mineral_id,band_depth_unc,fit}` over `downtrack`/`crosstrack` |
| — reference matrix | `tetrapy/data/v6.00a6.csv` | 312 entries; columns `index,id,group,library,record,title,path,url` |
| Delivered granule | [`refs/EMIT_L2B_MIN_001_20260825T151308_2623710_050.nc`](../../refs/) | Ground truth for spec 11. **Gitignored** — see `CLAUDE.md` |

---

## 2. The reframing Stratum is built on

V002 and EMIT-AMD use the same GLT machinery in opposite ways.

| | V002 | EMIT-AMD |
|---|---|---|
| GLTs per tile | one, for all granules | one **per granule** |
| Selection | fused into regrid, streaming argmin | deferred to a post-apply reduction |
| Criterion | `--criteria_band 5 --criteria_mode min` (view zenith) | none at regrid |
| Memory | O(grid) | O(grid × N) |
| Temporal integration | impossible — observations discarded | yes, the whole point |

Both are **reductions over an observation stack**. V002's reducer is decomposable, so it fuses into
the regrid loop for free; AMD's is not, so it must materialize. The difference is an optimization,
not an architecture.

Hence: **the GLT is a regrid operator, not the mosaic.** Selection is a separate reduction that may
or may not be fused into it. That is what makes temporal aggregation expressible, and why stage 2
carries no science.

Parity check that keeps the abstraction honest: a `streaming` scorer, one epoch, no reducer must
reproduce V002 exactly.

### The seam

`spectral_util/mosaic/mosaic.py` — the entire selection decision:

```python
ob = raw_ob[np.abs(sub_glt[...,1])-1, np.abs(sub_glt[...,0])-1]
existing_crit = criteria[sub_glt_insert_idx[...,0], sub_glt_insert_idx[...,1]]
valid = np.logical_and(sub_glt[...,0] != nodata, ob != local_nodata)

if criteria_mode == "min":
    crit_mask = np.logical_and(ob < existing_crit, valid)
elif criteria_mode == "max":
    crit_mask = np.logical_and(ob > existing_crit, valid)
```

Replacing the comparison with a callable is roughly a fifteen-line change.

---

## 3. Observations that forced design choices

| Observed | Where | Consequence in the design |
|---|---|---|
| Granule selection scans `coverage_pub.json` fetched from the **MMGIS load balancer**, refreshed when >24 h stale | `subset_from_coverage.py`, `pipeline.sh` | Indexed GeoParquet + a **frozen** per-run candidate set. An archival product must not depend on a viewer service. |
| `if 'Total Cloud Fraction' in feat['properties'] and ... <= max` | V002 filter | A granule lacking the key is dropped silently, indistinguishable from "too cloudy" → explicit `on_missing` policy, default `fail`, with per-filter counts in the report |
| `pipeline.sh` unconditionally `rm -r`s `glt/` and `applied/` | EMIT-AMD | No resume at all → content-addressed cache |
| `--n_cores 1` hardcoded while requesting `--cpus-per-task=4` | `pipeline.sh` | `n_cores` feeds `KDTree.query(workers=)`, the compute-bound loop → set it from the actual allocation |
| `watch.sh` greps stderr for `error|fail|oom`; matches `*Finished*` against `Done` | EMIT-AMD | Library warnings read as failure; silent OOM reads as success → `ResultWriter` per-item outcomes |
| Delivered rasters are `*.colors.tiff` RGBA; colormap lives only in `configs/config.yml` | EMIT-AMD | Nothing downstream can restyle or recount. The **counts product was dropped** because `gdal_rasterize -b 4 -burn 0` needs an alpha band → data and image both ship, legend as sidecar + STAC |
| `spec_io.open_netcdf` dispatches on filename substrings; no L2B mineral reader | SpectralUtil | `EMIT_L2B_MIN_*.nc` raises `ValueError: Unknown file type` → L2B reader is the first deliverable; product type passed explicitly from the role declaration |
| Two guards block S3: `os.path.exists` in `load_data`, `click.Path(exists=True)` on every mosaic CLI arg | SpectralUtil | Shallower than it looks — stage-in shim first, `/vsis3/` streaming second |
| `write_cog` builds output through GDAL's `MEM` driver plus overviews | SpectralUtil | Peak memory scales with **tile area regardless of observation count** → blocks attack the dominant term |
| `remove_negatives(clean_contiguous=True)` runs a 3×3 `convolve2d` | SpectralUtil regrid | A real spatial stencil → halos are required, not hypothetical |
| `build_obs_nc` declares four band names, allocates three | SpectralUtil | The criteria array is computed and discarded → persist the score band |
| AMD asks Slurm for 32 GB, 4 CPUs, up to 24 h per 1° bin | `split.sh` | Does not fit Lambda's 15 min / 10 GB → block decomposition, then routing |
| EDL S3 credentials last ~1 hour | LP DAAC | AMD's 24 h jobs would fail partway if ported naively → refresh wrapper |
| `convert_fids.py` hard-codes `b0106_v01` | EMIT-AMD | Build version becomes an indexed, filterable column |
| Delivered granule embeds `/mineral_metadata` (294 entries) | `refs/EMIT_L2B_MIN_*.nc` | Products carry their own class tables → generic `ClassTable`, config-declared location, attribute matching |
| `index` is 1…294 contiguous and positional; `v6.00a6.csv` has 312 | granule vs tetracorder-lite | Index space differs between vintages → fingerprint and fail, or remap through attributes. `(library, record, group)` uniquely resolves 292/294. |
| L2B MIN has no `id` column | `refs/EMIT_L2B_MIN_*.nc` | An earlier assumption that PR-20's slug would be available in delivered data was **wrong**; lumping matches on `library`/`record`/`group` instead |

---

## 4. Where the original sketch met service limits

| Sketched | Finding | Replaced by |
|---|---|---|
| Lambda is the main engine | 15 min / 10 GB / 10 GB `/tmp`; AMD needs 32 GB and hours per tile | A **router**, not a choice. Blocks shrink the unit until Lambda fits for most work. |
| Cost functions are Lambda invocations | A per-pixel decision cannot pay an invocation each | **In-process plugins** via entry points. A wheel fetched from S3 gives the layer *ergonomics* without the mechanism. |
| Dependencies ship as Lambda layers | Zip layers cap at 250 MB unzipped across all layers; GDAL + netCDF4 + scipy + numpy exceeds it | **Container image Lambdas** (10 GB), one image with two entrypoints |
| Stateful workflow orchestration | Confirmed — Distributed Map reads work lists from S3, 10,000 children | Kept as sketched |
| Terraform generates the infrastructure | Confirmed, with a boundary worth naming | ADR-0002 |

---

## 5. Upstream contributions wanted

Two changes belong in `SpectralUtil` rather than vendored here:

1. **Pluggable selection seam** — replace `criteria_band`/`criteria_mode` with an optional callable
   at the `crit_mask` computation.
2. **Persist the score band** — allocate and write the fourth band `build_obs_nc` already names.

**Wrap, never fork.** EMIT-AMD depends on a personal fork of SpectralUtil for a click interface
that upstream now ships. Do not repeat that.

---

## 6. What is inherited deliberately

| From | Kept |
|---|---|
| SpectralUtil | The KD-tree regrid, the 3-band GLT format, the negative-means-interpolated sign convention, and the `build_obs_nc` guard rails (positive-`y` rejection, metres-in-degrees rejection) |
| EMIT-AMD | Deferred reduction. Layered config composition (`base ← patch ← patch`). The `stack.mincount` / `stack.ignore` parameters — the only working precedent for mode-through-time. |
| V002 | Streaming argmin as a first-class execution mode, not a legacy path |
| Operator practice | `pipeline.sh`'s `steps=(...)` switches are hand-rolled caching; content addressing makes the same judgement automatic |

---

## 7. First measurements

**2026-09-02**, the first-slice trial (`examples/trial-nevada/`): tile (−118, 41), June 2026,
`MinViewZenith`, 14 L2B MIN + 14 L1B OBS granules staged locally, one arcsecond, `block_size: 720`.
Apple Silicon laptop, 18 CPUs, local disk, pixi environment. Wall-clock unless noted.

| Step | Measurement |
|---|---|
| OBS fetch (earthaccess, 14 × ~108 MB, 4 threads) | 42.7 s |
| Index build over `trial-data/` (1293 files, header reads only) | 4.1 s standalone; `stratum plan` end to end incl. the implicit build, asset check, class-table inspection, freeze, work lists, report: 5.4 s |
| First full `stratum run` (18 spawn workers) | 39.7 s = plan ~5 s + regrid 27.7 s (14 items, 4.3–25.0 s each, median 17.0 s, 203 s CPU under 14-way contention) + resolve 4.2 s (25 blocks, median 0.51 s) + reduce 3.9 s (median 0.52 s) + publish 1.6 s |
| Isolated regrid, no contention | granule covering 49 % of the tile: 19.3 s with `n_workers=1`, 11.3 s with 4; granule covering 0.7 %: 3.2 s / 1.9 s. KD-tree query ≈ 60 % of it |
| Re-run of the finished run | 11.4 s; regrid/resolve/reduce all hits at 2.4–2.9 s per stage (the spawn pool's floor), publish 1.6 s (never a hit) |
| Parity variant (same GLT keys, `pixel_mask: []`, `ignore: []`) | 15.6 s; regrid 14/14 hits |
| `build_obs_nc` over the same grid, 14 OBS files, `n_cores 4` | 84.9 s (loads the whole 70 MB `obs` cube per file); gather through the fused GLT 0.4 s |
| Cache footprint after both runs | GLT 35 MB (14), snapshot 27 MB per run, product 5.3 MB per run; products 7 MB per run; 166 MB total |

**Parity.** `parity.py` builds the fused GLT with `spectral_util.mosaic.mosaic.build_obs_nc`
over exactly the trial grid (its `target_extent_ul_lr` are cell *centres*, so the corners are
offset by half a cell), `criteria_band 2` (to-sensor zenith, as stored), `criteria_mode min`,
`max_distance 0.000589256` (1.5 × the diagonal), the file list in granule-id order — the same
order resolve's candidate loop uses, both strict comparisons so the earliest wins a tie. Result on
12,957,415 valid cells (0.9998 of the tile): fused-only 0, Stratum-only 0, fused hits landing on
a fill MIN pixel 0, `mineral_1` agreement **1.000000**, view zenith identical to < 10⁻⁴ °. An
independent scipy KDTree check per granule (nearest within `max_distance`, minimum zenith,
earliest wins) on 3000 random valid cells: 3000/3000. The one structural difference —
`build_obs_nc` runs a second `remove_negatives` over the *fused* GLT, Stratum only per granule —
zeroed nothing here. With `edge_trim` on (the main run) 4,723 cells seen only by a swath's outer
seven columns become nodata; otherwise the main run equals the parity run wherever valid.

**2026-09-02, the archive run** (`examples/emit-cmr-nevada/`): the same tile from CMR, January–August
2026, `P1M` epochs, `min_count: 2`, `max_cloud_fraction: 0.8`; nothing staged by hand. Same
laptop, home connection.

| Step | Measurement |
|---|---|
| `stratum index build` (CMR, scoped to the tile and eight months) | 4.1 s; 74 rows / 37 granules; anonymous, nothing downloaded |
| `stratum plan` | 93 s; staged 34 remote assets = 1.55 GB (1 OBS for the sample-asset check, 33 MIN for the per-granule class-table check) into `out/assets`; 4 granules dropped by the cloud filter (89–98 % cloud); one fingerprint `sha256:fc392354…` on all 33, identical to the local trial's; builds `010634` (3) / `010635` (30) |
| First `stratum run` | 131 s = re-plan ~2 s (34/34 hits) + regrid 99.2 s (32 OBS downloads ≈ 3.5 GB by parallel workers + 33 GLTs) + resolve 5.7 s (146 items) + reduce 20.8 s (25 items) + publish 3.8 s |
| Download volume | `du -sh out/assets` 5.0 GB, 66 files; `out/cache` 241 MB; `out/products` 19 MB |
| Effective throughput | ≈ 17 MB/s serial at plan time; ≈ 37 MB/s with parallel regrid workers |
| Second `stratum run`, nothing changed | 14.1 s; regrid 33/33 hits (2.8 s), resolve 146/146 (3.0 s), reduce 25/25 (3.0 s), publish rewrites (3.7 s) |
| Product sanity | 3600 × 3600; `n_epochs` 3–6 on every cell; voted fraction 0.329; mean `mineral_1_agreement` over voted cells 0.595; `depth_1` median 0.021, spread median 0.003; leaders Cummingtonite HS294.3B, Butlerite GDS25, Basalt_weathered BR93-43, Nanohematite BR93-34B2 — the same ids (20, 76, 40, 47, 45) as the local June trial |
| June 2026 cross-check | the CMR index holds 12 of the local trial's 14 granule ids, identical; the two absent (`20260613T203446_2616413_011`, `20260621T172640_2617211_012`) touch the tile with the header bounding box but not with the footprint polygon CMR searches on |

Two things this run fixed on the way: `CloudCover` is an integer percent and the index column is
a fraction, so the first `CMRSource` stored `19.0` and `max_cloud_fraction: 0.8` dropped every
granule (now `/100`, percent kept in `attributes.cloud_cover`); and the planner has to stage every
contributing granule's class-table asset, not just one sample, because the vintage check reads
the table out of each file — those bytes are the ones resolve needs anyway, so the run's total
volume is unchanged and the first plan is simply where 1.55 GB of it lands.

**Findings worth carrying.** (1) The delivered L2B is one gzip chunk per variable, so every
observation read decodes the full 1664 × 1242 array — cheap at int16, but it is why prepared
assets (12 §4) matter before a fan-out. (2) `ignore: [none]` over one epoch delivers 73.5 % of
observed cells as nodata (13 §7). (3) The granule's `/mineral_metadata` has eleven duplicate
names; `classes: source` suffixes the later one `#id`. (4) Products and GLTs fail GDAL's COG
validator (no `LAYOUT=COG`, no overviews) while the STAC media type claims COG (03 §6). (5) CMR
has no `EMITL1BOBS` short name; OBS is the second asset of `EMITL1BRAD.001`, which is what the
manifests and the reader registry now say (12 §8, resolved 2026-09-02). In the same pass:
`EMITL2AMASK` is published at collection version `002`, and `EMITL2BFRCOV.001` ships per-fraction
GeoTIFFs rather than one NetCDF.

### The EMIT ortho lattice, and adopt versus kdtree

*Measured 2026-09-03, over the granules on this machine; scripts were throwaway, the numbers are
reproducible from the assets in `examples/emit-cmr-nevada/out/assets` and `trial-data/`.*

**Every EMIT ortho grid is the same lattice.** 1360 granules across `EMITL1BRAD` (OBS, 47),
`EMITL2BMIN` (674) and `EMITL2BMINUNCERT` (639), 2026-01-29 to 2026-08-28, lon −121 to −45, lat
−25 to +48: one cell size (0.000542232520256367°), one CRS string, and an origin phase of
0.399041310736 cell in x and 0.051286937952 cell in y measured from (−180, 90), with a spread of
1.9 × 10⁻⁹ and 2.0 × 10⁻¹⁰ cell respectively — float64 noise. Where OBS and MIN cover the same
acquisition (33 pairs, 4 compared band-for-band) the tables are bit-identical and the
geotransforms equal. This is what makes `adopt` possible at all ([03 §3](../specs/03-regrid-glt.md)),
and it corrects an earlier claim in [11 §7](../specs/11-types.md) that the origin was per-granule:
the *extent* is, the lattice is not.

**The two methods do not agree, and the difference is systematic.** On tile (−234, 82) of a
0.5° grid laid on that lattice, for one granule:

| | |
|---|---|
| cells in tile | 850,084 |
| hit by `adopt` / by `kdtree` / by both | 811,536 / 811,801 / 811,536 |
| both hit, **same sensor pixel** | 469,070 — **57.8 %** |
| both hit, different | 342,466, every one at most **one** sensor pixel away |
| signed offset, row | −1 in 32.3 %, 0 in 67.7 %, **never +1** |
| signed offset, column | −1 in 20.1 %, 0 in 79.9 %, never +1 |
| `kdtree` cells marked interpolated | 1 |
| `adopt` hits | a strict subset: 265 cells `kdtree`-only, 0 `adopt`-only |
| wall clock | `adopt` < 0.01 s vs `kdtree` 0.46 s at 4 workers |

**The cause is a sub-cell registration offset, and three explanations are ruled out.** Measuring
the vector from each cell centre to the lat/lon of the sensor pixel that method chose, in cells:

| method | median distance | mean dx | mean dy |
|---|---|---|---|
| `kdtree` | 0.494 | **−0.000** | **+0.000** |
| `adopt` | 0.633 | −0.184 | −0.372 |

`kdtree` is exactly centred, which is what an inverse nearest-neighbour query against cell centres
must be. The producer's table is displaced about 11 m west and 22 m south of it. Ruled out:

- **An off-by-one in `adopt_glt`'s crop** — that would displace by ±1.000 cell, not 0.18/0.37.
- **A half-cell (corner versus centre) convention** — shifting the sensor points half a cell in
  each of the four diagonal directions gives 68.5 % (E+N), 57.8 % (unshifted), 39.3 %, 18.0 %,
  9.2 % agreement. The best is nowhere near identical, and 0.18/0.37 is not 0.5.
- **A forward scatter** (each sensor pixel floored into one cell, which would bias toward the
  cell's lower-left and leave holes). Both tables reuse sensor pixels at the same rate — 1.578
  cells per pixel for `adopt`, 1.549 for `kdtree`, with matching once/twice distributions — so the
  producer gathers as we do.

The offset is constant within a granule and varies between them, in clusters: over six granules,
(−0.185, −0.370) for two and (−0.390, +0.135) for four, sd ≈ 0.4 within each. Geometry-dependent,
therefore, not a fixed convention. What remains is that the producer's table was built against a
different geolocation than the `loc` shipped beside it in the same file, or with the query in a
different space. **Open.** Until it is understood, `adopt` should be read as *the producer's
registration*, not as a cheaper `kdtree`.

---

## 8. Related documents

- [`2026-08-28-cloud-mosaic-proposal.html`](2026-08-28-cloud-mosaic-proposal.html) — the original
  research write-up, with the full service-limit analysis. Archived; superseded by the specs.
- [`2026-08-28-mines-tagup.md`](2026-08-28-mines-tagup.md) — external group building the same thing;
  source of the detector-edge numbers, FRCOV as an input, and the bare-earth scorer.
- [`../specs/`](../specs/) — the specs carry these citations inline where a contract depends on one.
- [`2026-09-02-first-slice-plan.md`](2026-09-02-first-slice-plan.md) — the build contract the
  first slice was written against; §5 lists the decisions made while building and the spec each
  landed in.
