# ADR-0001 — Technology stack

**Status:** proposed · **Date:** 2026-08-28

Initial stack choices, made now because several specs depend on them. Each records what was
picked, what it was picked over, and what would make us revisit.

---

## 1. Language and runtime — **Python 3.11**

3.11 is the floor. `SpectralUtil` requires ≥3.9 and `tetracorder-lite`'s pixi environments cover
3.9–3.12, so 3.11 sits comfortably inside both while giving us modern typing (`Self`, better
generics) without chasing the newest wheels.

Not 3.12+ yet: geospatial wheels lag, and we inherit GDAL from conda-forge where the practical
constraint is whatever `SpectralUtil` resolves against.

**Revisit if** SpectralUtil raises its floor, or a dependency we need is 3.12-only.

---

## 2. Environment and packaging — **pixi (conda-forge)**

GDAL and netCDF4 are native libraries with no usable pure-pip story. Both `SpectralUtil` and
`tetracorder-lite` already use pixi, so this is consistency rather than novelty — and it means a
developer who can build one repo can build this one.

Rejected:
- **pip + wheels** — GDAL wheels exist but are fragile and diverge from the conda-forge build the
  rest of the org uses.
- **Poetry/uv alone** — same GDAL problem. `uv` is attractive for the pure-Python plugin wheels
  and we may use it *inside* the plugin build, but not for the core environment.

**Revisit if** GDAL's pip story materially improves, or we drop the GDAL dependency for rasterio-
only IO.

---

## 3. Raster IO — **rasterio / rioxarray for our code; `osgeo.gdal` only where SpectralUtil needs it**

We are writing new IO for staging, aux warping, windowed reads and COG output. rasterio gives a
far better API for windowed reads and `/vsis3/` streaming than raw `osgeo.gdal`, and rioxarray
bridges cleanly to the xarray data model below.

`SpectralUtil` uses `osgeo.gdal` directly and `tetracorder-lite`'s `tetrapy` uses
xarray + rioxarray. We match tetrapy for our own code and call SpectralUtil as-is at its
boundary rather than rewriting it.

**Consequence:** both `gdal` and `rasterio` will be in the image. They share the same underlying
libgdal from conda-forge, so this is a namespace overlap, not two copies.

---

## 4. In-memory data model — **xarray**

Bands are addressed by *role name*, not index, throughout the design (`obs["view_zenith"]`, not
`obs[:,:,5]`). That is exactly what xarray gives us, and it is what `tetrapy` already emits —
`group_{N}_mineral_id` and friends over `downtrack`/`crosstrack` dims.

Numpy remains the interface inside a `Scorer`: plugins receive plain arrays for the hot path.
xarray is the container and the metadata carrier, not the arithmetic layer.

**Not dask.** Block decomposition is our parallelism model, and the orchestrator fans blocks out
across workers. Adding dask inside a worker would be a second, competing scheduler. Revisit only
if we find a genuinely tile-scope reducer that cannot be blocked.

---

## 5. Granule index — **GeoParquet + DuckDB**

Replaces the linear scan over `coverage_pub.json`. DuckDB with the spatial and httpfs extensions
queries Parquet directly from S3 over range requests, with predicate pushdown and no server to
operate — it runs inside the planner Lambda.

The frozen per-run index is also GeoParquet, so freezing is a write of the same format.

Rejected:
- **pgSTAC** — a real STAC database is the right answer at organizational scale, and MMGIS already
  runs one. But it is a service to operate, and L3 needs a *frozen* snapshot rather than a live
  catalogue. We can populate our index *from* CMR/STAC without depending on one at run time.
- **Keep the GeoJSON** — grows unboundedly, no index, and three `deepcopy` passes per query.

**Revisit if** an org-wide STAC becomes authoritative and stable; we would then query it in stage
1 and still freeze the result.

---

## 6. Configuration — **Pydantic models + explicit patch composition**

The manifest must be *validated*, *hashed*, and *schema-documented*. Pydantic v2 gives typed
models, JSON Schema export, and precise error messages at plan time — which is where we want
failures, not after compute is provisioned.

Layered composition (AMD's `-p "mosaic<-v6<-filter-fit"`) is a genuinely good idea and we keep it:
`stratum plan -m base.yaml -p zones-pilot -p scorer-v3`. We implement it as an explicit ordered
dict-merge *before* validation, so the merged document is what gets validated and hashed.

Rejected: **mlky** (what AMD uses). It already does patch composition with `${}` interpolation and
James knows it well, which is a real argument. But it is an additional dependency owned outside
the team, and we need typed validation and JSON Schema more than we need interpolation.

**Revisit if** mlky adds schema validation, or if config compatibility with AMD becomes a
requirement rather than an influence.

---

## 7. Orchestration — **Step Functions (Distributed Map) + Lambda + Batch**

- **Step Functions** — Distributed Map reads its work list from S3 and fans out to 10,000
  concurrent children, which is the shape of our problem. Variables/JSONata carry the manifest
  hash without Pass-state chains.
- **Lambda** — control plane, and data plane for blocks that fit.
- **Batch on EC2 Spot** — data plane for everything else. Block-level retries make interruption
  cheap enough to take the discount.

Use **Express** child workflows on the inner maps: Standard bills per state transition, and at
hundreds of thousands of blocks that is a real line item.

Rejected: **EMR/Spark** (wrong shape — this is embarrassingly parallel over independent blocks,
not a shuffle), **plain EC2 fleet** (we would rebuild Batch), **Airflow/Dagster** (a service to
operate; Step Functions is managed and already fits).

---

## 8. Container and IaC — **Docker → ECR; Terraform**

Lambda zip layers cap at 250 MB unzipped across all layers, which GDAL + netCDF4 + scipy + numpy
exceeds before any of our code. **Container image Lambdas allow 10 GB and are the only viable
path.** One image, two entrypoints (Lambda RIC and CLI).

Terraform owns ECR, Batch, Lambda, Step Functions, S3, IAM, budgets and alarms — all run-agnostic.
It owns **no science parameters**; those live in the manifest. See ADR-0002.

---

## 9. CLI — **click**

Matches `SpectralUtil` and `tetracorder-lite`. Typer is nicer but the consistency is worth more
than the ergonomics here.

---

## 10. Testing — **pytest**, with two non-negotiable fixtures

1. **Seam equivalence** — block-wise output must be bit-identical to tile-wise output. This
   guards invariant 1 in [the overview](../specs/00-overview.md), and without it block
   decomposition silently corrupts any plugin with spatial extent.
2. **Cache-key sensitivity** — changing a scorer must invalidate snapshots and must *not*
   invalidate GLTs. Guards the property the whole iteration story rests on.

---

## Summary

| Concern | Choice |
|---|---|
| Language | Python 3.11 |
| Environment | pixi / conda-forge |
| Raster IO | rasterio + rioxarray; `osgeo.gdal` at the SpectralUtil boundary |
| Data model | xarray (numpy inside plugins) |
| Index | GeoParquet + DuckDB |
| Config | Pydantic v2 + ordered patch merge |
| Orchestration | Step Functions Distributed Map + Lambda + Batch/Spot |
| Packaging | Docker → ECR, one image two entrypoints |
| IaC | Terraform |
| CLI | click |
| Tests | pytest + seam-equivalence + cache-key fixtures |
