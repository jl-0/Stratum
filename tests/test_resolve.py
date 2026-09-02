"""stratum.resolve: the gather, masks, the read path, the streaming loop, snapshot IO, and the
seam-equivalence invariant (12 section 2, 04 sections 3-4, 13 section 3, 01 section 4)."""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

import numpy as np
import pytest
from synthetic import (
    EPOCH,
    GEOM_BANDS,
    GRID,
    TILE,
    NadirScorer,
    NeighbourScorer,
    ShallowIsNan,
    binding,
    block_of,
    build_plan,
    item,
    make_observation,
    remap_for,
    resolve_tile,
    schema,
    two_overlapping,
    with_block,
)

from stratum.cache import CacheRoot
from stratum.classes import UNMAPPED, Remap
from stratum.regrid import read_glt
from stratum.resolve import (
    CATEGORICAL_NODATA,
    NullAux,
    ObsContext,
    PlanContext,
    ResolveItem,
    apply_remap,
    candidates,
    gather,
    glt_key_for,
    observation_inputs,
    plugin_version,
    read_observation,
    read_snapshot,
    resolve_block,
    stack_snapshots,
    write_snapshot,
)
from stratum.types import (
    BlockRef,
    Epoch,
    GridDef,
    ObsWindow,
    SensorWindow,
    TileRef,
    Window,
    canonical_hash,
)
from stratum_emit.masks import EdgeTrim, L2AStandard, SlitDust

pytestmark = pytest.mark.filterwarnings("ignore::rasterio.errors.NotGeoreferencedWarning")


# ------------------------------------------------------------------------------- the gather
def test_gather_follows_12_section_2_step_14_with_negatives_and_masks() -> None:
    # a 4 x 5 sensor window cut at (row0=10, col0=20) from a bigger array
    sw = SensorWindow(row0=10, col0=20, height=4, width=5)
    data = np.arange(20, dtype=np.int16).reshape(4, 5) + 100
    mask = np.zeros((4, 5), dtype=bool)
    mask[1, 1] = True                                   # a fill pixel
    arr = np.ma.MaskedArray(data, mask=mask)
    ok = np.ones((4, 5), dtype=bool)
    ok[2, 3] = False                                    # a sensor mask rejects one pixel
    glt = np.zeros((2, 3, 3), dtype=np.int32)
    glt[0, 0] = (21, 11, 1)          # sensor (0, 0) of the window -> 100
    glt[0, 1] = (-22, -12, 1)        # interpolated hit on sensor (1, 1) -> fill
    glt[0, 2] = (24, -13, 1)         # one negative band is enough -> interpolated; sensor (2, 3)
    glt[1, 0] = (25, 14, 1)          # sensor (3, 4) -> 119
    glt[1, 1] = (0, 0, 0)            # no hit
    glt[1, 2] = (23, 13, 1)          # sensor (2, 2) -> 112
    g = gather(arr, glt, sw, ok)
    assert g.band.dtype == np.int16 and g.band.shape == (2, 3)
    assert g.band[0, 0] == 100 and g.band[1, 0] == 119 and g.band[1, 2] == 112
    assert g.band[0, 1] == data[1, 1]                   # value present, but not valid
    assert g.valid.tolist() == [[True, False, False], [True, False, True]]
    assert g.interpolated.tolist() == [[False, True, True], [False, False, False]]
    # ok=None means no sensor mask
    assert gather(arr, glt, sw, None).valid[0, 2]


def test_gather_multiband_and_empty() -> None:
    sw = SensorWindow(0, 0, 2, 2)
    cube = np.ma.MaskedArray(np.arange(12, dtype=np.float32).reshape(2, 2, 3))
    cube[1, 1, 2] = np.ma.masked
    glt = np.zeros((1, 2, 3), dtype=np.int32)
    glt[0, 0] = (1, 1, 1)
    glt[0, 1] = (2, 2, 1)
    g = gather(cube, glt, sw, None)
    assert g.band.shape == (1, 2, 3) and g.band[0, 0].tolist() == [0.0, 1.0, 2.0]
    assert g.valid.tolist() == [[True, False]]          # any band fill -> fill
    g = gather(cube, np.zeros((3, 3, 3), dtype=np.int32), sw, None)
    assert not g.valid.any() and g.band.shape == (3, 3, 3)
    with pytest.raises(IndexError):
        gather(cube, glt, SensorWindow(1, 1, 1, 1), None)   # window does not cover the GLT


