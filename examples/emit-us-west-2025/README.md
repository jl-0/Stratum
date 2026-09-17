# emit-us-west-2025: the grouping product, the eleven western states

Written against the **17 September 2026 Critical Minerals tag-up**. It exists to answer one
question with a running configuration instead of a slide: *how much of the mosaic product the
science leads described is expressible in Stratum today?*

All of it, as of 17 September 2026. Six manifests, all of them run.

| file | what it is |
|---|---|
| `manifest.yaml` | The delivered product: the **eleven western states**, all of 2025, both mineral groups ungrouped, three groupings over them, and four OBS bands from whichever granule won each cell. **Validates. ~$33-92 of Lambda — §3.** Full CONUS is the same manifest with a wider `bbox`. |
| `manifest-proof.yaml` | **The same configuration on one tile — 28 s.** 27 bands, 5 class tables, one publish. This is the one to run first. |
| `manifest-proof-native.yaml` | Both groups ungrouped, nothing else. 26 s. |
| `manifest-proof-alteration.yaml` / `-iron-oxide` / `-mineral-family` | One grouping each, for comparing a grouping against the ungrouped layer. 24-32 s. |
| `manifest-proof-bands.yaml` | The multi-band carry, proved two ways — §4. |
| `manifest-proof-abundance.yaml` | Band depth × XRD fraction per epoch, one constituent reaching two classes — §5. |

The proof manifests reuse `../emit-cmr-cuprite/`'s index and asset store, so they download
nothing. Run one:

```
pixi run stratum validate -m examples/emit-us-west-2025/manifest.yaml            # no data, no login
pixi run stratum run      -m examples/emit-us-west-2025/manifest-proof.yaml       # one tile, 28 s
```

---

## 1. What the tag-up asked for, and what the manifest says

**Two mosaics, not one.** *"The detections between group one and group 2 are independent from one
another… you cannot swap in a group 2 if a group one is absent."* So `mineral_1` and `mineral_2`
are separate roles feeding separate layers, and the manifest carries a comment saying why
`joint_mineral_vote` — which does exactly the forbidden thing — is **not** this product's reducer.
It stays in the repository as proof that a plugin can combine two categorical layers; it is not a
science-approved rule.

**Keep the ungrouped mosaic too.** *"We would need to retain the mosaic of the level 2B mineralogy
itself before grouping… that is what people want."* `classes: source` does that: the granules' own
294-entry `/mineral_metadata` becomes the product table, with no lumping at all. One band, not 294,
because Tetracorder assigns at most one constituent per group per pixel.

**Flood it with groupings.** *"The only way to do this is to basically flood it and give multiple
different groupings available."* Three are here, over the same detections:

| enumeration | group | cut by | classes |
|---|---|---|---|
| `iron-oxide-v1` | 1 | mineral — goethite / hematite / jarosite first, per Phil's "start with the easy ones" | 15 |
| `alteration-v1` | 2 | **alteration assemblage** — advanced-argillic, argillic, phyllic, propylitic: Dana's "groupings related to specific types of mineral deposit systems" | 17 |
| `mineral-family-v1` | 2 | plain mineral family — the same rows, cut differently | 16 |

A fourth is a YAML file, not a code change. All three ship in **one product**, each with its own
`classes.{layer}.json`, its own colour table and its own per-asset `classification:classes` — one
publish, 27 bands, 5 class tables. Until 17 September 2026 this was the one thing that did not
run: `publish/period.py` collapsed every categorical layer to a single product table and raised
`NotImplementedError` when they disagreed, which meant five products per tile where one would do.
**Class contents are provisional**: the structure is the deliverable, the bucket membership is the
science lead's call — *"for a first pass, don't worry about what goes into the buckets. Just
figure out what you want."*

**Annual, calendar-aligned.** `epoch: P1M`, `deliver: P1Y`. Adding 2026 later does not reprocess
2025: epoch snapshots are cached per (tile, epoch, block) and a widened window re-reads them
([06 §2](../../docs/specs/06-caching.md), §5) — the *"combine them at a higher level and not
reprocess everything"* ask. It holds only while the scorer, masks and schema hash the same.

**Every mask discussed.** `l2a_standard` (cloud, cirrus, water, spacecraft, dilated cloud) — *"the
mask layer, which is not applied yet"*. `soil_fraction` + `prefer_bare_earth` over EMITL2BFRCOV —
the vegetation component. `landcover` over ESA WorldCover aux — the land/water boundary.

---

## 2. Grouping before the vote measurably recovers ground

