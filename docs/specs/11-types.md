# 11 — Core Types and Data Contracts

**Status:** draft · **Depends on:** [01](01-grid-tiling.md), [02](02-granule-index.md) ·
**Depended on by:** everything

The concrete objects that cross every interface. Every other spec names these types; this one
defines them. **If you change a type here, update this document in the same commit** — see
[CLAUDE.md](../../CLAUDE.md).

Grounded in a real delivered granule:
[`refs/EMIT_L2B_MIN_001_20260825T151308_2623710_050.nc`](../../refs/), product version `V001`,
`software_build_version 010635`. Observations from that file are marked **[observed]**; anything
else is a design decision.

> **The code is authoritative for signatures.** Every type below lives in
> `src/stratum/types.py` (the hook protocols in `src/stratum/hooks.py`), and where a field list
> here and the dataclass disagree, the dataclass is right and this document is the thing to fix.
> This spec is the narrative — why a type is shaped the way it is and what the delivered data
> constrains — and the code blocks are illustrations, not a second copy. Additive changes made
> while building the first slice are marked **[design]** in the section where the type lives;
> they describe the reason and point at the module rather than repeating the field.

---

## 1. Coordinate spaces

Three spaces, and confusing them is the most likely source of silent error. Every array-bearing
type below declares which one it is in.

| Space | Axes | Extent **[observed]** | Where it appears |
|---|---|---|---|
| **sensor** | `(downtrack, crosstrack)` | 1664 × 1242 | Raw L2B/L1B variables; sensor-space `PixelMask` |
| **granule-ortho** | `(ortho_y, ortho_x)` | 2363 × 2309 | The GLT *shipped inside the granule*; see §7 |
| **block** | `(y, x)` | run-defined, e.g. 512 × 512 | Everything from resolve onward |

Cross-track is 1242 columns, which is what `EdgeTrim` operates on
([04 §3](04-cost-functions.md)).

---

## 2. Fill and nodata

The single most under-specified thing in the design so far, and the delivered file settles most of
it. **[observed]** unless noted.

| Quantity | Type | No-data | Meaning |
|---|---|---|---|
| `group_N_mineral_id` | `int16` | `_FillValue = -9999` | Pixel not processed / outside swath |
| `group_N_mineral_id` | `int16` | **`0`** | Processed, **no mineral identified** — *not* the same thing |
| `group_N_band_depth` | `float32` | `_FillValue = -9999.0` | Range observed 0.0 – 0.5 |
| `location/lat,lon,elev` | `float64` | `-9999.0` | |
| `location/glt_x` | `int32` | `0` | 1-based crosstrack index; observed range 1–1242, **no negatives** |
| `location/glt_y` | `int32` | `0` | 1-based downtrack index; observed range 1–1664, **no negatives** |
| Stratum GLT band 3 | `int32` | `0` | Granule index, 1-based |
| Stratum GLT bands 1–2 | `int32` | `0` | **Negative = interpolated** (SpectralUtil convention; *not* used in delivered granules) |
| `Scorer.score` | `float32` | `NaN` | This observation may not occupy this cell |
| Reducer outputs | per `BandSpec` | declared | |

### The rule

> **`-9999` means "no data". `0` in a mineral ID means "we looked and found nothing".**

These must never be conflated. AMD's config already distinguishes them — `-3: [-9999]` labelled
"fake NaN" and `-2: [0]` labelled "Tetracorder NaN" — and its `v7` `stack.ignore: [0, -4]`
excludes the found-nothing class from voting while counting the pixel as observed. A reducer that
treats them alike will report confident agreement over pixels that were never looked at.

**Internal convention.** Once inside Stratum, integer fills are converted to a mask and carried
separately; floats use `NaN`. Sentinel values are re-applied only on write. Plugins therefore see
masked arrays or explicit validity masks, never `-9999`.

---

## 3. Grid, tile, block

