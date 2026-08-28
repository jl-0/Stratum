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

`SnapshotStack.valid` is what keeps `min_count` honest: an epoch with no observation over a pixel
must not count toward agreement. And exposing `score` lets a reducer weight by confidence rather
than treating every epoch equally — the open question in [04 §9](04-cost-functions.md), left
available in the type so it stays cheap to answer later.

---

## 9. Mineral identity — the version problem

**[observed]** The granule embeds its own reference table at `/mineral_metadata`, dimension
`minerals = 294`:

| Variable | Type | Notes |
|---|---|---|
| `index` | `uint32` | **1…294, contiguous** — positional |
| `record` | `uint32` | specpr record number |
| `name` | `str` | e.g. `Goethite WS222 <250um MedGrn W1R1H_ AREF` |
| `library` | `str` | `splib06` or `sprlb06` |
| `group` | `uint32` | 1 or 2 (95 and 199 entries respectively) |
| `url` | `str` | USGS metadata page |

`group_N_mineral_id` values index into this table. **[observed]** group 1 in this granule uses 22
distinct values in 0…76.

### There is no stable unique key in V001

Measured on the delivered file:

| Candidate key | Unique | Duplicate keys |
|---|---:|---:|
| `index` | **294 / 294** | 0 |
| `(library, record, group)` | 292 / 294 | 2 |
| `(name, group)` | 292 / 294 | 2 |
| `(library, record)` | 282 / 294 | 12 |
| `name` | 283 / 294 | 11 |

Most `(library, record)` collisions are the same spectrum appearing in both groups — e.g.
`splib06/2568` Jarosite at `index 15` (group 1) and `index 205` (group 2) — so adding `group`
resolves all but two.

This is exactly why `tetracorder-lite` PR #20 deduplicated the matrix and added an `id` column.

> **Consequence.** In V001, **`index` is the only unique key, and it is positional.** The delivered
> file has 294 entries; `tetrapy/data/v6.00a6.csv` has 312. The index spaces are therefore
> *already different between vintages*, and a lumping or colour table keyed on `index` silently
> remaps minerals when the vintage changes.
>
> So a V001 class table is **vintage-locked by construction**. This strengthens rather than
> softens the pinning requirement in [02 §3](02-granule-index.md): the class table is only
> meaningful for the vintage it was built against.

### Practical policy

1. Class tables **declare the vintage they were built for** and fail validation against any other.
2. Tables carry `(library, record, group, name)` alongside `index` as **provenance**, so a
   migration between vintages can be computed and the ~2 ambiguous entries reconciled by hand
   rather than guessed.
3. **[design]** When V002 lands with the added metadata, migrate the primary key to the stable
   `id` slug and relax rule 1. Per Phil, V002 only *adds* metadata, so a V001-only reader stays
   forward-compatible — but a V001-keyed *class table* does not, and that distinction is the whole
   point of this section.

```python
@dataclass(frozen=True)
class MineralEntry:
    index: int; record: int; name: str; library: str; group: int; url: str

@dataclass(frozen=True)
class MineralTable:
    entries: Sequence[MineralEntry]
    vintage: str                  # build/product version it came from
    @classmethod
    def from_granule(cls, path) -> "MineralTable": ...   # read /mineral_metadata
```

Reading the table **from the granule** rather than a checked-in CSV is preferable where possible:
it cannot drift from the data it describes.

---

## 10. Observed oddities

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

## 11. Open questions

1. Are `software_build_version` / `product_version` exposed by CMR before download? Decides
   whether vintage filtering is an index predicate or requires touching files.
2. Should `ObsWindow` expose per-granule uncertainty as a first-class alias, or is it just another
   role? Leaning role.
3. Does `MineralTable.from_granule` need to reconcile tables across granules in one run, or can we
   assert they are identical within a pinned vintage? Assert, and fail loudly — but verify on a
   real multi-granule set first.
