"""Fixtures resolve from URLs or a local trial directory, never from files in the repo."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def trial_data() -> Path:
    """Granules for trial runs: STRATUM_TRIAL_DATA, else ./trial-data. Skips when empty."""
    d = Path(os.environ.get("STRATUM_TRIAL_DATA", ROOT / "trial-data"))
    if not d.is_dir() or not any(d.glob("*.nc")):
        pytest.skip(f"no trial granules under {d}; set STRATUM_TRIAL_DATA")
    return d


@pytest.fixture(scope="session")
def fixture_url() -> str:
    """Base URL for test fixtures (S3 or https). To be supplied; skips until then."""
    url = os.environ.get("STRATUM_FIXTURE_URL")
    if not url:
        pytest.skip("STRATUM_FIXTURE_URL not set")
    return url


# ---------------------------------------------------------------- reader / index fixtures (additive)
REF_GRANULE = ROOT / "refs" / "EMIT_L2B_MIN_001_20260825T151308_2623710_050.nc"


@pytest.fixture(scope="session")
def ref_granule() -> Path:
    """The observed L2B granule behind 11-types.md; present locally, git-ignored. Skips when
    absent (CLAUDE.md: re-obtain from LP DAAC)."""
    if not REF_GRANULE.is_file():
        pytest.skip(f"reference granule missing: {REF_GRANULE}")
    return REF_GRANULE


@pytest.fixture
def make_granule(tmp_path: Path):
    """Factory for tiny EMIT-shaped NetCDF files, so reader and index tests run without any real
    granule. `kind` selects the layout: `min` (L2B MIN), `minuncert`, `obs`, `mask`, `frcov`.

    MIN layout: `group_N_mineral_id` int16 with row 0 = -9999 (fill), row 1 = 0 (observed,
    nothing identified), the rest 1..; `group_N_band_depth` float32 with row 0 = -9999.0;
    `location/{lat,lon,elev}` float64 with [0, 0] = -9999.0; `location/{glt_x,glt_y}` int32 on a
    small ortho grid with 0 = nodata; `/mineral_metadata` with four entries; ACDD globals."""
    import netCDF4 as nc
    import numpy as np

    def _make(name: str, *, kind: str = "min", start: str = "2026-06-10T10:00:00+0000",
              end: str = "2026-06-10T10:00:16+0000",
              bbox: tuple[float, float, float, float] = (-118.5, 41.2, -117.9, 41.8),
              build: str = "010635", product: str = "V001", shape: tuple[int, int] = (6, 5),
              bands: int = 3, directory: Path | None = None) -> Path:
        path = (directory or tmp_path) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        dt, ct = shape
        oy, ox = dt + 2, ct + 2
        with nc.Dataset(path, "w", format="NETCDF4") as ds:
            ds.createDimension("downtrack", dt)
            ds.createDimension("crosstrack", ct)
            ds.createDimension("bands", bands)
            ds.createDimension("ortho_y", oy)
            ds.createDimension("ortho_x", ox)
            rows = np.arange(dt)[:, None] * np.ones((1, ct), dtype=np.int64)
            cols = np.ones((dt, 1), dtype=np.int64) * np.arange(ct)[None, :]
            if kind in ("min", "minuncert"):
                if kind == "min":
                    for g in (1, 2):
                        mid = ds.createVariable(f"group_{g}_mineral_id", "i2",
                                                ("downtrack", "crosstrack"), fill_value=-9999)
                        ids = (rows * ct + cols + 1).astype(np.int16)
                        ids[0, :] = -9999
                        ids[1, :] = 0
                        mid.units = "unitless"
                        mid[:] = ids
                        bd = ds.createVariable(f"group_{g}_band_depth", "f4",
                                               ("downtrack", "crosstrack"), fill_value=-9999.0)
                        depth = (0.01 * (rows * ct + cols)).astype(np.float32)
                        depth[0, :] = -9999.0
                        bd.units = "unitless"
                        bd[:] = depth
                    ds.createDimension("minerals", 4)
                    mm = ds.createGroup("mineral_metadata")
                    for col, vals in (("index", [1, 2, 3, 4]), ("record", [11, 12, 13, 14]),
                                      ("group", [1, 1, 2, 2])):
                        v = mm.createVariable(col, "u4", ("minerals",))
                        v[:] = np.array(vals, dtype=np.uint32)
                    for col, vals in (("name", ["Alunite", "Kaolinite", "Calcite", "Goethite"]),
                                      ("library", ["s06", "s06", "r06", "r06"]),
                                      ("url", ["u1", "u2", "u3", "u4"])):
                        v = mm.createVariable(col, str, ("minerals",))
                        v[:] = np.array(vals, dtype=object)
                else:
                    for g in (1, 2):
                        for stem in ("band_depth_unc", "fit"):
                            v = ds.createVariable(f"group_{g}_{stem}", "f4",
                                                  ("downtrack", "crosstrack"), fill_value=-9999.0)
                            arr = (0.001 * (rows * ct + cols)).astype(np.float32)
                            arr[0, :] = -9999.0
                            v.units = "unitless"
                            v[:] = arr
            elif kind in ("obs", "mask"):
                var = ds.createVariable(kind, "f4", ("downtrack", "crosstrack", "bands"),
                                        fill_value=-9999.0)
                cube = np.stack([(rows * ct + cols + 100 * b).astype(np.float32)
                                 for b in range(bands)], axis=-1)
                cube[0, :, :] = -9999.0
                var[:] = cube
                sbp = ds.createGroup("sensor_band_parameters")
                names_var = "observation_bands" if kind == "obs" else "mask_bands"
                v = sbp.createVariable(names_var, str, ("bands",))
                v[:] = np.array([f"{kind}_band_{b}" for b in range(bands)], dtype=object)
            elif kind == "frcov":
                var = ds.createVariable("soil", "f4", ("ortho_y", "ortho_x"), fill_value=-9999.0)
                var[:] = np.zeros((oy, ox), dtype=np.float32)
            else:
                raise ValueError(kind)
            if kind != "frcov":
                loc = ds.createGroup("location")
                w, s, e, n = bbox
                lon = np.linspace(w, e, ct)[None, :] * np.ones((dt, 1))
                lat = np.linspace(n, s, dt)[:, None] * np.ones((1, ct))
                for nm, arr, units in (("lon", lon, "degrees east"), ("lat", lat, "degrees north"),
                                       ("elev", np.full((dt, ct), 1500.0), "m")):
                    v = loc.createVariable(nm, "f8", ("downtrack", "crosstrack"), fill_value=-9999.0)
                    a = np.array(arr, dtype=np.float64)
                    a[0, 0] = -9999.0
                    v.units = units
                    v[:] = a
                gx = np.zeros((oy, ox), dtype=np.int32)
                gy = np.zeros((oy, ox), dtype=np.int32)
                gx[1:1 + dt, 1:1 + ct] = np.arange(1, ct + 1)[None, :]
                gy[1:1 + dt, 1:1 + ct] = np.arange(1, dt + 1)[:, None]
                for nm, arr in (("glt_x", gx), ("glt_y", gy)):
                    v = loc.createVariable(nm, "i4", ("ortho_y", "ortho_x"), fill_value=0)
                    v.units = "pixel location"
                    v[:] = arr
            w, s, e, n = bbox
            ds.setncattr("time_coverage_start", start)
            ds.setncattr("time_coverage_end", end)
            ds.setncattr("westernmost_longitude", float(w))
            ds.setncattr("easternmost_longitude", float(e))
            ds.setncattr("southernmost_latitude", float(s))
            ds.setncattr("northernmost_latitude", float(n))
            ds.setncattr("software_build_version", build)
            ds.setncattr("software_delivery_version", build)
            ds.setncattr("product_version", product)
            ds.setncattr("day_night_flag", "Day")
            ds.setncattr("flight_line", f"synthetic_{name}")
            ds.setncattr("spatialResolution", 0.000542232520256367)
            ds.setncattr("geotransform", np.array([w, (e - w) / ox, 0.0, n, 0.0, -(n - s) / oy]))
            ds.setncattr("spatial_ref", 'GEOGCS["WGS 84",DATUM["WGS_1984"]]')
            ds.setncattr("Conventions", "CF-1.13")
        return path

    return _make
