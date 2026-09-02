"""Schema validation and delivered-band derivation for the built-in reducer (13 section 4).

Everything the reduce stage delivers is derived from the snapshot schema without running anything,
which is what lets a bad declaration fail at plan time (04 section 8). The aggregation vocabulary
is parsed here once, into frozen parameter objects, and `builtin.reduce_stack` executes them.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from stratum.types import BandSpec, ClassTable, LayerSpec, SnapshotSchema

# Delivered dtypes and nodata (first-slice plan section 4; 11 section 2).
CATEGORICAL_DTYPE = "uint16"
CATEGORICAL_NODATA = 65535        # 0 is the `none` class and is real data
CONTINUOUS_DTYPE = "float32"      # nodata NaN
COUNT_DTYPE = "uint16"            # no nodata; 0 is a real count

NONE_CLASS = 0                    # reserved product id (13 section 3)
NONE_NAME = "none"

CATEGORICAL_METHODS = ("vote", "best", "none")
CONTINUOUS_METHODS = ("median", "mean", "min", "max", "percentile", "score_weighted",
                      "inverse_variance", "best", "none")
TIE_BREAKS = ("earliest", "latest", "highest_score", "nodata")
SPREADS = ("std", "iqr")

DEFAULT_MIN_COUNT = 1             # a single vote counts; the single-epoch identity needs this
DEFAULT_TIE_BREAK = "nodata"      # refuse to choose unless the schema says how

_VOTE_PARAMS = frozenset({"min_count", "ignore", "tie_break"})
_CONTINUOUS_PARAMS = frozenset({"conditional_on", "spread"})


class SchemaError(ValueError):
    """An aggregation the vocabulary of 13 section 4 cannot execute."""


@dataclass(frozen=True)
class VoteParams:
    """`vote` parameters after parsing (04 section 5). `ignore` holds class NAMES; ids are
    resolved against the layer's class table at reduce time."""

    min_count: int = DEFAULT_MIN_COUNT
    ignore: tuple[str, ...] = ()
    tie_break: str = DEFAULT_TIE_BREAK


@dataclass(frozen=True)
class BestParams:
    """`best`: the value from the highest-scoring valid epoch. No parameters."""


@dataclass(frozen=True)
class ContinuousParams:
    """A continuous method plus the options every continuous method accepts (13 section 4)."""

    method: str
    conditional_on: str | None = None
    spread: str | None = None
    p: float | None = None            # percentile only
    unc: str | None = None            # inverse_variance only


LayerParams = VoteParams | BestParams | ContinuousParams


def _parse_categorical(layer: LayerSpec) -> LayerParams | None:
    agg = layer.aggregate
    params = dict(agg.params)
    if agg.method == "none":
        _refuse_params(layer, params, frozenset())
        return None
    if agg.method == "best":
        _refuse_params(layer, params, frozenset())
        return BestParams()
    if agg.method != "vote":
        raise SchemaError(f"layer {layer.name!r}: categorical method {agg.method!r} is not one of "
                          f"{CATEGORICAL_METHODS} (13 section 4)")
    _refuse_params(layer, params, _VOTE_PARAMS)
    min_count = params.get("min_count", DEFAULT_MIN_COUNT)
    if isinstance(min_count, bool) or not isinstance(min_count, int) or min_count < 1:
        raise SchemaError(f"layer {layer.name!r}: min_count must be a positive integer")
    ignore = params.get("ignore", ())
    if isinstance(ignore, str) or not isinstance(ignore, Sequence) \
            or not all(isinstance(n, str) for n in ignore):
        raise SchemaError(f"layer {layer.name!r}: ignore must be a list of class names "
                          "(13 section 3; ids are never used here)")
    tie_break = params.get("tie_break", DEFAULT_TIE_BREAK)
    if tie_break not in TIE_BREAKS:
        raise SchemaError(f"layer {layer.name!r}: tie_break {tie_break!r} is not one of {TIE_BREAKS}")
    return VoteParams(min_count=min_count, ignore=tuple(ignore), tie_break=tie_break)


