"""Spec 05 executable: the warp, its cache, and the accessor a plugin sees.

Everything here is offline. Aux sources are written into `tmp_path` in a DIFFERENT CRS and at a
different resolution from the run's grid, because a warp that is only ever exercised on matching
grids proves nothing - the whole contract (05 section 1) is that a plugin never learns the source
had a projection at all.
"""
from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest
import rasterio
from affine import Affine

from stratum.ancillary import (
    WARP_ALGO_VERSION,
    AuxError,
    AuxSource,
    BlockAux,
    aux_key_for,
    check_recorded_hash,
    file_digest,
    module_content_hash,
    read_window,
    sources_digest,
    warp_aux_tile,
    warp_to_tile,
)
from stratum.cache import CacheRoot, aux_inputs
from stratum.types import BlockRef, GridDef, TileRef

# the synthetic run grid: 20 x 20 cells of 0.001 deg at the origin, four 10 x 10 blocks
GRID = GridDef("EPSG:4326", (0.001, -0.001), (0.0, 0.0), 0.02, block_size=10)
TILE = TileRef(GRID, 0, 0)


# ------------------------------------------------------------------------------------ fixtures
def write_source(path, values, *, transform, crs="EPSG:4326", nodata=None, dtype=None):
    values = np.asarray(values)
    dtype = dtype or values.dtype.name
    with rasterio.open(path, "w", driver="GTiff", height=values.shape[0], width=values.shape[1],
                       count=1, dtype=dtype, crs=crs, transform=transform, nodata=nodata) as dst:
        dst.write(values.astype(dtype), 1)
    return path


def aligned_source(tmp_path, values, name="aligned.tif", **kw):
    """A source on the tile's own lattice: 20 x 20 at 0.001 deg from (0, 0.02)."""
    return write_source(tmp_path / name, values, transform=Affine(0.001, 0, 0.0, 0, -0.001, 0.02),
                        **kw)


def source_for(path, alias="landcover", kind="categorical", resampling="nearest"):
    paths = (path,) if not isinstance(path, (list, tuple)) else tuple(path)
    return AuxSource(alias=alias, uri=f"file://{paths[0]}", paths=paths,
                     digest=sources_digest(paths), kind=kind, resampling=resampling)


# ---------------------------------------------------------------------------- 05 section 1: the warp
def test_a_source_on_the_tile_lattice_warps_to_itself(tmp_path):
    """The identity case. If this is not exact, nothing measured against a warp means anything."""
    values = np.arange(400, dtype="uint8").reshape(20, 20) % 100
    out = warp_to_tile([aligned_source(tmp_path, values)], TILE,
                       resampling="nearest", dtype="uint8", nodata=0)
    assert out.shape == (20, 20)
    np.testing.assert_array_equal(out, values)


def test_a_source_in_another_crs_comes_back_on_the_grid(tmp_path):
    """05 section 1: a scorer never sees a CRS. The source is Web Mercator at ~100 m; what comes
    back is the tile's 20 x 20 in EPSG:4326, and the plugin cannot tell."""
    # The tile is 0.02 deg square at the equator, so about 2226 m square in Web Mercator.
    # 40 x 40 cells of 100 m from (-200, 2400) spans x [-200, 3800], y [-1600, 2400] - the whole
    # tile with room to spare, so every destination cell has a source cell behind it.
    src = write_source(tmp_path / "merc.tif", np.full((40, 40), 3, dtype="uint8"),
                       transform=Affine(100.0, 0, -200.0, 0, -100.0, 2400.0), crs="EPSG:3857",
                       nodata=0)
    out = warp_to_tile([src], TILE, resampling="nearest", dtype="uint8", nodata=0)
    assert out.shape == (20, 20)
    assert (out == 3).all(), "the reprojected source should cover the whole tile"


def test_categorical_resampling_choices_actually_differ(tmp_path):
    """05 section 2 makes `nearest` vs `mode` a declared choice because it changes the answer.
    A checkerboard downsampled 4x is the smallest case where it demonstrably does."""
    fine = np.indices((80, 80)).sum(axis=0) % 2 + 1          # alternating 1/2, 4x the tile
    src = write_source(tmp_path / "fine.tif", fine.astype("uint8"),
                       transform=Affine(0.00025, 0, 0.0, 0, -0.00025, 0.02), nodata=0)
    near = warp_to_tile([src], TILE, resampling="nearest", dtype="uint8", nodata=0)
    mode = warp_to_tile([src], TILE, resampling="mode", dtype="uint8", nodata=0)
    assert (near != mode).any(), "nearest and mode must not be interchangeable"


