"""The arithmetic behind `matched_filter`, as pure functions over arrays (04 section 4).

Three steps, each testable alone:

1. **Continuum removal** (`continuum_removed`): divide each spectrum by the straight line between
   two shoulder intervals, as Tetracorder does for a feature. A per-pixel convex hull would follow
   every spectrum more closely but does not vectorise, and the shoulders are what fix the
   continuum in the library's own diagnostics anyway.
2. **Local background statistics on a fixed lattice** (`window_stats`): one mean and covariance
   per window centred at every multiple of `stride` cells of the GRID, gathered from the cells
   within `stride` of the centre. Anchoring to the grid rather than to the block is what keeps
   the answer independent of `block_size` (01 section 3 invariant).
3. **Blended matched filter** (`blended_mf`): every cell takes the MF score under each of the
   four windows covering it, weighted by a tent `(1 - |dr|/S)(1 - |dc|/S)`. The four weights sum
   to one, so the background model changes smoothly from window to window and there is no seam
   anywhere a window ends. A window with too few cells is dropped and the remaining weights are
   renormalised, which keeps partial coverage smooth too.

The MF is `sᵀΣ⁻¹(x−μ) / sᵀΣ⁻¹s` with `s = t − μ`: about 0 over background, about 1 for a cell that
is all target. On continuum-removed spectra it is the linear-mixing abundance of the target's
share of the cell's CONTINUUM, not of its area: a fraction `a` of a target with continuum `Ct`
in a background of continuum `Cb` reads `a·Ct / (a·Ct + (1−a)·Cb)`. Bright topaz in darker
volcanic rock therefore reads above its areal fraction.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

#: Tetracorder 6.00a `topaz` feature f1a (cmd.lib.setup.t6.00a6): continuum shoulders around the
#: 2.08 um band, in nm. `band_depth` uses it for the statistics-free cross-check.
F208_SHOULDERS = ((2030.0, 2060.0), (2120.0, 2150.0))
F208_CENTRE = (2075.0, 2095.0)


def band_index(wavelengths: np.ndarray, lo: float, hi: float) -> np.ndarray:
    """Indices of the bands with lo <= wavelength <= hi."""
    return np.flatnonzero((wavelengths >= lo) & (wavelengths <= hi))


def continuum_removed(x: np.ndarray, wavelengths: np.ndarray,
                      shoulders: Sequence[tuple[float, float]]) -> np.ndarray:
    """`x / continuum` over the last axis, the continuum the line through the means of the two
    shoulder intervals (`shoulders` in the units of `wavelengths`). Cells whose continuum is not
    positive come back NaN."""
    (a0, a1), (b0, b1) = shoulders
    ia, ib = band_index(wavelengths, a0, a1), band_index(wavelengths, b0, b1)
    if not ia.size or not ib.size:
        raise ValueError(f"no band in a continuum shoulder {shoulders} of "
                         f"{wavelengths.min():.0f}-{wavelengths.max():.0f}")
    wa, wb = float(wavelengths[ia].mean()), float(wavelengths[ib].mean())
    ya, yb = x[..., ia].mean(axis=-1), x[..., ib].mean(axis=-1)
    slope = (yb - ya) / (wb - wa)
    cont = ya[..., None] + slope[..., None] * (wavelengths - wa)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = x / cont
    out[~(cont > 0)] = np.nan
    return out


def band_depth(x: np.ndarray, wavelengths: np.ndarray,
               shoulders: Sequence[tuple[float, float]] = F208_SHOULDERS,
               centre: tuple[float, float] = F208_CENTRE) -> np.ndarray:
    """1 − (continuum-removed reflectance averaged over `centre`): the depth of one feature,
    against its own local continuum, with no statistics at all."""
    keep = np.concatenate([band_index(wavelengths, lo, hi) for lo, hi in (*shoulders, centre)])
    keep = np.unique(keep)
    wl = wavelengths[keep]
    cr = continuum_removed(x[..., keep], wl, shoulders)
    return 1.0 - cr[..., band_index(wl, *centre)].mean(axis=-1)


def noise_sigma(x: np.ndarray) -> np.ndarray:
    """Per-band noise standard deviation from the spectral second difference of (n, B) spectra.
    For white noise `x_b − (x_{b−1} + x_{b+1})/2` has variance 1.5 σ², and the smooth signal
    cancels. Spatial differences would read zero between the duplicated cells a nearest-neighbour
    gather produces, so this stays in the spectral domain. Edge bands take their neighbour's."""
    d = x[:, 1:-1] - 0.5 * (x[:, :-2] + x[:, 2:])
    s = np.sqrt(np.var(d, axis=0) / 1.5)
    s = np.concatenate([s[:1], s, s[-1:]])
    floor = max(float(np.median(s)) * 1e-3, 1e-12)
    return np.maximum(s, floor)


@dataclass(frozen=True)
class Background:
    """One window's background model, reduced to what the MF needs: the mean, and the projection
    `w` such that `mf(x) = w · (x − μ)`. `n` is the number of cells it was estimated from."""

    mu: np.ndarray
    w: np.ndarray
    n: int


