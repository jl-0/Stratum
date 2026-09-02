"""Config mappers: the ordinary renderings declared in the manifest, no code (07 section 3).

`categorical` colours a class band through its enumeration; `continuous` runs a value through a
ramp over a domain. Both satisfy the `OutputMapper` protocol (07 section 4) so publish treats
them like a plugin mapper would be treated; `aux` is accepted and ignored. `threshold` and
`composite` are later slices. Output is (H, W, 4) uint8 and is written as a georeferenced 4-band
GeoTIFF, `{name}_rgba.tif`.

The render block is keyed by output name: `outputs.render.{name}.{mapper, layer, ...}`, with
`layer` defaulting to the name (the manifest example keys renders by layer; 07 section 3 shows a
separate output name with `layer:`). Both forms work.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from rasterio.enums import ColorInterp

from stratum.hooks import ImageSpec, Legend
from stratum.publish.colors import (
    ON_UNMAPPED,
    apply_ramp,
    color_lut,
    ramp_stops,
    ramp_table,
    resolve_class_colors,
    table_ids_names,
)
from stratum.reduce import CATEGORICAL_NODATA
from stratum.types import BandStack, ClassTable, SnapshotSchema, TileRef

CONFIG_MAPPERS = ("categorical", "continuous")
LATER_MAPPERS = ("threshold", "composite")
_CAT_KEYS = frozenset({"mapper", "layer", "colors", "nodata", "on_unmapped", "alpha_from"})
_CONT_KEYS = frozenset({"mapper", "layer", "ramp", "domain", "clip", "band", "alpha_from"})


def _pair(value: object, where: str) -> tuple[float, float]:
    if (isinstance(value, str) or not isinstance(value, Sequence) or len(value) != 2
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value)):
        raise ValueError(f"{where}: expected [lo, hi], got {value!r}")
    return (float(value[0]), float(value[1]))


def _refuse_keys(cfg: Mapping[str, Any], allowed: frozenset[str], where: str) -> None:
    extra = sorted(set(cfg) - allowed)
    if extra:
        raise ValueError(f"{where}: unknown key(s) {extra} (07 section 3)")


@dataclass(frozen=True)
class AlphaFrom:
    """Confidence-driven alpha (07 section 3): `band` interpolated from `domain` into `range`."""

    band: str
    domain: tuple[float, float]
    range: tuple[float, float] = (0.0, 255.0)

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any], where: str) -> AlphaFrom:
        _refuse_keys(cfg, frozenset({"band", "domain", "range"}), where)
        if "band" not in cfg or not isinstance(cfg["band"], str):
            raise ValueError(f"{where}: alpha_from needs band: <delivered band>")
        if "domain" not in cfg:
            raise ValueError(f"{where}: alpha_from needs domain: [lo, hi]")
        rng = _pair(cfg.get("range", [0, 255]), f"{where}.range")
        if not all(0 <= v <= 255 for v in rng):
            raise ValueError(f"{where}.range: alpha values must lie in 0..255")
        return cls(cfg["band"], _pair(cfg["domain"], f"{where}.domain"), rng)

    def alpha(self, bands: BandStack) -> np.ndarray:
        """(H, W) uint8. Non-finite values (nodata) give alpha 0."""
        arr = np.asarray(bands[self.band], dtype=np.float64)
        if arr.ndim != 2:
            raise ValueError(f"alpha_from band {self.band!r} must be single-band, got {arr.shape}")
        lo, hi = self.domain
        a0, a1 = self.range
        if hi < lo:                       # np.interp needs increasing xp
            lo, hi, a0, a1 = hi, lo, a1, a0
        finite = np.isfinite(arr)
        a = np.interp(np.where(finite, arr, lo), [lo, hi], [a0, a1])
        a[~finite] = 0
        return np.clip(np.rint(a), 0, 255).astype(np.uint8)


class CategoricalMapper:
    """Class -> colour through the layer's class table (07 section 3). Nodata is transparent;
    `none` is transparent unless `colors` names it; `on_unmapped` governs classes `colors` omits.
    """

    def __init__(self, layer: str, class_table: ClassTable, *, name: str | None = None,
                 colors: Mapping[str, Sequence[int]] | None = None, on_unmapped: str = "fail",
                 alpha_from: AlphaFrom | None = None, nodata: int = CATEGORICAL_NODATA) -> None:
        if on_unmapped not in ON_UNMAPPED:
            raise ValueError(f"on_unmapped {on_unmapped!r} is not one of {ON_UNMAPPED}")
        self.layer = layer
        self.name = name or layer
        self.class_table = class_table
        self.colors = dict(colors) if colors is not None else None
        self.on_unmapped = on_unmapped
        self.alpha_from = alpha_from
        self.nodata = nodata
        self.outputs = (ImageSpec(f"{self.name}_rgba"),)
        # Validate colours against the table now, so a bad name fails at plan time.
        resolve_class_colors(class_table, self.colors, on_unmapped=on_unmapped)

    @classmethod
    def from_config(cls, name: str, cfg: Mapping[str, Any], class_table: ClassTable,
                    layer: str) -> CategoricalMapper:
        where = f"outputs.render.{name}"
        _refuse_keys(cfg, _CAT_KEYS, where)
        if cfg.get("nodata", "transparent") != "transparent":
            raise ValueError(f"{where}: nodata must be `transparent` (07 section 3)")
        alpha = cfg.get("alpha_from")
        return cls(layer, class_table, name=name, colors=cfg.get("colors"),
                   on_unmapped=cfg.get("on_unmapped", "fail"),
                   alpha_from=AlphaFrom.from_config(alpha, f"{where}.alpha_from")
                   if alpha is not None else None)

    def render(self, bands: BandStack, aux: Any = None) -> np.ndarray:
        arr = np.asarray(bands[self.layer])
        if arr.ndim != 2:
            raise ValueError(f"categorical layer {self.layer!r} must be (H, W), got {arr.shape}")
        valid = arr != self.nodata
        present = np.unique(arr[valid])
        colours = resolve_class_colors(self.class_table, self.colors, on_unmapped=self.on_unmapped,
                                       present=present.tolist())
        lut = color_lut(colours)
        rgba = np.zeros((*arr.shape, 4), dtype=np.uint8)
        rgba[valid] = lut[arr[valid].astype(np.int64)]
        if self.alpha_from is not None:
            a = self.alpha_from.alpha(bands)
            rgba[..., 3] = np.where(rgba[..., 3] > 0, a, 0)
        return rgba

    def legend(self) -> Legend:
        return categorical_legend(self.class_table, self.colors, on_unmapped=self.on_unmapped)


def categorical_legend(table: ClassTable, colors: Mapping[str, Sequence[int]] | None = None, *,
                       on_unmapped: str = "fail") -> Legend:
    """Every class of the table with its resolved colour; `color` is None for a class left
    uncoloured under `on_unmapped: fail`, and alpha is not part of a legend entry."""
    colours = resolve_class_colors(table, colors, on_unmapped=on_unmapped)
    ids, names = table_ids_names(table)
    entries = [{"id": cid, "name": name,
                "color": None if colours[cid] is None else [int(c) for c in colours[cid][:3]]}
               for cid, name in zip(ids, names, strict=True)]
    return Legend(kind="categorical", entries=entries)


class ContinuousMapper:
    """Value -> ramp over `domain` (07 section 3). `clip` maps out-of-domain values to the ramp
    ends; otherwise they are transparent like nodata. A multi-band layer needs `band`."""

    def __init__(self, layer: str, *, name: str | None = None, ramp: str = "viridis",
                 domain: tuple[float, float], clip: bool = False, band: int | None = None,
                 alpha_from: AlphaFrom | None = None) -> None:
        ramp_table(ramp)                          # validates the name
        lo, hi = domain
        if not (np.isfinite(lo) and np.isfinite(hi)) or hi <= lo:
            raise ValueError(f"domain must be finite [lo, hi] with hi > lo, got {domain}")
        self.layer = layer
        self.name = name or layer
        self.ramp = ramp
        self.domain = (float(lo), float(hi))
        self.clip = bool(clip)
        self.band = band
        self.alpha_from = alpha_from
        self.outputs = (ImageSpec(f"{self.name}_rgba"),)

    @classmethod
    def from_config(cls, name: str, cfg: Mapping[str, Any], layer: str) -> ContinuousMapper:
        where = f"outputs.render.{name}"
        _refuse_keys(cfg, _CONT_KEYS, where)
        if "domain" not in cfg:
            raise ValueError(f"{where}: continuous needs domain: [lo, hi] (07 section 3)")
        band = cfg.get("band")
        if band is not None and (isinstance(band, bool) or not isinstance(band, int) or band < 0):
            raise ValueError(f"{where}: band must be a non-negative integer index")
        alpha = cfg.get("alpha_from")
        return cls(layer, name=name, ramp=cfg.get("ramp", "viridis"),
                   domain=_pair(cfg["domain"], f"{where}.domain"), clip=cfg.get("clip", False),
                   band=band, alpha_from=AlphaFrom.from_config(alpha, f"{where}.alpha_from")
                   if alpha is not None else None)

    def render(self, bands: BandStack, aux: Any = None) -> np.ndarray:
        arr = np.asarray(bands[self.layer], dtype=np.float64)
        if arr.ndim == 3:
            if self.band is None:
                raise ValueError(f"layer {self.layer!r} has {arr.shape[-1]} bands; the render "
                                 "needs `band: <index>` to pick one (07 section 3)")
            if self.band >= arr.shape[-1]:
                raise ValueError(f"layer {self.layer!r}: band {self.band} out of range")
            arr = arr[..., self.band]
        if arr.ndim != 2:
            raise ValueError(f"layer {self.layer!r} must be (H, W) or (H, W, B), got {arr.shape}")
        lo, hi = self.domain
        finite = np.isfinite(arr)
        t = (arr - lo) / (hi - lo)
        # A float32 value equal to a domain end is not exactly the float64 end (0.15f32 is
        # 0.150000006); a relative 1e-6 tolerance keeps the ends inside the domain.
        eps = 1e-6
        shown = finite if self.clip else finite & (t >= -eps) & (t <= 1.0 + eps)
        rgba = np.zeros((*arr.shape, 4), dtype=np.uint8)
        rgba[..., :3] = apply_ramp(t, self.ramp)
        rgba[..., 3] = np.where(shown, 255, 0)
        rgba[~shown] = 0
        if self.alpha_from is not None:
            a = self.alpha_from.alpha(bands)
            rgba[..., 3] = np.where(shown, a, 0)
        return rgba

    def legend(self) -> Legend:
        return Legend(kind="continuous", entries=ramp_stops(self.ramp, self.domain),
                      note=f"ramp {self.ramp}; domain [{self.domain[0]}, {self.domain[1]}]; "
                           f"clip {'on' if self.clip else 'off'}")


ConfigMapper = CategoricalMapper | ContinuousMapper


def build_mapper(name: str, cfg: Mapping[str, Any], schema: SnapshotSchema) -> ConfigMapper:
    """One render entry -> a mapper, validated against the schema (plan time, 07 section 3)."""
    where = f"outputs.render.{name}"
    if not isinstance(cfg, Mapping):
        raise TypeError(f"{where}: expected a mapping")
    kind = cfg.get("mapper")
    if kind in LATER_MAPPERS:
        raise NotImplementedError(f"{where}: mapper {kind!r} is not in the first slice "
                                  "(07 section 3)")
    if kind is None and "ref" in cfg:
        raise NotImplementedError(f"{where}: OutputMapper plugins (`ref`) are not in the first "
                                  "slice (07 section 4)")
    if kind not in CONFIG_MAPPERS:
        raise ValueError(f"{where}: mapper must be one of {CONFIG_MAPPERS}, got {kind!r}")
    layer = cfg.get("layer", name)
    try:
        spec = schema[layer]
    except KeyError:
        raise ValueError(f"{where}: layer {layer!r} is not in schema {schema.name!r}") from None
    if kind == "categorical":
        if spec.kind != "categorical":
            raise ValueError(f"{where}: categorical mapper on {spec.kind} layer {layer!r}")
        if spec.classes is None:
            raise ValueError(f"{where}: layer {layer!r} has no class table to colour by")
        return CategoricalMapper.from_config(name, cfg, spec.classes, layer)
    if spec.kind != "continuous":
        raise ValueError(f"{where}: continuous mapper on {spec.kind} layer {layer!r}")
    return ContinuousMapper.from_config(name, cfg, layer)


def build_mappers(manifest_outputs: Mapping[str, Any],
                  schema: SnapshotSchema) -> dict[str, ConfigMapper]:
    """Every entry of `outputs.render`, keyed by output name, in manifest order."""
    render_cfg = manifest_outputs.get("render") or {}
    if not isinstance(render_cfg, Mapping):
        raise TypeError("outputs.render must be a mapping of output name -> render config")
    return {name: build_mapper(name, cfg, schema) for name, cfg in render_cfg.items()}


def render(manifest_outputs: Mapping[str, Any], stack: BandStack,
           schema: SnapshotSchema) -> dict[str, np.ndarray]:
    """Output name -> (H, W, 4) uint8 for every configured render (07 section 3)."""
    return {name: m.render(stack) for name, m in build_mappers(manifest_outputs, schema).items()}


def write_image(path: Path, rgba: np.ndarray, tile: TileRef, *,
                tags: Mapping[str, Any]) -> Path:
    """A 4-band uint8 RGBA GeoTIFF on the tile's grid, alpha declared as alpha."""
    rgba = np.asarray(rgba)
    if rgba.dtype != np.uint8 or rgba.ndim != 3 or rgba.shape[-1] != 4:
        raise ValueError(f"an image is (H, W, 4) uint8, got {rgba.dtype} {rgba.shape}")
    if rgba.shape[:2] != tile.shape:
        raise ValueError(f"image shape {rgba.shape[:2]} != tile shape {tile.shape}")
    rows, cols = tile.shape
    bs = 256 if min(rows, cols) >= 256 else 16
    profile = {"driver": "GTiff", "height": rows, "width": cols, "count": 4, "dtype": "uint8",
               "crs": tile.grid.crs, "transform": tile.transform, "tiled": True,
               "blockxsize": bs, "blockysize": bs, "compress": "deflate",
               "photometric": "RGB", "alpha": "YES"}      # ALPHA=YES marks band 4 as alpha
    path = Path(path)
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(np.moveaxis(rgba, -1, 0))
        dst.colorinterp = [ColorInterp.red, ColorInterp.green, ColorInterp.blue, ColorInterp.alpha]
        dst.update_tags(**{k: str(v) for k, v in tags.items()})
    return path


def write_images(out_dir: Path, images: Mapping[str, np.ndarray], tile: TileRef, *,
                 tags: Mapping[str, Any]) -> dict[str, Path]:
    """`{name}_rgba.tif` per rendered output. Returns name -> path."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    return {name: write_image(out_dir / f"{name}_rgba.tif", rgba, tile, tags=tags)
            for name, rgba in images.items()}
