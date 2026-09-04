"""The plan stage (00 section 2 stage 1; 09 sections 4-5; 02 sections 3, 6; 12 section 7).

    manifest -> validate -> index (read and scope-checked, or built) -> query -> filters ->
    role assets -> freeze -> budget gate -> plan-time checks against real granules ->
    vintage check -> work lists -> plan.json + report.md

Everything that can fail cheaply fails here, before a worker exists. Nothing here reads a pixel
band: the data-dependent checks open headers, `variables()` and embedded class tables only.
The budget gate (09 section 4) sits BEFORE those checks: against a catalogue source they
download real assets, and an over-budget plan must refuse before the first byte.
"""
from __future__ import annotations

import dataclasses
import logging
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pandas as pd
import yaml
from rasterio.warp import transform_bounds

from stratum.access import (
    AssetStore,
    CachedAsset,
    CMRSource,
    LocalSource,
    asset_cache_for,
    reader_for,
)
from stratum.cache import CacheRoot
from stratum.classes import Enumeration, EnumerationError, Remap, identity_enumeration
from stratum.filters import FilterError, FilterReport, apply_filters, build_filters
from stratum.index import (
    INDEX_FILE,
    build_index,
    freeze_index,
    granule_refs,
    index_scope_record,
    merge_index,
    query_index,
    read_index,
    read_index_scope,
    read_index_table,
    role_asset,
    role_uri,
    scope_problems,
    write_index,
)
from stratum.manifest import Manifest, load_manifest, manifest_hash, validate_static
from stratum.plan.document import (
    MERGED_NAME,
    PLAN_SCHEMA_VERSION,
    REPORT_NAME,
    STAGES,
    PlanError,
    context_to_doc,
    epoch_to_doc,
    iso,
    period_to_doc,
    write_plan,
    write_work,
)
from stratum.plugins import resolve
from stratum.publish import build_mappers, check_formats
from stratum.reduce import validate_schema
from stratum.regrid import (
    REGRID_ALGO_VERSION,
    LatticeMismatch,
    lattice_offset,
    resolve_max_distance,
)
from stratum.resolve import (
    AliasBinding,
    PlanContext,
    PluginBinding,
    RoleBinding,
    candidates,
    plugin_version,
)
from stratum.resolve.observation import is_lonlat
from stratum.types import (
    BlockRef,
    ClassTable,
    Epoch,
    GranuleReader,
    GranuleRef,
    GranuleSource,
    LayerSpec,
    SnapshotSchema,
    TileRef,
    VarSpec,
)

log = logging.getLogger(__name__)

Bbox = tuple[float, float, float, float]
#: Threads the planner stages class-table assets with (12 section 7).
PLAN_FETCH_WORKERS = 8


@dataclass
class PlanResult:
    """What `plan_run` produced. `over_budget` is reported, not raised: `stratum plan` exits 2
    on it and a run refuses unless `budget.on_exceed` is `warn` (09 section 4)."""

    run_id: str
    run_dir: Path
    root: Path
    document: dict[str, Any]
    report: str
    over_budget: bool
    budget_problems: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def on_exceed(self) -> str:
        return str(self.document["budget"]["on_exceed"])

    @property
    def refused(self) -> bool:
        """True when a run must not proceed: over budget and not `warn`."""
        return self.over_budget and self.on_exceed != "warn"


# ------------------------------------------------------------------------------------------ paths
def is_uri(text: str) -> bool:
    scheme = urlparse(text).scheme
    return len(scheme) > 1 and scheme.lower() != "file"


def resolve_local(m: Manifest, value: str, what: str) -> Path:
    """A manifest path made absolute against the manifest's directory. A remote URI is
    refused: storage beyond the local root is 12 section 4 / 08 section 1."""
    if is_uri(value):
        raise NotImplementedError(f"{what} {value!r} is a remote URI; this slice runs on a local "
                                  "root only (12 section 4, 08 section 1)")
    path = Path(value.removeprefix("file://")).expanduser()
    return path if path.is_absolute() else (m.base_dir / path).resolve()


def storage_root(m: Manifest) -> Path:
    """`outputs.bucket` locally is the storage root: cache/, runs/, products/ hang off it."""
    return resolve_local(m, m.outputs.bucket, "outputs.bucket")


# ------------------------------------------------------------------------------------------ index
def source_patterns(m: Manifest) -> dict[str, Mapping[str, Any]]:
    """`inputs.source.patterns` as a source takes it, `{collection: {asset: glob}}` for every
    kind (12 section 5). The one-glob form, `pattern:`, names no collection or asset, so it is
    accepted only when every role reads ONE collection and agrees on its asset: it becomes
    `{collection: {asset: pattern}}`, the asset named as the roles name it (or after the
    collection when none does)."""
    src = m.inputs.source
    assert src is not None
    if src.patterns:
        return dict(src.patterns)
    if src.pattern is None:
        raise PlanError(f"inputs.source (kind: {src.kind}) declares no patterns: "
                        "{collection: {asset: glob}}; nothing would be indexed (12 section 5)")
    collections = sorted({r.collection for r in m.inputs.roles.values()})
    assets = sorted({r.asset for r in m.inputs.roles.values() if r.asset})
    if len(collections) != 1 or len(assets) > 1:
        raise PlanError(f"inputs.source.pattern is one glob but the roles read collections "
                        f"{collections} / assets {assets}; use patterns: {{collection: {{asset: "
                        "glob}}} (12 section 5)")
    return {collections[0]: {assets[0] if assets else collections[0]: src.pattern}}


local_patterns = source_patterns   # the first-slice name


