# Stratum — working notes for Claude

Cost-function-driven mosaic engine for imaging spectroscopy. **Design stage: there is no
implementation yet.** Everything in `docs/` is specification.

Read [`docs/specs/00-overview.md`](docs/specs/00-overview.md) before doing anything substantive;
it fixes the vocabulary the rest of the repo assumes.

---

## The prime directive: specs and code stay in sync

This repo's whole value right now is that the specs are accurate. They are read by people who are
not going to check them against the code.

**When you make a design decision, record it in the same commit that makes it.**

| If you change… | Update |
|---|---|
| Any shared type (`ObsWindow`, `AuxAccessor`, `GLT`, `SnapshotStack`, `BandStack`, `GranuleRef`, `ClassTable`) | [`docs/specs/11-types.md`](docs/specs/11-types.md) — **always**, no exceptions |
| A plugin contract | [`04-cost-functions.md`](docs/specs/04-cost-functions.md), and [`07`](docs/specs/07-output-mapping.md) for `OutputMapper` |
| What goes into a cache key | [`06-caching.md`](docs/specs/06-caching.md) |
| The manifest schema | [`09-run-manifest.md`](docs/specs/09-run-manifest.md) |
| Fill/nodata handling anywhere | [`11-types.md` §2](docs/specs/11-types.md) — the single source of truth |
| A dependency, or how it's packaged | [`ADR-0001`](docs/decisions/ADR-0001-tech-stack.md) |
| Anything that moves a boundary between platform and science config | [`ADR-0002`](docs/decisions/ADR-0002-terraform-manifest-boundary.md) |

Once code exists, `11-types.md` describes the types **and the code is authoritative**. Keep the
spec as the narrative — why the type is shaped that way, what the observed constraints are — and
do not duplicate field lists that will drift.

A **new decision that changes an ADR** gets a new ADR superseding it, not an edit. Edits are for
corrections.

---

## Evidence standards

The specs make a lot of claims about three existing pipelines and about delivered data. Those
claims are load-bearing and several turned out to be wrong on first pass.

1. **Cite the file.** `mosaic.py:349`, not "SpectralUtil does X".
2. **Mark inferences as inferences.** `11-types.md` uses **[observed]** for things read out of a
   real granule and **[design]** for choices. Keep that discipline; it is the difference between
   a spec and a plausible story.
3. **Verify before propagating.** Reports from subagents, meeting transcripts, and recollections
   have all been wrong here. Check the source.
4. **Voice transcripts have unreliable attribution.** See the caveat in
   [`docs/notes/2026-08-28-mines-tagup.md`](docs/notes/2026-08-28-mines-tagup.md) — attribute by
   content, keep timestamps, confirm anything load-bearing.

---

## Facts that are easy to get wrong

Established by reading code and data. Do not re-derive; do not assume the opposite.

- **`stack-glts` in SpectralUtil is not temporal aggregation.** It is first-wins gap fill.
  Mode-through-time exists nowhere we can read.
- **V002 and AMD have opposite architectures.** V002 fuses selection into regrid (streaming
  argmin, discards observations, cannot do temporal). AMD builds one GLT *per granule* and defers
  everything. Both are reductions over an observation stack; that unification is the core idea.
- **`-9999` ≠ `0`.** In a mineral ID, `-9999` is "not observed" and `0` is "observed, nothing
  identified". Conflating them fabricates agreement.
- **Products carry their own class tables; use them.** The L2B granule embeds `/mineral_metadata`
  (294 entries). Read the table from the granule being processed rather than a checked-in CSV — it
  cannot drift from the pixels it describes. Raw values are positional and differ between vintages
  (294 in the granule vs 312 in `v6.00a6.csv`), so match on **attributes**
  (`library`, `record`, `group`), never on the integer. See [`11-types.md` §9](docs/specs/11-types.md).
- **EMIT is a push-broom.** Every *column* is a different detector, so cross-track edges need
  trimming (7 columns each side) but along-track granule boundaries are a download artifact and
  need nothing.
