# 04 — Cost Functions and Plugin Contracts

**Status:** draft · **Depends on:** [00](00-overview.md), [01](01-grid-tiling.md) ·
**Depended on by:** [05](05-ancillary-data.md), [07](07-output-mapping.md), [08](08-execution.md)

The five hooks, their contracts, how they are registered and resolved, and how the framework
decides where to run them.

---

## 1. Why five hooks and not one

The decisions a mosaic makes have wildly different cost profiles and run at different stages.
Collapsing them into one interface forces the cheapest to pay the price of the most expensive.

| Hook | Stage | Granularity | Cost | Decides |
|------|-------|-------------|------|---------|
| `GranuleFilter` | 1 plan | per granule | µs, no IO | Which granules are candidates |
| `PixelMask` | 2 regrid | per granule × block | vectorized | Which pixels are eligible |
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

Runs against the index in stage 1, before any pixel IO.

```python
class GranuleFilter(Protocol):
    def keep(self, granules: GranuleFrame) -> BoolArray:
        """Vectorized over the whole candidate frame. True = keep."""

    def describe(self) -> str:
        """One line for the run report, e.g. 'cloud_fraction <= 0.5'."""
```

`GranuleFrame` is the index as a dataframe: `granule_id`, `datetime`, `footprint`,
`cloud_fraction`, `build_version`, `collection`, asset URIs, plus whatever else the index carries.

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

### Temporal range lives here

The "limit the temporal range" requirement is a `GranuleFilter`, not pipeline code. EMIT-AMD
applies none at all and hands every observation ever acquired to the stacker. For a versioned
annual product the range must be declared, pinned, and recorded.

---

## 3. `PixelMask`

Runs during regrid, so masked pixels never enter the observation stack.

```python
class PixelMask(Protocol):
    required_roles: tuple[str, ...]          # e.g. ("mask",)

    def valid(self, obs: ObsWindow, aux: AuxAccessor) -> BoolArray:
        """(H, W) bool. True = this pixel may be used."""
```

Masks compose by conjunction: a pixel is eligible only if every mask in the chain admits it.

AMD's flag semantics are a reasonable default and are expressible directly in config —
`Cloud flag`, `Cirrus flag`, `Water flag`, `Spacecraft Flag`, with an optional `Dilated Cloud
Flag`. Dana's additions (snow, high vegetation) enter as further entries in the chain, several of
which will need aux data rather than the L2A mask product.

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

### Execution modes

`capability` is a promise about what the scorer needs to see, and it determines both memory
behaviour and where the work runs.

| Capability | Called | Memory | Enables | Example |
|---|---|---|---|---|
| `streaming` | once per granule | O(block) | fused into regrid | min view zenith |
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
```

### The score band is persisted

`build_obs_nc` computes the criteria array and then discards it — it declares four band names
(`GLT X`, `GLT Y`, `File Index`, `OBS val`) but allocates three bands.

**Requirement.** Stage 3 writes the winning score alongside the winning value. For a product whose
premise is defensible selection, "why did this pixel win?" must be answerable from the artifact.

---

## 5. `Reducer`

```python
class Reducer(Protocol):
    outputs: tuple[BandSpec, ...]
    halo: int = 0

    def reduce(self, snaps: SnapshotStack, aux: AuxAccessor) -> dict[str, Array]:
        """snaps[name] is (n_epochs, H, W). Returns one array per declared output."""
```

Outputs are declared up front so the COG/NetCDF schema, the STAC item and the `OutputMapper`
contract can all be derived without running anything.

```python
class ModeThroughTime:
    outputs = (
        BandSpec("mineral_id",   "uint16",  "most frequent class across epochs"),
        BandSpec("agreement",    "float32", "modal count / valid epochs"),
        BandSpec("n_epochs",     "uint8",   "epochs contributing"),
        BandSpec("runner_up_id", "uint16",  "second most frequent class"),
    )
```

Parameters, following AMD's `stack:` block, which is the only working precedent:

| Parameter | Meaning | AMD equivalent |
|---|---|---|
| `min_count` | Suppress pixels whose modal class has fewer votes | `stack.mincount: 3` |
| `ignore` | Classes excluded from the tally | `stack.ignore: [0, -4]` |
| `tie_break` | `earliest` \| `latest` \| `highest_score` \| `nodata` | *(absent)* |

> **Open.** Whether AMD's `freq-N` is a frequency *rank* or a time *period* is unresolved, and it
> decides how much of mode-through-time is genuinely new. See the README's open questions.

### A no-op reducer reproduces V002

Passing the single epoch through unchanged gives V002 semantics exactly. That is the check that
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
provides and the manifest refers to plugins by name.

```toml
[project.entry-points."stratum.scorers"]
min_view_zenith = "stratum_emit.scorers:MinViewZenith"

[project.entry-points."stratum.reducers"]
mode_through_time = "stratum_emit.reducers:ModeThroughTime"
```

Two delivery paths, both recorded in provenance:

| Path | Mechanism | For |
|---|---|---|
| **Iteration** | wheel URI in the manifest, fetched to `/tmp` at cold start | experiments — edit, publish, rerun in ~90s |
| **Production** | pinned in the container image, wheel-fetch disabled by policy | delivered products — reproducibility from an immutable digest |

---

## 8. Validation at plan time

Every failure below is detected in stage 1, before compute is provisioned:

- the named plugin resolves, and its version is recordable;
- `required_roles` are all present in the manifest's `inputs.roles`;
- `required_aux` are all declared in `aux` — an undeclared read is refused, because it produces a
  cache key that lies;
- `capability` and `halo` are consistent with the configured block size;
- declared `outputs` are non-empty, uniquely named, and have a mapper if rendering is requested;
- filter `on_missing` policy is explicit.

---

## 9. Open questions

1. Does `Scorer` need to see the *previous* epoch's snapshot? Would enable temporal smoothing but
   breaks epoch independence and therefore parallelism. Currently: no.
2. Should `Reducer` see per-epoch scores as well as values? Would let it weight by confidence.
   Cheap to add now, awkward later — leaning yes.
3. Is `tile` capability worth supporting in v1, or should it be deferred until something needs it?
