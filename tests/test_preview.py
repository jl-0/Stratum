"""The preview viewer: what it reads, what it refuses, and what it draws.

The viewer is not part of a run, so nothing here checks science. What it does check is the three
claims the two modes rest on:

1. a products tree is read through its STAC and through nothing else, so a run that publishes a
   new band needs no viewer change;
2. a path from a URL cannot leave the products root;
3. the server renders the same colours the deployed page renders - the two colour ramps are one
   table written twice, and this is what keeps them equal.
"""
from __future__ import annotations

import json
import re
import threading
from http.client import HTTPConnection
from pathlib import Path

import numpy as np
import pytest
from fake_s3 import install

from stratum.preview import render
from stratum.preview.catalog import CatalogError, Products
from stratum.preview.server import STATIC, serve

CELL = 0.01
SHAPE = (64, 64)


# ---------------------------------------------------------------------------- a products tree
def write_products(root: Path, run: str = "run-a", tiles: tuple[tuple[int, int], ...] =
                   ((0, 0), (1, 0))) -> Path:
    """A minimal published run: two tiles, one categorical band, one continuous, one legend.

    Deliberately written by hand rather than by `stratum.publish`, so that a change in publish
    that breaks the viewer shows up as a failure here and not as a passing test of two things
    that changed together.
    """
    import rasterio
    from rasterio.transform import from_origin

    products = root / "products" / run
    items = []
    for tx, ty in tiles:
        west, north = tx * SHAPE[1] * CELL, (ty + 1) * SHAPE[0] * CELL
        cell_dir = products / f"{tx}_{ty}" / "20260101_20260201"
        cell_dir.mkdir(parents=True, exist_ok=True)
        transform = from_origin(west, north, CELL, CELL)
        profile = {"driver": "GTiff", "width": SHAPE[1], "height": SHAPE[0], "count": 1,
                   "crs": "EPSG:4326", "transform": transform}

        mineral = np.zeros(SHAPE, "uint16")
        mineral[8:24, 8:24] = 1
        mineral[32:48, 32:48] = 2
        mineral[0:4, 0:4] = 65535                       # nodata: not observed
        with rasterio.open(cell_dir / "mineral_1.tif", "w", dtype="uint16",
                           nodata=65535, **profile) as dst:
            dst.write(mineral, 1)

        depth = np.linspace(0.0, 1.0, SHAPE[0] * SHAPE[1]).reshape(SHAPE).astype("float32")
        with rasterio.open(cell_dir / "depth_1.tif", "w", dtype="float32",
                           nodata=float("nan"), **profile) as dst:
            dst.write(depth, 1)

        (cell_dir / "mineral_1_legend.json").write_text(json.dumps({
            "kind": "categorical",
            "entries": [{"id": 0, "name": "none", "color": [0, 0, 0]},
                        {"id": 1, "name": "Goethite", "color": [200, 120, 40]},
                        {"id": 2, "name": "Kaolinite", "color": [40, 120, 200]}]}))

        rel = f"{tx}_{ty}/20260101_20260201/item.json"
        (cell_dir / "item.json").write_text(json.dumps({
            "type": "Feature", "stac_version": "1.0.0", "id": f"{run}-{tx}-{ty}",
            "bbox": [west, north - SHAPE[0] * CELL, west + SHAPE[1] * CELL, north],
            "properties": {"datetime": "2026-01-01T00:00:00Z", "stratum:tile": [tx, ty]},
            "stac_extensions": [
                "https://stac-extensions.github.io/classification/v1.1.0/schema.json"],
            "assets": {
                # `classification:classes` is what marks a band categorical (07 section 6). The
                # viewer reads it from the asset rather than inferring it from the asset key,
                # which is why the continuous band below deliberately does not carry one.
                "mineral_1": {"href": "./mineral_1.tif", "roles": ["data"],
                              "title": "mineral_1", "description": "vote over epochs",
                              "classification:classes": [
                                  {"value": 0, "name": "none"},
                                  {"value": 1, "name": "Goethite", "color_hint": "c87828"},
                                  {"value": 2, "name": "Kaolinite", "color_hint": "2878c8"}]},
                "depth_1": {"href": "./depth_1.tif", "roles": ["data"], "title": "depth_1"},
                "mineral_1_legend": {"href": "./mineral_1_legend.json",
                                     "roles": ["metadata", "legend"]}},
            "links": []}))
        items.append(rel)

    (products / "collection.json").write_text(json.dumps({
        "type": "Collection", "stac_version": "1.0.0", "id": run, "description": run,
        "extent": {"spatial": {"bbox": [[0, 0, 1, 1]]},
                   "temporal": {"interval": [["2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z"]]}},
        "links": [{"rel": "self", "href": "./collection.json"}]
                 + [{"rel": "item", "href": f"./{r}"} for r in items]}))
    return products


