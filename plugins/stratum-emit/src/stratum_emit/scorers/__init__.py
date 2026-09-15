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
    """Prefer the observation that actually sees ground, under usable illumination.

    Given several observations of a pixel, most of them vegetation, take the one that is not. Soil
    fraction comes from the L2B FRCOV product as an ortho-native role - already on a map grid,
    so it costs a warp and no KD-tree (12 section 2).

    Two terms, because bare ground seen at a low sun is not the same as bare ground seen well:

    **Cover.** Ranked on bare-soil fraction, with two thresholds rather than one. `hard_floor`
    (0.65) is V002's cutoff, chosen because grain-size retrieval "completely falls apart" below
    it - under it the observation may not occupy the cell at all, which is NaN, not a low score.
    `min_soil` (0.80) is the higher bar recommended for mosaicking. Keeping them separate is
    what lets the range between them still RANK rather than being discarded: 0.79 soil is worse
    than 0.85 and better than nothing, and collapsing that to a boolean destroys information the
    reduction could have used.

    **Illumination.** Solar zenith enters as cos(sun), normalised so straight overhead scores 1
    and the horizon 0. It is weighted below cover deliberately: a well-lit look at vegetation
    still does not see the rock, so cover decides and illumination breaks ties among comparable
    looks. `sun_weight` is the knob; 0 reproduces the pure cover ranking.

    Caveat to carry: the current FRCOV is NPV-false-positive-prone - it reads some bare soil as
    non-photosynthetic vegetation - so this scorer is conservative in a way that will improve
    when FRCOV does. Values are also not clamped to [0, 1]: small negative unmixing residuals
    are real, and clipping them here is the scorer's call, not the reader's (11 section 2).
    """

    capability = "streaming"
    halo = 0
    required_roles = ("frcov", "solar_zenith")
    required_aux: tuple[str, ...] = ()

    def __init__(self, min_soil: float = 0.80, hard_floor: float = 0.65,
                 sun_weight: float = 0.25, max_solar_zenith: float = 80.0) -> None:
        if not 0.0 <= hard_floor <= min_soil <= 1.0:
            raise ValueError("PreferBareEarth needs 0 <= hard_floor <= min_soil <= 1; got "
                             f"hard_floor={hard_floor}, min_soil={min_soil}")
        if sun_weight < 0:
            raise ValueError("PreferBareEarth: sun_weight must not be negative")
        self.min_soil = min_soil
        self.hard_floor = hard_floor
        self.sun_weight = sun_weight
        self.max_solar_zenith = max_solar_zenith

    def score(self, obs: ObsWindow, aux: AuxAccessor) -> np.ndarray:
        soil = np.clip(np.ma.filled(obs["frcov"], np.nan).astype("float32"), 0.0, 1.0)
        sun = np.ma.filled(obs["solar_zenith"], np.nan).astype("float32")
        # cos of the solar zenith: 1 overhead, 0 at the horizon. Angles are degrees (L1B OBS
        # band 4); anything past 90 is below the horizon and cannot light the surface.
        illumination = np.clip(np.cos(np.radians(np.clip(sun, 0.0, 90.0))), 0.0, 1.0)
        s = soil + self.sun_weight * illumination
        # NaN is "this observation may not occupy this cell" (04 section 4), not a low score:
        # below the floor the retrieval is not merely worse, it is unusable
        s[soil < self.hard_floor] = np.nan
        s[sun > self.max_solar_zenith] = np.nan
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