# ------------------------------------------------------------------------------------- masks
def _sensor_obs(sw: SensorWindow, sensor_shape: tuple[int, int] | None,
                bands: dict | None = None, band_attrs: dict | None = None) -> ObsWindow:
    return ObsWindow(space="sensor", bands=bands or {}, valid=np.ones((sw.height, sw.width), bool),
                     granules=(), epoch=EPOCH, sensor_window=sw, sensor_shape=sensor_shape,
                     band_attrs=band_attrs)


def test_edge_trim_uses_the_detector_width_not_the_window() -> None:
    sw = SensorWindow(row0=5, col0=1230, height=2, width=12)   # the last 12 of 1242 columns
    ok = EdgeTrim(columns=7).valid(_sensor_obs(sw, (1664, 1242)), NullAux())
    assert ok.shape == (2, 12)
    assert ok[0].tolist() == [True] * 5 + [False] * 7            # 1230..1234 ok, 1235.. trimmed
    # a narrower detector trims earlier: the width comes from the observation
    ok = EdgeTrim(columns=7).valid(_sensor_obs(sw, (1664, 1240)), NullAux())
    assert ok[0].tolist() == [True] * 3 + [False] * 9
    with pytest.raises(ValueError, match="sensor_shape"):
        EdgeTrim().valid(_sensor_obs(sw, None), NullAux())
    with pytest.raises(ValueError):
        EdgeTrim(columns=4)


def test_l2a_standard_reads_mask_bands_by_name() -> None:
    names = ["Cloud flag", "Cirrus flag", "Water flag", "Spacecraft Flag", "Dilated Cloud Flag",
             "AOD550", "H2O (g cm-2)", "Aggregate Flag"]
    mask = np.zeros((2, 3, len(names)), dtype=np.float32)
    mask[0, 0, 0] = 1        # cloud
    mask[0, 1, 2] = 1        # water
    mask[1, 2, 4] = 1        # dilated cloud: not in the default flags
    mask[1, 1, 5] = 0.3      # AOD is not a flag
    obs = ObsWindow(space="block", bands={"mask": mask}, valid=np.ones((2, 3), bool),
                    granules=(), epoch=EPOCH, band_attrs={"mask": {"name": names}})
    ok = L2AStandard().valid(obs, NullAux())
    assert ok.tolist() == [[False, False, True], [True, True, True]]
    ok = L2AStandard(flags=("dilated_cloud",)).valid(obs, NullAux())
    assert ok.tolist() == [[True, True, True], [True, True, False]]
    with pytest.raises(KeyError, match="Cirrus flag"):
        L2AStandard().valid(ObsWindow(space="block", bands={"mask": mask},
                                      valid=np.ones((2, 3), bool), granules=(), epoch=EPOCH,
                                      band_attrs={"mask": {"name": names[:1]}}), NullAux())
    with pytest.raises(ValueError):
        L2AStandard(flags=("snow",))


def test_slit_dust_is_not_in_the_slice() -> None:
    with pytest.raises(NotImplementedError, match="04 section 3"):
        SlitDust().valid(_sensor_obs(SensorWindow(0, 0, 1, 1), (1, 1)), NullAux())


def test_null_aux_refuses_everything() -> None:
    aux = NullAux()
    with pytest.raises(NotImplementedError, match="05"):
        aux.raster("slope")
    with pytest.raises(NotImplementedError, match="05"):
        _ = aux.granule_index


# ----------------------------------------------------------------------------- the read path
@pytest.fixture
def pair_plan(tmp_path: Path) -> PlanContext:
    return build_plan(tmp_path, two_overlapping(), scorer=NadirScorer())


def test_read_observation_fill_zero_and_remap(pair_plan: PlanContext) -> None:
    plan = pair_plan
    a = plan.granules["A"]
    block = block_of(plan, 0, 0)                      # cells [0, 10) x [0, 10); A starts at (2, 2)
    result = read_observation(ObsContext(plan, block, EPOCH, a, glt_key_for(plan, TILE, "A")))
    assert result is not None
    obs = result.obs
    assert obs.space == "block" and obs.granule is a and obs.block is block
    assert set(obs._bands) == {"mineral", "depth", "geometry", "view_zenith"}
    assert obs["geometry"].shape == (10, 10, 2) and obs["view_zenith"].shape == (10, 10)
    assert obs.band_attrs["geometry"]["name"] == GEOM_BANDS
    # sensor row 0 is fill (-9999): observed nothing -> invalid; nothing sees the sentinel
    assert not obs.valid[2, 2:].any()
    assert not obs.valid[:2].any() and not obs.valid[:, :2].any()   # outside the granule: no hit
    # sensor row 1 is class 0: a real observation
    assert obs.valid[3, 2] and result.layers["mineral"][3, 2] == 0
    # raw 7 -> product 1, raw 5 (column 2 of the sensor -> tile column 4) -> product 2
    assert result.layers["mineral"][4, 3] == 1 and result.layers["mineral"][4, 4] == 2
    assert obs["mineral"][4, 3] == 7                     # the ObsWindow carries the values as read
    # raw 9 at sensor (3, 3) -> tile (5, 5): described by no class -> unmapped -> invalid
    assert result.layers["mineral"][5, 5] == UNMAPPED and not obs.valid[5, 5]
    assert obs.valid[5, 4] and obs.valid[5, 6]
    # continuous layer carries the sensor value; depth at sensor (2, 1) = 0.01 * (2*14 + 1)
    assert result.layers["depth"][4, 3] == pytest.approx(0.29)
    assert obs["view_zenith"][4, 3] == pytest.approx(11.0)
    assert not obs.interpolated.any()
    # coords: cell centres, lon == x on EPSG:4326
    assert obs.coords.lon[0, 0] == pytest.approx(0.0005) and obs.coords.lat[0, 0] == pytest.approx(0.0195)
    assert np.array_equal(obs.coords.x, obs.coords.lon)


