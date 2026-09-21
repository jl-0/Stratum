"""A Reducer plugin replaces the aggregation vocabulary, and nothing else (04 section 5).

The built-in reducer runs what each layer's `aggregate` declares. A plugin is for what that
vocabulary cannot say - a joint vote across candidate layers - and the seam is deliberately
narrow: it receives the same `SnapshotStack`, writes the same product block, and its identity
enters the same product key.

The load-bearing test here is `test_a_run_with_no_reducer_keys_exactly_as_before`. Adding a
feature must not move a key for a run that does not use it, or every cached product in every
deployment silently rebuilds.
"""
from __future__ import annotations

import numpy as np
import pytest
from synthetic import NadirScorer, build_plan, two_overlapping
from test_reduce import cat, cont, epochs, stack

from stratum.cache import product_inputs
from stratum.manifest.validate import _reducer_problems
from stratum.reduce import N_EPOCHS, delivered_bands, product_bands
from stratum.types import BandSpec, SnapshotSchema, SnapshotStack

EPOCH_A = ["2026-06-01T00:00:00+00:00", "2026-07-01T00:00:00+00:00"]


# ------------------------------------------------------------------------------ the band list
class Counting:
    """Delivers one band the vocabulary has no name for: how many distinct classes were seen."""

    outputs = (BandSpec("n_classes", "uint16", "distinct classes over the epochs", units="count"),)
    halo = 0

    def reduce(self, snaps: SnapshotStack, aux: object) -> dict[str, np.ndarray]:
        # A plugin reads layers by the names the SCHEMA declares, not by hard-coded ones -
        # `snaps.schema` is how it learns what it was given (04 section 5).
        layer = next(a.name for a in snaps.schema.layers if a.kind == "categorical")
        vals = np.asarray(snaps[layer])
        valid = np.asarray(snaps.valid, dtype=bool)
        out = np.zeros(vals.shape[1:], dtype=np.uint16)
        for idx in np.ndindex(*vals.shape[1:]):
            seen = {int(vals[(e, *idx)]) for e in range(vals.shape[0]) if valid[(e, *idx)]}
            out[idx] = len(seen)
        return {"n_classes": out, "n_epochs": valid.sum(axis=0).astype(np.uint16)}


def schema_one_layer() -> SnapshotSchema:
    return SnapshotSchema(name="t", layers=[cat("m"), cont("d")])


def test_a_plugin_replaces_the_schemas_bands_entirely() -> None:
    """Not "adds to". The schema still says what a SNAPSHOT holds; the plugin says what the
    PRODUCT delivers, and those are different questions."""
    schema = schema_one_layer()
    from_schema = {b.name for b in delivered_bands(schema)}
    assert {"m", "m_agreement", "d", "d_n"} <= from_schema

    from_plugin = product_bands(schema, Counting())
    assert [b.name for b in from_plugin] == ["n_classes", "n_epochs"]
    assert not ({"m", "m_agreement", "d"} & {b.name for b in from_plugin})


def test_n_epochs_is_appended_when_a_plugin_forgets_it() -> None:
    """13 section 4: delivered once per product block regardless of schema. It is
    framework-knowable, so a plugin is not asked to remember it."""
    class Forgetful(Counting):
        outputs = (BandSpec("only", "uint16", "one band"),)

    bands = product_bands(schema_one_layer(), Forgetful())
    assert [b.name for b in bands] == ["only", "n_epochs"]
    assert bands[-1] == N_EPOCHS


def test_a_plugin_that_declares_its_own_n_epochs_keeps_it() -> None:
    mine = BandSpec("n_epochs", "float32", "mine, deliberately different")

    class Own(Counting):
        outputs = (BandSpec("a", "uint16", "x"), mine)

    bands = product_bands(schema_one_layer(), Own())
    assert bands == (BandSpec("a", "uint16", "x"), mine)
    assert bands.count(N_EPOCHS) == 0, "the framework must not append a second n_epochs"


def test_band_counts_are_ignored_for_a_plugin() -> None:
    """The schema alone cannot know a multi-band source's width, which is why band_counts exists.
    A plugin has no such problem - it declares its own widths."""
    bands = product_bands(schema_one_layer(), Counting(), band_counts={"n_classes": 7})
    assert bands[0].bands == 1


