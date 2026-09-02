"""The publish stage: stitch, data COG with colour table, RGBA rendering, legend, STAC,
provenance (07, 10).

The data product ships whatever the image looks like (07 section 2). `publish_period` runs the
whole sequence for one (tile, delivery period); each step is also importable on its own.
"""
from __future__ import annotations

from stratum.publish.cogs import (
    COG_MEDIA_TYPE,
    check_formats,
    internal_tile_size,
    is_categorical,
    write_data_cogs,
    write_geotiff,
)
from stratum.publish.colors import (
    RAMPS,
    UnmappedClassError,
    palette_color,
    resolve_class_colors,
)
from stratum.publish.legend import (
    class_table_record,
    legend_record,
    write_classes,
    write_legend,
)
from stratum.publish.mappers import (
    AlphaFrom,
    CategoricalMapper,
    ContinuousMapper,
    build_mapper,
    build_mappers,
    categorical_legend,
    render,
    write_image,
    write_images,
)
from stratum.publish.period import (
    Published,
    period_dirname,
    product_class_table,
    product_dir,
    publish_period,
)
from stratum.publish.provenance import (
    build_provenance,
    read_provenance,
    write_provenance,
)
from stratum.publish.stac import (
    build_stac_item,
    classification_classes,
    item_id,
    write_stac_collection,
    write_stac_item,
)
from stratum.publish.stitch import read_band, stitch, write_product_block

__all__ = [
    "COG_MEDIA_TYPE",
    "RAMPS",
    "AlphaFrom",
    "CategoricalMapper",
    "ContinuousMapper",
    "Published",
    "UnmappedClassError",
    "build_mapper",
    "build_mappers",
    "build_provenance",
    "build_stac_item",
    "categorical_legend",
    "check_formats",
    "class_table_record",
    "classification_classes",
    "internal_tile_size",
    "is_categorical",
    "item_id",
    "legend_record",
    "palette_color",
    "period_dirname",
    "product_class_table",
    "product_dir",
    "publish_period",
    "read_band",
    "read_provenance",
    "render",
    "resolve_class_colors",
    "stitch",
    "write_classes",
    "write_data_cogs",
    "write_geotiff",
    "write_image",
    "write_images",
    "write_legend",
    "write_product_block",
    "write_provenance",
    "write_stac_collection",
    "write_stac_item",
]
