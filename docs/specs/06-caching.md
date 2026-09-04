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
| **GLT**<br>granule × tile | `granule_id`, `grid_def`, `max_distance`, `regrid_method`, `regrid_algo_version`, and under `adopt` only, `source_checksum` | **No** |
| **Masked observation**<br>granule × tile × block | `glt_key`, `asset_roles` (per role read: the collection/asset/variable binding **and** the asset's catalogue checksum, `null` when the source has none), `pixel_mask_spec`, `mask_plugin_version`, `remaps` (per categorical layer: the raw table's fingerprint and a hash of the resolved lookup) | No |
| **Aux warp**<br>source × tile | `source_uri`, `source_etag`, `grid_def`, `resampling` | No |
| **Epoch snapshot**<br>tile × epoch × block | `obs_keys[]` (sorted), `aux_keys[]`, `scorer_ref`, `scorer_version`, `scorer_params`, `schema.layers_hash`, `epoch_bounds`, `window` | **Yes** |
| **Product block**<br>tile × block | `snapshot_keys[]`, `aux_keys[]`, `schema.aggregate_hash`; plus `reducer_ref`, `reducer_version`, `reducer_params` when a plugin is named | Yes |
| **Rendered image**<br>tile | `product_keys[]`, `aux_keys[]`, `mapper_ref`, `mapper_version`, `mapper_params` | No — see [07](07-output-mapping.md) |

Read down the "invalidates" column: that is the iteration story. Changing a **scorer** re-runs
resolve, reduce and publish, and re-reads cached GLTs. Changing only an aggregation method or the **reducer** re-runs reduce
and publish. Changing only the
**mapper** re-renders from published data without touching the pipeline. The first run over new
ground is expensive; the twentieth is not.

The **masked observation** is produced in resolve's read path ([12 §2](12-data-access.md)) and
cached only as an optimization: a hit lets the next scorer over the same ground skip the granule
read; a miss costs a windowed read, never a regrid. Nothing depends on it existing — and in the
first slice nothing is written under it. Its *key* is still computed, because each `obs_key` in
the snapshot key is the masked observation's hash, not the bare GLT hash: that is what makes a
mask change, a re-lumping or a re-delivered class table invalidate snapshots (rule 1) while a
GLT stays a hit. The `remaps` term is there for the same reason — the remap is applied at the
gather ([13 §3](13-snapshot-schema.md)), so it determines the observation as much as a mask does.

Two terms in the snapshot key are determined by the build (`stratum/resolve/block.py`):

- `obs_keys[]` are the observations that **reach** the block — the candidates whose GLT has a
  hit in the block window, known from a cheap GLT range read before any granule is opened — so
  the key exists before the hit check and a hit returns without reading a pixel. A granule a mask
  excludes entirely still counts: its mask identity is in the key. `contributing` (granules that
  won a cell) is an output and never enters a key. The list is sorted; candidate order is a
  function of the set, so the sorted list is its canonical form.
- `window` is the block's **core** window on the tile, `[row_off, col_off, height, width]` in
  cells. The tile is in the key's path but the block is not, and two blocks of one tile can be
  reached by the same granules while holding different cells; the block *index* alone would not
  do, because block sizes differ between runs and `(0, 0)` names a different rectangle under
  each. The halo is excluded — it never changes output ([01 §3](01-grid-tiling.md)).

The product key adds no window term: every snapshot key it lists already carries one.

The listed fields are the key's **determinants**; the sidecar may carry explanatory extras. A
snapshot's `.inputs.json` also lists `class_tables` — layer → the raw fingerprints its
observations were remapped from — already inside each `obs_key`, repeated so `stratum cache diff`
can name a vintage change rather than an opaque hash ([13 §6](13-snapshot-schema.md)).

`aux_keys[]` are the warp keys of every aux source the hook declared in `required_aux`, resolved
for the dates or epochs it actually used. A scorer that reads a DEM must have its snapshots
invalidated when the DEM changes; that is the whole reason declaration is mandatory
([05 §5](05-ancillary-data.md)), and it is why the key carries the resolved warps rather than the
source URIs.

### Key construction

```
{root}/cache/{artifact_type}/{grid_id}/{tx}_{ty}/{sha256(canonical_inputs)[:16]}[.tif | /]
```

`canonical_inputs` is a JSON document with sorted keys, explicit types, and no timestamps or
paths that vary between environments. It is stored **beside the artifact** as `.inputs.json`, so a
cache entry can always be explained — which matters the first time two keys unexpectedly differ.
Every inputs document carries `artifact_type`, so two artifact kinds with otherwise identical
inputs can never share a hash (`stratum/cache/__init__.py`, the `*_inputs` builders — the single
source of truth for what enters each key).