def pin_role_versions(m: Manifest, patterns: Mapping[str, Mapping[str, Any]],
                      ) -> dict[str, Mapping[str, Any]]:
    """Fold a role's `version:` into the collection's patterns entry (the long form, 12 section
    5) so a catalogue search is filtered on it and an unpinned one is not. Two roles pinning
    different versions of one collection, or a pin disagreeing with the long form, is a plan-time
    error (02 section 5)."""
    out: dict[str, Mapping[str, Any]] = {}
    pins: dict[str, set[str]] = {}
    for role in m.inputs.roles.values():
        if role.version:
            pins.setdefault(role.collection, set()).add(str(role.version))
    for collection, versions in pins.items():
        if len(versions) > 1:
            raise PlanError(f"roles pin {collection} to versions {sorted(versions)}; a collection "
                            "resolves to one version per run (02 section 5)")
    for collection, spec in patterns.items():
        pinned = next(iter(pins.get(collection, ())), None)
        long_form = "assets" in spec and isinstance(spec["assets"], Mapping)
        if pinned is None:
            out[collection] = spec
        elif long_form:
            declared = spec.get("version")
            if declared is not None and str(declared) != pinned:
                raise PlanError(f"inputs.source.patterns.{collection} pins version {declared!r} "
                                f"but a role pins {pinned!r} (02 section 5)")
            out[collection] = {**spec, "version": pinned}
        else:
            out[collection] = {"version": pinned, "assets": dict(spec)}
    return out


def source_from_manifest(m: Manifest) -> GranuleSource:
    """`inputs.source` as a `GranuleSource`, dispatching on `kind` (12 section 5-6): `local`
    walks `root`; `cmr` searches with `patterns` (role `version:` pins folded in) over
    `provider`, https URLs only - `prefer: direct` is refused naming 12 section 4; `stac` and
    `parquet` are later slices."""
    src = m.inputs.source
    if src is None:
        raise PlanError("inputs.source is not set; nothing to build the index from "
                        "(12 section 6)")
    if src.kind == "local":
        assert src.root is not None
        return LocalSource(resolve_local(m, src.root, "inputs.source.root"), source_patterns(m))
    if src.kind == "cmr":
        if src.prefer == "direct":
            raise NotImplementedError("inputs.source.prefer: direct - s3:// access is in-region "
                                      "only and a later slice; use prefer: https (12 section 4)")
        return CMRSource(pin_role_versions(m, source_patterns(m)),
                         provider=src.provider or "LPCLOUD", prefer=src.prefer or "https")
    raise NotImplementedError(f"inputs.source.kind: {src.kind} is a later slice (12 section 5)")


def local_source(m: Manifest) -> LocalSource:
    """The first-slice entry point: `source_from_manifest` for a `local` source only."""
    src = m.inputs.source
    if src is None or src.kind != "local":
        raise PlanError("inputs.source is not a local source; build the index with "
                        "`stratum index build` from a catalogue source (12 section 5)")
    source = source_from_manifest(m)
    assert isinstance(source, LocalSource)
    return source


def index_scope(m: Manifest, source: GranuleSource) -> dict[str, Any]:
    """What an index build asks the source for. A catalogue source is scoped to the manifest's
    AOI bbox and time range - the query is cheap and the answer is frozen per run - while a
    local source indexes its whole directory (12 section 5)."""
    if isinstance(source, LocalSource):
        return {}
    return {"bbox": m.aoi_bbox(), "start": m.time.start, "end": m.time.end}


def build_index_from_manifest(m: Manifest, *, collections: Sequence[str] | None = None,
                              since: datetime | None = None, path: Path | None = None) -> Path:
    """`stratum index build`: populate `inputs.index_location` from `inputs.source` - a local
    directory, or CMR over the manifest's AOI and time range (`index_scope`) - and record that
    scope in the file so `plan_run` can refuse a manifest the index does not cover (02 section
    6). `plan_run` calls this implicitly when the index file is absent, so a run is one command.

    `since` is a refresh, not a rebuild: it reaches the source as `updated_since` (revision
    date, or file mtime) and the rows that come back REPLACE their earlier versions in the
    existing index, keyed on (collection, collection_version, granule_id); every other row is
    kept, and the scope stays the one the index was built with (12 section 5). A refresh needs
    an existing index whose scope covers the manifest - a wider manifest is a full rebuild."""
    source = source_from_manifest(m)
    wanted = list(collections) if collections else list(getattr(source, "collections", ()))
    scope_kw = index_scope(m, source)
    target = path if path is not None else index_path(m)
    src = m.inputs.source
    assert src is not None
    existing = None
    if since is not None:
        if not target.is_file():
            raise PlanError(f"--since refreshes an existing index, and {target} does not exist; "
                            "build it first without --since (12 section 5)")
        scope = read_index_scope(target)
        problems = scope_problems(scope, source=src.kind, collections=wanted, **scope_kw)
        if problems:
            raise PlanError(f"--since cannot widen an index; {target} does not cover this "
                            "manifest (rebuild it without --since):\n  - "
                            + "\n  - ".join(problems))
        existing = read_index_table(target)
    else:
        scope = index_scope_record(source=src.kind, provider=src.provider, collections=wanted,
                                   **scope_kw)
    table = build_index(source, collections=wanted, updated_since=since, **scope_kw)
    skipped = getattr(source, "skipped", None)
    if skipped:
        log.warning("index build skipped records with no primary file: %s", dict(skipped))
    if existing is not None:
        log.info("index refresh: %d revised row(s) merged into %d", table.num_rows,
                 existing.num_rows)
        table = merge_index(existing, table)
    return write_index(table, target, scope)


def check_index_scope(m: Manifest, idx_path: Path, collections: Sequence[str]) -> None:
    """A run reads whatever index `inputs.index_location` holds, so before planning against it
    the planner checks that the index was built for at least this manifest: same source kind,
    every collection the roles read, an AOI bbox and `[time.start, time.end)` inside the ones
    indexed (02 section 6; 12 section 5). Widening `time.end` after the build would otherwise
    plan silently against the narrower index. Refuses with the reasons and the fix."""
    src = m.inputs.source
    problems = scope_problems(read_index_scope(idx_path), source=src.kind if src else None,
                              collections=collections, bbox=m.aoi_bbox(),
                              start=m.time.start, end=m.time.end)
    if problems:
        raise PlanError(f"index {idx_path} does not cover this manifest:\n  - "
                        + "\n  - ".join(problems)
                        + "\nRebuild it with `stratum index build -m <manifest>` (or delete it "
                        "and let `stratum plan` build it) (02 section 6, 12 section 5)")