Phil: *"if we intelligently make those groupings as part of the mosaicing step, we will get cleaner
results."* Dana: *"if you're bopping around between like 4 different Alunites… the aggregation gives
you some robustness."* Measured on tile `-235_75` (Cuprite, 1800 × 1800 cells) over all of 2025,
`min_count: 3` of 12 monthly epochs, 24 contributing granules:

| layer | cells classified | % of tile | distinct classes |
|---|---:|---:|---:|
| group 1, ungrouped (294-class) | 1,075,998 | 33.2 % | 27 |
| group 1 → `iron-oxide-v1` | **1,273,749** | **39.3 %** | 6 |
| group 2, ungrouped (294-class) | 895,544 | 27.6 % | 63 |
| group 2 → `alteration-v1` | **1,260,551** | **38.9 %** | 11 |
| group 2 → `mineral-family-v1` | 1,259,075 | 38.9 % | 10 |

**Grouping before the vote recovers +18.4 % of classified area in group 1 and +40.8 % in group 2.**
The mechanism is exactly the one Dana described: a cell that saw four different alunites across the
year fails `min_count: 3` on the raw label and passes once they are one class. It is not a free
lunch — the same arithmetic hides genuine disagreement inside a bucket — which is why
`*_agreement` and `*_runner_up` ship beside every categorical band.

Leading classes on that tile, for a sanity check against the district:

```
alteration-v1        phyllic 43.2 %  carbonate 25.5 %  smectite 15.8 %  argillic 13.8 %
                     silica 1.0 %  advanced-argillic 0.7 %
iron-oxide-v1        hematite 63.9 %  goethite 21.2 %  fe-silicate 14.2 %  jarosite 0.7 %
```

Cuprite itself sits on this tile's southern edge and `min_soil: 0.65` is strict, so the low
advanced-argillic fraction is about where the alunite is, not evidence the grouping is wrong.
Science review is the science lead's.

---

## 3. What it costs

Measured against CMR on 17 September 2026, and against the pipeline itself at exactly this
geometry — one-degree tiles, `block_size: 720`, one arcsecond, KD-tree. Nothing below is
extrapolated from a half-degree run.

### The west is cheap because the west is clear

**Median cloud cover is 39 % over the western states, against 65 % over CONUS.** That is the whole
reason this scope is affordable: the filters keep 56 % of the granules here rather than 42 %, over
a third of the tiles.

| | intersecting the box | **after the filters** |
|---|---:|---:|
| EMITL2BMIN granules | 3,430 | **1,932** |
| One-degree tiles touched (of 414 nominal) | 406 | **402** |
| Mean tiles a granule lands in | 4.71 | 4.78 |
| regrid items (granule × tile) | 16,147 | **9,233** |
| Blocks / reduce items | 10,150 | **10,050** |
| resolve items (block × observed epoch) | 81,400 | **59,325** |
| publish items | 406 | **402** |
| Median months observed per tile | 8 of 12 | **6 of 12** |
| Granule bytes at 45.9 + 112.3 + 49 + 21.4 MB each | 784 GB | **442 GB** |

`min_count: 3` is three agreeing months of a median **six** observed — a real bar, and a workable
one. (Over full CONUS the same filter leaves a median of five, which is why the cloud threshold is
worth revisiting at continental scope and not here.) Raising `max_cloud_fraction` to 0.8 would give
2,562 granules and a median of 7 months, at about 30 % more compute.

**Solar zenith barely bites:** 0.1 % of granules exceed 70°. Keep the filter — it is what stops a
winter scene winning a cell — but it does no work here.

### Per-item cost, measured

A cold run of `emit-cmr-nevada` with the GLT cache busted (nudge `grid.max_distance`, which is the
only key term that changes), on one one-degree tile with 33 granules already staged — so these are
compute seconds, not download seconds — on an Apple M-series core:

| stage | s/item measured | note |
|---|---:|---|
| regrid | **18.3** mean, 18.8 median, 30.5 max | matches [08 §5](../../docs/specs/08-execution.md)'s "about 19 s single-threaded" |
| resolve | **0.37** mean | nevada's 3 layers, no aux, no mask, no FRCOV |
| reduce | **8.65** mean | 3 layers at `block_size: 720` |
| publish | **3.87** per tile | |

This manifest asks for more than nevada does — 8 layers, the L2A mask role, the landcover aux and
the FRCOV ortho warp — so resolve and reduce carry a range, bounded below by nevada and above by
this repository's aux-bearing Cuprite runs scaled per cell (`block_size` 450 → 720 is ×2.56):

| stage | items | low | high |
|---|---:|---:|---:|
| regrid | 9,233 | 47.0 CPU-h | 47.0 CPU-h |
| resolve | 59,325 | 13.2 | 86.5 |
| reduce | 10,050 | 24.1 | 58.6 |
| publish | 402 | 0.4 | 0.4 |
| **total** | **79,010** | **85 CPU-h** | **193 CPU-h** |

