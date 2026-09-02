"""Product enumerations and their resolution against raw class tables.
Spec: docs/specs/13-snapshot-schema.md section 3, 07 section 6, 11 section 9.

The core never knows what a class means. It knows an enumeration names product classes with
explicit ids, that each class claims raw rows by attribute values, and how to turn one raw
table into a raw-key -> product-id lookup. Product id 0 is `none` - "observed, nothing
identified" - and raw key 0 maps to it without being listed.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import yaml

from stratum.types import ClassTable, canonical_hash

NONE_ID = 0
NONE_NAME = "none"
UNMAPPED = -1
# Categorical layers and products are stored as uint16 with 65535 as nodata (11 section 2,
# first-slice plan section 4), so a product id must fit below that sentinel: an id of 65535
# would read back as nodata and anything larger would wrap silently on the cast.
MAX_ID = 65534


class EnumerationError(ValueError):
    """A classes file is malformed, or it does not resolve against a raw table."""


def _same(a: Any, b: Any) -> bool:
    """Attribute equality across the YAML/pyarrow type gap: 882 matches "882"."""
    if a == b:
        return True
    return str(a).strip() == str(b).strip()


@dataclass(frozen=True)
class Member:
    """One raw class a product class claims, by attribute values (never by position)."""

    attrs: Mapping[str, Any]

    def __str__(self) -> str:
        return "{" + ", ".join(f"{k}: {v}" for k, v in self.attrs.items()) + "}"


@dataclass(frozen=True)
class ClassDef:
    id: int
    name: str
    members: tuple[Member, ...]


@dataclass
class Remap:
    """raw key -> product id for one raw table. `lookup[k]` is indexed by raw key value, so its
    size is max(key) + 1; -1 marks a key the table does not describe (13 section 3)."""

    lookup: np.ndarray
    raw_fingerprint: str
    enumeration: str

    def apply(self, raw: np.ndarray) -> np.ndarray:
        """Vectorised lookup. The caller keeps fill as a mask; this only sees real keys."""
        return self.lookup[np.asarray(raw, dtype=np.int64)]


@dataclass(frozen=True)
class Enumeration:
    """The product's own class table, and the lumping into it (13 section 3)."""

    name: str
    version: str
    match_on: tuple[str, ...]
    unmapped: str                      # "fail", or the name of the class unlisted raw rows join
    classes: tuple[ClassDef, ...]

    def __post_init__(self) -> None:
        problems: list[str] = []
        if not self.match_on:
            problems.append("match_on is empty")
        ids = [c.id for c in self.classes]
        names = [c.name for c in self.classes]
        if len(set(ids)) != len(ids):
            problems.append(f"duplicate ids {sorted({i for i in ids if ids.count(i) > 1})}")
        if len(set(names)) != len(names):
            problems.append(f"duplicate names {sorted({n for n in names if names.count(n) > 1})}")
        for c in self.classes:
            if c.id <= NONE_ID:
                problems.append(f"class {c.name!r}: id {c.id} - 0 is reserved for {NONE_NAME!r} "
                                "and ids are positive")
            elif c.id > MAX_ID:
                problems.append(f"class {c.name!r}: id {c.id} exceeds {MAX_ID}; categorical "
                                "layers are uint16 with 65535 as nodata (11 section 2)")
            if c.name == NONE_NAME:
                problems.append(f"{NONE_NAME!r} is the reserved id-0 class; do not declare it")
            for m in c.members:
                if set(m.attrs) != set(self.match_on):
                    problems.append(f"class {c.name!r} member {m}: attributes must be exactly "
                                    f"match_on {list(self.match_on)}")
        if self.unmapped != "fail" and self.unmapped not in names:
            problems.append(f"unmapped: {self.unmapped!r} is neither 'fail' nor a class name")
        if problems:
            raise EnumerationError(f"enumeration {self.name!r}: " + "; ".join(problems))

    def __getitem__(self, name: str) -> ClassDef:
        for c in self.classes:
            if c.name == name:
                return c
        raise KeyError(name)

    @property
    def names(self) -> tuple[str, ...]:
        """Every product class name, `none` first."""
        return (NONE_NAME, *(c.name for c in self.classes))

    def fingerprint(self) -> str:
        """A hash of the WHOLE enumeration document - name, version, match_on, unmapped and
        every member of every class - unlike `class_table().fingerprint()`, which sees only
        (id, name). This is what tells two lumpings apart that deliver the same product table,
        and it enters `layers_hash` through `LayerSpec.lumping` and the manifest hash through
        `manifest_hash` (06 section 3 rule 1, 13 section 6)."""
        return canonical_hash({
            "name": self.name, "version": self.version, "match_on": list(self.match_on),
            "unmapped": self.unmapped,
            "classes": [{"id": c.id, "name": c.name,
                         "members": [dict(sorted(m.attrs.items())) for m in c.members]}
                        for c in self.classes],
        })

    def class_table(self) -> ClassTable:
        """The product table: key `id`, columns `id`, `name`; `none` is row 0. This is what
        enters `layers_hash` and what publish ships beside the product (13 section 5)."""
        ids = [NONE_ID, *(c.id for c in self.classes)]
        names = [NONE_NAME, *(c.name for c in self.classes)]
        return ClassTable(key="id", entries=pa.table({"id": ids, "name": names}),
                          source=f"enumeration:{self.name}@{self.version}")

    def resolve(self, raw: ClassTable) -> Remap:
        """Match every member against `raw` on `match_on`; one raw row per member, exactly.

        Raises EnumerationError listing every member that matched zero or several rows, every
        raw row claimed twice, and - under `unmapped: fail` - every raw row no member claims.
        Raw key 0 always maps to product id 0 and is never reported as unmapped.
        """
        missing = [c for c in self.match_on if c not in raw.entries.column_names]
        if missing:
            raise EnumerationError(f"enumeration {self.name!r}: raw table {raw.source} lacks "
                                   f"match_on column(s) {missing}; it has {raw.entries.column_names}")
        keys = raw.entries.column(raw.key).to_pylist()
        if any(k is None or int(k) < 0 for k in keys):
            raise EnumerationError(f"raw table {raw.source}: key column {raw.key!r} has null or "
                                   "negative values")
        keys = [int(k) for k in keys]
        cols = {c: raw.entries.column(c).to_pylist() for c in self.match_on}
        lookup = np.full(max(keys, default=0) + 1, UNMAPPED, dtype=np.int32)
        lookup[NONE_ID] = NONE_ID
        claimed: dict[int, str] = {}
        problems: list[str] = []
        for c in self.classes:
            for m in c.members:
                rows = [i for i in range(len(keys))
                        if all(_same(cols[a][i], m.attrs[a]) for a in self.match_on)]
                if len(rows) != 1:
                    hits = [keys[i] for i in rows]
                    problems.append(f"class {c.name!r} member {m} matches {len(rows)} rows"
                                    + (f" (keys {hits})" if hits else ""))
                    continue
                k = keys[rows[0]]
                if k in claimed:
                    problems.append(f"raw key {k} claimed by both {claimed[k]!r} and {c.name!r}")
                    continue
                claimed[k] = c.name
                lookup[k] = c.id
        unclaimed = [i for i, k in enumerate(keys) if k != NONE_ID and k not in claimed]
        if unclaimed:
            if self.unmapped == "fail":
                shown = ", ".join(
                    f"{keys[i]}: " + str(Member({a: cols[a][i] for a in self.match_on}))
                    for i in unclaimed[:20])
                more = f" ... and {len(unclaimed) - 20} more" if len(unclaimed) > 20 else ""
                problems.append(f"{len(unclaimed)} raw row(s) no class claims (unmapped: fail): "
                                f"{shown}{more}")
            else:
                target = self[self.unmapped].id
                for i in unclaimed:
                    lookup[keys[i]] = target
        if problems:
            raise EnumerationError(f"enumeration {self.name!r} against {raw.source}: "
                                   + "; ".join(problems))
        return Remap(lookup=lookup, raw_fingerprint=raw.fingerprint(), enumeration=self.name)