def index_path(m: Manifest) -> Path:
    """`inputs.index_location` is a directory; the index file inside has a fixed name
    (INDEX_FILE), so nobody names or renames it by hand (12 section 6)."""
    if m.inputs.index_location is None:
        raise PlanError("inputs.index_location is not set; a run reads a frozen index "
                        "(02 section 6)")
    return resolve_local(m, m.inputs.index_location, "inputs.index_location") / INDEX_FILE


def pin_versions(m: Manifest, frame: pd.DataFrame) -> pd.DataFrame:
    """A collection the index holds under several `collection_version`s must be pinned with
    `version:` on every role that reads it (02 section 5, 12 section 3); pinned versions drop
    the other rows."""
    keep = pd.Series(True, index=frame.index)
    for name, role in m.inputs.roles.items():
        rows = frame["collection"] == role.collection
        versions = sorted(set(frame.loc[rows, "collection_version"].astype(str)))
        if role.version is not None:
            if str(role.version) not in versions and versions:
                raise PlanError(f"inputs.roles.{name}: version {role.version!r} pinned but the "
                                f"index holds {role.collection} only as {versions}")
            keep &= ~rows | (frame["collection_version"].astype(str) == str(role.version))
        elif len(versions) > 1:
            raise PlanError(f"inputs.roles.{name}: the index holds {role.collection} under "
                            f"collection versions {versions}; pin one with version: "
                            "(02 section 5)")
    return frame[keep].reset_index(drop=True)


# ---------------------------------------------------------------------------------------- plugins
def instantiate(kind: str, ref: str, params: Mapping[str, Any]) -> PluginBinding:
    inst = resolve(kind, ref)(**dict(params))
    return PluginBinding(ref=ref, version=plugin_version(inst), params=dict(params), instance=inst)


def role_of(m: Manifest, name: str) -> str:
    if name in m.inputs.roles:
        return name
    return m.inputs.band_aliases[name].role


def roles_needed(m: Manifest, scorer: PluginBinding, masks: Sequence[PluginBinding],
                 geolocation_role: str) -> list[str]:
    """The roles a granule must provide to contribute: everything resolve reads (schema
    sources, scorer and mask `required_roles`, through aliases) plus the geolocation role regrid
    reads `loc` from. Manifest order."""
    wanted = {role_of(m, layer.source) for layer in m.snapshot.layers.values()}
    wanted.update(role_of(m, r) for r in scorer.instance.required_roles)
    for mk in masks:
        wanted.update(role_of(m, r) for r in getattr(mk.instance, "required_roles", ()))
    wanted.add(geolocation_role)
    return [r for r in m.inputs.roles if r in wanted]


# ------------------------------------------------------------------------------------ inspection
@dataclass
class Inspection:
    """What the planner learned from real granules (12 section 7, 13 section 3)."""

    schema: SnapshotSchema
    aliases: dict[str, AliasBinding]
    band_counts: dict[str, int]
    remaps: dict[str, dict[str, Remap]]
    class_tables: dict[str, dict[str, list[str]]]     # layer -> fingerprint -> granule ids
    sensor_shape: tuple[int, ...]
    staged: list[dict[str, Any]] = field(default_factory=list)   # remote assets fetched at plan


class _Opener:
    """One open reader context per asset URI, closed together. Every open goes through the
    store with the asset's catalogue checksum, so a remote asset is staged into the node-local
    asset cache exactly as a worker would stage it (12 section 4) and is a cache hit for the
    run; `staged` records each remote asset the planner touched, for the report."""

    def __init__(self, store: AssetStore, readers: Mapping[str, GranuleReader]) -> None:
        self.store, self.readers = store, readers
        self.contexts: dict[str, Any] = {}
        self.staged: list[dict[str, Any]] = []
        self._seen: set[str] = set()

    def open(self, collection: str, uri: str, checksum: str | None = None
             ) -> tuple[GranuleReader, Any]:
        reader = self.readers[collection]
        ctx = self.contexts.get(uri)
        if ctx is None:
            handle = self.store.open(uri, checksum=checksum)
            if isinstance(handle, CachedAsset) and uri not in self._seen:
                self._seen.add(uri)
                path = handle.path()
                self.staged.append({"uri": uri, "path": str(path),
                                    "bytes": path.stat().st_size if path else 0})
            ctx = self.contexts[uri] = reader.open(handle)
        return reader, ctx

    def close(self) -> None:
        for ctx in self.contexts.values():
            close = getattr(ctx, "close", None)
            if close is not None:
                close()
        self.contexts.clear()


def _resolve_alias(name: str, spec_alias: Any, var: VarSpec | None) -> AliasBinding:
    if var is None:
        raise PlanError(f"inputs.band_aliases.{name}: role {spec_alias.role!r} could not be "
                        "inspected on any granule")
    if len(var.shape) < 3:
        raise PlanError(f"inputs.band_aliases.{name}: role {spec_alias.role!r} variable "
                        f"{var.name!r} is single-band {var.shape}; an alias needs a band axis")
    nbands = int(var.shape[2])
    if spec_alias.band is not None:
        if not 0 <= spec_alias.band < nbands:
            raise PlanError(f"inputs.band_aliases.{name}: band {spec_alias.band} is outside "
                            f"{var.name!r}'s {nbands} bands (0-based)")
        return AliasBinding(spec_alias.role, int(spec_alias.band))
    hits: set[int] | None = None
    for attr, wanted in spec_alias.match.items():
        values = var.band_attrs.get(attr)
        if values is None:
            raise PlanError(f"inputs.band_aliases.{name}: reader reports no band attribute "
                            f"{attr!r} for {var.name!r}; it has {sorted(var.band_attrs)}")
        found = {i for i, v in enumerate(values) if _matches(v, wanted, spec_alias.tolerance)}
        hits = found if hits is None else hits & found
    if not hits or len(hits) != 1:
        raise PlanError(f"inputs.band_aliases.{name}: match {spec_alias.match} selects "
                        f"{sorted(hits or ())} bands of {var.name!r}; exactly one is required "
                        "(11 section 5)")
    return AliasBinding(spec_alias.role, hits.pop())


