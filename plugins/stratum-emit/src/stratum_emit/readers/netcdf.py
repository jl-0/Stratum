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


#: What a ranged open should fetch up front: the head (superblock, root group) and the last
#: 256 KiB. [observed] 2026-10-06: EMIT L2B MIN files are not paged-aggregated, so where the
#: metadata lands varies. On three US West 2025 granules `/mineral_metadata` sat in the final
#: ~75 KB and these two ranges covered the read; on the reference granule (build 010635) three
#: object headers sat 1.4-4.4 MB from the end, costing three more round trips - 568 KB of a
#: 54 MB file in all. Widening the tail to cover that would cost MB per granule to save
#: milliseconds on the few granules an identity-sampled plan reads.
RANGED_PREFETCH: tuple[tuple[int, int], ...] = ((0, 64 * 1024), (-256 * 1024, 256 * 1024))


@dataclass
class RangedContext:
    """A granule opened over HTTP byte ranges (12 section 4) with h5py, which - unlike netCDF4 -
    reads from a file object. Only `class_table` works on it: it exists so the planner can read
    a table without staging the file. Anything else stages."""

    asset: AssetHandle
    h5: Any

    @property
    def dataset(self) -> nc.Dataset:
        raise NotImplementedError(f"{self.asset.uri!r} was opened over byte ranges, which serve "
                                  "class tables only; stage it to read anything else "
                                  "(12 section 4)")

    def close(self) -> None:
        self.h5.close()


def _h5_column(ds: Any) -> pa.Array:
    """`_column` for an h5py dataset, decoding to the SAME values netCDF4 yields, because a
    worker re-reads the table with netCDF4 and the two fingerprints must agree (12 section 7)."""
    import h5py

    if h5py.check_string_dtype(ds.dtype) is not None:
        return pa.array([None if v is None else str(v) for v in ds.asstr()[()].tolist()],
                        type=pa.string())
    return pa.array(np.asarray(ds[()]))


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
    """Base for the sensor-space EMIT readers. Subclasses set `collections`, `space`,
    `band_names_var` (the vlen-string variable under `sensor_band_parameters` naming the
    bands of a 3-D root variable; None when the product has none) and, where the product has
    them, `band_attr_vars`."""

    collections: tuple[str, ...] = ()
    space: Literal["sensor", "ortho"] = "sensor"
    band_names_var: str | None = None
    #: Further per-band attributes, {band_attrs key: variable under `sensor_band_parameters`},
    #: reported for a 3-D variable whose band axis they match (e.g. `wavelength` for a
    #: `match: {wavelength: ...}` alias, 11 section 5).
    band_attr_vars: Mapping[str, str] = {}
    #: Declares that `open` accepts a ranged handle (12 section 4), and what to fetch first.
    range_prefetch: tuple[tuple[int, int], ...] = RANGED_PREFETCH

    # ---- open -----------------------------------------------------------------------------
    def open(self, asset: AssetHandle) -> NetCDFContext | RangedContext:
        """Cheap: headers and metadata only (12 section 3 rule 3)."""
        path = asset.path()
        fileobj = getattr(asset, "fileobj", None)
        if path is None and fileobj is not None:
            import h5py

            return RangedContext(asset=asset, h5=h5py.File(fileobj(), mode="r"))
        if path is None:
            raise NotImplementedError(
                f"{type(self).__name__} reads a local path; streaming {asset.uri!r} without "
                "stage-in is 12 section 4")
        return NetCDFContext(asset=asset, dataset=nc.Dataset(Path(path), mode="r"))

    # ---- variables ------------------------------------------------------------------------
    def _band_attrs(self, ds: nc.Dataset, var: nc.Variable) -> Mapping[str, Sequence[Any]]:
        if var.ndim < 3:
            return {}
        params = ds.groups.get(BAND_PARAMS_GROUP)
        if params is None:
            return {}
        out: dict[str, Sequence[Any]] = {}
        if self.band_names_var is not None and self.band_names_var in params.variables:
            names = np.asarray(params.variables[self.band_names_var][:]).tolist()
            out["name"] = [str(n) for n in names]
        nbands = int(var.shape[-1])
        for attr, name in self.band_attr_vars.items():
            if name not in params.variables:
                continue
            values = np.ma.getdata(params.variables[name][:]).ravel()
            if values.size == nbands:          # a band table for THIS variable's band axis
                out[attr] = [v.item() for v in values]
        return out

    def variables(self, ctx: NetCDFContext) -> dict[str, VarSpec]:
        """Every ROOT variable: dtype, shape, fill, units, and band names where the product
        carries them (`band_attrs["name"]`, plus any `band_attr_vars`). Groups are not
        variables a role can name."""
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
    def class_table(self, ctx: NetCDFContext | RangedContext, path: str, key: str,
                    attributes: Sequence[str]) -> ClassTable | None:
        """A table embedded at group `path` (11 section 9): `key` first, then `attributes` in the
        order asked. None when the group is absent; a missing column is a KeyError, because the
        manifest named it."""
        wanted = [key, *[a for a in attributes if a != key]]
        if isinstance(ctx, RangedContext):
            grp = ctx.h5.get(path)
            if grp is None:
                return None
            missing = [c for c in wanted if c not in grp]
            if missing:
                raise KeyError(f"{ctx.asset.uri}:{path} has no column(s) {missing}; "
                               f"has {sorted(grp)}")
            table = pa.table({c: _h5_column(grp[c]) for c in wanted})
            return ClassTable(key=key, entries=table, source=f"{ctx.asset.uri}:{path}")
        grp = _walk(ctx.dataset, path)
        if grp is None:
            return None
        missing = [c for c in wanted if c not in grp.variables]
        if missing:
            raise KeyError(f"{ctx.asset.uri}:{path} has no column(s) {missing}; "
                           f"has {sorted(grp.variables)}")
        table = pa.table({c: _column(grp.variables[c]) for c in wanted})
        return ClassTable(key=key, entries=table, source=f"{ctx.asset.uri}:{path}")
