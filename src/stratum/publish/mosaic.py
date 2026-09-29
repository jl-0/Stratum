"""One file per layer: the run-wide, web-mercator-aligned COG a map server reads (07 section 7).

Publish writes one file per product tile per band, which is the right shape for a deliverable
and the wrong one for a map. A viewer drawing a layer of US West asks 402 files for every map
tile at a regional zoom, because every one of them overlaps the view. This step merges each
layer's tiles into ONE file, cut on the web-mercator tile grid: `TILING_SCHEME=GoogleMapsCompatible`
with 256-pixel blocks means every slippy-map tile at every zoom is exactly one internal block of
one file, at full resolution or in the overview that matches the zoom.

It runs AFTER publish, over the published tree, and reads only what publish wrote - the items
and the delivered rasters - so it needs no manifest and can be run, re-run or skipped without
touching a run. The per-tile products stay the deliverable and the full-resolution source a
point query reads; the mosaic is a rendering of them in the grid a web map speaks.

**The web-mercator zoom is chosen by GDAL (`ZOOM_LEVEL_STRATEGY=AUTO`, the closest level).** For
one-arcsecond products that is z12, whose 38 m mercator pixel is about the product's 31 m at the
latitudes of the western US - the file is then about the size of the tiles it merges. `UPPER`
would pick z13 and write four times the pixels to say nothing more.

The sources are read in place - `/vsis3/` for a bucket, via `Workspace.gdal_path` - so building a
mosaic never mirrors the few gigabytes of tiles it reads. Only the output is written locally, then
uploaded and dropped from the mirror.
"""
from __future__ import annotations

import logging
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from stratum.preview.catalog import MOSAIC_DIR, MOSAIC_ROLE, Products
from stratum.publish.cogs import COG_MEDIA_TYPE, is_categorical_dtype, resampling_for
from stratum.publish.stac import add_collection_assets

log = logging.getLogger(__name__)

#: Set by ECS in every task container. `stratum mosaic` builds from a bucket only where this is
#: set - in the Fargate task (`make cloud-mosaic`) - so a run's tiles never cross to a laptop.
ECS_METADATA_ENV = "ECS_CONTAINER_METADATA_URI_V4"
#: One internal block per slippy-map tile.
BLOCKSIZE = 256


@dataclass(frozen=True)
class MosaicJob:
    """One layer of one period: the tile rasters that merge into one mosaic."""

    run_id: str
    period: str                         # the item directory name, `{start}_{end}`
    asset: str                          # the item asset key, e.g. `mineral_1_native`
    sources: tuple[str, ...]            # hrefs relative to the products directory
    nearest: bool                       # categorical or a rendering: decimate, never average
    template: Mapping[str, Any] = field(default_factory=dict, compare=False, hash=False)
    classes: tuple[Mapping[str, Any], ...] = field(default=(), compare=False, hash=False)

    @property
    def rel(self) -> str:
        """Where the mosaic lives, relative to the products directory."""
        return f"{self.run_id}/{MOSAIC_DIR}/{self.period}/{self.asset}.tif"

    @property
    def key(self) -> str:
        """Its collection asset key."""
        return f"{MOSAIC_DIR}:{self.period}:{self.asset}"


def _period(item: Mapping[str, Any]) -> str:
    """The period directory an item was published in - the last directory of its href."""
    return str(item["stratum:href"]).rsplit("/", 2)[-2]


def _is_raster(asset: Mapping[str, Any]) -> bool:
    roles = asset.get("roles") or ()
    return ("data" in roles or "visual" in roles) and str(asset.get("href", "")).endswith(".tif")


def _nearest(asset: Mapping[str, Any]) -> bool:
    """Decimate rather than average: a rendering, or a band holding class ids. The same rule
    publish applies to each tile's own overviews (`cogs.resampling_for`)."""
    if "visual" in (asset.get("roles") or ()):
        return True
    if asset.get("classification:classes"):
        return True
    bands = asset.get("raster:bands") or [{}]
    return is_categorical_dtype(bands[0].get("data_type"), bands[0].get("nodata"))


def _summed_classes(entries: Iterable[Sequence[Mapping[str, Any]]]) -> tuple[dict[str, Any], ...]:
    """Every tile's `classification:classes`, merged: the first tile's names and hints, and each
    class's `count` summed over the tiles that report one. A class no tile counted keeps no
    `count`, so "not counted" is never rewritten as "zero"."""
    merged: dict[int, dict[str, Any]] = {}
    for classes in entries:
        for c in classes:
            value = int(c["value"])
            row = merged.setdefault(value, {k: v for k, v in c.items() if k != "count"})
            if "count" in c:
                row["count"] = int(row.get("count", 0)) + int(c["count"])
    return tuple(merged[v] for v in sorted(merged))


