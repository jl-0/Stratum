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

from stratum.storage import Workspace, ensure_free, local_workspace, pin, touch
from stratum.types import GridDef, TileRef, canonical_hash

# artifact_type -> kind. A file artifact carries a suffix; a directory artifact is renamed whole.
ARTIFACT_KINDS: Mapping[str, str] = {"aux": "file", "glt": "file", "snapshot": "dir",
                                     "ortho": "file", "product": "dir"}
FILE_SUFFIX: Mapping[str, str] = {"aux": ".tif", "glt": ".tif", "ortho": ".tif"}
INPUTS_NAME = ".inputs.json"
# A directory artifact also carries the list of its own members. Locally it is redundant - the
# commit is a whole-directory rename and cannot tear - but a remote commit is N uploads, and
# without the list a reader cannot tell a complete artifact from a prefix that lost an object.
# It is written BEFORE the sidecar, so `.inputs.json` remains the single commit marker.
MEMBERS_NAME = ".members.json"


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

    @property
    def members_path(self) -> Path:
        """`.members.json` inside a directory artifact. Meaningless for a file artifact, which
        is its own member."""
        if not self.is_dir:
            raise ValueError(f"{self.artifact!r} is a file artifact and lists no members")
        return self.path / MEMBERS_NAME


class CacheRoot:
    """A content-addressed store under one root (06 section 4: one per deployment).

    The root is a directory, or a `Workspace` over an `s3://` prefix with a node-local mirror
    (`stratum.storage`). Everything below writes files: what a remote workspace changes is that a
    probe falls back to the bucket before it is a miss, and a commit is followed by an upload in
    the same order the local write used - members first, `.inputs.json` last.
    """

    def __init__(self, root: Path | str | Workspace) -> None:
        self.workspace = root if isinstance(root, Workspace) else local_workspace(root)
        self.root = self.workspace.path

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
        first and the sidecar last, so a partial write never reads as a hit (06 section 3, rule 7).

        On a remote workspace a local miss is not yet a miss: the sidecar is probed in the bucket
        and, when it is there, the artifact is mirrored and the answer is yes. The sidecar is
        probed *first* for the same reason it is written last - it is the commit."""
        if self._present(key) or (self.workspace.remote and self._mirror(key)):
            self._claim(key)
            return True
        return False

    def _claim(self, key: CacheKey) -> None:
        """A hit is a promise to READ, and the read comes after every other hit in the item has
        been resolved - so the artifact is marked used and pinned until the item ends.

        Both halves are load-bearing on a remote root, where the local copy is an evictable
        mirror. `touch` because `relatime` does not advance access time on a read, so a snapshot
        resolve wrote hours ago looks coldest exactly when reduce needs it; `pin` because a
        reduce item must hold 52 snapshots at once and the pull of the 52nd would otherwise be
        free to evict the 1st. A local root is neither mirrored nor evictable, so neither
        applies.
        """
        if not self.workspace.remote:
            return
        # Both paths, because a FILE artifact keeps its sidecar as a SIBLING
        # (`{hash16}.inputs.json`) rather than inside itself. Pinning only the artifact leaves
        # the commit marker evictable, and losing that turns a hit into a miss - harmless but
        # silly, since it re-downloads bytes that never left. A directory artifact's sidecar is
        # inside it and is covered already.
        for path in (key.path, key.inputs_path):
            touch(path)
            pin(path)

    def _present(self, key: CacheKey) -> bool:
        """The artifact and its commit marker are both here - and, for a directory, every member
        the artifact says it has."""
        if not key.inputs_path.is_file():
            return False
        if not key.is_dir:
            return key.path.is_file()
        if not (key.path.is_dir() and key.members_path.is_file()):
            return False
        members = json.loads(key.members_path.read_text())
        return all((key.path / name).is_file() for name in members)

    def _mirror(self, key: CacheKey) -> bool:
        """Fetch a committed artifact from the bucket into the mirror. A directory artifact whose
        sidecar is present but whose members are not is a torn write, not a hit: the mirror is
        cleared and the stage recomputes."""
        if not self.workspace.exists(key.inputs_path):
            return False
        ensure_free(0)
        if key.is_dir:
            self.workspace.pull_tree(key.path)
        else:
            self.workspace.pull_file(key.path)
            self.workspace.pull_file(key.inputs_path)
        if self._present(key):
            return True
        self.workspace.drop_mirror(key.path)
        return False

    # --------------------------------------------------------------------------------- writing
    def write_file(self, key: CacheKey, write: Callable[[Path], None]) -> Path:
        """Atomic file write: `write(tmp)` into a sibling temp name, `os.replace` onto the key
        path, then the sidecar. If `write` raises, the temp file is removed and nothing is left
        at the key path."""
        if key.is_dir:
            raise ValueError(f"{key.artifact!r} is a directory artifact; use write_dir")
        ensure_free(0)
        key.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = key.path.with_name(f".{key.hash16}-{secrets.token_hex(4)}.tmp{key.path.suffix}")
        try:
            write(tmp)
            os.replace(tmp, key.path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        self._write_inputs(key.inputs_path, key.inputs)
        self.workspace.push_file(key.path)
        self.workspace.push_file(key.inputs_path)   # the commit: last, here as locally
        return key.path

    def write_dir(self, key: CacheKey, write: Callable[[Path], None]) -> Path:
        """Atomic directory write: `write(tmp_dir)` fills a sibling temp directory, the sidecar
        is written inside it, then the directory is renamed whole onto the key path. If `write`
        raises, the temp directory is removed."""
        if not key.is_dir:
            raise ValueError(f"{key.artifact!r} is a file artifact; use write_file")
        ensure_free(0)
        key.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = key.path.with_name(f".{key.hash16}-{secrets.token_hex(4)}.tmp")
        try:
            tmp.mkdir()
            write(tmp)
            members = sorted(p.relative_to(tmp).as_posix() for p in tmp.rglob("*") if p.is_file())
            (tmp / MEMBERS_NAME).write_text(json.dumps(members, indent=2) + "\n")
            self._write_inputs(tmp / INPUTS_NAME, key.inputs)
            if key.path.exists():  # a rewrite of an existing entry; rename cannot replace a dir
                shutil.rmtree(key.path)
            os.replace(tmp, key.path)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        self.workspace.push_tree(key.path, last=key.inputs_path)
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


def glt_inputs(granule_id: str, grid: GridDef, max_distance: float | None, regrid_method: str,
               regrid_algo_version: int, source: str | None = None) -> dict[str, Any]:
    """GLT key: granule x grid x max_distance x method x algorithm version (06 section 2).
    No run, AOI, mask or scorer term - geometry does not depend on them (03 section 5,
    06 section 4). `max_distance` is the number actually used, and is null only for a method
    that searches for nothing (`adopt`).

    `adopt` also carries `source_checksum`, and it is the one key term that identifies code
    outside this repository: the GLT is the product's, so the producer's pipeline determines the
    output and a reprocessed granule must not hit a GLT built from the old one. `None` records
    that the source could not be identified - a local granule with no catalogue checksum - which
    is a real hole rather than a hidden one. The field is absent for every other method, so
    existing keys are unchanged.
    """
    inputs = {
        "artifact_type": "glt",
        "granule_id": granule_id,
        "grid_def": grid_def_fields(grid),
        "max_distance": None if max_distance is None else float(max_distance),
        "regrid_method": regrid_method,
        "regrid_algo_version": int(regrid_algo_version),
    }
    if regrid_method == "adopt":
        inputs["source_checksum"] = source
    return inputs


def aux_inputs(alias: str, digest: str, grid: GridDef, resampling: str,
               warp_algo_version: int) -> dict[str, Any]:
    """Aux warp key: source x tile x grid x resampling (06 section 2). The tile is in the key's
    path, so it is not a field here.

    `digest` is the CONTENT of the source, not its URI: a DEM swapped in place under a stable URI
    is the classic silent-staleness bug (06 section 3, rule 4). 06 section 2 names `source_etag`
    for this; a digest of the staged bytes is what is actually used, because `AssetStore` never
    reads a response ETag, an aux file has no catalogue checksum, and a multipart ETag is a hash
    of part-hashes rather than of content. The alias is in the key because it is what a manifest
    and a plugin agree on (05 section 5); two aliases on one file are two artifacts, which costs
    a warp and keeps `stratum cache explain` legible.

    `warp_algo_version` is the 06 section 3 rule 2 guard: without it a fix to the warp serves
    stale rasters forever. `stratum.ancillary.ALGO_HASH` records it against the module's content
    hash and a test fails when the module changes without a bump.
    """
    return {
        "artifact_type": "aux",
        "alias": alias,
        "source_digest": digest,
        "grid_def": grid_def_fields(grid),
        "resampling": resampling,
        "warp_algo_version": int(warp_algo_version),
    }


def ortho_inputs(granule_id: str, role: str, asset_checksum: str | None, var: str, grid: GridDef,
                 resampling: str, warp_algo_version: int) -> dict[str, Any]:
    """Ortho role warp key: granule x role x tile x grid x resampling (12 section 2).

    The ortho twin of `glt_inputs`, and cached for the same reason: an ortho role is warped once
    per (granule, tile) and windowed per block, not re-warped for every block of every epoch
    (05 section 4, "warp once, slice many"). Unlike a GLT this DOES carry the asset's identity
    and the variable, because the pixels - not merely the geometry - are what it holds.
    """
    return {
        "artifact_type": "ortho",
        "granule_id": granule_id,
        "role": role,
        "asset_checksum": asset_checksum,
        "var": var,
        "grid_def": grid_def_fields(grid),
        "resampling": resampling,
        "warp_algo_version": int(warp_algo_version),
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
    "ARTIFACT_KINDS", "INPUTS_NAME", "MEMBERS_NAME", "CacheKey", "CacheRoot", "aux_inputs",
    "glt_inputs", "grid_def_fields", "ortho_inputs", "product_inputs", "snapshot_inputs",
]
