# Stratum documentation

## Start here

**[design/Cloud-Mosaic-Architecture.html](design/Cloud-Mosaic-Architecture.html)** — the
architecture proposal. Explains *why* the design looks like this, with references into the three
existing pipelines and the evidence behind each decision. Read before the specs.

## Specifications

Interfaces and contracts. Numbered by dependency order, not by build order.

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

## Decisions

| ADR | Decision |
|---|---|
| [0001 — Tech stack](decisions/ADR-0001-tech-stack.md) | Python, pixi, xarray, DuckDB, Step Functions, Terraform |
| [0002 — Terraform/manifest boundary](decisions/ADR-0002-terraform-manifest-boundary.md) | Platform vs science configuration |

## Reference material

[`refs/`](../refs/) — artifacts from the existing pipelines, kept verbatim.

| File | Source | Why it's here |
|---|---|---|
| `amd-config.yml` | `/store/jamesmo/amd/repo/configs/config.yml` | The only readable record of `amd stack` parameters (`mincount`, `ignore`), the lumping `hashmap`, and the RGBA `colors` table |

## Conventions

- Specs state **contracts and invariants**, not implementations.
- Every spec ends with **open questions**. An empty list means the spec is done, not that nobody
  thought about it.
- Claims about existing pipelines cite the file they came from. If a claim is an inference rather
  than something read directly, it says so.