```python
@dataclass(frozen=True)
class GridDef:
    crs: str                      # "EPSG:4326"
    resolution: tuple[float, float]   # (x, y); y negative
    origin: tuple[float, float]       # (x0, y0) - cell edges at origin + n*res
    tile_size: float
    block: int = 512

    @property
    def id(self) -> str: ...      # short stable hash; part of every cache key

@dataclass(frozen=True)
class TileRef:
    grid: GridDef
    tx: int
    ty: int
    @property
    def bounds(self) -> tuple[float, float, float, float]: ...
    @property
    def shape(self) -> tuple[int, int]: ...
    @property
    def name(self) -> str: ...    # "{grid.id}/{tx}_{ty}"

@dataclass(frozen=True)
class BlockRef:
    tile: TileRef
    bx: int
    by: int
    halo: int = 0
    @property
    def window(self) -> Window: ...        # with halo
    @property
    def core_window(self) -> Window: ...   # without - what actually gets written
    @property
    def transform(self) -> Affine: ...
```

A tile is the cells whose centres fall inside its nominal bounds, so `TileRef.shape` may differ by
one cell between neighbours when `tile_size` is not a whole number of cells, and `bounds` is the
lattice rectangle rather than the nominal one ([01 §1](01-grid-tiling.md)).

Two windows, deliberately. Plugins compute over `window`; the framework writes only `core_window`.
That is the whole halo mechanism ([01 §4](01-grid-tiling.md)), and keeping it in the type rather
than in caller code is what stops a plugin author getting it wrong.

> **[observed]** EMIT's native ortho resolution is `spatialResolution = 0.000542232520256367`.
> V002's `0.00055` is a rounding of it. A run grid at exactly the native resolution is *not*
> automatically aligned — origins are per-granule — so the KD-tree path is still required.

---

## 4. Granules

```python
@dataclass(frozen=True)
class GranuleRef:
    granule_id: str               # "001_20260825T151308_2623710_050"
    collection: str               # "EMITL2BMIN"
    datetime: datetime            # tz-aware UTC
    end_datetime: datetime
    bbox: tuple[float, float, float, float]
    assets: Mapping[str, str]     # role -> URI
    build_version: str            # "010635"  CMR SOFTWARE_BUILD_VERSION   [observed]
    product_version: str          # "V001"    file header; not in CMR      [observed]
    collection_version: str       # "001"     the CMR collection's own version
    cloud_fraction: float | None  # None is meaningful - see 02 section 4
    day_night: str | None         # "Day"             [observed]
    attributes: Mapping[str, str] # the source's remaining fields, verbatim
```

**Three version fields, deliberately.** `build_version` is a property of *this granule* and moves
often — one collection already holds eight of them. `product_version` is the file's own stamp and
in practice tracks the collection. `collection_version` is a property of the **collection**, so it
is identical for every granule in it. None of the three is the vintage check: that is the class
table's fingerprint (§9), because a class shift is what a reprocessing changes and a build number
merely correlates with it. All three are carried so a run can report exactly what it read, and so
a filter can pin any of them when there is a reason to ([02 §3](02-granule-index.md),
[12 §5](12-data-access.md)).

`GranuleFrame` is the plural form: a dataframe with these as columns, which is what
`GranuleFilter.keep()` receives. Filters are vectorized over it and never see individual granules.

**[design]** `GranuleRef` also carries `checksums`, keyed like `assets`: the catalogue's per-file
checksum, which is the asset identity that enters the masked-observation key
([12 §4](12-data-access.md)). A source with no catalogue checksum — `LocalSource` — leaves it
empty rather than inventing a size/mtime stand-in, and the observation key then carries
`checksum: null` for that role; locality must never leak into a key. `attributes` is
`Mapping[str, str]`: the map column cannot hold null, so `None`-valued source attributes are
dropped and the promoted columns are excluded from it (`src/stratum/index/build.py`).

> **[observed] Vintage identifiers.** The granule carries `software_build_version` (`010635`),
> `software_delivery_version`, and `product_version` (`V001`). Its `history` attribute names
> `tetracorder5.27c.cmds`, confirming this file predates the Tetracorder 6 reprocessing. CMR
> exposes the first two as granule-level `AdditionalAttributes` — verified 2026-09-01 — and not
> the third ([12 §5](12-data-access.md)).

