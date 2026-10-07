"""A rough order of magnitude for a run: compute, dollars, downloads and disk (08 section 2).

The planner knows every work item when it finishes, and what each one touches: how much of a
tile a granule covers (regrid), how many candidate granules a block has in an epoch (resolve),
how many cells and bytes per cell every layer carries (resolve, reduce, publish). Each stage's
time is modelled as cells x bytes x a constant, and the constants are FITTED to one cold,
fully-recorded run - `examples/emit-cmr-nevada`, run `nevada-regrid-cost-probe-20260917-d095b352`,
33 granules on one one-degree tile at one arcsecond, `block_size: 720`, three layers (uint16,
float32, float32 - 10 bytes a cell), on an Apple M-series core:

    regrid   6.56 s + 23.44 s x (fraction of the tile the granule's bbox covers)   r = 0.78
    resolve  0.074 s + 0.080 s per candidate granule                              r = 0.92
    reduce   8.65 s per block, flat in its epoch count                            r = -0.17
    publish  3.87 s per tile

It is an ESTIMATE and says so: the range is x0.5 to x2, and a run whose cost functions differ
from the calibration run - aux rasters, an ortho role, masks, more layers than bytes capture -
can sit at the top of it or past it. It exists so a run says what it will cost before it costs it,
and so `budget.max_vcpu_hours` can be checked at all.
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

#: Where the constants came from, carried into the report so a stale calibration is visible.
CALIBRATION = ("examples/emit-cmr-nevada, run nevada-regrid-cost-probe-20260917-d095b352: "
               "33 granules, 1 tile, 1 arcsec, block_size 720, 3 layers, Apple M-series core")
_CAL_TILE_CELLS = 3600 * 3600
_CAL_BLOCK_CELLS = 720 * 720
_CAL_BYTES_PER_CELL = 10

REGRID_FIXED_S = 6.56                                   # KD-tree build over the granule
REGRID_S_PER_CELL = 23.44 / _CAL_TILE_CELLS             # query, per tile cell the bbox covers
RESOLVE_FIXED_S = 0.074
RESOLVE_S_PER_CELL_BYTE = 0.080 / (_CAL_BLOCK_CELLS * _CAL_BYTES_PER_CELL)   # x candidates
REDUCE_S_PER_CELL_BYTE = 8.65 / (_CAL_BLOCK_CELLS * _CAL_BYTES_PER_CELL)
PUBLISH_S_PER_CELL_BYTE = 3.87 / (_CAL_TILE_CELLS * _CAL_BYTES_PER_CELL)

#: Bytes written per item, measured in the same run's cache (`du`, 2026-10-07).
GLT_BYTES_PER_COVERED_CELL = 1_561_600 / (0.502 * _CAL_TILE_CELLS)   # 1.5 MB at 50 % cover
SNAPSHOT_BYTES_PER_CELL_BYTE = 964_600 / (_CAL_BLOCK_CELLS * _CAL_BYTES_PER_CELL)
PRODUCT_BLOCK_BYTES_PER_CELL_BYTE = 690_200 / (_CAL_BLOCK_CELLS * _CAL_BYTES_PER_CELL)
PRODUCTS_BYTES_PER_CELL_BYTE = 20_200_000 / (_CAL_TILE_CELLS * _CAL_BYTES_PER_CELL)
#: An ortho role's warp per (granule, tile): FRCOV, 215 GB over 9,233 items in the US West
#: README's storage estimate - an estimate, not a measurement.
ORTHO_WARP_BYTES = 23_000_000

ROM_LOW, ROM_HIGH = 0.5, 2.0
#: A Graviton2 Lambda core against the calibration core: "roughly 2-2.5x slower"
#: (examples/emit-us-west-2025/README.md, "The bill").
LAMBDA_SLOWDOWN = 2.25
#: arm64 Lambda in us-west-2, per GB-second and per request (published price, 2026-09).
LAMBDA_USD_PER_GB_S = 0.0000133334
LAMBDA_USD_PER_REQUEST = 0.20 / 1_000_000
#: The deployment's default (terraform/variables.tf `memory_mb`); preflight reads the real one.
DEFAULT_LAMBDA_MEMORY_MB = 4096

_DTYPE_BYTES = {"bool": 1, "uint8": 1, "int8": 1, "uint16": 2, "int16": 2, "uint32": 4,
                "int32": 4, "float32": 4, "uint64": 8, "int64": 8, "float64": 8}


def layer_bytes_per_cell(layers: Sequence[Any], band_counts: Mapping[str, int]) -> int:
    """What one cell of the snapshot costs: each layer's dtype size times its band count."""
    total = 0
    for layer in layers:
        size = _DTYPE_BYTES.get(str(layer.dtype or "float32"), 4)
        total += size * int(band_counts.get(layer.name, 1))
    return max(total, 1)


