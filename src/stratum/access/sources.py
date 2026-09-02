"""GranuleSource implementations (12 section 5). Discovery runs at index build, never in a run.

`LocalSource` is the first slice. It walks a directory, reads each file's HEADER once - global
attributes only, never a pixel array - and yields one `GranuleRecord` per (collection,
granule_id), which is the same shape `CMRSource` will produce from UMM-G. Nothing downstream can
tell which source built the index; that is the test that the seam is in the right place.

Header attribute names are ACDD / NCEI swath-template names (`time_coverage_start`,
`*most_longitude`, `software_build_version`, ...), which is a NetCDF convention and not
instrument knowledge; the core still knows nothing about what the variables mean.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import netCDF4 as nc
import numpy as np
from shapely.geometry import box

from stratum.access.store import to_uri
from stratum.types import GranuleRecord

log = logging.getLogger(__name__)

_WILDCARDS = "*?["

# ACDD header attribute -> the canonical name the index promotes it under (02 section 2).
PROMOTED = {
    "software_build_version": "build_version",
    "product_version": "product_version",
    "day_night_flag": "day_night",
}


class CMRSource:
    """A bridge over earthaccess: maps UMM-G onto the index schema, resolves assets to names,
    applies the null policy, holds an explicit session. Verified field mapping in 12 section 5.
    Out of the first slice."""

    name = "cmr"

    def search(self, **kwargs: Any) -> Iterator[GranuleRecord]:
        raise NotImplementedError("CMRSource is a later slice (12 section 5)")

    def assets(self, record: GranuleRecord) -> Mapping[str, str]:
        raise NotImplementedError("CMRSource is a later slice (12 section 5)")


def parse_time(value: str) -> datetime:
    """ACDD time strings, tz-aware UTC. Accepts `+0000`, `Z`, `+00:00` and naive (assumed UTC)."""
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _scalar(value: Any) -> Any | None:
    """A global attribute as a plain Python scalar, or None when it is not one (arrays such as
    `geotransform` are not index attributes)."""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, np.generic):          # before float: np.float64 IS a float subclass
        return value.item()
    if isinstance(value, (str, bool, int, float)):
        return value
    return None


def read_header(path: Path) -> dict[str, Any]:
    """Every scalar string/number global attribute, verbatim. Opens the file for metadata only;
    no variable is touched."""
    attrs: dict[str, Any] = {}
    with nc.Dataset(path, mode="r") as ds:
        for name in ds.ncattrs():
            value = _scalar(ds.getncattr(name))
            if value is not None:
                attrs[name] = value
    return attrs


def split_pattern(pattern: str) -> tuple[str, str]:
    """(prefix, suffix) of a glob's basename around its wildcards. The granule id is what the
    wildcards matched: `EMIT_L2B_MIN_*.nc` -> (`EMIT_L2B_MIN_`, `.nc`)."""
    base = pattern.replace("\\", "/").rsplit("/", 1)[-1]
    first = min((base.find(c) for c in _WILDCARDS if c in base), default=-1)
    if first < 0:
        return base, ""
    last = max(base.rfind(c) for c in _WILDCARDS if c in base)
    return base[:first], base[last + 1:]


def granule_id_from(name: str, pattern: str) -> str:
    prefix, suffix = split_pattern(pattern)
    gid = name[len(prefix):]
    if suffix and gid.endswith(suffix):
        gid = gid[:-len(suffix)]
    return gid


def version_token(granule_id: str) -> str | None:
    """The leading all-digit token of an id (`001` in `001_20260825T151308_2623710_050`)."""
    head = granule_id.split("_", 1)[0]
    return head if head.isdigit() else None


def _normalise_patterns(
    patterns: Mapping[str, Mapping[str, Any]],
) -> dict[str, tuple[str | None, dict[str, str]]]:
    """collection -> (pinned collection_version | None, {asset: glob}). Two shapes accepted:
    the primary `{collection: {asset: glob}}` (first-slice plan section 4) and the long form
    `{collection: {version: "001", assets: {asset: glob}}}` when the version must be pinned
    explicitly. An asset literally named `assets` therefore needs the long form."""
    out: dict[str, tuple[str | None, dict[str, str]]] = {}
    for collection, spec in patterns.items():
        if "assets" in spec and isinstance(spec["assets"], Mapping):
            version = spec.get("version")
            assets = {str(k): str(v) for k, v in spec["assets"].items()}
        else:
            version = None
            assets = {str(k): str(v) for k, v in spec.items()}
        if not assets:
            raise ValueError(f"LocalSource: collection {collection!r} declares no asset globs")
        out[collection] = (None if version is None else str(version), assets)
    return out


class LocalSource:
    """Walks a directory and reads each file's header once. What makes a laptop run possible.

    `patterns` is `{collection: {asset: glob}}`; the glob is relative to `root`. A granule's id
    is the filename minus the glob's literal prefix and suffix, so `EMIT_L2B_MIN_*.nc` over
    `EMIT_L2B_MIN_001_20260825T151308_2623710_050.nc` yields `001_20260825T151308_2623710_050`,
    and the MIN and MINUNCERT files of one granule land in one record as two assets.

    Per record: `datetime`/`end_datetime` from `time_coverage_start/end`; geometry is the box of
    the `*most_longitude/latitude` attributes; `attributes` carries every scalar global attribute
    verbatim plus the promoted names (`granule_id`, `build_version`, `product_version`,
    `day_night`, `collection_version`) and `cloud_fraction: None` - absent locally, which is meaningful
    (02 section 4). `collection_version` is the pinned value from `patterns` when given, else the
    id's leading digit token, else the header's `product_version` without its `V`. The header is
    read from the first asset in sorted name order (`MIN` before `MINUNCERT`).
    """

    name = "local"

    def __init__(self, root: Path, patterns: Mapping[str, Mapping[str, Any]]) -> None:
        self.root = Path(root)
        self._patterns = _normalise_patterns(patterns)

    @property
    def collections(self) -> tuple[str, ...]:
        return tuple(self._patterns)

    def _discover(self, collection: str) -> dict[str, dict[str, Path]]:
        """granule_id -> {asset: path} for one collection."""
        _, assets = self._patterns[collection]
        found: dict[str, dict[str, Path]] = {}
        for asset, pattern in assets.items():
            for path in sorted(self.root.glob(pattern)):
                if not path.is_file():
                    continue
                gid = granule_id_from(path.name, pattern)
                found.setdefault(gid, {})[asset] = path
        return found

    def _record(self, collection: str, gid: str, files: dict[str, Path]) -> GranuleRecord:
        pinned, _ = self._patterns[collection]
        primary = files[min(files)]
        header = read_header(primary)
        missing = [k for k in ("time_coverage_start", "time_coverage_end", "westernmost_longitude",
                               "easternmost_longitude", "southernmost_latitude",
                               "northernmost_latitude") if k not in header]
        if missing:
            raise ValueError(f"{primary}: header lacks {missing}; cannot index it (12 section 5)")
        start = parse_time(header["time_coverage_start"])
        end = parse_time(header["time_coverage_end"])
        bbox = (float(header["westernmost_longitude"]), float(header["southernmost_latitude"]),
                float(header["easternmost_longitude"]), float(header["northernmost_latitude"]))
        attributes: dict[str, Any] = dict(header)
        attributes["granule_id"] = gid
        for raw, promoted in PROMOTED.items():
            attributes[promoted] = header.get(raw)
        attributes["build_version"] = str(attributes.get("build_version") or "")
        attributes["product_version"] = str(attributes.get("product_version") or "")
        version = pinned or version_token(gid) or attributes["product_version"].lstrip("Vv")
        attributes["collection_version"] = version
        attributes["cloud_fraction"] = None
        return GranuleRecord(native_id=str(primary), collection=collection, datetime=start,
                             end_datetime=end, geometry=box(*bbox), attributes=attributes,
                             raw={asset: path for asset, path in sorted(files.items())})

    def search(self, *, collections: Sequence[str] | None = None,
               bbox: tuple[float, float, float, float] | None = None,
               start: datetime | None = None, end: datetime | None = None,
               updated_since: datetime | None = None) -> Iterator[GranuleRecord]:
        """One record per (collection, granule_id). `bbox` filters by footprint intersection,
        `[start, end)` by acquisition start, `updated_since` by file modification time - the
        nearest local analogue of a catalogue's revision date."""
        wanted = list(collections) if collections is not None else list(self._patterns)
        unknown = [c for c in wanted if c not in self._patterns]
        if unknown:
            raise LookupError(f"LocalSource has no patterns for {unknown}; configured: "
                              f"{sorted(self._patterns)}")
        region = box(*bbox) if bbox is not None else None
        for collection in wanted:
            for gid, files in sorted(self._discover(collection).items()):
                if updated_since is not None:
                    mtime = max(datetime.fromtimestamp(p.stat().st_mtime, tz=UTC)
                                for p in files.values())
                    if mtime < updated_since:
                        continue
                record = self._record(collection, gid, files)
                if start is not None and record.datetime < _utc(start):
                    continue
                if end is not None and record.datetime >= _utc(end):
                    continue
                if region is not None and not record.geometry.intersects(region):
                    continue
                yield record

    def assets(self, record: GranuleRecord) -> Mapping[str, str]:
        """asset name -> file:// URI."""
        return {asset: to_uri(path) for asset, path in record.raw.items()}

    def checksums(self, record: GranuleRecord) -> Mapping[str, str]:
        """No catalogue checksums exist for local files; empty, honestly (02 section 2)."""
        return {}


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