def plan_mosaics(run_id: str, items: Sequence[Mapping[str, Any]], *,
                 layers: Sequence[str] | None = None,
                 periods: Sequence[str] | None = None) -> list[MosaicJob]:
    """One job per (period, raster asset) in the run's items, in publication order.

    `items` are as `Products.items` returns them: asset hrefs relative to the products
    directory. `layers` and `periods` restrict the plan; a name in either that the run does not
    have is an error rather than an empty result, because a typo should not look like success.
    """
    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    found_layers: set[str] = set()
    found_periods: set[str] = set()
    for item in items:
        period = _period(item)
        found_periods.add(period)
        for key, asset in (item.get("assets") or {}).items():
            if not _is_raster(asset):
                continue
            found_layers.add(key)
            if layers is not None and key not in layers:
                continue
            if periods is not None and period not in periods:
                continue
            grouped.setdefault((period, key), []).append(asset)
    for name, wanted, found in (("layer", layers, found_layers),
                                ("period", periods, found_periods)):
        missing = sorted(set(wanted or ()) - found)
        if missing:
            raise ValueError(f"run {run_id!r} has no {name} {', '.join(map(repr, missing))}; "
                             f"it has {', '.join(sorted(found)) or 'none'}")
    return [MosaicJob(run_id=run_id, period=period, asset=key,
                      sources=tuple(a["href"] for a in assets),
                      nearest=_nearest(assets[0]), template=assets[0],
                      classes=_summed_classes(a.get("classification:classes") or ()
                                              for a in assets))
            for (period, key), assets in grouped.items()]


def build_mosaic(sources: Sequence[str], dest: Path, *, nearest: bool,
                 config: Mapping[str, str] | None = None) -> Path:
    """Merge `sources` (GDAL paths) into one web-mercator COG at `dest`.

    A VRT over the sources, then GDAL's COG driver with the GoogleMapsCompatible tiling scheme,
    which reprojects, cuts on the tile grid and builds the overviews in one pass. Written to a
    temporary name and renamed, so an interrupted build never leaves a file that looks done.
    """
    from osgeo import gdal

    gdal.UseExceptions()
    resampling = resampling_for(categorical=nearest)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with gdal.config_options(dict(config or {})), \
            tempfile.TemporaryDirectory(dir=dest.parent, prefix=".mosaic-") as tmp:
        vrt = str(Path(tmp) / "sources.vrt")
        gdal.BuildVRT(vrt, list(sources)).Close()
        staged = Path(tmp) / dest.name
        gdal.Translate(str(staged), vrt, format="COG", creationOptions=[
            "TILING_SCHEME=GoogleMapsCompatible", f"BLOCKSIZE={BLOCKSIZE}",
            f"RESAMPLING={resampling}", f"OVERVIEW_RESAMPLING={resampling}",
            "COMPRESS=DEFLATE", "PREDICTOR=YES", "BIGTIFF=IF_SAFER", "NUM_THREADS=ALL_CPUS",
        ]).Close()
        staged.replace(dest)
    return dest


def mosaic_asset(job: MosaicJob) -> dict[str, Any]:
    """The collection asset for a built mosaic: the tile asset's description, retyped as a COG
    in EPSG:3857, with the tiles' class counts summed."""
    t = job.template
    asset: dict[str, Any] = {
        "href": f"./{MOSAIC_DIR}/{job.period}/{job.asset}.tif",
        "type": COG_MEDIA_TYPE,
        "title": f"{t.get('title', job.asset)} (mosaic)",
        "roles": [*(t.get("roles") or ()), MOSAIC_ROLE],
        "proj:epsg": 3857,
        "stratum:asset": job.asset,
        "stratum:period": job.period,
        "stratum:tiles": len(job.sources),
    }
    if "description" in t:
        asset["description"] = t["description"]
    if "raster:bands" in t:
        asset["raster:bands"] = t["raster:bands"]
    if job.classes:
        asset["classification:classes"] = list(job.classes)
    return asset


def run_mosaics(products: Products, run_id: str, *, layers: Sequence[str] | None = None,
                periods: Sequence[str] | None = None, force: bool = False,
                workers: int = 2) -> list[tuple[MosaicJob, str]]:
    """Build every mosaic a run lacks, then record them all on its `collection.json`.

    A mosaic already present is skipped unless `force`: a run id names its manifest, so a
    published run's tiles do not change under it. Returns `(job, "built" | "present")` per job.
    Layers build `workers` at a time; each one also uses every core inside GDAL, so more than a
    couple mostly buys contention.
    """
    ws = products.workspace
    jobs = plan_mosaics(run_id, products.items(run_id), layers=layers, periods=periods)

    def one(job: MosaicJob) -> tuple[MosaicJob, str]:
        dest = products.resolve(job.rel)
        if not force and (dest.is_file() or ws.exists(dest)):
            return job, "present"
        log.info("mosaic %s: %d tile(s)", job.rel, len(job.sources))
        build_mosaic([ws.gdal_path(products.resolve(s)) for s in job.sources], dest,
                     nearest=job.nearest, config=ws.gdal_config())
        ws.push_file(dest)
        ws.drop_mirror(dest)          # a bucket root keeps it there; the mirror copy is spent
        return job, "built"

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = list(pool.map(one, jobs))

    # Re-read the collection from the root, never from a mirror copy an earlier session left:
    # Finalize may have rewritten it since.
    ws.drop_mirror(products.resolve(f"{run_id}/collection.json"))
    collection = products.materialise(f"{run_id}/collection.json")
    add_collection_assets(collection, {job.key: mosaic_asset(job) for job, _ in results})
    ws.push_file(collection)
    return results


__all__ = ["BLOCKSIZE", "ECS_METADATA_ENV", "MOSAIC_DIR", "MOSAIC_ROLE", "MosaicJob", "build_mosaic", "mosaic_asset",
           "plan_mosaics", "run_mosaics"]
