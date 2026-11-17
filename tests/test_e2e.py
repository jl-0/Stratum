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
    assert regrid[0]["collection"] == "EMITL1BRAD" and regrid[0]["asset"] == "OBS"
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


def test_provenance_pins_the_code_that_ran(e2e: tuple[Path, PlanResult], monkeypatch,
                                           tmp_path: Path) -> None:
    """10 section 2: `code` "pins exactly what ran". Two algorithm version numbers do not - two
    runs from different commits recorded identical provenance - so the record carries the
    commit, and for a cloud run the image digest the deployment was pinned to."""
    from types import SimpleNamespace

    from stratum.executors.local import IMAGE_DIGEST_ENV, code_identity, stratum_commit

    _, result = e2e
    code = read_provenance(result.run_dir)["code"]
    commit = stratum_commit()
    if commit is None:
        pytest.skip("not running from a git checkout")
    assert code["stratum_commit"] == commit
    assert len(commit.removesuffix("-dirty")) == 40
    assert "image_digest" not in code, "a local run executes no image"
    # the content hashes are the plan's own (06 section 3 rule 3), not a second computation
    planned = json.loads((result.run_dir / "plan.json").read_text())["context"]["code"]
    assert planned["resolve"] and planned["reduce"]
    assert (code["resolve_code"], code["reduce_code"]) == (planned["resolve"], planned["reduce"])

    # outside a repository there is no commit to claim
    stray = tmp_path / "pkg" / "module.py"
    stray.parent.mkdir()
    stray.write_text("")
    assert stratum_commit(stray) is None

    monkeypatch.setenv(IMAGE_DIGEST_ENV, "sha256:" + "ab" * 32)
    ctx = SimpleNamespace(regrid_algo_version=3, resolve_code="sha256:r", reduce_code=None)
    assert "image_digest" not in code_identity(ctx, "local")
    aws = code_identity(ctx, "aws")
    assert aws["image_digest"] == "sha256:" + "ab" * 32
    # the content hashes the keys were built from are recorded as the plan carries them
    assert aws["resolve_code"] == "sha256:r" and "reduce_code" not in aws
    assert aws["regrid_algo_version"] == 3


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


def test_a_same_day_replan_does_not_link_tiles_the_new_plan_dropped(tmp_path: Path) -> None:
    """A re-plan on the same day reuses the run directory and the products tree (the run id is
    label + date + manifest hash). After an index rebuild that loses a period's only granule,
    that period's product directory is still on disk from the first run; the collection lists
    what THIS plan publishes, not what the directory happens to hold."""
    write_scenes(tmp_path / "granules")
    manifest = write_manifest(tmp_path / "manifest.yaml", time={
        "start": "2026-06-01", "end": "2026-08-01", "epoch": "P1M", "deliver": "P1M"})
    first = plan_run(manifest)
    run_all(first.run_dir, workers=1)
    products = Path(first.document["products_dir"])

    def linked() -> list[str]:
        doc = json.loads((products / "collection.json").read_text())
        return sorted(ln["href"] for ln in doc["links"] if ln["rel"] == "item")

    tile = f"{TILE.tx}_{TILE.ty}"
    assert linked() == [f"./{tile}/20260601_20260701/item.json",
                        f"./{tile}/20260701_20260801/item.json"]

    for path in (tmp_path / "granules").glob("*20260703T100000*"):    # C, the only July granule
        path.unlink()
    (tmp_path / "index" / "granules.parquet").unlink()
    second = plan_run(manifest)
    assert second.run_dir == first.run_dir
    assert len(read_work(second.run_dir, "publish")) == 1
    run_all(second.run_dir, workers=1)
    assert (products / tile / "20260701_20260801" / "item.json").is_file()   # stale, still there
    assert linked() == [f"./{tile}/20260601_20260701/item.json"]


def test_aux_is_warped_by_the_run_not_by_the_plan(tmp_path: Path) -> None:
    """05 section 5: the plan resolves an aux source to an identity and stops; the run stages
    and warps it before its first stage, onto the tiles it resolves. So a plan - and a dry run
    - writes no aux artifact, and the run's products are masked by the land cover all the same."""
    from affine import Affine

    write_scenes(tmp_path / "granules")
    w, s, e, n = TILE.bounds
    cover = np.full((60, 60), 60, dtype="uint8")              # WorldCover "bare"
    cover[:, :30] = 80                                        # the western half is water
    with rasterio.open(tmp_path / "landcover.tif", "w", driver="GTiff", height=60, width=60,
                       count=1, dtype="uint8", crs="EPSG:4326", nodata=0,
                       transform=Affine((e - w) / 50, 0, w - 5 * (e - w) / 50,
                                        0, -(n - s) / 50, n + 5 * (n - s) / 50)) as dst:
        dst.write(cover, 1)
    manifest = write_manifest(
        tmp_path / "manifest.yaml", run_label="aux",
        aux={"landcover": {"uri": "landcover.tif", "kind": "categorical",
                           "resampling": "nearest"}},
        pixel_mask=[{"ref": "edge_trim", "columns": 7},
                    {"ref": "landcover", "exclude": ["water"]}])
    result = plan_run(manifest)
    aux_dir = result.root / "cache" / "aux"
    assert not list(aux_dir.rglob("*.tif")), "the plan warped an aux source"
    assert "tiles_warped" not in result.document["aux"]

    execution = run_all(result.run_dir, workers=1)
    assert execution["aux"]["sources"] == ["landcover"] and execution["aux"]["warped"] == 1
    assert len(list(aux_dir.rglob("*.tif"))) == 1
    products = next(result.root.glob("products/*/*/*"))
    mineral = band(products, "mineral_1")
    assert (mineral[:, :20] == ND).all(), "water was not masked"
    assert (mineral[:, 30:] != ND).any(), "nothing survived on bare ground"
    assert run_all(result.run_dir, workers=1)["aux"]["warped"] == 0
