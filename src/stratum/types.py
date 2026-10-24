"""Core types - the objects that cross every interface. Spec: docs/specs/11-types.md.

Section numbers in comments refer to that spec. If a signature here changes, the spec changes in
the same commit.
"""
from __future__ import annotations

import hashlib
import json
import math
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

import numpy as np
from affine import Affine

if TYPE_CHECKING:  # heavy imports stay out of the hot path
    import pandas as pd
    import pyarrow as pa
    from shapely.geometry.base import BaseGeometry

Space = Literal["sensor", "granule-ortho", "block"]


def canonical_hash(obj: Any) -> str:
    """sha256 of a canonical JSON document: sorted keys, no whitespace (06 section 2)."""
    doc = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(doc.encode()).hexdigest()


# --------------------------------------------------------------------------- 3. grid, tile, block
@dataclass(frozen=True)
class Window:
    """A pixel rectangle in some array. Offsets are (row, col)."""

    row_off: int
    col_off: int
    height: int
    width: int

    @property
    def slices(self) -> tuple[slice, slice]:
        return (slice(self.row_off, self.row_off + self.height),
                slice(self.col_off, self.col_off + self.width))


@dataclass(frozen=True)
class GridDef:
    """CRS + resolution + origin. No extent of its own (01 section 1). Cell edges fall at
    origin + n * resolution; tiles are cut on that lattice, so they align whatever the ratio of
    tile_size to resolution."""

    crs: str
    resolution: tuple[float, float]  # (x, y); y negative
    origin: tuple[float, float]      # (x0, y0); cell edges at origin + n * res
    tile_size: float
    block_size: int = 512

    def __post_init__(self) -> None:
        rx, ry = self.resolution
        if rx <= 0 or ry >= 0:
            raise ValueError("resolution must be (positive x, negative y) (01 section 1 guard rail)")
        if self.tile_size <= 0 or self.block_size <= 0:
            raise ValueError("tile_size and block must be positive")

    @property
    def id(self) -> str:
        """Short stable hash; part of every cache key."""
        return canonical_hash({"crs": self.crs, "resolution": self.resolution,
                               "origin": self.origin, "tile_size": self.tile_size})[7:23]

    @property
    def nominal_cells_per_tile(self) -> float:
        """tile_size / resolution. A whole number means every tile has the same shape and its
        bounds sit exactly on the nominal edges; otherwise tiles differ by one cell."""
        return self.tile_size / self.resolution[0]

    @property
    def divides(self) -> bool:
        n = self.nominal_cells_per_tile
        return abs(n - round(n)) < 1e-6

    @property
    def diagonal(self) -> float:
        rx, ry = self.resolution
        return math.hypot(rx, ry)

    def cell_range(self, lo: float, hi: float, axis: int) -> tuple[int, int]:
        """Lattice indices [i0, i1) of the cells along one axis whose centres lie in [lo, hi).
        axis 0 is x (east), axis 1 is y (north); both measured from the origin."""
        step = abs(self.resolution[axis])
        o = self.origin[axis]
        i0 = math.ceil((lo - o) / step - 0.5 - 1e-9)
        i1 = math.ceil((hi - o) / step - 0.5 - 1e-9)
        return (i0, i1)


@dataclass(frozen=True)
class TileRef:
    """A bounded rectangle of the grid; the product unit. Named by position (01 section 2):
    tile (tx, ty) nominally spans [tx, tx+1) * tile_size east and north. Its cells are the ones
    whose centres fall inside that span, so neighbours never overlap and never gap."""

    grid: GridDef
    tx: int
    ty: int

    @property
    def nominal_bounds(self) -> tuple[float, float, float, float]:
        ts = self.grid.tile_size
        return (self.tx * ts, self.ty * ts, (self.tx + 1) * ts, (self.ty + 1) * ts)

    @property
    def col_range(self) -> tuple[int, int]:
        x0, _, x1, _ = self.nominal_bounds
        return self.grid.cell_range(x0, x1, 0)

    @property
    def row_range(self) -> tuple[int, int]:
        """Lattice rows counted northward from the origin; rasters are written north-up."""
        _, y0, _, y1 = self.nominal_bounds
        return self.grid.cell_range(y0, y1, 1)

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        """The tile's actual bounds on the lattice; equal to nominal_bounds when the grid divides."""
        c0, c1 = self.col_range
        r0, r1 = self.row_range
        ox, oy = self.grid.origin
        rx, ry = self.grid.resolution[0], abs(self.grid.resolution[1])
        return (ox + c0 * rx, oy + r0 * ry, ox + c1 * rx, oy + r1 * ry)

    @property
    def shape(self) -> tuple[int, int]:
        c0, c1 = self.col_range
        r0, r1 = self.row_range
        return (r1 - r0, c1 - c0)

    @property
    def name(self) -> str:
        return f"{self.grid.id}/{self.tx}_{self.ty}"

    @property
    def transform(self) -> Affine:
        x0, _, _, y1 = self.bounds
        rx, ry = self.grid.resolution
        return Affine(rx, 0.0, x0, 0.0, ry, y1)

    def blocks(self) -> list[BlockRef]:
        """Every block of a fully covered tile; the planner clips to the AOI (01 section 3)."""
        rows, cols = self.shape
        b = self.grid.block_size
        return [BlockRef(self, bx, by)
                for by in range(math.ceil(rows / b)) for bx in range(math.ceil(cols / b))]


