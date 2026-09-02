"""STAC item and collection (10 section 4): one item per (tile, delivery period), written by
hand as JSON - no pystac.

The item carries the classification extension on every categorical data asset (the authoritative
legend, 07 section 2), `processing` for the software, `proj` for the grid and `raster` for
per-band dtype, nodata and units, plus links to the provenance record and the frozen index. This
catalogue is the delivery boundary: Stratum writes it under `products/{run_id}` and stops.
"""
from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from rasterio.crs import CRS
from rasterio.warp import transform_bounds

from stratum import __version__
from stratum.publish.cogs import COG_MEDIA_TYPE, is_categorical
from stratum.publish.colors import hex_color, resolve_class_colors, table_ids_names
from stratum.publish.provenance import PROVENANCE_NAME, iso_utc
from stratum.types import BandSpec, BandStack, ClassTable, Epoch, TileRef

STAC_VERSION = "1.0.0"
EXT_CLASSIFICATION = "https://stac-extensions.github.io/classification/v1.1.0/schema.json"
EXT_PROCESSING = "https://stac-extensions.github.io/processing/v1.1.0/schema.json"
EXT_PROJ = "https://stac-extensions.github.io/projection/v1.1.0/schema.json"
EXT_RASTER = "https://stac-extensions.github.io/raster/v1.1.0/schema.json"
JSON_MEDIA_TYPE = "application/json"
ITEM_NAME = "item.json"
COLLECTION_NAME = "collection.json"
INDEX_NAME = "index.parquet"


def stamp(when: datetime) -> str:
    """An identifier-safe timestamp: the date when the instant is midnight, else to the second."""
    if (when.hour, when.minute, when.second, when.microsecond) == (0, 0, 0, 0):
        return when.strftime("%Y%m%d")
    return when.strftime("%Y%m%dT%H%M%S")


def item_id(run_id: str, tile: TileRef, period: Epoch) -> str:
    return f"{run_id}_{tile.tx}_{tile.ty}_{stamp(period.start)}"


