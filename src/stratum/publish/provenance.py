"""The run record (10 section 2): what a run recorded about itself, immutable, at
`{root}/runs/{run_id}/provenance.json`.

`build_provenance` assembles the documented shape from keyword arguments; the executor (plan and
run) fills them in. Nothing here computes a hash or reads a file: the record is what the run
reports, and the per-artifact `.inputs.json` sidecars (06 section 2) carry the rest.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from stratum import __version__

SCHEMA_VERSION = "1.0"
PROVENANCE_NAME = "provenance.json"
REQUIRED = ("run_id", "manifest_hash", "manifest", "schema_version", "started", "inputs", "code")


def iso_utc(when: datetime | str | None) -> str | None:
    """ISO 8601 with a `Z` suffix; naive datetimes are taken as UTC."""
    if when is None or isinstance(when, str):
        return when
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.astimezone(UTC).isoformat().replace("+00:00", "Z")


def build_provenance(*, run_id: str, manifest_hash: str, manifest: Mapping[str, Any],
                     inputs: Mapping[str, Any], started: datetime | str,
                     finished: datetime | str | None = None,
                     code: Mapping[str, Any] | None = None,
                     plugins: Mapping[str, Any] | None = None,
                     schema: Mapping[str, Any] | None = None,
                     aux: Sequence[Mapping[str, Any]] = (),
                     filters: Sequence[Mapping[str, Any]] = (),
                     execution: Mapping[str, Any] | None = None,
                     derived_from: str | None = None) -> dict[str, Any]:
    """Assemble the 10 section 2 record. `code.stratum_version` defaults to this package's
    version; every other value is recorded as given. `derived_from` is the previous run's id
    (10 section 6, resolution 3)."""
    code_rec = {"stratum_version": __version__, **dict(code or {})}
    record: dict[str, Any] = {
        "run_id": run_id,
        "manifest_hash": manifest_hash,
        "manifest": dict(manifest),
        "schema_version": SCHEMA_VERSION,
        "started": iso_utc(started),
        "finished": iso_utc(finished),
        "inputs": dict(inputs),
        "code": code_rec,
        "plugins": dict(plugins or {}),
        "schema": dict(schema or {}),
        "aux": [dict(a) for a in aux],
        "filters": [dict(f) for f in filters],
        "execution": dict(execution or {}),
    }
    if derived_from is not None:
        record["derived_from"] = derived_from
    return record


def write_provenance(run_dir: Path, record: Mapping[str, Any]) -> Path:
    """Write `provenance.json` under `run_dir`; refuses a record missing a required top-level key."""
    missing = [k for k in REQUIRED if k not in record]
    if missing:
        raise ValueError(f"provenance record lacks {missing} (10 section 2)")
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / PROVENANCE_NAME
    path.write_text(json.dumps(dict(record), indent=2, default=str) + "\n")
    return path


def read_provenance(path: Path) -> dict[str, Any]:
    path = Path(path)
    if path.is_dir():
        path = path / PROVENANCE_NAME
    return json.loads(path.read_text())