@pytest.fixture
def products(tmp_path: Path) -> Products:
    write_products(tmp_path)
    return Products.open(tmp_path)


# --------------------------------------------------------------------------------- the catalogue
def test_open_accepts_the_storage_root_or_the_products_directory(tmp_path: Path) -> None:
    """Both are things someone types, and they must name the same tree."""
    write_products(tmp_path)
    assert Products.open(tmp_path).run_ids() == ["run-a"]
    assert Products.open(tmp_path / "products").run_ids() == ["run-a"]
    assert Products.open(tmp_path).path == Products.open(tmp_path / "products").path


def test_items_carry_hrefs_relative_to_the_products_root(products: Products) -> None:
    """The one rewrite the viewer depends on: a STAC href is relative to its own item, and every
    caller here wants it relative to the root instead."""
    items = products.items("run-a")
    assert len(items) == 2
    for item in items:
        for asset in item["assets"].values():
            assert asset["href"].startswith("run-a/")
            assert products.resolve(asset["href"]).is_file()


def test_a_run_with_an_unreadable_item_is_a_gap_not_a_failure(tmp_path: Path) -> None:
    """A run still publishing, or one that lost an object, must still draw what it has."""
    products_dir = write_products(tmp_path)
    (products_dir / "1_0" / "20260101_20260201" / "item.json").write_text("{ truncated")
    assert len(Products.open(tmp_path).items("run-a")) == 1


@pytest.mark.parametrize("bad", ["../secret.json", "run-a/../../secret.json",
                                 "run-a/0_0/../../../../etc/passwd"])
def test_a_path_that_climbs_out_of_the_products_root_is_refused(products: Products,
                                                                bad: str) -> None:
    with pytest.raises(CatalogError):
        products.resolve(bad)


@pytest.mark.parametrize("absolute", ["/etc/passwd", "//etc/passwd"])
def test_an_absolute_path_is_read_as_one_inside_the_tree(products: Products,
                                                         absolute: str) -> None:
    """Not refused - confined. It names a file that is not there, which `materialise` reports as
    a miss; what matters is that it never reaches the real /etc."""
    assert products.path in products.resolve(absolute).parents
    with pytest.raises(CatalogError):
        products.materialise(absolute)


def test_a_bucket_root_lists_runs_without_walking_them(tmp_path: Path, monkeypatch) -> None:
    """`run_ids` must be a delimited list. Listing every object to learn a handful of names is
    the difference between a page that loads over a real products bucket and one that does not -
    a published run is thousands of objects.

    It also pins what a viewer fetches: the STAC, and not one byte of raster until a tile is
    asked for. A mirror is filled on touch (06 section 4).
    """
    monkeypatch.setenv("STRATUM_SCRATCH", str(tmp_path / "scratch"))
    fake = install(monkeypatch, tmp_path / "s3")
    staged = write_products(tmp_path / "staged").parent.parent
    for path in sorted(staged.rglob("*")):
        if path.is_file():
            fake.upload_file(str(path), "bkt", path.relative_to(staged).as_posix())
    fake.puts.clear()

    remote = Products.open("s3://bkt/", client=fake)
    assert remote.workspace.remote
    assert remote.run_ids() == ["run-a"]

    assert len(remote.items("run-a")) == 2
    assert fake.gets, "the STAC has to come from somewhere"
    assert all(k.endswith(".json") for k in fake.gets), "a raster was fetched to draw no tile"

    # And the mirror now holds them, so a second read is free.
    fake.gets.clear()
    remote.items("run-a")
    assert fake.gets == []


