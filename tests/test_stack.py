"""`capability = "stack"` scorers (04 section 4, 11 section 5): one `score()` call per block with
every observation of the epoch, the argmax along the observation axis, and the invariant that a
stack scorer equivalent to a streaming one writes the same snapshot, byte for byte."""
from __future__ import annotations

import re
import warnings
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import numpy as np
import pytest
from stratum_emit.masks import EdgeTrim
from synthetic import (
    EPOCH,
    TILE,
    NadirScorer,
    NeighbourScorer,
    ShallowIsNan,
    build_plan,
    item,
    make_observation,
    resolve_tile,
    schema,
    two_overlapping,
    with_block,
)

from stratum.resolve import (
    AliasBinding,
    PlanContext,
    read_snapshot,
    resolve_block,
    stack_argmax,
)
from stratum.types import (
    Aggregation,
    BandSpec,
    LayerSpec,
    ObsWindow,
    SnapshotSchema,
    TileRef,
    acquired_seconds,
)

pytestmark = pytest.mark.filterwarnings("ignore::rasterio.errors.NotGeoreferencedWarning")

#: The snapshot key of `test_resolve.py::test_resolve_block_writes_the_snapshot`'s block, recorded
#: before the stack path existed. A run without a stack scorer must keep producing it.
STREAMING_KEY = "sha256:d007cc58ea88bebd80af62a7e561324866f8d50955aeac422f3db5a22b20296a"


# --------------------------------------------------------------------------------- scorers
class StackOf:
    """A stack scorer that loops over the observations and asks a streaming scorer about each:
    the per-observation form of a stack scorer, and the twin of `inner` by construction."""

    capability = "stack"

    def __init__(self, inner: Any, extra_roles: tuple[str, ...] = (), passes: int = 1) -> None:
        self.inner = inner
        self.halo = inner.halo
        self.required_roles = tuple(inner.required_roles) + tuple(extra_roles)
        self.required_aux = tuple(inner.required_aux)
        self.passes = passes
        self.seen: list[ObsWindow] = []

    def score(self, obs: ObsWindow, aux: Any) -> np.ndarray:
        self.seen.append(obs)
        out = None
        for _ in range(self.passes):
            out = np.stack([np.asarray(self.inner.score(o, aux)) for o in obs])
        return out


class StackNadir:
    """MinViewZenith over the whole stack at once."""

    capability = "stack"
    halo = 0
    required_roles = ("view_zenith",)
    required_aux: tuple[str, ...] = ()

    def score(self, obs: ObsWindow, aux: Any) -> np.ndarray:
        return -obs["view_zenith"]


class TileNadir(StackNadir):
    capability = "tile"


class BogusNadir(StackNadir):
    capability = "bogus"


class LayerReader(StackNadir):
    """Declares a snapshot layer it reads; records what `obs.layer` returned."""

    required_layers = ("mineral",)

    def __init__(self) -> None:
        self.layers: list[np.ndarray] = []

    def score(self, obs: ObsWindow, aux: Any) -> np.ndarray:
        self.layers.append(np.array(obs.layer("mineral")))
        return -obs["view_zenith"]


class UnknownLayer(StackNadir):
    required_layers = ("no_such_layer",)


class Raises(StackNadir):
    def score(self, obs: ObsWindow, aux: Any) -> np.ndarray:
        raise AssertionError("score() must not be called on an empty block")


class MinusInf:
    """Every look scores -inf: the earliest VALID look must win, never an invalid row 0."""

    capability = "streaming"
    halo = 0
    required_roles = ("geometry",)
    required_aux: tuple[str, ...] = ()

    def score(self, obs: ObsWindow, aux: Any) -> np.ndarray:
        return np.full(obs.valid.shape, -np.inf, dtype=np.float32)


class Bare:
    """A scorer class that declares no capability at all."""

    halo = 0
    required_roles = ("geometry",)
    required_aux: tuple[str, ...] = ()

    def score(self, obs: ObsWindow, aux: Any) -> np.ndarray:
        return -obs["view_zenith"]


# ---------------------------------------------------------------------------------- helpers
def _with_acquired() -> SnapshotSchema:
    base = schema()
    return SnapshotSchema(name=base.name, layers=[
        *base.layers,
        LayerSpec(name="acquired", kind="continuous", source="acquired",
                  aggregate=Aggregation("mean"), dtype="float64")])


def _tifs(path: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(path.glob("*.tif"))}


def _assert_same_snapshot(a: Path, b: Path) -> None:
    ta, tb = _tifs(a), _tifs(b)
    assert set(ta) == set(tb) and ta
    for name in ta:
        assert ta[name] == tb[name], name
    sa, sb = read_snapshot(a), read_snapshot(b)
    assert set(sa) == set(sb)
    for name in sa:
        assert sa[name].dtype == sb[name].dtype, name
        assert np.array_equal(sa[name], sb[name], equal_nan=sa[name].dtype.kind == "f"), name


def _blocks(plan: PlanContext) -> list[tuple[int, int]]:
    return [(b.bx, b.by) for b in TileRef(plan.grid, TILE.tx, TILE.ty).blocks()]


def _resolve_pair(tmp_path: Path, observations: list, streaming: Any, stack: Any, *,
                  masks: tuple = (), snapshot_schema: SnapshotSchema | None = None,
                  ) -> list[tuple[Path, Path]]:
    a = build_plan(tmp_path / "streaming", observations, scorer=streaming, masks=list(masks),
                   snapshot_schema=snapshot_schema)
    b = build_plan(tmp_path / "stack", observations, scorer=stack, masks=list(masks),
                   snapshot_schema=snapshot_schema)
    out = []
    for bx, by in _blocks(a):
        out.append((resolve_block(item(TILE, EPOCH, bx, by), a).path,
                    resolve_block(item(TILE, EPOCH, bx, by), b).path))
    return out


