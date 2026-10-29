"""The `matched_filter` scorer (stratum_emit) and its arithmetic, on synthetic cubes.

The background is a few smooth spectral shapes mixed at random plus white noise; the target is the
packaged topaz spectrum mixed in linearly at a known fraction. Block-size independence is checked
the way resolve would see it: each block gets its halo window cut from one tile-sized cube.
"""
from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest
from stratum_emit.scorers import MatchedFilter, _mf
from stratum_emit.scorers.matched_filter import load_target

from stratum.types import BlockRef, Epoch, GranuleRef, GridDef, ObsWindow, TileRef

EPOCH = Epoch(datetime(2025, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC))
GRANULE = GranuleRef(granule_id="G", collection="EMITL2ARFL", datetime=EPOCH.start,
                     end_datetime=EPOCH.start, bbox=(0.0, 0.0, 1.0, 1.0), assets={},
                     build_version="1", product_version="V001", collection_version="001",
                     cloud_fraction=0.0)
WL = np.linspace(1950.0, 2350.0, 55)


def cube(rows: int, cols: int, *, seed: int = 0, noise: float = 0.002) -> np.ndarray:
    """(rows, cols, bands) background reflectance: three smooth shapes mixed per cell."""
    rng = np.random.default_rng(seed)
    x = (WL - 2150.0) / 200.0
    shapes = np.stack([0.30 + 0.05 * x, 0.25 - 0.08 * np.exp(-((WL - 2200) / 15) ** 2),
                       0.35 - 0.04 * np.exp(-((WL - 2330) / 20) ** 2)])
    mix = rng.dirichlet([2.0, 2.0, 2.0], size=(rows, cols))
    bright = rng.uniform(0.7, 1.3, size=(rows, cols, 1))
    return (mix @ shapes) * bright + rng.normal(0, noise, size=(rows, cols, WL.size))


def topaz() -> np.ndarray:
    twl, tref = load_target("topaz-mt-antero-5")
    return np.interp(WL, twl, tref)


def window(data: np.ndarray, valid: np.ndarray, block: BlockRef) -> ObsWindow:
    """The block's halo window cut from a tile-sized cube, invalid outside the tile."""
    w = block.window
    rows, cols = valid.shape
    out = np.zeros((w.height, w.width, data.shape[-1]), np.float32)
    ok = np.zeros((w.height, w.width), bool)
    r0, c0 = max(w.row_off, 0), max(w.col_off, 0)
    r1, c1 = min(w.row_off + w.height, rows), min(w.col_off + w.width, cols)
    out[r0 - w.row_off:r1 - w.row_off, c0 - w.col_off:c1 - w.col_off] = data[r0:r1, c0:c1]
    ok[r0 - w.row_off:r1 - w.row_off, c0 - w.col_off:c1 - w.col_off] = valid[r0:r1, c0:c1]
    return ObsWindow(space="block", bands={"reflectance": out}, valid=ok, granules=[GRANULE],
                     epoch=EPOCH, block=block,
                     band_attrs={"reflectance": {"wavelength": WL.tolist()}})


