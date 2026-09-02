"""Discovery, access and read - three seams (12 section 1).

Sources (`LocalSource`, `CMRSource`) and the `AssetStore` live here; readers live in plugin
packages and are found through `reader_for`. Earthdata Login is `stratum.access.auth`, imported
lazily by the store on the first https open so a local run never loads earthaccess.
"""
from __future__ import annotations

from stratum.access.readers import clear_cache, reader_for, reader_ref
from stratum.access.sources import CMRSource, LocalSource, read_header
from stratum.access.store import (
    ASSET_CACHE_ENV,
    TRUSTED_HOSTS,
    AssetCacheUnconfigured,
    AssetFetchError,
    AssetStore,
    AssetStoreError,
    CachedAsset,
    ChecksumMismatch,
    LocalAsset,
    UntrustedScheme,
    asset_cache_for,
    asset_cache_path,
    is_local,
    is_trusted_host,
    local_path,
    to_uri,
)

__all__ = [
    "ASSET_CACHE_ENV", "TRUSTED_HOSTS", "AssetCacheUnconfigured", "AssetFetchError",
    "AssetStore", "AssetStoreError", "CMRSource", "CachedAsset", "ChecksumMismatch",
    "LocalAsset", "LocalSource", "UntrustedScheme", "asset_cache_for", "asset_cache_path",
    "clear_cache", "is_local", "is_trusted_host", "local_path", "read_header", "reader_for",
    "reader_ref", "to_uri",
]
