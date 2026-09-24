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


def _below_floor(s: np.ndarray, soil: np.ndarray, hard_floor: float,
                 below_floor_weight: float | None) -> None:
    """Apply the cover floor to a score in place: NaN below it, or - with `below_floor_weight` -
    the score scaled down so it still ranks and still votes, but only as a fraction of a look at
    ground. The caller has already checked the weight is small enough that no graded-down look
    can outrank any look at or above the floor."""
    under = soil < hard_floor
    if below_floor_weight is None:
        s[under] = np.nan
    else:
        s[under] *= below_floor_weight


def _check_below_floor_weight(name: str, weight: float | None, hard_floor: float,
                              ceiling: float) -> None:
    """A graded-down look scores at most `ceiling * weight`; a look at the floor at least
    `hard_floor`. The first must stay below the second or the floor stops meaning anything."""
    if weight is None:
        return
    if not 0.0 < weight <= 1.0:
        raise ValueError(f"{name}: below_floor_weight must be in (0, 1]; got {weight}")
    if ceiling * weight >= hard_floor:
        raise ValueError(
            f"{name}: below_floor_weight {weight} lets a look below hard_floor score up to "
            f"{ceiling * weight:.3g}, which reaches a look at the floor ({hard_floor}); it must "
            f"be below {hard_floor / ceiling:.3g} so any look at ground still beats any look "
            "through vegetation")


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

    **Below the floor: veto or grade.** By default an observation under `hard_floor` is NaN and
    cannot occupy the cell. With `below_floor_weight` it keeps its score scaled by that factor
    instead, so it still ranks within the epoch (after every look at ground) and, under a
    `vote` with `weight: score`, still votes across epochs at that fraction of a bare look's
    strength. That is the difference between "we never saw ground here" and "we have no answer
    here": measured over US West 2025, the veto removed 82.5 % of observed pixels.

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
                 sun_weight: float = 0.25, max_solar_zenith: float = 80.0,
                 below_floor_weight: float | None = None) -> None:
        if not 0.0 <= hard_floor <= min_soil <= 1.0:
            raise ValueError("PreferBareEarth needs 0 <= hard_floor <= min_soil <= 1; got "
                             f"hard_floor={hard_floor}, min_soil={min_soil}")
        if sun_weight < 0:
            raise ValueError("PreferBareEarth: sun_weight must not be negative")
        _check_below_floor_weight("PreferBareEarth", below_floor_weight, hard_floor,
                                  hard_floor + sun_weight)
        self.min_soil = min_soil
        self.hard_floor = hard_floor
        self.sun_weight = sun_weight
        self.max_solar_zenith = max_solar_zenith
        self.below_floor_weight = below_floor_weight

    def score(self, obs: ObsWindow, aux: AuxAccessor) -> np.ndarray:
        soil = np.clip(np.ma.filled(obs["frcov"], np.nan).astype("float32"), 0.0, 1.0)
        sun = np.ma.filled(obs["solar_zenith"], np.nan).astype("float32")
        # cos of the solar zenith: 1 overhead, 0 at the horizon. Angles are degrees (L1B OBS
        # band 4); anything past 90 is below the horizon and cannot light the surface.
        illumination = np.clip(np.cos(np.radians(np.clip(sun, 0.0, 90.0))), 0.0, 1.0)
        s = soil + self.sun_weight * illumination
        # NaN is "this observation may not occupy this cell" (04 section 4), not a low score.
        # Below the cover floor that is the default; `below_floor_weight` grades instead.
        _below_floor(s, soil, self.hard_floor, self.below_floor_weight)
        s[sun > self.max_solar_zenith] = np.nan
        return s


