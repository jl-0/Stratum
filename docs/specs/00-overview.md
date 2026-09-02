# 00 — Pipeline Overview

**Status:** draft · **Depends on:** nothing · **Depended on by:** all other specs

The end-to-end flow, the data that moves between stages, and the vocabulary the rest of the specs
assume. Component detail lives in the numbered specs; this document is the map.

---

## 1. Vocabulary

Fixing these now, because three existing pipelines use the same words differently.

| Term | Meaning here |
|------|--------------|
| **Granule** | One acquisition from one instrument. The atom of input. EMIT: ~75 × 75 km, 60 m. |
| **Grid** | CRS + resolution + origin. A rule for where cell edges fall; has no extent of its own. |
| **Tile** | A bounded rectangle of the grid. The unit of *product* delivery. |
| **Block** | A subdivision of a tile. The unit of *compute*. Never appears in outputs. |
| **Epoch** | The unit of one vote. Granules within it resolve to exactly one snapshot. |
| **Delivery period** | One output product. Reduced from the epochs in its *window*, which need not equal the period. |
| **GLT** | Geographic lookup table. Maps a grid cell → (granule, raw row, raw col). |
| **Role** | A logical input name (`geometry`, `mineral`, `mask`) resolved per collection. |
| **Asset** | One file a granule resolves to for one role. What a reader opens. |
| **Source** | Where the index comes from — a catalogue (CMR, STAC) or a directory. Never queried by a run. |
| **Observation** | One granule's contribution to one block, after regrid and masking. |
| **Snapshot** | The winning value per cell for one epoch — output of `Scorer`. |
| **Schema** | The declared set of snapshot layers — type, enumeration, aggregation — constant for a run. One per manifest ([13](13-snapshot-schema.md)). |
| **Layer** | One band of a snapshot, as the schema declares it. |
| **Product** | The reduced result per tile — output of `Reducer`, plus its rendering. |

Two distinctions that matter and are easy to lose:

- **Tile ≠ block.** Tiles are a product decision (what a consumer downloads). Blocks are a
  compute decision (what fits in a worker). Changing block size must never change output.
- **Epoch ≠ delivery period.** An epoch is one vote, however many observations fall in it — this
  is what stops a densely revisited month outvoting a sparse one. A delivery period is one
  product, reduced from the epochs in its window. Monthly epochs delivered annually gives twelve
  votes per output pixel; a 13-month window delivered monthly gives thirteen, overlapping.

---

## 2. The five stages

```
                     manifest.yaml
                          │
        ┌─────────────────▼─────────────────┐
   1    │  PLAN                             │   unit: run
        │  index query → frozen candidate   │   Lambda
        │  set → work list → budget gate    │
        └─────────────────┬─────────────────┘
                          │  work list (S3), manifest hash
        ┌─────────────────▼─────────────────┐
   2    │  REGRID                           │   unit: granule × tile
        │  KD-tree loc → grid  ⇒  GLT       │   CACHED — geometry only
        │  reads loc only, no pixel bands   │   Lambda or Batch
        └─────────────────┬─────────────────┘
                          │  GLT COGs (content-addressed)
        ┌─────────────────▼─────────────────┐
   3    │  RESOLVE            ◀── Scorer    │   unit: tile × epoch × block
        │  apply GLTs, mask, score,         │   Lambda or Batch
        │  keep the winner                  │
        └─────────────────┬─────────────────┘
                          │  epoch snapshots + score + count
        ┌─────────────────▼─────────────────┐
   4    │  REDUCE             ◀── Reducer   │   unit: tile × block
        │  collapse epochs through time     │   Lambda or Batch
        └─────────────────┬─────────────────┘
                          │  product blocks + agreement + n_epochs
        ┌─────────────────▼─────────────────┐
   5    │  PUBLISH        ◀── OutputMapper  │   unit: tile
        │  stitch → data COG/NetCDF         │   Lambda
        │        → rendered image + legend  │
        │        → STAC item + provenance   │
        └───────────────────────────────────┘
```

The rest of the specs refer to stages **by name** — plan, regrid, resolve, reduce, publish. The
numbers exist only in this diagram, to show order.

### Stage 1 — Plan

Reads the manifest; resolves the AOI to a tile list and the time range to epochs; queries the
frozen granule index — never a live catalogue ([12 §1](12-data-access.md)); applies `GranuleFilter`; **freezes** the surviving candidate set into a
run-scoped artifact; computes the fan-out; compares it to the declared budget.

Freezing is not optional. Both existing pipelines query a coverage file that is refreshed
whenever it is stale, so the same inputs select different granules on different days and nothing
records which. A run must name exactly the granules it consumed, permanently.

Outputs: frozen index (GeoParquet), work list (JSONL in S3), run manifest hash, dry-run report.

### Stage 2 — Regrid

For each (granule, tile): build the GLT by nearest-neighbour from the granule's `loc` array to
the tile grid and write a GLT COG. Nothing else is read — regrid touches `loc`, and no pixel band
and no mask.

**This stage is pure geometry.** Its output depends on the granule, the grid definition and
`max_distance` — and on nothing about the science or the masks. It is content-addressed and cached indefinitely.
Changing a cost function does not invalidate it. See [03](03-regrid-glt.md), [06](06-caching.md).

### Stage 3 — Resolve

For each (tile, epoch, block): gather the granules intersecting this block in this epoch, apply
their GLTs, apply `PixelMask` so ineligible pixels never enter the stack, and run the `Scorer` to
pick a winner per cell. Masking happens here, in the read path, not in regrid
([12 §2](12-data-access.md)). The winner's values are written into the layers the snapshot schema
declares, with raw classes resolved into the product's own enumeration ([13](13-snapshot-schema.md)).

