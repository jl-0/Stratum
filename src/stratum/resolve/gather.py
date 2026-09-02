"""The gather: the only place sensor space and block space meet (12 section 2, step 14).

One-based GLT indices and the negative-means-interpolated convention are inherited so Stratum
GLTs interoperate with existing artifacts (03 section 2). Both are easy to get wrong once and
never notice, which is why this lives in the framework and not in plugin code.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from stratum.types import SensorWindow


@dataclass(frozen=True)
class Gathered:
    """One sensor variable pulled onto the block grid through a GLT window.

    `band` is (H, W) or (H, W, B) in the variable's own dtype, holding the sensor value wherever
    the GLT hit and 0 elsewhere - read it only under `valid`. `valid` is (H, W) bool: hit, not
    fill (for a multi-band variable: no band is fill), and admitted by the sensor mask.
    `interpolated` is (H, W) bool: the GLT reached beyond max_distance (03 section 2); such
    cells are valid unless a mask or scorer says otherwise (12 section 2)."""

    band: np.ndarray
    valid: np.ndarray
    interpolated: np.ndarray


def gather(arr: np.ma.MaskedArray, glt_win: np.ndarray, sw: SensorWindow,
           ok: np.ndarray | None) -> Gathered:
    """12 section 2 step 14, exactly:

        hit = fi != 0; interpolated = (gy < 0) | (gx < 0)
        r = |gy| - 1 - sw.row0;  c = |gx| - 1 - sw.col0          (GLT indices are 1-BASED)
        band = arr.data[r, c] where hit;  valid = hit & ~arr.mask[r, c] & ok[r, c]

    `arr` is the sensor window `sw` of one variable as the reader returned it (fill masked,
    11 section 2); `glt_win` is the (H, W, 3) int32 GLT over the block window; `ok` is the
    conjunction of the sensor-space masks over `sw`, or None for no mask. A GLT index outside
    `sw` is a caller error (the window did not cover the GLT) and raises IndexError."""
    glt_win = np.asarray(glt_win)
    if glt_win.ndim != 3 or glt_win.shape[-1] != 3:
        raise ValueError(f"glt_win must be (H, W, 3); got {glt_win.shape}")
    gx, gy, fi = glt_win[..., 0], glt_win[..., 1], glt_win[..., 2]
    hit = fi != 0
    interpolated = ((gy < 0) | (gx < 0)) & hit

    data = np.ma.getdata(arr)
    mask = np.ma.getmaskarray(arr)
    if mask.ndim == 3:                       # a multi-band variable: any band fill -> fill
        mask = mask.any(axis=-1)
    rest = data.shape[2:]
    band = np.zeros(hit.shape + rest, dtype=data.dtype)
    valid = np.zeros(hit.shape, dtype=bool)
    if not hit.any():
        return Gathered(band=band, valid=valid, interpolated=interpolated)

    r = np.abs(gy) - 1 - sw.row0
    c = np.abs(gx) - 1 - sw.col0
    rh, ch = r[hit], c[hit]
    if (rh < 0).any() or (ch < 0).any() or (rh >= sw.height).any() or (ch >= sw.width).any():
        raise IndexError(f"GLT indices fall outside the sensor window {sw}; "
                         "SensorWindow.covering was not used on this GLT window (12 section 2)")
    band[hit] = data[rh, ch]
    ok_hit = ~mask[rh, ch]
    if ok is not None:
        ok = np.asarray(ok, dtype=bool)
        if ok.shape != (sw.height, sw.width):
            raise ValueError(f"sensor mask shape {ok.shape} != window shape "
                             f"{(sw.height, sw.width)}")
        ok_hit &= ok[rh, ch]
    valid[hit] = ok_hit
    return Gathered(band=band, valid=valid, interpolated=interpolated)


__all__ = ["Gathered", "gather"]