@dataclass(frozen=True)
class BlockRef:
    """A subdivision of a tile; the compute unit. Two windows, deliberately (11 section 3)."""

    tile: TileRef
    bx: int
    by: int
    halo: int = 0

    @property
    def core_window(self) -> Window:
        b = self.tile.grid.block_size
        rows, cols = self.tile.shape
        r0, c0 = self.by * b, self.bx * b
        return Window(r0, c0, min(b, rows - r0), min(b, cols - c0))

    @property
    def window(self) -> Window:
        c = self.core_window
        h = self.halo
        return Window(c.row_off - h, c.col_off - h, c.height + 2 * h, c.width + 2 * h)

    @property
    def transform(self) -> Affine:
        w = self.window
        return self.tile.transform * Affine.translation(w.col_off, w.row_off)

    @property
    def name(self) -> str:
        return f"{self.tile.name}/{self.bx}_{self.by}"


# ------------------------------------------------------------------------------------- 4. granules
@dataclass(frozen=True)
class GranuleRef:
    granule_id: str
    collection: str
    datetime: datetime
    end_datetime: datetime
    bbox: tuple[float, float, float, float]
    assets: Mapping[str, str]          # asset name -> URI (02 section 5)
    build_version: str                 # granule-level; CMR SOFTWARE_BUILD_VERSION
    product_version: str               # file header; not in CMR
    collection_version: str            # the collection's own version
    cloud_fraction: float | None       # None is meaningful (02 section 4)
    day_night: str | None = None
    attributes: Mapping[str, str] = field(default_factory=dict)
    # asset name -> catalogue checksum, keyed like `assets` (02 section 2). The asset identity
    # that enters the masked-observation key (12 section 4); empty when the source has none
    # (a local directory), which is recorded as such rather than invented.
    checksums: Mapping[str, str] = field(default_factory=dict)


if TYPE_CHECKING:
    GranuleFrame = pd.DataFrame  # the index as a dataframe; what GranuleFilter.keep() receives
else:
    GranuleFrame = Any


@dataclass(frozen=True)
class Epoch:
    """One vote. Half-open [start, end), UTC (11 section 4)."""

    start: datetime
    end: datetime

    def contains(self, when: datetime) -> bool:
        return self.start <= when < self.end

    @property
    def bounds(self) -> tuple[str, str]:
        return (self.start.isoformat(), self.end.isoformat())


# ------------------------------------------------------------------------------------ 5. ObsWindow
@dataclass(frozen=True)
class Coords:
    """Cell centres for one block, halo included. Each array is (H, W)."""

    x: np.ndarray
    y: np.ndarray
    lon: np.ndarray
    lat: np.ndarray


