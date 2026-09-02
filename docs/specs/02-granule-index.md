# 02 — Granule Index

**Status:** draft · **Depends on:** [00](00-overview.md) ·
**Depended on by:** [04](04-cost-functions.md), [09](09-run-manifest.md), [10](10-provenance.md)

Finding candidate granules, resolving them to assets, and freezing the answer.

---

## 1. What is wrong with the current approach

Both existing pipelines select granules by scanning `coverage_pub.json`, fetched from the **MMGIS
load balancer**:

```
wget -O {file} https://earth.jpl.nasa.gov/emit-mmgis-lb/Missions/EMIT/Layers/coverage/coverage_pub.json
```

`subset_from_coverage.py` then applies ROI, time and cloud filters as three sequential passes,
each `deepcopy`-ing the entire document and rebuilding a Shapely polygon per feature. (A
vectorized pandas version sits commented out directly above, so the cost was known.)

Three problems, worsening:

1. **Slow** — linear scan plus three full deep copies over a file that grows with the mission.
2. **Irreproducible** — AMD refreshes the file whenever it is >24 h old, so the same inputs select
   different granules on different days, and nothing records which.
3. **Inverted dependency** — the archival L3 product's granule selection depends on a *viewer
   service's* web endpoint being up and current.

---

## 2. Index schema

GeoParquet, one row per granule per collection.

| Column | Type | Notes |
|---|---|---|
| `granule_id` | string | Natural key, e.g. `001_20230203T184949_2303413_009` |
| `collection` | string | `EMITL2BMIN`, `EMITL2ARFL`, `EMITL2AMASK`, `EMITL1BOBS` |
| `datetime` | timestamp UTC | Acquisition start |
| `end_datetime` | timestamp UTC | |
| `geometry` | polygon | Footprint, EPSG:4326 |
| `bbox` | float[4] | Denormalized for cheap prefilter |
| `cloud_fraction` | float | Nullable — **nullness is meaningful**, see §4 |
| `build_version` | string | Granule software build, e.g. `010635`; filterable, not hard-coded |
| `product_version` | string | Granule product stamp, e.g. `V001` |
| `collection_version` | string | The **collection's** version, e.g. `001`. Same for every row in a collection |
| `day_night` | string | Nullable; `Day` / `Night` |
| `last_seen` | timestamp UTC | When the index build last observed this row — see §3 |
| `assets` | map<string,string> | role → URI |
| `quality_flags` | map<string,int> | Collection-specific |

`build_version` and `product_version` are granule-level and are what a vintage predicate filters
on. `collection_version` is collection-level and cannot discriminate between granules within a
collection — it is recorded, never filtered for vintage ([11 §4](11-types.md),
[12 §5](12-data-access.md)).

Partitioned by `collection` and acquisition month — the two predicates every query uses.

---

## 3. Query

DuckDB with `spatial` and `httpfs`, reading Parquet directly from S3 over range requests. No
server to operate; it runs inside the planner Lambda.

```sql
SELECT granule_id, datetime, assets
FROM read_parquet('s3://.../emit-granules/**/*.parquet')
WHERE collection = 'EMITL2BMIN'
  AND datetime BETWEEN ? AND ?
  AND ST_Intersects(geometry, ST_GeomFromText(?))
  AND (cloud_fraction <= ? OR cloud_fraction IS NULL)   -- policy, see §4
```

Predicate pushdown plus partition pruning replaces the linear scan. Building the index from
CMR/STAC is a separate, periodic job — see §6.

### Vintage is a required predicate, not an optional one

Reprocessing of the **entire catalog** begins ~Sept 2026, takes ~**75 days**, and regenerates every
mineral map against Tetracorder 6 with updated reflectance. The mineral classes shift. For roughly
ten weeks the archive is **mixed-vintage**.

An unpinned query over that window returns some granules at the old vintage and some at the new,
blends two incompatible products, and produces a result that looks entirely plausible. Nothing in
the data announces the problem.

Requirements:

- `build_version` (and collection version) are **indexed, first-class columns**, never hard-coded
  as `convert_fids.py` does with `b0106_v01`;
