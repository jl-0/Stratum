"""`matched_filter`: a continuous per-cell target affinity from reflectance (04 section 4).

A streaming scorer, so every look gets its OWN background model (illumination and atmosphere
differ per granule; pooling looks would blur both). For each observation over a block's halo
window it:

1. keeps the good bands in `[lo, hi]` nm and removes their continuum - the straight line between
   the two `shoulders` intervals - from the cells and from the target alike;
2. estimates a background mean and covariance per window of a fixed lattice (stride `stride`
   cells, anchored to the grid origin), shrunk toward the diagonal, optionally trimmed of its
   own most target-like cells, optionally reduced to the top-`k` MNF components;
3. scores each core cell under the four windows covering it and blends the four with tent
   weights that sum to one (`_mf.blended_mf`), so the background moves smoothly and nothing
   steps where a window ends.

The arithmetic is in `stratum_emit.scorers._mf`. `halo = 2 * stride`: a core cell's farthest
window gathers cells up to two strides away. Inside a tile that halo is real data, so blocks
blend into each other exactly; across a tile edge it is whatever the GLT covers (01 section 4).

The SCORE (which look occupies the cell when an epoch has several) is the tent-weighted number
of background cells behind the cell's MF - a look with more usable surroundings has the better
estimated background - and never the MF itself, which would turn the epoch into a max over
looks. The affinity is reduced across epochs by the schema (`median`).

Emitted outputs (`source: scorer.<name>`): `mf` (≈0 background, ≈1 a cell of pure target; in
between, the target's share of the cell's continuum brightness rather than its area - see `_mf`),
`band_depth_208` (the 2.08 um feature's depth against its own local continuum - no statistics,
a cross-check), `bg_n` (the background cells behind the MF).
"""
from __future__ import annotations

from collections.abc import Sequence
from functools import cache
from importlib import resources

import numpy as np

from stratum.types import AuxAccessor, BandSpec, ObsWindow
from stratum_emit.scorers import _mf

OUTPUTS: tuple[BandSpec, ...] = (
    BandSpec("mf", "float32", "matched-filter abundance of the target against the local "
             "background (its share of the cell's continuum brightness), about 0 over "
             "background and 1 for a cell of pure target",
             units="fraction"),
    BandSpec("band_depth_208", "float32", "continuum-removed depth of the 2.08 um feature "
             "(Tetracorder topaz f1a shoulders)", units="fraction"),
    BandSpec("bg_n", "float32", "tent-weighted number of background cells behind the mf",
             units="count"),
)


@cache
def load_target(name: str) -> tuple[np.ndarray, np.ndarray]:
    """(wavelength nm, reflectance) of a packaged target, `stratum_emit/targets/<name>.csv`.
    Packaged, not a path: a worker in the cloud has the distribution, not the manifest's
    directory."""
    path = resources.files("stratum_emit") / "targets" / f"{name}.csv"
    if not path.is_file():
        have = sorted(p.name.removesuffix(".csv")
                      for p in (resources.files("stratum_emit") / "targets").iterdir()
                      if p.name.endswith(".csv"))
        raise ValueError(f"matched_filter: no packaged target {name!r}; have {have}")
    rows = [line for line in path.read_text().splitlines()
            if line and not line.startswith("#")][1:]
    data = np.array([[float(v) for v in r.split(",")] for r in rows])
    return data[:, 0], data[:, 1]