class ObsWindow:
    """What every Scorer and PixelMask receives. Two forms; the difference is one axis.

    Per observation (`stacked=False`): bands, `valid` and `interpolated` are (H, W[, B]) and
    `granule` is the one look. Stacked (`stacked=True`, a `stack` scorer, 04 section 4): every
    band, layer, `valid` and `interpolated` gains a leading axis of length `n`, row i is
    `granules[i]`, and `granule` is None even at n == 1. `coords`, `grid`, `block` and `epoch`
    are one per block in both forms.
    """

    def __init__(
        self,
        *,
        space: Literal["sensor", "block"],
        bands: Mapping[str, np.ndarray],
        valid: np.ndarray,
        granules: Sequence[GranuleRef],
        epoch: Epoch,
        block: BlockRef | None = None,
        sensor_window: SensorWindow | None = None,
        interpolated: np.ndarray | None = None,
        coords: Coords | None = None,
        sensor_shape: tuple[int, int] | None = None,
        band_attrs: Mapping[str, Mapping[str, Sequence[Any]]] | None = None,
        layers: Mapping[str, np.ndarray] | None = None,
        stacked: bool = False,
        layer_classes: Mapping[str, ClassTable] | None = None,
    ) -> None:
        self.space = space
        self._bands = dict(bands)
        self._valid = valid
        self.granules = tuple(granules)
        self.epoch = epoch
        self.block = block
        self.sensor_window = sensor_window
        self._interpolated = interpolated
        self._coords = coords
        # The FULL (downtrack, crosstrack) extent of the sensor array the window was cut from
        # (VarSpec.shape[:2]); a sensor-space mask needs it to know where the detector ends,
        # not where the window ends (12 section 2, 04 section 3). None in block space.
        self.sensor_shape = sensor_shape
        # Per-band attributes the reader reported for each multi-band role or alias
        # (VarSpec.band_attrs, e.g. {"mask": {"name": [...]}}); never interpreted by the core.
        self.band_attrs: Mapping[str, Mapping[str, Sequence[Any]]] = dict(band_attrs or {})
        # The schema layers' gathered values (product ids for a categorical layer). Held BY
        # REFERENCE, not copied: `read_observation` builds the window before the map masks run
        # and fills this same dict after them, so a mask finds it empty and a scorer full.
        self._layers: Mapping[str, np.ndarray] = {} if layers is None else layers
        # Each categorical schema layer's PRODUCT class table (LayerSpec.classes): what turns a
        # class NAME into the product id `layer()` returns. One per run, shared by every window.
        self._layer_classes: Mapping[str, ClassTable] = dict(layer_classes or {})
        self.stacked = bool(stacked)
        if self.stacked:
            n = self.n
            shape = np.shape(valid)
            if len(shape) != 3 or shape[0] != n:
                raise ValueError(f"stacked ObsWindow: 'valid' has shape {shape}; expected "
                                 f"(n, H, W) with n = {n}")
            named = [*self._bands.items(), *self._layers.items()]
            if interpolated is not None:
                named.append(("interpolated", interpolated))
            for name, arr in named:
                k = np.shape(arr)[0] if np.ndim(arr) else None
                if k != n:
                    raise ValueError(f"stacked ObsWindow: {name!r} has leading axis {k}; "
                                     f"expected n = {n}")

    @property
    def n(self) -> int:
        return len(self.granules)

    @property
    def granule(self) -> GranuleRef | None:
        if self.stacked:
            return None
        return self.granules[0] if self.n == 1 else None

    @property
    def grid(self) -> Affine | None:
        return self.block.transform if self.block is not None else None

    def __getitem__(self, alias: str) -> np.ndarray:
        try:
            return self._bands[alias]
        except KeyError:
            raise KeyError(f"no band alias {alias!r}; declare it through required_roles "
                           "and the manifest (11 section 5)") from None

    def layer(self, name: str) -> np.ndarray:
        """A snapshot schema layer's gathered value for this look: product ids (int32) for a
        categorical layer, the native dtype for a continuous one; (H, W[, B]), or (n, H, W[, B])
        stacked. Scorers only: layers exist after the map masks ran, so a mask gets KeyError."""
        try:
            return self._layers[name]
        except KeyError:
            raise KeyError(f"no snapshot layer {name!r} on this observation; layers exist only "
                           "after the map masks ran, and a scorer declares the ones it reads "
                           "through required_layers (11 section 5)") from None

    def layer_classes(self, name: str) -> ClassTable:
        """The product class table of categorical schema layer `name` - the table whose keys are
        the ids `layer(name)` returns - so a scorer can name classes rather than hard-code ids
        (13 section 3). The same table in both forms and for every observation."""
        try:
            return self._layer_classes[name]
        except KeyError:
            raise KeyError(f"no class table for snapshot layer {name!r}; only a categorical "
                           f"layer has one, and the categorical layers here are "
                           f"{sorted(self._layer_classes)} (11 section 5)") from None

    def at(self, i: int) -> ObsWindow:
        """Observation `i` of a stacked window as a per-observation window: views of the kept
        rows, no copy and no read. Negative `i` counts from the end. On a per-observation
        window only 0 (itself) exists."""
        if not self.stacked:
            if i == 0:
                return self
            raise IndexError(f"observation index {i} out of range for a per-observation window")
        n = self.n
        if not -n <= i < n:
            raise IndexError(f"observation index {i} out of range for a stack of {n}")
        i = i % n
        return ObsWindow(
            space="block", bands={k: v[i] for k, v in self._bands.items()},
            valid=self._valid[i], granules=(self.granules[i],), epoch=self.epoch,
            block=self.block,
            interpolated=None if self._interpolated is None else self._interpolated[i],
            coords=self._coords, band_attrs=self.band_attrs,
            layers={k: v[i] for k, v in self._layers.items()},
            layer_classes=self._layer_classes)

    def __iter__(self) -> Iterator[ObsWindow]:
        if not self.stacked:
            yield self
            return
        for i in range(self.n):
            yield self.at(i)

    @property
    def valid(self) -> np.ndarray:
        return self._valid

    @property
    def interpolated(self) -> np.ndarray:
        if self._interpolated is None:
            return np.zeros_like(self._valid, dtype=bool)
        return self._interpolated

    @property
    def coords(self) -> Coords:
        if self._coords is None:
            raise ValueError("coords are only available in block space")
        return self._coords


