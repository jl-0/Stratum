"""GranuleSource implementations (12 section 5). Discovery runs at index build, never in a run.

`LocalSource` walks a directory and reads each file's HEADER once - global attributes only,
never a pixel array. `CMRSource` is the bridge over earthaccess: it maps UMM-G onto the same
`GranuleRecord`. Both yield one record per (collection, granule_id) and both derive the id the
same way - what the `patterns` glob's wildcards matched on the collection's FIRST asset - so an
index built either way is interchangeable. Nothing downstream can tell which source built the
index; that is the test that the seam is in the right place.

`patterns` is `{collection: {asset: glob}}` for every source (12 section 5). Locally the glob
runs on disk; against CMR it is an fnmatch over each record's file names. No filename convention
lives here: every prefix a granule id is stripped of arrives through `patterns`.

Header attribute names are ACDD / NCEI swath-template names (`time_coverage_start`,
`*most_longitude`, `software_build_version`, ...), which is a NetCDF convention and not
instrument knowledge; the core still knows nothing about what the variables mean.
"""
from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

import netCDF4 as nc
import numpy as np
from shapely.geometry import Polygon, box

from stratum.access.auth import earthdata_login
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
    """The id shared by every source: the file name minus the glob's literal prefix and suffix,
    i.e. what the wildcards matched (12 section 5). `LocalSource` and `CMRSource` both call
    this on the collection's first asset, which is what makes their indexes interchangeable."""
    prefix, suffix = split_pattern(pattern)
    gid = name[len(prefix):]
    if suffix and gid.endswith(suffix):
        gid = gid[:-len(suffix)]
    return gid


def file_matches(name: str, pattern: str) -> bool:
    """Does a bare file name match the glob's basename? The catalogue analogue of `root.glob`:
    a CMR record lists file names, not paths, so only the pattern's last component applies."""
    base = pattern.replace("\\", "/").rsplit("/", 1)[-1]
    return fnmatchcase(name, base)


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
            raise ValueError(f"patterns: collection {collection!r} declares no asset globs")
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
                if asset in found.get(gid, {}):
                    raise ValueError(f"{collection}/{asset}: {pattern!r} matches two files of "
                                     f"granule {gid!r} ({found[gid][asset].name}, {path.name}); "
                                     "one glob names one file per granule (12 section 5)")
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


# ------------------------------------------------------------------------------------ CMR
GET_DATA = "GET DATA"
GET_DATA_DIRECT = "GET DATA VIA DIRECT ACCESS"


def _umm_of(granule: Any) -> Mapping[str, Any]:
    """The UMM-G document of an earthaccess `DataGranule` (a dict with an `umm` key), or of a
    bare UMM-G mapping."""
    try:
        return granule["umm"]
    except (KeyError, TypeError):
        return granule


def umm_polygon(umm: Mapping[str, Any]) -> Polygon:
    """`SpatialExtent.HorizontalSpatialDomain.Geometry`: the first GPolygon's boundary as a
    shapely polygon (EMIT footprints are simple, 12 section 5), else the first
    BoundingRectangle as a box. Anything else raises naming the granule rather than dropping it
    silently (02 section 4)."""
    geometry = (umm.get("SpatialExtent", {}).get("HorizontalSpatialDomain", {})
                .get("Geometry", {}))
    polygons = geometry.get("GPolygons") or []
    if polygons:
        points = polygons[0].get("Boundary", {}).get("Points") or []
        if len(points) >= 3:
            return Polygon([(float(p["Longitude"]), float(p["Latitude"])) for p in points])
    rects = geometry.get("BoundingRectangles") or []
    if rects:
        r = rects[0]
        return box(float(r["WestBoundingCoordinate"]), float(r["SouthBoundingCoordinate"]),
                   float(r["EastBoundingCoordinate"]), float(r["NorthBoundingCoordinate"]))
    raise ValueError(f"{umm.get('GranuleUR')!r}: no GPolygon or BoundingRectangle in UMM-G "
                     "SpatialExtent; cannot index it (12 section 5)")


