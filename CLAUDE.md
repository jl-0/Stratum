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
| **Site** | `docs/*.html` | How the tool works and how to use it. |
| **Heritage** | `docs/notes/heritage.md` | Prior art, where the old code lives, why choices were made. **Internal.** |
| **Code** | *(none yet)* | Authoritative for signatures once it exists. |

**When you make a design decision, record it in the same commit that makes it.**

| If you change… | Update |
|---|---|
| Any shared type (`ObsWindow`, `AuxAccessor`, `GLT`, `SnapshotStack`, `SnapshotSchema`, `LayerSpec`, `BandStack`, `GranuleRef`, `ClassTable`, `SensorWindow`) | [`docs/specs/11-types.md`](docs/specs/11-types.md) — **always**, no exceptions |
| A plugin contract | [`04-cost-functions.md`](docs/specs/04-cost-functions.md), and [`07`](docs/specs/07-output-mapping.md) for `OutputMapper` |
| How granules are found, fetched or read (`GranuleSource`, `GranuleReader`, `AssetStore`) | [`12-data-access.md`](docs/specs/12-data-access.md) |
| What goes into a cache key | [`06-caching.md`](docs/specs/06-caching.md) |
| The snapshot schema — layers, enumerations, aggregation vocabulary, extension rules | [`13-snapshot-schema.md`](docs/specs/13-snapshot-schema.md) |
| The manifest schema | [`09-run-manifest.md`](docs/specs/09-run-manifest.md) |
| Fill/nodata handling anywhere | [`11-types.md` §2](docs/specs/11-types.md) — the single source of truth |
| A dependency, or how it's packaged | [`ADR-0001`](docs/decisions/ADR-0001-tech-stack.md) |
| Anything that moves a boundary between platform and science config | [`ADR-0002`](docs/decisions/ADR-0002-terraform-manifest-boundary.md) |
| Anything a reader of the design site would now find **wrong** | the matching HTML page — see the map below |

Once code exists, `11-types.md` describes the types **and the code is authoritative**. Keep the
spec as the narrative — why the type is shaped that way, what the observed constraints are — and
do not duplicate field lists that will drift.

### Spec → site map

Not every spec edit needs a site edit. The site carries observable *behaviour*; the spec carries
the contract and the reasoning. Update the site when behaviour changes, a name changes, or a
statement on the page becomes false.

| Spec | Site page |
|---|---|
| `00-overview`, `01-grid-tiling`, `11-types` §1–2, §9 | [`docs/guide/concepts.html`](docs/guide/concepts.html) |
| `02-granule-index`, `09-run-manifest`, `10-provenance` | [`docs/guide/running.html`](docs/guide/running.html) |
| `12-data-access`, `02-granule-index` §5–6, `03-regrid-glt` §4 | [`docs/guide/reading-data.html`](docs/guide/reading-data.html) |
| `04-cost-functions`, `05-ancillary-data`, `07-output-mapping` | [`docs/guide/plugins.html`](docs/guide/plugins.html) |
| `06-caching` | [`docs/guide/caching.html`](docs/guide/caching.html) |
| `13-snapshot-schema` | [`docs/guide/plugins.html`](docs/guide/plugins.html) (what the snapshot carries, reducer) and [`docs/reference/manifest.html`](docs/reference/manifest.html) (`snapshot`) |
| `08-execution`, `ADR-0002` | [`docs/guide/scaling.html`](docs/guide/scaling.html) |
| `09-run-manifest` (fields) | [`docs/reference/manifest.html`](docs/reference/manifest.html) |
| `11-types` | [`docs/reference/types.html`](docs/reference/types.html) |
| Any CLI surface | [`docs/reference/cli.html`](docs/reference/cli.html) |
| Any ADR | [`docs/decisions/index.html`](docs/decisions/index.html) |
| Any spec's **open questions** | [`docs/status.html`](docs/status.html) |
| A finding about an existing pipeline | [`docs/notes/heritage.md`](docs/notes/heritage.md) — **not** the site |

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
- **Neither a collection version nor a build number is a vintage.** `CollectionReference.Version`
  is constant across a CMR collection. `AdditionalAttributes.SOFTWARE_BUILD_VERSION` is granule-level
  and exposed before download (verified 2026-09-01), but `EMITL2BMIN.001` already spans eight
  builds with no Tetracorder change. The vintage check is the embedded class-table fingerprint;
  build version is filterable and reported — see [`02-granule-index.md` §3](docs/specs/02-granule-index.md).
