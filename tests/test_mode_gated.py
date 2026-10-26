"""The `cos_i` mask and the `mode_gated_select` stack scorer (stratum_emit), on synthetic stacks.

Most tests build a stacked `ObsWindow` by hand - N looks of a few cells - and apply the
framework's own argmax (`stack_argmax`) to the score, so "the winner" here is exactly what resolve
would pick. One test runs the scorer through `resolve_block` on the synthetic plan harness.
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pyarrow as pa
import pytest
from stratum_emit.masks import CosineI
from stratum_emit.scorers import ModeGatedSelect
from stratum_emit.scorers.mode_gated import METRICS, rank_penalty, tally
from synthetic import (
    ENUMERATION,
    EPOCH,
    TILE,
    build_plan,
    item,
    make_observation,
)

from stratum.reduce.bands import SchemaError
from stratum.resolve import read_snapshot, resolve_block, stack_argmax
from stratum.types import (
    Aggregation,
    ClassTable,
    GranuleRef,
    LayerSpec,
    ObsWindow,
    SnapshotSchema,
    acquired_seconds,
)

pytestmark = pytest.mark.filterwarnings("ignore::rasterio.errors.NotGeoreferencedWarning")

# product ids of a toy group-1 / group-2 table; names are what a manifest uses
TABLE = ClassTable(key="id", entries=pa.table({
    "id": [0, 45, 47, 60, 119, 150, 200],
    "name": ["none", "Hematite", "Goethite", "Jarosite", "Kaolinite", "Dry vegetation",
             "Alunite"]}), source="test:product")
HEM, GOE, JAR, KAO, VEG, ALU = 45, 47, 60, 119, 150, 200
VZ_ONLY = {m: 0.0 for m in METRICS} | {"vza": 1.0}


def granule(i: int, cloud: float | None = 0.1) -> GranuleRef:
    when = datetime(2025, 1, 1, tzinfo=UTC) + timedelta(days=7 * i)
    return GranuleRef(granule_id=f"G{i:02d}", collection="EMITL2BMIN", datetime=when,
                      end_datetime=when, bbox=(0.0, 0.0, 1.0, 1.0), assets={},
                      build_version="1", product_version="V001", collection_version="001",
                      cloud_fraction=cloud)


def stack(pairs: Sequence[tuple[int, int]], *, bands: dict[str, Sequence[float]] | None = None,
          valid: Sequence[bool] | None = None,
          cloud: Sequence[float | None] | None = None) -> ObsWindow:
    """N looks of ONE cell, as a stacked (N, 1, 1) window: `pairs` is each look's (g1, g2)
    product id, `bands` each named band's per-look value."""
    n = len(pairs)
    g1 = np.array([p[0] for p in pairs], dtype=np.int32).reshape(n, 1, 1)
    g2 = np.array([p[1] for p in pairs], dtype=np.int32).reshape(n, 1, 1)
    arrays = {k: np.asarray(v, dtype=np.float32).reshape(n, 1, 1)
              for k, v in (bands or {}).items()}
    ok = np.ones((n, 1, 1), bool) if valid is None else np.asarray(valid).reshape(n, 1, 1)
    clouds = [0.1] * n if cloud is None else list(cloud)
    return ObsWindow(space="block", bands=arrays, valid=ok,
                     granules=[granule(i, clouds[i]) for i in range(n)], epoch=EPOCH,
                     layers={"mineral_1": g1, "mineral_2": g2}, stacked=True,
                     layer_classes={"mineral_1": TABLE, "mineral_2": TABLE})


def winner(scorer: ModeGatedSelect, obs: ObsWindow) -> int | None:
    """The row the framework would pick for the one cell, or None if no look may occupy it."""
    score = np.asarray(scorer.score(obs, None), dtype=np.float32)
    idx, won, _ = stack_argmax(score, obs.valid)
    return int(idx[0, 0]) if won[0, 0] else None