# ------------------------------------------------------------------------- byte identity
EQUIVALENT = {
    "nadir-loop": (lambda: NadirScorer(), lambda: StackOf(NadirScorer()), ()),
    "nadir-vectorised": (lambda: NadirScorer(), lambda: StackNadir(), ()),
    # ShallowIsNan reads `depth`, which streaming happens to carry because the schema reads it;
    # a stack window holds only what the scorer declares, so its twin declares it
    "shallow-loop": (lambda: ShallowIsNan(0.5), lambda: StackOf(ShallowIsNan(0.5), ("depth",)),
                     ()),
    "neighbour-halo1-loop": (lambda: NeighbourScorer(), lambda: StackOf(NeighbourScorer()), ()),
    "nadir-edgetrim-loop": (lambda: NadirScorer(), lambda: StackOf(NadirScorer()),
                            (EdgeTrim(5),)),
    "nadir-edgetrim-vectorised": (lambda: NadirScorer(), lambda: StackNadir(), (EdgeTrim(5),)),
}


@pytest.mark.parametrize("case", sorted(EQUIVALENT))
def test_stack_equals_streaming_byte_for_byte(tmp_path: Path, case: str) -> None:
    streaming, stack, masks = EQUIVALENT[case]
    pairs = _resolve_pair(tmp_path, two_overlapping(), streaming(), stack(), masks=masks,
                          snapshot_schema=_with_acquired())
    assert len(pairs) == 4
    won = 0
    for a, b in pairs:
        _assert_same_snapshot(a, b)
        won += int(read_snapshot(a)["valid"].sum())
    assert won > 50, "something real was resolved"


def test_stack_ties_go_to_the_earliest(tmp_path: Path) -> None:
    a = make_observation("A", datetime(2026, 6, 5, tzinfo=UTC), r0=2, c0=2, rows=8, cols=8,
                         view_zenith=10.0)
    b = make_observation("B", datetime(2026, 6, 4, tzinfo=UTC), r0=2, c0=2, rows=8, cols=8,
                         view_zenith=10.0, class_ids=np.full((8, 8), 5))
    [(sa, sb)] = _resolve_pair(tmp_path, [a, b], NadirScorer(), StackNadir(),
                               snapshot_schema=_with_acquired())[:1]
    _assert_same_snapshot(sa, sb)
    snap = read_snapshot(sb)
    assert snap["acquired"][6, 6] == acquired_seconds(b.ref.datetime)   # B is earlier
    assert snap["mineral"][6, 6] == 2


def test_stack_minus_inf_best_does_not_pick_an_invalid_row(tmp_path: Path) -> None:
    # unit: row 0 invalid (and would score higher), row 1 is -inf everywhere
    score = np.stack([np.full((2, 2), 5.0, np.float32), np.full((2, 2), -np.inf, np.float32)])
    valid = np.stack([np.zeros((2, 2), bool), np.ones((2, 2), bool)])
    idx, won, best = stack_argmax(score, valid)
    assert (idx == 1).all() and won.all() and (best == -np.inf).all()
    # nothing valid: not won, best NaN
    idx, won, best = stack_argmax(score, np.zeros_like(valid))
    assert not won.any() and np.isnan(best).all()
    # through resolve: B-only cells, where A (row 0) never reached, go to B as streaming does
    pairs = _resolve_pair(tmp_path, two_overlapping(), MinusInf(), StackOf(MinusInf()),
                          snapshot_schema=_with_acquired())
    for a, b in pairs:
        _assert_same_snapshot(a, b)
    snap = read_snapshot(pairs[3][1])                    # block (1, 1): cells [10, 20)
    b_start = acquired_seconds(two_overlapping()[1].ref.datetime)
    assert snap["valid"][7, 7] and snap["acquired"][7, 7] == b_start   # tile (17, 17): B only


# --------------------------------------------------------------------------- the window
def _three() -> list:
    when = datetime(2026, 6, 5, tzinfo=UTC)
    return [
        make_observation("C", datetime(2026, 6, 9, tzinfo=UTC), r0=2, c0=2, rows=8, cols=8,
                         view_zenith=30.0),
        make_observation("B", when, r0=2, c0=2, rows=8, cols=8, view_zenith=20.0,
                         class_ids=np.full((8, 8), 5)),
        make_observation("A", when, r0=2, c0=2, rows=8, cols=8, view_zenith=10.0),
    ]


def test_at_and_iteration_equal_the_slices(tmp_path: Path) -> None:
    scorer = StackOf(NadirScorer())
    plan = build_plan(tmp_path, _three(), scorer=scorer)
    resolve_block(item(TILE, EPOCH, 0, 0), plan)
    [obs] = scorer.seen
    assert obs.stacked and obs.n == 3 and obs.granule is None
    # ordered by (datetime, granule_id): A and B share a datetime
    assert [g.granule_id for g in obs.granules] == ["A", "B", "C"]
    assert obs.valid.shape == (3, 10, 10) and obs.interpolated.shape == (3, 10, 10)
    assert obs.coords.lon.shape == (10, 10) and obs.block is not None
    names = sorted(obs._bands)
    assert names == ["geometry", "view_zenith"]
    views = list(obs)
    assert len(views) == 3
    for i, o in enumerate(views):
        assert o.n == 1 and not o.stacked and o.granule == obs.granules[i]
        assert o.coords is obs.coords and o.epoch == obs.epoch
        for k in names:
            assert np.array_equal(o[k], obs[k][i]) and np.shares_memory(o[k], obs[k])
        for k in ("mineral", "depth"):
            assert np.array_equal(o.layer(k), obs.layer(k)[i])
            assert np.shares_memory(o.layer(k), obs.layer(k))
        assert np.array_equal(o.valid, obs.valid[i]) and np.shares_memory(o.valid, obs.valid)
        assert np.array_equal(o.interpolated, obs.interpolated[i])
        assert np.shares_memory(o.interpolated, obs.interpolated)
        assert obs.at(i - 3).granule == o.granule                      # negative indices
        assert list(o) == [o] and o.at(0) is o
    with pytest.raises(IndexError):
        obs.at(3)
    with pytest.raises(IndexError):
        views[0].at(1)
    # N = 1 is still a stack: granule stays None
    one = StackOf(NadirScorer())
    plan1 = build_plan(tmp_path / "one", _three()[:1], scorer=one)
    resolve_block(item(TILE, EPOCH, 0, 0), plan1)
    [o1] = one.seen
    assert o1.stacked and o1.n == 1 and o1.granule is None
    assert o1.at(0).granule == o1.granules[0]