def _require(doc: Mapping[str, Any], key: str, where: str) -> Any:
    if key not in doc:
        raise EnumerationError(f"{where}: missing {key!r}")
    return doc[key]


def enumeration_from_document(doc: Mapping[str, Any], where: str = "classes") -> Enumeration:
    """Build from the parsed YAML of a classes file (13 section 3)."""
    if not isinstance(doc, Mapping):
        raise EnumerationError(f"{where}: expected a mapping")
    allowed = {"name", "version", "match_on", "unmapped", "classes"}
    extra = set(doc) - allowed
    if extra:
        raise EnumerationError(f"{where}: unknown key(s) {sorted(extra)}")
    match_on = _require(doc, "match_on", where)
    if not isinstance(match_on, list) or not all(isinstance(a, str) for a in match_on):
        raise EnumerationError(f"{where}: match_on must be a list of attribute names")
    classes: list[ClassDef] = []
    for i, c in enumerate(_require(doc, "classes", where) or []):
        at = f"{where}: classes[{i}]"
        if not isinstance(c, Mapping) or set(c) - {"id", "name", "members"}:
            raise EnumerationError(f"{at}: expected {{id, name, members}}")
        cid, cname = _require(c, "id", at), _require(c, "name", at)
        if not isinstance(cid, int) or isinstance(cid, bool):
            raise EnumerationError(f"{at}: id must be an integer, got {cid!r}")
        members = _require(c, "members", at)
        if not isinstance(members, list) or not members:
            raise EnumerationError(f"{at} ({cname}): members must be a non-empty list")
        for j, m in enumerate(members):
            if not isinstance(m, Mapping):
                raise EnumerationError(f"{at} ({cname}) members[{j}]: expected a mapping")
        classes.append(ClassDef(cid, str(cname), tuple(Member(dict(m)) for m in members)))
    return Enumeration(
        name=str(_require(doc, "name", where)),
        version=str(doc.get("version", "1")),
        match_on=tuple(match_on),
        unmapped=str(doc.get("unmapped", "fail")),
        classes=tuple(classes),
    )