def _matches(value: Any, wanted: Any, tolerance: float | None) -> bool:
    if tolerance is not None:
        try:
            return abs(float(value) - float(wanted)) <= tolerance
        except (TypeError, ValueError):
            return False
    return value == wanted or str(value).strip() == str(wanted).strip()


def inspect_granules(m: Manifest, refs: Mapping[str, GranuleRef], readers: Mapping[str, Any],
                     needed: Sequence[str], geolocation_role: str, store: AssetStore) -> Inspection:
    """The data-dependent checks of 09 section 5 / 12 section 7 against real granules, and the
    per-granule class resolution of 13 section 3 rule 2.

    - every role's `var` exists in its reader's `variables()` on one sample granule (the first
      that provides every needed role); roles that will be read agree on the sensor shape and are
      sensor-space;
    - band aliases resolve to an index (`match:` against `VarSpec.band_attrs`);
    - continuous layers take their dtype (and, for `bands: None` on a multi-band source, their
      band count) from the VarSpec;
    - every categorical layer's class table is read from EVERY contributing granule, tables are
      grouped by fingerprint, and each distinct table is resolved into the enumeration - or, for
      `classes: source`, the identity enumeration of the first granule's table.
    """
    problems: list[str] = []
    roles = m.inputs.roles
    order = sorted(refs)
    opener = _Opener(store, readers)
    specs: dict[str, VarSpec] = {}
    try:
        # -- variables per role, on a sample granule (12 section 7)
        for name, role in roles.items():
            binding = RoleBinding.from_spec(role)
            sample = refs[order[0]]
            uri = role_uri(sample, binding)
            if uri is None:
                holder = next((g for g in order if role_uri(refs[g], binding) is not None), None)
                if holder is None:
                    problems.append(f"inputs.roles.{name}: no surviving granule provides an asset "
                                    f"for collection {role.collection!r}")
                    continue
                uri = role_uri(refs[holder], binding)
                sample = refs[holder]
            key = role_asset(sample, binding)
            reader, ctx = opener.open(role.collection, uri, sample.checksums.get(key) if key
                                      else None)
            variables = reader.variables(ctx)
            if role.var not in variables:
                problems.append(f"inputs.roles.{name}: variable {role.var!r} is not in "
                                f"{uri}; it has {sorted(variables)} (12 section 7)")
                continue
            specs[name] = variables[role.var]
        read_shapes = {r: tuple(specs[r].shape[:2]) for r in needed if r in specs}
        if len(set(read_shapes.values())) > 1:
            problems.append(f"roles read together disagree on the sensor shape {read_shapes}; one "
                            "GLT indexes one sensor array (03 section 3)")
        for r in needed:
            if getattr(readers[roles[r].collection], "space", "sensor") != "sensor":
                problems.append(f"inputs.roles.{r}: collection {roles[r].collection!r} is "
                                "ortho-native; reading an ortho role is not in this slice "
                                "(12 section 2)")

        # -- `adopt` needs the product's own lookup table, on this run's lattice (03 section 3).
        # One header answers it for the whole run, so a grid that cannot be adopted fails here
        # rather than on the first regrid work item, before any granule is downloaded.
        if m.grid.regrid_method == "adopt" and geolocation_role in roles:
            binding = RoleBinding.from_spec(roles[geolocation_role])
            holder = next((g for g in order if role_uri(refs[g], binding) is not None), None)
            if holder is None:
                problems.append(f"grid.regrid_method: 'adopt' reads the lookup table from the "
                                f"geolocation role {geolocation_role!r}, which no surviving "
                                "granule provides")
            else:
                uri = role_uri(refs[holder], binding)
                asset = role_asset(refs[holder], binding)
                reader, gctx = opener.open(binding.collection, uri,
                                           refs[holder].checksums.get(asset) if asset else None)
                embedded = reader.glt(gctx) if hasattr(reader, "glt") else None
                if embedded is None:
                    problems.append(f"grid.regrid_method: 'adopt' needs the product's own lookup "
                                    f"table, but {uri} ships none (03 section 3)")
                else:
                    try:
                        lattice_offset(m.grid_def(), embedded.transform, embedded.crs)
                    except LatticeMismatch as exc:
                        problems.append(f"grid.regrid_method: 'adopt' cannot use {uri}: {exc}")

        if problems:
            raise PlanError("plan-time validation failed:\n  - " + "\n  - ".join(problems))
        sensor_shape = read_shapes.get(geolocation_role) or next(iter(read_shapes.values()))

        # -- aliases (11 section 5)
        aliases = {name: _resolve_alias(name, a, specs.get(a.role))
                   for name, a in m.inputs.band_aliases.items()}

        # -- the schema, resolved (13 sections 2-3)
        enums = m.enumerations()
        layers: list[LayerSpec] = []
        band_counts: dict[str, int] = {}
        remaps: dict[str, dict[str, Remap]] = {}
        class_tables: dict[str, dict[str, list[str]]] = {}
        for layer in m.snapshot_schema().layers:
            src_role = role_of(m, layer.source)
            spec = specs[src_role]
            if layer.kind == "continuous":
                nb = 1
                if layer.source in roles and len(spec.shape) >= 3:
                    nb = int(spec.shape[2])
                    if layer.bands is not None:
                        bad = [b for b in layer.bands if not 0 <= b < nb]
                        if bad:
                            raise PlanError(f"snapshot.layers.{layer.name}: bands {bad} are "
                                            f"outside {spec.name!r}'s {nb} bands")
                        nb = len(layer.bands)
                band_counts[layer.name] = nb
                layers.append(dataclasses.replace(layer, dtype=layer.dtype or spec.dtype))
                continue
            if layer.source not in roles or roles[layer.source].class_table is None:
                raise PlanError(f"snapshot.layers.{layer.name}: a categorical layer's source must "
                                "be a role declaring class_table (11 section 9)")
            table_spec = roles[layer.source].class_table
            assert table_spec is not None
            if table_spec.source != "embedded":
                raise NotImplementedError(f"class_table.source {table_spec.source!r} is not in "
                                          "this slice; only embedded tables are (11 section 9)")
            binding = RoleBinding.from_spec(roles[layer.source])
            tables: dict[str, ClassTable] = {}
            by_granule: dict[str, str] = {}
            # every contributing granule's class-table asset is needed (the vintage check);
            # against a catalogue source that is one download per granule, so they are staged
            # together through a thread pool rather than one at a time (12 section 7)
            store.prefetch(_role_assets(refs, order, binding), workers=PLAN_FETCH_WORKERS)
            for gid in order:
                uri = role_uri(refs[gid], binding)
                assert uri is not None  # needed roles were filtered on asset availability
                key = role_asset(refs[gid], binding)
                reader, ctx = opener.open(binding.collection, uri,
                                          refs[gid].checksums.get(key) if key else None)
                table = reader.class_table(ctx, table_spec.path, table_spec.key,
                                           table_spec.attributes)
                if table is None:
                    raise PlanError(f"granule {gid!r}: {uri} ships no class table at "
                                    f"{table_spec.path!r} (11 section 9)")
                fp = table.fingerprint()
                tables.setdefault(fp, table)
                by_granule[gid] = fp
            class_tables[layer.name] = {fp: [g for g, f in by_granule.items() if f == fp]
                                        for fp in tables}
            enum = enums.get(layer.name)
            if enum is None:                      # classes: source
                enum = identity_enumeration(tables[by_granule[order[0]]])
            remaps[layer.name] = _resolve_tables(m, layer.name, enum, tables, by_granule)
            layers.append(dataclasses.replace(layer, classes=enum.class_table(),
                                              lumping=enum.fingerprint()))
            # every opened context of this layer's role is closed before the next layer's pass
            opener.close()
    finally:
        opener.close()

    schema = SnapshotSchema(name=m.snapshot.name, layers=tuple(layers),
                            extends=tuple(m.snapshot.extends))
    validate_schema(schema)
    return Inspection(schema=schema, aliases=aliases, band_counts=band_counts, remaps=remaps,
                      class_tables=class_tables, sensor_shape=sensor_shape,
                      staged=list(opener.staged))


