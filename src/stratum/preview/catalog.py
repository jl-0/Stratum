"""The products tree, as a viewer sees it.

A published products root is already a STAC catalogue (07 section 6): one collection per run,
one item per tile and period, one asset per delivered band plus its legend. So a viewer needs no
layout of its own - it lists the run directories, reads `collection.json`, and follows the item
links. This module is the whole of that knowledge, and it is deliberately small: everything else
in `stratum.preview` renders what this returns.

The root may be a directory or an `s3://` prefix. `Workspace` already makes those the same thing
for every other stage, so it does here too: `materialise` hands back a real path, pulling the
object into the node-local mirror on first touch. A preview session over a bucket therefore warms
into a local mirror one asset at a time, which is what makes panning the second tile instant.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from stratum.storage import Workspace

#: Where published runs live under the storage root (06 section 4). A `--root` that already
#: points at the products directory is accepted as-is; see `Products.open`.
PRODUCTS_DIR = "products"


class CatalogError(RuntimeError):
    """The root is not a products tree, or a path escaped it."""


@dataclass(frozen=True)
class Products:
    """A products root, local or remote, with the mirror already arranged.

    `workspace.path / prefix` is the products directory as this process sees it. Every public
    method takes a path *relative to that directory* and refuses one that climbs out of it - the
    server hands user input straight to these, so the guard belongs here and not at the route.
    """

    workspace: Workspace
    prefix: str = ""

    # -- construction ----------------------------------------------------------------------
    @classmethod
    def open(cls, root: str | Path, *, base_dir: Path | None = None,
             client: Any | None = None) -> Products:
        """Open `root` as a products tree.

        Accepts either the storage root (`.../out`, which has `products/` under it) or the
        products directory itself (`.../out/products`), because both are things someone
        reasonably types. A storage root wins when both readings are possible.
        """
        ws = Workspace.for_root(root, base_dir=base_dir, client=client)
        for prefix in (PRODUCTS_DIR, ""):
            probe = cls(ws, prefix)
            if probe.run_ids():
                return probe
        # Nothing found either way: report against the storage-root reading, which is the one
        # a manifest's `outputs.bucket` names.
        return cls(ws, PRODUCTS_DIR)

    @property
    def uri(self) -> str:
        """The durable name of the products directory - what a deployed viewer is pointed at."""
        base = self.workspace.uri
        return f"{base.rstrip('/')}/{self.prefix}" if self.prefix else base

    @property
    def path(self) -> Path:
        """Where the products directory lives in *this* process. Never durable (06 section 4)."""
        return self.workspace.path / self.prefix if self.prefix else self.workspace.path

    # -- listing ---------------------------------------------------------------------------
    def run_ids(self) -> list[str]:
        """Every run directory under the products root, newest name last.

        Remotely this is a delimited list, so it costs one request per thousand runs and never
        walks the objects inside them - a run holds thousands of files and listing them all to
        learn six names is the difference between a page that loads and one that does not.
        """
        if self.workspace.remote:
            store = self.workspace.store
            assert store is not None
            base = "/".join(p for p in (self.workspace.prefix, self.prefix) if p)
            names = [k.rstrip("/").rsplit("/", 1)[-1]
                     for k in store.list_dirs(f"{base}/" if base else "")]
        elif self.path.is_dir():
            names = [p.name for p in self.path.iterdir() if p.is_dir()]
        else:
            names = []
        return sorted(n for n in names if not n.startswith("."))

    # -- reading ---------------------------------------------------------------------------
    def resolve(self, rel: str) -> Path:
        """A path relative to the products directory, as a local path. Raises on escape.

        The check is on the *resolved* path, so `..`, an absolute path and a symlink out of the
        tree are all caught by the same test.
        """
        base = self.path.resolve()
        target = (self.path / rel.lstrip("/")).resolve()
        if target != base and base not in target.parents:
            raise CatalogError(f"{rel!r} is outside the products root")
        return target

    def materialise(self, rel: str) -> Path:
        """`resolve`, and for a bucket root fetch the object into the mirror if it is not there.

        Idempotent: a file already mirrored is left alone. This is the only place a preview
        session touches the network for data.
        """
        target = self.resolve(rel)
        if not target.is_file() and not self.workspace.pull_file(target):
            raise CatalogError(f"{rel!r} is not in the products tree")
        return target

    def read_json(self, rel: str) -> Any:
        return json.loads(self.materialise(rel).read_text())

    # -- the catalogue ---------------------------------------------------------------------
    def collection(self, run_id: str) -> dict[str, Any]:
        """One run's STAC collection, with the item hrefs rewritten relative to the products
        root so a caller can pass them straight back to `read_json`."""
        doc = self.read_json(f"{run_id}/collection.json")
        for link in doc.get("links", ()):
            if link.get("rel") == "item":
                link["href"] = f"{run_id}/{str(link['href']).lstrip('./')}"
        return doc

    def item_paths(self, run_id: str) -> list[str]:
        return [link["href"] for link in self.collection(run_id).get("links", ())
                if link.get("rel") == "item"]

    def items(self, run_id: str, *, workers: int = 16) -> list[dict[str, Any]]:
        """Every item in a run, hrefs rewritten the same way.

        Fetched in parallel because a multi-tile run is a few hundred small objects and doing
        them one at a time over HTTPS is the whole page-load budget.
        """
        from concurrent.futures import ThreadPoolExecutor

        paths = self.item_paths(run_id)
        with ThreadPoolExecutor(max_workers=min(workers, max(1, len(paths)))) as pool:
            docs = list(pool.map(self._item, paths))
        return [d for d in docs if d is not None]

    def _item(self, rel: str) -> dict[str, Any] | None:
        try:
            doc = self.read_json(rel)
        except (CatalogError, OSError, ValueError):
            return None            # a partially published run is a gap in the map, not an error
        parent = rel.rsplit("/", 1)[0]
        doc["stratum:href"] = rel
        for asset in doc.get("assets", {}).values():
            asset["href"] = f"{parent}/{str(asset['href']).lstrip('./')}"
        return doc


__all__ = ["PRODUCTS_DIR", "CatalogError", "Products"]