### The bill

A Graviton2 Lambda core at 4 GB runs this work roughly 2–2.5× slower than the M-series core above,
so 85–193 CPU-hours becomes **170–480 Lambda-hours**. At 4 GB and arm64's $0.0000133334 per
GB-second in `us-west-2`:

| line item | cost |
|---|---|
| **Lambda compute, one full pass** | **$33 – $92** |
| Lambda invocations (79,010 × $0.20/1M) | $0.02 |
| Data transfer in (LP DAAC → `us-west-2`) | $0 |
| S3 storage, everything retained: 845 GB | **$19 / month** |
| — raw granules, 442 GB | $10.16 / mo |
| — FRCOV ortho warps, 215 GB | $4.95 / mo |
| — epoch snapshots, 119 GB | $2.73 / mo |
| — GLT cache + product blocks, 48 GB | $1.10 / mo |
| — published products + aux, 22 GB | $0.52 / mo |
| S3 after cache eviction, products only | **$0.43 / month** |
| CloudWatch Logs, 30-day retention | < $1 |
| Preview viewer (Fargate, 1 vCPU / 2 GB) if left up | **$35 / month** — stop it between demos |

**One western pass: on the order of $60, inside the deployment's default $100 budget alarm.**

### Iteration is much cheaper than the first pass

This is the payoff for the regrid/resolve split. A GLT's cache key contains no scorer, no mask and
no schema ([06 §2](../../docs/specs/06-caching.md)), so **changing the cost function or the
groupings and rerunning re-reads 47 CPU-hours of cached geometry instead of rebuilding it**, and
re-downloads nothing. A second pass with a different scorer is **$15–50**; one that changes only
the enumerations reuses the epoch snapshots too and is cheaper still.

**The publish limit that used to cost real money is gone.** Five groupings in five separate runs
would repeat resolve and reduce (regrid and the 442 GB are shared, but both later stages are
schema-keyed): about 240 CPU-hours rather than 85–193, roughly **$95–120 instead of $60**, and
2,010 published products instead of 402. One product with five categorical layers is now the
cheaper *and* the simpler option.

### Scaling to CONUS

Same manifest, `aoi.bbox: [-125.0, 24.0, -66.0, 50.0]`, the 153-tile WorldCover list, and
`max_tiles`/`max_granules`/`max_vcpu_hours` raised. Measured fan-out: 8,462 granules indexed and
**3,550** surviving, **1,223** tiles, 17,801 regrid, 147,025 resolve, 30,575 reduce, 812 GB,
198–485 CPU-hours → **$76–233 of Lambda** and $39/month of S3. Roughly **4× the west for 2.5× the
tiles**, because the east is cloudier per unit area and the median tile drops to five observed
months.

### What would sharpen this

Two terms are ranges rather than numbers, and the western run itself fixes both: it measures resolve
and reduce per item under the real schema, mask role and aux, after which CONUS is arithmetic. The
standing warning against extrapolating fixed-cost-dominated runs applies to the $60 as much as to
anything else — watch `stratum status` over the first few hundred items and stop if regrid is not
landing near 18 s.

## 4. Many bands from whichever granule won the cell

A product can keep an arbitrary bundle of bands from the winning observation, and they are
coherent by construction rather than by convention. `src/stratum/resolve/block.py` computes **one**
boolean `take` mask from the scorer and then writes **every** layer under it:

```python
take = obs.valid & ~np.isnan(score) & (~won | (score > best))
...
for layer in plan.schema.layers:
    layers[layer.name][take] = values[take]
```

So within an epoch a cell's bands cannot come from different granules. `manifest-proof-bands.yaml`
proves it two ways: `geometry_stack` is **one layer with a band axis** carrying four bands of the
11-band L1B OBS cube, and `view_zenith` is a separate single-band layer over the same cube. Both
use `method: best`. Measured:

- band 1 of the stack is **bit-identical** to the separate `view_zenith` layer across
  **2,182,910 cells** (max absolute difference 0.0)
- all **250** distinct delivered UTC-time values match a real granule acquisition hour to within
  three minutes — so the bundle names one granule, not a blend
- physical ranges are sane: path length 415.7–426.6 km (EMIT on the ISS), to-sun zenith 17–60°,
  to-sensor zenith 5.2–12.1°

**One caveat that is easy to miss.** `method: best` takes the best-*scoring* epoch; `vote` takes
the *modal* epoch. Those can be different epochs, so a `best`-aggregated band bundle is coherent
within its own observation but is not guaranteed to come from the epoch that supplied
`mineral_1_native`. Where that matters, condition the continuous layer on the categorical one —
`conditional_on`, as `depth_1` does.