# ------------------------------------------------------------------------------- the rendering
def tile_covering(asset: Path) -> tuple[int, int, int]:
    """The deepest tile that still contains the whole raster - where a read neither decimates nor
    lands half off the edge, so a per-pixel assertion means what it says."""
    import rasterio

    with rasterio.open(asset) as src:
        west, south, east, north = src.bounds

    def xy(lon: float, lat: float, n: int) -> tuple[int, int]:
        rad = np.radians(lat)
        return (int((lon + 180) / 360 * n),
                int((1 - np.log(np.tan(rad) + 1 / np.cos(rad)) / np.pi) / 2 * n))

    for z in range(20, 0, -1):
        n = 1 << z
        lo, hi = xy(west, north - 1e-9, n), xy(east - 1e-9, south + 1e-9, n)
        if lo == hi:
            return z, lo[0], lo[1]
    raise AssertionError("no single tile contains this raster")


def read_png(data: bytes) -> np.ndarray:
    """Decode with GDAL, so the encoder is checked against something that did not write it."""
    import rasterio

    with rasterio.MemoryFile(data, ext=".png") as memfile, memfile.open() as src:
        return np.moveaxis(src.read(), 0, -1)


def test_encode_png_round_trips(tmp_path: Path) -> None:
    rng = np.random.default_rng(0)
    rgba = rng.integers(0, 256, (render.TILE, render.TILE, 4), dtype=np.uint8)
    assert np.array_equal(read_png(render.encode_png(rgba)), rgba)


def test_a_categorical_tile_draws_only_legend_colours(products: Products) -> None:
    """And class 0 - observed, nothing identified - is a hole, not a colour. Painting it would
    make "nothing was found here" indistinguishable from a mineral (11 section 2)."""
    asset = products.resolve("run-a/0_0/20260101_20260201/mineral_1.tif")
    legend = json.loads(products.resolve("run-a/0_0/20260101_20260201/mineral_1_legend.json")
                        .read_text())
    style = render.style_for("mineral_1", ["data"], legend)
    tile = read_png(render.render_tile(asset, *tile_covering(asset), style))

    drawn = {tuple(int(c) for c in px) for px in tile[tile[..., 3] > 0][:, :3]}
    assert drawn <= {(200, 120, 40), (40, 120, 200)}, "a colour the legend never named"
    assert drawn, "the tile covers the raster and must draw something"


def test_a_tile_that_misses_the_raster_is_transparent(products: Products) -> None:
    asset = products.resolve("run-a/0_0/20260101_20260201/mineral_1.tif")
    style = render.Style(kind="categorical", colors=((1, (200, 120, 40)),))
    assert render.render_tile(asset, 6, 0, 0, style) == render.blank_tile()


def test_a_continuous_tile_honours_its_stretch(products: Products) -> None:
    """Both ends of the ramp appear, at a zoom that does not decimate.

    Zoomed out they would not, and that is right: a continuous band is averaged on the way down,
    which is what makes a whole tile readable and is also what removes the extremes.
    """
    asset = products.resolve("run-a/0_0/20260101_20260201/depth_1.tif")
    z, x, y = tile_covering(asset)
    tile = read_png(render.render_tile(
        asset, z, x, y, render.Style(kind="continuous", vmin=0.0, vmax=1.0, ramp="viridis")))
    lut = render.ramp_lut("viridis")
    visible = tile[tile[..., 3] > 0][:, :3]
    assert len(visible)
    assert any(np.array_equal(px, lut[0]) for px in visible), "the bottom of the ramp"
    assert any(np.array_equal(px, lut[255]) for px in visible), "the top of the ramp"


def test_measure_clips_to_percentiles(products: Products) -> None:
    lo, hi = render.measure(products.resolve("run-a/0_0/20260101_20260201/depth_1.tif"))
    assert 0.0 < lo < 0.05 and 0.95 < hi < 1.0, "a 2nd-98th percentile stretch, not min-max"


