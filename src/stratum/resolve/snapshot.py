"""Snapshot IO (13, 11 section 8; first-slice plan section 4).

A snapshot is a DIRECTORY of single-band GeoTIFFs - `{layer}.tif` per schema layer plus the two
the framework always adds, `score.tif` and `valid.tif` - because layers have distinct dtypes.
Tiled and deflate-compressed like the GLTs so a later read is a window read. Nodata on disk:
categorical uint16 65535 (0 is the `none` class), continuous / score float32 NaN, valid uint8
with no nodata (0 is a real answer: nothing occupied the cell).
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import numpy as np
import rasterio
from affine import Affine
from rasterio.windows import Window as RioWindow

from stratum.regrid import internal_tile_size
from stratum.types import Epoch, LayerSpec, SnapshotSchema, SnapshotStack, Window

CATEGORICAL_DTYPE = "uint16"
CATEGORICAL_NODATA = 65535
CONTINUOUS_DTYPE = "float32"
SCORE_NAME = "score"
VALID_NAME = "valid"
RESERVED = (SCORE_NAME, VALID_NAME)


def layer_dtype(layer: LayerSpec) -> np.dtype:
    """What a layer is stored as: categorical uint16, continuous float32 (plan section 4)."""
    return np.dtype(CATEGORICAL_DTYPE if layer.kind == "categorical" else CONTINUOUS_DTYPE)


def layer_nodata(layer: LayerSpec) -> float | int:
    return CATEGORICAL_NODATA if layer.kind == "categorical" else np.nan


def empty_layer(layer: LayerSpec, shape: tuple[int, int], bands: int = 1) -> np.ndarray:
    """A layer no observation has won yet: all nodata."""
    full = shape if bands == 1 else (*shape, bands)
    return np.full(full, layer_nodata(layer), dtype=layer_dtype(layer))


def _write_band_file(path: Path, arr: np.ndarray, *, transform: Affine, crs: str,
                     nodata: float | None, tags: Mapping[str, str]) -> None:
    arr = np.asarray(arr)
    if arr.ndim == 2:
        planes = arr[None]
    elif arr.ndim == 3:
        planes = np.moveaxis(arr, -1, 0)
    else:
        raise ValueError(f"{path.name}: expected (H, W) or (H, W, B); got {arr.shape}")
    count, rows, cols = planes.shape
    bs = internal_tile_size(max(rows, cols))
    profile = {"driver": "GTiff", "height": rows, "width": cols, "count": count,
               "dtype": planes.dtype.name, "crs": crs, "transform": transform, "tiled": True,
               "blockxsize": bs, "blockysize": bs, "compress": "deflate"}
    if nodata is not None:
        profile["nodata"] = nodata
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(planes)
        dst.update_tags(**tags)


def write_snapshot(directory: Path, *, layers: Mapping[str, np.ndarray], score: np.ndarray,
                   valid: np.ndarray, schema: SnapshotSchema, transform: Affine,
                   crs: str) -> Path:
    """Write one block's snapshot: exactly the schema's layers plus `score` and `valid`
    (04 section 4, 11 section 8). Arrays are (H, W) or (H, W, B) over the block's CORE window;
    `transform` is that window's. Layers are cast to their storage dtype."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    names = [layer.name for layer in schema.layers]
    extra = set(layers) - set(names)
    missing = set(names) - set(layers)
    if extra or missing:
        raise ValueError(f"layers {sorted(layers)} do not match schema {sorted(names)}: "
                         f"extra {sorted(extra)}, missing {sorted(missing)}")
    shape = np.asarray(valid).shape
    if len(shape) != 2 or np.asarray(score).shape != shape:
        raise ValueError(f"valid {shape} and score {np.asarray(score).shape} must be (H, W)")
    common = {"schema": schema.name, "layers_hash": schema.layers_hash}
    for layer in schema.layers:
        arr = np.asarray(layers[layer.name])
        if arr.shape[:2] != shape:
            raise ValueError(f"layer {layer.name!r} shape {arr.shape} != {shape}")
        _write_band_file(directory / f"{layer.name}.tif", arr.astype(layer_dtype(layer)),
                         transform=transform, crs=crs, nodata=layer_nodata(layer),
                         tags={**common, "layer": layer.name, "kind": layer.kind,
                               "source": layer.source})
    _write_band_file(directory / f"{SCORE_NAME}.tif", np.asarray(score, dtype=np.float32),
                     transform=transform, crs=crs, nodata=np.nan,
                     tags={**common, "layer": SCORE_NAME})
    _write_band_file(directory / f"{VALID_NAME}.tif", np.asarray(valid, dtype=bool).astype(np.uint8),
                     transform=transform, crs=crs, nodata=None,
                     tags={**common, "layer": VALID_NAME})
    return directory


