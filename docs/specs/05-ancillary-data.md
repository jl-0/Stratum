# 05 — Ancillary Data

**Status:** draft · **Depends on:** [01](01-grid-tiling.md), [04](04-cost-functions.md) ·
**Depended on by:** [06](06-caching.md)

How a cost function reaches data that has nothing to do with EMIT's grid.

> **Not the granule path.** `AuxAccessor` covers everything that is *not* an observation — DEMs,
> landcover, claim polygons. Granules themselves arrive through `GranuleSource`, `AssetStore` and
> `GranuleReader` ([12](12-data-access.md)). The two paths make the same promise to a plugin
> author — already on your block's grid, already windowed, already cached — and share nothing else.

---

## 1. The contract

A scorer will want a DEM, a snow index, a landcover raster, mining-claim polygons. None of it
arrives on our grid, in our CRS, or at our resolution.

> **The block grid is the contract.** Everything `AuxAccessor` returns is already windowed to the
> block, already on the block's exact grid and CRS, and already cached. A scorer never does
> geometry, never sees a CRS, never resamples anything.

If a plugin author has to think about projections, the framework has failed. This is the single
rule that keeps cost functions readable — and readable cost functions are the entire point.

---

## 2. Readers dispatch on shape

| Source shape | Operation | Must be declared |
|---|---|---|
| Raster, different CRS/resolution | Warp to block grid | **Resampling method** |
| Vector polygons/lines | Rasterize, burning an attribute | Field; all-touched vs centroid |
| Vector, as geometry | Clip to the block plus a margin; hand the plugin the features | Margin |
| Vector, distance to nearest | Distance transform onto the block grid | Cutoff distance |
| Swath / irregular points | KD-tree — the same path as [03](03-regrid-glt.md) | Max distance |
| Date-keyed table, no geometry | Scalar lookup; no gridding | Nearest vs interpolated |

### Resampling is declared, never defaulted

Bilinear-interpolating a landcover class and nearest-neighbouring a DEM are both wrong, and
**neither raises an error**. They quietly produce a cost surface nobody can explain later.

```yaml
aux:
  dem:
    uri: s3://.../copernicus-dem-30m.tif
    kind: continuous
    resampling: bilinear
  landcover:
    uri: s3://.../worldcover-2021.tif
    kind: categorical
    resampling: nearest          # `mode` when downsampling
  snow:
    uri: s3://.../snow/{date}.tif
    kind: categorical
    resampling: nearest
    temporal: nearest            # nearest | previous | epoch - see below
    max_age: P3D
  ndvi:
    uri: s3://.../ndvi/{date}.tif
    kind: continuous
    resampling: bilinear
    temporal: epoch              # one resolution per epoch, not per granule
  claims:
    uri: s3://.../mining-claims.parquet
    kind: vector
    burn: claim_type
    all_touched: true
```

`kind` and `resampling` are both required. The reader refuses to guess: a categorical source with
a continuous resampling method is a plan-time error, not a runtime surprise.

`temporal` says how a date-keyed source is resolved. `nearest` and `previous` are relative to the
date the plugin passes — usually the granule's acquisition time — within `max_age`. `epoch`
resolves at the epoch's start and is read with `epoch=obs.epoch`, so a monthly climatology against
monthly epochs is one warp per epoch rather than one per granule.

---

## 3. The accessor

```python
aux.raster("dem")                          # (H, W), block grid, cached
aux.raster("snow", date=obs.granule.datetime)
aux.raster("ndvi", epoch=obs.epoch)          # resolved once per epoch - temporal: epoch
aux.vector("claims")                       # rasterized to block grid
aux.features("claims")                     # the geometries, clipped to this block, block CRS
aux.distance("mines")                      # (H, W) distance to the nearest feature, cached
aux.table("climate_index").at(date)        # scalar, no gridding
aux.granule_index                          # the frozen index, for stack-level reasoning
```

Sources are addressed by their **manifest alias**, not by URI. That keeps plugins portable across
deployments and is what makes the declaration in §5 enforceable.

A plugin that wants its own geometry has it: `obs.coords` is every cell's centre, in the grid CRS
and in lon/lat ([11 §5](11-types.md)), and `features` hands over raw geometries. `distance` exists
because distance-to-nearest is the common case and is worth caching like a warp; anything more
exotic is a few lines of Shapely in the scorer, against declared sources.

---

## 4. Warp once, slice many

A DEM is static. Warping it per block per epoch per granule would repeat identical work thousands
of times.

Aux sources are warped **once per (source, tile, grid, resampling)**, content-addressed exactly
like a GLT, with blocks reading windows out of the cached result. Cost is paid on first touch and
never again — and, like GLTs, the cache is shared across runs and experiments
([06](06-caching.md)).

Time-varying sources key on the resolved date as well, so `snow/{date}.tif` produces one cached
warp per distinct date actually used, not per granule.

---

## 5. Declaration is mandatory

Sources are declared up front, and the accessor **refuses undeclared URIs**.

Three things this buys:

1. The planner validates existence and readability **before provisioning compute** — a typo in a
   DEM path fails in the plan stage, not in 4,000 concurrent workers.
2. Aux can be prefetched and warped during planning, so resolve never blocks on a cold fetch.
3. Source identity enters the cache key.

That third point is the load-bearing one:

> A scorer that quietly opens an undeclared raster produces a **cache key that lies.** Swap that
> file and every cached snapshot is silently stale, with no signal that anything changed.

Refusing undeclared reads is a small restriction that protects the reproducibility guarantee
everything else rests on. Sources are keyed by content — ETag or explicit version — so replacing a
file in place under a stable URI still invalidates correctly.

---

## 6. Open questions

1. ~~Should `aux` support remote HTTP sources, or require staging into our bucket first?~~
   **Resolved:** staged. Aux is copied into the deployment's root first; a run never depends on
   third-party uptime.
2. ~~Do we need vector→vector spatial joins (e.g. "distance to nearest mine"), or is rasterize-and-
   burn sufficient?~~ **Resolved:** the scorer can do it itself, and helpers cover the common cases.
   `obs.coords` gives every cell's centre in the grid CRS and in lon/lat; `aux.features(alias)`
   returns a declared vector source's geometries clipped to the block plus a margin, in the block
   CRS; `aux.distance(alias)` is a cached derived raster — distance from each cell to the nearest
   feature — keyed like a warp (§3). All three go through a declared alias, so cache keys stay
   honest.
3. ~~How do we express aux that varies per *epoch* rather than per granule — monthly snow
   climatology against monthly epochs?~~ **Resolved:** `temporal: epoch`. The source is resolved at
   the epoch's start and read with `aux.raster(alias, epoch=obs.epoch)`; the warp is keyed on the
   resolved date, so it is computed once per epoch and shared by every granule and block in it, and
   its key enters the snapshot key like any other aux read ([06 §2](06-caching.md)).