- **Masks run in resolve, not regrid.** Regrid reads `loc` only and the GLT key has no mask term.
  Sensor-space masks apply to the sensor window before the gather, map-space masks to the block
  after it — [`03` §5](docs/specs/03-regrid-glt.md), [`12` §2](docs/specs/12-data-access.md).
- **Lumping runs at the gather in resolve**, into the snapshot schema's enumeration, so snapshots
  hold product ids and never raw Tetracorder classes. `ignore` names classes; `none` is the reserved
  id 0 — [`13` §3](docs/specs/13-snapshot-schema.md).
- **`PGEVersionClass.PGEVersion` is not the build.** It is `v1.3.1` on every L2B granule 2022–2026.
- **`CloudCover` is top-level in EMIT UMM-G**, not an `AdditionalAttribute`, and present on every
  L2B MIN granule. One L2B record carries two files, `MIN` and `MINUNCERT`, so a role may name an
  `asset:` within a collection.
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
docs/index.html    site landing page (GitHub Pages serves docs/)
docs/guide/        concepts, running, reading-data, plugins, caching, scaling  <- how to use it
docs/reference/    manifest, types, cli                             <- field/API reference
docs/decisions/    ADR digest (HTML) + the ADRs themselves (Markdown)
docs/status.html   what is implemented, what is open
docs/assets/       stratum.css, stratum.js - the only shared chrome
docs/specs/        00-13, numbered by dependency order   <- authoritative
docs/notes/        heritage.md, meeting notes, archived proposal    <- internal
refs/              verbatim external artifacts - do not edit
trial-data/        local granules for trial runs - git-ignored, never committed
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
`.chip-locked|draft|open|obs` status pills, `.gen` for generated-later regions, `.reading` for
further-reading blocks, `.cards`, `.stages`, and `.tw > table` for scrollable tables. Reach for one
of those before inventing a class; a one-off style in one page is how a docs site starts looking
like four docs sites.

### The wordmark

The mark beside "Stratum" is an eight-point compass rose with an open centre, defined **once** as
`--compass` in `stratum.css` and applied to `.glyph` with `mask`. It is a single flat colour, which
is what lets it be a mask and inherit `--accent` in both themes with no second copy.

Do not inline SVG into the pages, and do not add a per-theme variant. If you redraw it, **check it
at 19px** — its real size — not just large. Two earlier attempts failed there: a quartz crystal
that read as a city skyline, and a thin-armed rose that vanished. Render candidates side by side at
140/64/32/19px before choosing; a scratch preview page under `docs/` works, but delete it before
committing.

### Name a term where it is first used

`halo` is the API field name on `Scorer`, `Reducer` and `PixelMask`, so the docs use that word and
not a synonym — a doc term that disagrees with the code is worse than an unfamiliar one. What went
wrong once was **ordering**: "Halo overhead" appeared as a row in the block-size table one section
before halos were defined.

The rule: if a term appears in a table or list, it is defined before that table, or glossed inside
it. Prefer reordering the sections over inventing a friendlier synonym.

### Assume no GIS background — but do not let it take over the page

Readers include software engineers with no geospatial training. A section that leans on a domain
concept — CRS, EPSG, resampling kernels, push-broom geometry, COG internal tiling — must not assume
it. But the guide pages document **Stratum**, so background cannot be allowed to become the
narrative.

The split:

| Content | Where it goes |
|---|---|
| What the concept means *for Stratum* — the field, the unit, the failure it causes | Visible body text |
| What the concept *is*, for someone meeting it for the first time | `<details class="explainer">`, **collapsed** |
| Links to authoritative sources | Inside that expander, as a `.reading` block |

`guide/concepts.html` is the model. "Resolution is in the CRS's units, and confusing degrees with
metres is the most common error here" stays visible, because it explains a guard rail. "What is a
CRS, and what does `EPSG:4326` mean?" collapses.

