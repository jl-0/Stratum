"""The framework-provided `acquired` source (13 section 2): per-cell provenance - when the winning
look was taken, exact to the second, and through `granules.json` which granule that was."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import rasterio
from synthetic_nc import TILE, default_scenes, manifest_doc, write_manifest, write_scenes

from stratum.executors import run_all
from stratum.manifest import load_manifest
from stratum.manifest.validate import validate_static
from stratum.plan import PlanResult, plan_run
from stratum.types import acquired_seconds

ACQUIRED = {"kind": "continuous", "source": "acquired",
            "aggregate": {"method": "best", "conditional_on": "mineral_1"}}


def _layers(**extra: dict) -> dict:
    layers = manifest_doc()["snapshot"]["layers"]
    return {"name": "e2e-v1", "layers": {**layers, **extra}}


@pytest.fixture(scope="module")
def run(tmp_path_factory: pytest.TempPathFactory) -> PlanResult:
    root = tmp_path_factory.mktemp("acquired")
    write_scenes(root / "granules")
    result = plan_run(write_manifest(root / "m.yaml", snapshot=_layers(acquired_1=ACQUIRED)))
    run_all(result.run_dir, workers=1)
    return result


def _products(result: PlanResult) -> Path:
    return Path(result.document["products_dir"]) / f"{TILE.tx}_{TILE.ty}" / "20260601_20260801"


def test_acquired_is_float64_and_exact_to_the_second(run: PlanResult) -> None:
    with rasterio.open(_products(run) / "acquired_1.tif") as src:
        assert src.dtypes[0] == "float64"
        acquired = src.read(1)
    with rasterio.open(_products(run) / "mineral_1.tif") as src:
        mineral = src.read(1)
    starts = {acquired_seconds(s.start) for s in default_scenes()}
    got = set(np.unique(acquired[np.isfinite(acquired)]).tolist())
    assert got and got <= starts                 # every value IS a scene's start, to the second
    # conditional on the winner: a cell with a delivered class has a time, and nodata has none
    assert np.isfinite(acquired[mineral != 65535]).all()
    assert not np.isfinite(acquired[mineral == 65535]).any()


def test_granules_json_names_the_granule_behind_every_value(run: PlanResult) -> None:
    products = _products(run)
    doc = json.loads((products / "granules.json").read_text())
    by_start = {acquired_seconds(s.start): s.granule_id for s in default_scenes()}
    assert doc["layers"] == ["acquired_1"]
    for row in doc["granules"]:
        assert len(row["granule_ids"]) == 1
        assert by_start[row["acquired"]] in row["granule_ids"][0]
    item = json.loads((products / "item.json").read_text())
    assert item["assets"]["granules"]["href"] == "./granules.json"
    bands = item["assets"]["acquired_1"]["raster:bands"][0]
    assert bands["data_type"] == "float64" and bands["unit"] == "s since 1970-01-01 UTC"


def test_granule_band_names_the_selected_scene_as_a_class(run: PlanResult) -> None:
    """`acquired_1` delivered means `granule_1` delivered: the same cells, as a categorical band
    whose class names ARE the granule ids, so a click in any viewer reads the id itself."""
    products = _products(run)
    with rasterio.open(products / "acquired_1.tif") as src:
        acquired = src.read(1)
    with rasterio.open(products / "granule_1.tif") as src:
        assert src.dtypes[0] == "uint16" and src.nodata == 65535
        granule = src.read(1)
    assert ((granule != 65535) == np.isfinite(acquired)).all()

    table = json.loads((products / "classes.granule_1.json").read_text())
    names = {e["id"]: e["name"] for e in table["entries"]}
    gj = json.loads((products / "granules.json").read_text())
    by_second = {row["acquired"]: row["granule_ids"][0] for row in gj["granules"]}
    for v in np.unique(acquired[np.isfinite(acquired)]):
        ids = np.unique(granule[acquired == v])
        assert ids.size == 1 and names[int(ids[0])] == by_second[float(v)]

    item = json.loads((products / "item.json").read_text())
    classes = item["assets"]["granule_1"]["classification:classes"]
    assert {c["name"] for c in classes} >= set(by_second.values())
    assert (products / "granule_1_legend.json").is_file()
    # the mineral table is still the product's one shared table
    assert (products / "classes.json").is_file()


def test_the_estimate_prices_acquired_at_eight_bytes(run: PlanResult) -> None:
    # 3 layers (uint16 + float32 + float32 = 10 bytes; view_zenith is not stored) + 8
    assert run.document["estimate"]["bytes_per_cell"] == 2 + 4 + 4 + 8


@pytest.mark.parametrize(("layer", "message"), [
    ({"kind": "categorical", "source": "acquired", "classes": "source",
      "aggregate": {"method": "vote"}}, "provided by the framework"),
    ({**ACQUIRED, "bands": [0]}, "provided by the framework"),
    ({**ACQUIRED, "dtype": "float32"}, "provided by the framework"),
])
def test_acquired_must_be_one_continuous_float64_band(tmp_path: Path, layer: dict,
                                                      message: str) -> None:
    m = load_manifest(write_manifest(tmp_path / "m.yaml", snapshot=_layers(acquired_1=layer)))
    assert any(message in p for p in validate_static(m))


def test_acquired_cannot_name_a_role(tmp_path: Path) -> None:
    doc = manifest_doc()
    doc["inputs"]["band_aliases"]["acquired"] = {"role": "geometry", "band": 9}
    path = tmp_path / "m.yaml"
    path.write_text(json.dumps(doc))
    problems = validate_static(load_manifest(path))
    assert any("framework-provided source" in p for p in problems)


def test_a_reducer_delivered_acquisition_gets_its_granules_json(tmp_path: Path) -> None:
    """The regression: `confirmed_mineral_vote` delivers `acquired_1` / `acquired_2` under names
    of its own, so publish - which looked for `source: acquired` layers - wrote no granules.json
    and the bands could not be turned into granule ids. Bands are found by their units now."""
    from dataclasses import dataclass
    from datetime import UTC, datetime

    from stratum.publish import write_granules
    from stratum.types import ACQUIRED_UNITS, BandSpec, GranuleRef

    when = datetime(2025, 3, 24, 22, 10, 17, tzinfo=UTC)
    ref = GranuleRef("20250324T221017_2508314_005", "EMITL2BMIN", when, when,
                     (0.0, 0.0, 1.0, 1.0), {}, "010", "V001", "001", None)

    @dataclass
    class Stack:
        specs: tuple[BandSpec, ...]
        bands: dict[str, np.ndarray]

        def __getitem__(self, name: str) -> np.ndarray:
            return self.bands[name]

    stack = Stack((BandSpec("acquired_2", "float64", "reducer's", units=ACQUIRED_UNITS),
                   BandSpec("depth_2", "float32", "not a time")),
                  {"acquired_2": np.array([[acquired_seconds(when), np.nan]]),
                   "depth_2": np.array([[0.1, 0.2]])})
    path = write_granules(tmp_path, stack, schema=None, granules={ref.granule_id: ref})
    assert path is not None
    doc = json.loads(path.read_text())
    assert doc["layers"] == ["acquired_2"]
    assert doc["granules"][0]["granule_ids"] == [ref.granule_id]


def test_granule_numbering_is_the_runs_so_tiles_mosaic() -> None:
    """Two tiles see different granules, but a granule has ONE number in both - its place in the
    run's sorted granule list - and each tile's table lists only what it holds. A reducer's
    `acquired_2` gets `granule_2` the same way."""
    from datetime import UTC, datetime

    from stratum.publish.period import add_granule_bands
    from stratum.types import ACQUIRED_UNITS, BandSpec, BandStack, GranuleRef

    def ref(gid: str, minute: int) -> GranuleRef:
        when = datetime(2025, 5, 1, 18, minute, tzinfo=UTC)
        return GranuleRef(gid, "EMITL2BMIN", when, when, (0.0, 0.0, 1.0, 1.0), {}, "010",
                          "V001", "001", None)

    run = {r.granule_id: r for r in (ref("G_a", 1), ref("G_b", 2), ref("G_c", 3))}
    t = {gid: acquired_seconds(r.datetime) for gid, r in run.items()}
    specs = (BandSpec("acquired_1", "float64", "", units=ACQUIRED_UNITS),
             BandSpec("acquired_2", "float64", "", units=ACQUIRED_UNITS))
    west = BandStack({"acquired_1": np.array([[t["G_a"], t["G_c"]]]),
                      "acquired_2": np.array([[t["G_c"], np.nan]])}, specs)
    east = BandStack({"acquired_1": np.array([[t["G_c"], t["G_c"]]]),
                      "acquired_2": np.array([[np.nan, np.nan]])}, specs)

    w, w_tables = add_granule_bands(west, run)
    e, e_tables = add_granule_bands(east, run)
    assert [s.name for s in w.specs] == ["acquired_1", "granule_1", "acquired_2", "granule_2"]
    assert w["granule_1"].tolist() == [[1, 3]] and e["granule_1"].tolist() == [[3, 3]]
    assert w["granule_2"].tolist() == [[3, 65535]]
    assert w_tables["granule_1"].entries.to_pydict() == {"id": [0, 1, 3],
                                                        "name": ["none", "G_a", "G_c"]}
    assert e_tables["granule_1"].entries.to_pydict() == {"id": [0, 3], "name": ["none", "G_c"]}