def _role_assets(refs: Mapping[str, GranuleRef], order: Sequence[str],
                 binding: RoleBinding) -> list[tuple[str, str | None]]:
    """`(uri, checksum)` of one role's asset on each granule, for `AssetStore.prefetch`."""
    out: list[tuple[str, str | None]] = []
    for gid in order:
        uri = role_uri(refs[gid], binding)
        if uri is None:
            continue
        key = role_asset(refs[gid], binding)
        out.append((uri, refs[gid].checksums.get(key) if key else None))
    return out


def _resolve_tables(m: Manifest, layer: str, enum: Enumeration, tables: Mapping[str, ClassTable],
                    by_granule: Mapping[str, str]) -> dict[str, Remap]:
    """The vintage check (02 section 3, 13 section 3): one fingerprint, or `allow_mixed_vintage`
    with every table resolving fully. Fails naming the offending fingerprints and granules."""
    if len(tables) > 1 and not m.allow_mixed_vintage:
        lines = [f"    {fp}: {len([g for g, f in by_granule.items() if f == fp])} granule(s), "
                 f"e.g. {next(g for g, f in by_granule.items() if f == fp)!r}" for fp in tables]
        raise PlanError(
            f"snapshot.layers.{layer}: contributing granules carry {len(tables)} different class "
            "tables (mixed vintage, 02 section 3). Set allow_mixed_vintage with a reason, or pin "
            "the selection. Fingerprints:\n" + "\n".join(lines))
    resolved: dict[str, Remap] = {}
    failures: list[str] = []
    for fp, table in tables.items():
        try:
            resolved[fp] = enum.resolve(table)
        except EnumerationError as e:
            gids = [g for g, f in by_granule.items() if f == fp]
            failures.append(f"table {fp} ({len(gids)} granule(s), e.g. {gids[0]!r}): {e}")
    if failures:
        raise PlanError(f"snapshot.layers.{layer}: class tables do not resolve into enumeration "
                        f"{enum.name!r} (13 section 3):\n  - " + "\n  - ".join(failures))
    return {gid: resolved[fp] for gid, fp in by_granule.items()}


# --------------------------------------------------------------------------------------- geometry
def tile_lonlat_bounds(tile: TileRef) -> Bbox:
    w, s, e, n = tile.bounds
    if is_lonlat(tile.grid.crs):
        return (w, s, e, n)
    return transform_bounds(tile.grid.crs, "EPSG:4326", w, s, e, n)


def block_lonlat_bounds(block: BlockRef) -> Bbox:
    t = block.transform
    win = block.window
    x0, y0 = t.c, t.f
    x1, y1 = t.c + win.width * t.a, t.f + win.height * t.e
    w, e = min(x0, x1), max(x0, x1)
    s, n = min(y0, y1), max(y0, y1)
    if is_lonlat(block.tile.grid.crs):
        return (w, s, e, n)
    return transform_bounds(block.tile.grid.crs, "EPSG:4326", w, s, e, n)


def boxes_meet(a: Bbox, b: Bbox) -> bool:
    return not (a[0] >= b[2] or a[2] <= b[0] or a[1] >= b[3] or a[3] <= b[1])


def granule_meets(ref: GranuleRef, box: Bbox) -> bool:
    gw, gs, ge, gn = ref.bbox
    w, s, e, n = box
    return not (gw > e or ge < w or gs > n or gn < s)


