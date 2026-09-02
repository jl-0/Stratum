# Stratum

A cost-function-driven mosaic engine for imaging spectroscopy, built to run in the cloud.

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/jl-0/Stratum?quickstart=1)

Opens a codespace with the environment built and a walkthrough that runs a real mosaic from
NASA's archive — see [`.devcontainer/README.md`](.devcontainer/README.md).

> **Status: first slice built (2026-09-02).** A local run — staged granules, one grid, the
> streaming scorer, the built-in reducer, published COGs with STAC and provenance — exists and is
> tested, and so does a run from NASA's CMR catalogue with granules downloaded on first touch:
> see [`examples/emit-cmr-nevada/README.md`](examples/emit-cmr-nevada/README.md) for the credentials setup
> and the measured sizes and times. The cloud and cluster paths are specified but not built. See
> [`docs/status.html`](docs/status.html). "Stratum" is a working name.

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

## Five science hooks

| Hook | Stage | Decides |
|------|-------|---------|
| `GranuleFilter` | plan | Which granules are candidates — date range, cloud fraction, quality |
| `PixelMask` | regrid | Which pixels are eligible — cloud, cirrus, water, snow |
| `Scorer` | resolve | **Which observation wins in a cell** — the cost function proper |
| `Reducer` | reduce | How epochs collapse through time — mode, median, spread |
| `OutputMapper` | publish | What the result looks like — enum→colour, ramps, confidence alpha |

## Two access hooks

A separate tier, because they add a data source rather than change an answer. Keeping them apart is
what stops the science hook count drifting upward every time a format is added.

| Hook | Runs | Decides |
|------|------|---------|
| `GranuleSource` | index build | Where the catalogue of granules comes from — a directory, CMR, a STAC API |
| `GranuleReader` | every worker | How one product family's bytes become arrays |

A new instrument is a reader; a new archive is a source. See
[`12 — Data access`](docs/specs/12-data-access.md).

## Documentation

**Start at [`docs/index.html`](docs/index.html)** — how the tool works and how to use it. Open it
locally or browse it on GitHub Pages.

| | |
|---|---|
| [`docs/index.html`](docs/index.html) | The documentation site — concepts, running a mosaic, reading data, writing plugins, scaling, reference |
| [`docs/specs/`](docs/specs/) | Component specifications, `00`–`13`. **Authoritative** — contracts, invariants, citations |
| [`docs/decisions/`](docs/decisions/) | Architecture decision records |
| [`docs/notes/heritage.md`](docs/notes/heritage.md) | Internal: prior art, where the historical code lives, why choices were made |
| [`docs/notes/2026-09-02-first-slice-plan.md`](docs/notes/2026-09-02-first-slice-plan.md) | Internal: the build contract the first slice was written against — scope, module boundaries, internal signatures |
| [`refs/`](refs/) | Reference material from the existing pipelines, kept verbatim |

The site documents the tool. The specs state the contracts. Heritage and rationale stay in
`docs/notes/`. Keeping them in step is the first rule in [`CLAUDE.md`](CLAUDE.md).

## Relationship to existing code

| Repo | Relationship |
|------|--------------|
| `SpectralUtil` | **Wrapped, not forked.** Provides the KD-tree GLT build. Two upstream contributions wanted: a pluggable selection seam, and persisting the score band. |
| `emit-sds-l3` | V002 reference implementation, and the parity target for the first slice. No V002 output was available locally, so the first parity check ran against SpectralUtil's `build_obs_nc` over the same grid instead. |
| `EMIT-AMD` | Precedent for deferred reduction and for the lumping/colour config. Cluster-specific; not reusable directly. |
| `tetracorder-lite` | Upstream L2B producer. Its reference matrix is the source of the class table each granule embeds. |

File-level detail — which script does what, and which observations in it drove a design choice —
is in [`docs/notes/heritage.md`](docs/notes/heritage.md).

## Not only EMIT

Colorado School of Mines (with CMU and U. Wisconsin's Macrostrat) is independently building the
same thing: tiling Tetracorder mineral maps into state- and country-wide mosaics, with a QGIS
data stream, moving to AVIRIS-5, and aiming at cross-scale work from EMIT's 60 m down to UAV
imagery at 6–10 cm. See [the tag-up notes](docs/notes/2026-08-28-mines-tagup.md).

That makes "a framework, not an EMIT program" a present requirement rather than an aspiration.
Multi-instrument support is a design constraint from the start — which is also why input roles and
band aliases are resolved per collection rather than hard-coded.

## Open questions

None of these blocks the local run; the rolling list, with every spec's open questions, is
[`docs/status.html`](docs/status.html).

1. **Is `freq-N` in EMIT-AMD a frequency rank or a time period?** The `vote` reducer exists
   (`min_count`, `ignore`, `tie_break`); the question is now whether AMD's stack is a
   configuration of it or a different aggregation. One question to James.
2. **Mode over mineral ID directly, or over something continuous first?** Both directions are
   expressible today — `vote` over the class layer, continuous layers reduced conditionally on
   it — except classify-last, which waits on scorer-computed snapshot layers
   ([04 §4](docs/specs/04-cost-functions.md)). For Phil, who has leaned toward *not* reducing over
   binarized labels.
3. **AWS account, quota and Earthdata credential path.** The long pole for anything cloud;
   nothing local waits on it.

## Time-critical context

Reprocessing of the entire EMIT catalog begins around **September 2026** and takes roughly
**75 days**, regenerating every mineral map against Tetracorder 6 with updated reflectance. The
mineral classes shift.

For those ~10 weeks the archive is **mixed-vintage**, and any run that does not check class-table agreement will
silently blend two incompatible products into a plausible-looking result. The class-table
fingerprint check is therefore mandatory at plan time, not advisory —
[02 §3](docs/specs/02-granule-index.md), [09 §5](docs/specs/09-run-manifest.md).
