# Stratum

A cost-function-driven mosaic engine for imaging spectroscopy, built to run in the cloud.

> **Status: design.** No implementation yet. Everything here is specification and interface
> design, deliberately ahead of code. "Stratum" is a working name.

## What this is

EMIT has built a mineral mosaic pipeline three times. Each one hard-codes a different answer to
the same question: *when several observations cover the same patch of ground, which one wins?*

Stratum factors that question out. The framework owns the parts that never change — querying an
index, regridding granules onto a common grid, orchestrating the fan-out, caching, provenance —
and exposes the parts that are actually science as versioned plugins. A science team iterates on
a cost function; nobody rebuilds a pipeline.

The immediate driver is the **EMIT Critical Minerals L3 mosaic**, but nothing in the core is
EMIT-specific. Instrument knowledge lives in a plugin package.

## The shape of it

Five stages. Only the middle two are science:

| # | Stage | Unit | What it does |
|---|-------|------|--------------|
| 1 | **Plan** | run | Manifest → frozen granule index → work list → budget gate |
| 2 | **Regrid** | granule × tile | KD-tree nearest-neighbour → GLT. Pure geometry, **cached forever** |
| 3 | **Resolve** | tile × epoch × block | Run the `Scorer`; collapse observations into one epoch snapshot |
| 4 | **Reduce** | tile × block | Run the `Reducer`; collapse epochs through time |
| 5 | **Publish** | tile | Data product **and** rendered image, STAC item, provenance |

The load-bearing idea is the split between 2 and 3. Regridding is expensive and depends only on
geometry; scoring is cheap and changes hourly. Content-addressing the GLT means changing a cost
function re-reads cached geometry instead of rebuilding it.

## Five plugin hooks

| Hook | Stage | Decides |
|------|-------|---------|
| `GranuleFilter` | plan | Which granules are candidates — date range, cloud fraction, quality |
| `PixelMask` | regrid | Which pixels are eligible — cloud, cirrus, water, snow |
| `Scorer` | resolve | **Which observation wins in a cell** — the cost function proper |
| `Reducer` | reduce | How epochs collapse through time — mode, median, spread |
| `OutputMapper` | publish | What the result looks like — enum→colour, ramps, confidence alpha |

## Documentation

- **[docs/design/Cloud-Mosaic-Architecture.html](docs/design/Cloud-Mosaic-Architecture.html)** —
  the architecture proposal and the evidence behind it. Read this first; it explains *why* the
  design looks like this, with references into the three existing pipelines.
- **[docs/specs/](docs/specs/)** — component specifications. Interfaces and contracts.
- **[docs/decisions/](docs/decisions/)** — architecture decision records.
- **[refs/](refs/)** — reference material from the existing pipelines.

## Relationship to existing code

| Repo | Relationship |
|------|--------------|
| `SpectralUtil` | **Wrapped, not forked.** Provides the KD-tree GLT build. Two upstream contributions wanted: a pluggable selection seam, and persisting the score band. |
| `emit-sds-l3` | V002 reference implementation. Julia path stays as reference only. |
| `EMIT-AMD` | Precedent for deferred reduction and for the lumping/colour config. Cluster-specific; not reusable directly. |
| `tetracorder-lite` | Upstream L2B producer. Its reference matrix defines the stable mineral `id` that lumping tables must key on. |

## Open questions blocking design

1. **Is `freq-N` in EMIT-AMD a frequency rank or a time period?** Decides how much of
   mode-through-time is new work. One question to James.
2. **Mode over mineral ID directly, or over something continuous first?** For Phil.
3. **AWS account, quota and Earthdata credential path.** The long pole.