# ------------------------------------------------------------------------------------- the stage
def plan_run(manifest_path: Path | str, out_dir: Path | str | None = None,
             patches: Sequence[Path | str] = ()) -> PlanResult:
    """Plan one run. Writes `{run_dir}/manifest.merged.yaml`, `index.parquet`, `plan.json`,
    `work/*.jsonl` and `report.md`; `run_dir` defaults to `{root}/runs/{run_id}` where the root
    is `outputs.bucket` (a path relative to the manifest). Raises `PlanError` on anything a run
    could not survive; an exceeded budget is reported in the result, not raised."""
    manifest_path = Path(manifest_path).resolve()
    m = load_manifest(manifest_path, patches)
    problems = validate_static(m)
    if problems:
        raise PlanError("manifest is not runnable (09 section 5):\n  - " + "\n  - ".join(problems))

    root = storage_root(m)
    mhash = manifest_hash(m)
    run_id = m.run_id
    run_dir = Path(out_dir).resolve() if out_dir is not None else root / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / MERGED_NAME).write_text(yaml.safe_dump(m.document(), sort_keys=False))

    grid = m.grid_def()
    tiles = m.tiles(grid)
    epochs = m.epochs()
    periods = m.delivery_periods()

    # -- readers (12 section 3), geolocation role (03 section 3)
    overrides = dict(m.inputs.readers)
    readers: dict[str, Any] = {}
    for name, role in m.inputs.roles.items():
        try:
            readers[role.collection] = reader_for(role.collection, overrides)
        except (LookupError, ImportError, AttributeError) as e:
            raise PlanError(f"inputs.roles.{name}: {e}") from None
    geolocation_role = m.geolocation_role(lambda c: getattr(readers[c], "space", "sensor"))

    # -- the index (02 sections 3, 6): built when absent, and never planned against unless
    #    its recorded scope covers this manifest (a wider AOI or time window is a rebuild)
    idx_path = index_path(m)
    collections = sorted({r.collection for r in m.inputs.roles.values()})
    built = False
    if not idx_path.is_file():
        if m.inputs.source is not None:
            build_index_from_manifest(m, path=idx_path)      # local directory, or a scoped CMR query
            built = True
        else:
            raise PlanError(f"index {idx_path} does not exist and inputs.source is not set; build "
                            "it with `stratum index build` (12 section 5)")
    check_index_scope(m, idx_path, collections)
    frame = read_index(idx_path)
    aoi_boxes = m.aoi_boxes(grid)
    selected = query_index(frame, bbox=m.aoi_bbox(), start=m.time.start, end=m.time.end,
                           collections=collections)
    selected = pin_versions(m, selected)
    n_queried = int(selected["granule_id"].nunique())

    # -- filters (04 section 2, 02 section 4): per granule, and `on_missing: fail` is a plan
    #    refusal, not a traceback
    filters = build_filters(m)
    try:
        selected, reports = apply_filters(selected, filters)
    except FilterError as e:
        raise PlanError(f"granule_filter refused the selection: {e}") from None
    refs = granule_refs(selected)

    # -- the AOI is its tiles, not the box around them (09 section 4, 02 section 6): a granule
    #    in the gap between distant zones meets the query bbox but no tile, so no work item can
    #    read it; it must not be inspected, frozen, counted or budgeted
    tile_boxes = [tile_lonlat_bounds(t) for t in tiles]
    off_tile = [gid for gid, ref in refs.items()
                if not any(granule_meets(ref, tb) for tb in tile_boxes)]
    for gid in off_tile:
        del refs[gid]
    reports.append(FilterReport("meets a tile of the AOI", len(off_tile), None))

    # -- plugins and the roles a granule must provide (12 section 7)
    scorer = instantiate("scorer", m.scorer.ref, m.scorer.params)
    masks = [instantiate("mask", mk.ref, mk.params) for mk in m.pixel_mask]
    needed = roles_needed(m, scorer, masks, geolocation_role)
    bindings = {name: RoleBinding.from_spec(role) for name, role in m.inputs.roles.items()}
    lacking = [gid for gid, ref in refs.items()
               if any(role_uri(ref, bindings[r]) is None for r in needed)]
    for gid in lacking:
        del refs[gid]
    reports.append(FilterReport(f"provides an asset for every role read {needed}", len(lacking),
                                None))
    if not refs:
        raise PlanError("no granule survives selection: nothing to run (check aoi, time, "
                        "granule_filter and the roles' collections)")
    selected = selected[selected["granule_id"].isin(list(refs))].reset_index(drop=True)

    # -- every granule falls in an epoch (11 section 4)
    outside = [gid for gid, ref in refs.items()
               if not any(e.contains(_utc(ref.datetime)) for e in epochs)]
    if outside:
        raise PlanError(f"{len(outside)} granule(s) fall outside every epoch, e.g. {outside[:3]} "
                        "(11 section 4)")

    # -- freeze (02 section 6)
    frozen_path, frozen_hash = freeze_index(selected, run_dir)

    # -- the budget gate (09 section 4) comes BEFORE the data-dependent checks: against a
    #    catalogue source those download real assets, and an over-budget plan that is going to
    #    be refused must not pay for a single byte first. `on_exceed: warn` proceeds.
    budget_problems: list[str] = []
    if len(tiles) > m.budget.max_tiles:
        budget_problems.append(f"{len(tiles)} tiles exceed budget.max_tiles {m.budget.max_tiles}")
    if len(refs) > m.budget.max_granules:
        budget_problems.append(f"{len(refs)} granules exceed budget.max_granules "
                               f"{m.budget.max_granules}")
    build_versions = dict(sorted(Counter(r.build_version for r in refs.values()).items()))
    collection_versions = {c: sorted(set(selected.loc[selected["collection"] == c,
                                                      "collection_version"].astype(str)))
                           for c in collections}
    asset_cache = asset_cache_for(root)
    doc: dict[str, Any] = {
        "plan_schema_version": PLAN_SCHEMA_VERSION,
        "run_id": run_id, "run_label": m.run_label, "manifest_hash": mhash,
        "manifest_path": str(manifest_path), "root": str(root), "run_dir": str(run_dir),
        "products_dir": str(root / "products" / run_id),
        "planned_at": iso(datetime.now(UTC)),
        "tiles": [[t.tx, t.ty] for t in tiles],
        "epochs": [epoch_to_doc(e) for e in epochs],
        "periods": [period_to_doc(p) for p in periods],
        "index": {"source": str(idx_path), "built": built, "frozen": str(frozen_path),
                  "hash": frozen_hash, "granule_count": len(refs),
                  "rows": len(selected)},
        "filters": [dataclasses.asdict(r) for r in reports],
        "build_versions": build_versions,
        "collection_versions": collection_versions,
        "budget": {"max_tiles": m.budget.max_tiles, "max_granules": m.budget.max_granules,
                   "max_vcpu_hours": m.budget.max_vcpu_hours, "on_exceed": m.budget.on_exceed,
                   "over": bool(budget_problems), "problems": budget_problems},
    }
    if budget_problems and m.budget.on_exceed != "warn":
        counts = {"tiles": len(tiles), "epochs": len(epochs), "periods": len(periods),
                  "blocks": 0, "granules": len(refs), "granules_queried": n_queried,
                  **{f"work_{s}": 0 for s in STAGES}}
        doc.update({"inspected": False, "counts": counts, "class_tables": {}, "band_counts": {},
                    "staging": {"asset_cache": str(asset_cache) if asset_cache is not None
                                else None, "assets": 0, "bytes": 0, "uris": []}})
        write_plan(run_dir, doc)
        report = render_report(doc)
        (run_dir / REPORT_NAME).write_text(report)
        return PlanResult(run_id=run_id, run_dir=run_dir, root=root, document=doc,
                          report=report, over_budget=True, budget_problems=budget_problems,
                          counts=counts)

    # -- the data-dependent checks and per-granule class resolution. The store stages remote
    #    assets into the same node-local cache the workers use (12 section 4): a catalogue
    #    source pays for one geometry asset (the sample-asset check, 12 section 7) and every
    #    contributing granule's class-table asset (the vintage check, 02 section 3) here,
    #    and those are cache hits for the run
    store = AssetStore(asset_cache)
    inspection = inspect_granules(m, refs, readers, needed, geolocation_role, store)
    max_distance = resolve_max_distance(grid, m.grid.max_distance)

    # -- outputs, validated against the resolved schema now, not after regrid has run
    #    (07 section 3 "declared in the manifest and validated at plan time"; 12 section 7)
    outputs = outputs_document(m)
    try:
        check_formats(list(m.outputs.formats))
        build_mappers(outputs, inspection.schema)
    except (ValueError, TypeError, NotImplementedError) as e:
        raise PlanError(f"outputs are not publishable (07 section 3): {e}") from None
    ctx = PlanContext(
        grid=grid, cache=CacheRoot(root), store=store, granules=refs,
        roles={name: bindings[name] for name in m.inputs.roles},
        aliases=inspection.aliases, geolocation_role=geolocation_role, schema=inspection.schema,
        scorer=scorer, masks=masks, remaps=inspection.remaps, max_distance=max_distance,
        regrid_method=m.grid.regrid_method, regrid_algo_version=REGRID_ALGO_VERSION,
        reader_overrides=overrides)

    # -- work lists (plan section 4)
    work = build_work_lists(ctx, refs, tiles, epochs, periods, aoi_boxes, geolocation_role)
    for stage in STAGES:
        write_work(run_dir, stage, work[stage])
    blocks = {(tuple(it["tile"]), tuple(it["block"])) for it in work["resolve"]}

    counts = {"tiles": len(tiles), "epochs": len(epochs), "periods": len(periods),
              "blocks": len(blocks), "granules": len(refs), "granules_queried": n_queried,
              **{f"work_{s}": len(work[s]) for s in STAGES}}
    doc.update({
        "inspected": True,
        "context": context_to_doc(ctx),
        "outputs": outputs,
        "band_counts": inspection.band_counts,
        # remote assets the planner staged (12 section 4 / 7); the cache directory is where
        # bytes land and enters no key
        "staging": {"asset_cache": str(asset_cache) if asset_cache is not None else None,
                    "assets": len(inspection.staged),
                    "bytes": int(sum(a["bytes"] for a in inspection.staged)),
                    "uris": [a["uri"] for a in inspection.staged]},
        "class_tables": inspection.class_tables,
        "counts": counts,
    })
    write_plan(run_dir, doc)
    report = render_report(doc)
    (run_dir / REPORT_NAME).write_text(report)
    return PlanResult(run_id=run_id, run_dir=run_dir, root=root, document=doc, report=report,
                      over_budget=bool(budget_problems), budget_problems=budget_problems,
                      counts=counts)