### Epoch assignment

Not previously specified anywhere:

- A granule belongs to the epoch containing its **`datetime`** (acquisition start), regardless of
  `end_datetime`. **[observed]** EMIT granules span ~16 s, so straddling is not a practical
  concern; making the rule explicit costs nothing and removes an ambiguity.
- Epoch bounds are **half-open**, `[start, end)`, in UTC.
- Epochs are generated from `time.start` by `time.epoch` period, not from calendar alignment,
  unless `align: calendar` is set.
- A granule outside every epoch is an error at plan time, not a silent drop.

### Epoch vs delivery period

An **epoch** is the unit of one vote: the resolve stage collapses it to exactly one snapshot per cell, however
many observations fell in it. This is what normalises for revisit density — without it, a densely
revisited month outvotes a sparse one and the reduction partly describes the acquisition schedule.

A **delivery period** is one output product. It draws on the epochs inside its *window*, which need
not equal the period itself:

| `deliver` | Meaning |
|---|---|
| `P1Y` | Shorthand for `{every: P1Y, window: P1Y, align: exact}` — non-overlapping annual products |
| `{every: P1M, window: P13M, align: center}` | One product a month, each reduced from 13 monthly snapshots centred on it |

`align` is one of `exact`, `center`, `trailing`, `leading`. `window` must be a whole multiple of
`epoch` and at least `every`; both are plan-time checks.

Windows truncate at `time.start` / `time.end`, so edge products carry fewer epochs. That surfaces in
`n_epochs` rather than being hidden, and `min_count` on the reducer is the suppression lever.

**Snapshots do not key on the delivery period** ([06 §2](06-caching.md)), so overlapping windows
re-run reduce only — a 13-month window delivered monthly computes each snapshot once and reads it
thirteen times. The lifecycle in [06 §5](06-caching.md) keeps snapshots for the longest window a
deployment declares, so that holds in practice and not only in principle.

---

## 5. `ObsWindow`

The type every `Scorer` and `PixelMask` receives. It has two forms and the difference is one axis.

```python
class ObsWindow:
    space: Literal["sensor", "block"]
    n: int                        # 1 in streaming mode, N in stack mode
    block: BlockRef | None        # None in sensor space
    sensor_window: SensorWindow | None   # set in sensor space; carries row0/col0 - section 10
    granule: GranuleRef | None    # None in stack mode - see granules
    granules: Sequence[GranuleRef]

    epoch: Epoch                  # the epoch this block is being resolved for

    def __getitem__(self, alias: str) -> np.ndarray:
        """Band by alias. Shape (H, W) streaming, (N, H, W) stacked."""

    @property
    def coords(self) -> Coords:
        """Cell centres: .x/.y in the grid CRS, .lon/.lat. Each (H, W). Halo included."""

    @property
    def valid(self) -> np.ndarray:
        """Bool. True = observed and unmasked. Same shape as a band."""

    @property
    def interpolated(self) -> np.ndarray:
        """Bool. True where the GLT reached beyond max_distance - 03 section 2, 12 section 2."""
```

```python
@dataclass(frozen=True)
class Coords:
    """Cell centres for one block, halo included. Each array is (H, W)."""
    x: np.ndarray                 # grid CRS
    y: np.ndarray
    lon: np.ndarray               # EPSG:4326, always
    lat: np.ndarray
```

`coords` is what lets a scorer do its own geometry against a declared vector source
([05 §3](05-ancillary-data.md)) without the framework growing a spatial-join vocabulary.

**[design] Two attributes added while building resolve**, both optional keywords with defaults
so no existing construction changed (`src/stratum/types.py`, `ObsWindow.__init__`):

- `sensor_shape` — the *full* `(downtrack, crosstrack)` of the variable the window was cut from,
  `VarSpec.shape[:2]`; `None` in block space. `SensorWindow` says where the window *starts*; only
  the full shape says where the detector *ends*, and `EdgeTrim` needs both to trim the far edge
  (`src/stratum_emit/masks/__init__.py`). It comes from the geolocation role's variable when that
  role is read, else the first role read; every role read must agree on it, because one GLT
  indexes one sensor array ([12 §2](12-data-access.md)).
