"""Ancillary data: external rasters warped onto the grid (05).

Everything a plugin reaches through `AuxAccessor` comes from here, and so does the ortho-native
role path (12 section 2) - the two are the same operation seen from different ends, which is why
one module owns both:

    warp_to_tile     a source raster (any CRS, any resolution) -> the tile's exact grid
    warp_aux_tile    that, cached per (alias, source, tile)   -> 05 section 4
    warp_ortho_tile  that, cached per (granule, role, tile)   -> 12 section 2
    BlockAux         the accessor a Scorer or a map-space PixelMask actually sees

The contract, and the only thing a plugin author needs to know (05 section 1): what comes back is
already on the block's grid and CRS, already windowed to the block, already cached. A scorer never
does geometry, never sees a CRS, never resamples anything.

Nothing here knows what a mineral is, what EMIT is, or what a granule means - it warps rasters
onto grids. `stratum_emit` supplies the readers; this supplies the geometry.
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.warp import reproject, transform_bounds
from shapely.geometry.base import BaseGeometry

from stratum.cache import CacheKey, CacheRoot, aux_inputs
from stratum.types import (
    AuxAccessor,
    AuxTable,
    BlockRef,
    Epoch,
    GranuleFrame,
    TileRef,
    Window,
)

#: Bumped by hand when the warp's OUTPUT changes (06 section 3, rule 2). Without it a bug fix
#: silently serves stale rasters forever, because the key would not move. `ALGO_HASH` beside this
#: module records it against the module's content hash and `test_ancillary` fails when the module
#: changes without a bump - the same guard, and the same sign-off, as `stratum.regrid`.
WARP_ALGO_VERSION = 1

#: Where that version is recorded against the module's content hash.
ALGO_HASH_PATH = Path(__file__).with_name("ALGO_HASH")

#: What a manifest's `resampling` means to rasterio. Declared, never defaulted (05 section 2):
#: bilinear-interpolating a landcover class and nearest-neighbouring a DEM are both wrong and
#: neither raises, so the manifest must say which.
RESAMPLING: Mapping[str, Resampling] = {
    "nearest": Resampling.nearest,
    "bilinear": Resampling.bilinear,
    "cubic": Resampling.cubic,
    "mode": Resampling.mode,
    "average": Resampling.average,
}

AUX_NOT_BUILT = "is not built (05 section 3); only raster() is in this slice"


class AuxError(RuntimeError):
    """An aux source could not be read, or an alias was not declared."""


# ------------------------------------------------------------------------------- 05 section 2: a source
@dataclass(frozen=True)
class AuxSource:
    """One declared aux source, as the planner resolved it (05 section 5).

    `paths` are LOCAL files - the planner stages every source before a worker runs, so "a run
    never depends on third-party uptime" (05 section 6, question 1) is a property of the code and
    not a hope. `digest` is the content hash that enters the cache key; see `aux_inputs` for why
    it is a digest and not the ETag 06 section 2 names.
    """

    alias: str
    uri: str
    paths: tuple[Path, ...]
    digest: str
    kind: str
    resampling: str
    etag: str | None = None

    @property
    def nodata(self) -> float:
        """NaN for a continuous source; a categorical one uses 0, the reserved "nothing" id."""
        return float("nan") if self.kind == "continuous" else 0.0

    @property
    def dtype(self) -> str:
        return "float32" if self.kind == "continuous" else "uint8"

    def to_doc(self) -> dict[str, Any]:
        return {"uri": self.uri, "paths": [str(p) for p in self.paths], "digest": self.digest,
                "kind": self.kind, "resampling": self.resampling, "etag": self.etag}

    @classmethod
    def from_doc(cls, alias: str, doc: Mapping[str, Any]) -> AuxSource:
        return cls(alias=alias, uri=str(doc["uri"]),
                   paths=tuple(Path(p) for p in doc.get("paths", ())),
                   digest=str(doc["digest"]), kind=str(doc["kind"]),
                   resampling=str(doc["resampling"]), etag=doc.get("etag"))


def file_digest(path: Path, *, chunk: int = 1 << 20) -> str:
    """`sha256:<hex>` of a file's bytes - the source identity of `aux_inputs`."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(chunk):
            h.update(block)
    return f"sha256:{h.hexdigest()}"


def sources_digest(paths: Sequence[Path]) -> str:
    """One identity for a source that is several files: the digest of the digests, in the order
    declared, so reordering a mosaic is a different artifact (it is - later files win)."""
    digests = [file_digest(p) for p in paths]
    if len(digests) == 1:
        return digests[0]
    h = hashlib.sha256("\n".join(digests).encode()).hexdigest()
    return f"sha256:{h}"