class MatchedFilter:
    """Continuum-removed matched filter with overlap-blended local background statistics.

    Parameters: `target` (a packaged spectrum name), `role` (the reflectance role), `lo`/`hi`
    (the band window, nm), `shoulders` (two [lo, hi] nm continuum intervals inside the window),
    `stride` (cells between window centres; the halo is twice it), `shrink` (0-1, covariance
    toward its diagonal), `k` (None keeps every component; an int keeps the top-k MNF
    components), `min_n` (cells a window needs to be used), `trim` (fraction of a window's most
    target-like cells left out of its background, 0 to disable).
    """

    capability = "streaming"
    required_aux: tuple[str, ...] = ()
    outputs: tuple[BandSpec, ...] = OUTPUTS
    halo = 200
    required_roles: tuple[str, ...] = ("reflectance",)

    def __init__(self, target: str = "topaz-mt-antero-5", role: str = "reflectance",
                 lo: float = 2000.0, hi: float = 2300.0,
                 shoulders: Sequence[Sequence[float]] = ((2000.0, 2030.0), (2260.0, 2300.0)),
                 stride: int = 100, shrink: float = 0.05, k: int | None = None,
                 min_n: int = 500, trim: float = 0.01) -> None:
        name = "MatchedFilter"
        if not lo < hi:
            raise ValueError(f"{name}: needs lo < hi; got {lo}, {hi}")
        sh = tuple((float(a), float(b)) for a, b in shoulders)
        if len(sh) != 2 or not all(lo <= a < b <= hi for a, b in sh) or sh[0][1] > sh[1][0]:
            raise ValueError(f"{name}: shoulders are two ordered [lo, hi] intervals inside "
                             f"[{lo}, {hi}]; got {shoulders}")
        if int(stride) < 2:
            raise ValueError(f"{name}: stride must be at least 2 cells; got {stride}")
        if not 0.0 <= shrink <= 1.0:
            raise ValueError(f"{name}: shrink must be in [0, 1]; got {shrink}")
        if k is not None and int(k) < 1:
            raise ValueError(f"{name}: k is a positive component count or null; got {k}")
        if not 0.0 <= trim < 0.5:
            raise ValueError(f"{name}: trim must be in [0, 0.5); got {trim}")
        load_target(target)                 # fail at plan time, not on a worker
        self.target = target
        self.role = role
        self.lo, self.hi = float(lo), float(hi)
        self.shoulders = sh
        self.stride = int(stride)
        self.halo = 2 * self.stride
        self.shrink = float(shrink)
        self.k = None if k is None else int(k)
        self.min_n = int(min_n)
        self.trim = float(trim)
        self.required_roles = (role,)
        self.outputs = OUTPUTS
        self._memo: tuple[int, dict[str, np.ndarray]] | None = None

    def _wavelengths(self, obs: ObsWindow, nbands: int) -> tuple[np.ndarray, np.ndarray]:
        attrs = obs.band_attrs.get(self.role, {})
        if "wavelength" not in attrs:
            raise ValueError(f"matched_filter: role {self.role!r} reports no per-band "
                             "'wavelength'; it needs a reader that does (EMITL2ARFL)")
        wl = np.asarray(attrs["wavelength"], dtype=np.float64)
        if wl.size != nbands:
            raise ValueError(f"matched_filter: {wl.size} wavelengths for {nbands} bands")
        good = np.asarray(attrs.get("good", np.ones(nbands)), dtype=bool)
        return wl, good

    def _compute(self, obs: ObsWindow) -> dict[str, np.ndarray]:
        x = obs[self.role]
        h, w = x.shape[:2]
        wl, good = self._wavelengths(obs, x.shape[-1])
        keep = np.flatnonzero(good & (wl >= self.lo) & (wl <= self.hi))
        wl_k = wl[keep]
        xk = np.asarray(x[..., keep], dtype=np.float32)
        valid = np.asarray(obs.valid, dtype=bool) & np.isfinite(xk).all(axis=-1) \
            & (xk > -1.0).all(axis=-1)
        cr = _mf.continuum_removed(xk, wl_k, self.shoulders)
        valid &= np.isfinite(cr).all(axis=-1)
        twl, tref = load_target(self.target)
        t = np.interp(wl_k, twl, tref)
        t_cr = _mf.continuum_removed(t[None, :], wl_k, self.shoulders)[0]

        block = obs.block
        assert block is not None, "matched_filter runs in block space"
        grid = block.tile.grid
        tr = block.transform
        origin = (round((tr.f - grid.origin[1]) / grid.resolution[1]),
                  round((tr.c - grid.origin[0]) / grid.resolution[0]))
        hal = block.halo
        cw = block.core_window
        core = (slice(hal, hal + cw.height), slice(hal, hal + cw.width))
        centres = _mf.needed_centres(origin, core, self.stride)
        stats = _mf.window_stats(cr, valid, t_cr, origin=origin, stride=self.stride,
                                 centres=centres, shrink=self.shrink, k=self.k,
                                 min_n=self.min_n, trim=self.trim)
        mf, bg_n = _mf.blended_mf(cr, valid, stats, origin=origin, core=core,
                                  stride=self.stride)
        depth = np.full((h, w), np.nan, np.float32)
        d = _mf.band_depth(xk[core], wl_k)
        depth[core] = np.where(valid[core], d, np.nan)
        return {"mf": mf, "band_depth_208": depth, "bg_n": bg_n}

    def score(self, obs: ObsWindow, aux: AuxAccessor) -> np.ndarray:
        out = self._compute(obs)
        self._memo = (id(obs), out)
        return out["bg_n"].copy()

    def emit(self, obs: ObsWindow, aux: AuxAccessor) -> dict[str, np.ndarray]:
        if self._memo is None or self._memo[0] != id(obs):
            self.score(obs, aux)
        assert self._memo is not None
        out = self._memo[1]
        self._memo = None
        return out


__all__ = ["OUTPUTS", "MatchedFilter", "load_target"]