def test_read_observation_is_none_without_a_hit(pair_plan: PlanContext) -> None:
    plan = pair_plan
    block = block_of(plan, 0, 0)                      # B starts at (6, 6): it reaches block (0, 0)
    assert read_observation(ObsContext(plan, block, EPOCH, plan.granules["B"],
                                       glt_key_for(plan, TILE, "B"))) is not None
    # a block B does not reach: shrink the window to the corner
    tiny = block_of(with_block(plan, 5), 0, 0)
    assert read_observation(ObsContext(plan, tiny, EPOCH, plan.granules["B"],
                                       glt_key_for(plan, TILE, "B"))) is None


def test_reads_are_windowed_and_contexts_closed(pair_plan: PlanContext) -> None:
    plan = pair_plan
    reader = plan.reader("FAKE")
    block = block_of(plan, 0, 0)
    read_observation(ObsContext(plan, block, EPOCH, plan.granules["A"],
                                glt_key_for(plan, TILE, "A")))
    windows = {w for _, _, w in reader.reads}
    assert windows == {SensorWindow(row0=0, col0=0, height=8, width=8)}   # cells 2..9 of A
    assert len(reader.reads) == 3                                          # one read per role


def test_sensor_mask_runs_on_the_sensor_window(tmp_path: Path) -> None:
    obs = make_observation("A", datetime(2026, 6, 5, tzinfo=UTC), r0=2, c0=2, rows=14, cols=14,
                           view_zenith=10.0)
    plan = build_plan(tmp_path, [obs], scorer=NadirScorer(), masks=[EdgeTrim(columns=5)])
    block = block_of(with_block(plan, 20), 0, 0)
    result = read_observation(ObsContext(plan, block, EPOCH, plan.granules["A"],
                                         glt_key_for(plan, TILE, "A")))
    assert result is not None
    v = result.obs.valid
    # sensor columns 0-4 and 9-13 trimmed -> tile columns 2-6 and 11-15 invalid; 7-10 kept
    assert not v[8, 2:7].any() and not v[8, 11:16].any() and v[8, 7:11].all()


def test_remap_out_of_range_is_unmapped() -> None:
    remap = Remap(lookup=np.array([0, -1, 4], dtype=np.int32), raw_fingerprint="x",
                  enumeration="e")
    assert apply_remap(np.array([[0, 1], [2, 7]]), remap).tolist() == [[0, -1], [4, -1]]
    assert remap_for().lookup.tolist() == [0, -1, -1, -1, -1, 2, -1, 1]


def test_missing_remap_names_the_granule_and_layer(pair_plan: PlanContext) -> None:
    plan = pair_plan
    plan.remaps["mineral"].pop("A")
    with pytest.raises(KeyError, match="'A'.*'mineral'"):
        read_observation(ObsContext(plan, block_of(plan, 0, 0), EPOCH, plan.granules["A"],
                                    glt_key_for(plan, TILE, "A")))


# ---------------------------------------------------------------------------- resolve_block
def test_candidates_by_bbox_and_epoch(pair_plan: PlanContext) -> None:
    plan = pair_plan
    assert [g.granule_id for g in candidates(plan, block_of(plan, 0, 0), EPOCH)] == ["A", "B"]
    assert [g.granule_id for g in candidates(plan, block_of(plan, 1, 1), EPOCH)] == ["A", "B"]
    later = Epoch(datetime(2026, 6, 8, tzinfo=UTC), datetime(2026, 7, 1, tzinfo=UTC))
    assert [g.granule_id for g in candidates(plan, block_of(plan, 0, 0), later)] == ["B"]
    parsed = ResolveItem.parse(item(TILE, EPOCH, 1, 0), plan)
    assert parsed.block == BlockRef(TILE, 1, 0, halo=0) and parsed.epoch == EPOCH


