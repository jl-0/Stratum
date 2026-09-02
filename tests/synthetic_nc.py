"""Small REAL NetCDF granule pairs shaped like EMIT L2B MIN + L1B OBS, for the end-to-end and
invariant tests (tests/test_e2e.py, tests/test_invariants.py, tests/test_plan.py).

Unlike `tests/synthetic.py`, which fakes the reader, these files go through the real
`stratum_emit` readers, `LocalSource`, the index and the planner. The geometry is the one
`synthetic.py` uses: sensor pixel (i, j) of a scene sits exactly on tile cell (r0 + i, c0 + j),
so the KD-tree regrid is deterministic and every expectation can be stated in tile cells.

Layout of a scene (row 0 of every variable is fill, row 1 of the class band is 0):

    MIN  group_1_mineral_id  int16   (downtrack, crosstrack)      fill -9999
         group_1_band_depth  float32 (downtrack, crosstrack)      fill -9999.0
         /mineral_metadata   index, name, record, library, group, url
    OBS  obs                 float32 (downtrack, crosstrack, 11)  fill -9999.0; band 2 =
                             to-sensor zenith, band 4 = to-sun zenith (0-based)
    both /location/{lat,lon,elev} float64

The default three scenes (A, B in June 2026; C in July) and the manifest `manifest_doc` builds
around them are what the e2e test asserts on; see `default_scenes` for the picture.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import netCDF4 as nc
import numpy as np
import yaml
from synthetic import sensor_loc

from stratum.types import GridDef, TileRef

# tile_size 0.05 at 0.001 deg -> a 50 x 50 tile; block 25 -> four blocks. Tile (-2360, 820)
# is lon [-118, -117.95), lat [41, 41.05): negative tile indices are exercised on purpose.
RES = 0.001
GRID = GridDef("EPSG:4326", (RES, -RES), (-180.0, -90.0), 0.05, block_size=25)
TILE = TileRef(GRID, -2360, 820)

FILL_INT = -9999
FILL_FLOAT = -9999.0
VIEW_ZENITH_BAND = 2
SOLAR_ZENITH_BAND = 4
OBS_BANDS = [
    "Path length (m)", "To-sensor azimuth (0 to 360 degrees CW from N)",
    "To-sensor zenith (0 to 90 degrees from zenith)", "To-sun azimuth (0 to 360 degrees CW from N)",
    "To-sun zenith (0 to 90 degrees from zenith)", "Solar phase", "Slope", "Aspect", "Cosine(i)",
    "UTC Time", "Earth-sun distance (AU)",
]
CLASS_TABLE: dict[str, list[Any]] = {
    "index": [1, 2, 3],
    "name": ["Alunite", "Kaolinite", "Calcite"],
    "record": [11, 12, 13],
    "library": ["s06", "s06", "r06"],
    "group": [1, 1, 1],
    "url": ["u1", "u2", "u3"],
}
MIN_GLOB = "EMIT_L2B_MIN_001_*.nc"
OBS_GLOB = "EMIT_L1B_OBS_001_*.nc"


@dataclass
class Scene:
    """One synthetic granule: a MIN + OBS pair over cells [r0, r0+rows) x [c0, c0+cols)."""

    granule_id: str
    start: datetime
    r0: int
    c0: int
    rows: int = 40
    cols: int = 60
    class_id: int = 1
    view_zenith: float = 20.0
    depth: float = 0.2
    stripe: tuple[int, int] | None = None          # (sensor col, class id)
    unmapped: tuple[int, int, int] | None = None   # (sensor row, sensor col, raw id)
    fill_rows: bool = True
    build: str = "010635"
    class_table: Mapping[str, Sequence[Any]] = field(default_factory=lambda: dict(CLASS_TABLE))

    @property
    def end(self) -> datetime:
        return self.start.replace(second=16)


def default_scenes() -> list[Scene]:
    """A and B overlap in June; C repeats A's footprint in July.

        A  rows 0..39, cols -5..54   class 1, depth 0.2, vz 20, stripe class 2 at sensor col 12
                                     (tile col 7), unmapped raw 9 at sensor (3, 10) = tile (3, 5)
        B  rows 10..49, cols 15..74  class 3, depth 0.3, vz 15   <- most nadir where it overlaps A
        C  rows 0..39, cols -5..54   class 1, depth 0.4, vz 18, stripe as A, July

    With edge_trim 7 on a 60-column detector, A/C are usable on tile cols 2..47 and B on
    22..49.
    """
    return [
        Scene("20260605T100000_2615605_001", datetime(2026, 6, 5, 10, tzinfo=UTC), r0=0, c0=-5,
              class_id=1, view_zenith=20.0, depth=0.2, stripe=(12, 2), unmapped=(3, 10, 9)),
        Scene("20260610T100000_2616210_002", datetime(2026, 6, 10, 10, tzinfo=UTC), r0=10, c0=15,
              class_id=3, view_zenith=15.0, depth=0.3),
        Scene("20260703T100000_2618503_003", datetime(2026, 7, 3, 10, tzinfo=UTC), r0=0, c0=-5,
              class_id=1, view_zenith=18.0, depth=0.4, stripe=(12, 2)),
    ]


def _globals(ds: nc.Dataset, scene: Scene, lat: np.ndarray, lon: np.ndarray) -> None:
    fmt = "%Y-%m-%dT%H:%M:%S+0000"
    ds.setncattr("time_coverage_start", scene.start.strftime(fmt))
    ds.setncattr("time_coverage_end", scene.end.strftime(fmt))
    ds.setncattr("westernmost_longitude", float(lon.min()))
    ds.setncattr("easternmost_longitude", float(lon.max()))
    ds.setncattr("southernmost_latitude", float(lat.min()))
    ds.setncattr("northernmost_latitude", float(lat.max()))
    ds.setncattr("software_build_version", scene.build)
    ds.setncattr("product_version", "V001")
    ds.setncattr("day_night_flag", "Day")
    ds.setncattr("Conventions", "CF-1.13")


def _location(ds: nc.Dataset, lat: np.ndarray, lon: np.ndarray) -> None:
    loc = ds.createGroup("location")
    for name, arr, units in (("lon", lon, "degrees east"), ("lat", lat, "degrees north"),
                             ("elev", np.full(lat.shape, 1500.0), "m")):
        v = loc.createVariable(name, "f8", ("downtrack", "crosstrack"), fill_value=FILL_FLOAT)
        v.units = units
        v[:] = np.asarray(arr, dtype=np.float64)


def write_scene(directory: Path, scene: Scene, tile: TileRef = TILE) -> tuple[Path, Path]:
    """Write the MIN and OBS files of `scene` under `directory`; returns their paths."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    lat, lon = sensor_loc(tile, scene.r0, scene.c0, scene.rows, scene.cols)
    dt, ct = scene.rows, scene.cols

    ids = np.full((dt, ct), scene.class_id, dtype=np.int16)
    if scene.stripe is not None:
        ids[:, scene.stripe[0]] = scene.stripe[1]
    if scene.unmapped is not None:
        r, c, raw = scene.unmapped
        ids[r, c] = raw
    depth = np.full((dt, ct), scene.depth, dtype=np.float32)
    obs = np.zeros((dt, ct, len(OBS_BANDS)), dtype=np.float32)
    obs[..., VIEW_ZENITH_BAND] = scene.view_zenith
    obs[..., SOLAR_ZENITH_BAND] = 40.0
    if scene.fill_rows:
        ids[0, :] = FILL_INT
        ids[1, :] = 0
        depth[0, :] = FILL_FLOAT
        obs[0, :, :] = FILL_FLOAT

    min_path = directory / f"EMIT_L2B_MIN_001_{scene.granule_id}.nc"
    with nc.Dataset(min_path, "w", format="NETCDF4") as ds:
        ds.createDimension("downtrack", dt)
        ds.createDimension("crosstrack", ct)
        v = ds.createVariable("group_1_mineral_id", "i2", ("downtrack", "crosstrack"),
                              fill_value=FILL_INT)
        v.units = "unitless"
        v[:] = ids
        v = ds.createVariable("group_1_band_depth", "f4", ("downtrack", "crosstrack"),
                              fill_value=FILL_FLOAT)
        v.units = "unitless"
        v[:] = depth
        _location(ds, lat, lon)
        n = len(scene.class_table["index"])
        ds.createDimension("minerals", n)
        mm = ds.createGroup("mineral_metadata")
        for col, values in scene.class_table.items():
            if all(isinstance(x, int) for x in values):
                var = mm.createVariable(col, "u4", ("minerals",))
                var[:] = np.asarray(values, dtype=np.uint32)
            else:
                var = mm.createVariable(col, str, ("minerals",))
                var[:] = np.asarray([str(x) for x in values], dtype=object)
        _globals(ds, scene, lat, lon)

    obs_path = directory / f"EMIT_L1B_OBS_001_{scene.granule_id}.nc"
    with nc.Dataset(obs_path, "w", format="NETCDF4") as ds:
        ds.createDimension("downtrack", dt)
        ds.createDimension("crosstrack", ct)
        ds.createDimension("bands", len(OBS_BANDS))
        v = ds.createVariable("obs", "f4", ("downtrack", "crosstrack", "bands"),
                              fill_value=FILL_FLOAT)
        v.units = "unitless"
        v[:] = obs
        sbp = ds.createGroup("sensor_band_parameters")
        names = sbp.createVariable("observation_bands", str, ("bands",))
        names[:] = np.asarray(OBS_BANDS, dtype=object)
        _location(ds, lat, lon)
        _globals(ds, scene, lat, lon)
    return min_path, obs_path