def tile_geometry(tile: TileRef) -> tuple[dict[str, Any], list[float]]:
    """GeoJSON polygon and bbox of the tile's actual bounds, in WGS84 as STAC requires."""
    crs = CRS.from_user_input(tile.grid.crs)
    w, s, e, n = tile.bounds
    if crs.to_epsg() != 4326:
        w, s, e, n = transform_bounds(crs, CRS.from_epsg(4326), w, s, e, n, densify_pts=21)
    poly = {"type": "Polygon", "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]}
    return poly, [w, s, e, n]


def _nodata(spec: BandSpec) -> Any:
    if spec.nodata is None:
        return None
    if isinstance(spec.nodata, float) and math.isnan(spec.nodata):
        return "nan"
    return spec.nodata


def raster_bands(spec: BandSpec) -> list[dict[str, Any]]:
    """`raster:bands` entries, one per band the spec carries."""
    entry: dict[str, Any] = {"data_type": spec.dtype, "unit": spec.units}
    if _nodata(spec) is not None:
        entry["nodata"] = _nodata(spec)
    return [dict(entry) for _ in range(spec.bands)]


def classification_classes(table: ClassTable,
                           colors: Mapping[str, Sequence[int]] | None = None) -> list[dict[str, Any]]:
    """`classification:classes`: value, name and a `color_hint` for every class that renders
    opaque - an uncoloured class, or a transparent one such as the default `none`, gets none."""
    colours = resolve_class_colors(table, colors, on_unmapped="fail")
    ids, names = table_ids_names(table)
    out = []
    for cid, name in zip(ids, names, strict=True):
        entry: dict[str, Any] = {"value": cid, "name": name}
        rgba = colours[cid]
        if rgba is not None and rgba[3] > 0:
            entry["color_hint"] = hex_color(rgba)
        out.append(entry)
    return out


def _rel(target: Path, out_dir: Path) -> str:
    return os.path.relpath(Path(target), Path(out_dir)).replace(os.sep, "/")


def build_stac_item(*, run_id: str, manifest_hash: str, tile: TileRef, period: Epoch,
                    stack: BandStack, class_table: ClassTable | None,
                    colors: Mapping[str, Sequence[int]] | None = None,
                    data_paths: Mapping[str, Path] | None = None,
                    images: Mapping[str, Path] | None = None,
                    legends: Mapping[str, Path] | None = None,
                    classes_path: Path | None = None, out_dir: Path | None = None,
                    run_dir: Path | None = None, collection_href: str = "../../collection.json",
                    extra_properties: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The item as a dict. Asset hrefs are relative to `out_dir` (the item's directory); with
    `run_dir` given, links to `provenance.json` and `index.parquet` are relative paths too."""
    out_dir = Path(out_dir) if out_dir is not None else None
    geometry, bbox = tile_geometry(tile)
    crs = CRS.from_user_input(tile.grid.crs)
    rows, cols = tile.shape
    props: dict[str, Any] = {
        "datetime": iso_utc(period.start),
        "start_datetime": iso_utc(period.start),
        "end_datetime": iso_utc(period.end),
        "processing:software": {"stratum": __version__},
        "proj:epsg": crs.to_epsg(),
        "proj:transform": [float(v) for v in tile.transform][:9],
        "proj:shape": [rows, cols],
        "stratum:run_id": run_id,
        "stratum:manifest_hash": manifest_hash,
        "stratum:grid_id": tile.grid.id,
        "stratum:tile": [tile.tx, tile.ty],
    }
    if crs.to_epsg() is None:
        props["proj:wkt2"] = crs.to_wkt()
    if class_table is not None:
        props["stratum:class_table_fingerprint"] = class_table.fingerprint()
    props.update(dict(extra_properties or {}))

    def href(p: Path | None, default: str) -> str:
        if p is None or out_dir is None:
            return default
        return "./" + _rel(p, out_dir)

    assets: dict[str, Any] = {}
    for spec in stack.specs:
        asset: dict[str, Any] = {
            "href": href((data_paths or {}).get(spec.name), f"./{spec.name}.tif"),
            "type": COG_MEDIA_TYPE, "title": spec.name, "description": spec.description,
            "roles": ["data"], "raster:bands": raster_bands(spec),
        }
        if is_categorical(spec) and class_table is not None:
            asset["classification:classes"] = classification_classes(class_table, colors)
        assets[spec.name] = asset
    for name, p in (images or {}).items():
        assets[f"{name}_rgba"] = {"href": href(p, f"./{name}_rgba.tif"), "type": COG_MEDIA_TYPE,
                                  "title": f"{name} rendering", "roles": ["visual"]}
    for name, p in (legends or {}).items():
        assets[f"{name}_legend"] = {"href": href(p, f"./{name}_legend.json"),
                                    "type": JSON_MEDIA_TYPE, "title": f"{name} legend",
                                    "roles": ["metadata", "legend"]}
    if classes_path is not None or class_table is not None:
        assets["classes"] = {"href": href(classes_path, "./classes.json"), "type": JSON_MEDIA_TYPE,
                             "title": "product class table", "roles": ["metadata"]}

    links: list[dict[str, Any]] = [
        {"rel": "self", "href": f"./{ITEM_NAME}", "type": JSON_MEDIA_TYPE},
        {"rel": "collection", "href": collection_href, "type": JSON_MEDIA_TYPE},
        {"rel": "parent", "href": collection_href, "type": JSON_MEDIA_TYPE},
    ]
    if run_dir is not None:
        base = out_dir if out_dir is not None else Path(".")
        links.append({"rel": "stratum:provenance", "type": JSON_MEDIA_TYPE,
                      "href": _rel(Path(run_dir) / PROVENANCE_NAME, base)})
        links.append({"rel": "stratum:frozen-index", "type": "application/vnd.apache.parquet",
                      "href": _rel(Path(run_dir) / INDEX_NAME, base)})

    return {
        "type": "Feature",
        "stac_version": STAC_VERSION,
        "stac_extensions": [EXT_CLASSIFICATION, EXT_PROCESSING, EXT_PROJ, EXT_RASTER],
        "id": item_id(run_id, tile, period),
        "collection": run_id,
        "geometry": geometry,
        "bbox": bbox,
        "properties": props,
        "assets": assets,
        "links": links,
    }


def write_stac_item(out_dir: Path, **kwargs: Any) -> Path:
    """Build and write `item.json` under `out_dir`; keyword arguments as `build_stac_item`."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    item = build_stac_item(out_dir=out_dir, **kwargs)
    path = out_dir / ITEM_NAME
    path.write_text(json.dumps(item, indent=2, default=str) + "\n")
    return path


def write_stac_collection(products_dir: Path, items: Sequence[Path], *,
                          collection_id: str | None = None, description: str | None = None,
                          license: str = "various") -> Path:
    """`collection.json` under `products_dir` summarising the items' spatial and temporal
    extents, with one `item` link per item (relative). `collection_id` defaults to the items'
    `stratum:run_id`. `license` is a STAC requirement the manifest does not yet declare."""
    products_dir = Path(products_dir)
    products_dir.mkdir(parents=True, exist_ok=True)
    docs = [(Path(p), json.loads(Path(p).read_text())) for p in items]
    if not docs:
        raise ValueError("a collection needs at least one item")
    run_ids = {d["properties"].get("stratum:run_id") for _, d in docs}
    if collection_id is None:
        if len(run_ids) != 1 or None in run_ids:
            raise ValueError(f"items span run ids {sorted(map(str, run_ids))}; pass collection_id")
        collection_id = next(iter(run_ids))
    bboxes = [d["bbox"] for _, d in docs]
    bbox = [min(b[0] for b in bboxes), min(b[1] for b in bboxes),
            max(b[2] for b in bboxes), max(b[3] for b in bboxes)]
    starts = [d["properties"]["start_datetime"] for _, d in docs]
    ends = [d["properties"]["end_datetime"] for _, d in docs]
    collection = {
        "type": "Collection",
        "stac_version": STAC_VERSION,
        "stac_extensions": [],
        "id": collection_id,
        "description": description or f"Stratum products for run {collection_id}",
        "license": license,
        "extent": {"spatial": {"bbox": [bbox]},
                   "temporal": {"interval": [[min(starts), max(ends)]]}},
        "summaries": {"stratum:tiles": sorted({tuple(d["properties"]["stratum:tile"])
                                               for _, d in docs})},
        "links": [{"rel": "self", "href": f"./{COLLECTION_NAME}", "type": JSON_MEDIA_TYPE},
                  {"rel": "root", "href": f"./{COLLECTION_NAME}", "type": JSON_MEDIA_TYPE},
                  *({"rel": "item", "href": "./" + _rel(p, products_dir), "type": JSON_MEDIA_TYPE}
                    for p, _ in docs)],
    }
    path = products_dir / COLLECTION_NAME
    path.write_text(json.dumps(collection, indent=2, default=list) + "\n")
    return path