def test_an_unknown_resampling_is_refused_by_name(tmp_path):
    with pytest.raises(AuxError, match="05 section 2"):
        warp_to_tile([aligned_source(tmp_path, np.ones((20, 20), "uint8"))], TILE,
                     resampling="lanczos", dtype="uint8", nodata=0)


def test_sources_are_composited_later_over_earlier(tmp_path):
    """A mosaic: each source contributes only where it has data, so adjacent tiles join without a
    seam. Declaring a continental DEM as four files must not blank the three that do not reach."""
    left = np.zeros((20, 20), dtype="uint8")
    left[:, :10] = 5
    right = np.zeros((20, 20), dtype="uint8")
    right[:, 10:] = 9
    a = aligned_source(tmp_path, left, "a.tif", nodata=0)
    b = aligned_source(tmp_path, right, "b.tif", nodata=0)
    out = warp_to_tile([a, b], TILE, resampling="nearest", dtype="uint8", nodata=0)
    assert (out[:, :10] == 5).all() and (out[:, 10:] == 9).all()


def test_a_source_that_misses_the_tile_is_skipped_not_blanked(tmp_path):
    """The disjoint fast path must not overwrite what an earlier source wrote."""
    near = aligned_source(tmp_path, np.full((20, 20), 7, "uint8"), "near.tif", nodata=0)
    far = write_source(tmp_path / "far.tif", np.full((20, 20), 1, "uint8"),
                       transform=Affine(0.001, 0, 50.0, 0, -0.001, 50.0), nodata=0)
    out = warp_to_tile([near, far], TILE, resampling="nearest", dtype="uint8", nodata=0)
    assert (out == 7).all()


def test_continuous_gaps_come_back_as_nan(tmp_path):
    """11 section 2: a fill is never a sentinel above the reader. A continuous aux source's
    uncovered cells are NaN, so `> threshold` is False rather than accidentally true."""
    values = np.full((20, 20), -9999.0, dtype="float32")
    values[:10] = 0.5
    src = aligned_source(tmp_path, values, "c.tif", nodata=-9999.0, dtype="float32")
    out = warp_to_tile([src], TILE, resampling="bilinear", dtype="float32", nodata=np.nan)
    assert np.isnan(out[15]).all() and np.allclose(out[5], 0.5)


# --------------------------------------------------------------------- 05 section 4: warp once, slice many
def test_the_warp_is_cached_and_a_second_call_is_a_hit(tmp_path):
    cache = CacheRoot(tmp_path / "root")
    source = source_for(aligned_source(tmp_path, np.full((20, 20), 4, "uint8")))
    key = warp_aux_tile(cache, TILE, source)
    assert key.path.is_file() and cache.hit(key)
    mtime = key.path.stat().st_mtime_ns
    assert warp_aux_tile(cache, TILE, source).hash == key.hash
    assert key.path.stat().st_mtime_ns == mtime, "a hit must not rewrite the artifact"


def test_the_key_moves_with_everything_that_determines_the_warp(tmp_path):
    """06 section 2: source content, grid and resampling. And NOT with the block size, which is a
    compute knob that never changes output (01 section 3)."""
    cache = CacheRoot(tmp_path / "root")
    base = source_for(aligned_source(tmp_path, np.full((20, 20), 4, "uint8")))
    key = aux_key_for(cache, TILE, base)

    other = source_for(aligned_source(tmp_path, np.full((20, 20), 5, "uint8"), "b.tif"))
    assert aux_key_for(cache, TILE, other).hash != key.hash, "content must move the key"

    import dataclasses
    assert aux_key_for(cache, TILE, dataclasses.replace(base, resampling="mode")).hash != key.hash
    assert aux_key_for(cache, TILE, dataclasses.replace(base, alias="other")).hash != key.hash

    coarse = TileRef(GridDef("EPSG:4326", (0.002, -0.002), (0.0, 0.0), 0.02, block_size=10), 0, 0)
    assert aux_key_for(cache, coarse, base).hash != key.hash, "the grid must move the key"

    blocky = TileRef(GridDef("EPSG:4326", (0.001, -0.001), (0.0, 0.0), 0.02, block_size=5), 0, 0)
    assert aux_key_for(cache, blocky, base).hash == key.hash, "block_size must NOT move the key"