# --------------------------------------------------------------------------------- cos_i
def test_cos_i_mask() -> None:
    m = CosineI()
    assert m.min_cos_i == 0.3 and m.required_roles == ("cos_i",) and m.space == "map"
    obs = ObsWindow(space="block", bands={"cos_i": np.array([[0.29, 0.3, 0.9, np.nan]],
                                                            dtype=np.float32)},
                    valid=np.ones((1, 4), bool), granules=[granule(0)], epoch=EPOCH)
    assert m.valid(obs, None).tolist() == [[False, True, True, False]]
    masked = np.ma.MaskedArray(np.array([[0.8, 0.8]], np.float32), mask=[[False, True]])
    obs = ObsWindow(space="block", bands={"illum": masked}, valid=np.ones((1, 2), bool),
                    granules=[granule(0)], epoch=EPOCH)
    custom = CosineI(min_cos_i=0.5, alias="illum")
    assert custom.required_roles == ("illum",)
    assert custom.valid(obs, None).tolist() == [[True, False]]
    with pytest.raises(ValueError, match=r"\[-1, 1\]"):
        CosineI(min_cos_i=1.5)


# ------------------------------------------------------------------------------ the gate
def test_mode_gated_hard_gate() -> None:
    """The worked example: the joint mode is (45, 119); a (47, 119) look scores best of all and
    is still excluded, so the best of the MODE's looks wins."""
    pairs = [(HEM, KAO), (GOE, KAO), (HEM, KAO), (GOE, KAO), (HEM, KAO)]
    vz = [20.0, 1.0, 12.0, 25.0, 8.0]                 # the (47, 119) look at row 1 is most nadir
    s = ModeGatedSelect(weights=VZ_ONLY)
    obs = stack(pairs, bands={"view_zenith": vz})
    score = s.score(obs, None)
    assert np.isnan(score[1, 0, 0]) and np.isnan(score[3, 0, 0])
    assert winner(s, obs) == 4                        # vz 8, the best (45, 119) look
    out = s.emit(obs, None)
    assert out["mode_count"][0, 0] == 3 and out["n_valid"][0, 0] == 5
    assert out["agreement"][0, 0] == pytest.approx(0.6)
    assert out["runner_up_count"][0, 0] == 2
    assert out["selection_penalty"][4, 0, 0] == pytest.approx(8 / 30)


def test_mode_gated_soft_gate() -> None:
    pairs = [(HEM, KAO), (GOE, KAO), (HEM, KAO)]
    vz = [20.0, 1.0, 12.0]
    s = ModeGatedSelect(weights=VZ_ONLY, gate="soft")
    obs = stack(pairs, bands={"view_zenith": vz})
    score = s.score(obs, None)
    assert score[1, 0, 0] == pytest.approx(-(1 / 30 + 10.0))      # ranked, not excluded
    assert winner(s, obs) == 2
    # a soft penalty smaller than the penalty gap lets the better non-mode look through
    assert winner(ModeGatedSelect(weights=VZ_ONLY, gate="soft", soft_penalty=0.1), obs) == 1


def test_mode_gated_partial_votes() -> None:
    """(A, 0) three times votes 0.03; one full (A, B) votes 1.0 and is the mode."""
    s = ModeGatedSelect(weights=VZ_ONLY)
    obs = stack([(HEM, 0), (HEM, 0), (HEM, 0), (HEM, KAO)],
                bands={"view_zenith": [1, 2, 3, 20]})
    assert winner(s, obs) == 3
    assert s.emit(obs, None)["mode_votes"][0, 0] == pytest.approx(1.0)
    # with full partial weight the three partials outvote it
    assert winner(ModeGatedSelect(weights=VZ_ONLY, partial_vote_weight=1.0), obs) == 0


def test_mode_gated_tie_by_penalty() -> None:
    pairs = [(HEM, KAO), (GOE, ALU)]                  # one vote each
    obs = stack(pairs, bands={"view_zenith": [10.0, 5.0]})
    assert winner(ModeGatedSelect(weights=VZ_ONLY), obs) == 1          # lower penalty's key
    assert winner(ModeGatedSelect(weights=VZ_ONLY, tie_break="earliest"), obs) == 0
    assert winner(ModeGatedSelect(weights=VZ_ONLY, tie_break="latest"), obs) == 1
    assert winner(ModeGatedSelect(weights=VZ_ONLY, tie_break="nodata"), obs) is None
    # a full pair beats an equally voted partial pair on the partial_tie_penalty
    obs = stack([(HEM, KAO), (GOE, 0), (GOE, 0)],
                bands={"view_zenith": [10.0, 9.0, 9.0]})
    s = ModeGatedSelect(weights=VZ_ONLY, partial_vote_weight=0.5)
    assert winner(s, obs) == 0
    # ... unless the partial look is better by more than that penalty
    obs = stack([(HEM, KAO), (GOE, 0), (GOE, 0)],
                bands={"view_zenith": [10.0, 5.0, 5.0]})
    assert winner(s, obs) == 1


