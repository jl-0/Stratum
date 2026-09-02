"""Spec 09 in executable form: loading, hashing, derived time/area/schema, static validation,
and the built-in granule filters (04 section 2)."""
from __future__ import annotations

import copy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
import yaml
from pydantic import ValidationError

from stratum.filters import (
    ColumnIn,
    FilterError,
    MaxCloudFraction,
    MaxSolarZenith,
    MonthIn,
    apply_filters,
    build_filters,
    granule_level,
)
from stratum.manifest import Manifest, load_manifest, manifest_hash, validate_static
from stratum.types import GridDef

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples/emit-critical-minerals/manifest.yaml"
ARC = 1 / 3600

CLASSES = {
    "name": "t-v1", "version": 1, "match_on": ["library", "record", "group"], "unmapped": "fail",
    "classes": [
        {"id": 1, "name": "goethite", "members": [{"library": "sprlb06", "record": 882, "group": 1}]},
        {"id": 2, "name": "hematite", "members": [{"library": "splib06", "record": 5880, "group": 1}]},
    ],
}


def base_doc() -> dict[str, Any]:
    """A minimal, valid local manifest in the shape of the first slice."""
    return {
        "run_id": "trial",
        "grid": {"crs": "EPSG:4326", "resolution": [ARC, -ARC], "origin": [-180, -90],
                 "tile_size": 1.0, "block_size": 720},
        "aoi": {"bbox": [-117.9, 41.1, -117.2, 41.8]},
        "time": {"start": "2026-06-01", "end": "2026-07-01", "epoch": "P1M"},
        "inputs": {
            "source": {"kind": "local", "root": "./granules",
                       "patterns": {"EMITL2BMIN": {"MIN": "EMIT_L2B_MIN_*.nc"}}},
            "roles": {
                "geometry": {"collection": "EMITL1BRAD", "asset": "OBS", "var": "obs"},
                "mineral": {"collection": "EMITL2BMIN", "var": "group_1_mineral_id",
                            "class_table": {"source": "embedded", "path": "/mineral_metadata",
                                            "key": "index",
                                            "attributes": ["name", "record", "library", "group"]}},
                "mineral_depth": {"collection": "EMITL2BMIN", "var": "group_1_band_depth"},
            },
            "band_aliases": {"view_zenith": {"role": "geometry", "band": 5}},
        },
        "granule_filter": [{"max_cloud_fraction": 0.5, "on_missing": "keep"}],
        "pixel_mask": [{"ref": "edge_trim", "columns": 7}],
        "scorer": {"ref": "min_view_zenith"},
        "snapshot": {
            "name": "t-v1",
            "layers": {
                "mineral_1": {"kind": "categorical", "source": "mineral",
                              "classes": "@ref:classes/t-v1.yaml",
                              "aggregate": {"method": "vote", "min_count": 1, "ignore": ["none"]}},
                "depth_1": {"kind": "continuous", "source": "mineral_depth",
                            "aggregate": {"method": "median", "conditional_on": "mineral_1"}},
                "view_zenith": {"kind": "continuous", "source": "view_zenith",
                                "aggregate": {"method": "none"}},
            },
        },
        "outputs": {"bucket": "./out", "render": {"mineral_1": {"mapper": "categorical"}}},
        "budget": {"max_tiles": 4, "max_granules": 100, "max_vcpu_hours": 2},
    }


def write(tmp_path: Path, doc: dict[str, Any], name: str = "manifest.yaml") -> Path:
    (tmp_path / "classes").mkdir(exist_ok=True)
    (tmp_path / "classes/t-v1.yaml").write_text(yaml.safe_dump(CLASSES))
    p = tmp_path / name
    p.write_text(yaml.safe_dump(doc, sort_keys=False))
    return p


def loaded(tmp_path: Path, mutate=None) -> Manifest:
    doc = base_doc()
    if mutate:
        mutate(doc)
    return load_manifest(write(tmp_path, doc))