def test_aux_inputs_carries_exactly_the_spec_fields():
    """06 section 2. The `*_inputs` builders are the single source of truth for what enters a
    key, so the field set is asserted here rather than left to drift."""
    fields = set(aux_inputs("dem", "sha256:abc", GRID, "bilinear", WARP_ALGO_VERSION))
    assert fields == {"artifact_type", "alias", "source_digest", "grid_def", "resampling",
                      "warp_algo_version"}


def test_the_recorded_algorithm_hash_is_current():
    """06 section 3, rule 2. A change to the warp that does not bump WARP_ALGO_VERSION would
    serve every previously cached raster forever."""
    ok, message = check_recorded_hash()
    assert ok, message
    assert module_content_hash().startswith("sha256:")


# ------------------------------------------------------------------- 05 section 3: what a plugin sees
def test_the_accessor_returns_the_block_window_on_the_block_grid(tmp_path):
    cache = CacheRoot(tmp_path / "root")
    values = np.arange(400, dtype="uint8").reshape(20, 20) % 7 + 1
    source = source_for(aligned_source(tmp_path, values))
    key = warp_aux_tile(cache, TILE, source)

    block = BlockRef(TILE, 1, 1)            # the lower-right 10 x 10
    aux = BlockAux({source.alias: key}, {source.alias: source}, block)
    out = aux.raster("landcover")
    assert out.shape == (10, 10)
    np.testing.assert_array_equal(out, values[10:, 10:])


def test_a_halo_reaching_past_the_tile_is_filled_not_refused(tmp_path):
    """01 section 3: a block's window includes its halo, and at a tile edge that runs off the
    file with negative offsets. `read_glt` handles this with a boundless read and so must this,
    or every scorer with a halo fails on the edge blocks of every tile."""
    cache = CacheRoot(tmp_path / "root")
    source = source_for(aligned_source(tmp_path, np.full((20, 20), 6, "uint8")))
    key = warp_aux_tile(cache, TILE, source)

    block = BlockRef(TILE, 0, 0, halo=2)
    assert block.window.row_off < 0 and block.window.col_off < 0
    out = BlockAux({source.alias: key}, {source.alias: source}, block).raster("landcover")
    assert out.shape == (block.window.height, block.window.width)
    assert (out[:2, :] == 0).all() and (out[:, :2] == 0).all(), "the halo outside the tile is nodata"
    assert (out[2:, 2:] == 6).all()


def test_an_undeclared_alias_raises_and_never_falls_back(tmp_path):
    """05 section 5, the load-bearing rule: a plugin that opens an undeclared source produces a
    cache key that lies. The accessor holds only what the key was built from, so there is nothing
    to fall back to."""
    aux = BlockAux({}, {}, BlockRef(TILE, 0, 0))
    with pytest.raises(AuxError, match="required_aux"):
        aux.raster("slope")


def test_date_keyed_aux_is_refused_by_name(tmp_path):
    """Resolving "the nearest date that exists" needs a listing of what exists, and a run never
    queries a catalogue (02 section 6). Refused, naming the spec, rather than guessed."""
    aux = BlockAux({}, {}, BlockRef(TILE, 0, 0))
    with pytest.raises(NotImplementedError, match="05 section 2"):
        aux.raster("snow", date=datetime(2026, 1, 1, tzinfo=UTC))


def test_the_rest_of_the_accessor_still_refuses(tmp_path):
    """05 section 3: only raster() is built, and the others say so rather than returning
    something plausible."""
    aux = BlockAux({}, {}, BlockRef(TILE, 0, 0))
    for call in (lambda: aux.vector("x"), lambda: aux.features("x"),
                 lambda: aux.distance("x"), lambda: aux.table("x"),
                 lambda: aux.granule_index):
        with pytest.raises(NotImplementedError, match="05 section 3"):
            call()


