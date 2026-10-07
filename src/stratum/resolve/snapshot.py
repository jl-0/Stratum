"""Snapshot IO (13, 11 section 8).

A snapshot is a DIRECTORY of single-band GeoTIFFs - `{layer}.tif` per schema layer plus the three
the framework always adds, `score.tif`, `valid.tif` and `observed.tif` - because layers have
distinct dtypes. Tiled and deflate-compressed like the GLTs so a later read is a window read.
Nodata on disk: categorical uint16 65535 (0 is the `none` class), continuous / score float32 NaN,
valid and observed uint8 with no nodata (0 is a real answer).

`valid` is where an observation won the cell: it reached it, passed every mask and scored.
`observed` is where any observation REACHED it, before any pixel mask or the scorer - the
footprint of the granules that passed the granule filters. Their difference is what the masks
and the scorer removed, which `valid` alone cannot tell apart from never having been seen.
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
OBSERVED_NAME = "observed"
RESERVED = (SCORE_NAME, VALID_NAME, OBSERVED_NAME)


def layer_dtype(layer: LayerSpec) -> np.dtype:
    """What a layer is stored as: categorical uint16, continuous float32 (11 section 2) -
    except a float64 layer (the `acquired` source), which float32 would round by minutes."""
    if layer.kind == "categorical":
        return np.dtype(CATEGORICAL_DTYPE)
    return np.dtype("float64" if layer.dtype == "float64" else CONTINUOUS_DTYPE)


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
                   crs: str, observed: np.ndarray | None = None) -> Path:
    """Write one block's snapshot: exactly the schema's layers plus `score`, `valid` and
    `observed` (04 section 4, 11 section 8). Arrays are (H, W) or (H, W, B) over the block's
    CORE window; `transform` is that window's. Layers are cast to their storage dtype.

    `observed` defaults to `valid`: a caller that does not know the footprint can vouch only for
    the cells something won. Resolve always passes the real one. Every valid cell must be
    observed - winning a cell nothing reached is a bug, and is refused."""
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
    seen = np.asarray(valid if observed is None else observed, dtype=bool)
    if seen.shape != shape:
        raise ValueError(f"observed {seen.shape} must match valid {shape}")
    if (np.asarray(valid, dtype=bool) & ~seen).any():
        raise ValueError("a cell is valid but not observed: an observation cannot win a cell it "
                         "never reached")
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
    _write_band_file(directory / f"{OBSERVED_NAME}.tif", seen.astype(np.uint8),
                     transform=transform, crs=crs, nodata=None,
                     tags={**common, "layer": OBSERVED_NAME})
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
    (float32, NaN where nothing won), `valid` and `observed` (bool). Layers come back in their storage
    dtype with nodata in place (uint16 65535 / NaN) - `valid` says which cells were won.
    `window` is relative to the snapshot's own (core) extent."""
    directory = Path(directory)
    files = sorted(directory.glob("*.tif"))
    if not files:
        raise FileNotFoundError(f"no snapshot layers under {directory}")
    out = {f.stem: _read_band_file(f, window) for f in files}
    for name in (VALID_NAME, OBSERVED_NAME):
        if name in out:
            out[name] = out[name].astype(bool)
    return out


def stack_snapshots(dirs: Sequence[Path | None], epochs: Sequence[Epoch], schema: SnapshotSchema,
                    window: Window | None = None) -> SnapshotStack:
    """The Reducer input (11 section 8): every epoch's snapshot over one window, epoch-major
    and in ascending epoch order whatever order `dirs`/`epochs` arrived in. A None entry is an
    epoch that produced no snapshot: all-invalid, all-nodata, unobserved. Every layer becomes
    (n_epochs, H, W[, B]); `valid` and `observed` (n_epochs, H, W) bool; `score`
    (n_epochs, H, W) float32."""
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

    def width(layer: LayerSpec) -> int:
        """The layer's band count across the epochs that have one: the widest.

        An epoch in which NO observation won a multi-band layer can write a narrower plane
        (`bands: None` makes the width unknowable until something is read), and every value in
        it is nodata by definition. Widening that is lossless; widening a plane that actually
        holds data would not be, so `plane` refuses it.
        """
        return max((p[layer.name].shape[2] if p[layer.name].ndim == 3 else 1) for p in present)

    def plane(s: dict[str, np.ndarray] | None, layer: LayerSpec) -> np.ndarray:
        want = width(layer)
        if s is None:
            return empty_layer(layer, shape, want)
        arr = s[layer.name]
        have = arr.shape[2] if arr.ndim == 3 else 1
        if have == want:
            return arr
        fill = empty_layer(layer, shape, 1)
        if not np.array_equal(arr.reshape(shape + (have,)), np.broadcast_to(
                fill.reshape(shape + (1,)), shape + (have,)), equal_nan=True):
            raise ValueError(
                f"layer {layer.name!r}: an epoch carries {have} band(s) where others carry "
                f"{want}, and it is not all-nodata, so it cannot be widened. Snapshots were "
                "written under different schemas (11 section 8: one schema per run)")
        return empty_layer(layer, shape, want)

    layers = {layer.name: np.stack([plane(s, layer) for s in snaps]) for layer in schema.layers}
    valid = np.stack([s[VALID_NAME] if s is not None else np.zeros(shape, dtype=bool)
                      for s in snaps])
    score = np.stack([s[SCORE_NAME] if s is not None else np.full(shape, np.nan, dtype=np.float32)
                      for s in snaps]).astype(np.float32, copy=False)
    observed = np.stack([s[OBSERVED_NAME] if s is not None else np.zeros(shape, dtype=bool)
                         for s in snaps])
    return SnapshotStack(schema=schema, epochs=[epochs[i] for i in order], layers=layers,
                         valid=valid, score=score, observed=observed)


__all__ = [
    "CATEGORICAL_DTYPE", "CATEGORICAL_NODATA", "CONTINUOUS_DTYPE", "OBSERVED_NAME", "RESERVED",
    "SCORE_NAME", "VALID_NAME", "empty_layer", "layer_dtype", "layer_nodata", "read_snapshot",
    "stack_snapshots", "write_snapshot",
]