# ---------------------------------------------------------------------------------- 6. AuxAccessor
class AuxTable(Protocol):
    def at(self, when: datetime) -> float: ...


class AuxAccessor(ABC):
    """Everything not an observation, already on the block grid (05 section 1).
    Addressed by manifest alias, never URI; an undeclared alias raises."""

    @abstractmethod
    def raster(self, alias: str, *, date: datetime | None = None,
               epoch: Epoch | None = None) -> np.ndarray: ...

    @abstractmethod
    def vector(self, alias: str) -> np.ndarray: ...

    @abstractmethod
    def features(self, alias: str, *, margin: float = 0.0) -> Sequence[BaseGeometry]: ...

    @abstractmethod
    def distance(self, alias: str, *, cutoff: float | None = None) -> np.ndarray: ...

    @abstractmethod
    def table(self, alias: str) -> AuxTable: ...

    @property
    @abstractmethod
    def granule_index(self) -> GranuleFrame: ...


# ------------------------------------------------------------------------------------------ 7. GLT
@dataclass
class GLT:
    """(H, W, 3) int32: GLT X, GLT Y, File Index. 1-based; 0 = nodata; negative = interpolated.
    Band 3 is always 1 - one GLT per granule - and is kept for interoperability (03 section 2)."""

    data: np.ndarray
    grid: GridDef
    tile: TileRef
    granule: GranuleRef
    score: np.ndarray | None = None

    @property
    def hit(self) -> np.ndarray:
        return self.data[..., 2] != 0

    @property
    def interpolated(self) -> np.ndarray:
        return (self.data[..., 0] < 0) | (self.data[..., 1] < 0)


@dataclass
class EmbeddedGLT:
    """The lookup table a product ships on its OWN ortho grid (11 section 7 [observed]).

    (H, W, 3) int32 with bands (GLT X, GLT Y, hit): 1-based sensor indices, 0 = nodata, band 3
    is 1 wherever bands 1-2 are set. It is not on any Stratum grid, so unlike `GLT` it has no
    grid, tile or granule - it carries its own georeferencing instead. `GranuleReader.glt()`
    returns one; the `warp_embedded` regrid method (03 section 3) warps it onto a tile."""

    data: np.ndarray
    transform: Affine
    crs: str

    @property
    def hit(self) -> np.ndarray:
        return self.data[..., 2] != 0

    @property
    def shape(self) -> tuple[int, int]:
        return (int(self.data.shape[0]), int(self.data.shape[1]))


# ------------------------------------------------------------------------- 8. snapshots and outputs
@dataclass(frozen=True)
class BandSpec:
    """A delivered output band. Derived from the schema, or declared by a Reducer plugin."""

    name: str
    dtype: str
    description: str
    units: str = "unitless"
    nodata: float | int | None = None
    bands: int = 1                     # >1 when the band keeps a layer's band axis (13 section 2)


@dataclass(frozen=True)
class Aggregation:
    """How one layer collapses through time (13 section 4)."""

    method: str
    params: Mapping[str, Any] = field(default_factory=dict)

    def canonical(self) -> dict[str, Any]:
        return {"method": self.method, "params": dict(sorted(self.params.items()))}


