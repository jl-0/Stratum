"""The built-in schema-driven reducer: 13 section 4 executed exactly (04 section 5).

Every kernel here is vectorized over (H, W). The only Python loops are over the epoch axis or
over the class ids present in a block. Every aggregation masks on `valid` before it looks at a
value; that is a framework guarantee, not a plugin responsibility.

Conventions on the way out (first-slice plan section 4; 11 section 2): categorical bands are
uint16 with nodata 65535 and 0 the real `none` class; continuous bands float32 with NaN nodata;
counts uint16 with no nodata. Over a single epoch every aggregation is the identity.
"""
from __future__ import annotations

import warnings
from collections.abc import Sequence

import numpy as np

from stratum.reduce.bands import (
    CATEGORICAL_NODATA,
    BestParams,
    ContinuousParams,
    SchemaError,
    VoteParams,
    delivered_bands,
    parse_schema,
    resolve_class_names,
)
from stratum.types import Epoch, LayerSpec, SnapshotStack

SCORE_WEIGHT_EPS = np.float32(1e-6)
"""`score_weighted` uses weights = score - min(score over the aggregated epochs) + eps, because a
score is only an ordering (MinViewZenith is -zenith, so every score is negative) and a raw score
cannot be a weight. The lowest-scoring epoch gets weight eps, and equal scores reduce to a mean."""

_NODATA = np.uint16(CATEGORICAL_NODATA)


# ------------------------------------------------------------------------------------- entry point
def reduce_stack(snaps: SnapshotStack) -> dict[str, np.ndarray]:
    """Reduce one snapshot stack into the bands `delivered_bands(snaps.schema)` names.

    Categorical layers are reduced first because continuous layers may be `conditional_on` one of
    them. Epochs are processed in `snaps.epochs` ascending order whatever order the arrays arrived
    in, so `tie_break: earliest | latest` is well defined. Returns arrays of shape (H, W), or
    (H, W, B) for a continuous layer that carries a band axis.
    """
    schema = snaps.schema
    parsed = parse_schema(schema)
    order = _epoch_order(snaps.epochs)
    valid = np.asarray(snaps.valid, dtype=bool)
    if valid.ndim != 3:
        raise ValueError(f"valid must be (n_epochs, H, W); got shape {valid.shape}")
    if valid.shape[0] != len(snaps.epochs):
        raise ValueError(f"valid has {valid.shape[0]} epochs; snaps.epochs has {len(snaps.epochs)}")
    valid = valid[order]
    score = np.asarray(snaps.score, dtype=np.float32)
    if score.shape != valid.shape:
        raise ValueError(f"score shape {score.shape} != valid shape {valid.shape}")
    score = score[order]
    n_epochs = valid.sum(axis=0)

    out: dict[str, np.ndarray] = {}
    for layer in schema.layers:
        params = parsed[layer.name]
        if layer.kind != "categorical" or params is None:
            continue
        vals, extra = _layer_values(snaps, layer, order, valid.shape)
        if vals.ndim != 3:
            raise ValueError(f"categorical layer {layer.name!r} must be single-band (13 section 2)")
        if (vals[extra] < 0).any():
            raise ValueError(f"categorical layer {layer.name!r} holds negative values under valid; "
                             "sentinels must already be masked (11 section 2)")
        if isinstance(params, VoteParams):
            out.update(vote(layer, vals, extra, score, n_epochs, params))
        else:
            assert isinstance(params, BestParams)
            out[layer.name] = best_categorical(vals, extra, score)

    for layer in schema.layers:
        params = parsed[layer.name]
        if layer.kind != "continuous" or params is None:
            continue
        assert isinstance(params, ContinuousParams)
        vals, sel = _layer_values(snaps, layer, order, valid.shape)
        if params.conditional_on:
            winner = out[params.conditional_on]
            cond, cond_ok = _layer_values(snaps, schema[params.conditional_on], order, valid.shape)
            sel = sel & cond_ok & (cond == winner[None]) & (winner != _NODATA)[None]
        unc = None
        if params.unc:
            unc, unc_ok = _layer_values(snaps, schema[params.unc], order, valid.shape)
            unc = np.where(_match_axes(unc_ok, unc), unc, np.nan)
        out.update(continuous(layer, vals, sel, score, unc, params))

    out["n_epochs"] = n_epochs.astype(np.uint16)
    expected = [b.name for b in delivered_bands(schema)]
    if set(out) != set(expected):
        raise AssertionError(f"reducer delivered {sorted(out)}, schema promises {sorted(expected)}")
    return {name: out[name] for name in expected}   # schema order, n_epochs last


def band_counts(snaps: SnapshotStack) -> dict[str, int]:
    """Actual band count per continuous layer, for `delivered_bands(..., band_counts=...)`."""
    counts: dict[str, int] = {}
    for layer in snaps.schema.layers:
        if layer.kind == "continuous":
            arr = np.asarray(snaps[layer.name])
            counts[layer.name] = int(arr.shape[3]) if arr.ndim == 4 else 1
    return counts


