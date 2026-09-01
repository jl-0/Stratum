# 06 — Caching and Content Addressing

**Status:** draft · **Depends on:** [00](00-overview.md), [01](01-grid-tiling.md) ·
**Depended on by:** [08](08-execution.md), [10](10-provenance.md)

The property that decides whether people actually iterate.

---

## 1. The idea

Every artifact is written to a key derived from a hash of the inputs that **actually determine
it** — and nothing else. Get the key right and two things follow for free: re-running skips
completed work, and changing one thing invalidates exactly what depended on it.

The whole design leans on one asymmetry: **stage 2 is expensive and never changes; stage 3 is
cheap and changes hourly.** V002 welds them together, so changing a selection criterion rebuilds
all the geometry. Separating them deliberately is what turns "try a different cost function" from
a full rebuild into a re-read.

---

## 2. Cache keys

| Artifact | Key derived from | New scorer invalidates? |
|---|---|---|
| **GLT**<br>granule × tile | `granule_id`, `grid_def`, `max_distance`, `regrid_algo_version` | **No** |
| **Masked observation**<br>granule × tile × block | `glt_key`, `asset_roles`, `pixel_mask_spec`, `mask_plugin_version` | No |
| **Aux warp**<br>source × tile | `source_uri`, `source_etag`, `grid_def`, `resampling` | No |
| **Epoch snapshot**<br>tile × epoch × block | `obs_keys[]`, `scorer_ref`, `scorer_version`, `scorer_params`, `scorer_carry`, `epoch_bounds` | **Yes** |
| **Product block**<br>tile × block | `snapshot_keys[]`, `reducer_ref`, `reducer_version`, `reducer_params` | Yes |
| **Rendered image**<br>tile | `product_keys[]`, `mapper_ref`, `mapper_version`, `mapper_params` | No — see [07](07-output-mapping.md) |

Read down the "invalidates" column: that is the iteration story. Changing a **scorer** re-runs
stages 3–5 and re-reads cached GLTs. Changing only the **reducer** re-runs 4–5. Changing only the
**mapper** re-renders from published data without touching the pipeline. The first run over new
ground is expensive; the twentieth is not.

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
   changes. Without it, a bug fix silently serves stale geometry forever.
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
   artifact; whether the bytes happened to be local does not ([12 §4](12-data-access.md)).

---

## 4. Sharing

The GLT cache is keyed on `(granule, grid, mask)` with no run, project or AOI component. Two
consequences, both good:

- **Across experiments.** Five competing scorers over one zone share one GLT cache. The first pays
  for geometry; the rest skip stages 1–2. Comparing cost functions becomes the cheapest thing the
  system does.
- **Across projects.** If AMD and Critical Minerals adopt the same grid, the second project's
  first run is nearly free over ground the first has already touched.

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
| GLT | keep indefinitely | Expensive, immutable, shared, small relative to imagery |
| Masked observations | expire ~30 days | Large; cheap to rebuild from a cached GLT |
| Epoch snapshots | expire ~30 days | Reproducible; invalidated by scorer changes anyway |
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

1. Should the GLT cache be global (one bucket per account) or per-deployment? Global maximizes
   sharing but complicates lifecycle ownership and cost attribution.
2. Do we need negative caching — recording that a (granule, tile) pair does not intersect — to
   avoid repeatedly discovering out-of-bounds granules?
3. Is `regrid_algo_version` manual, or derivable from a hash of the regrid module? Manual is
   honest but forgettable; derived is automatic but churns on cosmetic edits.
