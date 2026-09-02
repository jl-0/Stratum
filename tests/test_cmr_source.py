"""CMRSource: UMM-G -> index rows through the same path as LocalSource (12 sections 4-6,
02 sections 2, 5). Offline against recorded UMM-G documents under tests/fixtures/umm/
(`record_fixtures.py` regenerates them); one live test behind STRATUM_LIVE=1."""
from __future__ import annotations

import copy
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow.parquet as pq
import pytest
import yaml
from synthetic_nc import manifest_doc

from stratum.access import CMRSource, LocalSource
from stratum.access import auth as auth_mod
from stratum.access.sources import (
    file_matches,
    granule_id_from,
    product_version_of,
    umm_files,
    umm_polygon,
)
from stratum.index import (
    INDEX_SCHEMA,
    build_index,
    granule_refs,
    read_index,
    read_index_scope,
    role_uri,
    write_index,
)
from stratum.manifest import load_manifest
from stratum.plan import PlanError
from stratum.plan.run import (
    build_index_from_manifest,
    index_scope,
    pin_role_versions,
    source_from_manifest,
)
from stratum.types import GranuleRecord

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "umm"
MIN, RAD, MASK = "EMITL2BMIN", "EMITL1BRAD", "EMITL2AMASK"
SCENE = "20260604T180210_2615512_009"
PATTERNS = {
    MIN: {"MIN": "EMIT_L2B_MIN_001_*.nc", "MINUNCERT": "EMIT_L2B_MINUNCERT_001_*.nc"},
    RAD: {"OBS": "EMIT_L1B_OBS_001_*.nc"},
    MASK: {"MASK": "EMIT_L2A_MASK_002_*.nc"},
}
NEVADA = (-118.0, 41.0, -117.0, 42.0)
JUNE = (datetime(2026, 6, 1, tzinfo=UTC), datetime(2026, 7, 1, tzinfo=UTC))


def umm(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text())


class FakeGranule(dict):
    """What earthaccess.search_data yields: a dict with an `umm` key (DataGranule is one)."""

    def __init__(self, doc: dict[str, Any]) -> None:
        super().__init__(umm=doc, meta={"concept-type": "granule"})


@pytest.fixture
def fake_search(monkeypatch):
    """`earthaccess.search_data` answering from the fixtures by short_name; records every call's
    kwargs. Reassign `.docs[short_name]` to vary a record."""
    import earthaccess

    docs = {MIN: [umm("EMITL2BMIN.001")], RAD: [umm("EMITL1BRAD.001")],
            MASK: [umm("EMITL2AMASK.002")]}
    calls: list[dict[str, Any]] = []

    def search_data(count: int = -1, **kwargs: Any) -> list[FakeGranule]:
        calls.append(dict(kwargs))
        return [FakeGranule(copy.deepcopy(d)) for d in docs.get(kwargs["short_name"], [])]

    monkeypatch.setattr(earthaccess, "search_data", search_data)
    search_data.docs = docs          # type: ignore[attr-defined]
    search_data.calls = calls        # type: ignore[attr-defined]
    return search_data


def no_login() -> Any:
    raise AssertionError("earthdata_login must not be called by a CMR search")


def source(**kw: Any) -> CMRSource:
    return CMRSource(PATTERNS, login=no_login, **kw)


# ---------------------------------------------------------------------------- helpers
def test_file_matches_uses_the_glob_basename():
    assert file_matches("EMIT_L2B_MIN_001_x.nc", "EMIT_L2B_MIN_001_*.nc")
    assert file_matches("EMIT_L2B_MIN_001_x.nc", "sub/**/EMIT_L2B_MIN_001_*.nc")
    assert not file_matches("EMIT_L2B_MIN_001_x.png", "EMIT_L2B_MIN_001_*.nc")
    assert not file_matches("EMIT_L2B_MINUNCERT_001_x.nc", "EMIT_L2B_MIN_001_*.nc")


def test_product_version_from_collection_version():
    assert product_version_of("001") == "V001"
    assert product_version_of("002") == "V002"
    assert product_version_of("") == ""


