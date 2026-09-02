"""The index schema (02 section 2): GeoParquet, one row per granule per collection."""
from __future__ import annotations

import json

import pyarrow as pa

GEOMETRY_COLUMN = "geometry"

GEO_METADATA: dict = {
    "version": "1.0.0",
    "primary_column": GEOMETRY_COLUMN,
    "columns": {
        GEOMETRY_COLUMN: {
            "encoding": "WKB",
            "geometry_types": ["Polygon"],
            # No `crs` key: GeoParquet 1.0.0 defaults it to OGC:CRS84 (lon/lat WGS 84), which is
            # what footprints are in (02 section 2).
        }
    },
}

_STR_MAP = pa.map_(pa.string(), pa.string())
_TS = pa.timestamp("us", tz="UTC")

INDEX_SCHEMA = pa.schema(
    [
        pa.field("granule_id", pa.string(), nullable=False),
        pa.field("collection", pa.string(), nullable=False),
        pa.field("datetime", _TS, nullable=False),
        pa.field("end_datetime", _TS, nullable=False),
        pa.field(GEOMETRY_COLUMN, pa.binary(), nullable=False),
        pa.field("bbox", pa.list_(pa.float64()), nullable=False),
        pa.field("cloud_fraction", pa.float64(), nullable=True),     # nullness is meaningful
        pa.field("build_version", pa.string(), nullable=False),
        pa.field("product_version", pa.string(), nullable=False),
        pa.field("collection_version", pa.string(), nullable=False),
        pa.field("day_night", pa.string(), nullable=True),
        pa.field("last_seen", _TS, nullable=False),
        pa.field("assets", _STR_MAP, nullable=False),
        pa.field("checksums", _STR_MAP, nullable=False),
        pa.field("attributes", _STR_MAP, nullable=False),
    ],
    metadata={b"geo": json.dumps(GEO_METADATA).encode()},
)

COLUMNS: tuple[str, ...] = tuple(INDEX_SCHEMA.names)
MAP_COLUMNS: tuple[str, ...] = ("assets", "checksums", "attributes")
PROMOTED_ATTRIBUTES: tuple[str, ...] = (
    "granule_id", "build_version", "product_version", "collection_version", "day_night", "cloud_fraction",
)
