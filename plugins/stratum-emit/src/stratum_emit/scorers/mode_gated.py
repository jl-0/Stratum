"""`mode_gated_select`: categorical consensus first, then the best look that agrees with it.

A `stack` scorer (04 section 4): it sees every observation of the epoch at once, which is what a
consensus needs. With `epoch: P1Y` the whole year is one stack, the framework's argmax is the
selected scene, and the snapshot's `acquired` layer names it.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from stratum.reduce.bands import resolve_class_names
from stratum.types import AuxAccessor, BandSpec, ObsWindow

#: metric -> (default weight, default bounds, better, default aliases). Weights and bounds are
#: the external tiled aggregator's defaults (`DEFAULT_W_*` and its CLI bound defaults); metrics
#: without bounds there are already fractions, so they carry [0, 1].
METRICS: Mapping[str, tuple[float, tuple[float, float], str, tuple[str, ...]]] = {
    "bd_unc":           (0.2, (0.0, 0.03), "low", ("mineral_unc", "mineral_2_unc")),
    "fit":              (0.2, (0.0, 0.5), "high", ("mineral_fit", "mineral_2_fit")),
    "banddepth":        (0.2, (0.0, 0.5), "high", ("mineral_depth", "mineral_2_depth")),
    "cloud_prob":       (0.2, (0.0, 1.0), "low", ("cloud_prob",)),
    "scene_cloudiness": (0.2, (0.0, 1.0), "low", ()),
    "bare":             (0.2, (0.0, 1.0), "high", ("frcov",)),
    "sza":              (0.2, (0.0, 70.0), "low", ("solar_zenith",)),
    "vza":              (0.2, (0.0, 30.0), "low", ("view_zenith",)),
    "path_length":      (0.2, (402404.0, 452394.0), "low", ("path_length",)),
    "airmass":          (0.2, (2.0, 4.0), "low", ("solar_zenith", "view_zenith")),
}

#: How many names each metric's alias entry takes: the per-group metrics take [group 1, group 2],
#: airmass takes [solar zenith, view zenith], scene_cloudiness reads no band at all.
_ARITY = {"bd_unc": 2, "fit": 2, "banddepth": 2, "airmass": 2, "scene_cloudiness": 0}

GATES = ("hard", "soft")
KEYS = ("joint", "independent")
TIE_BREAKS = ("weighted", "earliest", "latest", "nodata")
NORMALIZE = ("fixed", "rank")
GROUPS = ("g1", "g2")

#: the per-cell (and one per-observation) diagnostics this scorer can deliver (04 section 4)
OUTPUTS = (
    BandSpec("mode_count", "float32", "looks whose class key equals the cell's mode; 0 with no "
             "detection to vote on", units="count", nodata=float("nan")),
    BandSpec("mode_votes", "float32", "weighted votes of the mode (a look detecting one group "
             "only votes partial_vote_weight); equals mode_count under key: independent",
             units="votes", nodata=float("nan")),
    BandSpec("n_voters", "float32", "valid looks that voted (detected something, not ignored)",
             units="count", nodata=float("nan")),
    BandSpec("n_valid", "float32", "valid looks, voters or not", units="count",
             nodata=float("nan")),
    BandSpec("agreement", "float32", "mode_count / n_valid", units="fraction",
             nodata=float("nan")),
    BandSpec("runner_up_count", "float32", "looks holding the most common key other than the "
             "mode (joint key only; NaN under key: independent)", units="count",
             nodata=float("nan")),
    BandSpec("selection_penalty", "float32", "the selected look's weighted penalty, 0 best, "
             "before any soft-gate penalty", units="fraction", nodata=float("nan")),
)


def _roles_for(weights: Mapping[str, float],
               aliases: Mapping[str, Sequence[str]]) -> tuple[str, ...]:
    """The band names the positively weighted metrics read, in metric order, once each."""
    roles: list[str] = []
    for m, v in weights.items():
        if v > 0:
            for n in aliases[m]:
                if n not in roles:
                    roles.append(n)
    return tuple(roles)


def _mean2(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """nanmean of two arrays without the all-NaN warning."""
    both = np.stack([a, b])
    n = np.isfinite(both).sum(axis=0)
    s = np.where(np.isfinite(both), both, 0.0).sum(axis=0)
    return np.divide(s, n, out=np.full(n.shape, np.nan, dtype=np.float32), where=n > 0)


def _float(x: Any) -> np.ndarray:
    return np.asarray(np.ma.filled(np.ma.asarray(x).astype(np.float32), np.nan))


def _runs(sorted_vals: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """For values sorted along axis 0: each position's run start and run end (inclusive)."""
    n = sorted_vals.shape[0]
    pos = np.arange(n).reshape((n,) + (1,) * (sorted_vals.ndim - 1))
    new = np.ones(sorted_vals.shape, dtype=bool)
    new[1:] = sorted_vals[1:] != sorted_vals[:-1]
    start = np.maximum.accumulate(np.where(new, pos, 0), axis=0)
    last = np.ones(sorted_vals.shape, dtype=bool)
    last[:-1] = new[1:]
    end = np.minimum.accumulate(np.where(last, pos, n - 1)[::-1], axis=0)[::-1]
    return start, end