def _utc(when: datetime) -> datetime:
    return when.replace(tzinfo=UTC) if when.tzinfo is None else when.astimezone(UTC)


def outputs_document(m: Manifest) -> dict[str, Any]:
    """The `outputs` block as plan.json carries it and publish reads it: the keys the YAML
    actually wrote (`exclude_unset`), so a model default that belongs to another mapper -
    `nodata` and `on_unmapped` are the categorical mapper's - is never handed to the continuous
    one, which refuses unknown keys (07 section 3). Defaults publish and the model share
    (`formats: [cog]`, `stac: true`, `on_unmapped: fail`) are applied by publish."""
    return strip_none(m.outputs.model_dump(mode="json", by_alias=True, exclude_unset=True))


def strip_none(doc: Any) -> Any:
    """Drop None-valued keys and an empty `params` (the model's default for a config mapper,
    which takes none), recursively: publish reads `outputs` as the YAML was written, where an
    omitted key means "default" (07 section 3)."""
    if isinstance(doc, Mapping):
        return {k: strip_none(v) for k, v in doc.items()
                if v is not None and not (k == "params" and isinstance(v, Mapping) and not v)}
    if isinstance(doc, list):
        return [strip_none(v) for v in doc]
    return doc


def build_work_lists(ctx: PlanContext, refs: Mapping[str, GranuleRef], tiles: Sequence[TileRef],
                     epochs: Sequence[Epoch], periods: Iterable[Any], aoi_boxes: Sequence[Bbox],
                     geolocation_role: str) -> dict[str, list[dict[str, Any]]]:
    """The four work lists (plan section 4):

    regrid   every (granule, tile) whose bbox meets the tile, with the geolocation role's asset;
    resolve  every (tile, epoch, block) whose block meets the AOI and has >= 1 candidate;
    reduce   every (tile, block, period) with >= 1 resolve item among the period's epochs,
             listing exactly those epochs (an epoch with no candidates has no snapshot);
    publish  every (tile, period) with >= 1 reduce item.
    """
    geo = ctx.roles[geolocation_role]
    halo = int(getattr(ctx.scorer.instance, "halo", 0) or 0)
    regrid: list[dict[str, Any]] = []
    resolve_items: list[dict[str, Any]] = []
    reduce_items: list[dict[str, Any]] = []
    publish: list[dict[str, Any]] = []
    for tile in tiles:
        tb = tile_lonlat_bounds(tile)
        for gid in sorted(refs):
            ref = refs[gid]
            if not granule_meets(ref, tb):
                continue
            uri = role_uri(ref, geo)
            assert uri is not None
            asset = next((k.split("/", 1)[-1] for k, v in ref.assets.items() if v == uri), "")
            regrid.append({"granule_id": gid, "collection": geo.collection, "asset": asset,
                           "uri": uri, "tile": [tile.tx, tile.ty]})
        per_block: dict[tuple[int, int], list[Epoch]] = {}
        for block in tile.blocks():
            core = block_lonlat_bounds(block)
            if not any(boxes_meet(core, box) for box in aoi_boxes):
                continue
            haloed = BlockRef(tile, block.bx, block.by, halo=halo)
            for epoch in epochs:
                if candidates(ctx, haloed, epoch):
                    resolve_items.append({"tile": [tile.tx, tile.ty],
                                          "epoch": epoch_to_doc(epoch),
                                          "block": [block.bx, block.by]})
                    per_block.setdefault((block.bx, block.by), []).append(epoch)
        for period in periods:
            any_block = False
            for (bx, by), block_epochs in sorted(per_block.items(), key=lambda kv: (kv[0][1],
                                                                                    kv[0][0])):
                inside = [e for e in block_epochs if e in period.epochs]
                if not inside:
                    continue
                any_block = True
                reduce_items.append({"tile": [tile.tx, tile.ty], "block": [bx, by],
                                     "period": [iso(period.start), iso(period.end)],
                                     "epochs": [epoch_to_doc(e) for e in inside]})
            if any_block:
                publish.append({"tile": [tile.tx, tile.ty],
                                "period": [iso(period.start), iso(period.end)]})
    return {"regrid": regrid, "resolve": resolve_items, "reduce": reduce_items,
            "publish": publish}