def test_resolve_block_writes_the_snapshot(pair_plan: PlanContext) -> None:
    plan = pair_plan
    key = resolve_block(item(TILE, EPOCH, 0, 0), plan)
    assert key.artifact == "snapshot" and plan.cache.hit(key)
    snap = read_snapshot(key.path)
    assert set(snap) == {"mineral", "depth", "score", "valid"}
    assert snap["mineral"].dtype == np.uint16 and snap["depth"].dtype == np.float32
    assert snap["score"].dtype == np.float32 and snap["valid"].dtype == bool
    # never won: nodata, NaN, invalid
    assert snap["mineral"][0, 0] == CATEGORICAL_NODATA and np.isnan(snap["depth"][0, 0])
    assert np.isnan(snap["score"][0, 0]) and not snap["valid"][0, 0]
    # A alone at (4, 3): raw 7 -> 1, score = -view_zenith = -11
    assert snap["valid"][4, 3] and snap["mineral"][4, 3] == 1 and snap["score"][4, 3] == -11.0
    # A's row 1 (class 0) is a real observation: valid and product id 0
    assert snap["valid"][3, 5] and snap["mineral"][3, 5] == 0
    # A's fill row never wins
    assert not snap["valid"][2, 5]
    # overlap at (8, 8): A vz = 10 + 6 = 16, B vz = 10 + 13 - 2 = 21 -> A wins
    assert snap["score"][8, 8] == -16.0 and snap["depth"][8, 8] == pytest.approx(0.01 * (6 * 14 + 6))
    # overlap at (9, 9) in block (1, 1) -> (19, 19) tile is out of A; check the .inputs.json
    inputs = plan.cache.explain(key)
    assert inputs["artifact_type"] == "snapshot" and len(inputs["obs_keys"]) == 2
    assert inputs["window"] == [0, 0, 10, 10] and inputs["epoch_bounds"] == list(EPOCH.bounds)
    assert inputs["scorer_params"] == {"weight": 1.0} and inputs["aux_keys"] == []
    assert inputs["layers_hash"] == plan.schema.layers_hash
    # the snapshot is georeferenced with the block's core transform
    import rasterio
    with rasterio.open(key.path / "score.tif") as src:
        assert src.transform == BlockRef(TILE, 0, 0).transform
        assert src.crs.to_epsg() == 4326 and src.nodata is not None and np.isnan(src.nodata)


def test_ties_go_to_the_earliest_observation(tmp_path: Path) -> None:
    when = datetime(2026, 6, 5, tzinfo=UTC)
    a = make_observation("A", when, r0=2, c0=2, rows=8, cols=8, view_zenith=10.0)
    b = make_observation("B", datetime(2026, 6, 4, tzinfo=UTC), r0=2, c0=2, rows=8, cols=8,
                         view_zenith=10.0)
    plan = build_plan(tmp_path, [a, b], scorer=NadirScorer())
    snap = read_snapshot(resolve_block(item(TILE, EPOCH, 0, 0), plan).path)
    # both score -10 everywhere and carry the same values; (5, 5) is raw 9 in both -> unmapped
    assert snap["score"][6, 6] == -10.0 and not snap["valid"][5, 5]
    # to tell them apart give B no unmapped pixel and a different class
    b2 = make_observation("B", datetime(2026, 6, 4, tzinfo=UTC), r0=2, c0=2, rows=8, cols=8,
                          view_zenith=10.0, class_ids=np.full((8, 8), 5))
    plan = build_plan(tmp_path / "second", [a, b2], scorer=NadirScorer())
    snap = read_snapshot(resolve_block(item(TILE, EPOCH, 0, 0), plan).path)
    assert snap["mineral"][6, 6] == 2                    # B (earlier, raw 5 -> 2) keeps the tie
    assert snap["mineral"][5, 5] == 2                    # and B fills A's unmapped cell


def test_nan_score_excludes_the_observation(pair_plan: PlanContext) -> None:
    plan = pair_plan
    plan_nan = build_plan(plan.cache.root / "nan", two_overlapping(), scorer=ShallowIsNan(0.5))
    snap = read_snapshot(resolve_block(item(TILE, EPOCH, 0, 0), plan_nan).path)
    # A's depth at sensor (2, 1) is 0.29 < 0.5 -> A may not occupy (4, 3); nobody else reaches it
    assert not snap["valid"][4, 3] and snap["mineral"][4, 3] == CATEGORICAL_NODATA
    # at (8, 8) A's depth is 0.90 -> A still wins
    assert snap["valid"][8, 8] and snap["score"][8, 8] == -16.0