- `band_attrs` — per role or alias, the `VarSpec.band_attrs` the reader reported, never
  interpreted by the core. `L2AStandard` selects mask bands by their reported `name` through it
  rather than by a positional guess ([04 §3](04-cost-functions.md)).

In sensor space the bands are masked arrays; in block space they are plain arrays with `valid`
carried beside them. A multi-band role appears as `(H, W, B)` under the role name as well as
through its aliases, so a `bands:` layer subset has something to index.

### Aliases, not indices

`obs["view_zenith"]`, never `obs[:, :, 5]`. The manifest maps aliases to `(role, band)` per
collection ([09 §2](09-run-manifest.md)), so the same scorer works across instruments — which
matters now that multi-instrument is a live requirement, not a someday one. An alias names a band
by index, or by matching an attribute the reader reports for its bands —
`{role: reflectance, match: {wavelength: 2200}, tolerance: 10}` — and the core never interprets
the attribute; it only matches it ([12 §3](12-data-access.md)).

Requesting an alias the manifest does not define is an error at **plan time**, via
`Scorer.required_roles`, not at runtime in 4,000 workers.

### Streaming vs stack

| | `capability = "streaming"` | `capability = "stack"` |
|---|---|---|
| `score()` calls | once per granule | once per block |
| Band shape | `(H, W)` | `(N, H, W)` |
| `granule` | the granule | `None` |
| `granules` | one-element | all N, ordered by `datetime` |
| Memory | O(block) | O(block × N) |

Ordering by `datetime` is guaranteed so that `tie_break: earliest` is meaningful and stack-mode
scorers can reason about sequence.

---

## 6. `AuxAccessor`

```python
class AuxAccessor:
    def raster(self, alias: str, *, date: datetime | None = None,
               epoch: Epoch | None = None) -> np.ndarray:
        """(H, W) on THIS block's grid. Warped, windowed, cached. `epoch` for temporal: epoch sources."""

    def vector(self, alias: str) -> np.ndarray:
        """Rasterized to this block's grid."""

    def features(self, alias: str, *, margin: float = 0.0) -> Sequence[BaseGeometry]:
        """The source's geometries, clipped to this block plus margin, in the block CRS."""

    def distance(self, alias: str, *, cutoff: float | None = None) -> np.ndarray:
        """(H, W) distance from each cell centre to the nearest feature. Cached like a warp."""

    def table(self, alias: str) -> AuxTable:
        """.at(date) -> scalar. No gridding."""

    @property
    def granule_index(self) -> GranuleFrame: ...
```

Addressed by **manifest alias, never URI** — that is what makes the declaration requirement in
[05 §5](05-ancillary-data.md) enforceable. An undeclared alias raises; it does not fall back to
opening a path.

---

## 7. GLT

```python
@dataclass
class GLT:
    data: np.ndarray              # (H, W, 3) int32
    grid: GridDef
    tile: TileRef
    granule: GranuleRef           # one GLT per granule; band 3 is always 1
    score: np.ndarray | None      # (H, W) float32 - the winning score
```

Bands are `(GLT X, GLT Y, File Index)`, 1-based, `0` = nodata, negatives = interpolated. Band 3 is
always `1` and is kept for interoperability; `granule_id` and `grid.id` are in the file's metadata
([03 §2](03-regrid-glt.md)).

`score` is typed optional, and for a GLT regrid writes it **is** `None`: a per-granule GLT is
built before any selection happens, so there is no winning score to persist. "Why did this pixel
win?" is answered from the epoch snapshot's `score` layer instead ([03 §2](03-regrid-glt.md),
[13 §2](13-snapshot-schema.md)). The field is kept so a GLT read back from a fused, V002-era file can
carry the score that file has.