def test_umm_files_joins_links_and_checksums():
    files = umm_files(umm("EMITL2BMIN.001"))
    assert set(files) == {f"EMIT_L2B_MIN_001_{SCENE}.nc", f"EMIT_L2B_MINUNCERT_001_{SCENE}.nc"}
    f = files[f"EMIT_L2B_MIN_001_{SCENE}.nc"]
    assert f["https"].startswith("https://data.lpdaac.earthdatacloud.nasa.gov/lp-prod-protected/")
    assert f["s3"].startswith("s3://lp-prod-protected/")
    assert f["checksum"].startswith("sha512:") and len(f["checksum"]) == len("sha512:") + 128


def test_umm_polygon_and_fallbacks():
    poly = umm_polygon(umm("EMITL2BMIN.001"))
    assert poly.bounds == pytest.approx((-119.2935562133789, 40.57640838623047,
                                         -117.90381622314453, 41.82976531982422))
    rect = {"GranuleUR": "x", "SpatialExtent": {"HorizontalSpatialDomain": {"Geometry": {
        "BoundingRectangles": [{"WestBoundingCoordinate": -1, "SouthBoundingCoordinate": 0,
                                "EastBoundingCoordinate": 1, "NorthBoundingCoordinate": 2}]}}}}
    assert umm_polygon(rect).bounds == (-1.0, 0.0, 1.0, 2.0)
    with pytest.raises(ValueError, match="12 section 5"):
        umm_polygon({"GranuleUR": "x"})


# ---------------------------------------------------------------------------- records
def test_min_record_maps_umm_onto_the_index_row(fake_search):
    src = source()
    (rec,) = src.search(collections=[MIN])
    assert isinstance(rec, GranuleRecord)
    assert rec.native_id == f"EMIT_L2B_MIN_001_{SCENE}"
    assert rec.collection == MIN
    assert rec.datetime == datetime(2026, 6, 4, 18, 2, 10, tzinfo=UTC)
    assert rec.end_datetime == datetime(2026, 6, 4, 18, 2, 22, tzinfo=UTC)
    assert rec.geometry.bounds == pytest.approx((-119.2935562133789, 40.57640838623047,
                                                 -117.90381622314453, 41.82976531982422))
    a = rec.attributes
    assert a["granule_id"] == SCENE
    assert a["build_version"] == "010635"
    assert a["product_version"] == "V001"
    assert a["collection_version"] == "001"
    assert a["day_night"] == "Day"
    assert a["cloud_fraction"] == pytest.approx(0.19) and isinstance(a["cloud_fraction"], float)
    assert a["cloud_cover"] == "19"                                       # the percent, verbatim
    assert a["SOLAR_ZENITH"] == "29.79" and a["ORBIT"] == "2615512"      # verbatim
    assert a["s3:MIN"] == ("s3://lp-prod-protected/EMITL2BMIN.001/"
                           f"EMIT_L2B_MIN_001_{SCENE}/EMIT_L2B_MIN_001_{SCENE}.nc")
    assert a["s3:MINUNCERT"].endswith(f"EMIT_L2B_MINUNCERT_001_{SCENE}.nc")
    assert a["granule_ur"] == rec.native_id
    assets = src.assets(rec)
    assert set(assets) == {"MIN", "MINUNCERT"}                                # one record, two files
    assert assets["MIN"] == ("https://data.lpdaac.earthdatacloud.nasa.gov/lp-prod-protected/"
                             f"EMITL2BMIN.001/EMIT_L2B_MIN_001_{SCENE}/EMIT_L2B_MIN_001_{SCENE}.nc")
    assert not any(u.endswith(".png") for u in assets.values())
    sums = src.checksums(rec)
    assert set(sums) == {"MIN", "MINUNCERT"}
    for digest in sums.values():
        algo, hexed = digest.split(":")
        assert algo == "sha512" and len(hexed) == 128 and hexed == hexed.lower()
        int(hexed, 16)
    assert rec.raw["GranuleUR"] == rec.native_id