def test_stacked_window_checks_its_leading_axis() -> None:
    ok = np.zeros((2, 3, 3), bool)
    with pytest.raises(ValueError, match="'x' has leading axis 3; expected n = 2"):
        ObsWindow(space="block", bands={"x": np.zeros((3, 3, 3))}, valid=ok,
                  granules=two_overlapping_refs(), epoch=EPOCH, stacked=True)
    with pytest.raises(ValueError, match="'valid' has shape"):
        ObsWindow(space="block", bands={}, valid=ok[0], granules=two_overlapping_refs(),
                  epoch=EPOCH, stacked=True)


def two_overlapping_refs() -> tuple:
    return tuple(o.ref for o in two_overlapping())


def test_stack_reads_each_granule_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import stratum.resolve.block as block_mod

    calls: list[str] = []
    real = block_mod.read_observation

    def counting(ctx: Any) -> Any:
        calls.append(ctx.granule.granule_id)
        return real(ctx)

    monkeypatch.setattr(block_mod, "read_observation", counting)
    scorer = StackOf(NadirScorer(), passes=3)
    plan = build_plan(tmp_path, two_overlapping(), scorer=scorer)
    from stratum.resolve import ResolveItem, reached_observations

    parsed = ResolveItem.parse(item(TILE, EPOCH, 0, 0), plan)
    reached = reached_observations(plan, parsed)
    resolve_block(item(TILE, EPOCH, 0, 0), plan)
    assert calls == [g.granule_id for g, _, _ in reached] == ["A", "B"]


def test_stack_keeps_only_declared_names(tmp_path: Path) -> None:
    class Probe(StackNadir):
        seen: ClassVar[list] = []

        def score(self, obs: ObsWindow, aux: Any) -> np.ndarray:
            Probe.seen.append(obs)
            return super().score(obs, aux)

    plan = build_plan(tmp_path, two_overlapping(), scorer=Probe())
    # a second alias of the same role, which the scorer does not declare
    plan = replace(plan, aliases={**plan.aliases, "solar_zenith": AliasBinding("geometry", 1)})
    resolve_block(item(TILE, EPOCH, 0, 0), plan)
    [obs] = Probe.seen
    assert sorted(obs._bands) == ["view_zenith"]
    for name in ("solar_zenith", "geometry", "depth", "mineral"):
        with pytest.raises(KeyError, match="required_roles"):
            obs[name]
    vz = obs["view_zenith"]
    # a copied band, not a view into the two-band geometry array
    assert vz.shape == (2, 10, 10) and vz.base is not None and vz.base.ndim == 3


def test_stack_buffers_are_read_only(tmp_path: Path) -> None:
    scorer = StackOf(NadirScorer())
    plan = build_plan(tmp_path, two_overlapping(), scorer=scorer)
    resolve_block(item(TILE, EPOCH, 0, 0), plan)
    [obs] = scorer.seen
    for arr in (obs["view_zenith"], obs["geometry"], obs.valid, obs.layer("mineral"),
                obs.at(0)["view_zenith"]):
        with pytest.raises(ValueError, match="read-only"):
            arr[0, 0] = 1


def test_stack_empty_block_matches_streaming(tmp_path: Path) -> None:
    obs = [make_observation("A", datetime(2026, 6, 5, tzinfo=UTC), r0=2, c0=2, rows=6, cols=6,
                            view_zenith=10.0)]
    a = build_plan(tmp_path / "streaming", obs, scorer=NadirScorer())
    b = build_plan(tmp_path / "stack", obs, scorer=Raises())
    ka = resolve_block(item(TILE, EPOCH, 1, 1), a)
    kb = resolve_block(item(TILE, EPOCH, 1, 1), b)       # nothing reaches (1, 1)
    assert b.cache.explain(kb)["obs_keys"] == []
    _assert_same_snapshot(ka.path, kb.path)
    assert not read_snapshot(kb.path)["valid"].any()


def test_stack_wrong_shape_is_refused(tmp_path: Path) -> None:
    class Flat(StackNadir):
        def score(self, obs: ObsWindow, aux: Any) -> np.ndarray:
            return -obs["view_zenith"][0]

    plan = build_plan(tmp_path, two_overlapping(), scorer=Flat())
    with pytest.raises(ValueError, match=re.escape("expected (N, H, W) = (2, 10, 10)")):
        resolve_block(item(TILE, EPOCH, 0, 0), plan)


def test_stack_cross_observation_scorer(tmp_path: Path) -> None:
    """Closest to the per-cell median view zenith: needs every look at once."""

    class NearMedian(StackNadir):
        def score(self, obs: ObsWindow, aux: Any) -> np.ndarray:
            vz = np.where(obs.valid, obs["view_zenith"], np.nan)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)     # all-NaN cells
                med = np.nanmedian(vz, axis=0)
            return -np.abs(obs["view_zenith"] - med[None])

    looks = [make_observation(g, datetime(2026, 6, d, tzinfo=UTC), r0=2, c0=2, rows=8, cols=8,
                              view_zenith=vz, class_ids=np.full((8, 8), raw), fill_row=False)
             for g, d, vz, raw in (("A", 5, 10.0, 7), ("B", 6, 20.0, 5), ("C", 7, 30.0, 7))]
    plan = build_plan(tmp_path, looks, scorer=NearMedian(), snapshot_schema=_with_acquired())
    snap = read_snapshot(resolve_block(item(TILE, EPOCH, 0, 0), plan).path)
    valid = snap["valid"]
    assert valid.sum() > 30
    assert (snap["mineral"][valid] == 2).all()                       # B's raw 5 -> product 2
    assert (snap["acquired"][valid] == acquired_seconds(looks[1].ref.datetime)).all()
    assert (snap["score"][valid] == 0).all()


@pytest.mark.parametrize("stack", [StackNadir(), StackOf(NeighbourScorer())],
                         ids=["vectorised", "halo1-loop"])
def test_stack_seam_equivalence(tmp_path: Path, stack: Any) -> None:
    """01 section 3 invariant for a stack scorer: block size never changes output."""
    plan = build_plan(tmp_path, two_overlapping(), scorer=stack, masks=[EdgeTrim(5)])
    whole = resolve_tile(with_block(plan, 20))
    quarters = resolve_tile(plan)
    slivers = resolve_tile(with_block(plan, 7))
    assert whole["valid"].sum() > 50
    for name in whole:
        assert np.array_equal(whole[name], quarters[name], equal_nan=True), name
        assert np.array_equal(whole[name], slivers[name], equal_nan=True), name


