"""Discovery, access and read - three seams (12 section 1).

Sources (`LocalSource`, later `CMRSource`) and the `AssetStore` live here; readers live in plugin
packages and are found through `reader_for`.
"""
from __future__ import annotations

from stratum.access.readers import clear_cache, reader_for, reader_ref
from stratum.access.sources import CMRSource, LocalSource, read_header
from stratum.access.store import AssetStore, LocalAsset, is_local, local_path, to_uri

__all__ = [
    "AssetStore", "CMRSource", "LocalAsset", "LocalSource", "clear_cache", "is_local",
    "local_path", "read_header", "reader_for", "reader_ref", "to_uri",
]