def bbox_overlap(bbox: Sequence[float], tile: Sequence[float]) -> float:
    """The fraction of `tile` (lon/lat bounds) that `bbox` covers."""
    w, s, e, n = tile
    gw, gs, ge, gn = bbox
    area = (e - w) * (n - s)
    return 0.0 if area <= 0 else (max(0.0, min(e, ge) - max(w, gw))
                                  * max(0.0, min(n, gn) - max(s, gs)) / area)


def estimate(*, tile_cells: int, block_cells: int, bytes_per_cell: int,
             regrid_overlaps: Sequence[float], resolve_candidates: Sequence[int],
             reduce_items: int, publish_items: int, ortho_roles: int = 0,
             download_bytes: int | None = None) -> dict[str, Any]:
    """The ROM for one run's work lists. Seconds are on the calibration core; `lambda_usd` is at
    the deployment's default memory and is re-priced by preflight for the real function."""
    ov = np.asarray(regrid_overlaps, dtype=float)
    cand = np.asarray(resolve_candidates, dtype=float)
    per_item = {
        "regrid": REGRID_FIXED_S + REGRID_S_PER_CELL * tile_cells * ov,
        "resolve": RESOLVE_FIXED_S + RESOLVE_S_PER_CELL_BYTE * block_cells * bytes_per_cell * cand,
        "reduce": np.full(reduce_items, REDUCE_S_PER_CELL_BYTE * block_cells * bytes_per_cell),
        "publish": np.full(publish_items, PUBLISH_S_PER_CELL_BYTE * tile_cells * bytes_per_cell),
    }
    seconds = {s: float(v.sum()) for s, v in per_item.items()}
    longest = {s: float(v.max()) if v.size else 0.0 for s, v in per_item.items()}
    items = {s: int(v.size) for s, v in per_item.items()}
    cpu_h = sum(seconds.values()) / 3600
    footprint = {
        "glt": float(GLT_BYTES_PER_COVERED_CELL * tile_cells * ov.sum()),
        "ortho_warps": float(ORTHO_WARP_BYTES * ortho_roles * ov.size),
        "snapshots": float(SNAPSHOT_BYTES_PER_CELL_BYTE * block_cells * bytes_per_cell
                           * cand.size),
        "product_blocks": float(PRODUCT_BLOCK_BYTES_PER_CELL_BYTE * block_cells * bytes_per_cell
                                * reduce_items),
        "products": float(PRODUCTS_BYTES_PER_CELL_BYTE * tile_cells * bytes_per_cell
                          * publish_items),
    }
    return {
        "calibration": CALIBRATION,
        "range_factor": [ROM_LOW, ROM_HIGH],
        "bytes_per_cell": bytes_per_cell,
        "items": items,
        "cpu_seconds": seconds,
        "longest_item_seconds": longest,
        "cpu_hours": cpu_h,
        "lambda": lambda_cost(cpu_h, sum(items.values()), DEFAULT_LAMBDA_MEMORY_MB),
        "download_bytes": download_bytes,
        "footprint_bytes": {k: int(v) for k, v in footprint.items()},
    }


def lambda_cost(cpu_hours: float, invocations: int, memory_mb: int) -> dict[str, Any]:
    """Lambda's bill for `cpu_hours` of calibration-core work: slower cores, billed by GB-second
    at the function's memory, plus requests. Low and high carry the ROM factors."""
    gb_s = cpu_hours * 3600 * LAMBDA_SLOWDOWN * memory_mb / 1024
    usd = gb_s * LAMBDA_USD_PER_GB_S + invocations * LAMBDA_USD_PER_REQUEST
    return {"memory_mb": int(memory_mb), "gb_seconds": gb_s,
            "usd": [usd * ROM_LOW, usd, usd * ROM_HIGH]}


def fmt_bytes(n: float | None) -> str:
    if n is None:
        return "unknown"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} TB"


def fmt_hours(h: float) -> str:
    if h < 1 / 60:
        return f"{h * 3600:.0f} s"
    if h < 1:
        return f"{h * 60:.0f} min"
    return f"{h:.1f} h" if h < 100 else f"{math.floor(h):,} h"


__all__ = [
    "CALIBRATION", "DEFAULT_LAMBDA_MEMORY_MB", "LAMBDA_SLOWDOWN", "ROM_HIGH", "ROM_LOW",
    "bbox_overlap", "estimate", "fmt_bytes", "fmt_hours", "lambda_cost", "layer_bytes_per_cell",
]