def test_scorer_without_capability_is_streaming(tmp_path: Path) -> None:
    assert not hasattr(Bare, "capability")
    pairs = _resolve_pair(tmp_path, two_overlapping(), NadirScorer(), Bare())
    for a, b in pairs:
        _assert_same_snapshot(a, b)


def test_layer_accessor(tmp_path: Path) -> None:
    # streaming: product ids, per observation
    class StreamLayer(NadirScorer):
        required_layers = ("mineral",)
        layers: ClassVar[list] = []

        def score(self, obs: ObsWindow, aux: Any) -> np.ndarray:
            StreamLayer.layers.append(np.array(obs.layer("mineral")))
            return super().score(obs, aux)

    class MapProbe:
        space = "map"
        required_roles: tuple[str, ...] = ()
        required_aux: tuple[str, ...] = ()
        errors: ClassVar[list] = []

        def valid(self, obs: ObsWindow, aux: Any) -> np.ndarray:
            try:
                obs.layer("mineral")
            except KeyError as e:
                MapProbe.errors.append(str(e))
            return np.ones(obs.valid.shape, bool)

    plan = build_plan(tmp_path / "s", two_overlapping(), scorer=StreamLayer(), masks=[MapProbe()])
    resolve_block(item(TILE, EPOCH, 0, 0), plan)
    assert len(StreamLayer.layers) == 2 and len(MapProbe.errors) == 2
    assert "required_layers" in MapProbe.errors[0]
    a_ids = StreamLayer.layers[0]
    assert a_ids.dtype == np.int32
    assert a_ids[4, 3] == 1 and a_ids[4, 4] == 2           # raw 7 -> 1, raw 5 -> 2 (not raw)
    # stack: the same values, one row per observation
    stack = LayerReader()
    plan = build_plan(tmp_path / "k", two_overlapping(), scorer=stack)
    resolve_block(item(TILE, EPOCH, 0, 0), plan)
    [stacked] = stack.layers
    assert stacked.shape == (2, 10, 10) and stacked.dtype == np.int32
    assert np.array_equal(stacked[0], a_ids) and np.array_equal(stacked[1], StreamLayer.layers[1])
    # required_layers naming no snapshot layer: refused by the context ...
    with pytest.raises(ValueError, match="no_such_layer"):
        build_plan(tmp_path / "u", two_overlapping(), scorer=UnknownLayer())
    # ... and by validate_static
    problems = _validate(tmp_path, "test_stack:UnknownLayer")
    assert "scorer test_stack:UnknownLayer: required layer 'no_such_layer' is not a snapshot " \
           "layer" in problems


# ---------------------------------------------------------------------- validate, keys, plan
def _validate(tmp_path: Path, ref: str) -> list[str]:
    from synthetic_nc import write_manifest

    from stratum.manifest import load_manifest, validate_static

    path = write_manifest(tmp_path / f"{ref.split(':')[1]}.yaml", scorer={"ref": ref})
    return validate_static(load_manifest(path))


def test_validate_capabilities(tmp_path: Path) -> None:
    assert _validate(tmp_path, "test_stack:StackNadir") == []
    assert _validate(tmp_path, "test_stack:TileNadir") == [
        "scorer test_stack:TileNadir: capability 'tile' is a later slice (04 section 4)"]
    assert _validate(tmp_path, "test_stack:BogusNadir") == [
        ("scorer test_stack:BogusNadir: capability 'bogus' must be one of 'streaming', "
         "'stack', 'tile'")]
    assert _validate(tmp_path, "test_stack:Bare") == []
    # and the context: tile is not built, anything else is not a capability
    with pytest.raises(NotImplementedError, match="04 section 4"):
        build_plan(tmp_path / "t", two_overlapping(), scorer=TileNadir())
    with pytest.raises(ValueError, match="must be one of"):
        build_plan(tmp_path / "b", two_overlapping(), scorer=BogusNadir())


def test_streaming_keys_unchanged(tmp_path: Path) -> None:
    plan = build_plan(tmp_path, two_overlapping(), scorer=NadirScorer())
    assert resolve_block(item(TILE, EPOCH, 0, 0), plan).hash == STREAMING_KEY


def test_estimate_stack_peak_bytes(tmp_path: Path) -> None:
    from synthetic_nc import write_manifest, write_scenes

    from stratum.plan import plan_run
    from stratum.plan.estimate import estimate, stack_peak_bytes

    assert stack_peak_bytes(12, 30 * 30, 25) == 12 * 900 * 25
    rom = estimate(tile_cells=100, block_cells=25, bytes_per_cell=10, regrid_overlaps=[1.0],
                   resolve_candidates=[3, 7, 2], reduce_items=1, publish_items=1,
                   stack_kept_bytes_per_cell=20, halo_block_cells=49)
    assert rom["stack_max_candidates"] == 7 and rom["stack_peak_bytes"] == 7 * 49 * 20
    assert "stack_peak_bytes" not in estimate(
        tile_cells=100, block_cells=25, bytes_per_cell=10, regrid_overlaps=[1.0],
        resolve_candidates=[3], reduce_items=1, publish_items=1)

    write_scenes(tmp_path / "granules")
    stack = plan_run(write_manifest(tmp_path / "stack.yaml", run_label="stack",
                                    scorer={"ref": "test_stack:StackNadir"}))
    streaming = plan_run(write_manifest(tmp_path / "streaming.yaml", run_label="streaming"))
    rom = stack.document["estimate"]
    # view_zenith (one float32 band copied) + the schema's layers + valid + interpolated, less
    # the schema's `view_zenith` layer (float32), which IS the copied band and is held once
    assert stack.document["estimate"] and rom["stack_peak_bytes"] == (
        rom["stack_max_candidates"] * 25 * 25 * (4 + rom["bytes_per_cell"] - 4 + 2))
    assert "resolve (stack): ~" in stack.report and "candidates; Lambda default" in stack.report
    assert "resolve (stack)" not in streaming.report
    assert "stack_peak_bytes" not in streaming.document["estimate"]


