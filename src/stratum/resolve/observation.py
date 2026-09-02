"""One granule, one block: the read path of 12 section 2, steps 5-15.

    glt window -> hit? -> SensorWindow.covering -> open assets -> windowed reads
    -> sensor masks (conjunction) -> gather -> map masks (conjunction) -> ObsWindow

Below `ObsWindow` and invisible to every science plugin: a Scorer sees the result of step 15
and nothing else - no URIs, no sensor windows, no readers.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from affine import Affine
from rasterio.crs import CRS
from rasterio.warp import transform as warp_transform

from stratum.cache import CacheKey
from stratum.classes import UNMAPPED, Remap
from stratum.index import role_uri
from stratum.regrid import read_glt
from stratum.resolve.context import NullAux, PlanContext
from stratum.resolve.gather import Gathered, gather
from stratum.types import BlockRef, Coords, Epoch, GranuleRef, ObsWindow, SensorWindow, VarSpec


@dataclass
class ObsContext:
    """What `read_observation` needs: the plan, the block (halo included), the epoch, the
    granule, the GLT cache key regrid wrote for it, and - when the caller already read it -
    the GLT window, so the hit test and the gather share one range request."""

    plan: PlanContext
    block: BlockRef
    epoch: Epoch
    granule: GranuleRef
    glt_key: CacheKey
    glt: np.ndarray | None = None
    aux: Any = field(default_factory=NullAux)
    coords: Coords | None = None     # the window's cell centres, when the caller has them

    def glt_window(self) -> np.ndarray:
        if self.glt is None:
            self.glt = read_glt(self.glt_key.path, self.block.window)
        return self.glt


@dataclass
class Observation:
    """`read_observation`'s result: the block-space ObsWindow plus the per-layer values already
    remapped into the schema's enumerations (13 section 3 rule 3), ready for the scorer loop."""

    obs: ObsWindow
    layers: dict[str, np.ndarray]


def is_lonlat(crs: str) -> bool:
    return CRS.from_user_input(crs).to_epsg() == 4326


def block_coords(transform: Affine, shape: tuple[int, int], crs: str) -> Coords:
    """Cell centres of a window (11 section 5): x/y in the grid CRS from the transform,
    lon/lat through rasterio.warp when the CRS is not EPSG:4326, identity otherwise."""
    rows, cols = shape
    x = transform.c + (np.arange(cols, dtype=np.float64) + 0.5) * transform.a
    y = transform.f + (np.arange(rows, dtype=np.float64) + 0.5) * transform.e
    yy, xx = np.meshgrid(y, x, indexing="ij")
    if is_lonlat(crs):
        return Coords(x=xx, y=yy, lon=xx, lat=yy)
    lon, lat = warp_transform(CRS.from_user_input(crs), CRS.from_epsg(4326),
                              xx.ravel(), yy.ravel())
    return Coords(x=xx, y=yy, lon=np.asarray(lon).reshape(xx.shape),
                  lat=np.asarray(lat).reshape(yy.shape))


def apply_remap(raw: np.ndarray, remap: Remap) -> np.ndarray:
    """raw class keys -> product ids, int32; UNMAPPED (-1) for a key the remap does not
    describe, including keys beyond the raw table's range (13 section 3)."""
    raw = np.asarray(raw)
    out = np.full(raw.shape, UNMAPPED, dtype=np.int32)
    keys = raw.astype(np.int64, copy=False)
    inside = (keys >= 0) & (keys < remap.lookup.shape[0])
    out[inside] = remap.lookup[keys[inside]]
    return out


def _select_bands(arr: np.ndarray, bands: tuple[int, ...] | None, layer: str) -> np.ndarray:
    if bands is None:
        return arr
    if arr.ndim != 3:
        raise ValueError(f"layer {layer!r} selects bands {list(bands)} of a single-band source")
    return arr[..., list(bands)]


def _sensor_bands(plan: PlanContext, read: Mapping[str, np.ma.MaskedArray]) -> dict[str, Any]:
    """Role entries plus alias entries over the sensor window (12 section 2 step 13)."""
    bands: dict[str, Any] = dict(read)
    for name, alias in plan.aliases.items():
        if alias.role in read:
            arr = read[alias.role]
            if arr.ndim != 3:
                raise ValueError(f"alias {name!r} selects band {alias.band} of single-band "
                                 f"role {alias.role!r}")
            bands[name] = arr[..., alias.band]
    return bands


