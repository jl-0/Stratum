"""EMIT reducers (04 section 5).

A `Reducer` plugin is only for what the schema's aggregation vocabulary cannot say. Everything
the vocabulary covers - vote, median, inverse_variance, the rest of 13 section 4 - is declared
per layer in `aggregate` and needs no code, so there is exactly one reducer here and it earns
its place by combining two categorical layers, which no `aggregate` entry can do.
"""
from __future__ import annotations

from collections.abc import Mapping

import numpy as np

from stratum.reduce import CATEGORICAL_NODATA, VoteParams, vote
from stratum.types import BandSpec, SnapshotStack


class JointMineralVote:
    """One mineral answer from EMIT's two mineral groups.

    EMIT's L2B product identifies minerals in two spectral regions and reports them as separate
    variables. Group 1 is the ~1 micron region - iron oxides, Fe-bearing silicates. Group 2 is
    2 to 2.5 microns - clays, micas, carbonates, sulfates - and carries the diagnostic features
    that name a hydrothermal system: at Cuprite, alunite, kaolinite and buddingtonite are all
    group 2.

    The aggregation vocabulary votes each layer independently and stops there; it has no way to
    say "prefer this layer's answer where it has one". `conditional_on` is the nearest thing and
    it conditions a CONTINUOUS layer on a categorical one, not one categorical on another. So:
    a plugin.

    The rule, deliberately simple enough to explain to a geologist:

    1. Vote each group independently, using the framework's own kernel so the tie-breaks,
       `min_count` and agreement are identical to what the vocabulary would have produced.
    2. Where the preferred group has a winner, take it.
    3. Otherwise fall back to the other group's winner.
    4. Where neither has one, nodata.

    Both groups index ONE class table - EMIT's `/mineral_metadata`, 294 entries - and their id
    ranges are disjoint (group 1 is 1-95, group 2 is 96-294), so the combined band needs no new
    enumeration and reads correctly against the product's existing class table. That is what
    makes this expressible at all; a plugin cannot introduce an enumeration of its own.

    `which` reports where each cell's answer came from, because a combined band that does not
    say which group won is a band nobody can check: 0 nodata, 1 the fallback group, 2 the
    preferred group.
    """

    halo = 0

    #: 2 = the preferred layer won the cell, 1 = the fallback did, 0 = neither
    FROM_NONE, FROM_FALLBACK, FROM_PREFERRED = 0, 1, 2

    def __init__(self, prefer: str = "mineral_2", fallback: str = "mineral_1",
                 name: str = "mineral_joint", min_count: int = 2,
                 ignore: tuple[str, ...] = ("none",),
                 tie_break: str = "highest_score") -> None:
        if prefer == fallback:
            raise ValueError("JointMineralVote: prefer and fallback must name different layers; "
                             f"both are {prefer!r}")
        self.prefer = prefer
        self.fallback = fallback
        self.name = name
        self.min_count = min_count
        self.ignore = tuple(ignore)
        self.tie_break = tie_break
        self.outputs = (
            BandSpec(name, "uint16",
                     f"joint mineral id: {prefer} where it voted, else {fallback}; product ids",
                     nodata=CATEGORICAL_NODATA),
            BandSpec(f"{name}_agreement", "float32",
                     "agreement of whichever group supplied the answer", units="fraction",
                     nodata=float("nan")),
            BandSpec(f"{name}_from", "uint16",
                     f"which group answered: 2 = {prefer}, 1 = {fallback}, 0 = neither",
                     units="code"),
        )

    # ---------------------------------------------------------------------------------------
    def _vote_one(self, snaps: SnapshotStack, layer: str,
                  n_epochs: np.ndarray) -> Mapping[str, np.ndarray]:
        """One layer through the framework's own vote, so the result is what the vocabulary
        would have delivered for it - same decision order, same tie-breaks, same agreement."""
        spec = snaps.schema[layer]
        params = VoteParams(min_count=self.min_count, ignore=self.ignore,
                            tie_break=self.tie_break)
        return vote(spec, np.asarray(snaps[layer]), np.asarray(snaps.valid, dtype=bool),
                    np.asarray(snaps.score, dtype=np.float32), n_epochs, params)

    def reduce(self, snaps: SnapshotStack, aux: object) -> dict[str, np.ndarray]:
        declared = {layer.name for layer in snaps.schema.layers}
        missing = [n for n in (self.prefer, self.fallback) if n not in declared]
        if missing:
            raise KeyError(
                f"JointMineralVote needs layer(s) {missing}, which the snapshot schema does not "
                f"declare; it has {sorted(declared)}. Add them to `snapshot.layers`, or point "
                "`prefer`/`fallback` at layers that exist")

        n_epochs = np.asarray(snaps.valid, dtype=bool).sum(axis=0)
        top = self._vote_one(snaps, self.prefer, n_epochs)
        low = self._vote_one(snaps, self.fallback, n_epochs)

        won_top = top[self.prefer] != CATEGORICAL_NODATA
        won_low = low[self.fallback] != CATEGORICAL_NODATA

        joint = np.where(won_top, top[self.prefer], low[self.fallback]).astype(np.uint16)
        joint[~(won_top | won_low)] = CATEGORICAL_NODATA

        agreement = np.where(won_top, top[f"{self.prefer}_agreement"],
                             low[f"{self.fallback}_agreement"]).astype(np.float32)
        agreement[~(won_top | won_low)] = np.nan

        came_from = np.full(joint.shape, self.FROM_NONE, dtype=np.uint16)
        came_from[won_low] = self.FROM_FALLBACK
        came_from[won_top] = self.FROM_PREFERRED

        return {self.name: joint,
                f"{self.name}_agreement": agreement,
                f"{self.name}_from": came_from,
                "n_epochs": n_epochs.astype(np.uint16)}


__all__ = ["JointMineralVote"]
