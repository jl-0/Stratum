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

---

## 1. Coordinate spaces

Three spaces, and confusing them is the most likely source of silent error. Every array-bearing
type below declares which one it is in.

| Space | Axes | Extent **[observed]** | Where it appears |
|---|---|---|---|
| **sensor** | `(downtrack, crosstrack)` | 1664 × 1242 | Raw L2B/L1B variables; sensor-space `PixelMask` |
| **granule-ortho** | `(ortho_y, ortho_x)` | 2363 × 2309 | The GLT *shipped inside the granule*; see §7 |
| **block** | `(y, x)` | run-defined, e.g. 512 × 512 | Everything from stage 3 onward |

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
| `location/glt_x,glt_y` | `int32` | `0` | 1-based indices; observed range 1–1242, **no negatives** |
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
    build_version: str            # "010635"          [observed]
    product_version: str          # "V001"            [observed]
    cloud_fraction: float | None  # None is meaningful - see 02 section 4
    day_night: str | None         # "Day"             [observed]
```

`GranuleFrame` is the plural form: a dataframe with these as columns, which is what
`GranuleFilter.keep()` receives. Filters are vectorized over it and never see individual granules.

> **[observed] Vintage identifiers.** The open question in [02 §3](02-granule-index.md) is now
> partly answered: the granule carries `software_build_version` (`010635`),
> `software_delivery_version`, and `product_version` (`V001`). Its `history` attribute names
> `tetracorder5.27c.cmds`, confirming this file predates the Tetracorder 6 reprocessing.
> **Still open:** whether these are exposed by CMR *before* download.

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

An **epoch** is the unit of one vote: stage 3 resolves it to exactly one snapshot per cell, however
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
re-run stage 4 only — a 13-month window delivered monthly computes each snapshot once and reads it
thirteen times.

---

## 5. `ObsWindow`

The type every `Scorer` and `PixelMask` receives. It has two forms and the difference is one axis.

```python
class ObsWindow:
    space: Literal["sensor", "block"]
    n: int                        # 1 in streaming mode, N in stack mode
    block: BlockRef | None        # None in sensor space
    granule: GranuleRef | None    # None in stack mode - see granules
    granules: Sequence[GranuleRef]

    def __getitem__(self, alias: str) -> np.ndarray:
        """Band by alias. Shape (H, W) streaming, (N, H, W) stacked."""

    @property
    def valid(self) -> np.ndarray:
        """Bool. True = observed and unmasked. Same shape as a band."""
```

### Aliases, not indices

`obs["view_zenith"]`, never `obs[:, :, 5]`. The manifest maps aliases to `(role, band)` per
collection ([09 §2](09-run-manifest.md)), so the same scorer works across instruments — which
matters now that multi-instrument is a live requirement, not a someday one.

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
    def raster(self, alias: str, *, date: datetime | None = None) -> np.ndarray:
        """(H, W) on THIS block's grid. Warped, windowed, cached."""

    def vector(self, alias: str) -> np.ndarray:
        """Rasterized to this block's grid."""

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
    granules: Sequence[GranuleRef]    # File Index is 1-based into this
    score: np.ndarray | None      # (H, W) float32 - the winning score
```

Bands are `(GLT X, GLT Y, File Index)`, 1-based, `0` = nodata, negatives = interpolated.

`score` is not optional in practice — persisting it is a requirement from
[03 §2](03-regrid-glt.md), since "why did this pixel win?" must be answerable from the artifact.
It is typed optional only because a GLT read back from a V002-era file will not have one.

> **[observed] The granule already ships a GLT.** `location/glt_x` and `location/glt_y`
> (`int32`, `_FillValue = 0`, observed range 1–1242) map the granule's own ortho grid
> (2363 × 2309) back to sensor space. This is *not* our tile grid — the origin is per-granule — so
> it cannot substitute for the KD-tree regrid. It is worth knowing about for two reasons: it is
> what `SpectralUtil`'s `load_data(..., load_glt=True)` returns, and it is a ready-made fixture
> for testing GLT application without building one.

---

## 8. Snapshots, bands and outputs

```python
@dataclass(frozen=True)
class BandSpec:
    name: str
    dtype: str
    description: str
    units: str = "unitless"
    nodata: float | int | None = None

class SnapshotStack:
    """Reducer input. Epoch-major."""
    epochs: Sequence[Epoch]           # ordered, ascending
    def __getitem__(self, band: str) -> np.ndarray:   # (n_epochs, H, W)
    @property
    def valid(self) -> np.ndarray:                    # (n_epochs, H, W) bool
    @property
    def score(self) -> np.ndarray:                    # (n_epochs, H, W) float32

class BandStack:
    """OutputMapper input. Finished bands for one tile."""
    def __getitem__(self, band: str) -> np.ndarray:   # (H, W)
    specs: Sequence[BandSpec]
```

### Snapshot schema

`SnapshotStack.__getitem__` resolves any band the `Scorer` named in `carry`
([04 §4](04-cost-functions.md)), plus `score` and `valid`, which the framework always adds.
Scorer-*computed* bands are a planned enhancement, not in v1 — see the same section.

A snapshot is an **internal artifact** — nothing renders it and nothing outside the pipeline reads
it — so its width is a design choice rather than a product constraint. Carrying geometry,
acquisition time, runner-up margin or per-candidate evidence costs `O(block × n_bands)` in a
streaming worker, independent of observation count. Storage is the real limit, not memory.

`SnapshotStack.valid` is what keeps `min_count` honest: an epoch with no observation over a pixel
must not count toward agreement. And exposing `score` lets a reducer weight by confidence rather
than treating every epoch equally — the open question in [04 §9](04-cost-functions.md), left
available in the type so it stays cheap to answer later.

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
3. **They differ** → the run spans vintages. Either fail loudly, or remap through the attribute
   columns, which carry enough identity to compute the correspondence. Measured on the delivered
   file, `(library, record, group)` resolves 292 of 294 entries — so a remap is computable, with a
   short list of genuine ambiguities surfaced for a human rather than guessed.

That check is generic: it is "do these class tables agree", not "do these minerals agree", and it
belongs in the core.

### Output products carry their own table

A mosaic's classes are its own — post-lumping, they are not the input classes. Stage 5 therefore
publishes a `ClassTable` for the product alongside it, which is the same requirement as the legend
in [07 §2](07-output-mapping.md), reached from the other direction.
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

class GranuleReader(Protocol):
    collections: tuple[str, ...]          # index `collection` values this reader claims
    space: Literal["sensor", "ortho"]

    def open(self, asset: AssetHandle) -> ReaderContext: ...
    def variables(self, ctx: ReaderContext) -> Mapping[str, VarSpec]: ...
    def read(self, ctx, var: str, window: SensorWindow | None = None) -> MaskedArray: ...
    def geolocation(self, ctx) -> LocArray | None: ...
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

1. Are `software_build_version` / `product_version` exposed by CMR before download? Decides
   whether vintage filtering is an index predicate or requires touching files.
2. Should `ObsWindow` expose per-granule uncertainty as a first-class alias, or is it just another
   role? Leaning role.
3. Is fingerprint-and-assert enough for class-table agreement in practice, or is cross-vintage
   remapping needed in v1? Assert first; verify on a real multi-granule set spanning the
   reprocessing boundary before deciding.
4. Should `ClassTable` be `pyarrow.Table`, or a plain dataclass of columns? Arrow makes
   fingerprinting and attribute matching easy and is already implied by GeoParquet.