# ---------------------------------------------------------------------------------------- helpers
def _epoch_order(epochs: Sequence[Epoch]) -> np.ndarray:
    """Permutation putting epochs in ascending start order (11 section 8)."""
    starts = [e.start for e in epochs]
    return np.asarray(sorted(range(len(epochs)), key=lambda i: starts[i]), dtype=np.intp)


def _layer_values(snaps: SnapshotStack, layer: LayerSpec, order: np.ndarray,
                  shape: tuple[int, ...]) -> tuple[np.ndarray, np.ndarray]:
    """A layer's array in epoch order plus the epochs it is usable in: `valid`, further narrowed
    by the layer's own mask when it arrived as a masked array (11 section 2)."""
    raw = snaps[layer.name]
    data = np.asarray(np.ma.getdata(raw))
    if data.ndim not in (3, 4) or data.shape[:3] != shape:
        raise ValueError(f"layer {layer.name!r} has shape {data.shape}; expected {shape} "
                         f"or {shape + ('B',)}")
    data = data[order]
    usable = np.asarray(snaps.valid, dtype=bool)[order]
    if isinstance(raw, np.ma.MaskedArray):
        mask = np.ma.getmaskarray(raw)[order]
        if mask.ndim == 4:
            mask = mask.any(axis=-1)
        usable = usable & ~mask
    return data, usable


def _match_axes(mask: np.ndarray, arr: np.ndarray) -> np.ndarray:
    """Broadcast an (n, H, W) mask against an array that may carry a band axis."""
    return mask[..., None] if arr.ndim == 4 else mask


def _pick(counts: np.ndarray, keys: np.ndarray, top: np.ndarray, class_ids: np.ndarray,
          tie_break: str) -> np.ndarray:
    """The class holding `top` votes at each cell, ties broken by the highest `keys`.

    Equal keys fall to the lowest class id (`argmax` takes the first), which keeps the choice
    deterministic. `tie_break: nodata` refuses wherever more than one class holds `top`.
    Returns (H, W) uint16 with nodata where top == 0.
    """
    hw = top.shape
    if counts.shape[0] == 0:
        return np.full(hw, _NODATA, dtype=np.uint16)
    is_top = (counts == top[None]) & (top[None] > 0)
    ranked = np.where(is_top, keys, -np.inf)
    idx = ranked.argmax(axis=0)
    chosen = class_ids[idx].astype(np.uint16)
    chosen[top == 0] = _NODATA
    if tie_break == "nodata":
        chosen[is_top.sum(axis=0) > 1] = _NODATA
    return chosen


# ------------------------------------------------------------------------------------ categorical
def vote(layer: LayerSpec, vals: np.ndarray, valid: np.ndarray, score: np.ndarray,
         n_epochs: np.ndarray, params: VoteParams) -> dict[str, np.ndarray]:
    """Mode through time, in the decision order of 04 section 5.

    1. Invalid epochs are discarded; the remainder is `n_epochs` (computed by the caller).
    2. Ignored classes (by name, `none` = 0) leave the tally but stay in `n_epochs`.
    3. The modal class is taken.
    4. It is suppressed to nodata where its count is below `min_count`.
    5. Ties fall to `tie_break`: earliest / latest by epoch order, highest_score by the single
       highest score any tied class holds, nodata refuses.

    `agreement` = modal count / n_epochs (NaN where n_epochs is 0; 0.0 where every valid epoch was
    ignored, in which case the layer is nodata). It is reported even where the layer is suppressed
    by `min_count` or a refused tie, because it describes the tally, not the decision.
    `runner_up` is the second-most-frequent class under the same tie rule, nodata where the layer
    is nodata or no second class exists.
    """
    n, h, w = vals.shape
    ignore_ids = resolve_class_names(layer.classes, params.ignore)
    counted = valid & ~np.isin(vals, ignore_ids)
    class_ids = np.unique(vals[counted])
    c = class_ids.shape[0]

    counts = np.zeros((c, h, w), dtype=np.uint16)
    keys = np.zeros((c, h, w), dtype=np.float32)
    epoch_idx = np.arange(n, dtype=np.float32)[:, None, None]
    score_key = np.where(counted & ~np.isnan(score), score, -np.inf).astype(np.float32)
    for i, cls in enumerate(class_ids):
        hit = counted & (vals == cls)
        counts[i] = hit.sum(axis=0)
        if params.tie_break == "earliest":
            keys[i] = -np.where(hit, epoch_idx, n).min(axis=0)      # earliest epoch ranks highest
        elif params.tie_break == "latest":
            keys[i] = np.where(hit, epoch_idx, -1).max(axis=0)
        elif params.tie_break == "highest_score":
            keys[i] = np.where(hit, score_key, -np.inf).max(axis=0)

    modal = counts.max(axis=0) if c else np.zeros((h, w), dtype=np.uint16)
    winner = _pick(counts, keys, modal, class_ids, params.tie_break)
    winner[modal < params.min_count] = _NODATA

    with np.errstate(divide="ignore", invalid="ignore"):
        agreement = np.where(n_epochs > 0, modal / n_epochs, np.nan).astype(np.float32)

    is_winner = class_ids[:, None, None] == winner[None].astype(np.int64)
    rest = np.where(is_winner, 0, counts)
    second = rest.max(axis=0) if c else np.zeros((h, w), dtype=np.uint16)
    runner_up = _pick(rest, keys, second, class_ids, params.tie_break)
    runner_up[winner == _NODATA] = _NODATA

    return {layer.name: winner, f"{layer.name}_agreement": agreement,
            f"{layer.name}_runner_up": runner_up}


