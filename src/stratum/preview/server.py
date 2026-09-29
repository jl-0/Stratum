"""The local preview server: `stratum preview`.

A standard-library HTTP server in front of `catalog` and `render`. It exists so that a products
tree can be looked at *before* it is published anywhere - including one written as `gtiff`, which
a browser cannot range-read because it has no overviews (07 section 2). Deployed beside the
products, the same page runs without any of this.

It serves four things:

| route | what |
|---|---|
| `/` and `/<file>`         | the viewer, which is the same files a deployed one gets |
| `/api/config`             | where the products are, and the colour ramps the page draws legends with |
| `/api/runs`               | the run directories under the products root |
| `/api/range`              | one continuous layer's stretch, pooled over the run's tiles |
| `/api/value`              | every named asset sampled at one point - what a click asks |
| `/stac/<path>`            | any JSON in the products tree - collections, items, legends |
| `/tiles/<path>/z/x/y.png` | one asset, one web-mercator tile - a tile's raster, or a layer mosaic |

**It binds to the loopback interface and is not hardened.** It reads a directory the person who
started it can already read, and `Products.resolve` refuses a path that climbs out of the tree,
but it has no authentication and must not be put on a public address.
"""
from __future__ import annotations

import json
import re
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import rasterio

from stratum.preview import render
from stratum.preview.catalog import CatalogError, Products

STATIC = Path(__file__).parent / "static"

#: `/tiles/<asset path within the products tree>/<z>/<x>/<y>.png`
TILE_ROUTE = re.compile(r"^/tiles/(?P<asset>.+)/(?P<z>\d+)/(?P<x>\d+)/(?P<y>\d+)\.png$")

#: How many product tiles a layer's stretch is measured over, and at what decimated size each.
#: 64 x 128^2 is a million cells: plenty for two percentiles, and small enough to hold at once.
MEASURE_TILES = 64
MEASURE_SIZE = 128
#: The same budget when the layer has a mosaic: one read, at about the same million cells.
MEASURE_MOSAIC = 1024

MEDIA = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".json": "application/json",
         ".svg": "image/svg+xml", ".png": "image/png"}


class BadRequest(ValueError):
    """The request is well-formed HTTP but asks for something the page must say and did not."""


class _LRU(OrderedDict):
    """Rendered tiles, newest last. Bounded so a long pan cannot grow without limit."""

    def __init__(self, cap: int) -> None:
        super().__init__()
        self.cap = cap
        self.lock = threading.Lock()

    def take(self, key: Any) -> Any:
        with self.lock:
            if key not in self:
                return None
            self.move_to_end(key)
            return self[key]

    def give(self, key: Any, value: Any) -> Any:
        with self.lock:
            self[key] = value
            while len(self) > self.cap:
                self.popitem(last=False)
        return value