# ------------------------------------------------------------- the key, and what must not move
def test_a_run_with_no_reducer_keys_exactly_as_before() -> None:
    """The guard that matters. `product_inputs`' reducer field must stay None for the built-in
    path, or adding this feature rebuilds every cached product everywhere."""
    from stratum.types import canonical_hash

    without = product_inputs(["sha256:a"], [], "sha256:agg")
    explicit = product_inputs(["sha256:a"], [], "sha256:agg", None)
    assert without == explicit
    assert without["reducer"] is None
    # pinned, not merely equal to itself: this is the hash a deployment already has on disk
    assert canonical_hash(without) == canonical_hash(
        {"artifact_type": "product", "snapshot_keys": ["sha256:a"], "aux_keys": [],
         "aggregate_hash": "sha256:agg", "reducer": None})


def test_the_plugin_identity_moves_the_product_key() -> None:
    """06 section 2: a plugin's ref, version and params determine the product."""
    from stratum.types import canonical_hash

    base = product_inputs(["sha256:a"], [], "sha256:agg",
                          {"ref": "r", "version": "1", "params": {}})
    assert canonical_hash(base) != canonical_hash(product_inputs(["sha256:a"], [], "sha256:agg"))
    for changed in ({"ref": "other", "version": "1", "params": {}},
                    {"ref": "r", "version": "2", "params": {}},
                    {"ref": "r", "version": "1", "params": {"k": 1}}):
        assert canonical_hash(product_inputs(["sha256:a"], [], "sha256:agg", changed)) \
            != canonical_hash(base), f"{changed} did not move the key"


def test_the_schemas_aggregate_hash_still_counts_under_a_plugin() -> None:
    """The schema governs what the SNAPSHOTS hold, so it determines the product even when a
    plugin decides how they collapse."""
    from stratum.types import canonical_hash

    ident = {"ref": "r", "version": "1", "params": {}}
    a = product_inputs(["sha256:a"], [], "sha256:agg-one", ident)
    b = product_inputs(["sha256:a"], [], "sha256:agg-two", ident)
    assert canonical_hash(a) != canonical_hash(b)


# ------------------------------------------------------------------------------ the whole stage
def test_a_plugin_runs_through_the_real_reduce_stage(tmp_path) -> None:
    """Through `reduce_item`, not a stub: the plugin's arrays reach a written product block with
    the bands it declared, and the built-in reducer never runs."""
    from stratum.executors.worker import reduce_item
    from stratum.plan.document import RunPlan

    plan = build_plan(tmp_path, two_overlapping(), scorer=NadirScorer(), reducer=Counting())
    assert plan.reducer is not None and plan.reducer.instance.outputs[0].name == "n_classes"

    item = {"tile": [0, 0], "epoch": EPOCH_A, "block": [0, 0]}
    from stratum.resolve import resolve_block
    resolve_block(item, plan)

    from datetime import UTC, datetime

    from stratum.types import Epoch
    period = Epoch(datetime(2026, 6, 1, tzinfo=UTC), datetime(2026, 7, 1, tzinfo=UTC))
    run = RunPlan(run_id="r", run_label="r", manifest_hash="sha256:m",
                  manifest_path=tmp_path / "m.yaml", root=tmp_path,
                  run_dir=tmp_path / "runs" / "r", products_dir=tmp_path / "p",
                  context=plan, outputs={}, tiles=[], epochs=[period], periods=[],
                  band_counts={}, budget={}, document={})
    reduce = {"tile": [0, 0], "block": [0, 0], "period": EPOCH_A, "epochs": [EPOCH_A]}
    key, hit = reduce_item(reduce, run)

    assert not hit
    written = sorted(p.stem for p in key.path.glob("*.tif"))
    assert written == ["n_classes", "n_epochs"], written


def test_a_plugin_that_does_not_deliver_what_it_declared_is_caught_by_name() -> None:
    """A plugin's `outputs` are the contract; publish stitches exactly those. A mismatch is
    reported against the plugin rather than surfacing as a KeyError inside the writer."""
    from stratum.executors.worker import _check_plugin_output

    bands = (BandSpec("a", "uint16", "x"), BandSpec("b", "uint16", "y"))
    with pytest.raises(ValueError, match="did not deliver \\['b'\\]"):
        _check_plugin_output("mine", {"a": np.zeros((2, 2))}, bands)
    with pytest.raises(ValueError, match="delivered undeclared \\['c'\\]"):
        _check_plugin_output("mine", {"a": 1, "b": 2, "c": 3}, bands)