# ======================================================================= scorer-emitted layers
# 04 section 4 "Scorer-emitted layers": `outputs` + `emit()`, reached from the schema with
# `source: scorer.<output>`.
def _scorer_layer(name: str, output: str | None = None, dtype: str = "float32",
                  method: str = "mean") -> LayerSpec:
    return LayerSpec(name=name, kind="continuous", source=f"scorer.{output or name}",
                     aggregate=Aggregation(method), dtype=dtype)


def _with_layers(*extra: LayerSpec) -> SnapshotSchema:
    base = _with_acquired()
    return SnapshotSchema(name=base.name, layers=[*base.layers, *extra])


class EmitRow(StackNadir):
    """Stack: per observation, row index + 10."""

    outputs = (BandSpec("row", "float32", "row index + 10"),)

    def emit(self, obs: ObsWindow, aux: Any) -> dict[str, np.ndarray]:
        return {"row": np.broadcast_to(
            np.arange(obs.n, dtype=np.float32)[:, None, None] + 10, obs.valid.shape)}


class EmitCell(StackNadir):
    """Stack: a per-cell value, independent of which look wins."""

    outputs = (BandSpec("cell", "float32", "cell number"),)

    def emit(self, obs: ObsWindow, aux: Any) -> dict[str, np.ndarray]:
        h, w = obs.valid.shape[1:]
        return {"cell": np.arange(h * w, dtype=np.float32).reshape(h, w) + 0.5}


class StreamEmit(NadirScorer):
    """Streaming: twice the look's view zenith, per observation."""

    outputs = (BandSpec("vz2", "float32", "2 x view zenith"),)

    def emit(self, obs: ObsWindow, aux: Any) -> dict[str, np.ndarray]:
        return {"vz2": 2 * obs["view_zenith"]}


class StackEmitOf(StackOf):
    """The stack twin of a streaming emitter: loops over the observations for both calls."""

    def __init__(self, inner: Any) -> None:
        super().__init__(inner)
        self.outputs = inner.outputs

    def emit(self, obs: ObsWindow, aux: Any) -> dict[str, np.ndarray]:
        per = [self.inner.emit(o, aux) for o in obs]
        return {b.name: np.stack([np.asarray(p[b.name]) for p in per]) for b in self.outputs}


class EmitRaises(StackNadir):
    outputs = (BandSpec("x", "float32", "never computed"),)

    def emit(self, obs: ObsWindow, aux: Any) -> dict[str, np.ndarray]:
        raise AssertionError("emit() must not run without a scorer.* layer")


class StreamEmitRaises(NadirScorer):
    outputs = (BandSpec("x", "float32", "never computed"),)

    def emit(self, obs: ObsWindow, aux: Any) -> dict[str, np.ndarray]:
        raise AssertionError("emit() must not run without a scorer.* layer")


class Recorder:
    """Records the order of score/emit calls and the obs objects each received."""

    def __init__(self, base: type) -> None:
        self.calls: list[tuple[str, int]] = []
        self.base = base

    def wrap(self) -> Any:
        rec = self

        class Wrapped(self.base):
            def score(self, obs: ObsWindow, aux: Any) -> np.ndarray:
                rec.calls.append(("score", id(obs)))
                return super().score(obs, aux)

            def emit(self, obs: ObsWindow, aux: Any) -> dict[str, np.ndarray]:
                rec.calls.append(("emit", id(obs)))
                return super().emit(obs, aux)

        return Wrapped()


def _resolve_one(tmp_path: Path, scorer: Any, layers: tuple[LayerSpec, ...],
                 block: tuple[int, int] = (1, 1)) -> dict[str, np.ndarray]:
    plan = build_plan(tmp_path, two_overlapping(), scorer=scorer,
                      snapshot_schema=_with_layers(*layers))
    return read_snapshot(resolve_block(item(TILE, EPOCH, *block), plan).path)


def test_emit_per_observation_comes_from_the_winning_row(tmp_path: Path) -> None:
    snap = _resolve_one(tmp_path, EmitRow(), (_scorer_layer("row"),))
    valid = snap["valid"]
    starts = [acquired_seconds(o.ref.datetime) for o in two_overlapping()]   # rows 0, 1
    assert valid.sum() > 20
    winner = np.where(snap["acquired"] == starts[0], 0, 1)
    assert set(np.unique(winner[valid]).tolist()) == {0, 1}, "both looks win somewhere"
    assert np.array_equal(snap["row"][valid], (winner + 10)[valid].astype(np.float32))
    assert np.isnan(snap["row"][~valid]).all()
    assert snap["row"].dtype == np.float32


def test_emit_per_cell_is_taken_as_is(tmp_path: Path) -> None:
    snap = _resolve_one(tmp_path, EmitCell(), (_scorer_layer("cell"),), block=(0, 0))
    valid = snap["valid"]
    expect = np.arange(100, dtype=np.float32).reshape(10, 10) + 0.5
    assert valid.sum() > 20 and (~valid).any()
    assert np.array_equal(snap["cell"][valid], expect[valid])
    assert np.isnan(snap["cell"][~valid]).all()


def test_emit_streaming_equals_stack(tmp_path: Path) -> None:
    pairs = _resolve_pair(tmp_path, two_overlapping(), StreamEmit(), StackEmitOf(StreamEmit()),
                          snapshot_schema=_with_layers(_scorer_layer("vz2")))
    assert len(pairs) == 4
    for a, b in pairs:
        assert (a / "vz2.tif").is_file()
        _assert_same_snapshot(a, b)
    snap = read_snapshot(pairs[0][0])
    valid = snap["valid"]
    assert valid.any() and np.isfinite(snap["vz2"][valid]).all()
    assert (snap["vz2"][valid] == -2 * snap["score"][valid]).all()


def test_emit_not_called_without_scorer_layers(tmp_path: Path) -> None:
    for name, scorer in (("stack", EmitRaises()), ("streaming", StreamEmitRaises())):
        plan = build_plan(tmp_path / name, two_overlapping(), scorer=scorer)
        assert plan.scorer_layers() == []
        snap = read_snapshot(resolve_block(item(TILE, EPOCH, 0, 0), plan).path)
        assert snap["valid"].any()


