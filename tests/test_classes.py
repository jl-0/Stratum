"""Spec 13 section 3 and 07 section 6: enumerations resolve by attribute, never by position."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pyarrow as pa
import pytest

from stratum.classes import (
    MAX_ID,
    ClassDef,
    Enumeration,
    EnumerationError,
    Member,
    enumeration_from_document,
    identity_enumeration,
    load_enumeration,
)
from stratum.types import ClassTable

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples/emit-critical-minerals/classes/cm-v1.yaml"
REF_GRANULE = ROOT / "refs/EMIT_L2B_MIN_001_20260825T151308_2623710_050.nc"


def raw_table(rows: list[tuple[int, str, int, int, str]], source: str = "test") -> ClassTable:
    idx, lib, rec, grp, name = zip(*rows, strict=True)
    t = pa.table({"index": list(idx), "library": list(lib), "record": list(rec),
                  "group": list(grp), "name": list(name)})
    return ClassTable(key="index", entries=t, source=source)


# A raw table in the shape of the embedded /mineral_metadata: positional index, attributes.
RAW = raw_table([
    (1, "splib06", 6858, 1, "Plastic_Tarp"),
    (2, "sprlb06", 744, 1, "Hematite.02+Quartz.98"),
    (3, "sprlb06", 882, 1, "Goethite WS222"),
    (4, "splib06", 5736, 1, "Goethite0.02+Quartz"),
    (5, "splib06", 5880, 1, "Hematite GDS27"),
    (6, "splib06", 2568, 1, "Jarosite GDS99"),
])


def enum(unmapped: str = "fail", classes: tuple[ClassDef, ...] | None = None) -> Enumeration:
    classes = classes or (
        ClassDef(1, "goethite", (Member({"library": "sprlb06", "record": 882, "group": 1}),
                                 Member({"library": "splib06", "record": 5736, "group": 1}))),
        ClassDef(2, "hematite", (Member({"library": "splib06", "record": 5880, "group": 1}),)),
    )
    return Enumeration("t", "1", ("library", "record", "group"), unmapped, classes)


def test_example_classes_file_loads():
    e = load_enumeration(EXAMPLE)
    assert e.name == "cm-v1" and e.match_on == ("library", "record", "group")
    assert [c.id for c in e.classes] == [1, 2, 3] and e.unmapped == "fail"
    assert e["goethite"].members[0].attrs["record"] == 882


def test_class_table_has_none_first_and_stable_fingerprint():  # 13 section 5
    t = enum().class_table()
    assert t.key == "id"
    assert t.entries.to_pydict() == {"id": [0, 1, 2], "name": ["none", "goethite", "hematite"]}
    assert t.fingerprint() == enum().class_table().fingerprint()
    assert t.fingerprint() != enum(classes=(
        ClassDef(1, "goethite", (Member({"library": "sprlb06", "record": 882, "group": 1}),)),
        ClassDef(2, "haematite", (Member({"library": "splib06", "record": 5880, "group": 1}),)),
    )).class_table().fingerprint()                       # a rename is a redefine


def test_resolve_lumps_by_attribute_and_routes_unmapped_to_a_class():
    other = ClassDef(9, "other", (Member({"library": "splib06", "record": 6858, "group": 1}),))
    e = enum(unmapped="other", classes=(*enum().classes, other))
    r = e.resolve(RAW)
    assert r.lookup.tolist() == [0, 9, 9, 1, 1, 2, 9]      # index = raw key; 0 -> none always
    assert r.lookup.dtype == np.int32 and len(r.lookup) == 7
    assert r.enumeration == "t" and r.raw_fingerprint == RAW.fingerprint()
    ids = r.apply(np.array([[3, 4], [5, 0]]))
    assert ids.tolist() == [[1, 1], [2, 0]]


def test_resolve_fails_listing_unmapped_rows():  # unmapped: fail lists them
    with pytest.raises(EnumerationError) as ei:
        enum().resolve(RAW)
    msg = str(ei.value)
    assert "3 raw row(s) no class claims" in msg
    assert "record: 6858" in msg and "record: 744" in msg and "record: 2568" in msg


def test_resolve_fails_on_zero_and_multiple_matches():
    dup = raw_table([(1, "splib06", 5880, 1, "Hematite a"), (2, "splib06", 5880, 1, "Hematite b"),
                     (3, "sprlb06", 882, 1, "Goethite")])
    with pytest.raises(EnumerationError) as ei:
        enum().resolve(dup)
    msg = str(ei.value)
    assert "member {library: splib06, record: 5736, group: 1} matches 0 rows" in msg
    assert "member {library: splib06, record: 5880, group: 1} matches 2 rows (keys [1, 2])" in msg


def test_resolve_refuses_a_raw_row_claimed_twice():
    twice = enum(classes=(
        ClassDef(1, "goethite", (Member({"library": "sprlb06", "record": 882, "group": 1}),)),
        ClassDef(2, "also_goethite", (Member({"library": "sprlb06", "record": 882, "group": 1}),)),
        ClassDef(3, "rest", (Member({"library": "splib06", "record": 6858, "group": 1}),)),
    ), unmapped="rest")
    with pytest.raises(EnumerationError, match="raw key 3 claimed by both"):
        twice.resolve(RAW)


def test_resolve_needs_the_match_on_columns():
    bare = ClassTable("index", pa.table({"index": [1, 2], "name": ["a", "b"]}), "bare")
    with pytest.raises(EnumerationError, match="lacks match_on column"):
        enum().resolve(bare)


def test_attribute_match_crosses_the_string_int_gap():
    as_text = raw_table([(1, "sprlb06", 882, 1, "g"), (2, "splib06", 5736, 1, "g2"),
                         (3, "splib06", 5880, 1, "h")])
    t = as_text.entries
    t = t.set_column(t.schema.get_field_index("record"), "record",
                     pa.array([str(v) for v in t.column("record").to_pylist()]))
    r = enum().resolve(ClassTable("index", t, "text"))
    assert r.lookup.tolist() == [0, 1, 1, 2]


def test_enumeration_invariants():  # 13 section 3 rule 1
    m = (Member({"library": "x", "record": 1, "group": 1}),)
    with pytest.raises(EnumerationError, match="reserved"):
        Enumeration("t", "1", ("library", "record", "group"), "fail", (ClassDef(0, "z", m),))
    with pytest.raises(EnumerationError, match="duplicate ids"):
        Enumeration("t", "1", ("library", "record", "group"), "fail",
                    (ClassDef(1, "a", m), ClassDef(1, "b", m)))
    with pytest.raises(EnumerationError, match="neither 'fail' nor a class name"):
        Enumeration("t", "1", ("library", "record", "group"), "other", (ClassDef(1, "a", m),))
    with pytest.raises(EnumerationError, match="exactly match_on"):
        Enumeration("t", "1", ("library", "record"), "fail", (ClassDef(1, "a", m),))
    with pytest.raises(EnumerationError, match="unknown key"):
        enumeration_from_document({"name": "t", "match_on": ["a"], "classes": [], "colour": 1})


def test_identity_enumeration_is_the_raw_table():  # classes: source
    e = identity_enumeration(RAW)
    assert [c.id for c in e.classes] == [1, 2, 3, 4, 5, 6]
    assert e["Goethite WS222"].id == 3
    r = e.resolve(RAW)
    assert r.lookup.tolist() == [0, 1, 2, 3, 4, 5, 6]        # identity
    assert e.class_table().entries.column("name").to_pylist()[0] == "none"
    shifted = raw_table([(1, "sprlb06", 882, 1, "Goethite WS222")])
    with pytest.raises(EnumerationError):                    # a different table does not resolve
        e.resolve(shifted)


@pytest.mark.skipif(not REF_GRANULE.exists(), reason="reference granule not present")
def test_example_enumeration_against_the_reference_granule():
    """The embedded table has 294 rows; (library, record, group) resolves 292 uniquely
    (11 section 9). cm-v1's hematite member names group 1, which the granule lacks."""
    import netCDF4 as nc  # readers belong to another module; read the table directly
    with nc.Dataset(REF_GRANULE) as ds:
        g = ds.groups["mineral_metadata"]
        cols = {k: g.variables[k][:].tolist() for k in ("index", "record", "library", "group",
                                                         "name")}
    raw = ClassTable("index", pa.table(cols), f"{REF_GRANULE.name}:/mineral_metadata")
    assert raw.entries.num_rows == 294
    e = load_enumeration(EXAMPLE)
    with pytest.raises(EnumerationError) as ei:
        e.resolve(raw)
    assert "class 'hematite' member {library: splib06, record: 5880, group: 1} matches 0 rows" in (
        str(ei.value))
    # Route the unlisted rows to `other` and drop hematite: goethite lumps keys 3 and 4.
    other = ClassDef(99, "other", (Member({"library": "splib06", "record": 6858, "group": 1}),))
    e2 = Enumeration("t", "1", e.match_on, "other", (e["goethite"], e["pyrite"], other))
    r = e2.resolve(raw)
    assert r.lookup[3] == 1 and r.lookup[4] == 1 and r.lookup[15] == 3 and r.lookup[0] == 0
    assert (r.lookup[1:] > 0).all()


