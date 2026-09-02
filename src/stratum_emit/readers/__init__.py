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

# Source patterns for the delivered products (12 section 5): {collection: {asset: glob}}, the
# same globs the shipped manifests use, so an index built from these shares granule ids - and
# therefore GLT and snapshot cache keys (06 section 4) - with one built from a manifest or
# from CMR: the collection version is part of the literal prefix, and the id is
# `20260604T180210_2615512_009`, not `001_2026...`. Collection names are CMR short names
# (verified 2026-09-02): there is no EMITL1BOBS - the OBS file is the second asset of an
# EMITL1BRAD.001 record, and the 1.85 GB RAD file is deliberately not listed so a mosaic run
# never fetches it. EMITL2AMASK is published at collection version 002. EMITL2BFRCOV ships
# per-fraction GeoTIFFs (FRCOVBARE/PV/NPV, each with an UNC twin, plus FRCOVQC); the reader for
# them is not built (see L2BFrcov).
LOCAL_PATTERNS: dict[str, dict[str, str]] = {
    "EMITL2BMIN": {"MIN": "EMIT_L2B_MIN_001_*.nc", "MINUNCERT": "EMIT_L2B_MINUNCERT_001_*.nc"},
    "EMITL1BRAD": {"OBS": "EMIT_L1B_OBS_001_*.nc"},
    "EMITL2AMASK": {"MASK": "EMIT_L2A_MASK_002_*.nc"},
    "EMITL2BFRCOV": {"FRCOVBARE": "EMIT_L2B_FRCOVBARE_001_*.tif"},
}


class L2BMin(EmitNetCDFReader):
    """EMITL2BMIN: both files of a record - `MIN` (`group_N_mineral_id` int16, `group_N_band_depth`
    float32) and `MINUNCERT` (`group_N_band_depth_unc`, `group_N_fit` float32) - share one layout
    and one reader. The class table lives at `/mineral_metadata` (11 section 9)."""

    collections = ("EMITL2BMIN",)
    space: Literal["sensor", "ortho"] = "sensor"
    band_names_var = None


class L1BRad(EmitNetCDFReader):
    """EMITL1BRAD: reads whichever asset of the record it is handed (12 section 3). The `OBS`
    asset - the one a mosaic run reads - carries `obs` as (downtrack, crosstrack, bands)
    float32 with band names in `sensor_band_parameters/observation_bands` (`band_attrs["name"]`;
    layout per `spec_io.open_emit_obs_nc`). The `RAD` asset shares the layout with `radiance`
    over `sensor_band_parameters/wavelengths` instead; `variables()` reports that file's root
    variables without band names rather than failing on its header. CMR has no EMITL1BOBS."""

    collections = ("EMITL1BRAD",)
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
    plan-time validation. The delivered EMITL2BFRCOV.001 is per-fraction GeoTIFFs
    (`EMIT_L2B_FRCOVBARE_001_*.tif` etc., 12 section 5), not NetCDF; this class opens NetCDF
    only, so the reader for the shipped files is not built."""

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


__all__ = ["LOCAL_PATTERNS", "EmitNetCDFReader", "L1BRad", "L2AMask", "L2BFrcov", "L2BMin",
           "NetCDFContext"]
