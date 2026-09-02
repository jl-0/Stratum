# 03 — Regrid and the GLT

**Status:** draft · **Depends on:** [01](01-grid-tiling.md), [02](02-granule-index.md) ·
**Depended on by:** [04](04-cost-functions.md), [06](06-caching.md)

The regrid stage: putting a granule on the grid. Pure geometry, and the most valuable thing we cache.

---

## 1. The reframing

The two existing pipelines use the same GLT machinery in opposite ways, and the difference is the
single most useful thing in the prior art.

| | V002 | EMIT-AMD |
|---|---|---|
| GLTs per tile | one, for all granules | one **per granule** |
| Selection | fused into regrid, streaming argmin | deferred to a post-apply reduction |
| Criterion | `--criteria_band 5 --criteria_mode min` (view zenith) | none at regrid |
| Memory | O(grid) | O(grid × N) |
| Temporal integration | impossible — observations discarded | yes, the whole point |

Both are reductions over an observation stack. V002's reducer happens to be decomposable, so it
can be fused into the regrid loop for free; AMD's is not, so it must materialize.

> **The GLT is a *regrid operator*, not the mosaic.** Selection is a separate reduction that may
> or may not be fused into it. That reframing is what makes temporal aggregation expressible at
> all — and it is why regrid carries no science.

---

## 2. GLT format

3-band int32, matching the existing convention so artifacts interoperate:

| Band | Content |
|---|---|
| 1 | `GLT X` — source column, 1-based |
| 2 | `GLT Y` — source row, 1-based |
| 3 | `File Index` — which granule, 1-based |

With one GLT per granule, band 3 is always `1`. It is kept so SpectralUtil and AMD tooling open
Stratum GLTs unchanged; the granule's identity is in the file's metadata as `granule_id`, beside
`grid.id`, so a GLT is meaningful without any file list.

`0` is nodata. **Negative values mark interpolated cells** — those whose nearest neighbour
exceeded `max_distance`. Preserving the sign convention matters: it is how downstream code
distinguishes "no data" from "data, but reached for". Interpolated cells are valid observations;
the flag reaches a scorer as `obs.interpolated` ([12 §2](12-data-access.md)).

### Two additions

1. **Persist the score.** `build_obs_nc` declares four band names (`GLT X`, `GLT Y`,
   `File Index`, `OBS val`) but allocates three, computing the criteria array and discarding it.
   For a product whose premise is defensible selection, "why did this pixel win?" must be
   answerable from the artifact. In Stratum that artifact is the **epoch snapshot**, not the GLT:
   a per-granule GLT is built before any selection, so it has no winning score to carry, and
   `GLT.score` is `None` for everything regrid writes. Resolve writes `score.tif` beside the
   layers ([13 §2](13-snapshot-schema.md), [04 §4](04-cost-functions.md)). Persisting the band
   in the *fused* path remains the upstream contribution in §4.
2. **Carry grid identity in metadata**, so a GLT can be validated against the grid a run expects
   rather than assumed compatible.

---

## 3. Algorithm

Unchanged from `SpectralUtil` — this is good code and we wrap rather than reimplement:

1. Subset the tile grid to the granule's bounding box.
2. Build a `scipy.spatial.KDTree` over the granule's `loc` points.
3. Query it with the subset grid points (`workers=n_cores`).
4. Convert flat indices to (row, col), offset by 1 (0 = nodata).
5. Mark cells beyond `max_distance` negative; default threshold is 1.5 × the grid diagonal.
6. Clean contiguous interpolated regions — **a 3×3 `convolve2d`, and therefore a stencil**; see
   halos in [01 §4](01-grid-tiling.md).

Cost is dominated by step 3: ~1.6 M granule points against ~13 M grid cells for a 1° tile at
one arcsecond. This is the expensive step, and the reason it is cached.

### Which `loc`, and how the wrap is done

`stratum/regrid/__init__.py` wraps `find_subgrid_locations` and `remove_negatives` from
`spectral_util.mosaic.mosaic`; nothing is copied. The contracts it fixes:

- **Regrid takes `loc` from one role.** `inputs.geolocation` names it; when absent it is the
  first role in `inputs.roles` order whose collection's reader declares `space == "sensor"`
  ([09 §2](09-run-manifest.md)). Every sensor-space asset of a granule shares one geolocation,
  so one GLT per granule per tile serves every role, and the GLT indexes *that* role's sensor
  array — which is why every role read for an observation must agree on `VarSpec.shape[:2]`
  ([12 §2](12-data-access.md)).
