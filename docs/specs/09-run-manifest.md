# 09 — Run Manifest

**Status:** draft · **Depends on:** all component specs · **Depended on by:** [10](10-provenance.md)

One declarative document is the entire interface for driving a run.

---

## 1. Principle

> **Terraform owns the platform. The manifest owns the science.**

Confusing the two is how platforms become unusable by the people they were built for: science
parameters leak into HCL, and then every experiment needs an infrastructure PR. The manifest is
versioned in git, reviewed like code, and hashed into provenance.

| Terraform | Manifest | Neither — just code |
|---|---|---|
| ECR, image build/push | Grid, tiling, blocks | The framework package |
| Batch compute envs, queues | AOI and zones | The plugin package |
| Lambda functions, SFN definition | Time range, epochs, delivery windows | Contracts, accessors, readers |
| S3 buckets + lifecycle | Filters, masks, scorer, snapshot schema, reducer, mapper | Tests and fixtures |
| IAM roles, EDL secret | Aux sources | |
| Budgets, alarms | Budget ceilings, output formats | |

---

## 2. Example

```yaml
run_id: cm-zones-2026-annual-r3
description: Critical Minerals annual mineral ID, 4 pilot zones

grid:
  crs: EPSG:4326
  resolution: [0.000277778, -0.000277778]   # one arcsecond
  tile_size: 1.0
  origin: [-180, -90]
  block: 720                  # divides the 3600-cell tile exactly
  max_distance: 0.00059       # regrid cutoff; default 1.5 x the grid diagonal - see 03 section 3
  regrid_method: kdtree       # kdtree | warp_embedded - see 03 section 3
  # force_positive_y: true    # only to defeat the guard rail in 01 section 1

aoi:
  zones: [bingham-canyon, cuprite-nv, leadville-co, south-central-az]
  registry: ../zones.yaml     # zone name -> [w, s, e, n]; relative to this file

time:
  start: 2022-08-01
  end:   2026-07-31
  epoch: P1M              # the voting unit - one snapshot per epoch
  deliver: P1Y            # == {every: P1Y, window: P1Y, align: exact}

# Long form, for a rolling composite delivered more often than the window slides:
#
# time:
#   epoch: P1M
#   deliver: {every: P1M, window: P13M, align: center}

inputs:
  index: s3://emit-l3/index/emit-granules.parquet   # what a RUN reads
  source:                                           # how that index is BUILT - see 12
    kind: cmr
    provider: LPCLOUD
    prefer: direct                                  # direct (s3://) | https
  roles:
    geometry:       {collection: EMITL1BOBS,  var: obs}
    mineral:
      collection: EMITL2BMIN
      var: group_1_mineral_id
      class_table:                        # where this product keeps its own table
        source: embedded
        path: /mineral_metadata
        key: index
        attributes: [name, record, library, group, url]
    mineral_depth:  {collection: EMITL2BMIN,  var: group_1_band_depth}
    mineral_uncert: {collection: EMITL2BMIN,  asset: MINUNCERT, var: group_1_band_depth_unc}
    mask:           {collection: EMITL2AMASK, var: mask}
    frcov:          {collection: EMITL2BFRCOV, var: soil}   # already orthorectified
  # geolocation: geometry                    # the role regrid takes loc from; default: the
  #                                          # first sensor-space role above - see 03 section 3
  band_aliases:                 # 0-based band indices into the L1B OBS `obs` variable:
    view_zenith:  {role: geometry, band: 2}   # 2 = to-sensor zenith
    solar_zenith: {role: geometry, band: 4}   # 4 = to-sun zenith
    # swir_2200:  {role: reflectance, match: {wavelength: 2200}, tolerance: 10}   # by reader-reported attribute

aux:
  slope: {uri: "s3://.../slope.tif", kind: continuous,  resampling: bilinear}
  snow:  {uri: "s3://.../snow/{date}.tif", kind: categorical, resampling: nearest,
          temporal: nearest, max_age: P3D}

# allow_mixed_vintage: true           # top level; requires mixed_vintage_reason - see section 5
# mixed_vintage_reason: "..."

granule_filter:
  # - {build_version: "010635"}         # filterable, not required: the vintage check is the
  #                                     # class-table fingerprint - see 02 section 3
  - {max_cloud_fraction: 0.5, on_missing: fail}
  - {max_solar_zenith: 70}
  - {month_in: [8, 9, 10, 11]}          # recurring seasonal window, not an interval

pixel_mask:
  - {ref: edge_trim, columns: 7}               # sensor-space; detector edges
  - {ref: l2a_standard, flags: [cloud, cirrus, water, spacecraft]}
  - {ref: soil_fraction, min_soil: 0.65}       # refs: entry-point name, or module:Class

scorer:
  ref: cleanest_nadir
  params: {slope_penalty: 0.5}

snapshot:                                 # what resolve writes and how reduce collapses it - see 13
  name: cm-v1
  layers:
    mineral_1:
      kind: categorical
      source: mineral
      classes: "@ref:classes/cm-v1.yaml"  # the product enumeration; also the legend
      aggregate: {method: vote, min_count: 3, ignore: [none], tie_break: highest_score}
    depth_1:
      kind: continuous
      source: mineral_depth
      aggregate: {method: inverse_variance, unc: depth_1_unc, conditional_on: mineral_1, spread: iqr}
    depth_1_unc:
      kind: continuous
      source: mineral_uncert
      aggregate: {method: none}
    view_zenith:
      kind: continuous
      source: view_zenith
      aggregate: {method: none}

# reducer:                                # only for logic the schema vocabulary cannot express
#   ref: my_package:ClassifyLast

outputs:
  bucket: s3://emit-l3-products
  formats: [cog, netcdf]
  stac: true
  render:
    mineral_1:
      mapper: categorical                 # classes come from the layer's enumeration
      on_unmapped: fail
      alpha_from: {band: mineral_1_agreement, domain: [0.3, 0.8], range: [60, 255]}

plugins:
  wheel: s3://emit-l3/plugins/stratum_emit-0.4.2-py3-none-any.whl

budget:
  max_tiles: 64
  max_granules: 12000
  max_vcpu_hours: 400
  on_exceed: require_approval
```