def test_emit_not_called_on_an_empty_block(tmp_path: Path) -> None:
    obs = [make_observation("A", datetime(2026, 6, 5, tzinfo=UTC), r0=2, c0=2, rows=6, cols=6,
                            view_zenith=10.0)]

    class Never(Raises):
        outputs = (BandSpec("x", "float32", "never computed"),)

        def emit(self, obs: ObsWindow, aux: Any) -> dict[str, np.ndarray]:
            raise AssertionError("emit() must not run on an empty block")

    plan = build_plan(tmp_path, obs, scorer=Never(), snapshot_schema=_with_layers(
        _scorer_layer("x")))
    snap = read_snapshot(resolve_block(item(TILE, EPOCH, 1, 1), plan).path)
    assert not snap["valid"].any() and np.isnan(snap["x"]).all()


@pytest.mark.parametrize("mode", ["streaming", "stack"])
def test_emit_contract_errors(tmp_path: Path, mode: str) -> None:
    base = StreamEmit if mode == "streaming" else EmitRow
    name = base.outputs[0].name

    class Missing(base):
        def emit(self, obs: ObsWindow, aux: Any) -> dict[str, np.ndarray]:
            return {}

    class Extra(base):
        def emit(self, obs: ObsWindow, aux: Any) -> dict[str, np.ndarray]:
            return {**super().emit(obs, aux), "typo": np.zeros(obs.valid.shape[-2:])}

    class Shape(base):
        def emit(self, obs: ObsWindow, aux: Any) -> dict[str, np.ndarray]:
            return {name: np.zeros((3, 4))}

    layer = _scorer_layer(name)
    cases = [
        (Missing(), (f"emit() returned []; it must return exactly its declared outputs "
                     f"['{name}']")),
        (Extra(), (f"emit() returned {sorted(['typo', name])}; it must return exactly its "
                   f"declared outputs ['{name}']")),
        (Shape(), f"emit() output '{name}' has shape (3, 4); expected "
                  + ("(H, W) = (10, 10) (04 section 4)" if mode == "streaming" else
                     "(N, H, W) = (2, 10, 10) per observation or (H, W) = (10, 10) per cell")),
    ]
    for i, (scorer, message) in enumerate(cases):
        plan = build_plan(tmp_path / str(i), two_overlapping(), scorer=scorer,
                          snapshot_schema=_with_layers(layer))
        with pytest.raises(ValueError, match=re.escape(message)):
            resolve_block(item(TILE, EPOCH, 0, 0), plan)


@pytest.mark.parametrize("base", [StreamEmit, EmitRow], ids=["streaming", "stack"])
def test_emit_memoisation_contract(tmp_path: Path, base: type) -> None:
    rec = Recorder(base)
    layer = _scorer_layer(base.outputs[0].name)
    plan = build_plan(tmp_path, two_overlapping(), scorer=rec.wrap(),
                      snapshot_schema=_with_layers(layer))
    resolve_block(item(TILE, EPOCH, 0, 0), plan)
    kinds = [k for k, _ in rec.calls]
    assert kinds == ["score", "emit"] * (2 if base is StreamEmit else 1)
    for (_, s), (_, e) in zip(rec.calls[::2], rec.calls[1::2], strict=True):
        assert s == e, "emit() receives the very obs object score() did"


def test_snapshot_key_moves_with_scorer_layers(tmp_path: Path) -> None:
    from stratum.resolve import snapshot_key

    class TwoOut(EmitRow):
        outputs = (BandSpec("row", "float32", "row"), BandSpec("other", "float32", "other"))

        def emit(self, obs: ObsWindow, aux: Any) -> dict[str, np.ndarray]:
            row = super().emit(obs, aux)["row"]
            return {"row": row, "other": row + 1}

    cases = {
        "none": (),
        "added": (_scorer_layer("r", "row"),),
        "resourced": (_scorer_layer("r", "other"),),
        "retyped": (_scorer_layer("r", "row", dtype="float64"),),
    }
    keys = {}
    hashes = {}
    for name, layers in cases.items():
        schema_ = _with_layers(*layers)
        plan = build_plan(tmp_path / name, two_overlapping(), scorer=TwoOut(),
                          snapshot_schema=schema_)
        keys[name] = snapshot_key(item(TILE, EPOCH, 0, 0), plan)
        hashes[name] = schema_.layers_hash
        resolve_block(item(TILE, EPOCH, 0, 0), plan)
    assert len(set(hashes.values())) == len(cases)
    assert len({k.hash for k in keys.values()}) == len(cases)
    cache = build_plan(tmp_path / "none", two_overlapping(), scorer=TwoOut()).cache
    for name in ("added", "resourced", "retyped"):
        assert set(cache.diff(keys["none"], keys[name])) == {"layers_hash"}, name
    # a float64 output is stored as float64, NaN where nothing won
    snap = read_snapshot(keys["retyped"].path)
    assert snap["r"].dtype == np.float64


def test_planner_types_a_scorer_layer_from_its_bandspec(tmp_path: Path) -> None:
    """The layer's dtype - and so `layers_hash` and every snapshot key - comes from the scorer's
    declared BandSpec, filled in by the planner (06 section 2)."""
    from synthetic_nc import manifest_doc, write_manifest, write_scenes

    from stratum.plan import plan_run

    write_scenes(tmp_path / "granules")
    docs = {}
    for ref in ("test_stack:E2EEmit", "test_stack:E2EEmit64"):
        name = ref.split(":")[1]
        snapshot = manifest_doc()["snapshot"]
        snapshot["layers"].update(_e2e_scorer_layers())
        result = plan_run(write_manifest(tmp_path / f"{name}.yaml", scorer={"ref": ref},
                                         run_label=name.lower(), snapshot=snapshot))
        docs[ref] = result.document["context"]["schema"]
    a, b = docs["test_stack:E2EEmit"], docs["test_stack:E2EEmit64"]
    dtypes = {d["name"]: d["dtype"] for d in a["layers"]}
    assert dtypes["looks"] == "float32" and dtypes["vz"] == "float32"
    assert {d["name"]: d["dtype"] for d in b["layers"]}["vz"] == "float64"
    assert a["layers_hash"] != b["layers_hash"]