# --------------------------------------------------------------------- 05 section 1: the warp itself
def warp_to_tile(paths: Sequence[Path | str], tile: TileRef, *, resampling: str,
                 dtype: str, nodata: float, band: int = 1) -> np.ndarray:
    """One (rows, cols) array on `tile`'s exact grid and CRS, whatever the sources were on.

    Sources are composited in the order given, later over earlier, each contributing only where
    it has data - so a mosaic of adjacent tiles joins without a seam and an overlap resolves
    predictably rather than by whichever file rasterio opened last. A source that does not
    intersect the tile is skipped without being read, which is what makes declaring a continental
    DEM as four files cheap for a one-degree run.

    `resampling` is required and is not guessed: 05 section 2's rule exists because interpolating
    a class label produces a number that is not a class, and nothing downstream can detect it.
    """
    if resampling not in RESAMPLING:
        raise AuxError(f"resampling {resampling!r} is not one of {sorted(RESAMPLING)} "
                       "(05 section 2)")
    rows, cols = tile.shape
    out = np.full((rows, cols), nodata, dtype=dtype)
    if not paths:
        raise AuxError("warp_to_tile was given no sources")

    for path in paths:
        with rasterio.open(str(path)) as src:
            if _disjoint(src, tile):
                continue
            buf = np.full((rows, cols), nodata, dtype=dtype)
            reproject(
                source=rasterio.band(src, band),
                destination=buf,
                src_transform=src.transform,
                src_crs=src.crs,
                src_nodata=src.nodata,
                dst_transform=tile.transform,
                dst_crs=tile.grid.crs,
                dst_nodata=nodata,
                resampling=RESAMPLING[resampling],
            )
            here = ~_is_nodata(buf, nodata)
            out[here] = buf[here]
    return out


def _is_nodata(arr: np.ndarray, nodata: float) -> np.ndarray:
    return np.isnan(arr) if np.isnan(nodata) else arr == nodata


def _disjoint(src: rasterio.DatasetReader, tile: TileRef) -> bool:
    """True when the source cannot touch the tile. Compared in the TILE's CRS, and a source whose
    bounds will not transform (a rotated or exotic projection) is conservatively kept."""
    w, s, e, n = tile.bounds
    try:
        sw, ss, se, sn = transform_bounds(src.crs, tile.grid.crs, *src.bounds)
    except Exception:       # noqa: BLE001 - an untransformable source is warped, not skipped
        return False
    return sw > e or se < w or ss > n or sn < s


def write_raster(path: Path, data: np.ndarray, tile: TileRef, *, nodata: float,
                 tags: Mapping[str, Any]) -> Path:
    """A one-band tiled GeoTIFF on the tile's grid, laid out so a block read is a range request.

    Internally tiled on the block size for the same reason `write_glt` is (12 section 2, step 6):
    a block must fetch only the bytes covering its own window. Two runs with different
    `block_size` therefore write byte-different files under the same key - identical values, a
    different layout - exactly as GLTs already do. That is benign and must NOT be "fixed" by
    putting `block_size` in the key, which would cause spurious misses for a knob that never
    changes output (01 section 3).
    """
    from stratum.regrid import internal_tile_size

    rows, cols = tile.shape
    if data.shape != (rows, cols):
        raise AuxError(f"raster shape {data.shape} does not match tile shape {(rows, cols)}")
    bs = internal_tile_size(tile.grid.block_size)
    with rasterio.open(
        path, "w", driver="GTiff", height=rows, width=cols, count=1, dtype=data.dtype.name,
        nodata=nodata, crs=tile.grid.crs, transform=tile.transform, tiled=True,
        blockxsize=bs, blockysize=bs, compress="deflate",
    ) as dst:
        dst.write(data, 1)
        dst.update_tags(grid_id=tile.grid.id, **{k: str(v) for k, v in tags.items()})
    return Path(path)


def read_window(path: Path, window: Window, *, nodata: float) -> np.ndarray:
    """`window` of a warped tile, as (height, width).

    A block's window includes its halo (01 section 3), so at a tile edge it reaches outside the
    file with negative offsets. That is a boundless read filled with nodata, exactly as
    `read_glt` handles the same case - an in-bounds window takes the plain path.
    """
    from rasterio.windows import Window as RioWindow

    with rasterio.open(str(path)) as src:
        rio = RioWindow(window.col_off, window.row_off, window.width, window.height)
        inside = (window.row_off >= 0 and window.col_off >= 0
                  and window.row_off + window.height <= src.height
                  and window.col_off + window.width <= src.width)
        if inside:
            return src.read(1, window=rio)
        return src.read(1, window=rio, boundless=True, fill_value=nodata)