def test_granule_without_a_hit_is_skipped_and_a_hit_returns_early(tmp_path: Path) -> None:
    a = make_observation("A", datetime(2026, 6, 5, tzinfo=UTC), r0=2, c0=2, rows=6, cols=6,
                         view_zenith=10.0)
    b = make_observation("B", datetime(2026, 6, 6, tzinfo=UTC), r0=12, c0=12, rows=6, cols=6,
                         view_zenith=10.0)
    plan = build_plan(tmp_path, [a, b], scorer=NadirScorer())
    key = resolve_block(item(TILE, EPOCH, 0, 0), plan)
    assert len(plan.cache.explain(key)["obs_keys"]) == 1          # B never reaches block (0, 0)
    store = plan.store
    opened = list(store.opened)
    assert opened == [a.uri]                                       # one open per asset, A only
    assert resolve_block(item(TILE, EPOCH, 0, 0), plan) == key
    assert store.opened == opened                                  # a hit reads nothing
    # a block nothing reaches still writes an (all-invalid) snapshot with an empty obs_keys
    empty = resolve_block(item(TILE, EPOCH, 1, 0), plan)
    assert plan.cache.explain(empty)["obs_keys"] == []
    assert not read_snapshot(empty.path)["valid"].any()
    assert empty != resolve_block(item(TILE, EPOCH, 0, 1), plan)   # the window is in the key
    # and a differently sized block over the same corner is a different snapshot, not a hit
    assert plan.cache.explain(resolve_block(item(TILE, EPOCH, 0, 0), with_block(plan, 20)))[
        "window"] == [0, 0, 20, 20]


def test_missing_glt_is_a_clear_error(pair_plan: PlanContext) -> None:
    plan = pair_plan
    glt_key_for(plan, TILE, "A").path.unlink()
    with pytest.raises(FileNotFoundError, match="regrid did not run for granule 'A'"):
        resolve_block(item(TILE, EPOCH, 0, 0), plan)


def test_scorer_or_mask_change_invalidates_snapshots_not_glts(pair_plan: PlanContext) -> None:
    plan = pair_plan
    k1 = resolve_block(item(TILE, EPOCH, 0, 0), plan)
    glts = sorted(p.stat().st_mtime_ns for p in (plan.cache.root / "cache" / "glt").rglob("*.tif"))
    k2 = resolve_block(item(TILE, EPOCH, 0, 0), build_plan(plan.cache.root, two_overlapping(),
                                                          scorer=NadirScorer(weight=2.0)))
    k3 = resolve_block(item(TILE, EPOCH, 0, 0), build_plan(plan.cache.root, two_overlapping(),
                                                          scorer=NadirScorer(),
                                                          masks=[EdgeTrim(columns=5)]))
    assert len({k1.path, k2.path, k3.path}) == 3
    assert plan.cache.diff(k1, k2) == {"scorer_params.weight": (1.0, 2.0)}
    assert set(plan.cache.diff(k1, k3)) == {"obs_keys"}
    after = sorted(p.stat().st_mtime_ns for p in (plan.cache.root / "cache" / "glt").rglob("*.tif"))
    assert after == glts and len(after) == 2


def test_non_streaming_scorer_is_refused(tmp_path: Path) -> None:
    class Stack(NadirScorer):
        capability = "stack"

    with pytest.raises(NotImplementedError, match="04 section 4"):
        build_plan(tmp_path, two_overlapping(), scorer=Stack())


def test_plugin_version_comes_from_the_distribution() -> None:
    from stratum_emit.scorers import MinViewZenith

    assert plugin_version(MinViewZenith()) == version("stratum")
    assert plugin_version(NadirScorer()) == "unversioned"          # tests are not a distribution
    assert binding(NadirScorer()).ref.endswith(":NadirScorer")