- **`max_distance` is the number actually used.** `None` in the manifest resolves to 1.5 × the
  grid diagonal (`resolve_max_distance`) and the resolved float is what enters the key
  ([06 §2](06-caching.md)); the planner and resolve compute it through the same function so
  keys match.
- **The full sensor index survives the wrap.** `find_subgrid_locations` unravels KD-tree indices
  against the shape of the `loc` arrays it is handed, so it cannot take pre-filtered points. The
  valid `(lat, lon)` points are passed as an `(n, 1)` column, it returns `row = ±(flat + 1)`,
  and `|row| − 1` is mapped back to the `(row, col)` of the full sensor array with the sign
  preserved (`build_glt_raw`). Points that are masked *or* non-finite are dropped; `-9999` is
  not recognised here — a sentinel reaching regrid is a reader bug and is deliberately not hidden.
- **Step 6 runs once, on the assembled full-tile array**, not on the sub-grid. Everything outside
  the sub-grid is `0`, so the result is identical, and the stencil stays per tile so no block
  seam exists. `build_glt_raw` and `clean_contiguous` are exposed separately so the negative
  marking and the stencil are testable on their own.
- **A granule whose `loc` bounds miss the tile still writes an all-zero GLT** under its key, so
  the miss is a hit next time rather than a rediscovery ([06 §8](06-caching.md) question 2).
- **GLTs are GeoTIFFs with internal tiles dividing the block** (`internal_tile_size`), deflate,
  no overviews, written by the GTiff driver rather than GDAL's COG driver — which would
  `CreateCopy` the whole file through memory to reorder its headers, buying nothing for a
  windowed read. `read_glt` zero-pads a window that extends past the raster (a halo at a tile
  edge): `0` means no hit beyond the tile as well as inside it.
- **`n_workers`** (feeds `KDTree.query(workers=)`) is a keyword on `build_glt` and
  `regrid_granule_tile`; it changes nothing in the output and is therefore not in the key.

Two inherited behaviours worth knowing (`mosaic.py`, `get_subgrid_from_bounds` and
`remove_negatives`): the target grid is subset to the `loc` bounding box **before** the query, so
cells whose centres fall outside the granule's outermost points are never assigned even within
`max_distance`; and `clean_contiguous=True` zeroes *any* cell — a positive hit too — with three or
more flagged cells in its 3 × 3, all three bands. On the trial tile the second removed nothing
([notes/heritage.md](../notes/heritage.md), "First measurements").

### Two methods

`kdtree` above is the default and works for any product that ships a `loc` array. Some products
also ship a lookup table on their own ortho grid — EMIT's L2B does ([11 §7](11-types.md)) — and
`warp_embedded` uses it: the reader hands back the product's own GLT through
`GranuleReader.glt()` ([12 §3](12-data-access.md)) and regrid nearest-warps its integer bands onto
the tile grid, a raster operation over two small bands instead of a KD-tree over 1.6 M points.
The two do not give identical results — the warp is nearest-of-nearest — so `regrid_method` is in
the GLT cache key ([06 §2](06-caching.md)) and is a manifest field, `grid.regrid_method`, never a
heuristic. Which is faster, and how far they differ, is a pilot measurement.

### Use the cores

`pipeline.sh` hardcodes `--n_cores 1` — which feeds `KDTree.query(workers=)`, the compute-bound
inner loop — while requesting `--cpus-per-task=4`, with a strictly serial per-granule loop. Three
of four CPUs idle through the entire regrid phase. Set `n_cores` from the actual worker
allocation. The wrapper exposes it as `n_workers`; the local executor's `regrid_item` currently
leaves it at `1` because the process pool already saturates the cores — the right value for a
SLURM task is `$SLURM_CPUS_PER_TASK` (question 4).

---

## 4. Boundary with SpectralUtil

**Wrap, not fork.** EMIT-AMD points at a *personal fork* of SpectralUtil for a click interface
that upstream now ships; we do not repeat that.

Two contributions worth making upstream rather than vendoring:

1. **Pluggable selection seam.** Replace `criteria_band`/`criteria_mode` with an optional callable
   — roughly a 15-line change at the `crit_mask` computation.
2. **Persist the score band** (§2).

Everything else Stratum builds alongside.

### What must be solved locally

`SpectralUtil` cannot read S3, but the obstacle is shallower than it looks — two guards, not an
architecture:

