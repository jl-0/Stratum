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

    def __init__(self, min_soil: float = 0.65) -> None:
        self.min_soil = min_soil

    def valid(self, obs: ObsWindow, aux: AuxAccessor) -> np.ndarray:
        return obs["frcov"] >= self.min_soil


__all__ = ["EdgeTrim", "L2AStandard", "SlitDust", "SoilFraction"]
