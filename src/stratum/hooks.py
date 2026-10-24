"""The five science hooks. Spec: docs/specs/04-cost-functions.md and 07-output-mapping.md.

Each one changes the answer. The two access hooks - GranuleReader, GranuleSource - live in
`stratum.types` and add a data source instead (12 section 1).
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

import numpy as np

from stratum.types import AuxAccessor, BandSpec, BandStack, GranuleFrame, ObsWindow, SnapshotStack

FloatArray = np.ndarray
BoolArray = np.ndarray
RGBAArray = np.ndarray


@runtime_checkable
class GranuleFilter(Protocol):
    """Runs against the index at plan time, before any pixel IO (04 section 2)."""

    def keep(self, granules: GranuleFrame) -> BoolArray: ...
    def describe(self) -> str: ...


@runtime_checkable
class PixelMask(Protocol):
    """Runs in resolve's read path (04 section 3). Boolean: masking is not ranking.

    `required_aux` is declared here for the same reason it is on `Scorer`: an undeclared read
    produces a cache key that lies (05 section 5). Only a `space: "map"` mask may declare it -
    aux is on the BLOCK grid by definition (05 section 1) and a sensor-space mask sees sensor
    geometry, so the arrays would not even share a shape. `validate_static` refuses the
    combination and resolve hands a sensor-space mask `NullAux`.
    """

    space: Literal["map", "sensor"]
    required_roles: tuple[str, ...]
    required_aux: tuple[str, ...]

    def valid(self, obs: ObsWindow, aux: AuxAccessor) -> BoolArray: ...


@runtime_checkable
class Scorer(Protocol):
    """Decides which observation wins a cell. What is recorded about the winner is the snapshot
    schema's business, not the scorer's (04 section 4, 13).

    `capability` defaults to "streaming" when a class does not declare it; the framework reads
    it with `getattr(scorer, "capability", "streaming")`.

    - streaming: `score` is called once per observation; bands are (H, W[, B]), `obs.granule`
      is the look, the return is (H, W).
    - stack: `score` is called ONCE per block with every observation of the epoch that reaches
      it; bands, `obs.valid` and `obs.interpolated` are (N, H, W[, B]), row i is
      `obs.granules[i]` (ordered by (datetime, granule_id)), `obs.granule` is None, and the
      return is (N, H, W). `obs.at(i)` and `for o in obs` give per-observation views of the
      same rows. The kept buffers are read-only.

    In both, higher wins and NaN means "may not occupy this cell"; on a tie the earliest look
    keeps the cell. Optional `required_layers: tuple[str, ...]` names the snapshot layers the
    scorer reads through `obs.layer(name)`; it is read with `getattr(..., ())` and checked at
    plan time.

    Scorer-emitted layers (optional): `outputs: tuple[BandSpec, ...]` declares per-cell values
    the scorer can deliver beside its score, and `emit(obs, aux) -> Mapping[str, array]`
    computes them. A snapshot layer reaches one with `source: scorer.<output>`.
    - `emit` runs iff the schema has at least one `scorer.*` layer, and then once per `score`
      call, immediately after it, with the SAME `obs` object and no other call on the instance
      in between - so a scorer may memoise work from `score` and reuse it in `emit`.
    - It returns a mapping with exactly the declared output names.
    - Streaming: each array is (H, W), a per-observation value; the framework records it where
      that observation wins. Stack: (N, H, W) is per observation and the framework keeps the
      winning row; (H, W) is per cell - a whole-stack property such as how many looks agreed -
      and is taken as-is. Any other shape is an error.
    - Values are written only where some observation won the cell, NaN elsewhere.
    - Each output is one continuous float32 or float64 band. It enters the snapshot key through
      `layers_hash` (the layer's name, source and dtype); the code that computes it is covered
      by the scorer's version, exactly as `score` is (06 section 2).

    Optional members are deliberately not declared below: this protocol is runtime-checkable,
    and declaring them would make them mandatory.
    """

    capability: Literal["streaming", "stack", "tile"]
    halo: int
    required_roles: tuple[str, ...]
    required_aux: tuple[str, ...]

    def score(self, obs: ObsWindow, aux: AuxAccessor) -> FloatArray: ...


@runtime_checkable
class Reducer(Protocol):
    """Only for what the schema's aggregation vocabulary cannot say (04 section 5)."""

    outputs: tuple[BandSpec, ...]
    halo: int

    def reduce(self, snaps: SnapshotStack, aux: AuxAccessor) -> dict[str, np.ndarray]: ...


@dataclass(frozen=True)
class ImageSpec:
    name: str
    kind: Literal["RGBA"] = "RGBA"


@dataclass(frozen=True)
class Legend:
    """Class table or ramp stops. Sidecar and STAC; not optional (07 section 2)."""

    kind: Literal["categorical", "continuous"]
    entries: Sequence[Mapping[str, Any]]
    note: str | None = None


@runtime_checkable
class OutputMapper(Protocol):
    """A presentation layer over a data product that still ships (07)."""

    outputs: tuple[ImageSpec, ...]

    def render(self, bands: BandStack, aux: AuxAccessor) -> RGBAArray: ...
    def legend(self) -> Legend: ...
