# First-slice implementation plan

**Internal.** Written 2026-09-02, when the specs were declared reviewed and implementation began.
This is the build contract the initial code was written against: what is in scope, the module
boundaries, the internal signatures the modules agree on, and what was deliberately left out.
The specs stay authoritative for contracts; this note records the *construction* decisions so
that the code's shape can be traced back to a reason.

---

## 1. Scope

CLAUDE.md fixes the first slice:

> One tile, one epoch, `MinViewZenith`, staged granules on local disk, output diffed against a
> V002 cell. No blocks, caching, Step Functions, Batch, or manifest patching.

Two of those exclusions are relaxed, because the non-negotiable tests need them
([ADR-0001 §10](../decisions/ADR-0001-tech-stack.md)): **blocks** exist from the start so seam
equivalence is testable, and **cache keys** exist from the start so key sensitivity is testable.
The local cache root is a directory; nothing talks to S3.

| In | Out (later slices) |
|---|---|
| `local` executor, process pool | `slurm`, `aws`, Step Functions, Batch, Lambda |
| `LocalSource`, `file://` assets, stage-in as identity | `CMRSource`, `s3://`, `https://`, credential refresh, prepared-asset transcode |
| Manifest models, validation, hash, `@ref` | Patch composition (`-p`) — parsed and refused |
| Regrid by `kdtree` (wrapping SpectralUtil) | `warp_embedded` |
| Resolve, `streaming` scorers | `stack` and `tile` capabilities |
| Sensor- and map-space masks | Aux data — `AuxAccessor` exists, every declared alias raises "not implemented" |
| Built-in schema-driven reducer, full vocabulary | `Reducer` plugins (resolved and refused) |
| Publish: data COGs, `categorical`/`continuous` mappers, legend, STAC item, provenance | `threshold`/`composite` mappers, `stratum render` |
| Trial run over tile (-118, 41), June 2026 | V002 parity against a real V002 cell — no V002 output is available locally; a SpectralUtil `build_obs_nc` parity run stands in |

## 2. Trial data

`trial-data/` holds 640 `EMIT_L2B_MIN_001_*.nc` and 639 `EMIT_L2B_MINUNCERT_001_*.nc`, acquired
2026-01 to 2026-08 over the western US. **No L1B OBS granules**, so `MinViewZenith` cannot run on
real data until the matching `EMITL1BOBS` granules are fetched (Earthdata credentials are in
`~/.netrc`; `earthaccess` is in the environment).

Pilot tile: `(-118, 41)` — lon [-118, -117), lat [41, 42), northern Nevada. 45 MIN granules
intersect it; ~14 fall in June 2026. The trial manifest is
`examples/trial-nevada/manifest.yaml`.

## 3. Module ownership and build order

Waves are the parallelism boundary: everything inside one wave was built concurrently by agents
with disjoint file ownership, against the signatures in §4. Nothing in a wave edits
`src/stratum/types.py`, `hooks.py`, `plugins.py` or `pyproject.toml`; proposed changes to those
are reported and applied between waves.

| Wave | Module | Owner files |
|---|---|---|
| 1 | Manifest, time, enumerations, built-in filters | `src/stratum/manifest/`, `src/stratum/time.py`, `src/stratum/classes.py`, `src/stratum/filters.py`, `tests/test_manifest.py`, `tests/test_time.py`, `tests/test_classes.py` |
| 1 | Access + index + EMIT readers | `src/stratum/access/`, `src/stratum/index/`, `src/stratum_emit/readers/`, `tests/test_readers.py`, `tests/test_index.py`, `tests/conftest.py` (additive) |
| 1 | Cache + regrid | `src/stratum/cache/`, `src/stratum/regrid/`, `tests/test_cache.py`, `tests/test_regrid.py` |
| 1 | Reduce | `src/stratum/reduce/`, `tests/test_reduce.py` |
| 2 | Resolve | `src/stratum/resolve/`, `src/stratum_emit/masks/` (finish `L2AStandard`, `EdgeTrim` width from `VarSpec`), `tests/test_resolve.py` |
| 2 | Publish | `src/stratum/publish/`, `tests/test_publish.py` |
| 3 | Plan, local executor, CLI, end-to-end | `src/stratum/plan/`, `src/stratum/executors/`, `src/stratum/cli.py`, `tests/test_invariants.py`, `tests/test_e2e.py`, `examples/` |
| 4 | Trial run + parity; spec/doc sync; adversarial review | `trial-data/` (read), `docs/`, `CLAUDE.md`, `README.md` |

