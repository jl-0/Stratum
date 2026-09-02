"""Product blocks -> one tile (first-slice plan section 4; 06 section 4 layout).

A product block is a directory of single-band GeoTIFFs, `{band}.tif`, one per delivered
`BandSpec`, covering the block's CORE window (no halo). `write_product_block` writes one from the
reducer's arrays and `stitch` assembles them into full-tile arrays, filling every cell no block
covered with the band's nodata.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from rasterio.windows import Window as RioWindow

from stratum.publish.cogs import write_geotiff
from stratum.types import BandSpec, BandStack, BlockRef, TileRef, Window


def band_path(directory: Path, band: str) -> Path:
    return Path(directory) / f"{band}.tif"


def fill_value(spec: BandSpec) -> float | int:
    """What an uncovered cell holds: the band's nodata; 0 for a band with none (a count - zero
    epochs is the truth for a cell no block covered, 11 section 2)."""
    return 0 if spec.nodata is None else spec.nodata


def empty_band(spec: BandSpec, shape: tuple[int, int]) -> np.ndarray:
    full = shape if spec.bands == 1 else (*shape, spec.bands)
    return np.full(full, fill_value(spec), dtype=spec.dtype)


def read_band(path: Path, window: Window | None = None) -> np.ndarray:
    """(H, W) for a single-band file, (H, W, B) band-last otherwise."""
    with rasterio.open(path) as src:
        rw = None
        if window is not None:
            rw = RioWindow(window.col_off, window.row_off, window.width, window.height)
        data = src.read(window=rw)
    if data.shape[0] == 1:
        return data[0]
    return np.moveaxis(data, 0, -1)


def write_product_block(out_dir: Path, arrays: Mapping[str, np.ndarray],
                        bands: Sequence[BandSpec], block: BlockRef, *,
                        tags: Mapping[str, Any] | None = None) -> list[Path]:
    """Write the reducer's arrays for one block as `{band}.tif` files on the block's core grid.
    Every band in `bands` must be present in `arrays` with the core window's shape."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if block.halo:
        raise ValueError(f"product block {block.name} has halo {block.halo}; product blocks are "
                         "core windows (first-slice plan section 4)")
    win = block.core_window
    transform = block.transform                 # halo 0: the core window's transform
    paths: list[Path] = []
    for spec in bands:
        if spec.name not in arrays:
            raise KeyError(f"reducer output lacks band {spec.name!r}")
        data = np.asarray(arrays[spec.name])
        if data.shape[:2] != (win.height, win.width):
            raise ValueError(f"band {spec.name!r}: shape {data.shape} does not cover core window "
                             f"{(win.height, win.width)} of block {block.name}")
        paths.append(write_geotiff(band_path(out_dir, spec.name), data, spec, transform=transform,
                                   crs=block.tile.grid.crs, tags=dict(tags or {})))
    return paths


def stitch(product_dirs: Sequence[tuple[BlockRef, Path]], tile: TileRef,
           bands: Sequence[BandSpec]) -> BandStack:
    """Assemble product blocks into full-tile arrays.

    Each `(block, directory)` contributes its core window; cells no block covers hold the band's
    nodata (`fill_value`). Blocks must belong to `tile` and appear once; every band must be
    present with the core window's shape (and the spec's band count, band-last).
    """
    rows, cols = tile.shape
    out = {spec.name: empty_band(spec, (rows, cols)) for spec in bands}
    seen: set[tuple[int, int]] = set()
    for block, directory in product_dirs:
        if block.tile != tile:
            raise ValueError(f"block {block.name} does not belong to tile {tile.name}")
        if (block.bx, block.by) in seen:
            raise ValueError(f"block {block.name} appears twice")
        seen.add((block.bx, block.by))
        win = block.core_window
        for spec in bands:
            path = band_path(directory, spec.name)
            if not path.exists():
                raise FileNotFoundError(f"product block {directory} lacks {spec.name}.tif")
            data = read_band(path)
            expected = (win.height, win.width) + ((spec.bands,) if spec.bands > 1 else ())
            if data.shape != expected:
                raise ValueError(f"{path}: shape {data.shape}, expected {expected} for block "
                                 f"{block.name}")
            out[spec.name][win.slices] = data.astype(spec.dtype, copy=False)
    return BandStack(out, bands)
