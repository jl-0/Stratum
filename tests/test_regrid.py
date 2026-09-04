"""stratum.regrid: the KD-tree wrapper, GLT IO, the stage, the recorded hash (03, 06)."""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest
import rasterio
from affine import Affine

from stratum import regrid
from stratum.cache import CacheRoot
from stratum.regrid import (
    LATTICE_TOLERANCE,
    REGRID_ALGO_VERSION,
    LatticeMismatch,
    adopt_glt,
    build_glt,
    build_glt_raw,
    check_recorded_hash,
    clean_contiguous,
    internal_tile_size,
    lattice_offset,
    read_glt,
    regrid_granule_tile,
    resolve_max_distance,
    write_glt,
)
from stratum.regrid.__main__ import main as regrid_main
from stratum.types import EmbeddedGLT, GridDef, LocArray, TileRef, Window

# tile_size 0.01 at 0.001 -> a 10 x 10 tile; cell diagonal 0.001414, default max_distance 0.00212
GRID = GridDef("EPSG:4326", (0.001, -0.001), (0.0, 0.0), 0.01, block_size=512)
TILE = TileRef(GRID, 0, 0)
ANCHORS = (1, 3, 5, 7)   # tile rows/cols that carry a sensor pixel; sensor (i, j) -> cell (A[i], A[j])


def centre(tile: TileRef, r: int, c: int) -> tuple[float, float]:
    """(lat, lon) of a cell centre, written out independently of the module's helper."""
    x0, _, _, y1 = tile.bounds
    rx, ry = tile.grid.resolution
    return y1 + (r + 0.5) * ry, x0 + (c + 0.5) * rx


def anchor_loc(tile: TileRef = TILE, anchors=ANCHORS) -> LocArray:
    n = len(anchors)
    lat = np.full((n, n), np.nan)
    lon = np.full((n, n), np.nan)
    for i, r in enumerate(anchors):
        for j, c in enumerate(anchors):
            lat[i, j], lon[i, j] = centre(tile, r, c)
    return LocArray(lat=lat, lon=lon, elev=np.zeros((n, n)))


# ------------------------------------------------------------------------------------ geometry
def test_tile_is_ten_by_ten() -> None:
    assert TILE.shape == (10, 10)
    assert TILE.bounds == (0.0, 0.0, 0.01, 0.01)


def test_default_max_distance_is_1_5_diagonals() -> None:
    assert resolve_max_distance(GRID, None) == pytest.approx(1.5 * math.hypot(0.001, 0.001))
    assert resolve_max_distance(GRID, 0.004) == 0.004
    with pytest.raises(ValueError):
        resolve_max_distance(GRID, 0.0)


def test_anchor_cells_map_to_one_based_sensor_indices() -> None:
    glt = build_glt(anchor_loc(), TILE, max_distance=None)
    assert glt.shape == (10, 10, 3) and glt.dtype == np.int32
    for i, r in enumerate(ANCHORS):
        for j, c in enumerate(ANCHORS):
            assert tuple(glt[r, c]) == (j + 1, i + 1, 1), (r, c)   # GLT X = col, GLT Y = row
    # every cell inside the loc bounding box is within 0.0015 of an anchor: all hits, none negative
    inside = glt[1:8, 1:8]
    assert (inside[..., 2] == 1).all()
    assert (inside[..., :2] > 0).all()
    # cells whose centre lies outside the loc bounding box are never assigned
    assert not glt[0].any() and not glt[8:].any()
    assert not glt[:, 0].any() and not glt[:, 8:].any()
    # band 3 is exactly the hit mask
    assert np.array_equal(glt[..., 2] != 0, (glt[..., 0] != 0) & (glt[..., 1] != 0))