# ------------------------------------------------------------------------------ snapshot IO
def test_snapshot_round_trip_and_stack(tmp_path: Path) -> None:
    sch = schema()
    rng = np.random.default_rng(0)
    mineral = rng.integers(0, 3, (6, 8)).astype(np.uint16)
    mineral[0, 0] = CATEGORICAL_NODATA
    depth = rng.random((6, 8)).astype(np.float32)
    depth[0, 0] = np.nan
    score = -rng.random((6, 8)).astype(np.float32)
    valid = rng.random((6, 8)) > 0.3
    transform = BlockRef(TILE, 0, 0).transform
    d1 = write_snapshot(tmp_path / "e1", layers={"mineral": mineral, "depth": depth},
                        score=score, valid=valid, schema=sch, transform=transform, crs=GRID.crs)
    back = read_snapshot(d1)
    assert np.array_equal(back["mineral"], mineral)
    assert np.array_equal(back["depth"], depth, equal_nan=True)
    assert np.array_equal(back["score"], score) and np.array_equal(back["valid"], valid)
    win = read_snapshot(d1, Window(1, 2, 3, 4))
    assert np.array_equal(win["mineral"], mineral[1:4, 2:6])
    with pytest.raises(ValueError, match="extra"):
        write_snapshot(tmp_path / "bad", layers={"mineral": mineral, "depth": depth, "x": depth},
                       score=score, valid=valid, schema=sch, transform=transform, crs=GRID.crs)

    e1 = Epoch(datetime(2026, 6, 1, tzinfo=UTC), datetime(2026, 7, 1, tzinfo=UTC))
    e2 = Epoch(datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 8, 1, tzinfo=UTC))
    e3 = Epoch(datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 9, 1, tzinfo=UTC))
    d2 = write_snapshot(tmp_path / "e2", layers={"mineral": mineral + 1, "depth": depth + 1},
                        score=score + 1, valid=~valid, schema=sch, transform=transform,
                        crs=GRID.crs)
    # given out of order, with a missing epoch: comes back ascending, missing all-invalid
    stack = stack_snapshots([d2, None, d1], [e2, e3, e1], sch, Window(0, 0, 6, 8))
    assert stack.epochs == (e1, e2, e3)
    assert stack["mineral"].shape == (3, 6, 8) and stack.valid.shape == (3, 6, 8)
    assert np.array_equal(stack["mineral"][0], mineral)
    assert np.array_equal(stack["mineral"][1], mineral + 1)
    assert (stack["mineral"][2] == CATEGORICAL_NODATA).all() and not stack.valid[2].any()
    assert np.isnan(stack.score[2]).all() and np.array_equal(stack.valid[1], ~valid)
    assert stack.score.dtype == np.float32


def test_stack_feeds_the_reducer(tmp_path: Path) -> None:
    """The reduce module consumes what stack_snapshots builds (11 section 8)."""
    from stratum.reduce import reduce_stack

    sch = schema()
    transform = BlockRef(TILE, 0, 0).transform
    dirs, epochs = [], []
    for i in range(3):
        mineral = np.full((4, 4), 1 if i < 2 else 2, dtype=np.uint16)
        d = write_snapshot(tmp_path / f"e{i}", layers={"mineral": mineral,
                                                      "depth": np.full((4, 4), float(i), np.float32)},
                           score=np.full((4, 4), -1.0, np.float32), valid=np.ones((4, 4), bool),
                           schema=sch, transform=transform, crs=GRID.crs)
        dirs.append(d)
        epochs.append(Epoch(datetime(2026, 6 + i, 1, tzinfo=UTC),
                            datetime(2026, 7 + i, 1, tzinfo=UTC)))
    out = reduce_stack(stack_snapshots(dirs, epochs, sch, None))
    assert (out["mineral"] == 1).all() and (out["n_epochs"] == 3).all()
    assert np.allclose(out["depth"], 1.0)


# -------------------------------------------------------------------------- seam equivalence
@pytest.mark.parametrize("scorer", [NadirScorer(), NeighbourScorer()], ids=["halo0", "halo1"])
def test_seam_equivalence_blocks_equal_whole_tile(tmp_path: Path, scorer: NadirScorer) -> None:
    """01 section 3 invariant: block size never changes output, bit for bit. The same GLTs and
    granules resolved as one 20 x 20 block and as four 10 x 10 blocks (plus a 7-cell cut that
    leaves slivers) must assemble to identical core cells."""
    plan_blocks = build_plan(tmp_path, two_overlapping(), scorer=scorer, masks=[EdgeTrim(5)])
    whole = resolve_tile(with_block(plan_blocks, 20))
    quarters = resolve_tile(plan_blocks)
    slivers = resolve_tile(with_block(plan_blocks, 7))
    assert set(whole) == {"mineral", "depth", "score", "valid"}
    assert whole["valid"].shape == (20, 20) and whole["valid"].sum() > 50
    # something real got resolved: class 0 and product 1 (raw 5 sits in a trimmed column)
    assert set(np.unique(whole["mineral"][whole["valid"]]).tolist()) == {0, 1}
    for name in whole:
        assert np.array_equal(whole[name], quarters[name], equal_nan=True), name
        assert np.array_equal(whole[name], slivers[name], equal_nan=True), name
    # and every block read its GLT window from the same tile-wide file
    key = glt_key_for(plan_blocks, TILE, "A")
    assert np.array_equal(read_glt(key.path, Window(10, 10, 10, 10)), read_glt(key.path)[10:, 10:])