### Three fields that exist only to defeat a default

`grid.max_distance`, `grid.force_positive_y` and top-level `allow_mixed_vintage` are all overrides
of something the framework would otherwise decide or refuse. They are named here rather than left
implicit because `max_distance` enters the GLT cache key ([06 §2](06-caching.md)) — an input that
determines an artifact must be settable — and because the other two are the documented escape
hatches for guard rails in [01 §1](01-grid-tiling.md) and [02 §3](02-granule-index.md).
`allow_mixed_vintage` needs its "documented reason" as a concrete field, `mixed_vintage_reason`;
one without the other is a schema error. `force_positive_y` is accepted by the model but
`GridDef` cannot honour it, so `grid_def()` raises `NotImplementedError` — the guard rail cannot
be bypassed without a shared-type change.

### Forms the models fix

The Pydantic models are `src/stratum/manifest/models.py`; `extra="forbid"` everywhere, so a
misspelt key is an error with a location. Choices the build settled:

| Field | Contract |
|---|---|
| `aoi` | Exactly one of `zones` (names looked up in `registry`, a manifest-relative YAML of `name → [w, s, e, n]`), `bbox` `[w, s, e, n]`, or `tiles` `[[tx, ty], ...]`. `tiles()` is the **union** of the tiles meeting each zone box, not the tiles of the enclosing box, so two distant zones do not tile the gap between them; a box edge exactly on a tile edge does not pull in the next tile. A zone not in the registry, or `zones` without `registry`, is a `validate_static` problem. `geometry` (a polygon file) is not modelled yet |
| `time.deliver` | Normalised to the long form at load, so `P1Y` and `{every: P1Y, window: P1Y, align: exact}` produce the same merged document and the same hash; `window` defaults to `every`, and `deliver` itself defaults to the epoch. The rules in §5 are checked by the model |
| `time.align` | `start` (default) or `calendar`: `calendar` anchors the epoch lattice on the epoch's calendar unit and truncates the first epoch at `start`. Month arithmetic is computed from `start` in one step and clamps to month end (31 Jan + 2 × P1M = 31 Mar); month- and day-based durations are incommensurable. `center` places surplus epochs half before and half after, the odd one after; windows clip to `[start, end)` |
| `inputs.geolocation` | The role regrid takes `loc` from; defaults to the first sensor-space role in `inputs.roles` order ([03 §3](03-regrid-glt.md)). Must name a role |
| `inputs.index` / `inputs.source` | Both optional, at least one required. `source` for `kind: local` takes `root` plus either one `pattern` (honoured only when every role reads one collection) or `patterns` ([12 §5](12-data-access.md)) |
| `band_aliases` | `band:` is a **0-based** index, validated against the reader's band count at plan time; `match:` selects the single band whose reported attribute equals the value (string comparison, or within `tolerance`) and fails unless exactly one matches ([11 §5](11-types.md)). EMIT L1B OBS: 0 path length, 1 to-sensor azimuth, **2 to-sensor zenith**, 3 to-sun azimuth, **4 to-sun zenith**, 5 phase, 6 slope, 7 aspect, 8 cosine i, 9 UTC time, 10 earth–sun distance |
| `granule_filter` | The four built-ins plus `product_version`, `collection_version` and `day_night`. `on_missing` is `reject \| keep \| fail`, `fail` when omitted — so `{max_solar_zenith: 70}` is valid and means `fail`; `on_missing` on `month_in` is a schema error, since nothing can be missing. A `{ref, params}` entry resolves a `GranuleFilter` plugin |
| `snapshot.layers.*.aggregate` | Parameters are validated per `(kind, method)`: `vote` takes `min_count` / `ignore` / `tie_break`; `percentile` requires `p` in `[0, 100]`; `inverse_variance` requires `unc`; `conditional_on` and `spread` apply to any delivered continuous method and not to `none`. A parameter a method does not take is an error ([13 §4](13-snapshot-schema.md)) |
| `budget.on_exceed` | `require_approval` (default) \| `fail` \| `warn` — §4 |