def _parse_continuous(layer: LayerSpec, schema: SnapshotSchema) -> ContinuousParams | None:
    agg = layer.aggregate
    params = dict(agg.params)
    if agg.method == "none":
        _refuse_params(layer, params, frozenset())
        return None
    if agg.method not in CONTINUOUS_METHODS:
        raise SchemaError(f"layer {layer.name!r}: continuous method {agg.method!r} is not one of "
                          f"{CONTINUOUS_METHODS} (13 section 4)")
    allowed = set(_CONTINUOUS_PARAMS)
    if agg.method == "percentile":
        allowed.add("p")
    if agg.method == "inverse_variance":
        allowed.add("unc")
    _refuse_params(layer, params, frozenset(allowed))

    p = None
    if agg.method == "percentile":
        p = params.get("p")
        if isinstance(p, bool) or not isinstance(p, (int, float)) or not 0 <= p <= 100:
            raise SchemaError(f"layer {layer.name!r}: percentile needs p in [0, 100]")
        p = float(p)

    unc = None
    if agg.method == "inverse_variance":
        unc = params.get("unc")
        if not isinstance(unc, str):
            raise SchemaError(f"layer {layer.name!r}: inverse_variance needs unc: <layer>")
        try:
            unc_layer = schema[unc]
        except KeyError:
            raise SchemaError(f"layer {layer.name!r}: unc names undeclared layer {unc!r}") from None
        if unc_layer.kind != "continuous":
            raise SchemaError(f"layer {layer.name!r}: unc layer {unc!r} must be continuous")

    cond = params.get("conditional_on")
    if cond is not None:
        if not isinstance(cond, str):
            raise SchemaError(f"layer {layer.name!r}: conditional_on must name a layer")
        try:
            cond_layer = schema[cond]
        except KeyError:
            raise SchemaError(f"layer {layer.name!r}: conditional_on names undeclared layer "
                              f"{cond!r}") from None
        if cond_layer.kind != "categorical":
            raise SchemaError(f"layer {layer.name!r}: conditional_on {cond!r} is not categorical "
                              "(13 section 4)")
        if cond_layer.aggregate.method not in ("vote", "best"):
            raise SchemaError(f"layer {layer.name!r}: conditional_on {cond!r} delivers no winner "
                              f"(method {cond_layer.aggregate.method!r}); it must be vote or best")

    spread = params.get("spread")
    if spread is not None and spread not in SPREADS:
        raise SchemaError(f"layer {layer.name!r}: spread {spread!r} is not one of {SPREADS}")

    return ContinuousParams(method=agg.method, conditional_on=cond, spread=spread, p=p, unc=unc)


def _refuse_params(layer: LayerSpec, params: Mapping[str, Any], allowed: frozenset[str]) -> None:
    extra = sorted(set(params) - allowed)
    if extra:
        raise SchemaError(f"layer {layer.name!r}: method {layer.aggregate.method!r} does not take "
                          f"{extra} (13 section 4)")


def parse_schema(schema: SnapshotSchema) -> dict[str, LayerParams | None]:
    """Parse and validate every layer's aggregation. None means `none`: carried, not delivered.

    Raises SchemaError for anything outside the vocabulary of 13 section 4, a `conditional_on` or
    `unc` naming a layer that is missing or of the wrong kind, or a parameter a method does not
    take. Categorical layers cannot themselves be conditional, so a cycle cannot arise.
    """
    parsed: dict[str, LayerParams | None] = {}
    for layer in schema.layers:
        if layer.kind == "categorical":
            parsed[layer.name] = _parse_categorical(layer)
        elif layer.kind == "continuous":
            parsed[layer.name] = _parse_continuous(layer, schema)
        else:
            raise SchemaError(f"layer {layer.name!r}: kind {layer.kind!r} is not categorical or "
                              "continuous")
    return parsed


def validate_schema(schema: SnapshotSchema) -> None:
    """Plan-time check that the built-in reducer can execute this schema (04 section 8)."""
    parse_schema(schema)