# ------------------------------------------------------------------------------- loading and ids
def test_example_manifest_loads_and_derives():
    m = load_manifest(EXAMPLE)
    assert m.run_label == "cm-zones-2026-annual-r3"
    assert m.run_id.startswith("cm-zones-2026-annual-r3-") and len(m.run_id.split("-")[-1]) == 8
    assert len(m.epochs()) == 48 and len(m.delivery_periods()) == 4
    assert m.time.deliver is not None and str(m.time.deliver.window) == "P1Y"
    schema = m.snapshot_schema()
    assert [layer.name for layer in schema.layers] == ["mineral_1", "depth_1", "depth_1_unc",
                                                       "view_zenith"]
    assert schema["mineral_1"].classes is not None and schema["mineral_1"].dtype == "uint16"
    assert schema["mineral_1"].classes.entries.column("name").to_pylist() == [
        "none", "goethite", "hematite", "pyrite"]
    assert schema["depth_1"].aggregate.params == {"unc": "depth_1_unc",
                                                  "conditional_on": "mineral_1", "spread": "iqr"}
    assert m.geolocation_role() == "geometry"
    assert m.aux["snow"].max_age is not None and str(m.aux["snow"].max_age) == "P3D"
    # Every reference resolves (the zone registry included). What remains is exactly what this
    # slice does not run: the aux block, the scorer's required_aux, `formats: [netcdf]`.
    problems = validate_static(m)
    assert problems and all("slice" in p for p in problems), problems
    assert any(p.startswith("aux ['slope', 'snow']") for p in problems), problems
    assert any("'netcdf'" in p for p in problems), problems


def test_synthetic_manifest_is_clean(tmp_path):
    m = loaded(tmp_path)
    assert validate_static(m) == []
    assert m.run_id == f"trial-{manifest_hash(m)[7:15]}"
    assert m.grid_def() == GridDef("EPSG:4326", (ARC, -ARC), (-180.0, -90.0), 1.0, 720)
    assert m.base_dir == tmp_path.resolve()


def test_hash_is_stable_under_key_reordering(tmp_path):
    doc = base_doc()
    shuffled = {k: doc[k] for k in reversed(list(doc))}
    shuffled["inputs"] = {k: doc["inputs"][k] for k in reversed(list(doc["inputs"]))}
    a = load_manifest(write(tmp_path, doc, "a.yaml"))
    b = load_manifest(write(tmp_path, shuffled, "b.yaml"))
    assert manifest_hash(a) == manifest_hash(b) and a.run_id == b.run_id
    # ...and deliver shorthand hashes like its long form
    long = copy.deepcopy(doc)
    long["time"]["deliver"] = {"every": "P1M", "window": "P1M", "align": "exact"}
    assert manifest_hash(load_manifest(write(tmp_path, long, "c.yaml"))) == manifest_hash(a)
    changed = copy.deepcopy(doc)
    changed["scorer"] = {"ref": "min_view_zenith", "params": {"x": 1}}
    assert manifest_hash(load_manifest(write(tmp_path, changed, "d.yaml"))) != manifest_hash(a)


def test_unknown_keys_are_errors_with_locations(tmp_path):
    with pytest.raises(ValidationError) as ei:
        loaded(tmp_path, lambda d: d["grid"].update(tilesize=1.0))
    assert ("grid", "tilesize") == ei.value.errors()[0]["loc"]
    with pytest.raises(ValidationError, match="schema_version"):
        loaded(tmp_path, lambda d: d.update(schema_version="2.0"))


def test_patches_are_refused(tmp_path):
    p = write(tmp_path, base_doc())
    with pytest.raises(NotImplementedError, match="09 section 3"):
        load_manifest(p, patches=[p])


def test_missing_classes_file_fails_at_load(tmp_path):
    def mutate(d):
        d["snapshot"]["layers"]["mineral_1"]["classes"] = "@ref:classes/nope.yaml"
    with pytest.raises(Exception, match="nope.yaml"):
        loaded(tmp_path, mutate)


# ------------------------------------------------------------------------------------------ time
def test_time_rules_are_validated(tmp_path):
    def bad_multiple(d):
        d["time"].update(epoch="P2M", deliver="P13M")
    with pytest.raises(ValidationError, match="whole multiple"):
        loaded(tmp_path, bad_multiple)

    def exact_with_wider_window(d):
        d["time"].update(deliver={"every": "P1M", "window": "P13M"})
    with pytest.raises(ValidationError, match="exact"):
        loaded(tmp_path, exact_with_wider_window)

    def window_shorter(d):
        d["time"].update(deliver={"every": "P3M", "window": "P1M", "align": "trailing"})
    with pytest.raises(ValidationError, match="shorter"):
        loaded(tmp_path, window_shorter)

    with pytest.raises(ValidationError, match="not after"):
        loaded(tmp_path, lambda d: d["time"].update(end="2026-06-01"))