---

## 3. Composition

AMD's layered config is a good idea and we keep it. `amd config print -c base.yml -p
"mosaic<-v6<-filter-fit"` composes a base plus an ordered patch chain, so a variant is a patch
rather than a copied file that drifts.

```
stratum plan -m base.yaml -p zones-pilot -p scorer-v3
```

Implemented as an explicit ordered dict-merge **before** validation, so the merged document is
what gets validated and hashed. Lists replace rather than append — appending is convenient once
and confusing forever.

---

## 4. Budget as a first-class field

Not decoration. The planner computes the real fan-out — tiles × epochs × blocks × granules —
**before any compute is provisioned**, compares it to the ceiling, and on exceedance parks the
execution on a Step Functions task token awaiting human approval.

The difference between one zone and the globe is one line of YAML. A typo in a date range is
otherwise a five-figure mistake, and this pipeline actively invites that mistake.

`stratum plan` emits a **dry-run report** by default — tile count, granule count, estimated bytes
read, projected cache hit rate, estimated vCPU-hours — which should be reviewed in the PR
alongside the manifest diff.

**The gate as built** (`stratum/plan/run.py`, `stratum/executors/local.py`): `plan_run` never
raises on budget. It records `{over, problems, on_exceed}` in `plan.json` and the report,
`stratum plan` exits `2` when over, and `stratum run` refuses before the first stage unless
`on_exceed: warn`. `require_approval` refuses too, naming `stratum approve` as the later slice
([08 §3](08-execution.md)); `warn` proceeds with the exceedance reported. Only `max_tiles` and
`max_granules` gate today; vCPU-hours are reported as "not estimated" until per-stage constants
exist ([08 §2](08-execution.md)). Two selection rules sit in front of the count: the query bbox
is the AOI's enclosing box, but the selection is then **clipped to the AOI's tiles** and the
granules in the gap are reported as a "meets a tile of the AOI" filter row, so `granule_count`
and the budget count what a work item can actually read ([02 §6](02-granule-index.md)); and an
empty selection is a `PlanError` ("no granule survives selection"), not an empty plan — the
data-dependent checks need a sample granule, and an empty run is almost always a wrong `aoi` or
`time`.

---

## 5. Validation at plan time

Everything below fails in the plan stage, loudly, while it is cheap:

- schema validity (Pydantic v2) with precise error locations;
- named scorer/reducer/mapper/mask plugins resolve, and versions are recordable;
- every plugin's `required_roles` present in `inputs.roles`;
- every role's `collection` resolves to exactly one registered `GranuleReader`, and its `var`
  exists in that reader's variables ([12 §7](12-data-access.md)); a collection the index holds
  under more than one `collection_version` is pinned with `version:`;
- every asset URI scheme is supported and credentials for it are obtainable now;
- every plugin's `required_aux` declared in `aux` — undeclared reads are refused
  ([05 §5](05-ancillary-data.md));
- aux sources exist and are readable;
- `kind`/`resampling` consistent for each aux source;
- `capability` and `halo` consistent with `block`;
- `deliver.every` and `deliver.window` are whole multiples of `epoch`, and
  `window >= every`; `deliver.align` is `exact` only when `window == every`;
- the snapshot schema resolves: every layer `source` names a role or alias, every categorical
  layer's enumeration resolves to **exactly one** row per member in each contributing granule's
  embedded table — matched on attributes, never on positional index — `conditional_on` and `unc`
  name declared layers, and every class a colour table names exists in the enumeration
  ([13](13-snapshot-schema.md), [07 §6](07-output-mapping.md));
- the contributing granules' class tables **agree by fingerprint**, or a cross-vintage remap is
  explicitly permitted ([11 §9](11-types.md));
- filter `on_missing` policy explicit;
- **the vintage check** is that fingerprint agreement. A disagreement fails the plan unless
  `allow_mixed_vintage: true` is set with a documented reason, and then only if every raw table
  resolves fully into the enumeration ([13 §3](13-snapshot-schema.md)); `build_version` is
  reported, not pinned ([02 §3](02-granule-index.md));
- budget present and non-infinite.

### Where each check lives

Three layers, in the order a manifest meets them:

| Layer | Checks | Reports |
|---|---|---|
| **Models** (`stratum/manifest/models.py`) | The shape of one block: types, enumerated values, which fields go together, aux `kind`/`resampling` agreement, the time rules, a categorical layer needing `classes`, `mixed_vintage_reason` | The first failure, with a location |
| **`validate_static`** (`stratum/manifest/validate.py`; `stratum validate`) | Every cross-block reference, with no data: role/alias collision; `alias.role` and `geolocation` declared; scorer/mask/filter/mapper refs resolve and accept their params; `required_roles` in roles or aliases; `required_aux` in `aux`; scorer `halo` vs `block`; layer `source` in the namespace; a categorical source role declares a `class_table` whose attributes cover `match_on`; `conditional_on` names another *delivered* categorical layer and `unc` another continuous one; `ignore`/`colors` name enumeration classes; render targets a delivered layer of the right kind; `alpha_from` names a band the reducer actually delivers; zones resolve | Every problem, as a list |
| **Planner** (`stratum/plan/run.py`) | What needs data: the collection resolves to one reader; `var` exists in its `variables()`; band aliases resolve against a real granule's `VarSpec`; per-granule enumeration resolution and fingerprint agreement; a collection under several `collection_version`s is pinned; every granule provides an asset for every role that will be read | `PlanError` |

`validate_static` instantiates the scorer and masks with their params to read `required_roles`,
`halo` and `capability`, so a plugin constructor with side effects runs at validation; the
shipped plugins are pure. The checks that the slice cannot back are refused *here* rather than
in a worker: `aux` (and any `required_aux`), `formats: [netcdf]`, a `reducer:` block, a
non-`streaming` scorer. `-p` patches, `threshold`/`composite` mappers and `class_table.source:
file` raise `NotImplementedError` naming their section.

### Hash and run id

`manifest_hash` is `canonical_hash` of the merged document as the models dump it — key order
in the YAML never changes it, `deliver` always in long form — **plus** `resolved_refs`: the
fingerprint of every enumeration a `@ref:` resolved to. Editing a classes file under the same
path therefore yields a new `run_id`, as [00 §5](00-overview.md) invariant 4 requires; a manifest
without `@ref:` hashes as its document alone. The zone registry's contents do not enter the
hash. `run_id` is `f"{run_label}-{hash[7:15]}"` — the YAML `run_id` plus the first eight hex
digits after the `sha256:` prefix.

---

## 6. Open questions

1. ~~YAML or TOML?~~ **Resolved:** YAML, with a strict loader and no implicit typing.
2. ~~Should `aoi.zones` resolve from a checked-in registry, or take inline geometry?~~ **Resolved:**
   a checked-in registry, so runs are comparable and names stable — `aoi.registry`, a YAML of
   `name → [w, s, e, n]` relative to the manifest (`examples/zones.yaml`).
3. ~~Do we version the manifest schema itself, so old manifests keep parsing?~~ **Resolved:** yes —
   `schema_version` at the top, `1.0`; any other value is refused.
4. ~~Should `run_id` be user-supplied or derived from the manifest hash?~~ **Resolved:** both:
   `{label}-{hash[7:15]}` (§5).
5. `aoi.geometry` — a polygon file — appears in the example on the site but is not modelled;
   `extra="forbid"` rejects it. Add it when a consumer needs it.
6. `EMITL1BOBS` is a local collection name only: CMR has no such short name — the OBS file is
   the second asset of an `EMITL1BRAD.001` record. A `CMRSource` must map
   `{collection: EMITL1BRAD, asset: OBS}` onto it ([12 §5](12-data-access.md), question 7), and
   the example manifests will have to say so once that source exists.
7. The zone registry's contents are not in the manifest hash, so moving a zone's box keeps the
   `run_id`. The frozen index still changes, and provenance records it; whether run identity
   should track the registry the way it tracks `@ref:` enumerations is a one-line change in
   `Manifest.document()` if wanted.