# ---------------------------------------------------------------------------------------- report
def render_report(doc: Mapping[str, Any]) -> str:
    """The dry-run report (09 section 4, 10 section 5): counts, what each filter removed, the
    builds consumed, the class-table fingerprints validated, and the budget."""
    c = doc["counts"]
    b = doc["budget"]
    lines = [
        f"# Stratum plan report: {doc['run_id']}", "",
        f"- manifest: `{doc['manifest_path']}`",
        f"- manifest_hash: `{doc['manifest_hash']}`",
        f"- root: `{doc['root']}`",
        f"- run_dir: `{doc['run_dir']}`",
        f"- planned_at: {doc['planned_at']}", "",
        "## Selection", "",
        f"- index: `{doc['index']['source']}`" + (" (built by plan from inputs.source)"
                                                  if doc['index']['built'] else ""),
        f"- frozen index: `{doc['index']['frozen']}` `{doc['index']['hash']}`",
        f"- granules in AOI x time: {c['granules_queried']}",
        f"- granules after filters: {c['granules']} ({doc['index']['rows']} index rows)", "",
        "| filter | removed | on_missing |", "|---|---|---|",
    ]
    for f in doc["filters"]:
        lines.append(f"| {f['describe']} | {f['removed']} | {f['on_missing'] or '-'} |")
    lines += ["", "### Build versions consumed", ""]
    lines += [f"- `{bv or '(none)'}`: {n} granule(s)" for bv, n in doc["build_versions"].items()]
    lines += ["", "### Collection versions", ""]
    lines += [f"- {col}: {', '.join(v) or '-'}" for col, v in doc["collection_versions"].items()]
    lines += ["", "### Class-table fingerprints (the vintage check, 02 section 3)", ""]
    if doc.get("inspected") is False:
        lines.append("- not checked: the plan is over budget and was refused before the "
                     "data-dependent checks, so nothing was opened or downloaded (09 section 4)")
    elif not doc["class_tables"]:
        lines.append("- no categorical layers")
    for layer, tables in doc["class_tables"].items():
        for fp, gids in tables.items():
            lines.append(f"- {layer}: `{fp}` on {len(gids)} granule(s)")
    staging = doc.get("staging") or {}
    if staging.get("assets"):
        lines += ["", "### Assets staged at plan time (12 section 7)", "",
                  (f"- {staging['assets']} remote asset(s), {staging['bytes'] / 1e6:.0f} MB, "
                   f"into `{staging['asset_cache']}` - one geometry asset for the sample-asset "
                   "check plus every contributing granule's class-table asset for the vintage "
                   "check; all are cache hits for the run")]
    lines += ["", "## Fan-out", "",
              f"- tiles: {c['tiles']}", f"- epochs: {c['epochs']}", f"- periods: {c['periods']}",
              f"- blocks with observations: {c['blocks']}", ""]
    if doc.get("inspected") is False:
        lines.append("- work lists not written: the plan was refused at the budget gate")
    else:
        lines += ["| stage | work items |", "|---|---|"]
        lines += [f"| {s} | {c[f'work_{s}']} |" for s in STAGES]
    status = ("OVER BUDGET" if b["over"] else "within budget")
    lines += ["", "## Budget", "",
              f"- tiles: {c['tiles']} / {b['max_tiles']}",
              f"- granules: {c['granules']} / {b['max_granules']}",
              f"- vcpu hours: not estimated / {b['max_vcpu_hours']}",
              f"- status: {status} (on_exceed: {b['on_exceed']})"]
    lines += [f"  - {p}" for p in b["problems"]]
    return "\n".join(lines) + "\n"


__all__ = [
    "PLAN_FETCH_WORKERS", "Inspection", "PlanResult", "block_lonlat_bounds",
    "build_index_from_manifest", "build_work_lists", "check_index_scope", "index_path",
    "index_scope", "inspect_granules", "instantiate", "is_uri",
    "local_patterns", "local_source", "outputs_document", "pin_role_versions", "pin_versions",
    "plan_run", "render_report", "resolve_local", "roles_needed", "source_from_manifest",
    "source_patterns", "storage_root", "strip_none", "tile_lonlat_bounds",
]
