"""EMIT readers, the reader registry and the AssetStore (12 sections 3-4, 11 sections 2, 7, 9).

Synthetic granules (conftest `make_granule`) carry the fill-vs-zero cases; the reference granule
tests run only when refs/EMIT_L2B_MIN_*.nc is present.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from affine import Affine

from stratum.access import AssetStore, LocalAsset, clear_cache, reader_for, to_uri
from stratum.types import ClassTable, EmbeddedGLT, LocArray, SensorWindow
from stratum_emit.readers import L1BObs, L2AMask, L2BFrcov, L2BMin


def _open(path: Path):
    reader = L2BMin()
    return reader, reader.open(AssetStore().open(str(path)))


# --------------------------------------------------------------------------------- synthetic L2B
def test_variables_report_every_root_variable(make_granule):
    reader, ctx = _open(make_granule("EMIT_L2B_MIN_001_a.nc"))
    with ctx:
        specs = reader.variables(ctx)
    assert set(specs) == {"group_1_mineral_id", "group_1_band_depth",
                          "group_2_mineral_id", "group_2_band_depth"}
    mid = specs["group_1_mineral_id"]
    assert (mid.dtype, mid.shape, mid.fill, mid.units) == ("int16", (6, 5), -9999, "unitless")
    assert specs["group_1_band_depth"].fill == -9999.0
    assert specs["group_1_band_depth"].dtype == "float32"
    assert mid.band_attrs == {}


def test_int_fill_is_masked_and_zero_is_kept(make_granule):
    """-9999 is 'not observed'; 0 is 'observed, nothing identified' (11 section 2)."""
    reader, ctx = _open(make_granule("EMIT_L2B_MIN_001_a.nc"))
    with ctx:
        arr = reader.read(ctx, "group_1_mineral_id")
    assert isinstance(arr, np.ma.MaskedArray)
    assert arr.dtype == np.int16                      # never promoted to float
    assert arr.mask[0].all() and not arr.mask[1:].any()
    assert (arr.data[0] == -9999).all()               # fill left in place under the mask
    assert (arr[1] == 0).all() and not np.ma.is_masked(arr[1])
    assert arr[2, 0] == 2 * 5 + 1


def test_float_fill_masked_and_left_as_is(make_granule):
    reader, ctx = _open(make_granule("EMIT_L2B_MIN_001_a.nc"))
    with ctx:
        arr = reader.read(ctx, "group_1_band_depth")
    assert arr.dtype == np.float32
    assert arr.mask[0].all() and not arr.mask[1:].any()
    assert (arr.data[0] == np.float32(-9999.0)).all()
    assert arr[1, 1] == pytest.approx(0.01 * (1 * 5 + 1))


def test_window_read_returns_window_shape_at_its_origin(make_granule):
    reader, ctx = _open(make_granule("EMIT_L2B_MIN_001_a.nc"))
    with ctx:
        full = reader.read(ctx, "group_1_mineral_id")
        sw = SensorWindow(row0=2, col0=1, height=3, width=2)
        win = reader.read(ctx, "group_1_mineral_id", sw)
        empty = reader.read(ctx, "group_1_mineral_id", SensorWindow(0, 0, 0, 0))
    assert win.shape == (3, 2)
    np.testing.assert_array_equal(win.data, full.data[sw.slices])
    assert empty.shape == (0, 0) and empty.dtype == np.int16


def test_unknown_variable_names_what_exists(make_granule):
    reader, ctx = _open(make_granule("EMIT_L2B_MIN_001_a.nc"))
    with ctx, pytest.raises(KeyError, match="group_1_mineral_id"):
        reader.read(ctx, "nope")


def test_geolocation_is_float64_with_nan_fills(make_granule):
    reader, ctx = _open(make_granule("EMIT_L2B_MIN_001_a.nc"))
    with ctx:
        loc = reader.geolocation(ctx)
    assert isinstance(loc, LocArray)
    for arr in (loc.lat, loc.lon, loc.elev):
        assert arr.dtype == np.float64 and arr.shape == (6, 5)
        assert np.isnan(arr[0, 0]) and np.isfinite(arr[1:]).all()
    assert loc.lat[1, 0] > loc.lat[-1, 0]          # north-up rows


def test_embedded_glt(make_granule):
    reader, ctx = _open(make_granule("EMIT_L2B_MIN_001_a.nc"))
    with ctx:
        glt = reader.glt(ctx)
    assert isinstance(glt, EmbeddedGLT)
    assert glt.data.shape == (8, 7, 3) and glt.data.dtype == np.int32
    assert glt.hit.sum() == 6 * 5
    assert glt.data[..., 0].max() == 5 and glt.data[..., 1].max() == 6   # 1-based
    assert glt.data[0, 0].tolist() == [0, 0, 0]
    assert isinstance(glt.transform, Affine) and glt.transform.c == pytest.approx(-118.5)
    assert glt.transform.e < 0
    assert glt.crs.startswith("GEOGCS")


def test_class_table_from_embedded_group(make_granule):
    reader, ctx = _open(make_granule("EMIT_L2B_MIN_001_a.nc"))
    with ctx:
        table = reader.class_table(ctx, "/mineral_metadata", "index", ["name", "library", "group"])
        assert reader.class_table(ctx, "/no_such_group", "index", []) is None
        with pytest.raises(KeyError, match="bogus"):
            reader.class_table(ctx, "/mineral_metadata", "index", ["bogus"])
    assert isinstance(table, ClassTable)
    assert table.key == "index"
    assert table.entries.column_names == ["index", "name", "library", "group"]
    assert table.entries.column("name").to_pylist() == ["Alunite", "Kaolinite", "Calcite", "Goethite"]
    assert table.entries.column("index").to_pylist() == [1, 2, 3, 4]
    assert table.source.endswith("EMIT_L2B_MIN_001_a.nc:/mineral_metadata")
    assert table.attrs() == ["name", "library", "group"]
    assert table.fingerprint().startswith("sha256:")


def test_minuncert_is_read_by_the_same_reader(make_granule):
    reader, ctx = _open(make_granule("EMIT_L2B_MINUNCERT_001_a.nc", kind="minuncert"))
    with ctx:
        specs = reader.variables(ctx)
        arr = reader.read(ctx, "group_1_band_depth_unc", SensorWindow(1, 0, 2, 5))
        assert reader.class_table(ctx, "/mineral_metadata", "index", ["name"]) is None
    assert set(specs) == {"group_1_band_depth_unc", "group_1_fit",
                          "group_2_band_depth_unc", "group_2_fit"}
    assert arr.shape == (2, 5) and not arr.mask.any()


# --------------------------------------------------------------------------- other collections
def test_l1b_obs_reports_band_names_and_reads_3d(make_granule):
    reader = L1BObs()
    with reader.open(AssetStore().open(str(make_granule("EMIT_L1B_OBS_001_a.nc", kind="obs")))) as ctx:
        specs = reader.variables(ctx)
        cube = reader.read(ctx, "obs", SensorWindow(0, 0, 2, 3))
    assert specs["obs"].shape == (6, 5, 3)
    assert specs["obs"].band_attrs == {"name": ["obs_band_0", "obs_band_1", "obs_band_2"]}
    assert cube.shape == (2, 3, 3)
    assert cube.mask[0].all() and not cube.mask[1].any()
    assert cube[1, 2, 1] == pytest.approx(1 * 5 + 2 + 100)


def test_l2a_mask_reports_mask_band_names(make_granule):
    reader = L2AMask()
    path = make_granule("EMIT_L2A_MASK_001_a.nc", kind="mask", bands=2)
    with reader.open(AssetStore().open(str(path))) as ctx:
        specs = reader.variables(ctx)
        full = reader.read(ctx, "mask")
    assert specs["mask"].band_attrs == {"name": ["mask_band_0", "mask_band_1"]}
    assert full.shape == (6, 5, 2)


def test_frcov_is_ortho_and_refuses_to_read(make_granule):
    reader = L2BFrcov()
    assert reader.space == "ortho"
    with reader.open(AssetStore().open(str(make_granule("EMIT_L2B_FRCOV_001_a.nc", kind="frcov")))) as ctx:
        assert "soil" in reader.variables(ctx)
        assert reader.geolocation(ctx) is None
        assert reader.glt(ctx) is None
        with pytest.raises(NotImplementedError, match="12 section 2"):
            reader.read(ctx, "soil")


# ------------------------------------------------------------------------------------ registry
def test_reader_registry_by_collection_and_override():
    clear_cache()
    r = reader_for("EMITL2BMIN")
    assert isinstance(r, L2BMin) and reader_for("EMITL2BMIN") is r     # cached per process
    assert isinstance(reader_for("EMITL1BOBS"), L1BObs)
    assert isinstance(reader_for("EMITL2AMASK"), L2AMask)
    assert isinstance(reader_for("EMITL2BFRCOV"), L2BFrcov)
    over = reader_for("TETRAPY_L2B", {"TETRAPY_L2B": "stratum_emit.readers:L2BMin"})
    assert isinstance(over, L2BMin) and over is not r
    with pytest.raises(LookupError) as exc:
        reader_for("EMITL9ZZZ")
    assert "EMITL9ZZZ" in str(exc.value) and "EMITL2BMIN" in str(exc.value)


# --------------------------------------------------------------------------------- AssetStore
def test_asset_store_local_forms(tmp_path):
    f = tmp_path / "a.nc"
    f.write_bytes(b"x")
    store = AssetStore()
    for uri in (str(f), f.as_uri()):
        h = store.open(uri, etag=None)
        assert isinstance(h, LocalAsset)
        assert h.uri == uri and h.etag is None
        assert h.path() == f.absolute() and h.vsi() == str(f.absolute())
        assert store.stage(uri) == f.absolute()
        assert store.credentials_for(uri).kind == "none"
    assert to_uri(f) == f.absolute().as_uri()
    with pytest.raises(FileNotFoundError):
        store.open(str(tmp_path / "missing.nc"))
    for uri in ("s3://bucket/key.nc", "https://data.lpdaac.earthdatacloud.nasa.gov/x.nc"):
        with pytest.raises(NotImplementedError, match="12 section 4"):
            store.open(uri)
        with pytest.raises(NotImplementedError, match="12 section 4"):
            store.credentials_for(uri)


# ------------------------------------------------------------------------- reference granule
def test_reference_granule_variables_and_window(ref_granule):
    reader, ctx = _open(ref_granule)
    with ctx:
        specs = reader.variables(ctx)
        assert specs["group_1_mineral_id"].shape == (1664, 1242)          # 11 section 1
        assert specs["group_1_mineral_id"].fill == -9999
        assert specs["group_1_band_depth"].fill == -9999.0
        win = reader.read(ctx, "group_1_mineral_id", SensorWindow(100, 600, 4, 7))
        assert win.shape == (4, 7) and win.dtype == np.int16
        full = reader.read(ctx, "group_1_mineral_id", SensorWindow(0, 0, 1664, 10))
        assert (full.data == 0).any()                                     # class 0 survives


def test_reference_granule_geolocation_glt_and_class_table(ref_granule):
    reader, ctx = _open(ref_granule)
    with ctx:
        loc = reader.geolocation(ctx)
        glt = reader.glt(ctx)
        table = reader.class_table(ctx, "/mineral_metadata", "index",
                                   ["name", "record", "library", "group", "url"])
        gt = ctx.dataset.getncattr("geotransform")
    assert loc.lat.shape == (1664, 1242) and loc.lat.dtype == np.float64
    assert glt.data.shape == (2363, 2309, 3)
    assert glt.data[..., 0].min() >= 0 and glt.data[..., 0].max() == 1242
    assert glt.data[..., 1].max() == 1664
    assert glt.transform == Affine.from_gdal(*[float(g) for g in gt])
    assert table.entries.num_rows == 294
    assert table.entries.column("index").to_pylist() == list(range(1, 295))
    assert table.entries.column_names == ["index", "name", "record", "library", "group", "url"]


def test_trial_minuncert_companion(trial_data):
    """A delivered MINUNCERT file: group_N_band_depth_unc / group_N_fit, same reader."""
    files = sorted(trial_data.glob("EMIT_L2B_MINUNCERT_001_*.nc"))
    if not files:
        pytest.skip("no MINUNCERT granules in trial data")
    reader, ctx = _open(files[0])
    with ctx:
        specs = reader.variables(ctx)
        arr = reader.read(ctx, "group_1_band_depth_unc", SensorWindow(5, 5, 3, 4))
    assert {"group_1_band_depth_unc", "group_1_fit"} <= set(specs)
    assert specs["group_1_band_depth_unc"].fill == -9999.0
    assert arr.shape == (3, 4) and arr.dtype == np.float32