class PreviewHandler(BaseHTTPRequestHandler):
    """One request. `products`, `tiles` and `ranges` are bound by `serve`."""

    products: Products
    tiles: _LRU
    ranges: _LRU
    server_version = "stratum-preview"

    def log_message(self, fmt: str, *args: Any) -> None:       # quiet: one line per tile is noise
        pass

    # -- routing ---------------------------------------------------------------------------
    def do_GET(self) -> None:
        url = urlparse(self.path)
        route, query = unquote(url.path), parse_qs(url.query)
        try:
            if route in ("/", "/index.html"):
                return self._static("index.html")
            # The viewer's own files sit beside it, because that is how a deployed copy is laid
            # out - three files in one prefix, referring to each other by bare name. The server
            # serves them from the root for the same reason: one page, one set of links.
            if route.count("/") == 1 and (STATIC / route[1:]).is_file():
                return self._static(route[1:])
            if route == "/api/config":
                # The ramps travel as their control points, not as names: the page draws the
                # legend's gradient from them and so needs the colours, and a second copy of a
                # colour table in JavaScript is a copy that drifts.
                return self._json({"mode": "server", "products": self.products.uri,
                                   "ramps": {k: [list(c) for c in v]
                                             for k, v in render.RAMPS.items()}})
            if route == "/api/runs":
                return self._json({"runs": self.products.run_ids()})
            if route == "/api/range":
                # One stretch for a whole layer. The page sends it back on every map-tile
                # request and prints it on the legend, so the legend and every tile agree.
                span = self._layer_range(query["run"][0], query["layer"][0])
                return self._json({"vmin": span[0], "vmax": span[1]})
            if route.startswith("/stac/"):
                return self._stac(route[len("/stac/"):])
            if route == "/api/value":
                # Every asset the page asks about, in one request: a click is a question about the
                # place, not about the layer that happens to be drawn.
                return self._json({
                    "lon": float(query["lon"][0]), "lat": float(query["lat"][0]),
                    "values": {rel: self._sample(rel, float(query["lon"][0]),
                                                 float(query["lat"][0]))
                               for rel in query.get("asset", ())}})
            match = TILE_ROUTE.match(route)
            if match:
                return self._tile(match, query)
            self._error(404, "no such route")
        except CatalogError as e:
            self._error(404, str(e))
        except BadRequest as e:
            self._error(400, str(e))
        except (BrokenPipeError, ConnectionResetError):          # the map moved on; not an error
            pass
        except Exception as e:                                  # noqa: BLE001 - a tile must not
            self._error(500, f"{type(e).__name__}: {e}")        # take the server down with it

    # -- handlers --------------------------------------------------------------------------
    def _static(self, name: str) -> None:
        path = (STATIC / name).resolve()
        if STATIC.resolve() not in path.parents or not path.is_file():
            return self._error(404, "no such file")
        self._send(path.read_bytes(), MEDIA.get(path.suffix, "application/octet-stream"))

    def _stac(self, rel: str) -> None:
        if not rel.endswith(".json"):
            return self._error(415, "only JSON is served from the products tree")
        self._send(self.products.materialise(rel).read_bytes(), "application/json")

    def _tile(self, match: re.Match[str], query: dict[str, list[str]]) -> None:
        asset = match["asset"]
        z, x, y = (int(match[k]) for k in "zxy")
        key = (asset, z, x, y, self.path.partition("?")[2])
        cached = self.tiles.take(key)
        if cached is None:
            style = self._style(asset, query)
            if self.products.is_mosaic(asset):
                # A mosaic is read in place, by range: a map tile is one of its blocks, and
                # mirroring the whole file first would be gigabytes for the first tile.
                with rasterio.Env(**self.products.gdal_config()):
                    png = render.render_tile(self.products.gdal_path(asset), z, x, y, style)
            else:
                png = render.render_tile(self.products.materialise(asset), z, x, y, style)
            cached = self.tiles.give(key, png)
        self._send(cached, "image/png", cache="public, max-age=60")

    def _sample(self, rel: str, lon: float, lat: float) -> list[float] | None:
        """One raster at one point. A path that is not in the tree reads as no data rather than
        as an error, so one missing asset cannot lose the whole reading."""
        try:
            return render.sample(self.products.materialise(rel), lon, lat)
        except (CatalogError, OSError, ValueError):
            return None

    # -- the style -------------------------------------------------------------------------
    def _layer_range(self, run: str, layer: str) -> tuple[float, float]:
        """The stretch for one layer of one run, pooled over its product tiles.

        At most `MEASURE_TILES` of them, evenly spaced through the run: each is pulled into the
        mirror whole, and a layer like `geometry_stack` is ~28 MB a tile, so measuring all 402
        tiles of US West would be an 11 GB wait before the first pixel. Cached per layer.
        """
        key = (run, layer)
        cached = self.ranges.take(key)
        if cached is not None:
            return cached
        # A mosaic already holds the whole layer, overviews and all, so one decimated read of it
        # is the entire run at once - no sampling of tiles and nothing mirrored.
        mosaics = [m["href"] for m in self.products.mosaics(run) if m.get("stratum:asset") == layer]
        if mosaics:
            with rasterio.Env(**self.products.gdal_config()):
                return self.ranges.give(key, render.measure(
                    *(self.products.gdal_path(h) for h in mosaics), size=MEASURE_MOSAIC))
        hrefs = [item["assets"][layer]["href"] for item in self.products.items(run)
                 if layer in item.get("assets", {})]
        if not hrefs:
            raise CatalogError(f"no tile in {run!r} has a {layer!r} asset")
        sample = hrefs[::-(-len(hrefs) // MEASURE_TILES)]
        with ThreadPoolExecutor(max_workers=16) as pool:
            paths = list(pool.map(self.products.materialise, sample))
        return self.ranges.give(key, render.measure(*paths, size=MEASURE_SIZE))

    def _style(self, asset: str, query: dict[str, list[str]]) -> render.Style:
        """What the page asked for.

        A continuous tile MUST carry `vmin` and `vmax`. The server used to measure each asset
        itself when they were missing, which is one stretch per product tile, and that drew tile
        edges into a layer that has none. There is no per-tile fallback now: the range comes
        from `/api/range` for the whole layer.
        """
        one = {k: v[0] for k, v in query.items()}
        kind = one.get("kind", "continuous")
        if kind == "rgba":
            return render.Style(kind="rgba")
        if kind == "categorical":
            legend = json.loads(self.products.materialise(one["legend"]).read_text())
            return render.style_for("", ["data"], legend)
        if "vmin" not in one or "vmax" not in one:
            raise BadRequest("a continuous tile needs vmin and vmax - one stretch per layer, "
                             "from /api/range?run=&layer=")
        span = (float(one["vmin"]), float(one["vmax"]))
        return render.Style(kind="continuous", vmin=span[0], vmax=span[1],
                            ramp=one.get("ramp", "viridis"))

    # -- replies ---------------------------------------------------------------------------
    def _json(self, payload: Any) -> None:
        self._send(json.dumps(payload).encode(), "application/json")

    def _send(self, body: bytes, media: str, *, status: int = 200, cache: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", media)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._send(json.dumps({"error": message}).encode(), "application/json", status=status)


def serve(products: Products, *, host: str = "127.0.0.1", port: int = 8787,
          tile_cache: int = 2048) -> ThreadingHTTPServer:
    """Build a server over one products tree. The caller runs it, so a test can start and stop
    one, and `port=0` asks the OS for a free port.

    The handler keeps its state on the class, so each server gets its own subclass rather than
    sharing one - two previews in a process would otherwise serve each other's tiles.
    """
    bound = type("BoundPreviewHandler", (PreviewHandler,), {
        "products": products, "tiles": _LRU(tile_cache), "ranges": _LRU(64)})
    return ThreadingHTTPServer((host, port), bound)


__all__ = ["STATIC", "BadRequest", "PreviewHandler", "serve"]
