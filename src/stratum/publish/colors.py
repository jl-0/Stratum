"""Colour resolution shared by the data COG colour table, the config mappers and the legend
(07 section 2).

The legend is authoritative and the colour table and RGBA image are derived from it, so all three
must resolve a class to the same colour. This module is the one place that resolution happens.
Nothing here knows what a class means: it sees a class table with a key and a `name` column.
"""
from __future__ import annotations

import colorsys
from collections.abc import Iterable, Mapping, Sequence

import numpy as np

from stratum.types import ClassTable

RGB = tuple[int, int, int]
RGBA = tuple[int, int, int, int]

NONE_ID = 0                                  # the reserved `none` class (13 section 3)
TRANSPARENT: RGBA = (0, 0, 0, 0)
GREY: RGBA = (128, 128, 128, 255)
ON_UNMAPPED = ("fail", "grey", "transparent")

_GOLDEN = 0.618033988749895


class UnmappedClassError(ValueError):
    """Classes present in the data that the colour map does not name, under `on_unmapped: fail`
    (07 section 3)."""


def palette_color(class_id: int) -> RGB:
    """A deterministic colour from a product id alone.

    Hue walks the colour wheel by the golden angle (id x 0.618...), saturation 0.65, value 0.90,
    so neighbouring ids land far apart and a class's colour never depends on which other classes
    exist. Used when a categorical render declares no `colors`, so the map is still readable and
    reproducible; it is not a substitute for a curated colour map.
    """
    if class_id < 0:
        raise ValueError(f"class id {class_id} is negative; product ids are non-negative")
    h = (class_id * _GOLDEN) % 1.0
    r, g, b = colorsys.hsv_to_rgb(h, 0.65, 0.90)
    return (round(r * 255), round(g * 255), round(b * 255))


def parse_rgb(value: object, where: str) -> RGB:
    """Validate a manifest colour: three integers in 0..255."""
    if (isinstance(value, str) or not isinstance(value, Sequence) or len(value) != 3
            or not all(isinstance(c, int) and not isinstance(c, bool) and 0 <= c <= 255
                       for c in value)):
        raise ValueError(f"{where}: colour must be [r, g, b] with integers in 0..255, got {value!r}")
    return (int(value[0]), int(value[1]), int(value[2]))


def table_ids_names(table: ClassTable) -> tuple[list[int], list[str]]:
    """The product ids and names of a class table, in table order. A table with no `name` column
    names its classes by id."""
    ids = [int(k) for k in table.entries.column(table.key).to_pylist()]
    if "name" in table.entries.column_names:
        names = [str(n) for n in table.entries.column("name").to_pylist()]
    else:
        names = [str(i) for i in ids]
    return ids, names


def resolve_class_colors(table: ClassTable, colors: Mapping[str, Sequence[int]] | None = None, *,
                         on_unmapped: str = "fail",
                         present: Iterable[int] | None = None) -> dict[int, RGBA | None]:
    """Product id -> RGBA for every class in `table`; None marks a class left uncoloured.

    Colours are looked up by class NAME in `colors` (07 section 3). A name in `colors` that the
    table lacks is a ValueError - a plan-time mistake. With `colors` None every class takes the
    deterministic palette. Otherwise a class `colors` does not name follows `on_unmapped`:
    `grey`, `transparent`, or `fail` - which leaves it None, and raises UnmappedClassError
    listing the uncoloured names among `present` (the ids actually in the data) when `present`
    is given. `none` (id 0) is never "unmapped": it is transparent unless `colors` names it.
    """
    if on_unmapped not in ON_UNMAPPED:
        raise ValueError(f"on_unmapped {on_unmapped!r} is not one of {ON_UNMAPPED} (07 section 3)")
    ids, names = table_ids_names(table)
    if colors is not None:
        unknown = sorted(set(colors) - set(names))
        if unknown:
            raise ValueError(f"colors name classes not in the class table {table.source!r}: "
                             f"{unknown} (07 section 3)")
    out: dict[int, RGBA | None] = {}
    for cid, name in zip(ids, names, strict=True):
        if colors is not None and name in colors:
            out[cid] = (*parse_rgb(colors[name], f"colors[{name!r}]"), 255)
        elif colors is None:
            out[cid] = TRANSPARENT if cid == NONE_ID else (*palette_color(cid), 255)
        elif cid == NONE_ID:
            out[cid] = TRANSPARENT
        elif on_unmapped == "grey":
            out[cid] = GREY
        elif on_unmapped == "transparent":
            out[cid] = TRANSPARENT
        else:
            out[cid] = None
    if present is not None:
        present_ids = sorted({int(p) for p in present})
        foreign = [p for p in present_ids if p not in out]
        if foreign:
            raise ValueError(f"values {foreign} are not in the class table {table.source!r}; the "
                             "band and its table disagree (11 section 9)")
        uncoloured = [name for cid, name in zip(ids, names, strict=True)
                      if cid in present_ids and out[cid] is None]
        if uncoloured:
            raise UnmappedClassError(f"classes present but not in colors (on_unmapped: fail): "
                                     f"{uncoloured} (07 section 3)")
    return out