**Directory artifacts.** A GLT is one file. A snapshot and a product block are **directories** of
single-band GeoTIFFs (`{layer}.tif`, `score.tif`, `valid.tif`; the delivered band names for a
product block), because layers have distinct dtypes and nodata. A directory artifact's sidecar is
`{hash16}/.inputs.json` *inside* it; a file artifact's is `{hash16}.inputs.json` beside it. Nothing
else may be written into a snapshot directory: `read_snapshot` discovers layers by globbing
`*.tif`. The plan note ([§4](../notes/2026-09-02-first-slice-plan.md)) fixes the layout.

---

## 3. Rules

1. **Only determining inputs go in the key.** Adding a field that does not affect output causes
   spurious misses; omitting one that does causes silent staleness — much worse.
2. **Version every algorithm.** `regrid_algo_version` is bumped by hand when regrid output
   changes. Without it, a bug fix silently serves stale geometry forever. A test records the
   regrid module's content hash and fails when the module changes without a bump, so the manual
   step cannot be forgotten silently. As built: `stratum/regrid/ALGO_HASH` records
   `"{REGRID_ALGO_VERSION} {module_content_hash()}"` over every `*.py` in `stratum/regrid/`;
   `python -m stratum.regrid` checks it and `--record` rewrites it; the test distinguishes
   "missing", "changed without a bump" and "bumped without re-record". A cosmetic edit only needs
   `--record`, and that re-record is the human sign-off that output did not change. A plan made
   under one version is refused by a worker built at another.
3. **Plugin versions are part of the key.** Wheel path *and* content hash — a mutable S3 object at
   a stable URI must not masquerade as the same plugin.
4. **Aux sources are keyed by content**, via ETag or an explicit version. A DEM swapped in place
   under a stable URI is the classic silent-staleness bug.
5. **Undeclared reads are refused.** A plugin that opens an undeclared aux URI produces a key that
   lies; the accessor rejects it rather than trusting authors to remember ([05](05-ancillary-data.md)).
6. **Cache writes are atomic** — write to a temp name, then rename — so an interrupted Spot task
   cannot leave a truncated artifact that later reads as a hit. A directory artifact gets its
   sidecar written *inside* the temp directory before the rename, so the rename is the commit;
   a file artifact's sidecar lands after the file, and a hit requires both, so the window between
   them reads as a miss. Rewriting an existing directory entry removes it first, because
   `rename(2)` cannot replace a non-empty directory.
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

The GLT cache is keyed on `(granule, grid, max_distance)` with no run, project or AOI component. `max_distance` is null for a method that searches for nothing (`adopt`), which
instead keys on `source_checksum`: the table is the producer's, so their version, not
`regrid_algo_version`, is what makes it stale ([03 §3](03-regrid-glt.md)). Two
consequences, both good:

- **Across experiments.** Five competing scorers over one zone share one GLT cache. The first pays
  for geometry; the rest skip plan and regrid. Comparing cost functions becomes the cheapest thing the
  system does.
- **Across products.** If AMD and Critical Minerals adopt the same grid and run in the same
  deployment, the second product's first run is nearly free over ground the first has already
  touched. A deployment's cache is its own; two deployments do not share (§8).

Layout separates what is shared from what is not:

```
{root}/cache/...          shared, content-addressed, long-lived
{root}/runs/{run_id}/...  run-scoped: manifest.merged.yaml, index.parquet, plan.json,
                          work/{stage}.jsonl + work/{stage}.results.jsonl, report.md, provenance.json
{root}/products/{run_id}/{tx}_{ty}/{start}_{end}/   delivered outputs, one directory per tile x period
```

`{root}` is `outputs.bucket`: an `s3://` prefix in a deployment, and locally a **path** —
relative to the manifest — under which the same three directories hang, so the layout and the
keys are identical whichever executor writes them ([08 §1](08-execution.md)). The first slice
refuses a remote root; it is a later slice, not a different design.

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
always one field in `canonical_inputs`. As built, `explain` and `diff` take artifact paths — the
`.tif`, the directory, or the sidecar itself — flatten nested inputs to dotted names
(`grid_def.resolution`) and report a missing field as `None`, so the answer is always one line.
`stats` and `gc` are a later slice.

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
   avoid repeatedly discovering out-of-bounds granules?~~ **Resolved:** not as a separate
   mechanism. A granule whose `loc` bounds miss the tile writes an all-zero GLT under its key
   ([03 §3](03-regrid-glt.md)), so a cached pair is never rediscovered; the first discovery costs
   one KD-tree build, ~3 s for a 0.7 %-coverage granule on the trial machine — the fixed cost, not
   the query.
3. ~~Is `regrid_algo_version` manual, or derivable from a hash of the regrid module?~~ **Resolved:**
   manual, with a test that records the regrid module's content hash and fails when the module
   changes without a bump (§3).
4. Local observation keys carry `checksum: null` for every role, because `LocalSource` has no
   catalogue checksum. A re-delivered file under the same name and header is therefore invisible
   to the key until a source with checksums (CMR's SHA-512) is in use. Whether a local run should
   stat the file into the key — accepting the locality leak rule 7 forbids — is open.
