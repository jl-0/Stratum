"""The built-in reducer: 13 section 4 executed in the decision order of 04 section 5.

Every stack here is hand-built on a small block so each cell exercises one branch. Epoch order is
ascending; cell (r, c) comments say which branch it covers.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pyarrow as pa
import pytest

from stratum.reduce import (
    CATEGORICAL_NODATA,
    SchemaError,
    band_counts,
    delivered_bands,
    reduce_stack,
    resolve_class_names,
    validate_schema,
)
from stratum.types import Aggregation, ClassTable, Epoch, LayerSpec, SnapshotSchema, SnapshotStack

ND = CATEGORICAL_NODATA
T0 = datetime(2026, 1, 1, tzinfo=UTC)


# ------------------------------------------------------------------------------------- builders
def class_table() -> ClassTable:
    return ClassTable(key="id", source="test",
                      entries=pa.table({"id": [0, 1, 2, 3], "name": ["none", "a", "b", "c"]}))


def cat(name: str, method: str = "vote", **params) -> LayerSpec:
    return LayerSpec(name=name, kind="categorical", source=name, classes=class_table(),
                     aggregate=Aggregation(method, params))


def cont(name: str, method: str = "median", bands: tuple[int, ...] | None = None,
         **params) -> LayerSpec:
    return LayerSpec(name=name, kind="continuous", source=name, bands=bands,
                     aggregate=Aggregation(method, params))


def epochs(n: int) -> list[Epoch]:
    return [Epoch(T0 + timedelta(days=30 * i), T0 + timedelta(days=30 * (i + 1))) for i in range(n)]


def stack(layers: dict[str, np.ndarray], *, valid: np.ndarray, score: np.ndarray | None = None,
          specs: list[LayerSpec] | None = None, eps: list[Epoch] | None = None) -> SnapshotStack:
    valid = np.asarray(valid, dtype=bool)
    if score is None:
        score = np.zeros(valid.shape, dtype=np.float32)
    if specs is None:
        specs = [cat(n) if a.dtype.kind in "iu" else cont(n) for n, a in layers.items()]
    schema = SnapshotSchema(name="t", layers=specs)
    return SnapshotStack(schema=schema, epochs=eps or epochs(valid.shape[0]), layers=layers,
                         valid=valid, score=np.asarray(score, dtype=np.float32))


def column(*per_epoch: int) -> np.ndarray:
    """One (n, 1, 1) categorical column from per-epoch values."""
    return np.asarray(per_epoch, dtype=np.uint16)[:, None, None]


# ------------------------------------------------------------------------- delivered_bands
def spec13_schema() -> SnapshotSchema:
    """The example declaration of 13 section 2."""
    return SnapshotSchema(name="cm-v1", layers=[
        cat("mineral_1", min_count=3, ignore=["none"], tie_break="highest_score"),
        cont("depth_1", "median", conditional_on="mineral_1", spread="iqr"),
        cat("mineral_2", min_count=3, ignore=["none"]),
        cont("depth_2", "inverse_variance", unc="depth_2_unc", conditional_on="mineral_2"),
        cont("depth_2_unc", "none"),
        cont("view_zenith", "none"),
    ])


def test_delivered_bands_order_dtypes_nodata():
    bands = delivered_bands(spec13_schema())
    assert [b.name for b in bands] == [
        "mineral_1", "mineral_1_agreement", "mineral_1_runner_up",
        "depth_1", "depth_1_n", "depth_1_spread",
        "mineral_2", "mineral_2_agreement", "mineral_2_runner_up",
        "depth_2", "depth_2_n",
        "n_epochs",
    ]
    by = {b.name: b for b in bands}
    assert (by["mineral_1"].dtype, by["mineral_1"].nodata) == ("uint16", 65535)
    assert by["mineral_1_agreement"].dtype == "float32" and np.isnan(by["mineral_1_agreement"].nodata)
    assert (by["mineral_1_runner_up"].dtype, by["mineral_1_runner_up"].nodata) == ("uint16", 65535)
    assert by["depth_1"].dtype == "float32" and np.isnan(by["depth_1"].nodata)
    assert (by["depth_1_n"].dtype, by["depth_1_n"].nodata) == ("uint16", None)
    assert by["depth_1_spread"].dtype == "float32"
    assert (by["n_epochs"].dtype, by["n_epochs"].nodata) == ("uint16", None)
    assert all(b.bands == 1 for b in bands)


def test_delivered_bands_best_and_none():
    schema = SnapshotSchema(name="t", layers=[cat("m", "best"), cont("d", "none"), cat("k", "none")])
    assert [b.name for b in delivered_bands(schema)] == ["m", "n_epochs"]


def test_delivered_bands_multiband_reports_band_axis():
    schema = SnapshotSchema(name="t", layers=[cont("refl", "mean", bands=(0, 1, 2), spread="std"),
                                              cont("cube", "median")])
    by = {b.name: b for b in delivered_bands(schema)}
    assert by["refl"].bands == 3 and by["refl_spread"].bands == 3 and by["refl_n"].bands == 1
    assert by["cube"].bands == 1                       # bands: None - the schema cannot know
    by = {b.name: b for b in delivered_bands(schema, band_counts={"cube": 285})}
    assert by["cube"].bands == 285


@pytest.mark.parametrize("layers, message", [
    ([cont("d", "median", conditional_on="x")], "undeclared"),
    ([cont("a"), cont("d", "median", conditional_on="a")], "not categorical"),
    ([cat("k", "none"), cont("d", "median", conditional_on="k")], "delivers no winner"),
    ([cont("d", "inverse_variance")], "needs unc"),
    ([cont("d", "inverse_variance", unc="u")], "undeclared"),
    ([cont("d", "percentile")], "needs p"),
    ([cont("d", "percentile", p=101)], "needs p"),
    ([cont("d", "median", spread="mad")], "spread"),
    ([cont("d", "median", min_count=3)], "does not take"),
    ([cat("m", tie_break="random")], "tie_break"),
    ([cat("m", min_count=0)], "min_count"),
    ([cat("m", ignore=[0])], "class names"),
    ([cat("m", "median")], "categorical method"),
    ([cont("d", "vote")], "continuous method"),
])
def test_validate_schema_refuses(layers, message):
    with pytest.raises(SchemaError, match=message):
        validate_schema(SnapshotSchema(name="t", layers=layers))


def test_resolve_class_names():
    table = class_table()
    assert resolve_class_names(table, ["none", "b"]).tolist() == [0, 2]
    assert resolve_class_names(None, ["none"]).tolist() == [0]
    with pytest.raises(SchemaError, match="matches 0 rows"):
        resolve_class_names(table, ["zzz"])
    with pytest.raises(SchemaError, match="no class table"):
        resolve_class_names(None, ["a"])


# ----------------------------------------------------------------------------------- vote
def test_vote_decision_order():
    """A 2x2 block, four epochs. Cell by cell:
    (0,0) all valid, unanimous a         -> a, agreement 1, no runner-up
    (0,1) epoch 3 invalid, 2a 1b         -> a, agreement 2/3, runner-up b; n_epochs 3
    (1,0) 2a 2none, none ignored         -> a, agreement 2/4 (ignored lowers it), no runner-up
    (1,1) all none                       -> nodata, agreement 0.0, n_epochs 4
    """
    m = np.zeros((4, 2, 2), dtype=np.uint16)
    m[:, 0, 0] = [1, 1, 1, 1]
    m[:, 0, 1] = [1, 2, 1, 3]
    m[:, 1, 0] = [1, 0, 1, 0]
    m[:, 1, 1] = [0, 0, 0, 0]
    valid = np.ones((4, 2, 2), dtype=bool)
    valid[3, 0, 1] = False
    out = reduce_stack(stack({"m": m}, valid=valid, specs=[cat("m", ignore=["none"])]))

    assert out["m"].tolist() == [[1, 1], [1, ND]]
    np.testing.assert_allclose(out["m_agreement"], [[1.0, 2 / 3], [0.5, 0.0]])
    assert out["m_runner_up"].tolist() == [[ND, 2], [ND, ND]]
    assert out["n_epochs"].tolist() == [[4, 3], [4, 4]]
    assert out["m"].dtype == np.uint16 and out["m_agreement"].dtype == np.float32
    assert out["n_epochs"].dtype == np.uint16


def test_vote_min_count_suppresses_layer_only():
    m = column(1, 1, 2, 0)
    out = reduce_stack(stack({"m": m}, valid=np.ones((4, 1, 1), bool),
                             specs=[cat("m", min_count=3, ignore=["none"])]))
    assert out["m"][0, 0] == ND
    assert out["m_runner_up"][0, 0] == ND
    assert out["m_agreement"][0, 0] == pytest.approx(0.5)      # the tally is still reported
    out = reduce_stack(stack({"m": m}, valid=np.ones((4, 1, 1), bool),
                             specs=[cat("m", min_count=2, ignore=["none"])]))
    assert out["m"][0, 0] == 1 and out["m_runner_up"][0, 0] == 2


def test_vote_no_valid_epoch():
    out = reduce_stack(stack({"m": column(1, 2)}, valid=np.zeros((2, 1, 1), bool)))
    assert out["m"][0, 0] == ND and out["m_runner_up"][0, 0] == ND
    assert np.isnan(out["m_agreement"][0, 0]) and out["n_epochs"][0, 0] == 0


def test_vote_not_ignoring_none_counts_it_as_a_class():
    out = reduce_stack(stack({"m": column(0, 0, 1)}, valid=np.ones((3, 1, 1), bool)))
    assert out["m"][0, 0] == 0                                  # observed, nothing identified
    assert out["m_runner_up"][0, 0] == 1


@pytest.mark.parametrize("tie_break, expect, runner", [
    ("earliest", 2, 1), ("latest", 1, 2), ("highest_score", 1, 2), ("nodata", ND, ND),
])
def test_vote_tie_breaks(tie_break, expect, runner):
    """Epochs: b, a, b, a - two each. Scores make the single highest belong to class a."""
    m = column(2, 1, 2, 1)
    score = np.asarray([0.1, 0.9, 0.2, 0.5], np.float32)[:, None, None]
    out = reduce_stack(stack({"m": m}, valid=np.ones((4, 1, 1), bool), score=score,
                             specs=[cat("m", tie_break=tie_break)]))
    assert out["m"][0, 0] == expect
    assert out["m_runner_up"][0, 0] == runner
    assert out["m_agreement"][0, 0] == pytest.approx(0.5)


def test_vote_tie_break_uses_epoch_order_not_array_order():
    """Arrays arrive with epochs reversed; `earliest` still means the earliest epoch."""
    eps = epochs(2)[::-1]
    out = reduce_stack(stack({"m": column(2, 1)}, valid=np.ones((2, 1, 1), bool), eps=eps,
                             specs=[cat("m", tie_break="earliest")]))
    assert out["m"][0, 0] == 1


def test_vote_ignored_class_never_wins_and_stays_in_n_epochs():
    m = column(0, 0, 0, 2)
    out = reduce_stack(stack({"m": m}, valid=np.ones((4, 1, 1), bool),
                             specs=[cat("m", ignore=["none"])]))
    assert out["m"][0, 0] == 2 and out["n_epochs"][0, 0] == 4
    assert out["m_agreement"][0, 0] == pytest.approx(0.25)


def test_vote_ignore_by_name_through_class_table():
    m = column(1, 1, 3)
    out = reduce_stack(stack({"m": m}, valid=np.ones((3, 1, 1), bool),
                             specs=[cat("m", ignore=["a"])]))
    assert out["m"][0, 0] == 3


def test_categorical_negative_values_refused():
    m = np.asarray([[[-1]]], dtype=np.int16)
    with pytest.raises(ValueError, match="negative"):
        reduce_stack(stack({"m": m}, valid=np.ones((1, 1, 1), bool)))


def test_categorical_masked_array_narrows_validity():
    m = np.ma.MaskedArray(column(1, 2, 2), mask=[[[False]], [[True]], [[True]]])
    out = reduce_stack(stack({"m": m}, valid=np.ones((3, 1, 1), bool)))
    assert out["m"][0, 0] == 1 and out["n_epochs"][0, 0] == 3


# ----------------------------------------------------------------------------------- best
def test_best_categorical_takes_highest_score_among_valid():
    m = column(1, 2, 3)
    score = np.asarray([0.5, 9.0, 0.7], np.float32)[:, None, None]
    valid = np.asarray([True, False, True])[:, None, None]
    out = reduce_stack(stack({"m": m}, valid=valid, score=score, specs=[cat("m", "best")]))
    assert out["m"][0, 0] == 3
    assert list(out) == ["m", "n_epochs"]


def test_best_continuous():
    d = np.asarray([1.0, 5.0, 3.0], np.float32)[:, None, None]
    score = np.asarray([-40.0, -5.0, -20.0], np.float32)[:, None, None]
    out = reduce_stack(stack({"d": d}, valid=np.ones((3, 1, 1), bool), score=score,
                             specs=[cont("d", "best")]))
    assert out["d"][0, 0] == 5.0 and out["d_n"][0, 0] == 3


# ----------------------------------------------------------------------------- continuous
def test_continuous_methods_mask_on_valid():
    d = np.asarray([1.0, 2.0, 100.0, 4.0], np.float32)[:, None, None]
    valid = np.asarray([True, True, False, True])[:, None, None]
    expect = {"median": 2.0, "mean": 7 / 3, "min": 1.0, "max": 4.0}
    for method, value in expect.items():
        out = reduce_stack(stack({"d": d}, valid=valid, specs=[cont("d", method)]))
        assert out["d"][0, 0] == pytest.approx(value), method
        assert out["d_n"][0, 0] == 3 and out["d"].dtype == np.float32
    out = reduce_stack(stack({"d": d}, valid=valid, specs=[cont("d", "percentile", p=100)]))
    assert out["d"][0, 0] == 4.0


def test_continuous_nothing_valid_is_nan_and_zero_count():
    d = np.ones((2, 1, 1), np.float32)
    out = reduce_stack(stack({"d": d}, valid=np.zeros((2, 1, 1), bool), specs=[cont("d")]))
    assert np.isnan(out["d"][0, 0]) and out["d_n"][0, 0] == 0


def test_score_weighted_with_negative_scores():
    d = np.asarray([10.0, 20.0, 30.0], np.float32)[:, None, None]
    score = np.asarray([-60.0, -30.0, -10.0], np.float32)[:, None, None]  # -zenith
    out = reduce_stack(stack({"d": d}, valid=np.ones((3, 1, 1), bool), score=score,
                             specs=[cont("d", "score_weighted")]))
    w = np.asarray([0.0, 30.0, 50.0]) + 1e-6
    assert out["d"][0, 0] == pytest.approx((w * [10, 20, 30]).sum() / w.sum(), rel=1e-5)
    # equal scores reduce to a mean
    out = reduce_stack(stack({"d": d}, valid=np.ones((3, 1, 1), bool),
                             specs=[cont("d", "score_weighted")]))
    assert out["d"][0, 0] == pytest.approx(20.0)


def test_inverse_variance_excludes_bad_uncertainty():
    d = np.asarray([1.0, 3.0, 100.0, 100.0], np.float32)[:, None, None]
    u = np.asarray([1.0, 2.0, 0.0, np.nan], np.float32)[:, None, None]
    out = reduce_stack(stack({"d": d, "u": u}, valid=np.ones((4, 1, 1), bool),
                             specs=[cont("d", "inverse_variance", unc="u"), cont("u", "none")]))
    assert out["d"][0, 0] == pytest.approx((1.0 * 1 + 3.0 * 0.25) / 1.25)
    assert out["d_n"][0, 0] == 2
    assert list(out) == ["d", "d_n", "n_epochs"]                # `u` is carried, not delivered


def test_spread_std_and_iqr():
    d = np.asarray([1.0, 2.0, 3.0, 4.0], np.float32)[:, None, None]
    out = reduce_stack(stack({"d": d}, valid=np.ones((4, 1, 1), bool),
                             specs=[cont("d", "mean", spread="std")]))
    assert out["d_spread"][0, 0] == pytest.approx(np.std([1, 2, 3, 4]))
    out = reduce_stack(stack({"d": d}, valid=np.ones((4, 1, 1), bool),
                             specs=[cont("d", "mean", spread="iqr")]))
    assert out["d_spread"][0, 0] == pytest.approx(np.percentile([1, 2, 3, 4], 75)
                                                  - np.percentile([1, 2, 3, 4], 25))
    assert list(out) == ["d", "d_n", "d_spread", "n_epochs"]


def test_conditional_on_aggregates_concordant_epochs_only():
    """Cell 0: winner a in epochs 0,2 -> depth over those two; cell 1: all ignored -> nothing."""
    m = np.zeros((3, 1, 2), np.uint16)
    m[:, 0, 0] = [1, 2, 1]
    m[:, 0, 1] = [0, 0, 0]
    d = np.zeros((3, 1, 2), np.float32)
    d[:, 0, 0] = [0.10, 0.90, 0.30]
    d[:, 0, 1] = [0.5, 0.5, 0.5]
    out = reduce_stack(stack({"m": m, "d": d}, valid=np.ones((3, 1, 2), bool),
                             specs=[cat("m", ignore=["none"]),
                                    cont("d", "mean", conditional_on="m")]))
    assert out["m"].tolist() == [[1, ND]]
    np.testing.assert_allclose(out["d"][0, 0], 0.2)
    assert out["d_n"].tolist() == [[2, 0]]
    assert np.isnan(out["d"][0, 1])
    assert out["n_epochs"].tolist() == [[3, 3]]                    # concordant count != n_epochs


def test_conditional_on_a_later_layer_and_schema_order_preserved():
    m = column(2, 2, 1)
    d = np.asarray([1.0, 3.0, 50.0], np.float32)[:, None, None]
    out = reduce_stack(stack({"d": d, "m": m}, valid=np.ones((3, 1, 1), bool),
                             specs=[cont("d", "median", conditional_on="m"), cat("m")]))
    assert out["d"][0, 0] == 2.0 and out["d_n"][0, 0] == 2
    assert list(out) == ["d", "d_n", "m", "m_agreement", "m_runner_up", "n_epochs"]


def test_multiband_layer_aggregates_band_wise():
    refl = np.zeros((3, 2, 2, 3), np.float32)
    refl[..., 0] = np.asarray([1.0, 2.0, 3.0])[:, None, None]
    refl[..., 1] = np.asarray([10.0, 20.0, 30.0])[:, None, None]
    refl[..., 2] = np.asarray([5.0, 5.0, 100.0])[:, None, None]
    valid = np.ones((3, 2, 2), bool)
    valid[2, 1, 1] = False
    u = np.full((3, 2, 2), 2.0, np.float32)
    snaps = stack({"refl": refl, "u": u}, valid=valid,
                  specs=[cont("refl", "median", spread="iqr"), cont("u", "none")])
    out = reduce_stack(snaps)
    assert out["refl"].shape == (2, 2, 3) and out["refl_spread"].shape == (2, 2, 3)
    np.testing.assert_allclose(out["refl"][0, 0], [2.0, 20.0, 5.0])
    np.testing.assert_allclose(out["refl"][1, 1], [1.5, 15.0, 5.0])
    assert out["refl_n"].tolist() == [[3, 3], [3, 2]]
    assert band_counts(snaps) == {"refl": 3, "u": 1}
    # a single-band uncertainty layer weights every band of a multi-band layer
    snaps = stack({"refl": refl, "u": u}, valid=valid,
                  specs=[cont("refl", "inverse_variance", unc="u"), cont("u", "none")])
    out = reduce_stack(snaps)
    np.testing.assert_allclose(out["refl"][0, 0], [2.0, 20.0, 110 / 3], rtol=1e-6)
    for method in ("mean", "min", "max", "score_weighted", "best"):
        out = reduce_stack(stack({"refl": refl}, valid=valid, specs=[cont("refl", method)]))
        assert out["refl"].shape == (2, 2, 3), method


# ------------------------------------------------------------------- single-epoch identity
def test_single_epoch_is_identity():
    """04 section 5: over one epoch every aggregation is the identity."""
    m = np.asarray([[[1, 0], [3, 2]]], np.uint16)
    d = np.asarray([[[0.1, 0.2], [0.3, 0.4]]], np.float32)
    u = np.asarray([[[0.5, 0.5], [0.5, 0.5]]], np.float32)
    valid = np.asarray([[[True, True], [True, False]]])
    score = -np.asarray([[[10.0, 20.0], [30.0, 40.0]]], np.float32)
    methods = ["median", "mean", "min", "max", "score_weighted", "best"]
    specs = [cat("m", ignore=["none"], tie_break="highest_score"), cat("k", "best"),
             *[cont(f"d_{meth}", meth, conditional_on="m" if meth == "median" else None)
               for meth in methods],
             cont("d_pct", "percentile", p=50, spread="std"),
             cont("d_iv", "inverse_variance", unc="u"), cont("u", "none")]
    layers = {"m": m, "k": m, "u": u, "d_pct": d, "d_iv": d, **{f"d_{x}": d for x in methods}}
    out = reduce_stack(stack(layers, valid=valid, score=score, specs=specs))

    assert out["m"].tolist() == [[1, ND], [3, ND]]          # (0,1) is `none`, ignored; (1,1) invalid
    assert out["k"].tolist() == [[1, 0], [3, ND]]           # best does not ignore: 0 is a class
    np.testing.assert_allclose(out["m_agreement"], [[1.0, 0.0], [1.0, np.nan]])
    assert out["m_runner_up"].tolist() == [[ND, ND], [ND, ND]]
    assert out["n_epochs"].tolist() == [[1, 1], [1, 0]]
    for name in [f"d_{x}" for x in methods if x != "median"] + ["d_pct", "d_iv"]:
        np.testing.assert_allclose(out[name], [[0.1, 0.2], [0.3, np.nan]], err_msg=name)
        assert out[f"{name}_n"].tolist() == [[1, 1], [1, 0]], name
    np.testing.assert_allclose(out["d_median"], [[0.1, np.nan], [0.3, np.nan]])  # concordant only
    np.testing.assert_allclose(out["d_pct_spread"], [[0.0, 0.0], [0.0, np.nan]])


def test_output_names_match_delivered_bands():
    schema = spec13_schema()
    n, h, w = 3, 2, 2
    layers = {
        "mineral_1": np.ones((n, h, w), np.uint16), "mineral_2": np.full((n, h, w), 2, np.uint16),
        "depth_1": np.ones((n, h, w), np.float32), "depth_2": np.ones((n, h, w), np.float32),
        "depth_2_unc": np.ones((n, h, w), np.float32), "view_zenith": np.ones((n, h, w), np.float32),
    }
    snaps = SnapshotStack(schema=schema, epochs=epochs(n), layers=layers,
                          valid=np.ones((n, h, w), bool), score=np.zeros((n, h, w), np.float32))
    out = reduce_stack(snaps)
    assert list(out) == [b.name for b in delivered_bands(schema)]
    assert out["mineral_1"].tolist() == [[1, 1], [1, 1]] and out["depth_1_n"].tolist() == [[3, 3]] * 2


def test_epoch_count_mismatch_refused():
    with pytest.raises(ValueError, match="epochs"):
        reduce_stack(stack({"m": column(1, 1)}, valid=np.ones((2, 1, 1), bool), eps=epochs(3)))