def test_rolling_window_from_manifest(tmp_path):
    def rolling(d):
        d["time"].update(start="2022-01-01", end="2024-01-01",
                         deliver={"every": "P1M", "window": "P13M", "align": "center"})
    m = loaded(tmp_path, rolling)
    periods = m.delivery_periods()
    assert len(periods) == 24 and len(periods[12].epochs) == 13
    assert periods[12].epochs[0].start == datetime(2022, 7, 1, tzinfo=UTC)


# ------------------------------------------------------------------------------------------ area
def test_tiles_from_bbox_and_explicit_tiles(tmp_path):
    m = loaded(tmp_path)
    tiles = m.tiles()
    assert [(t.tx, t.ty) for t in tiles] == [(-118, 41)]
    assert m.aoi_bbox() == (-117.9, 41.1, -117.2, 41.8)
    m2 = loaded(tmp_path, lambda d: d.update(aoi={"bbox": [-118.0, 41.0, -117.0, 42.0]}))
    assert [(t.tx, t.ty) for t in m2.tiles()] == [(-118, 41)]      # edges do not spill over
    m3 = loaded(tmp_path, lambda d: d.update(aoi={"bbox": [-118.5, 41.5, -116.5, 42.5]}))
    assert [(t.tx, t.ty) for t in m3.tiles()] == [(-119, 41), (-118, 41), (-117, 41),
                                                  (-119, 42), (-118, 42), (-117, 42)]
    m4 = loaded(tmp_path, lambda d: d.update(aoi={"tiles": [[-118, 41], [-117, 41]]}))
    assert [(t.tx, t.ty) for t in m4.tiles()] == [(-118, 41), (-117, 41)]
    assert m4.aoi_bbox() == (-118.0, 41.0, -116.0, 42.0)


def test_zones_resolve_through_a_registry(tmp_path):
    (tmp_path / "zones.yaml").write_text(yaml.safe_dump({
        "cuprite-nv": [-117.3, 37.4, -117.1, 37.6], "leadville-co": [-106.4, 39.2, -106.2, 39.3]}))
    m = loaded(tmp_path, lambda d: d.update(
        aoi={"zones": ["cuprite-nv", "leadville-co"], "registry": "zones.yaml"}))
    assert m.aoi_bbox() == (-117.3, 37.4, -106.2, 39.3)
    assert {(t.tx, t.ty) for t in m.tiles()} == {(-118, 37), (-107, 39)}
    assert validate_static(m) == []
    bad = loaded(tmp_path, lambda d: d.update(aoi={"zones": ["atlantis"], "registry": "zones.yaml"}))
    assert any("atlantis" in p for p in validate_static(bad))
    with pytest.raises(ValidationError, match="exactly one"):
        loaded(tmp_path, lambda d: d.update(aoi={"zones": ["cuprite-nv"], "bbox": [0, 0, 1, 1]}))


# -------------------------------------------------------------------------------- static checks
def test_validate_static_catches_a_missing_role(tmp_path):
    m = loaded(tmp_path, lambda d: d["inputs"]["roles"].pop("geometry"))
    problems = validate_static(m)
    assert any("required role 'geometry'" in p for p in problems), problems
    assert any("band_aliases.view_zenith" in p for p in problems), problems


def test_validate_static_catches_cross_references(tmp_path):
    def mutate(d):
        d["inputs"]["band_aliases"]["mineral"] = {"role": "geometry", "band": 1}   # collision
        d["snapshot"]["layers"]["depth_1"]["aggregate"]["conditional_on"] = "view_zenith"
        d["snapshot"]["layers"]["extra"] = {"kind": "continuous", "source": "nowhere",
                                            "aggregate": {"method": "mean"}}
        d["outputs"]["render"]["ghost"] = {"mapper": "continuous", "ramp": "viridis",
                                           "domain": [0, 1]}
        d["outputs"]["render"]["mineral_1"]["colors"] = {"goethite": [1, 2, 3], "olivine": [0, 0, 0]}
        d["outputs"]["render"]["mineral_1"]["alpha_from"] = {"band": "mineral_1_confidence",
                                                             "domain": [0, 1]}
        d["scorer"] = {"ref": "cleanest_nadir"}                                     # needs aux
        d["pixel_mask"].append({"ref": "no.such:Mask"})
        d["granule_filter"].append({"ref": "nope", "params": {}})
    problems = validate_static(loaded(tmp_path, mutate))
    joined = "\n".join(problems)
    for needle in ("both a role and a band alias", "conditional_on 'view_zenith'",
                   "source 'nowhere'", "'ghost' is not a snapshot layer", "['olivine']",
                   "alpha_from band 'mineral_1_confidence'", "required aux 'slope'",
                   "mask 'no.such:Mask' does not resolve", "filter 'nope' does not resolve"):
        assert needle in joined, (needle, problems)


