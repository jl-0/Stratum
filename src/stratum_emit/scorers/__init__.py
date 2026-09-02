"""The worked examples from spec 04 section 4, as shipped plugins."""
from __future__ import annotations

import numpy as np

from stratum.types import AuxAccessor, ObsWindow


class MinViewZenith:
    """V002 parity: the most nadir look wins."""

    capability = "streaming"
    halo = 0
    required_roles = ("geometry",)
    required_aux: tuple[str, ...] = ()

    def score(self, obs: ObsWindow, aux: AuxAccessor) -> np.ndarray:
        return -obs["view_zenith"]


class CleanestNadir:
    """Nadir-preferring, penalised for steep terrain, rejecting snow."""

    capability = "streaming"
    halo = 0
    required_roles = ("geometry",)
    required_aux = ("slope", "snow")

    def __init__(self, slope_penalty: float = 0.5, slope_limit: float = 30.0) -> None:
        self.slope_penalty = slope_penalty
        self.slope_limit = slope_limit

    def score(self, obs: ObsWindow, aux: AuxAccessor) -> np.ndarray:
        s = -obs["view_zenith"] / 90.0
        s = s - self.slope_penalty * (aux.raster("slope") > self.slope_limit)
        s[aux.raster("snow", date=obs.granule.datetime) > 0] = np.nan
        return s


class PreferBareEarth:
    """Prefer the observation that actually sees ground (Mines tag-up, 2026-08-28)."""

    capability = "streaming"
    halo = 0
    required_roles = ("frcov",)
    required_aux: tuple[str, ...] = ()

    def __init__(self, min_soil: float = 0.80, hard_floor: float = 0.65) -> None:
        self.min_soil, self.hard_floor = min_soil, hard_floor

    def score(self, obs: ObsWindow, aux: AuxAccessor) -> np.ndarray:
        soil = obs["frcov"]
        s = soil.astype("float32", copy=True)
        s[soil < self.hard_floor] = np.nan
        return s


class MaxBandDepth:
    """The strongest absorption feature wins.

    Ranks on the L2B band-depth role alone, so it runs over granules that have no L1B OBS
    companion - the case for a directory of downloaded L2B files. Not nadir-preferring: the
    same ground seen off-nadir with a deeper feature beats a nadir look, which is the wrong
    answer for a base map and the right one only when "strongest expression" is the product.
    """

    capability = "streaming"
    halo = 0
    required_roles = ("mineral_depth",)
    required_aux: tuple[str, ...] = ()

    def score(self, obs: ObsWindow, aux: AuxAccessor) -> np.ndarray:
        return obs["mineral_depth"].astype("float32", copy=False)