def test_far_cell_is_negative_before_the_clean() -> None:
    # 0.0012 < the 0.001414 diagonal: cells at (odd, odd) offsets from the anchors are reached for
    raw = build_glt_raw(anchor_loc(), TILE, max_distance=0.0012)
    assert raw[2, 2, 0] < 0 and raw[2, 2, 1] < 0 and raw[2, 2, 2] == 1
    assert 1 <= -raw[2, 2, 0] <= 4 and 1 <= -raw[2, 2, 1] <= 4      # still a real sensor pixel
    assert raw[1, 2, 0] > 0 and raw[1, 2, 1] == 1                   # 0.001 away: measured in place
    # isolated negatives survive the clean, so they reach resolve as obs.interpolated
    cleaned = build_glt(anchor_loc(), TILE, max_distance=0.0012)
    assert cleaned[2, 2, 0] < 0 and cleaned[2, 2, 2] == 1
    assert tuple(cleaned[1, 1]) == (1, 1, 1)


def test_clean_contiguous_is_a_3x3_stencil() -> None:
    glt = np.ones((5, 5, 3), dtype=np.int32)
    glt[2, 2, :2] = -1                     # a lone flagged cell: 1 in its 3x3 -> kept
    glt = clean_contiguous(glt)
    assert tuple(glt[2, 2]) == (-1, -1, 1)
    glt = np.ones((5, 5, 3), dtype=np.int32)
    glt[1:3, 1:3, :2] = -1                 # a 2x2 flagged block: 4 in each 3x3 -> all three bands 0
    glt = clean_contiguous(glt)
    assert not glt[1:3, 1:3].any()
    assert glt[0, 0, 2] == 1 and glt[3, 3, 2] == 1 and glt[0, 1, 2] == 1   # <= 2 flagged: kept


def test_fills_are_dropped_and_indices_refer_to_the_full_sensor_array() -> None:
    loc = anchor_loc()
    lat, lon = loc.lat.copy(), loc.lon.copy()
    lat[0, :] = np.nan          # a whole sensor row of fill
    lon[2, 2] = np.nan          # one interior fill
    glt = build_glt(LocArray(lat=lat, lon=lon, elev=loc.elev), TILE, max_distance=None)
    x, y = glt[..., 0], glt[..., 1]
    assert not (y == 1).any()                       # sensor row 0 never referenced
    assert not ((x == 3) & (y == 3)).any()          # sensor (2, 2) never referenced
    assert tuple(glt[3, 3]) == (2, 2, 1)            # sensor (1, 1) keeps its full-array index
    assert tuple(glt[7, 7]) == (4, 4, 1)
    assert glt[5, 5, 2] == 1 and tuple(glt[5, 5, :2]) != (3, 3)   # reassigned to a neighbour
    assert not glt[:3].any()                        # bounding box shrank: rows above anchor 3


def test_masked_loc_matches_nan_loc() -> None:
    loc = anchor_loc()
    lat = loc.lat.copy()
    lat[0, :] = np.nan
    lat_ma = np.ma.masked_array(np.nan_to_num(lat, nan=-9999.0), mask=np.isnan(lat))
    a = build_glt(LocArray(lat=lat, lon=loc.lon, elev=loc.elev), TILE, max_distance=None)
    b = build_glt(LocArray(lat=lat_ma, lon=loc.lon, elev=loc.elev), TILE, max_distance=None)
    assert np.array_equal(a, b)


def test_granule_off_tile_or_all_fill_gives_zeros() -> None:
    far = LocArray(lat=np.full((3, 3), 41.5), lon=np.full((3, 3), -117.5), elev=np.zeros((3, 3)))
    assert not build_glt(far, TILE, max_distance=None).any()
    empty = LocArray(lat=np.full((3, 3), np.nan), lon=np.full((3, 3), np.nan),
                     elev=np.zeros((3, 3)))
    assert build_glt(empty, TILE, max_distance=None).shape == (10, 10, 3)
    assert not build_glt(empty, TILE, max_distance=None).any()


# ------------------------------------------------------------------------------------------ IO
@pytest.mark.parametrize("block,expected", [(720, 240), (512, 512), (1024, 512), (16, 16),
                                            (500, 256), (3600, 400), (720 * 3, 432)])