**[design] `EmbeddedGLT`** (`src/stratum/types.py`, directly after `GLT`) is the type for the
lookup table a product ships on its *own* ortho grid — the **[observed]** `location/glt_x`,
`glt_y` below. It is not on any Stratum grid, so unlike `GLT` it has no `grid`, `tile` or
`granule`; it carries its own `transform` (from the file's `geotransform`) and `crs` (the
`spatial_ref` WKT), and a third band `hit` mirroring Stratum's file-index band.
`GranuleReader.glt()` returns one (§10) and `warp_embedded` consumes it. Keeping it a separate
type is what stops a granule-ortho array being mistaken for a tile-grid one — the confusion §1
warns about.

> **[observed] The granule already ships a GLT.** `location/glt_x` and `location/glt_y`
> (`int32`, `_FillValue = 0`; observed ranges 1–1242 crosstrack, 1–1664 downtrack) map the
> granule's own ortho grid
> (2363 × 2309) back to sensor space. This is *not* our tile grid — the origin is per-granule — so
> it cannot substitute for the KD-tree regrid as-is. It is what `GranuleReader.glt()` returns and
> what the `warp_embedded` regrid method warps onto the tile grid ([03 §3](03-regrid-glt.md)); it is
> also what `SpectralUtil`'s `load_data(..., load_glt=True)` returns, and a ready-made fixture for
> testing GLT application without building one.

---

## 8. Snapshots, layers and outputs

```python
@dataclass(frozen=True)
class LayerSpec:
    name: str
    kind: Literal["categorical", "continuous"]
    source: str                           # role or band alias - one namespace
    dtype: str                            # defaulted from the source band
    bands: tuple[int, ...] | None         # subset of a multi-band source; None = all
    classes: ClassTable | None            # the enumeration; categorical only
    aggregate: Aggregation                # method + params - 13 section 4

@dataclass(frozen=True)
class Aggregation:
    method: str                           # vote | best | median | ... | none
    params: Mapping[str, object]          # min_count, ignore, conditional_on, unc, spread, ...

@dataclass(frozen=True)
class SnapshotSchema:
    name: str
    layers: Sequence[LayerSpec]
    extends: Sequence[str] = ()           # ancestor schema names; their layers_hash is recorded - 13 section 5
    @property
    def layers_hash(self) -> str: ...     # enters the snapshot key
    @property
    def aggregate_hash(self) -> str: ...  # enters the product key

@dataclass(frozen=True)
class BandSpec:
    """A delivered output band. Derived from the schema, or declared by a Reducer plugin."""
    name: str
    dtype: str
    description: str
    units: str = "unitless"
    nodata: float | int | None = None

class SnapshotStack:
    """Reducer input. Epoch-major."""
    schema: SnapshotSchema
    epochs: Sequence[Epoch]           # ordered, ascending
    def __getitem__(self, layer: str) -> np.ndarray:  # (n_epochs, H, W) or (n_epochs, H, W, B)
    @property
    def valid(self) -> np.ndarray:                    # (n_epochs, H, W) bool
    @property
    def score(self) -> np.ndarray:                    # (n_epochs, H, W) float32

class BandStack:
    """OutputMapper input. Finished bands for one tile."""
    def __getitem__(self, band: str) -> np.ndarray:   # (H, W)
    specs: Sequence[BandSpec]
```

**[design] Three additive changes from the build** (`src/stratum/types.py`):

- `LayerSpec.dtype` is optional at manifest level and filled by the planner from the source
  `VarSpec`, so `layers_hash` is final in `plan.json` and asserted on reload; categorical layers
  default to `uint16`. `LayerSpec.classes` is likewise `None` for `classes: source` until the
  planner substitutes the granules' own table ([13 §3](13-snapshot-schema.md)).
- `LayerSpec.lumping` holds `Enumeration.fingerprint()` — the whole enumeration document, members
  included — and enters `identity()`, so a change to *which raw classes* map to a product id
  moves `layers_hash` even when every `(id, name)` is unchanged. Without it a re-lumped schema
  would read stale snapshots as hits ([06 §3](06-caching.md) rule 1, [13 §5](13-snapshot-schema.md)).
- `BandSpec.bands` (default `1`) says how many bands a delivered band keeps from a multi-band
  layer, because the schema alone cannot know a `bands: None` source's width; the reducer's
  `band_counts(snaps)` supplies it from a real stack ([13 §2](13-snapshot-schema.md)).

