"""LocalSource and the GeoParquet index (02 sections 2, 3, 5, 6; 12 section 5)."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
import pytest
import shapely

from stratum.access import LocalSource
from stratum.access.sources import granule_id_from, split_pattern, version_token
from stratum.index import (
    INDEX_SCHEMA,
    SCOPE_KEY,
    build_index,
    freeze_index,
    granule_refs,
    index_scope_record,
    merge_index,
    query_index,
    read_index,
    read_index_scope,
    read_index_table,
    role_asset,
    role_uri,
    scope_problems,
    write_index,
)
from stratum.types import GranuleRecord, GranuleRef
from stratum_emit.readers import LOCAL_PATTERNS

MIN = "EMITL2BMIN"
PATTERNS = {MIN: LOCAL_PATTERNS[MIN]}


def _link(src: Path, dst: Path) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.symlink_to(src)
    return dst


# ------------------------------------------------------------------------------ id derivation
def test_granule_id_and_version_from_pattern():
    assert split_pattern("EMIT_L2B_MIN_*.nc") == ("EMIT_L2B_MIN_", ".nc")
    assert split_pattern("sub/**/EMIT_L2B_MIN_*.nc") == ("EMIT_L2B_MIN_", ".nc")
    name = "EMIT_L2B_MIN_001_20260825T151308_2623710_050.nc"
    assert granule_id_from(name, "EMIT_L2B_MIN_*.nc") == "001_20260825T151308_2623710_050"
    assert version_token("001_20260825T151308_2623710_050") == "001"
    assert version_token("abc_001") is None


# ------------------------------------------------------------------------------ LocalSource
def test_local_source_over_reference_granule(tmp_path, ref_granule):
    _link(ref_granule, tmp_path / ref_granule.name)
    src = LocalSource(tmp_path, PATTERNS)
    records = list(src.search(collections=[MIN]))
    assert len(records) == 1
    rec = records[0]
    assert isinstance(rec, GranuleRecord)
    assert rec.collection == MIN
    assert rec.datetime == datetime(2026, 8, 25, 15, 13, 8, tzinfo=UTC)
    assert rec.end_datetime == datetime(2026, 8, 25, 15, 13, 24, tzinfo=UTC)
    assert rec.geometry.bounds == pytest.approx(
        (-44.6449907578288, -25.845294599617095, -43.392975868556846, -24.5639991542513))
    assert rec.attributes["build_version"] == "010635"
    assert rec.attributes["product_version"] == "V001"
    assert rec.attributes["day_night"] == "Day"
    assert rec.attributes["collection_version"] == "001"
    assert rec.attributes["cloud_fraction"] is None
    assert rec.attributes["flight_line"] == "emit20260825t151308_o23710_s003"   # verbatim
    assert "geotransform" not in rec.attributes                                  # not a scalar
    assets = src.assets(rec)
    assert set(assets) == {"MIN"}
    assert assets["MIN"] == (tmp_path / ref_granule.name).absolute().as_uri()
    assert src.checksums(rec) == {}


def test_local_source_groups_assets_and_filters(make_granule, tmp_path):
    root = tmp_path / "granules"
    a = "20260610T100000_2600001_001"
    b = "20260710T100000_2600002_002"
    make_granule(f"EMIT_L2B_MIN_001_{a}.nc", directory=root)
    make_granule(f"EMIT_L2B_MINUNCERT_001_{a}.nc", kind="minuncert", directory=root)
    make_granule(f"EMIT_L2B_MIN_001_{b}.nc", directory=root, start="2026-07-10T10:00:00+0000",
                 end="2026-07-10T10:00:16+0000", bbox=(-112.0, 40.0, -111.5, 40.5), build="010636")
    src = LocalSource(root, PATTERNS)
    recs = {r.attributes["granule_id"]: r for r in src.search(collections=[MIN])}
    assert set(recs) == {a, b}
    by_id = {Path(r.native_id).name: r for r in recs.values()}
    ra = by_id[f"EMIT_L2B_MIN_001_{a}.nc"]
    assert set(src.assets(ra)) == {"MIN", "MINUNCERT"}            # one record, two files
    rb = by_id[f"EMIT_L2B_MIN_001_{b}.nc"]
    assert set(src.assets(rb)) == {"MIN"}                          # MINUNCERT may be missing
    assert rb.attributes["build_version"] == "010636"
    # bbox, time and collection filters
    assert len(list(src.search(collections=[MIN], bbox=(-118, 41, -117, 42)))) == 1
    assert len(list(src.search(collections=[MIN], start=datetime(2026, 7, 1, tzinfo=UTC)))) == 1
    assert len(list(src.search(collections=[MIN], end=datetime(2026, 7, 1)))) == 1  # noqa: DTZ001 naive = UTC
    assert len(list(src.search(collections=[MIN], start=datetime(2026, 6, 10, 10, 0, tzinfo=UTC),
                               end=datetime(2026, 6, 10, 10, 0, 1, tzinfo=UTC)))) == 1
    with pytest.raises(LookupError, match="EMITL1BOBS"):
        list(src.search(collections=["EMITL1BOBS"]))


def test_local_source_long_form_patterns_pin_collection_version(make_granule, tmp_path):
    root = tmp_path / "g"
    make_granule("EMIT_L2B_MIN_001_20260610T100000_2600001_001.nc", directory=root)
    src = LocalSource(root, {MIN: {"version": "002", "assets": LOCAL_PATTERNS[MIN]}})
    (rec,) = src.search(collections=[MIN])
    assert rec.attributes["collection_version"] == "002"


# ------------------------------------------------------------------------------------ index
def _source(make_granule, tmp_path) -> LocalSource:
    root = tmp_path / "granules"
    make_granule("EMIT_L2B_MIN_001_20260610T100000_2600001_001.nc", directory=root)
    make_granule("EMIT_L2B_MINUNCERT_001_20260610T100000_2600001_001.nc", kind="minuncert",
                 directory=root)
    make_granule("EMIT_L2B_MIN_001_20260710T100000_2600002_002.nc", directory=root,
                 start="2026-07-10T10:00:00+0000", end="2026-07-10T10:00:16+0000",
                 bbox=(-112.0, 40.0, -111.5, 40.5), build="010636")
    return LocalSource(root, PATTERNS)


def test_index_schema_is_geoparquet():
    assert INDEX_SCHEMA.names == [
        "granule_id", "collection", "datetime", "end_datetime", "geometry", "bbox",
        "cloud_fraction", "build_version", "product_version", "collection_version", "day_night",
        "last_seen", "assets", "checksums", "attributes",
    ]
    geo = json.loads(INDEX_SCHEMA.metadata[b"geo"])
    assert geo["version"] == "1.0.0" and geo["primary_column"] == "geometry"
    assert geo["columns"]["geometry"]["encoding"] == "WKB"


def test_index_round_trip_query_and_freeze(make_granule, tmp_path):
    src = _source(make_granule, tmp_path)
    seen = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
    table = build_index(src, collections=[MIN], last_seen=seen)
    assert table.schema.equals(INDEX_SCHEMA, check_metadata=True)
    assert table.num_rows == 2
    assert table.column("cloud_fraction").null_count == 2            # None, not 0.0
    assert table.column("granule_id").to_pylist() == [
        "20260610T100000_2600001_001", "20260710T100000_2600002_002"]

    path = write_index(table, tmp_path / "idx" / "index.parquet")
    assert b"geo" in pq.read_schema(path).metadata
    frame = read_index(path)
    assert isinstance(frame, pd.DataFrame) and len(frame) == 2
    row = frame.set_index("granule_id").loc["20260610T100000_2600001_001"]
    assert isinstance(row["geometry"], shapely.Polygon)
    assert row["geometry"].bounds == pytest.approx((-118.5, 41.2, -117.9, 41.8))
    assert row["bbox"] == pytest.approx((-118.5, 41.2, -117.9, 41.8))
    assert set(row["assets"]) == {"MIN", "MINUNCERT"}
    assert row["assets"]["MIN"].startswith("file://")
    assert row["attributes"]["flight_line"].startswith("synthetic_")
    assert row["attributes"]["spatialResolution"] == "0.000542232520256367"  # stringified
    assert "build_version" not in row["attributes"]                          # promoted
    assert row["build_version"] == "010635" and row["collection_version"] == "001"
    assert row["day_night"] == "Day" and pd.isna(row["cloud_fraction"])
    assert row["datetime"] == pd.Timestamp("2026-06-10T10:00:00Z")
    assert row["last_seen"] == pd.Timestamp(seen)

    # query: bbox, half-open time, collections
    assert len(query_index(frame, bbox=(-118, 41, -117, 42))) == 1
    assert len(query_index(frame, bbox=(-120, 30, -100, 50))) == 2
    assert len(query_index(frame, start=datetime(2026, 7, 1),  # noqa: DTZ001 naive = UTC
                           end=datetime(2026, 8, 1))) == 1  # noqa: DTZ001
    assert len(query_index(frame, start=datetime(2026, 6, 10, 10, 0, 0, tzinfo=UTC),
                           end=datetime(2026, 6, 10, 10, 0, 0, tzinfo=UTC))) == 0
    assert len(query_index(frame, collections=["EMITL1BOBS"])) == 0
    assert len(query_index(frame, collections=[MIN])) == 2

    # freeze: same rows, hashed bytes
    run_dir = tmp_path / "runs" / "r1"
    fpath, digest = freeze_index(query_index(frame, bbox=(-118, 41, -117, 42)), run_dir)
    assert fpath == run_dir / "index.parquet"
    assert digest == "sha256:" + hashlib.sha256(fpath.read_bytes()).hexdigest()
    frozen = read_index(fpath)
    assert len(frozen) == 1 and frozen.loc[0, "granule_id"] == "20260610T100000_2600001_001"
    assert frozen.loc[0, "assets"] == row["assets"]
    assert pd.isna(frozen.loc[0, "cloud_fraction"])
    _, digest2 = freeze_index(frozen, tmp_path / "runs" / "r2")
    assert digest2 == digest                                          # deterministic bytes


def test_local_patterns_match_the_shipped_manifest_globs():
    """06 section 4 / 12 section 5: an index built from LOCAL_PATTERNS must share granule ids -
    the GLT key term - with one built from the example manifests, so the globs are the same."""
    import yaml

    doc = yaml.safe_load(Path("examples/emit-cmr-nevada/manifest.yaml").read_text())
    shipped = doc["inputs"]["source"]["patterns"]
    for collection, assets in shipped.items():
        assert LOCAL_PATTERNS[collection] == assets
    name = "EMIT_L2B_MIN_001_20260825T151308_2623710_050.nc"
    assert granule_id_from(name, LOCAL_PATTERNS[MIN]["MIN"]) == "20260825T151308_2623710_050"


# ------------------------------------------------------------------------------------ scope
def test_index_records_its_scope_and_checks_containment(make_granule, tmp_path):
    """02 section 6 / 12 section 5: the file says what it was built for; a manifest reaching
    outside that - a wider bbox or window, a collection not indexed, another source kind - is
    a named problem, and a scope-less index is one problem of its own."""
    table = build_index(_source(make_granule, tmp_path), collections=[MIN])
    bare = write_index(table, tmp_path / "bare.parquet")
    assert read_index_scope(bare) is None
    assert scope_problems(None, source="cmr", collections=[MIN], bbox=None, start=None,
                          end=None) == [("the index records no build scope; rebuild it with "
                                         "`stratum index build`")]
    scope = index_scope_record(source="cmr", provider="LPCLOUD", collections=[MIN, "EMITL1BRAD"],
                               bbox=(-118, 41, -117, 42),
                               start=datetime(2026, 1, 1, tzinfo=UTC),
                               end=datetime(2026, 9, 1, tzinfo=UTC),
                               built_at=datetime(2026, 9, 2, tzinfo=UTC))
    path = write_index(table, tmp_path / "scoped.parquet", scope)
    assert SCOPE_KEY in pq.read_schema(path).metadata and b"geo" in pq.read_schema(path).metadata
    assert read_index_scope(path) == {
        "source": "cmr", "provider": "LPCLOUD", "collections": ["EMITL1BRAD", MIN],
        "bbox": [-118.0, 41.0, -117.0, 42.0], "start": "2026-01-01T00:00:00+00:00",
        "end": "2026-09-01T00:00:00+00:00", "built_at": "2026-09-02T00:00:00+00:00"}
    assert len(read_index(path)) == 2                                      # rows unaffected
    inside = {"source": "cmr", "collections": [MIN], "bbox": (-117.9, 41.1, -117.2, 41.9),
              "start": datetime(2026, 6, 1, tzinfo=UTC), "end": datetime(2026, 7, 1, tzinfo=UTC)}
    assert scope_problems(read_index_scope(path), **inside) == []
    assert scope_problems(read_index_scope(path), **{**inside, "source": None}) == []
    wider = scope_problems(read_index_scope(path), **{**inside, "end": datetime(2026, 10, 1, tzinfo=UTC)})
    assert wider == [("time.end 2026-10-01T00:00:00+00:00 is after the indexed end "
                      "2026-09-01T00:00:00+00:00")]
    assert scope_problems(read_index_scope(path), **{**inside, "bbox": (-119, 41, -117, 42)})[0]\
        .startswith("AOI bbox")
    assert scope_problems(read_index_scope(path), **{**inside, "start": datetime(2025, 12, 31, tzinfo=UTC)})[0]\
        .startswith("time.start")
    assert scope_problems(read_index_scope(path), **{**inside, "collections": [MIN, "EMITL2AMASK"]}) == [
        f"collections ['EMITL2AMASK'] were not indexed (it holds ['EMITL1BRAD', '{MIN}'])"]
    assert scope_problems(read_index_scope(path), **{**inside, "source": "local"}) == [
        "built from a 'cmr' source, the manifest names 'local'"]
    # an unscoped (local) record contains every request
    local = index_scope_record(source="local", collections=[MIN])
    assert local["bbox"] is None and local["start"] is None and local["end"] is None
    assert scope_problems(local, **{**inside, "source": "local", "bbox": (-180, -90, 180, 90)}) == []


def test_merge_index_replaces_revised_rows_and_keeps_the_rest(make_granule, tmp_path):
    """12 section 5: a `--since` refresh replaces rows by (collection, collection_version,
    granule_id) and keeps every other row; the replaced rows carry the new last_seen."""
    src = _source(make_granule, tmp_path)
    old = build_index(src, collections=[MIN], last_seen=datetime(2026, 9, 1, tzinfo=UTC))
    assert old.num_rows == 2
    delta = build_index(src, collections=[MIN], last_seen=datetime(2026, 9, 3, tzinfo=UTC),
                        start=datetime(2026, 7, 1, tzinfo=UTC))        # the July granule only
    assert delta.num_rows == 1
    merged = read_index(write_index(merge_index(read_index_table(write_index(old, tmp_path / "o.parquet")),
                                                delta), tmp_path / "m.parquet"))
    assert len(merged) == 2
    seen = merged.set_index("granule_id")["last_seen"]
    assert seen["20260610T100000_2600001_001"] == pd.Timestamp("2026-09-01", tz="UTC")
    assert seen["20260710T100000_2600002_002"] == pd.Timestamp("2026-09-03", tz="UTC")
    assert merge_index(old, delta.slice(0, 0)).num_rows == 2            # an empty delta is a no-op


# ----------------------------------------------------------------------------- GranuleRef
def test_granule_refs_merge_min_and_minuncert_and_resolve_roles(make_granule, tmp_path):
    frame = read_index(write_index(build_index(_source(make_granule, tmp_path), collections=[MIN]),
                                   tmp_path / "index.parquet"))
    refs = granule_refs(frame)
    assert set(refs) == {"20260610T100000_2600001_001", "20260710T100000_2600002_002"}
    ref = refs["20260610T100000_2600001_001"]
    assert isinstance(ref, GranuleRef)
    assert ref.collection == MIN
    assert set(ref.assets) == {f"{MIN}/MIN", f"{MIN}/MINUNCERT"}
    assert ref.attributes["collections"] == MIN
    assert ref.build_version == "010635" and ref.product_version == "V001"
    assert ref.collection_version == "001" and ref.cloud_fraction is None
    assert ref.day_night == "Day" and ref.datetime == datetime(2026, 6, 10, 10, 0, tzinfo=UTC)

    class Role:
        def __init__(self, collection, asset=None):
            self.collection, self.asset = collection, asset

    assert role_uri(ref, Role(MIN)) == ref.assets[f"{MIN}/MIN"]                  # primary
    assert role_uri(ref, Role(MIN, "MINUNCERT")) == ref.assets[f"{MIN}/MINUNCERT"]
    assert role_uri(ref, {"collection": MIN, "asset": "MINUNCERT"}) == ref.assets[f"{MIN}/MINUNCERT"]
    assert role_uri(ref, Role(MIN, "NOPE")) is None
    assert role_uri(ref, Role("EMITL1BOBS")) is None
    other = refs["20260710T100000_2600002_002"]
    assert role_uri(other, Role(MIN, "MINUNCERT")) is None                      # file absent


def test_granule_refs_merge_across_collections(make_granule, tmp_path):
    """Two collections sharing a granule id collapse to one ref keyed by the first collection."""
    root = tmp_path / "granules"
    gid = "20260610T100000_2600001_001"
    make_granule(f"EMIT_L2B_MIN_001_{gid}.nc", directory=root)
    make_granule(f"EMIT_L1B_OBS_001_{gid}.nc", kind="obs", directory=root)
    patterns = {MIN: LOCAL_PATTERNS[MIN], "EMITL1BRAD": LOCAL_PATTERNS["EMITL1BRAD"]}
    src = LocalSource(root, patterns)
    table = build_index(src, collections=["EMITL1BRAD", MIN])
    assert table.num_rows == 2
    frame = read_index(write_index(table, tmp_path / "index.parquet"))
    refs = granule_refs(frame)
    assert list(refs) == [gid]
    ref = refs[gid]
    assert ref.collection == "EMITL1BRAD"                       # first alphabetically
    assert ref.attributes["collections"] == f"EMITL1BRAD,{MIN}"
    assert set(ref.assets) == {"EMITL1BRAD/OBS", f"{MIN}/MIN"}
    assert role_uri(ref, {"collection": MIN}) == ref.assets[f"{MIN}/MIN"]
    assert role_uri(ref, {"collection": "EMITL1BRAD", "asset": "OBS"}) == ref.assets["EMITL1BRAD/OBS"]


def test_granule_refs_reference_granule_under_both_asset_names(tmp_path, ref_granule):
    """The 52 MB file is symlinked, not copied, under the MIN and MINUNCERT names."""
    gid = "20260825T151308_2623710_050"
    _link(ref_granule, tmp_path / f"EMIT_L2B_MIN_001_{gid}.nc")
    _link(ref_granule, tmp_path / f"EMIT_L2B_MINUNCERT_001_{gid}.nc")
    src = LocalSource(tmp_path, PATTERNS)
    table = build_index(src, collections=[MIN])
    assert table.num_rows == 1
    frame = read_index(write_index(table, tmp_path / "index.parquet"))
    (ref,) = granule_refs(frame).values()
    assert ref.granule_id == gid and ref.build_version == "010635"
    assert set(ref.assets) == {f"{MIN}/MIN", f"{MIN}/MINUNCERT"}
    assert role_uri(ref, {"collection": MIN, "asset": "MINUNCERT"}).endswith(
        f"EMIT_L2B_MINUNCERT_001_{gid}.nc")
    assert ref.bbox == pytest.approx(
        (-44.6449907578288, -25.845294599617095, -43.392975868556846, -24.5639991542513))


def test_granule_refs_carry_checksums_keyed_like_assets(make_granule, tmp_path):
    """02 section 2 / 12 section 4: the index's `checksums` column reaches the GranuleRef
    under the same `{collection}/{asset}` names as `assets`, and `role_asset` finds the entry a
    role reads. A local source records none, and the ref says so."""
    root = tmp_path / "granules"
    gid = "20260610T100000_2600001_001"
    make_granule(f"EMIT_L2B_MIN_001_{gid}.nc", directory=root)
    frame = read_index(write_index(build_index(LocalSource(root, PATTERNS), collections=[MIN]),
                                   tmp_path / "index.parquet"))
    assert granule_refs(frame)[gid].checksums == {}
    frame["checksums"] = [{"MIN": "sha512:abc"}] * len(frame)     # as a catalogue would fill it
    ref = granule_refs(frame)[gid]
    assert ref.checksums == {f"{MIN}/MIN": "sha512:abc"}
    assert role_asset(ref, {"collection": MIN}) == f"{MIN}/MIN"
    assert role_asset(ref, {"collection": MIN, "asset": "NOPE"}) is None