**A bug this surfaced and fixed.** An epoch in which nothing won a multi-band layer wrote an
`(H, W)` plane for a layer whose other epochs are `(H, W, B)`, and reduce then died with
`all input arrays must have the same shape` — *after* resolve had succeeded and written the
snapshots. `resolve/block.py` now writes the declared band count, and `stack_snapshots` widens an
all-nodata narrow plane (and refuses to widen one that holds data). Two regression tests in
`tests/test_resolve.py`.

---

## 5. Spectral abundance: band depth × XRD fraction, per epoch

The two things the schema vocabulary cannot say, and they turn out to be one thing:

> *"If I were going to take time into play, I would absolutely apply the mineral abundance
> calculations a priori… that takes us from a 294 binary to a 13 component continuous basis."*
>
> *"Some of the samples are shared between those categories, so we just split out and we say,
> yeah, we have a detection in each one of these."*

An `Enumeration` can do neither. It maps one raw class to exactly one product class with no
weight — `classes.py:resolve` raises `raw key 218 claimed by both 'carbonate' and 'smectite'`
(verified verbatim) — so there is nowhere to put a fraction and no way for a mixture to reach two
outputs. The `spectral_abundance` **Reducer** can, because it sees every epoch's winning class and
its band depth before anything collapses:

```
abundance[class, epoch] = depth[epoch] × fraction(winning constituent → class)
```

then aggregates over the epochs in which the class was **detected**, never over the zeros —
averaging in the months a mineral was absent would scale every abundance by how often the cell was
looked at, which is revisit and not geology.

`manifest-proof-abundance.yaml` runs it with the real EMIT-10 XRD columns
(keyed by the constituent name the granule's own `/mineral_metadata` reports, **inline** under
`reducer.params.weights` — see the note below). Measured on tile `-235_75`:

| band | cells | range | median |
|---|---:|---|---:|
| `abundance_goethite` | 1,132,453 | 0.0001–0.1735 | 0.0031 |
| `abundance_hematite` | 1,140,750 | 0.0000–0.1050 | 0.0017 |
| `abundance_total` | 1,584,406 | 0.0000–0.1735 | 0.0035 |

**688,797 cells carry both**, and on **466,258** of them the goethite/hematite ratio is exactly
**2.0** — which is 0.10/0.05, the two fractions the shared constituent
`nHematit+fg-Goethit 34B2+MPC` contributes. That is one constituent reaching two output classes at
different weights, which is the thing an enumeration refuses.

**It is an index, not a mass fraction.** The L3 ASA mass-fraction model — mean optical path length,
grain size, a quartz/feldspar term — is a further step on top and is not implemented.

**And it is a different product from the grouping one.** A `Reducer` plugin's `outputs` *replace*
the schema-derived bands, so one run delivers either the grouping product or the abundance product,
not both. That is a real constraint on the delivery shape, not an oversight.

---

## 6. Running it smaller

Even the west is an `--executor aws` job at 79,010 work items. To try the shape locally, cut the
AOI to one state and drop the budget to match — Nevada, say:

```yaml
aoi:
  bbox: [-120.0, 35.0, -114.0, 42.0]
budget:
  max_tiles: 42
  max_granules: 900
```

That is 42 one-degree tiles and tens of GB rather than 442. Always `stratum index build` first
(metadata only, no login, seconds), then `plan` to see the real granule count and staged bytes
before anything large downloads.

## 7. Generated files, and which of them the framework reads

| path | read by the framework? | how |
|---|---|---|
| `classes/*.yaml` | **yes** | a categorical layer's `classes: "@ref:classes/….yaml"`, resolved at plan time into `plan.json`'s `context.schema` |
| `weights/abundance-group1-v1.yaml` | **no** | provenance only. Reducer params are free-form by contract, so there is no `@ref:` for them; the numbers appear **inline** under `reducer.params.weights` and that copy is what hashes and what a worker sees |

That asymmetry is worth knowing before you edit a weight: changing `weights/` changes nothing,
changing the manifest changes the `manifest_hash` and therefore the product directory. Verified —
moving that file from `classes/` to `weights/` left the run id byte-identical.

`classes/*.yaml` are generated from `EMIT-Data-Resources/data/mineral_grouping_matrix_20230503.csv`
by ordered filename rules — the first pattern a constituent's expert-system path matches wins, which
is how a partition is produced from overlapping mineralogical language. The matrix was verified row
for row against a real granule's `/mineral_metadata`: 294 rows, same index, record, library and
group throughout. Members are emitted as `{library, record, group}` — attributes the product carries
— never as the positional index, so a re-delivered table with different integers still resolves.