# ------------------------------------------------------------------------------------- identity
def test_a_source_digest_is_content_not_path(tmp_path):
    """06 section 3, rule 4: a file swapped in place under a stable URI must not read as the same
    source. The digest is of the bytes, so two paths with equal content agree and one path with
    changed content does not."""
    same_a = aligned_source(tmp_path, np.ones((20, 20), "uint8"), "x.tif")
    same_b = aligned_source(tmp_path, np.ones((20, 20), "uint8"), "y.tif")
    assert file_digest(same_a) == file_digest(same_b)
    changed = aligned_source(tmp_path, np.full((20, 20), 2, "uint8"), "x.tif")
    assert file_digest(changed) != file_digest(same_b)


def test_a_mosaics_identity_depends_on_its_order(tmp_path):
    """Later sources win, so reordering is a different artifact and must be a different key."""
    a = aligned_source(tmp_path, np.ones((20, 20), "uint8"), "a.tif")
    b = aligned_source(tmp_path, np.full((20, 20), 2, "uint8"), "b.tif")
    assert sources_digest([a, b]) != sources_digest([b, a])


def test_read_window_of_a_written_raster_round_trips(tmp_path):
    from stratum.ancillary import write_raster
    from stratum.types import Window

    values = (np.arange(400, dtype="float32").reshape(20, 20) / 10.0)
    path = write_raster(tmp_path / "w.tif", values, TILE, nodata=np.nan, tags={"alias": "t"})
    got = read_window(path, Window(4, 6, 5, 3), nodata=np.nan)
    np.testing.assert_allclose(got, values[4:9, 6:9])


# --------------------------------------------------------------- the no-aux run is untouched
def test_a_run_with_no_aux_produces_the_keys_it_produced_before_aux_existed(tmp_path):
    """The whole feature must be invisible to a manifest that does not use it.

    `snapshot_inputs` has always emitted `"aux_keys": []`, and `observation_inputs` gains
    `mask_aux_keys` only when a map-space mask declares aux - so every artifact cached before
    this change is still a hit after it. Asserted on the key documents themselves, not on field
    names, because a field set can match while a value drifts.
    """
    from synthetic import NadirScorer, build_plan, two_overlapping

    from stratum.resolve import snapshot_key
    from stratum.resolve.block import (
        ResolveItem,
        aux_keys_for,
        observation_inputs,
        reached_observations,
    )
    from stratum.types import canonical_hash

    plan = build_plan(tmp_path, two_overlapping(), scorer=NadirScorer())
    assert plan.aux_to_read() == [] and plan.map_mask_aux() == []
    assert aux_keys_for(plan, TileRef(plan.grid, 0, 0)) == {}

    item = {"tile": [0, 0], "epoch": ["2026-06-01T00:00:00+00:00", "2026-07-01T00:00:00+00:00"],
            "block": [0, 0]}
    assert snapshot_key(item, plan).inputs["aux_keys"] == [], \
        "a no-aux run carries an empty aux_keys, not a missing one"

    parsed = ResolveItem.parse(item, plan)
    seen = 0
    for granule, glt_key, _ in reached_observations(plan, parsed):
        obs = observation_inputs(plan, glt_key, granule, {})
        assert "mask_aux_keys" not in obs, "no mask declares aux; the field must be absent"
        assert canonical_hash(obs) == canonical_hash(
            observation_inputs(plan, glt_key, granule)), "passing aux_keys must not move the key"
        seen += 1
    assert seen, "the fixture should reach this block"


def test_aux_keys_for_does_no_io(tmp_path, monkeypatch):
    """`snapshot_key` is recomputed by `product_key` once per epoch per block through the whole
    reduce stage, and again at publish. A stat or an open inside `aux_keys_for` would turn that
    into an IO storm of O(blocks x epochs)."""
    import os
    from pathlib import Path

    from synthetic import NadirScorer, build_plan, two_overlapping

    from stratum.resolve.block import aux_keys_for

    plan = build_plan(tmp_path, two_overlapping(), scorer=NadirScorer())

    def boom(*args, **kwargs):
        pytest.fail("aux_keys_for touched the filesystem")

    monkeypatch.setattr(Path, "open", boom)
    monkeypatch.setattr(Path, "is_file", boom)
    monkeypatch.setattr(Path, "exists", boom)
    monkeypatch.setattr(os, "stat", boom)
    assert aux_keys_for(plan, TileRef(plan.grid, 0, 0)) == {}