def test_the_page_carries_no_colour_table_of_its_own() -> None:
    """The ramps are served from `render.RAMPS` and interpolated in the browser only for the
    legend's gradient. A literal palette in `viewer.js` would be a second copy of a colour table,
    and a second copy is one that drifts out of step with the pixels."""
    source = (STATIC / "viewer.js").read_text()
    # A palette is a *sequence* of triples. A lone triple is not one - `[0, 1, 2]` over the
    # channels of a pixel is ordinary code - so the pattern looks for two in a row.
    triple = r"\[\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*\d{1,3}\s*\]"
    palettes = re.findall(rf"{triple}\s*,\s*{triple}", source)
    assert not palettes, (f"viewer.js declares a palette: {palettes[:2]} - "
                          "serve it from /api/config instead")


def test_sample_reads_one_point_from_every_band(products: Products) -> None:
    """A click asks about the place, not the layer - so a point resolves in each band, and the
    two kinds of absence stay apart: outside the raster is None, and so is a masked cell."""
    mineral = products.resolve("run-a/0_0/20260101_20260201/mineral_1.tif")
    depth = products.resolve("run-a/0_0/20260101_20260201/depth_1.tif")

    # The synthetic band is class 1 over rows 8-24, columns 8-24, at CELL degrees a pixel.
    inside = (0.5 * CELL + 12 * CELL, SHAPE[0] * CELL - (0.5 * CELL + 12 * CELL))
    assert render.sample(mineral, *inside) == [1.0]
    assert render.sample(depth, *inside) is not None

    # Rows 0-4, columns 0-4 are the nodata block: observed nothing, not "no such place".
    masked = (0.5 * CELL + 2 * CELL, SHAPE[0] * CELL - (0.5 * CELL + 2 * CELL))
    assert render.sample(mineral, *masked) is None

    # And a point outside the raster entirely.
    assert render.sample(mineral, 170.0, -80.0) is None


def test_the_value_endpoint_reads_several_assets_at_once(server: int,
                                                         products: Products) -> None:
    """One request per click, not one per band - a click on a run with ten delivered bands must
    not be ten round trips."""
    base = "run-a/0_0/20260101_20260201"
    lon = 0.5 * CELL + 12 * CELL
    lat = SHAPE[0] * CELL - (0.5 * CELL + 12 * CELL)
    reading = json.loads(get(server, f"/api/value?lon={lon}&lat={lat}"
                                     f"&asset={base}/mineral_1.tif&asset={base}/depth_1.tif")[1])
    assert reading["values"][f"{base}/mineral_1.tif"] == [1.0]
    assert reading["values"][f"{base}/depth_1.tif"] is not None


def test_a_bad_asset_in_a_reading_does_not_lose_the_others(server: int) -> None:
    """One missing band reads as no data. Failing the request would lose the reading of every
    other band over a path that may simply not be published in this run."""
    base = "run-a/0_0/20260101_20260201"
    reading = json.loads(get(server, f"/api/value?lon={0.1}&lat={0.1}"
                                     f"&asset={base}/mineral_1.tif&asset={base}/nope.tif")[1])
    assert reading["values"][f"{base}/nope.tif"] is None
    assert f"{base}/mineral_1.tif" in reading["values"]


def test_the_page_names_classes_from_the_asset_not_from_the_key() -> None:
    """`mineral_1_runner_up` is categorical and `mineral_1_agreement` is a fraction, and both
    start with `mineral_1`. Inferring a class table from the asset key would put a mineral name
    on an agreement value, so the viewer reads `classification:classes` off the asset itself."""
    source = (STATIC / "viewer.js").read_text()
    assert "classification:classes" in source
    assert "_legend`]" not in source.split("function describe")[1], (
        "describe() must not resolve a class name through a legend filename")


