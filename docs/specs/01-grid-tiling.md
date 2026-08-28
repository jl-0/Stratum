# 01 — Grid, Tiling and Blocks

**Status:** draft · **Depends on:** [00](00-overview.md) ·
**Depended on by:** [03](03-regrid-glt.md), [04](04-cost-functions.md), [06](06-caching.md)

---

## 1. Grid

A grid is CRS + resolution + origin. It is global and infinite; it defines cell boundaries
everywhere, so any tile derived from it aligns with any other tile from the same grid.

```yaml
grid:
  crs: EPSG:4326
  resolution: [0.0003, -0.0003]    # x positive, y negative
  origin: [-180, -90]
  tile_size: 1.0
  block: 512
```

Cell edges are at `origin + n × resolution`. Deriving tile bounds from the grid rather than from
data extents is what makes GLTs shareable between runs and between projects — a GLT computed for
Critical Minerals is byte-identical to one computed for AMD over the same ground at the same grid.

Precedent: V002 uses 0.00055° (~60 m) with 5° cells; EMIT-AMD uses 0.0003° (~30 m) with 1° bins.
Neither is inherited by default. **Grid choice is a manifest decision**, and blocks make tile size
much less load-bearing than it was for either.

### Guard rails

Carried over from `build_obs_nc`, which already refuses the classic mistake:

- reject a positive `y` resolution unless explicitly forced — it is almost always an error;
- reject `resolution > 1` with a `4xxx` EPSG — metres and degrees confused;
- warn when a tile at the configured resolution exceeds a cell count that will not fit a worker.

---

## 2. Tile — the product unit

A tile is a bounded rectangle of the grid and the unit a consumer downloads. Tiles are named
canonically by their grid indices so a name is a position, not a label:

```
{grid_id}/{tile_x}_{tile_y}          e.g.  emit30/-111_32
```

AMD names directories `32_33_-111_-110` (min/max lat/lon), which is readable but redundant with
the grid and awkward to sort. We keep bounds in metadata, not in the identifier.

Tiles need not tile the world. An AOI resolves to the set of tiles intersecting it, and
EMIT's coverage band (~52°N–52°S) means global runs should clip rather than iterate dead cells —
V002 already loops only −55…55.

---

## 3. Block — the compute unit

A block is a subdivision of a tile, and **the unit of work throughout stages 2–4**.

### Why blocks exist

AMD asks Slurm for 32 GB, 4 CPUs and up to 24 h per 1° bin, because a materialized observation
stack over a whole tile is enormous. Ported naively that is a large, long, expensive Batch job
where one late failure discards everything.

| Materialized stack | 1° tile | 512×512 block |
|---|---:|---:|
| Cells | 11.1 M | 0.26 M |
| × 100 obs × 10 bands × float32 | ~44 GB | ~1.0 GB |
| Observations intersecting the unit | all | a subset |
| Fits a 10 GB Lambda | no | **yes** |
| Blast radius of one failure | whole tile | 1/42 of a tile |

Three things follow: the materialized reduction fits a serverless worker; parallelism rises ~40×,
which matters against a 10,000-child Distributed Map; and retries get cheap enough to make Spot
safe.

A second argument, independent of the stack: `write_cog` materializes the full array plus a GDAL
`MEM` copy plus overviews before writing, so **peak memory scales with tile area** regardless of
observation count. Blocks attack the term that actually dominates.

### Invariant

> **Block size never changes output.** Block-wise results must be bit-identical to tile-wise
> results.

This is enforced by a seam-equivalence test in CI, not by convention. It is what allows block size
to be tuned per workload without touching the product contract — and, as noted, MMGIS tiling
merges tiles anyway, so block size is purely a compute knob.

---

## 4. Halos

Block decomposition is only sound for **spatially local** operations — each output cell depending
only on observations over that cell. Mode, median, percentile and count all qualify.

Some things do not, and this is not hypothetical. `remove_negatives(clean_contiguous=True)` in the
existing regrid runs a 3×3 `convolve2d` over the interpolated-pixel mask and zeroes cells with ≥3
flagged neighbours — a stencil reading one pixel beyond the block edge, which would produce
visible seams if applied block-wise without overlap.

```python
class Scorer(Protocol):
    halo: int = 0        # pixels of overlap required on every side
```

The framework materializes `halo` extra pixels around each block, runs the plugin, and trims
before writing. A plugin needing a genuinely global view declares `capability = "tile"` and routes
to Batch, giving up block parallelism honestly rather than corrupting results quietly.

Halo cost is `(1 + 2h/B)²`: at `B=512`, a 1-pixel halo costs 0.8%, a 32-pixel halo 13%.

---

## 5. Choosing block size

Not a product decision, so tune it freely:

| Pressure | Direction |
|---|---|
| Materialized stack must fit the worker | smaller |
| Halo overhead | larger |
| Per-task fixed cost (cold start, credential fetch, index read) | larger |
| Parallelism and retry granularity | smaller |
| Source chunk alignment (COG internal tiling, typically 512) | align to it |

Default **512**, aligned with typical COG internal tiling so windowed reads do not straddle
chunks. Expect to revisit once a pilot zone has been measured.

---

## 6. Open questions

1. Should tiles be allowed to be non-square, or aligned to something other than whole degrees?
2. Do we need a second, coarser grid for the ASA-style 0.5° aggregate, or is that a separate
   product built from these outputs?
3. Should block size be per-stage — larger for regrid, smaller for a materialized reduce?