# --------------------------------------------------------- the first real consumer, end to end
def test_a_map_mask_reads_aux_through_the_whole_resolve_path(tmp_path):
    """The point of all of it: a mask declares an alias, the manifest declares a source, and the
    mask excludes cells on ground truth it never had to fetch, warp or window itself.

    Runs the real `resolve_block` over the synthetic fixture, so the accessor is constructed the
    way a worker constructs it and the snapshot is the one a worker would write.
    """
    from stratum_emit.masks import Landcover
    from synthetic import NadirScorer, build_plan, two_overlapping

    from stratum.resolve import PluginBinding, read_snapshot, resolve_block

    plan = build_plan(tmp_path, two_overlapping(), scorer=NadirScorer())
    tile = TileRef(plan.grid, 0, 0)

    # water over the left half of the tile, bare ground over the right
    cover = np.full((20, 20), 60, dtype="uint8")        # bare/sparse vegetation
    cover[:, :10] = 80                                  # open water
    source = source_for(aligned_source(tmp_path, cover, "cover.tif", nodata=0))
    mask = Landcover(exclude=("water",))
    plan.aux_sources = {"landcover": source}
    plan.masks = [*plan.masks, PluginBinding(ref="landcover", version="test",
                                             params={"exclude": ["water"]}, instance=mask)]
    warp_aux_tile(plan.cache, tile, source)

    item = {"tile": [0, 0], "epoch": ["2026-06-01T00:00:00+00:00", "2026-07-01T00:00:00+00:00"],
            "block": [0, 0]}
    key = resolve_block(item, plan)
    valid = read_snapshot(key.path)["valid"]

    # the block is the tile's upper-left 10 x 10, which is entirely the water half
    assert not valid.any(), "every cell the mask called water must be invalid"

    # the bare half of the tile does still resolve
    bare = read_snapshot(resolve_block({**item, "block": [1, 0]}, plan).path)["valid"]
    assert bare.any(), "the bare half must still resolve"

    # the aux source is in the snapshot key, so swapping the raster invalidates the snapshot
    assert key.inputs["aux_keys"] == [aux_key_for(plan.cache, tile, source).hash]

    # and in the MASKED OBSERVATION key too - a mask that reads a raster is determined by it,
    # which `pixel_mask_spec` and `mask_plugin_version` do not capture (06 section 2)
    from stratum.resolve.block import (
        ResolveItem,
        aux_keys_for,
        observation_inputs,
        reached_observations,
    )

    parsed = ResolveItem.parse(item, plan)
    keys = aux_keys_for(plan, tile)
    for granule, glt_key, _ in reached_observations(plan, parsed):
        assert observation_inputs(plan, glt_key, granule, keys)["mask_aux_keys"] == {
            "landcover": keys["landcover"].hash}