# ------------------------------------------------------------------ 05 section 4: warp once, slice many
def aux_key_for(cache: CacheRoot, tile: TileRef, source: AuxSource) -> CacheKey:
    """The key `warp_aux_tile` writes under. Pure: no IO, no network, no cache probe.

    That purity is load-bearing rather than tidy. `snapshot_key` is recomputed by `product_key`
    once per epoch per block through the whole reduce stage and again at publish, so a stat or an
    open in here turns reduce into an IO storm of O(blocks x epochs).
    """
    inputs = aux_inputs(source.alias, source.digest, tile.grid, source.resampling,
                        WARP_ALGO_VERSION)
    return cache.key("aux", tile.grid.id, tile, inputs)


def warp_aux_tile(cache: CacheRoot, tile: TileRef, source: AuxSource) -> CacheKey:
    """One aux source over one tile, cached (05 section 4).

    A DEM is static; warping it per block, per epoch, per granule would repeat identical work
    thousands of times. Warped once per (source, tile, grid, resampling), with blocks reading
    windows out of the result - and, like a GLT, shared across runs and experiments.
    """
    key = aux_key_for(cache, tile, source)
    if cache.hit(key):
        return key
    data = warp_to_tile(source.paths, tile, resampling=source.resampling,
                        dtype=source.dtype, nodata=source.nodata)
    cache.write_file(key, lambda p: write_raster(
        p, data, tile, nodata=source.nodata,
        tags={"alias": source.alias, "source_uri": source.uri, "source_digest": source.digest,
              "resampling": source.resampling, "warp_algo_version": WARP_ALGO_VERSION}))
    return key


# ------------------------------------------------------- 12 section 2: ortho-native roles
@dataclass(frozen=True)
class OrthoSource:
    """A product already on a map grid, ready to be warped onto ours (12 section 2).

    What an ortho `GranuleReader` hands back instead of a sensor array: where the pixels are and
    which band, not the pixels themselves. The framework does the geometry, so the reader never
    learns what a tile is - the same split `AuxAccessor` makes from the other side.
    """

    uri: str                 # a path or a /vsi string: whatever rasterio can open
    band: int                # 1-based, as GDAL counts
    nodata: float | None
    dtype: str


def ortho_key_for(cache: CacheRoot, tile: TileRef, granule_id: str, role: str, var: str,
                  checksum: str | None, resampling: str) -> CacheKey:
    """The key `warp_ortho_tile` writes under. Pure, for the reason `aux_key_for` is."""
    from stratum.cache import ortho_inputs

    inputs = ortho_inputs(granule_id, role, checksum, var, tile.grid, resampling,
                          WARP_ALGO_VERSION)
    return cache.key("ortho", tile.grid.id, tile, inputs)


def warp_ortho_tile(cache: CacheRoot, tile: TileRef, source: OrthoSource, *, granule_id: str,
                    role: str, var: str, checksum: str | None, resampling: str) -> CacheKey:
    """One ortho role of one granule over one tile, cached (12 section 2, 05 section 4).

    Cached for exactly the reason a GLT is. Warping inside the block read path would repeat the
    work once per block, per epoch, per granule - hundreds of times over a real run - and turn
    "the cheapest possible input" into the most expensive thing in it. Warped once per
    (granule, role, tile) and windowed per block, like everything else on the grid.
    """
    key = ortho_key_for(cache, tile, granule_id, role, var, checksum, resampling)
    if cache.hit(key):
        return key
    fill = float("nan") if np.dtype(source.dtype).kind == "f" else 0.0
    data = warp_to_tile([source.uri], tile, resampling=resampling, dtype=source.dtype,
                        nodata=fill, band=source.band)
    cache.write_file(key, lambda p: write_raster(
        p, data, tile, nodata=fill,
        tags={"granule_id": granule_id, "role": role, "var": var, "resampling": resampling,
              "warp_algo_version": WARP_ALGO_VERSION}))
    return key


# --------------------------------------------------------------------- 05 section 3: the accessor