def test_mode_gated_equal_scores_go_to_the_earliest() -> None:
    s = ModeGatedSelect(weights=VZ_ONLY)
    obs = stack([(HEM, KAO)] * 3, bands={"view_zenith": [7.0, 7.0, 7.0]})
    assert winner(s, obs) == 0


def test_mode_gated_no_detection() -> None:
    """No look detects anything: no mode, no gate, the best look wins."""
    s = ModeGatedSelect(weights=VZ_ONLY)
    obs = stack([(0, 0), (0, 0), (0, 0)], bands={"view_zenith": [9.0, 3.0, 6.0]})
    assert winner(s, obs) == 1
    out = s.emit(obs, None)
    assert out["mode_count"][0, 0] == 0 and out["n_voters"][0, 0] == 0
    assert out["agreement"][0, 0] == 0.0
    # with a detection present the non-detect looks are gated out
    obs = stack([(0, 0), (0, 0), (HEM, 0)], bands={"view_zenith": [9.0, 3.0, 6.0]})
    assert winner(s, obs) == 2


def test_mode_gated_min_count() -> None:
    obs = stack([(HEM, KAO), (HEM, KAO), (GOE, KAO)], bands={"view_zenith": [3.0, 4.0, 1.0]})
    assert winner(ModeGatedSelect(weights=VZ_ONLY, min_count=2), obs) == 0
    assert winner(ModeGatedSelect(weights=VZ_ONLY, min_count=3), obs) is None


# ------------------------------------------------------------------ ignore and the g2 mask
def test_mode_gated_ignore_lists() -> None:
    pairs = [(HEM, VEG), (HEM, VEG), (HEM, VEG), (GOE, KAO), (GOE, KAO)]
    vz = [1.0, 2.0, 3.0, 9.0, 8.0]
    obs = stack(pairs, bands={"view_zenith": vz})
    assert winner(ModeGatedSelect(weights=VZ_ONLY), obs) == 0          # (HEM, VEG) x3 is the mode
    s = ModeGatedSelect(weights=VZ_ONLY, ignore={"g2": ["Dry vegetation"]})
    assert winner(s, obs) == 4                        # the VEG looks abstain AND fail the gate
    out = s.emit(obs, None)
    assert out["n_voters"][0, 0] == 2 and out["n_valid"][0, 0] == 5
    assert out["agreement"][0, 0] == pytest.approx(0.4)
    s = ModeGatedSelect(weights=VZ_ONLY, ignore={"g1": ["Goethite"], "g2": ["none"]})
    assert winner(s, obs) == 0


def test_mode_gated_ignored_class_never_occupies_without_a_mode() -> None:
    """An ignored look never matches, also where nothing else voted - so the ignored class can
    never come out of the hard gate, whether or not some other look detected anything."""
    s = ModeGatedSelect(weights=VZ_ONLY, ignore={"g2": ["Dry vegetation"]})
    # no voter at all: the non-detect look wins, never the ignored, more nadir one
    obs = stack([(0, VEG), (0, 0)], bands={"view_zenith": [2.0, 20.0]})
    assert winner(s, obs) == 1
    assert s.emit(obs, None)["n_voters"][0, 0] == 0
    # a voter elsewhere in the cell gates it out the same way
    obs = stack([(0, VEG), (HEM, 0)], bands={"view_zenith": [2.0, 20.0]})
    assert winner(s, obs) == 1
    # only ignored looks: the cell stays empty
    obs = stack([(0, VEG), (HEM, VEG)], bands={"view_zenith": [2.0, 20.0]})
    assert winner(s, obs) is None
    # soft gate: ranked behind every non-ignored look, but may still occupy a cell alone
    soft = ModeGatedSelect(weights=VZ_ONLY, ignore={"g2": ["Dry vegetation"]}, gate="soft")
    assert winner(soft, stack([(0, VEG), (0, 0)], bands={"view_zenith": [2.0, 20.0]})) == 1
    assert winner(soft, stack([(0, VEG)], bands={"view_zenith": [2.0]})) == 0
    # independent key: the same rule
    ind = ModeGatedSelect(weights=VZ_ONLY, key="independent", ignore={"g2": ["Dry vegetation"]})
    assert winner(ind, stack([(0, VEG), (0, 0)], bands={"view_zenith": [2.0, 20.0]})) == 1