def test_no_emit_keys_unchanged(tmp_path: Path) -> None:
    """Re-asserted after the emit framework: a run without scorer layers keeps its keys."""
    plan = build_plan(tmp_path, two_overlapping(), scorer=NadirScorer())
    assert plan.scorer_layers() == []
    assert resolve_block(item(TILE, EPOCH, 0, 0), plan).hash == STREAMING_KEY
    # and a scorer that declares outputs nobody asked for changes nothing but its own identity
    assert schema().layers_hash == build_plan(tmp_path / "e", two_overlapping(),
                                              scorer=StreamEmit()).schema.layers_hash


def test_context_refuses_bad_scorer_layers(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="layer\\(s\\) \\['nope'\\] name another"):
        build_plan(tmp_path / "a", two_overlapping(), scorer=EmitRow(),
                   snapshot_schema=_with_layers(_scorer_layer("nope")))

    class NoEmit(StackNadir):
        outputs = (BandSpec("row", "float32", "row"),)

    with pytest.raises(ValueError, match="no emit"):
        build_plan(tmp_path / "b", two_overlapping(), scorer=NoEmit(),
                   snapshot_schema=_with_layers(_scorer_layer("row")))

    class ReadsOwn(EmitRow):
        required_layers = ("row",)

    with pytest.raises(ValueError, match="own outputs"):
        build_plan(tmp_path / "c", two_overlapping(), scorer=ReadsOwn(),
                   snapshot_schema=_with_layers(_scorer_layer("row")))


# ------------------------------------------------------------------ manifest-level scorer layers
class E2EEmit(StackNadir):
    """A stack scorer for the manifest tests: a per-cell count of valid looks and a
    per-observation view zenith."""

    outputs = (BandSpec("looks", "float32", "valid looks at the cell"),
               BandSpec("vz", "float32", "the winner's view zenith"))

    def emit(self, obs: ObsWindow, aux: Any) -> dict[str, np.ndarray]:
        return {"looks": obs.valid.sum(axis=0).astype(np.float32), "vz": obs["view_zenith"]}


class E2EEmit64(E2EEmit):
    outputs = (BandSpec("looks", "float32", "valid looks at the cell"),
               BandSpec("vz", "float64", "the winner's view zenith"))


class OutputsNoEmit(StackNadir):
    outputs = (BandSpec("looks", "float32", "x"),)


class BadOutputs(StackNadir):
    outputs = (BandSpec("a", "float32", "x"), BandSpec("a", "float32", "y"),
               BandSpec("b", "uint8", "z"))

    def emit(self, obs: ObsWindow, aux: Any) -> dict[str, np.ndarray]:
        return {}


class NotBandSpecs(StackNadir):
    outputs = ("looks",)

    def emit(self, obs: ObsWindow, aux: Any) -> dict[str, np.ndarray]:
        return {}


class ReadsOwnOutput(E2EEmit):
    required_layers = ("looks",)


def _e2e_scorer_layers() -> dict[str, dict]:
    return {"looks": {"kind": "continuous", "source": "scorer.looks",
                      "aggregate": {"method": "median"}},
            "vz": {"kind": "continuous", "source": "scorer.vz",
                   "aggregate": {"method": "median"}}}


def _validate_doc(tmp_path: Path, ref: str, layers: dict | None = None,
                  aliases: dict | None = None) -> list[str]:
    from synthetic_nc import manifest_doc, write_manifest

    from stratum.manifest import load_manifest, validate_static

    doc = manifest_doc()
    snapshot = doc["snapshot"]
    snapshot["layers"].update(layers or {})
    inputs = doc["inputs"]
    inputs["band_aliases"].update(aliases or {})
    path = write_manifest(tmp_path / f"{ref.split(':')[1]}-{len(list(tmp_path.iterdir()))}.yaml",
                          scorer={"ref": ref}, snapshot=snapshot, inputs=inputs)
    return validate_static(load_manifest(path))


def test_validate_scorer_layers(tmp_path: Path) -> None:
    ref = "test_stack:E2EEmit"
    assert _validate_doc(tmp_path, ref, _e2e_scorer_layers()) == []
    # dtype spelled out and equal to the BandSpec's is fine
    assert _validate_doc(tmp_path, ref, {"looks": {**_e2e_scorer_layers()["looks"],
                                                   "dtype": "float32"}}) == []
    # a role or alias in the reserved namespace
    problems = _validate_doc(tmp_path, ref, aliases={
        "scorer.x": {"role": "geometry", "band": 1}, "scorer": {"role": "geometry", "band": 1}})
    assert "inputs: ['scorer', 'scorer.x'] use the reserved prefix 'scorer.' (13 section 2)" \
        in problems
    # an output the scorer does not declare
    problems = _validate_doc(tmp_path, ref, {"typo": {"kind": "continuous",
                                                      "source": "scorer.lookz",
                                                      "aggregate": {"method": "median"}}})
    assert problems == [(f"snapshot.layers.typo: scorer {ref} declares no output 'lookz'; it "
                         "declares ['looks', 'vz']")]
    # outputs without emit()
    problems = _validate_doc(tmp_path, "test_stack:OutputsNoEmit")
    assert problems == [("scorer test_stack:OutputsNoEmit: declares outputs but no emit() "
                         "(04 section 4)")]
    # duplicate names, a non-float dtype, entries that are not BandSpecs
    problems = _validate_doc(tmp_path, "test_stack:BadOutputs")
    assert problems == [
        "scorer test_stack:BadOutputs: `outputs` names ['a'] more than once; one band, one name",
        ("scorer test_stack:BadOutputs: output 'b' has dtype 'uint8'; a scorer output is one of "
         "['float32', 'float64'] (04 section 4)")]
    assert _validate_doc(tmp_path, "test_stack:NotBandSpecs") == [
        "scorer test_stack:NotBandSpecs: every entry of `outputs` must be a BandSpec"]
    # the layer must be one continuous band of the output's dtype
    want = ("snapshot.layers.{}: a scorer output is one continuous float32 band; declare kind: "
            "continuous with no bands or classes, and dtype omitted or float32 (13 section 2)")
    for name, layer in {
        "cat": {"kind": "categorical", "source": "scorer.looks", "classes": "source",
                "aggregate": {"method": "vote"}},
        "banded": {"kind": "continuous", "source": "scorer.looks", "bands": [0],
                   "aggregate": {"method": "median"}},
        "typed": {"kind": "continuous", "source": "scorer.looks", "dtype": "float64",
                  "aggregate": {"method": "median"}},
    }.items():
        problems = _validate_doc(tmp_path, ref, {name: layer})
        assert want.format(name) in problems, (name, problems)
    # a scorer may not read its own output through required_layers
    problems = _validate_doc(tmp_path, "test_stack:ReadsOwnOutput", _e2e_scorer_layers())
    assert problems == [("scorer test_stack:ReadsOwnOutput: required layer 'looks' is the "
                         "scorer's own output")]


