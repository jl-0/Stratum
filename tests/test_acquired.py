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
