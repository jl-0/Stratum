"""The ortho-native GeoTIFF reader (12 section 2, 3).

EMIT's L2B FRCOV is delivered as per-fraction GeoTIFFs on the granule's own ortho grid, so it has
no sensor space to window and no `loc` array to KD-tree. A reader declares which space it
produces and the framework warps an ortho-native role onto the block grid directly.

What that means for this file: `read()` is never called. An ortho reader's job is to say WHERE
the pixels are and which band - `ortho_source()` - and let the framework do the geometry, the
same split `AuxAccessor` makes from the other side. A reader that warped for itself would be a
reader that had to know what a tile is.

[observed] 2026-09-15, `EMIT_L2B_FRCOVBARE_001_20260124T212614_2602413_003.tif`: single band,
float64, EPSG:4326, cell 0.000542232520256367 deg, nodata -9999, internally tiled 512 with
overviews, band description `EMIT_L2B_FRCOVBARE`. The cell size is the EMIT ortho lattice
exactly, so FRCOV shares the one lattice with OBS and MIN - but against a one-arcsecond run grid
that is a ratio of 1.952, not an integer, so it is a genuine resample and not an `adopt`-style
crop.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, Self

import numpy as np
import rasterio

from stratum.ancillary import OrthoSource
from stratum.types import AssetHandle, ClassTable, LocArray, SensorWindow, VarSpec


@dataclass
class GeoTIFFContext:
    """What the reader keeps between open() and the rest: the handle and the open dataset.
    rasterio opens headers only; a window is decoded on demand."""

    asset: AssetHandle
    dataset: Any

    def close(self) -> None:
        if not self.dataset.closed:
            self.dataset.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class GeoTIFFReader:
    """Any single- or multi-band georeferenced raster, read as an ortho-native role.

    Variables are BANDS, named by the band's description where the file carries one and
    `band_{n}` where it does not - so a manifest says `var: EMIT_L2B_FRCOVBARE` and not `var: 1`.
    Naming beats positional indexing for the same reason 11 section 9 gives for class tables: a
    position silently means something else in the next delivery, while a name either matches or
    fails loudly at plan time.
    """

    collections: tuple[str, ...] = ()
    space: Literal["sensor", "ortho"] = "ortho"
    band_names_var: str | None = None

    # ---- open -----------------------------------------------------------------------------
    def open(self, asset: AssetHandle) -> GeoTIFFContext:
        """Cheap: the header only (12 section 3 rule 3). `vsi()` so a staged local copy and a
        future /vsi stream open the same way."""
        return GeoTIFFContext(asset=asset, dataset=rasterio.open(asset.vsi()))

    # ---- variables ------------------------------------------------------------------------
    def band_names(self, ctx: GeoTIFFContext) -> list[str]:
        ds = ctx.dataset
        return [str(d) if d else f"band_{i}"
                for i, d in enumerate(ds.descriptions, start=1)]

    def variables(self, ctx: GeoTIFFContext) -> dict[str, VarSpec]:
        ds = ctx.dataset
        out: dict[str, VarSpec] = {}
        for i, name in enumerate(self.band_names(ctx), start=1):
            out[name] = VarSpec(
                name=name,
                dtype=str(ds.dtypes[i - 1]),
                shape=(int(ds.height), int(ds.width)),
                fill=ds.nodatavals[i - 1],
                units=str(ds.units[i - 1] or "unitless"),
                band_attrs={},
            )
        return out

    def band_index(self, ctx: GeoTIFFContext, var: str) -> int:
        names = self.band_names(ctx)
        if var not in names:
            raise KeyError(f"{ctx.asset.uri}: no band named {var!r}; it has {names}")
        return names.index(var) + 1

    # ---- the ortho seam -------------------------------------------------------------------
    def ortho_source(self, ctx: GeoTIFFContext, var: str) -> OrthoSource:
        """Where this role's pixels are, for the framework to warp (12 section 2).

        Deliberately not the pixels: handing back a URI and a band lets GDAL do a windowed,
        overview-aware read out of the COG, so a tile costs the bytes it covers rather than the
        whole 2484 x 3376 float64 scene.
        """
        i = self.band_index(ctx, var)
        ds = ctx.dataset
        if ds.crs is None:
            raise ValueError(f"{ctx.asset.uri} has no CRS, so it cannot be warped onto the grid "
                             "(12 section 2)")
        return OrthoSource(uri=ctx.asset.vsi(), band=i, nodata=ds.nodatavals[i - 1],
                           dtype=str(ds.dtypes[i - 1]))

    # ---- the sensor-space members an ortho reader does not have ---------------------------
    def read(self, ctx: GeoTIFFContext, var: str,
             window: SensorWindow | None = None) -> np.ma.MaskedArray:
        raise NotImplementedError(
            f"{type(self).__name__} is ortho-native (space='ortho'): it has no sensor space to "
            "window. The framework warps it onto the block grid through ortho_source() "
            "(12 section 2); read() is the sensor path and is never called for this role.")

    def geolocation(self, ctx: GeoTIFFContext) -> LocArray | None:
        return None                    # already on a map grid; nothing to KD-tree

    def glt(self, ctx: GeoTIFFContext) -> None:
        return None

    def class_table(self, ctx: GeoTIFFContext, path: str, key: str,
                    attributes: Sequence[str]) -> ClassTable | None:
        return None                    # a GeoTIFF embeds no table this framework reads


class L2BFrcovTiff(GeoTIFFReader):
    """EMITL2BFRCOV: the delivered per-fraction GeoTIFFs (`FRCOVBARE`/`PV`/`NPV`, each with an
    `UNC` twin, plus `FRCOVQC`). A role names one asset and that file's one band, e.g.
    `{collection: EMITL2BFRCOV, asset: FRCOVBARE, var: EMIT_L2B_FRCOVBARE}`.

    Values are fractions but are not clamped to [0, 1]: the unmixing leaves small negative
    residuals (-0.10 observed), which are real and are the scorer's to handle, not the reader's.
    11 section 2's rule is that a reader resolves the FILL and changes nothing else.
    """

    collections = ("EMITL2BFRCOV",)


__all__ = ["GeoTIFFContext", "GeoTIFFReader", "L2BFrcovTiff"]