def layer_band_count(layer: LayerSpec) -> int:
    """Bands a layer's delivered arrays carry, as far as the schema alone can say: the declared
    subset's length, else 1. A multi-band source with `bands: None` is only knowable from the
    snapshot; pass `band_counts` to `delivered_bands` for that."""
    return len(layer.bands) if layer.bands else 1


def delivered_bands(schema: SnapshotSchema, *,
                    band_counts: Mapping[str, int] | None = None) -> tuple[BandSpec, ...]:
    """The output bands the built-in reducer delivers for `schema`, in schema order (13 section 4).

    Per layer: a categorical `vote` layer L delivers L, L_agreement, L_runner_up; `best` delivers
    L; `none` delivers nothing. A continuous layer with any method other than `none` delivers L
    (keeping its band axis) and L_n, plus L_spread when `spread` is set. `n_epochs` is delivered
    once, last, whatever the schema says. `band_counts` overrides the per-layer band count for
    layers whose width the schema alone cannot state (multi-band source, `bands: None`).
    """
    parsed = parse_schema(schema)
    out: list[BandSpec] = []
    for layer in schema.layers:
        params = parsed[layer.name]
        if params is None:
            continue
        if layer.kind == "categorical":
            out.append(BandSpec(layer.name, CATEGORICAL_DTYPE,
                                f"{layer.name}: {layer.aggregate.method} over epochs; product ids",
                                nodata=CATEGORICAL_NODATA))
            if isinstance(params, VoteParams):
                out.append(BandSpec(f"{layer.name}_agreement", CONTINUOUS_DTYPE,
                                    f"{layer.name}: modal count / n_epochs", units="fraction",
                                    nodata=float("nan")))
                out.append(BandSpec(f"{layer.name}_runner_up", CATEGORICAL_DTYPE,
                                    f"{layer.name}: second-most-frequent class",
                                    nodata=CATEGORICAL_NODATA))
            continue
        assert isinstance(params, ContinuousParams)
        nb = layer_band_count(layer)
        if band_counts is not None and layer.name in band_counts:
            nb = int(band_counts[layer.name])
        what = params.method + (f" over epochs concordant with {params.conditional_on}"
                                if params.conditional_on else " over valid epochs")
        out.append(BandSpec(layer.name, CONTINUOUS_DTYPE, f"{layer.name}: {what}",
                            nodata=float("nan"), bands=nb))
        out.append(BandSpec(f"{layer.name}_n", COUNT_DTYPE,
                            f"{layer.name}: epochs aggregated", units="count"))
        if params.spread:
            out.append(BandSpec(f"{layer.name}_spread", CONTINUOUS_DTYPE,
                                f"{layer.name}: {params.spread} over the same epochs",
                                nodata=float("nan"), bands=nb))
    out.append(BandSpec("n_epochs", COUNT_DTYPE, "epochs with valid true", units="count"))
    return tuple(out)


def resolve_class_names(table: ClassTable | None, names: Sequence[str]) -> np.ndarray:
    """Class names -> product ids through a layer's class table (13 section 3).

    `none` is always id 0, listed in the table or not. Any other name must match exactly one row
    of the table's `name` column; the id is that row's key. The core never interprets the names.
    """
    ids: list[int] = []
    for name in names:
        if name == NONE_NAME:
            ids.append(NONE_CLASS)
            continue
        if table is None:
            raise SchemaError(f"cannot resolve class {name!r}: the layer has no class table")
        if "name" not in table.entries.column_names:
            raise SchemaError(f"cannot resolve class {name!r}: class table {table.source!r} has "
                              "no `name` column")
        col = np.asarray(table.entries.column("name").to_pylist(), dtype=object)
        hits = np.flatnonzero(col == name)
        if hits.size != 1:
            raise SchemaError(f"class {name!r} matches {hits.size} rows of class table "
                              f"{table.source!r}; must match exactly one")
        ids.append(int(table.entries.column(table.key)[int(hits[0])].as_py()))
    return np.asarray(ids, dtype=np.int64)
