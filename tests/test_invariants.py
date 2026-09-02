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
    quarters = plan_run(write_manifest(granules / "b25.yaml", run_label="b25", block=25))
    whole = plan_run(write_manifest(granules / "b50.yaml", run_label="b50", block=50))
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