def test_ids_fit_below_the_uint16_nodata_sentinel():  # 11 section 2; plan section 4
    m = (Member({"library": "x", "record": 1, "group": 1}),)
    Enumeration("t", "1", ("library", "record", "group"), "fail", (ClassDef(MAX_ID, "a", m),))
    with pytest.raises(EnumerationError, match="65535 as nodata"):
        Enumeration("t", "1", ("library", "record", "group"), "fail", (ClassDef(65535, "a", m),))
    with pytest.raises(EnumerationError, match="exceeds 65534"):
        Enumeration("t", "1", ("library", "record", "group"), "fail", (ClassDef(70000, "a", m),))
    with pytest.raises(EnumerationError, match="exceed 65534"):
        identity_enumeration(raw_table([(1, "splib06", 1, 1, "a"), (70000, "splib06", 2, 1, "b")]))


def test_enumeration_fingerprint_sees_the_lumping_not_just_the_table():  # 13 section 6
    e1 = enum()
    goethite, hematite = e1.classes
    e2 = enum(classes=(ClassDef(1, "goethite", hematite.members),
                       ClassDef(2, "hematite", goethite.members)))
    assert e1.class_table().fingerprint() == e2.class_table().fingerprint()   # same (id, name)
    assert e1.fingerprint() != e2.fingerprint()                               # different lumping
    assert e1.fingerprint() == enum().fingerprint()                           # and stable