def run_tile(data: np.ndarray, valid: np.ndarray, block_size: int,
             scorer: MatchedFilter) -> dict[str, np.ndarray]:
    """Every block of one tile through score + emit, stitched by core - what resolve does."""
    rows, cols = valid.shape
    grid = GridDef("EPSG:4326", (0.001, -0.001), (0.0, 0.0), rows * 0.001, block_size=block_size)
    tile = TileRef(grid, 0, 0)
    assert tile.shape == (rows, cols)
    out = {b.name: np.full((rows, cols), np.nan, np.float32) for b in scorer.outputs}
    for by in range(-(-rows // block_size)):
        for bx in range(-(-cols // block_size)):
            block = BlockRef(tile, bx, by, halo=scorer.halo)
            obs = window(data, valid, block)
            scorer.score(obs, None)
            emitted = scorer.emit(obs, None)
            c, h = block.core_window, scorer.halo
            for name, arr in emitted.items():
                out[name][c.row_off:c.row_off + c.height, c.col_off:c.col_off + c.width] = \
                    arr[h:h + c.height, h:h + c.width]
    return out


def brightness_share(area: float, bg: np.ndarray, t: np.ndarray) -> float:
    """What the MF recovers after continuum removal: the target's share of the mixed CONTINUUM,
    `a·Ct / (a·Ct + (1−a)·Cb)` with C the mean reflectance over the window - not its area. A
    bright mineral in a dark background reads higher than its area, a dark one lower."""
    ct, cb = float(t.mean()), float(bg.mean())
    return area * ct / (area * ct + (1 - area) * cb)


def test_implanted_topaz_recovers_its_brightness_share():
    bg = cube(80, 80)
    t = topaz()
    frac = np.zeros((80, 80))
    frac[30:34, 30:34] = 0.1
    frac[50:52, 60:62] = 0.3
    data = (1 - frac[..., None]) * bg + frac[..., None] * t
    mf = MatchedFilter(stride=20, min_n=100, lo=1950.0, hi=2350.0,
                       shoulders=((1950.0, 1990.0), (2300.0, 2350.0)))
    out = run_tile(data, np.ones((80, 80), bool), 80, mf)["mf"]
    for (rs, cs), area in (((slice(30, 34), slice(30, 34)), 0.1),
                           ((slice(50, 52), slice(60, 62)), 0.3)):
        want = brightness_share(area, bg[rs, cs], t)
        assert np.nanmean(out[rs, cs]) == pytest.approx(want, abs=0.06), area
    rest = out[frac == 0]
    assert abs(np.nanmean(rest)) < 0.01 and np.nanstd(rest) < 0.03


def test_band_depth_sees_the_208_feature_without_statistics():
    t = topaz()
    flat = np.full_like(t, 0.3)
    depth = _mf.band_depth(np.stack([t, flat]), WL)
    assert depth[0] > 0.4 and abs(depth[1]) < 1e-6


def test_tent_weights_sum_to_one():
    """A window model that scores every cell 1 blends to exactly 1 everywhere it reaches."""
    stride, h = 10, 40
    x = np.zeros((h, h, 3))
    x[..., 0] = 1.0
    one = _mf.Background(mu=np.zeros(3), w=np.array([1.0, 0.0, 0.0]), n=7)
    stats = {(kr, kc): one for kr in range(5) for kc in range(5)}
    core = (slice(5, 35), slice(5, 35))
    mf, n = _mf.blended_mf(x, np.ones((h, h), bool), stats, origin=(0, 0), core=core,
                           stride=stride)
    assert np.allclose(mf[core], 1.0) and np.allclose(n[core], 7.0)
    assert np.isnan(mf[:5]).all()


def test_block_size_does_not_change_the_answer():
    """The lattice is anchored to the grid, so blocks of 40 and of 120 give one map (01
    section 3 invariant) - and blocks blend into each other with no seam."""
    data = cube(120, 120, seed=3)
    valid = np.ones((120, 120), bool)
    mf = MatchedFilter(stride=15, min_n=100, lo=1950.0, hi=2350.0,
                       shoulders=((1950.0, 1990.0), (2300.0, 2350.0)))
    a = run_tile(data, valid, 120, mf)
    b = run_tile(data, valid, 40, mf)
    for name in a:
        np.testing.assert_allclose(a[name], b[name], rtol=1e-5, atol=1e-6, equal_nan=True)


def test_a_dropped_window_renormalises_rather_than_leaving_a_hole():
    """Coverage that thins out below `min_n` drops that window; its neighbours carry the cells."""
    data = cube(90, 90, seed=5)
    valid = np.ones((90, 90), bool)
    valid[:, 60:] = False
    valid[::3, 60:] = True          # sparse strip: too few cells for its own windows
    mf = MatchedFilter(stride=15, min_n=200, lo=1950.0, hi=2350.0,
                       shoulders=((1950.0, 1990.0), (2300.0, 2350.0)), trim=0.0)
    out = run_tile(data, valid, 90, mf)["mf"]
    assert np.isfinite(out[valid]).mean() > 0.95
    assert np.isnan(out[~valid]).all()


def test_mnf_k_keeps_the_answer_when_it_keeps_the_signal():
    data = cube(60, 60, seed=7)
    data[20:24, 20:24] = 0.6 * data[20:24, 20:24] + 0.4 * topaz()
    kw = {"stride": 15, "min_n": 100, "lo": 1950.0, "hi": 2350.0,
          "shoulders": ((1950.0, 1990.0), (2300.0, 2350.0))}
    full = run_tile(data, np.ones((60, 60), bool), 60, MatchedFilter(**kw))["mf"]
    some = run_tile(data, np.ones((60, 60), bool), 60, MatchedFilter(k=20, **kw))["mf"]
    assert np.nanmean(some[20:24, 20:24]) == pytest.approx(np.nanmean(full[20:24, 20:24]),
                                                           abs=0.1)


@pytest.mark.parametrize("kw, match", [
    ({"lo": 2300.0, "hi": 2000.0}, "lo < hi"),
    ({"shoulders": ((1900.0, 1950.0), (2200.0, 2250.0))}, "shoulders"),
    ({"stride": 1}, "stride"),
    ({"k": 0}, "k is"),
    ({"target": "beryl"}, "no packaged target"),
])
def test_bad_parameters_fail_at_construction(kw, match):
    with pytest.raises(ValueError, match=match):
        MatchedFilter(**kw)


def test_halo_is_twice_the_stride():
    assert MatchedFilter(stride=60).halo == 120
