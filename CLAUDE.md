# Stratum — working notes for Claude

Cost-function-driven mosaic engine for imaging spectroscopy. **Design stage: there is no
implementation yet.** Everything in `docs/` is specification.

Read [`docs/specs/00-overview.md`](docs/specs/00-overview.md) before doing anything substantive;
it fixes the vocabulary the rest of the repo assumes.

---

## The prime directive: specs, docs and code stay in sync

This repo's whole value right now is that the specs are accurate. They are read by people who are
not going to check them against the code.

There are now **three** places a design fact can live, and a change to one that misses the others
is worse than not writing it down at all &mdash; a confidently wrong document outlives a missing
one.

| Layer | Where | Role |
|---|---|---|
| **Specs** | `docs/specs/*.md` | The contract. Carries citations, invariants, open questions. **Authoritative.** |
| **Design site** | `docs/*.html` | The narrative. Explains shape and rationale to someone new. |
| **Code** | *(none yet)* | Authoritative for signatures once it exists. |

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
| Anything a reader of the design site would now find **wrong** | the matching HTML page — see the map below |

Once code exists, `11-types.md` describes the types **and the code is authoritative**. Keep the
spec as the narrative — why the type is shaped that way, what the observed constraints are — and
do not duplicate field lists that will drift.

### Spec → site map

Not every spec edit needs a site edit. The site carries the *shape* of a decision; the spec carries
its detail. Update the site when the shape changes, a name changes, or a claim on the page becomes
false.

| Spec | Site page |
|---|---|
| `00-overview`, `01-grid-tiling`, `11-types` §1–2, §9 | [`docs/design/concepts.html`](docs/design/concepts.html) |
| `01` §3, `02`, `03` | [`docs/design/architecture.html`](docs/design/architecture.html) |
| `05`, `06` | [`docs/design/caching.html`](docs/design/caching.html) |
| `08`, `09` §4 | [`docs/design/execution.html`](docs/design/execution.html) |
| `04`, `07` | [`docs/reference/plugins.html`](docs/reference/plugins.html) |
| `11-types` | [`docs/reference/types.html`](docs/reference/types.html) |
| `09`, `10` | [`docs/reference/manifest.html`](docs/reference/manifest.html) |
| Any ADR | [`docs/decisions/index.html`](docs/decisions/index.html) |
| Any spec's **open questions** | [`docs/status.html`](docs/status.html) |

The last row is the one most easily forgotten. `status.html` rolls up every spec's open questions,
so resolving one means striking it there too — otherwise the page slowly fills with questions
that were answered months ago and nobody trusts it.

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
docs/index.html    design site landing page (GitHub Pages serves docs/)
docs/design/       concepts, architecture, caching, execution + the original proposal
docs/reference/    plugins, types, manifest
docs/decisions/    ADR digest (HTML) + the ADRs themselves (Markdown)
docs/status.html   rolled-up open questions and the first slice
docs/assets/       stratum.css, stratum.js - the only shared chrome
docs/specs/        00-11, numbered by dependency order   <- authoritative
docs/notes/        meeting notes, with attribution caveats
refs/              verbatim external artifacts - do not edit
```

---

## Working on the design site

**There is no build step and there must not be one.** Plain HTML, one stylesheet, one script.
It has to open correctly from `file://` as well as from Pages, so: no ES modules, no `fetch`, no
CDN dependencies, relative links only.

`docs/.nojekyll` is there on purpose. Without it GitHub Pages runs Jekyll, which would rewrite
`specs/*.md` to `.html` and break every link from the site into the specs.

**The nav model lives in exactly one place** — `PAGES` in `docs/assets/stratum.js`. The sidebar,
the active-page highlight and the prev/next pager all derive from it. Adding a page means one
entry there plus the file; do not hand-write a sidebar into a page.

Each page sets two attributes on `<body>`:

```html
<body data-page="design/concepts" data-root="../">
```

`data-page` must match a nav `id`. `data-root` is `""` at the top level and `"../"` one level
down. Getting either wrong breaks the highlight or every link on the page, silently — so after
adding or moving a page, run the link check:

```bash
python3 - <<'EOF'
import pathlib, re
root = pathlib.Path('docs')
for f in sorted(root.rglob('*.html')):
    for m in re.finditer(r'(?:href|src)="([^"#]+)"', f.read_text()):
        u = m.group(1)
        if u.startswith(('http', 'mailto:')): continue
        if not (f.parent / u.split('#')[0]).resolve().exists():
            print('BROKEN', f, u)
EOF
```

**Use the existing components.** `stratum.css` has `.key` / `.note` / `.warn` callouts,
`.chip-locked|draft|open|obs` status pills, `.gen` for generated-later regions, `.cards`,
`.stages`, and `.tw > table` for scrollable tables. Reach for one of those before inventing a
class; a one-off style in one page is how a docs site starts looking like four docs sites.

### The generation boundary

Some of this will be generated from code later. The dividing line:

> **Anything with a signature is a generation candidate. Anything that explains a choice is not.**

| Will be generated | Stays hand-written |
|---|---|
| Type field lists (from the dataclasses) | Why the type is shaped that way |
| Manifest field reference (from the Pydantic models) | The worked example manifest |
| `Protocol` blocks (from the protocol definitions) | The worked plugin examples and their rationale |
| Plugin registry listing (from entry points) | Which plugin to reach for and when |

Mark any region that will later be generated with a `.gen` block, so a future generator author
knows what it is allowed to overwrite — and so a reader knows which parts to trust less once code
exists. **Do not** add a generator now; the pages have to survive being hand-edited until there is
something to generate them from.

### Style on the site

The same rules as the specs, plus two:

- **Every claim about existing code names its source.** The site is where a claim gets read
  without the spec beside it, so an unattributed assertion here does more damage.
- **Status chips are load-bearing.** `locked` means someone may now build on it. Do not mark
  something locked to look decisive.

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
