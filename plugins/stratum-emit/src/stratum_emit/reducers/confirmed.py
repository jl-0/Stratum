"""`confirmed_mineral_vote`: every look votes, weighted by how well it saw and how well it fit;
a mineral needs a second look to agree; a cell seen with nothing confirmed is `none`.

WHY A REDUCER. Three things the aggregation vocabulary cannot say (04 section 5):

1. A vote weighted per LAYER. `vote` weighs an epoch by its one score, but the confidence of an
   identification differs between the two groups of the same look - group 1's fit is not group
   2's. Here each group's vote is weighted by score x that group's own Tetracorder fit.
2. A minimum count that picks among the classes that clear it. `vote` takes the top tally and
   suppresses it when it is thin, even where the runner-up was seen three times; here only the
   classes seen in at least `min_count` looks are candidates.
3. `none` for an observed cell. `vote` with `ignore: [none]` delivers nodata where every look
   said none, which reads as "not observed". Here nodata means exactly that - no valid look -
   and an observed cell with no confirmed mineral is `none` (0), as in the L2B product itself.

WHY FIT AND NOT DEPTH. Tetracorder names a material by its highest fit, independently of depth;
depth is how much of it there is, not whether it is there. At Cuprite, maps of the largest depth
and of the largest fit x depth both named the wrong mineral on ground-checked alunite where the
largest fit was right (Clark et al. 2003, JGR 108(E12) 5131). So fit weights the identity vote
and depth is only averaged afterwards, over the looks that agreed.

Each look's score (the scorer's grade of the observation - cover, sun, nadir) multiplies in, so
a look through vegetation or at a grazing sun counts for less but is never thrown away.

Use with a short epoch (`P1D`): the vote is over the epoch winners, so a long epoch would
discard every look but its best before the vote sees it.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

import numpy as np

from stratum.reduce import CATEGORICAL_NODATA
from stratum.types import BandSpec, SnapshotStack

_NODATA = np.uint16(CATEGORICAL_NODATA)


class ConfirmedMineralVote:
    """Per group, independently:

    1. Every valid epoch is a look. A look that identified a class votes for it with weight
       `score x fit` (`weight: count` makes every vote 1, `weight: score` drops the fit). A look
       that identified nothing (`none`, id 0) does not vote but still counts in `n_valid`.
    2. Candidates are the classes voted for by at least `min_count` looks - or by every valid
       look, where the cell had fewer than `min_count` (one look of a cell seen once is the
       whole record, and nothing contradicts it).
    3. The candidate with the largest summed weight wins; a tie goes to the one holding the
       single highest-weighted look, then to the lowest id.
    4. No candidate: `none` where the cell had a valid look, nodata where it had none.

    Delivered per group (`mineral` names the layer, e.g. `mineral_1`): the class (`mineral_1`),
    `{mineral}_support` (looks that voted for it), `{mineral}_agreement` (support / valid looks,
    the same ratio as `mode_gated_select`'s `agreement`), `{mineral}_runner_up` (the best other
    candidate or, failing one, the best unconfirmed class), the band depth averaged over the
    agreeing looks with weight score x fit (`depth` names both the layer and the band), and
    `acquired_out`, the acquisition of the agreeing look with the largest weight - the selected
    scene. `n_epochs` (valid looks, over both groups alike) is delivered once.
    """

    halo = 0
    WEIGHTS = ("score_fit", "score", "count")

    def __init__(self, groups: Sequence[Mapping[str, str]] = (
                     {"mineral": "mineral_1", "fit": "fit_1", "depth": "depth_1",
                      "acquired_out": "acquired_1"},
                     {"mineral": "mineral_2", "fit": "fit_2", "depth": "depth_2",
                      "acquired_out": "acquired_2"}),
                 acquired: str = "acquired", min_count: int = 2,
                 weight: str = "score_fit") -> None:
        name = "ConfirmedMineralVote"
        groups = [dict(g) for g in groups]
        if not groups:
            raise ValueError(f"{name}: groups names at least one mineral layer")
        for g in groups:
            missing = [k for k in ("mineral", "fit", "depth", "acquired_out") if k not in g]
            if missing:
                raise ValueError(f"{name}: each group needs mineral, fit, depth and "
                                 f"acquired_out; {g} lacks {missing}")
        if isinstance(min_count, bool) or not isinstance(min_count, int) or min_count < 1:
            raise ValueError(f"{name}: min_count must be a positive integer; got {min_count!r}")
        if weight not in self.WEIGHTS:
            raise ValueError(f"{name}: weight must be one of {list(self.WEIGHTS)}; got {weight!r}")
        self.groups = groups
        self.acquired = acquired
        self.min_count = min_count
        self.weight = weight
        out: list[BandSpec] = []
        for g in groups:
            m = g["mineral"]
            out += [
                BandSpec(m, "uint16", "confirmed mineral id; 0 = observed, nothing confirmed",
                         nodata=CATEGORICAL_NODATA),
                BandSpec(f"{m}_support", "float32", "looks that identified the delivered class",
                         units="count", nodata=float("nan")),
                BandSpec(f"{m}_agreement", "float32", "support / valid looks", units="fraction",
                         nodata=float("nan")),
                BandSpec(f"{m}_runner_up", "uint16", "the next class by summed weight",
                         nodata=CATEGORICAL_NODATA),
                BandSpec(g["depth"], "float32", "band depth, score x fit weighted mean over the "
                         "looks that identified the delivered class", nodata=float("nan")),
                BandSpec(g["acquired_out"], "float64", "acquisition start (Unix seconds) of the "
                         "highest-weighted agreeing look", units="s", nodata=float("nan")),
            ]
        out.append(BandSpec("n_epochs", "uint16", "valid looks", units="count"))
        self.outputs = tuple(out)

    def _layer(self, snaps: SnapshotStack, name: str) -> np.ndarray:
        declared = {layer.name for layer in snaps.schema.layers}
        if name not in declared:
            raise KeyError(f"ConfirmedMineralVote needs layer {name!r}, which the snapshot "
                           f"schema does not declare; it has {sorted(declared)}")
        return np.ma.filled(np.ma.asarray(snaps[name]).astype(np.float64), np.nan)

    def _one(self, snaps: SnapshotStack, g: Mapping[str, str], valid: np.ndarray,
             score: np.ndarray, acquired: np.ndarray) -> dict[str, np.ndarray]:
        ids = np.asarray(np.ma.getdata(snaps[g["mineral"]])).astype(np.int64)
        fit = self._layer(snaps, g["fit"])
        depth = self._layer(snaps, g["depth"])
        voter = valid & (ids > 0) & (ids != CATEGORICAL_NODATA)
        if self.weight == "count":
            w = np.ones(ids.shape)
        elif self.weight == "score":
            w = np.where(np.isfinite(score), score, 0.0)
        else:
            w = np.where(np.isfinite(score), score, 0.0) * np.where(np.isfinite(fit), fit, 0.0)
        w = np.where(voter, np.maximum(w, 0.0), 0.0)

        n_valid = valid.sum(axis=0)
        need = np.minimum(self.min_count, np.maximum(n_valid, 1))
        observed = n_valid > 0
        classes = np.unique(ids[voter])
        h, wd = ids.shape[1:]
        # per class: whether it is a candidate, its summed weight, its single best look, its count
        cand = np.zeros((classes.size, h, wd), dtype=bool)
        total = np.zeros((classes.size, h, wd), dtype=np.float64)
        top = np.full((classes.size, h, wd), -1.0, dtype=np.float64)
        count = np.zeros((classes.size, h, wd), dtype=np.float32)
        for i, cls in enumerate(classes):
            hit = voter & (ids == cls)
            count[i] = hit.sum(axis=0)
            total[i] = np.where(hit, w, 0.0).sum(axis=0)
            top[i] = np.where(hit, w, -1.0).max(axis=0)
            cand[i] = count[i] >= need
        # rank: every candidate above every unconfirmed class, then summed weight; the single
        # best look only separates totals equal to float32 precision
        span = 2.0 * (float(total.max()) + 1.0) if classes.size else 1.0
        rank = np.where(count > 0, np.where(cand, span, 0.0) + total.astype(np.float64)
                        + 1e-7 * np.maximum(top, 0.0), -1.0)
        if classes.size:
            order = np.argsort(-rank, axis=0, kind="stable")
            first, nxt = order[0], order[min(1, classes.size - 1)]
            confirmed = np.take_along_axis(cand, first[None], axis=0)[0]
            winner = classes[first]
            support = np.take_along_axis(count, first[None], axis=0)[0]
            has_next = (classes.size > 1) & (np.take_along_axis(count, nxt[None], axis=0)[0] > 0)
            runner = np.where(has_next, classes[nxt], int(_NODATA))
            # with no confirmed winner the best unconfirmed class IS the runner-up
            runner = np.where(confirmed, runner,
                              np.where(np.take_along_axis(count, first[None], axis=0)[0] > 0,
                                       winner, int(_NODATA)))
        else:
            confirmed = np.zeros((h, wd), dtype=bool)
            winner = np.zeros((h, wd), dtype=np.int64)
            support = np.zeros((h, wd))
            runner = np.full((h, wd), int(_NODATA))
        out_id = np.where(confirmed, winner, 0)
        out_id = np.where(observed, out_id, int(_NODATA)).astype(np.uint16)
        runner = np.where(observed, runner, int(_NODATA))

        agree = voter & (ids == out_id[None].astype(np.int64)) & confirmed[None]
        wa = np.where(agree, w, 0.0)
        with np.errstate(invalid="ignore", divide="ignore"):
            d = np.where(agree & np.isfinite(depth), depth, 0.0)
            dw = np.where(agree & np.isfinite(depth), wa, 0.0)
            depth_out = np.where(dw.sum(axis=0) > 0, (d * dw).sum(axis=0) / dw.sum(axis=0),
                                 np.nan)
            # weight 0 everywhere (weight: score with zero scores) still has agreeing looks
            pick = np.argmax(np.where(agree, wa + 1e-12, -1.0), axis=0)
            acq = np.take_along_axis(acquired, pick[None], axis=0)[0]
            acq = np.where(agree.any(axis=0), acq, np.nan)
            agreement = np.where(observed, np.where(confirmed, support, 0.0) / n_valid, np.nan)
        m = g["mineral"]
        return {m: out_id,
                f"{m}_support": np.where(observed, np.where(confirmed, support, 0.0),
                                         np.nan).astype(np.float32),
                f"{m}_agreement": agreement.astype(np.float32),
                f"{m}_runner_up": runner.astype(np.uint16),
                g["depth"]: depth_out.astype(np.float32),
                g["acquired_out"]: acq.astype(np.float64)}

    def reduce(self, snaps: SnapshotStack, aux: object) -> dict[str, np.ndarray]:
        valid = np.asarray(snaps.valid, dtype=bool)
        score = np.asarray(snaps.score, dtype=np.float64)
        acquired = self._layer(snaps, self.acquired)
        out: dict[str, np.ndarray] = {}
        for g in self.groups:
            out.update(self._one(snaps, g, valid, score, acquired))
        out["n_epochs"] = valid.sum(axis=0).astype(np.uint16)
        return out


__all__ = ["ConfirmedMineralVote"]
