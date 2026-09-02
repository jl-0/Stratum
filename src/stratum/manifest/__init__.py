"""The run manifest: models, loader, hash and static validation (09).

    m = load_manifest(Path("manifest.yaml"))
    m.run_id, m.epochs(), m.delivery_periods(), m.snapshot_schema(), validate_static(m)

Patch composition (`-p`, 09 section 3) is parsed and refused in this slice.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from stratum.manifest.models import (
    REF_PREFIX,
    SCHEMA_VERSION,
    AggregateSpec,
    AlphaFromSpec,
    AoiSpec,
    AuxSpec,
    BandAliasSpec,
    BudgetSpec,
    ClassTableSpec,
    DeliverSpec,
    GranuleFilterSpec,
    GridSpec,
    InputsSpec,
    LayerModel,
    Manifest,
    OutputsSpec,
    PixelMaskSpec,
    PluginRef,
    PluginsSpec,
    RenderSpec,
    RoleSpec,
    SnapshotSpec,
    SourceSpec,
    TimeSpec,
)
from stratum.manifest.validate import validate_static
from stratum.types import canonical_hash

__all__ = [
    "REF_PREFIX",
    "SCHEMA_VERSION",
    "AggregateSpec",
    "AlphaFromSpec",
    "AoiSpec",
    "AuxSpec",
    "BandAliasSpec",
    "BudgetSpec",
    "ClassTableSpec",
    "DeliverSpec",
    "GranuleFilterSpec",
    "GridSpec",
    "InputsSpec",
    "LayerModel",
    "Manifest",
    "OutputsSpec",
    "PixelMaskSpec",
    "PluginRef",
    "PluginsSpec",
    "RenderSpec",
    "RoleSpec",
    "SnapshotSpec",
    "SourceSpec",
    "TimeSpec",
    "load_document",
    "load_manifest",
    "manifest_hash",
    "validate_static",
]


def load_document(path: Path | str) -> dict[str, Any]:
    """Parse one YAML file with the safe loader; the top level must be a mapping."""
    path = Path(path)
    doc = yaml.safe_load(path.read_text())
    if not isinstance(doc, Mapping):
        raise TypeError(f"{path}: a manifest is a YAML mapping, got {type(doc).__name__}")
    return dict(doc)


def load_manifest(path: Path | str, patches: Sequence[Path | str] = ()) -> Manifest:
    """Load, validate, and resolve every `@ref:` classes file relative to the manifest's
    directory. A malformed or missing classes file fails here, not at plan time."""
    if patches:
        raise NotImplementedError("manifest patch composition (-p) is a later slice "
                                  "(09 section 3)")
    path = Path(path)
    m = Manifest.from_document(load_document(path), base_dir=path.parent)
    m.enumerations()
    return m


def manifest_hash(m: Manifest) -> str:
    """`canonical_hash` of the merged document as written - sorted keys, durations and dates as
    strings, `deliver` in long form - plus the fingerprint of every enumeration a `@ref:`
    resolved to. Key order in the YAML never changes it. The `@ref:` PATH enters as written
    (it is part of the document); the file's CONTENT enters as `Enumeration.fingerprint()`, so
    editing a classes file under the same path changes `run_id` too, as 00 section 5 invariant 4
    ("reproducible from its manifest hash alone") requires. A manifest without `@ref:` layers
    hashes exactly as its document."""
    doc = m.document()
    refs = {f"snapshot.layers.{name}.classes": enum.fingerprint()
            for name, enum in m.enumerations().items()}
    if refs:
        doc["resolved_refs"] = refs
    return canonical_hash(doc)