def test_granule_id_matches_local_source_for_the_same_file_name(fake_search, make_granule,
                                                                tmp_path):
    """The interchangeability test: the same file name through the same pattern gives the same
    id whichever source derives it (12 section 5)."""
    (rec,) = source().search(collections=[MIN])
    name = f"EMIT_L2B_MIN_001_{SCENE}.nc"
    assert rec.attributes["granule_id"] == granule_id_from(name, PATTERNS[MIN]["MIN"])
    make_granule(name, directory=tmp_path / "g")
    (local,) = LocalSource(tmp_path / "g", {MIN: PATTERNS[MIN]}).search(collections=[MIN])
    assert local.attributes["granule_id"] == rec.attributes["granule_id"] == SCENE


def test_rad_record_indexes_obs_only(fake_search):
    """The 1.85 GB RAD file and the PNG match no pattern and are not indexed."""
    src = source()
    (rec,) = src.search(collections=[RAD])
    assert rec.attributes["granule_id"] == SCENE
    assets = src.assets(rec)
    assert set(assets) == {"OBS"}
    assert assets["OBS"].endswith(f"EMIT_L1B_OBS_001_{SCENE}.nc")
    assert "RAD_001" not in assets["OBS"].rsplit("/", 1)[-1]
    assert set(src.checksums(rec)) == {"OBS"}
    assert {k for k in rec.attributes if k.startswith("s3:")} == {"s3:OBS"}


def test_mask_record_carries_v002(fake_search):
    (rec,) = source().search(collections=[MASK])
    assert rec.attributes["collection_version"] == "002"
    assert rec.attributes["product_version"] == "V002"
    assert rec.attributes["granule_id"] == SCENE


def test_cloud_fraction_is_none_when_cloudcover_absent(fake_search):
    doc = umm("EMITL2BMIN.001")
    del doc["CloudCover"]
    fake_search.docs[MIN] = [doc]
    (rec,) = source().search(collections=[MIN])
    assert rec.attributes["cloud_fraction"] is None
    table = build_index(source(), collections=[MIN])
    assert table.column("cloud_fraction").to_pylist() == [None]


def test_record_without_primary_file_is_skipped_and_counted(fake_search, caplog):
    doc = umm("EMITL2BMIN.001")
    keep = f"EMIT_L2B_MINUNCERT_001_{SCENE}.nc"
    doc["RelatedUrls"] = [u for u in doc["RelatedUrls"] if u["Type"] != "GET DATA"
                          or u["URL"].endswith(keep)]
    fake_search.docs[MIN] = [doc, umm("EMITL2BMIN.001")]
    src = source()
    with caplog.at_level("WARNING"):
        recs = list(src.search(collections=[MIN]))
    assert len(recs) == 1
    assert src.skipped == {MIN: 1}
    assert any("EMIT_L2B_MIN_001_*.nc" in r.getMessage() for r in caplog.records)


def test_search_parameters(fake_search):
    src = source(provider="LPCLOUD")
    since = datetime(2026, 8, 1, tzinfo=UTC)
    list(src.search(collections=[MIN, MASK], bbox=NEVADA, start=JUNE[0], end=JUNE[1],
                    updated_since=since))
    a, b = fake_search.calls
    assert a["short_name"] == MIN and b["short_name"] == MASK
    assert a["provider"] == "LPCLOUD"
    assert a["bounding_box"] == NEVADA
    assert a["temporal"] == JUNE
    assert a["revision_date"] == (since, None)
    assert "version" not in a                       # unpinned: record what CMR returns
    pinned = CMRSource({MIN: {"version": "001", "assets": PATTERNS[MIN]}}, login=no_login)
    list(pinned.search(collections=[MIN]))
    assert fake_search.calls[-1]["version"] == "001"
    assert "temporal" not in fake_search.calls[-1] and "bounding_box" not in fake_search.calls[-1]
    with pytest.raises(LookupError, match="EMITL2BFRCOV"):
        list(src.search(collections=["EMITL2BFRCOV"]))
    # the acquisition-start window is applied here too, as LocalSource applies it
    assert list(src.search(collections=[MIN], start=datetime(2026, 6, 5, tzinfo=UTC))) == []
    assert list(src.search(collections=[MIN], end=datetime(2026, 6, 4, tzinfo=UTC))) == []


