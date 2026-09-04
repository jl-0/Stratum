"""The regrid stage: pure geometry. loc -> GLT by KD-tree (03).

Regrid reads a granule's `loc` array and nothing else - no pixel band, no mask (03 section 5) -
and produces one GLT per granule per tile (03 section 6, resolution 2). The algorithm is
SpectralUtil's `find_subgrid_locations` + `remove_negatives(clean_contiguous=True)`, wrapped and
never copied (03 section 4).

**Algorithm version and the recorded hash (06 section 3, rule 2).** `REGRID_ALGO_VERSION` enters
every GLT cache key and is bumped by hand when regrid output changes. To make the manual step
impossible to forget silently, `ALGO_HASH` beside this file records
`"{REGRID_ALGO_VERSION} {module_content_hash()}"`, and `tests/test_regrid.py` recomputes it.
The workflow when you edit anything under `stratum/regrid/`:

1. If the change can alter any GLT value, bump `REGRID_ALGO_VERSION`.
2. Run `pixi run python -m stratum.regrid --record` to rewrite `ALGO_HASH`.
3. Commit both. The test fails until you do; a bump without a re-record fails too.

A cosmetic change (docstring, comment) only needs step 2. That is the deliberate sign-off: the
person re-recording is asserting that output did not change.
"""
from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from affine import Affine
from rasterio.crs import CRS
from rasterio.errors import CRSError
from rasterio.windows import Window as RioWindow
from spectral_util.mosaic.mosaic import find_subgrid_locations, remove_negatives

from stratum.cache import CacheKey, CacheRoot, glt_inputs
from stratum.types import EmbeddedGLT, GridDef, LocArray, TileRef, Window

REGRID_ALGO_VERSION = 1
"""Bump when GLT output changes; see the module docstring and 06 section 3."""

MAX_DISTANCE_FACTOR = 1.5
"""`max_distance=None` -> this many grid diagonals (03 section 3, step 5)."""

GLT_BAND_NAMES = ("GLT X", "GLT Y", "File Index")
ALGO_HASH_PATH = Path(__file__).with_name("ALGO_HASH")

REGRID_METHODS = ("kdtree", "adopt")
"""What `regrid_granule_tile` implements. `warp_embedded` (03 section 3) is a later slice."""

LATTICE_TOLERANCE = 1e-6
"""How far a product's own grid may sit off the run's lattice and still be adopted, in cells.
Measured spread across 1360 EMIT granules (3 collections, 7 months, both hemispheres) is 2e-9
cell, so this is three orders looser than the noise and six orders tighter than a cell."""


# --------------------------------------------------------------------------------------- geometry
def resolve_max_distance(grid: GridDef, max_distance: float | None) -> float:
    """The threshold actually used, which is what enters the cache key (plan section 4)."""
    if max_distance is None:
        return MAX_DISTANCE_FACTOR * grid.diagonal
    if max_distance <= 0:
        raise ValueError("max_distance must be positive")
    return float(max_distance)


def cell_centres(tile: TileRef) -> tuple[np.ndarray, np.ndarray]:
    """(y, x) cell-centre meshes for the full tile, from `TileRef.transform` (north-up, row 0 is
    the northern edge): x = x0 + (c + 0.5) * rx, y = y1 + (r + 0.5) * ry."""
    rows, cols = tile.shape
    t = tile.transform
    x = t.c + (np.arange(cols, dtype=np.float64) + 0.5) * t.a
    y = t.f + (np.arange(rows, dtype=np.float64) + 0.5) * t.e
    y_grid, x_grid = np.meshgrid(y, x, indexing="ij")
    return y_grid, x_grid


