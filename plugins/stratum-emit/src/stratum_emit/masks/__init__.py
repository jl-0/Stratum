"""Built-in EMIT masks (04 section 3). Column counts are Phil's numbers pending confirmation.

Sensor-space masks see the sensor window WITH its origin and the full sensor shape
(`obs.sensor_window`, `obs.sensor_shape`; 12 section 2), so a detector edge is the detector's
edge and not the window's. Map-space masks see the gathered block.
"""
from __future__ import annotations

from collections.abc import Mapping

import numpy as np

from stratum.types import AuxAccessor, ObsWindow


class EdgeTrim:
    """Drop the outer columns each side. Every column is a different detector, so the trim is
    cross-track only; along-track granule edges are a download artifact and are kept."""

    space = "sensor"
    required_roles: tuple[str, ...] = ()
    required_aux: tuple[str, ...] = ()

    def __init__(self, columns: int = 7) -> None:
        if columns < 5:
            raise ValueError("EdgeTrim: 5 columns is the minimum")
        self.columns = columns

    def valid(self, obs: ObsWindow, aux: AuxAccessor) -> np.ndarray:
        sw = obs.sensor_window
        if sw is None:
            raise ValueError("EdgeTrim is a sensor-space mask and needs the window origin")
        if obs.sensor_shape is None:
            raise ValueError("EdgeTrim needs obs.sensor_shape - the full (downtrack, crosstrack) "
                             "of the variable - to find the detector edges (12 section 2)")
        ncols = int(obs.sensor_shape[1])
        h, w = sw.height, sw.width
        col = sw.col0 + np.arange(w)[None, :]
        ok = (col >= self.columns) & (col < ncols - self.columns)
        return np.broadcast_to(ok, (h, w)).copy()


class SlitDust:
    """Drop 2-3 columns at cross-track centre. Off by default."""

    space = "sensor"
    required_roles: tuple[str, ...] = ()
    required_aux: tuple[str, ...] = ()

    def __init__(self, columns: int = 3) -> None:
        self.columns = columns

    def valid(self, obs: ObsWindow, aux: AuxAccessor) -> np.ndarray:
        raise NotImplementedError("SlitDust is not in the first slice (04 section 3): the "
                                  "affected columns are not confirmed")


class L2AStandard:
    """Cloud, cirrus, water, spacecraft flags from the L2A mask product - the `mask` role, a
    multi-band variable whose band names the reader reports (`obs.band_attrs["mask"]["name"]`,
    e.g. "Cloud flag", "Cirrus flag", "Water flag", "Spacecraft Flag", "Dilated Cloud Flag").
    A cell is usable when every selected flag is 0."""

    space = "map"
    required_roles = ("mask",)
    required_aux: tuple[str, ...] = ()

    #: flag keyword -> the band name the product carries; matched case-insensitively
    BANDS: Mapping[str, str] = {
        "cloud": "Cloud flag",
        "cirrus": "Cirrus flag",
        "water": "Water flag",
        "spacecraft": "Spacecraft Flag",
        "dilated_cloud": "Dilated Cloud Flag",
    }

    def __init__(self, flags: tuple[str, ...] = ("cloud", "cirrus", "water", "spacecraft")) -> None:
        unknown = [f for f in flags if f not in self.BANDS]
        if unknown:
            raise ValueError(f"L2AStandard: unknown flag(s) {unknown}; choose from "
                             f"{sorted(self.BANDS)}")
        self.flags = tuple(flags)

    def band_indices(self, names: list[str]) -> dict[str, int]:
        lowered = [str(n).strip().lower() for n in names]
        out: dict[str, int] = {}
        for flag in self.flags:
            wanted = self.BANDS[flag].lower()
            if wanted not in lowered:
                raise KeyError(f"L2AStandard: the mask role has no band named "
                               f"{self.BANDS[flag]!r} for flag {flag!r}; bands are {names}")
            out[flag] = lowered.index(wanted)
        return out

    def valid(self, obs: ObsWindow, aux: AuxAccessor) -> np.ndarray:
        mask = np.asarray(obs["mask"])
        if mask.ndim != 3:
            raise ValueError("L2AStandard: the mask role must be multi-band (H, W, bands)")
        names = list(obs.band_attrs.get("mask", {}).get("name", []))
        if not names:
            raise KeyError("L2AStandard: the reader reported no band names for the mask role "
                           "(VarSpec.band_attrs['name']); cannot select flags by name")
        ok = np.ones(mask.shape[:2], dtype=bool)
        for index in self.band_indices(names).values():
            ok &= ~(mask[..., index] > 0)
        return ok