def test_scorer_layer_reduces_and_publishes(tmp_path: Path) -> None:
    """End to end: a `scorer.*` layer is a continuous layer like any other - reduced by its
    declared aggregate and published as `<layer>` and `<layer>_n`."""
    import rasterio
    from synthetic_nc import TILE as NC_TILE
    from synthetic_nc import manifest_doc, write_manifest, write_scenes

    from stratum.executors import run_all
    from stratum.plan import plan_run

    write_scenes(tmp_path / "granules")
    snapshot = manifest_doc()["snapshot"]
    snapshot["layers"].update(_e2e_scorer_layers())
    result = plan_run(write_manifest(tmp_path / "m.yaml", scorer={"ref": "test_stack:E2EEmit"},
                                     snapshot=snapshot))
    run_all(result.run_dir, workers=1)
    products = (Path(result.document["products_dir"]) / f"{NC_TILE.tx}_{NC_TILE.ty}"
                / "20260601_20260801")
    for band in ("looks", "looks_n", "vz", "vz_n"):
        assert (products / f"{band}.tif").is_file(), band
    with rasterio.open(products / "looks.tif") as src:
        looks = src.read(1)
    with rasterio.open(products / "vz.tif") as src:
        vz = src.read(1)
    with rasterio.open(products / "looks_n.tif") as src:
        n = src.read(1)
    have = np.isfinite(looks)
    assert have.any()
    assert (looks[have] >= 1).all() and (n[have] >= 1).all()
    assert np.isfinite(vz[have]).all() and (vz[have] >= 0).all()


# ------------------------------------------------------------ kept buffers: sharing, dtype
def _with_band_layers() -> SnapshotSchema:
    """The base schema plus two continuous layers whose value IS a band a scorer may keep: the
    whole two-band `geometry` role and the `view_zenith` alias."""
    base = _with_acquired()
    return SnapshotSchema(name=base.name, layers=[
        *base.layers,
        LayerSpec(name="geom", kind="continuous", source="geometry",
                  aggregate=Aggregation("mean"), dtype="float32"),
        LayerSpec(name="vz", kind="continuous", source="view_zenith",
                  aggregate=Aggregation("mean"), dtype="float32")])


@pytest.mark.parametrize("stack", [lambda: StackOf(NadirScorer()), lambda: StackNadir()],
                         ids=["kept-role", "copied-alias"])
def test_stack_layer_shares_the_kept_band(tmp_path: Path, stack: Any) -> None:
    """A continuous layer whose source the scorer also keeps is served from the kept band, not
    held twice - and the snapshot is still byte-identical to streaming's."""
    scorer = stack()
    pairs = _resolve_pair(tmp_path, two_overlapping(), NadirScorer(), scorer,
                          snapshot_schema=_with_band_layers())
    for a, b in pairs:
        _assert_same_snapshot(a, b)
    plan = build_plan(tmp_path / "probe", two_overlapping(), scorer=StackOf(NadirScorer()),
                      snapshot_schema=_with_band_layers())
    shared = plan.stack_shared_layers()
    # depth is not kept by the scorer, mineral is categorical, acquired is the framework's
    assert shared == {"geom": "geometry", "vz": "view_zenith"}
    probe = StackOf(NadirScorer())
    plan = build_plan(tmp_path / "probe2", two_overlapping(), scorer=probe,
                      snapshot_schema=_with_band_layers())
    resolve_block(item(TILE, EPOCH, 0, 0), plan)
    [obs] = probe.seen
    assert np.shares_memory(obs.layer("geom"), obs["geometry"])
    assert np.shares_memory(obs.layer("vz"), obs["geometry"])
    assert not np.shares_memory(obs.layer("depth"), obs["geometry"])


def test_stack_kept_bytes_count_a_shared_layer_once(tmp_path: Path) -> None:
    from stratum.plan.run import stack_kept_bytes_per_cell
    from stratum.types import VarSpec

    plan = build_plan(tmp_path, two_overlapping(), scorer=StackOf(NadirScorer()),
                      snapshot_schema=_with_band_layers())
    specs = {"geometry": VarSpec(name="geom", dtype="float32", shape=(14, 14, 2), fill=None)}
    counts = {"geom": 2}
    from stratum.plan.estimate import layer_bytes_per_cell

    layer_bytes = layer_bytes_per_cell(plan.schema.layers, counts)
    # geometry kept whole (2 x 4), the layers less geom (2 x 4) and vz (4), valid + interpolated
    assert stack_kept_bytes_per_cell(plan, specs, layer_bytes, counts) == \
        8 + (layer_bytes - 8 - 4) + 2


@pytest.mark.parametrize("extra", [(), ("depth",)], ids=["layer-buffer", "kept-band"])
def test_stack_buffer_promotes_a_wider_later_dtype(tmp_path: Path, extra: tuple) -> None:
    """Granule A (earlier, read first) carries `depth` as int16, B as float32: the kept buffer
    must widen for B rather than truncate it, so stack still equals streaming. Both buffers:
    the layer's own, and the kept band a shared layer is served from."""
    obs = two_overlapping()
    a = obs[0]
    a.data.variables["depth"] = np.round(a.data.variables["depth"] * 100).astype(np.int16)
    pairs = _resolve_pair(tmp_path, obs, NadirScorer(), StackOf(NadirScorer(), extra),
                          snapshot_schema=_with_acquired())
    fractional = False
    for sa, sb in pairs:
        _assert_same_snapshot(sa, sb)
        d = read_snapshot(sb)["depth"]
        fractional |= bool((np.isfinite(d) & (d != np.round(d))).any())
    assert fractional, "B's fractional depths reached the snapshot"
