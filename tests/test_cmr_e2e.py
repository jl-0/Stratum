"""End to end from a `kind: cmr` source (12 sections 4-7), offline: `earthaccess.search_data`
is mocked to return UMM-G-shaped records whose `GET DATA` links are `file://` URIs of the
synthetic NetCDF granules, so CMRSource -> index -> plan -> run -> publish exercises the real
code path with the store's local branch standing in for the download. One live test behind
STRATUM_LIVE=1 builds the real index for June 2026 over the Nevada pilot tile.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from click.testing import CliRunner
from synthetic_nc import MIN_GLOB, OBS_GLOB, TILE, default_scenes, manifest_doc, write_scene

from stratum.access import auth as auth_mod
from stratum.access import read_header, to_uri
from stratum.cli import main
from stratum.executors import run_all
from stratum.index import read_index, read_index_scope
from stratum.manifest import load_manifest
from stratum.plan import (
    PlanError,
    PlanResult,
    build_index_from_manifest,
    load_run,
    plan_run,
    read_work,
)
from stratum.publish import read_provenance
from stratum.resolve import observation_inputs

MIN, RAD = "EMITL2BMIN", "EMITL1BRAD"
PATTERNS = {MIN: {"MIN": MIN_GLOB}, RAD: {"OBS": OBS_GLOB}}
NEVADA = (-118.0, 41.0, -117.0, 42.0)
CLOUD = {"20260605T100000_2615605_001": 10, "20260610T100000_2616210_002": 90,
         "20260703T100000_2618503_003": 30}          # percent, as CMR publishes CloudCover


def sha512(path: Path) -> str:
    return hashlib.sha512(path.read_bytes()).hexdigest()


def umm_for(collection: str, gid: str, files: dict[str, Path], extras: dict[str, str]) -> dict:
    """A UMM-G document shaped like an LP DAAC record (tests/fixtures/umm/*.json): the real
    files as `GET DATA` links, a fake s3 twin, a browse PNG and - for RAD - the 1.85 GB primary
    file that no pattern names, so both must be left out of the index."""
    primary = next(iter(files.values()))
    header = read_header(primary)
    ur = f"EMIT_{'L2B_MIN' if collection == MIN else 'L1B_RAD'}_001_{gid}"
    urls: list[dict[str, str]] = []
    archive: list[dict[str, Any]] = []
    for path in [*files.values(), *(Path(v) for v in extras.values())]:
        urls.append({"Type": "GET DATA", "URL": to_uri(path) if path.exists()
                     else f"https://example.invalid/{ur}/{path.name}"})
        urls.append({"Type": "GET DATA VIA DIRECT ACCESS", "URL": f"s3://fake/{ur}/{path.name}"})
        archive.append({"Name": path.name, "SizeInBytes": path.stat().st_size if path.exists()
                        else 1, "Checksum": {"Algorithm": "SHA-512",
                                             "Value": sha512(path) if path.exists() else "0" * 128}})
    urls.append({"Type": "GET DATA", "URL": f"https://example.invalid/{ur}/{ur}.png"})
    start = header["time_coverage_start"].replace("+0000", ".000Z")
    end = header["time_coverage_end"].replace("+0000", ".000Z")
    return {
        "GranuleUR": ur,
        "CollectionReference": {"ShortName": collection, "Version": "001"},
        "TemporalExtent": {"RangeDateTime": {"BeginningDateTime": start, "EndingDateTime": end}},
        "SpatialExtent": {"HorizontalSpatialDomain": {"Geometry": {"BoundingRectangles": [{
            "WestBoundingCoordinate": header["westernmost_longitude"],
            "SouthBoundingCoordinate": header["southernmost_latitude"],
            "EastBoundingCoordinate": header["easternmost_longitude"],
            "NorthBoundingCoordinate": header["northernmost_latitude"]}]}}},
        "CloudCover": CLOUD[gid],
        "DataGranule": {"DayNightFlag": "Day", "ArchiveAndDistributionInformation": archive},
        "AdditionalAttributes": [
            {"Name": "SOFTWARE_BUILD_VERSION", "Values": [header["software_build_version"]]},
            {"Name": "SOLAR_ZENITH", "Values": ["30.5"]}],
        "RelatedUrls": urls,
    }


@pytest.fixture(scope="module")
def cmr_e2e(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, PlanResult, list[dict]]:
    """Three synthetic scenes behind a fake CMR, planned and executed with a real process pool.
    Scene B is 90 % cloudy and is removed by the manifest's cloud filter, which a local source
    could never apply (02 section 4)."""
    import earthaccess

    root = tmp_path_factory.mktemp("cmr_e2e")
    docs: dict[str, list[dict]] = {MIN: [], RAD: []}
    for scene in default_scenes():
        min_path, obs_path = write_scene(root / "granules", scene)
        docs[MIN].append(umm_for(MIN, scene.granule_id, {"MIN": min_path}, {}))
        docs[RAD].append(umm_for(RAD, scene.granule_id, {"OBS": obs_path},
                                 {"RAD": str(obs_path).replace("_OBS_", "_RAD_")}))
    calls: list[dict[str, Any]] = []

    def search_data(count: int = -1, **kwargs: Any) -> list[dict]:
        calls.append(dict(kwargs))
        return [{"umm": d, "meta": {"concept-type": "granule"}}
                for d in docs.get(kwargs["short_name"], [])]

    doc = manifest_doc(run_label="cmr-e2e")
    doc["inputs"]["source"] = {"kind": "cmr", "provider": "LPCLOUD", "prefer": "https",
                               "patterns": PATTERNS}
    doc["granule_filter"] = [{"max_cloud_fraction": 0.5, "on_missing": "fail"}]
    manifest = root / "manifest.yaml"
    manifest.write_text(yaml.safe_dump(doc, sort_keys=False))

    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(earthaccess, "search_data", search_data)
        mp.setattr(earthaccess, "Auth", lambda: (_ for _ in ()).throw(
            AssertionError("no Earthdata login: every asset is file://")))
        auth_mod.reset_login()
        result = plan_run(manifest)
        run_all(result.run_dir, workers=2)
    finally:
        mp.undo()
    return root, result, calls


def test_plan_built_the_index_from_cmr_scoped_to_the_manifest(cmr_e2e) -> None:
    root, result, calls = cmr_e2e
    assert result.document["index"]["built"] is True
    assert [c["short_name"] for c in calls] == [MIN, RAD]
    assert calls[0]["bounding_box"] == pytest.approx(TILE.bounds)
    assert calls[0]["temporal"] == (datetime(2026, 6, 1, tzinfo=UTC),
                                    datetime(2026, 8, 1, tzinfo=UTC))
    frame = read_index(root / "index" / "granules.parquet")
    assert len(frame) == 6 and frame["granule_id"].nunique() == 3
    assert all(u.startswith("file://") for a in frame["assets"] for u in a.values())
    assert all(set(a) == {"MIN"} for a in frame.loc[frame["collection"] == MIN, "assets"])
    assert all(set(a) == {"OBS"} for a in frame.loc[frame["collection"] == RAD, "assets"])
    assert all(c[k].startswith("sha512:") for c in frame["checksums"] for k in c)
    assert set(frame["cloud_fraction"].round(2)) == {0.1, 0.9, 0.3}     # percent -> fraction
    assert (frame["build_version"] == "010635").all()
    # the cloud filter is real here: scene B (90 %) is gone, A and C remain
    assert result.counts["granules_queried"] == 3 and result.counts["granules"] == 2
    filters = {f["describe"]: f["removed"] for f in result.document["filters"]}
    assert filters["cloud_fraction <= 0.5 (on_missing: fail)"] == 1      # ONE granule (B), not its 2 rows
    assert result.document["staging"] == {"asset_cache": str(root / "out" / "assets"),
                                          "assets": 0, "bytes": 0, "uris": []}
    assert "Assets staged at plan time" not in result.report


def test_products_and_provenance_carry_the_asset_identity(cmr_e2e) -> None:
    root, result, _ = cmr_e2e
    products = Path(result.document["products_dir"]) / f"{TILE.tx}_{TILE.ty}" / "20260601_20260801"
    assert (products / "mineral_1.tif").is_file() and (products / "item.json").is_file()
    prov = read_provenance(result.run_dir)
    assert prov["inputs"]["granule_count"] == 2
    sums = prov["inputs"]["asset_checksums"]
    assert set(sums) == {"20260605T100000_2615605_001", "20260703T100000_2618503_003"}
    for gid, per_asset in sums.items():
        assert set(per_asset) == {f"{MIN}/MIN", f"{RAD}/OBS"}
        assert per_asset[f"{MIN}/MIN"] == "sha512:" + sha512(
            root / "granules" / f"EMIT_L2B_MIN_001_{gid}.nc")
        assert per_asset[f"{RAD}/OBS"] == "sha512:" + sha512(
            root / "granules" / f"EMIT_L1B_OBS_001_{gid}.nc")
    assert prov["execution"]["asset_cache"] == str(root / "out" / "assets")
    assert prov["inputs"]["collections"] == {RAD: "001", MIN: "001"}
    # the worker context stages into {root}/assets and every regrid item names a file:// asset
    run = load_run(result.run_dir)
    assert run.context.store.asset_cache == root / "out" / "assets"
    regrid = read_work(result.run_dir, "regrid")
    assert len(regrid) == 2 and all(it["uri"].startswith("file://") for it in regrid)
    assert all(it["collection"] == RAD and it["asset"] == "OBS" for it in regrid)
    # the observation key carries the checksum (12 section 4, 06 section 2): a re-delivered
    # asset changes `asset_roles[role].checksum`, so the obs key and the snapshot key miss
    snapshot = json.loads(next(Path(result.root, "cache", "snapshot").rglob(".inputs.json"))
                          .read_text())
    assert snapshot["artifact_type"] == "snapshot" and snapshot["obs_keys"]
    gid = "20260605T100000_2615605_001"
    obs = observation_inputs(run.context, SimpleNamespace(hash="glt"), run.context.granules[gid])
    assert obs["asset_roles"]["mineral"]["checksum"] == "sha512:" + sha512(
        root / "granules" / f"EMIT_L2B_MIN_001_{gid}.nc")
    assert obs["asset_roles"]["geometry"]["checksum"].startswith("sha512:")
    assert "uri" not in json.dumps(obs) and str(root) not in json.dumps(obs)


def test_index_carries_the_manifest_scope_and_a_wider_manifest_is_refused(cmr_e2e) -> None:
    """02 section 6 / 12 section 5: the scoped CMR index says what it covers; widening
    `time.end` without rebuilding is a PlanError naming the fix, never a quiet under-selection."""
    root, _, _ = cmr_e2e
    scope = read_index_scope(root / "index" / "granules.parquet")
    assert scope["source"] == "cmr" and scope["collections"] == sorted([MIN, RAD])
    assert scope["bbox"] == pytest.approx(TILE.bounds)
    assert scope["start"] == "2026-06-01T00:00:00+00:00" and scope["end"] == "2026-08-01T00:00:00+00:00"
    doc = yaml.safe_load((root / "manifest.yaml").read_text())
    doc["time"]["end"] = "2026-09-01"
    doc["time"]["deliver"] = "P3M"
    wider = root / "wider.yaml"
    wider.write_text(yaml.safe_dump(doc, sort_keys=False))
    with pytest.raises(PlanError, match="does not cover this manifest") as e:
        plan_run(wider)
    assert "time.end" in str(e.value) and "stratum index build" in str(e.value)
    # `--since` refreshes an index in place and cannot widen one either
    with pytest.raises(PlanError, match="cannot widen"):
        build_index_from_manifest(load_manifest(wider), since=datetime(2026, 8, 1, tzinfo=UTC))
    with pytest.raises(PlanError, match="does not exist"):
        build_index_from_manifest(load_manifest(wider), since=datetime(2026, 8, 1, tzinfo=UTC),
                                  path=root / "nowhere" / "granules.parquet")


def test_index_build_cli_reports_collections_and_time_span(cmr_e2e, monkeypatch) -> None:
    import earthaccess

    root, _, _ = cmr_e2e
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(earthaccess, "search_data", lambda **kw: calls.append(kw) or [])
    index = root / "index" / "granules.parquet"
    before = read_index(index)
    # a --since refresh that finds nothing revised keeps every row; `--collection` narrows
    # the query, not the file
    r = CliRunner().invoke(main, ["index", "build", "-m", str(root / "manifest.yaml"),
                                  "--collection", MIN, "--since", "2026-09-01"])
    assert r.exit_code == 0, r.output
    assert [c["short_name"] for c in calls] == [MIN]
    assert calls[0]["revision_date"][0] == datetime(2026, 9, 1, tzinfo=UTC)
    assert len(read_index(index)) == len(before) == 6
    assert read_index_scope(index)["collections"] == sorted([MIN, RAD])    # scope kept
    calls.clear()
    index.unlink()
    r = CliRunner().invoke(main, ["index", "build", "-m", str(root / "manifest.yaml"),
                                  "--collection", MIN])
    assert r.exit_code == 0, r.output
    assert "source: cmr (LPCLOUD)" in r.output and "0 row(s), 0 granule(s)" in r.output
    assert [c["short_name"] for c in calls] == [MIN]
    assert read_index_scope(index)["collections"] == [MIN]                 # what was built


# ------------------------------------------------------------------------------------------ live
@pytest.mark.skipif(os.environ.get("STRATUM_LIVE") != "1", reason="set STRATUM_LIVE=1")
def test_live_cmr_index_for_june_2026_over_the_nevada_tile() -> None:
    """The real archive (verified 2026-09-02): 12 EMITL2BMIN granules whose footprint meets tile
    (-118, 41) in June 2026, each with a MIN and a MINUNCERT asset, an https URL, a SHA-512
    checksum and a cloud fraction. The local trial indexed 14: two more whose header BOUNDING
    BOX touches the tile's corner while their footprint polygon - which CMR searches on - does
    not (20260613T203446_2616413_011, 20260621T172640_2617211_012)."""
    from stratum.access import CMRSource
    from stratum.index import build_index

    src = CMRSource({MIN: {"MIN": "EMIT_L2B_MIN_001_*.nc",
                           "MINUNCERT": "EMIT_L2B_MINUNCERT_001_*.nc"}})
    table = build_index(src, collections=[MIN], bbox=NEVADA,
                        start=datetime(2026, 6, 1, tzinfo=UTC), end=datetime(2026, 7, 1, tzinfo=UTC))
    frame = table.to_pandas()
    assert sorted(frame["granule_id"]) == [
        "20260604T180210_2615512_009", "20260604T180222_2615512_010",
        "20260604T180234_2615512_011", "20260608T162702_2615911_002",
        "20260608T162714_2615911_003", "20260609T220810_2616014_009",
        "20260609T220822_2616014_010", "20260609T220833_2616014_011",
        "20260613T203422_2616413_009", "20260613T203434_2616413_010",
        "20260621T172617_2617211_010", "20260621T172629_2617211_011",
    ]
    for assets, sums, cloud in zip(frame["assets"], frame["checksums"], frame["cloud_fraction"],
                                   strict=True):
        assets, sums = dict(assets), dict(sums)
        assert set(assets) == {"MIN", "MINUNCERT"} == set(sums)
        assert all(u.startswith("https://data.lpdaac.earthdatacloud.nasa.gov/") for u in assets.values())
        assert all(v.startswith("sha512:") and len(v) == 7 + 128 for v in sums.values())
        assert 0.0 <= cloud <= 1.0
    assert not src.skipped
