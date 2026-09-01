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
| **Grid** | CRS + resolution + origin. Global and infinite; defines cell boundaries everywhere. |
| **Tile** | A bounded rectangle of the grid. The unit of *product* delivery. |
| **Block** | A subdivision of a tile. The unit of *compute*. Never appears in outputs. |
| **Epoch** | One time window (e.g. a month). Granules within it produce one snapshot. |
| **Cadence** | The delivered temporal unit (e.g. a year). One or more epochs reduce into it. |
| **GLT** | Geographic lookup table. Maps a grid cell → (granule, raw row, raw col). |
| **Role** | A logical input name (`geometry`, `mineral`, `mask`) resolved per collection. |
| **Observation** | One granule's contribution to one block, after regrid and masking. |
| **Snapshot** | The winning value per cell for one epoch — output of `Scorer`. |
| **Product** | The reduced result per tile — output of `Reducer`, plus its rendering. |

Two distinctions that matter and are easy to lose:

- **Tile ≠ block.** Tiles are a product decision (what a consumer downloads). Blocks are a
  compute decision (what fits in a worker). Changing block size must never change output.
- **Epoch ≠ cadence.** Epochs are the voting population; cadence is the delivery unit. Monthly
  epochs reduced to an annual product means twelve votes per output pixel.

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
        │  + PixelMask applied              │   Lambda or Batch
        └─────────────────┬─────────────────┘
                          │  GLT COGs (content-addressed)
        ┌─────────────────▼─────────────────┐
   3    │  RESOLVE            ◀── Scorer    │   unit: tile × epoch × block
        │  apply GLTs for this block,       │   Lambda or Batch
        │  score, keep the winner           │
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

### Stage 1 — Plan

Reads the manifest; resolves the AOI to a tile list and the time range to epochs; queries the
granule index; applies `GranuleFilter`; **freezes** the surviving candidate set into a
run-scoped artifact; computes the fan-out; compares it to the declared budget.

Freezing is not optional. Both existing pipelines query a coverage file that is refreshed
whenever it is stale, so the same inputs select different granules on different days and nothing
records which. A run must name exactly the granules it consumed, permanently.

Outputs: frozen index (GeoParquet), work list (JSONL in S3), run manifest hash, dry-run report.

### Stage 2 — Regrid

For each (granule, tile): build the GLT by nearest-neighbour from the granule's `loc` array to
the tile grid, apply `PixelMask` so masked pixels never enter the stack, write a GLT COG.

**This stage is pure geometry.** Its output depends on the granule, the grid definition, and the
mask spec — and on nothing about the science. It is content-addressed and cached indefinitely.
Changing a cost function does not invalidate it. See [03](03-regrid-glt.md), [06](06-caching.md).

### Stage 3 — Resolve

For each (tile, epoch, block): gather the granules intersecting this block in this epoch, apply
their GLTs, and run the `Scorer` to pick a winner per cell.

Two execution modes, chosen by the scorer's declared capability:

| Mode | When | Memory | Example |
|------|------|--------|---------|
| `streaming` | Score decomposes per granule | O(block) | min view zenith |
| `stack` | Score needs all observations at once | O(block × N) | median, consensus |

Outputs the winning value(s), **the score that won**, and a contributing-observation count. The
score band is not optional — without it nobody can answer "why did this pixel win?" later.

### Stage 4 — Reduce

For each (tile, block): collapse the epoch snapshots through time via `Reducer`. For Critical
Minerals this is mode-through-time, emitting the modal class, an agreement measure, an epoch
count, and a runner-up.

The stack here is *epochs*, not granules — small and bounded by the cadence. A no-op reducer
reproduces V002 semantics exactly.

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
| 1 → 2 | Work list of (granule, tile) | JSONL in S3, read by an item reader |
| 2 → 3 | GLT + masked observation refs | COG, content-addressed key |
| 3 → 4 | Epoch snapshots | COG per (tile, epoch, block) |
| 4 → 5 | Product blocks | COG per (tile, block) |
| 5 → out | Data + image + legend + STAC + provenance | Per tile, run-prefixed |

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
2. **Geometry is independent of science.** Nothing in stage 2 may read a scorer parameter.
3. **Every artifact is content-addressed** by a hash of the inputs that determine it, and only
   those. See [06](06-caching.md).
4. **A run is reproducible from its manifest hash alone** — same frozen index, same plugin
   versions, same aux sources, same result.
5. **Aux data must be declared** in the manifest. Undeclared reads produce cache keys that lie.
6. **Data and rendering both ship.** Colour is never the only output.
7. **Categorical classes resolve through attributes**, never positional indices. Products carry
   their own class tables, and a vintage bump must not silently reassign classes. See
   [11 §9](11-types.md).

---

## 6. Specs

| Spec | Covers |
|------|--------|
| [01 — Grid, tiling, blocks](01-grid-tiling.md) | Grid definition, tile/block decomposition, halos |
| [02 — Granule index](02-granule-index.md) | Index schema, freezing, queries, role resolution |
| [03 — Regrid and GLT](03-regrid-glt.md) | GLT format, KD-tree, masking, SpectralUtil boundary |
| [04 — Cost functions](04-cost-functions.md) | All five hook contracts, execution modes |
| [05 — Ancillary data](05-ancillary-data.md) | Readers, regridding, `AuxAccessor`, caching |
| [06 — Caching](06-caching.md) | Content addressing, cache keys, invalidation |
| [07 — Output mapping](07-output-mapping.md) | `OutputMapper`, legends, data/image split |
| [08 — Execution](08-execution.md) | Step Functions, Batch, Lambda, routing, retries |
| [09 — Run manifest](09-run-manifest.md) | Schema, composition, budget, validation |
| [10 — Provenance](10-provenance.md) | STAC, run records, reproducibility |
| [11 — Core types](11-types.md) | Every shared type, fill/nodata rules, class tables |
