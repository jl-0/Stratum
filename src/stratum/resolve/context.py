"""What a resolve worker needs beyond the work item: `PlanContext` (first-slice plan section 4,
"plan.json carries everything a worker needs that is not in the manifest").

Every field is plain data or an already-constructed plugin instance, so the plan stage can
build one from `plan.json` + the manifest and a process-pool worker can build its own from the
same files. Nothing here reads a granule.
"""
from __future__ import annotations

import json
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from importlib.metadata import (
    PackageNotFoundError,
    distributions,
    packages_distributions,
    version,
)
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse
from urllib.request import url2pathname

import numpy as np
from shapely.geometry.base import BaseGeometry

from stratum.cache import CacheRoot
from stratum.classes import Remap
from stratum.hooks import PixelMask, Scorer
from stratum.types import (
    AssetHandle,
    AuxAccessor,
    AuxTable,
    Epoch,
    GranuleFrame,
    GranuleReader,
    GranuleRef,
    GridDef,
    SnapshotSchema,
)

AUX_NOT_IN_SLICE = "aux is not in the first slice (05)"


class NullAux(AuxAccessor):
    """The AuxAccessor of the first slice: every alias is undeclared (first-slice plan
    section 1, "AuxAccessor exists, every declared alias raises 'not implemented'")."""

    def raster(self, alias: str, *, date: datetime | None = None,
               epoch: Epoch | None = None) -> np.ndarray:
        raise NotImplementedError(f"{AUX_NOT_IN_SLICE}: raster({alias!r})")

    def vector(self, alias: str) -> np.ndarray:
        raise NotImplementedError(f"{AUX_NOT_IN_SLICE}: vector({alias!r})")

    def features(self, alias: str, *, margin: float = 0.0) -> Sequence[BaseGeometry]:
        raise NotImplementedError(f"{AUX_NOT_IN_SLICE}: features({alias!r})")

    def distance(self, alias: str, *, cutoff: float | None = None) -> np.ndarray:
        raise NotImplementedError(f"{AUX_NOT_IN_SLICE}: distance({alias!r})")

    def table(self, alias: str) -> AuxTable:
        raise NotImplementedError(f"{AUX_NOT_IN_SLICE}: table({alias!r})")

    @property
    def granule_index(self) -> GranuleFrame:
        raise NotImplementedError(f"{AUX_NOT_IN_SLICE}: granule_index")


class Store(Protocol):
    """The part of `AssetStore` resolve uses (12 section 4). Duck-typed so a test can hand in
    an in-memory store. `checksum` is the index's catalogue digest for the asset
    (`GranuleRef.checksums`), which a remote store verifies on first touch and keys its
    node-local cache on; a local store accepts and ignores it."""

    def open(self, uri: str, *, etag: str | None = None,
             checksum: str | None = None) -> AssetHandle: ...


@dataclass(frozen=True)
class RoleBinding:
    """A role as resolve reads it (02 section 5, 12 section 6): which collection's asset and
    which variable. Shaped so `stratum.index.role_uri` accepts it directly."""

    collection: str
    var: str
    asset: str | None = None

    @classmethod
    def from_spec(cls, spec: Any) -> RoleBinding:
        """From a manifest RoleSpec (or any object / mapping with collection, var, asset)."""
        get = spec.get if isinstance(spec, Mapping) else lambda k, d=None: getattr(spec, k, d)
        return cls(collection=str(get("collection")), var=str(get("var")), asset=get("asset"))


@dataclass(frozen=True)
class AliasBinding:
    """A band alias resolved to an index (11 section 5). `match:` aliases are resolved to a band
    index by the planner against a reader's `VarSpec.band_attrs` (12 section 7); resolve only
    ever sees the index."""

    role: str
    band: int


@dataclass(frozen=True)
class PluginBinding:
    """A resolved science plugin: the instance plus what enters a cache key for it (06 section 3
    rule 3): its `ref` as the manifest wrote it, its distribution version, its params."""

    ref: str
    version: str
    params: Mapping[str, Any]
    instance: Any

    def identity(self) -> dict[str, Any]:
        return {"ref": self.ref, "version": self.version, "params": dict(self.params)}


def plugin_version(obj: Any) -> str:
    """The version of the distribution that owns `obj`'s module, else "unversioned".

    Looks the top-level package up in the installed distributions' metadata, then tries a
    distribution of the same name, then - for an editable install, which records no top-level
    list - the distribution whose `direct_url.json` source directory contains the module file.
    A wheel that installs no metadata is "unversioned", which is why 06 section 3 rule 3 also
    wants a content hash once plugin wheels exist; not in this slice."""
    module = obj.__module__ if isinstance(obj, type) else type(obj).__module__
    top = module.split(".")[0]
    for dist in packages_distributions().get(top, []):
        try:
            return version(dist)
        except PackageNotFoundError:
            continue
    try:
        return version(top)
    except PackageNotFoundError:
        pass
    file = getattr(sys.modules.get(module), "__file__", None)
    if file:
        path = Path(file).resolve()
        for dist in distributions():
            text = dist.read_text("direct_url.json")
            if not text:
                continue
            info = json.loads(text)
            url = str(info.get("url", ""))
            if not info.get("dir_info", {}).get("editable") or not url.startswith("file:"):
                continue
            root = Path(url2pathname(urlparse(url).path)).resolve()
            # the package itself, not anything else under the source tree (tests, examples)
            if any(pkg in path.parents for pkg in (root / top, root / "src" / top)):
                return dist.version
    return "unversioned"