def write_scenes(directory: Path, scenes: Sequence[Scene] | None = None) -> list[tuple[Path, Path]]:
    return [write_scene(directory, s) for s in (scenes if scenes is not None else default_scenes())]


def manifest_doc(*, granules: str = "./granules", bucket: str = "./out", run_label: str = "e2e",
                 block_size: int = 25, scorer: Mapping[str, Any] | None = None,
                 mineral_aggregate: Mapping[str, Any] | None = None,
                 budget: Mapping[str, Any] | None = None,
                 time: Mapping[str, Any] | None = None,
                 tile: TileRef = TILE, **extra: Any) -> dict[str, Any]:
    """The e2e manifest as a document: `classes: source`, `min_view_zenith`, edge_trim 7, two
    monthly epochs delivered as one period, a categorical and a continuous render. Keyword
    arguments override the parts the tests vary."""
    doc: dict[str, Any] = {
        "run_id": run_label,
        "description": "synthetic end-to-end run",
        "grid": {"crs": tile.grid.crs, "resolution": list(tile.grid.resolution),
                 "origin": list(tile.grid.origin), "tile_size": tile.grid.tile_size,
                 "block_size": block_size},
        "aoi": {"tiles": [[tile.tx, tile.ty]]},
        "time": dict(time or {"start": "2026-06-01", "end": "2026-08-01", "epoch": "P1M",
                              "deliver": "P2M"}),
        "inputs": {
            "index_location": "./index",
            "source": {"kind": "local", "root": granules,
                       "patterns": {"EMITL2BMIN": {"MIN": MIN_GLOB},
                                    "EMITL1BOBS": {"OBS": OBS_GLOB}}},
            "roles": {
                "geometry": {"collection": "EMITL1BOBS", "var": "obs"},
                "mineral": {"collection": "EMITL2BMIN", "var": "group_1_mineral_id",
                            "class_table": {"source": "embedded", "path": "/mineral_metadata",
                                            "key": "index",
                                            "attributes": ["name", "record", "library", "group",
                                                           "url"]}},
                "mineral_depth": {"collection": "EMITL2BMIN", "var": "group_1_band_depth"},
            },
            "band_aliases": {"view_zenith": {"role": "geometry", "band": VIEW_ZENITH_BAND},
                             "solar_zenith": {"role": "geometry", "band": SOLAR_ZENITH_BAND}},
        },
        "pixel_mask": [{"ref": "edge_trim", "columns": 7}],
        "scorer": dict(scorer or {"ref": "min_view_zenith"}),
        "snapshot": {
            "name": "e2e-v1",
            "layers": {
                "mineral_1": {"kind": "categorical", "source": "mineral", "classes": "source",
                              "aggregate": dict(mineral_aggregate or {
                                  "method": "vote", "min_count": 1, "ignore": ["none"],
                                  "tie_break": "highest_score"})},
                "depth_1": {"kind": "continuous", "source": "mineral_depth",
                            "aggregate": {"method": "median", "conditional_on": "mineral_1"}},
                "view_zenith": {"kind": "continuous", "source": "view_zenith",
                                "aggregate": {"method": "none"}},
            },
        },
        "outputs": {"bucket": bucket, "formats": ["cog"], "stac": True,
                    "render": {"mineral_1": {"mapper": "categorical", "on_unmapped": "grey"},
                               "depth_1": {"mapper": "continuous", "ramp": "viridis",
                                           "domain": [0, 0.5]}}},
        "budget": dict(budget or {"max_tiles": 1, "max_granules": 100, "max_vcpu_hours": 1,
                                  "on_exceed": "fail"}),
    }
    doc.update(extra)
    return doc


def write_manifest(path: Path, **kwargs: Any) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(manifest_doc(**kwargs), sort_keys=False))
    return path


__all__ = [
    "CLASS_TABLE", "FILL_FLOAT", "FILL_INT", "GRID", "MIN_GLOB", "OBS_BANDS", "OBS_GLOB",
    "SOLAR_ZENITH_BAND", "TILE", "VIEW_ZENITH_BAND", "Scene", "default_scenes", "manifest_doc",
    "write_manifest", "write_scene", "write_scenes",
]
