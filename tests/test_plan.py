"""The plan stage on synthetic NetCDF granules: selection, plan-time checks, the vintage check,
the budget gate, work lists (09 sections 4-5, 02 section 3, 12 section 7)."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner
from synthetic_nc import (
    CLASS_TABLE,
    MIN_GLOB,
    OBS_GLOB,
    Scene,
    default_scenes,
    manifest_doc,
    write_manifest,
    write_scene,
    write_scenes,
)

from stratum.cli import main
from stratum.executors import run_all
from stratum.manifest import load_manifest
from stratum.plan import BudgetExceeded, PlanError, plan_run, read_work
from stratum.plan.run import local_source


@pytest.fixture
def root(tmp_path: Path) -> Path:
    write_scenes(tmp_path / "granules")
    return tmp_path


def test_work_lists_and_report(root: Path) -> None:
    result = plan_run(write_manifest(root / "m.yaml"))
    regrid = read_work(result.run_dir, "regrid")
    assert len(regrid) == 3
    assert all(set(it) == {"granule_id", "collection", "asset", "uri", "tile"} for it in regrid)
    assert all(it["uri"].startswith("file://") and it["uri"].endswith(".nc") for it in regrid)
    resolve = read_work(result.run_dir, "resolve")
    june = [it for it in resolve if it["epoch"][0].startswith("2026-06")]
    assert len(june) == 4 and {tuple(it["block"]) for it in june} == {(0, 0), (1, 0), (0, 1), (1, 1)}
    reduce_ = read_work(result.run_dir, "reduce")
    assert all(it["period"] == ["2026-06-01T00:00:00+00:00", "2026-08-01T00:00:00+00:00"]
               for it in reduce_)
    assert read_work(result.run_dir, "publish") == [{"tile": [-2360, 820],
                                                     "period": reduce_[0]["period"]}]
    report = (result.run_dir / "report.md").read_text()
    assert "provides an asset for every role read" in report
    assert "`010635`: 3 granule(s)" in report
    assert "within budget" in report
    doc = result.document
    assert doc["class_tables"]["mineral_1"] and doc["collection_versions"] == {
        "EMITL1BOBS": ["001"], "EMITL2BMIN": ["001"]}


def test_epoch_without_candidates_has_no_items(root: Path) -> None:
    """C is the only July granule; with it in a third epoch nothing lists August."""
    result = plan_run(write_manifest(root / "m.yaml", time={
        "start": "2026-06-01", "end": "2026-09-01", "epoch": "P1M", "deliver": "P3M"}))
    resolve = read_work(result.run_dir, "resolve")
    assert not [it for it in resolve if it["epoch"][0].startswith("2026-08")]
    assert all(len(it["epochs"]) == 2 for it in read_work(result.run_dir, "reduce"))


def test_granule_without_a_needed_role_is_dropped_and_reported(root: Path) -> None:
    """A MIN granule with no OBS lacks `geometry`; resolve would raise on it (12 section 7)."""
    lonely = Scene("20260620T100000_2617020_004", datetime(2026, 6, 20, 10, tzinfo=UTC), r0=5,
                   c0=5)
    _, obs = write_scene(root / "granules", lonely)
    obs.unlink()
    result = plan_run(write_manifest(root / "m.yaml"))
    assert result.counts["granules_queried"] == 4 and result.counts["granules"] == 3
    assert [f["removed"] for f in result.document["filters"]] == [0, 1]   # tile row, asset row
    assert lonely.granule_id not in result.document["context"]["granules"]


def test_mixed_vintage_is_refused_with_fingerprints(root: Path) -> None:
    """02 section 3 / 13 section 3: a granule whose embedded table differs fails the plan
    naming the fingerprints; `classes: source` cannot be relaxed by allow_mixed_vintage because
    there is no enumeration to resolve differing tables into."""
    other = dict(CLASS_TABLE, record=[11, 12, 99])
    odd = Scene("20260615T100000_2616715_005", datetime(2026, 6, 15, 10, tzinfo=UTC), r0=5, c0=5,
                class_table=other)
    write_scene(root / "granules", odd)
    with pytest.raises(PlanError, match="2 different class tables") as e:
        plan_run(write_manifest(root / "m.yaml"))
    assert "sha256:" in str(e.value) and odd.granule_id in str(e.value)
    with pytest.raises(PlanError, match="do not resolve into enumeration"):
        plan_run(write_manifest(root / "m.yaml", allow_mixed_vintage=True,
                                mixed_vintage_reason="testing the relaxation"))


def test_budget_gate(root: Path) -> None:
    over = plan_run(write_manifest(root / "m.yaml", budget={
        "max_tiles": 1, "max_granules": 2, "max_vcpu_hours": 1, "on_exceed": "fail"}))
    assert over.over_budget and over.refused and "3 granules exceed" in over.budget_problems[0]
    with pytest.raises(BudgetExceeded):
        run_all(over.run_dir, workers=1)
    approval = plan_run(write_manifest(root / "m.yaml", run_label="appr", budget={
        "max_tiles": 1, "max_granules": 2, "max_vcpu_hours": 1, "on_exceed": "require_approval"}))
    assert approval.refused
    with pytest.raises(BudgetExceeded, match="stratum approve"):
        run_all(approval.run_dir, workers=1)
    warned = plan_run(write_manifest(root / "m.yaml", run_label="warn", budget={
        "max_tiles": 1, "max_granules": 2, "max_vcpu_hours": 1, "on_exceed": "warn"}))
    assert warned.over_budget and not warned.refused
    r = CliRunner().invoke(main, ["plan", "-m", str(root / "m.yaml")])
    assert r.exit_code == 2 and "OVER BUDGET" in r.output, r.output


def test_plan_time_problems_fail_loudly(root: Path) -> None:
    with pytest.raises(PlanError, match="does not resolve"):
        plan_run(write_manifest(root / "m.yaml", scorer={"ref": "no_such_scorer"}))
    doc_alias = {"view_zenith": {"role": "geometry", "band": 11},
                 "solar_zenith": {"role": "geometry", "band": 4}}
    doc = manifest_doc()
    doc["inputs"]["band_aliases"] = doc_alias
    (root / "bad.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
    with pytest.raises(PlanError, match="outside 'obs''s 11 bands"):
        plan_run(root / "bad.yaml")
    doc = manifest_doc()
    doc["inputs"]["roles"]["mineral_depth"]["var"] = "group_9_band_depth"
    (root / "bad2.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
    with pytest.raises(PlanError, match="group_9_band_depth"):
        plan_run(root / "bad2.yaml")
    with pytest.raises(NotImplementedError, match="12 section 4"):
        plan_run(write_manifest(root / "m.yaml", bucket="s3://somewhere/products"))


def test_empty_selection_is_an_error(root: Path) -> None:
    with pytest.raises(PlanError, match="no granule survives"):
        plan_run(write_manifest(root / "m.yaml", time={
            "start": "2025-06-01", "end": "2025-08-01", "epoch": "P1M", "deliver": "P2M"}))


def test_match_alias_resolves_by_band_name(root: Path) -> None:
    """11 section 5: a `match:` alias selects the band whose reader-reported name matches."""
    doc = manifest_doc()
    doc["inputs"]["band_aliases"]["view_zenith"] = {
        "role": "geometry", "match": {"name": "To-sensor zenith (0 to 90 degrees from zenith)"}}
    (root / "match.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
    result = plan_run(root / "match.yaml")
    assert result.document["context"]["aliases"]["view_zenith"] == {"role": "geometry", "band": 2}


def test_index_build_cli(root: Path) -> None:
    manifest = write_manifest(root / "m.yaml")
    r = CliRunner().invoke(main, ["index", "build", "-m", str(manifest)])
    assert r.exit_code == 0 and "6 row(s), 3 granule(s)" in r.output, r.output
    assert (root / "index.parquet").is_file()
    assert len(default_scenes()) == 3


def test_granule_in_the_gap_between_tiles_is_dropped_and_reported(root: Path) -> None:
    """09 section 4 / 02 section 6: the AOI is its tiles, not the box around them. A granule
    between two distant tiles meets the query bbox but no tile; it is neither inspected, frozen,
    counted nor budgeted, and the drop is a reported row like the missing-asset step."""
    gap = Scene("20260612T100000_2616412_006", datetime(2026, 6, 12, 10, tzinfo=UTC), r0=5,
                c0=500)                                   # lon -117.5: between the two tiles
    write_scene(root / "granules", gap)
    result = plan_run(write_manifest(root / "m.yaml", aoi={"tiles": [[-2360, 820], [-2300, 820]]},
                                     budget={"max_tiles": 2, "max_granules": 100,
                                             "max_vcpu_hours": 1, "on_exceed": "fail"}))
    assert result.counts["granules_queried"] == 4 and result.counts["granules"] == 3
    rows = {f["describe"]: f["removed"] for f in result.document["filters"]}
    assert rows["meets a tile of the AOI"] == 1
    assert gap.granule_id not in result.document["context"]["granules"]
    assert result.document["index"]["granule_count"] == 3
    assert not any(it["granule_id"] == gap.granule_id for it in read_work(result.run_dir, "regrid"))


def test_local_source_pattern_shorthand_and_long_form(root: Path) -> None:
    """12 section 5 documents `pattern:` (one glob); it maps onto the roles' one collection.
    The long form pins a collection version."""
    doc = manifest_doc(scorer={"ref": "synthetic:NadirScorer"})
    doc["inputs"]["source"] = {"kind": "local", "root": "./granules", "pattern": MIN_GLOB}
    doc["inputs"]["roles"] = {k: v for k, v in doc["inputs"]["roles"].items() if k != "geometry"}
    (root / "one.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
    src = local_source(load_manifest(root / "one.yaml"))
    assert src.collections == ("EMITL2BMIN",)
    assert len(list(src.search(collections=["EMITL2BMIN"]))) == 3
    # two collections cannot share one glob
    with pytest.raises(PlanError, match="use patterns"):
        local_source(load_manifest(write_manifest(root / "two.yaml", inputs={
            **manifest_doc()["inputs"],
            "source": {"kind": "local", "root": "./granules", "pattern": MIN_GLOB}})))
    long = manifest_doc()
    long["inputs"]["source"]["patterns"] = {
        "EMITL2BMIN": {"version": "007", "assets": {"MIN": MIN_GLOB}},
        "EMITL1BOBS": {"OBS": OBS_GLOB}}
    (root / "long.yaml").write_text(yaml.safe_dump(long, sort_keys=False))
    records = list(local_source(load_manifest(root / "long.yaml")).search(
        collections=["EMITL2BMIN"]))
    assert {r.attributes["collection_version"] for r in records} == {"007"}


def test_outputs_are_validated_at_plan_time(root: Path) -> None:
    """07 section 3 / 12 section 7: a render the publisher would refuse fails the plan, and the
    plan document carries only the render keys the manifest wrote, so a continuous render is
    never handed the categorical mapper's defaults."""
    outputs = {"bucket": "./out", "render": {
        "mineral_1": {"mapper": "categorical", "on_unmapped": "grey"},
        "depth_1": {"mapper": "continuous", "ramp": "viridis", "domain": [0, 0.5]}}}
    result = plan_run(write_manifest(root / "m.yaml", outputs=outputs))
    assert result.document["outputs"]["render"]["depth_1"] == {
        "mapper": "continuous", "ramp": "viridis", "domain": [0.0, 0.5]}
    assert result.document["outputs"]["render"]["mineral_1"] == {
        "mapper": "categorical", "on_unmapped": "grey"}
    assert "formats" not in result.document["outputs"]          # publish applies the default
    bad = {"bucket": "./out", "render": {"depth_1": {
        "mapper": "continuous", "ramp": "viridis", "domain": [0, 0.5],
        "alpha_from": {"band": "depth_1_agreement", "domain": [0, 1]}}}}
    with pytest.raises(PlanError, match="alpha_from band 'depth_1_agreement'"):
        plan_run(write_manifest(root / "bad.yaml", outputs=bad))
    with pytest.raises(PlanError, match="netcdf"):
        plan_run(write_manifest(root / "nc.yaml", outputs={**outputs, "formats": ["cog", "netcdf"]}))
    with pytest.raises(PlanError, match="aux data is not in this slice"):
        plan_run(write_manifest(root / "aux.yaml", aux={
            "slope": {"uri": "s3://x/slope.tif", "kind": "continuous", "resampling": "bilinear"}}))