### What a snapshot holds

Exactly the layers `SnapshotSchema` declares, plus `score` and `valid`, which the framework always
adds. A categorical layer holds product ids from its enumeration, never a raw class
([13 §3](13-snapshot-schema.md)). Scorer-*computed* layers are a planned enhancement, not in v1
([04 §4](04-cost-functions.md)).

A snapshot is an **internal artifact** — nothing renders it and nothing outside the pipeline reads
it — so its width is a design choice rather than a product constraint. Carrying geometry,
acquisition time or per-candidate evidence costs `O(block × n_layers)` in a streaming worker,
independent of observation count. Storage is the real limit, not memory.

`SnapshotStack.valid` is what keeps `min_count` honest: an epoch with no observation over a pixel
must not count toward agreement. `score` is always populated, so an aggregation may weight by
confidence — `tie_break: highest_score`, `score_weighted` and `best` all depend on it
([13 §4](13-snapshot-schema.md)).

**Every snapshot in a run shares one `SnapshotSchema`**, and its `layers_hash` is in every
snapshot's cache key ([06 §2](06-caching.md)). A `Reducer` plugin receives the schema through
`snaps.schema` and is never handed epochs written under two of them.

---

## 9. Class tables

Some bands are categorical: their integer values index a table of classes. The framework needs a
generic way to carry that table around. **It must not know what the classes mean.**

> **Layering rule.** Nothing in `stratum` core knows about Tetracorder, minerals, spectral
> libraries or EMIT. It knows that a band may have a class table, that the table has a key column
> and some attribute columns, and where to find it. Every domain specific of what those attributes
> signify lives in `stratum_emit`.

### Self-describing products are the good case

**[observed]** The L2B granule embeds its own class table at `/mineral_metadata`
(`minerals = 294`), with columns `index`, `record`, `name`, `library`, `group`, `url`.
`group_N_mineral_id` values index into it.

This is the arrangement to prefer wherever a product offers it, because **the authority travels
with the data**. There is no external CSV to drift, no version to pin by hand, and no way for the
table to disagree with the pixels it describes. A reader that pulls the table out of the granule
it is currently processing is correct by construction.

```python
@dataclass(frozen=True)
class ClassTable:
    key: str                              # column whose values appear in pixels
    entries: pa.Table                     # key + arbitrary attribute columns
    source: str                           # provenance: URI + path, or file
    def attrs(self) -> Sequence[str]: ...
    def fingerprint(self) -> str: ...     # content hash; enters cache keys
```

### Where to find it is config

Declared per role, so the framework never hard-codes a path — and so products that *don't* embed
a table are equally expressible:

```yaml
inputs:
  roles:
    mineral:
      collection: EMITL2BMIN
      var: group_1_mineral_id
      class_table:
        source: embedded          # embedded | file
        path: /mineral_metadata   # group within the product
        key: index                # column matching pixel values
        attributes: [name, record, library, group, url]
```

### The residual problem, and why it is now tractable

A mosaic spans many granules, and their class tables must agree — a pixel value of `15` has to
mean the same thing in every granule contributing to a cell, or the reduction is nonsense.

**[observed]** `index` is 1…294 contiguous and positional. `v6.00a6.csv` has 312 entries. So the
index space genuinely does differ between vintages, and during the reprocessing window a run can
span both.

The embedded table does not make that go away. What it does is make it **detectable and
mechanically solvable**, where an external table would have made it silent:

1. **Fingerprint every granule's table** at plan time.
2. **All identical** → the common case. Proceed; record the fingerprint in provenance.
3. **They differ** → the run spans vintages. Fail loudly by default; under
   `allow_mixed_vintage` accept them only if each resolves fully into the product enumeration,
   which *is* the remap through the attribute columns ([13 §3](13-snapshot-schema.md)). Measured on the delivered
   file, `(library, record, group)` resolves 292 of 294 entries — so a remap is computable, with a
   short list of genuine ambiguities surfaced for a human rather than guessed.