def load_enumeration(path: Path | str) -> Enumeration:
    path = Path(path)
    try:
        doc = yaml.safe_load(path.read_text())
    except FileNotFoundError:
        raise EnumerationError(f"classes file not found: {path}") from None
    return enumeration_from_document(doc, where=str(path))


def identity_enumeration(raw: ClassTable) -> Enumeration:
    """`classes: source` (13 section 3): the raw table is the product table, no lumping.

    One class per raw row, id = raw key, name from the raw `name` column when there is one
    (made unique with the key when it is not). Members match on the key *and* every attribute
    column, so the identity resolves only against a table equal to this one row for row - which
    is the fingerprint-agreement rule 13 section 3 imposes on `source` layers. `unmapped` is
    `fail`.
    """
    keys = [int(k) for k in raw.entries.column(raw.key).to_pylist()]
    too_big = [k for k in keys if k > MAX_ID]
    if too_big:
        raise EnumerationError(f"raw table {raw.source}: key(s) {too_big[:5]} exceed {MAX_ID}, the "
                               "largest product id a uint16 layer with 65535 as nodata can carry; "
                               "`classes: source` cannot use this table as-is (11 section 2)")
    attrs = list(raw.attrs())
    cols = {c: raw.entries.column(c).to_pylist() for c in raw.entries.column_names}
    names: list[str] = []
    for i, k in enumerate(keys):
        n = str(cols["name"][i]) if "name" in cols else str(k)
        names.append(n)
    seen = {n for n in names if names.count(n) > 1}
    classes = tuple(
        ClassDef(k, f"{n}#{k}" if n in seen or n == NONE_NAME else n,
                 (Member({raw.key: k, **{a: cols[a][i] for a in attrs}}),))
        for i, (k, n) in enumerate(zip(keys, names, strict=True)) if k != NONE_ID
    )
    fp = raw.fingerprint()
    return Enumeration(name=f"source:{fp[7:23]}", version=fp, match_on=(raw.key, *attrs),
                       unmapped="fail", classes=classes)