def test_mode_gated_g2_mask_classes() -> None:
    """G2 -> none where it is a listed class; G1 is untouched, so the look still votes (A, 0)."""
    pairs = [(HEM, VEG), (HEM, VEG), (HEM, VEG), (HEM, KAO), (HEM, KAO)]
    vz = [1.0, 2.0, 3.0, 9.0, 8.0]
    obs = stack(pairs, bands={"view_zenith": vz})
    assert winner(ModeGatedSelect(weights=VZ_ONLY), obs) == 0
    masked = ModeGatedSelect(weights=VZ_ONLY, g2_mask_classes=["Dry vegetation"])
    assert winner(masked, obs) == 4                   # (HEM, KAO) x2 beats (HEM, 0) x3 at 0.01
    full = ModeGatedSelect(weights=VZ_ONLY, g2_mask_classes=["Dry vegetation"],
                           partial_vote_weight=1.0)
    assert winner(full, obs) == 0                     # ... unless partial looks vote in full
    assert full.emit(obs, None)["mode_count"][0, 0] == 3
    # masking and ignoring in the same group resolve their names separately
    both = ModeGatedSelect(weights=VZ_ONLY, g2_mask_classes=["Dry vegetation"],
                           ignore={"g2": ["Kaolinite"]})
    assert winner(both, obs) == 0                     # (HEM, KAO) abstains; (HEM, 0) is the mode
    with pytest.raises(SchemaError, match="Sagebrush"):
        ModeGatedSelect(weights=VZ_ONLY, g2_mask_classes=["Sagebrush"]).score(obs, None)
    with pytest.raises(ValueError, match="NAMES"):
        ModeGatedSelect(g2_mask_classes=[150])


# --------------------------------------------------------------------------- the penalty
def test_mode_gated_penalties_match_reference() -> None:
    bands = {
        "mineral_unc": [0.015], "mineral_2_unc": [0.0],       # g2 detects nothing -> ignored
        "mineral_fit": [0.25], "mineral_2_fit": [0.0],
        "mineral_depth": [0.1], "mineral_2_depth": [0.3],
        "cloud_prob": [0.2], "frcov": [0.7],
        "solar_zenith": [35.0], "view_zenith": [6.0], "path_length": [427399.0],
    }
    obs = stack([(HEM, 0)], bands=bands, cloud=[0.25])
    s = ModeGatedSelect()
    pen = s.penalties(obs, np.array([[[HEM]]]), np.array([[[0]]]))
    mu0, muv = np.cos(np.deg2rad(35.0)), np.cos(np.deg2rad(6.0))
    expect = {
        "bd_unc": 0.015 / 0.03, "fit": 1 - 0.25 / 0.5, "banddepth": 1 - 0.2 / 0.5,
        "cloud_prob": 0.2, "scene_cloudiness": 0.25, "bare": 0.3, "sza": 35 / 70,
        "vza": 6 / 30, "path_length": (427399 - 402404) / (452394 - 402404),
        "airmass": (1 / mu0 + 1 / muv - 2) / 2,
    }
    assert set(pen) == set(expect)
    for m, v in expect.items():
        assert pen[m][0, 0, 0] == pytest.approx(v, abs=1e-5), m
    assert s.weights == {m: pytest.approx(0.1) for m in METRICS}   # ten 0.2s renormalised
    total = s.penalty(obs, np.array([[[HEM]]]), np.array([[[0]]]))
    assert total[0, 0, 0] == pytest.approx(sum(expect.values()) / 10, abs=1e-5)


def test_mode_gated_missing_metric_scores_worst() -> None:
    w = {m: 0.0 for m in METRICS} | {"vza": 1.0, "scene_cloudiness": 1.0, "fit": 1.0}
    s = ModeGatedSelect(weights=w)
    obs = stack([(HEM, KAO), (HEM, KAO)],
                bands={"view_zenith": [np.nan, 3.0], "mineral_fit": [0.5, 0.5],
                       "mineral_2_fit": [0.5, 0.5]},
                cloud=[0.0, None])
    pen = s.penalties(obs, np.full((2, 1, 1), HEM), np.full((2, 1, 1), KAO))
    assert pen["vza"][:, 0, 0].tolist() == [1.0, pytest.approx(0.1)]
    assert pen["scene_cloudiness"][:, 0, 0].tolist() == [0.0, 1.0]   # None -> worst
    # fit is ignored for a group that detected nothing; both groups missing -> worst
    pen = s.penalties(obs, np.zeros((2, 1, 1), int), np.zeros((2, 1, 1), int))
    assert pen["fit"][:, 0, 0].tolist() == [1.0, 1.0]


