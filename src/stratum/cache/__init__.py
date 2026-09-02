"""Content addressing: key construction, .inputs.json, atomic writes (06).

Every artifact lives at a path derived from a hash of the inputs that determine it - and nothing
else (06 section 1). The local layout is the plan's storage layout:

    {root}/cache/{artifact_type}/{grid_id}/{tx}_{ty}/{hash16}[.tif | /]

A GLT is one file; a snapshot or a product block is a directory of single-band GeoTIFFs. Every
artifact has its canonical inputs beside it as `.inputs.json` so a cache entry can always be
explained (06 section 2, "Key construction"): a file's sidecar is `{hash16}.inputs.json` in the
same directory, a directory's is `{hash16}/.inputs.json` inside it.

Nothing here knows what an artifact contains. The `*_inputs` builders below are the single source
of truth for what enters each key (06 section 2); CLAUDE.md says a change to them changes
`06-caching.md` in the same commit.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from stratum.types import GridDef, TileRef, canonical_hash

# artifact_type -> kind. A file artifact carries a suffix; a directory artifact is renamed whole.
ARTIFACT_KINDS: Mapping[str, str] = {"glt": "file", "snapshot": "dir", "product": "dir"}
FILE_SUFFIX: Mapping[str, str] = {"glt": ".tif"}
INPUTS_NAME = ".inputs.json"


@dataclass(frozen=True)
class CacheKey:
    """Where an artifact lives and why. `hash` is the full `canonical_hash(inputs)`; the path
    uses its first 16 hex digits (06 section 2)."""

    artifact: str
    path: Path
    inputs: Mapping[str, Any]
    hash: str

    @property
    def hash16(self) -> str:
        return self.hash[7:23]

    @property
    def is_dir(self) -> bool:
        return ARTIFACT_KINDS[self.artifact] == "dir"

    @property
    def inputs_path(self) -> Path:
        """The `.inputs.json` sidecar: inside a directory artifact, beside a file artifact."""
        if self.is_dir:
            return self.path / INPUTS_NAME
        return self.path.with_name(f"{self.hash16}{INPUTS_NAME}")


class CacheRoot:
    """A content-addressed store under one root directory (06 section 4: one per deployment)."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    # ------------------------------------------------------------------------------ addressing
    def key(self, artifact: str, grid_id: str, tile: TileRef, inputs: Mapping[str, Any]) -> CacheKey:
        """Build the key for `inputs`. `inputs` must be a plain JSON-able mapping; it is hashed
        canonically (sorted keys, no whitespace) and stored verbatim beside the artifact."""
        if artifact not in ARTIFACT_KINDS:
            raise ValueError(f"unknown artifact type {artifact!r}; one of {sorted(ARTIFACT_KINDS)}")
        digest = canonical_hash(inputs)
        hash16 = digest[7:23]
        directory = self.root / "cache" / artifact / grid_id / f"{tile.tx}_{tile.ty}"
        name = hash16 + FILE_SUFFIX.get(artifact, "")
        return CacheKey(artifact=artifact, path=directory / name, inputs=dict(inputs), hash=digest)

    def hit(self, key: CacheKey) -> bool:
        """True when the artifact AND its `.inputs.json` exist. A write commits the artifact
        first and the sidecar last, so a partial write never reads as a hit (06 section 3, rule 6)."""
        if key.is_dir:
            present = key.path.is_dir()
        else:
            present = key.path.is_file()
        return present and key.inputs_path.is_file()

    # --------------------------------------------------------------------------------- writing
    def write_file(self, key: CacheKey, write: Callable[[Path], None]) -> Path:
        """Atomic file write: `write(tmp)` into a sibling temp name, `os.replace` onto the key
        path, then the sidecar. If `write` raises, the temp file is removed and nothing is left
        at the key path."""
        if key.is_dir:
            raise ValueError(f"{key.artifact!r} is a directory artifact; use write_dir")
        key.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = key.path.with_name(f".{key.hash16}-{secrets.token_hex(4)}.tmp{key.path.suffix}")
        try:
            write(tmp)
            os.replace(tmp, key.path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        self._write_inputs(key.inputs_path, key.inputs)
        return key.path

    def write_dir(self, key: CacheKey, write: Callable[[Path], None]) -> Path:
        """Atomic directory write: `write(tmp_dir)` fills a sibling temp directory, the sidecar
        is written inside it, then the directory is renamed whole onto the key path. If `write`
        raises, the temp directory is removed."""
        if not key.is_dir:
            raise ValueError(f"{key.artifact!r} is a file artifact; use write_file")
        key.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = key.path.with_name(f".{key.hash16}-{secrets.token_hex(4)}.tmp")
        try:
            tmp.mkdir()
            write(tmp)
            self._write_inputs(tmp / INPUTS_NAME, key.inputs)
            if key.path.exists():  # a rewrite of an existing entry; rename cannot replace a dir
                shutil.rmtree(key.path)
            os.replace(tmp, key.path)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        return key.path

    @staticmethod
    def _write_inputs(path: Path, inputs: Mapping[str, Any]) -> None:
        tmp = path.with_name(f"{path.name}.{secrets.token_hex(4)}.tmp")
        tmp.write_text(json.dumps(inputs, sort_keys=True, indent=2, default=str) + "\n")
        os.replace(tmp, path)

    # ------------------------------------------------------------------------------ explaining
    def explain(self, key_or_path: CacheKey | Path | str) -> dict[str, Any]:
        """The canonical inputs of an entry (06 section 6, `stratum cache explain`). For a key
        the stored sidecar wins when present, else the in-memory inputs; for a path, the path may
        be the artifact, its directory, or the sidecar itself."""
        if isinstance(key_or_path, CacheKey):
            if key_or_path.inputs_path.is_file():
                return json.loads(key_or_path.inputs_path.read_text())
            return dict(key_or_path.inputs)
        path = Path(key_or_path)
        if path.name.endswith(INPUTS_NAME):
            sidecar = path
        elif path.is_dir():
            sidecar = path / INPUTS_NAME
        else:
            sidecar = path.with_name(f"{path.stem}{INPUTS_NAME}")
        if not sidecar.is_file():
            raise FileNotFoundError(f"no {INPUTS_NAME} for {path}")
        return json.loads(sidecar.read_text())

    def diff(self, a: CacheKey | Path | str, b: CacheKey | Path | str) -> dict[str, tuple[Any, Any]]:
        """Which inputs differ (06 section 6, `stratum cache diff`): `{field: (a, b)}` for every
        field that differs, nested mappings flattened to dotted names, a missing field as None."""
        fa, fb = _flatten(self.explain(a)), _flatten(self.explain(b))
        return {k: (fa.get(k), fb.get(k)) for k in sorted(fa.keys() | fb.keys())
                if fa.get(k) != fb.get(k)}


def _flatten(doc: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in doc.items():
        name = f"{prefix}{k}"
        if isinstance(v, Mapping):
            out.update(_flatten(v, f"{name}."))
        else:
            out[name] = v
    return out


# ------------------------------------------------------------------------ 06 section 2: the keys
def grid_def_fields(grid: GridDef) -> dict[str, Any]:
    """The `grid_def` term: what fixes the lattice. `block` is a compute knob and never changes
    output (01 section 3 invariant), so it is excluded."""
    return {"crs": grid.crs, "resolution": list(grid.resolution), "origin": list(grid.origin),
            "tile_size": grid.tile_size}


def _key_str(k: CacheKey | str) -> str:
    return k.hash if isinstance(k, CacheKey) else str(k)


def glt_inputs(granule_id: str, grid: GridDef, max_distance: float, regrid_method: str,
               regrid_algo_version: int) -> dict[str, Any]:
    """GLT key: granule x grid x max_distance x method x algorithm version (06 section 2).
    No run, AOI, mask or scorer term - geometry does not depend on them (03 section 5,
    06 section 4). `max_distance` is the number actually used, never None."""
    return {
        "artifact_type": "glt",
        "granule_id": granule_id,
        "grid_def": grid_def_fields(grid),
        "max_distance": float(max_distance),
        "regrid_method": regrid_method,
        "regrid_algo_version": int(regrid_algo_version),
    }


def snapshot_inputs(obs_keys: Sequence[CacheKey | str], aux_keys: Sequence[CacheKey | str],
                    scorer_ref: str, scorer_version: str, scorer_params: Mapping[str, Any],
                    layers_hash: str, epoch_bounds: Sequence[str]) -> dict[str, Any]:
    """Epoch snapshot key (06 section 2). `obs_keys` are stored in the order given, and the
    caller is expected to pass them SORTED: resolve derives its candidate order from
    (datetime, granule_id) of the observations themselves, so the order is a function of the set
    and the sorted list is the set's canonical form - two blocks reached by the same
    observations get the same key however they were enumerated (06 section 3, rule 1)."""
    return {
        "artifact_type": "snapshot",
        "obs_keys": [_key_str(k) for k in obs_keys],
        "aux_keys": [_key_str(k) for k in aux_keys],
        "scorer_ref": scorer_ref,
        "scorer_version": scorer_version,
        "scorer_params": dict(scorer_params),
        "layers_hash": layers_hash,
        "epoch_bounds": list(epoch_bounds),
    }


def product_inputs(snapshot_keys: Sequence[CacheKey | str], aux_keys: Sequence[CacheKey | str],
                   aggregate_hash: str, reducer: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Product block key (06 section 2). `reducer` is `{ref, version, params}` when a plugin is
    named, else None - the built-in reducer is fully described by `aggregate_hash`."""
    return {
        "artifact_type": "product",
        "snapshot_keys": [_key_str(k) for k in snapshot_keys],
        "aux_keys": [_key_str(k) for k in aux_keys],
        "aggregate_hash": aggregate_hash,
        "reducer": dict(reducer) if reducer is not None else None,
    }


__all__ = [
    "ARTIFACT_KINDS", "CacheKey", "CacheRoot", "glt_inputs", "grid_def_fields",
    "product_inputs", "snapshot_inputs",
]