def test_layer_shape_rules(tmp_path):
    def no_classes(d):
        del d["snapshot"]["layers"]["mineral_1"]["classes"]
    with pytest.raises(ValidationError, match="needs classes"):
        loaded(tmp_path, no_classes)

    def wrong_params(d):
        d["snapshot"]["layers"]["depth_1"]["aggregate"] = {"method": "median", "min_count": 2}
    with pytest.raises(ValidationError, match="do not apply"):
        loaded(tmp_path, wrong_params)

    def inverse_variance_needs_unc(d):
        d["snapshot"]["layers"]["depth_1"]["aggregate"] = {"method": "inverse_variance"}
    with pytest.raises(ValidationError, match="needs unc"):
        loaded(tmp_path, inverse_variance_needs_unc)

    with pytest.raises(ValidationError, match="framework"):
        loaded(tmp_path, lambda d: d["snapshot"]["layers"].update(
            score={"kind": "continuous", "source": "mineral_depth", "aggregate": {"method": "mean"}}))


def test_source_layers_have_no_manifest_level_class_table(tmp_path):
    m = loaded(tmp_path, lambda d: d["snapshot"]["layers"]["mineral_1"].update(classes="source"))
    schema = m.snapshot_schema()
    assert schema["mineral_1"].classes is None and m.enumerations() == {}


def test_aux_kind_and_resampling_must_agree(tmp_path):
    with pytest.raises(ValidationError, match="interpolates class labels"):
        loaded(tmp_path, lambda d: d.update(
            aux={"lc": {"uri": "x.tif", "kind": "categorical", "resampling": "bilinear"}}))
    with pytest.raises(ValidationError, match="needs resampling"):
        loaded(tmp_path, lambda d: d.update(aux={"dem": {"uri": "x.tif", "kind": "continuous"}}))
    m = loaded(tmp_path, lambda d: d.update(aux={
        "snow": {"uri": "s3://b/{date}.tif", "kind": "categorical", "resampling": "nearest",
                 "temporal": "nearest", "max_age": "P3D"},
        "claims": {"uri": "s3://b/c.parquet", "kind": "vector", "burn": "type"}}))
    assert m.aux["claims"].resampling is None


def test_later_slice_items_are_refused_by_name(tmp_path):
    with pytest.raises(NotImplementedError, match="07 section 3"):
        loaded(tmp_path, lambda d: d["outputs"]["render"].update(
            rgb={"mapper": "composite"}))
    m = loaded(tmp_path, lambda d: d.update(reducer={"ref": "my.pkg:ClassifyLast"}))
    assert any("later slice" in p for p in validate_static(m))
    with pytest.raises(ValidationError, match="documented"):
        loaded(tmp_path, lambda d: d.update(allow_mixed_vintage=True))


def test_geolocation_role_prefers_declared_then_sensor_space(tmp_path):
    m = loaded(tmp_path, lambda d: d["inputs"]["roles"].update(
        frcov={"collection": "EMITL2BFRCOV", "var": "soil"}))
    space = {"EMITL2BFRCOV": "ortho"}
    assert m.geolocation_role(lambda c: space.get(c, "sensor")) == "geometry"
    m2 = loaded(tmp_path, lambda d: d["inputs"].update(geolocation="mineral"))
    assert m2.geolocation_role() == "mineral"
    m3 = loaded(tmp_path, lambda d: d["inputs"].update(
        roles={"frcov": {"collection": "EMITL2BFRCOV", "var": "soil"}}))
    with pytest.raises(ValueError, match="no sensor-space role"):
        m3.geolocation_role(lambda c: "ortho")


# --------------------------------------------------------------------------------------- filters
def frame() -> pd.DataFrame:
    return pd.DataFrame({
        "granule_id": ["a", "b", "c", "d"],
        "datetime": pd.to_datetime(["2026-06-02", "2026-07-15", "2026-08-01", "2026-06-30"],
                                   utc=True),
        "cloud_fraction": [0.1, 0.9, None, 0.3],
        "build_version": ["010635", "010635", "010630", None],
        "attributes": [{"SOLAR_ZENITH": "35.2"}, {"SOLAR_ZENITH": "80"}, {}, None],
    })