# ---------------------------------------------------------------------------------- the server
@pytest.fixture
def server(products: Products):
    httpd = serve(products, port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def get(port: int, path: str) -> tuple[int, bytes, str]:
    conn = HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        return response.status, response.read(), response.getheader("Content-Type") or ""
    finally:
        conn.close()


def test_the_server_serves_the_viewer_from_the_root(server: int) -> None:
    """Relative to each other, not under a prefix - the deployed copy is three files side by
    side in one bucket prefix, and the page must not need two sets of links."""
    for path, media in (("/", "text/html"), ("/viewer.js", "text/javascript"),
                        ("/viewer.css", "text/css")):
        status, body, content_type = get(server, path)
        assert status == 200 and media in content_type, path
        assert body


def test_the_api_reports_the_runs_and_the_ramps(server: int) -> None:
    assert json.loads(get(server, "/api/runs")[1]) == {"runs": ["run-a"]}
    config = json.loads(get(server, "/api/config")[1])
    assert config["mode"] == "server"
    assert next(iter(config["ramps"])) == "viridis", "the first ramp is the page's default"
    assert config["ramps"]["viridis"] == [list(c) for c in render.RAMPS["viridis"]], \
        "the page draws its legend from these, so they are the tiles' own colours"


def test_the_server_renders_a_tile(server: int) -> None:
    base = "run-a/0_0/20260101_20260201"
    status, body, content_type = get(
        server, f"/tiles/{base}/mineral_1.tif/6/32/31.png"
                f"?kind=categorical&legend={base}/mineral_1_legend.json")
    assert status == 200 and content_type == "image/png"
    assert read_png(body).shape == (render.TILE, render.TILE, 4)


def test_the_range_endpoint_reports_the_stretch_the_tiles_used(server: int,
                                                              products: Products) -> None:
    """Otherwise the page's legend prints a range the image was never scaled to."""
    asset = "run-a/0_0/20260101_20260201/depth_1.tif"
    span = json.loads(get(server, f"/api/range?asset={asset}")[1])
    vmin, vmax = render.measure(products.resolve(asset))
    assert (span["vmin"], span["vmax"]) == pytest.approx((vmin, vmax))


@pytest.mark.parametrize("path", ["/stac/%2e%2e%2fsecret.json", "/stac/nope/collection.json",
                                  "/stac/run-a/0_0/20260101_20260201/mineral_1.tif"])
def test_the_server_refuses_what_is_not_json_inside_the_tree(server: int, path: str) -> None:
    assert get(server, path)[0] in (404, 415)


# ------------------------------------------------------- "is it blank, or still drawing?"
# The viewer has no JS test runner, so these guard the two things about the loading indicator
# that are easy to break silently and were both wrong once. `STATIC` is the server's own notion
# of where the page lives, so a move breaks these rather than letting them pass vacuously.


def test_the_viewer_reports_whether_it_is_still_drawing():
    """A blank patch of map is either not-fetched-yet or nothing-published-there, and the two are
    pixel-identical. The tile endpoint answers a request outside the raster with a TRANSPARENT
    PNG rather than a 404 (see `test_a_tile_that_misses_the_raster_is_transparent`), which is
    what makes "drawn" a trustworthy statement: once nothing is outstanding, a gap is real."""
    html = (STATIC / "index.html").read_text()
    js = (STATIC / "viewer.js").read_text()

    assert 'id="tilestatus"' in html
    assert 'aria-live="polite"' in html, "announced, not only seen"
    for event in ("tileloadstart", "tileload", "tileerror"):
        assert event in js, f"{event} is how the outstanding count is kept"
    assert "no data, not a pending load" in js, "the sentence the indicator exists to say"
    assert "map tile" in js, "'tile' alone means a one-degree product tile in this codebase"


def test_the_drawing_indicator_does_not_depend_on_animation_frames():
    """It first coalesced repaints with `requestAnimationFrame`, which is PAUSED in a background
    tab - so the pill froze mid-load and was still stale on return, which is exactly when someone
    checks whether a blank area finished drawing. Observed reading "preparing the layer…" with
    two map tiles already drawn."""
    js = (STATIC / "viewer.js").read_text()
    tracker = js[js.index("const drawing ="):js.index("function paintDrawing")]
    # The CALL, not the word: the comment above the fix names rAF to explain why it is not used.
    assert "requestAnimationFrame(" not in tracker, \
        "coalesce with a timer; rAF does not run in a background tab"
    assert "setTimeout(" in tracker
