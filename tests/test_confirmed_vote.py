"""`confirmed_mineral_vote`: fit-weighted per look, confirmed by a second look, `none` when
observed and unconfirmed. Each test is one (n, 1, 1) column of looks."""
from __future__ import annotations

import numpy as np
import pytest
from stratum_emit.reducers import ConfirmedMineralVote
from test_reduce import cat, cont, stack

from stratum.reduce import CATEGORICAL_NODATA

GROUP = {"mineral": "m", "fit": "f", "depth": "d", "acquired_out": "acq"}


def run(ids, fit, depth=None, score=None, valid=None, **params):
    n = len(ids)
    col = lambda v, dt: np.asarray(v, dtype=dt)[:, None, None]
    layers = {"m": col(ids, np.uint16), "f": col(fit, np.float32),
              "d": col(depth if depth is not None else [0.1] * n, np.float32),
              "acquired": col([1000.0 * (i + 1) for i in range(n)], np.float64)}
    specs = [cat("m"), cont("f", "none"), cont("d", "none"), cont("acquired", "none")]
    s = stack(layers, valid=col(valid if valid is not None else [True] * n, bool),
              score=col(score if score is not None else [1.0] * n, np.float32), specs=specs)
    r = ConfirmedMineralVote(groups=[GROUP], **params)
    out = r.reduce(s, None)
    assert set(out) == {b.name for b in r.outputs}
    return {k: v[0, 0] for k, v in out.items()}


def test_every_look_none_is_none_not_nodata() -> None:
    out = run([0, 0, 0], [0, 0, 0])
    assert out["m"] == 0 and out["m_agreement"] == 0.0 and out["m_support"] == 0.0
    assert out["m_runner_up"] == CATEGORICAL_NODATA


def test_never_validly_observed_is_nodata() -> None:
    out = run([5, 5], [0.4, 0.4], valid=[False, False])
    assert out["m"] == CATEGORICAL_NODATA and np.isnan(out["m_agreement"])
    assert out["n_epochs"] == 0


def test_a_single_detection_among_several_looks_is_unconfirmed() -> None:
    out = run([0, 7, 0, 0, 0], [0, 0.45, 0, 0, 0])
    assert out["m"] == 0
    assert out["m_runner_up"] == 7                 # still named, so it can be inspected
    assert np.isnan(out["d"]) and np.isnan(out["acq"])


def test_a_cell_seen_once_keeps_its_one_detection() -> None:
    out = run([7], [0.3])
    assert out["m"] == 7 and out["m_support"] == 1.0 and out["m_agreement"] == 1.0


def test_fit_decides_between_equally_confirmed_classes() -> None:
    out = run([4, 9, 4, 9], [0.30, 0.48, 0.30, 0.48])
    assert out["m"] == 9 and out["m_runner_up"] == 4


def test_count_weight_ignores_fit() -> None:
    out = run([4, 9, 4, 9, 4], [0.30, 0.48, 0.30, 0.48, 0.30], weight="count")
    assert out["m"] == 4


def test_a_confirmed_class_beats_a_heavier_unconfirmed_one() -> None:
    out = run([3, 3, 8, 0], [0.2, 0.2, 0.49, 0], score=[0.5, 0.5, 9.0, 1.0])
    assert out["m"] == 3 and out["m_runner_up"] == 8


def test_score_weights_the_vote_and_none_counts_in_agreement() -> None:
    out = run([2, 2, 6, 6, 0], [0.4] * 4 + [0], score=[0.2, 0.2, 1.0, 1.0, 1.0])
    assert out["m"] == 6 and out["m_support"] == 2.0
    assert out["m_agreement"] == pytest.approx(2 / 5)


def test_depth_is_the_weighted_mean_of_the_agreeing_looks_and_acquired_the_best() -> None:
    out = run([5, 5, 1], [0.25, 0.5, 0.5], depth=[0.1, 0.4, 0.9])
    assert out["d"] == pytest.approx((0.25 * 0.1 + 0.5 * 0.4) / 0.75)
    assert out["acq"] == 2000.0


def test_parameters_are_checked() -> None:
    with pytest.raises(ValueError, match="min_count"):
        ConfirmedMineralVote(min_count=0)
    with pytest.raises(ValueError, match="weight"):
        ConfirmedMineralVote(weight="depth")
    with pytest.raises(ValueError, match="lacks"):
        ConfirmedMineralVote(groups=[{"mineral": "m"}])