def color_lut(colours: Mapping[int, RGBA | None]) -> np.ndarray:
    """(max_id + 1, 4) uint8 lookup; uncoloured classes are transparent."""
    lut = np.zeros((max(colours, default=0) + 1, 4), dtype=np.uint8)
    for cid, rgba in colours.items():
        if rgba is not None:
            lut[cid] = rgba
    return lut


def gdal_colormap(colours: Mapping[int, RGBA | None]) -> dict[int, RGBA]:
    """The rasterio `write_colormap` form of a resolved colour set; uncoloured -> transparent."""
    return {cid: (rgba if rgba is not None else TRANSPARENT) for cid, rgba in colours.items()}


def hex_color(rgba: RGBA) -> str:
    """6-digit hex without `#`, as the STAC classification extension's `color_hint` wants."""
    return f"{rgba[0]:02x}{rgba[1]:02x}{rgba[2]:02x}"


# --------------------------------------------------------------------------------------- ramps
# Eight-stop approximations of the matplotlib viridis and magma ramps at t = 0, 1/7, ..., 1, plus
# a linear grey. Hand-coded so the core does not import matplotlib; a ramp is a legend fact and
# must be reproducible from this table alone.
RAMPS: dict[str, tuple[RGB, ...]] = {
    "viridis": ((68, 1, 84), (70, 50, 126), (54, 92, 141), (39, 127, 142),
                (31, 161, 135), (74, 193, 109), (160, 218, 57), (253, 231, 37)),
    "magma": ((0, 0, 4), (29, 17, 71), (81, 18, 124), (130, 38, 129),
              (183, 55, 121), (241, 96, 93), (254, 159, 109), (252, 253, 191)),
    "grey": tuple((v, v, v) for v in (0, 36, 73, 109, 146, 182, 219, 255)),
}


def ramp_table(name: str) -> np.ndarray:
    """(n_stops, 3) float64 for a named ramp."""
    try:
        return np.asarray(RAMPS[name], dtype=np.float64)
    except KeyError:
        raise ValueError(f"ramp {name!r} is not one of {sorted(RAMPS)} (07 section 3)") from None


def apply_ramp(t: np.ndarray, name: str) -> np.ndarray:
    """Map t in [0, 1] (any shape) to (..., 3) uint8 by linear interpolation between stops."""
    table = ramp_table(name)
    xp = np.linspace(0.0, 1.0, table.shape[0])
    t = np.clip(np.nan_to_num(np.asarray(t, dtype=np.float64), nan=0.0), 0.0, 1.0)
    rgb = np.stack([np.interp(t, xp, table[:, c]) for c in range(3)], axis=-1)
    return np.rint(rgb).astype(np.uint8)


def ramp_stops(name: str, domain: tuple[float, float]) -> list[dict[str, object]]:
    """Legend entries for a ramp: the data value at each stop and its colour."""
    table = ramp_table(name)
    lo, hi = domain
    n = table.shape[0]
    return [{"value": lo + (hi - lo) * i / (n - 1), "color": [int(c) for c in table[i]]}
            for i in range(n)]
