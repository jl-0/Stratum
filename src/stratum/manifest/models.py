"""Pydantic models for the run manifest (09). `extra="forbid"` everywhere, so a misspelt key is
an error with a location rather than a silently ignored setting.

The split between here and `validate.py`: a model checks the shape of its own block - types,
enumerated values, which fields go together. Anything that references *another* block (a layer's
`source` naming a role, a render naming a layer, a plugin's `required_roles`) is a problem
reported by `validate_static`, so the planner and `stratum validate` get a list rather than the
first failure.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    PositiveFloat,
    PositiveInt,
    PrivateAttr,
    model_validator,
)

from stratum.classes import Enumeration, load_enumeration
from stratum.time import (
    Align,
    DeliveryPeriod,
    Duration,
    EpochAlign,
    as_utc,
    check_delivery,
    delivery_periods,
    epochs_between,
    parse_duration,
)
from stratum.types import (
    Aggregation,
    ClassTable,
    Epoch,
    GridDef,
    LayerSpec,
    SnapshotSchema,
    TileRef,
)

REF_PREFIX = "@ref:"
SCHEMA_VERSION = "1.0"

CATEGORICAL_METHODS = {"vote", "best", "none"}
CONTINUOUS_METHODS = {"median", "mean", "min", "max", "percentile", "score_weighted",
                      "inverse_variance", "best", "none"}
CONFIG_MAPPERS = {"categorical", "continuous"}
LATER_MAPPERS = {"threshold", "composite"}
BUILTIN_FILTERS = ("max_cloud_fraction", "max_solar_zenith", "month_in", "build_version",
                   "product_version", "collection_version", "day_night")
MISSABLE_FILTERS = {"max_cloud_fraction", "max_solar_zenith", "build_version", "product_version",
                    "collection_version", "day_night", "ref"}


def _to_utc(v: Any) -> datetime:
    if isinstance(v, str):
        v = datetime.fromisoformat(v)
    if isinstance(v, (datetime, date)):
        return as_utc(v)
    raise ValueError(f"expected a date or datetime, got {v!r}")


DurationField = Annotated[Duration, BeforeValidator(parse_duration),
                          PlainSerializer(str, return_type=str)]
UtcDatetime = Annotated[datetime, BeforeValidator(_to_utc),
                        PlainSerializer(lambda d: d.isoformat(), return_type=str)]
Bbox = tuple[float, float, float, float]
RGB = tuple[int, int, int]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------------------------------ grid
class GridSpec(Strict):
    """01 section 1. `max_distance` and `force_positive_y` exist only to defeat a default."""

    crs: str
    resolution: tuple[float, float]
    origin: tuple[float, float]
    tile_size: PositiveFloat
    block: PositiveInt = 512
    max_distance: PositiveFloat | None = None
    regrid_method: Literal["kdtree", "warp_embedded"] = "kdtree"
    force_positive_y: bool = False

    @model_validator(mode="after")
    def _guard_rails(self) -> GridSpec:
        rx, ry = self.resolution
        if rx <= 0:
            raise ValueError("resolution x must be positive")
        if ry >= 0 and not self.force_positive_y:
            raise ValueError("resolution y must be negative (north-up); set force_positive_y "
                             "to defeat this guard rail (01 section 1)")
        if self.crs.upper().startswith("EPSG:4") and rx > 1:
            raise ValueError("resolution > 1 with a geographic EPSG:4xxx CRS is almost certainly "
                             "metres where degrees were meant (01 section 1)")
        return self

    def grid_def(self) -> GridDef:
        if self.resolution[1] >= 0:
            raise NotImplementedError("force_positive_y: GridDef does not accept a positive y "
                                      "resolution yet (01 section 1)")
        return GridDef(crs=self.crs, resolution=self.resolution, origin=self.origin,
                       tile_size=self.tile_size, block=self.block)


# ------------------------------------------------------------------------------------------- aoi
class AoiSpec(Strict):
    """Exactly one of `zones` (names in `registry`), `bbox` [w, s, e, n] or `tiles` [[tx, ty]]
    (09 section 2)."""

    zones: list[str] | None = None
    bbox: Bbox | None = None
    tiles: list[tuple[int, int]] | None = None
    registry: str | None = None

    @model_validator(mode="after")
    def _one_of(self) -> AoiSpec:
        given = [k for k in ("zones", "bbox", "tiles") if getattr(self, k) is not None]
        if len(given) != 1:
            raise ValueError(f"aoi needs exactly one of zones, bbox or tiles; got {given or 'none'}")
        if self.zones is not None and not self.zones:
            raise ValueError("aoi.zones is empty")
        if self.tiles is not None and not self.tiles:
            raise ValueError("aoi.tiles is empty")
        if self.bbox is not None:
            w, s, e, n = self.bbox
            if not (w < e and s < n):
                raise ValueError(f"aoi.bbox must be [w, s, e, n] with w < e and s < n; got {self.bbox}")
        return self


# ------------------------------------------------------------------------------------------ time
class DeliverSpec(Strict):
    """`{every, window, align}`; `window` defaults to `every` (11 section 4)."""

    every: DurationField
    window: DurationField | None = None
    align: Align = "exact"

    @model_validator(mode="after")
    def _default_window(self) -> DeliverSpec:
        if self.window is None:
            self.window = self.every
        return self


def _deliver_shorthand(v: Any) -> Any:
    return {"every": v} if isinstance(v, str) else v


class TimeSpec(Strict):
    """[start, end) UTC, epochs of `epoch` from `start`; `deliver` shorthand `P1Y` means
    `{every: P1Y, window: P1Y, align: exact}` and defaults to the epoch itself (09 section 2)."""

    start: UtcDatetime
    end: UtcDatetime
    epoch: DurationField
    deliver: Annotated[DeliverSpec | None, BeforeValidator(_deliver_shorthand)] = None
    align: EpochAlign = "start"

    @model_validator(mode="after")
    def _rules(self) -> TimeSpec:
        if self.end <= self.start:
            raise ValueError(f"end {self.end.isoformat()} is not after start {self.start.isoformat()}")
        if self.deliver is None:
            self.deliver = DeliverSpec(every=self.epoch)
        assert self.deliver.window is not None
        check_delivery(self.epoch, self.deliver.every, self.deliver.window, self.deliver.align)
        return self


# ---------------------------------------------------------------------------------------- inputs
class ClassTableSpec(Strict):
    """Where a categorical role keeps its own class table (11 section 9)."""

    source: Literal["embedded", "file"]
    path: str
    key: str
    attributes: list[str]


class RoleSpec(Strict):
    """A role resolves to (asset URI, variable) through the index (02 section 5)."""

    collection: str
    var: str
    asset: str | None = None
    version: str | None = None
    class_table: ClassTableSpec | None = None


class BandAliasSpec(Strict):
    """One band of a role, by index or by a reader-reported attribute (11 section 5)."""

    role: str
    band: int | None = None
    match: dict[str, Any] | None = None
    tolerance: float | None = None

    @model_validator(mode="after")
    def _one_of(self) -> BandAliasSpec:
        if (self.band is None) == (self.match is None):
            raise ValueError("a band alias needs exactly one of band or match")
        if self.tolerance is not None and self.match is None:
            raise ValueError("tolerance only applies to match")
        return self


class SourceSpec(Strict):
    """How the index is built - read by `stratum index build`, ignored by a run (12 section 6).
    A local run needs no catalogue: `{kind: local, root, patterns}` (12 section 5)."""

    kind: Literal["cmr", "stac", "local", "parquet"]
    provider: str | None = None
    prefer: Literal["direct", "https"] | None = None
    root: str | None = None
    pattern: str | None = None           # one glob; the roles must then read ONE collection
    # {collection: {asset: glob}}, or the long form {collection: {version: "001",
    # assets: {asset: glob}}} when the collection version must be pinned (12 section 5).
    patterns: dict[str, dict[str, str | dict[str, str]]] | None = None

    @model_validator(mode="after")
    def _by_kind(self) -> SourceSpec:
        if self.kind == "local" and self.root is None:
            raise ValueError("source.root is required for kind: local")
        if self.kind == "local" and not (self.pattern or self.patterns):
            raise ValueError("kind: local needs pattern or patterns: {collection: {asset: glob}} "
                             "(12 section 5)")
        if self.pattern and self.patterns:
            raise ValueError("give pattern or patterns, not both")
        if self.kind != "local" and (self.root or self.pattern or self.patterns):
            raise ValueError("root, pattern and patterns apply to kind: local only")
        return self


class InputsSpec(Strict):
    index: str | None = None
    source: SourceSpec | None = None
    readers: dict[str, str] = Field(default_factory=dict)
    roles: dict[str, RoleSpec]
    band_aliases: dict[str, BandAliasSpec] = Field(default_factory=dict)
    geolocation: str | None = None

    @model_validator(mode="after")
    def _shape(self) -> InputsSpec:
        if not self.roles:
            raise ValueError("inputs.roles is empty")
        if self.index is None and self.source is None:
            raise ValueError("inputs needs an index to read or a source to build one from")
        return self

    @property
    def names(self) -> set[str]:
        """The one namespace `source:` and `obs[...]` resolve through (13 section 2)."""
        return set(self.roles) | set(self.band_aliases)


# ------------------------------------------------------------------------------------------- aux
class AuxSpec(Strict):
    """05 section 2: `kind` and `resampling` are declared, never defaulted, and must agree."""

    uri: str
    kind: Literal["continuous", "categorical", "vector", "table"]
    resampling: Literal["nearest", "bilinear", "cubic", "mode", "average"] | None = None
    temporal: Literal["nearest", "previous", "epoch"] | None = None
    max_age: DurationField | None = None
    burn: str | None = None
    all_touched: bool | None = None

    @model_validator(mode="after")
    def _agree(self) -> AuxSpec:
        raster = self.kind in ("continuous", "categorical")
        if raster and self.resampling is None:
            raise ValueError(f"a {self.kind} raster needs resampling (05 section 2)")
        if not raster and self.resampling is not None:
            raise ValueError(f"resampling does not apply to kind: {self.kind}")
        if self.kind == "categorical" and self.resampling not in ("nearest", "mode"):
            raise ValueError(f"categorical with {self.resampling} interpolates class labels; "
                             "use nearest or mode (05 section 2)")
        if self.kind != "vector" and (self.burn is not None or self.all_touched is not None):
            raise ValueError("burn and all_touched apply to kind: vector only")
        if self.max_age is not None and self.temporal not in ("nearest", "previous"):
            raise ValueError("max_age applies to temporal: nearest | previous")
        if self.temporal is not None and "{date}" not in self.uri:
            raise ValueError("temporal needs a {date} placeholder in uri")
        return self


# -------------------------------------------------------------------------------------- filters
class GranuleFilterSpec(Strict):
    """One built-in predicate, or `{ref, params}`; `on_missing` per entry (04 section 2).
    The policy defaults to `fail`, which is the explicit default 02 section 4 requires."""

    max_cloud_fraction: float | None = None
    max_solar_zenith: float | None = None
    month_in: list[int] | None = None
    build_version: str | list[str] | None = None
    product_version: str | list[str] | None = None
    collection_version: str | list[str] | None = None
    day_night: str | None = None
    ref: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    on_missing: Literal["reject", "keep", "fail"] | None = None

    @model_validator(mode="after")
    def _one_predicate(self) -> GranuleFilterSpec:
        given = [k for k in (*BUILTIN_FILTERS, "ref") if getattr(self, k) is not None]
        if len(given) != 1:
            raise ValueError(f"a granule_filter entry is exactly one predicate; got {given}")
        if self.params and self.ref is None:
            raise ValueError("params only applies to ref")
        if self.month_in is not None:
            bad = [m for m in self.month_in if not 1 <= m <= 12]
            if bad or not self.month_in:
                raise ValueError(f"month_in must be month numbers 1..12; got {self.month_in}")
        if self.on_missing is not None and given[0] not in MISSABLE_FILTERS:
            raise ValueError(f"on_missing does not apply to {given[0]}: nothing can be missing")
        return self

    @property
    def predicate(self) -> str:
        return next(k for k in (*BUILTIN_FILTERS, "ref") if getattr(self, k) is not None)

    @property
    def policy(self) -> str:
        return self.on_missing or "fail"


class PixelMaskSpec(BaseModel):
    """`{ref, **params}` - every other key goes to the plugin constructor (09 section 2)."""

    model_config = ConfigDict(extra="allow")
    ref: str

    @property
    def params(self) -> dict[str, Any]:
        return dict(self.model_extra or {})


class PluginRef(Strict):
    ref: str
    params: dict[str, Any] = Field(default_factory=dict)


# -------------------------------------------------------------------------------------- snapshot
class AggregateSpec(Strict):
    """13 section 4 vocabulary. Which parameters go with which method is checked on the layer,
    where the kind is known."""

    method: str
    min_count: PositiveInt | None = None
    ignore: list[str] | None = None
    tie_break: Literal["earliest", "latest", "highest_score", "nodata"] | None = None
    p: float | None = None
    unc: str | None = None
    conditional_on: str | None = None
    spread: Literal["std", "iqr"] | None = None

    def params(self) -> dict[str, Any]:
        return {k: v for k, v in self.model_dump(exclude={"method"}).items() if v is not None}

    def to_aggregation(self) -> Aggregation:
        return Aggregation(self.method, self.params())


class LayerModel(Strict):
    kind: Literal["categorical", "continuous"]
    source: str
    dtype: str | None = None
    bands: list[int] | None = None
    classes: str | None = None
    aggregate: AggregateSpec

    @model_validator(mode="after")
    def _by_kind(self) -> LayerModel:
        a, m = self.aggregate, self.aggregate.method
        given = set(a.params())
        if self.kind == "categorical":
            if m not in CATEGORICAL_METHODS:
                raise ValueError(f"categorical aggregate {m!r}; choose {sorted(CATEGORICAL_METHODS)}")
            if self.classes is None:
                raise ValueError("a categorical layer needs classes: '@ref:<file>' or 'source' "
                                 "(13 section 3)")
            if self.classes != "source" and not self.classes.startswith(REF_PREFIX):
                raise ValueError(f"classes must be 'source' or '{REF_PREFIX}<path>'; "
                                 f"got {self.classes!r}")
            if self.bands is not None:
                raise ValueError("categorical layers are single-band (13 section 2)")
            allowed = {"min_count", "ignore", "tie_break"} if m == "vote" else set()
        else:
            if m not in CONTINUOUS_METHODS:
                raise ValueError(f"continuous aggregate {m!r}; choose {sorted(CONTINUOUS_METHODS)}")
            if self.classes is not None:
                raise ValueError("classes applies to categorical layers only")
            allowed = set() if m == "none" else {"conditional_on", "spread"}
            if m == "percentile":
                allowed.add("p")
                if a.p is None or not 0 <= a.p <= 100:
                    raise ValueError("percentile needs p in [0, 100]")
            if m == "inverse_variance":
                allowed.add("unc")
                if a.unc is None:
                    raise ValueError("inverse_variance needs unc: <layer>")
        stray = given - allowed
        if stray:
            raise ValueError(f"{sorted(stray)} do not apply to {self.kind} aggregate {m!r}")
        return self

    @property
    def classes_ref(self) -> str | None:
        """The path after `@ref:`, or None for `source` / continuous."""
        if self.classes and self.classes.startswith(REF_PREFIX):
            return self.classes[len(REF_PREFIX):]
        return None


class SnapshotSpec(Strict):
    name: str
    extends: list[str] = Field(default_factory=list)
    layers: dict[str, LayerModel]

    @model_validator(mode="after")
    def _names(self) -> SnapshotSpec:
        if not self.layers:
            raise ValueError("snapshot.layers is empty")
        for reserved in ("score", "valid"):
            if reserved in self.layers:
                raise ValueError(f"{reserved!r} is added by the framework; do not declare it "
                                 "(13 section 2)")
        return self


# --------------------------------------------------------------------------------------- outputs
class AlphaFromSpec(Strict):
    band: str
    domain: tuple[float, float]
    range: tuple[int, int] = (0, 255)

    @model_validator(mode="after")
    def _ranges(self) -> AlphaFromSpec:
        if self.domain[0] >= self.domain[1]:
            raise ValueError("alpha_from.domain must be [lo, hi] with lo < hi")
        if not (0 <= self.range[0] <= 255 and 0 <= self.range[1] <= 255):
            raise ValueError("alpha_from.range values are 0..255")
        return self


class RenderSpec(Strict):
    """07 section 3. `categorical` takes the layer's enumeration as its legend and an optional
    colour table by class name; `continuous` takes a ramp over a domain. `threshold` and
    `composite` are a later slice. Anything else is a mapper plugin ref."""

    mapper: str
    layer: str | None = None
    colors: dict[str, RGB] | None = None
    nodata: Literal["transparent"] | RGB = "transparent"
    on_unmapped: Literal["fail", "grey", "transparent"] = "fail"
    alpha_from: AlphaFromSpec | None = None
    ramp: str | None = None
    domain: tuple[float, float] | None = None
    clip: bool | None = None
    params: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _by_mapper(self) -> RenderSpec:
        if self.mapper in LATER_MAPPERS:
            raise NotImplementedError(f"mapper {self.mapper!r} is a later slice (07 section 3)")
        continuous_keys = [k for k in ("ramp", "domain", "clip") if getattr(self, k) is not None]
        if self.mapper == "categorical" and continuous_keys:
            raise ValueError(f"{continuous_keys} apply to the continuous mapper")
        if self.mapper == "continuous":
            if self.colors is not None:
                raise ValueError("colors applies to the categorical mapper")
            if self.ramp is None or self.domain is None:
                raise ValueError("the continuous mapper needs ramp and domain (07 section 3)")
            if self.domain[0] >= self.domain[1]:
                raise ValueError("domain must be [lo, hi] with lo < hi")
        if self.mapper in CONFIG_MAPPERS and self.params:
            raise ValueError("params applies to a mapper plugin ref")
        return self


class OutputsSpec(Strict):
    """`bucket` is a URI or, locally, the storage root (plan section 4)."""

    bucket: str
    formats: list[Literal["cog", "netcdf"]] = Field(default_factory=lambda: ["cog"])
    stac: bool = True
    render: dict[str, RenderSpec] = Field(default_factory=dict)


class PluginsSpec(Strict):
    wheel: str | None = None


class BudgetSpec(Strict):
    """09 section 4. Required, finite, and checked by the planner before compute exists."""

    max_tiles: PositiveInt
    max_granules: PositiveInt
    max_vcpu_hours: PositiveFloat
    # `warn`: the planner reports the exceedance and a run proceeds; `fail` refuses;
    # `require_approval` parks the run until `stratum approve` (08 section 3), which is a later
    # slice, so locally it refuses too.
    on_exceed: Literal["require_approval", "fail", "warn"] = "require_approval"

    @model_validator(mode="after")
    def _finite(self) -> BudgetSpec:
        if math.isinf(self.max_vcpu_hours):
            raise ValueError("budget.max_vcpu_hours must be finite")
        return self


# -------------------------------------------------------------------------------------- manifest
class Manifest(Strict):
    """The whole document (09 section 2). `run_id` as written is `run_label`; the derived
    `run_id` is `{run_label}-{manifest_hash[:8]}` (09 section 6)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_version: str = SCHEMA_VERSION
    run_label: str = Field(alias="run_id")
    description: str | None = None
    grid: GridSpec
    aoi: AoiSpec
    time: TimeSpec
    inputs: InputsSpec
    aux: dict[str, AuxSpec] = Field(default_factory=dict)
    allow_mixed_vintage: bool = False
    mixed_vintage_reason: str | None = None
    granule_filter: list[GranuleFilterSpec] = Field(default_factory=list)
    pixel_mask: list[PixelMaskSpec] = Field(default_factory=list)
    scorer: PluginRef
    snapshot: SnapshotSpec
    reducer: PluginRef | None = None
    outputs: OutputsSpec
    plugins: PluginsSpec | None = None
    budget: BudgetSpec

    _base_dir: Path = PrivateAttr(default_factory=Path.cwd)
    _enumerations: dict[str, Enumeration] | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def _top(self) -> Manifest:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"schema_version {self.schema_version!r} is not supported; "
                             f"this build reads {SCHEMA_VERSION!r}")
        if self.allow_mixed_vintage and not self.mixed_vintage_reason:
            raise ValueError("allow_mixed_vintage requires mixed_vintage_reason - a documented "
                             "reason (09 section 5)")
        if self.mixed_vintage_reason and not self.allow_mixed_vintage:
            raise ValueError("mixed_vintage_reason without allow_mixed_vintage")
        if not self.run_label or "/" in self.run_label:
            raise ValueError("run_id must be a non-empty name without '/' (it becomes a prefix)")
        return self

    # -- construction -----------------------------------------------------------------------
    @classmethod
    def from_document(cls, doc: Mapping[str, Any], base_dir: Path | str | None = None) -> Manifest:
        """Validate a parsed document; `base_dir` is what `@ref:` and `aoi.registry` resolve
        against (the manifest file's directory)."""
        m = cls.model_validate(doc)
        m._base_dir = Path(base_dir).resolve() if base_dir is not None else Path.cwd()
        return m

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    def resolve_ref(self, value: str) -> Path:
        """`@ref:relative/path` -> absolute path under the manifest's directory."""
        rel = value.removeprefix(REF_PREFIX)
        return (self._base_dir / rel).resolve()

    def document(self) -> dict[str, Any]:
        """The canonical JSON-ready document: keys as written, durations and datetimes as
        strings, `deliver` in long form. What `manifest_hash` hashes."""
        return self.model_dump(mode="json", by_alias=True)

    @property
    def run_id(self) -> str:
        from stratum.manifest import manifest_hash  # avoids a cycle at import time
        return f"{self.run_label}-{manifest_hash(self)[7:15]}"

    # -- grid and area ----------------------------------------------------------------------
    def grid_def(self) -> GridDef:
        return self.grid.grid_def()

    def zone_registry(self) -> dict[str, Bbox]:
        """`aoi.registry`: a YAML mapping of zone name -> [w, s, e, n], relative to the manifest."""
        if self.aoi.registry is None:
            raise ValueError("aoi.zones needs aoi.registry, a YAML of zone name -> [w, s, e, n] "
                             "(09 section 2)")
        path = self.resolve_ref(self.aoi.registry)
        try:
            doc = yaml.safe_load(path.read_text())
        except FileNotFoundError:
            raise ValueError(f"aoi.registry not found: {path}") from None
        if not isinstance(doc, Mapping):
            raise TypeError(f"aoi.registry {path}: expected a mapping of name -> [w, s, e, n]")
        out: dict[str, Bbox] = {}
        for name, box in doc.items():
            if not (isinstance(box, list) and len(box) == 4):
                raise ValueError(f"aoi.registry {path}: zone {name!r} is not [w, s, e, n]")
            out[str(name)] = tuple(float(v) for v in box)  # type: ignore[assignment]
        return out

    def aoi_boxes(self, grid: GridDef | None = None) -> list[Bbox]:
        """The AOI as one box per zone, the bbox, or one box per explicit tile."""
        if self.aoi.bbox is not None:
            return [self.aoi.bbox]
        if self.aoi.zones is not None:
            reg = self.zone_registry()
            unknown = [z for z in self.aoi.zones if z not in reg]
            if unknown:
                raise ValueError(f"aoi.zones {unknown} not in registry; it has {sorted(reg)}")
            return [reg[z] for z in self.aoi.zones]
        assert self.aoi.tiles is not None
        grid = grid or self.grid_def()
        return [TileRef(grid, tx, ty).nominal_bounds for tx, ty in self.aoi.tiles]

    def aoi_bbox(self) -> Bbox:
        """(w, s, e, n) enclosing the whole area - the zones' union, the bbox, or the tiles'
        union. Zones far apart make this box mostly empty; `tiles()` does not tile the gap."""
        boxes = self.aoi_boxes()
        return (min(b[0] for b in boxes), min(b[1] for b in boxes),
                max(b[2] for b in boxes), max(b[3] for b in boxes))

    def tiles(self, grid: GridDef | None = None) -> list[TileRef]:
        """Tiles covering the AOI: the explicit list, or every tile whose nominal bounds
        intersect any zone / the bbox, ordered south to north then west to east. A box edge
        exactly on a tile edge does not pull in the next tile."""
        grid = grid or self.grid_def()
        if self.aoi.tiles is not None:
            return [TileRef(grid, tx, ty) for tx, ty in self.aoi.tiles]
        ts, eps = grid.tile_size, 1e-9
        found: set[tuple[int, int]] = set()
        for w, s, e, n in self.aoi_boxes(grid):
            tx0, tx1 = math.floor(w / ts + eps), math.ceil(e / ts - eps)
            ty0, ty1 = math.floor(s / ts + eps), math.ceil(n / ts - eps)
            found.update((tx, ty) for ty in range(ty0, ty1) for tx in range(tx0, tx1))
        return [TileRef(grid, tx, ty) for tx, ty in sorted(found, key=lambda t: (t[1], t[0]))]

    # -- time --------------------------------------------------------------------------------
    def epochs(self) -> list[Epoch]:
        return epochs_between(self.time.start, self.time.end, self.time.epoch,
                              align=self.time.align)

    def delivery_periods(self) -> list[DeliveryPeriod]:
        d = self.time.deliver
        assert d is not None and d.window is not None
        return delivery_periods(self.time.start, self.time.end, self.time.epoch, d.every,
                                d.window, d.align, epoch_align=self.time.align)

    # -- snapshot schema ---------------------------------------------------------------------
    def enumerations(self) -> dict[str, Enumeration]:
        """layer name -> Enumeration for every categorical layer with `@ref:` classes; loaded
        once, relative to the manifest, and cached."""
        if self._enumerations is None:
            found: dict[str, Enumeration] = {}
            for name, layer in self.snapshot.layers.items():
                if layer.classes_ref is not None:
                    found[name] = load_enumeration(self.resolve_ref(layer.classes))
            self._enumerations = found
        return self._enumerations

    def snapshot_schema(self) -> SnapshotSchema:
        """The manifest-level schema (13). A categorical layer with `@ref:` classes carries the
        enumeration's `class_table()`; one with `classes: source` carries `classes=None` here,
        because its table is the contributing granules' own and is only known at plan time.

        The planner substitutes per run with `dataclasses.replace(layer, classes=table)` -
        the fingerprint-agreed embedded table - and likewise fills a continuous layer's `dtype`
        from the reader's `VarSpec`. Categorical layers default to `uint16` here (13 section 2),
        so `layers_hash` of a fully `@ref` schema is already final at manifest level.
        """
        enums = self.enumerations()
        layers: list[LayerSpec] = []
        for name, layer in self.snapshot.layers.items():
            classes: ClassTable | None = None
            lumping: str | None = None
            dtype = layer.dtype
            if layer.kind == "categorical":
                dtype = dtype or "uint16"
                if name in enums:
                    classes = enums[name].class_table()
                    lumping = enums[name].fingerprint()
            layers.append(LayerSpec(
                name=name, kind=layer.kind, source=layer.source,
                aggregate=layer.aggregate.to_aggregation(), dtype=dtype,
                bands=tuple(layer.bands) if layer.bands is not None else None, classes=classes,
                lumping=lumping,
            ))
        return SnapshotSchema(name=self.snapshot.name, layers=tuple(layers),
                              extends=tuple(self.snapshot.extends))

    # -- roles -------------------------------------------------------------------------------
    def geolocation_role(self, reader_space: Callable[[str], str] | None = None) -> str:
        """The role regrid takes `loc` from (03 section 3): `inputs.geolocation` when set, else
        the first role in `inputs.roles` order whose collection's reader declares
        `space == "sensor"`. Readers are not importable here, so the caller passes
        `reader_space(collection) -> "sensor" | "ortho"`; without it the first role is assumed
        to be sensor-space, which is the planner's job to confirm."""
        if self.inputs.geolocation is not None:
            return self.inputs.geolocation
        roles = list(self.inputs.roles.items())
        if reader_space is None:
            return roles[0][0]
        for name, role in roles:
            if reader_space(role.collection) == "sensor":
                return name
        raise ValueError("no sensor-space role to take geolocation from; set inputs.geolocation "
                         "(03 section 3)")
