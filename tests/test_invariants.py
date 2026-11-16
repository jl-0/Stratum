"""The non-negotiable tests (ADR-0001 section 10, CLAUDE.md), on the synthetic NetCDF run:
seam equivalence, cache-key sensitivity, and the recorded regrid module hash."""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import rasterio
import yaml
from synthetic_nc import TILE, manifest_doc, write_manifest, write_scenes

from stratum.executors import product_key, run_all, run_stage
from stratum.plan import load_run, plan_run, read_work
from stratum.regrid import check_recorded_hash
from stratum.resolve import glt_key_for, snapshot_key


@pytest.fixture(scope="module")
def granules(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("inv")
    write_scenes(root / "granules")
    return root


def _bands(products_dir: Path) -> dict[str, np.ndarray]:
    out = {}
    for tif in sorted(products_dir.rglob("*.tif")):
        with rasterio.open(tif) as src:
            out[tif.name] = src.read()
    return out


def test_seam_equivalence_block_wise_equals_tile_wise(granules: Path) -> None:
    """Invariant 1 (00 section 5): four 25-cell blocks and one 50-cell block deliver
    bit-identical product bands. The two runs share one root, so the GLTs are reused."""
    quarters = plan_run(write_manifest(granules / "b25.yaml", run_label="b25", block_size=25))
    whole = plan_run(write_manifest(granules / "b50.yaml", run_label="b50", block_size=50))
    assert quarters.counts["blocks"] == 4 and whole.counts["blocks"] == 1
    run_all(quarters.run_dir, workers=1)
    whole_exec = run_all(whole.run_dir, workers=1)
    assert whole_exec["cache_hits"]["regrid"] == 3          # geometry is shared
    assert whole_exec["cache_hits"]["resolve"] == 0         # a different window is a new key
    a = _bands(Path(quarters.document["products_dir"]))
    b = _bands(Path(whole.document["products_dir"]))
    assert set(a) == set(b) and len(a) >= 7
    for name in a:
        assert a[name].dtype == b[name].dtype
        assert np.array_equal(a[name], b[name], equal_nan=a[name].dtype.kind == "f"), name


def test_cache_key_sensitivity_scorer_change_keeps_glts(granules: Path) -> None:
    """06 section 2: scorer params or ref change snapshot keys and not GLT keys; an aggregate
    param changes product keys and not snapshot keys."""
    base = {"ref": "synthetic:NadirScorer", "params": {"weight": 1.0}}
    plans = {
        "a": plan_run(write_manifest(granules / "ka.yaml", run_label="ka", scorer=base)),
        "b": plan_run(write_manifest(granules / "kb.yaml", run_label="kb",
                                     scorer={"ref": "synthetic:NadirScorer",
                                             "params": {"weight": 2.0}})),
        "c": plan_run(write_manifest(granules / "kc.yaml", run_label="kc", scorer=base,
                                     mineral_aggregate={"method": "vote", "min_count": 2,
                                                        "ignore": ["none"],
                                                        "tie_break": "highest_score"})),
        "d": plan_run(write_manifest(granules / "kd.yaml", run_label="kd",
                                     scorer={"ref": "min_view_zenith"})),
    }
    run_stage(plans["a"].run_dir, "regrid", workers=1)
    for other in ("b", "c", "d"):
        hits = run_stage(plans[other].run_dir, "regrid", workers=1)
        assert all(r["hit"] for r in hits), other
    runs = {k: load_run(p.run_dir) for k, p in plans.items()}

    def glt_keys(k: str) -> list[str]:
        ctx = runs[k].context
        return [glt_key_for(ctx, runs[k].tile(*it["tile"]), it["granule_id"]).hash
                for it in read_work(plans[k].run_dir, "regrid")]

    def snap_keys(k: str) -> list[str]:
        return [snapshot_key(it, runs[k].context).hash
                for it in read_work(plans[k].run_dir, "resolve")]

    def prod_keys(k: str) -> list[str]:
        return [product_key(it, runs[k])[0].hash for it in read_work(plans[k].run_dir, "reduce")]

    assert glt_keys("a") == glt_keys("b") == glt_keys("c") == glt_keys("d")
    assert snap_keys("a") == snap_keys("c")
    assert prod_keys("a") != prod_keys("c")
    for other in ("b", "d"):
        assert not set(snap_keys("a")) & set(snap_keys(other)), other
        assert not set(prod_keys("a")) & set(prod_keys(other)), other


def test_regrid_algo_version_bumped_with_module_hash() -> None:
    """06 section 3 rule 2: the regrid module's content hash is recorded beside its version; a
    change without a bump (or a bump without a re-record) fails here rather than serving stale
    geometry."""
    ok, message = check_recorded_hash()
    assert ok, message


# One product class per raw row of synthetic_nc.CLASS_TABLE, lumped by `record` alone.
LUMPING = {
    "name": "lump", "version": "1", "match_on": ["record"], "unmapped": "fail",
    "classes": [{"id": 1, "name": "alunite", "members": [{"record": 11}]},
                {"id": 2, "name": "kaolinite", "members": [{"record": 12}]},
                {"id": 3, "name": "calcite", "members": [{"record": 13}]}],
}


def _lumped_manifest(root: Path, label: str, classes: dict[str, Any]) -> Path:
    (root / "classes").mkdir(exist_ok=True)
    (root / "classes" / f"{label}.yaml").write_text(yaml.safe_dump(classes))
    doc = manifest_doc(run_label=label)
    doc["snapshot"]["layers"]["mineral_1"]["classes"] = f"@ref:classes/{label}.yaml"
    path = root / f"{label}.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    return path


def test_lumping_change_invalidates_snapshots_not_glts(granules: Path) -> None:
    """06 section 3 rule 1 / 13 section 6: two enumerations with the SAME ids and names but
    swapped members are different lumpings. The manifest hash, run_id, layers_hash and every
    snapshot key must move; the GLTs must not; and the product must actually differ."""
    swapped = copy.deepcopy(LUMPING)
    a_members, b_members = swapped["classes"][0]["members"], swapped["classes"][1]["members"]
    swapped["classes"][0]["members"], swapped["classes"][1]["members"] = b_members, a_members
    one = plan_run(_lumped_manifest(granules, "la", LUMPING))
    two = plan_run(_lumped_manifest(granules, "lb", swapped))
    assert one.document["manifest_hash"] != two.document["manifest_hash"]
    assert one.run_id != two.run_id
    s1, s2 = one.document["context"]["schema"], two.document["context"]["schema"]
    assert s1["layers"][0]["classes"] == s2["layers"][0]["classes"]     # same product table
    assert s1["layers_hash"] != s2["layers_hash"]                       # different lumping
    run_all(one.run_dir, workers=1)
    hits = run_all(two.run_dir, workers=1)["cache_hits"]
    assert hits["regrid"] == 3 and hits["resolve"] == 0 and hits["reduce"] == 0
    runs = {k: load_run(p.run_dir) for k, p in (("a", one), ("b", two))}
    keys = {k: {snapshot_key(it, r.context).hash for it in read_work(r.run_dir, "resolve")}
            for k, r in runs.items()}
    assert not keys["a"] & keys["b"]
    # the product follows the lumping: raw 1 (record 11) is product 1 under one, 2 under two
    period = f"{TILE.tx}_{TILE.ty}/20260601_20260801/mineral_1.tif"
    with rasterio.open(Path(one.document["products_dir"]) / period) as src:
        assert src.read(1)[5, 5] == 1
    with rasterio.open(Path(two.document["products_dir"]) / period) as src:
        assert src.read(1)[5, 5] == 2
    # and the snapshot sidecar names the raw table it was remapped from (13 section 6)
    ctx = runs["b"].context
    first = snapshot_key(read_work(two.run_dir, "resolve")[0], ctx)
    raw_fps = list(two.document["class_tables"]["mineral_1"])
    assert ctx.cache.explain(first)["class_tables"] == {"mineral_1": raw_fps}


def test_the_plan_records_the_code_its_keys_were_built_under(granules: Path) -> None:
    """06 section 3 rule 3: plugin and framework code enter the keys as content hashes the
    PLANNER computed, and plan.json records them, so a worker reproduces the planner's keys and
    can tell when its own code is not the code that was planned."""
    from importlib.metadata import version

    from stratum.cache import package_hash, reduce_code, resolve_code
    plan = plan_run(write_manifest(granules / "code.yaml", run_label="code",
                                   scorer={"ref": "min_view_zenith"}))
    ctx_doc = plan.document["context"]
    assert ctx_doc["code"] == {"resolve": resolve_code(), "reduce": reduce_code()}
    emit = package_hash("stratum_emit").removeprefix("sha256:")[:16]
    assert ctx_doc["scorer"]["version"] == f"{version('stratum-emit')}+{emit}"
    run = load_run(plan.run_dir)
    assert run.context.resolve_code == ctx_doc["code"]["resolve"]
    snap = snapshot_key(read_work(plan.run_dir, "resolve")[0], run.context)
    assert snap.inputs["resolve_code"] == ctx_doc["code"]["resolve"]
    assert snap.inputs["scorer_version"] == ctx_doc["scorer"]["version"]
    prod = product_key(read_work(plan.run_dir, "reduce")[0], run)[0]
    assert prod.inputs["reduce_code"] == ctx_doc["code"]["reduce"]
    # a reduce-only change re-runs reduce and keeps the snapshots; a resolve change moves both
    import dataclasses
    reduced = dataclasses.replace(run, context=dataclasses.replace(run.context,
                                                                   reduce_code="sha256:other"))
    item = read_work(plan.run_dir, "reduce")[0]
    key, snaps, _ = product_key(item, reduced)
    assert key.hash != prod.hash
    assert [k.hash for k in snaps] == [k.hash for k in product_key(item, run)[1]]


def test_a_worker_refuses_a_plan_made_under_other_code(granules: Path) -> None:
    """06 section 3 rule 3: the plan's code identity is what enters the keys, so a worker running
    OTHER code - a Lambda image built before the plugin was edited - would write old-code
    snapshots under the new code's keys into the shared cache, and every later run would hit
    them. As regrid already does for REGRID_ALGO_VERSION, resolve and reduce refuse first."""
    import json

    from stratum.executors.worker import clear_cache, exec_item
    from stratum.plan import PlanError
    plan = plan_run(write_manifest(granules / "stale.yaml", run_label="stale",
                                   scorer={"ref": "min_view_zenith"}))
    run_stage(plan.run_dir, "regrid", workers=1)
    path = plan.run_dir / "plan.json"
    planned = path.read_text()
    ctx_doc = json.loads(planned)["context"]

    def execute_under(change: dict[str, Any], stage: str = "resolve") -> None:
        doc = json.loads(planned)
        doc["context"].update(change)
        path.write_text(json.dumps(doc))
        clear_cache()
        exec_item(plan.run_dir, stage, 0)

    try:
        # the planner's plugin package differs from the one installed here
        newer = {**ctx_doc["scorer"], "version": ctx_doc["scorer"]["version"][:-1] + "!"}
        with pytest.raises(PlanError, match=r"scorer 'min_view_zenith'.*Re-plan"):
            execute_under({"scorer": newer})
        mask = [{**m, "version": "9.9.9"} for m in ctx_doc["masks"]]
        assert mask, "the synthetic manifest declares a mask"
        with pytest.raises(PlanError, match="mask"):
            execute_under({"masks": mask})
        # the framework's resolve or reduce code differs
        with pytest.raises(PlanError, match="resolve_code"):
            execute_under({"code": {**ctx_doc["code"], "resolve": "sha256:planner"}})
        with pytest.raises(PlanError, match="reduce_code"):
            execute_under({"code": {**ctx_doc["code"], "reduce": "sha256:planner"}}, "reduce")
        # ... and publish refuses too: its code is in reduce_code, and a stale image would
        # otherwise publish without error
        with pytest.raises(PlanError, match="reduce_code"):
            execute_under({"code": {**ctx_doc["code"], "reduce": "sha256:planner"}}, "publish")
        # a plan written before code identity was recorded names none: also refused
        with pytest.raises(PlanError, match="resolve_code"):
            execute_under({"code": {}})
        run = load_run(plan.run_dir)                 # ... and still loads, for `stratum status`
        assert run.context.resolve_code is None
    finally:
        path.write_text(planned)
        clear_cache()
    assert exec_item(plan.run_dir, "resolve", 0)["ok"]      # the matching plan runs