```python
# spec_io.load_data, first statement
if not os.path.exists(input_file):
    raise FileNotFoundError(f'{input_file} not found.')

# and every mosaic CLI argument
@click.argument('glt_file', type=click.Path(exists=True))
```

Sequencing: **stage-in shim first** (download to ephemeral disk, call existing code unmodified,
upload) — works immediately; **`/vsis3/` streaming second**, which turns a block read into a range
request over only the pixels it touches. Both modes sit behind one `AssetStore` handle, so a reader
does not change when the mode does ([12 §4](12-data-access.md)).

Also note `write_cog` builds the whole output through GDAL's in-memory `MEM` driver, so output
size is bounded by worker RAM. Blocks keep that comfortable.

### The L2B reader gap

`spec_io.open_netcdf` dispatches on filename substrings and has readers for EMIT `rdn`, `rfl`,
`obs` and `l2a_mask`, plus airborne sensors — but **none for the L2B mineral products**, the
primary Critical Minerals input. An `EMIT_L2B_MIN_*.nc` falls through to
`ValueError: Unknown file type`. AMD avoided this by reading cluster ENVI `.img`.

That reader now exists — `stratum_emit.readers:L2BMin`, covering both files of a record
(`MIN`, `MINUNCERT`) — beside `L1BRad`, `L2AMask` and `L2BFrcov` ([12 §3](12-data-access.md)); the
`tetrapy` flavour of [02 §5](02-granule-index.md) is a registry override away. Substring dispatch
is itself fragile once files are staged into cache-keyed paths — Stratum passes product type
explicitly from the role declaration instead.

It is a `GranuleReader` implementation rather than a patch to `spec_io`: the protocol, the
registry keyed on collection, and the reason it is shaped that way are [12 §3](12-data-access.md).

---

## 5. Masking is not done here

`PixelMask` runs in **resolve's read path**, not in regrid. Regrid reads `loc` and nothing else:
no pixel band, no mask asset. A sensor-space mask is applied to the sensor window before the gather
through the GLT; a map-space mask to the block after it ([12 §2](12-data-access.md)). Either way a
masked pixel never enters the observation stack, which is what keeps the scorer concerned only
with ranking.

The consequence for caching is the one that matters: the mask spec is **not** in the GLT key.
Geometry does not depend on cloudiness, so changing a mask must not rebuild GLTs. The masked result
may be cached per (granule, tile, block) as an optimization for the next scorer over the same
ground, and a miss costs a windowed read, not a regrid ([06 §2](06-caching.md)).

---

## 6. Open questions

1. ~~Should the GLT store the source `granule_id` directly rather than an index into a file list?~~
   **Resolved:** keep the band — always `1` for a per-granule GLT — so existing tooling opens the
   file, and write `granule_id` into the GLT's metadata so it is meaningful without a list (§2).
2. ~~Is per-granule GLT (AMD) or per-tile-epoch fused GLT (V002) the default?~~ **Resolved:** per-
   granule, always. A fused GLT would put the scorer inside regrid, which invariant 2 in [00
   §5](00-overview.md) forbids and the cache asymmetry in [06 §1](06-caching.md) depends on.
3. ~~Do we need sub-pixel/area-weighted resampling, or is nearest-neighbour sufficient?~~
   **Resolved:** nearest-neighbour. Area-weighted aggregation is a downstream product, as V002's ASA
   is.
4. `n_workers` is plumbed but the local executor leaves it at `1`. On the trial machine the KD-tree
   query was ~60 % of a 19 s single-threaded regrid of a 49 %-coverage granule and halved with four
   workers; the executor should size it from its allocation once the pool is not already saturating
   the node ([08 §1](08-execution.md)).
5. `build_obs_nc` runs a second `remove_negatives` over the *fused* GLT; Stratum cleans only per
   granule. On the trial tile the difference zeroed nothing. Whether it ever can — a positive hit
   ringed by another granule's interpolated cells — is a pilot question, and the answer decides
   whether the parity claim needs a caveat.
6. GLTs (and products, [07 §7](07-output-mapping.md)) are tiled GeoTIFFs, not valid COGs by GDAL's
   validator: no `LAYOUT=COG`, no overviews. Nothing in the read path needs the COG header order.
   Either write through the COG driver with `OVERVIEWS=NONE` or stop calling them COGs in the STAC
   media type; the GLT writer sits behind the `ALGO_HASH` gate, so changing it is a recorded bump.