def read_observation(ctx: ObsContext) -> Observation | None:
    """Steps 5-15 of 12 section 2 for one granule over one block. None when the GLT has no hit
    in the block window (step 7). The returned ObsWindow is in BLOCK space with an entry per
    role read and per alias whose role was read; `valid` is the conjunction over every role of
    (hit, not fill, admitted by every sensor mask) and then every map mask."""
    plan, block, granule = ctx.plan, ctx.block, ctx.granule
    glt = ctx.glt_window()
    if not (glt[..., 2] != 0).any():
        return None
    sw = SensorWindow.covering(glt[..., 0], glt[..., 1])

    roles = plan.roles_to_read()
    if not roles:
        raise ValueError("nothing to read: the schema, scorer and masks name no role")
    contexts: dict[str, Any] = {}
    readers: dict[str, Any] = {}
    try:
        read: dict[str, np.ma.MaskedArray] = {}
        specs: dict[str, VarSpec] = {}
        for role in roles:
            binding = plan.roles[role]
            uri = role_uri(granule, binding)
            if uri is None:
                raise ValueError(
                    f"granule {granule.granule_id!r} has no asset for role {role!r} "
                    f"(collection {binding.collection!r}); the planner should have excluded it "
                    "(12 section 7)")
            reader = readers.get(binding.collection)
            if reader is None:
                reader = readers[binding.collection] = plan.reader(binding.collection)
            rctx = contexts.get(uri)
            if rctx is None:
                rctx = contexts[uri] = reader.open(plan.store.open(uri))
            spec = reader.variables(rctx).get(binding.var)
            if spec is None:
                raise KeyError(f"{uri}: no variable {binding.var!r} for role {role!r}")
            specs[role] = spec
            read[role] = reader.read(rctx, binding.var, sw)
    finally:
        for rctx in contexts.values():
            close = getattr(rctx, "close", None)
            if close is not None:
                close()

    shapes = {role: tuple(int(n) for n in spec.shape[:2]) for role, spec in specs.items()}
    if len(set(shapes.values())) > 1:
        raise ValueError(f"roles disagree on the sensor shape {shapes}; one GLT indexes one "
                         "sensor array (03 section 3)")
    anchor = plan.geolocation_role if plan.geolocation_role in shapes else roles[0]
    sensor_shape = shapes[anchor]
    band_attrs = {role: dict(spec.band_attrs) for role, spec in specs.items() if spec.band_attrs}

    # step 13: sensor-space masks, by conjunction, over the sensor window with its origin
    ok: np.ndarray | None = None
    if plan.sensor_masks:
        sensor_valid = np.ones((sw.height, sw.width), dtype=bool)
        for arr in read.values():
            m = np.ma.getmaskarray(arr)
            sensor_valid &= ~(m.any(axis=-1) if m.ndim == 3 else m)
        sensor_obs = ObsWindow(space="sensor", bands=_sensor_bands(plan, read), valid=sensor_valid,
                               granules=(granule,), epoch=ctx.epoch, sensor_window=sw,
                               sensor_shape=sensor_shape, band_attrs=band_attrs)
        ok = np.ones((sw.height, sw.width), dtype=bool)
        for mask in plan.sensor_masks:
            ok &= np.asarray(mask.valid(sensor_obs, ctx.aux), dtype=bool)

    # step 14: the gather, per role
    gathered: dict[str, Gathered] = {role: gather(arr, glt, sw, ok) for role, arr in read.items()}
    valid = np.ones(glt.shape[:2], dtype=bool)
    interpolated = np.zeros(glt.shape[:2], dtype=bool)
    bands: dict[str, np.ndarray] = {}
    for role, g in gathered.items():
        valid &= g.valid
        interpolated |= g.interpolated
        bands[role] = g.band
    for name, alias in plan.aliases.items():
        if alias.role in bands:
            bands[name] = bands[alias.role][..., alias.band]

    obs = ObsWindow(space="block", bands=bands, valid=valid, granules=(granule,),
                    epoch=ctx.epoch, block=block, interpolated=interpolated,
                    coords=ctx.coords or block_coords(block.transform, glt.shape[:2],
                                                      plan.grid.crs),
                    band_attrs=band_attrs)

    # step 15: map-space masks, by conjunction, on the block
    for mask in plan.map_masks:
        valid &= np.asarray(mask.valid(obs, ctx.aux), dtype=bool)

    # 13 section 3 rule 3: the remap is applied at the gather, where the granule is known
    layers: dict[str, np.ndarray] = {}
    for layer in plan.schema.layers:
        values = _select_bands(bands[layer.source], layer.bands, layer.name)
        if layer.kind == "categorical":
            try:
                remap = plan.remaps[layer.name][granule.granule_id]
            except KeyError:
                raise KeyError(
                    f"no class remap for granule {granule.granule_id!r} on layer "
                    f"{layer.name!r}; per-granule resolution is a plan-time step "
                    "(13 section 3 rule 2)") from None
            values = apply_remap(values, remap)
            valid &= values != UNMAPPED
        layers[layer.name] = values
    return Observation(obs=obs, layers=layers)


__all__ = ["ObsContext", "Observation", "apply_remap", "block_coords", "is_lonlat",
           "read_observation"]