That check is generic: it is "do these class tables agree", not "do these minerals agree", and it
belongs in the core.

### Output products carry their own table

A mosaic's classes are its own — post-lumping, they are not the input classes. The publish stage therefore
writes a `ClassTable` for the product alongside it, which is the same requirement as the legend
in [07 §2](07-output-mapping.md), reached from the other direction.

---

## 10. Data access

The types below sit *underneath* `ObsWindow` — the framework uses them to fill it, and no plugin
in the science tier ever sees one. Contracts and rationale are in
[12](12-data-access.md).

```python
@dataclass(frozen=True)
class SensorWindow:
    """A rectangle in a granule's own sensor array. Carries its ORIGIN, not just a shape."""
    row0: int
    col0: int
    height: int
    width: int

    @classmethod
    def covering(cls, glt_x: np.ndarray, glt_y: np.ndarray) -> "SensorWindow": ...
    @property
    def slices(self) -> tuple[slice, slice]: ...

@dataclass(frozen=True)
class VarSpec:
    name: str
    dtype: str
    shape: tuple[int, ...]
    fill: float | int | None
    units: str = "unitless"
    band_attrs: Mapping[str, Sequence[object]] = {}   # per-band attributes the file carries,
                                                     # e.g. wavelength; never interpreted by the core

class GranuleReader(Protocol):
    collections: tuple[str, ...]          # index `collection` values this reader claims
    space: Literal["sensor", "ortho"]

    def open(self, asset: AssetHandle) -> ReaderContext: ...
    def variables(self, ctx: ReaderContext) -> Mapping[str, VarSpec]: ...
    def read(self, ctx, var: str, window: SensorWindow | None = None) -> MaskedArray: ...
    def geolocation(self, ctx) -> LocArray | None: ...
    def glt(self, ctx) -> GLT | EmbeddedGLT | None: ...   # the product's own lookup table, on its grid
    def class_table(self, ctx, path: str, key: str, attributes) -> ClassTable | None: ...

class GranuleSource(Protocol):
    name: str
    def search(self, *, collections, bbox, start, end,
               updated_since: datetime | None = None) -> Iterator[GranuleRecord]: ...
    def assets(self, record: GranuleRecord) -> Mapping[str, str]: ...

class AssetStore:
    def open(self, uri: str, *, etag: str | None = None) -> AssetHandle: ...
    def stage(self, uri: str) -> Path: ...
    def credentials_for(self, uri: str) -> Credentials: ...
```

The supporting types those signatures name:

```python
@dataclass(frozen=True)
class GranuleRecord:
    """One catalogue answer, before it becomes an index row. Source-shaped, not schema-shaped."""
    native_id: str                      # GranuleUR, or a path for LocalSource
    collection: str
    datetime: datetime
    end_datetime: datetime
    geometry: BaseGeometry              # footprint, EPSG:4326
    attributes: Mapping[str, object]    # everything else the source returned, verbatim
    raw: object                         # the untranslated record, for debugging a mapping

class AssetHandle(Protocol):
    """An openable asset. A reader never learns which mode produced it."""
    uri: str
    etag: str | None
    def path(self) -> Path | None: ...  # a local path when staged, else None
    def vsi(self) -> str: ...           # a GDAL/fsspec-openable string, always

class ReaderContext(Protocol):
    """Whatever a reader keeps between open() and read(). Opaque to the framework."""
    asset: AssetHandle

@dataclass(frozen=True)
class Credentials:
    kind: Literal["aws", "bearer", "netrc", "none"]
    expires: datetime | None            # None when it does not expire
    def as_env(self) -> Mapping[str, str]: ...
```

`LocArray` is the geolocation triple `geolocation()` returns — `lat`, `lon` and `elev`, each
`(downtrack, crosstrack)` `float64` in sensor space with fills already masked. It is the KD-tree
input and nothing else consumes it ([03 §3](03-regrid-glt.md)). **[design]** The EMIT readers
return it as plain arrays with `NaN` fills rather than masked arrays — a deliberate departure from
rule 2 below, because the regrid wrapper consumes `NaN` and nothing in the science tier ever sees
a `LocArray`; the wrapper drops masked *or* non-finite points and does not recognise `-9999`.