# --------------------------------------------------------------------------------- declaration
def test_what_a_reducer_must_declare() -> None:
    """04 section 5: up front, so a bad declaration fails at plan time rather than after the
    first block has been reduced."""
    class Bad:
        outputs = ()
        halo = 0

    assert any("declares no `outputs`" in p for p in _reducer_problems("bad", Bad()))

    class Haloed(Counting):
        halo = 2

    problems = _reducer_problems("haloed", Haloed())
    assert any("halo 2 is not built" in p and "CORE extent" in p for p in problems)

    class Aux(Counting):
        required_aux = ("slope",)

    assert any("NullAux" in p for p in _reducer_problems("aux", Aux()))
    assert _reducer_problems("good", Counting()) == []


# ------------------------------------------------------------------------------- round-tripping
def test_a_reducer_survives_the_plan_document(tmp_path) -> None:
    from stratum.plan.document import context_from_doc, context_to_doc

    plan = build_plan(tmp_path, two_overlapping(), scorer=NadirScorer(), reducer=Counting())
    doc = context_to_doc(plan)
    assert doc["reducer"]["ref"].endswith(":Counting")

    back = context_from_doc(doc, tmp_path)
    assert back.reducer is not None
    assert back.reducer.ref == plan.reducer.ref
    assert [b.name for b in back.reducer.instance.outputs] == ["n_classes"]


def test_a_plan_written_without_a_reducer_still_loads(tmp_path) -> None:
    """PLAN_SCHEMA_VERSION was deliberately not bumped, so plan.json files written before
    reducers existed - which carry no `reducer` key at all - must still load."""
    from stratum.plan.document import context_from_doc, context_to_doc

    plan = build_plan(tmp_path, two_overlapping(), scorer=NadirScorer())
    doc = context_to_doc(plan)
    assert doc["reducer"] is None
    doc.pop("reducer")                      # a document from before the field existed
    assert context_from_doc(doc, tmp_path).reducer is None


def test_the_builtin_path_is_untouched() -> None:
    """Belt and braces: with no plugin, `product_bands` is `delivered_bands`, exactly.

    Compared field-wise rather than by equality, because a BandSpec's nodata is often NaN and
    NaN never equals itself - a dataclass `==` would quietly report "different" forever.
    """
    def shape(bands):
        return [(b.name, b.dtype, b.units, b.bands, repr(b.nodata)) for b in bands]

    schema = schema_one_layer()
    assert shape(product_bands(schema)) == shape(delivered_bands(schema))
    assert shape(product_bands(schema, None, band_counts={"d": 3})) == \
        shape(delivered_bands(schema, band_counts={"d": 3}))


def test_the_stack_a_plugin_receives_is_the_one_the_builtin_gets() -> None:
    """Same input, different vocabulary. A plugin reads layers by the names the schema declares."""
    vals = np.array([[[1, 2]], [[1, 3]]], dtype=np.uint16)
    snaps = stack({"m": vals}, valid=np.ones(vals.shape, dtype=bool),
                  specs=[cat("m")], eps=epochs(2))
    out = Counting().reduce(snaps, None)
    assert out["n_classes"].tolist() == [[1, 2]]
    assert out["n_epochs"].tolist() == [[2, 2]]


# ------------------------------------------------- the shipped plugin: JointMineralVote
def joint_stack(g1: list[list[int]], g2: list[list[int]], *, valid=None) -> SnapshotStack:
    """A stack with two categorical layers, epochs-first. Each argument is one epoch's row."""
    a = np.array([[row] for row in g1], dtype=np.uint16)          # (n, 1, W)
    b = np.array([[row] for row in g2], dtype=np.uint16)
    v = np.ones(a.shape, dtype=bool) if valid is None else np.asarray(valid, dtype=bool)
    return stack({"mineral_1": a, "mineral_2": b}, valid=v,
                 specs=[cat("mineral_1"), cat("mineral_2")], eps=epochs(a.shape[0]))


