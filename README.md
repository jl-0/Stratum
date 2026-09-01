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

**Start at [`docs/index.html`](docs/index.html)** — the design site. Open it locally or browse it
on GitHub Pages. It explains *why* the design looks like this, with the evidence behind each
choice, and marks what is locked versus still moving.

| | |
|---|---|
| [`docs/index.html`](docs/index.html) | Design site — shape, rationale, locked types, open questions |
| [`docs/specs/`](docs/specs/) | Component specifications. **Authoritative** — interfaces, contracts, citations |
| [`docs/decisions/`](docs/decisions/) | Architecture decision records |
| [`docs/notes/`](docs/notes/) | Meeting notes, with attribution caveats |
| [`refs/`](refs/) | Reference material from the existing pipelines, kept verbatim |

The site is the narrative; the specs are the contract. When they disagree the specs win — they
carry the citations. Keeping them in step is the first rule in [`CLAUDE.md`](CLAUDE.md).

## Relationship to existing code

| Repo | Relationship |
|------|--------------|
| `SpectralUtil` | **Wrapped, not forked.** Provides the KD-tree GLT build. Two upstream contributions wanted: a pluggable selection seam, and persisting the score band. |
| `emit-sds-l3` | V002 reference implementation. Julia path stays as reference only. |
| `EMIT-AMD` | Precedent for deferred reduction and for the lumping/colour config. Cluster-specific; not reusable directly. |
| `tetracorder-lite` | Upstream L2B producer. Its reference matrix is the *source* of the class table each granule embeds — and it is that embedded table, not a checked-in copy, that lumping resolves against. |

## Not only EMIT

Colorado School of Mines (with CMU and U. Wisconsin's Macrostrat) is independently building the
same thing: tiling Tetracorder mineral maps into state- and country-wide mosaics, with a QGIS
data stream, moving to AVIRIS-5, and aiming at cross-scale work from EMIT's 60 m down to UAV
imagery at 6–10 cm. See [the tag-up notes](docs/notes/2026-08-28-mines-tagup.md).

That makes "a framework, not an EMIT program" a present requirement rather than an aspiration.
Multi-instrument support is a design constraint from the start — which is also why input roles and
band aliases are resolved per collection rather than hard-coded.

## Open questions blocking design

1. **Is `freq-N` in EMIT-AMD a frequency rank or a time period?** Decides how much of
   mode-through-time is new work. One question to James.
2. **Mode over mineral ID directly, or over something continuous first?** For Phil — who has since
   leaned toward *not* reducing over binarized labels. See
   [04 §5](docs/specs/04-cost-functions.md).
3. **What does the vintage identifier look like in delivered metadata**, and is a reprocessed
   granule distinguishable before download? Time-critical — see below.
4. **AWS account, quota and Earthdata credential path.** The long pole.

## Time-critical context

Reprocessing of the entire EMIT catalog begins around **September 2026** and takes roughly
**75 days**, regenerating every mineral map against Tetracorder 6 with updated reflectance. The
mineral classes shift.

For those ~10 weeks the archive is **mixed-vintage**, and any run that does not pin a vintage will
silently blend two incompatible products into a plausible-looking result. Vintage pinning is
therefore mandatory in the manifest, not advisory —
[02 §3](docs/specs/02-granule-index.md), [09 §5](docs/specs/09-run-manifest.md).