def test_halo_window_reads_past_the_tile_edge(pair_plan: PlanContext) -> None:
    plan = pair_plan
    parsed = ResolveItem.parse(item(TILE, EPOCH, 0, 0),
                               build_plan(plan.cache.root, two_overlapping(),
                                          scorer=NeighbourScorer()))
    assert parsed.block.halo == 1 and parsed.block.window == Window(-1, -1, 12, 12)
    glt = read_glt(glt_key_for(plan, TILE, "A").path, parsed.block.window)
    assert glt.shape == (12, 12, 3) and not glt[0].any() and not glt[:, 0].any()


def test_non_lonlat_grid_gets_lon_lat_coords(tmp_path: Path) -> None:
    from stratum.resolve import block_coords

    grid = GridDef("EPSG:32611", (30.0, -30.0), (500000.0, 4600000.0), 3000.0, block_size=100)
    block = BlockRef(TileRef(grid, 167, 1534), 0, 0)      # tile names are CRS positions
    t = block.transform
    c = block_coords(t, (2, 2), grid.crs)
    assert c.x[0, 0] == pytest.approx(t.c + 15.0) and c.y[0, 0] == pytest.approx(t.f - 15.0)
    assert 500990 <= t.c <= 501010 and 4604990 <= t.f <= 4605010
    assert -117.1 < c.lon[0, 0] < -116.9 and 41.5 < c.lat[0, 0] < 41.7


