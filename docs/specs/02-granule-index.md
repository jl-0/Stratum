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
| `collection` | string | `EMITL2BMIN`, `EMITL2ARFL`, `EMITL2AMASK`, `EMITL1BRAD` |
| `datetime` | timestamp UTC | Acquisition start |
| `end_datetime` | timestamp UTC | |
| `geometry` | polygon | Footprint, EPSG:4326 |
| `bbox` | float[4] | Denormalized for cheap prefilter |
| `cloud_fraction` | float | A **fraction in [0, 1]** — from CMR, `CloudCover / 100`, the integer percent kept in `attributes.cloud_cover`; `null` for a local source. Nullable — **nullness is meaningful**, see §4 |
| `build_version` | string | Granule software build, e.g. `010635`; filterable, not hard-coded |
| `product_version` | string | Granule product stamp, e.g. `V001` |
| `collection_version` | string | The **collection's** version, e.g. `001`. Same for every row in a collection |
| `day_night` | string | Nullable; `Day` / `Night` |
| `last_seen` | timestamp UTC | When the index build last observed this row — see §3 |
| `assets` | map<string,string> | asset name → URI, e.g. `MIN`, `MINUNCERT`. A role names one; see §5. From CMR the HTTPS `GET DATA` URL; from a directory a `file://` URI |
| `checksums` | map<string,string> | asset name → catalogue checksum, `sha512:<hex>` from CMR; empty for a local source. Asset identity in cache keys ([12 §4](12-data-access.md)) |
| `attributes` | map<string,string> | Everything else the source returned, verbatim — `SOLAR_ZENITH`, `ORBIT`, `SCENE`, … Filterable by name; promoted to a column only with a reason. From CMR also `granule_ur`, `cloud_cover` and the direct-access link per asset as `s3:<asset>` |

`build_version` is granule-level — CMR exposes it as `SOFTWARE_BUILD_VERSION` — and is filterable.
`product_version` is the file's own stamp and, from CMR, tracks the collection version.
`collection_version` is collection-level and cannot discriminate between granules within a
collection. None of the three is the vintage check on its own; see §3. A row's identity is
(`collection`, `collection_version`, `granule_id`), so a reprocessed collection published as
`EMITL2BMIN.002` sits beside `.001` rather than overwriting it ([11 §4](11-types.md),
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

### Vintage is checked by class table, not by build number

Reprocessing of the **entire catalog** begins ~Sept 2026, takes ~**75 days**, and regenerates every
mineral map against Tetracorder 6 with updated reflectance. The mineral classes shift. For roughly
ten weeks the archive is **mixed-vintage**.

An unpinned query over that window returns some granules at the old vintage and some at the new,
blends two incompatible products, and produces a result that looks entirely plausible. Nothing in
the data announces the problem.

The obvious guard — pin one `build_version` — does not survive contact with the archive.
`EMITL2BMIN.001` already carries at least eight software builds across 2022–2026, none of them a
Tetracorder change ([12 §5](12-data-access.md)). A single-build pin would discard most of a
multi-year run, and "one build per run" would flag every long run as mixed. Requirements, then:

- `build_version` is an **indexed, filterable column**, never hard-coded as `convert_fids.py` does
  with `b0106_v01`, and the run report states every build the run consumed, prominently;
- the index records **when each row was last observed** in `last_seen`, so a re-index after
  reprocessing is detectable rather than silent;
- **plan-time validation fails** if the contributing granules' embedded class tables disagree by
  fingerprint ([11 §9](11-types.md)) — that is the vintage check, because a class shift is exactly
  what a fingerprint catches and a build number does not — unless the manifest opts in with
  `allow_mixed_vintage: true` and a documented reason, and then only if every table still
  resolves fully into the product enumeration ([13 §3](13-snapshot-schema.md));
- if the reprocessing is published as a new collection version, as `EMITL3ASA.002` was, a role
  pins one with `version:` (§5) and the check is trivially satisfied.

**Verified against CMR (2026-09-01).** `SOFTWARE_BUILD_VERSION` and `SOFTWARE_DELIVERY_VERSION`
are granule-level `AdditionalAttributes`, exposed before download, so the index build is a metadata
query and not a header scan. The file additionally carries `product_version` (`V001`) and a
`history` naming `tetracorder5.27c.cmds`; neither is in CMR, and `LocalSource` records them from the
header. Field-by-field mapping in [12 §5](12-data-access.md).

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
    geometry:       {collection: EMITL1BRAD, asset: OBS, var: obs}
    mineral:        {collection: EMITL2BMIN,  var: group_1_mineral_id}
    mineral_uncert: {collection: EMITL2BMIN,  asset: MINUNCERT, var: group_1_band_depth_unc}
    mask:           {collection: EMITL2AMASK, var: mask}
```

A role resolves to `(asset_uri, variable)`. `collection` picks the index rows and the reader that
opens them; `asset` picks the file within a granule record — one CMR record for L2B MIN carries
both `MIN` and `MINUNCERT` — and defaults to the collection's primary file. An optional `version:`
pins a `collection_version` when the index holds more than one, which is a plan-time error to
leave ambiguous ([12 §3](12-data-access.md)). Build version becomes an indexed, filterable column —
which is also how a reprocessing campaign gets pinned to a specific build instead of a string
constant.

> **Two L2B flavours.** `tetrapy` writes an xarray Dataset with `group_{N}_{mineral_id,
> band_depth, band_depth_unc, fit}` over `downtrack`/`crosstrack`; the DAAC ships `EMITL2BMIN`
> NetCDF. Both must resolve through the same role. See [03](03-regrid-glt.md).

---

## 6. Freezing

The plan stage materializes the surviving candidate set into a **run-scoped frozen index** —
`s3://.../runs/{run_id}/index.parquet` — and hashes it into provenance.

This is not optional. A CMR query today and tomorrow return different answers; without freezing, a
"reproducible" run reproduces nothing. Frozen, a run names exactly the granules it consumed,
permanently, and a later re-run can be *verified* rather than merely repeated.

The index build itself is a separate step, so a run never depends on CMR being reachable:
`stratum index build`, or `stratum plan` when the index file is absent. As built a build from a
catalogue source is **scoped** to the manifest's AOI bbox and `[time.start, time.end)` — the
query is cheap and the answer is frozen per run — while a local source indexes its whole
directory. The scope is written into the file, and the plan stage **refuses an index whose
recorded scope does not cover the manifest** (a wider area or window, a collection not built,
another source kind): an existing index is reused only when it can answer the question being
asked, never silently with fewer granules. A `--since` refresh replaces revised rows in place
and keeps the rest. How that step talks to CMR — and how UMM-G maps onto the schema in §2 — is
[12 §5](12-data-access.md).

---

## 7. Open questions

1. ~~Build the index from CMR directly, from CMR-STAC, or from `earthaccess`?~~ **Resolved:**
   `earthaccess` behind a `CMRSource` bridge — it is already a `SpectralUtil` dependency and
   handles EDL, but it returns UMM-G rather than our schema and carries module-level global state.
   See [12 §5](12-data-access.md).
2. ~~Do we index the SDS-internal collections as well as the DAAC ones?~~ **Resolved:** delivered
   collections only. Reprocessing that needs internal builds is a `LocalSource` over them.
3. ~~Should the frozen index carry the full asset URIs, or resolve them at read time from a pinned
   collection version?~~ **Resolved:** full URIs, with the catalogue checksum beside each —
   reproducibility over size.
