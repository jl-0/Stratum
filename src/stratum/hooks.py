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
    """Runs in resolve's read path (04 section 3). Boolean: masking is not ranking."""

    space: Literal["map", "sensor"]
    required_roles: tuple[str, ...]

    def valid(self, obs: ObsWindow, aux: AuxAccessor) -> BoolArray: ...


@runtime_checkable
class Scorer(Protocol):
    """Decides which observation wins a cell. What is recorded about the winner is the snapshot
    schema's business, not the scorer's (04 section 4, 13)."""

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
