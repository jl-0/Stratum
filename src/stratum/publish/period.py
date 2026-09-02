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

from stratum.publish.cogs import check_formats, write_data_cogs
from stratum.publish.legend import write_classes, write_legend
from stratum.publish.mappers import build_mappers, write_images
from stratum.publish.stac import stamp, write_stac_item
from stratum.publish.stitch import stitch
from stratum.reduce import delivered_bands
from stratum.types import BlockRef, ClassTable, Epoch, SnapshotSchema, TileRef


@dataclass(frozen=True)
class Published:
    """What one publish item wrote."""

    out_dir: Path
    data: dict[str, Path] = field(default_factory=dict)
    images: dict[str, Path] = field(default_factory=dict)
    legends: dict[str, Path] = field(default_factory=dict)
    classes: Path | None = None
    item: Path | None = None


def period_dirname(period: Epoch) -> str:
    """`{start}_{end}` with `stamp` timestamps, e.g. `20260601_20260701`."""
    return f"{stamp(period.start)}_{stamp(period.end)}"


def product_dir(products_dir: Path, tile: TileRef, period: Epoch) -> Path:
    """`{products_dir}/{tx}_{ty}/{period}` (06 section 4; first-slice plan section 4)."""
    return Path(products_dir) / f"{tile.tx}_{tile.ty}" / period_dirname(period)


def product_class_table(schema: SnapshotSchema) -> ClassTable | None:
    """The product's class table: the one every categorical layer shares. None when the schema
    has no categorical layer with a table; NotImplementedError when layers carry different
    tables - one product, one table, in the first slice (13 section 3)."""
    tables = [(layer.name, layer.classes) for layer in schema.layers
              if layer.kind == "categorical" and layer.classes is not None]
    if not tables:
        return None
    fps = {t.fingerprint() for _, t in tables}
    if len(fps) > 1:
        raise NotImplementedError("categorical layers "
                                  f"{[n for n, _ in tables]} carry different class tables; one "
                                  "product class table per product in the first slice "
                                  "(13 section 3 rule 3)")
    return tables[0][1]


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
                   run_dir: Path | None = None, tags: Mapping[str, Any] | None = None) -> Published:
    """Stitch, write data COGs, render, write legends, classes and the STAC item for one
    (tile, period). `band_counts` is `stratum.reduce.band_counts(snaps)` for a schema whose
    multi-band widths it alone cannot state. `outputs` is the manifest block: `formats`
    (default `[cog]`), `stac` (default true), `render`."""
    out_dir = Path(out_dir)
    check_formats(outputs.get("formats") or ["cog"])
    bands = delivered_bands(schema, band_counts=band_counts)
    mappers = build_mappers(outputs, schema)              # validate before any IO
    stack = stitch(product_dirs, tile, bands)
    class_table = product_class_table(schema)
    cat_layers = [layer.name for layer in schema.layers if layer.kind == "categorical"]
    colors = render_colors(outputs, cat_layers[0]) if cat_layers else None

    all_tags = {"run_id": run_id, "manifest_hash": manifest_hash, "schema": schema.name,
                "period_start": period.start.isoformat(), "period_end": period.end.isoformat(),
                **dict(tags or {})}
    data_paths = write_data_cogs(out_dir, stack, tile, class_table=class_table, tags=all_tags,
                                 colors=colors)
    data = {spec.name: p for spec, p in zip(stack.specs, data_paths, strict=True)}

    images = write_images(out_dir, {n: m.render(stack) for n, m in mappers.items()}, tile,
                          tags=all_tags)
    legends = {n: write_legend(out_dir, n, m.legend()) for n, m in mappers.items()}
    classes = write_classes(out_dir, class_table) if class_table is not None else None

    item = None
    if outputs.get("stac", True):
        item = write_stac_item(out_dir, run_id=run_id, manifest_hash=manifest_hash, tile=tile,
                               period=period, stack=stack, class_table=class_table, colors=colors,
                               data_paths=data, images=images, legends=legends,
                               classes_path=classes, run_dir=run_dir)
    return Published(out_dir=out_dir, data=data, images=images, legends=legends, classes=classes,
                     item=item)