#: Layer sources the FRAMEWORK provides (13 section 2): not a role, not a band alias, never read
#: from a file. name -> storage dtype. `acquired` is the observation's acquisition start in Unix
#: seconds, UTC: float64, because float32 cannot hold a 2020s timestamp to the second, and
#: exact to the second is what makes it name the granule - an EMIT scene's start time is
#: unique. It is the per-cell provenance that a pixel -> granule -> spectrum click-through needs
#: and that a published mosaic cannot recover later (04 section 4, "Derived snapshot bands").
BUILTIN_SOURCES: Mapping[str, str] = {"acquired": "float64"}

#: A layer source naming one of the SCORER's declared `outputs` (04 section 4, 13 section 2):
#: `source: scorer.<output>`. Not a role, not an alias - the prefix is reserved in the one
#: namespace - and read from no file: the scorer's `emit()` computes it during resolve.
SCORER_SOURCE_PREFIX = "scorer."


def scorer_output(source: str) -> str | None:
    """The output name a `scorer.<output>` layer source names; None for any other source."""
    if source.startswith(SCORER_SOURCE_PREFIX):
        return source[len(SCORER_SOURCE_PREFIX):]
    return None


def acquired_seconds(when: datetime) -> float:
    """A granule's `acquired` value: Unix seconds, UTC; a naive datetime is UTC."""
    return (when if when.tzinfo is not None else when.replace(tzinfo=UTC)).timestamp()


@dataclass(frozen=True)
class LayerSpec:
    name: str
    kind: Literal["categorical", "continuous"]
    source: str                              # role or band alias - one namespace
    aggregate: Aggregation
    dtype: str | None = None                 # defaulted from the source band
    bands: tuple[int, ...] | None = None     # subset of a multi-band source; None = all
    classes: ClassTable | None = None        # the enumeration's product table; categorical only
    # The fingerprint of the whole enumeration document - match_on, unmapped, every member -
    # (`Enumeration.fingerprint()`), so a change to the LUMPING that leaves ids and names alone
    # still moves layers_hash (06 section 3 rule 1, 13 section 6). None for continuous layers.
    lumping: str | None = None

    def identity(self) -> dict[str, Any]:
        """What enters layers_hash: everything except how it aggregates (13 section 5)."""
        doc: dict[str, Any] = {
            "name": self.name, "kind": self.kind, "source": self.source, "dtype": self.dtype,
            "bands": list(self.bands) if self.bands is not None else None,
            "classes": self.classes.fingerprint() if self.classes is not None else None,
        }
        if self.lumping is not None:
            doc["lumping"] = self.lumping
        return doc


@dataclass(frozen=True)
class SnapshotSchema:
    """One declaration per run of what a snapshot holds (13). Extend, never redefine."""

    name: str
    layers: Sequence[LayerSpec]
    extends: Sequence[str] = ()              # ancestor schema names; hashes recorded beside them

    def __post_init__(self) -> None:
        names = [layer.name for layer in self.layers]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate layer names in schema {self.name!r}")
        for reserved in ("score", "valid"):
            if reserved in names:
                raise ValueError(f"{reserved!r} is added by the framework; do not declare it")

    def __getitem__(self, name: str) -> LayerSpec:
        for layer in self.layers:
            if layer.name == name:
                return layer
        raise KeyError(name)

    @property
    def layers_hash(self) -> str:
        return canonical_hash([layer.identity() for layer in self.layers])

    @property
    def aggregate_hash(self) -> str:
        return canonical_hash({layer.name: layer.aggregate.canonical() for layer in self.layers})


class SnapshotStack:
    """Reducer input. Epoch-major. Every epoch was written under `schema` (13 section 5)."""

    def __init__(self, *, schema: SnapshotSchema, epochs: Sequence[Epoch],
                 layers: Mapping[str, np.ndarray], valid: np.ndarray, score: np.ndarray,
                 observed: np.ndarray | None = None) -> None:
        self.schema = schema
        self.epochs = tuple(epochs)
        self._layers = dict(layers)
        self._valid = valid
        self._score = score
        self._observed = observed

    def __getitem__(self, layer: str) -> np.ndarray:
        return self._layers[layer]

    @property
    def valid(self) -> np.ndarray:
        return self._valid

    @property
    def score(self) -> np.ndarray:
        return self._score

    @property
    def observed(self) -> np.ndarray:
        """(n_epochs, H, W) bool: an observation reached the cell, before any pixel mask or the
        scorer. A stack built without it reports `valid` - the only cells it can vouch for."""
        return self._valid if self._observed is None else self._observed