class BareEarthNadir:
    """Bare ground, well lit, seen near nadir - the three terms a base map wants, ranked.

    `PreferBareEarth` reads cover and illumination and has no view-zenith term at all, so a
    further-off-nadir look at marginally barer ground wins. On EMIT that matters less than it
    sounds: the swath is ~74 km from ~420 km, so view zenith spans only a few degrees. Measured
    over one tile and a full year of 2025 (tile -235_75, 24 granules, `min_soil: 0.65`):

    | scorer | winner view zenith | winner solar zenith |
    |---|---|---|
    | `min_view_zenith` | mean 6.83 deg | mean 34.2 deg |
    | `prefer_bare_earth` | mean 8.40 deg | mean 27.9 deg |

    Ignoring nadir costs **+1.57 deg mean** (p95 +4.8, max +6.7; 34 % of cells take a >2 deg
    worse look) and buys **6.3 deg of illumination**. The group-1 mineral label agrees on
    **99.4 %** of co-classified cells either way, so this is a cosmetic-continuity knob and not a
    correctness fix - which is worth saying plainly, because the obvious assumption is the
    opposite.

    It exists so the trade is explicit rather than implied by which plugin you named. Cover
    decides; illumination and nadir break ties among comparable ground. `nadir_weight: 0`
    reproduces `PreferBareEarth` exactly. `below_floor_weight` grades a look below
    `hard_floor` instead of vetoing it, exactly as it does there.
    """

    capability = "streaming"
    halo = 0
    required_roles = ("frcov", "solar_zenith", "view_zenith")
    required_aux: tuple[str, ...] = ()

    #: EMIT's view zenith spans a few degrees, so the nadir term is normalised over this range
    #: rather than over 0-90: dividing by 90 would make it numerically irrelevant.
    VIEW_ZENITH_SCALE = 15.0

    def __init__(self, min_soil: float = 0.80, hard_floor: float = 0.65,
                 sun_weight: float = 0.25, nadir_weight: float = 0.10,
                 max_solar_zenith: float = 80.0, max_view_zenith: float = 90.0,
                 below_floor_weight: float | None = None) -> None:
        if not 0.0 <= hard_floor <= min_soil <= 1.0:
            raise ValueError("BareEarthNadir needs 0 <= hard_floor <= min_soil <= 1; got "
                             f"hard_floor={hard_floor}, min_soil={min_soil}")
        if sun_weight < 0 or nadir_weight < 0:
            raise ValueError("BareEarthNadir: sun_weight and nadir_weight must not be negative")
        if sun_weight + nadir_weight > 1.0:
            raise ValueError("BareEarthNadir: sun_weight + nadir_weight must not exceed 1, or "
                             "the tie-breakers outrank cover, which is the one thing they may "
                             f"not do; got {sun_weight} + {nadir_weight}")
        self.min_soil = min_soil
        self.hard_floor = hard_floor
        self.sun_weight = sun_weight
        self.nadir_weight = nadir_weight
        self.max_solar_zenith = max_solar_zenith
        self.max_view_zenith = max_view_zenith
        _check_below_floor_weight("BareEarthNadir", below_floor_weight, hard_floor,
                                  hard_floor + sun_weight + nadir_weight)
        self.below_floor_weight = below_floor_weight

    def score(self, obs: ObsWindow, aux: AuxAccessor) -> np.ndarray:
        soil = np.clip(np.ma.filled(obs["frcov"], np.nan).astype("float32"), 0.0, 1.0)
        sun = np.ma.filled(obs["solar_zenith"], np.nan).astype("float32")
        view = np.ma.filled(obs["view_zenith"], np.nan).astype("float32")
        illumination = np.clip(np.cos(np.radians(np.clip(sun, 0.0, 90.0))), 0.0, 1.0)
        nadir = 1.0 - np.clip(np.abs(view) / self.VIEW_ZENITH_SCALE, 0.0, 1.0)
        s = soil + self.sun_weight * illumination + self.nadir_weight * nadir
        # NaN is "this observation may not occupy this cell" (04 section 4), not a low score.
        # Below the cover floor that is the default; `below_floor_weight` grades instead.
        _below_floor(s, soil, self.hard_floor, self.below_floor_weight)
        s[sun > self.max_solar_zenith] = np.nan
        s[np.abs(view) > self.max_view_zenith] = np.nan
        return s