def _valid_points(loc: LocArray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(row, col) of every usable sensor pixel plus their (lat, lon). A fill arrives either as a
    masked element or as NaN (11 section 10: 'fills already masked'); both are dropped."""
    lat, lon = loc.lat, loc.lon
    valid = ~np.ma.getmaskarray(lat) & ~np.ma.getmaskarray(lon)
    valid &= np.isfinite(np.ma.getdata(lat)) & np.isfinite(np.ma.getdata(lon))
    rows, cols = np.nonzero(valid)
    lat_v = np.asarray(np.ma.getdata(lat), dtype=np.float64)[rows, cols]
    lon_v = np.asarray(np.ma.getdata(lon), dtype=np.float64)[rows, cols]
    return np.stack([rows, cols], axis=1), lat_v, lon_v


def build_glt_raw(loc: LocArray, tile: TileRef, *, max_distance: float | None,
                  n_workers: int = 1) -> np.ndarray:
    """`build_glt` before the contiguous-interpolation clean: steps 1-5 of 03 section 3.

    Wraps `find_subgrid_locations`. That function unravels KD-tree indices against the shape of
    the loc arrays it is given, so it cannot take pre-filtered points while preserving indices.
    The valid points are therefore passed as an (n, 1) column - it then returns
    `row = flat + 1, col = 1` - and `|row| - 1` is mapped back here to the (row, col) of the
    FULL sensor array, sign preserved. GLT X is the sensor column, GLT Y the sensor row, both
    1-based; negative marks a cell whose nearest sensor pixel lies beyond `max_distance`."""
    rows, cols = tile.shape
    glt = np.zeros((rows, cols, 3), dtype=np.int32)
    md = resolve_max_distance(tile.grid, max_distance)

    rc, lat_v, lon_v = _valid_points(loc)
    if rc.shape[0] == 0:
        return glt

    y_grid, x_grid = cell_centres(tile)
    sub, insert = find_subgrid_locations(y_grid, x_grid, lat_v[:, None], lon_v[:, None],
                                         max_distance=md, n_workers=n_workers)
    if sub is None:  # the granule's bounding box does not touch this tile
        return glt

    flat_signed = sub[..., 1]  # (h, w): +-(flat index + 1) into the valid-point list
    sign = np.sign(flat_signed).astype(np.int32)
    flat = np.abs(flat_signed) - 1
    src_row = rc[flat, 0].astype(np.int32) + 1
    src_col = rc[flat, 1].astype(np.int32) + 1
    r_ins, c_ins = insert[..., 0], insert[..., 1]
    glt[r_ins, c_ins, 0] = sign * src_col
    glt[r_ins, c_ins, 1] = sign * src_row
    glt[r_ins, c_ins, 2] = 1
    return glt


class LatticeMismatch(ValueError):
    """A product grid `adopt` cannot use: a different CRS, a different cell size, a rotation, or
    an origin that does not fall on the run grid's lattice (03 section 3)."""


def lattice_offset(grid: GridDef, transform: Affine, crs: str) -> tuple[int, int]:
    """Where a raster with `transform` sits on `grid`'s lattice, as whole cells (kx, ky) east and
    south of the grid origin. Raises `LatticeMismatch` when it does not sit on the lattice at all.

    This is the whole precondition for `adopt`: same CRS, same cell size, and an origin an
    integer number of cells from `grid.origin`. Nothing about extent - a product raster covers
    its own granule and is cropped to the tile afterwards.
    """
    rx, ry = grid.resolution
    try:
        if CRS.from_user_input(crs) != CRS.from_user_input(grid.crs):
            raise LatticeMismatch(f"CRS {crs!r} is not the grid's {grid.crs!r}")
    except CRSError as exc:
        raise LatticeMismatch(f"CRS {crs!r} cannot be compared with the grid's "
                              f"{grid.crs!r}: {exc}") from None
    if transform.b or transform.d:
        raise LatticeMismatch(f"transform {transform!r} is rotated or sheared; a grid raster is "
                              "north-up (01 section 1)")
    for got, want, axis in ((transform.a, rx, "x"), (transform.e, ry, "y")):
        if abs(got - want) > abs(want) * LATTICE_TOLERANCE:
            raise LatticeMismatch(f"{axis} cell size {got!r} is not the grid's {want!r} "
                                  f"(off by {abs(got - want) / abs(want):.3g} cell)")
    x0, y0 = grid.origin
    kx, ky = (transform.c - x0) / rx, (transform.f - y0) / ry
    off = max(abs(kx - round(kx)), abs(ky - round(ky)))
    if off > LATTICE_TOLERANCE:
        raise LatticeMismatch(
            f"origin ({transform.c!r}, {transform.f!r}) is {off:.3g} cell off the lattice of "
            f"grid origin ({x0!r}, {y0!r}) (tolerance {LATTICE_TOLERANCE:g} cell); the product "
            "was not gridded on this lattice, so its lookup table cannot be adopted")
    return round(kx), round(ky)


def adopt_glt(embedded: EmbeddedGLT, tile: TileRef) -> np.ndarray:
    """The product's own lookup table, moved onto `tile` (03 section 3, `adopt`).

    Valid only when the product was gridded on the run's lattice (`lattice_offset`, which this
    calls and which refuses otherwise). Then no value is resampled and no index is recomputed:
    the cells are already right, they only move to their place in the tile, and cells the
    product's raster does not cover are 0. A cell either product band leaves at 0 is 0 in all
    three bands, so bands 1-2 and the hit band cannot disagree.

    What is NOT inherited is the interpolated marker: a product ships its lookup table with the
    sign already resolved, so `max_distance` and the contiguity stencil have no meaning here and
    an adopted GLT carries whatever fill decisions its producer made.
    """
    lattice_offset(tile.grid, embedded.transform, embedded.crs)
    src = np.asarray(embedded.data)
    if src.ndim != 3 or src.shape[-1] < 2:
        raise ValueError(f"an embedded GLT is (H, W, >=2); got {src.shape}")
    rows, cols = tile.shape
    out = np.zeros((rows, cols, 3), dtype=np.int32)
    rx, ry = tile.grid.resolution
    t = tile.transform
    row0 = round((embedded.transform.f - t.f) / ry)
    col0 = round((embedded.transform.c - t.c) / rx)
    sh, sw = src.shape[:2]
    r0, r1 = max(row0, 0), min(row0 + sh, rows)
    c0, c1 = max(col0, 0), min(col0 + sw, cols)
    if r0 >= r1 or c0 >= c1:
        return out                      # the product's raster does not reach this tile
    win = src[r0 - row0:r1 - row0, c0 - col0:c1 - col0].astype(np.int32, copy=False)
    hit = (win[..., 0] != 0) & (win[..., 1] != 0)
    out[r0:r1, c0:c1, 0] = np.where(hit, win[..., 0], 0)
    out[r0:r1, c0:c1, 1] = np.where(hit, win[..., 1], 0)
    out[r0:r1, c0:c1, 2] = hit
    return out


def clean_contiguous(glt: np.ndarray) -> np.ndarray:
    """Step 6 of 03 section 3, in place: `remove_negatives(clean_contiguous=True)`. A 3x3
    `convolve2d` over the interpolated mask zeroes every cell (all three bands) with >= 3 flagged
    cells in its 3x3 neighbourhood, itself included. **This is a stencil** (01 section 4); it is
    only seam-free because GLTs are built per whole tile, never per block."""
    return remove_negatives(glt, clean_contiguous=True, clean_interpolated=False)


def build_glt(loc: LocArray, tile: TileRef, *, max_distance: float | None,
              n_workers: int = 1) -> np.ndarray:
    """(H, W, 3) int32 for the full tile (03 section 2): GLT X, GLT Y 1-based, 0 = no hit,
    negative = interpolated; band 3 is 1 wherever bands 1-2 are non-zero. See `build_glt_raw`
    for the index mapping and `clean_contiguous` for the stencil applied last."""
    return clean_contiguous(build_glt_raw(loc, tile, max_distance=max_distance,
                                          n_workers=n_workers))


# --------------------------------------------------------------------------------------------- IO
def internal_tile_size(block: int) -> int:
    """The GeoTIFF internal tile edge: the largest divisor of `block` that is a multiple of 16
    and <= 512, so a block's window read touches only its own bytes (12 section 2, step 6);
    256 when no such divisor exists (a block not a multiple of 16)."""
    best = 0
    for d in range(16, min(block, 512) + 1, 16):
        if block % d == 0:
            best = d
    return best or 256


def write_glt(path: Path, glt: np.ndarray, tile: TileRef, *, granule_id: str,
              tags: Mapping[str, Any]) -> Path:
    """Write a GLT as a tiled, deflate-compressed 3-band int32 GeoTIFF, nodata 0, no overviews
    (07 section 7, resolution 3). Tags carry `granule_id` and `grid_id` so the file is meaningful
    without any file list (03 section 2), plus whatever the caller passes (algorithm version,
    max_distance, method). Written with the GTiff driver rather than GDAL's COG driver, which
    would build the whole file in memory to reorder it; tiling is what makes the window read a
    range request."""
    glt = np.asarray(glt, dtype=np.int32)
    rows, cols = tile.shape
    if glt.shape != (rows, cols, 3):
        raise ValueError(f"GLT shape {glt.shape} does not match tile shape {(rows, cols, 3)}")
    bs = internal_tile_size(tile.grid.block_size)
    profile = {"driver": "GTiff", "height": rows, "width": cols, "count": 3, "dtype": "int32",
               "nodata": 0, "crs": tile.grid.crs, "transform": tile.transform, "tiled": True,
               "blockxsize": bs, "blockysize": bs, "compress": "deflate"}
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(np.moveaxis(glt, -1, 0))
        dst.update_tags(granule_id=granule_id, grid_id=tile.grid.id,
                        **{k: str(v) for k, v in tags.items()})
        for i, name in enumerate(GLT_BAND_NAMES, start=1):
            dst.set_band_description(i, name)
    return Path(path)


def read_glt(path: Path, window: Window | None = None) -> np.ndarray:
    """(H, W, 3) int32 for `window` (a tile-space `Window`, row/col offsets) or the whole file.
    A rasterio window read: only the internal tiles covering the window are fetched. A window
    that reaches past the tile - a halo at a tile edge (01 section 4) - is padded with 0, no hit,
    through a boundless read; in-bounds windows take the plain path."""
    with rasterio.open(path) as src:
        if window is None:
            data = src.read()
        else:
            rio = RioWindow(window.col_off, window.row_off, window.width, window.height)
            inside = (window.row_off >= 0 and window.col_off >= 0
                      and window.row_off + window.height <= src.height
                      and window.col_off + window.width <= src.width)
            if inside:
                data = src.read(window=rio)
            else:
                data = src.read(window=rio, boundless=True, fill_value=0)
    return np.moveaxis(data, 0, -1).astype(np.int32, copy=False)


# ------------------------------------------------------------------------------------- the stage
def regrid_granule_tile(cache: CacheRoot, tile: TileRef, granule_id: str,
                        loc: Callable[[], LocArray], *, max_distance: float | None,
                        regrid_method: str = "kdtree", n_workers: int = 1,
                        embedded: Callable[[], EmbeddedGLT] | None = None,
                        source_id: str | None = None) -> CacheKey:
    """One work item of the regrid stage: the GLT for `granule_id` over `tile`.

    Computes the key (06 section 2) and returns at once on a hit, so a hit never reads the
    granule: the reader callables are zero-argument and are called only on a miss. A granule that
    does not touch the tile still writes a GLT of zeros, so the miss is not rediscovered on the
    next run; 06 section 8 question 2 defers negative caching until the pilot has measured it,
    and a zero GLT is the cheapest thing that makes the question moot for a cached pair.

    `kdtree` builds the GLT from `loc`. `adopt` takes the product's own lookup table from
    `embedded` and crops it to the tile, which requires the product to have been gridded on the
    run's lattice; `source_id` identifies the bytes it came from (the asset's catalogue
    checksum) and enters the key, because under `adopt` the producer's pipeline, not this one,
    determines the output.
    """
    if regrid_method not in REGRID_METHODS:
        raise NotImplementedError(
            f"regrid_method {regrid_method!r} is not implemented in this slice; one of "
            f"{list(REGRID_METHODS)} (03 section 3, 'Three methods')")
    adopting = regrid_method == "adopt"
    md = None if adopting else resolve_max_distance(tile.grid, max_distance)
    inputs = glt_inputs(granule_id, tile.grid, md, regrid_method, REGRID_ALGO_VERSION,
                        source=source_id)
    key = cache.key("glt", tile.grid.id, tile, inputs)
    if cache.hit(key):
        return key
    tags: dict[str, Any] = {"regrid_algo_version": REGRID_ALGO_VERSION,
                            "regrid_method": regrid_method}
    if adopting:
        if embedded is None:
            raise ValueError("regrid_method 'adopt' needs the product's own GLT; no `embedded` "
                             "callable was given (03 section 3)")
        glt = adopt_glt(embedded(), tile)
        tags["glt_source"] = source_id or "unknown"
    else:
        glt = build_glt(loc(), tile, max_distance=md, n_workers=n_workers)
        tags["max_distance"] = md
    cache.write_file(key, lambda p: write_glt(p, glt, tile, granule_id=granule_id, tags=tags))
    return key


# ------------------------------------------------------------------------- the recorded hash
def module_content_hash() -> str:
    """sha256 over every `.py` in this package, in name order (06 section 3, rule 2)."""
    h = hashlib.sha256()
    for f in sorted(Path(__file__).parent.glob("*.py")):
        h.update(f.name.encode() + b"\0" + f.read_bytes() + b"\0")
    return "sha256:" + h.hexdigest()


def recorded_hash() -> tuple[int, str] | None:
    """`(version, hash)` from `ALGO_HASH`, or None when the file is missing."""
    if not ALGO_HASH_PATH.is_file():
        return None
    version, digest = ALGO_HASH_PATH.read_text().split()
    return int(version), digest


def record_hash() -> tuple[int, str]:
    """Rewrite `ALGO_HASH` for the current module and version."""
    entry = (REGRID_ALGO_VERSION, module_content_hash())
    ALGO_HASH_PATH.write_text(f"{entry[0]} {entry[1]}\n")
    return entry


def check_recorded_hash() -> tuple[bool, str]:
    """(ok, message). ok only when `ALGO_HASH` matches both the current version and the current
    content hash; the message says which step of the workflow was skipped."""
    current = (REGRID_ALGO_VERSION, module_content_hash())
    recorded = recorded_hash()
    how = "run `pixi run python -m stratum.regrid --record`"
    if recorded is None:
        return False, f"ALGO_HASH is missing; {how}"
    if recorded == current:
        return True, f"ALGO_HASH matches version {current[0]}"
    if recorded[0] == current[0]:
        return False, (f"stratum.regrid changed but REGRID_ALGO_VERSION is still {current[0]}: "
                       f"bump it if GLT output can differ, then {how} (06 section 3, rule 2)")
    return False, (f"REGRID_ALGO_VERSION is {current[0]} but ALGO_HASH records {recorded[0]}; "
                   f"{how}")


__all__ = [
    "GLT_BAND_NAMES", "LATTICE_TOLERANCE", "MAX_DISTANCE_FACTOR", "REGRID_ALGO_VERSION",
    "REGRID_METHODS", "LatticeMismatch", "adopt_glt", "build_glt", "build_glt_raw",
    "cell_centres", "check_recorded_hash", "clean_contiguous", "internal_tile_size",
    "lattice_offset", "module_content_hash", "read_glt", "record_hash", "recorded_hash",
    "regrid_granule_tile", "resolve_max_distance", "write_glt",
]
