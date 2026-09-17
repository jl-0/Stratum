"""Spectral abundance: band depth x the constituent's fraction, per epoch, before the temporal
aggregation. Closes the two things the aggregation vocabulary cannot say (04 section 5).

WHY A REDUCER AND NOT A LAYER. Two requirements from the 17 Sep 2026 tag-up are the same
requirement seen twice:

1. *"If I were going to take time into play, I would absolutely apply the mineral abundance
   calculations a priori... that takes us from a 294 binary to a 13 component continuous basis."*
2. *"Some of the samples are shared between those categories, so we just split out and we say,
   yeah, we have a detection in each one of these."*

An `Enumeration` cannot do either. It maps one raw class to exactly one product class with no
weight - `classes.py:resolve` raises `raw key N claimed by both A and B` - so there is no place
to put a fraction and no way for one constituent to reach two outputs. A Reducer can, because it
sees the whole SnapshotStack: every epoch's winning class and its band depth, before anything
collapses. That is precisely "a priori".

WHAT IT COMPUTES. For output class c, epoch e and cell:

    abundance[c, e] = depth[e] * fraction(class[e] -> c)

with `fraction` zero where the epoch's winning constituent does not contribute to c. The epochs
are then aggregated (median by default) over the epochs where the class was actually detected,
never over the zeros - averaging in the months a mineral was absent would drag every abundance
toward zero in proportion to how often the cell was looked at, which is an artefact of revisit
and not of geology.

WHAT IT IS NOT. The delivered band is an INDEX, not a mass fraction. Phil's words: *"there are
what we call spectral abundance is basically the band depth times the fraction of the sample that
we think is the component that is present there, which is a guesstimate at best, because the XRD
is not perfect."* The L3 ASA ATBD's mass-fraction model (`m4p.jl`, mean optical path length,
grain size, a quartz/feldspar term) is a further step on top of this one and is not here.
"""
from __future__ import annotations

import warnings
from collections.abc import Mapping, Sequence

import numpy as np

from stratum.types import BandSpec, SnapshotStack

#: Aggregations across epochs. `best` is not offered: the whole point is to use every epoch in
#: which the class was seen, not the single best-scoring one.
METHODS = ("median", "mean", "max", "sum")