def test_mode_gated_invalid_looks_never_vote_or_win() -> None:
    s = ModeGatedSelect(weights=VZ_ONLY)
    # three invalid (GOE, KAO) looks would be the mode; invalid, they do not vote
    obs = stack([(GOE, KAO), (GOE, KAO), (GOE, KAO), (HEM, KAO)],
                bands={"view_zenith": [1.0, 1.0, 1.0, 20.0]},
                valid=[False, False, False, True])
    score = s.score(obs, None)
    assert np.isnan(score[:3]).all()
    assert winner(s, obs) == 3
    out = s.emit(obs, None)
    assert out["n_valid"][0, 0] == 1 and out["mode_count"][0, 0] == 1
    # nothing valid: nothing wins and agreement is NaN
    obs = stack([(GOE, KAO)], bands={"view_zenith": [1.0]}, valid=[False])
    assert winner(s, obs) is None
    assert np.isnan(s.emit(obs, None)["agreement"][0, 0])


def test_mode_gated_rank_normalisation() -> None:
    raw = np.array([3.0, 1.0, 1.0, np.nan, 9.0], np.float32).reshape(5, 1, 1)
    feasible = np.isfinite(raw)
    pen = rank_penalty(raw, feasible, "low")[:, 0, 0]
    # ranks among the four feasible: 1.0 and 1.0 tie for 0-1 -> 0.5; 3.0 -> 2; 9.0 -> 3; / 3
    assert pen.tolist() == pytest.approx([2 / 3, 0.5 / 3, 0.5 / 3, 1.0, 1.0])
    assert rank_penalty(raw, feasible, "high")[4, 0, 0] == 0.0
    s = ModeGatedSelect(weights=VZ_ONLY, normalize="rank")
    obs = stack([(HEM, KAO)] * 3, bands={"view_zenith": [50.0, 40.0, 45.0]})
    assert winner(s, obs) == 1                        # fixed bounds would clip all three to 1


def test_mode_gated_independent_key() -> None:
    """Each group voted on its own: G1 mode HEM (3 of 4), G2 mode KAO (3 of 4); only the looks
    holding BOTH may win."""
    pairs = [(HEM, ALU), (HEM, KAO), (GOE, KAO), (HEM, KAO)]
    obs = stack(pairs, bands={"view_zenith": [1.0, 9.0, 2.0, 7.0]})
    s = ModeGatedSelect(weights=VZ_ONLY, key="independent")
    assert winner(s, obs) == 3
    out = s.emit(obs, None)
    assert out["mode_count"][0, 0] == 2 and np.isnan(out["runner_up_count"][0, 0])


def test_tally_counts_codes_per_cell() -> None:
    code = np.array([5, 7, 5, 5, 7], np.int64).reshape(5, 1, 1)
    w = np.array([1.0, 1.0, 0.5, 1.0, 1.0]).reshape(5, 1, 1)
    voters = np.array([True, True, True, False, True]).reshape(5, 1, 1)
    votes, counts = tally(code, w, voters)
    assert votes[:, 0, 0].tolist() == [1.5, 2.0, 1.5, 0.0, 2.0]
    assert counts[:, 0, 0].tolist() == [2, 2, 2, 0, 2]


# ------------------------------------------------------------------- declarations, contract
def test_mode_gated_class_declares_its_defaults() -> None:
    """`stratum plugins show` reads the CLASS when a plugin cannot be built bare, and an
    instance otherwise: both must report what the defaults declare."""
    from click.testing import CliRunner

    from stratum.cli import main

    s = ModeGatedSelect()
    for attr in ("outputs", "required_layers", "required_roles"):
        assert getattr(ModeGatedSelect, attr) == getattr(s, attr), attr
    r = CliRunner().invoke(main, ["plugins", "show", "mode_gated_select"])
    assert r.exit_code == 0, r.output
    assert "'stack'" in r.output and "('mineral_1', 'mineral_2')" in r.output
    assert "'mode_count'" in r.output and "'selection_penalty'" in r.output
    assert "'frcov'" in r.output


