"""Build, write, read, query and freeze the index (02 sections 2, 3, 6)."""
from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import shapely

from stratum.index.schema import (
    COLUMNS,
    GEOMETRY_COLUMN,
    INDEX_SCHEMA,
    MAP_COLUMNS,
    PROMOTED_ATTRIBUTES,
)
from stratum.types import GranuleRecord, GranuleSource

INDEX_FILENAME = "index.parquet"


def _utc(dt: datetime) -> datetime:
    return dt.astimezone(UTC) if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _stringify(value: Any) -> str:
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return repr(value)
    return str(value)


def _attribute_map(attributes: Mapping[str, Any]) -> dict[str, str]:
    """Everything the source returned, verbatim, minus the promoted columns; values stringified,
    None dropped (a map<string,string> has no null)."""
    return {str(k): _stringify(v) for k, v in attributes.items()
            if k not in PROMOTED_ATTRIBUTES and v is not None}


def _is_missing(value: Any) -> bool:
    """None, or a float NaN (pandas' null for object/float columns)."""
    return value is None or (isinstance(value, float) and math.isnan(value))


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    f = float(value)
    return None if math.isnan(f) else f


def row_from_record(source: GranuleSource, record: GranuleRecord, last_seen: datetime) -> dict:
    """One index row. `checksums` come from `source.checksums(record)` when the source has it."""
    attrs = record.attributes
    geom = record.geometry
    bbox = [float(v) for v in geom.bounds]
    checksums = source.checksums(record) if hasattr(source, "checksums") else {}
    day_night = attrs.get("day_night")
    return {
        "granule_id": record_granule_id(record),
        "collection": record.collection,
        "datetime": _utc(record.datetime),
        "end_datetime": _utc(record.end_datetime),
        GEOMETRY_COLUMN: shapely.to_wkb(geom),
        "bbox": bbox,
        "cloud_fraction": _optional_float(attrs.get("cloud_fraction")),
        "build_version": str(attrs.get("build_version") or ""),
        "product_version": str(attrs.get("product_version") or ""),
        "collection_version": str(attrs.get("collection_version") or ""),
        "day_night": None if day_night is None else str(day_night),
        "last_seen": _utc(last_seen),
        "assets": {str(k): str(v) for k, v in source.assets(record).items()},
        "checksums": {str(k): str(v) for k, v in checksums.items()},
        "attributes": _attribute_map(attrs),
    }


def record_granule_id(record: GranuleRecord) -> str:
    """A source may name the id in attributes (`granule_id`); otherwise the native id."""
    return str(record.attributes.get("granule_id") or record.native_id)


def table_from_rows(rows: Sequence[Mapping[str, Any]]) -> pa.Table:
    cols = {}
    for name in COLUMNS:
        values = [r[name] for r in rows]
        field = INDEX_SCHEMA.field(name)
        if name in MAP_COLUMNS:
            values = [sorted(dict(v).items()) for v in values]
        cols[name] = pa.array(values, type=field.type, from_pandas=True)
    return pa.table(cols, schema=INDEX_SCHEMA)


def build_index(source: GranuleSource, *, collections: Sequence[str],
                bbox: tuple[float, float, float, float] | None = None,
                start: datetime | None = None, end: datetime | None = None,
                last_seen: datetime | None = None) -> pa.Table:
    """Stream a source's records into an index table. `last_seen` is one timestamp for the
    whole build (02 section 3); it defaults to now."""
    seen = _utc(last_seen) if last_seen is not None else datetime.now(UTC)
    rows = [row_from_record(source, rec, seen)
            for rec in source.search(collections=list(collections), bbox=bbox, start=start,
                                     end=end)]
    return table_from_rows(rows)


def write_index(table: pa.Table, path: Path) -> Path:
    """Parquet with the GeoParquet `geo` metadata (02 section 2). Sorted by (collection,
    granule_id) so identical inputs give identical bytes."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if table.schema.metadata is None or b"geo" not in table.schema.metadata:
        table = table.replace_schema_metadata(INDEX_SCHEMA.metadata)
    if table.num_rows:
        table = table.sort_by([("collection", "ascending"), ("granule_id", "ascending")])
    pq.write_table(table, path)
    return path


def frame_from_table(table: pa.Table) -> pd.DataFrame:
    """The GranuleFrame: geometry as shapely objects, maps as dicts, bbox as a tuple. A null
    `cloud_fraction` is NaN in pandas - test it with `isna`, never `== 0` (02 section 4)."""
    frame = table.to_pandas(types_mapper=None)
    frame[GEOMETRY_COLUMN] = [shapely.from_wkb(b) for b in frame[GEOMETRY_COLUMN]]
    frame["bbox"] = [tuple(float(x) for x in b) for b in frame["bbox"]]
    for name in MAP_COLUMNS:
        frame[name] = [dict(v) if v is not None else {} for v in frame[name]]
    for name in ("datetime", "end_datetime", "last_seen"):
        frame[name] = pd.to_datetime(frame[name], utc=True)
    return frame


def table_from_frame(frame: pd.DataFrame) -> pa.Table:
    rows = []
    for rec in frame.to_dict("records"):
        row = dict(rec)
        geom = row[GEOMETRY_COLUMN]
        row[GEOMETRY_COLUMN] = geom if isinstance(geom, bytes) else shapely.to_wkb(geom)
        row["bbox"] = [float(x) for x in row["bbox"]]
        row["cloud_fraction"] = _optional_float(row.get("cloud_fraction"))
        dn = row.get("day_night")
        row["day_night"] = None if _is_missing(dn) else str(dn)
        for name in ("datetime", "end_datetime", "last_seen"):
            row[name] = _utc(pd.Timestamp(row[name]).to_pydatetime())
        rows.append(row)
    return table_from_rows(rows)


def read_index(path: Path) -> pd.DataFrame:
    return frame_from_table(pq.read_table(Path(path), schema=INDEX_SCHEMA))


def query_index(frame: pd.DataFrame, *, bbox: tuple[float, float, float, float] | None = None,
                start: datetime | None = None, end: datetime | None = None,
                collections: Sequence[str] | None = None) -> pd.DataFrame:
    """Rows whose bbox meets `bbox`, whose `datetime` lies in [start, end), and whose collection
    is listed. Each predicate is optional. Cloud policy is a filter's business, not the query's
    (02 section 4)."""
    keep = pd.Series(True, index=frame.index)
    if bbox is not None:
        w, s, e, n = bbox
        bb = pd.DataFrame(frame["bbox"].tolist(), index=frame.index, columns=list("wsen"))
        keep &= (bb["w"] <= e) & (bb["e"] >= w) & (bb["s"] <= n) & (bb["n"] >= s)
    if start is not None:
        keep &= frame["datetime"] >= pd.Timestamp(_utc(start))
    if end is not None:
        keep &= frame["datetime"] < pd.Timestamp(_utc(end))
    if collections is not None:
        keep &= frame["collection"].isin(list(collections))
    return frame[keep].reset_index(drop=True)


def freeze_index(frame: pd.DataFrame, run_dir: Path) -> tuple[Path, str]:
    """Materialise the surviving rows as `{run_dir}/index.parquet` and hash the file bytes
    (02 section 6). Returns (path, 'sha256:...')."""
    run_dir = Path(run_dir)
    path = write_index(table_from_frame(frame), run_dir / INDEX_FILENAME)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return path, f"sha256:{digest}"
