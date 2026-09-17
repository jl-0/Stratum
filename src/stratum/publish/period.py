"""One publish work item: `{"tile": [tx, ty], "period": [start, end]}` (first-slice plan
section 4) -> `products/{run_id}/{tx}_{ty}/{period}/` with data COGs, images, legends,
`classes.json` and `item.json`.

This is the sequence the stage runs; every step is a public function the executor may also call
on its own. The manifest `outputs` block arrives as a plain dict; the manifest models adapt to it.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from stratum.publish.cogs import check_formats, raster_format, write_data_cogs
from stratum.publish.legend import write_classes, write_legend
from stratum.publish.mappers import build_mappers, write_images
from stratum.publish.stac import stamp, write_stac_item
from stratum.publish.stitch import stitch
from stratum.reduce import product_bands
from stratum.types import BlockRef, ClassTable, Epoch, SnapshotSchema, TileRef


@dataclass(frozen=True)
class Published:
    """What one publish item wrote."""

    out_dir: Path
    data: dict[str, Path] = field(default_factory=dict)
    images: dict[str, Path] = field(default_factory=dict)
    legends: dict[str, Path] = field(default_factory=dict)
    classes: Path | None = None
    class_files: dict[str, Path] = field(default_factory=dict)
    item: Path | None = None


def period_dirname(period: Epoch) -> str:
    """`{start}_{end}` with `stamp` timestamps, e.g. `20260601_20260701`."""
    return f"{stamp(period.start)}_{stamp(period.end)}"


def product_dir(products_dir: Path, tile: TileRef, period: Epoch) -> Path:
    """`{products_dir}/{tx}_{ty}/{period}` (06 section 4; first-slice plan section 4)."""
    return Path(products_dir) / f"{tile.tx}_{tile.ty}" / period_dirname(period)


def layer_class_tables(schema: SnapshotSchema) -> dict[str, ClassTable]:
    """Layer name -> its own product class table, for every categorical layer that has one.

    A layer's table comes from its enumeration, so two layers over the SAME raw table that lump
    it differently have different product tables - which is the whole point of declaring several
    groupings over one detection band (13 section 3). This is the authoritative per-layer map;
    `product_class_table` is the special case where it collapses to one.
    """
    return {layer.name: layer.classes for layer in schema.layers
            if layer.kind == "categorical" and layer.classes is not None}


def band_class_tables(schema: SnapshotSchema) -> dict[str, ClassTable]:
    """Delivered BAND name -> the class table it is read against.

    A categorical layer L delivers two bands holding product ids, `L` and `L_runner_up`
    (`L_agreement` is a fraction), so both map to L's table. Keyed by band because that is what
    `write_data_cogs` and the STAC asset loop iterate over.
    """
    out: dict[str, ClassTable] = {}
    for name, table in layer_class_tables(schema).items():
        out[name] = table
        out[f"{name}_runner_up"] = table
    return out


def product_class_table(schema: SnapshotSchema) -> ClassTable | None:
    """The single class table every categorical layer shares, or None when there is not one.

    None means two different things and the caller must handle both: a schema with no categorical
    layer at all, and a schema whose categorical layers carry DIFFERENT tables. In the second
    case the product publishes one sidecar per layer instead of one per product - see
    `layer_class_tables`. Before 2026-09-17 this raised NotImplementedError on the second case.
    """
    tables = list(layer_class_tables(schema).values())
    if not tables:
        return None
    fps = {t.fingerprint() for t in tables}
    return tables[0] if len(fps) == 1 else None


def render_colors(outputs: Mapping[str, Any], layer: str) -> Mapping[str, Sequence[int]] | None:
    """The `colors` of the categorical render on `layer`, if any, so the data COG's colour table
    matches the image (07 section 2)."""
    for name, cfg in (outputs.get("render") or {}).items():
        if isinstance(cfg, Mapping) and cfg.get("mapper") == "categorical" \
                and cfg.get("layer", name) == layer and cfg.get("colors") is not None:
            return cfg["colors"]
    return None


def publish_period(out_dir: Path, product_dirs: Sequence[tuple[BlockRef, Path]], tile: TileRef,
                   period: Epoch, schema: SnapshotSchema, outputs: Mapping[str, Any], *,
                   run_id: str, manifest_hash: str, band_counts: Mapping[str, int] | None = None,
                   run_dir: Path | None = None, tags: Mapping[str, Any] | None = None,
                   reducer: Any | None = None) -> Published:
    """Stitch, write data COGs, render, write legends, classes and the STAC item for one
    (tile, period). `band_counts` is `stratum.reduce.band_counts(snaps)` for a schema whose
    multi-band widths it alone cannot state; it is ignored when `reducer` is given, because a
    plugin declares its own widths. `reducer` is the Reducer INSTANCE when the manifest names one,
    so publish stitches the bands the plugin declared rather than the ones the schema implies -
    the two must not disagree. `outputs` is the manifest block: `formats`
    (default `[cog]`), `stac` (default true), `render`."""
    out_dir = Path(out_dir)
    formats = outputs.get("formats") or ["cog"]
    check_formats(formats)
    fmt = raster_format(formats)
    bands = product_bands(schema, reducer, band_counts=band_counts)
    mappers = build_mappers(outputs, schema)              # validate before any IO
    stack = stitch(product_dirs, tile, bands)

    # One product table when every categorical layer shares one - the common case, and what
    # `classes.json` is. Several enumerations over the same detections means there is no single
    # product table, so each categorical BAND carries its own (13 section 3).
    class_table = product_class_table(schema)
    by_layer = layer_class_tables(schema)
    per_band = band_class_tables(schema) if class_table is None else {}
    colors_by_band: dict[str, Any] = {}
    if class_table is None:
        for layer in by_layer:
            cols = render_colors(outputs, layer)
            if cols is not None:
                colors_by_band[layer] = cols
                colors_by_band[f"{layer}_runner_up"] = cols
        colors = None
    else:
        cat_layers = list(by_layer)
        colors = render_colors(outputs, cat_layers[0]) if cat_layers else None

    all_tags = {"run_id": run_id, "manifest_hash": manifest_hash, "schema": schema.name,
                "period_start": period.start.isoformat(), "period_end": period.end.isoformat(),
                **dict(tags or {})}
    data_paths = write_data_cogs(out_dir, stack, tile, class_table=class_table, tags=all_tags,
                                 fmt=fmt, colors=colors,
                                 class_tables=per_band or None,
                                 colors_by_band=colors_by_band or None)
    data = {spec.name: p for spec, p in zip(stack.specs, data_paths, strict=True)}

    images = write_images(out_dir, {n: m.render(stack) for n, m in mappers.items()}, tile,
                          tags=all_tags, fmt=fmt)
    legends = {n: write_legend(out_dir, n, m.legend()) for n, m in mappers.items()}
    classes = write_classes(out_dir, class_table) if class_table is not None else None
    class_files = ({} if class_table is not None
                   else {n: write_classes(out_dir, tb, layer=n) for n, tb in by_layer.items()})

    item = None
    if outputs.get("stac", True):
        item = write_stac_item(out_dir, run_id=run_id, manifest_hash=manifest_hash, tile=tile,
                               period=period, stack=stack, class_table=class_table, colors=colors,
                               class_tables=per_band or None,
                               class_tables_by_layer=(by_layer if class_table is None else None),
                               colors_by_band=colors_by_band or None,
                               data_paths=data, images=images, legends=legends,
                               classes_path=classes, classes_paths=class_files or None,
                               run_dir=run_dir, fmt=fmt)
    return Published(out_dir=out_dir, data=data, images=images, legends=legends, classes=classes,
                     class_files=class_files, item=item)
