# 06 — Caching and Content Addressing

**Status:** draft · **Depends on:** [00](00-overview.md), [01](01-grid-tiling.md) ·
**Depended on by:** [08](08-execution.md), [10](10-provenance.md)

The property that decides whether people actually iterate.

---

## 1. The idea

Every artifact is written to a key derived from a hash of the inputs that **actually determine
it** — and nothing else. Get the key right and two things follow for free: re-running skips
completed work, and changing one thing invalidates exactly what depended on it.

The whole design leans on one asymmetry: **regrid is expensive and never changes; resolve is
cheap and changes hourly.** V002 welds them together, so changing a selection criterion rebuilds
all the geometry. Separating them deliberately is what turns "try a different cost function" from
a full rebuild into a re-read.

---

## 2. Cache keys

| Artifact | Key derived from | New scorer invalidates? |
|---|---|---|
| **Prepared asset**<br>asset | `asset_checksum`, `prepare_version` | No |
| **GLT**<br>granule × tile | `granule_id`, `grid_def`, `max_distance`, `regrid_method`, `regrid_algo_version` | **No** |
| **Masked observation**<br>granule × tile × block | `glt_key`, `asset_roles`, `pixel_mask_spec`, `mask_plugin_version` | No |
| **Aux warp**<br>source × tile | `source_uri`, `source_etag`, `grid_def`, `resampling` | No |
| **Epoch snapshot**<br>tile × epoch × block | `obs_keys[]`, `aux_keys[]`, `scorer_ref`, `scorer_version`, `scorer_params`, `schema.layers_hash`, `epoch_bounds` | **Yes** |
| **Product block**<br>tile × block | `snapshot_keys[]`, `aux_keys[]`, `schema.aggregate_hash`; plus `reducer_ref`, `reducer_version`, `reducer_params` when a plugin is named | Yes |
| **Rendered image**<br>tile | `product_keys[]`, `aux_keys[]`, `mapper_ref`, `mapper_version`, `mapper_params` | No — see [07](07-output-mapping.md) |

Read down the "invalidates" column: that is the iteration story. Changing a **scorer** re-runs
resolve, reduce and publish, and re-reads cached GLTs. Changing only an aggregation method or the **reducer** re-runs reduce
and publish. Changing only the
**mapper** re-renders from published data without touching the pipeline. The first run over new
ground is expensive; the twentieth is not.

The **masked observation** is produced in resolve's read path ([12 §2](12-data-access.md)) and
cached only as an optimization: a hit lets the next scorer over the same ground skip the granule
read; a miss costs a windowed read, never a regrid. Nothing depends on it existing.

`aux_keys[]` are the warp keys of every aux source the hook declared in `required_aux`, resolved
for the dates or epochs it actually used. A scorer that reads a DEM must have its snapshots
invalidated when the DEM changes; that is the whole reason declaration is mandatory
([05 §5](05-ancillary-data.md)), and it is why the key carries the resolved warps rather than the
source URIs.

### Key construction

```
{cache_prefix}/{artifact_type}/{grid_id}/{tile}/{sha256(canonical_inputs)[:16]}.tif
```

`canonical_inputs` is a JSON document with sorted keys, explicit types, and no timestamps or
paths that vary between environments. It is stored **beside the artifact** as `.inputs.json`, so a
cache entry can always be explained — which matters the first time two keys unexpectedly differ.

---

## 3. Rules

1. **Only determining inputs go in the key.** Adding a field that does not affect output causes
   spurious misses; omitting one that does causes silent staleness — much worse.
2. **Version every algorithm.** `regrid_algo_version` is bumped by hand when regrid output
   changes. Without it, a bug fix silently serves stale geometry forever. A test records the
   regrid module's content hash and fails when the module changes without a bump, so the manual
   step cannot be forgotten silently.
3. **Plugin versions are part of the key.** Wheel path *and* content hash — a mutable S3 object at
   a stable URI must not masquerade as the same plugin.
4. **Aux sources are keyed by content**, via ETag or an explicit version. A DEM swapped in place
   under a stable URI is the classic silent-staleness bug.
5. **Undeclared reads are refused.** A plugin that opens an undeclared aux URI produces a key that
   lies; the accessor rejects it rather than trusting authors to remember ([05](05-ancillary-data.md)).