# --------------------------------------------------------------------- the reference granule
def test_reference_granule_read_path(ref_granule: Path, tmp_path: Path) -> None:
    """The real read path over the observed L2B granule (11): the `EMITL2BMIN` reader through
    `AssetStore`, a KD-tree GLT on a coarse grid, `classes: source` through the embedded table,
    `EdgeTrim` on the real 1242-column sensor, and a snapshot written and read back."""
    from stratum.access import AssetStore, read_header, reader_for, to_uri
    from stratum.classes import identity_enumeration
    from stratum.regrid import regrid_granule_tile, resolve_max_distance
    from stratum.resolve import PluginBinding, RoleBinding
    from stratum.types import Aggregation, GranuleRef, LayerSpec, SnapshotSchema

    class DeepestWins:
        capability = "streaming"
        halo = 0
        required_roles = ("depth",)
        required_aux: tuple[str, ...] = ()

        def score(self, obs: ObsWindow, aux: object) -> np.ndarray:
            return obs["depth"].astype(np.float32)

    hdr = read_header(ref_granule)
    w, e = float(hdr["westernmost_longitude"]), float(hdr["easternmost_longitude"])
    s, n = float(hdr["southernmost_latitude"]), float(hdr["northernmost_latitude"])
    grid = GridDef("EPSG:4326", (0.005, -0.005), (-180.0, -90.0), 1.0, block_size=100)   # 200 x 200
    tile = TileRef(grid, int(np.floor((w + e) / 2)), int(np.floor((s + n) / 2)))
    uri = to_uri(ref_granule)
    gid = "ref"
    when = datetime.fromisoformat(str(hdr["time_coverage_start"]).replace("+0000", "+00:00"))
    ref = GranuleRef(granule_id=gid, collection="EMITL2BMIN", datetime=when, end_datetime=when,
                     bbox=(w, s, e, n), assets={"EMITL2BMIN/MIN": uri}, build_version="010635",
                     product_version="V001", collection_version="001", cloud_fraction=None)
    store = AssetStore()
    reader = reader_for("EMITL2BMIN")
    ctx = reader.open(store.open(uri))
    raw = reader.class_table(ctx, "mineral_metadata", "index", ["name", "library", "record", "group"])
    assert raw is not None
    loc = reader.geolocation(ctx)
    assert loc is not None and loc.lat.shape == (1664, 1242)
    remap = identity_enumeration(raw).resolve(raw)
    ctx.close()

    cache = CacheRoot(tmp_path)
    md = resolve_max_distance(grid, None)
    regrid_granule_tile(cache, tile, gid, lambda: loc, max_distance=md)
    sch = SnapshotSchema(name="ref", layers=[
        LayerSpec(name="mineral", kind="categorical", source="mineral",
                  aggregate=Aggregation("vote"), dtype="uint16", classes=raw),
        LayerSpec(name="depth", kind="continuous", source="depth",
                  aggregate=Aggregation("mean"), dtype="float32")])
    scorer = DeepestWins()
    plan = PlanContext(
        grid=grid, cache=cache, store=store, granules={gid: ref},
        roles={"mineral": RoleBinding("EMITL2BMIN", "group_1_mineral_id", "MIN"),
               "depth": RoleBinding("EMITL2BMIN", "group_1_band_depth", "MIN")},
        aliases={}, geolocation_role="mineral", schema=sch,
        scorer=PluginBinding("test:DeepestWins", "0", {}, scorer),
        masks=[PluginBinding("edge_trim", plugin_version(EdgeTrim()), {"columns": 7}, EdgeTrim())],
        remaps={"mineral": {gid: remap}}, max_distance=md)
    epoch = Epoch(when.replace(day=1, hour=0, minute=0, second=0),
                  when.replace(month=when.month + 1, day=1, hour=0, minute=0, second=0))
    key_glt = glt_key_for(plan, tile, gid)
    reached = [b for b in tile.blocks() if (read_glt(key_glt.path, b.window)[..., 2] != 0).any()]
    assert reached, "the granule reaches no block of its own tile"
    block = reached[len(reached) // 2]
    key = resolve_block(item(tile, epoch, block.bx, block.by), plan)
    snap = read_snapshot(key.path)
    glt = read_glt(key_glt.path, block.core_window)
    hit = glt[..., 2] != 0
    assert snap["valid"].shape == (100, 100) and snap["valid"].sum() > 100
    assert not snap["valid"][~hit].any()
    # EdgeTrim: cells fed by the outer 7 detector columns are never valid; interior ones may be
    col = np.abs(glt[..., 0]) - 1
    edge = hit & ((col < 7) | (col >= 1242 - 7))
    assert not snap["valid"][edge].any()
    # product ids are the granule's own raw keys (classes: source), 0 = nothing identified
    keys = set(raw.entries.column("index").to_pylist()) | {0}
    assert set(np.unique(snap["mineral"][snap["valid"]]).tolist()) <= keys
    assert (snap["mineral"][~snap["valid"]] == CATEGORICAL_NODATA).all()
    # the score is the band depth of the winner: finite, in the observed 0-0.5 range (11 s2)
    won = snap["score"][snap["valid"]]
    assert np.isfinite(won).all() and 0.0 <= won.min() and won.max() <= 0.5
    assert np.array_equal(snap["depth"][snap["valid"]], won)
    assert plan.cache.explain(key)["obs_keys"] and plan.cache.hit(key)


def test_observation_key_carries_the_remap_and_the_asset_identity(pair_plan: PlanContext) -> None:
    """06 section 2 / 12 section 4 / 13 section 3 rule 3: the masked observation is determined
    by the granule's remap (applied at the gather) and by the bytes it was read from, so both
    are in its key; a re-delivered asset or a new lumping moves the snapshot key, not the GLT."""
    plan = pair_plan
    a = plan.granules["A"]
    key = glt_key_for(plan, TILE, "A")
    inputs = observation_inputs(plan, key, a)
    remap = plan.remaps["mineral"]["A"]
    assert inputs["remaps"] == {"mineral": {
        "raw_fingerprint": remap.raw_fingerprint, "enumeration": remap.enumeration,
        "lookup": canonical_hash(remap.lookup.tolist())}}
    assert inputs["asset_roles"]["mineral"]["checksum"] is None       # no catalogue: honest
    asset = next(iter(a.assets))
    redelivered = replace(a, checksums={asset: "sha512:new-bytes"})
    changed = observation_inputs(plan, key, redelivered)
    assert changed["asset_roles"]["mineral"]["checksum"] == "sha512:new-bytes"
    assert canonical_hash(changed) != canonical_hash(inputs)
    relumped = Remap(lookup=remap.lookup[::-1].copy(), raw_fingerprint=remap.raw_fingerprint,
                     enumeration=remap.enumeration)
    other = replace(plan, remaps={"mineral": {"A": relumped, "B": relumped}})
    assert canonical_hash(observation_inputs(other, key, a)) != canonical_hash(inputs)
    # the snapshot sidecar lists the raw fingerprint (13 section 6) and moves with the lumping
    k1 = resolve_block(item(TILE, EPOCH, 0, 0), plan)
    assert plan.cache.explain(k1)["class_tables"] == {"mineral": [remap.raw_fingerprint]}
    k2 = resolve_block(item(TILE, EPOCH, 0, 0), other)
    assert k1.path != k2.path and set(plan.cache.diff(k1, k2)) == {"obs_keys"}
