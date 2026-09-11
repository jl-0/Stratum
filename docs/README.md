# Stratum documentation

Three layers, with different audiences. Keep them in step — see [`../CLAUDE.md`](../CLAUDE.md).

| Layer | Format | Documents | Audience |
|---|---|---|---|
| **Site** | HTML, [`index.html`](index.html) | How the tool works and how to use it | Anyone using or operating Stratum |
| **Specs** | Markdown, [`specs/`](specs/) | Contracts, invariants, citations, open questions | Whoever implements a stage |
| **Notes** | Markdown, [`notes/`](notes/) | Heritage, meeting records, archived proposals | Internal |

The site describes the **current tool**. It does not argue for the design, compare against
predecessors, or explain what was replaced — that belongs in [`notes/heritage.md`](notes/heritage.md).

`guide/` and `reference/` document the tool for whoever **uses** it. `developer/` documents the
codebase for whoever **changes** it — module layout, execution flow, object lifetimes, extension
points. Nothing in `developer/` is needed to write a manifest or a plugin.

## The site

Open [`index.html`](index.html) locally, or browse it on Pages. No build step: plain HTML with one
shared stylesheet and one shared nav script.

```
index.html                what it does, the pipeline, where you plug in
guide/
  concepts.html           grid/tile/block, epochs & delivery, roles, spaces, nodata, class tables
  running.html            write a manifest, dry-run it, submit it, read the output
  reading-data.html       the block read path; sources, readers, staging, credentials
  algorithms.html         every arithmetic step, linked to the line that performs it
  plugins.html            authoring guide for all five science hooks
  caching.html            what invalidates what; inspecting the cache
  scaling.html            local, SLURM and AWS executors; credentials; Terraform's role
reference/
  manifest.html           every manifest field                        [generation candidate]
  types.html              the types plugins receive                   [generation candidate]
  cli.html                every command and option                    [generation candidate]
developer/
  codebase.html           package map, the layer graph, module by module, spec -> code
  lifecycle.html          plan -> work lists -> exec_item; the key-agreement contract
  objects.html            object lifetimes, PlanContext, frozen vs not, the JSON seam
  extending.html          registries, plugin resolution, adding a reader/source/stage/executor
  working.html            environment, the test suite, the invariants, conventions, footguns
decisions/index.html      ADR digest
status.html               what is implemented, what is open
assets/                   stratum.css, stratum.js — the only shared chrome
```

### Adding a page

1. Add one entry to `PAGES` in [`assets/stratum.js`](assets/stratum.js) — the single copy of the
   nav model; the sidebar and prev/next pager both derive from it.
2. Create the file. Copy the shell from a sibling and set `data-page` (matching the nav `id`) and
   `data-root` (`""` at the top level, `"../"` one level down).
3. Use the existing components in `stratum.css` — `.key` / `.note` / `.warn` callouts, `.chip-*`
   status pills, `.gen` for generated-later regions, `.cards`, `.stages`, `.tw > table`.
4. Run `pytest tests/test_docs_links.py tests/test_docs_developer.py` — the first checks every
   `<a class="src">` still points at the line it names, the second that `data-page` matches a nav
   `id`, that `data-root` is right, and (for `developer/codebase.html`) that no package in `src/`
   is undocumented and no documented module has been deleted.

`.nojekyll` is present on purpose: Jekyll would rewrite `specs/*.md` to `.html` and break every
link from the site into the specs.

### What gets generated later

Marked in-page with a `.gen` block. **Anything with a signature is a generation candidate; anything
that explains how to use it is not.**

| Region | Source once code exists |
|---|---|
| `reference/types.html` — field lists | The dataclass definitions |
| `reference/manifest.html` — field tables | The Pydantic models, via JSON Schema export |
| `reference/cli.html` — options | The click command tree |
| `guide/plugins.html` — `Protocol` blocks | The protocol definitions |

Prose, guidance and worked examples stay hand-written.