## 4. Internal contracts

These are the signatures modules agreed on before any of them existed. The code is authoritative
once written; this list is the reason the pieces fit.

### Storage layout (local root)

`outputs.bucket` in the manifest is a path or URI; locally it is the **root**.

```
{root}/cache/{artifact_type}/{grid_id}/{tx}_{ty}/{hash16}[.tif | /]   content-addressed, shared
{root}/runs/{run_id}/            manifest.merged.yaml, index.parquet, plan.json,
                                 work/{regrid,resolve,reduce,publish}.jsonl, report.md, provenance.json
{root}/products/{run_id}/{tx}_{ty}/{period}/   data COGs, image, legend.json, classes.json, item.json
```

`artifact_type` is one of `glt`, `snapshot`, `product`. A GLT is one COG. A snapshot and a
product block are **directories** of single-band GeoTIFFs (`{layer}.tif`, `score.tif`,
`valid.tif` …) because layers have different dtypes; the directory is written whole to a temp name
and renamed, so a partial write never reads as a hit. Every artifact has `.inputs.json` beside it
(a GLT: `{hash16}.inputs.json`; a directory: `{hash16}/.inputs.json`).

### Work items (JSONL, one per line)

```
regrid : {"granule_id", "collection", "asset", "uri", "tile": [tx, ty]}
resolve: {"tile": [tx, ty], "epoch": [start_iso, end_iso], "block": [bx, by]}
reduce : {"tile": [tx, ty], "block": [bx, by], "period": [start_iso, end_iso], "epochs": [[s, e], ...]}
publish: {"tile": [tx, ty], "period": [start_iso, end_iso]}
```

`plan.json` carries everything a worker needs that is not in the manifest: the grid id, the
snapshot schema hashes, per-granule `GranuleRef` fields, the per-granule raw→product class
lookups, the geolocation role, and the resolved scorer/mask refs with their versions.

### Manifest (`stratum.manifest`)

```python
class Manifest(BaseModel)                      # the whole document; extra="forbid" everywhere
load_manifest(path: Path, patches: Sequence[Path] = ()) -> Manifest   # patches non-empty -> NotImplementedError
manifest_hash(m: Manifest) -> str              # canonical_hash of model_dump(mode="json")
m.run_label: str                               # the `run_id` field as written
m.run_id: str                                  # f"{run_label}-{manifest_hash[7:15]}"
m.grid_def() -> GridDef
m.epochs() -> list[Epoch]                      # from time.start by time.epoch, half-open, UTC
m.delivery_periods() -> list[DeliveryPeriod]   # (start, end, epochs) honouring deliver.{every,window,align}
m.snapshot_schema() -> SnapshotSchema          # classes resolved: "@ref:path" -> Enumeration -> ClassTable
m.geolocation_role() -> str                    # inputs.geolocation, else first sensor-space role in order
```

`aoi` accepts one of `zones` (names in `aoi.registry`, a YAML of name → bbox), `bbox`
`[w, s, e, n]`, or `tiles` `[[tx, ty], ...]`. `outputs.bucket` may be a plain path.

### Time (`stratum.time`)

```python
parse_duration("P13M") -> Duration(years=0, months=13, days=0)   # P#Y, P#M, P#D, P#W and combinations
Duration.add(dt) -> datetime;  Duration.multiple_of(other) -> bool
epochs_between(start, end, epoch: Duration) -> list[Epoch]
delivery_periods(start, end, epoch, every, window, align) -> list[DeliveryPeriod]
```