def test_joint_prefers_group_2_and_falls_back_to_group_1() -> None:
    """The rule, cell by cell, over two epochs with `min_count: 2` - so a class must appear in
    BOTH epochs to win.

      col 0  group 1 unanimous; group 2 sees 5 once, below min_count      -> group 1
      col 1  group 1 unanimous; group 2 is all `none`, which is ignored   -> group 1
      col 2  both unanimous                                              -> group 2 preferred
      col 3  group 1 splits 7/9, group 2 silent                          -> neither, nodata
    """
    from stratum_emit.reducers import JointMineralVote

    from stratum.reduce import CATEGORICAL_NODATA as ND

    #                 col:     0    1    2    3
    g1 = [[7, 7, 7, 7],     # epoch 0
          [7, 7, 7, 9]]     # epoch 1
    g2 = [[0, 0, 5, 0],
          [5, 0, 5, 0]]
    out = JointMineralVote(min_count=2).reduce(joint_stack(g1, g2), None)

    joint, whence = out["mineral_joint"][0], out["mineral_joint_from"][0]
    assert joint.tolist() == [7, 7, 5, ND]
    assert whence.tolist() == [1, 1, 2, 0], "only column 2 comes from group 2"


def test_joint_is_nodata_where_neither_group_voted() -> None:
    from stratum_emit.reducers import JointMineralVote

    from stratum.reduce import CATEGORICAL_NODATA as ND

    out = JointMineralVote(min_count=2).reduce(joint_stack([[0, 7], [0, 7]], [[0, 0], [0, 0]]),
                                               None)
    assert out["mineral_joint"][0].tolist() == [ND, 7]
    assert out["mineral_joint_from"][0].tolist() == [0, 1]
    assert np.isnan(out["mineral_joint_agreement"][0][0])


def test_joint_agreement_follows_whichever_group_answered() -> None:
    """A combined band whose agreement came from the layer that did not win would be a lie."""
    from stratum_emit.reducers import JointMineralVote

    # col 0: group 2 unanimous over 2 epochs. col 1: group 2 silent, group 1 unanimous.
    out = JointMineralVote(min_count=2).reduce(joint_stack([[7, 7], [7, 7]], [[5, 0], [5, 0]]),
                                               None)
    assert out["mineral_joint_from"][0].tolist() == [2, 1]
    assert out["mineral_joint_agreement"][0].tolist() == [1.0, 1.0]


def test_joint_reuses_the_frameworks_vote_rather_than_its_own() -> None:
    """Where only one group has data, the joint answer must equal what the vocabulary would
    have delivered for that layer alone - same tie-breaks, same min_count, same agreement."""
    from stratum_emit.reducers import JointMineralVote

    from stratum.reduce import VoteParams, vote

    snaps = joint_stack([[7, 3], [7, 3]], [[0, 0], [0, 0]])
    got = JointMineralVote(min_count=2).reduce(snaps, None)
    alone = vote(snaps.schema["mineral_1"], np.asarray(snaps["mineral_1"]),
                 np.asarray(snaps.valid), np.asarray(snaps.score, dtype=np.float32),
                 np.asarray(snaps.valid).sum(axis=0),
                 VoteParams(min_count=2, ignore=("none",), tie_break="highest_score"))
    assert got["mineral_joint"].tolist() == alone["mineral_1"].tolist()
    assert got["mineral_joint_agreement"].tolist() == alone["mineral_1_agreement"].tolist()


def test_joint_says_which_layers_it_needs() -> None:
    """A schema that does not declare both layers fails with the names, not a bare KeyError."""
    from stratum_emit.reducers import JointMineralVote

    snaps = stack({"m": np.ones((2, 1, 1), dtype=np.uint16)},
                  valid=np.ones((2, 1, 1), dtype=bool), specs=[cat("m")], eps=epochs(2))
    with pytest.raises(KeyError, match="mineral_1"):
        JointMineralVote().reduce(snaps, None)


def test_joint_refuses_to_prefer_a_layer_over_itself() -> None:
    from stratum_emit.reducers import JointMineralVote

    with pytest.raises(ValueError, match="different layers"):
        JointMineralVote(prefer="mineral_1", fallback="mineral_1")


def test_joint_is_registered_and_resolvable_by_name() -> None:
    from stratum_emit.reducers import JointMineralVote

    from stratum.plugins import resolve
    assert resolve("reducer", "joint_mineral_vote") is JointMineralVote