class SpectralAbundance:
    """Per-class spectral abundance from a categorical layer and its band depth.

    Parameters
    ----------
    mineral
        The categorical layer whose winning class names a library constituent. Its product class
        table supplies the name -> id map, so `weights` is keyed by NAME and survives a
        re-delivered table whose integers moved.
    depth
        The continuous layer holding that group's band depth. Must be single-band.
    classes
        Output class names, in delivered band order. Explicit rather than inferred from
        `weights`, so a class that no constituent reaches still ships as an all-nodata band
        instead of silently vanishing from the product - and so one weights table can serve
        several runs that each deliver a different subset of it.
    weights
        `{constituent name: {output class: fraction}}`. A constituent may name SEVERAL output
        classes - that is requirement 2 - and the fractions need not sum to 1, because the rest
        of the sample is usually quartz, feldspar or something with no VSWIR expression.
    method
        How epochs collapse: median (default), mean, max or sum.
    min_epochs
        A cell needs the class detected in at least this many epochs to deliver a value. The
        analogue of `vote`'s `min_count`, and it should usually match it.
    """

    halo = 0

    def __init__(self, mineral: str, depth: str, classes: Sequence[str],
                 weights: Mapping[str, Mapping[str, float]], *, method: str = "median",
                 min_epochs: int = 1, prefix: str = "abundance") -> None:
        if method not in METHODS:
            raise ValueError(f"SpectralAbundance: method {method!r} is not one of {METHODS}")
        if min_epochs < 1:
            raise ValueError("SpectralAbundance: min_epochs must be at least 1")
        if not classes:
            raise ValueError("SpectralAbundance: name at least one output class")
        names = list(dict.fromkeys(classes))
        if len(names) != len(classes):
            raise ValueError(f"SpectralAbundance: duplicate output class(es) in {list(classes)}")
        # Weights MAY name classes this run does not deliver, and those are ignored. One
        # weights table for the whole EMIT-10 with a run that delivers two of them is the
        # expected pattern, and refusing it would force a weights file per run. The check that
        # actually catches a typo is `_lookup`'s: a CONSTITUENT name matching no class of the
        # mineral layer's own table is an error, because that is what a vintage mismatch
        # produces and a silent zero would hide it.
        for k, w in weights.items():
            bad = {c: f for c, f in w.items() if not 0.0 <= float(f) <= 1.0}
            if bad:
                raise ValueError(f"SpectralAbundance: {k!r} has fraction(s) outside [0, 1]: {bad}")
        self.mineral = mineral
        self.depth = depth
        self.classes = tuple(names)
        self.weights = {k: {c: float(f) for c, f in w.items()} for k, w in weights.items()}
        self.method = method
        self.min_epochs = int(min_epochs)
        self.prefix = prefix
        self.outputs = tuple(
            spec for name in self.classes for spec in (
                BandSpec(f"{prefix}_{name}", "float32",
                         f"{name}: {method} over epochs where it was detected of "
                         f"(band depth x XRD fraction); an INDEX, not a mass fraction",
                         units="fraction", nodata=float("nan")),
                BandSpec(f"{prefix}_{name}_n", "uint16",
                         f"{name}: epochs in which it was detected", units="count"),
            )
        ) + (
            BandSpec(f"{prefix}_total", "float32",
                     "sum of every delivered class abundance in the cell", units="fraction",
                     nodata=float("nan")),
            BandSpec("n_epochs", "uint16", "epochs with a valid observation", units="count"),
        )

    # ---------------------------------------------------------------------------------------
    def _lookup(self, snaps: SnapshotStack) -> np.ndarray:
        """(n_raw_ids, n_classes) fraction matrix, indexed by the mineral layer's product id.

        Built from the layer's OWN class table, so the manifest never names an integer. A weight
        key that matches no class in that table is an error and not a silent zero: it is almost
        always a vintage mismatch or a typo, and both look identical in the output.
        """
        spec = snaps.schema[self.mineral]
        table = spec.classes
        if table is None:
            raise KeyError(
                f"SpectralAbundance: layer {self.mineral!r} carries no class table, so a weight "
                "keyed by class name cannot be resolved. It must be a categorical layer with "
                "`classes:` (13 section 3)")
        ids = table.entries.column(table.key).to_pylist()
        names = [str(n) for n in table.entries.column("name").to_pylist()]
        by_name: dict[str, int] = {}
        for i, n in zip(ids, names, strict=True):
            by_name.setdefault(n, int(i))
        missing = [k for k in self.weights if k not in by_name]
        if missing:
            shown = ", ".join(repr(k) for k in missing[:5])
            more = f" ... and {len(missing) - 5} more" if len(missing) > 5 else ""
            raise KeyError(
                f"SpectralAbundance: {len(missing)} weight key(s) match no class of layer "
                f"{self.mineral!r} ({table.source}): {shown}{more}. The table has "
                f"{len(by_name)} classes; a name that moved means the weights were written "
                "against a different product vintage")
        lut = np.zeros((max(ids) + 1, len(self.classes)), dtype=np.float32)
        col = {c: j for j, c in enumerate(self.classes)}
        for key, w in self.weights.items():
            for c, f in w.items():
                if c in col:          # a class this run does not deliver contributes nowhere
                    lut[by_name[key], col[c]] = f
        return lut

    def reduce(self, snaps: SnapshotStack, aux: object) -> dict[str, np.ndarray]:
        declared = {layer.name for layer in snaps.schema.layers}
        missing = [n for n in (self.mineral, self.depth) if n not in declared]
        if missing:
            raise KeyError(f"SpectralAbundance needs layer(s) {missing}, which the snapshot "
                           f"schema does not declare; it has {sorted(declared)}")
        lut = self._lookup(snaps)

        ids = np.asarray(snaps[self.mineral])
        depth = np.asarray(snaps[self.depth], dtype=np.float32)
        if depth.ndim != ids.ndim:
            raise ValueError(f"SpectralAbundance: layer {self.depth!r} must be single-band to "
                             f"pair with {self.mineral!r}; got shape {depth.shape}")
        valid = np.asarray(snaps.valid, dtype=bool)
        n_epochs = valid.sum(axis=0)

        # An id past the end of the lookup contributes nothing - the same statement as a zero
        # fraction, and it keeps a nodata sentinel (65535) from indexing out of bounds.
        safe = np.where((ids >= 0) & (ids < lut.shape[0]), ids, 0).astype(np.int64)
        frac = lut[safe]                                   # (n_epochs, H, W, n_classes)
        # weight x depth, only where the epoch actually saw something
        contrib = frac * np.where(valid, depth, np.nan)[..., None]
        seen = valid[..., None] & (frac > 0) & ~np.isnan(contrib)

        out: dict[str, np.ndarray] = {}
        totals = np.zeros(ids.shape[1:], dtype=np.float32)
        any_class = np.zeros(ids.shape[1:], dtype=bool)
        # An all-NaN column is the ordinary case - a cell in which this class was never
        # detected - so numpy's RuntimeWarning for it is noise, not a signal. `est` is set to
        # NaN there either way by the `min_epochs` guard below.
        with np.errstate(invalid="ignore", divide="ignore"), \
                warnings.catch_warnings():
            warnings.filterwarnings("ignore", r"All-NaN (slice|axis) encountered",
                                    RuntimeWarning)
            warnings.filterwarnings("ignore", "Mean of empty slice", RuntimeWarning)
            for j, name in enumerate(self.classes):
                vals = np.where(seen[..., j], contrib[..., j], np.nan)
                count = seen[..., j].sum(axis=0).astype(np.uint16)
                if self.method == "median":
                    est = np.nanmedian(vals, axis=0)
                elif self.method == "mean":
                    est = np.nanmean(vals, axis=0)
                elif self.method == "max":
                    est = np.nanmax(vals, axis=0)
                else:
                    est = np.nansum(vals, axis=0)
                est = np.asarray(est, dtype=np.float32)
                est[count < self.min_epochs] = np.nan
                out[f"{self.prefix}_{name}"] = est
                out[f"{self.prefix}_{name}_n"] = count
                good = ~np.isnan(est)
                totals[good] += est[good]
                any_class |= good
        totals[~any_class] = np.nan
        out[f"{self.prefix}_total"] = totals
        out["n_epochs"] = n_epochs.astype(np.uint16)
        return out


__all__ = ["SpectralAbundance"]