@dataclass
class PlanContext:
    """Everything `resolve_block` needs that is not in the work item.

    Fields, in the order a planner would fill them:

    grid            The run's GridDef (01 section 1); `grid.id` names the cache shard.
    cache           The content-addressed store (06); GLTs are read from it, snapshots written.
    store           Opens asset URIs into handles (12 section 4); `AssetStore` in a real run.
    granules        granule_id -> GranuleRef for every granule in the frozen index the run may
                    touch (02 section 5). Candidates for a block are found here by bbox/time.
    roles           role name -> RoleBinding, from `inputs.roles`.
    aliases         alias name -> AliasBinding, from `inputs.band_aliases`, match already
                    resolved to an index.
    geolocation_role
                    The role whose `loc` built the GLTs (03 section 3, plan section 5). The GLT
                    indexes THAT role's sensor array, so its VarSpec.shape is the sensor shape a
                    sensor-space mask sees when the role is read.
    schema          The run's SnapshotSchema, classes resolved (13); `layers_hash` keys snapshots.
    scorer          The Scorer plugin and its key identity (04 section 4, 06 section 2).
    masks           PixelMask plugins in manifest order, each with its key identity (04 section 3);
                    sensor-space ones run before the gather, map-space ones after (12 section 2).
    remaps          layer name -> granule_id -> Remap for every categorical layer: the raw->product
                    lookups the planner resolved per granule (13 section 3 rule 2). A `source`
                    layer carries identity remaps. Missing granule -> KeyError at resolve.
    max_distance    The KD-tree threshold regrid actually used (03 section 3); enters the GLT key.
    regrid_method   "kdtree" in this slice.
    regrid_algo_version
                    stratum.regrid.REGRID_ALGO_VERSION at plan time.
    reader_overrides
                    `inputs.readers`: collection -> plugin ref (12 section 3 rule 1).
    reader_lookup   Optional: collection -> GranuleReader; defaults to stratum.access.reader_for
                    with `reader_overrides`. A test injects a fake reader here.
    """

    grid: GridDef
    cache: CacheRoot
    store: Store
    granules: Mapping[str, GranuleRef]
    roles: Mapping[str, RoleBinding]
    aliases: Mapping[str, AliasBinding]
    geolocation_role: str
    schema: SnapshotSchema
    scorer: PluginBinding
    masks: Sequence[PluginBinding]
    remaps: Mapping[str, Mapping[str, Remap]]
    max_distance: float
    regrid_method: str = "kdtree"
    regrid_algo_version: int = 1
    reader_overrides: Mapping[str, str] = field(default_factory=dict)
    reader_lookup: Callable[[str], GranuleReader] | None = None

    def __post_init__(self) -> None:
        for name in self.aliases:
            if name in self.roles:
                raise ValueError(f"{name!r} is both a role and a band alias (13 section 2: one "
                                 "namespace)")
        for name, alias in self.aliases.items():
            if alias.role not in self.roles:
                raise ValueError(f"band alias {name!r} names unknown role {alias.role!r}")
        for m in self.masks:
            if getattr(m.instance, "space", None) not in ("sensor", "map"):
                raise ValueError(f"mask {m.ref!r} must declare space 'sensor' or 'map' "
                                 "(04 section 3)")
        if self.scorer.instance.capability != "streaming":
            raise NotImplementedError(
                f"scorer {self.scorer.ref!r} has capability "
                f"{self.scorer.instance.capability!r}; only 'streaming' is in this slice "
                "(04 section 4, first-slice plan section 1)")

    # ------------------------------------------------------------------------------ plugins
    @property
    def scorer_instance(self) -> Scorer:
        return self.scorer.instance

    @property
    def sensor_masks(self) -> list[PixelMask]:
        return [m.instance for m in self.masks if m.instance.space == "sensor"]

    @property
    def map_masks(self) -> list[PixelMask]:
        return [m.instance for m in self.masks if m.instance.space == "map"]

    def reader(self, collection: str) -> GranuleReader:
        if self.reader_lookup is not None:
            return self.reader_lookup(collection)
        from stratum.access import reader_for  # local: keeps entry-point scanning off import

        return reader_for(collection, self.reader_overrides)

    # ------------------------------------------------------------------------------- names
    def role_of(self, name: str) -> str:
        """The role behind a role-or-alias name (13 section 2: one namespace)."""
        if name in self.roles:
            return name
        if name in self.aliases:
            return self.aliases[name].role
        raise KeyError(f"{name!r} is neither a role nor a band alias; declared: "
                       f"{sorted(self.roles)} / {sorted(self.aliases)}")

    def roles_to_read(self) -> list[str]:
        """Every role an observation must carry: the schema's sources, the scorer's and the
        masks' `required_roles`, each resolved through aliases to its role. Manifest order."""
        wanted: set[str] = set()
        for layer in self.schema.layers:
            wanted.add(self.role_of(layer.source))
        for name in self.scorer.instance.required_roles:
            wanted.add(self.role_of(name))
        for m in self.masks:
            for name in getattr(m.instance, "required_roles", ()):
                wanted.add(self.role_of(name))
        return [r for r in self.roles if r in wanted]


__all__ = [
    "AUX_NOT_IN_SLICE", "AliasBinding", "NullAux", "PlanContext", "PluginBinding", "RoleBinding",
    "Store", "plugin_version",
]