def background(x: np.ndarray, target: np.ndarray, *, shrink: float,
               k: int | None) -> Background | None:
    """The MF filter for (n, B) background spectra `x`. `shrink` pulls Σ toward its diagonal.
    `k` keeps only the top-k MNF components (noise-whitened PCA, ordered by SNR); None keeps
    them all, where the MF already weights every direction by how much the background varies
    along it. None when the target is indistinguishable from the mean."""
    n, b = x.shape
    mu = x.mean(axis=0)
    xc = x - mu
    s = target - mu
    cov = (xc.T @ xc) / max(n - 1, 1)
    cov = (1.0 - shrink) * cov + shrink * np.diag(np.diag(cov))
    if k is None or k >= b:
        # tiny ridge so a degenerate window (constant bands) still inverts
        cov = cov + np.eye(b) * (np.trace(cov) / b * 1e-9 + 1e-20)
        try:
            ci_s = np.linalg.solve(cov, s)
        except np.linalg.LinAlgError:
            return None
    else:
        sig = noise_sigma(x)
        cz = cov / np.outer(sig, sig)
        lam, vec = np.linalg.eigh(cz)
        order = np.argsort(lam)[::-1][:k]
        lam, vec = np.maximum(lam[order], 1e-12), vec[:, order]
        # Σ⁻¹ restricted to the kept subspace, mapped back to band space
        sz = vec.T @ (s / sig)
        ci_s = (vec @ (sz / lam)) / sig
    denom = float(s @ ci_s)
    if not np.isfinite(denom) or denom <= 0:
        return None
    return Background(mu=mu.astype(np.float64), w=(ci_s / denom).astype(np.float64), n=n)


def window_stats(x: np.ndarray, valid: np.ndarray, target: np.ndarray, *, origin: tuple[int, int],
                 stride: int, centres: tuple[range, range], shrink: float, k: int | None,
                 min_n: int, trim: float) -> dict[tuple[int, int], Background]:
    """A `Background` per lattice window. `x` (H, W, B) and `valid` (H, W) cover a block's halo
    window whose top-left cell sits at GLOBAL lattice (row, col) `origin`. `centres` lists the
    global row and column multiples of `stride` to compute; a window gathers the valid cells
    with |dr| < stride and |dc| < stride. `trim` > 0 re-estimates after dropping that fraction of
    the window's highest-MF cells, so target-rich ground does not inflate its own background."""
    h, w = valid.shape
    r0, c0 = origin
    out: dict[tuple[int, int], Background] = {}
    for kr in centres[0]:
        cr = kr * stride - r0
        rs = slice(max(cr - stride + 1, 0), min(cr + stride, h))
        if rs.start >= rs.stop:
            continue
        for kc in centres[1]:
            cc = kc * stride - c0
            cs = slice(max(cc - stride + 1, 0), min(cc + stride, w))
            if cs.start >= cs.stop:
                continue
            ok = valid[rs, cs]
            if int(ok.sum()) < min_n:
                continue
            pix = x[rs, cs][ok].astype(np.float64)
            bg = background(pix, target, shrink=shrink, k=k)
            if bg is not None and trim > 0:
                score = (pix - bg.mu) @ bg.w
                keep = score <= np.quantile(score, 1.0 - trim)
                if int(keep.sum()) >= min_n:
                    bg = background(pix[keep], target, shrink=shrink, k=k) or bg
            if bg is not None:
                out[(kr, kc)] = bg
    return out


def needed_centres(origin: tuple[int, int], core: tuple[slice, slice],
                   stride: int) -> tuple[range, range]:
    """The global lattice multiples whose tent reaches a core cell: every k with
    |k·stride − cell| < stride for some cell of the core."""
    out = []
    for o, sl in zip(origin, core, strict=True):
        g0, g1 = o + sl.start, o + sl.stop - 1          # first and last core cell, global
        out.append(range(g0 // stride, g1 // stride + 2))
    return out[0], out[1]


def blended_mf(x: np.ndarray, valid: np.ndarray, stats: dict[tuple[int, int], Background], *,
               origin: tuple[int, int], core: tuple[slice, slice],
               stride: int) -> tuple[np.ndarray, np.ndarray]:
    """(mf, bg_n) over the CORE of the window, NaN outside it and where no window applies.
    `bg_n` is the tent-weighted number of background cells behind each cell's score."""
    h, w = valid.shape
    num = np.zeros((h, w), np.float64)
    den = np.zeros((h, w), np.float64)
    nbg = np.zeros((h, w), np.float64)
    r0, c0 = origin
    rows = np.arange(core[0].start, core[0].stop)
    cols = np.arange(core[1].start, core[1].stop)
    for (kr, kc), bg in stats.items():
        cr, cc = kr * stride - r0, kc * stride - c0
        wr = 1.0 - np.abs(rows - cr) / stride
        wc = 1.0 - np.abs(cols - cc) / stride
        ri, ci = rows[wr > 0], cols[wc > 0]
        if not ri.size or not ci.size:
            continue
        wt = np.outer(wr[wr > 0], wc[wc > 0])
        rs, cs = slice(ri[0], ri[-1] + 1), slice(ci[0], ci[-1] + 1)
        mf = (x[rs, cs].astype(np.float64) - bg.mu) @ bg.w
        good = np.isfinite(mf)
        wt = np.where(good, wt, 0.0)
        num[rs, cs] += wt * np.where(good, mf, 0.0)
        den[rs, cs] += wt
        nbg[rs, cs] += wt * bg.n
    out = np.full((h, w), np.nan, np.float32)
    n_out = np.full((h, w), np.nan, np.float32)
    has = (den > 0) & valid
    out[has] = num[has] / den[has]
    n_out[has] = nbg[has] / den[has]
    return out, n_out


__all__ = ["F208_CENTRE", "F208_SHOULDERS", "Background", "background", "band_depth",
           "band_index", "blended_mf", "continuum_removed", "needed_centres", "noise_sigma",
           "window_stats"]