- the index records **when each row was last observed** in `last_seen`, so a re-index after
  reprocessing is detectable rather than silent;
- **plan-time validation fails** if a run's frozen index spans more than one vintage, unless the
  manifest explicitly opts in with a documented reason;
- the run report states the vintage(s) selected, prominently.

**Partly answered by the reference granule.** It carries `software_build_version` (`010635`),
`software_delivery_version`, and `product_version` (`V001`) as global attributes, and its `history`
names `tetracorder5.27c.cmds`. So the identifier exists and is unambiguous *in the file*.

Still open: whether CMR exposes these **before download**. If not, vintage filtering cannot be an
index predicate and the index build must record it at ingest — which is the design assumed here.
Both outcomes are handled without changing the run-time contract; see
[12 §5](12-data-access.md).

> Phil has since confirmed that V002 only **adds** metadata. A reader written against V001 fields
> therefore stays forward-compatible. Note this does *not* extend to class tables keyed on
> `index` — see [11 §9](11-types.md).

---

## 4. Null cloud fraction is a policy, not an accident

V002's filter is:

```python
if 'Total Cloud Fraction' in feat['properties'] and feat['properties'][...] <= max_cloud_fraction:
```

A granule whose record *lacks* the key is dropped silently, indistinguishable from "too cloudy".

**Requirement.** `on_missing` is explicit in the manifest per filter — `reject | keep | fail` —
and defaults to `fail` at plan time, loudly, while it is cheap to fix. Counts of what each filter
removed, and why, go into the run report ([04 §2](04-cost-functions.md)).

---

## 5. Role resolution

`convert_fids.py` maps FIDs to ENVI paths under
`/store/emit/ops/data/acquisitions/{date}/{name}/{lvl}/` with the build version `b0106_v01`
hard-coded in a module-level dict. None of that survives the move: ENVI header/binary pairs have
no meaning in object storage, and the layout is cluster-specific.

Instead the manifest declares roles, and the index resolves them:

```yaml
inputs:
  roles:
    geometry: {collection: EMITL1BOBS,  var: obs}
    mineral:  {collection: EMITL2BMIN,  var: group_1_mineral_id}
    mask:     {collection: EMITL2AMASK, var: mask}
```

A role resolves to `(asset_uri, variable)`, and the collection selects the reader that opens it
([12 §3](12-data-access.md)). Build version becomes an indexed, filterable column —
which is also how a reprocessing campaign gets pinned to a specific build instead of a string
constant.

> **Two L2B flavours.** `tetrapy` writes an xarray Dataset with `group_{N}_{mineral_id,
> band_depth, band_depth_unc, fit}` over `downtrack`/`crosstrack`; the DAAC ships `EMITL2BMIN`
> NetCDF. Both must resolve through the same role. See [03](03-regrid-glt.md).

---

## 6. Freezing

Stage 1 materializes the surviving candidate set into a **run-scoped frozen index** —
`s3://.../runs/{run_id}/index.parquet` — and hashes it into provenance.

This is not optional. A CMR query today and tomorrow return different answers; without freezing, a
"reproducible" run reproduces nothing. Frozen, a run names exactly the granules it consumed,
permanently, and a later re-run can be *verified* rather than merely repeated.

The index build itself is a separate periodic job, so a run never depends on CMR being reachable.
How that job talks to CMR — and how UMM-G maps onto the schema in §2 — is
[12 §5](12-data-access.md).

---

## 7. Open questions

1. ~~Build the index from CMR directly, from CMR-STAC, or from `earthaccess`?~~ **Resolved:**
   `earthaccess` behind a `CMRSource` bridge — it is already a `SpectralUtil` dependency and
   handles EDL, but it returns UMM-G rather than our schema and carries module-level global state.
   See [12 §5](12-data-access.md).
2. Do we index the SDS-internal collections as well as the DAAC ones? Phil noted L3 must touch
   every delivered file, which suggests DAAC — but reprocessing may need internal builds.
3. Should the frozen index carry the full asset URIs, or resolve them at read time from a
   pinned collection version? Full URIs are more reproducible; resolution is more compact.
