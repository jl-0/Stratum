"""Turning a published raster into a 256-pixel web-mercator tile.

Only the local preview server uses this. A viewer deployed beside the products reads the COGs
itself, by HTTP range, and never calls any of it - which is why nothing here may become the only
place a rendering rule is written down. The rules are in the legend and the class table that
publish already wrote (07 section 7); this module reads them, it does not invent them.

Two things are deliberate:

* **The PNG is encoded here, not by GDAL.** GDAL's PNG driver is create-copy only, so going
  through it means writing a GeoTIFF into memory and rewriting it. A 256x256 RGBA buffer is
  three lines of `zlib` and a CRC, with no dependency and no driver quirk.
* **Categorical rasters resample nearest, always.** Averaging class ids invents classes that were
  never identified - the same conflation `-9999` vs `0` is (11 section 2). Continuous rasters
  take an average when zoomed out, which is what makes a one-degree tile readable at z6.
"""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np

#: Half the circumference of the sphere web mercator wraps, in metres. The extent of z0.
MERCATOR_R = 20037508.342789244

TILE = 256


# ------------------------------------------------------------------------------------ the PNG
def encode_png(rgba: np.ndarray) -> bytes:
    """An 8-bit RGBA PNG. `rgba` is `(h, w, 4)` uint8."""
    h, w, _ = rgba.shape
    raw = np.hstack([np.zeros((h, 1), np.uint8), rgba.reshape(h, w * 4)])  # filter byte 0 a row

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw.tobytes(), 6))
            + chunk(b"IEND", b""))


#: A fully transparent tile, encoded once. Most tiles over a tiled AOI are this one.
@lru_cache(maxsize=1)
def blank_tile() -> bytes:
    return encode_png(np.zeros((TILE, TILE, 4), np.uint8))


# --------------------------------------------------------------------------------- the ramps
#: Control points sampled from matplotlib's `viridis` and `magma`. Held as nine stops and
#: interpolated rather than as 256 rows, because the table is read by a human here.
RAMPS: dict[str, tuple[tuple[int, int, int], ...]] = {
    "viridis": ((68, 1, 84), (72, 40, 120), (62, 74, 137), (49, 104, 142), (38, 130, 142),
                (31, 158, 137), (53, 183, 121), (109, 205, 89), (253, 231, 37)),
    "magma": ((0, 0, 4), (28, 16, 68), (79, 18, 123), (129, 37, 129), (181, 54, 122),
              (229, 80, 100), (251, 135, 97), (254, 194, 135), (252, 253, 191)),
    "gray": ((0, 0, 0), (32, 32, 32), (64, 64, 64), (96, 96, 96), (128, 128, 128),
             (160, 160, 160), (192, 192, 192), (224, 224, 224), (255, 255, 255)),
}


@lru_cache(maxsize=8)
def ramp_lut(name: str) -> np.ndarray:
    """A 256x3 uint8 lookup table for a named ramp."""
    stops = np.array(RAMPS.get(name, RAMPS["viridis"]), dtype=float)
    x = np.linspace(0, 255, len(stops))
    return np.stack([np.interp(np.arange(256), x, stops[:, i]) for i in range(3)],
                    axis=1).round().astype(np.uint8)


# --------------------------------------------------------------------------------- the styles
@dataclass(frozen=True)
class Style:
    """How one asset becomes pixels.

    `kind` is `rgba` (pass the four bands through), `categorical` (look each value up in
    `colors`) or `continuous` (stretch between `vmin` and `vmax` through `ramp`).
    """

    kind: str = "continuous"
    colors: tuple[tuple[int, tuple[int, int, int]], ...] = ()
    vmin: float = 0.0
    vmax: float = 1.0
    ramp: str = "viridis"
    opaque_zero: bool = False       # categorical: draw class 0 ("none") instead of dropping it

    @property
    def nearest(self) -> bool:
        return self.kind != "continuous"

    def lut(self) -> tuple[np.ndarray, np.ndarray]:
        """A categorical style as `(rgb[n], drawn[n])` indexed by class id.

        Ids above the table are transparent, so a raster holding a class the legend does not
        describe draws as a hole rather than as a wrong colour.
        """
        top = max((i for i, _ in self.colors), default=0)
        rgb = np.zeros((top + 1, 3), np.uint8)
        drawn = np.zeros(top + 1, bool)
        for i, colour in self.colors:
            rgb[i] = colour
            drawn[i] = True
        if not self.opaque_zero and len(drawn):
            drawn[0] = False        # `none` is the reserved id 0 (13 section 3): observed, empty
        return rgb, drawn