### Enumerations (`stratum.classes`)

```python
@dataclass(frozen=True) class Member: attrs: Mapping[str, Any]
@dataclass(frozen=True) class ClassDef: id: int; name: str; members: tuple[Member, ...]
@dataclass(frozen=True) class Enumeration:
    name; version; match_on: tuple[str, ...]; unmapped: str; classes: tuple[ClassDef, ...]
    def class_table(self) -> ClassTable          # key "id", columns id, name — the product table; enters layers_hash
    def resolve(self, raw: ClassTable) -> Remap  # per-granule; raises EnumerationError listing zero/multi matches and unmapped raw rows
load_enumeration(path) -> Enumeration
identity_enumeration(raw: ClassTable) -> Enumeration   # `classes: source`
@dataclass class Remap: lookup: np.ndarray       # raw key -> product id; index = raw key value; -1 = unmapped
                        raw_fingerprint: str; enumeration: str
```

Product id `0` is reserved for `none` and raw value `0` maps to it without being listed.

### Filters (`stratum.filters`)

```python
build_filters(manifest) -> list[GranuleFilter]   # max_cloud_fraction/on_missing, max_solar_zenith, month_in, build_version, plus {ref, params}
apply_filters(frame, filters) -> (frame, list[FilterReport])   # FilterReport(describe, removed, on_missing)
```

### Access (`stratum.access`)

```python
class AssetStore:                              # store.py
    def __init__(self, scratch: Path | None = None)
    def open(self, uri: str, *, etag: str | None = None) -> AssetHandle   # file:// and bare paths; others NotImplementedError
    def stage(self, uri) -> Path
    def credentials_for(self, uri) -> Credentials
reader_for(collection: str, overrides: Mapping[str, str] = {}) -> GranuleReader   # readers.py; instance, cached per process
class LocalSource:                             # sources.py
    def __init__(self, root: Path, patterns: Mapping[str, Mapping[str, str]])   # collection -> asset -> glob
    def search(...) -> Iterator[GranuleRecord]  # one record per (collection, granule_id); header attrs, bbox polygon
    def assets(record) -> Mapping[str, str]     # asset name -> file:// URI
```

`granule_id` is the filename with the asset prefix and `.nc` stripped, e.g.
`001_20260210T210759_2604114_009`. Header attributes read are NCEI/ACDD names
(`time_coverage_start/end`, `*most_longitude/latitude`, `software_build_version`,
`product_version`, `day_night_flag`); `cloud_fraction` is `None` for a local source, which is
meaningful ([02 §4](../specs/02-granule-index.md)).

### Index (`stratum.index`)

```python
INDEX_SCHEMA: pa.Schema                        # 02 section 2, geometry as WKB with GeoParquet metadata
build_index(source, *, collections, bbox=None, start=None, end=None) -> pa.Table
write_index(table, path); read_index(path) -> pd.DataFrame   # GranuleFrame; geometry as shapely
query_index(frame, *, bbox, start, end, collections) -> pd.DataFrame
freeze_index(frame, run_dir) -> (Path, str)    # index.parquet + sha256
granule_refs(frame) -> dict[str, GranuleRef]   # one per granule_id, assets merged across collections as "{collection}/{asset}"
role_uri(ref: GranuleRef, role: RoleSpec) -> str | None
```

### Cache (`stratum.cache`)

```python
class CacheRoot:
    def __init__(self, root: Path)
    def key(self, artifact: str, grid_id: str, tile: TileRef, inputs: Mapping) -> CacheKey   # CacheKey(path, inputs, hash)
    def hit(self, key) -> bool
    def write_file(self, key, write: Callable[[Path], None])    # temp + rename; writes .inputs.json
    def write_dir(self, key, write: Callable[[Path], None])
    def explain(self, key_or_path) -> dict;  def diff(a, b) -> dict[str, tuple]
glt_inputs(granule_id, grid, max_distance, regrid_method, regrid_algo_version) -> dict
snapshot_inputs(obs_keys, aux_keys, scorer_ref, scorer_version, scorer_params, layers_hash, epoch_bounds) -> dict
product_inputs(snapshot_keys, aux_keys, aggregate_hash, reducer=None) -> dict
```