# ------------------------------------------------------------------------ SpectralAbundance
def _abundance_stack(ids, depth, valid=None):
    """A SnapshotStack over one categorical layer and its depth, with a 4-class raw table."""
    import numpy as np
    import pyarrow as pa

    from stratum.types import Aggregation, ClassTable, LayerSpec, SnapshotSchema, SnapshotStack
    table = ClassTable(key="id", entries=pa.table(
        {"id": [0, 1, 2, 3], "name": ["none", "Goethite WS222", "Hematite GDS27",
                                      "nHematit+fg-Goethit"]}),
        source="enumeration:source:test@1")
    schema = SnapshotSchema(name="ab", layers=[
        LayerSpec(name="m", kind="categorical", source="m", classes=table,
                  aggregate=Aggregation("vote"), dtype="uint16"),
        LayerSpec(name="d", kind="continuous", source="d", aggregate=Aggregation("median"),
                  dtype="float32")])
    ids = np.asarray(ids, dtype=np.uint16)
    depth = np.asarray(depth, dtype=np.float32)
    valid = np.ones(ids.shape, bool) if valid is None else np.asarray(valid, bool)
    return SnapshotStack(schema=schema, epochs=[None] * ids.shape[0],
                         layers={"m": ids, "d": depth}, valid=valid,
                         score=np.zeros(ids.shape, np.float32))


WEIGHTS = {"Goethite WS222": {"goethite": 1.0},
           "Hematite GDS27": {"hematite": 1.0},
           "nHematit+fg-Goethit": {"goethite": 0.10, "hematite": 0.05}}


def test_one_constituent_reaches_two_output_classes():
    """The thing an Enumeration refuses: `classes.py:resolve` raises 'raw key N claimed by both'.
    A reducer can, and the fractions differ per class."""
    import numpy as np
    from stratum_emit.reducers import SpectralAbundance

    r = SpectralAbundance(mineral="m", depth="d", classes=["goethite", "hematite"],
                          weights=WEIGHTS, min_epochs=1)
    # two epochs, both won by the shared constituent (id 3), depth 0.40
    snaps = _abundance_stack(np.full((2, 1, 1), 3), np.full((2, 1, 1), 0.40))
    out = r.reduce(snaps, None)
    assert out["abundance_goethite"][0, 0] == pytest.approx(0.040)     # 0.40 x 0.10
    assert out["abundance_hematite"][0, 0] == pytest.approx(0.020)     # 0.40 x 0.05
    assert out["abundance_total"][0, 0] == pytest.approx(0.060)
    assert out["abundance_goethite_n"][0, 0] == 2


def test_the_multiply_happens_per_epoch_not_on_the_aggregate():
    """"Apply the mineral abundance calculations a priori." Two epochs whose winners have
    DIFFERENT fractions must not be averaged before the multiply - that would use one epoch's
    fraction on the other's depth."""
    import numpy as np
    from stratum_emit.reducers import SpectralAbundance

    r = SpectralAbundance(mineral="m", depth="d", classes=["goethite"], weights=WEIGHTS,
                          min_epochs=1, method="mean")
    ids = np.array([[[1]], [[3]]], dtype=np.uint16)          # pure goethite, then the mixture
    depth = np.array([[[0.20]], [[0.60]]], dtype=np.float32)
    out = r.reduce(_abundance_stack(ids, depth), None)
    # per epoch: 0.20 x 1.0 = 0.20, then 0.60 x 0.10 = 0.06 -> mean 0.13
    assert out["abundance_goethite"][0, 0] == pytest.approx(0.13)
    # the wrong answer, had it aggregated first: mean depth 0.40 x either fraction
    assert out["abundance_goethite"][0, 0] != pytest.approx(0.40)
    assert out["abundance_goethite"][0, 0] != pytest.approx(0.04)


def test_epochs_where_the_class_was_absent_do_not_drag_the_estimate_down():
    """Averaging in the months a mineral was not detected would scale every abundance by how
    often the cell was looked at, which is revisit and not geology."""
    import numpy as np
    from stratum_emit.reducers import SpectralAbundance

    r = SpectralAbundance(mineral="m", depth="d", classes=["goethite"], weights=WEIGHTS,
                          min_epochs=1, method="median")
    ids = np.array([[[1]], [[2]], [[2]]], dtype=np.uint16)   # goethite once, hematite twice
    depth = np.full((3, 1, 1), 0.30, dtype=np.float32)
    out = r.reduce(_abundance_stack(ids, depth), None)
    assert out["abundance_goethite"][0, 0] == pytest.approx(0.30)   # not 0.10
    assert out["abundance_goethite_n"][0, 0] == 1
    assert out["n_epochs"][0, 0] == 3