Applying a GLT is also what makes the read cheap. The GLT names exactly which sensor pixels this
block touches — under 4% of a granule's 1664 × 1242 — so only that rectangle need be read, not the
scene ([12 §2](12-data-access.md)) — once the asset is in a windowable layout, which the delivered
L2B is not until it is prepared ([12 §4](12-data-access.md)).

Two execution modes, chosen by the scorer's declared capability:

| Mode | When | Memory | Example |
|------|------|--------|---------|
| `streaming` | Score decomposes per granule | O(block) | min view zenith |
| `stack` | Score needs all observations at once | O(block × N) | median, consensus |

Outputs the winning value(s), **the score that won**, and a contributing-observation count. The
score band is not optional — without it nobody can answer "why did this pixel win?" later.

### Stage 4 — Reduce

For each (tile, block): collapse the epoch snapshots through time, layer by layer, by the
aggregation each layer declares in the schema — a vote for a class layer, a median for a depth
([13 §4](13-snapshot-schema.md)). For Critical Minerals that is mode-through-time: the modal class,
an agreement measure, an epoch count, and a runner-up. A `Reducer` plugin exists for what the
vocabulary cannot express.

The stack here is *epochs*, not granules — small and bounded by the delivery window. Over a single epoch every
aggregation is the identity, which reproduces V002 semantics exactly.

### Stage 5 — Publish

Stitch blocks into tiles; write **both** the data product (class indices, counts, agreement) and
the rendered image via `OutputMapper`, with its legend as a sidecar; write the STAC item and the
provenance record.

Both artifacts, always. See [07](07-output-mapping.md) for why.

---

## 3. What flows between stages

Everything crossing a stage boundary is an S3 object plus a key. Nothing large travels through
the orchestrator — Step Functions caps child-execution input at 256 KiB, so work items carry
keys and hashes, never geometry or band lists.

| Boundary | Payload | Form |
|----------|---------|------|
| plan → regrid | Work list of (granule, tile) | JSONL in S3, read by an item reader |
| regrid → resolve | GLT refs | COG, content-addressed key |
| resolve → reduce | Epoch snapshots | COG per (tile, epoch, block) |
| reduce → publish | Product blocks | COG per (tile, block) |
| publish → out | Data + image + legend + STAC + provenance | Per tile, run-prefixed |

---

## 4. Where the work runs

Placement is a routing decision made by the planner from measurable properties, not a static
choice. One container image carries both a Lambda entrypoint and a CLI entrypoint.

```
if scorer.capability == "tile":   → Batch    # needs a global view
if est_peak_bytes  > 7 GB:        → Batch
if est_seconds     > 600:         → Batch    # 15-min Lambda ceiling
if est_bytes_read  > 8 GB:        → Batch
else:                             → Lambda
```

A pilot zone at block granularity routes almost entirely to Lambda and finishes in minutes with
nothing to warm up. A global reprocessing campaign routes to Batch on Spot. Same code, same
image, same manifest. See [08](08-execution.md).

---

## 5. Invariants

These hold across every stage, and violating any of them breaks something the design depends on.

1. **Block size never changes output.** Block-wise and tile-wise results must be bit-identical.
   Enforced by a seam-equivalence test in CI. Plugins needing spatial context declare a `halo`;
   plugins needing a global view declare `capability = "tile"`.
2. **Geometry is independent of science.** Nothing in regrid may read a scorer parameter
   or a mask; it reads `loc` and nothing else.
3. **Every artifact is content-addressed** by a hash of the inputs that determine it, and only
   those. See [06](06-caching.md).
4. **A run is reproducible from its manifest hash alone** — same frozen index, same plugin
   versions, same aux sources, same result.
5. **Aux data must be declared** in the manifest. Undeclared reads produce cache keys that lie.
6. **Data and rendering both ship.** Colour is never the only output.
7. **Categorical classes resolve through attributes**, never positional indices. Products carry
   their own class tables, and a vintage bump must not silently reassign classes. See
   [11 §9](11-types.md).
8. **One schema per run.** Every snapshot in a run is written under the same layer set and
   enumerations. Extend a schema between runs; never redefine one under cached snapshots
   ([13 §5](13-snapshot-schema.md)).

---

## 6. Specs

| Spec | Covers |
|------|--------|
| [01 — Grid, tiling, blocks](01-grid-tiling.md) | Grid definition, tile/block decomposition, halos |
| [02 — Granule index](02-granule-index.md) | Index schema, freezing, queries, role resolution |
| [03 — Regrid and GLT](03-regrid-glt.md) | GLT format, KD-tree, SpectralUtil boundary |
| [04 — Cost functions](04-cost-functions.md) | All five hook contracts, execution modes |
| [05 — Ancillary data](05-ancillary-data.md) | Aux readers, regridding, `AuxAccessor`, caching |
| [06 — Caching](06-caching.md) | Content addressing, cache keys, invalidation |
| [07 — Output mapping](07-output-mapping.md) | `OutputMapper`, legends, data/image split |
| [08 — Execution](08-execution.md) | Step Functions, Batch, Lambda, routing, retries |
| [09 — Run manifest](09-run-manifest.md) | Schema, composition, budget, validation |
| [10 — Provenance](10-provenance.md) | STAC, run records, reproducibility |
| [11 — Core types](11-types.md) | Every shared type, fill/nodata rules, class tables |
| [12 — Data access](12-data-access.md) | `GranuleSource`, `GranuleReader`, `AssetStore`, CMR, the block read path |
| [13 — Snapshot schema](13-snapshot-schema.md) | Layers, enumerations, aggregation vocabulary, extend-never-redefine |
