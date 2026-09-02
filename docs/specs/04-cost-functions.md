# 04 — Cost Functions and Plugin Contracts

**Status:** draft · **Depends on:** [00](00-overview.md), [01](01-grid-tiling.md) ·
**Depended on by:** [05](05-ancillary-data.md), [07](07-output-mapping.md), [08](08-execution.md)

The five science hooks, their contracts, how they are registered and resolved, and how the
framework decides where to run them. The two *access* hooks — `GranuleSource` and `GranuleReader` —
are a separate tier and live in [12](12-data-access.md): they add a data source rather than
changing an answer.

---

## 1. Why five hooks and not one

The decisions a mosaic makes have wildly different cost profiles and run at different stages.
Collapsing them into one interface forces the cheapest to pay the price of the most expensive.

| Hook | Stage | Granularity | Cost | Decides |
|------|-------|-------------|------|---------|
| `GranuleFilter` | 1 plan | per granule | µs, no IO | Which granules are candidates |
| `PixelMask` | 3 resolve | per granule × block | vectorized | Which pixels are eligible |
| `Scorer` | 3 resolve | per tile × epoch × block | vectorized | **Which observation wins** |
| `Reducer` | 4 reduce | per tile × block | N = #epochs | How epochs collapse through time |
| `OutputMapper` | 5 publish | per tile | presentation | What the result looks like |

The first four decide *which observation wins*; the fifth decides *what it looks like*.
`OutputMapper` is specified in [07](07-output-mapping.md) and only summarized here.

Two consequences worth stating:

- **Rejecting early is free.** A `GranuleFilter` prunes before a byte is read. The same predicate
  applied at pixel level costs a download, a regrid, and a scoring pass.
- **Masking is not ranking.** `PixelMask` returns a boolean; it never expresses preference. Mixing
  the two is how "this pixel is unusable" quietly becomes "this pixel is merely worse".

---

## 2. `GranuleFilter`

Runs against the index in the plan stage, before any pixel IO.

```python
class GranuleFilter(Protocol):
    def keep(self, granules: GranuleFrame) -> BoolArray:
        """Vectorized over the whole candidate frame. True = keep."""

    def describe(self) -> str:
        """One line for the run report, e.g. 'cloud_fraction <= 0.5'."""
```

`GranuleFrame` is the index as a dataframe: `granule_id`, `datetime`, `footprint`,
`cloud_fraction`, `build_version`, `collection`, asset URIs, the source's verbatim `attributes`, plus
whatever else the index carries.

### Exclusions must be reported by reason

V002's cloud filter is:

```python
if 'Total Cloud Fraction' in feat['properties'] and feat['properties'][...] <= max_cloud_fraction:
```

A granule whose record *lacks* the key is dropped silently — indistinguishable from "too cloudy",
with no warning and no count. That is data loss with no signal.

**Requirement.** The filter chain records, per filter, how many granules it removed and why, into
the run report. Missing metadata is an explicit policy in the manifest
(`on_missing: reject | keep | fail`), never the accidental result of a truth-test. Default is
`fail` at plan time — loudly, while it is cheap to fix.

### Temporal range lives here — and it is not just start/end

The "limit the temporal range" requirement is a `GranuleFilter`, not pipeline code. EMIT-AMD
applies none at all and hands every observation ever acquired to the stacker. For a versioned
annual product the range must be declared, pinned, and recorded.

Two distinct capabilities, and only the first is obvious:

| Filter | Expresses |
|---|---|
| `start` / `end` | An absolute interval — the mission window, or one delivery year |
| `month_in: [8, 9, 10, 11]` | A **recurring seasonal** window |

The seasonal form matters because the useful window recurs annually: taking August–November
scenes over the Western US avoids peak vegetation without discarding whole years. A start/end
range cannot express that, and building it out of many disjoint intervals is miserable.

### Vintage agreement is checked, not assumed

Reprocessing of the entire catalog begins ~Sept 2026 and takes ~75 days, regenerating every
mineral map against Tetracorder 6 with updated reflectance — **and the mineral classes shift**.
For that window the archive is mixed-vintage, and an unpinned run will silently blend two
incompatible products into a result that looks entirely plausible.