def style_for(asset_key: str, roles: list[str], legend: dict | None,
              *, ramp: str = "viridis") -> Style:
    """The default style for an asset, from its STAC roles and its legend if it has one."""
    if "visual" in roles:
        return Style(kind="rgba")
    if legend and legend.get("kind") == "categorical":
        colors = tuple((int(e["id"]), tuple(int(c) for c in e["color"]))  # type: ignore[misc]
                       for e in legend.get("entries", ()))
        return Style(kind="categorical", colors=colors)
    # An agreement layer is a fraction by construction (07 section 7); everything else gets its
    # range measured off the raster.
    if asset_key.endswith("_agreement"):
        return Style(kind="continuous", vmin=0.0, vmax=1.0, ramp=ramp)
    return Style(kind="continuous", ramp=ramp)


# ----------------------------------------------------------------------------------- the tile
def tile_bounds(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """A slippy-map tile's extent in EPSG:3857, as `(west, south, east, north)`."""
    span = 2 * MERCATOR_R / (1 << z)
    west = -MERCATOR_R + x * span
    north = MERCATOR_R - y * span
    return west, north - span, west + span, north


def measure(path: Path, *, band: int = 1, size: int = 512) -> tuple[float, float]:
    """The 2nd and 98th percentile of a raster, from a decimated read.

    A stretch has to come from somewhere, and reading the whole band per tile is not it. The
    percentiles clip the outliers that otherwise flatten every continuous layer to one colour.
    """
    import rasterio

    with rasterio.open(path) as src:
        shape = (min(size, src.height), min(size, src.width))
        data = src.read(band, out_shape=shape, masked=True).astype("float64")
    valid = data.compressed()
    valid = valid[np.isfinite(valid)]
    if valid.size == 0:
        return 0.0, 1.0
    lo, hi = (float(v) for v in np.percentile(valid, (2, 98)))
    return (lo, hi) if hi > lo else (lo, lo + 1.0)


def sample(path: Path, lon: float, lat: float) -> list[float] | None:
    """Every band of one raster at one point, or `None` where the point is outside it or masked.

    The point arrives in EPSG:4326 because that is what a map click is. Products are written in
    4326 too ([01](../../docs/specs/01-grid-tiling.md)), but the transform is done anyway rather
    than assumed - a reader that silently mis-samples a differently-projected product would be
    worse than one that is slightly slower.
    """
    import rasterio
    from rasterio.warp import transform as warp_points

    with rasterio.open(path) as src:
        x, y = (lon, lat)
        if src.crs and src.crs.to_epsg() != 4326:
            (x,), (y,) = warp_points("EPSG:4326", src.crs, [lon], [lat])
        row, col = src.index(x, y)
        if not (0 <= row < src.height and 0 <= col < src.width):
            return None
        window = ((row, row + 1), (col, col + 1))
        values = src.read(masked=True, window=window)

    if np.ma.getmaskarray(values).all():
        return None
    return [None if m else float(v)                              # type: ignore[misc]
            for v, m in zip(np.ma.getdata(values).ravel(),
                            np.ma.getmaskarray(values).ravel(), strict=True)]


def render_tile(path: Path, z: int, x: int, y: int, style: Style) -> bytes:
    """One raster, one tile, as PNG bytes. Transparent where the tile misses the raster.

    The warped VRT *is* the tile - its CRS, transform and shape are the tile's - so GDAL does the
    reprojection and the decimation in one pass and the read needs no window. That is also what
    fills the part of a tile that hangs off the raster: outside the source there is nodata, which
    comes back masked and becomes alpha 0.
    """
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.transform import from_bounds
    from rasterio.vrt import WarpedVRT

    bounds = tile_bounds(z, x, y)
    resampling = Resampling.nearest if style.nearest else Resampling.average

    with rasterio.open(path) as src:
        vrt_options: dict[str, object] = {
            "crs": "EPSG:3857",
            "transform": from_bounds(*bounds, TILE, TILE),
            "width": TILE, "height": TILE,
            "resampling": resampling,
        }
        # How "off the edge" becomes transparent. A raster that declares nodata says so itself;
        # one that does not (an RGBA rendering) already carries its own alpha band.
        if src.nodata is not None:
            vrt_options |= {"src_nodata": src.nodata, "nodata": src.nodata}
        elif src.count < 4:
            vrt_options["add_alpha"] = True
        with WarpedVRT(src, **vrt_options) as vrt:                 # type: ignore[arg-type]
            if _disjoint(bounds, _source_bounds(src)):
                return blank_tile()
            if style.kind == "rgba":
                return encode_png(_as_rgba(vrt.read(indexes=[1, 2, 3, 4][:vrt.count],
                                                    masked=True)))
            band = vrt.read(1, masked=True)

    return encode_png(_paint_categorical(band, style) if style.kind == "categorical"
                      else _paint_continuous(band, style))


def _source_bounds(src) -> tuple[float, float, float, float]:
    """The dataset's extent in EPSG:3857, so a tile that misses it costs no warp."""
    from rasterio.warp import transform_bounds

    return transform_bounds(src.crs, "EPSG:3857", *src.bounds)


def _disjoint(a: tuple[float, ...], b: tuple[float, ...]) -> bool:
    return a[0] >= b[2] or a[2] <= b[0] or a[1] >= b[3] or a[3] <= b[1]


def _as_rgba(bands: np.ndarray) -> np.ndarray:
    """A 3- or 4-band uint8 read as RGBA, honouring the mask as alpha."""
    data = np.ma.filled(bands, 0).astype(np.uint8)
    rgba = np.zeros((TILE, TILE, 4), np.uint8)
    rgba[..., :3] = np.moveaxis(data[:3], 0, -1)
    if data.shape[0] >= 4:
        rgba[..., 3] = data[3]
    else:
        rgba[..., 3] = np.where(np.ma.getmaskarray(bands).any(axis=0), 0, 255)
    return rgba


def _paint_categorical(band: np.ma.MaskedArray, style: Style) -> np.ndarray:
    rgb, drawn = style.lut()
    values = np.ma.filled(band, 0).astype("int64")
    inside = (values >= 0) & (values < len(drawn))
    idx = np.where(inside, values, 0)
    rgba = np.zeros((TILE, TILE, 4), np.uint8)
    rgba[..., :3] = rgb[idx]
    rgba[..., 3] = np.where(inside & drawn[idx] & ~np.ma.getmaskarray(band), 255, 0)
    return rgba


def _paint_continuous(band: np.ma.MaskedArray, style: Style) -> np.ndarray:
    # Cast before filling: an integer band cannot hold NaN, and `n_epochs` is uint16.
    data = np.ma.getdata(band).astype("float64")
    good = np.isfinite(data) & ~np.ma.getmaskarray(band)
    span = style.vmax - style.vmin or 1.0
    scaled = np.clip((np.nan_to_num(data) - style.vmin) / span, 0, 1)
    rgba = np.zeros((TILE, TILE, 4), np.uint8)
    rgba[..., :3] = ramp_lut(style.ramp)[(scaled * 255).astype(np.uint8)]
    rgba[..., 3] = np.where(good, 255, 0)
    return rgba


__all__ = ["MERCATOR_R", "RAMPS", "TILE", "Style", "blank_tile", "encode_png", "measure",
           "ramp_lut", "render_tile", "sample", "style_for", "tile_bounds"]