def test_mode_gated_required_roles_follow_weights() -> None:
    s = ModeGatedSelect()
    assert s.capability == "stack" and s.required_layers == ("mineral_1", "mineral_2")
    assert "cloud_prob" in s.required_roles and "frcov" in s.required_roles
    v001 = ModeGatedSelect(weights={"cloud_prob": 0})
    assert "cloud_prob" not in v001.required_roles
    assert sum(v001.weights.values()) == pytest.approx(1.0)
    renamed = ModeGatedSelect(weights=VZ_ONLY, aliases={"vza": "sensor_zenith"})
    assert renamed.required_roles == ("sensor_zenith",)
    for bad in ({"weights": {"det": 1.0}}, {"weights": {"vza": -1}},
                {"weights": {m: 0 for m in METRICS}}, {"bounds": {"sza": [70, 0]}},
                {"gate": "medium"}, {"key": "pairs"}, {"tie_break": "random"},
                {"normalize": "per_tile"}, {"ignore": {"g3": []}}, {"min_count": 0},
                {"aliases": {"fit": "mineral_fit"}}, {"layers": ["a", "a"]}):
        with pytest.raises(ValueError):
            ModeGatedSelect(**bad)


def test_mode_gated_emit_outputs() -> None:
    s = ModeGatedSelect(weights=VZ_ONLY)
    names = {b.name for b in s.outputs}
    assert names == {"mode_count", "mode_votes", "n_voters", "n_valid", "agreement",
                     "runner_up_count", "selection_penalty"}
    assert {b.dtype for b in s.outputs} == {"float32"}
    # two cells, three looks: emit reuses what score computed for the SAME window
    g1 = np.array([[HEM, GOE], [HEM, GOE], [GOE, 0]], np.int32).reshape(3, 1, 2)
    g2 = np.array([[KAO, KAO], [KAO, 0], [KAO, 0]], np.int32).reshape(3, 1, 2)
    obs = ObsWindow(space="block",
                    bands={"view_zenith": np.full((3, 1, 2), 5.0, np.float32)},
                    valid=np.ones((3, 1, 2), bool), granules=[granule(i) for i in range(3)],
                    epoch=EPOCH, layers={"mineral_1": g1, "mineral_2": g2}, stacked=True,
                    layer_classes={"mineral_1": TABLE, "mineral_2": TABLE})
    s.score(obs, None)
    out = s.emit(obs, None)
    for name in names - {"selection_penalty"}:
        assert out[name].shape == (1, 2) and out[name].dtype == np.float32, name
    assert out["selection_penalty"].shape == (3, 1, 2)
    assert out["mode_count"].tolist() == [[2, 1]]
    assert out["mode_votes"].tolist() == [[2, 1]]
    assert out["n_voters"].tolist() == [[3, 2]]       # (0, 0) does not vote
    assert out["runner_up_count"].tolist() == [[1, 1]]
    assert out["agreement"][0].tolist() == pytest.approx([2 / 3, 1 / 3])


# ---------------------------------------------------------------------- through resolve
def _two_layer_schema(*scorer_layers: str) -> SnapshotSchema:
    classes = ENUMERATION.class_table()
    layers = [
        LayerSpec(name="mineral", kind="categorical", source="mineral",
                  aggregate=Aggregation("vote"), dtype="uint16", classes=classes),
        LayerSpec(name="mineral_b", kind="categorical", source="mineral",
                  aggregate=Aggregation("vote"), dtype="uint16", classes=classes),
        LayerSpec(name="acquired", kind="continuous", source="acquired",
                  aggregate=Aggregation("none"), dtype="float64"),
    ]
    layers += [LayerSpec(name=n, kind="continuous", source=f"scorer.{n}",
                         aggregate=Aggregation("none"), dtype="float32")
               for n in scorer_layers]
    return SnapshotSchema(name="mg", layers=layers)