**[design]** `GranuleReader.glt()` returns `GLT | EmbeddedGLT | None` — widened from `GLT | None`
when the embedded type was added (§7); nothing else in the protocol changed. Readers are
instantiated with no arguments and cached per process (`src/stratum/access/readers.py`); they
hold no per-file state — that is the `ReaderContext` — so sharing one across granules is safe.

Three properties are worth stating as type-level guarantees, because each has a failure mode that
is invisible if it is left to convention:

1. **`SensorWindow` carries `row0`/`col0`.** A sensor-space `PixelMask` needs the *absolute*
   crosstrack column to know whether it is on a detector edge ([04 §3](04-cost-functions.md)). A
   bare `(h, w)` window would let `EdgeTrim` trim the edge of every block instead of the edge of
   the detector, and the result would look plausible.
2. **`GranuleReader.read` returns a masked array, never sentinels.** The reader is the last place
   `-9999` exists. Everything above it works in the internal convention of §2, which is what keeps
   "not observed" and "observed, nothing identified" distinguishable all the way to a `Reducer`.
3. **`space` is declared, not inferred.** An ortho-native product like L2B FRCOV has no sensor
   space and no `loc` array; asking it for a KD-tree regrid is a plan-time error rather than a
   confusing failure in a worker.

`GranuleReader` and `GranuleSource` are plugin hooks, but they belong to the **access tier**, not
the science tier of [04](04-cost-functions.md). A new instrument is a reader; a new archive is a
source; neither changes what a mosaic means.

---

## 11. Observed oddities

Recorded so nobody re-derives them.

- **Science variables are one gzip chunk each.** `group_N_mineral_id` and `group_N_band_depth` are
  stored as a single `(1664, 1242)` chunk with gzip and shuffle; `location/lat` and `lon` are
  contiguous and uncompressed; the embedded GLT is four `(1182, 1155)` gzip chunks. A windowed read
  of a science band therefore decodes the whole variable ([12 §2](12-data-access.md)).
- **`bands = 4` is declared but unused** by any root variable in the L2B MIN file. Presumably for
  the MINUNCERT companion. Do not assume it means four spectral bands.
- **Only two mineral groups** exist (`group_1_*`, `group_2_*`), matching AMD's config.
- The delivered GLT contains **no negative values**, so the interpolated-cell convention is
  Stratum/SpectralUtil-internal, not something read from granules.
- Global attributes carry `geotransform` and `spatial_ref` (WKT) directly, so granule-ortho
  georeferencing needs no reconstruction.
- Conventions are `CF-1.13` / `NCEI_NetCDF_Swath_Template_v2.0` — worth matching in our own
  NetCDF output.

---

## 12. Open questions

1. ~~Are `software_build_version` / `product_version` exposed by CMR before download?~~
   **Resolved:** the build version is; `product_version` is not, and tracks the collection version
   ([12 §5](12-data-access.md)).
2. ~~Should `ObsWindow` expose per-granule uncertainty as a first-class alias, or is it just another
   role?~~ **Resolved:** a role. Nothing about uncertainty is special to the framework.
3. ~~Is fingerprint-and-assert enough for class-table agreement in practice, or is cross-vintage
   remapping needed in v1?~~ **Resolved:** resolution into the schema's enumeration is the remap
   ([13 §3](13-snapshot-schema.md)). Fingerprint-and-fail remains the default, and
   `allow_mixed_vintage` admits differing tables only when each resolves fully.
4. ~~Should `ClassTable` be `pyarrow.Table`, or a plain dataclass of columns?~~ **Resolved:**
   `pyarrow.Table`. Fingerprinting and attribute matching are easy, and GeoParquet already implies
   it.
5. `GranuleReader.glt()` returning two types is a seam that `warp_embedded` (not yet built) will
   have to dispatch on. Whether `GLT` should stop being a reader return type altogether — a reader
   never produces a tile-grid GLT — is a question for the wave that builds `warp_embedded`.