## Specifications

Authoritative. Numbered by dependency order, not build order.

| Spec | Covers |
|---|---|
| [00 — Overview](specs/00-overview.md) | Vocabulary, the five stages, invariants |
| [01 — Grid, tiling, blocks](specs/01-grid-tiling.md) | Grid definition, tile/block split, halos |
| [02 — Granule index](specs/02-granule-index.md) | Schema, queries, freezing, role resolution |
| [03 — Regrid and GLT](specs/03-regrid-glt.md) | GLT format, KD-tree, SpectralUtil boundary |
| [04 — Cost functions](specs/04-cost-functions.md) | All five science-hook contracts, execution modes |
| [05 — Ancillary data](specs/05-ancillary-data.md) | Aux readers, regridding, `AuxAccessor` |
| [06 — Caching](specs/06-caching.md) | Content addressing, keys, invalidation, sharing |
| [07 — Output mapping](specs/07-output-mapping.md) | `OutputMapper`, legends, data/image split |
| [08 — Execution](specs/08-execution.md) | Step Functions, Batch, Lambda, routing, failures |
| [09 — Run manifest](specs/09-run-manifest.md) | Schema, composition, budget, validation |
| [10 — Provenance](specs/10-provenance.md) | STAC, run records, reproducibility |
| [11 — Core types](specs/11-types.md) | Every shared type, fill/nodata rules, class tables |
| [12 — Data access](specs/12-data-access.md) | `GranuleSource`, `GranuleReader`, `AssetStore`, CMR, the block read path |
| [13 — Snapshot schema](specs/13-snapshot-schema.md) | Layers, enumerations, aggregation vocabulary, extend-never-redefine |

Specs cite the file a claim came from, and mark inferences as inferences. Where a contract exists
because of something observed in an existing pipeline, the citation is inline and the fuller
account is in [`notes/heritage.md`](notes/heritage.md).

## Decisions

| ADR | Decision |
|---|---|
| [0001 — Tech stack](decisions/ADR-0001-tech-stack.md) | Python, pixi, xarray, DuckDB, Step Functions, Terraform |
| [0002 — Terraform/manifest boundary](decisions/ADR-0002-terraform-manifest-boundary.md) | Platform vs science configuration |

## Notes — internal

| Note | Contents |
|---|---|
| [heritage.md](notes/heritage.md) | Prior art, **where the historical code lives**, and which observations forced which design choices |
| [2026-08-28-mines-tagup.md](notes/2026-08-28-mines-tagup.md) | External group building the same thing; detector-edge numbers, FRCOV, the bare-earth scorer |
| [2026-08-28-cloud-mosaic-proposal.html](notes/2026-08-28-cloud-mosaic-proposal.html) | The original research proposal. Archived; superseded by the specs. |

## Reference material

[`refs/`](../refs/) — artifacts from existing pipelines, kept verbatim. **Never edit them.**

| File | Source | Why it's here |
|---|---|---|
| `amd-config.yml` | `/store/jamesmo/amd/repo/configs/config.yml` | The only readable record of `stack` parameters, the lumping `hashmap`, and the RGBA `colors` table |
| `2026-08-28-mines-transcript.md` | Voice transcript | Source for the tag-up notes. Attribution is unreliable — see the caveat there |
| `EMIT_L2B_MIN_001_20260825T151308_2623710_050.nc` | Delivered LP DAAC granule, `V001` / build `010635` | Ground truth for spec 11. **Gitignored** — see `../CLAUDE.md` |

## Conventions

- The **site** documents behaviour. The **specs** state contracts and invariants. Neither
  duplicates the other.
- Every spec ends with **open questions**. An empty list means resolved, not unconsidered.
- Claims about existing pipelines cite the file they came from, and live in the specs or
  `notes/heritage.md` — not on the site.
- Values read from delivered data are marked **[observed]** in the specs and with an `observed`
  chip on the site.