def test_mode_gated_runs_through_resolve(tmp_path: Path) -> None:
    """A nadir look that detects nothing against an off-nadir look that detects raw 7 ("alpha"):
    where both reach a cell the detecting look wins - the gate - although a nadir-only scorer
    would take the other; where only the non-detect look reaches, it wins (no voter, no gate)."""
    zeros = np.zeros((10, 10), np.int16)
    sevens = np.full((10, 10), 7, np.int16)
    a = make_observation("A", datetime(2026, 6, 5, tzinfo=UTC), r0=0, c0=0, rows=10, cols=10,
                         view_zenith=2.0, class_ids=zeros, fill_row=False)
    b = make_observation("B", datetime(2026, 6, 9, tzinfo=UTC), r0=0, c0=5, rows=10, cols=10,
                         view_zenith=20.0, class_ids=sevens, fill_row=False)
    scorer = ModeGatedSelect(layers=("mineral", "mineral_b"), weights=VZ_ONLY)
    plan = build_plan(tmp_path, [a, b], scorer=scorer,
                      snapshot_schema=_two_layer_schema("mode_count", "agreement",
                                                        "selection_penalty"))
    snap = read_snapshot(resolve_block(item(TILE, EPOCH, 0, 0), plan).path)
    t_a, t_b = acquired_seconds(a.ref.datetime), acquired_seconds(b.ref.datetime)
    assert snap["valid"].all()
    assert (snap["acquired"][:, :5] == t_a).all()                 # A alone: no gate
    assert (snap["acquired"][:, 5:] == t_b).all()                 # both: B detects, B wins
    assert (snap["mineral"][:, 5:] == 1).all()                    # raw 7 -> product 1 "alpha"
    assert (snap["mode_count"][:, :5] == 0).all() and (snap["mode_count"][:, 5:] == 1).all()
    assert snap["agreement"][:, 5:] == pytest.approx(0.5)
    assert snap["selection_penalty"][:, 5:] == pytest.approx(20 / 30)
    assert snap["selection_penalty"][:, :5] == pytest.approx(2 / 30)
    # masking "alpha" out of group 2 by NAME leaves group 1 voting alone (partial), same winner
    masked = ModeGatedSelect(layers=("mineral", "mineral_b"), weights=VZ_ONLY,
                             g2_mask_classes=["alpha"])
    plan = build_plan(tmp_path / "m", [a, b], scorer=masked,
                      snapshot_schema=_two_layer_schema("mode_votes"))
    snap = read_snapshot(resolve_block(item(TILE, EPOCH, 0, 0), plan).path)
    assert (snap["acquired"][:, 5:] == t_b).all()
    assert snap["mode_votes"][:, 5:] == pytest.approx(0.01)
    # the mask rewrote the KEY only: the gathered group-2 layer keeps the winner's raw class
    # (documented; a manifest lists the same names in that layer's aggregate.ignore)
    assert (snap["mineral_b"][:, 5:] == 1).all()



def test_mode_gated_score_weighted_vote() -> None:
    """Three poor looks agree on one pair, two good looks on another. Counting, the three win;
    weighted by quality (1 - penalty), the two good looks do."""
    obs = stack([(HEM, KAO), (HEM, KAO), (HEM, KAO), (GOE, ALU), (GOE, ALU)],
                bands={"view_zenith": [27.0, 27.0, 27.0, 0.0, 3.0]})
    assert winner(ModeGatedSelect(weights=VZ_ONLY), obs) == 0
    s = ModeGatedSelect(weights=VZ_ONLY, vote_weight="score")
    assert winner(s, obs) == 3
    out = s.emit(obs, None)
    assert out["mode_votes"][0, 0] == pytest.approx(1.0 + 0.9)     # quality 1 - vza/30
    assert out["mode_count"][0, 0] == 2.0
    # independent key: each group's mode is weighted the same way
    s = ModeGatedSelect(weights=VZ_ONLY, vote_weight="score", key="independent")
    assert winner(s, obs) == 3
    with pytest.raises(ValueError, match="vote_weight"):
        ModeGatedSelect(vote_weight="depth")


def test_mode_gated_vote_power_sharpens() -> None:
    """Three looks at quality 0.6 against two at 0.8: 1.8 vs 1.6 unsharpened, so the three win;
    at power 4, 0.389 vs 0.819, so the two do."""
    obs = stack([(HEM, KAO)] * 3 + [(GOE, ALU)] * 2,
                bands={"view_zenith": [12.0, 12.0, 12.0, 6.0, 6.0]})
    assert winner(ModeGatedSelect(weights=VZ_ONLY, vote_weight="score"), obs) == 0
    s = ModeGatedSelect(weights=VZ_ONLY, vote_weight="score", vote_power=4)
    assert winner(s, obs) == 3
    assert s.emit(obs, None)["mode_votes"][0, 0] == pytest.approx(2 * 0.8 ** 4, rel=1e-5)
    with pytest.raises(ValueError, match="vote_power"):
        ModeGatedSelect(vote_power=0)
