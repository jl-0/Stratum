"""A viewer for a published products tree.

Two ways to look at the same thing, sharing one page:

* **`stratum preview`** runs a local server that renders tiles with rasterio. It works over a
  directory or an `s3://` prefix, and over `gtiff` output as well as `cog`, because nothing is
  asked of the raster's layout.
* **`make deploy-viewer`** copies the page next to the products in a bucket, where it runs with
  no server at all: the browser range-reads the COGs. That one needs `formats: [cog]`.

Neither is part of a run. Nothing in `stratum.plan`, `stratum.resolve`, `stratum.reduce` or
`stratum.publish` imports this package, and it writes nothing into the products tree - a viewer
that could change what it shows would not be a viewer.
"""
from __future__ import annotations

from stratum.preview.catalog import PRODUCTS_DIR, CatalogError, Products

__all__ = ["PRODUCTS_DIR", "CatalogError", "Products"]
