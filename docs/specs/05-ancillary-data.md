# 05 — Ancillary Data

**Status:** draft · **Depends on:** [01](01-grid-tiling.md), [04](04-cost-functions.md) ·
**Depended on by:** [06](06-caching.md)

How a cost function reaches data that has nothing to do with EMIT's grid.

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
    temporal: nearest
    max_age: P3D
  claims:
    uri: s3://.../mining-claims.parquet
    kind: vector
    burn: claim_type
    all_touched: true
```

`kind` and `resampling` are both required. The reader refuses to guess: a categorical source with
a continuous resampling method is a plan-time error, not a runtime surprise.

---

## 3. The accessor

```python
aux.raster("dem")                          # (H, W), block grid, cached
aux.raster("snow", date=obs.granule.datetime)
aux.vector("claims")                       # rasterized to block grid
aux.table("climate_index").at(date)        # scalar, no gridding
aux.granule_index                          # the frozen index, for stack-level reasoning
```

Sources are addressed by their **manifest alias**, not by URI. That keeps plugins portable across
deployments and is what makes the declaration in §5 enforceable.

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
   DEM path fails in stage 1, not in 4,000 concurrent workers.
2. Aux can be prefetched and warped during planning, so stage 3 never blocks on a cold fetch.
3. Source identity enters the cache key.

That third point is the load-bearing one:

> A scorer that quietly opens an undeclared raster produces a **cache key that lies.** Swap that
> file and every cached snapshot is silently stale, with no signal that anything changed.

Refusing undeclared reads is a small restriction that protects the reproducibility guarantee
everything else rests on. Sources are keyed by content — ETag or explicit version — so replacing a
file in place under a stable URI still invalidates correctly.

---

## 6. Open questions

1. Should `aux` support remote HTTP sources, or require staging into our bucket first? Staging is
   more reproducible and avoids depending on third-party uptime mid-run; it costs a copy.
2. Do we need vector→vector spatial joins (e.g. "distance to nearest mine"), or is rasterize-and-
   burn sufficient? Distance transforms are a plausible near-term ask and are not expressible as
   a burn.
3. How do we express aux that varies per *epoch* rather than per granule — monthly snow climatology
   against monthly epochs? Probably a `temporal: epoch` mode, but it interacts with block caching.