class BandStack:
    """OutputMapper input. Finished bands for one tile."""

    def __init__(self, bands: Mapping[str, np.ndarray], specs: Sequence[BandSpec]) -> None:
        self._bands = dict(bands)
        self.specs = tuple(specs)

    def __getitem__(self, band: str) -> np.ndarray:
        return self._bands[band]


# ---------------------------------------------------------------------------------- 9. class tables
@dataclass(frozen=True)
class ClassTable:
    """A band's classes: a key column and attribute columns. The core never knows what they mean."""

    key: str
    entries: pa.Table
    source: str

    def attrs(self) -> Sequence[str]:
        return [c for c in self.entries.column_names if c != self.key]

    def fingerprint(self) -> str:
        cols = {c: self.entries.column(c).to_pylist() for c in sorted(self.entries.column_names)}
        return canonical_hash({"key": self.key, "columns": cols})


# ---------------------------------------------------------------------------------- 10. data access
@dataclass(frozen=True)
class SensorWindow:
    """A rectangle in a granule's own sensor array. Carries its ORIGIN, not just a shape."""

    row0: int
    col0: int
    height: int
    width: int

    @classmethod
    def covering(cls, glt_x: np.ndarray, glt_y: np.ndarray) -> SensorWindow:
        """Bounding box of the sensor pixels a GLT window touches. Indices are 1-based;
        0 is nodata and negatives are interpolated (03 section 2)."""
        x = np.abs(glt_x[glt_x != 0])
        y = np.abs(glt_y[glt_y != 0])
        if x.size == 0:
            return cls(0, 0, 0, 0)
        r0, r1 = int(y.min()) - 1, int(y.max())
        c0, c1 = int(x.min()) - 1, int(x.max())
        return cls(r0, c0, r1 - r0, c1 - c0)

    @property
    def slices(self) -> tuple[slice, slice]:
        return (slice(self.row0, self.row0 + self.height),
                slice(self.col0, self.col0 + self.width))

    @property
    def empty(self) -> bool:
        return self.height == 0 or self.width == 0


@dataclass(frozen=True)
class VarSpec:
    name: str
    dtype: str
    shape: tuple[int, ...]
    fill: float | int | None
    units: str = "unitless"
    band_attrs: Mapping[str, Sequence[Any]] = field(default_factory=dict)  # never interpreted


@dataclass(frozen=True)
class LocArray:
    """The KD-tree input: lat/lon/elev in sensor space, fills already masked."""

    lat: np.ndarray
    lon: np.ndarray
    elev: np.ndarray


@dataclass(frozen=True)
class GranuleRecord:
    """One catalogue answer, before it becomes an index row. Source-shaped, not schema-shaped."""

    native_id: str
    collection: str
    datetime: datetime
    end_datetime: datetime
    geometry: BaseGeometry
    attributes: Mapping[str, Any]
    raw: Any


class AssetHandle(Protocol):
    """An openable asset. A reader never learns which mode produced it."""

    uri: str
    etag: str | None

    def path(self) -> Path | None: ...
    def vsi(self) -> str: ...


class ReaderContext(Protocol):
    asset: AssetHandle


@dataclass(frozen=True)
class Credentials:
    kind: Literal["aws", "bearer", "netrc", "none"]
    expires: datetime | None

    def as_env(self) -> Mapping[str, str]:
        return {}


class GranuleReader(Protocol):
    """Access-tier hook (12 section 3). Where -9999 dies; the reader owns band meaning."""

    collections: tuple[str, ...]
    space: Literal["sensor", "ortho"]

    def open(self, asset: AssetHandle) -> ReaderContext: ...
    def variables(self, ctx: ReaderContext) -> Mapping[str, VarSpec]: ...
    def read(self, ctx: ReaderContext, var: str,
             window: SensorWindow | None = None) -> np.ma.MaskedArray: ...
    def geolocation(self, ctx: ReaderContext) -> LocArray | None: ...
    def glt(self, ctx: ReaderContext) -> GLT | EmbeddedGLT | None: ...
    def class_table(self, ctx: ReaderContext, path: str, key: str,
                    attributes: Sequence[str]) -> ClassTable | None: ...


class GranuleSource(Protocol):
    """Access-tier hook (12 section 5). Runs at index build, never in a run."""

    name: str

    def search(self, *, collections: Sequence[str], bbox: tuple[float, float, float, float],
               start: datetime, end: datetime,
               updated_since: datetime | None = None) -> Any: ...
    def assets(self, record: GranuleRecord) -> Mapping[str, str]: ...