def test_internal_tile_size(block: int, expected: int) -> None:
    assert internal_tile_size(block) == expected
    assert expected % 16 == 0 and expected <= 512


def test_write_read_round_trip(tmp_path: Path) -> None:
    glt = build_glt(anchor_loc(), TILE, max_distance=0.0012)
    path = tmp_path / "glt.tif"
    tags = {"regrid_algo_version": REGRID_ALGO_VERSION, "max_distance": 0.0012,
            "regrid_method": "kdtree"}
    write_glt(path, glt, TILE, granule_id="g1", tags=tags)
    assert np.array_equal(read_glt(path), glt)
    win = Window(2, 3, 4, 5)
    assert np.array_equal(read_glt(path, win), glt[2:6, 3:8])
    # a halo window past the tile edge (01 section 4) comes back zero-padded, never truncated
    halo = read_glt(path, Window(-1, -1, 12, 12))
    assert halo.shape == (12, 12, 3)
    assert np.array_equal(halo[1:11, 1:11], glt)
    assert not halo[0].any() and not halo[-1].any() and not halo[:, 0].any()
    assert not halo[:, -1].any()
    with rasterio.open(path) as src:
        assert src.count == 3 and src.dtypes == ("int32",) * 3 and src.nodata == 0
        assert src.descriptions == ("GLT X", "GLT Y", "File Index")
        assert src.crs.to_string() == "EPSG:4326"
        assert src.transform == TILE.transform
        assert src.profile["tiled"] and src.profile["compress"] == "deflate"
        assert src.block_shapes[0] == (512, 512)
        assert src.overviews(1) == []
        t = src.tags()
        assert t["granule_id"] == "g1" and t["grid_id"] == GRID.id
        assert t["regrid_algo_version"] == str(REGRID_ALGO_VERSION)
        assert t["max_distance"] == "0.0012" and t["regrid_method"] == "kdtree"


def test_write_glt_checks_shape(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="does not match tile shape"):
        write_glt(tmp_path / "bad.tif", np.zeros((3, 3, 3), np.int32), TILE, granule_id="g",
                  tags={})


# ------------------------------------------------------------------------------------- stage
def test_regrid_granule_tile_hit_never_reads_the_granule(tmp_path: Path) -> None:
    cache = CacheRoot(tmp_path)
    calls = 0

    def loc() -> LocArray:
        nonlocal calls
        calls += 1
        return anchor_loc()

    key = regrid_granule_tile(cache, TILE, "g1", loc, max_distance=None)
    assert calls == 1
    assert key.path == tmp_path / "cache" / "glt" / GRID.id / "0_0" / f"{key.hash16}.tif"
    assert cache.hit(key)
    assert key.inputs["max_distance"] == resolve_max_distance(GRID, None)   # the value used
    assert key.inputs["regrid_algo_version"] == REGRID_ALGO_VERSION
    assert np.array_equal(read_glt(key.path), build_glt(anchor_loc(), TILE, max_distance=None))
    again = regrid_granule_tile(cache, TILE, "g1", loc, max_distance=None)
    assert again.path == key.path and calls == 1


def test_regrid_key_sensitivity(tmp_path: Path) -> None:
    cache = CacheRoot(tmp_path)
    a = regrid_granule_tile(cache, TILE, "g1", anchor_loc, max_distance=None)
    b = regrid_granule_tile(cache, TILE, "g1", anchor_loc, max_distance=0.002)
    assert a.path != b.path
    assert cache.diff(a, b) == {"max_distance": (resolve_max_distance(GRID, None), 0.002)}
    small = TileRef(GridDef(GRID.crs, GRID.resolution, GRID.origin, GRID.tile_size, block_size=16), 0, 0)
    c = regrid_granule_tile(cache, small, "g1", anchor_loc, max_distance=None)
    assert c.path == a.path                         # block size is not in the key
    with rasterio.open(a.path) as src:
        assert src.block_shapes[0] == (512, 512)   # written under the first grid's block


