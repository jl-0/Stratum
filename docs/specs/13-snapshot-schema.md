# 13 — Snapshot Schema

**Status:** draft · **Depends on:** [04](04-cost-functions.md), [07](07-output-mapping.md), [11](11-types.md) ·
**Depended on by:** [06](06-caching.md), [09](09-run-manifest.md), [10](10-provenance.md)

One declaration of what a snapshot holds and how each layer collapses through time. The scorer
writes to it, the reducer reads from it, and neither of them defines it.

---

## 1. Why a schema and not two contracts

Before this spec the `Scorer` named the bands it carried into a snapshot and the `Reducer`
separately declared what it produced from them. The two had to be kept matched by hand, nothing
recorded the association, lumping had no place to run, and neither the lumping nor the class table
entered a cache key. A single schema fixes all four:

- The **scorer** decides *which observation wins*. The schema decides *what is recorded* about the
  winner. A scorer therefore works with any schema, and a schema with any scorer.
- The **schema** decides *how each layer is aggregated* through time. A `Reducer` plugin is needed
  only for logic the aggregation vocabulary cannot express.
- **Categorical layers carry the product's own enumeration.** Raw classes are resolved into it at
  the gather in resolve, so a snapshot never holds a raw Tetracorder index and every epoch is
  directly comparable with every other.
- **The schema is constant across a run.** Every epoch snapshot in a run has the identical layer
  set and enumerations. It may be *extended* between runs without rebuilding; it may not be
  *redefined* without rebuilding every epoch it changes (§5).

---

## 2. The declaration

```yaml
snapshot:
  name: cm-v1
  layers:
    mineral_1:
      kind: categorical
      source: mineral                          # a role, or a band alias - one namespace
      classes: "@ref:classes/cm-v1.yaml"       # the product enumeration - section 3
      aggregate: {method: vote, min_count: 3, ignore: [none], tie_break: highest_score}
    depth_1:
      kind: continuous
      source: mineral_depth
      aggregate: {method: median, conditional_on: mineral_1, spread: iqr}
    mineral_2:
      kind: categorical
      source: mineral_group2
      classes: "@ref:classes/cm-v1.yaml"
      aggregate: {method: vote, min_count: 3, ignore: [none]}
    depth_2:
      kind: continuous
      source: mineral_group2_depth
      aggregate: {method: inverse_variance, unc: depth_2_unc, conditional_on: mineral_2}
    depth_2_unc:
      kind: continuous
      source: mineral_uncert
      aggregate: {method: none}                # carried for depth_2; not delivered
    view_zenith:
      kind: continuous
      source: view_zenith
      aggregate: {method: none}
```

| Field | Meaning |
|---|---|
| `kind` | `categorical` or `continuous`. Decides the aggregation vocabulary available and how nodata is represented ([11 §2](11-types.md)) |
| `source` | Where the value comes from in the *winning* observation. A role name or a band alias; the two share one namespace ([09 §2](09-run-manifest.md)) |
| `dtype` | Optional. Defaults to the source band's dtype; categorical layers default to `uint16` |
| `bands` | Optional. Band indices to keep from a multi-band source; default all — see below |
| `classes` | Categorical only. The enumeration file, or `source` — §3 |
| `aggregate` | How the layer collapses through time — §4. `none` carries the layer without delivering it |

Two layers are added to every snapshot by the framework and cannot be declared: `score`, the
winning score, and `valid`, whether any observation occupied the cell ([04 §4](04-cost-functions.md)).

**One namespace for `source` and for `obs[...]`.** A scorer reads `obs["view_zenith"]` and a layer
declares `source: view_zenith`; both resolve through `inputs.roles` and `inputs.band_aliases`, which
may not collide. A role whose variable has one band is addressed by the role name; a multi-band
variable needs an alias. Naming something neither defines is a plan-time error.

**A layer may have a band axis.** A `source` that names a multi-band variable — a reflectance
cube, a set of abundance fractions — yields one layer of shape `(H, W, B)`, with one dtype and one
nodata across bands, and every continuous aggregation in §4 applies band-wise. A reflectance
mosaic is one layer, not several hundred. Categorical layers are single-band.

Multiple categorical layers are how ranked candidates are expressed. EMIT's L2B carries two mineral
groups per pixel, which is the `mineral_1` / `mineral_2` pair above; a product with a top-three
would declare three. Each layer aggregates independently (§7).

---

## 3. Categorical layers carry the product's enumeration

A categorical layer's `classes` file **is** the lumping of [07 §6](07-output-mapping.md), and it is
also the product's class table. It names every class the product can contain, with an explicit,
stable integer, and says which raw classes belong to it — by attribute, never by position:

```yaml
name: cm-v1
match_on: [library, record, group]
unmapped: fail                    # fail | <class name> - where unlisted raw classes go
classes:
  - {id: 1, name: goethite,
     members: [{library: sprlb06, record: 882,  group: 1},
               {library: splib06, record: 5736, group: 1}]}
  - {id: 2, name: hematite,
     members: [{library: splib06, record: 5880, group: 1}]}
  - {id: 3, name: pyrite,
     members: [{library: splib06, record: 2568, group: 1}]}
```

Three rules make it safe to carry across granules, vintages and runs:

1. **`id` is explicit and never reused.** `0` is reserved for `none` — "observed, nothing
   identified", which is a real observation ([11 §2](11-types.md)) — and is what `ignore: [none]`
   refers to. Nodata is `valid`, never a class.
2. **Resolution is per granule, at plan time.** Each contributing granule's embedded table is
   matched on `match_on`; a member matching zero or several rows is a plan-time error. The result
   is one raw→product map per granule, recorded in provenance.
3. **The remap is applied at the gather in resolve** ([12 §2](12-data-access.md)), where the
   granule is known. Snapshots therefore hold product ids. Nothing after resolve sees a raw class.

**`classes: source` adopts the input's own table.** For a single-vintage run, or a parity check
against an existing product, the enumeration is the contributing granules' embedded class table
itself, with no lumping. Every contributing table must then agree by fingerprint — there is no
enumeration to resolve differing tables into — and the product's class table is a copy of it.

A raw class no member claims is governed by `unmapped`: `fail` (the default) stops the plan and
lists them; a class name routes them there, so a product can declare an explicit `other` rather
than silently dropping what it does not track.

The resolution rules `stratum/classes.py` fixes:

- `class_table()` **includes the reserved `(0, none)` row**, so `ignore: [none]`, legends and the
  shipped product table all name it. Raw key `0` maps to product id `0` and is never reported as
  unmapped. Product ids are bounded `1..65534`, because categorical storage is `uint16` with
  `65535` as nodata (§4); `classes: source` refuses a raw table with keys above that.
- A raw row matched by two members is an error even when both belong to one class. Attribute
  equality crosses the YAML/pyarrow type gap by string comparison (`record: 882` matches `"882"`).
  Keys beyond the raw table's range map to unmapped (`-1`) rather than raising.
- **`classes: source`** is `identity_enumeration`: it matches on the key column *plus every
  attribute column*, so it resolves only against a row-for-row identical table — which is the
  fingerprint-agreement rule, and why `allow_mixed_vintage` cannot relax it; a real
  mixed-vintage run needs an `@ref:` enumeration. The product table is the first (sorted)
  granule's `(id, name)`; the other attribute columns survive only as the recorded raw
  fingerprint. Duplicate raw names are disambiguated as `name#key` (the delivered table has
  eleven names that appear twice, so twenty-two entries carry a suffix and a consumer matching
  names against the USGS library will not find `Azurite WS316 W1R1Ba#56` — match on
  `(library, record)` instead).
- **The remap is applied per layer at the gather**, through the planner's per-granule lookup
  (rule 3); an unmapped raw class makes the cell invalid for the whole observation. The
  block-space `ObsWindow` bands stay *raw*, because two layers may remap one source differently
  and a scorer is not supposed to interpret class integers; "nothing after resolve sees a raw
  class" is honoured by the snapshot, not by the observation.
- `plan.json` stores remaps per layer as `{tables: {raw_fingerprint: {lookup, enumeration}},
  granules: {granule_id: fingerprint}}` — one lookup per vintage, not per granule.

**What this does to the vintage check.** The plan-time rule in [02 §3](02-granule-index.md) —
contributing class tables must agree by fingerprint — remains the default. With a schema it has a
principled relaxation: tables that *differ* are admissible under `allow_mixed_vintage: true`
**provided every one of them resolves fully and unambiguously into the enumeration**. The
enumeration is the constant; the raw tables are allowed to move underneath it, and the run record
lists every raw fingerprint seen ([10 §2](10-provenance.md)).

---

## 4. Aggregation vocabulary

Everything below is executed by the framework's built-in reducer when `reducer:` is omitted from
the manifest. A `Reducer` plugin ([04 §5](04-cost-functions.md)) is for what this table cannot say.

### Categorical

| `method` | Parameters | Delivers |
|---|---|---|
| `vote` | `min_count`, `ignore` (class names), `tie_break: earliest \| latest \| highest_score \| nodata` | `<layer>`, `<layer>_agreement`, `<layer>_runner_up` |
| `best` | — | `<layer>` from the epoch with the highest `score` |
| `none` | — | Nothing. Carried for a custom reducer. A `none` categorical layer has no winner, so it **cannot** be the target of `conditional_on` — that needs `vote` or `best` |

`vote` follows the decision order in [04 §5](04-cost-functions.md) exactly. `agreement` is the
modal count over `n_epochs`, so ignored-but-observed epochs lower it, deliberately.

