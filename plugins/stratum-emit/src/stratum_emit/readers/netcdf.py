"""The shared NetCDF machinery behind the EMIT readers (12 section 3).

Every DAAC EMIT product shares one layout: root variables over (downtrack, crosstrack[, bands]),
a `location` group with lat/lon/elev and the granule's own GLT, ACDD global attributes with
`geotransform` and `spatial_ref`. The per-product classes in `stratum_emit.readers` only name
their collection, their space, and where their band names live.

Fill handling (11 section 2): `read()` masks the variable's `_FillValue` (and non-finite
floats) and returns the data otherwise untouched - an int16 stays int16, a float's fill is left
in place under the mask. `geolocation()` is the one exception: it hands the KD-tree float64
arrays with fills as NaN, which is what the regrid wrapper consumes.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Self

import netCDF4 as nc
import numpy as np
import pyarrow as pa
from affine import Affine

from stratum.types import (
    AssetHandle,
    ClassTable,
    EmbeddedGLT,
    LocArray,
    SensorWindow,
    VarSpec,
)

LOCATION_GROUP = "location"
BAND_PARAMS_GROUP = "sensor_band_parameters"


@dataclass
class NetCDFContext:
    """What a reader keeps between open() and read(): the handle and the open Dataset.
    netCDF4 opens headers only; a variable is decoded on first index."""

    asset: AssetHandle
    dataset: nc.Dataset

    def close(self) -> None:
        if self.dataset.isopen():
            self.dataset.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _fill_of(var: nc.Variable) -> float | int | None:
    fill = getattr(var, "_FillValue", None)
    if fill is None:
        return None
    return fill.item() if isinstance(fill, np.generic) else fill


def _dtype_name(var: nc.Variable) -> str:
    return "str" if var.dtype is str else np.dtype(var.dtype).name


def _walk(ds: nc.Dataset, path: str) -> nc.Group | None:
    node: Any = ds
    for part in [p for p in path.strip("/").split("/") if p]:
        if part not in node.groups:
            return None
        node = node.groups[part]
    return node


def _column(var: nc.Variable) -> pa.Array:
    values = var[:]
    if var.dtype is str or np.asarray(values).dtype.kind in "OU":
        return pa.array([None if v is None else str(v) for v in np.asarray(values).tolist()],
                        type=pa.string())
    return pa.array(np.ma.getdata(values))


class EmitNetCDFReader:
    """Base for the sensor-space EMIT readers. Subclasses set `collections`, `space`, and
    `band_names_var` (the vlen-string variable under `sensor_band_parameters` naming the
    bands of a 3-D root variable; None when the product has none)."""

    collections: tuple[str, ...] = ()
    space: Literal["sensor", "ortho"] = "sensor"
    band_names_var: str | None = None

    # ---- open -----------------------------------------------------------------------------
    def open(self, asset: AssetHandle) -> NetCDFContext:
        """Cheap: headers and metadata only (12 section 3 rule 3)."""
        path = asset.path()
        if path is None:
            raise NotImplementedError(
                f"{type(self).__name__} reads a local path; streaming {asset.uri!r} without "
                "stage-in is 12 section 4")
        return NetCDFContext(asset=asset, dataset=nc.Dataset(Path(path), mode="r"))

    # ---- variables ------------------------------------------------------------------------
    def _band_attrs(self, ds: nc.Dataset, var: nc.Variable) -> Mapping[str, Sequence[Any]]:
        if self.band_names_var is None or var.ndim < 3:
            return {}
        params = ds.groups.get(BAND_PARAMS_GROUP)
        if params is None or self.band_names_var not in params.variables:
            return {}
        names = np.asarray(params.variables[self.band_names_var][:]).tolist()
        return {"name": [str(n) for n in names]}

    def variables(self, ctx: NetCDFContext) -> dict[str, VarSpec]:
        """Every ROOT variable: dtype, shape, fill, units, and band names where the product
        carries them (`band_attrs["name"]`). Groups are not variables a role can name."""
        ds = ctx.dataset
        out: dict[str, VarSpec] = {}
        for name, var in ds.variables.items():
            out[name] = VarSpec(
                name=name,
                dtype=_dtype_name(var),
                shape=tuple(int(n) for n in var.shape),
                fill=_fill_of(var),
                units=str(getattr(var, "units", "unitless") or "unitless"),
                band_attrs=self._band_attrs(ds, var),
            )
        return out

    # ---- read -----------------------------------------------------------------------------
    def read(self, ctx: NetCDFContext, var: str,
             window: SensorWindow | None = None) -> np.ma.MaskedArray:
        """(h, w) or (h, w, bands) in SENSOR space with the fill masked. Values are returned as
        stored: no dtype change, fill left in place under the mask (11 section 2)."""
        ds = ctx.dataset
        if var not in ds.variables:
            raise KeyError(f"{ctx.asset.uri}: no root variable {var!r}; "
                           f"has {sorted(ds.variables)}")
        v = ds.variables[var]
        if v.ndim < 2:
            raise ValueError(f"{var!r} is {v.ndim}-D; read() expects (downtrack, crosstrack...)")
        v.set_auto_maskandscale(False)
        fill = _fill_of(v)
        rest = tuple(int(n) for n in v.shape[2:])
        if window is not None and window.empty:
            data = np.empty((0, 0, *rest), dtype=v.dtype)
        elif window is None:
            data = np.asarray(v[:])
        else:
            rows, cols = window.slices
            data = np.asarray(v[rows, cols])
        mask = np.zeros(data.shape, dtype=bool)
        if fill is not None:
            mask |= data == fill
        if data.dtype.kind == "f":
            mask |= ~np.isfinite(data)
        return np.ma.MaskedArray(data, mask=mask, fill_value=fill)

    # ---- geolocation ----------------------------------------------------------------------
    def geolocation(self, ctx: NetCDFContext) -> LocArray | None:
        """lat/lon/elev as float64 (downtrack, crosstrack) with fills as NaN - the KD-tree
        input (03 section 3). None when the product ships no `location` group."""
        loc = ctx.dataset.groups.get(LOCATION_GROUP)
        if loc is None or not {"lat", "lon"} <= set(loc.variables):
            return None

        def load(name: str) -> np.ndarray:
            if name not in loc.variables:
                return np.full(loc.variables["lat"].shape, np.nan, dtype=np.float64)
            v = loc.variables[name]
            v.set_auto_maskandscale(False)
            arr = np.asarray(v[:], dtype=np.float64)
            fill = _fill_of(v)
            bad = ~np.isfinite(arr)
            if fill is not None:
                bad |= arr == fill
            arr[bad] = np.nan
            return arr

        return LocArray(lat=load("lat"), lon=load("lon"), elev=load("elev"))

    # ---- embedded GLT ---------------------------------------------------------------------
    def glt(self, ctx: NetCDFContext) -> EmbeddedGLT | None:
        """The granule's own (ortho_y, ortho_x) lookup table: bands (glt_x, glt_y, hit), 1-based,
        0 = nodata, georeferenced by the file's `geotransform` and `spatial_ref` (11 section 7
        [observed]). None when any of those is absent."""
        ds = ctx.dataset
        loc = ds.groups.get(LOCATION_GROUP)
        if loc is None or not {"glt_x", "glt_y"} <= set(loc.variables):
            return None
        if "geotransform" not in ds.ncattrs() or "spatial_ref" not in ds.ncattrs():
            return None
        gx, gy = loc.variables["glt_x"], loc.variables["glt_y"]
        gx.set_auto_maskandscale(False)
        gy.set_auto_maskandscale(False)
        x = np.asarray(gx[:], dtype=np.int32)
        y = np.asarray(gy[:], dtype=np.int32)
        hit = ((x != 0) & (y != 0)).astype(np.int32)
        data = np.stack([x, y, hit], axis=-1)
        gt = [float(g) for g in np.asarray(ds.getncattr("geotransform")).ravel()]
        return EmbeddedGLT(data=data, transform=Affine.from_gdal(*gt),
                           crs=str(ds.getncattr("spatial_ref")))

    # ---- class table ----------------------------------------------------------------------
    def class_table(self, ctx: NetCDFContext, path: str, key: str,
                    attributes: Sequence[str]) -> ClassTable | None:
        """A table embedded at group `path` (11 section 9): `key` first, then `attributes` in the
        order asked. None when the group is absent; a missing column is a KeyError, because the
        manifest named it."""
        grp = _walk(ctx.dataset, path)
        if grp is None:
            return None
        wanted = [key, *[a for a in attributes if a != key]]
        missing = [c for c in wanted if c not in grp.variables]
        if missing:
            raise KeyError(f"{ctx.asset.uri}:{path} has no column(s) {missing}; "
                           f"has {sorted(grp.variables)}")
        table = pa.table({c: _column(grp.variables[c]) for c in wanted})
        return ClassTable(key=key, entries=table, source=f"{ctx.asset.uri}:{path}")