def test_filters_honour_on_missing():  # 02 section 4
    f = frame()
    with pytest.raises(FilterError, match="on_missing") as ei:
        MaxCloudFraction(0.5).keep(f)
    assert "c" in str(ei.value)
    assert MaxCloudFraction(0.5, "reject").keep(f).tolist() == [True, False, False, True]
    assert MaxCloudFraction(0.5, "keep").keep(f).tolist() == [True, False, True, True]
    assert MaxSolarZenith(70, "reject").keep(f).tolist() == [True, False, False, False]
    assert MaxSolarZenith(70, "keep").keep(f).tolist() == [True, False, True, True]
    assert MonthIn([6]).keep(f).tolist() == [True, False, False, True]
    assert MaxCloudFraction(0.5, "keep").keep(f.drop(columns=["cloud_fraction"])).all()
    # the index stores an absent version as "" (non-nullable column): that is missing too
    empty = pd.DataFrame({"build_version": ["010635", "", None]})
    with pytest.raises(FilterError, match="on_missing"):
        ColumnIn("build_version", "010635").keep(empty)
    assert ColumnIn("build_version", "010635", "reject").keep(empty).tolist() == [True, False, False]
    assert ColumnIn("build_version", "010635", "keep").keep(empty).tolist() == [True, True, True]


def test_filter_chain_reports_per_filter(tmp_path):
    m = loaded(tmp_path, lambda d: d.update(granule_filter=[
        {"max_cloud_fraction": 0.5, "on_missing": "keep"},
        {"max_solar_zenith": 70, "on_missing": "reject"},
        {"month_in": [6, 7]},
        {"build_version": "010635", "on_missing": "keep"},
    ]))
    filters = build_filters(m)
    kept, reports = apply_filters(frame(), filters)
    assert kept["granule_id"].tolist() == ["a"]
    assert [(r.removed, r.on_missing) for r in reports] == [(1, "keep"), (2, "reject"),
                                                             (0, None), (0, "keep")]
    assert reports[0].describe == "cloud_fraction <= 0.5 (on_missing: keep)"
    assert reports[2].describe == "month in [6, 7]"


def test_filters_run_per_granule_not_per_row(tmp_path):
    """02 section 4 / 12 section 8 question 11: the index holds a row per (granule,
    collection). A mask collection indexed beside MIN carries no CloudCover; the granule's
    MIN row does. `on_missing: fail` must not trip on the mask row, the report counts
    granules, and a dropped granule takes all of its rows with it."""
    rows = pd.DataFrame({
        "granule_id": ["a", "a", "b", "b"],
        "collection": ["EMITL2BMIN", "EMITL2AMASK", "EMITL2BMIN", "EMITL2AMASK"],
        "datetime": pd.to_datetime(["2026-06-02"] * 4, utc=True),
        "cloud_fraction": [0.1, None, 0.9, None],
        "build_version": ["010635", "", "010635", ""],
        "attributes": [{"SOLAR_ZENITH": "35.2"}, {}, {"SOLAR_ZENITH": "80"}, {}],
        "assets": [{"MIN": "u1"}, {"MASK": "u2"}, {"MIN": "u3"}, {"MASK": "u4"}],
    })
    per_granule = granule_level(rows)
    assert per_granule["granule_id"].tolist() == ["a", "b"]
    assert per_granule["cloud_fraction"].tolist() == [0.1, 0.9]           # from the MIN row
    assert per_granule["build_version"].tolist() == ["010635", "010635"]
    assert per_granule.loc[0, "attributes"] == {"SOLAR_ZENITH": "35.2"}
    assert per_granule.loc[0, "assets"] == {"MIN": "u1", "MASK": "u2"}
    kept, reports = apply_filters(rows, [MaxCloudFraction(0.5), ColumnIn("build_version", "010635")])
    assert kept["granule_id"].tolist() == ["a", "a"]                     # both of a's rows
    assert [(r.removed, r.on_missing) for r in reports] == [(1, "fail"), (0, "fail")]
    assert len(granule_level(frame())) == 4                            # unique ids: unchanged


def test_filter_spec_shape(tmp_path):
    with pytest.raises(ValidationError, match="exactly one predicate"):
        loaded(tmp_path, lambda d: d.update(granule_filter=[{"max_cloud_fraction": 0.5,
                                                             "month_in": [6]}]))
    with pytest.raises(ValidationError, match="nothing can be missing"):
        loaded(tmp_path, lambda d: d.update(granule_filter=[{"month_in": [6],
                                                             "on_missing": "keep"}]))
    m = loaded(tmp_path, lambda d: d.update(granule_filter=[{"max_solar_zenith": 70}]))
    assert m.granule_filter[0].policy == "fail"            # the explicit default
    assert build_filters(m)[0].on_missing == "fail"


