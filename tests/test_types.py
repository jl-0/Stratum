"""Spec 11 in executable form. Every assertion cites the spec section it guards."""
from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pyarrow as pa
import pytest

from stratum.types import (
    Aggregation,
    BlockRef,
    ClassTable,
    GridDef,
    LayerSpec,
    SensorWindow,
    SnapshotSchema,
    TileRef,
)

# One arcsecond: 3600 cells per degree, block 720 divides it exactly (01 section 1).
ARC = 1 / 3600
GRID = GridDef(crs="EPSG:4326", resolution=(ARC, -ARC), origin=(-180.0, -90.0),
               tile_size=1.0, block_size=720)


def test_grid_id_is_stable_and_ignores_block():  # 01 section 3: block never changes output
    same = GridDef("EPSG:4326", (ARC, -ARC), (-180.0, -90.0), 1.0, block_size=512)
    assert GRID.id == same.id and len(GRID.id) == 16


def test_grid_guard_rails():  # 01 section 1
    with pytest.raises(ValueError):
        GridDef("EPSG:4326", (ARC, ARC), (-180.0, -90.0), 1.0)


def test_tile_is_named_by_position_and_divides_cleanly():  # 01 section 2
    tile = TileRef(GRID, -111, 32)
    assert tile.name == f"{GRID.id}/-111_32"
    assert GRID.divides
    assert tile.shape == (3600, 3600)
    x0, y0, x1, y1 = tile.bounds
    assert (round(x0, 9), round(y0, 9), round(x1, 9), round(y1, 9)) == (-111.0, 32.0, -110.0, 33.0)


def test_non_dividing_grid_still_tiles_without_overlap_or_gap():  # 01 section 1
    amd = GridDef("EPSG:4326", (0.0003, -0.0003), (-180.0, -90.0), 1.0, block_size=512)
    assert not amd.divides
    a, b = TileRef(amd, -111, 32), TileRef(amd, -110, 32)
    assert a.col_range[1] == b.col_range[0]              # shared edge, on the lattice
    assert a.shape[1] in (3333, 3334) and b.shape[1] in (3333, 3334)
    assert abs(a.bounds[2] - (-110.0)) < 0.0003          # within one cell of the nominal edge
    v002 = GridDef("EPSG:4326", (0.00055, -0.00055), (-180.0, -90.0), 5.0)
    assert TileRef(v002, -22, 6).shape[0] in (9090, 9091)


def test_tile_block_count_and_edge_blocks_clip():  # 01 section 3
    assert len(TileRef(GRID, 0, 0).blocks()) == 25          # 720 divides 3600: no sliver
    g512 = GridDef("EPSG:4326", (ARC, -ARC), (-180.0, -90.0), 1.0, block_size=512)
    blocks = TileRef(g512, 0, 0).blocks()
    assert len(blocks) == 64                                # 8 x 8, the last one 16 cells wide
    assert blocks[-1].core_window.width == 3600 - 7 * 512


def test_block_windows_differ_by_halo():  # 11 section 3: two windows, deliberately
    b = BlockRef(TileRef(GRID, 0, 0), 1, 1, halo=3)
    assert b.core_window.row_off == 720 and b.window.row_off == 717
    assert b.window.height == b.core_window.height + 6


def test_sensor_window_is_1_based_and_carries_origin():  # 11 section 10, 12 section 2
    glt_x = np.array([[0, 5, -7], [9, 0, 6]])
    glt_y = np.array([[0, 100, -102], [110, 0, 101]])
    sw = SensorWindow.covering(glt_x, glt_y)
    assert (sw.row0, sw.col0) == (99, 4)
    assert (sw.height, sw.width) == (11, 5)
    assert sw.slices == (slice(99, 110), slice(4, 9))


def _table() -> ClassTable:
    t = pa.table({"index": [1, 2], "name": ["goethite", "hematite"], "record": [882, 5880]})
    return ClassTable(key="index", entries=t, source="test")


def test_schema_hashes_split_layers_from_aggregation():  # 13 section 5
    layer = LayerSpec("mineral_1", "categorical", "mineral",
                      Aggregation("vote", {"min_count": 3}), classes=_table())
    s1 = SnapshotSchema("cm-v1", [layer])
    s2 = SnapshotSchema("cm-v1", [LayerSpec("mineral_1", "categorical", "mineral",
                                            Aggregation("best"), classes=_table())])
    assert s1.layers_hash == s2.layers_hash          # aggregation changed: snapshots survive
    assert s1.aggregate_hash != s2.aggregate_hash    # ...but product blocks do not
    s3 = SnapshotSchema("cm-v1", [LayerSpec("mineral_1", "categorical", "mineral_2",
                                            Aggregation("vote", {"min_count": 3}),
                                            classes=_table())])
    assert s1.layers_hash != s3.layers_hash          # source changed: redefine


def test_schema_rejects_framework_layer_names():  # 13 section 2
    with pytest.raises(ValueError):
        SnapshotSchema("x", [LayerSpec("score", "continuous", "a", Aggregation("median"))])


def test_class_table_fingerprint_ignores_column_order():  # 11 section 9
    a = pa.table({"index": [1], "name": ["goethite"]})
    b = pa.table({"name": ["goethite"], "index": [1]})
    assert ClassTable("index", a, "x").fingerprint() == ClassTable("index", b, "y").fingerprint()


def test_epoch_bounds_are_half_open():  # 11 section 4
    from stratum.types import Epoch
    e = Epoch(datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 9, 1, tzinfo=UTC))
    assert e.contains(datetime(2026, 8, 31, 23, 59, tzinfo=UTC))
    assert not e.contains(e.end)