def _read_band_file(path: Path, window: Window | None) -> np.ndarray:
    with rasterio.open(path) as src:
        if window is None:
            data = src.read()
        else:
            data = src.read(window=RioWindow(window.col_off, window.row_off, window.width,
                                             window.height))
    return data[0] if data.shape[0] == 1 else np.moveaxis(data, 0, -1)


def read_snapshot(directory: Path, window: Window | None = None) -> dict[str, np.ndarray]:
    """{name: array} for every `.tif` in a snapshot directory: each schema layer, `score`
    (float32, NaN where nothing won) and `valid` (bool). Layers come back in their storage
    dtype with nodata in place (uint16 65535 / NaN) - `valid` says which cells were won.
    `window` is relative to the snapshot's own (core) extent."""
    directory = Path(directory)
    files = sorted(directory.glob("*.tif"))
    if not files:
        raise FileNotFoundError(f"no snapshot layers under {directory}")
    out = {f.stem: _read_band_file(f, window) for f in files}
    if VALID_NAME in out:
        out[VALID_NAME] = out[VALID_NAME].astype(bool)
    return out


def stack_snapshots(dirs: Sequence[Path | None], epochs: Sequence[Epoch], schema: SnapshotSchema,
                    window: Window | None = None) -> SnapshotStack:
    """The Reducer input (11 section 8): every epoch's snapshot over one window, epoch-major
    and in ascending epoch order whatever order `dirs`/`epochs` arrived in. A None entry is an
    epoch that produced no snapshot: all-invalid, all-nodata. Every layer becomes
    (n_epochs, H, W[, B]); `valid` (n_epochs, H, W) bool; `score` (n_epochs, H, W) float32."""
    if len(dirs) != len(epochs):
        raise ValueError(f"{len(dirs)} snapshot dirs for {len(epochs)} epochs")
    order = sorted(range(len(epochs)), key=lambda i: epochs[i].start)
    snaps = [read_snapshot(dirs[i], window) if dirs[i] is not None else None for i in order]
    present = [s for s in snaps if s is not None]
    if not present:
        raise ValueError("stack_snapshots needs at least one snapshot")
    shape = present[0][VALID_NAME].shape
    for s in present:
        if s[VALID_NAME].shape != shape:
            raise ValueError(f"snapshots disagree on shape: {shape} vs {s[VALID_NAME].shape}")
    names = [layer.name for layer in schema.layers]
    for s in present:
        missing = [n for n in [*names, *RESERVED] if n not in s]
        if missing:
            raise KeyError(f"snapshot lacks {missing}; written under another schema? "
                           "(11 section 8: one schema per run)")

    def plane(s: dict[str, np.ndarray] | None, layer: LayerSpec) -> np.ndarray:
        if s is not None:
            return s[layer.name]
        template = next(p[layer.name] for p in present)
        bands = template.shape[2] if template.ndim == 3 else 1
        return empty_layer(layer, shape, bands)

    layers = {layer.name: np.stack([plane(s, layer) for s in snaps]) for layer in schema.layers}
    valid = np.stack([s[VALID_NAME] if s is not None else np.zeros(shape, dtype=bool)
                      for s in snaps])
    score = np.stack([s[SCORE_NAME] if s is not None else np.full(shape, np.nan, dtype=np.float32)
                      for s in snaps]).astype(np.float32, copy=False)
    return SnapshotStack(schema=schema, epochs=[epochs[i] for i in order], layers=layers,
                         valid=valid, score=score)


__all__ = [
    "CATEGORICAL_DTYPE", "CATEGORICAL_NODATA", "CONTINUOUS_DTYPE", "RESERVED", "SCORE_NAME",
    "VALID_NAME", "empty_layer", "layer_dtype", "layer_nodata", "read_snapshot",
    "stack_snapshots", "write_snapshot",
]