def test_generic_filter_resolves_a_module_class(tmp_path):
    m = loaded(tmp_path, lambda d: d.update(granule_filter=[
        {"ref": "stratum.filters:MonthIn", "params": {"months": [6]}}]))
    assert validate_static(m) == []
    (f,) = build_filters(m)
    assert isinstance(f, MonthIn) and np.array_equal(f.keep(frame()), [True, False, False, True])


def test_hash_and_run_id_move_with_the_classes_file_content(tmp_path):
    """00 section 5 invariant 4: the `@ref:` path is in the document, its content enters as the
    enumeration's fingerprint, so editing the lumping under the same path is a new run."""
    m1 = loaded(tmp_path)
    swapped = copy.deepcopy(CLASSES)
    a, b = swapped["classes"]
    a["members"], b["members"] = b["members"], a["members"]
    (tmp_path / "classes/t-v1.yaml").write_text(yaml.safe_dump(swapped))
    m2 = load_manifest(tmp_path / "manifest.yaml")
    assert m1.document() == m2.document()                       # the document did not change
    assert manifest_hash(m1) != manifest_hash(m2) and m1.run_id != m2.run_id
    s1, s2 = m1.snapshot_schema(), m2.snapshot_schema()
    assert s1["mineral_1"].classes.fingerprint() == s2["mineral_1"].classes.fingerprint()
    assert s1["mineral_1"].lumping != s2["mineral_1"].lumping and s1.layers_hash != s2.layers_hash


def test_this_slice_refuses_aux_and_netcdf_statically(tmp_path):
    """First-slice plan section 1: what no worker can execute fails in `stratum validate`."""
    m = loaded(tmp_path, lambda d: d.update(
        aux={"slope": {"uri": "s3://x/slope.tif", "kind": "continuous", "resampling": "bilinear"}},
        scorer={"ref": "cleanest_nadir"}))
    problems = validate_static(m)
    assert any(p.startswith("aux ['slope']") and "05" in p for p in problems), problems
    assert any("required aux 'slope' is declared, but aux data is not in this slice" in p
               for p in problems), problems
    m = loaded(tmp_path, lambda d: d["outputs"].update(formats=["cog", "netcdf"]))
    assert any("'netcdf'" in p and "07 section 2" in p for p in validate_static(m))


def test_alpha_from_must_name_a_band_the_reducer_delivers(tmp_path):
    """09 section 5 / 13 section 4: `depth_1` is continuous, so `depth_1_agreement` is never
    written; `mineral_1_n` neither; `mineral_1_agreement` is."""
    def with_alpha(band):
        return lambda d: d["outputs"]["render"]["mineral_1"].update(
            alpha_from={"band": band, "domain": [0, 1]})
    for band in ("depth_1_agreement", "mineral_1_n", "depth_1_spread"):
        problems = validate_static(loaded(tmp_path, with_alpha(band)))
        assert any(f"alpha_from band {band!r}" in p for p in problems), (band, problems)
    assert validate_static(loaded(tmp_path, with_alpha("mineral_1_agreement"))) == []
    assert validate_static(loaded(tmp_path, with_alpha("depth_1_n"))) == []


def test_local_source_forms(tmp_path):
    """12 section 5: `pattern:` (one glob) and the long form `{version, assets}` both load;
    a local source with neither is refused."""
    m = loaded(tmp_path, lambda d: d["inputs"].update(
        source={"kind": "local", "root": "./granules", "pattern": "EMIT_L2B_MIN_*.nc"}))
    assert m.inputs.source.pattern == "EMIT_L2B_MIN_*.nc"
    m = loaded(tmp_path, lambda d: d["inputs"].update(source={
        "kind": "local", "root": "./granules",
        "patterns": {"EMITL2BMIN": {"version": "001", "assets": {"MIN": "EMIT_L2B_MIN_*.nc"}}}}))
    assert m.inputs.source.patterns["EMITL2BMIN"]["assets"] == {"MIN": "EMIT_L2B_MIN_*.nc"}
    with pytest.raises(ValidationError, match="pattern or patterns"):
        loaded(tmp_path, lambda d: d["inputs"].update(source={"kind": "local", "root": "./g"}))