The check is on the **class table**, not the build number: plan-time validation fails if the
frozen index's granules carry class tables that disagree by fingerprint, unless the manifest
explicitly opts in. `build_version` is filterable and reported, but one collection already spans
eight builds, so pinning one is not the guard. See [02 §3](02-granule-index.md) and
[the tag-up notes](../notes/2026-08-28-mines-tagup.md#1-time-critical-the-reprocessing-window).

---

## 3. `PixelMask`

Runs in resolve's read path, before an observation reaches the stack, so a masked pixel never
enters it. Regrid does not see masks ([03 §5](03-regrid-glt.md)). Masks compose by
conjunction: a pixel is eligible only if every mask in the chain admits it.

### Two coordinate spaces

Some masks are naturally expressed in **map geometry** (this location is water) and some in
**sensor geometry** (this detector column is unreliable). The contract must admit both, because
the second cannot be expressed after regridding has discarded the source column.

```python
class PixelMask(Protocol):
    space: Literal["map", "sensor"] = "map"
    required_roles: tuple[str, ...] = ()

    def valid(self, obs: ObsWindow, aux: AuxAccessor) -> BoolArray:
        """(H, W) bool. True = usable.
        space="map"    -> obs is on the block grid, post-GLT.
        space="sensor" -> obs is in raw (downtrack, crosstrack) geometry, pre-GLT.
        """
```

Sensor-space masks are applied to the sensor window before the gather through the GLT; map-space
masks to the block after it ([12 §2](12-data-access.md)). Neither touches the GLT, which is why the
mask spec is not in its key. (Sensor masks *could* be evaluated in map space by thresholding GLT
band 1, which holds the source column — but that is a trick, and it fails for any mask needing raw
values rather than raw indices. Applying them on the sensor window is honest and simpler.)

### Built-in EMIT masks

Instrument knowledge belongs in `stratum_emit`, not in user config. Per Phil at the Mines tag-up
([notes](../notes/2026-08-28-mines-tagup.md#2-concrete-instrument-artifacts-to-mask)):

| Mask | Space | Behaviour |
|---|---|---|
| `EdgeTrim` | sensor | Drop the outer **7 columns** each side (5 minimum). Every column is a different detector; the outermost are unreliable. |
| `SlitDust` | sensor | Drop 2–3 columns at cross-track centre. Mostly cleaned up upstream; off by default. |
| `L2AStandard` | map | Cloud, cirrus, water, spacecraft flags — AMD's `filter-clouds-1-4` semantics |
| `SoilFraction` | map | Require soil fraction ≥ threshold from the `frcov` role. See §4. |

**No along-track trimming.** EMIT is a push-broom collecting one continuous strip; granule
boundaries in the along-track direction are a download convenience with no physical meaning, so
top/bottom edges are sound. Trimming them would discard good data.

AMD's flag semantics (`Cloud flag`, `Cirrus flag`, `Water flag`, `Spacecraft Flag`, optionally
`Dilated Cloud Flag`) remain expressible directly in config. Dana's additions — snow, high
vegetation — enter as further chain entries, several needing aux data rather than the L2A mask.

---

## 4. `Scorer` — the cost function proper

```python
class Scorer(Protocol):
    capability: Literal["streaming", "stack", "tile"]
    halo: int = 0
    required_roles: tuple[str, ...] = ()
    required_aux:   tuple[str, ...] = ()

    def score(self, obs: ObsWindow, aux: AuxAccessor) -> FloatArray:
        """Higher wins. NaN marks a cell this observation may not occupy."""
```

### What the scorer writes: the snapshot schema

A scorer decides *which observation wins*. It does not decide what is recorded about the winner —
that is the **snapshot schema**, declared once in the manifest and constant for the run
([13](13-snapshot-schema.md)). The framework takes the argmax of `score()`, gathers every layer the
schema declares from the winning observation, resolves categorical layers into the product's
enumeration, and writes the snapshot. A scorer therefore works with any schema and a schema with
any scorer.

```python
class MinViewZenith:
    capability = "streaming"
    required_roles = ("geometry",)

    def score(self, obs, aux):
        return -obs["view_zenith"]
```

The framework adds two layers to every snapshot without being asked:

| Layer | Content |
|---|---|
| `score` | The winning score. Non-optional — "why did this pixel win?" must be answerable ([03 §2](03-regrid-glt.md)). |
| `valid` | Whether any observation occupied the cell at all ([11 §2](11-types.md)) |

**Selection semantics are unchanged.** `score()` ranks, the framework takes the argmax and gathers
the schema's layers from the winning row. In `stack` mode the score is `(N, H, W)` and the gather is
along axis 0.

**Cost is in layer count, not observation count.** In streaming mode the framework holds the running
best score plus the layers of the current best, so memory is `O(block × n_layers)` and independent
of how many observations overlap. Snapshot width is cheap; it does not reintroduce the memory
profile that block decomposition exists to avoid. Storage is not free, though — these are cached
artifacts, so declare what the reduction needs and not more.

The schema's `layers_hash` enters the snapshot cache key ([06 §2](06-caching.md)): changing what a
snapshot holds changes the artifact; changing how it is aggregated does not.

### Derived snapshot bands — planned, not in v1

Schema layers copy existing bands from the winning observation. They cannot express a layer the
scorer *computes* — a runner-up margin, per-candidate evidence, a broadcast acquisition timestamp.
The intended shape is a layer with `source: scorer`, fed by an optional member:

```python
    outputs: tuple[BandSpec, ...] = ()          # NOT IMPLEMENTED

    def emit(self, obs, aux) -> Mapping[str, Array]:
        """Per-observation values for each declared output; the framework keeps
        the winning row.  NOT IMPLEMENTED"""
```

Deferred deliberately. Copied layers cover the cases we can name today, and the derived form raises
questions worth answering with a real workload in hand rather than in the abstract:

- Several of the motivating bands are **framework-knowable, not scorer-derived** — `source_granule`,
  `acquired` and `runner_up_score` are all things the selection loop already computes. Those may
  belong as built-ins rather than as plugin output, which would leave `emit()` with a much narrower
  job.
- Whether a `stack`-capability scorer may emit a band that is a property of the whole stack rather
  than of one observation — a within-epoch median, say — blurs the scorer/reducer split and is
  unresolved.
- Per-candidate evidence, the strongest motivating case, needs a decision about representation
  (one band per candidate? a ragged structure?) that is premature without a reducer that consumes it.

**Consequence to accept knowingly:** classify-last — aggregating per-candidate evidence across
epochs and classifying the aggregate, rather than voting on labels — is **not expressible in v1**.
A reducer cannot recover evidence a snapshot never stored. Reaching it means adding derived outputs
and reprocessing snapshots, not merely writing a new reducer.

### Execution modes

`capability` is a promise about what the scorer needs to see, and it determines both memory
behaviour and where the work runs.

| Capability | Called | Memory | Enables | Example |
|---|---|---|---|---|
| `streaming` | once per granule | O(block) | memory independent of N | min view zenith |
| `stack` | once with all observations | O(block × N) | cross-observation reasoning | median, consensus |
| `tile` | once per whole tile | O(tile × N) | global spatial context | segmentation |

`streaming` is the V002 shape: a running best-score array, updated granule by granule, memory
independent of how many observations exist. That property is worth preserving and the framework
must keep it available.

`stack` is the AMD shape: nothing is discarded, so any reduction is expressible. It is what
mode-through-time needs, and block decomposition ([01](01-grid-tiling.md)) is what keeps it
affordable.

`tile` is the escape hatch. It routes to Batch and gives up block parallelism.

### `ObsWindow`

Bands are addressed by role-derived alias, never index, so a scorer is portable across
instruments — the manifest maps aliases to bands per collection.

```python
obs["view_zenith"]        # (H, W) float, this block only
obs.granule.datetime      # metadata
obs.granule.cloud_fraction
obs.grid                  # this block's transform and CRS
obs.coords.lon            # every cell's centre; also .lat, and .x/.y in the grid CRS
obs.epoch                 # the epoch being resolved
obs.n                     # observation count — 1 when streaming, N when stacked
```

In `stack` mode the same accessors return a leading observation axis: `(N, H, W)`.

### Worked examples

```python
class MinViewZenith:
    """V002 parity: the most nadir look wins."""
    capability = "streaming"
    required_roles = ("geometry",)

    def score(self, obs, aux):
        return -obs["view_zenith"]


class CleanestNadir:
    """Nadir-preferring, penalised for steep terrain, rejecting snow."""
    capability = "streaming"
    required_roles = ("geometry",)
    required_aux = ("slope", "snow")

    def score(self, obs, aux):
        s = -obs["view_zenith"] / 90.0
        s -= 0.5 * (aux.raster("slope") > 30)
        s[aux.raster("snow", date=obs.granule.datetime) > 0] = np.nan
        return s


class PreferBareEarth:
    """Prefer the observation that actually sees ground.

    Thomas Monecke's decision-tree idea from the Mines tag-up: given several
    observations of a pixel, most of which are vegetation, take the one that
    isn't. Soil fraction comes from the already-orthorectified L2B FRCOV
    product, so this costs nothing to regrid.
    """
    capability = "streaming"
    required_roles = ("frcov",)

    def __init__(self, min_soil=0.80, hard_floor=0.65):
        self.min_soil, self.hard_floor = min_soil, hard_floor

    def score(self, obs, aux):
        soil = obs["frcov"]
        s = soil.copy()
        s[soil < self.hard_floor] = np.nan     # unrecoverable; V002 used 0.65
        return s
```

The two thresholds are deliberate. `hard_floor=0.65` is V002's cutoff, chosen because grain-size
retrieval "completely falls apart" below it; `min_soil=0.80` is the higher bar recommended for
mosaicking. Keeping them separate lets the scorer *rank* between 0.65 and 0.80 rather than
discarding that range outright — which is the "don't destroy information" principle applied at the
smallest possible scale.

> **Caveat to carry:** the current FRCOV is reportedly NPV-false-positive-prone — it reads some
> bare soil as non-photosynthetic vegetation — so this scorer is conservative in a way that will
> improve when FRCOV does.

### The score band is persisted

`build_obs_nc` computes the criteria array and then discards it — it declares four band names
(`GLT X`, `GLT Y`, `File Index`, `OBS val`) but allocates three bands.

**Requirement.** Resolve writes the winning score alongside the winning value. For a product whose
premise is defensible selection, "why did this pixel win?" must be answerable from the artifact.

---

## 5. `Reducer`

For everything the schema's aggregation vocabulary covers, the reduction is **declared, not
coded**: each layer names its method — `vote`, `median`, `inverse_variance` and the rest of
[13 §4](13-snapshot-schema.md) — and the framework's built-in reducer runs it when the manifest
has no `reducer:` block. Outputs, the raster schema, the STAC item and the `OutputMapper` contract
are all derived from the schema without running anything.

A `Reducer` plugin is for what the vocabulary cannot say — a joint vote across candidate layers,
classify-last over per-candidate evidence — and it takes the same input:

```python
class Reducer(Protocol):
    outputs: tuple[BandSpec, ...]
    halo: int = 0

    def reduce(self, snaps: SnapshotStack, aux: AuxAccessor) -> dict[str, Array]:
        """snaps[layer] is (n_epochs, H, W); snaps.schema says what each layer is.
        Returns one array per declared output."""
```

A plugin declares its outputs up front for the same reason the schema does: so a bad declaration
fails at plan time.

Mode-through-time, the Critical Minerals baseline, is a schema entry rather than a plugin:

```yaml
snapshot:
  layers:
    mineral_1:
      kind: categorical
      source: mineral
      classes: "@ref:classes/cm-v1.yaml"
      aggregate: {method: vote, min_count: 3, ignore: [none], tie_break: highest_score}
```

Parameters follow AMD's `stack:` block, which is the only working precedent:

| Parameter | Meaning | AMD equivalent |
|---|---|---|
| `min_count` | Suppress pixels whose modal class has fewer votes | `stack.mincount: 3` |
| `ignore` | Classes excluded from the tally, **by name** — `none` is the reserved "observed, nothing identified" class | `stack.ignore: [0, -4]` |
| `tie_break` | `earliest` \| `latest` \| `highest_score` \| `nodata` | *(absent)* |

### Decision order for `vote`, normatively

Snapshots all sit on the identical block grid, so nothing spatial remains to resolve. For one cell,
given the per-epoch value, `valid` and `score`:

1. **Discard** epochs where `valid` is false. The count remaining is `n_epochs`, and it is the
   denominator for `agreement`.
2. **Exclude** classes named in `ignore` from the tally. They stay counted in `n_epochs` —
   "observed, nothing identified" is evidence of observation, and conflating it with "never
   observed" fabricates agreement ([11 §2](11-types.md)).
3. **Tally** the remaining values; take the modal class.
4. **Suppress** to nodata if the modal count is below `min_count`.
5. **On a tie**, apply `tie_break`:
   - `earliest` / `latest` — the tied class appearing in the earliest / latest epoch;
   - `highest_score` — the tied class holding the single highest `score` across its epochs;
   - `nodata` — refuse to choose; write nodata.

`agreement` is `modal_count / n_epochs`, so ignored-but-observed epochs lower it. That is
deliberate: goethite five times out of ten looks is less certain than goethite five times out of
five.

Note the asymmetry with `Scorer`. A scorer **ranks** and takes an argmax — one observation beats the
rest. A reducer **aggregates**; for a `vote` layer that is a tally, and no single epoch "wins". A
winner-take-all reducer is expressible (argmax over `snaps.score`) but is a different product, and a
less defensive one: a single well-scoring epoch takes the cell with no corroboration.

> **Open.** Whether AMD's `freq-N` is a frequency *rank* or a time *period* is unresolved, and it
> decides how much of mode-through-time is genuinely new. See the README's open questions.

### Reduce continuously where possible

At the Mines tag-up Phil framed the whole problem as a Kalman filter — *"coming up with the
appropriate loss function that one applies… to get the right solution out"* — and named the goal
as *"a version of the world where we're not destroying all of that information,"* noting that
Tetracorder's binarized output has already discarded some of it
([notes](../notes/2026-08-28-mines-tagup.md#5-framing-worth-adopting)).

That gives a direction on the open question of whether to take the mode over mineral IDs directly
or over something continuous first: **prefer reducing the continuous quantities — band depth, fit,
uncertainty — and classify at the end**, rather than voting over labels that have already thrown
information away. Two consequences:

- `vote` over labels stays as the baseline, because it is what AMD does and what we can validate
  against. It should not be the only aggregation the product uses.
- A classify-last reducer that aggregates band depths per candidate mineral and classifies the
  aggregate is the more principled version, and the design should not make it awkward. This is why
  a `Reducer` plugin receives the full snapshot stack, with its schema, rather than a single layer.

Both read the same snapshots; the difference is which layers the schema declares. That is the
abstraction earning its keep.

### Continuous bands

Mode applies to classes. A continuous layer — band depth, fit, uncertainty — needs a summary
statistic, and the choice is a science decision, not a default. Each row is a `method` in
[13 §4](13-snapshot-schema.md):

| Aggregation | Use when |
|---|---|
| `median` | Safe default; robust to a single bad epoch |
| `mean` | Outliers already excluded |
| `score_weighted` | The scorer's ordering is trusted |
| `inverse_variance` | An uncertainty layer exists — `group_N_band_depth_unc` does |
| `max` / `percentile` | Strongest expression rather than typical |
| `spread: std \| iqr` | Emitted *alongside* the estimate as a quality band |

Two requirements hold regardless of choice:

1. **Mask on `valid` before aggregating.** Cells an epoch never observed carry undefined values;
   a statistic over the raw stack silently folds them in.
2. **Publish the support count.** An estimate from two epochs and one from twelve must be
   distinguishable downstream.

#### Class-conditional aggregation

A band depth is the depth *of a specific mineral's absorption feature*. Averaging it across epochs
that identified **different** minerals produces a number with no meaning.

> **Requirement.** Where a continuous layer is conditional on a categorical one, the reducer resolves
> the class first and aggregates the continuous layer **only over concordant epochs** — those whose
> class matches the winner. In the schema that is `conditional_on` ([13 §4](13-snapshot-schema.md)). The concordant count `<layer>_n` and `n_epochs` are different numbers
> and both are published.

#### Classify last, where the snapshot allows it

Voting on labels discards information before the reduction begins: a class that placed second in
every epoch loses to one that placed first in a bare majority, leaving no trace. The better shape is
to reduce continuous evidence per candidate and classify the aggregate.

That requires the schema to hold per-candidate depths rather than only the winning label — a
decision about snapshot contents, not about the `Reducer`, and one that needs scorer-computed
layers (§4). The contract is
unchanged either way, which is the abstraction earning its keep, but it cannot be retrofitted onto
snapshots that only ever stored one label.

Limit worth stating: the upstream product is already binarised, so information is lost before
Stratum sees it. Reducing continuously recovers what survived; it does not recover what the
classifier discarded.

### Why this is pluggable at all

The clearest statement of the reason came from Phil at the Mines tag-up: temporal stability is an
open geological hypothesis, and the right assumption depends on the question being asked —
*"if you're trying to make a base map, you probably don't care so much; if you're trying to look at
sand dunes, you care a lot."* No single temporal algorithm is correct for both, which is precisely
why the reducer is a plugin and not a setting.

### A single epoch reproduces V002

Over one epoch every aggregation is the identity — the vote has one voter, the median one value —
so a single-epoch run gives V002 semantics exactly with no special case. That is the check that
the abstraction is honest: the old behaviour must be a configuration of the new system, not a
special case beside it.

---

## 6. `OutputMapper` (summary)

Runs at publish time; turns finished bands into the rendered image. Full spec in
[07](07-output-mapping.md).

```python
class OutputMapper(Protocol):
    outputs: tuple[ImageSpec, ...]
    def render(self, bands: BandStack, aux: AuxAccessor) -> RGBAArray: ...
    def legend(self) -> Legend: ...
```

---

## 7. Registration and resolution

Standard Python entry points, so `stratum plugins list` can enumerate what an image or wheel
provides and the manifest refers to plugins by name. A `ref` anywhere in the manifest — scorer,
reducer, mask, filter, mapper, reader — is an entry-point name when one is registered, or
`module:Class` otherwise; every hook accepts both forms.

```toml
[project.entry-points."stratum.scorers"]
min_view_zenith = "stratum_emit.scorers:MinViewZenith"

[project.entry-points."stratum.reducers"]
classify_last = "stratum_emit.reducers:ClassifyLast"   # only for what the schema cannot say
```

Two delivery paths, both recorded in provenance:

| Path | Mechanism | For |
|---|---|---|
| **Iteration** | wheel URI in the manifest, fetched to `/tmp` at cold start | experiments — edit, publish, rerun in ~90s |
| **Production** | pinned in the container image, wheel-fetch disabled by policy | delivered products — reproducibility from an immutable digest |

---

## 8. Validation at plan time

Every failure below is detected in the plan stage, before compute is provisioned:

- the named plugin resolves, and its version is recordable;
- `required_roles` are all present in the manifest's `inputs.roles`;
- `required_aux` are all declared in `aux` — an undeclared read is refused, because it produces a
  cache key that lies;
- `capability` and `halo` are consistent with the configured block size;
- the snapshot schema resolves: every layer `source` names a role or alias, every categorical
  layer's enumeration resolves once per contributing granule, `conditional_on` and `unc` name
  declared layers ([13](13-snapshot-schema.md));
- delivered outputs — derived from the schema, or declared by a `Reducer` plugin — are non-empty,
  uniquely named, and have a mapper if rendering is requested;
- filter `on_missing` policy is explicit.

---

## 9. Open questions

1. ~~Does `Scorer` need to see the *previous* epoch's snapshot?~~ **Resolved:** no. Epochs are
   independent, which is what makes resolve parallel.
2. ~~Should `Reducer` see per-epoch scores as well as values?~~ **Resolved: yes.**
   `SnapshotStack.score` is `(n_epochs, H, W)` and always populated ([11 §8](11-types.md)).
   `tie_break: highest_score` depends on it, and a winner-take-all reducer is expressible from it.
3. Is `tile` capability worth supporting in v1, or should it be deferred until something needs it?