def tally(code: np.ndarray, weight: np.ndarray, voters: np.ndarray
          ) -> tuple[np.ndarray, np.ndarray]:
    """Per row, the summed `weight` and the count of voter rows sharing its `code`, in the same
    cell. (N, H, W) in, (N, H, W) out; 0 for a non-voter row. Sort-based, O(N log N) a cell."""
    sentinel = np.iinfo(np.int64).max
    keyed = np.where(voters, code, sentinel)
    order = np.argsort(keyed, axis=0, kind="stable")
    s = np.take_along_axis(keyed, order, axis=0)
    w = np.take_along_axis(np.where(voters, weight, 0.0), order, axis=0).astype(np.float64)
    c = np.take_along_axis(voters, order, axis=0).astype(np.float64)
    start, end = _runs(s)
    out = []
    for vals in (w, c):
        cs = np.cumsum(vals, axis=0)
        before = np.take_along_axis(cs, np.maximum(start - 1, 0), axis=0) * (start > 0)
        total = np.take_along_axis(cs, end, axis=0) - before
        back = np.empty_like(total)
        np.put_along_axis(back, order, total, axis=0)
        out.append(np.where(voters, back, 0.0))
    return out[0], out[1]


def rank_penalty(raw: np.ndarray, feasible: np.ndarray, better: str) -> np.ndarray:
    """Per cell, each feasible look's average rank among the feasible looks, scaled to [0, 1]
    with 0 the best; 1.0 for an infeasible look; 0 when only one look is feasible. Depends on
    the looks of the cell alone, so it is block-invariant (unlike a window min-max)."""
    key = np.where(feasible, raw if better == "low" else -raw, np.inf).astype(np.float64)
    order = np.argsort(key, axis=0, kind="stable")
    s = np.take_along_axis(key, order, axis=0)
    start, end = _runs(s)
    avg = np.empty(s.shape, dtype=np.float64)
    np.put_along_axis(avg, order, (start + end) / 2.0, axis=0)
    n_f = feasible.sum(axis=0)
    scale = np.maximum(n_f - 1, 1)
    pen = np.where(n_f > 1, avg / scale, 0.0)
    return np.where(feasible, pen, 1.0).astype(np.float32)