def best_categorical(vals: np.ndarray, valid: np.ndarray, score: np.ndarray) -> np.ndarray:
    """The class from the highest-scoring valid epoch; nodata where no valid epoch has a score."""
    usable = valid & ~np.isnan(score)
    idx = np.where(usable, score, -np.inf).argmax(axis=0)
    chosen = np.take_along_axis(vals, idx[None], axis=0)[0].astype(np.uint16)
    chosen[~usable.any(axis=0)] = _NODATA
    return chosen


# ------------------------------------------------------------------------------------- continuous
def continuous(layer: LayerSpec, vals: np.ndarray, sel: np.ndarray, score: np.ndarray,
               unc: np.ndarray | None, params: ContinuousParams) -> dict[str, np.ndarray]:
    """One continuous layer, band-wise over (n_epochs, H, W[, B]) (13 section 4).

    `sel` is the epoch set to aggregate: valid, narrowed to concordant epochs when
    `conditional_on` is set. Non-finite values inside a selected epoch are skipped by every
    statistic (nan-aware reductions) but still count toward `<layer>_n`; `inverse_variance`
    additionally drops epochs whose uncertainty is NaN or <= 0 from both estimate and count.
    Cells with nothing to aggregate deliver NaN and a count of 0.
    """
    squeeze = vals.ndim == 3
    x = vals.astype(np.float32, copy=False)
    if squeeze:
        x = x[..., None]
    sel4 = sel[..., None]
    xm = np.where(sel4, x, np.nan).astype(np.float32)
    n_sel = sel.sum(axis=0)
    method = params.method

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)   # all-NaN slices are the nodata case
        if method == "median":
            est = np.nanmedian(xm, axis=0)
        elif method == "mean":
            est = np.nanmean(xm, axis=0)
        elif method == "min":
            est = np.nanmin(xm, axis=0)
        elif method == "max":
            est = np.nanmax(xm, axis=0)
        elif method == "percentile":
            est = np.nanpercentile(xm, params.p, axis=0)
        elif method == "score_weighted":
            s = np.where(sel, score, np.nan)
            smin = np.nanmin(s, axis=0)
            w = np.where(sel, s - smin[None] + SCORE_WEIGHT_EPS, 0.0).astype(np.float32)[..., None]
            finite = np.isfinite(xm)
            num = np.nansum(w * xm, axis=0)
            den = (w * finite).sum(axis=0)
            est = np.where(den > 0, num / den, np.nan)
        elif method == "inverse_variance":
            assert unc is not None
            u = unc.astype(np.float32, copy=False)
            if u.ndim == 3:
                u = u[..., None]
            ok = sel4 & np.isfinite(u) & (u > 0) & np.isfinite(xm)
            w = np.where(ok, 1.0 / np.square(np.where(ok, u, 1.0)), 0.0).astype(np.float32)
            num = (w * np.where(ok, xm, 0.0)).sum(axis=0)
            den = w.sum(axis=0)
            est = np.where(den > 0, num / den, np.nan)
            n_sel = (sel & ok.any(axis=-1)).sum(axis=0)
        elif method == "best":
            usable = sel & ~np.isnan(score)
            idx = np.where(usable, score, -np.inf).argmax(axis=0)
            idx4 = np.broadcast_to(idx[None, ..., None], (1,) + x.shape[1:])
            est = np.take_along_axis(x, idx4, axis=0)[0].astype(np.float32)
            est = np.where(usable.any(axis=0)[..., None], est, np.nan)
        else:  # pragma: no cover - parse_schema refuses anything else
            raise SchemaError(f"unknown continuous method {method!r}")

        est = est.astype(np.float32)
        est[n_sel == 0] = np.nan

        out: dict[str, np.ndarray] = {}
        out[layer.name] = est[..., 0] if squeeze else est
        out[f"{layer.name}_n"] = n_sel.astype(np.uint16)
        if params.spread:
            if params.spread == "std":
                spread = np.nanstd(xm, axis=0)
            else:
                spread = np.nanpercentile(xm, 75, axis=0) - np.nanpercentile(xm, 25, axis=0)
            spread = spread.astype(np.float32)
            spread[n_sel == 0] = np.nan
            out[f"{layer.name}_spread"] = spread[..., 0] if squeeze else spread
    return out
