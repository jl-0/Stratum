"""Legend and class-table sidecars (07 section 2; 13 section 3 rule 3; 11 section 9).

`{layer}_legend.json` per rendered output carries the legend; `classes.json` carries the
product's own class table as records. Both are derived from the same resolution the STAC item's
`classification:classes` uses, which is the authoritative copy.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from stratum.hooks import Legend
from stratum.publish.mappers import categorical_legend
from stratum.types import ClassTable

CLASSES_NAME = "classes.json"


def legend_record(legend: Legend) -> dict[str, Any]:
    """The JSON shape: {"kind", "entries": [...], "note"?}."""
    doc: dict[str, Any] = {"kind": legend.kind, "entries": [dict(e) for e in legend.entries]}
    if legend.note:
        doc["note"] = legend.note
    return doc


def legend_path(out_dir: Path, layer: str) -> Path:
    return Path(out_dir) / f"{layer}_legend.json"


def write_legend(out_dir: Path, layer: str, enumeration: Legend | ClassTable, *,
                 colors: Mapping[str, Sequence[int]] | None = None,
                 on_unmapped: str = "fail") -> Path:
    """Write `{layer}_legend.json`. `enumeration` is either a finished `Legend` (from a mapper)
    or a product `ClassTable`, which becomes a categorical legend through `colors`."""
    legend = enumeration if isinstance(enumeration, Legend) \
        else categorical_legend(enumeration, colors, on_unmapped=on_unmapped)
    path = legend_path(out_dir, layer)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(legend_record(legend), indent=2, default=str) + "\n")
    return path


def class_table_record(table: ClassTable) -> dict[str, Any]:
    """The product class table as records: key, source, fingerprint and one record per row."""
    return {"key": table.key, "source": table.source, "fingerprint": table.fingerprint(),
            "columns": list(table.entries.column_names),
            "entries": table.entries.to_pylist()}


def write_classes(out_dir: Path, class_table: ClassTable) -> Path:
    """Write `classes.json` - the product publishes its own class table (13 section 3 rule 3)."""
    path = Path(out_dir) / CLASSES_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(class_table_record(class_table), indent=2, default=str) + "\n")
    return path
