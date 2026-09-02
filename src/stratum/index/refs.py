"""From index rows to the GranuleRef a worker carries, and role resolution (02 section 5)."""
from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

import pandas as pd

from stratum.types import GranuleRef

COLLECTIONS_ATTR = "collections"


def _is_missing(value: Any) -> bool:
    """None, or a float NaN (pandas' null for object/float columns)."""
    return value is None or (isinstance(value, float) and math.isnan(value))


def _text(value: Any, default: str = "") -> str:
    return default if _is_missing(value) else str(value)


def granule_refs(frame: pd.DataFrame) -> dict[str, GranuleRef]:
    """One GranuleRef per granule_id, rows merged across collections.

    Assets are keyed `"{collection}/{asset}"` so nothing collides, and `checksums` are keyed
    the same way from the index's `checksums` column (02 section 2), so a granule re-delivered
    under the same id with new bytes changes the observation keys that read it (12 section 4).
    For a merged ref `collection` is the FIRST collection alphabetically and the full sorted set is in
    `attributes["collections"]`, comma-separated. The scalar fields (times, bbox, versions,
    cloud fraction, day/night) and `attributes` come from that first collection's row; the other
    rows' own values remain in the frame, not the ref."""
    refs: dict[str, GranuleRef] = {}
    ordered = frame.sort_values(["granule_id", "collection"], kind="stable")
    for gid, rows in ordered.groupby("granule_id", sort=True):
        head = rows.iloc[0]
        assets: dict[str, str] = {}
        checksums: dict[str, str] = {}
        for _, row in rows.iterrows():
            for asset, uri in dict(row["assets"]).items():
                assets[f"{row['collection']}/{asset}"] = str(uri)
            for asset, digest in dict(row.get("checksums") or {}).items():
                checksums[f"{row['collection']}/{asset}"] = str(digest)
        collections = sorted(set(rows["collection"]))
        attributes = {str(k): str(v) for k, v in dict(head["attributes"]).items()}
        attributes[COLLECTIONS_ATTR] = ",".join(collections)
        cloud = head["cloud_fraction"]
        cloud = None if _is_missing(cloud) else float(cloud)
        day_night = head["day_night"]
        refs[str(gid)] = GranuleRef(
            granule_id=str(gid),
            collection=collections[0],
            datetime=pd.Timestamp(head["datetime"]).to_pydatetime(),
            end_datetime=pd.Timestamp(head["end_datetime"]).to_pydatetime(),
            bbox=tuple(float(x) for x in head["bbox"]),
            assets=assets,
            build_version=_text(head["build_version"]),
            product_version=_text(head["product_version"]),
            collection_version=_text(head["collection_version"]),
            cloud_fraction=cloud,
            day_night=None if _is_missing(day_night) else str(day_night),
            attributes=attributes,
            checksums=checksums,
        )
    return refs


def role_asset(ref: GranuleRef, role: Any) -> str | None:
    """The `assets` key a role resolves to on one granule (the `"{collection}/{asset}"` name
    behind `role_uri`), so its `checksums` entry can be looked up; None when the role has no
    asset on the granule."""
    uri = role_uri(ref, role)
    if uri is None:
        return None
    return next(k for k, v in ref.assets.items() if v == uri)


def _role_field(role: Any, name: str) -> Any:
    if isinstance(role, Mapping):
        return role.get(name)
    return getattr(role, name, None)


def role_uri(ref: GranuleRef, role: Any) -> str | None:
    """The asset URI a role resolves to on one granule (02 section 5).

    `role` is anything with `collection` and optional `asset` (a manifest RoleSpec, or a
    mapping). `asset` picks `"{collection}/{asset}"`; otherwise the collection's PRIMARY asset,
    which is the first asset name for that collection in sorted order (`MIN` before
    `MINUNCERT`). A ref whose assets are not collection-prefixed (built outside `granule_refs`)
    is accepted when its `collection` matches the role's. None when the granule has no asset for
    the role, which the planner treats as "this granule does not contribute to this role"."""
    collection = _role_field(role, "collection")
    asset = _role_field(role, "asset")
    if not collection:
        raise ValueError("a role must declare a collection (02 section 5)")
    prefix = f"{collection}/"
    prefixed = {k[len(prefix):]: v for k, v in ref.assets.items() if k.startswith(prefix)}
    if not prefixed and ref.collection == collection:
        prefixed = {k: v for k, v in ref.assets.items() if "/" not in k}
    if not prefixed:
        return None
    if asset:
        return prefixed.get(str(asset))
    return prefixed[min(prefixed)]