def test_prefer_direct_is_refused_naming_12_section_4():
    with pytest.raises(NotImplementedError, match="12 section 4"):
        CMRSource(PATTERNS, prefer="direct", login=no_login)


# ---------------------------------------------------------------------------- the index
def test_build_index_over_fixtures_round_trips_and_resolves_roles(fake_search, tmp_path):
    src = source()
    table = build_index(src, collections=[MIN, RAD, MASK], bbox=NEVADA, start=JUNE[0],
                        end=JUNE[1], last_seen=datetime(2026, 9, 2, tzinfo=UTC))
    assert table.schema.equals(INDEX_SCHEMA, check_metadata=True)
    assert table.num_rows == 3
    path = write_index(table, tmp_path / "index" / "granules.parquet")
    assert b"geo" in pq.read_schema(path).metadata
    frame = read_index(path)
    assert list(frame["collection"]) == sorted([MIN, RAD, MASK])
    assert set(frame["granule_id"]) == {SCENE}
    row = frame[frame["collection"] == MIN].iloc[0]
    assert row["cloud_fraction"] == pytest.approx(0.19)                 # CloudCover 19 % -> 0.19
    assert row["attributes"]["cloud_cover"] == "19"
    assert row["build_version"] == "010635" and row["product_version"] == "V001"
    assert row["collection_version"] == "001" and row["day_night"] == "Day"
    assert row["last_seen"] == pd.Timestamp("2026-09-02", tz="UTC")
    assert set(row["assets"]) == {"MIN", "MINUNCERT"}
    assert row["checksums"]["MIN"].startswith("sha512:")
    assert row["attributes"]["SOFTWARE_BUILD_VERSION"] == "010635"
    assert row["attributes"]["s3:MIN"].startswith("s3://")
    assert "cloud_fraction" not in row["attributes"]                   # promoted, not duplicated
    assert row["geometry"].bounds == pytest.approx(
        (-119.2935562133789, 40.57640838623047, -117.90381622314453, 41.82976531982422))
    assert row["bbox"] == pytest.approx(row["geometry"].bounds)
    mask = frame[frame["collection"] == MASK].iloc[0]
    assert mask["product_version"] == "V002" and mask["collection_version"] == "002"

    refs = granule_refs(frame)
    assert list(refs) == [SCENE]
    ref = refs[SCENE]
    assert ref.attributes["collections"] == f"{RAD},{MASK},{MIN}"
    assert set(ref.assets) == {f"{MIN}/MIN", f"{MIN}/MINUNCERT", f"{RAD}/OBS", f"{MASK}/MASK"}
    assert set(ref.checksums) == set(ref.assets)
    obs = role_uri(ref, {"collection": RAD, "asset": "OBS"})
    assert obs is not None and obs.endswith(f"EMIT_L1B_OBS_001_{SCENE}.nc")
    assert role_uri(ref, {"collection": MIN}).endswith(f"EMIT_L2B_MIN_001_{SCENE}.nc")
    assert role_uri(ref, {"collection": MIN, "asset": "MINUNCERT"}).endswith(
        f"EMIT_L2B_MINUNCERT_001_{SCENE}.nc")
    assert role_uri(ref, {"collection": MASK}).endswith(f"EMIT_L2A_MASK_002_{SCENE}.nc")
    assert ref.checksums[f"{RAD}/OBS"].startswith("sha512:")


# ---------------------------------------------------------------------------- the manifest path
def cmr_manifest(tmp_path: Path, **source_extra: Any) -> Path:
    doc = manifest_doc()
    doc["inputs"]["source"] = {"kind": "cmr", "provider": "LPCLOUD", "prefer": "https",
                               "patterns": {MIN: PATTERNS[MIN], RAD: PATTERNS[RAD]},
                               **source_extra}
    path = tmp_path / "cmr.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    return path