# --------------------------------------------------------- 12 section 2: ortho-native roles
def test_an_ortho_role_is_warped_onto_the_block_beside_a_sensor_role(tmp_path):
    """The FRCOV shape, on synthetic data: a role whose product is already on a map grid joins
    `bands` like any other, without a sensor window, a GLT gather or a sensor mask.

    The source is deliberately at a DIFFERENT resolution from the run grid (2x coarser, as FRCOV
    is ~1.95x coarser than one arcsecond), so the warp is a real resample and not a copy.
    """
    import dataclasses

    from stratum_emit.readers.geotiff import L2BFrcovTiff
    from synthetic import NadirScorer, build_plan, two_overlapping

    from stratum.resolve.block import ResolveItem, reached_observations
    from stratum.resolve.observation import read_ortho_roles

    plan = build_plan(tmp_path, two_overlapping(), scorer=NadirScorer())

    # a 10 x 10 source at 0.002 deg over the tile: bare (0.9) north, vegetated (0.1) south
    soil = np.full((10, 10), 0.9, dtype="float32")
    soil[5:] = 0.1
    path = write_source(tmp_path / "frcov.tif", soil,
                        transform=Affine(0.002, 0, 0.0, 0, -0.002, 0.02), nodata=-9999.0)
    with rasterio.open(path, "r+") as dst:
        dst.set_band_description(1, "EMIT_L2B_FRCOVBARE")

    # bind it as an ortho role on every granule the fixture has
    granules = {gid: dataclasses.replace(ref, assets={**ref.assets, "FRCOV/BARE": str(path)})
                for gid, ref in plan.granules.items()}
    plan.granules = granules
    plan.roles = {**plan.roles, "frcov": type(next(iter(plan.roles.values())))(
        collection="FRCOV", var="EMIT_L2B_FRCOVBARE", asset="BARE",
        space="ortho", resampling="bilinear")}
    plan.reader_lookup = lambda c: L2BFrcovTiff() if c == "FRCOV" else plan.reader(c)

    item = {"tile": [0, 0], "epoch": ["2026-06-01T00:00:00+00:00", "2026-07-01T00:00:00+00:00"],
            "block": [0, 0]}
    parsed = ResolveItem.parse(item, plan)
    granule, glt_key, glt = reached_observations(plan, parsed)[0]

    # the fixture's store is an in-memory fake for the synthetic sensor granules; the ortho role
    # is a real file on disk, so it opens through the real store (12 section 4, stage-in is
    # identity for a local path)
    from stratum.access import AssetStore
    from stratum.resolve.observation import ObsContext

    plan.store = AssetStore()
    ctx = ObsContext(plan, parsed.block, parsed.epoch, granule, glt_key, glt)
    got = read_ortho_roles(ctx, ["frcov"])["frcov"]

    assert got.shape == (parsed.block.window.height, parsed.block.window.width)
    assert np.allclose(got[:9], 0.9), "the block sits in the bare half"
    # The last row is the proof that this is a RESAMPLE and not a copy. The block's row 9 has
    # its centre at y=0.0105, between source cell centres 0.011 (0.9) and 0.009 (0.1), so
    # bilinear puts it a quarter of the way across: 0.9 - 0.25 * 0.8 = 0.7.
    assert np.allclose(got[9], 0.7), f"expected the boundary blend, got {got[9][:3]}"

    # and it is cached per (granule, role, tile): the second call opens no reader at all
    plan.reader_lookup = lambda c: (_ for _ in ()).throw(
        AssertionError("a cached ortho warp must not open the granule again"))
    again = read_ortho_roles(ctx, ["frcov"])["frcov"]
    np.testing.assert_array_equal(again, got)


# ----------------------------------------------------- the bucket path: a mirror is not the bucket
def test_the_accessor_pulls_its_warp_from_a_bucket_before_reading(monkeypatch, tmp_path):
    """A worker's `key.path` is a node-local MIRROR, not the artifact.

    The planner warps aux into the artifact cache, which on a bucket root means the bytes land in
    S3. A worker's mirror is empty until something pulls them down, and `CacheRoot.hit` is what
    does that. Reading `key.path` directly works on the machine that planned and fails on every
    other one - which is exactly how this shipped and broke 861 of 1184 items in Lambda.

    Two workspaces over one bucket stand in for the planner and the worker.
    """
    from fake_s3 import install

    from stratum.cache import CacheRoot
    from stratum.storage import Workspace

    install(monkeypatch, tmp_path / "bucket")
    planner = Workspace.for_root("s3://stratum-test/", mirror=tmp_path / "planner-mirror")
    worker = Workspace.for_root("s3://stratum-test/", mirror=tmp_path / "worker-mirror")

    source = source_for(aligned_source(tmp_path, np.full((20, 20), 4, "uint8")))
    key = warp_aux_tile(CacheRoot(planner), TILE, source)          # the planner warps
    assert key.path.is_file(), "the planner's own mirror has it"

    worker_cache = CacheRoot(worker)
    worker_key = aux_key_for(worker_cache, TILE, source)
    assert not worker_key.path.exists(), "the worker's mirror starts empty - the bug's precondition"

    block = BlockRef(TILE, 0, 0)
    aux = BlockAux({source.alias: worker_key}, {source.alias: source}, block, worker_cache)
    out = aux.raster("landcover")                                   # must pull, not fail
    assert out.shape == (10, 10) and (out == 4).all()


def test_the_accessor_says_which_tile_is_missing_rather_than_naming_a_path(tmp_path):
    """A warp the planner never made is a disagreement between the plan and the worker, and the
    message should say so - `No such file or directory` on a mirror path does not."""
    cache = CacheRoot(tmp_path / "root")
    source = source_for(aligned_source(tmp_path, np.full((20, 20), 4, "uint8")))
    key = aux_key_for(cache, TILE, source)                          # keyed, never written
    aux = BlockAux({source.alias: key}, {source.alias: source}, BlockRef(TILE, 0, 0), cache)
    with pytest.raises(AuxError, match="planner warps every declared source"):
        aux.raster("landcover")
