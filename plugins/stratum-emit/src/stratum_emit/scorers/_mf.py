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

Or, instead of steps 2-3, **per detector column** (`column_mf`): one mean and covariance per
column of the sensor array that filled the cells (`sensor_column`), pooled over the neighbouring
columns only where a column has too few cells. On a push-broom every cross-track column is its
own spectrometer with its own response, so its background is estimated apart from the others.

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
    n = x.shape[0]
    mu = x.mean(axis=0)
    xc = x - mu
    cov = (xc.T @ xc) / max(n - 1, 1)
    return _filter(mu, cov, n, target, shrink=shrink, k=k,
                   sig=noise_sigma(x) if k is not None else None)


def _filter(mu: np.ndarray, cov: np.ndarray, n: int, target: np.ndarray, *, shrink: float,
            k: int | None, sig: np.ndarray | None) -> Background | None:
    """The MF projection for a background mean and covariance (`background`, `column_mf`)."""
    b = mu.size
    s = target - mu
    cov = (1.0 - shrink) * cov + shrink * np.diag(np.diag(cov))
    if k is None or k >= b:
        # tiny ridge so a degenerate window (constant bands) still inverts
        cov = cov + np.eye(b) * (np.trace(cov) / b * 1e-9 + 1e-20)
        try:
            ci_s = np.linalg.solve(cov, s)
        except np.linalg.LinAlgError:
            return None
    else:
        assert sig is not None
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


def _column_moments(x: np.ndarray, col: np.ndarray, ncol: int
                    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per column c < ncol: the cell count, Σx (B) and Σxxᵀ (B, B) of the (n, B) rows `x` whose
    column `col` is c."""
    b = x.shape[1]
    n = np.bincount(col, minlength=ncol).astype(np.float64)
    s1 = np.zeros((ncol, b))
    s2 = np.zeros((ncol, b, b))
    order = np.argsort(col, kind="stable")
    xs, cs = x[order], col[order]
    edges = np.flatnonzero(np.diff(cs)) + 1
    for lo, hi in zip(np.r_[0, edges], np.r_[edges, cs.size], strict=True):
        if hi > lo:
            c, part = cs[lo], xs[lo:hi]
            s1[c] = part.sum(axis=0)
            s2[c] = part.T @ part
    return n, s1, s2


def _column_filters(n: np.ndarray, s1: np.ndarray, s2: np.ndarray, present: np.ndarray,
                    target: np.ndarray, *, shrink: float, min_n: int, max_radius: int
                    ) -> dict[int, Background]:
    """A filter per present column from the moments of the columns c - r .. c + r, with r the
    smallest radius (up to `max_radius`) that reaches `min_n` cells."""
    cn, c1, c2 = (np.concatenate([np.zeros((1, *a.shape[1:])), np.cumsum(a, axis=0)])
                  for a in (n, s1, s2))
    last = n.size
    out: dict[int, Background] = {}
    for c in present:
        for r in range(max_radius + 1):
            lo, hi = max(c - r, 0), min(c + r + 1, last)
            m = cn[hi] - cn[lo]
            if m >= min_n:
                break
        else:
            continue
        mu = (c1[hi] - c1[lo]) / m
        cov = ((c2[hi] - c2[lo]) - m * np.outer(mu, mu)) / max(m - 1.0, 1.0)
        bg = _filter(mu, cov, int(m), target, shrink=shrink, k=None, sig=None)
        if bg is not None:
            out[int(c)] = bg
    return out


def column_mf(x: np.ndarray, valid: np.ndarray, col: np.ndarray, target: np.ndarray, *,
              shrink: float, min_n: int, trim: float, max_radius: int
              ) -> tuple[np.ndarray, np.ndarray]:
    """(mf, bg_n) for (H, W, B) spectra whose cells came from sensor columns `col` (H, W; NaN
    where unknown), each column scored against its own background. A column with fewer than
    `min_n` valid cells pools its neighbours, up to `max_radius` either side, and a column that
    still has too few is left NaN. `trim` > 0 drops each column's most target-like fraction
    from the background and re-estimates, as `window_stats` does."""
    h, w = valid.shape
    ok = valid & np.isfinite(col)
    mf = np.full((h, w), np.nan, np.float32)
    bg_n = np.full((h, w), np.nan, np.float32)
    if not ok.any():
        return mf, bg_n
    pix = x[ok].astype(np.float64)
    cols = col[ok].astype(np.int64)
    ncol = int(cols.max()) + 1
    present = np.unique(cols)
    n, s1, s2 = _column_moments(pix, cols, ncol)
    filters = _column_filters(n, s1, s2, present, target, shrink=shrink, min_n=min_n,
                              max_radius=max_radius)

    def apply(filters: dict[int, Background]) -> tuple[np.ndarray, np.ndarray]:
        score = np.full(cols.size, np.nan)
        count = np.full(cols.size, np.nan)
        for c, bg in filters.items():
            sel = cols == c
            score[sel] = (pix[sel] - bg.mu) @ bg.w
            count[sel] = bg.n
        return score, count

    score, count = apply(filters)
    if trim > 0 and filters:
        keep = np.ones(cols.size, dtype=bool)
        for c in filters:
            sel = np.flatnonzero(cols == c)
            if sel.size:
                keep[sel] = score[sel] <= np.quantile(score[sel], 1.0 - trim)
        n, s1, s2 = _column_moments(pix[keep], cols[keep], ncol)
        trimmed = _column_filters(n, s1, s2, present, target, shrink=shrink, min_n=min_n,
                                  max_radius=max_radius)
        score, count = apply(filters | trimmed)
    mf[ok] = score
    bg_n[ok] = count
    return mf, bg_n


__all__ = ["F208_CENTRE", "F208_SHOULDERS", "Background", "background", "band_depth",
           "band_index", "blended_mf", "column_mf", "continuum_removed", "needed_centres", "noise_sigma",
           "window_stats"]