```html
<details class="explainer">
<summary>What is a CRS, and what does <code>EPSG:4326</code> mean?</summary>
<div class="body">
  <p>…</p>
  <div class="reading">…</div>
</div>
</details>
```

Rules: **no `<h2>` for background material** — an expander, never a heading, so the page outline
stays a list of Stratum concepts. Phrase the summary as the question a newcomer would actually ask.
Make the body worth opening: a definition plus why it matters here, not just links.

A short standalone `.reading` block with no explainer is still fine at the end of a section on the
other pages, where it is a pointer rather than a lesson.

```html
<div class="reading">
  <span class="label">Further reading</span>
  <ul>
    <li><a href="https://epsg.org/">EPSG Geodetic Parameter Dataset</a>
        <span class="what">— the authoritative registry, maintained by IOGP</span></li>
  </ul>
</div>
```

Rules for these:

- **Prefer the standard body or the maintainer** — IOGP for EPSG, OGC for COG, PROJ/GDAL for
  transforms, CF for NetCDF metadata. Where a convenience site is genuinely more useful (epsg.io),
  link it *and say it is not authoritative*.
- **Say what the reader will get**, not just the title. "— what nearest, bilinear and mode actually
  do, and when each is wrong" earns the link; a bare URL does not.
- **Verify every URL before committing.** Dead links in docs are worse than no links:

  ```bash
  python3 - <<'EOF' | while IFS= read -r u; do
    printf "%-4s %s\n" "$(curl -sL -o /dev/null -w '%{http_code}' --max-time 15 "$u")" "$u"
  done
  import pathlib, re
  print("\n".join(sorted({m.group(1)
      for f in pathlib.Path('docs').rglob('*.html')
      for m in re.finditer(r'href="(https?://[^"]+)"', f.read_text())})))
  EOF
  ```

- **Annotate config examples for a newcomer.** The grid block in `guide/concepts.html` is the model:
  every field gets a comment saying what it is, what its units are, and what goes wrong if it is
  set carelessly. A bare YAML block that is obvious to us is not obvious to the reader we are
  writing for.

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

### What the site is for

**The site documents the tool as it is. It does not argue for it.**

This is the rule most easily broken, because the reasoning is interesting and the heritage is
fresh. It still does not belong there. A user reading `guide/plugins.html` needs to know that a
`PixelMask` returns a boolean and a `Scorer` returns a number — not which prior pipeline conflated
them.

| Belongs on the site | Belongs in `notes/heritage.md` |
|---|---|
| What a field does, what it defaults to | Why the default is that value |
| A constraint the user must satisfy | Which pipeline taught us the constraint |
| Behaviour, limits, error conditions | Comparisons with V002 / AMD / SpectralUtil |
| Worked examples | The code the example was derived from |
| EMIT data facts a user needs (extents, fill values) | How those facts were established |

Concretely: **no guide or reference page should name V002, EMIT-AMD, `pipeline.sh`, `watch.sh`,
`SpectralUtil` or a cluster path.** If you are about to write "unlike the existing pipeline", stop
— either state the rule on its own terms, or put the comparison in `heritage.md`.

`status.html` is the one exception, and only for naming a **parity or validation target** ("the
first slice must reproduce the existing V002 output"). That is a statement about project state,
not a justification. It still does not explain what V002 does or why we differ.

Where a constraint is genuinely EMIT-specific and a user needs it (detector-edge trimming, `-9999`
vs `0`), state it as a property of the data, not as a story about who discovered it.

### Style on the site

The same rules as the specs, plus two:

- **Write for someone doing the task**, not someone evaluating the design. Second person, present
  tense, concrete.
- **Status chips are load-bearing.** `locked` means someone may build on it. Do not mark something
  locked to look decisive.

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
file. Do not add further large binaries without asking. Test fixtures resolve from URLs supplied through
configuration, not from files in the repo; `trial-data/` holds local granules for trial runs and is
ignored; the reference granule in `refs/` goes once trial data exists.

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

- **Refer to pipeline stages by name** — plan, regrid, resolve, reduce, publish — never by
  number. The numbers exist only in the flow diagram in `00-overview.md` §2, to show order.
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