def test_manifest_accepts_patterns_for_kind_cmr(tmp_path):
    m = load_manifest(cmr_manifest(tmp_path))
    assert m.inputs.source.kind == "cmr"
    assert m.inputs.source.patterns[MIN] == PATTERNS[MIN]
    src = source_from_manifest(m)
    assert isinstance(src, CMRSource) and src.collections == (MIN, RAD)
    assert set(index_scope(m, src)) == {"bbox", "start", "end"}
    with pytest.raises(NotImplementedError, match="12 section 4"):
        source_from_manifest(load_manifest(cmr_manifest(tmp_path, prefer="direct")))


def test_cmr_source_without_patterns_is_a_plan_error(tmp_path):
    doc = manifest_doc()
    doc["inputs"]["source"] = {"kind": "cmr", "provider": "LPCLOUD"}
    (tmp_path / "bare.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
    m = load_manifest(tmp_path / "bare.yaml")          # validates: the model does not insist
    with pytest.raises(PlanError, match="patterns"):
        source_from_manifest(m)


def test_role_version_pins_the_search(tmp_path):
    m = load_manifest(cmr_manifest(tmp_path))
    patterns = pin_role_versions(m, {MIN: PATTERNS[MIN], RAD: PATTERNS[RAD]})
    assert patterns == {MIN: PATTERNS[MIN], RAD: PATTERNS[RAD]}                # nothing pinned
    doc = yaml.safe_load((tmp_path / "cmr.yaml").read_text())
    doc["inputs"]["roles"]["mineral"]["version"] = "002"
    (tmp_path / "pinned.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
    m = load_manifest(tmp_path / "pinned.yaml")
    patterns = pin_role_versions(m, {MIN: PATTERNS[MIN], RAD: PATTERNS[RAD]})
    assert patterns[MIN] == {"version": "002", "assets": PATTERNS[MIN]}
    assert patterns[RAD] == PATTERNS[RAD]
    with pytest.raises(PlanError, match="02 section 5"):
        pin_role_versions(m, {MIN: {"version": "001", "assets": PATTERNS[MIN]}})
    doc["inputs"]["roles"]["mineral_depth"]["version"] = "001"
    (tmp_path / "split.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
    with pytest.raises(PlanError, match="one version per run"):
        pin_role_versions(load_manifest(tmp_path / "split.yaml"), {MIN: PATTERNS[MIN]})


def test_build_index_from_manifest_is_scoped_to_aoi_and_time(fake_search, tmp_path,
                                                             monkeypatch):
    """`stratum index build` over a cmr source: the search is scoped to the manifest's AOI bbox
    and [start, end), the index lands at inputs.index_location, and no login happens."""
    import earthaccess

    monkeypatch.setattr(earthaccess, "Auth", lambda: (_ for _ in ()).throw(
        AssertionError("no login on an index build")))
    auth_mod.reset_login()
    m = load_manifest(cmr_manifest(tmp_path))
    path = build_index_from_manifest(m)
    assert path == tmp_path / "index" / "granules.parquet"
    frame = read_index(path)
    assert sorted(frame["collection"]) == [RAD, MIN]
    calls = fake_search.calls
    assert [c["short_name"] for c in calls] == [MIN, RAD]
    assert calls[0]["bounding_box"] == pytest.approx(m.aoi_bbox())
    assert calls[0]["temporal"] == (m.time.start, m.time.end)
    assert "revision_date" not in calls[0]
    assert read_index_scope(path) == {
        "source": "cmr", "provider": "LPCLOUD", "collections": [RAD, MIN],
        "bbox": [float(v) for v in m.aoi_bbox()], "start": m.time.start.isoformat(),
        "end": m.time.end.isoformat(), "built_at": read_index_scope(path)["built_at"]}
    # --since is a refresh: only the revised rows are queried and they REPLACE their earlier
    # versions; the RAD row, not revised, stays (12 section 5)
    first_seen = frame.set_index("collection")["last_seen"]
    since = datetime(2026, 8, 1, tzinfo=UTC)
    build_index_from_manifest(m, since=since, collections=[MIN])
    assert fake_search.calls[-1]["short_name"] == MIN
    assert fake_search.calls[-1]["revision_date"] == (since, None)
    assert len(fake_search.calls) == 3
    after = read_index(path)
    assert sorted(after["collection"]) == [RAD, MIN]
    seen = after.set_index("collection")["last_seen"]
    assert seen[RAD] == first_seen[RAD] and seen[MIN] > first_seen[MIN]
    assert read_index_scope(path)["collections"] == [RAD, MIN]


def test_glob_matching_two_files_of_one_record_is_refused(fake_search):
    """12 section 5: one glob names one file per record, or the CMR and local ids diverge."""
    with pytest.raises(ValueError, match="matches 2 files"):
        list(CMRSource({MIN: {"ANY": "EMIT_L2B_*_001_*.nc"}}, login=no_login)
             .search(collections=[MIN]))


def test_record_carries_file_sizes(fake_search):
    (rec,) = source().search(collections=[MIN])
    assert int(rec.attributes["size:MIN"]) > 1_000_000
    assert int(rec.attributes["size:MINUNCERT"]) > 1_000_000


# ---------------------------------------------------------------------------- login
def test_earthdata_login_tries_netrc_then_environment_and_never_leaks(monkeypatch):
    import earthaccess
    from earthaccess.exceptions import LoginStrategyUnavailable

    secret = "hunter2-very-secret"
    attempts: list[str] = []

    class FakeAuth:
        def __init__(self) -> None:
            self.authenticated = False

        def login(self, strategy: str = "netrc", **_: Any) -> FakeAuth:
            attempts.append(strategy)
            if strategy == "netrc":
                raise LoginStrategyUnavailable(f"no .netrc (password {secret})")
            self.authenticated = True
            return self

        def get_session(self) -> str:
            return "session"

    monkeypatch.setattr(earthaccess, "Auth", FakeAuth)
    auth_mod.reset_login()
    try:
        auth = auth_mod.earthdata_login()
        assert attempts == ["netrc", "environment"]
        assert auth.authenticated
        assert auth_mod.earthdata_login() is auth                  # cached per process
        assert attempts == ["netrc", "environment"]
        assert auth_mod.earthdata_session(auth) == "session"
        assert auth_mod.earthdata_login(force=True) is not auth    # re-login on demand
        assert attempts == ["netrc", "environment"] * 2

        class Failing(FakeAuth):
            def login(self, strategy: str = "netrc", **_: Any) -> FakeAuth:
                attempts.append(strategy)
                raise LoginStrategyUnavailable(f"bad ({secret})")

        monkeypatch.setattr(earthaccess, "Auth", Failing)
        auth_mod.reset_login()
        with pytest.raises(auth_mod.EarthdataLoginError) as info:
            auth_mod.earthdata_login()
        assert secret not in str(info.value)
        assert "netrc (LoginStrategyUnavailable)" in str(info.value)
        assert "environment (LoginStrategyUnavailable)" in str(info.value)
    finally:
        auth_mod.reset_login()


def test_importing_the_access_package_does_not_log_in():
    """auth._auth is only ever set by a call; importing stratum.access sets nothing."""
    auth_mod.reset_login()
    import importlib

    importlib.reload(auth_mod)
    assert auth_mod._auth is None


# ---------------------------------------------------------------------------- live
@pytest.mark.skipif(os.environ.get("STRATUM_LIVE") != "1", reason="STRATUM_LIVE=1 to hit CMR")
def test_live_cmr_search_nevada_june_2026():
    src = CMRSource({MIN: PATTERNS[MIN]}, login=no_login)
    recs = list(src.search(collections=[MIN], bbox=NEVADA, start=JUNE[0], end=JUNE[1]))
    assert len(recs) >= 10
    assert all(r.attributes["granule_id"] for r in recs)
    assert all(set(src.assets(r)) <= {"MIN", "MINUNCERT"} for r in recs)
    assert all(v.startswith("sha512:") for r in recs for v in src.checksums(r).values())
