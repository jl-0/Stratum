"""EMIT granule readers (12 section 3). Registered by collection name in pyproject
(`[project.entry-points."stratum.readers"]`).

Observed facts these honour (11 sections 2, 7, 11): -9999 is fill and 0 is "nothing
identified"; the shipped GLT is 1-based on the granule's own ortho grid; science variables are
one gzip chunk each, so a windowed read decodes the whole variable and the window bounds memory,
not bytes.
"""
from __future__ import annotations

from typing import Literal

import numpy as np

from stratum.types import LocArray, SensorWindow
from stratum_emit.readers.netcdf import EmitNetCDFReader, NetCDFContext

# LocalSource patterns for the delivered products (12 section 5): {collection: {asset: glob}}.
LOCAL_PATTERNS: dict[str, dict[str, str]] = {
    "EMITL2BMIN": {"MIN": "EMIT_L2B_MIN_*.nc", "MINUNCERT": "EMIT_L2B_MINUNCERT_*.nc"},
    "EMITL1BOBS": {"OBS": "EMIT_L1B_OBS_*.nc"},
    "EMITL2AMASK": {"MASK": "EMIT_L2A_MASK_*.nc"},
    "EMITL2BFRCOV": {"FRCOV": "EMIT_L2B_FRCOV_*.nc"},
}


class L2BMin(EmitNetCDFReader):
    """EMITL2BMIN: both files of a record - `MIN` (`group_N_mineral_id` int16, `group_N_band_depth`
    float32) and `MINUNCERT` (`group_N_band_depth_unc`, `group_N_fit` float32) - share one layout
    and one reader. The class table lives at `/mineral_metadata` (11 section 9)."""

    collections = ("EMITL2BMIN",)
    space: Literal["sensor", "ortho"] = "sensor"
    band_names_var = None


class L1BObs(EmitNetCDFReader):
    """EMITL1BOBS: `obs` is (downtrack, crosstrack, bands) float32; band names come from
    `sensor_band_parameters/observation_bands` as `band_attrs["name"]`. Written against the
    SpectralUtil reader (`spec_io.open_emit_obs_nc`) as documentation - no OBS granule is
    local yet."""

    collections = ("EMITL1BOBS",)
    space: Literal["sensor", "ortho"] = "sensor"
    band_names_var = "observation_bands"


class L2AMask(EmitNetCDFReader):
    """EMITL2AMASK: `mask` is (downtrack, crosstrack, bands); names from
    `sensor_band_parameters/mask_bands` (`spec_io.open_emit_l2a_mask_nc`)."""

    collections = ("EMITL2AMASK",)
    space: Literal["sensor", "ortho"] = "sensor"
    band_names_var = "mask_bands"


class L2BFrcov(EmitNetCDFReader):
    """Already orthorectified: no loc array, no KD-tree (12 section 2). The ortho-native read
    path is out of the first slice, so `read` refuses; `open`/`variables` still work for
    plan-time validation."""

    collections = ("EMITL2BFRCOV",)
    space: Literal["sensor", "ortho"] = "ortho"
    band_names_var = None

    def read(self, ctx: NetCDFContext, var: str,
             window: SensorWindow | None = None) -> np.ma.MaskedArray:
        raise NotImplementedError(
            "L2BFrcov is ortho-native; warping an ortho role onto the block grid is the "
            "12 section 2 ortho path, not in the first slice")

    def geolocation(self, ctx: NetCDFContext) -> LocArray | None:
        return None


__all__ = ["LOCAL_PATTERNS", "EmitNetCDFReader", "L1BObs", "L2AMask", "L2BFrcov", "L2BMin",
           "NetCDFContext"]