### Regrid (`stratum.regrid`)

```python
REGRID_ALGO_VERSION = 1
build_glt(loc: LocArray, tile: TileRef, *, max_distance: float | None, n_workers: int = 1) -> np.ndarray   # (H, W, 3) int32 full tile; wraps spectral_util.mosaic.mosaic.find_subgrid_locations + remove_negatives(clean_contiguous=True)
write_glt(path, glt, tile, *, granule_id, tags) ; read_glt(path, window: Window | None) -> np.ndarray
regrid_granule_tile(cache, tile, granule_id, loc: Callable[[], LocArray], *, max_distance, regrid_method) -> CacheKey
module_content_hash() -> str                   # for tests/test_invariants
```

`max_distance=None` means 1.5 × the grid diagonal, computed by Stratum and recorded in the key
as the number actually used.

### Resolve (`stratum.resolve`)

```python
gather(arr: np.ma.MaskedArray, glt_win: np.ndarray, sw: SensorWindow, ok: np.ndarray | None) -> Gathered(band, valid, interpolated)
read_observation(ctx) -> ObsWindow | None      # one granule, one block; None when the GLT has no hit in the window
resolve_block(item, plan: PlanContext) -> CacheKey        # streaming argmax; writes the snapshot directory
write_snapshot(dir, *, layers, score, valid, schema, transform, crs); read_snapshot(dir, window=None) -> dict
stack_snapshots(dirs, epochs, schema, window) -> SnapshotStack
```

### Reduce (`stratum.reduce`)

```python
delivered_bands(schema: SnapshotSchema) -> tuple[BandSpec, ...]    # <layer>, <layer>_agreement, <layer>_runner_up, <layer>_n, <layer>_spread, n_epochs
reduce_stack(snaps: SnapshotStack) -> dict[str, np.ndarray]        # the built-in reducer; 13 section 4 exactly
```

Delivered dtypes/nodata: categorical `uint16`, nodata `65535` (0 is the `none` class);
`_agreement` and continuous bands `float32`, nodata `NaN`; counts `uint16`, no nodata (0 is a
real count); `_runner_up` `uint16`, nodata `65535`.

### Publish (`stratum.publish`)

```python
stitch(product_dirs: Sequence[tuple[BlockRef, Path]], tile, bands) -> BandStack
write_data_cogs(out_dir, stack, tile, *, class_table: ClassTable | None, tags) -> list[Path]
render(manifest_outputs, stack, schema) -> dict[str, np.ndarray]   # config mappers: categorical, continuous
write_legend(out_dir, layer, enumeration) ; write_stac_item(out_dir, ...) ; write_provenance(run_dir, ...)
```

## 5. Decisions made while building

Recorded here first; each is also reflected in the spec named.

| Decision | Spec |
|---|---|
| Regrid takes `loc` from one role — `inputs.geolocation`, defaulting to the first sensor-space role in manifest order — because every sensor-space asset of a granule shares one geolocation | 03 §3, 09 §2 |
| Snapshots and product blocks are directories of single-band GeoTIFFs, not one multi-band file, because layers have distinct dtypes | 06 §2 |
| A local `outputs.bucket` path is the storage root; `cache/`, `runs/`, `products/` hang off it exactly as they would in S3 | 06 §4, 08 §1 |
| `LocalSource` takes `patterns: {collection: {asset: glob}}`; the filename minus the asset prefix is the granule id | 12 §5 |
| `aoi` accepts `zones` (registry), `bbox`, or `tiles` | 09 §2 |
| Delivered categorical nodata is `65535`; `none` stays `0` | 13 §4, 11 §2 |