def test_min_epochs_withholds_a_cell_seen_too_few_times():
    import numpy as np
    from stratum_emit.reducers import SpectralAbundance

    r = SpectralAbundance(mineral="m", depth="d", classes=["goethite"], weights=WEIGHTS,
                          min_epochs=2)
    out = r.reduce(_abundance_stack(np.full((1, 1, 1), 1), np.full((1, 1, 1), 0.3)), None)
    assert np.isnan(out["abundance_goethite"][0, 0])
    assert np.isnan(out["abundance_total"][0, 0])


def test_a_declared_class_no_constituent_reaches_still_ships_as_a_band():
    import numpy as np
    from stratum_emit.reducers import SpectralAbundance

    r = SpectralAbundance(mineral="m", depth="d", classes=["goethite", "jarosite"],
                          weights=WEIGHTS, min_epochs=1)
    assert "abundance_jarosite" in {s.name for s in r.outputs}
    out = r.reduce(_abundance_stack(np.full((1, 1, 1), 1), np.full((1, 1, 1), 0.3)), None)
    assert np.isnan(out["abundance_jarosite"][0, 0])


def test_a_weight_key_that_matches_no_class_is_an_error_not_a_silent_zero():
    """Almost always a vintage mismatch or a typo, and both look identical in the output."""
    import numpy as np
    from stratum_emit.reducers import SpectralAbundance

    r = SpectralAbundance(mineral="m", depth="d", classes=["goethite"],
                          weights={"Goethite WS222": {"goethite": 1.0},
                                   "Goethite FROM ANOTHER VINTAGE": {"goethite": 1.0}},
                          min_epochs=1)
    with pytest.raises(KeyError, match="different product vintage"):
        r.reduce(_abundance_stack(np.full((1, 1, 1), 1), np.full((1, 1, 1), 0.3)), None)


def test_spectral_abundance_refuses_configuration_that_cannot_mean_anything():
    from stratum_emit.reducers import SpectralAbundance

    with pytest.raises(ValueError, match="not one of"):
        SpectralAbundance(mineral="m", depth="d", classes=["a"], weights={}, method="best")
    with pytest.raises(ValueError, match="min_epochs"):
        SpectralAbundance(mineral="m", depth="d", classes=["a"], weights={}, min_epochs=0)
    with pytest.raises(ValueError, match="at least one output class"):
        SpectralAbundance(mineral="m", depth="d", classes=[], weights={})
    with pytest.raises(ValueError, match="duplicate output class"):
        SpectralAbundance(mineral="m", depth="d", classes=["a", "a"], weights={})
    with pytest.raises(ValueError, match="outside"):
        SpectralAbundance(mineral="m", depth="d", classes=["a"], weights={"X": {"a": 1.4}})


def test_weights_may_name_classes_this_run_does_not_deliver():
    """One weights table for the whole EMIT-10 serving a run that delivers two of them is the
    expected pattern; refusing it would force a weights file per run."""
    import numpy as np
    from stratum_emit.reducers import SpectralAbundance

    r = SpectralAbundance(mineral="m", depth="d", classes=["goethite"], weights=WEIGHTS,
                          min_epochs=1)
    assert {s.name for s in r.outputs} >= {"abundance_goethite", "abundance_goethite_n"}
    assert not any(s.name.startswith("abundance_hematite") for s in r.outputs)
    out = r.reduce(_abundance_stack(np.full((1, 1, 1), 3), np.full((1, 1, 1), 0.40)), None)
    assert out["abundance_goethite"][0, 0] == pytest.approx(0.040)


def test_spectral_abundance_needs_the_layers_it_names():
    import numpy as np
    from stratum_emit.reducers import SpectralAbundance

    r = SpectralAbundance(mineral="nope", depth="d", classes=["goethite"], weights=WEIGHTS)
    with pytest.raises(KeyError, match="does not declare"):
        r.reduce(_abundance_stack(np.full((1, 1, 1), 1), np.full((1, 1, 1), 0.3)), None)
