"""The publish stage on synthetic data: stitching, data COGs, config mappers, legend, STAC,
provenance (07, 10, 13 section 3, 11 sections 8-9).

The tile is 32 x 32 cells in four 16-cell blocks; two of the four blocks are written so the
uncovered half exercises nodata fill. Every colour assertion reads the value back from the
resolved colour set rather than restating the table.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pyarrow as pa
import pytest
import rasterio

from stratum import __version__
from stratum.publish import (
    RAMPS,
    AlphaFrom,
    CategoricalMapper,
    ContinuousMapper,
    UnmappedClassError,
    build_mapper,
    build_provenance,
    check_formats,
    internal_tile_size,
    palette_color,
    publish_period,
    read_provenance,
    render,
    resolve_class_colors,
    stitch,
    write_classes,
    write_data_cogs,
    write_legend,
    write_product_block,
    write_provenance,
    write_stac_collection,
)
from stratum.reduce import CATEGORICAL_NODATA, delivered_bands
from stratum.types import (
    Aggregation,
    BandSpec,
    BandStack,
    BlockRef,
    ClassTable,
    Epoch,
    GridDef,
    LayerSpec,
    SnapshotSchema,
    TileRef,
)

ND = CATEGORICAL_NODATA
GRID = GridDef(crs="EPSG:4326", resolution=(0.01, -0.01), origin=(-180.0, -90.0),
               tile_size=0.32, block=16)
TILE = TileRef(GRID, 100, 400)                      # 32 x 32 cells, four blocks
PERIOD = Epoch(datetime(2026, 6, 1, tzinfo=UTC), datetime(2026, 7, 1, tzinfo=UTC))
RUN_ID = "trial-deadbeef"
TAGS = {"run_id": RUN_ID, "manifest_hash": "sha256:" + "0" * 64}
COLORS = {"goethite": [174, 118, 163], "hematite": [209, 187, 215], "pyrite": [220, 5, 12]}


# ------------------------------------------------------------------------------------- builders
def class_table() -> ClassTable:
    return ClassTable(key="id", source="enumeration:t@1", entries=pa.table(
        {"id": [0, 1, 2, 3], "name": ["none", "goethite", "hematite", "pyrite"]}))


def schema() -> SnapshotSchema:
    return SnapshotSchema(name="t", layers=[
        LayerSpec(name="m", kind="categorical", source="m", classes=class_table(),
                  aggregate=Aggregation("vote", {"tie_break": "earliest"})),
        LayerSpec(name="d", kind="continuous", source="d", bands=(0, 1),
                  aggregate=Aggregation("median")),
    ])


def block_arrays(block: BlockRef, seed: int) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(seed)
    h, w = block.core_window.height, block.core_window.width
    m = rng.integers(0, 4, (h, w)).astype(np.uint16)
    m[0, 0] = ND                                        # one nodata cell per block
    return {
        "m": m,
        "m_agreement": rng.random((h, w), dtype=np.float32),
        "m_runner_up": rng.integers(0, 4, (h, w)).astype(np.uint16),
        "d": rng.random((h, w, 2), dtype=np.float32),
        "d_n": rng.integers(0, 5, (h, w)).astype(np.uint16),
        "n_epochs": rng.integers(1, 6, (h, w)).astype(np.uint16),
    }


@pytest.fixture
def product_dirs(tmp_path: Path) -> list[tuple[BlockRef, Path]]:
    """Blocks (0,0) and (1,1) written; (1,0) and (0,1) absent."""
    bands = delivered_bands(schema())
    out = []
    for block in (BlockRef(TILE, 0, 0), BlockRef(TILE, 1, 1)):
        d = tmp_path / "blocks" / f"{block.bx}_{block.by}"
        write_product_block(d, block_arrays(block, seed=block.bx * 7 + block.by), bands, block)
        out.append((block, d))
    return out


def stack_from(arrays: dict[str, np.ndarray], specs: list[BandSpec]) -> BandStack:
    return BandStack(arrays, specs)


def cat_specs() -> list[BandSpec]:
    return [BandSpec("m", "uint16", "m", nodata=ND),
            BandSpec("m_agreement", "float32", "agreement", units="fraction", nodata=float("nan"))]


# -------------------------------------------------------------------------------------- stitch
def test_stitch_places_blocks_and_fills_uncovered_with_nodata(product_dirs):
    bands = delivered_bands(schema())
    stack = stitch(product_dirs, TILE, bands)
    m, d, dn, ne = stack["m"], stack["d"], stack["d_n"], stack["n_epochs"]
    assert m.shape == (32, 32) and d.shape == (32, 32, 2)
    b00 = block_arrays(BlockRef(TILE, 0, 0), seed=0)
    b11 = block_arrays(BlockRef(TILE, 1, 1), seed=8)
    np.testing.assert_array_equal(m[:16, :16], b00["m"])
    np.testing.assert_array_equal(m[16:, 16:], b11["m"])
    np.testing.assert_array_equal(d[16:, 16:], b11["d"])
    # uncovered blocks: categorical nodata, NaN for floats, 0 for counts (no nodata)
    assert (m[:16, 16:] == ND).all() and (m[16:, :16] == ND).all()
    assert np.isnan(d[:16, 16:]).all() and np.isnan(stack["m_agreement"][16:, :16]).all()
    assert (dn[:16, 16:] == 0).all() and (ne[16:, :16] == 0).all()
    assert m.dtype == np.uint16 and d.dtype == np.float32 and ne.dtype == np.uint16


def test_stitch_refuses_foreign_and_duplicate_blocks(product_dirs):
    bands = delivered_bands(schema())
    other = TileRef(GRID, 101, 400)
    with pytest.raises(ValueError, match="does not belong"):
        stitch([(BlockRef(other, 0, 0), product_dirs[0][1])], TILE, bands)
    with pytest.raises(ValueError, match="twice"):
        stitch([product_dirs[0], product_dirs[0]], TILE, bands)


# ----------------------------------------------------------------------------------- data COGs
def test_write_data_cogs_round_trip(tmp_path, product_dirs):
    bands = delivered_bands(schema())
    stack = stitch(product_dirs, TILE, bands)
    paths = write_data_cogs(tmp_path / "out", stack, TILE, class_table=class_table(), tags=TAGS,
                            colors=COLORS)
    assert [p.name for p in paths] == [f"{b.name}.tif" for b in bands]
    by_name = {p.stem: p for p in paths}

    with rasterio.open(by_name["m"]) as src:
        assert src.nodata == ND and src.dtypes == ("uint16",)
        assert src.descriptions == (bands[0].description,)
        assert src.tags()["run_id"] == RUN_ID and src.tags()["manifest_hash"] == TAGS["manifest_hash"]
        assert src.tags()["grid_id"] == GRID.id and src.tags(1)["units"] == "unitless"
        assert src.block_shapes == [(32, 32)]
        assert src.transform == TILE.transform and src.crs.to_epsg() == 4326
        cmap = src.colormap(1)
        assert cmap[1][:3] == tuple(COLORS["goethite"]) and cmap[3][:3] == tuple(COLORS["pyrite"])
        assert cmap[0][:3] == (0, 0, 0)                     # `none` black; TIFF tables have no alpha
        np.testing.assert_array_equal(src.read(1), stack["m"])
    with rasterio.open(by_name["m_runner_up"]) as src:
        assert src.colormap(1)[2][:3] == tuple(COLORS["hematite"])
    with rasterio.open(by_name["m_agreement"]) as src:
        assert np.isnan(src.nodata) and src.tags(1)["units"] == "fraction"
        assert src.units == ("fraction",)
        with pytest.raises(ValueError):
            src.colormap(1)
    with rasterio.open(by_name["d"]) as src:                # multi-band, band-last on the way in
        assert src.count == 2 and src.dtypes == ("float32", "float32")
        np.testing.assert_array_equal(np.moveaxis(src.read(), 0, -1), stack["d"])
    with rasterio.open(by_name["d_n"]) as src:
        assert src.nodata is None and src.tags(1)["units"] == "count"
        with pytest.raises(ValueError):
            src.colormap(1)                                 # a count is not categorical
    with rasterio.open(by_name["n_epochs"]) as src:
        assert src.overviews(1) == []


def test_write_data_cogs_requires_run_tags(tmp_path):
    stack = stack_from({"m": np.zeros((32, 32), np.uint16)}, [BandSpec("m", "uint16", "m", nodata=ND)])
    with pytest.raises(ValueError, match="manifest_hash"):
        write_data_cogs(tmp_path, stack, TILE, class_table=None, tags={"run_id": "x"})


def test_internal_tile_size_and_formats():
    assert internal_tile_size((3600, 3600)) == 400
    assert internal_tile_size((512, 512)) == 512
    assert internal_tile_size((720, 720)) == 240
    assert internal_tile_size((33, 33)) == 256
    check_formats(["cog"])
    with pytest.raises(NotImplementedError, match="07"):
        check_formats(["cog", "netcdf"])
    with pytest.raises(ValueError):
        check_formats(["zarr"])


# ------------------------------------------------------------------------- categorical mapper
def test_categorical_fail_lists_present_uncoloured_classes():
    m = np.array([[1, 2], [ND, 0]], dtype=np.uint16)     # goethite, hematite, nodata, none
    stack = stack_from({"m": m}, cat_specs()[:1])
    mapper = CategoricalMapper("m", class_table(), colors={"goethite": [1, 2, 3]})
    with pytest.raises(UnmappedClassError) as e:
        mapper.render(stack)
    assert "hematite" in str(e.value) and "pyrite" not in str(e.value)   # pyrite is absent


def test_categorical_transparent_grey_and_nodata():
    m = np.array([[1, 2], [ND, 0]], dtype=np.uint16)
    stack = stack_from({"m": m}, cat_specs()[:1])
    rgba = CategoricalMapper("m", class_table(), colors={"goethite": [1, 2, 3]},
                             on_unmapped="transparent").render(stack)
    assert rgba.shape == (2, 2, 4) and rgba.dtype == np.uint8
    assert tuple(rgba[0, 0]) == (1, 2, 3, 255)
    assert rgba[0, 1, 3] == 0                                # hematite uncoloured -> transparent
    assert rgba[1, 0, 3] == 0                                # nodata -> transparent
    assert rgba[1, 1, 3] == 0                                # none -> transparent by default
    grey = CategoricalMapper("m", class_table(), colors={"goethite": [1, 2, 3]},
                             on_unmapped="grey").render(stack)
    assert tuple(grey[0, 1]) == (128, 128, 128, 255)
    named_none = CategoricalMapper("m", class_table(), colors={"goethite": [1, 2, 3],
                                                               "none": [9, 9, 9]},
                                   on_unmapped="grey").render(stack)
    assert tuple(named_none[1, 1]) == (9, 9, 9, 255)


def test_categorical_palette_is_deterministic_and_unknown_names_fail():
    m = np.array([[1, 3]], dtype=np.uint16)
    rgba = CategoricalMapper("m", class_table()).render(stack_from({"m": m}, cat_specs()[:1]))
    assert tuple(rgba[0, 0, :3]) == palette_color(1) and tuple(rgba[0, 1, :3]) == palette_color(3)
    assert palette_color(1) != palette_color(2) != palette_color(3)
    with pytest.raises(ValueError, match="not in the class table"):
        CategoricalMapper("m", class_table(), colors={"quartz": [1, 1, 1]})
    with pytest.raises(ValueError, match="on_unmapped"):
        CategoricalMapper("m", class_table(), on_unmapped="ignore")


def test_alpha_from_interpolates_and_zeroes_nan_and_nodata():
    m = np.array([[1, 1, 1, 1], [ND, 1, 1, 1]], dtype=np.uint16)
    agr = np.array([[0.3, 0.8, 0.55, np.nan], [0.9, 0.0, 1.0, 0.8]], dtype=np.float32)
    stack = stack_from({"m": m, "m_agreement": agr}, cat_specs())
    mapper = CategoricalMapper("m", class_table(), colors=COLORS,
                               alpha_from=AlphaFrom("m_agreement", (0.3, 0.8), (60, 255)))
    a = mapper.render(stack)[..., 3]
    assert a[0, 0] == 60 and a[0, 1] == 255 and a[0, 2] == pytest.approx(157.5, abs=1)
    assert a[0, 3] == 0                                      # NaN agreement -> 0
    assert a[1, 0] == 0                                      # nodata class -> 0 whatever agreement
    assert a[1, 1] == 60 and a[1, 2] == 255                  # clamped at the domain ends
    with pytest.raises(ValueError, match="domain"):
        AlphaFrom.from_config({"band": "m_agreement"}, "x")


# -------------------------------------------------------------------------- continuous mapper
def test_continuous_ramp_endpoints_clip_and_nodata():
    d = np.array([[0.0, 0.15, 0.3, np.nan]], dtype=np.float32)
    stack = stack_from({"d": d}, [BandSpec("d", "float32", "d", nodata=float("nan"))])
    clipped = ContinuousMapper("d", ramp="viridis", domain=(0.0, 0.15), clip=True).render(stack)
    assert tuple(clipped[0, 0]) == (*RAMPS["viridis"][0], 255)
    assert tuple(clipped[0, 1]) == (*RAMPS["viridis"][-1], 255)
    assert tuple(clipped[0, 2]) == (*RAMPS["viridis"][-1], 255)   # clip -> ramp end
    assert tuple(clipped[0, 3]) == (0, 0, 0, 0)                   # NaN -> transparent
    strict = ContinuousMapper("d", ramp="grey", domain=(0.0, 0.15)).render(stack)
    assert tuple(strict[0, 2]) == (0, 0, 0, 0)                    # out of domain -> nodata
    assert tuple(strict[0, 0]) == (0, 0, 0, 255) and tuple(strict[0, 1]) == (255, 255, 255, 255)
    with pytest.raises(ValueError, match="ramp"):
        ContinuousMapper("d", ramp="jet", domain=(0, 1))
    with pytest.raises(ValueError, match="domain"):
        ContinuousMapper("d", domain=(1, 1))


def test_continuous_multiband_needs_band_index():
    d = np.zeros((2, 2, 2), dtype=np.float32)
    d[..., 1] = 1.0
    stack = stack_from({"d": d}, [BandSpec("d", "float32", "d", nodata=float("nan"), bands=2)])
    with pytest.raises(ValueError, match="band"):
        ContinuousMapper("d", domain=(0, 1)).render(stack)
    rgba = ContinuousMapper("d", ramp="grey", domain=(0, 1), band=1).render(stack)
    assert tuple(rgba[0, 0]) == (255, 255, 255, 255)


def test_render_from_outputs_dict_and_out_of_scope_mappers(product_dirs):
    bands = delivered_bands(schema())
    stack = stitch(product_dirs, TILE, bands)
    outputs = {"render": {
        "m": {"mapper": "categorical", "on_unmapped": "transparent",
              "alpha_from": {"band": "m_agreement", "domain": [0.3, 0.8], "range": [60, 255]}},
        "depth": {"mapper": "continuous", "layer": "d", "band": 0, "domain": [0, 1]},
    }}
    images = render(outputs, stack, schema())
    assert set(images) == {"m", "depth"}
    assert all(im.shape == (32, 32, 4) and im.dtype == np.uint8 for im in images.values())
    assert (images["m"][:16, 16:, 3] == 0).all()             # uncovered block is transparent
    with pytest.raises(NotImplementedError, match="07 section 3"):
        build_mapper("x", {"mapper": "threshold", "layer": "d"}, schema())
    with pytest.raises(NotImplementedError, match="07 section 3"):
        build_mapper("x", {"mapper": "composite"}, schema())
    with pytest.raises(NotImplementedError, match="07 section 4"):
        build_mapper("x", {"ref": "pkg:Mapper"}, schema())
    with pytest.raises(ValueError, match="unknown key"):
        build_mapper("m", {"mapper": "categorical", "ramp": "viridis"}, schema())
    with pytest.raises(ValueError, match="categorical mapper on continuous"):
        build_mapper("d", {"mapper": "categorical"}, schema())


# ----------------------------------------------------------------------------- legend, classes
def test_legend_and_classes_json_shape(tmp_path):
    mapper = CategoricalMapper("m", class_table(), colors={"goethite": [1, 2, 3]},
                               on_unmapped="fail")
    p = write_legend(tmp_path, "m", mapper.legend())
    doc = json.loads(p.read_text())
    assert p.name == "m_legend.json" and doc["kind"] == "categorical"
    assert doc["entries"][1] == {"id": 1, "name": "goethite", "color": [1, 2, 3]}
    assert doc["entries"][2]["color"] is None                # uncoloured under fail
    assert [e["id"] for e in doc["entries"]] == [0, 1, 2, 3]
    # a class table straight in, coloured by the palette
    doc2 = json.loads(write_legend(tmp_path / "b", "m", class_table()).read_text())
    assert doc2["entries"][3]["color"] == list(palette_color(3))
    ramp = json.loads(write_legend(tmp_path, "d", ContinuousMapper(
        "d", ramp="magma", domain=(0.0, 0.15)).legend()).read_text())
    assert ramp["kind"] == "continuous" and len(ramp["entries"]) == 8
    assert ramp["entries"][0]["value"] == 0.0 and ramp["entries"][-1]["value"] == 0.15
    assert ramp["entries"][-1]["color"] == list(RAMPS["magma"][-1]) and "magma" in ramp["note"]

    c = json.loads(write_classes(tmp_path, class_table()).read_text())
    assert c["key"] == "id" and c["fingerprint"] == class_table().fingerprint()
    assert c["entries"] == [{"id": 0, "name": "none"}, {"id": 1, "name": "goethite"},
                            {"id": 2, "name": "hematite"}, {"id": 3, "name": "pyrite"}]


def test_resolve_class_colors_rejects_values_outside_table():
    with pytest.raises(ValueError, match="not in the class table"):
        resolve_class_colors(class_table(), None, present=[1, 7])


# ------------------------------------------------------------------------ STAC + publish_period
def test_publish_period_writes_everything_and_stac_item_is_well_formed(tmp_path, product_dirs):
    root = tmp_path / "root"
    run_dir = root / "runs" / RUN_ID
    out = root / "products" / RUN_ID / "100_400" / "20260601_20260701"
    outputs = {"formats": ["cog"], "stac": True, "render": {
        "m": {"mapper": "categorical", "colors": COLORS, "on_unmapped": "fail",
              "alpha_from": {"band": "m_agreement", "domain": [0.3, 0.8], "range": [60, 255]}}}}
    pub = publish_period(out, product_dirs, TILE, PERIOD, schema(), outputs, run_id=RUN_ID,
                         manifest_hash=TAGS["manifest_hash"], run_dir=run_dir)
    assert pub.out_dir == out
    assert set(pub.data) == {b.name for b in delivered_bands(schema())}
    assert pub.images["m"].name == "m_rgba.tif" and pub.legends["m"].name == "m_legend.json"
    assert pub.classes.name == "classes.json" and pub.item.name == "item.json"
    for p in (*pub.data.values(), *pub.images.values(), *pub.legends.values(), pub.classes, pub.item):
        assert p.exists()
    with rasterio.open(pub.images["m"]) as src:
        assert src.count == 4 and src.dtypes[0] == "uint8"
        assert [c.name for c in src.colorinterp] == ["red", "green", "blue", "alpha"]
        assert src.transform == TILE.transform
    with rasterio.open(pub.data["m"]) as src:                # COG colour table follows render colors
        assert src.colormap(1)[3][:3] == tuple(COLORS["pyrite"])

    item = json.loads(pub.item.read_text())
    for key in ("type", "stac_version", "stac_extensions", "id", "geometry", "bbox", "properties",
                "assets", "links", "collection"):
        assert key in item
    assert item["type"] == "Feature" and item["id"] == f"{RUN_ID}_100_400_20260601"
    assert item["bbox"] == pytest.approx(list(TILE.bounds))
    assert item["geometry"]["type"] == "Polygon"
    props = item["properties"]
    assert props["datetime"] == "2026-06-01T00:00:00Z"
    assert props["start_datetime"] == "2026-06-01T00:00:00Z"
    assert props["end_datetime"] == "2026-07-01T00:00:00Z"
    assert props["processing:software"] == {"stratum": __version__}
    assert props["proj:epsg"] == 4326 and props["proj:shape"] == [32, 32]
    assert props["proj:transform"] == pytest.approx([float(v) for v in TILE.transform][:9])
    assert props["stratum:run_id"] == RUN_ID
    assert props["stratum:class_table_fingerprint"] == class_table().fingerprint()

    assets = item["assets"]
    assert {"m", "m_agreement", "m_runner_up", "d", "d_n", "n_epochs", "m_rgba", "m_legend",
            "classes"} <= set(assets)
    classes = assets["m"]["classification:classes"]
    assert [(c["value"], c["name"]) for c in classes] == \
        [(0, "none"), (1, "goethite"), (2, "hematite"), (3, "pyrite")]
    assert classes[3]["color_hint"] == "dc050c" and "color_hint" not in classes[0]
    assert assets["m"]["raster:bands"] == [{"data_type": "uint16", "unit": "unitless", "nodata": ND}]
    assert assets["m_agreement"]["raster:bands"][0]["nodata"] == "nan"
    assert len(assets["d"]["raster:bands"]) == 2
    assert assets["d_n"]["raster:bands"] == [{"data_type": "uint16", "unit": "count"}]
    assert "classification:classes" in assets["m_runner_up"]   # runner-up is categorical too
    assert assets["m_rgba"]["roles"] == ["visual"] and "legend" in assets["m_legend"]["roles"]
    for a in assets.values():                                # every href resolves
        assert (out / a["href"]).exists(), a["href"]

    rels = {ln["rel"]: ln["href"] for ln in item["links"]}
    assert (out / rels["stratum:provenance"]).resolve() == (run_dir / "provenance.json").resolve()
    assert (out / rels["stratum:frozen-index"]).resolve() == (run_dir / "index.parquet").resolve()
    assert (out / rels["collection"]).resolve() == (root / "products" / RUN_ID / "collection.json").resolve()

    coll_path = write_stac_collection(root / "products" / RUN_ID, [pub.item])
    coll = json.loads(coll_path.read_text())
    assert coll["type"] == "Collection" and coll["id"] == RUN_ID
    assert coll["extent"]["spatial"]["bbox"] == [pytest.approx(list(TILE.bounds))]
    assert coll["extent"]["temporal"]["interval"] == [["2026-06-01T00:00:00Z", "2026-07-01T00:00:00Z"]]
    item_links = [ln for ln in coll["links"] if ln["rel"] == "item"]
    assert len(item_links) == 1 and (coll_path.parent / item_links[0]["href"]).exists()


def test_publish_period_refuses_netcdf_and_unmapped_classes(tmp_path, product_dirs):
    with pytest.raises(NotImplementedError, match="07 section 2"):
        publish_period(tmp_path / "a", product_dirs, TILE, PERIOD, schema(),
                       {"formats": ["cog", "netcdf"]}, run_id=RUN_ID, manifest_hash="sha256:x")
    outputs = {"render": {"m": {"mapper": "categorical", "colors": {"goethite": [1, 2, 3]}}}}
    with pytest.raises(UnmappedClassError, match="hematite"):
        publish_period(tmp_path / "b", product_dirs, TILE, PERIOD, schema(), outputs,
                       run_id=RUN_ID, manifest_hash="sha256:x")


def test_publish_period_without_render_or_stac(tmp_path, product_dirs):
    pub = publish_period(tmp_path / "c", product_dirs, TILE, PERIOD, schema(), {"stac": False},
                         run_id=RUN_ID, manifest_hash="sha256:x")
    assert pub.item is None and pub.images == {} and pub.legends == {}
    assert pub.classes is not None and len(pub.data) == 6


# ---------------------------------------------------------------------------------- provenance
def test_provenance_build_write_read(tmp_path):
    rec = build_provenance(
        run_id=RUN_ID, manifest_hash="sha256:abc", manifest={"run_id": "trial"},
        inputs={"frozen_index": "runs/x/index.parquet", "frozen_index_hash": "sha256:1",
                "granule_count": 3, "build_versions": {"010635": 3},
                "class_tables": {"C": ["sha256:2"]}, "collections": {"C": "001"}},
        code={"stratum_commit": "abc1234", "regrid_algo_version": 1},
        plugins={"scorer": {"ref": "min_view_zenith", "version": "0.0.1", "params": {}}},
        schema={"name": "t", "layers_hash": "sha256:3", "aggregate_hash": "sha256:4", "extends": []},
        filters=[{"describe": "month_in [6]", "removed": 2, "on_missing": "fail"}],
        execution={"tiles": 1, "epochs": 1, "blocks": 4, "cache_hits": {"glt": 0, "snapshot": 0},
                   "failed_items": []},
        started=datetime(2026, 9, 2, 14, 22, 11, tzinfo=UTC),
        finished=datetime(2026, 9, 2, 15, 47, 3, tzinfo=UTC), derived_from="trial-cafebabe")
    assert rec["schema_version"] == "1.0" and rec["code"]["stratum_version"] == __version__
    assert rec["started"] == "2026-09-02T14:22:11Z" and rec["finished"] == "2026-09-02T15:47:03Z"
    assert rec["derived_from"] == "trial-cafebabe" and rec["aux"] == []
    assert list(rec)[:5] == ["run_id", "manifest_hash", "manifest", "schema_version", "started"]
    p = write_provenance(tmp_path / "runs" / RUN_ID, rec)
    assert p.name == "provenance.json" and read_provenance(p) == rec
    assert read_provenance(p.parent) == rec
    with pytest.raises(ValueError, match="inputs"):
        write_provenance(tmp_path, {k: v for k, v in rec.items() if k != "inputs"})