class LowVegetationNadir:
    """Bare earth defined by what is NOT there: green vegetation below `max_pv`.

    `BareEarthNadir` ranks on the bare-soil fraction, which is `1 - PV - NPV` in a three-component
    unmixing - so it inherits the NPV estimate, and the current FRCOV reads some bare soil as
    NPV (the caveat on `PreferBareEarth`). The science team's current position is that NPV is
    not reliable enough to lean on, and that the test is photosynthetic vegetation alone: a look
    is bare earth when PV is at most `max_pv` (0.40). This scorer is that rule, with the same
    illumination and nadir tie-breakers as `BareEarthNadir`.

    **Cover** is `1 - PV`, so among bare looks the least green wins. A look above `max_pv` is
    NaN by default; with `vegetated_weight` it is graded instead - scored at that fraction, so
    it ranks after every bare look within an epoch and, under a `vote` with `weight: score`,
    votes across epochs at that fraction of a bare look's strength. Pair it with
    `min_count_unless_score` at `1 - max_pv` and a class seen only through vegetation needs
    `min_count` agreeing epochs while one bare look stands on its own.

    Snow, cloud, urban and water need no term here: FRCOV writes fill into every fraction
    wherever its QC band flags them (FRCOVQC 1-4; 4 is snow/ice at NDSI > 0.4), and fill makes
    the observation invalid before any scorer runs.
    """

    capability = "streaming"
    halo = 0
    required_roles = ("pv", "solar_zenith", "view_zenith")
    required_aux: tuple[str, ...] = ()

    def __init__(self, max_pv: float = 0.40, vegetated_weight: float | None = None,
                 sun_weight: float = 0.25, nadir_weight: float = 0.10,
                 max_solar_zenith: float = 80.0, max_view_zenith: float = 90.0) -> None:
        if not 0.0 <= max_pv < 1.0:
            raise ValueError(f"LowVegetationNadir: max_pv must be in [0, 1); got {max_pv}")
        if sun_weight < 0 or nadir_weight < 0:
            raise ValueError("LowVegetationNadir: sun_weight and nadir_weight must not be negative")
        if sun_weight + nadir_weight > 1.0:
            raise ValueError("LowVegetationNadir: sun_weight + nadir_weight must not exceed 1, or "
                             "the tie-breakers outrank cover")
        floor = 1.0 - max_pv
        _check_below_floor_weight("LowVegetationNadir", vegetated_weight, floor,
                                  floor + sun_weight + nadir_weight)
        self.max_pv = max_pv
        self.vegetated_weight = vegetated_weight
        self.sun_weight = sun_weight
        self.nadir_weight = nadir_weight
        self.max_solar_zenith = max_solar_zenith
        self.max_view_zenith = max_view_zenith

    def score(self, obs: ObsWindow, aux: AuxAccessor) -> np.ndarray:
        pv = np.clip(np.ma.filled(obs["pv"], np.nan).astype("float32"), 0.0, 1.0)
        sun = np.ma.filled(obs["solar_zenith"], np.nan).astype("float32")
        view = np.ma.filled(obs["view_zenith"], np.nan).astype("float32")
        illumination = np.clip(np.cos(np.radians(np.clip(sun, 0.0, 90.0))), 0.0, 1.0)
        nadir = 1.0 - np.clip(np.abs(view) / BareEarthNadir.VIEW_ZENITH_SCALE, 0.0, 1.0)
        cover = 1.0 - pv
        s = cover + self.sun_weight * illumination + self.nadir_weight * nadir
        # "above max_pv" is "cover below 1 - max_pv"; the helper takes the floor on cover
        _below_floor(s, cover, 1.0 - self.max_pv, self.vegetated_weight)
        s[sun > self.max_solar_zenith] = np.nan
        s[np.abs(view) > self.max_view_zenith] = np.nan
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


__all__ = ["BareEarthNadir", "CleanestNadir", "LowVegetationNadir", "MaxBandDepth",
           "MinViewZenith", "PreferBareEarth"]