Defaults and semantics the built-in reducer fixes (`stratum/reduce/`), where the table above is
silent:

| Rule | Contract |
|---|---|
| Delivered categorical nodata | `65535`; `none` stays `0`. Categorical bands are `uint16`, continuous `float32` with `NaN`, counts (`n_epochs`, `<layer>_n`) `uint16` with no nodata, whatever `LayerSpec.dtype` says about the snapshot ([11 §2](11-types.md)) |
| `min_count` | `1` when omitted — the value under which a single epoch reduces to itself with no special case |
| `tie_break` | `nodata` when omitted: refuse to choose unless the schema says how. Among tied classes with equal tie keys the lowest product id wins, so the result is deterministic |
| `agreement` | `modal_count / n_epochs`, reported even where the layer is suppressed by `min_count` or a refused tie — it describes the tally, not the decision. `NaN` where `n_epochs == 0`; `0.0` where every valid epoch was ignored (the layer is then nodata) |
| `runner_up` | The second-most-frequent class under the same `tie_break`; nodata wherever the layer is nodata |
| `ignore` with one epoch | Every observed cell whose only class is ignored delivers **nodata**, with `agreement 0` and `n_epochs 1`: literal step 4 of [04 §5](04-cost-functions.md), and exactly the `0`-vs-nodata conflation [11 §2](11-types.md) warns about, surfacing at the product. On the trial tile that is 73 % of observed cells. Only `n_epochs` then separates "observed, nothing identified" from "never observed". Single-epoch products should say `ignore: []` — question 5 |
| `score_weighted` | weights `= score − min(score over the aggregated epochs) + 1e-6`: scores are only an ordering (`MinViewZenith`'s are negative), equal scores reduce to a plain mean, the lowest-scoring epoch gets weight ε |
| `inverse_variance` | Epochs whose uncertainty is `NaN` or `≤ 0` are dropped from both the estimate and `<layer>_n`; a single-band `unc` layer weights every band of a multi-band value layer |
| Non-finite values | Inside a valid epoch they are skipped by every continuous statistic but still count toward `<layer>_n`; a cell with nothing aggregated delivers `NaN` and count `0`. `best` (either kind) treats an epoch with `NaN` score as unusable |
| Epoch order | `reduce_stack` sorts epochs by `Epoch.start` internally rather than trusting array order, so `earliest`/`latest` are well defined |
| Masked input | A layer arriving as a masked array has its mask folded into that layer's validity beside `snaps.valid`; a negative value in a categorical layer under `valid` raises — sentinels must be masked before reduce |

`delivered_bands(schema)` derives `BandSpec.bands` from `LayerSpec.bands` (its length, else 1);
the schema alone cannot know a `bands: None` multi-band source's width, so the planner records
`band_counts` from the source `VarSpec` and publish passes it through ([11 §8](11-types.md)). A
continuous multi-band layer is delivered as `(H, W, B)`.

### Continuous

| `method` | Parameters | Use when |
|---|---|---|
| `median` | — | The safe default; robust to one bad epoch |
| `mean` | — | Outliers already excluded |
| `min` / `max` | — | Weakest or strongest expression seen |
| `percentile` | `p` | A robust "high" or "low" |
| `score_weighted` | — | The scorer's ordering is trusted |
| `inverse_variance` | `unc: <layer>` | An uncertainty layer exists — `group_N_band_depth_unc` does |
| `best` | — | The value from the highest-scoring epoch |
| `none` | — | Carried, not delivered |

Every method applies band-wise to a multi-band layer. Options any continuous method accepts:

| Option | Effect |
|---|---|
| `conditional_on: <categorical layer>` | Aggregate only over epochs whose class matches that layer's delivered winner. Required wherever a value is the property of a class — a band depth is the depth *of a mineral's feature*, and averaging it across epochs that found different minerals produces a number with no meaning ([04 §5](04-cost-functions.md)). Delivers `<layer>_n`, the concordant count |
| `spread: std \| iqr` | Also deliver `<layer>_spread` as a quality band |

### Framework outputs

`n_epochs` — epochs with `valid` true — is delivered once per product block regardless of schema.
Every continuous layer also delivers `<layer>_n`, the count it actually aggregated over, because an
estimate from two epochs and one from twelve must be distinguishable downstream.

Every aggregation masks on `valid` before it looks at a value. That is a framework guarantee, not a
plugin responsibility.

---

## 5. Extend, never redefine

The schema enters two cache keys, and the split is what keeps iteration cheap ([06 §2](06-caching.md)):

| Hash | Covers | Enters |
|---|---|---|
| `layers_hash` | Every layer's `name`, `kind`, `dtype`, `source`, `bands`, and for categorical layers every `(id, name)` in the enumeration **and** the enumeration document's fingerprint (`LayerSpec.lumping`: `match_on`, `unmapped`, every member) | The epoch-snapshot key |
| `aggregate_hash` | Every layer's `aggregate` block | The product-block key |

Changing an aggregation method re-runs reduce and publish only. Changing what a snapshot *holds*
re-runs resolve — from cached GLTs, never a regrid. Within that, three tiers:

| Change | Existing snapshots | Cost |
|---|---|---|
| **Append** classes to an enumeration — new ids, nothing renumbered or renamed | Still valid | None. Declare the predecessor in `extends` and the planner accepts its snapshots |
| **Add** a layer | Lack it | Resolve re-runs for every epoch that lacks the layer |
| **Redefine** — change a layer's `kind`, `dtype` or `source`; rename, renumber or remove a class; change a member's attributes so a raw class lumps differently; remove a layer | Invalid | Resolve re-runs for every epoch |

Re-lumping is in the invalid tier even when every id and name is unchanged: the members are what
decide which raw class lands in which product id, so a snapshot written under the old members
holds different values. That is why the enumeration fingerprint, not only the `(id, name)` table,
is in `layers_hash`. The `extends` probe below is specified but not yet built.

Append is the tier that makes a long-lived product practical, so it is mechanical rather than
trusted:

```yaml
snapshot:
  name: cm-v2
  extends: [cm-v1]              # by name; the planner records cm-v1's layers_hash beside it
```

On a snapshot miss under the current `layers_hash`, the planner probes the same key under each
listed ancestor and accepts a hit only after checking its `.inputs.json` against the current
schema: identical layers, and every shared enumeration id carrying the same name. Anything else is
a miss, reported by `stratum cache diff` as the field that moved. A schema is never *patched* into
compatibility; it either extends its ancestor or it does not.

> **One schema per run.** Every epoch in a run is written under the same `layers_hash`. Reducing
> epochs written under two schemas is refused at plan time, which is the whole point of having one:
> the reducer must never be handed a vector whose elements mean different things.

---

## 6. What this settles

| Question | Answer |
|---|---|
| Where lumping runs | At the gather in resolve, into the enumeration. Snapshots hold product ids |
| Lumping and class tables in cache keys | The enumeration is in `layers_hash`; every contributing raw table's fingerprint is in the snapshot's `.inputs.json` and in provenance |
| `ignore: [0, -4]` | Classes are named — `ignore: [none]` — and `-4` had no meaning here |
| `Scorer.carry` | Gone. Layers are the schema's; the scorer only scores |
| `Reducer.outputs` and `ModeThroughTime` | Derived from the schema by the built-in reducer. A plugin still declares its own outputs |
| The `obs[...]` namespace | One namespace: roles and band aliases, which may not collide |

---

## 7. Open questions

1. ~~Should a ranked candidate list be expressible as one layer with `rank: N` rather than N named
   layers?~~ **Resolved:** named layers. A `rank: N` sugar can expand to them later without changing
   the schema.
2. ~~Joint aggregation across layers — a vote in which `mineral_1` and `mineral_2` candidates pool —
   is not expressible in §4 and needs a custom reducer. Is it wanted?~~ **Resolved:** not in the
   vocabulary; a custom reducer if anyone wants it.
3. Classify-last over per-candidate evidence still depends on scorer-computed layers, which remain
   deferred ([04 §4](04-cost-functions.md)). When they arrive they are layers with
   `source: scorer` and need no new schema machinery.
4. ~~Is `extends` by hash the right handle, or should schemas carry a name and version with the hash
   recorded beside it?~~ **Resolved:** both. The classes file carries `name` and `version`, the
   manifest lists ancestors by name, and the planner records each ancestor's `layers_hash` beside it
   in `.inputs.json` and provenance.
5. Should `vote` deliver `none` (`0`) rather than nodata where every valid epoch was ignored? As
   built it follows [04 §5](04-cost-functions.md) step 4 literally, and a single-epoch product
   with `ignore: [none]` turns "observed, nothing identified" into nodata on most of the tile
   (§4). The alternative — deliver `0`, let `agreement` say it was ignored — keeps the
   `-9999`-vs-`0` distinction at the product surface. Until decided, the recommendation is
   `ignore: []` for single-epoch products.
6. Two class-table fingerprints are in flight: `plan.json`, the report and provenance carry the
   **raw** table's (every attribute column), while the STAC item's
   `stratum:class_table_fingerprint` is the **product** `(id, name)` table's. `classes.json`
   links them through its `source` field, but the STAC name suggests the vintage fingerprint.
   Name both, or ship both on the item ([10 §4](10-provenance.md)).
7. One product, one class table: `product_class_table` refuses a schema whose categorical layers
   carry different tables. `mineral_1`/`mineral_2` share one enumeration today; a product whose
   layers do not will need per-layer `classes.json` and colour tables ([07 §2](07-output-mapping.md)).
