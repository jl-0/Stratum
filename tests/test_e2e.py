"""End to end on synthetic NetCDF granules through the real readers, index, planner, local
executor and publish: plan -> run_all -> products, STAC, provenance (00 section 2)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
import rasterio
from click.testing import CliRunner
from synthetic_nc import TILE, write_manifest, write_scenes

from stratum.cli import main
from stratum.executors import exec_item, run_all
from stratum.plan import PlanResult, load_run, plan_run, read_results, read_work
from stratum.publish import read_provenance

ND = 65535


@pytest.fixture(scope="module")
def e2e(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, PlanResult]:
    """Three granules, one manifest, planned and executed once with a real process pool."""
    root = tmp_path_factory.mktemp("e2e")
    write_scenes(root / "granules")
    manifest = write_manifest(root / "manifest.yaml")
    result = plan_run(manifest)
    run_all(result.run_dir, workers=2)
    return root, result


def band(products: Path, name: str) -> np.ndarray:
    with rasterio.open(products / f"{name}.tif") as src:
        return src.read(1)


def product_dir(result: PlanResult) -> Path:
    d = Path(result.document["products_dir"]) / f"{TILE.tx}_{TILE.ty}" / "20260601_20260801"
    assert d.is_dir(), sorted(Path(result.document["products_dir"]).rglob("*"))
    return d


def test_plan_counts_and_files(e2e: tuple[Path, PlanResult]) -> None:
    root, result = e2e
    assert result.counts == {
        "tiles": 1, "epochs": 2, "periods": 1, "blocks": 4, "granules": 3, "granules_queried": 3,
        "work_regrid": 3, "work_resolve": 8, "work_reduce": 4, "work_publish": 1,
    }
    assert not result.over_budget
    assert result.run_dir == root / "out" / "runs" / result.run_id
    for name in ("manifest.merged.yaml", "index.parquet", "plan.json", "report.md"):
        assert (result.run_dir / name).is_file(), name
    assert (root / "index" / "granules.parquet").is_file(), "plan built the index for the local source"
    assert result.document["index"]["built"] is True
    reduce_items = read_work(result.run_dir, "reduce")
    assert all(len(it["epochs"]) == 2 for it in reduce_items)
    regrid = read_work(result.run_dir, "regrid")
    assert regrid[0]["collection"] == "EMITL1BOBS" and regrid[0]["asset"] == "OBS"
    assert "Class-table fingerprints" in result.report and "| filter |" in result.report


def test_products_hold_the_most_nadir_observation(e2e: tuple[Path, PlanResult]) -> None:
    """B (view zenith 15) beats A (20) where both are usable; C alone in July; edge_trim,
    fill, the `none` class and an unmapped raw class each leave their mark."""
    _, result = e2e
    products = product_dir(result)
    mineral = band(products, "mineral_1")
    n_epochs = band(products, "n_epochs")
    depth = band(products, "depth_1")
    depth_n = band(products, "depth_1_n")
    agreement = band(products, "mineral_1_agreement")
    assert mineral.shape == (50, 50)

    # A in June, C in July, both class 1: unanimous, depth is the median of the two
    assert mineral[5, 5] == 1 and n_epochs[5, 5] == 2 and agreement[5, 5] == 1.0
    assert depth[5, 5] == pytest.approx(0.3, abs=1e-6) and depth_n[5, 5] == 2
    # A and B overlap in June: B's lower view zenith wins the epoch (class 3, depth 0.3);
    # July is C (class 1); the 1-1 tie goes to the highest score, B's -15 over C's -18
    assert mineral[20, 30] == 3 and n_epochs[20, 30] == 2 and agreement[20, 30] == 0.5
    assert depth[20, 30] == pytest.approx(0.3, abs=1e-6) and depth_n[20, 30] == 1
    # tile col 21 is B's sensor col 6: trimmed, so A wins June there and C agrees
    assert mineral[20, 21] == 1 and agreement[20, 21] == 1.0
    # B alone in the south-east; nothing in the south-west
    assert mineral[45, 45] == 3 and n_epochs[45, 45] == 1
    assert mineral[45, 5] == ND and n_epochs[45, 5] == 0 and np.isnan(depth[45, 5])
    # A's unmapped raw 9 at (3, 5) makes A invalid there; C carries the cell alone
    assert mineral[3, 5] == 1 and n_epochs[3, 5] == 1
    # sensor row 1 is class 0 ("none"): a real observation that `ignore: [none]` leaves out
    assert mineral[1, 5] == ND and n_epochs[1, 5] == 2 and agreement[1, 5] == 0.0
    # sensor row 0 is fill: not observed
    assert n_epochs[0, 5] == 0 and mineral[0, 5] == ND
    # the stripe of class 2 at tile col 7, in A and C
    assert mineral[10, 7] == 2 and agreement[10, 7] == 1.0
    # edge_trim 7: tile col 1 is A's sensor col 6 (trimmed), col 2 is sensor col 7 (kept)
    assert n_epochs[5, 1] == 0 and mineral[5, 1] == ND
    assert n_epochs[5, 2] == 2 and mineral[5, 2] == 1


def test_stac_provenance_and_collection(e2e: tuple[Path, PlanResult]) -> None:
    _, result = e2e
    products = product_dir(result)
    item = json.loads((products / "item.json").read_text())
    assert item["id"] == f"{result.run_id}_{TILE.tx}_{TILE.ty}_20260601"
    assert item["properties"]["stratum:manifest_hash"] == result.document["manifest_hash"]
    assert set(item["assets"]) >= {"mineral_1", "mineral_1_agreement", "mineral_1_runner_up",
                                   "depth_1", "depth_1_n", "n_epochs", "mineral_1_rgba",
                                   "mineral_1_legend", "classes"}
    classes = {c["value"]: c["name"] for c in item["assets"]["mineral_1"]["classification:classes"]}
    assert classes == {0: "none", 1: "Alunite", 2: "Kaolinite", 3: "Calcite"}
    rels = {ln["rel"]: ln["href"] for ln in item["links"]}
    assert (products / rels["stratum:frozen-index"]).resolve() == result.run_dir / "index.parquet"
    assert (products / rels["stratum:provenance"]).resolve() == result.run_dir / "provenance.json"
    assert (Path(result.document["products_dir"]) / "collection.json").is_file()
    assert (products / "mineral_1_rgba.tif").is_file()
    assert (products / "mineral_1_legend.json").is_file()
    # the continuous render went through plan.json without the categorical mapper's defaults
    assert (products / "depth_1_rgba.tif").is_file() and (products / "depth_1_legend.json").is_file()
    assert set(result.document["outputs"]["render"]["depth_1"]) == {"mapper", "ramp", "domain"}

    prov = read_provenance(result.run_dir)
    frozen_hash = "sha256:" + hashlib.sha256((result.run_dir / "index.parquet").read_bytes()).hexdigest()
    assert prov["inputs"]["frozen_index_hash"] == frozen_hash == result.document["index"]["hash"]
    assert prov["inputs"]["granule_count"] == 3
    assert prov["inputs"]["build_versions"] == {"010635": 3}
    assert list(prov["inputs"]["class_tables"]) == ["EMITL2BMIN"]
    assert prov["plugins"]["scorer"]["ref"] == "min_view_zenith"
    assert prov["schema"]["layers_hash"] == result.document["context"]["schema"]["layers_hash"]
    assert prov["manifest"]["run_id"] == "e2e"
    assert prov["execution"]["stages"]["publish"]["items"] == 1
    assert [f["removed"] for f in prov["filters"]] == [0, 0]     # tile row, asset row
    assert prov["inputs"]["asset_checksums"] == {}                # a local source has none
    assert "## Execution" in (result.run_dir / "report.md").read_text()


def test_results_files_and_rerun_hits(e2e: tuple[Path, PlanResult]) -> None:
    """Every stage recorded an outcome per item; a second run is all cache hits (08 section 1)."""
    _, result = e2e
    for stage, n in (("regrid", 3), ("resolve", 8), ("reduce", 4), ("publish", 1)):
        results = read_results(result.run_dir, stage)
        assert results is not None and len(results) == n and all(r["ok"] for r in results)
    again = run_all(result.run_dir, workers=1)
    assert again["cache_hits"] == {"regrid": 3, "resolve": 8, "reduce": 4, "publish": 0}
    one = exec_item(result.run_dir, "resolve", 0)
    assert one["ok"] and one["hit"] and Path(one["key"]).is_dir()


def test_plan_json_rebuilds_the_worker_context(e2e: tuple[Path, PlanResult]) -> None:
    _, result = e2e
    run = load_run(result.run_dir)
    ctx = run.context
    assert ctx.schema.layers_hash == result.document["context"]["schema"]["layers_hash"]
    assert set(ctx.granules) == {it["granule_id"] for it in read_work(result.run_dir, "regrid")}
    assert ctx.aliases["view_zenith"].band == 2 and ctx.geolocation_role == "geometry"
    assert ctx.roles_to_read() == ["geometry", "mineral", "mineral_depth"]
    assert ctx.schema["depth_1"].dtype == "float32"
    assert ctx.schema["mineral_1"].classes is not None
    assert run.band_counts == {"depth_1": 1, "view_zenith": 1}
    remaps = ctx.remaps["mineral_1"]
    assert len(remaps) == 3 and remaps[next(iter(remaps))].lookup.tolist() == [0, 1, 2, 3]


def test_cli_surface(e2e: tuple[Path, PlanResult]) -> None:
    root, result = e2e
    runner = CliRunner()
    r = runner.invoke(main, ["validate", "-m", str(root / "manifest.yaml")])
    assert r.exit_code == 0 and "ok" in r.output, r.output
    r = runner.invoke(main, ["status", "--run", str(result.run_dir)])
    assert r.exit_code == 0 and "publish" in r.output, r.output
    r = runner.invoke(main, ["report", "--run", result.run_id, "--root", str(root / "out")])
    assert r.exit_code == 0 and result.run_id in r.output
    r = runner.invoke(main, ["exec", "--plan", str(result.run_dir), "--stage", "regrid",
                             "--index", "0"])
    assert r.exit_code == 0 and json.loads(r.output)["hit"] is True, r.output
    r = runner.invoke(main, ["plugins", "show", "min_view_zenith"])
    assert r.exit_code == 0 and "MinViewZenith" in r.output and "streaming" in r.output
    glt = json.loads(runner.invoke(main, ["exec", "--plan", str(result.run_dir), "--stage",
                                          "regrid", "--index", "1"]).output)["key"]
    r = runner.invoke(main, ["cache", "explain", glt])
    assert r.exit_code == 0 and json.loads(r.output)["artifact_type"] == "glt"
    r = runner.invoke(main, ["index", "query", "--index", str(root / "index"),
                             "--bbox", "-118,41,-117.95,41.05"])
    assert r.exit_code == 0 and r.output.startswith("6 row(s), 3 granule(s)"), r.output
    r = runner.invoke(main, ["approve", "--run", "x"])
    assert r.exit_code == 1 and "not implemented" in r.output
    r = runner.invoke(main, ["plan", "-m", str(root / "manifest.yaml"), "-p", "x"])
    assert r.exit_code == 1 and "09 section 3" in r.output
    r = runner.invoke(main, ["run", "-m", str(root / "manifest.yaml"), "--executor", "slurm"])
    assert r.exit_code == 1 and "08 section" in r.output
