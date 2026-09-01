# Stratum documentation

This directory is both a GitHub Pages site and the source of record. Two layers, kept in sync:

| Layer | Format | Holds | Audience |
|---|---|---|---|
| **Design site** | HTML, [`index.html`](index.html) | The design — shape, rationale, what is locked | Anyone joining, reviewing, or deciding |
| **Component specs** | Markdown, [`specs/`](specs/) | Contracts, invariants, citations, per-spec open questions | Whoever implements a stage |

The site is the *narrative*; the specs are the *contract*. When they disagree, the specs win —
they carry the citations. See [`../CLAUDE.md`](../CLAUDE.md) for the rule on keeping them in step.

## The site

Open [`index.html`](index.html) locally, or browse it on Pages. No build step: plain HTML with one
shared stylesheet and one shared nav script.

```
index.html                landing — the question, five stages, five hooks, how to read
design/
  concepts.html           vocabulary, tile vs block, coordinate spaces, nodata, invariants
  architecture.html       why it looks like this — the evidence, and what it fixes
  caching.html            content addressing, cache keys, the iteration story
  execution.html          split plane, routing, state machine, failure handling
  Cloud-Mosaic-Architecture.html    the original research proposal, preserved
reference/
  plugins.html            all five hook contracts, with worked examples
  types.html              core types, with lock status                [generation candidate]
  manifest.html           the run manifest, validation, provenance    [generation candidate]
decisions/index.html      ADR digest
status.html               what is locked, what is open, the first slice
assets/                   stratum.css, stratum.js — the only shared chrome
```

### Adding a page

1. Add one entry to `PAGES` in [`assets/stratum.js`](assets/stratum.js) — that is the single copy
   of the nav model; the sidebar and the prev/next pager both derive from it.
2. Create the file. Copy the shell from any sibling page and set `data-page` (matching the nav
   `id`) and `data-root` (`""` at the top level, `"../"` one level down).
3. Use the existing components in `stratum.css` — `.key` / `.note` / `.warn` callouts, `.chip-*`
   status pills, `.gen` for generated-later regions, `.tw > table`. Do not invent one-off classes.

### What gets generated later

Marked in-page with a `.gen` block. The rule is that **anything with a signature is a generation
candidate; anything that explains a choice is not.**

| Region | Source once code exists |
|---|---|
| `reference/types.html` — field lists | The dataclass definitions |
| `reference/manifest.html` — field reference | The Pydantic v2 models, via JSON Schema export |
| `reference/plugins.html` — `Protocol` blocks | The protocol definitions |

The prose, the rationale and the worked examples stay hand-written. A generator cannot produce
"this threshold is 0.65 because grain-size retrieval falls apart below it".

## Specifications

Numbered by dependency order, not build order.

| Spec | Covers |
|---|---|
| [00 — Overview](specs/00-overview.md) | Vocabulary, the five stages, invariants |
| [01 — Grid, tiling, blocks](specs/01-grid-tiling.md) | Grid definition, tile/block split, halos |
| [02 — Granule index](specs/02-granule-index.md) | Schema, queries, freezing, role resolution |
| [03 — Regrid and GLT](specs/03-regrid-glt.md) | GLT format, KD-tree, SpectralUtil boundary |
| [04 — Cost functions](specs/04-cost-functions.md) | All five plugin contracts, execution modes |
| [05 — Ancillary data](specs/05-ancillary-data.md) | Readers, regridding, `AuxAccessor` |
| [06 — Caching](specs/06-caching.md) | Content addressing, keys, invalidation, sharing |
| [07 — Output mapping](specs/07-output-mapping.md) | `OutputMapper`, legends, data/image split |
| [08 — Execution](specs/08-execution.md) | Step Functions, Batch, Lambda, routing, failures |
| [09 — Run manifest](specs/09-run-manifest.md) | Schema, composition, budget, validation |
| [10 — Provenance](specs/10-provenance.md) | STAC, run records, reproducibility |
| [11 — Core types](specs/11-types.md) | Every shared type, fill/nodata rules, class tables |

## Decisions

| ADR | Decision |
|---|---|
| [0001 — Tech stack](decisions/ADR-0001-tech-stack.md) | Python, pixi, xarray, DuckDB, Step Functions, Terraform |
| [0002 — Terraform/manifest boundary](decisions/ADR-0002-terraform-manifest-boundary.md) | Platform vs science configuration |

## Meeting notes

| Note | Why it matters |
|---|---|
| [2026-08-28 — Colorado School of Mines tag-up](notes/2026-08-28-mines-tagup.md) | The catalog reprocessing window, concrete detector-edge masks, FRCOV as an input, and a second potential consumer of the framework |

## Reference material

[`refs/`](../refs/) — artifacts from the existing pipelines, kept verbatim. **Never edit them.**

| File | Source | Why it's here |
|---|---|---|
| `amd-config.yml` | `/store/jamesmo/amd/repo/configs/config.yml` | The only readable record of `amd stack` parameters (`mincount`, `ignore`), the lumping `hashmap`, and the RGBA `colors` table |
| `2026-08-28-mines-transcript.md` | Voice transcript, Mines tag-up | Source for the notes above. Attribution is unreliable — see the caveat in the notes |
| `EMIT_L2B_MIN_001_20260825T151308_2623710_050.nc` | Delivered LP DAAC granule, `V001` / build `010635` | Ground truth for [11 — Core types](specs/11-types.md). **Gitignored** — see `../CLAUDE.md` |

## Conventions

- Specs state **contracts and invariants**, not implementations.
- Every spec ends with **open questions**. An empty list means the spec is done, not that nobody
  thought about it.
- Claims about existing pipelines cite the file they came from. If a claim is an inference rather
  than something read directly, it says so.
- Observations from delivered data are marked **[observed]** in the specs and with an
  `observed` chip on the site.