class BlockAux(AuxAccessor):
    """The accessor a `Scorer` or a map-space `PixelMask` is handed, bound to one block.

    Constructed with the keys the snapshot key was built from, not with the sources - so it is
    structurally incapable of serving an alias the key does not name. That is stronger than
    checking afterwards: an undeclared read cannot produce a key that lies (05 section 5,
    06 section 3 rule 5) because there is nothing to read it from.

    Only `raster` is built. The rest raise, naming 05, exactly as `NullAux` does.
    """

    def __init__(self, keys: Mapping[str, CacheKey], sources: Mapping[str, AuxSource],
                 block: BlockRef) -> None:
        self._keys = dict(keys)
        self._sources = dict(sources)
        self._block = block
        self._cached: dict[str, np.ndarray] = {}

    def raster(self, alias: str, *, date: datetime | None = None,
               epoch: Epoch | None = None) -> np.ndarray:
        if date is not None or epoch is not None:
            raise NotImplementedError(
                f"raster({alias!r}, date=/epoch=): date-keyed aux is not built (05 section 2). "
                "Resolving 'the nearest date that exists' needs a listing of what exists, and a "
                "run never queries a catalogue (02 section 6); declare a static source.")
        if alias not in self._keys:
            declared = sorted(self._keys) or ["nothing"]
            raise AuxError(
                f"aux alias {alias!r} is not declared for this run; declared: {declared}. A "
                "plugin must list what it reads in `required_aux` and the manifest must declare "
                "it in `aux` - an undeclared read makes a cache key that lies (05 section 5).")
        if alias not in self._cached:
            source = self._sources[alias]
            self._cached[alias] = read_window(self._keys[alias].path, self._block.window,
                                              nodata=source.nodata)
        return self._cached[alias]

    # -- 05 section 3, not built in this slice ------------------------------------------------
    def vector(self, alias: str) -> np.ndarray:
        raise NotImplementedError(f"vector({alias!r}) {AUX_NOT_BUILT}")

    def features(self, alias: str, *, margin: float = 0.0) -> Sequence[BaseGeometry]:
        raise NotImplementedError(f"features({alias!r}) {AUX_NOT_BUILT}")

    def distance(self, alias: str, *, cutoff: float | None = None) -> np.ndarray:
        raise NotImplementedError(f"distance({alias!r}) {AUX_NOT_BUILT}")

    def table(self, alias: str) -> AuxTable:
        raise NotImplementedError(f"table({alias!r}) {AUX_NOT_BUILT}")

    @property
    def granule_index(self) -> GranuleFrame:
        raise NotImplementedError(f"granule_index {AUX_NOT_BUILT}")


# ------------------------------------------------------------------------- the recorded hash
def module_content_hash() -> str:
    """sha256 over every `.py` in this package, in name order (06 section 3, rule 2)."""
    h = hashlib.sha256()
    for f in sorted(Path(__file__).parent.glob("*.py")):
        h.update(f.name.encode() + b"\0" + f.read_bytes() + b"\0")
    return "sha256:" + h.hexdigest()


def recorded_hash() -> tuple[int, str] | None:
    """`(version, hash)` from `ALGO_HASH`, or None when the file is missing."""
    if not ALGO_HASH_PATH.is_file():
        return None
    version, digest = ALGO_HASH_PATH.read_text().split()
    return int(version), digest


def record_hash() -> tuple[int, str]:
    """Rewrite `ALGO_HASH` for the current module and version."""
    entry = (WARP_ALGO_VERSION, module_content_hash())
    ALGO_HASH_PATH.write_text(f"{entry[0]} {entry[1]}\n")
    return entry


def check_recorded_hash() -> tuple[bool, str]:
    """(ok, message). ok only when `ALGO_HASH` matches both the current version and the current
    content hash; the message says which step of the workflow was skipped. A cosmetic edit needs
    only a re-record, and that re-record is the human sign-off that output did not change."""
    current = (WARP_ALGO_VERSION, module_content_hash())
    recorded = recorded_hash()
    how = "run `pixi run python -m stratum.ancillary --record`"
    if recorded is None:
        return False, f"ALGO_HASH is missing; {how}"
    if recorded == current:
        return True, f"ALGO_HASH matches version {current[0]}"
    if recorded[0] == current[0]:
        return False, (f"stratum.ancillary changed but WARP_ALGO_VERSION is still {current[0]}: "
                       f"bump it if warped output can differ, then {how} (06 section 3, rule 2)")
    return False, (f"WARP_ALGO_VERSION is {current[0]} but ALGO_HASH records {recorded[0]}; "
                   f"{how}")


__all__ = [
    "ALGO_HASH_PATH", "AUX_NOT_BUILT", "RESAMPLING", "WARP_ALGO_VERSION", "AuxError", "AuxSource",
    "BlockAux", "OrthoSource", "aux_key_for", "check_recorded_hash", "file_digest",
    "module_content_hash", "ortho_key_for", "read_window", "record_hash", "recorded_hash",
    "sources_digest", "warp_aux_tile", "warp_ortho_tile", "warp_to_tile", "write_raster",
]
