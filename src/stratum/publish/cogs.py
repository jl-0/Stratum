"""Data GeoTIFF writers: one tiled, deflate-compressed file per delivered band (07 section 2).

The data product is what ships; the image is derived from it. Every file carries `run_id` and
`manifest_hash` in its tags so an orphaned GeoTIFF still leads back to its run (10 section 3).
Written with the GTiff driver rather than GDAL's COG driver, which would build the whole file in
memory to reorder it; internal tiling is what makes a window read a range request. No overviews
(07 section 7, resolution 3).
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from affine import Affine

from stratum.publish.colors import RGBA, gdal_colormap, resolve_class_colors
from stratum.reduce import CATEGORICAL_DTYPE, CATEGORICAL_NODATA
from stratum.types import BandSpec, BandStack, ClassTable, TileRef

COG_MEDIA_TYPE = "image/tiff; application=geotiff; profile=cloud-optimized"
REQUIRED_TAGS = ("run_id", "manifest_hash")     # 10 section 3
FORMATS = ("cog", "netcdf")                      # what the manifest may name (07)
IMPLEMENTED_FORMATS = ("cog",)


def check_formats(formats: Sequence[str]) -> None:
    """Refuse a `formats` list the first slice cannot write. `netcdf` is a later slice."""
    for f in formats:
        if f not in FORMATS:
            raise ValueError(f"outputs.formats: {f!r} is not one of {FORMATS}")
        if f not in IMPLEMENTED_FORMATS:
            raise NotImplementedError(f"outputs.formats: {f!r} is not implemented in the first "
                                      "slice; only `cog` is (07 section 2)")


def internal_tile_size(shape: tuple[int, int]) -> int:
    """The GeoTIFF internal tile edge for a raster of `shape`: the largest multiple of 16 up to
    512 that divides both dimensions, so block-aligned reads touch only their own bytes; 256 when
    none does."""
    rows, cols = shape
    best = 0
    for d in range(16, min(rows, cols, 512) + 1, 16):
        if rows % d == 0 and cols % d == 0:
            best = d
    return best or 256


def is_categorical(spec: BandSpec) -> bool:
    """A delivered band holding product ids: the reducer's categorical convention (first-slice
    plan section 4) - uint16 with nodata 65535. Counts are uint16 with no nodata."""
    return spec.dtype == CATEGORICAL_DTYPE and spec.nodata == CATEGORICAL_NODATA


def _require_tags(tags: Mapping[str, Any]) -> None:
    missing = [t for t in REQUIRED_TAGS if t not in tags]
    if missing:
        raise ValueError(f"tags must carry {list(REQUIRED_TAGS)}; missing {missing} (10 section 3)")


def _band_first(data: np.ndarray, spec: BandSpec) -> np.ndarray:
    """(H, W) or (H, W, B) as the reducer delivers -> (count, H, W) for rasterio."""
    data = np.asarray(data)
    if spec.bands == 1:
        if data.ndim == 3 and data.shape[-1] == 1:
            data = data[..., 0]
        if data.ndim != 2:
            raise ValueError(f"band {spec.name!r}: expected (H, W), got shape {data.shape}")
        return data[None]
    if data.ndim != 3 or data.shape[-1] != spec.bands:
        raise ValueError(f"band {spec.name!r}: expected (H, W, {spec.bands}), got {data.shape}")
    return np.moveaxis(data, -1, 0)


def write_geotiff(path: Path, data: np.ndarray, spec: BandSpec, *, transform: Affine, crs: str,
                  tags: Mapping[str, Any], colormap: Mapping[int, RGBA] | None = None) -> Path:
    """One delivered band as a tiled deflate GeoTIFF: nodata, description, units and the tags
    from `spec` and `tags`; a GDAL colour table on band 1 when `colormap` is given."""
    arr = _band_first(data, spec).astype(spec.dtype, copy=False)
    count, rows, cols = arr.shape
    bs = internal_tile_size((rows, cols))
    profile = {"driver": "GTiff", "height": rows, "width": cols, "count": count,
               "dtype": spec.dtype, "nodata": spec.nodata, "crs": crs, "transform": transform,
               "tiled": True, "blockxsize": bs, "blockysize": bs, "compress": "deflate"}
    path = Path(path)
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(arr)
        dst.update_tags(**{k: str(v) for k, v in tags.items()}, units=spec.units)
        for i in range(1, count + 1):
            desc = spec.description if count == 1 else f"{spec.description} [band {i}]"
            dst.set_band_description(i, desc)
            dst.set_band_unit(i, spec.units)
            dst.update_tags(i, units=spec.units)
        if colormap is not None:
            dst.write_colormap(1, dict(colormap))
    return path


def write_data_cogs(out_dir: Path, stack: BandStack, tile: TileRef, *,
                    class_table: ClassTable | None, tags: Mapping[str, Any],
                    colors: Mapping[str, Sequence[int]] | None = None) -> list[Path]:
    """Write every band of `stack` as `{band}.tif` under `out_dir` on the tile's grid.

    `tags` must carry `run_id` and `manifest_hash` (10 section 3); `grid_id` and `tile` are added.
    Categorical bands (`is_categorical`) embed a colour table derived from `class_table` and the
    render `colors` (by class name; the deterministic palette when None), so a viewer that styles
    single-band rasters can use the data COG directly (07 section 7, resolution 2). Classes
    `colors` leaves out are black in the table - the image, not the data COG, is where
    `on_unmapped` is enforced - and so is `none`: a TIFF colour table carries no alpha (GDAL
    reads every entry back opaque), so the data COG's only transparency is its nodata value.
    Returns the paths in band order.
    """
    _require_tags(tags)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    colormap = None
    if class_table is not None:
        colormap = gdal_colormap(resolve_class_colors(class_table, colors,
                                                      on_unmapped="transparent"))
    full_tags = {**tags, "grid_id": tile.grid.id, "tile": f"{tile.tx}_{tile.ty}"}
    paths: list[Path] = []
    for spec in stack.specs:
        cm = colormap if is_categorical(spec) else None
        paths.append(write_geotiff(out_dir / f"{spec.name}.tif", stack[spec.name], spec,
                                   transform=tile.transform, crs=tile.grid.crs, tags=full_tags,
                                   colormap=cm))
    return paths