6. **Cache writes are atomic** — write to a temp key, then copy — so an interrupted Spot task
   cannot leave a truncated artifact that later reads as a hit.
7. **Staged source files are not artifacts.** A local copy of an upstream granule is keyed by URI
   and ETag, scoped to a worker, and never enters a cache key. Asset *identity* determines an
   artifact; whether the bytes happened to be local does not ([12 §4](12-data-access.md)). A
   *prepared* asset is different: a transcode keyed on the checksum is an artifact like any other,
   and its science-free key is what lets every run share it.
8. **Extend a schema, never redefine it.** Appending to an enumeration keeps cached snapshots
   valid through `extends`; anything else is a new `layers_hash` and a rebuild of resolve
   ([13 §5](13-snapshot-schema.md)).

---

## 4. Sharing

The GLT cache is keyed on `(granule, grid, max_distance)` with no run, project or AOI component. Two
consequences, both good:

- **Across experiments.** Five competing scorers over one zone share one GLT cache. The first pays
  for geometry; the rest skip plan and regrid. Comparing cost functions becomes the cheapest thing the
  system does.
- **Across products.** If AMD and Critical Minerals adopt the same grid and run in the same
  deployment, the second product's first run is nearly free over ground the first has already
  touched. A deployment's cache is its own; two deployments do not share (§8).

Layout separates what is shared from what is not:

```
s3://{bucket}/cache/...          shared, content-addressed, long-lived
s3://{bucket}/runs/{run_id}/...  run-scoped: frozen index, work lists, reports
s3://{bucket}/products/{run_id}/ delivered outputs
```

---

## 5. Lifecycle

| Class | Policy | Why |
|---|---|---|
| Prepared assets | keep while a frozen index references them; evict by size otherwise | One transcode per asset per deployment; the layout every block read depends on ([12 §4](12-data-access.md)) |
| GLT | keep indefinitely | Expensive, immutable, shared, small relative to imagery |
| Masked observations | expire ~30 days | Optional; rebuilt from a cached GLT and a windowed read |
| Epoch snapshots | keep for the longest `deliver.window` any manifest in the deployment declares, floor 30 days | A rolling window must re-read snapshots, not recompute them ([11 §4](11-types.md)); invalidated by scorer changes anyway |
| Product blocks | keep until the tile is published, then expire | Superseded by the stitched product |
| Frozen index, reports, provenance | keep indefinitely | Tiny, and the basis of reproducibility |

---

## 6. Operations

```
stratum cache stats  --run <id>     # hit rate by artifact type; what a rerun would cost
stratum cache explain <key>         # show .inputs.json and what produced it
stratum cache diff <key-a> <key-b>  # which input differs — for "why did this miss?"
stratum cache gc    --dry-run       # unreferenced entries past their lifecycle
```

`cache diff` earns its place: the common confusion is a miss nobody expected, and the answer is
always one field in `canonical_inputs`.

---

## 7. Precedent

This is a principled version of something the team already does by hand. `pipeline.sh` carries
`steps=([query]=1 [apply-glt]=1 [classify]=1 [stack]=1)` so an operator can skip work they believe
is still good. Content addressing makes that judgement automatic and correct, rather than relying
on someone remembering what changed.

Note also that `pipeline.sh` unconditionally `rm -r`s `glt/` and `applied/` on every run, so AMD
has **no resume at all** — a re-run rebuilds every GLT. That is the specific cost this spec
removes.

---

## 8. Open questions

1. ~~Should the GLT cache be global (one bucket per account) or per-deployment?~~ **Resolved:** per
   deployment. Every run in a deployment shares its cache, which is what lets several products or
   scorer setups over one zone share geometry. Deployments do not share with each other; a local
   deployment and a cluster one are separate unless they point at one root ([08
   §1](08-execution.md)).
2. ~~Do we need negative caching — recording that a (granule, tile) pair does not intersect — to
   avoid repeatedly discovering out-of-bounds granules?~~ **Resolved:** not yet. Measure the cost of
   rediscovery in the pilot first.
3. ~~Is `regrid_algo_version` manual, or derivable from a hash of the regrid module?~~ **Resolved:**
   manual, with a test that records the regrid module's content hash and fails when the module
   changes without a bump (§3).
