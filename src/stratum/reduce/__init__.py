"""The reduce stage: the schema-driven built-in reducer (13 section 4, 04 section 5).

`delivered_bands(schema)` says what a run will deliver without running anything;
`reduce_stack(snaps)` produces exactly those bands from one snapshot stack. `Reducer` plugins are
resolved by the planner and refused in the first slice (first-slice plan section 1).
"""
from __future__ import annotations

from stratum.reduce.bands import (
    CATEGORICAL_DTYPE,
    CATEGORICAL_NODATA,
    CONTINUOUS_DTYPE,
    COUNT_DTYPE,
    SchemaError,
    delivered_bands,
    layer_band_count,
    parse_schema,
    resolve_class_names,
    validate_schema,
)
from stratum.reduce.builtin import band_counts, reduce_stack

__all__ = [
    "CATEGORICAL_DTYPE",
    "CATEGORICAL_NODATA",
    "CONTINUOUS_DTYPE",
    "COUNT_DTYPE",
    "SchemaError",
    "band_counts",
    "delivered_bands",
    "layer_band_count",
    "parse_schema",
    "reduce_stack",
    "resolve_class_names",
    "validate_schema",
]