class SoilFraction:
    space = "map"
    required_roles = ("frcov",)
    required_aux: tuple[str, ...] = ()

    def __init__(self, min_soil: float = 0.65) -> None:
        self.min_soil = min_soil

    def valid(self, obs: ObsWindow, aux: AuxAccessor) -> np.ndarray:
        return obs["frcov"] >= self.min_soil


class Landcover:
    """Drop cells whose land cover is not ground worth mapping, from a declared aux raster.

    The first mask that reads ancillary data rather than the granule. `classes` names what to
    EXCLUDE, by name rather than by integer, for the reason 11 section 9 gives for mineral
    classes: a raw value is positional and silently means something else in the next version,
    while a name either matches or fails loudly.

    Defaults to the three covers that cannot carry a useful surface mineral signal - open water,
    built-up, and closed tree canopy - and deliberately not to the vegetated classes, which are
    the scorer's business: "this pixel is unusable" and "this pixel is merely worse" are
    different statements and mixing them is the mistake 04 section 3 exists to prevent.

    **How a name becomes a pixel value.** `codes` maps class name to the integer the raster
    holds, and it defaults to ESA WorldCover's (10 m, v100/v200) - the map this ships against.
    Point `alias` at a different land-cover product (NLCD, Copernicus CGLS, MCD12Q1) and you
    MUST pass its codes, because nothing here can tell one uint8 raster from another: the aux
    accessor hands over a plain array, and an NLCD raster read with WorldCover codes would
    exclude the wrong classes silently.

    That is a real limitation and not a preference. A WorldCover GeoTIFF **publishes its own
    legend** in a TIFF tag - `10 Tree cover / ... / 80 Permanent water bodies / ...` - so the
    authority exists in the file, exactly as a mineral product's class table does. `AuxAccessor.
    raster()` returns an ndarray with no metadata, so a mask cannot reach it; reading and
    checking that legend needs framework surface that does not exist yet
    ([05 section 7](../../../../docs/specs/05-ancillary-data.md)).
    """

    space = "map"
    required_roles: tuple[str, ...] = ()
    required_aux = ("landcover",)

    #: ESA WorldCover class name -> code. 0 is the product's nodata and is never a class.
    CODES: Mapping[str, int] = {
        "tree": 10, "shrubland": 20, "grassland": 30, "cropland": 40, "built-up": 50,
        "bare": 60, "snow-ice": 70, "water": 80, "wetland": 90, "mangrove": 95,
        "moss-lichen": 100,
    }

    def __init__(self, alias: str = "landcover",
                 exclude: tuple[str, ...] = ("water", "built-up", "tree"),
                 on_missing: str = "keep",
                 codes: Mapping[str, int] | None = None) -> None:
        self.codes = dict(self.CODES if codes is None else codes)
        if not self.codes:
            raise ValueError("Landcover: codes is empty; name at least one class")
        bad = {k: v for k, v in self.codes.items() if not isinstance(v, int) or isinstance(v, bool)}
        if bad:
            raise ValueError(f"Landcover: code(s) must be integers; got {bad}")
        unknown = [c for c in exclude if c not in self.codes]
        if unknown:
            raise ValueError(f"Landcover: unknown class(es) {unknown}; choose from "
                             f"{sorted(self.codes)}")
        if on_missing not in ("keep", "reject"):
            raise ValueError("Landcover: on_missing is 'keep' or 'reject'")
        self.alias = alias
        self.exclude = tuple(exclude)
        self.on_missing = on_missing
        self.required_aux = (alias,)

    def valid(self, obs: ObsWindow, aux: AuxAccessor) -> np.ndarray:
        cover = np.asarray(aux.raster(self.alias))
        ok = np.ones(cover.shape, dtype=bool)
        for name in self.exclude:
            ok &= cover != self.codes[name]
        if self.on_missing == "reject":
            ok &= cover != 0        # 0 is the product's nodata, not a class
        return ok


__all__ = ["EdgeTrim", "L2AStandard", "Landcover", "SlitDust", "SoilFraction"]
