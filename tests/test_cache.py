"""stratum.cache: key layout, sensitivity, atomic writes, explain/diff (06)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from stratum.cache import (
    CacheKey,
    CacheRoot,
    glt_inputs,
    grid_def_fields,
    product_inputs,
    snapshot_inputs,
)
from stratum.types import GridDef, TileRef, canonical_hash

GRID = GridDef("EPSG:4326", (0.001, -0.001), (0.0, 0.0), 0.01, block_size=512)
TILE = TileRef(GRID, 3, -2)


@pytest.fixture
def root(tmp_path: Path) -> CacheRoot:
    return CacheRoot(tmp_path)


def _glt_key(root: CacheRoot, grid: GridDef = GRID, max_distance: float = 0.002,
             granule: str = "001_20260601T000000_2600000_001") -> CacheKey:
    inputs = glt_inputs(granule, grid, max_distance, "kdtree", 1)
    return root.key("glt", grid.id, TILE, inputs)


# ------------------------------------------------------------------------------------- layout
def test_key_layout_file_artifact(root: CacheRoot) -> None:
    key = _glt_key(root)
    assert key.hash == canonical_hash(key.inputs)
    assert key.hash16 == key.hash[7:23] and len(key.hash16) == 16
    assert key.path == root.root / "cache" / "glt" / GRID.id / "3_-2" / f"{key.hash16}.tif"
    assert key.inputs_path == key.path.with_name(f"{key.hash16}.inputs.json")


def test_key_layout_dir_artifact(root: CacheRoot) -> None:
    inputs = product_inputs(["sha256:a"], [], "sha256:agg")
    key = root.key("product", GRID.id, TILE, inputs)
    assert key.path == root.root / "cache" / "product" / GRID.id / "3_-2" / key.hash16
    assert key.path.suffix == ""
    assert key.inputs_path == key.path / ".inputs.json"


def test_unknown_artifact_refused(root: CacheRoot) -> None:
    with pytest.raises(ValueError, match="unknown artifact type"):
        root.key("mystery", GRID.id, TILE, {})


# -------------------------------------------------------------------------------- sensitivity
def test_glt_key_ignores_block_size(root: CacheRoot) -> None:
    small = GridDef(GRID.crs, GRID.resolution, GRID.origin, GRID.tile_size, block_size=256)
    assert small.id == GRID.id
    assert _glt_key(root, GRID).path == _glt_key(root, small).path
    assert "block" not in grid_def_fields(GRID)


def test_glt_key_changes_with_max_distance(root: CacheRoot) -> None:
    assert _glt_key(root, max_distance=0.002).path != _glt_key(root, max_distance=0.003).path


def test_artifact_type_is_in_every_key(root: CacheRoot) -> None:
    g = glt_inputs("g", GRID, 0.002, "kdtree", 1)
    s = snapshot_inputs([], [], "r", "v", {}, "sha256:l", ("2026-06-01", "2026-07-01"))
    p = product_inputs([], [], "sha256:a")
    assert (g["artifact_type"], s["artifact_type"], p["artifact_type"]) == \
        ("glt", "snapshot", "product")


def test_scorer_change_invalidates_snapshot_not_glt(root: CacheRoot) -> None:
    """ADR-0001 section 10, test 2 - the property the iteration story rests on."""
    glt = _glt_key(root)
    snap_a = snapshot_inputs([glt], [], "min_view_zenith", "1", {"k": 1}, "sha256:l",
                             ("2026-06-01", "2026-07-01"))
    snap_b = snapshot_inputs([glt], [], "min_view_zenith", "2", {"k": 1}, "sha256:l",
                             ("2026-06-01", "2026-07-01"))
    assert canonical_hash(snap_a) != canonical_hash(snap_b)
    assert _glt_key(root).path == glt.path
    assert snap_a["obs_keys"] == [glt.hash]


def test_input_builders_carry_exactly_the_spec_fields() -> None:
    g = glt_inputs("g", GRID, 0.002, "kdtree", 1)
    assert set(g) == {"artifact_type", "granule_id", "grid_def", "max_distance",
                      "regrid_method", "regrid_algo_version"}
    assert set(g["grid_def"]) == {"crs", "resolution", "origin", "tile_size"}
    s = snapshot_inputs(["sha256:o"], ["sha256:x"], "r", "v", {"b": 2, "a": 1}, "sha256:l",
                        ("2026-06-01T00:00:00+00:00", "2026-07-01T00:00:00+00:00"))
    assert set(s) == {"artifact_type", "obs_keys", "aux_keys", "scorer_ref", "scorer_version",
                      "scorer_params", "layers_hash", "epoch_bounds"}
    assert s["epoch_bounds"] == ["2026-06-01T00:00:00+00:00", "2026-07-01T00:00:00+00:00"]
    p = product_inputs(["sha256:s"], [], "sha256:agg", {"ref": "r", "version": "1", "params": {}})
    assert set(p) == {"artifact_type", "snapshot_keys", "aux_keys", "aggregate_hash", "reducer"}
    assert product_inputs([], [], "sha256:agg")["reducer"] is None
    for doc in (g, s, p):
        json.dumps(doc)  # plain, JSON-able


# ------------------------------------------------------------------------------------- writes
def test_hit_requires_artifact_and_sidecar(root: CacheRoot) -> None:
    key = _glt_key(root)
    assert not root.hit(key)
    root.write_file(key, lambda p: p.write_bytes(b"glt"))
    assert root.hit(key)
    assert key.path.read_bytes() == b"glt"
    assert json.loads(key.inputs_path.read_text()) == key.inputs
    key.inputs_path.unlink()
    assert not root.hit(key)


def test_write_file_is_atomic_when_writer_raises(root: CacheRoot) -> None:
    key = _glt_key(root)

    def bad(p: Path) -> None:
        p.write_bytes(b"partial")
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        root.write_file(key, bad)
    assert not key.path.exists()
    assert not key.inputs_path.exists()
    assert list(key.path.parent.iterdir()) == []
    assert not root.hit(key)


def test_write_file_refuses_dir_artifact(root: CacheRoot) -> None:
    key = root.key("snapshot", GRID.id, TILE, {"artifact_type": "snapshot"})
    with pytest.raises(ValueError, match="write_dir"):
        root.write_file(key, lambda p: None)
    with pytest.raises(ValueError, match="write_file"):
        root.write_dir(_glt_key(root), lambda p: None)


def test_write_dir_commits_whole_directory(root: CacheRoot) -> None:
    key = root.key("snapshot", GRID.id, TILE,
                   snapshot_inputs([], [], "r", "v", {}, "sha256:l", ("a", "b")))

    def write(d: Path) -> None:
        (d / "layer.tif").write_bytes(b"x")
        (d / "score.tif").write_bytes(b"y")

    root.write_dir(key, write)
    assert root.hit(key)
    assert sorted(p.name for p in key.path.iterdir()) == [".inputs.json", "layer.tif", "score.tif"]
    # a rewrite over an existing entry replaces it whole
    root.write_dir(key, lambda d: (d / "only.tif").write_bytes(b"z"))
    assert sorted(p.name for p in key.path.iterdir()) == [".inputs.json", "only.tif"]


def test_write_dir_is_atomic_when_writer_raises(root: CacheRoot) -> None:
    key = root.key("product", GRID.id, TILE, product_inputs([], [], "sha256:agg"))

    def bad(d: Path) -> None:
        (d / "layer.tif").write_bytes(b"partial")
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        root.write_dir(key, bad)
    assert not key.path.exists()
    assert list(key.path.parent.iterdir()) == []
    assert not root.hit(key)


# ------------------------------------------------------------------------------ explain/diff
def test_explain_and_diff(root: CacheRoot) -> None:
    a = _glt_key(root, max_distance=0.002)
    b = _glt_key(root, max_distance=0.003)
    assert root.explain(a) == a.inputs  # not yet written: the in-memory inputs
    root.write_file(a, lambda p: p.write_bytes(b"a"))
    root.write_file(b, lambda p: p.write_bytes(b"b"))
    assert root.explain(a) == a.inputs
    assert root.explain(a.path) == a.inputs
    assert root.explain(str(a.inputs_path)) == a.inputs
    assert root.diff(a, b) == {"max_distance": (0.002, 0.003)}
    assert root.diff(a.path, b.path) == {"max_distance": (0.002, 0.003)}
    assert root.diff(a, a) == {}


def test_diff_flattens_nested_fields(root: CacheRoot) -> None:
    other = GridDef(GRID.crs, (0.002, -0.002), GRID.origin, GRID.tile_size)
    a, b = _glt_key(root, GRID), _glt_key(root, other)
    assert root.diff(a, b) == {"grid_def.resolution": ([0.001, -0.001], [0.002, -0.002])}


def test_explain_missing_entry(root: CacheRoot, tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        root.explain(tmp_path / "nothing.tif")
