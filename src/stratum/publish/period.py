"""One publish work item: `{"tile": [tx, ty], "period": [start, end]}` ->
`products/{run_id}/{tx}_{ty}/{period}/` with data COGs, images, legends,
`classes.json` and `item.json`.

This is the sequence the stage runs; every step is a public function the executor may also call
on its own. The manifest `outputs` block arrives as a plain dict; the manifest models adapt to it.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa

from stratum.publish.cogs import check_formats, is_categorical, raster_format, write_data_cogs
from stratum.publish.colors import shared_colors
from stratum.publish.legend import write_classes, write_legend
from stratum.publish.mappers import build_mappers, categorical_legend, write_images
from stratum.publish.provenance import iso_utc
from stratum.publish.stac import stamp, write_stac_item
from stratum.publish.stitch import stitch
from stratum.reduce import CATEGORICAL_DTYPE, CATEGORICAL_NODATA, product_bands
from stratum.types import (
    ACQUIRED_UNITS,
    BandSpec,
    BandStack,
    BlockRef,
    ClassTable,
    Epoch,
    GranuleRef,
    SnapshotSchema,
    TileRef,
    acquired_seconds,
)

GRANULES_NAME = "granules.json"
#: Product ids a categorical band can carry: 1..65534, with 0 `none` and 65535 nodata.
MAX_GRANULE_ID = int(CATEGORICAL_NODATA) - 1


@dataclass(frozen=True)
class Published:
    """What one publish item wrote."""

    out_dir: Path
    data: dict[str, Path] = field(default_factory=dict)
    images: dict[str, Path] = field(default_factory=dict)
    legends: dict[str, Path] = field(default_factory=dict)
    classes: Path | None = None
    class_files: dict[str, Path] = field(default_factory=dict)
    granules: Path | None = None
    item: Path | None = None


def period_dirname(period: Epoch) -> str:
    """`{start}_{end}` with `stamp` timestamps, e.g. `20260601_20260701`."""
    return f"{stamp(period.start)}_{stamp(period.end)}"


def product_dir(products_dir: Path, tile: TileRef, period: Epoch) -> Path:
    """`{products_dir}/{tx}_{ty}/{period}` (06 section 4)."""
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


def write_granules(out_dir: Path, stack: Any, schema: SnapshotSchema,
                   granules: Mapping[str, GranuleRef]) -> Path | None:
    """`granules.json`: every `acquired` value delivered in this tile, beside the granule it
    names (10 section 3). The band says WHEN the winning look was taken; this says WHICH granule
    that was, so a viewer can go cell -> granule -> spectrum without the frozen index. None when
    no delivered band is in `ACQUIRED_UNITS`.

    The bands are found by their units, not by the schema's `source: acquired` layers. A reducer
    that carries a look's acquisition through, such as `confirmed_mineral_vote`, delivers it
    under a name of its own. Its product shipped without a `granules.json` until 2026-11-03."""
    _ = schema
    names = [b.name for b in stack.specs if b.units == ACQUIRED_UNITS]
    if not names:
        return None
    by_second: dict[float, list[GranuleRef]] = {}
    for ref in granules.values():
        by_second.setdefault(acquired_seconds(ref.datetime), []).append(ref)
    seen: set[float] = set()
    for name in names:
        values = np.asarray(stack[name], dtype=np.float64)
        seen.update(float(v) for v in np.unique(values[np.isfinite(values)]))
    rows = []
    for s in sorted(seen):
        refs = sorted(by_second.get(s, []), key=lambda r: r.granule_id)
        rows.append({"acquired": s, "datetime": iso_utc(refs[0].datetime) if refs else None,
                     "granule_ids": [r.granule_id for r in refs]})
    path = Path(out_dir) / GRANULES_NAME
    path.write_text(json.dumps({"layers": names, "units": ACQUIRED_UNITS,
                                "granules": rows}, indent=1) + "\n")
    return path


def granule_band_name(acquired_band: str, taken: Sequence[str]) -> str:
    """`acquired_1` -> `granule_1`, `acquired` -> `granule`; `{band}_granule` for any other name,
    or when that name is already a delivered band."""
    if acquired_band.startswith("acquired"):
        name = "granule" + acquired_band[len("acquired"):]
        if name not in taken:
            return name
    return f"{acquired_band}_granule"


def add_granule_bands(stack: BandStack, granules: Mapping[str, GranuleRef]
                      ) -> tuple[BandStack, dict[str, ClassTable]]:
    """For every delivered band in `ACQUIRED_UNITS`, a categorical band whose classes ARE the
    granule ids (10 section 3): the selected scene, readable as a name in any viewer.

    A pixel holds a number, never a string, so the id is a class. The numbering is the run's:
    the frozen index's granules, sorted by id, are 1..N, so every tile numbers a granule the same
    way and the tiles mosaic. Each tile's table lists only the granules present in it, which the
    mosaic merges by value. `acquired_*` stays: it is the exact time, and `granules.json` lists
    every granule sharing a second. Where a second names several granules, the band carries the
    first by id. A run over more than 65,534 granules cannot number them in uint16; it gets no
    granule band, and `granules.json` still answers."""
    order = sorted(granules)
    if not order or len(order) > MAX_GRANULE_ID:
        return stack, {}
    number = {gid: i + 1 for i, gid in enumerate(order)}
    by_second: dict[float, int] = {}
    for gid in order:
        by_second.setdefault(acquired_seconds(granules[gid].datetime), number[gid])

    taken = [spec.name for spec in stack.specs]
    bands = {spec.name: stack[spec.name] for spec in stack.specs}
    specs: list[BandSpec] = []
    tables: dict[str, ClassTable] = {}
    for spec in stack.specs:
        specs.append(spec)
        if spec.units != ACQUIRED_UNITS:
            continue
        name = granule_band_name(spec.name, taken)
        values = np.asarray(stack[spec.name], dtype=np.float64)
        out = np.full(values.shape, CATEGORICAL_NODATA, dtype=np.uint16)
        for v in np.unique(values[np.isfinite(values)]):
            gid = by_second.get(float(v))
            if gid is not None:
                out[values == v] = gid
        present = sorted(int(i) for i in np.unique(out) if i != CATEGORICAL_NODATA)
        tables[name] = ClassTable(key="id", entries=pa.table(
            {"id": [0, *present], "name": ["none", *(order[i - 1] for i in present)]}),
            source=f"granules:{len(order)}")
        bands[name] = out
        specs.append(BandSpec(name, CATEGORICAL_DTYPE,
                              f"{spec.name} as its granule id: the selected scene",
                              nodata=CATEGORICAL_NODATA))
        taken.append(name)
    return BandStack(bands, specs), tables


def render_colors(outputs: Mapping[str, Any], layer: str,
                  table: ClassTable | None = None) -> Mapping[str, Sequence[int]] | None:
    """The colours `layer`'s classes are drawn in, so the data COG's colour table, the image and
    the legend agree (07 section 2): the `colors` of the categorical render on `layer` if it
    names any, else the product-wide `outputs.colors` restricted to `table`'s classes, else
    None - the deterministic palette."""
    for name, cfg in (outputs.get("render") or {}).items():
        if isinstance(cfg, Mapping) and cfg.get("mapper") == "categorical" \
                and cfg.get("layer", name) == layer and cfg.get("colors") is not None:
            return cfg["colors"]
    if table is None:
        return None
    return shared_colors(outputs.get("colors"), table)


def default_legends(out_dir: Path, specs: Sequence[Any], tables: Mapping[str, ClassTable],
                    outputs: Mapping[str, Any], rendered: Sequence[str]) -> dict[str, Path]:
    """`{band}_legend.json` for every categorical delivered band with a class table that no
    render covers (07 section 2). A legend is what tells a viewer a band holds classes and what
    colour each is, so a band without one is drawn as a number - a ramp over class ids. Writing
    one costs a small JSON file and no image; the colours are the data COG's. A class the
    colours leave out is grey."""
    out: dict[str, Path] = {}
    for spec in specs:
        table = tables.get(spec.name)
        if spec.name in rendered or table is None or not is_categorical(spec):
            continue
        colors = render_colors(outputs, spec.name, table)
        legend = categorical_legend(table, colors,
                                    on_unmapped="grey" if colors is not None else "fail")
        out[spec.name] = write_legend(out_dir, spec.name, legend)
    return out


def publish_period(out_dir: Path, product_dirs: Sequence[tuple[BlockRef, Path]], tile: TileRef,
                   period: Epoch, schema: SnapshotSchema, outputs: Mapping[str, Any], *,
                   run_id: str, manifest_hash: str, band_counts: Mapping[str, int] | None = None,
                   run_dir: Path | None = None, tags: Mapping[str, Any] | None = None,
                   reducer: Any | None = None,
                   granules: Mapping[str, GranuleRef] | None = None) -> Published:
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
    stack, granule_tables = (add_granule_bands(stack, granules) if granules is not None
                             else (stack, {}))

    # One product table when every categorical layer shares one - the common case, and what
    # `classes.json` is. Several enumerations over the same detections means there is no single
    # product table, so each categorical BAND carries its own (13 section 3).
    class_table = product_class_table(schema)
    by_layer = layer_class_tables(schema)
    per_band = band_class_tables(schema) if class_table is None else {}
    colors_by_band: dict[str, Any] = {}
    if class_table is None:
        for layer, table in by_layer.items():
            cols = render_colors(outputs, layer, table)
            if cols is not None:
                colors_by_band[layer] = cols
                colors_by_band[f"{layer}_runner_up"] = cols
        colors = None
    else:
        cat_layers = list(by_layer)
        colors = render_colors(outputs, cat_layers[0], class_table) if cat_layers else None

    all_tags = {"run_id": run_id, "manifest_hash": manifest_hash, "schema": schema.name,
                "period_start": period.start.isoformat(), "period_end": period.end.isoformat(),
                **dict(tags or {})}
    # a granule band carries its own table, drawn in the deterministic palette: no class
    # colour table names a scene
    per_band = {**per_band, **granule_tables}
    colors_by_band.update({name: None for name in granule_tables})
    data_paths = write_data_cogs(out_dir, stack, tile, class_table=class_table, tags=all_tags,
                                 fmt=fmt, colors=colors,
                                 class_tables=per_band or None,
                                 colors_by_band=colors_by_band or None)
    data = {spec.name: p for spec, p in zip(stack.specs, data_paths, strict=True)}

    images = write_images(out_dir, {n: m.render(stack) for n, m in mappers.items()}, tile,
                          tags=all_tags, fmt=fmt)
    legends = {n: write_legend(out_dir, n, m.legend()) for n, m in mappers.items()}
    band_tables = {**{s.name: class_table for s in stack.specs if class_table is not None},
                   **per_band}
    legends.update(default_legends(out_dir, stack.specs, band_tables, outputs,
                                   [m.layer for m in mappers.values()] + list(mappers)
                                   + list(granule_tables)))
    legends.update({name: write_legend(out_dir, name, categorical_legend(table))
                    for name, table in granule_tables.items()})
    classes = write_classes(out_dir, class_table) if class_table is not None else None
    class_files = ({} if class_table is not None
                   else {n: write_classes(out_dir, tb, layer=n) for n, tb in by_layer.items()})
    class_files.update({n: write_classes(out_dir, tb, layer=n)
                        for n, tb in granule_tables.items()})

    granules_path = (write_granules(out_dir, stack, schema, granules)
                     if granules is not None else None)

    item = None
    if outputs.get("stac", True):
        item = write_stac_item(out_dir, run_id=run_id, manifest_hash=manifest_hash, tile=tile,
                               period=period, stack=stack, class_table=class_table, colors=colors,
                               class_tables=per_band or None,
                               class_tables_by_layer=(
                                   {**(by_layer if class_table is None else {}), **granule_tables}
                                   or None),
                               colors_by_band=colors_by_band or None,
                               data_paths=data, images=images, legends=legends,
                               classes_path=classes, classes_paths=class_files or None,
                               granules_path=granules_path, run_dir=run_dir, fmt=fmt)
    return Published(out_dir=out_dir, data=data, images=images, legends=legends, classes=classes,
                     class_files=class_files, granules=granules_path, item=item)