- **`spec_io` has no L2B mineral reader** and no S3 support — the latter blocked by two guards
  (`os.path.exists`, `click.Path(exists=True)`), not by architecture.
- **Wrap SpectralUtil, never fork it.** EMIT-AMD depends on a personal fork for a CLI upstream now
  ships; do not repeat that.

**Layering.** `stratum` core knows nothing about Tetracorder, minerals, spectral libraries or EMIT.
It knows a band may have a class table, where config says to find it, and how to check that
several agree. Everything domain-specific lives in `stratum_emit`. If you find yourself writing
"mineral" in a core module, it belongs in the plugin.

---

## Repo layout

```
docs/design/     the architecture proposal (HTML) - the "why"
docs/specs/      00-11, numbered by dependency order
docs/decisions/  ADRs
docs/notes/      meeting notes, with attribution caveats
refs/            verbatim external artifacts - do not edit
```

`refs/` holds things we did not write: a real L2B granule, AMD's `config.yml`, a meeting
transcript. **Never edit them.** They are evidence.

`refs/EMIT_L2B_MIN_*.nc` (~52 MB) is **present locally but excluded by `.gitignore`** — an open
decision, not an oversight. It is the ground truth behind `11-types.md` and the obvious reader
fixture, but 52 MB is permanent weight in a repo and GitHub warns above 50 MB. Options are Git
LFS, a small extracted fixture (metadata + a spatial subset), or committing it as-is. Until that
is settled, re-obtain it from LP DAAC:

```
EMIT_L2B_MIN_001_20260825T151308_2623710_050.nc   # EMITL2BMIN.001, product_version V001,
                                                  # software_build_version 010635
```

Everything `11-types.md` derives from it is marked **[observed]**, so the spec stands without the
file. Do not add further large binaries without asking.

---

## Inspecting the reference granule

There is no project environment yet. To read the NetCDF:

```bash
export MAMBA_ROOT_PREFIX="$HOME/micromamba"
micromamba create -y -n emit-inspect -c conda-forge python=3.11 netcdf4
micromamba run -n emit-inspect python -c "
import netCDF4 as nc
d = nc.Dataset('refs/EMIT_L2B_MIN_001_20260825T151308_2623710_050.nc')
print(d.groups['mineral_metadata'].variables.keys())"
```

Once implementation starts this becomes `pixi run`, per
[`ADR-0001`](docs/decisions/ADR-0001-tech-stack.md).

---

## When implementation starts

The agreed first slice — deliberately narrow:

> One tile, one epoch, `MinViewZenith`, staged granules on local disk, output diffed against a
> V002 cell. **No** blocks, caching, Step Functions, Batch, or manifest patching.

Build order within it: types → L2B reader → regrid wrapper → resolve → publish.

Two tests are non-negotiable from the first commit that makes them meaningful
([ADR-0001 §10](docs/decisions/ADR-0001-tech-stack.md)):

1. **Seam equivalence** — block-wise output bit-identical to tile-wise. Without it, block
   decomposition silently corrupts any plugin with spatial extent.
2. **Cache-key sensitivity** — a scorer change invalidates snapshots and does *not* invalidate
   GLTs. This is the property the entire iteration story rests on.

---

## Style

- Specs state **contracts and invariants**, not implementations.
- Every spec ends with **open questions**. An empty list means resolved, not unconsidered.
- Prefer a table to a list when there are more than three parallel items.
- Do not pad. If a section has one sentence of content, it is one sentence long.

---

## Blocking context

- **Catalog reprocessing** starts ~Sept 2026 and runs ~75 days, regenerating everything against
  Tetracorder 6. Mineral classes shift. The archive is mixed-vintage throughout, so vintage
  pinning is mandatory. Phil has confirmed V002 only *adds* metadata, so designing against V001
  fields is safe — but V001-keyed *class tables* are not forward-compatible.
- **AWS access** is the long pole and blocks nothing in the first slice.
- Open questions per spec are listed at the end of each; the ones that block design are in
  [`README.md`](README.md).
