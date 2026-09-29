"""Layer mosaics: one web-mercator COG per layer, built from a published run (`stratum mosaic`).

What is checked is what a map server relies on: the file is a COG on the GoogleMapsCompatible
grid, a categorical layer holds only ids its tiles held (decimated, never averaged), a continuous
one stays inside its tiles' range, the run's collection records every mosaic with the tiles'
class counts summed, and the preview draws a layer from its mosaic when there is one.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import numpy as np
import pytest
from fake_s3 import install
from test_preview import get, read_png, write_products

from stratum.preview import render
from stratum.preview.catalog import MOSAIC_ROLE, Products
from stratum.preview.server import serve
from stratum.publish.mosaic import (
    BLOCKSIZE,
    MosaicJob,
    _summed_classes,
    plan_mosaics,
    run_mosaics,
)

RUN = "run-a"
PERIOD = "20260101_20260201"


@pytest.fixture
def products(tmp_path: Path) -> Products:
    write_products(tmp_path)
    return Products.open(tmp_path)


def _job(results: list[tuple[MosaicJob, str]], asset: str) -> MosaicJob:
    return next(j for j, _ in results if j.asset == asset)


# ------------------------------------------------------------------------------------ the plan
def test_the_plan_is_one_job_per_layer_over_every_tile(products: Products) -> None:
    jobs = {j.asset: j for j in plan_mosaics(RUN, products.items(RUN))}
    assert set(jobs) == {"mineral_1", "depth_1"}, "legends are not rasters"
    assert all(len(j.sources) == 2 and j.period == PERIOD for j in jobs.values())
    assert jobs["mineral_1"].nearest, "class ids must decimate"
    assert not jobs["depth_1"].nearest, "a continuous band averages"
    assert jobs["mineral_1"].rel == f"{RUN}/mosaic/{PERIOD}/mineral_1.tif"


def test_a_layer_the_run_does_not_have_is_an_error(products: Products) -> None:
    with pytest.raises(ValueError, match="no layer 'mineral_9'"):
        plan_mosaics(RUN, products.items(RUN), layers=["mineral_9"])
    with pytest.raises(ValueError, match="no period"):
        plan_mosaics(RUN, products.items(RUN), periods=["19990101_19991231"])


def test_class_counts_sum_over_tiles_and_uncounted_stays_uncounted() -> None:
    merged = _summed_classes([
        [{"value": 0, "name": "none", "count": 3}, {"value": 1, "name": "a", "count": 2}],
        [{"value": 1, "name": "a", "count": 5}, {"value": 2, "name": "b"}],
    ])
    assert merged == ({"value": 0, "name": "none", "count": 3},
                      {"value": 1, "name": "a", "count": 7},
                      {"value": 2, "name": "b"})


# ----------------------------------------------------------------------------------- the build
def test_a_mosaic_is_a_web_mercator_cog(products: Products) -> None:
    import rasterio

    results = run_mosaics(products, RUN)
    assert sorted(s for _, s in results) == ["built", "built"]
    path = products.resolve(_job(results, "mineral_1").rel)
    with rasterio.open(path) as src:
        assert src.crs.to_epsg() == 3857
        assert src.tags(ns="IMAGE_STRUCTURE")["LAYOUT"] == "COG"
        assert src.tags(ns="TILING_SCHEME")["NAME"] == "GoogleMapsCompatible"
        assert src.block_shapes[0] == (BLOCKSIZE, BLOCKSIZE)
        # Cut on the tile grid: the origin is a tile corner at the file's zoom.
        tile = BLOCKSIZE * src.transform.a
        for offset in (src.transform.c + render.MERCATOR_R, render.MERCATOR_R - src.transform.f):
            assert offset / tile == pytest.approx(round(offset / tile), abs=1e-6)


def test_a_categorical_mosaic_holds_only_ids_its_tiles_held(products: Products) -> None:
    import rasterio

    results = run_mosaics(products, RUN)
    with rasterio.open(products.resolve(_job(results, "mineral_1").rel)) as src:
        assert src.nodata == 65535
        levels = [src.read(1)] + [src.read(1, out_shape=(src.height // f, src.width // f))
                                  for f in src.overviews(1)]
    for level in levels:
        assert set(np.unique(level)) <= {0, 1, 2, 65535}, "an averaged class id was invented"
    assert {1, 2} <= set(np.unique(levels[0]))


def test_a_continuous_mosaic_stays_inside_its_tiles_range(products: Products) -> None:
    import rasterio

    results = run_mosaics(products, RUN)
    with rasterio.open(products.resolve(_job(results, "depth_1").rel)) as src:
        data = src.read(1, masked=True).compressed()
    data = data[np.isfinite(data)]
    assert data.size and data.min() >= 0.0 and data.max() <= 1.0


def test_the_collection_records_every_mosaic(products: Products) -> None:
    run_mosaics(products, RUN)
    doc = products.collection(RUN)
    asset = doc["assets"][f"mosaic:{PERIOD}:mineral_1"]
    assert asset["href"] == f"./mosaic/{PERIOD}/mineral_1.tif"
    assert asset["roles"] == ["data", MOSAIC_ROLE]
    assert asset["proj:epsg"] == 3857 and asset["stratum:tiles"] == 2
    assert [c["name"] for c in asset["classification:classes"]] == ["none", "Goethite", "Kaolinite"]
    assert "https://stac-extensions.github.io/projection/v1.1.0/schema.json" \
        in doc["stac_extensions"]
    assert [m["stratum:asset"] for m in products.mosaics(RUN)] == ["mineral_1", "depth_1"]
    assert products.mosaics(RUN)[0]["href"] == f"{RUN}/mosaic/{PERIOD}/mineral_1.tif"


def test_a_mosaic_already_built_is_kept_unless_forced(products: Products) -> None:
    run_mosaics(products, RUN)
    assert {s for _, s in run_mosaics(products, RUN)} == {"present"}
    assert {s for _, s in run_mosaics(products, RUN, layers=["depth_1"], force=True)} == {"built"}
    assert len(products.mosaics(RUN)) == 2, "rebuilding one layer dropped the other's asset"


def test_a_bucket_root_reads_sources_in_place(tmp_path: Path, monkeypatch) -> None:
    """Over a bucket the sources go to GDAL as `/vsis3/` paths rather than being mirrored - a
    layer of US West is gigabytes of tiles, and the build needs only to stream them."""
    monkeypatch.setenv("STRATUM_SCRATCH", str(tmp_path / "scratch"))
    fake = install(monkeypatch, tmp_path / "s3")
    remote = Products.open("s3://bkt/root/", client=fake)
    rel = f"{RUN}/0_0/{PERIOD}/mineral_1.tif"
    assert remote.gdal_path(rel) == f"/vsis3/bkt/root/products/{rel}"
    assert remote.gdal_config()["GDAL_DISABLE_READDIR_ON_OPEN"] == "EMPTY_DIR"
    assert not (remote.path / rel).exists(), "naming a source must not fetch it"


# --------------------------------------------------------------------------------- the preview
@pytest.fixture
def mosaicked(products: Products):
    run_mosaics(products, RUN)
    httpd = serve(products, port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield products, httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def test_the_preview_draws_a_categorical_mosaic_in_legend_colours(mosaicked) -> None:
    _, port = mosaicked
    rel = f"{RUN}/mosaic/{PERIOD}/mineral_1.tif"
    legend = f"{RUN}/0_0/{PERIOD}/mineral_1_legend.json"
    status, body, media = get(port, f"/tiles/{rel}/7/64/63.png?kind=categorical&legend={legend}")
    assert status == 200 and media == "image/png"
    rgba = read_png(body)
    drawn = {tuple(p) for p in rgba[rgba[..., 3] == 255][:, :3]}
    assert drawn and drawn <= {(200, 120, 40), (40, 120, 200)}


def test_the_range_of_a_mosaicked_layer_comes_from_its_mosaic(mosaicked, monkeypatch) -> None:
    from stratum.preview import server

    products, port = mosaicked
    seen: list[str] = []
    real = render.measure
    monkeypatch.setattr(server.render, "measure",
                        lambda *paths, **kw: seen.extend(map(str, paths)) or real(*paths, **kw))
    span = json.loads(get(port, f"/api/range?run={RUN}&layer=depth_1")[1])
    assert seen == [products.gdal_path(f"{RUN}/mosaic/{PERIOD}/depth_1.tif")]
    assert 0.0 <= span["vmin"] < span["vmax"] <= 1.0


def test_the_page_draws_one_overlay_for_a_mosaicked_layer() -> None:
    from stratum.preview.server import STATIC

    js = (STATIC / "viewer.js").read_text()
    assert "includes('overview')" in js, "the page must find mosaics by their STAC role"
    assert "state.mosaics.filter" in js and "stratum:asset" in js


def test_the_command_builds_from_a_bucket_only_inside_ecs(tmp_path: Path, monkeypatch) -> None:
    """A laptop pointed at the bucket is sent to `make cloud-mosaic`: building there would pull
    every tile of the run out of the region and push the mosaics back."""
    from click.testing import CliRunner

    from stratum.cli import main
    from stratum.publish.mosaic import ECS_METADATA_ENV

    monkeypatch.setenv("STRATUM_SCRATCH", str(tmp_path / "scratch"))
    monkeypatch.delenv(ECS_METADATA_ENV, raising=False)
    fake = install(monkeypatch, tmp_path / "s3")
    staged = write_products(tmp_path / "staged").parent.parent
    for path in sorted(staged.rglob("*")):
        if path.is_file():
            fake.upload_file(str(path), "bkt", path.relative_to(staged).as_posix())
    fake.gets.clear()

    result = CliRunner().invoke(main, ["mosaic", "--root", "s3://bkt/", "--run", RUN])
    assert result.exit_code != 0
    assert f"make cloud-mosaic RUN={RUN}" in result.output
    assert not any(k.endswith(".tif") for k in fake.gets), "no tile may be fetched"
    assert not any("/mosaic/" in k for k in fake.puts)


def test_the_command_builds_a_local_root_where_it_is(tmp_path: Path) -> None:
    from click.testing import CliRunner

    from stratum.cli import main

    write_products(tmp_path)
    result = CliRunner().invoke(main, ["mosaic", "--root", str(tmp_path), "--run", RUN])
    assert result.exit_code == 0, result.output
    assert "2 built" in result.output