class ModeGatedSelect:
    """Pick, per cell, the best look among those that agree with the cell's mineral consensus.

    A `stack` scorer: `score` sees every valid look of the epoch at a cell and returns, per look,
    minus its weighted penalty - or NaN where the gate excludes it. With one epoch per year the
    framework's argmax is the selected scene for the year.

    1. **Key.** Each look's (group 1, group 2) class pair from two categorical snapshot layers
       (`layers`, product ids via `obs.layer`). `g2_mask_classes` first turns group 2 into `none`
       where it is a listed class - group 1 untouched - so, for example, a dry-vegetation
       detection in group 2 stops deciding the key without discarding the look's group 1. This
       rewrites the KEY only: the snapshot's group-2 layer is gathered from the winning look and
       still holds its raw class (scorer outputs are continuous, 13 section 2, so the scorer
       cannot deliver a rewritten categorical). To keep a masked class out of the product, list
       the same names in that layer's `aggregate.ignore`; such a cell then delivers nodata in
       group 2, not `none`.
    2. **Vote.** A valid look votes when it detects something in either group and neither group
       holds a class in its `ignore` list (an ignored look is still counted in `n_valid`, as
       `vote` counts it). Under `key: joint` a look that detects in one group only votes
       `partial_vote_weight`; under `key: independent` each group is voted on its own.
    3. **Mode.** The key with the most votes. Ties: `weighted` (default) takes the key of the
       tied look with the lowest penalty plus `partial_tie_penalty` if it is partial; `earliest`
       / `latest` the first / last tied look; `nodata` leaves the cell unoccupied. A mode held by
       fewer than `min_count` looks leaves the cell unoccupied. No voter at all: no gate.
    4. **Gate.** `hard`: a look whose key differs from the mode may not occupy the cell (NaN).
       `soft`: it is ranked with `soft_penalty` added instead. A look holding an `ignore`d class
       never matches - also where the cell has no mode - so under the hard gate an ignored class
       is never delivered, whether or not some other look detected anything.
    5. **Penalty.** Ten metrics, each mapped to [0, 1] with 0 best - `normalize: fixed` clips
       to `bounds`, `normalize: rank` uses the look's rank among the cell's valid looks - and
       combined as a weighted sum with the positive weights renormalised to sum to 1. A metric
       the look cannot supply scores the worst, 1.0 - but only where it is DERIVED missing:
       band-depth uncertainty and fit of a group that detected nothing (the product writes 0
       there, not fill), an airmass at a grazing angle (cos <= 1e-3), a granule without
       `cloud_fraction`. Fill in a band a weighted metric reads is not a missing metric: it
       makes the look invalid (11 section 2), so it neither votes nor wins - see below.

    Defaults are the external tiled aggregator's. Deliberate differences from it:

    - Fixed bounds or per-cell rank only, never a min-max over a tile window: a window statistic
      depends on `block_size` and would break block-wise = tile-wise. With its default bounds the
      result is the same as its default run.
    - `scene_cloudiness` is the granule's `cloud_fraction` (the catalogue's whole-scene value),
      not the cloudy share of the scene inside one tile; `None` scores 1.0.
    - Cloud and low illumination are pixel masks (`l2a_standard`, `cos_i`), not terms inside
      the selector; a masked look neither votes nor wins, as there.
    - `weighted` combination only (no Borda/Copeland); no `top_k`/`mode_k` composites.
    - The final tie between equally scored looks goes to the earliest observation (the
      framework's rule), not the lowest scene index.
    - Every band a positively weighted metric reads is a required role, and required roles
      decide validity: fill in it (FRCOV is fill wherever its QC flags cloud, urban, water or
      snow/ice) removes the look from the VOTE as well as from selection, and the planner drops
      a granule that has no asset for it. The external tool keeps such a look as a voter and
      scores the metric 1.0, so the mode can differ. `weights: {bare: 0}` restores its voter
      set for the cover fraction (the metric and its band are then not read at all).

    Metrics and the names they read (override with `aliases`): `bd_unc` [mineral_unc,
    mineral_2_unc], `fit` [mineral_fit, mineral_2_fit], `banddepth` [mineral_depth,
    mineral_2_depth], `cloud_prob` [cloud_prob], `bare` [frcov], `sza` [solar_zenith], `vza`
    [view_zenith], `path_length` [path_length], `airmass` [solar_zenith, view_zenith],
    `scene_cloudiness` (none). Every metric with a positive weight makes its names required, so a
    manifest lacking one fails `stratum validate`; set its weight to 0 instead. **V001 has no
    cloud-probability band: set `weights: {cloud_prob: 0}`.**

    Emitted outputs (`source: scorer.<name>`): `mode_count`, `mode_votes`, `n_voters`,
    `n_valid`, `agreement` (mode_count / n_valid), `runner_up_count` (per cell) and
    `selection_penalty` (per observation; the winner's is kept).
    """

    capability = "stack"
    halo = 0
    required_aux: tuple[str, ...] = ()
    # class-level defaults, so `stratum plugins show` reports what the default parameters
    # declare; __init__ replaces them from the parameters given
    outputs: tuple[BandSpec, ...] = OUTPUTS
    required_layers: tuple[str, ...] = ("mineral_1", "mineral_2")
    required_roles: tuple[str, ...] = _roles_for({m: s[0] for m, s in METRICS.items()},
                                                 {m: s[3] for m, s in METRICS.items()})

    def __init__(self, layers: Sequence[str] = ("mineral_1", "mineral_2"),
                 key: str = "joint",
                 ignore: Mapping[str, Sequence[str]] | None = None,
                 g2_mask_classes: Sequence[str] = (),
                 min_count: int = 1,
                 tie_break: str = "weighted",
                 gate: str = "hard",
                 soft_penalty: float = 10.0,
                 partial_vote_weight: float = 0.01,
                 partial_tie_penalty: float = 0.05,
                 normalize: str = "fixed",
                 weights: Mapping[str, float] | None = None,
                 bounds: Mapping[str, Sequence[float]] | None = None,
                 aliases: Mapping[str, str | Sequence[str]] | None = None) -> None:
        name = "ModeGatedSelect"
        layers = tuple(layers)
        if len(layers) != 2 or layers[0] == layers[1]:
            raise ValueError(f"{name}: layers names two different categorical snapshot layers "
                             f"(group 1, group 2); got {list(layers)}")
        for label, value, allowed in (("key", key, KEYS), ("gate", gate, GATES),
                                      ("tie_break", tie_break, TIE_BREAKS),
                                      ("normalize", normalize, NORMALIZE)):
            if value not in allowed:
                raise ValueError(f"{name}: {label} must be one of {list(allowed)}; got {value!r}")
        ignore = dict(ignore or {})
        bad = [g for g in ignore if g not in GROUPS]
        if bad:
            raise ValueError(f"{name}: ignore is keyed by group, {list(GROUPS)}; got {bad}")
        for group, names in (*ignore.items(), ("g2_mask_classes", g2_mask_classes)):
            if isinstance(names, str) or not all(isinstance(n, str) for n in names):
                raise ValueError(f"{name}: {group} must be a list of class NAMES (never ids); "
                                 f"got {names!r}")
        if int(min_count) < 1:
            raise ValueError(f"{name}: min_count must be at least 1; got {min_count}")
        if soft_penalty < 0 or partial_tie_penalty < 0:
            raise ValueError(f"{name}: soft_penalty and partial_tie_penalty must not be negative")
        if not 0.0 <= partial_vote_weight <= 1.0:
            raise ValueError(f"{name}: partial_vote_weight must be in [0, 1]; "
                             f"got {partial_vote_weight}")

        w = {m: spec[0] for m, spec in METRICS.items()}
        b = {m: spec[1] for m, spec in METRICS.items()}
        a = {m: spec[3] for m, spec in METRICS.items()}
        for label, given in (("weights", weights), ("bounds", bounds), ("aliases", aliases)):
            unknown = sorted(set(given or {}) - set(METRICS))
            if unknown:
                raise ValueError(f"{name}: unknown metric(s) {unknown} in {label}; metrics are "
                                 f"{sorted(METRICS)}")
        for m, v in (weights or {}).items():
            if float(v) < 0:
                raise ValueError(f"{name}: weight {m!r} is negative ({v})")
            w[m] = float(v)
        total = sum(w.values())
        if total <= 0:
            raise ValueError(f"{name}: every metric weight is zero; nothing would rank the looks")
        for m, v in (bounds or {}).items():
            lo, hi = (float(x) for x in v)
            if not lo < hi:
                raise ValueError(f"{name}: bounds {m!r} must be [lo, hi] with lo < hi; got {v}")
            b[m] = (lo, hi)
        for m, v in (aliases or {}).items():
            names = (v,) if isinstance(v, str) else tuple(v)
            if len(names) != _ARITY.get(m, 1):
                raise ValueError(f"{name}: aliases {m!r} takes {_ARITY.get(m, 1)} name(s); "
                                 f"got {list(names)}")
            a[m] = names

        self.layers = layers
        self.key = key
        self.ignore = {g: tuple(ignore.get(g, ())) for g in GROUPS}
        self.g2_mask_classes = tuple(g2_mask_classes)
        self.min_count = int(min_count)
        self.tie_break = tie_break
        self.gate = gate
        self.soft_penalty = float(soft_penalty)
        self.partial_vote_weight = float(partial_vote_weight)
        self.partial_tie_penalty = float(partial_tie_penalty)
        self.normalize = normalize
        #: the positive weights renormalised to sum to 1; zero-weight metrics are not computed
        self.weights = {m: v / total for m, v in w.items() if v > 0}
        self.bounds = b
        self.aliases = a
        self.required_roles = _roles_for(self.weights, a)
        self.required_layers = layers
        self.outputs = OUTPUTS
        self._memo: tuple[int, dict[str, np.ndarray]] | None = None
        self._ids: dict[str, np.ndarray] = {}

    # ------------------------------------------------------------------------- class names
    def _class_ids(self, obs: ObsWindow, layer: str, names: Sequence[str]) -> np.ndarray:
        """Names -> product ids through the layer's own class table (13 section 3), cached per
        table fingerprint: a name that is not in the table fails loudly, never silently."""
        if not names:
            return np.zeros(0, dtype=np.int64)
        table = obs.layer_classes(layer)
        cache = f"{layer}|{table.fingerprint()}|{','.join(names)}"
        if cache not in self._ids:
            self._ids[cache] = resolve_class_names(table, names)
        return self._ids[cache]

    # ----------------------------------------------------------------------------- metrics
    def _raw(self, metric: str, o: ObsWindow, g1: np.ndarray, g2: np.ndarray) -> np.ndarray:
        """One look's raw value of `metric`, (H, W) float32, NaN where missing."""
        names = self.aliases[metric]
        if metric == "scene_cloudiness":
            cf = o.granule.cloud_fraction if o.granule is not None else None
            return np.full(o.valid.shape, np.nan if cf is None else float(cf), np.float32)
        if metric in ("bd_unc", "fit", "banddepth"):
            a, b = _float(o[names[0]]), _float(o[names[1]])
            if metric != "banddepth":
                # MINUNCERT writes 0, not fill, where a group detected nothing
                a = np.where(g1 > 0, a, np.nan)
                b = np.where(g2 > 0, b, np.nan)
            return np.clip(_mean2(a, b), 0.0, 1.0)
        if metric in ("cloud_prob", "bare"):
            return np.clip(_float(o[names[0]]), 0.0, 1.0)
        if metric == "vza":
            return np.clip(_float(o[names[0]]), 0.0, 90.0)
        if metric == "airmass":
            mu0 = np.cos(np.deg2rad(np.clip(_float(o[names[0]]), 0.0, 90.0)))
            muv = np.cos(np.deg2rad(np.clip(_float(o[names[1]]), 0.0, 90.0)))
            mu0 = np.where(mu0 <= 1.0e-3, np.nan, mu0)
            muv = np.where(muv <= 1.0e-3, np.nan, muv)
            return (1.0 / mu0 + 1.0 / muv).astype(np.float32)
        return _float(o[names[0]])                       # sza, path_length

    def penalties(self, obs: ObsWindow, g1: np.ndarray, g2: np.ndarray
                  ) -> dict[str, np.ndarray]:
        """Each weighted metric's (N, H, W) penalty in [0, 1], 0 best and 1.0 where missing.
        Raw values are computed one look at a time (`for i, o in enumerate(obs)`)."""
        out: dict[str, np.ndarray] = {}
        valid = np.asarray(obs.valid, dtype=bool)
        for metric in self.weights:
            raw = np.stack([self._raw(metric, o, g1[i], g2[i]) for i, o in enumerate(obs)])
            better = METRICS[metric][2]
            lo, hi = self.bounds[metric]
            feasible = valid & np.isfinite(raw)
            if self.normalize == "rank":
                out[metric] = rank_penalty(raw, feasible, better)
                continue
            with np.errstate(invalid="ignore"):
                s = np.clip((raw - lo) / (hi - lo), 0.0, 1.0)
            pen = s if better == "low" else 1.0 - s
            out[metric] = np.where(feasible, pen, 1.0).astype(np.float32)
        return out

    def penalty(self, obs: ObsWindow, g1: np.ndarray, g2: np.ndarray) -> np.ndarray:
        """The combined (N, H, W) penalty: sum of weight x metric penalty, in [0, 1]."""
        total = np.zeros(np.shape(obs.valid), dtype=np.float32)
        for metric, pen in self.penalties(obs, g1, g2).items():
            total += np.float32(self.weights[metric]) * pen
        return total

    # ------------------------------------------------------------------------------- mode
    def _mode(self, code: np.ndarray, weight: np.ndarray, voters: np.ndarray,
              tie_score: np.ndarray) -> dict[str, np.ndarray]:
        """The mode of `code` over `voters` per cell: its code, weighted votes and look count,
        whether the cell has one (`has`), whether it is suppressed (`drop`: min_count or a
        `nodata` tie) and the runner-up count."""
        votes, counts = tally(code, weight, voters)
        has = voters.any(axis=0)
        best = np.where(voters, votes, -np.inf).max(axis=0)
        top = voters & np.isclose(votes, best[None], atol=1e-7, rtol=0.0)
        if self.tie_break == "latest":
            n = code.shape[0]
            row = n - 1 - np.argmax(top[::-1], axis=0)
        elif self.tie_break == "weighted":
            row = np.argmin(np.where(top, tie_score, np.inf), axis=0)
        else:                                            # earliest, nodata
            row = np.argmax(top, axis=0)
        pick = row[None]
        mode = np.take_along_axis(code, pick, axis=0)[0]
        mode_votes = np.where(has, np.take_along_axis(votes, pick, axis=0)[0], 0.0)
        mode_count = np.where(has, np.take_along_axis(counts, pick, axis=0)[0], 0.0)
        drop = has & (mode_count < self.min_count)
        if self.tie_break == "nodata":
            sentinel = np.iinfo(np.int64).max
            lo = np.where(top, code, sentinel).min(axis=0)
            hi = np.where(top, code, -sentinel).max(axis=0)
            drop |= has & (lo != hi)
        runner = np.where(voters & (code != mode[None]), counts, 0.0).max(axis=0)
        return {"mode": mode, "has": has, "drop": drop, "votes": mode_votes,
                "count": mode_count, "runner_up": runner}

    # ------------------------------------------------------------------------------- score
    def score(self, obs: ObsWindow, aux: AuxAccessor) -> np.ndarray:
        valid = np.asarray(obs.valid, dtype=bool)
        lay1, lay2 = self.layers
        g1 = np.asarray(obs.layer(lay1), dtype=np.int64)
        g2 = np.asarray(obs.layer(lay2), dtype=np.int64)
        if g1.ndim != 3:
            raise ValueError("mode_gated_select is a stack scorer and needs the stacked window "
                             "(04 section 4)")
        # invalid looks carry whatever the gather left; they never vote or match
        g1 = np.where(valid, np.maximum(g1, 0), 0)
        g2 = np.where(valid, np.maximum(g2, 0), 0)
        masked = self._class_ids(obs, lay2, self.g2_mask_classes)
        if masked.size:
            g2 = np.where(np.isin(g2, masked), 0, g2)
        ignored = (np.isin(g1, self._class_ids(obs, lay1, [n for n in self.ignore["g1"]
                                                           if n != "none"]))
                   | np.isin(g2, self._class_ids(obs, lay2, [n for n in self.ignore["g2"]
                                                             if n != "none"])))
        detect1, detect2 = g1 > 0, g2 > 0
        P = self.penalty(obs, g1, g2)
        n_valid = valid.sum(axis=0).astype(np.float32)

        if self.key == "joint":
            voters = valid & (detect1 | detect2) & ~ignored
            partial = detect1 ^ detect2
            code = (g1 << 32) | (g2 & 0xFFFFFFFF)
            weight = np.where(partial, self.partial_vote_weight, 1.0)
            m = self._mode(code, weight, voters, P + self.partial_tie_penalty * partial)
            match = ((code == m["mode"][None]) | ~m["has"][None]) & ~ignored
            has, drop = m["has"], m["drop"]
            n_voters = voters.sum(axis=0).astype(np.float32)
            mode_count = np.where(has, m["count"], 0.0)
            mode_votes = m["votes"]
            runner_up = m["runner_up"].astype(np.float32)
        else:
            match = np.ones(valid.shape, dtype=bool)
            has = np.zeros(valid.shape[1:], dtype=bool)
            drop = np.zeros(valid.shape[1:], dtype=bool)
            voters_any = np.zeros(valid.shape, dtype=bool)
            for g, det in ((g1, detect1), (g2, detect2)):
                voters = valid & det & ~ignored
                voters_any |= voters
                m = self._mode(g, np.ones(g.shape), voters, P)
                match &= (g == m["mode"][None]) | ~m["has"][None]
                has |= m["has"]
                drop |= m["drop"]
            match &= ~ignored
            n_voters = voters_any.sum(axis=0).astype(np.float32)
            # the looks carrying BOTH groups' modes: what the gate keeps
            mode_count = np.where(has, (valid & match).sum(axis=0), 0.0)
            mode_votes = mode_count
            runner_up = np.full(n_valid.shape, np.nan, dtype=np.float32)

        if self.gate == "hard":
            s = np.where(match, -P, np.nan)
        else:
            s = np.where(match, -P, -(P + np.float32(self.soft_penalty)))
        s = np.where(valid & ~drop[None], s, np.nan).astype(np.float32)

        with np.errstate(invalid="ignore", divide="ignore"):
            agreement = np.where(n_valid > 0, mode_count / n_valid, np.nan)
        self._memo = (id(obs), {
            "mode_count": np.asarray(mode_count, dtype=np.float32),
            "mode_votes": np.asarray(mode_votes, dtype=np.float32),
            "n_voters": n_voters,
            "n_valid": n_valid,
            "agreement": np.asarray(agreement, dtype=np.float32),
            "runner_up_count": runner_up,
            "selection_penalty": P,
        })
        return s

    def emit(self, obs: ObsWindow, aux: AuxAccessor) -> dict[str, np.ndarray]:
        """The diagnostics `score` computed for this same `obs` (04 section 4: emit runs right
        after score with the same window, so nothing is computed twice)."""
        if self._memo is None or self._memo[0] != id(obs):
            self.score(obs, aux)
        assert self._memo is not None
        out = self._memo[1]
        self._memo = None
        return out


__all__ = ["METRICS", "ModeGatedSelect", "rank_penalty", "tally"]