def umm_files(umm: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    """file name -> {https, s3, checksum} from `RelatedUrls` (typed `GET DATA` / `GET DATA VIA
    DIRECT ACCESS`) and `DataGranule.ArchiveAndDistributionInformation` (SHA-512 per file),
    joined on the file name (12 section 5). A file with no `GET DATA` link is unreachable over
    https and is left out."""
    files: dict[str, dict[str, str]] = {}
    for url in umm.get("RelatedUrls") or []:
        kind = url.get("Type")
        href = url.get("URL")
        if not href or kind not in (GET_DATA, GET_DATA_DIRECT):
            continue
        name = href.rstrip("/").rsplit("/", 1)[-1]
        files.setdefault(name, {})["https" if kind == GET_DATA else "s3"] = href
    for info in umm.get("DataGranule", {}).get("ArchiveAndDistributionInformation") or []:
        name = info.get("Name")
        checksum = info.get("Checksum") or {}
        if name not in files:
            continue
        if checksum.get("Value"):
            algo = str(checksum.get("Algorithm", "")).lower().replace("-", "")
            files[name]["checksum"] = f"{algo}:{str(checksum['Value']).lower()}"
        if info.get("SizeInBytes") is not None:
            files[name]["size"] = str(int(info["SizeInBytes"]))
    return {name: f for name, f in files.items() if "https" in f}


def umm_additional_attributes(umm: Mapping[str, Any]) -> dict[str, str]:
    """`AdditionalAttributes` verbatim, stringified: one value as itself, several joined with
    commas (12 section 5 - `SOFTWARE_BUILD_VERSION`, `SOLAR_ZENITH`, `ORBIT`, ...)."""
    out: dict[str, str] = {}
    for attr in umm.get("AdditionalAttributes") or []:
        name = attr.get("Name")
        if not name:
            continue
        values = attr.get("Values")
        if values is None:
            continue
        if isinstance(values, (list, tuple)):
            out[str(name)] = ",".join(str(v) for v in values)
        else:
            out[str(name)] = str(values)
    return out


def product_version_of(collection_version: str) -> str:
    """`001` -> `V001`: CMRSource derives the product stamp from the collection version, which
    is all it has ever tracked (12 section 5)."""
    v = str(collection_version or "")
    return f"V{v}" if v.isdigit() else v


class CMRSource:
    """The bridge over earthaccess (12 section 5): UMM-G -> `GranuleRecord`, assets resolved
    to names through `patterns`, the null policy applied, module-level login state never
    touched.

    `patterns` is the one shape every source takes, `{collection: {asset: glob}}` (or the
    long form pinning `version`), and here each glob is an fnmatch against the record's file
    names. A file matching no pattern is not indexed - which is how a record's 1.85 GB primary
    file and its browse PNG stay out of a mosaic run - and a collection with no entry is not
    searched. The granule id is what the FIRST listed asset's glob matched, exactly as
    `LocalSource` derives it; a record whose first asset matched nothing has no primary file and
    no id, so it is skipped and counted in `skipped[collection]` with a warning.

    Per record: `assets[asset]` is the https `GET DATA` URL (`prefer: https`, the only mode this
    slice runs; `direct` is refused at construction naming 12 section 4); the s3 link is kept in
    `attributes['s3:<asset>']` and its `SizeInBytes` in `attributes['size:<asset>']`;
    `checksums[asset]` is `sha512:<hex>`; `cloud_fraction` is
    `CloudCover` (an integer percent) as a fraction in [0, 1], None when absent (02 section
    4), with the percent kept verbatim in `attributes['cloud_cover']`; `build_version` is
    `SOFTWARE_BUILD_VERSION`; `collection_version` is `CollectionReference.Version` and
    `product_version` is derived from it (`001` -> `V001`); `day_night` is
    `DataGranule.DayNightFlag`; `attributes` carries every AdditionalAttribute verbatim plus
    `granule_ur`. `raw` is the UMM-G document.

    Search is a CMR granule query, which needs no Earthdata login; `login` is held for the
    authenticated paths (direct access) and is never called here, so a search costs no
    credential lookup. earthaccess pages with `CMR-Search-After` internally (12 section 5).
    """

    name = "cmr"

    def __init__(self, patterns: Mapping[str, Mapping[str, Any]], *, provider: str = "LPCLOUD",
                 prefer: str = "https", login: Callable[[], Any] = earthdata_login) -> None:
        if prefer != "https":
            raise NotImplementedError(
                f"prefer: {prefer!r} - direct s3:// access is in-region only and a later slice; "
                "this slice indexes https URLs (12 section 4)")
        self._patterns = _normalise_patterns(patterns)
        self.provider = provider
        self.prefer = prefer
        self._login = login
        self.skipped: Counter[str] = Counter()

    @property
    def collections(self) -> tuple[str, ...]:
        return tuple(self._patterns)

    # -- one record --------------------------------------------------------------------------
    def _matched(self, collection: str, umm: Mapping[str, Any]) -> dict[str, tuple[str, dict]]:
        """asset -> (file name, {https, s3, checksum, size}) for the files of one record that
        match the collection's patterns. A glob matching two files of one record is an error:
        `LocalSource` would make two records of them with different ids, so the two indexes
        would stop being interchangeable (12 section 5)."""
        _, assets = self._patterns[collection]
        files = umm_files(umm)
        out: dict[str, tuple[str, dict]] = {}
        for asset, pattern in assets.items():
            hits = [name for name in sorted(files) if file_matches(name, pattern)]
            if len(hits) > 1:
                raise ValueError(f"{collection}/{asset}: {pattern!r} matches {len(hits)} files "
                                 f"of {umm.get('GranuleUR')!r} ({hits}); one glob names one "
                                 "file per record (12 section 5)")
            if hits:
                out[asset] = (hits[0], files[hits[0]])
        return out

    def _record(self, collection: str, umm: Mapping[str, Any]) -> GranuleRecord | None:
        pinned, assets = self._patterns[collection]
        primary_asset, primary_glob = next(iter(assets.items()))
        matched = self._matched(collection, umm)
        if primary_asset not in matched:
            self.skipped[collection] += 1
            log.warning("%s: %s has no file matching %r (the first asset pattern); skipped",
                        collection, umm.get("GranuleUR"), primary_glob)
            return None
        gid = granule_id_from(matched[primary_asset][0], primary_glob)
        rng = umm.get("TemporalExtent", {}).get("RangeDateTime", {})
        try:
            start = parse_time(rng["BeginningDateTime"])
            end = parse_time(rng.get("EndingDateTime") or rng["BeginningDateTime"])
        except KeyError:
            raise ValueError(f"{umm.get('GranuleUR')!r}: no TemporalExtent.RangeDateTime; "
                             "cannot index it (12 section 5)") from None
        ref = umm.get("CollectionReference", {})
        version = str(ref.get("Version") or pinned or "")
        attributes: dict[str, Any] = umm_additional_attributes(umm)
        attributes["granule_ur"] = str(umm.get("GranuleUR", ""))
        for asset, (_, f) in matched.items():
            if "s3" in f:
                attributes[f"s3:{asset}"] = f["s3"]
            if "size" in f:
                attributes[f"size:{asset}"] = f["size"]      # SizeInBytes, for a size guard
        attributes["granule_id"] = gid
        attributes["build_version"] = attributes.get("SOFTWARE_BUILD_VERSION", "")
        attributes["product_version"] = product_version_of(version)
        attributes["collection_version"] = version
        attributes["day_night"] = umm.get("DataGranule", {}).get("DayNightFlag")
        # CloudCover is an integer PERCENT in UMM-G; the index column is a FRACTION in [0, 1]
        # (its name, GranuleRef.cloud_fraction and the manifest's max_cloud_fraction all say
        # so), so it is scaled here and the catalogue's value kept verbatim as `cloud_cover`.
        # Absent stays None, never 0.0 (02 section 4).
        cloud = umm.get("CloudCover")
        if cloud is not None:
            attributes["cloud_cover"] = str(cloud)
        attributes["cloud_fraction"] = None if cloud is None else float(cloud) / 100.0
        return GranuleRecord(native_id=str(umm.get("GranuleUR", gid)), collection=collection,
                             datetime=start, end_datetime=end, geometry=umm_polygon(umm),
                             attributes=attributes, raw=umm)

    # -- the query ---------------------------------------------------------------------------
    def query_parameters(self, collection: str, *,
                         bbox: tuple[float, float, float, float] | None = None,
                         start: datetime | None = None, end: datetime | None = None,
                         updated_since: datetime | None = None) -> dict[str, Any]:
        """The keyword arguments handed to `earthaccess.search_data` for one collection.
        `version` only when `patterns` pins one, so an unpinned search records whatever CMR
        returns; `updated_since` is CMR's `revision_date` (open-ended), which earthaccess
        exposes by that name - how a reprocessing campaign gets noticed (12 section 5)."""
        pinned, _ = self._patterns[collection]
        params: dict[str, Any] = {"short_name": collection}
        if self.provider:
            params["provider"] = self.provider
        if pinned:
            params["version"] = str(pinned)
        if bbox is not None:
            params["bounding_box"] = tuple(float(v) for v in bbox)
        if start is not None or end is not None:
            params["temporal"] = (_utc(start) if start is not None else None,
                                  _utc(end) if end is not None else None)
        if updated_since is not None:
            params["revision_date"] = (_utc(updated_since), None)
        return params

    def search(self, *, collections: Sequence[str] | None = None,
               bbox: tuple[float, float, float, float] | None = None,
               start: datetime | None = None, end: datetime | None = None,
               updated_since: datetime | None = None) -> Iterator[GranuleRecord]:
        """One record per (collection, granule). CMR matches on footprint intersection and on
        temporal *overlap*; the acquisition-start window `[start, end)` is applied here as well
        so the answer matches `LocalSource` exactly."""
        import earthaccess  # deferred: a local-only run never pays for it

        wanted = list(collections) if collections is not None else list(self._patterns)
        unknown = [c for c in wanted if c not in self._patterns]
        if unknown:
            raise LookupError(f"CMRSource has no patterns for {unknown}; configured: "
                              f"{sorted(self._patterns)} (12 section 5)")
        for collection in wanted:
            params = self.query_parameters(collection, bbox=bbox, start=start, end=end,
                                           updated_since=updated_since)
            log.info("CMR search %s", {k: v for k, v in params.items()})
            for granule in earthaccess.search_data(**params):
                record = self._record(collection, _umm_of(granule))
                if record is None:
                    continue
                if start is not None and record.datetime < _utc(start):
                    continue
                if end is not None and record.datetime >= _utc(end):
                    continue
                yield record

    # -- what the index stores ---------------------------------------------------------------
    def assets(self, record: GranuleRecord) -> Mapping[str, str]:
        """asset name -> https `GET DATA` URL, for the files that matched `patterns`."""
        return {asset: f["https"]
                for asset, (_, f) in sorted(self._matched(record.collection, record.raw).items())}

    def checksums(self, record: GranuleRecord) -> Mapping[str, str]:
        """asset name -> `sha512:<hex>`; the asset identity that enters cache keys
        (12 section 4, 02 section 2)."""
        return {asset: f["checksum"]
                for asset, (_, f) in sorted(self._matched(record.collection, record.raw).items())
                if "checksum" in f}
