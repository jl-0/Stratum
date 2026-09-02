"""Tiny in-memory "granules" for resolve tests, and the wiring to run resolve over them.

Nothing here touches NetCDF: a `FakeReader` and a `FakeStore` implement the 12 section 3/4
protocols over arrays, sensor pixels are placed exactly on tile cell centres so the KD-tree
regrid is deterministic, and `build_plan` produces a `PlanContext` with real GLTs in a real
`CacheRoot`. Wave 3 reuses this for `tests/test_invariants.py` (seam equivalence, cache-key
sensitivity).

The synthetic sensor variables of every observation:

    class_id  int16   (rows, cols)     fill -9999; row 0 fill, row 1 all 0 ("nothing identified")
    depth     float32 (rows, cols)     fill -9999.0; row 0 fill
    geom      float32 (rows, cols, 2)  bands ["view_zenith", "solar_zenith"]; row 0 fill

and the enumeration `syn`: raw record 7 -> product 1 "alpha", raw record 5 -> product 2 "beta";
raw 9 is described by no class (unmapped). The layer `mineral` is categorical over class_id,
`depth` continuous over depth; the alias `view_zenith` is band 0 of `geom`.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa

from stratum.cache import CacheKey, CacheRoot
from stratum.classes import ClassDef, Enumeration, Member, Remap
from stratum.regrid import REGRID_ALGO_VERSION, regrid_granule_tile, resolve_max_distance
from stratum.resolve import (
    AliasBinding,
    PlanContext,
    PluginBinding,
    RoleBinding,
    plugin_version,
    read_snapshot,
    resolve_block,
)
from stratum.types import (
    Aggregation,
    BlockRef,
    ClassTable,
    Epoch,
    GranuleRef,
    GridDef,
    LayerSpec,
    LocArray,
    ObsWindow,
    SensorWindow,
    SnapshotSchema,
    TileRef,
    VarSpec,
)

# tile_size 0.02 at 0.001 -> a 20 x 20 tile; block 10 -> four 10 x 10 blocks
GRID = GridDef("EPSG:4326", (0.001, -0.001), (0.0, 0.0), 0.02, block=10)
TILE = TileRef(GRID, 0, 0)
EPOCH = Epoch(datetime(2026, 6, 1, tzinfo=UTC), datetime(2026, 7, 1, tzinfo=UTC))
COLLECTION = "FAKE"
ASSET = "DATA"
FILL_INT = -9999
FILL_FLOAT = -9999.0
GEOM_BANDS = ["view_zenith", "solar_zenith"]


# ------------------------------------------------------------------------------ enumeration
ENUMERATION = Enumeration(
    name="syn", version="1", match_on=("record",), unmapped="fail",
    classes=(ClassDef(1, "alpha", (Member({"record": 7}),)),
             ClassDef(2, "beta", (Member({"record": 5}),))))

RAW_TABLE = ClassTable(key="index", entries=pa.table({"index": [5, 7], "record": [5, 7]}),
                       source="synthetic:raw")


def remap_for(raw: ClassTable = RAW_TABLE) -> Remap:
    return ENUMERATION.resolve(raw)


def schema() -> SnapshotSchema:
    return SnapshotSchema(name="syn", layers=[
        LayerSpec(name="mineral", kind="categorical", source="mineral",
                  aggregate=Aggregation("vote"), dtype="uint16",
                  classes=ENUMERATION.class_table()),
        LayerSpec(name="depth", kind="continuous", source="depth",
                  aggregate=Aggregation("mean"), dtype="float32"),
    ])


# ---------------------------------------------------------------------------- fake granules
@dataclass
class FakeGranuleData:
    """One synthetic granule: its sensor arrays and where they sit."""

    lat: np.ndarray
    lon: np.ndarray
    variables: dict[str, np.ndarray]
    fills: dict[str, float | int]
    band_names: dict[str, list[str]] = field(default_factory=dict)
    class_table: ClassTable | None = None

    @property
    def loc(self) -> LocArray:
        return LocArray(lat=self.lat, lon=self.lon, elev=np.zeros_like(self.lat))


@dataclass(frozen=True)
class FakeAsset:
    uri: str
    etag: str | None = None

    def path(self) -> Path | None:
        return None

    def vsi(self) -> str:
        return self.uri


class FakeStore:
    """`AssetStore.open` over a set of known URIs; a reader never learns there is no file."""

    def __init__(self, uris: Sequence[str]) -> None:
        self.uris = set(uris)
        self.opened: list[str] = []

    def open(self, uri: str, *, etag: str | None = None) -> FakeAsset:
        if uri not in self.uris:
            raise FileNotFoundError(uri)
        self.opened.append(uri)
        return FakeAsset(uri=uri, etag=etag)


@dataclass
class FakeContext:
    asset: FakeAsset
    data: FakeGranuleData
    closed: bool = False

    def close(self) -> None:
        self.closed = True


class FakeReader:
    """A 12 section 3 GranuleReader over in-memory arrays. -9999 dies here, as it must."""

    collections = (COLLECTION,)
    space = "sensor"

    def __init__(self, data: Mapping[str, FakeGranuleData]) -> None:
        self.data = dict(data)
        self.reads: list[tuple[str, str, SensorWindow | None]] = []

    def open(self, asset: FakeAsset) -> FakeContext:
        return FakeContext(asset=asset, data=self.data[asset.uri])

    def variables(self, ctx: FakeContext) -> dict[str, VarSpec]:
        out = {}
        for name, arr in ctx.data.variables.items():
            attrs = {"name": ctx.data.band_names[name]} if name in ctx.data.band_names else {}
            out[name] = VarSpec(name=name, dtype=arr.dtype.name, shape=tuple(arr.shape),
                                fill=ctx.data.fills.get(name), band_attrs=attrs)
        return out

    def read(self, ctx: FakeContext, var: str,
             window: SensorWindow | None = None) -> np.ma.MaskedArray:
        self.reads.append((ctx.asset.uri, var, window))
        arr = ctx.data.variables[var]
        data = arr if window is None else arr[window.slices]
        data = np.array(data, copy=True)
        fill = ctx.data.fills.get(var)
        mask = np.zeros(data.shape, dtype=bool) if fill is None else data == fill
        return np.ma.MaskedArray(data, mask=mask, fill_value=fill)

    def geolocation(self, ctx: FakeContext) -> LocArray:
        return ctx.data.loc

    def glt(self, ctx: FakeContext) -> None:
        return None

    def class_table(self, ctx: FakeContext, path: str, key: str,
                    attributes: Sequence[str]) -> ClassTable | None:
        return ctx.data.class_table


# ------------------------------------------------------------------------- placing pixels
def cell_centre(tile: TileRef, r: int, c: int) -> tuple[float, float]:
    """(lat, lon) of tile cell (r, c), row 0 at the northern edge."""
    x0, _, _, y1 = tile.bounds
    rx, ry = tile.grid.resolution
    return y1 + (r + 0.5) * ry, x0 + (c + 0.5) * rx


def sensor_loc(tile: TileRef, r0: int, c0: int, rows: int, cols: int,
               step: int = 1) -> tuple[np.ndarray, np.ndarray]:
    """lat/lon for a rows x cols sensor whose pixel (i, j) sits on cell (r0 + i*step,
    c0 + j*step); the GLT there is then exactly (j + 1, i + 1, 1)."""
    lat = np.zeros((rows, cols))
    lon = np.zeros((rows, cols))
    for i in range(rows):
        for j in range(cols):
            lat[i, j], lon[i, j] = cell_centre(tile, r0 + i * step, c0 + j * step)
    return lat, lon


@dataclass(frozen=True)
class SyntheticObservation:
    """A GranuleRef plus the arrays behind its one asset."""

    ref: GranuleRef
    data: FakeGranuleData
    r0: int
    c0: int

    @property
    def uri(self) -> str:
        return self.ref.assets[f"{COLLECTION}/{ASSET}"]


def make_observation(granule_id: str, when: datetime, *, r0: int, c0: int, rows: int,
                     cols: int, view_zenith: np.ndarray | float, tile: TileRef = TILE,
                     class_ids: np.ndarray | None = None, step: int = 1,
                     fill_row: bool = True) -> SyntheticObservation:
    """One synthetic granule over cells [r0, r0+rows*step) x [c0, c0+cols*step) of `tile`.

    class_id defaults to raw 7 everywhere except column 2 (raw 5) and cell (3, 3) (raw 9,
    unmapped); row 0 is fill and row 1 is 0 when `fill_row`. depth is 0.01 * (i*cols + j)
    with row 0 fill; view zenith is `view_zenith` broadcast, with row 0 fill."""
    lat, lon = sensor_loc(tile, r0, c0, rows, cols, step)
    ii, jj = np.meshgrid(np.arange(rows), np.arange(cols), indexing="ij")
    if class_ids is None:
        class_ids = np.full((rows, cols), 7, dtype=np.int16)
        class_ids[:, 2] = 5
        if rows > 3 and cols > 3:
            class_ids[3, 3] = 9
    class_ids = np.array(class_ids, dtype=np.int16)
    depth = (0.01 * (ii * cols + jj)).astype(np.float32)
    geom = np.zeros((rows, cols, 2), dtype=np.float32)
    geom[..., 0] = np.broadcast_to(np.asarray(view_zenith, dtype=np.float32), (rows, cols))
    geom[..., 1] = 40.0
    if fill_row:
        class_ids[0, :] = FILL_INT
        class_ids[1, :] = 0
        depth[0, :] = FILL_FLOAT
        geom[0, :, :] = FILL_FLOAT
    data = FakeGranuleData(
        lat=lat, lon=lon,
        variables={"class_id": class_ids, "depth": depth, "geom": geom},
        fills={"class_id": FILL_INT, "depth": FILL_FLOAT, "geom": FILL_FLOAT},
        band_names={"geom": list(GEOM_BANDS)}, class_table=RAW_TABLE)
    uri = f"fake://{granule_id}/{ASSET}"
    ref = GranuleRef(
        granule_id=granule_id, collection=COLLECTION, datetime=when, end_datetime=when,
        bbox=(float(lon.min()), float(lat.min()), float(lon.max()), float(lat.max())),
        assets={f"{COLLECTION}/{ASSET}": uri}, build_version="000001", product_version="V001",
        collection_version="001", cloud_fraction=None)
    return SyntheticObservation(ref=ref, data=data, r0=r0, c0=c0)


def two_overlapping(tile: TileRef = TILE) -> list[SyntheticObservation]:
    """The standard pair: A over cells [2, 16) x [2, 16) looking steeper to the east, B over
    [6, 20) x [6, 20) looking steeper to the west; they overlap on [6, 16) x [6, 16)."""
    cols = np.arange(14)[None, :]
    a = make_observation("A", datetime(2026, 6, 5, tzinfo=UTC), r0=2, c0=2, rows=14, cols=14,
                         view_zenith=10.0 + cols, tile=tile)
    b = make_observation("B", datetime(2026, 6, 10, tzinfo=UTC), r0=6, c0=6, rows=14, cols=14,
                         view_zenith=10.0 + (13 - cols), tile=tile)
    return [a, b]


# ---------------------------------------------------------------------------------- scorers
class NadirScorer:
    """MinViewZenith without the EMIT package: the most nadir look wins."""

    capability = "streaming"
    halo = 0
    required_roles = ("geometry",)
    required_aux: tuple[str, ...] = ()

    def __init__(self, weight: float = 1.0) -> None:
        self.weight = weight

    def score(self, obs: ObsWindow, aux: Any) -> np.ndarray:
        return -self.weight * obs["view_zenith"]


class NeighbourScorer(NadirScorer):
    """A 3x3 stencil over `obs.valid` (halo 1): exercises the halo path of 01 section 4."""

    halo = 1

    def score(self, obs: ObsWindow, aux: Any) -> np.ndarray:
        v = np.pad(obs.valid.astype(np.float32), 1)
        neighbours = sum(v[1 + dr:v.shape[0] - 1 + dr, 1 + dc:v.shape[1] - 1 + dc]
                         for dr in (-1, 0, 1) for dc in (-1, 0, 1))
        return -obs["view_zenith"] + 0.001 * neighbours


class ShallowIsNan(NadirScorer):
    """NaN where depth < `floor`: that observation may not occupy the cell (04 section 4)."""

    def __init__(self, floor: float = 0.5) -> None:
        super().__init__()
        self.floor = floor

    def score(self, obs: ObsWindow, aux: Any) -> np.ndarray:
        s = -obs["view_zenith"].astype(np.float32)
        s[obs["depth"] < self.floor] = np.nan
        return s


# ------------------------------------------------------------------------------------- plan
def binding(instance: Any, ref: str | None = None, params: Mapping[str, Any] | None = None
            ) -> PluginBinding:
    ref = ref or f"{type(instance).__module__}:{type(instance).__name__}"
    return PluginBinding(ref=ref, version=plugin_version(instance), params=dict(params or {}),
                         instance=instance)


def build_plan(root: Path, observations: Sequence[SyntheticObservation], *, scorer: Any,
               masks: Sequence[Any] = (), tile: TileRef = TILE,
               max_distance: float | None = None,
               snapshot_schema: SnapshotSchema | None = None) -> PlanContext:
    """A PlanContext over `observations` with their GLTs already built into `root`."""
    cache = CacheRoot(root)
    md = resolve_max_distance(tile.grid, max_distance)
    for o in observations:
        regrid_granule_tile(cache, tile, o.ref.granule_id, lambda o=o: o.data.loc,
                            max_distance=md)
    reader = FakeReader({o.uri: o.data for o in observations})
    store = FakeStore([o.uri for o in observations])
    sch = snapshot_schema or schema()
    remap = remap_for()
    return PlanContext(
        grid=tile.grid, cache=cache, store=store,
        granules={o.ref.granule_id: o.ref for o in observations},
        roles={"mineral": RoleBinding(COLLECTION, "class_id", ASSET),
               "depth": RoleBinding(COLLECTION, "depth", ASSET),
               "geometry": RoleBinding(COLLECTION, "geom", ASSET)},
        aliases={"view_zenith": AliasBinding("geometry", 0)},
        geolocation_role="mineral", schema=sch,
        scorer=binding(scorer, params=dict(vars(scorer))),
        masks=[binding(m, params=dict(vars(m))) for m in masks],
        remaps={layer.name: {o.ref.granule_id: remap for o in observations}
                for layer in sch.layers if layer.kind == "categorical"},
        max_distance=md, regrid_method="kdtree", regrid_algo_version=REGRID_ALGO_VERSION,
        reader_lookup=lambda collection: reader)


def with_block(plan: PlanContext, block: int) -> PlanContext:
    """The same plan on the same lattice cut into `block`-sized blocks. `GridDef.block` is not
    in `grid.id` or in any key (01 section 3 invariant), so GLTs and snapshots are shared."""
    return replace(plan, grid=replace(plan.grid, block=block))


def item(tile: TileRef, epoch: Epoch, bx: int, by: int) -> dict[str, Any]:
    return {"tile": [tile.tx, tile.ty], "epoch": list(epoch.bounds), "block": [bx, by]}


def resolve_tile(plan: PlanContext, epoch: Epoch = EPOCH, tile: TileRef | None = None,
                 run: Callable[[dict[str, Any], PlanContext], CacheKey] = resolve_block,
                 ) -> dict[str, np.ndarray]:
    """Resolve every block of `tile` under `plan` and assemble the snapshots into full-tile
    arrays ({layer, score, valid}). This is what seam equivalence compares."""
    tile = tile or TileRef(plan.grid, 0, 0)
    out: dict[str, np.ndarray] = {}
    for block in tile.blocks():
        key = run(item(tile, epoch, block.bx, block.by), plan)
        snap = read_snapshot(key.path)
        core = block.core_window
        for name, arr in snap.items():
            if name not in out:
                out[name] = np.full(tile.shape + arr.shape[2:], _nodata_like(arr), dtype=arr.dtype)
            out[name][core.slices] = arr
    return out


def _nodata_like(arr: np.ndarray) -> Any:
    if arr.dtype.kind == "f":
        return np.nan
    if arr.dtype.kind == "b":
        return False
    return np.iinfo(arr.dtype).max


def block_of(plan: PlanContext, bx: int, by: int, halo: int = 0,
             tile: TileRef | None = None) -> BlockRef:
    return BlockRef(tile or TileRef(plan.grid, 0, 0), bx, by, halo=halo)


__all__ = [
    "ASSET", "COLLECTION", "ENUMERATION", "EPOCH", "FILL_FLOAT", "FILL_INT", "GEOM_BANDS", "GRID",
    "RAW_TABLE", "TILE", "FakeAsset", "FakeContext", "FakeGranuleData", "FakeReader", "FakeStore",
    "NadirScorer", "NeighbourScorer", "ShallowIsNan", "SyntheticObservation", "binding",
    "block_of", "build_plan", "cell_centre", "item", "make_observation", "remap_for",
    "resolve_tile", "schema", "sensor_loc", "two_overlapping", "with_block",
]