def test_regrid_method_other_than_kdtree_not_implemented(tmp_path: Path) -> None:
    with pytest.raises(NotImplementedError, match="03 section 3"):
        regrid_granule_tile(CacheRoot(tmp_path), TILE, "g1", anchor_loc, max_distance=None,
                            regrid_method="warp_embedded")


def test_non_intersecting_granule_writes_a_zero_glt(tmp_path: Path) -> None:
    cache = CacheRoot(tmp_path)
    far = LocArray(lat=np.full((3, 3), 41.5), lon=np.full((3, 3), -117.5), elev=np.zeros((3, 3)))
    key = regrid_granule_tile(cache, TILE, "far", lambda: far, max_distance=None)
    assert cache.hit(key)
    assert not read_glt(key.path).any()


# ------------------------------------------------------------------------- the recorded hash
def test_algo_hash_matches_module() -> None:
    """06 section 3, rule 2: the module changed -> bump REGRID_ALGO_VERSION and re-record."""
    ok, message = check_recorded_hash()
    assert ok, message


def test_check_reports_which_step_was_skipped(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "ALGO_HASH"
    monkeypatch.setattr(regrid, "ALGO_HASH_PATH", path)
    ok, msg = check_recorded_hash()
    assert not ok and "missing" in msg
    path.write_text(f"{REGRID_ALGO_VERSION} sha256:{'0' * 64}\n")
    ok, msg = check_recorded_hash()
    assert not ok and "bump" in msg
    path.write_text(f"{REGRID_ALGO_VERSION + 1} {regrid.module_content_hash()}\n")
    ok, msg = check_recorded_hash()
    assert not ok and "REGRID_ALGO_VERSION" in msg
    assert regrid_main([]) == 1
    assert regrid_main(["--record"]) == 0
    assert path.read_text().split() == [str(REGRID_ALGO_VERSION), regrid.module_content_hash()]
    assert regrid_main([]) == 0
    assert regrid_main(["--bogus"]) == 2


# --------------------------------------------------------------------------------------- adopt
# A product that was gridded on the run's lattice ships a lookup table we can take as it stands.
# `embedded_at` puts a 4 x 4 table at whole-cell offset (row, col) of TILE.


def embedded_at(row: int, col: int, *, shift: float = 0.0, res: float = 0.001,
                crs: str = "EPSG:4326", n: int = 4) -> EmbeddedGLT:
    rx, ry = TILE.grid.resolution
    t = TILE.transform
    data = np.zeros((n, n, 3), dtype=np.int32)
    for i in range(n):
        for j in range(n):
            data[i, j] = (j + 1, i + 1, 1)          # sensor col, sensor row, hit
    return EmbeddedGLT(data=data,
                       transform=Affine(res, 0, t.c + col * rx + shift * rx,
                                        0, -res, t.f + row * ry + shift * abs(ry)),
                       crs=crs)


def test_lattice_offset_counts_whole_cells() -> None:
    """(kx, ky) are cells from the GRID origin, not from the tile: origin + k * res is the
    raster's own origin back again."""
    e = embedded_at(3, 2)
    kx, ky = lattice_offset(GRID, e.transform, e.crs)
    x0, y0 = GRID.origin
    rx, ry = GRID.resolution
    assert (x0 + kx * rx, y0 + ky * ry) == pytest.approx((e.transform.c, e.transform.f))


@pytest.mark.parametrize("kwargs, message", [
    ({"shift": 0.5}, "off the lattice"),
    ({"shift": 10 * LATTICE_TOLERANCE}, "off the lattice"),
    ({"res": 0.002}, "cell size"),
    ({"crs": "EPSG:3857"}, "CRS"),
])
def test_lattice_offset_refuses_a_grid_it_cannot_adopt(kwargs, message) -> None:
    e = embedded_at(3, 2, **kwargs)
    with pytest.raises(LatticeMismatch, match=message):
        lattice_offset(GRID, e.transform, e.crs)


def test_lattice_offset_accepts_float_noise() -> None:
    """The tolerance exists for float64 representation, not for a different grid."""
    noisy = embedded_at(3, 2, shift=LATTICE_TOLERANCE / 10)
    exact = embedded_at(3, 2)
    assert (lattice_offset(GRID, noisy.transform, noisy.crs)
            == lattice_offset(GRID, exact.transform, exact.crs))


def test_adopt_places_the_table_and_changes_no_value() -> None:
    glt = adopt_glt(embedded_at(3, 2), TILE)
    assert glt.shape == (10, 10, 3) and glt.dtype == np.int32
    assert np.array_equal(glt[3:7, 2:6, 0], np.tile(np.arange(1, 5, dtype=np.int32), (4, 1)))
    assert np.array_equal(glt[3:7, 2:6, 1], np.tile(np.arange(1, 5, dtype=np.int32), (4, 1)).T)
    assert glt[3:7, 2:6, 2].all()
    covered = np.zeros((10, 10), dtype=bool)
    covered[3:7, 2:6] = True
    assert not glt[~covered].any()                  # everything else is nodata in all three bands


def test_adopt_crops_at_the_tile_edge() -> None:
    glt = adopt_glt(embedded_at(8, 8), TILE)        # 4 x 4 starting two cells from the corner
    assert glt[8:10, 8:10, 2].all()
    assert glt[:8].sum() == 0 and glt[:, :8].sum() == 0


def test_adopt_of_a_product_that_misses_the_tile_is_all_zero() -> None:
    assert not adopt_glt(embedded_at(40, 40), TILE).any()


def test_adopt_zeroes_a_cell_either_band_left_as_nodata() -> None:
    e = embedded_at(3, 2)
    e.data[1, 1, 0] = 0                             # glt_x nodata, glt_y still set
    glt = adopt_glt(e, TILE)
    assert not glt[4, 3].any()


def test_adopt_refuses_a_product_on_another_lattice() -> None:
    with pytest.raises(LatticeMismatch):
        adopt_glt(embedded_at(3, 2, shift=0.5), TILE)


def test_adopt_through_the_stage_keys_on_the_source(tmp_path: Path) -> None:
    """Two granules, same table, different source bytes -> different keys; and no `loc` is read."""
    cache = CacheRoot(tmp_path)

    def no_loc() -> LocArray:
        raise AssertionError("adopt must not read the geolocation arrays")

    keys = [regrid_granule_tile(cache, TILE, "g1", no_loc, max_distance=None,
                                regrid_method="adopt", embedded=lambda: embedded_at(3, 2),
                                source_id=src)
            for src in ("sha512:aaa", "sha512:bbb")]
    assert keys[0].hash != keys[1].hash
    assert keys[0].inputs["source_checksum"] == "sha512:aaa"
    assert keys[0].inputs["max_distance"] is None
    glt = read_glt(keys[0].path)
    assert np.array_equal(glt, adopt_glt(embedded_at(3, 2), TILE))
    with rasterio.open(keys[0].path) as src:
        assert src.tags()["regrid_method"] == "adopt"
        assert src.tags()["glt_source"] == "sha512:aaa"


def test_adopt_does_not_disturb_the_kdtree_key(tmp_path: Path) -> None:
    """The `adopt` key term must not exist for `kdtree`, or every cached GLT is invalidated."""
    key = regrid_granule_tile(CacheRoot(tmp_path), TILE, "g1", anchor_loc, max_distance=None)
    assert "source_checksum" not in key.inputs
    assert key.inputs["max_distance"] == pytest.approx(resolve_max_distance(GRID, None))


def test_adopt_needs_an_embedded_glt(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="adopt"):
        regrid_granule_tile(CacheRoot(tmp_path), TILE, "g1", anchor_loc, max_distance=None,
                            regrid_method="adopt")


def test_warp_embedded_is_still_refused(tmp_path: Path) -> None:
    with pytest.raises(NotImplementedError, match="warp_embedded"):
        regrid_granule_tile(CacheRoot(tmp_path), TILE, "g1", anchor_loc, max_distance=None,
                            regrid_method="warp_embedded")
